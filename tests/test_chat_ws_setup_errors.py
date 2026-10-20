import sqlite3

import pytest
from fastapi import HTTPException

from novelvideo.api.routes import chat


class Socket:
    client = "test-client"

    def __init__(self):
        self.frames = []
        self.code = None

    async def accept(self):
        pass

    async def send_json(self, frame):
        self.frames.append(frame)

    async def close(self, code):
        self.code = code


@pytest.mark.asyncio
async def test_history_storage_failure_is_not_unauthorized(monkeypatch):
    async def authenticate(ws):
        return {"id": "local", "username": "local"}

    async def history(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(chat, "_authenticate_ws", authenticate)
    monkeypatch.setattr(chat, "_send_scope_changed", history)
    ws = Socket()
    await chat.chat_ws(ws)
    assert ws.code == 1011
    assert "unauthorized" not in ws.frames[0]["message"]
    assert "存储" in ws.frames[0]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 503])
async def test_auth_denial_and_service_unavailability_are_distinct(monkeypatch, status):
    async def authenticate(ws):
        raise HTTPException(status_code=status, detail="safe detail")

    monkeypatch.setattr(chat, "_authenticate_ws", authenticate)
    ws = Socket()
    await chat.chat_ws(ws)
    assert ws.code == (1008 if status in (401, 403) else 1011)
    assert (ws.frames[0]["message"] == "unauthorized") == (status in (401, 403))
