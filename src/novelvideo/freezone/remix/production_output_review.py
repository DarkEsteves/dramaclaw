"""Review the encoded output of an exact completed native render."""
from __future__ import annotations
from copy import deepcopy
from fractions import Fraction
import hashlib
from typing import Any, Literal
from urllib.parse import quote
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProductionOutputReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    schema_version: Literal['production_output_review.v1'] = 'production_output_review.v1'
    commission_ref: dict[str, Any]
    manifest_ref: dict[str, Any]
    plan_ref: dict[str, Any]
    compose_node_id: str
    operation_id: str
    media_url: str
    media: dict[str, Any]
    technical: dict[str, Any]
    content: dict[str, Any]
    status: Literal['needs_review', 'passed', 'rejected']
    findings: list[str] = Field(default_factory=list)
    actor_kind: Literal['system', 'user'] = 'system'
    decision_receipt: dict[str, Any] | None = None

    @model_validator(mode='after')
    def passed_requires_receipt(self):
        if self.status == 'passed' and (self.actor_kind != 'user' or not self.decision_receipt
                or self.technical.get('status') != 'passed' or self.content.get('status') != 'passed'):
            raise ValueError('passed output review requires authenticated decision receipt')
        if self.status == 'passed':
            receipt = self.decision_receipt or {}
            if (not all(receipt.get(key) for key in ('project_id','canvas_id','requester_user_id','receipt_id'))
                    or receipt.get('viewed_complete_media') is not True
                    or receipt.get('media_hash') != self.media.get('media_hash')
                    or receipt.get('manifest_ref') != self.manifest_ref
                    or receipt.get('plan_ref') != self.plan_ref):
                raise ValueError('output review receipt must bind actual media and exact plan')
        return self


def _exact(ref):
    return {'artifact_id':ref['artifact_id'], 'revision':ref['revision']}


def _observe(store, ctx, canvas, commission_ref, compose_node_id):
    from .reference_commission import read_reference_commission
    from .operations import RemixOperationStore
    from novelvideo.api.routes.remix import _validate_v2_canvas_bindings
    from novelvideo.api.routes.remix_media import _inspect_existing_media
    commission = read_reference_commission(store, commission_ref, latest=True)
    if _exact(commission['commission_ref']) != _exact(commission_ref):
        raise ValueError('commission changed; reload current revision')
    if (commission['project_id'],commission['canvas_id'],commission['requester_user_id']) != (ctx.project_id, canvas['canvas_id'] if 'canvas_id' in canvas else canvas['id'],ctx.requester_user_id):
        raise PermissionError('output commission belongs to another canvas or user')
    if commission['phase'] != 'reviewing_output':
        raise ValueError('output review requires reviewing_output phase')
    plan_ref, manifest_ref = commission['plan_ref'],commission['manifest_ref']
    plan = store.read_latest(plan_ref['artifact_id'])
    manifest = store.read(manifest_ref['artifact_id'],manifest_ref['revision'])
    if plan['revision'] != plan_ref['revision'] or manifest['schema'] != 'composition_manifest.v2' or manifest['payload']['source_versions'] != [_exact(plan_ref)]:
        raise ValueError('output requires exact current plan and manifest')
    data = next((n.get('data',{}) for n in canvas.get('nodes',[]) if n.get('id') == compose_node_id),{})
    if data.get('productionPlanRenderedRef') != {'artifactId':plan_ref['artifact_id'],'revision':plan_ref['revision']}:
        raise ValueError('canvas output does not prove current rendered plan')
    operation_id = data.get('remixRenderOperationId')
    operations = RemixOperationStore(store.project_dir,store.project_id)
    operation = operations.get(operation_id)
    if (operation['status'] != 'completed' or operation.get('producer_version') != 'remix-render.v1'
            or operation.get('canvas_id') != commission['canvas_id'] or operation.get('node_id') != compose_node_id
            or [_exact(r) for r in operation['input_refs']] != [_exact(manifest_ref)]
            or operation.get('node_revision') != data.get('remixNodeRevision')
            or data.get('remixInputsCurrent') is not True
            or data.get('remixInputFingerprint') != operation.get('normalized_parameters',{}).get('input_fingerprint')
            or not operation.get('task_id')):
        raise ValueError('output requires completed native render on current canvas node')
    plan_data = next((n.get('data',{}) for n in canvas.get('nodes',[]) if n.get('id') == data.get('productionPlanNodeId')), {})
    if plan_data.get('productionPlanV2Dirty') is True or plan_data.get('productionPlanV2Conflict') is True:
        raise ValueError('canvas plan has pending changes')
    if plan_data.get('productionPlanV2Draft') is not None:
        from .contracts import validate_payload
        if validate_payload('production_plan.v2',plan_data['productionPlanV2Draft']) != plan['payload']:
            raise ValueError('canvas plan draft differs from rendered plan')
    _validate_v2_canvas_bindings(canvas,manifest['payload'],manifest_ref,plan['payload'],compose_node_id,store.project_id)
    if not operations.media_bindings_unchanged(manifest['payload']['media_bindings'],operation.get('input_media_hashes')):
        raise ValueError('render source media changed')
    output_refs = operation.get('output_refs',[])
    if len(output_refs) != 1 or not output_refs[0].get('project_path','').startswith('freezone/remix_renders/'):
        raise ValueError('native operation requires one project render output')
    url = f"/api/v1/projects/{quote(store.project_id,safe='')}/media/{quote(output_refs[0]['project_path'],safe='/')}"
    if data.get('resultVideoUrl') != url or data.get('remixRenderManifestRef') != {'artifactId':manifest_ref['artifact_id'],'revision':manifest_ref['revision']}:
        raise ValueError('canvas output differs from completed render')
    media = _inspect_existing_media(store.project_dir,output_refs[0]['project_path'])
    spec = manifest['payload']['output_spec']
    fps = Fraction(spec['fps_num'],spec['fps_den'])
    checks = media['media_kind'] == 'video' and (media.get('width_px'),media.get('height_px')) == (spec['width'],spec['height'])
    checks = checks and Fraction(media.get('fps_num',0),media.get('fps_den',1) or 1) == fps
    checks = checks and abs(media['duration_us'] - round(spec['duration_frames'] / fps * 1_000_000)) <= round(1_000_000 / fps)
    checks = checks and (not manifest['payload'].get('audio_events') or media['has_audio'])
    technical = {'status':'passed' if checks else 'rejected','checks':['encoded_dimensions','encoded_fps','encoded_duration','expected_audio_stream','current_native_render','current_source_hashes']}
    return commission, manifest_ref, plan_ref, operation_id, url, media, technical


def prepare_output_review(store, ctx, canvas, commission_ref, compose_node_id):
    commission,mref,pref,oid,url,media,technical = _observe(store,ctx,canvas,commission_ref,compose_node_id)
    payload = ProductionOutputReview(commission_ref=commission['commission_ref'],manifest_ref=mref,plan_ref=pref,
        compose_node_id=compose_node_id,operation_id=oid,media_url=url,media=media,technical=technical,
        content={'status':'unavailable','scope':'complete_encoded_playback','requires_user_review':True},
        status='needs_review' if technical['status']=='passed' else 'rejected',
        findings=['Play the complete rendered video and listen to its audio before confirming.']).model_dump(mode='json')
    return store.create('production_output_review.v1',payload,[commission_ref,pref,mref])


def confirm_output_review(store,ctx,canvas,review_ref,*,actor_kind='user',viewed_complete_media=False):
    from .reference_commission import advance_reference_commission
    if actor_kind != 'user' or viewed_complete_media is not True:
        raise PermissionError('authenticated user must confirm complete actual media playback')
    saved = store.read(review_ref['artifact_id'],review_ref['revision'])
    if saved['schema'] != 'production_output_review.v1' or store.read_latest(saved['id'])['revision'] != saved['revision']:
        raise ValueError('exact current output review required')
    payload = deepcopy(saved['payload'])
    commission,mref,pref,oid,url,media,technical = _observe(store,ctx,canvas,payload['commission_ref'],payload['compose_node_id'])
    if any(payload[key] != value for key,value in [('manifest_ref',mref),('plan_ref',pref),('operation_id',oid),('media_url',url),('media',media)]) or technical['status'] != 'passed':
        raise ValueError('output changed or technical review failed; prepare a new review')
    receipt = {'project_id':ctx.project_id,'canvas_id':commission['canvas_id'],'requester_user_id':ctx.requester_user_id,
               'media_hash':media['media_hash'],'manifest_ref':mref,'plan_ref':pref,'review_ref':review_ref,'viewed_complete_media':True}
    receipt['receipt_id'] = hashlib.sha256(store._canonical(receipt)).hexdigest()
    payload.update(status='passed',actor_kind='user',decision_receipt=receipt,content={'status':'passed','scope':'authenticated_complete_encoded_playback'},findings=[])
    revised = store.revise(saved['id'],saved['revision'],payload,saved['upstream_refs'])
    ref = {'artifact_id':revised['id'],'revision':revised['revision']}
    advanced = advance_reference_commission(store,commission['commission_ref'],{'type':'output_reviewed','review_ref':ref},commission['commission_ref']['revision'])
    return {'review_ref':ref,'review':revised['payload'],'commission':advanced}


def validate_production_output_review_lineage(store,payload,upstream_refs):
    for field,schema in [('commission_ref','reference_commission.v1'),('plan_ref','production_plan.v2'),('manifest_ref','composition_manifest.v2')]:
        ref = payload[field]
        artifact = store._read(ref['artifact_id'],ref['revision'])
        if artifact['schema'] != schema or not any(_exact(r)==_exact(ref) for r in upstream_refs):
            raise ValueError('output review lineage mismatch')
    manifest = store._read(payload['manifest_ref']['artifact_id'],payload['manifest_ref']['revision'])
    if manifest['payload']['source_versions'] != [_exact(payload['plan_ref'])]:
        raise ValueError('output review manifest source mismatch')
