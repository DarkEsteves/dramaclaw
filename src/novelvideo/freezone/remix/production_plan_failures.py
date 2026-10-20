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
            ) if (value := _field(raw, key, pattern)) is not None}
            if issue.get("code"):
                issues.append(issue)
    count = record.get("validation_issue_count")
    return {
        "task_id": task_id,
        "operation_id": _field(record, "operation_id", _SAFE_ID) or "",
        "error_code": "plan_validation_failed",
        "phase": "validating",
        "issue_count": count if type(count) is int and 0 <= count <= 10_000_000 else len(issues),
        "issues": issues,
    }


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
    location = f"字段 {path}、错误类型 {code}" if code and path else "请查看任务日志中的校验位置"
    return f"作品方案校验未通过：{location}。本次方案尚未保存。"
