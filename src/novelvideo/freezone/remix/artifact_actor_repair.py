"""Conservative maintenance for legacy native artifacts with a blank owner."""

from __future__ import annotations

import json
import hashlib
import sqlite3
from pathlib import Path
from typing import Any

from novelvideo.freezone.canvas_store import read_canvas
from novelvideo.freezone.remix.operations import RemixOperationStore
from novelvideo.freezone.remix.store import RemixArtifactStore


def _has_ref(refs: list[dict], target: dict) -> bool:
    return any(row.get('artifact_id') == target.get('artifact_id')
               and row.get('revision') == target.get('revision') for row in refs)


def _verified_actor(
    *, output_dir: Path, state_dir: Path, project_id: str, canvas_id: str,
    artifact_id: str, expected_revision: int, operation_id: str,
) -> str:
    operation = RemixOperationStore(output_dir, project_id).get(operation_id)
    ref = {"artifact_id": artifact_id, "revision": expected_revision}
    if (operation.get("project_id") != project_id
            or operation.get("canvas_id") != canvas_id
            or operation.get("status") not in {"needs_confirmation", "completed"}):
        raise ValueError("operation does not prove this artifact and canvas")
    task_id = str(operation.get("task_id") or "")
    node_id = str(operation.get("node_id") or "")
    if not task_id or not node_id:
        raise ValueError("operation lacks native task or node identity")
    canvas = read_canvas(state_dir, canvas_id)
    if (not isinstance(canvas, dict) or canvas.get("project_id") != project_id
            or canvas.get("canvas_id") != canvas_id
            or not any(isinstance(node, dict) and node.get("id") == node_id
                       for node in canvas.get("nodes", []))):
        raise ValueError("canvas does not contain the original operation node")
    db_path = (state_dir / "data.db").resolve()
    if not db_path.is_file() or not db_path.is_relative_to(state_dir.resolve()):
        raise ValueError("native task database is unavailable")
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT task_id, task_type, project_id, requester_user_id, status, result_json "
                "FROM task_states WHERE task_id = ? AND project_id = ?", (task_id, project_id),
            ).fetchall()
    except sqlite3.Error as exc:
        raise ValueError("native task proof is unavailable") from exc
    if len(rows) != 1:
        raise ValueError("native task identity is ambiguous or expired")
    task = dict(rows[0])
    allowed_types = {"remix_generate_production_plan_draft", "remix_compile_production_plan",
                     "remix_joint_reference_analysis"}
    if task["task_type"] not in allowed_types or task["status"] != "completed":
        raise ValueError("native production task is not completed")
    actor = str(task.get("requester_user_id") or "").strip()
    if not actor or canvas.get("owner_principal_id") != actor:
        raise ValueError("canvas owner does not match original task actor")
    try:
        result: Any = json.loads(task.get("result_json") or "null")
    except ValueError as exc:
        raise ValueError("native task result is invalid") from exc
    if not isinstance(result, dict):
        raise ValueError("native task result does not reference this artifact")
    if task["task_type"] == "remix_joint_reference_analysis":
        analysis_ref = result.get("artifact_ref")
        if (not isinstance(analysis_ref, dict)
                or analysis_ref not in operation.get("output_refs", [])):
            raise ValueError("operation does not prove reference analysis")
        store = RemixArtifactStore(output_dir, project_id)
        analysis = store._read(analysis_ref["artifact_id"], analysis_ref["revision"])
        if analysis["schema"] != "reference_analysis.v1":
            raise ValueError("native reference task result is not analysis")
        if ref != analysis_ref:
            field = next((key for key in ("evidence_ref", "blueprint_ref")
                          if result.get(key) == ref and analysis["payload"].get(key) == ref), None)
            if field is None or not _has_ref(analysis["upstream_refs"], ref):
                raise ValueError("reference artifact is not in the proven analysis chain")
            linked = store._read(artifact_id, expected_revision)
            if linked["schema"] != {"evidence_ref": "reference_evidence.v1",
                                   "blueprint_ref": "reference_blueprint.v1"}[field]:
                raise ValueError("reference artifact schema differs from the proven chain")
        evidence_ref = analysis["payload"].get("evidence_ref")
        evidence = store._read(evidence_ref["artifact_id"], evidence_ref["revision"])
        if evidence["schema"] != "reference_evidence.v1" or not _has_ref(analysis["upstream_refs"], evidence_ref):
            raise ValueError("reference evidence chain is invalid")
        blueprint_ref = analysis["payload"].get("blueprint_ref")
        blueprint = store._read(blueprint_ref["artifact_id"], blueprint_ref["revision"])
        if (blueprint["schema"] != "reference_blueprint.v1"
                or not _has_ref(analysis["upstream_refs"], blueprint_ref)
                or blueprint["payload"].get("evidence_ref") != evidence_ref
                or not _has_ref(blueprint["upstream_refs"], evidence_ref)):
            raise ValueError("reference blueprint chain is invalid")
        media_path = (store.project_dir / str(analysis["payload"].get("media_path") or "")).resolve()
        if not media_path.is_relative_to(store.project_dir.resolve()) or not media_path.is_file():
            raise ValueError("reference media hash cannot be checked")
        digest = hashlib.sha256()
        with media_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if (analysis["payload"].get("media_hash") != "sha256:" + digest.hexdigest()
                or evidence["payload"].get("media_hash") != digest.hexdigest()):
            raise ValueError("reference media hash changed")
    elif result.get("artifact_ref") != ref or ref not in operation.get("output_refs", []):
        raise ValueError("native task result does not reference this artifact")
    return actor


def repair_blank_artifact_actor(
    *, output_dir: Path, state_dir: Path, project_id: str, canvas_id: str,
    artifact_id: str, expected_revision: int, operation_id: str,
    apply: bool = False,
) -> dict[str, Any]:
    """Report a repair by default; apply only with complete saved provenance."""
    output_dir = Path(output_dir).resolve()
    state_dir = Path(state_dir).resolve()
    def proof() -> str:
        return _verified_actor(
            output_dir=output_dir, state_dir=state_dir, project_id=project_id,
            canvas_id=canvas_id, artifact_id=artifact_id,
            expected_revision=expected_revision, operation_id=operation_id,
        )
    actor = proof()
    store = RemixArtifactStore(output_dir, project_id)
    return store.repair_blank_created_by(
        artifact_id=artifact_id, expected_revision=expected_revision,
        actor=actor, operation_id=operation_id,
        verify_provenance=proof, apply=apply,
    )
