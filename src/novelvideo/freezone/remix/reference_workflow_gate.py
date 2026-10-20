"""Check stage ownership of workflows bound to a reference commission."""

from __future__ import annotations

from .reference_director import list_director_artifacts


_BLOCKED_PHASES = {
    "awaiting_direction": "reference_direction_pending",
    "drafting_plan": "reference_plan_must_use_native_stage",
    "awaiting_plan_review": "reference_plan_review_pending",
    "awaiting_plan_confirmation": "reference_plan_pending",
}


def _is_native_plan_stage(plan: dict, analysis_node_id: str) -> bool:
    """Recognize the native plan graph, including an inert composition node."""
    if plan.get("workflow_type") != "dynamic.reference-video-remix":
        return False
    sources = plan.get("external_inputs")
    if not isinstance(sources, list) or len(sources) != 1:
        return False
    source = sources[0]
    if (not isinstance(source, dict) or source.get("node_id") != analysis_node_id
            or source.get("media_kind") != "reference_analysis"):
        return False
    source_id = source.get("id")
    if not isinstance(source_id, str) or not source_id:
        return False
    nodes = plan.get("nodes")
    edges = plan.get("edges")
    if not isinstance(nodes, list) or not nodes or not isinstance(edges, list):
        return False
    plan_ids: set[str] = set()
    compose_ids: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str):
            return False
        data = node.get("data")
        if not isinstance(data, dict):
            return False
        if (node.get("node_type") == "scriptNode"
                and node.get("stage") == "production_plans"
                and data.get("remixRole") == "production_plan"
                and data.get("processingAction") == "generate_production_plan_draft"):
            plan_ids.add(node["id"])
        elif (node.get("node_type") == "videoComposeNode"
              and node.get("stage") == "composition"
              and data.get("processingAction") == "compile_production_plan"):
            compose_ids.add(node["id"])
        else:
            return False
    if (not plan_ids or len(plan_ids) + len(compose_ids) != len(nodes)
            or len(compose_ids) > len(plan_ids)):
        return False
    expected_phases = ["production_plans", *(["composition"] if compose_ids else [])]
    if plan.get("phases") != expected_phases:
        return False
    expected_edges = {
        (source_id, plan_id, "media_input_for") for plan_id in plan_ids
    } | {
        (plan_id, compose_id, "composition_input_for")
        for plan_id in plan_ids for compose_id in compose_ids
        if compose_id.rsplit("_", 1)[-1] == plan_id.rsplit("_", 1)[-1]
    }
    actual_edges = {
        (edge.get("source"), edge.get("target"), edge.get("link_type"))
        for edge in edges if isinstance(edge, dict)
    }
    if len(actual_edges) != len(edges) or actual_edges != expected_edges:
        return False
    policy = plan.get("execution_policy")
    return (isinstance(policy, dict)
            and policy.get("requires_user_confirmation") is True
            and policy.get("auto_create_nodes") is False
            and policy.get("auto_generate_content") is False)


def reference_workflow_stage_decision(
    store, *, canvas_id: str, requester_user_id: str, plan: dict,
    commission_id: str | None = None,
) -> tuple[str | None, dict | None]:
    """Resolve an eligible saved commission and its immutable direction basis."""
    skill = plan.get("skill") if isinstance(plan, dict) else None
    if not isinstance(skill, dict) or skill.get("id") != "reference-video-remix":
        return None, None
    sources = plan.get("external_inputs")
    if not isinstance(sources, list):
        return None, None
    analysis_ids = {source.get("node_id") for source in sources
                    if isinstance(source, dict) and source.get("media_kind") == "reference_analysis"
                    and isinstance(source.get("node_id"), str)}
    if not analysis_ids:
        return None, None
    matches = [
        commission for commission in list_director_artifacts(
            store, canvas_id, requester_user_id, limit=None,
        )["commissions"]
        if commission.get("analysis_node_id") in analysis_ids
        and (not commission_id
             or commission.get("commission_ref", {}).get("artifact_id") == commission_id)
    ]
    if not matches:
        return ("reference_commission_unavailable", None) if commission_id else (None, None)
    if len(matches) != 1:
        return "reference_commission_ambiguous", None
    commission = matches[0]
    phase = commission.get("phase")
    if phase == "drafting_plan" and _is_native_plan_stage(plan, commission["analysis_node_id"]):
        from .production_requirements import resolve_production_requirements
        resolved = resolve_production_requirements(store, commission=commission)
        for node in plan['nodes']:
            if node['data'].get('processingAction') == 'generate_production_plan_draft':
                node['data'].setdefault('processingPayload', {}).update(resolved)
        return None, {
            "project_id": store.project_id,
            "canvas_id": canvas_id,
            "requester_user_id": requester_user_id,
            "commission_id": commission["commission_ref"]["artifact_id"],
            "analysis_node_id": commission["analysis_node_id"],
            "direction_revision": commission["direction_revision"],
            "direction_fingerprint": commission["direction_fingerprint"],
            "stage": "production_plans",
        }
    return _BLOCKED_PHASES.get(phase), None


def reference_workflow_stage_problem(store, *, canvas_id: str,
                                     requester_user_id: str, plan: dict,
                                     commission_id: str | None = None) -> str | None:
    """Retain the compact status-only check used by existing callers."""
    return reference_workflow_stage_decision(
        store, canvas_id=canvas_id, requester_user_id=requester_user_id,
        plan=plan, commission_id=commission_id,
    )[0]
