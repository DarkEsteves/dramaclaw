"""Private authoring stages for one saved production plan."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import re
from typing import Any, Literal

from jsonschema import Draft202012Validator

from .production_plan_v2 import production_plan_draft_json_schema


DraftStage = Literal["full", "outline", "arrangement", "finalize"]


class DraftStageValidationError(ValueError):
    def __init__(self, issues: list[dict[str, str]], count: int):
        super().__init__("production_plan_stage_validation_failed")
        self.issues = issues
        self.count = count


@dataclass(frozen=True)
class DraftStageEvent:
    stage: DraftStage
    status: Literal["started", "completed"]
    payload: dict[str, Any] | None = None


def _reachable_definitions(schema: dict[str, Any]) -> None:
    definitions = schema.get("$defs", {})
    seen: set[str] = set()
    pending: list[dict[str, Any]] = [
        {key: value for key, value in schema.items() if key != "$defs"},
    ]
    while pending:
        fragment = pending.pop()
        for name in re.findall(r'"\$ref":\s*"#\/\$defs\/([^\"]+)"', json.dumps(fragment)):
            if name not in seen:
                seen.add(name)
                pending.append(definitions[name])
    schema["$defs"] = {name: value for name, value in definitions.items() if name in seen}


def draft_stage_schema(stage: Literal["outline", "arrangement"]) -> dict[str, Any]:
    """Project each private stage from the one persisted plan authoring model."""
    schema = production_plan_draft_json_schema()
    if stage == "outline":
        allowed = {"output", "beats"}
        schema["properties"] = {key: value for key, value in schema["properties"].items()
                                if key in allowed}
        schema["properties"]["creative_direction"] = {"type": "string", "minLength": 1}
        schema["required"] = ["output", "beats", "creative_direction"]
        for name, keys in (
            ("BeatV2", {"purpose", "duration_frames", "turns"}),
            ("TurnV2", {"speaker", "text"}),
        ):
            definition = schema["$defs"][name]
            definition["properties"] = {key: value for key, value in definition["properties"].items()
                                        if key in keys}
            definition["required"] = [key for key in definition.get("required", []) if key in keys]
    elif stage == "arrangement":
        allowed = {"selections", "placements"}
        schema["properties"] = {key: value for key, value in schema["properties"].items()
                                if key in allowed}
        schema["properties"]["speech_bindings"] = {
            "type": "object", "additionalProperties": {"type": "string", "minLength": 1},
        }
        schema["required"] = ["selections", "placements", "speech_bindings"]
    else:
        raise ValueError("invalid_production_plan_stage")
    schema["additionalProperties"] = False
    _reachable_definitions(schema)
    return schema


def draft_stage_native_schema(
    stage: Literal["outline", "arrangement"], *, turn_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Project the stage contract into the subset accepted by strict model output.

    Conditional and value constraints stay in ``draft_stage_schema`` and are
    checked after generation. Optional fields become required nullable fields,
    as required by the native structured-output API.
    """
    source = draft_stage_schema(stage)
    if stage == "arrangement":
        ids = list(dict.fromkeys(turn_ids or []))
        if any(not isinstance(value, str) or not value for value in ids):
            raise ValueError("invalid_production_plan_turn_ids")
        source["properties"]["speech_bindings"] = {
            "type": "object",
            "properties": {value: {"type": ["string", "null"]} for value in ids},
            "required": ids,
            "additionalProperties": False,
        }

    def project(fragment: Any) -> Any:
        if isinstance(fragment, list):
            return [project(value) for value in fragment]
        if not isinstance(fragment, dict):
            return fragment
        if "$ref" in fragment:
            return {"$ref": fragment["$ref"]}
        if "anyOf" in fragment or "oneOf" in fragment:
            choices = fragment.get("anyOf", fragment.get("oneOf"))
            return {"anyOf": [project(value) for value in choices]}
        result = {key: fragment[key] for key in ("type", "enum", "const", "description")
                  if key in fragment}
        if fragment.get("type") == "object":
            required = set(fragment.get("required", []))
            properties = {}
            for key, value in fragment.get("properties", {}).items():
                item = project(value)
                if key not in required and not (
                    item.get("type") == "null"
                    or isinstance(item.get("type"), list) and "null" in item["type"]
                    or any(choice.get("type") == "null" for choice in item.get("anyOf", []))
                ):
                    item = {"anyOf": [item, {"type": "null"}]}
                properties[key] = item
            result.update(properties=properties, required=list(properties), additionalProperties=False)
        if fragment.get("type") == "array":
            result["items"] = project(fragment["items"])
        if "$defs" in fragment:
            result["$defs"] = {key: project(value) for key, value in fragment["$defs"].items()}
        return result

    return project(source)


def compact_native_stage_output(stage: Literal["outline", "arrangement"], payload: dict[str, Any]) -> dict[str, Any]:
    """Remove null placeholders used for optional fields in strict output."""
    schema = draft_stage_schema(stage)
    definitions = schema.get("$defs", {})

    def compact(fragment: dict[str, Any], value: Any) -> Any:
        if "$ref" in fragment:
            fragment = definitions[fragment["$ref"].rsplit("/", 1)[-1]]
        if isinstance(value, list):
            return [compact(fragment.get("items", {}), item) for item in value]
        if not isinstance(value, dict):
            return value
        if "anyOf" in fragment:
            branch = next((item for item in fragment["anyOf"]
                           if item.get("type") == "object" or "$ref" in item), {})
            fragment = branch
            if "$ref" in fragment:
                fragment = definitions[fragment["$ref"].rsplit("/", 1)[-1]]
        properties = fragment.get("properties", {})
        required = set(fragment.get("required", []))
        return {key: compact(properties.get(key, {}), item) for key, item in value.items()
                if item is not None or key in required}

    return compact(schema, payload)


def _validate(stage: Literal["outline", "arrangement"], payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ValueError("production_plan_stage_validation_failed")
    errors = list(Draft202012Validator(draft_stage_schema(stage)).iter_errors(payload))
    if errors:
        errors.sort(key=lambda item: tuple(map(str, item.absolute_path)))
        def safe_path(error: Any) -> str:
            parts = list(map(str, error.absolute_path))
            if len(parts) > 1 and parts[0] == "speech_bindings":
                parts[1] = "binding"
            return "/" + "/".join(parts)

        raise DraftStageValidationError([
            {"path": safe_path(error),
             "code": str(error.validator or "schema_validation")}
            for error in errors[:20]
        ], len(errors))


def prepare_outline(payload: dict[str, Any]) -> dict[str, Any]:
    """Freeze the story and assign reference IDs before requesting arrangements."""
    _validate("outline", payload)
    result = deepcopy(payload)
    turn_number = 0
    for beat_number, beat in enumerate(result["beats"], 1):
        beat["id"] = f"beat-{beat_number}"
        for turn in beat.get("turns", []):
            turn_number += 1
            turn["id"] = f"turn-{turn_number}"
    return result


def assemble_stage_draft(
    topic: str, outline: dict[str, Any], arrangement: dict[str, Any],
) -> dict[str, Any]:
    """Combine authored relationships without changing the accepted story."""
    if (not isinstance(arrangement, dict)
            or set(arrangement) != {"selections", "placements", "speech_bindings"}
            or not isinstance(arrangement["placements"], list)
            or not isinstance(arrangement["selections"], list)
            or not isinstance(arrangement["speech_bindings"], dict)):
        raise ValueError("production_plan_stage_validation_failed")
    beats = deepcopy(outline["beats"])
    turns = {turn["id"]: turn for beat in beats for turn in beat.get("turns", [])}
    placements = deepcopy(arrangement["placements"])
    if (any(not isinstance(row, dict) or not isinstance(row.get("id"), str)
            for row in placements)
            or any(not isinstance(turn_id, str) or not isinstance(placement_id, str)
                   for turn_id, placement_id in arrangement["speech_bindings"].items())):
        raise ValueError("production_plan_stage_validation_failed")
    placement_ids = {row["id"] for row in placements}
    if len(placement_ids) != len(placements):
        raise ValueError("production_plan_stage_validation_failed")
    for turn_id, placement_id in arrangement["speech_bindings"].items():
        if turn_id not in turns or placement_id not in placement_ids:
            raise ValueError("production_plan_stage_validation_failed")
        turns[turn_id]["speech_placement_id"] = placement_id
    plan = {
        "schema_version": 2, "goal": topic,
        "output": deepcopy(outline["output"]), "beats": beats,
        "selections": deepcopy(arrangement["selections"]),
        "placements": placements, "resources": [], "provenance": {},
    }
    from .production_plan_draft_normalization import normalize_production_plan_draft

    normalized, _ = normalize_production_plan_draft(plan)
    _validate("arrangement", {
        "selections": normalized["selections"],
        "placements": normalized["placements"],
        "speech_bindings": arrangement["speech_bindings"],
    })
    return plan


def outline_to_plan(topic: str, outline: dict[str, Any]) -> dict[str, Any]:
    """Persist the editable story without inventing media arrangements."""
    from .contracts import validate_payload
    return validate_payload("production_plan.v2", {
        "schema_version": 2, "authoring_stage": "outline", "goal": topic,
        "output": deepcopy(outline["output"]), "beats": deepcopy(outline["beats"]),
        "selections": [], "placements": [], "resources": [],
        "speech_takes": [], "speech_cues": [], "provenance": {},
    })


def outline_from_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Read the current saved story, preserving user edits and reference IDs."""
    from .contracts import validate_payload
    saved = validate_payload("production_plan.v2", plan)
    return {"creative_direction": saved["goal"], "output": deepcopy(saved["output"]),
            "beats": deepcopy(saved["beats"])}
