import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from novelvideo.freezone import canvas_store
from novelvideo.freezone.remix.reference_commission import (
    AuthenticatedDecisionEvidence, advance_reference_commission, create_reference_commission,
)
from novelvideo.freezone.remix.store import RemixArtifactStore


@pytest.mark.asyncio
@pytest.mark.parametrize('omit_project_id', [False, True])
@pytest.mark.parametrize('run', [False, True])
async def test_confirmed_chat_goal_prepares_native_draft_without_applying_canvas(basis, monkeypatch, omit_project_id, run):
    from novelvideo.chat import reference_remix_continuation as continuation
    from novelvideo.chat.reference_remix_continuation import ReferenceRemixStore
    from novelvideo.chat.store import ChatScope, ChatStore
    from novelvideo.freezone.canvas_context_reader import saved_context_execution
    from novelvideo.freezone.remix.production_node_entry import ProductionNodesRequest, prepare_production_nodes

    ctx, store, _, canvas = basis
    initial = create_reference_commission(store, canvas_id='c', requester_user_id='u',
        analysis_node_id='analysis', goal='original goal',
        analysis_evidence={'task_id': 't-new', 'result_fingerprint': 'h-new'})
    runs = ReferenceRemixStore(ctx.state_dir / 'native-entry-runs.db')
    runs.create(dict(run_id='native-entry', project_id='p', requester_user_id='u',
        canvas_id='c', agent_id='main', idempotency_key='native-entry',
        status='awaiting_direction', commission_ref=initial['commission_ref'],
        commission_phase='awaiting_direction'))
    monkeypatch.setattr(continuation.coordinator, 'store', runs)
    scope = ChatScope(kind='project', id='project-name', surface='freezone', canvas_id='c',
                      agent_id='main', state_dir=str(ctx.state_dir))
    message = ChatStore().append_message('u', scope, 'user',
        '产品换黑咖啡，用中文，其余保持不变。' if run else
        '按讨论好的黑咖啡方向创建方案节点，暂时不要执行起草。', turn_id='human-turn')
    identity = SimpleNamespace(surface='freezone', requester_user_id='u', project_id='p',
                               canvas_id='c', agent_id='main')
    before = repr(canvas)
    submitted_ref = dict(initial['commission_ref'])
    if omit_project_id:
        submitted_ref.pop('project_id')
    with saved_context_execution(execution_context=identity, turn_id='human-turn',
            state_dir=ctx.state_dir, human_message_id=message['id'], chat_scope_id=scope.id):
        result = await prepare_production_nodes(ctx=ctx, canvas_id='c', agent_id='main',
            turn_id='human-turn', request=ProductionNodesRequest(
                commission_ref=submitted_ref, goal='保留原片节奏，换成黑咖啡产品',
                run_after_create=run))
    assert result['native_plan_stage']['state'] == 'ready_for_canvas_approval'
    assert result['operation_id'] == result['task_id'] == ''
    assert result['run_after_create'] is run
    assert repr(canvas) == before


@pytest.mark.asyncio
async def test_prepared_direction_accepts_commission_ref_without_project_id(basis):
    from novelvideo.freezone.remix.production_node_entry import ProductionNodesRequest, prepare_production_nodes

    ctx, _, commission, canvas = basis
    submitted_ref = {key: value for key, value in commission['commission_ref'].items()
                     if key != 'project_id'}
    before = repr(canvas)
    result = await prepare_production_nodes(ctx=ctx, canvas_id='c', agent_id='main',
        request=ProductionNodesRequest(commission_ref=submitted_ref,
            goal='confirmed', run_after_create=False))

    assert result['native_plan_stage']['state'] == 'ready_for_canvas_approval'
    assert repr(canvas) == before


@pytest.fixture
def basis(tmp_path, monkeypatch):
    from novelvideo.api.routes import freezone
    ctx = SimpleNamespace(state_dir=tmp_path, output_dir=tmp_path / 'output', project_id='p',
                          requester_user_id='u', requester_username='u', owner_project_label='u/p',
                          owner_type='user', owner_id='u', real_preflight=freezone._check_workflow_runtime)
    store = RemixArtifactStore(ctx.output_dir, 'p', created_by='u')
    initial = create_reference_commission(store, canvas_id='c', requester_user_id='u',
        analysis_node_id='analysis', goal='original goal',
        analysis_evidence={'task_id': 't', 'result_fingerprint': 'h'})
    commission = advance_reference_commission(store, initial['commission_ref'],
        {'type': 'direction_confirmed', 'direction': {'target_goal': 'confirmed'}},
        initial['commission_ref']['revision'],
        host_evidence=AuthenticatedDecisionEvidence('p', 'c', 'u', 'message-1'))
    canvas = {'project_id': 'p', 'canvas_id': 'c', 'revision': 1, 'nodes': [
        {'id': 'analysis', 'type': 'videoStoryNode', 'data': {'sourceVideoUrl': '/source.mp4',
            'rawResult': {'video_story': {'summary': 'source analysis'}}}}], 'edges': []}
    monkeypatch.setattr(canvas_store, 'read_canvas', lambda *_: canvas)
    async def runtime(*args, **kwargs):
        return {'ok': True}
    monkeypatch.setattr(freezone, '_check_workflow_runtime', runtime)
    return ctx, store, commission, canvas


@pytest.mark.asyncio
@pytest.mark.parametrize('goal', ['保留访谈对白，修改场景', '无对白展示，强调质感', '三段叙事从冲突到解决'])
async def test_native_prepare_only_persists_draft_with_full_goal(basis, goal):
    from novelvideo.freezone.remix.production_node_entry import ProductionNodesRequest, prepare_production_nodes
    ctx, _, commission, canvas = basis
    before = repr(canvas)
    request = ProductionNodesRequest(commission_ref=commission['commission_ref'], goal=goal, run_after_create=True)
    results = await asyncio.gather(*[prepare_production_nodes(ctx=ctx, canvas_id='c', request=request, agent_id='main') for _ in range(2)])
    assert results[0]['draft_id'] == results[1]['draft_id']
    result = results[0]
    assert result['operation_id'] == '' and result['task_id'] == ''
    assert result['run_after_create'] is True
    nodes = result['compiled']['plan']['nodes']
    assert [n['node_type'] for n in nodes] == ['scriptNode', 'videoComposeNode']
    assert goal in nodes[0]['data']['processingPayload']['topic']
    assert result['intent']['user_goal'] == goal
    assert repr(canvas) == before
    assert result['native_plan_stage']['state'] == 'ready_for_canvas_approval'


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('goal', 'changed'), ('run_after_create', False), ('plan_count', 2)])
async def test_retry_conflicting_request_is_rejected(basis, field, value):
    from novelvideo.freezone.remix.production_node_entry import ProductionNodesRequest, prepare_production_nodes
    ctx, _, commission, _ = basis
    request = dict(commission_ref=commission['commission_ref'], goal='goal', run_after_create=True)
    await prepare_production_nodes(ctx=ctx, canvas_id='c', request=ProductionNodesRequest(**request), agent_id='main')
    with pytest.raises(HTTPException) as error:
        await prepare_production_nodes(ctx=ctx, canvas_id='c', request=ProductionNodesRequest(**{**request, field:value}), agent_id='main')
    assert error.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize('problem', ['stale', 'actor', 'private', 'project', 'incomplete'])
async def test_invalid_basis_cannot_prepare(basis, problem):
    from novelvideo.freezone.remix.production_node_entry import ProductionNodesRequest, prepare_production_nodes
    ctx, _, commission, canvas = basis
    ref = dict(commission['commission_ref'])
    if problem == 'stale': ref['revision'] -= 1
    if problem == 'project': ref['project_id'] = 'foreign'
    if problem == 'actor': ctx.requester_user_id = 'other'
    if problem == 'private': canvas.update(access_model='private', owner_principal_id='other')
    if problem == 'incomplete': canvas['nodes'][0]['data']['isAnalyzing'] = True
    with pytest.raises(HTTPException):
        await prepare_production_nodes(ctx=ctx, canvas_id='c', request=ProductionNodesRequest(commission_ref=ref, goal='goal', run_after_create=False), agent_id='main')


def test_request_requires_boolean_and_rejects_graph():
    from novelvideo.freezone.remix.production_node_entry import ProductionNodesRequest
    for extra in [{'run_after_create': 'false'}, {'run_after_create': False, 'graph': {}}, {}]:
        with pytest.raises(ValidationError):
            ProductionNodesRequest(commission_ref={}, goal='goal', **extra)


@pytest.mark.asyncio
@pytest.mark.parametrize('scope,count,expected', [('仅作品方案', 1, 1), ('仅作品方案', 3, 3), ('可编辑合成', 3, 6)])
async def test_saved_scope_and_requested_count_determine_nodes(basis, scope, count, expected):
    from novelvideo.freezone.remix.production_node_entry import ProductionNodesRequest, prepare_production_nodes
    ctx, store, _, _ = basis
    initial = create_reference_commission(store, canvas_id='c', requester_user_id='u',
        analysis_node_id='analysis', goal='goal', production_scope=scope,
        analysis_evidence={'task_id':'t', 'result_fingerprint':'h'})
    commission = advance_reference_commission(store, initial['commission_ref'],
        {'type':'direction_confirmed', 'direction': {'target_goal':'goal'}}, initial['commission_ref']['revision'],
        host_evidence=AuthenticatedDecisionEvidence('p','c','u','message-2'))
    result = await prepare_production_nodes(ctx=ctx, canvas_id='c', request=ProductionNodesRequest(
        commission_ref=commission['commission_ref'], goal='goal', run_after_create=False, plan_count=count), agent_id='main')
    assert len(result['compiled']['plan']['nodes']) == expected
    assert result['run_after_create'] is False
    assert len(result['native_plan_stage']['slots']) == count


@pytest.mark.asyncio
async def test_expired_ready_draft_is_replaced_using_same_claim(basis):
    import sqlite3
    from novelvideo.freezone.workflow_drafts import workflow_drafts_db_path
    from novelvideo.freezone.remix.production_node_entry import ProductionNodesRequest, prepare_production_nodes
    ctx, _, commission, _ = basis
    request = ProductionNodesRequest(commission_ref=commission['commission_ref'], goal='goal', run_after_create=False)
    first = await prepare_production_nodes(ctx=ctx, canvas_id='c', request=request, agent_id='main')
    with sqlite3.connect(workflow_drafts_db_path(ctx.state_dir)) as conn:
        conn.execute('UPDATE workflow_drafts SET expires_at=1 WHERE draft_id=?', (first['draft_id'],))
    second = await prepare_production_nodes(ctx=ctx, canvas_id='c', request=request, agent_id='main')
    assert first['draft_id'] != second['draft_id']


@pytest.mark.asyncio
async def test_prepare_has_no_product_admission_even_when_runtime_metered(basis, monkeypatch):
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.remix.production_node_entry import ProductionNodesRequest, prepare_production_nodes
    ctx, _, commission, _ = basis
    def unexpected(*args, **kwargs):
        raise AssertionError('native structure must not admit a model product')
    monkeypatch.setattr(freezone, 'get_usage_meter', lambda: object())
    monkeypatch.setattr(freezone, 'begin_agent_product_operation', unexpected)
    monkeypatch.setattr(freezone, 'finish_agent_product_operation', unexpected)
    result = await prepare_production_nodes(ctx=ctx, canvas_id='c', request=ProductionNodesRequest(
        commission_ref=commission['commission_ref'], goal='goal', run_after_create=True), agent_id='main')
    assert result['task_id'] == result['operation_id'] == ''


@pytest.mark.asyncio
async def test_claim_with_different_direction_binding_is_rejected(basis, monkeypatch):
    from copy import deepcopy
    from novelvideo.freezone import workflow_drafts
    from novelvideo.freezone.remix.production_node_entry import ProductionNodesRequest, prepare_production_nodes
    ctx, _, commission, _ = basis
    request = ProductionNodesRequest(commission_ref=commission['commission_ref'], goal='goal', run_after_create=False)
    draft = await prepare_production_nodes(ctx=ctx, canvas_id='c', request=request, agent_id='main')
    damaged = deepcopy(draft)
    damaged['compiled']['reference_binding']['direction_fingerprint'] = 'other-direction'
    monkeypatch.setattr(workflow_drafts, 'read_native_plan_claim', lambda **_: damaged)
    with pytest.raises(HTTPException) as error:
        await prepare_production_nodes(ctx=ctx, canvas_id='c', request=request, agent_id='main')
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_goal_cannot_override_confirmed_structured_output(basis):
    from novelvideo.freezone.remix.production_node_entry import ProductionNodesRequest, prepare_production_nodes
    ctx, store, commission, _ = basis
    requirements = {'duration_us': {'min': 18000000, 'max': 18000000},
                    'output_requirements': {'width': 1280, 'height': 720}}
    current = advance_reference_commission(store, commission['commission_ref'],
        {'type':'direction_confirmed', 'direction': {'target_goal':'goal', 'requirements':requirements}},
        commission['commission_ref']['revision'],
        host_evidence=AuthenticatedDecisionEvidence('p','c','u','message-3'))
    result = await prepare_production_nodes(ctx=ctx, canvas_id='c', request=ProductionNodesRequest(
        commission_ref=current['commission_ref'], goal='使用竖屏长片的创意描述', run_after_create=False), agent_id='main')
    payload = result['compiled']['plan']['nodes'][0]['data']['processingPayload']
    assert payload['requirements']['duration_us'] == requirements['duration_us']
    assert payload['requirements']['output_requirements'] == requirements['output_requirements']
    assert payload['requirements_provenance']['commission_ref'] == current['commission_ref']


@pytest.mark.asyncio
async def test_real_preflight_resolves_limits_by_authenticated_project_id(basis, monkeypatch):
    from novelvideo.api.routes import freezone, tasks
    from novelvideo.freezone.remix.production_node_entry import ProductionNodesRequest, prepare_production_nodes
    ctx, _, commission, _ = basis
    looked_up = []
    async def resolve(*, user, project_id, required_role):
        looked_up.append(project_id)
        if project_id != ctx.project_id:
            raise HTTPException(404, 'registry requires project ID')
        return ctx
    async def eligible(**kwargs):
        return 1
    monkeypatch.setattr(freezone, '_check_workflow_runtime', ctx.real_preflight)
    monkeypatch.setattr(tasks, 'resolve_project_context', resolve)
    monkeypatch.setattr(tasks, 'get_project_access', lambda: SimpleNamespace(count_project_task_eligible_users=eligible))
    monkeypatch.setattr(tasks, 'get_task_manager', lambda: SimpleNamespace(
        count_active_tasks_for_project_lane=lambda *_: 0,
        count_active_tasks_for_project_user_lane=lambda *_: 0))
    result = await prepare_production_nodes(ctx=ctx, canvas_id='c', request=ProductionNodesRequest(
        commission_ref=commission['commission_ref'], goal='goal', run_after_create=False), agent_id='main')
    assert looked_up == ['p']
    assert result['compiled']['preflight']['runtime_checks']['queue_capacity'] != 'unavailable'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['result', 'source', 'unfinished'])
async def test_analysis_changed_during_preflight_is_rejected_before_persist(basis, monkeypatch, change):
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.workflow_drafts import read_native_plan_claim
    from novelvideo.freezone.remix.production_node_entry import ProductionNodesRequest, prepare_production_nodes
    ctx, _, commission, canvas = basis
    async def mutate_during_preflight(*args, **kwargs):
        data = canvas['nodes'][0]['data']
        if change == 'result': data['rawResult']['video_story']['summary'] = 'different analysis'
        if change == 'source': data['sourceVideoUrl'] = '/changed.mp4'
        if change == 'unfinished': data['isAnalyzing'] = True
        return {'ok': True}
    monkeypatch.setattr(freezone, '_check_workflow_runtime', mutate_during_preflight)
    with pytest.raises(HTTPException) as error:
        await prepare_production_nodes(ctx=ctx, canvas_id='c', request=ProductionNodesRequest(
            commission_ref=commission['commission_ref'], goal='goal', run_after_create=False), agent_id='main')
    assert error.value.status_code == 409
    assert read_native_plan_claim(project_dir=ctx.state_dir, project_id='p', canvas_id='c',
        requester_user_id='u', commission_id=commission['commission_ref']['artifact_id'],
        direction_revision=commission['direction_revision']) is None
