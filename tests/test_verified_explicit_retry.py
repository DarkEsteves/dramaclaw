"""Only an explicit, proof-checked retry may revive the exact stale record."""
from copy import deepcopy
from contextlib import contextmanager
import json
import time

import pytest

from test_reference_remix_continuation import (
    context, legacy_joint_proof, ready_run, service,
)
from test_api_reference_analysis_runs import client
from novelvideo.chat import reference_remix_continuation as continuation
from novelvideo.freezone import canvas_store
from novelvideo.freezone.canvas_lock import CanvasLockBusy, canvas_write_lock
from novelvideo.ports.local.canvas_mutex import FileLockCanvasWriteMutex


def persist_stale(service, run):
    run.update(status='stale', error='Previously stale', message='Needs verification')
    assert service.store.update_owned(run, service.owner_token)
    service.store.release(run['run_id'], service.owner_token)
    return service.store.get(run['run_id'])


async def stale_completed(service, tmp_path, monkeypatch):
    ctx = context(tmp_path)
    run, task = await ready_run(service, ctx, monkeypatch)
    saved = await service._save_analysis(ctx, run, result=task.result)
    run.update(result_fingerprint=continuation._node_fingerprint(saved['nodes'][1]),
               completed_analysis_node_snapshot=deepcopy(saved['nodes'][1]))
    return ctx, persist_stale(service, run), task


def stored_row(service, run_id):
    with service.store._connect() as connection:
        return dict(connection.execute('SELECT * FROM reference_remix_continuations WHERE run_id=?', (run_id,)).fetchone())


def replace_record(service, run, **changes):
    run = {**run, **changes}
    with service.store._connect() as connection:
        connection.execute('UPDATE reference_remix_continuations SET payload_json=? WHERE run_id=?',
                           (json.dumps(run, ensure_ascii=False), run['run_id']))


@pytest.mark.asyncio
async def test_explicit_retry_reuses_intact_stale_completed_proof_and_preserves_all_pins(service, tmp_path, monkeypatch):
    ctx, run, _ = await stale_completed(service, tmp_path, monkeypatch)
    path = canvas_store.canvas_path(ctx.state_dir, 'canvas')
    canvas_before = path.read_bytes()
    wakes, broadcasts = [], []
    monkeypatch.setattr(service, '_ensure_worker', lambda value: wakes.append(value))
    async def broadcast(value): broadcasts.append(value)
    monkeypatch.setattr(service, '_broadcast', broadcast)
    result = await service.retry(ctx=ctx, run_id=run['run_id'])
    current = service.store.get(run['run_id'])
    assert result['status'] == current['status'] == 'analysis_ready'
    assert current['resume_attempt'] == int(run.get('resume_attempt', 0)) + 1
    for key in ('source_fingerprint', 'analysis_fingerprint', 'result_fingerprint', 'edge_fingerprint',
                'source_node_snapshot', 'analysis_node_snapshot', 'completed_analysis_node_snapshot',
                'task_id', 'task_key', 'job_id', 'idempotency_key', 'commission_ref'):
        assert current.get(key) == run.get(key)
    assert path.read_bytes() == canvas_before
    assert len(wakes) == len(broadcasts) == 1


@pytest.mark.asyncio
async def test_stale_source_must_be_restored_before_explicit_retry(service, tmp_path, monkeypatch):
    ctx, run, _ = await stale_completed(service, tmp_path, monkeypatch)
    path = canvas_store.canvas_path(ctx.state_dir, 'canvas')
    original = path.read_bytes()
    changed = json.loads(original)
    changed['nodes'][0]['data']['videoUrl'] = '/replacement.mp4'
    path.write_text(json.dumps(changed))
    before = stored_row(service, run['run_id'])
    with pytest.raises(continuation.StaleReference):
        await service.retry(ctx=ctx, run_id=run['run_id'])
    assert stored_row(service, run['run_id']) == before
    path.write_bytes(original)
    assert service.store.get(run['run_id'])['status'] == 'stale'
    assert service.store.retry(ctx, run['run_id']) is None
    result = await service.retry(ctx=ctx, run_id=run['run_id'])
    assert result['status'] == 'analysis_ready'


@pytest.mark.asyncio
@pytest.mark.parametrize('tamper', ['source', 'result', 'edge', 'missing_canvas', 'missing_analysis',
                                    'missing_proof', 'forged_snapshot', 'missing_task_id', 'failed_task', 'task_actor'])
async def test_changed_missing_or_forged_completed_proof_cannot_retry(service, tmp_path, monkeypatch, tamper):
    ctx, run, task = await stale_completed(service, tmp_path, monkeypatch)
    path = canvas_store.canvas_path(ctx.state_dir, 'canvas')
    payload = json.loads(path.read_text())
    if tamper == 'source': payload['nodes'][0]['data']['videoUrl'] = '/replacement.mp4'
    elif tamper == 'result': payload['nodes'][1]['data']['rawResult']['video_story']['title'] = 'edited'
    elif tamper == 'edge': payload['edges'] = []
    elif tamper == 'missing_canvas': path.unlink()
    elif tamper == 'missing_analysis': payload['nodes'] = payload['nodes'][:1]
    elif tamper == 'missing_proof':
        run.pop('completed_analysis_node_snapshot')
        monkeypatch.setattr(continuation, 'get_task_manager', lambda: type('Missing', (), {'get_task_for_project': lambda *_a, **_k: None})())
        replace_record(service, run)
    elif tamper == 'forged_snapshot':
        run['completed_analysis_node_snapshot']['data']['rawResult']['video_story']['title'] = 'forged'
        monkeypatch.setattr(continuation, 'get_task_manager', lambda: type('Missing', (), {'get_task_for_project': lambda *_a, **_k: None})())
        replace_record(service, run)
    elif tamper == 'missing_task_id':
        replace_record(service, run, task_id=None)
    elif tamper == 'failed_task': task.status = 'failed'
    elif tamper == 'task_actor': task.requester_user_id = 'other'
    if tamper != 'missing_canvas': path.write_text(json.dumps(payload))
    before = stored_row(service, run['run_id'])
    wakes = []
    monkeypatch.setattr(service, '_ensure_worker', lambda value: wakes.append(value))
    with pytest.raises(continuation.StaleReference):
        await service.retry(ctx=ctx, run_id=run['run_id'])
    assert stored_row(service, run['run_id']) == before
    assert not wakes


@pytest.mark.asyncio
@pytest.mark.parametrize('tamper', [None, 'operation_task', 'receipt', 'media', 'result_pin'])
async def test_legacy_completed_receipt_must_still_prove_original_result_before_retry(service, tmp_path, monkeypatch, tamper):
    ctx, run, operations, artifact, _ = await legacy_joint_proof(service, tmp_path, monkeypatch)
    run = persist_stale(service, run)
    if tamper == 'operation_task':
        record = operations.get(run['operation_id'])
        record['task_id'] = 'wrong'
        operations._path(run['operation_id']).write_text(json.dumps(record))
    elif tamper == 'receipt':
        from novelvideo.freezone.remix.store import RemixArtifactStore
        path = RemixArtifactStore(ctx.output_dir, ctx.project_id)._path(artifact['id'], artifact['revision'], '.payload.json')
        receipt = json.loads(path.read_text())
        receipt['story']['video_story']['summary'] = 'forged'
        path.write_text(json.dumps(receipt))
    elif tamper == 'media': (ctx.output_dir/'video.mp4').write_bytes(b'replaced')
    elif tamper == 'result_pin': replace_record(service, run, result_fingerprint='forged')
    before = stored_row(service, run['run_id'])
    if tamper is None:
        result = await service.retry(ctx=ctx, run_id=run['run_id'])
        assert result['status'] == 'analysis_ready'
        assert service.store.get(run['run_id'])['result_fingerprint'] == run['result_fingerprint']
    else:
        with pytest.raises(continuation.StaleReference):
            await service.retry(ctx=ctx, run_id=run['run_id'])
        assert stored_row(service, run['run_id']) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('scope', ['actor', 'project', 'private', 'resolved_actor', 'storage'])
async def test_stale_retry_resolves_actual_scope_and_preserves_access(service, tmp_path, monkeypatch, scope):
    ctx, run, _ = await stale_completed(service, tmp_path, monkeypatch)
    before = stored_row(service, run['run_id'])
    if scope in {'actor', 'project'}:
        other = context(tmp_path, actor='other' if scope == 'actor' else 'actor', project='other' if scope == 'project' else 'project')
        assert await service.retry(ctx=other, run_id=run['run_id']) is None
    else:
        if scope == 'private':
            path = canvas_store.canvas_path(ctx.state_dir, 'canvas')
            payload = json.loads(path.read_text())
            payload.update(access_model='private', owner_principal_id='other')
            path.write_text(json.dumps(payload))
            expected = PermissionError
        else:
            async def resolve(**kwargs):
                return context(tmp_path, actor='other') if scope == 'resolved_actor' else context(tmp_path/'different')
            monkeypatch.setattr(continuation, 'resolve_project_context', resolve)
            expected = continuation.StaleReference
        with pytest.raises(expected):
            await service.retry(ctx=ctx, run_id=run['run_id'])
    assert stored_row(service, run['run_id']) == before


@pytest.mark.asyncio
async def test_store_cannot_retry_stale_without_a_trusted_verified_record(service, tmp_path, monkeypatch):
    ctx, run, _ = await stale_completed(service, tmp_path, monkeypatch)
    before = stored_row(service, run['run_id'])
    assert service.store.retry(ctx, run['run_id']) is None
    assert stored_row(service, run['run_id']) == before
    for fabricated in (True, {}, continuation._VerifiedStaleRetry(()),
                       continuation._VerifiedStaleRetry(tuple(before.items()))):
        assert service.store.retry(ctx, run['run_id'], verified_stale=fabricated) is None
        assert stored_row(service, run['run_id']) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['payload', 'raw_payload', 'status', 'project_id', 'requester_user_id',
                                    'canvas_id', 'agent_id', 'idempotency_key', 'lease_owner', 'expired_lease'])
async def test_retry_full_record_cas_rejects_changes_after_proof_validation(service, tmp_path, monkeypatch, change):
    ctx, run, _ = await stale_completed(service, tmp_path, monkeypatch)
    original = service.store.retry
    before = []
    def race(*args, **kwargs):
        with service.store._connect() as connection:
            if change == 'payload':
                value = service.store.get(run['run_id'])
                value['unrelated_concurrent_change'] = True
                connection.execute('UPDATE reference_remix_continuations SET payload_json=? WHERE run_id=?', (json.dumps(value), run['run_id']))
            elif change == 'raw_payload': connection.execute("UPDATE reference_remix_continuations SET payload_json=payload_json || ' ' WHERE run_id=?", (run['run_id'],))
            elif change == 'status': connection.execute("UPDATE reference_remix_continuations SET status='resume_failed' WHERE run_id=?", (run['run_id'],))
            elif change in {'project_id', 'requester_user_id', 'canvas_id', 'agent_id', 'idempotency_key'}:
                connection.execute(f"UPDATE reference_remix_continuations SET {change}='other' WHERE run_id=?", (run['run_id'],))
            elif change == 'lease_owner': connection.execute('UPDATE reference_remix_continuations SET lease_owner=?,lease_until=? WHERE run_id=?', ('other-worker', time.time()+100, run['run_id']))
            else: connection.execute('UPDATE reference_remix_continuations SET lease_owner=?,lease_until=? WHERE run_id=?', ('expired-worker', time.time()-100, run['run_id']))
        before.append(stored_row(service, run['run_id']))
        return original(*args, **kwargs)
    monkeypatch.setattr(service.store, 'retry', race)
    wakes = []
    monkeypatch.setattr(service, '_ensure_worker', lambda value: wakes.append(value))
    await service.retry(ctx=ctx, run_id=run['run_id'])
    assert stored_row(service, run['run_id']) == before[0]
    assert not wakes


@pytest.mark.asyncio
async def test_retry_refuses_live_lease_and_reading_stale_does_not_autostart(service, tmp_path, monkeypatch):
    ctx, run, _ = await stale_completed(service, tmp_path, monkeypatch)
    with service.store._connect() as connection:
        connection.execute('UPDATE reference_remix_continuations SET lease_owner=?,lease_until=? WHERE run_id=?', ('live-worker', time.time()+100, run['run_id']))
    before = stored_row(service, run['run_id'])
    wakes = []
    monkeypatch.setattr(service, '_ensure_worker', lambda value: wakes.append(value))
    assert (await service.get(ctx=ctx, run_id=run['run_id']))['status'] == 'stale'
    assert (await service.list(ctx=ctx, canvas_id='canvas'))[0]['status'] == 'stale'
    await service.retry(ctx=ctx, run_id=run['run_id'])
    assert stored_row(service, run['run_id']) == before
    assert not wakes


def test_stale_retry_endpoint_keeps_canvas_scope_and_read_only_agent_auth(client, monkeypatch):
    from novelvideo.api.routes import reference_analysis as routes
    from novelvideo.api.auth import get_api_user

    calls = []
    async def get(**kwargs):
        return {'run_id': 'run', 'canvas_id': 'other-canvas', 'status': 'stale'}
    async def retry(**kwargs):
        calls.append(kwargs)
    monkeypatch.setattr(routes.coordinator, 'get', get)
    monkeypatch.setattr(routes.coordinator, 'retry', retry)
    path = '/api/v1/projects/project-a/freezone/canvases/canvas-a/reference-analysis-runs/run/retry'
    assert client.post(path).status_code == 404
    assert not calls
    client.app.dependency_overrides[get_api_user] = lambda: {
        'id': 'user-a', 'username': 'alice', 'credential_kind': 'agent_session',
        'current_scope_kind': 'project', 'current_project_id': 'project-a', 'scopes': ['agent:read'],
    }
    assert client.post(path).status_code == 403
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['source', 'result', 'edge'])
async def test_native_canvas_change_after_original_proof_is_rejected_before_retry_commit(service, tmp_path, monkeypatch, change):
    ctx, run, _ = await stale_completed(service, tmp_path, monkeypatch)
    original = service._assert_current
    changed_once = []
    def change_after_proof(*args, **kwargs):
        payload = original(*args, **kwargs)
        if not changed_once:
            changed_once.append(True)
            changed = deepcopy(payload)
            if change == 'source': changed['nodes'][0]['data']['videoUrl'] = '/replacement.mp4'
            elif change == 'result': changed['nodes'][1]['data']['rawResult']['video_story']['title'] = 'edited'
            else: changed['edges'] = []
            changed['revision'] += 1
            canvas_store.save_canvas(ctx.state_dir, 'canvas', base_revision=payload['revision'],
                                     build_payload=lambda _old: changed)
        return payload
    monkeypatch.setattr(service, '_assert_current', change_after_proof)
    before = stored_row(service, run['run_id'])
    wakes = []
    monkeypatch.setattr(service, '_ensure_worker', lambda value: wakes.append(value))
    with pytest.raises(continuation.StaleReference):
        await service.retry(ctx=ctx, run_id=run['run_id'])
    assert stored_row(service, run['run_id']) == before
    assert not wakes
    with canvas_write_lock(ctx.state_dir, 'canvas', timeout_seconds=.02):
        pass


@pytest.mark.asyncio
async def test_retry_canvas_lock_busy_never_commits_or_wakes(service, tmp_path, monkeypatch):
    ctx, run, _ = await stale_completed(service, tmp_path, monkeypatch)
    class FastMutex(FileLockCanvasWriteMutex):
        @contextmanager
        def write_mutex(self, project_dir, canvas_id, **kwargs):
            with super().write_mutex(project_dir, canvas_id, timeout_seconds=.02) as guard:
                yield guard
    monkeypatch.setattr(canvas_store, 'get_canvas_write_mutex', lambda: FastMutex())
    before = stored_row(service, run['run_id'])
    wakes = []
    monkeypatch.setattr(service, '_ensure_worker', lambda value: wakes.append(value))
    with canvas_write_lock(ctx.state_dir, 'canvas'):
        with pytest.raises(ValueError, match='画布正在保存'):
            await service.retry(ctx=ctx, run_id=run['run_id'])
    assert stored_row(service, run['run_id']) == before
    assert not wakes
    with canvas_write_lock(ctx.state_dir, 'canvas', timeout_seconds=.02):
        pass


@pytest.mark.asyncio
async def test_retry_holds_native_canvas_lock_during_sql_cas_and_releases_after_commit(service, tmp_path, monkeypatch):
    ctx, run, _ = await stale_completed(service, tmp_path, monkeypatch)
    original = service.store.retry
    def check_lock(*args, **kwargs):
        with pytest.raises(CanvasLockBusy):
            with canvas_write_lock(ctx.state_dir, 'canvas', timeout_seconds=.02):
                pass
        return original(*args, **kwargs)
    monkeypatch.setattr(service.store, 'retry', check_lock)
    result = await service.retry(ctx=ctx, run_id=run['run_id'])
    assert result['status'] == 'analysis_ready'
    with canvas_write_lock(ctx.state_dir, 'canvas', timeout_seconds=.02):
        pass


@pytest.mark.asyncio
async def test_retry_canvas_fence_failure_rolls_back_sql_and_releases_lock(service, tmp_path, monkeypatch):
    ctx, run, _ = await stale_completed(service, tmp_path, monkeypatch)
    class LostFenceMutex(FileLockCanvasWriteMutex):
        @contextmanager
        def write_mutex(self, project_dir, canvas_id, **kwargs):
            with super().write_mutex(project_dir, canvas_id) as guard:
                class LostGuard:
                    def reassert(self):
                        raise CanvasLockBusy(canvas_id)
                yield LostGuard()
    monkeypatch.setattr(canvas_store, 'get_canvas_write_mutex', lambda: LostFenceMutex())
    before = stored_row(service, run['run_id'])
    wakes = []
    monkeypatch.setattr(service, '_ensure_worker', lambda value: wakes.append(value))
    with pytest.raises(ValueError, match='画布正在保存'):
        await service.retry(ctx=ctx, run_id=run['run_id'])
    assert stored_row(service, run['run_id']) == before
    assert not wakes
    with canvas_write_lock(ctx.state_dir, 'canvas', timeout_seconds=.02):
        pass
