"""JSON Schema for native production edits and the isolated Hermes tool."""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator

from .production_plan_v2 import BeatV2, ExactArtifactRefV2, OutputV2, PlacementV2, ResourceV2, SelectionV2, SpeechTakeV2, TurnV2


_STRING = {"type": "string", "minLength": 1}
_INDEX = {"type": "integer", "minimum": 0}
_ID_LIST = {"type": "array", "items": _STRING}


def _field(model: type, name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    schema = model.model_json_schema()
    return deepcopy(schema["properties"][name]), deepcopy(schema.get("$defs", {}))


def _entity(model: type) -> tuple[dict[str, Any], dict[str, Any]]:
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})
    schema.pop("title", None)
    return schema, definitions


def _payload_specifications() -> dict[str, dict[str, tuple[dict[str, Any], dict[str, Any]]]]:
    def native(model: type, name: str) -> tuple[dict[str, Any], dict[str, Any]]:
        return _field(model, name)

    def entity(model: type) -> tuple[dict[str, Any], dict[str, Any]]:
        return _entity(model)

    def simple(schema: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        return schema, {}

    return {
        "set_goal": {"goal": simple({"type": "string", "minLength": 1, "maxLength": 2000})},
        "insert_beat": {"beat": entity(BeatV2), "index": simple(_INDEX)},
        "insert_turn": {"beat_id": simple(_STRING), "turn": entity(TurnV2), "index": simple(_INDEX)},
        "reorder_beats": {"ids": simple(_ID_LIST)},
        "reorder_turns": {"ids": simple(_ID_LIST)},
        "edit_beat": {name: native(BeatV2, name) for name in (
            "purpose", "duration_frames", "timing_mode", "duration_source_placement_id",
            "speech_pace", "extra_pause_seconds")},
        "edit_speech_take": {name: native(SpeechTakeV2, name) for name in
                             ("source_in_us", "source_out_us", "speed_num", "speed_den", "lead_in_frames", "volume")},
        "edit_turn": {**{name: native(TurnV2, name) for name in ("speaker", "text", "speech_placement_id")},
                      "invalidate_speech_timing": simple({"const": True})},
        "bind_media": {"resource_id": simple(_STRING), "resource": entity(ResourceV2),
                       "invalidate_speech_timing": simple({"const": True})},
        "bind_speech_source": {"placement_id": simple(_STRING), "turn_ids": simple({**_ID_LIST, "minItems": 1, "uniqueItems": True})},
        "reassign_speech_source": {"placement_id": simple({"anyOf": [_STRING, {"type": "null"}]}),
                                   "turn_ids": simple({**_ID_LIST, "minItems": 2, "uniqueItems": True})},
        "upsert_placement": {"placement": entity(PlacementV2)},
        "upsert_selection": {"selection": entity(SelectionV2)},
        "remove_entity": {"entity_kind": simple({"enum": ["placement", "selection", "beat", "turn"]})},
        "set_output": {"output": entity(OutputV2)},
    }


def production_payload_fields() -> dict[str, set[str]]:
    """The one command-to-payload field map used by native prevalidation."""
    return {**{kind: set(fields) for kind, fields in _payload_specifications().items()},
            "batch": {"commands"}}


def _payload(kind: str, specifications: dict[str, dict], definitions: dict[str, Any]) -> dict[str, Any]:
    properties = {}
    for name, (schema, nested) in specifications[kind].items():
        properties[name] = deepcopy(schema)
        for key, value in nested.items():
            if key in definitions and definitions[key] != value:
                raise ValueError(f"conflicting native schema definition: {key}")
            definitions[key] = value
    required = {
        "set_goal": ["goal"], "insert_beat": ["beat", "index"],
        "insert_turn": ["beat_id", "turn", "index"],
        "reorder_beats": ["ids"], "reorder_turns": ["ids"],
        "bind_media": ["resource_id"], "bind_speech_source": ["placement_id", "turn_ids"],
        "reassign_speech_source": ["placement_id", "turn_ids"],
        "upsert_placement": ["placement"], "upsert_selection": ["selection"], "remove_entity": ["entity_kind"],
        "set_output": ["output"],
    }.get(kind, [])
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": required, "minProperties": 1}


def _branch(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"properties": {"kind": {"const": kind}, "payload": payload}, "required": ["kind", "payload"]}


@lru_cache(maxsize=1)
def _cached_schema() -> dict[str, Any]:
    definitions: dict[str, Any] = {}
    specs = _payload_specifications()
    branches = {kind: _branch(kind, _payload(kind, specs, definitions)) for kind in specs}
    # Pydantic's model validators describe cross-field constraints that JSON
    # Schema cannot infer from the individual fields.
    anchor = definitions["AnchorV2"]
    anchor["allOf"] = [
        {
            "if": {"properties": {"kind": {"const": "program_frame"}}},
            "then": {"required": ["frame"], "properties": {
                "frame": {"type": "integer"}, "ref_id": {"type": "null"},
                "edge": {"type": "null"}, "offset_frames": {"const": 0},
                "char_offset": {"type": "null"}, "text_hash": {"type": "null"},
                "affinity": {"type": "null"},
            }},
        },
        {
            "if": {"properties": {"kind": {"const": "text"}}},
            "then": {"required": ["ref_id", "char_offset"], "properties": {
                "ref_id": _STRING, "char_offset": {"type": "integer", "minimum": 0},
                "frame": {"type": "null"}, "edge": {"type": "null"},
            }},
        },
        {
            "if": {"properties": {"kind": {"not": {"enum": ["program_frame", "text"]}}}},
            "then": {"required": ["ref_id", "edge"], "properties": {
                "ref_id": _STRING, "edge": {"enum": ["start", "end"]}, "frame": {"type": "null"},
                "char_offset": {"type": "null"}, "text_hash": {"type": "null"},
                "affinity": {"type": "null"},
            }},
        },
    ]
    placement = branches["upsert_placement"]["properties"]["payload"]["properties"]["placement"]
    placement["allOf"] = [{"if": {"properties": {"kind": {"const": "graphic"}}},
                            "then": {"required": ["graphic"], "properties": {"graphic": {"type": "object"},
                                                                           "resource_id": {"type": "null"}}}}]
    child = {"type": "object", "additionalProperties": False,
             "properties": {"kind": {"type": "string"}, "target_id": _STRING,
                            "payload": {"type": "object"}},
             "required": ["kind", "target_id", "payload"],
             "oneOf": list(branches.values())}
    branches["batch"] = _branch("batch", {"type": "object", "additionalProperties": False,
                                           "properties": {"commands": {"type": "array", "minItems": 1,
                                                                       "maxItems": 100, "items": child}},
                                           "required": ["commands"]})
    ref, ref_defs = _entity(ExactArtifactRefV2)
    definitions.update(ref_defs)
    return {"type": "object", "additionalProperties": False,
            "properties": {"command_id": {"type": "string", "minLength": 1, "maxLength": 128,
                                          "pattern": "^[A-Za-z0-9_-]+$"},
                           "plan_ref": ref, "kind": {"enum": list(branches)},
                           "target_id": _STRING, "payload": {"type": "object"}},
            "required": ["command_id", "plan_ref", "kind", "target_id", "payload"],
            "oneOf": list(branches.values()), "$defs": definitions}


def production_command_json_schema() -> dict[str, Any]:
    return deepcopy(_cached_schema())


def structural_command_errors(command: Any) -> list:
    return list(Draft202012Validator(_cached_schema()).iter_errors(command))


def production_command_error_details(command: Any) -> dict[str, Any]:
    """Locate the failed prevalidation field without claiming an execution result."""
    def details(path: str, branch: Any, missing: list[str] | None = None) -> dict[str, Any]:
        return {"error_path": path, "expected_branch": branch, "missing_fields": missing or []}

    def schema_issue(schema: dict[str, Any], value: Any, path: str, branch: Any) -> dict[str, Any] | None:
        validator = Draft202012Validator({**schema, "$defs": _cached_schema()["$defs"]})
        error = next(validator.iter_errors(value), None)
        if error is None:
            return None
        missing = (sorted(set(error.validator_value) - set(error.instance))
                   if error.validator == "required" and isinstance(error.instance, dict) else [])
        location = path + "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in error.path)
        return details(location + (f".{missing[0]}" if missing else ""), branch, missing)

    if not isinstance(command, dict):
        return details("command", None)
    kind = command.get("kind")
    outer_missing = sorted({"command_id", "plan_ref", "kind", "target_id", "payload"} - set(command))
    if outer_missing:
        return details(outer_missing[0], kind, outer_missing)
    for name in ("command_id", "plan_ref", "kind", "target_id"):
        issue = schema_issue(_cached_schema()["properties"][name], command[name], name, kind)
        if issue:
            return issue
    extra = sorted(set(command) - set(_cached_schema()["properties"]))
    if extra:
        return details(extra[0], kind)
    branch = next((row for row in _cached_schema()["oneOf"]
                   if row["properties"]["kind"].get("const") == kind), None)
    value = command.get("payload")
    prefix = "payload"
    if kind == "batch" and isinstance(value, dict) and isinstance(value.get("commands"), list):
        for index, child in enumerate(value["commands"]):
            child_path = f"payload.commands[{index}]"
            if not isinstance(child, dict):
                return details(child_path, "non-batch command")
            missing = sorted({"kind", "target_id", "payload"} - set(child))
            if missing:
                return details(f"{child_path}.{missing[0]}", child.get("kind"), missing)
            if child.get("kind") == "batch":
                return details(f"{child_path}.kind", "non-batch command")
            child_kind = child.get("kind")
            child_branch = next((row for row in _cached_schema()["oneOf"]
                                 if row["properties"]["kind"].get("const") == child_kind), None)
            if child_branch is None:
                return details(f"{child_path}.kind", "non-batch command")
            issue = schema_issue(_cached_schema()["properties"]["target_id"],
                                 child["target_id"], f"{child_path}.target_id", child_kind)
            if issue:
                return issue
            issue = schema_issue(child_branch["properties"]["payload"], child["payload"],
                                 f"{child_path}.payload", child_kind)
            if issue:
                return issue
            extra = sorted(set(child) - {"kind", "target_id", "payload"})
            if extra:
                return details(f"{child_path}.{extra[0]}", child_kind)
    if branch is not None:
        issue = schema_issue(branch["properties"]["payload"], value, prefix, kind)
        if issue:
            return issue
    return details(prefix, kind)
