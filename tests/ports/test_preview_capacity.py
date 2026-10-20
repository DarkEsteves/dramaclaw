"""CE previews retain a local limit; EE must install shared capacity."""

import asyncio
import sys

import pytest

from novelvideo.ports import get_preview_render_capacity
from novelvideo.ports import registry
from novelvideo.ports.preview_capacity import LocalPreviewCapacity, PreviewBusy, PreviewUnavailable


@pytest.mark.asyncio
async def test_ce_capacity_is_process_local(monkeypatch):
    monkeypatch.setenv("ST_EDITION", "ce")
    capacity = get_preview_render_capacity()
    async with capacity.slot(), capacity.slot():
        with pytest.raises(PreviewBusy):
            async with capacity.slot(wait_seconds=0.01):
                pytest.fail("third local preview entered")


def test_ee_capacity_missing_fails_closed(monkeypatch):
    monkeypatch.setenv("ST_EDITION", "ee")
    monkeypatch.delitem(registry._PORTS, "preview_render_capacity", raising=False)
    with pytest.raises(PreviewUnavailable):
        get_preview_render_capacity()


def test_ee_capacity_uses_registered_port(monkeypatch):
    monkeypatch.setenv("ST_EDITION", "ee")
    impl = object()
    monkeypatch.setitem(registry._PORTS, "preview_render_capacity", impl)
    assert get_preview_render_capacity() is impl


@pytest.mark.asyncio
async def test_cancelled_local_render_stops_worker_before_releasing_slot(tmp_path):
    capacity = LocalPreviewCapacity(
        limit=1,
        worker_command=(sys.executable, '-c', 'import time; time.sleep(30)'),
    )
    entered = asyncio.Event()

    async def render():
        async with capacity.slot():
            entered.set()
            await capacity.render_frame(None, {}, media_paths={}, frame=0,
                                        output_path=tmp_path / 'preview.png')

    task = asyncio.create_task(render())
    await entered.wait()
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    async with capacity.slot(wait_seconds=0.05):
        pass
