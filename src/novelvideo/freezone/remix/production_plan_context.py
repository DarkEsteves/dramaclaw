"""Lossless, bounded authoring context for the shared production-plan action."""

from __future__ import annotations

import copy
import json
import math
import os
from typing import Any


_RESOURCE_KEYS = {"url", "uri", "thumbnail", "thumbnail_url", "video_url", "image_url",
                  "audio_url", "signed_url", "project_path", "media_path", "frame_data",
                  "frame_url", "image_base64", "decode_pts", "pts", "image_prompt",
                  "motion_prompt"}
_RAW_SHOT_KEYS = {"shot", "start_time", "end_time", "duration"}


def _semantic_value(value: Any) -> Any:
    """Discard transport references, while retaining source meaning and timing."""
    if isinstance(value, dict):
        return {key: _semantic_value(item) for key, item in value.items()
                if key not in _RESOURCE_KEYS and not key.endswith("_url")}
    if isinstance(value, list):
        return [_semantic_value(item) for item in value]
    return copy.deepcopy(value)


def _same_source_event(event: dict[str, Any], observation: dict[str, Any]) -> bool:
    left_id = event.get("event_id") or event.get("id")
    right_id = observation.get("event_id") or observation.get("id")
    if left_id is not None and right_id is not None and left_id != right_id:
        return False
    left_range = (event.get("start_us"), event.get("end_us"))
    right_range = (observation.get("start_us"), observation.get("end_us"))
    if any(item is None for item in right_range):
        seconds = (observation.get("start_time"), observation.get("end_time"))
        if all(type(item) in (int, float) and math.isfinite(item) for item in seconds):
            right_range = tuple(round(item * 1_000_000) for item in seconds)
    if all(item is not None for item in (*left_range, *right_range)):
        return left_range == right_range
    return left_id is not None and right_id is not None and left_id == right_id


def _evidence(blueprint: dict[str, Any]) -> dict[str, Any]:
    events = [_semantic_value(row) for row in blueprint.get("events", []) if isinstance(row, dict)]
    unmatched: list[dict[str, Any]] = []
    for raw in blueprint.get("observations", []):
        if not isinstance(raw, dict):
            continue
        observation = _semantic_value(raw)
        matches = [event for event in events if _same_source_event(event, observation)]
        if len(matches) != 1:
            unmatched.append(observation)
            continue
        matching = matches[0]
        for key, value in observation.items():
            if key in _RAW_SHOT_KEYS:
                continue
            if key == "visual_description" and value == matching.get("model", {}).get("visual_observation"):
                continue
            if key == "narrative" and value == matching.get("model", {}).get("narrative_inference"):
                continue
            if key not in matching:
                matching[key] = value
            elif matching[key] != value:
                details = matching.setdefault("observation_detail", {}).setdefault(key, [])
                if value not in details:
                    details.append(value)
    result: dict[str, Any] = {}
    if events:
        result["events"] = events
    if unmatched:
        result["unmatched_observations"] = unmatched
    for key in ("evidence_ref", "transcript_ref"):
        if key in blueprint:
            result[key] = _semantic_value(blueprint[key])
    return result


def build_production_plan_context(
    *, blueprint: dict[str, Any] | None,
    source_timing: dict[str, Any] | None,
    requirements: dict[str, Any] | None,
    locked_turns: list[dict[str, str]] | None,
    output_preferences: dict[str, Any] | None,
) -> dict[str, Any]:
    """Project the complete authoring evidence without mutating stored artifacts."""
    from .production_requirements import normalize_requirements

    context: dict[str, Any] = {}
    if isinstance(blueprint, dict):
        for key in ("summary", "narrative_role"):
            if isinstance(blueprint.get(key), str) and blueprint[key].strip():
                context[key] = blueprint[key].strip()
        structure = blueprint.get("adaptation_structure")
        if isinstance(structure, dict):
            context["adaptation_structure"] = _semantic_value(structure)
        choices = blueprint.get("preserve_replace_rules") or blueprint.get("user_choices")
        if isinstance(choices, list):
            rules = [_semantic_value(row) for row in choices if isinstance(row, dict)
                     and isinstance(row.get("type"), str)
                     and row["type"] in {"preserve", "replace"}
                     and isinstance(row.get("text"), str) and row["text"].strip()]
            if rules:
                context["preserve_replace_rules"] = rules
    if source_timing is not None:
        context["source_timing"] = _semantic_value(source_timing)
    if isinstance(output_preferences, dict):
        preferences = {}
        for key in ("aspect_ratio", "video_aspect_ratio"):
            if isinstance(output_preferences.get(key), str) and 0 < len(output_preferences[key]) <= 32:
                preferences[key] = output_preferences[key]
        duration = output_preferences.get("source_clip_duration_seconds")
        if type(duration) in (int, float) and 0 < duration <= 600:
            preferences["source_clip_duration_seconds"] = duration
        if preferences:
            context["output_preferences"] = preferences
    if locked_turns is not None:
        context["locked_turns"] = copy.deepcopy(locked_turns)
    if requirements is not None:
        context["requirements"] = normalize_requirements(requirements)
    if isinstance(blueprint, dict):
        evidence = _evidence(blueprint)
        if evidence:
            context["source_evidence"] = evidence
    return context


def plan_context_char_budget() -> int:
    raw = os.getenv("FREEZONE_PRODUCTION_PLAN_CONTEXT_CHARS", "16000")
    try:
        budget = int(raw)
    except ValueError as exc:
        raise ValueError("invalid_production_plan_context_budget") from exc
    if budget < 1000:
        raise ValueError("invalid_production_plan_context_budget")
    return budget


def serialize_plan_context(context: dict[str, Any], *, max_chars: int) -> str:
    encoded = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > max_chars:
        raise ValueError(
            f"production_plan_context_budget_exceeded: stage=context actual_chars={len(encoded)} "
            f"limit_chars={max_chars}"
        )
    return encoded
