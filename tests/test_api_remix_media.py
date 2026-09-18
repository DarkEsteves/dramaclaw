"""Uploaded remix media is project-scoped, immutable, and ready for processing."""

from __future__ import annotations

import hashlib
import subprocess
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    from novelvideo.api.auth import get_api_user
    from novelvideo.api.routes import remix_media

    role = {"value": "editor", "home": True}

    async def resolve(*, user, project_id, required_role):
        if project_id != "demo" or (required_role == "editor" and role["value"] != "editor"):
            raise HTTPException(403, "Access denied")
        return SimpleNamespace(output_dir=str(tmp_path), project_id="demo", is_home_node=role["home"])

    def home(ctx, **kwargs):
        if not ctx.is_home_node:
            raise HTTPException(409, "Wrong home node")

    monkeypatch.setattr(remix_media, "resolve_project_context", resolve)
    monkeypatch.setattr(remix_media, "require_project_home_node", home)
    monkeypatch.setattr(remix_media, "make_static_url_for_context", lambda ctx, rel, local_path=None: f"/media/{rel}")
    app = FastAPI()
    app.include_router(remix_media.router, prefix="/api/v1")
    app.dependency_overrides[get_api_user] = lambda: {"username": "alice"}
    return TestClient(app), role, tmp_path


def _video_bytes(tmp_path) -> bytes:
    source = tmp_path / "sample.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=32x32:r=10:d=0.4",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=0.4", "-shortest", "-c:v", "mpeg4",
         "-c:a", "aac", "-y", str(source)],
        check=True,
    )
    return source.read_bytes()


def test_upload_returns_usable_immutable_project_media(client):
    api, _, root = client
    content = _video_bytes(root)
    endpoint = "/api/v1/projects/demo/freezone/remix/media"
    first = api.post(endpoint, files={"file": ("reference.mp4", content, "video/mp4")})
    second = api.post(endpoint, files={"file": ("reference.mp4", content, "video/mp4")})
    assert first.status_code == 200
    assert second.status_code == 200
    data = first.json()["data"]
    assert data["media_kind"] == "video"
    assert data["has_audio"] is True
    assert data["media_hash"] == hashlib.sha256(content).hexdigest()
    assert data["duration_us"] > 0
    assert data["url"] == f"/media/{data['media_path']}"
    assert (root / data["media_path"]).read_bytes() == content
    assert second.json()["data"]["media_path"] != data["media_path"]


def test_video_upload_reports_when_separate_narration_is_needed(client):
    api, _, root = client
    source = root / "silent.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=32x32:r=10:d=0.4",
         "-c:v", "mpeg4", "-y", str(source)], check=True,
    )
    response = api.post("/api/v1/projects/demo/freezone/remix/media", files={"file": ("silent.mp4", source.read_bytes())})
    assert response.status_code == 200
    assert response.json()["data"]["has_audio"] is False


def test_upload_enforces_role_home_and_media_probe(client):
    api, role, root = client
    endpoint = "/api/v1/projects/demo/freezone/remix/media"
    content = _video_bytes(root)
    role["value"] = "viewer"
    assert api.post(endpoint, files={"file": ("x.mp4", content)}).status_code == 403
    role["value"] = "editor"
    role["home"] = False
    assert api.post(endpoint, files={"file": ("x.mp4", content)}).status_code == 409
    role["home"] = True
    assert api.post(endpoint.replace("/demo/", "/peer/"), files={"file": ("x.mp4", content)}).status_code == 403
    assert api.post(endpoint, files={"file": ("x.mp4", b"garbage")}).status_code == 422
    assert api.post(endpoint, files={"file": ("x.txt", content)}).status_code == 422


def test_attach_existing_project_media_without_reupload(client):
    api, _, root = client
    source = root / "freezone" / "_uploads" / "reference.mp4"
    source.parent.mkdir(parents=True)
    content = _video_bytes(root)
    source.write_bytes(content)

    response = api.post(
        "/api/v1/projects/demo/freezone/remix/media/attach",
        json={"media_path": "freezone/_uploads/reference.mp4"},
    )
    assert response.status_code == 200
    media = response.json()["data"]
    assert media["media_path"] == "freezone/_uploads/reference.mp4"
    assert media["media_hash"] == hashlib.sha256(content).hexdigest()
    assert media["media_kind"] == "video"
    assert source.read_bytes() == content


def test_attach_rejects_non_project_media(client, tmp_path):
    api, _, _ = client
    (tmp_path.parent / "outside.mp4").write_bytes(_video_bytes(tmp_path))
    endpoint = "/api/v1/projects/demo/freezone/remix/media/attach"
    assert api.post(endpoint, json={"media_path": "../outside.mp4"}).status_code == 422
    assert api.post(endpoint, json={"media_path": "/etc/passwd"}).status_code == 422
