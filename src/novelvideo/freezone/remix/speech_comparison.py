"""Provider-neutral comparison of spoken script and ASR text."""

from __future__ import annotations

import unicodedata
import re
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from threading import local
from typing import Any

from opencc import OpenCC


_CONVERTER_CONTEXT = local()
_HAN = re.compile(r"[\u3400-\u9fff]")


def _simplify_chinese(text: str) -> str:
    if not _HAN.search(text):
        return text
    converter = getattr(_CONVERTER_CONTEXT, "converter", None)
    if converter is None:
        converter = OpenCC("t2s.json")
        _CONVERTER_CONTEXT.converter = converter
    return converter.convert(text)


@dataclass(frozen=True)
class NormalizedTextRange:
    """Dependency-free comparison range shared by speech adapters."""

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
    provenance: str = "newapi_asr_comparison"
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
        for normalized in unicodedata.normalize("NFKC", _simplify_chinese(character)).casefold():
            expanded.append(normalized)
            expanded_offsets.append((index, index + 1))
    normalized_text = unicodedata.normalize("NFKC", _simplify_chinese(text)).casefold()
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
