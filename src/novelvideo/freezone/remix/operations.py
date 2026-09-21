"""Durable, project-scoped idempotency records for remix producer calls."""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
import tempfile
import uuid
import re
import time
from typing import Any


class RemixOperationStore:
    def __init__(self, project_dir: Path, project_id: str) -> None:
        self.project_dir = Path(project_dir).resolve()
        if not isinstance(project_id, str) or not project_id:
            raise ValueError("project_id is required")
        self.project_id = project_id
        self.root = self.project_dir / "freezone" / "_remix_operations"

    @contextmanager
    def _locked(self):
        from novelvideo.ports import get_canvas_write_mutex

        self.root.mkdir(parents=True, exist_ok=True)
        with get_canvas_write_mutex().write_mutex(self.project_dir, "remix_operations") as guard:
            yield guard

    @staticmethod
    def _json(value: dict[str, Any]) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()

    def _atomic(self, path: Path, value: dict[str, Any], guard: Any) -> None:
        fd, name = tempfile.mkstemp(prefix=".remix-operation-", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(self._json(value))
                stream.flush()
                os.fsync(stream.fileno())
            guard.reassert()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _refs(self, refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(refs, list):
            raise ValueError("input_refs must be a list")
        result = []
        for ref in refs:
            if not isinstance(ref, dict) or ref.get("project_id", self.project_id) != self.project_id:
                raise ValueError("Cross-project references are not allowed")
            artifact_id, revision = ref.get("artifact_id"), ref.get("revision")
            if not isinstance(artifact_id, str) or not artifact_id or type(revision) is not int or revision < 1:
                raise ValueError("invalid artifact reference")
            result.append({"artifact_id": artifact_id, "revision": revision, "project_id": self.project_id})
        return result

    @staticmethod
    def _media_hashes(values: list[str] | None) -> list[str]:
        if values is None:
            return []
        if not isinstance(values, list):
            raise ValueError("input_media_hashes must be a list")
        normalized: list[str] = []
        for value in values:
            if not isinstance(value, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", value.strip().lower()):
                raise ValueError("invalid input media hash")
            normalized.append(value.strip().lower())
        return sorted(set(normalized))

    def _path(self, operation_id: str) -> Path:
        if not isinstance(operation_id, str) or not re.fullmatch(r"[a-f0-9]{32}", operation_id):
            raise ValueError("invalid operation ID")
        return self.root / f"{operation_id}.json"

    def get(self, operation_id: str) -> dict[str, Any]:
        return self._read(operation_id)

    def list(self, *, limit: int = 50) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if not self.root.is_dir():
            return []
        paths = sorted(
            (path for path in self.root.glob("*.json") if re.fullmatch(r"[a-f0-9]{32}\.json", path.name)),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        return [self._read(path.stem) for path in paths[:limit]]

    def _read(self, operation_id: str) -> dict[str, Any]:
        try:
            record = json.loads(self._path(operation_id).read_text())
        except FileNotFoundError:
            raise
        if not isinstance(record, dict) or record.get("project_id") != self.project_id:
            raise FileNotFoundError("operation not found")
        return record

    def _outputs_exist(self, output_refs: list[dict[str, Any]]) -> bool:
        for ref in output_refs:
            if isinstance(ref, dict) and "artifact_id" in ref:
                from .store import RemixArtifactStore

                try:
                    RemixArtifactStore(self.project_dir, self.project_id).read(
                        ref["artifact_id"], ref["revision"]
                    )
                except (FileNotFoundError, KeyError, ValueError):
                    return False
                continue
            relative = ref.get("project_path") if isinstance(ref, dict) else None
            if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
                return False
            path = (self.project_dir / relative).resolve()
            if not path.is_relative_to(self.project_dir) or not path.is_file():
                return False
        return True

    def start(self, *, idempotency_key: str, input_refs: list[dict[str, Any]], producer_version: str, normalized_parameters: dict[str, Any], node_revision: int, regenerate: bool = False, canvas_id: str = "", node_id: str = "", input_media_hashes: list[str] | None = None) -> dict[str, Any]:
        if not isinstance(idempotency_key, str) or not idempotency_key or not isinstance(producer_version, str) or not producer_version or type(node_revision) is not int or node_revision < 1 or not isinstance(normalized_parameters, dict):
            raise ValueError("invalid operation request")
        if bool(canvas_id) != bool(node_id) or (canvas_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", canvas_id)) or (node_id and (not isinstance(node_id, str) or len(node_id) > 128)):
            raise ValueError("invalid canvas or node identity")
        refs = self._refs(input_refs)
        media_hashes = self._media_hashes(input_media_hashes)
        key = hashlib.sha256(f"{self.project_id}\0{idempotency_key}".encode()).hexdigest()
        with self._locked() as guard:
            index_path = self.root / "index.json"
            index = json.loads(index_path.read_text()) if index_path.exists() else {}
            old_id = index.get(key)
            if old_id and not regenerate:
                old = self._read(old_id)
                if (old["input_refs"] != refs or old.get("input_media_hashes", []) != media_hashes or old["producer_version"] != producer_version
                    or old["normalized_parameters"] != normalized_parameters
                    or old["node_revision"] != node_revision
                    or old.get("canvas_id", "") != canvas_id or old.get("node_id", "") != node_id):
                    raise ValueError("Idempotency key reused for a different request")
                if old["status"] not in {"completed", "needs_confirmation"} or self._outputs_exist(old.get("output_refs", [])):
                    return {**old, "created": False}
            operation_id = uuid.uuid4().hex
            record = {"operation_id": operation_id, "project_id": self.project_id, "idempotency_key_hash": key, "input_refs": refs, "input_media_hashes": media_hashes, "producer_version": producer_version, "normalized_parameters": normalized_parameters, "node_revision": node_revision, "canvas_id": canvas_id, "node_id": node_id, "task_id": "", "provider_task_id": "", "status": "pending", "output_refs": [], "supersedes": old_id or "", "superseded_by": ""}
            self._atomic(self._path(operation_id), record, guard)
            if old_id:
                old = self._read(old_id)
                old["superseded_by"] = operation_id
                self._atomic(self._path(old_id), old, guard)
            index[key] = operation_id
            self._atomic(index_path, index, guard)
            return {**record, "created": True}

    def _update(self, operation_id: str, guard: Any, **changes: Any) -> dict[str, Any]:
        record = self._read(operation_id)
        record.update(changes)
        self._atomic(self._path(operation_id), record, guard)
        return record

    def provider_outcome_unknown(self, operation_id: str, *, provider_task_id: str) -> dict[str, Any]:
        with self._locked() as guard:
            return self._update(operation_id, guard, status="pending_reconciliation", provider_task_id=provider_task_id)

    def bind_task(self, operation_id: str, *, task_id: str, provider_task_id: str = "") -> dict[str, Any]:
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task_id is required")
        with self._locked() as guard:
            return self._update(operation_id, guard, task_id=task_id, provider_task_id=provider_task_id, submission_owner="", submission_claimed_at=0)

    def claim_submission(self, operation_id: str, *, owner: str, lease_seconds: int = 60) -> bool:
        if not isinstance(owner, str) or not owner:
            raise ValueError("submission owner is required")
        if type(lease_seconds) is not int or lease_seconds < 1:
            raise ValueError("submission lease must be positive")
        with self._locked() as guard:
            record = self._read(operation_id)
            claimed_at = float(record.get("submission_claimed_at") or 0)
            claim_active = bool(record.get("submission_owner")) and time.time() - claimed_at < lease_seconds
            if record["status"] != "pending" or record.get("task_id") or claim_active:
                return False
            self._update(operation_id, guard, submission_owner=owner, submission_claimed_at=time.time())
            return True

    def release_submission(self, operation_id: str, *, owner: str) -> None:
        with self._locked() as guard:
            record = self._read(operation_id)
            if record.get("submission_owner") == owner and not record.get("task_id"):
                self._update(operation_id, guard, submission_owner="", submission_claimed_at=0)

    def mark_stale(self, operation_id: str, *, reason: str) -> dict[str, Any]:
        with self._locked() as guard:
            return self._update(operation_id, guard, status="stale", stale_reason=reason)

    def cancel(self, operation_id: str, *, reason: str) -> dict[str, Any]:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("cancellation reason is required")
        with self._locked() as guard:
            record = self._read(operation_id)
            if record["status"] in {"completed", "stale"}:
                return record
            return self._update(operation_id, guard, status="cancelled", cancel_reason=reason.strip())

    def fail(self, operation_id: str, *, reason: str) -> dict[str, Any]:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("failure reason is required")
        with self._locked() as guard:
            return self._update(operation_id, guard, status="failed", failure_reason=reason.strip(), submission_owner="")

    def publish_result_unverified(self, operation_id: str, *, output_refs: list[dict[str, Any]]) -> dict[str, Any]:
        """Save durable runner output without claiming the canvas node is still current."""
        if not isinstance(output_refs, list) or not output_refs:
            raise ValueError("operation output references are unavailable")
        with self._locked() as guard:
            if not self._outputs_exist(output_refs):
                raise ValueError("operation output references are unavailable")
            record = self._read(operation_id)
            if record["status"] == "needs_confirmation" and record["output_refs"] == output_refs:
                return record
            if record.get("superseded_by"):
                return self._update(operation_id, guard, status="stale", stale_reason="superseded", output_refs=output_refs)
            if record["status"] == "cancelled":
                return self._update(operation_id, guard, status="stale", stale_reason="cancelled", output_refs=output_refs)
            if record["status"] not in {"pending", "pending_reconciliation"}:
                raise ValueError("operation cannot publish result in its current state")
            return self._update(operation_id, guard, status="needs_confirmation", output_refs=output_refs)

    def complete(self, operation_id: str, *, output_refs: list[dict[str, Any]], current_input_refs: list[dict[str, Any]], current_node_revision: int) -> dict[str, Any]:
        refs = self._refs(current_input_refs)
        with self._locked() as guard:
            record = self._read(operation_id)
            status = "completed" if record["input_refs"] == refs and record["node_revision"] == current_node_revision else "stale"
            stale_reason = "" if status == "completed" else ("input_refs_changed" if record["input_refs"] != refs else "node_revision_changed")
            return self._update(operation_id, guard, status=status, output_refs=output_refs, stale_reason=stale_reason)

    def confirm(self, operation_id: str, *, current_input_refs: list[dict[str, Any]], current_node_revision: int, canvas_id: str, node_id: str) -> dict[str, Any]:
        refs = self._refs(current_input_refs)
        with self._locked() as guard:
            record = self._read(operation_id)
            if record.get("canvas_id") and (record["canvas_id"] != canvas_id or record["node_id"] != node_id):
                raise ValueError("operation belongs to a different canvas node")
            if record["status"] in {"completed", "stale"}:
                return record
            if record["status"] != "needs_confirmation" or not self._outputs_exist(record["output_refs"]):
                raise ValueError("operation result is not available for confirmation")
            status = "completed" if record["input_refs"] == refs and record["node_revision"] == current_node_revision else "stale"
            stale_reason = "" if status == "completed" else ("input_refs_changed" if record["input_refs"] != refs else "node_revision_changed")
            return self._update(operation_id, guard, status=status, stale_reason=stale_reason)
