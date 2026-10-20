"""Receive exact saved revisions for an already bound production plan."""
from __future__ import annotations

from typing import Any

from .reference_commission import advance_reference_commission, read_reference_commission
from .store import RemixArtifactConflict


def reconcile_plan_revision(store, *, commission_ref: dict[str, Any],
                            plan_ref: dict[str, Any]) -> dict[str, Any]:
    """Receive a current plan revision without approving it or choosing a branch.

    The caller verifies the canvas binding. Initial plan binding still belongs
    to the native drafting/receipt path or the scoped review route.
    """
    from .production_advancement import _MutationStore

    ref, exact_ref = store._reference_list([commission_ref, plan_ref], store.project_id)
    # Only artifact reads and plan_ready run in this lease; no canvas lock is
    # acquired. Host callers already holding a mutation lease reuse it.
    with store._locked() as guard:
        locked = store if isinstance(store, _MutationStore) else _MutationStore(store, guard)
        return _receive_revision(locked, ref, exact_ref)


def _receive_revision(store, ref: dict[str, Any], exact_ref: dict[str, Any]) -> dict[str, Any]:
    commission = read_reference_commission(store, ref, latest=True)
    commission.setdefault('review_ref', None)
    if (commission['requester_user_id'] != store.created_by
            or commission['project_id'] != store.project_id):
        raise PermissionError('plan revision actor or project differs')
    if commission['commission_ref'] != ref:
        raise RemixArtifactConflict('Commission changed; reload before receiving plan revision')
    if not commission.get('direction') or not commission.get('direction_revision'):
        raise ValueError('confirmed direction required before receiving plan revision')
    bound = commission.get('plan_ref')
    if not bound:
        raise ValueError('plan revision requires an already bound plan')
    if bound['artifact_id'] != exact_ref['artifact_id']:
        raise ValueError('plan revision must belong to the bound plan artifact')
    plan = store.read_latest(exact_ref['artifact_id'])
    if plan['schema'] != 'production_plan.v2':
        raise ValueError('plan revision requires a saved production_plan.v2')
    if plan['created_by'] != store.created_by:
        raise PermissionError('plan revision belongs to another actor')
    if plan['revision'] != exact_ref['revision']:
        raise RemixArtifactConflict('Plan changed; use its current revision')
    if commission.get('plan_ref') == exact_ref and commission['phase'] != 'drafting_plan':
        return {'changed': False, 'commission': commission}
    updated = advance_reference_commission(
        store, commission['commission_ref'],
        {'type': 'plan_ready', 'plan_ref': exact_ref},
        commission['commission_ref']['revision'],
    )
    updated.setdefault('review_ref', None)
    return {'changed': True, 'commission': updated}


def _bridge_revision_directory(store):
    directory = store.root / '_plan_revision_bridge_receipts'
    if directory.is_symlink():
        raise ValueError('invalid plan revision receipt storage')
    return directory


def _bridge_revision_path(store, record):
    import hashlib
    identity = hashlib.sha256(store._canonical([
        record['bridge_key'], record['plan_ref'], record['commission_ref']['artifact_id']])).hexdigest()
    return _bridge_revision_directory(store) / (identity + '.json')


def _verified_command_receipt(store, command):
    """Read the saved transaction receipt without replaying the edit."""
    import hashlib
    from .production_commands import validate_production_command

    request = validate_production_command(command)
    if request['kind'] == 'batch':
        commands = [validate_production_command({**row, 'command_id': request['command_id'],
                    'plan_ref': request['plan_ref']}) for row in request['payload']['commands']]
        request = {'command_id': request['command_id'], 'plan_ref': request['plan_ref'], 'commands': commands}
    fingerprint = hashlib.sha256(store._canonical(request)).hexdigest()
    directory = store.root / '_production_commands'
    if directory.is_symlink():
        raise ValueError('invalid production command storage')
    path = directory / (hashlib.sha256(request['command_id'].encode()).hexdigest() + '.json')
    if not path.exists():
        return None
    receipt = store._read_json(path)
    if receipt.get('fingerprint') != fingerprint:
        return None
    result = receipt['result']
    ref = {**result['plan_ref'], 'project_id': store.project_id}
    saved = store.read(ref['artifact_id'], ref['revision'])
    if saved['schema'] != 'production_plan.v2' or saved['content_hash'] != receipt['content_hash'] or saved['created_by'] != store.created_by:
        return None
    return ref, fingerprint


def _receive_canvas_revision(ctx, store, record):
    """Canvas lease precedes artifact lease, matching the existing writer order."""
    from novelvideo.freezone import canvas_store
    from novelvideo.chat.reference_production_observer import _canvas_refs, _ref
    from .production_advancement import _MutationStore
    from .reference_director import _MaterialPublicationGuard

    binding, ref = record['binding'], record['plan_ref']
    with canvas_store.get_canvas_write_mutex().write_mutex(ctx.state_dir, binding['canvas_id']) as canvas_guard:
        canvas = canvas_store.read_canvas(ctx.state_dir, binding['canvas_id'])
        if not canvas or canvas.get('project_id') != ctx.project_id:
            return {'status': 'pending_canvas_save', 'plan_ref': ref}
        with store._locked() as guard:
            locked = _MutationStore(store, _MaterialPublicationGuard(guard, canvas_guard))
            commission = read_reference_commission(locked, record['commission_ref'], latest=True)
            if (commission['requester_user_id'] != ctx.requester_user_id
                    or commission['canvas_id'] != binding['canvas_id']
                    or commission.get('direction_revision') != record['direction_revision']
                    or commission.get('direction_fingerprint') != record['direction_fingerprint']):
                return {'status': 'scope_changed', 'plan_ref': ref}
            node = next((node for node in canvas.get('nodes', []) if node.get('id') == record['node_id']), None)
            persisted = ((node or {}).get('data') or {}).get('productionPlanV2Ref') or {}
            if (persisted.get('artifactId'), persisted.get('revision')) != (ref['artifact_id'], ref['revision']):
                return {'status': 'pending_canvas_save', 'plan_ref': ref}
            if not any(_ref(plan) == ref for plan in _canvas_refs(locked, canvas, commission, ctx.state_dir)):
                return {'status': 'scope_changed', 'plan_ref': ref}
            current = locked.read_latest(ref['artifact_id'])
            if current['revision'] != ref['revision']:
                return {'status': 'superseded', 'plan_ref': ref}
            checkpoint = record.get('received_commission')
            if checkpoint:
                try:
                    committed = locked.read(commission['commission_ref']['artifact_id'], checkpoint['revision'])
                except FileNotFoundError:
                    committed = None
                if committed and committed['content_hash'] == checkpoint['content_hash']:
                    return {'status': 'synced', 'plan_ref': ref, 'commission': commission}

            class ReceivingStore(_MutationStore):
                def revise(self, artifact_id, expected_revision, payload, upstream_refs):
                    import hashlib
                    from .contracts import validate_payload
                    payload = validate_payload('reference_commission.v1', payload)
                    proof = {**record, 'received_commission': {
                        'revision': expected_revision + 1,
                        'content_hash': 'sha256:' + hashlib.sha256(self._canonical(payload)).hexdigest(),
                    }}
                    self._atomic_json(_bridge_revision_path(self, record), proof, self._guard)
                    return super().revise(artifact_id, expected_revision, payload, upstream_refs)

            receiving = ReceivingStore(store, locked._guard)
            received = reconcile_plan_revision(receiving, commission_ref=commission['commission_ref'], plan_ref=ref)
            return {'status': 'synced', 'plan_ref': ref, 'commission': received['commission']}


def receive_canvas_plan_revisions(ctx, *, bridge_key, bridge_dir, username, agent_id, turn_id):
    """Receive only actual successful edits from a host-bound durable bridge."""
    from novelvideo.freezone.canvas_command_bridge import read_bridge_message
    from .store import RemixArtifactStore
    from .reference_director import list_director_artifacts

    bridge = read_bridge_message(bridge_key, bridge_dir=bridge_dir)
    binding = {'username': username, 'project_id': ctx.project_id, 'canvas_id': (bridge or {}).get('canvas_id'),
               'agent_id': agent_id, 'turn_id': turn_id}
    result = (bridge or {}).get('result') or {}
    if (not turn_id or not bridge or bridge.get('producer_binding') != binding
            or bridge.get('transport_status') != 'applied'
            or result.get('ok') is not True or result.get('applied') is not True
            or result.get('cancelled') or result.get('errors')
            or result.get('canvas_apply_status') != 'applied'
            or not bridge.get('request_fingerprint')
            or result.get('request_fingerprint') != bridge['request_fingerprint']):
        return []
    store = RemixArtifactStore(ctx.output_dir, ctx.project_id, created_by=ctx.requester_user_id)
    commissions = list_director_artifacts(store, binding['canvas_id'], ctx.requester_user_id, limit=None)['commissions']
    statuses = []
    for index, item in enumerate(bridge.get('commands') or []):
        if not isinstance(item, dict) or item.get('type') != 'production_plan_command':
            continue
        command = item.get('command') or {}
        verified = _verified_command_receipt(store, command)
        if verified is None:
            continue
        ref, fingerprint = verified
        for commission in commissions:
            if (commission.get('plan_ref') or {}).get('artifact_id') != ref['artifact_id']:
                continue
            if not any(row.get('type') == 'production_plan_command' and row.get('status') == 'success'
                       and row.get('commandIndex', index) == index
                       and (row.get('output') or {}).get('command_id') == command['command_id']
                       and (row.get('output') or {}).get('plan_ref') == {k: ref[k] for k in ('artifact_id', 'revision')}
                       for row in result.get('command_results', [])):
                continue
            record = {'binding': binding, 'bridge_key': bridge_key,
                      'request_fingerprint': bridge['request_fingerprint'], 'command_fingerprint': fingerprint,
                      'command': command, 'node_id': item['node_id'], 'plan_ref': ref,
                      'commission_ref': commission['commission_ref'],
                      'direction_revision': commission.get('direction_revision'),
                      'direction_fingerprint': commission.get('direction_fingerprint')}
            # Persist proof before the commission CAS. Recovery only receives this
            # saved revision; it never executes the command or restarts a turn.
            with store._locked() as guard:
                directory = _bridge_revision_directory(store)
                directory.mkdir(exist_ok=True)
                path = _bridge_revision_path(store, record)
                if path.exists():
                    record = store._read_json(path)
                else:
                    store._atomic_json(path, record, guard)
            statuses.append(_receive_canvas_revision(ctx, store, record))
    return statuses


def recover_same_turn_plan_revisions(ctx, store, run):
    """Validate persisted host proofs; busy state alone never suppresses events."""
    from novelvideo.chat.hermes_pool import canvas_bridge_dir_for_profile
    from novelvideo.freezone.canvas_command_bridge import list_scope_bridge_receipts

    turns = {run.get('resume_turn_id'), run.get('runtime_turn_id'),
             (run.get('ordinary_turn_owner') or {}).get('turn_id')} - {None, ''}
    if not turns:
        return []
    scope = {'username': run['username'], 'project_id': ctx.project_id,
             'canvas_id': run['canvas_id'], 'agent_id': run.get('agent_id', 'main')}
    # Existing project bridge directories only; recovery does not provision a
    # worker workspace or replay any tool. This closes receipt-before-index crashes.
    home = ctx.state_dir / 'agents' / 'hermes' / 'freezone'
    for bridge_dir in {canvas_bridge_dir_for_profile(home, 'freezone:' + scope['agent_id']),
                       canvas_bridge_dir_for_profile(home, 'freezone')}:
        if not bridge_dir.exists():
            continue
        for receipt in list_scope_bridge_receipts(binding=scope, bridge_dir=bridge_dir):
            if receipt['turn_id'] in turns and receipt['status'] == 'applied':
                receive_canvas_plan_revisions(ctx, bridge_key=receipt['request_id'], bridge_dir=bridge_dir,
                    username=run['username'], agent_id=scope['agent_id'], turn_id=receipt['turn_id'])
    directory = _bridge_revision_directory(store)
    if not directory.exists():
        return []
    recovered = []
    for path in directory.glob('*.json'):
        record = store._read_json(path)
        binding = record.get('binding') or {}
        if (binding.get('username') != run.get('username')
                or binding.get('project_id') != ctx.project_id
                or binding.get('canvas_id') != run['canvas_id']
                or binding.get('agent_id') != run.get('agent_id', 'main')
                or binding.get('turn_id') not in turns
                or not binding.get('turn_id')
                or record['commission_ref']['artifact_id'] != run['commission_ref']['artifact_id']):
            continue
        verified = _verified_command_receipt(store, record['command'])
        if verified != (record['plan_ref'], record['command_fingerprint']):
            continue
        received = _receive_canvas_revision(ctx, store, record)
        if received['status'] == 'synced':
            recovered.append({**received, 'turn_id': binding['turn_id']})
    return recovered
