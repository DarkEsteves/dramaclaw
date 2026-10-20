"""Background ACP warm-up cannot read the active turn's stdout stream."""

from __future__ import annotations

import asyncio

import pytest

from novelvideo.chat.hermes_sdk import HermesSdkThread
from novelvideo.chat.runtime_port import ChatBackendEvent


@pytest.mark.asyncio
async def test_warm_waits_until_the_active_stream_releases_stdio(tmp_path, monkeypatch):
    thread = HermesSdkThread(
        cli_path=tmp_path / "hermes", cwd=tmp_path, env={}, model=None,
        username="local", session_id="session-1",
    )
    started = asyncio.Event()
    release = asyncio.Event()
    prepared = asyncio.Event()

    async def stream_transport(*_args, **_kwargs):
        started.set()
        yield ChatBackendEvent(type="thread_started", thread_id="session-1")
        await release.wait()
        yield ChatBackendEvent(type="complete", thread_id="session-1")

    async def prepare_transport():
        prepared.set()

    monkeypatch.setattr(thread, "_stream_turn", stream_transport)
    monkeypatch.setattr(thread, "_prepare", prepare_transport)

    async def consume():
        return [event async for event in thread.stream("test")]

    turn = asyncio.create_task(consume())
    await started.wait()
    warm = asyncio.create_task(thread.warm())
    try:
        await asyncio.sleep(0)
        assert not prepared.is_set()
        release.set()
        await turn
        await warm
        assert prepared.is_set()
    finally:
        release.set()
        await asyncio.gather(turn, warm, return_exceptions=True)
