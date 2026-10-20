"""WebSocket chat endpoint for the React frontend.

Transport contract is typed JSON events. The backend keeps chat storage and
agent process management behind this endpoint so dramaclaw-fe does not need to
know whether the active backend is Hermes, Claude, or Codex.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import sqlite3
import time
import uuid
from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import AliasChoices, BaseModel, Field

from novelvideo.api.auth import (
    AGENT_WRITE_SCOPES,
    AUTH_COOKIE_NAME,
    UNSUPPORTED_QUERY_CREDENTIALS,
    _verify_agent_bearer,
    _verify_browser_session,
    get_api_user,
)
from novelvideo.api.deps import list_user_projects
from novelvideo.api.egress_binding import request_egress_scope
from novelvideo.chat import service as chat_service
from novelvideo.chat.execution_context import AgentExecutionContext
from novelvideo.chat.hermes_egress import EgressBoundaryError
from novelvideo.chat.hermes_fork_requirement import HermesForkMissing
from novelvideo.chat.hermes_pool import canvas_bridge_dir_for_profile
from novelvideo.chat.director_auto import coordinator as director_auto_coordinator
from novelvideo.chat.canvas_tool_session import dispatch_pending_bridge_messages, validate_bridge_receipt
from novelvideo.chat.live_events import (
    register_chat_websocket,
    unregister_chat_websocket,
)
from novelvideo.chat.hermes_workspace import (
    ensure_user_hermes_workspace,
    sync_freezone_hermes_workflow_skills,
)
from novelvideo.chat.store import ChatScope, chat_store
from novelvideo.freezone.canvas_command_bridge import (
    BRIDGE_PENDING_TTL_SECONDS,
    canonical_bridge_key,
    bridge_message_exists,
    list_pending_bridge_messages,
    mark_bridge_message_delivered,
    read_bridge_message,
    resolve_clarification_result,
    resolve_canvas_command,
    resolve_canvas_context,
    resolve_skill_studio_result,
)
from novelvideo.freezone.agent_config_store import save_user_agent_config_item
from novelvideo.freezone.agent_product_operations import AGENT_PRODUCT_PRICE_REFERENCE
from novelvideo.ports import get_product_surface_access, get_usage_meter
from novelvideo.ports.local.usage import NoOpUsageMeter
from novelvideo.project_context import (
    ProjectContext,
    require_project_home_node,
    resolve_project_context,
    user_id_from_api_user,
)
from novelvideo.shared.billing_errors import (
    BILLING_RULE_NOT_CONFIGURED_MESSAGE,
    INSUFFICIENT_CREDITS_MESSAGE,
    billing_error_payload,
    billing_rule_not_configured_payload,
    find_billing_error,
    find_billing_rule_not_configured_error,
    find_insufficient_credits_error,
    insufficient_credits_payload,
)
from novelvideo.utils.error_redaction import safe_exception_message

router = APIRouter()
logger = logging.getLogger(__name__)

AI_ASSISTANT_CHAT_FEATURE_KEY = "assistant.chat"
EMPTY_AGENT_REPLY_MESSAGE = "这轮操作没有收到虾导的有效回复，请稍后重试。"

# 出网台账里这条链路的 capability 口径。取自
# `chat/hermes_egress.py:131` 的 `capability="agent.hermes.text"`，与 EG-07 对齐；
# 绑定侧与账本侧必须是同一个字符串，不另取。
HERMES_TEXT_EGRESS_TASK_TYPE = "agent.hermes.text"

_REASONING_REQUIRED_ERROR_MESSAGE = (
    "模型请求失败：当前上游模型要求启用推理，但模型网关仍将本次请求识别为关闭推理。"
    "请检查 NewAPI 的模型映射和推理参数配置后重试。"
)


def _user_facing_chat_error(exc: BaseException) -> str:
    """Turn provider/runtime failures into stable, safe chat copy."""

    if isinstance(exc, HermesForkMissing):
        return (
            "Hermes 运行环境不匹配：当前安装版本无法支持虾导运行。"
            "请安装 DramaClaw 所需的 Hermes 配套版本后重试。"
        )

    if isinstance(exc, sqlite3.Error):
        return "聊天存储读写失败，请检查磁盘空间和数据库状态后重试。"

    raw = safe_exception_message(exc).strip()
    lowered = raw.lower()
    if "reasoning is mandatory" in lowered and "cannot be disabled" in lowered:
        return _REASONING_REQUIRED_ERROR_MESSAGE
    if "timed out" in lowered or "timeout" in lowered or "响应超时" in raw:
        return "模型响应超时：上游服务未在规定时间内返回结果，请稍后重试。"
    if "connection refused" in lowered or "connection error" in lowered:
        return "模型连接失败：当前无法连接上游模型服务，请检查服务状态后重试。"

    # The websocket used to expose this text directly in a transient red banner.
    # Keep the useful cause, but remove local paths/secrets and cap provider dumps.
    safe = chat_service._redact_local_filesystem_paths(raw).strip()  # type: ignore[attr-defined]
    if not safe:
        return "本轮处理失败：服务未返回可识别的错误原因，请稍后重试。"
    if len(safe) > 800:
        safe = f"{safe[:800].rstrip()}…"
    return safe


class ChatScopePayload(BaseModel):
    kind: str = "home"
    id: str | None = None
    surface: str | None = None
    canvasId: str | None = None
    canvas_id: str | None = None
    agentId: str | None = None
    agent_id: str | None = None


class CancelChatTurnRequest(BaseModel):
    scope: ChatScopePayload | None = None
    turn_id: str | None = None


@router.post("/chat/cancel")
async def cancel_chat_turn(
    user: dict = Depends(get_api_user),
    payload: CancelChatTurnRequest | None = None,
) -> dict[str, Any]:
    """Best-effort cancellation for the active agent turn.

    The WebSocket receive loop is blocked while a Hermes prompt is streaming,
    so a separate HTTP endpoint gives the frontend an out-of-band stop signal.
    Hermes closes its user worker; Codex interrupts the active App Server turn
    without stopping the shared home-node runtime.
    """
    username = str(user["username"])
    backend_name: str | None = None
    safe_to_recover_home_lock = False
    try:
        backend_name = chat_service.get_chat_backend_name()
    except Exception:
        return {"ok": True, "data": {"cancelled": False}}
    codex_target: tuple[str, str] | None = None
    production_stop = None
    if backend_name == "codex":
        if payload is None or payload.scope is None:
            raise HTTPException(status_code=400, detail="scope is required")
        business_turn_id = str(payload.turn_id or "").strip()
        if not business_turn_id:
            raise HTTPException(status_code=400, detail="turn_id is required")
        try:
            scope = _scope_from_model(payload.scope)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _enforce_agent_chat_scope(user, scope, require_write=True)
        if scope.kind in {"project", "freezone"}:
            project_ctx = await _project_context_for_scope(user, scope)
            if (isinstance(project_ctx, ProjectContext) and _is_freezone_scope(scope) and scope.canvas_id
                    and project_ctx.effective_role in {'owner', 'admin', 'editor'}):
                from novelvideo.chat.reference_remix_continuation import coordinator as reference_coordinator
                async def production_stop():
                    # service resolves the exact live business turn before this
                    # callback. Persist the scoped stop before runtime interruption.
                    await reference_coordinator.stop_automation_scope(ctx=project_ctx,
                        canvas_id=scope.canvas_id, agent_id=scope.agent_id or 'main')
        agent_profile = (
            _freezone_agent_profile(scope) if _is_freezone_scope(scope) else "main"
        )
        codex_target = (
            chat_service._codex_scope_key(
                str(scope.id or ""),
                agent_profile=agent_profile,
                canvas_id=scope.canvas_id,
            ),
            business_turn_id,
        )
    try:
        if backend_name == "codex":
            assert codex_target is not None
            if production_stop is not None:
                cancelled = await chat_service.interrupt_active_codex_turn(
                    username, codex_target[0], codex_target[1], before_interrupt=production_stop
                )
            else:
                cancelled = await chat_service.interrupt_active_codex_turn(
                    username, codex_target[0], codex_target[1]
                )
        else:
            from novelvideo.chat.hermes_pool import pool as hermes_pool

            cancelled = await hermes_pool.close_user(username)
        safe_to_recover_home_lock = backend_name == "hermes" and not bool(cancelled)
    except Exception:
        cancelled = False
        # Preserve staging's Hermes recovery behavior when close_user itself
        # fails. Codex interrupt failures leave the active-turn state unknown,
        # so releasing its lock here could race a turn that is still settling.
        safe_to_recover_home_lock = backend_name == "hermes"
    if safe_to_recover_home_lock:
        try:
            # No backend turn remains to own the lock. Preserve staging's
            # explicit recovery path for a stranded Home lock without racing
            # an interrupt that is still settling in its stream finally block.
            chat_service.force_release_chat_run_lock(username, "")
        except Exception:
            pass
    return {"ok": True, "data": {"cancelled": cancelled}}


class ClearChatRequest(BaseModel):
    scope: dict[str, Any]


@router.post("/chat/clear")
async def clear_chat_scope(
    body: ClearChatRequest,
    user: dict = Depends(get_api_user),
) -> dict[str, Any]:
    """Clear one conversation and start a fresh Codex context for that scope."""
    try:
        scope = ChatScope.from_payload(body.scope)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if scope.kind not in {"home", "project", "freezone"}:
        raise HTTPException(400, "unsupported chat scope for clear")
    if chat_service.get_chat_backend_name() != "codex":
        raise HTTPException(409, "清空上下文当前仅支持 Codex Agent")

    username = str(user["username"])
    project_ctx = (
        await resolve_project_context(
            user=user, project_id=str(scope.id), required_role="editor"
        )
        if scope.kind in {"project", "freezone"} and scope.id
        else None
    )
    project = str(scope.id or "")
    lock_project = _chat_run_lock_project_for_scope(scope)
    try:
        lock_id = chat_service._acquire_chat_run_lock(username, lock_project)
    except RuntimeError as exc:
        raise HTTPException(
            409, "当前对话仍在进行，请等待完成或先停止后再清空"
        ) from exc

    heartbeat_task = asyncio.create_task(
        chat_service._chat_run_lock_heartbeat_loop(username, lock_project, lock_id)
    )
    try:
        project_state_dir = project_ctx.state_dir if project_ctx is not None else None
        execution_context = (
            AgentExecutionContext.from_project_scope(scope=scope, project=project_ctx)
            if project_ctx is not None
            else None
        )
        archived_threads = await chat_service.archive_codex_scope_thread(
            username,
            project,
            agent_profile=(
                execution_context.agent_profile if execution_context else "main"
            ),
            canvas_id=execution_context.canvas_id if execution_context else None,
            project_state_dir=project_state_dir,
        )
        if not chat_service._heartbeat_chat_run_lock(username, lock_project, lock_id):
            raise HTTPException(409, "清空操作已失去对话锁，请刷新后重试")
        storage_scope = _chat_store_scope_for_project_context(scope, project_ctx)
        if project_ctx is not None and not _is_freezone_scope(scope):
            storage_scope = replace(storage_scope, state_dir=str(project_state_dir))
        cleared = await chat_store.clear_messages_async(username, storage_scope)
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        chat_service._release_chat_run_lock(username, lock_project, lock_id)
    logger.info(
        "chat scope cleared user=%s kind=%s project=%s surface=%s canvas=%s agent=%s "
        "messages=%d archived_threads=%d",
        username,
        scope.kind,
        project or "<home>",
        scope.surface or "-",
        scope.canvas_id or "-",
        scope.agent_id or "-",
        cleared,
        archived_threads,
    )
    return {
        "ok": True,
        "data": {
            "cleared_messages": cleared,
            "archived_threads": archived_threads,
        },
    }


class ChatAttachmentIn(BaseModel):
    id: str | None = None
    type: str | None = None
    kind: str | None = None
    mimeType: str | None = None
    fileName: str | None = None
    fileSize: int | None = None
    content: str | None = None
    url: str | None = None
    path: str | None = None
    label: str | None = None


class ChatMessageIn(BaseModel):
    type: str
    scope: ChatScopePayload | None = None
    text: str
    user_text: str | None = None
    turn_id: str | None = None
    attachments: list[ChatAttachmentIn] = []
    surface: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class ScopeSetIn(BaseModel):
    type: str
    scope: ChatScopePayload


class ChatUiEventIn(BaseModel):
    scope: ChatScopePayload
    turn_id: str
    event: dict[str, Any]


class AgentPermissionResultIn(BaseModel):
    scope: ChatScopePayload | None = None
    request_id: str | int
    option_id: str


class FreezoneCanvasAgentsIn(BaseModel):
    project_id: str
    canvas_id: str


class PendingCanvasCommandsIn(BaseModel):
    project_id: str
    canvas_id: str
    agent_id: str | None = Field(
        default=None, validation_alias=AliasChoices("agent_id", "agentId")
    )
    agent_ids: list[str] = Field(
        default_factory=list, validation_alias=AliasChoices("agent_ids", "agentIds")
    )
    seen_keys: list[str] = []


class PendingInteractionsIn(BaseModel):
    project_id: str = Field(min_length=1, max_length=128)
    canvas_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    agent_id: str = Field(default="main", pattern=r"^[A-Za-z0-9_-]+$")
    browser_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    cursor: int = Field(default=0, ge=0)
    event_cursor: str = Field(default="", max_length=128)


class CanvasCommandToolResultIn(BaseModel):
    turn_id: str | None = None
    bridge_key: str
    followup: bool = False
    project_id: str | None = None
    canvas_id: str | None = None
    agent_id: str | None = Field(
        default=None, validation_alias=AliasChoices("agent_id", "agentId")
    )
    tool_call_status: str = "completed"
    canvas_apply_status: str
    background_execution_status: str | None = None
    applied: bool = False
    cancelled: bool = False
    errors: list[str] = []
    applied_count: int = 0
    opened_ui_actions: int = 0
    created_node_ids: list[str] = []
    command_results: list[dict[str, Any]] = []
    message: str | None = None
    user_message: str | None = None
    agent_hint: str | None = None
    workflow_run_id: str | None = None
    operation_id: str | None = None
    task_id: str | None = None
    node_id: str | None = None
    action: str | None = None
    completion_owner: str | None = None


class CanvasContextToolResultIn(BaseModel):
    turn_id: str | None = None
    anchor_text_prefix: str | None = None
    bridge_key: str
    project_id: str | None = None
    canvas_id: str | None = None
    agent_id: str | None = Field(
        default=None, validation_alias=AliasChoices("agent_id", "agentId")
    )
    tool_call_status: str = "completed"
    canvas_context_status: str | None = None
    ok: bool = True
    responses: list[dict[str, Any]] = []
    errors: list[str] = []
    message: str | None = None


class SkillStudioToolResultIn(BaseModel):
    turn_id: str | None = None
    bridge_key: str
    project_id: str | None = None
    canvas_id: str | None = None
    agent_id: str | None = Field(
        default=None, validation_alias=AliasChoices("agent_id", "agentId")
    )
    tool_call_status: str = "completed"
    skill_studio_status: str = "answered"
    ok: bool = True
    action: str = "submit"
    selections: dict[str, Any] = Field(default_factory=dict)
    draft: dict[str, Any] | None = None
    draft_ref: dict[str, Any] | None = None
    saved_to_catalog: bool = False
    saved_skill_ids: list[str] = Field(default_factory=list)
    saved_recipe_ids: list[str] = Field(default_factory=list)
    errors: list[str] = []
    message: str | None = None
    agent_instruction: str | None = None
    client_debug: dict[str, Any] = Field(default_factory=dict)


class ClarificationToolResultIn(BaseModel):
    turn_id: str | None = None
    anchor_text_prefix: str | None = None
    bridge_key: str
    project_id: str | None = None
    canvas_id: str | None = None
    agent_id: str | None = Field(
        default=None, validation_alias=AliasChoices("agent_id", "agentId")
    )
    tool_call_status: str = "completed"
    clarification_status: str = "answered"
    ok: bool = True
    action: str = "submit"
    answers: dict[str, Any] = Field(default_factory=dict)
    skipped: bool = False
    used_recommended: bool = False
    errors: list[str] = []
    message: str | None = None


class ChatNotificationIn(BaseModel):
    scope: ChatScopePayload | None = None
    text: str


class DirectorAutoStartIn(BaseModel):
    episode: int = Field(default=1, ge=1)
    voice_policy: str | None = Field(default=None, pattern="^(system|custom)$")


class DirectorAutoSuspendIn(BaseModel):
    reason: str = Field(default="等待用户确认是否修改", max_length=500)


def _director_auto_payload(run: Any | None) -> dict[str, Any]:
    if run is None:
        return {"status": "manual", "episode": None, "run_id": None}
    return {
        "status": run.status,
        "episode": run.episode,
        "run_id": run.run_id,
        "activated_at": run.activated_at,
        "updated_at": run.updated_at,
        "last_error": run.last_error or None,
        "voice_policy": run.voice_policy or None,
    }


@router.get("/projects/{project}/chat/director-auto")
async def get_director_auto_run(
    project: str,
    user: dict = Depends(get_api_user),
) -> dict[str, Any]:
    ctx = await resolve_project_context(
        user=user, project_id=project, required_role="viewer"
    )
    run = await director_auto_coordinator.get(
        username=str(user["username"]),
        project_id=ctx.project_id,
    )
    return {"ok": True, "data": _director_auto_payload(run)}


@router.post("/projects/{project}/chat/director-auto/start")
async def start_director_auto_run(
    project: str,
    payload: DirectorAutoStartIn,
    user: dict = Depends(get_api_user),
) -> dict[str, Any]:
    ctx = await resolve_project_context(
        user=user, project_id=project, required_role="editor"
    )
    run = await director_auto_coordinator.start(
        username=str(user["username"]),
        ctx=ctx,
        episode=payload.episode,
        voice_policy=payload.voice_policy or "",
    )
    return {"ok": True, "data": _director_auto_payload(run)}


@router.post("/projects/{project}/chat/director-auto/pause")
async def pause_director_auto_run(
    project: str,
    user: dict = Depends(get_api_user),
) -> dict[str, Any]:
    ctx = await resolve_project_context(
        user=user, project_id=project, required_role="editor"
    )
    run = await director_auto_coordinator.pause(
        username=str(user["username"]),
        project_id=ctx.project_id,
        reason="用户切换为手动模式",
    )
    return {"ok": True, "data": _director_auto_payload(run)}


@router.post("/projects/{project}/chat/director-auto/suspend")
async def suspend_director_auto_run(
    project: str,
    payload: DirectorAutoSuspendIn,
    user: dict = Depends(get_api_user),
) -> dict[str, Any]:
    ctx = await resolve_project_context(
        user=user, project_id=project, required_role="editor"
    )
    try:
        run = await director_auto_coordinator.suspend_for_confirmation(
            username=str(user["username"]),
            project_id=ctx.project_id,
            reason=payload.reason.strip() or "等待用户确认是否修改",
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "data": _director_auto_payload(run)}


@router.post("/projects/{project}/chat/director-auto/resume")
async def resume_director_auto_run(
    project: str,
    user: dict = Depends(get_api_user),
) -> dict[str, Any]:
    ctx = await resolve_project_context(
        user=user, project_id=project, required_role="editor"
    )
    try:
        run = await director_auto_coordinator.resume_suspended(
            username=str(user["username"]),
            project_id=ctx.project_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "data": _director_auto_payload(run)}


@router.post("/chat/notifications")
async def append_chat_notification(
    payload: ChatNotificationIn,
    user: dict = Depends(get_api_user),
) -> dict[str, Any]:
    username = str(user["username"])
    scope = _scope_from_model(payload.scope)
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    if len(text) > 4000:
        raise HTTPException(status_code=400, detail="text is too long")

    if _is_freezone_scope(scope):
        project_ctx = await _project_context_for_scope(user, scope)
        message = await chat_store.append_message_async(
            username,
            _chat_store_scope_for_project_context(scope, project_ctx),
            "assistant",
            text,
        )
    elif scope.kind == "project":
        project_ctx = await _project_context_for_scope(user, scope)
        if not scope.id:
            raise HTTPException(status_code=400, detail="project scope id is required")
        message = await asyncio.to_thread(
            chat_service.add_assistant_message,
            username,
            str(scope.id),
            text,
            project_dir=project_ctx.output_dir if project_ctx is not None else None,
            project_state_dir=(
                project_ctx.state_dir if project_ctx is not None else None
            ),
        )
    else:
        message = await chat_store.append_message_async(
            username, scope, "assistant", text
        )
    return {"ok": True, "data": message}


@router.post("/chat/ui-events")
async def append_chat_ui_event(
    payload: ChatUiEventIn,
    user: dict = Depends(get_api_user),
) -> dict[str, Any]:
    username = str(user["username"])
    scope = _scope_from_model(payload.scope)
    project_ctx = None
    if scope.kind == "project":
        project_ctx = await _project_context_for_scope(user, scope)
    turn_id = payload.turn_id.strip()
    if not turn_id:
        raise HTTPException(status_code=400, detail="turn_id is required")
    try:
        event = await chat_store.append_ui_event_async(
            username,
            _chat_store_scope_for_project_context(scope, project_ctx),
            turn_id,
            payload.event,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "data": event}


@router.post("/chat/permission-result")
async def resolve_agent_permission(
    payload: AgentPermissionResultIn,
    user: dict = Depends(get_api_user),
) -> dict[str, Any]:
    scope = _scope_from_model(payload.scope)
    if scope.kind in {"project", "freezone"}:
        await _project_context_for_scope(user, scope)
    option_id = payload.option_id.strip()
    if not option_id:
        raise HTTPException(status_code=400, detail="option_id is required")
    from novelvideo.chat.hermes_pool import pool as hermes_pool

    agent_profile = (
        _freezone_agent_profile(scope) if _is_freezone_scope(scope) else "main"
    )
    resolved = await hermes_pool.resolve_permission(
        str(user["username"]),
        agent_profile,
        payload.request_id,
        option_id,
    )
    if not resolved:
        raise HTTPException(
            status_code=404, detail="permission request is no longer pending"
        )
    return {"ok": True, "data": {"resolved": True}}


@router.post("/chat/freezone-canvas-agents")
async def list_freezone_canvas_agents(
    payload: FreezoneCanvasAgentsIn,
    user: dict = Depends(get_api_user),
) -> dict[str, Any]:
    project_id = payload.project_id.strip()
    canvas_id = payload.canvas_id.strip()
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id is required")
    if not canvas_id:
        raise HTTPException(status_code=400, detail="canvas_id is required")
    scope = ChatScope(
        kind="project",
        id=project_id,
        surface="freezone",
        canvas_id=canvas_id,
        agent_id="main",
    )
    project_ctx = await _project_context_for_scope(user, scope)
    agents = await chat_store.list_freezone_canvas_agent_summaries_async(
        str(user["username"]),
        project_id=project_ctx.project_name if project_ctx is not None else project_id,
        canvas_id=canvas_id,
        project_state_dir=project_ctx.state_dir if project_ctx is not None else None,
    )
    return {"ok": True, "data": {"agents": agents}}


async def _bridge_project_state_dir(user: dict, payload: Any) -> Any:
    project_id = str(getattr(payload, "project_id", None) or "").strip()
    if not project_id:
        return None
    ctx = await resolve_project_context(
        user=user, project_id=project_id, required_role="viewer"
    )
    require_project_home_node(ctx)
    return ctx.state_dir


def _canvas_bridge_dir(
    username: str, *, profile: str = "director", project_state_dir: Any = None
) -> Any:
    workspace_profile = "freezone" if profile.startswith("freezone") else "director"
    home = ensure_user_hermes_workspace(
        username,
        profile=workspace_profile,
        **(
            {"project_state_dir": project_state_dir}
            if project_state_dir is not None
            else {}
        ),
    )
    return canvas_bridge_dir_for_profile(home, profile)


def _is_freezone_scope(scope: ChatScope) -> bool:
    return scope.kind == "freezone" or (
        scope.kind == "project" and scope.surface == "freezone"
    )


def _chat_store_scope_for_project_context(
    scope: ChatScope,
    project_ctx: ProjectContext | None,
) -> ChatScope:
    if not _is_freezone_scope(scope) or project_ctx is None:
        return scope
    return replace(
        scope,
        id=project_ctx.project_name,
        state_dir=(
            str(project_ctx.state_dir)
            if getattr(project_ctx, "state_dir", None) is not None
            else None
        ),
    )


async def _persist_chat_turn_error(
    *,
    user: dict[str, Any],
    username: str,
    scope: ChatScope,
    turn_id: str,
    reason: str,
) -> dict[str, Any] | None:
    """Persist a terminal turn error so history reconciliation cannot erase it."""

    content = f"本轮处理失败：{reason}\n\n请根据错误提示处理后重试。"
    try:
        project_ctx = (
            await _project_context_for_scope(user, scope)
            if scope.kind in {"project", "freezone"}
            else None
        )
        storage_scope = _chat_store_scope_for_project_context(scope, project_ctx)
        if (
            scope.kind == "project"
            and not _is_freezone_scope(scope)
            and project_ctx is not None
        ):
            storage_scope = replace(
                scope,
                id=project_ctx.project_name,
                state_dir=str(project_ctx.state_dir),
            )

        existing_messages = await chat_store.list_messages_async(
            username, storage_scope
        )
        for existing in reversed(existing_messages):
            if (
                str(existing.get("turn_id") or "") == turn_id
                and existing.get("chat_error") is True
            ):
                return existing
        return await chat_store.append_message_async(
            username,
            storage_scope,
            "assistant",
            content,
            turn_id=turn_id,
            metadata={"chat_error": True},
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "failed to persist chat turn error username=%s turn_id=%s scope=%s",
            username,
            turn_id,
            scope.to_dict(),
        )
        return None


def _canvas_bridge_profile_for_scope(scope: ChatScope) -> str:
    if _is_freezone_scope(scope):
        return f"freezone:{scope.agent_id or 'main'}"
    return "director"


def _candidate_canvas_bridge_dirs_for_scope(
    username: str, scope: ChatScope
) -> list[Any]:
    if not _is_freezone_scope(scope):
        return [
            _canvas_bridge_dir(
                username, profile="director", project_state_dir=scope.state_dir
            )
        ]
    dirs = [
        _canvas_bridge_dir(
            username,
            profile=_canvas_bridge_profile_for_scope(scope),
            project_state_dir=scope.state_dir,
        ),
        _canvas_bridge_dir(
            username, profile="freezone", project_state_dir=scope.state_dir
        ),
    ]
    unique: list[Any] = []
    seen: set[str] = set()
    for path in dirs:
        marker = str(path)
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(path)
    return unique


def _freezone_agent_profile(scope: ChatScope) -> str:
    return f"freezone:{scope.agent_id or 'main'}"


def _chat_run_lock_project_for_scope(scope: ChatScope) -> str:
    if _is_freezone_scope(scope) and scope.id:
        return chat_service._chat_run_lock_project_for_turn(
            str(scope.id),
            tool_mode="freezone_canvas",
            store_scope=scope,
        )
    return str(scope.id) if scope.kind == "project" and scope.id else ""


def _freezone_agent_id_from_payload(payload: Any) -> str:
    agent_id = str(getattr(payload, "agent_id", None) or "main").strip()
    return agent_id or "main"


def _candidate_canvas_bridge_dirs(
    username: str, payload: Any, *, project_state_dir: Any = None
) -> list[Any]:
    """Return bridge dirs that may contain the pending file for a canvas tool call.

    Older/freezone-main workers wrote pending files into the Freezone workspace's
    base bridge directory, while newer agent-profiled workers use a
    ``freezone_<agent>`` subdirectory.  Resolve against the directory that
    actually contains the pending file so Hermes does not keep waiting after the
    browser has already applied the command.
    """
    if getattr(payload, "canvas_id", None):
        dirs = [
            _canvas_bridge_dir(
                username,
                profile=f"freezone:{_freezone_agent_id_from_payload(payload)}",
                project_state_dir=project_state_dir,
            ),
            _canvas_bridge_dir(
                username, profile="freezone", project_state_dir=project_state_dir
            ),
        ]
    else:
        dirs = [
            _canvas_bridge_dir(
                username, profile="director", project_state_dir=project_state_dir
            )
        ]
    unique: list[Any] = []
    seen: set[str] = set()
    for path in dirs:
        marker = str(path)
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(path)
    return unique


def _bridge_dir_for_pending_key(
    username: str, payload: Any, *, project_state_dir: Any = None
) -> Any:
    key = canonical_bridge_key(str(getattr(payload, "bridge_key", "") or "").strip())
    candidates = _candidate_canvas_bridge_dirs(
        username, payload, project_state_dir=project_state_dir
    )
    for directory in candidates:
        try:
            if (directory / f"{key}.pending.json").exists() or read_bridge_message(
                key, bridge_dir=directory
            ):
                return directory
        except Exception:
            continue
    return candidates[0]


def _pending_workflow_draft_receipt(
    username: str,
    payload: CanvasCommandToolResultIn,
    project_state_dir: Any = None,
) -> dict[str, Any] | None:
    """Recover task/version binding from the pending command, never the callback body."""
    key = payload.bridge_key.strip()
    if not key:
        return None
    directory = _bridge_dir_for_pending_key(
        username, payload, project_state_dir=project_state_dir
    )
    pending = _load_pending_canvas_command(directory / f"{key}.pending.json")
    if pending is None:
        durable = read_bridge_message(key, bridge_dir=directory)
        if isinstance(durable, dict):
            pending = durable
    commands = (
        pending.get("envelope", {}).get("commands")
        if isinstance(pending, dict)
        else None
    )
    if not isinstance(commands, list):
        return None
    identities = set()
    for command in commands:
        if not isinstance(command, dict) or command.get("type") != "create_node":
            continue
        data = command.get("data")
        if not isinstance(data, dict):
            return None
        draft_id = data.get("workflowInstanceId")
        revision = data.get("workflowDraftRevision")
        task_id = data.get("workflowConfirmationTaskId")
        if (
            not isinstance(draft_id, str)
            or not draft_id.startswith("workflow_draft_")
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision <= 0
            or not isinstance(task_id, str)
            or not task_id.strip()
        ):
            return None
        identities.add((draft_id, revision, task_id))
    if len(identities) != 1:
        return None
    draft_id, revision, task_id = identities.pop()
    return {"draft_id": draft_id, "revision": revision, "task_id": task_id}


_NATIVE_PROCESSING_ACTIONS = {
    "generate_production_plan_draft", "generate_production_plan_arrangement",
    "compile_production_plan", "align_production_speech",
}


def _bridge_approves_native_action(
    commands: list[dict[str, Any]], canvas: dict[str, Any], *, node_id: str, action: str,
) -> bool:
    """Resolve approved graph aliases against the saved canvas, once at admission."""
    nodes = [node for node in canvas.get("nodes", []) if isinstance(node, dict)]
    if not any(node.get("id") == node_id for node in nodes):
        return False
    aliases: dict[str, str] = {}
    for command in commands:
        if command.get("type") != "create_node":
            continue
        alias = command.get("client_id")
        data = command.get("data")
        if not isinstance(alias, str) or not isinstance(data, dict):
            continue
        workflow_id = data.get("workflowInstanceId")
        plan_id = data.get("workflowPlanNodeId")
        if not workflow_id or not plan_id:
            continue
        matches = [node for node in nodes if isinstance(node.get("data"), dict)
                   and node["data"].get("workflowInstanceId") == workflow_id
                   and node["data"].get("workflowPlanNodeId") == plan_id]
        if len(matches) == 1:
            aliases[alias] = str(matches[0]["id"])
    for command in commands:
        if (command.get("type") == "run_node_action"
                and command.get("node_id") == node_id
                and command.get("action") == action):
            return True
        if command.get("type") == "run_workflow" and isinstance(command.get("node_ids"), list):
            for approved_id in command["node_ids"]:
                if aliases.get(approved_id, approved_id) == node_id:
                    return True
    return False


def _pending_native_processing_commands(
    username: str, payload: CanvasCommandToolResultIn, project_state_dir: Any = None,
) -> list[dict[str, Any]]:
    directory = _bridge_dir_for_pending_key(
        username, payload, project_state_dir=project_state_dir,
    )
    key = payload.bridge_key.strip()
    pending = _load_pending_canvas_command(directory / f"{key}.pending.json")
    if pending is None:
        pending = read_bridge_message(key, bridge_dir=directory)
    envelope = pending.get("envelope") if isinstance(pending, dict) else None
    commands = envelope.get("commands") if isinstance(envelope, dict) else None
    if not isinstance(commands, list) and isinstance(pending, dict):
        commands = pending.get("commands")
    if not isinstance(commands, list):
        return []
    return [command for command in commands if isinstance(command, dict)]


def _approved_native_processing_receipt_command(
    commands: list[dict[str, Any]], canvas: dict[str, Any], *, node_id: str, action: str,
) -> dict[str, str] | None:
    if action not in _NATIVE_PROCESSING_ACTIONS or not _bridge_approves_native_action(
        commands, canvas, node_id=node_id, action=action,
    ):
        return None
    return {"type": "run_node_action", "node_id": node_id, "action": action}


def _validate_native_workflow_receipt(
    ctx: ProjectContext,
    payload: CanvasCommandToolResultIn,
    approved_commands: list[dict[str, Any]],
    claimed_actions: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Validate one host-owned run against every native action in its bridge."""
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.freezone.workflow_runs import read_workflow_run

    if (payload.canvas_apply_status != "accepted"
            or payload.completion_owner != "host"
            or not payload.workflow_run_id):
        raise HTTPException(409, "native workflow acceptance is missing a durable run")
    run = read_workflow_run(
        project_dir=ctx.state_dir, canvas_id=str(payload.canvas_id),
        run_id=payload.workflow_run_id,
    )
    metadata = run.get("metadata") if isinstance(run, dict) else None
    if (not run or run.get("project_id") != ctx.project_id
            or run.get("canvas_id") != payload.canvas_id
            or run.get("actor_id") != ctx.requester_user_id
            or run.get("status") not in {"running", "completed"}
            or not isinstance(metadata, dict)
            or metadata.get("canvas_bridge_key") != canonical_bridge_key(payload.bridge_key)
            or metadata.get("native_completion_owner") != "host"):
        raise HTTPException(409, "native workflow approval or ownership differs")
    if not any(command.get("type") == "run_workflow" or (
        command.get("type") == "run_node_action"
        and command.get("action") in _NATIVE_PROCESSING_ACTIONS
    ) for command in approved_commands):
        raise HTTPException(409, "native workflow approval has no run action")
    actions = run.get("actions") or []
    if not actions or any(not isinstance(item, dict) or item.get("action") not in _NATIVE_PROCESSING_ACTIONS
                          for item in actions):
        raise HTTPException(409, "native workflow has an unowned action")
    expected = {(item["node_id"], item["action"]) for item in actions}
    if len(expected) != len(actions):
        raise HTTPException(409, "native workflow contains duplicate actions")
    if claimed_actions is not None and claimed_actions:
        claimed = {(item.get("node_id") or item.get("nodeId"), item.get("action"))
                   for item in claimed_actions}
        if claimed != expected:
            raise HTTPException(409, "native workflow receipt omits an action")
    recorded_approval = {
        (item.get("node_id"), item.get("action"))
        for item in metadata.get("native_approved_actions", [])
        if isinstance(item, dict)
    }
    if recorded_approval != expected:
        raise HTTPException(409, "native workflow approved targets differ")
    operation_store = RemixOperationStore(ctx.output_dir, ctx.project_id)
    for item in actions:
        node_id, action = item["node_id"], item["action"]
        operation_id = item.get("processing_operation_id")
        if (item.get("status") not in {"running", "completed"}
                or not isinstance(operation_id, str) or not operation_id
                or not item.get("task_key")):
            raise HTTPException(409, "native workflow action is not durably submitted")
        try:
            operation = operation_store.get(operation_id)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(409, "native workflow operation is unavailable") from exc
        parameters = operation.get("normalized_parameters")
        parameters = parameters if isinstance(parameters, dict) else {}
        if (operation.get("canvas_id") != payload.canvas_id
                or operation.get("node_id") != node_id
                or operation.get("status") in {"failed", "cancelled", "cancelling", "stale"}
                or (item.get("node_revision") is not None
                    and operation.get("node_revision") != item.get("node_revision"))
                or (isinstance(item.get("payload"), dict)
                    and (parameters.get("action") != action
                         or parameters.get("payload") != item["payload"]))):
            raise HTTPException(409, "native workflow operation identity differs")
    return {"workflow_run_id": payload.workflow_run_id, "completion_owner": "host"}


def _validate_native_processing_receipt(
    ctx: ProjectContext, payload: CanvasCommandToolResultIn, pending_command: dict[str, Any],
) -> dict[str, str]:
    """Prove browser acceptance against the server's run and submitted task."""
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.freezone.workflow_runs import read_workflow_run

    fields = (payload.workflow_run_id, payload.operation_id, payload.task_id,
              payload.node_id, payload.action)
    if (payload.canvas_apply_status != "accepted" or payload.completion_owner != "host"
            or not all(isinstance(value, str) and value.strip() for value in fields)
            or payload.action not in _NATIVE_PROCESSING_ACTIONS):
        raise HTTPException(409, "native processing acceptance is missing a durable task receipt")
    if (pending_command.get("node_id") != payload.node_id
            or pending_command.get("action") != payload.action):
        raise HTTPException(409, "native processing receipt differs from the approved command")
    run = read_workflow_run(project_dir=ctx.state_dir, canvas_id=str(payload.canvas_id),
                            run_id=str(payload.workflow_run_id))
    if (not run or run.get("project_id") != ctx.project_id
            or run.get("canvas_id") != payload.canvas_id
            or run.get("actor_id") != ctx.requester_user_id
            or run.get("status") in {"failed", "cancelled", "interrupted"}):
        raise HTTPException(409, "native processing workflow is unavailable or stopped")
    metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
    if metadata.get("canvas_bridge_key") != canonical_bridge_key(payload.bridge_key):
        raise HTTPException(409, "native processing receipt belongs to another approval")
    actions = [item for item in run.get("actions", []) if item.get("node_id") == payload.node_id
               and item.get("action") == payload.action]
    if len(actions) != 1:
        raise HTTPException(409, "native processing action is not in the workflow")
    action = actions[0]
    if (action.get("processing_operation_id") != payload.operation_id
            or not action.get("task_key") or action.get("status") in {"failed", "blocked", "skipped"}):
        raise HTTPException(409, "native processing action has no accepted task")
    try:
        operation = RemixOperationStore(ctx.output_dir, ctx.project_id).get(str(payload.operation_id))
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(409, "native processing operation is unavailable") from exc
    if (operation.get("canvas_id") != payload.canvas_id
            or operation.get("node_id") != payload.node_id
            or operation.get("task_id") != payload.task_id
            or operation.get("status") in {"failed", "cancelled"}):
        raise HTTPException(409, "native processing task identity differs")
    return {"workflow_run_id": str(payload.workflow_run_id),
            "operation_id": str(payload.operation_id), "task_id": str(payload.task_id),
            "node_id": str(payload.node_id), "action": str(payload.action),
            "completion_owner": "host"}


def _saved_workflow_graph_matches_receipt(
    payload: CanvasCommandToolResultIn, draft: dict[str, Any], canvas: dict[str, Any],
) -> bool:
    """A failed or pending action cannot undo an independently saved graph."""
    from novelvideo.freezone.workflow_delivery import inspect_workflow_graph_delivery

    return inspect_workflow_graph_delivery(
        receipt=payload.model_dump(), draft=draft, canvas=canvas,
    )["state"] == "delivered"


def _claimed_native_run_actions(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only the current admitted stage belongs to the durable host run."""
    return [item for item in steps
            if item.get("type") == "run_node_action" and item.get("status") == "pending"
            and isinstance(item.get("output"), dict)
            and item["output"].get("reason") == "native_action_accepted"]


async def _record_workflow_draft_canvas_result(
    *,
    user: dict[str, Any],
    payload: CanvasCommandToolResultIn,
    draft_receipt: dict[str, Any] | None,
    resolved: dict[str, Any],
    strict: bool = False,
) -> dict[str, Any] | None:
    if not draft_receipt:
        return
    draft_id = draft_receipt["draft_id"]
    project_id = str(payload.project_id or "").strip()
    canvas_id = str(payload.canvas_id or "").strip()
    if not project_id or not canvas_id:
        return
    try:
        from novelvideo.freezone.workflow_drafts import (
            read_workflow_draft,
            record_workflow_delivery_receipt,
            settle_workflow_draft_delivery,
        )
        from novelvideo.task_state import get_task_manager

        scope = ChatScope(
            kind="project",
            id=project_id,
            surface="freezone",
            canvas_id=canvas_id,
            agent_id=_freezone_agent_id_from_payload(payload),
        )
        project_ctx = await _project_context_for_scope(user, scope)
        if project_ctx is None:
            raise RuntimeError("workflow draft project context is unavailable")
        draft, _error = await asyncio.to_thread(
            read_workflow_draft,
            project_dir=project_ctx.state_dir,
            canvas_id=canvas_id,
            draft_id=draft_id,
        )
        if draft is None:
            if strict:
                raise ValueError("workflow draft is unavailable")
            return None
        task_id = str(draft.get("task_id") or "")
        revision = int(draft.get("revision") or 0)
        attempt_started_at = draft.get("confirmation_started_at")
        if task_id != draft_receipt["task_id"] or revision != draft_receipt["revision"]:
            if strict:
                raise ValueError("workflow draft confirmation identity changed")
            return None
        if attempt_started_at is None:
            if strict:
                raise ValueError("workflow draft has no active confirmation")
            return None
        task_manager = get_task_manager()
        task_state = None
        for task_scope in (
            f"{canvas_id}:{draft_id}:{revision}:{attempt_started_at}",
            f"{canvas_id}:{draft_id}:{revision}",
        ):
            task_state = await asyncio.to_thread(
                task_manager.get_task_for_project,
                project_ctx,
                "freezone_workflow_confirm",
                0,
                beat_num=None,
                scope=task_scope,
            )
            if task_state is not None:
                break
        if task_state is not None and str(task_state.task_id) != task_id:
            logger.info(
                "ignored workflow canvas result for another task draft_id=%s",
                draft_id,
            )
            if strict:
                raise ValueError("workflow draft confirmation task changed")
            return None
        from novelvideo.freezone import canvas_store
        from novelvideo.freezone.workflow_delivery import inspect_workflow_graph_delivery

        saved_canvas = await asyncio.to_thread(
            canvas_store.read_canvas, project_ctx.state_dir, canvas_id,
        )
        proof = inspect_workflow_graph_delivery(
            receipt=payload.model_dump(), draft=draft, canvas=saved_canvas or {},
        )
        await asyncio.to_thread(
            record_workflow_delivery_receipt,
            project_dir=project_ctx.state_dir, canvas_id=canvas_id, draft_id=draft_id,
            revision=revision, task_id=task_id, attempt=attempt_started_at,
            bridge_key=payload.bridge_key, receipt=payload.model_dump(), proof=proof,
        )
        await asyncio.to_thread(
            settle_workflow_draft_delivery,
            project_dir=project_ctx.state_dir,
            canvas_id=canvas_id,
            draft_id=draft_id,
            task_id=task_id,
            revision=revision,
            attempt=attempt_started_at,
            proof=proof,
            canvas=saved_canvas or {},
        )
        return proof
    except Exception:
        logger.exception(
            "failed to record durable workflow canvas outcome draft_id=%s",
            draft_id,
        )
        if strict:
            raise
        return None


async def _close_freezone_agent_worker(username: str, agent_id: str | None) -> bool:
    try:
        from novelvideo.chat.hermes_pool import pool as hermes_pool

        return await hermes_pool.close_user_profile(
            username, f"freezone:{agent_id or 'main'}"
        )
    except Exception:
        logger.exception(
            "failed to close freezone hermes worker after canvas command cancellation"
        )
        return False


async def _close_canvas_command_worker(
    username: str, payload: CanvasCommandToolResultIn
) -> bool:
    if payload.canvas_id:
        return await _close_freezone_agent_worker(
            username, _freezone_agent_id_from_payload(payload)
        )
    try:
        from novelvideo.chat.hermes_pool import pool as hermes_pool

        return await hermes_pool.close_user(username)
    except Exception:
        logger.exception(
            "failed to close hermes worker after canvas command cancellation"
        )
        return False


def _resolve_canvas_command_tool_result_payload(
    payload: CanvasCommandToolResultIn,
    *,
    username: str,
    project_state_dir: Any = None,
) -> dict[str, Any]:
    execution_token = validate_bridge_receipt(payload.bridge_key.strip(),
        bridge_dir=_bridge_dir_for_pending_key(username, payload, project_state_dir=project_state_dir),
        username=username, project_id=payload.project_id, canvas_id=payload.canvas_id,
        agent_id=_freezone_agent_id_from_payload(payload), turn_id=payload.turn_id)
    payload.bridge_key = canonical_bridge_key(payload.bridge_key)
    key = payload.bridge_key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="bridge_key is required")
    command_ok = (
        payload.tool_call_status == "completed"
        and payload.canvas_apply_status in {"applied", "accepted"}
        and payload.applied
        and not payload.cancelled
        and not payload.errors
    )
    has_run_node_action = any(
        isinstance(item, dict) and item.get("type") == "run_node_action"
        for item in payload.command_results
    )
    has_open_node_action = any(
        isinstance(item, dict)
        and item.get("type") == "run_node_action"
        and isinstance(item.get("action"), str)
        and item["action"].startswith("open_")
        for item in payload.command_results
    )
    if payload.canvas_apply_status == "cancelled_by_user":
        message = "User cancelled the canvas command before execution."
        agent_instruction = (
            "Do not claim the canvas change was applied; ask the user before retrying."
        )
    elif not command_ok:
        message = (
            payload.user_message
            or payload.message
            or "Frontend executor reported that the canvas command failed."
        )
        agent_instruction = (
            payload.agent_hint
            or "Do not claim success. Read errors and command_results, then fix the command before trying again. Do not expose raw canvas protocol details to the user."
        )
    elif payload.canvas_apply_status == "accepted":
        message = payload.message or "Canvas command was submitted and is running in the background."
        agent_instruction = (
            "Tell the user generation is running in the background and its status will update automatically. "
            "Do not claim that generation is complete, do not say a tool was opened, and do not ask the user "
            "to operate it manually."
        )
    else:
        message = payload.message or "Frontend executor applied the canvas command."
        if has_open_node_action:
            agent_instruction = (
                "Canvas command applied successfully. Tell the user the requested canvas panel has been opened. "
                "Do not say it is processing or submitted for generation."
            )
        elif has_run_node_action:
            agent_instruction = (
                "Canvas command applied successfully. Tell the user the requested canvas action has been submitted to the canvas. "
                "Do not say a panel was opened."
            )
        else:
            agent_instruction = "Canvas command applied successfully."
    requires_canvas_refresh = bool(
        payload.canvas_id
        and (
            payload.applied_count > 0
            or (
                not payload.cancelled
                and (
                    (payload.applied and not has_open_node_action)
                    or payload.canvas_apply_status in {"accepted", "pending", "partially_applied"}
                )
            )
        )
    )
    if requires_canvas_refresh:
        agent_instruction += (
            " Earlier canvas/story revisions are now stale or may become stale while "
            "generation finishes. Before the next revision-based write on this canvas, "
            "read its current persisted state with dramaclaw_get_freezone_canvas (or "
            "the current story with dramaclaw_get_interactive_story), recheck the intended "
            "changes, and use that read's revision as base_revision. Do not reuse a "
            "revision read before this command, guess revision + 1, or replay generation."
        )
    result = {
        "ok": command_ok,
        "turn_id": payload.turn_id,
        "tool_call_status": payload.tool_call_status,
        "canvas_apply_status": payload.canvas_apply_status,
        "background_execution_status": payload.background_execution_status,
        "applied": payload.applied,
        "cancelled": payload.cancelled,
        "errors": payload.errors,
        "applied_count": payload.applied_count,
        "opened_ui_actions": payload.opened_ui_actions,
        "created_node_ids": payload.created_node_ids,
        "command_results": payload.command_results,
        "project_id": payload.project_id,
        "canvas_id": payload.canvas_id,
        "message": message,
        "user_message": payload.user_message,
        "agent_instruction": agent_instruction,
        "agent_hint": payload.agent_hint,
        "requires_canvas_refresh": requires_canvas_refresh,
        **({
            "workflow_run_id": payload.workflow_run_id,
            "operation_id": payload.operation_id,
            "task_id": payload.task_id,
            "node_id": payload.node_id,
            "action": payload.action,
            "completion_owner": payload.completion_owner,
        } if payload.canvas_apply_status == "accepted" and payload.completion_owner == "host" else {}),
    }
    result["_bridge_execution_token"] = execution_token
    return resolve_canvas_command(
        key,
        result,
        bridge_dir=_bridge_dir_for_pending_key(
            username, payload, project_state_dir=project_state_dir
        ),
    )


def _resolve_canvas_context_tool_result_payload(
    payload: CanvasContextToolResultIn,
    *,
    username: str,
    project_state_dir: Any = None,
) -> dict[str, Any]:
    execution_token = validate_bridge_receipt(payload.bridge_key.strip(),
        bridge_dir=_bridge_dir_for_pending_key(username, payload, project_state_dir=project_state_dir),
        username=username, project_id=payload.project_id, canvas_id=payload.canvas_id,
        agent_id=_freezone_agent_id_from_payload(payload), turn_id=payload.turn_id)
    payload.bridge_key = canonical_bridge_key(payload.bridge_key)
    key = payload.bridge_key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="bridge_key is required")
    result = {
        "ok": payload.ok and payload.tool_call_status == "completed",
        "tool_call_status": payload.tool_call_status,
        "canvas_context_status": payload.canvas_context_status
        or (
            "resolved"
            if payload.ok and payload.tool_call_status == "completed"
            else "failed"
        ),
        "responses": payload.responses,
        "source": "current_browser",
        "read_consistency": "current_browser",
        "errors": payload.errors,
        "project_id": payload.project_id,
        "canvas_id": payload.canvas_id,
        "message": payload.message or "Frontend returned requested canvas context.",
    }
    result["_bridge_execution_token"] = execution_token
    return resolve_canvas_context(
        key,
        result,
        bridge_dir=_bridge_dir_for_pending_key(
            username, payload, project_state_dir=project_state_dir
        ),
    )


def _skill_studio_draft_catalog_ids(
    draft: dict[str, Any] | None,
) -> tuple[list[str], list[str]]:
    if not isinstance(draft, dict):
        return [], []
    skill = draft.get("skill")
    skill_id = ""
    if isinstance(skill, dict):
        skill_id = str(skill.get("id") or "").strip()
    recipe_ids: list[str] = []
    recipes = draft.get("recipes")
    if isinstance(recipes, list):
        for recipe in recipes:
            if not isinstance(recipe, dict):
                continue
            recipe_id = str(recipe.get("id") or "").strip()
            if recipe_id:
                recipe_ids.append(recipe_id)
    return ([skill_id] if skill_id else []), recipe_ids


def _save_skill_studio_draft_catalog(
    *,
    username: str,
    draft: dict[str, Any] | None,
) -> tuple[list[str], list[str], list[str]]:
    if not isinstance(draft, dict):
        return [], [], ["Skill Studio draft is missing."]

    saved_skill_ids: list[str] = []
    saved_recipe_ids: list[str] = []
    errors: list[str] = []
    recipes = draft.get("recipes")
    if isinstance(recipes, list):
        for index, recipe in enumerate(recipes):
            if not isinstance(recipe, dict) or not str(recipe.get("id") or "").strip():
                continue
            try:
                saved = save_user_agent_config_item(
                    username=username, kind="recipes", payload=recipe
                )
                saved_recipe_ids.append(str(saved.get("id") or recipe.get("id")))
            except Exception as exc:
                recipe_id = str(recipe.get("id") or f"#{index + 1}")
                errors.append(f"Failed to save Recipe {recipe_id}: {exc}")

    skill = draft.get("skill")
    if isinstance(skill, dict) and str(skill.get("id") or "").strip():
        try:
            saved = save_user_agent_config_item(
                username=username, kind="skills", payload=skill
            )
            saved_skill_ids.append(str(saved.get("id") or skill.get("id")))
        except Exception as exc:
            errors.append(f"Failed to save Skill: {exc}")

    if not saved_skill_ids and not saved_recipe_ids and not errors:
        errors.append("Skill Studio draft does not contain a Skill or Recipe id.")
    return saved_skill_ids, saved_recipe_ids, errors


def _resolve_skill_studio_tool_result_payload(
    payload: SkillStudioToolResultIn,
    *,
    username: str,
    project_state_dir: Any = None,
) -> dict[str, Any]:
    execution_token = validate_bridge_receipt(payload.bridge_key.strip(),
        bridge_dir=_bridge_dir_for_pending_key(username, payload, project_state_dir=project_state_dir),
        username=username, project_id=payload.project_id, canvas_id=payload.canvas_id,
        agent_id=_freezone_agent_id_from_payload(payload), turn_id=payload.turn_id)
    payload.bridge_key = canonical_bridge_key(payload.bridge_key)
    key = payload.bridge_key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="bridge_key is required")
    ok = payload.ok and payload.tool_call_status == "completed" and not payload.errors
    saved_to_catalog = (
        payload.saved_to_catalog or payload.skill_studio_status == "catalog_saved"
    )
    save_requested = saved_to_catalog
    saved_skill_ids: list[str] = []
    saved_recipe_ids: list[str] = []
    skill_studio_status = payload.skill_studio_status
    errors = list(payload.errors)
    cancelled = (
        payload.action == "cancel" or payload.skill_studio_status == "catalog_cancelled"
    )
    revision_started = (
        payload.action == "start_revision"
        or payload.skill_studio_status == "revision_started"
    )
    if ok and saved_to_catalog:
        saved_skill_ids, saved_recipe_ids, catalog_errors = (
            _save_skill_studio_draft_catalog(
                username=username,
                draft=payload.draft,
            )
        )
        if catalog_errors:
            errors.extend(catalog_errors)
            ok = False
        elif saved_skill_ids:
            sync_freezone_hermes_workflow_skills(username)
            try:
                from novelvideo.chat.hermes_pool import pool as hermes_pool

                hermes_pool.mark_user_freezone_profiles_dirty(username)
            except Exception:
                logger.exception(
                    "failed to mark Freezone Hermes worker dirty after Skill Studio save"
                )
    if save_requested:
        saved_to_catalog = ok
        skill_studio_status = (
            "catalog_saved"
            if ok
            else (
                "catalog_partially_saved"
                if saved_skill_ids or saved_recipe_ids
                else "catalog_save_failed"
            )
        )
    if ok:
        if saved_to_catalog:
            agent_instruction = (
                "The frontend has saved this Skill/Recipe draft to the Freezone catalog. "
                "This is a frontend save event, not a natural-language user reply and not the user saying 'ok'. "
                "Do not apply user-profile rules for short 'ok' replies. "
                "Treat it as official saved catalog content that can be used immediately. "
                "Do not ask the user to save it again, do not analyze the draft, do not start another Skill Studio step, "
                "and do not include hidden reasoning. Reply briefly in Chinese only that it has been saved to Xi画 Skills / Recipes."
            )
            message = (
                payload.message
                or "Frontend saved the Skill/Recipe draft to the Freezone catalog."
            )
        elif cancelled:
            agent_instruction = (
                "The user cancelled saving this Skill/Recipe draft. "
                "This is not a revision request and not a resubmission request. "
                "Do not resubmit, recreate, revise, display, or save this draft. "
                "Do not call any Skill Studio creation, patch, finish, or save tools. "
                "Acknowledge the cancellation and stop unless the user explicitly asks for a next step."
            )
            message = (
                payload.message
                or "Frontend reported that the user cancelled saving the Skill/Recipe draft."
            )
        elif revision_started:
            agent_instruction = (
                "The user started revising the Skill Studio draft. "
                "The frontend response intentionally only contains a lightweight draft reference, not the full draft. "
                "Do not infer concrete edits from the existing draft or conversation history. "
                "Ask one clarification question for the user's exact revision direction before patching or resubmitting draft content."
            )
            message = (
                payload.message
                or "Frontend reported that the user started revising the Skill/Recipe draft."
            )
        else:
            agent_instruction = (
                "Continue the Skill Studio flow using the frontend response."
            )
            message = (
                payload.message or "Frontend returned the user's Skill Studio response."
            )
    else:
        if save_requested:
            message = (
                "Skill / Recipe 仅部分保存成功，请修正失败项后重试。"
                if saved_skill_ids or saved_recipe_ids
                else "Skill / Recipe 保存失败，未保存任何条目，请修正错误后重试。"
            )
            agent_instruction = (
                "Catalog saving did not fully succeed. Report the errors and only the IDs "
                "in saved_skill_ids / saved_recipe_ids as saved. Do not claim the draft "
                "is ready to use or start its workflow. Keep the draft available for correction."
            )
        else:
            agent_instruction = (
                "Do not continue the Skill Studio flow; handle the frontend error or ask "
                "the user to retry."
            )
            message = (
                payload.message
                or "Frontend reported that the Skill Studio interaction failed."
            )
    agent_visible_draft = (
        None if saved_to_catalog or cancelled or revision_started else payload.draft
    )
    result = {
        "ok": ok,
        "status": "skill_studio_frontend_result",
        "turn_id": payload.turn_id,
        "tool_call_status": (
            "failed" if save_requested and not ok else payload.tool_call_status
        ),
        "skill_studio_status": skill_studio_status,
        "action": payload.action,
        "selections": payload.selections,
        "draft": agent_visible_draft,
        "draft_ref": payload.draft_ref,
        "saved_to_catalog": saved_to_catalog,
        "saved_skill_ids": saved_skill_ids,
        "saved_recipe_ids": saved_recipe_ids,
        "errors": errors,
        "project_id": payload.project_id,
        "canvas_id": payload.canvas_id,
        "message": message,
        "agent_instruction": agent_instruction,
        "client_debug": payload.client_debug,
    }
    result["_bridge_execution_token"] = execution_token
    return resolve_skill_studio_result(
        key,
        result,
        bridge_dir=_canvas_bridge_dir(
            username,
            project_state_dir=project_state_dir,
            profile=(
                f"freezone:{_freezone_agent_id_from_payload(payload)}"
                if result.get("canvas_id")
                else "director"
            ),
        ),
    )


def _skill_studio_result_log_fields(
    payload: SkillStudioToolResultIn,
    *,
    username: str,
) -> dict[str, Any]:
    return {
        "username": username,
        "turn_id": payload.turn_id,
        "bridge_key": payload.bridge_key,
        "project_id": payload.project_id,
        "canvas_id": payload.canvas_id,
        "agent_id": _freezone_agent_id_from_payload(payload),
        "action": payload.action,
        "skill_studio_status": payload.skill_studio_status,
        "tool_call_status": payload.tool_call_status,
        "ok": payload.ok,
        "saved_to_catalog": payload.saved_to_catalog,
        "saved_skill_ids": payload.saved_skill_ids,
        "saved_recipe_ids": payload.saved_recipe_ids,
        "errors": payload.errors,
        "client_debug": payload.client_debug,
    }


def _prepare_clarification_tool_result_payload(
    payload: ClarificationToolResultIn,
    *,
    username: str,
    project_state_dir: Any = None,
    credential_kind: str | None = None,
) -> dict[str, Any]:
    if credential_kind in {"agent_session", "local_trusted_agent"}:
        raise HTTPException(status_code=403, detail="clarification answers require an authenticated human user")
    execution_token = validate_bridge_receipt(payload.bridge_key.strip(),
        bridge_dir=_bridge_dir_for_pending_key(username, payload, project_state_dir=project_state_dir),
        username=username, project_id=payload.project_id, canvas_id=payload.canvas_id,
        agent_id=_freezone_agent_id_from_payload(payload), turn_id=payload.turn_id)
    payload.bridge_key = canonical_bridge_key(payload.bridge_key)
    key = payload.bridge_key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="bridge_key is required")
    ok = payload.ok and payload.tool_call_status == "completed" and not payload.errors
    if ok:
        agent_instruction = "Continue using the frontend clarification response."
        message = (
            payload.message or "Frontend returned the user's clarification response."
        )
    else:
        agent_instruction = (
            "Do not continue; handle the clarification error or ask the user to retry."
        )
        message = (
            payload.message
            or "Frontend reported that the clarification interaction failed."
        )
    result = {
        "ok": ok,
        "status": "clarification_frontend_result",
        "turn_id": payload.turn_id,
        "tool_call_status": payload.tool_call_status,
        "clarification_status": payload.clarification_status,
        "action": payload.action,
        "answers": payload.answers,
        "skipped": payload.skipped,
        "used_recommended": payload.used_recommended,
        "errors": payload.errors,
        "project_id": payload.project_id,
        "canvas_id": payload.canvas_id,
        "message": message,
        "agent_instruction": agent_instruction,
    }
    result["_bridge_execution_token"] = execution_token
    return result


def _deliver_clarification_tool_result_payload(
    payload: ClarificationToolResultIn, result: dict[str, Any], *,
    username: str, project_state_dir: Any = None,
) -> dict[str, Any]:
    key = payload.bridge_key.strip()
    resolved = resolve_clarification_result(
        key,
        result,
        bridge_dir=_canvas_bridge_dir(
            username,
            project_state_dir=project_state_dir,
            profile=(
                f"freezone:{_freezone_agent_id_from_payload(payload)}"
                if result.get("canvas_id")
                else "director"
            ),
        ),
    )

    if resolved.get("ok") and str(payload.turn_id or "").startswith("reference-remix-") and payload.project_id and payload.canvas_id:
        from novelvideo.chat.reference_remix_continuation import coordinator
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return resolved
        task = loop.create_task(coordinator.wake_clarification(
            username=username, project_id=payload.project_id, canvas_id=payload.canvas_id,
            agent_id=_freezone_agent_id_from_payload(payload), turn_id=payload.turn_id, bridge_key=key))
        task.add_done_callback(lambda done: logger.error("reference clarification wake failed", exc_info=done.exception()) if not done.cancelled() and done.exception() else None)
    return resolved


def _resolve_clarification_tool_result_payload(
    payload: ClarificationToolResultIn, *, username: str,
    project_state_dir: Any = None, credential_kind: str | None = None,
) -> dict[str, Any]:
    result = _prepare_clarification_tool_result_payload(
        payload, username=username, project_state_dir=project_state_dir,
        credential_kind=credential_kind,
    )
    return _deliver_clarification_tool_result_payload(
        payload, result, username=username, project_state_dir=project_state_dir,
    )


async def _resolve_clarification_with_reference_persistence(
    *, payload: ClarificationToolResultIn, user: dict[str, Any], username: str,
    project_state_dir: Any,
) -> dict[str, Any]:
    result = _prepare_clarification_tool_result_payload(
        payload, username=username, credential_kind=user.get("credential_kind"),
        project_state_dir=project_state_dir,
    )
    try:
        await _persist_reference_parameter_submission(
            user=user, payload=payload, result=result,
            project_state_dir=project_state_dir,
        )
    except Exception:
        logger.exception("failed to persist verified reference parameters")
        raise
    return _deliver_clarification_tool_result_payload(
        payload, result, username=username, project_state_dir=project_state_dir,
    )


def _scope_from_interaction_payload(
    payload: SkillStudioToolResultIn | ClarificationToolResultIn,
) -> ChatScope | None:
    project_id = str(payload.project_id or "").strip()
    canvas_id = str(payload.canvas_id or "").strip()
    if not project_id or not canvas_id:
        return None
    return ChatScope(
        kind="project",
        id=project_id,
        surface="freezone",
        canvas_id=canvas_id,
        agent_id=_freezone_agent_id_from_payload(payload),
    )


async def _project_context_for_interaction_result(
    user: dict[str, Any],
    scope: ChatScope,
) -> ProjectContext | None:
    try:
        result = _project_context_for_scope(user, scope)
        if inspect.isawaitable(result):
            return await result
        return result
    except Exception:
        logger.debug(
            "failed to resolve project context for interaction result", exc_info=True
        )
        return None


async def _persist_skill_studio_result_ui_event(
    *,
    user: dict[str, Any],
    username: str,
    payload: SkillStudioToolResultIn,
    resolved: dict[str, Any] | None = None,
) -> None:
    turn_id = str(payload.turn_id or "").strip()
    scope = _scope_from_interaction_payload(payload)
    if not turn_id or scope is None:
        return
    if (
        resolved is not None
        and resolved.get("ok") is False
        and (payload.saved_to_catalog or payload.skill_studio_status == "catalog_saved")
    ):
        return
    project_ctx = await _project_context_for_interaction_result(user, scope)
    event: dict[str, Any] = {
        "type": (
            "skill_studio.questions" if payload.draft is None else "skill_studio.draft"
        ),
        "bridge_key": payload.bridge_key,
        "project_id": payload.project_id,
        "canvas_id": payload.canvas_id,
        "agent_id": payload.agent_id,
        "submitted": True,
        "action": payload.action,
        "skill_studio_status": payload.skill_studio_status,
    }
    if payload.selections:
        event["selections"] = payload.selections
    if payload.draft is not None:
        event["draft"] = payload.draft
    if (
        payload.action == "start_revision"
        or payload.skill_studio_status == "revision_started"
    ):
        event["revision_pending"] = True
    saved_to_catalog = (
        bool(resolved.get("saved_to_catalog"))
        if resolved is not None
        else (
            payload.saved_to_catalog or payload.skill_studio_status == "catalog_saved"
        )
    )
    if saved_to_catalog:
        event["saved_to_catalog"] = True
        draft_skill_ids, draft_recipe_ids = _skill_studio_draft_catalog_ids(
            payload.draft
        )
        if resolved is not None:
            event["saved_skill_ids"] = list(resolved.get("saved_skill_ids") or [])
            event["saved_recipe_ids"] = list(resolved.get("saved_recipe_ids") or [])
        else:
            event["saved_skill_ids"] = payload.saved_skill_ids or draft_skill_ids
            event["saved_recipe_ids"] = payload.saved_recipe_ids or draft_recipe_ids
    await chat_store.append_ui_event_async(
        username,
        _chat_store_scope_for_project_context(scope, project_ctx),
        turn_id,
        event,
    )


async def _persist_clarification_result_ui_event(
    *,
    user: dict[str, Any],
    username: str,
    payload: ClarificationToolResultIn,
) -> None:
    turn_id = str(payload.turn_id or "").strip()
    scope = _scope_from_interaction_payload(payload)
    if not turn_id or scope is None:
        return
    project_ctx = await _project_context_for_interaction_result(user, scope)
    pending_status = payload.clarification_status in {
        "pending_user_input",
        "waiting_user",
    }
    # A result can be persisted while the bridge is still waiting (for
    # example when a stale client echoes the tool payload). That is not a
    # human submission and must not turn the request into an "answered 0"
    # summary. Cancellation is terminal even though it intentionally carries
    # ok=False so the pending bridge can be settled.
    submitted = not pending_status and (payload.ok or payload.action == "cancel")
    await chat_store.append_ui_event_async(
        username,
        _chat_store_scope_for_project_context(scope, project_ctx),
        turn_id,
        {
            "type": "assistant.clarification.request",
            "bridge_key": payload.bridge_key,
            "project_id": payload.project_id,
            "canvas_id": payload.canvas_id,
            "agent_id": payload.agent_id,
            "submitted": submitted,
            "action": payload.action,
            "clarification_status": payload.clarification_status,
            "answers": payload.answers,
            "skipped": payload.skipped,
            "used_recommended": payload.used_recommended,
            "anchor_text_prefix": payload.anchor_text_prefix,
        },
    )


async def _current_reference_parameter_owner(
    ctx: Any, *, canvas_id: str, agent_id: str,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Resolve one current production commission from authenticated run history."""
    from novelvideo.chat.reference_remix_continuation import coordinator
    from novelvideo.freezone.remix.reference_commission import read_reference_commission
    from novelvideo.freezone.remix.reference_parameter_receipts import generation_requirements_digest
    from novelvideo.freezone.remix.store import RemixArtifactStore

    runs = await asyncio.to_thread(coordinator.store.list_for_actor, ctx, canvas_id, agent_id)
    eligible = [run for run in runs if run.get("commission_ref")
                and run.get("project_id") == ctx.project_id
                and run.get("canvas_id") == canvas_id
                and run.get("agent_id") == agent_id
                and run.get("requester_user_id") == ctx.requester_user_id
                and run.get("status") not in
                {"cancelled", "analysis_failed", "failed", "superseded", "stale"}]
    by_commission: dict[str, list[dict[str, Any]]] = {}
    for run in eligible:
        commission_id = (run.get("commission_ref") or {}).get("artifact_id")
        if isinstance(commission_id, str) and commission_id:
            by_commission.setdefault(commission_id, []).append(run)
    if not by_commission:
        return None
    store = RemixArtifactStore(ctx.output_dir, ctx.project_id, created_by=ctx.requester_user_id)
    current: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for commission_id, candidates in by_commission.items():
        commission = await asyncio.to_thread(
            read_reference_commission, store, candidates[0]["commission_ref"], latest=True,
        )
        if ((commission.get("commission_ref") or {}).get("artifact_id") != commission_id
                or commission.get("phase") == "completed"):
            continue
        current.append((commission, candidates))
    if len(current) != 1:
        return None
    commission, candidates = current[0]
    if len({generation_requirements_digest(
        commission, item.get("inputs") or {}) for item in candidates}) != 1:
        return None
    return commission, candidates[0]


async def _pending_reference_run_for_bridge(
    ctx: Any, owner: tuple[dict[str, Any], dict[str, Any]] | None,
    *, bridge_key: str,
) -> dict[str, Any] | None:
    """Return the current commission run that owns this parameter card."""
    if owner is None:
        return None
    _commission, run = owner
    pending = run.get("pending_interaction")
    if isinstance(pending, dict) and pending.get("bridge_key") != bridge_key:
        return None
    if (run.get("project_id") != ctx.project_id
            or run.get("requester_user_id") != ctx.requester_user_id):
        return None
    return run


def _verified_generation_interaction(
    *, run: dict[str, Any], pending: dict[str, Any], result: dict[str, Any],
    project_id: str, canvas_id: str, requester_user_id: str,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Bind a host-created generation card to one authenticated material run."""
    from novelvideo.freezone.remix.reference_parameter_receipts import _parameter_receipt

    commission_id = (run.get("commission_ref") or {}).get("artifact_id")
    agent_id = run.get("agent_id")
    requirements_digest = run.get("generation_requirements_digest")
    run_pending = run.get("pending_interaction")
    if not isinstance(run_pending, dict):
        run_pending = {}
    if (not isinstance(commission_id, str) or not commission_id
            or not isinstance(agent_id, str) or not agent_id
            or not isinstance(requirements_digest, str) or not requirements_digest
            or not isinstance(pending, dict)
            or (run_pending and pending.get("bridge_key") != run_pending.get("bridge_key"))
            or (run_pending and pending.get("clarification_id") != run_pending.get("clarification_id"))
            or result.get("ok") is not True
            or result.get("status") != "clarification_frontend_result"):
        return None
    scope = {
        "project_id": project_id,
        "canvas_id": canvas_id,
        "agent_id": agent_id,
        "requester_user_id": requester_user_id,
        "commission_id": commission_id,
    }
    pending_scope = pending.get("scope")
    if isinstance(pending_scope, dict) and pending_scope != scope:
        return None
    event = pending.get("event")
    if not isinstance(event, dict) or not event.get("clarification_id"):
        return None
    interaction = {
        "bridge_key": pending["bridge_key"],
        "clarification_id": pending["clarification_id"],
        "scope": scope,
        "generation_requirements_digest": requirements_digest,
        "requirements_snapshot": pending.get("requirements_snapshot"),
        "event": event,
        "result": result,
    }
    if _parameter_receipt(interaction, require_complete_media=False) is None:
        return None
    return scope, interaction


async def _persist_reference_parameter_submission(
    *, user: dict[str, Any], payload: ClarificationToolResultIn,
    result: dict[str, Any], project_state_dir: Any,
) -> None:
    """Share a verified browser answer with the current production commission.

    A chat turn is not the ownership boundary: the authenticated project actor,
    current commission, and confirmed requirements are. Ambiguous ownership is
    intentionally left unbound.
    """
    if (result.get("ok") is not True or result.get("status") != "clarification_frontend_result"
            or not payload.project_id or not payload.canvas_id or project_state_dir is None):
        return
    scope = _scope_from_interaction_payload(payload)
    if scope is None:
        return
    ctx = await _project_context_for_interaction_result(user, scope)
    from pathlib import Path
    if (ctx is None or ctx.project_id != payload.project_id
            or Path(ctx.state_dir).resolve() != Path(project_state_dir).resolve()):
        return
    from novelvideo.freezone.remix.reference_parameter_receipts import generation_requirements_digest
    from novelvideo.freezone.workflow_drafts import record_reference_parameter_submission

    owner = await _current_reference_parameter_owner(
        ctx, canvas_id=payload.canvas_id, agent_id=scope.agent_id,
    )
    run = await _pending_reference_run_for_bridge(
        ctx, owner, bridge_key=payload.bridge_key,
    )
    if run is None:
        return
    commission, _ = owner
    if not run.get("generation_requirements_digest"):
        run = {
            **run,
            "generation_requirements_digest": generation_requirements_digest(
                commission, run.get("inputs") or {},
            ),
        }
    pending = run.get("pending_interaction")
    had_pending = isinstance(pending, dict)
    bridge: dict[str, Any] = {}
    if not had_pending or not isinstance(pending.get("event"), dict):
        username = str(user.get("username") or user.get("requester_username") or "").strip()
        directory = _canvas_bridge_dir(
            username, project_state_dir=ctx.state_dir,
            profile=f"freezone:{scope.agent_id}",
        )
        bridge = read_bridge_message(payload.bridge_key, bridge_dir=directory) or {}
    if not isinstance(bridge, dict):
        bridge = {}
    event = bridge.get("event") if isinstance(bridge, dict) else None
    if not had_pending:
        if bridge.get("key") != payload.bridge_key:
            return
        if not isinstance(event, dict):
            return
        pending = {
            "bridge_key": payload.bridge_key,
            "clarification_id": event.get("clarification_id"),
            "event": event,
        }
    elif not isinstance(pending.get("event"), dict) and isinstance(event, dict):
        pending = {**pending, "event": event}
    receipt = _verified_generation_interaction(
        run=run, pending=pending, result=result,
        project_id=ctx.project_id, canvas_id=payload.canvas_id,
        requester_user_id=ctx.requester_user_id,
    )
    if receipt is None:
        return
    receipt_scope, interaction = receipt
    await asyncio.to_thread(
        record_reference_parameter_submission, project_dir=ctx.state_dir,
        scope=receipt_scope, requirements_digest=run["generation_requirements_digest"],
        interaction=interaction,
    )


def _canvas_context_ui_event(
    payload: CanvasContextToolResultIn,
    resolved: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "canvas_context_result.v1",
        "type": "canvas_context_result",
        "canvas_id": resolved.get("canvas_id"),
        "bridge_key": payload.bridge_key,
        "result": {
            "ok": resolved.get("ok"),
            "tool_call_status": resolved.get("tool_call_status"),
            "canvas_context_status": resolved.get("canvas_context_status"),
            "responses": resolved.get("responses") or [],
            "errors": resolved.get("errors") or [],
            "message": resolved.get("message"),
        },
        "anchor_text_prefix": payload.anchor_text_prefix,
    }


@router.post("/chat/canvas-command-graph-delivery")
async def checkpoint_canvas_command_graph_delivery(
    payload: CanvasCommandToolResultIn,
    user: dict = Depends(get_api_user),
) -> dict[str, Any]:
    """Confirm the saved graph without resolving the command bridge or Agent turn."""
    if user.get("credential_kind") == "agent_session":
        raise HTTPException(403, "canvas graph receipts must come from the browser session")
    if payload.canvas_apply_status != "applied" or payload.followup or payload.cancelled:
        raise HTTPException(400, "graph delivery requires a nonterminal applied receipt")
    username = str(user["username"])
    project_state_dir = await _bridge_project_state_dir(user, payload)
    validate_bridge_receipt(
        payload.bridge_key.strip(),
        bridge_dir=_bridge_dir_for_pending_key(
            username, payload, project_state_dir=project_state_dir,
        ),
        username=username, project_id=payload.project_id, canvas_id=payload.canvas_id,
        agent_id=_freezone_agent_id_from_payload(payload), turn_id=payload.turn_id,
    )
    draft_receipt = _pending_workflow_draft_receipt(
        username, payload, project_state_dir=project_state_dir,
    )
    if draft_receipt is None:
        raise HTTPException(409, "approved workflow draft binding is unavailable")
    try:
        proof = await _record_workflow_draft_canvas_result(
            user=user, payload=payload, draft_receipt=draft_receipt,
            resolved={}, strict=True,
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc
    if proof is None:
        raise HTTPException(409, "workflow graph delivery was not recorded")
    return {"ok": True, "data": {
        "graph_state": proof["state"],
        "draft_id": draft_receipt["draft_id"],
        "revision": draft_receipt["revision"],
        "task_id": draft_receipt["task_id"],
        "canvas_revision": proof.get("canvas_revision"),
    }}


@router.post("/chat/canvas-command-tool-result")
async def resolve_canvas_command_tool_result(
    payload: CanvasCommandToolResultIn,
    user: dict = Depends(get_api_user),
) -> dict[str, Any]:
    from novelvideo.freezone import canvas_store

    if user.get("credential_kind") == "agent_session":
        raise HTTPException(
            403, "canvas execution receipts must come from the browser session"
        )
    username = str(user["username"])
    project_state_dir = await _bridge_project_state_dir(user, payload)
    if payload.followup:
        validate_bridge_receipt(payload.bridge_key.strip(),
            bridge_dir=_bridge_dir_for_pending_key(username, payload, project_state_dir=project_state_dir),
            username=username, project_id=payload.project_id, canvas_id=payload.canvas_id,
            agent_id=_freezone_agent_id_from_payload(payload), turn_id=payload.turn_id)
        payload.bridge_key = canonical_bridge_key(payload.bridge_key)
        project_id = str(payload.project_id or "").strip()
        canvas_id = str(payload.canvas_id or "").strip()
        turn_id = str(payload.turn_id or "").strip()
        if not all((payload.bridge_key.strip(), project_id, canvas_id, turn_id)):
            raise HTTPException(
                400, "background canvas result requires bridge and scope"
            )
        scope = ChatScope(
            kind="project",
            id=project_id,
            surface="freezone",
            canvas_id=canvas_id,
            agent_id=_freezone_agent_id_from_payload(payload),
        )
        project_ctx = await _project_context_for_scope(user, scope)
        store_scope = _chat_store_scope_for_project_context(scope, project_ctx)
        if payload.canvas_apply_status == "pending":
            instruction = (
                "The background canvas workflow is still syncing or reconciling its result. "
                "Do not claim the artifact is ready and do not rerun generation. "
                "Ask the user to check the canvas and workflow status later."
            )
        elif payload.canvas_apply_status in {"failed", "cancelled_by_user"}:
            instruction = (
                "The background canvas workflow did not finish successfully. "
                "Check the current canvas and workflow state before taking another action."
            )
        else:
            instruction = (
                "The background canvas workflow returned a result. "
                "Verify the current canvas output before claiming the artifact is ready."
            )
        await chat_store.append_message_async(
            username,
            store_scope,
            "agent_notification",
            f"[CANVAS_BACKGROUND_RESULT] {instruction} [/CANVAS_BACKGROUND_RESULT]",
            turn_id=turn_id,
            idempotency_key=f"canvas-background-result:{payload.bridge_key.strip()}",
        )
        return {
            "ok": True,
            "data": {"canvas_apply_status": payload.canvas_apply_status},
        }
    workflow_draft_receipt = _pending_workflow_draft_receipt(
        username, payload, project_state_dir=project_state_dir
    )
    native_commands = _pending_native_processing_commands(
        username, payload, project_state_dir=project_state_dir,
    )
    if payload.canvas_apply_status == "accepted" and payload.completion_owner == "host":
        scope = ChatScope(
            kind="project", id=str(payload.project_id or ""), surface="freezone",
            canvas_id=str(payload.canvas_id or ""),
            agent_id=_freezone_agent_id_from_payload(payload),
        )
        project_ctx = await _project_context_for_scope(user, scope)
        if project_ctx is None:
            raise HTTPException(409, "native processing project context is unavailable")
        if payload.workflow_run_id and not payload.operation_id and not payload.task_id:
            claimed_actions = _claimed_native_run_actions(payload.command_results)
            _validate_native_workflow_receipt(
                project_ctx, payload, native_commands,
                claimed_actions=claimed_actions or None,
            )
        else:
            saved_canvas = await asyncio.to_thread(
                canvas_store.read_canvas, project_ctx.state_dir, str(payload.canvas_id),
            )
            native_command = _approved_native_processing_receipt_command(
                native_commands, saved_canvas or {"nodes": []},
                node_id=str(payload.node_id or ""), action=str(payload.action or ""),
            )
            if native_command is None:
                raise HTTPException(409, "native processing command is unavailable")
            _validate_native_processing_receipt(project_ctx, payload, native_command)
    resolved = _resolve_canvas_command_tool_result_payload(
        payload, username=username, project_state_dir=project_state_dir
    )
    await _record_workflow_draft_canvas_result(
        user=user,
        payload=payload,
        draft_receipt=workflow_draft_receipt,
        resolved=resolved,
    )
    if resolved.get('ok') and any(
        item.get('type') == 'production_plan_command' for item in resolved.get('command_results', [])
    ):
        from novelvideo.freezone.remix.plan_revision_settlement import receive_canvas_plan_revisions
        from novelvideo.freezone.canvas_command_bridge import record_plan_revision_sync
        scope = ChatScope(kind='project', id=str(payload.project_id or ''), surface='freezone',
                          canvas_id=str(payload.canvas_id or ''), agent_id=_freezone_agent_id_from_payload(payload))
        project_ctx = await _project_context_for_scope(user, scope)
        if project_ctx is not None:
            directory = _bridge_dir_for_pending_key(username, payload, project_state_dir=project_state_dir)
            statuses = await asyncio.to_thread(receive_canvas_plan_revisions, project_ctx,
                bridge_key=payload.bridge_key, bridge_dir=directory, username=username,
                agent_id=scope.agent_id, turn_id=payload.turn_id)
            if statuses:
                resolved = await asyncio.to_thread(record_plan_revision_sync, payload.bridge_key,
                                                    statuses, bridge_dir=directory) or resolved
    if payload.cancelled or payload.canvas_apply_status == "cancelled_by_user":
        await _close_canvas_command_worker(username, payload)
    return {"ok": True, "data": resolved}


@router.post("/chat/canvas-context-tool-result")
async def resolve_canvas_context_tool_result(
    payload: CanvasContextToolResultIn,
    user: dict = Depends(get_api_user),
) -> dict[str, Any]:
    username = str(user["username"])
    resolved = _resolve_canvas_context_tool_result_payload(
        payload,
        username=username,
        project_state_dir=await _bridge_project_state_dir(user, payload),
    )
    context_turn_id = str(payload.turn_id or "").strip()
    project_id = str(resolved.get("project_id") or payload.project_id or "").strip()
    canvas_id = str(resolved.get("canvas_id") or payload.canvas_id or "").strip()
    if context_turn_id and project_id and canvas_id:
        try:
            scope = ChatScope(
                kind="project",
                id=project_id,
                surface="freezone",
                canvas_id=canvas_id,
                agent_id=_freezone_agent_id_from_payload(payload),
            )
            project_ctx = await _project_context_for_scope(user, scope)
            await chat_store.append_ui_event_async(
                username,
                _chat_store_scope_for_project_context(scope, project_ctx),
                context_turn_id,
                _canvas_context_ui_event(payload, resolved),
            )
        except Exception:
            logger.exception("failed to persist canvas.context.result ui event")
    return {"ok": True, "data": resolved}


@router.post("/chat/skill-studio-tool-result")
async def resolve_skill_studio_tool_result(
    payload: SkillStudioToolResultIn,
    user: dict = Depends(get_api_user),
) -> dict[str, Any]:
    username = str(user["username"])
    logger.info(
        "received skill_studio.result via http %s",
        _skill_studio_result_log_fields(payload, username=username),
    )
    # Saving is persistence only. Generation charges are admitted before the
    # model call and settled by the corresponding durable product operation.
    resolved = _resolve_skill_studio_tool_result_payload(
        payload,
        username=username,
        project_state_dir=await _bridge_project_state_dir(user, payload),
    )
    logger.info(
        "resolved skill_studio.result via http bridge_key=%s action=%s status=%s ok=%s saved=%s",
        payload.bridge_key,
        payload.action,
        resolved.get("skill_studio_status"),
        resolved.get("ok"),
        resolved.get("saved_to_catalog"),
    )
    try:
        await _persist_skill_studio_result_ui_event(
            user=user,
            username=username,
            payload=payload,
            resolved=resolved,
        )
    except Exception:
        logger.exception("failed to persist skill studio result ui event")
    return {"ok": True, "data": resolved}


@router.get("/chat/agent-capability-price-reference")
async def agent_capability_price_reference(
    _user: dict = Depends(get_api_user),
) -> dict[str, Any]:
    """Return the user-facing scope of Agent-only billing, excluding NewAPI media costs."""
    if isinstance(get_usage_meter(), NoOpUsageMeter):
        return {"ok": True, "data": {"enabled": False, "items": [], "note": ""}}
    return {
        "ok": True,
        "data": {
            "enabled": True,
            "items": list(AGENT_PRODUCT_PRICE_REFERENCE),
            "note": (
                "仅计算成功交付的 Workflow/Recipe 结果或定义；"
                "文字、图片、音频和视频仍通过各自任务独立计费。"
            ),
        },
    }


@router.post("/chat/clarification-tool-result")
async def resolve_clarification_tool_result(
    payload: ClarificationToolResultIn,
    user: dict = Depends(get_api_user),
) -> dict[str, Any]:
    username = str(user["username"])
    project_state_dir = await _bridge_project_state_dir(user, payload)
    resolved = await _resolve_clarification_with_reference_persistence(
        payload=payload, user=user, username=username,
        project_state_dir=project_state_dir,
    )
    try:
        await _persist_clarification_result_ui_event(
            user=user, username=username, payload=payload
        )
    except Exception:
        logger.exception("failed to persist clarification result ui event")
    return {"ok": True, "data": resolved}


def _is_websocket_disconnected_runtime_error(exc: RuntimeError) -> bool:
    message = str(exc)
    return (
        "WebSocket is not connected" in message
        or 'Cannot call "receive" once a disconnect message has been received.'
        in message
    )


async def _receive_bridge_results_during_turn(
    *,
    websocket: WebSocket,
    user: dict[str, Any],
    username: str,
) -> None:
    while True:
        try:
            raw = await websocket.receive_json()
        except asyncio.CancelledError:
            raise
        except RuntimeError as exc:
            if _is_websocket_disconnected_runtime_error(exc):
                return
            raise
        except WebSocketDisconnect:
            return

        event_type = str(raw.get("type") or "")
        if event_type == "canvas.command.result":
            payload = CanvasCommandToolResultIn.model_validate(raw)
            await resolve_canvas_command_tool_result(payload, user)
            continue

        if event_type == "canvas.context.result":
            payload = CanvasContextToolResultIn.model_validate(raw)
            _resolve_canvas_context_tool_result_payload(
                payload,
                username=username,
                project_state_dir=await _bridge_project_state_dir(user, payload),
            )
            continue

        if event_type == "skill_studio.result":
            payload = SkillStudioToolResultIn.model_validate(raw)
            resolved = _resolve_skill_studio_tool_result_payload(
                payload,
                username=username,
                project_state_dir=await _bridge_project_state_dir(user, payload),
            )
            try:
                await _persist_skill_studio_result_ui_event(
                    user=user,
                    username=username,
                    payload=payload,
                    resolved=resolved,
                )
            except Exception:
                logger.exception("failed to persist skill studio result ui event")
            continue

        if event_type == "assistant.clarification.result":
            payload = ClarificationToolResultIn.model_validate(raw)
            project_state_dir = await _bridge_project_state_dir(user, payload)
            resolved = await _resolve_clarification_with_reference_persistence(
                payload=payload, user=user, username=username,
                project_state_dir=project_state_dir,
            )
            try:
                await _persist_clarification_result_ui_event(
                    user=user, username=username, payload=payload
                )
            except Exception:
                logger.exception("failed to persist clarification result ui event")
            continue

        logger.debug("ignoring websocket event during active chat turn: %s", event_type)


async def _authenticate_ws(websocket: WebSocket) -> dict[str, Any]:
    if websocket.headers.get("X-API-Key"):
        raise HTTPException(status_code=401, detail="unsupported credential")
    if any(name in websocket.query_params for name in UNSUPPORTED_QUERY_CREDENTIALS):
        raise HTTPException(status_code=401, detail="unsupported credential")

    bearer = websocket.headers.get("Authorization", "").strip()
    if bearer:
        token = (
            bearer.partition(" ")[2].strip()
            if bearer.lower().startswith("bearer ")
            else ""
        )
        if not token:
            raise HTTPException(status_code=401, detail="invalid authorization")
        return await _verify_agent_bearer(token)

    cookie_value = websocket.cookies.get(AUTH_COOKIE_NAME)
    return await _verify_browser_session(cookie_value)


def _scope_for_authenticated_user(user: dict[str, Any]) -> ChatScope:
    if user.get("credential_kind") != "agent_session":
        return ChatScope(kind="home")
    kind = str(user.get("current_scope_kind") or "home")
    project_id = user.get("current_project_id")
    if kind == "project" and project_id:
        return ChatScope(kind="project", id=str(project_id))
    if kind == "home":
        return ChatScope(kind="home")
    raise HTTPException(status_code=403, detail="agent scope unavailable")


def _enforce_agent_chat_scope(
    user: dict[str, Any],
    scope: ChatScope,
    *,
    require_write: bool,
) -> None:
    if user.get("credential_kind") != "agent_session":
        return
    current = _scope_for_authenticated_user(user)
    if current.kind != scope.kind or current.id != scope.id:
        raise HTTPException(status_code=403, detail="agent scope mismatch")
    if require_write and set(user.get("scopes") or []).isdisjoint(AGENT_WRITE_SCOPES):
        raise HTTPException(status_code=403, detail="agent write scope missing")


async def _reauthenticate_ws_event(
    websocket: WebSocket,
    *,
    original_user: dict[str, Any],
    scope: ChatScope,
    require_write: bool,
) -> dict[str, Any]:
    fresh = await _authenticate_ws(websocket)
    original_id = str(original_user.get("id") or original_user.get("user_id") or "")
    fresh_id = str(fresh.get("id") or fresh.get("user_id") or "")
    original_kind = original_user.get("credential_kind") or "browser_session"
    fresh_kind = fresh.get("credential_kind") or "browser_session"
    if not original_id or fresh_id != original_id or fresh_kind != original_kind:
        raise HTTPException(status_code=403, detail="principal changed")
    _enforce_agent_chat_scope(fresh, scope, require_write=require_write)
    return fresh


async def _close_ws_unauthorized(websocket: WebSocket) -> None:
    await _send_json_best_effort(
        websocket, {"type": "error", "message": "unauthorized"}
    )
    await websocket.close(code=1008)


async def _close_ws_setup_error(websocket: WebSocket, exc: Exception) -> None:
    if isinstance(exc, HTTPException) and exc.status_code in (401, 403):
        await _close_ws_unauthorized(websocket)
        return
    logger.error("chat websocket setup failed error_type=%s", type(exc).__name__)
    message = (
        "聊天服务暂时不可用，请稍后重试。"
        if isinstance(exc, HTTPException)
        else _user_facing_chat_error(exc)
    )
    await _send_json_best_effort(websocket, {"type": "error", "message": message})
    await websocket.close(code=1011)


def _scope_from_model(model: ChatScopePayload | None) -> ChatScope:
    return ChatScope.from_payload(model.model_dump() if model else None)


def _should_prewarm_on_ws_connect(scope: ChatScope) -> bool:
    return scope.kind != "home"


def _completion_text_or_existing(event_text: object, existing: str) -> str:
    final_text = str(event_text or "").strip()
    if not final_text or final_text.startswith("stop="):
        return existing
    if final_text.lower() == "(hermes timed out)" and existing.strip():
        return existing
    if existing.strip() and _is_completion_notice(final_text):
        if final_text in existing:
            return existing
        return f"{existing.rstrip()}\n\n{final_text}"
    return final_text


def _is_completion_notice(text: str) -> bool:
    return text in {
        "当前任务已开始处理。请稍后让我查看当前任务进度，或在任务完成后再继续下一步。",
        "刚才这一步没有成功启动任务。请先根据返回的错误补齐前置条件；如果是配音缺少声线，可以到「虾塘」上传或录制缺失声线后再继续。",
    }


def _message_content(message: object) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    text = message.get("text")
    if isinstance(text, str):
        return text.strip()
    return ""


def _attachment_context_block(attachments: list[ChatAttachmentIn]) -> str:
    if not attachments:
        return ""
    lines = [
        "[CHAT_ATTACHMENTS]",
        "The browser sent these attachment records with the user message.",
    ]
    for index, attachment in enumerate(attachments, 1):
        lines.append("")
        lines.append(f"{index}. fileName={attachment.fileName or ''}")
        lines.append(f"   type={attachment.type or ''}")
        lines.append(f"   mimeType={attachment.mimeType or ''}")
        if attachment.fileSize is not None:
            lines.append(f"   fileSize={attachment.fileSize}")
        if attachment.url:
            lines.append(f"   url={attachment.url}")
        if attachment.path:
            lines.append(f"   path={attachment.path}")
        if attachment.content:
            lines.append("   content=present")
    lines.append("[/CHAT_ATTACHMENTS]")
    return "\n".join(lines)


def _text_with_attachment_context(
    text: str, attachments: list[ChatAttachmentIn]
) -> str:
    block = _attachment_context_block(attachments)
    return f"{text}\n\n{block}" if block else text


def _attachment_payloads(attachments: list[ChatAttachmentIn]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for attachment in attachments:
        payload = attachment.model_dump(exclude_none=True)
        if payload:
            payloads.append(payload)
    return payloads


def _should_emit_final_text(final_text: str, last_sent_text: str) -> bool:
    final = " ".join(str(final_text or "").split())
    last = " ".join(str(last_sent_text or "").split())
    return bool(final) and final != last


def _tool_display_payload(text: object, name: object = None) -> tuple[str, str]:
    raw = str(text or "").strip()
    tool_name = str(name or "").strip()
    lines = raw.splitlines()
    if lines and lines[0].lstrip().startswith("→ "):
        first = lines[0].lstrip()[2:].strip()
        head, sep, tail = first.partition(":")
        if sep and head.strip():
            tool_name = tool_name or head.strip()
            lines[0] = tail.strip()
        else:
            tool_name = tool_name or (first.split()[0].strip() if first else "")
            lines = lines[1:]
    body = "\n".join(line for line in lines if line.strip()).strip()
    return tool_name or "agent.tool", body


def _tool_result_payload(
    text: object, structured: object | None = None
) -> dict[str, Any]:
    body = str(text or "")
    payload: dict[str, Any] = {"text": body}
    if structured is not None:
        payload["json"] = structured
        return payload
    stripped = body.strip()
    if stripped.startswith(("{", "[")):
        try:
            payload["json"] = json.loads(stripped)
        except json.JSONDecodeError:
            pass
    return payload


async def _project_context_for_scope(
    user: dict[str, Any], scope: ChatScope
) -> ProjectContext | None:
    if scope.kind not in {"project", "freezone"} or not scope.id:
        return None
    return await resolve_project_context(
        user=user,
        project_id=str(scope.id),
        required_role="viewer",
    )


async def _requester_user_id_for_chat(user: dict[str, Any], scope: ChatScope) -> str:
    if scope.kind in {"project", "freezone"}:
        project_ctx = await _project_context_for_scope(user, scope)
        if project_ctx is not None and project_ctx.requester_user_id:
            return project_ctx.requester_user_id
    return await user_id_from_api_user(user)


def _assistant_surface_code(scope: ChatScope) -> str:
    return "freezone_assistant" if _is_freezone_scope(scope) else "assistant"


async def _assistant_surface_access(
    *,
    user: dict[str, Any],
    scope: ChatScope,
) -> dict[str, Any] | None:
    user_id = await _requester_user_id_for_chat(user, scope)
    surface_code = _assistant_surface_code(scope)
    items = await get_product_surface_access().get_effective_access(user_id)
    return next(
        (item for item in items if str(item.get("surface_code") or "") == surface_code),
        None,
    )


async def _assistant_surface_available(
    *,
    user: dict[str, Any],
    scope: ChatScope,
) -> bool:
    access = await _assistant_surface_access(user=user, scope=scope)
    return bool(access and access.get("available") is True)


async def _prewarm_chat_scope_if_available(
    *,
    user: dict[str, Any],
    username: str,
    scope: ChatScope,
) -> bool:
    # Product Surface only controls the new Freezone assistant. Existing
    # Home/Project scopes retain staging's unconditional prewarm behavior.
    if _is_freezone_scope(scope) and not await _assistant_surface_available(
        user=user,
        scope=scope,
    ):
        return False
    await chat_service.prewarm_chat_backend(
        username,
        project=scope.id if scope.kind in {"project", "freezone"} else None,
        surface="freezone" if _is_freezone_scope(scope) else None,
        agent_id=scope.agent_id if _is_freezone_scope(scope) else None,
        canvas_id=scope.canvas_id if _is_freezone_scope(scope) else None,
    )
    return True


async def _require_ai_assistant_access(
    *,
    user: dict[str, Any],
    scope: ChatScope,
) -> None:
    # Product Surface is an authorization boundary only for the newly added
    # Freezone assistant. Existing Home/Project Director chat keeps staging's
    # credit-only admission semantics.
    if _is_freezone_scope(scope):
        access = await _assistant_surface_access(user=user, scope=scope)
        if not access or access.get("available") is not True:
            message = str(
                (access or {}).get("unavailable_message") or "虾画功能暂未开放"
            )
            raise HTTPException(status_code=403, detail=message)
    user_id = await _requester_user_id_for_chat(user, scope)
    await get_usage_meter().require_feature_credit_balance(
        user_id=user_id,
        feature_key=AI_ASSISTANT_CHAT_FEATURE_KEY,
        project_id=str(scope.id or "") if scope.kind == "project" else "",
        resource_kind="chat",
        metadata={"scope": scope.to_dict()},
    )


async def _history(
    username: str,
    scope: ChatScope,
    *,
    project_ctx: ProjectContext | None = None,
) -> list[dict[str, Any]]:
    if scope.kind == "project" and (scope.surface or "director") == "director":
        return await asyncio.to_thread(
            chat_service.list_messages,
            username,
            str(scope.id),
            project_dir=project_ctx.output_dir if project_ctx is not None else None,
            project_state_dir=(
                project_ctx.state_dir if project_ctx is not None else None
            ),
        )
    if _is_freezone_scope(scope):
        return await chat_store.list_messages_async(
            username,
            _chat_store_scope_for_project_context(scope, project_ctx),
        )
    return await chat_store.list_messages_async(username, scope)


async def _send_scope_changed(
    websocket: WebSocket,
    user: dict[str, Any],
    username: str,
    scope: ChatScope,
) -> ChatScope | None:
    try:
        project_ctx = await _project_context_for_scope(user, scope)
    except HTTPException as exc:
        if scope.kind not in {"project", "freezone"} or exc.status_code != 404:
            raise
        scope = ChatScope(kind="home")
        project_ctx = None
        if not await _send_json_best_effort(
            websocket,
            {"type": "error", "message": "项目不存在或已删除，已切回首页聊天。"},
        ):
            return None
    if not await _send_json_best_effort(
        websocket,
        {
            "type": "scope.changed",
            "scope": scope.to_dict(),
            "history": await _history(username, scope, project_ctx=project_ctx),
            "busy": chat_service.chat_run_lock_is_active(
                username, _chat_run_lock_project_for_scope(scope)
            ),
        },
    ):
        return None
    return scope


async def _send_json_best_effort(
    websocket: WebSocket,
    payload: dict[str, Any],
    send_lock: asyncio.Lock | None = None,
) -> bool:
    try:
        if send_lock is None:
            await websocket.send_json(payload)
        else:
            async with send_lock:
                await websocket.send_json(payload)
        return True
    except Exception as exc:
        # 发送失败必须留痕。这里以前是静默 `return False`，调用方也不看返回值，于是
        # 「终态帧根本没送出去」在线上完全不可观测——CORE-INF-02 的排查有一整天花在
        # 后端找一个不存在的缺口上，就是因为这条路径无声。
        # 脱敏：只记帧类型与轮次标识，payload 里可能带用户内容，不入日志。
        logger.warning(
            "dropped chat ws frame type=%s turn_id=%s: %s",
            payload.get("type"),
            payload.get("turn_id"),
            exc.__class__.__name__,
        )
        return False


async def _chat_heartbeat(
    websocket: WebSocket,
    *,
    scope: ChatScope,
    turn_id: str,
    send_lock: asyncio.Lock,
    disconnected: asyncio.Event | None = None,
    interval_seconds: float = 10.0,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        sent = await _send_json_best_effort(
            websocket,
            {"type": "chat.ping", "turn_id": turn_id, "scope": scope.to_dict()},
            send_lock,
        )
        if not sent:
            if disconnected is not None:
                disconnected.set()
            return


async def _interrupt_agent_on_disconnect(
    disconnected: asyncio.Event,
    *,
    runtime_backend: str,
    username: str,
    project: str,
    agent_profile: str,
    runtime_ids: dict[str, str | None],
) -> None:
    await disconnected.wait()
    # A socket can disappear while the runtime is still starting. Give the
    # matching turn a short window to publish its IDs, but never fall back to
    # a username-wide shutdown.
    for _attempt in range(100):
        thread_id = str(runtime_ids.get("thread_id") or "").strip()
        turn_id = str(runtime_ids.get("turn_id") or "").strip()
        if thread_id and (runtime_backend == "hermes" or turn_id):
            break
        await asyncio.sleep(0.05)
    else:
        return
    try:
        if runtime_backend == "hermes":
            from novelvideo.chat.hermes_pool import pool as hermes_pool

            await hermes_pool.close_user_thread(
                username,
                agent_profile,
                thread_id,
            )
        else:
            await chat_service.interrupt_chat_turn(
                username,
                project,
                thread_id,
                turn_id,
                backend=runtime_backend,
            )
    except Exception:
        logger.warning("failed to interrupt disconnected chat turn", exc_info=True)


async def _sync_running_agent_scope(username: str, scope: ChatScope) -> None:
    try:
        from novelvideo.chat.hermes_pool import pool as hermes_pool

        await hermes_pool.set_scope_for_user(
            username,
            agent_profile=(
                _freezone_agent_profile(scope) if _is_freezone_scope(scope) else "main"
            ),
            scope_kind="project" if _is_freezone_scope(scope) else scope.kind,
            project_id=scope.id if scope.kind in {"project", "freezone"} else None,
        )
    except Exception:
        # Scope switching should not spawn or break the UI if Hermes is absent.
        return


def _load_pending_canvas_command(path: Any) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    commands = payload.get("commands")
    if not isinstance(commands, list) or not commands:
        return None
    envelope = payload.get("envelope")
    if not isinstance(envelope, dict):
        envelope = {
            "schema_version": "canvas_chat_commands.v1",
            "canvas_id": payload.get("canvas_id"),
            "commands": commands,
        }
    if not isinstance(envelope.get("commands"), list) or not envelope.get("commands"):
        return None
    return {
        "key": str(payload.get("key") or path.name.removesuffix(".pending.json")),
        "project_id": payload.get("project_id"),
        "canvas_id": payload.get("canvas_id") or envelope.get("canvas_id"),
        "envelope": envelope,
    }


def _pending_canvas_command_allows_external_poll(envelope: dict[str, Any]) -> bool:
    # Polling is a reconnect fallback for Codex/external MCP commands only.
    # Hermes already streams the same bridge item over its live websocket;
    # admitting every envelope here would duplicate the established Hermes path.
    return envelope.get("external_mcp_command") is True


@router.post("/chat/pending-interactions")
async def pull_pending_interactions(
    payload: PendingInteractionsIn, user: dict = Depends(get_api_user)
):
    """Authenticated browser recovery; request claims and receipts stay durable."""
    if user.get("credential_kind", "browser_session") not in {
        "browser_session",
        "user_session",
    }:
        raise HTTPException(403, "interaction claims require a human browser session")
    from novelvideo.api.routes.reference_director import _context
    from novelvideo.freezone.canvas_command_bridge import (
        heartbeat_bridge_browser,
        renew_browser_owned_messages,
        list_browser_owned_messages,
        list_scope_bridge_receipts,
    )
    from novelvideo.chat.canvas_tool_session import _FRAMES

    ctx, _, _ = await _context(
        payload.project_id, payload.canvas_id, user, role="viewer"
    )
    require_project_home_node(ctx, operation="browser interaction recovery")
    username = str(user["username"])
    scope = ChatScope(
        "project",
        ctx.project_id,
        "freezone",
        payload.canvas_id,
        payload.agent_id,
        str(ctx.state_dir),
    )
    binding = {
        "username": username,
        "project_id": ctx.project_id,
        "canvas_id": payload.canvas_id,
        "agent_id": payload.agent_id,
    }
    # Browser IDs cannot collide between actors/scopes in a shared bridge directory.
    from novelvideo.chat.live_events import browser_consumer_id

    consumer = browser_consumer_id(
        username=username, scope=scope, browser_id=payload.browser_id
    )
    frames = []
    receipts = []
    for directory in _candidate_canvas_bridge_dirs_for_scope(username, scope):
        receipts.extend(
            list_scope_bridge_receipts(binding=binding, bridge_dir=directory)
        )
        heartbeat_bridge_browser(
            consumer_id=consumer, binding=binding, bridge_dir=directory
        )
        renew_browser_owned_messages(consumer_id=consumer, bridge_dir=directory)
        owned = list_browser_owned_messages(consumer_id=consumer, bridge_dir=directory)
        owned_keys = {message["key"] for message in owned}
        for pending in owned + list_pending_bridge_messages(
            project_id=ctx.project_id, canvas_id=payload.canvas_id, bridge_dir=directory, producer_binding=binding
        ):
            origin = pending.get("producer_binding")
            if (
                not isinstance(origin, dict)
                or {k: origin.get(k) for k in binding} != binding
                or not origin.get("turn_id")
            ):
                continue
            key, kind = pending["key"], pending["kind"]
            if kind not in _FRAMES:
                continue
            token = (
                pending.get("execution_token")
                if key in owned_keys
                else uuid.uuid4().hex
            )
            if key not in owned_keys and not mark_bridge_message_delivered(
                key,
                consumer_id=consumer,
                binding=origin,
                execution_token=token,
                bridge_dir=directory,
            ):
                continue
            content_key = (
                "envelope" if kind in {"canvas_command", "canvas_context"} else "event"
            )
            frame = {
                "type": _FRAMES[kind],
                "scope": scope.to_dict(),
                "project_id": ctx.project_id,
                "canvas_id": payload.canvas_id,
                "agent_id": payload.agent_id,
                "turn_id": origin["turn_id"],
                "bridge_key": f"{key}.executor.{token}",
                "request_id": key,
                content_key: pending[content_key],
            }
            frames.append(frame)
            if content_key == "event" and key not in owned_keys:
                await chat_store.append_ui_event_async(
                    username,
                    scope,
                    origin["turn_id"],
                    {**pending[content_key], **origin, "bridge_key": key},
                )
    try:
        recovery = await asyncio.to_thread(
            chat_store.pull_recovery,
            username,
            scope,
            cursor=payload.cursor,
            event_cursor=payload.event_cursor,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "data": {**recovery, "frames": frames, "receipts": receipts}}


@router.post("/chat/pending-canvas-commands")
async def list_pending_canvas_commands(
    payload: PendingCanvasCommandsIn,
    user: dict = Depends(get_api_user),
) -> dict[str, Any]:
    username = str(user["username"])
    project_id = payload.project_id.strip()
    canvas_id = payload.canvas_id.strip()
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id is required")
    if not canvas_id:
        raise HTTPException(status_code=400, detail="canvas_id is required")

    project_state_dir = await _bridge_project_state_dir(user, payload)
    requested_agent_ids = [
        str(value).strip() for value in payload.agent_ids if str(value).strip()
    ]
    if not requested_agent_ids:
        requested_agent_ids = [str(payload.agent_id or "main").strip() or "main"]
    agent_ids = list(dict.fromkeys(requested_agent_ids))[:50]
    seen_keys = {str(key) for key in payload.seen_keys if str(key).strip()}
    frames: list[dict[str, Any]] = []
    bridge_targets: list[tuple[str, Any]] = []
    seen_bridge_dirs: set[str] = set()
    for agent_id in agent_ids:
        scope = ChatScope(
            kind="project",
            id=project_id,
            surface="freezone",
            canvas_id=canvas_id,
            agent_id=agent_id,
            state_dir=str(project_state_dir) if project_state_dir is not None else None,
        )
        for bridge_dir in _candidate_canvas_bridge_dirs_for_scope(username, scope):
            marker = str(bridge_dir)
            if marker in seen_bridge_dirs:
                continue
            seen_bridge_dirs.add(marker)
            bridge_targets.append((agent_id, bridge_dir))
    for agent_id, bridge_dir in bridge_targets:
        try:
            durable_pending = list_pending_bridge_messages(
                project_id=project_id,
                canvas_id=canvas_id,
                kinds={"canvas_command"},
                limit=10,
                bridge_dir=bridge_dir,
            )
        except Exception:
            durable_pending = []
        for pending in durable_pending:
            origin = pending.get("producer_binding")
            if origin and (origin.get("username") != username or origin.get("agent_id") != agent_id):
                continue
            if pending.get("execution_token"):
                # Owned browser requests cannot be claimed by external polling.
                continue
            key = str(pending.get("key") or "")
            if not key or key in seen_keys:
                continue
            envelope = pending.get("envelope")
            if not isinstance(envelope, dict):
                continue
            if not _pending_canvas_command_allows_external_poll(envelope):
                continue
            if not mark_bridge_message_delivered(
                key,
                consumer_id=f"poll:{username}:{agent_id}",
                bridge_dir=bridge_dir,
            ):
                continue
            seen_keys.add(key)
            frames.append(
                {
                    "type": "canvas.command",
                    "turn_id": str(envelope.get("turn_id") or "").strip()
                    or f"external-agent:{key}",
                    "canvas_id": envelope.get("canvas_id") or canvas_id,
                    "agent_id": str(envelope.get("agent_id") or agent_id),
                    "bridge_key": key,
                    "envelope": envelope,
                    "source": "pending_canvas_bridge",
                }
            )
            if len(frames) >= 10:
                return {"ok": True, "data": {"frames": frames}}
        try:
            pending_paths = sorted(
                bridge_dir.glob("*.pending.json"),
                key=lambda item: item.stat().st_mtime,
            )
        except Exception:
            continue
        for path in pending_paths:
            key = path.name.removesuffix(".pending.json")
            if key in seen_keys:
                continue
            if not _is_unmigrated_legacy_bridge_file(
                bridge_dir=bridge_dir,
                key=key,
            ):
                continue
            # A result file is authoritative. The browser may have applied the
            # command just before a reconnect, while this polling endpoint is
            # still scanning the old pending file. Never re-emit that command
            # as a fresh approval card.
            if _drop_resolved_pending_bridge_file(
                bridge_dir=bridge_dir,
                key=key,
                pending_path=path,
            ):
                continue
            pending = _load_pending_canvas_command(path)
            if pending is None:
                continue
            if pending.get("project_id") and pending.get("project_id") != project_id:
                continue
            if pending.get("canvas_id") and pending.get("canvas_id") != canvas_id:
                continue
            envelope = pending["envelope"]
            # The canvas UI consumes bridge frames through the same approval
            # path as websocket commands. Dedupe is handled by seen_keys.
            if not _pending_canvas_command_allows_external_poll(envelope):
                continue
            frames.append(
                {
                    "type": "canvas.command",
                    "turn_id": str(envelope.get("turn_id") or "").strip()
                    or f"external-agent:{key}",
                    "canvas_id": envelope.get("canvas_id") or canvas_id,
                    "agent_id": str(envelope.get("agent_id") or agent_id),
                    "bridge_key": key,
                    "envelope": envelope,
                    "source": "pending_canvas_bridge",
                }
            )
            if len(frames) >= 10:
                return {"ok": True, "data": {"frames": frames}}
    return {"ok": True, "data": {"frames": frames}}


def _load_pending_canvas_context(path: Any) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    requests = payload.get("requests")
    if not isinstance(requests, list) or not requests:
        return None
    envelope = payload.get("envelope")
    if not isinstance(envelope, dict):
        envelope = {
            "schema_version": "canvas_context_request.v1",
            "canvas_id": payload.get("canvas_id"),
            "requests": requests,
        }
    if envelope.get("schema_version") != "canvas_context_request.v1":
        return None
    if not isinstance(envelope.get("requests"), list) or not envelope.get("requests"):
        return None
    return {
        "key": str(payload.get("key") or path.name.removesuffix(".pending.json")),
        "project_id": payload.get("project_id"),
        "canvas_id": payload.get("canvas_id") or envelope.get("canvas_id"),
        "envelope": envelope,
    }


_PENDING_CANVAS_COMMAND_STALE_SECONDS = 75.0
_PENDING_CANVAS_CONTEXT_STALE_SECONDS = 45.0


def _pending_canvas_command_timed_out(
    path: Any, *, stale_seconds: float = 45.0
) -> bool:
    try:
        return (time.time() - path.stat().st_mtime) >= stale_seconds
    except Exception:
        return False


def _drop_resolved_pending_bridge_file(
    *, bridge_dir: Any, key: str, pending_path: Any
) -> bool:
    if not (bridge_dir / f"{key}.result.json").exists():
        return False
    with contextlib.suppress(FileNotFoundError):
        pending_path.unlink()
    return True


def _is_unmigrated_legacy_bridge_file(*, bridge_dir: Any, key: str) -> bool:
    """Allow JSON fallback only when no durable row owns this bridge key."""
    try:
        if bridge_message_exists(key, bridge_dir=bridge_dir):
            return False
        pending_path = bridge_dir / f"{key}.pending.json"
        payload = json.loads(pending_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return False
        # This guard is shared by all legacy bridge watchers. Read the raw
        # payload here so canvas context (requests), Skill Studio (event), and
        # clarification (event) mirrors are not rejected by the stricter
        # canvas-command parser before their own watcher can validate them.
        kind = str(payload.get("kind") or "canvas_command")
        ttl = BRIDGE_PENDING_TTL_SECONDS.get(kind, 10 * 60)
        try:
            created_at = float(payload.get("created_at"))
        except (TypeError, ValueError):
            created_at = pending_path.stat().st_mtime
        if created_at + ttl <= time.time():
            with contextlib.suppress(FileNotFoundError):
                pending_path.unlink()
            return False
        return True
    except Exception:
        # SQLite is the source of truth. If its state cannot be checked, do not
        # risk bypassing an active delivery lease through the legacy mirror.
        return False


def _resolve_stale_pending_canvas_command(
    *,
    bridge_dir: Any,
    path: Any,
    key: str,
    pending: dict[str, Any],
    scope: ChatScope,
    turn_id: str,
) -> bool:
    if not _pending_canvas_command_timed_out(
        path,
        stale_seconds=_PENDING_CANVAS_COMMAND_STALE_SECONDS,
    ):
        return False
    envelope = pending.get("envelope") if isinstance(pending, dict) else None
    commands = envelope.get("commands") if isinstance(envelope, dict) else None
    resolve_canvas_command(
        key,
        {
            "ok": False,
            "turn_id": turn_id,
            "tool_call_status": "failed",
            "canvas_apply_status": "timeout",
            "applied": False,
            "cancelled": True,
            "errors": ["Timed out waiting for frontend canvas command result."],
            "applied_count": 0,
            "opened_ui_actions": [],
            "created_node_ids": [],
            "command_results": [],
            "project_id": pending.get("project_id") or scope.id,
            "canvas_id": pending.get("canvas_id") or scope.canvas_id,
            "message": "Canvas command timed out before the frontend reported a result.",
            "user_message": "画布操作等待超时，已自动取消，没有应用新的画布变更。",
            "agent_instruction": (
                "Do not claim success. Tell the user the canvas command timed out and ask "
                "them to retry after checking the canvas connection."
            ),
        },
        bridge_dir=bridge_dir,
    )
    logger.warning(
        "auto-resolved stale canvas.command pending bridge_key=%s turn_id=%s canvas_id=%s commands=%s",
        key,
        turn_id,
        pending.get("canvas_id") or scope.canvas_id,
        len(commands) if isinstance(commands, list) else 0,
    )
    return True


def _resolve_stale_pending_canvas_context(
    *,
    bridge_dir: Any,
    path: Any,
    key: str,
    pending: dict[str, Any],
    scope: ChatScope,
    turn_id: str,
) -> bool:
    if not _pending_canvas_command_timed_out(
        path,
        stale_seconds=_PENDING_CANVAS_CONTEXT_STALE_SECONDS,
    ):
        return False
    envelope = pending.get("envelope") if isinstance(pending, dict) else None
    requests = envelope.get("requests") if isinstance(envelope, dict) else None
    resolve_canvas_context(
        key,
        {
            "ok": False,
            "turn_id": turn_id,
            "tool_call_status": "failed",
            "canvas_context_status": "timeout",
            "responses": [],
            "errors": ["Timed out waiting for frontend canvas context response."],
            "project_id": pending.get("project_id") or scope.id,
            "canvas_id": pending.get("canvas_id") or scope.canvas_id,
            "message": "Canvas context request timed out before the frontend reported a result.",
            "user_message": "读取画布上下文等待超时，请确认画布页面仍然打开后重试。",
            "agent_instruction": (
                "Do not wait indefinitely. Tell the user the canvas context request timed out "
                "and ask them to retry after checking the canvas connection."
            ),
        },
        bridge_dir=bridge_dir,
    )
    logger.warning(
        "auto-resolved stale canvas.context pending bridge_key=%s turn_id=%s canvas_id=%s requests=%s",
        key,
        turn_id,
        pending.get("canvas_id") or scope.canvas_id,
        len(requests) if isinstance(requests, list) else 0,
    )
    return True


def _load_pending_skill_studio_event(path: Any) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("kind") != "skill_studio_event":
        return None
    event = payload.get("event")
    if not isinstance(event, dict):
        return None
    event_type = str(event.get("type") or "").strip()
    if event_type not in {
        "skill_studio.questions",
        "skill_studio.draft",
        "skill_studio.status",
    }:
        return None
    if not str(event.get("skill_studio_session_id") or "").strip():
        return None
    return {
        "key": str(payload.get("key") or path.name.removesuffix(".pending.json")),
        "project_id": payload.get("project_id"),
        "canvas_id": payload.get("canvas_id"),
        "event": event,
    }


def _load_pending_clarification_event(path: Any) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("kind") != "clarification_event":
        return None
    event = payload.get("event")
    if not isinstance(event, dict):
        return None
    if str(event.get("type") or "").strip() != "assistant.clarification.request":
        return None
    if not str(event.get("clarification_id") or "").strip():
        return None
    return {
        "key": str(payload.get("key") or path.name.removesuffix(".pending.json")),
        "project_id": payload.get("project_id"),
        "canvas_id": payload.get("canvas_id"),
        "event": event,
    }


async def _watch_pending_canvas_commands(
    *,
    websocket: WebSocket,
    username: str,
    scope: ChatScope,
    turn_id: str,
    send_lock: asyncio.Lock,
    emitted_bridge_keys: set[str],
    started_at: float,
) -> None:
    if not _is_freezone_scope(scope):
        return
    bridge_dirs = _candidate_canvas_bridge_dirs_for_scope(username, scope)
    while True:
        await asyncio.sleep(0.4)
        await dispatch_pending_bridge_messages(
            username=username, scope=scope, turn_id=turn_id, bridge_dirs=bridge_dirs,
            kinds={"canvas_command"}, emitted_bridge_keys=emitted_bridge_keys, started_at=started_at, scoped_executor=True, executor_websocket=websocket,
            store_scope=scope,
            send=lambda frame: _send_json_best_effort(websocket, frame, send_lock),
        )
        pending_items = []
        for bridge_dir in bridge_dirs:
            try:
                pending_items.extend(
                    (bridge_dir, path) for path in bridge_dir.glob("*.pending.json")
                )
            except Exception:
                continue
        pending_items = sorted(pending_items, key=lambda item: item[1].stat().st_mtime)
        for bridge_dir, path in pending_items:
            try:
                is_preexisting_pending = path.stat().st_mtime < started_at - 1.0
            except Exception:
                continue
            key = path.name.removesuffix(".pending.json")
            if not _is_unmigrated_legacy_bridge_file(
                bridge_dir=bridge_dir,
                key=key,
            ):
                continue
            if _drop_resolved_pending_bridge_file(
                bridge_dir=bridge_dir,
                key=key,
                pending_path=path,
            ):
                continue
            pending = _load_pending_canvas_command(path)
            if pending is None:
                continue
            if pending.get("project_id") and pending.get("project_id") != scope.id:
                continue
            if _resolve_stale_pending_canvas_command(
                bridge_dir=bridge_dir,
                path=path,
                key=key,
                pending=pending,
                scope=scope,
                turn_id=turn_id,
            ):
                continue
            if is_preexisting_pending:
                continue
            if key in emitted_bridge_keys:
                continue
            emitted_bridge_keys.add(key)
            envelope = pending["envelope"]
            logger.info(
                "emitting canvas.command from pending bridge turn_id=%s canvas_id=%s commands=%s",
                turn_id,
                envelope.get("canvas_id"),
                len(envelope.get("commands") or []),
            )
            sent = await _send_json_best_effort(
                websocket,
                {
                    "type": "canvas.command",
                    "turn_id": turn_id,
                    "canvas_id": envelope.get("canvas_id"),
                    "agent_id": scope.agent_id or "main",
                    "bridge_key": key,
                    "envelope": envelope,
                },
                send_lock,
            )
            if not sent:
                return


async def _watch_pending_skill_studio_events(
    *,
    websocket: WebSocket,
    username: str,
    scope: ChatScope,
    store_scope: ChatScope | None = None,
    turn_id: str,
    send_lock: asyncio.Lock | None,
    emitted_bridge_keys: set[str],
    started_at: float,
) -> None:
    if not _is_freezone_scope(scope):
        return
    bridge_dir = _canvas_bridge_dir(
        username,
        project_state_dir=scope.state_dir,
        profile=_canvas_bridge_profile_for_scope(scope),
    )
    while True:
        await asyncio.sleep(0.4)
        await dispatch_pending_bridge_messages(
            username=username, scope=scope, turn_id=turn_id, bridge_dirs=[bridge_dir],
            kinds={"skill_studio_event"}, emitted_bridge_keys=emitted_bridge_keys, started_at=started_at, scoped_executor=True, executor_websocket=websocket,
            store_scope=store_scope,
            send=lambda frame: _send_json_best_effort(websocket, frame, send_lock),
        )
        try:
            pending_paths = sorted(
                bridge_dir.glob("*.pending.json"),
                key=lambda item: item.stat().st_mtime,
            )
        except Exception:
            continue
        for path in pending_paths:
            try:
                if path.stat().st_mtime < started_at - 1.0:
                    continue
            except Exception:
                continue
            key = path.name.removesuffix(".pending.json")
            if key in emitted_bridge_keys:
                continue
            if not _is_unmigrated_legacy_bridge_file(
                bridge_dir=bridge_dir,
                key=key,
            ):
                continue
            if _drop_resolved_pending_bridge_file(
                bridge_dir=bridge_dir,
                key=key,
                pending_path=path,
            ):
                continue
            pending = _load_pending_skill_studio_event(path)
            if pending is None:
                continue
            if pending.get("project_id") and pending.get("project_id") != scope.id:
                continue
            emitted_bridge_keys.add(key)
            ui_event = {
                **pending["event"],
                "bridge_key": key,
                "project_id": pending.get("project_id") or scope.id,
                "canvas_id": pending.get("canvas_id") or scope.canvas_id,
                "agent_id": scope.agent_id or "main",
                "turn_id": turn_id,
            }
            try:
                await chat_store.append_ui_event_async(
                    username,
                    store_scope or scope,
                    turn_id,
                    ui_event,
                )
            except Exception:
                logger.exception("failed to persist skill_studio.event ui event")
            logger.info(
                "emitting skill_studio.event from pending bridge turn_id=%s canvas_id=%s type=%s",
                turn_id,
                pending.get("canvas_id"),
                pending["event"].get("type"),
            )
            sent = await _send_json_best_effort(
                websocket,
                {
                    "type": "skill_studio.event",
                    "scope": scope.to_dict(),
                    "turn_id": turn_id,
                    "canvas_id": pending.get("canvas_id"),
                    "agent_id": scope.agent_id or "main",
                    "bridge_key": key,
                    "event": pending["event"],
                },
                send_lock,
            )
            if not sent:
                return


async def _watch_pending_clarification_events(
    *,
    websocket: WebSocket,
    username: str,
    scope: ChatScope,
    store_scope: ChatScope | None = None,
    turn_id: str,
    send_lock: asyncio.Lock | None,
    emitted_bridge_keys: set[str],
    started_at: float,
) -> None:
    if not _is_freezone_scope(scope):
        return
    bridge_dir = _canvas_bridge_dir(
        username,
        project_state_dir=scope.state_dir,
        profile=_canvas_bridge_profile_for_scope(scope),
    )
    while True:
        await asyncio.sleep(0.4)
        await dispatch_pending_bridge_messages(
            username=username, scope=scope, turn_id=turn_id, bridge_dirs=[bridge_dir],
            kinds={"clarification_event"}, emitted_bridge_keys=emitted_bridge_keys, started_at=started_at, scoped_executor=True, executor_websocket=websocket,
            store_scope=store_scope,
            send=lambda frame: _send_json_best_effort(websocket, frame, send_lock),
        )
        try:
            pending_paths = sorted(
                bridge_dir.glob("*.pending.json"),
                key=lambda item: item.stat().st_mtime,
            )
        except Exception:
            continue
        for path in pending_paths:
            try:
                if path.stat().st_mtime < started_at - 1.0:
                    continue
            except Exception:
                continue
            key = path.name.removesuffix(".pending.json")
            if key in emitted_bridge_keys:
                continue
            if not _is_unmigrated_legacy_bridge_file(
                bridge_dir=bridge_dir,
                key=key,
            ):
                continue
            if _drop_resolved_pending_bridge_file(
                bridge_dir=bridge_dir,
                key=key,
                pending_path=path,
            ):
                continue
            pending = _load_pending_clarification_event(path)
            if pending is None:
                continue
            if pending.get("project_id") and pending.get("project_id") != scope.id:
                continue
            emitted_bridge_keys.add(key)
            ui_event = {
                **pending["event"],
                "bridge_key": key,
                "project_id": pending.get("project_id") or scope.id,
                "canvas_id": pending.get("canvas_id") or scope.canvas_id,
                "agent_id": scope.agent_id or "main",
                "turn_id": turn_id,
            }
            try:
                await chat_store.append_ui_event_async(
                    username,
                    store_scope or scope,
                    turn_id,
                    ui_event,
                )
            except Exception:
                logger.exception("failed to persist assistant.clarification ui event")
            logger.info(
                "emitting assistant.clarification.event from pending bridge turn_id=%s canvas_id=%s",
                turn_id,
                pending.get("canvas_id"),
            )
            sent = await _send_json_best_effort(
                websocket,
                {
                    "type": "assistant.clarification.event",
                    "scope": scope.to_dict(),
                    "turn_id": turn_id,
                    "canvas_id": pending.get("canvas_id"),
                    "agent_id": scope.agent_id or "main",
                    "bridge_key": key,
                    "event": pending["event"],
                },
                send_lock,
            )
            if not sent:
                return


async def _watch_pending_canvas_context_requests(
    *,
    websocket: WebSocket,
    username: str,
    scope: ChatScope,
    turn_id: str,
    send_lock: asyncio.Lock,
    emitted_bridge_keys: set[str],
    started_at: float,
) -> None:
    if not _is_freezone_scope(scope):
        return
    bridge_dirs = _candidate_canvas_bridge_dirs_for_scope(username, scope)
    while True:
        await asyncio.sleep(0.4)
        await dispatch_pending_bridge_messages(
            username=username, scope=scope, turn_id=turn_id, bridge_dirs=bridge_dirs,
            kinds={"canvas_context"}, emitted_bridge_keys=emitted_bridge_keys, started_at=started_at, scoped_executor=True, executor_websocket=websocket,
            store_scope=scope,
            send=lambda frame: _send_json_best_effort(websocket, frame, send_lock),
        )
        pending_items = []
        for bridge_dir in bridge_dirs:
            try:
                pending_items.extend(
                    (bridge_dir, path) for path in bridge_dir.glob("*.pending.json")
                )
            except Exception:
                continue
        pending_items = sorted(pending_items, key=lambda item: item[1].stat().st_mtime)
        for bridge_dir, path in pending_items:
            try:
                is_preexisting_pending = path.stat().st_mtime < started_at - 1.0
            except Exception:
                continue
            key = path.name.removesuffix(".pending.json")
            if not _is_unmigrated_legacy_bridge_file(
                bridge_dir=bridge_dir,
                key=key,
            ):
                continue
            if _drop_resolved_pending_bridge_file(
                bridge_dir=bridge_dir,
                key=key,
                pending_path=path,
            ):
                continue
            pending = _load_pending_canvas_context(path)
            if pending is None:
                continue
            if pending.get("project_id") and pending.get("project_id") != scope.id:
                continue
            if _resolve_stale_pending_canvas_context(
                bridge_dir=bridge_dir,
                path=path,
                key=key,
                pending=pending,
                scope=scope,
                turn_id=turn_id,
            ):
                continue
            if is_preexisting_pending:
                continue
            if key in emitted_bridge_keys:
                continue
            emitted_bridge_keys.add(key)
            envelope = pending["envelope"]
            logger.info(
                "emitting canvas.context.request from pending bridge turn_id=%s canvas_id=%s requests=%s",
                turn_id,
                envelope.get("canvas_id"),
                len(envelope.get("requests") or []),
            )
            sent = await _send_json_best_effort(
                websocket,
                {
                    "type": "canvas.context.request",
                    "turn_id": turn_id,
                    "canvas_id": envelope.get("canvas_id"),
                    "agent_id": scope.agent_id or "main",
                    "bridge_key": key,
                    "envelope": envelope,
                },
                send_lock,
            )
            if not sent:
                return


def _skill_studio_status_frame(
    *,
    scope: ChatScope,
    turn_id: str,
    text: str,
    user_text: str | None = None,
    status: str = "routing",
    message: str | None = None,
) -> dict[str, Any] | None:
    route_text = user_text if user_text is not None else text
    if not chat_service._freezone_skill_studio_requested(route_text):  # type: ignore[attr-defined]
        return None
    status_messages = {
        "routing": "正在整理 Skill 方向...",
    }
    return {
        "type": "skill_studio.status",
        "scope": scope.to_dict(),
        "turn_id": turn_id,
        "status": status,
        "message": message or status_messages.get(status) or "正在整理 Skill 方向...",
    }


async def _reference_user_confirmation_context(
    *, user: dict[str, Any], scope: ChatScope, message: dict[str, Any], agent_text: str,
    project_id: str | None = None, username: str | None = None,
    turn_id: str | None = None, ownership: dict | None = None,
) -> str:
    """Expose the current saved proposal to the main Agent.

    Natural-language interpretation belongs to the main Agent. This hook only
    performs deterministic lookup of the one plan that was actually presented
    in this chat and gives the Agent its exact references and allowed next
    actions. Confirmation is recorded only by the explicit host action.
    """
    if not _is_freezone_scope(scope) or scope.kind not in {"project", "freezone"}:
        return agent_text
    credential_kind = str(user.get("credential_kind") or "browser_session")
    if credential_kind not in {"browser_session", "user_session"}:
        return agent_text
    from novelvideo.api.routes.reference_director import _context
    from novelvideo.chat.reference_decision_ingress import pending_user_decision
    try:
        # Freezone chat history is stored under the local project name, while
        # remix artifacts and authenticated project scope use the project ID.
        # Resolve both from the already-authorized project context.
        canonical_scope = replace(scope, id=project_id) if project_id else scope
        ctx, _, artifacts = await _context(str(canonical_scope.id), str(canonical_scope.canvas_id or ""), user)
        decision = await asyncio.to_thread(
            pending_user_decision, artifacts, scope=canonical_scope,
            requester_user_id=ctx.requester_user_id,
            credential_kind=credential_kind, message=message,
        )
        if decision is None or decision['kind'] != 'plan':
            return agent_text
        from novelvideo.freezone.remix.reference_commission import read_reference_commission
        commission = await asyncio.to_thread(
            read_reference_commission, artifacts, decision['commission_ref'], latest=True,
        )
        host_context = {
            'phase': 'awaiting_plan_confirmation',
            'confirmation_applied': False,
            'decision_ref': decision['decision_ref'],
            'commission_ref': commission['commission_ref'],
            'plan_ref': commission.get('plan_ref'),
            'review_ref': commission.get('review_ref'),
            'allowed_actions': [
                'discuss_current_plan',
                'edit_current_plan',
                'confirm_exact_presented_plan',
            ],
            'confirmation_rule': (
                'Only call freezone_confirm_reference_plan after the user clearly '
                'approves this exact presented plan. Do not infer approval from '
                'this context, a keyword, or an assistant message.'
            ),
        }
        return (agent_text + '\n\n[HOST_REFERENCE_DECISION_CONTEXT]\n'
                + json.dumps(host_context, ensure_ascii=False)
                + '\n[/HOST_REFERENCE_DECISION_CONTEXT]')

    except Exception:
        logger.exception("reference director confirmation ingress failed project=%s canvas=%s message=%s",
                         scope.id, scope.canvas_id, message.get("id"))
        return agent_text


async def _current_reference_parameter_answers(
    ctx: Any, *, canvas_id: str, agent_id: str,
) -> tuple[str, dict[str, Any]] | None:
    """Load parameters owned by the one current commission for an ordinary turn."""
    if not canvas_id:
        return None
    from novelvideo.freezone.remix.reference_parameter_receipts import (
        generation_requirements_digest, generation_requirements_snapshot,
        merged_reference_parameter_answers,
    )
    from novelvideo.freezone.workflow_drafts import read_reference_parameter_submissions

    owner = await _current_reference_parameter_owner(
        ctx, canvas_id=canvas_id, agent_id=agent_id,
    )
    if owner is None:
        return None
    commission, run = owner
    commission_id = commission["commission_ref"]["artifact_id"]
    scope = {"project_id": ctx.project_id, "canvas_id": canvas_id,
             "agent_id": agent_id, "requester_user_id": ctx.requester_user_id,
             "commission_id": commission_id}
    digest = generation_requirements_digest(commission, run.get("inputs") or {})
    interactions = await asyncio.to_thread(
        read_reference_parameter_submissions, project_dir=ctx.state_dir,
        scope=scope, requirements_digest=digest, include_prior_requirements=True,
    )
    answers = merged_reference_parameter_answers(
        interactions, scope, digest,
        current_requirements=generation_requirements_snapshot(commission, run.get("inputs") or {}),
    )
    return (commission_id, answers) if answers else None


async def _stream_project_turn(
    *,
    websocket: WebSocket,
    user: dict[str, Any],
    username: str,
    scope: ChatScope,
    text: str,
    attachments: list[ChatAttachmentIn],
    turn_id: str,
    user_text: str | None = None,
    surface: str | None = None,
    surface_context: dict[str, Any] | None = None,
    store_scope: ChatScope | None = None,
) -> None:
    project = str(scope.id)
    project_ctx = await _project_context_for_scope(user, scope)
    if project_ctx is None:
        # `ChatScope.from_payload`（`chat/store.py:82-83`）保证 project 态的 id 非空，
        # 所以这里在本分支里够不着。够不着也必须 fail-closed：没有 registry 校验过的
        # 身份就没法判定出网身份，绕过绑定直接开聊正是 OI-61 那条漏。
        # 不新造错误码，复用既有词汇。
        raise EgressBoundaryError("ORG_CONTEXT_REQUIRED")
    project_dir = project_ctx.output_dir
    project_state_dir = project_ctx.state_dir
    scope = replace(scope, state_dir=str(project_state_dir))
    execution_context = AgentExecutionContext.from_project_scope(
        scope=scope,
        project=project_ctx,
    )
    storage_scope = (
        _chat_store_scope_for_project_context(store_scope, project_ctx)
        if store_scope is not None
        else None
    )
    agent_text = _text_with_attachment_context(text, attachments)
    display_text = str(user_text or text).strip()
    reference_ownership: dict = {}
    if storage_scope is not None:
        user_message = await chat_store.append_message_async(
            username,
            storage_scope,
            "user",
            display_text,
            media=_attachment_payloads(attachments),
            turn_id=turn_id,
        )
        logger.info("reference_flow.message_received project_id=%s canvas_id=%s turn_id=%s message_id=%s",
                    project_ctx.project_id, storage_scope.canvas_id, turn_id, user_message['id'])
        agent_text = await _reference_user_confirmation_context(
            user=user, scope=storage_scope, message=user_message, agent_text=agent_text,
            project_id=project_ctx.project_id, username=username,
            turn_id=turn_id, ownership=reference_ownership,
        )
    else:
        await asyncio.to_thread(
            chat_service.add_user_message,
            username,
            project,
            display_text,
            project_dir=project_dir,
            project_state_dir=project_state_dir,
        )
    send_lock = asyncio.Lock()
    disconnected = asyncio.Event()
    runtime_backend = chat_service.get_chat_backend_name()
    runtime_ids: dict[str, str | None] = {"thread_id": None, "turn_id": None}
    agent_profile = execution_context.agent_profile
    heartbeat_task = asyncio.create_task(
        _chat_heartbeat(
            websocket,
            scope=scope,
            turn_id=turn_id,
            send_lock=send_lock,
            disconnected=disconnected,
        )
    )
    disconnect_task = asyncio.create_task(
        _interrupt_agent_on_disconnect(
            disconnected,
            runtime_backend=runtime_backend,
            username=username,
            project=project,
            agent_profile=agent_profile,
            runtime_ids=runtime_ids,
        )
    )
    bridge_result_receive_task = asyncio.create_task(
        _receive_bridge_results_during_turn(
            websocket=websocket, user=user, username=username
        )
    )
    from novelvideo.freezone.canvas_command_bridge import register_bridge_producer_turn, release_bridge_producer_turn
    producer_binding = {"username":username,"project_id":scope.id,"canvas_id":scope.canvas_id,
                        "agent_id":scope.agent_id or "main","turn_id":turn_id}
    producer_dirs = _candidate_canvas_bridge_dirs_for_scope(username,scope)
    for directory in producer_dirs:
        register_bridge_producer_turn(binding=producer_binding,bridge_dir=directory)
    emitted_bridge_keys: set[str] = set()
    pending_canvas_task = asyncio.create_task(
        _watch_pending_canvas_commands(
            websocket=websocket,
            username=username,
            scope=scope,
            turn_id=turn_id,
            send_lock=send_lock,
            emitted_bridge_keys=emitted_bridge_keys,
            started_at=time.time(),
        )
    )
    pending_canvas_context_task = asyncio.create_task(
        _watch_pending_canvas_context_requests(
            websocket=websocket,
            username=username,
            scope=scope,
            turn_id=turn_id,
            send_lock=send_lock,
            emitted_bridge_keys=emitted_bridge_keys,
            started_at=time.time(),
        )
    )
    pending_skill_studio_task = asyncio.create_task(
        _watch_pending_skill_studio_events(
            websocket=websocket,
            username=username,
            scope=scope,
            store_scope=storage_scope,
            turn_id=turn_id,
            send_lock=send_lock,
            emitted_bridge_keys=emitted_bridge_keys,
            started_at=time.time(),
        )
    )
    pending_clarification_task = asyncio.create_task(
        _watch_pending_clarification_events(
            websocket=websocket,
            username=username,
            scope=scope,
            store_scope=storage_scope,
            turn_id=turn_id,
            send_lock=send_lock,
            emitted_bridge_keys=emitted_bridge_keys,
            started_at=time.time(),
        )
    )
    skill_studio_status = _skill_studio_status_frame(
        scope=scope,
        turn_id=turn_id,
        text=agent_text,
        user_text=display_text,
    )
    if skill_studio_status is not None:
        await _send_json_best_effort(websocket, skill_studio_status, send_lock)
    done_sent = False
    assistant_sent_text = ""

    async def on_event(event: dict[str, Any]) -> None:
        nonlocal assistant_sent_text, done_sent
        event_type = event.get("type")
        if event_type == "thread_started":
            runtime_ids["thread_id"] = str(event.get("thread_id") or "").strip() or None
            runtime_ids["turn_id"] = str(event.get("turn_id") or "").strip() or None
            await _send_json_best_effort(
                websocket,
                {
                    "type": "thread.started",
                    "scope": scope.to_dict(),
                    "thread_id": event.get("thread_id"),
                    "turn_id": event.get("turn_id") or turn_id,
                },
                send_lock,
            )
        elif event_type in {"turn_started", "turn_completed"}:
            await _send_json_best_effort(
                websocket,
                {
                    "type": (
                        "agent.turn.started"
                        if event_type == "turn_started"
                        else "agent.turn.completed"
                    ),
                    "scope": scope.to_dict(),
                    "thread_id": event.get("thread_id"),
                    "turn_id": event.get("turn_id") or turn_id,
                    "status": event.get("status"),
                    "disposition": event.get("disposition"),
                    "error": event.get("error"),
                },
                send_lock,
            )
        elif event_type == "assistant_delta":
            assistant_sent_text = str(event.get("text") or "")
            await _send_json_best_effort(
                websocket,
                {
                    "type": "assistant.delta",
                    "scope": scope.to_dict(),
                    "text": assistant_sent_text,
                    "turn_id": turn_id,
                    "accumulated": True,
                },
                send_lock,
            )
        elif event_type == "thought_delta":
            await _send_json_best_effort(
                websocket,
                {
                    "type": "agent.thought.delta",
                    "scope": scope.to_dict(),
                    "turn_id": turn_id,
                    "text": str(event.get("text") or ""),
                    "source": event.get("source"),
                },
                send_lock,
            )
        elif event_type == "plan_update":
            await _send_json_best_effort(
                websocket,
                {
                    "type": "agent.plan.update",
                    "scope": scope.to_dict(),
                    "turn_id": turn_id,
                    "text": str(event.get("text") or ""),
                    "entries": event.get("entries") or [],
                },
                send_lock,
            )
        elif event_type == "usage_update":
            await _send_json_best_effort(
                websocket,
                {
                    "type": "agent.usage.update",
                    "scope": scope.to_dict(),
                    "turn_id": turn_id,
                    "usage": event.get("usage") or {},
                },
                send_lock,
            )
        elif event_type == "permission_requested":
            await _send_json_best_effort(
                websocket,
                {
                    "type": "agent.permission.requested",
                    "scope": scope.to_dict(),
                    "turn_id": turn_id,
                    "request_id": event.get("request_id"),
                    "text": str(event.get("text") or "需要操作授权"),
                    "options": event.get("options") or [],
                    "tool_call": event.get("tool_call") or {},
                },
                send_lock,
            )
        elif event_type in {"tool_started", "tool_updated", "tool_update"}:
            tool_name, tool_body = _tool_display_payload(
                event.get("text"), event.get("name")
            )
            status = str(
                event.get("status")
                or ("pending" if event_type == "tool_started" else "completed")
            )
            await _send_json_best_effort(
                websocket,
                {
                    "type": (
                        "agent.tool.started"
                        if event_type == "tool_started"
                        else "agent.tool.updated"
                    ),
                    "scope": scope.to_dict(),
                    "turn_id": turn_id,
                    "call_id": event.get("call_id"),
                    "name": tool_name,
                    "status": status,
                    "text": tool_body,
                    "input": event.get("input"),
                    "output": event.get("output"),
                    "error": event.get("error"),
                    "result_json": event.get("result_json"),
                },
                send_lock,
            )
        elif event_type == "skill_studio.event":
            await _send_json_best_effort(
                websocket,
                {
                    "type": "skill_studio.event",
                    "scope": scope.to_dict(),
                    "turn_id": event.get("turn_id") or turn_id,
                    "event": event.get("event"),
                },
                send_lock,
            )
        elif event_type == "assistant.clarification.event":
            await _send_json_best_effort(
                websocket,
                {
                    "type": "assistant.clarification.event",
                    "scope": scope.to_dict(),
                    "turn_id": event.get("turn_id") or turn_id,
                    "event": event.get("event"),
                },
                send_lock,
            )
        elif event_type == "assistant_message":
            message = event.get("message")
            if isinstance(message, dict):
                assistant_sent_text = _message_content(message)
                await _send_json_best_effort(
                    websocket,
                    {
                        "type": "assistant.message",
                        "scope": scope.to_dict(),
                        "turn_id": turn_id,
                        "message": message,
                    },
                    send_lock,
                )
        elif event_type == "done":
            final_message = event.get("message")
            final_text = _message_content(final_message)
            if _should_emit_final_text(final_text, assistant_sent_text):
                assistant_sent_text = final_text
                await _send_json_best_effort(
                    websocket,
                    {
                        "type": "assistant.delta",
                        "scope": scope.to_dict(),
                        "text": final_text,
                        "turn_id": turn_id,
                        "accumulated": True,
                    },
                    send_lock,
                )
            done_sent = await _send_json_best_effort(
                websocket,
                {
                    "type": "chat.done",
                    "turn_id": turn_id,
                    "scope": scope.to_dict(),
                    "message": (
                        final_message if isinstance(final_message, dict) else None
                    ),
                },
                send_lock,
            )

    try:
        trusted_reference = None
        if scope.canvas_id:
            try:
                trusted_reference = await _current_reference_parameter_answers(
                    project_ctx, canvas_id=scope.canvas_id, agent_id=scope.agent_id or "main",
                )
            except (ValueError, FileNotFoundError):
                logger.exception("reference parameter receipt lookup failed canvas_id=%s", scope.canvas_id)
        # 请求路径上唯一的出网身份绑定点。放在这里而不是分发块，是为了复用
        # `_project_context_for_scope` 已经解出的 `ProjectContext`——身份取值只走
        # registry 校验过的 `project_ctx`，不用客户端输入的 `scope.id`，也不新增
        # 第二次身份解析（第二条信任链就是第二个漏洞面）。
        # 非组织身份（平台／个人／CE local／灰度未开）下 `request_egress_scope`
        # 什么都不绑并 yield `None`；`egress_context=None` 照传，
        # `service.py` 的 `if egress_context is not None` 会把它当平台路径跳过，
        # 平台行为逐字节不变。刻意不写成 if/else 两条调用路径。
        async with request_egress_scope(
            requester_user_id=project_ctx.requester_user_id,
            project_id=project_ctx.project_id,
            task_type=HERMES_TEXT_EGRESS_TASK_TYPE,
        ) as egress_context:
            from novelvideo.freezone.canvas_context_reader import saved_context_execution
            from contextlib import nullcontext

            human_binding = (
                saved_context_execution(
                    execution_context=execution_context, turn_id=turn_id,
                    state_dir=project_state_dir, human_message_id=user_message['id'],
                    chat_scope_id=str(storage_scope.id),
                )
                if execution_context.surface == 'freezone' and storage_scope is not None
                else nullcontext()
            )
            with human_binding:
                if execution_context.surface == 'freezone':
                    logger.info("reference_flow.main_agent_started project_id=%s canvas_id=%s turn_id=%s",
                                project_ctx.project_id, scope.canvas_id, turn_id)
                await chat_service.stream_assistant_reply(
                username,
                project,
                agent_text,
                on_event,
                project_dir=project_dir,
                project_state_dir=project_state_dir,
                trusted_reference_turn_owner=reference_ownership or None,
                egress_context=egress_context,
                requester_user_id=project_ctx.requester_user_id,
                surface=surface,
                surface_context=surface_context,
                store_scope=storage_scope,
                turn_id=turn_id,
                route_prompt=display_text,
                backend=runtime_backend,
                execution_context=execution_context,
                trusted_reference_commission_id=trusted_reference[0] if trusted_reference else None,
                trusted_reference_parameter_answers=trusted_reference[1] if trusted_reference else None,
                )
    finally:
        heartbeat_task.cancel()
        disconnect_task.cancel()
        bridge_result_receive_task.cancel()
        pending_canvas_task.cancel()
        pending_canvas_context_task.cancel()
        pending_skill_studio_task.cancel()
        pending_clarification_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        with contextlib.suppress(asyncio.CancelledError):
            await disconnect_task
        with contextlib.suppress(asyncio.CancelledError):
            await bridge_result_receive_task
        with contextlib.suppress(asyncio.CancelledError):
            await pending_canvas_task
        with contextlib.suppress(asyncio.CancelledError):
            await pending_canvas_context_task
        with contextlib.suppress(asyncio.CancelledError):
            await pending_skill_studio_task
        with contextlib.suppress(asyncio.CancelledError):
            await pending_clarification_task
        for directory in producer_dirs:
            release_bridge_producer_turn(binding=producer_binding,bridge_dir=directory)
        if not done_sent:
            await _send_json_best_effort(
                websocket,
                {"type": "chat.done", "turn_id": turn_id, "scope": scope.to_dict()},
                send_lock,
            )


async def _stream_home_turn_codex(
    *,
    websocket: WebSocket,
    user: dict[str, Any],
    username: str,
    scope: ChatScope,
    text: str,
    attachments: list[ChatAttachmentIn],
    turn_id: str,
) -> None:
    """Run home chat through the same backend-neutral service as projects."""

    from novelvideo.chat.hermes_egress import HOME_SCOPE_EGRESS_PROJECT_ID

    before_projects = set(list_user_projects(username))
    agent_text = _text_with_attachment_context(text, attachments)
    await chat_store.append_message_async(
        username,
        scope,
        "user",
        text,
        media=_attachment_payloads(attachments),
        turn_id=turn_id,
    )
    send_lock = asyncio.Lock()
    disconnected = asyncio.Event()
    runtime_ids: dict[str, str | None] = {"thread_id": None, "turn_id": None}
    heartbeat_task = asyncio.create_task(
        _chat_heartbeat(
            websocket,
            scope=scope,
            turn_id=turn_id,
            send_lock=send_lock,
            disconnected=disconnected,
        )
    )
    disconnect_task = asyncio.create_task(
        _interrupt_agent_on_disconnect(
            disconnected,
            runtime_backend="codex",
            username=username,
            project="",
            agent_profile="main",
            runtime_ids=runtime_ids,
        )
    )
    done_sent = False
    assistant_sent_text = ""

    async def on_event(event: dict[str, Any]) -> None:
        nonlocal assistant_sent_text, done_sent
        event_type = str(event.get("type") or "")
        if event_type == "thread_started":
            runtime_ids["thread_id"] = str(event.get("thread_id") or "").strip() or None
            runtime_ids["turn_id"] = str(event.get("turn_id") or "").strip() or None
            await _send_json_best_effort(
                websocket,
                {
                    "type": "thread.started",
                    "scope": scope.to_dict(),
                    "thread_id": event.get("thread_id"),
                    "turn_id": event.get("turn_id") or turn_id,
                },
                send_lock,
            )
        elif event_type in {"turn_started", "turn_completed"}:
            await _send_json_best_effort(
                websocket,
                {
                    "type": (
                        "agent.turn.started"
                        if event_type == "turn_started"
                        else "agent.turn.completed"
                    ),
                    "scope": scope.to_dict(),
                    "thread_id": event.get("thread_id"),
                    "turn_id": event.get("turn_id") or turn_id,
                    "status": event.get("status"),
                    "disposition": event.get("disposition"),
                    "error": event.get("error"),
                },
                send_lock,
            )
        elif event_type == "assistant_delta":
            assistant_sent_text = str(event.get("text") or "")
            await _send_json_best_effort(
                websocket,
                {
                    "type": "assistant.delta",
                    "scope": scope.to_dict(),
                    "text": assistant_sent_text,
                    "turn_id": turn_id,
                    "accumulated": True,
                },
                send_lock,
            )
        elif event_type in {"thought_delta", "plan_update", "usage_update"}:
            payload = {
                "thought_delta": {
                    "type": "agent.thought.delta",
                    "text": str(event.get("text") or ""),
                    "source": event.get("source"),
                },
                "plan_update": {
                    "type": "agent.plan.update",
                    "text": str(event.get("text") or ""),
                    "entries": event.get("entries") or [],
                },
                "usage_update": {
                    "type": "agent.usage.update",
                    "usage": event.get("usage") or {},
                },
            }[event_type]
            await _send_json_best_effort(
                websocket,
                {**payload, "scope": scope.to_dict(), "turn_id": turn_id},
                send_lock,
            )
        elif event_type in {"tool_started", "tool_updated", "tool_update"}:
            tool_name, tool_body = _tool_display_payload(
                event.get("text"), event.get("name")
            )
            await _send_json_best_effort(
                websocket,
                {
                    "type": (
                        "agent.tool.started"
                        if event_type == "tool_started"
                        else "agent.tool.updated"
                    ),
                    "scope": scope.to_dict(),
                    "turn_id": turn_id,
                    "call_id": event.get("call_id"),
                    "name": tool_name,
                    "status": event.get("status") or "completed",
                    "text": tool_body,
                    "input": event.get("input"),
                    "output": event.get("output"),
                    "error": event.get("error"),
                    "result_json": event.get("result_json"),
                },
                send_lock,
            )
        elif event_type == "done":
            message = event.get("message")
            final_text = _message_content(message)
            if _should_emit_final_text(final_text, assistant_sent_text):
                assistant_sent_text = final_text
                await _send_json_best_effort(
                    websocket,
                    {
                        "type": "assistant.delta",
                        "scope": scope.to_dict(),
                        "text": final_text,
                        "turn_id": turn_id,
                        "accumulated": True,
                    },
                    send_lock,
                )
            if isinstance(message, dict):
                await _send_json_best_effort(
                    websocket,
                    {
                        "type": "assistant.message",
                        "scope": scope.to_dict(),
                        "turn_id": turn_id,
                        "message": message,
                    },
                    send_lock,
                )
            done_sent = await _send_json_best_effort(
                websocket,
                {
                    "type": "chat.done",
                    "turn_id": turn_id,
                    "scope": scope.to_dict(),
                    "message": message if isinstance(message, dict) else None,
                },
                send_lock,
            )

    try:
        requester_user_id = await _requester_user_id_for_chat(user, scope)
        async with request_egress_scope(
            requester_user_id=requester_user_id,
            project_id=HOME_SCOPE_EGRESS_PROJECT_ID,
            task_type=HERMES_TEXT_EGRESS_TASK_TYPE,
        ) as egress_context:
            await chat_service.stream_assistant_reply(
                username,
                "",
                agent_text,
                on_event,
                egress_context=egress_context,
                requester_user_id=requester_user_id,
                egress_project_id=HOME_SCOPE_EGRESS_PROJECT_ID,
                store_scope=scope,
                turn_id=turn_id,
                backend="codex",
            )
        after_projects = set(list_user_projects(username))
        for project in sorted(after_projects - before_projects):
            project_scope = ChatScope(kind="project", id=project)
            await chat_store.append_message_async(
                username,
                project_scope,
                "system",
                f"Created from home conversation turn {turn_id}.",
            )
            await _send_json_best_effort(
                websocket,
                {"type": "project.created", "project": project},
                send_lock,
            )
    finally:
        heartbeat_task.cancel()
        disconnect_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        with contextlib.suppress(asyncio.CancelledError):
            await disconnect_task
        if not done_sent:
            await _send_json_best_effort(
                websocket,
                {"type": "chat.done", "turn_id": turn_id, "scope": scope.to_dict()},
                send_lock,
            )


async def _stream_home_turn(
    *,
    websocket: WebSocket,
    user: dict[str, Any],
    username: str,
    scope: ChatScope,
    text: str,
    attachments: list[ChatAttachmentIn],
    turn_id: str,
) -> None:
    if chat_service.get_chat_backend_name() == "codex":
        await _stream_home_turn_codex(
            websocket=websocket,
            user=user,
            username=username,
            scope=scope,
            text=text,
            attachments=attachments,
            turn_id=turn_id,
        )
        return

    from novelvideo.chat.hermes_egress import HOME_SCOPE_EGRESS_PROJECT_ID
    from novelvideo.chat.hermes_pool import pool as hermes_pool

    before_projects = set(list_user_projects(username))
    home_messages = await chat_store.list_messages_async(username, scope)
    previous_assistant = next(
        (
            str(message.get("content") or "")
            for message in reversed(home_messages)
            if message.get("role") == "assistant"
        ),
        "",
    )
    agent_text = _text_with_attachment_context(text, attachments)
    await chat_store.append_message_async(
        username,
        scope,
        "user",
        text,
        media=_attachment_payloads(attachments),
        turn_id=turn_id,
    )
    # home 态请求路径上唯一的出网身份绑定点。`_stream_home_turn` 是一段绕开
    # `chat/service.py` 的独立流式循环，所以绑定必须在这里就地做——OI-61 只补了
    # project 态，home 态因此漏到了 OI-63。
    #
    # 身份解析复用既有 helper，不另发明第二条信任链；home 分支会落到
    # `user_id_from_api_user(user)`。
    #
    # 出网 project 身份用哨兵 `HOME_SCOPE_EGRESS_PROJECT_ID`：`TrustedEgressContext`
    # 的 `project_id` 有非空不变量，而 home 态的**会话**身份必须是 None。两者是
    # 两个口径，所以下面 `get_for_user` 里 `project_id` 与 `egress_project_id`
    # 各传各的——哨兵只喂出网比对，不得漏进子进程的 DRAMACLAW_PROJECT_ID。
    #
    # 哨兵必须两跳一致：绑定、换 authorization、取号三处同值，否则
    # `_strict_admission` 在 `authorize_credentialed_hermes` 与
    # `build_hermes_child_env` 两处各比一次，任一处不一致即 TASK_ENVELOPE_INVALID。
    #
    # 非组织身份下 `request_egress_scope` 什么都不绑并 yield `None`，
    # `authorize_hermes_launch` 随之返回 `None`，平台路径逐字节不变。
    # 刻意不写成 if/else 两条调用路径。
    requester_user_id = await _requester_user_id_for_chat(user, scope)
    async with request_egress_scope(
        requester_user_id=requester_user_id,
        project_id=HOME_SCOPE_EGRESS_PROJECT_ID,
        task_type=HERMES_TEXT_EGRESS_TASK_TYPE,
    ) as egress_context:
        authorization = await chat_service.authorize_hermes_launch(
            egress_context=egress_context,
            username=username,
            requester_user_id=requester_user_id,
            egress_project_id=HOME_SCOPE_EGRESS_PROJECT_ID,
            prompt=agent_text,
        )
        thread = await hermes_pool.get_for_user(
            username,
            scope_kind="home",
            # 会话身份：home 态恒为 None。出网身份走 egress_project_id。
            project_id=None,
            egress_project_id=HOME_SCOPE_EGRESS_PROJECT_ID,
            requester_user_id=requester_user_id,
            authorization=authorization,
        )

    assistant_text = ""
    assistant_sent_text = ""
    tool_text = ""
    tool_name = ""
    persisted = False
    send_lock = asyncio.Lock()
    disconnected = asyncio.Event()
    runtime_ids: dict[str, str | None] = {
        "thread_id": str(getattr(thread, "id", "") or "").strip() or None,
        "turn_id": None,
    }
    heartbeat_task = asyncio.create_task(
        _chat_heartbeat(
            websocket,
            scope=scope,
            turn_id=turn_id,
            send_lock=send_lock,
            disconnected=disconnected,
        )
    )
    disconnect_task = asyncio.create_task(
        _interrupt_agent_on_disconnect(
            disconnected,
            runtime_backend="hermes",
            username=username,
            project="",
            agent_profile="main",
            runtime_ids=runtime_ids,
        )
    )
    done_sent = False

    async def persist_partial_reply() -> dict[str, Any] | None:
        nonlocal persisted, assistant_text
        if persisted:
            return None
        final_text = chat_service._strip_replayed_chat_response(
            assistant_text,
            previous_assistant,
            text,
        ).strip()
        final_text = chat_service._strip_freezone_tool_lifecycle_failure_text(
            final_text,
            tool_mode="freezone_canvas" if _is_freezone_scope(scope) else "default",
        ).strip()
        if not final_text:
            return None
        message = await chat_store.append_message_async(
            username,
            scope,
            "assistant",
            final_text,
            turn_id=turn_id,
            idempotency_key=f"assistant:{turn_id}",
        )
        persisted = True
        return message

    await _send_json_best_effort(
        websocket,
        {
            "type": "thread.started",
            "scope": scope.to_dict(),
            "thread_id": getattr(thread, "id", None) or None,
            "turn_id": turn_id,
        },
        send_lock,
    )

    async def hermes_events_with_session_retry():
        nonlocal thread
        from novelvideo.chat.hermes_sdk import (
            HermesSessionUnavailableError,
            _is_session_unavailable_error,
        )

        retried = False
        while True:
            try:
                async for stream_event in thread.stream(
                    agent_text, current_project=None
                ):
                    if (
                        not retried
                        and stream_event.type == "complete"
                        and not assistant_text.strip()
                        and not tool_text.strip()
                        and _is_session_unavailable_error(stream_event.text)
                    ):
                        logger.warning(
                            "hermes websocket prompt completed with unavailable cached session; "
                            "resetting and retrying once user=%s scope=%s: %s",
                            username,
                            scope.to_dict(),
                            stream_event.text,
                        )
                        thread = await hermes_pool.reset_for_user(
                            username,
                            scope_kind="home",
                            project_id=None,
                        )
                        retried = True
                        break
                    yield stream_event
                else:
                    return
                continue
            except HermesSessionUnavailableError as exc:
                if retried or assistant_text.strip() or tool_text.strip():
                    raise
                logger.warning(
                    "hermes cached websocket session unavailable; resetting and retrying once "
                    "user=%s scope=%s: %s",
                    username,
                    scope.to_dict(),
                    exc,
                )
                thread = await hermes_pool.reset_for_user(
                    username,
                    scope_kind="home",
                    project_id=None,
                )
                retried = True
                continue
            return

    try:
        async for event in hermes_events_with_session_retry():
            if event.type == "thread_started":
                runtime_ids["thread_id"] = str(event.thread_id or "").strip() or None
                runtime_ids["turn_id"] = str(event.turn_id or "").strip() or None
                await _send_json_best_effort(
                    websocket,
                    {
                        "type": "thread.started",
                        "scope": scope.to_dict(),
                        "thread_id": str(event.thread_id or "").strip() or None,
                        "turn_id": str(event.turn_id or "").strip() or turn_id,
                    },
                    send_lock,
                )
            elif event.type in {"turn_started", "turn_completed"}:
                await _send_json_best_effort(
                    websocket,
                    {
                        "type": (
                            "agent.turn.started"
                            if event.type == "turn_started"
                            else "agent.turn.completed"
                        ),
                        "scope": scope.to_dict(),
                        "thread_id": str(event.thread_id or "").strip() or None,
                        "turn_id": str(event.turn_id or "").strip() or turn_id,
                        "status": event.status,
                        "disposition": event.disposition,
                        "error": event.error,
                    },
                    send_lock,
                )
            elif event.type == "assistant_delta":
                assistant_text = chat_service._merge_stream_text(
                    assistant_text, event.text
                )
                display_text = chat_service._strip_replayed_chat_response(
                    assistant_text,
                    previous_assistant,
                    text,
                    suppress_partial_replay=True,
                )
                display_text = chat_service._strip_freezone_tool_lifecycle_failure_text(
                    display_text,
                    tool_mode=(
                        "freezone_canvas" if _is_freezone_scope(scope) else "default"
                    ),
                )
                assistant_sent_text = display_text
                await _send_json_best_effort(
                    websocket,
                    {
                        "type": "assistant.delta",
                        "scope": scope.to_dict(),
                        "text": display_text,
                        "turn_id": turn_id,
                        "accumulated": True,
                    },
                    send_lock,
                )
            elif event.type == "thought_delta":
                await _send_json_best_effort(
                    websocket,
                    {
                        "type": "agent.thought.delta",
                        "scope": scope.to_dict(),
                        "turn_id": turn_id,
                        "text": str(event.text or ""),
                        "source": event.name,
                    },
                    send_lock,
                )
            elif event.type == "plan_update":
                await _send_json_best_effort(
                    websocket,
                    {
                        "type": "agent.plan.update",
                        "scope": scope.to_dict(),
                        "turn_id": turn_id,
                        "text": str(event.text or ""),
                        "entries": event.entries or [],
                    },
                    send_lock,
                )
            elif event.type == "usage_update":
                await _send_json_best_effort(
                    websocket,
                    {
                        "type": "agent.usage.update",
                        "scope": scope.to_dict(),
                        "turn_id": turn_id,
                        "usage": event.usage or {},
                    },
                    send_lock,
                )
            elif event.type == "permission_requested":
                await _send_json_best_effort(
                    websocket,
                    {
                        "type": "agent.permission.requested",
                        "scope": scope.to_dict(),
                        "turn_id": turn_id,
                        "request_id": event.request_id,
                        "text": str(event.text or "需要操作授权"),
                        "options": event.options or [],
                        "tool_call": event.raw or {},
                    },
                    send_lock,
                )
            elif event.type in {"tool_started", "tool_updated", "tool_update"}:
                if event.name:
                    tool_name = event.name
                tool_text += str(event.text or "") + "\n"
                display_name, display_body = _tool_display_payload(tool_text, tool_name)
                await _send_json_best_effort(
                    websocket,
                    {
                        "type": (
                            "agent.tool.started"
                            if event.type == "tool_started"
                            else "agent.tool.updated"
                        ),
                        "scope": scope.to_dict(),
                        "turn_id": turn_id,
                        "call_id": event.call_id,
                        "name": display_name,
                        "status": event.status
                        or ("pending" if event.type == "tool_started" else "completed"),
                        "text": display_body,
                        "input": event.input,
                        "output": event.output,
                        "error": event.error,
                        "result_json": event.structured,
                    },
                    send_lock,
                )
            elif event.type == "complete":
                assistant_text = _completion_text_or_existing(
                    event.text, assistant_text
                )

        assistant_text = chat_service._strip_replayed_chat_response(
            assistant_text,
            previous_assistant,
            text,
        )
        assistant_text = chat_service._strip_freezone_tool_lifecycle_failure_text(
            assistant_text,
            tool_mode="freezone_canvas" if _is_freezone_scope(scope) else "default",
        )
        assistant_text = assistant_text.strip() or EMPTY_AGENT_REPLY_MESSAGE
        message = await chat_store.append_message_async(
            username,
            scope,
            "assistant",
            assistant_text,
            turn_id=turn_id,
            idempotency_key=f"assistant:{turn_id}",
        )
        persisted = True
        await _send_json_best_effort(
            websocket,
            {
                "type": "assistant.message",
                "scope": scope.to_dict(),
                "turn_id": turn_id,
                "message": message,
            },
            send_lock,
        )
        assistant_sent_text = _message_content(message)
        if _should_emit_final_text(assistant_text, assistant_sent_text):
            assistant_sent_text = assistant_text
            await _send_json_best_effort(
                websocket,
                {
                    "type": "assistant.delta",
                    "scope": scope.to_dict(),
                    "text": assistant_text,
                    "turn_id": turn_id,
                    "accumulated": True,
                },
                send_lock,
            )

        after_projects = set(list_user_projects(username))
        for project in sorted(after_projects - before_projects):
            project_scope = ChatScope(kind="project", id=project)
            await chat_store.append_message_async(
                username,
                project_scope,
                "system",
                f"Created from home conversation turn {turn_id}.",
            )
            await _send_json_best_effort(
                websocket,
                {"type": "project.created", "project": project},
                send_lock,
            )

        done_sent = await _send_json_best_effort(
            websocket,
            {
                "type": "chat.done",
                "turn_id": turn_id,
                "scope": scope.to_dict(),
                "message": message,
            },
            send_lock,
        )
    finally:
        heartbeat_task.cancel()
        disconnect_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        with contextlib.suppress(asyncio.CancelledError):
            await disconnect_task
        await persist_partial_reply()
        if not done_sent:
            await _send_json_best_effort(
                websocket,
                {"type": "chat.done", "turn_id": turn_id, "scope": scope.to_dict()},
                send_lock,
            )


@router.websocket("/chat/ws")
async def chat_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    logger.info("chat websocket accepted client=%s", websocket.client)
    try:
        user = await _authenticate_ws(websocket)
    except Exception as exc:
        logger.warning(
            "chat websocket authentication failed client=%s", websocket.client
        )
        await _close_ws_setup_error(websocket, exc)
        return

    username = str(user["username"])
    try:
        current_scope = _scope_for_authenticated_user(user)
        _enforce_agent_chat_scope(user, current_scope, require_write=False)
        current_scope = await _send_scope_changed(
            websocket, user, username, current_scope
        )
    except Exception as exc:
        await _close_ws_setup_error(websocket, exc)
        return
    if current_scope is None:
        return
    logger.info(
        "chat websocket ready username=%s scope=%s:%s",
        username,
        current_scope.kind,
        current_scope.id or "",
    )
    await register_chat_websocket(websocket, username=username, scope=current_scope,
                                  browser_id=websocket.query_params.get("browser_id"),
                                  can_execute=user.get("credential_kind", "browser_session") in {"browser_session", "user_session"})
    # Do not pre-warm the default home scope on connect. The React client often
    # immediately sends scope.set for the active project; warming home first
    # creates a worker that is then rotated and logs a noisy initialize timeout.
    if _should_prewarm_on_ws_connect(current_scope):
        await _prewarm_chat_scope_if_available(
            user=user,
            username=username,
            scope=current_scope,
        )

    try:
        while True:
            try:
                raw = await websocket.receive_json()
            except RuntimeError as exc:
                if _is_websocket_disconnected_runtime_error(exc):
                    return
                raise
            event_type = str(raw.get("type") or "")
            logger.info(
                "chat websocket received type=%s username=%s", event_type, username
            )
            if event_type == "scope.set":
                msg = ScopeSetIn.model_validate(raw)
                requested_scope = _scope_from_model(msg.scope)
                try:
                    user = await _reauthenticate_ws_event(
                        websocket,
                        original_user=user,
                        scope=requested_scope,
                        require_write=False,
                    )
                except Exception as exc:
                    await _close_ws_setup_error(websocket, exc)
                    return
                current_scope = await _send_scope_changed(
                    websocket, user, username, requested_scope
                )
                if current_scope is None:
                    return
                await register_chat_websocket(
                    websocket,
                    username=username,
                    scope=current_scope,
                    browser_id=websocket.query_params.get("browser_id"),
                    can_execute=user.get("credential_kind", "browser_session") in {"browser_session", "user_session"},
                )
                await _sync_running_agent_scope(username, current_scope)
                # Switching project rotates the worker; warm the new scope now so
                # the first message in the project doesn't cold-start.
                await _prewarm_chat_scope_if_available(
                    user=user,
                    username=username,
                    scope=current_scope,
                )
                continue

            if event_type == "canvas.command.result":
                payload = CanvasCommandToolResultIn.model_validate(raw)
                await resolve_canvas_command_tool_result(payload, user)
                continue

            if event_type == "canvas.context.result":
                payload = CanvasContextToolResultIn.model_validate(raw)
                _resolve_canvas_context_tool_result_payload(
                    payload,
                    username=username,
                    project_state_dir=await _bridge_project_state_dir(user, payload),
                )
                continue

            if event_type == "skill_studio.result":
                payload = SkillStudioToolResultIn.model_validate(raw)
                logger.info(
                    "received skill_studio.result via ws %s",
                    _skill_studio_result_log_fields(payload, username=username),
                )
                resolved = _resolve_skill_studio_tool_result_payload(
                    payload,
                    username=username,
                    project_state_dir=await _bridge_project_state_dir(user, payload),
                )
                logger.info(
                    "resolved skill_studio.result via ws bridge_key=%s action=%s status=%s ok=%s saved=%s",
                    payload.bridge_key,
                    payload.action,
                    resolved.get("skill_studio_status"),
                    resolved.get("ok"),
                    resolved.get("saved_to_catalog"),
                )
                try:
                    await _persist_skill_studio_result_ui_event(
                        user=user,
                        username=username,
                        payload=payload,
                        resolved=resolved,
                    )
                except Exception:
                    logger.exception("failed to persist skill studio result ui event")
                continue

            if event_type == "assistant.clarification.result":
                payload = ClarificationToolResultIn.model_validate(raw)
                project_state_dir = await _bridge_project_state_dir(user, payload)
                resolved = await _resolve_clarification_with_reference_persistence(
                    payload=payload, user=user, username=username,
                    project_state_dir=project_state_dir,
                )
                try:
                    await _persist_clarification_result_ui_event(
                        user=user, username=username, payload=payload
                    )
                except Exception:
                    logger.exception("failed to persist clarification result ui event")
                continue

            if event_type != "chat.message":
                await _send_json_best_effort(
                    websocket,
                    {"type": "error", "message": f"unsupported event: {event_type}"},
                )
                continue

            msg = ChatMessageIn.model_validate(raw)
            scope = _scope_from_model(msg.scope) if msg.scope else current_scope
            turn_id = (msg.turn_id or "").strip() or uuid.uuid4().hex
            text = msg.text.strip()
            user_text = (msg.user_text or "").strip() or text
            logger.info(
                "chat message accepted username=%s turn_id=%s scope=%s:%s text_len=%d",
                username,
                turn_id,
                scope.kind,
                scope.id or "",
                len(text),
            )
            if not text:
                await _send_json_best_effort(
                    websocket,
                    {"type": "error", "turn_id": turn_id, "message": "empty message"},
                )
                continue

            try:
                user = await _reauthenticate_ws_event(
                    websocket,
                    original_user=user,
                    scope=scope,
                    require_write=True,
                )
            except Exception as exc:
                await _close_ws_setup_error(websocket, exc)
                return

            try:
                await _require_ai_assistant_access(user=user, scope=scope)
                if scope.kind in {"project", "freezone"}:
                    await _stream_project_turn(
                        websocket=websocket,
                        user=user,
                        username=username,
                        scope=scope,
                        text=text,
                        attachments=msg.attachments,
                        turn_id=turn_id,
                        user_text=user_text,
                        surface=("freezone" if _is_freezone_scope(scope) else None),
                        surface_context=(
                            msg.context if _is_freezone_scope(scope) else None
                        ),
                        store_scope=scope if _is_freezone_scope(scope) else None,
                    )
                elif scope.kind == "home":
                    await _stream_home_turn(
                        websocket=websocket,
                        user=user,
                        username=username,
                        scope=scope,
                        text=text,
                        attachments=msg.attachments,
                        turn_id=turn_id,
                    )
                else:
                    await _send_json_best_effort(
                        websocket,
                        {
                            "type": "error",
                            "turn_id": turn_id,
                            "message": f"scope not implemented: {scope.kind}",
                        },
                    )
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                logger.exception(
                    "chat turn failed username=%s turn_id=%s error_type=%s",
                    username,
                    turn_id,
                    type(exc).__name__,
                )
                if "当前用户已有 AI 对话正在处理中" in message:
                    await _send_json_best_effort(
                        websocket,
                        {
                            "type": "chat.busy",
                            "turn_id": turn_id,
                            "scope": scope.to_dict(),
                            "message": message,
                        },
                    )
                    continue
                billing_rule_error = find_billing_rule_not_configured_error(exc)
                if billing_rule_error is not None:
                    await _send_json_best_effort(
                        websocket,
                        {
                            "type": "error",
                            "turn_id": turn_id,
                            "message": BILLING_RULE_NOT_CONFIGURED_MESSAGE,
                            "data": billing_rule_not_configured_payload(
                                billing_rule_error
                            ),
                        },
                    )
                    continue
                insufficient_error = find_insufficient_credits_error(exc)
                if insufficient_error is not None:
                    await _send_json_best_effort(
                        websocket,
                        {
                            "type": "error",
                            "turn_id": turn_id,
                            "message": INSUFFICIENT_CREDITS_MESSAGE,
                            "data": insufficient_credits_payload(insufficient_error),
                        },
                    )
                    continue
                billing_error = find_billing_error(exc)
                if billing_error is not None:
                    await _send_json_best_effort(
                        websocket,
                        {
                            "type": "error",
                            "turn_id": turn_id,
                            "message": billing_error.user_message,
                            "data": billing_error_payload(billing_error),
                        },
                    )
                    continue
                logger.error(
                    "sending chat error username=%s turn_id=%s scope=%s backend=%s message=%s",
                    username,
                    turn_id,
                    scope.to_dict(),
                    (
                        chat_service.get_chat_backend_name()
                        if chat_service.is_chat_backend_available()
                        else "unavailable"
                    ),
                    message,
                )
                user_message = _user_facing_chat_error(exc)
                persisted_error = await _persist_chat_turn_error(
                    user=user,
                    username=username,
                    scope=scope,
                    turn_id=turn_id,
                    reason=user_message,
                )
                if persisted_error is not None:
                    await _send_json_best_effort(
                        websocket,
                        {
                            "type": "assistant.message",
                            "scope": scope.to_dict(),
                            "turn_id": turn_id,
                            "message": persisted_error,
                        },
                    )
                await _send_json_best_effort(
                    websocket,
                    {"type": "error", "turn_id": turn_id, "message": user_message},
                )
    except WebSocketDisconnect:
        logger.info("chat websocket disconnected username=%s", username)
        pass
    finally:
        await unregister_chat_websocket(websocket)
