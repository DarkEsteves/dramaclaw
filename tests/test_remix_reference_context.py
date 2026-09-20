"""Video and transcript evidence share one source timeline."""

import hashlib

from novelvideo.freezone.remix.reference_context import build_reference_events
from novelvideo.freezone.remix.reference_context import load_reference_events
from novelvideo.freezone.remix.reference_context import bind_reference_story
from novelvideo.freezone.remix.reference_context import select_reference_frame_times
from novelvideo.freezone.remix.store import RemixArtifactStore


def test_reference_events_pair_scene_windows_with_spoken_words() -> None:
    evidence = {
        "duration_us": 4_000_000,
        "cuts": [{"time_us": 2_000_000}],
        "representative_frames": [
            {"pts": 1, "time_us": 500_000},
            {"pts": 2, "time_us": 2_500_000},
        ],
    }
    transcript = {
        "status": "completed",
        "words": [
            {"text": "hello", "start_us": 300_000, "end_us": 700_000},
            {"text": "world", "start_us": 2_200_000, "end_us": 2_700_000},
        ],
    }

    assert build_reference_events(evidence, transcript) == [
        {
            "start_us": 0,
            "end_us": 2_000_000,
            "frame_pts": [1],
            "frame_times_us": [500_000],
            "spoken_text": "hello",
            "word_spans": [{"text": "hello", "start_us": 300_000, "end_us": 700_000}],
        },
        {
            "start_us": 2_000_000,
            "end_us": 4_000_000,
            "frame_pts": [2],
            "frame_times_us": [2_500_000],
            "spoken_text": "world",
            "word_spans": [{"text": "world", "start_us": 2_200_000, "end_us": 2_700_000}],
        },
    ]


def test_reference_events_allow_silent_video_and_ignore_invalid_cut_times() -> None:
    evidence = {
        "duration_us": 1_000_000,
        "cuts": [{"time_us": 0}, {"time_us": 1_000_000}, {"time_us": 400_000}],
        "representative_frames": [],
    }

    events = build_reference_events(evidence, None)

    assert [(event["start_us"], event["end_us"], event["spoken_text"]) for event in events] == [
        (0, 400_000, ""), (400_000, 1_000_000, ""),
    ]


def test_reference_context_reads_exact_project_artifact_revisions(tmp_path) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    digest = f"sha256:{hashlib.sha256(b'video').hexdigest()}"
    store = RemixArtifactStore(tmp_path, "demo")
    evidence = store.create("reference_evidence.v1", {
        "media_hash": digest, "time_base": "1/1000", "duration_us": 1_000_000,
        "cuts": [], "representative_frames": [],
    }, [])
    words = store.create("word_timeline.v1", {
        "media_hash": digest, "language": "en", "status": "completed", "transcript": "hello",
        "words": [{"text": "hello", "start_char": 0, "end_char": 5, "start_us": 1, "end_us": 500_000}],
    }, [])
    refs = ({"artifact_id": evidence["id"], "revision": 1}, {"artifact_id": words["id"], "revision": 1})

    assert load_reference_events(tmp_path, "demo", video, *refs)[0]["spoken_text"] == "hello"

    video.write_bytes(b"changed")
    import pytest
    with pytest.raises(ValueError, match="source video"):
        load_reference_events(tmp_path, "demo", video, *refs)


def test_story_notes_keep_evidence_times_and_observation_separate_from_inference() -> None:
    events = [
        {"start_us": 0, "end_us": 1_000_000, "spoken_text": "你好", "frame_times_us": [500_000]},
        {"start_us": 1_000_000, "end_us": 2_000_000, "spoken_text": "咖啡", "frame_times_us": [1_500_000]},
    ]
    shots = [
        {"start_time": 0, "end_time": 1, "visual_description": "人物起床", "narrative": "提出问题"},
        {"start_time": 1, "end_time": 2, "visual_description": "杯子特写", "narrative": "产品揭晓"},
    ]

    notes = bind_reference_story(events, shots, [500_000, 1_500_000], ["/one.png", "/two.png"])

    assert notes[0] == {
        **events[0], "visual_observation": "人物起床", "narrative_inference": "提出问题",
        "frame_url": "/one.png", "analysis_frame_time_us": 500_000, "note_status": "needs_review",
    }
    assert notes[1]["start_us"] == 1_000_000
    assert notes[1]["spoken_text"] == "咖啡"
    assert notes[1]["frame_url"] == "/two.png"


def test_excess_scene_cuts_are_merged_to_vision_budget() -> None:
    evidence = {"duration_us": 100_000_000, "cuts": [{"time_us": i * 1_000_000} for i in range(1, 100)], "representative_frames": []}
    events = build_reference_events(evidence, None)
    assert len(events) == 60
    assert events[0]["start_us"] == 0
    assert events[-1]["end_us"] == 100_000_000


def test_model_notes_with_wrong_timing_are_not_attached_to_another_event() -> None:
    events = [{"start_us": 0, "end_us": 1_000_000}, {"start_us": 1_000_000, "end_us": 2_000_000}]
    shots = [
        {"start_time": 1, "end_time": 2, "visual_description": "错误顺序"},
        {"start_time": 0, "end_time": 1, "visual_description": "错误顺序"},
    ]
    notes = bind_reference_story(events, shots, [], [])
    assert all(note["visual_observation"] == "" for note in notes)


def test_every_event_gets_one_frame_from_its_own_time_window() -> None:
    events = [
        {"start_us": 0, "end_us": 4_000_000, "frame_times_us": [0, 1_600_000, 3_200_000]},
        {"start_us": 4_000_000, "end_us": 5_000_000, "frame_times_us": []},
        {"start_us": 5_000_000, "end_us": 6_000_000, "frame_times_us": [5_100_000]},
    ]
    assert select_reference_frame_times(events) == [1_600_000, 4_500_000, 5_100_000]


def test_missing_frame_cannot_be_presented_as_observed_visual_fact() -> None:
    events = [{"start_us": 0, "end_us": 1_000_000, "frame_times_us": []}]
    shots = [{"start_time": 0, "end_time": 1, "visual_description": "男主持说话", "narrative": "提出问题"}]
    notes = bind_reference_story(events, shots, [], [])
    assert notes[0]["frame_url"] is None
    assert notes[0]["visual_observation"] == ""
    assert notes[0]["note_status"] == "missing_frame"


def test_fallback_frame_time_is_kept_as_analysis_evidence() -> None:
    events = [{"start_us": 4_000_000, "end_us": 5_000_000, "frame_times_us": []}]
    shots = [{"start_time": 4, "end_time": 5, "visual_description": "女子做饭"}]
    notes = bind_reference_story(events, shots, [4_500_000], ["/fallback.png"])
    assert notes[0]["analysis_frame_time_us"] == 4_500_000
    assert notes[0]["frame_times_us"] == []
