from types import SimpleNamespace

import pytest

from novelvideo.api.routes import chat
from novelvideo.freezone.remix.operations import RemixOperationStore


def test_native_receipt_requires_matching_persisted_task(monkeypatch, tmp_path):
    ctx = SimpleNamespace(project_id="project", requester_user_id="actor", state_dir=tmp_path, output_dir=tmp_path)
    store = RemixOperationStore(tmp_path, "project")
    operation = store.start(
        idempotency_key="accepted-receipt", input_refs=[], producer_version="remix-process.v1",
        normalized_parameters={"action": "generate_production_plan_draft", "payload": {"topic": "coffee"}},
        node_revision=1, canvas_id="canvas", node_id="plan",
    )
    token = store.claim_submission(operation["operation_id"], owner="test")
    store.bind_task(operation["operation_id"], task_id="task-real", owner="test", fencing_token=token)
    from novelvideo.freezone import workflow_runs
    monkeypatch.setattr(workflow_runs, "read_workflow_run", lambda **kwargs: {
        "project_id": "project", "canvas_id": "canvas", "actor_id": "actor", "run_id": "run",
        "metadata": {"canvas_bridge_key": "bridge"},
        "actions": [{"node_id": "plan", "action": "generate_production_plan_draft", "status": "running",
                     "processing_operation_id": operation["operation_id"], "task_key": "task-key"}],
    })
    pending = {"envelope": {"commands": [{"type": "run_node_action", "node_id": "plan",
                                           "action": "generate_production_plan_draft"}]}}
    valid = chat.CanvasCommandToolResultIn(
        bridge_key="bridge", project_id="project", canvas_id="canvas", canvas_apply_status="accepted",
        workflow_run_id="run", operation_id=operation["operation_id"], task_id="task-real",
        node_id="plan", action="generate_production_plan_draft", completion_owner="host", applied=True,
    )

    command = pending["envelope"]["commands"][0]
    assert chat._validate_native_processing_receipt(ctx, valid, command)["task_id"] == "task-real"
    with pytest.raises(Exception):
        chat._validate_native_processing_receipt(ctx, valid.model_copy(update={"task_id": "task-fake"}), command)
    with pytest.raises(Exception):
        chat._validate_native_processing_receipt(ctx, valid.model_copy(update={"workflow_run_id": None}), command)
    with pytest.raises(Exception):
        chat._validate_native_processing_receipt(ctx, valid.model_copy(update={"bridge_key": "different"}), command)


def test_native_workflow_receipt_validates_the_entire_approved_batch(monkeypatch, tmp_path):
    ctx = SimpleNamespace(project_id="project", requester_user_id="actor", state_dir=tmp_path, output_dir=tmp_path)
    store = RemixOperationStore(tmp_path, "project")
    actions = []
    for node_id in ("plan-a", "plan-b"):
        operation = store.start(
            idempotency_key=f"batch-{node_id}", input_refs=[], producer_version="remix-process.v1",
            normalized_parameters={"action": "generate_production_plan_draft", "payload": {"topic": node_id}},
            node_revision=1, canvas_id="canvas", node_id=node_id,
        )
        actions.append({"node_id": node_id, "action": "generate_production_plan_draft", "status": "running",
                        "node_revision": 1, "payload": {"topic": node_id},
                        "processing_operation_id": operation["operation_id"], "task_key": f"task:{node_id}"})
    from novelvideo.freezone import workflow_runs
    run = {"project_id": "project", "canvas_id": "canvas", "actor_id": "actor", "run_id": "run",
           "status": "running", "metadata": {"canvas_bridge_key": "bridge", "native_completion_owner": "host",
               "native_approved_actions": [{"node_id": item["node_id"], "action": item["action"]} for item in actions]},
           "actions": actions}
    monkeypatch.setattr(workflow_runs, "read_workflow_run", lambda **kwargs: run)
    receipt = chat.CanvasCommandToolResultIn(
        bridge_key="bridge", project_id="project", canvas_id="canvas", canvas_apply_status="accepted",
        workflow_run_id="run", completion_owner="host", applied=True,
    )
    commands = [{"type": "run_workflow", "node_ids": ["plan-a", "plan-b"]}]
    assert chat._validate_native_workflow_receipt(ctx, receipt, commands)["workflow_run_id"] == "run"
    assert chat._validate_native_workflow_receipt(ctx, receipt, commands, claimed_actions=[
        {"nodeId": "plan-a", "action": "generate_production_plan_draft"},
        {"nodeId": "plan-b", "action": "generate_production_plan_draft"},
    ])["workflow_run_id"] == "run"
    with pytest.raises(Exception):
        chat._validate_native_workflow_receipt(ctx, receipt, commands, claimed_actions=[{"node_id": "plan-a", "action": "generate_production_plan_draft"}])
    with pytest.raises(Exception):
        chat._validate_native_workflow_receipt(ctx, receipt.model_copy(update={"bridge_key": "other"}), commands)
    run["actions"] = [{**actions[0], "payload": {"topic": "changed"}}, actions[1]]
    with pytest.raises(Exception):
        chat._validate_native_workflow_receipt(ctx, receipt, commands)


def test_native_workflow_receipt_uses_durable_bridge_identity(monkeypatch, tmp_path):
    from novelvideo.freezone import workflow_runs
    run = {"project_id": "project", "canvas_id": "canvas", "actor_id": "actor", "run_id": "run",
           "status": "running", "metadata": {"canvas_bridge_key": "bridge", "native_completion_owner": "host",
               "native_approved_actions": [{"node_id": "plan", "action": "generate_production_plan_draft"}]},
           "actions": [{"node_id": "plan", "action": "generate_production_plan_draft", "status": "running",
                        "processing_operation_id": "operation", "task_key": "task"}]}
    monkeypatch.setattr(workflow_runs, "read_workflow_run", lambda **kwargs: run)
    ctx = SimpleNamespace(project_id="project", requester_user_id="actor", state_dir=tmp_path, output_dir=tmp_path)
    receipt = chat.CanvasCommandToolResultIn(
        bridge_key="bridge.executor.new-owner", project_id="project", canvas_id="canvas",
        canvas_apply_status="accepted", workflow_run_id="run", completion_owner="host", applied=True,
    )
    monkeypatch.setattr(RemixOperationStore, "get", lambda self, _id: {
        "canvas_id": "canvas", "node_id": "plan", "status": "running",
        "normalized_parameters": {"action": "generate_production_plan_draft"},
    })
    assert chat._validate_native_workflow_receipt(ctx, receipt, [
        {"type": "run_node_action", "node_id": "plan", "action": "generate_production_plan_draft"},
    ])["workflow_run_id"] == "run"


def test_deferred_stage_is_not_claimed_by_native_run_receipt():
    steps = [
        {"type": "run_node_action", "status": "pending", "nodeId": "plan",
         "action": "generate_production_plan_draft", "output": {"reason": "native_action_accepted"}},
        {"type": "run_node_action", "status": "pending", "nodeId": "compose",
         "action": "compile_production_plan", "output": {"reason": "workflow_stage_deferred"}},
    ]
    assert chat._claimed_native_run_actions(steps) == [steps[0]]


def test_approved_native_graph_alias_resolves_only_the_saved_node():
    commands = [
        {"type": "create_node", "client_id": "planned", "data": {
            "workflowInstanceId": "workflow_draft_1", "workflowPlanNodeId": "production_plan_1"}},
        {"type": "run_workflow", "node_ids": ["planned"]},
    ]
    canvas = {"nodes": [
        {"id": "actual", "data": {"workflowInstanceId": "workflow_draft_1",
                                    "workflowPlanNodeId": "production_plan_1"}},
        {"id": "other", "data": {"workflowInstanceId": "workflow_draft_2",
                                   "workflowPlanNodeId": "production_plan_1"}},
    ]}
    assert chat._bridge_approves_native_action(commands, canvas, node_id="actual", action="generate_production_plan_draft")
    assert not chat._bridge_approves_native_action(commands, canvas, node_id="other", action="generate_production_plan_draft")


def test_mixed_workflow_receipt_resolves_approved_run_workflow_alias():
    commands = [
        {"type": "create_node", "client_id": "plan-alias", "data": {
            "workflowInstanceId": "draft", "workflowPlanNodeId": "plan"}},
        {"type": "run_workflow", "node_ids": ["plan-alias", "ordinary"]},
    ]
    canvas = {"nodes": [
        {"id": "actual-plan", "data": {"workflowInstanceId": "draft", "workflowPlanNodeId": "plan"}},
        {"id": "ordinary", "data": {}},
    ]}
    assert chat._bridge_approves_native_action(
        commands, canvas, node_id="actual-plan", action="generate_production_plan_draft")
    assert chat._approved_native_processing_receipt_command(
        commands, canvas, node_id="actual-plan", action="generate_production_plan_draft") == {
            "type": "run_node_action", "node_id": "actual-plan", "action": "generate_production_plan_draft",
        }
    assert chat._approved_native_processing_receipt_command(
        commands, canvas, node_id="other", action="generate_production_plan_draft") is None
