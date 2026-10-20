"""Native entry journeys using real artifacts, drafts, graph and delivery stores.

The browser applies the compiled commands in a small transport fixture. Model
admission and provider execution stop at the task boundary, so these journeys do
not contact a provider or spend quota.
"""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from novelvideo.freezone import canvas_store
from novelvideo.freezone.agent_workflows.graph import build_workflow_graph_commands
from novelvideo.freezone.remix.native_plan_stage import read_native_plan_stage
from novelvideo.freezone.remix.production_node_entry import ProductionNodesRequest, prepare_production_nodes
from novelvideo.freezone.remix.reference_commission import (
    AuthenticatedDecisionEvidence, advance_reference_commission, create_reference_commission,
)
from novelvideo.freezone.remix.store import RemixArtifactStore
from novelvideo.freezone.workflow_delivery import inspect_workflow_graph_delivery
from novelvideo.freezone.workflow_drafts import (
    bind_workflow_draft_task, claim_workflow_draft_confirmation,
    list_workflow_delivery_receipts, read_native_plan_slot_bindings,
    record_workflow_delivery_receipt, settle_workflow_draft_delivery,
)
from novelvideo.freezone.workflow_runs import (
    bind_workflow_action_processing_operation, create_workflow_run, list_workflow_runs,
    reconcile_workflow_runs_with_tasks, update_workflow_run,
)
from novelvideo.freezone.remix.operations import RemixOperationStore


@pytest.fixture
def journey(tmp_path, monkeypatch):
    from novelvideo.api.routes import freezone
    ctx = SimpleNamespace(state_dir=tmp_path, output_dir=tmp_path / 'output', project_id='p',
        requester_user_id='u', requester_username='u', owner_project_label='u/p',
        owner_type='user', owner_id='u')
    store = RemixArtifactStore(ctx.output_dir, 'p', created_by='u')
    initial = create_reference_commission(store, canvas_id='c', requester_user_id='u',
        analysis_node_id='analysis', goal='initial creative discussion',
        analysis_evidence={'task_id': 'analysis-task', 'result_fingerprint': 'analysis-result'})
    canvas = {'project_id': 'p', 'canvas_id': 'c', 'revision': 1, 'nodes': [
        {'id': 'analysis', 'type': 'videoStoryNode', 'data': {
            'sourceVideoUrl': '/original.mp4',
            'rawResult': {'video_story': {'summary': 'completed original analysis'}}}},
        {'id': 'newer-unrelated-analysis', 'type': 'videoStoryNode', 'data': {
            'sourceVideoUrl': '/other.mp4',
            'rawResult': {'video_story': {'summary': 'another branch'}}}},
    ], 'edges': []}
    canvas_store.save_canvas(ctx.state_dir, 'c', base_revision=None, build_payload=lambda _: canvas)
    async def runtime(*args, **kwargs):
        return {'ok': True}
    monkeypatch.setattr(freezone, '_check_workflow_runtime', runtime)
    monkeypatch.setattr(freezone, 'begin_agent_product_operation',
        lambda *a, **kw: pytest.fail('structural preparation cannot admit paid generation'))
    return ctx, store, initial


def confirm(journey):
    ctx, store, initial = journey
    return advance_reference_commission(store, initial['commission_ref'],
        {'type': 'direction_confirmed', 'direction': {'target_goal': 'confirmed direction'}},
        initial['commission_ref']['revision'],
        host_evidence=AuthenticatedDecisionEvidence('p', 'c', 'u', 'user-confirmation'))


async def prepare(ctx, commission, goal, run):
    return await prepare_production_nodes(ctx=ctx, canvas_id='c', agent_id='main',
        request=ProductionNodesRequest(commission_ref=commission['commission_ref'],
                                      goal=goal, run_after_create=run))


def apply_approved(ctx, draft):
    """Transport-only substitute for browser writes; approval and proof stay real."""
    claimed, error = claim_workflow_draft_confirmation(project_dir=ctx.state_dir,
        canvas_id='c', draft_id=draft['draft_id'], revision=draft['revision'])
    assert error is None
    bind_workflow_draft_task(project_dir=ctx.state_dir, canvas_id='c', draft_id=draft['draft_id'],
                            task_id='approved-task', root_task_id='approved-task')
    built = build_workflow_graph_commands({'plan': draft['compiled']['plan'],
        'workflow_instance_id': draft['draft_id'], 'run_after_create': draft['run_after_create']})
    assert built['ok'] and not built['skipped_edges']
    assert {c['type'] for c in built['commands']} <= {
        'create_node', 'create_edge', 'group_nodes', 'layout_nodes', 'select_nodes', 'run_workflow'}
    canvas = canvas_store.read_canvas(ctx.state_dir, 'c')
    results = []
    aliases = {c['client_id']: 'real-' + c['client_id'] for c in built['commands'] if c['type'] == 'create_node'}
    for command in built['commands']:
        if command['type'] == 'create_node':
            node_id = aliases[command['client_id']]
            canvas['nodes'].append({'id': node_id, 'type': command['node_type'],
                                    'data': deepcopy(command['data'])})
            results.append({'type': 'create_node', 'status': 'success', 'createdNodeId': node_id})
        elif command['type'] == 'create_edge':
            canvas['edges'].append({'source': aliases.get(command['source'], command['source']),
                'target': aliases.get(command['target'], command['target']),
                'data': {'link_type': command['link_type']}})
            results.append({'type': 'create_edge', 'status': 'success'})
    canvas_store.save_canvas(ctx.state_dir, 'c', base_revision=canvas['revision'], build_payload=lambda _: {**canvas, 'revision': canvas['revision'] + 1})
    canvas = canvas_store.read_canvas(ctx.state_dir, 'c')
    receipt = {'command_results': results}
    proof = inspect_workflow_graph_delivery(receipt=receipt, draft=draft, canvas=canvas)
    assert proof['state'] == 'delivered'
    for _ in range(2):
        record_workflow_delivery_receipt(project_dir=ctx.state_dir, canvas_id='c',
            draft_id=draft['draft_id'], revision=draft['revision'], task_id='approved-task',
            attempt=claimed['confirmation_started_at'], bridge_key='original-approval', receipt=receipt, proof=proof)
        settle_workflow_draft_delivery(project_dir=ctx.state_dir, canvas_id='c',
            draft_id=draft['draft_id'], revision=draft['revision'], task_id='approved-task',
            attempt=claimed['confirmation_started_at'], proof=proof, canvas=canvas)
    runs = [c for c in built['commands'] if c['type'] == 'run_workflow']
    assert len(runs) == int(draft['run_after_create'])
    if runs:
        assert runs[0]['node_ids'] == ['production_plan_1']
        assert runs[0]['direction'] == 'node'
        run = create_workflow_run(project_dir=ctx.state_dir, project_id='p', canvas_id='c', actor_id='u',
            actions=[{'node_id': aliases['production_plan_1'], 'node_revision': 1,
                'action': 'generate_production_plan_draft',
                'payload': {'topic': draft['intent']['user_goal']}}])
        action = run['actions'][0]
        operations = RemixOperationStore(ctx.output_dir, ctx.project_id)
        operation = operations.start(idempotency_key='native-' + draft['draft_id'], input_refs=[],
            producer_version='journey-provider', node_revision=1, canvas_id='c',
            node_id=action['node_id'], normalized_parameters={
                'action': action['action'], 'payload': action['payload']})
        token = operations.claim_submission(operation['operation_id'], owner='test-provider')
        operations.bind_task(operation['operation_id'], task_id='draft-task',
                            owner='test-provider', fencing_token=token)
        bind_workflow_action_processing_operation(project_dir=ctx.state_dir, canvas_id='c',
            run_id=run['run_id'], node_id=action['node_id'], action=action['action'],
            operation_id=operation['operation_id'])
        update_workflow_run(project_dir=ctx.state_dir, canvas_id='c', run_id=run['run_id'],
            action_updates=[{'node_id': action['node_id'], 'action': action['action'],
                             'status': 'running', 'task_key': 'task:plan'}])
    return canvas


@pytest.mark.asyncio
@pytest.mark.parametrize('run', [True, False])
@pytest.mark.parametrize('goal', [
    '保留访谈对白，人物换到湖边；需要完整问题和回答。',
    '无对白展示：以光影表现材质，整片十一秒，不添加人声。',
    '三段叙事从冲突到解决，时长四十五秒，结尾留白。',
])
async def test_confirmation_to_approved_graph_preserves_goal_and_execution(journey, goal, run):
    ctx, _, _ = journey
    commission = confirm(journey)
    before = canvas_store.read_canvas(ctx.state_dir, 'c')
    draft = await prepare(ctx, commission, goal, run)
    duplicate = await prepare(ctx, commission, goal, run)
    assert duplicate['draft_id'] == draft['draft_id']
    assert canvas_store.read_canvas(ctx.state_dir, 'c') == before
    assert list_workflow_runs(project_dir=ctx.state_dir, canvas_id='c') == []
    assert draft['native_plan_stage']['state'] == 'ready_for_canvas_approval'
    assert draft['compiled']['plan']['external_inputs'][0]['node_id'] == 'analysis'
    assert draft['intent']['user_goal'] == goal
    payload = draft['compiled']['plan']['nodes'][0]['data']['processingPayload']
    requirements = payload.get('requirements') or {}
    assert 'duration_us' not in requirements
    assert 'shot_count' not in requirements
    assert 'voice' not in requirements
    canvas = apply_approved(ctx, draft)
    nodes = [n for n in canvas['nodes'] if n['data'].get('workflowInstanceId') == draft['draft_id']]
    assert len(nodes) == 2
    assert goal in nodes[0]['data']['processingPayload']['topic']
    assert list_workflow_delivery_receipts(project_dir=ctx.state_dir, canvas_id='c',
        draft_id=draft['draft_id'])[0]['graph_state'] == 'delivered'
    assert len(list_workflow_delivery_receipts(project_dir=ctx.state_dir, canvas_id='c', draft_id=draft['draft_id'])) == 1
    retried = await prepare(ctx, commission, goal, run)
    assert retried['draft_id'] == draft['draft_id'] and retried['status'] == 'confirmed'
    assert len(list_workflow_runs(project_dir=ctx.state_dir, canvas_id='c')) == int(run)
    stage = read_native_plan_stage(ctx=ctx, canvas=canvas, commission=commission)
    assert stage['slots'][0]['plan_node_id'] == nodes[0]['id']
    if not run:
        assert stage['slots'][0]['next_action'] == {
            'owner': 'agent', 'kind': 'run_node_action', 'tool': 'freezone_run_node_action',
            'node_id': nodes[0]['id'], 'action': 'generate_production_plan_draft'}
    assert read_native_plan_slot_bindings(project_dir=ctx.state_dir, draft_id=draft['draft_id'])['production_plan_1']['plan_node_id'] == nodes[0]['id']


@pytest.mark.asyncio
async def test_discussion_without_confirmed_direction_never_creates(journey):
    ctx, _, initial = journey
    before = canvas_store.read_canvas(ctx.state_dir, 'c')
    with pytest.raises(HTTPException):
        await prepare(ctx, initial, '能否解释一下这条创意的节奏？', True)
    assert canvas_store.read_canvas(ctx.state_dir, 'c') == before
    assert list_workflow_runs(project_dir=ctx.state_dir, canvas_id='c') == []


@pytest.mark.asyncio
@pytest.mark.parametrize('problem', ['stale', 'missing_analysis', 'wrong_actor'])
async def test_binding_errors_cannot_use_newer_analysis_or_create(journey, problem):
    ctx, _, _ = journey
    commission = confirm(journey)
    if problem == 'stale':
        commission = deepcopy(commission)
        commission['commission_ref']['revision'] -= 1
    elif problem == 'wrong_actor':
        ctx.requester_user_id = 'other'
    else:
        canvas = canvas_store.read_canvas(ctx.state_dir, 'c')
        canvas['nodes'] = [n for n in canvas['nodes'] if n['id'] != 'analysis']
        canvas_store.save_canvas(ctx.state_dir, 'c', base_revision=canvas['revision'], build_payload=lambda _: {**canvas, 'revision': canvas['revision'] + 1})
    before = canvas_store.read_canvas(ctx.state_dir, 'c')
    with pytest.raises(HTTPException):
        await prepare(ctx, commission, '完整创作目标', True)
    assert canvas_store.read_canvas(ctx.state_dir, 'c') == before
    assert list_workflow_runs(project_dir=ctx.state_dir, canvas_id='c') == []


@pytest.mark.asyncio
async def test_generation_failure_keeps_original_nodes_and_one_event_wakeup(journey, tmp_path):
    from novelvideo.chat.reference_remix_continuation import ReferenceRemixStore
    ctx, _, _ = journey
    commission = confirm(journey)
    draft = await prepare(ctx, commission, '无对白展示，保持完整目标', True)
    canvas = apply_approved(ctx, draft)
    run = list_workflow_runs(project_dir=ctx.state_dir, canvas_id='c')[0]
    node_id = read_native_plan_slot_bindings(project_dir=ctx.state_dir, draft_id=draft['draft_id'])['production_plan_1']['plan_node_id']
    update_workflow_run(project_dir=ctx.state_dir, canvas_id='c', run_id=run['run_id'],
        action_updates=[{'node_id': node_id, 'action': 'generate_production_plan_draft',
                         'status': 'failed', 'error': 'provider timeout'}])
    stage = read_native_plan_stage(ctx=ctx, canvas=canvas, commission=commission)
    assert stage['slots'][0]['state'] == 'failed'
    assert stage['slots'][0]['plan_node_id'] == node_id
    assert canvas_store.read_canvas(ctx.state_dir, 'c') == canvas
    assert (await prepare(ctx, commission, '无对白展示，保持完整目标', True))['draft_id'] == draft['draft_id']
    assert len(list_workflow_runs(project_dir=ctx.state_dir, canvas_id='c')) == 1
    continuation = ReferenceRemixStore(tmp_path / 'continuation.sqlite3')
    continuation.create({'run_id': 'continuation', 'project_id': 'p', 'requester_user_id': 'u',
        'canvas_id': 'c', 'agent_id': 'main', 'idempotency_key': 'journey',
        'status': 'drafting_plan', 'commission_ref': commission['commission_ref']})
    event_key = 'production:failed:' + run['run_id']
    assert continuation.wake_waiting(ctx, 'continuation', commission['commission_ref'],
                                    'drafting_plan', event_key=event_key) is not None
    assert continuation.wake_waiting(ctx, 'continuation', commission['commission_ref'],
                                    'drafting_plan', event_key=event_key) is None


@pytest.mark.asyncio
async def test_completed_task_and_saved_plan_reuse_original_node_for_review(journey, monkeypatch):
    import novelvideo.freezone.remix.native_plan_stage as stage_module
    ctx, store, _ = journey
    ctx.is_home_node = True
    commission = confirm(journey)
    goal = '无对白展示，整片十一秒，留出阅读时间'
    draft = await prepare(ctx, commission, goal, True)
    canvas = apply_approved(ctx, draft)
    run = list_workflow_runs(project_dir=ctx.state_dir, canvas_id='c')[0]
    action = run['actions'][0]
    artifact = store.create('production_plan.v2', {
        'schema_version': 2, 'goal': goal,
        'output': {'width': 1920, 'height': 1080, 'fps_num': 24, 'fps_den': 1},
        'beats': [{'id': 'beat', 'purpose': 'display texture', 'duration_frames': 264}],
    }, [])
    ref = {'artifact_id': artifact['id'], 'revision': artifact['revision']}
    operations = RemixOperationStore(ctx.output_dir, ctx.project_id)
    operations.publish_result_unverified(action['processing_operation_id'], output_refs=[ref])
    reconcile_workflow_runs_with_tasks(project_dir=ctx.state_dir, canvas_id='c',
        artifact_project_dir=ctx.output_dir, tasks_by_key={'task:plan': {
            'task_id': 'draft-task', 'status': 'completed', 'result': {'artifact_ref': ref}}})
    monkeypatch.setattr(stage_module, 'get_task_manager', lambda: SimpleNamespace(
        get_task_for_project=lambda *a, **kw: SimpleNamespace(task_id='draft-task', status='completed')))
    # A completed task before canvas publication must still wait for its artifact.
    assert read_native_plan_stage(ctx=ctx, canvas=canvas, commission=commission)['slots'][0]['state'] == 'awaiting_canvas_receipt'
    node = next(n for n in canvas['nodes'] if n['id'] == action['node_id'])
    node['data']['productionPlanV2Ref'] = {'artifactId': ref['artifact_id'], 'revision': ref['revision']}
    canvas_store.save_canvas(ctx.state_dir, 'c', base_revision=canvas['revision'],
        build_payload=lambda _: {**canvas, 'revision': canvas['revision'] + 1})
    final = await prepare(ctx, commission, goal, True)
    slot = final['native_plan_stage']['slots'][0]
    assert slot['state'] == 'ready_for_review'
    assert slot['plan_node_id'] == action['node_id']
    assert slot['next_action']['kind'] == 'review_production_plan'
    assert final['draft_id'] == draft['draft_id']
    assert len(list_workflow_runs(project_dir=ctx.state_dir, canvas_id='c')) == 1
