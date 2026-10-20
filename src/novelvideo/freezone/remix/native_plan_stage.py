"""Project the current native plan stage from durable draft and canvas evidence."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from novelvideo.freezone.workflow_drafts import (
    list_native_plan_drafts,
    read_native_plan_claim,
    read_native_plan_slot_bindings,
    recover_native_plan_claim,
)
from novelvideo.task_state import get_task_manager

logger = logging.getLogger(__name__)


def _binding(ctx: Any, commission: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_id": ctx.project_id,
        "canvas_id": commission["canvas_id"],
        "requester_user_id": commission["requester_user_id"],
        "commission_id": commission["commission_ref"]["artifact_id"],
        "analysis_node_id": commission["analysis_node_id"],
        "direction_revision": commission["direction_revision"],
        "direction_fingerprint": commission["direction_fingerprint"],
        "stage": "production_plans",
    }


def _next(kind: str, **extra: Any) -> dict[str, Any]:
    owner = "user" if kind == "await_user_confirmation" else "agent"
    if kind == "await_completion_event":
        owner = "system"
    return {"owner": owner, "kind": kind, **extra}


def _candidate_ids(canvas: dict[str, Any], draft_id: str, slot_id: str) -> list[str]:
    return sorted({str(node.get("id")) for node in canvas.get("nodes", [])
                   if isinstance(node, dict) and isinstance(node.get("data"), dict)
                   and node["data"].get("workflowInstanceId") == draft_id
                   and node["data"].get("workflowPlanNodeId") == slot_id
                   and isinstance(node.get("id"), str) and node.get("id")})


def _artifact_ref(value: Any) -> tuple[str, int] | None:
    if not isinstance(value, dict):
        return None
    artifact_id = value.get("artifactId", value.get("artifact_id"))
    revision = value.get("revision")
    if not isinstance(artifact_id, str) or not artifact_id or type(revision) is not int or revision < 1:
        return None
    return artifact_id, revision


def _settlement_conflict(ctx: Any, *, canvas_id: str, run: dict[str, Any],
                         action: dict[str, Any], binding: dict[str, Any]) -> bool:
    from novelvideo.freezone.remix.production_processing_settlement import read_processing_settlement_conflicts

    return any(
        record.get("commission_id") == binding["commission_id"]
        and record.get("direction_revision") == binding["direction_revision"]
        and record.get("direction_fingerprint") == binding["direction_fingerprint"]
        and record.get("node_id") == action.get("node_id")
        and record.get("action") == action.get("action")
        and record.get("operation_id") == action.get("processing_operation_id")
        and record.get("task_key") == action.get("task_key")
        and record.get("result_ref") == action.get("result_ref")
        for record in read_processing_settlement_conflicts(
            ctx, canvas_id=canvas_id, run_id=run["run_id"])
    )


def _bound_task_id(ctx: Any, canvas_id: str, node_id: str,
                   execution: tuple[dict[str, Any], dict[str, Any]] | None) -> str | None:
    if not execution or not getattr(ctx, "output_dir", None):
        return None
    operation_id = execution[1].get("processing_operation_id")
    if not operation_id:
        return None
    from novelvideo.freezone.remix.operations import RemixOperationStore

    try:
        operation = RemixOperationStore(ctx.output_dir, ctx.project_id).get(operation_id)
    except (ValueError, FileNotFoundError):
        return None
    if operation.get("canvas_id") != canvas_id or operation.get("node_id") != node_id:
        return None
    task_id = operation.get("task_id")
    return task_id if isinstance(task_id, str) and task_id else None


def _bound_task_status(ctx: Any, task_id: str | None,
                       execution: tuple[dict[str, Any], dict[str, Any]] | None) -> str | None:
    if not task_id or not execution or not getattr(ctx, "is_home_node", False):
        return None
    operation_id = execution[1].get("processing_operation_id")
    if not operation_id:
        return None
    task = get_task_manager().get_task_for_project(
        ctx, "remix_" + execution[1].get("action", "generate_production_plan_draft"), 0, scope=operation_id,
    )
    return task.status if task is not None and task.task_id == task_id else None


def _confirmation_task_status(ctx: Any, draft: dict[str, Any]) -> str | None:
    task_id = draft.get("task_id")
    attempt = draft.get("confirmation_started_at")
    if not task_id or attempt is None:
        return None
    task = get_task_manager().get_task_for_project(
        ctx, "freezone_workflow_confirm", 0,
        scope=f"{draft['canvas_id']}:{draft['draft_id']}:{draft['revision']}:{attempt}",
    )
    return task.status if task is not None and task.task_id == task_id else None


def read_native_plan_stage(*, ctx: Any, canvas: dict[str, Any],
                           commission: dict[str, Any],
                           artifact_reader: Any | None = None) -> dict[str, Any]:
    """Read exact plan slots for this approved production branch.

    Canvas selection, node labels and creation order are deliberately ignored.
    The draft claim identifies the workflow; compiled node IDs identify slots;
    the saved canvas supplies actual node IDs only after confirmation.
    """
    binding = _binding(ctx, commission)
    if commission["phase"] != "drafting_plan":
        return {"state": "other_phase", "slots": [], "next_action": commission.get("next_action")}
    if ctx.requester_user_id != binding["requester_user_id"]:
        raise PermissionError("native plan actor differs from commission")
    state_dir = Path(ctx.state_dir)
    drafts = list_native_plan_drafts(project_dir=state_dir, canvas_id=binding["canvas_id"],
                                     binding=binding)
    claimed = read_native_plan_claim(
        project_dir=state_dir, project_id=ctx.project_id, canvas_id=binding["canvas_id"],
        requester_user_id=binding["requester_user_id"], commission_id=binding["commission_id"],
        direction_revision=binding["direction_revision"],
    )
    if claimed and (claimed.get("compiled") or {}).get("reference_binding") != binding:
        return {"state": "binding_conflict", "slots": [], "candidate_node_ids": [],
                "next_action": _next("resolve_binding_conflict", candidate_node_ids=[])}
    if len(drafts) > 1:
        candidates = sorted({node_id for draft in drafts for node in (draft["compiled"].get("plan") or {}).get("nodes", [])
                             if isinstance(node, dict) and node.get("stage") == "production_plans"
                             for node_id in _candidate_ids(canvas, draft["draft_id"], node.get("id", ""))})
        return {"state": "binding_conflict", "slots": [], "candidate_node_ids": candidates,
                "next_action": _next("resolve_binding_conflict", candidate_node_ids=candidates)}
    draft = claimed or (drafts[0] if drafts else None)
    if draft and not claimed:
        draft = recover_native_plan_claim(project_dir=state_dir, canvas_id=binding["canvas_id"],
                                          draft_id=draft["draft_id"])
        logger.warning("native_plan.binding_recovered project_id=%s canvas_id=%s commission_id=%s draft_id=%s",
                       ctx.project_id, binding["canvas_id"], binding["commission_id"], draft["draft_id"])
    if not draft:
        action = _next("prepare_production_nodes", tool="freezone_prepare_production_nodes")
        return {"state": "unprepared", "slots": [], "next_action": action}
    nodes = (draft["compiled"].get("plan") or {}).get("nodes") or []
    plan_slot_count = sum(isinstance(node, dict) and node.get("stage") == "production_plans"
                          for node in nodes)
    receipt_slots = read_native_plan_slot_bindings(project_dir=state_dir, draft_id=draft["draft_id"])
    from novelvideo.freezone.workflow_runs import list_workflow_runs

    runs = [run for run in list_workflow_runs(project_dir=state_dir,
                                              canvas_id=binding["canvas_id"], limit=200)
            if run.get("project_id") == ctx.project_id
            and run.get("actor_id") == ctx.requester_user_id]
    slots = []
    for item in nodes:
        if not isinstance(item, dict) or item.get("stage") != "production_plans":
            continue
        slot_id = item.get("id")
        if not isinstance(slot_id, str) or not slot_id:
            continue
        plan_ids = _candidate_ids(canvas, draft["draft_id"], slot_id)
        compose_slot = "composition_" + slot_id.removeprefix("production_plan_")
        compose_ids = _candidate_ids(canvas, draft["draft_id"], compose_slot)
        receipt = receipt_slots.get(slot_id)
        receipt_changed = bool(receipt and (
            plan_ids != [receipt["plan_node_id"]]
            or compose_ids != ([receipt["compose_node_id"]] if receipt["compose_node_id"] else [])
        ))
        bound_ids = set(plan_ids)
        if receipt and receipt.get("plan_node_id"):
            bound_ids.add(receipt["plan_node_id"])
        execution = next(((run, candidate) for run in runs for candidate in run.get("actions", [])
                          if candidate.get("action") in {"generate_production_plan_draft", "generate_production_plan_arrangement"}
                          and (candidate.get("node_id") in bound_ids
                               or (not bound_ids and plan_slot_count == 1
                                   and candidate.get("status") == "completed"
                                   and _settlement_conflict(ctx, canvas_id=binding["canvas_id"],
                                                            run=run, action=candidate, binding=binding)))), None)
        data: dict[str, Any] = {}
        task_id = _bound_task_id(ctx, binding["canvas_id"], plan_ids[0], execution) if len(plan_ids) == 1 else None
        task_status = _bound_task_status(ctx, task_id, execution)
        conflict = bool(execution and execution[1].get("status") == "completed"
                        and _settlement_conflict(ctx, canvas_id=binding["canvas_id"],
                                                 run=execution[0], action=execution[1], binding=binding))
        if len(plan_ids) > 1 or len(compose_ids) > 1:
            action = _next("resolve_binding_conflict", candidate_node_ids=plan_ids)
            state = "binding_conflict"
        elif conflict and (not receipt_changed or not plan_ids):
            state, action = "resolve_conflict", _next("resolve_conflict", node_id=execution[1]["node_id"])
        elif receipt_changed:
            action = _next("resolve_binding_conflict", candidate_node_ids=plan_ids)
            state = "binding_conflict"
        elif draft["status"] == "ready":
            state, action = "ready_for_canvas_approval", _next(
                "submit_canvas_approval", tool="freezone_confirm_workflow_draft",
                draft_id=draft["draft_id"], revision=draft["revision"],
            )
        elif (draft["status"] == "submitted" and not plan_ids
              and _confirmation_task_status(ctx, draft) in {"failed", "cancelled"}):
            state, action = "delivery_unverified", _next(
                "inspect_delivery", draft_id=draft["draft_id"], task_id=draft["task_id"],
            )
        elif draft["status"] in {"confirming", "submitted"} or not plan_ids:
            state, action = "awaiting_canvas_receipt", _next("await_completion_event")
        else:
            plan_node = next((node for node in canvas.get("nodes", []) if node.get("id") == plan_ids[0]), {})
            data = plan_node.get("data") or {}
            node_ref = _artifact_ref(data.get("productionPlanV2Ref"))
            from .production_review import is_unarranged_outline
            plan_payload = data.get("productionPlanV2Draft") or {}
            if node_ref and getattr(ctx, "output_dir", None):
                from .store import RemixArtifactStore
                try:
                    reader = (artifact_reader if artifact_reader is not None
                              else RemixArtifactStore(ctx.output_dir, ctx.project_id))
                    saved = reader.read(*node_ref)
                    plan_payload = (saved["payload"] if reader.read_latest(node_ref[0])["revision"] == node_ref[1]
                                    else {})
                except (ValueError, KeyError, FileNotFoundError):
                    plan_payload = {}
            saved_plan_revision = bool(plan_payload
                and node_ref and execution
                and (execution[1].get("action") == "generate_production_plan_arrangement"
                     or (_artifact_ref(execution[1].get("result_ref")) or (None,))[0] == node_ref[0])
                and not data.get("productionPlanV2Dirty") and not data.get("productionPlanV2Conflict"))
            current_arrangement = bool(execution
                and execution[1].get("action") == "generate_production_plan_arrangement"
                and node_ref and node_ref == _artifact_ref(execution[1].get("result_ref"))
                and not data.get("productionPlanV2Dirty") and not data.get("productionPlanV2Conflict"))
            planned_revision = (item.get("data") or {}).get("remixNodeRevision")
            if (type(planned_revision) is int and data.get("remixNodeRevision") != planned_revision
                    and not saved_plan_revision and not current_arrangement):
                from .production_processing_settlement import _empty_plan
                original_goal = ((item.get("data") or {}).get("processingPayload") or {}).get("topic")
                current_goal = (data.get("processingPayload") or {}).get("topic")
                if (not execution and _empty_plan(data.get("productionPlanV2Draft"))
                        and not data.get("productionPlanV2Dirty") and not data.get("productionPlanV2Conflict")
                        and isinstance(current_goal, str) and current_goal.strip()
                        and (current_goal != original_goal or data.get('remixDirectionEdited') is True)
                        and type(data.get("remixNodeRevision")) is int
                        and data["remixNodeRevision"] > planned_revision):
                    # The same canvas command and confirmation path can update
                    # a goal before the first draft action. Its node revision is
                    # the new input version; the original claim still owns the slot.
                    state, action = "ready_to_submit", _next(
                        "run_node_action", tool="freezone_run_node_action",
                        node_id=plan_ids[0], action="generate_production_plan_draft")
                else:
                    state, action = "resolve_conflict", _next("resolve_conflict", node_id=plan_ids[0])
            elif execution and (execution[1].get("status") == "failed" or task_status in {"failed", "cancelled"}):
                state, action = "failed", _next("inspect_failure")
                if execution[1].get("action") == "generate_production_plan_arrangement":
                    action.update(node_id=plan_ids[0], action="generate_production_plan_arrangement")
            elif execution and execution[1].get("status") in {"pending", "running"}:
                state = "running" if task_status == "running" else (
                    "submitted" if task_id else "awaiting_task_receipt")
                action = _next("await_completion_event")
            elif execution and execution[1].get("status") == "completed":
                result_ref = _artifact_ref(execution[1].get("result_ref"))
                node_ref = _artifact_ref(data.get("productionPlanV2Ref"))
                edited_plan = bool(result_ref and node_ref and result_ref[0] == node_ref[0]
                    and result_ref[1] < node_ref[1] and plan_payload
                    and not data.get("productionPlanV2Dirty") and not data.get("productionPlanV2Conflict"))
                if result_ref and (result_ref == node_ref or edited_plan) and task_status == "completed":
                    if is_unarranged_outline(plan_payload):
                        state, action = "outline_ready", _next("run_node_action",
                            tool="freezone_run_node_action", node_id=plan_ids[0],
                            action="generate_production_plan_arrangement",
                            parameters={"plan_ref": {"artifact_id": node_ref[0], "revision": node_ref[1]}})
                    else:
                        state, action = "ready_for_review", _next("review_production_plan")
                elif result_ref and result_ref == node_ref:
                    state, action = "awaiting_canvas_receipt", _next("await_completion_event")
                elif data.get("productionPlanV2Ref"):
                    state, action = "resolve_conflict", _next("resolve_conflict", node_id=plan_ids[0])
                else:
                    state, action = "awaiting_canvas_receipt", _next("await_completion_event")
            elif data.get("productionPlanV2Ref"):
                state, action = "resolve_conflict", _next("resolve_conflict", node_id=plan_ids[0])
            else:
                state = "ready_to_submit"
                action = _next("run_node_action", tool="freezone_run_node_action",
                               node_id=plan_ids[0], action="generate_production_plan_draft")
        slots.append({"slot_id": slot_id, "state": state, "draft_id": draft["draft_id"],
                      "plan_node_id": plan_ids[0] if len(plan_ids) == 1 else None,
                      "compose_node_id": compose_ids[0] if len(compose_ids) == 1 else None,
                      "run_id": execution[0]["run_id"] if execution else None,
                      "operation_id": (execution[1].get("processing_operation_id")
                                       or execution[1].get("product_operation_id")) if execution else None,
                      "task_id": task_id,
                      "task_status": task_status,
                      "plan_ref": (data.get("productionPlanV2Ref")
                                   if state in {"ready_for_review", "outline_ready", "failed"} else None),
                      "next_action": action})
    next_action = next((slot["next_action"] for slot in slots
                        if slot["state"] in {"binding_conflict", "resolve_conflict", "ready_for_canvas_approval", "ready_to_submit"}),
                       slots[0]["next_action"] if slots else _next("resolve_binding_conflict"))
    return {"state": slots[0]["state"] if slots else "binding_conflict", "draft_id": draft["draft_id"],
            "run_after_create": draft["run_after_create"],
            "slots": slots, "next_action": next_action}


def validate_bound_plan_action(*, stage: dict[str, Any], node_id: str,
                               action: str) -> dict[str, Any] | None:
    """Reject an Agent's plan generation target when a unique bound slot exists."""
    if action not in {"generate_production_plan_draft", "generate_production_plan_arrangement"}:
        return None
    bound = [slot["plan_node_id"] for slot in stage.get("slots", []) if slot.get("plan_node_id")]
    if stage.get("state") == "binding_conflict":
        return {"code": "binding_conflict", "candidate_node_ids": stage.get("candidate_node_ids", [])}
    conflicting = next((slot for slot in stage.get("slots", [])
                        if slot.get("plan_node_id") == node_id
                        and slot.get("state") == "resolve_conflict"), None)
    if conflicting:
        return {"code": "resolve_conflict", "node_id": node_id}
    if bound and node_id not in bound:
        if len(bound) > 1:
            return {"code": "binding_conflict", "candidate_node_ids": bound}
        return {"code": "bound_plan_node_required", "node_id": bound[0],
                "candidate_node_ids": bound}
    return None
