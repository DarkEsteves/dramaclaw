"""Exact browser-selected material confirmations with recoverable per-item receipts."""
from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import re


def confirm_material_review_batch(store, ctx, canvas: dict, *, canvas_id: str,
                                  items: list[dict], command_id: str, _canvas_guard=None) -> dict:
    from .material_execution import canvas_material_resource, confirm_material_review
    from .material_review import validate_material_review_lineage
    from .material_work import _exact_ref, _fingerprint, _IndependentMaterialReviewContinuity
    from .production_inventory import _LockedEvidenceStore
    from .reference_director import _MaterialPublicationGuard
    from .source_speech import candidate_scope

    scope = candidate_scope(ctx, canvas, canvas_id)
    if store.project_id != ctx.project_id or store.project_dir != Path(ctx.output_dir).resolve():
        raise ValueError('batch store differs from authorized project')
    if not isinstance(command_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', command_id):
        raise ValueError('invalid batch command')
    if not isinstance(items, list) or not 1 <= len(items) <= 20:
        raise ValueError('select between one and twenty exact materials')
    ids = set()
    for item in items:
        if set(item) != {'review_ref', 'work_ref', 'step_id', 'node_id'}:
            raise ValueError('exact checked material identity required')
        _exact_ref(item['review_ref'])
        _exact_ref(item['work_ref'])
        if item['review_ref']['artifact_id'] in ids:
            raise ValueError('duplicate material confirmation')
        ids.add(item['review_ref']['artifact_id'])
    key = hashlib.sha256(store._canonical({'scope': scope, 'actor': ctx.requester_user_id,
                                          'command_id': command_id})).hexdigest()
    directory = store.root / '_material_review_batches'
    if directory.is_symlink():
        raise ValueError('material review index contains a symlink')
    receipt_path = directory / (key + '.json')
    identity = {'scope': scope, 'actor': ctx.requester_user_id, 'items': deepcopy(items)}
    with store._locked() as artifact_guard:
        guard = _MaterialPublicationGuard(artifact_guard, _canvas_guard) if _canvas_guard else artifact_guard
        guard.reassert()
        locked = _LockedEvidenceStore(store, guard)
        receipt = store._read_json(receipt_path) if receipt_path.is_file() else None
        if receipt and receipt.get('identity') != identity:
            raise ValueError('batch command reused with different inputs')
        # Validate every original identity and its current target before any
        # confirmation. Partial retries accept only the native original +1
        # user receipt, never a changed review or another actor's decision.
        reviews = []
        for item in items:
            ref = item['review_ref']
            original = store._read(ref['artifact_id'], ref['revision'])
            current = store._read(ref['artifact_id'], store._latest_revision(ref['artifact_id']))
            payload = original['payload']
            work = store._read(item['work_ref']['artifact_id'], store._latest_revision(item['work_ref']['artifact_id']))
            if (original['schema'] != 'material_review.v1' or original['created_by'] != ctx.requester_user_id
                    or work['schema'] != 'material_work_package.v1'
                    or work['revision'] != item['work_ref']['revision']
                    or work['created_by'] != ctx.requester_user_id
                    or payload['work_ref']['artifact_id'] != work['id']
                    or payload['step_id'] != item['step_id']
                    or payload['resource']['node_id'] != item['node_id']):
                raise ValueError('material review or work target changed')
            validate_material_review_lineage(locked, payload, original['upstream_refs'])
            plan = store._read(work['payload']['plan_ref']['artifact_id'],
                               store._latest_revision(work['payload']['plan_ref']['artifact_id']))
            node = next((n for n in canvas.get('nodes', []) if n.get('type') == 'scriptNode'
                         and (n.get('data', {}).get('productionPlanV2Ref') or {}).get('artifactId') == plan['id']), None)
            if (not node or node['data']['productionPlanV2Ref']['revision'] != plan['revision']
                    or node['data'].get('productionPlanV2Dirty') or node['data'].get('productionPlanV2Conflict')
                    or _fingerprint(locked, plan['payload'], work['payload']['requirements']) != work['payload']['input_fingerprint']):
                raise ValueError('current material plan changed')
            step = next((s for s in work['payload']['steps'] if s['id'] == item['step_id']), None)
            if not step or step.get('node_id') != item['node_id'] or step['status'] in {'stale', 'rejected'}:
                raise ValueError('current material candidate changed')
            resource = canvas_material_resource(locked, canvas, item['node_id'], payload['resource']['media_kind'])
            if resource != payload['resource']:
                raise ValueError('material changed after review')
            if payload['technical']['status'] != 'passed' or payload['content']['status'] == 'rejected':
                raise ValueError('material requires a current review before confirmation')
            decision = payload.get('user_decision')
            if decision and (decision.get('requester_user_id') != ctx.requester_user_id
                             or decision.get('canvas_id') != canvas_id):
                raise ValueError('material decision belongs to another user or canvas')
            expected = deepcopy(payload)
            expected.update(user_decision={'requester_user_id': ctx.requester_user_id, 'canvas_id': canvas_id,
                'project_id': store.project_id, 'media_hash': resource['media_hash']}, actor_kind='user',
                content={'status': 'passed', 'checks': ['authenticated_user_viewed_current_material'], 'findings': []})
            expected['decision_receipt_id'] = hashlib.sha256(store._canonical(expected['user_decision'])).hexdigest()
            if current['revision'] != original['revision'] and (
                    current['revision'] != original['revision'] + 1 or current['payload'] != expected):
                raise ValueError('material review changed after batch selection')
            if payload['work_ref'] != item['work_ref']:
                _IndependentMaterialReviewContinuity(locked, work, canvas_id).validate_target(current)
            reviews.append(current)
        if receipt and receipt.get('completed'):
            return {'items': [{'review_ref': {'artifact_id': r['id'], 'revision': r['revision']},
                               'review': r['payload']} for r in reviews], 'replayed': True}
        directory.mkdir(exist_ok=True)
        store._atomic_json(receipt_path, {'identity': identity, 'completed': False}, guard)
        confirmed = []
        for review in reviews:
            guard.reassert()
            confirmed.append(confirm_material_review(locked,
                {'artifact_id': review['id'], 'revision': review['revision']}, canvas,
                canvas_id=canvas_id, requester_user_id=ctx.requester_user_id, _guard=guard))
        store._atomic_json(receipt_path, {'identity': identity, 'completed': True}, guard)
        return {'items': confirmed, 'replayed': receipt is not None}
