"""Compile the native plan stage after a real reference analysis is available."""
from __future__ import annotations

from copy import deepcopy
from typing import Any


NATIVE_PRODUCTION_CONTRACT = {
    "prepare_tool": "freezone_prepare_production_nodes",
    "analysis_action": "run_video_analyze_story",
    "analysis_submit_tool": "freezone_start_reference_analysis",
    "analysis_node_type": "videoStoryNode",
    "external_input_kind": "reference_analysis",
    "stage": "production_plans",
    "direction_tool": None,
    "direction_guidance": "Discuss preserve/replace choices in ordinary chat. At submission use the concise agreed goal with prepare_tool; do not create a separate direction decision or invent details left to AI drafting.",
    "state_tool": "freezone_get_reference_director_state",
    "review_tool": "freezone_review_production_plan",
    "draft_action": {
        "tool": "freezone_run_node_action",
        "action": "generate_production_plan_draft",
        "node_type": "scriptNode",
        "remix_role": "production_plan",
        "node_id_source": "native_plan_stage.slots[].plan_node_id",
    },
    "arrangement_action": {
        "tool": "freezone_run_node_action",
        "action": "generate_production_plan_arrangement",
        "parameters": {"plan_ref": {"artifact_id": "CURRENT_SAVED_ID", "revision": 1}},
        "guidance": "Explicit separately authorized action on the same node after reading/editing saved structure and dialogue; never submit from a completion notification.",
    },
    "accepted_next_action": "await_completion_event",
    "material_work_tool": "freezone_prepare_material_work",
    "speech_review_tool": "freezone_get_speech_review",
    "execution": "Respect the approved draft's run_after_create policy: true submits drafting in the creation batch; false leaves a bound plan ready for its separate action. Bind media and accept speech evidence before compiling composition in a later run.",
    "request_example": {
        "commission_ref": {"artifact_id": "COPY_ACTUAL_SAVED_COMMISSION_ID", "revision": 1},
        "goal": "The complete concise creative direction agreed in conversation",
        "run_after_create": True,
        "plan_count": 1,
    },
    # Retained for internal compiler validation/recovery; native tool discovery
    # exposes request_example because the entry resolves its own input bindings.
    "intent_example": {
        "schema_version": "freezone_workflow_intent.v1",
        "skill_id": "reference-video-remix",
        "user_goal": "The user's recreation goal",
        "inputs": {"plan_count": 1},
        "reference_commission_id": "COPY_ACTUAL_SAVED_COMMISSION_ID",
        "external_inputs": [{"id": "analysis", "node_id": "COPY_ACTUAL_COMPLETED_ANALYSIS_NODE_ID", "media_kind": "reference_analysis"}],
    },
}


def supports_native_production(skill: dict[str, Any]) -> bool:
    return (
        skill.get("id") == "reference-video-remix"
        and str(skill.get("version", "")).split(".")[0] == "2"
        and "reference-production-plan" in (skill.get("allowed_recipe_ids") or [])
    )


def compile_reference_production_plan(
    intent: dict[str, Any], skill: dict[str, Any], inputs: dict[str, Any], user_goal: str,
) -> dict[str, Any]:
    # The host resolves this node on the authenticated, revision-pinned canvas.
    # Never take generated analysis prose or invented artifact refs from the LLM.
    sources = intent.get("external_inputs")
    if (not isinstance(sources, list) or len(sources) != 1
            or not isinstance(sources[0], dict)
            or sources[0].get("media_kind") != "reference_analysis"):
        return {
            "ok": False, "status": "reference_analysis_binding_required",
            "error": "作品方案缺少当前已完成原片分析节点的绑定。先读取制作状态，再把实际 videoStoryNode ID 放入 external_inputs，media_kind=reference_analysis。",
            "next_action": "read_current_production_state",
        }
    count = inputs.get("plan_count", 1)
    if type(count) not in (int, float) or not float(count).is_integer() or not 1 <= count <= 100:
        return {"ok": False, "status": "invalid_workflow_intent", "error": "plan_count must be an integer between 1 and 100"}
    planner = intent.get("planner")
    if planner not in (None, {}, {"mode": "standard"}):
        return {"ok": False, "status": "invalid_workflow_intent", "error": "Native production uses inputs.plan_count and production_scope; omit generic media planner fields."}
    topic = str(inputs.get("new_topic") or user_goal).strip()
    guidance = [topic]
    if topic != user_goal:
        guidance.append(f"完整创作要求：{user_goal}")
    for field, label in (("preserve_replace", "保留与改变"), ("speech_mode", "声音方式")):
        value = inputs.get(field)
        if value:
            guidance.append(f"{label}：{value}")
    topic = "\n".join(guidance)
    output_preferences = {
        field: inputs[field]
        for field in ("aspect_ratio", "video_aspect_ratio")
        if inputs.get(field) is not None
    }
    if inputs.get("video_duration_seconds") is not None:
        # This setting governs one generated source clip, not the total film.
        output_preferences["source_clip_duration_seconds"] = inputs["video_duration_seconds"]
    compose = inputs.get("production_scope") != "仅作品方案" and intent.get("include_compose") is not False
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    for index in range(1, int(count) + 1):
        plan_id = f"production_plan_{index}"
        nodes.append({
            "id": plan_id, "node_type": "scriptNode", "name": f"作品方案 {index}", "stage": "production_plans",
            "data": {"displayName": f"作品方案 {index}", "remixRole": "production_plan",
                     "semanticOutputRole": "production_plan", "processingAction": "generate_production_plan_draft",
                     "processingPayload": {"topic": topic, **({"output_preferences": output_preferences} if output_preferences else {})}, "remixNodeRevision": 1},
        })
        edges.append({"source": sources[0].get("id", ""), "target": plan_id, "link_type": "media_input_for"})
        if compose:
            compose_id = f"composition_{index}"
            nodes.append({
                "id": compose_id, "node_type": "videoComposeNode", "name": f"视频合成 {index}", "stage": "composition",
                "data": {"displayName": f"视频合成 {index}", "processingAction": "compile_production_plan"},
            })
            edges.append({"source": plan_id, "target": compose_id, "link_type": "composition_input_for"})
    return {"ok": True, "plan": {
        "schema_version": "freezone_workflow_plan.v1", "workflow_type": "dynamic.reference-video-remix",
        "skill": {"id": skill["id"], "version": skill.get("version")},
        "title": str(intent.get("title") or skill.get("name") or "参考视频复刻"),
        "summary": user_goal, "inputs": deepcopy(inputs), "external_inputs": deepcopy(sources),
        "phases": ["production_plans", *(["composition"] if compose else [])], "nodes": nodes, "edges": edges,
        "execution_policy": {"requires_user_confirmation": True, "auto_create_nodes": False,
                             "auto_generate_content": False, "handoff_tool": "freezone_prepare_workflow_draft"},
    }}


def native_entry_intent(request, commission: dict[str, Any]) -> dict[str, Any]:
    """Adapt authenticated saved facts to the existing compact native intent."""
    return {
        "schema_version": "freezone_workflow_intent.v1",
        "skill_id": "reference-video-remix", "user_goal": request.goal,
        "inputs": {"plan_count": request.plan_count, "new_topic": request.goal,
                   "production_scope": commission["production_scope"]},
        "reference_commission_id": commission["commission_ref"]["artifact_id"],
        "external_inputs": [{"id": "analysis", "node_id": commission["analysis_node_id"],
                             "media_kind": "reference_analysis"}],
    }
