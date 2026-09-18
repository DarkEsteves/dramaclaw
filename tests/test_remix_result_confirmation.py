from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from novelvideo.api.auth import get_api_user
from novelvideo.freezone.remix.operations import RemixOperationStore
from novelvideo.freezone.remix.store import RemixArtifactStore
from novelvideo.freezone.paths import canvas_path


def _manifest() -> dict:
    return {
        "output_spec": {"width": 1080, "height": 1920, "fps": 30, "duration_frames": 30},
        "media_bindings": [
            {"media_id": "background", "kind": "image", "project_path": "media/background.png", "media_hash": "sha256:" + "a" * 64},
            {"media_id": "narration", "kind": "audio", "project_path": "media/narration.wav", "media_hash": "sha256:" + "b" * 64},
        ],
        "layers": [{"media_id": "background", "start_frame": 0, "end_frame": 30, "z_order": 0}], "subtitle_events": [],
        "audio_events": [{"media_id": "narration", "role": "narration", "start_frame": 0, "end_frame": 30}],
        "manual_overrides": [], "source_versions": [],
    }


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    from novelvideo.api.routes import remix

    async def resolve(*, user, project_id, required_role):
        if project_id != "demo":
            raise HTTPException(403, "Access denied")
        return SimpleNamespace(output_dir=str(tmp_path), state_dir=str(tmp_path / "state"), project_id="demo", is_home_node=True)

    monkeypatch.setattr(remix, "resolve_project_context", resolve)
    monkeypatch.setattr(remix, "require_project_home_node", lambda ctx, **kwargs: None)
    app = FastAPI()
    app.include_router(remix.router, prefix="/api/v1")
    app.dependency_overrides[get_api_user] = lambda: {"username": "alice"}
    return TestClient(app)


def _published(tmp_path: Path, *, node_revision: int = 3) -> tuple[dict, dict]:
    artifact = RemixArtifactStore(tmp_path, "demo").create("composition_manifest.v1", _manifest(), [])
    operation_store = RemixOperationStore(tmp_path, "demo")
    operation = operation_store.start(
        idempotency_key="render-one", input_refs=[{"artifact_id": artifact["id"], "revision": 1}],
        producer_version="remix-render.v1", normalized_parameters={"job_id": "a" * 64},
        node_revision=node_revision, canvas_id="default", node_id="compose-1",
    )
    output = tmp_path / "freezone" / "remix_renders" / artifact["id"] / "1" / f"{'a' * 64}.mp4"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"video")
    operation_store.publish_result_unverified(operation["operation_id"], output_refs=[{"project_path": output.relative_to(tmp_path).as_posix()}])
    return artifact, operation


def _canvas(tmp_path: Path, artifact: dict, *, node_revision: int = 3) -> Path:
    path = canvas_path(tmp_path / "state", "default")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "canvas_id": "default", "project_id": "demo", "revision": 2,
        "nodes": [{"id": "compose-1", "type": "videoCompose", "data": {
            "remixNodeRevision": node_revision,
            "remixManifestRef": {"artifactId": artifact["id"], "revision": 1},
        }}], "edges": [],
    }))
    return path


def test_operation_is_readable_and_listed_after_publish(api: TestClient, tmp_path: Path) -> None:
    artifact, operation = _published(tmp_path)
    path = f"/api/v1/projects/demo/freezone/remix/operations/{operation['operation_id']}"
    single = api.get(path)
    listed = api.get("/api/v1/projects/demo/freezone/remix/operations")

    assert single.status_code == listed.status_code == 200
    assert single.json()["data"]["status"] == "needs_confirmation"
    assert single.json()["data"]["task_type"] == "remix_render"
    assert single.json()["data"]["task_scope"] == "a" * 64
    assert single.json()["data"]["output_urls"][0]["download_url"].endswith(".mp4")
    assert listed.json()["data"]["items"][0]["operation_id"] == operation["operation_id"]
    assert listed.json()["data"]["items"][0]["output_refs"][0]["project_path"].endswith(".mp4")


def test_confirm_applies_render_only_to_unchanged_canvas_node(api: TestClient, tmp_path: Path) -> None:
    artifact, operation = _published(tmp_path)
    canvas = _canvas(tmp_path, artifact)
    url = f"/api/v1/projects/demo/freezone/remix/operations/{operation['operation_id']}/confirm"

    response = api.post(url, json={"canvas_id": "default", "node_id": "compose-1"})
    again = api.post(url, json={"canvas_id": "default", "node_id": "compose-1"})

    assert response.status_code == again.status_code == 200
    assert response.json()["data"]["status"] == "completed"
    saved = json.loads(canvas.read_text())
    assert saved["revision"] == 3
    assert saved["nodes"][0]["data"]["remixRenderOperationId"] == operation["operation_id"]
    assert saved["nodes"][0]["data"]["resultVideoUrl"].endswith(".mp4")


def test_confirm_preserves_result_as_stale_when_node_changed(api: TestClient, tmp_path: Path) -> None:
    artifact, operation = _published(tmp_path)
    canvas = _canvas(tmp_path, artifact, node_revision=4)
    original = canvas.read_bytes()
    response = api.post(
        f"/api/v1/projects/demo/freezone/remix/operations/{operation['operation_id']}/confirm",
        json={"canvas_id": "default", "node_id": "compose-1"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "stale"
    assert canvas.read_bytes() == original
    assert RemixOperationStore(tmp_path, "demo").get(operation["operation_id"])["output_refs"]


def test_confirm_rejects_wrong_node_and_unpublished_result(api: TestClient, tmp_path: Path) -> None:
    artifact, operation = _published(tmp_path)
    _canvas(tmp_path, artifact)
    bad = api.post(
        f"/api/v1/projects/demo/freezone/remix/operations/{operation['operation_id']}/confirm",
        json={"canvas_id": "default", "node_id": "other"},
    )
    assert bad.status_code == 409
    assert RemixOperationStore(tmp_path, "demo").get(operation["operation_id"])["status"] == "needs_confirmation"


def test_unbound_legacy_operation_can_be_downloaded_but_not_applied(api: TestClient, tmp_path: Path) -> None:
    artifact = RemixArtifactStore(tmp_path, "demo").create("composition_manifest.v1", _manifest(), [])
    operation_store = RemixOperationStore(tmp_path, "demo")
    operation = operation_store.start(
        idempotency_key="legacy", input_refs=[{"artifact_id": artifact["id"], "revision": 1}],
        producer_version="remix-render.v1", normalized_parameters={"job_id": "b" * 64}, node_revision=3,
    )
    output = tmp_path / "freezone" / "remix_renders" / "legacy.mp4"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"video")
    operation_store.publish_result_unverified(operation["operation_id"], output_refs=[{"project_path": output.relative_to(tmp_path).as_posix()}])
    _canvas(tmp_path, artifact)

    response = api.post(
        f"/api/v1/projects/demo/freezone/remix/operations/{operation['operation_id']}/confirm",
        json={"canvas_id": "default", "node_id": "compose-1"},
    )

    assert response.status_code == 409
    assert api.get(f"/api/v1/projects/demo/freezone/remix/operations/{operation['operation_id']}").json()["data"]["output_urls"]


def test_operation_ids_are_validated(api: TestClient) -> None:
    assert api.get("/api/v1/projects/demo/freezone/remix/operations/../bad").status_code != 200
    assert api.get("/api/v1/projects/demo/freezone/remix/operations/not-an-id").status_code == 422
    assert api.post(
        "/api/v1/projects/demo/freezone/remix/operations/not-an-id/confirm",
        json={"canvas_id": "default", "node_id": "compose-1"},
    ).status_code == 422
