from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from novelvideo.brainclaw_contract import (
    BrainClawProfile,
    BrainClawProfileVariant,
)
from novelvideo.freezone import recipe_runtime
from novelvideo import llm_instrumentation
from novelvideo.freezone.agent_catalog_schema import validate_agent_recipe_config


@pytest.mark.asyncio
async def test_plan_invalid_json_stops_after_one_attempt_without_content(monkeypatch, caplog):
    from novelvideo.freezone.remix import production_plan
    from test_production_plan import native_plan

    secret = "full-original-video-transcript"
    replies = ["not-json-" + secret, json.dumps(native_plan(), ensure_ascii=False)]

    async def generate(**_kwargs):
        return replies.pop(0)

    monkeypatch.setattr(production_plan, "get_recipe_for_runtime", lambda **_kwargs: {})
    monkeypatch.setattr(production_plan, "generate_recipe_text", generate)
    monkeypatch.setattr(production_plan, "validate_structured_recipe_output", lambda *_args: None)
    with caplog.at_level(logging.INFO, logger="novelvideo.llm_instrumentation"):
        with llm_instrumentation.production_plan_diagnostic_context(task_id="task-1"):
            with pytest.raises(ValueError, match="json_parse_failed"):
                await production_plan.generate_production_plan_draft(
                    topic="做个新故事", blueprint=None, username="alice",
                )
    events = [json.loads(record.getMessage()) for record in caplog.records
              if record.name == "novelvideo.llm_instrumentation" and record.getMessage().startswith("{")]
    assert len(replies) == 1
    assert [item["attempt"] for item in events if item["event"] == "production_plan.validation_started"] == [1]
    assert any(item["event"] == "production_plan.validation_failed" and item["error_code"] == "json_parse_failed"
               and item["attempt"] == 1 for item in events)
    assert not any(item["event"] == "production_plan.validation_succeeded" for item in events)
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_plan_output_preferences_distinguish_source_clip_duration_from_film(monkeypatch):
    from novelvideo.freezone.remix import production_plan
    from test_production_plan import native_plan, _stage_response

    captured: dict[str, object] = {}

    async def generate(**kwargs):
        captured.update(kwargs)
        return _stage_response(native_plan(), kwargs)

    monkeypatch.setattr(production_plan, "get_recipe_for_runtime", lambda **_kwargs: {})
    monkeypatch.setattr(production_plan, "generate_recipe_text", generate)
    monkeypatch.setattr(production_plan, "validate_structured_recipe_output", lambda *_args: None)
    await production_plan.generate_production_plan_draft(
        topic="做一条黑咖啡短片", blueprint=None, username="alice",
        output_preferences={"aspect_ratio": "9:16", "source_clip_duration_seconds": 5},
    )
    assert captured["user_goal"] == "做一条黑咖啡短片"
    context = json.loads(captured["upstream_text"])
    assert context["output_preferences"]["source_clip_duration_seconds"] == 5
    assert "单条素材" in captured["node_prompt"]
    assert "成片总时长" in captured["node_prompt"]


@pytest.mark.asyncio
async def test_plan_recipe_timeout_logs_correlated_request_without_prompt(monkeypatch, caplog):
    secret = "secret-source-transcript-content"
    monkeypatch.setattr(recipe_runtime, "get_recipe_for_runtime", lambda **_kwargs: {
        "id": "reference-production-plan", "version": "1", "output_kind": "text",
        "system_prompt": secret, "_catalog_source": "builtin",
    })

    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self, _task):
            raise TimeoutError(secret)

    monkeypatch.setattr(recipe_runtime, "Agent", FakeAgent)
    monkeypatch.setattr("novelvideo.config.get_newapi_text_pydantic_model",
                        lambda *_args, **_kwargs: SimpleNamespace(model_name="real-route-model"))
    with caplog.at_level(logging.INFO, logger="novelvideo.llm_instrumentation"):
        with llm_instrumentation.production_plan_diagnostic_context(
            project_id="demo", workflow_run_id="run-1", operation_id="op-1", task_id="task-1", attempt="1"
        ):
            with pytest.raises(TimeoutError):
                await recipe_runtime.generate_recipe_text(
                    username="local", recipe_id="reference-production-plan", node_kind="text",
                    node_prompt=secret, user_goal=secret,
                )
    events = [json.loads(record.getMessage()) for record in caplog.records
              if record.name == "novelvideo.llm_instrumentation" and record.getMessage().startswith("{")]
    started = next(item for item in events if item["event"] == "production_plan.model_request_started")
    failed = next(item for item in events if item["event"] == "production_plan.model_request_failed")
    assert started["model_call_id"] == failed["model_call_id"]
    assert started["workflow_run_id"] == failed["workflow_run_id"] == "run-1"
    assert started["model"] == "real-route-model"
    assert started["timeout_seconds"] == 540
    assert failed["stage"] == "requesting"
    assert failed["response_received"] is False
    assert failed["elapsed_ms"] >= 0
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_plan_recipe_logs_actual_usage_and_retry_count_when_available(monkeypatch, caplog):
    monkeypatch.setattr(recipe_runtime, "get_recipe_for_runtime", lambda **_kwargs: {
        "id": "reference-production-plan", "version": "1", "output_kind": "text",
        "system_prompt": "draft", "_catalog_source": "builtin",
    })

    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self, _task):
            return SimpleNamespace(
                output="short output", retry_count=1, request_id="req-123",
                usage=lambda: SimpleNamespace(input_tokens=42, output_tokens=17),
            )

    monkeypatch.setattr(recipe_runtime, "Agent", FakeAgent)
    monkeypatch.setattr("novelvideo.config.get_newapi_text_pydantic_model",
                        lambda *_args, **_kwargs: SimpleNamespace(model_name="route-model", available_tokens=8192))
    with caplog.at_level(logging.INFO, logger="novelvideo.llm_instrumentation"):
        with llm_instrumentation.production_plan_diagnostic_context(task_id="task-1"):
            await recipe_runtime.generate_recipe_text(
                username="local", recipe_id="reference-production-plan", node_kind="text", node_prompt="draft",
            )
    events = [json.loads(record.getMessage()) for record in caplog.records
              if record.name == "novelvideo.llm_instrumentation" and record.getMessage().startswith("{")]
    started = next(item for item in events if item["event"] == "production_plan.model_request_started")
    returned = next(item for item in events if item["event"] == "production_plan.model_request_returned")
    assert started["available_tokens"] == 8192
    assert returned["input_tokens"] == 42
    assert returned["output_tokens"] == 17
    assert returned["sdk_retry_count"] == 1
    assert returned["provider_request_id"] == "req-123"


@pytest.mark.asyncio
async def test_plan_recipe_disables_agent_retries_and_logs_one_return(monkeypatch, caplog):
    monkeypatch.setattr(recipe_runtime, "get_recipe_for_runtime", lambda **_kwargs: {
        "id": "reference-production-plan", "version": "1", "output_kind": "text",
        "system_prompt": "draft", "_catalog_source": "builtin",
    })
    agent_options = []

    class FakeAgent:
        def __init__(self, *_args, **kwargs):
            agent_options.append(kwargs)

        async def run(self, _task, **_kwargs):
            return SimpleNamespace(output="{}", usage=lambda: SimpleNamespace(output_tokens=2))

    monkeypatch.setattr(recipe_runtime, "Agent", FakeAgent)
    monkeypatch.setattr("novelvideo.config.get_newapi_text_pydantic_model",
                        lambda *_args, **_kwargs: SimpleNamespace(model_name="writer"))
    with caplog.at_level(logging.INFO, logger="novelvideo.llm_instrumentation"):
        with llm_instrumentation.production_plan_diagnostic_context(task_id="task-1"):
            await recipe_runtime.generate_recipe_text(
                username="local", recipe_id="reference-production-plan", node_kind="text",
                node_prompt="draft", plan_deadline=recipe_runtime.time.monotonic() + 3,
            )
    records = [json.loads(row.getMessage()) for row in caplog.records
               if row.name == "novelvideo.llm_instrumentation" and row.getMessage().startswith("{")]
    assert agent_options[0]["retries"] == 0
    assert sum(row["event"] == "production_plan.model_request_returned" for row in records) == 1


@pytest.mark.asyncio
async def test_known_model_output_limit_rejects_plan_budget_before_request(monkeypatch, caplog):
    monkeypatch.setattr(recipe_runtime, "get_recipe_for_runtime", lambda **_kwargs: {
        "id": "reference-production-plan", "version": "1", "output_kind": "text",
        "system_prompt": "draft", "_catalog_source": "builtin",
    })
    called = []

    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            called.append(True)

    monkeypatch.setattr(recipe_runtime, "Agent", FakeAgent)
    monkeypatch.setattr("novelvideo.config.get_newapi_text_pydantic_model",
                        lambda *_args, **_kwargs: SimpleNamespace(
                            model_name="limited-writer", max_output_tokens=8192))
    with caplog.at_level(logging.INFO, logger="novelvideo.llm_instrumentation"):
        with llm_instrumentation.production_plan_diagnostic_context(task_id="task-a"):
            with pytest.raises(ValueError, match="production_plan_output_budget_exceeds_model_limit"):
                await recipe_runtime.generate_recipe_text(
                    username="local", recipe_id="reference-production-plan", node_kind="text",
                    node_prompt="draft", plan_deadline=recipe_runtime.time.monotonic() + 3,
                )
    assert called == []
    records = [json.loads(row.getMessage()) for row in caplog.records
               if row.name == "novelvideo.llm_instrumentation" and row.getMessage().startswith("{")]
    assert any(row["event"] == "production_plan.model_configuration_failed"
               and row["error_code"] == "production_plan_output_budget_exceeds_model_limit"
               for row in records)


@pytest.mark.asyncio
async def test_structured_provider_limit_is_logged_without_response_text(monkeypatch, caplog):
    secret = "private full transcript"
    monkeypatch.setattr(recipe_runtime, "get_recipe_for_runtime", lambda **_kwargs: {
        "id": "reference-production-plan", "version": "1", "output_kind": "text",
        "system_prompt": "draft", "_catalog_source": "builtin",
    })

    class ProviderOutputLimit(RuntimeError):
        body = {"error": {"code": "max_output_tokens"}}
        status_code = 400
        request_id = "req-structured-limit"

    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self, _task, **_kwargs):
            raise ProviderOutputLimit(secret)

    monkeypatch.setattr(recipe_runtime, "Agent", FakeAgent)
    monkeypatch.setattr("novelvideo.config.get_newapi_text_pydantic_model",
                        lambda *_args, **_kwargs: SimpleNamespace(model_name="writer"))
    with caplog.at_level(logging.INFO, logger="novelvideo.llm_instrumentation"):
        with llm_instrumentation.production_plan_diagnostic_context(
            operation_id="operation-a", task_id="task-a"
        ):
            with pytest.raises(ValueError, match="model_output_limit"):
                await recipe_runtime.generate_recipe_text(
                    username="local", recipe_id="reference-production-plan",
                    node_kind="text", node_prompt=secret,
                    plan_deadline=recipe_runtime.time.monotonic() + 3,
                )
    records = [json.loads(row.getMessage()) for row in caplog.records
               if row.name == "novelvideo.llm_instrumentation" and row.getMessage().startswith("{")]
    failed = next(row for row in records if row["event"] == "production_plan.model_request_failed")
    assert failed["finish_reason"] == "max_tokens"
    assert failed["error_code"] == "model_output_limit"
    assert failed["provider_http_status"] == 400
    assert failed["provider_request_id"] == "req-structured-limit"
    assert failed["response_received"] is True
    assert failed["operation_id"] == "operation-a"
    assert failed["task_id"] == "task-a"
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_plan_recipe_passes_empty_response_to_json_validation(monkeypatch):
    monkeypatch.setattr(recipe_runtime, "get_recipe_for_runtime", lambda **_kwargs: {
        "id": "reference-production-plan", "version": "1", "output_kind": "text",
        "system_prompt": "draft", "_catalog_source": "builtin",
    })

    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self, _task, **_kwargs):
            return SimpleNamespace(output="", usage=lambda: None)

    monkeypatch.setattr(recipe_runtime, "Agent", FakeAgent)
    monkeypatch.setattr("novelvideo.config.get_newapi_text_pydantic_model",
                        lambda *_args, **_kwargs: SimpleNamespace(model_name="writer"))
    content = await recipe_runtime.generate_recipe_text(
        username="local", recipe_id="reference-production-plan", node_kind="text",
        node_prompt="draft", plan_deadline=recipe_runtime.time.monotonic() + 3,
    )
    assert content == ""


def test_builtin_text_recipes_produce_final_deliverables():
    recipe_root = (
        Path(__file__).resolve().parents[1]
        / "src/novelvideo/freezone/agent_catalog/builtins/recipes"
    )
    text_recipe_ids: list[str] = []
    for recipe_path in sorted(recipe_root.glob("*.json")):
        payload = json.loads(recipe_path.read_text(encoding="utf-8"))
        if payload.get("output_kind") != "text":
            continue
        validated = validate_agent_recipe_config(payload)
        text_recipe_ids.append(str(validated["id"]))

    assert len(text_recipe_ids) == 28
    assert "general-text" in text_recipe_ids
    assert "ecommerce-text-plan" in text_recipe_ids
    assert {"reference-blueprint", "reference-remix-script", "reference-broll-plan", "reference-production-plan"} <= set(text_recipe_ids)


@pytest.mark.parametrize(
    "recipe_id",
    ["sci-fi-survival-shot-video", "anthropomorphic-kungfu-shot-video"],
)
def test_voiced_builtin_video_recipes_pass_strict_catalog_validation(recipe_id):
    recipe_path = (
        Path(__file__).resolve().parents[1]
        / "src/novelvideo/freezone/agent_catalog/builtins/recipes"
        / f"{recipe_id}.json"
    )
    payload = json.loads(recipe_path.read_text(encoding="utf-8"))

    validated = validate_agent_recipe_config(payload)

    assert validated["id"] == recipe_id
    assert validated["requires_generated_audio"] is True


def test_generated_audio_requirement_cannot_be_attached_to_text_recipe():
    recipe_path = (
        Path(__file__).resolve().parents[1]
        / "src/novelvideo/freezone/agent_catalog/builtins/recipes/general-text.json"
    )
    payload = json.loads(recipe_path.read_text(encoding="utf-8"))
    payload["requires_generated_audio"] = True

    with pytest.raises(
        ValueError, match="requires_generated_audio requires output_kind=video"
    ):
        validate_agent_recipe_config(payload)


def test_text_recipe_rejects_second_stage_model_instruction():
    with pytest.raises(ValueError, match="must produce the final deliverable"):
        validate_agent_recipe_config(
            {
                "id": "invalid-text-recipe",
                "name": "Invalid text Recipe",
                "output_kind": "text",
                "action_keys": ["invalid-text-recipe"],
                "system_prompt": "不要自己编写大纲，只输出能指导 LLM 的指令。",
                "planning_prompt": "生成大纲",
                "result_summary": "大纲",
            }
        )


def test_authoring_guide_text_recipe_template_produces_final_deliverable():
    guide_path = (
        Path(__file__).resolve().parents[1]
        / ".hermes/skills/freezone/references/skill-studio-authoring-guide.md"
    )
    guide = guide_path.read_text(encoding="utf-8")
    text_recipe_section = guide.split("#### 文字 Recipe", 1)[1]
    system_prompt = text_recipe_section.split("```text", 1)[1].split("```", 1)[0].strip()

    validated = validate_agent_recipe_config(
        {
            "id": "guide-text-template",
            "name": "Guide text template",
            "output_kind": "text",
            "action_keys": ["guide-text-template"],
            "system_prompt": system_prompt,
            "planning_prompt": "根据已确认输入直接生成最终文案。",
            "result_summary": "可直接交付的最终文案。",
        }
    )

    assert validated["output_kind"] == "text"
    assert "直接生成可交付的最终文本" in validated["system_prompt"]


def test_retro_skill_keeps_entry_guard_out_of_recipe_runtime_constraints():
    skill_path = (
        Path(__file__).resolve().parents[1]
        / "src/novelvideo/freezone/agent_catalog/builtins/skills"
        / "retro-hong-kong-kungfu-comedy-video.json"
    )
    skill = json.loads(skill_path.read_text(encoding="utf-8"))

    constraints = recipe_runtime._skill_constraints(skill)
    compiled_constraints = "\n".join(constraints["hard_constraints"])

    assert "请先上传至少一张角色参考图或一句文字灵感" not in compiled_constraints
    assert "暂停等待导演确认" not in compiled_constraints
    assert "阶段标题使用【模块名】格式" not in constraints["prompt_guide"]
    assert "一次性在草稿中列出完整工作流" in skill["planning"]["planning_notes"]


def test_kungfu_shot_recipe_requires_per_shot_action_phases():
    recipe_path = (
        Path(__file__).resolve().parents[1]
        / "src/novelvideo/freezone/agent_catalog/builtins/recipes"
        / "anthropomorphic-kungfu-shot-video.json"
    )
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    prompt = recipe["system_prompt"]

    assert "当前 Shot 的每个核心动作逐项写出" in prompt
    assert "不得只写泛化的三段式要求" in prompt
    assert "每个视频节点自身" in prompt
    assert "逐动作预备/运动/完成" in recipe["must_have_items"]


def test_outdoor_stage_duel_character_elements_use_one_turnaround_reference():
    recipe_path = (
        Path(__file__).resolve().parents[1]
        / "src/novelvideo/freezone/agent_catalog/builtins/recipes"
        / "outdoor-stage-duel-key-elements.json"
    )
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))

    assert "正面、侧面、背面" in recipe["system_prompt"]
    assert "全身三视图" in "\n".join(recipe["must_have_items"])


def test_outdoor_stage_duel_video_recipe_preserves_confirmed_beat_order():
    recipe_path = (
        Path(__file__).resolve().parents[1]
        / "src/novelvideo/freezone/agent_catalog/builtins/recipes"
        / "outdoor-stage-duel-shot-video.json"
    )
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    prompt = recipe["system_prompt"]

    assert "已确认" in prompt and "Beat" in prompt
    assert "Beat 3 中心峰值碰撞和明确受力反馈；Beat 4 反击" not in prompt
    assert "逐 Beat 保留已确认 Storyboard/用户顺序和动作" in prompt
    assert "若未提供确认顺序" in prompt
    assert "continuous handheld long take" in prompt
    assert "Three-layer composition" in prompt
    assert "First-person audience POV" in prompt
    assert "no visible phone body" in prompt
    assert "hard cuts" in prompt


def test_recipe_compiler_uses_the_dedicated_brainclaw_profile(monkeypatch):
    captured: dict[str, object] = {}

    def fake_model(model_env, default_model=None, **kwargs):
        captured.update(
            model_env=model_env,
            default_model=default_model,
            **kwargs,
        )
        return "model"

    class FakeAgent:
        def __init__(self, model, **kwargs):
            captured.update(model=model, agent_kwargs=kwargs)

        async def run(self, _task, **kwargs):
            captured.update(run_kwargs=kwargs)
            return SimpleNamespace(output="compiled prompt")

    from novelvideo import config

    monkeypatch.setattr(config, "get_newapi_text_pydantic_model", fake_model)
    monkeypatch.setattr(recipe_runtime, "Agent", FakeAgent)

    result = asyncio.run(recipe_runtime._run_recipe_compiler("compile this"))

    assert result.prompt == "compiled prompt"
    assert result.model_call_id.startswith("recipe-compiler:")
    assert result.executed_at > 0
    assert captured["brainclaw_profile"] is BrainClawProfile.FREEZONE_RECIPE_COMPILATION
    assert captured["run_kwargs"] == {
        "model_settings": {"openai_reasoning_effort": "low"}
    }


def test_recipe_compiler_reasoning_effort_is_configurable(monkeypatch):
    monkeypatch.setenv("FREEZONE_RECIPE_COMPILER_REASONING_EFFORT", "HIGH")

    assert recipe_runtime._recipe_compiler_reasoning_effort() == "high"


def test_recipe_compiler_reasoning_effort_rejects_unsupported_value(monkeypatch):
    monkeypatch.setenv("FREEZONE_RECIPE_COMPILER_REASONING_EFFORT", "none")

    with pytest.raises(
        recipe_runtime.RecipeRuntimeError,
        match="expected one of: high, low, max",
    ):
        recipe_runtime._recipe_compiler_reasoning_effort()


def test_build_recipe_compiler_task_checks_output_kind():
    with pytest.raises(recipe_runtime.RecipeRuntimeError, match="incompatible"):
        recipe_runtime.build_recipe_compiler_task(
            recipe={
                "id": "image-only",
                "output_kind": "image",
                "system_prompt": "refine",
            },
            node_kind="video",
            node_prompt="rotate product",
        )


def test_build_recipe_compiler_task_contains_runtime_context():
    task = recipe_runtime.build_recipe_compiler_task(
        recipe={"id": "scene", "output_kind": "image", "system_prompt": "商业摄影"},
        node_kind="image",
        node_prompt="北欧厨房",
        user_goal="生成三张咖啡机商品图",
        upstream_text="银色金属机身",
        reference_media=[{"kind": "image", "label": "产品参考图"}],
        confirmed_inputs={"aspect_ratio": "9:16", "language": "zh"},
        skill_constraints={
            "hard_constraints": ["不得虚构产品功能", "不要字幕"],
            "prompt_guide": "高端商业摄影",
        },
        skill_id="ecommerce",
        skill_version="2.1.0",
    )

    assert "商业摄影" in task
    assert "北欧厨房" in task
    assert "银色金属机身" in task
    assert "产品参考图" in task
    assert '"aspect_ratio": "9:16"' in task
    assert "不得虚构产品功能" in task
    assert "高端商业摄影" in task
    assert '"version": "2.1.0"' in task


def test_get_skill_for_runtime_enforces_version_and_recipe_whitelist(monkeypatch):
    monkeypatch.setattr(
        recipe_runtime,
        "list_user_agent_config_items",
        lambda _username, kind: (
            [
                {
                    "id": "ecommerce",
                    "version": "2.1.0",
                    "allowed_recipe_ids": ["product-image"],
                }
            ]
            if kind == "skills"
            else []
        ),
    )

    skill = recipe_runtime.get_skill_for_runtime(
        username="local",
        skill_id="ecommerce",
        skill_version="2.1.0",
        recipe_id="product-image",
    )
    assert skill is not None

    with pytest.raises(recipe_runtime.RecipeRuntimeError, match="version mismatch"):
        recipe_runtime.get_skill_for_runtime(
            username="local",
            skill_id="ecommerce",
            skill_version="1.0.0",
            recipe_id="product-image",
        )
    with pytest.raises(recipe_runtime.RecipeRuntimeError, match="not allowed"):
        recipe_runtime.get_skill_for_runtime(
            username="local",
            skill_id="ecommerce",
            recipe_id="other-image",
        )


def test_combined_recipe_preserves_pipeline_order():
    combined = recipe_runtime._combined_recipe(
        [
            {
                "id": "shotlist",
                "name": "分镜方法",
                "version": 1,
                "output_kind": "image",
                "system_prompt": "先确定镜头信息。",
            },
            {
                "id": "lighting",
                "name": "布光方法",
                "version": 2,
                "output_kind": "image",
                "system_prompt": "再设计光线。",
            },
        ]
    )

    assert combined["id"] == "shotlist+lighting"
    assert combined["system_prompt"].index("先确定镜头信息") < combined[
        "system_prompt"
    ].index("再设计光线")


def test_recipe_compiler_priority_places_skill_before_recipe():
    assert (
        "confirmed inputs, Skill hard constraints"
        in recipe_runtime._RECIPE_COMPILER_SYSTEM_PROMPT
    )
    assert (
        "Recipe method, then defaults" in recipe_runtime._RECIPE_COMPILER_SYSTEM_PROMPT
    )


def test_build_recipe_compiler_task_limits_large_upstream_context():
    upstream = "A" * 20_000 + "B" * 20_000
    task = recipe_runtime.build_recipe_compiler_task(
        recipe={"id": "scene", "output_kind": "image", "system_prompt": "商业摄影"},
        node_kind="image",
        node_prompt="厨房",
        upstream_text=upstream,
    )

    assert "[context truncated]" in task
    assert "A" * 100 in task
    assert "B" * 100 in task
    assert len(task) < 18_000


@pytest.mark.asyncio
async def test_compile_recipe_prompt_loads_server_recipe_and_returns_only_prompt(
    monkeypatch, tmp_path
):
    recipe_runtime._prompt_cache.clear()
    recipe_runtime._prompt_inflight.clear()
    monkeypatch.setattr(recipe_runtime, "OUTPUT_DIR", tmp_path)
    calls = 0
    monkeypatch.setattr(
        recipe_runtime,
        "get_recipe_for_runtime",
        lambda **_kwargs: {
            "id": "scene",
            "version": 1,
            "output_kind": "image",
            "system_prompt": "trusted internal method",
        },
    )

    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self, task, **_kwargs):
            nonlocal calls
            calls += 1
            assert "trusted internal method" in task
            return SimpleNamespace(output="最终可执行提示词")

    monkeypatch.setattr(recipe_runtime, "Agent", FakeAgent)
    monkeypatch.setattr(
        "novelvideo.config.get_newapi_text_pydantic_model",
        lambda *_args, **_kwargs: object(),
    )

    compiled = await recipe_runtime.compile_recipe_prompt_result(
        username="local",
        recipe_id="scene",
        recipe_version="1",
        node_kind="image",
        node_prompt="厨房场景",
    )
    cached = await recipe_runtime.compile_recipe_prompt_result(
        username="local",
        recipe_id="scene",
        recipe_version="1",
        node_kind="image",
        node_prompt="厨房场景",
    )

    assert compiled.prompt == "最终可执行提示词"
    assert compiled.mode == "model"
    assert cached.prompt == compiled.prompt
    assert cached.mode == "memory_cache"
    assert calls == 1

    recipe_runtime._prompt_cache.clear()
    persisted = await recipe_runtime.compile_recipe_prompt_result(
        username="local",
        recipe_id="scene",
        recipe_version="1",
        node_kind="image",
        node_prompt="厨房场景",
    )
    assert persisted.prompt == compiled.prompt
    assert persisted.mode == "persistent_cache"
    assert calls == 1

    compatible_prompt = await recipe_runtime.compile_recipe_prompt(
        username="local",
        recipe_id="scene",
        recipe_version="1",
        node_kind="image",
        node_prompt="厨房场景",
    )
    assert compatible_prompt == compiled.prompt


@pytest.mark.asyncio
async def test_compile_recipe_prompt_deduplicates_concurrent_model_calls(
    monkeypatch, tmp_path
):
    recipe_runtime._prompt_cache.clear()
    recipe_runtime._prompt_inflight.clear()
    monkeypatch.setattr(recipe_runtime, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(
        recipe_runtime,
        "get_recipe_for_runtime",
        lambda **_kwargs: {
            "id": "scene",
            "version": 1,
            "output_kind": "image",
            "system_prompt": "trusted internal method",
        },
    )
    calls = 0

    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self, _task, **_kwargs):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return SimpleNamespace(output="共享提示词")

    monkeypatch.setattr(recipe_runtime, "Agent", FakeAgent)
    monkeypatch.setattr(
        "novelvideo.config.get_newapi_text_pydantic_model",
        lambda *_args, **_kwargs: object(),
    )
    kwargs = {
        "username": "local",
        "recipe_id": "scene",
        "recipe_version": "1",
        "node_kind": "image",
        "node_prompt": "厨房场景",
    }

    results = await asyncio.gather(
        recipe_runtime.compile_recipe_prompt(**kwargs),
        recipe_runtime.compile_recipe_prompt(**kwargs),
    )

    assert results == ["共享提示词", "共享提示词"]
    assert calls == 1


@pytest.mark.asyncio
async def test_compile_recipe_prompt_uses_fallback_on_timeout_and_caches_late_result(
    monkeypatch,
    tmp_path,
):
    recipe_runtime._prompt_cache.clear()
    recipe_runtime._prompt_inflight.clear()
    monkeypatch.setattr(recipe_runtime, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(
        recipe_runtime,
        "get_recipe_for_runtime",
        lambda **_kwargs: {
            "id": "scene",
            "version": 1,
            "output_kind": "image",
            "system_prompt": "可信的商品摄影方法",
        },
    )

    async def slow_compiler(_task: str) -> str:
        await asyncio.sleep(0.03)
        return "后台完成的精炼提示词"

    monkeypatch.setattr(recipe_runtime, "_run_recipe_compiler", slow_compiler)
    monkeypatch.setattr(
        recipe_runtime, "_recipe_compiler_timeout_seconds", lambda: 0.01
    )

    fallback_result = await recipe_runtime.compile_recipe_prompt_result(
        username="local",
        recipe_id="scene",
        recipe_version="1",
        node_kind="image",
        node_prompt="厨房场景",
        user_goal="生成咖啡机商品图",
    )

    assert fallback_result.mode == "timeout_fallback"
    assert fallback_result.recipe_ids == ("scene",)
    assert "生成咖啡机商品图" in fallback_result.prompt
    assert "厨房场景" in fallback_result.prompt
    assert "可信的商品摄影方法" in fallback_result.prompt

    await asyncio.sleep(0.04)
    assert recipe_runtime._prompt_inflight == {}
    assert "后台完成的精炼提示词" in recipe_runtime._prompt_cache.values()

    cached = await recipe_runtime.compile_recipe_prompt(
        username="local",
        recipe_id="scene",
        recipe_version="1",
        node_kind="image",
        node_prompt="厨房场景",
        user_goal="生成咖啡机商品图",
    )
    assert cached == "后台完成的精炼提示词"


@pytest.mark.asyncio
async def test_compile_recipe_prompt_skips_model_for_deterministic_strategy(
    monkeypatch,
):
    monkeypatch.setattr(
        recipe_runtime,
        "get_recipe_for_runtime",
        lambda **_kwargs: {
            "id": "video",
            "output_kind": "video",
            "system_prompt": "refine video prompt",
        },
    )

    class UnexpectedAgent:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("deterministic strategy must not create an Agent")

    monkeypatch.setattr(recipe_runtime, "Agent", UnexpectedAgent)
    result = await recipe_runtime.compile_recipe_prompt(
        username="local",
        recipe_id="video",
        node_kind="video",
        node_prompt="镜头缓慢推进",
        upstream_text="商品分镜",
        prompt_strategy="previous_output",
    )

    assert result == "商品分镜\n\n镜头缓慢推进"


@pytest.mark.asyncio
async def test_compile_recipe_prompt_batch_preserves_order_and_bounds_concurrency(
    monkeypatch,
):
    active = 0
    max_active = 0

    async def fake_compile(**kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        if kwargs["recipe_id"] == "broken":
            raise recipe_runtime.RecipeRuntimeError("invalid recipe")
        return recipe_runtime.RecipeCompileResult(
            prompt=f"compiled:{kwargs['recipe_id']}",
            mode="model",
            recipe_ids=(kwargs["recipe_id"],),
        )

    monkeypatch.setattr(recipe_runtime, "compile_recipe_prompt_result", fake_compile)
    outcomes = await recipe_runtime.compile_recipe_prompt_batch(
        [
            {"recipe_id": "first"},
            {"recipe_id": "broken"},
            {"recipe_id": "third"},
            {"recipe_id": "fourth"},
        ],
        concurrency=2,
    )

    assert max_active == 2
    assert isinstance(outcomes[0], recipe_runtime.RecipeCompileResult)
    assert outcomes[0].prompt == "compiled:first"
    assert isinstance(outcomes[1], recipe_runtime.RecipeRuntimeError)
    assert isinstance(outcomes[2], recipe_runtime.RecipeCompileResult)
    assert outcomes[2].prompt == "compiled:third"
    assert isinstance(outcomes[3], recipe_runtime.RecipeCompileResult)


def test_recipe_pipeline_rejects_explicit_conflicts(monkeypatch):
    recipes = {
        "base": {
            "id": "base",
            "output_kind": "image",
            "conflicts_with": [],
        },
        "overlay": {
            "id": "overlay",
            "output_kind": "image",
            "conflicts_with": ["base"],
        },
    }
    monkeypatch.setattr(
        recipe_runtime,
        "get_recipe_for_runtime",
        lambda recipe_id, **_kwargs: recipes[recipe_id],
    )

    with pytest.raises(recipe_runtime.RecipeRuntimeError, match="conflicts with"):
        recipe_runtime.get_recipe_pipeline_for_runtime(
            username="local",
            primary_recipe=recipes["base"],
            recipe_pipeline=["overlay"],
            node_kind="image",
        )


@pytest.mark.asyncio
async def test_generate_recipe_text_rejects_display_copy_that_drops_product_subject(monkeypatch):
    monkeypatch.setattr(
        recipe_runtime,
        "get_recipe_for_runtime",
        lambda **_kwargs: {
            "id": "general-text",
            "version": "1",
            "output_kind": "text",
            "system_prompt": "生成展示文案",
            "_catalog_source": "builtin",
        },
    )

    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self, _task):
            return SimpleNamespace(output="一抹炽热红韵，点亮每个清晨。\n温润质感，暖意蔓延。")

    monkeypatch.setattr(recipe_runtime, "Agent", FakeAgent)
    monkeypatch.setattr(
        "novelvideo.config.get_newapi_text_pydantic_model",
        lambda *_args, **_kwargs: object(),
    )

    with pytest.raises(recipe_runtime.RecipeRuntimeError, match="product subject"):
        await recipe_runtime.generate_recipe_text(
            username="local",
            recipe_id="general-text",
            node_kind="text",
            node_prompt="为同一只亮红色陶瓷杯写两句展示文案",
            user_goal="两句红杯展示文案，不要标题",
        )


@pytest.mark.asyncio
async def test_generate_recipe_text_executes_compiled_instruction(monkeypatch):
    monkeypatch.delenv("FREEZONE_RECIPE_TEXT_TIMEOUT_SECONDS", raising=False)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        recipe_runtime,
        "get_recipe_for_runtime",
        lambda **_kwargs: {
            "id": "ecommerce-text-plan",
            "version": "1.0.0",
            "output_kind": "text",
            "system_prompt": "生成三屏详情页方案",
            "_catalog_source": "builtin",
        },
    )

    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self, task):
            captured["task"] = task
            assert "生成三屏详情页方案" in task
            assert "Produce the final text deliverable now" in task
            return SimpleNamespace(output="# 详情页方案")

    monkeypatch.setattr(recipe_runtime, "Agent", FakeAgent)
    monkeypatch.setattr(
        "novelvideo.config.get_newapi_text_pydantic_model",
        lambda *args, **kwargs: captured.update(model_env=args[0], **kwargs)
        or object(),
    )

    result = await recipe_runtime.generate_recipe_text(
        username="local",
        recipe_id="ecommerce-text-plan",
        node_kind="text",
        node_prompt="咖啡机",
    )

    assert result == "# 详情页方案"
    assert captured["model_env"] == "FREEZONE_TEXT_WRITER_MODEL"
    assert captured["timeout_seconds_override"] == 540.0
    assert captured["capability"] == "freezone.text.generate"
    assert captured["brainclaw_profile"] is (
        BrainClawProfile.FREEZONE_RECIPE_TEXT_GENERATION
    )
    assert captured["brainclaw_profile_variant"] == BrainClawProfileVariant(
        "recipe/ecommerce-text-plan@1.0.0"
    )
    assert "Structured output contract" not in str(captured["task"])
    assert "JSON Schema" not in str(captured["task"])


def test_v2_executor_contract_serializes_exact_schema_without_changing_v1_prompt():
    base = "Compiled instruction\n\nExecution requirement:\nProduce the final text deliverable now."
    schema = {
        "type": "object",
        "required": ["media_requirements"],
        "properties": {
            "media_requirements": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["event_id", "required", "purpose", "suggested_query"],
                },
            }
        },
    }

    assert recipe_runtime.append_structured_output_contract(
        base, {"schema_version": "dramaclaw.recipe.v1"}
    ) == base
    v2 = recipe_runtime.append_structured_output_contract(
        base,
        {"schema_version": "dramaclaw.recipe.v2", "output_schema": schema},
    )
    assert "Return only one JSON value" in v2
    assert json.dumps(schema, ensure_ascii=False, sort_keys=True) in v2
    for field in ("media_requirements", "event_id", "required", "purpose", "suggested_query"):
        assert field in v2


@pytest.mark.asyncio
async def test_generate_recipe_text_sends_v2_output_schema_to_executor(monkeypatch):
    captured: dict[str, object] = {}
    schema = {
        "type": "object",
        "required": ["media_requirements"],
        "properties": {"media_requirements": {"type": "array", "items": {
            "type": "object",
            "required": ["event_id", "required", "purpose", "suggested_query"],
        }}},
    }
    monkeypatch.setattr(
        recipe_runtime,
        "get_recipe_for_runtime",
        lambda **_kwargs: {
            "schema_version": "dramaclaw.recipe.v2",
            "id": "reference-broll-plan",
            "version": "1.1.0",
            "output_kind": "text",
            "system_prompt": "为每个事件规划 B-roll",
            "output_schema": schema,
            "_catalog_source": "builtin",
        },
    )

    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            captured["system_prompt"] = _kwargs.get("system_prompt")

        async def run(self, task):
            captured["task"] = task
            return SimpleNamespace(output='{"media_requirements":[]}')

    monkeypatch.setattr(recipe_runtime, "Agent", FakeAgent)
    monkeypatch.setattr(
        "novelvideo.config.get_newapi_text_pydantic_model",
        lambda *_args, **_kwargs: object(),
    )

    await recipe_runtime.generate_recipe_text(
        username="local",
        recipe_id="reference-broll-plan",
        node_kind="text",
        node_prompt="判断每个事件是否需要 B-roll",
    )

    assert json.dumps(schema, ensure_ascii=False, sort_keys=True) in str(captured["task"])
    assert "Return only one JSON value" in str(captured["task"])
    for field in ("event_id", "required", "purpose", "suggested_query"):
        assert field in str(captured["task"])


@pytest.mark.asyncio
async def test_internal_plan_repair_uses_patch_contract_without_full_plan_instruction(monkeypatch):
    import time
    from novelvideo.freezone.remix.production_plan_execution import PlanExecutionPolicy

    captured = {}
    monkeypatch.setattr(recipe_runtime, "get_recipe_for_runtime", lambda **_kwargs: {
        "schema_version": "dramaclaw.recipe.v2", "id": "reference-production-plan",
        "output_kind": "text", "system_prompt": "输出完整作品方案 JSON",
        "output_schema": {"type": "object", "required": ["beats"]},
    })

    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self, task, *, model_settings):
            captured["task"] = task
            return SimpleNamespace(output='{"base_digest":"abc","changes":[]}',
                                   usage=lambda: SimpleNamespace(output_tokens=10))

    monkeypatch.setattr(recipe_runtime, "Agent", FakeAgent)
    monkeypatch.setattr("novelvideo.config.get_newapi_text_pydantic_model",
                        lambda *_args, **_kwargs: object())
    patch_schema = {"type": "object", "required": ["base_digest", "changes"]}
    await recipe_runtime.generate_recipe_text(
        username="local", recipe_id="reference-production-plan", node_kind="text",
        node_prompt="修复字段", structured_upstream={"repair_scope": {}},
        structured_output_contract=patch_schema,
        plan_policy=PlanExecutionPolicy(10, 100, False),
        plan_deadline=time.monotonic() + 5,
    )
    assert "输出完整作品方案 JSON" not in captured["task"]
    assert json.dumps(patch_schema, ensure_ascii=False, sort_keys=True) in captured["task"]


@pytest.mark.asyncio
async def test_plan_stages_use_distinct_small_contracts_on_same_model_route(monkeypatch):
    import time
    from pydantic import TypeAdapter
    from novelvideo.freezone.remix.production_plan_execution import PlanExecutionPolicy

    captured = []
    output_schemas = []
    monkeypatch.setattr(recipe_runtime, "get_recipe_for_runtime", lambda **_kwargs: {
        "id": "reference-production-plan", "schema_version": "dramaclaw.recipe.v2", "output_kind": "text",
        "system_prompt": "保持人物和画面连续", "output_schema": {"type": "object"},
    })

    class FakeAgent:
        def __init__(self, model, **kwargs):
            assert kwargs["retries"] == 0
            output_schemas.append(TypeAdapter(kwargs["output_type"].outputs).json_schema())

        async def run(self, task, *, model_settings):
            captured.append((task, model_settings))
            return SimpleNamespace(output='{}', usage=lambda: SimpleNamespace(output_tokens=2))

    monkeypatch.setattr(recipe_runtime, "Agent", FakeAgent)
    model_requests = []

    def model_factory(setting, *args, **kwargs):
        model_requests.append((setting, kwargs))
        return SimpleNamespace(model_name="writer")

    monkeypatch.setattr("novelvideo.config.get_newapi_text_pydantic_model",
                        model_factory)
    for stage in ("outline", "arrangement"):
        await recipe_runtime.generate_recipe_text(
            username="local", recipe_id="reference-production-plan", node_kind="text",
            node_prompt="当前目标", production_plan_stage=stage,
            plan_policy=PlanExecutionPolicy(10, 200, False),
            plan_deadline=time.monotonic() + 5,
        )
    assert "creative_direction" in output_schemas[0]["properties"]
    assert "speech_bindings" not in output_schemas[0]["properties"]
    assert "speech_bindings" in output_schemas[1]["properties"]
    assert "creative_direction" not in output_schemas[1]["properties"]
    assert all("JSON Schema:\n" not in task for task, _ in captured)
    assert all("保持人物和画面连续" in task for task, _ in captured)
    assert [settings["max_tokens"] for _, settings in captured] == [200, 200]
    assert [setting for setting, _ in model_requests] == ["FREEZONE_TEXT_WRITER_MODEL"] * 2
    assert all(kwargs["brainclaw_profile"] == BrainClawProfile.FREEZONE_RECIPE_TEXT_GENERATION
               for _, kwargs in model_requests)


def test_plan_stage_native_schema_is_strict_compatible_and_closes_known_turns():
    from novelvideo.freezone.remix.production_plan_draft_stages import draft_stage_native_schema

    unsupported = {"allOf", "if", "then", "else", "oneOf", "not", "patternProperties"}

    def check(fragment):
        if isinstance(fragment, list):
            for item in fragment:
                check(item)
        elif isinstance(fragment, dict):
            assert not unsupported.intersection(fragment)
            if fragment.get("type") == "object":
                assert fragment["additionalProperties"] is False
                assert set(fragment["required"]) == set(fragment["properties"])
            for value in fragment.values():
                check(value)

    outline = draft_stage_native_schema("outline")
    arrangement = draft_stage_native_schema("arrangement", turn_ids=["turn-1", "turn-2"])
    check(outline)
    check(arrangement)
    bindings = arrangement["properties"]["speech_bindings"]
    assert set(bindings["properties"]) == {"turn-1", "turn-2"}
    assert bindings["properties"]["turn-1"]["type"] == ["string", "null"]


@pytest.mark.asyncio
async def test_plan_stage_uses_native_strict_output_and_returns_stage_json(monkeypatch):
    import time
    from pydantic_ai import NativeOutput
    from novelvideo.freezone.remix.production_plan_execution import PlanExecutionPolicy

    captured = []
    monkeypatch.setattr(recipe_runtime, "get_recipe_for_runtime", lambda **_kwargs: {
        "id": "reference-production-plan", "output_kind": "text", "system_prompt": "规划作品",
        "output_schema": {"type": "object"},
    })

    class FakeAgent:
        def __init__(self, _model, **kwargs):
            captured.append(kwargs)

        async def run(self, task, *, model_settings):
            return SimpleNamespace(output={
                "creative_direction": "保留结构", "output": {
                    "width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1,
                    "background": None,
                }, "beats": [],
            }, usage=lambda: SimpleNamespace(output_tokens=10))

    monkeypatch.setattr(recipe_runtime, "Agent", FakeAgent)
    monkeypatch.setattr("novelvideo.config.get_newapi_text_pydantic_model",
                        lambda *args, **kwargs: SimpleNamespace(model_name="writer"))
    content = await recipe_runtime.generate_recipe_text(
        username="local", recipe_id="reference-production-plan", node_kind="text",
        node_prompt="规划", production_plan_stage="outline",
        plan_policy=PlanExecutionPolicy(10, 200, False),
        plan_deadline=time.monotonic() + 5,
    )
    assert isinstance(captured[0]["output_type"], NativeOutput)
    assert captured[0]["output_type"].strict is True
    assert json.loads(content)["output"] == {
        "width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1,
    }


@pytest.mark.asyncio
async def test_builtin_plan_stage_tasks_carry_one_stage_schema_and_confirmed_context(monkeypatch):
    import time
    from pydantic import TypeAdapter
    from novelvideo.freezone.remix.production_plan_execution import PlanExecutionPolicy

    recipe_path = Path(__file__).parents[1] / (
        "src/novelvideo/freezone/agent_catalog/builtins/recipes/reference-production-plan.json")
    builtin_recipe = json.loads(recipe_path.read_text())
    monkeypatch.setattr(recipe_runtime, "get_recipe_for_runtime", lambda **_kwargs: builtin_recipe)
    tasks = []
    schemas = []

    class FakeAgent:
        def __init__(self, _model, **kwargs):
            assert kwargs["retries"] == 0
            schemas.append(TypeAdapter(kwargs["output_type"].outputs).json_schema())

        async def run(self, task, *, model_settings):
            tasks.append(task)
            return SimpleNamespace(output="{}", usage=lambda: SimpleNamespace(output_tokens=2))

    monkeypatch.setattr(recipe_runtime, "Agent", FakeAgent)
    monkeypatch.setattr("novelvideo.config.get_newapi_text_pydantic_model",
                        lambda *args, **kwargs: SimpleNamespace(model_name="writer"))
    context = {"requirements": {"dialogue": {"mode": "use_confirmed_text"}},
               "locked_turns": [{"speaker": "甲", "text": "黑咖啡。"}],
               "draft_outline": {"beats": [{"id": "beat-1", "turns": [
                   {"id": "turn-1", "speaker": "甲", "text": "黑咖啡。"}]}]}}
    for stage in ("outline", "arrangement"):
        await recipe_runtime.generate_recipe_text(
            username="alice", recipe_id="reference-production-plan", node_kind="text",
            node_prompt="已确认方向", structured_upstream=context,
            upstream_text=json.dumps(context, ensure_ascii=False),
            production_plan_stage=stage,
            plan_policy=PlanExecutionPolicy(10, 200, False),
            plan_deadline=time.monotonic() + 5,
        )
    assert set(schemas[0]["properties"]) == {"creative_direction", "output", "beats"}
    assert set(schemas[1]["properties"]) == {"selections", "placements", "speech_bindings"}
    assert set(schemas[1]["properties"]["speech_bindings"]["properties"]) == {"turn-1"}
    assert all("黑咖啡。" in task and "use_confirmed_text" in task for task in tasks)


@pytest.mark.asyncio
async def test_generate_recipe_text_omits_variant_for_user_recipe(monkeypatch):
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        recipe_runtime,
        "get_recipe_for_runtime",
        lambda **_kwargs: {
            "id": "private-recipe",
            "version": "1.0.0",
            "output_kind": "text",
            "system_prompt": "生成正文",
            "_catalog_source": "user",
        },
    )

    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self, _task):
            return SimpleNamespace(output="正文")

    monkeypatch.setattr(recipe_runtime, "Agent", FakeAgent)
    monkeypatch.setattr(
        "novelvideo.config.get_newapi_text_pydantic_model",
        lambda *_args, **kwargs: captured.update(kwargs) or object(),
    )

    assert (
        await recipe_runtime.generate_recipe_text(
            username="local",
            recipe_id="private-recipe",
            node_kind="text",
            node_prompt="写作",
        )
        == "正文"
    )
    assert captured["brainclaw_profile_variant"] is None
