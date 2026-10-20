"""Project a validated production draft onto a signed task payload."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .store import RemixArtifactStore


def production_plan_task_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep host identity evidence in the operation, outside the task envelope."""
    result = deepcopy(payload)
    result.pop("requirements_provenance", None)
    return result


def restore_production_plan_inputs(
    store: RemixArtifactStore, payload: dict[str, Any], operation: dict[str, Any] | None,
    *, action: str = "generate_production_plan_draft",
) -> dict[str, Any]:
    """Verify signed business inputs against the persisted host snapshot."""
    from .production_requirements import validate_native_requirements

    if operation is None:
        if payload.get("operation_id") or "requirements_provenance" in payload:
            raise ValueError("task operation is unavailable")
        restored = deepcopy(payload)
        if action == "generate_production_plan_draft":
            validate_native_requirements(store, restored)
        return restored

    parameters = operation.get("normalized_parameters")
    snapshot = parameters.get("payload") if isinstance(parameters, dict) else None
    if (operation.get("operation_id") != payload.get("operation_id")
            or operation.get("project_id") != store.project_id
            or operation.get("producer_version") != "remix-process.v1"
            or not isinstance(snapshot, dict)
            or parameters.get("action") != action
            or operation.get("status") not in {"pending", "pending_reconciliation"}):
        raise ValueError("task operation does not match production draft")

    runtime_fields = {"operation_id", "node_revision", "workflow_run_id", "workflow_node_id"}
    business_inputs = {key: value for key, value in payload.items() if key not in runtime_fields}
    if (business_inputs != production_plan_task_payload(snapshot)
            or payload.get("node_revision") != operation.get("node_revision")
            or (payload.get("workflow_node_id") is not None
                and payload["workflow_node_id"] != operation.get("node_id"))):
        raise ValueError("task operation payload differs from saved inputs")

    restored = deepcopy(snapshot)
    if action == "generate_production_plan_draft":
        validate_native_requirements(store, restored)
    return {**restored, **{key: payload[key] for key in runtime_fields if key in payload}}


def read_arrangement_plan(store: RemixArtifactStore, ref: dict[str, Any]) -> dict[str, Any]:
    """Resolve the exact editable revision before spending on arrangement."""
    if (not isinstance(ref, dict) or set(ref) != {"artifact_id", "revision"}
            or not isinstance(ref.get("artifact_id"), str)
            or type(ref.get("revision")) is not int or ref["revision"] < 1):
        raise ValueError("arrangement requires an exact plan reference")
    artifact = store.read(ref["artifact_id"], ref["revision"])
    if artifact["schema"] != "production_plan.v2":
        raise ValueError("arrangement requires production_plan.v2")
    if store.read_latest(ref["artifact_id"])["revision"] != ref["revision"]:
        raise ValueError("production plan changed; use the latest saved revision")
    plan = artifact["payload"]
    if not plan.get("beats"):
        raise ValueError("save structure before arranging media")
    if plan.get("resources") or plan.get("speech_takes") or plan.get("speech_cues") or any(
        row.get("resource_id") for row in plan.get("placements", [])
    ):
        raise ValueError("production_plan_has_bound_media")
    return artifact


def validate_arrangement_node(store: RemixArtifactStore, canvas: dict[str, Any] | None,
                              node_id: str, payload: dict[str, Any]) -> None:
    """Admit only the current saved plan in the explicitly selected canvas node."""
    artifact = read_arrangement_plan(store, payload.get("plan_ref"))
    nodes = [node for node in (canvas or {}).get("nodes", []) if node.get("id") == node_id]
    if len(nodes) != 1:
        raise ValueError("arrangement node unavailable")
    node = nodes[0]
    data = node.get("data") or {}
    ref = data.get("productionPlanV2Ref") or {}
    if (node.get("type") != "scriptNode" or data.get("remixRole") != "production_plan"
            or data.get("productionPlanV2Dirty") or data.get("productionPlanV2Conflict")
            or ref.get("artifactId", ref.get("artifact_id")) != artifact["id"]
            or ref.get("revision") != artifact["revision"]
            or data.get("productionPlanV2Draft") != artifact["payload"]):
        raise ValueError("save the current plan before submitting arrangement")
