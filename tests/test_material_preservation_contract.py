"""A confirmed visual identity must reach generated media through real pixels."""
from copy import deepcopy

import pytest
from PIL import Image

from novelvideo.freezone.remix.material_work import (
    material_workflow_intent, prepare_material_work,
)
from test_material_work import setup_work


def _source(tmp_path):
    Image.new('RGB', (8, 8), 'red').save(tmp_path / 'reference.png')
    return {'nodes': [{'id': 'reference', 'type': 'imageGenNode', 'data': {
        'imageUrl': '/static/projects/project/reference.png',
    }}]}


def _input(tmp_path):
    store, plan_ref, saved = setup_work(tmp_path)
    requirements = deepcopy(store.read(saved['work_ref']['artifact_id'], 1)['payload']['requirements'])
    steps = [{'id': 'video', 'label': 'Final clip', 'media_kind': 'video',
              'source': 'generate', 'recipe_id': 'general-video', 'prompt': 'Use the current source'}]
    direction = {'preserve_replace': [{'id': 'keep-visual-a', 'entity_kind': 'visual',
                 'entity_id': 'subject-a', 'action': 'preserve', 'visual_identity': True}]}
    return store, plan_ref, requirements, steps, direction, _source(tmp_path)


def test_confirmed_visual_preservation_rejects_text_only_generation(tmp_path):
    store, plan_ref, requirements, steps, direction, canvas = _input(tmp_path)
    with pytest.raises(ValueError, match='keep-visual-a'):
        prepare_material_work(store, plan_ref, requirements, steps, 'missing-reference',
                              canvas=canvas, direction=direction)


def test_visual_preservation_requires_a_real_reference_path_and_current_hash(tmp_path):
    store, plan_ref, requirements, steps, direction, canvas = _input(tmp_path)
    requirements[0]['preservation_bindings'] = [{
        'choice_id': 'keep-visual-a', 'source_node_id': 'reference', 'role': 'identity',
    }]
    with pytest.raises(ValueError, match='reference path'):
        prepare_material_work(store, plan_ref, requirements, steps, 'no-path',
                              canvas=canvas, direction=direction)
    steps[0]['references'] = [{'node_id': 'reference', 'role': 'identity'}]
    saved = prepare_material_work(store, plan_ref, requirements, steps, 'valid-path',
                                  canvas=canvas, direction=direction)
    work = store.read(saved['work_ref']['artifact_id'], saved['work_ref']['revision'])['payload']
    assert len(material_workflow_intent(work, canvas=canvas, store=store)['items']) == 1
    Image.new('RGB', (8, 8), 'blue').save(tmp_path / 'reference.png')
    with pytest.raises(ValueError, match='source media changed'):
        material_workflow_intent(work, canvas=canvas, store=store)


def test_explicit_visual_replacement_does_not_require_an_old_source(tmp_path):
    store, plan_ref, requirements, steps, direction, canvas = _input(tmp_path)
    direction['preserve_replace'][0]['action'] = 'replace'
    saved = prepare_material_work(store, plan_ref, requirements, steps, 'new-visual',
                                  canvas=canvas, direction=direction)
    assert saved['ready_step_ids'] == ['video']


def test_intermediate_reference_keeps_visual_source_traceable(tmp_path):
    store, plan_ref, requirements, steps, direction, canvas = _input(tmp_path)
    requirements[0]['preservation_bindings'] = [{
        'choice_id': 'keep-visual-a', 'source_node_id': 'reference', 'role': 'identity',
    }]
    steps.insert(0, {'id': 'portrait', 'label': 'Derived image', 'media_kind': 'image',
                     'source': 'generate', 'recipe_id': 'general-image',
                     'prompt': 'Use the current source',
                     'references': [{'node_id': 'reference', 'role': 'identity'}]})
    steps[1]['references'] = [{'step_id': 'portrait', 'role': 'first_frame'}]
    saved = prepare_material_work(store, plan_ref, requirements, steps, 'intermediate-source',
                                  canvas=canvas, direction=direction)
    assert saved['ready_step_ids'] == ['portrait']


def test_video_source_cannot_masquerade_as_preserved_image(tmp_path):
    store, plan_ref, requirements, steps, direction, canvas = _input(tmp_path)
    requirements[0]['preservation_bindings'] = [{
        'choice_id': 'keep-visual-a', 'source_node_id': 'reference', 'role': 'identity',
    }]
    canvas['nodes'][0] = {'id': 'reference', 'type': 'videoNode', 'data': {
        'videoUrl': '/static/projects/project/source.mp4',
    }}
    steps[0]['references'] = [{'node_id': 'reference', 'role': 'identity'}]
    with pytest.raises(ValueError, match='requires an image source'):
        prepare_material_work(store, plan_ref, requirements, steps, 'video-source',
                              canvas=canvas, direction=direction)
