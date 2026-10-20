"""Rebuildable project index. Facts do not replace artifact authorization."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_MAX_BYTES = 64 * 1024
_SCOPE_FIELDS = ("project_id", "canvas_id", "requester_user_id", "agent_id")


class ProductionIndexConflict(ValueError):
    pass


def _canonical(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _identity(scope: dict) -> dict:
    identity = {key: scope.get(key) for key in _SCOPE_FIELDS}
    if any(not isinstance(value, str) or not value.strip() or len(value) > 256
           for value in identity.values()):
        raise ValueError("production index requires a complete host scope")
    tenant = scope.get("tenant_id") or scope.get("storage_org_id") or ("user:" + identity["requester_user_id"])
    if not isinstance(tenant, str) or not tenant.strip() or len(tenant) > 256:
        raise ValueError("production index requires a valid host tenant")
    identity["tenant_id"] = tenant
    return identity


def _read(path: Path) -> dict | None:
    if path.is_symlink():
        raise ValueError("production index contains a symlink")
    try:
        with path.open("rb") as stream:
            content = stream.read(_MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(content) > _MAX_BYTES:
        raise ValueError("production index record exceeds its bound")
    value = json.loads(content)
    if not isinstance(value, dict):
        raise ValueError("invalid production index record")
    return value


def _atomic(path: Path, value: dict, guard) -> None:
    content = _canonical(value)
    if len(content) > _MAX_BYTES:
        raise ValueError("production index record exceeds its bound")
    fd, name = tempfile.mkstemp(prefix=".production-index-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        guard.reassert()
        if path.is_symlink():
            raise ValueError("production index contains a symlink")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class LocalProductionIndex:
    @staticmethod
    def _location(scope: dict, commission_id: str) -> tuple[Path, dict, str]:
        if not isinstance(commission_id, str) or not _ID.fullmatch(commission_id):
            raise ValueError("invalid commission id")
        identity = _identity(scope)
        if not isinstance(scope.get("project_dir"), (Path, str)):
            raise ValueError("host project directory required")
        project_dir = Path(scope["project_dir"]).resolve()
        scope_hash = hashlib.sha256(_canonical(identity)).hexdigest()
        folder = project_dir / "freezone" / "_production_index" / scope_hash / commission_id
        for parent in (project_dir / "freezone", project_dir / "freezone" / "_production_index",
                       folder.parent, folder):
            if parent.is_symlink():
                raise ValueError("production index contains a symlink")
        return folder, identity, scope_hash

    def read_current(self, scope: dict, commission_id: str) -> dict | None:
        folder, identity, _ = self._location(scope, commission_id)
        envelope = _read(folder / "head.json")
        if envelope is None:
            return None
        if envelope.get("scope") != identity:
            raise ValueError("production index scope differs")
        if type(envelope.get("sequence")) is not int or envelope["sequence"] < 1:
            raise ValueError("invalid production index sequence")
        generation = envelope.get("history_generation")
        if generation is not None and (not isinstance(generation, str) or not re.fullmatch(r"[a-f0-9]{32}", generation)):
            raise ValueError("invalid production index history generation")
        record = envelope.get("record")
        if (not isinstance(record, dict)
                or not isinstance(record.get("commission_ref"), dict)
                or record["commission_ref"].get("artifact_id") != commission_id
                or record.get("index_revision") != hashlib.sha256(
                    _canonical({key: value for key, value in record.items()
                                if key != "index_revision"})).hexdigest()):
            raise ValueError("production index record differs")
        return record

    def publish(self, scope: dict, record: dict, *, expected_revision: str | None) -> dict:
        from novelvideo.ports import get_canvas_write_mutex

        if not isinstance(record, dict) or not isinstance(record.get("commission_ref"), dict):
            raise ValueError("invalid production index commission reference")
        commission_id = record["commission_ref"].get("artifact_id")
        folder, identity, scope_hash = self._location(scope, commission_id)
        clean = {key: value for key, value in record.items() if key != "index_revision"}
        current_record = json.loads(_canonical(clean))
        current_record.setdefault("projection_revision", hashlib.sha256(_canonical(clean)).hexdigest())
        current_record["index_revision"] = hashlib.sha256(_canonical(current_record)).hexdigest()
        if len(_canonical(current_record)) > _MAX_BYTES:
            raise ValueError("production index record exceeds its bound")
        with get_canvas_write_mutex().write_mutex(Path(scope["project_dir"]).resolve(),
                                                  "production_index_" + scope_hash[:40]) as guard:
            folder, _, _ = self._location(scope, commission_id)
            old = self.read_current(scope, commission_id)
            if old == current_record:
                guard.reassert()
                return old
            if (old or {}).get("index_revision") != expected_revision:
                raise ProductionIndexConflict("production index revision changed")
            folder.mkdir(parents=True, exist_ok=True)
            head = _read(folder / "head.json") or {}
            sequence = head.get("sequence", 0)
            if type(sequence) is not int or sequence < 0:
                raise ValueError("invalid production index sequence")
            generation = head.get("history_generation") if head else uuid.uuid4().hex
            envelope = {"scope": identity, "sequence": sequence + 1, "record": current_record,
                        "history_generation": generation}
            _atomic(folder / f"{sequence + 1}.json", envelope, guard)
            _atomic(folder / "head.json", envelope, guard)
            return current_record

    def recover_current(self, scope: dict, commission_id: str) -> dict | None:
        """Optional scoped repair; a valid concurrent publication always wins.

        Invalid derived metadata is quarantined, never an artifact. The caller
        must still CAS-publish with expected_revision=None after a reset.
        """
        from novelvideo.ports import get_canvas_write_mutex

        folder, _, scope_hash = self._location(scope, commission_id)
        with get_canvas_write_mutex().write_mutex(Path(scope["project_dir"]).resolve(),
                                                  "production_index_" + scope_hash[:40]) as guard:
            folder, _, _ = self._location(scope, commission_id)
            try:
                current = self.read_current(scope, commission_id)
            except ValueError:
                current = None
            else:
                guard.reassert()
                return current
            # Keep the old derived history as quarantine. A fresh history
            # generation makes pre-recovery cursors fail rather than silently
            # point to unrelated new sequence numbers after a damaged head.
            guard.reassert()
            quarantine = folder.parent / ("_corrupt_" + commission_id + "_" + uuid.uuid4().hex)
            os.replace(folder, quarantine)
            return None

    def history(self, scope: dict, commission_id: str, *, cursor: str | None, limit: int) -> dict:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("production history limit must be between 1 and 100")
        folder, identity, scope_hash = self._location(scope, commission_id)
        head = _read(folder / "head.json")
        generation = (head or {}).get("history_generation")
        head_sequence = (head or {}).get("sequence", 0)
        if head is not None and (
                head.get("scope") != identity or type(head_sequence) is not int or head_sequence < 1
                or (generation is not None and (not isinstance(generation, str)
                    or not re.fullmatch(r"[a-f0-9]{32}", generation)))):
            raise ValueError("invalid production history head")
        cursor_scope = f"{scope_hash}:{commission_id}"
        if generation is not None:
            cursor_scope += ":" + generation
        fingerprint = hashlib.sha256(cursor_scope.encode()).hexdigest()[:24]
        if cursor is None:
            sequence = head_sequence
        else:
            if not isinstance(cursor, str) or len(cursor) > 64:
                raise ValueError("invalid production history cursor")
            match = re.fullmatch(r"([a-f0-9]{24}):(\d+)", cursor)
            if not match or match.group(1) != fingerprint:
                raise ValueError("invalid production history cursor")
            sequence = int(match.group(2))
        if type(sequence) is not int or sequence < 0 or sequence > head_sequence:
            raise ValueError("invalid production history cursor")
        items = []
        for number in range(sequence, max(sequence - limit, 0), -1):
            envelope = _read(folder / f"{number}.json")
            if (envelope is None or envelope.get("scope") != identity
                    or envelope.get("history_generation") != generation
                    or type(envelope.get("sequence")) is not int or envelope["sequence"] != number
                    or not isinstance(envelope.get("record"), dict)):
                raise ValueError("production history is unavailable")
            items.append(envelope["record"])
        after = sequence - len(items)
        return {"items": items, "next_cursor": f"{fingerprint}:{after}" if after > 0 else None}
