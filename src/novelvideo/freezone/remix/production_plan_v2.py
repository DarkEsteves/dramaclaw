"""Versioned author data for an editable production, separate from render output."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from fractions import Fraction
import hashlib
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError
from .graphic_effects import GraphicEffect


PLACEMENT_ROLES: dict[str, frozenset[str]] = {
    "visual": frozenset({"primary", "overlay"}),
    "audio": frozenset({"speech", "narration", "bgm", "sfx"}),
    "subtitle": frozenset({"dialogue"}),
    "graphic": frozenset({"effect"}),
}


class _Value(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class OutputV2(_Value):
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps_num: int = Field(gt=0)
    fps_den: int = Field(gt=0)
    background: str = Field(default="#000000", pattern=r"^#[0-9a-fA-F]{6}$")


class WordV2(_Value):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    start_frame: int = Field(ge=0)
    end_frame: int = Field(gt=0)

    @model_validator(mode="after")
    def increasing(self) -> "WordV2":
        if self.end_frame <= self.start_frame:
            raise ValueError("word bounds must increase")
        return self


class ExactArtifactRefV2(_Value):
    artifact_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    revision: int = Field(ge=1)


class SourceWordV2(_Value):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    start_us: int = Field(ge=0)
    end_us: int = Field(gt=0)
    start_char: int | None = Field(default=None, ge=0)
    end_char: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def increasing(self) -> "SourceWordV2":
        if self.end_us <= self.start_us:
            raise ValueError("source word bounds must increase")
        if (self.start_char is None) != (self.end_char is None):
            raise ValueError("source word character bounds must be paired")
        if self.start_char is not None and self.end_char <= self.start_char:
            raise ValueError("source word character bounds must increase")
        return self


class SpeechTimingV2(_Value):
    placement_id: str = Field(min_length=1)
    media_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    text: str = Field(min_length=1)
    text_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_in_us: int | None = Field(default=None, ge=0)
    source_out_us: int | None = Field(default=None, gt=0)
    evidence_ref: ExactArtifactRefV2
    words: list[SourceWordV2] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_text_hash(self) -> "SpeechTimingV2":
        if hashlib.sha256(self.text.encode("utf-8")).hexdigest() != self.text_hash:
            raise ValueError("speech timing text hash differs from its text snapshot")
        if (self.source_in_us is None) != (self.source_out_us is None):
            raise ValueError("speech timing source bounds must be paired")
        if self.source_in_us is not None and self.source_out_us <= self.source_in_us:
            raise ValueError("speech timing source bounds must increase")
        return self


class TurnV2(_Value):
    id: str = Field(min_length=1)
    speaker: str = ""
    text: str = Field(min_length=1)
    start_frame: int | None = Field(default=None, ge=0)
    end_frame: int | None = Field(default=None, gt=0)
    words: list[WordV2] = Field(default_factory=list)
    speech_placement_id: str | None = Field(default=None, min_length=1)
    speech_timing: SpeechTimingV2 | None = None
    timing_basis: Literal["estimated", "manual", "aligned"] = "estimated"

    @model_validator(mode="after")
    def valid_bounds(self) -> "TurnV2":
        if (self.start_frame is None) != (self.end_frame is None):
            raise ValueError("turn bounds must be paired")
        if self.start_frame is not None and self.end_frame <= self.start_frame:
            raise ValueError("turn bounds must increase")
        if self.timing_basis != "estimated" and self.start_frame is None:
            raise ValueError("manual or aligned turn timing needs bounds")
        if self.timing_basis == "aligned" and not self.words:
            raise ValueError("aligned turn timing needs word spans")
        return self


class BeatV2(_Value):
    id: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    duration_frames: int = Field(gt=0)
    timing_mode: Literal["auto", "fixed"] = "auto"
    speech_pace: Literal["slow", "normal", "fast"] | None = None
    extra_pause_seconds: float | None = Field(default=None, ge=0)
    duration_source_placement_id: str | None = Field(default=None, min_length=1)
    turns: list[TurnV2] = Field(default_factory=list)

    @model_validator(mode="after")
    def contained_times(self) -> "BeatV2":
        if self.duration_source_placement_id is not None and (self.timing_mode != "auto" or self.turns):
            raise ValueError("duration source requires an auto beat without dialogue")
        for turn in self.turns:
            if turn.end_frame is not None and turn.end_frame > self.duration_frames:
                raise PydanticCustomError(
                    "turn_exceeds_beat", "turn {turn_id} exceeds beat {beat_id}",
                    {"turn_id": turn.id, "beat_id": self.id},
                )
            for word in turn.words:
                if turn.start_frame is None or turn.end_frame is None:
                    raise ValueError(f"word {word.id} needs aligned turn bounds")
                if word.start_frame < turn.start_frame or word.end_frame > turn.end_frame:
                    raise ValueError(f"word {word.id} exceeds turn {turn.id}")
        return self


class ResourceV2(_Value):
    id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    node_revision: int | None = Field(default=None, ge=1)
    media_kind: Literal["image", "video", "audio"]
    media_path: str = Field(min_length=1)
    media_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    duration_us: int = Field(ge=0)
    has_audio: bool = False

    @field_validator("media_path")
    @classmethod
    def project_relative(cls, value: str) -> str:
        if value.startswith("/") or "\\" in value or any(part in {"", ".", ".."} for part in value.split("/")):
            raise ValueError("media_path must be project-relative")
        return value


class SpeechTakeV2(_Value):
    id: str = Field(min_length=1)
    beat_id: str
    resource_id: str
    turn_ids: list[str]
    origin_placement_id: str
    source_in_us: int = Field(default=0, ge=0)
    source_out_us: int | None = Field(default=None, gt=0)
    speed_num: int = Field(default=1, gt=0)
    speed_den: int = Field(default=1, gt=0)
    lead_in_frames: int = Field(default=0, ge=0)
    volume: float = Field(default=1, ge=0, le=1)


class SpeechCueV2(_Value):
    id: str = Field(min_length=1)
    turn_id: str
    take_id: str
    text: str
    source_start_us: int | None = Field(default=None, ge=0)
    source_end_us: int | None = Field(default=None, gt=0)
    timing_source: Literal["asr", "srt_import", "manual"] = "asr"
    timing_status: Literal["draft", "ready", "stale", "review"] = "draft"
    word_ids: list[str] = Field(default_factory=list)
    evidence_ref: ExactArtifactRefV2 | None = None


class AnchorV2(_Value):
    kind: Literal["program_frame", "beat", "turn", "word", "selection", "placement", "take", "cue", "text"]
    frame: int | None = Field(default=None, ge=0)
    ref_id: str | None = Field(default=None, min_length=1)
    edge: Literal["start", "end"] | None = None
    offset_frames: int = 0
    char_offset: int | None = Field(default=None, ge=0)
    text_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    affinity: Literal["before", "after"] | None = None

    @model_validator(mode="after")
    def valid_shape(self) -> "AnchorV2":
        if self.kind == "program_frame":
            if (self.frame is None or self.ref_id is not None or self.edge is not None or self.offset_frames
                    or self.char_offset is not None or self.text_hash is not None or self.affinity is not None):
                raise PydanticCustomError("invalid_program_frame_anchor", "program_frame anchor needs only frame")
        elif self.kind == "text":
            if (self.frame is not None or self.edge is not None or self.ref_id is None
                    or self.char_offset is None or self.text_hash is None or self.affinity is None):
                raise PydanticCustomError("invalid_text_anchor", "text anchor needs turn, character offset, text hash and affinity")
        elif (self.frame is not None or self.ref_id is None or self.edge is None
              or self.char_offset is not None or self.text_hash is not None or self.affinity is not None):
            raise PydanticCustomError("invalid_semantic_anchor", "semantic anchor needs ref_id and edge")
        return self


class SelectionV2(_Value):
    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    start: AnchorV2
    end: AnchorV2

    @model_validator(mode="after")
    def source_anchors_only(self) -> "SelectionV2":
        if self.start.kind in {"selection", "placement"} or self.end.kind in {"selection", "placement"}:
            raise ValueError(f"selection {self.id} must anchor to the work, beat, turn, or word")
        return self


class PlacementV2(_Value):
    id: str = Field(min_length=1)
    kind: Literal["visual", "audio", "subtitle", "graphic"]
    role: Literal["primary", "overlay", "speech", "narration", "bgm", "sfx", "dialogue", "effect"]
    track_id: str = Field(min_length=1)
    description: str = ""
    resource_id: str | None = None
    turn_id: str | None = None
    speech_take_id: str | None = None
    source_audio: bool | None = None
    start: AnchorV2
    end: AnchorV2
    source_in_us: int = Field(default=0, ge=0)
    source_out_us: int | None = Field(default=None, gt=0)
    speed_num: int = Field(default=1, gt=0)
    speed_den: int = Field(default=1, gt=0)
    loop: bool = False
    z_order: int = 0
    x: int = Field(default=0, ge=0)
    y: int = Field(default=0, ge=0)
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    fit: Literal["stretch", "cover", "contain"] = "cover"
    opacity: float = Field(default=1, ge=0, le=1)
    volume: float = Field(default=1, ge=0, le=1)
    text: str | None = None
    graphic: GraphicEffect | None = None
    subtitle_x: int | None = Field(default=None, ge=0)
    subtitle_y: int | None = Field(default=None, ge=0)
    font_size: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def valid_role(self) -> "PlacementV2":
        if self.role not in PLACEMENT_ROLES[self.kind]:
            raise PydanticCustomError(
                "invalid_placement_role", "{placement_id} has an invalid {kind} role",
                {"placement_id": self.id, "kind": self.kind},
            )
        if self.kind == "graphic" and (self.graphic is None or self.resource_id is not None):
            raise ValueError(f"graphic {self.id} needs an effect specification and no media resource")
        if self.kind != "graphic" and self.graphic is not None:
            raise ValueError(f"{self.id} graphic specification requires a graphic placement")
        if self.source_out_us is not None and self.source_out_us <= self.source_in_us:
            raise ValueError(f"{self.id} has an invalid source range")
        if self.kind == "subtitle" and not self.turn_id and not (self.text or "").strip():
            raise ValueError(f"subtitle {self.id} needs a turn or text")
        if (self.subtitle_x is None) != (self.subtitle_y is None):
            raise ValueError(f"subtitle {self.id} position needs both coordinates")
        if self.kind != "subtitle" and (self.subtitle_x is not None or self.font_size is not None):
            raise ValueError(f"{self.id} subtitle style requires a subtitle placement")
        return self


class NodeTargetV2(_Value):
    canvas_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    node_revision: int = Field(ge=1)
    goal: str = Field(min_length=1)


class ProvenanceV2(_Value):
    operation_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    requirements: dict[str, Any] | None = None
    requirements_fingerprint: str | None = None
    requirements_provenance: dict[str, Any] | None = None
    blueprint_ref: ExactArtifactRefV2 | None = None
    source_plan_ref: ExactArtifactRefV2 | None = None
    branch_source_ref: ExactArtifactRefV2 | None = None
    author: str | None = None
    node_target: NodeTargetV2 | None = None
    locked_turns: list[dict[str, str]] | None = None


class ProductionPlanV2(_Value):
    schema_version: Literal[2]
    authoring_stage: Literal["outline", "arranged"] | None = None
    goal: str = Field(min_length=1)
    output: OutputV2
    beats: list[BeatV2] = Field(default_factory=list)
    selections: list[SelectionV2] = Field(default_factory=list)
    resources: list[ResourceV2] = Field(default_factory=list)
    placements: list[PlacementV2] = Field(default_factory=list)
    speech_takes: list[SpeechTakeV2] = Field(default_factory=list)
    speech_cues: list[SpeechCueV2] = Field(default_factory=list)
    provenance: ProvenanceV2 = Field(default_factory=ProvenanceV2)

    @model_validator(mode="after")
    def unique_ids_and_references(self) -> "ProductionPlanV2":
        for label, values in (
            ("take", [row.id for row in self.speech_takes]),
            ("take origin", [row.origin_placement_id for row in self.speech_takes]),
            ("cue", [row.id for row in self.speech_cues]),
            ("beat", [row.id for row in self.beats]),
            ("turn", [turn.id for beat in self.beats for turn in beat.turns]),
            ("word", [word.id for beat in self.beats for turn in beat.turns for word in turn.words]
             + [word.id for beat in self.beats for turn in beat.turns if turn.speech_timing for word in turn.speech_timing.words]),
            ("selection", [row.id for row in self.selections]),
            ("resource", [row.id for row in self.resources]),
            ("placement", [row.id for row in self.placements]),
        ):
            duplicate = next((key for key, count in Counter(values).items() if count > 1), None)
            if duplicate is not None:
                raise ValueError(f"duplicate {label} id: {duplicate}")
        resources = {row.id: row for row in self.resources}
        placements = {row.id: row for row in self.placements}
        for beat in self.beats:
            if beat.duration_source_placement_id is None:
                continue
            placement = placements.get(beat.duration_source_placement_id)
            if placement is None or placement.loop or placement.kind not in {"visual", "audio"}:
                raise ValueError(f"duration source for beat {beat.id} requires a non-looping media placement")
            if (placement.start.kind != "beat" or placement.start.ref_id != beat.id or placement.start.edge != "start"
                    or placement.end.kind != "beat" or placement.end.ref_id != beat.id or placement.end.edge != "end"
                    or placement.start.offset_frames or placement.end.offset_frames):
                raise ValueError(f"duration source for beat {beat.id} must span that beat")
            resource = resources.get(placement.resource_id)
            if resource is not None:
                if (placement.kind == "visual" and resource.media_kind != "video"
                        or placement.kind == "audio" and resource.media_kind not in {"audio", "video"}):
                    raise ValueError(f"duration source for beat {beat.id} requires video or audio media")
                source_out = placement.source_out_us or resource.duration_us
                if not placement.source_in_us < source_out <= resource.duration_us:
                    raise ValueError(f"duration source for beat {beat.id} exceeds its media")
        turns = {turn.id for beat in self.beats for turn in beat.turns}
        refs = {
            "take": {row.id for row in self.speech_takes}, "cue": {row.id for row in self.speech_cues},
            "beat": {row.id for row in self.beats}, "turn": turns,
            "text": turns,
            "word": {word.id for beat in self.beats for turn in beat.turns for word in turn.words}
            | {word.id for beat in self.beats for turn in beat.turns if turn.speech_timing for word in turn.speech_timing.words},
            "selection": {row.id for row in self.selections},
            "placement": {row.id for row in self.placements},
        }
        takes = {row.id: row for row in self.speech_takes}
        for take in self.speech_takes:
            beat = next((row for row in self.beats if row.id == take.beat_id), None)
            resource = resources.get(take.resource_id)
            if beat is None or resource is None or not set(take.turn_ids) <= {turn.id for turn in beat.turns}:
                raise ValueError(f"take {take.id} references missing beat, resource, or turn")
            if not take.source_in_us < (take.source_out_us or resource.duration_us) <= resource.duration_us:
                raise ValueError(f"take {take.id} has invalid source interval")
        from .speech_take_order import validate_speech_take_order
        validate_speech_take_order(self)
        assigned_turns = [turn_id for take in self.speech_takes for turn_id in take.turn_ids]
        if len(assigned_turns) != len(set(assigned_turns)):
            raise ValueError("a turn must belong to one speech take")
        for cue in self.speech_cues:
            take = takes.get(cue.take_id)
            if take is None or cue.turn_id not in take.turn_ids:
                raise ValueError(f"cue {cue.id} references missing take or turn")
            if cue.timing_status == "ready":
                if cue.evidence_ref is None or cue.source_start_us is None or cue.source_end_us is None:
                    raise ValueError(f"ready cue {cue.id} needs timing evidence")
                if not take.source_in_us <= cue.source_start_us < cue.source_end_us <= (take.source_out_us or resources[take.resource_id].duration_us):
                    raise ValueError(f"cue {cue.id} exceeds its take")
            if not set(cue.word_ids) <= refs["word"]:
                raise ValueError(f"cue {cue.id} references missing words")
        for row in self.selections:
            for anchor in (row.start, row.end):
                if anchor.kind != "program_frame" and anchor.ref_id not in refs[anchor.kind]:
                    raise ValueError(f"selection {row.id} references missing {anchor.kind} {anchor.ref_id}")
        for row in self.placements:
            if row.speech_take_id is not None:
                if row.speech_take_id not in takes:
                    raise ValueError(f"placement {row.id} references missing take")
                if row.kind != "visual" or row.resource_id != takes[row.speech_take_id].resource_id:
                    raise ValueError(f"placement {row.id} must display its speech take resource")
            if row.resource_id is not None and row.resource_id not in resources:
                raise ValueError(f"placement {row.id} references missing resource {row.resource_id}")
            if row.turn_id is not None and row.turn_id not in turns:
                raise ValueError(f"placement {row.id} references missing turn {row.turn_id}")
            for anchor in (row.start, row.end):
                if anchor.kind != "program_frame" and anchor.ref_id not in refs[anchor.kind]:
                    raise ValueError(f"placement {row.id} references missing {anchor.kind} {anchor.ref_id}")
        from .production_audio_sources import validate_plan_audio_sources

        validate_plan_audio_sources(self.model_dump(mode="json", exclude_none=True))
        return self


def normalize_draft_subtitle_positions(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Use the normal subtitle layout when AI supplies only one valid coordinate."""
    result = deepcopy(payload)
    normalized_ids: list[str] = []
    placements = result.get("placements")
    if not isinstance(placements, list):
        return result, normalized_ids
    for row in placements:
        if not isinstance(row, dict) or row.get("kind") != "subtitle":
            continue
        x, y = row.get("subtitle_x"), row.get("subtitle_y")
        if (x is None) == (y is None):
            continue
        coordinate = y if x is None else x
        if type(coordinate) is not int or coordinate < 0:
            continue
        row.pop("subtitle_x", None)
        row.pop("subtitle_y", None)
        normalized_ids.append(str(row.get("id", "")))
    return result, normalized_ids


def production_plan_draft_json_schema() -> dict[str, Any]:
    """Derive the model's authorable JSON contract from its persisted model."""
    schema = ProductionPlanV2.model_json_schema()
    # Keep constraints on authorable fields from the persisted model. Measured
    # media, speech timing and source bindings belong to later canvas actions.
    allowed: dict[str, set[str]] = {
        "ProductionPlanV2": {"schema_version", "goal", "output", "beats", "selections",
                             "resources", "placements", "provenance"},
        "BeatV2": {"id", "purpose", "duration_frames", "timing_mode", "turns",
                   "speech_pace", "extra_pause_seconds"},
        "TurnV2": {"id", "speaker", "text", "start_frame", "end_frame", "timing_basis",
                   "speech_placement_id"},
        "AnchorV2": {"kind", "frame", "ref_id", "edge", "offset_frames", "char_offset",
                     "affinity"},
        "PlacementV2": {"id", "kind", "role", "track_id", "description", "turn_id",
                        "start", "end", "loop", "z_order", "x", "y", "width",
                        "height", "fit", "opacity", "volume", "text", "graphic",
                        "subtitle_x", "subtitle_y", "font_size", "source_audio"},
    }

    def project(name: str, definition: dict[str, Any]) -> None:
        fields = allowed.get(name)
        if fields is not None:
            definition["properties"] = {key: value for key, value in definition["properties"].items()
                                        if key in fields}
            definition["required"] = [key for key in definition.get("required", []) if key in fields]

    project("ProductionPlanV2", schema)
    for name, definition in schema["$defs"].items():
        if isinstance(definition, dict) and "properties" in definition:
            project(name, definition)
    # The host initializes a missing duration for an auto spoken draft before
    # validating or persisting the native plan. Other beats still need one.
    schema["$defs"]["BeatV2"]["required"] = [
        field for field in schema["$defs"]["BeatV2"].get("required", [])
        if field != "duration_frames"
    ]
    schema["properties"]["resources"] = {"type": "array", "maxItems": 0}
    schema["properties"]["provenance"] = {
        "type": "object", "properties": {}, "additionalProperties": False,
    }
    schema["properties"]["beats"]["minItems"] = 1
    schema["required"] = sorted(set(schema.get("required", [])) | {
        "schema_version", "goal", "output", "beats", "resources", "placements", "provenance",
    })
    schema["$defs"]["TurnV2"]["properties"]["timing_basis"]["enum"] = ["estimated"]
    schema["$defs"]["AnchorV2"]["properties"]["kind"]["enum"] = [
        "program_frame", "beat", "turn", "selection", "placement", "text",
    ]
    anchor = schema["$defs"]["AnchorV2"]
    anchor["allOf"] = [
        {"if": {"properties": {"kind": {"const": "program_frame"}}},
         "then": {"required": ["frame"], "properties": {
             "frame": {"type": "integer", "minimum": 0},
             "ref_id": {"type": "null"}, "edge": {"type": "null"},
             "offset_frames": {"const": 0}, "char_offset": {"type": "null"},
             "affinity": {"type": "null"}}}},
        {"if": {"properties": {"kind": {"const": "beat"}}},
         "then": {"required": ["ref_id"], "anyOf": [
             {"required": ["edge"], "properties": {"edge": {"enum": ["start", "end"]}}},
             {"required": ["offset_frames"], "properties": {
                 "edge": {"type": "null"},
                 "offset_frames": {"type": "integer", "minimum": 0}}},
         ], "properties": {"ref_id": {"type": "string", "minLength": 1},
                           "frame": {"type": "null"},
                           "char_offset": {"type": "null"},
                           "affinity": {"type": "null"}}}},
        {"if": {"properties": {"kind": {"enum": ["turn", "selection", "placement"]}}},
         "then": {"required": ["ref_id", "edge"], "properties": {
             "ref_id": {"type": "string", "minLength": 1},
             "edge": {"enum": ["start", "end"]},
             "frame": {"type": "null"}, "char_offset": {"type": "null"},
             "affinity": {"type": "null"}}}},
        {"if": {"properties": {"kind": {"const": "text"}}},
         "then": {"required": ["ref_id", "char_offset", "affinity"],
                  "properties": {"ref_id": {"type": "string", "minLength": 1},
                                 "char_offset": {"type": "integer", "minimum": 0},
                                 "affinity": {"enum": ["before", "after"]},
                                 "frame": {"type": "null"},
                                 "edge": {"type": "null"}}}},
    ]
    placement = schema["$defs"]["PlacementV2"]
    placement["required"] = [name for name in placement.get("required", []) if name != "role"]
    for kind, roles in PLACEMENT_ROLES.items():
        placement.setdefault("allOf", []).append({
            "if": {"properties": {"kind": {"const": kind}}},
            "then": {"required": [] if kind == "subtitle" else ["role"],
                     "properties": {"role": {"enum": sorted(roles)}}},
        })
    placement["allOf"].append({
        "if": {"properties": {"kind": {"const": "subtitle"}}, "required": ["kind"]},
        "then": {"oneOf": [
            {"required": ["subtitle_x", "subtitle_y"], "properties": {
                "subtitle_x": {"type": "integer", "minimum": 0},
                "subtitle_y": {"type": "integer", "minimum": 0}}},
            {"properties": {"subtitle_x": {"type": "null"},
                            "subtitle_y": {"type": "null"}}},
        ]},
    })

    # Pydantic emits definitions for all persisted fields. Drop definitions
    # no longer reachable from the authoring projection.
    import json
    import re
    reachable: set[str] = set()
    pending = [schema]
    while pending:
        current = pending.pop()
        for match in re.findall(r'"\$ref":\s*"#\/\$defs\/([^\"]+)"',
                                json.dumps({key: val for key, val in current.items() if key != "$defs"})):
            if match not in reachable:
                reachable.add(match)
                pending.append(schema["$defs"][match])
    schema["$defs"] = {key: val for key, val in schema["$defs"].items() if key in reachable}

    def strip_display(value: Any) -> None:
        if isinstance(value, dict):
            value.pop("title", None)
            value.pop("default", None)
            for child in value.values():
                strip_display(child)
        elif isinstance(value, list):
            for child in value:
                strip_display(child)

    strip_display(schema)
    return schema


def validate_production_plan_draft_recipe_compatibility(
    recipe: dict[str, Any], *, goal: str | None = None,
) -> None:
    """Reject obvious custom requirements for fields the draft cannot author."""
    schema = recipe.get("output_schema")
    if not isinstance(schema, dict):
        return
    protected = {"speech_takes", "speech_cues", "speech_timing", "words",
                 "speech_take_id", "resource_id",
                 "source_in_us", "source_out_us"}

    def conflicts(fragment: Any) -> bool:
        if isinstance(fragment, list):
            return any(conflicts(item) for item in fragment)
        if not isinstance(fragment, dict):
            return False
        required = fragment.get("required")
        if isinstance(required, list) and protected.intersection(required):
            return True
        properties = fragment.get("properties")
        if isinstance(properties, dict):
            for field, constraint in properties.items():
                if field in {"resources", "speech_takes", "speech_cues"} and isinstance(constraint, dict):
                    if constraint.get("minItems", 0) > 0 or (
                        "const" in constraint and constraint["const"] != []
                    ):
                        return True
                if field == "provenance" and isinstance(constraint, dict):
                    if constraint.get("minProperties", 0) > 0 or constraint.get("required"):
                        return True
                if field in protected and isinstance(constraint, dict) and (
                    "const" in constraint or "enum" in constraint
                ):
                    return True
                if conflicts(constraint):
                    return True
        return any(conflicts(value) for key, value in fragment.items()
                   if key in {"allOf", "anyOf", "oneOf", "items"})

    if conflicts(schema.get("allOf", [])):
        raise ValueError("production_plan_recipe_contract_conflict")
    if goal is not None:
        from jsonschema import Draft202012Validator

        def rejects_goal(fragment: Any) -> bool:
            if not isinstance(fragment, dict):
                return False
            properties = fragment.get("properties")
            constraint = properties.get("goal") if isinstance(properties, dict) else None
            if isinstance(constraint, dict) and not Draft202012Validator(constraint).is_valid(goal):
                return True
            return any(rejects_goal(part) for part in fragment.get("allOf", []))

        if rejects_goal(schema):
            raise ValueError("production_plan_recipe_contract_conflict")


def production_speech_source(payload: dict[str, Any], turn_id: str, placement_id: str | None = None, take_id: str | None = None) -> tuple[TurnV2, PlacementV2, ResourceV2]:
    """Resolve a spoken turn against one bound, audible project resource."""
    plan = ProductionPlanV2.model_validate(payload)
    turn = next((turn for beat in plan.beats for turn in beat.turns if turn.id == turn_id), None)
    take = next((row for row in plan.speech_takes if row.id == take_id), None) if take_id else None
    if take_id:
        if take is None or turn is None or turn.id not in take.turn_ids:
            raise ValueError("speech alignment needs a turn belonging to the saved take")
        if placement_id is not None:
            window = next((row for row in plan.placements if row.id == placement_id), None)
            if placement_id != take.origin_placement_id and (window is None or window.speech_take_id != take.id):
                raise ValueError("placement_id and take_id must map to the same speech instance")
        resource = next((row for row in plan.resources if row.id == take.resource_id), None)
        if resource is None or not (resource.media_kind == "audio" or resource.media_kind == "video" and resource.has_audio):
            raise ValueError("speech take requires audible media")
        from types import SimpleNamespace
        source = SimpleNamespace(**{**take.model_dump(), "id": take.origin_placement_id})
        return turn, source, resource
    placement = next((row for row in plan.placements if row.id == placement_id), None)
    resource = next((row for row in plan.resources if placement and row.id == placement.resource_id), None)
    if turn is None or placement is None or resource is None:
        raise ValueError("speech alignment needs an existing turn and bound placement")
    if turn.speech_placement_id is not None and turn.speech_placement_id != placement_id:
        raise ValueError("speech alignment placement differs from the saved turn binding")
    if placement.turn_id is not None and placement.turn_id != turn_id:
        raise ValueError("speech source placement belongs to another turn, not the current turn")
    audible = (placement.kind == "audio" and placement.role in {"speech", "narration"}
               and (resource.media_kind == "audio" or resource.media_kind == "video" and resource.has_audio))
    audible_video = placement.kind == "visual" and resource.media_kind == "video" and resource.has_audio
    if not (audible or audible_video) or placement.loop:
        raise ValueError("speech alignment requires a non-looping speech track or video with audio")
    if placement.start.kind not in {"program_frame", "beat", "turn"} or placement.end.kind not in {"program_frame", "beat", "turn"}:
        raise ValueError("speech source placement needs a direct time anchor")
    _, resolve, _, _, _, _, beat_ranges, _ = _timing_resolver(plan)
    for beat in plan.beats:
        beat_start, beat_end = beat_ranges[beat.id]
        if any(row.id == turn_id for row in beat.turns):
            if resolve(placement.id, "start") >= beat_end or resolve(placement.id, "end") <= beat_start:
                raise ValueError("speech source placement must overlap the current beat")
            break
    return turn, placement, resource


def production_speech_source_range(payload: dict[str, Any], placement_id: str) -> tuple[int, int]:
    """Return the exact source-media interval played by an audible placement."""
    plan = ProductionPlanV2.model_validate(payload)
    placement = next((row for row in plan.placements if row.id == placement_id), None)
    if placement is None:
        raise ValueError("speech placement is missing")
    resource = next((row for row in plan.resources if row.id == placement.resource_id), None)
    if resource is None:
        raise ValueError("speech placement has no bound resource")
    _, resolve, _, _, _, _, _, speech_take_ranges = _timing_resolver(plan)
    start, end = resolve(placement.id, "start"), resolve(placement.id, "end")
    fps = Fraction(plan.output.fps_num, plan.output.fps_den)
    take = next((take for take in plan.speech_takes if take.id == placement.speech_take_id or not placement.speech_take_id and take.origin_placement_id == placement.id), None)
    if take:
        a, b, out = speech_take_ranges[take.origin_placement_id]
        speed = Fraction(take.speed_num, take.speed_den)
        if not a <= start < end <= b:
            raise ValueError("speech source window exceeds its take")
        source_in = take.source_in_us + round(Fraction(start - a, 1) / fps * 1_000_000 * speed)
        source_out = out if end == b else min(out, take.source_in_us + round(Fraction(end - a, 1) / fps * 1_000_000 * speed))
        return source_in, source_out
    expected_span = Fraction(end - start, 1) / fps * 1_000_000 * Fraction(placement.speed_num, placement.speed_den)
    source_out = speech_take_ranges[placement.id][2] if placement.id in speech_take_ranges and not placement.speech_take_id else placement.source_out_us
    if source_out is None:
        source_out = placement.source_in_us + round(expected_span)
    elif abs(source_out - placement.source_in_us - expected_span) > max(1, Fraction(500_000, 1) / fps * Fraction(placement.speed_num, placement.speed_den)):
        raise ValueError("speech source range does not match its output span and speed")
    if source_out <= placement.source_in_us or source_out > resource.duration_us:
        raise ValueError("speech source range is outside the bound media")
    return placement.source_in_us, source_out


def migrate_production_plan_v1(payload: dict[str, Any]) -> dict[str, Any]:
    """Create a repeatable v2 source; never mutate the saved v1 revision."""
    from .production_plan import ProductionPlan

    old = ProductionPlan.model_validate(payload)
    resources: dict[tuple[str, str], dict[str, Any]] = {}
    beats: list[dict[str, Any]] = []
    placements: list[dict[str, Any]] = []
    speech_takes: list[dict[str, Any]] = []
    speech_cues: list[dict[str, Any]] = []
    for beat in old.beats:
        turns = []
        for index, turn in enumerate(beat.turns):
            start = turn.start_frame if turn.start_frame is not None else beat.duration_frames * index // len(beat.turns)
            end = turn.end_frame if turn.end_frame is not None else beat.duration_frames * (index + 1) // len(beat.turns)
            turn_id = f"turn:{uuid5(NAMESPACE_URL, f'{beat.id}:{index}:turn')}"
            turns.append({"id": turn_id, "speaker": turn.speaker, "text": turn.text,
                          "start_frame": start, "end_frame": end,
                          "timing_basis": "manual" if turn.start_frame is not None else "estimated"})
            placements.append({"id": f"caption:{turn_id}", "kind": "subtitle", "role": "dialogue",
                               "track_id": "subtitles", "turn_id": turn_id,
                               "start": {"kind": "turn", "ref_id": turn_id, "edge": "start"},
                               "end": {"kind": "turn", "ref_id": turn_id, "edge": "end"}})
        beats.append({"id": beat.id, "purpose": beat.purpose, "duration_frames": beat.duration_frames, "turns": turns})
        for kind, cues in (("visual", beat.visuals), ("audio", beat.audio)):
            for cue in cues:
                resource_id = None
                if cue.media is not None:
                    key = (cue.media.node_id, cue.media.media_hash)
                    resource_id = f"resource:{uuid5(NAMESPACE_URL, ':'.join(key))}"
                    resources[key] = {"id": resource_id, **cue.media.model_dump(mode="json")}
                start = cue.start_frame or 0
                end = cue.end_frame or beat.duration_frames
                placement_kind = "graphic" if kind == "visual" and cue.role == "graphic" else kind
                placement_role = "effect" if placement_kind == "graphic" else cue.role
                placements.append({"id": f"{placement_kind}:{beat.id}:{cue.id}", "kind": placement_kind, "role": placement_role,
                                   "track_id": f"{placement_kind}:{placement_role}", "description": cue.description,
                                   **({"resource_id": resource_id} if resource_id else {}),
                                   **({"graphic": cue.graphic.model_dump(mode="json", exclude_none=True)} if placement_kind == "graphic" else {}),
                                   "start": {"kind": "beat", "ref_id": beat.id, "edge": "start", "offset_frames": start},
                                   "end": {"kind": "beat", "ref_id": beat.id, "edge": "end", "offset_frames": end - beat.duration_frames},
                                   **({"z_order": 0 if cue.role == "primary" else 1} if kind == "visual" else {})})
        # A v1 dialogue with one full-beat main video uses that video's original
        # sound. Preserve the whole take for ASR; the estimated beat length is
        # not an audio crop. Multiple pictures or separate planned speech need
        # an explicit source choice instead.
        main_sources = [cue for cue in beat.visuals if cue.role == "primary"
                        and cue.media is not None and cue.media.media_kind == "video" and cue.media.has_audio
                        and (cue.start_frame or 0) == 0 and (cue.end_frame or beat.duration_frames) == beat.duration_frames]
        separate_speech = any(cue.role in {"speech", "narration"} for cue in beat.audio)
        single_main = sum(cue.role == "primary" for cue in beat.visuals) == 1
        if turns and single_main and len(main_sources) == 1 and not separate_speech:
            source = main_sources[0]
            origin = f"visual:{beat.id}:{source.id}"
            placement = next(row for row in placements if row["id"] == origin)
            take_id = f"take:{beat.id}:{origin}"
            speech_takes.append({"id": take_id, "beat_id": beat.id, "resource_id": placement["resource_id"],
                                 "turn_ids": [turn["id"] for turn in turns], "origin_placement_id": origin,
                                 "source_in_us": 0, "source_out_us": None,
                                 "speed_num": 1, "speed_den": 1, "lead_in_frames": 0})
            placement["speech_take_id"] = take_id
            for turn in turns:
                turn["speech_placement_id"] = origin
                speech_cues.append({"id": f"cue:{turn['id']}", "turn_id": turn["id"], "take_id": take_id,
                                    "text": turn["text"], "source_start_us": None, "source_end_us": None,
                                    "timing_source": "asr", "timing_status": "draft", "word_ids": []})
    migrated = {"schema_version": 2, "goal": old.goal,
                "output": {"width": old.output.width, "height": old.output.height,
                           "fps_num": old.output.fps, "fps_den": 1},
                "beats": beats, "resources": list(resources.values()), "placements": placements,
                "speech_takes": speech_takes, "speech_cues": speech_cues,
                "provenance": {}}
    return ProductionPlanV2.model_validate(migrated).model_dump(mode="json", exclude_none=True)


def _rebase_migrated_plan_anchors(plan: ProductionPlanV2) -> ProductionPlanV2:
    """Resolve old v1-generated global cue frames as beat-local relationships."""
    if plan.provenance.source_plan_ref is None:
        return plan
    cursor = 0
    ranges: dict[str, tuple[int, int]] = {}
    for beat in plan.beats:
        ranges[beat.id] = (cursor, cursor + beat.duration_frames)
        cursor += beat.duration_frames
    updates: dict[str, tuple[AnchorV2, AnchorV2]] = {}
    by_longest_id = sorted(plan.beats, key=lambda beat: len(beat.id), reverse=True)
    for row in plan.placements:
        if row.start.kind != "program_frame" or row.end.kind != "program_frame":
            continue
        beat = next((candidate for candidate in by_longest_id if row.id.startswith(f"{row.kind}:{candidate.id}:")), None)
        if beat is None:
            continue
        beat_start, beat_end = ranges[beat.id]
        if not beat_start <= row.start.frame < row.end.frame <= beat_end:
            continue
        updates[row.id] = (
            AnchorV2(kind="beat", ref_id=beat.id, edge="start", offset_frames=row.start.frame - beat_start),
            AnchorV2(kind="beat", ref_id=beat.id, edge="end", offset_frames=row.end.frame - beat_end),
        )
    if not updates:
        return plan
    upgraded = plan.model_copy(deep=True)
    for row in upgraded.placements:
        if row.id in updates:
            row.start, row.end = updates[row.id]
    return upgraded


def _speech_take(
    beat: BeatV2, turn: TurnV2, placements: dict[str, PlacementV2],
    resources: dict[str, ResourceV2], fps: Fraction,
) -> tuple[PlacementV2, int, int, int] | None:
    """Return a current semantic speaking take and its rounded program length."""
    timing = turn.speech_timing
    if timing is None or beat.timing_mode == "fixed" or (
        turn.speech_placement_id is not None and turn.speech_placement_id != timing.placement_id
    ) or timing.text != turn.text or hashlib.sha256(turn.text.encode("utf-8")).hexdigest() != timing.text_hash:
        return None
    placement = placements.get(timing.placement_id)
    resource = resources.get(placement.resource_id) if placement is not None else None
    if placement is None or resource is None or resource.media_hash != timing.media_hash or placement.loop:
        return None
    audible = placement.kind == "audio" and placement.role in {"speech", "narration"} and (
        resource.media_kind == "audio" or resource.media_kind == "video" and resource.has_audio
    ) or placement.kind == "visual" and resource.media_kind == "video" and resource.has_audio
    if not audible or placement.turn_id is not None and placement.turn_id != turn.id:
        return None
    for anchor, edge in ((placement.start, "start"), (placement.end, "end")):
        if anchor.kind not in {"beat", "turn"} or anchor.ref_id != (beat.id if anchor.kind == "beat" else turn.id) or anchor.edge != edge:
            return None
    if placement.start.offset_frames < 0 or placement.end.offset_frames:
        return None
    source_out = placement.source_out_us if placement.source_out_us is not None else resource.duration_us
    if source_out <= placement.source_in_us or source_out > resource.duration_us:
        return None
    frames = round(Fraction(source_out - placement.source_in_us, 1_000_000) * fps / Fraction(placement.speed_num, placement.speed_den))
    if frames < 1:
        return None
    return placement, source_out, frames, placement.start.offset_frames


def _timing_resolver(plan: ProductionPlanV2):
    from .authored_text_timing import locate_text_boundary

    plan = _rebase_migrated_plan_anchors(plan)
    edges: dict[tuple[str, str, str], int] = {}
    estimated_turns: set[str] = set()
    aligned_turns: set[str] = set()
    aligned_word_cues: dict[str, list[dict[str, int]]] = {}
    text_contexts: dict[str, tuple[list[dict[str, Any]], int | None, int | None, int, int, int, int, str]] = {}
    turns_by_id = {turn.id: turn for beat in plan.beats for turn in beat.turns}
    source_turns: list[TurnV2] = []
    turn_beats: dict[str, str] = {}
    beat_ranges: dict[str, tuple[int, int]] = {}
    speech_take_ranges: dict[str, tuple[int, int, int]] = {}
    placements_by_id = {row.id: row for row in plan.placements}
    resources_by_id = {row.id: row for row in plan.resources}
    fps = Fraction(plan.output.fps_num, plan.output.fps_den)
    from .production_timing import project_speech_mainline
    mainline = project_speech_mainline(plan)
    edges.update(mainline["edges"])
    beat_ranges.update(mainline["beat_ranges"])
    speech_take_ranges.update(mainline["speech_take_ranges"])
    duration = mainline["duration_frames"]
    for take in plan.speech_takes:
        take_start = edges.get(("take", take.id, "start"))
        resource = resources_by_id.get(take.resource_id)
        if take_start is None or resource is None:
            continue
        for turn_id in take.turn_ids:
            turn = turns_by_id[turn_id]
            ready_cues = [cue for cue in plan.speech_cues
                          if cue.take_id == take.id and cue.turn_id == turn_id
                          and cue.timing_status == "ready"
                          and cue.source_start_us is not None and cue.source_end_us is not None
                          and ("cue", cue.id, "start") in edges]
            if len(ready_cues) != 1:
                continue
            cue = ready_cues[0]
            if cue.text != turn.text:
                continue
            timing = turn.speech_timing
            if timing is not None and timing.text == turn.text and timing.media_hash == resource.media_hash \
                    and timing.placement_id == take.origin_placement_id:
                accepted_ids = set(cue.word_ids)
                accepted_words = [word.model_dump(mode="json") for word in timing.words
                                  if word.id in accepted_ids and ("word", word.id, "start") in edges]
            elif cue.timing_source in {"srt_import", "manual"} and not cue.word_ids:
                # A reviewed sentence cue proves its two edges, even when no
                # word-level recognition was ever recorded for this take.
                accepted_words = []
            else:
                continue
            text_contexts[turn_id] = (accepted_words, cue.source_start_us, cue.source_end_us,
                                      take_start, take.source_in_us, take.speed_den, take.speed_num,
                                      "manual" if cue.timing_source == "manual" else "measured")
    for beat in plan.beats:
        for turn in beat.turns:
            turn_beats[turn.id] = beat.id
            if turn.timing_basis == "estimated":
                estimated_turns.add(turn.id)
            if turn.speech_timing is not None:
                source_turns.append(turn)
    for cue in mainline["speech_cues"]:
        if cue["start_frame"] is not None:
            estimated_turns.discard(cue["turn_id"])
            aligned_turns.add(cue["turn_id"])
    base_edges = edges.copy()
    pinned_speech_placements: dict[tuple[str, str], int] = {}
    for take in plan.speech_takes:
        placement = placements_by_id.get(take.origin_placement_id)
        if placement is not None and placement.speech_take_id is None:
            a, b, _ = speech_take_ranges[take.origin_placement_id]
            pinned_speech_placements[(placement.id, "start")] = a
            pinned_speech_placements[(placement.id, "end")] = b

    for turn in source_turns:
        if any(turn.id in take.turn_ids for take in plan.speech_takes):
            continue
        timing = turn.speech_timing
        assert timing is not None
        placement = placements_by_id.get(timing.placement_id)
        resource = resources_by_id.get(placement.resource_id) if placement is not None else None
        if ((turn.speech_placement_id is not None and turn.speech_placement_id != timing.placement_id)
            or hashlib.sha256(turn.text.encode("utf-8")).hexdigest() != timing.text_hash
            or resource is None or resource.media_hash != timing.media_hash
            or not (placement.kind == "audio" and placement.role in {"speech", "narration"}
                    or placement.kind == "visual" and resource.media_kind == "video" and resource.has_audio)
            or placement.loop):
            continue

        def simple_edge(anchor: AnchorV2) -> int | None:
            if anchor.kind == "program_frame":
                return anchor.frame
            if anchor.kind not in {"beat", "turn"}:
                return None
            base = base_edges.get((anchor.kind, anchor.ref_id, anchor.edge))
            return base + anchor.offset_frames if base is not None else None

        take_range = speech_take_ranges.get(placement.id)
        start, end = (take_range[0], take_range[1]) if take_range is not None else (simple_edge(placement.start), simple_edge(placement.end))
        if start is None or end is None or not 0 <= start < end <= duration:
            continue
        speed = Fraction(placement.speed_num, placement.speed_den)
        source_out = take_range[2] if take_range is not None else placement.source_out_us
        if source_out is None:
            source_out = placement.source_in_us + round(Fraction(end - start, 1) / fps * 1_000_000 * speed)
        # Recognition evidence can cover the full media even when the edit uses
        # a shorter interval. The timed words themselves must still fit the edit.
        mapped_words: list[tuple[SourceWordV2, int, int]] = []
        for word in timing.words:
            if word.start_us < placement.source_in_us or word.end_us > source_out:
                break
            word_start = start + round(Fraction(word.start_us - placement.source_in_us, 1_000_000) / speed * fps)
            word_end = start + round(Fraction(word.end_us - placement.source_in_us, 1_000_000) / speed * fps)
            if not start <= word_start < word_end <= end:
                break
            mapped_words.append((word, word_start, word_end))
        if len(mapped_words) != len(timing.words):
            continue
        beat_id = turn_beats[turn.id]
        beat_start, beat_end = edges[("beat", beat_id, "start")], edges[("beat", beat_id, "end")]
        spoken_start = min(item[1] for item in mapped_words)
        spoken_end = max(item[2] for item in mapped_words)
        if not beat_start <= spoken_start < spoken_end <= beat_end:
            continue
        for word, word_start, word_end in mapped_words:
            edges[("word", word.id, "start")] = word_start
            edges[("word", word.id, "end")] = word_end
        aligned_word_cues[turn.id] = [
            {"start_char": word.start_char, "end_char": word.end_char,
             "start_frame": word_start, "end_frame": word_end}
            for word, word_start, word_end in mapped_words
            if word.start_char is not None and word.end_char is not None
        ]
        text_contexts[turn.id] = ([word.model_dump(mode="json") for word, _, _ in mapped_words],
                                  None, None, start, placement.source_in_us,
                                  placement.speed_den, placement.speed_num, "measured")
        pinned_speech_placements[(placement.id, "start")] = start
        pinned_speech_placements[(placement.id, "end")] = end
        edges[("turn", turn.id, "start")] = spoken_start
        edges[("turn", turn.id, "end")] = spoken_end
        estimated_turns.discard(turn.id)
        aligned_turns.add(turn.id)
    def text_anchor_frame(anchor: AnchorV2) -> tuple[int, str]:
        turn = turns_by_id[anchor.ref_id]
        if hashlib.sha256(turn.text.encode("utf-8")).hexdigest() != anchor.text_hash:
            raise ValueError(f"text anchor for {turn.id} is stale")
        if anchor.char_offset > len(turn.text):
            raise ValueError(f"text anchor for {turn.id} exceeds current text")
        context = text_contexts.get(turn.id)
        if context is None:
            raise ValueError(f"text anchor for {turn.id} needs accepted speech evidence")
        words, cue_start, cue_end, program_start, source_in, speed_den, speed_num, cue_quality = context
        found = locate_text_boundary(text=turn.text, char_offset=anchor.char_offset,
                                     affinity=anchor.affinity, words=words,
                                     cue_start_us=cue_start, cue_end_us=cue_end,
                                     cue_quality=cue_quality)
        if found["source_us"] is None:
            raise ValueError(f"text anchor for {turn.id}: {found['issue_code']}")
        frame = program_start + round(Fraction(found["source_us"] - source_in, 1_000_000)
                                      * fps * speed_den / speed_num) + anchor.offset_frames
        return frame, found["quality"]

    selection_rows: list[dict[str, Any]] = []
    selection_issues: list[dict[str, str]] = list(mainline["issues"])
    for selection in plan.selections:
        def selection_frame(anchor: AnchorV2) -> tuple[int, str]:
            if anchor.kind == "text":
                return text_anchor_frame(anchor)
            value = (anchor.frame if anchor.kind == "program_frame" else
                     edges[(anchor.kind, anchor.ref_id, anchor.edge)] + anchor.offset_frames)
            return value, "estimated" if anchor.kind == "turn" and anchor.ref_id in estimated_turns else "measured"

        try:
            start, start_quality = selection_frame(selection.start)
            end, end_quality = selection_frame(selection.end)
        except (KeyError, ValueError) as exc:
            selection_rows.append({**selection.model_dump(mode="json", exclude_none=True),
                                   "start_frame": None, "end_frame": None,
                                   "timing_status": "unresolved", "location_status": "stale" if "stale" in str(exc) else "unresolved",
                                   "timing_quality": None})
            selection_issues.append({"selection_id": selection.id, "code": "unresolved_speech_timing",
                                     "message": f"Selection {selection.id}: {exc}"})
            continue
        valid = 0 <= start < end <= duration
        estimated = any(anchor.kind == "turn" and anchor.ref_id in estimated_turns
                        for anchor in (selection.start, selection.end))
        selection_rows.append({**selection.model_dump(mode="json", exclude_none=True),
                               "start_frame": start, "end_frame": end,
                               "timing_status": "invalid" if not valid else "estimated" if estimated or "estimated" in (start_quality, end_quality) else "resolved",
                               "location_status": "resolved" if valid else "unresolved",
                               "timing_quality": "estimated" if "estimated" in (start_quality, end_quality) else "manual" if "manual" in (start_quality, end_quality) else "measured"})
        if valid:
            edges[("selection", selection.id, "start")] = start
            edges[("selection", selection.id, "end")] = end
        else:
            selection_issues.append({"selection_id": selection.id, "code": "invalid_selection_timing",
                                     "message": f"Selection {selection.id} has an empty or out-of-bounds interval"})
    placements = {row.id: row for row in plan.placements}
    resolved: dict[tuple[str, str], int] = {}
    def resolve(row_id: str, edge: str) -> int:
        requested = (row_id, edge)
        key = requested
        stack: list[tuple[tuple[str, str], int]] = []
        visiting: set[tuple[str, str]] = set()
        while key not in resolved:
            if key in pinned_speech_placements:
                resolved[key] = pinned_speech_placements[key]
                break
            if key in visiting:
                raise ValueError(f"placement anchor cycle at {key[0]}")
            visiting.add(key)
            anchor = getattr(placements[key[0]], key[1])
            if anchor.kind == "placement":
                stack.append((key, anchor.offset_frames))
                key = (anchor.ref_id, anchor.edge)
                continue
            if anchor.kind == "selection" and ("selection", anchor.ref_id, anchor.edge) not in edges:
                raise ValueError(f"placement {key[0]} references invalid selection {anchor.ref_id}")
            if anchor.kind in {"word", "cue", "take"} and (anchor.kind, anchor.ref_id, anchor.edge) not in edges:
                raise ValueError(f"placement {key[0]} needs current speech timing for {anchor.kind} {anchor.ref_id}")
            value = (text_anchor_frame(anchor)[0] if anchor.kind == "text" else
                     anchor.frame if anchor.kind == "program_frame" else
                     edges[(anchor.kind, anchor.ref_id, anchor.edge)] + anchor.offset_frames)
            resolved[key] = value
        value = resolved[key]
        for parent, offset in reversed(stack):
            value += offset
            resolved[parent] = value
        return resolved[requested]

    return duration, resolve, selection_rows, selection_issues, aligned_turns, aligned_word_cues, beat_ranges, speech_take_ranges


def compile_production_plan_v2(payload: dict[str, Any]) -> dict[str, Any]:
    """Resolve anchors against one saved author revision into an immutable frame plan."""
    from .render import validate_manifest

    plan = ProductionPlanV2.model_validate(payload)
    resources = {row.id: row for row in plan.resources}
    duration, resolve, _, selection_issues, aligned_turns, aligned_word_cues, beat_ranges, speech_take_ranges = _timing_resolver(plan)
    if selection_issues:
        raise ValueError(selection_issues[0]["message"])
    if any(cue.timing_status != "ready" for cue in plan.speech_cues) or any(
        not any(cue.turn_id == turn_id and cue.take_id == take.id and cue.timing_status == "ready" for cue in plan.speech_cues)
        for take in plan.speech_takes for turn_id in take.turn_ids
    ):
        raise ValueError("Speech cues need current accepted timing before export")

    layers: list[dict[str, Any]] = []
    audio: list[dict[str, Any]] = []
    subtitles: list[dict[str, Any]] = []
    graphics: list[dict[str, Any]] = []
    used: dict[str, dict[str, Any]] = {}
    fps = Fraction(plan.output.fps_num, plan.output.fps_den)
    turns = {turn.id: turn for beat in plan.beats for turn in beat.turns}
    placements_by_id = {row.id: row for row in plan.placements}
    for beat in plan.beats:
        beat_start, beat_end = beat_ranges[beat.id]
        for turn in beat.turns:
            if not turn.speech_placement_id or any(turn.id in take.turn_ids for take in plan.speech_takes):
                continue
            selected = placements_by_id.get(turn.speech_placement_id)
            if selected is None:
                raise ValueError(f"speech source placement {turn.speech_placement_id} is missing")
            if selected.turn_id is not None and selected.turn_id != turn.id:
                raise ValueError("speech source placement belongs to another turn, not the current turn")
            if resolve(selected.id, "start") >= beat_end or resolve(selected.id, "end") <= beat_start:
                raise ValueError("speech source placement must overlap the current beat")
    spoken_visuals = {turn.speech_placement_id for turn in turns.values() if turn.speech_placement_id}
    explicit_audio_mappings = set()
    for row in plan.placements:
        if row.kind != "audio" or not row.resource_id:
            continue
        audio_start, audio_end = resolve(row.id, "start"), resolve(row.id, "end")
        audio_span = round(Fraction(audio_end - audio_start, 1) / fps * 1_000_000
                           * Fraction(row.speed_num, row.speed_den))
        explicit_audio_mappings.add((row.resource_id, audio_start, audio_end, row.source_in_us,
                                     row.source_out_us if row.source_out_us is not None else row.source_in_us + audio_span,
                                     row.speed_num, row.speed_den))
    used_placement_ids = {row.id for row in plan.placements}
    explicit_take_origins = {take.origin_placement_id for take in plan.speech_takes}
    takes_by_origin = {take.origin_placement_id: take for take in plan.speech_takes}
    takes_by_id = {take.id: take for take in plan.speech_takes}
    for row in plan.placements:
        if row.kind == "audio" and row.id in explicit_take_origins:
            continue
        start, end = resolve(row.id, "start"), resolve(row.id, "end")
        if not 0 <= start < end <= duration:
            raise ValueError(f"placement {row.id} has invalid output interval")
        if row.kind == "audio" and row.role in {"speech", "narration"}:
            for take in plan.speech_takes:
                a, b, _ = speech_take_ranges[take.origin_placement_id]
                if row.resource_id == take.resource_id and start < b and end > a:
                    raise ValueError(f"audio placement {row.id} duplicates speech take {take.id} original audio")
        base = {"placement_id": row.id, "row_id": row.id, "start_frame": start, "end_frame": end}
        if row.kind == "graphic":
            assert row.graphic is not None
            if row.graphic.enter_frames + row.graphic.exit_frames > end - start:
                raise ValueError(f"graphic {row.id} animation exceeds its interval")
            graphic = row.graphic.model_dump(mode="json", exclude_none=True)
            if graphic.get("x", plan.output.width // 2) > plan.output.width or graphic.get("y", plan.output.height // 2) > plan.output.height:
                raise ValueError(f"graphic {row.id} position exceeds output canvas")
            graphics.append({**base, **graphic})
            continue
        if row.kind == "subtitle":
            turn = turns.get(row.turn_id)
            text = row.text or (f"{turn.speaker}：{turn.text}" if turn and turn.speaker else turn.text if turn else "")
            cue = next((cue for cue in plan.speech_cues if row.start.kind == "cue" and row.start.ref_id == cue.id), None)
            subtitle = {**base, "text": text,
                        "timing_basis": ("aligned" if cue.timing_source == "asr" else "manual") if cue else "aligned" if turn and turn.id in aligned_turns else turn.timing_basis if turn else "manual",
                        **({"x": row.subtitle_x, "y": row.subtitle_y} if row.subtitle_x is not None else {}),
                        **({"font_size": row.font_size} if row.font_size is not None else {})}
            if turn and not row.text and turn.id in aligned_turns:
                prefix = len(f"{turn.speaker}：") if turn.speaker else 0
                cues = [{"start_char": cue["start_char"] + prefix, "end_char": cue["end_char"] + prefix,
                         "start_frame": cue["start_frame"], "end_frame": cue["end_frame"]}
                        for cue in aligned_word_cues.get(turn.id, [])
                        if start <= cue["start_frame"] < cue["end_frame"] <= end
                        and cue["end_char"] <= len(turn.text)]
                if cues:
                    subtitle["word_cues"] = cues
            subtitles.append(subtitle)
            continue
        if not row.resource_id:
            raise ValueError(f"placement {row.id} has no bound media")
        resource = resources[row.resource_id]
        if (row.kind == "visual" and resource.media_kind not in {"image", "video"}) or (
            row.kind == "audio" and resource.media_kind != "audio" and not (resource.media_kind == "video" and resource.has_audio)
        ):
            raise ValueError(f"placement {row.id} has incompatible media")
        previous_binding = used.get(resource.id, {})
        used[resource.id] = {"media_id": resource.id, "kind": resource.media_kind,
                             "project_path": resource.media_path, "media_hash": f"sha256:{resource.media_hash}",
                             **({"has_audio": resource.has_audio} if resource.media_kind == "video" else {}),
                             **({"loop": True} if row.loop or previous_binding.get("loop") else {}),
                             **({"purpose": "a-roll"} if row.role == "primary" or previous_binding.get("purpose") == "a-roll" else {})}
        span_us = round(Fraction(end - start, 1) / fps * 1_000_000 * Fraction(row.speed_num, row.speed_den))
        source_out = speech_take_ranges[row.id][2] if row.id in speech_take_ranges else row.source_out_us if row.source_out_us is not None else row.source_in_us + span_us
        source = {"source_in_us": row.source_in_us, "source_out_us": source_out,
                  "speed_num": row.speed_num, "speed_den": row.speed_den}
        take = takes_by_id.get(row.speech_take_id) or takes_by_origin.get(row.id)
        if take:
            a, b, out = speech_take_ranges[take.origin_placement_id]
            if not a <= start < end <= b or resource.id != take.resource_id:
                raise ValueError(f"placement {row.id} exceeds its speech take window")
            speed = Fraction(take.speed_num, take.speed_den)
            source = {"source_in_us": take.source_in_us + round(Fraction(start - a, 1) / fps * 1_000_000 * speed),
                      "source_out_us": out if end == b else min(out, take.source_in_us + round(Fraction(end - a, 1) / fps * 1_000_000 * speed)),
                      "speed_num": take.speed_num, "speed_den": take.speed_den}
        if resource.media_kind != "image" and not row.loop and source["source_out_us"] > resource.duration_us:
            raise ValueError(f"placement {row.id} source range exceeds its media")
        if row.kind == "audio":
            for speech_take in plan.speech_takes:
                take_start, take_end, take_out = speech_take_ranges[speech_take.origin_placement_id]
                if (resource.id == speech_take.resource_id
                        and (start, end) == (take_start, take_end)
                        and (source["source_in_us"], source["source_out_us"],
                             source["speed_num"], source["speed_den"])
                        == (speech_take.source_in_us, take_out,
                            speech_take.speed_num, speech_take.speed_den)):
                    raise ValueError(f"audio placement {row.id} duplicates speech take {speech_take.id} original audio")
        if row.kind == "visual":
            layers.append({**base, "media_id": resource.id, "z_order": row.z_order, "fit": row.fit,
                           "x": row.x, "y": row.y, "width": row.width or plan.output.width,
                           "height": row.height or plan.output.height, "opacity": row.opacity, **source})
            audio_mapping = (resource.id, start, end, source["source_in_us"], source["source_out_us"],
                             source["speed_num"], source["speed_den"])
            if (not take and resource.media_kind == "video" and resource.has_audio
                    and (row.source_audio is True or row.id in spoken_visuals)
                    and audio_mapping not in explicit_audio_mappings):
                audio_id = f"{row.id}:embedded-speech" if row.id in spoken_visuals else f"{row.id}:source-audio"
                suffix = 1
                while audio_id in used_placement_ids:
                    audio_id = f"{row.id}:embedded-speech:{suffix}"
                    suffix += 1
                used_placement_ids.add(audio_id)
                audio.append({**base, "placement_id": audio_id, "media_id": resource.id,
                              "role": "speech" if row.id in spoken_visuals else "sfx",
                              "volume": row.volume, **source})
        else:
            audio.append({**base, "media_id": resource.id, "role": row.role, "volume": row.volume, **source})
    for take in plan.speech_takes:
        resource = resources[take.resource_id]
        if not resource.has_audio and resource.media_kind != "audio":
            raise ValueError(f"speech take {take.id} has no audible source")
        start, end, out = speech_take_ranges[take.origin_placement_id]
        used[resource.id] = {"media_id": resource.id, "kind": resource.media_kind,
                             "project_path": resource.media_path, "media_hash": f"sha256:{resource.media_hash}",
                             **({"has_audio": resource.has_audio} if resource.media_kind == "video" else {})}
        audio.append({"placement_id": f"{take.id}:speech", "row_id": take.id, "media_id": resource.id,
                      "start_frame": start, "end_frame": end, "role": "speech", "volume": take.volume,
                      "source_in_us": take.source_in_us, "source_out_us": out,
                      "speed_num": take.speed_num, "speed_den": take.speed_den})
    manifest = {"manifest_version": 4,
                "output_spec": {"width": plan.output.width, "height": plan.output.height,
                                "fps_num": plan.output.fps_num, "fps_den": plan.output.fps_den,
                                "duration_frames": duration, "background": plan.output.background},
                "media_bindings": list(used.values()), "layers": layers,
                "audio_events": audio, "subtitle_events": subtitles, "graphic_events": graphics,
                "manual_overrides": [], "source_versions": []}
    return validate_manifest(manifest)


def resolve_production_placement_timing(payload: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
    """Project author timing with the renderer's resolver, without requiring media."""
    plan = ProductionPlanV2.model_validate(payload)
    duration, resolve, *_ = _timing_resolver(plan)
    rows = []
    for placement in plan.placements:
        row = {'id': placement.id, 'kind': placement.kind, 'role': placement.role,
               'start_frame': None, 'end_frame': None, 'timing_status': 'unresolved'}
        try:
            start, end = resolve(placement.id, 'start'), resolve(placement.id, 'end')
            row.update(start_frame=start, end_frame=end,
                       timing_status='resolved' if 0 <= start < end <= duration else 'invalid')
        except (KeyError, ValueError) as exc:
            row['message'] = str(exc)
        rows.append(row)
    return duration, rows


def project_production_plan_v2(payload: dict[str, Any], *, project_dir: Path) -> dict[str, Any]:
    """Resolve incomplete tracks and check project media without rendering.

    File digests are deduplicated within one authorized project request. Private
    media caches are never shared across projects or tenants.
    """
    plan = ProductionPlanV2.model_validate(payload)
    duration, resolve, selection_rows, selection_issues, aligned_turns, _, beat_ranges, speech_take_ranges = _timing_resolver(plan)
    root = Path(project_dir).resolve()
    resources = {resource.id: resource for resource in plan.resources}
    turns = {turn.id: turn for beat in plan.beats for turn in beat.turns}
    digests: dict[Path, str] = {}
    statuses: dict[str, tuple[str, str, str]] = {}
    rows, issues = [], list(selection_issues)
    fps = Fraction(plan.output.fps_num, plan.output.fps_den)
    takes_by_origin = {take.origin_placement_id: take for take in plan.speech_takes}
    takes_by_id = {take.id: take for take in plan.speech_takes}
    for placement in plan.placements:
        effective_take = takes_by_id.get(placement.speech_take_id) or takes_by_origin.get(placement.id)
        if effective_take:
            take = effective_take
            a, b, out = speech_take_ranges[take.origin_placement_id]
            try:
                start, end = resolve(placement.id, "start"), resolve(placement.id, "end")
                speed = Fraction(take.speed_num, take.speed_den)
                placement = placement.model_copy(update={
                    "source_in_us": take.source_in_us + round(Fraction(start - a, 1) / fps * 1_000_000 * speed),
                    "source_out_us": out if end == b else min(out, take.source_in_us + round(Fraction(end - a, 1) / fps * 1_000_000 * speed)),
                    "speed_num": take.speed_num, "speed_den": take.speed_den, "volume": take.volume,
                })
            except (KeyError, ValueError):
                pass
        row = placement.model_dump(mode="json", exclude_none=True)
        row.update(start_frame=None, end_frame=None, timing_status="unresolved", resource_status="ready")
        row_issues = []

        def issue(code: str, message: str):
            item = {"placement_id": placement.id, "code": code, "message": message}
            if placement.resource_id:
                item["resource_id"] = placement.resource_id
            row_issues.append(item)

        try:
            start, end = resolve(placement.id, "start"), resolve(placement.id, "end")
            if not 0 <= start < end <= duration:
                raise ValueError("output interval is empty or outside the work")
            row.update(start_frame=start, end_frame=end, timing_status="resolved")
            if effective_take:
                take = effective_take
                a, b, _ = speech_take_ranges[take.origin_placement_id]
                if not a <= start < end <= b:
                    issue("invalid_take_window", "Visual window exceeds its speech take")
            if any(anchor.kind == "turn" and turns[anchor.ref_id].timing_basis == "estimated" and anchor.ref_id not in aligned_turns
                   for anchor in (placement.start, placement.end)):
                row["timing_status"] = "estimated"
        except (ValueError, KeyError) as exc:
            issue("invalid_timing", str(exc))
        if placement.kind == "visual" and (
            placement.x + (placement.width or plan.output.width) > plan.output.width
            or placement.y + (placement.height or plan.output.height) > plan.output.height
        ):
            issue("invalid_layout", "Placement geometry exceeds the output canvas")
        if placement.kind == "subtitle" and placement.subtitle_x is not None and (
            placement.subtitle_x > plan.output.width or placement.subtitle_y > plan.output.height
        ):
            issue("invalid_subtitle_position", "Subtitle position exceeds the output canvas")
        resource = resources.get(placement.resource_id)
        if placement.kind not in {"subtitle", "graphic"}:
            if resource is None:
                row["resource_status"] = "unbound"
                issue("unbound_media", "Bind media to this placement")
            else:
                if resource.id not in statuses:
                    candidate = (root / resource.media_path).resolve()
                    if not candidate.is_relative_to(root):
                        statuses[resource.id] = ("invalid", "invalid_media_path", "Media path escapes the authorized project")
                    elif not candidate.is_file():
                        statuses[resource.id] = ("missing", "missing_media", "Bound project media is missing")
                    else:
                        if candidate not in digests:
                            digest = hashlib.sha256()
                            with candidate.open("rb") as stream:
                                for chunk in iter(lambda: stream.read(1_048_576), b""):
                                    digest.update(chunk)
                            digests[candidate] = digest.hexdigest()
                        statuses[resource.id] = (("ready", "", "") if digests[candidate] == resource.media_hash else
                                                ("stale", "stale_media", "Media differs from the saved resource hash"))
                status, code, message = statuses[resource.id]
                row["resource_status"] = status
                if code:
                    issue(code, message)
                compatible = ((placement.kind == "visual" and resource.media_kind in {"image", "video"}) or
                              (placement.kind == "audio" and (resource.media_kind == "audio" or resource.media_kind == "video" and resource.has_audio)))
                if not compatible:
                    row["resource_status"] = "invalid"
                    issue("incompatible_media", "Media kind does not match this placement")
                if row["start_frame"] is not None:
                    span = Fraction(row["end_frame"] - row["start_frame"], 1) / fps * 1_000_000 * Fraction(placement.speed_num, placement.speed_den)
                    out = speech_take_ranges[placement.id][2] if placement.id in speech_take_ranges and not effective_take else placement.source_out_us if placement.source_out_us is not None else placement.source_in_us + round(span)
                    tolerance = max(1, Fraction(500_000, 1) / fps * Fraction(placement.speed_num, placement.speed_den))
                    if out <= placement.source_in_us or abs(out - placement.source_in_us - span) > tolerance:
                        issue("invalid_source_range", "Source range does not match output span and speed")
                    if resource.media_kind != "image" and not placement.loop and out > resource.duration_us:
                        issue("source_range_exceeds_media", "Source range exceeds the bound media duration")
        row["issues"] = row_issues
        rows.append(row)
        issues.extend(row_issues)
    from .production_timing import project_speech_mainline
    mainline = project_speech_mainline(plan)
    return {"speech_takes": mainline["speech_takes"], "speech_cues": mainline["speech_cues"],
            "output_spec": {**plan.output.model_dump(mode="json"), "duration_frames": duration},
            "beats": [{**beat.model_dump(mode="json", exclude_none=True),
                       "start_frame": beat_ranges[beat.id][0], "end_frame": beat_ranges[beat.id][1]}
                      for beat in plan.beats],
            "selections": selection_rows,
            "resources": [resource.model_dump(mode="json", exclude_none=True) for resource in plan.resources],
            "placements": rows, "issues": issues, "ready": not issues}
