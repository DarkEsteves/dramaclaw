from novelvideo.freezone.remix.operations import RemixOperationStore
from types import SimpleNamespace

import pytest
from datetime import datetime, timedelta, timezone


def test_native_processing_run_response_includes_durable_task_identity(tmp_path):
    from novelvideo.api.routes.freezone import _with_native_processing_task_ids

    operation_store = RemixOperationStore(tmp_path, "project")
    operation = operation_store.start(
        idempotency_key="plan-accepted", input_refs=[], producer_version="remix-process.v1",
        normalized_parameters={"action": "generate_production_plan_draft", "payload": {"topic": "coffee"}},
        node_revision=1, canvas_id="canvas", node_id="plan",
    )
    token = operation_store.claim_submission(operation["operation_id"], owner="test")
    assert token is not None
    operation_store.bind_task(operation["operation_id"], task_id="task-id",
                              owner="test", fencing_token=token)
    run = {"run_id": "run", "project_id": "project", "canvas_id": "canvas", "actions": [
        {"node_id": "plan", "action": "generate_production_plan_draft", "status": "running",
         "processing_operation_id": operation["operation_id"], "task_key": "task:plan"},
    ]}

    response = _with_native_processing_task_ids(run, output_dir=tmp_path, project_id="project")

    assert response["actions"][0]["task_id"] == "task-id"
    assert "task_id" not in run["actions"][0]


def test_native_workflow_acceptance_covers_all_actions_before_task_ids(tmp_path):
    from novelvideo.api.routes.freezone import _with_native_processing_task_ids

    store = RemixOperationStore(tmp_path, "project")
    actions = []
    for node_id in ("plan-a", "plan-b"):
        operation = store.start(
            idempotency_key=f"accepted-{node_id}", input_refs=[],
            producer_version="remix-process.v1",
            normalized_parameters={"action": "generate_production_plan_draft", "payload": {"topic": node_id}},
            node_revision=1, canvas_id="canvas", node_id=node_id,
        )
        actions.append({"node_id": node_id, "action": "generate_production_plan_draft",
                        "status": "running", "processing_operation_id": operation["operation_id"],
                        "task_key": f"task:{node_id}"})
    run = {"run_id": "run", "project_id": "project", "canvas_id": "canvas", "status": "running",
           "metadata": {"canvas_bridge_key": "approved-bridge", "native_completion_owner": "host",
                        "native_approved_actions": [{"node_id": item["node_id"], "action": item["action"]} for item in actions]},
           "actions": actions}

    response = _with_native_processing_task_ids(run, output_dir=tmp_path, project_id="project")

    assert response["acceptance"] == {"workflow_run_id": "run", "completion_owner": "host"}
    assert all("task_id" not in action for action in response["actions"])
    assert "acceptance" not in _with_native_processing_task_ids(
        {**run, "actions": [{**actions[0], "task_key": None}, actions[1]]},
        output_dir=tmp_path, project_id="project")
    assert "acceptance" not in _with_native_processing_task_ids(
        {**run, "metadata": {}}, output_dir=tmp_path, project_id="project")


def test_host_owned_native_run_is_not_interrupted_or_superseded_by_browser_lease(tmp_path):
    from novelvideo.freezone.workflow_runs import (
        WorkflowRunLeaseConflict, create_workflow_run, interrupt_stale_workflow_runs,
        read_workflow_run,
    )
    action = {"node_id": "plan", "action": "generate_production_plan_draft",
              "node_revision": 1, "payload": {"topic": "coffee"}}
    run = create_workflow_run(
        project_dir=tmp_path, project_id="project", canvas_id="canvas",
        actions=[action], actor_id="actor", runner_id="",
        metadata={"native_completion_owner": "host", "canvas_bridge_key": "bridge"},
    )
    assert interrupt_stale_workflow_runs(
        project_dir=tmp_path, canvas_id="canvas", stale_after_seconds=1,
        now=datetime.now(timezone.utc) + timedelta(minutes=10),
    ) == []
    assert read_workflow_run(project_dir=tmp_path, canvas_id="canvas", run_id=run["run_id"])["status"] == "running"
    with pytest.raises(WorkflowRunLeaseConflict):
        create_workflow_run(
            project_dir=tmp_path, project_id="project", canvas_id="canvas",
            actions=[action], actor_id="actor", runner_id="canvas-runner:other",
        )


@pytest.mark.anyio
async def test_host_recovery_dispatches_only_approved_native_runs(monkeypatch, tmp_path):
    from novelvideo.api.routes import freezone
    runs = [
        {"run_id": "host", "status": "running", "project_id": "project", "actor_id": "actor",
         "canvas_id": "canvas", "metadata": {"native_completion_owner": "host", "canvas_bridge_key": "bridge"}},
        {"run_id": "browser", "status": "running", "project_id": "project", "actor_id": "actor",
         "canvas_id": "canvas", "metadata": {}},
    ]
    monkeypatch.setattr(freezone, "list_workflow_runs", lambda **_kwargs: runs)
    dispatched = []
    async def dispatch(**kwargs):
        dispatched.append(kwargs["run"]["run_id"])
        return kwargs["run"]
    monkeypatch.setattr(freezone, "_dispatch_ready_remix_workflow_actions", dispatch)
    ctx = SimpleNamespace(project_id="project", requester_user_id="actor",
                          state_dir=tmp_path, output_dir=tmp_path)
    assert await freezone.resume_host_owned_native_workflows(ctx, canvas_id="canvas") == 1
    assert dispatched == ["host"]


@pytest.mark.anyio
async def test_saved_workflow_graph_is_confirmed_while_native_action_is_pending(monkeypatch, tmp_path):
    from novelvideo.api.routes import chat
    from novelvideo.freezone.workflow_drafts import (
        bind_workflow_draft_task, claim_workflow_draft_confirmation,
        create_workflow_draft, read_workflow_draft,
    )
    draft = create_workflow_draft(
        project_dir=tmp_path, project_id="project", canvas_id="canvas",
        intent={"skill_id": "reference-video-remix", "user_goal": "coffee"},
        compiled={"ok": True, "plan": {"nodes": [
            {"id": "production_plan_1", "stage": "production_plans", "data": {"remixNodeRevision": 1}},
        ], "edges": [], "phases": []}, "reference_binding": {
            "project_id": "project", "canvas_id": "canvas", "requester_user_id": "actor",
            "commission_id": "commission-1", "direction_revision": 1,
        }}, run_after_create=True,
    )
    claimed, error = claim_workflow_draft_confirmation(
        project_dir=tmp_path, canvas_id="canvas", draft_id=draft["draft_id"], revision=1, now=1000,
    )
    assert error is None and claimed is not None
    bind_workflow_draft_task(project_dir=tmp_path, canvas_id="canvas", draft_id=draft["draft_id"],
                             task_id="task-1", root_task_id="task-1")
    async def project_context(*_args):
        return SimpleNamespace(state_dir=tmp_path, requester_user_id="actor")
    monkeypatch.setattr(chat, "_project_context_for_scope", project_context)
    from novelvideo import task_state
    monkeypatch.setattr(task_state, "get_task_manager", lambda: SimpleNamespace(
        get_task_for_project=lambda *_args, **_kwargs: SimpleNamespace(task_id="task-1", status="running")))
    from novelvideo.freezone import canvas_store
    saved_canvas = {"project_id": "project", "canvas_id": "canvas", "revision": 1, "nodes": [
        {"id": "real-plan", "data": {"workflowInstanceId": draft["draft_id"],
                                     "workflowPlanNodeId": "production_plan_1", "remixNodeRevision": 1}},
    ]}
    monkeypatch.setattr(canvas_store, "read_canvas", lambda *_args: saved_canvas)
    payload = chat.CanvasCommandToolResultIn(
        bridge_key="bridge", project_id="project", canvas_id="canvas", canvas_apply_status="pending",
        applied=False, created_node_ids=["real-plan"], command_results=[
            {"type": "create_node", "status": "success", "createdNodeId": "real-plan"},
            {"type": "run_node_action", "status": "pending", "nodeId": "real-plan"},
        ],
    )
    await chat._record_workflow_draft_canvas_result(
        user={"username": "actor"}, payload=payload,
        draft_receipt={"draft_id": draft["draft_id"], "revision": 1, "task_id": "task-1"},
        resolved={"ok": False},
    )
    stored, _ = read_workflow_draft(project_dir=tmp_path, canvas_id="canvas", draft_id=draft["draft_id"])
    assert stored["status"] == "confirmed"


@pytest.mark.anyio
async def test_canvas_receipt_before_graph_save_keeps_same_confirmation_attempt(monkeypatch, tmp_path):
    from novelvideo.api.routes import chat
    from novelvideo.freezone import canvas_store
    from novelvideo.freezone.workflow_drafts import (
        bind_workflow_draft_task, claim_workflow_draft_confirmation,
        create_workflow_draft, read_workflow_draft, list_workflow_delivery_receipts,
    )
    from novelvideo import task_state

    draft = create_workflow_draft(
        project_dir=tmp_path, project_id="project", canvas_id="canvas",
        intent={"skill_id": "video-ad", "user_goal": "coffee"},
        compiled={"ok": True, "plan": {
            "nodes": [{"id": "plan"}], "edges": [], "phases": [],
        }}, run_after_create=True,
    )
    claim_workflow_draft_confirmation(
        project_dir=tmp_path, canvas_id="canvas", draft_id=draft["draft_id"],
        revision=1, now=1000,
    )
    bind_workflow_draft_task(
        project_dir=tmp_path, canvas_id="canvas", draft_id=draft["draft_id"],
        task_id="task-1", root_task_id="task-1",
    )

    async def project_context(*_args):
        return SimpleNamespace(state_dir=tmp_path, requester_user_id="actor")

    monkeypatch.setattr(chat, "_project_context_for_scope", project_context)
    monkeypatch.setattr(task_state, "get_task_manager", lambda: SimpleNamespace(
        get_task_for_project=lambda *_args, **_kwargs: SimpleNamespace(task_id="task-1", status="running")))
    saved_canvas = {"project_id": "project", "canvas_id": "canvas", "revision": 1,
                    "nodes": [], "edges": []}
    monkeypatch.setattr(canvas_store, "read_canvas", lambda *_args: saved_canvas)
    payload = chat.CanvasCommandToolResultIn(
        bridge_key="bridge", project_id="project", canvas_id="canvas",
        canvas_apply_status="pending", applied=False,
        command_results=[{"type": "create_node", "status": "success", "createdNodeId": "actual"},
                         {"type": "run_node_action", "status": "pending", "nodeId": "actual"}],
    )
    kwargs = {"user": {"username": "actor"}, "payload": payload,
              "draft_receipt": {"draft_id": draft["draft_id"], "revision": 1, "task_id": "task-1"},
              "resolved": {"ok": False}}
    await chat._record_workflow_draft_canvas_result(**kwargs)
    pending, _ = read_workflow_draft(project_dir=tmp_path, canvas_id="canvas", draft_id=draft["draft_id"])
    assert pending["status"] == "submitted"
    assert pending["confirmation_started_at"] == 1000
    from novelvideo.task_backend.runners import freezone as freezone_runner
    sleep_calls = 0

    async def save_after_wait(_seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 3:
            raise AssertionError("saved graph was not reconciled")
        saved_canvas["revision"] = 2
        saved_canvas["nodes"] = [{"id": "actual", "data": {
            "workflowInstanceId": draft["draft_id"], "workflowPlanNodeId": "plan"}}]

    monkeypatch.setattr(freezone_runner.asyncio, "sleep", save_after_wait)
    result = await freezone_runner._run_freezone_workflow_confirm_async({
        "__run_task_id": "task-1", "payload": {
            "canvas_id": "canvas", "draft_id": draft["draft_id"],
            "revision": 1, "plan_digest": draft["plan_digest"],
            "confirmation_started_at": 1000,
        },
    }, SimpleNamespace(state_dir=tmp_path))
    assert result["delivery_status"] == "canvas_applied"
    confirmed, _ = read_workflow_draft(project_dir=tmp_path, canvas_id="canvas", draft_id=draft["draft_id"])
    assert confirmed["status"] == "confirmed"
    assert [item["graph_state"] for item in list_workflow_delivery_receipts(
        project_dir=tmp_path, canvas_id="canvas", draft_id=draft["draft_id"]
    )] == ["pending", "delivered"]


def test_saved_workflow_graph_requires_planned_edges(tmp_path):
    from novelvideo.api.routes import chat
    draft = {"draft_id": "draft", "compiled": {"plan": {
        "nodes": [{"id": "plan"}],
        "external_inputs": [{"id": "analysis", "node_id": "source"}],
        "edges": [{"source": "analysis", "target": "plan", "link_type": "media_input_for"}],
    }}}
    payload = chat.CanvasCommandToolResultIn(
        bridge_key="bridge", project_id="project", canvas_id="canvas", canvas_apply_status="pending",
        command_results=[{"type": "create_node", "status": "success", "createdNodeId": "real-plan"},
                         {"type": "create_edge", "status": "success"}],
    )
    canvas = {"nodes": [{"id": "real-plan", "data": {"workflowInstanceId": "draft",
                                                  "workflowPlanNodeId": "plan"}},
                        {"id": "source", "data": {}}], "edges": []}
    assert not chat._saved_workflow_graph_matches_receipt(payload, draft, canvas)
    canvas["edges"] = [{"source": "source", "target": "real-plan", "data": {"link_type": "media_input_for"}}]
    assert chat._saved_workflow_graph_matches_receipt(payload, draft, canvas)
    canvas["edges"] = [{"source": "real-plan", "target": "source", "data": {"link_type": "media_input_for"}}]
    assert not chat._saved_workflow_graph_matches_receipt(payload, draft, canvas)


def test_binding_failure_does_not_commit_graph_confirmation(tmp_path):
    from novelvideo.freezone.workflow_drafts import (
        bind_workflow_draft_task, claim_workflow_draft_confirmation,
        create_workflow_draft, read_workflow_draft, read_native_plan_slot_bindings,
        settle_workflow_draft_delivery,
    )
    draft = create_workflow_draft(
        project_dir=tmp_path, project_id="project", canvas_id="canvas",
        intent={"skill_id": "reference-video-remix", "user_goal": "coffee"},
        compiled={"ok": True, "plan": {"nodes": [
            {"id": "production_plan_1", "stage": "production_plans",
             "data": {"remixNodeRevision": 2}},
        ], "edges": [], "phases": []}, "reference_binding": {
            "project_id": "project", "canvas_id": "canvas",
            "requester_user_id": "actor", "commission_id": "commission",
            "direction_revision": 1,
        }},
    )
    claim_workflow_draft_confirmation(project_dir=tmp_path, canvas_id="canvas",
                                      draft_id=draft["draft_id"], revision=1, now=1000)
    bind_workflow_draft_task(project_dir=tmp_path, canvas_id="canvas",
                             draft_id=draft["draft_id"], task_id="task-1", root_task_id="task-1")
    canvas = {"project_id": "project", "canvas_id": "canvas", "revision": 3,
              "nodes": [{"id": "actual", "data": {
                  "workflowInstanceId": draft["draft_id"],
                  "workflowPlanNodeId": "production_plan_1", "remixNodeRevision": 1}}]}
    with pytest.raises(ValueError, match="revision changed"):
        settle_workflow_draft_delivery(
            project_dir=tmp_path, canvas_id="canvas", draft_id=draft["draft_id"],
            task_id="task-1", revision=1, attempt=1000,
            proof={"state": "delivered"}, canvas=canvas,
        )
    stored, _ = read_workflow_draft(project_dir=tmp_path, canvas_id="canvas", draft_id=draft["draft_id"])
    assert stored["status"] == "confirming"
    assert read_native_plan_slot_bindings(project_dir=tmp_path, draft_id=draft["draft_id"]) == {}
