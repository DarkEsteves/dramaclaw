"""Bounded model requests for native production-plan drafting."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
import time
from typing import Any

from novelvideo.llm_instrumentation import (
    _extract_provider_ids,
    log_production_plan_diagnostic,
)


@dataclass(frozen=True)
class PlanExecutionPolicy:
    total_timeout_seconds: float
    max_output_tokens: int
    stream_diagnostics: bool


@dataclass(frozen=True)
class PlanModelResponseInfo:
    finish_reason: str | None
    output_tokens: int | None
    total_output_tokens: int | None
    reasoning_tokens: int | None
    output_characters: int


class PlanOutputLimitError(ValueError):
    def __init__(self, info: PlanModelResponseInfo):
        super().__init__("model_output_limit")
        self.info = info


def split_plan_stage_tokens(total: int) -> tuple[int, int]:
    """Share the existing per-draft output ceiling across two model calls."""
    if type(total) is not int or not 2 <= total <= 65536:
        raise ValueError("invalid_production_plan_output_tokens")
    outline = max(1, total // 4)
    return outline, total - outline


def explicitly_truncated(info: PlanModelResponseInfo) -> bool:
    return info.finish_reason in {"length", "max_tokens", "output_limit"}


def _safe_get(value: Any, name: str) -> Any:
    try:
        return getattr(value, name, None)
    except Exception:
        return None


def _usage(value: Any) -> Any:
    getter = _safe_get(value, "usage")
    try:
        return getter() if callable(getter) else getter
    except Exception:
        return None


def _is_model_response(message: Any) -> bool:
    return (_safe_get(message, "kind") == "response"
            or bool(_safe_get(message, "model_name"))
            or bool(_safe_get(message, "finish_reason")))


def _messages(result: Any) -> list[Any]:
    getter = _safe_get(result, "all_messages")
    try:
        value = getter() if callable(getter) else getter
        return list(value) if value is not None else []
    except Exception:
        return []


def inspect_plan_response(result: Any) -> PlanModelResponseInfo:
    """Inspect the final model response separately from cumulative run usage."""
    final = next((message for message in reversed(_messages(result))
                  if _is_model_response(message)), None)
    final_usage = _usage(final)
    total_usage = _usage(result)
    details = _safe_get(final_usage, "details")
    if not isinstance(details, dict):
        details = _safe_get(total_usage, "details")
    output = _safe_get(result, "output")
    reason = _safe_get(final, "finish_reason")
    return PlanModelResponseInfo(
        finish_reason=reason if isinstance(reason, str) else None,
        output_tokens=_safe_get(final_usage, "output_tokens"),
        total_output_tokens=_safe_get(total_usage, "output_tokens"),
        reasoning_tokens=details.get("reasoning_tokens") if isinstance(details, dict) else None,
        output_characters=(len(output) if isinstance(output, str) else
                           len(json.dumps(output, ensure_ascii=False)) if isinstance(output, dict) else 0),
    )


def _structured_limit_reason(error: BaseException) -> str | None:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        body = _safe_get(current, "body")
        if isinstance(body, dict):
            nested = body.get("error")
            values = (body, nested) if isinstance(nested, dict) else (body,)
            for value in values:
                finish = value.get("finish_reason")
                if finish in {"length", "max_tokens", "output_limit"}:
                    return finish
                code = value.get("code")
                if code in {"max_output_tokens", "max_completion_tokens", "max_tokens",
                            "output_limit"}:
                    return "max_tokens"
        code = _safe_get(current, "code")
        if code in {"max_output_tokens", "max_completion_tokens", "max_tokens",
                    "output_limit"}:
            return "max_tokens"
        current = current.__cause__ or current.__context__
    return None


def plan_execution_policy() -> PlanExecutionPolicy:
    from novelvideo.freezone.recipe_runtime import _recipe_text_generation_timeout_seconds

    try:
        max_tokens = int(os.getenv("FREEZONE_PRODUCTION_PLAN_OUTPUT_TOKENS", "16384"))
    except ValueError as exc:
        raise ValueError("invalid_production_plan_output_tokens") from exc
    if not 0 < max_tokens <= 65536:
        raise ValueError("invalid_production_plan_output_tokens")
    stream = os.getenv("FREEZONE_PRODUCTION_PLAN_STREAM_DIAGNOSTICS", "0") == "1"
    return PlanExecutionPolicy(_recipe_text_generation_timeout_seconds(), max_tokens, stream)


async def run_plan_text(
    agent: Any, task: str, *, policy: PlanExecutionPolicy, deadline: float,
) -> Any:
    """Call the existing metered Agent.run once, under the shared deadline."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("production_plan_deadline_exceeded")
    settings: dict[str, Any] = {
        "max_tokens": policy.max_output_tokens,
        "openai_reasoning_effort": "none",
    }
    options: dict[str, Any] = {}
    if policy.stream_diagnostics:
        started = time.monotonic()
        first_event: float | None = None
        first_text: float | None = None
        text_characters = 0

        async def observe(_ctx: Any, events: Any) -> None:
            nonlocal first_event, first_text, text_characters
            async for event in events:
                now = time.monotonic()
                if first_event is None:
                    first_event = now
                part = getattr(event, "part", None)
                delta = getattr(event, "delta", None)
                text = getattr(part, "content", None) or getattr(delta, "content_delta", None)
                if isinstance(text, str) and text:
                    if first_text is None:
                        first_text = now
                    text_characters += len(text)

        options["event_stream_handler"] = observe
    started = time.monotonic()
    try:
        async with asyncio.timeout(remaining):
            result = await agent.run(task, model_settings=settings, **options)
    except TimeoutError as exc:
        if time.monotonic() >= deadline:
            raise TimeoutError("production_plan_deadline_exceeded") from exc
        raise
    except Exception as exc:
        reason = _structured_limit_reason(exc)
        if reason is not None:
            raise PlanOutputLimitError(PlanModelResponseInfo(
                finish_reason=reason, output_tokens=None, total_output_tokens=None,
                reasoning_tokens=None, output_characters=0,
            )) from exc
        raise
    finally:
        if policy.stream_diagnostics:
            log_production_plan_diagnostic(
                "production_plan.stream_timing", stage="requesting",
                first_event_ms=round((first_event - started) * 1000) if first_event else None,
                first_text_ms=round((first_text - started) * 1000) if first_text else None,
                streamed_characters=text_characters,
                partial_output_seen=first_text is not None,
            )
    info = inspect_plan_response(result)
    provider_request_id, _, _ = _extract_provider_ids(result)
    final_response = next((message for message in reversed(_messages(result))
                           if _is_model_response(message)), None)
    log_production_plan_diagnostic(
        "production_plan.model_request_returned", stage="requesting",
        elapsed_ms=round((time.monotonic() - started) * 1000),
        finish_reason=info.finish_reason,
        output_tokens=info.output_tokens,
        total_output_tokens=info.total_output_tokens,
        reasoning_tokens=info.reasoning_tokens,
        output_characters=info.output_characters,
        requested_output_tokens=policy.max_output_tokens,
        input_tokens=_safe_get(_usage(result), "input_tokens"),
        model=_safe_get(_safe_get(agent, "model"), "model_name"),
        response_model=_safe_get(final_response, "model_name"),
        route_profile="freezone_recipe_text_generation",
        sdk_retry_count=_safe_get(result, "retry_count"),
        provider_request_id=provider_request_id,
        response_received=True,
    )
    if explicitly_truncated(info):
        raise PlanOutputLimitError(info)
    return result
