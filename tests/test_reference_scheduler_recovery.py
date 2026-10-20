"""Transient storage failure must not strand durable reference work."""

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from novelvideo.chat import reference_remix_continuation as continuation
from novelvideo.chat.reference_production_observer import ReferenceProductionObserver


class RecoveringStore:
    def __init__(self, saved_run, error):
        self.saved_run = saved_run
        self.error = error
        self.calls = 0

    def recover_expired_resume(self):
        return None

    def active(self):
        self.calls += 1
        if self.calls == 1:
            raise self.error
        return [self.saved_run]


@pytest.mark.asyncio
async def test_active_scheduler_recovers_saved_task_without_resubmission(monkeypatch):
    run = {'run_id': 'saved-run', 'task_id': 'saved-task', 'status': 'transcribing', 'updated_at': 'before'}
    store = RecoveringStore(run, sqlite3.OperationalError('disk I/O error'))
    coordinator = continuation.ReferenceRemixCoordinator(store)
    monkeypatch.setattr(continuation, 'automatic_reference_continuation_enabled', lambda: True)
    started = asyncio.Event()
    release = asyncio.Event()
    seen = []

    async def worker(run_id):
        seen.append(run_id)
        started.set()
        await release.wait()

    monkeypatch.setattr(coordinator, '_run', worker)
    waits = []

    async def wait(delay):
        waits.append(delay)
        await asyncio.sleep(0)

    monkeypatch.setattr(coordinator, '_wait_for_schedule_wake', wait)
    task = asyncio.create_task(coordinator._schedule_active())
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert not task.done()
        assert waits[0] == 1
        assert store.calls >= 2
        assert seen == ['saved-run']
        assert len([w for w in coordinator._workers.values() if not w.done()]) == 1
        assert store.saved_run['task_id'] == 'saved-task'
    finally:
        coordinator._stopping = True
        task.cancel()
        release.set()
        await asyncio.gather(task, *coordinator._workers.values(), return_exceptions=True)


@pytest.mark.asyncio
async def test_restart_starts_scheduler_when_initial_store_read_fails(monkeypatch):
    store = RecoveringStore({'run_id': 'saved', 'task_id': 'task', 'status': 'completed'},
                            sqlite3.OperationalError('disk I/O error'))
    coordinator = continuation.ReferenceRemixCoordinator(store)
    monkeypatch.setattr(continuation, 'automatic_reference_continuation_enabled', lambda: True)
    scheduled = asyncio.Event()
    monkeypatch.setattr(coordinator, '_ensure_worker', lambda run: scheduled.set())
    monkeypatch.setattr(coordinator, 'recover_interactions', lambda: asyncio.sleep(10))
    monkeypatch.setattr(coordinator, '_recover_draft_receipts', lambda: asyncio.sleep(10))
    try:
        await coordinator.resume()
        await asyncio.wait_for(scheduled.wait(), 2)
        assert coordinator._scheduler is not None and not coordinator._scheduler.done()
        assert store.saved_run['task_id'] == 'task'
    finally:
        await coordinator.shutdown()


@pytest.mark.asyncio
async def test_observer_recovers_locked_database_and_isolates_bad_run(monkeypatch):
    runs = [{'run_id': 'bad', 'status': 'awaiting_direction'},
            {'run_id': 'good', 'status': 'awaiting_direction'}]

    class Store:
        calls = 0

        def waiting_for_production(self):
            self.calls += 1
            if self.calls == 1:
                raise sqlite3.OperationalError('database is locked')
            return runs

    store = Store()
    observer = ReferenceProductionObserver(SimpleNamespace(store=store))
    monkeypatch.setattr(continuation, 'automatic_reference_continuation_enabled', lambda: True)
    good = asyncio.Event()

    async def observe(run):
        if run['run_id'] == 'bad':
            raise ValueError('bad run')
        good.set()
        return True

    monkeypatch.setattr(observer, 'observe_run', observe)
    monkeypatch.setattr(observer, '_diagnostic_context', lambda run: asyncio.sleep(0, result=('observe', {})))
    waits = []

    async def wait(delay):
        waits.append(delay)
        await asyncio.sleep(0)

    monkeypatch.setattr(observer, '_wait_for_wake', wait)
    task = asyncio.create_task(observer._loop())
    try:
        await asyncio.wait_for(good.wait(), 1)
        assert waits[0] == 1
        assert not task.done()
        assert store.calls >= 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_storage_retry_is_bounded_and_resets_after_success():
    recovery = continuation.StorageScanRecovery(component='reference_remix_continuation', stage='active_scan')
    delays = []

    async def failure():
        raise sqlite3.OperationalError('disk I/O error')

    async def success():
        return 'saved-task'

    async def wait(delay):
        delays.append(delay)

    for _ in range(7):
        assert await recovery.scan(failure, wait) == (False, None)
    assert delays == [1, 2, 4, 8, 16, 30, 30]
    assert await recovery.scan(success, wait) == (True, 'saved-task')
    assert await recovery.scan(failure, wait) == (False, None)
    assert delays[-1] == 1


@pytest.mark.asyncio
async def test_scan_cancellation_propagates_without_backoff():
    recovery = continuation.StorageScanRecovery(component='reference_remix_continuation', stage='active_scan')

    async def cancelled():
        raise asyncio.CancelledError

    async def wait(_delay):
        pytest.fail('cancelled scan waited for retry')

    with pytest.raises(asyncio.CancelledError):
        await recovery.scan(cancelled, wait)


@pytest.mark.asyncio
@pytest.mark.parametrize('scheduler,method', [('_schedule_interactions', 'recover_interactions'),
                                                  ('_schedule_draft_receipts', '_recover_draft_receipts')])
async def test_other_scheduler_loops_recover_storage_scan(monkeypatch, scheduler, method):
    coordinator = continuation.ReferenceRemixCoordinator(SimpleNamespace())
    scanned = asyncio.Event()
    calls = 0

    async def scan():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError('disk I/O error')
        scanned.set()

    delays = []

    async def wait(delay):
        delays.append(delay)
        await asyncio.sleep(0)

    monkeypatch.setattr(coordinator, method, scan)
    monkeypatch.setattr(coordinator, '_wait_for_background_wake', wait)
    task = asyncio.create_task(getattr(coordinator, scheduler)())
    try:
        await asyncio.wait_for(scanned.wait(), 1)
        assert not task.done()
        assert delays[0] == 1
    finally:
        coordinator._stopping = True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_cancels_active_scheduler_in_backoff(monkeypatch):
    class UnavailableStore:
        def recover_expired_resume(self):
            raise sqlite3.OperationalError('disk I/O error')

    coordinator = continuation.ReferenceRemixCoordinator(UnavailableStore())
    waiting = asyncio.Event()
    original = coordinator._wait_for_schedule_wake

    async def wait(delay):
        waiting.set()
        await original(delay)

    monkeypatch.setattr(coordinator, '_wait_for_schedule_wake', wait)
    coordinator._scheduler = asyncio.create_task(coordinator._schedule_active())
    await asyncio.wait_for(waiting.wait(), 1)
    await asyncio.wait_for(coordinator.shutdown(), 1)
    assert coordinator._scheduler is None
    assert coordinator._workers == {}


@pytest.mark.asyncio
async def test_persisted_wake_interrupts_sqlite_backoff(monkeypatch):
    run = {'run_id': 'saved', 'task_id': 'original-task', 'status': 'transcribing', 'updated_at': 'before'}
    store = RecoveringStore(run, sqlite3.OperationalError('disk I/O error'))
    coordinator = continuation.ReferenceRemixCoordinator(store)
    observed = asyncio.Event()
    monkeypatch.setattr(coordinator, '_ensure_worker', lambda saved: observed.set())
    waiting = asyncio.Event()
    original = coordinator._wait_for_schedule_wake

    async def wait(delay):
        if delay == 1:
            waiting.set()
        await original(delay)

    monkeypatch.setattr(coordinator, '_wait_for_schedule_wake', wait)
    task = asyncio.create_task(coordinator._schedule_active())
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        coordinator._schedule_wake.set()
        await asyncio.wait_for(observed.wait(), 0.2)
        assert store.calls >= 2
        assert run['task_id'] == 'original-task'
    finally:
        coordinator._stopping = True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_unknown_scan_error_logs_stack_without_sensitive_exception(caplog):
    recovery = continuation.StorageScanRecovery(component='reference_remix_continuation', stage='active_scan')

    async def broken():
        raise TypeError('Authorization: Bearer private-token')

    delays = []

    async def wait(delay):
        delays.append(delay)

    assert await recovery.scan(broken, wait) == (False, None)
    assert delays == [30]
    assert 'broken' in caplog.text
    assert 'private-token' not in caplog.text


@pytest.mark.asyncio
async def test_broken_unknown_error_log_sink_does_not_end_scan_recovery(monkeypatch):
    recovery = continuation.StorageScanRecovery(component='reference_remix_continuation', stage='active_scan')
    waits = []

    async def broken():
        raise TypeError('bug')

    async def wait(delay):
        waits.append(delay)

    def broken_sink(*_args, **_kwargs):
        raise RuntimeError('logging unavailable')

    monkeypatch.setattr(continuation.logger, 'error', broken_sink)
    assert await recovery.scan(broken, wait) == (False, None)
    assert waits == [30]


@pytest.mark.asyncio
@pytest.mark.parametrize('message', ['database is locked', 'disk I/O error'])
async def test_bound_analysis_task_survives_transient_sqlite_failure(tmp_path, monkeypatch, message):
    store = continuation.ReferenceRemixStore(tmp_path / 'runs.db')
    saved = store.create({
        'run_id': 'saved-run', 'project_id': 'project', 'requester_user_id': 'actor',
        'canvas_id': 'canvas', 'agent_id': 'main', 'idempotency_key': 'request',
        'status': 'analyzing', 'task_id': 'original-task', 'task_type': continuation.TASK_TYPE,
        'task_scope': 'original-scope', 'job_id': 'original-job',
    })
    coordinator = continuation.ReferenceRemixCoordinator(store)
    assert store.claim(saved['run_id'], coordinator.owner_token)
    ctx = SimpleNamespace(project_id='project', requester_user_id='actor')
    async def resolve(_run):
        return ctx

    monkeypatch.setattr(coordinator, '_resolve_context', resolve)
    monkeypatch.setattr(coordinator, '_assert_current', lambda *_args, **_kwargs: None)
    async def broadcast(_run):
        return None
    monkeypatch.setattr(coordinator, '_broadcast', broadcast)
    submissions = []

    async def submit(*_args, **_kwargs):
        submissions.append(True)
        pytest.fail('existing analysis task was resubmitted')

    monkeypatch.setattr(coordinator, '_submit_native_analysis', submit)
    monkeypatch.setattr(coordinator, '_submit_native_transcript', submit)
    task = SimpleNamespace(task_id='original-task', project_id='project',
                           requester_user_id='actor', status='running')
    calls = 0

    def read_task(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError(message)
        return task

    monkeypatch.setattr(continuation, 'get_task_manager', lambda: SimpleNamespace(get_task_for_project=read_task))
    assert await coordinator._process_once(store.get('saved-run')) is False
    after_fault = store.get('saved-run')
    assert after_fault['status'] == 'analyzing'
    assert after_fault['task_id'] == 'original-task'
    assert await coordinator._process_once(after_fault) is False
    assert store.get('saved-run')['status'] == 'analyzing'
    assert calls == 2
    assert submissions == []
