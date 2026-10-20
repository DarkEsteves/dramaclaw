import pytest

from novelvideo.freezone.remix.speech_correction import preview_recognized_words_to_plan_cues


def test_corrected_words_follow_plan_turns_and_keep_exact_source_times():
    result = preview_recognized_words_to_plan_cues(
        words=[
            {"text": "黑", "start_us": 101_999, "end_us": 180_001},
            {"text": "咖啡", "start_us": 180_001, "end_us": 440_333},
            {"text": "好", "start_us": 2_100_011, "end_us": 2_250_009},
        ],
        turns=[{"id": "host", "text": "黑咖啡？"}, {"id": "guest", "text": "好。"}],
        source_duration_us=3_000_000,
    )
    assert result == [
        {"turn_id": "host", "text": "黑咖啡？", "source_start_us": 101_000,
         "source_end_us": 441_000, "words": [
             {"text": "黑", "start_us": 101_999, "end_us": 180_001},
             {"text": "咖啡", "start_us": 180_001, "end_us": 440_333},
         ]},
        {"turn_id": "guest", "text": "好。", "source_start_us": 2_100_000,
         "source_end_us": 2_251_000, "words": [
             {"text": "好", "start_us": 2_100_011, "end_us": 2_250_009},
         ]},
    ]


def test_corrected_words_reject_word_crossing_turn_boundary():
    with pytest.raises(ValueError, match="crosses turn boundary"):
        preview_recognized_words_to_plan_cues(
            words=[{"text": "黑咖啡", "start_us": 100_000, "end_us": 300_000}],
            turns=[{"id": "a", "text": "黑"}, {"id": "b", "text": "咖啡"}],
            source_duration_us=1_000_000,
        )


def test_corrected_words_reject_extra_words_and_out_of_source_bounds():
    with pytest.raises(ValueError, match="extra recognized words"):
        preview_recognized_words_to_plan_cues(
            words=[{"text": "好", "start_us": 1, "end_us": 100_000},
                   {"text": "呀", "start_us": 100_000, "end_us": 200_000}],
            turns=[{"id": "a", "text": "好"}], source_duration_us=1_000_000,
        )
    with pytest.raises(ValueError, match="outside source range"):
        preview_recognized_words_to_plan_cues(
            words=[{"text": "好", "start_us": 900_000, "end_us": 1_100_000}],
            turns=[{"id": "a", "text": "好"}], source_duration_us=1_000_000,
        )
