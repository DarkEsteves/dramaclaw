"""One drafting task may re-ask the model, but only under a hard bound."""

from __future__ import annotations

import json

import pytest

from test_production_plan import native_plan


def _outline() -> dict:
    return {
        "creative_direction": "一段带声对话",
        "output": {"width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1},
        "beats": [{"purpose": "问答", "duration_frames": 120,
                   "turns": [{"speaker": "甲", "text": "咖啡？"}]}],
    }


def _arrangement() -> dict:
    return {
        "selections": [],
        "placements": [{
            "id": "performance", "kind": "visual", "role": "primary", "track_id": "visual:main",
            "source_audio": True, "description": "连续问答",
            "start": {"kind": "beat", "ref_id": "beat-1", "edge": "start"},
            "end": {"kind": "beat", "ref_id": "beat-1", "edge": "end"},
        }],
        "speech_bindings": {"turn-1": "performance"},
    }


def test_call_bound_defaults_to_three_and_clamps_or_reverts(monkeypatch):
    from novelvideo.freezone.remix.production_plan_execution import plan_attempt_policy

    monkeypatch.delenv("FREEZONE_PRODUCTION_PLAN_MAX_CALLS", raising=False)
    assert plan_attempt_policy().max_calls == 3
    monkeypatch.setenv("FREEZONE_PRODUCTION_PLAN_MAX_CALLS", "1")
    assert plan_attempt_policy().max_calls == 1
    monkeypatch.setenv("FREEZONE_PRODUCTION_PLAN_MAX_CALLS", "99")
    assert plan_attempt_policy().max_calls == 4
    monkeypatch.setenv("FREEZONE_PRODUCTION_PLAN_MAX_CALLS", "zero")
    with pytest.raises(ValueError, match="invalid_production_plan_max_calls"):
        plan_attempt_policy()


def test_failure_classification_table():
    from novelvideo.freezone.remix.production_plan_execution import (
        PlanModelResponseInfo, PlanOutputLimitError, classify_plan_failure,
    )
    from novelvideo.freezone.remix.production_plan import _ProductionRequirementValidationError

    truncated = PlanOutputLimitError(PlanModelResponseInfo(
        finish_reason="length", output_tokens=None, total_output_tokens=None,
        reasoning_tokens=None, output_characters=0))
    assert classify_plan_failure(truncated) == {
        "action": "retry", "strategy": "stage_split", "code": "model_output_limit"}

    for code in ("json_parse_failed", "production_plan_draft_must_be_object"):
        assert classify_plan_failure(ValueError(code))["action"] == "retry"

    requirement = _ProductionRequirementValidationError(
        [{"code": "story_structure_invalid", "path": "/"}])
    wrapped = ValueError("plan_validation_failed")
    wrapped.__cause__ = requirement
    assert classify_plan_failure(wrapped) == {
        "action": "retry", "strategy": "same", "code": "plan_validation_failed"}

    assert classify_plan_failure(TimeoutError("production_plan_deadline_exceeded"))["action"] == "stop"
    assert classify_plan_failure(RuntimeError("provider exploded"))["action"] == "stop"
    assert classify_plan_failure(ValueError("production_plan_context_budget_exceeded"))["action"] == "stop"


@pytest.mark.asyncio
async def test_bad_json_is_retried_once_and_then_published(monkeypatch):
    from novelvideo.freezone.remix import production_plan

    calls = []

    async def generate(**kwargs):
        calls.append(kwargs.get("plan_deadline"))
        return "{truncated" if len(calls) == 1 else json.dumps(native_plan(), ensure_ascii=False)

    monkeypatch.setattr(production_plan, "generate_recipe_text", generate)
    draft = await production_plan.generate_production_plan_draft(
        topic="把原片改成一则咖啡短片", blueprint=None, username="alice")
    assert draft["authoring_stage"] == "arranged"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_repeated_identical_candidate_stops_before_a_third_call(monkeypatch):
    from novelvideo.freezone.remix import production_plan

    bad = native_plan()
    bad["output"].pop("width")
    calls = []

    async def generate(**kwargs):
        calls.append(kwargs)
        return json.dumps(bad, ensure_ascii=False)

    monkeypatch.setattr(production_plan, "generate_recipe_text", generate)
    with pytest.raises(ValueError, match="plan_validation_failed"):
        await production_plan.generate_production_plan_draft(
            topic="把原片改成一则咖啡短片", blueprint=None, username="alice")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_truncated_draft_falls_back_to_the_two_stage_path(monkeypatch):
    from novelvideo.freezone.remix import production_plan
    from novelvideo.freezone.remix.production_plan_execution import (
        PlanModelResponseInfo, PlanOutputLimitError,
    )

    stages: list[str] = []
    tokens: list[int] = []
    events: list[tuple[str, str]] = []

    async def generate(**kwargs):
        stages.append(kwargs.get("production_plan_stage") or "full")
        tokens.append(kwargs["plan_policy"].max_output_tokens)
        if kwargs.get("production_plan_stage") is None:
            raise PlanOutputLimitError(PlanModelResponseInfo(
                finish_reason="length", output_tokens=None, total_output_tokens=None,
                reasoning_tokens=None, output_characters=0))
        return json.dumps(_outline() if kwargs["production_plan_stage"] == "outline"
                          else _arrangement(), ensure_ascii=False)

    async def on_stage(event):
        events.append((event.stage, event.status))

    monkeypatch.setattr(production_plan, "generate_recipe_text", generate)
    draft = await production_plan.generate_production_plan_draft(
        topic="一段带声对话", blueprint=None, username="alice", on_stage=on_stage)
    assert stages == ["full", "outline", "arrangement"]
    # The split never widens the per-draft ceiling: 1:4 of one 16384 budget.
    assert sum(tokens[1:]) == tokens[0]
    assert draft["authoring_stage"] == "arranged"
    assert [row for row in events if row[0] == "full"][-1] == ("full", "completed")


@pytest.mark.asyncio
async def test_max_calls_one_keeps_todays_single_call_behaviour(monkeypatch):
    from novelvideo.freezone.remix import production_plan

    monkeypatch.setenv("FREEZONE_PRODUCTION_PLAN_MAX_CALLS", "1")
    calls = []

    async def generate(**kwargs):
        calls.append(kwargs)
        return "{truncated"

    monkeypatch.setattr(production_plan, "generate_recipe_text", generate)
    with pytest.raises(ValueError, match="json_parse_failed"):
        await production_plan.generate_production_plan_draft(
            topic="把原片改成一则咖啡短片", blueprint=None, username="alice")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_no_retry_starts_when_the_task_budget_is_almost_gone(monkeypatch):
    from novelvideo.freezone.remix import production_plan

    calls = []
    clock = [1000.0]

    async def generate(**kwargs):
        calls.append(kwargs)
        # Leaves less than the 60s a second call could actually use.
        clock[0] += 500.0
        return "{truncated"

    monkeypatch.setattr(production_plan, "generate_recipe_text", generate)
    monkeypatch.setattr(production_plan.time, "monotonic", lambda: clock[0])
    with pytest.raises(ValueError, match="json_parse_failed"):
        await production_plan.generate_production_plan_draft(
            topic="把原片改成一则咖啡短片", blueprint=None, username="alice")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_failure_summary_and_notice_report_the_bounded_retries():
    from novelvideo.freezone.remix.production_plan_failures import (
        build_plan_failure_summary, format_plan_failure_notice,
    )

    record = {"event": "production_plan.validation_failed", "error_code": "plan_validation_failed",
              "attempt": 2, "validation_issue_count": 1,
              "validation_issues": [{"code": "required", "path": "/output"}]}
    summary = build_plan_failure_summary(record, task_id="task-1")
    assert summary["attempt_count"] == 2
    notice = format_plan_failure_notice(summary)
    assert "已自动重试 1 次" in notice and notice.endswith("本次方案尚未保存。")
    record["attempt"] = 1
    assert "attempt_count" not in build_plan_failure_summary(record, task_id="task-1")
