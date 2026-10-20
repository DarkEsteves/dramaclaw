import pytest
from novelvideo.freezone import canvas_command_bridge as bridge


def origin(monkeypatch, actor="actor", turn="t"):
    for name, value in {
        "USERNAME": actor,
        "PROJECT_ID": "p",
        "CANVAS_ID": "c",
        "AGENT_ID": "main",
        "TURN_ID": turn,
    }.items():
        monkeypatch.setenv("DRAMACLAW_" + name, value)


def test_producer_identity_is_saved_before_browser_claim(tmp_path, monkeypatch):
    origin(monkeypatch)
    bridge.put_pending_canvas_context(
        key="k",
        project_id="p",
        canvas_id="c",
        requests=[],
        envelope={"requests": []},
        bridge_dir=tmp_path,
    )
    assert bridge.read_bridge_message("k", bridge_dir=tmp_path)["producer_binding"] == {
        "username": "actor",
        "project_id": "p",
        "canvas_id": "c",
        "agent_id": "main",
        "turn_id": "t",
    }
    assert not bridge.mark_bridge_message_delivered(
        "k",
        consumer_id="other",
        binding={
            "username": "other",
            "project_id": "p",
            "canvas_id": "c",
            "agent_id": "main",
            "turn_id": "t",
        },
        execution_token="other",
        bridge_dir=tmp_path,
    )


def test_active_browser_owner_survives_delivery_expiry_and_is_fenced(
    tmp_path, monkeypatch
):
    origin(monkeypatch)
    bridge.put_pending_canvas_context(
        key="k",
        project_id="p",
        canvas_id="c",
        requests=[],
        envelope={"requests": []},
        bridge_dir=tmp_path,
    )
    binding = {
        "username": "actor",
        "project_id": "p",
        "canvas_id": "c",
        "agent_id": "main",
        "turn_id": "t",
    }
    assert bridge.mark_bridge_message_delivered(
        "k",
        consumer_id="tab-a",
        binding=binding,
        execution_token="a",
        bridge_dir=tmp_path,
    )
    with bridge._bridge_db(tmp_path) as conn:
        conn.execute("UPDATE canvas_command_messages SET lease_expires_at=0")
    assert not bridge.mark_bridge_message_delivered(
        "k",
        consumer_id="tab-b",
        binding=binding,
        execution_token="b",
        bridge_dir=tmp_path,
    )
    assert bridge.renew_bridge_owner(
        "k", consumer_id="tab-a", execution_token="a", bridge_dir=tmp_path
    )
    bridge.release_bridge_owner(consumer_id="tab-a", bridge_dir=tmp_path)
    assert bridge.mark_bridge_message_delivered(
        "k",
        consumer_id="tab-b",
        binding=binding,
        execution_token="b",
        bridge_dir=tmp_path,
    )
    from novelvideo.chat.canvas_tool_session import validate_bridge_receipt
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        validate_bridge_receipt("k.executor.a", bridge_dir=tmp_path, **binding)
    validate_bridge_receipt("k.executor.b", bridge_dir=tmp_path, **binding)


def test_presence_is_exact_scope_and_expires(tmp_path):
    scope = {
        "username": "actor",
        "project_id": "p",
        "canvas_id": "c",
        "agent_id": "main",
    }
    assert not bridge.browser_is_online(binding=scope, bridge_dir=tmp_path)
    bridge.heartbeat_bridge_browser(
        consumer_id="tab", binding=scope, bridge_dir=tmp_path
    )
    assert bridge.browser_is_online(binding=scope, bridge_dir=tmp_path)
    assert not bridge.browser_is_online(
        binding={**scope, "username": "other"}, bridge_dir=tmp_path
    )
    with bridge._bridge_db(tmp_path) as conn:
        conn.execute("UPDATE canvas_bridge_browsers SET expires_at=0")
    assert not bridge.browser_is_online(binding=scope, bridge_dir=tmp_path)


def test_recovery_cursor_returns_complete_messages_and_mutated_event(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("NOVELVIDEO_STATE_DIR", str(tmp_path))
    from novelvideo.chat.store import ChatStore, ChatScope

    store = ChatStore()
    scope = ChatScope("project", "p", "freezone", "c", "main")
    first = store.append_message("actor", scope, "assistant", "finished", turn_id="t")
    event = store.append_ui_event(
        "actor",
        scope,
        "t",
        {"type": "assistant.message_parts", "parts": [{"type": "text", "text": "a"}]},
    )
    one = store.pull_recovery("actor", scope)
    assert one["messages"][0]["id"] == first["id"]
    store.append_ui_event(
        "actor",
        scope,
        "t",
        {"type": "assistant.message_parts", "parts": [{"type": "text", "text": "ab"}]},
    )
    two = store.pull_recovery(
        "actor", scope, cursor=one["cursor"], event_cursor=one["event_cursor"]
    )
    assert two["messages"] == []
    assert two["events"][0]["id"] == event["id"]


def test_clarification_cache_and_stable_command_keys_are_actor_scoped(
    tmp_path, monkeypatch
):
    origin(monkeypatch)
    bridge.put_pending_clarification_event(
        key="q",
        project_id="p",
        canvas_id="c",
        event={"clarification_id": "same"},
        bridge_dir=tmp_path,
    )
    key = bridge.canvas_command_idempotency_key(
        project_id="p", canvas_id="c", commands=[]
    )
    assert bridge.find_clarification_bridge_message(
        project_id="p", canvas_id="c", clarification_id="same", bridge_dir=tmp_path
    )
    origin(monkeypatch, actor="other")
    assert (
        bridge.find_clarification_bridge_message(
            project_id="p", canvas_id="c", clarification_id="same", bridge_dir=tmp_path
        )
        is None
    )
    assert (
        bridge.canvas_command_idempotency_key(
            project_id="p", canvas_id="c", commands=[]
        )
        != key
    )
    bridge.put_pending_canvas_command(
        key="write",
        project_id="p",
        canvas_id="c",
        commands=[],
        envelope={"commands": []},
        bridge_dir=tmp_path,
    )
    bridge.resolve_canvas_command(
        "write", {"ok": True, "applied": True}, bridge_dir=tmp_path
    )
    origin(monkeypatch)
    conflict = bridge.put_pending_canvas_command(
        key="write",
        project_id="p",
        canvas_id="c",
        commands=[],
        envelope={"commands": []},
        bridge_dir=tmp_path,
    )
    assert conflict["status"] == "canvas_command_idempotency_conflict"


def test_resolution_checks_owner_inside_transaction(tmp_path, monkeypatch):
    origin(monkeypatch)
    bridge.put_pending_canvas_context(
        key="k",
        project_id="p",
        canvas_id="c",
        requests=[],
        envelope={"requests": []},
        bridge_dir=tmp_path,
    )
    binding = {
        "username": "actor",
        "project_id": "p",
        "canvas_id": "c",
        "agent_id": "main",
        "turn_id": "t",
    }
    assert bridge.mark_bridge_message_delivered(
        "k", consumer_id="a", binding=binding, execution_token="a", bridge_dir=tmp_path
    )
    from novelvideo.chat.canvas_tool_session import validate_bridge_receipt

    token = validate_bridge_receipt("k.executor.a", bridge_dir=tmp_path, **binding)
    bridge.release_bridge_owner(consumer_id="a", bridge_dir=tmp_path)
    assert bridge.mark_bridge_message_delivered(
        "k", consumer_id="b", binding=binding, execution_token="b", bridge_dir=tmp_path
    )
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        bridge.resolve_canvas_context(
            "k", {"ok": True, "_bridge_execution_token": token}, bridge_dir=tmp_path
        )
    assert bridge.read_bridge_message("k", bridge_dir=tmp_path)["result"] is None


def test_persistent_hermes_producer_uses_unique_host_registered_turn(
    tmp_path, monkeypatch
):
    origin(monkeypatch)
    monkeypatch.delenv("DRAMACLAW_TURN_ID")
    binding = {
        "username": "actor",
        "project_id": "p",
        "canvas_id": "c",
        "agent_id": "main",
        "turn_id": "foreground",
    }
    bridge.register_bridge_producer_turn(binding=binding, bridge_dir=tmp_path)
    bridge.put_pending_canvas_context(
        key="hermes",
        project_id="p",
        canvas_id="c",
        requests=[],
        envelope={"requests": []},
        bridge_dir=tmp_path,
    )
    assert (
        bridge.read_bridge_message("hermes", bridge_dir=tmp_path)["producer_binding"]
        == binding
    )
    bridge.register_bridge_producer_turn(
        binding={**binding, "turn_id": "other-active"}, bridge_dir=tmp_path
    )
    bridge.put_pending_canvas_context(
        key="ambiguous",
        project_id="p",
        canvas_id="c",
        requests=[],
        envelope={"requests": []},
        bridge_dir=tmp_path,
    )
    assert not bridge.read_bridge_message("ambiguous", bridge_dir=tmp_path).get(
        "producer_binding"
    )
    bridge.release_bridge_producer_turn(
        binding={**binding, "turn_id": "other-active"}, bridge_dir=tmp_path
    )
    monkeypatch.setenv("DRAMACLAW_USERNAME", "other")
    bridge.put_pending_canvas_context(
        key="other-actor",
        project_id="p",
        canvas_id="c",
        requests=[],
        envelope={"requests": []},
        bridge_dir=tmp_path,
    )
    assert not bridge.read_bridge_message("other-actor", bridge_dir=tmp_path).get(
        "producer_binding"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("writer_kind", ["sync", "async"])
async def test_delayed_older_writer_is_recovered_after_newer_committed_cursor(
    tmp_path, monkeypatch, writer_kind
):
    import asyncio
    import threading
    from novelvideo.chat.store import ChatStore, ChatScope

    monkeypatch.setenv("NOVELVIDEO_STATE_DIR", str(tmp_path))
    delayed, other = ChatStore(), ChatStore()
    scope = ChatScope("project", "p", "freezone", "c", "main")
    original = other.append_ui_event(
        "actor",
        scope,
        "t",
        {
            "type": "assistant.message_parts",
            "parts": [{"type": "text", "text": "initial"}],
        },
    )
    entered = threading.Event()
    release = threading.Event()
    original_connect = delayed.connect
    original_async_connect = delayed.connect_async
    if writer_kind == "sync":

        def blocked_connect(*args, **kwargs):
            entered.set()
            assert release.wait(3)
            return original_connect(*args, **kwargs)

        monkeypatch.setattr(delayed, "connect", blocked_connect)
        future = asyncio.create_task(
            asyncio.to_thread(
                delayed.append_ui_event,
                "actor",
                scope,
                "t",
                {
                    "type": "assistant.message_parts",
                    "parts": [{"type": "text", "text": "later actual update"}],
                },
            )
        )
    else:

        async def blocked_connect(*args, **kwargs):
            entered.set()
            assert await asyncio.to_thread(release.wait, 3)
            return await original_async_connect(*args, **kwargs)

        monkeypatch.setattr(delayed, "connect_async", blocked_connect)
        future = asyncio.create_task(
            delayed.append_ui_event_async(
                "actor",
                scope,
                "t",
                {
                    "type": "assistant.message_parts",
                    "parts": [{"type": "text", "text": "later actual update"}],
                },
            )
        )
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        await other.append_ui_event_async(
            "actor",
            scope,
            "t",
            {"type": "progress", "value": "committed before delayed update"},
        )
        one = other.pull_recovery("actor", scope)
    finally:
        release.set()
    updated = await asyncio.wait_for(future, 3)
    two = other.pull_recovery("actor", scope, event_cursor=one["event_cursor"])
    assert [event["id"] for event in two["events"]] == [original["id"]]
    assert updated["id"] == original["id"]
    assert two["event_cursor"] != one["event_cursor"]


def test_recovery_only_decodes_returned_turn_attachments(tmp_path, monkeypatch):
    from novelvideo.chat import store as module
    monkeypatch.setenv("NOVELVIDEO_STATE_DIR", str(tmp_path))
    store, scope = module.ChatStore(), module.ChatScope("project", "p")
    for i in range(8):
        store.append_message("actor", scope, "assistant", str(i), turn_id=f"t{i}")
        store.append_ui_event("actor", scope, f"t{i}", {"type":"assistant.message_parts", "parts":[{"type":"text", "text":f"history-{i}"}]})
    page = store.pull_recovery("actor", scope)
    loads, queries = [], []
    original_loads, original_connect = module.json.loads, store.connect
    def decode(raw, *args, **kwargs):
        if isinstance(raw, str) and "history-" in raw:
            loads.append(raw)
        return original_loads(raw, *args, **kwargs)
    def connect(*args, **kwargs):
        db = original_connect(*args, **kwargs)
        db.set_trace_callback(queries.append)
        return db
    monkeypatch.setattr(module.json, "loads", decode)
    monkeypatch.setattr(store, "connect", connect)
    idle = store.pull_recovery("actor", scope, cursor=page["cursor"], event_cursor=page["event_cursor"])
    assert idle["messages"] == idle["events"] == []
    assert loads == []
    assert not any("SELECT id, turn_id" in sql for sql in queries)
    part = store.pull_recovery("actor", scope, cursor=3, event_cursor=page["event_cursor"], limit=2)
    assert [m["turn_id"] for m in part["messages"]] == ["t3", "t4"]
    assert len(loads) == 2
    assert all("history-3" in s or "history-4" in s for s in loads)
    assert any("WHERE turn_id IN" in sql for sql in queries)
    assert all(m["parts"] for m in part["messages"])


@pytest.mark.asyncio
async def test_revision_backfill_runs_once_for_sync_and_async_connections(tmp_path, monkeypatch):
    import sqlite3
    from novelvideo.chat.store import ChatStore, ChatScope
    store, scope = ChatStore(), ChatScope("project", "p", state_dir=str(tmp_path))
    path = store.db_for("actor", scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE chat_ui_events (id INTEGER PRIMARY KEY AUTOINCREMENT, turn_id TEXT NOT NULL, event_type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)")
        db.execute("INSERT INTO chat_ui_events VALUES (1,'old','example','{}','now')")
    queries = []
    connect = sqlite3.connect
    def traced(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(queries.append)
        return db
    monkeypatch.setattr(sqlite3, "connect", traced)
    with store.connect("actor", scope) as db:
        assert db.execute("SELECT COUNT(*) FROM chat_ui_event_revisions").fetchone()[0] == 1
    queries.clear()
    with store.connect("actor", scope): pass
    db = await store.connect_async("actor", scope)
    await db.close()
    assert not any("SELECT id FROM chat_ui_events" in sql for sql in queries)


@pytest.mark.asyncio
async def test_legacy_revision_migration_concurrent_writers_and_rollback(tmp_path, monkeypatch):
    import asyncio
    import json
    import sqlite3
    from novelvideo.chat.store import ChatStore, ChatScope
    store, scope = ChatStore(), ChatScope("project", "p", state_dir=str(tmp_path))
    path = store.db_for("actor", scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE chat_ui_events (id INTEGER PRIMARY KEY AUTOINCREMENT, turn_id TEXT NOT NULL, event_type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)")
        db.executemany("INSERT INTO chat_ui_events(turn_id,event_type,payload_json,created_at) VALUES (?,'progress','{}','now')", [(f'old-{i}',) for i in range(40)])
    def sync_writer(i):
        return ChatStore().append_ui_event('actor', scope, f'new-{i}', {'type':'progress', 'value':i})
    async def async_writer(i):
        return await ChatStore().append_ui_event_async('actor', scope, f'new-{i}', {'type':'progress', 'value':i})
    await asyncio.gather(*(asyncio.to_thread(sync_writer, i) if i % 2 else async_writer(i) for i in range(12)))
    with store.connect('actor', scope) as db:
        assert db.execute('SELECT COUNT(*) FROM chat_ui_event_revisions').fetchone()[0] == 52
        assert db.execute('SELECT COUNT(DISTINCT revision) FROM chat_ui_event_revisions').fetchone()[0] == 52
        before = db.execute('SELECT MAX(revision) FROM chat_ui_event_revisions').fetchone()[0]
        db.execute("UPDATE chat_ui_events SET payload_json='{}' WHERE id=1")
        db.rollback()
        assert db.execute('SELECT MAX(revision) FROM chat_ui_event_revisions').fetchone()[0] == before
    # Paginated callbacks to old turns must advance committed rN cursors even
    # with no new messages, then produce an empty stable-cursor idle page.
    first = store.pull_recovery('actor', scope, limit=200)
    with store.connect('actor', scope) as db:
        for i in range(1, 6):
            db.execute("UPDATE chat_ui_events SET payload_json=? WHERE id=?", (json.dumps({'value':f'updated-{i}'}), i))
    seen, cursor = [], first['event_cursor']
    while True:
        page = store.pull_recovery('actor', scope, event_cursor=cursor, limit=2)
        assert page['messages'] == []
        seen.extend(e['id'] for e in page['events'])
        cursor = page['event_cursor']
        if not page['has_more']: break
    assert seen == [1,2,3,4,5]
    idle = store.pull_recovery('actor', scope, event_cursor=cursor, limit=2)
    assert idle['events'] == [] and idle['event_cursor'] == cursor
