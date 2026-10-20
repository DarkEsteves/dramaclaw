"""Explicit browser material choices persist only their exact native receipt."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from PIL import Image
from fastapi import HTTPException

from test_reference_commission import confirmed_plan
from novelvideo.freezone.remix.store import RemixArtifactStore
from novelvideo.freezone.remix.reference_director import prepare_reference_decision, confirm_reference_decision, list_director_artifacts
from novelvideo.freezone.remix.material_execution import review_material_candidate
from novelvideo.freezone.remix.material_work import prepare_material_work, accept_material_candidate


def fixture(tmp_path):
    store = RemixArtifactStore(tmp_path, 'p', created_by='u')
    commission, plan = confirmed_plan(store)
    plan_ref = {'artifact_id': plan['id'], 'revision': plan['revision']}
    work = prepare_material_work(store, plan_ref,
        [{'id': 'guest', 'description': 'Guest image', 'media_kind': 'image', 'final_step_id': 'guest'}],
        [{'id': 'guest', 'label': 'Guest image', 'source': 'existing', 'media_kind': 'image', 'node_id': 'image'}], 'work')
    Image.new('RGB', (16, 12), 'red').save(tmp_path / 'guest.png')
    canvas = {'project_id': 'p', 'canvas_id': 'c', 'revision': 1, 'edges': [], 'nodes': [
        {'id': 'plan', 'type': 'scriptNode', 'position': {'x': 0, 'y': 0},
         'data': {'productionPlanV2Ref': {'artifactId': plan['id'], 'revision': plan['revision']}}},
        {'id': 'image', 'type': 'imageGenNode', 'position': {'x': 0, 'y': 0},
         'data': {'imageUrl': '/static/projects/p/guest.png'}}]}
    review = review_material_candidate(store, work['work_ref'], 'guest', canvas, 'image')
    proposal = {'work_ref': work['work_ref'], 'plan_ref': plan_ref,
        'candidate': {'step_id': 'guest', 'review_ref': review['review_ref'],
                      'media_hash': review['review']['resource']['media_hash'], 'duration_us': 0, 'has_audio': False,
                      'node_label': 'Guest image'},
        'acceptance_note': 'Image matches the requested guest', 'next_action': 'Prepare the answer clip'}
    decision = prepare_reference_decision(store, commission['commission_ref'], 'material', proposal, 'Use this guest image', 'choose')
    return store, canvas, commission, work, review, decision


def confirm(store, canvas, decision, **kwargs):
    return confirm_reference_decision(store, decision['decision_ref'], project_id='p', canvas_id='c',
                                      requester_user_id='u', canvas=canvas, **kwargs)


def snapshot(store):
    return {str(p.relative_to(store.root)): p.read_bytes() for p in store.root.rglob('*.json')}


def test_explicit_candidate_persists_real_review_and_decision_then_native_accept(tmp_path):
    store, canvas, commission, work, review, decision = fixture(tmp_path)
    resource = review['review']['resource']
    with pytest.raises(ValueError, match='technical/content'):
        accept_material_candidate(store, work['work_ref'], 'guest', resource, review['review_ref'], 'too-early')
    result = confirm(store, canvas, decision)
    assert result['review_ref'] == {**review['review_ref'], 'revision': 2}
    saved_review = store.read_latest(review['review_ref']['artifact_id'])
    assert saved_review['payload']['content']['status'] == 'passed'
    assert saved_review['payload']['actor_kind'] == 'user'
    assert saved_review['payload']['user_decision'] == {'project_id': 'p', 'canvas_id': 'c',
        'requester_user_id': 'u', 'media_hash': resource['media_hash']}
    saved = store.read_latest(decision['decision_ref']['artifact_id'])
    assert saved['payload']['status'] == 'confirmed' and saved['revision'] == 2
    assert saved['payload']['decided_by'] == 'u'
    assert any(ref['artifact_id'] == result['review_ref']['artifact_id'] and ref['revision'] == 2 for ref in saved['upstream_refs'])
    assert list_director_artifacts(store, 'c', 'u')['decisions'] == []
    assert result['commission']['commission_ref'] == commission['commission_ref']
    assert store.read_latest(work['work_ref']['artifact_id'])['revision'] == 1
    accepted = accept_material_candidate(store, work['work_ref'], 'guest', resource, result['review_ref'], 'accept-selected')
    assert accepted['work_ref']['revision'] == 2


def test_exact_repeated_confirmation_reuses_saved_receipt_without_writes(tmp_path):
    store, canvas, _, _, _, decision = fixture(tmp_path)
    first = confirm(store, canvas, decision)
    before = snapshot(store)
    assert confirm(store, canvas, decision) == first
    assert snapshot(store) == before
    Image.new('RGB', (16, 12), 'blue').save(tmp_path / 'guest.png')
    with pytest.raises(ValueError, match='changed'):
        confirm(store, canvas, decision)
    assert snapshot(store) == before


@pytest.mark.parametrize('change', ['work-ref', 'review-ref', 'step', 'hash', 'plan', 'duration', 'audio',
                                   'missing-candidate', 'narrative-only', 'batch', 'extra-target'])
def test_invalid_material_proposal_has_no_review_or_decision_write(tmp_path, change):
    store, canvas, commission, work, review, _ = fixture(tmp_path)
    proposal = {'work_ref': work['work_ref'], 'plan_ref': store.read_latest(work['work_ref']['artifact_id'])['payload']['plan_ref'],
                'candidate': {'step_id': 'guest', 'review_ref': review['review_ref'], 'media_hash': review['review']['resource']['media_hash']}}
    if change == 'work-ref': proposal['work_ref']['revision'] = True
    elif change == 'review-ref': proposal['candidate']['review_ref']['revision'] = 0
    elif change == 'step': proposal['candidate']['step_id'] = 'other'
    elif change == 'hash': proposal['candidate']['media_hash'] = 'a' * 64
    elif change == 'plan': proposal['plan_ref'] = {'artifact_id': commission['commission_ref']['artifact_id'], 'revision': 1}
    elif change == 'duration': proposal['candidate']['duration_us'] = 100
    elif change == 'audio': proposal['candidate']['has_audio'] = True
    elif change == 'missing-candidate': proposal.pop('candidate')
    elif change == 'narrative-only': proposal = {'acceptance_note': 'User approved everything', 'next_action': 'Generate'}
    elif change == 'batch': proposal['candidate'] = [proposal['candidate']]
    else: proposal['speech_approved'] = True
    decision = prepare_reference_decision(store, commission['commission_ref'], 'material', proposal, 'Candidate', change)
    before = snapshot(store)
    with pytest.raises((ValueError, PermissionError, FileNotFoundError)):
        confirm(store, canvas, decision)
    assert snapshot(store) == before


@pytest.mark.parametrize('change', ['actor', 'project', 'canvas', 'work-revision', 'review-revision',
                                   'commission-revision', 'plan-revision', 'no-plan-node', 'stale-output', 'foreign-media'])
def test_scope_revision_or_current_media_change_rejects_without_writes(tmp_path, change):
    store, canvas, _, work, review, decision = fixture(tmp_path)
    kwargs = dict(project_id='p', canvas_id='c', requester_user_id='u', canvas=canvas)
    if change in {'actor', 'project', 'canvas'}:
        kwargs[{'actor': 'requester_user_id', 'project': 'project_id', 'canvas': 'canvas_id'}[change]] = 'other'
    elif change in {'work-revision', 'review-revision', 'commission-revision', 'plan-revision'}:
        refs = {'work-revision': work['work_ref'], 'review-revision': review['review_ref'],
                'commission-revision': decision['decision']['commission_ref'], 'plan-revision': decision['decision']['proposal']['plan_ref']}
        row = store.read_latest(refs[change]['artifact_id'])
        store.revise(row['id'], row['revision'], row['payload'], row['upstream_refs'])
    elif change == 'no-plan-node': canvas['nodes'] = canvas['nodes'][1:]
    elif change == 'stale-output': canvas['nodes'][1]['data']['workflowResultStale'] = True
    else: canvas['nodes'][1]['data']['imageUrl'] = '/static/projects/foreign/guest.png'
    before = snapshot(store)
    with pytest.raises((ValueError, PermissionError)):
        confirm_reference_decision(store, decision['decision_ref'], **kwargs)
    assert snapshot(store) == before


def test_partial_review_publication_recovers_only_existing_exact_user_receipt(tmp_path, monkeypatch):
    store, canvas, _, _, review, decision = fixture(tmp_path)
    original = store._publish
    def interrupted(**kwargs):
        if kwargs['schema'] == 'reference_decision.v1' and kwargs['payload']['status'] == 'confirmed':
            raise OSError('simulated decision publication failure')
        return original(**kwargs)
    monkeypatch.setattr(store, '_publish', interrupted)
    with pytest.raises(OSError): confirm(store, canvas, decision)
    saved = store.read_latest(review['review_ref']['artifact_id'])
    assert saved['revision'] == 2 and saved['payload']['user_decision']['requester_user_id'] == 'u'
    assert store.read_latest(decision['decision_ref']['artifact_id'])['payload']['status'] == 'pending'
    monkeypatch.setattr(store, '_publish', original)
    result = confirm(store, canvas, decision)
    assert result['review_ref']['revision'] == 2
    assert store.read_latest(saved['id']) == saved


@pytest.mark.parametrize('change', ['extra-revision', 'different-observation', 'other-actor'])
def test_partial_recovery_cannot_adopt_unrelated_or_changed_review(tmp_path, monkeypatch, change):
    from novelvideo.freezone.remix.material_execution import confirm_material_review
    store, canvas, _, _, review, decision = fixture(tmp_path)
    if change == 'other-actor':
        original_user = store.created_by
        store.created_by = 'other'
        confirmed = confirm_material_review(store, review['review_ref'], canvas, canvas_id='c', requester_user_id='other')
        store.created_by = original_user
    else:
        confirmed = confirm_material_review(store, review['review_ref'], canvas, canvas_id='c', requester_user_id='u')
        if change == 'extra-revision':
            row = store.read_latest(confirmed['review_ref']['artifact_id'])
            store.revise(row['id'], row['revision'], row['payload'], row['upstream_refs'])
        else:
            # A replaced observation on a newer version is not the exact original confirmation.
            row = store.read_latest(confirmed['review_ref']['artifact_id'])
            row['payload']['evidence_refs'] = [{'artifact_id': decision['decision_ref']['artifact_id'], 'revision': 1}]
            store.revise(row['id'], row['revision'], row['payload'], [*row['upstream_refs'], decision['decision_ref']])
    before = snapshot(store)
    with pytest.raises((ValueError, PermissionError)):
        confirm(store, canvas, decision)
    assert snapshot(store) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('credential_kind', ['agent_session', 'local_trusted_agent'])
async def test_browser_button_endpoint_denies_agent_credentials(credential_kind):
    from novelvideo.api.routes.reference_director import confirm_decision
    with pytest.raises(HTTPException) as rejected:
        await confirm_decision('p', 'c', 'decision', {'credential_kind': credential_kind})
    assert rejected.value.status_code == 403


@pytest.mark.asyncio
async def test_button_uses_current_saved_canvas_not_context_snapshot(tmp_path, monkeypatch):
    from novelvideo.api.routes import reference_director as routes
    from novelvideo.freezone import canvas_store
    store, canvas, _, _, review, decision = fixture(tmp_path)
    context_canvas = deepcopy(canvas)
    canvas['nodes'][1]['data']['workflowResultStale'] = True
    canvas_store.save_canvas(tmp_path, 'c', base_revision=None, build_payload=lambda _: canvas)
    async def context(*args):
        return SimpleNamespace(project_id='p', requester_user_id='u', state_dir=tmp_path), context_canvas, store
    async def noop(*args, **kwargs): pass
    monkeypatch.setattr(routes, '_context', context)
    monkeypatch.setattr(routes, '_notify_commission', noop)
    monkeypatch.setattr(routes, '_emit_director_update', noop)
    before = snapshot(store)
    with pytest.raises(HTTPException) as failed:
        await routes.confirm_decision('p', 'c', decision['decision_ref']['artifact_id'], {'credential_kind': 'browser_session'})
    assert failed.value.status_code == 422
    assert snapshot(store) == before


def test_material_confirmation_cannot_use_generic_chat_receipt(tmp_path):
    store, canvas, _, _, _, decision = fixture(tmp_path)
    before = snapshot(store)
    with pytest.raises(ValueError, match='explicit browser'):
        confirm(store, canvas, decision, message_id='user-message')
    assert snapshot(store) == before


@pytest.mark.asyncio
async def test_native_button_holds_canvas_mutex_during_material_publications(tmp_path, monkeypatch):
    from novelvideo.api.routes import reference_director as routes
    from novelvideo.freezone import canvas_store
    from novelvideo.freezone.canvas_lock import CanvasLockBusy
    store, canvas, _, _, _, decision = fixture(tmp_path)
    canvas_store.save_canvas(tmp_path, 'c', base_revision=None, build_payload=lambda _: canvas)
    async def context(*args):
        return SimpleNamespace(project_id='p', requester_user_id='u', state_dir=tmp_path), canvas, store
    async def noop(*args, **kwargs): pass
    monkeypatch.setattr(routes, '_context', context)
    monkeypatch.setattr(routes, '_notify_commission', noop)
    monkeypatch.setattr(routes, '_emit_director_update', noop)
    original = store._publish
    locked = []
    def publish(**kwargs):
        if kwargs['schema'] in {'material_review.v1', 'reference_decision.v1'}:
            with pytest.raises(CanvasLockBusy):
                with canvas_store.get_canvas_write_mutex().write_mutex(tmp_path, 'c', timeout_seconds=0.02):
                    pytest.fail('Native material confirmation lost the current canvas mutex')
            locked.append(kwargs['schema'])
        return original(**kwargs)
    monkeypatch.setattr(store, '_publish', publish)
    result = await routes.confirm_decision('p', 'c', decision['decision_ref']['artifact_id'], {'credential_kind': 'browser_session'})
    assert result['data']['review_ref']['revision'] == 2
    assert locked == ['material_review.v1', 'reference_decision.v1']
    # Both mutexes have been released on the successful return.
    with canvas_store.get_canvas_write_mutex().write_mutex(tmp_path, 'c', timeout_seconds=0.02): pass
    assert store.read_latest(decision['decision_ref']['artifact_id'])['payload']['status'] == 'confirmed'
