from copy import deepcopy

import pytest

from novelvideo.freezone.remix.material_work import _IndependentMaterialReviewContinuity
from novelvideo.freezone.remix.material_execution import confirm_material_review
from test_independent_material_review_continuity import fixture


def generated_review(tmp_path):
    store, ref, canvas, reviews = fixture(tmp_path)
    saved = store.read_latest(ref['artifact_id'])
    payload = deepcopy(saved['payload'])
    payload['steps'][1].update(source='generate', recipe_id='general-image', prompt='product',
        status='submitted', execution={'draft_id': 'native-draft', 'run_id': 'native-run'})
    saved = store.revise(saved['id'], saved['revision'], payload, saved['upstream_refs'])
    review_payload = deepcopy(reviews[1]['review'])
    review_payload['work_ref'] = {'artifact_id': saved['id'], 'revision': saved['revision']}
    for key in ('user_decision', 'decision_receipt_id'):
        review_payload.pop(key)
    review_payload.update(actor_kind='system', content={'status': 'unavailable'})
    review = store.create('material_review.v1', review_payload, [review_payload['work_ref']])
    confirmed = confirm_material_review(store, {'artifact_id': review['id'], 'revision': 1},
        canvas, canvas_id='canvas', requester_user_id='alice')
    return store, saved, store.read_latest(confirmed['review_ref']['artifact_id'])


def test_generated_review_survives_sibling_execution_registration(tmp_path):
    store, saved, review = generated_review(tmp_path)
    payload = deepcopy(saved['payload'])
    payload['steps'][2].update(status='submitted', execution={'run_id': 'sibling-run'})
    current = store.revise(saved['id'], saved['revision'], payload, saved['upstream_refs'])
    proof = _IndependentMaterialReviewContinuity(store, current, 'canvas')
    proof.validate_target(review)
    proof.validate(review)
    with pytest.raises(ValueError, match='canvas|actor'):
        _IndependentMaterialReviewContinuity(store, current, 'other').validate(review)


@pytest.mark.parametrize('change', ['prompt', 'execution', 'retry'])
def test_generated_review_cannot_survive_target_changes(tmp_path, change):
    store, saved, review = generated_review(tmp_path)
    payload = deepcopy(saved['payload'])
    target = payload['steps'][1]
    if change == 'prompt':
        target['prompt'] = 'changed product'
    elif change == 'execution':
        target['execution']['run_id'] = 'replacement-run'
    else:
        target['status'] = 'stale'
    current = store.revise(saved['id'], saved['revision'], payload, saved['upstream_refs'])
    with pytest.raises(ValueError, match='changed'):
        _IndependentMaterialReviewContinuity(store, current, 'canvas').validate(review)
