from contextlib import contextmanager
from copy import deepcopy

import pytest

from test_independent_material_review_continuity import fixture, accept
from novelvideo.freezone.remix import material_work as module


def acceptance(store, current, reviews):
    return (store, current, 's1', reviews[1]['review']['resource'],
            reviews[1]['review_ref'], 'accept-1')


def test_native_history_fingerprints_run_outside_real_artifact_mutex(tmp_path, monkeypatch):
    store, original, _, reviews = fixture(tmp_path)
    current = accept(store, original, reviews, 0)['work_ref']
    locked = False
    original_lock, fingerprint = store._locked, module._fingerprint

    @contextmanager
    def real_lock():
        nonlocal locked
        with original_lock() as guard:
            locked = True
            try:
                yield guard
            finally:
                locked = False

    def outside(*args):
        assert not locked, 'historical fingerprint computed inside artifact write mutex'
        return fingerprint(*args)

    monkeypatch.setattr(store, '_locked', real_lock)
    monkeypatch.setattr(module, '_fingerprint', outside)
    result = module.accept_material_candidate(*acceptance(store, current, reviews), canvas_id='canvas')
    assert result['work_ref']['revision'] == 3


@pytest.mark.parametrize('change', ['work-head', 'plan-head', 'review-head', 'history-bytes', 'media'])
def test_snapshot_change_before_commit_is_rejected(tmp_path, change):
    store, original, _, reviews = fixture(tmp_path)
    current = accept(store, original, reviews, 0)['work_ref']
    assert hasattr(module, 'prepare_material_acceptance'), 'native acceptance needs snapshot preparation'
    prepared = module.prepare_material_acceptance(*acceptance(store, current, reviews), canvas_id='canvas')
    before = store._latest_revision(current['artifact_id'])
    if change == 'media':
        (tmp_path / reviews[1]['review']['resource']['media_path']).write_bytes(b'changed actual media')
    elif change == 'history-bytes':
        path = store._path(original['artifact_id'], 1, '.payload.json')
        path.write_bytes(path.read_bytes() + b' ')
    else:
        identity = {'work-head': current['artifact_id'],
                    'plan-head': store.read_latest(current['artifact_id'])['payload']['plan_ref']['artifact_id'],
                    'review-head': reviews[1]['review_ref']['artifact_id']}[change]
        saved = store.read_latest(identity)
        store.revise(identity, saved['revision'], deepcopy(saved['payload']), saved['upstream_refs'])
    expected = before + (change == 'work-head')
    with pytest.raises(ValueError, match='changed|snapshot|current head'):
        module.accept_material_candidate(*acceptance(store, current, reviews), canvas_id='canvas', _prepared=prepared)
    assert store._latest_revision(current['artifact_id']) == expected


def test_snapshot_cannot_be_reused_for_another_command_or_actor(tmp_path):
    store, original, _, reviews = fixture(tmp_path)
    assert hasattr(module, 'prepare_material_acceptance'), 'native acceptance needs snapshot preparation'
    args = acceptance(store, original, reviews)
    prepared = module.prepare_material_acceptance(*args, canvas_id='canvas')
    with pytest.raises(ValueError, match='snapshot|request|scope'):
        module.accept_material_candidate(*args[:-1], 'other-command', canvas_id='canvas', _prepared=prepared)
    store.created_by = 'other'
    with pytest.raises((ValueError, PermissionError), match='scope|actor'):
        module.accept_material_candidate(*args, canvas_id='canvas', _prepared=prepared)


def test_original_pending_review_remains_visible_after_independent_acceptance(tmp_path):
    from novelvideo.freezone.remix.material_execution import review_material_candidate, read_material_state

    store, original, canvas, reviews = fixture(tmp_path)
    pending = review_material_candidate(store, original, 's1', canvas, 'n1')
    current = accept(store, original, reviews, 0)['work_ref']
    state = read_material_state(store, current, canvas=canvas, canvas_id='canvas')
    row = next(row for row in state['material_reviews'] if row['review_ref'] == pending['review_ref'])
    assert row['review_compatible_with_current_work'] is True
    assert row['content']['status'] == 'unavailable'
    assert row['matches_current_work_revision'] is False
    with pytest.raises(ValueError, match='receipt|passed|human|scoped'):
        saved = store.read_latest(current['artifact_id'])
        review = store.read_latest(pending['review_ref']['artifact_id'])
        module._IndependentMaterialReviewContinuity(store, saved, 'canvas').validate(review)


def test_pending_target_continuity_rejects_target_change_and_foreign_canvas(tmp_path):
    from novelvideo.freezone.remix.material_execution import review_material_candidate

    store, original, canvas, reviews = fixture(tmp_path)
    pending = review_material_candidate(store, original, 's1', canvas, 'n1')
    current = accept(store, original, reviews, 0)['work_ref']
    saved = store.read_latest(current['artifact_id'])
    review = store.read_latest(pending['review_ref']['artifact_id'])
    assert hasattr(module._IndependentMaterialReviewContinuity, 'validate_target'), 'display needs exact target continuity'
    module._IndependentMaterialReviewContinuity(store, saved, 'canvas').validate_target(review)
    with pytest.raises(ValueError, match='canvas|receipt|scoped'):
        module._IndependentMaterialReviewContinuity(store, saved, 'other').validate_target(review)
    payload = deepcopy(saved['payload'])
    payload['steps'][1]['label'] = 'changed target'
    changed = store.revise(saved['id'], saved['revision'], payload, saved['upstream_refs'])
    with pytest.raises(ValueError, match='history|changed|native'):
        module._IndependentMaterialReviewContinuity(store, changed, 'canvas').validate_target(review)


@pytest.mark.parametrize('change', ['media', 'history'])
def test_final_publication_guard_rechecks_content_after_precompute(tmp_path, monkeypatch, change):
    store, original, _, reviews = fixture(tmp_path)
    current = accept(store, original, reviews, 0)['work_ref']
    prepared = module.prepare_material_acceptance(*acceptance(store, current, reviews), canvas_id='canvas')
    atomic = store._atomic_json

    def changed_before_publication(path, value, guard):
        if path.name == '3.payload.json':
            if change == 'media':
                (tmp_path / reviews[1]['review']['resource']['media_path']).write_bytes(b'changed before atomic replace')
            else:
                historical = store._path(original['artifact_id'], 1, '.payload.json')
                historical.write_bytes(historical.read_bytes() + b' ')
        return atomic(path, value, guard)

    monkeypatch.setattr(store, '_atomic_json', changed_before_publication)
    with pytest.raises(ValueError, match='changed|snapshot'):
        module.accept_material_candidate(*acceptance(store, current, reviews), canvas_id='canvas', _prepared=prepared)
    assert store._latest_revision(current['artifact_id']) == current['revision']


def test_snapshot_preparation_has_an_aggregate_content_bound(tmp_path, monkeypatch):
    store, original, _, reviews = fixture(tmp_path)
    monkeypatch.setattr(module, 'MAX_ACCEPTANCE_SNAPSHOT_BYTES', 1, raising=False)
    with pytest.raises(ValueError, match='snapshot.*bound'):
        module.prepare_material_acceptance(*acceptance(store, original, reviews), canvas_id='canvas')


def test_two_real_mutex_commits_of_same_snapshot_publish_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    store, original, _, reviews = fixture(tmp_path)
    prepared = module.prepare_material_acceptance(*acceptance(store, original, reviews), canvas_id='canvas')

    def commit(_):
        try:
            return module.accept_material_candidate(*acceptance(store, original, reviews), canvas_id='canvas', _prepared=prepared)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(commit, range(2)))
    assert sum(result is not None for result in results) == 1
    assert store._latest_revision(original['artifact_id']) == 2


def test_snapshot_preparation_rejects_foreign_work_actor(tmp_path):
    import json

    store, original, _, reviews = fixture(tmp_path)
    header_path = store._path(original['artifact_id'], 1, '.json')
    header = json.loads(header_path.read_text())
    header['created_by'] = 'other'
    header_path.write_text(json.dumps(header))
    with pytest.raises((PermissionError, ValueError), match='actor|scope'):
        module.prepare_material_acceptance(*acceptance(store, original, reviews), canvas_id='canvas')


@pytest.mark.parametrize('prepared', [False, True])
@pytest.mark.parametrize('defaults', ['missing', 'none'])
def test_acceptance_normalizes_valid_default_resource_fields(tmp_path, prepared, defaults):
    store, original, _, reviews = fixture(tmp_path)
    resource = deepcopy(reviews[1]['review']['resource'])
    if defaults == 'missing':
        resource.pop('has_audio')
    else:
        resource['node_revision'] = None
    args = (store, original, 's1', resource, reviews[1]['review_ref'], 'normalized-default')
    snapshot = module.prepare_material_acceptance(*args, canvas_id='canvas') if prepared else None
    result = module.accept_material_candidate(*args, canvas_id='canvas', _prepared=snapshot)
    assert result['work_ref']['revision'] == 2
    saved = store.read_latest(original['artifact_id'])
    assert saved['payload']['steps'][1]['accepted_resource'] == reviews[1]['review']['resource']
