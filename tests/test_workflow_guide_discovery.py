from __future__ import annotations

from copy import deepcopy
import json
import os
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from novelvideo.chat import workflow_mcp
from novelvideo.freezone.agent_workflows import catalog
from novelvideo.freezone.workflow_planning import WORKFLOW_PLANNING_INSTRUCTIONS


GUIDE_REFERENCE = {
    "tool": "workflow_skill_reference_get",
    "arguments": {
        "skill_id": "reference-video-remix",
        "reference": "reference-video-remix.md",
    },
    "resource_uri": (
        "dramaclaw-workflow://skills/reference-video-remix/"
        "references/reference-video-remix.md"
    ),
}


def _canonical_guide() -> str:
    root = Path(__file__).resolve().parents[1]
    return (
        root / "src/novelvideo/agent_skills/dramaclaw-workflows/references/"
        "reference-video-remix.md"
    ).read_text(encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("compact", [False, True])
async def test_native_remix_package_discovers_complete_guide(compact):
    package = catalog.get_workflow_skill(
        {"skill_id": "reference-video-remix", "compact": compact}
    )
    assert package["ok"] is True, package
    locator = package["planning_contract"]["native_production"]["guide_reference"]
    assert locator == GUIDE_REFERENCE
    assert "Read the full guide" in package["agent_instruction"]
    assert "before preparing material dependencies" in package["agent_instruction"]
    assert "planning_contract.native_production.guide_reference" in package["agent_instruction"]
    assert _canonical_guide() not in json.dumps(package)
    assert str(Path(__file__).resolve().parents[1]) not in json.dumps(package)

    tools = {tool.name: tool for tool in await workflow_mcp.list_tools()}
    Draft202012Validator(tools[locator["tool"]].inputSchema).validate(
        locator["arguments"]
    )
    result = await workflow_mcp.call_tool(locator["tool"], locator["arguments"])
    payload = result.structuredContent
    assert payload["ok"] is True, payload
    assert payload["status"] == "workflow_reference_ready"
    assert payload["content"] == _canonical_guide()
    assert set(payload) == {"ok", "status", "skill_id", "reference", "content"}
    resource = json.loads(await workflow_mcp.read_resource(locator["resource_uri"]))
    assert resource == payload


@pytest.mark.asyncio
async def test_remix_guide_is_readable_through_tool_and_resource():
    payload = (await workflow_mcp.call_tool(
        GUIDE_REFERENCE["tool"], GUIDE_REFERENCE["arguments"]
    )).structuredContent
    assert payload["ok"] is True, payload
    assert payload["content"] == _canonical_guide()
    resource = json.loads(await workflow_mcp.read_resource(GUIDE_REFERENCE["resource_uri"]))
    assert resource == payload


@pytest.mark.asyncio
async def test_remix_reference_tool_advertises_exact_allowlist():
    tool = next(
        tool for tool in await workflow_mcp.list_tools()
        if tool.name == "workflow_skill_reference_get"
    )
    assert set(tool.inputSchema["properties"]["reference"]["enum"]) == {
        "custom-topology.md", "error-recovery.md", "integration.md",
        "reference-video-remix.md",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", [
    "../../reference-video-remix.md", "references/../reference-video-remix.md",
    "/tmp/reference-video-remix.md", "unknown-guide.md",
    "references/reference-video-remix.md/extra",
])
async def test_remix_guide_rejects_arbitrary_reference_paths(reference):
    arguments = {**GUIDE_REFERENCE["arguments"], "reference": reference}
    payload = (await workflow_mcp.call_tool(
        GUIDE_REFERENCE["tool"], arguments
    )).structuredContent
    assert payload["ok"] is False
    assert payload["status"] == "tool_arguments_invalid"
    direct = workflow_mcp._read_skill_reference("reference-video-remix", reference)
    assert direct["ok"] is False
    assert direct["status"] == "workflow_reference_not_found"
    with pytest.raises(ValueError, match="workflow skill reference not found"):
        await workflow_mcp.read_resource(
            "dramaclaw-workflow://skills/reference-video-remix/references/" + reference
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("availability", ["unknown", "disabled"])
async def test_remix_guide_keeps_real_catalog_availability_checks(monkeypatch, availability):
    skill = deepcopy(catalog._load_skill("reference-video-remix"))
    skill["enabled"] = False
    monkeypatch.setattr(catalog, "_load_skills", lambda: [skill] if availability == "disabled" else [])
    payload = (await workflow_mcp.call_tool(
        GUIDE_REFERENCE["tool"], GUIDE_REFERENCE["arguments"]
    )).structuredContent
    assert payload["ok"] is False
    assert payload["status"] == "workflow_skill_not_found"
    assert "content" not in payload
    with pytest.raises(ValueError, match="workflow skill reference not found"):
        await workflow_mcp.read_resource(GUIDE_REFERENCE["resource_uri"])


@pytest.mark.parametrize("skill_id", ["short-drama-quick", "text-to-image-video"])
def test_unrelated_skill_package_keeps_existing_planning_guidance(skill_id):
    package = catalog.get_workflow_skill({"skill_id": skill_id, "compact": True})
    assert package["ok"] is True, package
    assert package["planning_contract"]["native_production"] is None
    assert package["agent_instruction"] == WORKFLOW_PLANNING_INSTRUCTIONS
    assert "guide_reference" not in json.dumps(package)


def test_legacy_remix_override_keeps_existing_planning_guidance(monkeypatch):
    skill = deepcopy(catalog._load_skill("reference-video-remix"))
    skill["version"] = "1.0.0"
    monkeypatch.setattr(catalog, "_load_skills", lambda: [skill])
    package = catalog.get_workflow_skill({"skill_id": "reference-video-remix"})
    assert package["ok"] is True, package
    assert package["skill"]["version"] == "1.0.0"
    assert package["planning_contract"]["native_production"] is None
    assert package["agent_instruction"] == WORKFLOW_PLANNING_INSTRUCTIONS
    assert "guide_reference" not in json.dumps(package)


@pytest.mark.asyncio
async def test_remix_guide_discovery_over_real_stdio(tmp_path):
    root = Path(__file__).resolve().parents[1]
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "novelvideo.chat.workflow_mcp"],
        cwd=str(tmp_path),
        env={
            **os.environ,
            "PYTHONPATH": str(root / "src"),
            "DRAMACLAW_USERNAME": "guide-discovery",
            "NOVELVIDEO_OUTPUT_DIR": str(tmp_path / "output"),
            "ST_EDITION": "ce",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            package_result = await session.call_tool(
                "workflow_skill_get", {"skill_id": "reference-video-remix"}
            )
            package = json.loads(package_result.content[0].text)
            locator = package["planning_contract"]["native_production"]["guide_reference"]
            assert locator == GUIDE_REFERENCE
            result = await session.call_tool(locator["tool"], locator["arguments"])
            payload = json.loads(result.content[0].text)
            assert result.isError is False
            assert payload["content"] == _canonical_guide()
            resource = await session.read_resource(locator["resource_uri"])
            assert json.loads(resource.contents[0].text) == payload


@pytest.mark.parametrize("base", [
    ".hermes/skills/workflows",
    "src/novelvideo/agent_skills/dramaclaw-workflows",
    "agent-kit/skills/dramaclaw-workflows",
])
def test_native_production_guides_use_single_approved_entry(base):
    root = Path(__file__).resolve().parents[1]
    skill = (root / base / "SKILL.md").read_text(encoding="utf-8")
    guide = (root / base / "references/reference-video-remix.md").read_text(encoding="utf-8")
    assert "freezone_prepare_production_nodes" in skill
    entry = guide.split("## New-plan tool contract", 1)[1].split("## ", 1)[0]
    assert "freezone_prepare_production_nodes" in entry
    for field in ("commission_ref", "goal", "run_after_create", "plan_count"):
        assert field in entry
    assert "freezone_prepare_workflow_draft" not in entry
    assert "freezone_confirm_workflow_draft" not in entry
    assert "freezone_run_node_action" in entry
    assert "await_completion_event" in entry
    assert "freezone_begin_agent_product_generation" in skill


def test_rendered_native_skill_does_not_recommend_dynamic_entry():
    from novelvideo.chat.hermes_workspace import _render_workflow_skill

    native, _ = _render_workflow_skill({"id": "reference-video-remix", "name": "Remix"})
    dynamic, _ = _render_workflow_skill({"id": "text-to-image", "name": "Image"})
    assert "freezone_prepare_production_nodes" in native
    assert "已有节点" in native
    assert "freezone_begin_agent_product_generation" not in native
    assert "freezone_get_workflow_skill" not in native
    assert "freezone_prepare_workflow_draft" not in native
    assert "freezone_begin_agent_product_generation" in dynamic
    assert "freezone_prepare_production_nodes" not in dynamic
