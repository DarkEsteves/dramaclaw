"""Commit a discussed reference direction at the native node submission boundary.

Only an active server-bound human chat turn can create this evidence. The agent's
candidate goal and HTTP turn header are business inputs, never authorization.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from novelvideo.chat.store import ChatScope, chat_store
from novelvideo.freezone import canvas_store
from novelvideo.freezone.canvas_context_reader import saved_turn_binding
from novelvideo.freezone.remix.reference_commission import read_reference_commission
from novelvideo.freezone.remix.reference_director import (
    confirm_reference_decision, prepare_reference_decision,
)
from novelvideo.freezone.remix.store import RemixArtifactStore

def _verified_visual_choices(raw: Any, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate Agent-supplied preserve/replace entries against human evidence."""
    choices = raw
    if isinstance(raw, str):
        try:
            choices = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, {"code": "direction_submission_invalid",
                                      "field_path": "preserve_replace"}) from exc
    if not isinstance(choices, list) or len(choices) > 30:
        raise HTTPException(422, {"code": "direction_submission_invalid",
                                  "field_path": "preserve_replace"})
    human_text = [str(row.get('content') or '') for row in messages if row.get('role') == 'user']
    seen: set[str] = set()
    for choice in choices:
        if not isinstance(choice, dict) or set(choice) != {
            'id', 'action', 'entity_kind', 'entity_id', 'visual_identity', 'evidence',
        }:
            raise HTTPException(422, {"code": "direction_submission_invalid",
                                      "field_path": "preserve_replace"})
        if (choice['action'] not in {'preserve', 'replace'}
                or type(choice['visual_identity']) is not bool
                or any(not isinstance(choice[key], str) or not choice[key].strip()
                       or len(choice[key]) > 300
                       for key in ('id', 'entity_kind', 'entity_id', 'evidence'))
                or choice['id'] in seen
                or not any(choice['evidence'] in text for text in human_text)):
            raise HTTPException(422, {"code": "direction_submission_invalid",
                                      "field_path": "preserve_replace"})
        seen.add(choice['id'])
    return choices


def _normalize_submission_requirements(value: Any) -> dict[str, Any]:
    from novelvideo.freezone.remix.production_requirements import normalize_requirements

    if value is None:
        return {}
    try:
        return normalize_requirements(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, {
            "code": "direction_submission_invalid",
            "field_path": "requirements",
            "message": str(exc),
            "retryable": False,
        }) from exc


def _human_context(*, ctx, canvas_id: str, agent_id: str, turn_id: str) -> tuple[dict, ChatScope, list[dict], dict]:
    binding = saved_turn_binding(ctx.state_dir, turn_id)
    expected = (ctx.requester_user_id, ctx.project_id, canvas_id, agent_id)
    if (not binding or binding.get("coordinator_lease")
            or tuple(binding.get(key) for key in
                     ("requester_user_id", "project_id", "canvas_id", "agent_id")) != expected
            or type(binding.get("human_message_id")) is not int
            or not isinstance(binding.get("chat_scope_id"), str)):
        raise HTTPException(403, {"code": "human_turn_binding_required"})
    scope = ChatScope(kind="project", id=binding["chat_scope_id"], surface="freezone",
                      canvas_id=canvas_id, agent_id=agent_id, state_dir=str(ctx.state_dir))
    messages = chat_store.list_messages(ctx.requester_username, scope, limit=201)
    # Preserve a recoverable path for long conversations. Deterministic
    # evidence checks require a self-contained instruction whenever older
    # context was omitted; a short "as above" cannot authorize anything then.
    context_truncated = len(messages) > 200
    if context_truncated:
        messages = messages[-200:]
    length = sum(len(str(row.get('content') or '')) for row in messages)
    while len(messages) > 1 and length > 200_000:
        length -= len(str(messages[0].get('content') or ''))
        messages = messages[1:]
        context_truncated = True
    if length > 200_000:
        raise HTTPException(409, {"code": "direction_context_too_large",
                                  "message": "请用一条较短的消息完整重述目标和操作"})
    user_rows = [row for row in messages if row.get("role") == "user"]
    if (not user_rows or user_rows[-1].get("id") != binding["human_message_id"]
            or user_rows[-1].get("turn_id") != turn_id):
        raise HTTPException(409, {"code": "human_turn_changed"})
    return {**binding, 'context_truncated': context_truncated}, scope, messages, user_rows[-1]


def _claim_ordinary_owner(ctx, *, canvas_id: str, agent_id: str,
                          commission_id: str, direction_revision: int,
                          turn_id: str) -> dict:
    from novelvideo.chat.reference_remix_continuation import coordinator
    owner = coordinator.store.claim_ordinary_turn(
        ctx, canvas_id=canvas_id, agent_id=agent_id,
        commission_id=commission_id,
        direction_revision=direction_revision, turn_id=turn_id,
    )
    if owner is not None:
        return owner
    runs = coordinator.store.list_for_actor(ctx, canvas_id, agent_id)
    if any((run.get('commission_ref') or {}).get('artifact_id') == commission_id
           and (run.get('ordinary_turn_owner') or {}).get('state') == 'active'
           for run in runs):
        raise HTTPException(409, {"code": "production_owned_by_another_turn"})
    raise HTTPException(409, {"code": "production_turn_unavailable"})


def _publish_under_human_fence(ctx, *, scope: ChatScope, canvas_id: str,
                               turn_id: str, message_id: int, canvas_revision: int,
                               store: RemixArtifactStore, commission_ref: dict,
                               decision_ref: dict, identity: str,
                               message_content: str) -> dict:
    """Serialize the final direction CAS with new messages in the same chat DB."""
    connection = chat_store.connect(ctx.requester_username, scope)
    try:
        connection.execute('BEGIN IMMEDIATE')
        latest_user = connection.execute(
            "SELECT id, turn_id FROM chat_messages WHERE role='user' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if (latest_user is None or latest_user['id'] != message_id
                or latest_user['turn_id'] != turn_id):
            raise HTTPException(409, {"code": "human_turn_changed"})
        with canvas_store.get_canvas_write_mutex().write_mutex(ctx.state_dir, canvas_id) as guard:
            canvas = canvas_store.read_canvas(ctx.state_dir, canvas_id)
            if not canvas or canvas.get('revision') != canvas_revision:
                raise HTTPException(409, {"code": "direction_source_changed"})
            current = read_reference_commission(store, commission_ref, latest=True)
            if current['commission_ref'] != commission_ref or current['phase'] != 'awaiting_direction':
                raise HTTPException(409, {"code": "direction_source_changed"})
            guard.reassert()
            result = confirm_reference_decision(
                store, decision_ref, project_id=ctx.project_id, canvas_id=canvas_id,
                requester_user_id=ctx.requester_user_id, message_id=identity,
                message_content=message_content,
                expected_commission_ref=commission_ref,
            )
        connection.commit()
        return result
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


async def ensure_confirmed_direction_for_entry(
    *, ctx, canvas_id: str, agent_id: str, turn_id: str,
    commission_ref: dict, goal: str, requested_operation: dict[str, Any],
    agent_request: bool = True, requirements: dict[str, Any] | None = None,
    preserve_replace: list[dict[str, Any]] | None = None,
) -> dict:
    """Return a confirmed ref; this function never creates nodes or executes actions."""
    # The tool schema permits omitting project_id. Resolve that optional field
    # against the authenticated request scope before any exact revision checks.
    # An explicitly supplied project_id is still checked by the artifact store.
    if 'project_id' not in commission_ref:
        commission_ref = {**commission_ref, 'project_id': ctx.project_id}
    store = RemixArtifactStore(Path(ctx.output_dir), ctx.project_id, created_by=ctx.requester_user_id)
    try:
        current = await asyncio.to_thread(read_reference_commission, store, commission_ref, latest=True)
    except (FileNotFoundError, ValueError, KeyError) as exc:
        raise HTTPException(409, {"code": "commission_unavailable"}) from exc
    if (current["project_id"], current["canvas_id"], current["requester_user_id"]) != (
        ctx.project_id, canvas_id, ctx.requester_user_id,
    ):
        raise HTTPException(403, {"code": "commission_scope_mismatch"})
    # Existing confirmed directions and their native drafts retain their old
    # contract. This boundary is required only to turn discussion into the
    # *first* confirmed direction; a new target on an existing node uses its
    # ordinary canvas update command and confirmation policy.
    if current.get("direction_revision"):
        if not agent_request:
            existing_goal = (current.get('direction') or {}).get('target_goal')
            native_chat_direction = any(
                str(row.get('message_id') or '').startswith('chat-message:')
                for row in current.get('decisions', []) if row.get('type') == 'direction_confirmed')
            if native_chat_direction and existing_goal is not None and existing_goal != goal:
                raise HTTPException(409, {"code": "confirmed_goal_changed"})
            if current['commission_ref'] != commission_ref:
                raise HTTPException(409, {"code": "stale_commission_ref",
                                          "commission_ref": current['commission_ref']})
            return {"commission_ref": current['commission_ref'],
                    "direction_revision": current['direction_revision'],
                    "direction_fingerprint": current['direction_fingerprint'],
                    "ordinary_owner": None}
        binding, scope, messages, user_message = await asyncio.to_thread(
            _human_context, ctx=ctx, canvas_id=canvas_id,
            agent_id=agent_id, turn_id=turn_id,
        )
        existing_goal = (current.get('direction') or {}).get('target_goal',
            (current.get('direction') or {}).get('goal'))
        if existing_goal is not None and existing_goal != goal:
            raise HTTPException(409, {"code": "confirmed_goal_changed",
                                      "message": "更新现有节点目标后使用原有画布确认流程"})
        identity = f"{ctx.requester_user_id}:{canvas_id}:{agent_id}:{user_message['id']}"
        submission = (current.get('direction') or {}).get('_native_submission') or {}
        exact_retry = (submission.get('message_id') == identity
                       and submission.get('requested_operation') == requested_operation)
        if current["commission_ref"] != commission_ref and not exact_retry:
            raise HTTPException(409, {"code": "stale_commission_ref",
                                      "commission_ref": current["commission_ref"]})
        owner = await asyncio.to_thread(
            _claim_ordinary_owner, ctx, canvas_id=canvas_id, agent_id=agent_id,
            commission_id=commission_ref['artifact_id'],
            direction_revision=current['direction_revision'], turn_id=turn_id,
        )
        from novelvideo.chat.reference_remix_continuation import coordinator
        from novelvideo.freezone.remix.production_node_entry import _renew_submission_owner
        stop_renewal, lost_owner = asyncio.Event(), asyncio.Event()
        renewal = asyncio.create_task(_renew_submission_owner(
            coordinator.store, owner, stop_renewal, lost_owner))
        try:
            if not exact_retry:
                canvas = await asyncio.to_thread(canvas_store.read_canvas, ctx.state_dir, canvas_id)
                if not canvas or (canvas.get('project_id'), canvas.get('canvas_id')) != (ctx.project_id, canvas_id):
                    raise HTTPException(404, "canvas not found")
                _, _, _, latest_message = await asyncio.to_thread(
                    _human_context, ctx=ctx, canvas_id=canvas_id, agent_id=agent_id, turn_id=turn_id,
                )
                latest = await asyncio.to_thread(read_reference_commission, store, commission_ref, latest=True)
                latest_canvas = await asyncio.to_thread(canvas_store.read_canvas, ctx.state_dir, canvas_id)
                if (latest['commission_ref'] != current['commission_ref']
                        or latest_message['id'] != user_message['id'] or not latest_canvas
                        or latest_canvas.get('revision') != canvas.get('revision')):
                    raise HTTPException(409, {"code": "direction_source_changed"})
            if lost_owner.is_set():
                raise HTTPException(409, {'code': 'production_turn_ownership_lost'})
        except BaseException:
            await asyncio.to_thread(coordinator.store.finish_ordinary_turn,
                owner['run_id'], owner['token'], state='released',
                receipt={'error': 'direction_verification_not_published'})
            raise
        finally:
            stop_renewal.set()
            await renewal
        return {"commission_ref": current["commission_ref"],
                "direction_revision": current["direction_revision"],
                "direction_fingerprint": current["direction_fingerprint"],
                "ordinary_owner": owner,
                "human_guard": {"chat_scope_id": scope.id,
                                "message_id": user_message['id'], "turn_id": turn_id}}
    if current["commission_ref"] != commission_ref or current["phase"] != "awaiting_direction":
        raise HTTPException(409, {"code": "stale_commission_ref"})
    binding, scope, messages, user_message = await asyncio.to_thread(
        _human_context, ctx=ctx, canvas_id=canvas_id, agent_id=agent_id, turn_id=turn_id,
    )
    canvas = await asyncio.to_thread(canvas_store.read_canvas, ctx.state_dir, canvas_id)
    if not canvas or (canvas.get("project_id"), canvas.get("canvas_id")) != (ctx.project_id, canvas_id):
        raise HTTPException(404, "canvas not found")
    canvas_revision = canvas.get("revision")
    normalized_requirements = _normalize_submission_requirements(requirements)
    choices = _verified_visual_choices(preserve_replace or [], messages)
    # Recheck all server-owned sources immediately before publishing the
    # authenticated human receipt.
    _, _, _, latest_message = await asyncio.to_thread(
        _human_context, ctx=ctx, canvas_id=canvas_id, agent_id=agent_id, turn_id=turn_id,
    )
    latest = await asyncio.to_thread(read_reference_commission, store, commission_ref, latest=True)
    latest_canvas = await asyncio.to_thread(canvas_store.read_canvas, ctx.state_dir, canvas_id)
    if (latest["commission_ref"] != commission_ref or latest["phase"] != "awaiting_direction"
            or latest_message["id"] != user_message["id"] or not latest_canvas
            or latest_canvas.get("revision") != canvas_revision):
        raise HTTPException(409, {"code": "direction_source_changed"})
    from novelvideo.chat.reference_remix_continuation import coordinator
    owner = await asyncio.to_thread(
        _claim_ordinary_owner, ctx, canvas_id=canvas_id, agent_id=agent_id,
        commission_id=commission_ref['artifact_id'],
        direction_revision=latest['direction_revision'] + 1, turn_id=turn_id,
    )
    identity = f"{ctx.requester_user_id}:{canvas_id}:{agent_id}:{user_message['id']}"
    command_id = "native-direction-" + hashlib.sha256(json.dumps(
        [commission_ref, identity, goal, requested_operation], ensure_ascii=False,
        sort_keys=True,
    ).encode()).hexdigest()[:32]
    proposal = {"target_goal": goal, "_native_submission": {
        "message_id": identity, "requested_operation": requested_operation,
    }}
    if normalized_requirements:
        proposal['requirements'] = normalized_requirements
    proposal['preserve_replace'] = choices
    try:
        prepared = await asyncio.to_thread(
            prepare_reference_decision, store, commission_ref, "direction", proposal,
            goal, command_id,
        )
        result = await asyncio.to_thread(
            _publish_under_human_fence, ctx, scope=scope,
            canvas_id=canvas_id, turn_id=turn_id, message_id=user_message['id'],
            canvas_revision=canvas_revision, store=store,
            commission_ref=commission_ref, decision_ref=prepared['decision_ref'],
            identity=identity,
            message_content='\n'.join(str(row['content']) for row in messages if row.get('role') == 'user'),
        )
    except BaseException as exc:
        if owner:
            try:
                now = await asyncio.to_thread(read_reference_commission, store, commission_ref, latest=True)
                owner_state = 'blocked' if now.get('direction_revision', 0) > latest['direction_revision'] else 'released'
            except Exception:
                owner_state = 'blocked'
            await asyncio.to_thread(coordinator.store.finish_ordinary_turn,
                owner['run_id'], owner['token'], state=owner_state,
                receipt={'error': 'direction_commit_interrupted'})
        if not isinstance(exc, (ValueError, FileNotFoundError)):
            raise
        raise HTTPException(409, {"code": "direction_commit_conflict", "message": str(exc)}) from exc
    confirmed = result['commission']
    return {"commission_ref": confirmed['commission_ref'],
            "direction_revision": confirmed['direction_revision'],
            "direction_fingerprint": confirmed['direction_fingerprint'],
            "ordinary_owner": owner,
            "human_guard": {"chat_scope_id": scope.id,
                            "message_id": user_message['id'], "turn_id": turn_id}}
