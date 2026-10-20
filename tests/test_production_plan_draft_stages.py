"""Stage results compose into one native editable production plan."""

from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from novelvideo.freezone.remix.production_plan_draft_stages import (
    assemble_stage_draft,
    draft_stage_schema,
    prepare_outline,
)


def _stage_pair():
    outline = prepare_outline({
        "creative_direction": "同一段连续对话",
        "output": {"width": 720, "height": 1280, "fps_num": 30, "fps_den": 1},
        "beats": [{"purpose": "提问和回应", "duration_frames": 150,
                   "turns": [{"speaker": "甲", "text": "你好？"},
                             {"speaker": "乙", "text": "你好。"}]}],
    })
    arrangement = {"selections": [], "placements": [{
        "id": "performance-1", "kind": "visual", "role": "primary",
        "track_id": "visual:main", "source_audio": True,
        "start": {"kind": "beat", "ref_id": "beat-1", "edge": "start"},
        "end": {"kind": "beat", "ref_id": "beat-1", "edge": "end"},
    }], "speech_bindings": {"turn-1": "performance-1", "turn-2": "performance-1"}}
    return outline, arrangement


def test_stages_keep_one_dialogue_owner_and_shared_video_voice():
    outline, arrangement = _stage_pair()
    before = deepcopy((outline, arrangement))

    plan = assemble_stage_draft("已确认目标", outline, arrangement)

    assert (outline, arrangement) == before
    assert plan["goal"] == "已确认目标"
    assert [turn["text"] for turn in plan["beats"][0]["turns"]] == ["你好？", "你好。"]
    assert {turn["speech_placement_id"] for turn in plan["beats"][0]["turns"]} == {"performance-1"}
    assert not any(row["kind"] == "audio" for row in plan["placements"])
    assert "creative_direction" not in plan and "speech_bindings" not in plan


def test_outline_assigns_ordered_ids_and_accepts_silent_passage():
    outline = prepare_outline({
        "creative_direction": "动作转场",
        "output": {"width": 720, "height": 1280, "fps_num": 30, "fps_den": 1},
        "beats": [{"purpose": "提问", "duration_frames": 90,
                   "turns": [{"speaker": "主持人", "text": "为什么？"}]},
                  {"purpose": "展示动作", "duration_frames": 120, "turns": []}],
    })
    assert [beat["id"] for beat in outline["beats"]] == ["beat-1", "beat-2"]
    assert outline["beats"][0]["turns"][0]["id"] == "turn-1"
    assert outline["beats"][1]["turns"] == []


def test_stage_schemas_keep_only_their_authored_root_fields():
    outline_schema = draft_stage_schema("outline")
    arrangement_schema = draft_stage_schema("arrangement")
    assert set(outline_schema["properties"]) == {"creative_direction", "output", "beats"}
    assert set(arrangement_schema["properties"]) == {"selections", "placements", "speech_bindings"}
    errors = list(Draft202012Validator(outline_schema).iter_errors({
        "creative_direction": "x",
        "output": {"width": 720, "height": 1280, "fps_num": 30, "fps_den": 1},
        "beats": [{"purpose": "动作", "duration_frames": 30, "turns": []}],
        "placements": [],
    }))
    assert errors and all(error.validator == "additionalProperties" for error in errors)


def test_arrangement_cannot_replace_story_or_bind_an_unknown_turn():
    outline, arrangement = _stage_pair()
    arrangement["beats"] = []
    with pytest.raises(ValueError, match="production_plan_stage_validation_failed"):
        assemble_stage_draft("目标", outline, arrangement)
    arrangement.pop("beats")
    arrangement["speech_bindings"]["turn-99"] = "performance-1"
    with pytest.raises(ValueError, match="production_plan_stage_validation_failed"):
        assemble_stage_draft("目标", outline, arrangement)


def test_arrangement_rejects_missing_voice_placement():
    outline, arrangement = _stage_pair()
    arrangement["speech_bindings"]["turn-1"] = "missing"
    with pytest.raises(ValueError, match="production_plan_stage_validation_failed"):
        assemble_stage_draft("目标", outline, arrangement)


def test_arrangement_rejects_malformed_rows_as_stage_errors():
    outline, arrangement = _stage_pair()
    arrangement["placements"] = [None]
    with pytest.raises(ValueError, match="production_plan_stage_validation_failed"):
        assemble_stage_draft("目标", outline, arrangement)


def test_two_beats_can_share_one_continuous_music_placement():
    from novelvideo.freezone.remix.production_plan import validate_generated_production_plan
    from novelvideo.freezone.recipe_runtime import get_recipe_for_runtime

    outline = prepare_outline({
        "creative_direction": "先提问，再展示答案",
        "output": {"width": 720, "height": 1280, "fps_num": 30, "fps_den": 1},
        "beats": [
            {"purpose": "提问", "duration_frames": 90,
             "turns": [{"speaker": "甲", "text": "今天喝什么？"}]},
            {"purpose": "展示", "duration_frames": 90, "turns": []},
        ],
    })
    arrangement = {"selections": [], "speech_bindings": {"turn-1": "talking-video"},
                   "placements": [
        {"id": "talking-video", "kind": "visual", "role": "primary", "track_id": "visual:main",
         "source_audio": True,
         "start": {"kind": "beat", "ref_id": "beat-1", "edge": "start"},
         "end": {"kind": "beat", "ref_id": "beat-1", "edge": "end"}},
        {"id": "answer-video", "kind": "visual", "role": "primary", "track_id": "visual:main",
         "start": {"kind": "beat", "ref_id": "beat-2", "edge": "start"},
         "end": {"kind": "beat", "ref_id": "beat-2", "edge": "end"}},
        {"id": "music", "kind": "audio", "role": "bgm", "track_id": "audio:music",
         "start": {"kind": "beat", "ref_id": "beat-1", "edge": "start"},
         "end": {"kind": "beat", "ref_id": "beat-2", "edge": "end"}},
    ]}
    draft = assemble_stage_draft("做一条问答短片", outline, arrangement)
    recipe = get_recipe_for_runtime(username="alice", recipe_id="reference-production-plan")
    validated = validate_generated_production_plan(
        draft, topic="做一条问答短片", recipe=recipe)
    assert len(validated["beats"]) == 2
    assert [row["id"] for row in validated["placements"] if row["kind"] == "audio"] == ["music"]
    assert validated["beats"][0]["turns"][0]["speech_placement_id"] == "talking-video"


def test_operation_stage_record_is_private_and_project_scoped(tmp_path):
    from novelvideo.freezone.remix.operations import RemixOperationStore

    store = RemixOperationStore(tmp_path, "demo")
    operation = store.start(
        idempotency_key="stage-record", input_refs=[], producer_version="remix-process.v1",
        normalized_parameters={"action": "generate_production_plan_draft", "payload": {"topic": "咖啡"}},
        node_revision=1, canvas_id="canvas", node_id="plan",
    )
    operation_id = operation["operation_id"]
    store.record_draft_stage(operation_id, "task-1", "outline", {"beats": []})
    assert store.read_draft_stage(operation_id, "task-1", "outline") == {"beats": []}
    assert "draft_stages" not in store.get(operation_id)
    with pytest.raises(ValueError):
        store.record_draft_stage(operation_id, "../other", "outline", {})
    with pytest.raises((ValueError, FileNotFoundError)):
        RemixOperationStore(tmp_path, "other").read_draft_stage(operation_id, "task-1", "outline")


def test_saved_outline_keeps_edited_ids_and_stage():
    from novelvideo.freezone.remix.production_plan_draft_stages import outline_to_plan, outline_from_plan
    from novelvideo.freezone.remix.contracts import validate_payload
    outline, _ = _stage_pair()
    plan = outline_to_plan('方向', outline)
    assert plan['authoring_stage'] == 'outline'
    assert plan['placements'] == plan['resources'] == []
    plan['beats'][0]['turns'][0]['id'] = 'human-turn'
    plan['beats'][0]['turns'][0]['text'] = '用户修改'
    saved = validate_payload('production_plan.v2', plan)
    reloaded = outline_from_plan(saved)
    assert reloaded['beats'][0]['turns'][0]['id'] == 'human-turn'
    assert reloaded['beats'][0]['turns'][0]['text'] == '用户修改'
    assert reloaded['creative_direction'] == '方向'
