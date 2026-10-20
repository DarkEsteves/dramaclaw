"""Native artifact references in chat; presentation always re-reads authorized facts.

Publication is only called after native writes. Reads never publish, poll, or
walk historical revisions. A chat snapshot cannot confer approval or access.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from itertools import islice
from pathlib import Path

from .production_inventory import _artifact, _commission, _ref
from .store import RemixArtifactStore

LABELS = {'reference_decision.v1': '制作选择', 'material_work_package.v1': '素材清单',
          'material_review.v1': '素材审看', 'production_output_review.v1': '成片审看',
          'speech_correction_preview.v1': '实际语音校对'}
DECISIONS = {'direction': '复刻方向', 'plan': '作品方案', 'material': '素材选择', 'composition': '可编辑合成'}
MAX_CARDS = 20


def _authorize(store, canvas, canvas_id, requester, agent_id, runs, commission_id):
    if (canvas.get('project_id'), canvas.get('canvas_id'), store.created_by) != (store.project_id, canvas_id, requester):
        raise PermissionError('card scope differs')
    if canvas.get('access_model') in {'private', 'user_private'} and canvas.get('owner_principal_id') != requester:
        raise PermissionError('private canvas access denied')
    commission = _commission(store, commission_id, canvas)
    ancestor = commission
    for _ in range(8):
        if any((run.get('project_id'), run.get('canvas_id'), run.get('requester_user_id'), run.get('agent_id') or 'main',
                (run.get('commission_ref') or {}).get('artifact_id'), run.get('analysis_node_id'))
               == (store.project_id, canvas_id, requester, agent_id, ancestor['commission_ref']['artifact_id'], commission['analysis_node_id']) for run in runs):
            return commission
        parent = ancestor.get('parent_commission_ref')
        if not parent:
            break
        owning_parent = _commission(store, parent['artifact_id'], canvas)
        if not any((row.get('commission_ref') or {}).get('artifact_id') == ancestor['commission_ref']['artifact_id'] for row in owning_parent.get('plan_branches', [])):
            raise PermissionError('card branch is not registered by its parent')
        ancestor = owning_parent
    raise PermissionError('card has no authorized Agent/run association')


def _belongs(store, saved, c):
    p, schema = saved['payload'], saved['schema']
    if schema in {'reference_decision.v1', 'production_output_review.v1'}:
        return (p.get('commission_ref') or {}).get('artifact_id') == c['commission_ref']['artifact_id']
    plans = {(c.get('plan_ref') or {}).get('artifact_id')}
    plans.update((row.get('plan_ref') or {}).get('artifact_id') for row in c.get('decisions', []))
    if schema == 'material_review.v1':
        work = _artifact(store, p['work_ref']['artifact_id'])
        return work['schema'] == 'material_work_package.v1' and work['payload']['plan_ref']['artifact_id'] in plans
    if schema == 'material_work_package.v1':
        return p['plan_ref']['artifact_id'] in plans
    if schema == 'speech_correction_preview.v1':
        return p['source_plan_ref']['artifact_id'] in plans
    return False


def _speech_applied(store, preview):
    plan = _artifact(store, preview['payload']['source_plan_ref']['artifact_id'])
    if not any(ref['artifact_id'] == preview['id'] and ref['revision'] == preview['revision'] for ref in plan.get('upstream_refs', [])):
        return False
    for ref in plan.get('upstream_refs', [])[:100]:
        saved = store._read(ref['artifact_id'], ref['revision'])
        proof = saved['payload']
        if saved['schema'] == 'speech_correction_review.v1' and proof.get('actor_kind') == 'user' and proof.get('actor_id') == store.created_by and proof.get('accepted') is True and (proof.get('preview_ref') or {}).get('artifact_id') == preview['id'] and (proof.get('preview_ref') or {}).get('revision') == preview['revision']:
            return True
    return False


def _card(store, original, head, c, scope):
    p = head['payload']
    status, reason = 'recorded', '历史记录；操作以当前作品方案为准。'
    title, summary = LABELS[head['schema']], ''
    if head['schema'] == 'reference_decision.v1':
        title, summary = DECISIONS[p['kind']], p['summary']
        if p['status'] == 'confirmed':
            receipt = p.get('receipt_id')
            receipt_matches = any(row.get('message_id') == receipt and
                (row.get('project_id'), row.get('canvas_id'), row.get('requester_user_id')) ==
                (scope['project_id'], scope['canvas_id'], store.created_by) for row in c.get('decisions', []))
            if p['kind'] == 'material':
                receipt_matches = any(ref.get('artifact_id') != p['commission_ref']['artifact_id'] and
                    (review := _artifact(store, ref['artifact_id']))['schema'] == 'material_review.v1' and
                    (review['payload'].get('user_decision') or {}).get('requester_user_id') == store.created_by and
                    (review['payload'].get('user_decision') or {}).get('canvas_id') == scope['canvas_id']
                    for ref in head.get('upstream_refs', []))
            if p.get('decided_by') != store.created_by or not receipt or not receipt_matches:
                raise PermissionError('card confirmation has no native user receipt')
            status, reason = 'confirmed', '已由用户确认；历史卡片不可再次提交。'
        elif p['status'] == 'superseded':
            status, reason = 'stale', '复刻方向已更新；请以最新对话提议为准。' if p['kind'] == 'direction' else '制作选择已更新；请核对当前卡片。'
        else:
            status = 'pending' if p['commission_ref'] == c['commission_ref'] else 'stale'
            reason = ('请在对话中回复确认或修改意见。' if p['kind'] == 'direction' else '请使用当前操作卡片确认。') if status == 'pending' else '作品方案版本已变化；此卡片不可确认。'
    elif head['schema'] == 'speech_correction_preview.v1':
        if _speech_applied(store, head):
            status, reason = 'confirmed', '已由用户校对并应用；原始语音证据已保留。'
        elif store._latest_revision(p['source_plan_ref']['artifact_id']) != p['source_plan_ref']['revision']:
            status, reason = 'stale', '作品方案版本已变化；此校对记录不可再次提交。'
        else:
            status, reason = 'pending', '请在当前语音校对卡片核对并应用。'
    elif head['schema'] in {'material_review.v1', 'production_output_review.v1'}:
        proof = p.get('user_decision') or p.get('decision_receipt') or {}
        passed = p.get('content', {}).get('status') == 'passed'
        if passed and (proof.get('project_id'), proof.get('canvas_id'), proof.get('requester_user_id')) == (store.project_id, scope['canvas_id'], store.created_by):
            status, reason = 'confirmed', '已由用户审看确认；历史记录不可再次提交。'
        else:
            status, reason = 'recorded', '审看记录；当前确认以当前素材和版本为准。'
    return {'card_id': original['id'], 'ref': _ref(head, store.project_id), 'schema': head['schema'],
            'commission_ref': c['commission_ref'], 'title': title, 'summary': summary, 'status': status, 'reason': reason,
            **({'decision_kind': p['kind']} if head['schema'] == 'reference_decision.v1' else {})}


def read_conversation_cards(store, *, canvas, canvas_id, requester_user_id, agent_id, runs, commission_id, refs):
    if not 1 <= len(refs) <= MAX_CARDS:
        raise ValueError('card read requires 1 to 20 exact references')
    scope = {'project_id': store.project_id, 'canvas_id': canvas_id, 'agent_id': agent_id}
    with store._locked():
        c = _authorize(store, canvas, canvas_id, requester_user_id, agent_id, runs, commission_id)
        items = []
        for ref in refs:
            if ref.get('project_id', store.project_id) != store.project_id or type(ref.get('revision')) is not int or ref['revision'] < 1:
                raise PermissionError('card reference scope differs')
            original = store._read(ref['artifact_id'], ref['revision'])
            head = _artifact(store, original['id'])
            if original['created_by'] != requester_user_id or head['schema'] not in LABELS or not _belongs(store, original, c) or not _belongs(store, head, c):
                raise PermissionError('card does not belong to the requested commission')
            items.append(_card(store, original, head, c, scope))
        return {'scope': scope, 'commission_ref': c['commission_ref'], 'items': items}


async def publish_conversation_cards(ctx, *, canvas_id, runs, chat=None):
    from novelvideo.chat.store import ChatScope, chat_store
    from novelvideo.freezone import canvas_store
    from novelvideo.chat.live_events import broadcast_chat_scope_event
    chat = chat or chat_store
    store = RemixArtifactStore(Path(ctx.output_dir), ctx.project_id, created_by=ctx.requester_user_id)
    def references(paths):
        with canvas_store.get_canvas_write_mutex().write_mutex(ctx.state_dir, canvas_id) as guard:
            canvas = canvas_store.read_canvas(ctx.state_dir, canvas_id)
            if not canvas:
                return []
            result = []
            with store._locked():
                heads = []
                for directory in paths:
                    if directory.is_symlink() or not directory.is_dir() or directory.name.startswith('_'):
                        continue
                    try:
                        header = store._read_json(store._path(directory.name, store._latest_revision(directory.name), '.json'))
                    except (ValueError, FileNotFoundError):
                        continue
                    if header.get('schema') in LABELS and header.get('project_id') == store.project_id and header.get('created_by') == store.created_by:
                        heads.append(header)
                for run in runs[:20]:
                    if not run.get('commission_ref'):
                        continue
                    agent = run.get('agent_id') or 'main'
                    try:
                        c = _authorize(store, canvas, canvas_id, ctx.requester_user_id, agent, runs, run['commission_ref']['artifact_id'])
                    except (PermissionError, ValueError, FileNotFoundError):
                        continue
                    commissions = [c]
                    for row in c.get('plan_branches', [])[:20]:
                        try:
                            commissions.append(_authorize(store, canvas, canvas_id, ctx.requester_user_id, agent, runs, row['commission_ref']['artifact_id']))
                        except (PermissionError, ValueError, FileNotFoundError):
                            continue
                    for branch in commissions:
                        for head in heads:
                            if head['schema'] not in LABELS:
                                continue
                            saved = _artifact(store, head['id'])
                            if saved['schema'] == 'material_work_package.v1':
                                continue
                            if (saved['schema'] == 'reference_decision.v1'
                                    and saved['payload']['kind'] in {'direction', 'plan'}):
                                continue
                            if not _belongs(store, saved, branch):
                                continue
                            event = {'type': 'reference.production.card', 'project_id': ctx.project_id, 'canvas_id': canvas_id,
                                'agent_id': agent, 'commission_ref': branch['commission_ref'], 'ref': _ref(saved, ctx.project_id), 'schema': saved['schema']}
                            result.append((agent, event))
            guard.reassert()
            return result
    async def publish_batch(events):
        for agent, event in events:
            identity = hashlib.sha256(f"{ctx.requester_user_id}:{ctx.project_id}:{canvas_id}:{agent}:{event['commission_ref']['artifact_id']}:{event['ref']['artifact_id']}".encode()).hexdigest()[:32]
            turn = 'reference-card-' + identity
            wire_scope = ChatScope(kind='project', id=ctx.project_id, surface='freezone', canvas_id=canvas_id, agent_id=agent)
            storage_scope = ChatScope(kind='project', id=getattr(ctx, 'project_name', ctx.project_id), surface='freezone', canvas_id=canvas_id, agent_id=agent, state_dir=str(ctx.state_dir))
            snapshot = {'type':'assistant.message_parts','parts':[{'id':identity,'type':'reference_production','event':event}]}
            def already_published():
                connection = chat.connect(ctx.requester_username, storage_scope)
                try:
                    saved = connection.execute("SELECT payload_json FROM chat_ui_events WHERE turn_id=? AND event_type='assistant.message_parts' ORDER BY id DESC LIMIT 1", (turn,)).fetchone()
                    return bool(saved and json.loads(saved['payload_json']) == snapshot)
                finally:
                    connection.close()
            if await asyncio.to_thread(already_published):
                continue
            await chat.append_message_async(ctx.requester_username, storage_scope, 'assistant', '', turn_id=turn, idempotency_key=turn)
            await chat.append_ui_event_async(ctx.requester_username, storage_scope, turn, snapshot)
            await broadcast_chat_scope_event(username=ctx.requester_username, scope=wire_scope, payload={'type':'reference.production.card','scope':wire_scope.to_dict(),'turn_id':turn,'event':event})
    # No fixed first-page cutoff: each native call drains bounded head batches.
    # A restart replays the scan, skipping exact snapshots already persisted.
    entries = iter(store.root.iterdir()) if store.root.exists() else iter(())
    while paths := list(islice(entries, MAX_CARDS)):
        await publish_batch(await asyncio.to_thread(references, paths))
