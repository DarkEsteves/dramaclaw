"""Pure current-production projection of facts read by an authorized host.

This module never discovers or adopts work. ``basis_ref`` on an action denotes
the exact current commission, plan, or work revision. Material-step actions
require the work basis; hosts resolve candidate continuity before supplying actions. ``final`` on a fact denotes a final binding, not a reference.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json


EXECUTION_STATUSES = frozenset({'idle', 'running', 'waiting_user', 'waiting_agent',
                                'recovering', 'stopped', 'failed', 'completed'})


def _unique(rows: list[dict], key: str) -> list[dict]:
    by_id = {}
    for row in rows:
        identity = row.get(key)
        if not isinstance(identity, str) or not identity:
            raise ValueError(f'{key} is required')
        if identity in by_id and by_id[identity] != row:
            raise ValueError(f'conflicting {key} facts: {identity}')
        by_id[identity] = row
    return [by_id[identity] for identity in sorted(by_id)]


def _facts(facts: list[dict]) -> list[dict]:
    normalized = []
    for fact in facts:
        if type(fact.get('available')) is not bool or type(fact.get('accepted')) is not bool:
            raise ValueError('material available and accepted must be booleans')
        if fact['accepted'] and not fact['available']:
            raise ValueError('accepted material must be available')
        if type(fact.get('final', False)) is not bool:
            raise ValueError('material final must be a boolean')
        normalized.append({key: fact.get(key, False) for key in ('step_id', 'available', 'accepted', 'final')})
    return _unique(normalized, 'step_id')


def _pending(actions: list[dict]) -> list[dict]:
    return _unique([action for action in actions if action.get('status') == 'pending'
                    and action.get('valid', True) is True], 'action_id')


def summarize_progress(facts: list[dict], actions: list[dict]) -> dict:
    """Count unique current steps, final bindings, and valid user actions."""
    facts = _facts(facts)
    actions = _pending(actions)
    return {'materials_total': len(facts),
            'materials_available': sum(fact['available'] for fact in facts),
            'materials_accepted': sum(fact['accepted'] for fact in facts),
            'final_materials_total': sum(fact['final'] for fact in facts),
            'final_materials_accepted': sum(fact['final'] and fact['accepted'] for fact in facts),
            'user_action_count': sum(action.get('owner') == 'user' for action in actions)}


def _ref(value: dict | None, project_id: str) -> dict | None:
    if value is None:
        return None
    if (not isinstance(value, dict) or not isinstance(value.get('artifact_id'), str)
            or not value['artifact_id'] or type(value.get('revision')) is not int or value['revision'] < 1
            or value.get('project_id', project_id) != project_id):
        raise ValueError('exact current project reference required')
    return {'artifact_id': value['artifact_id'], 'revision': value['revision'], 'project_id': project_id}


def build_production_state(*, scope: dict, commission: dict, plan: dict | None,
                           active_work: dict | None, material_facts: list[dict],
                           pending_actions: list[dict], execution: dict) -> dict:
    """Return reference_production_state.v1 without reading or changing state."""
    if set(scope) != {'project_id', 'canvas_id', 'agent_id'} or any(
            not isinstance(value, str) or not value for value in scope.values()):
        raise ValueError('scope requires only project_id, canvas_id and agent_id')
    project_id = scope['project_id']
    for field in ('project_id', 'canvas_id'):
        if field in commission and commission[field] != scope[field]:
            raise ValueError('commission scope differs')
    commission_ref = _ref(commission.get('commission_ref'), project_id)
    if commission_ref is None:
        raise ValueError('commission_ref required')
    plan_ref = _ref(commission.get('plan_ref'), project_id)
    selected_ref = _ref(commission.get('work_ref'), project_id)
    if plan is not None and _ref(plan.get('plan_ref'), project_id) != plan_ref:
        raise ValueError('current plan differs from commission')
    work_ref = selected_ref
    facts, actions = [], []
    blockers = []
    if active_work is not None:
        work_ref = _ref(active_work.get('work_ref'), project_id)
        if work_ref is None or selected_ref is None or work_ref['artifact_id'] != selected_ref['artifact_id']:
            raise ValueError('active work must be the explicitly selected work')
        if work_ref['revision'] < selected_ref['revision']:
            raise ValueError('selected work revision regressed')
        work_plan = _ref(active_work.get('plan_ref'), project_id)
        if plan_ref is None or work_plan is None or work_plan['artifact_id'] != plan_ref['artifact_id']:
            raise ValueError('selected work belongs to another plan')
        final_ids = {row['final_step_id'] for row in active_work.get('requirements', [])}
        facts = _facts([{**row, 'final': row['step_id'] in final_ids} for row in material_facts])
        if 'steps' in active_work and {row['step_id'] for row in facts} != {row['id'] for row in active_work['steps']}:
            raise ValueError('material facts must cover all current steps')
    elif selected_ref is not None:
        blockers.append({'owner': 'system', 'kind': 'selected_work_unavailable', 'action_id': None})
    elif commission['phase'] == 'preparing_materials':
        blockers.append({'owner': 'user', 'kind': 'work_selection_required', 'action_id': None})
    # Nonmaterial decisions use the exact current commission or plan basis.
    # Filter history before deduplication, since old attempts can share IDs.
    current_actions = []
    step_ids = {row['step_id'] for row in facts}
    for action in pending_actions:
        if action.get('status') != 'pending' or action.get('valid', True) is not True:
            continue
        basis = action.get('basis_ref')
        if basis is None:
            continue
        basis = _ref(basis, project_id)
        is_material = active_work is not None and basis == work_ref
        is_decision = action.get('step_id') is None and basis in (commission_ref, plan_ref)
        if not (is_material or is_decision):
            continue
        if action.get('step_id') is not None and action['step_id'] not in step_ids:
            continue
        if action.get('owner') not in {'system', 'agent', 'user'} or not isinstance(action.get('kind'), str):
            raise ValueError('pending action requires owner and kind')
        current_actions.append({**{key: deepcopy(action.get(key)) for key in
                                ('action_id', 'kind', 'owner', 'step_id', 'status')},
                                **{key: deepcopy(action[key]) for key in ('tool', 'draft_id', 'revision', 'request_id') if key in action}, 'basis_ref': basis})
    actions = _pending(current_actions)
    if active_work is None and any(action['kind'] in {'prepare_materials', 'work_selection_required'} for action in actions):
        blockers = [row for row in blockers if row['kind'] != 'work_selection_required']
    for blocker in execution.get('blockers', []):
        if blocker.get('owner') not in {'system', 'agent', 'user'} or not isinstance(blocker.get('kind'), str):
            raise ValueError('blocker requires owner and kind')
        blockers.append({key: deepcopy(blocker.get(key)) for key in ('owner', 'kind', 'action_id')})
    blockers.extend({key: deepcopy(action[key]) for key in
                     ('owner', 'kind', 'action_id', 'tool', 'draft_id', 'revision', 'request_id') if key in action} for action in actions)
    blockers = sorted(blockers, key=lambda row: json.dumps(row, sort_keys=True))
    status = execution.get('status', 'idle')
    if status not in EXECUTION_STATUSES:
        raise ValueError('invalid execution status')
    if commission['phase'] == 'completed' and status not in {'stopped', 'failed', 'recovering'}:
        status = 'completed'
    next_action = None
    if status not in {'stopped', 'failed', 'completed', 'running', 'recovering'}:
        priorities = {'system': 0, 'agent': 1, 'user': 2}
        choices = sorted(blockers, key=lambda row: (priorities[row['owner']], row['kind'], row['action_id'] or ''))
        if choices:
            choice = choices[0]
            next_action = {**choice, 'kind': 'select_material_work' if choice['kind'] == 'work_selection_required' else choice['kind']}
    result = {'schema_version': 'reference_production_state.v1', 'scope': deepcopy(scope),
              'commission_ref': commission_ref, 'plan_ref': plan_ref, 'work_ref': work_ref,
              'phase': commission['phase'], 'execution_status': status,
              'progress': summarize_progress(facts, actions), 'blockers': blockers, 'next_action': next_action}
    # Include individual normalized facts, so changed current evidence with the
    # same aggregate counts still invalidates the projection revision.
    digest = {'projection': result, 'material_facts': facts, 'pending_actions': sorted(actions, key=lambda row: row['action_id'])}
    result['projection_revision'] = hashlib.sha256(json.dumps(digest, ensure_ascii=False, sort_keys=True,
                                                            separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    return result


def native_plan_execution_status(stage: dict) -> str:
    """Project only the current native slots; the creative phase is not a task."""
    slots = stage.get('slots') or []
    states = {slot.get('state') for slot in slots} or {stage.get('state')}
    if states & {'failed', 'delivery_unverified', 'binding_conflict', 'resolve_conflict'}:
        return 'failed'
    if any(slot.get('state') == 'running' and slot.get('task_id') for slot in slots):
        return 'running'
    # Submission and delivery wait for a host event, but are not running proof.
    if states & {'submitted', 'awaiting_canvas_receipt', 'awaiting_task_receipt'}:
        return 'idle'
    if states == {'ready_for_review'}:
        return 'waiting_agent'
    return 'waiting_agent'
