from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from novelvideo.freezone import recipe_runtime
from novelvideo.freezone.remix import production_plan


CASES = json.loads((Path(__file__).parent / "fixtures/production_plan_generation_cases.json").read_text())


class CapturedRequest(Exception):
    pass


def test_builtin_plan_recipe_teaches_estimated_turn_and_beat_anchor_convention():
    recipe = recipe_runtime.get_recipe_for_runtime(username="alice",
                                                   recipe_id="reference-production-plan")
    guidance = recipe["system_prompt"]
    assert "start_frame/end_frame" in guidance
    assert "offset_frames" in guidance and "段首" in guidance
    assert "subtitle" in guidance and "dialogue" in guidance


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda item: item["name"])
async def test_real_plan_request_assembly_has_complete_structured_context(case, monkeypatch):
    events = []
    observations = []
    for index in range(case["event_count"]):
        start = index * 1_000_000
        detail = f"第{index}段的独特画面" + ("细节" * 200 if case.get("long_detail") else "")
        events.append({"event_id": f"event-{index}", "start_us": start,
                       "end_us": start + 1_000_000, "visual": detail,
                       "spoken_range": {"text": f"第{index}句"}})
        observations.append({"shot": index + 1, "start_time": float(index),
                             "end_time": float(index + 1), "duration": 1.0,
                             "visual": detail, "lighting": f"第{index}段的光线",
                             "image_prompt": f"Suggested shot {index}"})
    blueprint = {"summary": "由问题进入解决，再以结果收束", "events": events,
                 "observations": observations}
    snapshot = copy.deepcopy(blueprint)
    captured = {}

    async def capture(**kwargs):
        captured.update(kwargs)
        raise CapturedRequest()

    monkeypatch.setattr(production_plan, "generate_recipe_text", capture)
    args = {"topic": case["topic"], "blueprint": blueprint, "username": "alice",
            "requirements": case["requirements"], "locked_turns": case["locked_turns"]}
    if case["name"] == "oversize_rejected":
        with pytest.raises(ValueError, match="production_plan_context_budget_exceeded"):
            await production_plan.generate_production_plan_draft(**args)
        assert not captured
        return
    with pytest.raises(CapturedRequest):
        await production_plan.generate_production_plan_draft(**args)
    assert blueprint == snapshot
    context = json.loads(captured["upstream_text"])
    assert context == captured["structured_upstream"]
    assert "observations" not in context.get("source_evidence", {})
    if case["event_count"]:
        assert "source_evidence" in context
        assert len(context["source_evidence"]["events"]) == case["event_count"]
        assert context["source_evidence"]["events"][-1]["lighting"] == f"第{case['event_count']-1}段的光线"
        assert "unmatched_observations" not in context["source_evidence"]
    recipe = recipe_runtime.get_recipe_for_runtime(username="alice",
                                                  recipe_id="reference-production-plan")
    task = recipe_runtime.build_recipe_compiler_task(
        recipe=recipe, node_kind="text", node_prompt=captured["node_prompt"],
        user_goal=captured["user_goal"], upstream_text=captured["upstream_text"],
        structured_upstream=captured["structured_upstream"],
    )
    task = recipe_runtime.append_structured_output_contract(task, recipe)
    assert len(task) <= 24_000


@pytest.mark.asyncio
async def test_reference_event_count_does_not_fill_primary_shots(monkeypatch):
    captured = {}

    async def capture(**kwargs):
        captured.update(kwargs)
        raise CapturedRequest()

    monkeypatch.setattr(production_plan, "generate_recipe_text", capture)

    blueprint = {
        "summary": "原片包含多个分析窗口",
        "events": [
            {"event_id": f"event-{index}", "start_us": index * 1_000_000,
             "end_us": (index + 1) * 1_000_000}
            for index in range(8)
        ],
    }

    with pytest.raises(CapturedRequest):
        await production_plan.generate_production_plan_draft(
            topic="重新安排复刻节奏",
            blueprint=blueprint,
            username="alice",
            requirements={},
        )

    context = json.loads(captured["upstream_text"])
    assert len(context["source_evidence"]["events"]) == 8
    assert "primary_shots" not in context.get("requirements", {})
