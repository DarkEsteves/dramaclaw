from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from novelvideo.api.routes import freezone, remix
from novelvideo.freezone import canvas_store, workflow_runs
from novelvideo.freezone.remix.operations import RemixOperationStore
from novelvideo.ports.local.canvas_mutex import FileLockCanvasWriteMutex
from novelvideo.project_context import ProjectContext
from novelvideo.freezone.remix.store import RemixArtifactStore
from novelvideo.task_backend.runners import remix as remix_runner
from novelvideo.task_identity import project_task_state_key


def context(root: Path, *, actor="actor", role="editor"):
    return ProjectContext("project", "demo", "user", "actor", "actor", actor, actor,
                          (("user", actor),), role, "local", root / "output", root / "state", root / "runtime", True)


def test_native_plan_runner_publishes_one_editable_artifact(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    draft = {"schema_version": 2, "goal": "黑咖啡短片",
             "output": {"width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1},
             "beats": [{"id": "opening", "purpose": "开场", "duration_frames": 90, "turns": []}],
             "resources": [], "placements": [], "provenance": {}}
    async def generate(**_kwargs):
        return draft
    monkeypatch.setattr(remix_runner, 'generate_production_plan_draft', generate)
    result = remix_runner.run_remix_generate_production_plan_draft({'payload': {'topic': '黑咖啡短片',
        'analysis_context': {'summary': '访谈反转'}}}, ctx)
    store = RemixArtifactStore(ctx.output_dir, ctx.project_id)
    saved = store.read(result['artifact_ref']['artifact_id'], result['artifact_ref']['revision'])
    assert saved['schema'] == 'production_plan.v2'
    assert saved['created_by'] == ctx.requester_user_id
    assert 'source_plan_ref' not in result
    assert 'source_plan_ref' not in saved['payload']['provenance']


@pytest.fixture
def dispatch(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    monkeypatch.setattr(canvas_store, "get_canvas_write_mutex", lambda: FileLockCanvasWriteMutex())
    monkeypatch.setattr(canvas_store, "read_canvas", lambda *_args: {
        "nodes": [{"id": "plan", "type": "scriptNode", "data": {"remixRole": "production_plan"}}],
    })
    def prepare(_store, action, payload, **kwargs):
        return {"input_refs": [], "producer_version": "remix-process.v1", "normalized_parameters": {"action": action, "payload": payload},
                "scope": "", "task_type": "remix_generate_production_plan_draft", "task_payload": payload,
                "queue_kind": "default", "input_media_hashes": []}
    monkeypatch.setattr(remix, "prepare_registered_action", prepare)
    calls = []
    async def enqueue(*_args, **kwargs):
        calls.append(kwargs)
        await asyncio.sleep(0)
        return SimpleNamespace(task_state=SimpleNamespace(task_id="paid-task"))
    monkeypatch.setattr(freezone, "get_task_backend", lambda: SimpleNamespace(enqueue_project_task=enqueue))
    run = workflow_runs.create_workflow_run(project_dir=ctx.state_dir, project_id=ctx.project_id, canvas_id="canvas", actor_id=ctx.requester_user_id,
        runner_id="canvas-runner:original", actions=[{"node_id": "plan", "action": "generate_production_plan_draft", "payload": {"topic": "复刻"}, "node_revision": 1}])
    return ctx, run, calls


@pytest.mark.asyncio
async def test_real_frontend_leased_run_dispatches_without_losing_runner(dispatch):
    ctx, run, calls = dispatch
    result = await freezone._dispatch_ready_remix_workflow_actions(ctx=ctx, state_dir=ctx.state_dir, output_dir=ctx.output_dir, run=run)
    assert result["runner_id"] == "canvas-runner:original"
    assert result["actions"][0]["status"] == "running"
    assert result["actions"][0]["job_id"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_concurrent_poll_dispatch_reuses_paid_operation(dispatch):
    ctx, run, calls = dispatch
    await asyncio.gather(*(freezone._dispatch_ready_remix_workflow_actions(ctx=ctx, state_dir=ctx.state_dir, output_dir=ctx.output_dir, run=run) for _ in range(2)))
    assert len(calls) == 1
    again = await freezone._dispatch_ready_remix_workflow_actions(ctx=ctx, state_dir=ctx.state_dir, output_dir=ctx.output_dir, run=run)
    assert again["actions"][0]["status"] == "running"
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("actor,role", [("another", "editor"), ("actor", "viewer")])
async def test_reading_viewer_or_another_actor_cannot_enqueue(dispatch, actor, role):
    ctx, run, calls = dispatch
    observer = context(ctx.state_dir.parent, actor=actor, role=role)
    result = await freezone._dispatch_ready_remix_workflow_actions(ctx=observer, state_dir=ctx.state_dir, output_dir=ctx.output_dir, run=run)
    assert result["actions"][0]["status"] == "pending"
    assert calls == []


@pytest.mark.asyncio
async def test_expired_runner_is_observed_without_lease_takeover_or_dispatch(dispatch):
    ctx, run, calls = dispatch
    with sqlite3.connect(workflow_runs.workflow_runs_db_path(ctx.state_dir)) as connection:
        connection.execute("UPDATE workflow_runs SET lease_expires_at=? WHERE run_id=?", ("2020-01-01T00:00:00Z", run["run_id"]))
    result = await freezone._dispatch_ready_remix_workflow_actions(ctx=ctx, state_dir=ctx.state_dir, output_dir=ctx.output_dir, run=run)
    assert result["runner_id"] == "canvas-runner:original"
    assert result["lease_expires_at"] == "2020-01-01T00:00:00Z"
    assert calls == []


@pytest.mark.asyncio
async def test_dispatch_does_not_overwrite_a_new_runner_after_enqueue(dispatch, monkeypatch):
    ctx, run, calls = dispatch
    async def takeover(*_args, **kwargs):
        calls.append(kwargs)
        expiry = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        with sqlite3.connect(workflow_runs.workflow_runs_db_path(ctx.state_dir)) as connection:
            connection.execute("UPDATE workflow_runs SET runner_id=?,lease_expires_at=? WHERE run_id=?", ("canvas-runner:new", expiry, run["run_id"]))
        return SimpleNamespace(task_state=SimpleNamespace(task_id="paid-task"))
    monkeypatch.setattr(freezone, "get_task_backend", lambda: SimpleNamespace(enqueue_project_task=takeover))
    result = await freezone._dispatch_ready_remix_workflow_actions(ctx=ctx, state_dir=ctx.state_dir, output_dir=ctx.output_dir, run=run)
    assert result["runner_id"] == "canvas-runner:new"
    operation_id = result["actions"][0]["processing_operation_id"]
    assert RemixOperationStore(ctx.output_dir, ctx.project_id).get(operation_id)["task_id"] == "paid-task"
    assert len(calls) == 1


def test_interrupted_pending_action_recovers_bound_paid_task_failure(dispatch):
    ctx, run, _calls = dispatch
    operations = RemixOperationStore(ctx.output_dir, ctx.project_id)
    operation = operations.start(idempotency_key="paid", input_refs=[], producer_version="remix-process.v1",
        normalized_parameters={"action": "generate_production_plan_draft", "payload": {"topic": "复刻"}},
        node_revision=1, canvas_id="canvas", node_id="plan")
    token = operations.claim_submission(operation["operation_id"], owner="submit")
    operations.bind_task(operation["operation_id"], task_id="paid-task", owner="submit", fencing_token=token)
    workflow_runs.bind_workflow_action_processing_operation(project_dir=ctx.state_dir, canvas_id="canvas", run_id=run["run_id"],
        node_id="plan", action="generate_production_plan_draft", operation_id=operation["operation_id"])
    with sqlite3.connect(workflow_runs.workflow_runs_db_path(ctx.state_dir)) as connection:
        connection.execute("UPDATE workflow_runs SET status='interrupted' WHERE run_id=?", (run["run_id"],))
    task_key = project_task_state_key("remix_generate_production_plan_draft", ctx.project_id, 0, scope=operation["operation_id"])
    workflow_runs.reconcile_workflow_runs_with_tasks(project_dir=ctx.state_dir, canvas_id="canvas", artifact_project_dir=ctx.output_dir,
        tasks_by_key={task_key: {"task_id": "paid-task", "status": "failed", "error": "invalid graphic enter"}})
    result = workflow_runs.read_workflow_run(project_dir=ctx.state_dir, canvas_id="canvas", run_id=run["run_id"])
    assert result["status"] == "failed"
    assert result["actions"][0]["task_key"] == task_key
    assert result["actions"][0]["processing_operation_id"] == operation["operation_id"]
    assert result["actions"][0]["error"] == "invalid graphic enter"


@pytest.mark.parametrize("mismatch", ["canvas", "payload", "task"])
def test_task_identity_repair_rejects_mismatched_operation_proof(dispatch, mismatch):
    ctx, run, _calls = dispatch
    operations = RemixOperationStore(ctx.output_dir, ctx.project_id)
    operation = operations.start(idempotency_key="paid-proof", input_refs=[], producer_version="remix-process.v1",
        normalized_parameters={"action": "generate_production_plan_draft", "payload": {"topic": "另一个目标" if mismatch == "payload" else "复刻"}},
        node_revision=1, canvas_id="another" if mismatch == "canvas" else "canvas", node_id="plan")
    token = operations.claim_submission(operation["operation_id"], owner="submit")
    operations.bind_task(operation["operation_id"], task_id="paid-task", owner="submit", fencing_token=token)
    item = {**run["actions"][0], "processing_operation_id": operation["operation_id"]}
    task_key = project_task_state_key("remix_generate_production_plan_draft", ctx.project_id, 0, scope=operation["operation_id"])
    recovered = workflow_runs._recover_processing_task_identity(item=item, run=run, artifact_project_dir=ctx.output_dir,
        tasks_by_key={task_key: {"task_id": "another-task" if mismatch == "task" else "paid-task", "status": "failed"}})
    assert recovered is None
