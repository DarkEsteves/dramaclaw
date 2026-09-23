"""Compile accepted speaking and voiceover media into one exact remix clock."""

from __future__ import annotations

from typing import Any


def _floor_frame(us: int, fps: int) -> int:
    return us * fps // 1_000_000


def _ceil_frame(us: int, fps: int) -> int:
    return (us * fps + 999_999) // 1_000_000


def compile_take_timing(script: dict[str, Any], takes: list[dict[str, Any]], *, fps: int) -> dict[str, Any]:
    """Place one aligned, immutable media take per ordered script segment."""
    revision = script.get("revision")
    segments = script.get("segments")
    dialogue = script.get("dialogue")
    if type(fps) is not int or fps <= 0 or type(revision) is not int or revision < 1:
        raise ValueError("take timing requires a positive fps and script revision")
    if not isinstance(dialogue, str) or not isinstance(segments, list) or not segments:
        raise ValueError("take timing requires performance segments")
    if len(takes) != len(segments):
        raise ValueError("every performance segment must have one accepted take")

    placed: list[dict[str, Any]] = []
    global_words: list[dict[str, int]] = []
    cursor = 0
    for segment, take in zip(segments, takes, strict=True):
        segment_id = segment.get("segment_id")
        if take.get("segment_id") != segment_id:
            raise ValueError("accepted take order does not match the script")
        start_char, end_char = segment.get("start_char"), segment.get("end_char")
        if type(start_char) is not int or type(end_char) is not int or not 0 <= start_char < end_char <= len(dialogue):
            raise ValueError("take segment range is invalid")
        expected_text = dialogue[start_char:end_char]
        media_hash = take.get("media_hash")
        alignment = take.get("alignment")
        if not isinstance(alignment, dict) or alignment.get("media_hash") != media_hash:
            raise ValueError("take alignment media hash does not match accepted media")
        if alignment.get("status") != "completed" or alignment.get("script_revision") != revision or alignment.get("transcript") != expected_text:
            raise ValueError("take alignment does not match the script segment")
        duration_us = take.get("duration_us")
        if type(duration_us) is not int or duration_us <= 0:
            raise ValueError("accepted take duration is invalid")
        duration_frames = _floor_frame(duration_us, fps)
        if duration_frames < 1:
            raise ValueError("accepted take is shorter than one frame")
        cues: list[dict[str, Any]] = []
        previous_end = 0
        for word in alignment.get("words", []):
            word_start, word_end = word.get("start_char"), word.get("end_char")
            start_us, end_us = word.get("start_us"), word.get("end_us")
            if any(type(value) is not int for value in (word_start, word_end, start_us, end_us)) or not 0 <= word_start < word_end <= len(expected_text):
                raise ValueError("take word character range is invalid")
            if start_us < 0 or end_us <= start_us or end_us > duration_us + 500_000 // fps:
                raise ValueError("take word timing exceeds media duration")
            start_frame = max(previous_end, cursor + _floor_frame(start_us, fps))
            end_frame = min(cursor + duration_frames, cursor + _ceil_frame(end_us, fps))
            if start_frame >= end_frame:
                raise ValueError("take word timing is shorter than one output frame")
            cues.append({"text": word.get("text", ""), "start_char": word_start, "end_char": word_end,
                         "start_frame": start_frame, "end_frame": end_frame})
            global_words.append({"start_char": start_char + word_start, "end_char": start_char + word_end,
                                 "start_frame": start_frame, "end_frame": end_frame})
            previous_end = end_frame
        if not cues:
            raise ValueError("completed take alignment has no spoken words")
        placed.append({
            "segment_id": segment_id, "source": segment["source"], "speaker": segment["speaker"],
            "media_hash": media_hash, "start_frame": cursor, "end_frame": cursor + duration_frames,
            "caption": expected_text, "word_cues": cues,
        })
        cursor += duration_frames
        if cursor > fps * 60:
            raise ValueError("accepted takes exceed the 60 second remix output limit")

    selection_by_id = {row["selection_id"]: row for row in script.get("selections", [])}
    events: list[dict[str, Any]] = []
    unresolved: list[dict[str, str]] = []
    for event in script.get("events", []):
        selection = selection_by_id.get(event.get("selection_id"))
        matching = [word for word in global_words if selection and
                    word["start_char"] < selection.get("end_char", -1) and word["end_char"] > selection.get("start_char", -1)]
        if not matching:
            if event.get("required", False):
                unresolved.append({"event_id": str(event.get("event_id")), "reason": "event has no aligned spoken words"})
            continue
        events.append({"event_id": event["event_id"], "start_frame": min(word["start_frame"] for word in matching),
                       "end_frame": max(word["end_frame"] for word in matching)})
    return {"schema": "performance_timing.v2", "script_revision": revision, "fps": fps,
            "duration_frames": cursor, "takes": placed, "events": events,
            "unresolved_required_events": unresolved, "override_conflicts": []}
