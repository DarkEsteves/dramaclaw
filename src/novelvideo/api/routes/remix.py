"""Authorized CRUD endpoints for project-local remix artifacts."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Annotated, Literal
import hashlib
import importlib.util
import os
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from novelvideo.api.auth import get_api_user
from novelvideo.freezone.canvas_lock import CanvasLockBusy
from novelvideo.freezone.remix.store import RemixArtifactConflict, RemixArtifactStore
from novelvideo.freezone.remix.operations import RemixOperationStore
from novelvideo.freezone.remix.alignment import speech_engine_ready
from novelvideo.freezone.remix.script_draft import generate_script_draft
from novelvideo.project_context import require_project_home_node, resolve_project_context
from novelvideo.ports import get_task_backend
from novelvideo.task_identity import project_task_state_key


router = APIRouter(prefix="/projects/{project}/freezone/remix", tags=["freezone-remix"])


class CreateArtifactBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_name: str = Field(alias="schema", min_length=1, max_length=128)
    payload: dict[str, Any]
    upstream_refs: list[dict[str, Any]] = Field(default_factory=list)


class ReviseArtifactBody(BaseModel):
    expected_revision: int = Field(ge=1, strict=True)
    payload: dict[str, Any]
    upstream_refs: list[dict[str, Any]] = Field(default_factory=list)


class RenderArtifactBody(BaseModel):
    revision: int = Field(ge=1, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=256)
    node_revision: int = Field(ge=1, strict=True)
    canvas_id: str = ""
    node_id: str = ""


class ConfirmOperationBody(BaseModel):
    canvas_id: str = Field(min_length=1, max_length=64)
    node_id: str = Field(min_length=1, max_length=128)


class ProcessActionBody(BaseModel):
    action: Literal["transcribe_media", "align_script", "compile_semantic_timeline", "analyze_reference_video"]
    payload: dict[str, Any]
    idempotency_key: str = Field(min_length=1, max_length=256)
    node_revision: int = Field(ge=1, strict=True)


class ScriptDraftBody(BaseModel):
    blueprint_ref: dict[str, Any]
    new_topic: str = Field(min_length=1, max_length=2_000)


_PROCESS_TASK_TYPES = {
    "transcribe_media": "remix_transcribe_media",
    "align_script": "remix_align_script",
    "compile_semantic_timeline": "remix_compile_semantic_timeline",
    "analyze_reference_video": "remix_analyze_reference_video",
}


def _speech_engine_ready() -> bool:
    return speech_engine_ready()


def _speech_engine_readiness() -> dict[str, Any]:
    """Describe local speech configuration without importing or starting WhisperX."""
    engine = os.getenv("DRAMACLAW_REMIX_SPEECH_ENGINE", "").strip()
    cache_value = os.getenv("DRAMACLAW_REMIX_MODEL_CACHE", "").strip()
    python_value = os.getenv("DRAMACLAW_REMIX_WHISPERX_PYTHON", "").strip()
    cache = Path(cache_value) if cache_value else None
    python = Path(python_value) if python_value else None

    if python is not None:
        python_status = (
            "external"
            if python.is_file() and os.access(python, os.X_OK)
            else "missing"
        )
    else:
        python_status = (
            "in-process" if importlib.util.find_spec("whisperx") else "missing"
        )
    cache_status = "available" if cache and cache.is_dir() else "missing"

    error = None
    if engine != "whisperx":
        error = "DRAMACLAW_REMIX_SPEECH_ENGINE must be set to whisperx"
    elif cache_status == "missing":
        error = (
            f"WhisperX model cache directory is missing: {cache}"
            if cache is not None
            else "DRAMACLAW_REMIX_MODEL_CACHE must name an existing local directory"
        )
    elif python_status == "missing":
        error = (
            f"WhisperX Python executable is missing or not executable: {python}"
            if python is not None
            else "WhisperX is not installed in the API Python environment"
        )
    return {
        "ready": error is None,
        "engine": engine or "disabled",
        "python": python_status,
        "model_cache": cache_status,
        "downloads_allowed": False,
        "error": error,
    }


async def _store(project: str, user: dict, role: str) -> RemixArtifactStore:
    ctx = await resolve_project_context(user=user, project_id=project, required_role=role)
    require_project_home_node(ctx, operation="access remix artifact files")
    return RemixArtifactStore(
        Path(ctx.output_dir),
        project_id=ctx.project_id,
        created_by=str(user.get("username") or ""),
    )


async def _call(fn, *args, **kwargs):
    try:
        return await run_in_threadpool(fn, *args, **kwargs)
    except RemixArtifactConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, "Artifact or revision not found") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (CanvasLockBusy, OSError) as exc:
        raise HTTPException(503, "Artifact storage temporarily unavailable; retry after reloading") from exc


def _data(value: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "data": value}


@router.get("/speech-readiness")
async def read_speech_readiness(user: dict = Depends(get_api_user)):
    return _data(_speech_engine_readiness())


@router.post("/script-drafts")
async def create_script_draft(project: str, body: ScriptDraftBody, user: dict = Depends(get_api_user)):
    store = await _store(project, user, "editor")
    topic = body.new_topic.strip()
    if not topic:
        raise HTTPException(422, "new_topic must not be blank")
    ref = body.blueprint_ref
    if set(ref) != {"artifact_id", "revision"} or not isinstance(ref.get("artifact_id"), str) or type(ref.get("revision")) is not int:
        raise HTTPException(422, "blueprint_ref must be an exact artifact reference")
    artifact = await _call(store.read, ref["artifact_id"], ref["revision"])
    if artifact["schema"] != "reference_blueprint.v1":
        raise HTTPException(422, "blueprint_ref must reference a saved blueprint")
    try:
        draft = await generate_script_draft(
            blueprint=artifact["payload"], topic=topic, username=str(user.get("username") or "")
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _data(draft)


def _operation_view(record: dict[str, Any]) -> dict[str, Any]:
    parameters = record.get("normalized_parameters") or {}
    if record.get("producer_version") == "remix-render.v1":
        task_type = "remix_render"
        scope = parameters.get("job_id")
    elif record.get("producer_version") == "remix-process.v1":
        task_type = _PROCESS_TASK_TYPES.get(parameters.get("action"))
        scope = record["operation_id"]
    else:
        task_type = None
        scope = None
    project_id = record["project_id"]
    output_urls = []
    for ref in record.get("output_refs") or []:
        path = ref.get("project_path") if isinstance(ref, dict) else None
        if isinstance(path, str) and path:
            base = f"/api/v1/projects/{quote(project_id, safe='')}"
            encoded = quote(path, safe="/")
            output_urls.append({"media_url": f"{base}/media/{encoded}", "download_url": f"{base}/files/{encoded}"})
    return {
        **record,
        "task_type": task_type,
        "task_scope": scope,
        "task_key": project_task_state_key(task_type, project_id, 0, scope=scope) if task_type and scope else None,
        "output_urls": output_urls,
    }


def _action_inputs(store: RemixArtifactStore, action: str, payload: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    expected = {
        "transcribe_media": ({"media_path", "language"}, set()),
        "align_script": ({"media_path", "script_ref"}, set()),
        "compile_semantic_timeline": ({"script_ref", "word_timeline_ref", "edit_map", "overrides", "fps"}, set()),
        "analyze_reference_video": ({"media_path"}, set()),
    }
    required, optional = expected[action]
    if set(payload) != required | optional:
        raise ValueError(f"{action} requires exactly {', '.join(sorted(required))}")
    refs: list[dict[str, Any]] = []
    media_hash = None
    if "media_path" in payload:
        relative = payload["media_path"]
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise ValueError("media_path must be project relative")
        path = (store.project_dir / relative).resolve()
        if not path.is_relative_to(store.project_dir) or not path.is_file():
            raise ValueError("media_path must name an existing project file")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1_048_576), b""):
                digest.update(chunk)
        media_hash = digest.hexdigest()
    if action == "transcribe_media" and (not isinstance(payload["language"], str) or not payload["language"]):
        raise ValueError("language is required")
    schema_by_ref = {"script_ref": "remix_script.v1", "word_timeline_ref": "word_timeline.v1"}
    for key, schema in schema_by_ref.items():
        if key not in payload:
            continue
        ref = payload[key]
        if not isinstance(ref, dict) or set(ref) != {"artifact_id", "revision"}:
            raise ValueError(f"{key} must be an exact artifact reference")
        artifact = store.read(ref["artifact_id"], ref["revision"])
        if artifact["schema"] != schema:
            raise ValueError(f"{key} must reference {schema}")
        refs.append(ref)
    if action == "compile_semantic_timeline":
        if not isinstance(payload["edit_map"], list) or not isinstance(payload["overrides"], list) or type(payload["fps"]) is not int or payload["fps"] <= 0:
            raise ValueError("compile action requires edit_map, overrides, and positive fps")
    parameters = {"action": action, "payload": payload}
    if media_hash is not None:
        parameters["media_sha256"] = media_hash
    return refs, parameters


@router.post("/actions")
async def enqueue_process_action(project: str, body: ProcessActionBody, user: dict = Depends(get_api_user)):
    store = await _store(project, user, "editor")
    if body.action in {"transcribe_media", "align_script"} and not _speech_engine_ready():
        raise HTTPException(503, "Speech alignment engine is not configured")
    try:
        refs, parameters = await run_in_threadpool(_action_inputs, store, body.action, body.payload)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Artifact or revision not found") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    ctx = await resolve_project_context(user=user, project_id=project, required_role="editor")
    operations = RemixOperationStore(store.project_dir, store.project_id)
    try:
        operation = await run_in_threadpool(
            operations.start,
            idempotency_key=body.idempotency_key,
            input_refs=refs,
            producer_version="remix-process.v1",
            normalized_parameters=parameters,
            node_revision=body.node_revision,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    task_type = _PROCESS_TASK_TYPES[body.action]
    task_key = project_task_state_key(task_type, ctx.project_id, 0, scope=operation["operation_id"])
    if operation["task_id"] or operation["status"] == "needs_confirmation":
        return _data({"action": body.action, "task_type": task_type, "task_id": operation["task_id"], "task_key": task_key, "operation_id": operation["operation_id"], "operation_status": operation["status"], "output_refs": operation["output_refs"]})
    queued = await get_task_backend().enqueue_project_task(
        ctx,
        product_surface="freezone",
        task_type=task_type,
        queue_kind="default",
        episode=0,
        scope=operation["operation_id"],
        payload={**body.payload, **({"media_sha256": parameters["media_sha256"]} if "media_sha256" in parameters else {}), "operation_id": operation["operation_id"], "node_revision": body.node_revision},
    )
    await run_in_threadpool(operations.bind_task, operation["operation_id"], task_id=queued.task_state.task_id)
    return _data({"action": body.action, "task_type": task_type, "task_id": queued.task_state.task_id, "task_key": task_key, "operation_id": operation["operation_id"], "operation_status": "pending"})


@router.post("/artifacts")
async def create_artifact(project: str, body: CreateArtifactBody, user: dict = Depends(get_api_user)):
    store = await _store(project, user, "editor")
    return _data(await _call(store.create, body.schema_name, body.payload, body.upstream_refs))


@router.get("/artifacts/{artifact_id}")
async def read_artifact(
    project: str,
    artifact_id: str,
    revision: Annotated[int, Query(ge=1)],
    user: dict = Depends(get_api_user),
):
    store = await _store(project, user, "viewer")
    return _data(await _call(store.read, artifact_id, revision))


@router.put("/artifacts/{artifact_id}")
async def revise_artifact(
    project: str,
    artifact_id: str,
    body: ReviseArtifactBody,
    user: dict = Depends(get_api_user),
):
    store = await _store(project, user, "editor")
    return _data(await _call(
        store.revise,
        artifact_id,
        body.expected_revision,
        body.payload,
        body.upstream_refs,
    ))


@router.post("/artifacts/{artifact_id}/render")
async def render_artifact(
    project: str,
    artifact_id: str,
    body: RenderArtifactBody,
    user: dict = Depends(get_api_user),
):
    store = await _store(project, user, "editor")
    artifact = await _call(store.read, artifact_id, body.revision)
    if artifact["schema"] != "composition_manifest.v1":
        raise HTTPException(422, "Artifact is not a composition manifest")
    try:
        from novelvideo.freezone.remix.render import validate_manifest

        validate_manifest(artifact["payload"])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    job_id = hashlib.sha256(
        f"{store.project_id}\0{artifact_id}\0{body.revision}\0{body.idempotency_key}".encode()
    ).hexdigest()
    ctx = await resolve_project_context(user=user, project_id=project, required_role="editor")
    operations = RemixOperationStore(store.project_dir, store.project_id)
    try:
        operation = await run_in_threadpool(
            operations.start,
            idempotency_key=body.idempotency_key,
            input_refs=[{"artifact_id": artifact_id, "revision": body.revision}],
            producer_version="remix-render.v1",
            normalized_parameters={"job_id": job_id},
            node_revision=body.node_revision,
            canvas_id=body.canvas_id,
            node_id=body.node_id,
        )
    except ValueError as exc:
        if "Idempotency key" in str(exc):
            raise HTTPException(409, str(exc)) from exc
        raise HTTPException(422, str(exc)) from exc
    if operation["task_id"] or operation["status"] == "needs_confirmation":
        return _data({
            "task_type": "remix_render",
            "job_id": job_id,
            "task_id": operation["task_id"],
            "task_key": project_task_state_key("remix_render", ctx.project_id, 0, scope=job_id),
            "manifest_ref": {"artifact_id": artifact_id, "revision": body.revision},
            "node_revision": body.node_revision,
            "operation_status": operation["status"],
            "operation_id": operation["operation_id"],
            "output_refs": operation["output_refs"],
        })
    queued = await get_task_backend().enqueue_project_task(
        ctx,
        product_surface="freezone",
        task_type="remix_render",
        queue_kind="ffmpeg",
        episode=0,
        scope=job_id,
        payload={
            "job_id": job_id,
            "operation_id": operation["operation_id"],
            "project_dir": str(store.project_dir),
            "manifest_ref": {"artifact_id": artifact_id, "revision": body.revision},
            "idempotency_key": body.idempotency_key,
            "node_revision": body.node_revision,
        },
    )
    await run_in_threadpool(operations.bind_task, operation["operation_id"], task_id=queued.task_state.task_id)
    return _data({
        "task_type": "remix_render",
        "job_id": job_id,
        "task_id": queued.task_state.task_id,
        "task_key": project_task_state_key("remix_render", ctx.project_id, 0, scope=job_id),
        "backend": queued.backend,
        "queue": queued.queue,
        "manifest_ref": {"artifact_id": artifact_id, "revision": body.revision},
        "node_revision": body.node_revision,
        "operation_id": operation["operation_id"],
    })


@router.get("/operations")
async def list_operations(
    project: str,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    user: dict = Depends(get_api_user),
):
    store = await _store(project, user, "viewer")
    operations = RemixOperationStore(store.project_dir, store.project_id)
    records = await _call(operations.list, limit=limit)
    return _data({"items": [_operation_view(record) for record in records]})


@router.get("/operations/{operation_id}")
async def read_operation(project: str, operation_id: str, user: dict = Depends(get_api_user)):
    store = await _store(project, user, "viewer")
    return _data(_operation_view(await _call(RemixOperationStore(store.project_dir, store.project_id).get, operation_id)))


@router.post("/operations/{operation_id}/confirm")
async def confirm_operation(
    project: str,
    operation_id: str,
    body: ConfirmOperationBody,
    user: dict = Depends(get_api_user),
):
    store = await _store(project, user, "editor")
    ctx = await resolve_project_context(user=user, project_id=project, required_role="editor")
    from novelvideo.freezone.remix.results import RemixNodeConflict, confirm_render

    try:
        result = await run_in_threadpool(
            confirm_render, store.project_dir, Path(ctx.state_dir), store.project_id, operation_id,
            canvas_id=body.canvas_id, node_id=body.node_id,
        )
    except RemixNodeConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, "Operation or canvas not found") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (CanvasLockBusy, OSError) as exc:
        raise HTTPException(503, "Remix result temporarily unavailable; retry") from exc
    return _data(_operation_view(result))
