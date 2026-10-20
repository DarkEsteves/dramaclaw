"""Authenticated actual rendered-output review endpoints."""
from typing import Any
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from novelvideo.api.auth import get_api_user
from novelvideo.api.routes.reference_director import _context, _data
from novelvideo.api.routes.remix import _call
from novelvideo.freezone.remix.production_output_review import prepare_output_review, confirm_output_review
router = APIRouter(prefix='/projects/{project}/freezone/canvases/{canvas_id}/director')

class PrepareOutputRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    commission_ref: dict[str,Any]
    compose_node_id: str = Field(min_length=1,max_length=128)

class ConfirmOutputRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    review_ref: dict[str,Any]
    viewed_complete_media: bool

@router.post('/output-review')
async def prepare(project:str,canvas_id:str,body:PrepareOutputRequest,user:dict=Depends(get_api_user)):
    ctx,canvas,store = await _context(project,canvas_id,user)
    saved = await _call(prepare_output_review,store,ctx,canvas,body.commission_ref,body.compose_node_id)
    return _data({'review_ref':{'artifact_id':saved['id'],'revision':saved['revision']},**saved['payload']})

@router.post('/output-review/confirm')
async def confirm(project:str,canvas_id:str,body:ConfirmOutputRequest,user:dict=Depends(get_api_user)):
    if user.get('credential_kind') in {'agent_session','local_trusted_agent'}:
        raise HTTPException(403,'authenticated user playback confirmation required')
    ctx,canvas,store = await _context(project,canvas_id,user)
    return _data(await _call(confirm_output_review,store,ctx,canvas,body.review_ref,actor_kind='user',viewed_complete_media=body.viewed_complete_media))
