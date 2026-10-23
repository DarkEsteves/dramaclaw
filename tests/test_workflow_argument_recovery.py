from copy import deepcopy

import pytest
from mcp import types

from novelvideo.chat import workflow_mcp
from novelvideo.freezone.workflow_schema import normalize_workflow_tool_arguments


@pytest.mark.parametrize("name", ["workflow_intent_compile", "freezone_prepare_workflow_draft"])
def test_repairs_contiguous_items_and_optional_null_duration_without_mutation(name):
    args = {"intent": {
        "items[1]": {"id": "video", "duration_seconds": 5},
        "items[0]": {"id": "text", "duration_seconds": None},
        "planner": {"units": [{"title": "shot", "duration_seconds": None}]},
    }}
    original = deepcopy(args)
    result = normalize_workflow_tool_arguments(name, args)
    assert result["intent"]["items"] == [{"id": "text"}, {"id": "video", "duration_seconds": 5}]
    assert result["intent"]["planner"]["units"] == [{"title": "shot"}]
    assert args == original


@pytest.mark.parametrize("name", ["workflow_intent_compile", "freezone_prepare_workflow_draft"])
def test_canonicalizes_unambiguous_generation_input_scalar_types(name):
    args = {"intent": {"inputs": {
        "image_variants_per_node": "1",
        "video_variants_per_node": "4",
        "video_duration_seconds": "5.5",
        "video_generate_audio": "false",
        "skill_specific_value": "1",
    }}}
    original = deepcopy(args)

    result = normalize_workflow_tool_arguments(name, args)

    assert result["intent"]["inputs"] == {
        "image_variants_per_node": 1,
        "video_variants_per_node": 4,
        "video_duration_seconds": 5.5,
        "video_generate_audio": False,
        "skill_specific_value": "1",
    }
    assert args == original


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("image_variants_per_node", "1.0"),
        ("video_variants_per_node", "-1"),
        ("image_variants_per_node", "9" * 5000),
        ("video_duration_seconds", "5 seconds"),
        ("video_duration_seconds", "9" * 5000),
        ("video_generate_audio", "False"),
    ],
)
def test_does_not_guess_ambiguous_generation_input_scalar_types(key, value):
    args = {"intent": {"inputs": {key: value}}}
    assert normalize_workflow_tool_arguments("workflow_intent_compile", args) == args


@pytest.mark.parametrize("intent", [
    {"items[1]": {}},
    {"items": [], "items[0]": {}},
    {"items[01]": {}},
    {"items": [{"duration_seconds": "5", "id": None}]},
])
def test_ambiguous_or_semantic_values_are_not_repaired(intent):
    args = {"intent": intent}
    assert normalize_workflow_tool_arguments("workflow_intent_compile", args) == args


@pytest.mark.asyncio
async def test_compile_boundary_normalizes_before_validating(monkeypatch):
    seen = []

    def compile_intent(intent):
        seen.append(intent)
        return {"ok": False, "status": "sentinel", "error": "compiler reached"}

    monkeypatch.setattr(workflow_mcp, "compile_workflow_intent", compile_intent)
    result = await workflow_mcp.call_tool("workflow_intent_compile", {"intent": {
        "skill_id": "test", "user_goal": "test",
        "items[0]": {"id": "text", "title": "Text", "recipe_id": "general-text",
                     "duration_seconds": None},
    }})
    assert result.structuredContent["status"] == "sentinel"
    assert "duration_seconds" not in seen[0]["items"][0]


@pytest.mark.asyncio
async def test_compile_boundary_canonicalizes_generation_counts_before_validation(monkeypatch):
    seen = []

    def compile_intent(intent):
        seen.append(intent)
        return {"ok": False, "status": "sentinel", "error": "compiler reached"}

    monkeypatch.setattr(workflow_mcp, "compile_workflow_intent", compile_intent)
    result = await workflow_mcp.call_tool("workflow_intent_compile", {"intent": {
        "skill_id": "test",
        "user_goal": "test",
        "inputs": {
            "image_variants_per_node": "1",
            "video_variants_per_node": "2",
        },
    }})

    assert result.structuredContent["status"] == "sentinel"
    assert seen[0]["inputs"] == {
        "image_variants_per_node": 1,
        "video_variants_per_node": 2,
    }


@pytest.mark.asyncio
async def test_compile_boundary_rejects_oversized_generation_count_without_crashing(
    monkeypatch,
):
    def must_not_compile(_intent):
        raise AssertionError("invalid input reached compiler")

    monkeypatch.setattr(workflow_mcp, "compile_workflow_intent", must_not_compile)
    result = await workflow_mcp.call_tool("workflow_intent_compile", {"intent": {
        "skill_id": "test",
        "user_goal": "test",
        "inputs": {"image_variants_per_node": "9" * 5000},
    }})

    assert result.isError
    assert result.structuredContent["status"] == "tool_arguments_invalid"


@pytest.mark.asyncio
async def test_compile_boundary_rejects_conflict_without_calling_compiler(monkeypatch):
    def must_not_compile(_intent):
        raise AssertionError("invalid input reached compiler")

    monkeypatch.setattr(workflow_mcp, "compile_workflow_intent", must_not_compile)
    result = await workflow_mcp.call_tool("workflow_intent_compile", {"intent": {
        "skill_id": "test", "user_goal": "test", "items": [], "items[0]": {},
    }})
    assert result.isError
    assert result.structuredContent["status"] == "tool_arguments_invalid"
    assert "intent" in result.structuredContent["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["workflow", "dramaclaw"])
async def test_sdk_boundary_repairs_before_validation_but_keeps_required_fields(monkeypatch, adapter):
    import json
    from novelvideo.chat import dramaclaw_mcp
    from novelvideo.freezone.workflow_schema import workflow_intent_json_schema

    seen = []
    if adapter == "workflow":
        server = workflow_mcp.SERVER
        name = "workflow_intent_compile"

        def compiler(intent):
            seen.append(intent)
            return {"ok": False, "status": "sentinel", "error": "compiler reached"}

        monkeypatch.setattr(workflow_mcp, "compile_workflow_intent", compiler)
    else:
        server = dramaclaw_mcp.SERVER
        name = "freezone_prepare_workflow_draft"
        monkeypatch.setenv("DRAMACLAW_TOOL_MODE", "freezone_canvas")
        monkeypatch.setenv("DRAMACLAW_PROJECT_ID", "project-a")
        real_schema = dramaclaw_mcp._agent_tools()[name][0]

        def handler(args):
            seen.append(args["intent"])
            return json.dumps({"ok": False, "status": "sentinel", "error": "handler reached"})

        monkeypatch.setattr(dramaclaw_mcp, "_agent_tools", lambda: {name: ({
            **real_schema,
            "parameters": {"type": "object", "properties": {
                "intent": workflow_intent_json_schema(),
            }, "required": ["intent"], "additionalProperties": False},
        }, handler)})

    sdk_handler = server.request_handlers[types.CallToolRequest]
    response = await sdk_handler(types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(
        name=name, arguments={"intent": {"skill_id": "test", "user_goal": "test",
            "items[0]": {"id": "a", "title": "A", "recipe_id": "general-text",
                         "duration_seconds": None}}},
    )))
    assert len(seen) == 1, response
    assert seen[0]["items"] == [{"id": "a", "title": "A", "recipe_id": "general-text"}]
    response = await sdk_handler(types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(
        name=name, arguments={"intent": {"skill_id": "test"}},
    )))
    assert len(seen) == 1
    assert response.root.isError


@pytest.mark.asyncio
@pytest.mark.parametrize('adapter', ['workflow', 'dramaclaw'])
async def test_html_missing_prompt_reports_repair_without_changing_node_type(monkeypatch, adapter):
    import json
    from novelvideo.chat import dramaclaw_mcp
    from novelvideo.freezone.workflow_schema import workflow_plan_json_schema

    plan = {'schema_version': 'freezone_workflow_plan.v1', 'skill': {'id': 'page'},
            'nodes': [{'id': 'page', 'node_type': 'htmlArtifactNode',
                       'data': {'workflowCatalog': {'recipeId': 'page-recipe'}}}], 'edges': []}
    if adapter == 'workflow':
        result = await workflow_mcp.call_tool('workflow_graph_compile', {'plan': plan})
        payload = result.structuredContent
    else:
        monkeypatch.setenv('DRAMACLAW_TOOL_MODE', 'freezone_canvas')
        monkeypatch.setenv('DRAMACLAW_PROJECT_ID', 'project-a')
        name = 'freezone_prepare_workflow_plan_draft'
        schema = dramaclaw_mcp._agent_tools()[name][0]
        def must_not_run(_args):
            raise AssertionError('Invalid HTML reached handler')
        monkeypatch.setattr(dramaclaw_mcp, '_agent_tools', lambda: {name: ({**schema,
            'parameters': {'type': 'object', 'properties': {'plan': workflow_plan_json_schema()}, 'required': ['plan']}}, must_not_run)})
        result = await dramaclaw_mcp.call_tool(name, {'plan': plan})
        payload = json.loads(result.content[0].text)
    text = json.dumps(payload)
    assert 'plan.nodes[0].prompt' in text
    assert 'data.prompt' in text
    assert 'Keep node_type=htmlArtifactNode' in text
    assert plan['nodes'][0]['node_type'] == 'htmlArtifactNode'
    assert 'prompt' not in plan['nodes'][0]['data']


def _plan_with(node):
    return {
        "schema_version": "freezone_workflow_plan.v1",
        "skill": {"id": "page"},
        "nodes": [node],
        "edges": [],
    }


@pytest.mark.parametrize(
    "name",
    ["freezone_prepare_workflow", "freezone_prepare_workflow_plan_draft", "workflow_graph_compile"],
)
@pytest.mark.parametrize("top_level_stage", [None, "storyboard"])
def test_moves_node_data_stage_to_top_level_without_mutation(name, top_level_stage):
    node = {
        "id": "shot",
        "node_type": "imageGenNode",
        "data": {"prompt": "p", "stage": "storyboard", "workflowCatalog": {"recipeId": "r"}},
    }
    if top_level_stage:
        node["stage"] = top_level_stage
    args = {"plan": _plan_with(node)}
    original = deepcopy(args)

    result = normalize_workflow_tool_arguments(name, args)

    moved = result["plan"]["nodes"][0]
    assert moved["stage"] == "storyboard"
    assert "stage" not in moved["data"]
    assert args == original


def test_conflicting_node_stage_is_not_merged():
    node = {
        "id": "shot",
        "node_type": "videoNode",
        "stage": "video",
        "data": {"stage": "storyboard", "workflowCatalog": {"recipeId": "r"}},
    }
    args = {"plan": _plan_with(node)}
    assert normalize_workflow_tool_arguments("workflow_graph_compile", args) == args
    assert normalize_workflow_tool_arguments("workflow_skill_get", args) == args


@pytest.mark.parametrize(
    ("node", "path", "fragment"),
    [
        (
            {"id": "shot", "node_type": "videoNode", "stage": "video",
             "data": {"prompt": "p", "stage": "storyboard",
                      "workflowCatalog": {"recipeId": "r"}}},
            "plan.nodes[0].data.stage",
            "plan.nodes[0].stage is already set to a different value",
        ),
        (
            {"id": "shot", "node_type": "videoNode", "scene": "s1",
             "data": {"workflowCatalog": {"recipeId": "r"}}},
            "plan.nodes[0].scene",
            "allowed keys: content, data",
        ),
        (
            {"id": "shot", "node_type": "videoNode", "data": {"prompt": "p"}},
            "plan.nodes[0].data.workflowCatalog",
            "field is required",
        ),
        (
            {"id": "shot", "node_type": "movieNode", "data": {}},
            "plan.nodes[0].node_type",
            "allowed values:",
        ),
    ],
)
@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["workflow", "dramaclaw"])
async def test_invalid_node_reports_field_path_instead_of_echoing_node(
    monkeypatch, adapter, node, path, fragment
):
    import json
    from novelvideo.chat import dramaclaw_mcp
    from novelvideo.freezone.workflow_schema import workflow_plan_json_schema

    plan = _plan_with(node)
    if adapter == "workflow":
        result = await workflow_mcp.call_tool("workflow_graph_compile", {"plan": plan})
        payload = result.structuredContent
        message = payload["error"]
        assert payload["errors"][0]["path"] == path
    else:
        monkeypatch.setenv("DRAMACLAW_TOOL_MODE", "freezone_canvas")
        monkeypatch.setenv("DRAMACLAW_PROJECT_ID", "project-a")
        name = "freezone_prepare_workflow_plan_draft"
        schema = dramaclaw_mcp._agent_tools()[name][0]

        def must_not_run(_args):
            raise AssertionError("invalid plan reached handler")

        monkeypatch.setattr(dramaclaw_mcp, "_agent_tools", lambda: {name: ({
            **schema,
            "parameters": {"type": "object", "properties": {
                "plan": workflow_plan_json_schema(),
            }, "required": ["plan"]},
        }, must_not_run)})
        result = await dramaclaw_mcp.call_tool(name, {"plan": plan})
        payload = json.loads(result.content[0].text)
        message = payload["message"]
        assert payload["path"] == path
    assert message.startswith(f"{path}: ")
    assert fragment in message
    assert "is not valid under any of the given schemas" not in message


@pytest.mark.asyncio
async def test_node_data_stage_reaches_compiler_as_top_level_stage(monkeypatch):
    seen = []

    def validate(plan):
        seen.append(plan)
        return {"ok": False, "status": "sentinel", "errors": []}

    monkeypatch.setattr(workflow_mcp, "validate_agent_workflow_plan", validate)
    await workflow_mcp.call_tool("workflow_graph_compile", {"plan": _plan_with({
        "id": "shot", "node_type": "imageGenNode", "prompt": "p",
        "data": {"stage": "storyboard", "workflowCatalog": {"recipeId": "r"}},
    })})

    assert seen[0]["nodes"][0]["stage"] == "storyboard"
    assert "stage" not in seen[0]["nodes"][0]["data"]


@pytest.mark.parametrize(
    ("stage", "fragment"),
    [
        (3, "a stage label must be a string at plan.nodes[0].stage"),
        ({"name": "storyboard"}, "a stage label must be a string at plan.nodes[0].stage"),
    ],
)
def test_data_stage_hint_only_claims_conflict_when_top_level_stage_differs(stage, fragment):
    from novelvideo.freezone.workflow_schema import (
        workflow_plan_json_schema,
        workflow_plan_schema_diagnostics,
    )

    args = {"plan": _plan_with({
        "id": "shot", "node_type": "videoNode",
        "data": {"stage": stage, "workflowCatalog": {"recipeId": "r"}},
    })}
    schema = {"type": "object", "properties": {"plan": workflow_plan_json_schema()}}

    issues = workflow_plan_schema_diagnostics(
        normalize_workflow_tool_arguments("workflow_graph_compile", args), schema
    )

    assert issues == [{
        "path": "plan.nodes[0].data.stage",
        "message": f"field is not allowed for node_type videoNode; {fragment}",
    }]


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["workflow", "dramaclaw"])
async def test_node_diagnostics_keep_plan_level_errors(monkeypatch, adapter):
    import json
    from novelvideo.chat import dramaclaw_mcp
    from novelvideo.freezone.workflow_schema import workflow_plan_json_schema

    plan = _plan_with({
        "id": "shot", "node_type": "videoNode", "scene": "s1",
        "data": {"workflowCatalog": {"recipeId": "r"}},
    })
    del plan["skill"]
    if adapter == "workflow":
        result = await workflow_mcp.call_tool("workflow_graph_compile", {"plan": plan})
        payload = result.structuredContent
        message = payload["error"]
        assert {issue["path"] for issue in payload["errors"]} == {
            "plan.skill", "plan.nodes[0].scene",
        }
    else:
        monkeypatch.setenv("DRAMACLAW_TOOL_MODE", "freezone_canvas")
        monkeypatch.setenv("DRAMACLAW_PROJECT_ID", "project-a")
        name = "freezone_prepare_workflow_plan_draft"
        schema = dramaclaw_mcp._agent_tools()[name][0]

        def must_not_run(_args):
            raise AssertionError("invalid plan reached handler")

        monkeypatch.setattr(dramaclaw_mcp, "_agent_tools", lambda: {name: ({
            **schema,
            "parameters": {"type": "object", "properties": {
                "plan": workflow_plan_json_schema(),
            }, "required": ["plan"]},
        }, must_not_run)})
        result = await dramaclaw_mcp.call_tool(name, {"plan": plan})
        payload = json.loads(result.content[0].text)
        message = payload["message"]
    assert "plan.skill: field is required" in message
    assert "plan.nodes[0].scene: field is not allowed for node_type videoNode" in message


def test_native_missing_analysis_binding_reports_external_inputs():
    from novelvideo.freezone.agent_workflows.catalog import compile_workflow_intent

    result = compile_workflow_intent({
        "schema_version": "freezone_workflow_intent.v1",
        "skill_id": "reference-video-remix", "user_goal": "Create a production plan",
        "inputs": {"plan_count": 1},
    })
    assert result["status"] == "reference_analysis_binding_required"
    assert result["field_path"] == "external_inputs"
    assert result["errors"][0]["path"] == "external_inputs"


@pytest.mark.asyncio
async def test_http_prepare_preserves_native_compiler_recovery_details():
    from fastapi import HTTPException
    from novelvideo.api.routes.freezone import _prepare_workflow_source

    with pytest.raises(HTTPException) as caught:
        await _prepare_workflow_source({"intent": {
            "skill_id": "reference-video-remix", "user_goal": "Create the plan",
            "summary": "unsupported field",
        }}, {"username": "alice"})
    assert caught.value.status_code == 400
    detail = caught.value.detail
    assert detail["field_path"] == "summary"
    assert "external_inputs" in detail["allowed_fields"]
    assert detail["compact_example"]["skill_id"] == "reference-video-remix"
    assert detail["agent_instruction"]


@pytest.mark.asyncio
async def test_dramaclaw_mcp_keeps_handler_recovery_details(monkeypatch):
    import json
    from novelvideo.chat import dramaclaw_mcp

    monkeypatch.setenv("DRAMACLAW_TOOL_MODE", "freezone_canvas")
    monkeypatch.setenv("DRAMACLAW_PROJECT_ID", "project-a")
    name = "freezone_prepare_workflow_draft"
    schema = dramaclaw_mcp._agent_tools()[name][0]
    payload = {"ok": False, "status": "invalid_workflow_intent", "error": "bad field",
               "field_path": "summary", "allowed_fields": ["external_inputs"],
               "compact_example": {"skill_id": "reference-video-remix"},
               "errors": [{"path": "summary", "message": "bad field"}],
               "agent_instruction": "Correct summary", "next_action": "correct_reported_fields",
               "operation_status": "failed", "requires_new_operation": True}
    monkeypatch.setattr(dramaclaw_mcp, "_agent_tools", lambda: {name: (schema, lambda _: json.dumps(payload))})
    result = await dramaclaw_mcp.call_tool(name, {"operation_id": "op-a", "intent": {
        "skill_id": "reference-video-remix", "user_goal": "Create plan",
    }})
    assert json.loads(result.content[0].text) == payload


def test_generation_clarification_error_keeps_the_draft_its_answers_belong_to(monkeypatch):
    import json
    from novelvideo.chat import dramaclaw_mcp
    from novelvideo.chat.agent_api_errors import _http_error_result as shared_error_result

    monkeypatch.setenv("DRAMACLAW_TOOL_MODE", "freezone_canvas")
    plugin = dramaclaw_mcp._load_plugin("freezone")
    detail = {
        "status": "clarification_required",
        "code": "generation_parameters_required",
        "error": "image/video generation parameters require user clarification",
        "required_choices": {"image": ["quality"]},
        "missing_parameters": [
            {"node_id": "ref_identity_m", "node_type": "imageGenNode", "fields": ["quality"]},
        ],
        "draft_id": "workflow_draft_1",
        "revision": 3,
    }
    body = json.dumps({"detail": detail})
    for result in (
        plugin._http_error_result(400, body, "Bad Request"),
        shared_error_result(400, body, "Bad Request"),
    ):
        assert result["draft_id"] == "workflow_draft_1"
        assert result["revision"] == 3


@pytest.mark.parametrize("production_recovery", [False, True])
def test_plugin_http_error_preserves_allowlisted_recovery_fields(monkeypatch, production_recovery):
    import json
    from novelvideo.chat import dramaclaw_mcp

    monkeypatch.setenv("DRAMACLAW_TOOL_MODE", "freezone_canvas")
    plugin = dramaclaw_mcp._load_plugin("freezone")
    diagnostics = {"field_path": "external_inputs", "allowed_fields": ["external_inputs"],
                   "compact_example": {"external_inputs": [{"id": "analysis", "node_id": "COPY_ACTUAL_NODE_ID", "media_kind": "reference_analysis"}]},
                   "agent_instruction": "Read the saved reference before correcting input.",
                   "operation_status": "failed", "requires_new_operation": True}
    result = plugin._http_error_result(400, json.dumps({"detail": {
        "status": "invalid_workflow_intent", "error": "binding required", **diagnostics,
        "private_debug": "excluded",
    }}), "Bad Request", production_recovery=production_recovery)
    assert {key: result.get(key) for key in diagnostics} == diagnostics
    assert "private_debug" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("status,action,new_operation", [
    ("failed", "begin_new_workflow_result_operation", True),
    ("cancelled", "stop", False),
    ("admitting", "read_current_operation_receipt", False),
    ("delivered", "begin_new_workflow_result_operation", True),
])
async def test_workflow_http_operation_conflict_has_explicit_recovery(monkeypatch, tmp_path, status, action, new_operation):
    from types import SimpleNamespace
    from fastapi import HTTPException
    from novelvideo.api.routes import freezone

    ctx = SimpleNamespace(project_id="project-a")
    async def resolve(*_):
        return ctx, "alice", "project-a", tmp_path, tmp_path
    monkeypatch.setattr(freezone, "_resolve_freezone_project", resolve)
    monkeypatch.setattr(freezone, "_canvas_state_project_dir", lambda *_: tmp_path)
    monkeypatch.setattr(freezone, "read_agent_product_operation", lambda **_: {
        "operation_id": "op-a", "status": status, "product_kind": "workflow_result",
        "canvas_id": "canvas-a", "artifact_id": "reference-video-remix@1",
        "metadata": {"skill_id": "reference-video-remix", "skill_version": "1"},
        **({"result_ref": {"id": "missing-draft"}} if status == "delivered" else {}),
    })
    monkeypatch.setattr(freezone, "read_workflow_draft", lambda **_: (None, "draft not found"))
    with pytest.raises(HTTPException) as caught:
        await freezone.create_canvas_workflow_draft("project-a", "canvas-a", {
            "operation_id": "op-a", "intent": {"skill_id": "reference-video-remix"},
        }, {"username": "alice"})
    assert caught.value.status_code == 409
    assert caught.value.detail["operation_status"] == status
    assert caught.value.detail["next_action"] == action
    assert caught.value.detail["requires_new_operation"] is new_operation
