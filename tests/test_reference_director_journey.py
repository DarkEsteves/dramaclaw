"""Persisted production events require no browser connection or paid model."""
import pytest
from novelvideo.freezone.remix.store import RemixArtifactStore
from novelvideo.freezone.remix import reference_commission as commissions
from novelvideo.chat.reference_production_observer import production_event, ReferenceProductionObserver


def bind_native_plan(tmp_path, commission, node, *, state_dir=None):
    """Persist the approved workflow provenance required for a plan node."""
    from novelvideo.freezone.workflow_drafts import (
        bind_workflow_draft_task, claim_workflow_draft_confirmation,
        create_workflow_draft, finish_workflow_draft_confirmation,
    )
    state_dir = state_dir or tmp_path
    plan_node_id = 'native-' + node['id']
    draft = create_workflow_draft(
        project_dir=state_dir, project_id='p', canvas_id='c',
        intent={'skill_id':'reference-video-remix', 'user_goal':'g'},
        compiled={'ok':True, 'skill_id':'reference-video-remix',
            'reference_binding': {
                'project_id':'p', 'canvas_id':'c',
                'requester_user_id': commission['requester_user_id'],
                'commission_id': commission['commission_ref']['artifact_id'],
                'analysis_node_id': commission['analysis_node_id'],
                'direction_revision': commission['direction_revision'],
                'direction_fingerprint': commission['direction_fingerprint'],
                'stage':'production_plans'},
            'plan':{'nodes':[{'id':plan_node_id, 'node_type':'scriptNode',
                              'data':{'processingAction':'generate_production_plan_draft'}}],
                    'edges':[]}},
    )
    claimed, error = claim_workflow_draft_confirmation(
        project_dir=state_dir, canvas_id='c', draft_id=draft['draft_id'], revision=1)
    assert error is None
    bind_workflow_draft_task(
        project_dir=state_dir, canvas_id='c', draft_id=draft['draft_id'],
        task_id='task-' + node['id'], root_task_id='task-' + node['id'],
        expected_confirmation_started_at=claimed['confirmation_started_at'])
    finish_workflow_draft_confirmation(
        project_dir=state_dir, canvas_id='c', draft_id=draft['draft_id'],
        outcome='confirmed', expected_task_id='task-' + node['id'])
    node['data'].update(workflowInstanceId=draft['draft_id'],
                        workflowPlanNodeId=plan_node_id)


def setup_branch(tmp_path):
    store = RemixArtifactStore(tmp_path, 'p')
    c = commissions.create_reference_commission(store, canvas_id='c', requester_user_id='u', analysis_node_id='a', goal='g', analysis_evidence={'task_id':'t','result_fingerprint':'h'})
    c = commissions.advance_reference_commission(store,c['commission_ref'],{'type':'direction_confirmed','direction':{'target_goal':'g'}},1,host_evidence=commissions.AuthenticatedDecisionEvidence('p','c','u','m'))
    p = store.create('production_plan.v2', {'schema_version':2,'goal':'g','output':{'width':1920,'height':1080,'fps_num':24,'fps_den':1},'beats':[{'id':'b','purpose':'intro','duration_frames':48}]},[])
    canvas = {'nodes':[{'id':'a','data':{}},{'id':'plan','data':{'productionPlanV2Ref':{'artifactId':p['id'],'revision':1}}}], 'edges':[{'source':'a','target':'plan'}]}
    bind_native_plan(tmp_path, c, canvas['nodes'][1])
    return store,c,p,canvas


def test_saved_native_plan_observed_offline(tmp_path):
    store,c,p,canvas = setup_branch(tmp_path)
    observed = production_event(store,canvas,c)
    assert observed['event']['type'] == 'plan_ready'
    assert observed['event']['plan_ref']['artifact_id'] == p['id']
    assert observed['event_key'] == production_event(store,canvas,c)['event_key']
    c = commissions.advance_reference_commission(store,c['commission_ref'],observed['event'],2)
    assert c['phase'] == 'awaiting_plan_review'
    assert production_event(store,canvas,c) is None


def test_unlinked_or_duplicate_native_plans_are_not_selected(tmp_path):
    store,c,p,canvas = setup_branch(tmp_path)
    canvas['edges'] = []
    assert production_event(store,canvas,c) is None
    canvas['edges'] = [{'source':'a','target':'plan'}]
    other = store.create('production_plan.v2', p['payload'], [])
    canvas['nodes'].append({'id':'other','data':{'productionPlanV2Ref':{'artifactId':other['id'],'revision':1}}})
    # A second draft for the same confirmed direction cannot claim another
    # native workflow. The first accepted result remains the sole candidate.
    with pytest.raises(AssertionError, match='workflow_draft_already_confirmed'):
        bind_native_plan(tmp_path, c, canvas['nodes'][-1])
    canvas['edges'].append({'source':'a','target':'other'})
    assert production_event(store, canvas, c)['event']['plan_ref']['artifact_id'] == p['id']


def test_stale_saved_plan_reference_is_not_observed(tmp_path):
    store,c,p,canvas = setup_branch(tmp_path)
    changed = p['payload']
    changed['goal'] = 'changed'
    store.revise(p['id'],1,changed,[])
    assert production_event(store,canvas,c) is None


@pytest.mark.asyncio
async def test_observer_resolves_scope_and_wakes_from_persisted_state(tmp_path):
    from types import SimpleNamespace
    store,c,p,canvas = setup_branch(tmp_path)
    calls, continuations = [], []
    run = {'run_id':'r','commission_ref':c['commission_ref'],'project_id':'p','canvas_id':'c','requester_user_id':'u'}
    resolved = SimpleNamespace(output_dir=tmp_path,state_dir=tmp_path,project_id='p',requester_user_id='u')
    expected_event_key = production_event(store, canvas, c)['event_key']
    class Coordinator:
        async def _resolve_context(self, record):
            assert record is run
            calls.append('resolve')
            return resolved
        def _assert_current(self, ctx, record, **kwargs):
            assert ctx is resolved and record is run
            assert ctx.project_id == record['project_id'] and ctx.requester_user_id == record['requester_user_id']
            assert kwargs['completed'] is True
            calls.append('guard')
            return canvas
        async def continue_commission(self, **kwargs):
            # The real resolved scope must be guarded before any continuation.
            assert calls[0] == 'resolve' and 'guard' in calls[1:]
            calls.append('continue')
            continuations.append(kwargs)
    await ReferenceProductionObserver(Coordinator()).observe_run(run)
    assert len(continuations) == 1
    assert continuations[0] == {'ctx': resolved, 'run_id': 'r', 'event_key': expected_event_key}
    assert expected_event_key.startswith('production:')
    assert commissions.read_reference_commission(store,c['commission_ref'],latest=True)['phase'] == 'awaiting_plan_review'


def test_no_dialogue_skips_alignment_and_exact_manifest_advances(tmp_path):
    from test_reference_commission import confirmed_plan
    from novelvideo.freezone.remix.production_plan_v2 import compile_production_plan_v2
    store = RemixArtifactStore(tmp_path,'p')
    c,p = confirmed_plan(store)
    canvas = {'nodes':[{'id':'a','data':{}},{'id':'plan','data':{'productionPlanV2Ref':{'artifactId':p['id'],'revision':1}}}], 'edges':[{'source':'a','target':'plan'}]}
    bind_native_plan(tmp_path, c, canvas['nodes'][1])
    observed = production_event(store,canvas,c)
    assert observed['event']['type'] == 'speech_aligned'
    c = commissions.advance_reference_commission(store,c['commission_ref'],observed['event'],5)
    assert c['phase'] == 'composing'
    assert production_event(store,canvas,c) is None
    payload = compile_production_plan_v2(p['payload'])
    payload['source_versions'] = [{'artifact_id':p['id'],'revision':1}]
    manifest = store.create('composition_manifest.v2',payload,[c['plan_ref']])
    canvas['nodes'][1]['data']['remixManifestRef'] = {'artifactId':manifest['id'],'revision':1}
    observed = production_event(store,canvas,c)
    assert observed['event']['type'] == 'composition_ready'
    c = commissions.advance_reference_commission(store,c['commission_ref'],observed['event'],6)
    assert c['phase'] == 'reviewing_output'
    assert production_event(store,canvas,c) is None  # No invented passed review.


@pytest.mark.asyncio
async def test_saved_transition_recovers_wake_after_server_restart(tmp_path):
    from types import SimpleNamespace
    store,c,p,canvas = setup_branch(tmp_path)
    observed = production_event(store,canvas,c)
    commissions.advance_reference_commission(store,c['commission_ref'],observed['event'],2)
    run = {'run_id':'r','commission_ref':c['commission_ref'],'commission_phase':'drafting_plan','project_id':'p','canvas_id':'c','requester_user_id':'u'}
    wakes = []
    class Coordinator:
        async def _resolve_context(self, record):
            return SimpleNamespace(output_dir=tmp_path,state_dir=tmp_path,project_id='p',requester_user_id='u')
        def _assert_current(self, ctx, record, **kwargs):
            return canvas
        async def continue_commission(self, **kwargs):
            wakes.append(kwargs['event_key'])
    observer = ReferenceProductionObserver(Coordinator())
    await observer.observe_run(run)
    assert len(wakes) == 1
    run['commission_event_receipts'] = wakes[:]
    await observer.observe_run(run)
    assert len(wakes) == 1


@pytest.mark.asyncio
async def test_source_fence_prevents_any_saved_stage_transition(tmp_path):
    from types import SimpleNamespace
    store,c,p,canvas = setup_branch(tmp_path)
    continuations = []
    class Coordinator:
        async def _resolve_context(self, record):
            return SimpleNamespace(output_dir=tmp_path,state_dir=tmp_path,project_id='p',requester_user_id='u')
        def _assert_current(self, *args, **kwargs):
            raise ValueError('source replaced')
        async def continue_commission(self, **kwargs):
            continuations.append(kwargs)
    with pytest.raises(ValueError,match='source replaced'):
        await ReferenceProductionObserver(Coordinator()).observe_run({'run_id':'r','commission_ref':c['commission_ref']})
    assert continuations == []
    assert commissions.read_reference_commission(store,c['commission_ref'],latest=True)['phase'] == 'drafting_plan'


def uploaded_branch(tmp_path):
    from test_material_work import plan_payload
    from novelvideo.freezone.remix.material_work import prepare_material_work
    from PIL import Image
    store = RemixArtifactStore(tmp_path,'p',created_by='u')
    p = store.create('production_plan.v2',plan_payload(),[])
    ref = {'artifact_id':p['id'],'revision':1,'project_id':'p'}
    work = prepare_material_work(store,{'artifact_id':p['id'],'revision':1},[{'id':'final','placement_ids':['picture'],'description':'uploaded picture','media_kind':'image','final_step_id':'upload'}],[{'id':'upload','label':'upload','media_kind':'image','source':'upload','node_id':'n','status':'awaiting_upload'}],'upload-one')
    Image.new('RGB',(16,12),'red').save(tmp_path/'picture.png')
    c = {'analysis_node_id':'a','canvas_id':'c','phase':'preparing_materials','plan_ref':ref,'work_ref':None}
    canvas = {'nodes':[{'id':'a','data':{}},{'id':'plan','data':{'productionPlanV2Ref':{'artifactId':p['id'],'revision':1}}},{'id':'n','data':{'imageUrl':'/static/projects/p/picture.png'}}], 'edges':[{'source':'a','target':'plan'}]}
    return store,c,p,canvas,work


def test_uploaded_candidate_hash_observed_without_quality_acceptance(tmp_path):
    store,c,p,canvas,work = uploaded_branch(tmp_path)
    observation = production_event(store,canvas,c)
    assert observation['event'] is None
    assert store.read_latest(work['work_ref']['artifact_id'])['payload']['steps'][0]['status'] == 'awaiting_upload'
    assert production_event(store,canvas,c)['event_key'] == observation['event_key']
    canvas['nodes'][2]['data']['workflowResultStale'] = True
    assert production_event(store,canvas,c) is None
    canvas['nodes'][2]['data']['workflowResultStale'] = False
    from PIL import Image
    Image.new('RGB',(16,12),'blue').save(tmp_path/'picture.png')
    assert production_event(store,canvas,c)['event_key'] != observation['event_key']


def test_candidate_acceptance_and_binding_required_before_materials_ready(tmp_path):
    from novelvideo.freezone.remix.material_execution import review_material_candidate, confirm_material_review
    from novelvideo.freezone.remix.material_work import accept_material_candidate
    store,c,p,canvas,work = uploaded_branch(tmp_path)
    review = review_material_candidate(store,work['work_ref'],'upload',canvas,'n')
    confirmed = confirm_material_review(store,review['review_ref'],canvas,canvas_id='c',requester_user_id='u')
    accepted = accept_material_candidate(store,work['work_ref'],'upload',confirmed['review']['resource'],confirmed['review_ref'],'accept-one')
    current = store.read_latest(p['id'])
    canvas['nodes'][1]['data']['productionPlanV2Ref']['revision'] = current['revision']
    # First incorporate the saved binding revision, preserving confirmed content.
    c['plan_ref']['revision'] = current['revision']
    observed = production_event(store,canvas,c)
    assert observed['event']['type'] == 'materials_ready'
    assert observed['event']['work_ref']['revision'] == accepted['work_ref']['revision']
    from PIL import Image
    Image.new('RGB',(16,12),'blue').save(tmp_path/'picture.png')
    assert production_event(store,canvas,c) is None


def test_failed_native_material_task_is_observed_once_without_acceptance(tmp_path,monkeypatch):
    import novelvideo.freezone.remix.material_execution as execution
    from novelvideo.freezone.workflow_runs import create_workflow_run, update_workflow_run
    store,c,p,canvas,work = uploaded_branch(tmp_path)
    native = create_workflow_run(project_dir=tmp_path,project_id='p',canvas_id='c',actions=[{'node_id':'n','action':'generate_image','recipe_id':'general-image'}])
    update_workflow_run(project_dir=tmp_path,canvas_id='c',run_id=native['run_id'],status='failed',action_updates=[{'node_id':'n','action':'generate_image','status':'failed','task_key':'actual-task','error':'provider rejected request'}])
    saved = store.read_latest(work['work_ref']['artifact_id'])
    payload = saved['payload']
    proof = {'run_id':native['run_id'],'task_id':'actual-task','canvas_id':'c'}
    payload['steps'][0].update(source='generate',recipe_id='general-image',prompt='make image',status='submitted',execution=proof,task_id='actual-task')
    store.revise(saved['id'],saved['revision'],payload,saved['upstream_refs'])
    monkeypatch.setattr(execution,'validate_material_native_execution',lambda *a,**kw:proof)
    observed = production_event(store,canvas,c)
    assert observed['event'] is None
    assert '失败' in observed['notice']
    assert observed['event_key'] == production_event(store,canvas,c)['event_key']
    assert store.read_latest(saved['id'])['payload']['steps'][0]['status'] == 'submitted'


def test_dialogue_is_never_skipped_without_ready_saved_cues(tmp_path):
    store,c,p,canvas = setup_branch(tmp_path)
    payload = p['payload']
    payload['beats'][0]['turns'] = [{'id':'turn','speaker':'speaker','text':'say the complete line'}]
    p = store.revise(p['id'],1,payload,[])
    canvas['nodes'][1]['data']['productionPlanV2Ref']['revision'] = 2
    c.update(phase='aligning_speech',plan_ref={'artifact_id':p['id'],'revision':2,'project_id':'p'})
    assert production_event(store,canvas,c) is None


@pytest.mark.asyncio
async def test_server_observer_lifecycle_works_without_browser(tmp_path):
    import asyncio
    from types import SimpleNamespace
    visited = asyncio.Event()
    class Store:
        def waiting_for_production(self):
            return [{'run_id':'offline'}]
    class Observer(ReferenceProductionObserver):
        async def observe_run(self,run):
            assert run['run_id'] == 'offline'
            visited.set()
    async def recover_interactions():
        pass
    observer = Observer(SimpleNamespace(store=Store(), recover_interactions=recover_interactions))
    await observer.resume()
    task = observer._worker
    await asyncio.wait_for(visited.wait(),timeout=1)
    await observer.shutdown()
    assert task.done() and observer._worker is None


@pytest.mark.asyncio
async def test_observer_reads_native_proof_from_distinct_state_root(tmp_path,monkeypatch):
    from novelvideo.freezone.workflow_drafts import create_workflow_draft, claim_workflow_draft_confirmation, bind_workflow_draft_task, finish_workflow_draft_confirmation
    from novelvideo.freezone.workflow_runs import create_workflow_run, update_workflow_run
    output = tmp_path/'output'
    state = tmp_path/'state'
    store,c,p,canvas,work = uploaded_branch(output)
    from novelvideo.freezone.remix.material_work import material_workflow_intent
    from novelvideo.freezone.remix.material_execution import attach_material_execution_draft
    saved = store.read_latest(work['work_ref']['artifact_id'])
    payload = saved['payload']
    payload['steps'][0].update(source='generate',recipe_id='general-image',prompt='make image',status='planned',node_id=None)
    saved = store.revise(saved['id'],saved['revision'],payload,saved['upstream_refs'])
    intent = material_workflow_intent(saved['payload'])
    draft = create_workflow_draft(project_dir=state,project_id='p',canvas_id='c',intent=intent,compiled={'ok':True,'plan':{'nodes':[]}})
    attached = attach_material_execution_draft(store,{'artifact_id':saved['id'],'revision':saved['revision']},canvas_id='c',draft_id=draft['draft_id'],draft_revision=1,workflow_state_dir=state)
    claim_workflow_draft_confirmation(project_dir=state,canvas_id='c',draft_id=draft['draft_id'],revision=1)
    bind_workflow_draft_task(project_dir=state,canvas_id='c',draft_id=draft['draft_id'],task_id='draft-task',root_task_id='draft-task')
    finish_workflow_draft_confirmation(project_dir=state,canvas_id='c',draft_id=draft['draft_id'],outcome='confirmed',expected_task_id='draft-task')
    native = create_workflow_run(project_dir=state,project_id='p',canvas_id='c',actions=[{'node_id':'n','action':'generate_image','recipe_id':'general-image'}])
    update_workflow_run(project_dir=state,canvas_id='c',run_id=native['run_id'],action_updates=[{'node_id':'n','action':'generate_image','status':'running','task_key':'actual-task'}])
    canvas['nodes'][2]['data'].update(workflowInstanceId=draft['draft_id'],workflowPlanNodeId='upload')
    assert production_event(store,canvas,c) is None
    observed = production_event(store,canvas,c,workflow_state_dir=state)
    assert observed is None  # Registered running task is not a completed candidate.
    registered = store.read_latest(attached['work_ref']['artifact_id'])
    step = registered['payload']['steps'][0]
    assert step['node_id'] == 'n' and step['execution']['run_id'] == native['run_id']
    assert step['task_id'] == 'actual-task'
    assert production_event(store,canvas,c,workflow_state_dir=state) is None
    assert store.read_latest(registered['id'])['revision'] == registered['revision']

    # A real saved commission can wake from attached work without the execution API.
    from novelvideo.freezone.remix.production_review import review_production_plan, REVIEW_CATEGORIES
    from types import SimpleNamespace
    auth = commissions.AuthenticatedDecisionEvidence('p','c','u','host-confirmation')
    saved_commission = commissions.create_reference_commission(store,canvas_id='c',requester_user_id='u',analysis_node_id='a',goal=p['payload']['goal'],analysis_evidence={'task_id':'original-analysis','result_fingerprint':'original-hash'})
    saved_commission = commissions.advance_reference_commission(store,saved_commission['commission_ref'],{'type':'direction_confirmed','direction':{'target_goal':p['payload']['goal']}},1,host_evidence=auth)
    saved_commission = commissions.advance_reference_commission(store,saved_commission['commission_ref'],{'type':'plan_ready','plan_ref':c['plan_ref']},2)
    findings = [{'category':category,'entity_kind':'beat','entity_id':'opening','verdict':'pass','reason':'Confirmed opening meets direction','evidence':['opening:提问']} for category in REVIEW_CATEGORIES]
    reviewed = review_production_plan(store,c['plan_ref'],saved_commission['commission_ref'],findings)
    saved_commission = commissions.advance_reference_commission(store,saved_commission['commission_ref'],{'type':'plan_reviewed','review_ref':{'artifact_id':reviewed['id'],'revision':1,'project_id':'p'}},3)
    saved_commission = commissions.advance_reference_commission(store,saved_commission['commission_ref'],{'type':'plan_confirmed','plan_ref':c['plan_ref']},4,host_evidence=auth)
    bind_native_plan(output, saved_commission, canvas['nodes'][1], state_dir=state)
    calls = []
    class Coordinator:
        async def _resolve_context(self,record):
            return SimpleNamespace(output_dir=output,state_dir=state,project_id='p',requester_user_id='u')
        def _assert_current(self,*args,**kwargs):
            return canvas
        async def continue_commission(self,**kwargs):
            calls.append(kwargs['event_key'])
    import novelvideo.task_state as task_state
    task = SimpleNamespace(task_id='actual-native-task',status='completed',result={'image_path':str(output/'picture.png')},error=None)
    monkeypatch.setattr(task_state,'get_task_manager',lambda:SimpleNamespace(list_tasks_for_project=lambda ctx:[task],_task_key_for_state=lambda task:'actual-task'))
    run = {'run_id':'offline-native','commission_ref':saved_commission['commission_ref'],'commission_phase':'preparing_materials','canvas_id':'c','requester_user_id':'u'}
    await ReferenceProductionObserver(Coordinator()).observe_run(run)
    candidate = production_event(store,canvas,c,workflow_state_dir=state)
    assert candidate is not None and candidate['event'] is None
    assert calls == [candidate['event_key']]
    assert store.read_latest(registered['id'])['revision'] == registered['revision']


def observer_branches(tmp_path, scope='可编辑合成'):
    from novelvideo.freezone.remix.reference_branches import prepare_reference_branches
    store = RemixArtifactStore(tmp_path,'p')
    parent = commissions.create_reference_commission(store,canvas_id='c',requester_user_id='u',analysis_node_id='a',goal='g',analysis_evidence={'task_id':'original','result_fingerprint':'original-hash'},production_scope=scope)
    parent = commissions.advance_reference_commission(store,parent['commission_ref'],{'type':'direction_confirmed','direction':{'target_goal':'g'}},1,host_evidence=commissions.AuthenticatedDecisionEvidence('p','c','u','direction-message'))
    refs = []
    canvas = {'nodes':[{'id':'a','data':{}}],'edges':[]}
    for index in range(2):
        plan = store.create('production_plan.v2',{'schema_version':2,'goal':'g','output':{'width':1920,'height':1080,'fps_num':24,'fps_den':1},'beats':[{'id':'b','purpose':f'branch {index}','duration_frames':48}]},[])
        refs.append({'artifact_id':plan['id'],'revision':1,'project_id':'p'})
        canvas['nodes'].append({'id':f'p{index}','data':{'productionPlanV2Ref':{'artifactId':plan['id'],'revision':1}}})
        canvas['edges'].append({'source':'a','target':f'p{index}'})
    result = prepare_reference_branches(store,parent['commission_ref'],refs,'two-real-branches')
    for child, node in zip(result['branches'], canvas['nodes'][1:]):
        bind_native_plan(tmp_path, child, node)
    return store,result['commission'],result['branches'],canvas


def approve_observer_branch(store,child,message):
    from novelvideo.freezone.remix.production_review import review_production_plan, REVIEW_CATEGORIES
    findings = [{'category':category,'entity_kind':'beat','entity_id':'b','verdict':'pass','reason':'This independent branch meets confirmed direction','evidence':['b:branch']} for category in REVIEW_CATEGORIES]
    review = review_production_plan(store,child['plan_ref'],child['commission_ref'],findings)
    child = commissions.advance_reference_commission(store,child['commission_ref'],{'type':'plan_reviewed','review_ref':{'artifact_id':review['id'],'revision':1,'project_id':'p'}},child['commission_ref']['revision'])
    return commissions.advance_reference_commission(store,child['commission_ref'],{'type':'plan_confirmed','plan_ref':child['plan_ref']},child['commission_ref']['revision'],host_evidence=commissions.AuthenticatedDecisionEvidence('p','c','u',message))


@pytest.mark.asyncio
async def test_parent_observer_advances_ready_child_without_overwriting_waiting_sibling(tmp_path,monkeypatch):
    from types import SimpleNamespace
    store,parent,children,canvas = observer_branches(tmp_path)
    child = approve_observer_branch(store,children[0],'approve-only-first')
    wakes = []
    class Coordinator:
        async def _resolve_context(self,run):
            return SimpleNamespace(output_dir=tmp_path,state_dir=tmp_path,project_id='p',requester_user_id='u')
        def _assert_current(self,*args,**kwargs):
            return canvas
        async def continue_commission(self,**kwargs):
            wakes.append(kwargs['event_key'])
    import novelvideo.task_state as task_state
    monkeypatch.setattr(task_state,'get_task_manager',lambda:SimpleNamespace(list_tasks_for_project=lambda ctx:[]))
    run = {'run_id':'parent','commission_ref':parent['commission_ref'],'commission_phase':parent['phase'],'canvas_id':'c','requester_user_id':'u'}
    observer = ReferenceProductionObserver(Coordinator())
    await observer.observe_run(run)
    progressed = commissions.read_reference_commission(store,child['commission_ref'],latest=True)
    untouched = commissions.read_reference_commission(store,children[1]['commission_ref'],latest=True)
    assert progressed['phase'] == 'composing'
    assert untouched['phase'] == 'awaiting_plan_review' and untouched['commission_ref']['revision'] == 1
    assert progressed['plan_ref'] != untouched['plan_ref']
    assert len(wakes) == 1
    run['commission_event_receipts'] = wakes[:]
    await observer.observe_run(run)
    assert len(wakes) == 1
    assert commissions.read_reference_commission(store,parent['commission_ref'],latest=True)['phase'] != 'completed'


@pytest.mark.asyncio
async def test_parent_completion_requires_both_independent_real_user_approvals(tmp_path):
    from types import SimpleNamespace
    store,parent,children,canvas = observer_branches(tmp_path,'仅作品方案')
    first = approve_observer_branch(store,children[0],'approve-first-branch')
    assert first['phase'] == 'completed'
    calls = []
    class Coordinator:
        async def _resolve_context(self,run):
            return SimpleNamespace(output_dir=tmp_path,state_dir=tmp_path,project_id='p',requester_user_id='u')
        def _assert_current(self,*args,**kwargs):
            return canvas
        async def continue_commission(self,**kwargs):
            calls.append(kwargs['event_key'])
    observer = ReferenceProductionObserver(Coordinator())
    run = {'run_id':'parent','commission_ref':parent['commission_ref'],'commission_phase':parent['phase'],'canvas_id':'c','requester_user_id':'u'}
    await observer.observe_run(run)
    incomplete = commissions.read_reference_commission(store,parent['commission_ref'],latest=True)
    assert incomplete['phase'] != 'completed' and incomplete.get('plan_ref') is None
    assert commissions.read_reference_commission(store,children[1]['commission_ref'],latest=True)['phase'] == 'awaiting_plan_review'
    run['commission_event_receipts'] = calls[:]
    second = approve_observer_branch(store,children[1],'approve-second-branch')
    await observer.observe_run(run)
    completed = commissions.read_reference_commission(store,parent['commission_ref'],latest=True)
    assert completed['phase'] == 'completed' and completed.get('plan_ref') is None
    assert {row['plan_ref']['artifact_id'] for row in completed['plan_branches']} == {first['plan_ref']['artifact_id'],second['plan_ref']['artifact_id']}
    assert first['decisions'][-1]['message_id'] == 'approve-first-branch'
    assert second['decisions'][-1]['message_id'] == 'approve-second-branch'
    run['commission_event_receipts'] = calls[:]
    await observer.observe_run(run)
    assert len(calls) == 2
    assert commissions.read_reference_commission(store,parent['commission_ref'],latest=True)['commission_ref'] == completed['commission_ref']


def test_completed_child_with_changed_saved_plan_cannot_complete_parent(tmp_path):
    from novelvideo.chat.reference_production_observer import _completed_branch_current
    store,parent,children,canvas = observer_branches(tmp_path,'仅作品方案')
    completed = [approve_observer_branch(store,child,f'approve-{index}') for index,child in enumerate(children)]
    assert all(_completed_branch_current(store,canvas,child) for child in completed)
    ref = completed[0]['plan_ref']
    plan = store.read(ref['artifact_id'],ref['revision'])
    payload = plan['payload']
    payload['beats'][0]['purpose'] = 'unapproved changed branch'
    store.revise(plan['id'],plan['revision'],payload,plan['upstream_refs'])
    assert not _completed_branch_current(store,canvas,completed[0])
    with pytest.raises(ValueError):
        commissions.advance_reference_commission(store,parent['commission_ref'],{'type':'branches_completed','commission_refs':[child['commission_ref'] for child in completed]},parent['commission_ref']['revision'])
    assert commissions.read_reference_commission(store,parent['commission_ref'],latest=True)['phase'] != 'completed'


@pytest.mark.asyncio
@pytest.mark.parametrize('decision', ['direction', 'plan'])
@pytest.mark.parametrize('guard', ['valid', 'pending', 'stale_source', 'wrong_actor'])
async def test_scan_recovers_persisted_human_decision_after_missed_notification(tmp_path, decision, guard):
    from types import SimpleNamespace
    from novelvideo.chat.reference_remix_continuation import ReferenceRemixStore

    artifacts = RemixArtifactStore(tmp_path, 'p')
    if decision == 'direction':
        pending = commissions.create_reference_commission(artifacts, canvas_id='c', requester_user_id='u',
            analysis_node_id='a', goal='g', analysis_evidence={'task_id':'t','result_fingerprint':'h'})
        if guard != 'pending':
            commissions.advance_reference_commission(artifacts, pending['commission_ref'],
                {'type':'direction_confirmed','direction':{'target_goal':'g'}}, 1,
                host_evidence=commissions.AuthenticatedDecisionEvidence('p','c','u','human-direction-message'))
        expected_phase = 'drafting_plan'
    else:
        from novelvideo.freezone.remix.production_review import review_production_plan, REVIEW_CATEGORIES
        artifacts, pending, plan, _ = setup_branch(tmp_path)
        plan_ref = {'artifact_id':plan['id'], 'revision':1, 'project_id':'p'}
        pending = commissions.advance_reference_commission(artifacts, pending['commission_ref'],
            {'type':'plan_ready','plan_ref':plan_ref}, 2)
        findings = [{'category':category,'entity_kind':'beat','entity_id':'b','verdict':'pass',
            'reason':'Concrete beat meets confirmed direction','evidence':['b:intro']} for category in REVIEW_CATEGORIES]
        review = review_production_plan(artifacts, plan_ref, pending['commission_ref'], findings)
        pending = commissions.advance_reference_commission(artifacts, pending['commission_ref'],
            {'type':'plan_reviewed','review_ref':{'artifact_id':review['id'],'revision':1,'project_id':'p'}}, 3)
        if guard != 'pending':
            commissions.advance_reference_commission(artifacts, pending['commission_ref'],
                {'type':'plan_confirmed','plan_ref':plan_ref}, 4,
                host_evidence=commissions.AuthenticatedDecisionEvidence('p','c','u','human-plan-message'))
        expected_phase = 'preparing_materials'

    canvas = {'nodes':[{'id':'a','data':{}}], 'edges':[]}
    if guard == 'pending':
        if decision == 'direction':
            for index in range(2):
                saved = artifacts.create('production_plan.v2', {'schema_version':2,'goal':'g',
                    'output':{'width':1920,'height':1080,'fps_num':24,'fps_den':1},
                    'beats':[{'id':'b','purpose':'intro','duration_frames':48}]}, [])
                node_id = f'plan-{index}'
                canvas['nodes'].append({'id':node_id,'data':{'productionPlanV2Ref':{'artifactId':saved['id'],'revision':1}}})
                canvas['edges'].append({'source':'a','target':node_id})
        else:
            changed = plan['payload']
            changed['beats'][0]['purpose'] = 'changed before human confirmation'
            artifacts.revise(plan['id'], 1, changed, [])
            canvas['nodes'].append({'id':'plan','data':{'productionPlanV2Ref':{'artifactId':plan['id'],'revision':2}}})
            canvas['edges'].append({'source':'a','target':'plan'})

    runs = ReferenceRemixStore(tmp_path / 'runs.db')
    runs.create(dict(run_id='r', project_id='p', canvas_id='c', requester_user_id='u', agent_id='main',
        idempotency_key='r', request_fingerprint='r', status=pending['phase'],
        commission_phase=pending['phase'], commission_ref=pending['commission_ref']))
    fences, wakes = [], []
    class Coordinator:
        store = runs
        async def _resolve_context(self, run):
            return SimpleNamespace(output_dir=tmp_path, state_dir=tmp_path, project_id='p',
                requester_user_id='other' if guard == 'wrong_actor' else 'u')
        def _assert_current(self, ctx, run, **kwargs):
            fences.append(run['run_id'])
            if guard == 'stale_source':
                raise ValueError('source replaced')
            return canvas
        async def _notify(self, *args):
            pass
        async def continue_commission(self, **kwargs):
            wakes.append(kwargs['event_key'])
            latest = commissions.read_reference_commission(artifacts, pending['commission_ref'], latest=True)
            assert runs.wake_waiting(kwargs['ctx'], 'r', latest['commission_ref'], latest['phase'],
                event_key=kwargs['event_key']) is not None

    observer = ReferenceProductionObserver(Coordinator())
    async def material_snapshot(ctx, run):
        return {}
    observer._material_snapshot = material_snapshot
    assert await observer.scan_once() == (1 if guard == 'valid' else 0)
    # A material snapshot may settle a task and publish a newer canvas
    # revision; the observer then checks the source fence again before use.
    assert fences in (['r'], ['r', 'r'])
    assert len(wakes) == (1 if guard == 'valid' else 0)
    if guard == 'valid':
        assert runs.get('r')['commission_phase'] == expected_phase
        assert await observer.scan_once() == 0
        assert len(wakes) == 1
    else:
        assert runs.get('r')['commission_phase'] == pending['phase']
