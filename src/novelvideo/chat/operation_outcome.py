"""Settle retries of the same verified Freezone business operation."""

from __future__ import annotations

import json
from typing import Any


_TARGETS = {
    "freezone_prepare_reference_decision": ("commission_ref", "kind"),
    "freezone_review_production_plan": ("commission_ref", "plan_ref"),
    "freezone_prepare_material_work": ("commission_ref", "plan_ref"),
    "freezone_prepare_material_stage": ("commission_ref", "work_ref"),
    "freezone_review_material_candidate": ("work_ref", "step_id"),
    "freezone_accept_material_candidate": ("work_ref", "step_id"),
    "freezone_retry_material_step": ("work_ref", "step_id"),
    "freezone_revise_material_strategy": ("work_ref",),
    "freezone_advance_reference_production": ("commission_ref", "event"),
    "freezone_advance_production": ("commission_ref", "command_id"),
}


def production_edit_retry_matches(failed: Any, corrected: Any,
                                  rejection: Any) -> bool:
    """Prove a schema correction kept the original edit's business content.

    The rejected command is compared as data. Only fields the validator reported
    missing may be added; a new command_id is a transport retry identifier.
    """
    if (not isinstance(failed, dict) or not isinstance(corrected, dict)
            or not isinstance(rejection, dict)
            or rejection.get('side_effects') != 'none'):
        return False
    if any(failed.get(key) != corrected.get(key)
           for key in ('project_id', 'canvas_id', 'node_id')):
        return False
    old = failed.get('command')
    new = corrected.get('command')
    if not isinstance(old, dict) or not isinstance(new, dict):
        return False
    if old.get('plan_ref') != new.get('plan_ref'):
        return False
    missing = rejection.get('missing_fields')
    path = rejection.get('error_path')
    if not isinstance(missing, list) or not isinstance(path, str):
        return False
    allowed = {path.rsplit('.', 1)[0] + '.' + field if '.' in path else field
               for field in missing if isinstance(field, str) and field}
    def same(before: Any, after: Any, location: str = '') -> bool:
        if isinstance(before, dict) and isinstance(after, dict):
            wrapper = path.rsplit('.', 1)[-1]
            if (location == path.rsplit('.', 1)[0]
                    and path in allowed and set(after) == {wrapper}
                    and wrapper not in before and isinstance(after[wrapper], dict)):
                return same(before, after[wrapper], location)
            for key in set(before) | set(after):
                child = f'{location}.{key}' if location else key
                if child == 'command_id':
                    continue
                if key not in before:
                    if child not in allowed or after[key] in (None, '', [], {}):
                        return False
                    continue
                if key not in after or not same(before[key], after[key], child):
                    return False
            return True
        if isinstance(before, list) and isinstance(after, list):
            return len(before) == len(after) and all(
                same(left, right, f'{location}[{index}]')
                for index, (left, right) in enumerate(zip(before, after)))
        if location in allowed and before in (None, '', [], {}):
            return after not in (None, '', [], {})
        if (location == path and type(before) is str and type(after) is int
                and before.isascii() and before.isdecimal()):
            try:
                return int(before) == after
            except ValueError:
                return False
        return type(before) is type(after) and before == after

    return same(old, new)


def recovered_plan_precondition_attempts(attempts: list[dict[str, Any]],
                                         verified_settlement: dict[str, Any]) -> list[str]:
    """Settle only an early review prerequisite after the exact review exists."""
    if (verified_settlement.get('verified') is not True
            or verified_settlement.get('review_status') != 'passed'
            or verified_settlement.get('decision_status') != 'pending'):
        return []
    keys = ('project_id', 'canvas_id', 'requester_user_id', 'commission_id',
            'plan_ref', 'direction_revision')
    if any(not verified_settlement.get(key) for key in keys):
        return []
    resolved = []
    for attempt in attempts:
        blocker = attempt.get('blocker') or {}
        proposal = attempt.get('proposal') or {}
        requested_ref = proposal.get('plan_ref')
        if (attempt.get('status') == 'failed'
                and attempt.get('tool') == 'freezone_prepare_reference_decision'
                and blocker.get('code') == 'plan_review_required'
                and all(blocker.get(key) == verified_settlement[key] for key in keys)
                and (requested_ref is None or requested_ref == verified_settlement['plan_ref'])
                and isinstance(attempt.get('call_id'), str)):
            resolved.append(attempt['call_id'])
    return resolved


def logical_operation_key(
    tool: str, arguments: Any, *, project: str, canvas: str | None, agent: str,
) -> str | None:
    """Return a scope-bound target only when enough identity is present.

    A verified receipt may supersede an earlier failed attempt with this key.
    Submission IDs and expected revisions are intentionally absent: correcting
    either is an ordinary retry of the same target, not a new creative request.
    """
    if not isinstance(arguments, dict) or not project or not canvas or not agent:
        return None
    if tool == "freezone_prepare_production_nodes":
        ref = arguments.get("commission_ref")
        if (not isinstance(ref, dict) or not isinstance(ref.get("artifact_id"), str)
                or not ref["artifact_id"] or not isinstance(arguments.get("goal"), str)
                or not arguments["goal"] or type(arguments.get("run_after_create")) is not bool):
            return None
        target = {"commission_id": ref["artifact_id"], "goal": arguments["goal"],
                  "run_after_create": arguments["run_after_create"],
                  "plan_count": arguments.get("plan_count", 1)}
    elif tool == "freezone_start_reference_analysis":
        source = arguments.get("source_node_id")
        goal = arguments.get("user_goal")
        if not isinstance(source, str) or not source or not isinstance(goal, str) or not goal:
            return None
        target = {"source_node_id": source, "user_goal": goal, "inputs": arguments.get("inputs")}
    else:
        fields = _TARGETS.get(tool)
        if fields is None:
            return None
        target = {}
        for field in fields:
            value = arguments.get(field)
            if field.endswith("_ref"):
                value = value.get("artifact_id") if isinstance(value, dict) else None
            if tool == "freezone_advance_reference_production" and field == "event":
                if not isinstance(value, dict) or not isinstance(value.get("type"), str) or not value["type"]:
                    return None
                target[field] = value
                continue
            if not isinstance(value, str) or not value:
                return None
            target[field] = value
        # The same commission can contain several distinct creative choices.
        if tool == "freezone_prepare_reference_decision":
            target["proposal"] = arguments.get("proposal")
        if tool == "freezone_revise_material_strategy":
            target["replacements"] = arguments.get("replacements")
    try:
        return json.dumps([project, canvas, agent, tool, target], ensure_ascii=False,
                          sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return None


def recovered_attempt_ids(
    *, successful_call_id: str, successful_key: str | None,
    attempt_states: dict[str, str], attempt_keys: dict[str, str | None],
    successful_revision: int | None = None,
    attempt_revisions: dict[str, int | None] | None = None,
) -> list[str]:
    """A saved receipt settles only earlier failed attempts at that target."""
    if not successful_key:
        return []
    result = []
    for call_id, state in attempt_states.items():
        if call_id == successful_call_id:
            break
        old_revision = (attempt_revisions or {}).get(call_id)
        if (state in {"failed", "timeout", "waiting_approval"}
                and attempt_keys.get(call_id) == successful_key
                and (successful_revision is None or old_revision is None
                     or successful_revision >= old_revision)):
            result.append(call_id)
    return result


def operation_input_revision(arguments: Any) -> int | None:
    if not isinstance(arguments, dict):
        return None
    for key in ("work_ref", "commission_ref", "plan_ref"):
        ref = arguments.get(key)
        value = ref.get("revision") if isinstance(ref, dict) else None
        if type(value) is int and value >= 1:
            return value
    return None


def reduce_operation_outcomes(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    """Settle failures only against later verified receipts of the same target.

    `accepted` proves native submission, not completed media generation. The
    caller retains the original attempt log; this projection identifies which
    errors still block the current turn.
    """
    recovered: set[str] = set()
    verified: list[dict[str, Any]] = []
    for index, attempt in enumerate(attempts):
        if (attempt.get("verified_receipt") is not True
                or attempt.get("status") not in {"accepted", "running", "completed"}):
            continue
        identity = tuple(attempt.get(key) for key in
                         ("scope_key", "logical_operation_id", "target_key"))
        if any(not isinstance(value, str) or not value for value in identity):
            continue
        revision = attempt.get("input_revision")
        if type(revision) is not int or revision < 0:
            continue
        verified.append(attempt)
        links = attempt.get('retry_of')
        linked_ids = ({links} if isinstance(links, str) else
                      set(links) if isinstance(links, (list, tuple)) and
                      all(isinstance(item, str) for item in links) else set())
        if attempt.get('status') != 'completed' or not linked_ids:
            continue
        for earlier in attempts[:index]:
            if (earlier.get("status") != "failed"
                    or earlier.get("side_effects") != "none"
                    or earlier.get("attempt_id") not in linked_ids):
                continue
            if tuple(earlier.get(key) for key in
                     ("scope_key", "logical_operation_id", "target_key")) != identity:
                continue
            old_revision = earlier.get("input_revision")
            if type(old_revision) is int and old_revision == revision:
                old_id = earlier.get("attempt_id")
                if isinstance(old_id, str) and old_id:
                    recovered.add(old_id)
    blockers = [attempt for attempt in attempts
                if attempt.get("status") in {"failed", "cancelled", "timeout"}
                and attempt.get("attempt_id") not in recovered]
    if blockers:
        status = "partial" if verified else "failed"
    elif any(attempt.get("status") == "running" for attempt in verified):
        status = "running"
    elif any(attempt.get("status") == "accepted" for attempt in verified):
        status = "accepted"
    elif verified:
        status = "completed"
    else:
        status = "incomplete"
    return {"status": status,
            "blocking_errors": blockers,
            "recovered_attempt_ids": [attempt["attempt_id"] for attempt in attempts
                                      if attempt.get("attempt_id") in recovered]}
