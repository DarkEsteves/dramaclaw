"""Host-owned evidence for a reference continuation, independent of creative phase.

Call context only for content validated by the host, never an assistant claim.
"""

from __future__ import annotations

from typing import Any

ACTIONS = {
    "production_execution_failed": "inspect_current_production_receipt",
    "agent_session_invalid": "restore_agent_session",
    "context_delivery_failed": "retry_current_stage",
    "browser_required": "open_canvas",
    "canvas_conflict": "refresh_canvas",
    "capability_unavailable": "retry_supported_capability",
    "user_required": "answer_existing_request",
    "request_expired": "refresh_request",
    "permission_denied": "check_project_access",
}
MESSAGES = {
    "production_execution_failed": "作品方案操作未完成，请检查当前草稿、节点或任务回执",
    "agent_session_invalid": "系统工具会话已失效，请恢复服务会话后重试；已保存的原片分析仍可继续使用",
    "context_delivery_failed": "原片分析已完成，虾导读取或交付失败，请继续处理",
    "browser_required": "原片分析已完成，等待打开画布",
    "canvas_conflict": "画布版本已变化，请刷新画布后继续",
    "capability_unavailable": "当前工具能力不可用，请继续处理",
    "user_required": "等待回答已有确认卡",
    "request_expired": "请求已过期，请刷新后继续",
    "permission_denied": "当前没有项目访问权限",
}


class ReferenceTurnOutcome:
    def __init__(self, *, turn_id: str, scope: dict[str, Any], revision: Any):
        self.turn_id, self.scope, self.revision = turn_id, scope, revision
        self.receipt = None
        self.delivered = False
        self.blocker = None

    def context(self, *, source: str, revision: Any, content: dict[str, Any]) -> None:
        if content:
            self.receipt = {
                "turn_id": self.turn_id,
                "scope": self.scope,
                "source": source,
                "revision": revision,
                "coverage": "reference_summary",
                **{
                    key: content[key]
                    for key in (
                        "analysis_node_id",
                        "source_url",
                        "artifact_ref",
                        "summary_digest",
                    )
                    if key in content
                },
            }

    def assistant(self, text: str) -> None:
        """Record conversational delivery only; this never proves execution."""
        self.delivered = self.delivered or bool(text.strip())

    def failure(self, code: str, *, request_id: str | None = None) -> None:
        # A matched host terminal authentication failure survives later bridge
        # reconciliation; pending card evidence is retained separately by the run.
        if self.blocker and self.blocker["code"] == "agent_session_invalid":
            return
        code = code if code in ACTIONS else "context_delivery_failed"
        self.blocker = {
            "code": code,
            "next_action": ACTIONS[code],
            "message": MESSAGES[code],
        }
        if request_id:
            self.blocker["request_id"] = request_id

    def finish(
        self, *, current_revision: Any, context_current: bool = True
    ) -> dict[str, Any]:
        if not context_current:
            self.failure("canvas_conflict")
        elif (
            self.receipt
            and self.delivered
            and (
                not self.blocker
                or self.blocker["code"]
                in {"context_delivery_failed", "capability_unavailable"}
            )
        ):
            self.blocker = None
        elif not self.blocker:
            self.failure("context_delivery_failed")
        return {
            "execution_blocker": self.blocker,
            "last_successful_context_receipt": self.receipt,
        }


def reference_summary(value: Any) -> str | None:
    if isinstance(value, dict):
        analysis = value.get("reference_analysis")
        if isinstance(analysis, dict) and analysis.get("summary"):
            return str(analysis["summary"])
        return next(
            (
                summary
                for item in value.values()
                if (summary := reference_summary(item))
            ),
            None,
        )
    if isinstance(value, list):
        return next(
            (summary for item in value if (summary := reference_summary(item))), None
        )
    return None


def _receipt_db(state_dir):
    import sqlite3

    path = state_dir / "freezone" / "context_receipts.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS context_receipts (scope_key TEXT NOT NULL, receipt_json TEXT NOT NULL, created_at REAL NOT NULL DEFAULT 0)"
    )
    return connection


def _receipt_scope(binding):
    import json

    return json.dumps(
        {
            key: binding.get(key)
            for key in (
                "turn_id",
                "requester_user_id",
                "project_id",
                "canvas_id",
                "agent_id",
            )
        },
        sort_keys=True,
    )


def record_context_receipt(state_dir, binding, result):
    """Called only after the API validated its host binding and read succeeded."""
    import hashlib
    import json

    analysis_reads = analysis_summary_reads(result)
    if not analysis_reads and not (result.get("analysis_ref") and result.get("items")):
        return
    receipt = {
        "turn_id": binding["turn_id"],
        "scope": {
            key: binding.get(key)
            for key in ("requester_user_id", "project_id", "canvas_id", "agent_id")
        },
        "source": result.get("source", "artifact_api"),
        "revision": result.get("canvas_revision"),
        "artifact_ref": result.get("analysis_ref"),
        "analysis_reads": [
            {key: value for key, value in read.items() if key != "summary"}
            for read in analysis_reads
        ],
        "content_digest": hashlib.sha256(
            json.dumps(result, sort_keys=True).encode()
        ).hexdigest(),
        "coverage": result.get("coverage"),
    }
    connection = _receipt_db(state_dir)
    try:
        with connection:
            import time

            connection.execute(
                "DELETE FROM context_receipts WHERE created_at<?",
                (time.time() - 86400,),
            )
            connection.execute(
                "INSERT INTO context_receipts VALUES (?, ?, ?)",
                (_receipt_scope(binding), json.dumps(receipt), time.time()),
            )
            connection.execute(
                "DELETE FROM context_receipts WHERE rowid NOT IN (SELECT rowid FROM context_receipts ORDER BY rowid DESC LIMIT 1000)"
            )
    finally:
        connection.close()


def read_context_receipts(state_dir, binding):
    import json

    connection = _receipt_db(state_dir)
    try:
        return [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT receipt_json FROM context_receipts WHERE scope_key=?",
                (_receipt_scope(binding),),
            )
        ]
    finally:
        connection.close()


def clear_context_receipts(state_dir, binding):
    connection = _receipt_db(state_dir)
    try:
        with connection:
            connection.execute(
                "DELETE FROM context_receipts WHERE scope_key=?",
                (_receipt_scope(binding),),
            )
    finally:
        connection.close()


def historical_false_wait(run, events, commission):
    """Only real host tool errors, no successful read or native decision, qualify."""
    import json

    if (
        run.get("status") != "awaiting_direction"
        or not run.get("resume_turn_id")
        or run.get("last_successful_context_receipt")
        or run.get("pending_interaction")
        or commission.get("direction")
        or commission.get("decisions")
    ):
        return False
    failed = False
    traces = []
    expected_scope = {"kind": "project", "id": run.get("project_id"),
                      "surface": "freezone", "canvasId": run.get("canvas_id"),
                      "agentId": run.get("agent_id") or "main"}
    for event in events:
        if event.get("type") != "assistant.message_parts":
            traces.append(event)
            continue
        # This persisted snapshot is host output. Only its scoped tool raw
        # frames qualify; assistant text and presentation labels are not proof.
        if (event.get("turn_id") != run["resume_turn_id"]
                or not expected_scope["id"] or not expected_scope["canvasId"]):
            continue
        parts = event.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict) or part.get("type") != "tool_status":
                continue
            tool_event = part.get("event")
            if not isinstance(tool_event, dict):
                continue
            raw = tool_event.get("raw")
            if not isinstance(raw, dict) or raw.get("turn_id") != run["resume_turn_id"]:
                continue
            scope = raw.get("scope")
            if not isinstance(scope, dict) or any(scope.get(key) != value for key, value in expected_scope.items()):
                continue
            traces.append(raw)
    for event in traces:
        if event.get("turn_id") != run["resume_turn_id"] or event.get("type") not in {
            "agent.tool.updated",
            "tool_updated",
            "tool_completed",
        }:
            continue
        name = str(event.get("name") or "")
        if not any(
            value in name for value in ("context", "node_detail", "reference_evidence")
        ):
            continue
        result = event.get("result_json")
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except ValueError:
                continue
        if not isinstance(result, dict):
            continue
        if result.get("ok") is True:
            target_id = run.get("analysis_node_id")
            if target_id:
                # Browser and saved reads use the same bounded responses/nodes
                # protocol. Empty, selected-source or other-analysis responses
                # cannot hide an actual failed read of this analysis.
                envelope = result.get("data") if isinstance(result.get("data"), dict) else result
                for response in envelope.get("responses") or []:
                    if not isinstance(response, dict) or not isinstance(response.get("data"), dict):
                        continue
                    for node in response["data"].get("nodes") or []:
                        if not isinstance(node, dict) or (node.get("node_id") or node.get("id")) != target_id:
                            continue
                        data = node.get("data") or {}
                        if not isinstance(data, dict):
                            continue
                        analysis = node.get("reference_analysis") or {}
                        raw_result = data.get("rawResult") or {}
                        if not isinstance(analysis, dict) or not isinstance(raw_result, dict):
                            continue
                        story = raw_result.get("video_story") or {}
                        if not isinstance(story, dict):
                            continue
                        summary = analysis.get("summary") or story.get("summary")
                        source = analysis.get("source_url") or data.get("sourceVideoUrl")
                        if (isinstance(summary, str) and summary.strip()
                                and (not source or not run.get("source_video_url") or source == run["source_video_url"])):
                            return False
            elif result.get("data") or reference_summary(result) or result.get("items"):
                # Retain the older top-level helper contract for legacy records
                # without an analysis identity; scoped current records use above.
                return False
        failed = failed or (
            result.get("ok") is False
            and bool(result.get("error") or result.get("code") or (
                result.get("status") == "failed"
                and result.get("tool_call_status") == "failed"
                and isinstance(result.get("errors"), list)
                and any(isinstance(error, str) and error.strip() for error in result["errors"])
            ))
        )
    return failed


def context_failure_code(result):
    code = result.get("code") or result.get("status")
    if code in {"requires_browser", "browser_required"}:
        return "browser_required"
    if code in {"unsupported", "capability_unavailable"}:
        return "capability_unavailable"
    if (
        code in {"canvas_conflict", "revision_conflict", "canvas_revision_conflict"}
        or result.get("status_code") == 409
    ):
        return "canvas_conflict"
    if code == "request_expired" or result.get("status_code") == 410:
        return "request_expired"
    if code == "permission_denied" or result.get("status_code") in {401, 403}:
        return "permission_denied"
    return (
        "context_delivery_failed"
        if result.get("ok") is False or result.get("error")
        else None
    )


def analysis_summary_reads(value: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract only analysis nodes in real context response protocol positions."""
    import hashlib
    import json

    envelope = value
    if isinstance(value.get("data"), dict) and not value.get("responses"):
        envelope = value["data"]
    reads = []
    for response in envelope.get("responses") or []:
        if not isinstance(response, dict) or not isinstance(response.get("data"), dict):
            continue
        for node in response["data"].get("nodes") or []:
            if not isinstance(node, dict):
                continue
            node_id = node.get("node_id") or node.get("id")
            if not node_id:
                continue
            analysis = node.get("reference_analysis")
            if not isinstance(analysis, dict):
                if (node.get("node_type") or node.get("type")) != "videoStoryNode":
                    continue
                data = node.get("data") or {}
                story = (data.get("rawResult") or {}).get("video_story") or {}
                analysis = {
                    "summary": story.get("summary"),
                    "source_url": data.get("sourceVideoUrl"),
                    "analysis_ref": data.get("referenceAnalysisRef"),
                }
            if (
                not analysis.get("summary")
                or analysis.get("analysis_node_id", node_id) != node_id
            ):
                continue
            content = {
                "summary": analysis["summary"],
                "source_url": analysis.get("source_url"),
                "artifact_ref": analysis.get("analysis_ref"),
            }
            reads.append(
                {
                    "analysis_node_id": node_id,
                    **content,
                    "summary_digest": hashlib.sha256(
                        json.dumps(content, sort_keys=True).encode()
                    ).hexdigest(),
                }
            )
    return reads
