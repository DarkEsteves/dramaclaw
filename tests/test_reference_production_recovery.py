"""Public human recovery of persisted current commission/source evidence."""
from dataclasses import replace
import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from test_production_advancement import production as production, completed_native_source_adoption
from test_reference_remix_continuation import context
from novelvideo.chat import reference_remix_continuation as continuation
from novelvideo.freezone import canvas_store
from novelvideo.freezone.remix import production_automation as controls
from novelvideo.freezone.remix.store import RemixArtifactStore


def register_origin(coordinator,ctx,commission,canvas,status='stopped'):
    source=next(n for n in canvas['nodes'] if n['id']=='source')
    analysis=next(n for n in canvas['nodes'] if n['id']==commission['analysis_node_id'])
    run=dict(run_id='origin',project_id=ctx.project_id,requester_user_id=ctx.requester_user_id,
        username=ctx.requester_username,canvas_id=canvas['canvas_id'],agent_id='main',idempotency_key='original',
        request_fingerprint='original',status=status,commission_ref=commission['commission_ref'],commission_phase=commission['phase'],
        source_node_id='source',analysis_node_id=commission['analysis_node_id'],source_fingerprint=continuation._node_fingerprint(source),
        analysis_fingerprint=continuation._node_fingerprint(analysis),result_fingerprint=continuation._node_fingerprint(analysis),
        analysis_node_snapshot=analysis,source_node_snapshot=source,task_id='original-analysis',job_id='original-job',
        task_type='freezone_video_story',context_json=continuation._serialize_context(ctx),goal=commission['goal'],inputs={})
    run['edge_fingerprint']=coordinator._edges_fingerprint(canvas,run)
    coordinator.store.create(run)
    return run


def add_newer_history(coordinator,ctx,origin):
    # Real SQLite rows, same scope, newer than the enrolled production.
    for i in range(100):
        history={**origin,'run_id':'history-'+str(i),'idempotency_key':'history-'+str(i),
            'request_fingerprint':'history-'+str(i),'status':'analysis_failed'}
        history.pop('commission_ref',None)
        coordinator.store.create(history)
    for field,value in [('requester_user_id','other'),('project_id','other'),('canvas_id','other'),('agent_id','private-other')]:
        foreign={**origin,field:value,'run_id':'foreign-'+field,'idempotency_key':'foreign-'+field,
            'request_fingerprint':'foreign-'+field}
        coordinator.store.create(foreign)
    assert len(coordinator.store.list_for_actor(ctx,origin['canvas_id'],'main'))==100
    assert all(row['run_id']!='origin' for row in coordinator.store.list_for_actor(ctx,origin['canvas_id'],'main'))


def app_for(ctx,coordinator,monkeypatch,identity=None):
    from novelvideo.api.routes import reference_director as route
    from novelvideo.api.auth import get_api_user
    monkeypatch.setattr(continuation,'coordinator',coordinator)
    user=identity or {'user_id':ctx.requester_user_id,'username':ctx.requester_username,'credential_kind':'browser_session'}
    async def scoped(project,canvas_id,actual,role='editor',**kwargs):
        actor=actual.get('user_id') or ctx.requester_user_id
        actual_ctx=ctx if actor==ctx.requester_user_id else replace(ctx,requester_user_id=actor,requester_username=actor)
        canvas=canvas_store.read_canvas(ctx.state_dir,canvas_id)
        if project!=ctx.project_id:
            raise HTTPException(404,'project')
        if canvas.get('access_model') in {'private','user_private'} and canvas.get('owner_principal_id')!=actor:
            raise HTTPException(403,'private canvas')
        return actual_ctx,canvas,RemixArtifactStore(ctx.output_dir,ctx.project_id,created_by=actor)
    async def emit(*a,**kw):pass
    monkeypatch.setattr(route,'_context',scoped)
    monkeypatch.setattr(route,'_emit_director_update',emit)
    app=FastAPI()
    app.include_router(route.router,prefix='/api/v1')
    app.dependency_overrides[get_api_user]=lambda:user
    return app,user


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmation_crash", [False, True])
async def test_cold_http_recovery_resumes_saved_source_without_current_plan_projection(production,monkeypatch,tmp_path,confirmation_crash):
    ctx,store,_,_,canvas=production
    canvas['nodes'].append({'id':'source','type':'videoNode','data':{'videoUrl':'/static/projects/p/reference.mp4'}})
    canvas['edges'].append({'source':'source','target':'a'})
    def owned(ctx,store,commission,plan_ref,operation):
        controls.enroll_production_automation(ctx,canvas_id='c',commission_ref=commission['commission_ref'],command_id='first')
        controls.bind_automation_operation(ctx,canvas_id='c',commission_id=commission['commission_ref']['artifact_id'],generation=1,operation_id=operation['operation_id'])
    ctx,store,commission,plan_ref,operation,result,_=await completed_native_source_adoption(production,monkeypatch,before_worker=owned)
    if confirmation_crash:
        from novelvideo.freezone.remix.operations import RemixOperationStore
        confirm=RemixOperationStore.confirm
        failed=[]
        def crash(self,*a,**kw):
            answer=confirm(self,*a,**kw)
            if not failed:
                failed.append(True)
                raise RuntimeError('after actual confirmation before commission publication')
            return answer
        monkeypatch.setattr(RemixOperationStore,'confirm',crash)
        with pytest.raises(RuntimeError,match='actual confirmation'):
            await controls.advance_enrolled_production(ctx,{'run_id':'origin','canvas_id':'c','agent_id':'main','commission_ref':commission['commission_ref']},runs=[])
        assert RemixOperationStore(ctx.output_dir,ctx.project_id).get(operation['operation_id'])['status']=='completed'
    coordinator=continuation.ReferenceRemixCoordinator(continuation.ReferenceRemixStore(tmp_path/'runs.db'))
    origin=register_origin(coordinator,ctx,commission,canvas_store.read_canvas(ctx.state_dir,'c'),status=commission['phase'])
    add_newer_history(coordinator,ctx,origin)
    assert await coordinator.stop_automation_scope(ctx=ctx,canvas_id='c',agent_id='main')==1
    assert not controls.read_production_control(ctx,canvas_id='c',commission_id=commission['commission_ref']['artifact_id'])['active']
    app,_=app_for(ctx,coordinator,monkeypatch)
    def no_backend():raise AssertionError('resume tried to enqueue another native/provider job')
    monkeypatch.setattr('novelvideo.ports.get_task_backend',no_backend)
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
        base='/api/v1/projects/p/freezone/canvases/c/director'
        initial=await client.get(base+'/production-state',params={'commission_id':commission['commission_ref']['artifact_id']})
        assert initial.status_code==200,initial.text
        assert initial.json()['data']['plan_revision_sync']['status']==(
            'pending_commission_sync' if confirmation_crash else 'pending_canvas_save'
        )
        recovery=await client.get(base+'/production-recovery',params={'agent_id':'main'})
        assert recovery.status_code==200,recovery.text
        assert recovery.json()['data']['items'][0]['commission_ref']==commission['commission_ref']
        assert coordinator.store.get('origin')['status']=='stopped'
        body={'commission_ref':commission['commission_ref'],'command_id':'resume','agent_id':'main'}
        resumed=await client.post(base+'/resume-production',json=body)
        assert resumed.status_code==200,resumed.text
        assert resumed.json()['data']['accepted'] is True
        fresh=await client.get(base+'/production-state',params={'commission_id':commission['commission_ref']['artifact_id']})
        assert fresh.status_code==200,fresh.text
        assert fresh.json()['data']['plan_ref']['revision']==result['plan_ref']['revision']
        replay=await client.post(base+'/resume-production',json=body)
        assert replay.status_code==200,replay.text
        assert replay.json()['data']['accepted'] is True
        await coordinator.stop_automation_scope(ctx=ctx,canvas_id='c',agent_id='main')
        after_stop=await client.post(base+'/resume-production',json=body)
        assert after_stop.status_code==200,after_stop.text
        assert coordinator.store.get('origin')['status']=='stopped'
        assert not controls.read_production_control(ctx,canvas_id='c',commission_id=commission['commission_ref']['artifact_id'])['active']
    assert coordinator.store.get('origin')['task_id']=='original-analysis'
    assert store.read_latest(plan_ref['artifact_id'])['revision']==result['plan_ref']['revision']


@pytest.mark.asyncio
async def test_http_child_only_resume_creates_scoped_watch_and_keeps_parent_sibling_stopped(tmp_path,monkeypatch):
    from test_reference_branches import setup
    from novelvideo.freezone.remix.reference_branches import prepare_reference_branches
    store,parent,refs=setup(tmp_path)
    branches=prepare_reference_branches(store,parent['commission_ref'],refs,'branches')
    parent=branches['commission']
    child,sibling=branches['branches']
    ctx=replace(context(tmp_path,actor='u',project='p'),output_dir=tmp_path,state_dir=tmp_path/'state')
    canvas={'project_id':'p','canvas_id':'c','revision':1,'access_model':'shared','nodes':[{'id':'source','type':'videoNode','data':{'videoUrl':'/ref.mp4'}},{'id':'a','type':'videoStoryNode','data':{}}], 'edges':[{'source':'source','target':'a'}]}
    for i,ref in enumerate(refs):
        canvas['nodes'].append({'id':'plan'+str(i),'type':'scriptNode','data':{'productionPlanV2Ref':{'artifactId':ref['artifact_id'],'revision':1}}})
        canvas['edges'].append({'source':'a','target':'plan'+str(i)})
    canvas_store.save_canvas(ctx.state_dir,'c',base_revision=None,build_payload=lambda _:canvas)
    controls.enroll_production_automation(ctx,canvas_id='c',commission_ref=parent['commission_ref'],command_id='first')
    controls.enroll_registered_branch_controls(ctx,canvas_id='c',parent_ref=parent['commission_ref'])
    coordinator=continuation.ReferenceRemixCoordinator(continuation.ReferenceRemixStore(tmp_path/'runs.db'))
    origin=register_origin(coordinator,ctx,parent,canvas,status=parent['phase'])
    add_newer_history(coordinator,ctx,origin)
    assert await coordinator.stop_automation_scope(ctx=ctx,canvas_id='c',agent_id='main')==1
    app,user=app_for(ctx,coordinator,monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
        base='/api/v1/projects/p/freezone/canvases/c/director'
        recovery=await client.get(base+'/production-recovery',params={'agent_id':'main'})
        assert {item['commission_ref']['artifact_id'] for item in recovery.json()['data']['items']}=={row['commission_ref']['artifact_id'] for row in [parent,child,sibling]}
        for kind in ['agent_session','local_trusted_agent']:
            user['credential_kind']=kind
            assert (await client.post(base+'/resume-production',json={'commission_ref':child['commission_ref'],'command_id':'deny','agent_id':'main'})).status_code==403
        user['credential_kind']='browser_session'
        response=await client.post(base+'/resume-production',json={'commission_ref':child['commission_ref'],'command_id':'child-resume','agent_id':'main'})
        assert response.status_code==200,response.text
        assert response.json()['data']['accepted'] is True
        user['user_id']='other'
        recovery=await client.get(base+'/production-recovery',params={'agent_id':'main'})
        assert recovery.json()['data']['items']==[]
        assert (await client.post(base+'/resume-production',json={'commission_ref':child['commission_ref'],'command_id':'foreign','agent_id':'main'})).status_code in {403,404}
    runs=coordinator.store.list_for_actor(ctx,'c','main')
    watch=next(r for r in runs if (r.get('commission_ref') or {}).get('artifact_id')==child['commission_ref']['artifact_id'])
    assert watch['task_id']=='original-analysis'
    assert watch['run_id']!='origin' and watch['status']==child['phase']
    assert coordinator.store.get('origin')['status']=='stopped'
    assert not controls.read_production_control(ctx,canvas_id='c',commission_id=parent['commission_ref']['artifact_id'])['active']
    assert not controls.read_production_control(ctx,canvas_id='c',commission_id=sibling['commission_ref']['artifact_id'])['active']
    assert controls.read_production_control(ctx,canvas_id='c',commission_id=child['commission_ref']['artifact_id'])['active']
    assert coordinator.store.get('history-99')['status']=='analysis_failed'
    assert all(coordinator.store.get('foreign-'+field)['status']==parent['phase'] for field in ['requester_user_id','project_id','canvas_id','agent_id'])
