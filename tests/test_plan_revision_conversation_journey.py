"""Saved revisions supersede old conversation proposals and survive restarts."""
# ruff: noqa: F401, F811
from copy import deepcopy
from types import SimpleNamespace

import pytest

from test_plan_confirmation_contract import reviewed_plan
from test_reference_director_api import setup_api
from novelvideo.chat.plan_confirmation_delivery import metadata_for_delivered_plan, prepare_plan_presentation_target
from novelvideo.chat.reference_production_observer import ReferenceProductionObserver, production_event
from novelvideo.chat.reference_remix_continuation import ReferenceRemixCoordinator, ReferenceRemixStore
from novelvideo.chat.store import ChatScope, ChatStore
from novelvideo.freezone import canvas_store
from novelvideo.freezone.remix.production_commands import apply_production_command
from novelvideo.freezone.remix.reference_commission import read_reference_commission


def _journey(reviewed_plan, monkeypatch, *, phase='awaiting_plan_confirmation', turn_id=None):
    from novelvideo.api.routes import freezone
    _, artifacts, commission, _ = reviewed_plan
    ctx = SimpleNamespace(project_id='p', requester_user_id='u', requester_username='alice',
                          output_dir=artifacts.project_dir, state_dir=artifacts.project_dir)
    db = ReferenceRemixStore(artifacts.project_dir / 'revision-runs.db')
    run = dict(run_id='revision-run', project_id='p', requester_user_id='u', username='alice',
               canvas_id='c', analysis_node_id='analysis', agent_id='main', idempotency_key='revision',
               request_fingerprint='revision', status=phase, commission_phase=phase, production_watch_only=True,
               commission_ref=commission['commission_ref'], commission_event_receipts=[])
    if turn_id:
        run['resume_turn_id'] = turn_id
    db.create(run)
    calls = []
    class Coordinator(ReferenceRemixCoordinator):
        async def _resolve_context(self, run): return ctx
        def _assert_current(self, *args, **kwargs): return canvas_store.read_canvas(ctx.state_dir, 'c')
        def scope(self, run):
            return ChatScope(kind="project", id="p", surface="freezone", canvas_id="c",
                             agent_id="main", state_dir=str(ctx.state_dir))
        async def continue_commission(self, *, ctx, run_id, event_key, wake_event=None):
            current = read_reference_commission(artifacts, commission['commission_ref'], latest=True)
            updated = db.wake_waiting(ctx, run_id, current['commission_ref'], current['phase'], event_key=event_key)
            if updated:
                calls.append(event_key)
            return updated
    coordinator = Coordinator(db)
    _save_plan_ref(ctx.state_dir, commission['plan_ref'])
    target = prepare_plan_presentation_target(artifacts, commission['commission_ref'], agent_id='main')
    ChatStore().append_message('alice', coordinator.scope(run), 'assistant', '已审核旧版作品方案，请确认。',
        metadata=metadata_for_delivered_plan(artifacts, target, assistant_delivered=True,
                                            message='已审核旧版作品方案，请确认。'),
        idempotency_key='old-plan-presentation')
    async def no_resume(*args, **kwargs): return None
    monkeypatch.setattr(freezone, 'resume_host_owned_native_workflows', no_resume)
    async def no_tasks(*args): return {}
    monkeypatch.setattr(ReferenceProductionObserver, '_material_snapshot', no_tasks)
    return ctx, db, coordinator, calls


def _save_plan_ref(state_dir, ref):
    canvas = canvas_store.read_canvas(state_dir, 'c')
    for node in canvas['nodes']:
        saved = node.get('data', {}).get('productionPlanV2Ref')
        if saved and saved['artifactId'] == ref['artifact_id']:
            saved['revision'] = ref['revision']
    canvas_store.save_canvas(state_dir, 'c', base_revision=canvas['revision'], build_payload=lambda _: canvas)


def _edit(artifacts, plan_ref, *, command_id='saved-edit'):
    result = apply_production_command(artifacts, {
        'plan_ref': {key: plan_ref[key] for key in ('artifact_id', 'revision')},
        'command_id': command_id, 'kind': 'edit_beat', 'target_id': 'intro',
        'payload': {'purpose': '展示产品新功能'},
    })
    ref = {**result['plan_ref'], 'project_id': 'p'}
    _save_plan_ref(artifacts.project_dir, ref)
    return ref


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['awaiting_plan_confirmation', 'awaiting_plan_review'])
async def test_saved_revision_is_received_before_old_presentation_recovery(reviewed_plan, monkeypatch, phase):
    _, artifacts, original, _ = reviewed_plan
    ctx, db, coordinator, calls = _journey(reviewed_plan, monkeypatch, phase=phase)
    if phase == 'awaiting_plan_review':
        # A prior revision is still in review when another local edit is saved.
        ref = _edit(artifacts, original['plan_ref'], command_id='first-edit')
        from novelvideo.freezone.remix.plan_revision_settlement import reconcile_plan_revision
        original = reconcile_plan_revision(artifacts, commission_ref=original['commission_ref'], plan_ref=ref)['commission']
    saved_ref = _edit(artifacts, original['plan_ref'])
    before_canvas = canvas_store.read_canvas(ctx.state_dir, 'c')
    observer = ReferenceProductionObserver(coordinator)
    assert await observer.observe_run(db.get('revision-run'))
    current = read_reference_commission(artifacts, original['commission_ref'], latest=True)
    assert current['phase'] == 'awaiting_plan_review' and current['plan_ref'] == saved_ref
    assert current.get('review_ref') is None
    assert current['direction'] == original['direction']
    assert current['analysis_evidence'] == original['analysis_evidence']
    assert canvas_store.read_canvas(ctx.state_dir, 'c') == before_canvas
    assert len(calls) == 1
    notices = [row for row in ChatStore().list_messages('alice', coordinator.scope(db.get('revision-run')))
               if row['role'] == 'system']
    assert len(notices) == 1
    assert saved_ref['artifact_id'] in notices[0]['content']
    assert str(saved_ref['revision']) in notices[0]['content']
    assert not await observer.observe_run(db.get('revision-run'))
    assert not await ReferenceProductionObserver(coordinator).observe_run(db.get('revision-run'))
    assert len(calls) == 1
    assert read_reference_commission(artifacts, current['commission_ref'], latest=True) == current


@pytest.mark.asyncio
@pytest.mark.parametrize('interrupt_at', ['saved_plan', 'commission', 'notice'])
async def test_saved_revision_restart_recovers_one_transition_and_notification(reviewed_plan, monkeypatch, interrupt_at):
    _, artifacts, original, _ = reviewed_plan
    _, db, coordinator, calls = _journey(reviewed_plan, monkeypatch)
    saved_ref = _edit(artifacts, original['plan_ref'])
    observer = ReferenceProductionObserver(coordinator)
    if interrupt_at == 'commission':
        original_advance = observer._advance_observed
        def stop_after_cas(*args, **kwargs):
            original_advance(*args, **kwargs)
            raise RuntimeError('interrupted after commission save')
        monkeypatch.setattr(observer, '_advance_observed', stop_after_cas)
    if interrupt_at == 'notice':
        original_notify = coordinator._notify
        async def stop_after_notice(*args):
            await original_notify(*args)
            raise RuntimeError('interrupted after notice save')
        monkeypatch.setattr(coordinator, '_notify', stop_after_notice)
    if interrupt_at != 'saved_plan':
        with pytest.raises(RuntimeError, match='interrupted'):
            await observer.observe_run(db.get('revision-run'))
    prior = read_reference_commission(artifacts, original['commission_ref'], latest=True)
    if interrupt_at == 'notice':
        monkeypatch.setattr(coordinator, '_notify', original_notify)
    assert await ReferenceProductionObserver(coordinator).observe_run(db.get('revision-run'))
    current = read_reference_commission(artifacts, original['commission_ref'], latest=True)
    assert current['commission_ref']['revision'] == original['commission_ref']['revision'] + 1
    if interrupt_at != 'saved_plan':
        assert current == prior
    assert current['plan_ref'] == saved_ref
    assert len(calls) == 1
    assert len([row for row in ChatStore().list_messages('alice', coordinator.scope(db.get('revision-run')))
                if row['role'] == 'system']) == 1
    assert not await ReferenceProductionObserver(coordinator).observe_run(db.get('revision-run'))
    assert artifacts.read_latest(saved_ref['artifact_id'])['revision'] == saved_ref['revision']


@pytest.mark.asyncio
@pytest.mark.parametrize('interrupt_at', ['before_cas', 'before_cas_competing_commit', 'after_cas', None])
async def test_material_refinement_receipt_preserves_approved_ref_and_dedupes(reviewed_plan, monkeypatch, interrupt_at):
    from novelvideo.chat.reference_decision_ingress import apply_user_confirmation, pending_user_decision
    from novelvideo.freezone.remix import plan_revision_settlement
    _, artifacts, original, _ = reviewed_plan
    ctx, db, coordinator, calls = _journey(reviewed_plan, monkeypatch, phase='preparing_materials')
    chat = ChatStore()
    user = chat.append_message('alice', coordinator.scope(db.get('revision-run')), 'user', '按这个方案继续。')
    pending = pending_user_decision(artifacts, scope=coordinator.scope(db.get('revision-run')),
        requester_user_id='u', credential_kind='browser_session', message=user)
    assert pending
    original = apply_user_confirmation(artifacts, scope=coordinator.scope(db.get('revision-run')),
        requester_user_id='u', credential_kind='browser_session', message=user,
        interpretation={'intent': 'approve'}, expected_decision_ref=pending['decision_ref'])['commission']
    plan = artifacts.read_latest(original['plan_ref']['artifact_id'])
    payload = deepcopy(plan['payload'])
    payload['beats'][0]['duration_frames'] = 96
    saved = artifacts.revise(plan['id'], plan['revision'], payload, plan['upstream_refs'])
    ref = {'artifact_id': saved['id'], 'revision': saved['revision'], 'project_id': 'p'}
    _save_plan_ref(ctx.state_dir, ref)
    observer = ReferenceProductionObserver(coordinator)
    if interrupt_at in {'before_cas', 'before_cas_competing_commit'}:
        revise = artifacts.__class__.revise
        def fail_cas(self, artifact_id, *args, **kwargs):
            if artifact_id == original['commission_ref']['artifact_id']:
                raise RuntimeError('interrupted before commission CAS')
            return revise(self, artifact_id, *args, **kwargs)
        monkeypatch.setattr(artifacts.__class__, 'revise', fail_cas)
    if interrupt_at == 'after_cas':
        receive = plan_revision_settlement.reconcile_plan_revision
        def fail_after_receive(*args, **kwargs):
            receive(*args, **kwargs)
            raise RuntimeError('interrupted after commission CAS')
        monkeypatch.setattr(plan_revision_settlement, 'reconcile_plan_revision', fail_after_receive)
    if interrupt_at:
        with pytest.raises(RuntimeError, match='interrupted'):
            await observer.observe_run(db.get('revision-run'))
        if interrupt_at in {'before_cas', 'before_cas_competing_commit'}:
            monkeypatch.setattr(artifacts.__class__, 'revise', revise)
            if interrupt_at == 'before_cas_competing_commit':
                from novelvideo.freezone.remix.material_work import prepare_material_work
                from novelvideo.freezone.remix.reference_commission import advance_reference_commission
                work = prepare_material_work(artifacts, {key: ref[key] for key in ('artifact_id', 'revision')},
                    [{'id': 'product', 'placement_ids': ['primary'], 'description': '产品图',
                      'media_kind': 'image', 'final_step_id': 'photo'}],
                    [{'id': 'photo', 'label': '产品图', 'media_kind': 'image', 'source': 'generate',
                      'recipe_id': 'general-image', 'prompt': '产品图'}], 'concurrent-material-work')
                competing = advance_reference_commission(artifacts, original['commission_ref'],
                    {'type': 'materials_invalidated', 'work_ref': work['work_ref']},
                    original['commission_ref']['revision'])
        else:
            monkeypatch.setattr(plan_revision_settlement, 'reconcile_plan_revision', receive)
    assert await ReferenceProductionObserver(coordinator).observe_run(db.get('revision-run'))
    current = read_reference_commission(artifacts, original['commission_ref'], latest=True)
    assert current['phase'] == 'preparing_materials'
    assert current['plan_ref'] == original['plan_ref']
    expected_commits = 2 if interrupt_at == 'before_cas_competing_commit' else 1
    assert current['commission_ref']['revision'] == original['commission_ref']['revision'] + expected_commits
    if interrupt_at == 'before_cas_competing_commit':
        assert current['work_ref'] == competing['work_ref']
    assert current['review_ref'] == original['review_ref']
    assert len(calls) == 1
    assert not await ReferenceProductionObserver(coordinator).observe_run(db.get('revision-run'))
    assert read_reference_commission(artifacts, current['commission_ref'], latest=True) == current
    event = production_event(artifacts, canvas_store.read_canvas(artifacts.project_dir, 'c'), current)
    assert event['event']['plan_ref'] == ref


def test_native_command_wakes_only_after_saved_success(reviewed_plan, monkeypatch):
    from novelvideo.chat import reference_production_observer as module
    _, artifacts, original, _ = reviewed_plan
    wakes = []
    monkeypatch.setattr(module, 'wake_local_observers', lambda: wakes.append(artifacts.read_latest(original['plan_ref']['artifact_id'])['revision']))
    saved_ref = _edit(artifacts, original['plan_ref'])
    assert wakes == [saved_ref['revision']]
    # Durable command replay is already saved; no new success hint is needed.
    assert _edit(artifacts, original['plan_ref']) == saved_ref
    assert wakes == [saved_ref['revision']]
    with pytest.raises(ValueError):
        _edit(artifacts, original['plan_ref'], command_id='stale-edit')
    assert wakes == [saved_ref['revision']]


def test_artifact_revision_route_posts_hint_after_real_plan_save(reviewed_plan, monkeypatch):
    from novelvideo.chat import reference_production_observer as module
    client, artifacts, original, _ = reviewed_plan
    saved = artifacts.read_latest(original['plan_ref']['artifact_id'])
    payload = deepcopy(saved['payload'])
    payload['beats'][0]['purpose'] = '新的人工画布修改'
    wakes = []
    monkeypatch.setattr(module, 'wake_local_observers', lambda: wakes.append(artifacts.read_latest(saved['id'])['revision']))
    path = '/api/v1/projects/p/freezone/remix/artifacts/' + saved['id']
    body = {'expected_revision': saved['revision'], 'payload': payload, 'upstream_refs': saved['upstream_refs']}
    response = client.put(path, json=body)
    assert response.status_code == 200, response.text
    assert wakes == [saved['revision'] + 1]
    assert client.put(path, json=body).status_code == 409
    assert wakes == [saved['revision'] + 1]


@pytest.mark.asyncio
@pytest.mark.parametrize('binding_change', ['other_analysis', 'unapproved_workflow'])
async def test_latest_saved_revision_requires_current_bound_canvas_branch(reviewed_plan, monkeypatch, binding_change):
    _, artifacts, original, _ = reviewed_plan
    ctx, db, coordinator, calls = _journey(reviewed_plan, monkeypatch)
    _edit(artifacts, original['plan_ref'])
    canvas = canvas_store.read_canvas(ctx.state_dir, 'c')
    node = next(row for row in canvas['nodes'] if (row.get('data') or {}).get('productionPlanV2Ref'))
    if binding_change == 'other_analysis':
        canvas['edges'] = []
    else:
        node['data'].update(workflowInstanceId='unapproved-draft', workflowPlanNodeId='unapproved-plan')
    canvas_store.save_canvas(ctx.state_dir, 'c', base_revision=canvas['revision'], build_payload=lambda _: canvas)
    with pytest.raises(ValueError, match='current reviewed plan required'):
        await ReferenceProductionObserver(coordinator).observe_run(db.get('revision-run'))
    assert not calls
    assert read_reference_commission(artifacts, original['commission_ref'], latest=True) == original
    assert not (artifacts.root / '_plan_revision_observations').exists()


@pytest.mark.asyncio
async def test_consumed_material_refinement_allows_subsequent_material_observation(reviewed_plan, monkeypatch):
    from PIL import Image
    from novelvideo.chat.reference_decision_ingress import apply_user_confirmation, pending_user_decision
    from novelvideo.freezone.remix.material_work import prepare_material_work
    _, artifacts, original, _ = reviewed_plan
    ctx, db, coordinator, calls = _journey(reviewed_plan, monkeypatch, phase='preparing_materials')
    scope = coordinator.scope(db.get('revision-run'))
    user = ChatStore().append_message('alice', scope, 'user', '照这个方案继续。')
    pending = pending_user_decision(artifacts, scope=scope, requester_user_id='u',
                                   credential_kind='browser_session', message=user)
    approved = apply_user_confirmation(artifacts, scope=scope, requester_user_id='u',
        credential_kind='browser_session', message=user, interpretation={'intent': 'approve'},
        expected_decision_ref=pending['decision_ref'])['commission']
    plan = artifacts.read_latest(approved['plan_ref']['artifact_id'])
    payload = deepcopy(plan['payload'])
    payload['beats'][0]['duration_frames'] = 96
    saved = artifacts.revise(plan['id'], plan['revision'], payload, plan['upstream_refs'])
    _save_plan_ref(ctx.state_dir, {'artifact_id': saved['id'], 'revision': saved['revision']})
    assert await ReferenceProductionObserver(coordinator).observe_run(db.get('revision-run'))
    received = read_reference_commission(artifacts, approved['commission_ref'], latest=True)
    assert received['plan_ref'] == approved['plan_ref']
    assert len(calls) == 1
    revision_event = calls[0]

    Image.new('RGB', (16, 12), 'red').save(ctx.output_dir / 'new-material.png')
    canvas = canvas_store.read_canvas(ctx.state_dir, 'c')
    canvas['nodes'].append({'id': 'photo', 'type': 'uploadNode', 'data': {
        'imageUrl': '/static/projects/p/new-material.png'}})
    canvas_store.save_canvas(ctx.state_dir, 'c', base_revision=canvas['revision'], build_payload=lambda _: canvas)
    prepare_material_work(artifacts, {'artifact_id': saved['id'], 'revision': saved['revision']},
        [{'id': 'product', 'placement_ids': ['primary'], 'description': '产品图',
          'media_kind': 'image', 'final_step_id': 'photo'}],
        [{'id': 'photo', 'label': '产品图', 'media_kind': 'image', 'source': 'existing', 'node_id': 'photo'}],
        'new-material-after-refinement', canvas=canvas)
    assert await ReferenceProductionObserver(coordinator).observe_run(db.get('revision-run'))
    assert len(calls) == 2 and calls[-1] != revision_event
    assert read_reference_commission(artifacts, received['commission_ref'], latest=True) == received
    assert len([row for row in ChatStore().list_messages('alice', scope) if row['role'] == 'system']) == 1
    assert not await ReferenceProductionObserver(coordinator).observe_run(db.get('revision-run'))


@pytest.mark.asyncio
@pytest.mark.parametrize('interrupt_before_continue', [False, True, 'mapping_write', 'historical_mapping_missing'])
async def test_group_consumed_refinement_allows_child_material_progress(reviewed_plan, monkeypatch, interrupt_before_continue):
    from PIL import Image
    from novelvideo.freezone.remix.reference_branches import prepare_reference_branches
    from novelvideo.freezone.remix.reference_commission import advance_reference_commission, AuthenticatedDecisionEvidence
    from novelvideo.freezone.remix.plan_review_settlement import submit_plan_review
    from novelvideo.freezone.remix.material_work import prepare_material_work
    _, artifacts, original, _ = reviewed_plan
    ctx, db, coordinator, calls = _journey(reviewed_plan, monkeypatch, phase='awaiting_plan_review')
    original_plan = artifacts.read_latest(original['plan_ref']['artifact_id'])
    sibling_plan = artifacts.create('production_plan.v2', original_plan['payload'], [])
    group = prepare_reference_branches(artifacts, original['commission_ref'],
        [original['plan_ref'], {'artifact_id': sibling_plan['id'], 'revision': sibling_plan['revision']}],
        'material-progress-group')
    child, sibling = group['branches']
    findings = artifacts.read(original['review_ref']['artifact_id'], original['review_ref']['revision'])['payload']['findings']
    reviewed = submit_plan_review(artifacts, commission_ref=child['commission_ref'],
        plan_ref=child['plan_ref'], findings=findings)['commission']
    approved = advance_reference_commission(artifacts, reviewed['commission_ref'],
        {'type': 'plan_confirmed', 'plan_ref': reviewed['plan_ref']}, reviewed['commission_ref']['revision'],
        host_evidence=AuthenticatedDecisionEvidence('p', 'c', 'u', 'real-group-plan-approval'))
    canvas = canvas_store.read_canvas(ctx.state_dir, 'c')
    canvas['nodes'].append({'id': 'sibling-plan', 'type': 'scriptNode', 'data': {
        'remixRole': 'production_plan', 'productionPlanV2Ref': {
            'artifactId': sibling_plan['id'], 'revision': sibling_plan['revision']}}})
    canvas['edges'].append({'source': 'analysis', 'target': 'sibling-plan'})
    canvas_store.save_canvas(ctx.state_dir, 'c', base_revision=canvas['revision'], build_payload=lambda _: canvas)
    payload = deepcopy(original_plan['payload'])
    payload['beats'][0]['duration_frames'] = 96
    refined = artifacts.revise(original_plan['id'], original_plan['revision'], payload, original_plan['upstream_refs'])
    _save_plan_ref(ctx.state_dir, {'artifact_id': refined['id'], 'revision': refined['revision']})
    if interrupt_before_continue is True:
        continue_commission = coordinator.continue_commission
        async def stop_after_notification(**kwargs):
            raise RuntimeError('interrupted before aggregate consumption')
        monkeypatch.setattr(coordinator, 'continue_commission', stop_after_notification)
        with pytest.raises(RuntimeError, match='aggregate consumption'):
            await ReferenceProductionObserver(coordinator).observe_run(db.get('revision-run'))
        assert not db.get('revision-run')['commission_event_receipts']
        monkeypatch.setattr(coordinator, 'continue_commission', continue_commission)
    if interrupt_before_continue == 'mapping_write':
        atomic_json = artifacts.__class__._atomic_json
        def fail_mapping(self, path, *args, **kwargs):
            if path.name.startswith('group-'):
                raise OSError('aggregate mapping write failed')
            return atomic_json(self, path, *args, **kwargs)
        monkeypatch.setattr(artifacts.__class__, '_atomic_json', fail_mapping)
        with pytest.raises(OSError, match='mapping write failed'):
            await ReferenceProductionObserver(coordinator).observe_run(db.get('revision-run'))
        assert not db.get('revision-run')['commission_event_receipts']
        assert not calls
        assert not [row for row in ChatStore().list_messages('alice', coordinator.scope(db.get('revision-run')))
                    if row['role'] == 'system']
        monkeypatch.setattr(artifacts.__class__, '_atomic_json', atomic_json)
    assert await ReferenceProductionObserver(coordinator).observe_run(db.get('revision-run'))
    received = read_reference_commission(artifacts, child['commission_ref'], latest=True)
    assert received['plan_ref'] == approved['plan_ref']
    assert received['commission_ref']['revision'] == approved['commission_ref']['revision'] + 1
    assert len(calls) == 1
    aggregate_revision_event = calls[0]
    if interrupt_before_continue == 'historical_mapping_missing':
        maps = list((artifacts.root / '_plan_revision_observations').glob('group-*.json'))
        assert len(maps) == 1
        maps[0].unlink()

    Image.new('RGB', (16, 12), 'red').save(ctx.output_dir / 'group-material.png')
    canvas = canvas_store.read_canvas(ctx.state_dir, 'c')
    canvas['nodes'].append({'id': 'group-photo', 'type': 'uploadNode', 'data': {
        'imageUrl': '/static/projects/p/group-material.png'}})
    canvas_store.save_canvas(ctx.state_dir, 'c', base_revision=canvas['revision'], build_payload=lambda _: canvas)
    prepare_material_work(artifacts, {'artifact_id': refined['id'], 'revision': refined['revision']},
        [{'id': 'product', 'placement_ids': ['primary'], 'description': '产品图',
          'media_kind': 'image', 'final_step_id': 'group-photo'}],
        [{'id': 'group-photo', 'label': '产品图', 'media_kind': 'image', 'source': 'existing', 'node_id': 'group-photo'}],
        'group-material-after-refinement', canvas=canvas)
    if interrupt_before_continue == 'historical_mapping_missing':
        # The exact old aggregate receipt can recover its missing map first.
        assert not await ReferenceProductionObserver(coordinator).observe_run(db.get('revision-run'))
    assert await ReferenceProductionObserver(coordinator).observe_run(db.get('revision-run'))
    assert len(calls) == 2 and calls[-1] != aggregate_revision_event
    assert read_reference_commission(artifacts, received['commission_ref'], latest=True) == received
    scope = coordinator.scope(db.get('revision-run'))
    assert len([row for row in ChatStore().list_messages('alice', scope) if row['role'] == 'system']) == 1
    assert not await ReferenceProductionObserver(coordinator).observe_run(db.get('revision-run'))
    assert read_reference_commission(artifacts, sibling['commission_ref'], latest=True)['phase'] == 'awaiting_plan_review'


def _successful_edit_bridge(artifacts, original, monkeypatch, *, turn_id='editing-turn', key='edit-bridge', edit_payload=None):
    from novelvideo.freezone import canvas_command_bridge as bridge
    command = {'command_id': key, 'plan_ref': {name: original['plan_ref'][name] for name in ('artifact_id', 'revision')},
               'kind': 'edit_beat', 'target_id': 'intro', 'payload': edit_payload or {'purpose': key}}
    commands = [{'type': 'production_plan_command', 'node_id': 'plan', 'command': command}]
    for name, value in {'USERNAME': 'alice', 'PROJECT_ID': 'p', 'CANVAS_ID': 'c', 'AGENT_ID': 'main', 'TURN_ID': turn_id}.items():
        monkeypatch.setenv('DRAMACLAW_' + name, value)
    directory = artifacts.project_dir / 'bridge'
    bridge.put_pending_canvas_command(key=key, project_id='p', canvas_id='c', commands=commands,
                                      envelope={'commands': commands}, bridge_dir=directory)
    output = apply_production_command(artifacts, command)
    bridge.resolve_canvas_command(key, {'ok': True, 'applied': True, 'cancelled': False,
        'tool_call_status': 'completed', 'canvas_apply_status': 'applied', 'errors': [],
        'command_results': [{'type': 'production_plan_command', 'status': 'success', 'nodeId': 'plan', 'output': output}]}, bridge_dir=directory)
    return directory, {**output['plan_ref'], 'project_id': 'p'}


def _save_edit_canvas(ctx, ref):
    canvas = canvas_store.read_canvas(ctx.state_dir, 'c')
    node = next(node for node in canvas['nodes'] if node['data'].get('productionPlanV2Ref'))
    node['id'] = 'plan'
    for edge in canvas['edges']:
        if edge['source'] == 'analysis':
            edge['target'] = 'plan'
    node['data']['productionPlanV2Ref'] = {'artifactId': ref['artifact_id'], 'revision': ref['revision']}
    canvas_store.save_canvas(ctx.state_dir, 'c', base_revision=canvas['revision'], build_payload=lambda _: canvas)


@pytest.mark.asyncio
@pytest.mark.parametrize('saved_canvas', [True, False])
async def test_same_turn_bridge_receives_revision_without_new_agent_turn(reviewed_plan, monkeypatch, saved_canvas):
    from novelvideo.freezone.remix.plan_revision_settlement import receive_canvas_plan_revisions
    _, artifacts, original, _ = reviewed_plan
    ctx, db, coordinator, calls = _journey(reviewed_plan, monkeypatch, turn_id='editing-turn')
    run = db.get('revision-run')
    directory, ref = _successful_edit_bridge(artifacts, original, monkeypatch)
    if saved_canvas:
        _save_edit_canvas(ctx, ref)
    result = receive_canvas_plan_revisions(ctx, bridge_key='edit-bridge', bridge_dir=directory,
                                         username='alice', agent_id='main', turn_id='editing-turn')
    assert result[0]['plan_ref'] == ref
    assert result[0]['status'] == ('synced' if saved_canvas else 'pending_canvas_save')
    current = read_reference_commission(artifacts, original['commission_ref'], latest=True)
    assert current['plan_ref'] == (ref if saved_canvas else original['plan_ref'])
    if not saved_canvas:
        _save_edit_canvas(ctx, ref)
    await ReferenceProductionObserver(coordinator).observe_run(db.get('revision-run'))
    current = read_reference_commission(artifacts, original['commission_ref'], latest=True)
    assert current['plan_ref'] == ref and current['phase'] == 'awaiting_plan_review'
    assert current.get('review_ref') is None
    assert not calls
    assert db.get('revision-run')['resume_turn_id'] == 'editing-turn'
    assert not await ReferenceProductionObserver(coordinator).observe_run(db.get('revision-run'))
    assert not [row for row in ChatStore().list_messages('alice', coordinator.scope(run)) if row['role'] == 'system']


@pytest.mark.asyncio
async def test_other_turn_bridge_keeps_revision_notification(reviewed_plan, monkeypatch):
    from novelvideo.freezone.remix.plan_revision_settlement import receive_canvas_plan_revisions
    _, artifacts, original, _ = reviewed_plan
    ctx, db, coordinator, calls = _journey(reviewed_plan, monkeypatch)
    directory, ref = _successful_edit_bridge(artifacts, original, monkeypatch, turn_id='other-turn')
    _save_edit_canvas(ctx, ref)
    receive_canvas_plan_revisions(ctx, bridge_key='edit-bridge', bridge_dir=directory,
                                 username='alice', agent_id='main', turn_id='other-turn')
    assert await ReferenceProductionObserver(coordinator).observe_run(db.get('revision-run'))
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_edit_receipt_waits_for_persisted_canvas_before_observation(reviewed_plan, monkeypatch):
    from novelvideo.freezone.remix.plan_revision_settlement import receive_canvas_plan_revisions
    _, artifacts, original, _ = reviewed_plan
    ctx, db, coordinator, calls = _journey(reviewed_plan, monkeypatch, turn_id='editing-turn')
    directory, ref = _successful_edit_bridge(artifacts, original, monkeypatch)
    receive_canvas_plan_revisions(ctx, bridge_key='edit-bridge', bridge_dir=directory,
                                 username='alice', agent_id='main', turn_id='editing-turn')
    assert not await ReferenceProductionObserver(coordinator).observe_run(db.get('revision-run'))
    assert read_reference_commission(artifacts, original['commission_ref'], latest=True)['plan_ref'] == original['plan_ref']
    assert not calls
    _save_edit_canvas(ctx, ref)
    await ReferenceProductionObserver(coordinator).observe_run(db.get('revision-run'))
    assert read_reference_commission(artifacts, original['commission_ref'], latest=True)['plan_ref'] == ref
    assert not calls


@pytest.mark.asyncio
async def test_bridge_receipt_recovers_after_crash_before_revision_index(reviewed_plan, monkeypatch):
    from novelvideo.chat.hermes_pool import canvas_bridge_dir_for_profile
    _, artifacts, original, _ = reviewed_plan
    ctx, db, coordinator, calls = _journey(reviewed_plan, monkeypatch, turn_id='editing-turn')
    directory, ref = _successful_edit_bridge(artifacts, original, monkeypatch)
    runtime_directory = canvas_bridge_dir_for_profile(ctx.state_dir / 'agents/hermes/freezone', 'freezone:main')
    runtime_directory.parent.mkdir(parents=True, exist_ok=True)
    directory.rename(runtime_directory)
    _save_edit_canvas(ctx, ref)
    await ReferenceProductionObserver(coordinator).observe_run(db.get('revision-run'))
    assert read_reference_commission(artifacts, original['commission_ref'], latest=True)['plan_ref'] == ref
    assert not calls
    assert db.get('revision-run')['resume_turn_id'] == 'editing-turn'


@pytest.mark.asyncio
async def test_second_same_turn_edit_and_interrupted_resume_stay_in_current_turn(reviewed_plan, monkeypatch):
    from novelvideo.freezone.remix.plan_revision_settlement import receive_canvas_plan_revisions
    _, artifacts, original, _ = reviewed_plan
    ctx, db, coordinator, calls = _journey(reviewed_plan, monkeypatch, turn_id='editing-turn')
    for number in range(2):
        directory, ref = _successful_edit_bridge(artifacts, original, monkeypatch, key=f'edit-{number}')
        _save_edit_canvas(ctx, ref)
        receive_canvas_plan_revisions(ctx, bridge_key=f'edit-{number}', bridge_dir=directory,
                                     username='alice', agent_id='main', turn_id='editing-turn')
        if number == 1:
            with db._connect() as connection:
                connection.execute("UPDATE reference_remix_continuations SET status='resume_failed',payload_json=json_set(payload_json,'$.status','resume_failed','$.error','interrupted') WHERE run_id='revision-run'")
            assert db.waiting_for_production()[0]['status'] == 'resume_failed'
        await ReferenceProductionObserver(coordinator).observe_run(db.get('revision-run'))
        original = read_reference_commission(artifacts, original['commission_ref'], latest=True)
        assert original['plan_ref'] == ref
        assert not calls
    assert db.get('revision-run')['status'] == 'resume_failed'
    assert db.get('revision-run')['error'] == 'interrupted'
    assert db.get('revision-run')['resume_turn_id'] == 'editing-turn'


@pytest.mark.asyncio
async def test_http_edit_receipt_returns_current_plan_and_sync_state(reviewed_plan, monkeypatch):
    from novelvideo.api.routes import chat as routes
    from novelvideo.freezone.canvas_command_bridge import read_bridge_message
    _, artifacts, original, _ = reviewed_plan
    ctx, _, _, _ = _journey(reviewed_plan, monkeypatch, turn_id='editing-turn')
    directory, ref = _successful_edit_bridge(artifacts, original, monkeypatch)
    _save_edit_canvas(ctx, ref)
    async def state(*args):
        return ctx.state_dir
    async def context(*args):
        return ctx
    monkeypatch.setattr(routes, '_bridge_project_state_dir', state)
    monkeypatch.setattr(routes, '_project_context_for_scope', context)
    monkeypatch.setattr(routes, '_bridge_dir_for_pending_key', lambda *args, **kwargs: directory)
    result = read_bridge_message('edit-bridge', bridge_dir=directory)['result']
    payload = routes.CanvasCommandToolResultIn(**{**result, 'bridge_key': 'edit-bridge',
        'project_id': 'p', 'canvas_id': 'c', 'turn_id': 'editing-turn', 'agent_id': 'main'})
    response = await routes.resolve_canvas_command_tool_result(payload, {'username': 'alice', 'user_id': 'u'})
    assert response['data']['plan_ref'] == ref
    assert response['data']['plan_revision_sync'] == [{'status': 'synced', 'plan_ref': ref}]
    assert read_bridge_message('edit-bridge', bridge_dir=directory)['result'] == response['data']
    assert read_reference_commission(artifacts, original['commission_ref'], latest=True)['plan_ref'] == ref


@pytest.mark.parametrize('interrupt_at', [None, 'before_cas', 'after_cas'])
def test_timing_refinement_bridge_receipt_is_received_once(reviewed_plan, monkeypatch, interrupt_at):
    from test_plan_revision_settlement import _confirm
    from novelvideo.freezone.remix.plan_revision_settlement import receive_canvas_plan_revisions
    _, artifacts, original, _ = reviewed_plan
    ctx, _, _, _ = _journey(reviewed_plan, monkeypatch, turn_id='editing-turn')
    original = _confirm(artifacts, original)
    directory, ref = _successful_edit_bridge(artifacts, original, monkeypatch, edit_payload={'duration_frames': 96})
    _save_edit_canvas(ctx, ref)
    def receive():
        return receive_canvas_plan_revisions(ctx, bridge_key='edit-bridge', bridge_dir=directory,
                                            username='alice', agent_id='main', turn_id='editing-turn')[0]
    if interrupt_at:
        revise = type(artifacts).revise
        def interrupted(self, artifact_id, *args, **kwargs):
            if artifact_id != original['commission_ref']['artifact_id']:
                return revise(self, artifact_id, *args, **kwargs)
            if interrupt_at == 'after_cas':
                revise(self, artifact_id, *args, **kwargs)
            raise RuntimeError('interrupted revision receipt')
        monkeypatch.setattr(type(artifacts), 'revise', interrupted)
        with pytest.raises(RuntimeError, match='interrupted revision receipt'):
            receive()
        monkeypatch.setattr(type(artifacts), 'revise', revise)
    first = receive()
    assert first['status'] == 'synced'
    assert first['commission']['commission_ref']['revision'] == original['commission_ref']['revision'] + 1
    assert first['commission']['phase'] == 'preparing_materials'
    assert first['commission']['plan_ref'] == original['plan_ref']
    assert receive()['commission'] == first['commission']


@pytest.mark.parametrize('receipt_kind', ['failed', 'cancelled', 'forged_origin'])
def test_unsuccessful_or_untrusted_bridge_never_receives_plan_revision(reviewed_plan, monkeypatch, receipt_kind):
    from novelvideo.freezone import canvas_command_bridge as bridge
    from novelvideo.freezone.remix.plan_revision_settlement import receive_canvas_plan_revisions
    _, artifacts, original, _ = reviewed_plan
    ctx, _, _, _ = _journey(reviewed_plan, monkeypatch, turn_id='editing-turn')
    directory = ctx.state_dir / 'failed-bridge'
    for name, value in {'USERNAME': 'alice', 'PROJECT_ID': 'p', 'CANVAS_ID': 'c', 'AGENT_ID': 'main', 'TURN_ID': 'editing-turn'}.items():
        monkeypatch.setenv('DRAMACLAW_' + name, value)
    if receipt_kind == 'forged_origin':
        monkeypatch.delenv('DRAMACLAW_TURN_ID')
    command = {'type': 'production_plan_command', 'node_id': 'plan', 'origin': 'agent',
        'command': {'command_id': 'never-saved', 'plan_ref': original['plan_ref'], 'kind': 'edit_beat',
                    'target_id': 'intro', 'payload': {'purpose': 'never-saved'}}}
    bridge.put_pending_canvas_command(key='unsuccessful', project_id='p', canvas_id='c', commands=[command],
                                      envelope={'commands': [command]}, bridge_dir=directory)
    bridge.resolve_canvas_command('unsuccessful', {
        'ok': receipt_kind == 'forged_origin', 'applied': receipt_kind == 'forged_origin',
        'canvas_apply_status': 'applied' if receipt_kind == 'forged_origin' else 'failed',
        'cancelled': receipt_kind == 'cancelled',
    }, bridge_dir=directory)
    assert receive_canvas_plan_revisions(ctx, bridge_key='unsuccessful', bridge_dir=directory,
        username='alice', agent_id='main', turn_id='editing-turn') == []
    assert read_reference_commission(artifacts, original['commission_ref'], latest=True) == original
    assert artifacts.read_latest(original['plan_ref']['artifact_id'])['revision'] == original['plan_ref']['revision']
    assert not (artifacts.root / '_plan_revision_bridge_receipts').exists()


@pytest.mark.asyncio
async def test_reviewed_same_turn_receipt_recovery_does_not_restart_interrupted_presentation(reviewed_plan, monkeypatch):
    from novelvideo.freezone.remix.plan_revision_settlement import receive_canvas_plan_revisions
    from novelvideo.freezone.remix.plan_review_settlement import submit_plan_review
    _, artifacts, original, _ = reviewed_plan
    ctx, db, coordinator, calls = _journey(reviewed_plan, monkeypatch, turn_id='editing-turn')
    directory, ref = _successful_edit_bridge(artifacts, original, monkeypatch)
    _save_edit_canvas(ctx, ref)
    received = receive_canvas_plan_revisions(ctx, bridge_key='edit-bridge', bridge_dir=directory,
        username='alice', agent_id='main', turn_id='editing-turn')[0]['commission']
    await ReferenceProductionObserver(coordinator).observe_run(db.get('revision-run'))
    findings = artifacts.read(original['review_ref']['artifact_id'], original['review_ref']['revision'])['payload']['findings']
    reviewed = submit_plan_review(artifacts, commission_ref=received['commission_ref'], plan_ref=ref, findings=findings)['commission']
    assert reviewed['phase'] == 'awaiting_plan_confirmation'
    with db._connect() as connection:
        connection.execute("UPDATE reference_remix_continuations SET status='resume_failed',payload_json=json_set(payload_json,'$.status','resume_failed','$.error','interrupted before presentation') WHERE run_id='revision-run'")
    assert not await ReferenceProductionObserver(coordinator).observe_run(db.get('revision-run'))
    assert not calls
    run = db.get('revision-run')
    assert run['status'] == 'resume_failed'
    assert run['error'] == 'interrupted before presentation'
    assert run['resume_turn_id'] == 'editing-turn'
    assert run['commission_ref'] == reviewed['commission_ref']
    assert not any(key.startswith('plan-presentation:') for key in run['commission_event_receipts'])


@pytest.mark.asyncio
async def test_same_turn_outline_edit_consumes_actual_outline_observation(reviewed_plan, monkeypatch):
    from novelvideo.freezone.remix.plan_revision_settlement import receive_canvas_plan_revisions, reconcile_plan_revision
    _, artifacts, original, _ = reviewed_plan
    ctx, db, coordinator, calls = _journey(reviewed_plan, monkeypatch, turn_id='editing-turn')
    saved = artifacts.read_latest(original['plan_ref']['artifact_id'])
    payload = deepcopy(saved['payload'])
    payload['authoring_stage'] = 'outline'
    payload['placements'] = []
    outline = artifacts.revise(saved['id'], saved['revision'], payload, saved['upstream_refs'])
    original = reconcile_plan_revision(artifacts, commission_ref=original['commission_ref'],
        plan_ref={'artifact_id': outline['id'], 'revision': outline['revision'], 'project_id': 'p'})['commission']
    directory, ref = _successful_edit_bridge(artifacts, original, monkeypatch)
    _save_edit_canvas(ctx, ref)
    receive_canvas_plan_revisions(ctx, bridge_key='edit-bridge', bridge_dir=directory,
        username='alice', agent_id='main', turn_id='editing-turn')
    current = read_reference_commission(artifacts, original['commission_ref'], latest=True)
    actual = production_event(artifacts, canvas_store.read_canvas(ctx.state_dir, 'c'), current)
    assert actual['authoring_stage'] == 'outline'
    assert not await ReferenceProductionObserver(coordinator).observe_run(db.get('revision-run'))
    assert not calls
    run = db.get('revision-run')
    assert actual['event_key'] in run['commission_event_receipts']
    assert run['resume_turn_id'] == 'editing-turn'
    assert run['commission_ref'] == current['commission_ref']
    assert not await ReferenceProductionObserver(coordinator).observe_run(run)
    assert not [row for row in ChatStore().list_messages('alice', coordinator.scope(run)) if row['role'] == 'system']
