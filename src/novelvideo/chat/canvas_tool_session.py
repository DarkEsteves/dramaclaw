"""Scope-bound durable canvas bridge delivery; never receives websocket frames."""
from __future__ import annotations

import asyncio
import logging
import hashlib
import time
from contextlib import suppress
from typing import Any, Awaitable, Callable

from novelvideo.chat.execution_context import AgentExecutionContext
from novelvideo.chat.store import ChatScope, chat_store
from novelvideo.project_context import ProjectContext, require_project_home_node
from novelvideo.freezone.canvas_command_bridge import (
    list_pending_bridge_messages, mark_bridge_message_delivered, release_bridge_delivery,
    read_bridge_message, canonical_bridge_key,
)

logger = logging.getLogger(__name__)

_FRAMES = {"canvas_command": "canvas.command", "canvas_context": "canvas.context.request",
           "skill_studio_event": "skill_studio.event", "clarification_event": "assistant.clarification.event"}

async def dispatch_pending_bridge_messages(*, username: str, scope: ChatScope, turn_id: str,
        bridge_dirs: list[Any], send: Callable[[dict[str, Any]], Awaitable[bool]],
        emitted_bridge_keys: set[str], kinds: set[str] | None = None, store_scope: ChatScope | None = None, started_at: float | None = None, scoped_executor: bool = False, executor_websocket=None, on_delivery_status: Callable[[str], None] | None = None, lease_valid: Callable[[], bool] | None = None) -> None:
    if lease_valid is not None and not lease_valid():
        if on_delivery_status:
            on_delivery_status("lease_lost")
        return
    from novelvideo.freezone.canvas_command_bridge import register_bridge_producer_turn
    producer = {"username":username,"project_id":scope.id,"canvas_id":scope.canvas_id,
                "agent_id":scope.agent_id or "main","turn_id":turn_id}
    for directory in bridge_dirs:
        register_bridge_producer_turn(binding=producer,bridge_dir=directory)
    consumer = f"websocket:{username}:{turn_id}"
    if scoped_executor:
        from novelvideo.chat.live_events import heartbeat_chat_browser, _connections, _scope_key
        for socket in list(_connections.get(_scope_key(username, scope), ())):
            heartbeat_chat_browser(socket, username=username, scope=scope, bridge_dirs=bridge_dirs)
        await prune_terminal_executors(username=username, scope=scope, bridge_dirs=bridge_dirs)
    for directory in bridge_dirs:
        pending_items = list_pending_bridge_messages(project_id=str(scope.id or ""),
            canvas_id=str(scope.canvas_id or ""), kinds=kinds or set(_FRAMES), bridge_dir=directory, created_after=started_at, producer_binding=producer if scoped_executor else None)
        for pending in pending_items:
            if lease_valid is not None and not lease_valid():
                if on_delivery_status:
                    on_delivery_status("lease_lost")
                return
            expected_binding = {"username": username, "project_id": scope.id, "canvas_id": scope.canvas_id,
                                "agent_id": scope.agent_id or "main", "turn_id": turn_id}
            if scoped_executor and pending.get("producer_binding") != expected_binding:
                continue
            if pending.get("delivery_binding") and pending["delivery_binding"] != expected_binding:
                continue
            key, kind = str(pending.get("key") or ""), pending["kind"]
            content_key = "envelope" if kind in {"canvas_command", "canvas_context"} else "event"
            content = pending.get(content_key)
            if not key or not isinstance(content, dict):
                continue
            token = None
            if scoped_executor:
                from novelvideo.chat.live_events import reserve_chat_executor, release_chat_executor
                socket, token, is_new = await reserve_chat_executor(username=username, scope=scope,
                    bridge_key=key, preferred=executor_websocket)
                if not socket:
                    if on_delivery_status:
                        on_delivery_status("no_connection")
                    continue
                if not is_new:
                    continue
                consumer = heartbeat_chat_browser(socket, username=username, scope=scope, bridge_dirs=bridge_dirs)
            # A lease expiry must permit retry even in the same session.
            if not mark_bridge_message_delivered(key, consumer_id=consumer, bridge_dir=directory,
                    binding=expected_binding, execution_token=token):
                if token:
                    await release_chat_executor(username=username, scope=scope, bridge_key=key, token=token)
                continue
            if lease_valid is not None and not lease_valid():
                release_bridge_delivery(key, consumer_id=consumer, bridge_dir=directory)
                if token:
                    await release_chat_executor(username=username, scope=scope, bridge_key=key, token=token)
                if on_delivery_status:
                    on_delivery_status("lease_lost")
                return
            frame = {"type": _FRAMES[kind], "scope": scope.to_dict(), "turn_id": turn_id, "project_id": scope.id,
                "canvas_id": scope.canvas_id, "agent_id": scope.agent_id or "main", "request_id":key, "bridge_key": f"{key}.executor.{token}" if token else key,
                content_key: content}
            started = time.monotonic()
            try:
                sent = await send(frame)
            except BaseException:
                release_bridge_delivery(key, consumer_id=consumer, bridge_dir=directory)
                if token:
                    await release_chat_executor(username=username, scope=scope, bridge_key=key, token=token)
                raise
            if lease_valid is not None and not lease_valid():
                release_bridge_delivery(key, consumer_id=consumer, bridge_dir=directory)
                if token:
                    await release_chat_executor(username=username, scope=scope, bridge_key=key, token=token)
                if on_delivery_status:
                    on_delivery_status("lease_lost")
                return
            if not sent:
                release_bridge_delivery(key, consumer_id=consumer, bridge_dir=directory)
                if token:
                    await release_chat_executor(username=username, scope=scope, bridge_key=key, token=token)
                continue
            logger.debug("Canvas bridge delivery turn=%s bridge_digest=%s source=%s version=%s attempts=%d elapsed_ms=%.1f error_code=%s",
                turn_id, hashlib.sha256(key.encode()).hexdigest()[:16], kind,
                content.get("version") if isinstance(content.get("version"), int) else None,
                int(pending.get("delivery_attempts", 0)) + 1, (time.monotonic()-started)*1000, "")
            emitted_bridge_keys.add(key)
            if content_key == "event":
                try:
                    await chat_store.append_ui_event_async(username, store_scope or scope, turn_id,
                        {**content, "bridge_key": key, "project_id": scope.id, "canvas_id": scope.canvas_id,
                         "agent_id": scope.agent_id or "main", "turn_id": turn_id})
                except Exception:
                    logger.exception("failed to persist canvas bridge UI event")


class CanvasToolSession:
    """Lifecycle for background delivery using already resolved trusted project identity."""
    def __init__(self, *, execution_context: AgentExecutionContext, project: ProjectContext,
                 scope: ChatScope, turn_id: str, send=None,
                 lease_valid: Callable[[], bool] | None = None):
        require_project_home_node(project, operation="canvas tool delivery")
        if execution_context != AgentExecutionContext.from_project_scope(scope=scope, project=project):
            raise ValueError("canvas tool scope does not match execution identity")
        self.execution_context, self.project, self.scope, self.turn_id = execution_context, project, scope, turn_id
        self.started_at = time.time()
        self.emitted_bridge_keys: set[str] = set()
        self.delivery_status: str | None = None
        self.task = None
        self.send = send or self._send
        self.lease_valid = lease_valid

    def _set_delivery_status(self, status: str) -> None:
        self.delivery_status = status

    async def _send(self, frame):
        from novelvideo.chat.live_events import deliver_chat_scope_event
        self.delivery_status = await deliver_chat_scope_event(username=self.project.requester_username, scope=self.scope, payload=frame)
        return self.delivery_status == "sent"

    async def __aenter__(self):
        if self.lease_valid is not None and not self.lease_valid():
            raise ValueError("reference continuation lease was lost")
        from novelvideo.api.routes.chat import _canvas_bridge_dir
        self.bridge_dirs = [_canvas_bridge_dir(self.project.requester_username,
            profile=self.execution_context.agent_profile, project_state_dir=self.project.state_dir)]
        from novelvideo.freezone.canvas_command_bridge import register_bridge_producer_turn
        for directory in self.bridge_dirs:
            register_bridge_producer_turn(binding={"username":self.project.requester_username,"project_id":self.scope.id,
                "canvas_id":self.scope.canvas_id,"agent_id":self.scope.agent_id or "main","turn_id":self.turn_id},bridge_dir=directory)
        self.task = asyncio.create_task(self._run())
        return self

    async def _run(self):
        while True:
            await dispatch_pending_bridge_messages(username=self.project.requester_username, scope=self.scope,
                turn_id=self.turn_id, bridge_dirs=self.bridge_dirs, send=self.send,
                emitted_bridge_keys=self.emitted_bridge_keys, started_at=self.started_at,
                scoped_executor=True, on_delivery_status=self._set_delivery_status,
                lease_valid=self.lease_valid)
            if self.delivery_status == "lease_lost":
                return
            await asyncio.sleep(0.4)

    def receipts(self) -> list[dict[str, Any]]:
        return [message for directory in self.bridge_dirs for key in self.emitted_bridge_keys
                if (message := read_bridge_message(key, bridge_dir=directory)) and message.get("result")]

    async def __aexit__(self, exc_type, exc, tb):
        self.task.cancel()
        try:
            with suppress(asyncio.CancelledError):
                await self.task
        finally:
            from novelvideo.freezone.canvas_command_bridge import release_bridge_producer_turn
            for directory in self.bridge_dirs:
                release_bridge_producer_turn(binding={"username":self.project.requester_username,"project_id":self.scope.id,
                    "canvas_id":self.scope.canvas_id,"agent_id":self.scope.agent_id or "main","turn_id":self.turn_id},bridge_dir=directory)
            await prune_terminal_executors(username=self.project.requester_username,
                scope=self.scope, bridge_dirs=self.bridge_dirs)


def validate_bridge_receipt(key: str, *, bridge_dir, username: str, project_id,
                            canvas_id, agent_id, turn_id) -> str | None:
    """Only the saved delivery identity may acknowledge a hosted bridge request."""
    from fastapi import HTTPException
    message = read_bridge_message(key, bridge_dir=bridge_dir)
    if ".executor." in key and (message is None or not message.get("execution_token")):
        raise HTTPException(status_code=409, detail="bridge executor receipt has no current durable owner")
    token = message.get("execution_token") if message else None
    if token and key != f"{canonical_bridge_key(key)}.executor.{token}":
        raise HTTPException(status_code=409, detail="bridge receipt executor was superseded")
    binding = message.get("delivery_binding") if message else None
    if not binding:  # Legacy requests retain their established callback protocol.
        return
    if message.get("transport_status") != "delivered" and not message.get("result"):
        raise HTTPException(status_code=409, detail="bridge delivery no longer has a live owner")
    actual = {"username": username, "project_id": project_id, "canvas_id": canvas_id,
              "agent_id": agent_id or "main", "turn_id": turn_id}
    if binding != actual:
        raise HTTPException(status_code=409, detail="bridge receipt does not match the saved delivery scope and turn")
    return token


async def prune_terminal_executors(*, username: str, scope: ChatScope, bridge_dirs: list[Any]) -> None:
    from novelvideo.chat.live_events import prune_chat_executors
    def is_terminal(key: str) -> bool:
        messages = [message for directory in bridge_dirs
                    if (message := read_bridge_message(key, bridge_dir=directory))]
        return not messages or all(message["transport_status"] in {"applied", "failed", "cancelled", "expired"}
                                      for message in messages)
    await prune_chat_executors(username=username, scope=scope, is_terminal=is_terminal)
