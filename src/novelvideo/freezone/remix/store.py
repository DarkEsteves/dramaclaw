"""Project-scoped immutable storage for structured remix artifacts."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path
import re
import tempfile
import uuid
from typing import Any

from .contracts import RemixArtifactEnvelope, validate_payload


_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_MAX_FILE_BYTES = 8 * 1024 * 1024


class RemixArtifactConflict(ValueError):
    """The supplied expected revision is no longer current."""


class RemixArtifactStore:
    def __init__(self, project_dir: Path, project_id: str, *, created_by: str = "") -> None:
        self.project_dir = Path(project_dir).resolve()
        self.project_id = self._project_id(project_id)
        self.created_by = created_by
        self.root = self.project_dir / "freezone" / "_remix_artifacts"

    @staticmethod
    def _project_id(project_id: str) -> str:
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("project_id is required")
        return project_id

    @staticmethod
    def _validate_id(artifact_id: str) -> None:
        if not isinstance(artifact_id, str) or not _ID.fullmatch(artifact_id):
            raise ValueError("Invalid remix artifact ID")

    @staticmethod
    def _validate_revision(revision: int) -> None:
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise ValueError("Revision must be positive")

    def _directory(self, artifact_id: str) -> Path:
        self._validate_id(artifact_id)
        path = self.root / artifact_id
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ValueError("Invalid remix artifact directory")
        return path

    @contextmanager
    def _locked(self):
        from novelvideo.ports import get_canvas_write_mutex

        self.project_dir.mkdir(parents=True, exist_ok=True)
        freezone = self.project_dir / "freezone"
        if freezone.is_symlink() or self.root.is_symlink():
            raise ValueError("Remix artifact storage contains a symlink")
        freezone.mkdir(exist_ok=True)
        self.root.mkdir(exist_ok=True)
        with get_canvas_write_mutex().write_mutex(self.project_dir, "remix_artifacts") as guard:
            yield guard

    @staticmethod
    def _canonical(value: dict[str, Any]) -> bytes:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _reference_list(upstream_refs: list[dict[str, Any]], project_id: str) -> list[dict[str, Any]]:
        if not isinstance(upstream_refs, list):
            raise ValueError("upstream_refs must be a list")
        normalized: list[dict[str, Any]] = []
        for reference in upstream_refs:
            if not isinstance(reference, dict):
                raise ValueError("upstream_refs must contain objects")
            artifact_id = reference.get("artifact_id")
            revision = reference.get("revision")
            ref_project = reference.get("project_id", project_id)
            if ref_project != project_id:
                raise ValueError("Cross-project artifact references are not allowed")
            RemixArtifactStore._validate_id(artifact_id)
            RemixArtifactStore._validate_revision(revision)
            normalized.append({"artifact_id": artifact_id, "revision": revision, "project_id": project_id})
        return normalized

    def _path(self, artifact_id: str, revision: int, suffix: str) -> Path:
        self._validate_revision(revision)
        return self._directory(artifact_id) / f"{revision}{suffix}"

    def _validate_upstream_refs(self, refs: list[dict[str, Any]]) -> None:
        """Resolve every declared dependency within this project's storage scope."""
        for reference in refs:
            self._read(reference["artifact_id"], reference["revision"])

    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as exc:
            raise FileNotFoundError("Artifact or revision not found") from exc
        try:
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("Invalid remix artifact file")
                if info.st_size > _MAX_FILE_BYTES:
                    raise ValueError("Remix artifact file exceeds size limit")
                content = stream.read(_MAX_FILE_BYTES + 1)
                if len(content) > _MAX_FILE_BYTES:
                    raise ValueError("Remix artifact file exceeds size limit")
            value = json.loads(content.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid remix artifact file") from exc
        if not isinstance(value, dict):
            raise ValueError("Invalid remix artifact file")
        return value

    def _read(self, artifact_id: str, revision: int) -> dict[str, Any]:
        envelope_path = self._path(artifact_id, revision, ".json")
        raw = self._read_json(envelope_path)
        try:
            envelope = RemixArtifactEnvelope.model_validate(raw)
        except Exception as exc:
            raise ValueError("Invalid remix artifact envelope") from exc
        if envelope.id != artifact_id or envelope.revision != revision or envelope.project_id != self.project_id:
            raise FileNotFoundError("Artifact or revision not found")
        expected_payload_path = f"{revision}.payload.json"
        if envelope.payload_path != expected_payload_path:
            raise ValueError("Invalid remix artifact payload path")
        payload_path = envelope_path.parent / envelope.payload_path
        payload = self._read_json(payload_path)
        if "sha256:" + hashlib.sha256(self._canonical(payload)).hexdigest() != envelope.content_hash:
            raise ValueError("Remix artifact content hash mismatch")
        validated = validate_payload(envelope.schema_name, payload)
        return {**envelope.model_dump(mode="json", by_alias=True), "payload": validated}

    def _latest_revision(self, artifact_id: str) -> int:
        directory = self._directory(artifact_id)
        revisions = [int(entry.stem) for entry in directory.glob("*.json") if entry.stem.isdecimal()]
        if not revisions:
            raise FileNotFoundError("Artifact or revision not found")
        return max(revisions)

    def _atomic_json(self, target: Path, value: dict[str, Any], guard: Any) -> None:
        fd, temporary_name = tempfile.mkstemp(prefix=".remix-", suffix=".tmp", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(self._canonical(value))
                stream.flush()
                os.fsync(stream.fileno())
            guard.reassert()
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def _publish(
        self, *, artifact_id: str, schema: str, revision: int, payload: dict[str, Any],
        upstream_refs: list[dict[str, Any]], created_by: str, guard: Any,
    ) -> dict[str, Any]:
        directory = self._directory(artifact_id)
        directory.mkdir(exist_ok=True)
        envelope_path = self._path(artifact_id, revision, ".json")
        if envelope_path.exists():
            raise RemixArtifactConflict("Artifact revision already exists")
        payload_path = self._path(artifact_id, revision, ".payload.json")
        content_hash = "sha256:" + hashlib.sha256(self._canonical(payload)).hexdigest()
        envelope = RemixArtifactEnvelope(
            id=artifact_id, schema=schema, revision=revision, project_id=self.project_id,
            created_by=created_by, content_hash=content_hash, upstream_refs=upstream_refs,
            producer_version="remix-store.v1", payload_path=payload_path.name,
        ).model_dump(mode="json", by_alias=True)
        # The envelope is the commit record. A crashed writer can leave an orphaned
        # payload, but readers can never observe a published partial revision.
        self._atomic_json(payload_path, payload, guard)
        self._atomic_json(envelope_path, envelope, guard)
        return {**envelope, "payload": payload}

    def create(self, schema: str, payload: dict[str, Any], upstream_refs: list[dict[str, Any]]) -> dict[str, Any]:
        validated = validate_payload(schema, payload)
        refs = self._reference_list(upstream_refs, self.project_id)
        artifact_id = uuid.uuid4().hex
        with self._locked() as guard:
            self._validate_upstream_refs(refs)
            return self._publish(
                artifact_id=artifact_id, schema=schema, revision=1, payload=validated,
                upstream_refs=refs, created_by=self.created_by, guard=guard,
            )

    def read(self, artifact_id: str, revision: int) -> dict[str, Any]:
        self._validate_id(artifact_id)
        self._validate_revision(revision)
        with self._locked():
            return self._read(artifact_id, revision)

    def revise(
        self, artifact_id: str, expected_revision: int, payload: dict[str, Any],
        upstream_refs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self._validate_id(artifact_id)
        self._validate_revision(expected_revision)
        refs = self._reference_list(upstream_refs, self.project_id)
        with self._locked() as guard:
            self._validate_upstream_refs(refs)
            current = self._read(artifact_id, self._latest_revision(artifact_id))
            if current["revision"] != expected_revision:
                raise RemixArtifactConflict("Artifact changed; reload the latest revision before saving")
            validated = validate_payload(current["schema"], payload)
            return self._publish(
                artifact_id=artifact_id, schema=current["schema"], revision=expected_revision + 1,
                payload=validated, upstream_refs=refs, created_by=current["created_by"], guard=guard,
            )
