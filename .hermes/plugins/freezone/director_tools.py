"""Small tool adapters for the native reference production services."""
from __future__ import annotations

import os
import re
from urllib.parse import quote


_PRODUCTION_TOOLS = frozenset({"freezone_get_production_progress", "freezone_advance_production"})
_RECOVERY_FIELDS = ("code", "retryable", "retry_scope", "next_action", "required_user_action", "existing_result_ref", "problem")
_MATERIAL_DRAFT_TOOLS = frozenset({
    "freezone_prepare_material_work",
    "freezone_prepare_material_stage",
    "freezone_revise_material_strategy",
})
_MATERIAL_REVIEW_TOOLS = frozenset({
    "freezone_review_material_candidate",
    "freezone_accept_material_candidate",
})


def _safe_current_ref(source: object) -> dict[str, object]:
    """Copy only native refs that let the Agent continue the same work."""
    if not isinstance(source, dict):
        return {}
    result: dict[str, object] = {}
    for name in ("work_ref", "plan_ref"):
        value = source.get(name)
        if (isinstance(value, dict)
                and isinstance(value.get("artifact_id"), str)
                and value["artifact_id"]
                and type(value.get("revision")) is int
                and value["revision"] >= 1):
            result[name] = {
                "artifact_id": value["artifact_id"],
                "revision": value["revision"],
            }
    operation_id = source.get("operation_id")
    if isinstance(operation_id, str) and 1 <= len(operation_id) <= 128:
        result["operation_id"] = operation_id
    return result


def _material_failure_phase(name: str) -> str:
    if name in _MATERIAL_REVIEW_TOOLS:
        return "awaiting_review"
    if name in _MATERIAL_DRAFT_TOOLS:
        return "drafting"
    return "executing"


def _material_failure_instruction(failure: dict, *, ref_hint: str) -> str:
    """Name the one correction the native rejection actually requires."""
    if failure.get("request_id") and str(failure.get("code", "")).endswith("internal_error"):
        return (
            "素材工作包服务端处理失败。先记录 request_id 并停止本轮，不要原样重试；"
            + ref_hint
            + "等待用户或主 Agent 决定如何修正。"
        )
    if failure.get("issues"):
        steps = "、".join(
            str(issue.get("step_id")) for issue in failure["issues"] if issue.get("step_id")
        )
        bounds = failure["issues"][0]
        return (
            f"这些视频素材步骤缺少时长：{steps}。请在每个 generate 视频步骤的 generation_inputs "
            f"里给出整数 durationSec（{bounds.get('minimum')}～{bounds.get('maximum')} 秒），"
            "可参照已确认方案中对应 beat 的 duration_frames；"
            + ref_hint
            + "不要原样重试，也不要创建新的素材工作包。"
        )
    if failure.get("placement_id") and failure.get("step_ids"):
        placement = str(failure["placement_id"])
        turns = "、".join(str(turn) for turn in failure.get("turn_ids") or [])
        owners = "、".join(str(step) for step in failure["step_ids"])
        return (
            f"台词归属冲突：方案要求 {placement} 的台词（{turns or '全部'}）必须由最终步骤 {owners} 亲自生成。"
            f"请把 {owners} 的 dialogue_turn_ids 按剧本顺序补全为 {turns or '该 placement 的全部 turn'}，"
            f"并在它对应的 requirement.turn_ids 里列出同一批 turn；生成视频的画面步骤只保留 references，不要各自带走台词。"
            + ref_hint
            + "不要原样重试，也不要创建新的素材工作包。"
        )
    if failure.get("errors"):
        return (
            "按 error 中的字段路径和 errors 中的约束修正当前素材请求后再提交；"
            + ref_hint
            + "不要原样重试，也不要创建新的素材工作包。"
        )
    return (
        "按返回的 error 修正本次素材请求后再提交；"
        + ref_hint
        + "不要原样重试，也不要创建新的素材工作包。"
    )


def _safe_material_errors(value: object) -> list[dict[str, str]]:
    """Forward only bounded field diagnostics from a material request."""
    if not isinstance(value, list):
        return []
    errors: list[dict[str, str]] = []
    for item in value[:8]:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        message = item.get("message") or item.get("reason")
        if not isinstance(path, str) or not re.fullmatch(r"[A-Za-z0-9_.\[\]-]{1,160}", path):
            continue
        if not isinstance(message, str):
            continue
        message = " ".join(message.split())[:240]
        if not message or re.search(r"traceback|authorization|bearer\s|api[_-]?key|secret|password", message, re.I):
            continue
        errors.append({"path": path, "message": message})
    return errors


def _safe_material_issues(value: object) -> list[dict[str, object]]:
    """Forward bounded per-step material constraints from the native API."""
    if not isinstance(value, list):
        return []
    issues: list[dict[str, object]] = []
    for item in value[:8]:
        if not isinstance(item, dict):
            continue
        issue: dict[str, object] = {}
        step_id = _safe_material_ids(item.get("step_id"))
        if not step_id:
            continue
        issue["step_id"] = step_id
        for key in ("minimum", "maximum"):
            if type(item.get(key)) is int:
                issue[key] = item[key]
        actual = item.get("actual")
        if isinstance(actual, dict):
            bounded = {
                name: field for name, field in list(actual.items())[:4]
                if isinstance(name, str) and len(name) <= 64
                and (type(field) is int or isinstance(field, str) and len(field) <= 64)
            }
            if bounded:
                issue["actual"] = bounded
        message = item.get("message")
        if isinstance(message, str):
            message = " ".join(message.split())[:240]
            if message and not re.search(
                r"traceback|authorization|bearer\s|api[_-]?key|secret|password", message, re.I
            ):
                issue["message"] = message
        issues.append(issue)
    return issues


def _safe_material_ids(value: object) -> list[str] | str | None:
    """Keep the actual plan/step identifiers a correction must use."""
    if isinstance(value, str):
        return value[:128] if 0 < len(value) <= 128 else None
    if not isinstance(value, list):
        return None
    ids = [item[:128] for item in value[:200]
           if isinstance(item, str) and 0 < len(item) <= 128]
    return ids or None


def _session_agent_id():
    profile = os.environ.get("DRAMACLAW_AGENT_PROFILE", "").strip()
    if profile.startswith("freezone:"):
        return profile.removeprefix("freezone:").strip() or "main"
    return os.environ.get("DRAMACLAW_AGENT_ID", "").strip() or "main"


def _production_instruction(data):
    status = data.get("execution_status")
    if status in {"running", "recovering"}:
        return "End this turn while the saved operation runs or recovers. Wait for the host completion event; do not poll nodes, recreate work or repeat paid generation."
    if status == "stopped":
        return "End this turn. The production is stopped; preserve its results and wait for an explicit user resume."
    if status == "failed":
        return "Read the actual problem, execution_problem and blockers. Preserve existing results and repair only the stated failure within the authorized scope; failure does not authorize regeneration."
    if status == "completed":
        return "End this turn and present the actual saved delivery evidence for this production scope."
    if "branches" in data:
        return "Use the actual branches to identify the user's requested commission. If ambiguous, ask for the specific choice; never select the most recent plan or work. Read that commission with freezone_get_production_progress."
    action = data.get("next_action") or {}
    if action.get('kind') == 'await_canvas_receipt':
        return 'End this turn and await the existing canvas delivery event. Keep the current draft and task references; do not resubmit confirmation or prepare another draft.'
    if action.get("owner") == "system":
        return "Call freezone_advance_production with this exact commission_ref and expected_projection_revision=projection_revision plus a stable command_id. The host rechecks current evidence; this does not approve user decisions or paid generation."
    if action.get("owner") == "agent":
        return "Perform the actual returned agent next_action using saved current references and registered native actions, then reread freezone_get_production_progress. Preserve accepted media and all pending user decisions."
    if status == "waiting_user" or action.get("owner") == "user":
        return "Present the actual pending user action and End this turn. Waiting for review or a creative choice is normal; never supply approval or claim unobserved audio passed."
    return "Read the actual blockers and current saved direction before choosing more work. Preserve existing results; no missing next_action authorizes duplicate work or paid generation."


_MATERIAL_ADMISSION_FLOW = (
    "For each ready generation wave, first read freezone_get_workflow_skill with "
    "skill_id=reference-video-remix and use the returned exact skill.version, not a "
    "remembered or placeholder version. Call freezone_begin_agent_product_generation once with "
    "product_kind=workflow_result and that exact Skill identity, then copy its "
    "returned operation_id into this material tool at top level. Use run_after_create=true "
    "when the user requested actual material production. If work_ref is already saved, "
    "reuse its exact returned revision in freezone_prepare_material_stage. Reuse a valid "
    "nonterminal admission already returned for this same wave without registering again. "
    "Do not switch to generic workflow preparation; native workflow approval remains required."
)

_MATERIAL_ADMISSION_RECOVERY = (
    "The material work is already saved. Use the exact returned work_ref; do not create "
    "another work package. If no valid current admission exists, call "
    "freezone_begin_agent_product_generation with product_kind=workflow_result and the "
    "actual selected Skill identity. Read its successful server response and copy the "
    "real operation_id at top level into freezone_prepare_material_stage with this "
    "saved work_ref. Use run_after_create=true when the user requested actual material "
    "production; otherwise retain the agreed preparation-only execution choice. "
    "Do not register again if a valid nonterminal operation_id was already returned for "
    "this ready wave. Never invent, reconstruct or auto-fill an ID, reuse a terminal "
    "operation, or bypass a rejected admission. Do not switch to generic workflow "
    "preparation (freezone_prepare_workflow/freezone_prepare_workflow_draft): it does not "
    "associate this material work with its execution draft. Present the actual native "
    "workflow approval before execution; registration alone does not start generation."
)

_MATERIAL_PASSED_REVIEW_RECOVERY = (
    "The native accept remains rejected. Preserve the same work_ref, selected step and existing inputs. "
    "Call freezone_get_material_work with that exact saved work_ref; the director summary contains "
    "pending reviews and is not the record of saved passed confirmation. Read the actual returned "
    "material_reviews and use only matches_current_work_revision=true or host-certified "
    "review_compatible_with_current_work=true records for the same work, step and resource whose "
    "technical/content statuses are both passed. Use the returned current work_ref head, retaining "
    "the original eligible review_ref and its original basis. Retry accept "
    "with the actual returned saved review_ref and exact resource. Do not guess revisions or create "
    "another work or review to evade the check, infer approval from chat, or silently switch the "
    "reference strategy. If the work revision changed or no matching passed record is available, "
    "keep acceptance blocked and request the specific native recheck or user review. Truncated "
    "review results are not proof that no saved confirmation exists."
)


def build_director_tools(host: dict):
    ref = {"type": "object", "properties": {"artifact_id": {"type": "string", "minLength": 1},
           "revision": {"type": "integer", "minimum": 1}, "project_id": {"type": "string"}}, "required": ["artifact_id", "revision"], "additionalProperties": False}
    obj = {"type": "object"}
    text = {"type": "string", "minLength": 1}
    # Material operations use exact native revision refs, unlike scoped commission refs.
    exact_ref = {"type": "object", "properties": {"artifact_id": {"type": "string"},
                 "revision": {"type": "integer", "minimum": 1}},
                 "required": ["artifact_id", "revision"], "additionalProperties": False,
                 "description": "Exactly artifact_id and revision from the current saved artifact; omit project_id inside this ref."}
    media_kind = {"type": "string", "enum": ["image", "video", "audio"]}
    nullable_text = {"type": ["string", "null"]}
    material_reference = {
        "type": "object", "properties": {
            "step_id": nullable_text, "node_id": nullable_text,
            "role": {"type": "string", "enum": ["identity", "product", "setting", "composition", "motion", "voice", "first_frame", "last_frame"]}},
        "required": ["role"], "additionalProperties": False,
        "oneOf": [
            {"required": ["step_id"], "properties": {"step_id": text, "node_id": {"enum": [None, ""]}}},
            {"required": ["node_id"], "properties": {"node_id": text, "step_id": {"enum": [None, ""]}}}],
        "description": "Exactly one source: an actual graph step_id OR current-canvas node_id, with a supported reference role."}
    material_requirement = {
        "type": "object", "properties": {
            "id": {**text, "maxLength": 128},
            **{key: {"type": "array", "items": {"type": "string"}, "maxItems": 200,
                     "description": "Actual IDs read from the confirmed saved plan; never invent IDs."}
               for key in ("placement_ids", "take_ids", "turn_ids")},
            "description": {**text, "maxLength": 4000}, "media_kind": media_kind,
            "final_step_id": {**text, "description": "ID of the final step producing this requirement's media_kind; shared intermediate references are separate steps."},
            "acceptance": {"type": "array", "items": {"type": "string"}, "maxItems": 100},
            "preservation_bindings": {"type": "array", "maxItems": 100, "items": {
                "type": "object", "properties": {
                    "choice_id": text,
                    "source_node_id": text,
                    "role": {"type": "string", "enum": ["identity", "product", "setting", "composition", "motion", "first_frame", "last_frame"]},
                }, "required": ["choice_id", "source_node_id", "role"], "additionalProperties": False},
                "description": "For each explicitly confirmed visual_identity preserve choice, bind its stable choice ID to a real current image node used through this requirement's reference chain. The host records its media hash."}},
        "required": ["id", "description", "media_kind", "final_step_id"], "additionalProperties": False}
    material_step = {
        "type": "object", "properties": {
            "id": {**text, "maxLength": 128, "pattern": "^[A-Za-z0-9_-]+$"},
            "label": {**text, "maxLength": 200}, "media_kind": media_kind,
            "source": {"type": "string", "enum": ["generate", "existing", "upload"]},
            "recipe_id": {"enum": ["general-image", "general-video", "general-audio", None]},
            "prompt": {"type": "string", "maxLength": 20000}, "node_id": nullable_text,
            "dialogue_turn_ids": {"type": "array", "items": text, "maxItems": 200, "uniqueItems": True,
                "description": "Ordered IDs of exact saved plan turns this audio/video step must speak. Omit for a silent or reference-only step; never copy dialogue into generation_inputs."},
            "depends_on": {"type": "array", "items": {"type": "string"}, "maxItems": 100},
            "references": {"type": "array", "items": material_reference, "maxItems": 100},
            "generation_inputs": {"type": "object",
                "propertyNames": {"not": {"enum": ["id", "recipe_id", "prompt", "name", "reference_inputs", "node_id", "depends_on", "source", "status", "dialogue", "narration", "voiceover", "speech_text", "speechText"]}},
                "description": "Optional real model catalog inputs, e.g. aspect_ratio when supported. Identity, prompt and references use the named step fields; never override them here."},
            "status": {"enum": ["planned", "awaiting_upload"]},
            "candidates": {"type": "array", "maxItems": 0},
            "accepted_resource": {"type": "null"},
            **{key: {"type": ["object", "null"], "maxProperties": 0} for key in ("review_ref", "execution")},
            "task_id": {"enum": [None, ""]},
            "reason": nullable_text},
        "required": ["id", "label", "media_kind", "source"], "additionalProperties": False,
        "allOf": [
            {"if": {"properties": {"source": {"const": "generate"}}},
             "then": {"required": ["recipe_id", "prompt"], "properties": {"prompt": {"pattern": "\\S"}},
                      "oneOf": [{"properties": {"media_kind": {"const": kind}, "recipe_id": {"const": "general-" + kind}}}
                                for kind in ("image", "video", "audio")]},
             "else": {"properties": {"recipe_id": {"type": "null"}}}}],
        "description": "Author initial steps only. Generate needs matching general-image/video/audio Recipe and nonblank prompt; existing/upload has no Recipe. Execution, candidates and acceptance are server owned."}
    review_finding = {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": ["dialogue", "narrative", "audio_visual_roles", "material_requirements", "native_effects", "output", "semantic_anchors", "direction"]},
            "entity_kind": {"type": "string", "enum": ["plan", "beat", "turn", "placement", "resource", "selection"]},
            "entity_id": text,
            "requirement_id": {"anyOf": [text, {"type": "null"}], "default": None,
                               "description": "For every current semantic requirement, copy its exact id and cite the actual saved entity ID plus its purpose, text, description or media path as evidence."},
            "verdict": {"type": "string", "enum": ["pass", "change", "unsupported"]},
            "reason": text,
            "evidence": {"type": "array", "items": text, "minItems": 1},
            "evidence_refs": {"anyOf": [{"type": "array", "items": {
                "type": "object", "properties": {
                    "entity_kind": {"type": "string", "enum": ["plan", "beat", "turn", "placement", "resource", "selection"]},
                    "entity_id": text,
                    "field": {"type": "string", "enum": ["goal", "purpose", "text", "description", "media_path", "graphic.text"]},
                }, "required": ["entity_kind", "entity_id", "field"], "additionalProperties": False,
            }}, {"type": "null"}], "default": None},
        },
        "required": ["category", "entity_kind", "entity_id", "verdict", "reason", "evidence"],
        "additionalProperties": False,
    }
    scopes = host["_SCOPE_PROPS"]
    material_draft_options = {
        "operation_id": {**text, "description": (
            "Exact admitted workflow_result operation_id returned by freezone_begin_agent_product_generation "
            "for this ready wave. Never invent or reconstruct it."
        )},
        "run_after_create": {"type": "boolean", "description": (
            "Use true when the user requested material production so confirming the draft starts generation. "
            "Set this while preparing the draft; omitted behavior follows the existing workflow default."
        )},
    }
    def handler(name, method, suffix, keys, *, evidence=False, material=False):
        def execute(args, **_):
            def result(value):
                return host["_structured_tool_result"]({**value, "project_id": project, "canvas_id": canvas}, tool_name=name)
            project = str(args.get("project_id") or host["_default_project_id"]() or "").strip()
            canvas = str(args.get("canvas_id") or host["_default_canvas_id"]() or "").strip()
            if not project or not canvas:
                return result({"ok": False, "status": "scope_required", "error": "project and canvas are required"})
            mismatch = host["_bound_scope_mismatch"](project, canvas)
            if mismatch:
                return result(mismatch)
            if name == 'freezone_prepare_reference_decision' and args.get('kind') == 'direction':
                # Cached tool lists may still allow the retired direction write.
                # Keep human discussion on the same native submission boundary.
                return result({
                    'ok': False, 'status': 'direction_discussion_only',
                    'code': 'direction_discussion_only',
                    'error': 'Discuss direction in ordinary chat; no separate direction decision is needed.',
                    'next_action': 'freezone_prepare_production_nodes',
                    'agent_instruction': 'Use the actual conversation and a concise faithful goal at native submission. If submission was rejected, resolve its stated reason first; this tool cannot bypass the rejection. Do not ask the user to confirm a saved direction decision.',
                })
            base = f"/api/v1/projects/{quote(project, safe='')}/freezone/canvases/{quote(canvas, safe='')}"
            path = base + ("/reference-evidence" if evidence else "/director/" + suffix)
            params = {key: args[key] for key in keys if key in args
                      and (not material or key not in material_draft_options)}
            query = None
            if name in _PRODUCTION_TOOLS:
                # Actor scope comes from the tool session, never model arguments.
                params["agent_id"] = _session_agent_id()
            if name == "freezone_confirm_reference_plan":
                # Keep the Agent identity out of the body contract while binding
                # the action to the current tool session.
                query = {"agent_id": _session_agent_id()}
            response = host["_request"](
                method, path,
                **({"query": query or params} if method == "GET"
                   else {"query": query, "body": params}),
            )
            if not response.get("ok"):
                error = response.get("error") or response.get("detail") or "Director operation failed"
                error_message = (
                    error.get("error") or error.get("message") or error.get("code")
                    or "Director operation failed"
                    if isinstance(error, dict) else error
                )
                failure = {"ok": False, "status": "director_operation_failed",
                           "error": error_message}
                if name == 'freezone_prepare_reference_decision' and isinstance(error, dict):
                    blocker = error.get('blocker')
                    if isinstance(blocker, dict) and blocker.get('code') == 'plan_review_required':
                        failure['blocker'] = blocker
                if name in _PRODUCTION_TOOLS:
                    # Keep host-issued recovery actions and result refs intact.
                    for source in (error, response.get("data"), response):
                        if isinstance(source, dict):
                            failure.update({key: source[key] for key in _RECOVERY_FIELDS if key in source})
                    if response.get("status_code") == 409 and not failure.get("code"):
                        failure.update(code="current_version_conflict", retryable=True,
                                       retry_scope="read_current_state", next_action=None,
                                       required_user_action=None, existing_result_ref=None)
                        failure["agent_instruction"] = "Read freezone_get_production_progress for this commission again. Inspect the current problem and revisions before retrying with a new command_id; never recreate work or regenerate media to repair a version conflict."
                if material:
                    # Keep bounded API validation details and the current
                    # native refs with the tool result so the Agent can
                    # correct the same work instead of guessing or creating
                    # another package.
                    sources = tuple(
                        source for source in (
                            error if isinstance(error, dict) else None,
                            response.get("data"), response,
                        )
                        if isinstance(source, dict)
                    )
                    for source in sources:
                        if "code" not in failure and isinstance(source.get("code"), str):
                            failure["code"] = source["code"][:96]
                        if "phase" not in failure and isinstance(source.get("phase"), str):
                            failure["phase"] = source["phase"][:96]
                        if "side_effects" not in failure and isinstance(source.get("side_effects"), str):
                            failure["side_effects"] = source["side_effects"][:96]
                        if "request_id" not in failure and isinstance(source.get("request_id"), str):
                            failure["request_id"] = source["request_id"][:160]
                        if "retryable" not in failure and isinstance(source.get("retryable"), bool):
                            failure["retryable"] = source["retryable"]
                        if "next_action" not in failure and isinstance(source.get("next_action"), (str, dict)):
                            failure["next_action"] = source["next_action"]
                        if "errors" not in failure:
                            errors = _safe_material_errors(source.get("errors"))
                            if errors:
                                failure["errors"] = errors
                        for key in ("work_ref", "draft_id", "revision", "operation_id", "material_lifecycle"):
                            if key in source:
                                failure[key] = source[key]
                        # A rejected request states which step and which planned
                        # speech failed. Without these the Agent can only guess
                        # and re-submit the same conflict.
                        if "issues" not in failure:
                            issues = _safe_material_issues(source.get("issues"))
                            if issues:
                                failure["issues"] = issues
                        for key in ("placement_id", "turn_ids", "step_ids"):
                            if key in failure or key not in source:
                                continue
                            value = _safe_material_ids(source[key])
                            if value:
                                failure[key] = value
                    current_ref: dict[str, object] = {}
                    for source in sources:
                        for key, value in _safe_current_ref(source).items():
                            current_ref.setdefault(key, value)
                    if current_ref:
                        failure["current_ref"] = current_ref
                    failure.setdefault("phase", _material_failure_phase(name))
                    if "work_ref" not in current_ref:
                        failure.setdefault("side_effects", "none")
                    if "next_action" not in failure:
                        failure["next_action"] = (
                            "revise_current_material_work"
                            if "work_ref" in current_ref
                            else "correct_material_request"
                        )
                    failure.setdefault("retryable", False)
                    ref_hint = (
                        "保留 current_ref 中的 work_ref 和最新 revision，"
                        if "work_ref" in current_ref else
                        # This rejection happens before any work package is
                        # persisted, so there is nothing to read back yet.
                        "当前还没有保存任何素材工作包，直接修正本次提交的请求；"
                    )
                    failure["agent_instruction"] = _material_failure_instruction(
                        failure, ref_hint=ref_hint,
                    )
                if (name == "freezone_accept_material_candidate" and response.get("ok") is False
                        and response.get("status_code", 422) == 422
                        and error == "material technical/content review has not passed"):
                    failure["agent_instruction"] = _MATERIAL_PASSED_REVIEW_RECOVERY
                return result(failure)
            data = response.get("data") or {}
            if name == "freezone_list_reference_media":
                if not isinstance(data, list):
                    return result({"ok": False, "status": "reference_media_inventory_invalid",
                                   "error": "Reference media inventory response was invalid"})
                return result({"ok": True, "status": "ready", "items": data,
                               "agent_instruction": "This is a read-only inventory. Use video rows as structure/action/pace evidence, but never pass a video row into an image-only reference input. An image row with existing_node_id is ready to bind; an image row without existing_node_id can be materialized by running the canvas action import_reference_keyframes on analysis_node_id, optionally with its shot_number values, then reread freezone_list_reference_media and use the returned current image node IDs. Do not generate or retry automatically when import is unavailable or fails."})
            if name in _PRODUCTION_TOOLS:
                return result({"ok": True, "status": data.get("execution_status", "ready"), **data,
                               "agent_instruction": _production_instruction(data)})
            if name == "freezone_get_material_speech_review":
                return result({"ok": True, "status": "ready", **data,
                               "agent_instruction": "Read the raw ASR differences and propose traceable corrections. An authenticated user saves lexical corrections after listening in the native material review. Do not claim to have listened when audio_review_available is false, approve sound, modify raw ASR, or invent word times."})
            if material and data.get('selection_status') == 'selection_required':
                return result({"ok": True, "status": "material_work_saved", **data,
                    "agent_instruction": "工作包已保存，尚未关联当前委托。保留 work_ref，用户明确选定后调用现有 freezone_select_material_work；不要自动采用、准备新草稿或重复创建工作包。"})
            if material and data.get("workflow_intent", {}).get("items"):
                # The work package is persistent before a draft is prepared;
                # existing approval/graph validation own actual node creation.
                from novelvideo.freezone.remix.material_work import material_lifecycle_status
                data["material_lifecycle"] = material_lifecycle_status(data.get("work") or {})
                drafted = host["_handle_prepare_workflow_draft"]({"project_id": project, "canvas_id": canvas,
                                      "intent": data["workflow_intent"],
                                      **{key: args[key] for key in material_draft_options if key in args}})
                import json
                try:
                    draft = json.loads(drafted)
                except (ValueError, TypeError):
                    draft = {"ok": False, "status": "workflow_draft_unavailable"}
                if draft.get("ok") and draft.get("draft_id"):
                    attached = host["_request"]("POST", base + "/director/material-work/" + quote(data["work_ref"]["artifact_id"], safe='') + "/execution-draft", body={"revision": data["work_ref"]["revision"], "draft_id": draft["draft_id"], "draft_revision": draft.get("revision", draft.get("draft_revision", 1))})
                    if not attached.get("ok"):
                        return result({"ok": False, "status": "material_draft_attachment_failed", "work_ref": data["work_ref"], "draft_id": draft["draft_id"], "material_lifecycle": material_lifecycle_status(data.get("work") or {}, workflow_draft=draft), "error": attached.get("error") or attached.get("detail") or "Execution draft could not be attached; preserve the draft and report the error; do not auto-retry"})
                    data["work_ref"] = (attached.get("data") or {}).get("work_ref", data["work_ref"])
                    data['work'] = (attached.get('data') or {}).get('work', data.get('work') or {})
                if draft.get("ok"):
                    lifecycle = material_lifecycle_status(
                        data.get("work") or {}, workflow_draft=draft,
                    )
                    draft["material_lifecycle"] = lifecycle
                    # Name the exact next call: a bare lifecycle word leaves the
                    # Agent with no tool to invoke, so it reports success early.
                    from novelvideo.freezone.remix.material_execution import material_submit_action
                    action = material_submit_action(draft)
                    draft['next_action'] = action
                    draft['next_tool'] = ({key: action[key] for key in ('tool', 'draft_id', 'revision')}
                                          if action else None)
                    draft["agent_instruction"] = (
                        "素材工作包已保存，工作流草稿已准备（workflow_draft_status=ready），"
                        "但画布尚未交付，实际素材也尚未生成。"
                        "现在调用 freezone_confirm_workflow_draft，参数使用返回的 draft_id 和 revision，"
                        "提交现有原生画布确认流程；确认回执和真实候选出现前，"
                        "不要声称画布节点已创建、确认卡已挂出或素材已生成。"
                        "完成本次调用后等待宿主完成事件，不要轮询或自动重试。"
                    )
                elif draft.get("status") == "workflow_result_admission_required":
                    draft["agent_instruction"] = _MATERIAL_ADMISSION_RECOVERY
                if not draft.get("ok") and args.get("operation_id"):
                    draft.setdefault("operation_id", args["operation_id"])
                if not draft.get("ok") and draft.get("status") in {"workflow_preflight_failed", "workflow_preflight_blocked", "workflow_model_unavailable"}:
                    recovery_instruction = (
                        "The material work is saved but no generation was submitted. Keep this exact "
                        "work_ref and operation_id. Resolve only the reported ready-step blockers; "
                        "revise the same work with freezone_revise_material_strategy if its saved "
                        "generation inputs need correction, then call freezone_prepare_material_stage "
                        "with the returned current work_ref. Do not create another work package or "
                        "silently drop references or user-selected parameters."
                    )
                    draft["material_recovery_instruction"] = recovery_instruction
                    draft.setdefault("agent_instruction", recovery_instruction)
                return result({**draft, "work_ref": data["work_ref"],
                               "ready_step_ids": data["ready_step_ids"],
                               "material_lifecycle": draft.get("material_lifecycle")
                               or data["material_lifecycle"]})
            if name == 'freezone_review_production_plan' and (data.get('review') or {}).get('status') == 'passed':
                data['agent_instruction'] = ('The saved plan passed review and its pending decision was '
                                             'registered internally. Present the actual reviewed plan in ordinary '
                                             'chat and ask for the user’s own reply. Do not create another plan '
                                             'decision or start materials before authenticated confirmation.')
            if name == 'freezone_confirm_reference_plan' and data.get('commission'):
                comm = data['commission']
                phase = comm.get('phase', '')
                prod_scope = comm.get('production_scope', '')
                next_action = comm.get('next_action') or {}
                if prod_scope == '仅作品方案' or phase == 'completed':
                    data['agent_instruction'] = (
                        'The plan has been confirmed and this commission is complete (production_scope=仅作品方案). '
                        'Acknowledge the confirmation in ordinary chat and End this turn. '
                        'Do not start material work or propose generation.'
                    )
                elif next_action.get('owner') == 'system':
                    data['agent_instruction'] = (
                        'The plan was confirmed successfully. A system action is queued next; '
                        'call freezone_advance_production with the returned commission_ref and '
                        'expected_projection_revision=projection_revision plus a stable command_id. '
                        'Then reread freezone_get_production_progress and present the new state.'
                    )
                elif next_action.get('owner') == 'agent':
                    data['agent_instruction'] = (
                        'The plan was confirmed successfully and the commission has advanced to phase='
                        + phase + '. Perform the returned next_action (kind=' + str(next_action.get('kind', ''))
                        + ') using saved current references, then reread freezone_get_production_progress. '
                        'Preserve accepted media and all pending user decisions. If the next action is '
                        'prepare_material_work_package, read freezone_get_reference_director_state for the '
                        'confirmed plan details and begin preparing the material work package.'
                    )
                elif next_action.get('owner') == 'user' or phase == 'waiting_user':
                    data['agent_instruction'] = (
                        'The plan was confirmed successfully. Present the actual pending user action '
                        'and End this turn. Waiting for user review or a creative choice is normal; '
                        'never supply approval or claim unobserved media passed.'
                    )
                else:
                    data['agent_instruction'] = (
                        'The plan was confirmed successfully. Read freezone_get_production_progress '
                        'for the current commission to determine the actual next step. Present the '
                        'confirmation result in ordinary chat and proceed with the saved next_action.'
                    )
            return result({"ok": True, "status": "ready", **data})
        execute.__name__ = "_handle_" + name
        return execute

    specs = [
        ("freezone_list_reference_media", "Read verified source video, extracted frames and already imported media for one saved analysis node. This is a read, not permission to create nodes. Preserve unknown ranges; use the returned real resource refs and media kinds.", "POST", "reference-media", {"analysis_node_id": {**text, "maxLength": 160}}, ["analysis_node_id"], False, False),
        ("freezone_get_production_progress", "Read current production progress and actual next actions for a saved commission. Omit commission_id to list actual independent branches; never choose the most recent work. Preserves execution_status, blockers and current projection_revision. Running tasks mean end this turn and wait for host completion events. Actor identity is from the bound tool session.", "GET", "production-state", {"commission_id": {**text, "maxLength": 160}}, [], False, False),
        ("freezone_advance_production", "Execute one deterministic system action from the current production projection. Copy the exact returned commission_ref and projection_revision into expected_projection_revision, with a stable command_id. The host revalidates CAS, saved evidence and original approvals. This never approves paid generation, user choices or unavailable audio review; preserve existing results and stop while tasks run.", "POST", "advance-production", {"commission_ref": ref, "expected_projection_revision": {"type": "string", "pattern": "^[a-f0-9]{64}$"}, "command_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,128}$"}}, ["commission_ref", "expected_projection_revision", "command_id"], False, False),
        ("freezone_select_material_work", "Select the user's explicitly chosen existing material work for this commission using current exact revisions and a stable command_id. Keep branches independent and all history intact. Never select by latest revision, create duplicate work, infer content approval, or treat selection as permission to generate.", "POST", "select-material-work", {"commission_ref": ref, "work_ref": exact_ref, "command_id": {**text, "maxLength": 128}}, ["commission_ref", "work_ref", "command_id"], False, False),
        ("freezone_get_material_speech_review", "Read a technically valid candidate's full-source raw ASR, target dialogue differences and any authenticated saved correction before adoption. Propose corrections with actual word IDs; the authenticated user listens and saves lexical corrections in native review. This read cannot confirm audio or alter the raw observation.", "POST", "material-speech-review", {"work_ref": exact_ref, "step_id": {**text, "maxLength": 128}, "node_id": {**text, "maxLength": 160}, "observation_ref": exact_ref}, ["work_ref", "step_id", "node_id", "observation_ref"], False, False),
        ("freezone_get_reference_evidence", "Read time-located facts from a saved reference analysis. artifact_id and revision must come from the analysis node's analysis_ref; evidence_ref and commission_ref identify different artifacts. For the next page, send the returned next_cursor as cursor and omit start_us/end_us; the cursor carries its range. Unknown motion or speech remains unknown.", "GET", "", {"artifact_id": {**text, "description": "Copy analysis_ref.artifact_id, never evidence_ref or commission_ref"}, "revision": {"type": "integer", "minimum": 1}, "start_us": {"type": "integer", "minimum": 0}, "end_us": {"type": "integer", "minimum": 1}, "cursor": text, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["artifact_id", "revision"], True, False),
        ("freezone_get_reference_director_state", "Read saved reference direction, current plan confirmation, material work and pending decisions in this canvas. This is a pending-decision summary: its material reviews list pending review, so absence is not proof that no saved passed review exists. Read freezone_get_material_work with the exact saved work_ref for actual saved material review refs and statuses. Reuse confirmed requirements; waiting for a user is a normal stage.", "GET", "state", {}, [], False, False),
        ("freezone_confirm_reference_plan", "Confirm the exact reviewed plan currently presented in ordinary chat. Call only after the user clearly approves that exact plan; the host binds this action to the active human turn and checks the saved commission, plan, review and presentation references. Do not pass an approval flag, free-form intent, user identity or message text. A successful host receipt is the only confirmation proof. This advances only the existing plan confirmation stage and does not start media generation.", "POST", "decisions/confirm-reference-plan", {"commission_ref": ref, "plan_ref": exact_ref}, ["commission_ref", "plan_ref"], False, False),
        ("freezone_prepare_reference_decision", "Save a plan, material or editable-composition proposal. Direction stays in ordinary chat and is committed by freezone_prepare_production_nodes; never use this tool to recover a rejected direction submission. A passed freezone_review_production_plan automatically registers its exact plan confirmation target; present that plan in ordinary chat, without manually preparing another plan decision. This tool never confirms for the user. Read current commission_ref first. For editable scope, show its current manifest_ref and compose_node_id; do not require an export.", "POST", "decisions", {"commission_ref": ref, "command_id": text, "kind": {"type": "string", "enum": ["plan", "material", "composition"]}, "proposal": {**obj, "description": "General proposal content. For an explicitly preserved visual identity use preserve_replace entries with stable id, entity_kind, entity_id, action=preserve and visual_identity=true; story-structure or relationship preservation does not set visual_identity. The later material requirement must bind each such choice to a real image source. A reviewed plan decision is normally created by plan review. A direct kind=plan retry must copy the current returned commission.plan_ref exactly, including project_id."}, "summary": text}, ["commission_ref", "command_id", "kind", "proposal", "summary"], False, False),
        ("freezone_review_production_plan", 'Review inspected real saved plan content against confirmed direction, covering all eight finding categories. Use one actual entity ID per finding; entity_kind plan uses entity_id plan. Never use pipe-joined, wildcard or imagined IDs. Supply concrete nonblank reason and evidence. Read review_context.requirements from current production state. Every semantic entry needs a finding with its exact requirement_id and evidence_refs pointing to a real saved content entity and field (purpose, text, description, media_path or graphic.text); the host resolves its value. A direction finding for an expanded plan goal also cites plan.goal and one real content entity. Explain the conclusion in reason and evidence; do not copy an entire saved field just to pass a string match. Missing or unsupported requirements block review. Use change or unsupported for content that cannot pass. This tool saves the review and advances automatically; user confirmation remains required. Do not invent an advance event. Placeholder finding example: {"category":"direction","entity_kind":"plan","entity_id":"plan","verdict":"unsupported","reason":"REPLACE with the actual review conclusion","evidence":["REPLACE with inspected saved plan content and confirmed direction"]}. Replace placeholder text with real observations. Schema-valid alone does not approve creative content.', "POST", "plan-review", {"commission_ref": ref, "plan_ref": ref, "findings": {"type": "array", "items": review_finding}}, ["commission_ref", "plan_ref", "findings"], False, False),
        ("freezone_prepare_reference_branches", "Register independently reviewed production branches for multiple real saved plans descended from one reference analysis. The parent's actual confirmed direction is inherited; each plan keeps its own content approval, materials and progress. No plan is overwritten.", "POST", "branches", {"commission_ref": ref, "plan_refs": {"type": "array", "items": ref, "minItems": 1, "maxItems": 100}, "command_id": text}, ["commission_ref", "plan_refs", "command_id"], False, False),
        ("freezone_prepare_material_work", "Plan the dependency graph that produces the confirmed plan's final media: shared identity/product/setting/voice references, intermediate images, generated video or uploaded inputs. Use exact two-key plan_ref (artifact_id, revision); project_id belongs at tool scope. Requirements require id, description, media_kind and final_step_id plus actual saved placement/take/turn IDs; steps require id, label, media_kind and source. Generated audio/video dialogue comes only from ordered dialogue_turn_ids in the saved plan; keep prompt for creative direction and leave the IDs empty for silent or reference-only steps. Shared references are separate steps linked by depends_on/references, not invented placement_id/output_kind/step_kind/uses fields. Generate uses a matching general-image/video/audio Recipe and nonblank prompt. Set select_for_commission=true when creating the actual work for this confirmed commission; default false saves a candidate only. Association does not approve content or generation. This call saves the material work package and its 4-15 second video plans; it does not create or run a native generation task. Return the saved work_ref and generation_readiness, then use the existing parameter card and canvas confirmation before native generation. Only ready generation steps become an existing workflow draft; execution and acceptance remain server owned. Read material_lifecycle to distinguish saved package, ready draft, canvas delivery and actual generation. " + _MATERIAL_ADMISSION_FLOW, "POST", "material-work", {"commission_ref": ref, "plan_ref": exact_ref, "requirements": {"type": "array", "items": material_requirement, "minItems": 1, "maxItems": 200}, "steps": {"type": "array", "items": material_step, "minItems": 1, "maxItems": 200}, "command_id": {**text, "maxLength": 128}, "select_for_commission": {"type": "boolean", "default": False}, **material_draft_options}, ["commission_ref", "plan_ref", "requirements", "steps", "command_id"], False, True),
        ("freezone_get_material_work", "Read the current material work head, pending uploads, accepted shared references and ready next steps. Read material_reviews before another review. matches_current_work_revision=true identifies the exact basis. Historical records retain their original work_ref; only host-certified review_compatible_with_current_work=true permits an unchanged independent existing item across genuine native acceptance bookkeeping. Accept using the returned current work_ref head plus the original eligible passed review_ref and exact resource; never rewrite its basis or infer compatibility yourself. Other historical records require native rechecking. Do not infer unobserved audio passed. If material_reviews_truncated is true, absence is not proof of no review. Completed generation remains a candidate until content review.", "POST", "material-state", {"work_ref": exact_ref}, ["work_ref"], False, False),
        ("freezone_prepare_material_stage", "Prepare the next ready generation wave from a saved material work package. Optionally provide exact saved placement_ids to include only their dependency closure; already accepted dependencies are reused and unrelated placements are excluded. Uses the existing workflow approval and associates the real draft with the material steps. Read material_lifecycle before claiming delivery or generation; a ready draft still requires the existing canvas confirmation. " + _MATERIAL_ADMISSION_FLOW, "POST", "material-stage", {"work_ref": exact_ref, "placement_ids": {"type": "array", "items": text, "maxItems": 200, "uniqueItems": True}, **material_draft_options}, ["work_ref"], False, True),
        ("freezone_review_material_candidate", "Inspect a real current-canvas candidate using native media evidence. Technical checks and content observation are separate. If content review is unavailable, ask for the specific review; never assert that unseen media passed.", "POST", "material-review", {"work_ref": exact_ref, "step_id": text, "node_id": text}, ["work_ref", "step_id", "node_id"], False, False),
        ("freezone_accept_material_candidate", "Accept only saved passed technical/content review. After browser confirmation read freezone_get_material_work, not a cached pre-confirmation review_ref. Use its current work_ref head and exact resource with an original passed review_ref whose matches_current_work_revision=true or host-certified review_compatible_with_current_work=true. Preserve the original review basis and user receipt; never guess compatibility or carry approval to another work/resource. For material whose content review is unavailable (audio/video the user must judge by listening/watching) or otherwise not yet content-passed: once the authenticated user explicitly confirms in ordinary chat that they listened to / watched and approve this exact material, call with user_confirmed=true and THIS current review_ref (it need not already be content-passed; the host records the session user's content-review acceptance before binding) — do not send the user to the canvas merely to click confirm. Set user_confirmed only after a real user confirmation, never on your own initiative. Returns final binding candidates and ready dependent steps; intermediate references are not automatically bound to the film.", "POST", "material-accept", {"work_ref": exact_ref, "step_id": text, "resource": obj, "review_ref": exact_ref, "command_id": text, "user_confirmed": {"type": "boolean", "default": False, "description": "Set true only after the authenticated user explicitly confirmed in chat that they viewed/listened to and approve this exact material (works even when content review is unavailable); the host uses the session user to record the content-review acceptance."}}, ["work_ref", "step_id", "resource", "review_ref", "command_id"], False, False),
        ("freezone_retry_material_step", "Invalidate only the failed or rejected material step and its dependents, preserving accepted independent work and previous candidates. Use a current saved work revision and stable command_id; prepare the next ready stage after this operation, within the agreed generation scope.", "POST", "material-retry", {"work_ref": exact_ref, "step_id": text, "reason": text, "command_id": text}, ["work_ref", "step_id", "reason", "command_id"], False, False),
        ("freezone_revise_material_strategy", "Replace complete steps in one current saved material work revision. This changes source strategy; retry_material_step only retries the existing strategy. Use current work_ref, batch related replacements, and keep accepted independent material. This saves a revision but does not start generation or bypass native approval.", "POST", "material-strategy", {"work_ref": exact_ref, "replacements": {"type": "array", "items": obj, "minItems": 1, "maxItems": 200}, "command_id": {**text, "maxLength": 128}}, ["work_ref", "replacements", "command_id"], False, False),
        ("freezone_associate_material_upload", "Associate an explicitly selected uploaded canvas resource with its saved pending material step. Verifies project/canvas media, retains the requirement identity, and makes it a candidate for content review; uploading does not automatically certify quality.", "POST", "material-upload", {"work_ref": exact_ref, "step_id": text, "node_id": text}, ["work_ref", "step_id", "node_id"], False, False),
        ("freezone_get_speech_review", "Read intended dialogue, raw ASR and real source-word times for one complete shared speaking take. Page words; do not transcribe each speaker separately or cut at the estimated beat duration.", "POST", "speech-review", {"plan_ref": ref, "take_id": text, "recognition_ref": ref, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 500}}, ["plan_ref", "take_id", "recognition_ref"], False, False),
        ("freezone_prepare_speech_correction", "Prepare once a correction against the plan's original words while retaining raw ASR and measured timings. Corrections are [{word_id,text,reason}]. Lexical changes need audio evidence or a real user receipt; missing performed words require material repair, never fabricated SRT or evenly invented times.", "POST", "speech-correction", {"plan_ref": ref, "take_id": text, "recognition_ref": ref, "corrections": {"type": "array", "items": obj}}, ["plan_ref", "take_id", "recognition_ref", "corrections"], False, False),
        ("freezone_review_production_output", "Inspect the actual current encoded render and native operation/manifest proof. Return measured technical checks and a playable review; when content/audio inspection is unavailable the user reviews the actual file. Do not certify an old export or claim unobserved sound passed.", "POST", "output-review", {"commission_ref": ref, "compose_node_id": text}, ["commission_ref", "compose_node_id"], False, False),
    ]
    director_fields = ("selection_status", "analysis_ref", "evidence_ref", "transcript_ref", "range_us", "items", "next_cursor", "coverage", "transcript_status", "limitations", "unknowns", "requested_frame_coverage", "commissions", "decisions", "material_work", "material_lifecycle", "material_reviews", "material_reviews_truncated", "output_reviews", "commission_ref", "decision_ref", "decision", "review_ref", "review", "commission", "work_ref", "current_ref", "ready_step_ids", "changed_step_ids", "affected_step_ids", "workflow_intent", "work", "generation_readiness", "binding_candidates", "resource", "preview_ref", "source_plan_ref", "recognition_ref", "take_id", "take", "target_turns", "source_duration_us", "recognized_words", "total_words", "next_offset", "audio_review_available", "actor_id", "actor_kind", "corrections", "corrected_words", "cues", "blockers", "lexical_change", "node_id", "compose_node_id", "plan_ref", "instruction", "agent_instruction", "schema_version", "project_id", "canvas_id", "requester_user_id", "analysis_node_id", "analysis_evidence", "goal", "phase", "production_scope", "direction", "direction_revision", "direction_fingerprint", "plan_ref", "review_ref", "work_ref", "manifest_ref", "output_review_ref", "next_action", "next_tool", "manifest_ref", "operation_id", "media_url", "media", "technical", "content", "findings", "errors", "issues", "placement_id", "turn_ids", "step_ids", "decision_receipt", "message_id", "human_message_id", "turn_id", "replayed", "side_effects")
    director_fields += ("blocker", "settlement_key", "branches", "selection_required", "scope", "generation_scope", "progress", "execution_status", "execution_problem",
                        "ready_system_actions", "projection_revision", "director_state", "material_steps",
                        "candidate_work_refs", "suggested_work_ref", "inventory_truncated", "plan_review_valid",
                        "speech_estimates", "plan_diagnostics", "review_context", "model_capabilities",
                        "visual_preservation_status",
                        "advancement", "advancement_blocker", "step_id", "summary", "observation_ref", "correction_ref",
                        *_RECOVERY_FIELDS)
    for name, *_ in specs:
        host["_RESULT_FIELDS"][name] = tuple(dict.fromkeys((*director_fields, *host["_RESULT_FIELDS"]["freezone_prepare_workflow_draft"])))
        required_results = {
            "freezone_list_reference_media": ("items",),
            "freezone_get_production_progress": ("project_id", "canvas_id"),
            "freezone_advance_production": ("commission_ref", "phase", "execution_status", "progress", "projection_revision"),
            "freezone_select_material_work": ("commission_ref", "phase"),
            "freezone_get_material_speech_review": ("work_ref", "plan_ref", "step_id", "node_id", "summary"),
            "freezone_get_reference_evidence": ("analysis_ref", "items", "coverage"),
            "freezone_get_reference_director_state": ("commissions", "decisions", "material_work"),
            "freezone_confirm_reference_plan": ("commission", "decision_ref", "message_id"),
            "freezone_prepare_reference_decision": ("decision_ref", "decision"),
            "freezone_review_production_plan": ("review_ref", "review", "commission"),
            "freezone_prepare_reference_branches": ("commission", "branches"),
            "freezone_prepare_material_work": ("work_ref", "ready_step_ids"),
            "freezone_prepare_material_stage": ("work_ref", "ready_step_ids"),
            "freezone_get_material_work": ("work_ref", "work"),
            "freezone_review_material_candidate": ("review_ref", "review"),
            "freezone_accept_material_candidate": ("work_ref", "binding_candidates"),
            "freezone_retry_material_step": ("work_ref", "ready_step_ids", "work"),
            "freezone_revise_material_strategy": ("work_ref", "changed_step_ids", "affected_step_ids"),
            "freezone_associate_material_upload": ("work_ref", "work"),
            "freezone_get_speech_review": ("source_plan_ref", "recognition_ref", "recognized_words"),
            "freezone_prepare_speech_correction": ("preview_ref", "corrected_words", "blockers"),
            "freezone_review_production_output": ("review_ref", "manifest_ref", "media_url", "technical"),
        }
        host["_RESULT_FIELDS"][name] += ("requires_user_review", "content_observation", "branches", "plan_branches", "parent_commission_ref")
        host["_RESULT_SUCCESS_REQUIRED"][name] = required_results[name]
    return tuple((name, host["_schema"](name, description, {**scopes, **properties}, required, reject_unknown=True),
                  handler(name, method, suffix, list(properties), evidence=evidence, material=material))
                 for name, description, method, suffix, properties, required, evidence, material in specs)
