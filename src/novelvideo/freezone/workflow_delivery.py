"""Verify workflow graph delivery independently of subsequent node actions."""

from __future__ import annotations

from typing import Any


def inspect_workflow_graph_delivery(*, receipt: dict[str, Any], draft: dict[str, Any],
                                    canvas: dict[str, Any]) -> dict[str, Any]:
    """Prove the approved graph is saved, or explain which evidence is missing."""
    revision = canvas.get("revision") if isinstance(canvas, dict) else None

    def result(state: str, reason: str, bindings: dict[str, str] | None = None,
               mismatches: list[dict[str, str]] | None = None) -> dict[str, Any]:
        return {"state": state, "reason": reason,
                "node_bindings": bindings or {}, "canvas_revision": revision,
                "mismatches": mismatches or []}

    steps = receipt.get("command_results") or []
    has_side_effect_receipt = any(
        isinstance(step, dict) and step.get("status") in {"success", "pending", "running", "completed"}
        for step in steps
    )
    saved_nodes = canvas.get("nodes") if isinstance(canvas, dict) else None
    saved_workflow_nodes = isinstance(saved_nodes, list) and any(
        isinstance(node, dict) and isinstance(node.get("data"), dict)
        and node["data"].get("workflowInstanceId") == draft.get("draft_id")
        for node in saved_nodes
    )
    if ((receipt.get("cancelled") or receipt.get("canvas_apply_status") == "cancelled_by_user")
            and not has_side_effect_receipt and isinstance(saved_nodes, list)
            and not saved_workflow_nodes):
        return result("rejected", "cancelled_by_user")
    plan = ((draft.get("compiled") or {}).get("plan") or {})
    planned = plan.get("nodes")
    if not isinstance(planned, list) or not planned:
        return result("conflict", "planned_nodes_missing")
    created = {step.get("createdNodeId") for step in steps if isinstance(step, dict)
               and step.get("type") == "create_node" and step.get("status") == "success"
               and isinstance(step.get("createdNodeId"), str)}
    if not created:
        return result("pending", "created_node_receipt_missing")
    if not isinstance(saved_nodes, list):
        return result("pending", "saved_nodes_unavailable")
    bindings: dict[str, str] = {}
    for planned_node in planned:
        plan_id = planned_node.get("id") if isinstance(planned_node, dict) else None
        if not isinstance(plan_id, str) or not plan_id or plan_id in bindings:
            return result("conflict", "planned_node_identity_invalid")
        matches = [node for node in saved_nodes if isinstance(node, dict)
                   and isinstance(node.get("data"), dict)
                   and node["data"].get("workflowInstanceId") == draft.get("draft_id")
                   and node["data"].get("workflowPlanNodeId") == plan_id]
        if len(matches) > 1:
            return result("conflict", "duplicate_saved_plan_node", bindings)
        if not matches:
            return result("pending", "planned_node_not_visible", bindings)
        actual_id = matches[0].get("id")
        if not isinstance(actual_id, str) or actual_id not in created:
            return result("conflict", "saved_node_receipt_mismatch", bindings)
        bindings[plan_id] = actual_id
    for external in plan.get("external_inputs") or []:
        if not isinstance(external, dict):
            return result("conflict", "external_input_invalid", bindings)
        alias, node_id = external.get("id"), external.get("node_id")
        if (not isinstance(alias, str) or not alias or alias in bindings
                or not isinstance(node_id, str) or not node_id):
            return result("conflict", "external_input_invalid", bindings)
        if not any(isinstance(node, dict) and node.get("id") == node_id
                   for node in saved_nodes):
            return result("conflict", "external_input_not_visible", bindings)
        bindings[alias] = node_id
    saved_edges = canvas.get("edges") if isinstance(canvas, dict) else None
    for planned_edge in plan.get("edges") or []:
        if not isinstance(planned_edge, dict):
            return result("conflict", "planned_edge_invalid", bindings)
        source, target = bindings.get(planned_edge.get("source")), bindings.get(planned_edge.get("target"))
        if not source or not target:
            return result("conflict", "planned_edge_endpoint_invalid", bindings)
        link_type = planned_edge.get("link_type")
        matches = [edge for edge in saved_edges or [] if isinstance(edge, dict)
                   and edge.get("source") == source and edge.get("target") == target]
        mismatch = {"source": source, "target": target,
                    "link_type": link_type if isinstance(link_type, str) else ""}
        if not matches:
            return result("pending", "planned_edge_not_visible", bindings, [mismatch])
        if link_type and not any(isinstance(edge.get("data"), dict)
                                 and edge["data"].get("link_type") == link_type
                                 for edge in matches):
            return result("conflict", "planned_edge_link_type_changed", bindings, [mismatch])
    return result("delivered", "saved_graph_matches", {node["id"]: bindings[node["id"]]
                   for node in planned})


def reconcile_workflow_draft_delivery(*, project_dir: Any, canvas_id: str,
                                      draft_id: str, task_id: str,
                                      attempt: float) -> bool:
    """Recover a saved graph when its first authenticated receipt preceded storage."""
    from novelvideo.freezone import canvas_store
    from novelvideo.freezone.workflow_drafts import (
        list_workflow_delivery_receipts, read_workflow_draft,
        record_workflow_delivery_receipt, settle_workflow_draft_delivery,
    )

    draft, _ = read_workflow_draft(project_dir=project_dir, canvas_id=canvas_id,
                                   draft_id=draft_id)
    if not draft or draft.get("task_id") != task_id or draft.get("confirmation_started_at") != attempt:
        return False
    if draft["status"] == "confirmed":
        return True
    if draft["status"] not in {"confirming", "submitted"}:
        return False
    receipts = list_workflow_delivery_receipts(project_dir=project_dir, canvas_id=canvas_id,
                                                draft_id=draft_id)
    receipt = next((item for item in reversed(receipts)
                    if item.get("task_id") == task_id and item.get("attempt") == attempt
                    and any(step.get("type") == "create_node" and step.get("status") == "success"
                            for step in item.get("commands", []))), None)
    if receipt is None:
        return False
    canvas = canvas_store.read_canvas(project_dir, canvas_id)
    proof = inspect_workflow_graph_delivery(
        receipt={"command_results": receipt["commands"]}, draft=draft,
        canvas=canvas or {},
    )
    if proof["state"] != "delivered":
        return False
    record_workflow_delivery_receipt(
        project_dir=project_dir, canvas_id=canvas_id, draft_id=draft_id,
        revision=draft["revision"], task_id=task_id, attempt=attempt,
        bridge_key=receipt["bridge_key"],
        receipt={"command_results": receipt["commands"]}, proof=proof,
    )
    settle_workflow_draft_delivery(
        project_dir=project_dir, canvas_id=canvas_id, draft_id=draft_id,
        task_id=task_id, revision=draft["revision"], attempt=attempt,
        proof=proof, canvas=canvas,
    )
    return True
