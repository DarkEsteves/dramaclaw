from __future__ import annotations

import asyncio

import pytest

from novelvideo.chat.store import ChatScope, ChatStore


@pytest.mark.asyncio
async def test_stream_snapshots_replace_one_row_and_preserve_durable_parts(monkeypatch, tmp_path):
    monkeypatch.setenv("NOVELVIDEO_STATE_DIR", str(tmp_path))
    store = ChatStore()
    scope = ChatScope(kind="home")
    await store.append_message_async("alice", scope, "user", "hello", turn_id="one")
    context = {"id": "context-1", "type": "canvas_context", "event": {"type": "canvas_context_result"}}
    for index in range(20):
        parts = ([context] if index == 0 else []) + [
            {"id": "thought-1", "type": "agent_thought", "event": {"text": "x" * index}}
        ]
        await store.append_ui_event_async("alice", scope, "one", {"type": "assistant.message_parts", "parts": parts})
    store.append_ui_event("alice", scope, "one", {"type": "assistant.message_parts", "parts": []})
    store.append_ui_event("alice", scope, "one", {"type": "canvas_command_result", "ok": True})
    with store.connect("alice", scope) as conn:
        assert conn.execute("SELECT count(*) FROM chat_ui_events WHERE event_type='assistant.message_parts'").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM chat_ui_events WHERE event_type='canvas_command_result'").fetchone()[0] == 1
    history = await store.list_messages_async("alice", scope)
    assert history[0]["parts"] == [context, {"id": "thought-1", "type": "agent_thought", "event": {"text": "x" * 19}}]


@pytest.mark.asyncio
async def test_concurrent_snapshot_writers_keep_parts_and_turns_separate(monkeypatch, tmp_path):
    monkeypatch.setenv("NOVELVIDEO_STATE_DIR", str(tmp_path))
    store = ChatStore()
    scope = ChatScope(kind="home")
    await asyncio.gather(*[
        store.append_ui_event_async("alice", scope, turn, {
            "type": "assistant.message_parts", "parts": [{"id": f"part-{index}", "type": "tool_status"}]
        })
        for turn in ["one", "two"] for index in range(5)
    ])
    with store.connect("alice", scope) as conn:
        rows = conn.execute("SELECT turn_id,payload_json FROM chat_ui_events ORDER BY turn_id").fetchall()
    assert len(rows) == 2
    import json
    assert all(len(json.loads(row[1])["parts"]) == 5 for row in rows)
