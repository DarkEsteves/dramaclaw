from __future__ import annotations

import asyncio
import logging
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from novelvideo.freezone.remix import production_plan_execution as plan_execution
from novelvideo.freezone.remix.production_plan_execution import (
    PlanExecutionPolicy, plan_execution_policy, run_plan_text, split_plan_stage_tokens,
)


def test_two_stage_budget_shares_existing_output_limit():
    assert split_plan_stage_tokens(16384) == (4096, 12288)
    for total in (2, 7, 4096, 65536):
        outline, arrangement = split_plan_stage_tokens(total)
        assert outline >= 1 and arrangement >= 1
        assert outline + arrangement == total
    with pytest.raises(ValueError, match="invalid_production_plan_output_tokens"):
        split_plan_stage_tokens(1)


@pytest.mark.asyncio
async def test_expired_deadline_never_calls_model():
    agent = SimpleNamespace(run=AsyncMock())
    policy = PlanExecutionPolicy(540.0, 8192, False)
    with pytest.raises(TimeoutError, match="production_plan_deadline_exceeded"):
        await run_plan_text(agent, "draft", policy=policy, deadline=time.monotonic() - 1)
    agent.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_plan_request_uses_remaining_budget_and_preserves_agent_run():
    response = SimpleNamespace(output="{}", usage=lambda: SimpleNamespace(output_tokens=2))
    agent = SimpleNamespace(run=AsyncMock(return_value=response))
    policy = PlanExecutionPolicy(5.0, 2048, False)
    assert await run_plan_text(agent, "draft", policy=policy,
                               deadline=time.monotonic() + 2) is response
    args, kwargs = agent.run.await_args
    assert args == ("draft",)
    assert kwargs["model_settings"] == {"max_tokens": 2048, "openai_reasoning_effort": "none"}


@pytest.mark.asyncio
async def test_complete_response_at_budget_reaches_plan_validation():
    response = SimpleNamespace(output="{}", usage=lambda: SimpleNamespace(output_tokens=4),
        all_messages=lambda: [SimpleNamespace(model_name="writer", finish_reason="stop",
            usage=SimpleNamespace(output_tokens=4))])
    agent = SimpleNamespace(run=AsyncMock(return_value=response))
    assert await run_plan_text(agent, "draft", policy=PlanExecutionPolicy(5, 4, False),
                               deadline=time.monotonic() + 2) is response


@pytest.mark.asyncio
async def test_cumulative_usage_above_budget_does_not_imply_truncation():
    response = SimpleNamespace(output="{}", usage=lambda: SimpleNamespace(output_tokens=9000),
        all_messages=lambda: [SimpleNamespace(model_name="writer", finish_reason="stop",
            usage=SimpleNamespace(output_tokens=2000))])
    agent = SimpleNamespace(run=AsyncMock(return_value=response))
    assert await run_plan_text(agent, "draft", policy=PlanExecutionPolicy(5, 8192, False),
                               deadline=time.monotonic() + 2) is response
    info = plan_execution.inspect_plan_response(response)
    assert info.output_tokens == 2000
    assert info.total_output_tokens == 9000


def test_native_output_diagnostic_counts_serialized_characters():
    response = SimpleNamespace(output={"creative_direction": "保留节奏", "beats": []})
    info = plan_execution.inspect_plan_response(response)
    assert info.output_characters == len(json.dumps(response.output, ensure_ascii=False))


@pytest.mark.asyncio
async def test_return_diagnostic_uses_provider_details_request_id(caplog):
    response = SimpleNamespace(
        output="{}", usage=lambda: SimpleNamespace(output_tokens=5),
        all_messages=lambda: [SimpleNamespace(
            kind="response", model_name="writer", finish_reason="stop",
            provider_details={"request_id": "req-provider-details"},
            usage=SimpleNamespace(output_tokens=5),
        )],
    )
    agent = SimpleNamespace(run=AsyncMock(return_value=response))
    with caplog.at_level(logging.INFO, logger="novelvideo.llm_instrumentation"):
        await run_plan_text(agent, "draft", policy=PlanExecutionPolicy(5, 100, False),
                            deadline=time.monotonic() + 2)
    returned = [json.loads(row.getMessage()) for row in caplog.records
                if row.name == "novelvideo.llm_instrumentation"
                and row.getMessage().startswith("{")
                and json.loads(row.getMessage()).get("event") ==
                "production_plan.model_request_returned"]
    assert len(returned) == 1
    assert returned[0]["provider_request_id"] == "req-provider-details"


@pytest.mark.asyncio
async def test_finish_reason_length_is_terminal_even_without_usage():
    response = SimpleNamespace(
        output='{"partial":', usage=lambda: None,
        all_messages=lambda: [SimpleNamespace(model_name="text-model", finish_reason="length")],
    )
    agent = SimpleNamespace(run=AsyncMock(return_value=response))
    with pytest.raises(ValueError, match="model_output_limit") as error:
        await run_plan_text(agent, "draft", policy=PlanExecutionPolicy(5, 100, False),
                            deadline=time.monotonic() + 2)
    assert isinstance(error.value, plan_execution.PlanOutputLimitError)
    assert error.value.info.finish_reason == "length"
    assert agent.run.await_count == 1


@pytest.mark.asyncio
async def test_last_model_response_without_model_name_still_reports_finish_reason():
    response = SimpleNamespace(
        output="{}", usage=lambda: None,
        all_messages=lambda: [SimpleNamespace(kind="response", model_name=None,
                                               finish_reason="length", usage=None)],
    )
    agent = SimpleNamespace(run=AsyncMock(return_value=response))
    with pytest.raises(ValueError, match="model_output_limit"):
        await run_plan_text(agent, "draft", policy=PlanExecutionPolicy(5, 100, False),
                            deadline=time.monotonic() + 2)


@pytest.mark.asyncio
async def test_missing_usage_metadata_does_not_discard_complete_response():
    def unavailable_usage():
        raise RuntimeError("usage metadata unavailable")

    response = SimpleNamespace(output="{}", usage=unavailable_usage)
    agent = SimpleNamespace(run=AsyncMock(return_value=response))
    assert await run_plan_text(agent, "draft", policy=PlanExecutionPolicy(5, 100, False),
                               deadline=time.monotonic() + 2) is response


@pytest.mark.asyncio
async def test_missing_message_metadata_does_not_discard_complete_response():
    response = SimpleNamespace(output="{}", usage=lambda: None, all_messages=lambda: None)
    agent = SimpleNamespace(run=AsyncMock(return_value=response))
    assert await run_plan_text(agent, "draft", policy=PlanExecutionPolicy(5, 100, False),
                               deadline=time.monotonic() + 2) is response


@pytest.mark.asyncio
async def test_cancel_propagates_without_retry():
    agent = SimpleNamespace(run=AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await run_plan_text(agent, "draft", policy=PlanExecutionPolicy(5, 4, False),
                            deadline=time.monotonic() + 2)
    assert agent.run.await_count == 1


@pytest.mark.asyncio
async def test_provider_timeout_before_deadline_keeps_provider_error():
    agent = SimpleNamespace(run=AsyncMock(side_effect=TimeoutError("gateway_request_timed_out")))
    with pytest.raises(TimeoutError, match="gateway_request_timed_out"):
        await run_plan_text(agent, "draft", policy=PlanExecutionPolicy(5, 100, False),
                            deadline=time.monotonic() + 4)


@pytest.mark.asyncio
async def test_structured_provider_output_limit_becomes_terminal_error():
    class ProviderOutputLimit(RuntimeError):
        body = {"error": {"code": "max_output_tokens"}}

    agent = SimpleNamespace(run=AsyncMock(side_effect=ProviderOutputLimit("unsafe text")))
    with pytest.raises(ValueError, match="model_output_limit") as caught:
        await run_plan_text(agent, "draft", policy=PlanExecutionPolicy(5, 100, False),
                            deadline=time.monotonic() + 2)
    assert isinstance(caught.value, plan_execution.PlanOutputLimitError)
    assert caught.value.info.finish_reason == "max_tokens"
    assert agent.run.await_count == 1


@pytest.mark.asyncio
async def test_unknown_provider_error_remains_original():
    failure = RuntimeError("max tokens exceeded according to message text")
    agent = SimpleNamespace(run=AsyncMock(side_effect=failure))
    with pytest.raises(RuntimeError) as caught:
        await run_plan_text(agent, "draft", policy=PlanExecutionPolicy(5, 100, False),
                            deadline=time.monotonic() + 2)
    assert caught.value is failure


@pytest.mark.asyncio
async def test_newapi_provider_failure_sends_one_http_request(monkeypatch):
    import httpx
    from pydantic_ai import Agent
    import novelvideo.config as config

    requests = []

    def reject(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, json={"error": {"code": "upstream_unavailable"}})

    monkeypatch.setattr(
        config, "_newapi_text_http_client_factory",
        lambda *, timeout_seconds: lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(reject), timeout=timeout_seconds),
    )
    model = config._newapi_text_openai_model(
        "writer", api_key="key", base_url="https://example.test/v1",
        timeout_seconds=3.0, profile=None,
    )
    agent = Agent(model, output_type=str, retries=0)
    with pytest.raises(Exception):
        await run_plan_text(agent, "draft", policy=PlanExecutionPolicy(5, 100, False),
                            deadline=time.monotonic() + 3)
    assert len(requests) == 1


def test_invalid_plan_budget_fails_before_request(monkeypatch):
    monkeypatch.setenv("FREEZONE_PRODUCTION_PLAN_OUTPUT_TOKENS", "0")
    with pytest.raises(ValueError, match="invalid_production_plan_output_tokens"):
        plan_execution_policy()


def test_default_output_budget_is_16k(monkeypatch):
    monkeypatch.delenv("FREEZONE_PRODUCTION_PLAN_OUTPUT_TOKENS", raising=False)
    assert plan_execution_policy().max_output_tokens == 16384


def test_explicit_output_budget_is_preserved(monkeypatch):
    monkeypatch.setenv("FREEZONE_PRODUCTION_PLAN_OUTPUT_TOKENS", "4096")
    assert plan_execution_policy().max_output_tokens == 4096


@pytest.mark.parametrize("stage,output_tokens", [("outline", 4096), ("arrangement", 12288)])
@pytest.mark.parametrize("environment_effort", [None, "high", "invalid"])
@pytest.mark.asyncio
async def test_plan_stage_wire_request_always_disables_reasoning(
    monkeypatch, stage, output_tokens, environment_effort,
):
    from dataclasses import replace

    import httpx
    from pydantic_ai import Agent, NativeOutput, StructuredDict

    import novelvideo.config as config
    from novelvideo.freezone.remix.production_plan_draft_stages import draft_stage_native_schema

    if environment_effort is None:
        monkeypatch.delenv("FREEZONE_PRODUCTION_PLAN_REASONING_EFFORT", raising=False)
    else:
        monkeypatch.setenv("FREEZONE_PRODUCTION_PLAN_REASONING_EFFORT", environment_effort)
    requests = []
    output = ({"output": {"width": 1080, "height": 1920, "fps_num": 30,
                          "fps_den": 1, "background": None},
               "beats": [], "creative_direction": "保留节奏"}
              if stage == "outline" else
              {"selections": [], "placements": [], "speech_bindings": {}})

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "chatcmpl-no-reasoning", "object": "chat.completion", "created": 1,
            "model": "brainclaw", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(output)}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    monkeypatch.setattr(
        config, "_newapi_text_http_client_factory",
        lambda *, timeout_seconds: lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(respond), timeout=timeout_seconds),
    )
    model = config._newapi_text_openai_model(
        "brainclaw", api_key="key", base_url="https://example.test/v1",
        timeout_seconds=3.0, profile=None,
    )
    agent = Agent(model, output_type=NativeOutput(
        StructuredDict(draft_stage_native_schema(stage), name=f"production_plan_{stage}"),
        strict=True, template=False,
    ), retries=0)
    policy = replace(plan_execution_policy(), max_output_tokens=output_tokens)

    await run_plan_text(agent, "draft", policy=policy, deadline=time.monotonic() + 3)

    assert len(requests) == 1
    assert requests[0]["reasoning_effort"] == "none"
    assert requests[0]["max_completion_tokens"] == output_tokens
    assert requests[0]["response_format"]["json_schema"]["strict"] is True


@pytest.mark.asyncio
async def test_stream_diagnostic_logs_only_timing_and_lengths(caplog):
    from novelvideo.llm_instrumentation import production_plan_diagnostic_context

    secret = "private model text"

    class FakeAgent:
        async def run(self, _task, *, model_settings, event_stream_handler):
            async def events():
                yield SimpleNamespace(part=SimpleNamespace(content=secret), delta=None)
            await event_stream_handler(None, events())
            return SimpleNamespace(output="{}", usage=lambda: SimpleNamespace(output_tokens=2))

    with caplog.at_level(logging.INFO, logger="novelvideo.llm_instrumentation"):
        with production_plan_diagnostic_context(task_id="task-one"):
            await run_plan_text(FakeAgent(), "draft", policy=PlanExecutionPolicy(5, 200, True),
                                deadline=time.monotonic() + 3)
    records = [json.loads(row.getMessage()) for row in caplog.records
               if row.getMessage().startswith("{")]
    assert records[0]["event"] == "production_plan.stream_timing"
    assert records[0]["streamed_characters"] == len(secret)
    assert records[0]["partial_output_seen"] is True
    assert secret not in caplog.text
