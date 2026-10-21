import pytest
from PIL import Image
from test_material_work import setup_work


def test_current_canvas_resource_is_probed_and_foreign_node_rejected(tmp_path):
    from novelvideo.freezone.remix.material_execution import canvas_material_resource
    store, _, _ = setup_work(tmp_path)
    Image.new('RGB', (16, 12), 'red').save(tmp_path / 'picture.png')
    canvas = {'nodes': [{'id': 'n', 'data': {'imageUrl': '/static/projects/project/picture.png'}}]}
    resource = canvas_material_resource(store, canvas, 'n', 'image')
    assert resource['duration_us'] == 0 and len(resource['media_hash']) == 64
    canvas['nodes'][0]['data']['imageUrl'] = '/static/projects/foreign/picture.png'
    with pytest.raises(ValueError, match='project'):
        canvas_material_resource(store, canvas, 'n', 'image')


def test_material_state_exposes_saved_lifecycle_without_claiming_generation(tmp_path):
    from novelvideo.freezone.remix.material_execution import read_material_state

    store, _, prepared = setup_work(tmp_path)
    state = read_material_state(store, prepared['work_ref'], include_workflow_intent=False)

    assert state['material_lifecycle'] == {
        'material_work_status': 'saved',
        'workflow_draft_status': 'not_created',
        'canvas_delivery_status': 'not_requested',
        'generation_status': 'not_started',
    }


def test_generation_inputs_cannot_override_execution_identity(tmp_path):
    from novelvideo.freezone.remix.material_work import MaterialStep
    with pytest.raises(ValueError, match='generation_inputs'):
        MaterialStep.model_validate({'id':'one','label':'one','media_kind':'image','source':'generate',
        'recipe_id':'general-image','prompt':'one','generation_inputs':{'id':'foreign'}})


def test_user_review_acceptance_replays_durable_command(tmp_path):
    from novelvideo.freezone.remix.material_execution import review_material_candidate, confirm_material_review
    from novelvideo.freezone.remix.material_work import accept_material_candidate
    store, _, prepared = setup_work(tmp_path)
    saved = store.read(prepared['work_ref']['artifact_id'], 1)
    work = saved['payload']
    work['steps'][0].update(source='upload')
    work['steps'][0].pop('recipe_id')
    saved = store.revise(saved['id'], 1, work, saved['upstream_refs'])
    ref = {'artifact_id':saved['id'],'revision':saved['revision']}
    Image.new('RGB',(16,12),'red').save(tmp_path/'picture.png')
    canvas = {'nodes':[{'id':'n','data':{'imageUrl':'/static/projects/project/picture.png'}}]}
    review = review_material_candidate(store,ref,'image',canvas,'n')
    assert review['review']['source_evidence'][0]['node_id'] == 'n'
    assert review['review']['source_evidence'][0]['media_hash'] == review['review']['resource']['media_hash']
    confirmed = confirm_material_review(store,review['review_ref'],canvas,canvas_id='canvas',requester_user_id='alice')
    args = (store,ref,'image',confirmed['review']['resource'],confirmed['review_ref'],'accept-one')
    first = accept_material_candidate(*args)
    assert first['ready_step_ids'] == ['video']
    assert accept_material_candidate(*args) == first


def test_content_pass_cannot_use_fabricated_user_receipt(tmp_path):
    store, _, prepared = setup_work(tmp_path)
    resource = {'id':'a','node_id':'n','media_kind':'image','media_path':'a.png','media_hash':'a'*64,'duration_us':0}
    with pytest.raises(ValueError, match='user decision'):
        store.create('material_review.v1', {'work_ref':prepared['work_ref'],'step_id':'image','resource':resource,
            'technical':{'status':'passed'},'content':{'status':'passed'},'actor_kind':'agent','decision_receipt_id':'fake'},[prepared['work_ref']])


@pytest.mark.asyncio
async def test_content_inspection_supplies_actual_image_and_requires_user_confirmation(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.material_execution import inspect_material_content
    import novelvideo.freezone.vision_gateway as gateway
    Image.new('RGB',(16,12),'red').save(tmp_path/'picture.png')
    async def observe(**kwargs):
        assert len(kwargs['images']) == 1
        assert kwargs['images'][0].data
        assert 'red product' in kwargs['prompt']
        return 'vision-test', 'The product is red.'
    monkeypatch.setattr(gateway, 'call_freezone_vision_model',observe)
    result = await inspect_material_content(tmp_path, {'media_path':'picture.png','media_kind':'image','media_hash':'a'*64}, {'prompt':'red product'}, [])
    assert result['status'] == 'unavailable'
    assert 'reason' in result
    assert result['requires_user_review'] is True


def test_initial_work_rejects_claimed_accepted_material(tmp_path):
    from novelvideo.freezone.remix.material_work import prepare_material_work
    store, plan_ref, prepared = setup_work(tmp_path)
    work = store.read(prepared['work_ref']['artifact_id'],1)['payload']
    work['steps'][0]['execution'] = {'draft_id':'fake'}
    with pytest.raises(ValueError, match='server'):
        prepare_material_work(store,plan_ref,work['requirements'],work['steps'],'injected')


def test_planned_generation_review_reports_missing_native_execution_receipt(tmp_path):
    from novelvideo.freezone.remix.material_execution import review_material_candidate

    store, _, prepared = setup_work(tmp_path)
    Image.new('RGB', (16, 12), 'red').save(tmp_path / 'picture.png')
    canvas = {'nodes': [{'id': 'n', 'data': {'imageUrl': '/static/projects/project/picture.png'}}]}

    with pytest.raises(ValueError, match='native execution receipt') as rejected:
        review_material_candidate(store, prepared['work_ref'], 'image', canvas, 'n')

    assert 'freezone_prepare_material_work' in str(rejected.value)
    assert 'freezone_prepare_material_stage' in str(rejected.value)
    assert 'source=existing' in str(rejected.value)
    assert store.read_latest(prepared['work_ref']['artifact_id'])['revision'] == 1


_NATIVE_EXECUTION_IDENTITY = {
    'canvas_id': 'canvas',
    'draft_id': 'draft',
    'draft_revision': 1,
    'workflow_instance_id': 'draft',
    'plan_node_id': 'image',
}


@pytest.mark.parametrize('execution', [
    None, {}, [], 'not-a-receipt',
    *[{key: value for key, value in _NATIVE_EXECUTION_IDENTITY.items() if key != field}
      for field in _NATIVE_EXECUTION_IDENTITY],
    *[{**_NATIVE_EXECUTION_IDENTITY, field: value}
      for field in ('canvas_id', 'draft_id', 'workflow_instance_id', 'plan_node_id')
      for value in (' ', 123)],
    *[{**_NATIVE_EXECUTION_IDENTITY, 'draft_revision': value} for value in (True, 0, 1.0)],
])
def test_incomplete_execution_identity_is_rejected_before_native_draft_lookup(tmp_path, monkeypatch, execution):
    from novelvideo.freezone.remix.material_execution import validate_material_native_execution
    import novelvideo.freezone.workflow_drafts as drafts

    store, _, _ = setup_work(tmp_path)

    def unexpected_lookup(**kwargs):
        pytest.fail('Incomplete execution must not access a native workflow draft')

    monkeypatch.setattr(drafts, 'read_workflow_draft', unexpected_lookup)
    step = {'id': 'image', 'source': 'generate', 'execution': execution}
    with pytest.raises(ValueError, match='native execution receipt') as rejected:
        validate_material_native_execution(store, {'nodes': []}, step, 'n', require_complete=True)

    assert 'not-a-receipt' not in str(rejected.value)


def test_separately_generated_existing_source_still_requires_current_media_review(tmp_path):
    from novelvideo.freezone.remix.material_execution import review_material_candidate, confirm_material_review
    from novelvideo.freezone.remix.material_work import prepare_material_work, accept_material_candidate

    store, plan_ref, prepared = setup_work(tmp_path)
    work = store.read(prepared['work_ref']['artifact_id'], 1)['payload']
    work['steps'][0].update(source='existing', node_id='n')
    work['steps'][0].pop('recipe_id')
    reused = prepare_material_work(store, plan_ref, work['requirements'], work['steps'], 'reuse-existing')
    Image.new('RGB', (16, 12), 'red').save(tmp_path / 'picture.png')
    canvas = {'nodes': [{'id': 'n', 'data': {'imageUrl': '/static/projects/project/picture.png'}}]}

    with pytest.raises(ValueError, match='planned resource'):
        review_material_candidate(store, reused['work_ref'], 'image', canvas, 'other-node')
    review = review_material_candidate(store, reused['work_ref'], 'image', canvas, 'n')
    assert review['review']['technical']['status'] == 'passed'
    assert review['review']['content']['status'] == 'unavailable'
    assert review['requires_user_review'] is True
    # Unobserved content stays advisory: the technical pass is enough to accept
    # and bind the candidate before a human confirms the look.
    accepted = accept_material_candidate(store, reused['work_ref'], 'image', review['review']['resource'],
                                         review['review_ref'], 'accept-advisory-image')
    assert accepted['work_ref']['revision'] > reused['work_ref']['revision']

    Image.new('RGB', (16, 12), 'blue').save(tmp_path / 'picture.png')
    with pytest.raises(ValueError, match='changed after review'):
        confirm_material_review(store, review['review_ref'], canvas, canvas_id='canvas', requester_user_id='alice')


def test_execution_draft_maps_ready_wave_and_blocks_unconfirmed_candidate(tmp_path):
    from novelvideo.freezone.remix.material_execution import attach_material_execution_draft, review_material_candidate
    from novelvideo.freezone.remix.material_work import material_workflow_intent
    from novelvideo.freezone.workflow_drafts import create_workflow_draft
    store, _, prepared = setup_work(tmp_path)
    work = store.read(prepared['work_ref']['artifact_id'],1)['payload']
    intent = material_workflow_intent(work)
    draft = create_workflow_draft(project_dir=tmp_path,project_id='project',canvas_id='canvas',intent=intent,
        compiled={'ok':True,'skill_id':intent['skill_id'],'plan':{'nodes':[]}})
    result = attach_material_execution_draft(store,prepared['work_ref'],canvas_id='canvas',draft_id=draft['draft_id'],draft_revision=1)
    step = result['work']['steps'][0]
    assert step['execution']['workflow_instance_id'] == draft['draft_id']
    assert step['status'] == 'submitted'
    with pytest.raises(ValueError,match='confirmed'):
        review_material_candidate(store,result['work_ref'],'image',{'nodes':[]},'n')


@pytest.mark.asyncio
async def test_structured_actual_content_can_pass_and_bad_json_stays_unavailable(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.material_execution import inspect_material_content
    import novelvideo.freezone.vision_gateway as gateway
    Image.new('RGB',(16,12),'red').save(tmp_path/'picture.png')
    async def passed(**kwargs):
        assert kwargs['images'][0].data
        return 'vision-test', '{"status":"passed","findings":[],"checked_items":["target"]}'
    monkeypatch.setattr(gateway,'call_freezone_vision_model',passed)
    result = await inspect_material_content(tmp_path,{'media_path':'picture.png','media_kind':'image','media_hash':'a'*64},{'id':'image','prompt':'red product'},[])
    assert result['status'] == 'passed' and result['requires_user_review'] is False
    async def malformed(**kwargs):
        return 'vision-test', 'Looks good'
    monkeypatch.setattr(gateway,'call_freezone_vision_model',malformed)
    result = await inspect_material_content(tmp_path,{'media_path':'picture.png','media_kind':'image','media_hash':'a'*64},{'id':'image','prompt':'red product'},[])
    assert result['status'] == 'unavailable' and result['requires_user_review'] is True


def test_native_execution_uses_distinct_state_root_not_media_output_root(tmp_path):
    from novelvideo.freezone.remix.material_execution import attach_material_execution_draft, validate_material_native_execution
    from novelvideo.freezone.remix.material_work import material_workflow_intent
    from novelvideo.freezone.workflow_drafts import (create_workflow_draft, claim_workflow_draft_confirmation,
        bind_workflow_draft_task, finish_workflow_draft_confirmation)
    from novelvideo.freezone.workflow_runs import create_workflow_run, _connect
    state = tmp_path / 'state'
    output = tmp_path / 'output'
    state.mkdir(); output.mkdir()
    store, _, prepared = setup_work(output)
    work = store.read(prepared['work_ref']['artifact_id'],1)['payload']
    intent = material_workflow_intent(work)
    draft = create_workflow_draft(project_dir=state,project_id='project',canvas_id='canvas',intent=intent,
        compiled={'ok':True,'skill_id':intent['skill_id'],'plan':{'nodes':[]}})
    attached = attach_material_execution_draft(store,prepared['work_ref'],canvas_id='canvas',draft_id=draft['draft_id'],draft_revision=1,workflow_state_dir=state)
    claim_workflow_draft_confirmation(project_dir=state,canvas_id='canvas',draft_id=draft['draft_id'],revision=1)
    bind_workflow_draft_task(project_dir=state,canvas_id='canvas',draft_id=draft['draft_id'],task_id='draft-task',root_task_id='root-task')
    finish_workflow_draft_confirmation(project_dir=state,canvas_id='canvas',draft_id=draft['draft_id'],outcome='confirmed',expected_task_id='draft-task')
    canvas = {'nodes':[{'id':'n','data':{'workflowInstanceId':draft['draft_id'],'workflowPlanNodeId':'image','imageUrl':'/static/projects/project/picture.png'}}]}
    run = create_workflow_run(project_dir=state,project_id='project',canvas_id='canvas',actions=[{'node_id':'n','action':'generate_image','recipe_id':'general-image'}])
    # Persist the same completion proof written by the native task reconciler.
    with _connect(state) as conn:
        conn.execute("UPDATE workflow_run_actions SET status='completed',artifact_status='valid',task_key='image-task',result_ref_json=? WHERE run_id=?",('{"value":"/static/projects/project/picture.png"}',run['run_id']))
    execution = validate_material_native_execution(store,canvas,attached['work']['steps'][0],'n',require_complete=True,workflow_state_dir=state)
    assert execution['task_id'] == 'image-task'
    assert not (output/'data.db').exists()


def test_retry_invalidates_only_dependent_branch_and_replays_command(tmp_path):
    from novelvideo.freezone.remix.material_work import retry_material_step
    store, _, prepared = setup_work(tmp_path)
    first = retry_material_step(store,prepared['work_ref'],'image','Candidate failed quality','retry-one')
    assert first['ready_step_ids'] == ['image']
    assert first['work']['steps'][0]['status'] == 'rejected'
    assert first['work']['steps'][1]['status'] == 'stale'
    assert retry_material_step(store,prepared['work_ref'],'image','Candidate failed quality','retry-one') == first
    with pytest.raises(ValueError,match='different'):
        retry_material_step(store,prepared['work_ref'],'image','other reason','retry-one')


def test_retry_keeps_producing_node_for_same_node_regeneration(tmp_path):
    from novelvideo.freezone.remix.material_work import retry_material_step

    store, _, prepared = setup_work(tmp_path)
    saved = store.read(prepared['work_ref']['artifact_id'], 1)
    work = saved['payload']
    work['steps'][0].update(node_id='image-node', task_id='image-task', execution={'run_id': 'run-1'})
    work['steps'][1].update(node_id='video-node')
    current = store.revise(saved['id'], saved['revision'], work, saved['upstream_refs'])

    result = retry_material_step(store, {'artifact_id': current['id'], 'revision': current['revision']},
                                 'image', 'Candidate rejected by review', 'retry-same-node')

    image, video = result['work']['steps']
    assert image['status'] == 'rejected' and image['node_id'] == 'image-node'
    assert 'task_id' not in image and 'execution' not in image
    assert video['status'] == 'stale' and video['node_id'] == 'video-node'
    assert result['ready_step_ids'] == ['image']


def test_explicit_content_rejection_still_blocks_acceptance(tmp_path):
    from copy import deepcopy
    from novelvideo.freezone.remix.material_execution import review_material_candidate
    from novelvideo.freezone.remix.material_work import accept_material_candidate, prepare_material_work

    store, plan_ref, prepared = setup_work(tmp_path)
    work = store.read(prepared['work_ref']['artifact_id'], 1)['payload']
    work['steps'][0].update(source='existing', node_id='n')
    work['steps'][0].pop('recipe_id')
    reused = prepare_material_work(store, plan_ref, work['requirements'], work['steps'], 'reject-existing')
    Image.new('RGB', (16, 12), 'red').save(tmp_path / 'picture.png')
    canvas = {'nodes': [{'id': 'n', 'data': {'imageUrl': '/static/projects/project/picture.png'}}]}
    review = review_material_candidate(store, reused['work_ref'], 'image', canvas, 'n')

    saved_review = store.read(review['review_ref']['artifact_id'], review['review_ref']['revision'])
    payload = deepcopy(saved_review['payload'])
    payload['content'] = {'status': 'rejected', 'checks': ['visible mismatch'], 'findings': ['wrong product']}
    rejected = store.revise(saved_review['id'], saved_review['revision'], payload, saved_review['upstream_refs'])
    with pytest.raises(ValueError, match='technical/content review'):
        accept_material_candidate(store, reused['work_ref'], 'image', review['review']['resource'],
                                  {'artifact_id': rejected['id'], 'revision': rejected['revision']},
                                  'accept-rejected-image')


@pytest.mark.asyncio
async def test_rejected_actual_review_is_recorded_without_auto_retry(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import novelvideo.api.routes.reference_material as route
    import novelvideo.freezone.remix.material_execution as execution
    store, _, prepared = setup_work(tmp_path)
    saved = store.read(prepared['work_ref']['artifact_id'],1)
    saved['payload']['steps'][0]['source'] = 'upload'
    saved['payload']['steps'][0].pop('recipe_id')
    changed = store.revise(saved['id'],1,saved['payload'],saved['upstream_refs'])
    ref = {'artifact_id':changed['id'],'revision':changed['revision']}
    Image.new('RGB',(16,12),'red').save(tmp_path/'picture.png')
    canvas = {'nodes':[{'id':'n','data':{'imageUrl':'/static/projects/project/picture.png'}}]}
    async def scope(*args,**kwargs):
        return SimpleNamespace(state_dir=tmp_path,output_dir=tmp_path,project_id='project',requester_user_id='alice',requester_username='alice'),canvas,store,{}
    async def rejected(*args,**kwargs):
        return {'status':'rejected','findings':['wrong product'],'checked_items':['target'],'requires_user_review':False}
    monkeypatch.setattr(route,'_scope',scope)
    from novelvideo.chat.reference_remix_continuation import coordinator
    monkeypatch.setattr(coordinator.store,'list_for_actor',lambda *args: [])
    monkeypatch.setattr(execution,'inspect_material_content',rejected)
    before = store.read_latest(ref['artifact_id'])
    result = await route.material_review('project','canvas',route.ReviewBody(work_ref=ref,step_id='image',node_id='n'),{})
    state = store.read_latest(ref['artifact_id'])
    # A rejection is recorded as review evidence only: regenerating stays an
    # explicit agent/user action, so the work package keeps its status.
    assert state['revision'] == before['revision']
    assert state['payload']['steps'][0]['status'] == before['payload']['steps'][0]['status']
    assert result['data']['review']['content']['status'] == 'rejected'
    assert result['data']['requires_user_review'] is False
