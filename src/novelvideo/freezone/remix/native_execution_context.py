"""Classify a native canvas action from authenticated host evidence."""

from typing import Any, Literal


NativeExecutionSource = Literal["canvas_user", "agent_command"]


def resolve_native_execution_source(
    *, credential_kind: str, has_verified_bridge: bool,
) -> NativeExecutionSource:
    if has_verified_bridge or credential_kind in {"agent_session", "local_trusted_agent"}:
        return "agent_command"
    return "canvas_user"


def is_manual_native_run(run: dict[str, Any]) -> bool:
    """Read only the saved host context for this exact run and actor."""
    metadata = run.get("metadata") or {}
    context = metadata.get("native_execution_context")
    return (metadata.get("native_execution_source") == "canvas_user"
            and isinstance(context, dict)
            and context.get("source") == "canvas_user"
            and context.get("requester_user_id") == run.get("actor_id")
            and context.get("project_id") == run.get("project_id")
            and context.get("canvas_id") == run.get("canvas_id")
            and context.get("run_id") == run.get("run_id"))
