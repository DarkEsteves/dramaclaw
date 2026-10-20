"""Local estimates and deterministic checks for an editable production plan."""

from __future__ import annotations

import re
from typing import Any

from .production_plan_v2 import resolve_production_placement_timing


_RATES = {
    "zh": {"slow": 4.2, "normal": 5.25, "fast": 6.5625},
    "en": {"slow": 4.2, "normal": 4.6, "fast": 5.6},
}
_HAN = re.compile(r"[\u3400-\u9fff]")
_LATIN_WORD = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*")
_VOWELS = re.compile(r"[aeiouy]+", re.IGNORECASE)


def _english_syllables(word: str) -> int:
    word = word.casefold()
    if len(word) > 3 and word.endswith("e") and not word.endswith(("le", "ye")):
        word = word[:-1]
    return max(1, len(_VOWELS.findall(word)))


def estimate_speech_duration(
    text: str, *, language: str = "auto", pace: str = "normal",
    padding_seconds: float = 0,
) -> dict[str, Any]:
    """Estimate pronunciation time, never a verified utterance boundary."""
    if language not in {*_RATES, "auto"} or pace not in _RATES["zh"]:
        raise ValueError("unsupported speech estimate language or pace")
    if isinstance(padding_seconds, bool) or not isinstance(padding_seconds, (int, float)) or padding_seconds < 0:
        raise ValueError("padding_seconds must be nonnegative")
    han = len(_HAN.findall(text))
    latin = _LATIN_WORD.findall(text)
    detected = ("zh" if han else "en") if language == "auto" else language
    units = han + sum(_english_syllables(word) for word in latin)
    if units == 0:
        return {"estimated_seconds": None, "pronunciation_units": None,
                "units_per_second": None, "language": detected, "pace": pace,
                "padding_seconds": float(padding_seconds), "confidence": "unsupported",
                "basis": "no_countable_speech"}
    rate = _RATES[detected][pace]
    low_confidence = bool(re.search(r"\d", text)) or bool(han and latin)
    return {"estimated_seconds": units / rate + padding_seconds,
            "pronunciation_units": units, "units_per_second": rate,
            "language": detected, "pace": pace,
            "padding_seconds": float(padding_seconds),
            "confidence": "low" if low_confidence else "approximate",
            "basis": "han_characters_and_approximate_latin_syllables"}


def measure_plan_speech(
    payload: dict[str, Any], *, local_timing_beat_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Estimate each authored turn and the sequential speech in its beat."""
    fps = payload.get("output") or {}
    frame_rate = fps.get("fps_num", 0) / fps.get("fps_den", 1) if fps.get("fps_den") else 0
    rows: list[dict[str, Any]] = []
    for beat in payload.get("beats") or []:
        if not isinstance(beat, dict) or not isinstance(beat.get("turns"), list) or not beat["turns"]:
            continue
        pace = beat.get("speech_pace") or "normal"
        pause = beat.get("extra_pause_seconds") or 0
        pace_basis = "authored" if beat.get("speech_pace") is not None else "default_normal"
        pause_basis = "authored" if beat.get("extra_pause_seconds") is not None else "default_zero"
        seconds = 0.0
        countable = True
        for turn in beat["turns"]:
            estimate = estimate_speech_duration(turn["text"], pace=pace)
            rows.append({"entity_kind": "turn", "entity_id": turn["id"],
                         "available_seconds": None, "estimate": estimate})
            if estimate["estimated_seconds"] is None:
                countable = False
            else:
                seconds += estimate["estimated_seconds"]
        estimate = {"estimated_seconds": seconds + pause if countable else None,
                    "pace": pace, "padding_seconds": float(pause),
                    "pace_basis": pace_basis, "pause_basis": pause_basis,
                    "duration_basis": ("local_speech_estimate" if beat["id"] in (local_timing_beat_ids or set())
                                       else "authored_or_saved"),
                    "confidence": "approximate" if countable else "unsupported",
                    "basis": "sequential_turns_plus_explicit_pause"}
        available = beat.get("duration_frames")
        rows.append({"entity_kind": "beat", "entity_id": beat["id"],
                     "available_seconds": available / frame_rate if available is not None and frame_rate else None,
                     "estimate": estimate})
    return rows


def collect_plan_diagnostics(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Report local issues without changing the plan or requiring generated media."""
    issues: list[dict[str, Any]] = []
    requirements = (payload.get("provenance") or {}).get("requirements")
    if isinstance(requirements, dict) and requirements.get("duration_us"):
        from pathlib import Path
        from .production_requirements import check_production_requirements
        for check in check_production_requirements(payload, requirements, project_dir=Path('.')):
            if check["code"] == "duration_mismatch" and check["status"] == "failed":
                issues.append({"code": "duration_mismatch", "severity": "warning",
                               "entity_kind": "plan", "entity_id": "plan",
                               "message": check["message"], "details": {"basis": "confirmed_total_duration"}})
    for row in measure_plan_speech(payload):
        if row["entity_kind"] != "beat":
            continue
        estimated, available = row["estimate"]["estimated_seconds"], row["available_seconds"]
        if estimated is not None and available is not None and estimated > available:
            issues.append({"code": "speech_density", "severity": "warning",
                           "entity_kind": "beat", "entity_id": row["entity_id"],
                           "message": "Estimated speech and pauses exceed the authored beat time",
                           "details": {"available_seconds": available, "estimated_seconds": estimated,
                                       "basis": row["estimate"]["basis"]}})
    _, placements = resolve_production_placement_timing(payload)
    authored_placements = {row["id"]: row for row in payload.get("placements") or []}
    for row in placements:
        if row["timing_status"] == "invalid":
            issues.append({"code": "invalid_timing", "severity": "error",
                           "entity_kind": "placement", "entity_id": row["id"],
                           "message": "Placement has an empty or out-of-bounds interval",
                           "details": {"start_frame": row["start_frame"], "end_frame": row["end_frame"]}})
        elif row["timing_status"] == "resolved" and row["kind"] == "graphic":
            from .graphic_effects import GraphicEffect
            effect = GraphicEffect.model_validate(authored_placements[row["id"]]["graphic"])
            available = row["end_frame"] - row["start_frame"]
            animated = effect.enter_frames + effect.exit_frames
            if animated > available:
                issues.append({"code": "graphic_animation_exceeds_interval", "severity": "error",
                               "entity_kind": "placement", "entity_id": row["id"],
                               "message": "Graphic entrance and exit exceed the placement interval",
                               "details": {"actual_frames": animated, "available_frames": available}})
    return issues
