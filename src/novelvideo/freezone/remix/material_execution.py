"""Host observations of real canvas material, never Agent supplied media metadata."""
from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from .material_work import (
    _exact_ref,
    ready_material_steps,
    material_workflow_intent,
    material_lifecycle_status,
    _IndependentMaterialReviewContinuity,
)
from .production_plan_v2 import ResourceV2


def canvas_material_resource(store, canvas: dict, node_id: str, media_kind: str) -> dict:
    from novelvideo.api.routes.remix_media import _inspect_existing_media
    node = next((n for n in canvas.get('nodes', []) if n.get('id') == node_id), None)
    if node is None:
        raise ValueError('material node is not on the current canvas')
    data = node.get('data', {})
    if data.get('workflowResultStale') is True or data.get('remixVisualOutputCurrent') is False:
        raise ValueError('material node output is stale')
    field = {'image':'imageUrl', 'video':'videoUrl', 'audio':'audioUrl'}[media_kind]
    url = data.get(field) or (data.get('previewImageUrl') if media_kind == 'image' else None)
    if not isinstance(url, str):
        raise ValueError('material node has no current output')
    parsed = urlsplit(url)
    prefixes = (f'/api/v1/projects/{quote(store.project_id, safe="")}/media/',
                f'/static/projects/{quote(store.project_id, safe="")}/')
    relative = next((unquote(parsed.path[len(p):]) for p in prefixes if parsed.path.startswith(p)), None)
    if relative is None or parsed.scheme or parsed.netloc:
        raise ValueError('material must use a local project media URL')
    inspected = _inspect_existing_media(store.project_dir, relative)
    if inspected['media_kind'] != media_kind:
        raise ValueError('material media kind differs from step')
    resource = {'id': 'material_' + hashlib.sha256((node_id + inspected['media_hash']).encode()).hexdigest()[:24],
                'node_id': node_id, **{key: inspected[key] for key in ('media_path', 'media_hash', 'media_kind', 'duration_us', 'has_audio')}}
    return ResourceV2.model_validate(resource).model_dump(mode='json', exclude_none=True)


def _attached_material_drafts(project_dir, canvas_id, work) -> dict[str, dict]:
    """Read every draft a material step points at, keyed by draft id."""
    from novelvideo.freezone.workflow_drafts import read_workflow_draft
    drafts: dict[str, dict] = {}
    for step in work.get('steps') or []:
        if not isinstance(step, dict):
            continue
        draft_id = str((step.get('execution') or {}).get('draft_id') or '').strip()
        if not draft_id or draft_id in drafts:
            continue
        draft, error = read_workflow_draft(
            project_dir=project_dir, canvas_id=canvas_id, draft_id=draft_id,
        )
        if error or not draft:
            continue
        drafts[draft_id] = draft
    return drafts


def reopenable_material_step_ids(project_dir, canvas_id, work) -> list[str]:
    """Steps whose attached draft was never confirmed and can be replanned.

    A ``ready`` draft still waits for the canvas confirmation; a ``cancelled``
    one is abandoned. Treating their steps as pending again lets an incorrect
    draft be replanned without a manual retry, and never claims generation.
    """
    drafts = _attached_material_drafts(project_dir, canvas_id, work)
    reopenable = {draft_id for draft_id, draft in drafts.items()
                  if str(draft.get('status') or '') in {'ready', 'cancelled'}}
    return [step['id'] for step in work.get('steps') or []
            if isinstance(step, dict)
            and str((step.get('execution') or {}).get('draft_id') or '').strip() in reopenable]


def material_submit_action(draft: dict) -> dict | None:
    revision = draft.get('revision') or draft.get('draft_revision')
    if ((draft.get('draft_status') or draft.get('status')) != 'ready'
            or draft.get('confirmation_started_at') is not None
            or not draft.get('draft_id') or type(revision) is not int):
        return None
    return {'owner': 'agent', 'kind': 'submit_canvas_approval',
            'action_id': 'submit:%s:%s' % (draft['draft_id'], revision),
            'tool': 'freezone_confirm_workflow_draft',
            'draft_id': draft['draft_id'], 'revision': revision}


def _material_approval_requests(draft, *, bridge_dirs=()):
    """Read current native requests without pruning or creating bridge state."""
    import json
    import os
    import sqlite3
    import time
    from novelvideo.freezone.canvas_command_bridge import _bridge_db_path
    task_id = draft.get('task_id')
    if not task_id or draft.get('confirmation_started_at') is None:
        return []

    def matches(payload):
        if (payload.get('project_id'), payload.get('canvas_id')) != (draft['project_id'], draft['canvas_id']):
            return False
        return any(isinstance(command, dict) and command.get('type') == 'create_node'
                   and (command.get('data') or {}).get('workflowInstanceId') == draft['draft_id']
                   and (command.get('data') or {}).get('workflowDraftRevision') == draft['revision']
                   and (command.get('data') or {}).get('workflowConfirmationTaskId') == task_id
                   for command in payload.get('commands', []))

    requests = []
    roots = list(dict.fromkeys([_bridge_db_path().parent, *map(Path, bridge_dirs)]))
    for directory in roots:
        path = _bridge_db_path(directory)
        if not path.is_file():
            continue
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='canvas_command_messages'").fetchone():
                rows = conn.execute("SELECT bridge_key,payload_json FROM canvas_command_messages WHERE project_id=? AND canvas_id=? AND kind='canvas_command' AND status IN ('pending','delivered') AND created_at+600>?",
                                    (draft['project_id'], draft['canvas_id'], time.time())).fetchall()
                for identity, raw in rows:
                    payload = json.loads(raw)
                    if matches(payload) and not (payload.get('envelope') or {}).get('external_mcp_command'):
                        requests.append({'request_id': identity, 'kind': 'canvas_command'})
    roots.append(Path(os.environ.get('DRAMACLAW_CANVAS_COMMAND_BRIDGE_DIR') or '/tmp/dramaclaw_canvas_command_bridge'))
    for directory in dict.fromkeys(roots):
        root = directory / 'mcp_canvas_approvals'
        if not root.is_dir():
            continue
        for path in sorted(root.glob('*.json'))[:1000]:
            if path.is_symlink():
                continue
            try:
                payload = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if payload.get('expires_at_ms', 0) > time.time() * 1000 and matches(payload):
                requests.append({'request_id': payload['approval_id'], 'kind': 'canvas_approval'})
    return requests


def read_material_execution_evidence(work: dict, *, project_id: str, canvas_id: str,
                                     state_dir, requester_user_id=None, agent_id='main') -> dict:
    """Observe only saved step bindings using read-only database connections."""
    import json
    import sqlite3
    import time
    from novelvideo.freezone.workflow_drafts import _payload_from_row
    result = {'drafts': [], 'native_runs': [], 'problems': []}
    path = Path(state_dir) / 'data.db'
    if not path.is_file():
        return result
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        bindings = {}
        for step in work.get('steps', []):
            execution = step.get('execution') or {}
            if execution.get('canvas_id', canvas_id) != canvas_id:
                continue
            draft_id = execution.get('draft_id')
            if draft_id:
                bindings.setdefault(draft_id, []).append(execution)
            run_id = execution.get('run_id')
            if run_id and 'workflow_runs' in tables:
                row = conn.execute('SELECT run_id,status FROM workflow_runs WHERE run_id=? AND project_id=? AND canvas_id=?', (run_id, project_id, canvas_id)).fetchone()
                if row and dict(row) not in result['native_runs']:
                    result['native_runs'].append(dict(row))
        for draft_id, executions in bindings.items():
            row = conn.execute('SELECT * FROM workflow_drafts WHERE draft_id=? AND project_id=? AND canvas_id=?', (draft_id, project_id, canvas_id)).fetchone() if 'workflow_drafts' in tables else None
            if not row or any(execution.get('draft_revision') != row['revision']
                              or execution.get('workflow_instance_id') != draft_id for execution in executions):
                result['problems'].append({'draft_id': draft_id, 'kind': 'material_draft_binding_conflict'})
                continue
            draft = _payload_from_row(row)
            if draft['status'] == 'ready' and draft.get('expires_at', 0) <= time.time():
                result['problems'].append({'draft_id': draft_id, 'revision': draft['revision'],
                                           'kind': 'material_draft_expired'})
                continue
            receipts = []
            if 'workflow_delivery_receipts' in tables:
                rows = conn.execute('SELECT summary_json FROM workflow_delivery_receipts WHERE canvas_id=? AND draft_id=? AND revision=? AND task_id=? AND attempt=? ORDER BY id',
                                    (canvas_id, draft_id, draft['revision'], draft.get('task_id'), draft.get('confirmation_started_at'))).fetchall()
                receipts = [json.loads(row[0]) for row in rows]
            from novelvideo.chat.hermes_pool import canvas_bridge_dir_for_profile
            home = Path(state_dir) / 'agents' / 'hermes' / 'freezone'
            bridge_dirs = [canvas_bridge_dir_for_profile(home, 'freezone:' + agent_id),
                           canvas_bridge_dir_for_profile(home, 'freezone')]
            approvals = _material_approval_requests(draft, bridge_dirs=bridge_dirs) if draft['status'] in {'confirming', 'submitted'} else []
            action = material_submit_action(draft)
            if approvals:
                action = {'owner': 'user', 'kind': 'await_user_confirmation', 'action_id': approvals[0]['request_id'], 'request_id': approvals[0]['request_id']}
            elif draft['status'] in {'confirming', 'submitted'} and not any(r.get('graph_state') == 'delivered' for r in receipts):
                task = conn.execute("SELECT status FROM task_states WHERE task_id=? AND project_id=? AND task_type='freezone_workflow_confirm'", (draft.get('task_id'), project_id)).fetchone() if 'task_states' in tables else None
                if task and task['status'] in {'queued', 'pending', 'running'}:
                    action = {'owner': 'system', 'kind': 'await_canvas_receipt', 'action_id': draft['task_id']}
                else:
                    result['problems'].append({'draft_id': draft_id, 'revision': draft['revision'], 'kind': 'material_delivery_unverified'})
            lifecycle = material_lifecycle_status(work, workflow_draft=draft, delivery_receipts=receipts,
                pending_approval=bool(approvals), native_statuses=[r['status'] for r in result['native_runs']])
            result['drafts'].append({'draft_id': draft_id, 'revision': draft['revision'], 'status': draft['status'],
                                     'material_lifecycle': lifecycle, 'next_action': action})
    return result


def read_material_state(store, work_ref: dict, *, canvas: dict | None = None, canvas_id: str | None = None,
                        include_workflow_intent: bool = True, workflow_state_dir=None, agent_id='main') -> dict:
    _exact_ref(work_ref)
    saved = store.read_latest(work_ref['artifact_id'])
    if saved['schema'] != 'material_work_package.v1':
        raise ValueError('material work package required')
    work = saved['payload']
    lifecycle = material_lifecycle_status(work)
    drafts_dir = Path(workflow_state_dir) if workflow_state_dir is not None else store.project_dir
    evidence = read_material_execution_evidence(work, project_id=store.project_id,
        canvas_id=canvas_id, state_dir=drafts_dir, requester_user_id=store.created_by, agent_id=agent_id) if canvas_id else None
    reopenable_step_ids: list[str] = []
    if canvas_id:
        drafts = {row['draft_id']: row for row in (evidence or {}).get('drafts', [])}
        reopenable_draft_ids = {draft_id for draft_id, draft in drafts.items()
                                if str(draft.get('status') or '') in {'ready', 'cancelled'}}
        reopenable_step_ids = [step['id'] for step in work.get('steps', [])
                               if isinstance(step, dict)
                               and str((step.get('execution') or {}).get('draft_id') or '').strip()
                               in reopenable_draft_ids]
        lifecycle_by_draft: list[dict] = []
        pending_draft_ids: list[str] = []
        for draft_id in sorted(drafts):
            state = drafts[draft_id]['material_lifecycle']
            if state['canvas_delivery_status'] in {'ready_to_submit', 'awaiting_confirmation'}:
                pending_draft_ids.append(draft_id)
            lifecycle_by_draft.append(state)
        if lifecycle_by_draft:
            # Report the draft that still needs host action first: a delivered
            # earlier wave must not hide a later draft that is waiting for the
            # canvas confirmation.
            precedence = {'ready_to_submit': 5, 'awaiting_confirmation': 4, 'pending': 3, 'delivered': 2,
                          'failed': 1, 'not_requested': 0}
            lifecycle = max(lifecycle_by_draft,
                            key=lambda state: precedence.get(state['canvas_delivery_status'], 0))
            if pending_draft_ids:
                lifecycle = {**lifecycle, 'pending_draft_ids': pending_draft_ids}
    return {'work_ref': {'artifact_id': saved['id'], 'revision': saved['revision']}, 'work': work,
            'ready_step_ids': ready_material_steps(work, reopenable_step_ids),
            'reopenable_step_ids': reopenable_step_ids,
            'material_lifecycle': lifecycle, 'material_execution': evidence,
            **({'workflow_intent': material_workflow_intent(
                work, canvas=canvas, store=store, reopenable_step_ids=reopenable_step_ids)}
               if include_workflow_intent else {}),
            **(_saved_material_reviews(store, saved, canvas, canvas_id) if canvas is not None and canvas_id else {})}



def _saved_material_reviews(store, work, canvas: dict, canvas_id: str) -> dict:
    """Keep original evidence; certify only unchanged independent accept history."""
    import json
    from .material_review import validate_material_review_lineage
    from .production_inventory import discover_artifact_heads

    exact = {'artifact_id': work['id'], 'revision': work['revision']}
    rows, total_bytes, truncated = [], 2, False
    with store._locked():
        continuity = _IndependentMaterialReviewContinuity(store, work, canvas_id)
        heads, truncated = discover_artifact_heads(store)
        for header in heads:
            try:
                if header.get('schema') != 'material_review.v1' or header.get('created_by') != store.created_by:
                    continue
                saved = continuity._read(header['id'], header['revision'])
                payload = saved['payload']
                if payload['work_ref'].get('artifact_id') != exact['artifact_id']:
                    continue
                validate_material_review_lineage(continuity, payload, saved['upstream_refs'])
                decision = payload.get('user_decision')
                if decision and (decision.get('canvas_id') != canvas_id or decision.get('requester_user_id') != store.created_by):
                    continue
                resource = payload['resource']
                step = next(s for s in work['payload']['steps'] if s['id'] == payload['step_id'])
                if step['media_kind'] != resource['media_kind'] or (step.get('node_id') and step['node_id'] != resource['node_id']):
                    continue
                if canvas_material_resource(store, canvas, resource['node_id'], resource['media_kind']) != resource:
                    continue
                compatible = payload['work_ref'] == exact or (
                    step.get('status') == 'accepted'
                    and step.get('accepted_resource') == resource
                    and step.get('review_ref') == {'artifact_id': saved['id'], 'revision': saved['revision']}
                    and payload['technical']['status'] == payload['content']['status'] == 'passed')
                if not compatible:
                    try:
                        continuity.validate_target(saved)
                        compatible = True
                    except (ValueError, FileNotFoundError, KeyError, StopIteration):
                        pass
                row = {'review_ref': {'artifact_id': saved['id'], 'revision': saved['revision']},
                       'work_ref': payload['work_ref'], 'upstream_refs': saved['upstream_refs'],
                       'matches_current_work_revision': payload['work_ref'] == exact,
                       'review_compatible_with_current_work': compatible,
                       'step_id': payload['step_id'], 'resource': resource,
                       'technical': {'status': payload['technical']['status']},
                       'content': {'status': payload['content']['status']}, 'actor_kind': payload['actor_kind']}
                size = len(json.dumps(row, ensure_ascii=False).encode('utf-8')) + 2
                if len(rows) >= 50 or total_bytes + size > 65536:
                    truncated = True
                    break
                rows.append(row)
                total_bytes += size
            except (ValueError, FileNotFoundError, KeyError, StopIteration):
                continue
    return {'material_reviews': rows, 'material_reviews_truncated': truncated}


def register_material_execution(store, work_ref: dict, step_id: str, node_id: str, execution: dict, *, command_id: str | None = None) -> dict:
    """Caller must first verify execution against the approved native workflow run."""
    _exact_ref(work_ref)
    from .contracts import validate_payload
    if command_id is not None and (not isinstance(command_id, str) or not 1 <= len(command_id) <= 128):
        raise ValueError('invalid material registration command_id')
    with store._locked() as guard:
        saved = store._read(work_ref['artifact_id'], store._latest_revision(work_ref['artifact_id']))
        if saved['schema'] != 'material_work_package.v1':
            raise ValueError('material work package required')
        work = deepcopy(saved['payload'])
        step = next((s for s in work['steps'] if s['id'] == step_id), None)
        registered = {**execution, **({'settlement_command_id': command_id} if command_id else {})}
        if command_id and step and (step.get('execution') or {}).get('settlement_command_id') == command_id:
            if step.get('node_id') != node_id or step.get('execution') != registered:
                raise ValueError('material registration command reused with different inputs')
            return {'work_ref': {'artifact_id': saved['id'], 'revision': saved['revision']}, 'work': work}
        # Discovery may already have bound this exact native identity. Its
        # publication is the registration receipt; do not create another work
        # revision solely to attach a settlement marker.
        if step and step.get('node_id') == node_id and step.get('execution') == execution and step.get('task_id') == execution.get('task_id'):
            return {'work_ref': {'artifact_id': saved['id'], 'revision': saved['revision']}, 'work': work}
        if saved['revision'] != work_ref['revision']:
            raise ValueError('material work changed; reload current revision')
        if not step or step['source'] != 'generate' or (step_id not in ready_material_steps(work) and step.get('status') != 'submitted'):
            raise ValueError('generation step is not ready')
        step.update(status='submitted', node_id=node_id, execution=registered, task_id=execution.get('task_id'))
        work = validate_payload(saved['schema'], work)
        store._validate_v2_lineage(saved['schema'], work, saved['upstream_refs'])
        result = store._publish(artifact_id=saved['id'], schema=saved['schema'], revision=saved['revision'] + 1,
            payload=work, upstream_refs=saved['upstream_refs'], created_by=saved['created_by'], guard=guard)
    return {'work_ref': {'artifact_id': result['id'], 'revision': result['revision']}, 'work': result['payload']}


def review_material_candidate(store, work_ref: dict, step_id: str, canvas: dict, node_id: str, *, workflow_state_dir: Path | None = None, command_id: str | None = None, settlement_id: str | None = None) -> dict:
    _exact_ref(work_ref)
    saved = store.read(work_ref['artifact_id'], work_ref['revision'])
    if saved['schema'] != 'material_work_package.v1':
        raise ValueError('material work package required')
    if store.read_latest(saved['id'])['revision'] != saved['revision']:
        raise ValueError('material work changed; reload current revision')
    step = next((s for s in saved['payload']['steps'] if s['id'] == step_id), None)
    if not step:
        raise ValueError('unknown material step')
    if step['source'] == 'generate':
        validate_material_native_execution(store, canvas, step, node_id, require_complete=True, workflow_state_dir=workflow_state_dir, settlement_id=settlement_id, work_ref=work_ref)
    if step['source'] == 'existing' and step.get('node_id') != node_id:
        raise ValueError('existing material node differs from planned resource')
    resource = canvas_material_resource(store, canvas, node_id, step['media_kind'])
    source_evidence = []
    if step['source'] in {'existing', 'upload'}:
        source_evidence.append({'role': 'supplied_source', 'node_id': node_id,
            'media_kind': resource['media_kind'], 'media_hash': resource['media_hash']})
    steps = {row['id']: row for row in saved['payload']['steps']}
    for reference in step.get('references', []):
        source_step = steps.get(reference.get('step_id')) if reference.get('step_id') else None
        reference_node = (source_step.get('accepted_resource') or {}).get('node_id') if source_step else reference.get('node_id')
        if not reference_node:
            raise ValueError('material reference has no accepted source media')
        reference_kind = (source_step.get('accepted_resource') or {}).get('media_kind') if source_step else 'image'
        observed = canvas_material_resource(store, canvas, reference_node, reference_kind)
        if source_step and observed != source_step.get('accepted_resource'):
            raise ValueError('material reference differs from accepted source media')
        source_evidence.append({'role': reference['role'], 'node_id': reference_node,
            'step_id': reference.get('step_id'), 'media_kind': observed['media_kind'],
            'media_hash': observed['media_hash']})
    payload = {'resource':resource, 'step_id':step_id, 'work_ref':work_ref,
               'technical':{'status':'passed','checks':['current_canvas_node','project_file_hash','native_media_probe']},
               'content':{'status':'unavailable','findings':['Actual content inspection unavailable; review the current material and confirm.']},
               'actor_kind':'system', 'evidence_refs':[], 'source_evidence':source_evidence}
    if command_id is None:
        review = store.create('material_review.v1', payload, [work_ref])
    else:
        from .contracts import validate_payload
        artifact_id = hashlib.sha256(('material-review:' + command_id).encode()).hexdigest()
        with store._locked() as guard:
            try:
                review = store._read(artifact_id, 1)
                if review['schema'] != 'material_review.v1' or review['payload'] != validate_payload('material_review.v1', payload):
                    raise ValueError('material check command reused with different inputs')
            except FileNotFoundError:
                validated = validate_payload('material_review.v1', payload)
                refs = store._reference_list([work_ref], store.project_id)
                store._validate_upstream_refs(refs)
                store._validate_v2_lineage('material_review.v1', validated, refs)
                review = store._publish(artifact_id=artifact_id, schema='material_review.v1', revision=1,
                    payload=validated, upstream_refs=refs, created_by=store.created_by, guard=guard)
    return {'review_ref':{'artifact_id':review['id'],'revision':review['revision']}, 'review':review['payload'],
            'requires_user_review':True}


def confirm_material_review(store, review_ref: dict, canvas: dict, *, canvas_id: str, requester_user_id: str, _guard=None) -> dict:
    from .material_review import validate_material_review_lineage
    saved = (store._read(review_ref['artifact_id'], store._latest_revision(review_ref['artifact_id']))
             if _guard is not None else store.read_latest(review_ref['artifact_id']))
    if saved['schema'] != 'material_review.v1':
        raise ValueError('material review required')
    payload = deepcopy(saved['payload'])
    validate_material_review_lineage(store, payload, saved['upstream_refs'])
    current = canvas_material_resource(store, canvas, payload['resource']['node_id'], payload['resource']['media_kind'])
    if current != payload['resource']:
        raise ValueError('material changed after review')
    if payload['technical']['status'] != 'passed':
        raise ValueError('technical review has not passed')
    if payload.get('user_decision'):
        if payload['user_decision']['requester_user_id'] != requester_user_id or payload['user_decision']['canvas_id'] != canvas_id:
            raise ValueError('material decision belongs to another user or canvas')
        return {'review_ref':{'artifact_id':saved['id'],'revision':saved['revision']},'review':payload}
    payload['user_decision'] = {'requester_user_id':requester_user_id,'canvas_id':canvas_id,'project_id':store.project_id,
                                'media_hash':current['media_hash']}
    payload['decision_receipt_id'] = hashlib.sha256(store._canonical(payload['user_decision'])).hexdigest()
    payload['content'] = {'status':'passed','checks':['authenticated_user_viewed_current_material'], 'findings':[]}
    payload['actor_kind'] = 'user'
    if _guard is None:
        revised = store.revise(saved['id'], saved['revision'], payload, saved['upstream_refs'])
    else:
        # The director already holds the artifact mutex for both publications.
        # Calling revise here would try to acquire that non-reentrant mutex again.
        from .contracts import validate_payload
        payload = validate_payload(saved['schema'], payload)
        store._validate_upstream_refs(saved['upstream_refs'])
        store._validate_v2_lineage(saved['schema'], payload, saved['upstream_refs'])
        revised = store._publish(artifact_id=saved['id'], schema=saved['schema'], revision=saved['revision'] + 1,
            payload=payload, upstream_refs=saved['upstream_refs'], created_by=saved['created_by'], guard=_guard)
    return {'review_ref':{'artifact_id':revised['id'],'revision':revised['revision']},'review':revised['payload']}


def attach_material_execution_draft(store, work_ref: dict, *, canvas_id: str, draft_id: str, draft_revision: int, workflow_state_dir: Path | None = None, canvas: dict | None = None) -> dict:
    from novelvideo.freezone.workflow_drafts import read_workflow_draft
    state_dir = workflow_state_dir if workflow_state_dir is not None else store.project_dir
    saved = store.read(work_ref['artifact_id'], work_ref['revision'])
    if saved['schema'] != 'material_work_package.v1':
        raise ValueError('material work package required')
    draft, error = read_workflow_draft(project_dir=state_dir, canvas_id=canvas_id, draft_id=draft_id)
    if error or not draft or draft['revision'] != draft_revision or draft['project_id'] != store.project_id:
        raise ValueError('native workflow draft scope or revision differs')
    # A wrong draft that was never confirmed is replanned: include the steps it
    # still holds so the whole graph is recompiled in one wave.
    reopenable_step_ids = reopenable_material_step_ids(state_dir, canvas_id, saved['payload'])
    expected = material_workflow_intent(saved['payload'], canvas=canvas, store=store,
                                        reopenable_step_ids=reopenable_step_ids)
    # Compilers normalize optional defaults; compare executable identity and supplied parameters.
    intent = draft.get('intent', {})
    if intent.get('skill_id') != expected['skill_id'] or intent.get('items') != expected['items'] or intent.get('external_inputs', []) != expected['external_inputs']:
        raise ValueError('native workflow draft differs from ready material wave')
    if expected['inputs']:
        supplied = intent.get('inputs')
        if (not isinstance(supplied, dict) or any(key not in supplied for key in expected['inputs'])
                or store._canonical({key: supplied[key] for key in expected['inputs']}) != store._canonical(expected['inputs'])):
            raise ValueError('native workflow draft generation inputs differ from saved material controls')
    work = deepcopy(saved['payload'])
    for step in work['steps']:
        if step['id'] in {item['id'] for item in expected['items']}:
            step['status'] = 'submitted'
            step['execution'] = {'draft_id':draft_id, 'draft_revision':draft_revision,
                                 'workflow_instance_id':draft_id, 'plan_node_id':step['id'], 'canvas_id':canvas_id}
    result = store.revise(saved['id'], saved['revision'], work, saved['upstream_refs'])
    _cancel_superseded_material_drafts(state_dir, canvas_id, saved['payload'], keep_draft_id=draft_id)
    return {'work_ref':{'artifact_id':result['id'],'revision':result['revision']}, 'work':result['payload'],
            'ready_step_ids':ready_material_steps(result['payload']),
            'reopenable_step_ids':reopenable_material_step_ids(state_dir, canvas_id, result['payload'])}


def _cancel_superseded_material_drafts(project_dir, canvas_id, work, *, keep_draft_id) -> None:
    """Cancel ready drafts a replanned wave supersedes so they cannot be confirmed."""
    from novelvideo.freezone.workflow_drafts import cancel_workflow_draft
    for draft_id, draft in _attached_material_drafts(project_dir, canvas_id, work).items():
        if draft_id == keep_draft_id or str(draft.get('status') or '') != 'ready':
            continue
        try:
            cancel_workflow_draft(project_dir=project_dir, canvas_id=canvas_id,
                                  draft_id=draft_id, expected_revision=draft['revision'])
        except Exception:  # best-effort supersession; the new draft stays authoritative
            pass


def material_step_target(step: dict) -> dict:
    """Inputs whose changes invalidate native authorization and material checks."""
    return {key: step.get(key) for key in ('source', 'recipe_id', 'label', 'prompt',
        'media_kind', 'depends_on', 'references', 'generation_inputs')}


def _settled_complete_execution(store, canvas, step, node_id, execution, settlement_id, work_ref, workflow_state_dir):
    from .operations import RemixOperationStore
    from .material_settlement import settlement_key
    if not settlement_id and work_ref:
        scope = {'project_id': store.project_id, 'requester_user_id': store.created_by,
            'canvas_id': execution['canvas_id'], 'work_id': work_ref['artifact_id'], 'step_id': step['id']}
        task = {'run_id': execution.get('run_id'), 'node_id': node_id, 'task_key': execution.get('task_id')}
        settlement_id = settlement_key(scope, task, {})
    if not settlement_id:
        return None
    receipt = RemixOperationStore(store.project_dir, store.project_id).get_settlement(settlement_id) or {}
    scope, proof = receipt.get('scope') or {}, receipt.get('native_evidence') or {}
    authorization = receipt.get('native_authorization') or {}
    native_execution = proof.get('execution') or {}
    exact_work_ref = work_ref or receipt.get('work_ref')
    if (receipt.get('disposition') or not exact_work_ref
            or scope != {'project_id': store.project_id, 'requester_user_id': store.created_by,
                'canvas_id': execution['canvas_id'], 'work_id': exact_work_ref['artifact_id'], 'step_id': step['id']}
            or authorization.get('status') != 'confirmed'
            or authorization.get('project_id') != store.project_id
            or authorization.get('canvas_id') != execution['canvas_id']
            or authorization.get('draft_id') != execution['draft_id']
            or authorization.get('revision') != execution['draft_revision']
            or not authorization.get('confirmed_at') or not authorization.get('task_id')
            or proof.get('node_id') != node_id or proof.get('run_id') != execution.get('run_id')
            or proof.get('task_key') != execution.get('task_id') or proof.get('recipe_id') != step['recipe_id']
            or proof.get('status') != 'completed' or proof.get('artifact_status') != 'valid'
            or not proof.get('result_ref') or not proof.get('task_type') or not proof.get('job_id')
            or any(native_execution.get(key) != execution.get(key) for key in
                ('draft_id', 'draft_revision', 'canvas_id', 'workflow_instance_id', 'plan_node_id', 'run_id', 'task_id'))):
        return None
    task = {'run_id': proof['run_id'], 'node_id': node_id, 'task_key': proof['task_key']}
    resource = receipt.get('resource') or {}
    if (settlement_key(scope, task, {}) != settlement_id
            or receipt.get('result_key') != settlement_key(scope,
                {**task, 'task_type': proof['task_type'], 'job_id': proof['job_id']},
                {'revision': proof.get('node_revision'), 'result_ref': proof['result_ref'], 'media_hash': resource.get('media_hash')})):
        return None
    origin_ref = receipt.get('authorization_work_ref') or {}
    if origin_ref.get('artifact_id') != scope['work_id']:
        return None
    origin = store.read(origin_ref['artifact_id'], origin_ref['revision'])
    current = store.read(exact_work_ref['artifact_id'], exact_work_ref['revision'])
    original_step = next((s for s in origin['payload']['steps'] if s['id'] == step['id']), None)
    current_step = next((s for s in current['payload']['steps'] if s['id'] == step['id']), None)
    if (origin['schema'] != 'material_work_package.v1' or current['schema'] != origin['schema']
            or not original_step or not current_step or current_step['status'] in {'stale', 'rejected'}
            or (current_step.get('node_id') and current_step['node_id'] != node_id)
            or any((current_step.get('execution') or {}).get(key, execution.get(key)) != execution.get(key)
                for key in ('draft_id', 'draft_revision', 'canvas_id', 'workflow_instance_id', 'plan_node_id', 'run_id', 'task_id'))
            or material_step_target(original_step) != material_step_target(step)
            or material_step_target(current_step) != material_step_target(step)
            or current['payload']['plan_ref'] != origin['payload']['plan_ref']
            or store.read_latest(origin['payload']['plan_ref']['artifact_id'])['revision'] != origin['payload']['plan_ref']['revision']
            or [r for r in current['payload']['requirements'] if r['final_step_id'] == step['id']]
                != [r for r in origin['payload']['requirements'] if r['final_step_id'] == step['id']]
            or any((original_step.get('execution') or {}).get(key) != execution.get(key) for key in
                ('draft_id', 'draft_revision', 'canvas_id', 'workflow_instance_id', 'plan_node_id'))
            or canvas_material_resource(store, canvas, node_id, step['media_kind']) != receipt.get('resource')):
        return None
    from novelvideo.freezone.workflow_runs import read_workflow_run
    run = read_workflow_run(project_dir=workflow_state_dir if workflow_state_dir is not None else store.project_dir,
        canvas_id=execution['canvas_id'], run_id=proof['run_id'])
    if run and (run.get('project_id') != store.project_id or run.get('status') == 'cancelled'):
        return None
    return {**execution, 'run_id': proof['run_id'], 'task_id': proof['task_key']}


def validate_material_native_execution(store, canvas: dict, step: dict, node_id: str, *, require_complete: bool = False, workflow_state_dir: Path | None = None, settlement_id: str | None = None, work_ref: dict | None = None) -> dict:
    from novelvideo.freezone.workflow_drafts import read_workflow_draft
    execution = step.get('execution')
    if (not isinstance(execution, dict)
            or any(not isinstance(execution.get(key), str) or not execution[key].strip()
                   for key in ('canvas_id', 'draft_id', 'workflow_instance_id', 'plan_node_id'))
            or type(execution.get('draft_revision')) is not int or execution['draft_revision'] < 1):
        raise ValueError(
            'material generation has no complete native execution receipt; use '
            'freezone_prepare_material_work or freezone_prepare_material_stage and confirm '
            'the native workflow, or prepare separately generated current-canvas media as '
            'source=existing with its actual node_id and review it'
        )
    draft, error = read_workflow_draft(project_dir=workflow_state_dir if workflow_state_dir is not None else store.project_dir, canvas_id=execution['canvas_id'], draft_id=execution['draft_id'])
    if draft and (draft.get('status') != 'confirmed' or draft.get('project_id') != store.project_id
            or draft.get('canvas_id') != execution['canvas_id'] or draft.get('revision') != execution['draft_revision']):
        raise ValueError('material generation requires a user confirmed native workflow')
    if work_ref is not None:
        _exact_ref(work_ref)
    node = next((n for n in canvas.get('nodes',[]) if n.get('id') == node_id),None)
    data = node.get('data',{}) if node else {}
    if data.get('workflowInstanceId') != execution['workflow_instance_id'] or data.get('workflowPlanNodeId') != execution['plan_node_id']:
        raise ValueError('material node differs from approved native workflow step')
    if error or not draft:
        recovered = _settled_complete_execution(store, canvas, step, node_id, execution, settlement_id, work_ref, workflow_state_dir)
        if recovered:
            return recovered
        raise ValueError('material generation requires a user confirmed native workflow')
    if require_complete:
        from novelvideo.freezone.workflow_runs import list_workflow_runs
        runs = list_workflow_runs(project_dir=workflow_state_dir if workflow_state_dir is not None else store.project_dir,canvas_id=execution['canvas_id'],limit=200)
        matches = [(run,action) for run in runs if run.get('project_id') == store.project_id
                   and (not execution.get('run_id') or run['run_id'] == execution['run_id'])
                   for action in run.get('actions',[])
                   if action.get('node_id') == node_id and action.get('status') == 'completed'
                   and action.get('artifact_status') == 'valid' and action.get('recipe_id') == step['recipe_id']
                   and action.get('task_key') and action.get('result_ref')
                   and (not execution.get('task_id') or action['task_key'] == execution['task_id'])]
        if not matches:
            recovered = _settled_complete_execution(store, canvas, step, node_id, execution, settlement_id, work_ref, workflow_state_dir)
            if recovered:
                return recovered
        if len(matches) != 1:
            raise ValueError('material candidate requires a verified completed native task result')
        run, action = matches[0]
        field = {'image':'imageUrl','video':'videoUrl','audio':'audioUrl'}[step['media_kind']]
        current_url = data.get(field) or data.get('previewImageUrl')
        proof = action['result_ref']
        value = proof.get('value')
        if value is not None and value != current_url:
            raise ValueError('canvas candidate differs from native task output')
        if proof.get('project_path'):
            resource = canvas_material_resource(store,canvas,node_id,step['media_kind'])
            if (store.project_dir / proof['project_path']).resolve() != (store.project_dir / resource['media_path']).resolve():
                raise ValueError('canvas candidate differs from native task output')
        return {**execution,'run_id':run['run_id'],'task_id':action['task_key']}
    return execution


async def inspect_material_content(project_dir, resource: dict, step: dict, requirements: list[dict]) -> dict:
    """Bounded actual image/three video frame inspection; audio requests user review."""
    import asyncio
    import json
    import tempfile
    import subprocess
    from pathlib import Path
    from novelvideo.freezone.vision_gateway import load_compact_vision_inputs, call_freezone_vision_model
    kind = resource['media_kind']
    unavailable = {'status':'unavailable','requires_user_review':True,'media_hash':resource['media_hash']}
    if kind == 'audio':
        return {**unavailable,'reason':'Audio content inspection unavailable; listen and confirm the complete source.'}
    path = (Path(project_dir) / resource['media_path']).resolve()
    if not path.is_relative_to(Path(project_dir).resolve()) or not path.is_file():
        return {**unavailable,'reason':'Current project media unavailable.'}
    try:
        with tempfile.TemporaryDirectory(prefix='material-observation-') as directory:
            paths = [path]
            times = []
            if kind == 'video':
                duration = resource['duration_us'] / 1_000_000
                times = [0.0,duration / 2,max(0,duration - min(0.1,duration / 10))]
                paths = []
                for index, time in enumerate(times):
                    target = Path(directory) / f'frame-{index}.jpg'
                    await asyncio.to_thread(subprocess.run,
                        ['ffmpeg','-v','error','-nostdin','-ss',str(time),'-i',str(path),'-frames:v','1',
                         '-vf','scale=640:640:force_original_aspect_ratio=decrease',str(target)],
                        capture_output=True,timeout=10,check=True)
                    paths.append(target)
            images = await load_compact_vision_inputs(paths)
            targets = {'target': step.get('prompt','')[:6000] or step.get('label','')}
            criteria = [text for requirement in requirements if requirement.get('final_step_id') == step.get('id')
                        for text in [requirement.get('description',''), *requirement.get('acceptance',[])]]
            targets.update({f'acceptance_{index}':text[:2000] for index,text in enumerate(criteria) if text})
            prompt = ('Inspect the actual candidate images against every target below. '
                      'Respond ONLY JSON with status passed|rejected|unavailable, findings as string array, '
                      'checked_items as array of target IDs actually checked. Pass only if ALL targets are observable and met. '
                      'Any speech, motion, continuity, or uncertain detail unprovable from three sampled frames is unavailable. '
                      'Reject visible mismatches. Never infer quality from task completion. Targets: ' + json.dumps(targets,ensure_ascii=False))
            model, response = await asyncio.wait_for(call_freezone_vision_model(prompt=prompt,images=images,timeout_seconds=35),timeout=40)
            parsed = json.loads(response)
            if (not isinstance(parsed,dict) or set(parsed) != {'status','findings','checked_items'}
                    or parsed['status'] not in {'passed','rejected','unavailable'}
                    or not isinstance(parsed['findings'],list) or len(parsed['findings']) > 100
                    or any(not isinstance(v,str) or len(v)>2000 for v in parsed['findings'])
                    or not isinstance(parsed['checked_items'],list) or len(parsed['checked_items']) > 100
                    or any(not isinstance(v,str) for v in parsed['checked_items'])):
                raise ValueError('invalid structured content observation')
            status = parsed['status']
            if status == 'passed' and set(parsed['checked_items']) != set(targets):
                status = 'unavailable'
            critical = any(r.get('role') in {'identity','product','voice'} for r in step.get('references',[]))
            if critical and status == 'passed':
                status = 'unavailable'
                parsed['findings'].append('Critical identity/product/voice reference needs user confirmation.')
            return {'status':status,'requires_user_review':status == 'unavailable','media_hash':resource['media_hash'],
                    'model':model,'findings':parsed['findings'],'checked_items':parsed['checked_items'],
                    'required_items':list(targets),'sample_times_seconds':times,'host_observed':True,
                    'scope':'image' if kind == 'image' else 'three_video_frames'}

    except Exception as exc:
        return {**unavailable,'reason':f'Actual content inspection unavailable ({type(exc).__name__}); view and confirm current media.'}
