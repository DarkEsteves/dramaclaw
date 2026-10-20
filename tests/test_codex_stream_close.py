"""Closing an SDK stream interrupts only its turn and has a bounded worker wait."""
import asyncio
from contextlib import contextmanager
import threading
from types import SimpleNamespace

import pytest

from novelvideo.chat import backend_sdk, codex_app_server
from novelvideo.chat.backend_sdk import CodexThread


@pytest.mark.parametrize('honors_interrupt', [True, False])
async def test_early_close_interrupts_exact_turn_and_never_waits_forever(monkeypatch, tmp_path, honors_interrupt):
    release, cleaned = threading.Event(), threading.Event()
    interruptions = []
    class Turn:
        id = 'close-turn'
        def stream(self):
            try:
                release.wait(timeout=2)
                yield from ()
            finally:
                cleaned.set()
        def interrupt(self):
            interruptions.append(self.id)
            if honors_interrupt:
                release.set()
    @contextmanager
    def fake_shared(config):
        yield object()
    monkeypatch.setattr(codex_app_server, 'shared_codex', fake_shared)
    monkeypatch.setattr(backend_sdk, '_start_or_resume_codex_thread', lambda *args, **kwargs: SimpleNamespace(id='close-thread'))
    monkeypatch.setattr(backend_sdk, '_start_codex_turn', lambda *args, **kwargs: Turn())
    monkeypatch.setattr(backend_sdk, 'CODEX_INTERRUPT_GRACE_TIMEOUT', .05)
    thread = CodexThread(codex_bin=None, cwd=tmp_path, env={}, model='local-test',
        model_provider='local-test', developer_instructions='', config_overrides=(),
        thread_config={}, turn_metadata={}, thread_id=None)
    stream = thread.stream('local test')
    try:
        first = await anext(stream)
        assert first.type == 'thread_started'
        await asyncio.wait_for(stream.aclose(), timeout=.3)
        assert interruptions == ['close-turn']
        if honors_interrupt:
            assert cleaned.is_set()
    finally:
        release.set()
        await asyncio.to_thread(cleaned.wait, .5)
        backend_sdk.unregister_live_codex_turn('close-thread', 'close-turn')


@pytest.mark.parametrize('blocked_at', ['thread', 'turn', 'registration'])
async def test_cancel_before_identity_interrupts_late_handle_without_entering_stream(monkeypatch, tmp_path, blocked_at):
    startup_entered, release_startup, worker_cleaned = threading.Event(), threading.Event(), threading.Event()
    stream_entered, release_stream = threading.Event(), threading.Event()
    interruptions = []
    original_register = backend_sdk.register_live_codex_turn
    def pause(stage):
        if blocked_at == stage:
            startup_entered.set()
            release_startup.wait(timeout=2)
    class Turn:
        id = 'late-turn'
        def stream(self):
            stream_entered.set()
            release_stream.wait(timeout=2)
            yield from ()
        def interrupt(self):
            interruptions.append(self.id)
            release_stream.set()
    class OtherTurn:
        def interrupt(self):
            pytest.fail('unrelated live turn was interrupted')
    @contextmanager
    def shared(config):
        try:
            yield object()
        finally:
            worker_cleaned.set()
    def start_thread(*args, **kwargs):
        pause('thread')
        return SimpleNamespace(id='late-thread')
    def start_turn(*args, **kwargs):
        pause('turn')
        return Turn()
    def register(thread_id, turn_id, handle):
        if turn_id == 'late-turn':
            pause('registration')
        original_register(thread_id, turn_id, handle)
    monkeypatch.setattr(codex_app_server, 'shared_codex', shared)
    monkeypatch.setattr(backend_sdk, '_start_or_resume_codex_thread', start_thread)
    monkeypatch.setattr(backend_sdk, '_start_codex_turn', start_turn)
    monkeypatch.setattr(backend_sdk, 'register_live_codex_turn', register)
    monkeypatch.setattr(backend_sdk, 'CODEX_INTERRUPT_GRACE_TIMEOUT', .05)
    original_register('other-thread', 'other-turn', OtherTurn())
    thread = CodexThread(codex_bin=None, cwd=tmp_path, env={}, model='local-test',
        model_provider='local-test', developer_instructions='', config_overrides=(),
        thread_config={}, turn_metadata={}, thread_id=None)
    pending = asyncio.create_task(anext(thread.stream('local cancellation test')))
    try:
        assert await asyncio.to_thread(startup_entered.wait, .5)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, timeout=.3)
        release_startup.set()
        assert await asyncio.to_thread(worker_cleaned.wait, .5)
        assert not stream_entered.is_set()
        assert interruptions == ([] if blocked_at == 'thread' else ['late-turn'])
        with backend_sdk._LIVE_CODEX_TURNS_LOCK:
            assert ('late-thread', 'late-turn') not in backend_sdk._LIVE_CODEX_TURNS
            assert ('other-thread', 'other-turn') in backend_sdk._LIVE_CODEX_TURNS
    finally:
        release_startup.set()
        release_stream.set()
        await asyncio.to_thread(worker_cleaned.wait, .5)
        backend_sdk.unregister_live_codex_turn('late-thread', 'late-turn')
        backend_sdk.unregister_live_codex_turn('other-thread', 'other-turn')


async def test_close_keeps_exact_handle_when_worker_unregisters_before_interrupt_dispatch(monkeypatch, tmp_path):
    released, cleaned = threading.Event(), threading.Event()
    interruptions = []
    original_interrupt = backend_sdk.interrupt_live_codex_turn
    class Turn:
        id = 'dispatch-turn'
        def stream(self):
            released.wait(timeout=2)
            yield from ()
        def interrupt(self):
            interruptions.append(self.id)
    @contextmanager
    def shared(config):
        try:
            yield object()
        finally:
            cleaned.set()
    def dispatch_after_unregister(thread_id, turn_id):
        assert (thread_id, turn_id) == ('dispatch-thread', 'dispatch-turn')
        released.set()
        assert cleaned.wait(timeout=.3)
        return original_interrupt(thread_id, turn_id)
    monkeypatch.setattr(codex_app_server, 'shared_codex', shared)
    monkeypatch.setattr(backend_sdk, '_start_or_resume_codex_thread', lambda *args, **kwargs: SimpleNamespace(id='dispatch-thread'))
    monkeypatch.setattr(backend_sdk, '_start_codex_turn', lambda *args, **kwargs: Turn())
    monkeypatch.setattr(backend_sdk, 'interrupt_live_codex_turn', dispatch_after_unregister)
    monkeypatch.setattr(backend_sdk, 'CODEX_INTERRUPT_GRACE_TIMEOUT', .5)
    thread = CodexThread(codex_bin=None, cwd=tmp_path, env={}, model='local-test',
        model_provider='local-test', developer_instructions='', config_overrides=(),
        thread_config={}, turn_metadata={}, thread_id=None)
    stream = thread.stream('local test')
    try:
        assert (await anext(stream)).type == 'thread_started'
        await asyncio.wait_for(stream.aclose(), timeout=.8)
        assert interruptions == ['dispatch-turn']
        with backend_sdk._LIVE_CODEX_TURNS_LOCK:
            assert ('dispatch-thread', 'dispatch-turn') not in backend_sdk._LIVE_CODEX_TURNS
    finally:
        released.set()
        await asyncio.to_thread(cleaned.wait, .5)
        backend_sdk.unregister_live_codex_turn('dispatch-thread', 'dispatch-turn')


@pytest.mark.parametrize('close_kind', ['early_close', 'timeout'])
async def test_close_claim_survives_worker_cleanup_during_closure_publication(monkeypatch, tmp_path, close_kind):
    release_worker, unregister_entered, worker_cleaned = threading.Event(), threading.Event(), threading.Event()
    interruptions, cleanup_during_publication = [], []
    original_unregister = backend_sdk.unregister_live_codex_turn
    class ClosureEvent:
        def __init__(self):
            self.event = threading.Event()
        def is_set(self):
            return self.event.is_set()
        def set(self):
            first_close = not self.event.is_set()
            self.event.set()
            if first_close:
                release_worker.set()
                assert unregister_entered.wait(timeout=.3)
                # Permit cleanup to complete before the consumer can claim if
                # publication does not hold the registration lock.
                cleanup_during_publication.append(worker_cleaned.wait(timeout=.05))
    class Turn:
        id = 'claim-turn'
        def stream(self):
            release_worker.wait(timeout=2)
            yield SimpleNamespace(payload=None)  # nonterminal; closed reader returns first
        def interrupt(self):
            interruptions.append(self.id)
    class OtherTurn:
        def interrupt(self):
            pytest.fail('unrelated live turn was interrupted')
    @contextmanager
    def shared(config):
        try:
            yield object()
        finally:
            worker_cleaned.set()
    def unregister(thread_id, turn_id):
        if (thread_id, turn_id) == ('claim-thread', 'claim-turn'):
            unregister_entered.set()
        original_unregister(thread_id, turn_id)
    monkeypatch.setattr(backend_sdk, 'threading', SimpleNamespace(Event=ClosureEvent, Lock=threading.Lock))
    monkeypatch.setattr(codex_app_server, 'shared_codex', shared)
    monkeypatch.setattr(backend_sdk, '_start_or_resume_codex_thread', lambda *args, **kwargs: SimpleNamespace(id='claim-thread'))
    monkeypatch.setattr(backend_sdk, '_start_codex_turn', lambda *args, **kwargs: Turn())
    monkeypatch.setattr(backend_sdk, 'unregister_live_codex_turn', unregister)
    monkeypatch.setattr(backend_sdk, 'CODEX_INTERRUPT_GRACE_TIMEOUT', .5)
    monkeypatch.setattr(backend_sdk, 'CODEX_STREAM_FIRST_PROGRESS_TIMEOUT', .05)
    monkeypatch.setattr(backend_sdk, 'CODEX_STREAM_IDLE_TIMEOUT', .05)
    monkeypatch.setattr(backend_sdk, 'CODEX_STREAM_TOTAL_TIMEOUT', .1)
    backend_sdk.register_live_codex_turn('unrelated-thread', 'unrelated-turn', OtherTurn())
    thread = CodexThread(codex_bin=None, cwd=tmp_path, env={}, model='local-test',
        model_provider='local-test', developer_instructions='', config_overrides=(),
        thread_config={}, turn_metadata={}, thread_id=None)
    stream = thread.stream('local test')
    try:
        assert (await anext(stream)).type == 'thread_started'
        if close_kind == 'early_close':
            await asyncio.wait_for(stream.aclose(), timeout=.8)
        else:
            events = await asyncio.wait_for(_remaining(stream), timeout=.8)
            assert events[-1].type == 'complete' and events[-1].disposition == 'timeout'
        assert await asyncio.to_thread(worker_cleaned.wait, .3)
        assert interruptions == ['claim-turn']
        assert cleanup_during_publication == [False]
        with backend_sdk._LIVE_CODEX_TURNS_LOCK:
            assert ('claim-thread', 'claim-turn') not in backend_sdk._LIVE_CODEX_TURNS
            assert ('unrelated-thread', 'unrelated-turn') in backend_sdk._LIVE_CODEX_TURNS
    finally:
        release_worker.set()
        await asyncio.to_thread(worker_cleaned.wait, .5)
        original_unregister('claim-thread', 'claim-turn')
        original_unregister('unrelated-thread', 'unrelated-turn')


async def _remaining(stream):
    return [event async for event in stream]


async def test_timeout_before_any_raw_notification_reports_unavailable(monkeypatch, tmp_path, caplog):
    release = threading.Event()
    interruptions = []

    class Turn:
        id = 'silent-turn'

        def stream(self):
            release.wait(timeout=2)
            yield from ()

        def interrupt(self):
            interruptions.append(self.id)
            release.set()

    @contextmanager
    def shared(config):
        yield object()

    monkeypatch.setattr(codex_app_server, 'shared_codex', shared)
    monkeypatch.setattr(backend_sdk, '_start_or_resume_codex_thread', lambda *args, **kwargs: SimpleNamespace(id='silent-thread'))
    monkeypatch.setattr(backend_sdk, '_start_codex_turn', lambda *args, **kwargs: Turn())
    monkeypatch.setattr(backend_sdk, 'CODEX_STREAM_FIRST_PROGRESS_TIMEOUT', .05)
    monkeypatch.setattr(backend_sdk, 'CODEX_STREAM_TOTAL_TIMEOUT', 1)
    thread = CodexThread(codex_bin=None, cwd=tmp_path, env={}, model='local-test',
        model_provider='local-test', developer_instructions='', config_overrides=(),
        thread_config={}, turn_metadata={}, thread_id=None)
    events = await asyncio.wait_for(_remaining(thread.stream('silent request')), timeout=.8)

    assert interruptions == ['silent-turn']
    assert events[-1].type == 'complete' and events[-1].disposition == 'timeout'
    assert events[-1].error['kind'] == 'first-progress'
    timeout_log = next(record.message for record in caplog.records if 'stream timed out' in record.message)
    assert 'last_raw_event_type=unavailable' in timeout_log
    assert 'last_mapped_event_type=egress_submitted' in timeout_log
    assert 'time_since_raw_event=unavailable' in timeout_log
    assert 'raw_event_count=0' in timeout_log
    assert 'event_count=2' in timeout_log
