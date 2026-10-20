"""Typed, editable production declarations and their AI draft entry point."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .graphic_effects import GraphicEffect
from novelvideo.llm_instrumentation import (
    log_production_plan_diagnostic,
    production_plan_diagnostic_context,
)
from .production_plan_draft_stages import DraftStageEvent
from .production_audio_sources import SPEECH_SOURCE_TIMING_GUIDANCE, SpeechSourceTimingError
from .production_plan_repair import (
    apply_plan_repair,
    build_plan_repair_scope,
    plan_repair_issues,
    plan_repair_json_schema,
)


_SOURCE_EVIDENCE_GUIDANCE = (
    "原片分析事件（包括抽帧/关键帧窗口）是用于理解原片的观察证据，"
    "不等于复刻视频必须生成同样数量的镜头、beat、素材或单镜时长。"
    "事件数量可以作为导演编排的参考；镜头数量和编排由 Agent 根据新目标、台词、动作、反应、节奏和可用素材判断，"
    "可以采用相近数量，也可以合并、拆分、跳过或重组，不要把分析事件数量自动转换为镜头数量或固定时长。"
    "1.5–2.5 秒等原片时长只作为节奏观察，是否沿用由 Agent 判断；生成素材仍按 4–15 秒及模型能力规划。"
    "source_evidence.events 是原片分析得到的观察窗口和时间证据，"
    "不是目标视频必须照搬的镜头清单，也不要求一一对应的 beat 或 placement。"
    "原片事件可以合并、拆分、跳过或重新组织；"
    "只有 requirements.primary_shots 明确指定时才校验镜头数量，"
    "未指定时不要从原片事件、关键帧或分析段落数量推导镜头数量。"
)


async def generate_recipe_text(**kwargs: Any) -> str:
    """Load model execution only when drafting, keeping contracts portable."""
    from novelvideo.freezone.recipe_runtime import generate_recipe_text as generate

    return await generate(**kwargs)


def get_recipe_for_runtime(**kwargs: Any) -> dict[str, Any]:
    from novelvideo.freezone.recipe_runtime import get_recipe_for_runtime as get_recipe

    return get_recipe(**kwargs)


def validate_structured_recipe_output(recipe: dict[str, Any], content: str) -> Any:
    from novelvideo.freezone.recipe_runtime import validate_structured_recipe_output as validate

    return validate(recipe, content)


def _repairable_plan_issue(issue: dict[str, Any]) -> bool:
    if issue.get("code") == "speech_source_timing_invalid":
        return True
    return (
        issue.get("code") == "value_error"
        and str(issue.get("path") or "").endswith("/graphic/emphasis")
        and "emphasis" in str(issue.get("message") or "").lower()
    )


def _repair_context_for_plan(draft: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    context = scope.get("repair_context")
    result: dict[str, Any] = {
        "issues": scope.get("issues", []),
        "fragments": scope.get("fragments", {}),
    }
    if isinstance(context, dict):
        result["repair_context"] = context
        beat_id = context.get("beat_id")
        if isinstance(beat_id, str):
            beat = next((row for row in draft.get("beats", [])
                         if isinstance(row, dict) and row.get("id") == beat_id), None)
            if isinstance(beat, dict):
                result["target_beat"] = beat
    graphic_targets = []
    for issue in scope.get("issues", []):
        path = issue.get("path") if isinstance(issue, dict) else None
        if not isinstance(path, str) or not path.endswith("/graphic/emphasis"):
            continue
        parts = path.split("/")
        if len(parts) != 4 or parts[1] != "placements" or not parts[2].isdigit():
            continue
        placements = draft.get("placements", [])
        index = int(parts[2])
        if not isinstance(placements, list) or index >= len(placements):
            continue
        placement = placements[index]
        graphic = placement.get("graphic") if isinstance(placement, dict) else None
        if isinstance(placement, dict) and isinstance(graphic, dict):
            graphic_targets.append({"placement_id": placement.get("id"),
                                    "text": graphic.get("text"),
                                    "path": path})
    if graphic_targets:
        result["graphic_targets"] = graphic_targets
    return result


async def _repair_generated_plan_once(
    candidate: dict[str, Any], error: BaseException, *, topic: str,
    recipe: dict[str, Any], requirements: dict[str, Any] | None,
    locked_turns: list[dict[str, str]] | None, timing_assumptions: list[dict[str, str]] | None,
    username: str, deadline: float, policy: Any,
) -> dict[str, Any] | None:
    issues = plan_repair_issues(error)
    if not issues or not all(_repairable_plan_issue(issue) for issue in issues):
        return None
    scope = build_plan_repair_scope(candidate, issues)
    if not scope["allowed_paths"]:
        return None
    repair_context = _repair_context_for_plan(candidate, scope)
    log_production_plan_diagnostic(
        "production_plan.repair_started", stage="repairing",
        repair_issue_count=len(issues), validation_issues=issues,
    )
    remaining = deadline - time.monotonic()
    if remaining <= 2:
        log_production_plan_diagnostic(
            "production_plan.repair_failed", stage="repairing",
            error_code="production_plan_deadline_exceeded",
        )
        return None
    repair_policy = replace(policy, max_output_tokens=min(policy.max_output_tokens, 2048))
    repair_deadline = deadline - 1
    prompt = (
        "只修复当前作品方案中列出的局部校验错误。"
        "只返回符合 repair schema 的 base_digest 和 changes；不要输出完整方案。"
        "只能修改 allowed_paths 中的字段，不能修改台词、段落、产品方向、素材资源或输出规格。"
        "对于 speech_source_timing_invalid，只把发声素材的起止锚点调整到所属 beat 的有效范围内；"
        "对于 graphic emphasis 错误，只能把 emphasis 改成当前 graphic.text 的连续子串，或设为 null。"
    )
    try:
        with production_plan_diagnostic_context(draft_stage="repair"):
            content = await generate_recipe_text(
                username=username, recipe_id="reference-production-plan", node_kind="text",
                node_prompt=prompt, user_goal=topic,
                upstream_text=json.dumps(repair_context, ensure_ascii=False, separators=(",", ":")),
                structured_upstream=repair_context,
                structured_output_contract=plan_repair_json_schema(scope),
                plan_policy=repair_policy, plan_deadline=repair_deadline,
            )
            response = _parse_stage_json(content)
            if not isinstance(response, dict):
                raise ValueError("plan_repair_response_must_be_object")
            repaired = apply_plan_repair(candidate, response, scope=scope)
            validated = validate_generated_production_plan(
                repaired, topic=topic, recipe=recipe, requirements=requirements,
                locked_turns=locked_turns, timing_assumptions=None,
            )
        log_production_plan_diagnostic(
            "production_plan.repair_applied", stage="repairing",
            repair_issue_count=len(issues), validation_issues=issues,
        )
        return validated
    except Exception as exc:
        log_production_plan_diagnostic(
            "production_plan.repair_failed", stage="repairing", error=exc,
            error_code=str(exc), repair_issue_count=len(issues),
        )
        return None


def _parse_stage_json(content: str) -> Any:
    log_production_plan_diagnostic(
        "production_plan.validation_started", stage="validating",
        output_characters=len(content),
    )
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        log_production_plan_diagnostic(
            "production_plan.validation_failed", stage="validating",
            error_code="json_parse_failed", error=exc, response_received=True,
        )
        raise ValueError("json_parse_failed") from exc


def _log_stage_validation_failure(exc: ValueError) -> None:
    log_production_plan_diagnostic(
        "production_plan.validation_failed", stage="validating",
        error_code=str(exc) if str(exc) in {
            "production_plan_stage_validation_failed", "plan_validation_failed",
        } else "production_plan_stage_validation_failed",
        error=exc, response_received=True,
        validation_issue_count=getattr(exc, "count", 1),
        validation_issues=getattr(exc, "issues", []),
    )


def _requirement_validation_issues(checks: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Reduce deterministic requirement failures to safe, actionable locations."""
    paths = {
        "primary_shot_timing_invalid": "/placements",
        "primary_shot_count_mismatch": "/requirements/primary_shots",
        "duration_mismatch": "/requirements/duration_us",
        "output_requirement_mismatch": "/requirements/output_requirements",
        "locked_dialogue_evidence_missing": "/requirements/dialogue",
        "locked_dialogue_changed": "/requirements/dialogue",
    }
    issues: list[dict[str, str]] = []
    for row in checks:
        if not isinstance(row, dict) or row.get("status") != "failed":
            continue
        code = row.get("code")
        if not isinstance(code, str) or not code:
            continue
        path = row.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            path = paths.get(code, "/requirements")
        issues.append({"code": code, "path": path})
    return issues[:20]


class _ProductionRequirementValidationError(ValueError):
    """Keep deterministic requirement failures available to bounded diagnostics."""

    def __init__(self, checks: list[dict[str, Any]]) -> None:
        self.issues = _requirement_validation_issues(checks)
        self.count = len(self.issues)
        super().__init__(json.dumps(checks, ensure_ascii=False))


class PlanValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PlanOutput(PlanValue):
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: int = Field(gt=0, le=60)


class PlanMediaReference(PlanValue):
    node_id: str = Field(min_length=1, max_length=128)
    media_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    media_path: str = Field(min_length=1)
    media_kind: Literal["image", "video", "audio"]
    duration_us: int = Field(ge=0)
    has_audio: bool = False

    @field_validator("media_path")
    @classmethod
    def project_relative_path(cls, value: str) -> str:
        if value.startswith("/") or "\\" in value or any(part in {"", ".", ".."} for part in value.split("/")):
            raise ValueError("media_path must be a project-relative path")
        return value


class PlanTurn(PlanValue):
    speaker: str = Field(default="", max_length=80)
    text: str = Field(min_length=1, max_length=2000)
    start_frame: int | None = Field(default=None, ge=0)
    end_frame: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def check_range(self) -> "PlanTurn":
        if (self.start_frame is None) != (self.end_frame is None):
            raise ValueError("turn time bounds must be paired")
        if self.start_frame is not None and self.end_frame <= self.start_frame:
            raise ValueError("turn time bounds must be increasing")
        return self


class PlanVisualCue(PlanValue):
    id: str = Field(min_length=1, max_length=128)
    role: Literal["primary", "overlay", "graphic"]
    description: str = Field(default="", max_length=1000)
    media: PlanMediaReference | None = None
    graphic: GraphicEffect | None = None
    start_frame: int | None = Field(default=None, ge=0)
    end_frame: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def check_range(self) -> "PlanVisualCue":
        if self.role == "graphic" and (self.graphic is None or self.media is not None):
            raise ValueError("graphic cue needs a graphic specification and no media")
        if self.role != "graphic" and self.graphic is not None:
            raise ValueError("media visual cannot contain a graphic specification")
        if (self.start_frame is None) != (self.end_frame is None):
            raise ValueError("visual cue time bounds must be paired")
        if self.start_frame is not None and self.end_frame <= self.start_frame:
            raise ValueError("visual cue time bounds must be increasing")
        return self


class PlanAudioCue(PlanValue):
    id: str = Field(min_length=1, max_length=128)
    role: Literal["speech", "narration", "bgm", "sfx"]
    description: str = Field(default="", max_length=1000)
    media: PlanMediaReference | None = None
    start_frame: int | None = Field(default=None, ge=0)
    end_frame: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def check_range(self) -> "PlanAudioCue":
        if (self.start_frame is None) != (self.end_frame is None):
            raise ValueError("audio cue time bounds must be paired")
        if self.start_frame is not None and self.end_frame <= self.start_frame:
            raise ValueError("audio cue time bounds must be increasing")
        return self


class PlanBeat(PlanValue):
    id: str = Field(min_length=1, max_length=128)
    purpose: str = Field(min_length=1, max_length=1000)
    duration_frames: int = Field(gt=0)
    turns: list[PlanTurn] = Field(default_factory=list)
    visuals: list[PlanVisualCue] = Field(default_factory=list)
    audio: list[PlanAudioCue] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_cues(self) -> "PlanBeat":
        ids = [cue.id for cue in (*self.visuals, *self.audio)]
        if len(ids) != len(set(ids)):
            raise ValueError("cue ids must be unique inside a beat")
        for cue in (*self.visuals, *self.audio):
            if cue.end_frame is not None and cue.end_frame > self.duration_frames:
                raise ValueError("cue exceeds its beat duration")
        for turn in self.turns:
            if turn.end_frame is not None and turn.end_frame > self.duration_frames:
                raise ValueError("turn exceeds its beat duration")
        return self


class ProductionPlan(PlanValue):
    schema_version: Literal[1]
    goal: str = Field(min_length=1, max_length=2000)
    output: PlanOutput
    beats: list[PlanBeat] = Field(min_length=1)

    @model_validator(mode="after")
    def check_beats(self) -> "ProductionPlan":
        ids = [beat.id for beat in self.beats]
        if len(ids) != len(set(ids)):
            raise ValueError("beat ids must be unique")
        return self


def compile_production_plan(payload: dict[str, Any]) -> dict[str, Any]:
    """Compile a saved declaration into the same v3 manifest used by the canvas renderer."""
    from .render import validate_manifest

    plan = ProductionPlan.model_validate(payload)
    if any(cue.role == "graphic" for beat in plan.beats for cue in beat.visuals):
        from .production_plan_v2 import compile_production_plan_v2, migrate_production_plan_v1

        return compile_production_plan_v2(migrate_production_plan_v1(plan.model_dump(mode="json", exclude_none=True)))
    if (plan.output.width, plan.output.height, plan.output.fps) != (1080, 1920, 30):
        raise ValueError("当前成片编排支持 1080×1920、30 fps")
    bindings: dict[str, dict[str, Any]] = {}
    layers: list[dict[str, Any]] = []
    audio_events: list[dict[str, Any]] = []
    subtitles: list[dict[str, Any]] = []
    cursor = 0

    def bind(media: PlanMediaReference, *, primary: bool = False) -> str:
        media_id = f"node:{media.node_id}"
        row = {
            "media_id": media_id, "kind": media.media_kind,
            "project_path": media.media_path, "media_hash": f"sha256:{media.media_hash}",
            **({"has_audio": media.has_audio} if media.media_kind == "video" else {}),
        }
        if primary:
            row["purpose"] = "a-roll"
        existing = bindings.get(media_id)
        if existing and any(existing.get(key) != row.get(key) for key in ("kind", "project_path", "media_hash", "has_audio")):
            raise ValueError(f"素材 {media.node_id} 的版本不一致，请重新绑定")
        if existing:
            if primary:
                existing["purpose"] = "a-roll"
        else:
            bindings[media_id] = row
        return media_id

    for beat in plan.beats:
        primaries = sorted(
            (cue for cue in beat.visuals if cue.role == "primary"),
            key=lambda cue: cue.start_frame or 0,
        )
        covered = 0
        for cue in primaries:
            start = cue.start_frame or 0
            end = cue.end_frame or beat.duration_frames
            if start != covered:
                raise ValueError(f"段落 {beat.id} 的主画面必须连续覆盖全段")
            covered = end
        if covered != beat.duration_frames:
            raise ValueError(f"段落 {beat.id} 缺少覆盖全段的主画面")

        for cue in beat.visuals:
            if cue.media is None:
                raise ValueError(f"画面 {cue.id} 尚未绑定素材")
            if cue.media.media_kind not in {"image", "video"}:
                raise ValueError(f"画面 {cue.id} 需要图片或视频")
            media_id = bind(cue.media, primary=cue.role == "primary")
            start = cursor + (cue.start_frame or 0)
            end = cursor + (cue.end_frame or beat.duration_frames)
            span = end - start
            if cue.media.media_kind == "video" and cue.media.duration_us * 30 < span * 1_000_000:
                bindings[media_id]["loop"] = True
            layers.append({"row_id": f"visual:{beat.id}:{cue.id}", "media_id": media_id,
                           "start_frame": start, "end_frame": end,
                           "z_order": 0 if cue.role == "primary" else 1, "fit": "cover"})

        for cue in beat.audio:
            if cue.media is None:
                raise ValueError(f"声音 {cue.id} 尚未绑定素材")
            if cue.media.media_kind != "audio" and not (cue.media.media_kind == "video" and cue.media.has_audio):
                raise ValueError(f"声音 {cue.id} 需要音频或带声音的视频")
            media_id = bind(cue.media)
            start = cursor + (cue.start_frame or 0)
            end = cursor + (cue.end_frame or beat.duration_frames)
            if cue.media.duration_us * plan.output.fps < (end - start) * 1_000_000:
                if cue.role in {"speech", "narration"}:
                    raise ValueError(f"声音 {cue.id} 短于编排时长，请按实际说话时长调整")
                if cue.media.media_kind == "audio":
                    bindings[media_id]["loop"] = True
            audio_events.append({"row_id": f"audio:{beat.id}:{cue.id}", "media_id": media_id,
                                 "role": cue.role, "start_frame": start, "end_frame": end,
                                 "volume": 0.3 if cue.role == "bgm" else 1})

        for index, turn in enumerate(beat.turns):
            start = turn.start_frame if turn.start_frame is not None else beat.duration_frames * index // len(beat.turns)
            end = turn.end_frame if turn.end_frame is not None else beat.duration_frames * (index + 1) // len(beat.turns)
            if start >= end:
                raise ValueError(f"段落 {beat.id} 的台词时间不足")
            subtitles.append({"row_id": f"turn:{beat.id}:{index}",
                              "text": f"{turn.speaker}：{turn.text}" if turn.speaker else turn.text,
                              "start_frame": cursor + start, "end_frame": cursor + end})
        cursor += beat.duration_frames

    manifest = {
        "manifest_version": 3,
        "output_spec": {"width": 1080, "height": 1920, "fps": 30,
                        "duration_frames": cursor,
                        "subtitle_safe_area": {"left": 90, "right": 90, "top": 120, "bottom": 180}},
        "media_bindings": list(bindings.values()), "layers": layers,
        "audio_events": audio_events, "subtitle_events": subtitles,
        "manual_overrides": [], "source_versions": [],
    }
    return validate_manifest(manifest)


def _narrative_planning_context(source: dict[str, Any] | None) -> dict[str, Any]:
    """Give the writer transferable story guidance, not a timed shot transcript."""
    if not isinstance(source, dict):
        return {}
    context: dict[str, Any] = {}
    for key in ("summary", "narrative_role"):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            context[key] = value.strip()[:1200]

    structure = source.get("adaptation_structure")
    if isinstance(structure, dict):
        role = structure.get("narrative_role")
        beats = structure.get("reusable_beats")
        abstract = {}
        if isinstance(role, str) and role.strip():
            abstract["narrative_role"] = role.strip()[:500]
        if isinstance(beats, list):
            abstract["reusable_beats"] = [beat.strip()[:120] for beat in beats
                                          if isinstance(beat, str) and beat.strip()][:12]
        if abstract:
            context["adaptation_structure"] = abstract

    choices = source.get("preserve_replace_rules") or source.get("user_choices")
    if isinstance(choices, list):
        rules = [{"type": choice["type"], "text": choice["text"].strip()[:1000]}
                 for choice in choices if isinstance(choice, dict)
                 and isinstance(choice.get("type"), str)
                 and choice["type"] in {"preserve", "replace"}
                 and isinstance(choice.get("text"), str) and choice["text"].strip()]
        if rules:
            context["preserve_replace_rules"] = rules[:10]
    return context


_GRAPHIC_EMPHASIS_GUIDANCE = (
    "graphic.emphasis 可选；填写时必须是 graphic.text 中逐字一致的一个连续子串，"
    "不得填写语义描述、多个不连续关键词或样式指令。"
    "仅在不需要强调时省略 emphasis。"
)


class DraftAnchorIntervalError(ValueError):
    def __init__(self, index: int, placement_id: str):
        self.index = index
        self.placement_id = placement_id
        super().__init__("draft_anchor_interval_invalid")


class DraftSelectionIntervalError(ValueError):
    def __init__(self, index: int, selection_id: str):
        self.index = index
        self.selection_id = selection_id
        super().__init__("draft_anchor_interval_invalid")


class DraftSchemaValidationErrors(ValueError):
    def __init__(self, errors: list[Any]):
        self.errors = errors
        super().__init__("draft_schema_validation_failed")


def _draft_issue_identity(parsed: Any, path: Any, error_row: dict[str, Any] | None = None) -> dict[str, str]:
    """Attach safe entity IDs to a validation path without logging model text."""
    parts = list(path)
    if not isinstance(parsed, dict) or len(parts) < 2 or type(parts[1]) is not int:
        return {}
    if parts[0] == "placements":
        placements = parsed.get("placements")
        if not isinstance(placements, list) or not 0 <= parts[1] < len(placements):
            return {}
        placement = placements[parts[1]]
        placement_id = placement.get("id") if isinstance(placement, dict) else None
        return {"placement_id": placement_id} if isinstance(placement_id, str) else {}
    if parts[0] == "selections":
        selections = parsed.get("selections")
        if not isinstance(selections, list) or not 0 <= parts[1] < len(selections):
            return {}
        selection = selections[parts[1]]
        selection_id = selection.get("id") if isinstance(selection, dict) else None
        return {"selection_id": selection_id} if isinstance(selection_id, str) else {}
    if parts[0] != "beats":
        return {}
    beats = parsed.get("beats")
    if not isinstance(beats, list) or not 0 <= parts[1] < len(beats):
        return {}
    beat = beats[parts[1]]
    if not isinstance(beat, dict):
        return {}
    identity = {"beat_id": beat["id"]} if isinstance(beat.get("id"), str) else {}
    turns = beat.get("turns")
    if (len(parts) >= 4 and parts[2] == "turns" and type(parts[3]) is int
            and isinstance(turns, list) and 0 <= parts[3] < len(turns)):
        turn = turns[parts[3]]
        if isinstance(turn, dict) and isinstance(turn.get("id"), str):
            identity["turn_id"] = turn["id"]
    elif error_row and error_row.get("type") == "turn_exceeds_beat":
        turn_id = (error_row.get("ctx") or {}).get("turn_id")
        if isinstance(turn_id, str) and isinstance(turns, list) and any(
            isinstance(turn, dict) and turn.get("id") == turn_id for turn in turns
        ):
            identity["turn_id"] = turn_id
    return identity


async def _generate_production_plan_stage(
    *, topic: str, blueprint: dict[str, Any] | None, username: str,
    plan: dict[str, Any] | None = None,
    locked_turns: list[dict[str, str]] | None = None,
    output_preferences: dict[str, Any] | None = None,
    requirements: dict[str, Any] | None = None,
    source_timing: dict[str, Any] | None = None,
    on_stage: Callable[[DraftStageEvent], Awaitable[None]] | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Draft story and arrangements, then validate one native editable plan."""
    recipe = get_recipe_for_runtime(username=username, recipe_id="reference-production-plan")
    from .production_plan_v2 import PLACEMENT_ROLES, validate_production_plan_draft_recipe_compatibility
    validate_production_plan_draft_recipe_compatibility(recipe, goal=topic)
    prompt = (
        "依据 draft_outline 的段落、台词与程序分配的 ID，只安排 selections、placements 和 speech_bindings；"
        "不得重写 output、beats、台词或全片方向。台词全文仅保留在 turns，安排中概述画面和声音任务。"
        "一段连续可见表演可由同一个 visual placement 承担多句台词；"
        "在 speech_bindings 中把每个 turn_id 指向发声 placement_id；程序组装后成为 speech_placement_id。"
        "带声视频按需设置 source_audio=true。"
        "独立旁白、单独声音控制、模型能力限制或用户明确要求时才安排 speech/narration 音频；"
        "普通画面无需默认拆独立音轨，真实动作声也可以来自带声视频。"
        "整段画面使用 beat 的 start/end；同段不同镜头使用 beat 的 offset_frames 或 turn、selection 锚点，"
        "不要让所有镜头覆盖整段。语义锚点必须有 ref_id 与 edge。"
        "字幕 kind=subtitle、role=dialogue；图形文字 kind=graphic、role=effect，"
        "graphic.template 可为 question_card、keyword、title_bar。"
        "未存在的资源不要填写 resource_id；不能伪造素材、SRT 或真实语音时间。"
        f"画面 role 取 {'、'.join(sorted(PLACEMENT_ROLES['visual']))}；"
        f"声音 role 取 {'、'.join(sorted(PLACEMENT_ROLES['audio']))}。只输出 JSON。"
    )
    prompt += _SOURCE_EVIDENCE_GUIDANCE + SPEECH_SOURCE_TIMING_GUIDANCE + _GRAPHIC_EMPHASIS_GUIDANCE
    from .production_plan_context import (
        build_production_plan_context, plan_context_char_budget, serialize_plan_context,
    )
    planning_context = build_production_plan_context(
        blueprint=blueprint, source_timing=source_timing, requirements=requirements,
        locked_turns=locked_turns, output_preferences=output_preferences,
    )
    if source_timing is not None:
        prompt += 'source_timing 是真实原片时间证据，可用于规划叙事节奏；不是单次视频素材生成时长。用户的成片硬约束仍以 requirements 为准。'
    if isinstance(output_preferences, dict):
        preferences: dict[str, Any] = {}
        for field in ("aspect_ratio", "video_aspect_ratio"):
            value = output_preferences.get(field)
            if isinstance(value, str) and 0 < len(value) <= 32:
                preferences[field] = value
        clip_seconds = output_preferences.get("source_clip_duration_seconds")
        if type(clip_seconds) in (int, float) and 0 < clip_seconds <= 600:
            preferences["source_clip_duration_seconds"] = clip_seconds
            prompt += (
                "output_preferences.source_clip_duration_seconds 是单条素材片段的时长约束，"
                "不是成片总时长；叙事段落和整片时长按用户目标与故事节奏规划。"
            )
    if locked_turns is not None:
        prompt += "已确认台词必须逐字保留，所有 turns 的说话者、文本和顺序必须与 locked_turns 完全一致；只能补充段落、画面和声音规划。"
    if "source_evidence" in planning_context:
        prompt += ('source_evidence 是实际原片证据；先核对用户要求保留的具体事件、铺垫与落点，'
                   '再安排新内容。观察事实与叙事推断分别对待，不把屏幕文字当作 spoken_range 台词。')
    if requirements is not None:
        prompt += '遵守本次明确的 requirements；未指定的镜头数、时长和画幅由叙事证据决定。semantic 项逐项落实；屏幕字幕与原台词分别处理。'
    upstream_text = serialize_plan_context(planning_context, max_chars=plan_context_char_budget())
    source_events = planning_context.get("source_evidence", {}).get("events", [])
    log_production_plan_diagnostic(
        "production_plan.context_ready", stage="requesting",
        source_event_count=len(blueprint.get("events", [])) if isinstance(blueprint, dict) else 0,
        passed_event_count=len(source_events),
        spoken_event_count=sum(bool(row.get("spoken_range")) for row in source_events),
        context_characters=len(upstream_text),
    )
    from .production_plan_draft_stages import (
        assemble_stage_draft, prepare_outline, outline_to_plan, outline_from_plan,
    )
    from .production_plan_execution import plan_execution_policy, split_plan_stage_tokens
    policy = plan_execution_policy()
    outline_tokens, arrangement_tokens = split_plan_stage_tokens(policy.max_output_tokens)
    started = time.monotonic()
    deadline = min(deadline, started + policy.total_timeout_seconds) if deadline is not None else started + policy.total_timeout_seconds
    remaining = deadline - started
    if remaining <= 0:
        raise TimeoutError("production_plan_deadline_exceeded")
    model_deadline = deadline - min(30.0, remaining * 0.1)

    async def notify(stage: Literal["outline", "arrangement", "finalize"],
                     status: Literal["started", "completed"],
                     payload: dict[str, Any] | None = None) -> None:
        if on_stage is not None:
            await on_stage(DraftStageEvent(stage, status, payload))

    if plan is None:
        await notify("outline", "started")
        with production_plan_diagnostic_context(attempt=1, draft_stage="outline"):
            outline_content = await generate_recipe_text(
                username=username, recipe_id="reference-production-plan", node_kind="text",
                node_prompt=("先规划叙事段落、逐句台词及成片规格。只输出结构与台词阶段 JSON。"
                             "不要安排镜头、画面、声音素材、字幕和时间引用。"
                             "已确认台词必须逐字保持说话者、文本和顺序。"
                             "source_clip_duration_seconds 是单条素材生成时长，不是成片总时长。"
                             "source_timing 是原片时间证据，不是新作品时长指令。"),
                user_goal=topic,
                upstream_text=serialize_plan_context(
                    planning_context, max_chars=plan_context_char_budget()),
                structured_upstream=planning_context,
                plan_policy=replace(policy, max_output_tokens=outline_tokens),
                plan_deadline=model_deadline, production_plan_stage="outline",
            )
            try:
                outline = prepare_outline(_parse_stage_json(outline_content))
                if locked_turns is not None:
                    actual_turns = [{"speaker": turn["speaker"], "text": turn["text"]}
                                    for beat in outline["beats"] for turn in beat["turns"]]
                    expected_turns = [{"speaker": turn.get("speaker", ""), "text": turn["text"]}
                                      for turn in locked_turns]
                    if actual_turns != expected_turns:
                        raise ValueError("plan_validation_failed")
            except ValueError as exc:
                if str(exc) != "json_parse_failed":
                    _log_stage_validation_failure(exc)
                raise
        await notify("outline", "completed", outline)

        draft = outline_to_plan(topic, outline)
        from .production_requirements import check_production_requirements, duration_frame_bounds
        outline_requirements = {key: value for key, value in (requirements or {}).items()
                                if key != "primary_shots"}
        duration_frame_bounds(outline_requirements, fps_num=draft["output"]["fps_num"],
                              fps_den=draft["output"]["fps_den"])
        failed = [row for row in check_production_requirements(
            draft, outline_requirements, project_dir=Path('.'), locked_turns=locked_turns)
                  if row["status"] == "failed"]
        if failed:
            raise ValueError(json.dumps(failed, ensure_ascii=False))
        return draft
    outline = outline_from_plan(plan)
    await notify("arrangement", "started")
    arrangement_context = {**planning_context, "draft_outline": outline}
    outline_characters = len(json.dumps(outline, ensure_ascii=False, separators=(",", ":")))
    with production_plan_diagnostic_context(attempt=1, draft_stage="arrangement"):
        arrangement_content = await generate_recipe_text(
            username=username, recipe_id="reference-production-plan", node_kind="text",
            node_prompt=prompt + "已确定段落和台词见 draft_outline；只能安排 selections、placements 和 speech_bindings，不能重写段落、台词或输出规格。",
            user_goal=topic,
            upstream_text=serialize_plan_context(
                arrangement_context, max_chars=plan_context_char_budget() + outline_characters + 32),
            structured_upstream=arrangement_context,
            plan_policy=replace(policy, max_output_tokens=arrangement_tokens),
            plan_deadline=model_deadline, production_plan_stage="arrangement",
        )
        try:
            arrangement = _parse_stage_json(arrangement_content)
            assembled = assemble_stage_draft(topic, outline, arrangement)
            # The saved revision owns the story and provenance. Only media
            # arrangement and speech bindings come from this request.
            from copy import deepcopy
            merged = deepcopy(plan)
            merged["selections"] = assembled["selections"]
            merged["placements"] = assembled["placements"]
            merged["beats"] = assembled["beats"]
            assembled = merged
        except ValueError as exc:
            if str(exc) != "json_parse_failed":
                _log_stage_validation_failure(exc)
            raise
    await notify("arrangement", "completed", arrangement)

    await notify("finalize", "started")
    with production_plan_diagnostic_context(attempt=1, draft_stage="finalize"):
        validation_input = {**assembled, "provenance": {}}
        validation_input.pop("authoring_stage", None)
        validation_input.pop("speech_takes", None)
        validation_input.pop("speech_cues", None)
        draft = validate_generated_production_plan(
            validation_input, topic=topic, recipe=recipe, requirements=requirements,
            locked_turns=locked_turns, saved_story=True,
        )
    await notify("finalize", "completed")
    # Keep exact saved story/output fields, including authored timing metadata.
    draft["beats"] = assembled["beats"]
    draft["output"] = deepcopy(plan["output"])
    draft["provenance"] = deepcopy(plan.get("provenance", {}))
    draft["authoring_stage"] = "arranged"
    from .contracts import validate_payload
    return validate_payload("production_plan.v2", draft)


async def generate_production_plan_outline(
    *, topic: str, blueprint: dict[str, Any] | None, username: str,
    locked_turns: list[dict[str, str]] | None = None,
    output_preferences: dict[str, Any] | None = None,
    requirements: dict[str, Any] | None = None,
    source_timing: dict[str, Any] | None = None,
    on_stage: Callable[[DraftStageEvent], Awaitable[None]] | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Generate only the independently saved structure and dialogue stage."""
    return await _generate_production_plan_stage(
        topic=topic, blueprint=blueprint, username=username, locked_turns=locked_turns,
        output_preferences=output_preferences, requirements=requirements,
        source_timing=source_timing, on_stage=on_stage, deadline=deadline)


async def generate_production_plan_draft(
    *, topic: str, blueprint: dict[str, Any] | None, username: str,
    locked_turns: list[dict[str, str]] | None = None,
    output_preferences: dict[str, Any] | None = None,
    requirements: dict[str, Any] | None = None,
    source_timing: dict[str, Any] | None = None,
    on_stage: Callable[[DraftStageEvent], Awaitable[None]] | None = None,
    deadline: float | None = None,
    timing_assumptions: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Generate the native editable plan directly, without an intermediate schema."""
    recipe = get_recipe_for_runtime(username=username, recipe_id="reference-production-plan")
    from .production_plan_v2 import PLACEMENT_ROLES, validate_production_plan_draft_recipe_compatibility
    validate_production_plan_draft_recipe_compatibility(recipe, goal=topic)
    prompt = (
        "为目标起草可编辑的短视频制作方案。先确定新作品的叙事推进，"
        "只有一段承担的叙事目的发生变化时才分出新 beat，自由决定数量和时长。"
        "同一叙事目的内的连续台词、说话人切换和镜头切换应放进同一 beat；"
        "不要按单句台词、原片事件编号或原片镜头边界逐项生成 beat。"
        "原片分析事件和关键帧窗口只是理解原片的证据，不等于复刻视频必须生成同样数量的镜头；"
        "事件数量可作为导演编排参考，镜头数量和编排由 Agent 根据新目标、台词、动作、反应、节奏和可用素材判断。"
        "turns 可以为空，一个 beat 可以包含多位说话者和多个画面。"
        "placements 描述所需画面与实际需要单独准备的声音，不填写尚不存在的 resource_id。"
        "先确定每句台词的实际发声来源：可见连续表演优先评估一份带声视频，"
        "同一视频负责的连续台词共用同一个 visual placement，并在 turns 的 speech_placement_id 引用它；"
        "该 visual 可设 source_audio=true。普通画面默认无需原声；无台词但需要真实动作声或环境声时，"
        "可用 visual 的 source_audio=true，不额外虚构 audio placement。"
        "独立旁白、需要单独控制的声音、模型能力限制或用户明确要求时，可用 speech/narration audio placement 承担台词。"
        "根据实际声音任务和来源选择并描述依据，不强制所有视频带声，也不把每句台词做成一份独立音频。"
        "同一段连续表演不要为每句话重复创建画面或声音；台词全文只写在 turns 中，"
        "画面和声音描述概述其素材需求，不复写整段对白。"
        "除语速和独立留白外，可选且有默认值的字段省略即可，不必逐项输出。"
        "台词默认不填 start_frame/end_frame；确需估计时二者必须是所属 beat 内的帧数，"
        "不能把原片全局时间当成新段落内部时间。"
        "整段画面 start={kind:beat,ref_id:b1,edge:start}、end={kind:beat,ref_id:b1,edge:end}；"
        "段内片段可给同一 beat 的起止锚点分别填 offset_frames，省略 edge 就表示从段首计算。"
        "其他语义锚点必须提供 ref_id 和 start/end edge；文字锚点依其字段规则填写。"
        "字幕及不承担这些台词发声的画面可引用 turn 的 start/end。"
        "同一 beat 的独立镜头用 offset_frames 或 turn、selection 锚点安排不同的连续区间，"
        "不能让所有镜头都覆盖整段；"
        f"画面 kind=visual，role 取 {'、'.join(sorted(PLACEMENT_ROLES['visual']))}；"
        f"声音 kind=audio，role 取 {'、'.join(sorted(PLACEMENT_ROLES['audio']))}。"
        "字幕 kind=subtitle 的 role 可省略，填写时只能是 dialogue。"
        "屏幕文字卡片、标题条、关键词强调等不依赖图片视频的效果请写为 kind=graphic、role=effect 的 placement，"
        "并填写 graphic={template,text,emphasis?,enter?,enter_frames?,exit_frames?}；"
        "template 仅可为 question_card、keyword、title_bar，graphic 不需要 media。"
        "graphic.enter 只能取 none、fade、pop；enter_frames 和 exit_frames 为非负整数帧数。"
        "AI 负责台词、段落、语速意图、动作及额外停顿。画面描述写清哪句台词或哪个原片事件触发动作、"
        "人物看谁、何时切镜、是否需要独立留白；边说边做可以并行，同一连续素材可包含多位说话者。"
        "例如：回答产品时展示物品；听到反问后切采访者，短暂停顿后回应；结尾看向手臂并保留对方反应。"
        "关键反转要保留具体铺垫与落点，不只写抽象标签；不要求每句都配置动作。"
        "普通 auto 口播 beat 可省略 duration_frames，"
        "由本地发音测时填充初值。speech_pace 可选 slow/normal/fast；额外独立停顿用 extra_pause_seconds，"
        "请明确选择 speech_pace 和 extra_pause_seconds，后者为 0 也是有效安排；"
        "边说边做不重复计时。固定节奏、无台词或无法测时的 beat 必须明确填写 duration_frames。"
        "已填写的 duration_frames 是创作安排，以 output.fps_num/output.fps_den 为基准；"
        "不要把预估帧数写成真实词时间。后续单次生成视频按 4～15 秒及所选模型能力规划，"
        "成片短镜头可以复用较长生成素材。"
        "参考结构是可迁移的叙事线索，并非必须逐项复刻的段落清单；"
        "优先遵循用户目标与保留、替换规则。台词按 requirements.dialogue.mode 执行，"
        "preserve_source 和 use_confirmed_text 逐字保留 locked_turns，rewrite 才改写台词。不得编造产品事实。只输出 JSON。"
    )
    prompt += _SOURCE_EVIDENCE_GUIDANCE + SPEECH_SOURCE_TIMING_GUIDANCE + _GRAPHIC_EMPHASIS_GUIDANCE
    from .production_plan_context import (
        build_production_plan_context, plan_context_char_budget, serialize_plan_context,
    )
    planning_context = build_production_plan_context(
        blueprint=blueprint, source_timing=source_timing, requirements=requirements,
        locked_turns=locked_turns, output_preferences=output_preferences,
    )
    if source_timing is not None:
        prompt += 'source_timing 是真实原片时间证据，可用于规划叙事节奏；不是单次视频素材生成时长。用户的成片硬约束仍以 requirements 为准。'
    if isinstance(output_preferences, dict):
        preferences: dict[str, Any] = {}
        for field in ("aspect_ratio", "video_aspect_ratio"):
            value = output_preferences.get(field)
            if isinstance(value, str) and 0 < len(value) <= 32:
                preferences[field] = value
        clip_seconds = output_preferences.get("source_clip_duration_seconds")
        if type(clip_seconds) in (int, float) and 0 < clip_seconds <= 600:
            preferences["source_clip_duration_seconds"] = clip_seconds
            prompt += (
                "output_preferences.source_clip_duration_seconds 是单条素材片段的时长约束，"
                "不是成片总时长；叙事段落和整片时长按用户目标与故事节奏规划。"
            )
    if locked_turns is not None:
        prompt += "已确认台词必须逐字保留，所有 turns 的说话者、文本和顺序必须与 locked_turns 完全一致；只能补充段落、画面和声音规划。"
    if "source_evidence" in planning_context:
        prompt += ('source_evidence 是实际原片证据；先核对用户要求保留的具体事件、铺垫与落点，'
                   '再安排新内容。观察事实与叙事推断分别对待，不把屏幕文字当作 spoken_range 台词。')
    if requirements is not None:
        prompt += '遵守本次明确的 requirements；未指定的镜头数、时长和画幅由叙事证据决定。semantic 项逐项落实；屏幕字幕与原台词分别处理。'
    upstream_text = serialize_plan_context(planning_context, max_chars=plan_context_char_budget())
    source_events = planning_context.get("source_evidence", {}).get("events", [])
    log_production_plan_diagnostic(
        "production_plan.context_ready", stage="requesting",
        source_event_count=len(blueprint.get("events", [])) if isinstance(blueprint, dict) else 0,
        passed_event_count=len(source_events),
        spoken_event_count=sum(bool(row.get("spoken_range")) for row in source_events),
        context_characters=len(upstream_text),
    )
    from .production_plan_execution import plan_execution_policy
    policy = plan_execution_policy()
    started = time.monotonic()
    deadline = min(deadline, started + policy.total_timeout_seconds) if deadline is not None else started + policy.total_timeout_seconds
    remaining = deadline - started
    if remaining <= 0:
        raise TimeoutError("production_plan_deadline_exceeded")
    model_deadline = deadline - min(30.0, remaining * 0.1)
    if on_stage is not None:
        await on_stage(DraftStageEvent("full", "started"))
    with production_plan_diagnostic_context(attempt=1, draft_stage="full"):
        content = await generate_recipe_text(
            username=username,
            recipe_id="reference-production-plan",
            node_kind="text",
            node_prompt=prompt,
            user_goal=topic,
            upstream_text=upstream_text,
            structured_upstream=planning_context,
            plan_policy=policy,
            plan_deadline=model_deadline,
        )
        if on_stage is not None:
            await on_stage(DraftStageEvent("finalize", "started"))
        candidate: dict[str, Any] | None = None
        try:
            parsed_candidate = _parse_stage_json(content)
            candidate = parsed_candidate if isinstance(parsed_candidate, dict) else None
            draft = validate_generated_production_plan(
                parsed_candidate, topic=topic, recipe=recipe,
                requirements=requirements, locked_turns=locked_turns,
                timing_assumptions=timing_assumptions,
            )
        except ValueError as exc:
            if candidate is None or str(exc) != "plan_validation_failed":
                raise
            repaired = await _repair_generated_plan_once(
                candidate, exc, topic=topic, recipe=recipe, requirements=requirements,
                locked_turns=locked_turns, timing_assumptions=timing_assumptions,
                username=username, deadline=deadline, policy=policy,
            )
            if repaired is None:
                raise
            draft = repaired
        draft["authoring_stage"] = "arranged"
    if on_stage is not None:
        await on_stage(DraftStageEvent("full", "completed", draft))
    return draft


async def generate_production_plan_arrangement(
    *, plan: dict[str, Any], blueprint: dict[str, Any] | None, username: str,
    locked_turns: list[dict[str, str]] | None = None,
    requirements: dict[str, Any] | None = None,
    source_timing: dict[str, Any] | None = None,
    output_preferences: dict[str, Any] | None = None,
    on_stage: Callable[[DraftStageEvent], Awaitable[None]] | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Arrange media against the exact current saved story revision."""
    if plan.get("resources") or plan.get("speech_takes") or plan.get("speech_cues") or any(
        row.get("resource_id") for row in plan.get("placements", [])
    ):
        raise ValueError("production_plan_has_bound_media")
    return await _generate_production_plan_stage(
        topic=plan["goal"], plan=plan, blueprint=blueprint, username=username,
        requirements=requirements, source_timing=source_timing, locked_turns=locked_turns,
        output_preferences=output_preferences, on_stage=on_stage, deadline=deadline)


def validate_generated_production_plan(
    payload: dict[str, Any], *, topic: str, recipe: dict[str, Any],
    requirements: dict[str, Any] | None = None,
    locked_turns: list[dict[str, str]] | None = None,
    saved_story: bool = False,
    timing_assumptions: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Apply the existing final authoring checks to one assembled draft."""
    content = json.dumps(payload, ensure_ascii=False)
    validation_started = time.monotonic()
    log_production_plan_diagnostic(
        "production_plan.validation_started", stage="validating",
        output_characters=len(content),
    )
    failure_code = "json_parse_failed"
    parsed: Any = None
    try:
        from .contracts import validate_payload
        from .production_plan_draft_normalization import (
            discard_extra_draft_fields, initialize_draft_timing,
            normalize_production_plan_draft,
        )
        from .production_plan_v2 import production_plan_draft_json_schema
        from jsonschema import Draft202012Validator, ValidationError as JsonSchemaValidationError

        parsed = json.loads(content)
        log_production_plan_diagnostic("production_plan.response_parsed", stage="validating")
        failure_code = "plan_validation_failed"
        if not isinstance(parsed, dict):
            raise ValueError("production_plan_draft_must_be_object")
        parsed["goal"] = topic
        extra_fields = []
        if not saved_story:
            raw_beats = {beat.get("id"): beat for beat in parsed.get("beats", [])
                         if isinstance(beat, dict) and isinstance(beat.get("id"), str)}
            parsed, extra_fields = discard_extra_draft_fields(parsed)
            parsed, initialized = initialize_draft_timing(parsed)
            extra_fields += initialized
            if timing_assumptions is not None:
                for change in initialized:
                    if change["code"] != "local_speech_duration_initialized":
                        continue
                    index = int(change["path"].split("/")[2])
                    beat_id = parsed["beats"][index]["id"]
                    original = raw_beats.get(beat_id, {})
                    timing_assumptions.append({
                        "beat_id": beat_id, "duration_basis": "local_speech_estimate",
                        "pace_basis": ("authored" if original.get("speech_pace") is not None else "default_normal"),
                        "pause_basis": ("authored" if original.get("extra_pause_seconds") is not None else "default_zero"),
                    })
        parsed, normalizations = normalize_production_plan_draft(parsed)
        normalizations = extra_fields + normalizations
        if normalizations:
            log_production_plan_diagnostic(
                "production_plan.draft_normalized", stage="validating",
                normalized_count=len(normalizations), validation_issues=normalizations[:20],
            )
        defaulted_subtitles = [row for row in normalizations
                               if row["code"] == "subtitle_position_defaulted"]
        if defaulted_subtitles:
            log_production_plan_diagnostic(
                "production_plan.subtitle_position_defaulted", stage="validating",
                normalized_count=len(defaulted_subtitles),
                validation_issues=defaulted_subtitles[:20],
            )
        if not saved_story:
            schema_errors = list(Draft202012Validator(
                production_plan_draft_json_schema()).iter_errors(parsed))
            if schema_errors:
                schema_errors.sort(key=lambda error: tuple(map(str, error.absolute_path)))
                raise DraftSchemaValidationErrors(schema_errors)
            validate_structured_recipe_output(recipe, json.dumps(parsed, ensure_ascii=False))
        # Arrangement authoring was already checked by assemble_stage_draft.
        # Saved turns may have user timing metadata excluded from the model schema.
        from .authored_text_timing import materialize_text_anchors
        parsed = materialize_text_anchors(parsed)
        draft = validate_payload("production_plan.v2", parsed)
        changed_anchor_ids = {row["placement_id"] for row in normalizations
                              if row["code"] == "beat_anchor_start_defaulted"
                              and isinstance(row.get("placement_id"), str)}
        if changed_anchor_ids:
            from .production_plan_v2 import resolve_production_placement_timing
            _, timing_rows = resolve_production_placement_timing(draft)
            for index, row in enumerate(timing_rows):
                if row["id"] in changed_anchor_ids and row["timing_status"] != "resolved":
                    raise DraftAnchorIntervalError(index, row["id"])
        changed_selection_indices = {int(row["path"].split("/")[2])
                                     for row in normalizations
                                     if row["code"] == "beat_anchor_start_defaulted"
                                     and row["path"].startswith("/selections/")}
        if changed_selection_indices:
            from .production_plan_v2 import project_production_plan_v2
            selection_rows = project_production_plan_v2(
                draft, project_dir=Path('.'))["selections"]
            for index in sorted(changed_selection_indices):
                row = selection_rows[index]
                if row["timing_status"] not in {"resolved", "estimated"}:
                    raise DraftSelectionIntervalError(index, row["id"])
        from .production_requirements import check_production_requirements, duration_frame_bounds
        duration_frame_bounds(requirements or {}, fps_num=draft['output']['fps_num'],
                              fps_den=draft['output']['fps_den'])
        checks = check_production_requirements(draft, requirements or {}, project_dir=Path('.'),
                                               locked_turns=locked_turns)
        # A new, schema-valid draft remains editable when its creative timing
        # misses the target. The saved revision cannot pass review or execute
        # until the existing requirement gate is satisfied.
        failed = [row for row in checks if row['status'] == 'failed'
                  and (saved_story or row['code'] != 'duration_mismatch')]
        if failed:
            raise _ProductionRequirementValidationError(failed)
        log_production_plan_diagnostic(
            "production_plan.validation_succeeded", stage="validating",
            elapsed_ms=round((time.monotonic() - validation_started) * 1000),
        )
        return draft
    except (ValueError, JsonSchemaValidationError) as exc:
        issue_source = exc.__cause__ or exc
        validation_issues = []
        validation_issue_count = 1
        if isinstance(issue_source, DraftAnchorIntervalError):
            validation_issues = [{"path": f"/placements/{issue_source.index}",
                                  "code": "draft_anchor_interval_invalid",
                                  "placement_id": issue_source.placement_id}]
        elif isinstance(issue_source, DraftSelectionIntervalError):
            validation_issues = [{"path": f"/selections/{issue_source.index}",
                                  "code": "draft_anchor_interval_invalid",
                                  "selection_id": issue_source.selection_id}]
        elif isinstance(issue_source, DraftSchemaValidationErrors):
            validation_issue_count = len(issue_source.errors)
            validation_issues = [
                {"path": "/" + "/".join(map(str, error.absolute_path)),
                 "code": str(error.validator or "schema_validation"),
                 **_draft_issue_identity(parsed, error.absolute_path)}
                for error in issue_source.errors[:20]
            ]
        elif isinstance(issue_source, JsonSchemaValidationError):
            path = list(issue_source.absolute_path)
            validation_issues = [{"path": "/" + "/".join(map(str, path)),
                                  "code": str(issue_source.validator or "schema_validation"),
                                  **_draft_issue_identity(parsed, path)}]
        elif isinstance(issue_source, _ProductionRequirementValidationError):
            validation_issue_count = issue_source.count
            validation_issues = issue_source.issues
        elif callable(getattr(issue_source, "errors", None)):
            error_rows = issue_source.errors()
            validation_issue_count = len(error_rows)
            validation_issues = []
            for row in error_rows[:20]:
                underlying = (row.get("ctx") or {}).get("error")
                if isinstance(underlying, SpeechSourceTimingError):
                    validation_issues.append(underlying.issue)
                else:
                    validation_issues.append({
                        "path": "/" + "/".join(map(str, row.get("loc", ()))),
                        "code": str(row.get("type") or "validation"),
                        **_draft_issue_identity(parsed, row.get("loc", ()), row),
                    })
        log_production_plan_diagnostic(
            "production_plan.validation_failed", stage="validating",
            elapsed_ms=round((time.monotonic() - validation_started) * 1000),
            error_code=failure_code, error=exc, response_received=True,
            validation_issue_count=validation_issue_count,
            validation_issues=validation_issues,
        )
        raise ValueError(failure_code) from exc
