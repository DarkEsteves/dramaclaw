import asyncio
import pytest
from novelvideo.chat import reference_remix_continuation as module

from concurrent.futures import ThreadPoolExecutor

from novelvideo.chat.reference_remix_continuation import ReferenceRemixStore


def seed(store, run_id):
    store.create(dict(run_id=run_id, project_id=run_id, requester_user_id='actor', canvas_id='c', agent_id='main', idempotency_key=run_id, request_fingerprint=run_id, status='analysis_ready'))
    assert store.claim(run_id, run_id)


def test_ee_default_store_requires_an_explicit_absolute_home_state_root(tmp_path, monkeypatch):
    monkeypatch.setenv('ST_CONTROL_PLANE_DSN', 'postgresql://configured')
    monkeypatch.delenv('NOVELVIDEO_STATE_DIR', raising=False)
    monkeypatch.setattr(module, '_state_root', lambda: tmp_path)
    with pytest.raises(RuntimeError, match='NOVELVIDEO_STATE_DIR'):
        ReferenceRemixStore().active()
    monkeypatch.setenv('NOVELVIDEO_STATE_DIR', 'relative-state')
    with pytest.raises(RuntimeError, match='absolute'):
        ReferenceRemixStore().active()
    other_root = tmp_path / 'other-root'
    other_root.mkdir()
    monkeypatch.setenv('NOVELVIDEO_STATE_DIR', str(other_root))
    with pytest.raises(RuntimeError, match='changed'):
        ReferenceRemixStore().active()


def test_shared_home_node_limits_and_owner_fencing(tmp_path):
    stores = [ReferenceRemixStore(tmp_path / 'runs.db') for _ in range(8)]
    for i in range(8):
        seed(stores[0], str(i))
    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = list(pool.map(lambda i: stores[i].claim_model_slot(str(i), str(i), ('organization', str(i))), range(8)))
    assert sum(accepted) == 4
    first = str(accepted.index(True))
    assert not stores[0].claim_model_slot('7', 'wrong-owner', ('organization', 'other'))
    stores[0].release_model_slot(first, 'wrong-owner')
    assert stores[0].model_slot_count() == 4
    stores[0].release(first, first)
    assert stores[1].model_slot_count() == 3


def test_tenant_namespace_lower_limits_and_expiry(tmp_path, monkeypatch):
    monkeypatch.setenv('DRAMACLAW_REFERENCE_BACKGROUND_GLOBAL_LIMIT', '2')
    store = ReferenceRemixStore(tmp_path / 'runs.db')
    for i in range(4):
        seed(store, str(i))
    assert store.claim_model_slot('0', '0', ('organization', 'owner'))
    assert not store.claim_model_slot('1', '1', ('organization', 'owner'))
    assert store.claim_model_slot('2', '2', ('user', 'owner'))
    assert not store.claim_model_slot('3', '3', ('user', 'other'))
    with store._connect() as db:
        db.execute('UPDATE reference_remix_continuations SET lease_until=0 WHERE run_id=?', ('0',))
    assert store.model_slot_count() == 1
    assert not store.renew_model_slot('0', '0')
    assert store.claim_model_slot('1', '1', ('organization', 'owner'))



@pytest.mark.asyncio
async def test_capacity_denial_stays_waiting_and_cancellation_releases(tmp_path, monkeypatch):
    from test_reference_remix_continuation import context
    ctx = context(tmp_path)
    store = ReferenceRemixStore(tmp_path / 'runs.db')
    coordinator = module.ReferenceRemixCoordinator(store)
    coordinator.owner_token = 'queued'
    for run_id in ('held','queued'):
        store.create(dict(run_id=run_id, project_id=ctx.project_id, requester_user_id=ctx.requester_user_id, canvas_id='c', agent_id='main', idempotency_key=run_id, request_fingerprint=run_id, status='analysis_ready', username=ctx.requester_username))
        assert store.claim(run_id, run_id)
    assert store.claim_model_slot('held', 'held', (ctx.owner_type, ctx.owner_id))
    async def resolve(run):
        return ctx
    coordinator._resolve_context = resolve
    coordinator._assert_current = lambda *args, **kwargs: {}
    coordinator.scope = lambda run: None
    monkeypatch.setattr(module.chat_service, '_chat_run_lock_project_for_turn', lambda *args, **kwargs: 'p')
    monkeypatch.setattr(module.chat_service, 'chat_run_lock_is_active', lambda *args: False)
    calls = []
    started = asyncio.Event()
    async def model(run, project):
        calls.append(run['run_id'])
        started.set()
        await asyncio.Event().wait()
    coordinator._resume_agent = model
    async def broadcast(run):
        pass
    coordinator._broadcast = broadcast
    assert not await coordinator._process_once(store.get('queued'))
    assert store.get('queued')['status'] == 'analysis_ready'
    assert calls == []
    store.release('held', 'held')
    work = asyncio.create_task(coordinator._process_once(store.get('queued')))
    await asyncio.wait_for(started.wait(), 1)
    assert store.model_slot_count() == 1
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert store.model_slot_count() == 0


def test_bridge_expiry_housekeeping_is_bounded(tmp_path):
    import time
    from novelvideo.freezone import canvas_command_bridge as bridge
    now = time.time()
    with bridge._bridge_db(tmp_path) as db:
        db.executemany('INSERT INTO canvas_bridge_producer_turns VALUES (?,?,?)', [(str(i),str(i),now-1) for i in range(1000)])
    bridge.register_bridge_producer_turn(binding={'project_id':'p','canvas_id':'c','username':'u','agent_id':'a','turn_id':'new'},bridge_dir=tmp_path)
    with bridge._bridge_db(tmp_path) as db:
        expired = db.execute('SELECT COUNT(*) FROM canvas_bridge_producer_turns WHERE expires_at<=?', (now,)).fetchone()[0]
    assert expired == 800


def _process_admission(db_path, index, queue):
    store = ReferenceRemixStore(db_path)
    queue.put(store.claim_model_slot(str(index), str(index), ('organization', str(index))))


def test_separate_processes_share_four_home_node_slots(tmp_path):
    import multiprocessing
    store = ReferenceRemixStore(tmp_path / 'runs.db')
    for i in range(8):
        seed(store, str(i))
    process_context = multiprocessing.get_context('spawn')
    queue = process_context.Queue()
    processes = [process_context.Process(target=_process_admission, args=(store.db_path, i, queue)) for i in range(8)]
    try:
        for process in processes:
            process.start()
        assert sum(queue.get(timeout=15) for _ in processes) == 4
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0
        assert store.model_slot_count() == 4
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join()
        queue.close()


def _process_claim_and_update(db_path, token, gate, command, result):
    store = ReferenceRemixStore(db_path)
    gate.wait(timeout=15)
    claimed = store.claim('shared-run', token)
    result.put((token, claimed))
    if claimed:
        command.get(timeout=15)
        run = store.get('shared-run')
        run['status'] = 'completed'
        result.put((token, store.update_owned(run, token)))


def test_separate_processes_fence_expired_run_owner(tmp_path):
    import multiprocessing

    store = ReferenceRemixStore(tmp_path / 'runs.db')
    store.create(dict(run_id='shared-run', project_id='p', requester_user_id='u',
        canvas_id='c', agent_id='main', idempotency_key='shared-run',
        request_fingerprint='shared-run', status='analysis_ready'))
    process_context = multiprocessing.get_context('spawn')
    gate = process_context.Event()
    result = process_context.Queue()
    commands = {token: process_context.Queue() for token in ('first', 'second')}
    processes = [process_context.Process(target=_process_claim_and_update,
        args=(store.db_path, token, gate, commands[token], result)) for token in commands]
    try:
        for process in processes:
            process.start()
        gate.set()
        claims = [result.get(timeout=15) for _ in processes]
        assert sorted(claimed for _, claimed in claims) == [False, True]
        old_owner = next(token for token, claimed in claims if claimed)
        with store._connect() as db:
            db.execute("UPDATE reference_remix_continuations SET lease_until=0 WHERE run_id='shared-run'")
        assert store.claim('shared-run', 'new-owner')
        commands[old_owner].put(True)
        assert result.get(timeout=15) == (old_owner, False)
        run = store.get('shared-run')
        run['status'] = 'completed'
        assert store.update_owned(run, 'new-owner')
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join()
        result.close()
        for command in commands.values():
            command.close()


def test_run_renewal_preserves_model_slot_atomically_across_old_deadline(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(module.time, 'time', lambda: clock[0])
    store = ReferenceRemixStore(tmp_path / 'runs.db')
    for run_id in ('first', 'second'):
        seed(store, run_id)
    assert store.claim_model_slot('first', 'first', ('user', 'owner'))
    clock[0] = 1059.0
    assert store.renew('first', 'first')
    clock[0] = 1061.0
    assert store.claim('second', 'second')
    assert not store.claim_model_slot('second', 'second', ('user', 'owner'))
    with store._connect() as db:
        run_until = db.execute('SELECT lease_until FROM reference_remix_continuations WHERE run_id=?', ('first',)).fetchone()[0]
        slot_until = db.execute('SELECT expires_at FROM reference_background_slots WHERE run_id=?', ('first',)).fetchone()[0]
    assert run_until == slot_until == 1119.0


@pytest.mark.asyncio
async def test_heartbeat_cancels_active_model_when_owned_slot_is_missing(tmp_path, monkeypatch):
    store = ReferenceRemixStore(tmp_path / 'runs.db')
    coordinator = module.ReferenceRemixCoordinator(store)
    coordinator.owner_token = 'first'
    seed(store, 'first')
    assert store.claim_model_slot('first', 'first', ('user', 'owner'))
    coordinator._admitted_runs = {'first'}
    store.release_model_slot('first', 'first')
    parent = asyncio.create_task(asyncio.Event().wait())
    monkeypatch.setattr(module, 'HEARTBEAT_SECONDS', .001)
    heartbeat = asyncio.create_task(coordinator._heartbeat('first', parent))
    try:
        done, _ = await asyncio.wait({parent}, timeout=.2)
        assert parent in done and parent.cancelled()
        await asyncio.wait_for(heartbeat, .2)
    finally:
        parent.cancel()
        heartbeat.cancel()
        await asyncio.gather(parent, heartbeat, return_exceptions=True)


@pytest.mark.asyncio
async def test_heartbeat_does_not_cancel_after_legitimate_model_release(tmp_path, monkeypatch):
    store = ReferenceRemixStore(tmp_path / 'runs.db')
    coordinator = module.ReferenceRemixCoordinator(store)
    coordinator.owner_token = 'first'
    coordinator._admitted_runs.add('first')
    seed(store, 'first')
    assert store.claim_model_slot('first', 'first', ('user', 'owner'))
    real_renew = store.renew
    def racing_renew(run_id, token, *, require_model_slot=False):
        if require_model_slot:
            coordinator._admitted_runs.discard('first')
            store.release_model_slot('first', 'first')
            return False
        coordinator._stopping = True
        return real_renew(run_id, token)
    monkeypatch.setattr(store, 'renew', racing_renew)
    monkeypatch.setattr(module, 'HEARTBEAT_SECONDS', .001)
    parent = asyncio.create_task(asyncio.Event().wait())
    try:
        await asyncio.wait_for(coordinator._heartbeat('first', parent), .2)
        assert parent.cancelling() == 0
    finally:
        parent.cancel()
        await asyncio.gather(parent, return_exceptions=True)
