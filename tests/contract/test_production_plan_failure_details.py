"""Current task diagnostics keep useful validation facts without model content."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from novelvideo.freezone.remix.production_plan_failures import (
    build_plan_failure_summary, format_plan_failure_notice, read_plan_failure_summary,
)


def test_failure_summary_is_task_bound_and_bounded() -> None:
    record = {
        "event": "production_plan.validation_failed", "error_code": "plan_validation_failed",
        "operation_id": "operation-1", "stage": "validating", "validation_issue_count": 25,
        "validation_issues": [{"code": "speech_source_timing_cycle", "path": "/placements/0/start",
                               "placement_id": "carrier", "turn_id": "line-1", "input_value": "secret"}] * 25,
    }
    summary = build_plan_failure_summary(record, task_id="task-1")
    assert summary["issue_count"] == 25
    assert len(summary["issues"]) == 20
    assert "input_value" not in str(summary)
    task = SimpleNamespace(task_id="task-1", task_type="remix_generate_production_plan_draft",
                           status="failed", error="plan_validation_failed",
                           metadata={"production_plan_failure": summary}, result=None)
    assert read_plan_failure_summary(task) == summary
    assert "carrier" in format_plan_failure_notice(summary)
    task.task_id = "task-2"
    assert read_plan_failure_summary(task) is None
    task.task_id = "task-1"
    task.status = "completed"
    assert read_plan_failure_summary(task) is None


def test_non_validation_and_old_tasks_have_no_fabricated_detail() -> None:
    assert build_plan_failure_summary({"event": "production_plan.runner_failed",
                                       "error_code": "model_request_timeout"}, task_id="task") is None
    task = SimpleNamespace(task_id="old", task_type="remix_generate_production_plan_draft",
                           status="failed", error="plan_validation_failed", metadata=None, result=None)
    assert read_plan_failure_summary(task) is None


@pytest.mark.asyncio
async def test_signed_single_request_failure_persists_specific_issue(
    tmp_path, monkeypatch, signed_inline_backend,
) -> None:
    from novelvideo.api.routes.remix import prepare_registered_action
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.ports import registry
    from novelvideo.ports.local.tasks import InMemoryCancellationStore
    from novelvideo.project_context import ProjectContext
    from novelvideo.task_state import TaskStateManager
    from test_production_audio_sources import planned_video_dialogue

    manager = TaskStateManager()
    monkeypatch.setattr(registry, "_PORTS", dict(registry._PORTS))
    registry.register_port("cancellation_store", InMemoryCancellationStore())
    monkeypatch.setattr("novelvideo.task_state._task_manager", manager)
    monkeypatch.setattr("novelvideo.ports.local.tasks.get_task_manager", lambda: manager)
    ctx = ProjectContext("p", "p", "user", "u", "alice", "u", "alice", (("user", "u"),),
                         "owner", "local", tmp_path, tmp_path, tmp_path, True)
    store = RemixArtifactStore(tmp_path, "p")
    plan = planned_video_dialogue()
    plan["placements"][0]["start"] = {"kind": "turn", "ref_id": "ask", "edge": "start"}
    calls = []

    async def generate(**kwargs):
        calls.append(kwargs)
        return json.dumps(plan, ensure_ascii=False)

    monkeypatch.setattr("novelvideo.freezone.remix.production_plan.generate_recipe_text", generate)
    prepared = prepare_registered_action(
        store, "generate_production_plan_draft", {"topic": plan["goal"]},
        idempotency_key="timing-cycle", node_revision=1,
    )
    operations = RemixOperationStore(tmp_path, "p")
    operation = operations.start(
        idempotency_key="timing-cycle", input_refs=prepared["input_refs"],
        producer_version=prepared["producer_version"],
        normalized_parameters=prepared["normalized_parameters"], node_revision=1,
    )
    queued = await signed_inline_backend().enqueue_project_task(
        ctx, product_surface="freezone", task_type=prepared["task_type"],
        queue_kind=prepared["queue_kind"], episode=0, scope=operation["operation_id"],
        payload={**prepared["task_payload"], "operation_id": operation["operation_id"]},
    )
    deadline = time.monotonic() + 5
    state = None
    while time.monotonic() < deadline:
        state = manager.get_task_for_project(ctx, prepared["task_type"], 0,
                                             scope=operation["operation_id"])
        if state and state.status in {"failed", "completed"}:
            break
        await asyncio.sleep(0.02)
    assert state is not None and state.status == "failed", state
    assert state.task_id == queued.task_state.task_id
    assert state.error == "plan_validation_failed"
    assert len(calls) == 1
    assert operations.get(operation["operation_id"])["output_refs"] == []
    persisted = TaskStateManager().get_task_for_project(
        ctx, prepared["task_type"], 0, scope=operation["operation_id"])
    failure = read_plan_failure_summary(persisted)
    assert failure is not None
    assert failure["operation_id"] == operation["operation_id"]
    assert failure["issues"][0] == {"code": "speech_source_timing_cycle",
                                   "path": "/placements/0/start",
                                   "placement_id": "demo-video", "turn_id": "ask"}
    from novelvideo.chat.reference_production_observer import production_event
    from novelvideo.freezone.workflow_runs import (
        bind_workflow_action_processing_operation, create_workflow_run, update_workflow_run,
    )

    workflow = create_workflow_run(
        project_dir=ctx.state_dir, project_id=ctx.project_id, canvas_id="canvas",
        actor_id=ctx.requester_user_id, actions=[{
            "node_id": "plan", "action": "generate_production_plan_draft",
            "payload": {"topic": plan["goal"]}, "node_revision": 1,
        }],
    )
    update_workflow_run(
        project_dir=ctx.state_dir, canvas_id="canvas", run_id=workflow["run_id"],
        action_updates=[{"node_id": "plan", "action": "generate_production_plan_draft",
                         "status": "running", "task_key": manager._task_key_for_state(persisted),
                         "job_id": operation["operation_id"]}],
    )
    bind_workflow_action_processing_operation(
        project_dir=ctx.state_dir, canvas_id="canvas", run_id=workflow["run_id"],
        node_id="plan", action="generate_production_plan_draft",
        operation_id=operation["operation_id"],
    )
    update_workflow_run(
        project_dir=ctx.state_dir, canvas_id="canvas", run_id=workflow["run_id"],
        status="failed", action_updates=[{
            "node_id": "plan", "action": "generate_production_plan_draft",
            "status": "failed", "error": "plan_validation_failed",
        }],
    )
    canvas = {"project_id": ctx.project_id,
              "nodes": [{"id": "analysis", "data": {}},
                        {"id": "plan", "data": {"remixRole": "production_plan"}}],
              "edges": [{"source": "analysis", "target": "plan"}]}
    commission = {"canvas_id": "canvas", "analysis_node_id": "analysis",
                  "requester_user_id": ctx.requester_user_id, "phase": "drafting_plan",
                  "commission_ref": {"artifact_id": "commission", "revision": 1}}
    observed = production_event(
        store, canvas, commission, {manager._task_key_for_state(persisted): persisted},
        workflow_state_dir=ctx.state_dir,
    )
    assert observed["event"] is None and observed["native_failure"] is True
    assert "demo-video" in observed["notice"] and "ask" in observed["notice"]
    assert "不得自动" in observed["notice"]
