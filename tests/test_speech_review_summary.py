from copy import deepcopy

import pytest


@pytest.mark.parametrize(
    "target,observed,expected",
    [
        ("黑咖啡，没有之一。", "黑咖啡，没有之一。", "exact"),
        ("黑咖啡，没有之一。", "黑咖啡没有之一", "normalized_match"),
        ("黑咖啡，没有之一。", "黑咖啡", "missing"),
        ("黑咖啡", "黑咖啡黑咖啡", "extra"),
        ("黑咖啡", "白咖啡", "review_required"),
        ("NEXT", "next", "normalized_match"),
        ("黑咖啡", "黑咖啡还有红茶", "extra"),
        ("", "你好", "extra"),
        ("你好", "", "missing"),
        ("", "", "exact"),
    ],
)
def test_classify_differences_without_fuzzy_approval(target, observed, expected):
    from novelvideo.freezone.remix.speech_review_summary import classify_speech_difference

    assert classify_speech_difference(target, observed) == expected


def test_summary_preserves_observed_tokens_and_separates_listening():
    from novelvideo.freezone.remix.speech_review_summary import build_speech_review_summary

    observation = {
        "status": "completed",
        "timing_granularity": "word",
        "source_duration_us": 4_100_000,
        "recognized_words": [
            {"id": "w1", "text": "黑咖啡", "start_us": 0, "end_us": 1_000_000},
            {"id": "w2", "text": "没有", "start_us": 1_300_000, "end_us": 2_000_000},
            {"id": "w3", "text": "之一", "start_us": 2_100_000, "end_us": 3_560_000},
        ],
        "recognized_segments": [],
    }
    before = deepcopy(observation)
    turns = [{"id": "guest", "speaker": "嘉宾", "text": "黑咖啡，没有之一。"}]
    summary = build_speech_review_summary(
        observation,
        target_turns=turns,
        observation_ref={"artifact_id": "observed", "revision": 1},
    )
    assert observation == before
    assert summary["transcript_match"] == "normalized_match"
    assert summary["audio_review"] == "unavailable"
    assert summary["lip_review"] == "unavailable"
    assert summary["recognized_words"] == before["recognized_words"]
    assert summary["source_duration_us"] == 4_100_000
    assert summary["differences"] == []
    assert summary["target_turns"] == turns


def test_segment_observation_does_not_claim_word_timing():
    from novelvideo.freezone.remix.speech_review_summary import build_speech_review_summary

    summary = build_speech_review_summary(
        {"status": "completed", "timing_granularity": "segment",
         "source_duration_us": 4_100_000, "recognized_words": [],
         "recognized_segments": [{"text": "黑咖啡没有之一", "start_us": 0, "end_us": 3_560_000}]},
        target_turns=[{"id": "guest", "text": "黑咖啡，没有之一。"}],
        observation_ref={"artifact_id": "segments", "revision": 1},
    )
    assert summary["timing_granularity"] == "segment"
    assert summary["recognized_words"] == []
    assert summary["word_timing_available"] is False
    assert summary["transcript_match"] == "normalized_match"


def test_changed_target_invalidates_mapping_not_observation():
    from novelvideo.freezone.remix.speech_review_summary import build_speech_review_summary

    observation = {"status": "completed", "timing_granularity": "word",
                   "source_duration_us": 1_000_000,
                   "recognized_words": [{"id": "w", "text": "黑咖啡", "start_us": 0, "end_us": 900_000}],
                   "recognized_segments": []}
    ref = {"artifact_id": "source", "revision": 1}
    first = build_speech_review_summary(observation, observation_ref=ref,
                                        target_turns=[{"id": "t", "text": "黑咖啡"}])
    changed = build_speech_review_summary(observation, observation_ref=ref,
                                          target_turns=[{"id": "t", "text": "红茶"}])
    assert first["target_signature"] != changed["target_signature"]
    assert first["observation_ref"] == changed["observation_ref"]
    assert changed["transcript_match"] == "review_required"


def test_target_signature_ignores_resolved_timing_fields():
    from novelvideo.freezone.remix.speech_review_summary import build_speech_review_summary
    observation = {"status": "completed", "recognized_words": [], "recognized_segments": []}
    kwargs = {"observation_ref": {"artifact_id": "source", "revision": 1}}
    before = build_speech_review_summary(observation,
        target_turns=[{"id": "t", "speaker": "嘉宾", "text": "黑咖啡", "start_frame": 0}], **kwargs)
    after = build_speech_review_summary(observation,
        target_turns=[{"id": "t", "speaker": "嘉宾", "text": "黑咖啡", "start_frame": 180}], **kwargs)
    assert before["target_signature"] == after["target_signature"]


def test_diff_marks_known_source_range_without_inventing_token_times():
    from novelvideo.freezone.remix.speech_review_summary import build_speech_review_summary

    summary = build_speech_review_summary(
        {"status": "completed", "timing_granularity": "word", "source_duration_us": 6_000_000,
         "recognized_words": [{"id": "joined", "text": "黑咖啡还有红茶", "start_us": 0, "end_us": 5_000_000}],
         "recognized_segments": []},
        observation_ref={"artifact_id": "source", "revision": 1},
        target_turns=[{"id": "t", "text": "黑咖啡"}],
    )
    extra = summary["differences"][0]
    assert extra["kind"] == "extra"
    assert extra["observed_text"] == "还有红茶"
    assert extra["source_word_ids"] == ["joined"]
    assert extra["start_us"] == 0 and extra["end_us"] == 5_000_000


@pytest.mark.parametrize("field,value", [("audio_review", "guessed"), ("lip_review", "guessed")])
def test_unknown_review_states_are_rejected(field, value):
    from novelvideo.freezone.remix.speech_review_summary import build_speech_review_summary

    with pytest.raises(ValueError, match="review status"):
        build_speech_review_summary(
            {"status": "completed", "timing_granularity": "word", "recognized_words": []},
            observation_ref={"artifact_id": "source", "revision": 1},
            target_turns=[], **{field: value},
        )
