"""An actual failed project tool authentication ends exactly its live turn."""
import asyncio
import json

import pytest

from novelvideo.chat import backend_sdk, hermes_sdk, service
from novelvideo.chat.runtime_port import ChatBackendEvent
from novelvideo.chat.store import ChatScope


@pytest.fixture
def auth_turn(monkeypatch, tmp_path):
    monkeypatch.setenv('NOVELVIDEO_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('NOVELVIDEO_RUNTIME_DIR', str(tmp_path / 'runtime'))
    prompts, emitted, calls, revoked, finished = [], [], [], [], []
    interrupted = asyncio.Event()
    async def authorize(**kwargs):
        return None
    async def token(*args, **kwargs):
        return 'local-test-token'
    class Auth:
        async def revoke_agent_session(self, token):
            revoked.append(token)
    class Operation:
        async def finish(self, disposition):
            finished.append(disposition)
    class Handle:
        def interrupt(self):
            calls.append(('interrupt', 'runtime-thread', 'runtime-turn'))
            interrupted.set()
    monkeypatch.setattr(service, 'authorize_hermes_launch', authorize)
    monkeypatch.setattr(service, '_create_page_agent_session_token', token)
    monkeypatch.setattr(service, 'get_auth_session_port', lambda: Auth())
    monkeypatch.setattr(service, '_turn_operation_finalizer', lambda auth: Operation())
    monkeypatch.setattr(hermes_sdk, '_issue_turn_capability', lambda **kwargs: None)
    async def run(payload, *, tool_name='freezone_read_context', representation='structured', text=None):
        class Thread:
            async def stream(self, prompt):
                prompts.append(prompt)
                backend_sdk.register_live_codex_turn('runtime-thread', 'runtime-turn', Handle())
                try:
                    yield ChatBackendEvent(type='thread_started', thread_id='runtime-thread', turn_id='runtime-turn')
                    yield ChatBackendEvent(type='turn_started', thread_id='runtime-thread', turn_id='runtime-turn')
                    event = ChatBackendEvent(type='tool_updated', name='dramaclaw.' + tool_name,
                        call_id='read-a', status='completed', text=text)
                    setattr(event, representation, payload if representation == 'structured' else
                        {'content': [{'type': 'text', 'text': json.dumps(payload)}]})
                    yield event
                    if not interrupted.is_set():
                        calls.append(('second-business-call', tool_name))
                        yield ChatBackendEvent(type='assistant_delta', text='{"message":"已创建方案。","mode":"mutation","canvas_receipts":[]}')
                        yield ChatBackendEvent(type='complete', text='{"message":"已创建方案。","mode":"mutation","canvas_receipts":[]}')
                finally:
                    calls.append(('stream-cleaned',))
                    backend_sdk.unregister_live_codex_turn('runtime-thread', 'runtime-turn')
        monkeypatch.setattr(service, '_build_codex_thread', lambda *args, **kwargs: Thread())
        async def collect(event):
            emitted.append(event)
        state = tmp_path / 'state' / 'admin' / 'p'
        result = await asyncio.wait_for(service._stream_assistant_reply_codex('admin', 'p', '原片复刻', collect,
            project_state_dir=state, tool_mode='freezone_canvas', turn_id='business-turn',
            store_scope=ChatScope(kind='project', id='p', surface='freezone', canvas_id='c', agent_id='main', state_dir=str(state))), 2)
        return result
    return run, prompts, emitted, calls, revoked, finished, tmp_path


@pytest.mark.parametrize('tool_name,payload,representation', [
    ('freezone_read_context', {'ok': False, 'status_code': 401, 'error': 'Invalid agent session'}, 'structured'),
    ('freezone_create_production_plan', {'ok': False, 'error': 'Invalid agent session'}, 'output'),
    ('freezone_read_node_detail', {'ok': False, 'code': 'invalid_agent_session', 'error': 'authentication invalid'}, 'structured'),
])
async def test_first_genuine_auth_failure_interrupts_before_second_business_call(auth_turn, tool_name, payload, representation):
    run, prompts, events, calls, revoked, finished, tmp_path = auth_turn
    result = await run(payload, tool_name=tool_name, representation=representation)
    assert calls == [('interrupt', 'runtime-thread', 'runtime-turn'), ('stream-cleaned',)]
    assert len(prompts) == 1  # no model retry or format repair
    assert '会话' in result['content'] and '重试' in result['content']
    assert '已创建' not in result['content']
    assert revoked == ['local-test-token'] and finished == ['failed']
    assert not [path for path in (tmp_path / 'runtime' / 'codex' / 'turn_tokens').rglob('*') if path.is_file()]
    assert not service._ACTIVE_CODEX_TURNS
    terminal = [event for event in events if event['type'] == 'turn_completed']
    assert len(terminal) == 1 and terminal[0]['status'] == 'failed'
    assert terminal[0]['error']['code'] == 'agent_session_invalid'
    assert events[-1]['type'] == 'done' and events[-1]['assistant_delivered'] is False
    assert any(event['type'] == 'tool_updated' and event['call_id'] == 'read-a'
               and (event.get('result_json') == payload or event.get('output')) for event in events)


@pytest.mark.parametrize('payload', [
    {'ok': True, 'data': {'transcript': 'Invalid agent session', 'nested': {'ok': False, 'status_code': 401}}},
    {'ok': False, 'status_code': 403, 'code': 'permission_denied', 'error': 'user confirmation required'},
    {'ok': False, 'error': 'Media subtitle contains Invalid agent session'},
])
async def test_media_auth_text_and_ordinary_403_do_not_interrupt(auth_turn, payload):
    run, _, _, calls, _, _, _ = auth_turn
    await run(payload)
    assert ('second-business-call', 'freezone_read_context') in calls
    assert not any(call[0] == 'interrupt' for call in calls)


def test_host_terminal_auth_code_has_actionable_durable_blocker():
    from novelvideo.chat.reference_turn_outcome import ReferenceTurnOutcome
    outcome = ReferenceTurnOutcome(turn_id='t', scope={'project_id': 'p'}, revision=1)
    outcome.failure('agent_session_invalid')
    blocker = outcome.finish(current_revision=1)['execution_blocker']
    assert blocker['code'] == 'agent_session_invalid'
    assert blocker['next_action'] == 'restore_agent_session'
    assert '会话' in blocker['message']


@pytest.mark.parametrize('runtime_identity_matches,phase', [(True, 'awaiting_direction'), (False, 'awaiting_direction'), (True, 'drafting_plan')])
@pytest.mark.parametrize('pending_state', [None, 'browser', 'user', 'expired'])
async def test_native_write_auth_terminal_persists_scoped_recovery_blocker(monkeypatch, tmp_path, runtime_identity_matches, phase, pending_state):
    from contextlib import asynccontextmanager
    from test_reference_remix_continuation import context, ready_run
    from novelvideo.api import egress_binding
    from novelvideo.chat import reference_remix_continuation as continuation
    from novelvideo.freezone import canvas_store
    from novelvideo.ports.local.canvas_mutex import FileLockCanvasWriteMutex
    monkeypatch.setattr(canvas_store, 'get_canvas_write_mutex', lambda: FileLockCanvasWriteMutex())
    coordinator = continuation.ReferenceRemixCoordinator(continuation.ReferenceRemixStore(tmp_path / 'runs.db'))
    monkeypatch.setattr(coordinator, '_ensure_worker', lambda run: None)
    from novelvideo.chat import canvas_tool_session
    from novelvideo.freezone.canvas_command_bridge import put_pending_clarification_event, resolve_clarification_result
    from novelvideo.api.routes.chat import _canvas_bridge_dir
    sessions = []
    class TrackedSession(canvas_tool_session.CanvasToolSession):
        async def __aenter__(self):
            sessions.append(self)
            return await super().__aenter__()
    monkeypatch.setattr(canvas_tool_session, 'CanvasToolSession', TrackedSession)
    ctx = context(tmp_path)
    run, _ = await ready_run(coordinator, ctx, monkeypatch)
    run['result_fingerprint'] = continuation._node_fingerprint(coordinator._node(coordinator._canvas(ctx, 'canvas'), 'analysis'))
    if phase == 'drafting_plan':
        from novelvideo.freezone.remix.store import RemixArtifactStore
        from novelvideo.freezone.remix.reference_commission import (
            create_reference_commission, advance_reference_commission, AuthenticatedDecisionEvidence,
        )
        artifacts = RemixArtifactStore(ctx.output_dir, ctx.project_id)
        commission = create_reference_commission(artifacts, canvas_id='canvas',
            requester_user_id=ctx.requester_user_id, analysis_node_id='analysis', goal=run['goal'],
            analysis_evidence={'task_id': run['task_id'], 'result_fingerprint': run['result_fingerprint']})
        commission = advance_reference_commission(artifacts, commission['commission_ref'],
            {'type': 'direction_confirmed', 'direction': {'target_goal': run['goal']}}, 1,
            host_evidence=AuthenticatedDecisionEvidence(ctx.project_id, 'canvas', ctx.requester_user_id, 'main'))
        run.update(commission_ref=commission['commission_ref'], commission_phase=commission['phase'])
    assert coordinator.store.update_owned(run, coordinator.owner_token)
    @asynccontextmanager
    async def egress(**kwargs):
        yield None
    monkeypatch.setattr(egress_binding, 'request_egress_scope', egress)
    async def reply(*args, **kwargs):
        if pending_state:
            bridge_dir = _canvas_bridge_dir(ctx.requester_username, project_state_dir=ctx.state_dir, profile='freezone:main')
            put_pending_clarification_event(key='auth-pending-card', project_id=ctx.project_id, canvas_id='canvas',
                event={'clarification_id': 'auth-question'}, bridge_dir=bridge_dir)
            if pending_state in {'user', 'expired'}:
                # Model a delivered pending card, without any answer/approval.
                sessions[0].emitted_bridge_keys.add('auth-pending-card')
            if pending_state == 'expired':
                resolve_clarification_result('auth-pending-card', {'ok': False, 'canvas_apply_status': 'timeout'}, bridge_dir=bridge_dir)
        emit = args[3]
        await emit({'type': 'turn_started', 'thread_id': 'native-thread', 'turn_id': 'native-turn'})
        await emit({'type': 'tool_updated', 'name': 'dramaclaw.freezone_create_production_plan',
            'result_json': {'ok': False, 'status_code': 401, 'error': 'Invalid agent session'}})
        await emit({'type': 'turn_completed', 'thread_id': 'native-thread',
            'turn_id': 'native-turn' if runtime_identity_matches else 'other-turn',
            'status': 'failed', 'disposition': 'failed', 'error': {'code': 'agent_session_invalid'}})
        await emit({'type': 'done', 'assistant_delivered': False, 'message': {
            'id': 1, 'role': 'assistant', 'turn_id': kwargs['turn_id'], 'content': '系统工具会话已失效。'}})
    monkeypatch.setattr(continuation.chat_service, 'stream_assistant_reply', reply)
    await coordinator._resume_agent(run, ctx)
    saved = coordinator.store.get(run['run_id'])
    expected_other = {None: 'context_delivery_failed', 'browser': 'browser_required', 'user': 'user_required', 'expired': 'request_expired'}[pending_state]
    assert saved['status'] == ('awaiting_direction' if not runtime_identity_matches and pending_state == 'user' else 'resume_failed')
    assert saved['commission_phase'] == phase
    assert saved['source_fingerprint'] == run['source_fingerprint']
    assert saved['result_fingerprint'] == run['result_fingerprint']
    if pending_state:
        assert saved['pending_interaction']['clarification_id'] == 'auth-question'
        assert saved['pending_interaction']['bridge_key'] == 'auth-pending-card'
    blocker = saved['execution_blocker']
    assert blocker['code'] == ('agent_session_invalid' if runtime_identity_matches else expected_other)
    if runtime_identity_matches:
        assert blocker['next_action'] == 'restore_agent_session'
        assert '会话' in saved['message']


def test_successful_response_content_cannot_be_reinterpreted_as_auth_failure():
    from novelvideo.chat.tool_auth import codex_tool_agent_session_invalid
    event = ChatBackendEvent(type='tool_updated', name='dramaclaw.freezone_read_context',
        structured={'ok': True, 'content': [{'type': 'text', 'text': json.dumps({'ok': False, 'status_code': 401})}]})
    assert codex_tool_agent_session_invalid(event) is False


def test_auth_diagnostics_from_user_or_other_server_cannot_stop_project_turn():
    from novelvideo.chat.tool_auth import codex_tool_agent_session_invalid
    payload = {'ok': False, 'status_code': 401, 'error': 'Invalid agent session'}
    for event in [
        ChatBackendEvent(type='assistant_delta', name='dramaclaw.freezone_read_context', structured=payload),
        ChatBackendEvent(type='tool_updated', name='media.freezone_read_context', structured=payload),
        ChatBackendEvent(type='tool_updated', name='dramaclaw.freezone_read_context', text=json.dumps(payload)),
    ]:
        assert codex_tool_agent_session_invalid(event) is False


@pytest.mark.parametrize('dispatch', ['external', 'hermes'])
@pytest.mark.parametrize('delivery', ['fresh', 'cached'])
@pytest.mark.parametrize('boundary', ['receipt', 'bridge'])
async def test_reference_analysis_plugin_auth_failure_stops_actual_service_before_business_retry(monkeypatch, auth_turn, dispatch, delivery, boundary):
    from test_freezone_plugin import _reference_analysis_transport_result
    payload = _reference_analysis_transport_result(monkeypatch, dispatch=dispatch, delivery=delivery, boundary=boundary,
        failure={'ok': False, 'status_code': 401, 'code': 'invalid_agent_session', 'error': 'Session expired; authenticate again'})
    run, prompts, events, calls, revoked, finished, tmp_path = auth_turn
    result = await run(payload, tool_name='freezone_start_reference_analysis', representation='output' if dispatch == 'external' else 'structured')
    assert calls == [('interrupt', 'runtime-thread', 'runtime-turn'), ('stream-cleaned',)]
    assert len(prompts) == 1
    assert '会话' in result['content'] and '重试' in result['content']
    assert '已创建' not in result['content']
    assert revoked == ['local-test-token'] and finished == ['failed']
    assert not service._ACTIVE_CODEX_TURNS
    assert not [path for path in (tmp_path / 'runtime' / 'codex' / 'turn_tokens').rglob('*') if path.is_file()]
    terminal = [event for event in events if event['type'] == 'turn_completed']
    assert len(terminal) == 1 and terminal[0]['status'] == 'failed'
    assert terminal[0]['error'] == {'code': 'agent_session_invalid', 'next_action': 'restore_agent_session'}
    assert events[-1]['type'] == 'done' and events[-1]['assistant_delivered'] is False
