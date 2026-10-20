from types import SimpleNamespace
import hashlib
import subprocess
import shutil
import pytest
from novelvideo.freezone.remix import production_output_review as review
from novelvideo.freezone.remix.operations import RemixOperationStore


@pytest.fixture
def scene(tmp_path,monkeypatch):
    if not shutil.which('ffmpeg'):
        pytest.skip('ffmpeg unavailable')
    relative='freezone/remix_renders/test.mp4'
    path=tmp_path/relative
    path.parent.mkdir(parents=True)
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=red:s=64x64:r=24:d=1','-c:v','libx264','-pix_fmt','yuv420p',str(path)],check=True)
    pref={'artifact_id':'plan','revision':1}
    mref={'artifact_id':'manifest','revision':1}
    cref={'artifact_id':'commission','revision':1}
    commission={'commission_ref':cref,'project_id':'p','canvas_id':'c','requester_user_id':'u','phase':'reviewing_output','plan_ref':pref,'manifest_ref':mref}
    plan={'id':'plan','revision':1,'schema':'production_plan.v2','payload':{'resources':[],'placements':[]}}
    manifest={'schema':'composition_manifest.v2','payload':{'source_versions':[pref],'media_bindings':[],'audio_events':[],'output_spec':{'width':64,'height':64,'fps_num':24,'fps_den':1,'duration_frames':24}}}
    data={'productionPlanRenderedRef':{'artifactId':'plan','revision':1},'productionPlanNodeId':'plan-node','productionPlanV2Ref':{'artifactId':'plan','revision':1},'remixManifestRef':{'artifactId':'manifest','revision':1},'remixRenderManifestRef':{'artifactId':'manifest','revision':1},'remixRenderOperationId':'op','remixInputsCurrent':True,'remixInputFingerprint':'fingerprint','remixNodeRevision':1,'resultVideoUrl':'/api/v1/projects/p/media/'+relative}
    canvas={'id':'c','nodes':[{'id':'compose','data':data},{'id':'plan-node','data':{'productionPlanV2Ref':{'artifactId':'plan','revision':1}}}],'edges':[{'source':'plan-node','target':'compose'}]}
    operation={'status':'completed','producer_version':'remix-render.v1','canvas_id':'c','node_id':'compose','input_refs':[mref],'node_revision':1,'normalized_parameters':{'input_fingerprint':'fingerprint'},'task_id':'task','input_media_hashes':[],'output_refs':[{'project_path':relative}]}
    monkeypatch.setattr(RemixOperationStore,'get',lambda self,oid:operation)
    from novelvideo.freezone.remix import reference_commission
    monkeypatch.setattr(reference_commission,'read_reference_commission',lambda *a,**k:commission)
    class Store:
        project_dir=tmp_path
        project_id='p'
        def read_latest(self,identity): return plan
        def read(self,identity,revision): return manifest if identity=='manifest' else plan
        def create(self,schema,payload,refs): return {'id':'review','revision':1,'payload':review.ProductionOutputReview.model_validate(payload).model_dump(),'schema':schema,'upstream_refs':refs}
    return Store(),SimpleNamespace(project_id='p',requester_user_id='u'),canvas,cref,plan,manifest,operation


def test_encoded_video_requires_user_playback(scene):
    store,ctx,canvas,cref,*_=scene
    saved=review.prepare_output_review(store,ctx,canvas,cref,'compose')
    assert saved['payload']['status']=='needs_review'
    assert saved['payload']['technical']['status']=='passed'
    assert saved['payload']['media']['width_px']==64
    assert saved['payload']['content']['status']=='unavailable'


def test_stale_plan_rejected(scene):
    store,ctx,canvas,cref,plan,*_=scene
    plan['revision']=2
    with pytest.raises(ValueError,match='current plan'):
        review.prepare_output_review(store,ctx,canvas,cref,'compose')


def test_changed_source_file_rejected(scene):
    store,ctx,canvas,cref,plan,manifest,operation=scene
    source=store.project_dir/'source.txt'
    source.write_text('original')
    digest='sha256:'+hashlib.sha256(source.read_bytes()).hexdigest()
    manifest['payload']['media_bindings']=[{'project_path':'source.txt','media_hash':digest}]
    operation['input_media_hashes']=[digest]
    source.write_text('changed')
    with pytest.raises(ValueError,match='source media changed'):
        review.prepare_output_review(store,ctx,canvas,cref,'compose')


def test_agent_cannot_confirm(scene):
    store,ctx,canvas,*_=scene
    with pytest.raises(PermissionError):
        review.confirm_output_review(store,ctx,canvas,{},actor_kind='agent',viewed_complete_media=True)


@pytest.mark.asyncio
async def test_agent_confirmation_route_denied():
    from novelvideo.api.routes.reference_output import confirm,ConfirmOutputRequest
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await confirm('p','c',ConfirmOutputRequest(review_ref={},viewed_complete_media=True),user={'credential_kind':'agent_session'})
    assert exc.value.status_code==403


def test_changed_output_rejects_confirmation(scene):
    store,ctx,canvas,cref,*_=scene
    saved=review.prepare_output_review(store,ctx,canvas,cref,'compose')
    original_read=store.read
    store.read=lambda identity,revision:saved if identity=='review' else original_read(identity,revision)
    store.read_latest=lambda identity:saved if identity=='review' else scene[4]
    saved['payload']['media']['media_hash']='0'*64
    with pytest.raises(ValueError,match='output changed'):
        review.confirm_output_review(store,ctx,canvas,{'artifact_id':'review','revision':1},viewed_complete_media=True)


def test_confirm_persists_exact_media_receipt_before_advance(scene,monkeypatch):
    store,ctx,canvas,cref,*_=scene
    saved=review.prepare_output_review(store,ctx,canvas,cref,'compose')
    original_read=store.read
    store.read=lambda identity,revision:saved if identity=='review' else original_read(identity,revision)
    store.read_latest=lambda identity:saved if identity=='review' else scene[4]
    persisted=[]
    def revise(identity,revision,payload,refs):
        result={**saved,'revision':2,'payload':review.ProductionOutputReview.model_validate(payload).model_dump()}
        persisted.append(result)
        return result
    store.revise=revise
    import json
    store._canonical=lambda payload:json.dumps(payload,sort_keys=True).encode()
    from novelvideo.freezone.remix import reference_commission
    def advance(*args,**kwargs):
        assert persisted and persisted[0]['payload']['decision_receipt']['media_hash']==saved['payload']['media']['media_hash']
        assert args[2]['review_ref']['revision']==2
        return {'phase':'completed'}
    monkeypatch.setattr(reference_commission,'advance_reference_commission',advance)
    result=review.confirm_output_review(store,ctx,canvas,{'artifact_id':'review','revision':1},viewed_complete_media=True)
    assert result['review']['status']=='passed'
    assert result['commission']['phase']=='completed'


@pytest.mark.parametrize('canvas_change', ['replacement', 'disconnected', 'stale'])
def test_completed_export_requires_current_canvas_source(scene, canvas_change):
    from copy import deepcopy
    from PIL import Image
    from novelvideo.chat.reference_production_observer import _completed_branch_current
    from novelvideo.freezone.remix.material_execution import canvas_material_resource
    from novelvideo.freezone.remix.production_plan_v2 import compile_production_plan_v2, project_production_plan_v2
    from novelvideo.freezone.remix.production_review import production_decision_fingerprint
    from novelvideo.api.routes.remix_media import _inspect_existing_media

    store,ctx,canvas,cref,plan,manifest,operation = scene
    Image.new('RGB',(64,64),'red').save(store.project_dir/'source.png')
    source = {'id':'source','data':{'imageUrl':'/static/projects/p/source.png'}}
    canvas['nodes'].append(source)
    resource = canvas_material_resource(store,canvas,'source','image')
    source['data']['remixMedia'] = resource
    canvas['edges'].append({'source':'source','target':'plan-node'})
    payload = {'schema_version':2,'goal':'Product photograph','output':{'width':64,'height':64,'fps_num':24,'fps_den':1},
        'beats':[{'id':'opening','purpose':'Show product','duration_frames':24}], 'resources':[resource],
        'placements':[{'id':'picture','kind':'visual','role':'primary','track_id':'main','resource_id':resource['id'],
            'start':{'kind':'program_frame','frame':0},'end':{'kind':'program_frame','frame':24}}]}
    plan['payload'] = payload
    assert project_production_plan_v2(payload,project_dir=store.project_dir)['ready']
    manifest['payload'] = compile_production_plan_v2(payload)
    manifest['payload']['source_versions'] = [{'artifact_id':'plan','revision':1}]
    child = {'plan_ref':{'artifact_id':'plan','revision':1},'review_ref':{'artifact_id':'creative-review','revision':1},
        'direction_fingerprint':'approved-direction','production_scope':'导出成片','manifest_ref':{'artifact_id':'manifest','revision':1},
        'output_review_ref':{'artifact_id':'output-review','revision':1}}
    creative = {'payload':{'status':'passed','decision_fingerprint':production_decision_fingerprint(payload),'direction_fingerprint':'approved-direction'}}
    output = {'payload':{'status':'passed','manifest_ref':child['manifest_ref'],'plan_ref':child['plan_ref'],
        'media':_inspect_existing_media(store.project_dir,operation['output_refs'][0]['project_path']),'compose_node_id':'compose'}}
    artifacts = {'plan':plan,'manifest':manifest,'creative-review':creative,'output-review':output}
    store.read = lambda identity,revision: artifacts[identity]
    store.read_latest = lambda identity: artifacts[identity]
    assert _completed_branch_current(store,canvas,child)
    original_plan = deepcopy(plan)
    original_manifest = deepcopy(manifest)
    original_source = (store.project_dir/'source.png').read_bytes()
    if canvas_change == 'replacement':
        Image.new('RGB',(64,64),'blue').save(store.project_dir/'replacement.png')
        source['data']['imageUrl'] = '/static/projects/p/replacement.png'
    elif canvas_change == 'disconnected':
        canvas['edges'].remove({'source':'source','target':'plan-node'})
    else:
        source['data']['workflowResultStale'] = True
    assert plan == original_plan and manifest == original_manifest
    assert (store.project_dir/'source.png').read_bytes() == original_source
    assert not _completed_branch_current(store,canvas,child)
