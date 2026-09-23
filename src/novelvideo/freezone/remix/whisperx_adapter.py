"""Offline WhisperX implementation of the normalized speech adapter boundary."""

from __future__ import annotations

import unicodedata
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class NormalizedTextRange:
    """Dependency-free comparison range used inside the external worker."""

    text: str
    start_char: int
    end_char: int
    normalized_start: int
    normalized_end: int

    def __post_init__(self) -> None:
        if min(
            self.start_char,
            self.end_char,
            self.normalized_start,
            self.normalized_end,
        ) < 0 or self.end_char < self.start_char or self.normalized_end < self.normalized_start:
            raise ValueError("comparison ranges must be non-negative and ordered")

    def model_dump(self, **_options: Any) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SpeechDifference:
    script_range: NormalizedTextRange
    asr_range: NormalizedTextRange
    confidence: str = "low"
    provenance: str = "asr_comparison"


@dataclass(frozen=True)
class MatchReport:
    normalized_script: str
    normalized_asr: str
    matched_characters: int
    script_characters: int
    character_coverage: float
    required_script_covered: bool
    substitutions: list[SpeechDifference]
    insertions: list[SpeechDifference]
    omissions: list[SpeechDifference]
    provenance: str = "whisperx_asr_comparison"
    asr_is_ground_truth: bool = False

    def model_dump(self, **_options: Any) -> dict[str, Any]:
        return asdict(self)


MIN_SCRIPT_CHARACTER_COVERAGE = 0.8


def compare_spoken_script(script: str, asr: str) -> MatchReport:
    """Compare spoken characters while retaining offsets into both source strings.

    Punctuation, spacing, case and compatibility forms do not affect coverage.
    Differences are low confidence evidence because an ASR hypothesis is not an
    authoritative transcript of the recorded performance.
    """
    script_chars, script_offsets = _normalized_characters(script)
    asr_chars, asr_offsets = _normalized_characters(asr)
    normalized_script = "".join(script_chars)
    normalized_asr = "".join(asr_chars)
    substitutions: list[SpeechDifference] = []
    insertions: list[SpeechDifference] = []
    omissions: list[SpeechDifference] = []
    matched = 0
    for tag, script_start, script_end, asr_start, asr_end in SequenceMatcher(
        None, normalized_script, normalized_asr, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            matched += script_end - script_start
            continue
        if tag == "replace":
            shared = min(script_end - script_start, asr_end - asr_start)
            if shared:
                substitutions.append(_difference(
                    script, script_offsets, script_start, script_start + shared,
                    asr, asr_offsets, asr_start, asr_start + shared,
                ))
            if script_start + shared < script_end:
                omissions.append(_difference(
                    script, script_offsets, script_start + shared, script_end,
                    asr, asr_offsets, asr_start + shared, asr_start + shared,
                ))
            if asr_start + shared < asr_end:
                insertions.append(_difference(
                    script, script_offsets, script_start + shared, script_start + shared,
                    asr, asr_offsets, asr_start + shared, asr_end,
                ))
        elif tag == "insert":
            insertions.append(_difference(
                script, script_offsets, script_start, script_end,
                asr, asr_offsets, asr_start, asr_end,
            ))
        else:
            omissions.append(_difference(
                script, script_offsets, script_start, script_end,
                asr, asr_offsets, asr_start, asr_end,
            ))
    script_count = len(normalized_script)
    coverage = matched / script_count if script_count else float(not normalized_asr)
    return MatchReport(
        normalized_script=normalized_script,
        normalized_asr=normalized_asr,
        matched_characters=matched,
        script_characters=script_count,
        character_coverage=coverage,
        required_script_covered=not omissions,
        substitutions=substitutions,
        insertions=insertions,
        omissions=omissions,
    )


def _normalized_characters(text: str) -> tuple[list[str], list[tuple[int, int]]]:
    expanded: list[str] = []
    expanded_offsets: list[tuple[int, int]] = []
    for index, character in enumerate(text):
        for normalized in unicodedata.normalize("NFKC", character).casefold():
            expanded.append(normalized)
            expanded_offsets.append((index, index + 1))
    normalized_text = unicodedata.normalize("NFKC", text).casefold()
    normalized_offsets: list[tuple[int, int] | None] = [None] * len(normalized_text)
    for tag, source_start, source_end, target_start, target_end in SequenceMatcher(
        None, "".join(expanded), normalized_text, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            for offset in range(target_end - target_start):
                normalized_offsets[target_start + offset] = expanded_offsets[source_start + offset]
            continue
        if source_start < source_end:
            source_range = (
                expanded_offsets[source_start][0],
                expanded_offsets[source_end - 1][1],
            )
        else:
            anchor = (
                expanded_offsets[source_start][0]
                if source_start < len(expanded_offsets)
                else len(text)
            )
            source_range = (anchor, anchor)
        for target_index in range(target_start, target_end):
            normalized_offsets[target_index] = source_range
    characters: list[str] = []
    offsets: list[tuple[int, int]] = []
    for character, source_range in zip(normalized_text, normalized_offsets, strict=True):
        if character.isalnum():
            characters.append(character)
            offsets.append(source_range or (len(text), len(text)))
    return characters, offsets


def _difference(
    script: str,
    script_offsets: list[tuple[int, int]],
    script_start: int,
    script_end: int,
    asr: str,
    asr_offsets: list[tuple[int, int]],
    asr_start: int,
    asr_end: int,
) -> SpeechDifference:
    return SpeechDifference(
        script_range=_source_range(
            script, script_offsets, script_start, script_end
        ),
        asr_range=_source_range(asr, asr_offsets, asr_start, asr_end),
    )


def _source_range(
    source: str,
    offsets: list[tuple[int, int]],
    normalized_start: int,
    normalized_end: int,
) -> NormalizedTextRange:
    if normalized_start < normalized_end:
        start_char = offsets[normalized_start][0]
        end_char = offsets[normalized_end - 1][1]
    else:
        start_char = (
            offsets[normalized_start][0]
            if normalized_start < len(offsets)
            else len(source)
        )
        end_char = start_char
    return NormalizedTextRange(
        text=source[start_char:end_char],
        start_char=start_char,
        end_char=end_char,
        normalized_start=normalized_start,
        normalized_end=normalized_end,
    )


class WhisperXSpeechAligner:
    """Run local ASR with word timing, or character timing for a supplied script."""

    def __init__(
        self, *, model_cache: Path, model_name: str, device: str,
        compute_type: str, batch_size: int,
    ) -> None:
        if batch_size < 1:
            raise ValueError("WhisperX batch size must be positive")
        self.model_cache = model_cache
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.batch_size = batch_size

    def transcribe(self, media_path: Path, language: str) -> dict[str, Any]:
        whisperx, audio, segments, detected_language = self._recognize(media_path, language)
        text = "".join(str(row.get("text", "")) for row in segments)
        if not text.strip():
            return {"status": "no_speech", "words": []}
        result = self._align_words(whisperx, audio, text, detected_language, segments)
        result["language"] = detected_language
        return result

    def align(self, media_path: Path, script: str, language: str) -> dict[str, Any]:
        whisperx, audio, segments, detected_language = self._recognize(media_path, language)
        transcript = "".join(str(row.get("text", "")) for row in segments)
        if not transcript.strip():
            return {"status": "no_speech", "words": []}
        report = compare_spoken_script(script, transcript)
        serialized_report = report.model_dump(mode="json")
        if report.omissions:
            return {
                "status": "script_mismatch", "words": [],
                "mismatch": {
                    "reason": "ASR omitted required script text",
                    "report": serialized_report,
                },
            }
        if report.character_coverage < MIN_SCRIPT_CHARACTER_COVERAGE:
            return {
                "status": "script_mismatch", "words": [],
                "mismatch": {
                    "reason": "ASR character coverage below alignment threshold",
                    "minimum_character_coverage": MIN_SCRIPT_CHARACTER_COVERAGE,
                    "report": serialized_report,
                },
            }
        result = self._force_align_characters(
            whisperx, audio, script, detected_language, segments
        )
        result["language"] = detected_language
        result["match_report"] = serialized_report
        if result["status"] == "script_mismatch":
            result.setdefault("mismatch", {})["report"] = serialized_report
        return result

    def _recognize(self, media_path: Path, language: str):
        try:
            import whisperx
        except ImportError as error:
            raise RuntimeError("WhisperX is not installed") from error
        language_option = {} if language == "auto" else {"language": language}
        model = whisperx.load_model(
            self.model_name, self.device, compute_type=self.compute_type,
            download_root=str(self.model_cache), local_files_only=True,
            **language_option,
        )
        audio = whisperx.load_audio(str(media_path))
        result = model.transcribe(audio, batch_size=self.batch_size, **language_option)
        segments = result.get("segments", [])
        if not isinstance(segments, list):
            raise ValueError("WhisperX returned invalid transcription segments")
        detected_language = result.get("language") if language == "auto" else language
        if segments and (not isinstance(detected_language, str) or not detected_language):
            raise ValueError("WhisperX did not report the detected language")
        return whisperx, audio, segments, detected_language

    def _alignment_model(self, whisperx, language: str):
        return whisperx.load_align_model(
            language_code=language, device=self.device,
            model_dir=str(self.model_cache), model_cache_only=True,
        )

    def _align_words(self, whisperx, audio, text: str, language: str, segments: list[dict[str, Any]]) -> dict[str, Any]:
        model, metadata = self._alignment_model(whisperx, language)
        result = whisperx.align(segments, model, metadata, audio, self.device)
        words: list[dict[str, Any]] = []
        cursor = 0
        for segment in result.get("segments", []):
            for raw_word in segment.get("words", []):
                value = raw_word.get("word", raw_word.get("text", ""))
                token = value.strip() if isinstance(value, str) else ""
                if not token or "start" not in raw_word or "end" not in raw_word:
                    continue
                start, end = float(raw_word["start"]), float(raw_word["end"])
                if end <= start:
                    continue
                position = text.find(token, cursor)
                if position < 0:
                    return {
                        "status": "script_mismatch", "words": [],
                        "mismatch": {"reason": "aligned word does not match the transcript"},
                    }
                cursor = position + len(token)
                word: dict[str, Any] = {
                    "text": token, "start_char": position, "end_char": cursor,
                    "start": start, "end": end,
                }
                score = raw_word.get("score")
                if isinstance(score, (int, float)) and 0 <= score <= 1:
                    word["confidence"] = float(score)
                words.append(word)
        if not words:
            return {
                "status": "script_mismatch", "words": [],
                "mismatch": {"reason": "WhisperX found speech but produced no timed words"},
            }
        return {"status": "completed", "transcript": text, "words": words}

    def _force_align_characters(self, whisperx, audio, text: str, language: str, segments: list[dict[str, Any]]) -> dict[str, Any]:
        duration = max((float(row["end"]) for row in segments), default=0.0)
        if duration <= 0:
            return {"status": "no_speech", "words": []}
        model, metadata = self._alignment_model(whisperx, language)
        result = whisperx.align(
            [{"text": text, "start": 0.0, "end": duration}],
            model, metadata, audio, self.device, return_char_alignments=True,
        )
        chars = [char for segment in result.get("segments", []) for char in segment.get("chars", [])]
        if "".join(str(char.get("char", "")) for char in chars) != text:
            return {
                "status": "script_mismatch", "words": [],
                "mismatch": {"reason": "forced alignment did not preserve the script"},
            }
        words = []
        untimed_required: list[int] = []
        for index, char in enumerate(chars):
            letter = char["char"]
            if letter.isspace() or "start" not in char or "end" not in char:
                if _is_required_character(letter):
                    untimed_required.append(index)
                continue
            start, end = float(char["start"]), float(char["end"])
            if end <= start:
                if _is_required_character(letter):
                    untimed_required.append(index)
                continue
            words.append({
                "text": letter, "start_char": index, "end_char": index + 1,
                "start": start, "end": end,
            })
        if untimed_required:
            return {
                "status": "script_mismatch",
                "words": [],
                "mismatch": {
                    "reason": "forced alignment left required script characters untimed",
                    "untimed_script_ranges": _contiguous_ranges(
                        untimed_required, text
                    ),
                },
            }
        if not words:
            return {
                "status": "script_mismatch", "words": [],
                "mismatch": {"reason": "WhisperX found speech but produced no timed characters"},
            }
        return {"status": "completed", "transcript": text, "words": words}


def _is_required_character(value: object) -> bool:
    return any(
        character.isalnum()
        for character in unicodedata.normalize("NFKC", str(value))
    )


def _contiguous_ranges(
    indices: list[int], source: str
) -> list[dict[str, Any]]:
    ranges: list[dict[str, Any]] = []
    for index in indices:
        if ranges and ranges[-1]["end_char"] == index:
            ranges[-1]["end_char"] = index + 1
        else:
            ranges.append({"start_char": index, "end_char": index + 1})
    for item in ranges:
        item["text"] = source[item["start_char"]:item["end_char"]]
    return ranges
