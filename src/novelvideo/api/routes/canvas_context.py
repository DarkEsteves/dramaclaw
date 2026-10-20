"""Authenticated saved canvas reads; no canvas writes or browser receipts."""

from __future__ import annotations

import asyncio
from typing import Any, Literal
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from novelvideo.api.auth import require_scope
from novelvideo.api.routes import freezone
from novelvideo.freezone import canvas_store
from novelvideo.freezone.canvas_context_reader import (
    read_saved_context,
    saved_turn_binding,
)
from novelvideo.project_context import require_project_home_node

router = APIRouter(tags=["freezone-canvas"])


class ContextReadBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requests: list[dict[str, Any]] = Field(max_length=20)
    read_consistency: Literal["saved"] = "saved"
    expected_revision: str | int | None = None


@router.post("/projects/{project}/freezone/canvases/{canvas_id}/context/read")
async def read_canvas_context(
    project: str,
    canvas_id: str,
    body: ContextReadBody,
    request: Request,
    user: dict = Depends(require_scope("projects:read")),
):
    if not freezone.CANVAS_ID_RE.fullmatch(canvas_id):
        raise HTTPException(400, "invalid canvas_id")
    ctx, _, _, project_dir, _ = await freezone._resolve_freezone_project(
        project, user, required_role="viewer", require_home_node=True
    )
    require_project_home_node(ctx, operation="saved canvas context read")
    state_dir = freezone._canvas_state_project_dir(ctx, project_dir)
    binding = None
    if user.get("credential_kind") in {"agent_session", "local_trusted_agent"}:
        if user.get("credential_kind") == "agent_session" and (
            user.get("current_scope_kind") != "project"
            or user.get("current_project_id") != ctx.project_id
        ):
            raise HTTPException(403, "agent project scope mismatch")
        binding = await asyncio.to_thread(
            saved_turn_binding,
            state_dir,
            request.headers.get("X-DramaClaw-Turn-ID", ""),
        )
        if (
            not binding
            or (
                binding.get("requester_user_id"),
                binding.get("project_id"),
                binding.get("canvas_id"),
            )
            != (ctx.requester_user_id, ctx.project_id, canvas_id)
            or binding.get("agent_id") != request.headers.get("X-DramaClaw-Agent-ID")
        ):
            raise HTTPException(
                403,
                "saved canvas read requires a hosted turn for this actor, canvas and agent",
            )
    try:
        payload = await asyncio.to_thread(
            canvas_store.read_canvas, state_dir, canvas_id
        )
    except canvas_store.CanvasStoreError as exc:
        freezone._raise_canvas_store_http(exc)
    if (
        not payload
        or payload.get("deleted")
        or payload.get("project_id") != ctx.project_id
    ):
        raise HTTPException(404, "saved canvas not found")
    if (
        payload.get("access_model") in {"private", "user_private"}
        and payload.get("owner_principal_id") != ctx.requester_user_id
    ):
        raise HTTPException(403, "private canvas belongs to another actor")
    try:
        result = await asyncio.to_thread(
            read_saved_context,
            payload,
            project_id=ctx.project_id,
            canvas_id=canvas_id,
            requests=body.requests,
            expected_revision=body.expected_revision,
        )
    except ValueError as exc:
        if str(exc) == "revision_conflict":
            raise HTTPException(
                409,
                {
                    "code": "canvas_revision_conflict",
                    "canvas_revision": payload.get("revision"),
                },
            ) from exc
        raise HTTPException(400, str(exc)) from exc
    if binding:
        from novelvideo.chat.reference_turn_outcome import record_context_receipt
        await asyncio.to_thread(record_context_receipt, state_dir, binding, result)
    return {"ok": True, "data": result}


@router.get(
    "/projects/{project}/freezone/canvases/{canvas_id}/context/browser-presence"
)
async def read_canvas_browser_presence(
    project: str,
    canvas_id: str,
    request: Request,
    user: dict = Depends(require_scope("projects:read")),
):
    from novelvideo.api.routes.reference_director import _context
    from novelvideo.api.routes.chat import _candidate_canvas_bridge_dirs_for_scope
    from novelvideo.chat.store import ChatScope
    from novelvideo.freezone.canvas_command_bridge import browser_is_online

    ctx, _, _ = await _context(project, canvas_id, user, role="viewer")
    require_project_home_node(ctx, operation="browser presence read")
    agent_id = request.headers.get("X-DramaClaw-Agent-ID", "main")
    turn_id = request.headers.get("X-DramaClaw-Turn-ID", "")
    if user.get("credential_kind") in {"agent_session", "local_trusted_agent"}:
        hosted = await asyncio.to_thread(saved_turn_binding, ctx.state_dir, turn_id)
        if not hosted or (
            hosted.get("requester_user_id"),
            hosted.get("project_id"),
            hosted.get("canvas_id"),
            hosted.get("agent_id"),
        ) != (ctx.requester_user_id, ctx.project_id, canvas_id, agent_id):
            raise HTTPException(
                403,
                "browser presence requires exact hosted actor, canvas and agent turn",
            )
    try:
        scope = ChatScope.from_payload(
            {
                "kind": "project",
                "id": ctx.project_id,
                "surface": "freezone",
                "canvasId": canvas_id,
                "agentId": agent_id,
            }
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    from dataclasses import replace

    scope = replace(scope, state_dir=str(ctx.state_dir))
    binding = {
        "username": str(user["username"]),
        "project_id": ctx.project_id,
        "canvas_id": canvas_id,
        "agent_id": agent_id,
    }
    online = any(
        browser_is_online(binding=binding, bridge_dir=directory)
        for directory in _candidate_canvas_bridge_dirs_for_scope(
            str(user["username"]), scope
        )
    )
    return {
        "ok": True,
        "data": {
            "online": online,
            "project_id": ctx.project_id,
            "canvas_id": canvas_id,
            "agent_id": agent_id,
        },
    }
