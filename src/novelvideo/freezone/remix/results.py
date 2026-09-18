"""Apply a finished remix render only while its originating canvas node is current."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from urllib.parse import quote

from novelvideo.freezone import canvas_store
from novelvideo.freezone.remix.operations import RemixOperationStore


class RemixNodeConflict(ValueError):
    """The supplied node identity does not match the recorded operation."""


class _StaleNode(Exception):
    pass


def _node(canvas: dict | None, node_id: str) -> dict | None:
    if not isinstance(canvas, dict):
        return None
    return next((node for node in canvas.get("nodes", []) if isinstance(node, dict) and node.get("id") == node_id), None)


def _current(node: dict | None) -> tuple[list[dict], int]:
    data = node.get("data") if isinstance(node, dict) else None
    if not isinstance(data, dict):
        return [], 0
    ref = data.get("remixManifestRef")
    if not isinstance(ref, dict) or not isinstance(ref.get("artifactId"), str) or type(ref.get("revision")) is not int:
        return [], 0
    revision = data.get("remixNodeRevision")
    return [{"artifact_id": ref["artifactId"], "revision": ref["revision"]}], revision if type(revision) is int else 0


def confirm_render(output_dir: Path, state_dir: Path, project_id: str, operation_id: str, *, canvas_id: str, node_id: str) -> dict:
    operations = RemixOperationStore(output_dir, project_id)
    record = operations.get(operation_id)
    if not record.get("canvas_id") or not record.get("node_id"):
        raise RemixNodeConflict("operation was not bound to a canvas node")
    if record.get("canvas_id") and (record["canvas_id"] != canvas_id or record["node_id"] != node_id):
        raise RemixNodeConflict("operation belongs to a different canvas node")
    if record["status"] in {"completed", "stale"}:
        return record
    if record["status"] != "needs_confirmation":
        raise ValueError("operation result is not available for confirmation")
    output_refs = record.get("output_refs") or []
    if len(output_refs) != 1 or not isinstance(output_refs[0].get("project_path"), str):
        raise ValueError("operation does not contain one render output")
    relative = output_refs[0]["project_path"]
    if not relative.startswith("freezone/remix_renders/"):
        raise ValueError("operation does not contain a remix render")
    path = (Path(output_dir) / relative).resolve()
    if not path.is_relative_to(Path(output_dir).resolve()) or not path.is_file():
        raise ValueError("render output is unavailable")
    url = f"/api/v1/projects/{quote(project_id, safe='')}/media/{quote(relative, safe='/')}"

    # The node check and write share the canvas mutex. A concurrent edit either
    # lands before this check (stale) or after our versioned save.
    def build(existing: dict | None) -> dict:
        node = _node(existing, node_id)
        refs, revision = _current(node)
        expected = record["input_refs"]
        expected_exact = [{"artifact_id": ref["artifact_id"], "revision": ref["revision"]} for ref in expected]
        if refs != expected_exact or revision != record["node_revision"]:
            raise _StaleNode()
        updated = deepcopy(existing)
        target = _node(updated, node_id)
        assert target is not None
        target["data"]["resultVideoUrl"] = url
        target["data"]["remixRenderOperationId"] = operation_id
        target["data"]["remixRenderManifestRef"] = {
            "artifactId": expected[0]["artifact_id"], "revision": expected[0]["revision"]
        }
        updated["revision"] = int(updated["revision"]) + 1
        updated["updated_at"] = canvas_store.utc_now_iso()
        updated["save_source"] = "remix_result_confirmation"
        return updated

    try:
        canvas_store.save_canvas(
            Path(state_dir), canvas_id, base_revision=None,
            build_payload=build, enforce_revision=False,
            save_source="remix_result_confirmation",
        )
    except _StaleNode:
        canvas = canvas_store.read_canvas(Path(state_dir), canvas_id)
        refs, revision = _current(_node(canvas, node_id))
        return operations.confirm(operation_id, current_input_refs=refs, current_node_revision=revision, canvas_id=canvas_id, node_id=node_id)
    return operations.confirm(
        operation_id, current_input_refs=record["input_refs"],
        current_node_revision=record["node_revision"], canvas_id=canvas_id, node_id=node_id,
    )
