"""Scoped chat persistence shared by NiceGUI and the React WebSocket API.

Lovart-style split:
    * home scope: user-level conversation before a project exists.
    * project scope: project/canvas conversation and iteration history.

The project chat DB path intentionally matches ``chat_service.py`` so existing
NiceGUI history remains readable by the future React UI.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import aiosqlite

from novelvideo.sqlite_pragmas import (
    configure_sqlite_connection,
    configure_sqlite_connection_async,
)


def _assistant_prefix_candidates(previous_assistant: object) -> list[str]:
    if isinstance(previous_assistant, (list, tuple)):
        items = [str(item or "").strip() for item in previous_assistant if str(item or "").strip()]
        candidates = ["".join(items[index:]) for index in range(len(items))]
        candidates.extend(items)
        return sorted(set(candidates), key=len, reverse=True)
    prefix = str(previous_assistant or "").strip()
    return [prefix] if prefix else []


def _strip_replayed_assistant_prefix(content: str, previous_assistant: object) -> str:
    text = str(content or "")
    for prefix in _assistant_prefix_candidates(previous_assistant):
        if text.startswith(prefix):
            return text[len(prefix):].lstrip()
        compact_prefix = "".join(prefix.split())
        if not compact_prefix:
            continue
        matched = 0
        end_index = 0
        for index, char in enumerate(text):
            if char.isspace():
                continue
            if matched >= len(compact_prefix) or char != compact_prefix[matched]:
                break
            matched += 1
            end_index = index + 1
            if matched == len(compact_prefix):
                return text[end_index:].lstrip()
    return text


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _state_root() -> Path:
    configured = os.environ.get("NOVELVIDEO_STATE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return _repo_root() / "state"


@dataclass(frozen=True)
class ChatScope:
    kind: Literal["home", "project", "freezone", "asset", "task"]
    id: str | None = None
    surface: str | None = None
    canvas_id: str | None = None
    agent_id: str | None = None
    # Server-resolved project state root. This is deliberately omitted from
    # ``to_dict`` so filesystem topology never becomes a browser contract.
    state_dir: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> "ChatScope":
        payload = payload or {"kind": "home"}
        kind = str(payload.get("kind") or "home")
        if kind not in {"home", "project", "freezone", "asset", "task"}:
            raise ValueError(f"unsupported chat scope: {kind}")
        raw_id = payload.get("id")
        scope_id = str(raw_id).strip() if raw_id is not None else None
        raw_surface = payload.get("surface")
        surface = str(raw_surface).strip() if raw_surface is not None else None
        raw_canvas_id = payload.get("canvasId", payload.get("canvas_id"))
        canvas_id = str(raw_canvas_id).strip() if raw_canvas_id is not None else None
        raw_agent_id = payload.get("agentId", payload.get("agent_id"))
        agent_id = str(raw_agent_id).strip() if raw_agent_id is not None else None
        if kind == "home":
            scope_id = None
            surface = None
            canvas_id = None
            agent_id = None
        if kind != "home" and not scope_id:
            raise ValueError(f"scope id is required for {kind}")
        if kind == "project":
            surface = surface or "director"
            if surface not in {"director", "freezone"}:
                raise ValueError(f"unsupported project chat surface: {surface}")
            if surface != "freezone":
                canvas_id = None
                agent_id = None
            else:
                agent_id = agent_id or "main"
                if not re.fullmatch(r"[A-Za-z0-9_-]+", agent_id):
                    raise ValueError("unsupported freezone agent id")
        else:
            surface = None
            canvas_id = None
            agent_id = None
        return cls(kind=kind, id=scope_id, surface=surface, canvas_id=canvas_id, agent_id=agent_id)

    def to_dict(self) -> dict[str, str | None]:
        data: dict[str, str | None] = {"kind": self.kind, "id": self.id}
        if self.surface:
            data["surface"] = self.surface
        if self.canvas_id:
            data["canvasId"] = self.canvas_id
        if self.agent_id:
            data["agentId"] = self.agent_id
        return data


# One latest revision per event: no token/event payload history is duplicated.
# SQLite serializes writers; trigger revisions commit atomically with the row.
_UI_EVENT_REVISION_SQL = (
    "CREATE TABLE IF NOT EXISTS chat_ui_event_revisions (revision INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL UNIQUE)",
    """CREATE TRIGGER IF NOT EXISTS chat_ui_event_revision_insert AFTER INSERT ON chat_ui_events
       BEGIN INSERT INTO chat_ui_event_revisions(event_id) VALUES (NEW.id)
       ON CONFLICT(event_id) DO UPDATE SET revision=excluded.revision; END""",
    """CREATE TRIGGER IF NOT EXISTS chat_ui_event_revision_update AFTER UPDATE ON chat_ui_events
       BEGIN INSERT INTO chat_ui_event_revisions(event_id) VALUES (NEW.id)
       ON CONFLICT(event_id) DO UPDATE SET revision=excluded.revision; END""",
    """CREATE TRIGGER IF NOT EXISTS chat_ui_event_revision_delete AFTER DELETE ON chat_ui_events
       BEGIN DELETE FROM chat_ui_event_revisions WHERE event_id=OLD.id; END""",
    """INSERT INTO chat_ui_event_revisions(event_id) SELECT id FROM chat_ui_events
       WHERE NOT EXISTS (SELECT 1 FROM chat_ui_event_revisions WHERE event_id=chat_ui_events.id) ORDER BY id""",
)


class ChatStore:
    def pull_recovery(self, username: str, scope: ChatScope, *, cursor: int = 0,
                      event_cursor: str = "", limit: int = 100) -> dict[str, Any]:
        """Pull persisted messages and updated UI snapshots without storing deltas."""
        limit = max(1, min(limit, 200))
        if event_cursor and (not event_cursor.startswith("r") or not event_cursor[1:].isdigit()):
            raise ValueError("invalid event cursor")
        conn = self.connect(username, scope, db_path=self.read_db_for(username, scope))
        try:
            rows = conn.execute("SELECT * FROM chat_messages WHERE id>? AND role NOT IN ('trace','agent_notification') ORDER BY id LIMIT ?", (cursor,limit)).fetchall()
            events = conn.execute("SELECT event.*, revision.revision FROM chat_ui_events AS event JOIN chat_ui_event_revisions AS revision ON revision.event_id=event.id WHERE revision.revision>? ORDER BY revision.revision LIMIT ?", (int(event_cursor[1:] or 0) if event_cursor else 0,limit)).fetchall()
            messages = [self._message_from_row(row) for row in rows]
            if messages:
                turn_ids = list({str(message["turn_id"]) for message in messages if message.get("turn_id")})
                self._attach_ui_events_to_messages(messages, self._load_ui_events(conn, turn_ids=turn_ids))
        finally:
            conn.close()
        return {"messages": messages,
                "events": [{"id":int(row["id"]),"turn_id":row["turn_id"],"type":row["event_type"],"payload":json.loads(row["payload_json"]),"created_at":row["created_at"]} for row in events],
                "cursor": int(rows[-1]["id"]) if rows else cursor,
                "event_cursor": f"r{events[-1]['revision']}" if events else event_cursor,
                "has_more": len(rows)==limit or len(events)==limit}

    @staticmethod
    def _project_root(username: str, scope: ChatScope) -> Path:
        if scope.state_dir:
            return Path(scope.state_dir)
        return _state_root() / username / str(scope.id)

    def db_for(self, username: str, scope: ChatScope) -> Path:
        if scope.kind == "home":
            return _state_root() / username / "_home" / "chat.db"
        if scope.kind == "project":
            if (scope.surface or "director") == "director" and not scope.canvas_id:
                return self._project_root(username, scope) / "chat.db"
            surface = scope.surface or "director"
            if surface == "freezone" and scope.canvas_id:
                agent_id = scope.agent_id or "main"
                return (
                    self._project_root(username, scope)
                    / "_chat"
                    / surface
                    / str(scope.canvas_id)
                    / "agents"
                    / agent_id
                    / "chat.db"
                )
            return self._project_root(username, scope) / "_chat" / surface / "chat.db"
        if scope.kind == "freezone":
            return _state_root() / username / "_freezone" / str(scope.id) / "chat.db"
        return _state_root() / username / f"_{scope.kind}" / str(scope.id) / "chat.db"

    def _legacy_freezone_canvas_db_for(self, username: str, scope: ChatScope) -> Path | None:
        if (
            scope.state_dir
            or scope.kind != "project"
            or scope.surface != "freezone"
            or not scope.canvas_id
        ):
            return None
        if scope.agent_id not in {None, "", "main"}:
            return None
        return (
            _state_root()
            / username
            / str(scope.id)
            / "_chat"
            / "freezone"
            / str(scope.canvas_id)
            / "chat.db"
        )

    def read_db_for(self, username: str, scope: ChatScope) -> Path:
        db_path = self.db_for(username, scope)
        legacy_db_path = self._legacy_freezone_canvas_db_for(username, scope)
        if legacy_db_path is not None and not db_path.exists() and legacy_db_path.exists():
            return legacy_db_path
        return db_path

    async def clear_messages_async(self, username: str, scope: ChatScope) -> int:
        """Clear only one chat scope, preserving its settings and other project data."""
        db_path = self.read_db_for(username, scope)
        if not db_path.exists():
            return 0
        db = await self.connect_async(username, scope, db_path=db_path)
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute("DELETE FROM chat_messages")
            await db.execute("DELETE FROM chat_ui_events")
            await db.commit()
            return max(cursor.rowcount, 0)
        except BaseException:
            await db.rollback()
            raise
        finally:
            await db.close()

    def _freezone_canvas_agents_dir(
        self,
        username: str,
        *,
        project_id: str,
        canvas_id: str,
        project_state_dir: str | Path | None = None,
    ) -> Path:
        project_root = (
            Path(project_state_dir)
            if project_state_dir is not None
            else _state_root() / username / str(project_id)
        )
        return (
            project_root
            / "_chat"
            / "freezone"
            / str(canvas_id)
            / "agents"
        )

    def _freezone_canvas_legacy_db(
        self,
        username: str,
        *,
        project_id: str,
        canvas_id: str,
    ) -> Path:
        return (
            _state_root()
            / username
            / str(project_id)
            / "_chat"
            / "freezone"
            / str(canvas_id)
            / "chat.db"
        )

    @staticmethod
    def _timestamp_ms(value: str) -> int:
        try:
            normalized = value.replace("Z", "+00:00")
            return int(datetime.fromisoformat(normalized).timestamp() * 1000)
        except Exception:
            return 0

    def _freezone_agent_summary_from_db(self, agent_id: str, db_path: Path) -> dict[str, Any] | None:
        if not db_path.exists():
            return None
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            tables = {
                str(row["name"])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "chat_messages" not in tables:
                return None
            latest = conn.execute(
                """
                SELECT content, created_at
                  FROM chat_messages
                 WHERE role NOT IN ('trace', 'agent_notification')
                 ORDER BY id DESC
                 LIMIT 1
                """
            ).fetchone()
            if latest is None:
                return None
            first_user = conn.execute(
                """
                SELECT content
                  FROM chat_messages
                 WHERE role = 'user'
                 ORDER BY id ASC
                 LIMIT 1
                """
            ).fetchone()
        finally:
            conn.close()
        title = str(first_user["content"] if first_user is not None else latest["content"] or "").strip()
        title = re.sub(r"\s+", " ", title) or agent_id
        if len(title) > 32:
            title = f"{title[:31]}…"
        timestamp = self._timestamp_ms(str(latest["created_at"] or ""))
        return {
            "id": agent_id,
            "name": title,
            "createdAt": timestamp,
            "lastActiveAt": timestamp,
        }

    def list_freezone_canvas_agent_summaries(
        self,
        username: str,
        *,
        project_id: str,
        canvas_id: str,
        limit: int = 20,
        project_state_dir: str | Path | None = None,
    ) -> list[dict[str, Any]]:
        agents_dir = self._freezone_canvas_agents_dir(
            username,
            project_id=project_id,
            canvas_id=canvas_id,
            project_state_dir=project_state_dir,
        )
        candidates: list[tuple[str, Path]] = []
        if agents_dir.exists():
            candidates.extend(
                (path.parent.name, path)
                for path in agents_dir.glob("*/chat.db")
                if path.parent.name
            )
        legacy_db = (
            self._freezone_canvas_legacy_db(
                username,
                project_id=project_id,
                canvas_id=canvas_id,
            )
            if project_state_dir is None
            else None
        )
        main_db = agents_dir / "main" / "chat.db"
        if legacy_db is not None and legacy_db.exists() and not main_db.exists():
            candidates.append(("main", legacy_db))

        summaries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for agent_id, db_path in candidates:
            if agent_id in seen:
                continue
            seen.add(agent_id)
            summary = self._freezone_agent_summary_from_db(agent_id, db_path)
            if summary is not None:
                summaries.append(summary)
        bounded_limit = max(1, min(int(limit), 100))
        return sorted(
            summaries,
            key=self._freezone_agent_summary_sort_key,
            reverse=True,
        )[:bounded_limit]

    async def _freezone_agent_summary_from_db_async(
        self,
        agent_id: str,
        db_path: Path,
    ) -> dict[str, Any] | None:
        if not db_path.exists():
            return None
        db = await aiosqlite.connect(db_path, timeout=10)
        db.row_factory = aiosqlite.Row
        try:
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='chat_messages'"
            )
            if await cursor.fetchone() is None:
                return None
            cursor = await db.execute(
                """
                SELECT content, created_at
                  FROM chat_messages
                 WHERE role NOT IN ('trace', 'agent_notification')
                 ORDER BY id DESC
                 LIMIT 1
                """
            )
            latest = await cursor.fetchone()
            if latest is None:
                return None
            cursor = await db.execute(
                """
                SELECT content
                  FROM chat_messages
                 WHERE role = 'user'
                 ORDER BY id ASC
                 LIMIT 1
                """
            )
            first_user = await cursor.fetchone()
        finally:
            await db.close()
        title = str(
            first_user["content"] if first_user is not None else latest["content"] or ""
        ).strip()
        title = re.sub(r"\s+", " ", title) or agent_id
        if len(title) > 32:
            title = f"{title[:31]}…"
        timestamp = self._timestamp_ms(str(latest["created_at"] or ""))
        return {
            "id": agent_id,
            "name": title,
            "createdAt": timestamp,
            "lastActiveAt": timestamp,
        }

    async def list_freezone_canvas_agent_summaries_async(
        self,
        username: str,
        *,
        project_id: str,
        canvas_id: str,
        limit: int = 20,
        project_state_dir: str | Path | None = None,
    ) -> list[dict[str, Any]]:
        agents_dir = self._freezone_canvas_agents_dir(
            username,
            project_id=project_id,
            canvas_id=canvas_id,
            project_state_dir=project_state_dir,
        )
        bounded_limit = max(1, min(int(limit), 100))

        def candidate_paths() -> list[tuple[str, Path]]:
            candidates: list[tuple[str, Path]] = []
            if agents_dir.exists():
                candidates.extend(
                    (path.parent.name, path)
                    for path in agents_dir.glob("*/chat.db")
                    if path.parent.name
                )
            legacy_db = (
                self._freezone_canvas_legacy_db(
                    username,
                    project_id=project_id,
                    canvas_id=canvas_id,
                )
                if project_state_dir is None
                else None
            )
            main_db = agents_dir / "main" / "chat.db"
            if legacy_db is not None and legacy_db.exists() and not main_db.exists():
                candidates.append(("main", legacy_db))
            return candidates

        candidates = await asyncio.to_thread(candidate_paths)

        semaphore = asyncio.Semaphore(8)

        async def load_summary(agent_id: str, db_path: Path) -> dict[str, Any] | None:
            async with semaphore:
                return await self._freezone_agent_summary_from_db_async(agent_id, db_path)

        raw_summaries = await asyncio.gather(
            *(
                load_summary(agent_id, db_path)
                for agent_id, db_path in candidates
            )
        )
        summaries = [summary for summary in raw_summaries if summary is not None]
        return sorted(
            summaries,
            key=self._freezone_agent_summary_sort_key,
            reverse=True,
        )[:bounded_limit]

    @staticmethod
    def _freezone_agent_summary_sort_key(summary: dict[str, Any]) -> tuple[int, int, str]:
        agent_id = str(summary.get("id") or "")
        match = re.fullmatch(r"agent-(\d+)", agent_id)
        agent_rank = int(match.group(1)) if match else -1
        return (int(summary.get("lastActiveAt") or 0), agent_rank, agent_id)

    def connect(self, username: str, scope: ChatScope, *, db_path: Path | None = None) -> sqlite3.Connection:
        db_path = db_path or self.db_for(username, scope)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        configure_sqlite_connection(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              role TEXT NOT NULL,
              content TEXT NOT NULL,
              media_json TEXT NOT NULL DEFAULT '[]',
              turn_id TEXT,
              metadata_json TEXT NOT NULL DEFAULT '{}',
              idempotency_key TEXT,
              created_at TEXT NOT NULL
            )
            """
        )
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(chat_messages)").fetchall()
        }
        if "turn_id" not in columns:
            conn.execute("ALTER TABLE chat_messages ADD COLUMN turn_id TEXT")
        if "metadata_json" not in columns:
            conn.execute("ALTER TABLE chat_messages ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")
        if "idempotency_key" not in columns:
            conn.execute("ALTER TABLE chat_messages ADD COLUMN idempotency_key TEXT")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_messages_idempotency_key
              ON chat_messages(idempotency_key)
             WHERE idempotency_key IS NOT NULL AND idempotency_key <> ''
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_ui_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              turn_id TEXT NOT NULL,
              event_type TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_chat_ui_events_turn_id
              ON chat_ui_events(turn_id, id)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE TABLE IF NOT EXISTS chat_store_migrations (name TEXT PRIMARY KEY)")
        if not conn.execute("SELECT 1 FROM chat_store_migrations WHERE name='ui_event_revisions_v1'").fetchone():
            # The unique marker acquires the writer lock before triggers/backfill;
            # competing initializers wait and only the winning transaction migrates.
            marker = conn.execute("INSERT OR IGNORE INTO chat_store_migrations(name) VALUES ('ui_event_revisions_v1')")
            if marker.rowcount:
                for statement in _UI_EVENT_REVISION_SQL:
                    conn.execute(statement)
        conn.commit()
        return conn

    async def connect_async(
        self,
        username: str,
        scope: ChatScope,
        *,
        db_path: Path | None = None,
        _schema_attempt: int = 0,
    ) -> aiosqlite.Connection:
        resolved_path = db_path or self.db_for(username, scope)
        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(resolved_path, timeout=10)
        db.row_factory = aiosqlite.Row
        try:
            await self._initialize_connection_async(db)
        except sqlite3.OperationalError as exc:
            await db.close()
            if "locked" not in str(exc).lower() or _schema_attempt >= 5:
                raise
            await asyncio.sleep(0.05 * (_schema_attempt + 1))
            return await self.connect_async(
                username,
                scope,
                db_path=resolved_path,
                _schema_attempt=_schema_attempt + 1,
            )
        except BaseException:
            await db.close()
            raise
        return db

    @staticmethod
    async def _initialize_connection_async(db: aiosqlite.Connection) -> None:
        await configure_sqlite_connection_async(db)
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              role TEXT NOT NULL,
              content TEXT NOT NULL,
              media_json TEXT NOT NULL DEFAULT '[]',
              turn_id TEXT,
              metadata_json TEXT NOT NULL DEFAULT '{}',
              idempotency_key TEXT,
              created_at TEXT NOT NULL
            )
            """
        )
        cursor = await db.execute("PRAGMA table_info(chat_messages)")
        columns = {str(row["name"]) for row in await cursor.fetchall()}
        if "turn_id" not in columns:
            await db.execute("ALTER TABLE chat_messages ADD COLUMN turn_id TEXT")
        if "metadata_json" not in columns:
            await db.execute(
                "ALTER TABLE chat_messages ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "idempotency_key" not in columns:
            await db.execute("ALTER TABLE chat_messages ADD COLUMN idempotency_key TEXT")
        await db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_messages_idempotency_key
              ON chat_messages(idempotency_key)
             WHERE idempotency_key IS NOT NULL AND idempotency_key <> ''
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_ui_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              turn_id TEXT NOT NULL,
              event_type TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_chat_ui_events_turn_id
              ON chat_ui_events(turn_id, id)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        await db.execute("CREATE TABLE IF NOT EXISTS chat_store_migrations (name TEXT PRIMARY KEY)")
        cursor = await db.execute("SELECT 1 FROM chat_store_migrations WHERE name='ui_event_revisions_v1'")
        if not await cursor.fetchone():
            marker = await db.execute("INSERT OR IGNORE INTO chat_store_migrations(name) VALUES ('ui_event_revisions_v1')")
            if marker.rowcount:
                for statement in _UI_EVENT_REVISION_SQL:
                    await db.execute(statement)
        await db.commit()

    @staticmethod
    def _message_from_row(row: sqlite3.Row | aiosqlite.Row) -> dict[str, Any]:
        try:
            media = json.loads(row["media_json"] or "[]")
        except json.JSONDecodeError:
            media = []
        if not isinstance(media, list):
            media = []
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        turn_id = str(row["turn_id"] or "").strip()
        return {
            "id": int(row["id"]),
            "role": str(row["role"]),
            "content": str(row["content"]),
            "media": media,
            "attachments": media,
            **({"turn_id": turn_id} if turn_id else {}),
            **({"metadata": metadata} if metadata else {}),
            "created_at": str(row["created_at"]),
        }

    def append_message(
        self,
        username: str,
        scope: ChatScope,
        role: str,
        content: str,
        media: list[dict[str, Any]] | None = None,
        *,
        turn_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        media = media or []
        metadata = metadata or {}
        created_at = datetime.now(timezone.utc).isoformat()
        conn = self.connect(username, scope)
        try:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO chat_messages(
                    role, content, media_json, turn_id, metadata_json,
                    idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    role,
                    content,
                    json.dumps(media, ensure_ascii=False),
                    turn_id,
                    json.dumps(metadata, ensure_ascii=False),
                    idempotency_key,
                    created_at,
                ),
            )
            conn.commit()
            if cursor.rowcount == 0 and idempotency_key:
                existing = conn.execute(
                    """
                    SELECT id, role, content, media_json, turn_id, metadata_json, created_at
                      FROM chat_messages
                     WHERE idempotency_key = ?
                    """,
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    return self._message_from_row(existing)
            return {
                "id": int(cursor.lastrowid),
                "role": role,
                "content": content,
                "media": media,
                "attachments": media,
                **({"turn_id": turn_id} if turn_id else {}),
                **({"metadata": metadata} if metadata else {}),
                "created_at": created_at,
            }
        finally:
            conn.close()

    async def append_message_async(
        self,
        username: str,
        scope: ChatScope,
        role: str,
        content: str,
        media: list[dict[str, Any]] | None = None,
        *,
        turn_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        task = asyncio.create_task(
            self._append_message_async(
                username,
                scope,
                role,
                content,
                media,
                turn_id=turn_id,
                metadata=metadata,
                idempotency_key=idempotency_key,
            )
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def _append_message_async(
        self,
        username: str,
        scope: ChatScope,
        role: str,
        content: str,
        media: list[dict[str, Any]] | None = None,
        *,
        turn_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        media = media or []
        metadata = metadata or {}
        created_at = datetime.now(timezone.utc).isoformat()
        db = await self.connect_async(username, scope)
        try:
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO chat_messages(
                    role, content, media_json, turn_id, metadata_json,
                    idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    role,
                    content,
                    json.dumps(media, ensure_ascii=False),
                    turn_id,
                    json.dumps(metadata, ensure_ascii=False),
                    idempotency_key,
                    created_at,
                ),
            )
            await db.commit()
            if cursor.rowcount == 0 and idempotency_key:
                existing_cursor = await db.execute(
                    """
                    SELECT id, role, content, media_json, turn_id, metadata_json, created_at
                      FROM chat_messages
                     WHERE idempotency_key = ?
                    """,
                    (idempotency_key,),
                )
                existing = await existing_cursor.fetchone()
                if existing is not None:
                    return self._message_from_row(existing)
            return {
                "id": int(cursor.lastrowid),
                "role": role,
                "content": content,
                "media": media,
                "attachments": media,
                **({"turn_id": turn_id} if turn_id else {}),
                **({"metadata": metadata} if metadata else {}),
                "created_at": created_at,
            }
        finally:
            await db.close()

    def append_ui_event(
        self,
        username: str,
        scope: ChatScope,
        turn_id: str,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        turn_id = str(turn_id or "").strip()
        if not turn_id:
            raise ValueError("turn_id is required")
        event_type = str(event.get("type") or event.get("event_type") or "ui_event").strip()
        created_at = datetime.now(timezone.utc).isoformat()
        conn = self.connect(username, scope)
        try:
            existing = None
            if event_type == "assistant.message_parts" and isinstance(event.get("parts"), list):
                # A streamed snapshot is mutable state, not a new interaction.
                # Serialize writers so two tabs cannot lose each other's cards.
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    "SELECT id,payload_json FROM chat_ui_events WHERE turn_id=? AND event_type=? ORDER BY id DESC LIMIT 1",
                    (turn_id, event_type),
                ).fetchone()
            if existing is not None:
                event = self._merge_snapshot_event(existing["payload_json"], event)
                event_id = int(existing["id"])
                conn.execute(
                    "UPDATE chat_ui_events SET payload_json=?,created_at=? WHERE id=?",
                    (json.dumps(event, ensure_ascii=False), created_at, event_id),
                )
            else:
                cursor = conn.execute(
                    "INSERT INTO chat_ui_events(turn_id,event_type,payload_json,created_at) VALUES (?,?,?,?)",
                    (turn_id, event_type, json.dumps(event, ensure_ascii=False), created_at),
                )
                event_id = int(cursor.lastrowid)
            conn.commit()
            return {
                "id": event_id,
                "turn_id": turn_id,
                "type": event_type,
                "payload": event,
                "created_at": created_at,
            }
        finally:
            conn.close()

    async def append_ui_event_async(
        self,
        username: str,
        scope: ChatScope,
        turn_id: str,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        turn_id = str(turn_id or "").strip()
        if not turn_id:
            raise ValueError("turn_id is required")
        event_type = str(event.get("type") or event.get("event_type") or "ui_event").strip()
        created_at = datetime.now(timezone.utc).isoformat()
        db = await self.connect_async(username, scope)
        try:
            existing = None
            if event_type == "assistant.message_parts" and isinstance(event.get("parts"), list):
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    "SELECT id,payload_json FROM chat_ui_events WHERE turn_id=? AND event_type=? ORDER BY id DESC LIMIT 1",
                    (turn_id, event_type),
                )
                existing = await cursor.fetchone()
                await cursor.close()
            if existing is not None:
                event = self._merge_snapshot_event(existing["payload_json"], event)
                event_id = int(existing["id"])
                await db.execute(
                    "UPDATE chat_ui_events SET payload_json=?,created_at=? WHERE id=?",
                    (json.dumps(event, ensure_ascii=False), created_at, event_id),
                )
            else:
                cursor = await db.execute(
                    "INSERT INTO chat_ui_events(turn_id,event_type,payload_json,created_at) VALUES (?,?,?,?)",
                    (turn_id, event_type, json.dumps(event, ensure_ascii=False), created_at),
                )
                event_id = int(cursor.lastrowid)
            await db.commit()
            return {
                "id": event_id,
                "turn_id": turn_id,
                "type": event_type,
                "payload": event,
                "created_at": created_at,
            }
        finally:
            await db.close()

    @classmethod
    def _merge_snapshot_event(cls, previous_json: str, event: dict[str, Any]) -> dict[str, Any]:
        try:
            previous = json.loads(previous_json)
        except (ValueError, TypeError):
            previous = {}
        old_parts = previous.get("parts", []) if isinstance(previous, dict) else []
        return {
            **event,
            "parts": cls._merge_message_parts_snapshots([old_parts, event["parts"]]),
        }

    async def _load_ui_events_async(
        self,
        db: aiosqlite.Connection,
    ) -> dict[str, list[dict[str, Any]]]:
        cursor = await db.execute(
            """
            SELECT id, turn_id, event_type, payload_json, created_at
              FROM chat_ui_events
             ORDER BY id ASC
            """
        )
        rows = await cursor.fetchall()
        events_by_turn: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            turn_id = str(row["turn_id"] or "").strip()
            if not turn_id:
                continue
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except json.JSONDecodeError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {"value": payload}
            payload = {
                "id": int(row["id"]),
                "type": str(row["event_type"] or payload.get("type") or "ui_event"),
                "turn_id": turn_id,
                "created_at": str(row["created_at"]),
                **payload,
            }
            events_by_turn.setdefault(turn_id, []).append(payload)
        return events_by_turn

    def _load_ui_events(self, conn: sqlite3.Connection, *, turn_ids: list[str] | None = None) -> dict[str, list[dict[str, Any]]]:
        if turn_ids == []:
            return {}
        where = "" if turn_ids is None else " WHERE turn_id IN (" + ",".join("?" for _ in turn_ids) + ")"
        rows = conn.execute(
            "SELECT id, turn_id, event_type, payload_json, created_at FROM chat_ui_events"
            + where + " ORDER BY id ASC", turn_ids or [],
        ).fetchall()
        events_by_turn: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            turn_id = str(row["turn_id"] or "").strip()
            if not turn_id:
                continue
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except json.JSONDecodeError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {"value": payload}
            payload = {
                "id": int(row["id"]),
                "type": str(row["event_type"] or payload.get("type") or "ui_event"),
                "turn_id": turn_id,
                "created_at": str(row["created_at"]),
                **payload,
            }
            events_by_turn.setdefault(turn_id, []).append(payload)
        return events_by_turn

    @staticmethod
    def _message_part_key(part: Any) -> str | None:
        if not isinstance(part, dict):
            return None
        part_id = str(part.get("id") or "").strip()
        if part_id:
            return part_id
        event = part.get("event")
        if not isinstance(event, dict):
            return None
        event_type = str(event.get("type") or "").strip()
        stable_id = (
            str(event.get("bridge_key") or "").strip()
            or str(event.get("skill_studio_session_id") or "").strip()
            or str(event.get("clarification_id") or "").strip()
        )
        if event_type and stable_id:
            return f"{event_type}:{stable_id}"
        return None

    @staticmethod
    def _is_transient_message_part(part: Any) -> bool:
        if not isinstance(part, dict):
            return False
        if str(part.get("type") or "") != "skill_studio":
            return False
        event = part.get("event")
        return isinstance(event, dict) and str(event.get("type") or "") == "skill_studio.status"

    @classmethod
    def _merge_message_parts_snapshots(cls, snapshots: list[list[Any]]) -> list[Any]:
        merged: list[Any] = []
        for snapshot in snapshots:
            if not isinstance(snapshot, list):
                continue
            snapshot_keys = {
                key
                for part in snapshot
                if (key := cls._message_part_key(part))
            }
            preserved: list[Any] = []
            for part in merged:
                if not isinstance(part, dict):
                    continue
                part_type = str(part.get("type") or "")
                key = cls._message_part_key(part)
                if part_type == "canvas_approval":
                    continue
                if cls._is_transient_message_part(part):
                    continue
                if part_type == "text" and key not in snapshot_keys:
                    continue
                if key and key in snapshot_keys:
                    continue
                preserved.append(part)
            merged = [*preserved, *snapshot]
        return merged

    @staticmethod
    def _attach_ui_events_to_messages(
        messages: list[dict[str, Any]],
        events_by_turn: dict[str, list[dict[str, Any]]],
    ) -> None:
        if not messages or not events_by_turn:
            return
        for turn_id, events in events_by_turn.items():
            if not events:
                continue
            target_index: int | None = None
            for index, message in enumerate(messages):
                if message.get("role") == "assistant" and message.get("turn_id") == turn_id:
                    target_index = index
                    break
            if target_index is None:
                user_index = next(
                    (
                        index
                        for index, message in enumerate(messages)
                        if message.get("role") == "user" and message.get("turn_id") == turn_id
                    ),
                    None,
                )
                if user_index is not None:
                    for index in range(user_index + 1, len(messages)):
                        if messages[index].get("role") == "assistant":
                            target_index = index
                            break
                    if target_index is None:
                        target_index = user_index
            if target_index is None:
                # Some tool-only turns do not produce an assistant message, for example
                # when Hermes returns no natural-language content after a canvas approval.
                # Keep the UI events visible by anchoring them to the user message for
                # that turn; the frontend expands tool_activity events beside it.
                target_index = next(
                    (
                        index
                        for index, message in enumerate(messages)
                        if message.get("role") == "user" and message.get("turn_id") == turn_id
                    ),
                    None,
                )
            if target_index is None:
                first_event = events[0]
                messages.append(
                    {
                        "id": f"ui-events:{turn_id}",
                        "role": "assistant",
                        "content": "",
                        "media": [],
                        "attachments": [],
                        "turn_id": turn_id,
                        "created_at": str(first_event.get("created_at") or ""),
                        "ui_events": [],
                    }
                )
                target_index = len(messages) - 1
            parts_events = [
                event
                for event in events
                if str(event.get("type") or "") == "assistant.message_parts"
                and isinstance(event.get("parts"), list)
            ]
            visible_events = [
                event
                for event in events
                if str(event.get("type") or "") != "assistant.message_parts"
            ]
            if parts_events:
                messages[target_index]["parts"] = ChatStore._merge_message_parts_snapshots(
                    [
                        event["parts"]
                        for event in parts_events
                        if isinstance(event.get("parts"), list)
                    ]
                )
            existing = messages[target_index].get("ui_events")
            if not isinstance(existing, list):
                existing = []
            messages[target_index]["ui_events"] = [*existing, *visible_events]

    def list_messages(
        self,
        username: str,
        scope: ChatScope,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        conn = self.connect(username, scope, db_path=self.read_db_for(username, scope))
        try:
            rows = conn.execute(
                """
                SELECT id, role, content, media_json, turn_id, metadata_json, created_at
                  FROM chat_messages
                 WHERE role NOT IN ('trace', 'agent_notification')
                 ORDER BY id DESC
                 LIMIT ?
                """,
                (limit,),
            ).fetchall()
            events_by_turn = self._load_ui_events(conn)
        finally:
            conn.close()
        messages: list[dict[str, Any]] = []
        previous_assistants: list[str] = []
        for row in reversed(rows):
            try:
                media = json.loads(row["media_json"] or "[]")
            except json.JSONDecodeError:
                media = []
            role = str(row["role"])
            content = str(row["content"])
            if role == "assistant":
                raw_content = content
                content = _strip_replayed_assistant_prefix(content, previous_assistants)
                previous_assistants.append(raw_content)
            else:
                previous_assistants = []
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except json.JSONDecodeError:
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            messages.append(
                {
                    "id": int(row["id"]),
                    "role": role,
                    "content": content,
                    "media": media if isinstance(media, list) else [],
                    "attachments": media if isinstance(media, list) else [],
                    **({"turn_id": str(row["turn_id"])} if row["turn_id"] else {}),
                    **metadata,
                    "created_at": str(row["created_at"]),
                }
            )
        visible_turn_ids = {
            str(message.get("turn_id") or "").strip()
            for message in messages
            if str(message.get("turn_id") or "").strip()
        }
        self._attach_ui_events_to_messages(
            messages,
            {
                turn_id: events
                for turn_id, events in events_by_turn.items()
                if turn_id in visible_turn_ids
            },
        )
        return messages

    async def list_messages_async(
        self,
        username: str,
        scope: ChatScope,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        db = await self.connect_async(
            username,
            scope,
            db_path=self.read_db_for(username, scope),
        )
        try:
            cursor = await db.execute(
                """
                SELECT id, role, content, media_json, turn_id, metadata_json, created_at
                  FROM chat_messages
                 WHERE role NOT IN ('trace', 'agent_notification')
                 ORDER BY id DESC
                 LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
            events_by_turn = await self._load_ui_events_async(db)
        finally:
            await db.close()
        messages: list[dict[str, Any]] = []
        previous_assistants: list[str] = []
        for row in reversed(rows):
            message = self._message_from_row(row)
            role = str(message["role"])
            content = str(message["content"])
            if role == "assistant":
                raw_content = content
                message["content"] = _strip_replayed_assistant_prefix(
                    content,
                    previous_assistants,
                )
                previous_assistants.append(raw_content)
            else:
                previous_assistants = []
            metadata = message.pop("metadata", {})
            if isinstance(metadata, dict):
                message.update(metadata)
            messages.append(message)
        visible_turn_ids = {
            str(message.get("turn_id") or "").strip()
            for message in messages
            if str(message.get("turn_id") or "").strip()
        }
        self._attach_ui_events_to_messages(
            messages,
            {
                turn_id: events
                for turn_id, events in events_by_turn.items()
                if turn_id in visible_turn_ids
            },
        )
        return messages

    def recent_user_messages(
        self,
        username: str,
        scope: ChatScope,
        *,
        created_after: str,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Read bounded visible role=user input from one already authorized scope.

        A shared canvas chat can contain several participants. These rows are
        creative input, not per-message actor proof or a new approval receipt.
        Read canonical columns only; rendered metadata/UI events cannot change
        who the database says authored a message or replace its visible text.
        """
        def utc(value: str) -> datetime | None:
            try:
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
            except (AttributeError, TypeError, ValueError):
                return None
        cutoff = utc(created_after)
        if cutoff is None:
            return []
        db_path = self.read_db_for(username, scope)
        if not db_path.is_file():
            return []
        bounded_limit = max(1, min(int(limit), 8))
        # A read must not create/migrate a missing chat or load media/UI history.
        with closing(sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)) as conn:
            conn.row_factory = sqlite3.Row
            columns = {row['name'] for row in conn.execute('PRAGMA table_info(chat_messages)')}
            if not {'id', 'role', 'content', 'turn_id', 'created_at'} <= columns:
                return []
            rows = conn.execute("""SELECT id, substr(content,1,2049) AS content, turn_id, created_at
                FROM chat_messages WHERE role='user' AND julianday(created_at)>=julianday(?)
                  AND (turn_id IS NULL OR length(turn_id)<=256) AND length(created_at)<=64
                ORDER BY id DESC LIMIT ?""", (cutoff.isoformat(), bounded_limit)).fetchall()
        messages = []
        remaining = 8192 - 2  # Include the JSON array's brackets and separators.
        def wire_size(row: dict[str, Any]) -> int:
            return len(json.dumps(row, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        for row in rows:
            timestamp = utc(row['created_at'])
            raw = str(row['content'] or '')
            if timestamp is None or timestamp < cutoff or not raw.strip():
                continue
            text = raw.encode('utf-8')[:2048].decode('utf-8', errors='ignore')
            message = {'message_id': int(row['id']), 'turn_id': row['turn_id'],
                       'created_at': row['created_at'], 'content': text,
                       'content_truncated': text != raw}
            available = remaining - bool(messages)
            if wire_size(message) > available:
                message['content_truncated'] = True
                low, high = 0, len(text)
                while low < high:
                    middle = (low + high + 1) // 2
                    message['content'] = text[:middle]
                    if wire_size(message) <= available:
                        low = middle
                    else:
                        high = middle - 1
                message['content'] = text[:low]
                if not message['content'] or wire_size(message) > available:
                    break
            remaining -= wire_size(message) + bool(messages)
            messages.append(message)
        return list(reversed(messages))

    async def history_contents_async(
        self,
        username: str,
        scope: ChatScope,
        role: str,
        *,
        limit: int,
    ) -> list[str]:
        db = await self.connect_async(
            username,
            scope,
            db_path=self.read_db_for(username, scope),
        )
        try:
            cursor = await db.execute(
                """
                SELECT content
                  FROM chat_messages
                 WHERE role = ?
                 ORDER BY id DESC
                 LIMIT ?
                """,
                (role, limit),
            )
            rows = await cursor.fetchall()
        finally:
            await db.close()
        return [str(row["content"] or "") for row in reversed(rows)]


chat_store = ChatStore()
