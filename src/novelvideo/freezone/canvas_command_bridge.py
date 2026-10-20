"""Durable Inbox/Outbox for frontend-executed Freezone canvas commands.

Hermes tools run on the backend, while Freezone canvas commands are applied by
the browser. SQLite is the transport source of truth; pending/result JSON files
remain as a temporary compatibility mirror for legacy workers.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from novelvideo.sqlite_pragmas import configure_sqlite_connection

BRIDGE_RESULT_TTL_SECONDS = 24 * 60 * 60
BRIDGE_DELIVERY_LEASE_SECONDS = 15
BRIDGE_OWNER_LEASE_SECONDS = 45
BRIDGE_PENDING_TTL_SECONDS = {
    # Commands include a browser approval (up to five minutes), validation,
    # and receipt delivery. A 75-second transport TTL expired a live approval
    # before the user confirmed it, causing the agent to submit a second run.
    # Keep this aligned with the default tool wait, not generation duration:
    # long-running actions return an accepted receipt and leave this inbox.
    "canvas_command": 10 * 60,
    "canvas_context": 45,
    "skill_studio_event": 10 * 60,
    "clarification_event": 10 * 60,
}
_BRIDGE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS canvas_bridge_browsers (
    consumer_id TEXT PRIMARY KEY, binding_json TEXT NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS canvas_bridge_producer_turns (
    scope_json TEXT NOT NULL, turn_id TEXT NOT NULL, expires_at REAL NOT NULL,
    PRIMARY KEY(scope_json,turn_id)
);
CREATE TABLE IF NOT EXISTS canvas_command_messages (
    bridge_key          TEXT PRIMARY KEY,
    kind                TEXT NOT NULL,
    project_id          TEXT,
    canvas_id           TEXT,
    request_fingerprint TEXT,
    payload_json        TEXT NOT NULL,
    result_json         TEXT,
    status              TEXT NOT NULL,
    consumer_id         TEXT,
    delivery_attempts   INTEGER NOT NULL DEFAULT 0,
    lease_expires_at    REAL,
    created_at          REAL NOT NULL,
    updated_at          REAL NOT NULL,
    expires_at          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_canvas_bridge_browsers_expiry ON canvas_bridge_browsers(expires_at);
CREATE INDEX IF NOT EXISTS idx_canvas_bridge_browsers_scope ON canvas_bridge_browsers(binding_json,expires_at);
CREATE INDEX IF NOT EXISTS idx_canvas_bridge_producers_expiry ON canvas_bridge_producer_turns(expires_at);
CREATE INDEX IF NOT EXISTS idx_canvas_command_messages_pending
ON canvas_command_messages(project_id, canvas_id, kind, status, created_at);
CREATE INDEX IF NOT EXISTS idx_canvas_command_messages_expiry
ON canvas_command_messages(expires_at);
"""
_TERMINAL_BRIDGE_STATUSES = {"applied", "failed", "cancelled", "expired"}
_BRIDGE_SCHEMA_READY_PATHS: set[Path] = set()
_BRIDGE_SCHEMA_LOCK = threading.Lock()


def _bridge_dir(bridge_dir: str | Path | None = None) -> Path:
    if bridge_dir:
        return Path(bridge_dir)
    root = os.environ.get("DRAMACLAW_CANVAS_COMMAND_BRIDGE_DIR", "").strip()
    if root:
        return Path(root)
    return Path(tempfile.gettempdir()) / "supertale_canvas_command_bridge"


def _bridge_db_path(bridge_dir: str | Path | None = None) -> Path:
    return _bridge_dir(bridge_dir) / "canvas_command_inbox.sqlite3"


@contextmanager
def _bridge_db(bridge_dir: str | Path | None = None):
    path = _bridge_db_path(bridge_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    database_existed = path.exists()
    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    configure_sqlite_connection(conn, set_journal_mode=False)
    if not database_existed or path not in _BRIDGE_SCHEMA_READY_PATHS:
        with _BRIDGE_SCHEMA_LOCK:
            if not database_existed or path not in _BRIDGE_SCHEMA_READY_PATHS:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_BRIDGE_SCHEMA_SQL)
                _BRIDGE_SCHEMA_READY_PATHS.add(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _decode_bridge_json(value: Any) -> dict[str, Any] | None:
    try:
        decoded = json.loads(str(value or ""))
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _terminal_status(result: dict[str, Any]) -> str:
    if result.get("canvas_apply_status") == "timeout":
        return "expired"
    if result.get("cancelled") is True:
        return "cancelled"
    if result.get("ok") is True and result.get("applied") is not False:
        return "applied"
    return "failed"


def _prune_bridge_rows(
    conn: sqlite3.Connection,
    *,
    now: float,
    bridge_dir: str | Path | None = None,
) -> int:
    # Filter actual kind deadlines before LIMIT, avoiding an old long-TTL
    # prefix starving newer short-TTL messages during bounded cleanup.
    kind_ttl = "CASE kind " + " ".join("WHEN ? THEN ?" for _ in BRIDGE_PENDING_TTL_SECONDS) + " ELSE 600 END"
    parameters = [value for pair in BRIDGE_PENDING_TTL_SECONDS.items() for value in pair]
    expirable = conn.execute(
        "SELECT bridge_key,kind,request_fingerprint,created_at FROM canvas_command_messages "
        "WHERE status IN ('pending','delivered') AND created_at + " + kind_ttl + " <= ? ORDER BY created_at LIMIT 200",
        [*parameters, now],
    ).fetchall()
    for row in expirable:
        ttl = BRIDGE_PENDING_TTL_SECONDS.get(str(row["kind"]), 10 * 60)
        if float(row["created_at"]) + ttl > now:
            continue
        result = {
            "key": str(row["bridge_key"]),
            "bridge_key": str(row["bridge_key"]),
            "resolved_at": now,
            "ok": False,
            "tool_call_status": "failed",
            "canvas_apply_status": "timeout",
            "applied": False,
            "cancelled": True,
            "errors": ["Persistent canvas inbox message expired before completion."],
            **(
                {"request_fingerprint": str(row["request_fingerprint"])}
                if row["request_fingerprint"]
                else {}
            ),
        }
        conn.execute(
            """
            UPDATE canvas_command_messages
               SET status = 'expired', result_json = ?, updated_at = ?,
                   lease_expires_at = NULL
             WHERE bridge_key = ? AND status IN ('pending', 'delivered')
            """,
            (_canonical_json(result), now, row["bridge_key"]),
        )
        # JSON is only a migration mirror. Once SQLite expires a message, its
        # mirror must not survive long enough to be mistaken for an unmigrated
        # command after the durable tombstone is eventually pruned.
        _unlink_if_exists(_path("pending", str(row["bridge_key"]), bridge_dir))
    cursor = conn.execute(
        "DELETE FROM canvas_command_messages WHERE bridge_key IN (SELECT bridge_key FROM canvas_command_messages WHERE expires_at<=? ORDER BY expires_at LIMIT 200)",
        (now,),
    )
    return max(int(cursor.rowcount or 0), 0)


def bridge_status_counts(*, bridge_dir: str | Path | None = None) -> dict[str, int]:
    """Return observable queue depth by durable transport status."""
    with _bridge_db(bridge_dir) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _prune_bridge_rows(conn, now=time.time(), bridge_dir=bridge_dir)
        rows = conn.execute(
            "SELECT status, COUNT(*) AS count FROM canvas_command_messages GROUP BY status"
        ).fetchall()
    return {str(row["status"]): int(row["count"] or 0) for row in rows}


def read_bridge_message(
    key: str, *, bridge_dir: str | Path | None = None
) -> dict[str, Any] | None:
    key = canonical_bridge_key(key)
    with _bridge_db(bridge_dir) as conn:
        row = conn.execute(
            "SELECT * FROM canvas_command_messages WHERE bridge_key = ?",
            (key,),
        ).fetchone()
    if row is None:
        return None
    payload = _decode_bridge_json(row["payload_json"])
    if payload is None:
        return None
    return {
        **payload,
        "key": str(row["bridge_key"]),
        "kind": str(row["kind"]),
        "transport_status": str(row["status"]),
        "result": _decode_bridge_json(row["result_json"]),
    }


def find_clarification_bridge_message(
    *,
    project_id: str | None,
    canvas_id: str | None,
    clarification_id: str,
    bridge_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    """Locate the newest live clarification card for one clarification id.

    Returns the stored pending payload plus ``key``, ``transport_status`` and,
    once the frontend answered, ``result``. Used to resume waiting on a card
    after the tool call that created it timed out, so the user's later answer
    is collected instead of a second card being shown. An expired card (its
    pending TTL passed before anyone answered) is reported as absent.
    """
    wanted = str(clarification_id or "").strip()
    if not wanted:
        return None
    now = time.time()
    with _bridge_db(bridge_dir) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _prune_bridge_rows(conn, now=now, bridge_dir=bridge_dir)
        rows = conn.execute(
            "SELECT * FROM canvas_command_messages "
            "WHERE project_id = ? AND canvas_id = ? AND kind = 'clarification_event' "
            "ORDER BY created_at DESC LIMIT 50",
            (project_id or "", canvas_id or ""),
        ).fetchall()
    for row in rows:
        payload = _decode_bridge_json(row["payload_json"])
        actor = os.environ.get("DRAMACLAW_USERNAME", "").strip()
        profile = os.environ.get("DRAMACLAW_AGENT_PROFILE", "")
        agent = os.environ.get("DRAMACLAW_AGENT_ID", "").strip() or (profile.split(":",1)[1] if ":" in profile else "main")
        if actor and (not payload or not isinstance(payload.get("producer_binding"),dict)
                      or payload["producer_binding"].get("username") != actor
                      or payload["producer_binding"].get("agent_id") != agent):
            continue
        event = payload.get("event") if payload else None
        if not isinstance(event, dict) or str(event.get("clarification_id") or "") != wanted:
            continue
        status = str(row["status"])
        if status == "expired":
            return None
        return {
            **payload,
            "key": str(row["bridge_key"]),
            "kind": str(row["kind"]),
            "transport_status": status,
            "result": (
                _decode_bridge_json(row["result_json"])
                if status in _TERMINAL_BRIDGE_STATUSES
                else None
            ),
        }
    return None


def bridge_message_exists(key: str, *, bridge_dir: str | Path | None = None) -> bool:
    """Return whether SQLite owns a key, even if its payload cannot be decoded."""
    with _bridge_db(bridge_dir) as conn:
        row = conn.execute(
            "SELECT 1 FROM canvas_command_messages WHERE bridge_key = ?",
            (key,),
        ).fetchone()
    return row is not None


def list_pending_bridge_messages(
    *,
    project_id: str,
    canvas_id: str,
    kinds: set[str] | None = None,
    created_after: float | None = None,
    limit: int = 100,
    bridge_dir: str | Path | None = None,
    producer_binding: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Read pending or redeliverable messages across API workers."""
    now = time.time()
    parameters: list[Any] = [project_id, canvas_id, now]
    where = (
        "project_id = ? AND canvas_id = ? AND "
        "(status = 'pending' OR (status = 'delivered' AND lease_expires_at <= ?))"
    )
    if producer_binding is not None:
        producer_where, producer_parameters = _producer_scope_sql(producer_binding)
        where += " AND " + producer_where
        parameters.extend(producer_parameters)
    if kinds:
        placeholders = ",".join("?" for _ in kinds)
        where += f" AND kind IN ({placeholders})"
        parameters.extend(sorted(kinds))
    if created_after is not None:
        where += " AND created_at >= ?"
        parameters.append(float(created_after))
    parameters.append(max(1, min(int(limit), 500)))
    with _bridge_db(bridge_dir) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _prune_bridge_rows(conn, now=now, bridge_dir=bridge_dir)
        rows = conn.execute(
            f"SELECT * FROM canvas_command_messages WHERE {where} "
            "ORDER BY created_at ASC LIMIT ?",
            parameters,
        ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        payload = _decode_bridge_json(row["payload_json"])
        if payload is None:
            continue
        items.append(
            {
                **payload,
                "key": str(row["bridge_key"]),
                "kind": str(row["kind"]),
                "transport_status": str(row["status"]),
                "created_at": float(row["created_at"]),
                "delivery_attempts": int(row["delivery_attempts"]),
            }
        )
    return items


def _producer_scope_sql(binding: dict[str, Any]) -> tuple[str, list[Any]]:
    """Filter verified producer identity before any bounded transport page."""
    fields = {"username", "project_id", "canvas_id", "agent_id", "turn_id"}
    if not set(binding).issubset(fields):
        raise ValueError("invalid producer binding fields")
    clauses = ["json_valid(payload_json)", "json_type(payload_json,'$.producer_binding')='object'",
               "COALESCE(json_extract(payload_json,'$.producer_binding.turn_id'),'')<>''"]
    parameters = []
    for field, value in binding.items():
        clauses.append(f"json_extract(payload_json,'$.producer_binding.{field}')=?")
        parameters.append(value)
    return " AND ".join(clauses), parameters


def mark_bridge_message_delivered(
    key: str,
    *,
    consumer_id: str,
    binding: dict[str, Any] | None = None,
    execution_token: str | None = None,
    bridge_dir: str | Path | None = None,
) -> bool:
    now = time.time()
    with _bridge_db(bridge_dir) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if binding is not None:
            row = conn.execute("SELECT payload_json,consumer_id FROM canvas_command_messages WHERE bridge_key = ?", (key,)).fetchone()
            payload = _decode_bridge_json(row["payload_json"]) if row else None
            producer = payload.get("producer_binding") if payload else None
            if producer and producer != binding:
                return False
            if (payload and payload.get("execution_token")
                    and float(payload.get("owner_expires_at") or 0) > now
                    and (row["consumer_id"] != consumer_id or payload["execution_token"] != execution_token)):
                return False
            previous_binding = payload.get("delivery_binding") if payload else None
            if previous_binding and previous_binding != binding:
                return False
        cursor = conn.execute(
            """
            UPDATE canvas_command_messages
               SET status = 'delivered', consumer_id = ?,
                   delivery_attempts = delivery_attempts + 1,
                   lease_expires_at = ?, updated_at = ?
             WHERE bridge_key = ?
               AND (status = 'pending' OR
                    (status = 'delivered' AND lease_expires_at <= ?))
            """,
            (
                consumer_id[:160],
                now + BRIDGE_DELIVERY_LEASE_SECONDS,
                now,
                key,
                now,
            ),
        )
        claimed = int(cursor.rowcount or 0) == 1
        if claimed and binding is not None:
            row = conn.execute("SELECT payload_json FROM canvas_command_messages WHERE bridge_key = ?", (key,)).fetchone()
            payload = _decode_bridge_json(row["payload_json"]) or {}
            payload["delivery_binding"] = binding
            if execution_token:
                payload["execution_token"] = execution_token
                payload["owner_expires_at"] = now + BRIDGE_OWNER_LEASE_SECONDS
            fingerprint = str(payload.get("request_fingerprint") or "")
            if not fingerprint:
                request = {name: value for name, value in payload.items() if name != "delivery_binding"}
                fingerprint = hashlib.sha256(_canonical_json(request).encode("utf-8")).hexdigest()
                payload["request_fingerprint"] = fingerprint
            conn.execute("UPDATE canvas_command_messages SET payload_json = ?, request_fingerprint = ? WHERE bridge_key = ?", (_canonical_json(payload), fingerprint, key))
        return claimed


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def canvas_command_bridge_key(
    *,
    project_id: str | None,
    canvas_id: str | None,
    commands: list[Any],
) -> str:
    payload = {
        "kind": "canvas_command",
        "project_id": project_id or "",
        "canvas_id": canvas_id or "",
        "commands": commands,
        "nonce": time.time_ns(),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()[:32]


def canvas_command_idempotency_key(
    *,
    project_id: str | None,
    canvas_id: str | None,
    commands: list[Any],
) -> str:
    """Return a stable key for replaying the same workflow write.

    Ordinary canvas commands keep the nonce-bearing bridge key above so each
    user action remains independent. Workflow submissions pass an explicit
    ``workflowInstanceId`` in every create-node command and use this stable
    variant to make transport retries idempotent.
    """
    payload = {
        "kind": "canvas_command_idempotency",
        "producer_actor": os.environ.get("DRAMACLAW_USERNAME", ""),
        "producer_agent": os.environ.get("DRAMACLAW_AGENT_ID", "") or os.environ.get("DRAMACLAW_AGENT_PROFILE", ""),
        "project_id": project_id or "",
        "canvas_id": canvas_id or "",
        "commands": commands,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()[:32]


def canvas_context_bridge_key(
    *,
    project_id: str | None,
    canvas_id: str | None,
    requests: list[Any],
) -> str:
    payload = {
        "kind": "canvas_context_request",
        "project_id": project_id or "",
        "canvas_id": canvas_id or "",
        "requests": requests,
        "nonce": time.time_ns(),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()[:32]


def skill_studio_bridge_key(
    *,
    project_id: str | None,
    canvas_id: str | None,
    event: dict[str, Any],
) -> str:
    payload = {
        "kind": "skill_studio_event",
        "project_id": project_id or "",
        "canvas_id": canvas_id or "",
        "event": event,
        "nonce": time.time_ns(),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()[:32]


def clarification_bridge_key(
    *,
    project_id: str | None,
    canvas_id: str | None,
    event: dict[str, Any],
) -> str:
    payload = {
        "kind": "clarification_event",
        "project_id": project_id or "",
        "canvas_id": canvas_id or "",
        "event": event,
        "nonce": time.time_ns(),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()[:32]


def _path(kind: str, key: str, bridge_dir: str | Path | None = None) -> Path:
    safe_key = "".join(ch for ch in key if ch.isalnum() or ch in {"-", "_"})[:128]
    return _bridge_dir(bridge_dir) / f"{safe_key}.{kind}.json"


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(_canonical_json(data), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _unlink_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _canvas_command_request_fingerprint(
    *,
    project_id: str | None,
    canvas_id: str | None,
    commands: list[Any],
) -> str:
    payload = {
        "project_id": project_id or "",
        "canvas_id": canvas_id or "",
        "commands": commands,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _put_pending_bridge_message(
    *,
    key: str,
    kind: str,
    project_id: str | None,
    canvas_id: str | None,
    payload: dict[str, Any],
    request_fingerprint: str | None = None,
    terminal_result: dict[str, Any] | None = None,
    bridge_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    origin = {
        "username": os.environ.get("DRAMACLAW_USERNAME", "").strip(),
        "project_id": os.environ.get("DRAMACLAW_PROJECT_ID", "").strip(),
        "canvas_id": os.environ.get("DRAMACLAW_CANVAS_ID", "").strip(),
        "agent_id": os.environ.get("DRAMACLAW_AGENT_ID", "").strip() or
            (os.environ.get("DRAMACLAW_AGENT_PROFILE", "").split(":", 1)[1]
             if ":" in os.environ.get("DRAMACLAW_AGENT_PROFILE", "") else "main"),
        "turn_id": os.environ.get("DRAMACLAW_TURN_ID", "").strip(),
    }
    if not origin["turn_id"] and all(value for key,value in origin.items() if key != "turn_id"):
        # A persistent Hermes worker has trusted actor/profile provenance but
        # no process-wide turn ID. Resolve only a unique, live host registration.
        scope = {key:value for key,value in origin.items() if key != "turn_id"}
        with _bridge_db(bridge_dir) as origin_db:
            turns = origin_db.execute("SELECT turn_id FROM canvas_bridge_producer_turns WHERE scope_json=? AND expires_at>?",(_canonical_json(scope),time.time())).fetchall()
        if len(turns)==1:
            origin["turn_id"] = str(turns[0]["turn_id"])
    if all(origin.values()) and origin["project_id"] == project_id and origin["canvas_id"] == canvas_id:
        payload = {**payload, "producer_binding": origin}
    now = time.time()
    with _bridge_db(bridge_dir) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _prune_bridge_rows(conn, now=now, bridge_dir=bridge_dir)
        existing = conn.execute(
            "SELECT * FROM canvas_command_messages WHERE bridge_key = ?",
            (key,),
        ).fetchone()
        if existing is not None:
            previous_payload = _decode_bridge_json(existing["payload_json"]) or {}
            previous_origin = previous_payload.get("producer_binding")
            next_origin = payload.get("producer_binding")
            if previous_origin != next_origin and (previous_origin or next_origin):
                return _canvas_command_idempotency_conflict(key=key,project_id=project_id,canvas_id=canvas_id)
            existing_fingerprint = str(existing["request_fingerprint"] or "")
            if request_fingerprint and existing_fingerprint != request_fingerprint:
                return _canvas_command_idempotency_conflict(
                    key=key, project_id=project_id, canvas_id=canvas_id
                )
            if str(existing["status"]) in _TERMINAL_BRIDGE_STATUSES:
                return _decode_bridge_json(existing["result_json"])
            return None
        if terminal_result is not None:
            resolved = {
                "key": key,
                "bridge_key": key,
                "resolved_at": now,
                **(
                    {"request_fingerprint": request_fingerprint}
                    if request_fingerprint
                    else {}
                ),
                **terminal_result,
            }
            conn.execute(
                """
                INSERT INTO canvas_command_messages (
                    bridge_key, kind, project_id, canvas_id, request_fingerprint,
                    payload_json, result_json, status, created_at, updated_at,
                    expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    kind,
                    project_id,
                    canvas_id,
                    request_fingerprint,
                    _canonical_json(payload),
                    _canonical_json(resolved),
                    _terminal_status(resolved),
                    now,
                    now,
                    now + BRIDGE_RESULT_TTL_SECONDS,
                ),
            )
            return resolved
        conn.execute(
            """
            INSERT INTO canvas_command_messages (
                bridge_key, kind, project_id, canvas_id, request_fingerprint,
                payload_json, status, created_at, updated_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                key,
                kind,
                project_id,
                canvas_id,
                request_fingerprint,
                _canonical_json(payload),
                now,
                now,
                now + BRIDGE_RESULT_TTL_SECONDS,
            ),
        )
    return None


def _read_durable_bridge_result(
    key: str, *, bridge_dir: str | Path | None = None
) -> dict[str, Any] | None:
    with _bridge_db(bridge_dir) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _prune_bridge_rows(conn, now=time.time(), bridge_dir=bridge_dir)
        row = conn.execute(
            "SELECT status, result_json FROM canvas_command_messages WHERE bridge_key = ?",
            (key,),
        ).fetchone()
    if row is None or str(row["status"]) not in _TERMINAL_BRIDGE_STATUSES:
        return None
    return _decode_bridge_json(row["result_json"])


def _resolve_durable_bridge_message(
    key: str,
    result: dict[str, Any],
    *,
    bridge_dir: str | Path | None = None,
) -> dict[str, Any]:
    result = dict(result)
    expected_token = result.pop("_bridge_execution_token", None)
    now = time.time()
    with _bridge_db(bridge_dir) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM canvas_command_messages WHERE bridge_key = ?",
            (key,),
        ).fetchone()
        if expected_token is not None:
            pending = _decode_bridge_json(existing["payload_json"]) if existing else None
            if not pending or pending.get("execution_token") != expected_token:
                from fastapi import HTTPException
                raise HTTPException(409, "bridge receipt executor was superseded")
        if (
            existing is not None
            and str(existing["status"]) in _TERMINAL_BRIDGE_STATUSES
        ):
            persisted = _decode_bridge_json(existing["result_json"])
            return persisted if persisted is not None else result
        fingerprint = str(existing["request_fingerprint"] or "") if existing else ""
        payload = {
            "key": key,
            "bridge_key": key,
            "resolved_at": now,
            **({"request_fingerprint": fingerprint} if fingerprint else {}),
            **result,
        }
        if existing is None:
            conn.execute(
                """
                INSERT INTO canvas_command_messages (
                    bridge_key, kind, payload_json, result_json, status,
                    created_at, updated_at, expires_at
                ) VALUES (?, 'unknown', '{}', ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    _canonical_json(payload),
                    _terminal_status(payload),
                    now,
                    now,
                    now + BRIDGE_RESULT_TTL_SECONDS,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE canvas_command_messages
                   SET result_json = ?, status = ?, updated_at = ?, expires_at = ?,
                       lease_expires_at = NULL
                 WHERE bridge_key = ?
                """,
                (
                    _canonical_json(payload),
                    _terminal_status(payload),
                    now,
                    now + BRIDGE_RESULT_TTL_SECONDS,
                    key,
                ),
            )
    return payload


@contextmanager
def _key_lock(key: str, bridge_dir: str | Path | None = None):
    directory = _bridge_dir(bridge_dir)
    directory.mkdir(parents=True, exist_ok=True)
    safe_key = "".join(ch for ch in key if ch.isalnum() or ch in {"-", "_"})[:128]
    lock_path = directory / f"{safe_key}.lock"
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _canvas_command_idempotency_conflict(
    *, key: str, project_id: str | None, canvas_id: str | None
) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "canvas_command_idempotency_conflict",
        "tool_call_status": "failed",
        "canvas_apply_status": "failed",
        "applied": False,
        "cancelled": False,
        "bridge_key": key,
        "project_id": project_id,
        "canvas_id": canvas_id,
        "errors": [
            "The bridge key is already bound to a different canvas command payload."
        ],
        "message": "Canvas command idempotency key conflicts with an existing operation.",
        "user_message": "画布操作的幂等标识与已有请求冲突，请重新发起操作。",
        "agent_instruction": "Do not retry this operation with the conflicting bridge key.",
    }


def put_pending_canvas_command(
    *,
    key: str,
    project_id: str | None,
    canvas_id: str | None,
    commands: list[Any],
    envelope: dict[str, Any],
    bridge_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    """Publish once, replaying a durable result for an identical stable key.

    Returning a dict means the operation already reached a terminal state, or
    the caller reused the key for a different payload. The existing durable
    result is never deleted during retry.
    """
    fingerprint = _canvas_command_request_fingerprint(
        project_id=project_id,
        canvas_id=canvas_id,
        commands=commands,
    )
    durable_payload = {
        "key": key,
        "kind": "canvas_command",
        "project_id": project_id,
        "canvas_id": canvas_id,
        "commands": commands,
        "envelope": envelope,
        "request_fingerprint": fingerprint,
        "created_at": time.time(),
    }
    with _key_lock(key, bridge_dir):
        # SQLite is authoritative once a row exists. Consult it before the
        # compatibility mirror so stale files cannot override durable state.
        if bridge_message_exists(key, bridge_dir=bridge_dir):
            return _put_pending_bridge_message(
                key=key,
                kind="canvas_command",
                project_id=project_id,
                canvas_id=canvas_id,
                payload=durable_payload,
                request_fingerprint=fingerprint,
                bridge_dir=bridge_dir,
            )

        # Pre-SQLite workers may have completed or queued this key already.
        # Validate those files before inserting a pending row; otherwise a
        # legacy terminal result (or conflict) would leave a deliverable row.
        existing_result = _read_json(_path("result", key, bridge_dir))
        if existing_result is not None:
            if existing_result.get("request_fingerprint") != fingerprint:
                return _canvas_command_idempotency_conflict(
                    key=key, project_id=project_id, canvas_id=canvas_id
                )

        pending_path = _path("pending", key, bridge_dir)
        existing_pending = _read_json(pending_path)
        if existing_pending is not None:
            if existing_pending.get("request_fingerprint") != fingerprint:
                return _canvas_command_idempotency_conflict(
                    key=key, project_id=project_id, canvas_id=canvas_id
                )

        durable_result = _put_pending_bridge_message(
            key=key,
            kind="canvas_command",
            project_id=project_id,
            canvas_id=canvas_id,
            payload=durable_payload,
            request_fingerprint=fingerprint,
            terminal_result=existing_result,
            bridge_dir=bridge_dir,
        )
        if durable_result is not None:
            if existing_result is not None:
                _unlink_if_exists(pending_path)
            return durable_result
        if existing_pending is not None:
            return None

        _write_json(pending_path, durable_payload)
    return None


def put_pending_canvas_context(
    *,
    key: str,
    project_id: str | None,
    canvas_id: str | None,
    requests: list[Any],
    envelope: dict[str, Any],
    bridge_dir: str | Path | None = None,
) -> None:
    payload = {
        "key": key,
        "kind": "canvas_context",
        "project_id": project_id,
        "canvas_id": canvas_id,
        "requests": requests,
        "envelope": envelope,
        "created_at": time.time(),
    }
    _put_pending_bridge_message(
        key=key,
        kind="canvas_context",
        project_id=project_id,
        canvas_id=canvas_id,
        payload=payload,
        bridge_dir=bridge_dir,
    )
    _unlink_if_exists(_path("result", key, bridge_dir))
    _write_json(_path("pending", key, bridge_dir), payload)


def put_pending_skill_studio_event(
    *,
    key: str,
    project_id: str | None,
    canvas_id: str | None,
    event: dict[str, Any],
    bridge_dir: str | Path | None = None,
) -> None:
    payload = {
        "key": key,
        "kind": "skill_studio_event",
        "project_id": project_id,
        "canvas_id": canvas_id,
        "event": event,
        "created_at": time.time(),
    }
    _put_pending_bridge_message(
        key=key,
        kind="skill_studio_event",
        project_id=project_id,
        canvas_id=canvas_id,
        payload=payload,
        bridge_dir=bridge_dir,
    )
    _unlink_if_exists(_path("result", key, bridge_dir))
    _write_json(_path("pending", key, bridge_dir), payload)


def put_pending_clarification_event(
    *,
    key: str,
    project_id: str | None,
    canvas_id: str | None,
    event: dict[str, Any],
    bridge_dir: str | Path | None = None,
) -> None:
    payload = {
        "key": key,
        "kind": "clarification_event",
        "project_id": project_id,
        "canvas_id": canvas_id,
        "event": event,
        "created_at": time.time(),
    }
    _put_pending_bridge_message(
        key=key,
        kind="clarification_event",
        project_id=project_id,
        canvas_id=canvas_id,
        payload=payload,
        bridge_dir=bridge_dir,
    )
    _unlink_if_exists(_path("result", key, bridge_dir))
    _write_json(_path("pending", key, bridge_dir), payload)


def resolve_canvas_command(
    key: str,
    result: dict[str, Any],
    *,
    bridge_dir: str | Path | None = None,
) -> dict[str, Any]:
    with _key_lock(key, bridge_dir):
        return _resolve_canvas_command_locked(key, result, bridge_dir=bridge_dir)


def _resolve_canvas_command_locked(
    key: str,
    result: dict[str, Any],
    *,
    bridge_dir: str | Path | None = None,
) -> dict[str, Any]:
    result_path = _path("result", key, bridge_dir)
    existing = _read_json(result_path)
    if existing is not None:
        # A background workflow reports "accepted" immediately, while the
        # browser continues executing it. A duplicate recovered approval may
        # later expire; that late cancellation must not overwrite the result
        # already consumed by the waiting tool call.
        _resolve_durable_bridge_message(key, {**existing, "_bridge_execution_token":result.get("_bridge_execution_token")}, bridge_dir=bridge_dir)
        _unlink_if_exists(_path("pending", key, bridge_dir))
        return existing
    pending = _read_json(_path("pending", key, bridge_dir))
    proposed = {
        "key": key,
        "bridge_key": key,
        "resolved_at": time.time(),
        **(
            {"request_fingerprint": pending["request_fingerprint"]}
            if isinstance(pending, dict)
            and isinstance(pending.get("request_fingerprint"), str)
            else {}
        ),
        **result,
    }
    payload = _resolve_durable_bridge_message(key, proposed, bridge_dir=bridge_dir)
    _write_json(result_path, payload)
    _unlink_if_exists(_path("pending", key, bridge_dir))
    return payload


def resolve_canvas_context(
    key: str,
    result: dict[str, Any],
    *,
    bridge_dir: str | Path | None = None,
) -> dict[str, Any]:
    return resolve_canvas_command(key, result, bridge_dir=bridge_dir)


def resolve_skill_studio_result(
    key: str,
    result: dict[str, Any],
    *,
    bridge_dir: str | Path | None = None,
) -> dict[str, Any]:
    return resolve_canvas_command(key, result, bridge_dir=bridge_dir)


def resolve_clarification_result(
    key: str,
    result: dict[str, Any],
    *,
    bridge_dir: str | Path | None = None,
) -> dict[str, Any]:
    return resolve_canvas_command(key, result, bridge_dir=bridge_dir)


def wait_canvas_command_result(
    key: str,
    timeout_seconds: float,
    poll_seconds: float = 0.2,
    *,
    bridge_dir: str | Path | None = None,
    timeout_result: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    deadline = time.time() + max(0.0, timeout_seconds)
    while True:
        durable_result = _read_durable_bridge_result(key, bridge_dir=bridge_dir)
        if durable_result is not None:
            return durable_result
        result = _read_json(_path("result", key, bridge_dir))
        if result is not None:
            return result
        if time.time() >= deadline:
            break
        time.sleep(max(0.05, poll_seconds))
    if timeout_result is not None:
        return resolve_canvas_command(key, timeout_result, bridge_dir=bridge_dir)
    return None


def wait_canvas_context_result(
    key: str,
    timeout_seconds: float,
    poll_seconds: float = 0.2,
    *,
    bridge_dir: str | Path | None = None,
    timeout_result: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    return wait_canvas_command_result(
        key,
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
        bridge_dir=bridge_dir,
        timeout_result=timeout_result,
    )


def wait_skill_studio_result(
    key: str,
    timeout_seconds: float,
    poll_seconds: float = 0.2,
    *,
    bridge_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    return wait_canvas_command_result(
        key,
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
        bridge_dir=bridge_dir,
    )


def wait_clarification_result(
    key: str,
    timeout_seconds: float,
    poll_seconds: float = 0.2,
    *,
    bridge_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    return wait_canvas_command_result(
        key,
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
        bridge_dir=bridge_dir,
    )


def release_bridge_delivery(
    key: str, *, consumer_id: str, bridge_dir: str | Path | None = None
) -> None:
    """Make an unsuccessful delivery immediately available without stealing a lease."""
    with _bridge_db(bridge_dir) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT payload_json FROM canvas_command_messages WHERE bridge_key=? AND status='delivered' AND consumer_id=?",
            (key, consumer_id[:160]),
        ).fetchone()
        if row:
            payload = _decode_bridge_json(row["payload_json"]) or {}
            payload["owner_expires_at"] = 0
            conn.execute(
                "UPDATE canvas_command_messages SET payload_json=? WHERE bridge_key=?",
                (_canonical_json(payload), key),
            )
        conn.execute(
            "UPDATE canvas_command_messages SET status = 'pending', consumer_id = NULL, lease_expires_at = NULL WHERE bridge_key = ? AND status = 'delivered' AND consumer_id = ?",
            (key, consumer_id[:160]),
        )


def canonical_bridge_key(key: str) -> str:
    """Decode an opaque executor receipt key to its durable inbox key."""
    return key.rsplit(".executor.", 1)[0] if ".executor." in key else key


def heartbeat_bridge_browser(
    *, consumer_id: str, binding: dict[str, Any], bridge_dir=None
) -> None:
    with _bridge_db(bridge_dir) as conn:
        conn.execute(
            "DELETE FROM canvas_bridge_browsers WHERE consumer_id IN (SELECT consumer_id FROM canvas_bridge_browsers WHERE expires_at<=? ORDER BY expires_at LIMIT 200)", (time.time(),)
        )
        conn.execute(
            "INSERT INTO canvas_bridge_browsers VALUES (?, ?, ?) ON CONFLICT(consumer_id) DO UPDATE SET binding_json=excluded.binding_json, expires_at=excluded.expires_at",
            (
                consumer_id,
                _canonical_json(binding),
                time.time() + BRIDGE_OWNER_LEASE_SECONDS,
            ),
        )


def browser_is_online(*, binding: dict[str, Any], bridge_dir=None) -> bool:
    with _bridge_db(bridge_dir) as conn:
        return (
            conn.execute(
                "SELECT 1 FROM canvas_bridge_browsers WHERE binding_json=? AND expires_at>? LIMIT 1",
                (_canonical_json(binding), time.time()),
            ).fetchone()
            is not None
        )


def renew_bridge_owner(
    key: str, *, consumer_id: str, execution_token: str, bridge_dir=None
) -> bool:
    with _bridge_db(bridge_dir) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT payload_json FROM canvas_command_messages WHERE bridge_key=? AND consumer_id=? AND status='delivered'",
            (canonical_bridge_key(key), consumer_id),
        ).fetchone()
        payload = _decode_bridge_json(row["payload_json"]) if row else None
        if not payload or payload.get("execution_token") != execution_token:
            return False
        payload["owner_expires_at"] = time.time() + BRIDGE_OWNER_LEASE_SECONDS
        conn.execute(
            "UPDATE canvas_command_messages SET payload_json=?, lease_expires_at=? WHERE bridge_key=?",
            (
                _canonical_json(payload),
                time.time() + BRIDGE_DELIVERY_LEASE_SECONDS,
                canonical_bridge_key(key),
            ),
        )
        return True


def release_bridge_owner(*, consumer_id: str, bridge_dir=None) -> None:
    with _bridge_db(bridge_dir) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM canvas_bridge_browsers WHERE consumer_id=?", (consumer_id,)
        )
        rows = conn.execute(
            "SELECT bridge_key,payload_json FROM canvas_command_messages WHERE consumer_id=? AND status='delivered'",
            (consumer_id,),
        ).fetchall()
        for row in rows:
            payload = _decode_bridge_json(row["payload_json"]) or {}
            payload["owner_expires_at"] = 0
            conn.execute(
                "UPDATE canvas_command_messages SET payload_json=?,lease_expires_at=0 WHERE bridge_key=?",
                (_canonical_json(payload), row["bridge_key"]),
            )


def renew_browser_owned_messages(*, consumer_id: str, bridge_dir=None) -> None:
    with _bridge_db(bridge_dir) as conn:
        rows = conn.execute(
            "SELECT bridge_key,payload_json FROM canvas_command_messages WHERE consumer_id=? AND status='delivered'",
            (consumer_id,),
        ).fetchall()
    for row in rows:
        payload = _decode_bridge_json(row["payload_json"]) or {}
        if token := payload.get("execution_token"):
            renew_bridge_owner(
                str(row["bridge_key"]),
                consumer_id=consumer_id,
                execution_token=token,
                bridge_dir=bridge_dir,
            )


def list_browser_owned_messages(
    *, consumer_id: str, bridge_dir=None
) -> list[dict[str, Any]]:
    with _bridge_db(bridge_dir) as conn:
        _prune_bridge_rows(conn, now=time.time(), bridge_dir=bridge_dir)
        rows = conn.execute(
            "SELECT * FROM canvas_command_messages WHERE consumer_id=? AND status='delivered' ORDER BY created_at LIMIT 100",
            (consumer_id,),
        ).fetchall()
    return [
        {
            **(_decode_bridge_json(row["payload_json"]) or {}),
            "key": row["bridge_key"],
            "kind": row["kind"],
        }
        for row in rows
    ]


def list_scope_bridge_receipts(
    *, binding: dict[str, Any], bridge_dir=None
) -> list[dict[str, Any]]:
    """Real terminal receipts only, with producer identity checked before exposure."""
    producer_where, producer_parameters = _producer_scope_sql(binding)
    with _bridge_db(bridge_dir) as conn:
        _prune_bridge_rows(conn, now=time.time(), bridge_dir=bridge_dir)
        rows = conn.execute(
            "SELECT * FROM canvas_command_messages WHERE project_id=? AND canvas_id=? "
            "AND status IN ('applied','failed','cancelled','expired') AND " + producer_where +
            " ORDER BY updated_at DESC LIMIT 100",
            [binding["project_id"], binding["canvas_id"], *producer_parameters],
        ).fetchall()
    receipts = []
    for row in rows:
        payload = _decode_bridge_json(row["payload_json"]) or {}
        producer = payload.get("producer_binding")
        result = _decode_bridge_json(row["result_json"])
        if (
            isinstance(producer, dict)
            and {key: producer.get(key) for key in binding} == binding
            and result
        ):
            receipts.append(
                {
                    "request_id": row["bridge_key"],
                    "kind": row["kind"],
                    "turn_id": producer["turn_id"],
                    "status": row["status"],
                    "result": result,
                }
            )
        if len(receipts) >= 100:
            break
    return receipts


def register_bridge_producer_turn(*, binding: dict[str, Any], bridge_dir=None) -> None:
    scope = {key: value for key, value in binding.items() if key != "turn_id"}
    with _bridge_db(bridge_dir) as conn:
        conn.execute(
            "DELETE FROM canvas_bridge_producer_turns WHERE rowid IN (SELECT rowid FROM canvas_bridge_producer_turns WHERE expires_at<=? ORDER BY expires_at LIMIT 200)",
            (time.time(),),
        )
        conn.execute(
            "INSERT INTO canvas_bridge_producer_turns VALUES (?,?,?) ON CONFLICT(scope_json,turn_id) DO UPDATE SET expires_at=excluded.expires_at",
            (
                _canonical_json(scope),
                binding["turn_id"],
                time.time() + BRIDGE_OWNER_LEASE_SECONDS,
            ),
        )


def release_bridge_producer_turn(*, binding: dict[str, Any], bridge_dir=None) -> None:
    scope = {key: value for key, value in binding.items() if key != "turn_id"}
    with _bridge_db(bridge_dir) as conn:
        conn.execute(
            "DELETE FROM canvas_bridge_producer_turns WHERE scope_json=? AND turn_id=?",
            (_canonical_json(scope), binding["turn_id"]),
        )


def record_plan_revision_sync(key: str, statuses: list[dict[str, Any]], *, bridge_dir=None) -> dict[str, Any] | None:
    """Host-only enrichment of a successful receipt; never replay its command."""
    key = canonical_bridge_key(key)
    with _key_lock(key, bridge_dir):
        with _bridge_db(bridge_dir) as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT result_json FROM canvas_command_messages WHERE bridge_key=? AND status=\'applied\'', (key,)).fetchone()
            result = _decode_bridge_json(row['result_json']) if row else None
            if not result or result.get('ok') is not True or not statuses:
                return result
            summary = [{name: item[name] for name in ('status', 'plan_ref') if name in item}
                       for item in statuses]
            result['plan_revision_sync'] = summary
            if len(summary) == 1:
                result['plan_ref'] = summary[0]['plan_ref']
            conn.execute('UPDATE canvas_command_messages SET result_json=? WHERE bridge_key=?',
                         (_canonical_json(result), key))
        _write_json(_path('result', key, bridge_dir), result)
        return result
