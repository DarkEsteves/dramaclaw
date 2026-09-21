"""Speech adapter output is normalized before it becomes a remix artifact."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from novelvideo.freezone.remix.alignment import align_script, transcribe_media


class FakeSpeechAligner:
    def __init__(self, *, transcription: dict[str, Any], alignment: dict[str, Any] | None = None) -> None:
        self.transcription = transcription
        self.alignment = alignment if alignment is not None else transcription

    def transcribe(self, media_path: Path, language: str) -> dict[str, Any]:
        return self.transcription

    def align(self, media_path: Path, script: str, language: str) -> dict[str, Any]:
        return self.alignment


def test_transcribe_normalizes_unicode_character_spans_and_absent_confidence(tmp_path: Path) -> None:
    media = tmp_path / "voice.wav"
    media.write_bytes(b"synthetic narration")

    result = transcribe_media(
        media,
        "zh",
        aligner=FakeSpeechAligner(
            transcription={
                "status": "completed",
                "words": [
                    {"text": "你好", "start": 0.0, "end": 0.4},
                    {"text": "🙂", "start": 0.5, "end": 0.7, "confidence": None},
                ],
            }
        ),
    )

    assert result["status"] == "completed"
    assert result["media_hash"] == f"sha256:{hashlib.sha256(b'synthetic narration').hexdigest()}"
    assert result["transcript"] == "你好🙂"
    assert result["confidence_available"] is False
    assert result["words"] == [
        {
            "text": "你好",
            "start_char": 0,
            "end_char": 2,
            "start_us": 0,
            "end_us": 400_000,
            "confidence": None,
            "confidence_status": "unavailable",
        },
        {
            "text": "🙂",
            "start_char": 2,
            "end_char": 3,
            "start_us": 500_000,
            "end_us": 700_000,
            "confidence": None,
            "confidence_status": "unavailable",
        },
    ]


def test_transcribe_explicitly_returns_no_speech(tmp_path: Path) -> None:
    media = tmp_path / "silent.wav"
    media.write_bytes(b"silence")

    result = transcribe_media(
        media,
        "en",
        aligner=FakeSpeechAligner(transcription={"status": "no_speech", "words": []}),
    )

    assert result["status"] == "no_speech"
    assert result["transcript"] == ""
    assert result["words"] == []
    assert result["confidence_available"] is False


def test_align_script_marks_sequential_text_mismatch_without_guessing_repeated_words(tmp_path: Path) -> None:
    media = tmp_path / "voice.wav"
    media.write_bytes(b"voice")

    result = align_script(
        media,
        {"revision": 4, "language": "en", "dialogue": "go then go"},
        aligner=FakeSpeechAligner(
            transcription={"status": "completed", "words": []},
            alignment={
                "status": "completed",
                "words": [
                    {"text": "go", "start": 0.0, "end": 0.2},
                    {"text": "than", "start": 0.2, "end": 0.5},
                    {"text": "go", "start": 0.5, "end": 0.7},
                ],
            },
        ),
    )

    assert result["status"] == "script_mismatch"
    assert result["script_revision"] == 4
    assert result["words"] == []
    assert result["mismatch"] == {"reason": "aligned token does not match script at character 3"}


def test_align_script_uses_engine_character_spans_for_repeated_words(tmp_path: Path) -> None:
    media = tmp_path / "voice.wav"
    media.write_bytes(b"voice")

    result = align_script(
        media,
        {"revision": 4, "language": "en", "dialogue": "go then go"},
        aligner=FakeSpeechAligner(
            transcription={"status": "completed", "words": []},
            alignment={
                "status": "completed",
                "words": [
                    {"text": "go", "start_char": 0, "end_char": 2, "start_us": 0, "end_us": 200_000, "confidence": 0.9},
                    {"text": "then", "start_char": 3, "end_char": 7, "start_us": 200_000, "end_us": 500_000, "confidence": 0.85},
                    {"text": "go", "start_char": 8, "end_char": 10, "start_us": 500_000, "end_us": 700_000, "confidence": 0.8},
                ],
            },
        ),
    )

    assert result["status"] == "completed"
    assert [(word["start_char"], word["end_char"]) for word in result["words"]] == [(0, 2), (3, 7), (8, 10)]
    assert result["confidence_available"] is True


def test_align_script_skips_speaker_labels_and_maps_words_to_original_script(tmp_path: Path) -> None:
    media = tmp_path / "dialogue.wav"
    media.write_bytes(b"voice")

    class CheckingAligner(FakeSpeechAligner):
        def align(self, media_path: Path, script: str, language: str) -> dict[str, Any]:
            assert script == "你好？\n再见。"
            return self.alignment

    result = align_script(
        media,
        {"revision": 2, "language": "zh", "dialogue": "男：你好？\n女：再见。"},
        aligner=CheckingAligner(
            transcription={"status": "completed", "words": []},
            alignment={"status": "completed", "words": [
                {"text": "你好？", "start_char": 0, "end_char": 3, "start_us": 0, "end_us": 500_000},
                {"text": "再见。", "start_char": 4, "end_char": 7, "start_us": 600_000, "end_us": 1_000_000},
            ]},
        ),
    )
    assert result["status"] == "completed"
    assert result["transcript"] == "男：你好？\n女：再见。"
    assert [(word["start_char"], word["end_char"]) for word in result["words"]] == [(2, 5), (8, 11)]


def test_align_script_keeps_single_line_colon_as_spoken_text(tmp_path: Path) -> None:
    media = tmp_path / "narration.wav"
    media.write_bytes(b"voice")

    class CheckingAligner(FakeSpeechAligner):
        def align(self, media_path: Path, script: str, language: str) -> dict[str, Any]:
            assert script == "注意：保持安静。"
            return self.alignment

    result = align_script(
        media,
        {"revision": 1, "language": "zh", "dialogue": "注意：保持安静。"},
        aligner=CheckingAligner(
            transcription={"status": "completed", "words": []},
            alignment={"status": "completed", "words": [
                {"text": "注意", "start_char": 0, "end_char": 2, "start_us": 0, "end_us": 200_000},
            ]},
        ),
    )
    assert result["words"][0]["start_char"] == 0


def test_align_script_preserves_match_report_and_maps_script_ranges_after_role_prefixes(
    tmp_path: Path,
) -> None:
    media = tmp_path / "dialogue.wav"
    media.write_bytes(b"voice")
    report = {
        "normalized_script": "你好再见",
        "normalized_asr": "你号再见",
        "matched_characters": 3,
        "script_characters": 4,
        "character_coverage": 0.75,
        "required_script_covered": True,
        "substitutions": [{
            "script_range": {"text": "好", "start_char": 1, "end_char": 2, "normalized_start": 1, "normalized_end": 2},
            "asr_range": {"text": "号", "start_char": 1, "end_char": 2, "normalized_start": 1, "normalized_end": 2},
            "confidence": "low",
            "provenance": "asr_comparison",
        }],
        "insertions": [],
        "omissions": [],
    }

    result = align_script(
        media,
        {"revision": 2, "language": "zh", "dialogue": "男：你好\n女：再见"},
        aligner=FakeSpeechAligner(
            transcription={"status": "completed", "words": []},
            alignment={
                "status": "completed",
                "match_report": report,
                "words": [
                    {"text": "你", "start_char": 0, "end_char": 1, "start_us": 0, "end_us": 100_000},
                    {"text": "好", "start_char": 1, "end_char": 2, "start_us": 100_000, "end_us": 200_000},
                    {"text": "再", "start_char": 3, "end_char": 4, "start_us": 300_000, "end_us": 400_000},
                    {"text": "见", "start_char": 4, "end_char": 5, "start_us": 400_000, "end_us": 500_000},
                ],
            },
        ),
    )

    assert result["status"] == "completed"
    assert result["match_report"]["substitutions"][0]["script_range"] == {
        "text": "好",
        "start_char": 3,
        "end_char": 4,
        "normalized_start": 1,
        "normalized_end": 2,
    }
