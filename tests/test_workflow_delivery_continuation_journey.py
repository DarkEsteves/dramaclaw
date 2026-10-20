"""One approved graph is retained across receipt order and action failure."""

from types import SimpleNamespace

from novelvideo.freezone import canvas_store
from novelvideo.freezone.remix.native_plan_stage import read_native_plan_stage
from novelvideo.freezone.workflow_delivery import (
    inspect_workflow_graph_delivery, reconcile_workflow_draft_delivery,
)
from novelvideo.freezone.workflow_drafts import (
    bind_workflow_draft_task, claim_workflow_draft_confirmation,
    create_workflow_draft, list_workflow_delivery_receipts,
    read_native_plan_slot_bindings, read_workflow_draft,
    record_workflow_delivery_receipt, settle_workflow_draft_delivery,
)
from novelvideo.freezone.workflow_runs import (
    create_workflow_run, list_workflow_runs, update_workflow_run,
)


def test_canvas_save_after_receipt_keeps_one_graph_and_failed_action(tmp_path):
    binding = {
        "project_id": "project", "canvas_id": "canvas", "requester_user_id": "actor",
        "commission_id": "commission", "analysis_node_id": "analysis",
        "direction_revision": 1, "direction_fingerprint": "confirmed-direction",
        "stage": "production_plans",
    }
    compiled = {"ok": True, "skill_id": "reference-video-remix", "reference_binding": binding,
                "plan": {"nodes": [
                    {"id": "production_plan_1", "node_type": "scriptNode", "stage": "production_plans",
                     "data": {"remixRole": "production_plan"}},
                    {"id": "composition_1", "node_type": "videoComposeNode", "stage": "composition"},
                ], "external_inputs": [{"id": "analysis", "node_id": "analysis"}],
                    "edges": [
                        {"source": "analysis", "target": "production_plan_1", "link_type": "media_input_for"},
                        {"source": "production_plan_1", "target": "composition_1", "link_type": "media_input_for"},
                    ]}}
    draft = create_workflow_draft(project_dir=tmp_path, project_id="project", canvas_id="canvas",
                                  intent={"reference_commission_id": "commission"},
                                  compiled=compiled, run_after_create=True)
    claimed, error = claim_workflow_draft_confirmation(
        project_dir=tmp_path, canvas_id="canvas", draft_id=draft["draft_id"],
        revision=1, now=1_000,
    )
    assert error is None and claimed is not None
    bind_workflow_draft_task(project_dir=tmp_path, canvas_id="canvas", draft_id=draft["draft_id"],
                             task_id="confirmation-task", root_task_id="confirmation-task")
    receipt = {"command_results": [
        {"type": "create_node", "status": "success", "createdNodeId": "actual-plan"},
        {"type": "create_node", "status": "success", "createdNodeId": "actual-compose"},
        {"type": "create_edge", "status": "success"},
        {"type": "create_edge", "status": "success"},
        {"type": "run_node_action", "status": "pending", "nodeId": "actual-plan"},
    ]}
    old_canvas = {"project_id": "project", "canvas_id": "canvas", "revision": 1,
                  "nodes": [{"id": "analysis", "data": {}}], "edges": []}
    pending = inspect_workflow_graph_delivery(receipt=receipt, draft=draft, canvas=old_canvas)
    assert pending["state"] == "pending"
    record_workflow_delivery_receipt(project_dir=tmp_path, canvas_id="canvas",
        draft_id=draft["draft_id"], revision=1, task_id="confirmation-task", attempt=1_000,
        bridge_key="approved-bridge", receipt=receipt, proof=pending)
    settle_workflow_draft_delivery(project_dir=tmp_path, canvas_id="canvas",
        draft_id=draft["draft_id"], task_id="confirmation-task", revision=1,
        attempt=1_000, proof=pending, canvas=old_canvas)
    saved, _ = read_workflow_draft(project_dir=tmp_path, canvas_id="canvas", draft_id=draft["draft_id"])
    assert saved["status"] == "submitted"

    new_canvas = {**old_canvas, "revision": 2, "nodes": [*old_canvas["nodes"],
        {"id": "actual-plan", "data": {"workflowInstanceId": draft["draft_id"],
                                     "workflowPlanNodeId": "production_plan_1", "remixRole": "production_plan"}},
        {"id": "actual-compose", "data": {"workflowInstanceId": draft["draft_id"],
                                        "workflowPlanNodeId": "composition_1"}}],
        "edges": [
            {"source": "analysis", "target": "actual-plan", "data": {"link_type": "media_input_for"}},
            {"source": "actual-plan", "target": "actual-compose", "data": {"link_type": "media_input_for"}},
        ]}
    canvas_store.save_canvas(tmp_path, "canvas", base_revision=None,
                             build_payload=lambda _existing: new_canvas)
    from novelvideo.chat.reference_production_observer import _reconcile_native_workflow_delivery
    ctx = SimpleNamespace(state_dir=tmp_path, project_id="project", requester_user_id="actor")
    commission = {**binding, "phase": "drafting_plan",
                  "commission_ref": {"artifact_id": "commission", "revision": 1}}
    assert _reconcile_native_workflow_delivery(ctx, commission)
    assert reconcile_workflow_draft_delivery(project_dir=tmp_path, canvas_id="canvas",
        draft_id=draft["draft_id"], task_id="confirmation-task", attempt=1_000)
    saved, _ = read_workflow_draft(project_dir=tmp_path, canvas_id="canvas", draft_id=draft["draft_id"])
    assert saved["status"] == "confirmed"
    assert saved["confirmation_started_at"] == 1_000
    assert read_native_plan_slot_bindings(project_dir=tmp_path, draft_id=draft["draft_id"]) == {
        "production_plan_1": {"plan_node_id": "actual-plan", "compose_node_id": "actual-compose"},
    }
    assert [item["graph_state"] for item in list_workflow_delivery_receipts(
        project_dir=tmp_path, canvas_id="canvas", draft_id=draft["draft_id"]
    )] == ["pending", "delivered"]

    run = create_workflow_run(project_dir=tmp_path, project_id="project", canvas_id="canvas",
        actor_id="actor", actions=[{"node_id": "actual-plan",
        "action": "generate_production_plan_draft", "node_revision": 1,
        "payload": {"topic": "confirmed direction"}}])
    update_workflow_run(project_dir=tmp_path, canvas_id="canvas", run_id=run["run_id"],
        action_updates=[{"node_id": "actual-plan", "action": "generate_production_plan_draft",
                         "status": "failed", "error": "provider rejected draft"}])
    stage = read_native_plan_stage(ctx=ctx, canvas=new_canvas, commission=commission)
    assert stage["slots"][0]["state"] == "failed"
    assert len(list_workflow_runs(project_dir=tmp_path, canvas_id="canvas", limit=10)) == 1
