from types import SimpleNamespace

import pytest

from novelvideo.chat.reference_production_observer import ReferenceProductionObserver, production_event
from novelvideo.chat.reference_remix_continuation import ReferenceRemixStore, _active_plan_failure_instruction
from novelvideo.freezone.remix.store import RemixArtifactStore
from novelvideo.freezone.workflow_runs import create_workflow_run, update_workflow_run


def test_confirmed_native_draft_plan_result_advances_its_commission(tmp_path):
    from novelvideo.freezone.workflow_drafts import (
        bind_workflow_draft_task, claim_workflow_draft_confirmation,
        create_workflow_draft, finish_workflow_draft_confirmation,
    )

    store = RemixArtifactStore(tmp_path / "output", "project")
    plan = store.create("production_plan.v2", {
        "schema_version": 2, "goal": "coffee",
        "output": {"width": 1920, "height": 1080, "fps_num": 24, "fps_den": 1},
        "beats": [{"id": "beat", "purpose": "coffee opening", "duration_frames": 48}],
    }, [])
    commission = {
        "canvas_id": "canvas", "analysis_node_id": "analysis",
        "requester_user_id": "user", "direction_revision": 1,
        "direction_fingerprint": "confirmed-direction",
        "phase": "drafting_plan",
        "commission_ref": {"artifact_id": "current-commission", "revision": 2},
    }
    draft = create_workflow_draft(
        project_dir=tmp_path / "state", project_id="project", canvas_id="canvas",
        intent={"skill_id": "reference-video-remix", "user_goal": "coffee"},
        compiled={
            "ok": True, "skill_id": "reference-video-remix",
            "reference_binding": {
                "project_id": "project", "canvas_id": "canvas", "requester_user_id": "user",
                "commission_id": "current-commission", "analysis_node_id": "analysis",
                "direction_revision": 1, "direction_fingerprint": "confirmed-direction",
                "stage": "production_plans",
            },
            "plan": {"nodes": [{"id": "production_plan_1", "node_type": "scriptNode",
                                "data": {"processingAction": "generate_production_plan_draft"}}],
                     "edges": []},
        },
    )
    claimed, error = claim_workflow_draft_confirmation(
        project_dir=tmp_path / "state", canvas_id="canvas",
        draft_id=draft["draft_id"], revision=1,
    )
    assert error is None
    bind_workflow_draft_task(
        project_dir=tmp_path / "state", canvas_id="canvas",
        draft_id=draft["draft_id"], task_id="task", root_task_id="task",
        expected_confirmation_started_at=claimed["confirmation_started_at"],
    )
    finish_workflow_draft_confirmation(
        project_dir=tmp_path / "state", canvas_id="canvas",
        draft_id=draft["draft_id"], outcome="confirmed", expected_task_id="task",
    )
    canvas = {
        "nodes": [
            {"id": "analysis", "data": {}},
            {"id": "plan", "data": {
                "remixRole": "production_plan",
                "workflowInstanceId": draft["draft_id"],
                "workflowPlanNodeId": "production_plan_1",
                "productionPlanV2Ref": {"artifactId": plan["id"], "revision": plan["revision"]},
            }},
        ],
        "edges": [{"source": "analysis", "target": "plan"}],
    }
    observed = production_event(
        store, canvas, commission, workflow_state_dir=tmp_path / "state",
    )
    assert observed["event"] == {
        "type": "plan_ready",
        "plan_ref": {"artifact_id": plan["id"], "revision": 1, "project_id": "project"},
    }


@pytest.mark.parametrize("include_remix_role", [False, True])
def test_saved_plan_from_unbound_canvas_branch_does_not_advance_current_commission(tmp_path, include_remix_role):
    store = RemixArtifactStore(tmp_path / "output", "project")
    plan = store.create("production_plan.v2", {
        "schema_version": 2, "goal": "other branch",
        "output": {"width": 1920, "height": 1080, "fps_num": 24, "fps_den": 1},
        "beats": [{"id": "beat", "purpose": "other branch", "duration_frames": 48}],
    }, [])
    canvas = {
        "nodes": [
            {"id": "analysis", "data": {}},
            {"id": "plan", "data": {
                **({"remixRole": "production_plan"} if include_remix_role else {}),
                "productionPlanV2Ref": {"artifactId": plan["id"], "revision": plan["revision"]},
            }},
        ],
        "edges": [{"source": "analysis", "target": "plan"}],
    }
    commission = {
        "canvas_id": "canvas", "analysis_node_id": "analysis",
        "requester_user_id": "user", "direction_revision": 1,
        "direction_fingerprint": "confirmed-direction",
        "phase": "drafting_plan",
        "commission_ref": {"artifact_id": "current-commission", "revision": 2},
    }
    assert production_event(
        store, canvas, commission, workflow_state_dir=tmp_path / "state",
    ) is None


def test_failed_canvas_plan_generation_wakes_reference_continuation(tmp_path):
    store = RemixArtifactStore(tmp_path / "output", "project")
    canvas = {
        "nodes": [
            {"id": "analysis", "data": {}},
            {"id": "plan", "data": {"remixRole": "production_plan"}},
        ],
        "edges": [{"source": "analysis", "target": "plan"}],
    }
    commission = {
        "canvas_id": "canvas", "requester_user_id": "user", "analysis_node_id": "analysis",
        "phase": "drafting_plan", "commission_ref": {"artifact_id": "commission", "revision": 2},
    }
    run = create_workflow_run(
        project_dir=tmp_path / "state", project_id="project", canvas_id="canvas",
        actor_id="user", actions=[{"node_id": "plan", "action": "generate_production_plan_draft",
                                   "payload": {"topic": "coffee"}, "node_revision": 1}],
    )
    assert production_event(store, canvas, commission, workflow_state_dir=tmp_path / "state") is None
    update_workflow_run(project_dir=tmp_path / "state", canvas_id="canvas", run_id=run["run_id"],
                        action_updates=[{"node_id": "plan", "action": "generate_production_plan_draft",
                                         "status": "running", "task_key": "task:plan", "job_id": "job-1"}])
    from novelvideo.freezone.workflow_runs import bind_workflow_action_processing_operation
    bind_workflow_action_processing_operation(
        project_dir=tmp_path / "state", canvas_id="canvas", run_id=run["run_id"],
        node_id="plan", action="generate_production_plan_draft", operation_id="op-1",
    )
    update_workflow_run(project_dir=tmp_path / "state", canvas_id="canvas", run_id=run["run_id"],
                        status="failed", action_updates=[{"node_id": "plan", "action": "generate_production_plan_draft",
                                                         "status": "failed", "error": "invalid plan"}])
    observed = production_event(store, canvas, commission, workflow_state_dir=tmp_path / "state")
    assert observed["event"] is None
    assert observed["event_key"].startswith("production:")
    assert "方案生成失败" in observed["notice"]
    continuation = {"commission_event_receipts": [observed["event_key"]]}
    instruction = _active_plan_failure_instruction(
        commission, continuation, canvas, tmp_path / "state",
    )
    assert instruction is not None
    assert "作品方案生成失败" in instruction
    assert "请根据已保存方向起草作品方案" not in instruction

    # The task key identifies this workflow action; its task ID guards the
    # persisted validation detail against a later task on the same node.
    failure = {"task_id": "task-id", "operation_id": "op-1",
               "error_code": "plan_validation_failed", "issues": [{
                   "code": "speech_source_timing_cycle", "path": "/placements/0/start",
                   "placement_id": "carrier", "turn_id": "line-1"}]}
    task = SimpleNamespace(task_id="task-id", task_type="remix_generate_production_plan_draft",
                           project_id="project", scope="job-1", status="failed",
                           error="plan_validation_failed", metadata={"production_plan_failure": failure})
    tasks = {"task:plan": task}
    detailed = production_event(store, canvas, commission, tasks, workflow_state_dir=tmp_path / "state")
    assert detailed["event_key"] == observed["event_key"]
    assert "carrier" in detailed["notice"] and "line-1" in detailed["notice"]
    detailed_instruction = _active_plan_failure_instruction(
        commission, continuation, canvas, tmp_path / "state", tasks_by_key=tasks)
    assert "carrier" in detailed_instruction and "不得自动" in detailed_instruction
    task.scope = "another-job"
    assert "carrier" not in production_event(
        store, canvas, commission, tasks, workflow_state_dir=tmp_path / "state")["notice"]
    task.scope = "job-1"
    failure["operation_id"] = "another-operation"
    assert "carrier" not in production_event(
        store, canvas, commission, tasks, workflow_state_dir=tmp_path / "state")["notice"]
    failure["operation_id"] = "op-1"
    task.task_id = "later-task"
    assert "carrier" not in production_event(
        store, canvas, commission, tasks, workflow_state_dir=tmp_path / "state")["notice"]

    # A later retry owns the current node; its running status suppresses the
    # historical failure until it settles.
    create_workflow_run(
        project_dir=tmp_path / "state", project_id="project", canvas_id="canvas",
        actor_id="user", actions=[{"node_id": "plan", "action": "generate_production_plan_draft",
                                   "payload": {"topic": "coffee retry"}, "node_revision": 1}],
    )
    assert production_event(store, canvas, commission, workflow_state_dir=tmp_path / "state") is None
    assert _active_plan_failure_instruction(
        commission, continuation, canvas, tmp_path / "state",
    ) is None


@pytest.mark.asyncio
async def test_observer_reconciles_failed_plan_task_without_a_browser_get(tmp_path, monkeypatch):
    from novelvideo.chat import reference_production_observer as module
    from novelvideo.freezone.workflow_runs import reconcile_workflow_runs_with_tasks, read_workflow_run

    ctx = SimpleNamespace(project_id="project", requester_user_id="user",
                          output_dir=tmp_path / "output", state_dir=tmp_path / "state")
    canvas = {"nodes": [{"id": "analysis", "data": {}},
                        {"id": "plan", "data": {"remixRole": "production_plan"}}],
              "edges": [{"source": "analysis", "target": "plan"}]}
    commission = {"canvas_id": "canvas", "analysis_node_id": "analysis",
                  "requester_user_id": "user", "phase": "drafting_plan",
                  "commission_ref": {"artifact_id": "commission", "revision": 2}}
    workflow = create_workflow_run(
        project_dir=ctx.state_dir, project_id="project", canvas_id="canvas", actor_id="user",
        actions=[{"node_id": "plan", "action": "generate_production_plan_draft",
                  "node_revision": 1, "payload": {"topic": "coffee"}}],
    )
    update_workflow_run(project_dir=ctx.state_dir, canvas_id="canvas", run_id=workflow["run_id"],
                        action_updates=[{"node_id": "plan", "action": "generate_production_plan_draft",
                                         "status": "running", "task_key": "task:plan"}])
    monkeypatch.setattr(module, "read_reference_commission", lambda *_args, **_kwargs: commission)
    notices = []
    resumptions = []
    diagnostics = []
    monkeypatch.setattr(module, "log_production_plan_diagnostic", lambda event, **fields: diagnostics.append((event, fields)), raising=False)

    class Coordinator:
        async def _resolve_context(self, _run):
            return ctx

        def _assert_current(self, *_args, **_kwargs):
            return canvas

        async def _notify(self, _run, notice, _event_key):
            notices.append(notice)

        async def continue_commission(self, **kwargs):
            resumptions.append(kwargs)

    observer = ReferenceProductionObserver(Coordinator())

    async def reconcile(_ctx, _run):
        reconcile_workflow_runs_with_tasks(
            project_dir=ctx.state_dir, canvas_id="canvas",
            tasks_by_key={"task:plan": {"task_id": "task-id", "status": "failed",
                                        "result": None, "error": "Request timed out."}},
        )
        return {}

    observer._material_snapshot = reconcile
    continuation = {"run_id": "chat-run", "canvas_id": "canvas", "commission_ref": commission["commission_ref"],
                    "commission_phase": "drafting_plan", "status": "drafting_plan"}

    assert await observer.observe_run(continuation)
    assert read_workflow_run(project_dir=ctx.state_dir, canvas_id="canvas", run_id=workflow["run_id"])["status"] == "failed"
    assert len(notices) == len(resumptions) == 1
    assert [event for event, _fields in diagnostics] == [
        "production_plan.notification_started", "production_plan.notification_finished",
    ]


@pytest.mark.asyncio
async def test_direction_transition_waits_for_running_canvas_plan(tmp_path, monkeypatch):
    from novelvideo.chat import reference_production_observer as module

    ctx = SimpleNamespace(project_id="project", requester_user_id="user",
                          output_dir=tmp_path / "output", state_dir=tmp_path / "state")
    canvas = {
        "nodes": [{"id": "analysis", "data": {}},
                  {"id": "plan", "data": {"remixRole": "production_plan"}}],
        "edges": [{"source": "analysis", "target": "plan"}],
    }
    workflow = create_workflow_run(
        project_dir=ctx.state_dir, project_id="project", canvas_id="canvas", actor_id="user",
        actions=[{"node_id": "plan", "action": "generate_production_plan_draft",
                  "payload": {"topic": "coffee"}, "node_revision": 1}],
    )
    commission = {"canvas_id": "canvas", "requester_user_id": "user", "analysis_node_id": "analysis",
                  "phase": "drafting_plan", "commission_ref": {"artifact_id": "commission", "revision": 2}}
    monkeypatch.setattr(module, "read_reference_commission", lambda *_args, **_kwargs: commission)
    calls = []

    class Coordinator:
        async def _resolve_context(self, _run):
            return ctx

        def _assert_current(self, *_args, **_kwargs):
            return canvas

        async def continue_commission(self, **kwargs):
            calls.append(kwargs)

    run = {"run_id": "continuation", "canvas_id": "canvas", "commission_ref": commission["commission_ref"],
           "commission_phase": "awaiting_direction", "status": "awaiting_direction"}
    assert await ReferenceProductionObserver(Coordinator()).observe_run(run) is False
    assert calls == []
    from novelvideo.freezone import workflow_runs
    completed = {**workflow, 'status': 'completed', 'actions': [
        {**workflow['actions'][0], 'status': 'completed'}]}
    monkeypatch.setattr(workflow_runs, 'list_workflow_runs', lambda **_kwargs: [completed])
    # Task completion alone is not the canvas result; keep waiting for its
    # saved plan reference instead of sending another generic Agent turn.
    assert await ReferenceProductionObserver(Coordinator()).observe_run(run) is False
    assert calls == []


def test_only_premature_plan_failure_is_scanned_for_saved_plan_recovery(tmp_path):
    store = ReferenceRemixStore(tmp_path / 'continuations.db')
    for run_id, error in [('recoverable', 'Agent 未交付真实作品方案草稿，分析结果已保留'),
                          ('other', 'Agent session failed')]:
        store.create({'run_id': run_id, 'project_id': 'project', 'requester_user_id': 'user',
                      'canvas_id': 'canvas', 'agent_id': 'main', 'idempotency_key': run_id,
                      'request_fingerprint': run_id, 'status': 'resume_failed',
                      'commission_phase': 'drafting_plan', 'commission_ref': {'artifact_id': 'commission', 'revision': 2},
                      'error': error})
    assert [run['run_id'] for run in store.waiting_for_production()] == ['recoverable']


@pytest.mark.asyncio
async def test_saved_plan_recovers_premature_failure_without_another_generic_wake(tmp_path, monkeypatch):
    from novelvideo.chat import reference_production_observer as module

    ctx = SimpleNamespace(project_id='project', requester_user_id='user',
                          output_dir=tmp_path / 'output', state_dir=tmp_path / 'state')
    commission = {'canvas_id': 'canvas', 'requester_user_id': 'user', 'analysis_node_id': 'analysis',
                  'phase': 'drafting_plan', 'commission_ref': {'artifact_id': 'commission', 'revision': 2}}
    monkeypatch.setattr(module, 'read_reference_commission', lambda *_args, **_kwargs: commission)
    event = {'type': 'plan_ready', 'plan_ref': {'artifact_id': 'plan', 'revision': 1}}
    monkeypatch.setattr(module, 'production_event', lambda *_args, **_kwargs: None)
    calls = []

    class Coordinator:
        async def _resolve_context(self, _run):
            return ctx

        def _assert_current(self, *_args, **_kwargs):
            return {'nodes': [], 'edges': []}

        async def continue_commission(self, **kwargs):
            calls.append(kwargs)

    observer = ReferenceProductionObserver(Coordinator())
    run = {'run_id': 'continuation', 'canvas_id': 'canvas', 'commission_ref': commission['commission_ref'],
           'commission_phase': 'drafting_plan', 'status': 'resume_failed', 'error': 'Agent 未交付真实作品方案草稿，分析结果已保留'}
    assert await observer.observe_run(run) is False
    assert calls == []

    monkeypatch.setattr(module, 'production_event', lambda *_args, **_kwargs: {'event': event, 'event_key': 'saved:plan'})
    monkeypatch.setattr(observer, '_advance_observed', lambda *_args: None)
    assert await observer.observe_run(run) is True
    assert calls == [{'ctx': ctx, 'run_id': 'continuation', 'event_key': 'saved:plan'}]


def test_plan_settlement_conflict_is_scoped_and_stable(monkeypatch, tmp_path):
    from novelvideo.chat import reference_production_observer as module
    from novelvideo.freezone.remix import production_processing_settlement as settlement

    ctx = SimpleNamespace(project_id="project", requester_user_id="user", state_dir=tmp_path)
    commission = {"canvas_id": "canvas", "phase": "drafting_plan", "direction_revision": 2,
                  "direction_fingerprint": "direction", "commission_ref": {"artifact_id": "commission", "revision": 3}}
    monkeypatch.setattr(module, "list_workflow_runs", lambda **kwargs: [{
        "run_id": "run", "project_id": "project", "actor_id": "user", "actions": []}], raising=False)
    records = [{"commission_id": "commission", "direction_revision": 2,
                "direction_fingerprint": "direction", "workflow_run_id": "run", "node_id": "plan",
                "action": "generate_production_plan_draft", "operation_id": "operation",
                "result_ref": {"artifact_id": "artifact", "revision": 1},
                "reason": "target_changed_or_stopped", "recorded_at": "first"}]
    monkeypatch.setattr(settlement, "read_processing_settlement_conflicts", lambda *_args, **_kwargs: records)

    first = module._native_plan_conflict_observation(ctx, "canvas", commission)
    records[0]["recorded_at"] = "later"
    second = module._native_plan_conflict_observation(ctx, "canvas", commission)

    assert first["event_key"] == second["event_key"]
    assert first["native_failure"] is True
    assert first["notice"]
    assert module._native_plan_conflict_observation(ctx, "canvas", {
        **commission, "direction_revision": 3,
    }) is None


def test_latest_plan_generation_ignores_another_actor_on_shared_canvas(monkeypatch, tmp_path):
    from novelvideo.chat import reference_production_observer as module
    from novelvideo.freezone import workflow_runs

    canvas = {"project_id": "project", "nodes": [
        {"id": "analysis", "type": "videoStoryNode", "data": {}},
        {"id": "plan", "type": "scriptNode", "data": {"remixRole": "production_plan"}},
    ], "edges": [{"source": "analysis", "target": "plan"}]}
    commission = {"canvas_id": "canvas", "requester_user_id": "owner", "analysis_node_id": "analysis"}
    action = {"node_id": "plan", "action": "generate_production_plan_draft"}
    monkeypatch.setattr(module, "_bound_plan_node", lambda *_args: True)
    monkeypatch.setattr(workflow_runs, "list_workflow_runs", lambda **_kwargs: [
        {"run_id": "other", "project_id": "project", "actor_id": "other-user", "actions": [action]},
        {"run_id": "owner", "project_id": "project", "actor_id": "owner", "actions": [action]},
    ])

    latest = module._latest_plan_generation(canvas, commission, tmp_path)

    assert latest is not None and latest[0]["run_id"] == "owner"


@pytest.mark.parametrize("latest_status", ["failed", "running"])
def test_old_saved_plan_does_not_complete_a_newer_attempt(monkeypatch, tmp_path, latest_status):
    from novelvideo.chat import reference_production_observer as module

    store = RemixArtifactStore(tmp_path / "output", "project")
    plan = {"id": "old-plan", "revision": 1, "schema": "production_plan.v2", "content_hash": "old"}
    commission = {"canvas_id": "canvas", "phase": "drafting_plan",
                  "commission_ref": {"artifact_id": "commission", "revision": 2}}
    monkeypatch.setattr(module, "_canvas_refs", lambda *_args: [plan])
    monkeypatch.setattr(module, "_latest_plan_generation", lambda *_args: ({
        "run_id": "new-run", "status": latest_status,
    }, [{"node_id": "plan", "action": "generate_production_plan_draft",
         "status": latest_status, "result_ref": None}]))

    observed = module.production_event(store, {"nodes": [], "edges": []}, commission,
                                       workflow_state_dir=tmp_path / "state")

    if latest_status == "failed":
        assert observed is not None and observed.get("native_failure") is True
        assert observed.get("event") is None
    else:
        assert observed is None


@pytest.mark.asyncio
async def test_observer_recovers_native_speech_review_without_browser_get(monkeypatch, tmp_path):
    from novelvideo.chat.reference_production_observer import ReferenceProductionObserver
    from novelvideo.freezone import history, workflow_runs
    from novelvideo.freezone.remix import production_processing_settlement as settlement
    from novelvideo import task_state

    ctx = SimpleNamespace(project_id="project", requester_user_id="user",
                          state_dir=tmp_path / "state", output_dir=tmp_path / "output")
    manager = SimpleNamespace(list_tasks_for_project=lambda _ctx: [],
                              _task_key_for_state=lambda task: task.task_id)
    monkeypatch.setattr(task_state, "get_task_manager", lambda: manager)
    monkeypatch.setattr(history, "read_canvas_generation_history", lambda **_kwargs: [])
    monkeypatch.setattr(workflow_runs, "reconcile_workflow_runs_with_tasks", lambda **_kwargs: None)
    monkeypatch.setattr(workflow_runs, "list_workflow_runs", lambda **_kwargs: [{
        "project_id": "project", "actor_id": "user", "run_id": "speech-run",
        "actions": [{"action": "align_production_speech", "status": "blocked",
                     "artifact_status": "needs_review", "result_ref": {"artifact_id": "words", "revision": 1}}],
    }])
    settled = []
    monkeypatch.setattr(settlement, "settle_production_processing",
                        lambda _ctx, **kwargs: settled.append(kwargs))

    await ReferenceProductionObserver(SimpleNamespace())._material_snapshot(ctx, {"canvas_id": "canvas"})

    assert settled == [{"canvas_id": "canvas", "run_id": "speech-run"}]
