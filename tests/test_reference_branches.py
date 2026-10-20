import pytest
from novelvideo.freezone.remix import reference_commission as commissions
from novelvideo.freezone.remix.store import RemixArtifactStore, RemixArtifactConflict


def setup(tmp_path):
    store = RemixArtifactStore(tmp_path, 'p')
    c = commissions.create_reference_commission(store,canvas_id='c',requester_user_id='u',analysis_node_id='a',goal='g',analysis_evidence={'task_id':'t','result_fingerprint':'hash'})
    c = commissions.advance_reference_commission(store,c['commission_ref'],{'type':'direction_confirmed','direction':{'target_goal':'g'}},1,host_evidence=commissions.AuthenticatedDecisionEvidence('p','c','u','real-user-message'))
    refs = []
    for purpose in ['opening one','opening two']:
        p = store.create('production_plan.v2',{'schema_version':2,'goal':'g','output':{'width':1920,'height':1080,'fps_num':24,'fps_den':1},'beats':[{'id':'b','purpose':purpose,'duration_frames':48}]},[])
        refs.append({'artifact_id':p['id'],'revision':1,'project_id':'p'})
    return store,c,refs


def test_branches_inherit_real_direction_and_have_independent_plan_approvals(tmp_path):
    from novelvideo.freezone.remix.reference_branches import prepare_reference_branches
    store,c,refs = setup(tmp_path)
    result = prepare_reference_branches(store,c['commission_ref'],refs,'branch-command')
    children = result['branches']
    assert len(children) == 2
    assert {b['plan_ref']['artifact_id'] for b in children} == {r['artifact_id'] for r in refs}
    for child in children:
        assert child['phase'] == 'awaiting_plan_review'
        assert child['direction'] == c['direction']
        assert child['decisions'] == c['decisions']
        assert child.get('review_ref') is None and child.get('work_ref') is None
        assert child['parent_commission_ref'] == c['commission_ref']
    assert prepare_reference_branches(store,c['commission_ref'],refs,'branch-command') == result
    assert commissions.read_reference_commission(store,c['commission_ref']).get('plan_ref') is None
    with pytest.raises(RemixArtifactConflict):
        prepare_reference_branches(store,c['commission_ref'],refs,'other-command')


def test_branch_scope_change_and_foreign_or_stale_plan_rejected(tmp_path):
    from novelvideo.freezone.remix.reference_branches import prepare_reference_branches
    store,c,refs = setup(tmp_path)
    with pytest.raises(ValueError,match='project'):
        prepare_reference_branches(store,c['commission_ref'],[{**refs[0],'project_id':'foreign'}],'foreign')
    plan = store.read(refs[0]['artifact_id'],1)
    changed = {**plan['payload'],'goal':'new goal'}
    revised = store.revise(plan['id'],1,changed,[])
    with pytest.raises(RemixArtifactConflict):
        prepare_reference_branches(store,c['commission_ref'],refs,'stale')
    with pytest.raises(ValueError,match='direction'):
        prepare_reference_branches(store,c['commission_ref'],[{**refs[0],'revision':revised['revision']}],'changed-goal')


def test_upload_association_preserves_requirement_and_requires_review(tmp_path):
    from PIL import Image
    from test_material_work import setup_work
    from novelvideo.freezone.remix.reference_branches import associate_material_upload
    store,_,prepared = setup_work(tmp_path)
    saved = store.read(prepared['work_ref']['artifact_id'],1)
    work = saved['payload']
    work['steps'][0].update(source='upload',status='awaiting_upload')
    work['steps'][0].pop('recipe_id')
    saved = store.revise(saved['id'],1,work,saved['upstream_refs'])
    ref = {'artifact_id':saved['id'],'revision':saved['revision']}
    Image.new('RGB',(12,12),'red').save(tmp_path/'picture.png')
    canvas = {'nodes':[{'id':'upload','data':{'imageUrl':'/static/projects/project/picture.png'}}]}
    result = associate_material_upload(store,ref,'image',canvas,'upload')
    assert result['work']['requirements'] == work['requirements']
    step = result['work']['steps'][0]
    assert step['node_id'] == 'upload' and step['status'] == 'candidate'
    assert not step.get('accepted_resource') and not step.get('review_ref')
    assert associate_material_upload(store,result['work_ref'],'image',canvas,'upload') == result
    with pytest.raises(RemixArtifactConflict):
        associate_material_upload(store,ref,'image',canvas,'upload')
    with pytest.raises(ValueError):
        associate_material_upload(store,result['work_ref'],'video',canvas,'upload')
    canvas['nodes'][0]['data']['workflowResultStale'] = True
    with pytest.raises(ValueError,match='stale'):
        associate_material_upload(store,result['work_ref'],'image',canvas,'upload')


def test_branch_plan_confirmation_is_independent(tmp_path):
    from novelvideo.freezone.remix.reference_branches import prepare_reference_branches
    from novelvideo.freezone.remix.production_review import REVIEW_CATEGORIES,review_production_plan
    store,parent,refs = setup(tmp_path)
    result = prepare_reference_branches(store,parent['commission_ref'],refs,'multiple')
    child,sibling = result['branches']
    findings = [{'category':x,'entity_kind':'beat','entity_id':'b','verdict':'pass','reason':'Opening meets confirmed goal','evidence':['b:opening']} for x in REVIEW_CATEGORIES]
    review = review_production_plan(store,child['plan_ref'],child['commission_ref'],findings)
    child = commissions.advance_reference_commission(store,child['commission_ref'],{'type':'plan_reviewed','review_ref':{'artifact_id':review['id'],'revision':1,'project_id':'p'}},1)
    child = commissions.advance_reference_commission(store,child['commission_ref'],{'type':'plan_confirmed','plan_ref':child['plan_ref']},2,host_evidence=commissions.AuthenticatedDecisionEvidence('p','c','u','only-first-approved'))
    assert child['phase']=='preparing_materials'
    assert commissions.read_reference_commission(store,sibling['commission_ref'],latest=True)['phase']=='awaiting_plan_review'
    assert commissions.read_reference_commission(store,result['commission']['commission_ref'])['phase']=='awaiting_plan_review'


@pytest.mark.asyncio
async def test_branch_api_rejects_unrelated_canvas_plan(monkeypatch,tmp_path):
    from types import SimpleNamespace
    from fastapi import HTTPException
    from novelvideo.api.routes import reference_director as director,reference_branches as routes
    store,c,refs = setup(tmp_path)
    ctx = SimpleNamespace(requester_user_id='u')
    canvas = {'nodes':[{'id':'a','type':'videoStoryNode'},{'id':'plan','type':'scriptNode','data':{'productionPlanV2Ref':{'artifactId':refs[0]['artifact_id'],'revision':1}}}],'edges':[]}
    async def context(*args):
        return ctx,canvas,store
    monkeypatch.setattr(director,'_context',context)
    with pytest.raises(HTTPException,match='descend'):
        await routes.branches('p','c',routes.BranchBody(commission_ref=c['commission_ref'],plan_refs=[refs[0]],command_id='api-branch'),{})
    canvas['edges']=[{'source':'a','target':'plan'}]
    async def notify(*args):
        pass
    monkeypatch.setattr(director,'_notify_commission',notify)
    result = await routes.branches('p','c',routes.BranchBody(commission_ref=c['commission_ref'],plan_refs=[refs[0]],command_id='api-branch'),{})
    assert result['data']['branches'][0]['phase']=='awaiting_plan_review'


def test_fabricated_branch_group_reference_is_rejected(tmp_path):
    store,parent,refs = setup(tmp_path)
    saved = store.read(parent['commission_ref']['artifact_id'],parent['commission_ref']['revision'])
    payload = {**saved['payload'],'plan_branches':[{'command_id':'fake','parent_revision':2,'result_parent_revision':3,'plan_ref':refs[0],'commission_ref':refs[0]}]}
    with pytest.raises(ValueError,match='branch'):
        store.revise(saved['id'],saved['revision'],payload,refs)
