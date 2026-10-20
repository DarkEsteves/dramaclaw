"""Host-owned outcome projection for canvas operations.

Legacy envelope helpers remain for stored history and migration tests; live
canvas turns use :func:`summarize_canvas_operations` independently of prose.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def summarize_canvas_operations(*, operations: list[dict[str, Any]], disposition: str) -> dict[str, Any]:
    """Report host-observed operations without interpreting assistant prose."""
    settled: list[dict[str, Any]] = []
    blocking: list[str] = []
    for operation in operations:
        status = str(operation.get("status") or "")
        verified = operation.get("verified") is True
        if status in {"completed", "succeeded"}:
            status = "completed" if verified and operation.get("receipt") else "unverified"
        elif status in {"accepted", "submitted", "running"}:
            status = status if verified else "unverified"
        elif status in {"pending", "awaiting_approval", "waiting_approval"}:
            status = "awaiting_approval"
        elif status not in {"failed", "timeout", "cancelled"}:
            status = "unverified"
        call_id = str(operation.get("call_id") or "")
        if status in {"failed", "timeout", "cancelled", "unverified"} and call_id:
            blocking.append(call_id)
        settled.append({**operation, "status": status})
    return {
        "operations": settled,
        "blocking_call_ids": blocking,
        "disposition": disposition,
    }

CANVAS_FINAL_RESPONSE_INSTRUCTIONS = (
    "Your final response must be exactly one JSON object with message, mode, and "
    "canvas_receipts. No Markdown fences, headings, or prose outside that object. "
    "Put all user-facing explanations and success summaries in the message field. "
    "For an interactive-story or interactive-ad proposal with no write tools called, put the complete "
    "natural-language outline inside message and use mode=read_only with "
    "canvas_receipts=[]. Do not output the outline as top-level Markdown. "
    "Instructions from tools to report success apply only to the message field; "
    "they never change the final response format. For a successful canvas mutation, "
    "use mode=mutation and include every exact same-turn successful receipt as "
    "{\"bridge_key\":\"actual returned key\",\"revision\":null}, or "
    "{\"bridge_key\":null,\"revision\":actual_returned_integer} for direct apply. "
    "Saving a story outline and confirming story stages are canvas mutations, even "
    "when no visible nodes are created. If a turn confirms stages and submits a "
    "workflow, include BOTH the stage write revision and the workflow bridge key; "
    "the last receipt never replaces earlier receipts from this turn. "
    "Never invent receipts. For read_only or blocked, use canvas_receipts=[]. "
    "Validation/read tools may return a newer snapshot revision; that value is not "
    "a write receipt. For a mutation, copy the revision from the successful write "
    "tool result, not from a later validation result. "
    "Saved director decisions/reviews/material packages, prepared workflow drafts, "
    "and background reference analysis submission are not canvas apply receipts: "
    "use mode=read_only with canvas_receipts=[] to describe those exact outcomes. "
    "Direct production plan creation adds canvas nodes and returns a saved canvas "
    "revision; use that revision for its mutation receipt. "
    "A creation receipt does not prove media generation or parameter persistence."
)

# Sent once, on the same thread, when a turn with no canvas write attempt ended
# in a reply that is not a well-formed envelope (#680). The answer is rewritten
# from this turn's evidence rather than repeated: prose from the malformed
# reply may claim a change that never happened.
CANVAS_FORMAT_REPAIR_PROMPT = (
    "Your previous final response was not the required JSON object. This turn "
    "made zero canvas writes: no node, edge, parameter, or run was created, "
    "changed, deleted, or started. Do not call any tools. Rewrite your answer to "
    "the user using only this turn's tool results. If your previous answer said "
    "or implied that anything on the canvas was created, changed, saved, or run, "
    "that statement is false: do not repeat it, and say instead that the canvas "
    "was not changed. Use mode=read_only (or blocked if the request could not be "
    "completed) and canvas_receipts=[]."
)

CANVAS_SAVED_ARTIFACT_FORMAT_REPAIR_PROMPT = (
    "Your previous final response was not the required JSON object. This turn "
    "has successful, verified non-canvas tool results but no canvas apply "
    "receipts. Do not call any tools or repeat any writes. Rewrite your answer "
    "using only this turn's verified tool results and preserve the distinction "
    "between saved artifacts or background submissions and canvas changes. "
    "Do not claim a canvas node, edge, or parameter was changed. Use "
    "mode=read_only, canvas_receipts=[], and put the user-facing answer in "
    "message."
)

_REPLY_CONTRACT_FAILURE = "回复未通过操作结果校验："

CANVAS_REPLY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "message": {"type": "string"},
        "mode": {"type": "string", "enum": ["read_only", "mutation", "blocked"]},
        "canvas_receipts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "bridge_key": {"type": ["string", "null"]},
                    "revision": {"type": ["integer", "null"]},
                },
                "required": ["bridge_key", "revision"],
            },
        },
    },
    "required": ["message", "mode", "canvas_receipts"],
}


def receipt_reference(payload: dict[str, Any]) -> tuple[str, int | None]:
    """Identify a browser receipt or a direct saved revision, never tool status."""
    key = str(payload.get("bridge_key") or "").strip()
    revision = payload.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool):
        revision = None
    if payload.get("canvas_apply_status") == "direct_applied":
        return "", revision
    return (key, None) if key else ("", revision)


def _claim_reference(value: Any) -> tuple[str, int | None] | None:
    if not isinstance(value, dict) or set(value) != {"bridge_key", "revision"}:
        return None
    key, revision = value["bridge_key"], value["revision"]
    if isinstance(key, str) and key.strip() and revision is None:
        return key.strip(), None
    if (
        key is None
        and isinstance(revision, int)
        and not isinstance(revision, bool)
        and revision >= 0
    ):
        return "", revision
    return None


def recover_unstructured_canvas_message(text: str) -> str | None:
    """Recover only a complete JSON message string from a malformed reply."""
    match = re.search(r'"message"\s*:\s*', text)
    if match is None:
        return None
    try:
        message, _ = json.JSONDecoder().raw_decode(text, match.end())
    except ValueError:
        return None
    return message.strip() if isinstance(message, str) and message.strip() else None


def _parse_canvas_envelope(text: str) -> tuple[dict[str, Any] | None, str]:
    """Check only the reply's shape; claims are judged against evidence later."""
    candidate = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", candidate, re.I | re.S)
    if fenced is not None:
        candidate = fenced.group(1).strip()
    try:
        reply = json.loads(candidate)
    except (TypeError, ValueError):
        return None, _REPLY_CONTRACT_FAILURE + "未返回结构化结果，请重试。"
    if not isinstance(reply, dict):
        return None, _REPLY_CONTRACT_FAILURE + "结果格式无效，请重试。"
    message = reply.get("message")
    mode = reply.get("mode")
    if (
        set(reply) != {"message", "mode", "canvas_receipts"}
        or not isinstance(message, str)
        or not message.strip()
        or not isinstance(mode, str)
        or mode not in {"read_only", "mutation", "blocked"}
        or not isinstance(reply.get("canvas_receipts"), list)
    ):
        return None, _REPLY_CONTRACT_FAILURE + "结果格式无效，请重试。"
    return reply, ""


def canvas_reply_content(text: str) -> str | None:
    """Return actual non-blocked reply content, never a generated diagnostic."""
    reply, _ = _parse_canvas_envelope(text)
    if reply is None or reply["mode"] == "blocked":
        return None
    return reply["message"].strip()


def finalize_canvas_reply(
    text: str,
    *,
    attempts: dict[str, str],
    receipts: set[tuple[str, int | None]],
    receipt_aliases: dict[
        tuple[str, int | None], tuple[str, int | None]
    ] | None = None,
    failure: str = "",
    draft_ready: bool = False,
) -> str:
    """Validate claims against same-turn evidence without interpreting user prose.

    Every attempted write must succeed; one successful operation cannot mask
    another failed operation. Catalog-only and read-only answers need no canvas
    receipt. A prepared workflow remains a pending user approval, not a write.
    """
    states = set(attempts.values())
    if "failed" in states:
        return "画布操作未完成：" + (
            failure or "没有收到所有操作的成功画布写入回执，请重试。"
        )
    if "timeout" in states:
        return "画布操作等待超时：未收到执行回执，请确认画布连接后重试。"
    if "cancelled" in states:
        return "画布操作已取消，未完成全部操作。"
    if "running" in states:
        return "画布操作进行中，尚未完成。"
    if "accepted" in states:
        return "画布操作已提交，尚未完成。"
    if states - {"succeeded"}:
        return "画布操作等待确认或执行回执，尚未完成。"
    if draft_ready and not attempts:
        return "工作流草稿已准备完成，等待你确认后创建画布节点；尚未执行生成。"

    def verified_status() -> str:
        # Report only host evidence, never the rejected model prose. A bridge
        # receipt proves admission, not completion of background generation.
        logger.warning(
            "canvas reply receipt omission: reporting verified status writes=%d receipts=%d",
            len(attempts), len(receipts),
        )
        parts = []
        if any(not key for key, _ in receipts):
            parts.append("本轮画布修改已保存。")
        if any(key for key, _ in receipts):
            parts.append("本轮画布执行请求已受理，生成进度请以画布任务状态为准。")
        return "".join(parts) + "回复确认信息不完整，请查看画布结果，无需重复提交已成功的操作。"

    reply, envelope_error = _parse_canvas_envelope(text)
    if reply is None:
        return envelope_error
    message, mode, claims = reply["message"], reply["mode"], reply["canvas_receipts"]
    if mode == "mutation":
        if attempts and receipts and not claims:
            return verified_status()
        if not attempts or not claims:
            return "画布操作未完成：本轮没有可验证的画布写入回执，请重试。"
        references = set()
        aliases = receipt_aliases or {}
        for claim in claims:
            reference = _claim_reference(claim)
            if reference is not None:
                reference = aliases.get(reference, reference)
            if reference is None or reference not in receipts:
                return "画布操作未完成：成功声明与本轮写入回执不匹配，请重试。"
            references.add(reference)
        if references != receipts:
            return verified_status()
    elif claims or attempts:
        if mode == "read_only" and not claims and attempts and receipts:
            return verified_status()
        return _REPLY_CONTRACT_FAILURE + "操作声明与工具结果不一致，请重试。"
    return message.strip()


def needs_canvas_format_repair(
    text: str,
    *,
    attempts: dict[str, str],
    receipts: set[tuple[str, int | None]],
    draft_ready: bool = False,
) -> bool:
    """Whether a no-write turn's reply failed the envelope shape, not the evidence.

    Turns with any write attempt, receipt, or pending draft are settled by the
    receipt checks alone. A well-formed envelope whose claims contradict the
    evidence (for example read_only with invented receipts) is a false claim,
    not a format slip, and is never given a second attempt.
    """
    if attempts or receipts or draft_ready:
        return False
    return _parse_canvas_envelope(text)[0] is None


def production_reply_from_evidence(payload: dict[str, Any]) -> str:
    """Describe a native submission using host tool data, never model prose."""
    if payload.get('ok') is False:
        return '作品方案操作未完成：' + str(payload.get('error') or payload.get('code') or '未收到有效执行回执。')[:1000]
    stage = payload.get('native_plan_stage') or {}
    slots = stage.get('slots') or []
    states = {slot.get('state') for slot in slots} or {stage.get('state')}
    if states == {'ready_for_review'} and slots and all(
            slot.get('task_id') and slot.get('plan_ref') for slot in slots):
        return '作品方案已生成并保存到原方案节点，等待审核。'
    if 'failed' in states:
        return '作品方案节点已保留；起草任务失败，请查看原任务错误后决定下一步。'
    if any(slot.get('state') == 'running' and slot.get('task_id') for slot in slots):
        return '作品方案起草任务正在运行，完成后会继续审核。'
    if any(slot.get('state') == 'submitted' and slot.get('task_id') for slot in slots):
        return '作品方案起草任务已提交，等待完成事件。'
    if states == {'ready_to_submit'} and slots and all(slot.get('plan_node_id') for slot in slots):
        return '作品方案节点已创建，尚未提交起草任务。'
    if payload.get('canvas_apply_status') in {'accepted', 'applied', 'direct_applied'} and payload.get('applied') is True:
        return '作品方案画布执行请求已受理；节点和起草进度以实际任务回执为准。'
    if payload.get('canvas_apply_status') in {'pending', 'awaiting_approval', 'waiting_approval'} or payload.get('status') in {'pending', 'awaiting_approval', 'waiting_approval'}:
        return '作品方案操作等待画布审批，尚未执行生成。'
    if stage.get('draft_id'):
        return '作品方案草稿已保存，等待画布或任务执行回执；尚无生成完成凭据。'
    return '作品方案操作尚未收到有效执行回执。'
