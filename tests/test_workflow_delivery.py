from novelvideo.freezone.workflow_delivery import inspect_workflow_graph_delivery
from novelvideo.freezone.workflow_drafts import (
    list_workflow_delivery_receipts,
    record_workflow_delivery_receipt,
)


def _draft():
    return {"draft_id": "workflow_draft_delivery", "compiled": {"plan": {
        "nodes": [{"id": "production_plan_1"}, {"id": "composition_1"}],
        "external_inputs": [{"id": "analysis", "node_id": "source"}],
        "edges": [
            {"source": "analysis", "target": "production_plan_1", "link_type": "media_input_for"},
            {"source": "production_plan_1", "target": "composition_1", "link_type": "media_input_for"},
        ],
    }}}


def _receipt(action_status="pending"):
    return {"command_results": [
        {"type": "create_node", "status": "success", "createdNodeId": "plan"},
        {"type": "create_node", "status": "success", "createdNodeId": "compose"},
        {"type": "create_edge", "status": "success"},
        {"type": "create_edge", "status": "success"},
        {"type": "run_node_action", "status": action_status, "nodeId": "plan"},
    ]}


def _canvas():
    return {"revision": 8, "nodes": [
        {"id": "source", "data": {}},
        {"id": "plan", "data": {"workflowInstanceId": "workflow_draft_delivery",
                                "workflowPlanNodeId": "production_plan_1"}},
        {"id": "compose", "data": {"workflowInstanceId": "workflow_draft_delivery",
                                   "workflowPlanNodeId": "composition_1"}},
    ], "edges": [
        {"source": "source", "target": "plan", "data": {"link_type": "media_input_for"}},
        {"source": "plan", "target": "compose", "data": {"link_type": "media_input_for"}},
    ]}


def test_saved_graph_is_delivered_while_action_is_pending_or_failed():
    for action_status in ("pending", "error"):
        result = inspect_workflow_graph_delivery(
            receipt=_receipt(action_status), draft=_draft(), canvas=_canvas(),
        )
        assert result["state"] == "delivered"
        assert result["node_bindings"] == {
            "production_plan_1": "plan", "composition_1": "compose",
        }
        assert result["canvas_revision"] == 8


def test_graph_not_yet_visible_is_pending_not_rejected():
    canvas = _canvas()
    canvas["edges"] = canvas["edges"][:1]
    result = inspect_workflow_graph_delivery(
        receipt=_receipt(), draft=_draft(), canvas=canvas,
    )
    assert result["state"] == "pending"
    assert result["reason"] == "planned_edge_not_visible"
    assert result["mismatches"] == [{"source": "plan", "target": "compose",
                                    "link_type": "media_input_for"}]


def test_cancelled_receipt_releases_only_a_zero_side_effect_batch():
    cancelled = {"cancelled": True, "command_results": []}
    assert inspect_workflow_graph_delivery(
        receipt=cancelled, draft=_draft(), canvas={"nodes": [], "edges": []},
    )["state"] == "rejected"
    assert inspect_workflow_graph_delivery(
        receipt=cancelled, draft=_draft(), canvas=_canvas(),
    )["state"] == "pending"
    partial = {**_receipt(), "cancelled": True}
    assert inspect_workflow_graph_delivery(
        receipt=partial, draft=_draft(), canvas=_canvas(),
    )["state"] == "delivered"


def test_saved_wrong_edge_is_not_accepted():
    canvas = _canvas()
    canvas["edges"][1]["data"]["link_type"] = "wrong"
    result = inspect_workflow_graph_delivery(
        receipt=_receipt(), draft=_draft(), canvas=canvas,
    )
    assert result["state"] != "delivered"


def test_deleted_external_input_does_not_confirm_graph():
    canvas = _canvas()
    canvas["nodes"] = [node for node in canvas["nodes"] if node["id"] != "source"]
    proof = inspect_workflow_graph_delivery(receipt=_receipt(), draft=_draft(), canvas=canvas)
    assert proof["state"] != "delivered"
    assert proof["reason"] == "external_input_not_visible"


def test_first_delivery_receipt_is_preserved_when_later_result_arrives(tmp_path):
    base = dict(project_dir=tmp_path, canvas_id="canvas", draft_id="workflow_draft_delivery",
                revision=1, task_id="task", attempt=1000.0, bridge_key="bridge")
    first = {"command_results": _receipt()["command_results"],
             "secret": "private-value", "videoUrl": "https://signed.example/private"}
    record_workflow_delivery_receipt(**base, receipt=first,
        proof={"state": "pending", "reason": "planned_edge_not_visible", "canvas_revision": 7})
    record_workflow_delivery_receipt(**base, receipt=first,
        proof={"state": "pending", "reason": "planned_edge_not_visible", "canvas_revision": 7})
    record_workflow_delivery_receipt(**base, receipt=first,
        proof={"state": "delivered", "reason": "saved_graph_matches", "canvas_revision": 8})
    records = list_workflow_delivery_receipts(project_dir=tmp_path, canvas_id="canvas",
                                                draft_id="workflow_draft_delivery")
    assert [(item["graph_state"], item["canvas_revision"]) for item in records] == [
        ("pending", 7), ("delivered", 8),
    ]
    assert "private-value" not in str(records)
    assert "signed.example" not in str(records)
