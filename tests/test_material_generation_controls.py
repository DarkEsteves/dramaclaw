from copy import deepcopy

import pytest

from test_html_workflow_intent import private_recipe
from test_material_work import setup_work
from novelvideo.freezone.agent_workflows import catalog
from novelvideo.freezone.remix.material_work import (
    material_workflow_intent, prepare_material_work, missing_material_parameter_choices,
    material_duration_issues,
    material_parameter_conflicts,
)
from novelvideo.freezone.remix.material_execution import attach_material_execution_draft, read_material_state
from novelvideo.freezone.workflow_drafts import create_workflow_draft, read_workflow_draft


CONTROLS = {'aspectRatio': '9:16', 'count': 1, 'durationSec': 5, 'genMode': 'allReference',
            'generateAudio': True, 'model': 'seedance-2.0-mini', 'quality': '480p'}


def step(identity='video', controls=None, kind='video'):
    return {'id': identity, 'label': identity, 'source': 'generate', 'status': 'planned',
            'media_kind': kind, 'recipe_id': 'general-' + kind, 'prompt': 'Guest answers; estimated 12 seconds',
            'generation_inputs': deepcopy(CONTROLS if controls is None else controls)}


def test_material_generation_requires_submitted_model_ratio_and_resolution():
    choices = {'video_model': 'chosen', 'video_aspect_ratio': '9:16',
               'video_resolution': '720P'}
    video = step(controls={'durationSec': 5})
    assert missing_material_parameter_choices([video], {}) == {
        'video': ['model', 'aspect_ratio', 'resolution']}
    assert missing_material_parameter_choices([video], choices) == {}
    assert missing_material_parameter_choices([{'source': 'existing', 'media_kind': 'image'}], {}) == {}
    assert missing_material_parameter_choices([step('image', {}, 'image')], choices) == {
        'image': ['model', 'aspect_ratio', 'resolution']}


def test_material_duration_errors_are_returned_per_step_without_a_global_duration():
    steps = [step('short', {'durationSec': 2}), step('missing', {}),
             step('valid', {'durationSec': 5}), step('image', {}, 'image')]
    assert [issue['step_id'] for issue in material_duration_issues(steps)] == ['short', 'missing']
    assert all(issue['minimum'] == 4 and issue['maximum'] == 15
               for issue in material_duration_issues(steps))


def test_agent_step_cannot_override_saved_user_model_choice():
    video = step(controls={'model': 'agent-model', 'durationSec': 5})
    conflicts = material_parameter_conflicts([video], {
        'video_model': 'user-model', 'video_aspect_ratio': '9:16',
        'video_resolution': '720P'})
    assert conflicts == [{'step_id': 'video', 'field': 'video_model',
                          'chosen': 'user-model', 'proposed': 'agent-model'}]


def test_saved_user_model_replaces_symbolic_recommendation(tmp_path):
    store, plan_ref, saved = setup_work(tmp_path)
    requirement = deepcopy(store.read(saved['work_ref']['artifact_id'], 1)['payload']['requirements'])
    requested = step(controls={'model': 'recommended', 'durationSec': 5})
    prepared = prepare_material_work(
        store, plan_ref, requirement, [requested], 'replace-recommendation',
        confirmed_choices={'video_model': 'user-model', 'video_aspect_ratio': '9:16',
                           'video_resolution': '720P'},
    )
    saved_work = store.read_latest(prepared['work_ref']['artifact_id'])['payload']
    assert saved_work['steps'][0]['generation_inputs']['video_model'] == 'user-model'
    assert saved_work['steps'][0]['generation_inputs'].get('model') != 'recommended'


@pytest.fixture
def native_catalog(monkeypatch):
    recipes = [private_recipe('general-' + kind, kind) for kind in ('image', 'video')]
    skill = {'id': 'reference-video-remix', 'version': '1', 'allowed_recipe_ids': [r['id'] for r in recipes]}
    monkeypatch.setattr(catalog, '_load_skill', lambda _: skill)
    monkeypatch.setattr(catalog, '_load_agent_config_items', lambda kind, *roots: recipes if kind == 'recipes' else [skill])


@pytest.mark.parametrize('audio', [True, False])
def test_saved_seven_controls_reach_actual_native_video_and_media_edge(native_catalog, audio):
    clip = step(controls={**CONTROLS, 'generateAudio': audio})
    clip['references'] = [{'node_id': 'guest', 'role': 'identity'}]
    canvas = {'nodes': [{'id': 'guest', 'type': 'imageGenNode', 'data': {'imageUrl': '/static/projects/p/guest.png'}}]}
    work = {'steps': [clip]}
    before = deepcopy((work, canvas))
    intent = material_workflow_intent(work, canvas=canvas)
    compiled = catalog.compile_workflow_intent(intent)
    assert compiled['ok'], compiled
    node = next(n for n in compiled['plan']['nodes'] if n['id'] == 'video')
    assert {key: node['data'].get(key) for key in CONTROLS} == {**CONTROLS, 'generateAudio': audio}
    assert intent['items'][0]['duration_seconds'] == 5
    assert {'source': intent['external_inputs'][0]['id'], 'target': 'video', 'link_type': 'media_input_for'} in compiled['plan']['edges']
    assert (work, canvas) == before
    assert not catalog._video_generation_mode_blockers([node], intent['inputs'], {})
    changed = deepcopy(node)
    changed['data']['genMode'] = 'textToVideo'
    assert catalog._video_generation_mode_blockers([changed], intent['inputs'], {})[0]['code'] == 'video_generation_mode_conflict'


def test_explicit_text_mode_with_real_media_edge_remains_blocked_by_native_preflight(native_catalog):
    from novelvideo.freezone.workflow_preflight import evaluate_workflow_preflight
    clip = step(controls={**CONTROLS, 'genMode': 'textToVideo'})
    clip['references'] = [{'node_id': 'guest', 'role': 'identity'}]
    canvas = {'nodes': [{'id': 'guest', 'type': 'imageGenNode', 'data': {'imageUrl': '/static/projects/p/guest.png'}}]}
    intent = material_workflow_intent({'steps': [clip]}, canvas=canvas)
    compiled = catalog.compile_workflow_intent(intent)
    assert compiled['ok'], compiled
    preflight = evaluate_workflow_preflight(compiled, model_responses={}, limits={}, runtime_available=False)
    assert preflight['status'] == 'blocked'
    blocker = next(item for item in preflight['blockers'] if item['code'] == 'media_reference_mode_conflict')
    assert blocker['recovery'] == 'confirm_reference_mode'
    assert 'textToVideo' in blocker['message'] and 'does not consume media' in blocker['message']
    assert 'mode_confirmations' not in intent


def test_image_and_video_prefixes_can_share_wave_without_conflation(native_catalog):
    image = step('image', {'model': 'image-model', 'aspect_ratio': '1:1', 'size': '1K', 'quality': 'high', 'count': 2}, 'image')
    intent = material_workflow_intent({'steps': [image, step()]})
    compiled = catalog.compile_workflow_intent(intent)
    assert compiled['ok'], compiled
    node = next(n for n in compiled['plan']['nodes'] if n['id'] == 'image')
    assert {key: node['data'].get(key) for key in ('model', 'aspectRatio', 'size', 'quality', 'count')} == {
        'model': 'image-model', 'aspectRatio': '1:1', 'size': '1K', 'quality': 'high', 'count': 2}
    assert intent['inputs']['video_aspect_ratio'] == '9:16'


def test_missing_material_model_uses_native_catalog_recommendation_without_overriding_choice():
    image = step('image-a', {'aspect_ratio': '9:16'}, 'image')
    chosen = step('image-b', {'model': 'chosen-model'}, 'image')
    first = material_workflow_intent({'steps': [image]})
    second = material_workflow_intent({'steps': [chosen]})
    assert first['items'][0]['model'] == 'recommended'
    assert second['items'][0]['model'] == 'chosen-model'


def test_saved_user_model_choice_fills_missing_step_fields_without_replacing_explicit_values(tmp_path):
    store, plan_ref, saved = setup_work(tmp_path)
    requirement = deepcopy(store.read(saved['work_ref']['artifact_id'], 1)['payload']['requirements'])
    requested = step('video', {'aspectRatio': '9:16'})
    before = deepcopy(requested)
    choices = {'video_model': 'saved-model', 'video_aspect_ratio': '1:1',
               'video_resolution': '480p'}
    prepared = prepare_material_work(
        store, plan_ref, requirement, [requested], 'saved-choice',
        confirmed_choices=choices,
    )
    work = store.read(prepared['work_ref']['artifact_id'], prepared['work_ref']['revision'])['payload']
    actual = work['steps'][0]['generation_inputs']
    assert actual['video_model'] == 'saved-model'
    assert actual['aspectRatio'] == '9:16'
    assert actual['video_resolution'] == '480p'
    assert 'video_aspect_ratio' not in actual
    assert requested == before


def test_dependent_video_waits_for_image_acceptance(native_catalog):
    image = step('ref_guest', {'aspectRatio': '9:16', 'count': 1, 'model': 'image-model',
                               'size': '1K', 'quality': 'high'}, 'image')
    video = step('shot_1')
    video['depends_on'] = ['ref_guest']
    video['references'] = [{'step_id': 'ref_guest', 'role': 'identity'}]
    intent = material_workflow_intent({'steps': [image, video]})
    assert [item['id'] for item in intent['items']] == ['ref_guest']
    assert intent['external_inputs'] == []
    compiled = catalog.compile_workflow_intent(intent)
    assert compiled['ok'], compiled
    assert {node['id'] for node in compiled['plan']['nodes']
            if node['node_type'] == 'imageGenNode'} == {'ref_guest'}
    assert not any(node['node_type'] == 'videoNode' for node in compiled['plan']['nodes'])


def test_dependents_wait_when_an_incoherent_prerequisite_leaves_the_wave():
    first = step('ref_a', {'model': 'image-model'}, 'image')
    other = step('ref_b', {'model': 'image-model-2'}, 'image')
    video = step('shot_1')
    video['references'] = [{'step_id': 'ref_b', 'role': 'identity'}]
    intent = material_workflow_intent({'steps': [first, other, video]})
    assert [item['id'] for item in intent['items']] == ['ref_a']


def test_unconfirmed_draft_returns_to_pending_so_the_whole_wave_is_replanned(tmp_path):
    store, plan_ref, prepared = setup_work(tmp_path)
    work = store.read(prepared['work_ref']['artifact_id'], 1)['payload']
    intent = material_workflow_intent(work)
    first = create_workflow_draft(project_dir=tmp_path, project_id='project', canvas_id='canvas',
                                  intent=intent, compiled={'ok': True, 'plan': {'nodes': []}})
    attached = attach_material_execution_draft(
        store, prepared['work_ref'], canvas_id='canvas', draft_id=first['draft_id'],
        draft_revision=1, workflow_state_dir=tmp_path)
    state = read_material_state(store, attached['work_ref'], canvas_id='canvas',
                                workflow_state_dir=tmp_path)
    assert state['material_lifecycle']['canvas_delivery_status'] == 'ready_to_submit'
    assert state['material_lifecycle']['generation_status'] == 'not_started'
    assert state['reopenable_step_ids'] == ['image']
    assert [item['id'] for item in state['workflow_intent']['items']] == ['image']
    replanned = material_workflow_intent(state['work'], reopenable_step_ids=state['reopenable_step_ids'])
    second = create_workflow_draft(project_dir=tmp_path, project_id='project', canvas_id='canvas',
                                   intent=replanned, compiled={'ok': True, 'plan': {'nodes': []}})
    again = attach_material_execution_draft(
        store, {'artifact_id': attached['work_ref']['artifact_id'],
                'revision': attached['work_ref']['revision']},
        canvas_id='canvas', draft_id=second['draft_id'], draft_revision=1,
        workflow_state_dir=tmp_path)
    assert {step['id']: step['status'] for step in again['work']['steps']} == {
        'image': 'submitted', 'video': 'planned'}
    superseded, _ = read_workflow_draft(project_dir=tmp_path, canvas_id='canvas',
                                        draft_id=first['draft_id'])
    assert superseded['status'] == 'cancelled'


def test_coherent_wave_selects_stable_first_controls_and_keeps_per_item_durations(native_catalog):
    work = {'steps': [step('first'), step('different', {**CONTROLS, 'generateAudio': False}),
                      step('longer', {**CONTROLS, 'durationSec': 8}), step('legacy', {})]}
    before = deepcopy(work)
    intent = material_workflow_intent(work)
    assert [i['id'] for i in intent['items']] == ['first', 'longer']
    assert 'video_duration_seconds' not in intent['inputs']
    compiled = catalog.compile_workflow_intent(intent)
    assert compiled['ok'], compiled
    assert {n['id']: n['data']['durationSec'] for n in compiled['plan']['nodes'] if n['node_type'] == 'videoNode'} == {'first': 5, 'longer': 8}
    assert work == before


@pytest.mark.parametrize('controls', [
    {**CONTROLS, 'generateAudio': 1}, {**CONTROLS, 'count': True}, {**CONTROLS, 'count': 3},
    {**CONTROLS, 'durationSec': 2.5}, {**CONTROLS, 'durationSec': 601},
    {**CONTROLS, 'durationSec': '5'}, {**CONTROLS, 'genMode': 'invented'},
    {**CONTROLS, 'video_generate_audio': False}, {**CONTROLS, 'aspectRatio': ''},
    {**CONTROLS, 'unknownProviderOption': 'value'}, {**CONTROLS, 'mode_confirmations': {}},
])
def test_unrepresentable_controls_fail_without_mutating_saved_spec(controls):
    work = {'steps': [step(controls=controls)]}
    before = deepcopy(work)
    with pytest.raises(ValueError, match='generation control'):
        material_workflow_intent(work)
    assert work == before


def test_portable_aliases_preserve_identical_values(native_catalog):
    portable = {'video_aspect_ratio': '9:16', 'video_variants_per_node': 1, 'video_duration_seconds': 5,
        'video_generation_mode': 'allReference', 'video_generate_audio': False,
        'video_model': 'seedance-2.0-mini', 'video_resolution': '480p'}
    intent = material_workflow_intent({'steps': [step(controls=portable)]})
    assert intent['inputs'] == portable
    assert intent['items'][0]['duration_seconds'] == 5


def test_existing_audio_generation_inputs_keep_original_per_item_shape():
    controls = {'model': 'voice-model', 'voice_id': 'real-voice', 'audio_kind': 'speech',
                'narration': 'Original dialogue', 'language': 'Chinese'}
    intent = material_workflow_intent({'steps': [step('voice', controls, 'audio')]})
    assert intent['inputs'] == {}
    assert {key: intent['items'][0][key] for key in controls} == controls


def test_native_per_item_fields_are_retained_with_saved_video_controls(native_catalog):
    controls = {**CONTROLS, 'narration': 'Original dialogue', 'stage': 'shot',
                'timeline_role': 'main', 'requires_generated_audio': False}
    intent = material_workflow_intent({'steps': [step(controls=controls)]})
    compiled = catalog.compile_workflow_intent(intent)
    assert compiled['ok'], compiled
    item = catalog._intent_items(intent)[0]
    assert {key: item[key] for key in ('narration', 'stage', 'timeline_role', 'requires_generated_audio')} == {
        'narration': 'Original dialogue', 'stage': 'shot', 'timeline_role': 'main', 'requires_generated_audio': False}


def test_legacy_duration_does_not_inherit_siblings_declared_duration(native_catalog):
    intent = material_workflow_intent({'steps': [step('legacy', {}), step('explicit', {'durationSec': 5})]})
    assert [item['id'] for item in intent['items']] == ['legacy', 'explicit']
    assert 'video_duration_seconds' not in intent['inputs']
    compiled = catalog.compile_workflow_intent(intent)
    assert compiled['ok'], compiled
    nodes = {node['id']: node['data'] for node in compiled['plan']['nodes'] if node['node_type'] == 'videoNode'}
    assert 'durationSec' not in nodes['legacy']
    assert nodes['explicit']['durationSec'] == 5


def saved_work(tmp_path, steps):
    store, plan_ref, prepared = setup_work(tmp_path)
    requirement = store.read_latest(prepared['work_ref']['artifact_id'])['payload']['requirements']
    prepared = prepare_material_work(store, plan_ref, requirement, steps, 'controlled-wave')
    return store, prepared['work_ref']


def saved_image_work(tmp_path, controls):
    store, plan_ref, prepared = setup_work(tmp_path)
    work = store.read_latest(prepared['work_ref']['artifact_id'])['payload']
    work['steps'][0]['generation_inputs'] = deepcopy(controls)
    prepared = prepare_material_work(store, plan_ref, work['requirements'], work['steps'], 'image-controls')
    return store, prepared['work_ref']


@pytest.mark.parametrize('per_item', [False, True])
def test_attachment_accepts_saved_controls_in_shared_or_item_fields(tmp_path, per_item):
    controls = {'image_model': 'image-model', 'image_aspect_ratio': '9:16',
                'image_resolution': '1k', 'image_quality': 'low',
                'image_variants_per_node': 1}
    store, ref = saved_image_work(tmp_path, controls)
    intent = material_workflow_intent(store.read_latest(ref['artifact_id'])['payload'])
    if not per_item:
        for key in controls:
            intent['items'][0].pop(key, None)
    draft = create_workflow_draft(project_dir=tmp_path, project_id='project', canvas_id='canvas',
                                  intent=intent, compiled={'ok': True, 'plan': {'nodes': []}})
    result = attach_material_execution_draft(store, ref, canvas_id='canvas',
        draft_id=draft['draft_id'], draft_revision=1)
    assert result['work']['steps'][0]['execution']['draft_revision'] == 1


def test_parameter_revision_rebinds_only_after_saved_user_choices_match(tmp_path):
    from novelvideo.freezone.workflow_drafts import patch_workflow_draft

    controls = {'image_model': 'image-model', 'image_aspect_ratio': '9:16', 'image_resolution': '1k'}
    store, ref = saved_image_work(tmp_path, controls)
    intent = material_workflow_intent(store.read_latest(ref['artifact_id'])['payload'])
    compiled = {'ok': True, 'skill_id': intent['skill_id'], 'plan': {'nodes': []}}
    draft = create_workflow_draft(project_dir=tmp_path, project_id='project', canvas_id='canvas',
                                  intent=intent, compiled=compiled)
    attached = attach_material_execution_draft(store, ref, canvas_id='canvas',
        draft_id=draft['draft_id'], draft_revision=1)
    choices = {**controls, 'image_quality': 'low', 'image_variants_per_node': 1}
    intent['inputs'].update(choices)
    revised, error = patch_workflow_draft(project_dir=tmp_path, canvas_id='canvas',
        draft_id=draft['draft_id'], expected_revision=1, intent=intent, compiled=compiled)
    assert error is None and revised['revision'] == 2
    result = attach_material_execution_draft(store, attached['work_ref'], canvas_id='canvas',
        draft_id=draft['draft_id'], draft_revision=2, confirmed_choices=choices)
    actual = result['work']['steps'][0]
    assert actual['execution']['draft_revision'] == 2
    assert actual['generation_inputs']['image_quality'] == 'low'
    assert actual['generation_inputs']['image_variants_per_node'] == 1


@pytest.mark.asyncio
async def test_claim_syncs_attached_revision_using_durable_choices(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from novelvideo.api.routes import freezone, reference_material
    from novelvideo.freezone.remix import reference_director
    from novelvideo.freezone.workflow_drafts import patch_workflow_draft

    controls = {'image_model': 'image-model', 'image_aspect_ratio': '9:16', 'image_resolution': '1k'}
    store, ref = saved_image_work(tmp_path, controls)
    intent = material_workflow_intent(store.read_latest(ref['artifact_id'])['payload'])
    compiled = {'ok': True, 'skill_id': intent['skill_id'], 'plan': {'nodes': []}}
    draft = create_workflow_draft(project_dir=tmp_path, project_id='project', canvas_id='canvas',
                                  intent=intent, compiled=compiled)
    attached = attach_material_execution_draft(store, ref, canvas_id='canvas',
        draft_id=draft['draft_id'], draft_revision=1)
    choices = {**controls, 'image_quality': 'low', 'image_variants_per_node': 1}
    intent['inputs'].update(choices)
    revised, error = patch_workflow_draft(project_dir=tmp_path, canvas_id='canvas',
        draft_id=draft['draft_id'], expected_revision=1, intent=intent, compiled=compiled)
    assert error is None
    monkeypatch.setattr(reference_director, 'list_director_artifacts', lambda *_a, **_k: {
        'material_work': [{**attached['work'], 'work_ref': attached['work_ref']}]})
    async def saved_choices(*_a):
        return choices
    monkeypatch.setattr(reference_material, '_current_material_generation_choices', saved_choices)
    monkeypatch.setattr(freezone.canvas_store, 'read_canvas', lambda *_a: {'nodes': []})
    ctx = SimpleNamespace(output_dir=tmp_path, state_dir=tmp_path,
                          project_id='project', requester_user_id=store.created_by)
    await freezone._sync_material_draft_before_claim(ctx, 'canvas', revised)
    current = store.read_latest(ref['artifact_id'])
    assert current['payload']['steps'][0]['execution']['draft_revision'] == 2
    assert current['payload']['steps'][0]['generation_inputs']['image_quality'] == 'low'


def test_attachment_rejects_item_override_of_saved_shared_controls(tmp_path):
    store, ref = saved_work(tmp_path, [step()])
    intent = material_workflow_intent(store.read_latest(ref['artifact_id'])['payload'])
    intent['items'][0]['quality'] = '720p'
    draft = create_workflow_draft(project_dir=tmp_path, project_id='project', canvas_id='canvas',
                                  intent=intent, compiled={'ok': True, 'plan': {'nodes': []}})
    with pytest.raises(ValueError, match='differs from ready material wave'):
        attach_material_execution_draft(store, ref, canvas_id='canvas',
            draft_id=draft['draft_id'], draft_revision=1)
    assert store.read_latest(ref['artifact_id'])['revision'] == ref['revision']


@pytest.mark.parametrize('drift', ['audio-type', 'mode', 'quality', 'missing'])
def test_attachment_rejects_explicit_input_drift_before_publication(tmp_path, drift):
    store, ref = saved_work(tmp_path, [step()])
    intent = material_workflow_intent(store.read_latest(ref['artifact_id'])['payload'])
    # Test the portable draft boundary independently of whether the initial
    # compiler has yet acquired the control-mapping repair.
    intent['inputs'] = {'video_aspect_ratio': '9:16', 'video_variants_per_node': 1, 'video_duration_seconds': 5,
        'video_generation_mode': 'allReference', 'video_generate_audio': True,
        'video_model': 'seedance-2.0-mini', 'video_resolution': '480p'}
    if drift == 'audio-type':
        intent['inputs']['video_generate_audio'] = 1
    elif drift == 'mode':
        intent['inputs']['video_generation_mode'] = 'textToVideo'
    elif drift == 'quality':
        intent['inputs']['video_resolution'] = '720p'
    else:
        intent['inputs'].pop('video_generate_audio')
    draft = create_workflow_draft(project_dir=tmp_path, project_id='project', canvas_id='canvas', intent=intent, compiled={'ok': True, 'plan': {'nodes': []}})
    with pytest.raises(ValueError, match='generation inputs'):
        attach_material_execution_draft(store, ref, canvas_id='canvas', draft_id=draft['draft_id'], draft_revision=1)
    assert store.read_latest(ref['artifact_id'])['revision'] == 1


def test_attachment_submits_only_selected_wave_and_reports_remaining_steps(tmp_path):
    store, ref = saved_work(tmp_path, [step(), step('later', {**CONTROLS, 'quality': '720p'})])
    intent = material_workflow_intent(store.read_latest(ref['artifact_id'])['payload'])
    intent['inputs']['execution_mode'] = 'manual'
    draft = create_workflow_draft(project_dir=tmp_path, project_id='project', canvas_id='canvas', intent=intent, compiled={'ok': True, 'plan': {'nodes': []}})
    result = attach_material_execution_draft(store, ref, canvas_id='canvas', draft_id=draft['draft_id'], draft_revision=1)
    assert [s['status'] for s in result['work']['steps']] == ['submitted', 'planned']
    assert result['ready_step_ids'] == ['later']
    assert [i['id'] for i in material_workflow_intent(result['work'])['items']] == ['later']


def test_legacy_no_controls_keeps_original_intent_shape_and_defaults(tmp_path):
    store, ref = saved_work(tmp_path, [step(controls={})])
    intent = material_workflow_intent(store.read_latest(ref['artifact_id'])['payload'])
    assert intent['inputs'] == {} and 'duration_seconds' not in intent['items'][0]
    intent['inputs']['execution_mode'] = 'manual'
    draft = create_workflow_draft(project_dir=tmp_path, project_id='project', canvas_id='canvas', intent=intent, compiled={'ok': True, 'plan': {'nodes': []}})
    assert attach_material_execution_draft(store, ref, canvas_id='canvas', draft_id=draft['draft_id'], draft_revision=1)['work']['steps'][0]['status'] == 'submitted'
