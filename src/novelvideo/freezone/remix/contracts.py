"""Validated, version-one structured remix artifact contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class _Contract(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True, populate_by_name=True)


class ArtifactReference(_Contract):
    artifact_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    revision: int = Field(ge=1)


class ReferenceEvidence(_Contract):
    media_hash: str = Field(min_length=1)
    time_base: str = Field(min_length=1)
    duration_us: int = Field(gt=0)
    cuts: list[dict[str, Any]]
    representative_frames: list[dict[str, Any]]
    transcript_ref: ArtifactReference | None = None


class WordSpan(_Contract):
    text: str = Field(min_length=1)
    start_char: int = Field(ge=0)
    end_char: int = Field(ge=1)
    start_us: int = Field(ge=0)
    end_us: int = Field(ge=1)

    @model_validator(mode="after")
    def _validate_half_open_ranges(self) -> "WordSpan":
        if self.end_char <= self.start_char or self.end_us <= self.start_us:
            raise ValueError("word character and time ranges must be non-empty")
        return self


class WordTimeline(_Contract):
    schema_name: Literal["word_timeline.v1"] | None = Field(
        default=None, alias="schema"
    )
    media_ref: ArtifactReference | None = None
    media_hash: str | None = None
    script_ref: ArtifactReference | None = None
    script_revision: int | None = Field(default=None, ge=1)
    language: str = Field(min_length=1)
    status: Literal["completed", "no_speech", "script_mismatch"] = "completed"
    transcript: str | None = None
    confidence_available: bool | None = None
    words: list[WordSpan]
    mismatch: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _validate_status_and_source(self) -> "WordTimeline":
        if self.media_ref is None and not self.media_hash:
            raise ValueError("word timeline requires a media reference or hash")
        if self.status == "completed" and not self.words:
            raise ValueError("completed word timeline requires aligned words")
        if self.status != "completed" and self.words:
            raise ValueError(
                "non-completed word timeline must not publish aligned words"
            )
        return self


class ReferenceBlueprint(_Contract):
    narrative_role: str = Field(min_length=1)
    events: list[dict[str, Any]]
    preserve_replace_rules: list[dict[str, Any]]
    observations: list[dict[str, Any]]
    inferences: list[dict[str, Any]]
    user_choices: list[dict[str, Any]]


class RemixScript(_Contract):
    dialogue: str
    segments: list[dict[str, Any]]
    selections: list[dict[str, Any]]
    blueprint_mappings: list[dict[str, Any]]
    media_requirements: list[dict[str, Any]]


class OutputFrameEvent(_Contract):
    event_id: str = Field(min_length=1)
    start_frame: int = Field(ge=0)
    end_frame: int = Field(ge=1)

    @model_validator(mode="after")
    def _validate_half_open_frames(self) -> "OutputFrameEvent":
        if self.end_frame <= self.start_frame:
            raise ValueError("output event frames must be non-empty")
        return self


class PerformanceTiming(_Contract):
    schema_name: Literal["performance_timing.v1"] | None = Field(
        default=None, alias="schema"
    )
    script_revision: int = Field(ge=1)
    media_hash: str = Field(min_length=1)
    fps: int = Field(gt=0)
    events: list[OutputFrameEvent]
    unresolved_required_events: list[dict[str, Any]]
    override_conflicts: list[dict[str, Any]]


class CompositionManifest(_Contract):
    output_spec: dict[str, Any]
    media_bindings: list[dict[str, Any]]
    layers: list[dict[str, Any]]
    subtitle_events: list[dict[str, Any]]
    audio_events: list[dict[str, Any]]
    manual_overrides: list[dict[str, Any]]
    source_versions: list[dict[str, Any]]


PayloadModel = (
    ReferenceEvidence
    | WordTimeline
    | ReferenceBlueprint
    | RemixScript
    | PerformanceTiming
    | CompositionManifest
)

SCHEMA_MODELS: dict[str, type[PayloadModel]] = {
    "reference_evidence.v1": ReferenceEvidence,
    "word_timeline.v1": WordTimeline,
    "reference_blueprint.v1": ReferenceBlueprint,
    "remix_script.v1": RemixScript,
    "performance_timing.v1": PerformanceTiming,
    "composition_manifest.v1": CompositionManifest,
}
V1_SCHEMAS = frozenset(SCHEMA_MODELS)


def validate_payload(schema: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Return JSON-safe payload data after checking its declared v1 schema."""
    model = SCHEMA_MODELS.get(schema)
    if model is None:
        raise ValueError("Unsupported remix artifact schema")
    if not isinstance(payload, dict):
        raise ValueError(f"{schema} payload must be an object")
    try:
        validated = model.model_validate(payload).model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
    except ValidationError as exc:
        raise ValueError(f"{schema} payload is invalid: {exc}") from exc
    if schema == "composition_manifest.v1":
        from .render import validate_manifest

        validate_manifest(validated)
    return validated


class RemixArtifactEnvelope(_Contract):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    schema_name: Literal[
        "reference_evidence.v1",
        "word_timeline.v1",
        "reference_blueprint.v1",
        "remix_script.v1",
        "performance_timing.v1",
        "composition_manifest.v1",
    ] = Field(alias="schema")
    revision: int = Field(ge=1)
    project_id: str = Field(min_length=1)
    created_by: str
    content_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    upstream_refs: list[dict[str, Any]]
    producer_version: str = Field(min_length=1)
    payload_path: str = Field(pattern=r"^[1-9][0-9]*\.payload\.json$")
