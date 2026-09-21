from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace
import hashlib

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest


BASE = "/api/v1/projects/demo/freezone/remix/artifacts"


def manifest() -> dict:
    return {
        "output_spec": {"width": 1080, "height": 1920, "fps": 30, "duration_frames": 30},
        "media_bindings": [
            {"media_id": "background", "kind": "image", "project_path": "media/background.png", "media_hash": "sha256:" + hashlib.sha256(b"media").hexdigest()},
            {"media_id": "narration", "kind": "audio", "project_path": "media/narration.wav", "media_hash": "sha256:" + hashlib.sha256(b"media").hexdigest()},
        ],
        "layers": [{"media_id": "background", "start_frame": 0, "end_frame": 30, "z_order": 0}],
        "subtitle_events": [],
        "audio_events": [{"media_id": "narration", "role": "narration", "start_frame": 0, "end_frame": 30}],
        "manual_overrides": [],
        "source_versions": [],
    }


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    from novelvideo.api.auth import get_api_user
    from novelvideo.api.routes import remix as routes

    async def resolve(*, user, project_id, required_role):
        if project_id != "demo":
            raise HTTPException(403, "Access denied")
        return SimpleNamespace(output_dir=str(tmp_path), project_id="demo", is_home_node=True)

    queued = []

    class Backend:
        async def enqueue_project_task(self, ctx, **kwargs):
            queued.append(kwargs)
            return SimpleNamespace(task_state=SimpleNamespace(task_id="task-1"), backend="local", queue="ffmpeg")

    monkeypatch.setattr(routes, "resolve_project_context", resolve)
    monkeypatch.setattr(routes, "require_project_home_node", lambda ctx, **kwargs: None)
    monkeypatch.setattr(routes, "get_task_backend", lambda: Backend())
    monkeypatch.setattr(routes, "_speech_engine_ready", lambda: True, raising=False)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    app.dependency_overrides[get_api_user] = lambda: {"username": "alice"}
    return TestClient(app), queued


def test_enqueue_render_uses_saved_exact_manifest_revision(client, tmp_path):
    from novelvideo.freezone.remix.operations import RemixOperationStore
    api, queued = client
    created = api.post(BASE, json={"schema": "composition_manifest.v1", "payload": manifest(), "upstream_refs": []}).json()["data"]

    response = api.post(
        f"{BASE}/{created['id']}/render",
        json={"revision": 1, "idempotency_key": "render-one", "node_revision": 4},
    )

    assert response.status_code == 200
    assert response.json()["data"]["task_type"] == "remix_render"
    payload = queued[0]["payload"]
    assert payload["manifest_ref"] == {"artifact_id": created["id"], "revision": 1}
    assert payload["idempotency_key"] == "render-one"
    assert payload["node_revision"] == 4
    assert "project_path" not in response.request.content.decode()
    operation_id = response.json()["data"]["operation_id"]
    assert RemixOperationStore(tmp_path, "demo").get(operation_id)["input_media_hashes"] == ["sha256:" + hashlib.sha256(b"media").hexdigest()]


def test_repeated_render_request_returns_same_task_without_requeue(client):
    api, queued = client
    created = api.post(BASE, json={"schema": "composition_manifest.v1", "payload": manifest(), "upstream_refs": []}).json()["data"]
    url = f"{BASE}/{created['id']}/render"
    body = {"revision": 1, "idempotency_key": "render-once", "node_revision": 4}

    first = api.post(url, json=body)
    second = api.post(url, json=body)

    assert first.status_code == second.status_code == 200
    assert second.json()["data"]["task_id"] == first.json()["data"]["task_id"]
    assert len(queued) == 1


def test_render_retries_operation_left_without_task_id(client, monkeypatch: pytest.MonkeyPatch):
    from novelvideo.api.routes import remix as routes

    api, queued = client
    created = api.post(BASE, json={"schema": "composition_manifest.v1", "payload": manifest(), "upstream_refs": []}).json()["data"]
    url = f"{BASE}/{created['id']}/render"
    body = {"revision": 1, "idempotency_key": "render-after-enqueue-failure", "node_revision": 4}
    backend = routes.get_task_backend()
    original_enqueue = backend.enqueue_project_task
    attempts = 0

    async def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("queue unavailable")
        return await original_enqueue(*args, **kwargs)

    monkeypatch.setattr(backend, "enqueue_project_task", fail_once)
    monkeypatch.setattr(routes, "get_task_backend", lambda: backend)

    with pytest.raises(RuntimeError, match="queue unavailable"):
        api.post(url, json=body)
    retried = api.post(url, json=body)

    assert retried.status_code == 200
    assert retried.json()["data"]["task_id"] == "task-1"
    assert len(queued) == 1


def test_render_does_not_requeue_when_result_published_before_task_binding(client, tmp_path):
    from novelvideo.freezone.remix.operations import RemixOperationStore

    api, queued = client
    created = api.post(BASE, json={"schema": "composition_manifest.v1", "payload": manifest(), "upstream_refs": []}).json()["data"]
    body = {"revision": 1, "idempotency_key": "fast-render", "node_revision": 4}
    store = RemixOperationStore(tmp_path, "demo")
    job_id = hashlib.sha256(f"demo\0{created['id']}\0{1}\0fast-render".encode()).hexdigest()
    operation = store.start(
        idempotency_key="fast-render",
        input_refs=[{"artifact_id": created["id"], "revision": 1}],
        producer_version="remix-render.v1", normalized_parameters={"job_id": job_id, "input_fingerprint": ""},
        node_revision=4, input_media_hashes=["sha256:" + hashlib.sha256(b"media").hexdigest()],
    )
    output = tmp_path / "freezone" / "remix_renders" / created["id"] / "1" / f"{job_id}.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"video")
    store.publish_result_unverified(operation["operation_id"], output_refs=[{"project_path": output.relative_to(tmp_path).as_posix()}])

    response = api.post(f"{BASE}/{created['id']}/render", json=body)

    assert response.status_code == 200
    assert response.json()["data"]["operation_status"] == "needs_confirmation"
    assert queued == []


@pytest.mark.parametrize("state", ["pending_reconciliation", "cancelled"])
def test_render_reconciles_existing_uncertain_or_cancelled_operation_before_requeue(client, tmp_path, state):
    from novelvideo.freezone.remix.operations import RemixOperationStore
    api, queued = client
    created = api.post(BASE, json={"schema": "composition_manifest.v1", "payload": manifest(), "upstream_refs": []}).json()["data"]
    job_id = hashlib.sha256(f"demo\0{created['id']}\0{1}\0recover-render".encode()).hexdigest()
    store = RemixOperationStore(tmp_path, "demo")
    operation = store.start(idempotency_key="recover-render", input_refs=[{"artifact_id": created["id"], "revision": 1}], producer_version="remix-render.v1", normalized_parameters={"job_id": job_id, "input_fingerprint": ""}, node_revision=4, input_media_hashes=["sha256:" + hashlib.sha256(b"media").hexdigest()])
    if state == "pending_reconciliation":
        store.provider_outcome_unknown(operation["operation_id"], provider_task_id="provider-1")
    else:
        store.cancel(operation["operation_id"], reason="user_cancelled")
    response = api.post(f"{BASE}/{created['id']}/render", json={"revision": 1, "idempotency_key": "recover-render", "node_revision": 4})
    assert response.status_code == 200
    assert response.json()["data"]["operation_status"] == state
    assert queued == []


def test_simultaneous_render_api_calls_enqueue_once(client, monkeypatch: pytest.MonkeyPatch):
    from novelvideo.api.routes import remix as routes
    api, _queued = client
    created = api.post(BASE, json={"schema": "composition_manifest.v1", "payload": manifest(), "upstream_refs": []}).json()["data"]
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    class SlowBackend:
        async def enqueue_project_task(self, ctx, **kwargs):
            nonlocal calls
            calls += 1
            entered.set()
            await asyncio.to_thread(release.wait, 2)
            return SimpleNamespace(task_state=SimpleNamespace(task_id="task-one"), backend="local", queue="ffmpeg")
    monkeypatch.setattr(routes, "get_task_backend", lambda: SlowBackend())
    body = {"revision": 1, "idempotency_key": "simultaneous", "node_revision": 4}
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(api.post, f"{BASE}/{created['id']}/render", json=body)
        assert entered.wait(2)
        second = executor.submit(api.post, f"{BASE}/{created['id']}/render", json=body)
        second_response = second.result(timeout=2)
        release.set()
        first_response = first.result(timeout=2)
    assert first_response.status_code == second_response.status_code == 200
    assert calls == 1


def test_force_regenerate_is_reachable_through_render_api(client):
    api, queued = client
    created = api.post(BASE, json={"schema": "composition_manifest.v1", "payload": manifest(), "upstream_refs": []}).json()["data"]
    url = f"{BASE}/{created['id']}/render"
    body = {"revision": 1, "idempotency_key": "force", "node_revision": 4}
    first = api.post(url, json=body)
    second = api.post(url, json={**body, "regenerate": True})
    assert first.status_code == second.status_code == 200
    assert first.json()["data"]["operation_id"] != second.json()["data"]["operation_id"]
    assert len(queued) == 2


def test_idempotency_key_cannot_be_reused_for_different_manifest_revision(client):
    api, queued = client
    created = api.post(BASE, json={"schema": "composition_manifest.v1", "payload": manifest(), "upstream_refs": []}).json()["data"]
    url = f"{BASE}/{created['id']}/render"
    revision_two = api.put(f"{BASE}/{created['id']}", json={"expected_revision": 1, "payload": manifest(), "upstream_refs": []})
    assert revision_two.status_code == 200

    assert api.post(url, json={"revision": 1, "idempotency_key": "same", "node_revision": 4}).status_code == 200
    conflict = api.post(url, json={"revision": 2, "idempotency_key": "same", "node_revision": 4})

    assert conflict.status_code == 409
    assert len(queued) == 1


def test_processing_action_queues_once_with_project_local_media(client, tmp_path):
    api, queued = client
    media = tmp_path / "media" / "voice.wav"
    media.parent.mkdir()
    media.write_bytes(b"voice")
    body = {
        "action": "transcribe_media",
        "payload": {"media_path": "media/voice.wav", "language": "zh"},
        "idempotency_key": "transcribe-once",
        "node_revision": 3,
    }

    first = api.post("/api/v1/projects/demo/freezone/remix/actions", json=body)
    second = api.post("/api/v1/projects/demo/freezone/remix/actions", json=body)

    assert first.status_code == second.status_code == 200
    assert first.json()["data"]["task_type"] == "remix_transcribe_media"
    assert second.json()["data"]["task_id"] == first.json()["data"]["task_id"]
    assert len(queued) == 1
    assert queued[0]["payload"]["media_path"] == "media/voice.wav"
    assert queued[0]["payload"]["media_sha256"] == hashlib.sha256(b"voice").hexdigest()


def test_reference_analysis_action_queues_project_media_without_speech_engine(client, tmp_path, monkeypatch):
    api, queued = client
    from novelvideo.api.routes import remix as routes

    media = tmp_path / "media" / "reference.mp4"
    media.parent.mkdir()
    media.write_bytes(b"video")
    monkeypatch.setattr(routes, "_speech_engine_ready", lambda: False)
    response = api.post("/api/v1/projects/demo/freezone/remix/actions", json={
        "action": "analyze_reference_video", "payload": {"media_path": "media/reference.mp4"},
        "idempotency_key": "reference-once", "node_revision": 1,
    })
    assert response.status_code == 200
    assert response.json()["data"]["task_type"] == "remix_analyze_reference_video"
    assert queued[0]["payload"]["media_sha256"] == hashlib.sha256(b"video").hexdigest()


def test_processing_action_rejects_unsafe_media_and_unknown_action(client):
    api, queued = client
    base = "/api/v1/projects/demo/freezone/remix/actions"
    common = {"idempotency_key": "unsafe", "node_revision": 1}

    unsafe = api.post(base, json={**common, "action": "transcribe_media", "payload": {"media_path": "../../etc/passwd", "language": "en"}})
    unknown = api.post(base, json={**common, "action": "shell", "payload": {}})

    assert unsafe.status_code == 422
    assert unknown.status_code == 422
    assert queued == []


def test_speech_action_reports_unavailable_engine_before_queueing(client, monkeypatch):
    api, queued = client
    from novelvideo.api.routes import remix as routes

    monkeypatch.setattr(routes, "_speech_engine_ready", lambda: False)
    response = api.post("/api/v1/projects/demo/freezone/remix/actions", json={
        "action": "transcribe_media", "payload": {"media_path": "media/voice.wav", "language": "en"},
        "idempotency_key": "missing-engine", "node_revision": 1,
    })

    assert response.status_code == 503
    assert queued == []


def test_speech_action_runs_readiness_probe_off_the_async_event_loop(
    client, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    api, queued = client
    from novelvideo.api.routes import remix as routes

    media = tmp_path / "media" / "voice.wav"
    media.parent.mkdir()
    media.write_bytes(b"voice")
    calls = []

    def readiness() -> bool:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            calls.append("worker-thread")
            return True
        raise AssertionError("readiness probe blocked the async API event loop")

    monkeypatch.setattr(routes, "_speech_engine_ready", readiness)

    response = api.post(
        "/api/v1/projects/demo/freezone/remix/actions",
        json={
            "action": "transcribe_media",
            "payload": {"media_path": "media/voice.wav", "language": "en"},
            "idempotency_key": "threaded-readiness",
            "node_revision": 1,
        },
    )

    assert response.status_code == 200
    assert calls == ["worker-thread"]
    assert len(queued) == 1


def test_enqueue_render_rejects_invalid_saved_manifest_before_queueing(client, monkeypatch: pytest.MonkeyPatch):
    api, queued = client
    created = api.post(BASE, json={"schema": "composition_manifest.v1", "payload": manifest(), "upstream_refs": []}).json()["data"]
    from novelvideo.freezone.remix import render

    monkeypatch.setattr(render, "validate_manifest", lambda _: (_ for _ in ()).throw(ValueError("invalid manifest")))

    response = api.post(f"{BASE}/{created['id']}/render", json={"revision": 1, "idempotency_key": "bad", "node_revision": 1})

    assert response.status_code == 422
    assert queued == []


def test_runner_resolves_only_saved_project_local_media(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.task_backend.runners import remix as runner

    for path in (tmp_path / "media/background.png", tmp_path / "media/narration.wav"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"media")
    artifact = RemixArtifactStore(tmp_path, "demo").create(
        schema="composition_manifest.v1", payload=manifest(), upstream_refs=[]
    )
    operations = RemixOperationStore(tmp_path, "demo")
    operation = operations.start(
        idempotency_key="render-runner", input_refs=[{"artifact_id": artifact["id"], "revision": 1}],
        producer_version="remix-render.v1", normalized_parameters={"job_id": "a" * 64}, node_revision=2,
    )
    rendered = {}

    def fake_render(saved, *, media_paths, output_path):
        rendered["manifest"] = saved
        rendered["paths"] = media_paths
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"mp4")
        return output_path

    monkeypatch.setattr(runner, "render_manifest", fake_render)
    result = runner.run_remix_render(
        {"payload": {"job_id": "a" * 64, "manifest_ref": {"artifact_id": artifact["id"], "revision": 1}, "node_revision": 2, "operation_id": operation["operation_id"]}},
        SimpleNamespace(output_dir=str(tmp_path), project_id="demo"),
    )

    assert set(rendered["paths"]) == {"background", "narration"}
    assert result["manifest_ref"] == {"artifact_id": artifact["id"], "revision": 1}
    recorded = operations.start(
        idempotency_key="render-runner", input_refs=[{"artifact_id": artifact["id"], "revision": 1}],
        producer_version="remix-render.v1", normalized_parameters={"job_id": "a" * 64}, node_revision=2,
    )
    assert recorded["status"] == "needs_confirmation"
    assert recorded["output_refs"] == [{"project_path": result["output_path"]}]
