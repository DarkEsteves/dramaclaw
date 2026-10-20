import pytest
import httpx
from types import SimpleNamespace
from novelvideo.freezone import canvas_command_bridge as bridge, canvas_store


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from novelvideo.api.app import create_app
    from novelvideo.api.routes import chat, freezone
    from novelvideo.api.auth import get_api_user

    ctx = SimpleNamespace(
        project_id="p",
        requester_user_id="u",
        requester_username="actor",
        state_dir=tmp_path,
        output_dir=tmp_path,
        is_home_node=True,
        home_node_id="local",
    )
    user = {"id": "u", "username": "actor", "credential_kind": "browser_session"}

    async def resolve(project, actor, **kwargs):
        from fastapi import HTTPException

        if project != "p":
            raise HTTPException(403, "denied")
        return ctx, "actor", "p", tmp_path, tmp_path

    monkeypatch.setattr(freezone, "_resolve_freezone_project", resolve)
    monkeypatch.setattr(
        chat,
        "_candidate_canvas_bridge_dirs_for_scope",
        lambda *args: [tmp_path / "bridge"],
    )
    import json

    path = canvas_store.canvas_path(tmp_path, "c")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"project_id": "p", "canvas_id": "c", "nodes": [], "edges": []})
    )
    app = create_app()
    app.dependency_overrides[get_api_user] = lambda: user
    for key, value in {
        "USERNAME": "actor",
        "PROJECT_ID": "p",
        "CANVAS_ID": "c",
        "AGENT_ID": "main",
        "TURN_ID": "t",
    }.items():
        monkeypatch.setenv("DRAMACLAW_" + key, value)
    return app, ctx, user, tmp_path / "bridge"


@pytest.mark.asyncio
async def test_real_application_pull_claims_all_kinds_and_preserves_active_owner(setup):
    app, ctx, user, directory = setup
    bridge.put_pending_canvas_context(
        key="context",
        project_id="p",
        canvas_id="c",
        requests=[],
        envelope={"requests": []},
        bridge_dir=directory,
    )
    bridge.put_pending_canvas_command(
        key="command",
        project_id="p",
        canvas_id="c",
        commands=[],
        envelope={"commands": []},
        bridge_dir=directory,
    )
    bridge.put_pending_clarification_event(
        key="card",
        project_id="p",
        canvas_id="c",
        event={"type": "assistant.clarification.request", "clarification_id": "q"},
        bridge_dir=directory,
    )
    bridge.put_pending_skill_studio_event(
        key="skill",
        project_id="p",
        canvas_id="c",
        event={"type": "skill_studio.questions"},
        bridge_dir=directory,
    )
    from novelvideo.chat.store import ChatScope
    from novelvideo.api.routes import chat

    scope = ChatScope("project", "p", "freezone", "c", "main", str(ctx.state_dir))
    final = chat.chat_store.append_message(
        "actor", scope, "assistant", "actual completed delivery", turn_id="t"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        body = {
            "project_id": "p",
            "canvas_id": "c",
            "agent_id": "main",
            "browser_id": "a" * 32,
        }
        response = await client.post("/api/v1/chat/pending-interactions", json=body)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert len(data["frames"]) == 4
        assert data["messages"][0]["id"] == final["id"]
        assert all(
            ".executor." in frame["bridge_key"] and frame["turn_id"] == "t"
            for frame in data["frames"]
        )
        with bridge._bridge_db(directory) as conn:
            conn.execute("UPDATE canvas_command_messages SET lease_expires_at=0")
        other = await client.post(
            "/api/v1/chat/pending-interactions", json={**body, "browser_id": "b" * 32}
        )
        assert other.json()["data"]["frames"] == []
        user["credential_kind"] = "agent_session"
        assert (
            await client.post("/api/v1/chat/pending-interactions", json=body)
        ).status_code == 403
        user["credential_kind"] = "browser_session"
        path = canvas_store.canvas_path(ctx.state_dir, "c")
        import json

        canvas = json.loads(path.read_text())
        canvas.update(access_model="private", owner_principal_id="other")
        path.write_text(json.dumps(canvas))
        assert (
            await client.post("/api/v1/chat/pending-interactions", json=body)
        ).status_code == 403


@pytest.mark.asyncio
async def test_pull_replays_same_owned_alias_after_lost_http_response(setup):
    app, ctx, user, directory = setup
    bridge.put_pending_canvas_context(
        key="lost",
        project_id="p",
        canvas_id="c",
        requests=[],
        envelope={"requests": []},
        bridge_dir=directory,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        body = {
            "project_id": "p",
            "canvas_id": "c",
            "agent_id": "main",
            "browser_id": "a" * 32,
        }
        one = (
            await client.post("/api/v1/chat/pending-interactions", json=body)
        ).json()["data"]
        two = (
            await client.post("/api/v1/chat/pending-interactions", json=body)
        ).json()["data"]
        assert one["frames"] == two["frames"]
        bridge.resolve_canvas_context(
            "lost", {"ok": True, "responses": []}, bridge_dir=directory
        )
        assert (
            await client.post("/api/v1/chat/pending-interactions", json=body)
        ).json()["data"]["frames"] == []


@pytest.mark.asyncio
async def test_agent_presence_is_read_only_and_requires_exact_hosted_turn(
    setup, monkeypatch
):
    app, ctx, user, directory = setup
    from novelvideo.api.routes import canvas_context

    binding = {
        "requester_user_id": "u",
        "username": "actor",
        "project_id": "p",
        "canvas_id": "c",
        "agent_id": "main",
        "turn_id": "t",
    }
    monkeypatch.setattr(canvas_context, "saved_turn_binding", lambda *args: binding)
    user.update(
        credential_kind="agent_session",
        current_scope_kind="project",
        current_project_id="p",
        scopes=["projects:read"],
    )
    headers = {"X-DramaClaw-Turn-ID": "t", "X-DramaClaw-Agent-ID": "main"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        url = "/api/v1/projects/p/freezone/canvases/c/context/browser-presence"
        offline = await client.get(url, headers=headers)
        assert offline.status_code == 200, offline.text
        assert offline.json()["data"]["online"] is False
        bridge.heartbeat_bridge_browser(
            consumer_id="human",
            binding={
                k: binding[k]
                for k in ["username", "project_id", "canvas_id", "agent_id"]
            },
            bridge_dir=directory,
        )
        assert (await client.get(url, headers=headers)).json()["data"]["online"] is True
        assert (
            await client.get(url, headers={**headers, "X-DramaClaw-Agent-ID": "other"})
        ).status_code == 403


@pytest.mark.asyncio
async def test_pull_recovers_actual_receipt_without_notification_after_worker_crash(
    setup,
):
    app, ctx, user, directory = setup
    bridge.put_pending_canvas_context(
        key="completed",
        project_id="p",
        canvas_id="c",
        requests=[],
        envelope={"requests": []},
        bridge_dir=directory,
    )
    actual = bridge.resolve_canvas_context(
        "completed",
        {
            "ok": True,
            "responses": [{"type": "selection_detail", "data": {"node_ids": ["real"]}}],
        },
        bridge_dir=directory,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/chat/pending-interactions",
            json={"project_id": "p", "canvas_id": "c", "browser_id": "a" * 32},
        )
    data = response.json()["data"]
    assert data["frames"] == []
    assert data["receipts"] == [
        {
            "request_id": "completed",
            "kind": "canvas_context",
            "turn_id": "t",
            "status": "applied",
            "result": actual,
        }
    ]


@pytest.mark.asyncio
async def test_pull_never_adopts_other_actor_agent_or_unverified_legacy(
    setup, monkeypatch
):
    app, ctx, user, directory = setup
    for key, actor, agent in [
        ("peer", "other", "main"),
        ("other-agent", "actor", "peer-agent"),
    ]:
        monkeypatch.setenv("DRAMACLAW_USERNAME", actor)
        monkeypatch.setenv("DRAMACLAW_AGENT_ID", agent)
        bridge.put_pending_canvas_context(
            key=key,
            project_id="p",
            canvas_id="c",
            requests=[],
            envelope={"requests": []},
            bridge_dir=directory,
        )
    monkeypatch.delenv("DRAMACLAW_USERNAME")
    bridge.put_pending_canvas_context(
        key="unverified",
        project_id="p",
        canvas_id="c",
        requests=[],
        envelope={"requests": []},
        bridge_dir=directory,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/chat/pending-interactions",
            json={
                "project_id": "p",
                "canvas_id": "c",
                "agent_id": "main",
                "browser_id": "a" * 32,
            },
        )
    assert response.status_code == 200
    assert response.json()["data"]["frames"] == []
    for key in ["peer", "other-agent", "unverified"]:
        assert (
            bridge.read_bridge_message(key, bridge_dir=directory)["transport_status"]
            == "pending"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("peer_field",["USERNAME","AGENT_ID"])
async def test_canonical_pull_filters_producers_before_bounded_pending_and_receipt_pages(
    setup, monkeypatch, peer_field
):
    app, ctx, user, directory = setup
    # Own receipt is older than500newer peer receipts; own pending request is
    # newer than100older peer pending rows in the shared project/profile inbox.
    bridge.put_pending_canvas_context(
        key="own-result",
        project_id="p",
        canvas_id="c",
        requests=[],
        envelope={"requests": []},
        bridge_dir=directory,
    )
    bridge.resolve_canvas_context(
        "own-result", {"ok": True, "responses": []}, bridge_dir=directory
    )
    monkeypatch.setenv("DRAMACLAW_"+peer_field, "peer")
    for number in range(500):
        key = f"peer-result-{number}"
        bridge.put_pending_canvas_context(
            key=key,
            project_id="p",
            canvas_id="c",
            requests=[],
            envelope={"requests": []},
            bridge_dir=directory,
        )
        bridge.resolve_canvas_context(
            key, {"ok": True, "responses": []}, bridge_dir=directory
        )
    for number in range(100):
        bridge.put_pending_canvas_context(
            key=f"peer-pending-{number}",
            project_id="p",
            canvas_id="c",
            requests=[],
            envelope={"requests": []},
            bridge_dir=directory,
        )
    monkeypatch.setenv("DRAMACLAW_"+peer_field, "actor" if peer_field=="USERNAME" else "main")
    bridge.put_pending_canvas_context(
        key="own-pending",
        project_id="p",
        canvas_id="c",
        requests=[],
        envelope={"requests": []},
        bridge_dir=directory,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/chat/pending-interactions",
            json={
                "project_id": "p",
                "canvas_id": "c",
                "agent_id": "main",
                "browser_id": "a" * 32,
            },
        )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (
        [frame["request_id"] for frame in data["frames"]],
        [receipt["request_id"] for receipt in data["receipts"]],
    ) == (["own-pending"], ["own-result"])


@pytest.mark.asyncio
async def test_recovery_pages_messages_and_old_turn_real_clarification_callback(setup, monkeypatch):
    import json
    from novelvideo.api.routes import chat
    from novelvideo.chat.store import ChatScope
    app, ctx, user, directory = setup
    ctx.project_name = 'p'
    scope = ChatScope('project', 'p', 'freezone', 'c', 'main', str(ctx.state_dir))
    async def state(*args, **kwargs): return ctx.state_dir
    async def context(*args, **kwargs): return ctx
    monkeypatch.setattr(chat, '_bridge_project_state_dir', state)
    monkeypatch.setattr(chat, '_project_context_for_interaction_result', context)
    monkeypatch.setattr(chat, '_canvas_bridge_dir', lambda *args, **kwargs: directory)
    with chat.chat_store.connect('actor', scope) as db:
        for i in range(101):
            turn = 't' if i == 0 else f't{i}'
            db.execute("INSERT INTO chat_messages(role,content,media_json,metadata_json,turn_id,created_at) VALUES ('assistant',?,'[]','{}',?,'now')", (str(i),turn))
            db.execute("INSERT INTO chat_ui_events(turn_id,event_type,payload_json,created_at) VALUES (?,?,?,'now')", (turn,'assistant.message_parts',json.dumps({'type':'assistant.message_parts','parts':[{'type':'text','text':str(i)}]})))
    bridge.put_pending_clarification_event(key='page-question', project_id='p', canvas_id='c', event={'type':'assistant.clarification.request','clarification_id':'original','questions':[{'id':'choice','title':'原问题'}]}, bridge_dir=directory)
    body = {'project_id':'p','canvas_id':'c','agent_id':'main','browser_id':'a'*32}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        async def pull(**cursor):
            response = await client.post('/api/v1/chat/pending-interactions',json={**body,**cursor})
            assert response.status_code == 200, response.text
            return response.json()['data']
        one = await pull()
        assert len(one['messages']) == len(one['events']) == 100
        assert one['has_more']
        assert all(message.get('parts') for message in one['messages'])
        frame = one['frames'][0]
        two = await pull(cursor=one['cursor'],event_cursor=one['event_cursor'])
        assert len(two['messages']) == 1
        assert two['messages'][0]['parts'][0]['text'] == '100'
        response = await client.post('/api/v1/chat/clarification-tool-result',json={'bridge_key':frame['bridge_key'],'turn_id':'t','project_id':'p','canvas_id':'c','agent_id':'main','answers':{'choice':'actual human'}})
        assert response.status_code == 200, response.text
        updated = await pull(cursor=two['cursor'],event_cursor=two['event_cursor'])
        assert updated['messages'] == []
        assert len(updated['events']) == 1
        assert updated['events'][0]['turn_id'] == 't'
        assert updated['events'][0]['payload']['answers'] == {'choice':'actual human'}
        idle = await pull(cursor=updated['cursor'],event_cursor=updated['event_cursor'])
        assert idle['messages'] == idle['events'] == []
        assert idle['event_cursor'] == updated['event_cursor']
