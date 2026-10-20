"""Deterministic defaults for a newly generated, unbound production draft only."""

from __future__ import annotations

from copy import deepcopy
from math import ceil
from typing import Any

from .production_plan_v2 import PLACEMENT_ROLES, normalize_draft_subtitle_positions


def initialize_draft_timing(payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Fill missing durations only on new, auto-timed spoken model drafts."""
    from .production_plan_diagnostics import measure_plan_speech

    result = deepcopy(payload)
    output = result.get("output") or {}
    rate = output.get("fps_num", 0) / output.get("fps_den", 1) if output.get("fps_den") else 0
    if not rate:
        return result, []  # Output validation reports the exact missing or invalid field.
    estimates = {row["entity_id"]: row["estimate"] for row in measure_plan_speech(result)
                 if row["entity_kind"] == "beat"}
    changes: list[dict[str, str]] = []
    for index, beat in enumerate(result.get("beats") or []):
        if not isinstance(beat, dict) or "duration_frames" in beat:
            continue
        estimate = estimates.get(beat.get("id"), {}).get("estimated_seconds")
        if beat.get("timing_mode", "auto") != "auto" or estimate is None:
            raise ValueError(f"/beats/{index}/duration_frames requires an authored duration")
        beat["duration_frames"] = max(1, ceil(estimate * rate))
        changes.append({"path": f"/beats/{index}/duration_frames",
                        "code": "local_speech_duration_initialized"})
    return result, changes


def discard_extra_draft_fields(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Ignore model-only additions without repairing known fields or saved data."""
    from .production_plan_v2 import production_plan_draft_json_schema

    schema = production_plan_draft_json_schema()
    changes: list[dict[str, str]] = []

    def project(value: Any, fragment: dict[str, Any], path: str) -> Any:
        if "$ref" in fragment:
            fragment = schema["$defs"][fragment["$ref"].rsplit("/", 1)[-1]]
        # Optional objects use an object/null union. Select only by shape;
        # invalid scalar types must survive for the existing validator.
        if "anyOf" in fragment:
            for branch in fragment["anyOf"]:
                resolved = (schema["$defs"][branch["$ref"].rsplit("/", 1)[-1]]
                            if "$ref" in branch else branch)
                if (isinstance(value, dict) and resolved.get("type") == "object"
                        or isinstance(value, list) and resolved.get("type") == "array"):
                    return project(value, resolved, path)
            return deepcopy(value)
        if isinstance(value, dict) and fragment.get("type") == "object":
            properties = fragment.get("properties", {})
            closed = fragment.get("additionalProperties") is False
            if closed and value.keys() - properties.keys():
                # Never log model-supplied key names or values.
                changes.append({"path": path or "/", "code": "extra_draft_fields_ignored"})
            return {key: project(item, properties.get(key, {}), f"{path}/{key}")
                    for key, item in value.items() if not closed or key in properties}
        if isinstance(value, list) and fragment.get("type") == "array":
            return [project(item, fragment.get("items", {}), f"{path}/{index}")
                    for index, item in enumerate(value)]
        return deepcopy(value)

    return project(payload, schema, ""), changes


def _beat_durations(payload: dict[str, Any]) -> dict[str, int]:
    durations: dict[str, int] = {}
    duplicates: set[str] = set()
    beats = payload.get("beats")
    if not isinstance(beats, list):
        return durations
    for beat in beats:
        if not isinstance(beat, dict):
            continue
        beat_id, duration = beat.get("id"), beat.get("duration_frames")
        if not isinstance(beat_id, str) or not beat_id or type(duration) is not int or duration <= 0:
            continue
        if beat_id in durations:
            duplicates.add(beat_id)
        durations[beat_id] = duration
    for beat_id in duplicates:
        durations.pop(beat_id, None)
    return durations


def _default_beat_anchor(anchor: Any, durations: dict[str, int]) -> bool:
    if not isinstance(anchor, dict) or anchor.get("kind") != "beat" or anchor.get("edge") is not None:
        return False
    beat_id, offset = anchor.get("ref_id"), anchor.get("offset_frames")
    duration = durations.get(beat_id) if isinstance(beat_id, str) else None
    if (duration is None or "offset_frames" not in anchor or type(offset) is not int
            or not 0 <= offset <= duration):
        return False
    if any(anchor.get(field) is not None for field in ("frame", "char_offset", "text_hash", "affinity")):
        return False
    if set(anchor) - {"kind", "ref_id", "edge", "offset_frames", "frame",
                       "char_offset", "text_hash", "affinity"}:
        return False
    anchor["edge"] = "start"
    return True


def _subtitle_has_content(row: dict[str, Any], turn_ids: set[str]) -> bool:
    text, turn_id = row.get("text"), row.get("turn_id")
    return (isinstance(text, str) and bool(text.strip())) or (
        isinstance(turn_id, str) and turn_id in turn_ids
    )


def _safe_estimated_beat(payload: dict[str, Any], beat: dict[str, Any], *, has_absolute_anchor: bool) -> bool:
    duration, turns = beat.get("duration_frames"), beat.get("turns")
    if (beat.get("timing_mode", "auto") != "auto" or has_absolute_anchor
            or type(duration) is not int or not isinstance(turns, list)
            or not turns or duration < len(turns)):
        return False
    if payload.get("resources") or payload.get("speech_takes") or payload.get("speech_cues"):
        return False
    overflow = False
    for turn in turns:
        if not isinstance(turn, dict) or turn.get("timing_basis", "estimated") != "estimated":
            return False
        if turn.get("words") or turn.get("speech_timing"):
            return False
        start, end = turn.get("start_frame"), turn.get("end_frame")
        if start is None and end is None:
            continue
        if (type(start) is not int or type(end) is not int
                or start < 0 or end <= start):
            return False
        overflow |= end > duration
    return overflow


def normalize_production_plan_draft(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Return an authorable plan and metadata-only normalization records.

    This entry point is for a fresh model draft. Persisted plans, user edits and
    measured source timing must go through their existing strict validators.
    """
    result = deepcopy(payload)
    changes: list[dict[str, str]] = []
    durations = _beat_durations(result)
    beats, placements, selections = result.get("beats"), result.get("placements"), result.get("selections")
    if not isinstance(placements, list):
        placements = []
    if not isinstance(selections, list):
        selections = []
    turns = [turn for beat in beats if isinstance(beat, dict)
             for turn in (beat.get("turns") if isinstance(beat.get("turns"), list) else [])
             if isinstance(turn, dict)] if isinstance(beats, list) else []
    turn_ids = {turn["id"] for turn in turns if isinstance(turn.get("id"), str)}
    known_roles = frozenset().union(*PLACEMENT_ROLES.values())

    for label, rows in (("placements", placements), ("selections", selections)):
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            identity = {"placement_id": row["id"]} if label == "placements" and isinstance(row.get("id"), str) else {}
            for edge in ("start", "end"):
                if _default_beat_anchor(row.get(edge), durations):
                    changes.append({"path": f"/{label}/{index}/{edge}",
                                    "code": "beat_anchor_start_defaulted", **identity})
            if label != "placements" or row.get("kind") != "subtitle":
                continue
            role = row.get("role")
            if (role == "dialogue" or (role is not None and
                                        (not isinstance(role, str) or role not in known_roles))
                    or not _subtitle_has_content(row, turn_ids)):
                continue
            if any(row.get(key) is not None for key in (
                "resource_id", "graphic", "source_audio", "speech_take_id",
                "source_in_us", "source_out_us",
            )):
                continue
            row["role"] = "dialogue"
            changes.append({"path": f"/placements/{index}/role", "code": "subtitle_role_defaulted",
                            **identity})

    all_rows = [row for row in [*placements, *selections] if isinstance(row, dict)]
    has_absolute_anchor = any(
        isinstance(row.get(edge), dict) and row[edge].get("kind") == "program_frame"
        for row in all_rows for edge in ("start", "end")
    )
    if isinstance(beats, list):
        for beat_index, beat in enumerate(beats):
            if not isinstance(beat, dict) or not _safe_estimated_beat(result, beat, has_absolute_anchor=has_absolute_anchor):
                continue
            for turn_index, turn in enumerate(beat["turns"]):
                if turn.get("start_frame") is None and turn.get("end_frame") is None:
                    continue
                turn.pop("start_frame", None)
                turn.pop("end_frame", None)
                turn["timing_basis"] = "estimated"
                changes.append({"path": f"/beats/{beat_index}/turns/{turn_index}",
                                "code": "estimated_turn_bounds_cleared",
                                **({"beat_id": beat["id"]} if isinstance(beat.get("id"), str) else {}),
                                **({"turn_id": turn["id"]} if isinstance(turn.get("id"), str) else {})})

    before_positions = result
    result, defaulted_positions = normalize_draft_subtitle_positions(result)
    if defaulted_positions:
        before_rows = before_positions.get("placements")
        after_rows = result.get("placements")
        for index, (before, after) in enumerate(zip(before_rows, after_rows)):
            if before == after or not isinstance(after, dict):
                continue
            changes.append({"path": f"/placements/{index}", "code": "subtitle_position_defaulted",
                            **({"placement_id": after["id"]} if isinstance(after.get("id"), str) else {})})
    return result, changes
