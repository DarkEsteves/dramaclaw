from copy import deepcopy

from novelvideo.freezone.remix.material_work import (
    material_stage_groups, material_workflow_intent, ready_material_steps,
)


def staged_work():
    return {'requirements': [], 'steps': [
        {'id': name, 'label': name, 'media_kind': kind, 'source': 'generate',
         'recipe_id': 'general-' + kind, 'prompt': name, 'status': 'planned',
         'depends_on': dependencies, 'references': []}
        for name, kind, dependencies in [
            ('scene', 'image', []), ('product', 'image', []),
            ('clip-scene', 'video', ['scene']), ('clip-product', 'video', ['product'])]
    ]}


def test_native_draft_contains_only_first_stage():
    work = staged_work()
    assert material_stage_groups(work) == [['scene', 'product'], ['clip-scene', 'clip-product']]
    assert [item['id'] for item in material_workflow_intent(work)['items']] == ['scene', 'product']


def test_partial_stage_acceptance_does_not_unlock_dependent_video():
    work = staged_work()
    work['steps'][0]['status'] = 'accepted'
    work['steps'][1]['status'] = 'submitted'
    assert ready_material_steps(work) == []
    assert material_workflow_intent(work)['items'] == []
    work['steps'][1]['status'] = 'accepted'
    assert ready_material_steps(work) == ['clip-scene', 'clip-product']
    assert [item['id'] for item in material_workflow_intent(work)['items']] == ['clip-scene', 'clip-product']


def test_candidate_or_technical_pass_does_not_unlock_next_stage():
    for status in ['submitted', 'candidate', 'rejected', 'stale']:
        work = deepcopy(staged_work())
        work['steps'][0]['status'] = 'accepted'
        work['steps'][1]['status'] = status
        assert 'clip-scene' not in ready_material_steps(work)
        assert all(item['id'] not in {'clip-scene', 'clip-product'}
                   for item in material_workflow_intent(work)['items'])
