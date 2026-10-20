"""Prepare fixed native nodes through the existing draft/approval lifecycle."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from novelvideo.freezone import canvas_store
from novelvideo.freezone.agent_workflows.reference_production import native_entry_intent
from .reference_commission import read_reference_commission
from .store import RemixArtifactStore
from .native_plan_stage import _binding, read_native_plan_stage

logger = logging.getLogger(__name__)

class ProductionNodesRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    commission_ref: dict[str, Any]
    goal: str = Field(min_length=1, max_length=20000)
    run_after_create: StrictBool
    plan_count: int = Field(default=1, ge=1, le=100, strict=True)
    requirements: dict[str, Any] | None = None
    preserve_replace: list[dict[str, Any]] | None = Field(default=None, max_length=30)


def _basis(ctx, canvas_id, request, store=None):
    canvas = canvas_store.read_canvas(ctx.state_dir, canvas_id)
    if not canvas or (canvas.get('project_id'), canvas.get('canvas_id')) != (ctx.project_id, canvas_id):
        raise HTTPException(404, 'canvas not found')
    if canvas.get('access_model') in {'private', 'user_private'} and canvas.get('owner_principal_id') != ctx.requester_user_id:
        raise HTTPException(403, 'private canvas access denied')
    store = store or RemixArtifactStore(Path(ctx.output_dir), ctx.project_id, created_by=ctx.requester_user_id)
    try:
        commission = read_reference_commission(store, request.commission_ref, latest=True)
    except FileNotFoundError as exc:
        raise HTTPException(404, 'commission not found') from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc
    if (commission['project_id'], commission['canvas_id'], commission['requester_user_id']) != (ctx.project_id, canvas_id, ctx.requester_user_id):
        raise HTTPException(403, 'commission belongs to another canvas or actor')
    if request.commission_ref != commission['commission_ref']:
        raise HTTPException(409, {'code': 'stale_commission_ref', 'commission_ref': commission['commission_ref']})
    if not commission.get('direction_revision') or not commission.get('direction_fingerprint'):
        raise HTTPException(409, {'code': 'reference_direction_pending'})
    return commission, canvas


def _assert_request(draft, intent, run_after_create):
    # Existing native claim owns the direction, including all its slots. Never
    # silently replace its requested target or execution policy on a retry.
    previous = draft['intent']
    if (previous.get('user_goal') != intent['user_goal']
            or (previous.get('inputs') or {}).get('plan_count', 1) != intent['inputs']['plan_count']
            or draft['run_after_create'] != run_after_create):
        raise HTTPException(409, {'code': 'native_request_conflict', 'draft_id': draft['draft_id'],
                                  'revision': draft['revision']})


async def _renew_submission_owner(store, owner: dict, stop: asyncio.Event,
                                  lost: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=20)
            break
        except asyncio.TimeoutError:
            try:
                renewed = await asyncio.to_thread(store.renew_ordinary_turn,
                    owner['run_id'], owner['token'])
            except Exception:
                logger.exception('native submission owner renewal failed run_id=%s', owner['run_id'])
                renewed = False
            if not renewed:
                lost.set()
                break


def _persist_under_human_fence(ctx, *, canvas_id: str, agent_id: str,
                               human_guard: dict | None, persist):
    """Serialize a native draft write with a later user withdrawal."""
    if human_guard is None:
        return persist()
    from novelvideo.chat.store import ChatScope, chat_store

    scope = ChatScope(kind='project', id=human_guard['chat_scope_id'],
                      surface='freezone', canvas_id=canvas_id, agent_id=agent_id,
                      state_dir=str(ctx.state_dir))
    connection = chat_store.connect(ctx.requester_username, scope)
    try:
        connection.execute('BEGIN IMMEDIATE')
        latest_user = connection.execute(
            "SELECT id, turn_id FROM chat_messages WHERE role='user' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if (latest_user is None or latest_user['id'] != human_guard['message_id']
                or latest_user['turn_id'] != human_guard['turn_id']):
            raise HTTPException(409, {'code': 'human_turn_changed'})
        result = persist()
        connection.commit()
        return result
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


async def prepare_production_nodes(*, ctx, canvas_id: str, request: ProductionNodesRequest,
                                   agent_id: str, turn_id: str = '', agent_request: bool = False) -> dict:
    """Verify direction once, then use the existing native draft and approval path."""
    from novelvideo.chat.reference_direction_commit import ensure_confirmed_direction_for_entry
    from novelvideo.chat.reference_remix_continuation import coordinator, finish_ordinary_production_turn

    resolution = await ensure_confirmed_direction_for_entry(
        ctx=ctx, canvas_id=canvas_id, agent_id=agent_id, turn_id=turn_id,
        commission_ref=request.commission_ref, goal=request.goal,
        agent_request=agent_request,
        requested_operation={"kind": "prepare_production_nodes",
                             "run_after_create": request.run_after_create,
                             "plan_count": request.plan_count},
        requirements=request.requirements,
        preserve_replace=request.preserve_replace,
    )
    owner = resolution.get('ordinary_owner')
    if resolution['commission_ref'] != request.commission_ref:
        request = request.model_copy(update={"commission_ref": resolution['commission_ref']})
    stop_renewal = asyncio.Event()
    lost_owner = asyncio.Event()
    renewal = (asyncio.create_task(_renew_submission_owner(
        coordinator.store, owner, stop_renewal, lost_owner)) if owner else None)
    try:
        try:
            prepared = await _prepare_production_nodes_core(ctx=ctx, canvas_id=canvas_id,
                request=request, agent_id=agent_id,
                human_guard=resolution.get('human_guard'))
        finally:
            stop_renewal.set()
            if renewal is not None:
                await renewal
        if lost_owner.is_set():
            raise HTTPException(409, {'code': 'production_turn_ownership_lost'})
    except BaseException:
        if owner:
            await asyncio.to_thread(coordinator.store.finish_ordinary_turn,
                owner['run_id'], owner['token'], state='blocked',
                receipt={'error': 'native_prepare_failed'})
        raise
    if owner:
        await asyncio.to_thread(finish_ordinary_production_turn, {'ctx': ctx, 'owner': owner})
    logger.info("reference_flow.native_draft_prepared project_id=%s canvas_id=%s turn_id=%s draft_id=%s revision=%s run_after_create=%s",
                ctx.project_id, canvas_id, turn_id, prepared.get('draft_id'),
                prepared.get('revision'), request.run_after_create)
    return prepared


async def _prepare_production_nodes_core(*, ctx, canvas_id: str, request: ProductionNodesRequest,
                                         agent_id: str, human_guard: dict | None = None) -> dict:
    """Pure structural preparation; model admission remains on actual generation."""
    from novelvideo.api.routes.freezone import (
        prepare_validated_workflow_draft, persist_validated_workflow_draft,
        _workflow_draft_api_data,
    )
    from novelvideo.freezone.workflow_drafts import read_native_plan_claim

    commission, canvas = await asyncio.to_thread(_basis, ctx, canvas_id, request)
    intent = native_entry_intent(request, commission)
    existing = await asyncio.to_thread(read_native_plan_claim, project_dir=ctx.state_dir,
        project_id=ctx.project_id, canvas_id=canvas_id, requester_user_id=ctx.requester_user_id,
        commission_id=commission['commission_ref']['artifact_id'], direction_revision=commission['direction_revision'])
    if existing and existing['status'] != 'cancelled' and (existing['status'] != 'ready' or existing['expires_at'] >= time.time()):
        if existing['compiled'].get('reference_binding') != _binding(ctx, commission):
            raise HTTPException(409, {'code': 'binding_conflict', 'draft_id': existing['draft_id']})
        _assert_request(existing, intent, request.run_after_create)
        draft = existing
    else:
        if commission['phase'] != 'drafting_plan':
            raise HTTPException(409, {'code': 'reference_plan_stage_unavailable'})
        prepared = await prepare_validated_workflow_draft(ctx=ctx, canvas_id=canvas_id,
            body={'intent': intent, 'run_after_create': request.run_after_create},
            state_dir=ctx.state_dir, project=ctx.project_id,
            user={'username': ctx.requester_username})
        # Reject changes during compilation/preflight before persisting anything.
        await asyncio.to_thread(_basis, ctx, canvas_id, request)
        try:
            def persist_current():
                # Serialize with canvas mutation and commission publication. The
                # provider-free preflight has already completed outside locks.
                with canvas_store.get_canvas_write_mutex().write_mutex(ctx.state_dir, canvas_id) as guard:
                    store = RemixArtifactStore(Path(ctx.output_dir), ctx.project_id, created_by=ctx.requester_user_id)
                    with store._locked() as artifact_guard:
                        from .production_inventory import _LockedEvidenceStore
                        current, current_canvas = _basis(ctx, canvas_id, request, _LockedEvidenceStore(store, artifact_guard))
                        from novelvideo.freezone.workflow_external_inputs import resolve_external_image_inputs
                        resolve_external_image_inputs(
                            prepared['compiled']['plan'], current_canvas,
                            project_id=ctx.project_id, canvas_id=canvas_id,
                            expected=prepared['compiled']['external_inputs_verified'],
                        )
                        expected = prepared['compiled']['reference_binding']
                        if (expected['direction_revision'], expected['direction_fingerprint']) != (current['direction_revision'], current['direction_fingerprint']):
                            raise HTTPException(409, {'code': 'binding_conflict'})
                        guard.reassert()
                        artifact_guard.reassert()
                        return persist_validated_workflow_draft(ctx=ctx, canvas_id=canvas_id,
                            state_dir=ctx.state_dir, prepared=prepared, run_after_create=request.run_after_create)
            draft = await asyncio.to_thread(
                _persist_under_human_fence, ctx, canvas_id=canvas_id,
                agent_id=agent_id, human_guard=human_guard, persist=persist_current)
        except ValueError as exc:
            raise HTTPException(409, {'code': 'binding_conflict', 'message': str(exc)}) from exc
        _assert_request(draft, intent, request.run_after_create)
    stage = await asyncio.to_thread(read_native_plan_stage, ctx=ctx, canvas=canvas, commission=commission)
    return {**_workflow_draft_api_data(draft, summary=False), 'native_plan_stage': stage,
            'next_action': stage['next_action']}
