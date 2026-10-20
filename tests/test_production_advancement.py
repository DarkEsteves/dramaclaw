from copy import deepcopy
import json

import pytest
from PIL import Image

from test_reference_commission import confirmed_plan, selection_canvas, select
from novelvideo.freezone.remix.material_work import prepare_material_work
from novelvideo.freezone.remix.material_execution import review_material_candidate, confirm_material_review
from novelvideo.freezone.remix.store import RemixArtifactStore, RemixArtifactConflict
from novelvideo.project_context import ProjectContext
from novelvideo.freezone import canvas_store


@pytest.mark.parametrize('status', ['stopped', 'running', 'recovering', 'failed', 'completed'])
def test_inactive_or_submitted_work_does_not_auto_advance(status):
    from novelvideo.freezone.remix.production_advancement import select_next_action
    state = {'execution_status': status, 'ready_system_actions': [
        {'owner': 'system', 'kind': 'bind_materials', 'action_id': 'a'}]}
    assert select_next_action(state) is None


def test_missing_audio_review_returns_original_decision():
    from novelvideo.freezone.remix.production_advancement import select_next_action
    review = {'owner': 'user', 'kind': 'review_materials', 'action_id': 'original'}
    assert select_next_action({'execution_status': 'waiting_user', 'blockers': [review]}) == review


def test_system_order_is_deterministic_and_dirty_blocks():
    from novelvideo.freezone.remix.production_advancement import select_next_action
    actions = [{'owner': 'system', 'kind': kind, 'action_id': kind} for kind in
               ('compile_production_plan', 'bind_materials', 'accept_materials', 'register_material')]
    state = {'execution_status': 'idle', 'ready_system_actions': actions}
    assert select_next_action(state)['kind'] == 'register_material'
    state['blockers'] = [{'owner': 'user', 'kind': 'plan_edit_conflict', 'action_id': 'draft'}]
    assert select_next_action(state)['action_id'] == 'draft'


@pytest.fixture
def production(tmp_path, monkeypatch):
    from novelvideo.chat.reference_remix_continuation import coordinator
    monkeypatch.setattr(coordinator.store, 'production_for_actor', lambda *args: [])
    store = RemixArtifactStore(tmp_path, 'p', created_by='u')
    c, plan = confirmed_plan(store)
    payload = deepcopy(plan['payload'])
    payload['placements'] = [{'id': 'picture', 'kind': 'visual', 'role': 'primary', 'track_id': 'main',
                             'start': {'kind': 'beat', 'ref_id': 'b', 'edge': 'start'},
                             'end': {'kind': 'beat', 'ref_id': 'b', 'edge': 'end'}}]
    # Confirm an actual plan containing the saved target, before preparing work.
    plan = store.revise(plan['id'], 1, payload, [])
    from novelvideo.freezone.remix.production_review import review_production_plan, REVIEW_CATEGORIES
    from novelvideo.freezone.remix.reference_commission import advance_reference_commission, AuthenticatedDecisionEvidence
    c = advance_reference_commission(store, c['commission_ref'], {'type': 'plan_ready', 'plan_ref': {'artifact_id': plan['id'], 'revision': 2}}, c['commission_ref']['revision'])
    findings = [{'category': x, 'entity_kind': 'beat', 'entity_id': 'b', 'verdict': 'pass',
                 'reason': 'Concrete content', 'evidence': ['b:intro']} for x in REVIEW_CATEGORIES]
    review = review_production_plan(store, c['plan_ref'], c['commission_ref'], findings)
    c = advance_reference_commission(store, c['commission_ref'], {'type': 'plan_reviewed', 'review_ref': {'artifact_id': review['id'], 'revision': 1}}, c['commission_ref']['revision'])
    c = advance_reference_commission(store, c['commission_ref'], {'type': 'plan_confirmed', 'plan_ref': c['plan_ref']}, c['commission_ref']['revision'], host_evidence=AuthenticatedDecisionEvidence('p', 'c', 'u', 'approved'))
    Image.new('RGB', (16, 12), 'red').save(tmp_path / 'picture.png')
    canvas = {**selection_canvas(plan), 'revision': 1}
    canvas['nodes'].append({'id': 'image', 'type': 'imageNode', 'data': {'imageUrl': '/static/projects/p/picture.png'}})
    canvas['nodes'].append({'id': 'compose', 'type': 'videoComposeNode', 'data': {'productionPlanNodeId': 'plan'}})
    canvas['edges'].append({'source': 'plan', 'target': 'compose'})
    work = prepare_material_work(store, {'artifact_id': plan['id'], 'revision': plan['revision']},
        [{'id': 'r', 'placement_ids': ['picture'], 'media_kind': 'image', 'final_step_id': 'image', 'description': 'picture'}],
        [{'id': 'image', 'label': 'image', 'media_kind': 'image', 'source': 'existing', 'node_id': 'image'}], 'prepare')['work_ref']
    c = select(store, c, work, canvas)
    canvas_store.save_canvas(tmp_path, 'c', base_revision=None, build_payload=lambda _: canvas)
    ctx = ProjectContext('p', 'p', 'user', 'u', 'u', 'u', 'u', (('user', 'u'),), 'owner', 'local', tmp_path, tmp_path, tmp_path, True)
    return ctx, store, c, work, canvas


def current(production, runs=None):
    from novelvideo.freezone.remix.production_inventory import read_current_production
    ctx, store, c, _, _ = production
    canvas = canvas_store.read_canvas(ctx.state_dir, 'c')
    return read_current_production(store, canvas=canvas, canvas_id='c', agent_id='main', requester_user_id='u',
                                   runs=runs or [], state_dir=ctx.state_dir, commission_id=c['commission_ref']['artifact_id'])


@pytest.mark.asyncio
@pytest.mark.parametrize('old_receipt', [False, True])
async def test_waiting_for_plan_completion_does_not_execute_or_reserve_local_action(
    production, monkeypatch, old_receipt,
):
    from novelvideo.freezone.remix import production_advancement as advancement

    ctx, store, commission, _work, _canvas = production
    state = current(production)
    waiting = {
        **state, 'phase': 'drafting_plan', 'plan_ref': None, 'work_ref': None,
        'execution_status': 'idle', 'blockers': [], 'ready_system_actions': [],
        'next_action': {'owner': 'system', 'kind': 'await_completion_event'},
    }
    monkeypatch.setattr(advancement, '_read', lambda *_args, **_kwargs: waiting)
    monkeypatch.setattr(advancement, '_prepare_native_acceptance', lambda *_args, **_kwargs: None)
    command_id = 'await-plan-completion'
    path = advancement._receipt_path(store, 'c', 'main', command_id)
    if old_receipt:
        path.write_text(json.dumps({
            'request': {'commission_ref': commission['commission_ref'],
                        'projection_revision': waiting['projection_revision']},
            'action': waiting['next_action'], 'state': waiting, 'completed': False,
        }))

    result = await advancement.advance_production(
        ctx, canvas_id='c', commission_ref=commission['commission_ref'],
        expected_projection_revision=waiting['projection_revision'], command_id=command_id,
    )

    assert result['advancement'] == {'action': None, 'replayed': old_receipt}
    assert result['next_action']['kind'] == 'await_completion_event'
    assert path.exists() is old_receipt


def passed(production):
    _, store, _, work, canvas = production
    review = review_material_candidate(store, work, 'image', canvas, 'image')
    return confirm_material_review(store, review['review_ref'], canvas, canvas_id='c', requester_user_id='u')


@pytest.mark.asyncio
async def test_one_native_adoption_and_replay_preserve_user_evidence(production):
    from novelvideo.freezone.remix.production_advancement import advance_production
    ctx, store, c, work, _ = production
    review = passed(production)
    state = current(production)
    result = await advance_production(ctx, canvas_id='c', commission_ref=c['commission_ref'],
        expected_projection_revision=state['projection_revision'], command_id='advance-1')
    saved = store.read_latest(work['artifact_id'])
    assert saved['revision'] == work['revision'] + 1
    assert saved['payload']['steps'][0]['review_ref'] == review['review_ref']
    assert result['progress']['materials_accepted'] == 1
    assert store.read_latest(c['plan_ref']['artifact_id'])['revision'] == 2
    replay = await advance_production(ctx, canvas_id='c', commission_ref=c['commission_ref'],
        expected_projection_revision=state['projection_revision'], command_id='advance-1')
    assert store.read_latest(work['artifact_id'])['revision'] == saved['revision']
    assert replay['advancement']['replayed'] is True


@pytest.mark.asyncio
async def test_ee_disabled_continuation_still_allows_manual_native_advance(production, monkeypatch):
    from novelvideo.freezone.remix.production_advancement import advance_production
    from novelvideo.chat.reference_remix_continuation import coordinator

    ctx, store, commission, work, _ = production
    passed(production)
    state = current(production)
    monkeypatch.setenv('ST_CONTROL_PLANE_DSN', 'postgresql://configured')
    monkeypatch.delenv('ST_REFERENCE_REMIX_AUTO_CONTINUATION', raising=False)
    monkeypatch.setattr(coordinator.store, 'production_for_actor',
                        lambda *args: pytest.fail('manual advancement opened continuation state'))
    result = await advance_production(ctx, canvas_id='c',
        commission_ref=commission['commission_ref'],
        expected_projection_revision=state['projection_revision'], command_id='ee-manual-advance')
    assert result['progress']['materials_accepted'] == 1
    assert store.read_latest(work['artifact_id'])['revision'] == work['revision'] + 1


@pytest.mark.asyncio
async def test_unpassed_review_never_auto_confirms(production):
    from novelvideo.freezone.remix.production_advancement import advance_production
    ctx, store, c, work, canvas = production
    review = review_material_candidate(store, work, 'image', canvas, 'image')
    state = current(production)
    result = await advance_production(ctx, canvas_id='c', commission_ref=c['commission_ref'],
        expected_projection_revision=state['projection_revision'], command_id='unpassed')
    assert result['next_action']['action_id'] == review['review_ref']['artifact_id']
    assert store.read_latest(work['artifact_id'])['revision'] == work['revision']
    assert store.read_latest(review['review_ref']['artifact_id'])['payload']['content']['status'] == 'unavailable'


@pytest.mark.asyncio
async def test_stale_projection_and_changed_media_refuse_without_mutation(production):
    from novelvideo.freezone.remix.production_advancement import advance_production
    ctx, store, c, work, _ = production
    passed(production)
    state = current(production)
    Image.new('RGB', (16, 12), 'blue').save(ctx.output_dir / 'picture.png')
    with pytest.raises(RemixArtifactConflict, match='projection'):
        await advance_production(ctx, canvas_id='c', commission_ref=c['commission_ref'],
            expected_projection_revision=state['projection_revision'], command_id='stale')
    assert store.read_latest(work['artifact_id'])['revision'] == 1


@pytest.mark.asyncio
async def test_accepted_final_binds_one_real_batch_and_preserves_plan_approval(production):
    from novelvideo.freezone.remix.production_advancement import advance_production
    ctx, store, c, work, _ = production
    passed(production)
    before = current(production)
    accepted = await advance_production(ctx, canvas_id='c', commission_ref=c['commission_ref'],
        expected_projection_revision=before['projection_revision'], command_id='adopt')
    result = await advance_production(ctx, canvas_id='c', commission_ref=accepted['commission_ref'],
        expected_projection_revision=accepted['projection_revision'], command_id='bind')
    plan = store.read_latest(c['plan_ref']['artifact_id'])
    assert plan['payload']['placements'][0]['resource_id'] == plan['payload']['resources'][0]['id']
    assert result['phase'] == 'preparing_materials'
    assert result['next_action']['kind'] == 'materials_ready'
    canvas = canvas_store.read_canvas(ctx.state_dir, 'c')
    assert canvas['nodes'][1]['data']['productionPlanV2Ref']['revision'] == plan['revision']
    assert any(e['source'] == 'image' and e['target'] == 'plan' for e in canvas['edges'])
    replay = await advance_production(ctx, canvas_id='c', commission_ref=accepted['commission_ref'],
        expected_projection_revision=accepted['projection_revision'], command_id='bind')
    assert replay['advancement']['replayed'] is True
    assert store.read_latest(plan['id'])['revision'] == plan['revision']
    ready = await advance_production(ctx, canvas_id='c', commission_ref=result['commission_ref'],
        expected_projection_revision=result['projection_revision'], command_id='ready')
    assert ready['phase'] == 'aligning_speech'
    silent = await advance_production(ctx, canvas_id='c', commission_ref=ready['commission_ref'],
        expected_projection_revision=ready['projection_revision'], command_id='silent')
    assert silent['phase'] == 'composing'


@pytest.mark.asyncio
async def test_dirty_canvas_is_preserved(production):
    from novelvideo.freezone.remix.production_advancement import advance_production
    ctx, store, c, work, _ = production
    passed(production)
    state = current(production)
    canvas = canvas_store.read_canvas(ctx.state_dir, 'c')
    canvas['nodes'][1]['data']['productionPlanV2Dirty'] = True
    canvas_store.save_canvas(ctx.state_dir, 'c', base_revision=1,
        build_payload=lambda _: {**canvas, 'revision': 2})
    with pytest.raises(RemixArtifactConflict):
        await advance_production(ctx, canvas_id='c', commission_ref=c['commission_ref'],
            expected_projection_revision=state['projection_revision'], command_id='dirty')
    assert canvas_store.read_canvas(ctx.state_dir, 'c')['nodes'][1]['data']['productionPlanV2Dirty'] is True
    assert store.read_latest(work['artifact_id'])['revision'] == 1


async def composing(production):
    from novelvideo.freezone.remix.production_advancement import advance_production
    passed(production)
    ctx = production[0]
    state = current(production)
    for command in ('adopt', 'bind', 'ready', 'silent'):
        state = await advance_production(ctx, canvas_id='c', commission_ref=state['commission_ref'],
            expected_projection_revision=state['projection_revision'], command_id=command)
    assert state['phase'] == 'composing'
    return state


@pytest.mark.asyncio
async def test_compile_uses_native_operation_and_task_and_waits_for_actual_result(production, monkeypatch):
    from types import SimpleNamespace
    from novelvideo.freezone.remix.production_advancement import advance_production
    from novelvideo import ports
    from novelvideo.freezone.remix.operations import RemixOperationStore
    ctx, store, _, _, _ = production
    c = store.read_latest(production[2]['commission_ref']['artifact_id'])
    payload = deepcopy(c['payload'])
    payload['production_scope'] = '预览成片'
    store.revise(c['id'], c['revision'], payload, c['upstream_refs'])
    state = await composing(production)
    queued = []
    class Backend:
        async def enqueue_project_task(self, context, **request):
            assert context is ctx
            queued.append(request)
            return SimpleNamespace(task_state=SimpleNamespace(task_id='native-compile'))
    monkeypatch.setattr(ports, 'get_task_backend', lambda: Backend())
    submitted = await advance_production(ctx, canvas_id='c', commission_ref=state['commission_ref'],
        expected_projection_revision=state['projection_revision'], command_id='compile')
    assert queued[0]['task_type'] == 'remix_compile_production_plan'
    assert queued[0]['payload']['plan_ref'] == {'artifact_id': state['plan_ref']['artifact_id'], 'revision': state['plan_ref']['revision']}
    assert submitted['execution_status'] == 'running'
    assert submitted['next_action'] is None
    again = await advance_production(ctx, canvas_id='c', commission_ref=submitted['commission_ref'],
        expected_projection_revision=submitted['projection_revision'], command_id='other-command')
    assert len(queued) == 1 and again['execution_status'] == 'running'
    operation = RemixOperationStore(ctx.output_dir, ctx.project_id).get(submitted['advancement']['result']['operation_id'])
    assert operation['task_id'] == 'native-compile'
    from novelvideo.task_backend.runners.remix import run_remix_compile_production_plan
    native_result = run_remix_compile_production_plan({'payload': queued[0]['payload']}, ctx)
    from novelvideo import task_state
    task = SimpleNamespace(task_id='native-compile', status='completed', result=native_result)
    monkeypatch.setattr(task_state, 'get_task_manager', lambda: SimpleNamespace(get_task_for_project=lambda *a, **kw: task))
    completed = current(production)
    assert completed['next_action']['kind'] == 'composition_ready'
    bound = await advance_production(ctx, canvas_id='c', commission_ref=completed['commission_ref'],
        expected_projection_revision=completed['projection_revision'], command_id='settle-compile')
    assert bound['phase'] == 'reviewing_output'
    assert RemixOperationStore(ctx.output_dir, ctx.project_id).get(operation['operation_id'])['status'] == 'completed'
    canvas = canvas_store.read_canvas(ctx.state_dir, 'c')
    compose = next(n for n in canvas['nodes'] if n['id'] == 'compose')
    assert compose['data']['remixManifestRef']['artifactId'] == native_result['artifact_ref']['artifact_id']
    assert store.read_latest(state['plan_ref']['artifact_id'])['revision'] == state['plan_ref']['revision']
    assert bound['next_action']['kind'] == 'render_manifest'
    render = await advance_production(ctx, canvas_id='c', commission_ref=bound['commission_ref'],
        expected_projection_revision=bound['projection_revision'], command_id='render')
    assert queued[-1]['task_type'] == 'remix_render'
    assert queued[-1]['payload']['manifest_ref'] == native_result['artifact_ref']
    assert render['execution_status'] == 'running'
    assert render['phase'] == 'reviewing_output'
    from novelvideo.task_backend.runners.remix import run_remix_render
    from novelvideo.freezone.remix.results import confirm_render
    rendered = run_remix_render({'payload': queued[-1]['payload']}, ctx)
    assert (ctx.output_dir / rendered['output_path']).is_file()
    waiting = current(production)
    assert waiting['next_action']['kind'] == 'confirm_operation'
    confirmed = confirm_render(ctx.output_dir, ctx.state_dir, ctx.project_id, render['advancement']['result']['operation_id'], canvas_id='c', node_id='compose')
    assert confirmed['status'] == 'completed'
    reviewed = current(production)
    assert reviewed['phase'] == 'reviewing_output'
    assert reviewed['next_action']['kind'] == 'review_output'
    assert reviewed['next_action']['owner'] == 'agent'


@pytest.mark.parametrize('stage', ['adoption_receipt', 'bind_canvas', 'bind_commission'])
@pytest.mark.asyncio
async def test_partial_native_transition_replays_without_duplicate_publication(production, monkeypatch, stage):
    from novelvideo.freezone.remix import production_advancement as service
    ctx, store, _, work, _ = production
    passed(production)
    state = current(production)
    if stage in {'bind_canvas', 'bind_commission'}:
        state = await service.advance_production(ctx, canvas_id='c', commission_ref=state['commission_ref'],
            expected_projection_revision=state['projection_revision'], command_id='adopt')
        canvas = canvas_store.read_canvas(ctx.state_dir, 'c')
        draft = store.read(state['plan_ref']['artifact_id'], state['plan_ref']['revision'])['payload']
        for node in canvas['nodes']:
            if node['id'] in {'plan', 'compose'}:
                node['data']['productionPlanV2Draft'] = deepcopy(draft)
        canvas_store.save_canvas(ctx.state_dir, 'c', base_revision=canvas['revision'], build_payload=lambda _: canvas)
        state = current(production)
        original = service._canvas_publish
        fired = []
        def crash(*args, **kwargs):
            if not fired:
                fired.append(True)
                if stage == 'bind_commission':
                    original(*args, **kwargs)
                raise RuntimeError('crash after binding artifact')
            return original(*args, **kwargs)
        monkeypatch.setattr(service, '_canvas_publish', crash)
    else:
        original = RemixArtifactStore._atomic_json
        fired = []
        def crash(self, path, value, guard):
            if path.parent.name == '_production_advancement' and value.get('completed') and not fired:
                fired.append(True)
                raise RuntimeError('crash after adoption')
            return original(self, path, value, guard)
        monkeypatch.setattr(RemixArtifactStore, '_atomic_json', crash)
    with pytest.raises(RuntimeError, match='crash'):
        await service.advance_production(ctx, canvas_id='c', commission_ref=state['commission_ref'],
            expected_projection_revision=state['projection_revision'], command_id='recover')
    work_revision = store.read_latest(work['artifact_id'])['revision']
    plan_revision = store.read_latest(state['plan_ref']['artifact_id'])['revision']
    result = await service.advance_production(ctx, canvas_id='c', commission_ref=state['commission_ref'],
        expected_projection_revision=state['projection_revision'], command_id='recover')
    assert store.read_latest(work['artifact_id'])['revision'] == work_revision
    assert store.read_latest(state['plan_ref']['artifact_id'])['revision'] == plan_revision
    assert result['progress']['materials_accepted'] == 1


@pytest.mark.asyncio
async def test_route_uses_host_context_and_shared_projection(production, monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from novelvideo.api.auth import get_api_user
    from novelvideo.api.routes import reference_director as route
    ctx, store, c, work, canvas = production
    passed(production)
    async def context(*args, **kwargs): return ctx, canvas, store
    async def emit(*args, **kwargs): pass
    monkeypatch.setattr(route, '_context', context)
    monkeypatch.setattr(route, '_emit_director_update', emit)
    app = FastAPI()
    app.include_router(route.router, prefix='/api/v1')
    app.dependency_overrides[get_api_user] = lambda: {'user_id': 'u', 'username': 'u'}
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        url = '/api/v1/projects/p/freezone/canvases/c/director'
        response = await client.get(url + '/production-state', params={'commission_id': c['commission_ref']['artifact_id']})
        state = response.json()['data']
        assert state['next_action']['owner'] == 'system'
        response = await client.post(url + '/advance-production', json={
            'commission_ref': state['commission_ref'], 'expected_projection_revision': state['projection_revision'],
            'command_id': 'route', 'agent_id': 'main'})
        assert response.status_code == 200, response.text
        assert response.json()['data']['progress']['materials_accepted'] == 1
        assert store.read_latest(work['artifact_id'])['revision'] == 2
        rejected = await client.post(url + '/advance-production', json={
            'commission_ref': state['commission_ref'], 'expected_projection_revision': state['projection_revision'],
            'command_id': 'another', 'agent_id': 'main', 'ctx': {'requester_user_id': 'u'}})
        assert rejected.status_code == 422


@pytest.mark.asyncio
async def test_clean_flag_with_changed_draft_still_protects_user_edit(production):
    from novelvideo.freezone.remix.production_advancement import advance_production
    ctx, store, c, work, _ = production
    passed(production)
    canvas = canvas_store.read_canvas(ctx.state_dir, 'c')
    draft = deepcopy(store.read_latest(c['plan_ref']['artifact_id'])['payload'])
    draft['goal'] = 'unsaved creative change'
    canvas['nodes'][1]['data']['productionPlanV2Draft'] = draft
    canvas_store.save_canvas(ctx.state_dir, 'c', base_revision=canvas['revision'],
        build_payload=lambda _: {**canvas, 'revision': canvas['revision'] + 1})
    state = current(production)
    result = await advance_production(ctx, canvas_id='c', commission_ref=state['commission_ref'],
        expected_projection_revision=state['projection_revision'], command_id='protect-draft')
    assert result['next_action']['kind'] == 'plan_edit_conflict'
    assert store.read_latest(work['artifact_id'])['revision'] == 1
    assert canvas_store.read_canvas(ctx.state_dir, 'c')['nodes'][1]['data']['productionPlanV2Draft'] == draft


def test_current_basis_pass_does_not_bypass_revoked_review_head(production):
    from novelvideo.freezone.remix.material_work import accept_material_candidate
    _, store, _, work, _ = production
    confirmed = passed(production)
    saved = store.read_latest(confirmed['review_ref']['artifact_id'])
    payload = deepcopy(saved['payload'])
    payload['content']['status'] = 'rejected'
    store.revise(saved['id'], saved['revision'], payload, saved['upstream_refs'])
    with pytest.raises(ValueError, match='current|head'):
        accept_material_candidate(store, work, 'image', confirmed['review']['resource'],
            confirmed['review_ref'], 'old-pass', canvas_id='c')
    assert store.read_latest(work['artifact_id'])['revision'] == 1


async def completed_native_source_adoption(production, monkeypatch, *, before_worker=None):
    import wave
    from types import SimpleNamespace
    from novelvideo.freezone.remix.production_advancement import enqueue_source_observation_adoption
    from novelvideo.freezone.remix.source_speech import observe_source_speech, candidate_scope
    from novelvideo.freezone.remix.source_speech_adoption import prepare_source_observation_adoption
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.freezone.remix.reference_commission import advance_reference_commission, AuthenticatedDecisionEvidence
    from novelvideo.freezone.remix.production_review import review_production_plan, REVIEW_CATEGORIES
    from novelvideo.task_backend.runners.remix import run_remix_adopt_source_speech
    from novelvideo import task_state
    ctx, store, commission, _, canvas = production
    path = ctx.output_dir / 'voice.wav'
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b'\0\0' * 32800)
    from novelvideo.freezone.remix.material_execution import canvas_material_resource
    canvas['nodes'].append({'id': 'voice', 'type': 'audioNode', 'data': {'audioUrl': '/static/projects/p/voice.wav'}})
    media = canvas_material_resource(store, canvas, 'voice', 'audio')
    current_plan = store.read_latest(commission['plan_ref']['artifact_id'])
    payload = deepcopy(current_plan['payload'])
    payload['beats'][0]['timing_mode'] = 'auto'
    payload['beats'][0]['turns'] = [{'id': 'one', 'speaker': 'A', 'text': 'one'}, {'id': 'two', 'speaker': 'B', 'text': 'two'}]
    payload['resources'].append(media)
    payload['placements'].append({'id': 'speech', 'kind': 'audio', 'role': 'speech', 'track_id': 'speech',
        'resource_id': media['id'], 'start': {'kind': 'beat', 'ref_id': 'b', 'edge': 'start'},
        'end': {'kind': 'beat', 'ref_id': 'b', 'edge': 'end'}})
    payload['speech_takes'] = [{'id': 'take', 'beat_id': 'b', 'origin_placement_id': 'speech',
                              'resource_id': media['id'], 'turn_ids': ['one', 'two']}]
    plan = store.revise(current_plan['id'], current_plan['revision'], payload, current_plan['upstream_refs'])
    plan_ref = {'artifact_id': plan['id'], 'revision': plan['revision']}
    commission = advance_reference_commission(store, commission['commission_ref'], {'type': 'plan_ready', 'plan_ref': plan_ref}, commission['commission_ref']['revision'])
    findings = [{'category': category, 'entity_kind': 'beat', 'entity_id': 'b', 'verdict': 'pass',
                 'reason': 'Reviewed saved dialogue', 'evidence': ['b:one two']} for category in REVIEW_CATEGORIES]
    review = review_production_plan(store, commission['plan_ref'], commission['commission_ref'], findings)
    commission = advance_reference_commission(store, commission['commission_ref'], {'type': 'plan_reviewed', 'review_ref': {'artifact_id': review['id'], 'revision': 1}}, commission['commission_ref']['revision'])
    commission = advance_reference_commission(store, commission['commission_ref'], {'type': 'plan_confirmed', 'plan_ref': commission['plan_ref']}, commission['commission_ref']['revision'], host_evidence=AuthenticatedDecisionEvidence('p', 'c', 'u', 'dialogue-approved'))
    canvas['nodes'][1]['data']['productionPlanV2Ref']['revision'] = plan['revision']
    work = prepare_material_work(store, plan_ref,
        [{'id': 'voice', 'take_ids': ['take'], 'turn_ids': ['one', 'two'], 'media_kind': 'audio', 'final_step_id': 'voice', 'description': 'Dialogue'}],
        [{'id': 'voice', 'label': 'Voice', 'source': 'existing', 'media_kind': 'audio', 'node_id': 'voice'}], 'voice-work')['work_ref']
    commission = select(store, commission, work, canvas, 'select-voice')
    review = review_material_candidate(store, work, 'voice', canvas, 'voice')
    review = confirm_material_review(store, review['review_ref'], canvas, canvas_id='c', requester_user_id='u')
    from novelvideo.freezone.remix.material_work import accept_material_candidate
    work = accept_material_candidate(store, work, 'voice', media, review['review_ref'], 'voice-accept', canvas_id='c')['work_ref']
    commission = advance_reference_commission(store, commission['commission_ref'], {'type': 'materials_ready', 'work_ref': work}, commission['commission_ref']['revision'])
    canvas_store.save_canvas(ctx.state_dir, 'c', base_revision=1, build_payload=lambda _: {**canvas, 'revision': 2})
    scope = candidate_scope(ctx, canvas, 'c')
    class Engine:
        def transcribe(self, *args):
            return {'status': 'completed', 'transcript': 'one two', 'segments': [],
                    'words': [{'text': 'one', 'start': .1, 'end': 1.0}, {'text': 'two', 'start': 3.2, 'end': 4.1}]}
    observation = observe_source_speech(store, scope=scope, resource=media, language='en', provider_signature='test', aligner=Engine())
    prepared = prepare_source_observation_adoption(store, ctx, canvas, canvas_id='c', plan_ref=plan_ref,
        take_id='take', observation_ref=observation, work_ref=work)
    operations = RemixOperationStore(ctx.output_dir, ctx.project_id)
    manager = task_state.TaskStateManager()
    monkeypatch.setattr(task_state, 'get_task_manager', lambda: manager)
    from novelvideo import ports
    queued = []
    class Backend:
        async def enqueue_project_task(self, context, **request):
            assert context is ctx
            queued.append(request)
            task = manager.create_task_for_project(ctx, request['task_type'], request['episode'],
                scope=request['scope'], queue_kind=request['queue_kind'])
            return SimpleNamespace(task_state=task)
    monkeypatch.setattr(ports, 'get_task_backend', lambda: Backend())
    operation = await enqueue_source_observation_adoption(ctx, store, prepared, command_id='source-adopt', node_revision=plan_ref['revision'])
    assert queued[0]['task_type'] == 'remix_adopt_source_speech'
    assert operations.get(operation['operation_id'])['normalized_parameters'] == prepared['normalized_parameters']
    if before_worker is not None:
        before_worker(ctx, store, commission, plan_ref, operation)
    result = run_remix_adopt_source_speech({'__run_task_id': operation['task_id'], 'payload': {**prepared['task_payload'], 'operation_id': operation['operation_id']}}, ctx)
    assert manager.complete_task_for_project(ctx, 'remix_adopt_source_speech', 0,
        scope=operation['operation_id'], result=result, expected_task_id=operation['task_id'])
    return ctx, store, commission, plan_ref, operation, result, review


@pytest.mark.asyncio
async def test_native_source_adoption_result_updates_canvas_and_commission_once(production, monkeypatch):
    from novelvideo.freezone.remix.production_advancement import settle_source_adoption
    ctx, store, commission, plan_ref, operation, _, _ = await completed_native_source_adoption(production, monkeypatch)
    settled = await settle_source_adoption(ctx, canvas_id='c', commission_ref=commission['commission_ref'], operation_id=operation['operation_id'])
    assert settled['plan_ref']['revision'] == plan_ref['revision'] + 1
    assert settled['phase'] == 'aligning_speech'
    assert settled['next_action']['kind'] == 'speech_aligned'
    canvas_revision = canvas_store.read_canvas(ctx.state_dir, 'c')['revision']
    assert canvas_store.read_canvas(ctx.state_dir, 'c')['nodes'][1]['data']['productionPlanV2Ref']['revision'] == settled['plan_ref']['revision']
    assert (await settle_source_adoption(ctx, canvas_id='c', commission_ref=commission['commission_ref'], operation_id=operation['operation_id']))['plan_ref'] == settled['plan_ref']
    assert canvas_store.read_canvas(ctx.state_dir, 'c')['revision'] == canvas_revision


@pytest.mark.asyncio
async def test_host_stopped_state_cannot_accept_or_wake_native_work(production, monkeypatch):
    from novelvideo.freezone.remix.production_advancement import advance_production
    from novelvideo.chat.reference_remix_continuation import coordinator
    from novelvideo import ports
    ctx, store, c, work, _ = production
    passed(production)
    run = {'project_id': 'p', 'canvas_id': 'c', 'agent_id': 'main', 'requester_user_id': 'u',
           'commission_ref': c['commission_ref'], 'status': 'stopped', 'run_id': 'stopped'}
    monkeypatch.setattr(coordinator.store, 'production_for_actor', lambda *args: [run])
    def unexpected_backend():
        raise AssertionError('stopped production must not wake a native task')
    monkeypatch.setattr(ports, 'get_task_backend', unexpected_backend)
    state = current(production, [run])
    result = await advance_production(ctx, canvas_id='c', commission_ref=state['commission_ref'],
        expected_projection_revision=state['projection_revision'], command_id='stopped')
    assert result['execution_status'] == 'stopped' and result['next_action'] is None
    assert store.read_latest(work['artifact_id'])['revision'] == 1
    assert store.read_latest(c['commission_ref']['artifact_id'])['revision'] == c['commission_ref']['revision']


@pytest.mark.asyncio
async def test_rejects_model_dictionary_context_before_any_storage(production):
    from novelvideo.freezone.remix.production_advancement import advance_production
    _, store, c, work, _ = production
    with pytest.raises(PermissionError, match='ProjectContext'):
        await advance_production({'project_id': 'p', 'requester_user_id': 'u'}, canvas_id='c',
            commission_ref=c['commission_ref'], expected_projection_revision='0' * 64, command_id='fake-context')
    assert store.read_latest(work['artifact_id'])['revision'] == 1


@pytest.mark.parametrize('edit', ['script_draft', 'script_conflict', 'script_ref',
                                  'compose_draft', 'compose_dirty', 'compose_conflict', 'compose_ref'])
@pytest.mark.asyncio
async def test_partial_binding_replay_preserves_intervening_canvas_edits(production, monkeypatch, edit):
    from novelvideo.freezone.remix import production_advancement as service
    ctx, store, _, work, _ = production
    passed(production)
    state = current(production)
    state = await service.advance_production(ctx, canvas_id='c', commission_ref=state['commission_ref'],
        expected_projection_revision=state['projection_revision'], command_id='adopt')
    original = service._canvas_publish
    def crash(*args, **kwargs):
        raise RuntimeError('crash after binding artifact')
    monkeypatch.setattr(service, '_canvas_publish', crash)
    with pytest.raises(RuntimeError, match='crash'):
        await service.advance_production(ctx, canvas_id='c', commission_ref=state['commission_ref'],
            expected_projection_revision=state['projection_revision'], command_id='recover')
    monkeypatch.setattr(service, '_canvas_publish', original)
    canvas = canvas_store.read_canvas(ctx.state_dir, 'c')
    target = next(n for n in canvas['nodes'] if n['id'] == ('compose' if edit.startswith('compose') else 'plan'))
    if edit.endswith('draft'):
        draft = deepcopy(store.read(state['plan_ref']['artifact_id'], state['plan_ref']['revision'])['payload'])
        draft['goal'] = 'new user edit after interrupted binding'
        target['data']['productionPlanV2Draft'] = draft
        target['data']['productionPlanV2Dirty'] = False
    elif edit.endswith('ref'):
        target['data']['productionPlanV2Ref'] = {'artifactId': state['plan_ref']['artifact_id'], 'revision': 1}
    else:
        target['data']['productionPlanV2' + ('Dirty' if edit.endswith('dirty') else 'Conflict')] = True
    canvas_store.save_canvas(ctx.state_dir, 'c', base_revision=canvas['revision'], build_payload=lambda _: canvas)
    before = canvas_store.read_canvas(ctx.state_dir, 'c')
    heads = {ref['artifact_id']: store.read_latest(ref['artifact_id']) for ref in
             (state['plan_ref'], state['commission_ref'], work)}
    with pytest.raises(RemixArtifactConflict, match='draft|canvas|plan|compose'):
        await service.advance_production(ctx, canvas_id='c', commission_ref=state['commission_ref'],
            expected_projection_revision=state['projection_revision'], command_id='recover')
    assert canvas_store.read_canvas(ctx.state_dir, 'c') == before
    assert {artifact_id: store.read_latest(artifact_id) for artifact_id in heads} == heads


@pytest.mark.parametrize('edit', ['url', 'file', 'review', 'plan'])
@pytest.mark.asyncio
async def test_source_settlement_revalidates_current_media_before_publication(production, monkeypatch, edit):
    import wave
    from novelvideo.freezone.remix.production_advancement import settle_source_adoption
    from novelvideo.freezone.remix.operations import RemixOperationStore
    ctx, store, commission, _, operation, result, review = await completed_native_source_adoption(production, monkeypatch)
    if edit in {'url', 'file'}:
        filename = 'replacement.wav' if edit == 'url' else 'voice.wav'
        with wave.open(str(ctx.output_dir / filename), 'wb') as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8000)
            output.writeframes(b'\x01\x00' * 32800)
        if edit == 'url':
            canvas = canvas_store.read_canvas(ctx.state_dir, 'c')
            next(n for n in canvas['nodes'] if n['id'] == 'voice')['data']['audioUrl'] = '/static/projects/p/replacement.wav'
            canvas_store.save_canvas(ctx.state_dir, 'c', base_revision=canvas['revision'], build_payload=lambda _: canvas)
    elif edit == 'review':
        saved = store.read_latest(review['review_ref']['artifact_id'])
        payload = deepcopy(saved['payload'])
        payload['content']['status'] = 'rejected'
        store.revise(saved['id'], saved['revision'], payload, saved['upstream_refs'])
    else:
        saved = store.read_latest(result['plan_ref']['artifact_id'])
        payload = deepcopy(saved['payload'])
        payload['goal'] = 'new plan after source worker'
        store.revise(saved['id'], saved['revision'], payload, saved['upstream_refs'])
    before = canvas_store.read_canvas(ctx.state_dir, 'c')
    operations = RemixOperationStore(ctx.output_dir, ctx.project_id)
    native_before = operations.get(operation['operation_id'])
    commission_before = store.read_latest(commission['commission_ref']['artifact_id'])
    plan_before = store.read_latest(result['plan_ref']['artifact_id'])
    with pytest.raises(ValueError, match='current|changed|hash|review|head'):
        await settle_source_adoption(ctx, canvas_id='c', commission_ref=commission['commission_ref'], operation_id=operation['operation_id'])
    assert canvas_store.read_canvas(ctx.state_dir, 'c') == before
    assert operations.get(operation['operation_id']) == native_before
    assert store.read_latest(commission['commission_ref']['artifact_id']) == commission_before
    assert store.read_latest(result['plan_ref']['artifact_id']) == plan_before


@pytest.mark.asyncio
async def test_source_worker_rejects_media_replaced_before_execution(production, monkeypatch):
    import wave
    from novelvideo.freezone.remix.operations import RemixOperationStore
    before = {}
    def replace(ctx, store, commission, plan_ref, operation):
        with wave.open(str(ctx.output_dir / 'replacement.wav'), 'wb') as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8000)
            output.writeframes(b'\x01\x00' * 32800)
        canvas = canvas_store.read_canvas(ctx.state_dir, 'c')
        next(n for n in canvas['nodes'] if n['id'] == 'voice')['data']['audioUrl'] = '/static/projects/p/replacement.wav'
        canvas_store.save_canvas(ctx.state_dir, 'c', base_revision=canvas['revision'], build_payload=lambda _: canvas)
        before.update(ctx=ctx, store=store, canvas=canvas_store.read_canvas(ctx.state_dir, 'c'),
                      commission=store.read_latest(commission['commission_ref']['artifact_id']),
                      plan=store.read_latest(plan_ref['artifact_id']), operation=operation)
    with pytest.raises(ValueError, match='current|changed|resource|media'):
        await completed_native_source_adoption(production, monkeypatch, before_worker=replace)
    assert canvas_store.read_canvas(before['ctx'].state_dir, 'c') == before['canvas']
    assert before['store'].read_latest(before['commission']['id']) == before['commission']
    assert before['store'].read_latest(before['plan']['id']) == before['plan']
    operation = RemixOperationStore(before['ctx'].output_dir, before['ctx'].project_id).get(before['operation']['operation_id'])
    assert operation['status'] != 'completed' and operation['output_refs'] == []


@pytest.mark.asyncio
async def test_partial_binding_replay_rejects_unowned_commission_before_canvas_write(production, monkeypatch):
    from novelvideo.freezone.remix import production_advancement as service
    ctx, store, _, _, _ = production
    passed(production)
    state = current(production)
    state = await service.advance_production(ctx, canvas_id='c', commission_ref=state['commission_ref'],
        expected_projection_revision=state['projection_revision'], command_id='adopt')
    original = service._canvas_publish
    def crash(*args, **kwargs):
        raise RuntimeError('crash after binding artifact')
    monkeypatch.setattr(service, '_canvas_publish', crash)
    with pytest.raises(RuntimeError, match='crash'):
        await service.advance_production(ctx, canvas_id='c', commission_ref=state['commission_ref'],
            expected_projection_revision=state['projection_revision'], command_id='recover')
    monkeypatch.setattr(service, '_canvas_publish', original)
    commission = store.read_latest(state['commission_ref']['artifact_id'])
    payload = deepcopy(commission['payload'])
    payload['production_scope'] = '导出成片'
    bound = store.read_latest(state['plan_ref']['artifact_id'])
    payload['plan_ref'] = {'artifact_id': bound['id'], 'revision': bound['revision'], 'project_id': 'p'}
    refs = store._reference_list([*commission['upstream_refs'], payload['plan_ref']], 'p')
    store.revise(commission['id'], commission['revision'], payload, refs)
    canvas_before = canvas_store.read_canvas(ctx.state_dir, 'c')
    with pytest.raises(ValueError):
        await service.advance_production(ctx, canvas_id='c', commission_ref=state['commission_ref'],
            expected_projection_revision=state['projection_revision'], command_id='recover')
    assert canvas_store.read_canvas(ctx.state_dir, 'c') == canvas_before


@pytest.mark.asyncio
async def test_binding_checks_compose_edits_before_saving_plan(production):
    from novelvideo.freezone.remix.production_advancement import advance_production
    ctx, store, _, _, _ = production
    passed(production)
    state = current(production)
    state = await advance_production(ctx, canvas_id='c', commission_ref=state['commission_ref'],
        expected_projection_revision=state['projection_revision'], command_id='adopt')
    canvas = canvas_store.read_canvas(ctx.state_dir, 'c')
    next(n for n in canvas['nodes'] if n['id'] == 'compose')['data']['productionPlanV2Dirty'] = True
    canvas_store.save_canvas(ctx.state_dir, 'c', base_revision=canvas['revision'], build_payload=lambda _: canvas)
    state = current(production)
    plan_before = store.read_latest(state['plan_ref']['artifact_id'])
    with pytest.raises(RemixArtifactConflict, match='compose|draft'):
        await advance_production(ctx, canvas_id='c', commission_ref=state['commission_ref'],
            expected_projection_revision=state['projection_revision'], command_id='bind-dirty-compose')
    assert store.read_latest(state['plan_ref']['artifact_id']) == plan_before


def test_pending_compatible_review_exposes_current_work_and_complete_display_checks(production):
    from novelvideo.freezone.remix.material_work import accept_material_candidate
    ctx, store, c, original, canvas = production
    canvas['nodes'].append({'id': 'other', 'type': 'imageNode', 'data': {'imageUrl': '/static/projects/p/picture.png'}})
    work = prepare_material_work(store, {key: c['plan_ref'][key] for key in ('artifact_id', 'revision')},
        [{'id': 'r', 'placement_ids': ['picture'], 'description': 'image', 'media_kind': 'image', 'final_step_id': 'image'},
         {'id': 'other', 'description': 'reference', 'media_kind': 'image', 'final_step_id': 'other'}],
        [{'id': 'image', 'label': 'image', 'media_kind': 'image', 'source': 'existing', 'node_id': 'image'},
         {'id': 'other', 'label': 'other', 'media_kind': 'image', 'source': 'existing', 'node_id': 'other'}], 'two-items')['work_ref']
    c = select(store, c, work, canvas, 'two-items-select')
    first = review_material_candidate(store, work, 'image', canvas, 'image')
    second = review_material_candidate(store, work, 'other', canvas, 'other')
    first = confirm_material_review(store, first['review_ref'], canvas, canvas_id='c', requester_user_id='u')
    accepted = accept_material_candidate(store, work, 'image', first['review']['resource'], first['review_ref'], 'one-only', canvas_id='c')['work_ref']
    saved_canvas = canvas_store.read_canvas(ctx.state_dir, 'c')
    canvas_store.save_canvas(ctx.state_dir, 'c', base_revision=saved_canvas['revision'], build_payload=lambda _: canvas)
    state = current((ctx, store, c, accepted, canvas))
    pending = next(row for row in state['director_state']['material_reviews'] if row['review_ref'] == second['review_ref'])
    assert pending['work_ref'] == work
    assert pending['current_work_ref'] == accepted
    assert isinstance(pending['technical']['checks'], list)
    assert isinstance(pending['technical']['findings'], list)
    assert isinstance(pending['content']['checks'], list)
    assert isinstance(pending['content']['findings'], list)
