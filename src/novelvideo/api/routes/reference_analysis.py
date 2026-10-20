"""Authenticated submission and recovery of the reference remix analysis stage."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from novelvideo.api.auth import get_api_user
from novelvideo.chat.reference_remix_continuation import automatic_reference_continuation_enabled, coordinator
from novelvideo.freezone import canvas_store
from novelvideo.freezone.paths import CANVAS_ID_RE
from novelvideo.api.reference_operation_logging import ReferenceOperationRoute

router = APIRouter(route_class=ReferenceOperationRoute)


async def _enqueue_reference_transcript(ctx, *, media_path: str, media_hash: str,
                                        idempotency_key: str, on_reserved):
    """Admit the same independent speech action used by a manual transcript node."""
    from novelvideo.api.routes.remix import prepare_registered_action
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.ports import get_task_backend
    from novelvideo.task_state import get_task_manager
    from novelvideo.task_identity import project_task_state_key
    from novelvideo.freezone.remix.reference_analysis_service import source_snapshot

    snapshot = await asyncio.to_thread(source_snapshot, ctx, {'media_path': media_path, 'media_hash': 'sha256:' + media_hash})
    prepared = await asyncio.to_thread(prepare_registered_action, RemixArtifactStore(ctx.output_dir, ctx.project_id),
        'transcribe_media', {'media_path': snapshot['media_path'], 'language': 'auto'},
        idempotency_key=idempotency_key, node_revision=1)
    operations = RemixOperationStore(ctx.output_dir, ctx.project_id)
    operation = await asyncio.to_thread(operations.start, idempotency_key=idempotency_key,
        input_refs=prepared['input_refs'], producer_version=prepared['producer_version'],
        normalized_parameters=prepared['normalized_parameters'], node_revision=1,
        input_media_hashes=prepared['input_media_hashes'])
    await on_reserved(operation['operation_id'])
    task_type = prepared['task_type']
    task_key = project_task_state_key(task_type, ctx.project_id, 0, scope=operation['operation_id'])
    if not operation.get('task_id'):
        # Enqueue may have succeeded just before a worker stopped. Reconcile
        # the scoped task under the original submission fence before retrying.
        task = await asyncio.to_thread(get_task_manager().get_task_for_project, ctx,
            task_type, 0, scope=operation['operation_id'])
        if task is not None:
            if task.project_id != ctx.project_id or task.requester_user_id != ctx.requester_user_id:
                raise ValueError('reference transcript task identity changed')
            operation = await asyncio.to_thread(operations.bind_task, operation['operation_id'],
                task_id=task.task_id, owner=operation['submission_owner'],
                fencing_token=operation['submission_fence'])
    if not operation.get('task_id') and operation['status'] == 'pending':
        from novelvideo.api.routes.remix import record_enqueue_exception
        owner = uuid.uuid4().hex
        fence = await asyncio.to_thread(operations.claim_submission, operation['operation_id'], owner=owner)
        if fence is not None:
            try:
                queued = await get_task_backend().enqueue_project_task(ctx, product_surface='freezone',
                    task_type=task_type, queue_kind='default', episode=0, scope=operation['operation_id'],
                    payload={**prepared['task_payload'], 'operation_id': operation['operation_id']})
            except Exception as exc:
                await record_enqueue_exception(operations, operation['operation_id'], owner=owner,
                    fencing_token=fence, error=exc)
                raise
            await asyncio.to_thread(operations.bind_task, operation['operation_id'],
                task_id=queued.task_state.task_id, owner=owner, fencing_token=fence)
    operation = await asyncio.to_thread(operations.get, operation['operation_id'])
    return {'task_id': operation.get('task_id'), 'task_key': task_key, 'operation_id': operation['operation_id']}


def _node_transcript_ref(node):
    data = node.get('data', {})
    if data.get('operationId') or data.get('autoStartMode') == 'transcribe' or data.get('transcriptSubmissionPending') or data.get('transcriptSubmissionRequest') or data.get('managedOperationId') not in {None, '', 'pending'}:
        raise ValueError('原片转写仍在处理中，请完成后重新点击分析原片')
    if data.get('status') not in {'completed', 'no_speech'}:
        return None
    ref = data.get('artifactRef')
    if not isinstance(ref, dict):
        raise ValueError('saved transcript requires its artifact revision')
    return {'artifact_id': ref.get('artifactId'), 'revision': ref.get('revision'),
            **({'project_id': ref['projectId']} if 'projectId' in ref else {})}


def _connected_transcript(canvas, source_node_id, url, transcript_node_id, analysis_node_id=None):
    """Validate saved graph facts without taking an artifact lease."""
    nodes = {node.get('id'): node for node in canvas.get('nodes', [])}
    links = {(edge.get('source'), edge.get('target')) for edge in canvas.get('edges', [])}
    transcript = nodes.get(transcript_node_id)
    if not transcript or transcript.get('type') != 'transcriptNode' or (source_node_id, transcript_node_id) not in links:
        raise ValueError('reference transcript must be connected to the source video')
    if analysis_node_id is not None:
        dependencies = [node for node in nodes.values() if node.get('type') == 'transcriptNode'
                        and (node.get('id'), analysis_node_id) in links]
        if len(dependencies) != 1:
            raise ValueError('selected analysis requires one unambiguous transcript dependency')
        analysis = nodes.get(analysis_node_id)
        if not analysis or analysis.get('type') != 'videoStoryNode' or (source_node_id, analysis_node_id) not in links or (transcript_node_id, analysis_node_id) not in links:
            raise ValueError('reference transcript must be connected to the selected analysis')
        if analysis.get('data', {}).get('sourceVideoUrl', url) != url:
            raise ValueError('reference analysis source changed')
    return _node_transcript_ref(transcript)


def _native_transcript_node(canvas, source_node_id):
    links = {(edge.get('source'), edge.get('target')) for edge in canvas.get('edges', [])}
    for node in canvas.get('nodes', []):
        if node.get('type') == 'transcriptNode' and (source_node_id, node.get('id')) in links:
            _node_transcript_ref(node)  # Defer standalone recognition before reserving joint work.
    candidates = [node for node in canvas.get('nodes', []) if node.get('type') == 'transcriptNode'
                  and node.get('data', {}).get('status') in {'completed', 'no_speech'}
                  and (source_node_id, node.get('id')) in links]
    if len(candidates) > 1:
        raise ValueError('multiple completed source transcripts require an explicit selection')
    return candidates[0] if candidates else None


class ReferenceAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_node_id: str = Field(min_length=1, max_length=160)
    user_goal: str = Field(min_length=1, max_length=2000)
    idempotency_key: str = Field(
        min_length=1, max_length=160, pattern=r"^[a-zA-Z0-9._:-]+$"
    )
    inputs: dict[str, Any] = Field(default_factory=dict)
    agent_id: str = Field(default="main", pattern=r"^[a-zA-Z0-9_-]{1,80}$")


async def _context(project: str, canvas_id: str, user: dict, *, required_role: str = "editor", allow_unsaved: bool = False):
    from novelvideo.api.routes.freezone import _resolve_freezone_project

    if not CANVAS_ID_RE.fullmatch(canvas_id):
        raise HTTPException(400, "invalid canvas_id")
    from novelvideo.api.routes.chat import _enforce_agent_chat_scope
    from novelvideo.chat.store import ChatScope

    _enforce_agent_chat_scope(
        user,
        ChatScope(kind="project", id=project, surface="freezone", canvas_id=canvas_id),
        require_write=required_role != "viewer",
    )
    ctx, username, name, output, _ = await _resolve_freezone_project(
        project, user, required_role=required_role
    )
    canvas = await asyncio.to_thread(canvas_store.read_canvas, ctx.state_dir, canvas_id)
    if canvas is None and allow_unsaved:
        return ctx, username, name, output, None
    if not canvas or canvas.get("project_id") != ctx.project_id:
        raise HTTPException(404, "canvas not found")
    if (
        canvas.get("access_model") in {"private", "user_private"}
        and canvas.get("owner_principal_id") != ctx.requester_user_id
    ):
        raise HTTPException(403, "private canvas access denied")
    return ctx, username, name, output, canvas


@router.post(
    "/projects/{project}/freezone/canvases/{canvas_id}/reference-analysis-runs"
)
async def start_reference_analysis(
    project: str,
    canvas_id: str,
    body: ReferenceAnalysisRequest,
    user: dict = Depends(get_api_user),
):
    from novelvideo.api.routes import freezone

    ctx, username, name, output, canvas = await _context(project, canvas_id, user)
    if not automatic_reference_continuation_enabled():
        raise HTTPException(503, "automatic reference continuation is disabled")
    source = next(
        (
            node
            for node in canvas.get("nodes", [])
            if node.get("id") == body.source_node_id and node.get("type") == "videoNode"
        ),
        None,
    )
    url = (source or {}).get("data", {}).get("videoUrl")
    if not isinstance(url, str) or not url.strip():
        raise HTTPException(404, "source video not found on this canvas")
    from novelvideo.freezone.canvas_media_scope import scan_foreign_media_refs

    if scan_foreign_media_refs({"nodes": [source]}, project_id=ctx.project_id):
        raise HTTPException(400, "reference media belongs to another project")
    try:
        video_path = freezone.resolve_static_url_to_path(url, output)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not video_path.is_file():
        raise HTTPException(404, "source video media not found")
    from novelvideo.freezone.remix import reference_analysis_service as service
    # An inconclusive probe cannot prove this video is silent. Preserve the
    # speech-first dependency until a real source observation settles it.
    has_audio = await asyncio.to_thread(service.probe_audio_track, video_path) is not False
    try:
        relative = video_path.resolve().relative_to(output.resolve()).as_posix()
        transcript_node = _native_transcript_node(canvas, body.source_node_id)
        transcript_ref = (_connected_transcript(canvas, body.source_node_id, url, transcript_node['id'])
                          if transcript_node else None)
        canvas_transcript_ref = transcript_ref
        if transcript_ref:
            snapshot = await asyncio.to_thread(service.source_snapshot, ctx, {'media_path': relative})
            validator = (service.validated_no_speech_ref if transcript_node['data']['status'] == 'no_speech'
                         else service.validated_transcript_ref)
            transcript_ref = await asyncio.to_thread(validator, ctx, transcript_ref, snapshot['media_sha256'])
        elif has_audio:
            snapshot = await asyncio.to_thread(service.source_snapshot, ctx, {'media_path': relative})
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(409, str(exc)) from exc
    from novelvideo.freezone.agent_workflows import catalog

    with catalog.workflow_catalog_scope(ctx.requester_username):
        package = catalog.get_workflow_skill(
            {"skill_id": "reference-video-remix", "inputs": body.inputs}
        )
    if (
        not package.get("ok")
        or package["input_contract"]["errors"]
        or package["input_contract"]["missing_required"]
    ):
        raise HTTPException(
            400, "reference remix Skill is unavailable or inputs are invalid"
        )
    completed = [
        node
        for node in canvas.get("nodes", [])
        if node.get("type") == "videoStoryNode"
        and any(
            edge.get("source") == body.source_node_id
            and edge.get("target") == node.get("id")
            for edge in canvas.get("edges", [])
        )
        and node.get("data", {}).get("sourceVideoUrl") == url
        and not node.get("data", {}).get("isAnalyzing")
        and not node.get("data", {}).get("analysisError")
        and not node.get("data", {}).get("blueprintDirty")
        and isinstance(node.get("data", {}).get("rawResult"), dict)
        and node["data"]["rawResult"].get("video_story", {}).get("summary")
        and (not transcript_ref or (
            {'artifact_id': (node.get('data', {}).get('referenceTranscriptRef') or {}).get('artifactId'),
             'revision': (node.get('data', {}).get('referenceTranscriptRef') or {}).get('revision')} == transcript_ref
            and any(edge.get('source') == transcript_node['id'] and edge.get('target') == node.get('id')
                    for edge in canvas.get('edges', []))))
    ]
    if transcript_ref:
        # Display fields cannot prove which immutable speech revision a saved
        # analysis consumed. Resolve its real lineage before the canvas lease.
        from novelvideo.freezone.remix.store import RemixArtifactStore
        matching = []
        for node in completed:
            ref = node.get('data', {}).get('referenceAnalysisRef') or {}
            try:
                artifact = await asyncio.to_thread(RemixArtifactStore(output, ctx.project_id).read,
                                                   ref.get('artifactId'), ref.get('revision'))
            except (ValueError, FileNotFoundError):
                continue
            if (artifact['schema'] == 'reference_analysis.v1'
                    and artifact['payload'].get('media_hash') == 'sha256:' + snapshot['media_sha256']
                    and artifact['payload'].get('transcript_ref') == transcript_ref):
                matching.append(node)
        completed = matching
    saved_analysis = completed[0] if len(completed) == 1 else None
    if has_audio and not transcript_ref and not saved_analysis:
        from novelvideo.api.routes.remix import _transcription_engine_ready
        if not await asyncio.to_thread(_transcription_engine_ready):
            raise HTTPException(503, 'NewAPI audio-transcribe is not configured')
    try:
        run = await coordinator.reserve(
            ctx=ctx,
            canvas_id=canvas_id,
            source_node_id=body.source_node_id,
            source_video_url=url,
            analysis_node_id=saved_analysis["id"] if saved_analysis else None,
            goal=body.user_goal,
            inputs=body.inputs,
            idempotency_key=body.idempotency_key,
            agent_id=body.agent_id,
            expected_revision=canvas["revision"],
            automation_authorized=user.get("credential_kind") not in {"agent_session", "local_trusted_agent"},
            **({'transcript_node_id': transcript_node['id'], 'transcript_ref': transcript_ref} if transcript_ref else {}),
            **({'transcript_status': 'no_speech'} if transcript_ref and transcript_node['data']['status'] == 'no_speech' else {}),
            **({'create_transcript': True, 'transcript_media_path': relative,
                'transcript_media_hash': snapshot['media_sha256']} if has_audio and not transcript_ref and not saved_analysis else {}),
        )
        token = await coordinator.claim_submission(ctx=ctx, run_id=run["run_id"])
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if token is None:
        return {"ok": True, "data": run}
    analysis_id = run["analysis_node_id"]
    new_transcript_id = run.get('transcript_node_id') if has_audio and not transcript_ref and not saved_analysis else None
    task_receipt = {}
    try:
        if saved_analysis and analysis_id == saved_analysis["id"]:
            adopted = await coordinator.adopt_saved_analysis(
                ctx=ctx, run_id=run["run_id"], submission_token=token
            )
            return {"ok": True, "data": adopted}

        def build(current):
            if not current or current.get("project_id") != ctx.project_id:
                raise ValueError("canvas changed")
            current_source = next(
                (
                    node
                    for node in current.get("nodes", [])
                    if node.get("id") == body.source_node_id
                ),
                None,
            )
            if (
                not current_source
                or current_source.get("data", {}).get("videoUrl") != url
            ):
                raise ValueError("reference source changed")
            if transcript_node and _connected_transcript(current, body.source_node_id, url, transcript_node['id']) != canvas_transcript_ref:
                raise ValueError('reference transcript revision changed')
            result = deepcopy(current)
            position = current_source.get("position") or {}
            source_x = float(position.get("x", 0))
            source_y = float(position.get("y", 0))
            source_width = current_source.get("width") or (current_source.get("measured") or {}).get("width") or 480
            next_x = source_x + float(source_width) + 120
            if new_transcript_id and not any(node.get('id') == new_transcript_id for node in result.get('nodes', [])):
                result['nodes'].append({'id': new_transcript_id, 'type': 'transcriptNode',
                    'position': {'x': next_x, 'y': source_y},
                    'data': {'displayName': '原片转写', 'sourceMediaPath': relative,
                        'sourceMediaHash': snapshot['media_sha256'], 'sourceVideoUrl': url,
                        'sourceProjectId': ctx.project_id, 'transcriptLanguage': 'auto',
                        'status': 'needs_review', 'unresolved': [], 'words': [], 'autoStartMode': None,
                        'managedOperationId': f"reference-run:{run['run_id']}"}})
                result.setdefault('edges', []).append({'id': f"reference-source-transcript-{run['run_id']}",
                    'source': body.source_node_id, 'target': new_transcript_id,
                    'sourceHandle': 'source', 'targetHandle': 'target', 'type': 'disconnectableEdge'})
            dependency_id = new_transcript_id or (transcript_node['id'] if transcript_node else None)
            if dependency_id:
                dependency = next(node for node in result['nodes'] if node.get('id') == dependency_id)
                dependency_x = float((dependency.get('position') or {}).get('x', 0))
                dependency_width = dependency.get('width') or (dependency.get('measured') or {}).get('width') or 420
                next_x = max(next_x, dependency_x + float(dependency_width) + 120)
            if not any(
                node.get("id") == analysis_id for node in result.get("nodes", [])
            ):
                result["nodes"].append(
                    {
                        "id": analysis_id,
                        "type": "videoStoryNode",
                        "position": {
                            "x": next_x,
                            "y": source_y,
                        },
                        "data": {
                            "displayName": "原片拆解",
                            "sourceVideoUrl": url,
                            "rows": [],
                            "rawResult": None,
                            "referenceBlueprintRef": None,
                            "isAnalyzing": True,
                            "analysisError": None,
                            "remixAnalysisStage": "transcribing" if new_transcript_id else "analyzing",
                            "referenceAnalysisRunId": run["run_id"],
                        },
                    }
                )
                result.setdefault("edges", []).append(
                    {
                        "id": f"reference-{run['run_id']}",
                        "source": body.source_node_id,
                        "target": analysis_id,
                        "sourceHandle": "source",
                        "targetHandle": "target",
                        "type": "disconnectableEdge",
                    }
                )
            if transcript_node and not any(edge.get('source') == transcript_node['id'] and edge.get('target') == analysis_id for edge in result.get('edges', [])):
                result.setdefault('edges', []).append({'id': f"reference-transcript-{run['run_id']}",
                    'source': transcript_node['id'], 'target': analysis_id, 'sourceHandle': 'source',
                    'targetHandle': 'target', 'type': 'disconnectableEdge'})
            if new_transcript_id and not any(edge.get('source') == new_transcript_id and edge.get('target') == analysis_id for edge in result.get('edges', [])):
                result.setdefault('edges', []).append({'id': f"reference-transcript-{run['run_id']}",
                    'source': new_transcript_id, 'target': analysis_id,
                    'sourceHandle': 'source', 'targetHandle': 'target', 'type': 'disconnectableEdge'})
            result["revision"] = current["revision"] + 1
            result["updated_by"] = ctx.requester_user_id
            result["updated_at"] = canvas_store.utc_now_iso()
            return result

        await canvas_store.save_canvas_async(
            ctx.state_dir,
            canvas_id,
            base_revision=canvas["revision"],
            build_payload=build,
            save_source="reference_analysis_submission",
        )
        if new_transcript_id:
            async def bind_transcript(operation_id):
                await coordinator.bind_transcript_operation(ctx=ctx, run_id=run['run_id'],
                    operation_id=operation_id, submission_token=token)
            task_receipt = await _enqueue_reference_transcript(ctx, media_path=relative,
                media_hash=snapshot['media_sha256'], idempotency_key='reference-transcript:' + run['run_id'],
                on_reserved=bind_transcript)
            if not task_receipt.get('task_id'):
                waiting = await coordinator.release_analysis_submission(ctx=ctx, run_id=run['run_id'], submission_token=token)
                return {'ok': True, 'data': waiting}
            return {'ok': True, 'data': await coordinator.attach_task(ctx=ctx, run_id=run['run_id'],
                task_id=task_receipt['task_id'], task_key=task_receipt.get('task_key'), submission_token=token)}
        from novelvideo.freezone.remix.reference_analysis_service import ensure_reference_analysis

        async def bind_operation(operation_id):
            await coordinator.bind_analysis_operation(ctx=ctx, run_id=run["run_id"],
                                                       operation_id=operation_id, submission_token=token)

        task_receipt = await ensure_reference_analysis(
            ctx, {"media_path": relative, **({('no_speech_ref' if transcript_node['data']['status'] == 'no_speech'
                else 'transcript_ref'): transcript_ref} if transcript_ref else {})}, {},
            body.idempotency_key, on_reserved=bind_operation,
        )
        task_id = task_receipt.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            # Another entrance owns admission for this content. The durable
            # operation scope is already saved; recovery waits for its real task.
            waiting = await coordinator.release_analysis_submission(ctx=ctx, run_id=run["run_id"], submission_token=token)
            return {"ok": True, "data": waiting}
        run = await coordinator.attach_task(
            ctx=ctx,
            run_id=run["run_id"],
            task_id=task_id,
            task_key=task_receipt.get("task_key"),
            submission_token=token,
        )
        return {"ok": True, "data": run}
    except (ValueError, canvas_store.CanvasStoreError) as exc:
        await coordinator.fail_submission(
            ctx=ctx,
            run_id=run["run_id"],
            error=str(exc),
            submission_token=token,
            task_id=task_receipt.get("task_id"),
            task_key=task_receipt.get("task_key"),
        )
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        await coordinator.fail_submission(
            ctx=ctx,
            run_id=run["run_id"],
            error=str(exc),
            submission_token=token,
            task_id=task_receipt.get("task_id"),
            task_key=task_receipt.get("task_key"),
        )
        raise


@router.get("/projects/{project}/freezone/canvases/{canvas_id}/reference-analysis-runs")
async def list_reference_analysis(
    project: str,
    canvas_id: str,
    agent_id: str = "main",
    user: dict = Depends(get_api_user),
):
    ctx, _, _, _, canvas = await _context(project, canvas_id, user, allow_unsaved=True)
    if canvas is None:
        # Opening a user canvas does not persist it until the first edit.
        # Its optional progress inventory is empty; submission still requires
        # the actual saved canvas and source through the strict context above.
        return {"ok": True, "data": {"runs": []}}
    if not automatic_reference_continuation_enabled():
        return {"ok": True, "data": {"runs": []}}
    return {
        "ok": True,
        "data": {
            "runs": await coordinator.list(
                ctx=ctx, canvas_id=canvas_id, agent_id=agent_id
            )
        },
    }


@router.get(
    "/projects/{project}/freezone/canvases/{canvas_id}/reference-analysis-runs/{run_id}"
)
async def get_reference_analysis(
    project: str, canvas_id: str, run_id: str, user: dict = Depends(get_api_user)
):
    ctx, *_ = await _context(project, canvas_id, user)
    if not automatic_reference_continuation_enabled():
        raise HTTPException(503, "automatic reference continuation is disabled")
    run = await coordinator.get(ctx=ctx, run_id=run_id)
    if not run or run["canvas_id"] != canvas_id:
        raise HTTPException(404, "reference analysis run not found")
    return {"ok": True, "data": run}


@router.post(
    "/projects/{project}/freezone/canvases/{canvas_id}/reference-analysis-runs/{run_id}/retry"
)
async def retry_reference_analysis(
    project: str, canvas_id: str, run_id: str, user: dict = Depends(get_api_user)
):
    ctx, *_ = await _context(project, canvas_id, user)
    if not automatic_reference_continuation_enabled():
        raise HTTPException(503, "automatic reference continuation is disabled")
    run = await coordinator.get(ctx=ctx, run_id=run_id)
    if not run or run["canvas_id"] != canvas_id:
        raise HTTPException(404, "reference analysis run not found")
    try:
        result = await coordinator.retry(ctx=ctx, run_id=run_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "data": result}


@router.get("/projects/{project}/freezone/canvases/{canvas_id}/reference-analysis-runs/{run_id}/receipt")
async def read_reference_submission_receipt(
    project: str, canvas_id: str, run_id: str,
    source_node_id: str = Query(min_length=1, max_length=160),
    agent_id: str = Query(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$"),
    idempotency_key: str = Query(min_length=1, max_length=160),
    user: dict = Depends(get_api_user),
):
    ctx, *_ = await _context(project, canvas_id, user)
    if not automatic_reference_continuation_enabled():
        raise HTTPException(503, "automatic reference continuation is disabled")
    try:
        run = await coordinator.read_submission_receipt(ctx=ctx, run_id=run_id, canvas_id=canvas_id,
            source_node_id=source_node_id, agent_id=agent_id, idempotency_key=idempotency_key)
    except (ValueError, OSError, canvas_store.CanvasStoreError) as exc:
        raise HTTPException(409, str(exc)) from exc
    if run is None:
        raise HTTPException(404, "matching reference analysis admission not found")
    return {"ok": True, "data": run}


class SharedReferenceAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_node_id: str = Field(min_length=1, max_length=160)
    transcript_node_id: str | None = Field(default=None, min_length=1, max_length=160)
    analysis_node_id: str | None = Field(default=None, min_length=1, max_length=160)
    idempotency_key: str = Field(min_length=1, max_length=160)
    requested_evidence: dict[str, Any] = Field(default_factory=dict)


def _canvas_source_path(ctx, canvas, source_node_id, output):
    from novelvideo.api.routes.freezone import resolve_static_url_to_path
    from novelvideo.freezone.canvas_media_scope import scan_foreign_media_refs
    source = next((node for node in canvas.get("nodes", [])
                   if node.get("id") == source_node_id and node.get("type") == "videoNode"), None)
    url = (source or {}).get("data", {}).get("videoUrl")
    if not isinstance(url, str) or not url.strip():
        raise HTTPException(404, "source video not found on this canvas")
    if scan_foreign_media_refs({"nodes": [source]}, project_id=ctx.project_id):
        raise HTTPException(400, "reference media belongs to another project")
    try:
        path = resolve_static_url_to_path(url, output).resolve()
        root = output.resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("reference media is unavailable")
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/projects/{project}/freezone/canvases/{canvas_id}/reference-analysis")
async def ensure_shared_reference_analysis(project: str, canvas_id: str, body: SharedReferenceAnalysisRequest,
                                           user: dict = Depends(get_api_user)):
    from novelvideo.freezone.remix import reference_analysis_service as service
    ctx, _, _, output, canvas = await _context(project, canvas_id, user)
    relative = _canvas_source_path(ctx, canvas, body.source_node_id, output)
    try:
        ref = None
        transcript_status = None
        if body.transcript_node_id:
            if not body.analysis_node_id:
                raise ValueError('transcript input requires the selected analysis node')
            source = next(node for node in canvas['nodes'] if node.get('id') == body.source_node_id)
            ref = _connected_transcript(canvas, body.source_node_id, source['data']['videoUrl'], body.transcript_node_id, body.analysis_node_id)
            if ref:
                transcript_status = next(node for node in canvas['nodes'] if node.get('id') == body.transcript_node_id)['data']['status']
                snapshot = await asyncio.to_thread(service.source_snapshot, ctx, {'media_path': relative})
                validator = service.validated_no_speech_ref if transcript_status == 'no_speech' else service.validated_transcript_ref
                ref = await asyncio.to_thread(validator, ctx, ref, snapshot['media_sha256'])
        elif body.analysis_node_id:
            analysis = next((node for node in canvas.get('nodes', []) if node.get('id') == body.analysis_node_id and node.get('type') == 'videoStoryNode'), None)
            source = next(node for node in canvas['nodes'] if node.get('id') == body.source_node_id)
            if not analysis or not any(edge.get('source') == body.source_node_id and edge.get('target') == body.analysis_node_id for edge in canvas.get('edges', [])) or analysis.get('data', {}).get('sourceVideoUrl', source['data']['videoUrl']) != source['data']['videoUrl']:
                raise ValueError('selected analysis must be connected to the source video')
        result = await service.ensure_reference_analysis(ctx, {"media_path": relative,
            **({('no_speech_ref' if transcript_status == 'no_speech' else 'transcript_ref'): ref} if ref else {})},
            body.requested_evidence, body.idempotency_key)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    return {"ok": True, "data": result}


@router.get("/projects/{project}/freezone/canvases/{canvas_id}/reference-analysis/{operation_id}")
async def read_shared_reference_analysis(project: str, canvas_id: str, operation_id: str,
                                         user: dict = Depends(get_api_user)):
    from novelvideo.freezone.remix import reference_analysis_service as service
    from novelvideo.freezone.remix.operations import RemixOperationStore
    ctx, _, _, output, canvas = await _context(project, canvas_id, user, required_role="viewer")
    try:
        operation = await asyncio.to_thread(RemixOperationStore(output, ctx.project_id).get, operation_id)
        relative = operation.get("normalized_parameters", {}).get("payload", {}).get("media_path")
        sources = [_canvas_source_path(ctx, canvas, node["id"], output) for node in canvas.get("nodes", [])
                   if node.get("type") == "videoNode"]
        if relative not in sources:
            raise HTTPException(404, "reference source not found on this canvas")
        result = await service.get_reference_analysis(ctx, operation_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "reference analysis not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "data": result}


@router.get("/projects/{project}/freezone/canvases/{canvas_id}/reference-evidence")
async def read_shared_reference_evidence(project: str, canvas_id: str, request: Request, artifact_id: str,
    revision: int = Query(ge=1), start_us: int = Query(default=0, ge=0), end_us: int | None = Query(default=None, ge=1),
    cursor: str | None = None, limit: int = Query(default=50, ge=1, le=100), user: dict = Depends(get_api_user)):
    from novelvideo.freezone.remix import reference_analysis_service as service
    from novelvideo.freezone.remix.store import RemixArtifactStore
    ctx, _, _, output, canvas = await _context(project, canvas_id, user, required_role="viewer")
    ref = {"artifact_id": artifact_id, "revision": revision}
    try:
        artifact = await asyncio.to_thread(RemixArtifactStore(output, ctx.project_id).read, artifact_id, revision)
        if artifact["schema"] != "reference_analysis.v1":
            raise ValueError("analysis_ref must name reference analysis")
        sources = [_canvas_source_path(ctx, canvas, node["id"], output) for node in canvas.get("nodes", [])
                   if node.get("type") == "videoNode"]
        if artifact["payload"]["media_path"] not in sources:
            raise HTTPException(404, "reference source not found on this canvas")
        if end_us is None:
            end_us = artifact["payload"].get("coverage", {}).get("duration_us")
            if end_us is None:
                evidence_ref = artifact["payload"]["evidence_ref"]
                evidence = await asyncio.to_thread(RemixArtifactStore(output, ctx.project_id).read, evidence_ref["artifact_id"], evidence_ref["revision"])
                end_us = evidence["payload"]["duration_us"]
        # A cursor already carries its authenticated range. Let the service
        # restore it when the caller requests the next page without restating
        # (and potentially changing) start_us/end_us.
        requested_range = (None if cursor and "start_us" not in request.query_params
                           and "end_us" not in request.query_params else (start_us, end_us))
        result = await asyncio.to_thread(service.read_reference_evidence, ctx, ref,
                                         requested_range, cursor, limit=limit)
    except FileNotFoundError as exc:
        raise HTTPException(404, "reference analysis not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    from novelvideo.freezone.canvas_context_reader import saved_turn_binding
    from novelvideo.chat.reference_turn_outcome import record_context_receipt
    binding = await asyncio.to_thread(saved_turn_binding, ctx.state_dir, request.headers.get("X-DramaClaw-Turn-ID", ""))
    if binding and (binding.get("requester_user_id"), binding.get("project_id"), binding.get("canvas_id"), binding.get("agent_id")) == (ctx.requester_user_id, ctx.project_id, canvas_id, request.headers.get("X-DramaClaw-Agent-ID")):
        await asyncio.to_thread(record_context_receipt, ctx.state_dir, binding,
            {**result, "source": "reference_evidence_api", "canvas_revision": canvas.get("revision")})
    return {"ok": True, "data": result}
