"""Original user reviews survive only genuine independent acceptance bookkeeping."""
from copy import deepcopy
from types import SimpleNamespace
from pathlib import Path

import pytest
from PIL import Image

from novelvideo.freezone.remix.material_execution import (
    confirm_material_review, read_material_state, review_material_candidate,
)
from novelvideo.freezone.remix.material_work import accept_material_candidate, prepare_material_work
from novelvideo.freezone.remix.store import RemixArtifactStore
from test_material_work import plan_payload


def fixture(tmp_path):
    store = RemixArtifactStore(tmp_path, 'project', created_by='alice')
    plan = plan_payload()
    plan['placements'] = [{**plan['placements'][0], 'id': f'p{i}'} for i in range(3)]
    saved = store.create('production_plan.v2', plan, [])
    requirements = [{'id': f'r{i}', 'placement_ids': [f'p{i}'], 'description': '实际画面',
                     'media_kind': 'image', 'final_step_id': f's{i}'} for i in range(3)]
    steps = [{'id': f's{i}', 'label': f'画面{i}', 'media_kind': 'image', 'source': 'existing',
              'node_id': f'n{i}'} for i in range(3)]
    prepared = prepare_material_work(store, {'artifact_id': saved['id'], 'revision': 1}, requirements, steps, 'prepare')
    canvas = {'project_id': 'project', 'nodes': [{'id': 'plan', 'type': 'scriptNode',
              'data': {'productionPlanV2Ref': {'artifactId': saved['id'], 'revision': 1}}}]}
    reviews = []
    for i, color in enumerate(['red', 'green', 'blue']):
        Image.new('RGB', (16, 12), color).save(tmp_path / f'{i}.png')
        canvas['nodes'].append({'id': f'n{i}', 'data': {'imageUrl': f'/static/projects/project/{i}.png'}})
    for i in range(3):
        review = review_material_candidate(store, prepared['work_ref'], f's{i}', canvas, f'n{i}')
        reviews.append(confirm_material_review(store, review['review_ref'], canvas,
                                               canvas_id='canvas', requester_user_id='alice'))
    return store, prepared['work_ref'], canvas, reviews


def accept(store, ref, reviews, i):
    return accept_material_candidate(store, ref, f's{i}', reviews[i]['review']['resource'],
                                     reviews[i]['review_ref'], f'accept-{i}', canvas_id='canvas')


def test_three_original_user_reviews_accepted_without_rewriting_evidence(tmp_path):
    store, original, canvas, reviews = fixture(tmp_path)
    before = {str(path): path.read_bytes() for review in reviews
              for path in store._directory(review['review_ref']['artifact_id']).iterdir()}
    ref = original
    for i in range(3):
        result = accept(store, ref, reviews, i)
        ref = result['work_ref']
        assert ref['revision'] == i + 2
    assert all(store.read_latest(ref['artifact_id'])['payload']['steps'][i]['status'] == 'accepted' for i in range(3))
    assert all(Path(path).read_bytes() == value for path, value in before.items())


def test_discovery_certifies_old_basis_without_relabeling_it(tmp_path):
    store, original, canvas, reviews = fixture(tmp_path)
    ref = accept(store, original, reviews, 0)['work_ref']
    state = read_material_state(store, ref, canvas=canvas, canvas_id='canvas')
    row = next(row for row in state['material_reviews'] if row['step_id'] == 's1')
    assert row['work_ref'] == original and row['matches_current_work_revision'] is False
    assert row['review_compatible_with_current_work'] is True
    assert row['review_ref'] == reviews[1]['review_ref']


@pytest.mark.asyncio
async def test_route_forwards_trusted_canvas_scope_and_uses_locked_current_media(tmp_path, monkeypatch):
    from novelvideo.api.routes import reference_material as route
    from novelvideo.freezone import canvas_store
    store, ref, canvas, reviews = fixture(tmp_path)
    ref = accept(store, ref, reviews, 0)['work_ref']
    ctx = SimpleNamespace(state_dir=tmp_path, project_id='project', requester_user_id='alice')
    async def scope(*args):
        return ctx, canvas, store, read_material_state(store, ref)
    async def notify(*args, **kwargs):
        pass
    monkeypatch.setattr(route, '_scope', scope)
    monkeypatch.setattr(route, '_notify_material', notify)
    monkeypatch.setattr(canvas_store, 'read_canvas', lambda *args: deepcopy(canvas))
    body = route.AcceptBody(work_ref=ref, step_id='s1', resource=reviews[1]['review']['resource'],
                            review_ref=reviews[1]['review_ref'], command_id='accept-1')
    result = await route.material_accept('project', 'canvas', body, {'user_id': 'alice'})
    assert result['data']['work_ref']['revision'] == 3


def snapshot(store):
    return {str(path): path.read_bytes() for path in store.root.rglob('*.json')}


def revise_work(store, ref, change=lambda payload: None):
    saved = store.read_latest(ref['artifact_id'])
    payload = deepcopy(saved['payload'])
    change(payload)
    revised = store.revise(saved['id'], saved['revision'], payload, saved['upstream_refs'])
    return {'artifact_id': revised['id'], 'revision': revised['revision']}


@pytest.mark.parametrize('change', ['no-scope', 'canvas', 'user', 'foreign-work', 'stale-head',
                                   'file', 'missing-receipt', 'revoked', 'later-review', 'latest-plan'])
def test_historical_scope_head_file_and_original_receipt_remain_strict(tmp_path, change):
    store, original, canvas, reviews = fixture(tmp_path)
    current = accept(store, original, reviews, 0)['work_ref']
    scope, ref, review_ref = 'canvas', current, reviews[1]['review_ref']
    if change == 'no-scope': scope = None
    elif change == 'canvas': scope = 'other'
    elif change == 'user': store.created_by = 'bob'
    elif change == 'foreign-work':
        old = store.read_latest(current['artifact_id'])
        other = store.create('material_work_package.v1', old['payload'], old['upstream_refs'])
        ref = {'artifact_id': other['id'], 'revision': 1}
    elif change == 'stale-head': ref = original
    elif change == 'file': Image.new('RGB', (16, 12), 'yellow').save(tmp_path / '1.png')
    elif change == 'latest-plan':
        plan = store.read_latest(store.read_latest(current['artifact_id'])['payload']['plan_ref']['artifact_id'])
        store.revise(plan['id'], plan['revision'], plan['payload'], plan['upstream_refs'])
    else:
        saved = store.read_latest(review_ref['artifact_id'])
        payload = deepcopy(saved['payload'])
        if change == 'missing-receipt':
            payload.pop('user_decision'); payload.pop('decision_receipt_id')
            payload['actor_kind'] = 'system'
            payload['content'] = {'status': 'unavailable', 'checks': [], 'findings': []}
        elif change == 'revoked': payload['content']['status'] = 'rejected'
        revised = store.revise(saved['id'], saved['revision'], payload, saved['upstream_refs'])
        if change == 'missing-receipt': review_ref = {'artifact_id': revised['id'], 'revision': revised['revision']}
    before = snapshot(store)
    with pytest.raises((ValueError, FileNotFoundError)):
        accept_material_candidate(store, ref, 's1', reviews[1]['review']['resource'], review_ref,
                                  'reject', canvas_id=scope)
    assert snapshot(store) == before


@pytest.mark.parametrize('change', ['no-op', 'target', 'parameters', 'reference', 'dependency',
                                   'source', 'recipe', 'retry', 'changed-command', 'changed-result',
                                   'extra-receipt', 'upstream', 'requirements', 'fingerprint', 'typed-parameters',
                                   'typed-receipt'])
def test_non_native_or_changed_history_never_certifies_original_review(tmp_path, change):
    from novelvideo.freezone.remix.material_work import retry_material_step
    store, original, canvas, reviews = fixture(tmp_path)
    current = accept(store, original, reviews, 0)['work_ref']
    if change == 'retry':
        current = retry_material_step(store, current, 's2', '实际修改', 'retry')['work_ref']
    elif change == 'upstream':
        saved = store.read_latest(current['artifact_id'])
        changed = store.revise(saved['id'], saved['revision'], saved['payload'], saved['upstream_refs'][:-1])
        current = {'artifact_id': changed['id'], 'revision': changed['revision']}
    elif change in {'requirements', 'fingerprint', 'changed-command', 'changed-result', 'extra-receipt', 'typed-receipt'}:
        # Isolated corrupted storage, with a valid header hash, exercises the host
        # proof rather than create/revise's pre-existing lineage gate.
        import hashlib, json
        path = store._path(current['artifact_id'], current['revision'], '.payload.json')
        payload = json.loads(path.read_text())
        if change == 'requirements': payload['requirements'][1]['description'] = '变更要求'
        elif change == 'fingerprint': payload['input_fingerprint'] = '0' * 64
        elif change == 'changed-command': payload['command_receipts'][-1]['request']['step_id'] = 's2'
        elif change == 'changed-result': payload['command_receipts'][-1]['result']['ready_step_ids'] = []
        elif change == 'typed-receipt': payload['command_receipts'][-1]['request']['work_ref']['revision'] = True
        else: payload['command_receipts'].append(deepcopy(payload['command_receipts'][-1]))
        path.write_bytes(store._canonical(payload))
        header_path = store._path(current['artifact_id'], current['revision'], '.json')
        header = json.loads(header_path.read_text())
        header['content_hash'] = 'sha256:' + hashlib.sha256(store._canonical(payload)).hexdigest()
        header_path.write_bytes(store._canonical(header))
    else:
        def modify(payload):
            target = payload['steps'][1]
            if change == 'target': target['label'] = '已改'
            elif change == 'parameters': target['generation_inputs'] = {'quality': '480p'}
            elif change == 'reference': target['references'] = [{'node_id': 'n0', 'role': 'identity'}]
            elif change == 'dependency': target['depends_on'] = ['s0']
            elif change == 'source': target['source'] = 'upload'
            elif change == 'recipe': target.update(source='generate', recipe_id='general-image', prompt='changed')
            elif change == 'typed-parameters': target['generation_inputs'] = {'count': True}
        current = revise_work(store, current, modify)
    before = snapshot(store)
    with pytest.raises((ValueError, FileNotFoundError)):
        accept(store, current, reviews, 1)
    assert snapshot(store) == before
    state = read_material_state(store, current, canvas=canvas, canvas_id='canvas', include_workflow_intent=False)
    row = next(row for row in state['material_reviews'] if row['step_id'] == 's1')
    assert row['review_compatible_with_current_work'] is False


def test_changed_then_restored_target_still_invalidates_original_review(tmp_path):
    store, original, canvas, reviews = fixture(tmp_path)
    current = accept(store, original, reviews, 0)['work_ref']
    changed = revise_work(store, current, lambda p: p['steps'][1].update(prompt='changed'))
    restored = revise_work(store, changed, lambda p: p['steps'][1].update(prompt=''))
    before = snapshot(store)
    with pytest.raises(ValueError): accept(store, restored, reviews, 1)
    assert snapshot(store) == before


@pytest.mark.parametrize('field', ['canvas_id', 'requester_user_id', 'project_id', 'media_hash', 'digest', 'header-actor', 'header-project'])
def test_fabricated_original_receipt_or_foreign_header_rejected(tmp_path, field):
    import hashlib, json
    store, original, canvas, reviews = fixture(tmp_path)
    current = accept(store, original, reviews, 0)['work_ref']
    ref = reviews[1]['review_ref']
    path = store._path(ref['artifact_id'], ref['revision'], '.payload.json')
    payload = json.loads(path.read_text())
    header_path = store._path(ref['artifact_id'], ref['revision'], '.json')
    header = json.loads(header_path.read_text())
    if field == 'digest': payload['decision_receipt_id'] = '0' * 64
    elif field.startswith('header-'):
        header[{'header-actor': 'created_by', 'header-project': 'project_id'}[field]] = 'foreign'
    else: payload['user_decision'][field] = 'foreign'
    path.write_bytes(store._canonical(payload))
    header['content_hash'] = 'sha256:' + hashlib.sha256(store._canonical(payload)).hexdigest()
    header_path.write_bytes(store._canonical(header))
    before = snapshot(store)
    with pytest.raises((ValueError, FileNotFoundError)):
        accept(store, current, reviews, 1)
    assert snapshot(store) == before


def test_historical_proof_is_bounded_before_out_of_range_history_io(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.material_work import _IndependentMaterialReviewContinuity
    store, original, canvas, reviews = fixture(tmp_path)
    current = deepcopy(store.read_latest(original['artifact_id']))
    current['revision'] = 202
    review = store.read_latest(reviews[1]['review_ref']['artifact_id'])
    read = store._read
    def guarded(artifact_id, revision):
        assert not (artifact_id == current['id'] and revision < 2), 'unbounded history I/O'
        return read(artifact_id, revision)
    monkeypatch.setattr(store, '_read', guarded)
    with pytest.raises(ValueError, match='bounded proof'):
        _IndependentMaterialReviewContinuity(store, current, 'canvas').validate(review)


def test_canvas_publication_fence_failure_prevents_native_accept_write(tmp_path):
    store, original, canvas, reviews = fixture(tmp_path)
    current = accept(store, original, reviews, 0)['work_ref']
    class LostFence:
        def reassert(self): raise ValueError('lost current canvas fence')
    before = snapshot(store)
    with pytest.raises(ValueError, match='lost current canvas fence'):
        accept_material_candidate(store, current, 's1', reviews[1]['review']['resource'], reviews[1]['review_ref'],
                                  'accept-1', canvas_id='canvas', _canvas_guard=LostFence())
    assert snapshot(store) == before


@pytest.mark.parametrize('source', ['upload', 'generate'])
def test_historical_user_confirmation_does_not_generalize_other_source_types(tmp_path, source):
    store, original, canvas, reviews = fixture(tmp_path)
    def modify(payload):
        payload['steps'][1]['source'] = source
        if source == 'generate': payload['steps'][1].update(recipe_id='general-image', prompt='actual prompt')
    basis = revise_work(store, original, modify)
    payload = deepcopy(reviews[1]['review'])
    payload['work_ref'] = basis
    for key in ('user_decision', 'decision_receipt_id'): payload.pop(key)
    payload['actor_kind'] = 'system'; payload['content']['status'] = 'unavailable'
    saved = store.create('material_review.v1', payload, [basis])
    review = confirm_material_review(store, {'artifact_id': saved['id'], 'revision': 1}, canvas,
                                     canvas_id='canvas', requester_user_id='alice')
    current_review = review_material_candidate(store, basis, 's0', canvas, 'n0')
    reviews[0] = confirm_material_review(store, current_review['review_ref'], canvas,
                                         canvas_id='canvas', requester_user_id='alice')
    reviews[1] = review
    current = accept(store, basis, reviews, 0)['work_ref']
    before = snapshot(store)
    with pytest.raises(ValueError, match='independent existing|submitted native execution'):
        accept(store, current, reviews, 1)
    assert snapshot(store) == before


def test_original_command_replay_and_current_head_rebinding_remain_unchanged(tmp_path):
    store, original, canvas, reviews = fixture(tmp_path)
    first = accept(store, original, reviews, 0)
    second = accept(store, first['work_ref'], reviews, 1)
    assert accept(store, first['work_ref'], reviews, 1) == second
    before = snapshot(store)
    with pytest.raises(ValueError, match='different inputs'):
        accept_material_candidate(store, second['work_ref'], 's1', reviews[1]['review']['resource'],
                                  reviews[1]['review_ref'], 'accept-1', canvas_id='canvas')
    assert snapshot(store) == before


def test_discovery_caches_history_reads_and_preserves_a_separate_unavailable_review(tmp_path, monkeypatch):
    from collections import Counter
    store, original, canvas, reviews = fixture(tmp_path)
    current = accept(store, original, reviews, 0)['work_ref']
    unavailable = review_material_candidate(store, current, 's1', canvas, 'n1')
    counts = Counter()
    original_read = store._read
    def read(artifact_id, revision):
        counts[(artifact_id, revision)] += 1
        return original_read(artifact_id, revision)
    monkeypatch.setattr(store, '_read', read)
    state = read_material_state(store, current, canvas=canvas, canvas_id='canvas')
    old = next(row for row in state['material_reviews'] if row['review_ref'] == reviews[1]['review_ref'])
    new = next(row for row in state['material_reviews'] if row['review_ref'] == unavailable['review_ref'])
    assert old['review_compatible_with_current_work'] is True
    assert new['content']['status'] == 'unavailable'
    assert all(count <= 1 for count in counts.values())


def test_successful_lineage_and_real_fingerprint_are_linear_in_distinct_work_revisions(tmp_path, monkeypatch):
    from collections import Counter
    from novelvideo.freezone.remix import material_work as module
    store, original, canvas, reviews = fixture(tmp_path)
    current = accept(store, original, reviews, 0)['work_ref']
    current = accept(store, current, reviews, 1)['work_ref']
    lineage_calls, fingerprint_calls = Counter(), []
    lineage, fingerprint = module.validate_material_work_lineage, module._fingerprint
    def validate(store, payload, refs):
        lineage_calls[id(payload)] += 1
        return lineage(store, payload, refs)
    def measure(*args):
        fingerprint_calls.append(1)
        return fingerprint(*args)
    monkeypatch.setattr(module, 'validate_material_work_lineage', validate)
    monkeypatch.setattr(module, '_fingerprint', measure)
    with store._locked():
        saved = store._read(current['artifact_id'], current['revision'])
        review = store._read(reviews[2]['review_ref']['artifact_id'], reviews[2]['review_ref']['revision'])
        helper = module._IndependentMaterialReviewContinuity(store, saved, 'canvas')
        helper.validate(review)
        # Repeated work access inside this same operation still checks immutable
        # cached identities, without rehashing all requirements again.
        for revision in range(1, 4):
            helper._work(revision)
        assert len(lineage_calls) == 3
        assert set(lineage_calls.values()) == {1}
        assert len(fingerprint_calls) == 3


def test_failed_work_lineage_is_never_cached_as_valid(tmp_path, monkeypatch):
    from novelvideo.freezone.remix import material_work as module
    store, original, canvas, reviews = fixture(tmp_path)
    calls = []
    validate = module.validate_material_work_lineage
    def measured(*args):
        calls.append(1)
        return validate(*args)
    monkeypatch.setattr(module, 'validate_material_work_lineage', measured)
    with store._locked():
        saved = store._read(original['artifact_id'], original['revision'])
        invalid = deepcopy(saved)
        invalid['payload']['input_fingerprint'] = '0' * 64
        helper = module._IndependentMaterialReviewContinuity(store, invalid, 'canvas')
        for _ in range(2):
            with pytest.raises(ValueError, match='input differs'):
                helper._work(original['revision'])
        assert len(calls) == 2


@pytest.mark.parametrize('change,status', [('output', 409), ('private', 403), ('project', 404), ('plan', 404)])
@pytest.mark.asyncio
async def test_accept_route_rechecks_locked_actual_scope_and_media(tmp_path, monkeypatch, change, status):
    from fastapi import HTTPException
    from novelvideo.api.routes import reference_material as route
    from novelvideo.freezone import canvas_store
    store, original, canvas, reviews = fixture(tmp_path)
    current = accept(store, original, reviews, 0)['work_ref']
    ctx = SimpleNamespace(state_dir=tmp_path, project_id='project', requester_user_id='alice')
    async def scope(*args): return ctx, canvas, store, read_material_state(store, current)
    latest = deepcopy(canvas)
    if change == 'output': latest['nodes'][2]['data']['imageUrl'] = '/static/projects/project/2.png'
    elif change == 'private': latest.update(access_model='private', owner_principal_id='bob')
    elif change == 'project': latest['project_id'] = 'other'
    else: latest['nodes'] = latest['nodes'][1:]
    monkeypatch.setattr(route, '_scope', scope)
    monkeypatch.setattr(canvas_store, 'read_canvas', lambda *args: latest)
    before = snapshot(store)
    body = route.AcceptBody(work_ref=current, step_id='s1', resource=reviews[1]['review']['resource'],
                            review_ref=reviews[1]['review_ref'], command_id='accept-1')
    with pytest.raises(HTTPException) as rejected:
        await route.material_accept('project', 'canvas', body, {'user_id': 'alice'})
    assert rejected.value.status_code == status
    assert snapshot(store) == before
