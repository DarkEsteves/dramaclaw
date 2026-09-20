"""Video and transcript evidence share one source timeline."""

import hashlib

from novelvideo.freezone.remix.reference_context import build_reference_events
from novelvideo.freezone.remix.reference_context import load_reference_events
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
            "spoken_text": "hello",
            "word_spans": [{"text": "hello", "start_us": 300_000, "end_us": 700_000}],
        },
        {
            "start_us": 2_000_000,
            "end_us": 4_000_000,
            "frame_pts": [2],
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
