from copy import deepcopy
from types import SimpleNamespace

import pytest
from PIL import Image


def setup(tmp_path):
    from test_reference_commission import confirmed_plan
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.freezone.remix.material_work import prepare_material_work
    from novelvideo.freezone.remix.material_execution import review_material_candidate
    store = RemixArtifactStore(tmp_path, 'p', created_by='u')
    _, plan = confirmed_plan(store)
    ref = {'artifact_id': plan['id'], 'revision': plan['revision']}
    work = prepare_material_work(store, ref,
        [{'id': x, 'description': x, 'media_kind': 'image', 'final_step_id': x} for x in ['a', 'b']],
        [{'id': x, 'label': x, 'source': 'existing', 'media_kind': 'image', 'node_id': x} for x in ['a', 'b']], 'two')
    canvas = {'project_id': 'p', 'access_model': 'private', 'owner_principal_id': 'u',
        'nodes': [{'id': 'plan', 'type': 'scriptNode', 'data': {'productionPlanV2Ref': {
            'artifactId': plan['id'], 'revision': plan['revision']}}}], 'edges': []}
    items = []
    for x in ['a', 'b']:
        Image.new('RGB', (16, 12), 'red').save(tmp_path / (x + '.png'))
        canvas['nodes'].append({'id': x, 'type': 'imageNode', 'data': {'imageUrl': f'/static/projects/p/{x}.png'}})
        review = review_material_candidate(store, work['work_ref'], x, canvas, x)
        items.append({'review_ref': review['review_ref'], 'work_ref': work['work_ref'], 'step_id': x, 'node_id': x})
    ctx = SimpleNamespace(project_id='p', output_dir=tmp_path, state_dir=tmp_path, requester_user_id='u',
        owner_type='user', owner_id='u', effective_role='owner', is_home_node=True)
    return ctx, canvas, store, items


def test_exact_checked_material_batch_and_replay(tmp_path):
    from novelvideo.freezone.remix.material_review_batch import confirm_material_review_batch
    ctx, canvas, store, items = setup(tmp_path)
    first = confirm_material_review_batch(store, ctx, canvas, canvas_id='c', items=items, command_id='batch')
    assert len(first['items']) == 2
    assert all(x['review']['content']['status'] == 'passed' for x in first['items'])
    assert all(x['review']['user_decision']['requester_user_id'] == 'u' for x in first['items'])
    second = confirm_material_review_batch(store, ctx, canvas, canvas_id='c', items=items, command_id='batch')
    assert second['replayed'] is True
    assert second['items'] == first['items']


def test_invalid_second_item_does_not_confirm_first(tmp_path):
    from novelvideo.freezone.remix.material_review_batch import confirm_material_review_batch
    ctx, canvas, store, items = setup(tmp_path)
    Image.new('RGB', (16, 12), 'blue').save(tmp_path / 'b.png')
    with pytest.raises(ValueError, match='changed'):
        confirm_material_review_batch(store, ctx, canvas, canvas_id='c', items=items, command_id='batch')
    assert store.read_latest(items[0]['review_ref']['artifact_id'])['revision'] == 1


def test_partial_batch_replays_only_original_user_receipt(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.material_review_batch import confirm_material_review_batch
    ctx, canvas, store, items = setup(tmp_path)
    original = store._publish
    interrupted = []
    def fail_second(**kw):
        if kw['schema'] == 'material_review.v1' and kw['artifact_id'] == items[1]['review_ref']['artifact_id'] and not interrupted:
            interrupted.append(True)
            raise OSError('second write')
        return original(**kw)
    monkeypatch.setattr(store, '_publish', fail_second)
    with pytest.raises(OSError):
        confirm_material_review_batch(store, ctx, canvas, canvas_id='c', items=items, command_id='batch')
    assert store.read_latest(items[0]['review_ref']['artifact_id'])['revision'] == 2
    restored = confirm_material_review_batch(store, ctx, canvas, canvas_id='c', items=items, command_id='batch')
    assert all(x['review_ref']['revision'] == 2 for x in restored['items'])


@pytest.mark.parametrize('change', ['private', 'dirty', 'target', 'stale', 'duplicate', 'wrong_node'])
def test_batch_scope_and_current_target_guard(tmp_path, change):
    from novelvideo.freezone.remix.material_review_batch import confirm_material_review_batch
    ctx, canvas, store, items = setup(tmp_path)
    if change == 'private': canvas['owner_principal_id'] = 'other'
    elif change == 'dirty': canvas['nodes'][0]['data']['productionPlanV2Dirty'] = True
    elif change == 'target':
        saved = store.read_latest(items[0]['work_ref']['artifact_id'])
        store.revise(saved['id'], saved['revision'], saved['payload'], saved['upstream_refs'])
    elif change == 'stale':
        saved = store.read_latest(items[1]['review_ref']['artifact_id'])
        payload = deepcopy(saved['payload']); payload['content']['findings'] = ['new review']
        store.revise(saved['id'], saved['revision'], payload, saved['upstream_refs'])
    elif change == 'duplicate': items[1] = items[0]
    else: items[1]['node_id'] = 'a'
    with pytest.raises(ValueError):
        confirm_material_review_batch(store, ctx, canvas, canvas_id='c', items=items, command_id='batch')
    assert store.read_latest(items[0]['review_ref']['artifact_id'])['revision'] == 1


@pytest.mark.asyncio
async def test_batch_api_denies_agent_confirmation():
    from fastapi import HTTPException
    from novelvideo.api.routes import reference_material as routes
    body = routes.MaterialReviewBatchBody(items=[{
        'review_ref': {'artifact_id': 'r', 'revision': 1}, 'work_ref': {'artifact_id': 'w', 'revision': 1},
        'step_id': 's', 'node_id': 'n'}], command_id='batch')
    with pytest.raises(HTTPException) as caught:
        await routes.material_reviews_confirm_batch('p', 'c', body, {'credential_kind': 'agent_session'})
    assert caught.value.status_code == 403


def test_other_canvas_receipt_is_rejected_before_any_batch_confirmation(tmp_path):
    from novelvideo.freezone.remix.material_execution import confirm_material_review
    from novelvideo.freezone.remix.material_review_batch import confirm_material_review_batch
    ctx, canvas, store, items = setup(tmp_path)
    other = confirm_material_review(store, items[1]['review_ref'], canvas,
        canvas_id='other-canvas', requester_user_id=ctx.requester_user_id)
    items[1]['review_ref'] = other['review_ref']
    with pytest.raises(ValueError, match='another user or canvas'):
        confirm_material_review_batch(store, ctx, canvas, canvas_id='c', items=items, command_id='batch')
    assert store.read_latest(items[0]['review_ref']['artifact_id'])['revision'] == 1


def test_pending_review_can_use_current_work_after_independent_acceptance(tmp_path):
    from novelvideo.freezone.remix.material_execution import confirm_material_review
    from novelvideo.freezone.remix.material_review_batch import confirm_material_review_batch
    from novelvideo.freezone.remix.material_work import accept_material_candidate
    ctx, canvas, store, items = setup(tmp_path)
    approved = confirm_material_review(store, items[0]['review_ref'], canvas,
        canvas_id='c', requester_user_id=ctx.requester_user_id)
    current = accept_material_candidate(store, items[0]['work_ref'], 'a', approved['review']['resource'],
                                       approved['review_ref'], 'accept-a', canvas_id='c')['work_ref']
    original = items[1]['work_ref']
    items[1]['work_ref'] = current
    result = confirm_material_review_batch(store, ctx, canvas, canvas_id='c', items=items[1:], command_id='remaining-b')
    assert result['items'][0]['review']['content']['status'] == 'passed'
    assert result['items'][0]['review']['work_ref'] == original
    assert store.read_latest(items[0]['review_ref']['artifact_id'])['revision'] == 2
