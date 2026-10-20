import json

import pytest
from PIL import Image
from test_material_work import setup_work


def native_material(tmp_path):
    from novelvideo.freezone.remix.material_execution import attach_material_execution_draft
    from novelvideo.freezone.remix.material_work import material_workflow_intent
    from novelvideo.freezone.workflow_drafts import (create_workflow_draft, claim_workflow_draft_confirmation,
        bind_workflow_draft_task, finish_workflow_draft_confirmation)
    from novelvideo.freezone.workflow_runs import create_workflow_run, _connect
    state, output = tmp_path / 'state', tmp_path / 'output'
    state.mkdir()
    output.mkdir()
    store, plan_ref, prepared = setup_work(output)
    intent = material_workflow_intent(store.read(prepared['work_ref']['artifact_id'], 1)['payload'])
    draft = create_workflow_draft(project_dir=state, project_id='project', canvas_id='canvas', intent=intent,
        compiled={'ok': True, 'skill_id': intent['skill_id'], 'plan': {'nodes': []}})
    attached = attach_material_execution_draft(store, prepared['work_ref'], canvas_id='canvas', draft_id=draft['draft_id'], draft_revision=1, workflow_state_dir=state)
    claim_workflow_draft_confirmation(project_dir=state, canvas_id='canvas', draft_id=draft['draft_id'], revision=1)
    bind_workflow_draft_task(project_dir=state, canvas_id='canvas', draft_id=draft['draft_id'], task_id='draft-task', root_task_id='root')
    finish_workflow_draft_confirmation(project_dir=state, canvas_id='canvas', draft_id=draft['draft_id'], outcome='confirmed', expected_task_id='draft-task')
    url = '/static/projects/project/picture.png'
    Image.new('RGB', (16, 12), 'red').save(output / 'picture.png')
    canvas = {'nodes': [{'id': 'n', 'data': {'workflowInstanceId': draft['draft_id'], 'workflowPlanNodeId': 'image', 'imageUrl': url}}]}
    run = create_workflow_run(project_dir=state, project_id='project', canvas_id='canvas', actions=[{'node_id': 'n', 'action': 'generate_image', 'recipe_id': 'general-image'}])
    with _connect(state) as db:
        db.execute("UPDATE workflow_run_actions SET status='completed', artifact_status='valid', task_key='image-task', task_type='freezone_image_gen', job_id='image-job', result_ref_json=? WHERE run_id=?", (json.dumps({'value': url}), run['run_id']))
    native = {'run_id': run['run_id'], 'node_id': 'n', 'task_key': 'image-task'}
    return store, state, canvas, attached['work_ref'], native


def settle(fixture, **kw):
    from novelvideo.freezone.remix.material_settlement import settle_material_result
    store, state, canvas, ref, native = fixture
    return settle_material_result(store=store, canvas=canvas, work_ref=ref, step_id='image', native_result=native, workflow_state_dir=state, **kw)


def test_settlement_key_is_scope_bound():
    from novelvideo.freezone.remix.material_settlement import settlement_key
    task = {'run_id': 'r', 'task_key': 't', 'node_id': 'n'}
    result = {'revision': 1, 'media_hash': 'a' * 64}
    a = {'tenant_id': 'a', 'project_id': 'p', 'canvas_id': 'c'}
    assert settlement_key(a, task, result) == settlement_key(a, task, result)
    assert settlement_key(a, task, result) != settlement_key({**a, 'tenant_id': 'b'}, task, result)
    assert settlement_key(a, task, result) != settlement_key(a, {**task, 'task_key': 'new'}, result)


def test_repeat_and_restart_return_exact_same_review_without_extra_revision(tmp_path):
    fixture = native_material(tmp_path)
    first = settle(fixture)
    assert first['status'] == 'needs_review'
    assert first['checkpoint'] == 'checks_scheduled'
    assert first['review_ref'] and first['resource']['media_hash']
    revision = fixture[0].read_latest(fixture[3]['artifact_id'])['revision']
    assert settle(fixture) == first
    from novelvideo.freezone.remix.store import RemixArtifactStore
    restarted = (RemixArtifactStore(fixture[0].project_dir, 'project', created_by='alice'), *fixture[1:])
    assert settle(restarted) == first
    assert fixture[0].read_latest(fixture[3]['artifact_id'])['revision'] == revision


def test_completed_task_without_saved_action_output_waits_without_review(tmp_path):
    fixture = native_material(tmp_path)
    from novelvideo.freezone.workflow_runs import _connect
    with _connect(fixture[1]) as db:
        db.execute('UPDATE workflow_run_actions SET result_ref_json=NULL WHERE run_id=?', (fixture[4]['run_id'],))
    fixture[4].update(status='completed', result_ref={'value': '/static/projects/project/picture.png'})
    result = settle(fixture)
    assert result['reason'] == 'native_result_unavailable'
    assert result['status'] == 'registered'
    assert not result.get('review_ref')


def test_registration_crash_recovery_does_not_repeat_work_publication(tmp_path, monkeypatch):
    fixture = native_material(tmp_path)
    from novelvideo.freezone.remix.operations import RemixOperationStore
    original = RemixOperationStore.checkpoint_settlement
    crashed = False
    def fail_after_registration(self, key, **changes):
        nonlocal crashed
        if changes.get('checkpoint') == 'registered' and not crashed:
            crashed = True
            raise RuntimeError('crash after artifact publication')
        return original(self, key, **changes)
    monkeypatch.setattr(RemixOperationStore, 'checkpoint_settlement', fail_after_registration)
    with pytest.raises(RuntimeError, match='crash'):
        settle(fixture)
    revision = fixture[0].read_latest(fixture[3]['artifact_id'])['revision']
    recovered = settle(fixture)
    assert recovered['status'] == 'needs_review'
    assert fixture[0].read_latest(fixture[3]['artifact_id'])['revision'] == revision


def test_old_attempt_late_result_is_saved_without_overwriting_current_work(tmp_path):
    fixture = native_material(tmp_path)
    store, state, canvas, ref, native = fixture
    from novelvideo.freezone.remix.material_execution import register_material_execution
    saved = store.read(ref['artifact_id'], ref['revision'])
    newer = {**saved['payload']['steps'][0]['execution'], 'run_id': 'new-run', 'task_id': 'new-task'}
    register_material_execution(store, ref, 'image', 'n', newer)
    before = store.read_latest(ref['artifact_id'])
    result = settle(fixture)
    assert result['status'] == 'stale'
    assert result['resource']['media_hash']
    assert store.read_latest(ref['artifact_id']) == before


def test_plan_change_or_stopped_generation_does_not_schedule_checks(tmp_path):
    fixture = native_material(tmp_path)
    store, state, canvas, ref, native = fixture
    work = store.read(ref['artifact_id'], ref['revision'])
    plan = store.read_latest(work['payload']['plan_ref']['artifact_id'])
    store.revise(plan['id'], plan['revision'], {**plan['payload'], 'goal': 'new goal'}, plan['upstream_refs'])
    calls = []
    result = settle(fixture, check_scheduler=lambda **kw: calls.append(kw))
    assert result['status'] == 'stale' and calls == []
    assert store.read_latest(ref['artifact_id'])['revision'] == ref['revision']


def test_cleanup_requires_persisted_native_completion_not_canvas_url(tmp_path):
    fixture = native_material(tmp_path)
    first = settle(fixture)
    from novelvideo.freezone.workflow_runs import _connect
    with _connect(fixture[1]) as db:
        db.execute('DELETE FROM workflow_run_actions WHERE run_id=?', (fixture[4]['run_id'],))
    assert settle(fixture) == first  # complete durable evidence may replay
    # Another task cannot borrow that evidence or the node's output URL.
    fixture[4]['task_key'] = 'unknown'
    result = settle(fixture)
    assert result['reason'] == 'native_result_unavailable' and not result.get('review_ref')


def test_checks_use_fixed_command_id_and_resume_after_scheduler_failure(tmp_path):
    fixture = native_material(tmp_path)
    commands = []
    def checks(**kwargs):
        commands.append(kwargs['command_id'])
        if len(commands) == 1:
            raise RuntimeError('check enqueue failed')
        return {'status': 'checking', 'check_ref': {'task_key': 'trusted-check'}}
    with pytest.raises(RuntimeError, match='enqueue'):
        settle(fixture, check_scheduler=checks)
    result = settle(fixture, check_scheduler=checks)
    assert commands[0] == commands[1]
    assert result['status'] == 'checking'
    assert settle(fixture, check_scheduler=checks) == result and len(commands) == 2


def test_completion_without_file_stays_pending_persistence(tmp_path):
    fixture = native_material(tmp_path)
    (fixture[0].project_dir / 'picture.png').unlink()
    result = settle(fixture)
    assert result['status'] == 'registered' and result['reason'] == 'native_result_unavailable'
    assert not result.get('review_ref')


@pytest.mark.parametrize('stopped', ['native_cancelled', 'inactive'])
def test_stopped_native_generation_is_historical_only(tmp_path, stopped):
    fixture = native_material(tmp_path)
    if stopped == 'native_cancelled':
        from novelvideo.freezone.workflow_runs import update_workflow_run
        update_workflow_run(project_dir=fixture[1], canvas_id='canvas', run_id=fixture[4]['run_id'], status='cancelled')
    before = fixture[0].read_latest(fixture[3]['artifact_id'])
    calls = []
    result = settle(fixture, active=stopped != 'inactive', check_scheduler=lambda **kw: calls.append(kw))
    assert result['status'] == 'stale' and calls == []
    assert fixture[0].read_latest(fixture[3]['artifact_id']) == before


def test_recovered_completed_generation_can_settle_after_run_cancelled(tmp_path):
    fixture = native_material(tmp_path)
    from novelvideo.freezone.workflow_runs import update_workflow_run

    update_workflow_run(
        project_dir=fixture[1],
        canvas_id="canvas",
        run_id=fixture[4]["run_id"],
        status="cancelled",
    )
    result = settle(fixture, allow_cancelled_completed=True)
    assert result["status"] == "needs_review"
    assert result["review_ref"]


def test_old_status_events_cannot_downgrade_saved_native_completion(tmp_path):
    fixture = native_material(tmp_path)
    first = settle(fixture)
    fixture[4].update(status='running', result_ref=None)
    assert settle(fixture) == first
    fixture[4].update(status='failed')
    assert settle(fixture) == first


def test_saved_full_completion_recovers_checks_after_native_action_cleanup(tmp_path):
    fixture = native_material(tmp_path)
    def failed(**kwargs): raise RuntimeError('enqueue failed')
    with pytest.raises(RuntimeError, match='enqueue'):
        settle(fixture, check_scheduler=failed)
    from novelvideo.freezone.workflow_runs import _connect
    with _connect(fixture[1]) as db:
        db.execute('DELETE FROM workflow_run_actions WHERE run_id=?', (fixture[4]['run_id'],))
    result = settle(fixture)
    assert result['status'] == 'needs_review' and result['review_ref']


def test_settlement_reports_saved_authenticated_acceptance(tmp_path):
    fixture = native_material(tmp_path)
    first = settle(fixture)
    from novelvideo.freezone.remix.material_execution import confirm_material_review
    from novelvideo.freezone.remix.material_work import accept_material_candidate
    store, state, canvas, ref, native = fixture
    confirmed = confirm_material_review(store, first['review_ref'], canvas, canvas_id='canvas', requester_user_id='alice')
    accepted = accept_material_candidate(store, first['work_ref'], 'image', first['resource'], confirmed['review_ref'], 'accept-settled')
    result = settle(fixture)
    assert result['status'] == 'accepted' and result['work_ref'] == accepted['work_ref']
    assert result['review_ref'] == confirmed['review_ref']


def test_registered_checks_recover_after_independent_work_revision(tmp_path):
    fixture = native_material(tmp_path)
    def unavailable(**kwargs): raise RuntimeError('checks unavailable')
    with pytest.raises(RuntimeError, match='unavailable'):
        settle(fixture, check_scheduler=unavailable)
    store, state, canvas, ref, native = fixture
    saved = store.read_latest(ref['artifact_id'])
    saved['payload']['steps'][1]['label'] = 'new independent label'
    store.revise(saved['id'], saved['revision'], saved['payload'], saved['upstream_refs'])
    result = settle(fixture)
    assert result['status'] == 'needs_review' and result['review_ref']


def test_review_publication_crash_replays_exact_review_reference(tmp_path, monkeypatch):
    fixture = native_material(tmp_path)
    from novelvideo.freezone.remix.operations import RemixOperationStore
    original = RemixOperationStore.checkpoint_settlement
    first_review = []
    def fail_once(self, key, **changes):
        if changes.get('checkpoint') == 'checks_scheduled' and not first_review:
            first_review.append(changes['review_ref'])
            raise RuntimeError('crash after review publication')
        return original(self, key, **changes)
    monkeypatch.setattr(RemixOperationStore, 'checkpoint_settlement', fail_once)
    with pytest.raises(RuntimeError, match='review publication'):
        settle(fixture)
    store, _, _, ref, _ = fixture
    saved = store.read_latest(ref['artifact_id'])
    saved['payload']['steps'][1]['label'] = 'updated independent label'
    store.revise(saved['id'], saved['revision'], saved['payload'], saved['upstream_refs'])
    result = settle(fixture)
    assert result['review_ref'] == first_review[0]


@pytest.mark.parametrize('stopped', ['native_cancelled', 'inactive'])
def test_stopped_disposition_survives_run_cleanup_and_default_active_replay(tmp_path, stopped):
    from datetime import datetime, timezone
    from novelvideo.freezone.workflow_runs import update_workflow_run, prune_workflow_runs
    fixture = native_material(tmp_path)
    if stopped == 'native_cancelled':
        update_workflow_run(project_dir=fixture[1], canvas_id='canvas', run_id=fixture[4]['run_id'], status='cancelled')
    first = settle(fixture, active=stopped != 'inactive')
    before = fixture[0].read_latest(fixture[3]['artifact_id'])
    prune_workflow_runs(project_dir=fixture[1], canvas_id='canvas', now=datetime(2100, 1, 1, tzinfo=timezone.utc))
    calls = []
    replay = settle(fixture, check_scheduler=lambda **kw: calls.append(kw))
    assert replay['status'] == first['status'] == 'stale'
    assert calls == []
    assert fixture[0].read_latest(fixture[3]['artifact_id']) == before


@pytest.mark.parametrize('action_cleanup', [False, True])
def test_complete_settlement_recovers_after_confirmed_draft_cleanup(tmp_path, action_cleanup):
    from novelvideo.freezone.workflow_drafts import prune_expired_workflow_drafts
    from novelvideo.freezone.workflow_runs import _connect
    fixture = native_material(tmp_path)
    def unavailable(**kw): raise RuntimeError('queue unavailable')
    with pytest.raises(RuntimeError, match='queue'):
        settle(fixture, check_scheduler=unavailable)
    before = fixture[0].read_latest(fixture[3]['artifact_id'])
    assert prune_expired_workflow_drafts(project_dir=fixture[1], canvas_id='canvas', now=10**12) == 1
    if action_cleanup:
        with _connect(fixture[1]) as db:
            db.execute('DELETE FROM workflow_run_actions WHERE run_id=?', (fixture[4]['run_id'],))
    result = settle(fixture)
    assert result['status'] == 'needs_review' and result['review_ref']
    assert fixture[0].read_latest(fixture[3]['artifact_id']) == before


def test_external_scheduler_receives_latest_compatible_work_after_queue_failure(tmp_path):
    from novelvideo.freezone.remix.material_execution import review_material_candidate
    fixture = native_material(tmp_path)
    commands, revisions = [], []
    def scheduler(**kw):
        commands.append(kw['command_id'])
        revisions.append(kw['work_ref']['revision'])
        if len(commands) == 1:
            raise RuntimeError('queue unavailable')
        checked = review_material_candidate(kw['store'], kw['work_ref'], kw['step_id'], kw['canvas'], fixture[4]['node_id'],
            workflow_state_dir=kw['workflow_state_dir'], command_id=kw['command_id'], settlement_id=kw['settlement_id'])
        return {'status': 'needs_review', 'review_ref': checked['review_ref']}
    with pytest.raises(RuntimeError, match='queue'):
        settle(fixture, check_scheduler=scheduler)
    saved = fixture[0].read_latest(fixture[3]['artifact_id'])
    saved['payload']['steps'][1]['label'] = 'independent update'
    current = fixture[0].revise(saved['id'], saved['revision'], saved['payload'], saved['upstream_refs'])
    result = settle(fixture, check_scheduler=scheduler)
    assert revisions == [saved['revision'], current['revision']]
    assert commands[0] == commands[1]
    assert result['work_ref']['revision'] == current['revision']
    assert settle(fixture, check_scheduler=scheduler) == result and len(commands) == 2


@pytest.mark.parametrize('missing', ['native_authorization', 'native_evidence', 'resource', 'result_key', 'authorization_work_ref'])
def test_draft_cleanup_never_recovers_partial_settlement_proof(tmp_path, missing):
    from novelvideo.freezone.workflow_drafts import prune_expired_workflow_drafts
    from novelvideo.freezone.remix.material_execution import validate_material_native_execution
    from novelvideo.freezone.remix.operations import RemixOperationStore
    fixture = native_material(tmp_path)
    completed = settle(fixture)
    store, state, canvas, ref, native = fixture
    operations = RemixOperationStore(store.project_dir, store.project_id)
    path = operations._settlement_path(completed['settlement_id'])
    record = json.loads(path.read_text())
    record.pop(missing, None)
    path.write_text(json.dumps(record))
    prune_expired_workflow_drafts(project_dir=state, canvas_id='canvas', now=10**12)
    saved = store.read_latest(ref['artifact_id'])
    with pytest.raises(ValueError, match='confirmed'):
        validate_material_native_execution(store, canvas, saved['payload']['steps'][0], 'n', require_complete=True,
            workflow_state_dir=state, work_ref={'artifact_id': saved['id'], 'revision': saved['revision']})


@pytest.mark.parametrize('changed', ['target', 'requirements', 'task', 'resource'])
def test_external_retry_rejects_changed_inputs_even_with_latest_caller_ref(tmp_path, changed):
    from novelvideo.freezone.remix.material_settlement import settle_material_result
    fixture = native_material(tmp_path)
    store, state, canvas, ref, native = fixture
    def unavailable(**kw): raise RuntimeError('queue unavailable')
    with pytest.raises(RuntimeError, match='queue'):
        settle(fixture, check_scheduler=unavailable)
    saved = store.read_latest(ref['artifact_id'])
    if changed == 'target':
        saved['payload']['steps'][0]['prompt'] += ' changed target'
    if changed == 'requirements':
        from novelvideo.freezone.remix.material_work import _fingerprint
        saved['payload']['requirements'][0].update(final_step_id='image', media_kind='image', acceptance=['new criterion'])
        plan = store.read(saved['payload']['plan_ref']['artifact_id'], saved['payload']['plan_ref']['revision'])
        saved['payload']['input_fingerprint'] = _fingerprint(store, plan['payload'], saved['payload']['requirements'])
    if changed == 'task':
        saved['payload']['steps'][0]['execution']['task_id'] = 'replacement-task'
    if changed == 'resource':
        Image.new('RGB', (16, 12), 'blue').save(store.project_dir / 'picture.png')
    if changed != 'resource':
        saved = store.revise(saved['id'], saved['revision'], saved['payload'], saved['upstream_refs'])
    before = store.read_latest(ref['artifact_id'])
    calls = []
    result = settle_material_result(store=store, canvas=canvas,
        work_ref={'artifact_id': saved['id'], 'revision': saved['revision']}, step_id='image', native_result=native,
        workflow_state_dir=state, check_scheduler=lambda **kw: calls.append(kw))
    assert result['status'] == 'stale' and calls == []
    assert store.read_latest(ref['artifact_id']) == before


def test_external_review_publication_crash_replays_actual_historical_reference(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.freezone.remix.material_execution import review_material_candidate
    fixture = native_material(tmp_path)
    original = RemixOperationStore.checkpoint_settlement
    first = []
    calls = []
    def fail_once(self, key, **changes):
        if changes.get('checkpoint') == 'checks_scheduled' and not first:
            first.append(changes['review_ref'])
            raise RuntimeError('after published check')
        return original(self, key, **changes)
    monkeypatch.setattr(RemixOperationStore, 'checkpoint_settlement', fail_once)
    def scheduler(**kw):
        calls.append(kw['work_ref'])
        result = review_material_candidate(kw['store'], kw['work_ref'], kw['step_id'], kw['canvas'], fixture[4]['node_id'],
            workflow_state_dir=kw['workflow_state_dir'], settlement_id=kw['settlement_id'], command_id=kw['command_id'])
        return {'status': 'needs_review', 'review_ref': result['review_ref']}
    with pytest.raises(RuntimeError, match='published'):
        settle(fixture, check_scheduler=scheduler)
    saved = fixture[0].read_latest(fixture[3]['artifact_id'])
    saved['payload']['steps'][1]['label'] = 'independent change'
    fixture[0].revise(saved['id'], saved['revision'], saved['payload'], saved['upstream_refs'])
    result = settle(fixture, check_scheduler=scheduler)
    assert result['review_ref'] == first[0] and result['work_ref'] == calls[0]
    assert len(calls) == 1


def test_complete_authorization_fallback_is_bound_to_exact_work_and_actor(tmp_path):
    from novelvideo.freezone.workflow_drafts import prune_expired_workflow_drafts
    from novelvideo.freezone.remix.material_execution import validate_material_native_execution
    from novelvideo.freezone.remix.store import RemixArtifactStore
    fixture = native_material(tmp_path)
    first = settle(fixture)
    store, state, canvas, ref, native = fixture
    saved = store.read_latest(ref['artifact_id'])
    other = store.create(saved['schema'], saved['payload'], saved['upstream_refs'])
    prune_expired_workflow_drafts(project_dir=state, canvas_id='canvas', now=10**12)
    with pytest.raises(ValueError, match='confirmed'):
        validate_material_native_execution(store, canvas, other['payload']['steps'][0], 'n', require_complete=True,
            workflow_state_dir=state, settlement_id=first['settlement_id'],
            work_ref={'artifact_id': other['id'], 'revision': other['revision']})
    other_actor = RemixArtifactStore(store.project_dir, store.project_id, created_by='bob')
    with pytest.raises(ValueError, match='confirmed'):
        validate_material_native_execution(other_actor, canvas, saved['payload']['steps'][0], 'n', require_complete=True,
            workflow_state_dir=state, settlement_id=first['settlement_id'], work_ref=first['work_ref'])


def test_auto_lookup_recovers_only_exact_native_proof_after_both_retention_cleanups(tmp_path):
    from datetime import datetime, timezone
    from novelvideo.freezone.workflow_drafts import prune_expired_workflow_drafts
    from novelvideo.freezone.workflow_runs import update_workflow_run, prune_workflow_runs
    from novelvideo.freezone.remix.material_execution import validate_material_native_execution
    fixture = native_material(tmp_path)
    first = settle(fixture)
    store, state, canvas, ref, native = fixture
    update_workflow_run(project_dir=state, canvas_id='canvas', run_id=native['run_id'], status='completed')
    prune_workflow_runs(project_dir=state, canvas_id='canvas', now=datetime(2100, 1, 1, tzinfo=timezone.utc))
    prune_expired_workflow_drafts(project_dir=state, canvas_id='canvas', now=10**12)
    saved = store.read_latest(ref['artifact_id'])
    result = validate_material_native_execution(store, canvas, saved['payload']['steps'][0], 'n', require_complete=True,
        workflow_state_dir=state, work_ref=first['work_ref'])
    assert result['run_id'] == native['run_id'] and result['task_id'] == native['task_key']
    assert settle(fixture) == first
