"""Apply a finished remix render only while its originating canvas node is current."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from urllib.parse import quote

from novelvideo.freezone import canvas_store
from novelvideo.freezone.remix.operations import RemixOperationStore
from novelvideo.freezone.remix.store import RemixArtifactStore


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

    # This state claim is the serialization point with cancellation and
    # provider-timeout reconciliation. Once claimed, neither can win between
    # the exact-input check and the canvas write.
    record = operations.claim_confirmation(operation_id, canvas_id=canvas_id, node_id=node_id)
    if record["status"] == "completed":
        return record

    def exact_inputs(node: dict | None) -> bool:
        refs, revision = _current(node)
        expected = [{"artifact_id": ref["artifact_id"], "revision": ref["revision"]} for ref in record["input_refs"]]
        data = node.get("data") if isinstance(node, dict) else None
        if (refs != expected or revision != record["node_revision"] or not isinstance(data, dict)
            or data.get("remixInputsCurrent") is not True
            or data.get("remixInputFingerprint") != (record.get("normalized_parameters") or {}).get("input_fingerprint")
            or record.get("producer_version") != "remix-render.v1"):
            return False
        try:
            manifest = RemixArtifactStore(Path(output_dir), project_id).read(expected[0]["artifact_id"], expected[0]["revision"])
        except (FileNotFoundError, ValueError):
            return False
        hashes = sorted({str(item.get("media_hash")).lower() for item in manifest["payload"].get("media_bindings", []) if isinstance(item, dict) and isinstance(item.get("media_hash"), str)})
        if hashes != record.get("input_media_hashes", []):
            return False
        if not operations.media_bindings_unchanged(
            manifest["payload"].get("media_bindings"),
            record.get("input_media_hashes"),
        ):
            raise _StaleNode("input_media_changed")
        return True

    # The node check and write share the canvas mutex. A concurrent edit either
    # lands before this check (stale) or after our versioned save.
    def build(existing: dict | None) -> dict:
        node = _node(existing, node_id)
        expected = record["input_refs"]
        if not exact_inputs(node):
            raise _StaleNode("exact_inputs_changed")
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
    except _StaleNode as exc:
        return operations.mark_stale(operation_id, reason=str(exc) or "exact_inputs_changed")
    except Exception:
        operations.release_confirmation(operation_id)
        raise
    return operations.finish_confirmation(operation_id)
