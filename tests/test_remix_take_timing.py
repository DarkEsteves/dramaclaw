from __future__ import annotations

import pytest

from novelvideo.freezone.remix.take_timing import compile_take_timing
from novelvideo.freezone.remix.contracts import PerformanceSegment
from novelvideo.freezone.remix.contracts import RemixScript


def test_performance_segment_accepts_custom_references_and_rejects_duplicate_nodes() -> None:
    base = {"segment_id": "one", "start_char": 0, "end_char": 2, "speaker": "甲", "source": "speaking_video"}
    segment = PerformanceSegment.model_validate({**base, "reference_bindings": [
        {"node_id": "shot", "purpose": "保持镜头运动"}, {"node_id": "voice", "purpose": "参考音色"},
    ]})
    assert [item.purpose for item in segment.reference_bindings] == ["保持镜头运动", "参考音色"]
    without_purpose = PerformanceSegment.model_validate({**base, "reference_bindings": [
        {"node_id": "shot", "purpose": ""},
    ]})
    assert without_purpose.reference_bindings[0].purpose == ""
    with pytest.raises(ValueError, match="duplicate"):
        PerformanceSegment.model_validate({**base, "reference_bindings": [
            {"node_id": "shot", "purpose": "镜头"}, {"node_id": "shot", "purpose": "构图"},
        ]})


def test_one_take_aligns_two_spoken_turns_without_speaker_labels() -> None:
    dialogue = "男：你好。\n女：再见。"
    segment = {"segment_id": "both", "start_char": 2, "end_char": len(dialogue), "speaker": "多人", "source": "speaking_video",
               "speech_ranges": [{"start_char": 2, "end_char": 5}, {"start_char": 8, "end_char": 11}]}
    script = {"dialogue": dialogue, "revision": 1, "segments": [segment],
              "selections": [{"selection_id": "second", "start_char": 8, "end_char": 10, "script_revision": 1}],
              "events": [{"event_id": "second", "selection_id": "second", "required": True}]}
    take = {"segment_id": "both", "media_hash": "sha256:" + "a" * 64, "duration_us": 2_000_000,
            "alignment": {"status": "completed", "transcript": "你好。\n再见。", "media_hash": "sha256:" + "a" * 64,
                          "script_revision": 1, "words": [
                              {"text": "你好", "start_char": 0, "end_char": 2, "start_us": 100_000, "end_us": 500_000},
                              {"text": "再见", "start_char": 4, "end_char": 6, "start_us": 1_100_000, "end_us": 1_500_000},
                          ]}}
    result = compile_take_timing(script, [take], fps=30)
    assert result["takes"][0]["caption"] == "你好。\n再见。"
    assert result["events"] == [{"event_id": "second", "start_frame": 33, "end_frame": 45}]


def test_spoken_ranges_must_be_ordered_inside_the_segment() -> None:
    base = {"dialogue": "男：你好。\n女：再见。", "segments": [{"segment_id": "both", "start_char": 2, "end_char": 11,
            "speaker": "多人", "source": "speaking_video", "speech_ranges": [
                {"start_char": 8, "end_char": 11}, {"start_char": 2, "end_char": 5}]}],
            "selections": [], "blueprint_mappings": [], "media_requirements": []}
    with pytest.raises(ValueError, match="speech range"):
        RemixScript.model_validate(base)


def test_take_timing_joins_exact_media_and_maps_broll_to_spoken_words() -> None:
    script = {
        "dialogue": "男：你好。\n女：咖啡来了。", "revision": 4,
        "segments": [
            {"segment_id": "one", "start_char": 2, "end_char": 5, "speaker": "男", "source": "speaking_video"},
            {"segment_id": "two", "start_char": 8, "end_char": 13, "speaker": "女", "source": "speaking_video"},
        ],
        "selections": [{"selection_id": "coffee", "start_char": 8, "end_char": 10, "script_revision": 4}],
        "events": [{"event_id": "coffee", "selection_id": "coffee", "required": True}],
    }
    takes = [
        {"segment_id": "one", "media_hash": "sha256:" + "a" * 64, "duration_us": 1_000_000,
         "alignment": {"status": "completed", "transcript": "你好。", "media_hash": "sha256:" + "a" * 64, "script_revision": 4,
                       "words": [{"text": "你好", "start_char": 0, "end_char": 2, "start_us": 100_000, "end_us": 600_000}]}},
        {"segment_id": "two", "media_hash": "sha256:" + "b" * 64, "duration_us": 2_000_000,
         "alignment": {"status": "completed", "transcript": "咖啡来了。", "media_hash": "sha256:" + "b" * 64, "script_revision": 4,
                       "words": [{"text": "咖啡", "start_char": 0, "end_char": 2, "start_us": 200_000, "end_us": 800_000}]}},
    ]
    result = compile_take_timing(script, takes, fps=30)
    assert [(row["segment_id"], row["start_frame"], row["end_frame"]) for row in result["takes"]] == [
        ("one", 0, 30), ("two", 30, 90),
    ]
    assert result["events"] == [{"event_id": "coffee", "start_frame": 36, "end_frame": 54}]
    assert result["takes"][1]["word_cues"] == [{"text": "咖啡", "start_char": 0, "end_char": 2, "start_frame": 36, "end_frame": 54}]


def test_take_timing_rejects_alignment_for_a_different_media_hash() -> None:
    script = {"dialogue": "你好", "revision": 1, "segments": [{"segment_id": "one", "start_char": 0, "end_char": 2, "speaker": "甲", "source": "voiceover"}], "selections": [], "events": []}
    take = {"segment_id": "one", "media_hash": "sha256:" + "a" * 64, "duration_us": 1_000_000,
            "alignment": {"status": "completed", "transcript": "你好", "media_hash": "sha256:" + "b" * 64,
                          "script_revision": 1, "words": [{"text": "你好", "start_char": 0, "end_char": 2, "start_us": 0, "end_us": 500_000}]}}
    with pytest.raises(ValueError, match="media hash"):
        compile_take_timing(script, [take], fps=30)
