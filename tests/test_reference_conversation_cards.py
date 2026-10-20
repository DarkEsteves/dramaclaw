from copy import deepcopy
from types import SimpleNamespace
import pytest
from novelvideo.freezone.remix.store import RemixArtifactStore
from novelvideo.freezone.remix.reference_commission import create_reference_commission
from novelvideo.freezone.remix.reference_director import prepare_reference_decision, confirm_reference_decision
from test_plan_confirmation_contract import reviewed_plan  # noqa: F401, F811

def facts(tmp_path):
    store = RemixArtifactStore(tmp_path, 'p', created_by='u')
    commission = create_reference_commission(store, canvas_id='c', requester_user_id='u', analysis_node_id='analysis', goal='咖啡问答', analysis_evidence={'task_id':'original-task', 'result_fingerprint':'real'})
    decision = prepare_reference_decision(store, commission['commission_ref'], 'direction', {'target_goal':'咖啡问答'}, '保留问答，替换咖啡', 'direction-one')
    canvas = {'project_id':'p', 'canvas_id':'c', 'nodes':[{'id':'analysis'}], 'edges':[]}
    run = {'project_id':'p', 'canvas_id':'c', 'agent_id':'main', 'requester_user_id':'u', 'analysis_node_id':'analysis', 'commission_ref':commission['commission_ref'], 'run_id':'original-run'}
    return store, commission, decision, canvas, run

def read(store, commission, decision, canvas, run):
    from novelvideo.freezone.remix.conversation_cards import read_conversation_cards
    return read_conversation_cards(store, canvas=canvas, canvas_id='c', requester_user_id='u', agent_id='main', runs=[run], commission_id=commission['commission_ref']['artifact_id'], refs=[decision['decision_ref']])

def test_native_card_hydrates_the_same_confirmed_identity_and_actual_user_receipt(tmp_path):
    store,c,d,canvas,run=facts(tmp_path)
    pending=read(store,c,d,canvas,run)
    assert pending['items'][0]['status']=='pending'
    confirm_reference_decision(store,d['decision_ref'],project_id='p',canvas_id='c',requester_user_id='u')
    confirmed=read(store,c,d,canvas,run)
    assert confirmed['items'][0]['card_id']==pending['items'][0]['card_id']
    assert confirmed['items'][0]['status']=='confirmed'
    assert confirmed['items'][0]['ref']['revision']==2
    assert confirmed['items'][0]['summary']=='保留问答，替换咖啡'

@pytest.mark.parametrize('change',[{'agent_id':'other'}, {'requester_user_id':'peer'}, {'canvas_id':'other'}, {'commission_ref':{'artifact_id':'peer','revision':1}}])
def test_native_card_rejects_cross_scope_run_association(tmp_path,change):
    store,c,d,canvas,run=facts(tmp_path)
    with pytest.raises(PermissionError): read(store,c,d,canvas,{**run,**change})

def test_native_card_rejects_private_canvas_and_unrelated_commission(tmp_path):
    store,c,d,canvas,run=facts(tmp_path)
    with pytest.raises(PermissionError): read(store,c,d,{**canvas,'access_model':'private','owner_principal_id':'peer'},run)
    peer=create_reference_commission(store,canvas_id='c',requester_user_id='u',analysis_node_id='analysis',goal='other',analysis_evidence={'task_id':'other','result_fingerprint':'other'})
    other=prepare_reference_decision(store,peer['commission_ref'],'direction',{'target_goal':'other'},'other','other-direction')
    with pytest.raises(PermissionError): read(store,c,other,canvas,run)

@pytest.mark.asyncio
async def test_host_publisher_persists_only_native_refs_with_a_genuine_chat_association(tmp_path,monkeypatch):
    from novelvideo.freezone.remix.conversation_cards import publish_conversation_cards
    from novelvideo.chat.store import ChatStore, ChatScope
    from novelvideo.freezone import canvas_store
    store,c,d,canvas,run=facts(tmp_path)
    canvas.update(revision=1)
    canvas_store.save_canvas(tmp_path,'c',base_revision=None,build_payload=lambda _:deepcopy(canvas))
    ctx=SimpleNamespace(project_id='p',project_name='demo',state_dir=tmp_path,output_dir=tmp_path,requester_user_id='u',requester_username='alice')
    chat=ChatStore()
    writes=[];original=chat.append_ui_event_async
    async def tracked(*args,**kwargs): writes.append(args); return await original(*args,**kwargs)
    monkeypatch.setattr(chat,'append_ui_event_async',tracked)
    await publish_conversation_cards(ctx,canvas_id='c',runs=[run],chat=chat)
    await publish_conversation_cards(ctx,canvas_id='c',runs=[run],chat=chat)
    messages=chat.list_messages('alice',ChatScope(kind='project',id='demo',surface='freezone',canvas_id='c',agent_id='main',state_dir=str(tmp_path)))
    assert messages == []
    assert writes == []
    assert store.read_latest(d['decision_ref']['artifact_id'])['payload']['status'] == 'pending'


@pytest.mark.asyncio
async def test_passed_plan_decision_is_internal_and_not_published_as_a_card(reviewed_plan):
    from novelvideo.freezone.remix.conversation_cards import publish_conversation_cards
    from novelvideo.chat.store import ChatStore, ChatScope

    _, store, commission, _ = reviewed_plan
    ctx = SimpleNamespace(project_id='p', project_name='demo', state_dir=store.project_dir,
                          output_dir=store.project_dir, requester_user_id='u',
                          requester_username='alice')
    run = {'project_id': 'p', 'canvas_id': 'c', 'agent_id': 'main',
           'requester_user_id': 'u', 'analysis_node_id': 'analysis',
           'commission_ref': commission['commission_ref'], 'run_id': 'original-run'}
    await publish_conversation_cards(ctx, canvas_id='c', runs=[run])
    messages = ChatStore().list_messages('alice', ChatScope(kind='project', id='demo',
        surface='freezone', canvas_id='c', agent_id='main', state_dir=str(store.project_dir)))
    assert messages == []

from test_reference_director_api import setup_api, BASE

def test_card_endpoint_checks_native_run_scope_and_is_read_only(setup_api,monkeypatch):
    from novelvideo.chat.reference_remix_continuation import coordinator
    client,store,c,_=setup_api
    d=prepare_reference_decision(store,c['commission_ref'],'direction',{'target_goal':'产品介绍'},'产品方向','card-route')
    run={'project_id':'p','canvas_id':'c','agent_id':'main','requester_user_id':'u','analysis_node_id':'analysis','commission_ref':c['commission_ref']}
    monkeypatch.setattr(coordinator.store,'production_for_actor',lambda *_: [run])
    import json
    response=client.get(BASE+'/conversation-cards',params={'commission_id':c['commission_ref']['artifact_id'],'refs':json.dumps([d['decision_ref']]),'agent_id':'main'})
    assert response.status_code==200,response.text
    assert response.json()['data']['items'][0]['status']=='pending'
    assert store.read_latest(d['decision_ref']['artifact_id'])['revision']==1
    denied=client.get(BASE+'/conversation-cards',params={'commission_id':c['commission_ref']['artifact_id'],'refs':json.dumps([d['decision_ref']]),'agent_id':'peer'})
    assert denied.status_code==403,denied.text

def test_user_card_confirmation_rejects_stale_branch_before_native_mutation(setup_api,monkeypatch):
    client,store,c,_=setup_api
    d=prepare_reference_decision(store,c['commission_ref'],'direction',{'target_goal':'产品介绍'},'产品方向','fenced-route')
    response=client.post(BASE+f"/decisions/{d['decision_ref']['artifact_id']}/confirm",json={'decision_ref':d['decision_ref'],'commission_ref':{**c['commission_ref'],'revision':999},'agent_id':'main','expected_projection_revision':'f'*64})
    assert response.status_code==409,response.text
    assert store.read_latest(d['decision_ref']['artifact_id'])['payload']['status']=='pending'

def test_native_confirmation_rechecks_the_expected_commission_at_the_cas_boundary(tmp_path):
    store,c,d,canvas,run=facts(tmp_path)
    from novelvideo.freezone.remix.store import RemixArtifactConflict
    store.revise(c['commission_ref']['artifact_id'],1,store.read_latest(c['commission_ref']['artifact_id'])['payload'],[])
    with pytest.raises(RemixArtifactConflict):
        confirm_reference_decision(store,d['decision_ref'],project_id='p',canvas_id='c',requester_user_id='u',expected_commission_ref=c['commission_ref'])
    assert store.read_latest(d['decision_ref']['artifact_id'])['payload']['status']=='pending'

@pytest.mark.asyncio
async def test_native_observer_publishes_cards_on_a_real_observed_event_without_a_get(monkeypatch,tmp_path):
    from novelvideo.chat.reference_production_observer import ReferenceProductionObserver
    from novelvideo.freezone.remix import conversation_cards
    from novelvideo.chat import reference_production_observer as module
    store,c,d,canvas,run=facts(tmp_path)
    ctx=SimpleNamespace(output_dir=tmp_path,state_dir=tmp_path,project_id='p',requester_user_id='u',requester_username='alice')
    run['commission_phase']='drafting_plan'
    calls=[]
    class Coordinator:
        async def _resolve_context(self,_): return ctx
        def _assert_current(self,*args,**kwargs): return canvas
        async def continue_commission(self,**kwargs): calls.append('continued')
    async def publisher(*args,**kwargs): calls.append(('cards',kwargs['runs']))
    monkeypatch.setattr(conversation_cards,'publish_conversation_cards',publisher)
    monkeypatch.setattr(module,'production_event',lambda *args,**kwargs:{'event':None,'event_key':'production:native-candidate'})
    await ReferenceProductionObserver(Coordinator()).observe_run(run)
    assert calls[0]==('cards',[run])
    assert calls[1]=='continued'

def test_applied_speech_card_requires_the_real_exact_preview_user_receipt(tmp_path):
    from test_agent_speech_review import setup, service
    from novelvideo.freezone.remix.conversation_cards import _speech_applied
    store,plan,rec=setup(tmp_path)
    api=service();review=api.get_speech_review(store,plan,'take',rec)
    preview=api.preview_speech_correction(store,plan,'take',rec,[{'word_id':review['recognized_words'][0]['id'],'text':'为什么','reason':'试听校正'}],actor_id='agent-1',actor_kind='agent')
    pref={'artifact_id':preview['id'],'revision':1}
    assert _speech_applied(store,preview) is False
    receipt=api.create_speech_correction_review(store,pref,actor_id='user-1',actor_kind='user',accepted=True)
    api.apply_speech_correction(store,pref,{'artifact_id':receipt['id'],'revision':1},'native-user-apply')
    assert _speech_applied(store,preview) is True
    # Native original words/times stay unchanged; no card snapshot supplies them.
    assert store.read(rec['artifact_id'],1)['payload']['recognized_words'][0]['text']=='为什魔'

@pytest.mark.asyncio
async def test_publisher_includes_registered_child_branch_refs_and_rejects_an_unregistered_parent_claim(tmp_path):
    from novelvideo.freezone.remix.conversation_cards import publish_conversation_cards, read_conversation_cards
    from novelvideo.freezone import canvas_store
    from novelvideo.chat.store import ChatStore,ChatScope
    store,c,d,canvas,run=facts(tmp_path)
    from novelvideo.freezone.remix.reference_branches import prepare_reference_branches
    confirm_reference_decision(store,d['decision_ref'],project_id='p',canvas_id='c',requester_user_id='u')
    c=store.read_latest(c['commission_ref']['artifact_id'])
    plan=store.create('production_plan.v2',{'schema_version':2,'goal':'咖啡问答','output':{'width':1920,'height':1080,'fps_num':24,'fps_den':1},'beats':[{'id':'b','purpose':'opening','duration_frames':48}]},[])
    group=prepare_reference_branches(store,{'artifact_id':c['id'],'revision':c['revision'],'project_id':'p'},[{'artifact_id':plan['id'],'revision':1,'project_id':'p'}],'native-branches')
    child=group['branches'][0]
    choice=prepare_reference_decision(store,child['commission_ref'],'direction',{'target_goal':'调整咖啡问答'},'branch direction','branch-card')
    parent=store.read_latest(c['id']);original_parent=parent['payload'];without_children={**original_parent,'plan_branches':[]}
    unregistered=store.revise(parent['id'],parent['revision'],without_children,parent['upstream_refs'])
    with pytest.raises(PermissionError):
        read_conversation_cards(store,canvas=canvas,canvas_id='c',requester_user_id='u',agent_id='main',runs=[run],commission_id=child['commission_ref']['artifact_id'],refs=[choice['decision_ref']])
    store.revise(parent['id'],unregistered['revision'],original_parent,parent['upstream_refs'])
    canvas.update(revision=1);canvas_store.save_canvas(tmp_path,'c',base_revision=None,build_payload=lambda _:deepcopy(canvas))
    ctx=SimpleNamespace(project_id='p',project_name='demo',state_dir=tmp_path,output_dir=tmp_path,requester_user_id='u',requester_username='alice')
    chat=ChatStore();await publish_conversation_cards(ctx,canvas_id='c',runs=[run],chat=chat)
    messages=chat.list_messages('alice',ChatScope(kind='project',id='demo',surface='freezone',canvas_id='c',agent_id='main',state_dir=str(tmp_path)))
    assert not any(m['parts'][0]['event']['ref']['artifact_id']==choice['decision_ref']['artifact_id'] for m in messages)
    assert read_conversation_cards(store,canvas=canvas,canvas_id='c',requester_user_id='u',agent_id='main',runs=[run],commission_id=child['commission_ref']['artifact_id'],refs=[choice['decision_ref']])['items'][0]['status']=='pending'

def test_user_native_card_confirmation_uses_the_actual_current_projection_and_publishes_history(setup_api,monkeypatch):
    from novelvideo.chat.reference_remix_continuation import coordinator
    client,store,c,_=setup_api
    d=prepare_reference_decision(store,c['commission_ref'],'direction',{'target_goal':'产品介绍'},'产品方向','fenced-positive')
    run={'project_id':'p','canvas_id':'c','agent_id':'main','requester_user_id':'u','analysis_node_id':'analysis','commission_ref':c['commission_ref'],'run_id':'native-run','status':'awaiting_direction'}
    monkeypatch.setattr(coordinator.store,'production_for_actor',lambda *_:[run])
    async def continue_existing(**kwargs): return None
    monkeypatch.setattr(coordinator,'continue_commission',continue_existing)
    actual=client.get(BASE+'/production-state',params={'commission_id':c['commission_ref']['artifact_id'],'agent_id':'main'})
    assert actual.status_code==200,actual.text
    from novelvideo.freezone.remix.reference_director import _MaterialPublicationGuard
    original_publish=RemixArtifactStore._publish
    publication_guards=[]
    def checked_publish(self,*args,**kwargs):
        guard=kwargs['guard']
        assert isinstance(guard,_MaterialPublicationGuard), 'native user writes must retain the composite canvas/artifact lease'
        assert guard.canvas_guard is not None
        guard.reassert();publication_guards.append(guard)
        return original_publish(self,*args,**kwargs)
    monkeypatch.setattr(RemixArtifactStore,'_publish',checked_publish)
    response=client.post(BASE+f"/decisions/{d['decision_ref']['artifact_id']}/confirm",json={'decision_ref':d['decision_ref'],'commission_ref':c['commission_ref'],'agent_id':'main','expected_projection_revision':actual.json()['data']['projection_revision']})
    assert response.status_code==200,response.text
    assert store.read_latest(d['decision_ref']['artifact_id'])['payload']['status']=='confirmed'
    assert len(publication_guards)==2
    assert publication_guards[0] is publication_guards[1]

@pytest.mark.asyncio
async def test_publisher_does_not_emit_direction_cards_across_bounded_batches(tmp_path):
    from novelvideo.freezone.remix.conversation_cards import publish_conversation_cards
    from novelvideo.freezone import canvas_store
    from novelvideo.chat.store import ChatStore,ChatScope
    store,c,d,canvas,run=facts(tmp_path)
    refs=[d['decision_ref']]
    for i in range(25):
        refs.append(prepare_reference_decision(store,c['commission_ref'],'direction',{'target_goal':f'choice {i}'},f'choice {i}',f'card-{i}')['decision_ref'])
    canvas.update(revision=1);canvas_store.save_canvas(tmp_path,'c',base_revision=None,build_payload=lambda _:deepcopy(canvas))
    ctx=SimpleNamespace(project_id='p',project_name='demo',state_dir=tmp_path,output_dir=tmp_path,requester_user_id='u',requester_username='alice')
    chat=ChatStore()
    await publish_conversation_cards(ctx,canvas_id='c',runs=[run],chat=chat)
    await publish_conversation_cards(ctx,canvas_id='c',runs=[run],chat=chat)
    messages=chat.list_messages('alice',ChatScope(kind='project',id='demo',surface='freezone',canvas_id='c',agent_id='main',state_dir=str(tmp_path)))
    published={m['parts'][0]['event']['ref']['artifact_id'] for m in messages}
    assert published==set()
    assert len(refs)==26


def test_projection_changed_after_advisory_read_cannot_confirm_old_card(setup_api,monkeypatch):
    from novelvideo.api.routes import reference_director as routes
    from novelvideo.chat.reference_remix_continuation import coordinator
    client,store,c,_=setup_api
    d=prepare_reference_decision(store,c['commission_ref'],'direction',{'target_goal':'产品介绍'},'产品方向','race-original')
    run={'project_id':'p','canvas_id':'c','agent_id':'main','requester_user_id':'u','analysis_node_id':'analysis','commission_ref':c['commission_ref'],'run_id':'native-run','status':'awaiting_direction'}
    monkeypatch.setattr(coordinator.store,'production_for_actor',lambda *_:[run])
    async def noop(*args,**kwargs): pass
    monkeypatch.setattr(coordinator,'continue_commission',noop);monkeypatch.setattr(routes,'_emit_director_update',noop)
    actual=client.get(BASE+'/production-state',params={'commission_id':c['commission_ref']['artifact_id'],'agent_id':'main'}).json()['data']
    original=routes._production_read
    async def changed_after_read(*args,**kwargs):
        result=await original(*args,**kwargs)
        if args[4].__name__=='read_current_production':
            prepare_reference_decision(store,c['commission_ref'],'direction',{'target_goal':'并发新选择'},'并发新选择','race-new')
        return result
    monkeypatch.setattr(routes,'_production_read',changed_after_read)
    response=client.post(BASE+f"/decisions/{d['decision_ref']['artifact_id']}/confirm",json={'decision_ref':d['decision_ref'],'commission_ref':c['commission_ref'],'agent_id':'main','expected_projection_revision':actual['projection_revision']})
    # Even a preceding advisory read may be superseded before native admission.
    if response.status_code==200 and store.read_latest(d['decision_ref']['artifact_id'])['payload']['status']=='confirmed':
        assert False,'old projection was confirmed after a genuine concurrent choice changed it'
    assert response.status_code==409,response.text
    assert store.read_latest(d['decision_ref']['artifact_id'])['payload']['status']=='superseded'


def test_native_mutation_rechecks_private_canvas_after_successful_advisory_read(setup_api,monkeypatch):
    from novelvideo.api.routes import reference_director as routes
    from novelvideo.chat.reference_remix_continuation import coordinator
    from novelvideo.freezone import canvas_store
    client,store,c,_=setup_api
    d=prepare_reference_decision(store,c['commission_ref'],'direction',{'target_goal':'产品介绍'},'产品方向','private-race')
    run={'project_id':'p','canvas_id':'c','agent_id':'main','requester_user_id':'u','analysis_node_id':'analysis','commission_ref':c['commission_ref'],'run_id':'native-run','status':'awaiting_direction'}
    monkeypatch.setattr(coordinator.store,'production_for_actor',lambda *_:[run])
    actual=client.get(BASE+'/production-state',params={'commission_id':c['commission_ref']['artifact_id'],'agent_id':'main'}).json()['data']
    original=routes._production_read
    async def private_after_read(*args,**kwargs):
        result=await original(*args,**kwargs)
        if args[4].__name__=='read_current_production':
            current=canvas_store.read_canvas(store.project_dir,'c')
            canvas_store.save_canvas(store.project_dir,'c',base_revision=current['revision'],build_payload=lambda old:{**old,'access_model':'private','owner_principal_id':'peer'})
        return result
    monkeypatch.setattr(routes,'_production_read',private_after_read)
    response=client.post(BASE+f"/decisions/{d['decision_ref']['artifact_id']}/confirm",json={'decision_ref':d['decision_ref'],'commission_ref':c['commission_ref'],'agent_id':'main','expected_projection_revision':actual['projection_revision']})
    assert response.status_code==403,response.text
    assert store.read_latest(d['decision_ref']['artifact_id'])['payload']['status']=='pending'
