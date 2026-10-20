"""MCP bridge for DramaClaw tools.

Hermes uses ``.hermes/plugins/dramaclaw`` directly. Claude, Codex, and other
MCP-speaking agents use this stdio server to call that same toolset without
duplicating DramaClaw API wrappers.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
import os
import sys
import time
import types as py_types
from dataclasses import replace
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from novelvideo.chat import mcp_runtime
from novelvideo.chat.mcp_runtime import (
    _integer_string_argument_paths as _integer_string_argument_paths,
    _unexpected_argument_fields as _unexpected_argument_fields,
)

logger = logging.getLogger("novelvideo.chat.dramaclaw_mcp")
ToolIndex = mcp_runtime.ToolIndex


def _validation_error_details(errors: list[Any]) -> list[dict[str, Any]]:
    from novelvideo.chat.story_mcp_policy import validation_error_details

    return validation_error_details(errors)


_FIXED_RUNTIME_ROOT = Path("/app")
_SOURCE_MODULE_PATH = Path("src") / "novelvideo" / "chat" / "dramaclaw_mcp.py"


def _trusted_source_root() -> Path | None:
    """Return the checkout root only when this module has the reviewed src layout."""
    module_path = Path(__file__).resolve()
    source_parts = _SOURCE_MODULE_PATH.parts
    if tuple(module_path.parts[-len(source_parts) :]) != source_parts:
        return None
    return module_path.parents[len(source_parts) - 1]


def _plugin_from_root(root: Path, relative_paths: tuple[Path, ...]) -> Path | None:
    plugins_root = (root / ".hermes" / "plugins").resolve()
    for relative_path in relative_paths:
        candidate = root / relative_path
        if not candidate.is_file():
            continue
        resolved = candidate.resolve()
        try:
            resolved.relative_to(plugins_root)
        except ValueError as exc:
            raise RuntimeError(
                f"Hermes plugin path escapes the trusted plugin root: {candidate}"
            ) from exc
        return resolved
    return None


def _plugin_path(plugin_name: str) -> Path:
    """Resolve a bundled Hermes plugin without assuming a source checkout import.

    Production installs may import this module from ``site-packages`` while the
    reviewed plugins live under ``/app/.hermes``.  In that layout deriving the
    repository root from ``__file__`` points at the Python installation instead
    of the application root.
    """
    relative_paths = (
        Path(".hermes") / "plugins" / plugin_name / "__init__.py",
        Path(".hermes") / "plugins" / plugin_name / "init.py",
    )
    configured_root = os.environ.get("DRAMACLAW_ROOT", "").strip()
    if configured_root:
        root = Path(configured_root).expanduser().resolve()
        plugin_path = _plugin_from_root(root, relative_paths)
        if plugin_path is not None:
            return plugin_path
        expected = ", ".join(str(root / path) for path in relative_paths)
        raise RuntimeError(
            f"DRAMACLAW_ROOT does not contain the {plugin_name} plugin; expected {expected}"
        )

    roots: list[Path] = []
    source_root = _trusted_source_root()
    if source_root is not None:
        roots.append(source_root)
    roots.append(_FIXED_RUNTIME_ROOT)

    searched: list[str] = []
    seen: set[Path] = set()
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        searched.extend(str(root / relative_path) for relative_path in relative_paths)
        plugin_path = _plugin_from_root(root, relative_paths)
        if plugin_path is not None:
            return plugin_path
    raise RuntimeError(
        f"cannot locate bundled Hermes plugin {plugin_name}; searched: "
        + ", ".join(searched)
    )


def _install_hermes_registry_shim() -> None:
    if "tools.registry" in sys.modules:
        return

    tools_pkg = py_types.ModuleType("tools")
    registry = py_types.ModuleType("tools.registry")

    def tool_result(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    def tool_error(message: Any) -> str:
        return json.dumps({"ok": False, "error": str(message)}, ensure_ascii=False)

    registry.tool_result = tool_result
    registry.tool_error = tool_error
    tools_pkg.registry = registry
    sys.modules.setdefault("tools", tools_pkg)
    sys.modules["tools.registry"] = registry


def _load_plugin(plugin_name: str) -> Any:
    _install_hermes_registry_shim()
    plugin_path = _plugin_path(plugin_name)
    spec = importlib.util.spec_from_file_location(
        f"_dramaclaw_{plugin_name}_hermes_plugin_for_mcp",
        plugin_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {plugin_name} plugin from {plugin_path}")
    module = importlib.util.module_from_spec(spec)
    module._MCP_EXCLUDE_INTERACTIVE_STORY = _EXCLUDE_STORY_TOOLS
    spec.loader.exec_module(module)
    return module


def _tool_index(*plugins: Any) -> dict[str, tuple[dict[str, Any], Any]]:
    index: dict[str, tuple[dict[str, Any], Any]] = {}
    for plugin in plugins:
        for entry in getattr(plugin, "TOOLS", ()):
            if not isinstance(entry, tuple) or len(entry) != 3:
                continue
            name, schema, handler = entry
            if isinstance(name, str) and isinstance(schema, dict) and callable(handler):
                if name in index:
                    raise RuntimeError(f"duplicate MCP tool name: {name}")
                index[name] = (schema, handler)
    return index


_PLUGIN_CACHE: dict[tuple[str, bool], Any] = {}
_PLUGIN_TOOL_CACHE: dict[tuple[str, bool], ToolIndex] = {}
SERVER = Server("dramaclaw", version="0.1.0")
_EXCLUDE_STORY_TOOLS = False


def _plugin(plugin_name: str) -> Any:
    cache_key = (plugin_name, _EXCLUDE_STORY_TOOLS)
    plugin = _PLUGIN_CACHE.get(cache_key)
    if plugin is None:
        plugin = _load_plugin(plugin_name)
        _PLUGIN_CACHE[cache_key] = plugin
    return plugin


def _agent_plugin_name() -> str:
    """Choose one capability boundary for this agent process."""
    return "freezone" if _freezone_canvas_mode() else "dramaclaw"


def _plugin_tools(plugin_name: str) -> dict[str, tuple[dict[str, Any], Any]]:
    cache_key = (plugin_name, _EXCLUDE_STORY_TOOLS)
    tools = _PLUGIN_TOOL_CACHE.get(cache_key)
    if tools is None:
        tools = _tool_index(_plugin(plugin_name))
        _PLUGIN_TOOL_CACHE[cache_key] = tools
    return tools


def _agent_tools() -> dict[str, tuple[dict[str, Any], Any]]:
    """Load only the plugin belonging to the active agent profile."""
    tools = _plugin_tools(_agent_plugin_name())
    if _scope_kind() == "home":
        return {name: tools[name] for name in sorted(HOME_TOOL_NAMES) if name in tools}
    return tools


def _adapt_external_agent_tool_result(name: str, value: Any) -> str:
    """Resolve legacy workflow instructions at the external MCP boundary.

    Hermes consumes the plugin result directly and keeps its existing flow. External
    MCP agents receive an instruction that distinguishes an already-authorized create
    imperative from a draft that genuinely still needs confirmation.
    """

    raw = str(value or "")
    try:
        result = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        result = None
    if name not in {
        "freezone_prepare_workflow",
        "freezone_prepare_workflow_draft",
        "freezone_prepare_workflow_plan_draft",
    }:
        return raw
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return raw
    if not isinstance(payload, dict) or not (
        payload.get("ok") is True
        and str(payload.get("status") or "") == "workflow_draft_ready"
    ):
        return raw
    for field in ("billing", "agent_planning_charge", "agent_credit_estimate"):
        payload.pop(field, None)
    instruction = (
        "Present the compact preview and do not call freezone_get_workflow; this result is the "
        "durable draft receipt. If the current request explicitly asked to create or run "
        "and generation choices are complete, call freezone_confirm_workflow_draft exactly once "
        "now with the named draft_id and revision, without asking for another confirmation. A "
        "parameter clarification card is not canvas creation; otherwise wait for confirmation. "
        "Never prepare a replacement draft or claim success before the actual canvas write receipt. "
    )
    instruction += (
        "For adjustments, prepare a new complete Plan draft."
        if name == "freezone_prepare_workflow_plan_draft"
        else "For adjustments, patch this draft instead of rebuilding the intent."
    )
    instruction += " Do not invent or mention credits, billing, pricing, or editions."
    payload["agent_instruction"] = instruction
    return json.dumps(payload, ensure_ascii=False)


def _output_schema_for_tool(
    name: str, tool_index: ToolIndex | None = None
) -> dict[str, Any]:
    return mcp_runtime.output_schema_for_tool(
        name, tool_index if tool_index is not None else _agent_tools()
    )


# Home turns have no bound project and should only manage the project
# collection. Project-scoped tokens remain the authority for every underlying
# API call, but this allow-list also keeps irrelevant production schemas out of
# discovery and prevents the model from selecting a project-only operation.
HOME_TOOL_NAMES = frozenset(
    {
        "dramaclaw_get",
        "dramaclaw_post",
        "dramaclaw_patch",
        "dramaclaw_delete",
    }
)


def _scope_kind() -> str:
    return "project" if os.environ.get("DRAMACLAW_PROJECT_ID", "").strip() else "home"


def _freezone_canvas_mode() -> bool:
    """Detect Freezone even when a shared App Server drops one env flag."""
    if os.environ.get("DRAMACLAW_TOOL_MODE", "").strip() == "freezone_canvas":
        return True
    return (
        bool(
            os.environ.get("DRAMACLAW_CANVAS_ID", "").strip()
            and os.environ.get("DRAMACLAW_AGENT_PROFILE", "")
            .strip()
            .startswith("freezone")
        )
        or os.environ.get("DRAMACLAW_CHAT_SURFACE", "").strip() == "freezone"
    )


def _normalize_structured_result(
    schema: dict[str, Any], decoded: Any
) -> dict[str, Any]:
    structured = mcp_runtime.normalize_structured_result(schema, decoded)
    raw = decoded if isinstance(decoded, dict) else {}
    properties = schema.get("properties") or {}
    name = str(schema.get("x-dramaclaw-tool") or "")
    if "canvas_context_status" in properties and not structured["ok"]:
        if not any(
            structured.get(key) for key in ("code", "error", "message", "errors")
        ):
            structured["message"] = (
                "Canvas context request failed without error details."
            )
    if name in {
        "dramaclaw_get",
        "dramaclaw_post",
        "dramaclaw_patch",
        "dramaclaw_delete",
    }:
        structured["response"] = raw.get("data", decoded)
    elif name == "dramaclaw_get_task":
        structured["task"] = raw.get("data")
    elif name == "dramaclaw_get_episode_script":
        structured["script"] = raw.get("data")
    elif name == "dramaclaw_update_character_face_prompt":
        structured["character"] = raw.get("data")
    for field, count in (
        ("skills", "count"),
        ("canvases", "count"),
        ("tasks", "count"),
        ("files", "count"),
        ("candidates", "candidate_count"),
    ):
        if isinstance(structured.get(field), list) and count in properties:
            structured.setdefault(count, len(structured[field]))
    Draft202012Validator(schema).validate(structured)
    return structured


def _mcp_error_result(
    name: str, payload: dict[str, Any], tool_index: ToolIndex | None = None
) -> types.CallToolResult:
    return mcp_runtime.mcp_error_result(
        name,
        payload,
        tool_index if tool_index is not None else _agent_tools(),
        hooks=_tool_call_hooks(name),
    )


def _structured_tool_result(
    name: str, adapted: str, tool_index: ToolIndex | None = None
) -> types.CallToolResult:
    return mcp_runtime.structured_tool_result(
        name,
        adapted,
        tool_index if tool_index is not None else _agent_tools(),
        hooks=_tool_call_hooks(name),
    )


def _workflow_schema_recovery_instruction(tool_name: str) -> str | None:
    if tool_name not in {
        "freezone_prepare_workflow_plan_draft",
        "workflow_graph_compile",
    }:
        return None
    return (
        "WorkflowPlan 校验失败。先按 message/path 指出的字段逐项修正，不要逐个猜删其它字段。"
        "不要提交单节点探测、空 edges 或 compact Intent。"
        "请保留同一份完整节点清单和所有边；每个可执行节点必须把"
        "workflowCatalog.recipeId 放在节点 data 内。确认所有 edge 的 source/target"
        "都对应 nodes[].id。提交前由 Agent 检查整图连通性；独立 Beat/镜头分支应通过"
        "非执行型公共输入根节点扇出连接，不能要求用户说明内部连线，也不能把需要故障"
        "隔离的兄弟分支串行连接。连线兼容性不明确时先读取 link type catalog，禁止猜测"
        "类型或反复试编译。恢复编译成功后立即用同一计划提交创建。"
    )


def _workflow_plan_log_summary(arguments: Any) -> dict[str, Any]:
    plan = arguments.get("plan") if isinstance(arguments, dict) else None
    if not isinstance(plan, dict):
        return {"plan_type": type(plan).__name__}
    nodes = plan.get("nodes")
    edges = plan.get("edges")
    node_types: dict[str, int] = {}
    if isinstance(nodes, list):
        for node in nodes:
            if isinstance(node, dict):
                node_type = str(node.get("node_type") or node.get("type") or "unknown")
                node_types[node_type] = node_types.get(node_type, 0) + 1
    return {
        "schema_version": plan.get("schema_version"),
        "node_count": len(nodes) if isinstance(nodes, list) else None,
        "edge_count": len(edges) if isinstance(edges, list) else None,
        "node_types": node_types,
        "has_skill": isinstance(plan.get("skill"), dict),
    }


@SERVER.list_tools()
async def list_tools() -> list[types.Tool]:
    return build_mcp_tools(_agent_tools())


def build_mcp_tools(tool_index: ToolIndex) -> list[types.Tool]:
    return mcp_runtime.build_mcp_tools(tool_index)


def _skill_resource_path(uri: str) -> Path:
    """Resolve only Markdown files below an agent ``.agents/skills`` root.

    Codex can send either a standards-based ``file://`` URI or the resource's
    absolute/agent-root-relative path while progressively loading a skill.
    Both forms are constrained and then remapped to the current thread root.
    """
    raw_uri = str(uri or "").strip()
    parsed = urlparse(raw_uri)
    if parsed.scheme == "file":
        if parsed.netloc:
            raise ValueError("remote file skill resources are not supported")
        raw_path = unquote(parsed.path)
    elif not parsed.scheme and not parsed.netloc:
        raw_path = unquote(parsed.path)
    else:
        raise ValueError("only local skill resources are supported")
    if not raw_path:
        raise ValueError("skill resource path is required")
    raw_target = Path(raw_path).expanduser()
    parts = raw_target.parts
    relative: Path | None = None
    for index in range(len(parts) - 1):
        if parts[index] == ".agents" and parts[index + 1] == "skills":
            relative = Path(*parts[index + 2 :])
            break
    if relative is None:
        raise ValueError("resource is outside the agent skills directory")
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("resource is outside the agent skills directory")
    if relative.suffix.lower() != ".md":
        raise ValueError("only Markdown skill resources are supported")
    relative_parts = relative.parts
    if len(relative_parts) < 2 or (
        relative_parts[-1] != "SKILL.md" and "references" not in relative_parts[1:-1]
    ):
        raise ValueError("resource is not a skill document")
    if relative_parts[0] == "interactive-story" and not _freezone_canvas_mode():
        raise ValueError("interactive-story resources are only available in the Freezone canvas")
    roots = _skill_resource_roots()
    if raw_target.is_absolute() and raw_target.exists():
        existing_target = raw_target.resolve()
        if not any(_path_is_within(existing_target, root) for root in roots):
            raise ValueError("resource belongs to a different agent workspace")
    # Persisted Codex threads can retain a file URI from an older workspace.
    # Resolve the same skill-relative path against the current thread's
    # explicitly scoped skills root; never search arbitrary host directories.
    for root in roots:
        candidate = (root / relative).resolve()
        if not _path_is_within(candidate, root):
            continue
        if candidate.is_file():
            return candidate
    raise ValueError("skill resource is unavailable")


def _path_is_within(target: Path, root: Path) -> bool:
    try:
        target.relative_to(root)
    except ValueError:
        return False
    return True


def _skill_resource_roots() -> list[Path]:
    candidates: list[Path] = []
    configured = os.environ.get("DRAMACLAW_SKILLS_DIR", "").strip()
    if configured:
        candidates.append(Path(configured))
    candidates.append(Path.cwd() / ".agents" / "skills")
    roots: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            root = candidate.expanduser().resolve()
        except OSError:
            continue
        if root in seen or not root.is_dir():
            continue
        seen.add(root)
        roots.append(root)
    return roots


@SERVER.list_resources()
async def list_resources() -> list[types.Resource]:
    """Advertise readable skill Markdown resources to MCP clients."""
    resources: list[types.Resource] = []
    seen: set[Path] = set()
    for root in _skill_resource_roots():
        for target in sorted(root.rglob("*.md")):
            if not target.is_file() or target in seen:
                continue
            try:
                _skill_resource_path(target.as_uri())
            except ValueError:
                continue
            seen.add(target)
            resources.append(
                types.Resource(
                    name=target.relative_to(root).as_posix(),
                    uri=target.as_uri(),
                    description="DramaClaw agent skill resource",
                    mimeType="text/markdown",
                    size=target.stat().st_size,
                )
            )
    logger.info("mcp resources/list scope=%s count=%d", _scope_kind(), len(resources))
    return resources


@SERVER.list_resource_templates()
async def list_resource_templates() -> list[types.ResourceTemplate]:
    """DramaClaw exposes concrete skill files, not parameterized resources."""
    logger.info("mcp resources/templates/list scope=%s count=0", _scope_kind())
    return []


@SERVER.read_resource()
async def read_resource(uri: Any) -> str:
    """Read a referenced SKILL.md or references/*.md file only."""
    target = _skill_resource_path(str(uri))
    logger.info("mcp resources/read scope=%s resource=%s", _scope_kind(), target.name)
    try:
        return target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError("skill resource is unavailable") from exc


def _normalize_tool_arguments(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    from novelvideo.freezone.workflow_schema import normalize_workflow_tool_arguments

    return normalize_workflow_tool_arguments(name, arguments)


def _repair_tool_arguments(
    name: str, arguments: dict[str, Any], schema: dict[str, Any]
) -> dict[str, Any] | None:
    nested = arguments.get("plan")
    if (
        name == "freezone_prepare_workflow_plan_draft"
        and isinstance(nested, dict)
        and isinstance(nested.get("plan"), dict)
    ):
        return {**arguments, "plan": nested["plan"]}
    return None


def _tool_validation_error(name, arguments, schema, exc, errors) -> dict[str, Any]:
    from novelvideo.freezone.workflow_schema import workflow_plan_schema_diagnostics

    payload = mcp_runtime.validation_error_payload(name, arguments, schema, exc, errors)
    material_diagnostic = _material_validation_diagnostic(name, arguments, schema, exc)
    production_diagnostic = (
        None if material_diagnostic else
        _production_validation_diagnostic(name, arguments, schema, exc)
    )
    diagnostics = (
        [] if material_diagnostic else workflow_plan_schema_diagnostics(arguments, schema)
    )
    if diagnostics:
        payload.update(
            message="; ".join(
                f"{issue['path']}: {issue['message']}" for issue in diagnostics
            ),
            path=diagnostics[0]["path"],
        )
    diagnostic = material_diagnostic or production_diagnostic
    if diagnostic is not None:
        payload.update(path=diagnostic[0], message=diagnostic[1])
        payload.pop("unexpected_fields", None)
        payload.pop("integer_string_fields", None)
    if production_diagnostic is not None:
        from novelvideo.freezone.remix.production_command_schema import production_command_error_details

        payload.update(
            production_command_error_details(arguments.get("command")),
            side_effects="none",
        )
        bound_project = os.environ.get("DRAMACLAW_PROJECT_ID", "").strip()
        bound_canvas = os.environ.get("DRAMACLAW_CANVAS_ID", "").strip()
        bound_turn = os.environ.get("DRAMACLAW_TURN_ID", "").strip()
        if bound_project and bound_canvas and bound_turn:
            payload.update(
                project_id=bound_project,
                canvas_id=bound_canvas,
                retry_token=uuid.uuid4().hex,
            )
    if name in {"freezone_prepare_workflow_plan_draft", "workflow_graph_compile"}:
        payload.update(
            status="workflow_validation_failed", phase="graph_compile", retryable=True
        )
    recovery = _workflow_schema_recovery_instruction(name)
    if recovery:
        payload["agent_instruction"] = recovery
    return payload


def _tool_call_hooks(name: str) -> mcp_runtime.ToolCallHooks:
    if name.startswith("dramaclaw_") and "_interactive_story" in name:
        from novelvideo.chat.story_mcp_policy import STORY_HOOKS

        return replace(
            STORY_HOOKS,
            adapt_result=_adapt_external_agent_tool_result,
            normalize_result=_normalize_structured_result,
        )
    return mcp_runtime.ToolCallHooks(
        normalize_arguments=_normalize_tool_arguments,
        repair_arguments=_repair_tool_arguments,
        validation_error=_tool_validation_error,
        collect_all_errors=lambda name: False,
        adapt_result=_adapt_external_agent_tool_result,
        normalize_result=_normalize_structured_result,
    )


def _production_validation_diagnostic(
    name: str, arguments: dict[str, Any], input_schema: dict[str, Any],
    exc: SchemaError | ValidationError,
) -> tuple[str, str] | None:
    """Use native validation only as a bounded, non-executing explanation."""
    if name != "freezone_prepare_production_edit" or not isinstance(exc, ValidationError):
        return None
    # An unrelated outer failure must keep its normal field/path diagnostic,
    # even if the command happens to be malformed too.
    errors = Draft202012Validator(input_schema).iter_errors(arguments)
    if any(not error.absolute_path or error.absolute_path[0] != "command" for error in errors):
        return None
    command = arguments.get("command")
    pending = [(command, 0)]
    visited = 0
    while pending:
        value, depth = pending.pop()
        visited += 1
        if visited > 2048 or depth > 12:
            return None
        if isinstance(value, str) and len(value) > 4096:
            return None
        if isinstance(value, (dict, list)) and len(value) + len(pending) + visited > 2048:
            return None
        if isinstance(value, dict):
            if any(not isinstance(key, str) or len(key) > 128 for key in value):
                return None
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
    from novelvideo.freezone.remix.production_commands import validate_production_command

    try:
        validate_production_command(command)
    except ValueError as native_error:
        cause = native_error.__cause__
        field_error = (native_error if callable(getattr(native_error, "errors", None))
                       else cause)
        if field_error is not None and callable(getattr(field_error, "errors", None)):
            # Pydantic's default rendering includes input values. Only expose
            # field locations and error codes, never dialogue or argument data.
            message = "production command fields must match the native model: " + "; ".join(
                f"{'.'.join(str(part) for part in issue['loc'])}: {issue['type']}"
                for issue in field_error.errors(include_input=False)[:8]
            )
        else:
            message = str(native_error)[:900]
        kind = command.get("kind") if isinstance(command, dict) else None
        if kind in {"insert_turn", "batch"}:
            message += (
                "; insert_turn requires payload={beat_id, turn:{id,text,speaker (optional)}, index}; "
                "target_id must equal the new turn.id, not beat_id; beat_id belongs only in payload; "
                "timing_status is not a TurnV2 field. "
                "Atomic batch children have exactly {kind,target_id,payload}, without command_id or plan_ref. "
                "Use the saved beat id and actual dialogue; do not probe with empty objects or invent timing."
            )
        return "command", message[:1600]
    except (TypeError, KeyError):
        return None
    return None


_MATERIAL_CONTRACT_GUIDE = (
    "Native material contract: plan_ref={artifact_id,revision}; "
    "requirement={id,description,media_kind,final_step_id,placement_ids/take_ids/turn_ids arrays,acceptance array}; "
    "step={id,label,media_kind,source,recipe_id,prompt,depends_on,references,generation_inputs}. "
    "source=generate needs matching general-image/video/audio Recipe and nonblank prompt. "
    "Use actual IDs from the saved plan and the actual final step matching media_kind; "
    "shared intermediate references are separate steps. Each reference has exactly one step_id or node_id and role. "
    "Reread dramaclaw-workflows references/reference-video-remix.md material dependency graph section. "
    "Execution and candidates are server owned; native approval and acceptance remain required."
)


def _material_validation_diagnostic(
    name: str, arguments: dict[str, Any], input_schema: dict[str, Any],
    exc: SchemaError | ValidationError,
) -> tuple[str, str] | None:
    """Explain material schema failures using only bounded schema paths/codes."""
    if name != "freezone_prepare_material_work" or not isinstance(exc, ValidationError):
        return None
    material_fields = {"plan_ref", "requirements", "steps"}
    # Exclude graph values only for this independent outer-field check. The
    # original schema still rejects the call before any handler can execute.
    outer_schema = {
        **input_schema,
        "properties": {key: ({} if key in material_fields else value)
                       for key, value in input_schema.get("properties", {}).items()},
        "required": [key for key in input_schema.get("required", []) if key not in material_fields],
    }
    if not Draft202012Validator(outer_schema).is_valid(arguments):
        return None
    pending = [(arguments.get(key), 0) for key in material_fields]
    visited = 0
    while pending:
        value, depth = pending.pop()
        visited += 1
        if (visited > 2048 or depth > 12
                or isinstance(value, str) and len(value) > 4096
                or isinstance(value, (dict, list)) and len(value) + len(pending) + visited > 2048):
            return "requirements", "material graph diagnostic limit reached; " + _MATERIAL_CONTRACT_GUIDE
        if isinstance(value, dict):
            if any(not isinstance(key, str) or len(key) > 128 for key in value):
                return "requirements", "material graph diagnostic limit reached; " + _MATERIAL_CONTRACT_GUIDE
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
    issues: list[str] = []
    first_path = "requirements"
    for index, error in enumerate(Draft202012Validator(input_schema).iter_errors(arguments)):
        if index >= 16:
            issues.append("additional schema errors omitted")
            break
        # Only schema-declared names and array indices may enter output.
        schema = input_schema
        parts: list[str | int] = []
        for part in error.absolute_path:
            if isinstance(part, int):
                parts.append(part)
                schema = schema.get("items", {})
            elif part in schema.get("properties", {}):
                parts.append(part)
                schema = schema["properties"][part]
            else:
                break
        if error.validator == "required" and isinstance(error.instance, dict):
            # Derive missing names from trusted schema data, never error text.
            missing = [key for key in error.validator_value if key not in error.instance]
            candidates = [f"{mcp_runtime._format_schema_path([*parts, key])}: required" for key in missing]
        else:
            candidates = [f"{mcp_runtime._format_schema_path(parts)}: {error.validator}"]
        for issue in candidates:
            if issue not in issues and len(issues) < 16:
                if not issues:
                    first_path = issue.split(":", 1)[0]
                issues.append(issue)
    if not issues:
        return None
    detail = "; ".join(issues)[:2200 - len(_MATERIAL_CONTRACT_GUIDE) - 2]
    return first_path, detail + "; " + _MATERIAL_CONTRACT_GUIDE


@SERVER.call_tool(validate_input=False)
async def call_tool(
    name: str, arguments: dict[str, Any], *, tool_index: ToolIndex | None = None
) -> types.CallToolResult:
    tools = tool_index if tool_index is not None else _agent_tools()
    if name not in tools:
        raise ValueError(f"unknown DramaClaw tool: {name}")
    workflow_started = (
        time.monotonic() if name == "freezone_prepare_workflow_plan_draft" else None
    )
    if workflow_started is not None:
        logger.info(
            "freezone_prepare_workflow_plan_draft.start scope=%s summary=%s",
            _scope_kind(),
            _workflow_plan_log_summary(arguments),
        )
    result = await mcp_runtime.call_tool(
        name,
        arguments,
        tool_index=tools,
        hooks=_tool_call_hooks(name),
        scope=_scope_kind(),
        log=logger,
    )
    if workflow_started is not None:
        payload = result.structuredContent or {}
        if payload.get("error") == "tool_arguments_invalid":
            logger.warning(
                "freezone_prepare_workflow_plan_draft.validation_failed elapsed_ms=%d message=%s path=%s summary=%s",
                int((time.monotonic() - workflow_started) * 1000),
                payload.get("message"),
                payload.get("path"),
                _workflow_plan_log_summary(arguments),
            )
        else:
            logger.info(
                "freezone_prepare_workflow_plan_draft.end elapsed_ms=%d result_bytes=%d",
                int((time.monotonic() - workflow_started) * 1000),
                len(result.content[0].text),
            )
    return result


async def _main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await SERVER.run(
            read_stream,
            write_stream,
            SERVER.create_initialization_options(),
        )


def main() -> None:
    global _EXCLUDE_STORY_TOOLS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exclude-interactive-story", action="store_true")
    _EXCLUDE_STORY_TOOLS = parser.parse_args().exclude_interactive_story
    asyncio.run(_main())


if __name__ == "__main__":
    main()
