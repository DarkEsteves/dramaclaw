from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest


BASE = "/api/v1/projects/demo/freezone/remix/artifacts"


def evidence_payload() -> dict:
    return {
        "media_hash": "sha256:reference",
        "time_base": "1/90000",
        "duration_us": 1_000_000,
        "cuts": [],
        "representative_frames": [],
    }


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    from novelvideo.api.auth import get_api_user
    from novelvideo.api.routes import remix as routes

    role = {"value": "editor", "home": True}

    async def resolve(*, user, project_id, required_role):
        if project_id != "demo" or (required_role == "editor" and role["value"] != "editor"):
            raise HTTPException(403, "Access denied")
        return SimpleNamespace(
            output_dir=str(tmp_path), project_id="demo", owner_username="alice",
            project_name="demo", is_home_node=role["home"], home_node_id="other", current_node_id="local",
        )

    def home(ctx, **kwargs):
        if not ctx.is_home_node:
            raise HTTPException(409, "Wrong home node")

    monkeypatch.setattr(routes, "resolve_project_context", resolve)
    monkeypatch.setattr(routes, "require_project_home_node", home)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    app.dependency_overrides[get_api_user] = lambda: {"username": "alice"}
    return TestClient(app), role


def test_artifact_create_read_and_revise(client):
    api, _ = client
    created = api.post(BASE, json={"schema": "reference_evidence.v1", "payload": evidence_payload(), "upstream_refs": []})
    assert created.status_code == 200
    artifact = created.json()["data"]
    url = f"{BASE}/{artifact['id']}"

    assert api.get(url, params={"revision": 1}).json()["data"] == artifact
    revised_payload = {**evidence_payload(), "duration_us": 2_000_000}
    revised = api.put(url, json={"expected_revision": 1, "payload": revised_payload, "upstream_refs": []})
    assert revised.status_code == 200
    assert revised.json()["data"]["revision"] == 2
    assert api.put(url, json={"expected_revision": 1, "payload": revised_payload, "upstream_refs": []}).status_code == 409
    assert api.get(url, params={"revision": 1}).json()["data"]["payload"] == evidence_payload()


def test_artifact_api_enforces_acl_home_node_and_project_scope(client):
    api, role = client
    artifact = api.post(BASE, json={"schema": "reference_evidence.v1", "payload": evidence_payload(), "upstream_refs": []}).json()["data"]
    url = f"{BASE}/{artifact['id']}"

    role["value"] = "viewer"
    assert api.get(url, params={"revision": 1}).status_code == 200
    assert api.post(BASE, json={"schema": "reference_evidence.v1", "payload": evidence_payload(), "upstream_refs": []}).status_code == 403
    assert api.put(url, json={"expected_revision": 1, "payload": evidence_payload(), "upstream_refs": []}).status_code == 403
    assert api.get(url.replace("/demo/", "/peer/"), params={"revision": 1}).status_code == 403
    role["home"] = False
    assert api.get(url, params={"revision": 1}).status_code == 409


def test_artifact_api_maps_validation_and_missing_artifacts(client):
    api, _ = client
    assert api.post(BASE, json={"schema": "unknown.v1", "payload": {}, "upstream_refs": []}).status_code == 422
    assert api.post(BASE, json={"schema": "reference_evidence.v1", "payload": {}, "upstream_refs": []}).status_code == 422
    assert api.get(f"{BASE}/unknown", params={"revision": 1}).status_code == 404
