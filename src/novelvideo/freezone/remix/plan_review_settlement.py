"""Settle one real plan review and its internal conversational confirmation target."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .plan_revision_settlement import reconcile_plan_revision
from .production_review import ReviewFinding, evaluate_saved_production_review, review_production_plan
from .reference_commission import advance_reference_commission, read_reference_commission


def _identity(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                  separators=(',', ':')).encode()).hexdigest()


def _exact_ref(store, saved: dict[str, Any]) -> dict[str, Any]:
    return {'artifact_id': saved['id'], 'revision': saved['revision'], 'project_id': store.project_id}


def _reviewed_commission(store, commission_ref: dict[str, Any]) -> tuple[dict, dict]:
    commission = read_reference_commission(store, commission_ref, latest=True)
    if commission['phase'] != 'awaiting_plan_confirmation' or not commission.get('review_ref'):
        raise ValueError('saved passing review required before plan confirmation')
    review_ref, plan_ref = commission['review_ref'], commission['plan_ref']
    review = store.read(review_ref['artifact_id'], review_ref['revision'])
    plan = store.read_latest(plan_ref['artifact_id'])
    if (review['schema'] != 'production_review.v1' or review['created_by'] != store.created_by
            or review['payload']['status'] != 'passed'
            or review['payload']['plan_ref'] != plan_ref
            or review['payload']['direction_fingerprint'] != commission['direction_fingerprint']
            or plan['revision'] != plan_ref['revision']):
        raise ValueError('current reviewed plan required')
    return commission, review


def ensure_reviewed_plan_confirmation(store, *, commission_ref: dict[str, Any]) -> dict[str, Any]:
    """Recover only a decision for the commission's bound, current passed review."""
    from .reference_director import prepare_reference_decision, reviewed_plan_command_id

    commission, review = _reviewed_commission(store, commission_ref)
    command_id = reviewed_plan_command_id(commission)
    decision = prepare_reference_decision(
        store, commission['commission_ref'], 'plan', {'plan_ref': commission['plan_ref']},
        '当前作品方案已审核，请在对话中确认或提出修改意见。', command_id,
    )
    return {'review_ref': commission['review_ref'], 'review': review['payload'],
            'commission': commission, 'decision_ref': decision['decision_ref'],
            'decision': decision['decision'], 'settlement_key': command_id}


def verified_plan_review_settlement(store, receipt: dict[str, Any]) -> dict[str, Any] | None:
    """Check a tool receipt against current stored review and pending decision."""
    from .reference_director import reviewed_plan_command_id

    try:
        supplied = receipt['commission']
        commission, review = _reviewed_commission(store, supplied['commission_ref'])
        if (supplied != commission or receipt['review_ref'] != commission['review_ref']
                or receipt['decision_ref']['artifact_id'] != hashlib.sha256(
                    (commission['commission_ref']['artifact_id'] + ':decision:'
                     + reviewed_plan_command_id(commission)).encode()).hexdigest()[:32]):
            return None
        decision = store.read_latest(receipt['decision_ref']['artifact_id'])
        if (receipt['decision_ref']['revision'] != decision['revision']
                or decision['schema'] != 'reference_decision.v1'
                or decision['created_by'] != store.created_by
                or decision['payload']['kind'] != 'plan'
                or decision['payload']['status'] != 'pending'
                or decision['payload']['commission_ref'] != commission['commission_ref']
                or decision['payload']['proposal'].get('plan_ref') != commission['plan_ref']):
            return None
    except (KeyError, TypeError, ValueError, FileNotFoundError):
        return None
    return {'verified': True, 'project_id': store.project_id,
            'canvas_id': commission['canvas_id'],
            'requester_user_id': commission['requester_user_id'],
            'commission_id': commission['commission_ref']['artifact_id'],
            'plan_ref': commission['plan_ref'],
            'direction_revision': commission['direction_revision'],
            'review_status': review['payload']['status'],
            'decision_status': decision['payload']['status']}


def submit_plan_review(store, *, commission_ref: dict[str, Any], plan_ref: dict[str, Any],
                       findings: list[dict[str, Any]]) -> dict[str, Any]:
    """Audit the exact saved plan, then finish the current stage and pending record."""
    if (not isinstance(plan_ref, dict)
            or not isinstance(plan_ref.get('artifact_id'), str)
            or not plan_ref['artifact_id']
            or type(plan_ref.get('revision')) is not int
            or plan_ref['revision'] < 1
            or plan_ref.get('project_id', store.project_id) != store.project_id):
        raise ValueError('plan_ref must identify a revision in the current project')
    plan_ref = {**plan_ref, 'project_id': store.project_id}
    commission = read_reference_commission(store, commission_ref, latest=True)
    if commission['requester_user_id'] != store.created_by or commission['project_id'] != store.project_id:
        raise PermissionError('plan review actor or project differs')
    saved_plan = store.read(plan_ref['artifact_id'], plan_ref['revision'])
    if saved_plan['schema'] != 'production_plan.v2':
        raise ValueError('plan_ref must identify a saved production plan')
    direction_artifact = store.read(commission['commission_ref']['artifact_id'],
                                    commission['commission_ref']['revision'])
    evaluate_saved_production_review(store, saved_plan['payload'], direction_artifact, findings,
                                     plan_ref=plan_ref)
    if commission.get('plan_ref'):
        received = reconcile_plan_revision(store, commission_ref=commission['commission_ref'],
                                           plan_ref=plan_ref)
        commission = received['commission']
    if (commission['phase'] == 'awaiting_plan_confirmation'
            and commission['plan_ref'] == plan_ref):
        reviewed = store.read(commission['review_ref']['artifact_id'],
                              commission['review_ref']['revision'])
        requested_findings = [ReviewFinding.model_validate(row).model_dump(mode='json', exclude_none=True)
                              for row in findings]
        if reviewed['payload']['findings'] != requested_findings:
            raise ValueError('review request differs from the current passed review; revise the plan before reviewing again')
        return ensure_reviewed_plan_confirmation(store, commission_ref=commission['commission_ref'])
    if commission['phase'] not in {'drafting_plan', 'awaiting_plan_review'}:
        raise ValueError('current plan is not awaiting review')
    if commission.get('plan_ref') != plan_ref or commission['phase'] != 'awaiting_plan_review':
        commission = advance_reference_commission(store, commission['commission_ref'],
            {'type': 'plan_ready', 'plan_ref': plan_ref}, commission['commission_ref']['revision'])
    review_id = _identity([store.project_id, commission['canvas_id'],
        commission['requester_user_id'], commission['commission_ref']['artifact_id'],
        commission['direction_fingerprint'], plan_ref, findings])[:32]
    review = review_production_plan(store, plan_ref, commission['commission_ref'],
                                    findings, review_id=review_id)
    review_ref = _exact_ref(store, review)
    if review['payload']['status'] != 'passed':
        return {'review_ref': review_ref, 'review': review['payload'], 'commission': commission}
    commission = advance_reference_commission(store, commission['commission_ref'],
        {'type': 'plan_reviewed', 'review_ref': review_ref}, commission['commission_ref']['revision'])
    return ensure_reviewed_plan_confirmation(store, commission_ref=commission['commission_ref'])
