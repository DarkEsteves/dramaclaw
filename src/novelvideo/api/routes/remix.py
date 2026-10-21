"""Authorized CRUD endpoints for project-local remix artifacts."""
from __future__ import annotations

from pathlib import Path
from contextlib import nullcontext
from typing import Any, Annotated, Awaitable, Callable, Literal
import hashlib
import json
import logging
import os
import subprocess
import uuid
from urllib.parse import quote, unquote, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool

from novelvideo.api.auth import get_api_user
from novelvideo.freezone import canvas_store
from novelvideo.freezone.canvas_lock import CanvasLockBusy
from novelvideo.freezone.remix.store import RemixArtifactConflict, RemixArtifactStore
from novelvideo.freezone.remix.production_store import production_store
from novelvideo.freezone.remix.contracts import reference_blueprint_readiness_errors
from novelvideo.freezone.remix.operations import RemixOperationStore
from novelvideo.freezone.remix.script_draft import generate_script_draft
from novelvideo.freezone.remix.production_plan import compile_production_plan
from novelvideo.freezone.remix.production_plan_v2 import (
    compile_failure_message, compile_production_plan_v2, migrate_production_plan_v1,
    production_speech_readiness, project_production_plan_v2,
)
from novelvideo.freezone.remix.production_commands import (
    ProductionCommandConflict, apply_production_command, preflight_production_command,
)
from novelvideo.freezone.remix.broll_plan import generate_broll_plan
from novelvideo.freezone.remix.broll_plan import validated_broll_events
from novelvideo.freezone.recipe_runtime import get_recipe_for_runtime
from novelvideo.llm_instrumentation import production_plan_diagnostic_context
from novelvideo.project_context import ProjectContext, require_project_home_node, resolve_project_context
from novelvideo.ports import get_preview_render_capacity, get_task_backend, get_usage_meter
from novelvideo.ports.preview_capacity import PreviewBusy, PreviewUnavailable
from novelvideo.task_identity import project_task_state_key
from novelvideo.task_state import get_task_manager


router = APIRouter(prefix="/projects/{project}/freezone/remix", tags=["freezone-remix"])
logger = logging.getLogger(__name__)
MAX_RENDER_INPUT_FINGERPRINT_CHARS = 5 * 1024 * 1024


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
    input_fingerprint: str = Field(default="", max_length=MAX_RENDER_INPUT_FINGERPRINT_CHARS)
    regenerate: bool = False


class ConfirmOperationBody(BaseModel):
    canvas_id: str = Field(min_length=1, max_length=64)
    node_id: str = Field(min_length=1, max_length=128)


class OperationTransitionBody(BaseModel):
    outcome: Literal["unknown", "failed", "cancelled"]
    provider_task_id: str = ""
    reason: str = Field(default="", max_length=500)


class ProcessActionBody(BaseModel):
    action: Literal[
        "transcribe_media", "align_script", "align_production_speech", "compile_semantic_timeline", "compile_take_timeline",
        "analyze_material_speech", "adopt_source_observation", "analyze_reference_video", "render_manifest", "plan_broll", "generate_script_draft",
        "generate_production_plan_draft", "generate_production_plan_arrangement",
        "compile_production_plan",
    ]
    payload: dict[str, Any]
    idempotency_key: str = Field(min_length=1, max_length=256)
    node_revision: int = Field(ge=1, strict=True)


class ScriptDraftBody(BaseModel):
    blueprint_ref: dict[str, Any]
    new_topic: str = Field(min_length=1, max_length=2_000)


class LockedPlanTurnBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    speaker: str = Field(max_length=80)
    text: str = Field(min_length=1, max_length=2_000)

    @field_validator("text")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("locked turn text must not be blank")
        return value


class ProductionPlanDraftBody(BaseModel):
    topic: str = Field(min_length=1, max_length=2_000)
    blueprint_ref: dict[str, Any] | None = None
    analysis_context: dict[str, Any] | None = None
    locked_turns: list[LockedPlanTurnBody] | None = Field(default=None, min_length=1, max_length=80)


class CompileProductionPlanBody(BaseModel):
    plan_ref: dict[str, Any]


class PreviewProductionFrameBody(CompileProductionPlanBody):
    frame: int = Field(ge=0, strict=True)


class BrollPlanBody(BaseModel):
    script_ref: dict[str, Any]


_PROCESS_TASK_TYPES = {
    "analyze_material_speech": "remix_analyze_material_speech",
    "adopt_source_observation": "remix_adopt_source_speech",
    "transcribe_media": "remix_transcribe_media",
    "align_script": "remix_align_script",
    "align_production_speech": "remix_align_production_speech",
    "compile_semantic_timeline": "remix_compile_semantic_timeline",
    "compile_take_timeline": "remix_compile_take_timeline",
    "analyze_reference_video": "remix_analyze_reference_video",
    "joint_reference_analysis": "remix_joint_reference_analysis",
    "render_manifest": "remix_render",
    "plan_broll": "remix_plan_broll",
    "generate_script_draft": "remix_generate_script_draft",
    "generate_production_plan_draft": "remix_generate_production_plan_draft",
    "generate_production_plan_arrangement": "remix_generate_production_plan_arrangement",
    "compile_production_plan": "remix_compile_production_plan",
}
_PREVIEW_CACHE_FRAMES_PER_PROJECT = 40


def _prune_preview_frames(cache_dir: Path) -> None:
    """Keep click-to-review frames bounded within this one project's cache."""
    frames = []
    for path in cache_dir.glob("*.png"):
        try:
            if path.is_file() and not path.is_symlink():
                frames.append((path.stat().st_mtime, path))
        except OSError:
            continue
    frames.sort(reverse=True)
    for _, path in frames[_PREVIEW_CACHE_FRAMES_PER_PROJECT:]:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("preview cache pruning failed", exc_info=True)


def _validate_preview_budget(plan: dict, frame: int) -> None:
    """Reject implausible FFmpeg jobs before hashing any project media."""
    try:
        output = plan["output"]
        width, height = output["width"], output["height"]
        fps_num, fps_den = output["fps_num"], output["fps_den"]
        beats = plan["beats"]
        resources, placements = plan["resources"], plan["placements"]
        too_large = (
            any(type(value) is not int or value <= 0 for value in
                (width, height, fps_num, fps_den))
            or width > 4096 or height > 4096 or width * height > 8_847_360
            or fps_num > 60 * fps_den
            or len(beats) > 1000 or len(resources) > 256 or len(placements) > 2000
            or sum(beat["duration_frames"] for beat in beats) > 432_000
            or frame >= 432_000
        )
    except (KeyError, TypeError, OverflowError):
        too_large = True
    if too_large:
        raise HTTPException(422, "Production plan exceeds the preview resource budget")


def _transcription_engine_ready() -> bool:
    from novelvideo.config import get_newapi_runtime_credentials

    api_key, base_url = get_newapi_runtime_credentials()
    return bool(api_key and base_url)


async def _transcription_engine_ready_for_context(context) -> bool:
    """Inspect the credential for this admitted organization, never platform state."""
    if context is None or not context.is_organization:
        return await run_in_threadpool(_transcription_engine_ready)
    from novelvideo.ports import get_model_credentials
    from novelvideo.ports.authz import AdmissionContext
    from novelvideo.ports.model_credentials import ModelCredentialError
    from novelvideo.model_gateway_runtime import _validate_organization_credential

    if (context.credential.source != "organization"
            or context.credential.org_id != context.billing_principal.id):
        return False
    admission = AdmissionContext(
        requester_user_id=context.requester_user_id,
        billing_principal=context.billing_principal,
        credential=context.credential,
        admission_id=context.admission_id,
        root_task_id=context.root_task_id,
        admitted_at=context.admitted_at,
        membership_id=context.membership_id,
        authz_version=context.authz_version,
    )
    try:
        credential = await get_model_credentials().resolve(admission)
        _validate_organization_credential(credential, context)
    except ModelCredentialError:
        return False
    return True


async def _request_transcription_context(project: str, user: dict, role: str):
    from novelvideo.api.egress_binding import build_request_egress_context

    project_context = await resolve_project_context(user=user, project_id=project, required_role=role)
    return await build_request_egress_context(
        requester_user_id=project_context.requester_user_id,
        project_id=project_context.project_id,
        task_type="remix_transcribe_media",
    )


def _input_media_hashes(payload: dict[str, Any]) -> list[str]:
    """Snapshot the exact immutable media inputs used by an operation."""
    hashes: set[str] = set()
    def visit(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                visit(child, key)
        elif isinstance(value, str) and key in {"media_hash", "media_sha256"}:
            normalized = value.strip().lower()
            if not normalized.startswith("sha256:"):
                normalized = f"sha256:{normalized}"
            if len(normalized) == 71:
                hashes.add(normalized)
    visit(payload)
    return sorted(hashes)


async def _store_with_context(
    project: str, user: dict, role: str,
) -> tuple[RemixArtifactStore, ProjectContext]:
    ctx = await resolve_project_context(user=user, project_id=project, required_role=role)
    require_project_home_node(ctx, operation="access remix artifact files")
    store = production_store(ctx)
    return store, ctx


async def _store(project: str, user: dict, role: str) -> RemixArtifactStore:
    store, _ = await _store_with_context(project, user, role)
    return store


async def _run_billed_remix_plan(
    *,
    context: ProjectContext,
    task_type: str,
    endpoint: str,
    operation: Callable[[], Awaitable[Any]],
) -> Any:
    """Charge one fixed plan fee around a synchronous model delivery."""
    from novelvideo.api.egress_binding import request_egress_scope

    user_id = context.requester_user_id
    project_id = context.project_id
    operation_id = f"remix-sync-{uuid.uuid4().hex}"
    metadata = {"source": "sync_api", "endpoint": endpoint, "operation_id": operation_id}
    async with request_egress_scope(
        requester_user_id=user_id, project_id=project_id, task_type=task_type,
    ):
        meter = get_usage_meter()
        reservation = await meter.reserve_feature_start_credits(
            user_id=user_id,
            feature_key="freezone.remix_plan",
            product_surface="freezone",
            project_id=project_id,
            resource_kind="script",
            task_id=operation_id,
            task_type=task_type,
            metadata=metadata,
            quantity=1,
            require_price_rule=True,
            require_positive_cost=True,
        )
        reservation_id = str(reservation.get("id") or "")
        billing_metadata: dict[str, Any] = {
            "model_call_credit_policy": "feature_included",
            "feature_key": "freezone.remix_plan",
            "source": "sync_api",
        }
        if reservation_id:
            billing_metadata.update({
                "feature_credit_reservation_id": reservation_id,
                "feature_credit_charge_id": reservation_id,
                "feature_credit_cost": str(reservation.get("cost") or 0),
            })
        started = False
        try:
            meter.set_llm_usage_context(
                user_id,
                project_id=project_id,
                resource_kind="script",
                billing_metadata=billing_metadata,
            )
            started = True
            diagnostic_scope = (
                production_plan_diagnostic_context(task_id=operation_id, project_id=project_id)
                if task_type == "remix_generate_production_plan_draft" else nullcontext()
            )
            with diagnostic_scope:
                result = await operation()
        except BaseException as exc:
            if reservation_id:
                try:
                    if started:
                        await meter.mark_feature_credit_settlement_for_review(
                            reservation_id,
                            metadata={**metadata, "business_outcome": "unknown", "error_type": type(exc).__name__},
                        )
                    else:
                        await meter.settle_cancelled_feature_credit_reservation(
                            reservation_id,
                            metadata={**metadata, "business_outcome": "not_started", "error_type": type(exc).__name__},
                        )
                except Exception:
                    logger.exception("Could not record Remix plan billing settlement after failure")
            raise
        finally:
            meter.clear_llm_usage_context()
        if reservation_id:
            try:
                await meter.settle_feature_credit_reservation(
                    reservation_id, action="confirm", metadata=metadata,
                )
            except Exception:
                logger.exception("Remix plan delivered but billing confirmation remains pending")
        return result


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


def _validate_v2_canvas_bindings(
    canvas: dict[str, Any] | None,
    manifest: dict[str, Any],
    manifest_ref: dict[str, Any],
    plan: dict[str, Any],
    compose_node_id: str,
    project_id: str,
) -> None:
    """Reject a saved v2 render when its current canvas no longer binds the exact media."""
    if not isinstance(canvas, dict):
        raise ValueError("Canvas must be saved before rendering this production plan")
    nodes = {item.get("id"): item for item in canvas.get("nodes", []) if isinstance(item, dict)}
    edges = {(item.get("source"), item.get("target")) for item in canvas.get("edges", []) if isinstance(item, dict)}
    compose = nodes.get(compose_node_id)
    compose_data = compose.get("data", {}) if isinstance(compose, dict) else {}
    plan_id = compose_data.get("productionPlanNodeId") if isinstance(compose_data, dict) else None
    versions = manifest.get("source_versions")
    plan_ref = versions[0] if isinstance(versions, list) and versions else None
    if not isinstance(plan_ref, dict):
        raise ValueError("Manifest has no exact production plan revision")
    if not isinstance(plan_id, str) or not plan_id or (plan_id, compose_node_id) not in edges:
        raise ValueError("Production plan is not directly connected to this compose node")
    if compose_data.get("productionPlanV2Ref") != {"artifactId": plan_ref.get("artifact_id"), "revision": plan_ref.get("revision")}:
        raise ValueError("Compose node no longer references the rendered plan revision")
    if compose_data.get("remixManifestRef") != {"artifactId": manifest_ref["artifact_id"], "revision": manifest_ref["revision"]}:
        raise ValueError("Compose node no longer references this manifest revision")
    plan_node = nodes.get(plan_id)
    plan_data = plan_node.get("data", {}) if isinstance(plan_node, dict) else {}
    if not isinstance(plan_data, dict) or plan_data.get("productionPlanV2Ref") != compose_data.get("productionPlanV2Ref"):
        raise ValueError("Canvas plan revision changed after compilation")
    resources = {item["id"]: item for item in plan.get("resources", [])}
    # Speech takes own their original audio even after their visual placement is
    # removed, so they remain resource consumers for the canvas binding gate.
    used = {item.get("resource_id") for item in [*plan.get("placements", []), *plan.get("speech_takes", [])]
            if item.get("resource_id")}
    for resource_id in used:
        resource = resources[resource_id]
        node_id = resource["node_id"]
        source = nodes.get(node_id)
        source_data = source.get("data", {}) if isinstance(source, dict) else {}
        media = source_data.get("remixMedia") if isinstance(source_data, dict) else None
        if (node_id, plan_id) not in edges or not isinstance(media, dict) or source_data.get("workflowResultStale") is True or source_data.get("remixVisualOutputCurrent") is False:
            raise ValueError(f"Canvas media {node_id} is no longer connected or current")
        for field in ("media_hash", "media_path", "media_kind", "duration_us", "has_audio"):
            if media.get(field) != resource[field]:
                raise ValueError(f"Canvas media {node_id} changed after binding")
        url_field = {"image": "imageUrl", "video": "videoUrl", "audio": "audioUrl"}[resource["media_kind"]]
        source_url = source_data.get(url_field)
        if url_field == "imageUrl" and not source_url:
            source_url = source_data.get("previewImageUrl")
        if not isinstance(source_url, str):
            raise ValueError(f"Canvas media {node_id} has no current source")
        path = urlsplit(source_url).path
        prefixes = (f"/api/v1/projects/{quote(project_id, safe='')}/media/",
                    f"/static/projects/{quote(project_id, safe='')}/")
        relative = next((unquote(path[len(prefix):]) for prefix in prefixes if path.startswith(prefix)), None)
        if relative != resource["media_path"]:
            raise ValueError(f"Canvas media {node_id} source changed after binding")


def _data(value: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "data": value}


@router.get("/transcription-readiness")
async def read_transcription_readiness(project: str, user: dict = Depends(get_api_user)):
    context = await _request_transcription_context(project, user, "viewer")
    return _data({"ready": await _transcription_engine_ready_for_context(context), "engine": "newapi", "model": "audio-transcribe"})


@router.post("/script-drafts")
async def create_script_draft(project: str, body: ScriptDraftBody, user: dict = Depends(get_api_user)):
    store, context = await _store_with_context(project, user, "editor")
    topic = body.new_topic.strip()
    if not topic:
        raise HTTPException(422, "new_topic must not be blank")
    ref = body.blueprint_ref
    if set(ref) != {"artifact_id", "revision"} or not isinstance(ref.get("artifact_id"), str) or type(ref.get("revision")) is not int:
        raise HTTPException(422, "blueprint_ref must be an exact artifact reference")
    artifact = await _call(store.read, ref["artifact_id"], ref["revision"])
    if artifact["schema"] != "reference_blueprint.v1":
        raise HTTPException(422, "blueprint_ref must reference a saved blueprint")
    readiness_errors = reference_blueprint_readiness_errors(
        artifact["payload"], artifact.get("upstream_refs", [])
    )
    if readiness_errors:
        raise HTTPException(
            422, "blueprint is not script-ready: " + "; ".join(readiness_errors)
        )
    try:
        get_recipe_for_runtime(
            username=str(user.get("username") or ""), recipe_id="reference-remix-script",
        )
        draft = await _run_billed_remix_plan(
            context=context, task_type="remix_generate_script_draft", endpoint="script-drafts",
            operation=lambda: generate_script_draft(
                blueprint=artifact["payload"], topic=topic, username=str(user.get("username") or "")
            ),
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _data(draft)


@router.post("/production-plan-drafts")
async def create_production_plan_draft(
    project: str, body: ProductionPlanDraftBody, user: dict = Depends(get_api_user)
):
    await _store(project, user, "editor")
    raise HTTPException(422, {
        "code": "production_plan_canvas_action_required",
        "message": "请在作品方案节点分别提交结构与台词、画面与声音任务。",
        "action": "generate_production_plan_draft",
    })


@router.post("/production-plans/project")
async def project_saved_production_plan(project: str, body: CompileProductionPlanBody, user: dict = Depends(get_api_user)):
    """Read exact author data as editable tracks, including incomplete media."""
    store = await _store(project, user, "viewer")
    ref = body.plan_ref
    if not isinstance(ref, dict) or set(ref) != {"artifact_id", "revision"} or not isinstance(ref.get("artifact_id"), str) or type(ref.get("revision")) is not int:
        raise HTTPException(422, "plan_ref must be an exact artifact reference")
    artifact = await _call(store.read, ref["artifact_id"], ref["revision"])
    if artifact["schema"] != "production_plan.v2":
        raise HTTPException(422, "plan_ref must reference a saved production_plan.v2")
    projection = await _call(project_production_plan_v2, artifact["payload"], project_dir=store.project_dir)
    return _data({"plan_ref": ref, **projection})


@router.post("/production-plans/commands")
async def apply_saved_production_command(project: str, body: dict[str, Any], user: dict = Depends(get_api_user)):
    """Apply one bounded production edit against an exact saved revision."""
    store = await _store(project, user, "editor")
    try:
        return _data(await run_in_threadpool(apply_production_command, store, body))
    except ProductionCommandConflict as exc:
        raise HTTPException(409, {"message": str(exc), "current_revision": exc.current_revision}) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, "Artifact or revision not found") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (CanvasLockBusy, OSError) as exc:
        raise HTTPException(503, "Artifact storage temporarily unavailable; retry after reloading") from exc


@router.post("/production-plans/commands/preflight")
async def preflight_saved_production_command(
    project: str, body: dict[str, Any], user: dict = Depends(get_api_user),
):
    """Check a saved plan command before presenting the existing canvas approval."""
    store = await _store(project, user, "editor")
    try:
        return _data(await run_in_threadpool(preflight_production_command, store, body))
    except ProductionCommandConflict as exc:
        raise HTTPException(409, {"message": str(exc), "current_revision": exc.current_revision}) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, "Artifact or revision not found") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (CanvasLockBusy, OSError) as exc:
        raise HTTPException(503, "Artifact storage temporarily unavailable; reload before checking") from exc


def _production_placement_summary(placement: dict[str, Any]) -> dict[str, Any]:
    """Expose saved creative content without expanding layers or media bindings."""
    description = placement.get("description", "")
    summary = {key: placement.get(key) for key in
               ("id", "kind", "role", "resource_id", "track_id", "turn_id", "start", "end")}
    summary.update(description=description[:512], description_truncated=len(description) > 512)
    graphic = placement.get("graphic")
    if graphic:
        preview = {"template": graphic["template"]}
        for key in ("text", "emphasis"):
            value = graphic.get(key)
            if value is not None:
                preview[key] = value[:512]
                preview[f"{key}_truncated"] = len(value) > 512
        summary["graphic"] = preview
    return summary


@router.get("/production-plans/{artifact_id}/state")
async def read_saved_production_state(
    project: str, artifact_id: str, revision: int | None = Query(default=None, ge=1),
    entity_id: str | None = Query(default=None, min_length=1, max_length=128),
    include_words: bool = False,
    user: dict = Depends(get_api_user),
):
    """Return compact state for an agent without sending the whole author document."""
    store = await _store(project, user, "viewer")
    artifact = await _call(store.read, artifact_id, revision) if revision is not None else await _call(store.read_latest, artifact_id)
    if artifact["schema"] != "production_plan.v2":
        raise HTTPException(422, "production state requires production_plan.v2")
    plan = artifact["payload"]
    if include_words and entity_id is None:
        raise HTTPException(422, "include_words requires entity_id")
    entity = None
    if entity_id is not None:
        groups = [("beat", plan["beats"]),
                  ("turn", [turn for beat in plan["beats"] for turn in beat.get("turns", [])]),
                  ("resource", plan.get("resources", [])), ("placement", plan.get("placements", [])),
                  ("selection", plan.get("selections", [])), ("take", plan.get("speech_takes", [])),
                  ("cue", plan.get("speech_cues", []))]
        matches = [(kind, item) for kind, items in groups for item in items if item["id"] == entity_id]
        if not matches:
            raise HTTPException(404, "Production entity not found")
        if len(matches) > 1:
            raise HTTPException(409, "Entity ID is shared by multiple entity kinds")
        kind, item = matches[0]
        detail = dict(item)
        if kind == "turn":
            detail["word_count"] = len(detail.pop("words", []))
            if detail.get("speech_timing"):
                timing = dict(detail["speech_timing"])
                words = timing.pop("words", [])
                timing["word_count"] = len(words)
                if include_words:
                    timing["words"] = words[:500]
                    timing["words_truncated"] = len(words) > 500
                detail["speech_timing"] = timing
        elif kind == "beat":
            detail["turns"] = [{"id": turn["id"], "speaker": turn.get("speaker", ""), "text": turn["text"]}
                               for turn in detail.get("turns", [])]
        entity = {"kind": kind, "data": detail}
    return _data({
        "plan_ref": {"artifact_id": artifact_id, "revision": artifact["revision"]},
        "goal": plan["goal"], "output": plan["output"],
        "beats": [{"id": beat["id"], "purpose": beat["purpose"], "turn_count": len(beat.get("turns", [])),
                   "duration_frames": beat["duration_frames"], "timing_mode": beat["timing_mode"]}
                  for beat in plan["beats"]],
        "turns": [{"id": turn["id"], "beat_id": beat["id"], "speaker": turn.get("speaker", ""),
                   "text": turn["text"], "timing_status": turn.get("timing_basis", "estimated")}
                  for beat in plan["beats"] for turn in beat.get("turns", [])],
        "placements": [_production_placement_summary(placement) for placement in plan.get("placements", [])],
        "resources": [{"id": resource["id"], "node_id": resource["node_id"],
                       "media_kind": resource["media_kind"], "media_hash": resource["media_hash"]}
                      for resource in plan.get("resources", [])],
        "speech_takes": [{"id": take["id"], "beat_id": take["beat_id"], "resource_id": take["resource_id"],
                          "turn_ids": take["turn_ids"]} for take in plan.get("speech_takes", [])],
        "speech_cues": [{"id": cue["id"], "turn_id": cue["turn_id"], "take_id": cue["take_id"],
                         "timing_status": cue["timing_status"]} for cue in plan.get("speech_cues", [])],
        "resource_count": len(plan.get("resources", [])),
        "placement_count": len(plan.get("placements", [])),
        # Speech readiness comes from the same projection export uses, so a plan
        # that reads "pending" here cannot compile and vice versa.
        **production_speech_readiness(plan),
        **({"entity": entity} if entity is not None else {}),
    })


@router.post("/production-plans/compile")
async def compile_saved_production_plan(project: str, body: CompileProductionPlanBody, user: dict = Depends(get_api_user)):
    store = await _store(project, user, "editor")
    ref = body.plan_ref
    if not isinstance(ref, dict) or set(ref) != {"artifact_id", "revision"} or not isinstance(ref.get("artifact_id"), str) or type(ref.get("revision")) is not int:
        raise HTTPException(422, "plan_ref must be an exact artifact reference")
    artifact = await _call(store.read, ref["artifact_id"], ref["revision"])
    if artifact["schema"] not in {"production_plan.v1", "production_plan.v2"}:
        raise HTTPException(422, "plan_ref must reference a saved production plan")
    compiler = compile_production_plan_v2 if artifact["schema"] == "production_plan.v2" else compile_production_plan
    try:
        manifest = await _call(compiler, artifact["payload"])
    except HTTPException as exc:
        # _call already turns ValueError into a 422; translate only stable codes
        # so the author sees an actionable sentence instead of an internal key.
        if exc.status_code != 422 or not isinstance(exc.detail, str):
            raise
        raise HTTPException(422, compile_failure_message(ValueError(exc.detail))) from exc
    manifest["source_versions"] = [ref]
    return _data(manifest)


@router.post("/production-plans/preview-frame")
async def preview_saved_production_frame(
    project: str, body: PreviewProductionFrameBody, user: dict = Depends(get_api_user)
):
    """Render one exact saved frame after checking every bound project media hash."""
    store = await _store(project, user, "viewer")
    ref = body.plan_ref
    if (not isinstance(ref, dict) or set(ref) != {"artifact_id", "revision"}
            or not isinstance(ref.get("artifact_id"), str) or type(ref.get("revision")) is not int):
        raise HTTPException(422, "plan_ref must be an exact artifact reference")
    artifact = await _call(store.read, ref["artifact_id"], ref["revision"])
    if artifact["schema"] != "production_plan.v2":
        raise HTTPException(422, "plan_ref must reference a saved production_plan.v2")
    _validate_preview_budget(artifact["payload"], body.frame)
    temporary = None
    try:
        capacity = get_preview_render_capacity()
        async with capacity.slot() as lease:
            projection = await _call(
                project_production_plan_v2, artifact["payload"], project_dir=store.project_dir
            )
            if not projection["ready"]:
                raise HTTPException(409, "Bound media or plan timing has changed; check the production plan")
            try:
                manifest = await _call(compile_production_plan_v2, artifact["payload"])
            except HTTPException as exc:
                if exc.status_code != 422 or not isinstance(exc.detail, str):
                    raise
                raise HTTPException(422, compile_failure_message(ValueError(exc.detail))) from exc
            manifest["source_versions"] = [ref]
            active_layers = [layer for layer in manifest["layers"]
                             if layer["start_frame"] <= body.frame < layer["end_frame"]]
            if (manifest["output_spec"]["duration_frames"] > 432_000
                    or len(active_layers) > 8):
                raise HTTPException(422, "Production plan exceeds the preview resource budget")
            if body.frame >= manifest["output_spec"]["duration_frames"]:
                raise HTTPException(422, "preview frame is outside the manifest duration")
            media_paths = {}
            for binding in manifest["media_bindings"]:
                path = (store.project_dir / binding["project_path"]).resolve()
                if not path.is_relative_to(store.project_dir):
                    raise HTTPException(422, "Media path escapes the authorized project")
                media_paths[binding["media_id"]] = path
            cache_dir = store.project_dir / "freezone" / ".preview_frames"
            if cache_dir.is_symlink() or not cache_dir.resolve().is_relative_to(store.project_dir):
                raise HTTPException(503, "Preview cache is unavailable")
            cache_key = hashlib.sha256(("production-preview-v1\0" + str(body.frame) + "\0" +
                json.dumps(manifest, sort_keys=True, separators=(",", ":"))).encode()).hexdigest()
            cached = cache_dir / f"{cache_key}.png"
            if cached.is_symlink():
                raise HTTPException(503, "Preview cache is unavailable")
            try:
                image = await run_in_threadpool(cached.read_bytes)
            except FileNotFoundError:
                temporary = cache_dir / f".{cache_key}.{uuid.uuid4().hex}.png"
                cache_dir.mkdir(parents=True, exist_ok=True)
                await capacity.render_frame(
                    lease, manifest, media_paths=media_paths,
                    frame=body.frame, output_path=temporary,
                )
                await lease.ensure_current()
                image = await run_in_threadpool(temporary.read_bytes)
                os.replace(temporary, cached)
                # Cache maintenance must never turn a successful render into 503.
                try:
                    await run_in_threadpool(_prune_preview_frames, cache_dir)
                except OSError:
                    logger.warning("preview cache pruning failed", exc_info=True)
            return Response(image, media_type="image/png", headers={"Cache-Control": "private, no-store"})
    except PreviewBusy as exc:
        raise HTTPException(429, "Preview renderer is busy; try again shortly") from exc
    except PreviewUnavailable as exc:
        raise HTTPException(503, "Preview renderer is temporarily unavailable") from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        raise HTTPException(503, "Preview frame could not be rendered") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                logger.warning("preview temporary cleanup failed", exc_info=True)


@router.post("/production-plans/migrate")
async def migrate_saved_production_plan(project: str, body: CompileProductionPlanBody, user: dict = Depends(get_api_user)):
    """Publish an explicit v2 copy while retaining the exact v1 source revision."""
    store = await _store(project, user, "editor")
    ref = body.plan_ref
    if not isinstance(ref, dict) or set(ref) != {"artifact_id", "revision"} or not isinstance(ref.get("artifact_id"), str) or type(ref.get("revision")) is not int:
        raise HTTPException(422, "plan_ref must be an exact artifact reference")
    artifact = await _call(store.read, ref["artifact_id"], ref["revision"])
    if artifact["schema"] != "production_plan.v1":
        raise HTTPException(422, "plan_ref must reference a saved v1 production plan")
    try:
        migrated = migrate_production_plan_v1(artifact["payload"])
        migrated["provenance"]["source_plan_ref"] = ref
        return _data(await _call(store.create, "production_plan.v2", migrated, [ref]))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/broll-plans")
async def create_broll_plan(project: str, body: BrollPlanBody, user: dict = Depends(get_api_user)):
    store, context = await _store_with_context(project, user, "editor")
    ref = body.script_ref
    if set(ref) != {"artifact_id", "revision"} or not isinstance(ref.get("artifact_id"), str) or type(ref.get("revision")) is not int:
        raise HTTPException(422, "script_ref must be an exact artifact reference")
    artifact = await _call(store.read, ref["artifact_id"], ref["revision"])
    if artifact["schema"] != "remix_script.v1":
        raise HTTPException(422, "script_ref must reference a saved remix script")

    async def generate_and_publish():
        payload = await generate_broll_plan(
            script=artifact["payload"], username=str(user.get("username") or "")
        )
        return await _call(store.create, "remix_script.v1", payload, [ref])

    try:
        validated_broll_events(artifact["payload"])
        get_recipe_for_runtime(
            username=str(user.get("username") or ""), recipe_id="reference-broll-plan",
        )
        published = await _run_billed_remix_plan(
            context=context, task_type="remix_plan_broll", endpoint="broll-plans",
            operation=generate_and_publish,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _data(published)


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


async def record_enqueue_exception(
    operations: RemixOperationStore,
    operation_id: str,
    *,
    owner: str,
    fencing_token: int,
    error: Exception,
) -> None:
    """Release only definite pre-accept rejections; preserve ambiguous submissions."""
    from novelvideo.task_backend.envelope import InvalidTaskEnvelope

    if isinstance(error, InvalidTaskEnvelope):
        await run_in_threadpool(
            operations.reject_submission, operation_id, owner=owner,
            fencing_token=fencing_token, reason="TASK_ENVELOPE_INVALID",
        )
        return
    if isinstance(error, ValueError):
        await run_in_threadpool(
            operations.release_submission,
            operation_id,
            owner=owner,
            fencing_token=fencing_token,
        )
        return
    await run_in_threadpool(
        operations.submission_outcome_unknown,
        operation_id,
        owner=owner,
        fencing_token=fencing_token,
        reason=f"enqueue outcome unknown: {type(error).__name__}",
    )


def _action_inputs(store: RemixArtifactStore, action: str, payload: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    expected = {
        "transcribe_media": ({"media_path", "language"}, set()),
        "align_script": ({"media_path", "script_ref"}, set()),
        "align_production_speech": ({"media_path", "plan_ref", "turn_id"}, {"placement_id", "take_id"}),
        "compile_semantic_timeline": (
            {"media_path", "script_ref", "word_timeline_ref", "blueprint_ref", "edit_map", "overrides", "fps"},
            set(),
        ),
        "compile_take_timeline": ({"script_ref", "take_bindings", "fps"}, set()),
        "analyze_reference_video": ({"media_path"}, set()),
        "render_manifest": ({"manifest_ref"}, set()),
        "plan_broll": ({"script_ref"}, set()),
        "generate_script_draft": ({"blueprint_ref", "new_topic"}, set()),
        "generate_production_plan_draft": ({"topic"}, {"blueprint_ref", "analysis_context", "locked_turns", "output_preferences", "requirements", "requirements_fingerprint", "requirements_provenance"}),
        "generate_production_plan_arrangement": ({"plan_ref"}, set()),
        "compile_production_plan": ({"plan_ref"}, set()),
    }
    required, optional = expected[action]
    missing = sorted(required - set(payload))
    unexpected = sorted(set(payload) - (required | optional))
    if missing or unexpected:
        # Name the offending keys: "requires exactly <required>" alone cannot
        # tell a caller whether it forgot a field or smuggled an extra one
        # (e.g. a command routing key like ``node_id`` leaked into parameters).
        problems = []
        if missing:
            problems.append("missing " + ", ".join(missing))
        if unexpected:
            problems.append("unexpected " + ", ".join(unexpected))
        raise ValueError(
            f"{action} requires exactly {', '.join(sorted(required))} "
            f"({'; '.join(problems)})"
        )
    refs: list[dict[str, Any]] = []
    media_hash = None
    manifest_media_bindings: list[dict[str, Any]] | None = None
    take_media_bindings: list[dict[str, str]] | None = None
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
    if action == "generate_script_draft" and (
        not isinstance(payload["new_topic"], str) or not payload["new_topic"].strip()
        or len(payload["new_topic"]) > 2_000
    ):
        raise ValueError("new_topic must be a non-blank string of at most 2000 characters")
    if action == "generate_production_plan_draft":
        topic = payload["topic"]
        if not isinstance(topic, str) or not topic.strip() or len(topic) > 2_000:
            raise ValueError("topic must be a non-blank string of at most 2000 characters")
        if "blueprint_ref" in payload and "analysis_context" in payload:
            raise ValueError("choose either blueprint_ref or analysis_context")
        context = payload.get("analysis_context")
        if "analysis_context" in payload and (not isinstance(context, dict) or len(json.dumps(context, ensure_ascii=False)) > 65_536):
            raise ValueError("analysis_context must be an object of at most 65536 characters")
        if "output_preferences" in payload:
            preferences = payload["output_preferences"]
            allowed = {"aspect_ratio", "video_aspect_ratio", "source_clip_duration_seconds"}
            if not isinstance(preferences, dict) or set(preferences) - allowed:
                raise ValueError("output_preferences contains unsupported fields")
            for field in ("aspect_ratio", "video_aspect_ratio"):
                value = preferences.get(field)
                if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 32):
                    raise ValueError(f"output_preferences.{field} must be a short ratio")
            duration = preferences.get("source_clip_duration_seconds")
            if duration is not None and (type(duration) not in (int, float) or not 0 < duration <= 60):
                raise ValueError("output_preferences.source_clip_duration_seconds must be between 0 and 60")
        if "locked_turns" in payload:
            try:
                payload["locked_turns"] = [turn.model_dump() for turn in ProductionPlanDraftBody.model_validate({"topic": topic, "locked_turns": payload["locked_turns"]}).locked_turns or []]
            except Exception as exc:
                raise ValueError("locked_turns must contain 1 to 80 valid speaker/text pairs") from exc
    if action == 'generate_production_plan_draft':
        from novelvideo.freezone.remix.production_requirements import validate_native_requirements
        validate_native_requirements(store, payload)
    schema_by_ref = {
        "script_ref": "remix_script.v1",
        "word_timeline_ref": "word_timeline.v1",
        "manifest_ref": ("composition_manifest.v1", "composition_manifest.v2"),
        "blueprint_ref": "reference_blueprint.v1",
        "plan_ref": "production_plan.v2",
    }
    for key, schema in schema_by_ref.items():
        if key not in payload:
            continue
        ref = payload[key]
        if not isinstance(ref, dict) or set(ref) != {"artifact_id", "revision"}:
            raise ValueError(f"{key} must be an exact artifact reference")
        artifact = store.read(ref["artifact_id"], ref["revision"])
        allowed_schemas = schema if isinstance(schema, tuple) else (schema,)
        if artifact["schema"] not in allowed_schemas:
            raise ValueError(f"{key} must reference {schema}")
        if key == "manifest_ref":
            if action == "render_manifest" and artifact["payload"].get("manifest_version") == 4:
                raise ValueError("v2 production manifests must use the canvas-bound render endpoint")
            bindings = artifact["payload"].get("media_bindings")
            if not isinstance(bindings, list) or not all(isinstance(item, dict) for item in bindings):
                raise ValueError("composition manifest media bindings are invalid")
            manifest_media_bindings = [dict(item) for item in bindings]
        refs.append(ref)
    if action == "generate_production_plan_arrangement":
        from novelvideo.freezone.remix.production_task_inputs import read_arrangement_plan
        read_arrangement_plan(store, payload["plan_ref"])
    if action == "align_production_speech":
        from novelvideo.freezone.remix.production_plan_v2 import production_speech_source
        plan_ref = payload["plan_ref"]
        plan = store.read(plan_ref["artifact_id"], plan_ref["revision"])
        if not payload.get("placement_id") and not payload.get("take_id"):
            raise ValueError("speech alignment requires placement_id or take_id")
        turn, placement, resource = production_speech_source(plan["payload"], payload["turn_id"], payload.get("placement_id"), payload.get("take_id"))
        if resource.media_path != payload["media_path"] or resource.media_hash != media_hash:
            raise ValueError("speech source differs from the saved plan resource")
    if action == "compile_semantic_timeline":
        if not isinstance(payload["edit_map"], list) or not isinstance(payload["overrides"], list) or type(payload["fps"]) is not int or payload["fps"] <= 0:
            raise ValueError("compile action requires edit_map, overrides, and positive fps")
    if action == "compile_take_timeline":
        bindings = payload["take_bindings"]
        if type(payload["fps"]) is not int or payload["fps"] <= 0 or not isinstance(bindings, list) or not bindings:
            raise ValueError("take timeline requires bindings and positive fps")
        script = store.read(payload["script_ref"]["artifact_id"], payload["script_ref"]["revision"])["payload"]
        segments = script.get("segments", [])
        if not segments or any(not isinstance(segment, dict) or not segment.get("segment_id") for segment in segments):
            raise ValueError("take timeline requires performance segments in the saved script")
        if len(bindings) != len(segments):
            raise ValueError("take timeline requires one binding per script segment")
        hashed: list[dict[str, str]] = []
        for segment, binding in zip(segments, bindings, strict=True):
            if not isinstance(binding, dict) or set(binding) != {"segment_id", "media_path"} or binding["segment_id"] != segment["segment_id"]:
                raise ValueError("take binding must match its script segment")
            relative = binding["media_path"]
            if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
                raise ValueError("take media_path must be project relative")
            path = (store.project_dir / relative).resolve()
            if not path.is_relative_to(store.project_dir) or not path.is_file():
                raise ValueError("take media_path must name a project file")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1_048_576), b""):
                    digest.update(chunk)
            hashed.append({**binding, "media_sha256": digest.hexdigest()})
        take_media_bindings = hashed
    parameters = {"action": action, "payload": payload}
    if take_media_bindings is not None:
        parameters["take_media_bindings"] = take_media_bindings
    if media_hash is not None:
        parameters["media_sha256"] = media_hash
    if manifest_media_bindings is not None:
        parameters["manifest_media_bindings"] = manifest_media_bindings
    return refs, parameters


def prepare_registered_action(
    store: RemixArtifactStore,
    action: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str,
    node_revision: int,
) -> dict[str, Any]:
    """Validate one registered deterministic action and build its task envelope."""
    if action not in _PROCESS_TASK_TYPES:
        raise ValueError(f"unsupported registered processing action: {action}")
    from copy import deepcopy
    payload = deepcopy(payload)
    refs, parameters = _action_inputs(store, action, payload)
    task_type = _PROCESS_TASK_TYPES[action]
    if action == "generate_production_plan_draft":
        from novelvideo.freezone.remix.production_task_inputs import production_plan_task_payload

        task_payload = production_plan_task_payload(payload)
    else:
        task_payload = dict(payload)
    if "media_sha256" in parameters:
        task_payload["media_sha256"] = parameters["media_sha256"]
    if "take_media_bindings" in parameters:
        task_payload["take_bindings"] = parameters["take_media_bindings"]
    if action == "render_manifest":
        ref = payload["manifest_ref"]
        job_id = hashlib.sha256(
            f"{store.project_id}\0{ref['artifact_id']}\0{ref['revision']}\0{idempotency_key}".encode()
        ).hexdigest()
        task_payload["job_id"] = job_id
        task_payload["project_dir"] = str(store.project_dir)
        scope = job_id
        queue_kind = "ffmpeg"
        producer_version = "remix-render.v1"
        parameters = {
            "job_id": job_id,
            "action": action,
            "payload": payload,
            "manifest_media_bindings": parameters["manifest_media_bindings"],
        }
    else:
        scope = ""
        queue_kind = "default"
        producer_version = "remix-process.v1"
    task_payload["node_revision"] = node_revision
    return {
        "input_refs": refs,
        "normalized_parameters": parameters,
        "task_type": task_type,
        "task_payload": task_payload,
        "scope": scope,
        "queue_kind": queue_kind,
        "producer_version": producer_version,
        "input_media_hashes": _input_media_hashes(parameters),
    }


@router.post("/actions")
async def enqueue_process_action(project: str, body: ProcessActionBody, user: dict = Depends(get_api_user)):
    store = await _store(project, user, "editor")
    if body.action == "generate_production_plan_arrangement":
        raise HTTPException(422, {
            "code": "production_plan_canvas_action_required",
            "message": "请通过作品方案节点提交画面与声音任务，沿用画布动作确认流程。",
        })
    if body.action in {"transcribe_media", "align_script", "align_production_speech", "compile_take_timeline"}:
        context = await _request_transcription_context(project, user, "editor")
        if not await _transcription_engine_ready_for_context(context):
            raise HTTPException(503, "NewAPI audio-transcribe is not configured")
    try:
        if body.action == "adopt_source_observation":
            required = {"canvas_id", "plan_ref", "take_id", "observation_ref", "work_ref"}
            if not required <= set(body.payload) or set(body.payload) - required - {"correction_ref"}:
                raise ValueError("adopt_source_observation requires only current plan/take/work/source references")
            from .reference_director import _context
            from novelvideo.freezone.remix.source_speech_adoption import prepare_source_observation_adoption
            from novelvideo.freezone.remix.production_advancement import enqueue_source_observation_adoption
            ctx, canvas, store = await _context(project, body.payload["canvas_id"], user)
            prepared = await run_in_threadpool(prepare_source_observation_adoption, store, ctx, canvas, **body.payload)
            result = await enqueue_source_observation_adoption(ctx, store, prepared,
                command_id=body.idempotency_key, node_revision=body.payload["plan_ref"]["revision"])
            return _data({"action": body.action, **result})
        elif body.action == "analyze_material_speech":
            if set(body.payload) != {"canvas_id", "work_ref", "step_id", "node_id", "language"}:
                raise ValueError("analyze_material_speech requires only canvas/work/step/current-node references and language")
            from .reference_director import _context
            from novelvideo.api.egress_binding import request_egress_scope
            from novelvideo.freezone.remix.source_speech import prepare_material_speech
            ctx, canvas, store = await _context(project, body.payload["canvas_id"], user)
            async with request_egress_scope(
                requester_user_id=ctx.requester_user_id, project_id=ctx.project_id,
                task_type="remix_analyze_material_speech",
            ):
                prepared = await run_in_threadpool(prepare_material_speech, store, ctx, canvas, **body.payload)
            prepared["task_payload"] = {**prepared["task_payload"], "node_revision": body.node_revision}
        else:
            prepared = await run_in_threadpool(
                prepare_registered_action, store, body.action, body.payload,
                idempotency_key=body.idempotency_key, node_revision=body.node_revision,
            )
        refs, parameters = prepared["input_refs"], prepared["normalized_parameters"]
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
            producer_version=prepared["producer_version"],
            normalized_parameters=parameters,
            node_revision=body.node_revision,
            input_media_hashes=prepared["input_media_hashes"],
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    task_type = prepared["task_type"]
    scope = prepared["scope"] or operation["operation_id"]
    task_key = project_task_state_key(task_type, ctx.project_id, 0, scope=scope)
    if operation["task_id"] or operation["status"] in {"needs_confirmation", "pending_reconciliation", "cancelled", "completed", "stale", "failed"}:
        return _data({"action": body.action, "task_type": task_type, "task_id": operation["task_id"], "task_key": task_key, "operation_id": operation["operation_id"], "operation_status": operation["status"], "output_refs": operation["output_refs"]})
    owner = uuid.uuid4().hex
    fencing_token = await run_in_threadpool(operations.claim_submission, operation["operation_id"], owner=owner)
    if fencing_token is None:
        current = await run_in_threadpool(operations.get, operation["operation_id"])
        return _data({"action": body.action, "task_type": task_type, "task_id": current["task_id"], "task_key": task_key, "operation_id": current["operation_id"], "operation_status": current["status"], "output_refs": current["output_refs"]})
    try:
        queued = await get_task_backend().enqueue_project_task(
            ctx, product_surface="freezone", task_type=task_type, queue_kind=prepared["queue_kind"],
            episode=0, scope=scope, payload={**prepared["task_payload"], "operation_id": operation["operation_id"]},
        )
    except Exception as exc:
        await record_enqueue_exception(
            operations, operation["operation_id"], owner=owner,
            fencing_token=fencing_token, error=exc,
        )
        from novelvideo.task_backend.envelope import InvalidTaskEnvelope

        if isinstance(exc, InvalidTaskEnvelope):
            raise HTTPException(
                500, detail={"code": "TASK_ENVELOPE_INVALID", "operation_id": operation["operation_id"]},
            ) from exc
        raise
    await run_in_threadpool(
        operations.bind_task, operation["operation_id"], task_id=queued.task_state.task_id,
        owner=owner, fencing_token=fencing_token,
    )
    return _data({"action": body.action, "task_type": task_type, "task_id": queued.task_state.task_id, "task_key": task_key, "operation_id": operation["operation_id"], "operation_status": "pending"})


@router.post("/artifacts")
async def create_artifact(project: str, body: CreateArtifactBody, user: dict = Depends(get_api_user)):
    if body.schema_name in {"source_speech_observation.v1", "candidate_speech_correction.v1", "reference_commission.v1", "reference_analysis.v1", "production_review.v1", "material_work_package.v1", "material_review.v1", "reference_decision.v1", "production_output_review.v1", "speech_correction_preview.v1", "speech_correction_review.v1"}:
        raise HTTPException(422, "Use the authenticated director operation for this evidence")
    store = await _store(project, user, "editor")
    return _data(await _call(store.create, body.schema_name, body.payload, body.upstream_refs))


async def _authorize_source_speech_artifact(project: str, artifact: dict, user: dict):
    producers = {'source_speech_observation.v1': 'remix-source-speech.v1',
                 'candidate_speech_correction.v1': 'remix-candidate-speech-correction.v1'}
    expected_producer = producers.get(artifact['schema'])
    if artifact['schema'] == 'word_timeline.v1' and artifact['producer_version'] == 'remix-adopt-source-speech.v1':
        expected_producer = 'remix-adopt-source-speech.v1'
    if expected_producer is None:
        return
    from .reference_director import _context
    from novelvideo.freezone.remix.source_speech import candidate_scope
    scope = artifact["payload"]["authorization_scope"]
    ctx, canvas, _ = await _context(project, scope.get("canvas_id", ""), user, "viewer")
    if artifact["producer_version"] != expected_producer or candidate_scope(ctx, canvas, scope["canvas_id"], require_write=False) != scope:
        raise HTTPException(403, "source observation authorization differs")


@router.get("/artifacts/{artifact_id}")
async def read_artifact(
    project: str,
    artifact_id: str,
    revision: Annotated[int, Query(ge=1)],
    user: dict = Depends(get_api_user),
):
    store = await _store(project, user, "viewer")
    artifact = await _call(store.read, artifact_id, revision)
    await _authorize_source_speech_artifact(project, artifact, user)
    return _data(artifact)


@router.get("/artifacts/{artifact_id}/latest")
async def read_latest_artifact(project: str, artifact_id: str, user: dict = Depends(get_api_user)):
    """Read the newest authorized revision for conflict comparison."""
    store = await _store(project, user, "viewer")
    artifact = await _call(store.read_latest, artifact_id)
    await _authorize_source_speech_artifact(project, artifact, user)
    return _data(artifact)


@router.put("/artifacts/{artifact_id}")
async def revise_artifact(
    project: str,
    artifact_id: str,
    body: ReviseArtifactBody,
    user: dict = Depends(get_api_user),
):
    store = await _store(project, user, "editor")
    current = await _call(store.read_latest, artifact_id)
    if current["schema"] == "word_timeline.v1" and current["producer_version"] == "remix-adopt-source-speech.v1":
        await _authorize_source_speech_artifact(project, current, user)
        raise HTTPException(422, "Use the authenticated source adoption operation for this evidence")
    if current["schema"] in {"source_speech_observation.v1", "candidate_speech_correction.v1", "reference_commission.v1", "reference_analysis.v1", "production_review.v1", "material_work_package.v1", "material_review.v1", "reference_decision.v1", "production_output_review.v1", "speech_correction_preview.v1", "speech_correction_review.v1"}:
        raise HTTPException(422, "Use the authenticated director operation for this evidence")
    saved = await _call(
        store.revise,
        artifact_id,
        body.expected_revision,
        body.payload,
        body.upstream_refs,
    )
    if saved["schema"] == "production_plan.v2":
        from novelvideo.chat.reference_production_observer import wake_local_observers
        wake_local_observers()
    return _data(saved)


@router.post("/artifacts/{artifact_id}/render")
async def render_artifact(
    project: str,
    artifact_id: str,
    body: RenderArtifactBody,
    user: dict = Depends(get_api_user),
):
    store = await _store(project, user, "editor")
    artifact = await _call(store.read, artifact_id, body.revision)
    if artifact["schema"] not in {"composition_manifest.v1", "composition_manifest.v2"}:
        raise HTTPException(422, "Artifact is not a composition manifest")
    try:
        from novelvideo.freezone.remix.render import validate_manifest

        validate_manifest(artifact["payload"])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    job_seed = f"{store.project_id}\0{artifact_id}\0{body.revision}\0{body.idempotency_key}"
    if body.regenerate:
        job_seed += f"\0{uuid.uuid4().hex}"
    job_id = hashlib.sha256(job_seed.encode()).hexdigest()
    ctx = await resolve_project_context(user=user, project_id=project, required_role="editor")
    operations = RemixOperationStore(store.project_dir, store.project_id)
    if bool(body.canvas_id) != bool(body.input_fingerprint):
        raise HTTPException(422, "Bound render requires the exact input fingerprint")
    if artifact["payload"].get("manifest_version") == 4:
        if not body.canvas_id or not body.node_id:
            raise HTTPException(422, "Production plan render requires a saved canvas and compose node")
        source_versions = artifact["payload"].get("source_versions")
        source_ref = source_versions[0] if isinstance(source_versions, list) and source_versions else None
        if not isinstance(source_ref, dict):
            raise HTTPException(422, "Production plan source revision is missing")
        source = await _call(store.read, source_ref.get("artifact_id"), source_ref.get("revision"))
        if source["schema"] != "production_plan.v2":
            raise HTTPException(422, "Production plan source revision is invalid")
        canvas = await _call(canvas_store.read_canvas, Path(ctx.state_dir), body.canvas_id)
        try:
            _validate_v2_canvas_bindings(canvas, artifact["payload"],
                {"artifact_id": artifact_id, "revision": body.revision}, source["payload"], body.node_id, store.project_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    manifest_media_bindings = [dict(item) for item in artifact["payload"]["media_bindings"]]
    try:
        operation = await run_in_threadpool(
            operations.start,
            idempotency_key=body.idempotency_key,
            input_refs=[{"artifact_id": artifact_id, "revision": body.revision}],
            producer_version="remix-render.v1",
            normalized_parameters={
                "job_id": job_id,
                "input_fingerprint": body.input_fingerprint,
                "manifest_media_bindings": manifest_media_bindings,
            },
            node_revision=body.node_revision,
            input_media_hashes=_input_media_hashes(artifact["payload"]),
            canvas_id=body.canvas_id,
            node_id=body.node_id,
            regenerate=body.regenerate,
        )
    except ValueError as exc:
        if "Idempotency key" in str(exc):
            raise HTTPException(409, str(exc)) from exc
        raise HTTPException(422, str(exc)) from exc
    if operation["task_id"] or operation["status"] in {"needs_confirmation", "pending_reconciliation", "cancelled", "completed", "stale"}:
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
    owner = uuid.uuid4().hex
    fencing_token = await run_in_threadpool(operations.claim_submission, operation["operation_id"], owner=owner)
    if fencing_token is None:
        current = await run_in_threadpool(operations.get, operation["operation_id"])
        return _data({"task_type": "remix_render", "job_id": job_id, "task_id": current["task_id"], "task_key": project_task_state_key("remix_render", ctx.project_id, 0, scope=job_id), "manifest_ref": {"artifact_id": artifact_id, "revision": body.revision}, "node_revision": body.node_revision, "operation_status": current["status"], "operation_id": current["operation_id"], "output_refs": current["output_refs"]})
    try:
        queued = await get_task_backend().enqueue_project_task(
            ctx, product_surface="freezone", task_type="remix_render", queue_kind="ffmpeg", episode=0, scope=job_id,
            payload={
            "job_id": job_id,
            "operation_id": operation["operation_id"],
            "project_dir": str(store.project_dir),
            "manifest_ref": {"artifact_id": artifact_id, "revision": body.revision},
            "idempotency_key": body.idempotency_key,
            "node_revision": body.node_revision,
            },
        )
    except Exception as exc:
        await record_enqueue_exception(
            operations, operation["operation_id"], owner=owner,
            fencing_token=fencing_token, error=exc,
        )
        raise
    await run_in_threadpool(
        operations.bind_task, operation["operation_id"], task_id=queued.task_state.task_id,
        owner=owner, fencing_token=fencing_token,
    )
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


async def _authorize_material_speech_operation(project: str, record: dict, user: dict, role: str = "viewer"):
    from novelvideo.freezone.remix.source_speech import PRODUCER, candidate_scope
    if record.get("producer_version") not in {PRODUCER, "remix-adopt-source-speech.v1"}:
        return
    from .reference_director import _context
    payload = record["normalized_parameters"]["payload"]
    ctx, canvas, _ = await _context(project, payload["canvas_id"], user, role)
    if candidate_scope(ctx, canvas, payload["canvas_id"], require_write=role == "editor") != payload["scope"]:
        raise HTTPException(403, "candidate operation authorization differs")


@router.get("/operations")
async def list_operations(
    project: str,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    user: dict = Depends(get_api_user),
):
    store = await _store(project, user, "viewer")
    operations = RemixOperationStore(store.project_dir, store.project_id)
    records = await _call(operations.list, limit=limit)
    permitted = []
    for record in records:
        try:
            await _authorize_material_speech_operation(project, record, user)
        except HTTPException as exc:
            if exc.status_code in {403, 404}:
                continue
            raise
        permitted.append(_operation_view(record))
    return _data({"items": permitted})


@router.get("/operations/{operation_id}")
async def read_operation(project: str, operation_id: str, user: dict = Depends(get_api_user)):
    store = await _store(project, user, "viewer")
    record = await _call(RemixOperationStore(store.project_dir, store.project_id).get, operation_id)
    await _authorize_material_speech_operation(project, record, user)
    return _data(_operation_view(record))


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


@router.post("/operations/{operation_id}/transition")
async def transition_operation(project: str, operation_id: str, body: OperationTransitionBody, user: dict = Depends(get_api_user)):
    store = await _store(project, user, "editor")
    operations = RemixOperationStore(store.project_dir, store.project_id)
    await _authorize_material_speech_operation(project, await _call(operations.get, operation_id), user, "editor")
    try:
        if body.outcome == "unknown":
            result = await _call(operations.provider_outcome_unknown, operation_id, provider_task_id=body.provider_task_id)
        elif body.outcome == "cancelled":
            reason = body.reason or "user_cancelled"
            cancellation_owner = uuid.uuid4().hex
            record = await _call(
                operations.claim_cancellation, operation_id,
                reason=reason, owner=cancellation_owner,
            )
            if record["status"] == "cancelled":
                return _data(_operation_view(record))
            if record["status"] == "pending_reconciliation" and not record.get("task_id"):
                raise HTTPException(409, "Submission outcome is unknown; reconcile before cancellation")
            if record.get("task_id"):
                ctx = await resolve_project_context(user=user, project_id=project, required_role="editor")
                view = _operation_view(record)
                task = await run_in_threadpool(
                    get_task_manager().get_task_for_project, ctx, view["task_type"], 0,
                    beat_num=None, scope=view["task_scope"],
                )
                if task is None or task.task_id != record["task_id"]:
                    await _call(operations.cancellation_outcome_unknown, operation_id, reason="bound task unavailable")
                    raise HTTPException(409, "Task outcome is unknown; reconcile before cancellation")
                try:
                    cancelled = await get_task_backend().cancel_project_task(
                        ctx, task, force=True, acknowledge_no_refund=True,
                    )
                except Exception:
                    await _call(operations.cancellation_outcome_unknown, operation_id, reason="task cancellation request failed")
                    raise
                if not cancelled.get("ok") or cancelled.get("requires_confirmation"):
                    await _call(operations.cancellation_outcome_unknown, operation_id, reason="task cancellation was not confirmed")
                    raise HTTPException(409, "Task cancellation was not confirmed")
            result = await _call(operations.finish_cancellation, operation_id)
        else:
            result = await _call(operations.fail, operation_id, reason=body.reason or "task_failed")
    except FileNotFoundError as exc:
        raise HTTPException(404, "Operation not found") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _data(_operation_view(result))


@router.post("/operations/{operation_id}/reconcile")
async def reconcile_operation(project: str, operation_id: str, user: dict = Depends(get_api_user)):
    """Refresh an uncertain operation from its durable project task without resubmitting."""
    store = await _store(project, user, "editor")
    operations = RemixOperationStore(store.project_dir, store.project_id)
    try:
        record = await _call(operations.get, operation_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Operation not found") from exc
    await _authorize_material_speech_operation(project, record, user, "editor")
    if record.get("producer_version") == "remix-source-speech.v1":
        from novelvideo.freezone.remix.source_speech import reconcile_material_speech
        from novelvideo.model_gateway_runtime import model_gateway_request_scope
        ctx = await resolve_project_context(user=user, project_id=project, required_role="editor")
        try:
            context = await _request_transcription_context(project, user, "editor")
            with model_gateway_request_scope(context):
                recovered = await _call(reconcile_material_speech, ctx, record)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return _data(_operation_view(recovered or record))
    if record.get("producer_version") == "remix-adopt-source-speech.v1":
        from novelvideo.freezone.remix.source_speech_adoption import reconcile_source_speech_adoption
        ctx = await resolve_project_context(user=user, project_id=project, required_role="editor")
        try:
            recovered = await _call(reconcile_source_speech_adoption, ctx, record)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return _data(_operation_view(recovered or record))
    if record["status"] == "confirming":
        ctx = await resolve_project_context(user=user, project_id=project, required_role="editor")
        canvas = await _call(canvas_store.read_canvas, Path(ctx.state_dir), record.get("canvas_id", ""))
        node = next((item for item in (canvas or {}).get("nodes", []) if item.get("id") == record.get("node_id")), None)
        data = node.get("data") if isinstance(node, dict) else None
        output_refs = record.get("output_refs") or []
        output_path = output_refs[0].get("project_path") if len(output_refs) == 1 and isinstance(output_refs[0], dict) else None
        expected_url = f"/api/v1/projects/{quote(store.project_id, safe='')}/media/{quote(output_path, safe='/')}" if isinstance(output_path, str) else None
        input_refs = record.get("input_refs") or []
        expected_manifest = ({"artifactId": input_refs[0]["artifact_id"], "revision": input_refs[0]["revision"]}
                             if len(input_refs) == 1 else None)
        applied = (isinstance(data, dict)
                   and data.get("remixRenderOperationId") == operation_id
                   and data.get("resultVideoUrl") == expected_url
                   and data.get("remixRenderManifestRef") == expected_manifest)
        record = await _call(operations.recover_confirmation, operation_id, applied_to_canvas=applied)
        return _data(_operation_view(record))
    if record["status"] not in {"pending_reconciliation", "cancelling"}:
        return _data(_operation_view(record))
    view = _operation_view(record)
    if not view.get("task_type") or not view.get("task_scope"):
        if record["status"] == "cancelling":
            record = await _call(operations.cancellation_outcome_unknown, operation_id, reason="bound task unavailable")
            return _data(_operation_view(record))
        return _data(view)
    ctx = await resolve_project_context(user=user, project_id=project, required_role="editor")
    task = await run_in_threadpool(
        get_task_manager().get_task_for_project, ctx, view["task_type"], 0,
        beat_num=None, scope=view["task_scope"],
    )
    if task is None:
        if record["status"] == "cancelling":
            record = await _call(operations.cancellation_outcome_unknown, operation_id, reason="bound task unavailable")
            return _data(_operation_view(record))
        return _data(view)
    if not record.get("task_id"):
        submission_owner = record.get("submission_owner")
        submission_fence = record.get("submission_fence")
        if not submission_owner or type(submission_fence) is not int:
            return _data(view)
        try:
            record = await _call(
                operations.bind_task, operation_id, task_id=task.task_id,
                owner=submission_owner, fencing_token=submission_fence,
            )
        except ValueError:
            return _data(_operation_view(await _call(operations.get, operation_id)))
        view = _operation_view(record)
    if task.task_id != record["task_id"]:
        return _data(view)
    if record.get("cancel_reason") and record["status"] in {"cancelling", "pending_reconciliation"}:
        if task.status == "cancelled":
            if record["status"] == "pending_reconciliation":
                record = await _call(operations.cancel, operation_id, reason=record["cancel_reason"])
            else:
                record = await _call(operations.finish_cancellation, operation_id)
        elif task.status == "failed":
            if record["status"] == "cancelling":
                await _call(operations.cancellation_outcome_unknown, operation_id, reason=task.error or "task_failed")
            record = await _call(operations.fail, operation_id, reason=task.error or "task_failed")
        elif task.status == "completed":
            if record["status"] == "cancelling":
                record = await _call(operations.cancellation_outcome_unknown, operation_id, reason="task completed while cancellation was pending")
        else:
            owner = uuid.uuid4().hex
            claimed = await _call(operations.claim_cancellation_reconciliation, operation_id, owner=owner)
            if not claimed:
                return _data(_operation_view(await _call(operations.get, operation_id)))
            try:
                cancelled = await get_task_backend().cancel_project_task(ctx, task, force=True, acknowledge_no_refund=True)
            except Exception:
                if record["status"] == "cancelling":
                    record = await _call(operations.cancellation_outcome_unknown, operation_id, reason="task cancellation request failed")
                else:
                    record = await _call(operations.get, operation_id)
            else:
                if cancelled.get("ok") and not cancelled.get("requires_confirmation"):
                    if record["status"] == "cancelling":
                        record = await _call(operations.finish_cancellation, operation_id)
                    else:
                        record = await _call(operations.cancel, operation_id, reason=record["cancel_reason"])
                else:
                    if record["status"] == "cancelling":
                        record = await _call(operations.cancellation_outcome_unknown, operation_id, reason="task cancellation was not confirmed")
                    else:
                        record = await _call(operations.get, operation_id)
            finally:
                await _call(operations.release_cancellation_reconciliation, operation_id, owner=owner)
        return _data(_operation_view(record))
    if task.status == "failed":
        record = await _call(operations.fail, operation_id, reason=task.error or "task_failed")
    elif task.status == "cancelled":
        record = await _call(operations.cancel, operation_id, reason="task_cancelled")
    # Successful runners publish output_refs themselves. Leaving an operation
    # uncertain until that durable publish appears prevents a false success.
    return _data(_operation_view(record))
