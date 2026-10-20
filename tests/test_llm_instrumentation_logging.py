from __future__ import annotations

import base64
import json
import logging
import io
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict
from pydantic_ai import BinaryContent, BinaryImage
from pydantic_ai.messages import ModelRequest, UserPromptPart

from novelvideo import llm_instrumentation
from novelvideo import ports
from novelvideo.generators import video_generator


def test_plan_diagnostic_is_emitted_by_default_without_root_info_logging():
    diagnostic = logging.getLogger("novelvideo.production_plan_diagnostic")
    stream = io.StringIO()
    original_handlers = diagnostic.handlers[:]
    original_level = diagnostic.level
    original_propagate = diagnostic.propagate
    handler = logging.StreamHandler(stream)
    try:
        diagnostic.handlers = [handler]
        root = logging.getLogger()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(root, "level", logging.WARNING)
            llm_instrumentation.log_production_plan_diagnostic(
                "production_plan.model_request_started", task_id="task-visible",
                stage="requesting")
        assert json.loads(stream.getvalue())["task_id"] == "task-visible"
        assert diagnostic.level <= logging.INFO
        assert diagnostic.propagate is False
    finally:
        diagnostic.handlers = original_handlers
        diagnostic.setLevel(original_level)
        diagnostic.propagate = original_propagate


def test_production_plan_diagnostics_redact_content_and_bound_record_size(caplog) -> None:
    secret = "sk-sensitive-prompt-" + "x" * 5000
    with caplog.at_level(logging.INFO, logger="novelvideo.llm_instrumentation"):
        with llm_instrumentation.production_plan_diagnostic_context(
            project_id="demo", task_id="task-1", model_call_id="call-1"
        ):
            llm_instrumentation.log_production_plan_diagnostic(
                "production_plan.model_request_failed",
                stage="requesting",
                attempt=1,
                timeout_seconds=540,
                error=TimeoutError(secret),
                prompt=secret,
                signed_url="https://example.test/video?token=" + secret,
            )
    logged = json.loads(caplog.records[-1].getMessage())
    assert logged["project_id"] == "demo"
    assert logged["task_id"] == "task-1"
    assert logged["model_call_id"] == "call-1"
    assert logged["error_code"] == "model_request_timeout"
    assert logged["error_type"] == "TimeoutError"
    assert logged["response_received"] is False
    assert "provider_http_status" not in logged
    assert "provider_request_id" not in logged
    assert "prompt" not in logged
    assert "signed_url" not in logged
    assert secret not in caplog.text
    assert len(caplog.records[-1].getMessage().encode("utf-8")) <= 2048


def test_production_plan_diagnostic_records_real_provider_response_only(caplog) -> None:
    class ProviderFailure(RuntimeError):
        status_code = 429
        request_id = "req-actual"

    with caplog.at_level(logging.INFO, logger="novelvideo.llm_instrumentation"):
        llm_instrumentation.log_production_plan_diagnostic(
            "production_plan.model_request_failed", stage="requesting",
            error=ProviderFailure("do not log this content"),
        )
    logged = json.loads(caplog.records[-1].getMessage())
    assert logged["provider_http_status"] == 429
    assert logged["provider_request_id"] == "req-actual"
    assert logged["response_received"] is True
    assert "do not log this content" not in caplog.text


def test_wrapped_output_limit_keeps_provider_status_and_request_id_in_task_sink() -> None:
    from novelvideo.freezone.remix.production_plan_execution import (
        PlanModelResponseInfo, PlanOutputLimitError,
    )

    class ProviderOutputLimit(RuntimeError):
        status_code = 400
        request_id = "req-original"
        body = {"error": {"code": "max_output_tokens"}}

    secret = "private response body"
    provider_error = ProviderOutputLimit(secret)
    wrapped = PlanOutputLimitError(PlanModelResponseInfo(
        finish_reason="max_tokens", output_tokens=None, total_output_tokens=None,
        reasoning_tokens=None, output_characters=0,
    ))
    wrapped.__cause__ = provider_error
    captured = []
    with llm_instrumentation.production_plan_diagnostic_sink(captured.append):
        llm_instrumentation.log_production_plan_diagnostic(
            "production_plan.model_request_failed", error=wrapped,
            error_code="model_output_limit",
        )
    assert captured[0]["provider_http_status"] == 400
    assert captured[0]["provider_request_id"] == "req-original"
    assert captured[0]["response_received"] is True
    assert secret not in json.dumps(captured[0])


def test_plan_diagnostic_sink_receives_only_bounded_sanitized_records() -> None:
    captured: list[dict] = []
    secret = "sk-secret-" + "x" * 5000
    with llm_instrumentation.production_plan_diagnostic_sink(captured.append):
        with llm_instrumentation.production_plan_diagnostic_context(task_id="task-a"):
            llm_instrumentation.log_production_plan_diagnostic(
                "production_plan.model_request_returned", finish_reason="stop",
                output_tokens=13, prompt=secret, signed_url="https://test?token=" + secret,
            )
    llm_instrumentation.log_production_plan_diagnostic("production_plan.runner_started")
    assert len(captured) == 1
    assert captured[0]["task_id"] == "task-a"
    assert captured[0]["output_tokens"] == 13
    assert "prompt" not in captured[0] and "signed_url" not in captured[0]
    assert len(json.dumps(captured[0], ensure_ascii=False).encode()) <= 2048


def test_plan_validation_diagnostic_keeps_count_and_safe_turn_identity() -> None:
    captured: list[dict] = []
    with llm_instrumentation.production_plan_diagnostic_sink(captured.append):
        llm_instrumentation.log_production_plan_diagnostic(
            "production_plan.validation_failed", stage="validating",
            validation_issue_count=51,
            validation_issues=[{
                "path": "/beats/0/turns/1", "code": "turn_exceeds_beat",
                "beat_id": "b1", "turn_id": "t2", "secret": "私密台词",
            }],
        )
    assert captured[0]["validation_issue_count"] == 51
    assert captured[0]["validation_issues"] == [{
        "path": "/beats/0/turns/1", "code": "turn_exceeds_beat",
        "beat_id": "b1", "turn_id": "t2",
    }]
    assert "私密台词" not in json.dumps(captured[0], ensure_ascii=False)


def test_plan_validation_diagnostic_retains_some_paths_under_log_cap() -> None:
    captured: list[dict] = []
    with llm_instrumentation.production_plan_diagnostic_sink(captured.append):
        llm_instrumentation.log_production_plan_diagnostic(
            "production_plan.validation_failed", stage="validating",
            validation_issue_count=51,
            validation_issues=[{
                "path": f"/beats/{index}/turns/12/start_frame",
                "code": "turn_exceeds_beat", "beat_id": "b" * 64,
                "turn_id": "t" * 64,
            } for index in range(20)],
        )
    assert captured[0]["validation_issue_count"] == 51
    assert 0 < len(captured[0]["validation_issues"]) < 20
    assert len(json.dumps(captured[0], ensure_ascii=False).encode()) <= 2048


def test_plan_diagnostic_sink_bounds_many_allowed_fields() -> None:
    captured = []
    identifiers = {key: "a" * 128 for key in (
        "project_id", "canvas_id", "node_id", "workflow_run_id", "operation_id",
        "task_id", "model_call_id", "model", "response_model", "route_profile",
        "provider_request_id",
    )}
    with llm_instrumentation.production_plan_diagnostic_sink(captured.append):
        with llm_instrumentation.production_plan_diagnostic_context(**identifiers):
            llm_instrumentation.log_production_plan_diagnostic(
                "production_plan.model_request_returned",
                **{key: 9999999 for key in llm_instrumentation._PLAN_DIAGNOSTIC_NUMBERS},
                finish_reason="output_limit",
            )
    assert len(json.dumps(captured[0], ensure_ascii=False).encode()) <= 2048
    assert captured[0]["task_id"] == identifiers["task_id"]
    assert captured[0]["model_call_id"] == identifiers["model_call_id"]


def test_plan_diagnostic_sink_failure_does_not_affect_request() -> None:
    def broken(_record):
        raise RuntimeError("diagnostic storage failed")
    with llm_instrumentation.production_plan_diagnostic_sink(broken):
        llm_instrumentation.log_production_plan_diagnostic("production_plan.runner_started")


def test_plan_task_log_sink_uses_exact_task_id_without_progress_change(monkeypatch) -> None:
    from novelvideo.task_backend.runners import remix
    from novelvideo import task_state

    rows = []

    class TaskManager:
        def get_task_for_project(self, *_args, **_kwargs):
            return SimpleNamespace(task_id="task-a", status="queued")

        def update_progress_for_project(self, *args, **kwargs):
            rows.append((args, kwargs))

    monkeypatch.setattr(task_state, "get_task_manager", lambda: TaskManager())
    ctx = SimpleNamespace(project_id="project-a")
    envelope = {"__run_task_id": "task-a", "episode": 2, "beat_num": None,
                "scope": "scope-a"}
    remix._persist_plan_diagnostic(
        envelope, ctx, {"event": "production_plan.model_request_returned",
                        "model_call_id": "call-a", "output_tokens": 100},
    )
    assert len(rows) == 1
    args, kwargs = rows[0]
    assert args[:3] == (ctx, "remix_generate_production_plan_draft", 2)
    assert kwargs["expected_task_id"] == "task-a"
    assert kwargs["scope"] == "scope-a"
    assert kwargs["logs"] == [json.dumps({"event": "production_plan.model_request_returned",
                                          "model_call_id": "call-a", "output_tokens": 100},
                                         ensure_ascii=False)]
    assert kwargs["status"] == "queued"
    assert "progress" not in kwargs and "current_task" not in kwargs


def test_plan_diagnostics_survive_failed_task_and_reject_stale_id(tmp_path, monkeypatch) -> None:
    from novelvideo.task_backend.runners import remix
    from novelvideo import task_state
    from novelvideo.project_context import ProjectContext

    manager = task_state.TaskStateManager()
    monkeypatch.setattr(task_state, "_task_manager", manager)
    ctx = ProjectContext(
        "project-a", "project-a", "user", "u", "alice", "u", "alice",
        (("user", "u"),), "owner", "local", tmp_path, tmp_path, tmp_path, True,
    )
    kind = "remix_generate_production_plan_draft"
    first = manager.create_task_for_project(ctx, kind, 0, scope="operation-a",
                                            status="queued")
    second = manager.create_task_for_project(ctx, kind, 0, scope="operation-b",
                                             status="running")
    envelope = {"__run_task_id": first.task_id, "episode": 0, "scope": "operation-a"}
    with llm_instrumentation.production_plan_diagnostic_sink(
        lambda record: remix._persist_plan_diagnostic(envelope, ctx, record)
    ):
        with llm_instrumentation.production_plan_diagnostic_context(
            task_id=first.task_id, operation_id="operation-a", model_call_id="call-a"
        ):
            llm_instrumentation.log_production_plan_diagnostic(
                "production_plan.model_request_returned", finish_reason="stop",
                output_tokens=33,
            )
    remix._persist_plan_diagnostic(
        {**envelope, "__run_task_id": "stale-task"}, ctx,
        {"event": "production_plan.runner_failed", "error_code": "json_parse_failed"},
    )
    assert manager.get_task_for_project(ctx, kind, 0, scope="operation-a").status == "queued"
    manager.fail_task_for_project(ctx, kind, 0, scope="operation-a",
                                  error="json_parse_failed", expected_task_id=first.task_id)
    saved = manager.get_task_for_project(ctx, kind, 0, scope="operation-a")
    other = manager.get_task_for_project(ctx, kind, 0, scope="operation-b")
    assert saved.status == "failed"
    diagnostics = [json.loads(entry) for entry in saved.logs if entry.startswith("{")]
    assert any(row["task_id"] == first.task_id and row["operation_id"] == "operation-a"
               and row["model_call_id"] == "call-a" and row["output_tokens"] == 33
               for row in diagnostics)
    assert not any(row["event"] == "production_plan.runner_failed" for row in diagnostics)
    assert other.task_id == second.task_id and other.logs == []


@pytest.mark.asyncio
async def test_plan_model_call_id_is_shared_with_metering(monkeypatch) -> None:
    from pydantic_ai import Agent

    captured: dict[str, object] = {}

    async def original_run(self, *args, **kwargs):
        return SimpleNamespace(output="ok")

    async def reserve(**kwargs):
        captured.update(kwargs["metadata"])
        return ""

    monkeypatch.setattr(Agent, "run", original_run)
    monkeypatch.setattr(llm_instrumentation, "_agent_run_patched", False)
    monkeypatch.setattr(llm_instrumentation, "_meter_reserve", reserve)
    monkeypatch.setattr(llm_instrumentation, "_forward_agent_usage", lambda *args, **kwargs: _async_none())
    llm_instrumentation._install_agent_run_patch()
    with llm_instrumentation.production_plan_diagnostic_context(model_call_id="call-42"):
        await Agent.run(SimpleNamespace(model="demo-model"), "test prompt")
    assert captured["model_call_id"] == "call-42"
    assert "test prompt" not in json.dumps(captured)
    assert captured["request_payload"]["input_characters"] == len("test prompt")


async def _async_none():
    return None


def test_json_log_value_keeps_diagnostic_fields_json_safe() -> None:
    class Result:
        def model_dump(self, *, mode: str):
            assert mode == "python"
            return {
                "api_key": "hm-7neo-rDyL-example",
                "prompt": "保留完整提示词",
                "binary": b"not-stored-verbatim",
            }

    assert llm_instrumentation._json_log_value(Result()) == {
        "api_key": "hm-7neo-rDyL-example",
        "prompt": "保留完整提示词",
        "binary": {"type": "bytes", "size_bytes": 19},
    }


def test_json_log_value_summarizes_binary_content_without_image_bytes() -> None:
    png_bytes = b"\x89PNG\r\n\x1a\n" + (b"frame-data" * 128)
    frames = [BinaryContent(data=png_bytes, media_type="image/png") for _ in range(10)]

    logged = llm_instrumentation._json_log_value(["分析这些视频关键帧", *frames])

    assert logged[0] == "分析这些视频关键帧"
    assert logged[1:] == [
        {
            "type": "BinaryContent",
            "media_type": "image/png",
            "size_bytes": len(png_bytes),
        }
        for _ in range(10)
    ]
    serialized = json.dumps(logged)
    assert "frame-data" not in serialized
    assert "BinaryContent(data=" not in serialized
    assert llm_instrumentation._json_log_value(frames[0], depth=8) == logged[1]


def test_json_log_value_summarizes_other_binary_data_wrappers() -> None:
    image = BinaryImage(data=b"image-data", media_type="image/webp")

    assert llm_instrumentation._json_log_value(image) == {
        "type": "BinaryImage",
        "media_type": "image/webp",
        "size_bytes": 10,
    }
    assert llm_instrumentation._json_log_value(memoryview(b"audio")) == {
        "type": "bytes",
        "size_bytes": 5,
    }


def test_json_log_value_recurses_through_message_history_dataclasses() -> None:
    png_bytes = b"\x89PNG\r\n\x1a\n" + (b"private-frame-data" * 128)
    request = ModelRequest(
        parts=[
            UserPromptPart(
                content=[
                    "保留完整提示词",
                    BinaryContent(data=png_bytes, media_type="image/png"),
                ]
            )
        ]
    )

    logged = llm_instrumentation._json_log_value({"message_history": [request]})

    content = logged["message_history"][0]["fields"]["parts"][0]["fields"][
        "content"
    ]
    assert content == [
        "保留完整提示词",
        {
            "type": "BinaryContent",
            "media_type": "image/png",
            "size_bytes": len(png_bytes),
        },
    ]
    serialized = json.dumps(logged, ensure_ascii=False)
    assert "保留完整提示词" in serialized
    assert "private-frame-data" not in serialized
    assert "BinaryContent(data=" not in serialized


def test_json_log_value_summarizes_binary_nested_in_pydantic_model() -> None:
    class RequestDeps(BaseModel):
        model_config = ConfigDict(arbitrary_types_allowed=True)

        prompt: str
        attachment: BinaryContent

    media_bytes = b"private-pydantic-media" * 256
    deps = RequestDeps(
        prompt="保留 Pydantic 提示词",
        attachment=BinaryContent(data=media_bytes, media_type="image/png"),
    )

    logged = llm_instrumentation._json_log_value(deps)

    assert logged["prompt"] == "保留 Pydantic 提示词"
    assert logged["attachment"]["data"] == {
        "type": "bytes",
        "size_bytes": len(media_bytes),
    }
    assert logged["attachment"]["media_type"] == "image/png"
    serialized = json.dumps(logged, ensure_ascii=False)
    assert base64.b64encode(media_bytes).decode("ascii") not in serialized
    assert "private-pydantic-media" not in serialized


def test_failure_log_metadata_keeps_only_normalized_provider_http_status() -> None:
    class ProviderRejection(RuntimeError):
        status_code = 429

    rejected = llm_instrumentation._failure_log_metadata(
        ProviderRejection("rate limited")
    )
    unknown = llm_instrumentation._failure_log_metadata(RuntimeError("timeout"))

    assert rejected["provider_http_status"] == 429
    assert "provider_http_status" not in unknown


@pytest.mark.asyncio
async def test_meter_refund_forwards_log_metadata_without_changing_reservation_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict | None]] = []

    class Meter:
        async def refund_model_call_credit_reservation(
            self, reservation_id: str, *, metadata: dict | None = None
        ) -> None:
            calls.append((reservation_id, metadata))

    monkeypatch.setattr(ports, "get_usage_meter", lambda: Meter())

    metadata = llm_instrumentation._failure_log_metadata(
        RuntimeError("provider failed")
    )
    await llm_instrumentation._meter_refund("reservation_1", metadata=metadata)

    assert calls == [("reservation_1", metadata)]
    assert metadata["error_message"] == "provider failed"
    assert metadata["response_payload"]["status"] == "failed"


@pytest.mark.asyncio
async def test_video_failure_forwards_empty_reservation_for_observability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict | None]] = []

    class Meter:
        async def refund_model_call_credit_reservation(
            self, reservation_id: str, *, metadata: dict | None = None
        ) -> None:
            calls.append((reservation_id, metadata))

    monkeypatch.setattr(video_generator, "get_usage_meter", lambda: Meter())

    await video_generator._refund_video_model_call(
        "",
        source="seedance_2",
        error="provider unavailable",
    )

    assert calls == [
        (
            "",
            {"source": "seedance_2", "error": "provider unavailable"},
        )
    ]


@pytest.mark.asyncio
async def test_feature_included_agent_and_litellm_share_one_instrumentation_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic_ai import Agent

    events: list[tuple[str, object]] = []
    fake_litellm = SimpleNamespace()

    async def provider_acompletion(*args, **kwargs):
        events.append(
            (
                "provider",
                llm_instrumentation._MODEL_CALL_INSTRUMENTATION_ACTIVE.get(),
            )
        )
        return SimpleNamespace(id="response_1", usage={})

    fake_litellm.acompletion = provider_acompletion

    async def original_agent_run(self, *args, **kwargs):
        await fake_litellm.acompletion(model="gateway-text-model", messages=[])
        return SimpleNamespace(output="ok")

    async def reserve_feature_included(**kwargs):
        events.append(("reserve", kwargs["metadata"]["source"]))
        return ""

    async def forward_agent_usage(self, result, *, credit_reservation_id: str):
        events.append(("finish", credit_reservation_id))

    monkeypatch.setattr(Agent, "run", original_agent_run)
    monkeypatch.setattr(llm_instrumentation, "_agent_run_patched", False)
    monkeypatch.setattr(llm_instrumentation, "_litellm_acompletion_patched", False)
    monkeypatch.setattr(llm_instrumentation, "_meter_reserve", reserve_feature_included)
    monkeypatch.setattr(
        llm_instrumentation, "_forward_agent_usage", forward_agent_usage
    )

    llm_instrumentation._patch_litellm_acompletion(fake_litellm)
    llm_instrumentation._install_agent_run_patch()

    result = await Agent.run(SimpleNamespace(model="gateway-text-model"), "prompt")

    assert result.output == "ok"
    assert events == [
        ("reserve", "pydantic_ai_agent_run"),
        ("provider", True),
        ("finish", ""),
    ]
    assert llm_instrumentation._MODEL_CALL_INSTRUMENTATION_ACTIVE.get() is False
