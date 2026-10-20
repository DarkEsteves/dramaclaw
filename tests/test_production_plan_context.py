from __future__ import annotations

import copy
import json

import pytest

from novelvideo.freezone.remix.production_plan_context import (
    build_production_plan_context,
    serialize_plan_context,
)


def test_context_merges_only_matching_observations_and_keeps_constraints():
    blueprint = {
        "summary": "原片摘要",
        "preserve_replace_rules": [{"type": "replace", "text": "改成另一种产品",
                                    "signed_url": "https://private.example/rule"}],
        "events": [
            {"event_id": "a", "start_us": 0, "end_us": 1_000_000,
             "visual": "近景", "spoken_range": {"text": "真实台词"},
             "screen_subtitles": ["屏幕字幕"], "thumbnail_url": "https://private.example/a"},
            {"event_id": "b", "start_us": 1_000_000, "end_us": 2_000_000, "visual": "中景"},
            {"event_id": "c", "start_us": 2_000_000, "end_us": 3_000_000, "visual": "远景"},
        ],
        "observations": [
            {"event_id": "b", "start_us": 1_000_000, "end_us": 2_000_000,
             "visual": "中景", "lighting": "侧光", "motion": "缓慢推近",
             "video_url": "https://private.example/b"},
            {"event_id": "unknown", "start_us": 4_000_000, "end_us": 5_000_000,
             "sound": "脚步声"},
        ],
    }
    snapshot = copy.deepcopy(blueprint)
    context = build_production_plan_context(
        blueprint=blueprint, source_timing={"duration_us": 5_000_000},
        requirements={"dialogue": {"mode": "preserve_source"}},
        locked_turns=[{"speaker": "甲", "text": "真实台词"}],
        output_preferences={"source_clip_duration_seconds": 4},
    )
    assert blueprint == snapshot
    assert context["source_evidence"]["events"][1]["lighting"] == "侧光"
    assert context["source_evidence"]["events"][1]["motion"] == "缓慢推近"
    assert [e["event_id"] for e in context["source_evidence"]["events"]] == ["a", "b", "c"]
    assert context["source_evidence"]["unmatched_observations"][0]["sound"] == "脚步声"
    assert context["source_evidence"]["events"][0]["spoken_range"]["text"] == "真实台词"
    assert context["source_evidence"]["events"][0]["screen_subtitles"] == ["屏幕字幕"]
    assert context["locked_turns"][0]["text"] == "真实台词"
    assert context["source_timing"]["duration_us"] == 5_000_000
    assert "private.example" not in json.dumps(context)
    assert "observations" not in context["source_evidence"]


def test_real_analysis_shots_join_events_by_unique_second_ranges():
    blueprint = {"events": [
        {"event_id": "event-1", "start_us": 0, "end_us": 4_000_000,
         "model": {"visual_observation": "人物与杯子"}},
        {"event_id": "event-2", "start_us": 4_000_000, "end_us": 8_000_000,
         "model": {"visual_observation": "杯子特写"}},
    ], "observations": [
        {"shot": 1, "start_time": 0.0, "end_time": 4.0, "duration": 4.0,
         "visible_subject": "人物", "lighting": "自然光", "image_prompt": "a person"},
        {"shot": 2, "start_time": 4.0, "end_time": 8.0, "duration": 4.0,
         "visible_subject": "手", "lighting": "侧光"},
    ]}
    context = build_production_plan_context(
        blueprint=blueprint, source_timing=None,
        requirements={"dialogue": {"mode": "rewrite"}},
        locked_turns=None, output_preferences=None,
    )
    evidence = context["source_evidence"]
    assert len(evidence["events"]) == 2
    assert "unmatched_observations" not in evidence
    assert evidence["events"][0]["visible_subject"] == "人物"
    assert evidence["events"][1]["lighting"] == "侧光"
    assert "start_time" not in evidence["events"][0]
    assert "image_prompt" not in evidence["events"][0]


def test_multiple_conflicting_observations_keep_each_source_note():
    blueprint = {
        "events": [{"event_id": "event-1", "start_us": 0, "end_us": 1_000_000,
                    "lighting": "原始光线"}],
        "observations": [
            {"start_time": 0.0, "end_time": 1.0, "lighting": "侧光"},
            {"start_time": 0.0, "end_time": 1.0, "lighting": "逆光"},
        ],
    }
    context = build_production_plan_context(
        blueprint=blueprint, source_timing=None,
        requirements={"dialogue": {"mode": "rewrite"}}, locked_turns=None,
        output_preferences=None,
    )
    assert context["source_evidence"]["events"][0]["observation_detail"]["lighting"] == [
        "侧光", "逆光"]


def test_structured_context_is_never_spliced():
    context = {"locked_turns": [{"speaker": "甲", "text": "字🙂" * 9000}]}
    with pytest.raises(ValueError, match=r"production_plan_context_budget_exceeded: stage=context actual_chars=\d+ limit_chars=16000"):
        serialize_plan_context(context, max_chars=16000)
    encoded = serialize_plan_context(context, max_chars=100000)
    assert json.loads(encoded) == context
    assert "context truncated" not in encoded


@pytest.mark.parametrize("requirements", [None, {}, {"dialogue": {"mode": "rewrite"}}])
def test_reference_evidence_does_not_depend_on_constraints(requirements):
    context = build_production_plan_context(
        blueprint={"summary": "保留结尾", "events": [{"event_id": "ending",
            "start_us": 15_000_000, "end_us": 18_000_000,
            "spoken_range": {"text": "看看你的手臂。"},
            "model": {"visual_observation": "受访者看向对方手臂",
                      "narrative_inference": "用手臂反应完成反转"}}]},
        source_timing=None, requirements=requirements, locked_turns=None,
        output_preferences=None,
    )
    assert context["source_evidence"]["events"][0]["spoken_range"]["text"] == "看看你的手臂。"
    assert context["source_evidence"]["events"][0]["model"]["visual_observation"] == "受访者看向对方手臂"


def test_no_source_blueprint_keeps_free_creation_context_empty():
    assert build_production_plan_context(
        blueprint=None, source_timing=None, requirements=None,
        locked_turns=None, output_preferences=None) == {}


def test_recipe_builder_keeps_structured_context_parseable(monkeypatch):
    from novelvideo.freezone import recipe_runtime

    monkeypatch.setenv("FREEZONE_PRODUCTION_PLAN_CONTEXT_CHARS", "40000")
    context = {"locked_turns": [{"speaker": "甲", "text": "字🙂" * 9000}]}
    task = recipe_runtime.build_recipe_compiler_task(
        recipe={"output_kind": "text", "system_prompt": "起草作品方案"},
        node_kind="text", node_prompt="起草", user_goal="保持台词",
        upstream_text="ignored", structured_upstream=context,
    )
    encoded = task.split("Upstream text context:\n", 1)[1].split(
        "\n\nAvailable reference media metadata:", 1)[0]
    assert json.loads(encoded) == context


def test_structured_plan_goal_over_budget_is_rejected_before_truncation():
    from novelvideo.freezone import recipe_runtime

    with pytest.raises(ValueError, match="production_plan_goal_budget_exceeded"):
        recipe_runtime.build_recipe_compiler_task(
            recipe={"output_kind": "text", "system_prompt": "起草作品方案"},
            node_kind="text", node_prompt="起草", user_goal="重要要求" * 3000,
            structured_upstream={},
        )
