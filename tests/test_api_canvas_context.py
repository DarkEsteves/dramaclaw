import json
from pathlib import Path
from types import SimpleNamespace
import httpx
import pytest
from fastapi import FastAPI, HTTPException
from novelvideo.freezone import canvas_store

F = json.loads(
    (Path(__file__).parent / "fixtures/canvas-context-read.json").read_text()
)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from novelvideo.api.routes import canvas_context as route
    from novelvideo.api.auth import get_api_user

    ctx = SimpleNamespace(
        project_id="p",
        requester_user_id="u",
        requester_username="user",
        state_dir=tmp_path,
        is_home_node=True,
        home_node_id="local",
    )
    user = {"id": "u", "username": "user"}

    async def resolve(project, actor, **kwargs):
        if project != "p" or actor["id"] != "u":
            raise HTTPException(403, "denied")
        assert kwargs["require_home_node"] is True
        return ctx, "user", "project", tmp_path, tmp_path

    monkeypatch.setattr(route.freezone, "_resolve_freezone_project", resolve)
    monkeypatch.setattr(route.freezone, "_canvas_state_project_dir", lambda ctx, p: p)
    path = canvas_store.canvas_path(tmp_path, "c")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(F["canvas"]))
    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[get_api_user] = lambda: user
    return app, ctx, user, path


@pytest.mark.asyncio
async def test_read_real_saved_canvas_revision_no_write_receipt(setup, monkeypatch):
    app, ctx, user, path = setup
    before = path.read_bytes()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.post(
            "/projects/p/freezone/canvases/c/context/read",
            json={
                "requests": [{"type": "node_detail", "node_id": "analysis"}],
                "read_consistency": "saved",
                "expected_revision": "r7",
            },
        )
        assert result.status_code == 200
        data = result.json()["data"]
        from novelvideo.chat.dramaclaw_mcp import _normalize_structured_result
        from test_freezone_plugin import _load_plugin_module
        import sys
        with monkeypatch.context() as registry_patch:
            registry_patch.setitem(sys.modules, "tools", sys.modules.get("tools"))
            registry_patch.setitem(sys.modules, "tools.registry", sys.modules.get("tools.registry"))
            plugin = _load_plugin_module()
        structured = _normalize_structured_result(plugin._output_schema("freezone_get_node_detail"), result.json())
        assert structured["source"] == "saved_canvas"
        assert structured["canvas_revision"] == data["canvas_revision"]
        assert structured["coverage"] == data["coverage"]
        assert data["source"] == "saved_canvas"
        assert data["responses"][0]["data"]["nodes"][0]["reference_analysis"][
            "result_ref"
        ] == {"artifact_id": "blueprint", "revision": 2}
        assert "receipt" not in data
        assert (
            await client.post(
                "/projects/p/freezone/canvases/c/context/read",
                json={"requests": [], "expected_revision": "old"},
            )
        ).status_code == 409
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_project_private_missing_and_paths(setup):
    app, ctx, user, path = setup
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (
            await client.post(
                "/projects/other/freezone/canvases/c/context/read",
                json={"requests": []},
            )
        ).status_code == 403
        assert (
            await client.post(
                "/projects/p/freezone/canvases/missing/context/read",
                json={"requests": []},
            )
        ).status_code == 404
        path.write_text(
            json.dumps(
                {
                    **F["canvas"],
                    "access_model": "private",
                    "owner_principal_id": "other",
                }
            )
        )
        assert (
            await client.post(
                "/projects/p/freezone/canvases/c/context/read", json={"requests": []}
            )
        ).status_code == 403
        user["id"] = "other"
        assert (
            await client.post(
                "/projects/p/freezone/canvases/c/context/read", json={"requests": []}
            )
        ).status_code == 403


@pytest.mark.asyncio
async def test_agent_requires_durable_host_turn_binding(setup):
    from novelvideo.freezone.canvas_context_reader import saved_context_execution

    app, ctx, user, path = setup
    user.update(
        credential_kind="agent_session",
        current_scope_kind="project",
        current_project_id="p",
        scopes=["projects:read"],
    )
    identity = SimpleNamespace(
        surface="freezone",
        requester_user_id="u",
        project_id="p",
        canvas_id="c",
        agent_id="main",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        url = "/projects/p/freezone/canvases/c/context/read"
        assert (await client.post(url, json={"requests": []})).status_code == 403
        with saved_context_execution(
            execution_context=identity, turn_id="turn", state_dir=ctx.state_dir
        ):
            assert (
                await client.post(
                    url,
                    headers={
                        "X-DramaClaw-Turn-ID": "turn",
                        "X-DramaClaw-Agent-ID": "main",
                    },
                    json={"requests": []},
                )
            ).status_code == 200
            assert (
                await client.post(
                    url,
                    headers={
                        "X-DramaClaw-Turn-ID": "turn",
                        "X-DramaClaw-Agent-ID": "other",
                    },
                    json={"requests": []},
                )
            ).status_code == 403
            assert (
                await client.post(
                    "/projects/p/freezone/canvases/other/context/read",
                    headers={
                        "X-DramaClaw-Turn-ID": "turn",
                        "X-DramaClaw-Agent-ID": "main",
                    },
                    json={"requests": []},
                )
            ).status_code == 403
        assert (
            await client.post(
                url,
                headers={"X-DramaClaw-Turn-ID": "turn", "X-DramaClaw-Agent-ID": "main"},
                json={"requests": []},
            )
        ).status_code == 403


def test_central_agent_boundary_accepts_only_exact_read_endpoint():
    from novelvideo.api.auth import _enforce_agent_request_boundary
    from starlette.requests import Request

    user = {
        "credential_kind": "agent_session",
        "current_scope_kind": "project",
        "current_project_id": "p",
        "scopes": ["projects:read"],
    }
    _enforce_agent_request_boundary(
        Request(
            {
                "type": "http",
                "headers": [],
                "method": "POST",
                "path": "/projects/p/freezone/canvases/c/context/read",
            }
        ),
        user,
    )
    with pytest.raises(HTTPException):
        _enforce_agent_request_boundary(
            Request(
                {
                    "type": "http",
                    "headers": [],
                    "method": "POST",
                    "path": "/projects/p/freezone/canvases/c",
                }
            ),
            user,
        )


@pytest.mark.asyncio
async def test_decoded_object_cursor_returns_http_400(setup):
    import base64

    app, _, _, _ = setup
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.post(
            "/projects/p/freezone/canvases/c/context/read",
            json={
                "requests": [
                    {
                        "type": "canvas_summary",
                        "cursor": base64.urlsafe_b64encode(b"{}").decode(),
                    }
                ]
            },
        )
    assert result.status_code == 400
    assert result.json()["detail"] == "invalid or stale cursor"

@pytest.mark.asyncio
async def test_production_app_serves_plugin_canonical_saved_context_url(setup):
    from novelvideo.api.app import create_app
    from novelvideo.api.auth import get_api_user, _enforce_agent_request_boundary
    from starlette.requests import Request
    _, ctx, user, path = setup
    app = create_app()
    app.dependency_overrides[get_api_user] = lambda: user
    url = '/api/v1/projects/p/freezone/canvases/c/context/read'
    _enforce_agent_request_boundary(Request({'type': 'http', 'headers': [], 'method': 'POST', 'path': url}),
                                    {'credential_kind': 'agent_session', 'current_scope_kind': 'project', 'current_project_id': 'p', 'scopes': ['projects:read']})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post(url, json={'requests': [{'type': 'node_detail', 'node_id': 'analysis'}]})
    assert response.status_code == 200
    assert response.json()['data']['source'] == 'saved_canvas'
