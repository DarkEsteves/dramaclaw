"""Bounded, task-specific explanations of production plan validation failures."""

from __future__ import annotations

import re
from typing import Any


_PLAN_TASKS = {
    "remix_generate_production_plan_draft",
    "remix_generate_production_plan_arrangement",
}
_SAFE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
_SAFE_PATH = re.compile(r"/[A-Za-z0-9_/-]{0,95}\Z")
_SAFE_MESSAGE = re.compile(r"[A-Za-z0-9 _.,;:()\[\]/=_-]{1,160}\Z")
# Codes whose name alone does not tell a reader what to change.
_NEEDS_HINT_CODES = frozenset({"value_error", "validation", "speech_source_kind_invalid",
                               "speech_source_role_invalid", "speech_source_media_unusable"})


def _field(row: dict, name: str, pattern: re.Pattern[str]) -> str | None:
    value = row.get(name)
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def build_plan_failure_summary(record: dict, *, task_id: str) -> dict | None:
    """Copy only allowlisted fields from one sanitized validation event."""
    if (record.get("event") != "production_plan.validation_failed"
            or record.get("error_code") != "plan_validation_failed"
            or not _field({"task_id": task_id}, "task_id", _SAFE_ID)):
        return None
    record_task_id = record.get("task_id")
    if record_task_id and record_task_id != task_id:
        return None
    raw_issues = record.get("validation_issues")
    issues: list[dict[str, str]] = []
    if isinstance(raw_issues, list):
        for raw in raw_issues[:20]:
            if not isinstance(raw, dict):
                continue
            issue = {key: value for key, pattern in (
                ("code", _SAFE_ID), ("path", _SAFE_PATH),
                ("placement_id", _SAFE_ID), ("turn_id", _SAFE_ID),
                ("selection_id", _SAFE_ID), ("beat_id", _SAFE_ID),
                ("take_id", _SAFE_ID), ("cue_id", _SAFE_ID),
            ) if (value := _field(raw, key, pattern)) is not None}
            if issue.get("code"):
                issues.append(issue)
    count = record.get("validation_issue_count")
    summary = {
        "task_id": task_id,
        "operation_id": _field(record, "operation_id", _SAFE_ID) or "",
        "error_code": "plan_validation_failed",
        "phase": "validating",
        "issue_count": count if type(count) is int and 0 <= count <= 10_000_000 else len(issues),
        "issues": issues,
    }
    # Root-level rule failures have no field path; keep the bounded hint so a
    # failure can be explained from the task itself.
    error_message = _field(record, "error_message", _SAFE_MESSAGE)
    if error_message:
        summary["error_message"] = error_message
    return summary


def _get(task: Any, key: str) -> Any:
    return task.get(key) if isinstance(task, dict) else getattr(task, key, None)


def read_plan_failure_summary(task: Any) -> dict | None:
    """Never attach a stale or unrelated task's diagnosis to this failure."""
    if (_get(task, "task_type") not in _PLAN_TASKS or _get(task, "status") != "failed"
            or _get(task, "error") != "plan_validation_failed"):
        return None
    metadata = _get(task, "metadata")
    if not isinstance(metadata, dict) or "production_plan_failure" not in metadata:
        result = _get(task, "result")
        metadata = result.get("task_metadata") if isinstance(result, dict) else None
    failure = metadata.get("production_plan_failure") if isinstance(metadata, dict) else None
    if (not isinstance(failure, dict) or failure.get("task_id") != _get(task, "task_id")
            or failure.get("error_code") != "plan_validation_failed"):
        return None
    return failure


def format_plan_failure_notice(failure: dict) -> str:
    """Render known issue codes; never quote model output or exception text."""
    issues = failure.get("issues")
    issue = issues[0] if isinstance(issues, list) and issues and isinstance(issues[0], dict) else {}
    if issue.get("code") == "speech_source_timing_cycle":
        placement = _field(issue, "placement_id", _SAFE_ID)
        turn = _field(issue, "turn_id", _SAFE_ID)
        path = _field(issue, "path", _SAFE_PATH)
        if placement and turn and path:
            edge = "开始" if path.endswith("/start") else "结束" if path.endswith("/end") else "起止"
            return (f"作品方案校验未通过：发声素材 {placement} 的{edge}时间依赖它自己承载、"
                    f"但尚未确定时间的台词 {turn}（{path}），形成循环。"
                    "先确定发声素材的段落或片段范围，再让字幕等元素跟随台词。本次方案尚未保存。")
    code = _field(issue, "code", _SAFE_ID)
    path = _field(issue, "path", _SAFE_PATH)
    placement = _field(issue, "placement_id", _SAFE_ID)
    beat = _field(issue, "beat_id", _SAFE_ID)
    turn = _field(issue, "turn_id", _SAFE_ID)
    if code == "speech_source_spans_beats" and placement and beat and turn:
        return (f"作品方案校验未通过：发声素材 {placement} 同时承载了多处段落（{path}）的台词，"
                f"包括不属于 {beat} 的台词 {turn}。发声素材只能对应一个段落；"
                "请把这些台词改回各自段落的发声素材，或让它们改指向本段的发声素材。本次方案尚未保存。")
    if code == "speech_source_turns_not_contiguous" and placement and beat:
        target = f"台词 {turn} " if turn else "中间的台词"
        return (f"作品方案校验未通过：发声素材 {placement} 承载的台词在 {beat} 中不连续（{path}）。"
                f"请把{target}移出或改指向别的发声素材，让这段发声只覆盖连续台词。本次方案尚未保存。")
    if code == "speech_source_interval_excludes_turn" and placement and turn:
        return (f"作品方案校验未通过：发声素材 {placement} 的时间范围没有覆盖它承载的台词 {turn}（{path}）。"
                "请扩大发声素材范围或调整它的起止锚点。本次方案尚未保存。")
    if code == "speech_source_missing" and placement:
        return (f"作品方案校验未通过：台词引用了方案里不存在的发声素材 {placement}（{path}）。"
                "请把这些台词改指向方案内已有的画面或人声素材。本次方案尚未保存。")
    hint = _field(failure, "error_message", _SAFE_MESSAGE)
    if code and path and path != "/":
        # Keep the field and the stable code; add the hint when the code is not
        # self-explanatory (e.g. a cross-object rule surfaced without IDs).
        location = f"字段 {path}、错误类型 {code}"
        if hint and code in _NEEDS_HINT_CODES:
            location += f"（{hint}）"
    else:
        location = f"内部校验原因（技术信息）：{hint}" if hint else "请查看任务日志中的校验位置"
    return f"作品方案校验未通过：{location}。本次方案尚未保存。"
