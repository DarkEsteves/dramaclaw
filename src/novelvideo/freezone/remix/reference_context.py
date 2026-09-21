"""Pair visual evidence and speech on the original media clock."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .store import RemixArtifactStore


def load_reference_events(
    project_dir: Path, project_id: str, video_path: Path,
    evidence_ref: dict[str, Any], transcript_ref: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Resolve exact project-owned evidence and check both artifacts against the video."""
    store = RemixArtifactStore(project_dir, project_id)
    evidence = store.read(evidence_ref["artifact_id"], evidence_ref["revision"])
    if evidence["schema"] != "reference_evidence.v1":
        raise ValueError("reference evidence has an invalid schema")
    transcript = None
    if transcript_ref is not None:
        artifact = store.read(transcript_ref["artifact_id"], transcript_ref["revision"])
        if artifact["schema"] != "word_timeline.v1":
            raise ValueError("reference transcript has an invalid schema")
        transcript = artifact["payload"]
    with video_path.open("rb") as source:
        digest = f"sha256:{hashlib.file_digest(source, 'sha256').hexdigest()}"
    if evidence["payload"].get("media_hash") != digest:
        raise ValueError("reference evidence does not match the source video")
    if transcript is not None and transcript.get("media_hash") != digest:
        raise ValueError("reference transcript does not match the source video")
    return build_reference_events(evidence["payload"], transcript)


def build_reference_events(
    evidence: dict[str, Any], transcript: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    duration = evidence.get("duration_us")
    if type(duration) is not int or duration <= 0:
        raise ValueError("reference evidence has no valid duration")
    boundaries = {0, duration}
    for cut in evidence.get("cuts", []):
        time_us = cut.get("time_us") if isinstance(cut, dict) else None
        if type(time_us) is int and 0 < time_us < duration:
            boundaries.add(time_us)
    times = sorted(boundaries)
    # The vision prompt accepts at most 60 source-clock windows. Keep the
    # original first/last boundary and evenly merge excess scene cuts.
    if len(times) > 61:
        times = [times[index * (len(times) - 1) // 60] for index in range(61)]
    frames = evidence.get("representative_frames", [])
    evidence_time_base = evidence.get("time_base")
    words = transcript.get("words", []) if transcript and transcript.get("status") == "completed" else []
    source_text = transcript.get("transcript") if transcript else None
    events: list[dict[str, Any]] = []
    for start, end in zip(times, times[1:]):
        selected = [
            word for word in words
            if isinstance(word, dict)
            and type(word.get("start_us")) is int
            and type(word.get("end_us")) is int
            and start <= (word["start_us"] + word["end_us"]) // 2 < end
        ]
        spoken_text = _spoken_text(selected, source_text)
        frame_references = [
            {
                "pts": frame["pts"],
                "time_base": frame.get("time_base", evidence_time_base),
                "time_us": frame["time_us"],
            }
            for frame in frames
            if isinstance(frame, dict)
            and type(frame.get("time_us")) is int
            and start <= frame["time_us"] < end
            and type(frame.get("pts")) is int
        ]
        events.append({
            "start_us": start,
            "end_us": end,
            "frame_pts": [
                frame["pts"] for frame in frames
                if isinstance(frame, dict)
                and type(frame.get("time_us")) is int
                and start <= frame["time_us"] < end
                and type(frame.get("pts")) is int
            ],
            "frame_times_us": [
                frame["time_us"] for frame in frames
                if isinstance(frame, dict)
                and type(frame.get("time_us")) is int
                and start <= frame["time_us"] < end
                and type(frame.get("pts")) is int
            ],
            "frame_references": frame_references,
            "spoken_text": spoken_text,
            "spoken_range": _spoken_range(selected, spoken_text),
            "word_spans": [
                {"text": word["text"], "start_us": word["start_us"], "end_us": word["end_us"]}
                for word in selected
            ],
        })
    return events


def _spoken_range(words: list[dict[str, Any]], text: str) -> dict[str, Any] | None:
    if not words:
        return None
    first, last = words[0], words[-1]
    return {
        "start_us": first["start_us"],
        "end_us": last["end_us"],
        "start_char": first.get("start_char") if type(first.get("start_char")) is int else None,
        "end_char": last.get("end_char") if type(last.get("end_char")) is int else None,
        "text": text,
    }


def _spoken_text(words: list[dict[str, Any]], source_text: object) -> str:
    if not words:
        return ""
    first, last = words[0], words[-1]
    if (
        isinstance(source_text, str)
        and type(first.get("start_char")) is int
        and type(last.get("end_char")) is int
        and 0 <= first["start_char"] < last["end_char"] <= len(source_text)
    ):
        return source_text[first["start_char"]:last["end_char"]].strip()
    result = ""
    for word in words:
        text = str(word.get("text", ""))
        if result and result[-1:].isascii() and result[-1:].isalnum() and text[:1].isascii() and text[:1].isalnum():
            result += " "
        result += text
    return result


def select_reference_frame_times(events: list[dict[str, Any]]) -> list[int]:
    """Select one source-clock frame inside each event, including sparse events."""
    selected: list[int] = []
    for event in events:
        start, end = event["start_us"], event["end_us"]
        midpoint = (start + end) // 2
        candidates = [
            time for time in event.get("frame_times_us", [])
            if type(time) is int and start <= time < end
        ]
        selected.append(min(candidates, key=lambda time: (abs(time - midpoint), time)) if candidates else midpoint)
    return selected


def normalize_reference_shots(shots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build factual notes from explicit still-frame fields, never free-form motion prose."""
    normalized: list[dict[str, Any]] = []
    for shot in shots:
        if not isinstance(shot, dict):
            normalized.append({})
            continue
        parts = []
        for field, label in (("visible_subject", "人物"), ("visible_setting", "环境")):
            value = shot.get(field)
            if isinstance(value, str) and value.strip():
                parts.append(f"{label}：{value.strip()}")
        objects = shot.get("visible_objects")
        if isinstance(objects, list):
            names = [value.strip() for value in objects if isinstance(value, str) and value.strip()]
            if names:
                parts.append(f"物品：{'、'.join(names)}")
        visible_text = shot.get("visible_text")
        visible_text = visible_text.strip() if isinstance(visible_text, str) else ""
        if visible_text:
            parts.append(f"画面文字：{visible_text}")
        normalized.append({**shot, "visible_text": visible_text, "visual_description": "；".join(parts)})
    return normalized


def _transcript_warnings(visible_text: str, spoken_text: str) -> list[dict[str, str]]:
    caption_words = re.findall(r"[A-Za-z]{2,}", visible_text)
    spoken_words = re.findall(r"[A-Za-z]{2,}", spoken_text)
    spoken_counts = Counter(word.casefold() for word in spoken_words)
    if sum(min(count, spoken_counts[word]) for word, count in Counter(word.casefold() for word in caption_words).items()) < 2:
        return []
    warnings = []
    for caption in caption_words:
        if len(caption) < 6 or caption.casefold() in spoken_counts:
            continue
        candidates = [
            (SequenceMatcher(None, caption.casefold(), word.casefold()).ratio(), word)
            for word in spoken_words if len(word) >= 6
        ]
        if candidates:
            score, word = max(candidates)
            if 0.68 <= score < 1:
                warnings.append({"transcript_word": word, "caption_word": caption})
    return warnings


def bind_reference_story(
    events: list[dict[str, Any]], shots: list[dict[str, Any]],
    frame_times_us: list[int], frame_urls: list[str],
) -> list[dict[str, Any]]:
    """Keep factual media time boundaries while attaching reviewable model notes."""
    notes: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        candidate = shots[index] if len(shots) == len(events) and isinstance(shots[index], dict) else {}
        try:
            aligned = (
                abs(float(candidate["start_time"]) - event["start_us"] / 1_000_000) <= 0.5
                and abs(float(candidate["end_time"]) - event["end_us"] / 1_000_000) <= 0.5
            )
        except (KeyError, TypeError, ValueError):
            aligned = False
        shot = candidate if aligned else {}
        midpoint = (event["start_us"] + event["end_us"]) // 2
        candidates = [
            (abs(time - midpoint), time, url)
            for time, url in zip(frame_times_us, frame_urls)
            if event["start_us"] <= time < event["end_us"]
        ]
        selected_frame = min(candidates) if candidates else None
        frame_url = selected_frame[2] if selected_frame else None
        frame_reference = next((
            frame for frame in event.get("frame_references", [])
            if isinstance(frame, dict) and frame.get("time_us") == (selected_frame[1] if selected_frame else None)
        ), None)
        observed_frame = ({
            "pts": frame_reference.get("pts") if frame_reference else None,
            "time_base": frame_reference.get("time_base") if frame_reference else event.get("time_base"),
            "time_us": selected_frame[1],
            "url": frame_url,
        } if selected_frame else None)
        notes.append({
            **event,
            "frame_url": frame_url,
            "analysis_frame_time_us": selected_frame[1] if selected_frame else None,
            "observed_frame": observed_frame,
            "visible_text": shot.get("visible_text", "") if frame_url else "",
            "transcript_warnings": _transcript_warnings(
                str(shot.get("visible_text", "")), str(event.get("spoken_text", ""))
            ) if frame_url else [],
            "visual_observation": shot.get("visual_description", "") if frame_url else "",
            "narrative_inference": shot.get("narrative", "") if frame_url else "",
            "note_status": "needs_review" if frame_url else "missing_frame",
        })
    return notes
