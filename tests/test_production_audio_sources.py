"""Planned audio ownership is valid before any media has been created."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from novelvideo.freezone.remix.contracts import validate_payload
from novelvideo.freezone.remix.production_plan_v2 import production_plan_draft_json_schema


def planned_video_dialogue() -> dict:
    return {
        "schema_version": 2,
        "goal": "两人演示一个新工具",
        "output": {"width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1},
        "beats": [{"id": "demo", "purpose": "问答演示", "duration_frames": 180,
                   "turns": [
                       {"id": "ask", "speaker": "甲", "text": "这个怎么用？",
                        "speech_placement_id": "demo-video", "timing_basis": "estimated"},
                       {"id": "answer", "speaker": "乙", "text": "按下这里就行。",
                        "speech_placement_id": "demo-video", "timing_basis": "estimated"},
                   ]}],
        "resources": [],
        "placements": [{"id": "demo-video", "kind": "visual", "role": "primary",
                        "track_id": "picture", "description": "两人完成问答和操作演示",
                        "source_audio": True,
                        "start": {"kind": "beat", "ref_id": "demo", "edge": "start"},
                        "end": {"kind": "beat", "ref_id": "demo", "edge": "end"}}],
        "provenance": {},
    }


def test_unbound_video_owns_two_turns_in_one_authorable_plan() -> None:
    from novelvideo.freezone.remix.production_audio_sources import planned_speech_sources

    plan = planned_video_dialogue()
    Draft202012Validator(production_plan_draft_json_schema()).validate(plan)
    saved = validate_payload("production_plan.v2", plan)
    assert planned_speech_sources(saved) == {"demo-video": ["ask", "answer"]}
    assert len([row for row in saved["placements"] if row["kind"] == "audio"]) == 0


@pytest.mark.parametrize(("change", "error"), [
    (lambda plan: plan["beats"][0]["turns"][0].update(speech_placement_id="missing"), "missing"),
    (lambda plan: plan["placements"][0].update(kind="graphic", role="effect", source_audio=None, graphic={"template": "title_bar", "text": "问答"}), "visual or speech"),
    (lambda plan: plan["placements"][0].update(kind="audio", role="bgm", source_audio=None), "speech or narration"),
    (lambda plan: plan["placements"][0].update(source_audio=False), "muted"),
    (lambda plan: plan["placements"][0].update(loop=True), "nonloop"),
])
def test_invalid_speech_sources_fail_at_plan_validation(change, error: str) -> None:
    plan = planned_video_dialogue()
    change(plan)
    with pytest.raises(ValueError, match=error):
        validate_payload("production_plan.v2", plan)


def test_source_cannot_belong_to_another_beat() -> None:
    plan = planned_video_dialogue()
    plan["beats"].append({"id": "closing", "purpose": "收尾", "duration_frames": 60,
                          "turns": [{"id": "bye", "text": "再见", "speech_placement_id": "demo-video"}]})
    with pytest.raises(ValueError, match="beat"):
        validate_payload("production_plan.v2", plan)


def test_speech_source_must_cover_each_explicitly_timed_turn() -> None:
    plan = planned_video_dialogue()
    plan["beats"][0]["turns"][0].update(start_frame=100, end_frame=140)
    plan["placements"][0]["start"] = {"kind": "program_frame", "frame": 0}
    plan["placements"][0]["end"] = {"kind": "program_frame", "frame": 20}
    with pytest.raises(ValueError, match="turn ask.*source interval"):
        validate_payload("production_plan.v2", plan)


def test_speech_source_overlap_failure_exposes_repair_issue() -> None:
    from novelvideo.freezone.remix.production_audio_sources import validate_plan_audio_sources

    plan = planned_video_dialogue()
    plan["placements"][0]["start"] = {"kind": "program_frame", "frame": 200}
    plan["placements"][0]["end"] = {"kind": "program_frame", "frame": 220}

    with pytest.raises(ValueError) as raised:
        validate_plan_audio_sources(plan)

    assert raised.value.issue == {
        "code": "speech_source_timing_invalid",
        "path": "/placements/0",
        "placement_id": "demo-video",
        "beat_id": "demo",
    }


def test_speech_source_id_cannot_be_empty() -> None:
    from novelvideo.freezone.remix.production_audio_sources import planned_speech_sources

    plan = planned_video_dialogue()
    plan["beats"][0]["turns"][0]["speech_placement_id"] = ""
    assert planned_speech_sources(plan)[""] == ["ask"]
    with pytest.raises(ValueError, match="speech_placement_id"):
        validate_payload("production_plan.v2", plan)
    errors = list(Draft202012Validator(production_plan_draft_json_schema()).iter_errors(plan))
    assert errors


def test_noncontiguous_turns_cannot_share_one_speech_source() -> None:
    plan = planned_video_dialogue()
    plan["beats"][0]["turns"].insert(1, {"id": "interruption", "text": "等一下"})
    with pytest.raises(ValueError, match="contiguous"):
        validate_payload("production_plan.v2", plan)


def test_speech_source_cannot_anchor_to_its_own_turn() -> None:
    plan = planned_video_dialogue()
    plan["placements"][0]["start"] = {"kind": "turn", "ref_id": "ask", "edge": "start"}
    with pytest.raises(ValueError, match="circular"):
        validate_payload("production_plan.v2", plan)


@pytest.mark.parametrize(("edge", "indirect"), [
    ("start", False), ("end", False), ("start", "selection"),
    ("end", "placement"),
])
def test_speech_timing_cycle_reports_source_turn_and_anchor(edge: str, indirect: bool | str) -> None:
    from novelvideo.freezone.remix.production_audio_sources import validate_plan_audio_sources

    plan = planned_video_dialogue()
    anchor = {"kind": "turn", "ref_id": "ask", "edge": "start"}
    if indirect == "selection":
        plan["selections"] = [{"id": "cut", "kind": "interval", "description": "片段",
                               "start": anchor, "end": {"kind": "beat", "ref_id": "demo", "edge": "end"}}]
        anchor = {"kind": "selection", "ref_id": "cut", "edge": "start"}
    elif indirect == "placement":
        plan["placements"].append({"id": "cover", "kind": "visual", "role": "broll",
                                   "start": {"kind": "beat", "ref_id": "demo", "edge": "start"},
                                   "end": anchor})
        anchor = {"kind": "placement", "ref_id": "cover", "edge": "end"}
    plan["placements"][0][edge] = anchor

    with pytest.raises(ValueError) as raised:
        validate_plan_audio_sources(plan)
    assert raised.value.issue == {
        "code": "speech_source_timing_cycle", "path": f"/placements/0/{edge}",
        "placement_id": "demo-video", "turn_id": "ask", "beat_id": "demo",
    }


def test_explicitly_timed_turn_can_anchor_its_speech_source() -> None:
    plan = planned_video_dialogue()
    plan["beats"][0]["turns"][0].update(start_frame=0, end_frame=30)
    plan["placements"][0]["start"] = {"kind": "turn", "ref_id": "ask", "edge": "start"}
    validate_payload("production_plan.v2", plan)


def test_image_cannot_enable_source_audio_or_own_speech() -> None:
    plan = planned_video_dialogue()
    plan["resources"] = [{"id": "still", "node_id": "still-node", "media_kind": "image",
                          "media_path": "assets/still.png", "media_hash": "a" * 64,
                          "duration_us": 0}]
    plan["placements"][0]["resource_id"] = "still"
    with pytest.raises(ValueError, match="video"):
        validate_payload("production_plan.v2", plan)
    plan["placements"][0]["source_audio"] = None
    with pytest.raises(ValueError, match="video"):
        validate_payload("production_plan.v2", plan)


def test_independent_narration_and_silent_plan_are_valid() -> None:
    plan = planned_video_dialogue()
    plan["placements"][0]["source_audio"] = False
    plan["placements"].append({"id": "voice", "kind": "audio", "role": "narration",
                               "track_id": "narration", "description": "画外旁白",
                               "start": {"kind": "beat", "ref_id": "demo", "edge": "start"},
                               "end": {"kind": "beat", "ref_id": "demo", "edge": "end"}})
    for turn in plan["beats"][0]["turns"]:
        turn["speech_placement_id"] = "voice"
    validate_payload("production_plan.v2", plan)
    silent = deepcopy(plan)
    silent["beats"][0]["turns"] = []
    silent["placements"] = silent["placements"][:1]
    silent["placements"][0]["source_audio"] = True
    validate_payload("production_plan.v2", silent)


def _plan_issue_from_validation(plan: dict) -> dict:
    """Mirror the production pipeline: rule ctx first, pydantic loc otherwise."""
    from novelvideo.freezone.remix.contracts import validate_payload
    from novelvideo.freezone.remix.plan_issues import issue_from_pydantic_row

    with pytest.raises(ValueError) as raised:
        validate_payload("production_plan.v2", plan)
    rows = raised.value.__cause__.errors()
    assert len(rows) == 1
    issue = issue_from_pydantic_row(rows[0])
    assert rows[0]["type"] == issue["code"]
    return issue


def test_cross_beat_carrier_reports_code_ids_and_repairable_path() -> None:
    plan = planned_video_dialogue()
    plan["beats"].append({"id": "closing", "purpose": "收尾", "duration_frames": 60,
                          "turns": [{"id": "bye", "text": "再见", "speech_placement_id": "demo-video"}]})

    issue = _plan_issue_from_validation(plan)

    assert issue["code"] == "speech_source_spans_beats"
    assert issue["placement_id"] == "demo-video"
    assert issue["beat_id"] == "demo"
    assert issue["turn_id"] == "bye"
    assert issue["path"] == "/placements/0"


def test_noncontiguous_carrier_reports_the_interrupting_turn() -> None:
    plan = planned_video_dialogue()
    plan["beats"][0]["turns"].insert(1, {"id": "interruption", "text": "等一下"})

    issue = _plan_issue_from_validation(plan)

    assert issue["code"] == "speech_source_turns_not_contiguous"
    assert issue["placement_id"] == "demo-video"
    assert issue["beat_id"] == "demo"
    assert issue["turn_id"] == "answer"
    assert issue["path"] == "/placements/0"


@pytest.mark.parametrize(("mutate", "code", "identity"), [
    (lambda plan: plan["beats"][0]["turns"].__setitem__(
        1, {**plan["beats"][0]["turns"][1], "id": "ask"}), "duplicate_plan_id", {"turn_id": "ask"}),
    (lambda plan: plan["placements"][0].update(
        start={"kind": "turn", "ref_id": "ghost", "edge": "start"}),
     "placement_anchor_missing", {"placement_id": "demo-video", "ref_id": "ghost"}),
    (lambda plan: plan["placements"][0].update(turn_id="ghost"),
     "placement_turn_missing", {"placement_id": "demo-video", "turn_id": "ghost"}),
    (lambda plan: plan["placements"][0].update(source_audio=False),
     "speech_source_muted", {"placement_id": "demo-video", "beat_id": "demo"}),
])
def test_every_cross_object_rule_keeps_a_locatable_code(mutate, code: str, identity: dict) -> None:
    plan = planned_video_dialogue()
    mutate(plan)

    issue = _plan_issue_from_validation(plan)

    assert issue["code"] == code
    assert issue["path"] != "/"
    for key, value in identity.items():
        assert issue[key] == value


def test_reference_and_beat_scope_rules_report_codes_and_ids() -> None:
    plan = planned_video_dialogue()
    plan["placements"][0]["start"] = {"kind": "turn", "ref_id": "ghost", "edge": "start"}
    issue = _plan_issue_from_validation(plan)
    assert issue == {"code": "placement_anchor_missing", "path": "/placements/0/start",
                     "placement_id": "demo-video", "ref_id": "ghost"}

    plan = planned_video_dialogue()
    plan["beats"][0]["turns"][0].update(start_frame=170, end_frame=260)
    issue = _plan_issue_from_validation(plan)
    assert issue == {"code": "turn_exceeds_beat", "path": "/beats/0",
                     "beat_id": "demo", "turn_id": "ask"}


def test_root_level_issue_reports_a_bounded_hint_without_draft_text() -> None:
    from novelvideo.freezone.remix.production_plan import validate_generated_production_plan
    from novelvideo.llm_instrumentation import production_plan_diagnostic_sink
    from novelvideo.freezone.recipe_runtime import get_recipe_for_runtime

    plan = planned_video_dialogue()
    plan["beats"].append({"id": "closing", "purpose": "收尾", "duration_frames": 60,
                          "turns": [{"id": "bye", "text": "这句台词不能进日志",
                                     "speech_placement_id": "demo-video"}]})
    records = []
    with production_plan_diagnostic_sink(records.append):
        with pytest.raises(ValueError, match="plan_validation_failed"):
            validate_generated_production_plan(
                plan, topic=plan["goal"],
                recipe=get_recipe_for_runtime(username="alice", recipe_id="reference-production-plan"),
            )
    failure = next(row for row in records if row["event"] == "production_plan.validation_failed")
    assert failure["validation_issues"][0]["path"] == "/placements/0"
    assert "这句台词" not in json.dumps(records, ensure_ascii=False)
