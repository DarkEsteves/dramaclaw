from __future__ import annotations

import pytest

from novelvideo.freezone.remix.timing import (
    TimingCompileError,
    compile_timing,
    map_source_interval_to_frames,
)


def test_trimmed_double_speed_interval() -> None:
    assert map_source_interval_to_frames(
        start_us=2_000_000,
        end_us=3_000_000,
        source_start_us=1_000_000,
        timeline_start_us=3_000_000,
        speed_num=2,
        speed_den=1,
        fps=30,
    ) == (105, 120)


def test_map_source_interval_uses_half_open_deterministic_boundaries() -> None:
    assert map_source_interval_to_frames(
        start_us=0,
        end_us=1,
        source_start_us=0,
        timeline_start_us=0,
        speed_num=1,
        speed_den=1,
        fps=30,
    ) == (0, 1)


def test_compile_matches_repeated_words_by_character_span() -> None:
    result = compile_timing(
        script={
            "revision": 7,
            "media_hash": "voice-a",
            "text": "go then go",
            "selections": [
                {"selection_id": "second-go", "start_char": 8, "end_char": 10, "script_revision": 7}
            ],
            "events": [{"event_id": "product", "selection_id": "second-go", "required": True}],
        },
        words={
            "script_revision": 7,
            "media_hash": "voice-a",
            "words": [
                {"start_char": 0, "end_char": 2, "start_us": 0, "end_us": 300_000},
                {"start_char": 8, "end_char": 10, "start_us": 800_000, "end_us": 1_100_000},
            ],
        },
        edit_map=[{"source_start_us": 0, "source_end_us": 1_200_000, "timeline_start_us": 0}],
        overrides=[],
        fps=30,
    )

    assert result["events"] == [{"event_id": "product", "start_frame": 24, "end_frame": 33}]
    assert result["unresolved_required_events"] == []


def test_compile_rejects_mismatched_script_or_media_revision() -> None:
    with pytest.raises(TimingCompileError, match="script revision"):
        compile_timing(
            script={"revision": 2, "media_hash": "voice-a", "text": "hi", "selections": [], "events": []},
            words={"script_revision": 1, "media_hash": "voice-a", "words": []},
            edit_map=[],
            overrides=[],
            fps=30,
        )

    with pytest.raises(TimingCompileError, match="media hash"):
        compile_timing(
            script={"revision": 2, "media_hash": "voice-a", "text": "hi", "selections": [], "events": []},
            words={"script_revision": 2, "media_hash": "voice-b", "words": []},
            edit_map=[],
            overrides=[],
            fps=30,
        )


def test_compile_applies_trim_speed_and_concat_to_cross_cut_event() -> None:
    result = compile_timing(
        script={
            "revision": 1,
            "media_hash": "voice-a",
            "text": "one two",
            "selections": [{"selection_id": "phrase", "start_char": 0, "end_char": 7, "script_revision": 1}],
            "events": [{"event_id": "phrase", "selection_id": "phrase", "required": True}],
        },
        words={
            "script_revision": 1,
            "media_hash": "voice-a",
            "words": [{"start_char": 0, "end_char": 7, "start_us": 1_500_000, "end_us": 3_500_000}],
        },
        edit_map=[
            {"source_start_us": 1_000_000, "source_end_us": 2_000_000, "timeline_start_us": 3_000_000, "speed_num": 2, "speed_den": 1},
            {"source_start_us": 2_000_000, "source_end_us": 4_000_000, "timeline_start_us": 3_500_000, "speed_num": 1, "speed_den": 1},
        ],
        overrides=[],
        fps=30,
    )

    assert result["events"] == [
        {"event_id": "phrase", "start_frame": 97, "end_frame": 105},
        {"event_id": "phrase", "start_frame": 105, "end_frame": 150},
    ]


def test_compile_blocks_unresolved_required_event_and_omits_optional_event() -> None:
    result = compile_timing(
        script={
            "revision": 1,
            "media_hash": "voice-a",
            "text": "hello",
            "selections": [{"selection_id": "missing", "start_char": 0, "end_char": 5, "script_revision": 1}],
            "events": [
                {"event_id": "must-find", "selection_id": "missing", "required": True},
                {"event_id": "nice-to-have", "selection_id": "missing", "required": False, "out_of_bounds": "omit"},
            ],
        },
        words={"script_revision": 1, "media_hash": "voice-a", "words": []},
        edit_map=[],
        overrides=[],
        fps=30,
    )

    assert result["events"] == []
    assert result["unresolved_required_events"] == [{"event_id": "must-find", "reason": "selection has no aligned words"}]


def test_compile_blocks_required_event_with_partial_character_alignment() -> None:
    result = compile_timing(
        script={
            "revision": 1,
            "media_hash": "voice-a",
            "text": "hello",
            "selections": [{"selection_id": "hello", "start_char": 0, "end_char": 5, "script_revision": 1}],
            "events": [{"event_id": "hello", "selection_id": "hello", "required": True}],
        },
        words={
            "script_revision": 1,
            "media_hash": "voice-a",
            "words": [{"start_char": 0, "end_char": 2, "start_us": 0, "end_us": 200_000}],
        },
        edit_map=[{"source_start_us": 0, "source_end_us": 1_000_000, "timeline_start_us": 0}],
        overrides=[],
        fps=30,
    )

    assert result["events"] == []
    assert result["unresolved_required_events"] == [
        {"event_id": "hello", "reason": "selection has incomplete aligned character coverage"}
    ]


def test_compile_allows_unaligned_whitespace_between_aligned_words() -> None:
    result = compile_timing(
        script={
            "revision": 1,
            "media_hash": "voice-a",
            "dialogue": "hello world",
            "selections": [{"selection_id": "phrase", "start_char": 0, "end_char": 11, "script_revision": 1}],
            "events": [{"event_id": "phrase", "selection_id": "phrase", "required": True}],
        },
        words={
            "script_revision": 1,
            "media_hash": "voice-a",
            "words": [
                {"start_char": 0, "end_char": 5, "start_us": 0, "end_us": 400_000},
                {"start_char": 6, "end_char": 11, "start_us": 500_000, "end_us": 1_000_000},
            ],
        },
        edit_map=[{"source_start_us": 0, "source_end_us": 1_000_000, "timeline_start_us": 0}],
        overrides=[],
        fps=30,
    )

    assert result["events"] == [{"event_id": "phrase", "start_frame": 0, "end_frame": 30}]
    assert result["unresolved_required_events"] == []


def test_compile_rejects_overlapping_source_edit_segments() -> None:
    with pytest.raises(TimingCompileError, match="overlap"):
        compile_timing(
            script={"revision": 1, "media_hash": "voice-a", "text": "", "selections": [], "events": []},
            words={"script_revision": 1, "media_hash": "voice-a", "words": []},
            edit_map=[
                {"source_start_us": 0, "source_end_us": 1_000_000, "timeline_start_us": 0},
                {"source_start_us": 500_000, "source_end_us": 1_500_000, "timeline_start_us": 1_000_000},
            ],
            overrides=[],
            fps=30,
        )


@pytest.mark.parametrize(
    "word",
    [
        {"start_char": 0, "end_char": 5, "start_us": -1, "end_us": 100_000},
        {"start_char": 0, "end_char": 5, "start_us": 100_000, "end_us": 100_000},
        {"start_char": 0, "end_char": 0, "start_us": 0, "end_us": 100_000},
    ],
)
def test_compile_rejects_malformed_aligned_word(word: dict[str, int]) -> None:
    with pytest.raises(TimingCompileError, match="aligned word"):
        compile_timing(
            script={
                "revision": 1,
                "media_hash": "voice-a",
                "text": "hello",
                "selections": [{"selection_id": "hello", "start_char": 0, "end_char": 5, "script_revision": 1}],
                "events": [{"event_id": "hello", "selection_id": "hello", "required": True}],
            },
            words={"script_revision": 1, "media_hash": "voice-a", "words": [word]},
            edit_map=[{"source_start_us": 0, "source_end_us": 1_000_000, "timeline_start_us": 0}],
            overrides=[],
            fps=30,
        )


def test_compile_requires_explicit_optional_omission_policy() -> None:
    with pytest.raises(TimingCompileError, match="optional event.*omit"):
        compile_timing(
            script={
                "revision": 1,
                "media_hash": "voice-a",
                "text": "hello",
                "selections": [{"selection_id": "hello", "start_char": 0, "end_char": 5, "script_revision": 1}],
                "events": [{"event_id": "optional", "selection_id": "hello", "required": False}],
            },
            words={
                "script_revision": 1,
                "media_hash": "voice-a",
                "words": [{"start_char": 0, "end_char": 5, "start_us": 0, "end_us": 100_000}],
            },
            edit_map=[],
            overrides=[],
            fps=30,
        )


def test_compile_reports_fixed_frame_override_conflict_after_recompile() -> None:
    result = compile_timing(
        script={
            "revision": 1,
            "media_hash": "voice-a",
            "text": "hello",
            "selections": [{"selection_id": "hello", "start_char": 0, "end_char": 5, "script_revision": 1}],
            "events": [{"event_id": "hello", "selection_id": "hello", "required": True}],
        },
        words={
            "script_revision": 1,
            "media_hash": "voice-a",
            "words": [{"start_char": 0, "end_char": 5, "start_us": 1_000_000, "end_us": 2_000_000}],
        },
        edit_map=[{"source_start_us": 0, "source_end_us": 3_000_000, "timeline_start_us": 0}],
        overrides=[{"event_id": "hello", "kind": "fixed_frames", "start_frame": 10, "end_frame": 20}],
        fps=30,
    )

    assert result["events"] == [{"event_id": "hello", "start_frame": 30, "end_frame": 60}]
    assert result["override_conflicts"] == [
        {"event_id": "hello", "reason": "fixed frame override differs from compiled timing", "override": {"start_frame": 10, "end_frame": 20}}
    ]


def test_compile_records_exact_alignment_revision_and_selected_narration_hash() -> None:
    result = compile_timing(
        script={
            "revision": 3,
            "media_hash": "sha256:final-take",
            "text": "hello",
            "selections": [{"selection_id": "hello", "start_char": 0, "end_char": 5, "script_revision": 3}],
            "events": [{"event_id": "hello", "selection_id": "hello", "required": True}],
        },
        words={
            "artifact_revision": 9,
            "script_revision": 3,
            "media_hash": "sha256:final-take",
            "words": [{"text": "hello", "start_char": 0, "end_char": 5, "start_us": 0, "end_us": 1_000_000}],
        },
        edit_map=[{"source_start_us": 0, "source_end_us": 1_000_000, "timeline_start_us": 0}],
        overrides=[],
        fps=30,
    )

    assert result["script_revision"] == 3
    assert result["alignment_revision"] == 9
    assert result["media_hash"] == "sha256:final-take"


def test_compile_applies_minimum_duration_after_speed_mapping_within_one_frame() -> None:
    result = compile_timing(
        script={
            "revision": 1,
            "media_hash": "voice-a",
            "text": "hello",
            "selections": [{"selection_id": "hello", "start_char": 0, "end_char": 5, "script_revision": 1}],
            "events": [{"event_id": "hello", "selection_id": "hello", "required": True}],
        },
        words={
            "artifact_revision": 1,
            "script_revision": 1,
            "media_hash": "voice-a",
            "words": [{"text": "hello", "start_char": 0, "end_char": 5, "start_us": 0, "end_us": 1_000_000}],
        },
        edit_map=[{"source_start_us": 0, "source_end_us": 1_000_000, "timeline_start_us": 0, "speed_num": 2, "speed_den": 1}],
        overrides=[{"event_id": "hello", "kind": "minimum_duration_frames", "frames": 20}],
        fps=30,
    )

    # 2x narration maps to 15 frames, then the linked event grows to the requested floor.
    assert result["events"] == [{"event_id": "hello", "start_frame": 0, "end_frame": 20}]


def test_narration_speed_retimes_linked_event_on_the_output_frame_grid() -> None:
    script = {
        "revision": 1,
        "media_hash": "voice-a",
        "text": "product",
        "selections": [{"selection_id": "product", "start_char": 0, "end_char": 7, "script_revision": 1}],
        "events": [{"event_id": "product", "selection_id": "product", "required": True}],
    }
    words = {
        "script_revision": 1,
        "media_hash": "voice-a",
        "words": [{"text": "product", "start_char": 0, "end_char": 7, "start_us": 1_000_000, "end_us": 2_000_000}],
    }

    normal = compile_timing(script, words, [{"source_start_us": 0, "source_end_us": 2_000_000, "timeline_start_us": 0}], [], 30)
    faster = compile_timing(script, words, [{"source_start_us": 0, "source_end_us": 2_000_000, "timeline_start_us": 0, "speed_num": 2, "speed_den": 1}], [], 30)

    assert normal["events"] == [{"event_id": "product", "start_frame": 30, "end_frame": 60}]
    assert faster["events"] == [{"event_id": "product", "start_frame": 15, "end_frame": 30}]
