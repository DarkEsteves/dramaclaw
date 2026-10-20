"""Host reference typing must preserve native image-only inputs and scope checks."""
from copy import deepcopy

import pytest
from PIL import Image

from test_material_work import setup_work
from novelvideo.freezone.remix.material_work import material_workflow_intent, prepare_material_work
from novelvideo.freezone.remix.material_execution import read_material_state, attach_material_execution_draft


def work_with_reference(node_id='reference'):
    return {'steps': [{'id': 'clip', 'label': 'Guest answer', 'media_kind': 'video',
        'source': 'generate', 'recipe_id': 'general-video', 'prompt': 'Same guest and scene',
        'status': 'planned', 'references': [{'node_id': node_id, 'role': 'identity'}]}]}


def image_canvas():
    return {'nodes': [{'id': 'reference', 'type': 'imageGenNode',
                      'data': {'imageUrl': '/static/projects/project/picture.png'}}]}


def test_direct_image_reference_obeys_exact_native_external_contract(monkeypatch):
    from novelvideo.freezone.agent_workflows import catalog
    from test_html_workflow_intent import private_recipe
    recipes = [private_recipe('general-video', 'video')]
    skill = {'id': 'reference-video-remix', 'version': '1', 'allowed_recipe_ids': ['general-video']}
    monkeypatch.setattr(catalog, '_load_skill', lambda _: skill)
    monkeypatch.setattr(catalog, '_load_agent_config_items', lambda kind, *roots: recipes if kind == 'recipes' else [skill])
    intent = material_workflow_intent(work_with_reference(), canvas=image_canvas())
    external, = intent['external_inputs']
    assert external == {'id': intent['items'][0]['reference_inputs'][0],
                        'node_id': 'reference', 'media_kind': 'image'}
    compiled = catalog.compile_workflow_intent(intent)
    assert compiled['ok'], compiled
    # This host repair must not extend the compiler's existing input capability.
    for kind in ('video', 'audio'):
        unsupported = deepcopy(intent)
        unsupported['external_inputs'][0]['media_kind'] = kind
        result = catalog.compile_workflow_intent(unsupported)
        assert result['ok'] is False
        assert 'invalid external image input' in str(result)


@pytest.mark.parametrize('kind,node_type,field', [
    ('video', 'videoNode', 'videoUrl'), ('audio', 'audioNode', 'audioUrl'),
    ('video', 'uploadNode', 'videoUrl'),
])
def test_nonimage_reference_is_not_disguised_by_role_preview_or_metadata(kind, node_type, field):
    canvas = {'nodes': [{'id': 'reference', 'type': node_type,
        'data': {field: '/static/projects/project/source', 'previewImageUrl': '/static/projects/project/preview.png',
                 'media_kind': 'image'}}]}
    with pytest.raises(ValueError, match='only image references') as rejected:
        material_workflow_intent(work_with_reference(), canvas=canvas)
    assert kind in str(rejected.value)
    assert 'select or prepare an image' in str(rejected.value)


@pytest.mark.parametrize('change,reason', [
    ('missing', 'current canvas'), ('duplicate', 'ambiguous'), ('stale', 'stale'),
    ('not-current', 'stale'), ('no-output', 'current output'), ('multiple', 'ambiguous'),
    ('contradictory', 'contradictory'), ('unknown-type', 'media node'),
])
def test_direct_reference_requires_unambiguous_current_output(change, reason):
    canvas = image_canvas()
    node = canvas['nodes'][0]
    if change == 'missing': canvas['nodes'] = []
    elif change == 'duplicate': canvas['nodes'].append(deepcopy(node))
    elif change == 'stale': node['data']['workflowResultStale'] = True
    elif change == 'not-current': node['data']['remixVisualOutputCurrent'] = False
    elif change == 'no-output': node['data'] = {'media_kind': 'image'}
    elif change == 'multiple': node['data']['videoUrl'] = '/static/projects/project/movie.mp4'
    elif change == 'contradictory': node['type'] = 'videoNode'
    elif change == 'unknown-type': node['type'] = 'textAnnotationNode'
    with pytest.raises(ValueError, match=reason):
        material_workflow_intent(work_with_reference(), canvas=canvas)


def test_direct_reference_cannot_compile_without_authorized_canvas():
    with pytest.raises(ValueError, match='authorized current canvas'):
        material_workflow_intent(work_with_reference())


def test_accepted_dependency_uses_real_resource_kind_and_checks_consistency():
    work = work_with_reference()
    work['steps'][0]['references'] = [{'step_id': 'person', 'role': 'identity'}]
    source = {'id': 'person', 'media_kind': 'image', 'source': 'existing', 'status': 'accepted',
              'node_id': 'reference', 'accepted_resource': {'node_id': 'reference', 'media_kind': 'image'}}
    work['steps'].insert(0, source)
    intent = material_workflow_intent(work)
    assert intent['external_inputs'][0]['media_kind'] == 'image'
    source['accepted_resource']['media_kind'] = 'video'
    with pytest.raises(ValueError, match='accepted resource'):
        material_workflow_intent(work)
    source['accepted_resource']['media_kind'] = 'image'
    source['accepted_resource']['node_id'] = 'other'
    with pytest.raises(ValueError, match='accepted resource'):
        material_workflow_intent(work)


def test_reference_free_and_not_ready_steps_need_no_canvas():
    work = work_with_reference()
    work['steps'][0]['references'] = []
    assert material_workflow_intent(work)['external_inputs'] == []
    work['steps'][0].update(references=[{'node_id': 'missing', 'role': 'identity'}], depends_on=['waiting'])
    work['steps'].append({'id': 'waiting', 'status': 'planned', 'source': 'upload'})
    assert material_workflow_intent(work)['items'] == []


def saved_direct_work(tmp_path):
    store, plan_ref, prepared = setup_work(tmp_path)
    original = store.read(prepared['work_ref']['artifact_id'], 1)['payload']
    steps = work_with_reference()['steps']
    steps[0]['id'] = 'video'
    prepared = prepare_material_work(store, plan_ref, original['requirements'], steps, 'direct-reference')
    return store, prepared['work_ref']


def test_state_only_read_does_not_synthesize_invalid_intent_or_mutate_work(tmp_path):
    store, ref = saved_direct_work(tmp_path)
    before = store.read_latest(ref['artifact_id'])
    state = read_material_state(store, ref, include_workflow_intent=False)
    assert 'workflow_intent' not in state
    assert state['work'] == before['payload']
    assert store.read_latest(ref['artifact_id']) == before
    with pytest.raises(ValueError, match='authorized current canvas'):
        read_material_state(store, ref)


def test_read_and_attach_use_same_current_canvas_intent_and_reject_drift(tmp_path):
    from novelvideo.freezone.workflow_drafts import create_workflow_draft
    store, ref = saved_direct_work(tmp_path)
    canvas = image_canvas()
    state = read_material_state(store, ref, canvas=canvas)
    draft = create_workflow_draft(project_dir=tmp_path, project_id='project', canvas_id='canvas',
        intent=state['workflow_intent'], compiled={'ok': True, 'plan': {'nodes': []}})
    changed = deepcopy(canvas)
    changed['nodes'][0]['data']['workflowResultStale'] = True
    with pytest.raises(ValueError, match='stale'):
        attach_material_execution_draft(store, ref, canvas=changed, canvas_id='canvas',
                                       draft_id=draft['draft_id'], draft_revision=1)
    assert store.read_latest(ref['artifact_id'])['revision'] == 1
    result = attach_material_execution_draft(store, ref, canvas=canvas, canvas_id='canvas',
                                           draft_id=draft['draft_id'], draft_revision=1)
    assert result['work']['steps'][0]['status'] == 'submitted'
    state = read_material_state(store, result['work_ref'], canvas=canvas, canvas_id='canvas',
                                include_workflow_intent=False)
    assert state['material_lifecycle']['workflow_draft_status'] == 'ready'
    assert state['material_lifecycle']['canvas_delivery_status'] == 'awaiting_confirmation'
    assert state['material_lifecycle']['generation_status'] == 'submitted'


@pytest.mark.asyncio
async def test_material_routes_forward_authorized_canvas_after_plan_scope(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from novelvideo.api.routes import reference_material as routes, reference_director as director
    store, ref = saved_direct_work(tmp_path)
    canvas = image_canvas()
    checked = []
    async def context(*args):
        return SimpleNamespace(state_dir=tmp_path), canvas, store
    async def plan_scope(*args):
        checked.append('plan')
    monkeypatch.setattr(director, '_context', context)
    monkeypatch.setattr(director, '_plan_in_canvas', plan_scope)
    stage = await routes.material_stage('project', 'canvas', routes.WorkBody(work_ref=ref), {})
    read = await routes.material_state('project', 'canvas', routes.WorkBody(work_ref=ref), {})
    assert checked == ['plan', 'plan']
    assert stage['data']['workflow_intent'] == read['data']['workflow_intent']
    canvas['nodes'] = []
    with pytest.raises(Exception) as failed:
        await routes.material_stage('project', 'canvas', routes.WorkBody(work_ref=ref), {})
    assert getattr(failed.value, 'status_code', None) == 422


def test_native_local_media_probe_still_rejects_foreign_or_wrong_kind(tmp_path):
    from novelvideo.freezone.remix.material_execution import canvas_material_resource
    store, _ = saved_direct_work(tmp_path)
    Image.new('RGB', (16, 12), 'red').save(tmp_path / 'picture.png')
    canvas = image_canvas()
    assert canvas_material_resource(store, canvas, 'reference', 'image')['media_kind'] == 'image'
    canvas['nodes'][0]['data']['imageUrl'] = '/static/projects/foreign/picture.png'
    with pytest.raises(ValueError, match='project'):
        canvas_material_resource(store, canvas, 'reference', 'image')
    canvas['nodes'][0]['data'] = {'videoUrl': '/static/projects/project/picture.png'}
    with pytest.raises(ValueError, match='media kind'):
        canvas_material_resource(store, canvas, 'reference', 'video')


@pytest.mark.parametrize('drift', ['missing-kind', 'different-node'])
def test_attach_cannot_adopt_invalid_or_different_external_identity(tmp_path, drift):
    from novelvideo.freezone.workflow_drafts import create_workflow_draft
    store, ref = saved_direct_work(tmp_path)
    canvas = image_canvas()
    intent = material_workflow_intent(store.read_latest(ref['artifact_id'])['payload'], canvas=canvas)
    if drift == 'missing-kind': intent['external_inputs'][0].pop('media_kind')
    else: intent['external_inputs'][0]['node_id'] = 'other'
    draft = create_workflow_draft(project_dir=tmp_path, project_id='project', canvas_id='canvas',
        intent=intent, compiled={'ok': True, 'plan': {'nodes': []}})
    with pytest.raises(ValueError, match='differs from ready material wave'):
        attach_material_execution_draft(store, ref, canvas=canvas, canvas_id='canvas',
                                       draft_id=draft['draft_id'], draft_revision=1)
    assert store.read_latest(ref['artifact_id'])['revision'] == 1


@pytest.mark.parametrize('kind,node_type,field', [('video', 'videoNode', 'videoUrl'),
                                              ('audio', 'audioNode', 'audioUrl')])
def test_accepted_nonimage_dependency_reports_actual_unsupported_kind(kind, node_type, field):
    work = work_with_reference()
    work['steps'][0]['references'] = [{'step_id': 'source', 'role': 'setting'}]
    work['steps'].insert(0, {'id': 'source', 'media_kind': kind, 'source': 'existing', 'status': 'accepted',
        'accepted_resource': {'node_id': 'reference', 'media_kind': kind}})
    canvas = {'nodes': [{'id': 'reference', 'type': node_type, 'data': {field: '/static/projects/project/source'}}]}
    with pytest.raises(ValueError, match=f'actual reference is {kind}'):
        material_workflow_intent(work, canvas=canvas)


def test_preparation_validates_current_canvas_before_publishing_work(tmp_path):
    store, plan_ref, original = setup_work(tmp_path)
    Image.new('RGB', (16, 12), 'red').save(tmp_path / 'picture.png')
    requirements = store.read(original['work_ref']['artifact_id'], 1)['payload']['requirements']
    steps = work_with_reference()['steps']
    steps[0]['id'] = 'video'
    before = set(store.root.iterdir())
    with pytest.raises(ValueError, match='current canvas'):
        prepare_material_work(store, plan_ref, requirements, steps, 'invalid-direct', canvas={'nodes': []})
    assert set(store.root.iterdir()) == before
    prepared = prepare_material_work(store, plan_ref, requirements, steps, 'valid-direct', canvas=image_canvas())
    assert read_material_state(store, prepared['work_ref'], canvas=image_canvas())['workflow_intent']['external_inputs'][0]['media_kind'] == 'image'


def test_preparation_rejects_missing_image_bytes_even_with_image_node(tmp_path):
    store, plan_ref, original = setup_work(tmp_path)
    requirements = store.read(original['work_ref']['artifact_id'], 1)['payload']['requirements']
    steps = work_with_reference()['steps']
    steps[0]['id'] = 'video'
    with pytest.raises(ValueError, match='existing project file'):
        prepare_material_work(store, plan_ref, requirements, steps, 'missing-image', canvas=image_canvas())


def test_preparation_rejects_existing_video_strategy_bound_to_image(tmp_path):
    store, plan_ref, original = setup_work(tmp_path)
    Image.new('RGB', (16, 12), 'red').save(tmp_path / 'picture.png')
    requirements = store.read(original['work_ref']['artifact_id'], 1)['payload']['requirements']
    step = {'id': 'video', 'label': 'Use original clip', 'media_kind': 'video',
            'source': 'existing', 'node_id': 'reference'}
    with pytest.raises(ValueError, match='media kind'):
        prepare_material_work(store, plan_ref, requirements, [step],
                              'wrong-existing-kind', canvas=image_canvas())


@pytest.mark.asyncio
async def test_native_execution_route_preserves_canvas_scope_and_typed_identity(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fastapi import HTTPException
    from novelvideo.api.routes import reference_material as routes, reference_director as director
    from novelvideo.freezone.workflow_drafts import create_workflow_draft
    store, ref = saved_direct_work(tmp_path)
    canvas = image_canvas()
    draft = create_workflow_draft(project_dir=tmp_path, project_id='project', canvas_id='canvas',
        intent=material_workflow_intent(store.read_latest(ref['artifact_id'])['payload'], canvas=canvas),
        compiled={'ok': True, 'plan': {'nodes': []}})
    async def context(*args):
        return SimpleNamespace(state_dir=tmp_path), canvas, store
    async def denied(*args):
        raise HTTPException(403, 'private canvas access denied')
    monkeypatch.setattr(director, '_context', context)
    monkeypatch.setattr(director, '_plan_in_canvas', denied)
    body = routes.DraftBody(revision=1, draft_id=draft['draft_id'], draft_revision=1)
    with pytest.raises(HTTPException, match='private canvas'):
        await routes.execution_draft('project', 'canvas', ref['artifact_id'], body, {})
    assert store.read_latest(ref['artifact_id'])['revision'] == 1
    async def scoped(*args): pass
    monkeypatch.setattr(director, '_plan_in_canvas', scoped)
    result = await routes.execution_draft('project', 'canvas', ref['artifact_id'], body, {})
    assert result['data']['work']['steps'][0]['execution']['canvas_id'] == 'canvas'


@pytest.mark.asyncio
async def test_notification_can_read_unresolved_work_without_invalid_intent(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from novelvideo.api.routes import reference_material as routes, reference_director as director
    from novelvideo.freezone.remix import reference_director as inventory
    store, ref = saved_direct_work(tmp_path)
    before = store.read_latest(ref['artifact_id'])
    emitted = []
    monkeypatch.setattr(inventory, 'list_director_artifacts', lambda *args: {'commissions': []})
    async def emit(*args): emitted.append(True)
    monkeypatch.setattr(director, '_emit_director_update', emit)
    await routes._notify_material(SimpleNamespace(requester_user_id='alice'), 'canvas', store, ref, 'test-event')
    assert emitted == [True]
    assert store.read_latest(ref['artifact_id']) == before
