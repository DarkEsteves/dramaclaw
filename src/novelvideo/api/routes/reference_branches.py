"""Authenticated independent director branches and pending upload association."""
from typing import Any
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from novelvideo.api.auth import get_api_user
from novelvideo.api.routes.remix import _call
from novelvideo.freezone.remix.reference_branches import prepare_reference_branches, associate_material_upload

router = APIRouter(prefix='/projects/{project}/freezone/canvases/{canvas_id}/director')

class BranchBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    commission_ref: dict[str, Any]
    plan_refs: list[dict[str, Any]] = Field(min_length=1,max_length=100)
    command_id: str = Field(min_length=1,max_length=128)

class UploadBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    work_ref: dict[str, Any]
    step_id: str = Field(min_length=1,max_length=128)
    node_id: str = Field(min_length=1,max_length=160)

@router.post('/branches')
async def branches(project: str,canvas_id: str,body: BranchBody,user: dict=Depends(get_api_user)):
    from .reference_director import _context,_commission_in_scope,_plan_in_canvas,_belongs_to_analysis,_notify_commission
    ctx,canvas,store = await _context(project,canvas_id,user)
    parent = await _commission_in_scope(store,body.commission_ref,ctx,canvas_id)
    for ref in body.plan_refs:
        _,node = await _plan_in_canvas(store,ref,canvas)
        if not _belongs_to_analysis(canvas,parent['analysis_node_id'],node['id']):
            raise HTTPException(422,'branch plan must descend from this reference analysis')
        node_ref = node['data']['productionPlanV2Ref']
        if node_ref.get('revision') != ref['revision']:
            raise HTTPException(409,'canvas branch plan revision changed')
    result = await _call(prepare_reference_branches,store,body.commission_ref,body.plan_refs,body.command_id)
    await _notify_commission(ctx,canvas_id,result['commission'],'director-branches:'+body.command_id)
    return {'ok':True,'data':result}

@router.post('/material-upload')
async def material_upload(project: str,canvas_id: str,body: UploadBody,user: dict=Depends(get_api_user)):
    from .reference_material import _scope,_notify_material
    ctx,canvas,store,_ = await _scope(project,canvas_id,user,body.work_ref)
    result = await _call(associate_material_upload,store,body.work_ref,body.step_id,canvas,body.node_id)
    await _notify_material(ctx,canvas_id,store,result['work_ref'],'material-upload:'+body.work_ref['artifact_id']+':'+str(result['work_ref']['revision']))
    return {'ok':True,'data':result}
