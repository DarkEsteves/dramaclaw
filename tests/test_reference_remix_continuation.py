from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from contextlib import asynccontextmanager

import pytest

from novelvideo.chat import reference_remix_continuation as continuation
from novelvideo.freezone import canvas_store
from novelvideo.ports.local.canvas_mutex import FileLockCanvasWriteMutex
from novelvideo.project_context import ProjectContext
from test_plan_confirmation_contract import reviewed_plan  # noqa: F401, F811
from test_reference_director_api import setup_api  # noqa: F401


def context(tmp_path: Path, actor: str = "actor", project: str = "project") -> ProjectContext:
    return ProjectContext(project, "demo", "user", "owner", "owner", actor, actor,
                          (("user", actor),), "editor", "local", tmp_path / "output",
                          tmp_path / "state", tmp_path / "runtime", True)


def write_canvas(ctx: ProjectContext, *, private: bool = False) -> None:
    path = canvas_store.canvas_path(ctx.state_dir, "canvas")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "project_id": ctx.project_id, "canvas_id": "canvas", "revision": 1,
        "access_model": "private" if private else "project_role", "owner_principal_id": "actor",
        "nodes": [{"id": "source", "type": "videoNode", "data": {"videoUrl": "/video.mp4", "displayName": "原片"}},
                  {"id": "analysis", "type": "videoStoryNode", "data": {"sourceVideoUrl": "/video.mp4", "rows": [], "isAnalyzing": True}}],
        "edges": [{"id": "link", "source": "source", "target": "analysis"}],
    }), encoding="utf-8")


@pytest.fixture
def service(tmp_path: Path, monkeypatch):
    if not hasattr(continuation, "ReferenceRemixCoordinator"):
        pytest.skip("coordinator contract is not implemented yet")
    monkeypatch.setattr(canvas_store, "get_canvas_write_mutex", lambda: FileLockCanvasWriteMutex())
    monkeypatch.setattr(continuation, "POLL_SECONDS", 0.01)
    coordinator = continuation.ReferenceRemixCoordinator(continuation.ReferenceRemixStore(tmp_path / "runs.db"))
    monkeypatch.setattr(coordinator, "_ensure_worker", lambda _run: None)
    return coordinator


def test_durable_continuation_contract_is_available():
    assert hasattr(continuation, "ReferenceRemixCoordinator"), "reference analysis lacks a durable continuation service"


async def reserve(service, ctx, **kwargs):
    return await service.reserve(ctx=ctx, canvas_id="canvas", source_node_id="source",
                                 analysis_node_id="analysis", goal="复刻这个视频", inputs={"plan_count": 1},
                                 idempotency_key="request", **kwargs)


@pytest.mark.asyncio
async def test_ee_auto_continuation_does_not_start_recovery_workers(service, monkeypatch):
    from novelvideo.chat.reference_production_observer import ReferenceProductionObserver

    monkeypatch.setenv("ST_CONTROL_PLANE_DSN", "postgresql://configured")
    monkeypatch.delenv("ST_REFERENCE_REMIX_AUTO_CONTINUATION", raising=False)
    monkeypatch.setattr(service.store, "recover_expired_resume", lambda: pytest.fail("recovery was started"))
    observer = ReferenceProductionObserver(service)
    await service.resume()
    await observer.resume()
    assert service._scheduler is None
    assert service._draft_scheduler is None
    assert service._interaction_scheduler is None
    assert observer._worker is None


def test_ee_disabled_production_recovery_does_not_open_continuation_store(service, tmp_path, monkeypatch):
    ctx = context(tmp_path)
    write_canvas(ctx)
    monkeypatch.setenv("ST_CONTROL_PLANE_DSN", "postgresql://configured")
    monkeypatch.delenv("ST_REFERENCE_REMIX_AUTO_CONTINUATION", raising=False)
    monkeypatch.setattr(service.store, "production_for_actor",
                        lambda *args: pytest.fail("disabled recovery opened continuation state"))
    canvas = service._canvas(ctx, "canvas")
    assert service.production_recovery_candidates(ctx, canvas_id="canvas",
        agent_id="main", canvas=canvas) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('speech_status', ['completed', 'no_speech'])
async def test_native_agent_transcript_is_saved_before_visual_analysis_submission(service, tmp_path, monkeypatch, speech_status):
    import hashlib
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.freezone.remix import reference_analysis_service

    ctx = context(tmp_path)
    ctx.output_dir.mkdir(parents=True)
    (ctx.output_dir / 'video.mp4').write_bytes(b'video-with-speech')
    digest = hashlib.sha256(b'video-with-speech').hexdigest()
    write_canvas(ctx)
    original = canvas_store.load_canvas_json(canvas_store.canvas_path(ctx.state_dir, 'canvas'))
    original['nodes'].append({'id': 'reference-transcript-' + 'x' * 24, 'type': 'transcriptNode',
        'data': {'displayName': '原片转写', 'sourceMediaPath': 'video.mp4', 'sourceMediaHash': digest,
                 'status': 'needs_review', 'words': [], 'managedOperationId': 'pending'}})
    original['edges'].extend([{'source': 'source', 'target': 'reference-transcript-' + 'x' * 24},
        {'source': 'reference-transcript-' + 'x' * 24, 'target': 'analysis'}])
    path = canvas_store.canvas_path(ctx.state_dir, 'canvas')
    path.write_text(json.dumps(original), encoding='utf-8')
    run = await reserve(service, ctx, create_transcript=True, transcript_media_path='video.mp4', transcript_media_hash=digest)
    # The durable admission chooses the deterministic transcript ID. Mirror the
    # route's saved node after the reservation has fixed that identity.
    current = canvas_store.load_canvas_json(path)
    for node in current['nodes']:
        if node['type'] == 'transcriptNode': node['id'] = run['transcript_node_id']
    for edge in current['edges']:
        if edge['source'].startswith('reference-transcript-'): edge['source'] = run['transcript_node_id']
        if edge['target'].startswith('reference-transcript-'): edge['target'] = run['transcript_node_id']
    path.write_text(json.dumps(current), encoding='utf-8')
    token = await service.claim_submission(ctx=ctx, run_id=run['run_id'])
    await service.bind_transcript_operation(ctx=ctx, run_id=run['run_id'], operation_id='b' * 32, submission_token=token)
    attached = await service.attach_task(ctx=ctx, run_id=run['run_id'], task_id='speech-task',
        task_key='speech-key', submission_token=token)
    assert attached['status'] == 'transcribing'
    artifact = RemixArtifactStore(ctx.output_dir, ctx.project_id).create('word_timeline.v1', {
        'media_hash': 'sha256:' + digest, 'language': 'zh', 'status': speech_status,
        'transcript': '你好' if speech_status == 'completed' else '',
        'words': [{'text': '你', 'start_us': 0, 'end_us': 100000, 'start_char': 0, 'end_char': 1},
                  {'text': '好', 'start_us': 100000, 'end_us': 200000, 'start_char': 1, 'end_char': 2}]
                 if speech_status == 'completed' else []}, [])
    ref = {'artifact_id': artifact['id'], 'revision': artifact['revision']}
    task = SimpleNamespace(task_id='speech-task', project_id=ctx.project_id,
        requester_user_id=ctx.requester_user_id, status='completed', error=None,
        result={'artifact_ref': ref})
    monkeypatch.setattr(continuation, 'get_task_manager', lambda: SimpleNamespace(
        get_task_for_project=lambda *_args, **_kwargs: task))
    async def resolved(_run): return ctx
    monkeypatch.setattr(service, '_resolve_context', resolved)
    def operation(_self, operation_id):
        assert operation_id == 'b' * 32
        return {'task_id': 'speech-task', 'normalized_parameters': {
            'action': 'transcribe_media', 'payload': {'media_path': 'video.mp4'}, 'media_sha256': digest},
            'output_refs': [ref], 'status': 'needs_confirmation',
            'input_media_hashes': ['sha256:' + digest]}
    monkeypatch.setattr(RemixOperationStore, 'get', operation)
    from novelvideo.freezone import paths
    monkeypatch.setattr(paths, 'resolve_static_url_to_path', lambda *_args: ctx.output_dir / 'video.mp4')
    receipt = await service.read_submission_receipt(ctx=ctx, run_id=run['run_id'], canvas_id='canvas',
        source_node_id='source', agent_id='main', idempotency_key='request')
    assert receipt['status'] == 'transcribing' and receipt['task_id'] == 'speech-task'
    calls = []
    async def ensure(_ctx, source_ref, _evidence, _key, *, on_reserved):
        saved = canvas_store.load_canvas_json(path)
        transcript = next(node for node in saved['nodes'] if node['type'] == 'transcriptNode')
        assert transcript['data']['status'] == speech_status
        assert transcript['data']['artifactRef'] == {'artifactId': ref['artifact_id'], 'revision': 1}
        calls.append(source_ref)
        await on_reserved('c' * 32)
        return {'task_id': 'analysis-task', 'task_key': 'analysis-key'}
    monkeypatch.setattr(reference_analysis_service, 'ensure_reference_analysis', ensure)
    assert service.store.claim(run['run_id'], service.owner_token)
    await service._process_once(service.store.get(run['run_id']))
    updated = service.store.get(run['run_id'])
    assert updated['status'] == 'analyzing' and updated['task_id'] == 'analysis-task'
    assert calls == [{'media_path': 'video.mp4',
                      'transcript_ref' if speech_status == 'completed' else 'no_speech_ref': ref}]


@pytest.mark.asyncio
@pytest.mark.parametrize('operation_bound', [False, True])
async def test_native_agent_recovers_submission_after_graph_save_before_speech_admission(service, tmp_path, monkeypatch, operation_bound):
    import hashlib
    from novelvideo.api.routes import reference_analysis as routes
    ctx = context(tmp_path)
    ctx.output_dir.mkdir(parents=True)
    (ctx.output_dir / 'video.mp4').write_bytes(b'video')
    digest = hashlib.sha256(b'video').hexdigest()
    write_canvas(ctx)
    run = await reserve(service, ctx, create_transcript=True,
                        transcript_media_path='video.mp4', transcript_media_hash=digest)
    path = canvas_store.canvas_path(ctx.state_dir, 'canvas')
    canvas = canvas_store.load_canvas_json(path)
    analysis = next(node for node in canvas['nodes'] if node['id'] == 'analysis')
    analysis['data']['referenceAnalysisRunId'] = run['run_id']
    canvas['nodes'].append({'id': run['transcript_node_id'], 'type': 'transcriptNode',
        'data': {'sourceMediaPath': 'video.mp4', 'sourceMediaHash': digest,
                 'status': 'needs_review', 'words': [],
                 'managedOperationId': 'reference-run:' + run['run_id']}})
    canvas['edges'].extend([{'source': 'source', 'target': run['transcript_node_id']},
                            {'source': run['transcript_node_id'], 'target': 'analysis'}])
    path.write_text(json.dumps(canvas), encoding='utf-8')
    async def resolved(_run): return ctx
    monkeypatch.setattr(service, '_resolve_context', resolved)
    calls = []
    async def enqueue(_ctx, *, media_path, media_hash, idempotency_key, on_reserved):
        calls.append((media_path, media_hash, idempotency_key))
        await on_reserved('b' * 32)
        return {'task_id': 'speech-task', 'task_key': 'speech-key'}
    monkeypatch.setattr(routes, '_enqueue_reference_transcript', enqueue)
    if operation_bound:
        token = await service.claim_submission(ctx=ctx, run_id=run['run_id'])
        await service.bind_transcript_operation(ctx=ctx, run_id=run['run_id'],
            operation_id='b' * 32, submission_token=token)
        service.store.release(run['run_id'], token)
    assert service.store.claim(run['run_id'], service.owner_token)
    await service._process_once(service.store.get(run['run_id']))
    updated = service.store.get(run['run_id'])
    assert updated['status'] == 'transcribing' and updated['task_id'] == 'speech-task'
    assert calls == [('video.mp4', digest, 'reference-transcript:' + run['run_id'])]


@pytest.mark.asyncio
async def test_reservation_reuses_identity_but_rejects_changed_request(service, tmp_path):
    ctx = context(tmp_path)
    write_canvas(ctx)
    first, second = await asyncio.gather(reserve(service, ctx), reserve(service, ctx))
    assert first["run_id"] == second["run_id"]
    assert first["job_id"] == second["job_id"]
    assert first["status"] == "submitting"
    with pytest.raises(ValueError, match="idempotency"):
        await service.reserve(ctx=ctx, canvas_id="canvas", source_node_id="source", goal="不同目标",
                              inputs={}, idempotency_key="request")
    token = await service.claim_submission(ctx=ctx, run_id=first["run_id"])
    assert token
    assert await service.claim_submission(ctx=ctx, run_id=first["run_id"]) is None


@pytest.mark.asyncio
async def test_reservation_retry_survives_manual_source_metadata_completion(service, tmp_path):
    ctx = context(tmp_path)
    write_canvas(ctx)
    first = await reserve(service, ctx)
    path = canvas_store.canvas_path(ctx.state_dir, "canvas")
    payload = canvas_store.load_canvas_json(path)
    payload["nodes"][0]["data"].update({
        "remixRole": "reference",
        "remixMedia": {"media_path": "video.mp4", "media_hash": "same-source-hash",
                       "media_kind": "video", "url": "/video.mp4"},
    })
    path.write_text(json.dumps(payload), encoding="utf-8")
    retried = await reserve(service, ctx)
    assert retried["run_id"] == first["run_id"]


@pytest.mark.asyncio
async def test_run_lookup_cannot_cross_actor_or_project(service, tmp_path):
    ctx = context(tmp_path)
    write_canvas(ctx)
    run = await reserve(service, ctx)
    assert await service.get(ctx=context(tmp_path, actor="other"), run_id=run["run_id"]) is None
    assert await service.get(ctx=context(tmp_path, project="other"), run_id=run["run_id"]) is None
    assert await service.list(ctx=context(tmp_path, actor="other"), canvas_id="canvas") == []


@pytest.mark.asyncio
async def test_explicit_private_canvas_rejects_other_project_editor(service, tmp_path):
    ctx = context(tmp_path, actor="other")
    write_canvas(ctx, private=True)
    with pytest.raises(PermissionError):
        await reserve(service, ctx)


async def ready_run(service, ctx, monkeypatch, *, task_status="completed"):
    write_canvas(ctx)
    run = await reserve(service, ctx)
    token = await service.claim_submission(ctx=ctx, run_id=run["run_id"])
    task = SimpleNamespace(task_id="task", project_id=ctx.project_id, requester_user_id=ctx.requester_user_id,
                           task_type="freezone_video_story", status=task_status, progress=1,
                           error="Vision unavailable", result={"job_id": run["job_id"], "video_story": {"title": "标题", "shots": [{"shot": 1, "narrative": "开场"}]}})
    monkeypatch.setattr(continuation, "get_task_manager", lambda: SimpleNamespace(get_task_for_project=lambda *_args, **_kwargs: task, _project_key=lambda *_args, **_kwargs: "actual-task-key"))
    async def resolve(**_kwargs):
        return ctx
    monkeypatch.setattr(continuation, "resolve_project_context", resolve)
    await service.attach_task(ctx=ctx, run_id=run["run_id"], task_id="task", submission_token=token)
    assert service.store.claim(run["run_id"], service.owner_token)
    return service.store.get(run["run_id"]), task


@pytest.mark.asyncio
async def test_completed_analysis_is_saved_before_one_agent_continuation(service, tmp_path, monkeypatch):
    ctx = context(tmp_path)
    run, _task = await ready_run(service, ctx, monkeypatch)
    observed = []
    async def resume_agent(_run, refreshed_ctx):
        payload = canvas_store.load_canvas_json(canvas_store.canvas_path(refreshed_ctx.state_dir, "canvas"))
        observed.append(payload["nodes"][1]["data"])
    monkeypatch.setattr(service, "_resume_agent", resume_agent)
    await service._process_once(run)
    assert observed[0]["rawResult"]["video_story"]["title"] == "标题"
    assert observed[0]["remixAnalysisStage"] == "completed"
    assert observed[0]["isAnalyzing"] is False
    latest = service.store.get(run["run_id"])
    assert latest["status"] == "completed"
    await service._process_once(latest)
    assert len(observed) == 1
    from novelvideo.chat.store import ChatStore
    messages = ChatStore().list_messages(ctx.requester_username, service.scope(latest), limit=20)
    assert not [message for message in messages if message.get("message_kind") == "reference_remix_status"]


@pytest.mark.asyncio
async def test_changed_source_does_not_write_analysis_or_resume(service, tmp_path, monkeypatch):
    ctx = context(tmp_path)
    run, _task = await ready_run(service, ctx, monkeypatch)
    path = canvas_store.canvas_path(ctx.state_dir, "canvas")
    payload = canvas_store.load_canvas_json(path)
    payload["nodes"][0]["data"]["videoUrl"] = "/replacement.mp4"
    path.write_text(json.dumps(payload), encoding="utf-8")
    async def forbidden(*_args):
        pytest.fail("stale source must not resume")
    monkeypatch.setattr(service, "_resume_agent", forbidden)
    await service._process_once(run)
    assert service.store.get(run["run_id"])["status"] == "stale"
    assert canvas_store.load_canvas_json(path)["nodes"][1]["data"]["rows"] == []


@pytest.mark.asyncio
async def test_manual_analysis_branch_does_not_stale_agent_bound_analysis(service, tmp_path, monkeypatch):
    ctx = context(tmp_path)
    run, _task = await ready_run(service, ctx, monkeypatch)
    path = canvas_store.canvas_path(ctx.state_dir, "canvas")
    payload = canvas_store.load_canvas_json(path)
    payload["nodes"][0]["data"].update({
        "remixRole": "reference",
        "remixMedia": {"media_path": "video.mp4", "media_hash": "same-source-hash",
                       "media_kind": "video", "url": "/video.mp4", "has_audio": True},
    })
    payload["nodes"].append({"id": "manual-analysis", "type": "videoStoryNode",
                             "data": {"sourceVideoUrl": "/video.mp4", "isAnalyzing": True}})
    payload["edges"].append({"id": "manual-link", "source": "source", "target": "manual-analysis"})
    path.write_text(json.dumps(payload), encoding="utf-8")
    resumed = []

    async def resume_agent(_run, _ctx):
        resumed.append(_run["analysis_node_id"])

    monkeypatch.setattr(service, "_resume_agent", resume_agent)
    await service._process_once(run)
    assert service.store.get(run["run_id"])["status"] == "completed"
    assert resumed == ["analysis"]
    saved = canvas_store.load_canvas_json(path)
    assert saved["nodes"][1]["data"]["rawResult"]["video_story"]["title"] == "标题"
    assert saved["nodes"][2]["data"]["isAnalyzing"] is True


@pytest.mark.asyncio
async def test_manual_metadata_cannot_hide_source_replacement(service, tmp_path, monkeypatch):
    ctx = context(tmp_path)
    run, _task = await ready_run(service, ctx, monkeypatch)
    path = canvas_store.canvas_path(ctx.state_dir, "canvas")
    payload = canvas_store.load_canvas_json(path)
    payload["nodes"][0]["data"].update({
        "remixRole": "reference",
        "remixMedia": {"media_path": "replacement.mp4", "media_hash": "replacement-hash",
                       "media_kind": "video", "url": "/replacement.mp4"},
    })
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(continuation.StaleReference):
        service._assert_current(ctx, run)


def test_source_fence_rejects_replacing_existing_media_hash():
    pinned = {"type": "videoNode", "data": {"videoUrl": "/video.mp4",
              "remixMedia": {"media_path": "video.mp4", "media_hash": "original",
                             "media_kind": "video", "url": "/video.mp4"}}}
    run = {"source_fingerprint": continuation._node_fingerprint(pinned),
           "source_node_snapshot": pinned}
    changed = json.loads(json.dumps(pinned))
    changed["data"]["remixMedia"]["media_hash"] = "replacement"
    assert continuation._source_node_matches(changed, run) is False


def test_source_fence_rejects_added_media_hash_that_disagrees_with_pending_transcript():
    pinned = {"type": "videoNode", "data": {"videoUrl": "/video.mp4"}}
    run = {"source_fingerprint": continuation._node_fingerprint(pinned),
           "source_node_snapshot": pinned, "pending_transcript": {"media_hash": "original"}}
    changed = json.loads(json.dumps(pinned))
    changed["data"]["remixMedia"] = {"media_path": "video.mp4", "media_hash": "replacement",
                                     "media_kind": "video", "url": "/video.mp4"}
    assert continuation._source_node_matches(changed, run) is False


@pytest.mark.asyncio
async def test_changed_analysis_does_not_overwrite_manual_edit(service, tmp_path, monkeypatch):
    ctx = context(tmp_path)
    run, _task = await ready_run(service, ctx, monkeypatch)
    path = canvas_store.canvas_path(ctx.state_dir, "canvas")
    payload = canvas_store.load_canvas_json(path)
    payload["nodes"][1]["data"]["rows"] = [{"narrative": "用户修改"}]
    path.write_text(json.dumps(payload), encoding="utf-8")
    await service._process_once(run)
    assert service.store.get(run["run_id"])["status"] == "stale"
    assert canvas_store.load_canvas_json(path)["nodes"][1]["data"]["rows"] == [{"narrative": "用户修改"}]


@pytest.mark.asyncio
async def test_model_failure_retains_analysis_and_retry_does_not_enqueue(service, tmp_path, monkeypatch, caplog):
    ctx = context(tmp_path)
    run, _task = await ready_run(service, ctx, monkeypatch)
    calls = []
    async def resume_agent(_run, _ctx):
        calls.append(_run["run_id"])
        if len(calls) == 1:
            raise ConnectionError("model connection failed; token=private https://media.example/signed?sig=private")
    monkeypatch.setattr(service, "_resume_agent", resume_agent)
    await service._process_once(run)
    latest = service.store.get(run["run_id"])
    assert latest["status"] == "resume_failed"
    diagnostics = [record.message for record in caplog.records if 'reference_runtime_failure' in record.message]
    assert len(diagnostics) == 1
    assert '"run_id"' in diagnostics[0] and 'model connection failed' in diagnostics[0]
    assert 'private' not in diagnostics[0] and 'media.example' not in diagnostics[0]
    assert canvas_store.load_canvas_json(canvas_store.canvas_path(ctx.state_dir, "canvas"))["nodes"][1]["data"]["rawResult"]
    from novelvideo.chat.store import ChatStore
    messages = ChatStore().list_messages(ctx.requester_username, service.scope(latest), limit=20)
    assert not [message for message in messages if message.get("message_kind") == "reference_remix_status"]
    service.store.release(run["run_id"], service.owner_token)
    retry = await service.retry(ctx=ctx, run_id=run["run_id"])
    assert retry["task_id"] == "task"
    assert retry["status"] == "analysis_ready"
    assert service.store.claim(run["run_id"], service.owner_token)
    await service._process_once(service.store.get(run["run_id"]))
    assert service.store.get(run["run_id"])["status"] == "completed"
    assert any('reference_runtime_recovered' in record.message for record in caplog.records)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_failed_analysis_reports_error_without_agent_turn(service, tmp_path, monkeypatch):
    ctx = context(tmp_path)
    run, _task = await ready_run(service, ctx, monkeypatch, task_status="failed")
    async def forbidden(*_args):
        pytest.fail("failed analysis must not resume")
    monkeypatch.setattr(service, "_resume_agent", forbidden)
    await service._process_once(run)
    assert service.store.get(run["run_id"])["status"] == "analysis_failed"
    assert canvas_store.load_canvas_json(canvas_store.canvas_path(ctx.state_dir, "canvas"))["nodes"][1]["data"]["analysisError"] == "Vision unavailable"
    from novelvideo.chat.store import ChatStore
    messages = ChatStore().list_messages(ctx.requester_username, service.scope(run), limit=20)
    assert not [message for message in messages if message.get("message_kind") == "reference_remix_status"]


def test_expired_resume_is_not_automatically_dispatched_twice(service, tmp_path):
    record = {"run_id": "run", "project_id": "project", "requester_user_id": "actor", "canvas_id": "canvas", "agent_id": "main",
              "idempotency_key": "key", "status": "resuming", "request_fingerprint": "fp", "created_at": "now", "updated_at": "now"}
    service.store.create(record)
    assert service.store.claim("run", "dead-worker", now=0)
    assert service.store.recover_expired_resume(now=100) == 1
    assert service.store.get("run")["status"] == "resume_failed"


@pytest.mark.asyncio
async def test_restart_after_canvas_result_write_recovers_without_overwrite(service, tmp_path, monkeypatch):
    ctx = context(tmp_path)
    run, task = await ready_run(service, ctx, monkeypatch)
    # A process stops between the mutex-protected result save and its state
    # transition. Recovery accepts exactly that task's saved result.
    await service._save_analysis(ctx, run, result=task.result)
    called = []
    async def resume_agent(*_args):
        called.append(True)
    monkeypatch.setattr(service, "_resume_agent", resume_agent)
    await service._process_once(service.store.get(run["run_id"]))
    assert service.store.get(run["run_id"])["status"] == "completed"
    assert called == [True]


@pytest.mark.asyncio
async def test_lost_lease_cannot_write_result(service, tmp_path, monkeypatch):
    ctx = context(tmp_path)
    run, task = await ready_run(service, ctx, monkeypatch)
    service.store.release(run["run_id"], service.owner_token)
    assert service.store.claim(run["run_id"], "new-worker")
    with pytest.raises(ValueError, match="lease"):
        await service._save_analysis(ctx, run, result=task.result)
    assert canvas_store.load_canvas_json(canvas_store.canvas_path(ctx.state_dir, "canvas"))["nodes"][1]["data"]["rows"] == []


@pytest.mark.asyncio
async def test_each_worker_claim_has_a_distinct_fencing_token(service, monkeypatch):
    record = dict(run_id='claim-generation', project_id='project', requester_user_id='actor',
        canvas_id='canvas', agent_id='main', idempotency_key='claim-generation',
        request_fingerprint='claim-generation', status='analysis_ready')
    service.store.create(record)
    claims = []
    original_claim = service.store.claim

    def capture_claim(run_id, token, **kwargs):
        claims.append(token)
        return original_claim(run_id, token, **kwargs)

    monkeypatch.setattr(service.store, 'claim', capture_claim)
    async def process(_run): return True
    async def recover(**kwargs): return None
    monkeypatch.setattr(service, '_process_once', process)
    monkeypatch.setattr(service, 'recover_interactions', recover)
    await service._run(record['run_id'])
    await service._run(record['run_id'])
    assert len(claims) == 2
    assert claims[0] != claims[1]


@pytest.mark.asyncio
async def test_lost_lease_cannot_publish_transcript_failure(service, tmp_path, monkeypatch):
    ctx = context(tmp_path)
    write_canvas(ctx)
    path = canvas_store.canvas_path(ctx.state_dir, 'canvas')
    canvas = canvas_store.load_canvas_json(path)
    canvas['nodes'].append({'id': 'transcript', 'type': 'transcriptNode', 'data': {'status': 'needs_review'}})
    path.write_text(json.dumps(canvas))
    run = dict(run_id='transcript-fence', project_id=ctx.project_id,
        requester_user_id=ctx.requester_user_id, canvas_id='canvas', agent_id='main',
        idempotency_key='transcript-fence', request_fingerprint='transcript-fence',
        status='transcribing', analysis_node_id='analysis',
        pending_transcript={'node_id': 'transcript'})
    service.store.create(run)
    monkeypatch.setattr(service, '_assert_current', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, '_assert_pending_transcript', lambda *_args, **_kwargs: None)
    before = path.read_bytes()
    with pytest.raises(ValueError, match='lease'):
        await service._save_native_transcript_failure(ctx, run, 'late failure')
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_startup_additive_coordinator_is_nonfatal(monkeypatch):
    from novelvideo.api import app
    assert hasattr(app, "_resume_reference_remix_best_effort"), "API startup must recover reference continuations"
    async def fail():
        raise RuntimeError("unavailable")
    monkeypatch.setattr(continuation.coordinator, "resume", fail)
    application = SimpleNamespace(state=SimpleNamespace())
    await app._resume_reference_remix_best_effort(application)
    assert application.state.reference_remix_available is False


@pytest.mark.asyncio
async def test_ee_disabled_continuation_keeps_api_startup_available(monkeypatch):
    from novelvideo.api import app

    monkeypatch.setenv('ST_CONTROL_PLANE_DSN', 'postgresql://configured')
    monkeypatch.delenv('ST_REFERENCE_REMIX_AUTO_CONTINUATION', raising=False)
    async def unexpected_resume():
        pytest.fail('disabled EE startup must not open the continuation store')
    monkeypatch.setattr(continuation.coordinator, 'resume', unexpected_resume)
    application = SimpleNamespace(state=SimpleNamespace())
    await app._resume_reference_remix_best_effort(application)
    assert application.state.reference_remix_available is False
    assert 'disabled' in application.state.reference_remix_startup_error


@pytest.mark.asyncio
async def test_home_migration_does_not_resume_a_saved_run(service, tmp_path, monkeypatch):
    original = replace(context(tmp_path), home_node_id='home-a')
    moved = replace(original, home_node_id='home-b')

    async def resolve_project_context(**kwargs):
        return moved

    monkeypatch.setattr(continuation, 'resolve_project_context', resolve_project_context)
    with pytest.raises(continuation.StaleReference, match='归属'):
        await service._resolve_context({'project_id': original.project_id,
            'requester_user_id': original.requester_user_id,
            'username': original.requester_username,
            'context_json': continuation._serialize_context(original)})


@pytest.mark.asyncio
async def test_wrong_home_observer_does_not_mark_run_failed(service, tmp_path, monkeypatch):
    ctx = replace(context(tmp_path), home_node_id='other-home', is_home_node=False)
    run = {'run_id': 'wrong-home', 'project_id': ctx.project_id,
        'requester_user_id': ctx.requester_user_id, 'username': ctx.requester_username,
        'canvas_id': 'canvas', 'agent_id': 'main', 'idempotency_key': 'wrong-home',
        'request_fingerprint': 'wrong-home', 'status': 'analysis_ready',
        'context_json': continuation._serialize_context(ctx)}
    service.store.create(run)

    async def resolve_project_context(**kwargs):
        return ctx

    monkeypatch.setattr(continuation, 'resolve_project_context', resolve_project_context)
    assert await service._process_once(run) is False
    assert service.store.get(run['run_id'])['status'] == 'analysis_ready'


@pytest.mark.asyncio
async def test_adopting_saved_analysis_uses_real_result_without_a_new_task(service, tmp_path):
    ctx = context(tmp_path)
    write_canvas(ctx)
    path = canvas_store.canvas_path(ctx.state_dir, "canvas")
    payload = canvas_store.load_canvas_json(path)
    payload["nodes"][1]["data"].update(rawResult={"video_story": {"summary": "已经完成的分析"}}, isAnalyzing=False)
    path.write_text(json.dumps(payload), encoding="utf-8")
    run = await reserve(service, ctx)
    token = await service.claim_submission(ctx=ctx, run_id=run["run_id"])
    assert hasattr(service, "adopt_saved_analysis"), "successful reference results must be reusable"
    adopted = await service.adopt_saved_analysis(ctx=ctx, run_id=run["run_id"], submission_token=token)
    assert adopted["status"] == "analysis_ready"
    assert adopted["task_id"] is None
    assert canvas_store.load_canvas_json(path) == payload


@pytest.mark.asyncio
async def test_agent_draft_evidence_survives_when_no_browser_is_connected(service, tmp_path, monkeypatch):
    from novelvideo.api import egress_binding
    from novelvideo.freezone import workflow_drafts
    from novelvideo.chat.store import ChatStore
    ctx = context(tmp_path)
    run, _task = await ready_run(service, ctx, monkeypatch)
    draft = {"draft_id": "workflow_draft_real", "project_id": ctx.project_id, "canvas_id": "canvas", "skill_id": "reference-video-remix",
             "revision": 1, "status": "ready", "intent": {"external_inputs": [{"node_id": "analysis", "media_kind": "reference_analysis"}]}}
    monkeypatch.setattr(workflow_drafts, "read_workflow_draft", lambda **_kwargs: (draft, None))
    @asynccontextmanager
    async def egress(**_kwargs):
        yield None
    monkeypatch.setattr(egress_binding, "request_egress_scope", egress)
    evidence = {"ok": True, "status": "workflow_draft_ready", "draft_id": "workflow_draft_real", "revision": 1}
    agent_input = []
    async def reply(*_args, **kwargs):
        agent_input.append(_args[2])
        delivered = ChatStore().list_messages(ctx.requester_username, service.scope(run), limit=20)
        directives = [message for message in delivered if message.get("message_kind") == "reference_remix_agent_directive"]
        assert len(directives) == 1
        assert directives[0]["role"] == "system"
        assert directives[0]["recipient"] == "agent"
        assert directives[0]["content"] in _args[2]
        await _args[3]({"type": "tool_updated", "name": "freezone_prepare_workflow_draft", "status": "completed", "call_id": "call", "result_json": evidence})
    monkeypatch.setattr(continuation.chat_service, "stream_assistant_reply", reply)
    await service._resume_agent(run, ctx)
    assert len(agent_input) == 1
    latest = service.store.get(run["run_id"])
    assert latest.get("workflow_draft_id") == "workflow_draft_real"
    connection = ChatStore().connect(ctx.requester_username, service.scope(latest))
    try:
        events = [json.loads(row[0]) for row in connection.execute("SELECT payload_json FROM chat_ui_events")]
    finally:
        connection.close()
    assert any(event.get("parts", [{}])[0].get("type") == "tool_status" for event in events)


@pytest.mark.asyncio
async def test_agent_cannot_claim_completion_without_a_real_draft(service, tmp_path, monkeypatch):
    from novelvideo.api import egress_binding
    ctx = context(tmp_path)
    run, _task = await ready_run(service, ctx, monkeypatch)
    @asynccontextmanager
    async def egress(**_kwargs):
        yield None
    monkeypatch.setattr(egress_binding, "request_egress_scope", egress)
    async def reply(*_args, **_kwargs):
        return {"content": "我正在准备"}
    monkeypatch.setattr(continuation.chat_service, "stream_assistant_reply", reply)
    with pytest.raises(ValueError, match="草稿"):
        await service._resume_agent(run, ctx)


def test_row_normalization_matches_existing_vision_aliases_and_sorted_frames(service):
    rows = service._story_rows({"video_story": {"shots": [{"shot": "2", "叙事": "开场", "focus_depth": "浅景深", "voice_sound": "说话", "motion_prompt": "推进", "keyframes": [1]}]},
                               "frame_urls": ["/scene_1041.png", "/scene_423.png"]})
    assert rows[0]["shotNumber"] == 2
    assert rows[0]["narrative"] == "开场"
    assert rows[0]["focalAndDof"] == "浅景深"
    assert rows[0]["voiceAndSfx"] == "说话"
    assert rows[0]["videoMotionPrompt"] == "推进"
    assert rows[0]["keyframeUrl"] == "/scene_423.png"


@pytest.mark.asyncio
async def test_submission_claim_starts_recovery_worker(service, tmp_path, monkeypatch):
    ctx = context(tmp_path)
    write_canvas(ctx)
    run = await reserve(service, ctx)
    observed = []
    monkeypatch.setattr(service, "_ensure_worker", lambda value: observed.append(value["run_id"]))
    assert await service.claim_submission(ctx=ctx, run_id=run["run_id"])
    assert observed == [run["run_id"]]


@pytest.mark.asyncio
async def test_interrupted_submission_adopts_existing_task_identity(service, tmp_path, monkeypatch):
    ctx = context(tmp_path)
    write_canvas(ctx)
    run = await reserve(service, ctx)
    task = SimpleNamespace(task_id="already-paid", project_id=ctx.project_id, requester_user_id=ctx.requester_user_id)
    monkeypatch.setattr(continuation, "get_task_manager", lambda: SimpleNamespace(get_task_for_project=lambda *_args, **_kwargs: task,
                                                                              _project_key=lambda *_args, **_kwargs: "real-key"))
    async def resolve(**_kwargs):
        return ctx
    monkeypatch.setattr(continuation, "resolve_project_context", resolve)
    assert service.store.claim(run["run_id"], service.owner_token)
    await service._process_once(service.store.get(run["run_id"]))
    current = service.store.get(run["run_id"])
    assert current["task_id"] == "already-paid"
    assert current["status"] == "analyzing"
    assert service.store.owns(run["run_id"], service.owner_token)


@pytest.mark.asyncio
async def test_interrupted_submission_without_task_stops_waiting(service, tmp_path, monkeypatch):
    ctx = context(tmp_path)
    write_canvas(ctx)
    run = await reserve(service, ctx)
    monkeypatch.setattr(continuation, "get_task_manager", lambda: SimpleNamespace(get_task_for_project=lambda *_args, **_kwargs: None))
    async def resolve(**_kwargs):
        return ctx
    monkeypatch.setattr(continuation, "resolve_project_context", resolve)
    assert service.store.claim(run["run_id"], service.owner_token)
    stored = service.store.get(run["run_id"])
    stored["created_at"] = "2020-01-01T00:00:00Z"
    assert service.store.update_owned(stored, service.owner_token)
    await service._process_once(stored)
    assert service.store.get(run["run_id"])["status"] == "analysis_failed"


@pytest.mark.asyncio
async def test_attach_keeps_paid_task_identity_after_source_changed(service, tmp_path):
    ctx = context(tmp_path)
    write_canvas(ctx)
    run = await reserve(service, ctx)
    token = await service.claim_submission(ctx=ctx, run_id=run["run_id"])
    path = canvas_store.canvas_path(ctx.state_dir, "canvas")
    payload = canvas_store.load_canvas_json(path)
    payload["nodes"][0]["data"]["videoUrl"] = "/new.mp4"
    path.write_text(json.dumps(payload), encoding="utf-8")
    attached = await service.attach_task(ctx=ctx, run_id=run["run_id"], task_id="paid-task", task_key="real-key", submission_token=token)
    assert attached["status"] == "stale"
    assert attached["task_id"] == "paid-task"
    assert attached["task_key"] == "real-key"


@pytest.mark.asyncio
async def test_attach_keeps_paid_task_when_canvas_cannot_be_loaded(service, tmp_path, monkeypatch):
    ctx = context(tmp_path)
    write_canvas(ctx)
    run = await reserve(service, ctx)
    token = await service.claim_submission(ctx=ctx, run_id=run["run_id"])
    def unavailable(*_args):
        raise OSError("canvas read failed")
    monkeypatch.setattr(service, "_canvas", unavailable)
    attached = await service.attach_task(ctx=ctx, run_id=run["run_id"], task_id="paid-task", task_key="real-key", submission_token=token)
    assert attached["status"] == "stale"
    assert attached["task_id"] == "paid-task"


@pytest.mark.asyncio
async def test_attach_persists_identity_and_analysis_fence_in_one_transition(service, tmp_path, monkeypatch):
    ctx = context(tmp_path)
    write_canvas(ctx)
    run = await reserve(service, ctx)
    token = await service.claim_submission(ctx=ctx, run_id=run["run_id"])
    transitions = []
    original = service.store.update_owned
    def observe(record, owner):
        transitions.append(dict(record))
        return original(record, owner)
    monkeypatch.setattr(service.store, "update_owned", observe)
    await service.attach_task(ctx=ctx, run_id=run["run_id"], task_id="paid-task", task_key="real-key", submission_token=token)
    assert all(value.get("analysis_fingerprint") for value in transitions if value["status"] == "analyzing")


@pytest.mark.asyncio
async def test_normal_frontend_hydration_and_autosave_do_not_cancel_analysis(service, tmp_path, monkeypatch):
    ctx = context(tmp_path)
    run, _task = await ready_run(service, ctx, monkeypatch)
    path = canvas_store.canvas_path(ctx.state_dir, "canvas")
    payload = canvas_store.load_canvas_json(path)
    payload["nodes"][1]["data"]["referenceBlueprintRef"] = None
    payload["edges"][0].update(type="disconnectableEdge", sourceHandle="source", targetHandle="target", selected=True, style={"color": "red"})
    payload["nodes"][0]["position"] = {"x": 100, "y": 150}
    path.write_text(json.dumps(payload), encoding="utf-8")
    async def resume(*_args):
        pass
    monkeypatch.setattr(service, "_resume_agent", resume)
    await service._process_once(run)
    assert service.store.get(run["run_id"])["status"] == "completed"


@pytest.mark.asyncio
async def test_concurrent_manual_chat_keeps_resume_waiting_instead_of_failing(service, tmp_path, monkeypatch):
    ctx = context(tmp_path)
    run, _task = await ready_run(service, ctx, monkeypatch)
    async def occupied(*_args):
        raise RuntimeError("当前用户已有 AI 对话正在处理中，请稍后再试。")
    monkeypatch.setattr(service, "_resume_agent", occupied)
    assert await service._process_once(run) is False
    assert service.store.get(run["run_id"])["status"] == "analysis_ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [False, True])
async def test_draft_scope_uses_canonical_server_plan_and_supports_wrapped_intent(service, tmp_path, monkeypatch, wrapped):
    from novelvideo.freezone.workflow_drafts import create_workflow_draft, public_workflow_draft
    ctx = context(tmp_path)
    run, _task = await ready_run(service, ctx, monkeypatch)
    external = [{"id": "reference_analysis", "node_id": "analysis", "media_kind": "reference_analysis"}]
    plan = {"external_inputs": external, "nodes": [], "edges": [], "skill": {"id": "reference-video-remix"}}
    intent = {"schema_version": "freezone_workflow_plan_draft.v1", "plan": plan} if wrapped else {"schema_version": "freezone_workflow_intent.v1", "external_inputs": external}
    draft = create_workflow_draft(project_dir=ctx.state_dir, project_id=ctx.project_id, canvas_id="canvas",
                                  intent=intent, compiled={"ok": True, "skill_id": "reference-video-remix", "plan": plan})
    await service._record_draft_receipt(run, ctx, "business-turn", public_workflow_draft(draft))
    assert service.store.get(run["run_id"])["workflow_draft_id"] == draft["draft_id"]


def test_nested_acp_content_preserves_actual_workflow_receipt(service):
    receipt = {"ok": True, "status": "workflow_draft_ready", "draft_id": "workflow_draft_real", "revision": 1}
    envelope = [{"type": "content", "content": {"type": "text", "text": json.dumps({"result": receipt})}}]
    assert receipt in service._json_objects(envelope)


async def delivered_draft(ctx, *, runtime_turn_id="runtime-real", actor=None):
    from novelvideo.task_state import TaskStateManager
    from novelvideo.freezone import agent_product_operations as operations
    from novelvideo.freezone.workflow_drafts import create_workflow_draft
    op_ctx = context(ctx.state_dir.parent, actor=actor or ctx.requester_user_id)
    task = TaskStateManager().create_task_for_project(op_ctx, "freezone_agent_workflow_result", 0, scope="operation", status="completed")
    operation = operations.create_agent_product_operation(project_dir=ctx.state_dir, project_id=ctx.project_id,
        product_kind="workflow_result", idempotency_key="operation", generation_session_id="session", canvas_id="canvas", artifact_id="reference-video-remix@2.0.0")
    operations.bind_agent_product_task(project_dir=ctx.state_dir, operation_id=operation["operation_id"], task_id=task.task_id, root_task_id=task.task_id)
    import time
    operations.bind_agent_product_model_execution(project_dir=ctx.state_dir, operation_id=operation["operation_id"], model_call_id="observed-model",
        executed_at=time.time(), source="server_observed_agent_turn", turn_id=runtime_turn_id, tool_call_id="tool")
    plan = {"external_inputs": [{"id": "reference_analysis", "node_id": "analysis", "media_kind": "reference_analysis"}], "nodes": [], "edges": [], "skill": {"id": "reference-video-remix"}}
    draft = create_workflow_draft(project_dir=ctx.state_dir, project_id=ctx.project_id, canvas_id="canvas", intent={"plan": plan},
        compiled={"ok": True, "skill_id": "reference-video-remix", "plan": plan}, operation_id=operation["operation_id"])
    operations.finish_agent_product_operation(project_dir=ctx.state_dir, operation_id=operation["operation_id"], outcome="delivered", expected_task_id=task.task_id,
        result_ref={"kind": "workflow_draft", "id": draft["draft_id"], "revision": 1})
    return draft


@pytest.mark.asyncio
@pytest.mark.parametrize("already_submitted", [False, True])
async def test_retry_recovers_scope_bound_delivered_draft_without_another_model_call(service, tmp_path, monkeypatch, already_submitted):
    from novelvideo.api import egress_binding
    from novelvideo.freezone.workflow_drafts import (
        bind_workflow_draft_task, claim_workflow_draft_confirmation,
        finish_workflow_draft_confirmation,
    )
    ctx = context(tmp_path)
    run, _task = await ready_run(service, ctx, monkeypatch)
    draft = await delivered_draft(ctx)
    if already_submitted:
        claimed, error = claim_workflow_draft_confirmation(
            project_dir=ctx.state_dir, canvas_id="canvas", draft_id=draft["draft_id"], revision=1,
        )
        assert error is None and claimed is not None
        bind_workflow_draft_task(project_dir=ctx.state_dir, canvas_id="canvas",
            draft_id=draft["draft_id"], task_id="confirm-task", root_task_id="confirm-task")
        finish_workflow_draft_confirmation(project_dir=ctx.state_dir, canvas_id="canvas",
            draft_id=draft["draft_id"], outcome="submitted", expected_task_id="confirm-task")
    run.update(runtime_turn_id="runtime-real", resume_turn_id="business-original")
    assert service.store.update_owned(run, service.owner_token)
    @asynccontextmanager
    async def egress(**_kwargs):
        yield None
    monkeypatch.setattr(egress_binding, "request_egress_scope", egress)
    async def forbidden(*_args, **_kwargs):
        pytest.fail("an already delivered draft must not cause another model call")
    monkeypatch.setattr(continuation.chat_service, "stream_assistant_reply", forbidden)
    await service._resume_agent(run, ctx)
    assert service.store.get(run["run_id"])["workflow_draft_id"] == draft["draft_id"]


@pytest.mark.asyncio
async def test_same_turn_canvas_submission_retains_the_prepared_draft_receipt(service, tmp_path, monkeypatch):
    from novelvideo.freezone.workflow_drafts import (
        bind_workflow_draft_task, claim_workflow_draft_confirmation,
        finish_workflow_draft_confirmation,
    )

    ctx = context(tmp_path)
    run, _task = await ready_run(service, ctx, monkeypatch)
    draft = await delivered_draft(ctx)
    claimed, error = claim_workflow_draft_confirmation(
        project_dir=ctx.state_dir, canvas_id="canvas", draft_id=draft["draft_id"],
        revision=1,
    )
    assert error is None and claimed is not None
    bind_workflow_draft_task(project_dir=ctx.state_dir, canvas_id="canvas",
        draft_id=draft["draft_id"], task_id="confirm-task", root_task_id="confirm-task")
    finish_workflow_draft_confirmation(project_dir=ctx.state_dir, canvas_id="canvas",
        draft_id=draft["draft_id"], outcome="submitted", expected_task_id="confirm-task")
    run.update(runtime_turn_id="runtime-real", resume_turn_id="business-original")
    assert service.store.update_owned(run, service.owner_token)
    receipt = service._find_existing_draft_receipt(ctx, run)
    assert receipt and receipt["draft_id"] == draft["draft_id"]
    await service._record_draft_receipt(run, ctx, "business-original", receipt)
    assert service.store.get(run["run_id"])["workflow_draft_id"] == draft["draft_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime,actor", [("another-runtime", "actor"), ("runtime-real", "another-actor")])
async def test_draft_recovery_cannot_adopt_another_turn_or_actor(service, tmp_path, monkeypatch, runtime, actor):
    ctx = context(tmp_path)
    run, _task = await ready_run(service, ctx, monkeypatch)
    await delivered_draft(ctx, runtime_turn_id=runtime, actor=actor)
    run["runtime_turn_id"] = "runtime-real"
    assert hasattr(service, "_find_existing_draft_receipt"), "recovery must read scoped persisted execution evidence"
    assert service._find_existing_draft_receipt(ctx, run) is None


@pytest.mark.asyncio
async def test_generic_router_acp_receipt_records_runtime_and_real_draft(service, tmp_path, monkeypatch):
    from novelvideo.api import egress_binding
    from novelvideo.freezone.workflow_drafts import public_workflow_draft
    ctx = context(tmp_path)
    run, _task = await ready_run(service, ctx, monkeypatch)
    draft = await delivered_draft(ctx)
    @asynccontextmanager
    async def egress(**_kwargs):
        yield None
    monkeypatch.setattr(egress_binding, "request_egress_scope", egress)
    async def reply(*args, **_kwargs):
        on_event = args[3]
        await on_event({"type": "turn_started", "turn_id": "runtime-real"})
        await on_event({"type": "tool_updated", "name": "tool_call", "status": "completed", "call_id": "call",
                        "input": {"name": "freezone_prepare_workflow_draft", "arguments": {}},
                        "output": [{"type": "content", "content": {"type": "text", "text": json.dumps(public_workflow_draft(draft))}}]})
    monkeypatch.setattr(continuation.chat_service, "stream_assistant_reply", reply)
    await service._resume_agent(run, ctx)
    recorded = service.store.get(run["run_id"])
    assert recorded["workflow_draft_id"] == draft["draft_id"]
    assert recorded["runtime_turn_id"] == "runtime-real"


@pytest.mark.asyncio
async def test_analysis_summary_can_wait_without_a_draft(service, tmp_path, monkeypatch):
    from novelvideo.api import egress_binding
    ctx = context(tmp_path)
    run, _task = await ready_run(service, ctx, monkeypatch)
    path = canvas_store.canvas_path(ctx.state_dir, 'canvas')
    payload = canvas_store.load_canvas_json(path)
    payload['nodes'][1]['data'].update(isAnalyzing=False, rawResult={'video_story': {'summary': '三秒切换镜头'}})
    path.write_text(json.dumps(payload))
    run['result_fingerprint'] = continuation._node_fingerprint(payload['nodes'][1])
    assert service.store.update_owned(run, service.owner_token)
    prompts = []
    @asynccontextmanager
    async def egress(**_kwargs):
        yield None
    monkeypatch.setattr(egress_binding, 'request_egress_scope', egress)
    async def reply(*args, **_kwargs):
        prompts.append(args[2])
        await args[3]({"type": "assistant_message", "message": {"text": "建议保留三秒切换镜头的节奏"}})
    monkeypatch.setattr(continuation.chat_service, 'stream_assistant_reply', reply)
    await service._resume_agent(run, ctx)
    assert run['status'] == 'awaiting_direction'
    assert '普通对话' in prompts[0]
    assert '普通对话' in prompts[0]
    assert '先调用 freezone_prepare_reference_decision' not in prompts[0]
    assert '直接使用实际分析节点准备' not in prompts[0]
    assert service.store.get(run['run_id'])['commission_ref']


@pytest.mark.asyncio
async def test_direction_confirmed_during_summary_turn_waits_for_next_turn(service, tmp_path, monkeypatch):
    from novelvideo.api import egress_binding
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.freezone.remix.reference_commission import advance_reference_commission, AuthenticatedDecisionEvidence
    ctx = context(tmp_path)
    run, _ = await ready_run(service, ctx, monkeypatch)
    path = canvas_store.canvas_path(ctx.state_dir, 'canvas')
    payload = canvas_store.load_canvas_json(path)
    payload['nodes'][1]['data'].update(isAnalyzing=False, rawResult={'video_story': {'summary': '访谈反转'}})
    path.write_text(json.dumps(payload))
    run['result_fingerprint'] = continuation._node_fingerprint(payload['nodes'][1])
    assert service.store.update_owned(run, service.owner_token)
    @asynccontextmanager
    async def egress(**_kwargs):
        yield None
    monkeypatch.setattr(egress_binding, 'request_egress_scope', egress)
    async def reply(*args, **_kwargs):
        store = RemixArtifactStore(ctx.output_dir, ctx.project_id, created_by=ctx.requester_user_id)
        evidence = AuthenticatedDecisionEvidence(ctx.project_id, 'canvas', ctx.requester_user_id, 'human-confirmation')
        advance_reference_commission(store, run['commission_ref'],
            {'type': 'direction_confirmed', 'direction': {'target_goal': '黑咖啡访谈短片'}},
            run['commission_ref']['revision'], host_evidence=evidence)
        await args[3]({'type': 'assistant_message', 'message': {'text': '方向已确认，接下来起草方案'}})
    monkeypatch.setattr(continuation.chat_service, 'stream_assistant_reply', reply)
    await service._resume_agent(run, ctx)
    assert run['status'] == 'awaiting_direction'
    assert run['commission_phase'] == 'awaiting_direction'
    assert run['error'] is None
    assert service.store.get(run['run_id'])['status'] == 'awaiting_direction'


@pytest.mark.asyncio
async def test_explicit_direction_remains_conversation_until_native_submission(service, tmp_path, monkeypatch):
    from novelvideo.api import egress_binding
    from novelvideo.freezone.remix.reference_director import list_director_artifacts
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.chat.store import ChatStore
    ctx = context(tmp_path)
    run, _task = await ready_run(service, ctx, monkeypatch)
    path = canvas_store.canvas_path(ctx.state_dir, 'canvas')
    payload = canvas_store.load_canvas_json(path)
    payload['nodes'][1]['data'].update(isAnalyzing=False, rawResult={'video_story': {'summary': '三秒切换镜头'}})
    path.write_text(json.dumps(payload))
    run['result_fingerprint'] = continuation._node_fingerprint(payload['nodes'][1])
    run['goal'] = '把产品换成黑咖啡，其他不变'
    run['inputs'] = {'remix_direction': run['goal']}
    assert service.store.update_owned(run, service.owner_token)
    @asynccontextmanager
    async def egress(**_kwargs):
        yield None
    monkeypatch.setattr(egress_binding, 'request_egress_scope', egress)
    async def reply(*args, **_kwargs):
        store = RemixArtifactStore(ctx.output_dir, ctx.project_id, created_by=ctx.requester_user_id)
        assert len(list_director_artifacts(store, 'canvas', ctx.requester_user_id)['decisions']) == 0
        assert '当前已保存的内部方向提议' not in args[2]
        await args[3]({'type': 'assistant_message', 'message': {'text': '请确认复刻方向'}})
    monkeypatch.setattr(continuation.chat_service, 'stream_assistant_reply', reply)
    await service._resume_agent(run, ctx)
    messages = ChatStore().list_messages(ctx.requester_username, service.scope(run), limit=20)
    assert not any(message.get('turn_id', '').startswith('reference-card-') for message in messages)


def test_waiting_run_wakes_once_without_replacing_paid_task(service, tmp_path):
    ctx = context(tmp_path)
    record = {'run_id':'waiting', 'project_id':ctx.project_id, 'requester_user_id':ctx.requester_user_id,
              'canvas_id':'canvas','agent_id':'main','idempotency_key':'wait', 'request_fingerprint':'fp',
              'status':'awaiting_direction','task_id':'paid', 'commission_ref':{'artifact_id':'commission','revision':1}}
    service.store.create(record)
    assert service.store.wake_waiting(ctx, 'waiting', {'artifact_id':'commission','revision':2}, 'drafting_plan')['task_id'] == 'paid'
    assert service.store.wake_waiting(ctx, 'waiting', {'artifact_id':'commission','revision':2}, 'drafting_plan') is None


def test_saved_production_event_can_recover_failed_plan_continuation(service, tmp_path):
    ctx = context(tmp_path)
    record = {'run_id': 'failed', 'project_id': ctx.project_id, 'requester_user_id': ctx.requester_user_id,
              'canvas_id': 'canvas', 'agent_id': 'main', 'idempotency_key': 'failed', 'request_fingerprint': 'fp',
              'status': 'resume_failed', 'commission_phase': 'drafting_plan', 'commission_ref': {'artifact_id': 'commission', 'revision': 2}}
    service.store.create(record)
    assert service.store.wake_waiting(ctx, 'failed', {'artifact_id': 'commission', 'revision': 3}, 'awaiting_plan_review') is None
    woke = service.store.wake_waiting(ctx, 'failed', {'artifact_id': 'commission', 'revision': 3}, 'awaiting_plan_review', event_key='saved:plan-ready')
    assert woke is not None
    assert woke['status'] == 'analysis_ready'
    assert woke['error'] is None
    assert service.store.wake_waiting(ctx, 'failed', {'artifact_id': 'commission', 'revision': 3}, 'awaiting_plan_review', event_key='saved:plan-ready') is None


def test_failed_native_plan_fact_is_consumed_without_retrying_the_model(service, tmp_path):
    ctx = context(tmp_path)
    ref = {'artifact_id': 'commission', 'revision': 2}
    service.store.create({'run_id': 'failed-native', 'project_id': ctx.project_id,
        'requester_user_id': ctx.requester_user_id, 'canvas_id': 'canvas', 'agent_id': 'main',
        'idempotency_key': 'failed-native', 'status': 'resume_failed',
        'commission_phase': 'drafting_plan', 'commission_ref': ref})
    saved = service.store.wake_waiting(ctx, 'failed-native', ref, 'drafting_plan',
                                       event_key='native-plan:failed')
    assert saved['status'] == 'resume_failed'
    assert saved['commission_event_receipts'] == ['native-plan:failed']
    assert service.store.wake_waiting(ctx, 'failed-native', ref, 'drafting_plan',
                                      event_key='native-plan:failed') is None


def test_failed_plan_receipt_needs_a_distinct_saved_plan_event_to_resume(service, tmp_path):
    ctx = context(tmp_path)
    direction_ref = {'artifact_id': 'commission', 'revision': 2}
    service.store.create({'run_id': 'plan-failure-then-save', 'project_id': ctx.project_id,
        'requester_user_id': ctx.requester_user_id, 'canvas_id': 'canvas', 'agent_id': 'main',
        'idempotency_key': 'plan-failure-then-save', 'status': 'resume_failed',
        'commission_phase': 'drafting_plan', 'commission_ref': direction_ref})

    failed = service.store.wake_waiting(ctx, 'plan-failure-then-save', direction_ref,
                                        'drafting_plan', event_key='native-plan:failed')
    assert failed['status'] == 'resume_failed'
    assert failed['commission_event_receipts'] == ['native-plan:failed']
    assert service.store.wake_waiting(ctx, 'plan-failure-then-save', direction_ref,
                                      'drafting_plan', event_key='native-plan:failed') is None
    assert service.store.get('plan-failure-then-save')['status'] == 'resume_failed'

    saved_ref = {'artifact_id': 'commission', 'revision': 3}
    saved = service.store.wake_waiting(ctx, 'plan-failure-then-save', saved_ref,
                                       'awaiting_plan_review', event_key='saved:plan-ready')
    assert saved['status'] == 'analysis_ready'
    assert saved['commission_ref'] == saved_ref
    assert saved['commission_event_receipts'] == ['native-plan:failed', 'saved:plan-ready']
    assert service.store.wake_waiting(ctx, 'plan-failure-then-save', saved_ref,
                                      'awaiting_plan_review', event_key='saved:plan-ready') is None


@pytest.mark.asyncio
async def test_shared_task_binding_preserves_operation_scope_before_paid_admission(service, tmp_path):
    ctx = context(tmp_path)
    write_canvas(ctx)
    run = await reserve(service, ctx)
    token = await service.claim_submission(ctx=ctx, run_id=run['run_id'])
    await service.bind_analysis_operation(ctx=ctx, run_id=run['run_id'], operation_id='a' * 32, submission_token=token)
    stored = service.store.get(run['run_id'])
    assert stored['task_type'] == 'remix_joint_reference_analysis'
    assert stored['task_scope'] == 'a' * 32
    assert stored['operation_id'] == 'a' * 32
    assert stored['job_id'] == run['job_id']
    attached = await service.attach_task(ctx=ctx, run_id=run['run_id'], task_id='shared-paid-task', submission_token=token)
    assert attached['task_type'] == 'remix_joint_reference_analysis'
    assert service.store.get(run['run_id'])['task_scope'] == 'a' * 32


def test_joint_completion_saves_analysis_refs_for_manual_and_agent_views(service):
    result = {'video_story': {'summary': 'Structure'}, 'analysis_ref': {'artifact_id': 'receipt', 'revision': 1},
      'evidence_ref': {'artifact_id': 'evidence', 'revision': 1}, 'transcript_ref': None,
      'blueprint_ref': {'artifact_id': 'blueprint', 'revision': 1}, 'transcript_status': 'failed',
      'limitations': ['ASR unavailable'], 'coverage': {'visual_inspection': 'sampled_frames'}}
    data = service._analysis_data({'data': {}}, {'task_id': 'task', 'run_id': 'run'}, result=result)
    assert data['referenceAnalysisRef'] == {'artifactId': 'receipt', 'revision': 1}
    assert data['referenceEvidenceRef'] == {'artifactId': 'evidence', 'revision': 1}
    assert data['referenceTranscriptStatus'] == 'failed'
    assert data['referenceAnalysisLimitations'] == ['ASR unavailable']


@pytest.mark.asyncio
async def test_joint_completed_task_requires_shared_source_receipt_before_canvas_copy(service, tmp_path, monkeypatch):
    from novelvideo.freezone.remix import reference_analysis_service as analysis
    ctx = context(tmp_path)
    run, task = await ready_run(service, ctx, monkeypatch)
    run.update(task_type='remix_joint_reference_analysis', task_scope='a' * 32, operation_id='a' * 32)
    task.result['analysis_ref'] = {'artifact_id': 'receipt', 'revision': 1}
    assert service.store.update_owned(run, service.owner_token)
    async def unavailable(*args):
        return {'status': 'pending', 'analysis_ref': None}
    monkeypatch.setattr(analysis, 'get_reference_analysis', unavailable)
    await service._process_once(run)
    saved = canvas_store.load_canvas_json(canvas_store.canvas_path(ctx.state_dir, 'canvas'))
    assert saved['nodes'][1]['data'].get('rawResult') is None
    assert service.store.get(run['run_id'])['status'] == 'stale'


def test_same_phase_event_wakeup_is_durable_and_clears_old_output(service, tmp_path):
    ctx = context(tmp_path)
    row = {'run_id':'events', 'project_id':ctx.project_id, 'requester_user_id':ctx.requester_user_id,
           'canvas_id':'canvas','agent_id':'main','idempotency_key':'events','request_fingerprint':'fp',
           'status':'preparing_materials', 'commission_phase':'preparing_materials',
           'commission_ref':{'artifact_id':'commission','revision':2},
           'workflow_draft_id':'old', 'workflow_draft_revision':1,
           'workflow_draft_receipt':{'draft_id':'old'}, 'assistant_message':'old response'}
    service.store.create(row)
    woke = service.store.wake_waiting(ctx, 'events', row['commission_ref'], 'preparing_materials', event_key='task:one:completed')
    assert woke['status'] == 'analysis_ready'
    assert not woke.get('workflow_draft_receipt')
    assert not woke.get('workflow_draft_id')
    assert woke['draft_receipt_history'][0]['workflow_draft_id'] == 'old'
    woke['status'] = 'preparing_materials'
    assert service.store.claim('events', service.owner_token)
    assert service.store.update_owned(woke, service.owner_token)
    service.store.release('events', service.owner_token)
    assert service.store.wake_waiting(ctx, 'events', row['commission_ref'], 'preparing_materials', event_key='task:one:completed') is None


def test_material_work_failure_event_does_not_resume_agent(service, tmp_path):
    ctx = context(tmp_path)
    row = {'run_id': 'material-failed', 'project_id': ctx.project_id,
           'requester_user_id': ctx.requester_user_id, 'canvas_id': 'canvas',
           'agent_id': 'main', 'idempotency_key': 'material-failed',
           'request_fingerprint': 'fp', 'status': 'resume_failed',
           'commission_phase': 'preparing_materials',
           'commission_ref': {'artifact_id': 'commission', 'revision': 2},
           'manual_resume_required': True,
           'manual_resume_reason': {
               'code': 'material_work_request_invalid',
               'phase': 'material_plan_drafting',
               'errors': [{'path': 'steps[0].source', 'message': '来源无效'}],
           }}
    service.store.create(row)

    saved = service.store.wake_waiting(
        ctx, row['run_id'], row['commission_ref'], 'preparing_materials',
        event_key='material:failed',
    )

    assert saved['status'] == 'resume_failed'
    assert saved['manual_resume_required'] is True
    assert saved['resume_attempt'] == 0
    assert saved['commission_event_receipts'] == ['material:failed']
    assert service.store.wake_waiting(
        ctx, row['run_id'], row['commission_ref'], 'preparing_materials',
        event_key='material:failed',
    ) is None


def test_explicit_retry_clears_material_work_failure_gate(service, tmp_path):
    ctx = context(tmp_path)
    row = {'run_id': 'material-retry', 'project_id': ctx.project_id,
           'requester_user_id': ctx.requester_user_id, 'canvas_id': 'canvas',
           'agent_id': 'main', 'idempotency_key': 'material-retry',
           'request_fingerprint': 'fp', 'status': 'resume_failed',
           'commission_phase': 'preparing_materials',
           'commission_ref': {'artifact_id': 'commission', 'revision': 2},
           'manual_resume_required': True,
           'manual_resume_reason': {'code': 'material_work_request_invalid',
                                    'phase': 'material_plan_drafting'}}
    service.store.create(row)

    retried = service.store.retry(ctx, row['run_id'])

    assert retried['status'] == 'analysis_ready'
    assert retried['resume_attempt'] == 1
    assert retried.get('manual_resume_required') is not True
    assert 'manual_resume_reason' not in retried


@pytest.mark.asyncio
@pytest.mark.parametrize('native_started', [False, True])
async def test_queued_analysis_event_waits_when_plan_stage_advanced_before_dispatch(service, tmp_path, monkeypatch, native_started):
    from novelvideo.freezone.remix.reference_commission import (
        AuthenticatedDecisionEvidence, create_reference_commission, advance_reference_commission,
    )
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.chat import reference_production_observer as observer

    ctx = context(tmp_path)
    write_canvas(ctx)
    artifacts = RemixArtifactStore(ctx.output_dir, ctx.project_id, created_by=ctx.requester_user_id)
    commission = create_reference_commission(
        artifacts, canvas_id='canvas', requester_user_id=ctx.requester_user_id,
        analysis_node_id='analysis', goal='produce',
        analysis_evidence={'task_id': 'analysis-task', 'result_fingerprint': 'analysis-hash'},
    )
    run = {'run_id': 'queued-analysis', 'project_id': ctx.project_id,
           'requester_user_id': ctx.requester_user_id, 'username': ctx.requester_username,
           'canvas_id': 'canvas', 'agent_id': 'main', 'analysis_node_id': 'analysis',
           'idempotency_key': 'queued-analysis', 'request_fingerprint': 'queued-analysis',
           'status': 'awaiting_direction', 'commission_phase': 'awaiting_direction',
           'commission_ref': commission['commission_ref'], 'task_id': 'analysis-task',
           'context_json': continuation._serialize_context(ctx)}
    service.store.create(run)
    queued = service.store.wake_waiting(ctx, run['run_id'], commission['commission_ref'],
                                        'awaiting_direction', event_key='analysis:completed')
    assert queued['status'] == 'analysis_ready'

    # Direction confirmation and potentially its native action happen after
    # the old event is queued but before its worker runs.
    commission = advance_reference_commission(
        artifacts, commission['commission_ref'],
        {'type': 'direction_confirmed', 'direction': {'goal': 'produce'}, 'message_id': 'human'},
        commission['commission_ref']['revision'],
        host_evidence=AuthenticatedDecisionEvidence(ctx.project_id, 'canvas', ctx.requester_user_id, 'human'),
    )
    monkeypatch.setattr(observer, '_plan_generation_awaiting_canvas_result', lambda *_args: native_started)
    async def resolve(_run): return ctx
    monkeypatch.setattr(service, '_resolve_context', resolve)
    monkeypatch.setattr(service, '_assert_current', lambda *_args, **_kwargs: {'nodes': []})
    calls = []
    async def resume(*_args): calls.append('model')
    monkeypatch.setattr(service, '_resume_agent', resume)
    assert service.store.claim(run['run_id'], service.owner_token)
    await service._process_once(service.store.get(run['run_id']))
    saved = service.store.get(run['run_id'])
    assert calls == []
    assert saved['status'] == 'drafting_plan'
    assert saved['commission_ref'] == commission['commission_ref']
    assert saved['task_id'] == 'analysis-task'
    if native_started:
        service.store.release(run['run_id'], service.owner_token)
        plan = artifacts.create('production_plan.v2', {
            'schema_version': 2, 'goal': 'produce',
            'output': {'width': 1920, 'height': 1080, 'fps_num': 24, 'fps_den': 1},
            'beats': [{'id': 'opening', 'purpose': 'intro', 'duration_frames': 48}],
        }, [])
        commission = advance_reference_commission(
            artifacts, commission['commission_ref'],
            {'type': 'plan_ready', 'plan_ref': {'artifact_id': plan['id'], 'revision': 1}},
            commission['commission_ref']['revision'],
        )
        completion_key = 'plan:' + plan['id'] + ':1'
        assert service.store.wake_waiting(ctx, run['run_id'], commission['commission_ref'],
                                          'awaiting_plan_review', event_key=completion_key)
        assert service.store.wake_waiting(ctx, run['run_id'], commission['commission_ref'],
                                          'awaiting_plan_review', event_key=completion_key) is None
        assert service.store.claim(run['run_id'], service.owner_token)
        await service._process_once(service.store.get(run['run_id']))
        assert calls == ['model']
        assert service.store.get(run['run_id'])['commission_phase'] == 'awaiting_plan_review'


@pytest.mark.asyncio
async def test_old_draft_receipt_does_not_skip_material_stage(service, tmp_path, monkeypatch):
    from novelvideo.api import egress_binding
    from novelvideo.freezone.remix import reference_commission
    ctx = context(tmp_path)
    run, _task = await ready_run(service, ctx, monkeypatch)
    run.update(result_fingerprint='saved-analysis', commission_ref={'artifact_id':'commission','revision':5},
               commission_phase='preparing_materials', workflow_draft_receipt={'draft_id':'old'},
               workflow_draft_id='old', runtime_turn_id='old-turn')
    assert service.store.update_owned(run, service.owner_token)
    monkeypatch.setattr(reference_commission, 'read_reference_commission', lambda *_args, **_kwargs: {
        'phase':'preparing_materials','commission_ref':run['commission_ref']})
    @asynccontextmanager
    async def egress(**_kwargs):
        yield None
    monkeypatch.setattr(egress_binding, 'request_egress_scope', egress)
    prompts = []
    async def reply(*args, **_kwargs):
        prompts.append(args[2])
    monkeypatch.setattr(continuation.chat_service, 'stream_assistant_reply', reply)
    await service._resume_agent(run, ctx)
    assert len(prompts) == 1
    assert 'preparing_materials' in prompts[0]
    assert run['status'] == 'preparing_materials'


def test_expired_commission_receipt_never_marks_output_completed(service, tmp_path):
    ctx = context(tmp_path)
    row = {'run_id':'expired', 'project_id':ctx.project_id, 'requester_user_id':ctx.requester_user_id,
           'canvas_id':'canvas','agent_id':'main','idempotency_key':'expired','request_fingerprint':'fp',
           'status':'resuming','commission_phase':'drafting_plan','commission_ref':{'artifact_id':'c','revision':1},
           'workflow_draft_id':'old','workflow_draft_receipt':{'draft_id':'old'}}
    service.store.create(row)
    assert service.store.recover_expired_resume() == 1
    assert service.store.get('expired')['status'] == 'resume_failed'


@pytest.mark.asyncio
async def test_background_prompt_includes_current_saved_artifact_refs(service, tmp_path, monkeypatch):
    from novelvideo.api import egress_binding
    from novelvideo.freezone.canvas_context_reader import canvas_read_mode, saved_turn_binding
    ctx=context(tmp_path)
    run,_=await ready_run(service,ctx,monkeypatch)
    run['result_fingerprint']='saved-analysis'
    assert service.store.update_owned(run,service.owner_token)
    path=canvas_store.canvas_path(ctx.state_dir,'canvas')
    payload=canvas_store.load_canvas_json(path)
    payload['nodes'][1]['data'].update(rawResult={'video_story': {'summary': '实际摘要'}}, referenceAnalysisRef={'artifact_id':'analysis_real','revision':2},referenceBlueprintRef={'artifact_id':'blueprint_real','revision':3})
    path.write_text(json.dumps(payload))
    run['result_fingerprint'] = continuation._node_fingerprint(payload['nodes'][1])
    assert service.store.update_owned(run, service.owner_token)
    observed=[]
    @asynccontextmanager
    async def egress(**kwargs): yield None
    monkeypatch.setattr(egress_binding,'request_egress_scope',egress)
    async def reply(*args,**kwargs):
        observed.append(args[2])
        await args[3]({'type': 'assistant_message', 'message': {'text': '实际摘要与复刻建议'}})
        assert canvas_read_mode()=='saved'
        assert saved_turn_binding(ctx.state_dir,kwargs['turn_id'])['canvas_id']=='canvas'
    monkeypatch.setattr(continuation.chat_service,'stream_assistant_reply',reply)
    await service._resume_agent(run,ctx)
    assert 'saved_artifact_refs' in observed[0]
    assert 'analysis_real' in observed[0]
    assert 'blueprint_real' in observed[0]

@pytest.mark.asyncio
async def test_normal_model_finish_cannot_claim_summary_without_assistant_delivery(service, tmp_path, monkeypatch):
    from novelvideo.api import egress_binding
    from novelvideo.chat.canvas_tool_session import CanvasToolSession
    ctx = context(tmp_path)
    run, _ = await ready_run(service, ctx, monkeypatch)
    run['result_fingerprint'] = continuation._node_fingerprint(service._node(service._canvas(ctx, 'canvas'), 'analysis'))
    assert service.store.update_owned(run, service.owner_token)
    @asynccontextmanager
    async def egress(**kwargs):
        yield None
    monkeypatch.setattr(egress_binding, 'request_egress_scope', egress)
    captured = []
    original_init = CanvasToolSession.__init__
    def capture_lease(self, *args, **kwargs):
        captured.append(kwargs.get('lease_valid'))
        original_init(self, *args, **kwargs)
    monkeypatch.setattr(CanvasToolSession, '__init__', capture_lease)
    async def reply(*args, **kwargs):
        await args[3]({'type': 'assistant_delta', 'text': '分析工具调用失败'})
    monkeypatch.setattr(continuation.chat_service, 'stream_assistant_reply', reply)
    await service._resume_agent(run, ctx)
    assert callable(captured[0])
    assert captured[0]() is True
    assert run['status'] == 'resume_failed'
    assert run['commission_phase'] == 'awaiting_direction'
    assert run['execution_blocker']['code'] == 'context_delivery_failed'
    assert '摘要已准备' not in run['message']

@pytest.mark.asyncio
async def test_background_clarification_is_real_wait_not_failed_resume(service, tmp_path, monkeypatch):
    from novelvideo.api import egress_binding
    from novelvideo.chat.canvas_tool_session import CanvasToolSession
    from novelvideo.freezone.canvas_command_bridge import put_pending_clarification_event
    ctx = context(tmp_path)
    run, _ = await ready_run(service, ctx, monkeypatch)
    run['result_fingerprint'] = continuation._node_fingerprint(service._node(service._canvas(ctx, 'canvas'), 'analysis'))
    assert service.store.update_owned(run, service.owner_token)
    @asynccontextmanager
    async def egress(**kwargs): yield None
    monkeypatch.setattr(egress_binding, 'request_egress_scope', egress)
    sent = []
    async def send(self, frame):
        sent.append(frame)
        self.delivery_status = 'sent'
        return True
    monkeypatch.setattr(CanvasToolSession, '_send', send)
    from novelvideo.chat.live_events import register_chat_websocket, unregister_chat_websocket
    class Socket:
        async def send_json(self, payload): pass
    socket = Socket()
    await register_chat_websocket(socket, username=ctx.requester_username, scope=service.scope(run))
    async def reply(*args, **kwargs):
        # Simulate the real hosted subprocess identity, not an unbound legacy producer.
        for name,value in {"USERNAME":ctx.requester_username,"PROJECT_ID":ctx.project_id,"CANVAS_ID":"canvas","AGENT_ID":"main","TURN_ID":kwargs["turn_id"]}.items():
            monkeypatch.setenv("DRAMACLAW_"+name,value)
        from novelvideo.api.routes.chat import _canvas_bridge_dir
        directory = _canvas_bridge_dir(ctx.requester_username, project_state_dir=ctx.state_dir, profile='freezone:main')
        put_pending_clarification_event(key='real-card', project_id=ctx.project_id, canvas_id='canvas', event={'type': 'assistant.clarification', 'clarification_id': 'question-1', 'questions': [{'id': 'choice', 'title': '需要补充的实际选择'}]}, bridge_dir=directory)
        await asyncio.sleep(.6)
    monkeypatch.setattr(continuation.chat_service, 'stream_assistant_reply', reply)
    try:
        await service._resume_agent(run, ctx)
    finally:
        await unregister_chat_websocket(socket)
    assert sent
    assert run['status'] == 'awaiting_direction'
    assert run['execution_blocker']['code'] == 'user_required'
    assert run['pending_interaction']['clarification_id'] == 'question-1'
    from novelvideo.api.routes import chat as chat_route
    monkeypatch.setattr(continuation, 'coordinator', service)
    service.store.release(run['run_id'], service.owner_token)
    frame = next(frame for frame in sent if frame.get('type') == 'assistant.clarification.event')
    result = chat_route._resolve_clarification_tool_result_payload(chat_route.ClarificationToolResultIn(
        bridge_key=frame['bridge_key'], turn_id=run['resume_turn_id'], project_id=ctx.project_id,
        canvas_id='canvas', agent_id='main', answers={'choice': '真实回答'}), username=ctx.requester_username, project_state_dir=ctx.state_dir)
    assert result['ok'] is True
    await asyncio.sleep(.4)
    awakened = service.store.get(run['run_id'])
    assert awakened['status'] == 'analysis_ready'
    assert awakened['task_id'] == run['task_id']
    assert awakened['user_interaction_receipt']['clarification_id'] == 'question-1'



def test_user_interaction_wake_keeps_task_and_rejects_other_card(service, tmp_path):
    ctx = context(tmp_path)
    record = {'run_id': 'question', 'project_id': ctx.project_id, 'requester_user_id': ctx.requester_user_id,
              'username': ctx.requester_username, 'canvas_id': 'canvas', 'agent_id': 'main', 'idempotency_key': 'question',
              'status': 'awaiting_direction', 'commission_phase': 'awaiting_direction', 'task_id': 'paid',
              'pending_interaction': {'bridge_key': 'card', 'clarification_id': 'q', 'turn_id': 'turn', 'canvas_revision': 1}}
    service.store.create(record)
    receipt = {'ok': True, 'status': 'clarification_frontend_result', 'answers': {'choice': 'actual-user-answer'}}
    assert service.store.wake_interaction(ctx, 'question', {**record['pending_interaction'], 'bridge_key': 'other'}, receipt) is None
    resumed = service.store.wake_interaction(ctx, 'question', record['pending_interaction'], receipt)
    assert resumed['status'] == 'analysis_ready'
    assert resumed['task_id'] == 'paid'
    assert resumed['user_interaction_receipt']['clarification_id'] == 'q'
    assert service.store.wake_interaction(ctx, 'question', record['pending_interaction'], receipt) is None


def test_verified_parameter_cards_accumulate_across_interaction_wakes(service, tmp_path):
    ctx = context(tmp_path)
    scope = {'project_id': ctx.project_id, 'canvas_id': 'canvas', 'agent_id': 'main',
             'requester_user_id': ctx.requester_user_id, 'commission_id': 'commission'}
    def card(key, field):
        return {'bridge_key': key, 'clarification_id': key, 'turn_id': key,
                'scope': scope, 'commission_ref': {'artifact_id': 'commission', 'revision': 1},
                'generation_requirements_digest': 'current-requirements',
                'event': {'questions': [{'id': field, 'title': field}]}}
    first = card('model-card', 'image_model')
    service.store.create({'run_id': 'cards', 'project_id': ctx.project_id,
        'requester_user_id': ctx.requester_user_id, 'canvas_id': 'canvas', 'agent_id': 'main',
        'idempotency_key': 'cards', 'status': 'awaiting_direction',
        'commission_ref': {'artifact_id': 'commission', 'revision': 1},
        'commission_phase': 'awaiting_direction',
        'generation_requirements_digest': 'current-requirements', 'pending_interaction': first})
    first_result = {'ok': True, 'status': 'clarification_frontend_result',
                    'answers': {'image_model': 'selected-model'}}
    assert service.store.wake_interaction(ctx, 'cards', first, first_result)
    after_first = service.store.get('cards')
    assert len(after_first.get('generation_parameter_receipts', [])) == 1
    assert after_first['generation_parameter_receipts'][0]['result'] == first_result

    second = card('duration-card', 'video_duration_seconds')
    assert service.store.claim('cards', service.owner_token)
    after_first.update(status='awaiting_direction', pending_interaction=second)
    assert service.store.update_owned(after_first, service.owner_token)
    service.store.release('cards', service.owner_token)
    second_result = {'ok': True, 'status': 'clarification_frontend_result',
                     'answers': {'video_duration_seconds': 10}}
    assert service.store.wake_interaction(ctx, 'cards', second, second_result)
    saved = service.store.get('cards')
    assert [r['bridge_key'] for r in saved['generation_parameter_receipts']] == ['model-card', 'duration-card']
    assert service.store.wake_interaction(ctx, 'cards', second, second_result) is None


def test_material_parameter_card_is_persisted_for_later_turns(service, tmp_path):
    from novelvideo.freezone.workflow_drafts import read_reference_parameter_submissions

    ctx = context(tmp_path)
    scope = {'project_id': ctx.project_id, 'canvas_id': 'canvas', 'agent_id': 'main',
             'requester_user_id': ctx.requester_user_id, 'commission_id': 'commission'}
    interaction = {'bridge_key': 'material-card', 'clarification_id': 'material-question',
                   'turn_id': 'turn', 'scope': scope,
                   'commission_ref': {'artifact_id': 'commission', 'revision': 1},
                   'generation_requirements_digest': 'material-requirements',
                   'event': {'questions': [{'id': 'video_model', 'title': 'video model'}]}}
    service.store.create({'run_id': 'material-card-run', 'project_id': ctx.project_id,
        'requester_user_id': ctx.requester_user_id, 'canvas_id': 'canvas', 'agent_id': 'main',
        'idempotency_key': 'material-card-run', 'status': 'preparing_materials',
        'commission_ref': {'artifact_id': 'commission', 'revision': 1},
        'commission_phase': 'preparing_materials',
        'generation_requirements_digest': 'material-requirements',
        'pending_interaction': interaction})
    result = {'ok': True, 'status': 'clarification_frontend_result',
              'answers': {'video_model': 'seedance-2.0-mini'}}

    assert service.store.wake_interaction(ctx, 'material-card-run', interaction, result)

    saved = read_reference_parameter_submissions(
        project_dir=ctx.state_dir, scope=scope,
        requirements_digest='material-requirements',
    )
    assert [row['bridge_key'] for row in saved] == ['material-card']
    assert saved[0]['result']['answers'] == result['answers']


def test_material_parameter_card_with_wrong_scope_keeps_pending(service, tmp_path):
    ctx = context(tmp_path)
    interaction = {
        'bridge_key': 'wrong-scope-card', 'clarification_id': 'question',
        'scope': {'project_id': 'other', 'canvas_id': 'canvas', 'agent_id': 'main',
                  'requester_user_id': ctx.requester_user_id, 'commission_id': 'commission'},
        'generation_requirements_digest': 'requirements',
        'event': {'questions': [{'id': 'video_model'}]},
    }
    service.store.create({
        'run_id': 'wrong-scope-run', 'project_id': ctx.project_id,
        'requester_user_id': ctx.requester_user_id, 'username': ctx.requester_username,
        'canvas_id': 'canvas', 'agent_id': 'main', 'idempotency_key': 'wrong-scope-run',
        'status': 'preparing_materials', 'commission_phase': 'preparing_materials',
        'commission_ref': {'artifact_id': 'commission', 'revision': 1},
        'generation_requirements_digest': 'requirements', 'pending_interaction': interaction,
    })
    assert service.store.wake_interaction(
        ctx, 'wrong-scope-run', interaction,
        {'ok': True, 'status': 'clarification_frontend_result',
         'answers': {'video_model': 'seedance-2.0-mini'}},
    ) is None
    assert service.store.get('wrong-scope-run')['pending_interaction'] == interaction


def test_late_clarification_answer_does_not_reopen_an_older_stage(service, tmp_path):
    ctx = context(tmp_path)
    interaction = {'bridge_key': 'direction-card', 'clarification_id': 'direction',
                   'commission_phase': 'awaiting_direction', 'turn_id': 'old-turn'}
    service.store.create({'run_id': 'late-answer', 'project_id': ctx.project_id,
        'requester_user_id': ctx.requester_user_id, 'canvas_id': 'canvas', 'agent_id': 'main',
        'idempotency_key': 'late-answer', 'status': 'drafting_plan',
        'commission_phase': 'drafting_plan', 'task_id': 'native-plan',
        'pending_interaction': interaction})
    result = {'ok': True, 'status': 'clarification_frontend_result',
              'answers': {'decision': 'adjust later'}}
    saved = service.store.wake_interaction(ctx, 'late-answer', interaction, result)
    assert saved['status'] == 'drafting_plan'
    assert saved['task_id'] == 'native-plan'
    assert saved['user_interaction_receipt']['result'] == result


@pytest.mark.parametrize(('phase', 'reference_field', 'directive'), [
    ('awaiting_direction', None, '原片分析'),
    ('awaiting_plan_review', 'plan_ref', '作品方案'),
    ('preparing_materials', 'work_ref', '素材制作'),
    ('reviewing_output', 'manifest_ref', '实际输出'),
])
def test_stage_instruction_names_current_saved_result(phase, reference_field, directive):
    commission = {'phase': phase, 'analysis_node_id': 'analysis',
                  'plan_ref': {'artifact_id': 'plan', 'revision': 2},
                  'work_ref': {'artifact_id': 'work', 'revision': 3},
                  'manifest_ref': {'artifact_id': 'manifest', 'revision': 4}}
    instruction = continuation._production_stage_instruction(commission)
    assert directive in instruction
    if reference_field:
        assert reference_field in instruction
        assert commission[reference_field]['artifact_id'] in instruction


@pytest.mark.asyncio
async def test_failed_plan_wake_sends_failure_directive_to_agent(service, tmp_path, monkeypatch):
    from novelvideo.api import egress_binding
    from novelvideo.chat import reference_production_observer as observer_module
    from novelvideo.chat.reference_production_observer import production_event
    from novelvideo.chat.store import ChatStore
    from novelvideo.freezone.remix.reference_commission import (
        AuthenticatedDecisionEvidence, advance_reference_commission,
        create_reference_commission,
    )
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.freezone.workflow_runs import create_workflow_run, update_workflow_run

    ctx = context(tmp_path)
    run, _ = await ready_run(service, ctx, monkeypatch)
    path = canvas_store.canvas_path(ctx.state_dir, 'canvas')
    canvas = canvas_store.load_canvas_json(path)
    canvas['nodes'][1]['data'].update(isAnalyzing=False, rawResult={'video_story': {'summary': '原片摘要'}})
    canvas['nodes'].append({'id': 'plan', 'type': 'scriptNode',
                            'data': {'remixRole': 'production_plan'}})
    canvas['edges'].append({'source': 'analysis', 'target': 'plan'})
    path.write_text(json.dumps(canvas), encoding='utf-8')
    run['result_fingerprint'] = continuation._node_fingerprint(canvas['nodes'][1])
    store = RemixArtifactStore(ctx.output_dir, ctx.project_id, created_by=ctx.requester_user_id)
    commission = create_reference_commission(
        store, canvas_id='canvas', requester_user_id=ctx.requester_user_id,
        analysis_node_id='analysis', goal=run['goal'],
        analysis_evidence={'task_id': run['task_id'], 'result_fingerprint': run['result_fingerprint']},
    )
    commission = advance_reference_commission(
        store, commission['commission_ref'],
        {'type': 'direction_confirmed', 'direction': {'target_goal': '黑咖啡访谈'}},
        commission['commission_ref']['revision'],
        host_evidence=AuthenticatedDecisionEvidence(ctx.project_id, 'canvas', ctx.requester_user_id, 'user-message'),
    )
    workflow = create_workflow_run(
        project_dir=ctx.state_dir, project_id=ctx.project_id, canvas_id='canvas',
        actor_id=ctx.requester_user_id,
        actions=[{'node_id': 'plan', 'action': 'generate_production_plan_draft',
                  'payload': {'topic': 'coffee'}, 'node_revision': 1}],
    )
    update_workflow_run(
        project_dir=ctx.state_dir, canvas_id='canvas', run_id=workflow['run_id'],
        status='failed', action_updates=[{'node_id': 'plan',
            'action': 'generate_production_plan_draft', 'status': 'failed', 'error': 'Request timed out.'}],
    )
    # This test covers directive delivery; draft-to-node binding is tested in
    # the observer contract tests.
    monkeypatch.setattr(observer_module, '_bound_plan_node', lambda *_args: True)
    event = production_event(store, canvas, commission, workflow_state_dir=ctx.state_dir)
    run.update(commission_ref=commission['commission_ref'], commission_phase='drafting_plan',
               commission_event_receipts=[event['event_key']])
    assert service.store.update_owned(run, service.owner_token)

    @asynccontextmanager
    async def egress(**_kwargs):
        yield None
    monkeypatch.setattr(egress_binding, 'request_egress_scope', egress)
    captured = []
    async def reply(*args, **kwargs):
        captured.append((args[2], kwargs['route_prompt']))
        await args[3]({'type': 'assistant_message', 'message': {
            'text': '作品方案生成任务超时，方向仍已保存。请检查任务后再试。',
        }})
    monkeypatch.setattr(continuation.chat_service, 'stream_assistant_reply', reply)
    await service._resume_agent(run, ctx)
    messages = ChatStore().list_messages(ctx.requester_username, service.scope(run), limit=20)
    directive = next(message['content'] for message in messages
                     if message.get('message_kind') == 'reference_remix_agent_directive')
    assert '作品方案生成失败' in directive
    assert '方向已确认。请根据已保存方向起草作品方案' not in directive
    assert '作品方案生成失败' in captured[0][0]
    assert '作品方案生成失败' in captured[0][1]
    assert run['status'] == 'drafting_plan'


@pytest.mark.asyncio
async def test_native_stage_failure_notice_is_idempotent_for_the_saved_event(service, tmp_path):
    from novelvideo.chat.store import ChatStore
    ctx = context(tmp_path)
    run = {'run_id': 'native-failure', 'project_id': ctx.project_id,
           'requester_user_id': ctx.requester_user_id, 'username': ctx.requester_username,
           'canvas_id': 'canvas', 'agent_id': 'main',
           'context_json': continuation._serialize_context(ctx)}
    await service._notify(run, '作品方案生成失败，请检查画布任务后重试。', 'plan-task:failed')
    await service._notify(run, '作品方案生成失败，请检查画布任务后重试。', 'plan-task:failed')
    messages = ChatStore().list_messages(ctx.requester_username, service.scope(run), limit=20)
    matching = [row for row in messages if row.get('content') == '作品方案生成失败，请检查画布任务后重试。']
    assert len(matching) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('presented,old_phase', [
    (True, False), (True, True), (False, False), (False, True),
])
async def test_plan_confirmation_event_waits_only_after_ordinary_presentation(
        service, tmp_path, monkeypatch, reviewed_plan, presented, old_phase):
    from novelvideo.chat.plan_confirmation_delivery import (
        metadata_for_delivered_plan, prepare_plan_presentation_target,
    )
    from novelvideo.chat.store import ChatStore

    _, store, commission, _ = reviewed_plan
    ctx = replace(context(tmp_path, actor='u', project='p'),
                  output_dir=tmp_path, state_dir=tmp_path)
    ref = commission['commission_ref']
    run = {'run_id': 'plan-user-wait', 'project_id': ctx.project_id,
           'requester_user_id': ctx.requester_user_id, 'username': ctx.requester_username,
           'canvas_id': 'c', 'agent_id': 'main', 'analysis_node_id': 'analysis',
           'idempotency_key': 'plan-user-wait', 'request_fingerprint': 'plan-user-wait',
           'status': 'analysis_ready',
           'commission_phase': 'awaiting_plan_review' if old_phase else 'awaiting_plan_confirmation',
           'commission_ref': ref, 'context_json': continuation._serialize_context(ctx)}
    service.store.create(run)
    if presented:
        target = prepare_plan_presentation_target(store, ref, agent_id='main')
        body = '当前方案包括三秒产品介绍，请确认或提出修改意见。'
        ChatStore().append_message(ctx.requester_username, service.scope(run), 'assistant', body,
            metadata=metadata_for_delivered_plan(store, target, assistant_delivered=True, message=body))
    async def resolve(_run): return ctx
    monkeypatch.setattr(service, '_resolve_context', resolve)
    monkeypatch.setattr(service, '_assert_current', lambda *_args, **_kwargs: {'nodes': []})
    calls = []
    async def resume(*_args): calls.append('model')
    monkeypatch.setattr(service, '_resume_agent', resume)
    assert service.store.claim(run['run_id'], service.owner_token)
    await service._process_once(service.store.get(run['run_id']))
    assert calls == ([] if presented else ['model'])
    if presented:
        assert service.store.get(run['run_id'])['status'] == 'awaiting_plan_confirmation'

@pytest.mark.asyncio
@pytest.mark.parametrize('foreign_delivery', [False, True])
async def test_card_relation_survives_model_finish_before_first_delivery_tick(service, tmp_path, monkeypatch, foreign_delivery):
    from novelvideo.api import egress_binding
    from novelvideo.freezone import canvas_command_bridge as bridge
    from novelvideo.freezone.canvas_command_bridge import put_pending_clarification_event
    from novelvideo.api.routes.chat import _canvas_bridge_dir
    ctx = context(tmp_path)
    run, _ = await ready_run(service, ctx, monkeypatch)
    run['result_fingerprint'] = continuation._node_fingerprint(service._node(service._canvas(ctx, 'canvas'), 'analysis'))
    assert service.store.update_owned(run, service.owner_token)
    @asynccontextmanager
    async def egress(**kwargs): yield None
    monkeypatch.setattr(egress_binding, 'request_egress_scope', egress)
    async def reply(*args, **kwargs):
        for name, value in {'USERNAME': ctx.requester_username, 'PROJECT_ID': ctx.project_id,
                            'CANVAS_ID': 'canvas', 'AGENT_ID': 'main', 'TURN_ID': kwargs['turn_id']}.items():
            monkeypatch.setenv('DRAMACLAW_' + name, value)
        put_pending_clarification_event(key='instant-card', project_id=ctx.project_id, canvas_id='canvas', event={'clarification_id': 'same-question'}, bridge_dir=_canvas_bridge_dir(ctx.requester_username, project_state_dir=ctx.state_dir, profile='freezone:main'))
    monkeypatch.setattr(continuation.chat_service, 'stream_assistant_reply', reply)
    if foreign_delivery:
        original_read = bridge.read_bridge_message
        def read_with_foreign_delivery(key, **kwargs):
            message = original_read(key, **kwargs)
            return {**message, 'delivery_binding': {'username': 'other'}} if message else message
        monkeypatch.setattr(bridge, 'read_bridge_message', read_with_foreign_delivery)
    await service._resume_agent(run, ctx)
    if foreign_delivery:
        assert not run.get('pending_interaction')
    else:
        assert run['pending_interaction']['bridge_key'] == 'instant-card'
        assert run['execution_blocker']['code'] == 'browser_required'


@pytest.mark.asyncio
async def test_background_turn_ignores_another_collaborators_card(service, tmp_path, monkeypatch):
    from novelvideo.api import egress_binding
    from novelvideo.api.routes.chat import _canvas_bridge_dir
    from novelvideo.freezone.canvas_command_bridge import put_pending_clarification_event

    ctx = context(tmp_path)
    run, _ = await ready_run(service, ctx, monkeypatch)
    run['result_fingerprint'] = continuation._node_fingerprint(
        service._node(service._canvas(ctx, 'canvas'), 'analysis'))
    assert service.store.update_owned(run, service.owner_token)

    @asynccontextmanager
    async def egress(**kwargs): yield None
    monkeypatch.setattr(egress_binding, 'request_egress_scope', egress)

    async def reply(*args, **kwargs):
        for name, value in {'USERNAME': 'peer', 'PROJECT_ID': ctx.project_id,
                            'CANVAS_ID': 'canvas', 'AGENT_ID': 'main', 'TURN_ID': 'peer-turn'}.items():
            monkeypatch.setenv('DRAMACLAW_' + name, value)
        directory = _canvas_bridge_dir(ctx.requester_username,
            project_state_dir=ctx.state_dir, profile='freezone:main')
        put_pending_clarification_event(key='peer-card', project_id=ctx.project_id,
            canvas_id='canvas', event={'clarification_id': 'private-peer-question'},
            bridge_dir=directory)

    monkeypatch.setattr(continuation.chat_service, 'stream_assistant_reply', reply)
    await service._resume_agent(run, ctx)
    assert not run.get('pending_interaction')

@pytest.mark.asyncio
async def test_failed_then_real_saved_api_read_and_assistant_delivery_recovers(service, tmp_path, monkeypatch):
    from novelvideo.api import egress_binding
    from novelvideo.api.routes import canvas_context as route
    from novelvideo.api.auth import get_api_user
    from fastapi import FastAPI
    import httpx
    ctx = context(tmp_path)
    run, _ = await ready_run(service, ctx, monkeypatch)
    path = canvas_store.canvas_path(ctx.state_dir, 'canvas')
    payload = canvas_store.load_canvas_json(path)
    payload['nodes'][1]['data'].update(isAnalyzing=False, rawResult={'video_story': {'summary': '真实三秒镜头切换'}})
    path.write_text(json.dumps(payload))
    run['result_fingerprint'] = continuation._node_fingerprint(payload['nodes'][1])
    assert service.store.update_owned(run, service.owner_token)
    async def resolve(*args, **kwargs): return ctx, ctx.requester_username, 'demo', ctx.output_dir, ctx.state_dir
    monkeypatch.setattr(route.freezone, '_resolve_freezone_project', resolve)
    monkeypatch.setattr(route.freezone, '_canvas_state_project_dir', lambda *_: ctx.state_dir)
    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[get_api_user] = lambda: {'user_id': ctx.requester_user_id, 'username': ctx.requester_username, 'credential_kind': 'agent_session', 'current_scope_kind': 'project', 'current_project_id': ctx.project_id, 'scopes': ['projects:read']}
    @asynccontextmanager
    async def egress(**kwargs): yield None
    monkeypatch.setattr(egress_binding, 'request_egress_scope', egress)
    async def reply(*args, **kwargs):
        url = '/projects/project/freezone/canvases/canvas/context/read'
        headers = {'X-DramaClaw-Turn-ID': kwargs['turn_id'], 'X-DramaClaw-Agent-ID': 'main'}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            failed = await client.post(url, headers=headers, json={'requests': [{'type': 'node_detail', 'node_id': 'analysis'}], 'expected_revision': 999})
            assert failed.status_code == 409
            await args[3]({'type': 'tool_completed', 'name': 'freezone_request_canvas_context', 'result_json': {'ok': False, 'error': 'temporary read failed'}})
            succeeded = await client.post(url, headers=headers, json={'requests': [{'type': 'node_detail', 'node_id': 'analysis'}]})
            assert succeeded.status_code == 200
            await args[3]({'type': 'tool_completed', 'name': 'freezone_request_canvas_context', 'result_json': succeeded.json()})
        await args[3]({'type': 'assistant_message', 'message': {'text': '原片三秒切换镜头，建议保留节奏。'}})
    monkeypatch.setattr(continuation.chat_service, 'stream_assistant_reply', reply)
    await service._resume_agent(run, ctx)
    assert run['status'] == 'awaiting_direction'
    assert run['execution_blocker'] is None
    assert run['last_successful_context_receipt']['source'] == 'saved_canvas'
    assert run['last_successful_context_receipt']['content_digest']

@pytest.mark.asyncio
async def test_only_historical_false_wait_with_saved_tool_error_offers_recovery(service, tmp_path, monkeypatch):
    from novelvideo.api import egress_binding
    ctx = context(tmp_path)
    run, _ = await ready_run(service, ctx, monkeypatch)
    run['result_fingerprint'] = continuation._node_fingerprint(service._node(service._canvas(ctx, 'canvas'), 'analysis'))
    assert service.store.update_owned(run, service.owner_token)
    @asynccontextmanager
    async def egress(**kwargs): yield None
    monkeypatch.setattr(egress_binding, 'request_egress_scope', egress)
    async def reply(*args, **kwargs): pass
    monkeypatch.setattr(continuation.chat_service, 'stream_assistant_reply', reply)
    await service._resume_agent(run, ctx)
    run.update(status='awaiting_direction', execution_blocker=None, error=None)
    assert service.store.update_owned(run, service.owner_token)
    service.store.release(run['run_id'], service.owner_token)
    ordinary = await service.get(ctx=ctx, run_id=run['run_id'])
    assert ordinary['status'] == 'awaiting_direction'
    continuation.ChatStore().append_ui_event(ctx.requester_username, service.scope(run), run['resume_turn_id'],
        {'type': 'agent.tool.updated', 'name': 'freezone_request_canvas_context', 'result_json': {'ok': False, 'error': 'real saved tool error'}})
    recoverable = await service.get(ctx=ctx, run_id=run['run_id'])
    assert recoverable['status'] == 'resume_failed'
    assert recoverable['execution_blocker']['historical_recovery'] is True
    retried = await service.retry(ctx=ctx, run_id=run['run_id'])
    assert retried['task_id'] == run['task_id']
    assert retried['commission_phase'] == 'awaiting_direction'

@pytest.mark.asyncio
@pytest.mark.parametrize('credential', ['agent_session', 'local_trusted_agent'])
async def test_agent_cannot_answer_its_own_persisted_clarification(tmp_path, monkeypatch, credential):
    from fastapi import FastAPI
    import httpx
    from novelvideo.api.auth import get_api_user
    from novelvideo.api.routes import chat as route
    from novelvideo.freezone.canvas_command_bridge import put_pending_clarification_event, read_bridge_message
    directory = route._canvas_bridge_dir('actor', project_state_dir=tmp_path, profile='freezone:main')
    put_pending_clarification_event(key='protected-card', project_id='project', canvas_id='canvas', event={'clarification_id': 'user-only'}, bridge_dir=directory)
    async def state(*args): return tmp_path
    monkeypatch.setattr(route, '_bridge_project_state_dir', state)
    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[get_api_user] = lambda: {'username': 'actor', 'credential_kind': credential}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/chat/clarification-tool-result', json={'bridge_key': 'protected-card', 'turn_id': 'reference-remix-r-resume-0', 'project_id': 'project', 'canvas_id': 'canvas', 'agent_id': 'main', 'answers': {'choice': 'model self approval'}})
    assert response.status_code == 403
    assert 'human' in response.json()['detail']
    assert read_bridge_message('protected-card', bridge_dir=directory).get('result') is None

@pytest.mark.asyncio
async def test_real_answer_after_tool_timeout_before_stream_finish_wakes_after_release(service, tmp_path, monkeypatch):
    from novelvideo.api import egress_binding
    from novelvideo.api.routes import chat as route
    from novelvideo.api.auth import get_api_user
    from novelvideo.chat.live_events import register_chat_websocket, unregister_chat_websocket
    from novelvideo.freezone.canvas_command_bridge import put_pending_clarification_event, wait_clarification_result
    from fastapi import FastAPI
    import httpx
    ctx = context(tmp_path)
    run, _ = await ready_run(service, ctx, monkeypatch)
    path = canvas_store.canvas_path(ctx.state_dir, 'canvas')
    payload = canvas_store.load_canvas_json(path)
    payload['nodes'][1]['data'].update(isAnalyzing=False, rawResult={'video_story': {'summary': '当前目标实际摘要'}})
    path.write_text(json.dumps(payload))
    run.update(status='analysis_ready', result_fingerprint=continuation._node_fingerprint(payload['nodes'][1]))
    assert service.store.update_owned(run, service.owner_token)
    service.store.release(run['run_id'], service.owner_token)
    monkeypatch.setattr(continuation, 'coordinator', service)
    scheduled = []
    monkeypatch.setattr(service, '_ensure_worker', scheduled.append)
    @asynccontextmanager
    async def egress(**kwargs): yield None
    monkeypatch.setattr(egress_binding, 'request_egress_scope', egress)
    async def state(*args): return ctx.state_dir
    monkeypatch.setattr(route, '_bridge_project_state_dir', state)
    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[get_api_user] = lambda: {'username': ctx.requester_username, 'user_id': ctx.requester_user_id}
    frames = []
    delivered = asyncio.Event()
    class Socket:
        async def send_json(self, frame):
            frames.append(frame)
            if frame.get('type') == 'assistant.clarification.event': delivered.set()
    socket = Socket()
    await register_chat_websocket(socket, username=ctx.requester_username, scope=service.scope(run))
    async def reply(*args, **kwargs):
        # Simulate the real hosted subprocess identity, not an unbound legacy producer.
        for name,value in {"USERNAME":ctx.requester_username,"PROJECT_ID":ctx.project_id,"CANVAS_ID":"canvas","AGENT_ID":"main","TURN_ID":kwargs["turn_id"]}.items():
            monkeypatch.setenv("DRAMACLAW_"+name,value)
        directory = route._canvas_bridge_dir(ctx.requester_username, project_state_dir=ctx.state_dir, profile='freezone:main')
        put_pending_clarification_event(key='during-stream', project_id=ctx.project_id, canvas_id='canvas', event={'type': 'assistant.clarification.request', 'clarification_id': 'original-q', 'questions': [{'id': 'choice', 'title': '实际选择'}]}, bridge_dir=directory)
        assert await asyncio.to_thread(wait_clarification_result, 'during-stream', timeout_seconds=1, bridge_dir=directory) is None
        await asyncio.wait_for(delivered.wait(), 2)
        frame = next(frame for frame in frames if frame.get('type') == 'assistant.clarification.event')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/chat/clarification-tool-result', json={'bridge_key': frame['bridge_key'], 'turn_id': kwargs['turn_id'], 'project_id': ctx.project_id, 'canvas_id': 'canvas', 'agent_id': 'main', 'answers': {'choice': 'human-original-answer'}})
        assert response.status_code == 200
        assert service.store.owns(run['run_id'], service.owner_token)
        # Model is still finishing its turn after the genuine user callback.
        await asyncio.sleep(.05)
    monkeypatch.setattr(continuation.chat_service, 'stream_assistant_reply', reply)
    try:
        worker = asyncio.create_task(service._run(run['run_id']))
        await worker
        await asyncio.sleep(.4)
    finally:
        await unregister_chat_websocket(socket)
    awakened = service.store.get(run['run_id'])
    assert awakened['status'] == 'analysis_ready'
    assert awakened['task_id'] == run['task_id']
    assert awakened['user_interaction_receipt']['clarification_id'] == 'original-q'
    assert awakened['user_interaction_receipt']['result']['answers'] == {'choice': 'human-original-answer'}
    assert len(scheduled) == 1 and scheduled[-1]['run_id'] == run['run_id']
    assert service.store.claim(run['run_id'], service.owner_token)
    followup_prompts = []
    async def followup(*args, **kwargs):
        followup_prompts.append(args[2])
        await args[3]({'type': 'assistant_message', 'message': {'text': '根据实际摘要和你的原确认卡选择，建议保留镜头节奏。'}})
    monkeypatch.setattr(continuation.chat_service, 'stream_assistant_reply', followup)
    await service._resume_agent(awakened, ctx)
    assert len(followup_prompts) == 1
    assert 'original-q' in followup_prompts[0] and 'human-original-answer' in followup_prompts[0]
    assert awakened['status'] == 'awaiting_direction'
    assert awakened['task_id'] == run['task_id']
    service.store.release(run['run_id'], service.owner_token)

@pytest.mark.asyncio
@pytest.mark.parametrize('wrong_read_path', ['saved_api', 'host_plan_snapshot'])
async def test_other_analysis_summary_cannot_prove_target_was_read(service, tmp_path, monkeypatch, wrong_read_path):
    from novelvideo.api import egress_binding
    from novelvideo.api.routes import canvas_context as route
    from novelvideo.api.auth import get_api_user
    from fastapi import FastAPI
    import httpx
    ctx = context(tmp_path)
    run, _ = await ready_run(service, ctx, monkeypatch)
    path = canvas_store.canvas_path(ctx.state_dir, 'canvas')
    payload = canvas_store.load_canvas_json(path)
    payload['nodes'][1]['data'].update(isAnalyzing=False, referenceAnalysisRef={'artifact_id': 'target-artifact', 'revision': 1})
    payload['nodes'].append({'id': 'other-analysis', 'type': 'videoStoryNode', 'data': {'sourceVideoUrl': '/other-video.mp4', 'isAnalyzing': False, 'rawResult': {'video_story': {'summary': '另一个视频实际摘要'}}, **({'remixRole': 'production_plan'} if wrong_read_path == 'host_plan_snapshot' else {})}})
    path.write_text(json.dumps(payload))
    run['result_fingerprint'] = continuation._node_fingerprint(payload['nodes'][1])
    assert service.store.update_owned(run, service.owner_token)
    async def resolve(*args, **kwargs): return ctx, ctx.requester_username, 'demo', ctx.output_dir, ctx.state_dir
    monkeypatch.setattr(route.freezone, '_resolve_freezone_project', resolve)
    monkeypatch.setattr(route.freezone, '_canvas_state_project_dir', lambda *_: ctx.state_dir)
    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[get_api_user] = lambda: {'user_id': ctx.requester_user_id, 'username': ctx.requester_username, 'credential_kind': 'agent_session', 'current_scope_kind': 'project', 'current_project_id': ctx.project_id, 'scopes': ['projects:read']}
    @asynccontextmanager
    async def egress(**kwargs): yield None
    monkeypatch.setattr(egress_binding, 'request_egress_scope', egress)
    async def reply(*args, **kwargs):
        if wrong_read_path == 'saved_api':
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                response = await client.post('/projects/project/freezone/canvases/canvas/context/read', headers={'X-DramaClaw-Turn-ID': kwargs['turn_id'], 'X-DramaClaw-Agent-ID': 'main'}, json={'requests': [{'type': 'node_detail', 'node_id': 'other-analysis'}]})
            assert response.status_code == 200
            assert '另一个视频实际摘要' in response.text
        else:
            assert '另一个视频实际摘要' in args[2]
        await args[3]({'type': 'assistant_message', 'message': {'text': '已提供摘要与复刻方向建议'}})
    monkeypatch.setattr(continuation.chat_service, 'stream_assistant_reply', reply)
    await service._resume_agent(run, ctx)
    assert run['status'] == 'resume_failed'
    assert run['commission_phase'] == 'awaiting_direction'
    assert run['execution_blocker']['code'] == 'context_delivery_failed'
    assert run['last_successful_context_receipt'] is None

async def durable_answer_fixture(service, tmp_path, monkeypatch, *, status='resume_failed', answer=True):
    from novelvideo.api.routes.chat import _canvas_bridge_dir
    from novelvideo.freezone import canvas_command_bridge as bridge
    ctx = context(tmp_path)
    run, _ = await ready_run(service, ctx, monkeypatch)
    path = canvas_store.canvas_path(ctx.state_dir, 'canvas')
    payload = canvas_store.load_canvas_json(path)
    payload['nodes'][1]['data'].update(isAnalyzing=False, rawResult={'video_story': {'summary': '真实原片'}})
    path.write_text(json.dumps(payload))
    run.update(status=status, resume_turn_id='original-turn', result_fingerprint=continuation._node_fingerprint(payload['nodes'][1]))
    assert service.store.update_owned(run, service.owner_token)
    service.store.release(run['run_id'], service.owner_token)
    binding = {'username': ctx.requester_username, 'project_id': ctx.project_id, 'canvas_id': 'canvas', 'agent_id': 'main', 'turn_id': 'original-turn'}
    for name, value in binding.items():
        monkeypatch.setenv('DRAMACLAW_' + name.upper(), value)
    directory = _canvas_bridge_dir(ctx.requester_username, project_state_dir=ctx.state_dir, profile='freezone:main')
    event = {'type': 'assistant.clarification.request', 'clarification_id': 'original-question', 'questions': [{'id': 'choice', 'title': '原问题'}]}
    bridge.put_pending_clarification_event(key='restart-question', project_id=ctx.project_id, canvas_id='canvas', event=event, bridge_dir=directory)
    if answer:
        assert bridge.mark_bridge_message_delivered('restart-question', consumer_id='human-tab', binding=binding, bridge_dir=directory)
        bridge.resolve_clarification_result('restart-question', {'ok': True, 'status': 'clarification_frontend_result', 'answers': {'choice': '真实回答'}}, bridge_dir=directory)
    return ctx, run, directory

@pytest.mark.asyncio
async def test_restart_recovers_real_answer_without_local_delivery_or_callback(service, tmp_path, monkeypatch):
    ctx, run, directory = await durable_answer_fixture(service, tmp_path, monkeypatch)
    restarted = continuation.ReferenceRemixCoordinator(continuation.ReferenceRemixStore(service.store.db_path))
    scheduled = []
    monkeypatch.setattr(restarted, '_ensure_worker', scheduled.append)
    await restarted.recover_interactions()
    recovered = restarted.store.get(run['run_id'])
    assert recovered['status'] == 'analysis_ready'
    assert recovered['task_id'] == run['task_id']
    assert recovered['user_interaction_receipt']['event']['questions'][0]['title'] == '原问题'
    assert recovered['user_interaction_receipt']['result']['answers'] == {'choice': '真实回答'}
    await restarted.recover_interactions()
    assert len(scheduled) == 1

@pytest.mark.asyncio
async def test_observer_captures_undelivered_question_without_waking_ordinary_wait(service, tmp_path, monkeypatch):
    ctx, run, directory = await durable_answer_fixture(service, tmp_path, monkeypatch, status='awaiting_direction', answer=False)
    await service.recover_interactions()
    captured = service.store.get(run['run_id'])
    assert captured['status'] == 'awaiting_direction'
    assert captured['pending_interaction']['clarification_id'] == 'original-question'
    assert captured['pending_interaction']['event']['questions'][0]['title'] == '原问题'

@pytest.mark.asyncio
@pytest.mark.parametrize('conflict', ['source', 'actor', 'agent', 'turn', 'fingerprint', 'private'])
async def test_durable_answer_recovery_rejects_changed_scope_or_source(service, tmp_path, monkeypatch, conflict):
    from novelvideo.freezone import canvas_command_bridge as bridge
    ctx, run, directory = await durable_answer_fixture(service, tmp_path, monkeypatch)
    if conflict in {'source', 'private'}:
        path = canvas_store.canvas_path(ctx.state_dir, 'canvas')
        payload = canvas_store.load_canvas_json(path)
        if conflict == 'source':
            payload['nodes'][0]['data']['videoUrl'] = '/changed.mp4'
        else:
            payload.update(access_model='private', owner_principal_id='peer')
        path.write_text(json.dumps(payload))
    else:
        with bridge._bridge_db(directory) as conn:
            row = conn.execute('SELECT payload_json FROM canvas_command_messages WHERE bridge_key=?', ('restart-question',)).fetchone()
            payload = json.loads(row[0])
            if conflict == 'fingerprint':
                payload['request_fingerprint'] = 'tampered'
            else:
                field = {'actor': 'username', 'agent': 'agent_id', 'turn': 'turn_id'}[conflict]
                payload['producer_binding'][field] = 'peer'
            conn.execute('UPDATE canvas_command_messages SET payload_json=? WHERE bridge_key=?', (json.dumps(payload), 'restart-question'))
    await service.recover_interactions()
    assert service.store.get(run['run_id'])['status'] == 'resume_failed'

@pytest.mark.asyncio
async def test_answer_waits_for_active_worker_release_then_two_observers_wake_once(service, tmp_path, monkeypatch):
    ctx, run, directory = await durable_answer_fixture(service, tmp_path, monkeypatch, status='analysis_ready')
    assert service.store.claim(run['run_id'], service.owner_token)
    active = service.store.get(run['run_id'])
    active['status'] = 'resuming'
    assert service.store.update_owned(active, service.owner_token)
    scheduled = []
    monkeypatch.setattr(service, '_ensure_worker', scheduled.append)
    await service.recover_interactions()
    assert service.store.get(run['run_id'])['status'] == 'resuming'
    assert service.store.get(run['run_id'])['pending_interaction']['clarification_id'] == 'original-question'
    # The callback window has elapsed; final output moves to waiting and releases.
    active = service.store.get(run['run_id'])
    active['status'] = 'awaiting_direction'
    assert service.store.update_owned(active, service.owner_token)
    service.store.release(run['run_id'], service.owner_token)
    other = continuation.ReferenceRemixCoordinator(continuation.ReferenceRemixStore(service.store.db_path))
    monkeypatch.setattr(other, '_ensure_worker', scheduled.append)
    await asyncio.gather(service.recover_interactions(), other.recover_interactions())
    recovered = service.store.get(run['run_id'])
    assert recovered['status'] == 'analysis_ready'
    assert recovered['task_id'] == run['task_id']
    assert len(scheduled) == 1

@pytest.mark.asyncio
async def test_startup_recovers_answer_from_expired_resuming_worker(service, tmp_path, monkeypatch):
    ctx, run, directory = await durable_answer_fixture(service, tmp_path, monkeypatch, status='resuming')
    recovered_answer = asyncio.Event()
    def scheduled(row):
        if row.get('user_interaction_receipt'):
            recovered_answer.set()
    monkeypatch.setattr(service, '_ensure_worker', scheduled)
    try:
        await service.resume()
        await asyncio.wait_for(recovered_answer.wait(), 2)
        recovered = service.store.get(run['run_id'])
        assert recovered['status'] == 'analysis_ready'
        assert recovered['resume_attempt'] == 1
        assert recovered['task_id'] == run['task_id']
    finally:
        await service.shutdown()


def insert_interaction_history(store, *, count=1000, consumed=True):
    records = []
    for index in range(count):
        record = {'run_id': f'history-{index}', 'project_id': 'project', 'requester_user_id': 'actor',
                  'username': 'actor', 'canvas_id': 'canvas', 'agent_id': 'main', 'idempotency_key': f'history-{index}',
                  'status': 'completed' if consumed else 'resume_failed', 'resume_turn_id': f'history-turn-{index}'}
        if consumed:
            record['user_interaction_receipt'] = {'turn_id': record['resume_turn_id'], 'result': {'ok': True}}
        records.append((record['run_id'], record['project_id'], record['requester_user_id'], record['canvas_id'],
                        record['agent_id'], record['idempotency_key'], record['status'], json.dumps(record)))
    with store._connect() as conn:
        conn.executemany('INSERT INTO reference_remix_continuations(run_id,project_id,requester_user_id,canvas_id,agent_id,idempotency_key,status,payload_json) VALUES (?,?,?,?,?,?,?,?)', records)


@pytest.mark.asyncio
async def test_consumed_history_cannot_starve_real_answer_recovery(service, tmp_path, monkeypatch):
    insert_interaction_history(service.store)
    ctx, run, directory = await durable_answer_fixture(service, tmp_path, monkeypatch)
    await service.recover_interactions()
    assert service.store.get(run['run_id'])['status'] == 'analysis_ready'


def test_interaction_candidate_scan_pages_durably_and_targets_run_before_limit(service):
    insert_interaction_history(service.store, count=1001, consumed=False)
    assert [run['run_id'] for run in service.store.interaction_candidates(run_id='history-1000')] == ['history-1000']
    first = service.store.interaction_candidates()
    assert len(first) == 100
    # A second process continues the fair cursor rather than starting at row1.
    restarted = continuation.ReferenceRemixStore(service.store.db_path)
    seen = {run['run_id'] for run in first}
    for _ in range(10):
        page = restarted.interaction_candidates()
        assert len(page) <= 100
        seen.update(run['run_id'] for run in page)
    assert seen == {f'history-{i}' for i in range(1001)}
    assert restarted.interaction_candidates()[0]['run_id'] == 'history-0'


@pytest.mark.asyncio
async def test_blocked_old_question_does_not_block_startup_production_or_later_answer(service, tmp_path, monkeypatch):
    from novelvideo.chat.reference_production_observer import ReferenceProductionObserver
    ctx, run, directory = await durable_answer_fixture(service, tmp_path, monkeypatch)
    original_candidates = service.store.interaction_candidates
    original_resolve = service._resolve_context
    blocked, cancelled, production, answer = (asyncio.Event() for _ in range(4))
    scheduled = []
    def candidates(**kwargs):
        rows = original_candidates(**kwargs)
        return [{"run_id":"blocked-old"}, *rows] if not kwargs.get("run_id") else rows
    async def resolve(row):
        if row['run_id'] == 'blocked-old':
            blocked.set()
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        return await original_resolve(row)
    def worker(row):
        scheduled.append(row)
        if row.get('user_interaction_receipt'): answer.set()
    monkeypatch.setattr(service.store, 'interaction_candidates', candidates)
    monkeypatch.setattr(service, '_resolve_context', resolve)
    monkeypatch.setattr(service, '_ensure_worker', worker)
    observer = ReferenceProductionObserver(service)
    async def scan(): production.set(); return 0
    monkeypatch.setattr(observer, 'scan_once', scan)
    try:
        await asyncio.wait_for(service.resume(), 1)
        await observer.resume()
        await asyncio.wait_for(blocked.wait(), 1)
        await asyncio.wait_for(production.wait(), 1)
        await asyncio.wait_for(answer.wait(), 1)
        recovered = service.store.get(run['run_id'])
        assert recovered['user_interaction_receipt']['result']['answers'] == {'choice':'真实回答'}
        await service.recover_interactions(run_id=run['run_id'])
        assert len([r for r in scheduled if r.get('user_interaction_receipt')]) == 1
    finally:
        await observer.shutdown()
        await service.shutdown()
    assert cancelled.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['success', 'empty', 'failed_runtime', 'failed_tool', 'material_failed', 'blocked', 'approval', 'missing_context', 'timeout', 'cancelled', 'invalid_envelope'])
async def test_real_codex_done_and_persisted_message_control_reference_delivery(service, tmp_path, monkeypatch, case):
    from novelvideo.api import egress_binding
    from novelvideo.chat import hermes_sdk
    from novelvideo.chat.runtime_port import ChatBackendEvent
    from novelvideo.chat.store import chat_store
    ctx = context(tmp_path)
    run, _ = await ready_run(service, ctx, monkeypatch)
    path = canvas_store.canvas_path(ctx.state_dir, 'canvas')
    payload = canvas_store.load_canvas_json(path)
    payload['nodes'][1]['data'].update(isAnalyzing=False)
    if case != 'missing_context':
        payload['nodes'][1]['data']['rawResult'] = {'video_story': {'summary':'真实原片三秒切换镜头'}}
    path.write_text(json.dumps(payload))
    run['result_fingerprint'] = continuation._node_fingerprint(payload['nodes'][1])
    assert service.store.update_owned(run, service.owner_token)
    monkeypatch.setenv('DRAMACLAW_CHAT_BACKEND', 'codex')
    monkeypatch.setenv('NOVELVIDEO_STATE_DIR', str(tmp_path / 'state-root'))
    monkeypatch.setenv('NOVELVIDEO_RUNTIME_DIR', str(tmp_path / 'runtime-root'))
    @asynccontextmanager
    async def egress(**kwargs): yield None
    async def authorize(**kwargs): return None
    async def token(*args, **kwargs): return 'fixture-token'
    class Auth:
        async def revoke_agent_session(self, token): pass
    monkeypatch.setattr(egress_binding, 'request_egress_scope', egress)
    monkeypatch.setattr(continuation.chat_service, 'authorize_hermes_launch', authorize)
    monkeypatch.setattr(continuation.chat_service, '_create_page_agent_session_token', token)
    monkeypatch.setattr(continuation.chat_service, 'get_auth_session_port', lambda: Auth())
    monkeypatch.setattr(hermes_sdk, '_issue_turn_capability', lambda **kwargs: None)
    class Thread:
        async def stream(self, prompt):
            if case in {'failed_runtime', 'timeout', 'cancelled'}:
                yield ChatBackendEvent(type='turn_completed', status='failed' if case == 'failed_runtime' else case, error='runtime failed')
            if case in {'failed_tool', 'approval'}:
                yield ChatBackendEvent(type='tool_updated', name='dramaclaw.freezone_prepare_reference_decision', call_id='d', status='failed' if case == 'failed_tool' else 'awaiting_approval', structured={'ok':False,'error':'denied','status':'failed' if case == 'failed_tool' else 'awaiting_approval'})
            if case == 'material_failed':
                yield ChatBackendEvent(
                    type='tool_updated', name='freezone_prepare_material_work',
                    call_id='material-call', status='failed',
                    structured={'ok': False, 'status': 'failed',
                                'code': 'material_work_request_invalid',
                                'phase': 'material_plan_drafting',
                                'error': '素材请求不完整',
                                'errors': [{'path': 'steps[0].source', 'message': '必须使用已保存来源'}],
                                'next_action': 'correct_material_request',
                                'retryable': False, 'side_effects': 'none'},
                )
            message = '' if case == 'empty' else '原片三秒切换镜头，建议保留节奏，等待确认方向。'
            yield ChatBackendEvent(type='complete', text='普通文字可直接交付。' if case == 'invalid_envelope' else message)
    monkeypatch.setattr(continuation.chat_service, '_build_codex_thread', lambda *args, **kwargs: Thread())
    broadcasts = []
    async def broadcast(**kwargs): broadcasts.append(kwargs['payload'])
    monkeypatch.setattr(continuation, 'broadcast_chat_scope_event', broadcast)
    # The public service dispatch, actual Codex stream adapter, finalization and
    # ChatStore append all run; only the external runtime/auth/egress are fixtures.
    await service._resume_agent(run, ctx)
    done = next(event for event in broadcasts if event['type'] == 'chat.done')
    persisted = chat_store.pull_recovery(ctx.requester_username, service.scope(run))['messages']
    assert any(message['id'] == done['message']['id'] and message['content'] == done['message']['content'] for message in persisted)
    assert done['message']['turn_id'] == run['resume_turn_id']
    system_messages = [event['message'] for event in broadcasts if event['type'] == 'assistant.message']
    assert len(system_messages) == 1
    assert system_messages[0]['role'] == 'system'
    assert system_messages[0]['metadata']['recipient'] == 'agent'
    assert system_messages[0]['metadata']['message_kind'] == 'reference_remix_agent_directive'
    assert any(event['type'] == 'assistant.delta' for event in broadcasts)
    if case in {'success', 'blocked', 'approval', 'invalid_envelope'}:
        assert run['status'] == 'awaiting_direction'
        assert run['execution_blocker'] is None
        assert run['last_successful_context_receipt']['source'] == 'host_saved_snapshot'
    elif case == 'failed_tool':
        assert run['status'] == 'resume_failed'
        assert run['execution_blocker']['code'] == 'production_execution_failed'
    elif case == 'material_failed':
        assert run['status'] == 'resume_failed'
        assert run['execution_blocker']['code'] == 'material_work_failed'
        assert run['manual_resume_required'] is True
        assert run['manual_resume_reason']['errors'][0]['path'] == 'steps[0].source'
    else:
        assert run['status'] == 'resume_failed'
        assert run['execution_blocker']['code'] == 'context_delivery_failed'
    persisted_run = service.store.get(run['run_id'])
    assert persisted_run['status'] == run['status']
    assert persisted_run['execution_blocker'] == run['execution_blocker']
    assert run['commission_phase'] == 'awaiting_direction'
    assert run['task_id'] == 'task'

@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [0, 0.1, False, "0"])
async def test_legacy_completed_numeric_fence_requires_exact_task_proof(service, tmp_path, monkeypatch, replacement):
    ctx = context(tmp_path)
    run, task = await ready_run(service, ctx, monkeypatch)
    task.result["video_story"]["shots"][0]["start_time"] = 0.0
    payload = await service._save_analysis(ctx, run, result=task.result)
    run["result_fingerprint"] = continuation._node_fingerprint(payload["nodes"][1])
    payload = json.loads(json.dumps(payload))
    payload["nodes"][1]["data"]["rawResult"]["video_story"]["shots"][0]["start_time"] = replacement
    canvas_store.canvas_path(ctx.state_dir, "canvas").write_text(json.dumps(payload))
    if type(replacement) is int:
        assert service._assert_current(ctx, run, completed=True)
        task.task_id = "wrong-task"
    with pytest.raises(continuation.StaleReference):
        service._assert_current(ctx, run, completed=True)

@pytest.mark.asyncio
async def test_numeric_fence_saved_result_recovery(service, tmp_path, monkeypatch):
    ctx = context(tmp_path)
    run, task = await ready_run(service, ctx, monkeypatch)
    task.result["reference_events"] = [{"start_time": 0.0}]
    payload = await service._save_analysis(ctx, run, result=task.result)
    payload = json.loads(json.dumps(payload))
    payload["nodes"][1]["data"]["referenceEvents"][0]["start_time"] = 0
    payload["nodes"][1]["data"]["rawResult"]["reference_events"][0]["start_time"] = 0
    canvas_store.canvas_path(ctx.state_dir, "canvas").write_text(json.dumps(payload))
    assert service._already_saved_result(ctx, run, task.result)

@pytest.mark.asyncio
@pytest.mark.parametrize("edit", ["roundtrip", "text", "edge", "missing", "untrusted"])
async def test_numeric_fence_pending_snapshots_are_pinned(service, tmp_path, monkeypatch, edit):
    ctx = context(tmp_path)
    run, task = await ready_run(service, ctx, monkeypatch)
    payload = service._canvas(ctx, "canvas")
    for node in payload["nodes"]:
        node["data"]["duration"] = 0.0
    run.update(source_node_snapshot=json.loads(json.dumps(payload["nodes"][0])),
               source_fingerprint=continuation._node_fingerprint(payload["nodes"][0]),
               analysis_node_snapshot=json.loads(json.dumps(payload["nodes"][1])),
               analysis_fingerprint=continuation._node_fingerprint(payload["nodes"][1]))
    for node in payload["nodes"]:
        node["data"]["duration"] = 0
    if edit == "text":
        payload["nodes"][0]["data"]["displayName"] = "edited"
    elif edit == "edge":
        payload["edges"] = []
    elif edit == "missing":
        run.pop("source_node_snapshot")
    elif edit == "untrusted":
        run["analysis_node_snapshot"]["data"]["duration"] = 0
    canvas_store.canvas_path(ctx.state_dir, "canvas").write_text(json.dumps(payload))
    if edit == "roundtrip":
        assert service._assert_current(ctx, run)
    else:
        with pytest.raises(continuation.StaleReference):
            service._assert_current(ctx, run)

@pytest.mark.asyncio
async def test_numeric_fence_legacy_missing_proof_fails_closed(service, tmp_path, monkeypatch):
    ctx = context(tmp_path)
    run, task = await ready_run(service, ctx, monkeypatch)
    task.result["reference_events"] = [{"start_time": 0.0}]
    payload = await service._save_analysis(ctx, run, result=task.result)
    run["result_fingerprint"] = continuation._node_fingerprint(payload["nodes"][1])
    payload = json.loads(json.dumps(payload))
    payload["nodes"][1]["data"]["referenceEvents"][0]["start_time"] = 0
    payload["nodes"][1]["data"]["rawResult"]["reference_events"][0]["start_time"] = 0
    canvas_store.canvas_path(ctx.state_dir, "canvas").write_text(json.dumps(payload))
    run.pop("analysis_node_snapshot")
    with pytest.raises(continuation.StaleReference):
        service._assert_current(ctx, run, completed=True)

@pytest.mark.asyncio
@pytest.mark.parametrize("original,current", [(None, True), (True, False), (False, None)])
async def test_presentation_fence_legacy_source_with_completed_numeric_analysis(service, tmp_path, monkeypatch, original, current):
    ctx = context(tmp_path)
    run, task = await ready_run(service, ctx, monkeypatch)
    task.result["video_story"]["shots"][0]["start_time"] = 0.0
    payload = await service._save_analysis(ctx, run, result=task.result)
    payload = json.loads(json.dumps(payload))
    source = payload["nodes"][0]
    if original is not None:
        source["data"]["isSizeManuallyAdjusted"] = original
    run["source_fingerprint"] = continuation._node_fingerprint(source)
    run.pop("source_node_snapshot", None)
    run["result_fingerprint"] = continuation._node_fingerprint(payload["nodes"][1])
    if current is None:
        source["data"].pop("isSizeManuallyAdjusted", None)
    else:
        source["data"]["isSizeManuallyAdjusted"] = current
    payload["nodes"][1]["data"]["rawResult"]["video_story"]["shots"][0]["start_time"] = 0
    canvas_store.canvas_path(ctx.state_dir, "canvas").write_text(json.dumps(payload))
    assert service._assert_current(ctx, run, completed=True)

@pytest.mark.parametrize("edit", ["media", "text", "type", "nonbool", "nested"])
def test_presentation_fence_legacy_rejects_semantic_changes(edit):
    pinned = {"type": "videoNode", "data": {"videoUrl": "/original.mp4", "displayName": "original"}}
    current = json.loads(json.dumps(pinned))
    current["data"]["isSizeManuallyAdjusted"] = True
    if edit == "media":
        current["data"]["videoUrl"] = "/edited.mp4"
    elif edit == "text":
        current["data"]["displayName"] = "edited"
    elif edit == "type":
        current["type"] = "imageNode"
    elif edit == "nonbool":
        current["data"]["isSizeManuallyAdjusted"] = 1
    else:
        current["data"]["other"] = {"isSizeManuallyAdjusted": True}
    assert not continuation._numeric_node_matches(current, continuation._node_fingerprint(pinned), None)

@pytest.mark.parametrize("edit", ["roundtrip", "untrusted", "missing", "bool", "string", "fractional", "nonbool"])
def test_presentation_fence_numeric_requires_raw_pinned_snapshot(edit):
    pinned = {"type": "videoNode", "data": {"duration": 0.0, "isSizeManuallyAdjusted": False}}
    expected = continuation._node_fingerprint(pinned)
    current = {"type": "videoNode", "data": {"duration": 0, "isSizeManuallyAdjusted": True}}
    snapshot = json.loads(json.dumps(pinned))
    if edit == "untrusted":
        snapshot["data"]["duration"] = 0
    elif edit == "missing":
        snapshot = None
    elif edit in ("bool", "string", "fractional"):
        current["data"]["duration"] = {"bool": False, "string": "0", "fractional": 0.1}[edit]
    elif edit == "nonbool":
        current["data"]["isSizeManuallyAdjusted"] = "true"
    assert continuation._numeric_node_matches(current, expected, snapshot) is (edit == "roundtrip")

@pytest.mark.asyncio
@pytest.mark.parametrize("edit", ["presentation", "edge", "source_text", "analysis_text"])
async def test_presentation_fence_pending_snapshots_preserve_semantic_gates(service, tmp_path, monkeypatch, edit):
    ctx = context(tmp_path)
    run, task = await ready_run(service, ctx, monkeypatch)
    payload = json.loads(json.dumps(service._canvas(ctx, "canvas")))
    run["source_node_snapshot"] = json.loads(json.dumps(payload["nodes"][0]))
    for node in payload["nodes"]:
        node["data"]["isSizeManuallyAdjusted"] = True
    if edit == "edge":
        payload["edges"] = []
    elif edit == "source_text":
        payload["nodes"][0]["data"]["displayName"] = "edited"
    elif edit == "analysis_text":
        payload["nodes"][1]["data"]["rows"] = [{"text": "edited"}]
    canvas_store.canvas_path(ctx.state_dir, "canvas").write_text(json.dumps(payload))
    if edit == "presentation":
        assert service._assert_current(ctx, run)
    else:
        with pytest.raises(continuation.StaleReference):
            service._assert_current(ctx, run)

@pytest.mark.parametrize("original,current,allowed", [(1.0, 1, False), (1.0, 1.0, True), (1, True, False), (True, 1, False), (None, 1, False)])
def test_presentation_fence_nonboolean_flag_requires_exact_raw_value(original, current, allowed):
    pinned = {"type": "videoNode", "data": {"duration": 0.0}}
    node = {"type": "videoNode", "data": {"duration": 0}}
    if original is not None:
        pinned["data"]["isSizeManuallyAdjusted"] = original
    node["data"]["isSizeManuallyAdjusted"] = current
    assert continuation._numeric_node_matches(node, continuation._node_fingerprint(pinned), pinned) is allowed

@pytest.mark.asyncio
@pytest.mark.parametrize("edit", ["roundtrip", "text", "nonbool", "pending_proof", "task", "requester"])
async def test_presentation_fence_completed_reconstruction_after_pending_flag(service, tmp_path, monkeypatch, edit):
    ctx = context(tmp_path)
    run, task = await ready_run(service, ctx, monkeypatch)
    task.result["video_story"]["shots"][0]["start_time"] = 0.0
    payload = json.loads(json.dumps(service._canvas(ctx, "canvas")))
    payload["nodes"][1]["data"]["isSizeManuallyAdjusted"] = True
    canvas_store.canvas_path(ctx.state_dir, "canvas").write_text(json.dumps(payload))
    assert service._assert_current(ctx, run)
    saved = await service._save_analysis(ctx, run, result=task.result)
    run["result_fingerprint"] = continuation._node_fingerprint(saved["nodes"][1])
    current = json.loads(json.dumps(saved))
    current["nodes"][1]["data"]["rawResult"]["video_story"]["shots"][0]["start_time"] = 0
    if edit == "text":
        current["nodes"][1]["data"]["rawResult"]["video_story"]["title"] = "edited"
    elif edit == "nonbool":
        current["nodes"][1]["data"]["isSizeManuallyAdjusted"] = 1
    elif edit == "pending_proof":
        run["analysis_node_snapshot"]["data"]["isSizeManuallyAdjusted"] = True
    elif edit == "task":
        task.task_id = "wrong"
    elif edit == "requester":
        task.requester_user_id = "wrong"
    canvas_store.canvas_path(ctx.state_dir, "canvas").write_text(json.dumps(current))
    if edit == "roundtrip":
        assert service._assert_current(ctx, run, completed=True)
        assert continuation._node_fingerprint(service._completed_snapshot(ctx, run)) == run["result_fingerprint"]
    else:
        with pytest.raises(continuation.StaleReference):
            service._assert_current(ctx, run, completed=True)

@pytest.mark.asyncio
async def test_completed_snapshot_survives_terminal_task_expiry(service, tmp_path, monkeypatch):
    ctx = context(tmp_path)
    run, task = await ready_run(service, ctx, monkeypatch)
    task.result['video_story']['shots'][0]['start_time'] = 0.0
    async def resume(*_args): pass
    monkeypatch.setattr(service, '_resume_agent', resume)
    await service._process_once(run)
    run = service.store.get(run['run_id'])
    assert isinstance(run.get('completed_analysis_node_snapshot'), dict)
    assert continuation._node_fingerprint(run['completed_analysis_node_snapshot']) == run['result_fingerprint']
    monkeypatch.setattr(continuation, 'get_task_manager', lambda: SimpleNamespace(get_task_for_project=lambda *_a, **_k: None))
    path = canvas_store.canvas_path(ctx.state_dir, 'canvas')
    payload = canvas_store.load_canvas_json(path)
    payload['nodes'][1]['data']['rawResult']['video_story']['shots'][0]['start_time'] = 0
    path.write_text(json.dumps(payload))
    assert service._assert_current(ctx, run, completed=True)


async def legacy_joint_proof(service, tmp_path, monkeypatch):
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.freezone.remix.reference_analysis_service import source_snapshot
    ctx = context(tmp_path)
    run, task = await ready_run(service, ctx, monkeypatch)
    ctx.output_dir.mkdir(parents=True, exist_ok=True)
    (ctx.output_dir / 'video.mp4').write_bytes(b'original media')
    source = source_snapshot(ctx, {'media_path': 'video.mp4'})
    operations = RemixOperationStore(ctx.output_dir, ctx.project_id)
    operation = operations.start(idempotency_key='analysis', input_refs=[], producer_version='test',
        normalized_parameters={'action': 'joint_reference_analysis', 'payload': {'media_path': 'video.mp4'}, 'media_sha256': source['media_sha256']},
        node_revision=1, input_media_hashes=['sha256:' + source['media_sha256']])
    fence = operations.claim_submission(operation['operation_id'], owner='test')
    operations.bind_task(operation['operation_id'], task_id=task.task_id, owner='test', fencing_token=fence)
    story = {'video_story': {'summary': 'Structure', 'shots': [{'start_time': 0.0}]}}
    receipt = {'media_path': 'video.mp4', 'media_hash': 'sha256:' + source['media_sha256'],
        'story': story, 'evidence_ref': {'artifact_id': 'evidence', 'revision': 1}, 'transcript_ref': None,
        'blueprint_ref': None, 'transcript_status': 'no_audio', 'limitations': [], 'coverage': {}}
    artifact = RemixArtifactStore(ctx.output_dir, ctx.project_id).create('reference_analysis.v1', receipt, [])
    ref = {'artifact_id': artifact['id'], 'revision': artifact['revision']}
    operations.publish_result_unverified(operation['operation_id'], output_refs=[ref])
    run.update(task_type='remix_joint_reference_analysis', task_scope=operation['operation_id'], operation_id=operation['operation_id'])
    result = {**story, 'status': 'completed', 'analysis_ref': ref, 'artifact_ref': ref,
        **{key: receipt[key] for key in ('evidence_ref', 'transcript_ref', 'blueprint_ref', 'transcript_status', 'limitations', 'coverage')}}
    payload = await service._save_analysis(ctx, run, result=result)
    run['result_fingerprint'] = continuation._node_fingerprint(payload['nodes'][1])
    payload['nodes'][1]['data']['rawResult']['video_story']['shots'][0]['start_time'] = 0
    canvas_store.canvas_path(ctx.state_dir, 'canvas').write_text(json.dumps(payload))
    monkeypatch.setattr(continuation, 'get_task_manager', lambda: SimpleNamespace(get_task_for_project=lambda *_a, **_k: None))
    return ctx, run, operations, artifact, task


@pytest.mark.asyncio
async def test_legacy_joint_completed_proof_survives_terminal_task_expiry(service, tmp_path, monkeypatch):
    ctx, run, *_ = await legacy_joint_proof(service, tmp_path, monkeypatch)
    assert service._assert_current(ctx, run, completed=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('tamper', ['no_pin', 'no_snapshot', 'snapshot', 'operation', 'task', 'action', 'project', 'status', 'output', 'media', 'canvas', 'node', 'existing_failed', 'existing_wrong', 'semantic', 'nonbool', 'raw_pin', 'schema', 'artifact_hash', 'input_hash', 'output_project', 'output_revision', 'parameters_shape', 'outputs_shape', 'ref_shape'])
async def test_legacy_joint_durable_proof_rejects_tampering(service, tmp_path, monkeypatch, tamper):
    ctx, run, operations, artifact, task = await legacy_joint_proof(service, tmp_path, monkeypatch)
    op_path = operations._path(run['operation_id'])
    record = operations.get(run['operation_id'])
    if tamper == 'no_pin': run['result_fingerprint'] = None
    elif tamper == 'no_snapshot': run.pop('analysis_node_snapshot')
    elif tamper == 'snapshot': run['analysis_node_snapshot']['data']['rows'] = ['untrusted']
    elif tamper in {'operation', 'task', 'project', 'status', 'canvas', 'node'}:
        record[{'operation': 'operation_id', 'task': 'task_id', 'project': 'project_id', 'status': 'status', 'canvas': 'canvas_id', 'node': 'node_id'}[tamper]] = 'wrong'
    elif tamper == 'parameters_shape': record['normalized_parameters'] = []
    elif tamper == 'outputs_shape': record['output_refs'] = {'artifact_id': 'bad'}
    elif tamper == 'ref_shape': record['output_refs'] = ['bad']
    elif tamper == 'raw_pin': run['result_fingerprint'] = 'untrusted'
    elif tamper == 'input_hash': record['input_media_hashes'] = ['sha256:' + '0' * 64]
    elif tamper == 'output_project': record['output_refs'][0]['project_id'] = 'other'
    elif tamper == 'output_revision': record['output_refs'][0]['revision'] = 2
    elif tamper in {'schema', 'artifact_hash'}:
        from novelvideo.freezone.remix.store import RemixArtifactStore
        store = RemixArtifactStore(ctx.output_dir, ctx.project_id)
        path = store._path(artifact['id'], artifact['revision'], '.json' if tamper == 'schema' else '.payload.json')
        value = json.loads(path.read_text())
        value['schema' if tamper == 'schema' else 'story'] = 'invalid'
        path.write_text(json.dumps(value))
    elif tamper == 'action': record['normalized_parameters']['action'] = 'other'
    elif tamper == 'output': record['output_refs'] = []
    elif tamper == 'media': (ctx.output_dir / 'video.mp4').write_bytes(b'changed')
    elif tamper.startswith('existing_'):
        task.status = 'failed' if tamper == 'existing_failed' else 'completed'
        if tamper == 'existing_wrong': task.task_id = 'wrong'
        monkeypatch.setattr(continuation, 'get_task_manager', lambda: SimpleNamespace(get_task_for_project=lambda *_a, **_k: task))
    else:
        path = canvas_store.canvas_path(ctx.state_dir, 'canvas')
        payload = canvas_store.load_canvas_json(path)
        if tamper == 'semantic': payload['nodes'][1]['data']['rawResult']['video_story']['summary'] = 'edited'
        else: payload['nodes'][1]['data']['isSizeManuallyAdjusted'] = 0
        path.write_text(json.dumps(payload))
    op_path.write_text(json.dumps(record))
    with pytest.raises(continuation.StaleReference): service._assert_current(ctx, run, completed=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('manual_size', [None, False, True])
async def test_legacy_joint_retained_proof_allows_presentation_roundtrip(service, tmp_path, monkeypatch, manual_size):
    ctx, run, *_ = await legacy_joint_proof(service, tmp_path, monkeypatch)
    path = canvas_store.canvas_path(ctx.state_dir, 'canvas')
    payload = canvas_store.load_canvas_json(path)
    if manual_size is not None:
        payload['nodes'][1]['data']['isSizeManuallyAdjusted'] = manual_size
    payload['nodes'][1].update(position={'x': 123, 'y': 456}, selected=True)
    path.write_text(json.dumps(payload))
    assert service._assert_current(ctx, run, completed=True)
    assert continuation._node_fingerprint(service._completed_snapshot(ctx, run)) == run['result_fingerprint']


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid_task', ['failed', 'task_id', 'project_id', 'requester_user_id'])
async def test_completed_snapshot_rejects_existing_invalid_task(service, tmp_path, monkeypatch, invalid_task):
    ctx = context(tmp_path)
    run, task = await ready_run(service, ctx, monkeypatch)
    task.result['video_story']['shots'][0]['start_time'] = 0.0
    async def resume(*_args): pass
    monkeypatch.setattr(service, '_resume_agent', resume)
    await service._process_once(run)
    run = service.store.get(run['run_id'])
    if invalid_task == 'failed': task.status = 'failed'
    else: setattr(task, invalid_task, 'other')
    path = canvas_store.canvas_path(ctx.state_dir, 'canvas')
    payload = canvas_store.load_canvas_json(path)
    payload['nodes'][1]['data']['rawResult']['video_story']['shots'][0]['start_time'] = 0
    path.write_text(json.dumps(payload))
    with pytest.raises(continuation.StaleReference): service._assert_current(ctx, run, completed=True)


async def transcript_ready_run(service, tmp_path, monkeypatch):
    import hashlib
    from novelvideo.freezone.remix.store import RemixArtifactStore
    ctx = context(tmp_path)
    run, task = await ready_run(service, ctx, monkeypatch)
    ctx.output_dir.mkdir(parents=True, exist_ok=True)
    (ctx.output_dir / 'video.mp4').write_bytes(b'original-video')
    digest = 'sha256:' + hashlib.sha256(b'original-video').hexdigest()
    store = RemixArtifactStore(ctx.output_dir, ctx.project_id)
    words = store.create('word_timeline.v1', {'media_hash': digest, 'language': 'zh', 'status': 'completed',
        'transcript': '原片台词', 'words': [{'text': '原片台词', 'start_char': 0, 'end_char': 4, 'start_us': 123456, 'end_us': 987654}]}, [])
    ref = {'artifact_id': words['id'], 'revision': words['revision']}
    analysis = store.create('reference_analysis.v1', {'media_path': 'video.mp4', 'media_hash': digest,
        'evidence_ref': {}, 'transcript_ref': ref, 'transcript_status': 'completed', 'story': task.result,
        'coverage': {}}, [ref])
    task.result.update(analysis_ref={'artifact_id': analysis['id'], 'revision': 1}, transcript_ref=ref, transcript_status='completed')
    return ctx, run, task, store, ref, digest


@pytest.mark.asyncio
async def test_completed_reference_analysis_saves_genuine_transcript_once_in_same_canvas_transaction(service, tmp_path, monkeypatch):
    ctx, run, task, store, ref, digest = await transcript_ready_run(service, tmp_path, monkeypatch)
    payload = await service._save_analysis(ctx, run, result=task.result)
    transcript = next((node for node in payload['nodes'] if node['type'] == 'transcriptNode'), None)
    assert transcript is not None, 'completed reference recognition must be visible on canvas'
    assert transcript['data']['artifactRef'] == {'artifactId': ref['artifact_id'], 'revision': 1}
    assert transcript['data']['words'] == [{'text': '原片台词', 'startUs': 123456, 'endUs': 987654, 'startChar': 0, 'endChar': 4, 'confidence': None}]
    assert transcript['data']['sourceMediaPath'] == 'video.mp4'
    assert transcript['data']['sourceMediaHash'] == digest.removeprefix('sha256:')
    assert transcript['data']['status'] == 'completed'
    assert {('source', transcript['id']), (transcript['id'], 'analysis')} <= {(edge['source'],edge['target']) for edge in payload['edges']}
    again = await service._save_analysis(ctx, run, result=task.result)
    assert [n['id'] for n in again['nodes'] if n['type'] == 'transcriptNode'] == [transcript['id']]
    assert len(again['edges']) == len(payload['edges'])
    assert store.read(ref['artifact_id'], 1)['payload']['transcript'] == '原片台词'


@pytest.mark.asyncio
async def test_reference_transcript_preserves_matching_user_correction(service, tmp_path, monkeypatch):
    ctx, run, task, store, ref, digest = await transcript_ready_run(service, tmp_path, monkeypatch)
    payload = service._canvas(ctx, 'canvas')
    corrected = store.read(ref['artifact_id'], 1)['payload']
    corrected.update(transcript='校字', words=[{'text':'校字','start_char':0,'end_char':2,'start_us':123456,'end_us':987654}])
    store.revise(ref['artifact_id'], 1, corrected, [])
    data = {'artifactRef': {'artifactId': ref['artifact_id'], 'revision': 2}, 'status': 'completed',
            'sourceMediaPath': 'video.mp4', 'sourceMediaHash': digest.removeprefix('sha256:'),
            'words': [{'text': '校字', 'startUs': 123456, 'endUs': 987654}], 'transcriptText': '校字'}
    payload['nodes'].append({'id': 'corrected', 'type': 'transcriptNode', 'data': data})
    payload['edges'].append({'id': 'corrected-source', 'source': 'source', 'target': 'corrected'})
    canvas_store.canvas_path(ctx.state_dir, 'canvas').write_text(json.dumps(payload))
    with pytest.raises(continuation.StaleReference, match='实际输入不同'):
        await service._save_analysis(ctx, run, result=task.result)
    saved = service._canvas(ctx, 'canvas')
    assert [n['id'] for n in saved['nodes'] if n['type'] == 'transcriptNode'] == ['corrected']
    assert service._node(saved, 'corrected')['data'] == data
    assert not any(e['source'] == 'corrected' and e['target'] == 'analysis' for e in saved['edges'])


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['no_audio', 'no_speech', 'failed'])
async def test_reference_analysis_without_completed_recognition_never_invents_transcript(service, tmp_path, monkeypatch, state):
    ctx, run, task, *_ = await transcript_ready_run(service, tmp_path, monkeypatch)
    task.result.update(transcript_status=state, transcript_ref=None)
    payload = await service._save_analysis(ctx, run, result=task.result)
    assert not any(n['type'] == 'transcriptNode' for n in payload['nodes'])
    assert service._node(payload, 'analysis')['data']['referenceTranscriptStatus'] == state


@pytest.mark.asyncio
async def test_resume_failed_retry_restores_old_missing_transcript_without_recognition(service, tmp_path, monkeypatch):
    ctx, run, task, *_ = await transcript_ready_run(service, tmp_path, monkeypatch)
    payload = await service._save_analysis(ctx, run, result=task.result)
    analysis = service._node(payload, 'analysis')
    payload['nodes'] = [n for n in payload['nodes'] if n['type'] != 'transcriptNode']
    payload['edges'] = [e for e in payload['edges'] if e['source'] == 'source' and e['target'] == 'analysis']
    canvas_store.canvas_path(ctx.state_dir, 'canvas').write_text(json.dumps(payload))
    run.update(status='resume_failed', result_fingerprint=continuation._node_fingerprint(analysis), completed_analysis_node_snapshot=analysis)
    assert service.store.update_owned(run, service.owner_token)
    service.store.release(run['run_id'], service.owner_token)
    resumed = await service.retry(ctx=ctx, run_id=run['run_id'])
    saved = service._canvas(ctx, 'canvas')
    assert any(node['type'] == 'transcriptNode' for node in saved['nodes']), 'retry must restore genuine already completed recognition'
    assert resumed['task_id'] == task.task_id
    assert service._assert_current(ctx, service.store.get(run['run_id']), completed=True)


@pytest.mark.asyncio
async def test_saved_analysis_adoption_restores_transcript_without_new_task(service, tmp_path, monkeypatch):
    ctx, run, task, *_ = await transcript_ready_run(service, tmp_path, monkeypatch)
    task.result['video_story']['summary'] = '已完成真实原片摘要'
    saved = await service._save_analysis(ctx, run, result=task.result)
    saved['nodes'] = [n for n in saved['nodes'] if n['type'] != 'transcriptNode']
    saved['edges'] = [e for e in saved['edges'] if e['source'] == 'source' and e['target'] == 'analysis']
    canvas_store.canvas_path(ctx.state_dir, 'canvas').write_text(json.dumps(saved))
    new = await service.reserve(ctx=ctx, canvas_id='canvas', source_node_id='source', analysis_node_id='analysis', goal='复用结果', inputs={}, idempotency_key='adopt-existing')
    token = await service.claim_submission(ctx=ctx, run_id=new['run_id'])
    adopted = await service.adopt_saved_analysis(ctx=ctx, run_id=new['run_id'], submission_token=token)
    assert adopted['task_id'] is None
    assert any(n['type'] == 'transcriptNode' for n in service._canvas(ctx, 'canvas')['nodes'])


@pytest.mark.asyncio
async def test_saved_analysis_adoption_does_not_restore_after_submission_lease_loss(service, tmp_path, monkeypatch):
    ctx, run, task, *_ = await transcript_ready_run(service, tmp_path, monkeypatch)
    saved = await service._save_analysis(ctx, run, result=task.result)
    saved['nodes'] = [node for node in saved['nodes'] if node['type'] != 'transcriptNode']
    saved['edges'] = [edge for edge in saved['edges'] if edge['source'] == 'source' and edge['target'] == 'analysis']
    path = canvas_store.canvas_path(ctx.state_dir, 'canvas')
    path.write_text(json.dumps(saved))
    new = await service.reserve(ctx=ctx, canvas_id='canvas', source_node_id='source',
                                analysis_node_id='analysis', goal='复用结果', inputs={},
                                idempotency_key='adopt-after-lease-loss')
    token = await service.claim_submission(ctx=ctx, run_id=new['run_id'])
    before = path.read_bytes()
    original_save = canvas_store.save_canvas_async

    async def lose_lease_before_write(*args, **kwargs):
        build = kwargs['build_payload']

        def wrapped_build(existing):
            payload = build(existing)
            service.store.release(new['run_id'], token)
            return payload

        return await original_save(*args, **{**kwargs, 'build_payload': wrapped_build})

    monkeypatch.setattr(canvas_store, 'save_canvas_async', lose_lease_before_write)
    with pytest.raises(ValueError, match='lease'):
        await service.adopt_saved_analysis(ctx=ctx, run_id=new['run_id'], submission_token=token)
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_reference_transcript_fills_existing_empty_node_and_consumes_auto_start(service, tmp_path, monkeypatch):
    ctx, run, task, *_ = await transcript_ready_run(service, tmp_path, monkeypatch)
    payload = service._canvas(ctx, 'canvas')
    receipt = task.result['analysis_ref']
    from novelvideo.freezone.remix.store import RemixArtifactStore
    media_hash = RemixArtifactStore(ctx.output_dir, ctx.project_id).read(receipt['artifact_id'], 1)['payload']['media_hash'].removeprefix('sha256:')
    payload['nodes'].append({'id':'empty','type':'transcriptNode','data':{'sourceMediaPath':'video.mp4','sourceMediaHash':media_hash,
        'words':[],'status':'needs_review','autoStartMode':'transcribe'}})
    payload['edges'].append({'id':'empty-link','source':'source','target':'empty'})
    canvas_store.canvas_path(ctx.state_dir, 'canvas').write_text(json.dumps(payload))
    saved = await service._save_analysis(ctx, run, result=task.result)
    words = service._node(saved, 'empty')['data']
    assert words['words'][0]['text'] == '原片台词'
    assert words['autoStartMode'] is None
    assert words['status'] == 'completed'
    assert len([n for n in saved['nodes'] if n['type'] == 'transcriptNode']) == 1


async def native_receipt_run(service, tmp_path, monkeypatch):
    ctx, run, operations, artifact, task = await legacy_joint_proof(service, tmp_path, monkeypatch)
    run.update(status='analysis_ready', completed_analysis_node_snapshot=service._node(service._canvas(ctx,'canvas'),'analysis'))
    assert service.store.update_owned(run,service.owner_token)
    return ctx,run,operations,artifact,task


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['unchanged','source_url','bytes','agent','key','canvas','actor','status','task','operation','edge'])
async def test_native_submission_receipt_reads_original_fences_and_media_without_admission(service,tmp_path,monkeypatch,change):
    ctx,run,operations,artifact,task = await native_receipt_run(service,tmp_path,monkeypatch)
    before = canvas_store.canvas_path(ctx.state_dir,'canvas').read_bytes()
    lookup={'ctx':ctx,'run_id':run['run_id'],'canvas_id':'canvas','source_node_id':'source','agent_id':'main','idempotency_key':'request'}
    if change=='source_url':
        payload=service._canvas(ctx,'canvas');payload['nodes'][0]['data']['videoUrl']='/replacement.mp4'
        canvas_store.canvas_path(ctx.state_dir,'canvas').write_text(json.dumps(payload))
    elif change=='bytes':(ctx.output_dir/'video.mp4').write_bytes(b'replaced same URL')
    elif change in {'agent','key','canvas'}:lookup[{'agent':'agent_id','key':'idempotency_key','canvas':'canvas_id'}[change]]='different'
    elif change=='actor':lookup['ctx']=context(tmp_path,actor='other')
    elif change=='status':
        run.update(status='stale');assert service.store.update_owned(run,service.owner_token)
    elif change in {'task','operation'}:
        record=operations.get(run['operation_id']);record['task_id' if change=='task' else 'normalized_parameters']='different'
        operations._path(run['operation_id']).write_text(json.dumps(record))
    elif change=='edge':
        payload=service._canvas(ctx,'canvas');payload['edges']=[];canvas_store.canvas_path(ctx.state_dir,'canvas').write_text(json.dumps(payload))
    persisted_before=service.store.get(run['run_id']); canvas_before=canvas_store.canvas_path(ctx.state_dir,'canvas').read_bytes()
    if not hasattr(service,'read_submission_receipt'):pytest.fail('native submission read proof is not implemented')
    if change in {'agent','key','canvas','actor'}:
        assert await service.read_submission_receipt(**lookup) is None
    elif change=='unchanged':
        first=await service.read_submission_receipt(**lookup);second=await service.read_submission_receipt(**lookup)
        assert first==second and first['run_id']==run['run_id'] and first['task_id']==task.task_id
        assert canvas_store.canvas_path(ctx.state_dir,'canvas').read_bytes()==before
        assert service.store.get(run['run_id'])==run
    else:
        with pytest.raises(ValueError):await service.read_submission_receipt(**lookup)
    assert service.store.get(run['run_id']) == persisted_before
    assert canvas_store.canvas_path(ctx.state_dir,'canvas').read_bytes() == canvas_before


async def bound_transcript_run(service, tmp_path, monkeypatch):
    ctx, run, task, store, ref, digest = await transcript_ready_run(service, tmp_path, monkeypatch)
    payload = service._canvas(ctx, 'canvas')
    node = {'id': 'input-words', 'type': 'transcriptNode', 'data': {
        'artifactRef': {'artifactId': ref['artifact_id'], 'revision': 1}, 'status': 'completed',
        'sourceMediaPath': 'video.mp4', 'sourceMediaHash': digest.removeprefix('sha256:'),
        'words': [{'text': '原片台词', 'startUs': 123456, 'endUs': 987654}], 'transcriptText': '原片台词'}}
    payload['nodes'].append(node)
    payload['edges'].extend([{'id': 'source-words', 'source': 'source', 'target': 'input-words'},
                             {'id': 'words-analysis', 'source': 'input-words', 'target': 'analysis'}])
    canvas_store.canvas_path(ctx.state_dir, 'canvas').write_text(json.dumps(payload))
    run['transcript_dependency'] = {'node_id': node['id'], 'artifact_ref': ref,
        'node_fingerprint': continuation._node_fingerprint(node), 'required_links': [
        {'source': 'source', 'target': 'input-words'}, {'source': 'input-words', 'target': 'analysis'}]}
    run['edge_fingerprint'] = service._edges_fingerprint(payload, run)
    assert service.store.update_owned(run, service.owner_token)
    return ctx, run, task, store, ref


def change_transcript_dependency(service, ctx, change):
    payload = service._canvas(ctx, 'canvas')
    if change == 'revision':
        service._node(payload, 'input-words')['data']['artifactRef']['revision'] = 2
        service._node(payload, 'input-words')['data']['transcriptText'] = '用户校字'
    elif change == 'words':
        service._node(payload, 'input-words')['data']['words'][0]['text'] = '用户编辑'
    elif change == 'node':
        payload['nodes'] = [node for node in payload['nodes'] if node['id'] != 'input-words']
    else:
        payload['edges'] = [edge for edge in payload['edges'] if edge['id'] != change]
    canvas_store.canvas_path(ctx.state_dir, 'canvas').write_text(json.dumps(payload))
    return canvas_store.canvas_path(ctx.state_dir, 'canvas').read_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['revision', 'words', 'node', 'words-analysis', 'source-words'])
async def test_bound_native_dependency_changes_stop_actual_result_publication(service, tmp_path, monkeypatch, change):
    ctx, run, task, *_ = await bound_transcript_run(service, tmp_path, monkeypatch)
    before = change_transcript_dependency(service, ctx, change)
    with pytest.raises(continuation.StaleReference):
        service._assert_current(ctx, run)
    with pytest.raises(continuation.StaleReference):
        await service._save_analysis(ctx, run, result=task.result)
    assert canvas_store.canvas_path(ctx.state_dir, 'canvas').read_bytes() == before
    monkeypatch.setattr(service, '_resume_agent', lambda *a: pytest.fail('changed dependency must not resume'))
    await service._process_once(run)
    assert service.store.get(run['run_id'])['status'] == 'stale'
    assert canvas_store.canvas_path(ctx.state_dir, 'canvas').read_bytes() == before


@pytest.mark.asyncio
async def test_native_reservation_persists_exact_transcript_dependency_in_request_identity(service, tmp_path, monkeypatch):
    ctx, run, _task, _store, ref = await bound_transcript_run(service, tmp_path, monkeypatch)
    new = await service.reserve(ctx=ctx, canvas_id='canvas', source_node_id='source', analysis_node_id='analysis',
        goal='继续复刻', inputs={}, idempotency_key='bound-admission', transcript_node_id='input-words', transcript_ref=ref)
    stored = service.store.get(new['run_id'])
    assert stored['transcript_dependency'] == run['transcript_dependency']
    payload = service._canvas(ctx, 'canvas')
    service._node(payload, 'input-words')['data']['artifactRef']['revision'] = 2
    canvas_store.canvas_path(ctx.state_dir, 'canvas').write_text(json.dumps(payload))
    with pytest.raises(ValueError, match='idempotency'):
        await service.reserve(ctx=ctx, canvas_id='canvas', source_node_id='source', analysis_node_id='analysis',
            goal='继续复刻', inputs={}, idempotency_key='bound-admission', transcript_node_id='input-words',
            transcript_ref={**ref, 'revision': 2})


@pytest.mark.asyncio
async def test_saved_no_speech_dependency_is_pinned_to_its_node_and_revision(service, tmp_path):
    ctx = context(tmp_path)
    write_canvas(ctx)
    path = canvas_store.canvas_path(ctx.state_dir, 'canvas')
    canvas = canvas_store.load_canvas_json(path)
    ref = {'artifact_id': 'observation', 'revision': 1}
    canvas['nodes'].append({'id': 'words', 'type': 'transcriptNode',
        'data': {'status': 'no_speech', 'artifactRef': {'artifactId': 'observation', 'revision': 1}}})
    canvas['edges'].extend([{'source': 'source', 'target': 'words'},
                            {'source': 'words', 'target': 'analysis'}])
    path.write_text(json.dumps(canvas), encoding='utf-8')
    run = await reserve(service, ctx, transcript_node_id='words', transcript_ref=ref,
                        transcript_status='no_speech')
    stored = service.store.get(run['run_id'])
    assert stored['transcript_no_speech_ref'] == ref
    service._assert_transcript_dependency(canvas, stored, result={'transcript_ref': ref})
    canvas['nodes'][-1]['data']['status'] = 'completed'
    with pytest.raises(continuation.StaleReference):
        service._assert_transcript_dependency(canvas, stored, result={'transcript_ref': ref})


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['adopt', 'retry', 'receipt', 'recovery'])
async def test_bound_native_dependency_is_fenced_in_adoption_retry_receipt_and_recovery(service, tmp_path, monkeypatch, action):
    ctx, run, task, *_ = await bound_transcript_run(service, tmp_path, monkeypatch)
    payload = await service._save_analysis(ctx, run, result=task.result)
    run.update(result_fingerprint=continuation._node_fingerprint(service._node(payload, 'analysis')),
               completed_analysis_node_snapshot=service._node(payload, 'analysis'), status='analysis_ready')
    assert service.store.update_owned(run, service.owner_token)
    if action == 'adopt':
        new = await reserve(service, ctx, agent_id='adoption')
        token = await service.claim_submission(ctx=ctx, run_id=new['run_id'])
        stored = service.store.get(new['run_id'])
        stored['transcript_dependency'] = run['transcript_dependency']
        assert service.store.update_owned(stored, token)
    elif action == 'retry':
        run['status'] = 'resume_failed'
        assert service.store.update_owned(run, service.owner_token)
        service.store.release(run['run_id'], service.owner_token)
    before = change_transcript_dependency(service, ctx, 'words-analysis')
    with pytest.raises(continuation.StaleReference):
        if action == 'adopt':
            await service.adopt_saved_analysis(ctx=ctx, run_id=new['run_id'], submission_token=token)
        elif action == 'retry':
            await service.retry(ctx=ctx, run_id=run['run_id'])
        elif action == 'receipt':
            await service.read_submission_receipt(ctx=ctx, run_id=run['run_id'], canvas_id='canvas',
                source_node_id='source', agent_id='main', idempotency_key='request')
        else:
            await service._save_analysis(ctx, run, result=task.result)
    assert canvas_store.canvas_path(ctx.state_dir, 'canvas').read_bytes() == before


@pytest.mark.asyncio
async def test_native_transcript_projection_reads_artifacts_before_canvas_writer_lease(service, tmp_path, monkeypatch):
    from contextlib import contextmanager
    from novelvideo.freezone.remix.store import RemixArtifactStore
    ctx, run, task, *_ = await bound_transcript_run(service, tmp_path, monkeypatch)
    active = []
    class Mutex:
        @contextmanager
        def write_mutex(self, *args, **kwargs):
            with FileLockCanvasWriteMutex().write_mutex(*args, **kwargs) as guard:
                active.append(True)
                try: yield guard
                finally: active.pop()
    original = RemixArtifactStore.read
    def read(store, *args, **kwargs):
        assert not active, 'artifact reads must occur outside canvas writer leases'
        return original(store, *args, **kwargs)
    monkeypatch.setattr(canvas_store, 'get_canvas_write_mutex', lambda: Mutex())
    monkeypatch.setattr(RemixArtifactStore, 'read', read)
    payload = await service._save_analysis(ctx, run, result=task.result)
    run.update(result_fingerprint=continuation._node_fingerprint(service._node(payload, 'analysis')),
        completed_analysis_node_snapshot=service._node(payload, 'analysis'))
    await service._restore_saved_transcript(ctx, run)
    assert service.store.update_owned(run, service.owner_token)
    assert await service.read_submission_receipt(ctx=ctx, run_id=run['run_id'], canvas_id='canvas',
        source_node_id='source', agent_id='main', idempotency_key='request')


@pytest.mark.asyncio
async def test_native_bound_projection_preserves_selected_input_and_ignores_other_source_transcript(service, tmp_path, monkeypatch):
    from copy import deepcopy
    ctx, run, task, *_ = await bound_transcript_run(service, tmp_path, monkeypatch)
    payload = service._canvas(ctx, 'canvas')
    other = deepcopy(service._node(payload, 'input-words')); other['id'] = 'other-words'
    other['data']['artifactRef'] = {'artifactId': 'unrelated-old', 'revision': 1}
    payload['nodes'].insert(0, other)
    payload['edges'].append({'id': 'other-source', 'source': 'source', 'target': 'other-words'})
    canvas_store.canvas_path(ctx.state_dir, 'canvas').write_text(json.dumps(payload))
    saved = await service._save_analysis(ctx, run, result=task.result)
    assert service._node(saved, 'other-words') == other
    assert not any(e['source'] == 'other-words' and e['target'] == 'analysis' for e in saved['edges'])
    assert service._node(saved, 'analysis')['data']['referenceTranscriptRef'] == service._node(saved, 'input-words')['data']['artifactRef']


@pytest.mark.asyncio
async def test_legacy_native_projection_rejects_ambiguous_source_inputs_without_adding_edges(service, tmp_path, monkeypatch):
    from copy import deepcopy
    ctx, run, task, *_ = await bound_transcript_run(service, tmp_path, monkeypatch)
    run.pop('transcript_dependency')
    payload = service._canvas(ctx, 'canvas')
    payload['edges'] = [e for e in payload['edges'] if e['id'] != 'words-analysis']
    other = deepcopy(service._node(payload, 'input-words')); other['id'] = 'other-words'
    payload['nodes'].append(other)
    payload['edges'].append({'id': 'other-source', 'source': 'source', 'target': 'other-words'})
    canvas_store.canvas_path(ctx.state_dir, 'canvas').write_text(json.dumps(payload))
    run['edge_fingerprint'] = service._edges_fingerprint(payload, run)
    before = canvas_store.canvas_path(ctx.state_dir, 'canvas').read_bytes()
    with pytest.raises(continuation.StaleReference, match='多个'):
        await service._save_analysis(ctx, run, result=task.result)
    assert canvas_store.canvas_path(ctx.state_dir, 'canvas').read_bytes() == before
