"""Pair visual evidence and speech on the original media clock."""

from __future__ import annotations

import hashlib
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
    frames = evidence.get("representative_frames", [])
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
            "spoken_text": spoken_text,
            "word_spans": [
                {"text": word["text"], "start_us": word["start_us"], "end_us": word["end_us"]}
                for word in selected
            ],
        })
    return events


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
