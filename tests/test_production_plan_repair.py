from __future__ import annotations

import copy

import pytest
from jsonschema import Draft202012Validator

from test_production_plan import native_plan
from novelvideo.freezone.remix.production_plan_repair import (
    apply_plan_repair, build_plan_repair_scope, plan_repair_json_schema,
)


def test_ai_draft_defaults_only_incomplete_valid_subtitle_positions():
    from novelvideo.freezone.remix.production_plan_v2 import normalize_draft_subtitle_positions

    draft = native_plan()
    subtitle = {"id": "s1", "kind": "subtitle", "role": "dialogue", "track_id": "subtitles",
                "text": "原句", "start": {"kind": "program_frame", "frame": 0},
                "end": {"kind": "program_frame", "frame": 10}, "subtitle_x": 20}
    draft["placements"] += [
        subtitle,
        {**subtitle, "id": "s2", "subtitle_x": 30, "subtitle_y": 40},
        {**subtitle, "id": "s3", "subtitle_x": -1},
    ]
    original = copy.deepcopy(draft)
    normalized, ids = normalize_draft_subtitle_positions(draft)
    assert ids == ["s1"]
    assert "subtitle_x" not in normalized["placements"][1]
    assert "subtitle_y" not in normalized["placements"][1]
    assert normalized["placements"][1]["text"] == "原句"
    assert normalized["placements"][2] == original["placements"][2]
    assert normalized["placements"][3] == original["placements"][3]
    assert draft == original


def test_repair_does_not_mutate_input_or_unrelated_beats():
    original = native_plan()
    original["beats"].append({"id": "second", "purpose": "反转", "duration_frames": 60,
                              "turns": [{"id": "line", "text": "已确认台词"}]})
    snapshot = copy.deepcopy(original)
    scope = build_plan_repair_scope(original, [
        {"path": "/placements/0/description", "code": "invalid_description"}])
    change = {"base_digest": scope["base_digest"], "changes": [
        {"path": "/placements/0/description", "value": "清晨的室内桌面"}]}
    Draft202012Validator(plan_repair_json_schema(scope)).validate(change)
    fixed = apply_plan_repair(original, change, scope=scope)
    assert original == snapshot
    assert fixed["beats"] == snapshot["beats"]
    assert fixed["placements"][0]["description"] == "清晨的室内桌面"


def test_repair_rejects_stale_digest_and_unapproved_paths():
    original = native_plan()
    scope = build_plan_repair_scope(original, [
        {"path": "/placements/0/description", "code": "invalid_description"}])
    with pytest.raises(ValueError, match="repair_digest_mismatch"):
        apply_plan_repair(original, {"base_digest": "old", "changes": [
            {"path": "/placements/0/description", "value": "新描述"}]}, scope=scope)
    with pytest.raises(ValueError, match="repair_path_not_allowed"):
        apply_plan_repair(original, {"base_digest": scope["base_digest"], "changes": [
            {"path": "/resources", "value": [{"id": "invented"}]}]}, scope=scope)


def test_requirement_issue_opens_only_related_collection():
    original = native_plan()
    scope = build_plan_repair_scope(original, [{"code": "primary_shot_count_mismatch",
                                               "entity_id": "plan"}])
    assert scope["allowed_paths"] == ["/placements"]
    assert "beats" not in scope["fragments"]


def test_shot_count_repair_preserves_existing_visual_content():
    original = native_plan()
    scope = build_plan_repair_scope(original, [{"code": "primary_shot_count_mismatch",
                                               "entity_id": "plan"}])
    changed = copy.deepcopy(original["placements"])
    changed[0]["description"] = "无关视觉被重写"
    response = {"base_digest": scope["base_digest"],
                "changes": [{"path": "/placements", "value": changed}]}
    with pytest.raises(ValueError, match="repair_unrelated_field_changed"):
        apply_plan_repair(original, response, scope=scope)


def test_shot_count_repair_preserves_existing_placement_order():
    original = native_plan()
    original["placements"].append({**copy.deepcopy(original["placements"][0]),
                                   "id": "second-picture", "description": "另一画面"})
    scope = build_plan_repair_scope(original, [{"code": "primary_shot_count_mismatch",
                                               "entity_id": "plan"}])
    reordered = list(reversed(copy.deepcopy(original["placements"])))
    with pytest.raises(ValueError, match="repair_unrelated_field_changed"):
        apply_plan_repair(original, {"base_digest": scope["base_digest"],
            "changes": [{"path": "/placements", "value": reordered}]}, scope=scope)


def test_duration_repair_preserves_beat_purpose_and_turns():
    original = native_plan()
    original["beats"][0]["turns"] = [{"id": "line", "text": "锁定台词"}]
    scope = build_plan_repair_scope(original, [{"code": "duration_mismatch",
                                               "entity_id": "plan"}])
    changed = copy.deepcopy(original["beats"])
    changed[0]["purpose"] = "另一故事"
    changed[0]["duration_frames"] = 120
    with pytest.raises(ValueError, match="repair_unrelated_field_changed"):
        apply_plan_repair(original, {"base_digest": scope["base_digest"],
            "changes": [{"path": "/beats", "value": changed}]}, scope=scope)


def test_locked_dialogue_repair_preserves_turn_identity_and_timing():
    original = native_plan()
    original["beats"][0]["turns"] = [
        {"id": "line", "speaker": "甲", "text": "旧词", "start_frame": 0,
         "end_frame": 30}]
    scope = build_plan_repair_scope(original, [{"code": "locked_dialogue_changed",
                                               "entity_id": "plan"}])
    changed = copy.deepcopy(original["beats"][0]["turns"])
    changed[0]["id"] = "different"
    changed[0]["text"] = "正确台词"
    with pytest.raises(ValueError, match="repair_unrelated_field_changed"):
        apply_plan_repair(original, {"base_digest": scope["base_digest"],
            "changes": [{"path": "/beats/0/turns", "value": changed}]}, scope=scope)


def test_output_requirement_repair_only_exposes_failed_field():
    original = native_plan()
    scope = build_plan_repair_scope(original, [{"code": "output_requirement_mismatch",
                                               "path": "/output/width", "entity_id": "plan"}])
    assert scope["allowed_paths"] == ["/output/width"]


def test_repair_can_fix_only_an_unconfirmed_planned_source():
    original = native_plan()
    original["beats"][0]["turns"] = [{"id": "line", "text": "你好", "speech_placement_id": "missing"}]
    scope = build_plan_repair_scope(original, [{"code": "speech_source_missing",
                                               "path": "/beats/0/turns/0/speech_placement_id"}])
    assert scope["allowed_paths"] == ["/beats/0/turns/0/speech_placement_id"]
    fixed = apply_plan_repair(original, {"base_digest": scope["base_digest"], "changes": [
        {"path": "/beats/0/turns/0/speech_placement_id", "value": "hook-picture"}]}, scope=scope)
    assert fixed["beats"][0]["turns"][0]["speech_placement_id"] == "hook-picture"


def test_repair_cannot_reselect_confirmed_speech_source():
    original = native_plan()
    original["beats"][0]["turns"] = [{"id": "line", "text": "你好", "speech_placement_id": "hook-picture",
                                           "timing_basis": "manual", "start_frame": 0, "end_frame": 20}]
    scope = build_plan_repair_scope(original, [{"code": "speech_source_conflict",
                                               "path": "/beats/0/turns/0/speech_placement_id"}])
    assert scope["allowed_paths"] == []


def test_speech_source_timing_repair_exposes_only_target_anchors():
    original = native_plan()
    original["beats"][0]["turns"] = [{"id": "line", "text": "你好",
                                      "speech_placement_id": "hook-picture"}]
    scope = build_plan_repair_scope(original, [{
        "code": "speech_source_timing_invalid",
        "path": "/placements/0",
        "placement_id": "hook-picture",
        "beat_id": "hook",
    }])

    assert scope["allowed_paths"] == ["/placements/0/end", "/placements/0/start"]
    assert scope["repair_context"] == {"beat_id": "hook", "placement_id": "hook-picture"}


def test_cross_beat_carrier_repair_rebinds_turns_not_the_carrier():
    from novelvideo.freezone.remix.contracts import validate_payload

    original = native_plan()
    original["beats"][0]["turns"] = [{"id": "line", "text": "你好",
                                      "speech_placement_id": "hook-picture"}]
    original["beats"].append({"id": "second", "purpose": "反转", "duration_frames": 60,
                              "turns": [{"id": "later", "text": "再见",
                                         "speech_placement_id": "hook-picture"}]})
    with pytest.raises(ValueError) as raised:
        validate_payload("production_plan.v2", original)
    from novelvideo.freezone.remix.production_plan_repair import plan_repair_issues

    issues = plan_repair_issues(raised.value.__cause__)
    assert issues[0]["code"] == "speech_source_spans_beats"

    scope = build_plan_repair_scope(original, issues)

    assert scope["allowed_paths"] == ["/beats/0/turns/0/speech_placement_id",
                                      "/beats/1/turns/0/speech_placement_id"]
    fixed = apply_plan_repair(original, {"base_digest": scope["base_digest"], "changes": [
        {"path": "/beats/1/turns/0/speech_placement_id", "value": None}]}, scope=scope)
    assert fixed["beats"][1]["turns"][0]["speech_placement_id"] is None
    validate_payload("production_plan.v2", fixed)


def test_carrier_repair_refuses_confirmed_speech_turns():
    original = native_plan()
    original["beats"][0]["turns"] = [{"id": "line", "text": "你好",
                                      "speech_placement_id": "hook-picture",
                                      "timing_basis": "manual", "start_frame": 0, "end_frame": 20}]
    scope = build_plan_repair_scope(original, [{
        "code": "speech_source_spans_beats", "path": "/placements/0",
        "placement_id": "hook-picture", "beat_id": "hook", "turn_id": "line"}])
    assert scope["allowed_paths"] == []


def test_noncontiguous_carrier_repair_opens_only_that_beats_turns():
    original = native_plan()
    original["beats"][0]["turns"] = [
        {"id": "a", "text": "一", "speech_placement_id": "hook-picture"},
        {"id": "b", "text": "二"},
        {"id": "c", "text": "三", "speech_placement_id": "hook-picture"},
    ]
    original["beats"].append({"id": "second", "purpose": "反转", "duration_frames": 60,
                              "turns": [{"id": "d", "text": "四"}]})
    scope = build_plan_repair_scope(original, [{
        "code": "speech_source_turns_not_contiguous", "path": "/placements/0",
        "placement_id": "hook-picture", "beat_id": "hook", "turn_id": "c"}])

    assert scope["allowed_paths"] == ["/beats/0/turns/0/speech_placement_id",
                                      "/beats/0/turns/2/speech_placement_id"]
    assert scope["repair_context"]["beat_id"] == "hook"
