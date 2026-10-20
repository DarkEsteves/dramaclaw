"""Best-effort live chat event fan-out for server-originated messages."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections import defaultdict
from typing import Any

from fastapi import WebSocket

from novelvideo.chat.store import ChatScope


_connections: dict[tuple[str, str, str, str, str], set[WebSocket]] = defaultdict(set)
_connection_keys: dict[WebSocket, tuple[str, str, str, str, str]] = {}
_lock = asyncio.Lock()
_browser_ids: dict[Any, str] = {}
_browser_dirs: dict[Any, list[Any]] = {}
_execution_allowed: dict[Any, bool] = {}


def browser_consumer_id(*, username: str, scope: ChatScope, browser_id: str) -> str:
    binding = {"username":username,"project_id":scope.id,"canvas_id":scope.canvas_id,"agent_id":scope.agent_id or "main"}
    return "browser:" + hashlib.sha256(json.dumps([binding,browser_id],sort_keys=True).encode()).hexdigest()


def heartbeat_chat_browser(websocket, *, username: str, scope: ChatScope, bridge_dirs=None) -> str:
    from novelvideo.freezone.canvas_command_bridge import heartbeat_bridge_browser, renew_browser_owned_messages
    if websocket not in _connection_keys or not _execution_allowed.get(websocket, False):
        return ""
    consumer = _browser_ids.setdefault(websocket, "websocket:" + uuid.uuid4().hex)
    if bridge_dirs is None:
        from novelvideo.api.routes.chat import _candidate_canvas_bridge_dirs_for_scope
        bridge_dirs = _candidate_canvas_bridge_dirs_for_scope(username, scope)
    _browser_dirs[websocket] = bridge_dirs
    binding = {"username": username, "project_id": scope.id, "canvas_id": scope.canvas_id,
               "agent_id": scope.agent_id or "main"}
    for directory in bridge_dirs:
        heartbeat_bridge_browser(consumer_id=consumer, binding=binding, bridge_dir=directory)
        renew_browser_owned_messages(consumer_id=consumer, bridge_dir=directory)
    return consumer


def _scope_key(username: str, scope: ChatScope) -> tuple[str, str, str, str, str]:
    surface = scope.surface or ("director" if scope.kind == "project" else "")
    return (username, str(scope.id or ""), surface,
            str(scope.canvas_id or "") if surface == "freezone" else "",
            str(scope.agent_id or "main") if surface == "freezone" else "")


async def register_chat_websocket(
    websocket: WebSocket,
    *,
    username: str,
    scope: ChatScope,
    browser_id: str | None = None,
    can_execute: bool = True,
) -> None:
    if websocket in _connection_keys:
        await unregister_chat_websocket(websocket)
    async with _lock:
        previous = _connection_keys.pop(websocket, None)
        if previous is not None:
            _connections[previous].discard(websocket)
            if not _connections[previous]:
                _connections.pop(previous, None)
        key = _scope_key(username, scope)
        _connections[key].add(websocket)
        _connection_keys[websocket] = key
        _execution_allowed[websocket] = can_execute
        if browser_id and re.fullmatch(r"[A-Za-z0-9_-]{16,128}",browser_id):
            _browser_ids[websocket] = browser_consumer_id(username=username,scope=scope,browser_id=browser_id)
    heartbeat_chat_browser(websocket, username=username, scope=scope)


async def unregister_chat_websocket(websocket: WebSocket) -> None:
    from novelvideo.freezone.canvas_command_bridge import release_bridge_owner
    consumer = _browser_ids.pop(websocket, None)
    _execution_allowed.pop(websocket,None)
    for directory in _browser_dirs.pop(websocket, []):
        if consumer:
            release_bridge_owner(consumer_id=consumer, bridge_dir=directory)
    async with _lock:
        key = _connection_keys.pop(websocket, None)
        if key is None:
            return
        _connections[key].discard(websocket)
        for owner_key, owner in list(_execution_owners.items()):
            if owner[0] is websocket:
                _execution_owners.pop(owner_key, None)
        if not _connections[key]:
            _connections.pop(key, None)


async def broadcast_project_chat_event(
    *,
    username: str,
    project_id: str,
    payload: dict[str, Any],
) -> None:
    await broadcast_chat_scope_event(
        username=username,
        scope=ChatScope(kind="project", id=project_id, surface="director"),
        payload=payload,
    )


async def broadcast_chat_scope_event(
    *,
    username: str,
    scope: ChatScope,
    payload: dict[str, Any],
) -> None:
    """Fan out a server event within its exact authenticated chat scope."""
    key = _scope_key(username, scope)
    async with _lock:
        sockets = list(_connections.get(key, ()))
    stale: list[WebSocket] = []
    for websocket in sockets:
        try:
            await websocket.send_json(payload)
        except Exception:
            stale.append(websocket)
    for websocket in stale:
        await unregister_chat_websocket(websocket)


async def deliver_chat_scope_event(*, username: str, scope: ChatScope, payload: dict[str, Any]) -> str:
    """Send an executable bridge frame to exactly one authenticated connection."""
    async with _lock:
        sockets = list(_connections.get(_scope_key(username, scope), ()))
    alias = str(payload.get("bridge_key") or "")
    if ".executor." in alias:
        original, token = alias.rsplit(".executor.", 1)
        async with _lock:
            owner = _execution_owners.get((*_scope_key(username, scope), original))
        if not owner or owner[1] != token or owner[0] not in sockets:
            return "send_failed"
        sockets = [owner[0]]
    if not sockets:
        return "no_connection"
    for websocket in sockets:
        try:
            await websocket.send_json(payload)
            return "sent"
        except Exception:
            await unregister_chat_websocket(websocket)
    return "send_failed"


_execution_owners: dict[tuple[Any, ...], tuple[WebSocket, str]] = {}

async def reserve_chat_executor(*, username: str, scope: ChatScope, bridge_key: str, preferred=None):
    """Keep a live executor sticky; only disconnected owners can be superseded."""
    key = (*_scope_key(username, scope), bridge_key)
    async with _lock:
        sockets = {socket for socket in _connections.get(_scope_key(username, scope), set()) if _execution_allowed.get(socket,False)}
        previous = _execution_owners.get(key)
        if previous and previous[0] in sockets:
            return previous[0], previous[1], False
        socket = preferred if preferred in sockets else next(iter(sockets), None)
        if socket is None:
            return None, None, False
        token = uuid.uuid4().hex
        _execution_owners[key] = (socket, token)
        return socket, token, True

async def release_chat_executor(*, username: str, scope: ChatScope, bridge_key: str, token: str):
    key = (*_scope_key(username, scope), bridge_key)
    async with _lock:
        current = _execution_owners.get(key)
        if current and current[1] == token:
            _execution_owners.pop(key, None)


async def prune_chat_executors(*, username: str, scope: ChatScope, is_terminal) -> None:
    """Drop finished request reservations, preserving live unanswered execution."""
    prefix = _scope_key(username, scope)
    async with _lock:
        candidates = [(key, owner) for key, owner in _execution_owners.items() if key[:-1] == prefix]
    terminal = [(key, owner) for key, owner in candidates if is_terminal(key[-1])]
    async with _lock:
        for key, owner in terminal:
            if _execution_owners.get(key) == owner:
                _execution_owners.pop(key, None)
