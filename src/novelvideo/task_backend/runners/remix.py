"""Project-bound rendering of immutable remix composition artifacts."""
from __future__ import annotations

import re
import hashlib
from pathlib import Path
from typing import Any

from novelvideo.freezone.remix.render import render_manifest, validate_manifest
from novelvideo.freezone.remix.alignment import align_script, transcribe_media
from novelvideo.freezone.remix.evidence import build_reference_evidence
from novelvideo.freezone.remix.timing import compile_timing
from novelvideo.freezone.remix.store import RemixArtifactStore
from novelvideo.freezone.remix.operations import RemixOperationStore
from novelvideo.project_context import ProjectContext
from novelvideo.task_backend.registry import register_project_task_runner


_JOB_ID = re.compile(r"[a-f0-9]{64}\Z")


def _project_media_path(project_dir: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("remix processing requires a project-relative media path")
    root = project_dir.resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise FileNotFoundError("remix processing media is unavailable")
    return candidate


def _publish_operation_result(payload: dict[str, Any], ctx: ProjectContext, output_ref: dict[str, Any]) -> None:
    operation_id = payload.get("operation_id")
    if operation_id:
        RemixOperationStore(Path(ctx.output_dir), ctx.project_id).publish_result_unverified(
            str(operation_id), output_refs=[output_ref]
        )


def _verified_project_media_path(project_dir: Path, payload: dict[str, Any]) -> Path:
    path = _project_media_path(project_dir, payload.get("media_path"))
    expected = payload.get("media_sha256")
    if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise ValueError("remix media content hash is required")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1_048_576), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise ValueError("remix media changed since submission")
    return path


def run_remix_transcribe_media(envelope: dict[str, Any], ctx: ProjectContext) -> dict[str, Any]:
    """Publish a normalized transcript from a server-confirmed project media path."""
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
    project_dir = Path(ctx.output_dir).resolve()
    media_path = _verified_project_media_path(project_dir, payload)
    language = payload.get("language")
    if not isinstance(language, str) or not language:
        raise ValueError("remix transcription language is required")
    timeline = transcribe_media(media_path, language)
    artifact = RemixArtifactStore(project_dir, ctx.project_id).create(
        schema="word_timeline.v1", payload=timeline, upstream_refs=[]
    )
    _publish_operation_result(payload, ctx, {"artifact_id": artifact["id"], "revision": artifact["revision"]})
    return {"status": "completed", "artifact_ref": {"artifact_id": artifact["id"], "revision": artifact["revision"]}}


def run_remix_analyze_reference_video(envelope: dict[str, Any], ctx: ProjectContext) -> dict[str, Any]:
    """Publish timestamped evidence for the entire uploaded reference video."""
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
    project_dir = Path(ctx.output_dir).resolve()
    media_path = _verified_project_media_path(project_dir, payload)
    evidence = build_reference_evidence(media_path)
    artifact = RemixArtifactStore(project_dir, ctx.project_id).create(
        schema="reference_evidence.v1", payload=evidence, upstream_refs=[]
    )
    ref = {"artifact_id": artifact["id"], "revision": artifact["revision"]}
    _publish_operation_result(payload, ctx, ref)
    return {"status": "completed", "artifact_ref": ref}


def run_remix_align_script(envelope: dict[str, Any], ctx: ProjectContext) -> dict[str, Any]:
    """Align server-confirmed narration to one immutable remix script revision."""
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
    project_dir = Path(ctx.output_dir).resolve()
    media_path = _verified_project_media_path(project_dir, payload)
    ref = payload.get("script_ref") if isinstance(payload.get("script_ref"), dict) else {}
    artifact_id, revision = ref.get("artifact_id"), ref.get("revision")
    if not isinstance(artifact_id, str) or type(revision) is not int or revision < 1:
        raise ValueError("remix alignment script reference is incomplete")
    store = RemixArtifactStore(project_dir, ctx.project_id)
    script = store.read(artifact_id, revision)
    if script["schema"] != "remix_script.v1":
        raise ValueError("remix alignment requires a remix script")
    source = {**script["payload"], "revision": revision}
    timeline = align_script(media_path, source)
    artifact = store.create("word_timeline.v1", timeline, [{"artifact_id": artifact_id, "revision": revision}])
    _publish_operation_result(payload, ctx, {"artifact_id": artifact["id"], "revision": artifact["revision"]})
    return {"status": "completed", "artifact_ref": {"artifact_id": artifact["id"], "revision": artifact["revision"]}}


def run_remix_compile_semantic_timeline(envelope: dict[str, Any], ctx: ProjectContext) -> dict[str, Any]:
    """Compile exact script and completed alignment revisions into timing evidence."""
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
    project_dir = Path(ctx.output_dir).resolve()
    store = RemixArtifactStore(project_dir, ctx.project_id)
    _verified_project_media_path(project_dir, payload)
    def read_ref(name: str, schema: str) -> dict[str, Any]:
        ref = payload.get(name) if isinstance(payload.get(name), dict) else {}
        artifact_id, revision = ref.get("artifact_id"), ref.get("revision")
        if not isinstance(artifact_id, str) or type(revision) is not int or revision < 1:
            raise ValueError(f"{name} is incomplete")
        artifact = store.read(artifact_id, revision)
        if artifact["schema"] != schema:
            raise ValueError(f"{name} must reference {schema}")
        return artifact
    script_artifact = read_ref("script_ref", "remix_script.v1")
    words_artifact = read_ref("word_timeline_ref", "word_timeline.v1")
    blueprint_artifact = read_ref("blueprint_ref", "reference_blueprint.v1")
    words = {**words_artifact["payload"], "artifact_revision": words_artifact["revision"]}
    if words.get("status") != "completed":
        raise ValueError("semantic timing requires completed alignment")
    linked = any(
        ref.get("artifact_id") == script_artifact["id"]
        and ref.get("revision") == script_artifact["revision"]
        for ref in words_artifact["upstream_refs"]
    )
    if not linked or words.get("script_revision") != script_artifact["revision"]:
        raise ValueError("word timeline does not belong to the exact script revision")
    narration_hash = "sha256:" + str(payload["media_sha256"])
    if words.get("media_hash") != narration_hash:
        raise ValueError("word timeline does not belong to the exact narration media")
    script = {
        **script_artifact["payload"],
        "revision": script_artifact["revision"],
        "media_hash": words["media_hash"],
    }
    timing = compile_timing(script, words, payload.get("edit_map", []), payload.get("overrides", []), payload.get("fps"))
    artifact = store.create(
        "performance_timing.v1",
        timing,
        [
            {"artifact_id": script_artifact["id"], "revision": script_artifact["revision"]},
            {"artifact_id": words_artifact["id"], "revision": words_artifact["revision"]},
            {"artifact_id": blueprint_artifact["id"], "revision": blueprint_artifact["revision"]},
        ],
    )
    _publish_operation_result(payload, ctx, {"artifact_id": artifact["id"], "revision": artifact["revision"]})
    status = "needs_review" if timing["unresolved_required_events"] else "completed"
    return {"status": status, "artifact_ref": {"artifact_id": artifact["id"], "revision": artifact["revision"]}, "unresolved_required_events": timing["unresolved_required_events"]}


def _project_media_paths(project_dir: Path, manifest: dict[str, Any]) -> dict[str, Path]:
    """Resolve only relative paths saved in the immutable manifest."""
    root = project_dir.resolve()
    resolved: dict[str, Path] = {}
    for binding in manifest["media_bindings"]:
        media_id = binding["media_id"]
        relative = binding.get("project_path")
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise ValueError(f"media binding {media_id} lacks a project-local path")
        candidate = (root / relative).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            raise FileNotFoundError(f"bound media is unavailable: {media_id}")
        resolved[media_id] = candidate
    return resolved


def run_remix_render(envelope: dict[str, Any], ctx: ProjectContext) -> dict[str, Any]:
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
    ref = payload.get("manifest_ref") if isinstance(payload.get("manifest_ref"), dict) else {}
    artifact_id, revision = ref.get("artifact_id"), ref.get("revision")
    job_id = str(payload.get("job_id") or "")
    if not isinstance(artifact_id, str) or type(revision) is not int or revision < 1:
        raise ValueError("remix render manifest reference is incomplete")
    if not _JOB_ID.fullmatch(job_id):
        raise ValueError("invalid remix render job id")
    project_dir = Path(ctx.output_dir).resolve()
    artifact = RemixArtifactStore(project_dir, ctx.project_id).read(artifact_id, revision)
    if artifact["schema"] != "composition_manifest.v1":
        raise ValueError("remix render requires a composition manifest")
    manifest = artifact["payload"]
    validate_manifest(manifest)
    media_paths = _project_media_paths(project_dir, manifest)
    output = project_dir / "freezone" / "remix_renders" / artifact_id / str(revision) / f"{job_id}.mp4"
    render_manifest(manifest, media_paths=media_paths, output_path=output)
    _publish_operation_result(payload, ctx, {"project_path": output.relative_to(project_dir).as_posix()})
    return {
        "job_id": job_id,
        "output_path": output.relative_to(project_dir).as_posix(),
        "manifest_ref": {"artifact_id": artifact_id, "revision": revision},
        "node_revision": payload.get("node_revision"),
    }


register_project_task_runner("remix_render", run_remix_render, requires_home_node=True, lane="ffmpeg")
register_project_task_runner("remix_transcribe_media", run_remix_transcribe_media, requires_home_node=True)
register_project_task_runner("remix_analyze_reference_video", run_remix_analyze_reference_video, requires_home_node=True)
register_project_task_runner("remix_align_script", run_remix_align_script, requires_home_node=True)
register_project_task_runner("remix_compile_semantic_timeline", run_remix_compile_semantic_timeline, requires_home_node=True)
