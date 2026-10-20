"""Deterministic checks for persisted human replies to director proposals.

Ordinary reference conversation is handled by the main Agent. The active chat
route only uses this module to locate the exact presented proposal; the explicit
Agent confirmation action owns plan confirmation. The legacy apply helper below
is retained for compatible card and migration callers and never performs model
interpretation itself.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from novelvideo.chat.store import ChatScope
from novelvideo.freezone.remix.reference_director import (
    confirm_reference_decision,
    list_director_artifacts,
    prepare_reference_decision,
)

_PHASES = {'direction': 'awaiting_direction', 'plan': 'awaiting_plan_confirmation', 'composition': 'reviewing_output'}
_HUMAN_CREDENTIALS = {'browser_session', 'user_session'}


def _authorized_message(store, scope, requester_user_id, credential_kind, message):
    return (credential_kind in _HUMAN_CREDENTIALS and message.get('role') == 'user'
            and isinstance(message.get('id'), int) and not isinstance(message.get('id'), bool)
            and message['id'] > 0 and bool(str(message.get('content') or '').strip())
            and scope.kind in {'project', 'freezone'} and scope.id == store.project_id
            and scope.surface == 'freezone' and bool(scope.canvas_id)
            and requester_user_id == store.created_by)


def pending_user_decision(store, *, scope: ChatScope, requester_user_id: str,
                          credential_kind: str, message: dict[str, Any]) -> dict[str, Any] | None:
    """Return exactly one current decision; never infer one from chat wording."""
    if not _authorized_message(store, scope, requester_user_id, credential_kind, message):
        return None
    artifacts = list_director_artifacts(store, scope.canvas_id, requester_user_id, limit=None)
    commissions = {c['commission_ref']['artifact_id']: c for c in artifacts['commissions']}
    candidates = []
    for decision in artifacts['decisions']:
        commission = commissions.get(decision['commission_ref']['artifact_id'])
        if not commission or decision['kind'] not in _PHASES:
            continue
        if (_PHASES[decision['kind']] != commission['phase']
                or decision['commission_ref']['revision'] != commission['commission_ref']['revision']):
            continue
        if decision['kind'] == 'plan' and decision['proposal'].get('plan_ref') != commission.get('plan_ref'):
            continue
        if decision['kind'] == 'plan':
            from novelvideo.chat.plan_confirmation_delivery import find_plan_presentation
            from novelvideo.chat.store import ChatStore
            if not find_plan_presentation(
                ChatStore(), requester_user_id, scope,
                decision_ref=decision['decision_ref'], plan_ref=commission['plan_ref'],
                review_ref=commission['review_ref'], before_message_id=message['id'],
                requester_user_id=requester_user_id,
            ):
                continue
        candidates.append(decision)
    return candidates[0] if len(candidates) == 1 else None


def ensure_explicit_direction_decision(store, commission: dict[str, Any],
                                       run: dict[str, Any]) -> dict[str, Any] | None:
    """Save an already supplied creative direction before inviting confirmation.

    This prepares a proposal only. The authenticated chat/card ingress still
    owns confirmation and the following commission transition.
    """
    direction = (run.get('inputs') or {}).get('remix_direction')
    if (commission.get('phase') != 'awaiting_direction' or not isinstance(direction, str)
            or not direction.strip() or any(run.get(key) != commission.get(key) for key in
                                         ('project_id', 'canvas_id', 'requester_user_id', 'analysis_node_id'))):
        return None
    current_ref = commission['commission_ref']
    proposal = {'target_goal': run['goal'], 'remix_direction': direction.strip()}
    pending = [decision for decision in list_director_artifacts(
        store, commission['canvas_id'], commission['requester_user_id'], limit=None)['decisions']
        if decision['kind'] == 'direction' and decision['commission_ref'] == current_ref]
    matching = next((decision for decision in pending if decision['proposal'] == proposal), None)
    for decision in pending:
        if decision is matching:
            continue
        saved = store.read_latest(decision['decision_ref']['artifact_id'])
        if saved['payload']['status'] == 'pending':
            store.revise(saved['id'], saved['revision'],
                         {**saved['payload'], 'status': 'superseded'}, saved['upstream_refs'])
    if matching:
        return {'decision_ref': matching['decision_ref'], 'decision': matching, 'created': False}
    result = prepare_reference_decision(store, current_ref, 'direction', proposal,
                                        direction.strip(), 'explicit-direction-' + run['run_id'])
    return {**result, 'created': True}


def apply_user_confirmation(
    store, *, scope: ChatScope, requester_user_id: str,
    credential_kind: str, message: dict[str, Any],
    canvas: dict | None = None, _canvas_guard=None,
    interpretation: dict[str, Any] | None = None,
    expected_decision_ref: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Apply an unambiguous stored user reply within a trusted project scope.

    Callers must pass the ChatStore append result, never request/model metadata.
    The returned state belongs to the current ordinary user turn; no background
    Agent is started here.
    """
    if not _authorized_message(store, scope, requester_user_id, credential_kind, message):
        return None
    if isinstance(interpretation, dict) and 'requirements' in interpretation:
        from novelvideo.freezone.remix.production_requirements import normalize_requirements
        try:
            interpretation = {**interpretation, 'requirements': normalize_requirements(interpretation['requirements'])}
        except ValueError:
            return None
    intent = interpretation.get('intent') if isinstance(interpretation, dict) else None
    if intent not in {'approve', 'approve_with_changes', 'revise'}:
        return None
    artifacts = list_director_artifacts(store, scope.canvas_id, requester_user_id, limit=None)
    commissions = {c['commission_ref']['artifact_id']: c for c in artifacts['commissions']}
    message_identity = f"{requester_user_id}:{scope.canvas_id}:{scope.agent_id or 'main'}:{message['id']}"
    receipt = f"chat-message:{message_identity}"
    # A repeated delivery of the same persisted message must not confirm a new
    # card after its original card has disappeared.
    for commission in commissions.values():
        if any(d.get('message_id') == receipt for d in commission['decisions']):
            return {'commission': commission, 'message_id': receipt, 'replayed': True}
    if 'requirements' in interpretation:
        from novelvideo.freezone.remix.production_requirements import snapshot_confirmed_dialogue
        ref = expected_decision_ref or {}
        previous = next((c.get('direction') for c in commissions.values()
                         if c['commission_ref']['artifact_id'] == ref.get('artifact_id')), None)
        if previous is None:
            matching = [d for d in artifacts['decisions'] if d['decision_ref'] == expected_decision_ref]
            if len(matching) == 1:
                previous = commissions[matching[0]['commission_ref']['artifact_id']].get('direction')
        try:
            snapshot_confirmed_dialogue({'requirements': interpretation['requirements']},
                previous=previous, message_content=message['content'])
        except ValueError:
            return None
    if expected_decision_ref and expected_decision_ref.get('kind') == 'requirement_amendment':
        candidate = current_requirement_amendment(store, scope=scope, requester_user_id=requester_user_id,
            credential_kind=credential_kind, message=message)
        if not candidate or candidate['decision_ref'] != expected_decision_ref or intent not in {'revise', 'approve_with_changes'}:
            return None
        if not isinstance(interpretation.get('requirements'), dict):
            return None
        from novelvideo.freezone.remix.production_requirements import apply_requirement_amendment
        from novelvideo.freezone.remix.reference_commission import AuthenticatedDecisionEvidence
        amended = apply_requirement_amendment(store, commission_ref=candidate['commission_ref'],
            requirements=interpretation['requirements'], canvas=canvas, _canvas_guard=_canvas_guard,
            host_evidence=AuthenticatedDecisionEvidence(store.project_id, scope.canvas_id, requester_user_id, receipt, message['content']))
        return {'commission': amended, 'message_id': receipt, 'kind': 'requirement_amendment', 'user_message_id': message['id']}
    decision = pending_user_decision(store, scope=scope, requester_user_id=requester_user_id,
                                     credential_kind=credential_kind, message=message)
    if not decision or not expected_decision_ref or decision['decision_ref'] != expected_decision_ref:
        return None
    if decision['kind'] == 'plan' and intent in {'revise', 'approve_with_changes'} and isinstance(interpretation.get('requirements'), dict):
        from novelvideo.freezone.remix.production_requirements import apply_requirement_amendment
        from novelvideo.freezone.remix.reference_commission import AuthenticatedDecisionEvidence
        amended = apply_requirement_amendment(store, commission_ref=decision['commission_ref'],
            requirements=interpretation['requirements'], canvas=canvas, _canvas_guard=_canvas_guard,
            host_evidence=AuthenticatedDecisionEvidence(store.project_id, scope.canvas_id, requester_user_id, receipt, message['content']))
        return {'commission': amended, 'message_id': receipt, 'kind': 'requirement_amendment', 'user_message_id': message['id']}
    if intent == 'revise':
        return None
    if (intent == 'approve' and decision['kind'] == 'direction' and 'requirements' in interpretation
            and interpretation['requirements'] != decision['proposal'].get('requirements')):
        # The interpreter can extract constraints already stated in the saved
        # direction. Keep its prose unchanged and retain the same human receipt;
        # this is a typed representation of the approved proposal, not a change
        # attributed to the user's acceptance reply.
        proposal = {**decision['proposal'], 'requirements': interpretation['requirements']}
        command_id = 'chat-requirements-' + hashlib.sha256(
            json.dumps([decision['decision_ref'], receipt], sort_keys=True).encode()).hexdigest()[:32]
        prepared = prepare_reference_decision(store, decision['commission_ref'], 'direction',
            proposal, decision['summary'], command_id)
        decision = prepared['decision'] | {'decision_ref': prepared['decision_ref']}
    if intent == 'approve_with_changes':
        if decision['kind'] != 'direction':
            return None  # A changed plan or output needs another saved review.
        revision = str(message['content']).strip()
        revised_proposal = {**decision['proposal'], 'user_requested_change': revision}
        if 'requirements' in interpretation:
            from novelvideo.freezone.remix.production_requirements import normalize_requirements
            revised_proposal['requirements'] = normalize_requirements(interpretation['requirements'])
        for field in ('remix_direction', 'direction'):
            if isinstance(revised_proposal.get(field), str):
                revised_proposal[field] = revised_proposal[field].rstrip() + '\n用户补充：' + revision
                break
        command_id = 'chat-revision-' + hashlib.sha256(
            json.dumps([decision['decision_ref'], receipt], sort_keys=True).encode()).hexdigest()[:32]
        revised = prepare_reference_decision(store, decision['commission_ref'], 'direction',
                                             revised_proposal, revision, command_id)
        decision = revised['decision'] | {'decision_ref': revised['decision_ref']}
    result = confirm_reference_decision(store, decision['decision_ref'],
        project_id=store.project_id, canvas_id=scope.canvas_id,
        requester_user_id=requester_user_id, message_id=message_identity, message_content=message['content'])
    return {**result, 'message_id': receipt, 'kind': decision['kind'], 'user_message_id': message['id'], 'scope': scope.to_dict()}


def current_requirement_amendment(store, *, scope: ChatScope, requester_user_id: str,
                                  credential_kind: str, message: dict) -> dict | None:
    """Offer semantic context for the sole bound plan even without a pending decision."""
    if not _authorized_message(store, scope, requester_user_id, credential_kind, message):
        return None
    commissions = [c for c in list_director_artifacts(store, scope.canvas_id, requester_user_id, limit=None)['commissions']
                   if c.get('plan_ref') and c.get('direction')]
    if len(commissions) != 1:
        return None
    c = commissions[0]
    return {'kind': 'requirement_amendment', 'summary': c['goal'],
            'proposal': {'direction': c['direction'], 'requirements': c['direction'].get('requirements', {}), 'plan_ref': c['plan_ref']},
            'commission_ref': c['commission_ref'], 'decision_ref': {**c['commission_ref'], 'kind': 'requirement_amendment'}}
