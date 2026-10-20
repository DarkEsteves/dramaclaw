"""One authorized, replayable production transition using native services.

The host supplies ProjectContext. Projection/action dictionaries are observations,
never acceptance, task completion, or user authorization evidence.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import re
from pathlib import Path

from novelvideo.project_context import ProjectContext, require_project_home_node
from novelvideo.freezone import canvas_store
from .store import RemixArtifactStore, RemixArtifactConflict
from .production_inventory import _LockedEvidenceStore, read_current_production

_ORDER = {kind: index for index, kind in enumerate((
    'register_material', 'check_materials', 'accept_materials', 'bind_materials',
    'materials_ready', 'speech_aligned', 'compile_production_plan', 'composition_ready', 'render_manifest',
))}
_HALT = {'stopped', 'running', 'recovering', 'failed', 'completed'}
_CONFLICTS = {'plan_edit_conflict', 'selected_work_unavailable', 'review_production_plan'}
_LOCAL_ACTIONS = frozenset({
    'check_materials', 'accept_materials', 'bind_materials',
    'materials_ready', 'speech_aligned',
})


class _ProductionControlGuard:
    """Keep automation authority inside the existing canvas publication lease."""
    def __init__(self, ctx, guard, *, canvas_id, commission_id, agent_id, generation, event=None):
        self.ctx, self.guard, self.event = ctx, guard, event
        self.scope = dict(canvas_id=canvas_id, commission_id=commission_id, agent_id=agent_id, generation=generation)

    def reassert(self):
        from .production_automation import assert_production_automation_current
        self.guard.reassert()
        assert_production_automation_current(self.ctx, **self.scope)
        if self.event is not None:
            from .production_automation import assert_automation_event_current
            identity = self.event['scope']
            if any(identity.get(key) != self.scope[key] for key in ('canvas_id','commission_id','agent_id')):
                raise PermissionError('automation event belongs to another production')
            assert_automation_event_current(self.ctx, self.event)


class _MutationStore(_LockedEvidenceStore):
    def revise(self, *args, **kwargs):
        return type(self._store).revise(self, *args, **kwargs)


def select_next_action(state: dict) -> dict | None:
    """Choose one current action without I/O or calls to media/model providers."""
    if state.get('execution_status') in _HALT:
        return None
    blockers = state.get('blockers', [])
    conflicts = [row for row in blockers if row.get('kind') in _CONFLICTS]
    if conflicts:
        return deepcopy(sorted(conflicts, key=lambda r: (r['kind'], r.get('action_id') or ''))[0])
    ready = [row for row in state.get('ready_system_actions', [])
             if row.get('owner') == 'system' and row.get('kind') in _ORDER]
    if ready:
        return deepcopy(min(ready, key=lambda r: (_ORDER[r['kind']], r.get('action_id') or '')))
    choices = [row for row in blockers if row.get('owner') in {'agent', 'user'}]
    if choices:
        choice = deepcopy(min(choices, key=lambda r: (r.get('owner') != 'user', r['kind'], r.get('action_id') or '')))
        if choice['kind'] == 'work_selection_required':
            choice['kind'] = 'select_material_work'
        return choice
    return deepcopy(state.get('next_action'))


def _exact(ref):
    return {'artifact_id': ref['artifact_id'], 'revision': ref['revision']}


def _action(kind, identity, **fields):
    return {'owner': 'system', 'kind': kind, 'action_id': identity, **fields}


def enrich_production_state(store, state: dict, *, canvas: dict, workflow_state_dir) -> dict:
    """Expose executable system actions after the host's authoritative inventory.

    The caller computes the final projection digest after this enrichment. This
    function only reads; callers must still revalidate under publication guards.
    """
    state = deepcopy(state)
    actions = []
    if state.get('plan_review_valid', True) is False or state['execution_status'] in _HALT:
        state['ready_system_actions'] = actions
        state['next_action'] = select_next_action(state)
        return state
    from .material_work import material_binding_commands, validate_material_completion
    work_ref, plan_ref = state.get('work_ref'), state.get('plan_ref')
    plan = store.read(plan_ref['artifact_id'], plan_ref['revision']) if plan_ref else None
    work = store.read(work_ref['artifact_id'], work_ref['revision']) if work_ref else None
    if plan:
        for node in canvas.get('nodes', []):
            data = node.get('data', {})
            if (data.get('productionPlanV2Ref') or {}).get('artifactId') != plan['id']:
                continue
            draft = data.get('productionPlanV2Draft')
            conflict = data.get('productionPlanV2Dirty') or data.get('productionPlanV2Conflict')
            if draft is not None:
                from .contracts import validate_payload
                try:
                    conflict = conflict or validate_payload('production_plan.v2', draft) != plan['payload']
                except ValueError:
                    conflict = True
            if conflict:
                state['blockers'].append({'owner': 'user', 'kind': 'plan_edit_conflict', 'action_id': node['id']})
                state['ready_system_actions'] = []
                state['next_action'] = select_next_action(state)
                return state
    for blocker in state.get('blockers', []):
        if blocker['kind'] == 'accept_materials':
            actions.append(_action('accept_materials', blocker['action_id']))
        elif blocker['kind'] == 'check_materials':
            actions.append(_action('check_materials', blocker['action_id']))
    if plan and work and state['progress']['final_materials_total'] == state['progress']['final_materials_accepted']:
        try:
            commands = material_binding_commands(store, work, plan['payload'])
            if commands:
                actions.append(_action('bind_materials', 'bindings:' + work['id']))
            elif state['phase'] == 'preparing_materials':
                validate_material_completion(store, work, plan['payload'])
                actions.append(_action('materials_ready', 'materials:' + work['id']))
        except (ValueError, FileNotFoundError) as exc:
            state['blockers'].append({'owner': 'agent', 'kind': 'resolve_material_bindings',
                                      'action_id': 'bindings:' + work['id']})
            state['advancement_blocker'] = str(exc)
    if plan and state['phase'] == 'aligning_speech':
        turns = [turn for beat in plan['payload']['beats'] for turn in beat.get('turns', []) if turn.get('text')]
        cues = plan['payload'].get('speech_cues', [])
        if all(any(c['turn_id'] == t['id'] and c['timing_status'] == 'ready' for c in cues) for t in turns):
            actions.append(_action('speech_aligned', 'speech:' + plan['id']))
        elif not any(b['kind'] == 'review_speech_correction' for b in state['blockers']):
            state['blockers'].append({'owner': 'agent', 'kind': 'align_adopted_speech', 'action_id': 'speech:' + plan['id']})
    if plan and state['phase'] == 'composing':
        from .production_plan_v2 import project_production_plan_v2
        projection = project_production_plan_v2(plan['payload'], project_dir=store.project_dir)
        if projection['ready']:
            actions.append(_action('compile_production_plan', 'compile:' + plan['id']))
        else:
            state['blockers'].append({'owner': 'agent', 'kind': 'arrange_production', 'action_id': 'compose:' + plan['id']})
            state['advancement_blocker'] = projection['issues']
    if plan and state['phase'] == 'reviewing_output':
        commission = state['director_state']['commissions'][0]
        if commission['production_scope'] in {'预览成片', '导出成片'} and commission.get('manifest_ref'):
            manifest_ref = _exact(commission['manifest_ref'])
            try:
                from novelvideo.api.routes.remix import _validate_v2_canvas_bindings
                manifest = store.read(manifest_ref['artifact_id'], manifest_ref['revision'])
                compose = _compose(canvas, state['plan_ref'])
                _validate_v2_canvas_bindings(canvas, manifest['payload'], manifest_ref, plan['payload'], compose['id'], store.project_id)
                actions.append(_action('render_manifest', 'render:' + manifest_ref['artifact_id']))
            except (ValueError, FileNotFoundError):
                state['blockers'].append({'owner': 'agent', 'kind': 'prepare_composition_binding', 'action_id': 'compose:' + plan['id']})
    actions = _operation_actions(store, state, plan, canvas, actions)
    state['ready_system_actions'] = actions
    state['next_action'] = select_next_action(state)
    return state


def _read(ctx, store, canvas, canvas_id, commission_id, agent_id, runs):
    state = read_current_production(store, canvas=canvas, canvas_id=canvas_id, agent_id=agent_id,
        requester_user_id=ctx.requester_user_id, runs=runs, state_dir=ctx.state_dir, commission_id=commission_id)
    # Inventory integration calls enrichment before digesting. Preserve old
    # inventory's digest during rollout so its GET remains a valid CAS basis.
    if 'ready_system_actions' not in state:
        state = enrich_production_state(store, state, canvas=canvas, workflow_state_dir=ctx.state_dir)
    return state


def _receipt_path(store, canvas_id, agent_id, command_id):
    identity = [store.project_id, store.created_by, canvas_id, agent_id, command_id]
    key = hashlib.sha256(store._canonical(identity)).hexdigest()
    directory = store.root / '_production_advancement'
    if directory.is_symlink():
        raise ValueError('invalid advancement storage')
    directory.mkdir(exist_ok=True)
    return directory / (key + '.json')


def _canvas_publish(ctx, canvas_id, canvas, updated, guard):
    """Use canvas storage publication primitives while retaining its host mutex."""
    path = canvas_store.canvas_path(ctx.state_dir, canvas_id)
    current = canvas_store.read_canvas(ctx.state_dir, canvas_id)
    if current != canvas:
        raise RemixArtifactConflict('canvas changed before production publication')
    updated = deepcopy(updated)
    updated['revision'] = canvas.get('revision', 0) + 1
    updated['updated_at'] = canvas_store.utc_now_iso()
    canvas_store.check_dangerous_empty_overwrite(existing=canvas, payload=updated,
        save_source='production_advancement', allow_empty_overwrite=False)
    canvas_store.backup_canvas_snapshot(path, canvas)
    canvas_store.atomic_write_json(path, updated, fence=guard.reassert)
    canvas_store.prune_canvas_history(ctx.state_dir, canvas_id)
    return updated


def _validate_plan_canvas(store, canvas, original_ref, result_ref=None):
    """Allow only the reserved input or its certified result and matching drafts."""
    from .contracts import validate_payload
    refs = [_exact(original_ref), *([_exact(result_ref)] if result_ref else [])]
    allowed = [{'artifactId': ref['artifact_id'], 'revision': ref['revision']} for ref in refs]
    nodes = [n for n in canvas['nodes'] if n.get('type') == 'scriptNode' and
             (n.get('data', {}).get('productionPlanV2Ref') or {}).get('artifactId') == original_ref['artifact_id']]
    if len(nodes) != 1:
        raise RemixArtifactConflict('current production plan node is ambiguous')
    plan_node = nodes[0]
    old_ref = plan_node['data']['productionPlanV2Ref']
    if old_ref not in allowed:
        raise RemixArtifactConflict('canvas plan changed beyond the reserved publication')
    targets = [n for n in canvas['nodes'] if n.get('type') == 'videoComposeNode' and
               n.get('data', {}).get('productionPlanNodeId') == plan_node['id']]
    for node in [plan_node, *targets]:
        data = node['data']
        if data.get('productionPlanV2Dirty') or data.get('productionPlanV2Conflict'):
            raise RemixArtifactConflict('plan or compose draft changed; preserve current user edits')
        ref = data.get('productionPlanV2Ref')
        if ref is None:
            ref = old_ref
        if ref != old_ref:
            raise RemixArtifactConflict('compose plan changed; preserve current user edits')
        if data.get('productionPlanV2Draft') is not None:
            referenced = store.read(ref['artifactId'], ref['revision'])
            try:
                matches = validate_payload('production_plan.v2', data['productionPlanV2Draft']) == referenced['payload']
            except ValueError:
                matches = False
            if not matches:
                raise RemixArtifactConflict('plan or compose serialized draft changed; preserve current user edits')
    return plan_node


def _plan_canvas(ctx, store, canvas_id, canvas, plan_ref, resources, guard, *, original_plan_ref):
    _validate_plan_canvas(store, canvas, original_plan_ref, plan_ref)
    updated = deepcopy(canvas)
    plan_node = next(n for n in updated['nodes'] if n.get('type') == 'scriptNode' and
                     (n.get('data', {}).get('productionPlanV2Ref') or {}).get('artifactId') == plan_ref['artifact_id'])
    payload = store.read(plan_ref['artifact_id'], plan_ref['revision'])['payload']
    for node in updated['nodes']:
        if node is plan_node or (node.get('type') == 'videoComposeNode' and
                                node.get('data', {}).get('productionPlanNodeId') == plan_node['id']):
            if node['data'].get('productionPlanV2Draft') is not None:
                node['data']['productionPlanV2Draft'] = deepcopy(payload)
            node['data']['productionPlanV2Ref'] = {'artifactId': plan_ref['artifact_id'], 'revision': plan_ref['revision']}
            if node is not plan_node:
                node['data'].pop('remixManifestRef', None)
    for resource in resources:
        node = next(n for n in updated['nodes'] if n['id'] == resource['node_id'])
        node.setdefault('data', {})['remixMedia'] = deepcopy(resource)
        if not any(e.get('source') == node['id'] and e.get('target') == plan_node['id'] for e in updated['edges']):
            updated['edges'].append({'id': 'material:' + node['id'] + ':' + plan_node['id'],
                                    'source': node['id'], 'target': plan_node['id']})
    if updated == canvas:
        return canvas
    return _canvas_publish(ctx, canvas_id, canvas, updated, guard)


def _prepare_native_acceptance(ctx, store, canvas_id, commission_ref, agent_id, command_id, expected_projection_revision, *, enrolling=False):
    """Full acceptance lineage is prepared outside canvas/artifact commit locks."""
    from novelvideo.chat.reference_remix_continuation import coordinator
    from .material_execution import read_material_state, canvas_material_resource
    from .material_work import prepare_material_acceptance
    canvas = canvas_store.read_canvas(ctx.state_dir, canvas_id)
    if not canvas:
        return None
    path = _receipt_path(store, canvas_id, agent_id, command_id)
    receipt = store._read_json(path) if path.is_file() else None
    if receipt:
        request = {'commission_ref': commission_ref, 'projection_revision': expected_projection_revision}
        if receipt.get('request') != request or receipt.get('completed'):
            return None
        state, action = receipt['state'], receipt['action']
    else:
        runs = coordinator.production_for_actor(ctx, canvas_id, agent_id)
        state = _read(ctx, store, canvas, canvas_id, commission_ref['artifact_id'], agent_id, runs)
        if state['projection_revision'] != expected_projection_revision or _exact(state['commission_ref']) != _exact(commission_ref):
            return None
        if enrolling and state.get('execution_status') == 'stopped':
            # Preview only: publication still compares the real stopped CAS,
            # enrolls host intent, and rereads actual restored recovery rows.
            preview_runs = [{**run, 'status': state['phase']} if run.get('status') == 'stopped'
                and (run.get('commission_ref') or {}).get('artifact_id') == commission_ref['artifact_id'] else run for run in runs]
            state = _read(ctx, store, canvas, canvas_id, commission_ref['artifact_id'], agent_id, preview_runs)
        action = select_next_action(state)
    if not action or action.get('kind') != 'accept_materials' or action.get('owner') != 'system':
        return None
    work_ref = _exact(state['work_ref'])
    step_id = action['action_id'].removeprefix('step:')
    work = store.read(work_ref['artifact_id'], work_ref['revision'])
    step = next(s for s in work['payload']['steps'] if s['id'] == step_id)
    native_receipt = next((r for r in store.read_latest(work_ref['artifact_id'])['payload'].get('command_receipts', []) if r['command_id'] == command_id), None)
    if native_receipt:
        resource, review_ref = native_receipt['request']['resource'], native_receipt['request']['review_ref']
    else:
        candidates = read_material_state(store, work_ref, canvas=canvas, canvas_id=canvas_id, include_workflow_intent=False)['material_reviews']
        passed = [r for r in candidates if r['step_id'] == step_id and r['review_compatible_with_current_work']
                  and r['technical']['status'] == r['content']['status'] == 'passed']
        if not passed:
            raise RemixArtifactConflict('no current passed material review')
        if len({store._canonical(r['resource']) for r in passed}) != 1:
            raise RemixArtifactConflict('multiple passed candidates require a creative choice')
        review = sorted(passed, key=lambda r: (r['review_ref']['artifact_id'], r['review_ref']['revision']))[0]
        resource, review_ref = review['resource'], _exact(review['review_ref'])
    if canvas_material_resource(store, canvas, resource['node_id'], step['media_kind']) != resource:
        raise RemixArtifactConflict('current resource differs from the passed review')
    prepared = prepare_material_acceptance(store, work_ref, step_id, resource, review_ref, command_id, canvas_id=canvas_id)
    return {'action': action, 'work_ref': work_ref, 'step_id': step_id, 'resource': resource,
            'review_ref': review_ref, 'canvas_hash': hashlib.sha256(store._canonical(canvas)).hexdigest(), 'prepared': prepared}


def _execute_local(ctx, store, canvas, canvas_id, state, action, command_id, canvas_guard, prepared_acceptance=None):
    from .material_execution import canvas_material_resource, review_material_candidate, validate_material_native_execution
    from .material_work import accept_material_candidate, material_binding_commands
    from .reference_commission import advance_reference_commission
    kind = action['kind']
    work_ref, plan_ref = _exact(state['work_ref']) if state.get('work_ref') else None, _exact(state['plan_ref']) if state.get('plan_ref') else None
    if kind in {'accept_materials', 'check_materials'}:
        step_id = action['action_id'].removeprefix('step:')
        work = store.read(work_ref['artifact_id'], work_ref['revision'])
        step = next(s for s in work['payload']['steps'] if s['id'] == step_id)
        if kind == 'check_materials':
            return review_material_candidate(store, work_ref, step_id, canvas, step['node_id'],
                workflow_state_dir=ctx.state_dir, command_id=command_id), canvas
        packet = prepared_acceptance
        if (not packet or packet['action'] != action or packet['work_ref'] != work_ref or packet['step_id'] != step_id
                or hashlib.sha256(store._canonical(canvas)).hexdigest() != packet['canvas_hash']):
            raise RemixArtifactConflict('material acceptance snapshot changed; reload current state')
        if step['source'] == 'generate':
            validate_material_native_execution(store, canvas, step, packet['resource']['node_id'], require_complete=True,
                workflow_state_dir=ctx.state_dir, work_ref=work_ref)
        result = accept_material_candidate(store, work_ref, step_id, packet['resource'], packet['review_ref'], command_id,
            canvas_id=canvas_id, _canvas_guard=canvas_guard, _prepared=packet['prepared'])
        return result, canvas
    if kind == 'bind_materials':
        from .material_work import bind_accepted_materials
        work = store.read(work_ref['artifact_id'], work_ref['revision'])
        plan = store.read(plan_ref['artifact_id'], plan_ref['revision'])
        material_binding_commands(store, work, plan['payload'])
        # Validate all current nodes before any plan transaction publication.
        resources = [s['accepted_resource'] for s in work['payload']['steps'] if s['status'] == 'accepted']
        for resource in resources:
            if canvas_material_resource(store, canvas, resource['node_id'], resource['media_kind']) != resource:
                raise RemixArtifactConflict('accepted current media changed before binding')
        # Refuse linked editor conflicts before the native plan transaction.
        _validate_plan_canvas(store, canvas, plan_ref,
                              {'artifact_id': plan['id'], 'revision': store.read_latest(plan['id'])['revision']})
        result = bind_accepted_materials(store, work_ref, plan_ref, command_id)
        canvas = _plan_canvas(ctx, store, canvas_id, canvas, result['plan_ref'], resources, canvas_guard, original_plan_ref=plan_ref)
        # Binding refines media, retains approval; material-ready validates every
        # final before later changing phases. Keep the commission's exact plan head.
        c = store.read(state['commission_ref']['artifact_id'], state['commission_ref']['revision'])
        payload = deepcopy(c['payload'])
        payload['plan_ref'] = {**result['plan_ref'], 'project_id': ctx.project_id}
        payload['work_ref'] = {**result['work_ref'], 'project_id': ctx.project_id}
        refs = store._reference_list([*c['upstream_refs'], result['plan_ref'], result['work_ref']], ctx.project_id)
        head = store.read_latest(c['id'])
        if head['revision'] == c['revision'] + 1 and head['payload'] == payload and head['upstream_refs'] == refs:
            saved = head
        else:
            saved = store.revise(c['id'], c['revision'], payload, refs)
        return {**result, 'commission_ref': {'artifact_id': saved['id'], 'revision': saved['revision']}}, canvas
    if kind in {'materials_ready', 'speech_aligned'}:
        ref = state['commission_ref']
        event = {'type': kind, **({'work_ref': work_ref} if kind == 'materials_ready' else {'plan_ref': state['plan_ref']})}
        return advance_reference_commission(store, ref, event, ref['revision']), canvas
    raise ValueError('unsupported deterministic local production action')


async def advance_production(ctx, *, canvas_id: str, commission_ref: dict,
                             expected_projection_revision: str, command_id: str,
                             agent_id: str = 'main', automation_generation: int | None = None, automation_enrollment_command: str | None = None, automation_event: dict | None = None) -> dict:
    """CAS the host inventory and complete at most one native system action."""
    if not isinstance(ctx, ProjectContext):
        raise PermissionError('authenticated ProjectContext required')
    require_project_home_node(ctx, operation='production advancement')
    if ctx.effective_role not in {'owner', 'admin', 'editor'}:
        raise PermissionError('production advancement requires editor role')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', command_id):
        raise ValueError('invalid production advancement command_id')
    if commission_ref.get('project_id', ctx.project_id) != ctx.project_id:
        raise PermissionError('commission project differs from host context')
    from novelvideo.chat.reference_remix_continuation import coordinator
    store = RemixArtifactStore(Path(ctx.output_dir), ctx.project_id, created_by=ctx.requester_user_id)
    prepared_acceptance = await asyncio.to_thread(_prepare_native_acceptance, ctx, store, canvas_id,
        commission_ref, agent_id, command_id, expected_projection_revision, enrolling=automation_enrollment_command is not None)

    def execute():
        with canvas_store.get_canvas_write_mutex().write_mutex(ctx.state_dir, canvas_id) as lease:
            canvas_guard = _ProductionControlGuard(ctx, lease, canvas_id=canvas_id,
                commission_id=commission_ref['artifact_id'], agent_id=agent_id, generation=automation_generation, event=automation_event)
            if automation_enrollment_command is None:
                canvas_guard.reassert()
            else:
                lease.reassert()  # Only read the old CAS before explicit host enrollment.
            canvas = canvas_store.read_canvas(ctx.state_dir, canvas_id)
            if not canvas:
                raise FileNotFoundError('canvas not found')
            runs = coordinator.production_for_actor(ctx, canvas_id, agent_id)
            # Native services may acquire artifact mutexes. The adapter reuses
            # this held lease explicitly, avoiding portalocker recursion.
            with store._locked() as artifact_guard:
                from .reference_director import _MaterialPublicationGuard
                locked = _MutationStore(store, _MaterialPublicationGuard(artifact_guard, lease if automation_enrollment_command else canvas_guard))
                path = _receipt_path(locked, canvas_id, agent_id, command_id)
                request = {'commission_ref': commission_ref, 'projection_revision': expected_projection_revision}
                receipt = locked._read_json(path) if path.exists() else None
                if receipt and receipt['request'] != request:
                    raise RemixArtifactConflict('advancement command_id reused with different inputs')
                if receipt and not receipt.get('completed'):
                    canvas_guard.reassert()
                    if receipt.get('action', {}).get('kind') == 'await_completion_event':
                        state = _read(ctx, locked, canvas, canvas_id,
                                      commission_ref['artifact_id'], agent_id, runs)
                        return {**state, 'advancement': {'action': None, 'replayed': True}}
                    if receipt.get('action', {}).get('kind') not in _LOCAL_ACTIONS:
                        raise RemixArtifactConflict('unsupported local advancement receipt')
                    locked._guard = _MaterialPublicationGuard(artifact_guard, canvas_guard)
                    return _resume_local(ctx, locked, canvas_id, canvas, runs, agent_id, receipt, path, command_id, canvas_guard, artifact_guard, prepared_acceptance)
                state = _read(ctx, locked, canvas, canvas_id, commission_ref['artifact_id'], agent_id, runs)
                if receipt and receipt.get('completed'):
                    canvas_guard.reassert()
                    return {**state, 'advancement': {'action': receipt['action'], 'replayed': True, 'result': receipt['result']}}
                if state['projection_revision'] != expected_projection_revision or _exact(state['commission_ref']) != _exact(commission_ref):
                    raise RemixArtifactConflict('production projection changed; reload current state')
                if automation_enrollment_command is not None:
                    if automation_enrollment_command != command_id:
                        raise ValueError('native enrollment must use the explicit request command identity')
                    from .production_automation import enroll_production_automation
                    from .reference_commission import _result
                    commission = _result(locked.read_latest(commission_ref['artifact_id']))
                    control = enroll_production_automation(ctx, canvas_id=canvas_id, commission_ref=state['commission_ref'],
                        command_id=automation_enrollment_command, agent_id=agent_id, canvas_guard=lease, _saved_commission=commission)
                    canvas_guard.scope['generation'] = control['generation']
                    canvas_guard.reassert()
                    # The explicit human request reopens only this current saved
                    # production. Analysis task identity and other branches stay intact.
                    for run in runs:
                        if (run.get('commission_ref') or {}).get('artifact_id') == commission_ref['artifact_id'] and run.get('status') == 'stopped':
                            coordinator.store.set_production_control_status(ctx, run['run_id'], stopped=False, commission=commission)
                    runs = coordinator.production_for_actor(ctx, canvas_id, agent_id)
                    locked._guard = _MaterialPublicationGuard(artifact_guard, canvas_guard)
                    state = _read(ctx, locked, canvas, canvas_id, commission_ref['artifact_id'], agent_id, runs)
                action = select_next_action(state)
                if not action or action['owner'] != 'system':
                    return {**state, 'advancement': {'action': None, 'replayed': False}}
                if action['kind'] == 'await_completion_event':
                    return {**state, 'advancement': {'action': None, 'replayed': False}}
                if action['kind'] in {'compile_production_plan', 'composition_ready', 'render_manifest'}:
                    return {**state, '_submit': {'action': action, 'request': request, 'command_id': command_id,
                        'admission_projection_revision': state['projection_revision'], 'automation_generation': canvas_guard.scope['generation']}}
                if action['kind'] not in _LOCAL_ACTIONS:
                    raise RemixArtifactConflict('unsupported local production action')
                locked._atomic_json(path, {'request': request, 'action': action, 'state': state, 'completed': False}, locked._guard)
                result, canvas = _execute_local(ctx, locked, canvas, canvas_id, state, action, command_id, canvas_guard, prepared_acceptance)
                receipt = {'request': request, 'action': action, 'result': result, 'completed': True}
                locked._atomic_json(path, receipt, locked._guard)
                updated = _read(ctx, locked, canvas, canvas_id, commission_ref['artifact_id'], agent_id, runs)
                return {**updated, 'advancement': {'action': action, 'result': result, 'replayed': False}}
    result = await asyncio.to_thread(execute)
    if automation_enrollment_command is not None:
        from novelvideo.chat.reference_production_observer import wake_local_observers
        wake_local_observers()
    if '_submit' in result:
        automation_generation = result['_submit'].get('automation_generation', automation_generation)
        if result['_submit']['action']['kind'] == 'composition_ready':
            return await _settle_compile(ctx, store, canvas_id, commission_ref, agent_id, result, automation_generation=automation_generation, automation_event=automation_event)
        return await _submit_compile(ctx, store, canvas_id, commission_ref, agent_id, result, automation_generation=automation_generation, automation_event=automation_event)
    return result


def _operation_actions(store, state, plan, canvas, actions):
    from .operations import RemixOperationStore
    if not plan or state['phase'] not in {'composing', 'reviewing_output'}:
        return actions
    scope = {'canvas_id': state['scope']['canvas_id'], 'agent_id': state['scope']['agent_id'],
             'requester_user_id': store.created_by, 'commission_id': state['commission_ref']['artifact_id']}
    wanted = 'compile_production_plan' if state['phase'] == 'composing' else 'render_manifest'
    rows = [r for r in RemixOperationStore(store.project_dir, store.project_id).list(limit=100)
            if r.get('normalized_parameters', {}).get('production_scope') == scope
            and r['normalized_parameters'].get('action') == wanted]
    expected = [_exact(state['plan_ref'])] if wanted == 'compile_production_plan' else [_exact(state['director_state']['commissions'][0]['manifest_ref'])]
    rows = [r for r in rows if expected is None or [_exact(ref) for ref in r['input_refs']] == expected]
    if not rows:
        return actions
    row = rows[0]
    state['production_operation'] = {key: row.get(key) for key in ('operation_id', 'task_id', 'status', 'output_refs')}
    actions = [a for a in actions if a['kind'] != wanted]
    if row['status'] == 'pending' and row.get('task_id'):
        state['execution_status'] = 'running'
    elif row['status'] == 'needs_confirmation' and wanted == 'compile_production_plan':
        refs = row.get('output_refs', [])
        if len(refs) == 1 and refs[0].get('artifact_id'):
            saved = store.read(refs[0]['artifact_id'], refs[0]['revision'])
            if saved['schema'] == 'composition_manifest.v2' and saved['payload']['source_versions'] == [_exact(state['plan_ref'])]:
                actions.append(_action('composition_ready', 'composition:' + row['operation_id'],
                    operation_id=row['operation_id'], manifest_ref=_exact(refs[0])))
        if not actions:
            state['blockers'].append({'owner': 'agent', 'kind': 'recover_compilation_result', 'action_id': row['operation_id']})
    elif row['status'] == 'needs_confirmation':
        state['execution_status'] = 'waiting_user'
        state['blockers'].append({'owner': 'user', 'kind': 'confirm_operation', 'action_id': row['operation_id']})
    elif row['status'] == 'pending' and not row.get('task_id'):
        actions.append(_action(wanted, wanted + ':' + plan['id']))
    elif row['status'] == 'completed' and wanted == 'compile_production_plan':
        refs = row.get('output_refs', [])
        if len(refs) == 1 and refs[0].get('artifact_id'):
            saved = store.read(refs[0]['artifact_id'], refs[0]['revision'])
            if (saved['schema'] == 'composition_manifest.v2' and
                    saved['payload']['source_versions'] == [_exact(state['plan_ref'])]):
                actions.append(_action('composition_ready', 'composition:' + row['operation_id'],
                    operation_id=row['operation_id'], manifest_ref=_exact(refs[0])))
                return actions
        state['blockers'].append({'owner': 'agent', 'kind': 'recover_compilation_result', 'action_id': row['operation_id']})
    elif row['status'] == 'completed' and wanted == 'render_manifest':
        compose = _compose(canvas, state['plan_ref'])
        data = compose['data']
        manifest_ref = state['director_state']['commissions'][0]['manifest_ref']
        if (data.get('remixRenderOperationId') == row['operation_id']
                and data.get('remixRenderManifestRef') == {'artifactId': manifest_ref['artifact_id'], 'revision': manifest_ref['revision']}
                and data.get('productionPlanRenderedRef') == {'artifactId': state['plan_ref']['artifact_id'], 'revision': state['plan_ref']['revision']}):
            state['blockers'].append({'owner': 'agent', 'kind': 'review_output', 'action_id': row['operation_id']})
        else:
            state['blockers'].append({'owner': 'agent', 'kind': 'recover_render_result', 'action_id': row['operation_id']})
    else:
        state['blockers'].append({'owner': 'user', 'kind': 'resolve_operation', 'action_id': row['operation_id']})
    return actions


async def _submit_compile(ctx, store, canvas_id, commission_ref, agent_id, state, *, automation_generation=None, automation_event=None):
    """Concrete admission through the registered task and durable operation lease."""
    import uuid
    from novelvideo.ports import get_task_backend
    from .operations import RemixOperationStore
    from novelvideo.api.routes.remix import prepare_registered_action, record_enqueue_exception
    from novelvideo.chat.reference_remix_continuation import coordinator
    submission = state.pop('_submit')
    action = submission['action']
    plan_ref = _exact(state['plan_ref'])
    manifest_ref = _exact(state['director_state']['commissions'][0]['manifest_ref']) if action['kind'] == 'render_manifest' else None
    identity = [ctx.project_id, ctx.requester_user_id, canvas_id, agent_id, commission_ref['artifact_id'], plan_ref, manifest_ref, action['kind']]
    key = 'advance-' + hashlib.sha256(store._canonical(identity)).hexdigest()
    mutex = canvas_store.get_canvas_write_mutex().write_mutex(ctx.state_dir, canvas_id)
    lease = await asyncio.to_thread(mutex.__enter__)
    guard = _ProductionControlGuard(ctx, lease, canvas_id=canvas_id, commission_id=commission_ref['artifact_id'],
        agent_id=agent_id, generation=automation_generation, event=automation_event)
    try:
        guard.reassert()
        canvas = await asyncio.to_thread(canvas_store.read_canvas, ctx.state_dir, canvas_id)
        runs = await asyncio.to_thread(coordinator.production_for_actor, ctx, canvas_id, agent_id)
        current = await asyncio.to_thread(_read, ctx, store, canvas, canvas_id, commission_ref['artifact_id'], agent_id, runs)
        if current['projection_revision'] != submission.get('admission_projection_revision', submission['request']['projection_revision']):
            raise RemixArtifactConflict('production projection changed before native task admission')
        compose = _compose(canvas, state['plan_ref'])
        node_revision = max(1, int(compose['data'].get('remixNodeRevision', 0)) + 1)
        if action['kind'] == 'render_manifest':
            node_revision = compose['data']['remixNodeRevision']
        existing = _current_native_operation(store, state, action['kind'])
        if existing is not None:
            node_revision = existing['node_revision']
        prepared = await asyncio.to_thread(prepare_registered_action, store, 'compile_production_plan',
            {'plan_ref': plan_ref}, idempotency_key=key, node_revision=node_revision)
        if action['kind'] == 'render_manifest':
            from novelvideo.api.routes.remix import _validate_v2_canvas_bindings, _input_media_hashes
            from .render import validate_manifest
            manifest = await asyncio.to_thread(store.read, manifest_ref['artifact_id'], manifest_ref['revision'])
            validate_manifest(manifest['payload'])
            source = await asyncio.to_thread(store.read, plan_ref['artifact_id'], plan_ref['revision'])
            _validate_v2_canvas_bindings(canvas, manifest['payload'], manifest_ref, source['payload'], compose['id'], ctx.project_id)
            fingerprint = compose['data'].get('remixInputFingerprint')
            if not fingerprint or compose['data'].get('remixInputsCurrent') is not True:
                raise RemixArtifactConflict('bound render requires current compose input fingerprint')
            job_id = hashlib.sha256(f'{ctx.project_id}\0{manifest_ref["artifact_id"]}\0{manifest_ref["revision"]}\0{key}'.encode()).hexdigest()
            prepared = {'input_refs': [manifest_ref], 'producer_version': 'remix-render.v1',
                'input_media_hashes': _input_media_hashes(manifest['payload']), 'scope': job_id,
                'task_type': 'remix_render', 'queue_kind': 'ffmpeg',
                'normalized_parameters': {'action': 'render_manifest', 'payload': {'manifest_ref': manifest_ref},
                    'job_id': job_id, 'input_fingerprint': fingerprint, 'manifest_media_bindings': manifest['payload']['media_bindings']},
                'task_payload': {'job_id': job_id, 'project_dir': str(store.project_dir), 'manifest_ref': manifest_ref,
                    'idempotency_key': key, 'node_revision': node_revision}}
        prepared['normalized_parameters']['production_scope'] = {
            'canvas_id': canvas_id, 'agent_id': agent_id, 'requester_user_id': ctx.requester_user_id,
            'commission_id': commission_ref['artifact_id']}
        operations = RemixOperationStore(store.project_dir, ctx.project_id)
        operation = await asyncio.to_thread(operations.start, idempotency_key=key,
            input_refs=prepared['input_refs'], producer_version=prepared['producer_version'],
            normalized_parameters=prepared['normalized_parameters'], node_revision=node_revision,
            canvas_id=canvas_id, node_id=compose['id'], input_media_hashes=prepared['input_media_hashes'])
        updated_canvas = deepcopy(canvas)
        target = next(n for n in updated_canvas['nodes'] if n['id'] == compose['id'])
        target['data'].update(remixNodeRevision=node_revision, remixOperationId=operation['operation_id'],
            productionPlanV2Ref={'artifactId': plan_ref['artifact_id'], 'revision': plan_ref['revision']})
        if updated_canvas != canvas:
            canvas = await asyncio.to_thread(_canvas_publish, ctx, canvas_id, canvas, updated_canvas, guard)
        if not operation['task_id'] and operation['status'] == 'pending':
            owner = uuid.uuid4().hex
            fence = await asyncio.to_thread(operations.claim_submission, operation['operation_id'], owner=owner)
            if fence is not None:
                try:
                    guard.reassert()
                    queued = await get_task_backend().enqueue_project_task(ctx, product_surface='freezone',
                        task_type=prepared['task_type'], queue_kind=prepared['queue_kind'], episode=0,
                        scope=prepared.get('scope') or operation['operation_id'], payload={**prepared['task_payload'], 'operation_id': operation['operation_id']})
                except Exception as exc:
                    await record_enqueue_exception(operations, operation['operation_id'], owner=owner,
                        fencing_token=fence, error=exc)
                    raise
                operation = await asyncio.to_thread(operations.bind_task, operation['operation_id'],
                    task_id=queued.task_state.task_id, owner=owner, fencing_token=fence)
        def finish():
            with store._locked() as artifact_guard:
                path = _receipt_path(store, canvas_id, agent_id, submission['command_id'])
                receipt = {'request': submission['request'], 'action': action,
                           'result': {'operation_id': operation['operation_id'], 'task_id': operation['task_id']}, 'completed': True}
                from .reference_director import _MaterialPublicationGuard
                store._atomic_json(path, receipt, _MaterialPublicationGuard(artifact_guard, guard))
            updated = _read(ctx, store, canvas, canvas_id, commission_ref['artifact_id'], agent_id, runs)
            return {**updated, 'advancement': {'action': action, 'result': receipt['result'], 'replayed': False}}
        return await asyncio.to_thread(finish)
    finally:
        await asyncio.to_thread(mutex.__exit__, None, None, None)


def _resume_local(ctx, store, canvas_id, canvas, runs, agent_id, receipt, path, command_id, canvas_guard, artifact_guard, prepared_acceptance=None):
    """Resume only the same reserved native command, retaining its original CAS."""
    from .source_speech import candidate_scope
    from .production_inventory import _commission, _plan_review_is_valid
    candidate_scope(ctx, canvas, canvas_id)
    state = receipt['state']
    if not isinstance(state.get('plan_ref'), dict):
        raise RemixArtifactConflict('local advancement receipt has no approved plan')
    commission_id = state['commission_ref']['artifact_id']
    commission = _commission(store, commission_id, canvas)
    relevant = [r for r in runs if (r.get('commission_ref') or {}).get('artifact_id') == commission_id]
    if relevant and relevant[0].get('status') in {'stopped', 'cancelled', 'canceled'}:
        stopped = deepcopy(state)
        stopped.update(execution_status='stopped', next_action=None, ready_system_actions=[])
        return {**stopped, 'advancement': {'action': None, 'replayed': True}}
    plan = store.read_latest(state['plan_ref']['artifact_id'])
    if not _plan_review_is_valid(store, commission, plan):
        raise RemixArtifactConflict('current plan approval changed before native recovery')
    # A binding transaction may have published exactly one plan revision before
    # its canvas/commission metadata write. Native batch idempotency certifies
    # that result; all other edits retain their normal CAS rejection.
    own_plan_ref = None
    if receipt['action']['kind'] == 'bind_materials':
        from .material_work import material_binding_commands
        from .production_commands import validate_production_command
        original = store.read(state['plan_ref']['artifact_id'], state['plan_ref']['revision'])
        work = store.read(state['work_ref']['artifact_id'], state['work_ref']['revision'])
        commands = material_binding_commands(store, work, original['payload'])
        validated = [validate_production_command({**row, 'command_id': command_id,
                     'plan_ref': _exact(state['plan_ref'])}) for row in commands]
        request = {'command_id': command_id, 'plan_ref': _exact(state['plan_ref']), 'commands': validated}
        native_path = store.root / '_production_commands' / (hashlib.sha256(command_id.encode()).hexdigest() + '.json')
        native_receipt = store._read_json(native_path) if native_path.exists() else None
        if plan['revision'] != original['revision']:
            own_plan_ref = {'artifact_id': original['id'], 'revision': original['revision'] + 1}
            if (not native_receipt or native_receipt['fingerprint'] != hashlib.sha256(store._canonical(request)).hexdigest()
                    or native_receipt['result']['plan_ref'] != own_plan_ref
                    or {'artifact_id': plan['id'], 'revision': plan['revision']} != own_plan_ref
                    or native_receipt['content_hash'] != plan['content_hash']):
                raise RemixArtifactConflict('plan changed beyond the reserved native binding result')
        if commission['commission_ref']['revision'] not in {state['commission_ref']['revision'], state['commission_ref']['revision'] + 1}:
            raise RemixArtifactConflict('commission changed beyond the reserved binding')
        if commission['commission_ref']['revision'] != state['commission_ref']['revision']:
            current = store.read_latest(commission_id)
            original_commission = store.read(commission_id, state['commission_ref']['revision'])
            current_work = store.read_latest(state['work_ref']['artifact_id'])
            work_ref = _exact(state['work_ref'])
            if current_work['revision'] != work_ref['revision']:
                rebased = next((r for r in current_work['payload'].get('command_receipts', [])
                                if r['command_id'] == command_id and r['request'] == {
                                    'kind': 'bind_materials', 'work_ref': work_ref,
                                    'plan_ref': _exact(state['plan_ref'])}), None)
                if (not rebased or rebased['result']['plan_ref'] != own_plan_ref
                        or rebased['result']['work_ref'] != {'artifact_id': current_work['id'], 'revision': current_work['revision']}):
                    raise RemixArtifactConflict('selected work changed beyond the reserved binding')
                work_ref = rebased['result']['work_ref']
            payload = deepcopy(original_commission['payload'])
            payload['plan_ref'] = {**(own_plan_ref or _exact(state['plan_ref'])), 'project_id': ctx.project_id}
            payload['work_ref'] = {**work_ref, 'project_id': ctx.project_id}
            refs = store._reference_list([*original_commission['upstream_refs'],
                                         own_plan_ref or _exact(state['plan_ref']), work_ref], ctx.project_id)
            if current['payload'] != payload or current['upstream_refs'] != refs:
                raise RemixArtifactConflict('commission changed beyond the reserved binding result')
    elif (_exact(commission['commission_ref']) != _exact(state['commission_ref'])
          or plan['revision'] != state['plan_ref']['revision']):
        raise RemixArtifactConflict('commission or plan changed before native recovery')
    _validate_plan_canvas(store, canvas, state['plan_ref'], own_plan_ref)
    result, canvas = _execute_local(ctx, store, canvas, canvas_id, state, receipt['action'], command_id, canvas_guard, prepared_acceptance)
    store._atomic_json(path, {**receipt, 'completed': True, 'result': result}, store._guard)
    updated = _read(ctx, store, canvas, canvas_id, commission_id, agent_id, runs)
    return {**updated, 'advancement': {'action': receipt['action'], 'result': result, 'replayed': True}}


def _compose(canvas, plan_ref):
    plan_nodes = [n for n in canvas['nodes'] if n.get('type') == 'scriptNode' and
                  (n.get('data', {}).get('productionPlanV2Ref') or {}).get('artifactId') == plan_ref['artifact_id']]
    if len(plan_nodes) != 1:
        raise RemixArtifactConflict('current production plan node is ambiguous')
    plan_node = plan_nodes[0]
    if plan_node['data'].get('productionPlanV2Dirty') or plan_node['data'].get('productionPlanV2Conflict'):
        raise RemixArtifactConflict('current production plan has user edits')
    targets = [n for n in canvas['nodes'] if n.get('type') == 'videoComposeNode' and
               n.get('data', {}).get('productionPlanNodeId') == plan_node['id'] and
               any(e.get('source') == plan_node['id'] and e.get('target') == n['id'] for e in canvas['edges'])]
    if len(targets) != 1:
        raise RemixArtifactConflict('current plan requires one explicitly linked compose node')
    if targets[0]['data'].get('productionPlanV2Dirty') or targets[0]['data'].get('productionPlanV2Conflict'):
        raise RemixArtifactConflict('compose draft has user edits')
    return targets[0]


def _current_native_operation(store, state, action):
    from .operations import RemixOperationStore
    scope = {'canvas_id': state['scope']['canvas_id'], 'agent_id': state['scope']['agent_id'],
             'requester_user_id': store.created_by, 'commission_id': state['commission_ref']['artifact_id']}
    return next((row for row in RemixOperationStore(store.project_dir, store.project_id).list(limit=100)
                 if row['normalized_parameters'].get('production_scope') == scope
                 and row['normalized_parameters'].get('action') == action
                 and row['normalized_parameters'].get('payload', {}).get('plan_ref' if action == 'compile_production_plan' else 'manifest_ref')
                 == _exact(state['plan_ref'] if action == 'compile_production_plan' else state['director_state']['commissions'][0]['manifest_ref'])), None)


async def _settle_compile(ctx, store, canvas_id, commission_ref, agent_id, state, *, automation_generation=None, automation_event=None):
    from .operations import RemixOperationStore
    from novelvideo.task_state import get_task_manager
    from novelvideo.chat.reference_remix_continuation import coordinator
    from .reference_commission import advance_reference_commission
    submission = state.pop('_submit')
    action = submission['action']
    mutex = canvas_store.get_canvas_write_mutex().write_mutex(ctx.state_dir, canvas_id)
    lease = await asyncio.to_thread(mutex.__enter__)
    guard = _ProductionControlGuard(ctx, lease, canvas_id=canvas_id, commission_id=commission_ref['artifact_id'],
        agent_id=agent_id, generation=automation_generation, event=automation_event)
    try:
        guard.reassert()
        canvas = await asyncio.to_thread(canvas_store.read_canvas, ctx.state_dir, canvas_id)
        runs = await asyncio.to_thread(coordinator.production_for_actor, ctx, canvas_id, agent_id)
        current = await asyncio.to_thread(_read, ctx, store, canvas, canvas_id, commission_ref['artifact_id'], agent_id, runs)
        if current['projection_revision'] != submission.get('admission_projection_revision', submission['request']['projection_revision']):
            raise RemixArtifactConflict('production projection changed before compile settlement')
        operations = RemixOperationStore(store.project_dir, store.project_id)
        operation = operations.get(action['operation_id'])
        compose = _compose(canvas, state['plan_ref'])
        task = await asyncio.to_thread(get_task_manager().get_task_for_project, ctx,
            'remix_compile_production_plan', 0, scope=operation['operation_id'])
        if (task is None or task.task_id != operation['task_id'] or task.status != 'completed'
                or (task.result or {}).get('artifact_ref') != action['manifest_ref']):
            current['next_action'] = {'owner': 'agent', 'kind': 'await_native_compilation_result', 'action_id': operation['operation_id']}
            current['advancement'] = {'action': None, 'replayed': False}
            return current
        if (operation['status'] not in {'needs_confirmation', 'completed'} or operation['canvas_id'] != canvas_id
                or operation['node_id'] != compose['id'] or compose['data'].get('remixNodeRevision') != operation['node_revision']
                or compose['data'].get('remixOperationId') != operation['operation_id']):
            raise RemixArtifactConflict('compile result no longer belongs to current compose node')
        manifest = store.read(action['manifest_ref']['artifact_id'], action['manifest_ref']['revision'])
        store._validate_v2_lineage(manifest['schema'], manifest['payload'], manifest['upstream_refs'])
        if manifest['payload']['source_versions'] != [_exact(state['plan_ref'])]:
            raise RemixArtifactConflict('compiled manifest is from another plan revision')
        updated = deepcopy(canvas)
        target = next(n for n in updated['nodes'] if n['id'] == compose['id'])
        target['data'].update(remixManifestRef={'artifactId': manifest['id'], 'revision': manifest['revision']},
            productionPlanCompiledRef={'artifactId': state['plan_ref']['artifact_id'], 'revision': state['plan_ref']['revision']},
            remixInputsCurrent=True, remixInputFingerprint=hashlib.sha256(store._canonical({
                'plan_ref': _exact(state['plan_ref']), 'manifest_ref': action['manifest_ref'],
                'media_bindings': manifest['payload']['media_bindings']})).hexdigest())
        if updated != canvas:
            canvas = await asyncio.to_thread(_canvas_publish, ctx, canvas_id, canvas, updated, guard)
        # This native result confirmation is deterministic bookkeeping. The
        # original creative/material approvals remain their immutable receipts.
        guard.reassert()
        await asyncio.to_thread(operations.confirm, operation['operation_id'],
            current_input_refs=[_exact(state['plan_ref'])], current_node_revision=operation['node_revision'],
            canvas_id=canvas_id, node_id=compose['id'])
        def finish():
            with store._locked() as artifact_guard:
                from .reference_director import _MaterialPublicationGuard
                locked = _MutationStore(store, _MaterialPublicationGuard(artifact_guard, guard))
                c = advance_reference_commission(locked, state['commission_ref'],
                    {'type': 'composition_ready', 'manifest_ref': action['manifest_ref']}, state['commission_ref']['revision'])
                path = _receipt_path(locked, canvas_id, agent_id, submission['command_id'])
                locked._atomic_json(path, {'request': submission['request'], 'action': action,
                    'completed': True, 'result': c}, artifact_guard)
                result = _read(ctx, locked, canvas, canvas_id, commission_ref['artifact_id'], agent_id, runs)
                return {**result, 'advancement': {'action': action, 'result': c, 'replayed': False}}
        return await asyncio.to_thread(finish)
    finally:
        await asyncio.to_thread(mutex.__exit__, None, None, None)


async def settle_source_adoption(ctx, *, canvas_id: str, commission_ref: dict, operation_id: str,
                                 agent_id: str = 'main', automation_generation: int | None = None, automation_event: dict | None = None) -> dict:
    """Bind a real C3 native plan result to its existing canvas and commission.

    Called by the native result/observer path. No caller-supplied task status,
    timing words, passing review or user decision enters this interface.
    """
    if not isinstance(ctx, ProjectContext) or ctx.effective_role not in {'owner', 'admin', 'editor'}:
        raise PermissionError('authenticated editor ProjectContext required')
    require_project_home_node(ctx, operation='source timing result settlement')
    from .operations import RemixOperationStore
    from .source_speech import candidate_scope
    from .source_speech_adoption import PRODUCER, read_source_adoption_result, adoption_source_signature
    from .reference_commission import read_reference_commission
    from .material_work import validate_material_completion
    from .material_execution import canvas_material_resource
    from novelvideo.task_state import get_task_manager
    from novelvideo.chat.reference_remix_continuation import coordinator
    store = RemixArtifactStore(ctx.output_dir, ctx.project_id, created_by=ctx.requester_user_id)
    operations = RemixOperationStore(ctx.output_dir, ctx.project_id)
    mutex = canvas_store.get_canvas_write_mutex().write_mutex(ctx.state_dir, canvas_id)
    lease = await asyncio.to_thread(mutex.__enter__)
    canvas_guard = _ProductionControlGuard(ctx, lease, canvas_id=canvas_id, commission_id=commission_ref['artifact_id'],
        agent_id=agent_id, generation=automation_generation, event=automation_event)
    try:
        canvas_guard.reassert()
        from .production_automation import assert_operation_automation_current
        assert_operation_automation_current(ctx, operation_id)
        canvas = await asyncio.to_thread(canvas_store.read_canvas, ctx.state_dir, canvas_id)
        if not canvas or canvas.get('canvas_id') != canvas_id:
            raise PermissionError('source result canvas differs from host scope')
        scope = candidate_scope(ctx, canvas, canvas_id)
        operation = operations.get(operation_id)
        parameters = operation.get('normalized_parameters') or {}
        packet = parameters.get('payload') or {}
        if (operation['producer_version'] != PRODUCER or parameters.get('action') != 'adopt_source_observation'
                or packet.get('scope') != scope or packet.get('requester_user_id') != ctx.requester_user_id
                or packet.get('canvas_id') != canvas_id or operation.get('cancel_reason')
                or operation['status'] not in {'needs_confirmation', 'completed'}):
            raise RemixArtifactConflict('source timing operation is not current in host scope')
        task = await asyncio.to_thread(get_task_manager().get_task_for_project, ctx,
            'remix_adopt_source_speech', 0, scope=operation_id)
        if task is None or task.task_id != operation['task_id'] or task.status != 'completed':
            raise RemixArtifactConflict('source timing awaits its actual completed native task')
        request = {key: packet.get(key) for key in ('plan_ref', 'take_id', 'observation_ref', 'work_ref',
                                                   'scope', 'requester_user_id', 'correction_ref')}
        result = await asyncio.to_thread(read_source_adoption_result, store, scope=scope,
            requester_user_id=ctx.requester_user_id, command_id=operation_id, request=request)
        if (not result or (task.result or {}).get('plan_ref') != result['plan_ref']
                or (task.result or {}).get('source_signature') != result['source_signature']
                or operation['output_refs'] != [result['plan_ref'], *result['timeline_refs']]):
            raise RemixArtifactConflict('source timing task and immutable result receipt differ')
        commission = await asyncio.to_thread(read_reference_commission, store, commission_ref, latest=True)
        if (commission['canvas_id'] != canvas_id or commission['requester_user_id'] != ctx.requester_user_id
                or (commission.get('work_ref') or {}).get('artifact_id') != packet['work_ref']['artifact_id']
                or (commission.get('plan_ref') or {}).get('artifact_id') != packet['plan_ref']['artifact_id']):
            raise RemixArtifactConflict('source timing belongs to another selected production')
        runs = await asyncio.to_thread(coordinator.production_for_actor, ctx, canvas_id, agent_id)
        relevant = [r for r in runs if (r.get('commission_ref') or {}).get('artifact_id') == commission_ref['artifact_id']]
        if relevant and relevant[0].get('status') in {'stopped', 'cancelled', 'canceled'}:
            raise RemixArtifactConflict('stopped production retains source result without advancing')
        plan = await asyncio.to_thread(store.read_latest, result['plan_ref']['artifact_id'])
        if (plan['revision'] != result['plan_ref']['revision']
                or adoption_source_signature(plan['payload'], packet['take_id']) != result['source_signature']):
            raise RemixArtifactConflict('source timing plan changed before canvas settlement')
        work = await asyncio.to_thread(store.read_latest, packet['work_ref']['artifact_id'])
        await asyncio.to_thread(validate_material_completion, store, work, plan['payload'])
        resources = [s['accepted_resource'] for s in work['payload']['steps'] if s['status'] == 'accepted']
        take = next(t for t in plan['payload']['speech_takes'] if t['id'] == packet['take_id'])
        source = next(r for r in plan['payload']['resources'] if r['id'] == take['resource_id'])
        for resource in [*resources, source]:
            actual = await asyncio.to_thread(canvas_material_resource, store, canvas, resource['node_id'], resource['media_kind'])
            if actual != resource:
                raise RemixArtifactConflict('accepted current canvas media changed before source settlement')
        from .production_inventory import _plan_review_is_valid
        if not _plan_review_is_valid(store, commission, plan):
            raise RemixArtifactConflict('creative plan approval changed before source settlement')
        if _exact(commission['plan_ref']) not in [packet['plan_ref'], result['plan_ref']]:
            raise RemixArtifactConflict('commission changed beyond source timing result')
        nodes = [n for n in canvas['nodes'] if n.get('type') == 'scriptNode' and
                 (n.get('data', {}).get('productionPlanV2Ref') or {}).get('artifactId') == plan['id']]
        if len(nodes) != 1:
            raise RemixArtifactConflict('source timing plan node is ambiguous')
        data = nodes[0]['data']
        ref = data['productionPlanV2Ref']
        if data.get('productionPlanV2Dirty') or data.get('productionPlanV2Conflict') or ref.get('revision') not in [packet['plan_ref']['revision'], result['plan_ref']['revision']]:
            raise RemixArtifactConflict('source timing preserves current user draft conflict')
        if data.get('productionPlanV2Draft') is not None:
            from .contracts import validate_payload
            referenced = await asyncio.to_thread(store.read, plan['id'], ref['revision'])
            if validate_payload('production_plan.v2', data['productionPlanV2Draft']) != referenced['payload']:
                raise RemixArtifactConflict('source timing preserves changed user draft')
        _validate_plan_canvas(store, canvas, packet['plan_ref'], result['plan_ref'])
        if ref['revision'] != result['plan_ref']['revision']:
            canvas = await asyncio.to_thread(_plan_canvas, ctx, store, canvas_id, canvas, result['plan_ref'], resources, canvas_guard, original_plan_ref=packet['plan_ref'])
        canvas_guard.reassert()
        assert_operation_automation_current(ctx, operation_id)
        await asyncio.to_thread(operations.confirm, operation_id, current_input_refs=operation['input_refs'],
            current_node_revision=operation['node_revision'], canvas_id=canvas_id, node_id=nodes[0]['id'])
        def finish():
            with store._locked() as artifact_guard:
                from .reference_director import _MaterialPublicationGuard
                locked = _MutationStore(store, _MaterialPublicationGuard(artifact_guard, canvas_guard))
                current = locked.read_latest(commission_ref['artifact_id'])
                payload = deepcopy(current['payload'])
                if _exact(payload['plan_ref']) != result['plan_ref']:
                    if current['revision'] != commission_ref['revision'] or payload['phase'] != 'aligning_speech':
                        raise RemixArtifactConflict('commission changed before timing result publication')
                    payload['plan_ref'] = {**result['plan_ref'], 'project_id': ctx.project_id}
                    refs = locked._reference_list([*current['upstream_refs'], result['plan_ref']], ctx.project_id)
                    locked.revise(current['id'], current['revision'], payload, refs)
                return _read(ctx, locked, canvas, canvas_id, commission_ref['artifact_id'], agent_id, runs)
        return await asyncio.to_thread(finish)
    finally:
        await asyncio.to_thread(mutex.__exit__, None, None, None)


async def enqueue_source_observation_adoption(ctx, store, prepared: dict, *, command_id: str,
                                             node_revision: int, automation_control: dict | None = None, automation_event: dict | None = None) -> dict:
    """Enqueue C3's exact native packet through the existing submission fence."""
    import uuid
    from novelvideo.ports import get_task_backend
    from novelvideo.task_identity import project_task_state_key
    from .operations import RemixOperationStore
    from .source_speech_adoption import prepare_source_observation_adoption
    from novelvideo.api.routes.remix import record_enqueue_exception
    if not isinstance(ctx, ProjectContext) or ctx.effective_role not in {'owner', 'admin', 'editor'}:
        raise PermissionError('authenticated editor ProjectContext required')
    require_project_home_node(ctx, operation='source timing task submission')
    if store.project_id != ctx.project_id or store.created_by != ctx.requester_user_id or store.project_dir != Path(ctx.output_dir).resolve():
        raise PermissionError('source timing store differs from host context')
    packet = prepared['task_payload']
    canvas_id = packet['canvas_id']
    if type(node_revision) is not int or node_revision != packet['plan_ref']['revision']:
        raise ValueError('source timing node_revision must be the original saved plan revision')
    operations = RemixOperationStore(store.project_dir, ctx.project_id)
    mutex = canvas_store.get_canvas_write_mutex().write_mutex(ctx.state_dir, canvas_id)
    guard = await asyncio.to_thread(mutex.__enter__)
    try:
        from .production_automation import assert_production_automation_current, bind_automation_operation
        if automation_control is not None:
            control_scope = automation_control['scope']
            if (control_scope.get('project_id'), control_scope.get('requester_user_id'), control_scope.get('canvas_id')) != (ctx.project_id, ctx.requester_user_id, canvas_id):
                raise PermissionError('source timing automation host scope differs')
            assert_production_automation_current(ctx, canvas_id=canvas_id, commission_id=control_scope['commission_id'],
                agent_id=control_scope['agent_id'], generation=automation_control['generation'])
        if automation_event is not None:
            from .production_automation import assert_automation_event_current
            if automation_control is None or automation_event['scope'] != automation_control['scope']:
                raise PermissionError('source timing event belongs to another automation')
            assert_automation_event_current(ctx, automation_event)
        canvas = await asyncio.to_thread(canvas_store.read_canvas, ctx.state_dir, canvas_id)
        if not canvas or canvas.get('canvas_id') != canvas_id:
            raise PermissionError('source timing canvas differs from host scope')
        current = await asyncio.to_thread(prepare_source_observation_adoption, store, ctx, canvas,
            canvas_id=canvas_id, plan_ref=packet['plan_ref'], take_id=packet['take_id'],
            observation_ref=packet['observation_ref'], work_ref=packet['work_ref'], correction_ref=packet.get('correction_ref'))
        if current != prepared:
            raise RemixArtifactConflict('source timing inputs changed before task admission')
        node = next(n for n in canvas['nodes'] if n.get('type') == 'scriptNode' and
                    (n.get('data', {}).get('productionPlanV2Ref') or {}).get('artifactId') == packet['plan_ref']['artifact_id'])
        operation = await asyncio.to_thread(operations.start, idempotency_key=command_id,
            input_refs=prepared['input_refs'], producer_version=prepared['producer_version'],
            normalized_parameters=prepared['normalized_parameters'], node_revision=node_revision,
            input_media_hashes=prepared['input_media_hashes'], canvas_id=canvas_id, node_id=node['id'])
        if automation_control is not None:
            bind_automation_operation(ctx, canvas_id=canvas_id, commission_id=control_scope['commission_id'],
                agent_id=control_scope['agent_id'], generation=automation_control['generation'],
                operation_id=operation['operation_id'], canvas_guard=guard)
        if not operation['task_id'] and operation['status'] == 'pending':
            owner = uuid.uuid4().hex
            fence = await asyncio.to_thread(operations.claim_submission, operation['operation_id'], owner=owner)
            if fence is not None:
                try:
                    guard.reassert()
                    if automation_event is not None:
                        assert_automation_event_current(ctx, automation_event)
                    queued = await get_task_backend().enqueue_project_task(ctx, product_surface='freezone',
                        task_type='remix_adopt_source_speech', queue_kind='default', episode=0, scope=operation['operation_id'],
                        payload={**packet, 'node_revision': node_revision, 'operation_id': operation['operation_id']})
                except Exception as exc:
                    await record_enqueue_exception(operations, operation['operation_id'], owner=owner, fencing_token=fence, error=exc)
                    raise
                operation = await asyncio.to_thread(operations.bind_task, operation['operation_id'],
                    task_id=queued.task_state.task_id, owner=owner, fencing_token=fence)
        return {**operation, 'task_type': 'remix_adopt_source_speech',
                'task_key': project_task_state_key('remix_adopt_source_speech', ctx.project_id, 0, scope=operation['operation_id']),
                'operation_status': operation['status']}
    finally:
        await asyncio.to_thread(mutex.__exit__, None, None, None)
