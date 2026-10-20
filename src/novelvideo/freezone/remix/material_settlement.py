"""Recover native material registration and checks from durable server evidence.

The existing production observer calls this service. It never generates media or
accepts a candidate. Check schedulers must durably deduplicate ``command_id`` and
return a real check/review reference; they run after registration, outside locks.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Callable

from .operations import RemixOperationStore
from .material_execution import (canvas_material_resource, register_material_execution,
                                 review_material_candidate, validate_material_native_execution, material_step_target)


def settlement_key(scope: dict, task: dict, result: dict) -> str:
    """Digest authorization scope, exact native task and measured output version."""
    return hashlib.sha256(json.dumps({'scope': scope, 'task': task, 'result': result},
        sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _public(record: dict) -> dict:
    return {key: deepcopy(record[key]) for key in ('settlement_id', 'result_key', 'checkpoint',
        'status', 'reason', 'work_ref', 'step_id', 'resource', 'review_ref', 'check_ref') if key in record}


def _current(store, original: dict, step_id: str, task: dict) -> tuple[dict, bool]:
    latest = store.read_latest(original['id'])
    step = next((s for s in latest['payload']['steps'] if s['id'] == step_id), None)
    plan_ref = original['payload']['plan_ref']
    plan_current = store.read_latest(plan_ref['artifact_id'])['revision'] == plan_ref['revision']
    execution = (step or {}).get('execution') or {}
    original_step = next(s for s in original['payload']['steps'] if s['id'] == step_id)
    original_execution = original_step.get('execution') or {}
    same = bool(step and plan_current and latest['payload']['plan_ref'] == plan_ref
        and step['status'] not in {'stale', 'rejected'}
        and material_step_target(step) == material_step_target(original_step)
        and [r for r in latest['payload']['requirements'] if r['final_step_id'] == step_id]
            == [r for r in original['payload']['requirements'] if r['final_step_id'] == step_id]
        and all(execution.get(key) == original_execution.get(key)
                for key in ('draft_id', 'draft_revision', 'workflow_instance_id', 'plan_node_id', 'canvas_id'))
        and execution.get('run_id', task['run_id']) == task['run_id']
        and execution.get('task_id', task['task_key']) == task['task_key']
        and (not step.get('node_id') or step['node_id'] == task['node_id']))
    return latest, same


def settle_material_result(*, store, canvas: dict, work_ref: dict, step_id: str,
    native_result: dict, workflow_state_dir: Path,
    check_scheduler: Callable[..., dict] | None = None, active: bool = True,
    allow_cancelled_completed: bool = False) -> dict:
    """Register a native completion, then schedule checks using a fixed command.

    ``native_result`` is an identity hint from saved workflow actions; its status,
    output and media hashes are never trusted. ``active=False`` stores historical
    completion evidence without registering, checking or waking old generations.
    """
    from novelvideo.freezone.workflow_runs import read_workflow_run

    original = store.read(work_ref['artifact_id'], work_ref['revision'])
    if original['schema'] != 'material_work_package.v1':
        raise ValueError('material work package required')
    step = next((s for s in original['payload']['steps'] if s['id'] == step_id), None)
    if not step or step['source'] != 'generate':
        raise ValueError('native settlement requires a generated material step')
    execution = step.get('execution') or {}
    task = {key: native_result.get(key) for key in ('run_id', 'node_id', 'task_key')}
    if any(not isinstance(value, str) or not value for value in task.values()):
        return {'status': 'registered', 'reason': 'native_result_unavailable'}
    scope = {'project_id': store.project_id, 'requester_user_id': store.created_by,
             'canvas_id': execution.get('canvas_id'), 'work_id': original['id'], 'step_id': step_id}
    identity = settlement_key(scope, task, {})
    operations = RemixOperationStore(store.project_dir, store.project_id)
    record = operations.get_settlement(identity)
    if record and record.get('authorization_work_ref'):
        bound = record['authorization_work_ref']
        original = store.read(bound['artifact_id'], bound['revision'])
        step = next(s for s in original['payload']['steps'] if s['id'] == step_id)
        execution = step.get('execution') or {}
    run = read_workflow_run(project_dir=workflow_state_dir, canvas_id=execution.get('canvas_id', ''), run_id=task['run_id'])
    action = next((a for a in (run or {}).get('actions', []) if a.get('node_id') == task['node_id']
        and a.get('task_key') == task['task_key'] and a.get('recipe_id') == step.get('recipe_id')), None)
    if run and (run.get('project_id') != store.project_id or run.get('canvas_id') != scope['canvas_id']):
        raise PermissionError('native material scope differs')
    latest, current = _current(store, original, step_id, task)
    stopped = not active or (
        (run or {}).get('status') == 'cancelled' and not allow_cancelled_completed
    )
    if record and record.get('disposition'):
        return {**_public(record), 'status': 'stale', 'reason': 'generation_superseded'}
    current = current and not stopped
    if not current:
        # The disposition outlives transient run rows and caller activity hints.
        record = operations.checkpoint_settlement(identity, scope=scope, task=task,
            step_id=step_id, work_ref=(record or {}).get('work_ref', work_ref),
            status='stale', reason='generation_superseded', disposition='historical',
            authorization_work_ref=(record or {}).get('authorization_work_ref', work_ref))
    if record and record.get('checkpoint') in {'checks_scheduled', 'notified'}:
        if not current:
            return {**_public(record), 'status': 'stale', 'reason': 'generation_superseded'}
        # Cleanup of native drafts/actions requires full saved authorization and output proof.
        try:
            validate_material_native_execution(store, canvas, {**step, 'execution': {
                **execution, 'run_id': task['run_id'], 'task_id': task['task_key']}}, task['node_id'],
                require_complete=True, workflow_state_dir=workflow_state_dir,
                settlement_id=identity, work_ref={'artifact_id':latest['id'],'revision':latest['revision']})
        except (ValueError, FileNotFoundError):
            return {'status': 'registered', 'reason': 'native_result_unavailable'}
        evidence = record.get('native_evidence') or {}
        if evidence.get('status') != 'completed' or not evidence.get('result_ref'):
            return {'status': 'registered', 'reason': 'native_result_unavailable'}
        try:
            resource = canvas_material_resource(store, canvas, task['node_id'], step['media_kind'])
            if resource != record['resource']:
                return {**_public(record), 'status': 'stale', 'reason': 'material_changed'}
        except (ValueError, FileNotFoundError):
            return {**_public(record), 'status': 'stale', 'reason': 'material_unavailable'}
        accepted = next(s for s in latest['payload']['steps'] if s['id'] == step_id)
        if accepted['status'] == 'accepted' and accepted.get('accepted_resource') == resource:
            return {**_public(record), 'status': 'accepted',
                'work_ref': {'artifact_id': latest['id'], 'revision': latest['revision']},
                'review_ref': accepted['review_ref']}
        return _public(record)
    if not action and record:
        proof = record.get('native_evidence') or {}
        if (record.get('scope') == scope and proof.get('status') == 'completed'
                and proof.get('artifact_status') == 'valid' and proof.get('result_ref')
                and all(proof.get(key) == value for key, value in task.items())
                and proof.get('execution')):
            action = proof
    if not action:
        return _public(record) if stopped else {'status': 'registered', 'reason': 'native_result_unavailable'}
    if current and record and record.get('resource'):
        try:
            resource = canvas_material_resource(store, canvas, task['node_id'], step['media_kind'])
        except (ValueError, FileNotFoundError):
            resource = None
        if resource != record['resource']:
            return _public(operations.checkpoint_settlement(identity, status='stale',
                reason='material_changed', disposition='historical'))
    verified = {**{key: value for key, value in execution.items() if key != 'settlement_command_id'},
                'run_id': task['run_id'], 'task_id': task['task_key']}
    native_step = {**step, 'execution': verified}
    validate_material_native_execution(store, canvas, native_step, task['node_id'], workflow_state_dir=workflow_state_dir, settlement_id=identity, work_ref=work_ref)
    record = operations.checkpoint_settlement(identity, checkpoint='discovered', scope=scope,
        task=task, step_id=step_id, work_ref=(record or {}).get('work_ref', work_ref), status='registered',
        authorization_work_ref=(record or {}).get('authorization_work_ref', work_ref))
    if action.get('status') in {'failed', 'cancelled', 'blocked'}:
        return _public(operations.checkpoint_settlement(identity, status='failed', reason='native_task_' + action['status']))
    complete = action.get('status') == 'completed' and action.get('artifact_status') == 'valid' and bool(action.get('result_ref'))
    if complete:
        try:
            validate_material_native_execution(store, canvas, native_step, task['node_id'], require_complete=True, workflow_state_dir=workflow_state_dir, settlement_id=identity, work_ref=work_ref)
            resource = canvas_material_resource(store, canvas, task['node_id'], step['media_kind'])
        except (ValueError, FileNotFoundError):
            # Provider completion can precede durable output publication. A
            # missing or unverified file never authorizes another generation.
            complete = False
    if complete:
        evidence = {'run_id': task['run_id'], 'node_id': task['node_id'], 'execution': verified,
            **{key: action.get(key) for key in ('task_key', 'task_type', 'job_id', 'recipe_id', 'status', 'artifact_status', 'result_ref', 'node_revision')}}
        result_key = settlement_key(scope, {**task, 'task_type': action.get('task_type'), 'job_id': action.get('job_id')},
            {'revision': action.get('node_revision'), 'result_ref': action['result_ref'], 'media_hash': resource['media_hash']})
        from novelvideo.freezone.workflow_drafts import read_workflow_draft
        draft, error = read_workflow_draft(project_dir=workflow_state_dir,
            canvas_id=execution['canvas_id'], draft_id=execution['draft_id'])
        authorization = (record or {}).get('native_authorization')
        if not authorization and not error and draft and draft.get('status') == 'confirmed':
            authorization = {key: draft.get(key) for key in
                ('draft_id', 'revision', 'project_id', 'canvas_id', 'status', 'confirmed_at', 'task_id')}
        record = operations.checkpoint_settlement(identity, native_evidence=evidence, resource=resource,
            result_key=result_key, native_authorization=authorization,
            authorization_work_ref=(record or {}).get('authorization_work_ref', work_ref))
    if not current:
        return _public(operations.checkpoint_settlement(identity, status='stale', reason='generation_superseded', disposition='historical'))
    if step.get('status') == 'accepted':
        return _public(operations.checkpoint_settlement(identity, status='accepted'))
    if record['checkpoint'] == 'discovered':
        registered = register_material_execution(store, {'artifact_id': latest['id'], 'revision': latest['revision']},
            step_id, task['node_id'], verified, command_id='settle:' + identity)
        record = operations.checkpoint_settlement(identity, checkpoint='registered', work_ref=registered['work_ref'])
    if not complete:
        return {**_public(record), 'reason': 'native_result_unavailable'}
    # Recheck after publication before any external enqueue. No lock spans checks.
    latest, current = _current(store, original, step_id, task)
    if not current:
        return _public(operations.checkpoint_settlement(identity, status='stale', reason='generation_superseded', disposition='historical'))
    command_id = 'material-check:' + record['result_key']
    # Replay an actual published technical review after a checkpoint crash.
    review_id = hashlib.sha256(('material-review:' + command_id).encode()).hexdigest()
    try:
        saved_review = store.read(review_id, 1)
    except FileNotFoundError:
        saved_review = None
    if saved_review:
        from .material_review import validate_material_review_lineage
        review = saved_review['payload']
        validate_material_review_lineage(store, review, saved_review['upstream_refs'])
        if (saved_review['schema'] != 'material_review.v1' or saved_review['created_by'] != store.created_by
                or review['work_ref']['artifact_id'] != original['id'] or review['step_id'] != step_id
                or review['resource'] != record['resource']):
            raise ValueError('saved material check differs from settlement')
        record = operations.checkpoint_settlement(identity, work_ref=review['work_ref'])
        checks = {'status': 'needs_review', 'review_ref': {'artifact_id': review_id, 'revision': 1}}
    else:
        resource = canvas_material_resource(store, canvas, task['node_id'], step['media_kind'])
        if resource != record['resource']:
            return _public(operations.checkpoint_settlement(identity, status='stale',
                reason='material_changed', disposition='historical'))
        latest_ref = {'artifact_id': latest['id'], 'revision': latest['revision']}
        record = operations.checkpoint_settlement(identity, work_ref=latest_ref)
        if check_scheduler is None:
            checked = review_material_candidate(store, latest_ref, step_id, canvas, task['node_id'],
                workflow_state_dir=workflow_state_dir, command_id=command_id, settlement_id=identity)
            checks = {'status': 'needs_review', 'review_ref': checked['review_ref']}
        else:
            checks = check_scheduler(store=store, canvas=canvas, work_ref=latest_ref, step_id=step_id,
                resource=record['resource'], native_result=record['native_evidence'], workflow_state_dir=workflow_state_dir,
                command_id=command_id, settlement_id=identity)
            if not isinstance(checks, dict) or checks.get('status') not in {'checking', 'needs_review', 'failed'} or not (checks.get('check_ref') or checks.get('review_ref')):
                raise ValueError('material check scheduler requires a durable check or review reference')
    record = operations.checkpoint_settlement(identity, checkpoint='checks_scheduled',
        **{key: checks[key] for key in ('status', 'review_ref', 'check_ref') if key in checks})
    return _public(record)

