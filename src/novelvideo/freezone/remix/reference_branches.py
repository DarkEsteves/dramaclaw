"""Independent plan branches and host association of real uploaded media."""
from __future__ import annotations
from copy import deepcopy
import hashlib
from .reference_commission import ReferenceCommission, _result
from .store import RemixArtifactConflict


def validate_reference_branch_lineage(store, payload, refs):
    for row in payload.get('plan_branches',[]):
        ref = store._reference_list([row.get('commission_ref')],store.project_id)[0]
        if ref not in refs:
            raise ValueError('branch group requires exact child dependency')
        child = store._read(ref['artifact_id'],ref['revision'])
        if child['schema'] != 'reference_commission.v1' or not child['payload'].get('parent_commission_ref'):
            raise ValueError('branch group requires actual child commission')
        data = child['payload']
        if data.get('plan_ref') != row.get('plan_ref'):
            raise ValueError('branch group plan differs from child')
        for key in ('project_id','canvas_id','requester_user_id','analysis_node_id','analysis_evidence','goal','production_scope'):
            if data[key] != payload[key]:
                raise ValueError('branch group child scope differs')
    parent_ref = payload.get('parent_commission_ref')
    if not parent_ref:
        return
    normalized = store._reference_list([parent_ref],store.project_id)[0]
    if normalized not in refs:
        raise ValueError('branch requires exact parent dependency')
    parent = store._read(normalized['artifact_id'],normalized['revision'])
    if parent['schema'] != 'reference_commission.v1':
        raise ValueError('branch parent must be a commission')
    source = parent['payload']
    for key in ('project_id','canvas_id','requester_user_id','analysis_node_id','analysis_evidence','goal','production_scope'):
        if payload[key] != source[key]:
            raise ValueError('branch scope differs; confirm a new direction')
    inherited = [r for r in source['decisions'] if r.get('type') == 'direction_confirmed' and r.get('direction_revision') == source['direction_revision']]
    if not inherited or any(r not in payload['decisions'] for r in inherited):
        raise ValueError('branch requires original authenticated direction receipt')
    if payload['direction_revision'] == source['direction_revision'] and payload['direction'] != source['direction']:
        raise ValueError('branch inherited direction differs from parent')


def prepare_reference_branches(store, commission_ref, plan_refs, command_id):
    if not isinstance(command_id,str) or not command_id.strip() or len(command_id)>128:
        raise ValueError('branch command_id required')
    if not plan_refs or len(plan_refs)>100:
        raise ValueError('one to one hundred independent plans required')
    parent_ref = store._reference_list([commission_ref],store.project_id)[0]
    refs = store._reference_list(plan_refs,store.project_id)
    if len({r['artifact_id'] for r in refs}) != len(refs):
        raise ValueError('branch plans must be distinct')
    with store._locked() as guard:
        latest = store._read(parent_ref['artifact_id'],store._latest_revision(parent_ref['artifact_id']))
        if latest['schema'] != 'reference_commission.v1':
            raise ValueError('parent must be a commission')
        old = [row for row in latest['payload'].get('plan_branches',[]) if row['command_id']==command_id]
        if old:
            if [r['plan_ref'] for r in old] != refs or any(r['parent_revision'] != parent_ref['revision'] for r in old):
                raise ValueError('branch command_id reused for different plans')
            parent = store._read(latest['id'],old[0]['result_parent_revision'])
            return {'commission':_result(parent),'branches':[_result(store._read(r['commission_ref']['artifact_id'],r['commission_ref']['revision'])) for r in old]}
        if latest['revision'] != parent_ref['revision']:
            raise RemixArtifactConflict('commission changed; reload before branching')
        source = ReferenceCommission.model_validate(latest['payload']).model_dump(mode='json')
        if not source['direction'] or not source['direction_revision']:
            raise ValueError('confirmed direction required before branches')
        plans = [store._read(r['artifact_id'],r['revision']) for r in refs]
        for ref,plan in zip(refs,plans):
            if store._latest_revision(plan['id']) != ref['revision']:
                raise RemixArtifactConflict('branch plan changed; reload current revision')
            expected_goal = (source.get('direction') or {}).get('target_goal') or source['goal']
            if plan['schema'] != 'production_plan.v2' or plan['payload']['goal'] != expected_goal:
                raise ValueError('branch goal differs; confirm a new direction')
        children,rows = [],[]
        for ref in refs:
            identity = hashlib.sha256((latest['id']+':branch:'+command_id+':'+ref['artifact_id']).encode()).hexdigest()[:32]
            child = deepcopy(source)
            child.update(parent_commission_ref=parent_ref,plan_branches=[],phase='awaiting_plan_review',plan_ref=ref,review_ref=None,work_ref=None,manifest_ref=None,output_review_ref=None,
                         decisions=[r for r in source['decisions'] if r.get('type')=='direction_confirmed' and r.get('direction_revision')==source['direction_revision']])
            child = ReferenceCommission.model_validate(child).model_dump(mode='json')
            dependencies = [parent_ref,ref]
            validate_reference_branch_lineage(store,child,dependencies)
            try:
                existing = store._read(identity,1)
            except FileNotFoundError:
                existing = None
            if existing and existing['payload'] != {k:v for k,v in child.items() if v is not None}:
                raise RemixArtifactConflict('branch identity already used for different content')
            if existing is None:
                from .contracts import validate_payload
                child = validate_payload('reference_commission.v1',child)
                store._publish(artifact_id=identity,schema='reference_commission.v1',revision=1,payload=child,upstream_refs=dependencies,created_by=store.created_by,guard=guard)
            children.append(_result(store._read(identity,1)))
            rows.append({'command_id':command_id,'parent_revision':parent_ref['revision'],'result_parent_revision':latest['revision']+1,'plan_ref':ref,'commission_ref':{'artifact_id':identity,'revision':1,'project_id':store.project_id}})
        payload = {**source,'plan_branches':[*source.get('plan_branches',[]),*rows], 'phase':'awaiting_plan_review','plan_ref':None,'review_ref':None,'work_ref':None,'manifest_ref':None,'output_review_ref':None}
        dependencies = latest['upstream_refs']+[r['commission_ref'] for r in rows]
        store._publish(artifact_id=latest['id'],schema='reference_commission.v1',revision=latest['revision']+1,payload=payload,upstream_refs=dependencies,created_by=store.created_by,guard=guard)
        return {'commission':_result(store._read(latest['id'],latest['revision']+1)),'branches':children}


def associate_material_upload(store,work_ref,step_id,canvas,node_id):
    from .material_work import _exact_ref, ready_material_steps
    from .material_execution import canvas_material_resource
    _exact_ref(work_ref)
    saved = store.read_latest(work_ref['artifact_id'])
    if saved['schema'] != 'material_work_package.v1':
        raise ValueError('material work package required')
    if saved['revision'] != work_ref['revision']:
        raise RemixArtifactConflict('material work changed; reload current revision')
    payload = deepcopy(saved['payload'])
    step = next((s for s in payload['steps'] if s['id']==step_id),None)
    if not step or step['source'] != 'upload':
        raise ValueError('pending upload step required')
    resource = canvas_material_resource(store,canvas,node_id,step['media_kind'])
    if step['status']=='candidate' and step.get('node_id')==node_id and step.get('candidates')==[resource]:
        return {'work_ref':work_ref,'work':payload}
    if step_id not in ready_material_steps(payload):
        raise ValueError('upload step is not ready')
    step.update(node_id=node_id,status='candidate',candidates=[resource])
    result = store.revise(saved['id'],saved['revision'],payload,saved['upstream_refs'])
    return {'work_ref':{'artifact_id':result['id'],'revision':result['revision']},'work':result['payload']}
