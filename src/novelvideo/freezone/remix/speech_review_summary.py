"""Compare immutable source recognition with intended dialogue for review."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any

from .speech_comparison import compare_spoken_script

_REVIEW_STATES = frozenset({"unavailable", "pending", "passed", "rejected"})


def classify_speech_difference(target: str, observed: str) -> str:
    if not isinstance(target, str) or not isinstance(observed, str):
        raise ValueError("speech text must be a string")
    if max(len(target), len(observed)) > 20_000:
        raise ValueError("speech review text exceeds its bounded limit")
    if target == observed:
        return "exact"
    report = compare_spoken_script(target, observed)
    if report.normalized_script == report.normalized_asr:
        return "normalized_match"
    if report.omissions and not report.substitutions and not report.insertions:
        return "missing"
    if report.insertions and not report.substitutions and not report.omissions:
        return "extra"
    return "review_required"


def _text(rows: list[dict[str, Any]], *, separator: str = "") -> str:
    if any(not isinstance(row.get("text"), str) for row in rows):
        raise ValueError("speech review rows require text")
    return separator.join(row["text"] for row in rows)


def build_speech_review_summary(
    observation: dict[str, Any], *, target_turns: list[dict[str, Any]],
    observation_ref: dict[str, Any], audio_review: str = "unavailable",
    lip_review: str = "unavailable", correction_preview_ref: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build display facts; transcript similarity never certifies listening."""
    if audio_review not in _REVIEW_STATES or lip_review not in _REVIEW_STATES:
        raise ValueError("unknown speech review status")
    if (not isinstance(observation_ref.get("artifact_id"), str)
            or type(observation_ref.get("revision")) is not int
            or observation_ref["revision"] < 1):
        raise ValueError("exact observation reference required")
    words = observation.get("recognized_words") or []
    segments = observation.get("recognized_segments") or []
    if not isinstance(words, list) or not isinstance(segments, list):
        raise ValueError("speech observations must be lists")
    granularity = observation.get("timing_granularity", "word" if words else "segment" if segments else "none")
    if granularity not in {"word", "segment", "none"}:
        raise ValueError("invalid timing granularity")
    target = _text(target_turns, separator="\n")
    rows = words if granularity == "word" else segments
    observed = _text(rows)
    match = classify_speech_difference(target, observed)
    report = compare_spoken_script(target, observed)
    offsets: list[tuple[int, int, dict]] = []
    offset = 0
    for row in rows:
        offsets.append((offset, offset + len(row["text"]), row))
        offset += len(row["text"])
    differences = []
    for kind, items in (("replacement", report.substitutions),
                        ("extra", report.insertions), ("missing", report.omissions)):
        for item in items:
            source = [row for start, end, row in offsets
                      if start < item.asr_range.end_char and end > item.asr_range.start_char]
            timed = source and all(
                type(row.get("start_us")) is int and type(row.get("end_us")) is int
                and 0 <= row["start_us"] < row["end_us"] for row in source)
            differences.append({
                "kind": kind,
                "target_text": item.script_range.text,
                "observed_text": item.asr_range.text,
                "target_range": {"start_char": item.script_range.start_char,
                                 "end_char": item.script_range.end_char},
                "observed_range": {"start_char": item.asr_range.start_char,
                                   "end_char": item.asr_range.end_char},
                "source_word_ids": [row["id"] for row in source if granularity == "word" and row.get("id")],
                "start_us": min(row["start_us"] for row in source) if timed else None,
                "end_us": max(row["end_us"] for row in source) if timed else None,
                "provenance": "asr_comparison",
                "requires_review": True,
            })
    target_content = [{"id": row.get("id"), "speaker": row.get("speaker") or "", "text": row["text"]}
                      for row in target_turns]
    signature = hashlib.sha256(json.dumps(target_content, ensure_ascii=False,
                                          sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    word_timing = bool(words) and granularity == "word" and all(
        type(row.get("start_us")) is int and type(row.get("end_us")) is int
        and 0 <= row["start_us"] < row["end_us"] for row in words)
    return {
        "schema_version": "speech_review_summary.v1",
        "observation_ref": deepcopy(observation_ref),
        "observation_status": observation.get("status"),
        "target_signature": signature,
        "target_turns": deepcopy(target_turns),
        "target_text": target,
        "recognized_text": observed,
        "recognized_words": deepcopy(words),
        "recognized_segments": deepcopy(segments),
        "source_duration_us": observation.get("source_duration_us"),
        "timing_granularity": granularity,
        "word_timing_available": word_timing,
        "transcript_match": match,
        "differences": differences,
        "audio_review": audio_review,
        "lip_review": lip_review,
        "correction_preview_ref": deepcopy(correction_preview_ref),
    }
