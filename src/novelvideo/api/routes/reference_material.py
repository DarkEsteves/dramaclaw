"""Scoped material execution and authenticated user acceptance decisions."""
from __future__ import annotations
from typing import Any, Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from novelvideo.api.auth import get_api_user
from novelvideo.api.routes.remix import _call
from novelvideo.freezone.remix.material_execution import (
    read_material_state, review_material_candidate, confirm_material_review,
    canvas_material_resource, attach_material_execution_draft, validate_material_native_execution,
    register_material_execution,
)
from novelvideo.freezone.remix.material_work import (accept_material_candidate,
    validate_final_material_bindings, material_workflow_intent, _validate_preservation_bindings,
    revise_material_strategy, MaterialStrategyConflict, material_duration_issues,
    missing_material_parameter_choices, material_parameter_conflicts)
from novelvideo.freezone.remix.reference_media import list_reference_media
from novelvideo.api.reference_operation_logging import ReferenceOperationRoute

router = APIRouter(prefix='/projects/{project}/freezone/canvases/{canvas_id}/director',
                   route_class=ReferenceOperationRoute)

class WorkBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    work_ref: dict[str, Any]


class MaterialStageBody(WorkBody):
    # The existing native workflow approval is used for this selected wave.
    # Omission keeps the historical all-ready-wave behavior.
    placement_ids: list[str] | None = Field(default=None, max_length=200)

class ReferenceMediaBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    analysis_node_id: str = Field(min_length=1, max_length=160)

class StrategyBody(WorkBody):
    replacements: list[dict[str, Any]] = Field(min_length=1, max_length=200)
    command_id: str = Field(min_length=1, max_length=128)

class ReviewBody(WorkBody):
    step_id: str = Field(min_length=1, max_length=128)
    node_id: str = Field(min_length=1, max_length=160)

class MaterialSpeechStatusBody(ReviewBody):
    language: str = Field(default="auto", min_length=1, max_length=32)

class MaterialSpeechReviewBody(ReviewBody):
    observation_ref: dict[str, Any]

class MaterialSpeechCorrectionBody(MaterialSpeechReviewBody):
    target_signature: str = Field(min_length=1, max_length=128)
    corrections: list[dict[str, Any]] = Field(min_length=1, max_length=20000)
    confirmed: Literal[True]
    command_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")

class MaterialReviewBatchItem(ReviewBody):
    review_ref: dict[str, Any]

class MaterialReviewBatchBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    items: list[MaterialReviewBatchItem] = Field(min_length=1, max_length=20)
    command_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")

class AcceptBody(WorkBody):
    step_id: str = Field(min_length=1, max_length=128)
    resource: dict[str, Any]
    review_ref: dict[str, Any]
    command_id: str = Field(min_length=1, max_length=128)

class ExecutionBody(ReviewBody):
    run_id: str = Field(min_length=1, max_length=160)
    task_id: str = Field(min_length=1, max_length=200)

class DraftBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1)
    draft_id: str = Field(min_length=1, max_length=160)
    draft_revision: int = Field(ge=1)

async def _scope(project, canvas_id, user, work_ref, role='editor'):
    from .reference_director import _context, _plan_in_canvas
    ctx, canvas, store = await _context(project, canvas_id, user, role)
    state = await _call(read_material_state, store, work_ref, include_workflow_intent=False)
    await _plan_in_canvas(store, state['work']['plan_ref'], canvas)
    return ctx, canvas, store, state

@router.post('/reference-media')
async def reference_media_inventory(project: str, canvas_id: str, body: ReferenceMediaBody,
                                    user: dict = Depends(get_api_user)):
    from .reference_director import _context
    _, canvas, store = await _context(project, canvas_id, user, 'viewer')
    rows = await _call(list_reference_media, store=store, canvas=canvas,
                       analysis_node_id=body.analysis_node_id)
    return {'ok': True, 'data': rows}

@router.post('/material-strategy')
async def material_strategy(project: str, canvas_id: str, body: StrategyBody,
                            user: dict = Depends(get_api_user)):
    ctx, canvas, store, _ = await _scope(project, canvas_id, user, body.work_ref)
    try:
        result = await run_in_threadpool(revise_material_strategy, store, body.work_ref,
            body.replacements, body.command_id, canvas=canvas)
    except MaterialStrategyConflict as exc:
        raise HTTPException(409, {'code': exc.code, 'latest_ref': exc.latest_ref,
                                  'changed_step_ids': exc.changed_step_ids}) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, 'Artifact or revision not found') from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await _notify_material(ctx, canvas_id, store, result['work_ref'],
                           'material-strategy:' + body.command_id, invalidated=True)
    return {'ok': True, 'data': result}

@router.get('/material-state')
async def material_state_get(project: str, canvas_id: str, artifact_id: str = Query(), revision: int = Query(ge=1), user: dict = Depends(get_api_user)):
    _, canvas, store, state = await _scope(project, canvas_id, user, {'artifact_id':artifact_id,'revision':revision}, 'viewer')
    state = await _call(read_material_state, store, state['work_ref'], canvas=canvas, canvas_id=canvas_id)
    return {'ok':True,'data':state}

@router.post('/material-state')
async def material_state(project: str, canvas_id: str, body: WorkBody, user: dict = Depends(get_api_user)):
    _, canvas, store, state = await _scope(project, canvas_id, user, body.work_ref, 'viewer')
    state = await _call(read_material_state, store, state['work_ref'], canvas=canvas, canvas_id=canvas_id)
    return {'ok':True,'data':state}

@router.post('/material-speech-status')
async def material_speech_status(project: str, canvas_id: str, body: MaterialSpeechStatusBody,
                                user: dict = Depends(get_api_user)):
    from novelvideo.api.egress_binding import request_egress_scope
    from novelvideo.freezone.remix.candidate_speech_correction import find_candidate_speech_review
    ctx, canvas, store, _ = await _scope(project, canvas_id, user, body.work_ref, 'viewer')
    async with request_egress_scope(
        requester_user_id=ctx.requester_user_id, project_id=ctx.project_id,
        task_type='remix_analyze_material_speech',
    ):
        result = await _call(find_candidate_speech_review, store, ctx, canvas, canvas_id=canvas_id,
            work_ref=body.work_ref, step_id=body.step_id, node_id=body.node_id, language=body.language)
    return {'ok': True, 'data': result}

@router.post('/material-speech-review')
async def material_speech_review(project: str, canvas_id: str, body: MaterialSpeechReviewBody,
                                user: dict = Depends(get_api_user)):
    from novelvideo.freezone.remix.candidate_speech_correction import read_candidate_speech_review
    ctx, canvas, store, _ = await _scope(project, canvas_id, user, body.work_ref, 'viewer')
    result = await _call(read_candidate_speech_review, store, ctx, canvas, canvas_id=canvas_id,
        work_ref=body.work_ref, step_id=body.step_id, node_id=body.node_id,
        observation_ref=body.observation_ref)
    return {'ok': True, 'data': result}

@router.post('/material-speech-correction')
async def material_speech_correction(project: str, canvas_id: str, body: MaterialSpeechCorrectionBody,
                                    user: dict = Depends(get_api_user)):
    from novelvideo.freezone import canvas_store
    from novelvideo.freezone.remix.candidate_speech_correction import save_candidate_speech_correction
    from .reference_director import _emit_director_update
    if user.get('credential_kind') in {'agent_session', 'local_trusted_agent'}:
        raise HTTPException(403, 'speech listening confirmation requires an authenticated browser user')
    ctx, _, store, _ = await _scope(project, canvas_id, user, body.work_ref)
    def save_current():
        with canvas_store.get_canvas_write_mutex().write_mutex(ctx.state_dir, canvas_id) as guard:
            canvas = canvas_store.read_canvas(ctx.state_dir, canvas_id)
            if not canvas or canvas.get('project_id') != ctx.project_id:
                raise HTTPException(404, 'canvas not found')
            if canvas.get('access_model') in {'private', 'user_private'} and canvas.get('owner_principal_id') != ctx.requester_user_id:
                raise HTTPException(403, 'private canvas access denied')
            return save_candidate_speech_correction(store, ctx, canvas, canvas_id=canvas_id,
                work_ref=body.work_ref, step_id=body.step_id, node_id=body.node_id,
                observation_ref=body.observation_ref, target_signature=body.target_signature,
                corrections=body.corrections, confirmed=body.confirmed,
                command_id=body.command_id, actor_kind='user', _canvas_guard=guard)
    result = await _call(save_current)
    await _emit_director_update(ctx, canvas_id)
    return {'ok': True, 'data': result}

@router.post('/material-work/{artifact_id}/execution-draft')
async def execution_draft(project: str, canvas_id: str, artifact_id: str, body: DraftBody, user: dict = Depends(get_api_user)):
    ref = {'artifact_id':artifact_id,'revision':body.revision}
    ctx, canvas, store, _ = await _scope(project,canvas_id,user,ref)
    result = await _call(attach_material_execution_draft,store,ref,canvas_id=canvas_id,draft_id=body.draft_id,draft_revision=body.draft_revision,workflow_state_dir=ctx.state_dir,canvas=canvas)
    return {'ok':True,'data':result}

@router.post('/material-execution')
async def material_execution(project: str, canvas_id: str, body: ExecutionBody, user: dict = Depends(get_api_user)):
    from novelvideo.freezone.workflow_runs import read_workflow_run
    ctx, canvas, store, state = await _scope(project,canvas_id,user,body.work_ref)
    step = next((s for s in state['work']['steps'] if s['id'] == body.step_id),None)
    if not step:
        raise HTTPException(404,'unknown material step')
    execution = await _call(validate_material_native_execution,store,canvas,step,body.node_id,workflow_state_dir=ctx.state_dir)
    run = await _call(read_workflow_run,project_dir=ctx.state_dir,canvas_id=canvas_id,run_id=body.run_id)
    action = next((a for a in (run or {}).get('actions',[]) if a['node_id'] == body.node_id and a.get('task_key') == body.task_id),None)
    if not run or run['project_id'] != ctx.project_id or not action or action.get('recipe_id') != step['recipe_id']:
        raise HTTPException(422,'task does not belong to approved native material workflow')
    result = await _call(register_material_execution,store,body.work_ref,body.step_id,body.node_id,
                         {**execution,'run_id':body.run_id,'task_id':body.task_id})
    return {'ok':True,'data':result}

@router.post('/material-review')
async def material_review(project: str, canvas_id: str, body: ReviewBody, user: dict = Depends(get_api_user)):
    ctx, canvas, store, _ = await _scope(project,canvas_id,user,body.work_ref)
    result = await _call(review_material_candidate,store,body.work_ref,body.step_id,canvas,body.node_id,workflow_state_dir=ctx.state_dir)
    from novelvideo.freezone.remix.material_execution import inspect_material_content
    work = await _call(store.read,body.work_ref['artifact_id'],body.work_ref['revision'])
    step = next(s for s in work['payload']['steps'] if s['id'] == body.step_id)
    observation = await inspect_material_content(store.project_dir,result['review']['resource'],step,work['payload']['requirements'])
    review = await _call(store.read,result['review_ref']['artifact_id'],result['review_ref']['revision'])
    payload = {**review['payload'],'content_observation':observation}
    if observation['status'] in {'passed','rejected'}:
        payload['content'] = {'status':observation['status'],'checks':observation.get('checked_items',[]),
                              'findings':observation.get('findings',[])}
    saved = await _call(store.revise,review['id'],review['revision'],payload,review['upstream_refs'])
    if observation['status'] == 'rejected':
        from novelvideo.freezone.remix.material_work import retry_material_step
        failure = await _call(retry_material_step,store,body.work_ref,body.step_id,
                              '; '.join(observation.get('findings',[]))[:4000] or 'Actual content review rejected candidate',
                              'review-rejected:' + saved['id'],rejected_review_ref={'artifact_id':saved['id'],'revision':saved['revision']})
        result['work_ref'] = failure['work_ref']
    await _notify_material(ctx,canvas_id,store,result.get('work_ref',body.work_ref),
        'material-review:' + saved['id'],invalidated=observation['status']=='rejected')
    from .reference_director import _emit_director_update
    await _emit_director_update(ctx,canvas_id)
    return {'ok':True,'data':{**result,'review_ref':{'artifact_id':saved['id'],'revision':saved['revision']},'review':saved['payload'],'requires_user_review':observation['requires_user_review']}}

@router.post('/material-reviews/confirm-batch')
async def material_reviews_confirm_batch(project: str, canvas_id: str, body: MaterialReviewBatchBody,
                                         user: dict = Depends(get_api_user)):
    from novelvideo.freezone import canvas_store
    from novelvideo.freezone.remix.material_review_batch import confirm_material_review_batch
    from .reference_director import _context, _emit_director_update
    if user.get('credential_kind') in {'agent_session', 'local_trusted_agent'}:
        raise HTTPException(403, 'material confirmation requires an authenticated browser user')
    ctx, _, store = await _context(project, canvas_id, user)
    def confirm_current():
        with canvas_store.get_canvas_write_mutex().write_mutex(ctx.state_dir, canvas_id) as guard:
            canvas = canvas_store.read_canvas(ctx.state_dir, canvas_id)
            if not canvas or canvas.get('project_id') != ctx.project_id:
                raise HTTPException(404, 'canvas not found')
            if canvas.get('access_model') in {'private', 'user_private'} and canvas.get('owner_principal_id') != ctx.requester_user_id:
                raise HTTPException(403, 'private canvas access denied')
            return confirm_material_review_batch(store, ctx, canvas, canvas_id=canvas_id,
                items=[item.model_dump(mode='json') for item in body.items], command_id=body.command_id,
                _canvas_guard=guard)
    result = await _call(confirm_current)
    for item in result['items']:
        await _notify_material(ctx, canvas_id, store, item['review']['work_ref'],
            'material-review:' + item['review_ref']['artifact_id'] + ':' + str(item['review_ref']['revision']))
    await _emit_director_update(ctx, canvas_id)
    return {'ok': True, 'data': result}

@router.post('/material-reviews/{artifact_id}/confirm')
async def material_confirm(project: str, canvas_id: str, artifact_id: str, user: dict = Depends(get_api_user)):
    from .reference_director import _context
    if user.get('credential_kind') in {'agent_session','local_trusted_agent'}:
        raise HTTPException(403,'material content confirmation requires an authenticated browser user')
    ctx, canvas, store = await _context(project,canvas_id,user)
    review = await _call(store.read_latest,artifact_id)
    if review['schema'] != 'material_review.v1':
        raise HTTPException(422,'material review required')
    await _scope(project,canvas_id,user,review['payload']['work_ref'])
    result = await _call(confirm_material_review,store,{'artifact_id':artifact_id},canvas,
                         canvas_id=canvas_id,requester_user_id=ctx.requester_user_id)
    await _notify_material(ctx,canvas_id,store,review['payload']['work_ref'],'material-review:' + artifact_id + ':' + str(result['review_ref']['revision']))
    return {'ok':True,'data':result}

@router.post('/material-accept')
async def material_accept(project: str, canvas_id: str, body: AcceptBody, user: dict = Depends(get_api_user)):
    from novelvideo.freezone import canvas_store
    ctx, _, store, state = await _scope(project,canvas_id,user,body.work_ref)
    from novelvideo.freezone.remix.material_work import prepare_material_acceptance
    prepared = await _call(prepare_material_acceptance, store, body.work_ref, body.step_id,
        body.resource, body.review_ref, body.command_id, canvas_id=canvas_id)
    def accept_current_material():
        with canvas_store.get_canvas_write_mutex().write_mutex(ctx.state_dir, canvas_id) as guard:
            canvas = canvas_store.read_canvas(ctx.state_dir, canvas_id)
            if not canvas or canvas.get('project_id') != ctx.project_id:
                raise HTTPException(404, 'canvas not found')
            if canvas.get('access_model') in {'private', 'user_private'} and canvas.get('owner_principal_id') != ctx.requester_user_id:
                raise HTTPException(403, 'private canvas access denied')
            plan_id = state['work']['plan_ref']['artifact_id']
            if not any(n.get('type') == 'scriptNode' and
                       (n.get('data', {}).get('productionPlanV2Ref') or {}).get('artifactId',
                        (n.get('data', {}).get('productionPlanV2Ref') or {}).get('artifact_id')) == plan_id
                       for n in canvas.get('nodes', [])):
                raise HTTPException(404, 'plan is not on the current canvas')
            current = canvas_material_resource(store,canvas,body.resource.get('node_id'),body.resource.get('media_kind'))
            if current != body.resource:
                raise HTTPException(409,'candidate differs from current canvas media')
            return accept_material_candidate(store,body.work_ref,body.step_id,body.resource,body.review_ref,body.command_id,
                                             canvas_id=canvas_id, _canvas_guard=guard, _prepared=prepared)
    result = await _call(accept_current_material)
    await _notify_material(ctx,canvas_id,store,result['work_ref'],'material-accept:' + body.work_ref['artifact_id'] + ':' + body.command_id)
    return {'ok':True,'data':result}


@router.post('/material-stage')
async def material_stage(project: str, canvas_id: str, body: MaterialStageBody, user: dict = Depends(get_api_user)):
    _, canvas, store, state = await _scope(project,canvas_id,user,body.work_ref)
    if state['work_ref'] != body.work_ref:
        raise HTTPException(409,'material work changed; reload current revision')
    await _call(validate_final_material_bindings,state['work']['requirements'])
    await _call(_validate_preservation_bindings, store, state['work'], canvas)
    for step in state['work']['steps']:
        if step.get('status') == 'accepted':
            resource = step['accepted_resource']
            current = await _call(canvas_material_resource,store,canvas,resource['node_id'],resource['media_kind'])
            if current != resource:
                raise HTTPException(409,'accepted material changed; review replacement before continuing')
    selected = getattr(body, 'placement_ids', None)
    scope = None
    if selected is not None:
        from novelvideo.freezone.remix.material_generation_scope import select_material_generation_steps
        try:
            scope = await _call(select_material_generation_steps, state['work'], placement_ids=selected)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    selected_ids = set(scope['ready'] if scope is not None else state['ready_step_ids'])
    ready_steps = [step for step in state['work']['steps'] if step['id'] in selected_ids]
    duration_issues = material_duration_issues(ready_steps)
    if duration_issues:
        raise HTTPException(422, {'code': 'material_duration_invalid', 'issues': duration_issues})
    choices = state['work'].get('confirmed_generation_choices')
    if choices is not None:
        missing = missing_material_parameter_choices(ready_steps, choices)
        conflicts = material_parameter_conflicts(ready_steps, choices)
        if missing or conflicts:
            raise HTTPException(409, {'code': 'material_parameter_choice_conflict',
                                      'required_choices': missing, 'conflicts': conflicts})
    intent = await _call(material_workflow_intent, state['work'], canvas=canvas, store=store,
                         selected_step_ids=scope['ready'] if scope is not None else None)
    return {'ok':True,'data':{**state, 'workflow_intent':intent,
                              **({'ready_step_ids': scope['ready'], 'generation_scope': scope}
                                 if scope is not None else {})}}


async def _notify_material(ctx, canvas_id, store, work_ref, event_key, *, invalidated=False):
    from novelvideo.freezone.remix.reference_director import list_director_artifacts
    from .reference_director import _notify_commission
    state = await _call(read_material_state,store,work_ref,include_workflow_intent=False)
    artifacts = await _call(list_director_artifacts,store,canvas_id,ctx.requester_user_id)
    for commission in artifacts['commissions']:
        if (commission.get('plan_ref') or {}).get('artifact_id') == state['work']['plan_ref']['artifact_id']:
            if invalidated and commission['phase'] in {'preparing_materials','aligning_speech','composing','reviewing_output','completed'}:
                from novelvideo.freezone.remix.reference_commission import advance_reference_commission
                commission = await _call(advance_reference_commission, store, commission['commission_ref'],
                    {'type':'materials_invalidated','work_ref':state['work_ref']}, commission['commission_ref']['revision'])
            await _notify_commission(ctx,canvas_id,commission,event_key)
    from .reference_director import _emit_director_update
    await _emit_director_update(ctx,canvas_id)


class RetryBody(WorkBody):
    step_id: str = Field(min_length=1,max_length=128)
    reason: str = Field(min_length=1,max_length=4000)
    command_id: str = Field(min_length=1,max_length=128)

@router.post('/material-retry')
async def material_retry(project: str, canvas_id: str, body: RetryBody, user: dict = Depends(get_api_user)):
    from novelvideo.freezone.remix.material_work import retry_material_step
    ctx, _, store, _ = await _scope(project,canvas_id,user,body.work_ref)
    result = await _call(retry_material_step,store,body.work_ref,body.step_id,body.reason,body.command_id)
    await _notify_material(ctx,canvas_id,store,result['work_ref'],'material-retry:' + body.work_ref['artifact_id'] + ':' + body.command_id, invalidated=True)
    return {'ok':True,'data':result}
