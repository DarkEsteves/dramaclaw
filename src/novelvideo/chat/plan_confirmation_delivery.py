"""Bind an ordinary assistant reply to the exact reviewed plan it presents."""
from __future__ import annotations

import hashlib
from typing import Any


def prepare_plan_presentation_target(store, commission_ref: dict[str, Any], *, agent_id: str) -> dict:
    """Read the current saved plan and its one internal pending decision."""
    from novelvideo.freezone.remix.plan_review_settlement import _reviewed_commission
    from novelvideo.freezone.remix.reference_director import list_director_artifacts

    commission, _ = _reviewed_commission(store, commission_ref)
    decisions = [row for row in list_director_artifacts(
        store, commission['canvas_id'], commission['requester_user_id'], limit=None)['decisions']
        if row['kind'] == 'plan' and row['status'] == 'pending'
        and row['commission_ref'] == commission['commission_ref']
        and row['proposal'].get('plan_ref') == commission['plan_ref']]
    if len(decisions) != 1:
        raise ValueError('one current reviewed plan decision required')
    plan_ref = commission['plan_ref']
    saved = store.read(plan_ref['artifact_id'], plan_ref['revision'])
    return {'project_id': store.project_id, 'canvas_id': commission['canvas_id'],
            'requester_user_id': commission['requester_user_id'], 'agent_id': agent_id,
            'commission_ref': commission['commission_ref'],
            'decision_ref': decisions[0]['decision_ref'], 'plan_ref': plan_ref,
            'review_ref': commission['review_ref'], 'direction_revision': commission['direction_revision'],
            'plan_content_hash': saved['content_hash'], 'plan': saved['payload']}


def metadata_for_delivered_plan(store, target: dict | None, *,
                                assistant_delivered: bool, message: str) -> dict:
    """Only a completed response for an unchanged host-selected target earns metadata."""
    if not assistant_delivered or not target or not str(message or '').strip():
        return {}
    try:
        current = prepare_plan_presentation_target(
            store, target['commission_ref'], agent_id=target['agent_id'])
    except (KeyError, ValueError, FileNotFoundError):
        return {}
    if current != target:
        return {}
    identity = {key: current[key] for key in (
        'project_id', 'canvas_id', 'requester_user_id', 'agent_id',
        'commission_ref', 'decision_ref', 'plan_ref', 'review_ref',
        'direction_revision', 'plan_content_hash')}
    identity['message_hash'] = hashlib.sha256(message.encode()).hexdigest()
    return {'plan_confirmation': identity}


def find_plan_presentation(chat, username: str, scope, *, decision_ref: dict,
                           plan_ref: dict, review_ref: dict,
                           before_message_id: int | None = None,
                           requester_user_id: str | None = None) -> dict | None:
    """Find a prior message in this precise chat DB, without a recent-N cutoff."""
    if (scope.kind != 'project' or scope.surface != 'freezone'
            or not scope.canvas_id or not scope.state_dir):
        return None
    db_path = chat.read_db_for(username, scope)
    if not db_path.is_file():
        return None
    conn = chat.connect(username, scope, db_path=db_path)
    try:
        rows = conn.execute(
            """SELECT id, role, content, media_json, turn_id, metadata_json, created_at
                 FROM chat_messages
                WHERE role='assistant' AND id < ?
                  AND json_extract(metadata_json, '$.plan_confirmation.decision_ref.artifact_id')=?
                ORDER BY id DESC""",
            (before_message_id if before_message_id is not None else 2**63 - 1,
             decision_ref.get('artifact_id')),
        ).fetchall()
        for row in rows:
            message = chat._message_from_row(row)
            record = (message.get('metadata') or {}).get('plan_confirmation') or {}
            if (record.get('project_id') == scope.id
                    and record.get('canvas_id') == scope.canvas_id
                    and record.get('agent_id') == (scope.agent_id or 'main')
                    and (requester_user_id is None or record.get('requester_user_id') == requester_user_id)
                    and record.get('decision_ref') == decision_ref
                    and record.get('plan_ref') == plan_ref
                    and record.get('review_ref') == review_ref
                    and record.get('message_hash') == hashlib.sha256(
                        message['content'].encode()).hexdigest()):
                return message
    finally:
        conn.close()
    return None
