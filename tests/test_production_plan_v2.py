"""The editable production source and its compiled render projection."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from novelvideo.freezone.remix.contracts import validate_payload
from novelvideo.freezone.remix.production_plan_v2 import (
    compile_production_plan_v2,
    migrate_production_plan_v1,
    project_production_plan_v2,
    production_speech_source,
    production_speech_source_range,
)
from novelvideo.freezone.remix.render import render_manifest, render_preview_frame, validate_manifest


def _anchor(frame: int) -> dict:
    return {"kind": "program_frame", "frame": frame}


def _plan() -> dict:
    return {
        "schema_version": 2,
        "goal": "横屏咖啡故事",
        "output": {"width": 1920, "height": 1080, "fps_num": 30000, "fps_den": 1001},
        "beats": [
            {"id": "hook", "purpose": "提出问题", "duration_frames": 120,
             "turns": [{"id": "question", "speaker": "甲", "text": "为什么？", "start_frame": 0, "end_frame": 60},
                       {"id": "answer", "speaker": "乙", "text": "因为好喝。", "start_frame": 60, "end_frame": 120}]},
            {"id": "payoff", "purpose": "给出答案", "duration_frames": 120, "turns": []},
        ],
        "resources": [
            {"id": "picture", "node_id": "image-1", "media_kind": "image",
             "media_path": "assets/picture.png", "media_hash": "a" * 64, "duration_us": 0},
            {"id": "music", "node_id": "audio-1", "media_kind": "audio",
             "media_path": "assets/music.wav", "media_hash": "b" * 64, "duration_us": 10_000_000},
        ],
        "placements": [
            {"id": "base", "kind": "visual", "role": "primary", "resource_id": "picture",
             "track_id": "picture", "start": _anchor(0), "end": _anchor(240), "z_order": 0},
            {"id": "score", "kind": "audio", "role": "bgm", "resource_id": "music",
             "track_id": "music", "start": _anchor(0), "end": _anchor(240), "volume": 0.3},
            {"id": "caption", "kind": "subtitle", "role": "dialogue", "turn_id": "answer",
             "track_id": "captions", "start": {"kind": "turn", "ref_id": "answer", "edge": "start", "offset_frames": 0},
             "end": {"kind": "turn", "ref_id": "answer", "edge": "end", "offset_frames": 0}},
        ],
        "provenance": {},
    }


@pytest.mark.parametrize(("change", "expected_code"), [
    (lambda p: p["placements"][0].update(role="bgm"), "invalid_placement_role"),
    (lambda p: p["placements"][2]["start"].pop("edge"), "invalid_semantic_anchor"),
    (lambda p: p["beats"][0]["turns"][0].update(end_frame=121), "turn_exceeds_beat"),
])
def test_plan_semantic_validation_has_stable_error_codes(change, expected_code):
    from pydantic import ValidationError
    from novelvideo.freezone.remix.production_plan_v2 import ProductionPlanV2

    payload = _plan()
    change(payload)
    with pytest.raises(ValidationError) as error:
        ProductionPlanV2.model_validate(payload)
    assert expected_code in {row["type"] for row in error.value.errors()}


def test_speaking_take_reflows_following_beat_and_word_anchored_effect(tmp_path: Path) -> None:
    plan = {
        "schema_version": 2, "goal": "按台词编排", "output": {"width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1},
        "beats": [
            {"id": "spoken", "purpose": "提出问题", "duration_frames": 90,
             "turns": [{"id": "line", "speaker": "嘉宾", "text": "黑咖啡，没有之一。", "speech_placement_id": "talk",
                        "speech_timing": {"placement_id": "talk", "media_hash": "a" * 64, "text": "黑咖啡，没有之一。",
                                          "text_hash": hashlib.sha256("黑咖啡，没有之一。".encode()).hexdigest(),
                                          "source_in_us": 0, "source_out_us": 4_100_000,
                                          "evidence_ref": {"artifact_id": "spoken-words", "revision": 1},
                                          "words": [{"id": "coffee-word", "text": "黑咖啡", "start_us": 2_900_000,
                                                     "end_us": 3_560_000, "start_char": 0, "end_char": 3}]}}]},
            {"id": "next", "purpose": "继续", "duration_frames": 60, "turns": []},
        ],
        "selections": [{"id": "coffee", "label": "说黑咖啡时", "start": {"kind": "word", "ref_id": "coffee-word", "edge": "start"},
                        "end": {"kind": "word", "ref_id": "coffee-word", "edge": "end"}}],
        "resources": [
            {"id": "talk-media", "node_id": "talk-node", "media_kind": "video", "media_path": "media/talk.mp4",
             "media_hash": "a" * 64, "duration_us": 4_100_000, "has_audio": True},
            {"id": "still-media", "node_id": "still-node", "media_kind": "image", "media_path": "media/still.png",
             "media_hash": "b" * 64, "duration_us": 0},
        ],
        "placements": [
            {"id": "talk", "kind": "visual", "role": "primary", "track_id": "primary", "resource_id": "talk-media",
             "start": {"kind": "beat", "ref_id": "spoken", "edge": "start"},
             "end": {"kind": "beat", "ref_id": "spoken", "edge": "end"}},
            {"id": "next-picture", "kind": "visual", "role": "primary", "track_id": "primary", "resource_id": "still-media",
             "start": {"kind": "beat", "ref_id": "next", "edge": "start"},
             "end": {"kind": "beat", "ref_id": "next", "edge": "end"}},
            {"id": "caption", "kind": "subtitle", "role": "dialogue", "track_id": "subtitles", "turn_id": "line",
             "start": {"kind": "turn", "ref_id": "line", "edge": "start"},
             "end": {"kind": "turn", "ref_id": "line", "edge": "end"}},
            {"id": "word-effect", "kind": "graphic", "role": "effect", "track_id": "graphics",
             "graphic": {"template": "title_bar", "text": "黑咖啡"},
             "start": {"kind": "selection", "ref_id": "coffee", "edge": "start"},
             "end": {"kind": "selection", "ref_id": "coffee", "edge": "end"}},
        ], "provenance": {},
    }
    first = compile_production_plan_v2(plan)
    assert first["output_spec"]["duration_frames"] == 183
    assert (first["layers"][0]["start_frame"], first["layers"][0]["end_frame"]) == (0, 123)
    assert (first["layers"][1]["start_frame"], first["layers"][1]["end_frame"]) == (123, 183)
    assert (first["subtitle_events"][0]["start_frame"], first["subtitle_events"][0]["end_frame"]) == (87, 107)
    assert (first["graphic_events"][0]["start_frame"], first["graphic_events"][0]["end_frame"]) == (87, 107)
    projected = project_production_plan_v2(plan, project_dir=tmp_path)
    assert projected["output_spec"]["duration_frames"] == 183
    assert (projected["beats"][1]["start_frame"], projected["beats"][1]["end_frame"]) == (123, 183)
    assert [(row["start_frame"], row["end_frame"]) for row in projected["placements"][:2]] == [(0, 123), (123, 183)]
    plan["resources"][0]["duration_us"] = 5_000_000
    plan["beats"][0]["turns"][0]["speech_timing"]["source_out_us"] = 5_000_000
    longer = compile_production_plan_v2(plan)
    assert longer["layers"][1]["start_frame"] == 150
    assert longer["output_spec"]["duration_frames"] == 210
    assert longer["graphic_events"][0]["start_frame"] == 87
    plan["placements"][0]["start"]["offset_frames"] = 15
    delayed = compile_production_plan_v2(plan)
    assert (delayed["layers"][0]["start_frame"], delayed["layers"][0]["end_frame"]) == (15, 165)
    assert delayed["layers"][1]["start_frame"] == 165
    assert delayed["graphic_events"][0]["start_frame"] == 102
    plan["placements"][0]["source_out_us"] = 4_000_000
    trimmed = compile_production_plan_v2(plan)
    assert trimmed["layers"][1]["start_frame"] == 135


def test_speaking_take_accepts_real_media_length_between_frame_boundaries() -> None:
    plan = {"schema_version": 2, "goal": "非整帧素材", "output": {"width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1},
            "beats": [{"id": "b", "purpose": "发声", "duration_frames": 90,
                       "turns": [{"id": "t", "speaker": "甲", "text": "黑咖啡", "speech_placement_id": "voice",
                                  "speech_timing": {"placement_id": "voice", "media_hash": "a" * 64, "text": "黑咖啡",
                                                    "text_hash": hashlib.sha256("黑咖啡".encode()).hexdigest(),
                                                    "source_in_us": 0, "source_out_us": 4_096_000,
                                                    "evidence_ref": {"artifact_id": "words", "revision": 1},
                                                    "words": [{"id": "w", "text": "黑咖啡", "start_us": 2_900_000, "end_us": 3_560_000}]}}]}],
            "resources": [{"id": "r", "node_id": "n", "media_kind": "audio", "media_path": "media/voice.wav",
                           "media_hash": "a" * 64, "duration_us": 4_096_000, "has_audio": True}],
            "placements": [{"id": "voice", "kind": "audio", "role": "speech", "track_id": "speech", "resource_id": "r",
                            "start": {"kind": "beat", "ref_id": "b", "edge": "start"},
                            "end": {"kind": "beat", "ref_id": "b", "edge": "end"}}], "provenance": {}}
    assert production_speech_source_range(plan, "voice") == (0, 4_096_000)
    manifest = compile_production_plan_v2(plan)
    assert manifest["output_spec"]["duration_frames"] == 123
    assert manifest["audio_events"][0]["source_out_us"] == 4_096_000


def test_actual_speech_reflow_fails_hard_program_duration(tmp_path: Path) -> None:
    from novelvideo.freezone.remix.production_requirements import check_production_requirements

    plan = {
        'schema_version': 2, 'goal': '完整保留对白',
        'output': {'width': 1080, 'height': 1920, 'fps_num': 30, 'fps_den': 1},
        'beats': [{'id': 'speech', 'purpose': '说话', 'duration_frames': 90,
                   'turns': [{'id': 'line', 'speaker': '甲', 'text': '完整台词',
                              'speech_placement_id': 'voice',
                              'speech_timing': {'placement_id': 'voice', 'media_hash': 'a' * 64,
                                                'text': '完整台词',
                                                'text_hash': hashlib.sha256('完整台词'.encode()).hexdigest(),
                                                'source_in_us': 0, 'source_out_us': 4_100_000,
                                                'evidence_ref': {'artifact_id': 'words', 'revision': 1},
                                                'words': [{'id': 'w', 'text': '完整台词',
                                                           'start_us': 0, 'end_us': 4_100_000}]}}]}],
        'resources': [{'id': 'voice-resource', 'node_id': 'voice-node', 'media_kind': 'audio',
                       'media_path': 'media/voice.wav', 'media_hash': 'a' * 64,
                       'duration_us': 4_100_000, 'has_audio': True}],
        'placements': [{'id': 'voice', 'kind': 'audio', 'role': 'speech', 'track_id': 'speech',
                        'resource_id': 'voice-resource',
                        'start': {'kind': 'beat', 'ref_id': 'speech', 'edge': 'start'},
                        'end': {'kind': 'beat', 'ref_id': 'speech', 'edge': 'end'}}],
        'provenance': {},
    }
    assert compile_production_plan_v2(plan)['audio_events'][0]['source_out_us'] == 4_100_000
    checks = check_production_requirements(plan, {'duration_us': {'max': 4_000_000}},
                                           project_dir=tmp_path)
    assert any(row['code'] == 'duration_mismatch' and row['status'] == 'failed' for row in checks)


def test_old_saved_migrated_plan_rebases_generated_cue_without_moving_manual_clock_item() -> None:
    plan = {"schema_version": 2, "goal": "旧方案", "output": {"width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1},
            "beats": [{"id": "first", "purpose": "说话", "duration_frames": 90,
                       "turns": [{"id": "line", "speaker": "甲", "text": "黑咖啡", "speech_placement_id": "visual:first:talk",
                                  "speech_timing": {"placement_id": "visual:first:talk", "media_hash": "a" * 64,
                                                    "text": "黑咖啡", "text_hash": hashlib.sha256("黑咖啡".encode()).hexdigest(),
                                                    "evidence_ref": {"artifact_id": "words", "revision": 1},
                                                    "words": [{"id": "w", "text": "黑咖啡", "start_us": 2_900_000, "end_us": 3_560_000}]}}]},
                      {"id": "second", "purpose": "下一段", "duration_frames": 60, "turns": []}],
            "resources": [{"id": "r", "node_id": "video", "media_kind": "video", "media_path": "media/talk.mp4",
                           "media_hash": "a" * 64, "duration_us": 4_100_000, "has_audio": True}],
            "placements": [{"id": "visual:first:talk", "kind": "visual", "role": "primary", "track_id": "primary", "resource_id": "r",
                            "start": _anchor(0), "end": _anchor(90)},
                           {"id": "graphic:second:legacy", "kind": "graphic", "role": "effect", "track_id": "graphics",
                            "graphic": {"template": "title_bar", "text": "下一段"}, "start": _anchor(90), "end": _anchor(150)},
                           {"id": "manual-clock", "kind": "graphic", "role": "effect", "track_id": "graphics",
                            "graphic": {"template": "title_bar", "text": "固定"}, "start": _anchor(15), "end": _anchor(30)}],
            "provenance": {"source_plan_ref": {"artifact_id": "old-plan", "revision": 1}}}
    manifest = compile_production_plan_v2(plan)
    assert manifest["output_spec"]["duration_frames"] == 183
    assert manifest["layers"][0]["end_frame"] == 123
    assert (manifest["graphic_events"][0]["start_frame"], manifest["graphic_events"][0]["end_frame"]) == (123, 183)
    assert (manifest["graphic_events"][1]["start_frame"], manifest["graphic_events"][1]["end_frame"]) == (15, 30)


def test_two_spoken_turns_use_their_own_takes_then_move_the_next_beat() -> None:
    def turn(identifier: str, text: str, placement_id: str, start: int, end: int, duration_us: int) -> dict:
        return {"id": identifier, "speaker": identifier, "text": text, "start_frame": start, "end_frame": end,
                "speech_placement_id": placement_id,
                "speech_timing": {"placement_id": placement_id, "media_hash": ("a" if identifier == "a" else "b") * 64,
                                  "text": text, "text_hash": hashlib.sha256(text.encode()).hexdigest(),
                                  "evidence_ref": {"artifact_id": identifier + "-words", "revision": 1},
                                  "words": [{"id": identifier + "-word", "text": text, "start_us": 500_000,
                                             "end_us": duration_us - 500_000}]}}
    plan = {"schema_version": 2, "goal": "对话", "output": {"width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1},
            "beats": [{"id": "dialogue", "purpose": "对话", "duration_frames": 180,
                       "turns": [turn("a", "第一句", "a-voice", 0, 90, 3_000_000),
                                 turn("b", "第二句", "b-voice", 90, 180, 4_000_000)]},
                      {"id": "next", "purpose": "下一段", "duration_frames": 30, "turns": []}],
            "resources": [{"id": "a-media", "node_id": "a-node", "media_kind": "audio", "media_path": "media/a.wav",
                           "media_hash": "a" * 64, "duration_us": 3_000_000, "has_audio": True},
                          {"id": "b-media", "node_id": "b-node", "media_kind": "audio", "media_path": "media/b.wav",
                           "media_hash": "b" * 64, "duration_us": 4_000_000, "has_audio": True}],
            "placements": [{"id": identifier + "-voice", "kind": "audio", "role": "speech", "track_id": "speech",
                            "resource_id": identifier + "-media",
                            "start": {"kind": "turn", "ref_id": identifier, "edge": "start"},
                            "end": {"kind": "turn", "ref_id": identifier, "edge": "end"}}
                           for identifier in ("a", "b")], "provenance": {}}
    manifest = compile_production_plan_v2(plan)
    assert manifest["output_spec"]["duration_frames"] == 240
    assert [(row["start_frame"], row["end_frame"]) for row in manifest["audio_events"]] == [(0, 90), (90, 210)]
    plan["placements"][1]["start"] = {"kind": "beat", "ref_id": "dialogue", "edge": "start", "offset_frames": 90}
    plan["placements"][1]["end"] = {"kind": "beat", "ref_id": "dialogue", "edge": "end"}
    beat_relative = compile_production_plan_v2(plan)
    assert beat_relative["output_spec"]["duration_frames"] == 240
    assert [(row["start_frame"], row["end_frame"]) for row in beat_relative["audio_events"]] == [(0, 90), (90, 210)]
    plan["beats"][0]["timing_mode"] = "fixed"
    fixed = compile_production_plan_v2(plan)
    assert fixed["output_spec"]["duration_frames"] == 210


def test_v2_can_save_incomplete_long_horizontal_plan_without_mvp_caps() -> None:
    plan = _plan()
    plan["beats"] = [{"id": f"beat-{i}", "purpose": "叙事", "duration_frames": 60, "turns": []} for i in range(41)]
    plan["resources"] = []
    plan["placements"] = [{"id": "pending", "kind": "visual", "role": "primary", "track_id": "picture",
                           "description": "待做画面", "start": _anchor(0), "end": _anchor(2460), "z_order": 0}]
    saved = validate_payload("production_plan.v2", plan)
    assert len(saved["beats"]) == 41
    assert saved["output"]["fps_num"] == 30000
    with pytest.raises(ValueError, match="pending"):
        compile_production_plan_v2(saved)


def test_legacy_draft_can_cross_previous_limits_before_explicit_v2_migration() -> None:
    legacy = {
        "schema_version": 1,
        "goal": "长片草稿",
        "output": {"width": 1080, "height": 1920, "fps": 30},
        "beats": [
            {"id": f"beat-{index}", "purpose": "叙事", "duration_frames": 90}
            for index in range(41)
        ],
    }
    saved = validate_payload("production_plan.v1", legacy)
    migrated = migrate_production_plan_v1(saved)
    assert len(migrated["beats"]) == 41
    assert sum(beat["duration_frames"] for beat in migrated["beats"]) == 3690


def test_v2_compiles_cross_beat_music_overlay_and_turn_anchor() -> None:
    plan = _plan()
    plan["placements"].insert(1, {"id": "badge", "kind": "visual", "role": "overlay", "resource_id": "picture",
                                  "track_id": "overlay", "start": {"kind": "beat", "ref_id": "hook", "edge": "end", "offset_frames": -15},
                                  "end": {"kind": "beat", "ref_id": "payoff", "edge": "start", "offset_frames": 15},
                                  "z_order": 1, "x": 20, "y": 20, "width": 400, "height": 300})
    manifest = compile_production_plan_v2(plan)
    assert manifest["manifest_version"] == 4
    assert manifest["output_spec"]["duration_frames"] == 240
    assert manifest["output_spec"]["fps_den"] == 1001
    assert manifest["layers"][1]["placement_id"] == "badge"
    assert (manifest["layers"][1]["start_frame"], manifest["layers"][1]["end_frame"]) == (105, 135)
    assert manifest["subtitle_events"][0]["text"] == "乙：因为好喝。"
    assert manifest["subtitle_events"][0]["timing_basis"] == "estimated"
    assert (manifest["subtitle_events"][0]["start_frame"], manifest["subtitle_events"][0]["end_frame"]) == (60, 120)
    assert validate_manifest(manifest) == manifest


def test_graphic_effect_needs_no_media_and_follows_turn_anchor() -> None:
    plan = _plan()
    plan["placements"].append({
        "id": "question-card", "kind": "graphic", "role": "effect", "track_id": "graphics",
        "graphic": {"template": "question_card", "text": "为什么每天少一杯？", "emphasis": "少一杯",
                    "enter": "pop", "enter_frames": 6, "exit_frames": 4},
        "start": {"kind": "turn", "ref_id": "question", "edge": "start"},
        "end": {"kind": "turn", "ref_id": "question", "edge": "end"},
    })
    manifest = compile_production_plan_v2(plan)
    assert manifest["graphic_events"] == [{
        "placement_id": "question-card", "row_id": "question-card", "start_frame": 0,
        "end_frame": 60, "template": "question_card", "text": "为什么每天少一杯？",
        "emphasis": "少一杯", "foreground": "#FFFFFF", "background": "#111827",
        "accent": "#00BDCF", "enter": "pop", "enter_frames": 6, "exit_frames": 4,
    }]
    assert validate_manifest(manifest) == manifest


def test_graphic_effect_projects_ready_without_media(tmp_path: Path) -> None:
    from novelvideo.freezone.remix.production_plan_v2 import project_production_plan_v2

    plan = _plan()
    plan["resources"] = []
    plan["placements"] = [{
        "id": "question-card", "kind": "graphic", "role": "effect", "track_id": "graphics",
        "graphic": {"template": "question_card", "text": "有什么不同？"},
        "start": _anchor(0), "end": _anchor(60),
    }]
    projection = project_production_plan_v2(plan, project_dir=tmp_path)
    assert projection["ready"] is True
    assert projection["placements"][0]["resource_status"] == "ready"
    assert projection["issues"] == []


def test_graphic_effect_rejects_unknown_template_and_media_binding() -> None:
    plan = _plan()
    plan["placements"].append({
        "id": "bad", "kind": "graphic", "role": "effect", "track_id": "graphics",
        "resource_id": "picture", "graphic": {"template": "arbitrary_html", "text": "你好"},
        "start": _anchor(0), "end": _anchor(30),
    })
    with pytest.raises(ValueError):
        validate_payload("production_plan.v2", plan)
    plan["placements"][-1]["graphic"]["template"] = "title_bar"
    with pytest.raises(ValueError, match="graphic|media|resource"):
        validate_payload("production_plan.v2", plan)


@pytest.mark.parametrize("invalid", [
    {"text": ""},
    {"text": "你好", "emphasis": "没有出现的词"},
    {"foreground": "red"},
])
def test_graphic_effect_rejects_invalid_content_and_style(invalid: dict) -> None:
    plan = _plan()
    plan["placements"] = [{
        "id": "card", "kind": "graphic", "role": "effect", "track_id": "graphic:effect",
        "graphic": {"template": "question_card", "text": "提问", **invalid},
        "start": _anchor(0), "end": _anchor(60),
    }]
    with pytest.raises(ValueError, match="production_plan.v2 payload is invalid"):
        validate_payload("production_plan.v2", plan)


def test_v1_ai_graphic_cue_migrates_without_fake_media() -> None:
    legacy = {"schema_version": 1, "goal": "提问视频", "output": {"width": 1920, "height": 1080, "fps": 25},
              "beats": [{"id": "hook", "purpose": "提问", "duration_frames": 100, "turns": [],
                         "visuals": [{"id": "question", "role": "graphic", "description": "问题条",
                                      "graphic": {"template": "question_card", "text": "问题是什么？"}}]}]}
    saved = validate_payload("production_plan.v1", legacy)
    migrated = migrate_production_plan_v1(saved)
    graphic = migrated["placements"][0]
    assert graphic["kind"] == "graphic"
    assert graphic["resource_id"] is None if "resource_id" in graphic else True
    assert graphic["graphic"]["text"] == "问题是什么？"
    assert compile_production_plan_v2(migrated)["graphic_events"][0]["start_frame"] == 0


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg unavailable")
def test_graphic_only_video_renders_and_exact_preview_samples_it(tmp_path: Path) -> None:
    legacy = {"schema_version": 1, "goal": "无素材图形片", "output": {"width": 640, "height": 360, "fps": 25},
              "beats": [{"id": "intro", "purpose": "提问", "duration_frames": 50, "turns": [],
                         "visuals": [{"id": "q", "role": "graphic", "description": "问题",
                                      "graphic": {"template": "question_card", "text": "HELLO QUESTION",
                                                  "enter": "pop", "enter_frames": 6, "exit_frames": 5}}]}]}
    manifest = compile_production_plan_v2(migrate_production_plan_v1(legacy))
    video = render_manifest(manifest, media_paths={}, output_path=tmp_path / "graphic.mp4")
    first_preview = render_preview_frame(manifest, media_paths={}, frame=0, output_path=tmp_path / "first.png")
    preview = render_preview_frame(manifest, media_paths={}, frame=20, output_path=tmp_path / "frame.png")
    assert video.is_file() and preview.is_file() and first_preview.is_file()
    first_rgb = subprocess.run(["ffmpeg", "-v", "error", "-i", str(first_preview), "-frames:v", "1",
                                "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"], check=True, capture_output=True).stdout
    rgb = subprocess.run(["ffmpeg", "-v", "error", "-i", str(preview), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"], check=True, capture_output=True).stdout
    assert max(first_rgb) < 10
    assert max(rgb) > 100
    assert 10 < rgb[(75 * 640 + 230) * 3] < 40  # Opaque card background, not a black subtitle outline.


def test_source_speech_words_follow_placement_move_without_realigning() -> None:
    plan = _plan()
    plan["output"]["fps_num"] = 30
    plan["output"]["fps_den"] = 1
    plan["placements"][1].update(role="speech", turn_id="question", source_in_us=200_000)
    plan["beats"][0]["turns"][0]["speech_timing"] = {
        "placement_id": "score", "media_hash": "b" * 64, "text": "为什么？",
        "text_hash": hashlib.sha256("为什么？".encode()).hexdigest(),
        "evidence_ref": {"artifact_id": "words", "revision": 1},
        "words": [{"id": "spoken-why", "text": "为什么", "start_char": 0, "end_char": 3, "start_us": 500_000, "end_us": 900_000}],
    }
    plan["selections"] = [{"id": "spoken", "label": "说为什么时", "start": {"kind": "word", "ref_id": "spoken-why", "edge": "start"}, "end": {"kind": "word", "ref_id": "spoken-why", "edge": "end"}}]
    plan["placements"].append({"id": "word-overlay", "kind": "visual", "role": "overlay", "track_id": "overlay", "resource_id": "picture", "start": {"kind": "selection", "ref_id": "spoken", "edge": "start"}, "end": {"kind": "selection", "ref_id": "spoken", "edge": "end"}})
    first = compile_production_plan_v2(plan)
    assert (first["layers"][-1]["start_frame"], first["layers"][-1]["end_frame"]) == (9, 21)
    plan["placements"][1]["start"] = _anchor(10)
    plan["placements"][1]["end"] = _anchor(130)
    moved = compile_production_plan_v2(plan)
    assert (moved["layers"][-1]["start_frame"], moved["layers"][-1]["end_frame"]) == (19, 31)


def test_full_media_speech_evidence_survives_changes_to_edit_duration() -> None:
    plan = _plan()
    plan["output"].update(fps_num=30, fps_den=1)
    plan["placements"][1].update(role="speech", turn_id="question")
    plan["beats"][0]["turns"][0]["speech_placement_id"] = "score"
    plan["beats"][0]["turns"][0]["speech_timing"] = {
        "placement_id": "score", "media_hash": "b" * 64, "text": "为什么？",
        "text_hash": hashlib.sha256("为什么？".encode()).hexdigest(),
        "source_in_us": 0, "source_out_us": 10_000_000,
        "evidence_ref": {"artifact_id": "words", "revision": 1},
        "words": [{"id": "spoken-why", "text": "为什么", "start_char": 0, "end_char": 3,
                   "start_us": 500_000, "end_us": 900_000}],
    }
    plan["selections"] = [{"id": "spoken", "label": "说话时", "start": {"kind": "word", "ref_id": "spoken-why", "edge": "start"},
                           "end": {"kind": "word", "ref_id": "spoken-why", "edge": "end"}}]
    plan["placements"].append({"id": "word-overlay", "kind": "visual", "role": "overlay", "track_id": "overlay",
                               "resource_id": "picture", "start": {"kind": "selection", "ref_id": "spoken", "edge": "start"},
                               "end": {"kind": "selection", "ref_id": "spoken", "edge": "end"}})
    first = compile_production_plan_v2(plan)
    assert (first["layers"][-1]["start_frame"], first["layers"][-1]["end_frame"]) == (15, 27)
    plan["placements"][1]["end"] = _anchor(180)
    moved = compile_production_plan_v2(plan)
    assert (moved["layers"][-1]["start_frame"], moved["layers"][-1]["end_frame"]) == (15, 27)


def test_aligned_turn_and_subtitle_follow_spoken_word_bounds() -> None:
    plan = _plan()
    plan["output"]["fps_num"] = 30
    plan["output"]["fps_den"] = 1
    plan["placements"][1].update(role="speech", turn_id="question")
    plan["beats"][0]["turns"][0]["speech_placement_id"] = "score"
    plan["beats"][0]["turns"][0]["speech_timing"] = {
        "placement_id": "score", "media_hash": "b" * 64, "text": "为什么？",
        "text_hash": hashlib.sha256("为什么？".encode()).hexdigest(),
        "evidence_ref": {"artifact_id": "words", "revision": 1},
        "words": [{"id": "spoken-why", "text": "为什么", "start_char": 0, "end_char": 3, "start_us": 500_000, "end_us": 900_000}],
    }
    plan["placements"][2]["turn_id"] = "question"
    plan["placements"][2]["start"]["ref_id"] = "question"
    plan["placements"][2]["end"]["ref_id"] = "question"
    manifest = compile_production_plan_v2(plan)
    assert (manifest["subtitle_events"][0]["start_frame"], manifest["subtitle_events"][0]["end_frame"]) == (15, 27)
    assert manifest["subtitle_events"][0]["timing_basis"] == "aligned"
    assert manifest["subtitle_events"][0]["word_cues"] == [{"start_char": 2, "end_char": 5, "start_frame": 15, "end_frame": 27}]
    plan["placements"][1]["start"] = {"kind": "turn", "ref_id": "question", "edge": "start"}
    plan["placements"][1]["end"] = {"kind": "turn", "ref_id": "question", "edge": "end"}
    anchored = compile_production_plan_v2(plan)
    # Once the speaking Take is semantically attached, its real ten-second media
    # length replaces the old two-second estimate for this turn.
    assert (anchored["audio_events"][0]["start_frame"], anchored["audio_events"][0]["end_frame"]) == (0, 300)
    assert (anchored["subtitle_events"][0]["start_frame"], anchored["subtitle_events"][0]["end_frame"]) == (15, 27)


def test_stale_speech_evidence_does_not_block_non_word_edits() -> None:
    plan = _plan()
    plan["placements"][1].update(role="speech", turn_id="question")
    plan["beats"][0]["turns"][0]["speech_timing"] = {
        "placement_id": "score", "media_hash": "b" * 64, "text": "别的台词",
        "text_hash": hashlib.sha256("别的台词".encode()).hexdigest(), "evidence_ref": {"artifact_id": "words", "revision": 1},
        "words": [{"id": "spoken-why", "text": "为什么", "start_us": 100_000, "end_us": 500_000}],
    }
    assert compile_production_plan_v2(plan)["subtitle_events"]
    plan["selections"] = [{"id": "spoken", "label": "说话时", "start": {"kind": "word", "ref_id": "spoken-why", "edge": "start"}, "end": {"kind": "word", "ref_id": "spoken-why", "edge": "end"}}]
    with pytest.raises(ValueError, match="speech timing|word"):
        compile_production_plan_v2(plan)


def test_saved_source_words_require_their_exact_evidence_revision(tmp_path) -> None:
    from novelvideo.freezone.remix.store import RemixArtifactStore

    store = RemixArtifactStore(tmp_path, "demo")
    original = store.create("production_plan.v2", _plan(), [])
    source_ref = {"artifact_id": original["id"], "revision": original["revision"]}
    evidence = RemixArtifactStore(tmp_path, "demo", producer_version="remix-align-production-speech.v1").create("word_timeline.v1", {
        "plan_ref": source_ref,
        "media_hash": "sha256:" + "b" * 64, "language": "zh", "status": "completed",
        "turn_id": "question", "placement_id": "score",
        "text_hash": hashlib.sha256("为什么？".encode()).hexdigest(),
        "words": [{"text": "为什么", "start_char": 0, "end_char": 3,
                   "start_us": 100_000, "end_us": 500_000}],
    }, [source_ref])
    ref = {"artifact_id": evidence["id"], "revision": evidence["revision"]}
    revised = _plan()
    revised["beats"][0]["turns"][0]["speech_timing"] = {
        "placement_id": "score", "media_hash": "b" * 64, "text": "为什么？",
        "text_hash": hashlib.sha256("为什么？".encode()).hexdigest(), "evidence_ref": ref,
        "words": [{"id": "question:spoken:0", "text": "为什么", "start_us": 100_000, "end_us": 500_000}],
    }
    with pytest.raises(ValueError, match="evidence_ref"):
        store.revise(original["id"], original["revision"], revised, [])
    saved = store.revise(original["id"], original["revision"], revised, [ref])
    assert saved["revision"] == 2


def test_audible_video_selected_for_speech_keeps_its_audio_in_output() -> None:
    plan = _plan()
    plan["resources"].append({"id": "talk", "node_id": "video-node", "media_kind": "video",
                              "media_path": "media/talk.mp4", "media_hash": "c" * 64,
                              "duration_us": 8_000_000, "has_audio": True})
    plan["placements"].append({"id": "talk-picture", "kind": "visual", "role": "primary",
                               "track_id": "visual:primary", "resource_id": "talk",
                               "start": _anchor(0), "end": _anchor(120)})
    plan["beats"][0]["turns"][0]["speech_placement_id"] = "talk-picture"
    manifest = compile_production_plan_v2(plan)
    assert any(row["media_id"] == "talk" and row["role"] == "speech" for row in manifest["audio_events"])


def test_ordinary_audible_video_uses_its_source_audio_once_at_placement_volume() -> None:
    plan = _plan()
    plan['resources'].append({'id': 'ambience', 'node_id': 'video-node', 'media_kind': 'video',
                              'media_path': 'media/ambience.mp4', 'media_hash': 'd' * 64,
                              'duration_us': 8_000_000, 'has_audio': True})
    plan['placements'].append({'id': 'ambience-shot', 'kind': 'visual', 'role': 'overlay',
                               'track_id': 'visual:broll', 'resource_id': 'ambience',
                               'source_audio': True, 'volume': 0.35,
                               'start': _anchor(0), 'end': _anchor(120)})
    manifest = compile_production_plan_v2(plan)
    sounds = [row for row in manifest['audio_events'] if row['media_id'] == 'ambience']
    assert len(sounds) == 1
    assert sounds[0]['volume'] == 0.35
    assert sounds[0]['source_in_us'] == manifest['layers'][-1]['source_in_us']
    assert sounds[0]['source_out_us'] == manifest['layers'][-1]['source_out_us']


def test_explicit_audio_mapping_suppresses_only_matching_video_auto_sound() -> None:
    plan = _plan()
    plan['resources'].append({'id': 'ambience', 'node_id': 'video-node', 'media_kind': 'video',
                              'media_path': 'media/ambience.mp4', 'media_hash': 'd' * 64,
                              'duration_us': 8_000_000, 'has_audio': True})
    visual = {'id': 'ambience-shot', 'kind': 'visual', 'role': 'overlay',
              'track_id': 'visual:overlay', 'resource_id': 'ambience', 'source_audio': True,
              'start': _anchor(0), 'end': _anchor(120)}
    plan['placements'].append(visual)
    plan['placements'].append({'id': 'explicit-sound', 'kind': 'audio', 'role': 'sfx',
                               'track_id': 'audio:sfx', 'resource_id': 'ambience',
                               'start': _anchor(0), 'end': _anchor(120)})
    manifest = compile_production_plan_v2(plan)
    assert [row['placement_id'] for row in manifest['audio_events'] if row['media_id'] == 'ambience'] == ['explicit-sound']
    plan['placements'][-1]['source_in_us'] = 1_000_000
    plan['placements'][-1]['source_out_us'] = 5_004_000
    manifest = compile_production_plan_v2(plan)
    assert len([row for row in manifest['audio_events'] if row['media_id'] == 'ambience']) == 2


def test_speech_alignment_rejects_audible_video_from_another_beat() -> None:
    plan = _plan()
    plan["resources"].append({"id": "talk", "node_id": "video-node", "media_kind": "video",
                              "media_path": "media/talk.mp4", "media_hash": "c" * 64,
                              "duration_us": 8_000_000, "has_audio": True})
    plan["placements"].append({"id": "payoff-video", "kind": "visual", "role": "primary",
                               "track_id": "visual:primary", "resource_id": "talk",
                               "start": _anchor(120), "end": _anchor(240)})
    plan["beats"][0]["turns"][0]["speech_placement_id"] = "payoff-video"
    with pytest.raises(ValueError, match="overlap the current beat"):
        production_speech_source(plan, "question", "payoff-video")
    with pytest.raises(ValueError, match="overlap the current beat"):
        compile_production_plan_v2(plan)


def test_speech_alignment_rejects_material_assigned_to_another_turn() -> None:
    plan = _plan()
    plan["resources"].append({"id": "talk", "node_id": "video-node", "media_kind": "video",
                              "media_path": "media/talk.mp4", "media_hash": "c" * 64,
                              "duration_us": 8_000_000, "has_audio": True})
    plan["placements"].append({"id": "answer-video", "kind": "visual", "role": "primary",
                               "track_id": "visual:primary", "resource_id": "talk", "turn_id": "answer",
                               "start": _anchor(0), "end": _anchor(120)})
    plan["beats"][0]["turns"][0]["speech_placement_id"] = "answer-video"
    with pytest.raises(ValueError, match="current turn"):
        production_speech_source(plan, "question", "answer-video")


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
def test_selected_speaking_video_renders_an_audio_stream(tmp_path: Path) -> None:
    source = tmp_path / "talk.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=64x64:d=1:r=25",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-shortest",
        "-c:v", "mpeg4", "-c:a", "aac", str(source),
    ], check=True)
    plan = {"schema_version": 2, "goal": "讲话视频", "output": {"width": 64, "height": 64, "fps_num": 25, "fps_den": 1},
            "beats": [{"id": "b", "purpose": "讲话", "duration_frames": 25,
                       "turns": [{"id": "t", "speaker": "甲", "text": "你好", "speech_placement_id": "v"}]}],
            "resources": [{"id": "r", "node_id": "n", "media_kind": "video", "media_path": "talk.mp4",
                           "media_hash": hashlib.sha256(source.read_bytes()).hexdigest(), "duration_us": 1_000_000,
                           "has_audio": True}],
            "placements": [{"id": "v", "kind": "visual", "role": "primary", "track_id": "visual", "resource_id": "r",
                            "start": _anchor(0), "end": _anchor(25)}], "provenance": {}}
    manifest = compile_production_plan_v2(plan)
    output = render_manifest(manifest, media_paths={"r": source}, output_path=tmp_path / "output.mp4")
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                            "stream=codec_type", "-of", "json", str(output)], check=True, capture_output=True, text=True)
    assert json.loads(probe.stdout)["streams"][0]["codec_type"] == "audio"


def test_named_word_range_drives_visual_and_sound_without_copied_frames() -> None:
    plan = _plan()
    turn = plan["beats"][0]["turns"][0]
    turn["timing_basis"] = "aligned"
    turn["words"] = [
        {"id": "w-start", "text": "为", "start_frame": 12, "end_frame": 22},
        {"id": "w-end", "text": "么", "start_frame": 22, "end_frame": 35},
    ]
    plan["selections"] = [{
        "id": "question-cue", "label": "提问强调",
        "start": {"kind": "word", "ref_id": "w-start", "edge": "start"},
        "end": {"kind": "word", "ref_id": "w-end", "edge": "end"},
    }]
    for placement in plan["placements"][:2]:
        placement["start"] = {"kind": "selection", "ref_id": "question-cue", "edge": "start"}
        placement["end"] = {"kind": "selection", "ref_id": "question-cue", "edge": "end"}
    manifest = compile_production_plan_v2(plan)
    assert (manifest["layers"][0]["start_frame"], manifest["layers"][0]["end_frame"]) == (12, 35)
    assert (manifest["audio_events"][0]["start_frame"], manifest["audio_events"][0]["end_frame"]) == (12, 35)


def test_named_range_reports_bad_bounds_and_missing_word(tmp_path: Path) -> None:
    from novelvideo.freezone.remix.production_plan_v2 import project_production_plan_v2

    plan = _plan()
    plan["selections"] = [{
        "id": "bad", "label": "倒序",
        "start": {"kind": "turn", "ref_id": "answer", "edge": "start"},
        "end": {"kind": "turn", "ref_id": "question", "edge": "end"},
    }]
    projection = project_production_plan_v2(plan, project_dir=tmp_path)
    assert any(issue["code"] == "invalid_selection_timing" and issue["selection_id"] == "bad" for issue in projection["issues"])
    with pytest.raises(ValueError, match="bad"):
        compile_production_plan_v2(plan)
    plan["selections"][0]["start"] = {"kind": "word", "ref_id": "missing", "edge": "start"}
    with pytest.raises(ValueError, match="missing word"):
        validate_payload("production_plan.v2", plan)


def test_old_v2_payload_without_selections_remains_valid() -> None:
    assert validate_payload("production_plan.v2", _plan())["selections"] == []
    plan = _plan()
    plan["placements"][0]["start"] = {"kind": "selection", "ref_id": "missing", "edge": "start"}
    with pytest.raises(ValueError, match="missing selection"):
        validate_payload("production_plan.v2", plan)


def test_v2_subtitle_position_and_size_reach_the_ass_renderer() -> None:
    from novelvideo.freezone.remix.render import _ass_document

    plan = _plan()
    plan['placements'][2].update({'subtitle_x': 960, 'subtitle_y': 970, 'font_size': 52})
    event = compile_production_plan_v2(plan)['subtitle_events'][0]
    assert (event['x'], event['y'], event['font_size']) == (960, 970, 52)
    document = _ass_document([event], width=1920, height=1080, fps=30)
    assert r'\an2\pos(960,970)\fs52' in document
    plan['placements'][2]['subtitle_y'] = 2000
    with pytest.raises(ValueError, match='subtitle position'):
        compile_production_plan_v2(plan)


def test_v2_rejects_duplicate_ids_and_anchor_cycles() -> None:
    plan = _plan()
    plan["placements"][1]["id"] = "base"
    with pytest.raises(ValueError, match="duplicate"):
        validate_payload("production_plan.v2", plan)
    plan = _plan()
    plan["placements"][0]["start"] = {"kind": "placement", "ref_id": "score", "edge": "start", "offset_frames": 0}
    plan["placements"][1]["start"] = {"kind": "placement", "ref_id": "base", "edge": "start", "offset_frames": 0}
    with pytest.raises(ValueError, match="cycle"):
        compile_production_plan_v2(plan)


def test_v1_migration_is_repeatable_and_retains_original() -> None:
    legacy = {
        "schema_version": 1, "goal": "一段短片", "output": {"width": 1080, "height": 1920, "fps": 30},
        "beats": [{"id": "hook", "purpose": "提问", "duration_frames": 90,
                   "turns": [{"speaker": "甲", "text": "喝什么？"}],
                   "visuals": [{"id": "picture", "role": "primary", "description": "展示杯子"}], "audio": []}],
    }
    original = deepcopy(legacy)
    migrated = migrate_production_plan_v1(legacy)
    assert legacy == original
    assert migrated == migrate_production_plan_v1(legacy)
    assert migrated["beats"][0]["turns"][0]["id"]
    visual = next(row for row in migrated["placements"] if row["kind"] == "visual")
    assert visual.get("resource_id") is None
    assert visual["start"] == {"kind": "beat", "ref_id": "hook", "edge": "start", "offset_frames": 0}
    assert visual["end"] == {"kind": "beat", "ref_id": "hook", "edge": "end", "offset_frames": 0}
    assert validate_payload("production_plan.v2", migrated) == migrated


def _legacy_main_video_dialogue() -> dict:
    return {
        "schema_version": 1, "goal": "两人对话", "output": {"width": 1080, "height": 1920, "fps": 30},
        "beats": [{"id": "talk", "purpose": "一问一答", "duration_frames": 90,
                   "turns": [{"speaker": "主持人", "text": "喝什么？"},
                             {"speaker": "嘉宾", "text": "黑咖啡。"}],
                   "visuals": [{"id": "main", "role": "primary", "description": "双人对话",
                                "media": {"node_id": "video", "media_kind": "video", "media_path": "media/talk.mp4",
                                          "media_hash": "a" * 64, "duration_us": 6_000_000, "has_audio": True}}],
                   "audio": []}],
    }


def test_explicit_no_speech_source_survives_saved_plan_normalization() -> None:
    plan = _plan()
    plan["beats"][0]["turns"][0]["speech_placement_id"] = None
    normalized = validate_payload("production_plan.v2", plan)
    assert "speech_placement_id" in normalized["beats"][0]["turns"][0]
    assert normalized["beats"][0]["turns"][0]["speech_placement_id"] is None
    assert "speech_placement_id" not in normalized["beats"][0]["turns"][1]


@pytest.mark.parametrize("speaker_names", [True, False])
def test_v1_migration_uses_one_full_main_video_take_for_both_speakers(speaker_names: bool) -> None:
    legacy = _legacy_main_video_dialogue()
    if not speaker_names:
        for turn in legacy["beats"][0]["turns"]:
            turn["speaker"] = ""
    original = deepcopy(legacy)
    migrated = migrate_production_plan_v1(legacy)
    assert legacy == original
    assert migrated == migrate_production_plan_v1(legacy)
    turns = migrated["beats"][0]["turns"]
    assert len(migrated["speech_takes"]) == 1
    take = migrated["speech_takes"][0]
    assert take["turn_ids"] == [turn["id"] for turn in turns]
    assert take["source_in_us"] == 0
    assert take.get("source_out_us") is None
    assert all(turn["speech_placement_id"] == "visual:talk:main" for turn in turns)
    assert all(cue["take_id"] == take["id"] and cue["timing_status"] == "draft"
               for cue in migrated["speech_cues"])
    assert next(row for row in migrated["placements"] if row["kind"] == "visual")["speech_take_id"] == take["id"]
    with pytest.raises(ValueError, match="needs accepted timing"):
        compile_production_plan_v2(migrated)


@pytest.mark.parametrize("mode", ["no_dialogue", "silent_video", "additional_voice", "partial_main", "multiple_main"])
def test_v1_migration_does_not_guess_main_video_speech_for_other_uses(mode: str) -> None:
    legacy = _legacy_main_video_dialogue()
    beat = legacy["beats"][0]
    if mode == "no_dialogue":
        beat["turns"] = []
    elif mode == "silent_video":
        beat["visuals"][0]["media"]["has_audio"] = False
    elif mode == "additional_voice":
        beat["audio"] = [{"id": "voice", "role": "speech", "description": "配音",
                          "media": {"node_id": "voice", "media_kind": "audio", "media_path": "media/voice.wav",
                                    "media_hash": "b" * 64, "duration_us": 6_000_000, "has_audio": True}}]
    elif mode == "partial_main":
        beat["visuals"][0]["start_frame"] = 0
        beat["visuals"][0]["end_frame"] = 45
    else:
        second = deepcopy(beat["visuals"][0])
        second["id"] = "other"
        beat["visuals"].append(second)
    migrated = migrate_production_plan_v1(legacy)
    assert not migrated["speech_takes"]
    assert not migrated["speech_cues"]


def test_v2_manifest_accepts_long_horizontal_output_without_truncation() -> None:
    plan = _plan()
    plan["beats"] = [{"id": "long", "purpose": "完整内容", "duration_frames": 2400, "turns": []}]
    plan["placements"] = [{"id": "base", "kind": "visual", "role": "primary", "resource_id": "picture",
                           "track_id": "picture", "start": _anchor(0), "end": _anchor(2400)}]
    plan["resources"] = plan["resources"][:1]
    manifest = compile_production_plan_v2(plan)
    assert manifest["output_spec"]["duration_frames"] == 2400
    assert manifest["output_spec"]["width"] == 1920
    assert validate_payload("composition_manifest.v2", manifest) == manifest
    manifest["layers"][0]["html_effect"] = {"code": "unregistered"}
    with pytest.raises(ValueError, match="unsupported layer fields"):
        validate_manifest(manifest)


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
def test_v2_renders_horizontal_fractional_fps_image(tmp_path: Path) -> None:
    still = tmp_path / "still.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=blue:s=320x180", "-frames:v", "1", str(still)], check=True)
    plan = _plan()
    plan["output"].update({"width": 320, "height": 180, "fps_num": 24000, "fps_den": 1001})
    plan["beats"] = [{"id": "story", "purpose": "展示画面", "duration_frames": 48, "turns": []}]
    plan["resources"] = [{**plan["resources"][0], "media_hash": hashlib.sha256(still.read_bytes()).hexdigest()}]
    plan["placements"] = [{"id": "base", "kind": "visual", "role": "primary", "resource_id": "picture",
                           "track_id": "picture", "start": _anchor(0), "end": _anchor(48)}]
    manifest = compile_production_plan_v2(plan)
    result = render_manifest(manifest, media_paths={"picture": still}, output_path=tmp_path / "result.mp4")
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height,r_frame_rate,nb_frames", "-of", "json", str(result)],
                           check=True, capture_output=True, text=True)
    stream = json.loads(probe.stdout)["streams"][0]
    assert (stream["width"], stream["height"], stream["r_frame_rate"]) == (320, 180, "24000/1001")
    assert int(stream["nb_frames"]) == 48
    preview = render_preview_frame(manifest, media_paths={"picture": still}, frame=24, output_path=tmp_path / "frame.png")
    frame_probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                  "stream=width,height", "-of", "json", str(preview)],
                                 check=True, capture_output=True, text=True)
    assert json.loads(frame_probe.stdout)["streams"][0] == {"width": 320, "height": 180}


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
def test_v2_video_source_trim_and_speed_are_applied(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x64:d=2:r=24",
        "-f", "lavfi", "-i", "color=c=blue:s=64x64:d=2:r=24",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]", str(source),
    ], check=True)
    plan = _plan()
    plan["output"] = {"width": 64, "height": 64, "fps_num": 24, "fps_den": 1}
    plan["beats"] = [{"id": "scene", "purpose": "只取后半段", "duration_frames": 24, "turns": []}]
    plan["resources"] = [{"id": "clip", "node_id": "video-1", "media_kind": "video",
                          "media_path": "assets/source.mp4", "media_hash": hashlib.sha256(source.read_bytes()).hexdigest(),
                          "duration_us": 4_000_000}]
    plan["placements"] = [{"id": "base", "kind": "visual", "role": "primary", "resource_id": "clip",
                           "track_id": "picture", "start": _anchor(0), "end": _anchor(24),
                           "source_in_us": 2_000_000, "source_out_us": 4_000_000,
                           "speed_num": 2, "speed_den": 1}]
    manifest = compile_production_plan_v2(plan)
    result = render_manifest(manifest, media_paths={"clip": source}, output_path=tmp_path / "trimmed.mp4")
    sample = subprocess.run(["ffmpeg", "-v", "error", "-ss", "0.5", "-i", str(result),
                             "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                            check=True, capture_output=True).stdout
    assert sample[2] > sample[0] * 2


def test_projection_keeps_unbound_and_stale_rows_with_resolved_timing(tmp_path):
    from novelvideo.freezone.remix.production_plan_v2 import project_production_plan_v2
    plan = _plan()
    plan['placements'][0].pop('resource_id')
    (tmp_path / 'assets').mkdir()
    (tmp_path / 'assets/music.wav').write_bytes(b'changed')
    projection = project_production_plan_v2(plan, project_dir=tmp_path)
    assert projection['ready'] is False
    assert projection['placements'][0]['start_frame'] == 0
    assert projection['placements'][0]['resource_status'] == 'unbound'
    assert projection['placements'][1]['resource_status'] == 'stale'
    assert {issue['code'] for issue in projection['issues']} == {'unbound_media', 'stale_media'}


def test_projection_rejects_symlink_escape_without_reading_peer(tmp_path):
    from novelvideo.freezone.remix.production_plan_v2 import project_production_plan_v2
    peer = tmp_path.parent / 'peer-private.png'
    peer.write_bytes(b'private')
    (tmp_path / 'assets').mkdir()
    (tmp_path / 'assets/picture.png').symlink_to(peer)
    projection = project_production_plan_v2(_plan(), project_dir=tmp_path)
    assert projection['placements'][0]['resource_status'] == 'invalid'


def test_v2_preview_renders_only_selected_frame_of_long_work(tmp_path, monkeypatch):
    from novelvideo.freezone.remix import render
    plan = _plan()
    plan['beats'] = [{'id': 'long', 'purpose': 'long', 'duration_frames': 100000, 'turns': []}]
    plan['resources'] = plan['resources'][:1]
    plan['placements'] = [{**plan['placements'][0], 'end': _anchor(100000)}]
    manifest = compile_production_plan_v2(plan)
    seen = []
    def record(snapshot, *, media_paths, output_path):
        seen.append(snapshot)
        output_path.write_bytes(b'fake video')
        return output_path
    monkeypatch.setattr(render, 'render_manifest', record)
    monkeypatch.setattr(render.subprocess, 'run', lambda *args, **kwargs: None)
    render.render_preview_frame(manifest, media_paths={}, frame=90000, output_path=tmp_path / 'preview.png')
    assert seen[0]['output_spec']['duration_frames'] == 1
    assert seen[0]['layers'][0]['start_frame'] == 0
    assert seen[0]['layers'][0]['end_frame'] == 1


def test_exact_frame_preview_keeps_active_spoken_word_highlight(tmp_path, monkeypatch):
    from novelvideo.freezone.remix import render

    manifest = compile_production_plan_v2(_plan())
    manifest["subtitle_events"][0]["word_cues"] = [{"start_char": 2, "end_char": 4,
                                                       "start_frame": 70, "end_frame": 80}]
    seen = []
    def record(snapshot, *, media_paths, output_path):
        seen.append(snapshot)
        output_path.write_bytes(b"fake video")
        return output_path
    monkeypatch.setattr(render, "render_manifest", record)
    monkeypatch.setattr(render.subprocess, "run", lambda *args, **kwargs: None)
    render.render_preview_frame(manifest, media_paths={}, frame=75, output_path=tmp_path / "preview.png")
    assert seen[0]["subtitle_events"][0]["word_cues"] == [{"start_char": 2, "end_char": 4,
                                                              "start_frame": 0, "end_frame": 1}]


def test_v2_typed_semantic_ids_do_not_shadow_other_anchor_kinds():
    plan = _plan()
    plan['beats'][0]['turns'][0]['id'] = 'hook'
    plan['placements'][2]['start'] = {'kind': 'beat', 'ref_id': 'hook', 'edge': 'start'}
    plan['placements'][2]['end'] = {'kind': 'beat', 'ref_id': 'hook', 'edge': 'end'}
    manifest = compile_production_plan_v2(plan)
    assert manifest['subtitle_events'][0]['end_frame'] == 120


def test_v2_long_placement_anchor_chain_does_not_use_python_recursion():
    plan = _plan()
    plan['placements'] = [plan['placements'][0]]
    for index in range(1500):
        previous = 'base' if index == 0 else f'copy-{index - 1}'
        plan['placements'].append({**plan['placements'][0], 'id': f'copy-{index}',
                                   'start': {'kind': 'placement', 'ref_id': previous, 'edge': 'start'},
                                   'end': {'kind': 'placement', 'ref_id': previous, 'edge': 'end'}})
    plan['placements'].reverse()
    assert len(compile_production_plan_v2(plan)['layers']) == 1501


def test_projection_reports_geometry_error_before_export(tmp_path):
    from novelvideo.freezone.remix.production_plan_v2 import project_production_plan_v2
    plan = _plan()
    plan['placements'] = [plan['placements'][0]]
    plan['placements'][0]['x'] = 1900
    (tmp_path / 'assets').mkdir()
    (tmp_path / 'assets/picture.png').write_bytes(b'picture')
    plan['resources'][0]['media_hash'] = hashlib.sha256(b'picture').hexdigest()
    projection = project_production_plan_v2(plan, project_dir=tmp_path)
    assert projection['ready'] is False
    assert projection['issues'][0]['code'] == 'invalid_layout'


def _mainline_fixture() -> dict:
    return json.loads((Path(__file__).parent / "fixtures/production-srt-mainline.json").read_text())


def test_text_boundary_uses_ready_sentence_cue_without_inventing_word_time(tmp_path: Path) -> None:
    from novelvideo.freezone.remix.authored_text_timing import make_text_anchor

    plan = _mainline_fixture()
    plan["selections"] = [{"id": "whole-line", "label": "整句",
                           "start": make_text_anchor("Next question", "host", 0),
                           "end": make_text_anchor("Next question", "host", len("Next question"))}]
    projected = project_production_plan_v2(plan, project_dir=tmp_path)["selections"][0]
    assert (projected["start_frame"], projected["end_frame"]) == (138, 174)
    assert projected["timing_quality"] == "manual"
    plan["selections"][0]["start"] = make_text_anchor("Next question", "host", 4)
    unresolved = project_production_plan_v2(plan, project_dir=tmp_path)["selections"][0]
    assert unresolved["start_frame"] is None
    assert unresolved["location_status"] == "unresolved"


def test_shared_speech_mainline_occupies_one_full_video_and_audio() -> None:
    from novelvideo.freezone.remix.production_plan_v2 import ProductionPlanV2
    from novelvideo.freezone.remix.production_timing import project_speech_mainline
    plan = _mainline_fixture()
    projection = project_speech_mainline(ProductionPlanV2.model_validate(plan))
    assert projection["beat_ranges"] == {"first": (0, 192), "silent": (192, 282), "third": (282, 402)}
    assert [(cue["start_frame"], cue["end_frame"]) for cue in projection["speech_cues"]] == [(15, 123), (138, 174), (282, 387)]
    compiled = compile_production_plan_v2(plan)
    assert compiled["output_spec"]["duration_frames"] == 402
    assert len(compiled["layers"]) == 2
    assert len(compiled["audio_events"]) == 2
    assert compiled["subtitle_events"][0]["start_frame"] == 138


def test_legacy_shared_speech_source_keeps_full_actual_duration() -> None:
    from novelvideo.freezone.remix.production_plan_v2 import ProductionPlanV2
    from novelvideo.freezone.remix.production_timing import project_speech_mainline
    plan = _mainline_fixture()
    plan.pop("speech_takes")
    plan.pop("speech_cues")
    plan["placements"][-1]["start"] = {"kind": "turn", "ref_id": "host", "edge": "start"}
    plan["placements"][-1]["end"] = {"kind": "turn", "ref_id": "host", "edge": "end"}
    projection = project_speech_mainline(ProductionPlanV2.model_validate(plan))
    assert projection["beat_ranges"]["first"] == (0, 192)
    assert compile_production_plan_v2(plan)["subtitle_events"][0]["start_frame"] == 138


def test_take_visual_windows_continue_sampling_and_mix_original_once() -> None:
    plan = _mainline_fixture()
    original = plan["placements"][0]
    original["speech_take_id"] = "take:first"
    original["end"] = {"kind": "program_frame", "frame": 90}
    second = {**deepcopy(original), "id": "second-window", "start": {"kind": "program_frame", "frame": 90},
              "end": {"kind": "take", "ref_id": "take:first", "edge": "end"}}
    plan["placements"].append(second)
    result = compile_production_plan_v2(plan)
    window = next(row for row in result["layers"] if row["placement_id"] == "second-window")
    assert window["source_in_us"] == 3_000_000
    assert window["source_out_us"] == 6_400_000
    assert len(result["audio_events"]) == 2


def test_mainline_rational_fps_speed_and_lead_in() -> None:
    from novelvideo.freezone.remix.production_plan_v2 import ProductionPlanV2
    from novelvideo.freezone.remix.production_timing import project_speech_mainline
    plan = _mainline_fixture()
    plan["output"].update(fps_num=24000, fps_den=1001)
    plan["speech_takes"][0].update(speed_num=2, speed_den=1, lead_in_frames=7)
    result = project_speech_mainline(ProductionPlanV2.model_validate(plan))
    assert result["beat_ranges"]["first"] == (0, 84)
    assert result["speech_cues"][1]["start_frame"] == 62


def test_manual_cue_timing_is_authoritative_over_old_asr_and_has_no_fake_words(tmp_path: Path) -> None:
    from novelvideo.freezone.remix.production_plan_v2 import ProductionPlanV2
    from novelvideo.freezone.remix.production_timing import project_speech_mainline
    plan = _mainline_fixture()
    plan["speech_cues"][1]["source_start_us"] = 5_000_000
    projection = project_speech_mainline(ProductionPlanV2.model_validate(plan))
    assert projection["edges"][("turn", "host", "start")] == 150
    assert projection["word_windows"] == []
    assert compile_production_plan_v2(plan)["subtitle_events"][0]["start_frame"] == 150


def test_draft_speech_cues_block_export_and_are_projection_issues(tmp_path: Path) -> None:
    plan = _mainline_fixture()
    plan["speech_cues"][1].update(timing_status="draft", source_start_us=None, source_end_us=None)
    projection = project_production_plan_v2(plan, project_dir=tmp_path)
    assert not projection["ready"]
    assert any(row["code"] == "unresolved_speech_timing" for row in projection["issues"])
    with pytest.raises(ValueError, match="accepted timing"):
        compile_production_plan_v2(plan)


def test_take_audio_origin_is_not_mixed_twice() -> None:
    plan = _mainline_fixture()
    plan["placements"][0].update(kind="audio", role="speech", track_id="speech")
    result = compile_production_plan_v2(plan)
    assert len(result["audio_events"]) == 2
    assert len(result["layers"]) == 1


@pytest.mark.parametrize('role', ['bgm', 'sfx'])
def test_take_audio_rejects_exact_explicit_duplicate_under_other_roles(role: str) -> None:
    plan = _mainline_fixture()
    plan['placements'].append({'id': 'same-sound', 'kind': 'audio', 'role': role,
                               'track_id': 'audio:extra', 'resource_id': 'video',
                               'start': {'kind': 'beat', 'ref_id': 'first', 'edge': 'start'},
                               'end': {'kind': 'beat', 'ref_id': 'first', 'edge': 'end'},
                               'source_in_us': 0, 'source_out_us': 6_400_000})
    with pytest.raises(ValueError, match='duplicates speech take'):
        compile_production_plan_v2(plan)
    plan['placements'][-1]['start'] = {'kind': 'program_frame', 'frame': 1}
    plan['placements'][-1]['end'] = {'kind': 'program_frame', 'frame': 193}
    manifest = compile_production_plan_v2(plan)
    assert len([row for row in manifest['audio_events'] if row['media_id'] == 'video']) == 2


def test_explicit_origin_visual_uses_take_owned_trim_and_speed(tmp_path: Path) -> None:
    plan = _mainline_fixture()
    plan["speech_takes"][0].update(source_in_us=100_000, source_out_us=6_000_000, speed_num=2)
    assert production_speech_source_range(plan, "talk") == (100_000, 6_000_000)
    result = compile_production_plan_v2(plan)
    visual = next(row for row in result["layers"] if row["placement_id"] == "talk")
    assert (visual["source_in_us"], visual["source_out_us"], visual["speed_num"]) == (100_000, 6_000_000, 2)
    projection = project_production_plan_v2(plan, project_dir=tmp_path)
    visual = next(row for row in projection["placements"] if row["id"] == "talk")
    assert (visual["source_in_us"], visual["source_out_us"], visual["speed_num"]) == (100_000, 6_000_000, 2)
    assert not any(row["code"] == "invalid_source_range" for row in visual["issues"])


def test_unassigned_turn_in_auto_take_beat_blocks_export() -> None:
    plan = _mainline_fixture()
    plan["beats"][0]["turns"].append({"id": "new", "speaker": "guest", "text": "More"})
    with pytest.raises(ValueError, match="assigned take"):
        compile_production_plan_v2(plan)
    plan["beats"][0].update(timing_mode="fixed", duration_frames=192)
    compile_production_plan_v2(plan)


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
def test_shared_take_actual_export_keeps_audio_continuous_under_overlay(tmp_path: Path) -> None:
    import array
    import math

    source, overlay = tmp_path / "dialogue.mp4", tmp_path / "overlay.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x180:d=2:r=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1:sample_rate=48000",
                    "-f", "lavfi", "-i", "sine=frequency=880:duration=1:sample_rate=48000",
                    "-filter_complex", "[1:a][2:a]concat=n=2:v=0:a=1[a]", "-map", "0:v", "-map", "[a]",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=320x180",
                    "-frames:v", "1", str(overlay)], check=True)
    plan = _mainline_fixture()
    plan["output"].update(width=320, height=180)
    plan["beats"] = plan["beats"][:1]
    plan["beats"][0]["duration_frames"] = 45
    plan["resources"] = [{**plan["resources"][0], "duration_us": 2_000_000,
                           "media_hash": hashlib.sha256(source.read_bytes()).hexdigest()},
                          {"id": "overlay", "node_id": "overlay-node", "media_kind": "image",
                           "media_path": "media/overlay.png", "media_hash": hashlib.sha256(overlay.read_bytes()).hexdigest(),
                           "duration_us": 0}]
    plan["speech_takes"] = plan["speech_takes"][:1]
    plan["speech_cues"] = plan["speech_cues"][:2]
    for cue, start, end in zip(plan["speech_cues"], (200_000, 1_200_000), (800_000, 1_800_000)):
        cue.update(source_start_us=start, source_end_us=end)
    for turn in plan["beats"][0]["turns"]:
        turn.pop("speech_timing", None)
    plan["placements"] = [plan["placements"][0], plan["placements"][-1],
                          {"id": "cover", "kind": "visual", "role": "overlay", "track_id": "overlay",
                           "resource_id": "overlay", "z_order": 1,
                           "start": {"kind": "cue", "ref_id": "cue:guest", "edge": "start", "offset_frames": 12},
                           "end": {"kind": "cue", "ref_id": "cue:guest", "edge": "end"}}]
    manifest = compile_production_plan_v2(plan)
    assert len(manifest["audio_events"]) == 1
    assert manifest["subtitle_events"][0]["start_frame"] == 36
    assert next(row for row in manifest["layers"] if row["placement_id"] == "cover")["start_frame"] == 18
    result = render_manifest(manifest, media_paths={"video": source, "overlay": overlay}, output_path=tmp_path / "result.mp4")

    def audio(path: Path, start: float) -> array.array:
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(start), "-i", str(path), "-t", "0.25",
                              "-vn", "-ac", "1", "-ar", "48000", "-f", "f32le", "pipe:1"],
                             check=True, capture_output=True).stdout
        samples = array.array("f")
        samples.frombytes(raw)
        return samples

    def energy(samples: array.array, frequency: int) -> float:
        return abs(sum(value * complex(math.cos(2 * math.pi * frequency * i / 48000), math.sin(2 * math.pi * frequency * i / 48000)) for i, value in enumerate(samples)))

    first, second = audio(result, 0.3), audio(result, 1.3)
    assert energy(first, 440) > 5 * energy(first, 880)
    assert energy(second, 880) > 5 * energy(second, 440)
    original = audio(source, 1.3)
    rms = lambda values: math.sqrt(sum(value * value for value in values) / len(values))
    assert 0.8 < rms(second) / rms(original) < 1.2
    for time, red in ((0.3, False), (0.7, True), (1.1, False)):
        pixel = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(time), "-i", str(result), "-frames:v", "1",
                                "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"], check=True, capture_output=True).stdout[:3]
        assert (pixel[0] > pixel[2]) is red


def test_split_take_audio_cannot_mix_an_extra_original_audio_window() -> None:
    plan = _mainline_fixture()
    plan["placements"][0].update(kind="audio", role="speech", track_id="speech")
    copied = deepcopy(plan["placements"][0])
    copied.update(id="split-audio", start={"kind": "program_frame", "frame": 90})
    plan["placements"].append(copied)
    with pytest.raises(ValueError, match="duplicates speech take"):
        compile_production_plan_v2(plan)


def test_take_original_audio_volume_is_author_owned() -> None:
    plan = _mainline_fixture()
    plan["speech_takes"][0]["volume"] = 0.3
    audio = compile_production_plan_v2(plan)["audio_events"]
    assert next(row for row in audio if row["row_id"] == "take:first")["volume"] == 0.3
