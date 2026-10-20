from types import SimpleNamespace
from copy import deepcopy
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_unsaved_canvas_has_no_analysis_runs_and_can_later_restore_them(client, monkeypatch):
    from novelvideo.api.routes import reference_analysis as routes
    from novelvideo.freezone import canvas_store

    canvas_id = "user_alice_unsaved"
    path = f"/api/v1/projects/project-a/freezone/canvases/{canvas_id}/reference-analysis-runs"
    restored = {"run_id": "saved-run", "canvas_id": canvas_id, "status": "analyzing"}
    calls = []

    async def list_runs(**kwargs):
        calls.append(kwargs)
        return [restored]

    monkeypatch.setattr(routes.coordinator, "list", list_runs)
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"runs": []}
    assert canvas_store.read_canvas(client.state_dir, canvas_id) is None
    assert calls == []

    canvas_store.save_canvas(client.state_dir, canvas_id, base_revision=None,
                             build_payload=lambda _: {"project_id": "project-a", "canvas_id": canvas_id, "nodes": [], "edges": []})
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["runs"] == [restored]
    assert len(calls) == 1


def test_analysis_submission_still_requires_a_saved_canvas(client):
    response = client.post("/api/v1/projects/project-a/freezone/canvases/user_alice_unsaved/reference-analysis-runs",
                           json={"source_node_id": "source-a", "user_goal": "复刻", "idempotency_key": "unsaved"})
    assert response.status_code == 404
    assert client.calls == []


def test_ee_auto_continuation_requires_explicit_opt_in_before_submission(client, monkeypatch):
    monkeypatch.setenv("ST_CONTROL_PLANE_DSN", "postgresql://configured")
    monkeypatch.delenv("ST_REFERENCE_REMIX_AUTO_CONTINUATION", raising=False)
    response = client.post(
        "/api/v1/projects/project-a/freezone/canvases/canvas-a/reference-analysis-runs",
        json={"source_node_id": "source-a", "user_goal": "复刻", "idempotency_key": "ee-off"},
    )
    assert response.status_code == 503
    assert "disabled" in response.json()["detail"]
    assert client.calls == []

    monkeypatch.setenv("ST_REFERENCE_REMIX_AUTO_CONTINUATION", "1")
    response = client.post(
        "/api/v1/projects/project-a/freezone/canvases/canvas-a/reference-analysis-runs",
        json={"source_node_id": "source-a", "user_goal": "复刻", "idempotency_key": "ee-on"},
    )
    assert response.status_code == 200, response.text
    assert [name for name, _ in client.calls] == ["reserve", "enqueue", "attach"]


def test_ee_disabled_continuation_lists_no_runs_without_opening_store(client, monkeypatch):
    from novelvideo.api.routes import reference_analysis as routes

    monkeypatch.setenv("ST_CONTROL_PLANE_DSN", "postgresql://configured")
    monkeypatch.delenv("ST_REFERENCE_REMIX_AUTO_CONTINUATION", raising=False)
    async def inaccessible(**kwargs):
        pytest.fail("disabled EE listing opened continuation state")
    monkeypatch.setattr(routes.coordinator, "list", inaccessible)
    response = client.get(
        "/api/v1/projects/project-a/freezone/canvases/canvas-a/reference-analysis-runs"
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"runs": []}


@pytest.fixture
def client(monkeypatch, tmp_path):
    from novelvideo.api.auth import get_api_user
    from novelvideo.api.routes import reference_analysis as routes
    from novelvideo.api.routes import freezone
    from novelvideo.freezone import canvas_store
    from novelvideo.freezone.remix import reference_analysis_service

    ctx = SimpleNamespace(
        project_id="project-a",
        state_dir=tmp_path,
        output_dir=tmp_path,
        owner_username="alice",
        requester_username="alice",
        requester_user_id="user-a",
        project_name="demo",
        is_home_node=True,
        effective_role="editor",
    )

    async def resolve(*args, **kwargs):
        return ctx, "alice", "demo", tmp_path, str(tmp_path)

    monkeypatch.setattr(freezone, "_resolve_freezone_project", resolve)
    monkeypatch.setattr(reference_analysis_service, "probe_audio_track", lambda _path: False)
    (tmp_path / "reference.mp4").write_bytes(b"fixture")
    canvas = {
        "project_id": "project-a",
        "canvas_id": "canvas-a",
        "revision": 1,
        "nodes": [
            {
                "id": "source-a",
                "type": "videoNode",
                "position": {"x": 0, "y": 0},
                "width": 480,
                "data": {"videoUrl": "/static/projects/project-a/reference.mp4"},
            }
        ],
        "edges": [],
    }
    canvas_store.save_canvas(
        tmp_path,
        "canvas-a",
        base_revision=None,
        build_payload=lambda _: deepcopy(canvas),
    )
    calls = []

    class Coordinator:
        async def reserve(self, **kwargs):
            calls.append(("reserve", kwargs))
            return {
                "run_id": "run-a",
                "job_id": "job-a",
                "analysis_node_id": "analysis-a",
                "status": "submitting",
            }

        async def claim_submission(self, **kwargs):
            return "claim"

        async def bind_analysis_operation(self, **kwargs):
            pass

        async def attach_task(self, **kwargs):
            calls.append(("attach", kwargs))
            return {
                "run_id": "run-a",
                "status": "analyzing",
                "analysis_node_id": "analysis-a",
                "task_id": "task-a",
                "task_key": "key-a",
            }

        async def fail_submission(self, **kwargs):
            calls.append(("failed", kwargs))

        async def list(self, **kwargs):
            return []

        async def get(self, **kwargs):
            return None

        async def retry(self, **kwargs):
            return None

    monkeypatch.setattr(routes, "coordinator", Coordinator())

    async def enqueue(ctx, source_ref, requested_evidence, idempotency_key, *, on_reserved=None):
        calls.append(("enqueue", {"source_ref": source_ref}))
        if on_reserved:
            await on_reserved("a" * 32)
        return {"task_id": "task-a", "task_key": "key-a", "operation_id": "a" * 32}

    from novelvideo.freezone.remix import reference_analysis_service
    monkeypatch.setattr(reference_analysis_service, "ensure_reference_analysis", enqueue)
    monkeypatch.setattr(
        freezone, "resolve_static_url_to_path", lambda *args: tmp_path / "reference.mp4"
    )
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    app.dependency_overrides[get_api_user] = lambda: {
        "id": "user-a",
        "username": "alice",
    }
    result = TestClient(app)
    result.calls = calls
    result.state_dir = tmp_path
    return result


def test_start_reserves_before_submission_and_saves_analysis_node(client):
    result = client.post(
        "/api/v1/projects/project-a/freezone/canvases/canvas-a/reference-analysis-runs",
        json={
            "source_node_id": "source-a",
            "user_goal": "原片换产品",
            "idempotency_key": "turn-a",
            "inputs": {},
        },
    )
    assert result.status_code == 200, result.text
    assert [name for name, _ in client.calls] == ["reserve", "enqueue", "attach"]
    from novelvideo.freezone.canvas_store import read_canvas

    nodes = read_canvas(client.state_dir, "canvas-a")["nodes"]
    assert (
        next(n for n in nodes if n["id"] == "analysis-a")["data"]["isAnalyzing"] is True
    )
    assert next(n for n in nodes if n["id"] == "analysis-a")["position"] == {"x": 600, "y": 0}
    assert result.json()["data"]["task_key"] == "key-a"


def test_native_agent_analysis_with_audio_starts_connected_transcript_before_story(client, monkeypatch):
    from novelvideo.api.routes import reference_analysis as routes
    from novelvideo.freezone import canvas_store
    from novelvideo.freezone.remix import reference_analysis_service
    from novelvideo.api.routes import remix

    monkeypatch.setattr(reference_analysis_service, "probe_audio_track", lambda path: True)
    monkeypatch.setattr(remix, "_transcription_engine_ready", lambda: True)
    async def reserve(**kwargs):
        client.calls.append(("reserve", kwargs))
        return {"run_id": "run-a", "analysis_node_id": "analysis-a",
                "transcript_node_id": "transcript-a", "status": "submitting"}
    monkeypatch.setattr(routes.coordinator, "reserve", reserve)
    async def transcribe(ctx, *, media_path, media_hash, idempotency_key, on_reserved):
        client.calls.append(("transcribe", {"media_path": media_path, "media_hash": media_hash}))
        await on_reserved("b" * 32)
        return {"task_id": "speech-task", "task_key": "speech-key", "operation_id": "b" * 32}
    monkeypatch.setattr(routes, "_enqueue_reference_transcript", transcribe, raising=False)
    async def bind_transcript(**kwargs):
        client.calls.append(("bind_transcript", kwargs))
    monkeypatch.setattr(routes.coordinator, "bind_transcript_operation", bind_transcript, raising=False)
    async def attach(**kwargs):
        client.calls.append(("attach", kwargs))
        return {"run_id": "run-a", "analysis_node_id": "analysis-a", "transcript_node_id": "transcript-a",
                "status": "transcribing", "task_id": "speech-task", "task_key": "speech-key"}
    monkeypatch.setattr(routes.coordinator, "attach_task", attach)
    response = client.post("/api/v1/projects/project-a/freezone/canvases/canvas-a/reference-analysis-runs", json={
        "source_node_id": "source-a", "user_goal": "复刻", "idempotency_key": "agent-a"})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "transcribing"
    saved = canvas_store.read_canvas(client.state_dir, "canvas-a")
    assert {node["id"] for node in saved["nodes"]} == {"source-a", "transcript-a", "analysis-a"}
    assert {(edge["source"], edge["target"]) for edge in saved["edges"]} == {
        ("source-a", "transcript-a"), ("source-a", "analysis-a"), ("transcript-a", "analysis-a")}
    positions = {node["id"]: node["position"] for node in saved["nodes"]}
    assert positions["transcript-a"] == {"x": 600, "y": 0}
    assert positions["analysis-a"] == {"x": 1140, "y": 0}
    assert [name for name, _ in client.calls] == ["reserve", "transcribe", "bind_transcript", "attach"]


@pytest.mark.asyncio
async def test_native_transcript_admission_uses_independent_speech_task(tmp_path, monkeypatch):
    import hashlib
    from novelvideo.api.routes import reference_analysis as routes
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo import ports

    media = tmp_path / 'reference.mp4'
    media.write_bytes(b'fixture')
    digest = hashlib.sha256(b'fixture').hexdigest()
    ctx = SimpleNamespace(output_dir=tmp_path, state_dir=tmp_path,
        project_id='project-a', requester_user_id='user-a', is_home_node=True)
    calls = []
    class Backend:
        async def enqueue_project_task(self, _ctx, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(task_state=SimpleNamespace(task_id='speech-task'))
    monkeypatch.setattr(ports, 'get_task_backend', lambda: Backend())
    async def reserved(operation_id): calls.append({'reserved': operation_id})
    receipt = await routes._enqueue_reference_transcript(ctx, media_path='reference.mp4',
        media_hash=digest, idempotency_key='agent-transcript', on_reserved=reserved)
    assert receipt['task_id'] == 'speech-task'
    assert calls[0] == {'reserved': receipt['operation_id']}
    assert calls[1]['task_type'] == 'remix_transcribe_media' and calls[1]['queue_kind'] == 'default'
    assert calls[1]['payload']['media_sha256'] == digest
    operation = RemixOperationStore(tmp_path, 'project-a').get(receipt['operation_id'])
    assert operation['task_id'] == 'speech-task'
    assert operation['normalized_parameters']['action'] == 'transcribe_media'


@pytest.mark.asyncio
async def test_native_transcript_admission_recovers_enqueued_task_before_operation_binding(tmp_path, monkeypatch):
    import hashlib
    from novelvideo.api.routes import reference_analysis as routes
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo import ports
    from novelvideo import task_state

    (tmp_path / 'reference.mp4').write_bytes(b'fixture')
    digest = hashlib.sha256(b'fixture').hexdigest()
    ctx = SimpleNamespace(output_dir=tmp_path, project_id='project-a', requester_user_id='user-a', is_home_node=True)
    queued = {}

    class Backend:
        async def enqueue_project_task(self, _ctx, **kwargs):
            queued['task'] = SimpleNamespace(task_id='speech-task', project_id=ctx.project_id,
                requester_user_id=ctx.requester_user_id)
            return SimpleNamespace(task_state=queued['task'])

    monkeypatch.setattr(ports, 'get_task_backend', lambda: Backend())
    monkeypatch.setattr(task_state, 'get_task_manager', lambda: SimpleNamespace(
        get_task_for_project=lambda *_args, **_kwargs: queued.get('task')))
    original_bind = RemixOperationStore.bind_task
    def interrupted_bind(*_args, **_kwargs):
        raise RuntimeError('process stopped after enqueue')
    monkeypatch.setattr(RemixOperationStore, 'bind_task', interrupted_bind)
    async def reserved(_operation_id): pass
    with pytest.raises(RuntimeError, match='process stopped'):
        await routes._enqueue_reference_transcript(ctx, media_path='reference.mp4',
            media_hash=digest, idempotency_key='agent-transcript', on_reserved=reserved)
    monkeypatch.setattr(RemixOperationStore, 'bind_task', original_bind)
    receipt = await routes._enqueue_reference_transcript(ctx, media_path='reference.mp4',
        media_hash=digest, idempotency_key='agent-transcript', on_reserved=reserved)
    assert receipt['task_id'] == 'speech-task'
    assert RemixOperationStore(tmp_path, ctx.project_id).get(receipt['operation_id'])['task_id'] == 'speech-task'


def test_missing_source_cannot_enqueue(client):
    result = client.post(
        "/api/v1/projects/project-a/freezone/canvases/canvas-a/reference-analysis-runs",
        json={
            "source_node_id": "foreign-source",
            "user_goal": "原片换产品",
            "idempotency_key": "turn-a",
        },
    )
    assert result.status_code == 404
    assert not client.calls


def test_run_not_found_has_no_retry_side_effect(client):
    path = "/api/v1/projects/project-a/freezone/canvases/canvas-a/reference-analysis-runs/missing"
    assert client.get(path).status_code == 404
    assert client.post(path + "/retry").status_code == 404


def test_foreign_media_cannot_be_submitted(client):
    from novelvideo.freezone import canvas_store

    canvas = canvas_store.read_canvas(client.state_dir, "canvas-a")
    canvas["nodes"][0]["data"]["videoUrl"] = "/static/projects/project-b/reference.mp4"
    canvas["revision"] += 1
    canvas_store.save_canvas(
        client.state_dir, "canvas-a", base_revision=1, build_payload=lambda _: canvas
    )
    response = client.post(
        "/api/v1/projects/project-a/freezone/canvases/canvas-a/reference-analysis-runs",
        json={
            "source_node_id": "source-a",
            "user_goal": "复刻",
            "idempotency_key": "turn-a",
        },
    )
    assert response.status_code == 400
    assert not client.calls


def test_private_canvas_denies_other_actor(client):
    from novelvideo.freezone import canvas_store

    canvas = canvas_store.read_canvas(client.state_dir, "canvas-a")
    canvas.update(access_model="private", owner_principal_id="another-user", revision=2)
    canvas_store.save_canvas(
        client.state_dir, "canvas-a", base_revision=1, build_payload=lambda _: canvas
    )
    response = client.post(
        "/api/v1/projects/project-a/freezone/canvases/canvas-a/reference-analysis-runs",
        json={
            "source_node_id": "source-a",
            "user_goal": "复刻",
            "idempotency_key": "turn-a",
        },
    )
    assert response.status_code == 403
    assert not client.calls


@pytest.mark.parametrize('audio', [False, True])
def test_saved_completed_analysis_is_reused_without_enqueue(client, monkeypatch, audio):
    from novelvideo.api.routes import reference_analysis as routes
    from novelvideo.freezone import canvas_store
    from novelvideo.freezone.remix import reference_analysis_service
    from novelvideo.api.routes import remix
    monkeypatch.setattr(reference_analysis_service, 'probe_audio_track', lambda _path: audio)
    monkeypatch.setattr(remix, '_transcription_engine_ready', lambda: False)

    canvas = canvas_store.read_canvas(client.state_dir, "canvas-a")
    canvas["nodes"].append(
        {
            "id": "analysis-a",
            "type": "videoStoryNode",
            "data": {
                "sourceVideoUrl": "/static/projects/project-a/reference.mp4",
                "isAnalyzing": False,
                "rawResult": {"video_story": {"summary": "采访反转"}},
            },
        }
    )
    canvas["edges"].append(
        {"id": "source-link", "source": "source-a", "target": "analysis-a"}
    )
    canvas["revision"] = 2
    canvas_store.save_canvas(
        client.state_dir, "canvas-a", base_revision=1, build_payload=lambda _: canvas
    )

    async def adopt(**kwargs):
        client.calls.append(("adopt", kwargs))
        return {
            "run_id": "run-a",
            "status": "analysis_ready",
            "analysis_node_id": "analysis-a",
            "task_id": None,
            "task_key": None,
        }

    monkeypatch.setattr(
        routes.coordinator, "adopt_saved_analysis", adopt, raising=False
    )
    response = client.post(
        "/api/v1/projects/project-a/freezone/canvases/canvas-a/reference-analysis-runs",
        json={
            "source_node_id": "source-a",
            "user_goal": "复刻",
            "idempotency_key": "turn-a",
        },
    )
    assert response.status_code == 200, response.text
    assert [name for name, _ in client.calls] == ["reserve", "adopt"]
    assert response.json()["data"]["task_id"] is None


@pytest.mark.parametrize("phase", ["awaiting_direction", "awaiting_plan_confirmation"])
def test_replay_returns_saved_commission_progress_without_new_submission(client, monkeypatch, phase):
    from novelvideo.api.routes import reference_analysis as routes
    from novelvideo.chat.runtime_event_evidence import _codex_freezone_non_canvas_receipt
    from novelvideo.chat.runtime_port import ChatBackendEvent
    from novelvideo.freezone.remix.reference_analysis_identity import analysis_admission_key

    saved = {"run_id": "run-a", "project_id": "project-a", "canvas_id": "canvas-a",
             "source_node_id": "source-a", "analysis_node_id": "analysis-a", "status": phase,
             "task_id": None, "task_key": None, "error": None, "commission_phase": phase,
             "commission_ref": {"artifact_id": "commission-a", "revision": 4, "project_id": "project-a"}}

    async def replay(**kwargs):
        client.calls.append(("replay", kwargs))
        return deepcopy(saved)

    async def no_claim(**kwargs):
        client.calls.append(("claim", kwargs))
        return None

    monkeypatch.setattr(routes.coordinator, "reserve", replay)
    monkeypatch.setattr(routes.coordinator, "claim_submission", no_claim)
    arguments = {"source_node_id": "source-a", "user_goal": "复刻", "idempotency_key": "turn-a"}
    response = client.post("/api/v1/projects/project-a/freezone/canvases/canvas-a/reference-analysis-runs", json=arguments)
    assert response.status_code == 200, response.text
    assert response.json()["data"] == saved
    assert [name for name, _ in client.calls] == ["replay", "claim"]
    # The plugin carries this persisted public run unchanged and adds its
    # actual admission key and bound scope; no task or canvas receipt is made.
    payload = {"ok": True, "status": "reference_analysis_submitted", "submitted": True,
               "project_id": "project-a", "canvas_id": "canvas-a", "run": response.json()["data"],
               "idempotency_key": "turn-a", **{key: saved[key] for key in ("run_id", "analysis_node_id", "task_id", "task_key")}}
    event = ChatBackendEvent(type="tool_updated", name="freezone_start_reference_analysis",
                             status="completed", call_id="call-a", input=arguments, structured=payload)
    assert _codex_freezone_non_canvas_receipt(
        event, expected_project="project-a", expected_canvas="canvas-a",
        admission_key_for_request=analysis_admission_key,
    ) is payload


def test_agent_read_only_token_cannot_submit_analysis(client):
    from novelvideo.api.auth import get_api_user

    client.app.dependency_overrides[get_api_user] = lambda: {
        "id": "user-a",
        "username": "alice",
        "credential_kind": "agent_session",
        "current_scope_kind": "project",
        "current_project_id": "project-a",
        "scopes": ["agent:read"],
    }
    response = client.post(
        "/api/v1/projects/project-a/freezone/canvases/canvas-a/reference-analysis-runs",
        json={
            "source_node_id": "source-a",
            "user_goal": "复刻",
            "idempotency_key": "turn-a",
        },
    )
    assert response.status_code == 403
    assert not client.calls


def test_failure_after_enqueue_preserves_paid_task_identity(client, monkeypatch):
    from novelvideo.api.routes import reference_analysis as routes

    async def attach(**kwargs):
        raise ValueError("source changed after admission")

    monkeypatch.setattr(routes.coordinator, "attach_task", attach)
    response = client.post(
        "/api/v1/projects/project-a/freezone/canvases/canvas-a/reference-analysis-runs",
        json={
            "source_node_id": "source-a",
            "user_goal": "复刻",
            "idempotency_key": "turn-a",
        },
    )
    assert response.status_code == 409
    failed = next(kwargs for name, kwargs in client.calls if name == "failed")
    assert failed["task_id"] == "task-a"
    assert failed["task_key"] == "key-a"


@pytest.mark.parametrize("ee_auto_disabled", [False, True])
def test_shared_analysis_endpoint_uses_canvas_source_not_client_path(client, monkeypatch, ee_auto_disabled):
    if ee_auto_disabled:
        monkeypatch.setenv("ST_CONTROL_PLANE_DSN", "postgresql://configured")
        monkeypatch.delenv("ST_REFERENCE_REMIX_AUTO_CONTINUATION", raising=False)
    from novelvideo.freezone.remix import reference_analysis_service as service
    calls = []
    async def ensure(ctx, source_ref, requested_evidence, idempotency_key):
        calls.append(source_ref)
        return {'operation_id': 'operation', 'task_id': 'task', 'analysis_ref': None}
    monkeypatch.setattr(service, 'ensure_reference_analysis', ensure)
    response = client.post('/api/v1/projects/project-a/freezone/canvases/canvas-a/reference-analysis',
                           json={'source_node_id': 'source-a', 'idempotency_key': 'manual'})
    assert response.status_code == 200, response.text
    assert calls == [{'media_path': 'reference.mp4'}]
    assert response.json()['data']['operation_id'] == 'operation'


def test_shared_analysis_private_canvas_denies_other_actor(client):
    from novelvideo.freezone import canvas_store
    canvas = canvas_store.read_canvas(client.state_dir, 'canvas-a')
    canvas.update(access_model='private', owner_principal_id='another-user', revision=2)
    canvas_store.save_canvas(client.state_dir, 'canvas-a', base_revision=1, build_payload=lambda _: canvas)
    response = client.post('/api/v1/projects/project-a/freezone/canvases/canvas-a/reference-analysis',
                           json={'source_node_id': 'source-a', 'idempotency_key': 'manual'})
    assert response.status_code == 403


def test_reference_evidence_is_viewer_read_and_requires_canvas_source(client, monkeypatch):
    from novelvideo.api.routes import reference_analysis as routes
    from novelvideo.freezone.remix import reference_analysis_service as service
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from test_reference_analysis_service import saved_receipt
    ref = saved_receipt(client.state_dir)
    # Receipt source isn't on this canvas: project access alone cannot make it
    # available through another user's canvas-scoped tool.
    response = client.get('/api/v1/projects/project-a/freezone/canvases/canvas-a/reference-evidence', params={**ref, 'start_us': 0, 'end_us': 1000000})
    assert response.status_code == 404


def test_reference_evidence_route_resolves_project_as_viewer(client, monkeypatch):
    from novelvideo.api.routes import freezone
    calls = []
    original = freezone._resolve_freezone_project
    async def resolve(*args, **kwargs):
        calls.append(kwargs.get('required_role'))
        return await original(*args, **kwargs)
    monkeypatch.setattr(freezone, '_resolve_freezone_project', resolve)
    result = client.get('/api/v1/projects/project-a/freezone/canvases/canvas-a/reference-evidence',
                        params={'artifact_id': 'missing', 'revision': 1})
    assert result.status_code == 404
    assert calls == ['viewer']


def test_evidence_reader_private_canvas_denies_other_actor(client):
    from novelvideo.freezone import canvas_store
    canvas = canvas_store.read_canvas(client.state_dir, 'canvas-a')
    canvas.update(access_model='private', owner_principal_id='another-user', revision=2)
    canvas_store.save_canvas(client.state_dir, 'canvas-a', base_revision=1, build_payload=lambda _: canvas)
    result = client.get('/api/v1/projects/project-a/freezone/canvases/canvas-a/reference-evidence',
                        params={'artifact_id': 'missing', 'revision': 1})
    assert result.status_code == 403


def test_shared_admission_in_progress_keeps_run_recoverable_without_fake_task(client, monkeypatch):
    from novelvideo.freezone.remix import reference_analysis_service as service
    from novelvideo.api.routes import reference_analysis as routes
    async def ensure(ctx, source_ref, requested_evidence, idempotency_key, *, on_reserved):
        await on_reserved('a' * 32)
        return {'operation_id': 'a' * 32, 'task_id': None, 'task_key': 'real-scope', 'status': 'pending'}
    async def release(**kwargs):
        return {'run_id': 'run-a', 'status': 'submitting', 'task_id': None, 'operation_id': 'a' * 32}
    monkeypatch.setattr(service, 'ensure_reference_analysis', ensure)
    monkeypatch.setattr(routes.coordinator, 'release_analysis_submission', release, raising=False)
    response = client.post('/api/v1/projects/project-a/freezone/canvases/canvas-a/reference-analysis-runs',
             json={'source_node_id': 'source-a', 'user_goal': 'Remix', 'idempotency_key': 'agent'})
    assert response.status_code == 200, response.text
    assert response.json()['data']['status'] == 'submitting'
    assert not any(name == 'failed' for name, kwargs in client.calls)


def test_old_public_run_defaults_execution_receipts_without_changing_phase():
    from novelvideo.chat.reference_remix_continuation import public_run
    result = public_run({'status': 'awaiting_direction', 'commission_phase': 'awaiting_direction'})
    assert result['execution_blocker'] is None
    assert result['last_successful_context_receipt'] is None
    assert result['commission_phase'] == 'awaiting_direction'


@pytest.mark.parametrize('proof', ['valid','changed','not_found'])
def test_native_admission_receipt_endpoint_only_validates_original_run(client,monkeypatch,proof):
    from novelvideo.api.routes import reference_analysis as routes
    observed=[]
    async def receipt(**kwargs):
        observed.append(kwargs)
        if proof=='changed':raise ValueError('original source version no longer matches')
        if proof=='not_found':return None
        return {'run_id':'original','project_id':'project-a','canvas_id':'canvas-a','source_node_id':'source-a','agent_id':'director',
            'analysis_node_id':'existing','task_id':'existing-task','task_key':'existing-key','status':'analyzing'}
    monkeypatch.setattr(routes.coordinator,'read_submission_receipt',receipt,raising=False)
    response=client.get('/api/v1/projects/project-a/freezone/canvases/canvas-a/reference-analysis-runs/original/receipt',
        params={'source_node_id':'source-a','agent_id':'director','idempotency_key':'stable'})
    assert response.status_code=={'valid':200,'changed':409,'not_found':404}[proof],response.text
    assert observed[0]['run_id']=='original' and observed[0]['source_node_id']=='source-a'
    assert observed[0]['agent_id']=='director' and observed[0]['idempotency_key']=='stable'
    assert client.calls==[], 'a receipt read must never reserve or enqueue an analysis'


def connect_saved_transcript(client, *, completed=True, speech_status='completed'):
    import hashlib
    from novelvideo.freezone import canvas_store
    from novelvideo.freezone.remix.store import RemixArtifactStore
    digest = 'sha256:' + hashlib.sha256(b'fixture').hexdigest()
    artifact = RemixArtifactStore(client.state_dir, 'project-a').create('word_timeline.v1', {
        'media_hash': digest, 'language': 'zh', 'status': speech_status,
        'transcript': '校字' if speech_status == 'completed' else '',
        'words': [{'text': '校字', 'start_us': 123456, 'end_us': 765432, 'start_char': 0, 'end_char': 2}]
            if speech_status == 'completed' else []}, [])
    ref = {'artifact_id': artifact['id'], 'revision': 1}
    canvas = canvas_store.read_canvas(client.state_dir, 'canvas-a')
    canvas['nodes'].extend([
        {'id': 'words-a', 'type': 'transcriptNode', 'data': {'status': speech_status if completed else 'needs_review',
            'artifactRef': {'artifactId': artifact['id'], 'revision': 1} if completed else None}},
        {'id': 'analysis-a', 'type': 'videoStoryNode', 'data': {'sourceVideoUrl': '/static/projects/project-a/reference.mp4'}}])
    canvas['edges'].extend([{'id': 'v-w', 'source': 'source-a', 'target': 'words-a'},
        {'id': 'w-a', 'source': 'words-a', 'target': 'analysis-a'},
        {'id': 'v-a', 'source': 'source-a', 'target': 'analysis-a'}])
    canvas_store.save_canvas(client.state_dir, 'canvas-a', base_revision=1, build_payload=lambda _: canvas)
    return ref


def test_shared_analysis_reads_exact_connected_corrected_transcript(client):
    ref = connect_saved_transcript(client)
    response = client.post('/api/v1/projects/project-a/freezone/canvases/canvas-a/reference-analysis', json={
        'source_node_id': 'source-a', 'transcript_node_id': 'words-a', 'analysis_node_id': 'analysis-a', 'idempotency_key': 'manual'})
    assert response.status_code == 200, response.text
    assert next(kwargs for name, kwargs in client.calls if name == 'enqueue')['source_ref'] == {
        'media_path': 'reference.mp4', 'transcript_ref': ref}


def test_shared_analysis_reuses_saved_no_speech_observation(client):
    ref = connect_saved_transcript(client, speech_status='no_speech')
    response = client.post('/api/v1/projects/project-a/freezone/canvases/canvas-a/reference-analysis', json={
        'source_node_id': 'source-a', 'transcript_node_id': 'words-a',
        'analysis_node_id': 'analysis-a', 'idempotency_key': 'manual'})
    assert response.status_code == 200, response.text
    assert next(kwargs for name, kwargs in client.calls if name == 'enqueue')['source_ref'] == {
        'media_path': 'reference.mp4', 'no_speech_ref': ref}


@pytest.mark.parametrize('invalid', ['source-link', 'analysis-link', 'video-link', 'type', 'source-url', 'stale-ref'])
def test_shared_analysis_rejects_invalid_transcript_graph_before_enqueue(client, invalid):
    from novelvideo.freezone import canvas_store
    connect_saved_transcript(client)
    canvas = canvas_store.read_canvas(client.state_dir, 'canvas-a')
    edge = {'source-link': 'v-w', 'analysis-link': 'w-a', 'video-link': 'v-a'}.get(invalid)
    if edge:
        canvas['edges'] = [item for item in canvas['edges'] if item['id'] != edge]
    if invalid == 'type':
        canvas['nodes'][1]['type'] = 'textNode'
    if invalid == 'source-url':
        canvas['nodes'][2]['data']['sourceVideoUrl'] = '/static/projects/project-a/other.mp4'
    if invalid == 'stale-ref':
        canvas['nodes'][1]['data']['artifactRef']['revision'] = 999
    canvas_store.save_canvas(client.state_dir, 'canvas-a', base_revision=canvas['revision'], build_payload=lambda _: canvas)
    response = client.post('/api/v1/projects/project-a/freezone/canvases/canvas-a/reference-analysis', json={
        'source_node_id': 'source-a', 'transcript_node_id': 'words-a', 'analysis_node_id': 'analysis-a', 'idempotency_key': 'manual'})
    assert response.status_code in {409, 422}, response.text
    assert client.calls == []


def test_shared_pending_connected_transcript_keeps_single_joint_recognition(client):
    connect_saved_transcript(client, completed=False)
    response = client.post('/api/v1/projects/project-a/freezone/canvases/canvas-a/reference-analysis', json={
        'source_node_id': 'source-a', 'transcript_node_id': 'words-a', 'analysis_node_id': 'analysis-a', 'idempotency_key': 'manual'})
    assert response.status_code == 200, response.text
    assert next(kwargs for name, kwargs in client.calls if name == 'enqueue')['source_ref'] == {'media_path': 'reference.mp4'}


def test_native_analysis_reuses_unambiguous_source_connected_transcript(client):
    ref = connect_saved_transcript(client)
    response = client.post('/api/v1/projects/project-a/freezone/canvases/canvas-a/reference-analysis-runs', json={
        'source_node_id': 'source-a', 'user_goal': '复刻', 'idempotency_key': 'agent'})
    assert response.status_code == 200, response.text
    assert next(kwargs for name, kwargs in client.calls if name == 'enqueue')['source_ref']['transcript_ref'] == ref
    reserved = next(kwargs for name, kwargs in client.calls if name == 'reserve')
    assert reserved['transcript_node_id'] == 'words-a'
    assert reserved['transcript_ref'] == ref


def test_native_analysis_reuses_saved_no_speech_without_new_recognition(client, monkeypatch):
    from novelvideo.freezone import canvas_store
    from novelvideo.freezone.remix import reference_analysis_service
    from novelvideo.api.routes import remix
    monkeypatch.setattr(reference_analysis_service, 'probe_audio_track', lambda _path: True)
    monkeypatch.setattr(remix, '_transcription_engine_ready', lambda: False)
    ref = connect_saved_transcript(client, speech_status='no_speech')
    response = client.post('/api/v1/projects/project-a/freezone/canvases/canvas-a/reference-analysis-runs', json={
        'source_node_id': 'source-a', 'user_goal': '复刻', 'idempotency_key': 'agent'})
    assert response.status_code == 200, response.text
    assert next(kwargs for name, kwargs in client.calls if name == 'enqueue')['source_ref'] == {
        'media_path': 'reference.mp4', 'no_speech_ref': ref}
    reserved = next(kwargs for name, kwargs in client.calls if name == 'reserve')
    assert reserved['transcript_node_id'] == 'words-a'
    assert reserved['transcript_status'] == 'no_speech'
    assert {node['id'] for node in canvas_store.read_canvas(client.state_dir, 'canvas-a')['nodes']} == {
        'source-a', 'words-a', 'analysis-a'}


@pytest.mark.parametrize('old_ref', [{'artifactId': 'old', 'revision': 1}, None])
def test_native_analysis_does_not_adopt_older_transcript_revision(client, monkeypatch, old_ref):
    from novelvideo.freezone import canvas_store
    from novelvideo.api.routes import reference_analysis as routes
    ref = connect_saved_transcript(client)
    canvas = canvas_store.read_canvas(client.state_dir, 'canvas-a')
    canvas['nodes'][2]['data'].update(rawResult={'video_story': {'summary': '旧分析'}},
        referenceTranscriptRef=old_ref)
    canvas_store.save_canvas(client.state_dir, 'canvas-a', base_revision=canvas['revision'], build_payload=lambda _: canvas)
    async def adopt(**kwargs):
        pytest.fail('older transcript revision must not be adopted')
    monkeypatch.setattr(routes.coordinator, 'adopt_saved_analysis', adopt, raising=False)
    response = client.post('/api/v1/projects/project-a/freezone/canvases/canvas-a/reference-analysis-runs', json={
        'source_node_id': 'source-a', 'user_goal': '复刻', 'idempotency_key': 'agent'})
    assert response.status_code == 200, response.text
    assert next(kwargs for name, kwargs in client.calls if name == 'enqueue')['source_ref']['transcript_ref'] == ref


def test_native_analysis_rejects_ambiguous_completed_transcripts(client):
    from novelvideo.freezone import canvas_store
    connect_saved_transcript(client)
    canvas = canvas_store.read_canvas(client.state_dir, 'canvas-a')
    extra = deepcopy(canvas['nodes'][1])
    extra['id'] = 'words-b'
    canvas['nodes'].append(extra)
    canvas['edges'].append({'id': 'v-w-b', 'source': 'source-a', 'target': 'words-b'})
    canvas_store.save_canvas(client.state_dir, 'canvas-a', base_revision=canvas['revision'], build_payload=lambda _: canvas)
    response = client.post('/api/v1/projects/project-a/freezone/canvases/canvas-a/reference-analysis-runs', json={
        'source_node_id': 'source-a', 'user_goal': '复刻', 'idempotency_key': 'agent'})
    assert response.status_code == 409
    assert client.calls == []


def test_native_analysis_checks_actual_saved_analysis_transcript_lineage(client, monkeypatch):
    import hashlib
    from novelvideo.freezone import canvas_store
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.api.routes import reference_analysis as routes
    ref = connect_saved_transcript(client)
    store = RemixArtifactStore(client.state_dir, 'project-a')
    digest = 'sha256:' + hashlib.sha256(b'fixture').hexdigest()
    evidence = store.create('reference_evidence.v1', {'media_hash': digest, 'time_base': '1/1000',
        'duration_us': 1000000, 'cuts': [], 'representative_frames': []}, [])
    evidence_ref = {'artifact_id': evidence['id'], 'revision': 1}
    saved = store.create('reference_analysis.v1', {'media_path': 'reference.mp4', 'media_hash': digest,
        'evidence_ref': evidence_ref, 'transcript_ref': None, 'transcript_status': 'no_audio',
        'story': {'video_story': {'summary': '无声旧分析'}}, 'coverage': {}}, [evidence_ref])
    canvas = canvas_store.read_canvas(client.state_dir, 'canvas-a')
    canvas['nodes'][2]['data'].update(rawResult={'video_story': {'summary': '无声旧分析'}},
        referenceTranscriptRef={'artifactId': ref['artifact_id'], 'revision': 1},
        referenceAnalysisRef={'artifactId': saved['id'], 'revision': 1})
    canvas_store.save_canvas(client.state_dir, 'canvas-a', base_revision=canvas['revision'], build_payload=lambda _: canvas)
    async def adopt(**kwargs):
        pytest.fail('display reference cannot replace real artifact lineage')
    monkeypatch.setattr(routes.coordinator, 'adopt_saved_analysis', adopt, raising=False)
    response = client.post('/api/v1/projects/project-a/freezone/canvases/canvas-a/reference-analysis-runs', json={
        'source_node_id': 'source-a', 'user_goal': '复刻', 'idempotency_key': 'agent'})
    assert response.status_code == 200, response.text
    assert next(kwargs for name, kwargs in client.calls if name == 'enqueue')['source_ref']['transcript_ref'] == ref


@pytest.mark.parametrize('native', [True, False])
@pytest.mark.parametrize('busy', [{'operationId': 'standalone'}, {'autoStartMode': 'transcribe'}, {'managedOperationId': 'submitting'}, {'transcriptSubmissionPending': 'real-request-pending'}, {'transcriptSubmissionRequest': {'request': {'idempotencyKey': 'original-pending-key'}}}])
def test_canvas_analysis_defers_existing_standalone_recognition(client, native, busy):
    from novelvideo.freezone import canvas_store
    connect_saved_transcript(client, completed=False)
    canvas = canvas_store.read_canvas(client.state_dir, 'canvas-a')
    canvas['nodes'][1]['data'].update(busy)
    canvas_store.save_canvas(client.state_dir, 'canvas-a', base_revision=canvas['revision'], build_payload=lambda _: canvas)
    endpoint = 'reference-analysis-runs' if native else 'reference-analysis'
    body = {'source_node_id': 'source-a', 'idempotency_key': 'pending', **({'user_goal': '复刻'} if native else {'transcript_node_id': 'words-a', 'analysis_node_id': 'analysis-a'})}
    response = client.post('/api/v1/projects/project-a/freezone/canvases/canvas-a/' + endpoint, json=body)
    assert response.status_code in {409, 422}, response.text
    assert '处理中' in response.json()['detail']
    assert client.calls == []


def test_shared_analysis_rejects_multiple_saved_target_transcript_dependencies(client):
    from novelvideo.freezone import canvas_store
    connect_saved_transcript(client)
    canvas = canvas_store.read_canvas(client.state_dir, 'canvas-a')
    another = deepcopy(canvas['nodes'][1]); another['id'] = 'words-b'
    canvas['nodes'].append(another)
    canvas['edges'].extend([{'id': 'v-b', 'source': 'source-a', 'target': 'words-b'}, {'id': 'b-a', 'source': 'words-b', 'target': 'analysis-a'}])
    canvas_store.save_canvas(client.state_dir, 'canvas-a', base_revision=canvas['revision'], build_payload=lambda _: canvas)
    response = client.post('/api/v1/projects/project-a/freezone/canvases/canvas-a/reference-analysis', json={
        'source_node_id': 'source-a', 'transcript_node_id': 'words-a', 'analysis_node_id': 'analysis-a', 'idempotency_key': 'ambiguous'})
    assert response.status_code == 422
    assert client.calls == []
