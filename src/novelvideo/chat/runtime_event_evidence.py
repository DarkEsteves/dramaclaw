"""Normalize Freezone tool outcomes from Agent runtime events.

The chat application uses these pure checks to distinguish a durable canvas
write receipt from a successful transport call. Keep provider payload parsing
at this boundary while callers migrate to provider-neutral event names.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from novelvideo.chat.tool_policy import (
    FREEZONE_ARTIFACT_WRITE_TOOLS,
    FREEZONE_BACKGROUND_SUBMISSION_TOOLS,
    FREEZONE_CANVAS_WRITE_TOOLS as _FREEZONE_CANVAS_WRITE_TOOLS,
    FREEZONE_WORKFLOW_DRAFT_PREPARE_TOOLS as _FREEZONE_WORKFLOW_DRAFT_PREPARE_TOOLS,
)

_REFERENCE_PRODUCTION_PROGRESS_STATES = frozenset({
    "awaiting_direction", "drafting_plan", "awaiting_plan_review", "awaiting_plan_confirmation",
    "preparing_materials", "aligning_speech", "composing", "reviewing_output", "completed",
})


def _codex_freezone_is_non_canvas_write_event(event: Any) -> bool:
    return _codex_freezone_tool_name(event) in (
        FREEZONE_ARTIFACT_WRITE_TOOLS | FREEZONE_BACKGROUND_SUBMISSION_TOOLS
        | {"freezone_prepare_production_nodes"}
    )


def _valid_artifact_ref(value: Any, project: str) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("artifact_id"), str)
        and bool(value["artifact_id"].strip())
        and isinstance(value.get("revision"), int)
        and not isinstance(value["revision"], bool)
        and value["revision"] >= 1
        and value.get("project_id", project) == project
    )


def _codex_freezone_non_canvas_receipt(
    event: Any, *, expected_project: str, expected_canvas: str | None,
    expected_agent: str | None = None,
    admission_key_for_request: Callable[..., str] | None = None,
) -> dict[str, Any] | None:
    """Validate typed durable outcomes without manufacturing canvas receipts."""
    if not _codex_freezone_is_non_canvas_write_event(event):
        return None
    if str(getattr(event, "status", "") or "").lower() not in {"completed", "success", "succeeded"} or getattr(event, "error", None):
        return None
    name = _codex_freezone_tool_name(event)
    ref_fields = {
        "freezone_prepare_reference_decision": ("decision_ref",),
        "freezone_review_production_plan": ("review_ref",),
        "freezone_prepare_reference_branches": (),
        "freezone_associate_material_upload": ("work_ref",),
        "freezone_review_production_output": ("review_ref", "manifest_ref"),
        "freezone_prepare_material_work": ("work_ref",),
        "freezone_prepare_material_stage": ("work_ref",),
        "freezone_review_material_candidate": ("review_ref",),
        "freezone_accept_material_candidate": ("work_ref",),
        "freezone_retry_material_step": ("work_ref",),
        "freezone_revise_material_strategy": ("work_ref",),
        "freezone_prepare_speech_correction": ("preview_ref",),
        "freezone_advance_reference_production": ("commission_ref",),
        "freezone_select_material_work": ("commission_ref", "plan_ref", "work_ref"),
        "freezone_advance_production": ("commission_ref",),
    }
    for value in (getattr(event, "structured", None), getattr(event, "output", None)):
        for payload in _json_objects_from_codex_tool_value(value):
            if (payload.get("ok") is not True or payload.get("project_id") != expected_project
                    or not isinstance(payload.get("canvas_id"), str) or not payload["canvas_id"]
                    or (expected_canvas is not None and payload["canvas_id"] != expected_canvas)):
                continue
            if name == "freezone_prepare_production_nodes":
                # The thin entry can return an approval/apply receipt, or reuse
                # a durable native draft. Neither implies plan generation.
                applied = _codex_freezone_write_receipt(
                    event, expected_project=expected_project, expected_canvas=expected_canvas,
                )
                if applied is not None:
                    return applied
                stage = payload.get("native_plan_stage")
                if (isinstance(payload.get("draft_id"), str) and payload["draft_id"]
                        and type(payload.get("revision")) is int and payload["revision"] >= 1
                        and type(payload.get("run_after_create")) is bool
                        and isinstance(stage, dict) and stage.get("draft_id") == payload["draft_id"]
                        and stage.get("state") in {"ready_for_canvas_approval", "awaiting_canvas_receipt",
                            "ready_to_submit", "submitted", "running", "ready_for_review", "failed"}):
                    return payload
                continue
            if name in FREEZONE_BACKGROUND_SUBMISSION_TOOLS:
                if payload.get("status") == "reference_analysis_reused" and payload.get("submitted") is False:
                    arguments = getattr(event, "input", None)
                    if (isinstance(arguments, dict)
                            and arguments.get("source_node_id") == payload.get("source_node_id")
                            and isinstance(payload.get("analysis_node_id"), str) and payload["analysis_node_id"]
                            and isinstance(payload.get("run_id"), str) and payload["run_id"]
                            and _valid_artifact_ref(payload.get("commission_ref"), expected_project)):
                        return payload
                    continue
                run = payload.get("run")
                if (payload.get("submitted") is not True or not isinstance(run, dict)
                        or payload.get("status") != "reference_analysis_submitted"):
                    continue
                # public_run intentionally omits the private request key; the
                # plugin returns the actual admission key separately.
                identity_fields = ("run_id", "analysis_node_id")
                if not all(isinstance(payload.get(key), str) and payload[key].strip()
                           and run.get(key) == payload[key] for key in identity_fields):
                    continue
                if not isinstance(payload.get("idempotency_key"), str) or not payload["idempotency_key"].strip():
                    continue
                if run.get("project_id") != expected_project or run.get("canvas_id") != payload["canvas_id"]:
                    continue
                state = run.get("status")
                if run.get("error") or state not in ({"submitting", "transcribing", "analyzing", "analysis_ready", "resuming"} | _REFERENCE_PRODUCTION_PROGRESS_STATES):
                    continue
                # Admission replay can return the persisted continuation after
                # it progressed. Its actual current commission must identify
                # both the saved version and the same reported native phase.
                if state in _REFERENCE_PRODUCTION_PROGRESS_STATES and (
                    not _valid_artifact_ref(run.get("commission_ref"), expected_project)
                    or run.get("commission_phase") != state
                ):
                    continue
                # Adoption can reuse a manually saved result with no task,
                # and a leased submission may be waiting for its task bind.
                # Transcribing and analyzing runs must identify their admitted task.
                task_fields = ("task_id", "task_key")
                task_bound = all(isinstance(payload.get(key), str) and payload[key].strip()
                                 and run.get(key) == payload[key] for key in task_fields)
                task_absent = all(payload.get(key) is None and run.get(key) is None for key in task_fields)
                if not task_bound and not (state not in {"transcribing", "analyzing"} and task_absent):
                    continue
                arguments = getattr(event, "input", None)
                if (not isinstance(arguments, dict) or not arguments.get("source_node_id")
                        or arguments["source_node_id"] != run.get("source_node_id")
                        or not isinstance(arguments.get("idempotency_key"), str)
                        or not arguments["idempotency_key"].strip()
                        or admission_key_for_request is None):
                    continue
                expected_key = admission_key_for_request(
                    project_id=expected_project, canvas_id=payload["canvas_id"],
                    source_node_id=arguments["source_node_id"],
                    user_goal=str(arguments.get("user_goal") or ""),
                    inputs=arguments.get("inputs") or {},
                    operation_id=arguments["idempotency_key"],
                )
                if expected_key != payload["idempotency_key"]:
                    continue
                return payload
            fields = ref_fields[name]
            if name == "freezone_revise_material_strategy":
                arguments = getattr(event, "input", None)
                requested = arguments.get("work_ref") if isinstance(arguments, dict) else None
                saved = payload.get("work_ref")
                if (not _valid_artifact_ref(requested, expected_project)
                        or not _valid_artifact_ref(saved, expected_project)
                        or saved["artifact_id"] != requested["artifact_id"]
                        or saved["revision"] < requested["revision"]
                        or not isinstance(payload.get("changed_step_ids"), list)
                        or not isinstance(payload.get("affected_step_ids"), list)):
                    continue
            if name in {"freezone_select_material_work", "freezone_advance_production"}:
                arguments = getattr(event, "input", None)
                if not isinstance(arguments, dict):
                    continue
                request_ref = arguments.get("commission_ref")
                current_ref = payload.get("commission_ref")
                if (not _valid_artifact_ref(request_ref, expected_project)
                        or not _valid_artifact_ref(current_ref, expected_project)
                        or current_ref["artifact_id"] != request_ref["artifact_id"]
                        or current_ref["revision"] < request_ref["revision"]):
                    continue
                command_id = arguments.get("command_id")
                if not isinstance(command_id, str) or not command_id.strip() or len(command_id) > 128:
                    continue
                if name == "freezone_select_material_work":
                    requested_work, current_work = arguments.get("work_ref"), payload.get("work_ref")
                    if (not _valid_artifact_ref(requested_work, expected_project)
                            or not _valid_artifact_ref(current_work, expected_project)
                            or current_work["artifact_id"] != requested_work["artifact_id"]
                            or current_work["revision"] < requested_work["revision"]):
                        continue
                elif not _valid_production_advancement(payload, arguments, expected_project,
                                                       payload["canvas_id"], expected_agent):
                    continue
            if name == "freezone_prepare_reference_branches":
                commission = payload.get("commission")
                branches = payload.get("branches")
                if (not isinstance(commission, dict)
                        or not _valid_artifact_ref(commission.get("commission_ref"), expected_project)
                        or not isinstance(branches, list) or not branches
                        or not all(isinstance(branch, dict) and _valid_artifact_ref(branch.get("commission_ref"), expected_project) for branch in branches)):
                    continue
            if all(_valid_artifact_ref(payload.get(key), expected_project) for key in fields):
                return payload
    return None


def _valid_production_advancement(payload: dict, arguments: dict, project: str,
                                  canvas: str, agent: str | None) -> bool:
    """Validate the actual current host projection and its native result packet.

    A native binding may publish canvas state on the server; this packet is
    evidence of that native operation, never a frontend applied receipt.
    """
    scope = payload.get("scope")
    if (payload.get("schema_version") != "reference_production_state.v1"
            or not isinstance(scope, dict) or set(scope) != {"project_id", "canvas_id", "agent_id"}
            or scope.get("project_id") != project or scope.get("canvas_id") != canvas
            or not isinstance(scope.get("agent_id"), str) or not scope["agent_id"].strip()
            or (agent is not None and scope["agent_id"] != agent)):
        return False
    for revision in (payload.get("projection_revision"), arguments.get("expected_projection_revision")):
        if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{64}", revision):
            return False
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", arguments["command_id"]):
        return False
    for key in ("plan_ref", "work_ref"):
        if key not in payload or (payload[key] is not None and not _valid_artifact_ref(payload[key], project)):
            return False
    advancement = payload.get("advancement")
    if (not isinstance(advancement, dict) or "action" not in advancement
            or type(advancement.get("replayed")) is not bool):
        return False
    action = advancement["action"]
    if action is None:
        return True
    if (not isinstance(action, dict) or action.get("owner") != "system"
            or not all(isinstance(action.get(key), str) and action[key].strip() for key in ("kind", "action_id"))):
        return False
    result = advancement.get("result")
    if not isinstance(result, dict) or not result:
        return False
    for key in ("commission_ref", "plan_ref", "work_ref", "review_ref", "manifest_ref", "result_ref"):
        if key in result and result[key] is not None and not _valid_artifact_ref(result[key], project):
            return False
        if key in {"commission_ref", "plan_ref", "work_ref"} and result.get(key) is not None:
            current_ref = payload.get(key)
            if (not _valid_artifact_ref(current_ref, project)
                    or current_ref["artifact_id"] != result[key]["artifact_id"]
                    or current_ref["revision"] < result[key]["revision"]):
                return False
    if "operation_id" in result or "task_id" in result:
        if not all(isinstance(result.get(key), str) and result[key].strip() for key in ("operation_id", "task_id")):
            return False
    return True


def _codex_freezone_tool_name(event: Any) -> str:
    return str(getattr(event, "name", "") or "").rsplit(".", 1)[-1].strip()


def _json_objects_from_codex_tool_value(value: Any) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    if isinstance(value, dict):
        objects.append(value)
        for nested in value.values():
            objects.extend(_json_objects_from_codex_tool_value(nested))
    elif isinstance(value, list):
        for nested in value:
            objects.extend(_json_objects_from_codex_tool_value(nested))
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            # ValueError also covers a digit string beyond int()'s digit limit,
            # which json.loads raises as a plain ValueError, not a decode error.
            return objects
        objects.extend(_json_objects_from_codex_tool_value(parsed))
    return objects


def plan_review_precondition_blocker(event: Any) -> dict[str, Any] | None:
    """Read only a typed server rejection of an early plan proposal."""
    if (_codex_freezone_tool_name(event) != 'freezone_prepare_reference_decision'
            or not isinstance(getattr(event, 'input', None), dict)
            or event.input.get('kind') != 'plan'):
        return None
    required = ('project_id', 'canvas_id', 'requester_user_id', 'commission_id',
                'plan_ref', 'direction_revision')
    for value in (getattr(event, 'structured', None), getattr(event, 'output', None)):
        for payload in _json_objects_from_codex_tool_value(value):
            blocker = payload.get('blocker')
            if (payload.get('ok') is False and isinstance(blocker, dict)
                    and blocker.get('code') == 'plan_review_required'
                    and all(blocker.get(key) for key in required)):
                return blocker
    return None


def _codex_freezone_is_write_event(event: Any) -> bool:
    name = _codex_freezone_tool_name(event)
    if name == "freezone_prepare_production_nodes":
        # A preparation is not a canvas write. Track only its verified apply;
        # the non-canvas attempt already captures pre-apply errors and waits.
        return _codex_freezone_write_receipt(event) is not None
    if name not in _FREEZONE_CANVAS_WRITE_TOOLS:
        return False
    if name == "freezone_run_node_action":
        for payload in _json_objects_from_codex_tool_value(
            getattr(event, "input", None)
        ):
            action = payload.get("action")
            if action in {"read_source", "history"}:
                return False
            if isinstance(action, str) and action.strip():
                return True
        return True
    return True


def _codex_freezone_write_result_succeeded(event: Any) -> bool:
    return _codex_freezone_write_receipt(event) is not None


def _codex_freezone_write_receipt(
    event: Any,
    *,
    expected_project: str | None = None,
    expected_canvas: str | None = None,
) -> dict[str, Any] | None:
    if _codex_freezone_tool_name(event) not in (_FREEZONE_CANVAS_WRITE_TOOLS | {"freezone_prepare_production_nodes"}):
        return None
    status = str(getattr(event, "status", "") or "").strip().lower()
    if status not in {"completed", "success", "succeeded"} or getattr(
        event, "error", None
    ):
        return None
    values = [getattr(event, "structured", None), getattr(event, "output", None)]
    for value in values:
        for payload in _json_objects_from_codex_tool_value(value):
            if payload.get("ok") is not True:
                continue
            apply_status = str(payload.get("canvas_apply_status") or "").strip().lower()
            project_id = str(payload.get("project_id") or "").strip()
            canvas_id = str(payload.get("canvas_id") or "").strip()
            if expected_project is not None and project_id != expected_project:
                continue
            if expected_canvas is not None and canvas_id != expected_canvas:
                continue
            bridge_key = str(payload.get("bridge_key") or "").strip()
            revision = payload.get("revision")
            story_tool = _codex_freezone_tool_name(event)
            if story_tool in {
                "dramaclaw_create_interactive_story",
                "dramaclaw_patch_interactive_story",
                "dramaclaw_save_interactive_story_outline",
                "dramaclaw_confirm_interactive_story_outline",
                "dramaclaw_confirm_interactive_story_stages",
            }:
                identity_field = (
                    "outline_id"
                    if story_tool in {"dramaclaw_save_interactive_story_outline", "dramaclaw_confirm_interactive_story_outline"}
                    else "story_id"
                )
                if (
                    payload.get("refresh_canvas") is True
                    and isinstance(payload.get(identity_field), str)
                    and payload[identity_field].strip()
                    and project_id
                    and canvas_id
                    and type(revision) is int
                    and revision >= 0
                ):
                    return payload
                continue
            # A transport/tool status is not proof that the canvas mutation
            # was persisted. Browser-applied results are durable only when
            # they carry the bridge receipt identity; direct applies must
            # carry the saved canvas revision returned by the persistence API.
            browser_receipt = (
                apply_status in {"applied", "accepted"}
                and payload.get("applied") is True
                and bool(bridge_key and project_id and canvas_id)
            )
            direct_receipt = (
                apply_status == "direct_applied"
                and payload.get("applied") is True
                and bool(project_id and canvas_id)
                and isinstance(revision, int)
                and not isinstance(revision, bool)
                and revision >= 0
            )
            if browser_receipt or direct_receipt:
                return payload
    return None


def production_edit_rejection(event: Any, *, expected_project: str,
                              expected_canvas: str | None) -> dict[str, Any] | None:
    """Read the plugin's scoped, no-effect schema rejection and retry link."""
    if _codex_freezone_tool_name(event) != 'freezone_prepare_production_edit':
        return None
    status = str(getattr(event, 'status', '') or '').lower()
    if (status not in {'completed', 'success', 'succeeded', 'failed'}
            or getattr(event, 'error', None)):
        return None
    for value in (getattr(event, 'structured', None), getattr(event, 'output', None)):
        for payload in _json_objects_from_codex_tool_value(value):
            if (payload.get('ok') is False and payload.get('side_effects') == 'none'
                    and (status != 'failed' or (
                        payload.get('error') == 'tool_arguments_invalid'
                        and payload.get('phase') == 'tool_validation'))
                    and payload.get('project_id') == expected_project
                    and payload.get('canvas_id') == expected_canvas
                    and isinstance(payload.get('retry_token'), str)
                    and payload['retry_token']
                    and isinstance(payload.get('error_path'), str)
                    and isinstance(payload.get('missing_fields'), list)):
                return payload
    return None


def production_edit_saved_receipt(event: Any, *, expected_project: str,
                                  expected_canvas: str | None) -> dict[str, Any] | None:
    """A completed plan edit needs both canvas apply and the exact saved ref."""
    if _codex_freezone_tool_name(event) != 'freezone_prepare_production_edit':
        return None
    arguments = getattr(event, 'input', None)
    command = arguments.get('command') if isinstance(arguments, dict) else None
    source = command.get('plan_ref') if isinstance(command, dict) else None
    if (not isinstance(source, dict)
            or not isinstance(source.get('artifact_id'), str)
            or type(source.get('revision')) is not int):
        return None
    receipt = _codex_freezone_write_receipt(
        event, expected_project=expected_project, expected_canvas=expected_canvas)
    if not receipt or receipt.get('canvas_apply_status') not in {'applied', 'direct_applied'}:
        return None
    saved = receipt.get('production_plan_ref')
    if (not isinstance(saved, dict)
            or saved.get('artifact_id') != source['artifact_id']
            or type(saved.get('revision')) is not int
            or saved['revision'] != source['revision'] + 1):
        return None
    if receipt.get('production_command_id') != command.get('command_id'):
        return None
    edits = (command.get('payload', {}).get('commands')
             if command.get('kind') == 'batch' and isinstance(command.get('payload'), dict)
             else [command])
    if not isinstance(edits, list) or not edits:
        return None
    steps = [{'kind': edit.get('kind'), 'target_id': edit.get('target_id')}
             for edit in edits if isinstance(edit, dict)]
    if (len(steps) != len(edits) or not all(
            isinstance(step['kind'], str) and step['kind']
            and isinstance(step['target_id'], str) and step['target_id']
            for step in steps)
            or receipt.get('production_command_steps') != steps):
        return None
    return receipt


def _codex_freezone_write_result_error(event: Any) -> str:
    """Extract the business error returned by a completed Freezone write tool."""

    if not (_codex_freezone_tool_name(event) in _FREEZONE_CANVAS_WRITE_TOOLS
            or _codex_freezone_is_non_canvas_write_event(event)):
        return ""
    values = [
        getattr(event, "structured", None),
        getattr(event, "output", None),
        getattr(event, "error", None),
    ]
    for value in values:
        for payload in _json_objects_from_codex_tool_value(value):
            if _codex_freezone_tool_name(event) in FREEZONE_BACKGROUND_SUBMISSION_TOOLS:
                run = payload.get("run")
                if isinstance(run, dict) and run.get("status") in {"stale", "analysis_failed", "resume_failed"}:
                    for key in ("error", "message"):
                        message = run.get(key)
                        if isinstance(message, str) and message.strip():
                            return message.strip()[:1000]
            if payload.get("ok") is not False:
                continue
            for key in ("user_message", "error"):
                message = payload.get(key)
                if isinstance(message, str) and message.strip():
                    return message.strip()[:1000]
            errors = payload.get("errors")
            if isinstance(errors, list):
                messages = [str(item).strip() for item in errors if str(item).strip()]
                if messages:
                    return "；".join(messages[:3])[:1000]
            message = payload.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()[:1000]
    raw_error = getattr(event, "error", None)
    if isinstance(raw_error, str) and raw_error.strip():
        return raw_error.strip()[:1000]
    return ""


def _codex_freezone_write_result_state(event: Any) -> str:
    """Keep cancellation, timeout, and pending approval separate from failure."""
    for value in (
        getattr(event, "structured", None),
        getattr(event, "output", None),
        {"tool_call_status": getattr(event, "status", None)},
    ):
        for payload in _json_objects_from_codex_tool_value(value):
            if (_codex_freezone_tool_name(event) in FREEZONE_BACKGROUND_SUBMISSION_TOOLS
                    and isinstance(payload.get("run"), dict) and payload["run"].get("status") == "stale"):
                return "stale"
            states = {
                str(payload.get(key) or "").lower()
                for key in ("canvas_apply_status", "tool_call_status", "status")
            }
            if states & {"cancelled", "canceled", "rejected"}:
                return "cancelled"
            if states & {"timeout", "timed_out", "expired"}:
                return "timeout"
            if states & {"running", "in_progress"}:
                return "running"
            if states & {"accepted", "submitted"}:
                return "accepted"
            if states & {
                "pending",
                "awaiting_approval",
                "waiting_approval",
            }:
                return "waiting_approval"
    return "failed"


_GENERATION_RETRY_DATA_FIELDS = frozenset(
    {
        "model",
        "modelId",
        "aspectRatio",
        "size",
        "quality",
        "resolution",
        "duration",
        "durationSec",
        "durationSeconds",
        "generateAudio",
        "count",
        "variantsPerNode",
    }
)


def _codex_freezone_generation_retry_key(event: Any) -> str | None:
    """Match a rejected generation run to a receipt-backed retry of that run."""
    name = _codex_freezone_tool_name(event)
    if name not in {
        "freezone_emit_canvas_command",
        "freezone_run_node_action",
        "freezone_run_workflow",
        "freezone_confirm_workflow_draft",
    }:
        return None
    for payload in _json_objects_from_codex_tool_value(getattr(event, "input", None)):
        if name == "freezone_confirm_workflow_draft":
            draft_id = str(payload.get("draft_id") or "").strip()
            if not draft_id:
                continue
            # Patching generation choices raises the revision, but confirmation
            # still targets the same persisted draft and canvas.
            identity = [
                name,
                payload.get("project_id"),
                payload.get("canvas_id"),
                draft_id,
            ]
            return json.dumps(identity, sort_keys=True, ensure_ascii=False)
        if name == "freezone_run_node_action":
            node_id = str(payload.get("node_id") or "").strip()
            action = str(payload.get("action") or "").strip()
            if not node_id or action not in {"generate_image", "generate_video"}:
                continue
            parameters = payload.get("parameters") or payload.get("params") or {}
            if not isinstance(parameters, dict):
                continue
            parameters = {
                key: value
                for key, value in parameters.items()
                if key not in _GENERATION_RETRY_DATA_FIELDS
            }
            identity = [
                name,
                payload.get("project_id"),
                payload.get("canvas_id"),
                node_id,
                action,
                parameters,
                bool(payload.get("regenerate") or payload.get("force_regenerate")),
            ]
            return json.dumps(identity, sort_keys=True, ensure_ascii=False)
        if name == "freezone_run_workflow":
            node_ids = payload.get("node_ids") or []
            scope = str(payload.get("scope") or "").strip()
            if not isinstance(node_ids, list) or (not node_ids and scope != "canvas"):
                continue
            identity = [
                name,
                payload.get("project_id"),
                payload.get("canvas_id"),
                node_ids,
                scope,
                str(payload.get("direction") or "connected").strip(),
                bool(payload.get("regenerate") or payload.get("force_regenerate")),
            ]
            return json.dumps(identity, sort_keys=True, ensure_ascii=False)
        commands = payload.get("commands")
        if (
            not isinstance(commands, list)
            or not commands
            or not all(isinstance(command, dict) for command in commands)
        ):
            continue
        normalized = []
        for command in commands:
            item = dict(command)
            data = item.get("data")
            if isinstance(data, dict):
                item["data"] = {
                    key: value
                    for key, value in data.items()
                    if key not in _GENERATION_RETRY_DATA_FIELDS
                }
            elif data is None:
                item["data"] = {}
            normalized.append(item)
        return json.dumps(
            [payload.get("project_id"), payload.get("canvas_id"), normalized],
            sort_keys=True,
            ensure_ascii=False,
        )
    return None


# ASCII only: str.isdigit() also accepts characters such as "²" that int()
# rejects, and those must stay distinct rather than raise.
_ASCII_INTEGER = re.compile(r"-?[0-9]+", re.ASCII)


def _argument_path_parent(root: Any, path: Any) -> tuple[Any, Any] | None:
    """The container and key/index a server-reported argument path names."""
    if not isinstance(path, list) or not path:
        return None
    target = root
    for part in path[:-1]:
        if isinstance(target, dict) and isinstance(part, str):
            target = target.get(part)
        elif (
            isinstance(target, list)
            and isinstance(part, int)
            and not isinstance(part, bool)
            and 0 <= part < len(target)
        ):
            target = target[part]
        else:
            return None
    key = path[-1]
    if isinstance(target, dict) and isinstance(key, str) and key in target:
        return target, key
    if (
        isinstance(target, list)
        and isinstance(key, int)
        and not isinstance(key, bool)
        and 0 <= key < len(target)
    ):
        return target, key
    return None


def _codex_freezone_tool_arguments(event: Any) -> dict[str, Any] | None:
    payload = getattr(event, "input", None)
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return None
    return payload if isinstance(payload, dict) else None


def _argument_retry_signature(name: str, arguments: dict[str, Any]) -> str | None:
    try:
        return json.dumps([name, arguments], sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def _codex_freezone_tool_argument_rejection(event: Any) -> dict[str, Any] | None:
    """The payload of a write the MCP input schema refused before its handler ran."""
    if _codex_freezone_tool_name(event) not in _FREEZONE_CANVAS_WRITE_TOOLS:
        return None
    rejection = None
    for value in (
        getattr(event, "structured", None),
        getattr(event, "output", None),
        getattr(event, "error", None),
    ):
        for payload in _json_objects_from_codex_tool_value(value):
            if (
                payload.get("ok") is False
                and payload.get("error") == "tool_arguments_invalid"
                and payload.get("phase") == "tool_validation"
            ):
                # structuredContent keeps only output-schema keys; prefer the
                # raw copy that still carries the reported corrections.
                if "unexpected_fields" in payload or "integer_string_fields" in payload:
                    return payload
                rejection = rejection or payload
    return rejection


def _codex_freezone_is_tool_argument_rejection(event: Any) -> bool:
    return _codex_freezone_tool_argument_rejection(event) is not None


def _codex_freezone_argument_retry_expectation(event: Any) -> str | None:
    """The exact retry that would prove a schema rejection was a correctable slip.

    A rejected call is superseded only by a successful call of the same tool
    whose complete, ordered arguments equal the rejected ones after the
    corrections the MCP server reported from its schema: dropping the fields
    no variant allows, and coercing numeric strings at paths the schema
    declares integer. Any other change (coordinates, open data, targets, count
    or order of commands) may be a different operation, so the rejection
    stays failed (#686).
    """
    rejection = _codex_freezone_tool_argument_rejection(event)
    arguments = _codex_freezone_tool_arguments(event)
    if rejection is None or arguments is None:
        return None
    try:
        expected = json.loads(json.dumps(arguments))
    except (TypeError, ValueError):
        return None
    coercions = rejection.get("integer_string_fields") or []
    removals = rejection.get("unexpected_fields") or []
    if not isinstance(coercions, list) or not isinstance(removals, list):
        return None
    # Coerce before removing: both are reported against the original arguments,
    # and removal only deletes object keys, so list indices stay valid.
    for path in coercions:
        located = _argument_path_parent(expected, path)
        if located is None:
            return None
        container, key = located
        value = container[key]
        if not isinstance(value, str) or not _ASCII_INTEGER.fullmatch(value):
            return None
        try:
            container[key] = int(value)
        except ValueError:
            # Beyond the int conversion digit limit: not a provable correction.
            return None
    for correction in removals:
        if not isinstance(correction, dict):
            return None
        path, fields = correction.get("path"), correction.get("fields")
        if not isinstance(path, list) or not isinstance(fields, list):
            return None
        if path:
            located = _argument_path_parent(expected, path)
            if located is None:
                return None
            container, key = located
            target = container[key]
        else:
            target = expected
        if not isinstance(target, dict):
            return None
        for field in fields:
            if not isinstance(field, str) or field not in target:
                return None
            del target[field]
    return _argument_retry_signature(_codex_freezone_tool_name(event), expected)


def _codex_freezone_argument_retry_signature(event: Any) -> str | None:
    """The normalized, ordered arguments of a write call, for exact comparison."""
    arguments = _codex_freezone_tool_arguments(event)
    if arguments is None:
        return None
    return _argument_retry_signature(_codex_freezone_tool_name(event), arguments)


# Workflow draft confirmation guard that rejects before any claim or dispatch.
# The plugin answers it from a single GET of the draft, and the only way forward
# is the agent's own patch of run_after_create in this turn, so a later
# confirmation of the same draft that reports the requested policy is the real
# outcome of that call (see _codex_freezone_execution_policy_requirement). A
# workflow_draft_revision_conflict is deliberately excluded: the new revision
# may hold changes the user never reviewed, so it needs a fresh confirmation
# rather than a silent retry.
_FREEZONE_DRAFT_CONFIRM_GUARD_STATUSES = frozenset(
    {"workflow_draft_execution_policy_changed"}
)


def _codex_freezone_is_generation_preflight_rejection(event: Any) -> bool:
    """Only explicit, side-effect-free rejections may be superseded by a retry."""
    if getattr(event, "error", None) or str(
        getattr(event, "status", "") or ""
    ).lower() not in {"completed", "success", "succeeded"}:
        return False
    for value in (getattr(event, "structured", None), getattr(event, "output", None)):
        for payload in _json_objects_from_codex_tool_value(value):
            if payload.get("ok") is not False:
                continue
            status = str(payload.get("status") or "")
            if (
                status == "clarification_required"
                and payload.get("code") == "generation_parameters_required"
            ):
                return True
            if (
                status in _FREEZONE_DRAFT_CONFIRM_GUARD_STATUSES
                and _codex_freezone_tool_name(event)
                == "freezone_confirm_workflow_draft"
            ):
                return True
    return False


def _codex_freezone_is_execution_policy_rejection(event: Any) -> bool:
    """A draft confirmation the plugin refused because run_after_create differed."""
    if _codex_freezone_tool_name(event) != "freezone_confirm_workflow_draft":
        return False
    for value in (getattr(event, "structured", None), getattr(event, "output", None)):
        for payload in _json_objects_from_codex_tool_value(value):
            if (
                payload.get("ok") is False
                and str(payload.get("status") or "")
                in _FREEZONE_DRAFT_CONFIRM_GUARD_STATUSES
            ):
                return True
    return False


def _codex_freezone_execution_policy_requirement(event: Any) -> bool | None:
    """The run_after_create the agent asked for in a policy-guard rejection.

    Only a boolean request can be honoured later. The rejection is superseded
    solely by a confirmation of the same draft whose receipt reports this same
    policy; a confirmation that silently kept the old policy (the agent skipped
    the patch and simply omitted run_after_create) leaves the rejection on
    record, because the user's request was not executed.
    """
    if not _codex_freezone_is_execution_policy_rejection(event):
        return None
    for payload in _json_objects_from_codex_tool_value(getattr(event, "input", None)):
        requested = payload.get("run_after_create")
        if isinstance(requested, bool):
            return requested
    return None


def _codex_freezone_confirmed_execution_policy(event: Any) -> bool | None:
    """The run_after_create a successful draft confirmation receipt reports."""
    if _codex_freezone_tool_name(event) != "freezone_confirm_workflow_draft":
        return None
    for value in (getattr(event, "structured", None), getattr(event, "output", None)):
        for payload in _json_objects_from_codex_tool_value(value):
            if payload.get("ok") is True and isinstance(
                payload.get("run_after_create"), bool
            ):
                return payload["run_after_create"]
    return None


def _codex_freezone_clarification_answered(event: Any) -> bool:
    """Recognize a successful answer, not merely a submitted or failed tool call."""
    if _codex_freezone_tool_name(event) != "freezone_request_user_clarification":
        return False
    if getattr(event, "error", None) or str(
        getattr(event, "status", "") or ""
    ).lower() not in {"completed", "success", "succeeded"}:
        return False
    for value in (getattr(event, "structured", None), getattr(event, "output", None)):
        for payload in _json_objects_from_codex_tool_value(value):
            if (
                payload.get("ok") is True
                and not payload.get("errors")
                and payload.get("clarification_status") == "answered"
            ):
                return True
    return False


def _codex_freezone_ready_workflow_draft(event: Any) -> dict[str, Any] | None:
    """Return a successfully prepared workflow draft carried by a Codex event."""

    if _codex_freezone_tool_name(event) not in _FREEZONE_WORKFLOW_DRAFT_PREPARE_TOOLS:
        return None
    status = str(getattr(event, "status", "") or "").strip().lower()
    if status not in {"completed", "success", "succeeded"} or getattr(
        event, "error", None
    ):
        return None
    for value in (getattr(event, "structured", None), getattr(event, "output", None)):
        for payload in _json_objects_from_codex_tool_value(value):
            if (
                payload.get("ok") is True
                and str(payload.get("status") or "") == "workflow_draft_ready"
                and str(payload.get("draft_id") or "").strip()
            ):
                return payload
    return None
