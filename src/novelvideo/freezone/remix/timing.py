"""Pure, deterministic timing compilation for reference-video remixes.

The compiler deliberately accepts plain dictionaries at this boundary.  Task 2's
artifact models can validate and persist the result without making the timing
rules depend on storage or Pydantic.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


MICROSECONDS_PER_SECOND = 1_000_000


class TimingCompileError(ValueError):
    """Raised when timing inputs cannot describe one unambiguous compilation."""


class _IncompleteAlignment(TimingCompileError):
    """Internal signal used to report a semantic event as unresolved."""


def map_source_interval_to_frames(
    *,
    start_us: int,
    end_us: int,
    source_start_us: int,
    timeline_start_us: int,
    speed_num: int,
    speed_den: int,
    fps: int,
) -> tuple[int, int]:
    """Map a source half-open microsecond interval to output half-open frames.

    ``speed_num / speed_den`` is the positive playback-speed ratio.  The
    returned start uses floor rounding and the returned end uses ceiling
    rounding, so even a subframe source interval remains visible in output.
    All arguments are integers; no floating-point accumulation is used.
    """
    if end_us < start_us:
        raise TimingCompileError("source interval end precedes start")
    if speed_num <= 0 or speed_den <= 0:
        raise TimingCompileError("speed numerator and denominator must be positive")
    if fps <= 0:
        raise TimingCompileError("fps must be positive")

    denominator = speed_num * MICROSECONDS_PER_SECOND
    start_numerator = (timeline_start_us * speed_num + (start_us - source_start_us) * speed_den) * fps
    end_numerator = (timeline_start_us * speed_num + (end_us - source_start_us) * speed_den) * fps
    start_frame = start_numerator // denominator
    end_frame = -(-end_numerator // denominator)
    return start_frame, end_frame


def compile_timing(
    script: dict[str, Any],
    words: dict[str, Any],
    edit_map: list[dict[str, Any]],
    overrides: list[dict[str, Any]],
    fps: int,
) -> dict[str, Any]:
    """Compile semantic script events against aligned speech into frame events.

    ``script`` contains ``revision``, ``media_hash``, ``selections`` and
    ``events``. A selection is ``{selection_id, start_char, end_char,
    script_revision}``, using Unicode-code-point half-open character offsets.
    An event references a selection through ``selection_id`` and has an
    ``event_id`` plus optional ``required`` and ``out_of_bounds`` (``block``,
    ``trim``, or ``omit``). ``words`` carries the exact matching
    ``script_revision`` and ``media_hash`` plus aligned words with character and
    microsecond half-open ranges. ``edit_map`` holds source segments with
    ``source_start_us``, ``source_end_us``, ``timeline_start_us``, and optional
    positive ``speed_num``/``speed_den``. ``overrides`` may hold a
    ``relative_offset_us`` or a fixed-frame override.

    The returned dictionary is the payload portion of ``performance_timing.v1``:
    it records input bindings, split frame events, unresolved required events,
    and fixed-frame conflicts.  Required unresolved events are reported rather
    than fabricated as valid events so callers can return their normal 422 path.
    """
    if fps <= 0:
        raise TimingCompileError("fps must be positive")
    _validate_bindings(script, words)
    aligned_words = _validate_aligned_words(words.get("words", []))
    edit_segments = _validate_edit_map(edit_map)

    selections = _index_by_id(script.get("selections", []), "selection_id", "selection")
    overrides_by_event = _index_overrides(overrides)
    event_rows: list[dict[str, Any]] = []
    unresolved_required: list[dict[str, str]] = []
    conflicts: list[dict[str, Any]] = []

    for event in script.get("events", []):
        event_id = _required_string(event, "event_id", "event")
        required = bool(event.get("required", False))
        selection = selections.get(event.get("selection_id"))
        reason: str | None = None
        source_interval: tuple[int, int] | None = None
        if selection is None:
            reason = "selection is missing or invalid"
        elif selection.get("script_revision") != script["revision"]:
            reason = "selection script revision does not match script"
        else:
            try:
                source_interval = _aligned_interval(selection, aligned_words)
            except _IncompleteAlignment as error:
                reason = str(error)
            if source_interval is None:
                reason = reason or "selection has no aligned words"

        if source_interval is not None:
            offset_us = _relative_offset_us(overrides_by_event.get(event_id, []))
            source_interval = (source_interval[0] + offset_us, source_interval[1] + offset_us)
            pieces = _map_interval(source_interval, edit_segments, fps)
            if not pieces:
                reason = "selection is outside the edit map"
            elif not _fully_covered(source_interval, edit_segments):
                policy = event.get("out_of_bounds", "block")
                if policy == "trim":
                    pass
                elif policy == "omit" and not required:
                    pieces = []
                else:
                    reason = "selection crosses an unmapped edit range"

        if reason is not None:
            if required:
                unresolved_required.append({"event_id": event_id, "reason": reason})
            elif event.get("out_of_bounds") != "omit":
                raise TimingCompileError(f"optional event {event_id} must explicitly choose out_of_bounds='omit'")
            continue

        assert source_interval is not None
        pieces = _apply_minimum_duration(pieces, overrides_by_event.get(event_id, []))
        for start_frame, end_frame in pieces:
            event_rows.append({"event_id": event_id, "start_frame": start_frame, "end_frame": end_frame})
        conflicts.extend(_fixed_frame_conflicts(event_id, pieces, overrides_by_event.get(event_id, [])))

    result = {
        "schema": "performance_timing.v1",
        "script_revision": script["revision"],
        "media_hash": script["media_hash"],
        "fps": fps,
        "events": event_rows,
        "unresolved_required_events": unresolved_required,
        "override_conflicts": conflicts,
    }
    if type(words.get("artifact_revision")) is int and words["artifact_revision"] > 0:
        result["alignment_revision"] = words["artifact_revision"]
    return result


def _validate_bindings(script: dict[str, Any], words: dict[str, Any]) -> None:
    if "revision" not in script:
        raise TimingCompileError("script revision is required")
    if "media_hash" not in script:
        raise TimingCompileError("script media hash is required")
    if words.get("script_revision") != script["revision"]:
        raise TimingCompileError("word timeline script revision does not match script")
    if words.get("media_hash") != script["media_hash"]:
        raise TimingCompileError("word timeline media hash does not match script")


def _validate_aligned_words(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reject malformed alignment before it can produce output-frame events."""
    validated = list(rows)
    for word in validated:
        values = (word.get("start_char"), word.get("end_char"), word.get("start_us"), word.get("end_us"))
        if not all(isinstance(value, int) for value in values):
            raise TimingCompileError("aligned word ranges must use integers")
        start_char, end_char, start_us, end_us = values
        if start_char < 0 or end_char <= start_char or start_us < 0 or end_us <= start_us:
            raise TimingCompileError("aligned word range is invalid")
    return validated


def _validate_edit_map(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort source segments and reject source ranges with ambiguous ownership."""
    segments = sorted(rows, key=lambda item: item.get("source_start_us", -1))
    previous_end: int | None = None
    for segment in segments:
        start = segment.get("source_start_us")
        end = segment.get("source_end_us")
        timeline_start = segment.get("timeline_start_us")
        if not all(isinstance(value, int) for value in (start, end, timeline_start)) or end <= start:
            raise TimingCompileError("edit map segment has an invalid interval")
        if previous_end is not None and start < previous_end:
            raise TimingCompileError("edit map source segments overlap")
        previous_end = end
    return segments


def _index_by_id(rows: Iterable[dict[str, Any]], key: str, label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        identifier = _required_string(row, key, label)
        if identifier in result:
            raise TimingCompileError(f"duplicate {label} id: {identifier}")
        result[identifier] = row
    return result


def _index_overrides(overrides: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for override in overrides:
        event_id = _required_string(override, "event_id", "override")
        result.setdefault(event_id, []).append(override)
    return result


def _required_string(row: dict[str, Any], key: str, label: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise TimingCompileError(f"{label} {key} is required")
    return value


def _aligned_interval(selection: dict[str, Any], word_rows: Iterable[dict[str, Any]]) -> tuple[int, int] | None:
    start_char = selection.get("start_char")
    end_char = selection.get("end_char")
    if not isinstance(start_char, int) or not isinstance(end_char, int) or end_char <= start_char:
        raise TimingCompileError("selection character range must be a non-empty integer interval")

    matched = [
        word
        for word in word_rows
        if word.get("start_char", -1) < end_char and word.get("end_char", -1) > start_char
    ]
    if not matched:
        return None
    cursor = start_char
    for word in sorted(matched, key=lambda item: item["start_char"]):
        if word["start_char"] > cursor:
            raise _IncompleteAlignment("selection has incomplete aligned character coverage")
        cursor = max(cursor, word["end_char"])
    if cursor < end_char:
        raise _IncompleteAlignment("selection has incomplete aligned character coverage")
    return min(word["start_us"] for word in matched), max(word["end_us"] for word in matched)


def _map_interval(source: tuple[int, int], edit_map: Iterable[dict[str, Any]], fps: int) -> list[tuple[int, int]]:
    start_us, end_us = source
    pieces: list[tuple[int, int]] = []
    for segment in edit_map:
        segment_start = segment.get("source_start_us")
        segment_end = segment.get("source_end_us")
        timeline_start = segment.get("timeline_start_us")
        intersection_start = max(start_us, segment_start)
        intersection_end = min(end_us, segment_end)
        if intersection_end <= intersection_start:
            continue
        pieces.append(
            map_source_interval_to_frames(
                start_us=intersection_start,
                end_us=intersection_end,
                source_start_us=segment_start,
                timeline_start_us=timeline_start,
                speed_num=segment.get("speed_num", 1),
                speed_den=segment.get("speed_den", 1),
                fps=fps,
            )
        )
    return pieces


def _fully_covered(source: tuple[int, int], edit_map: Iterable[dict[str, Any]]) -> bool:
    cursor = source[0]
    for segment in edit_map:
        segment_start = segment.get("source_start_us")
        segment_end = segment.get("source_end_us")
        if not isinstance(segment_start, int) or not isinstance(segment_end, int):
            continue
        if segment_end <= cursor or segment_start > cursor:
            continue
        cursor = max(cursor, segment_end)
        if cursor >= source[1]:
            return True
    return cursor >= source[1]


def _relative_offset_us(overrides: Iterable[dict[str, Any]]) -> int:
    offsets = [override["relative_offset_us"] for override in overrides if "relative_offset_us" in override]
    if len(offsets) > 1:
        raise TimingCompileError("only one relative timing override is allowed per event")
    if offsets and not isinstance(offsets[0], int):
        raise TimingCompileError("relative timing override must be an integer microsecond offset")
    return offsets[0] if offsets else 0


def _fixed_frame_conflicts(
    event_id: str, pieces: list[tuple[int, int]], overrides: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    for override in overrides:
        if override.get("kind") != "fixed_frames":
            continue
        fixed = {"start_frame": override.get("start_frame"), "end_frame": override.get("end_frame")}
        if not all(isinstance(value, int) for value in fixed.values()):
            raise TimingCompileError("fixed frame override requires integer frame boundaries")
        if len(pieces) != 1 or pieces[0] != (fixed["start_frame"], fixed["end_frame"]):
            conflicts.append(
                {
                    "event_id": event_id,
                    "reason": "fixed frame override differs from compiled timing",
                    "override": fixed,
                }
            )
    return conflicts


def _apply_minimum_duration(
    pieces: list[tuple[int, int]], overrides: Iterable[dict[str, Any]]
) -> list[tuple[int, int]]:
    values = [override.get("frames") for override in overrides if override.get("kind") == "minimum_duration_frames"]
    if len(values) > 1:
        raise TimingCompileError("only one minimum duration override is allowed per event")
    if not values:
        return pieces
    frames = values[0]
    if type(frames) is not int or frames <= 0:
        raise TimingCompileError("minimum duration override requires positive integer frames")
    if not pieces:
        return pieces
    actual = sum(end - start for start, end in pieces)
    if actual >= frames:
        return pieces
    result = list(pieces)
    start, end = result[-1]
    result[-1] = (start, end + frames - actual)
    return result
