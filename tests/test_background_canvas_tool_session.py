import asyncio
import sqlite3

import pytest

from novelvideo.chat.store import ChatScope
from novelvideo.freezone import canvas_command_bridge as bridge


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,frame', [('canvas_context','canvas.context.request'), ('canvas_command','canvas.command'), ('clarification_event','assistant.clarification.event'), ('skill_studio_event','skill_studio.event')])
async def test_background_dispatch_uses_durable_bridge_and_real_receipt(tmp_path, kind, frame):
    from novelvideo.chat.canvas_tool_session import dispatch_pending_bridge_messages
    directory = tmp_path / 'bridge'
    envelope = {'schema_version': 'canvas_context_request.v1', 'canvas_id': 'c', 'requests': [{'type': 'node_detail', 'node_id': 'analysis'}]}
    if kind == 'canvas_context':
        bridge.put_pending_canvas_context(key='k', project_id='p', canvas_id='c', requests=envelope['requests'], envelope=envelope, bridge_dir=directory)
    elif kind == 'canvas_command':
        bridge.put_pending_canvas_command(key='k', project_id='p', canvas_id='c', commands=[], envelope={'commands': [], 'canvas_id': 'c'}, bridge_dir=directory)
    else:
        writer = bridge.put_pending_clarification_event if kind == 'clarification_event' else bridge.put_pending_skill_studio_event
        writer(key='k', project_id='p', canvas_id='c', event={'type': 'request'}, bridge_dir=directory)
    received = []
    async def browser(payload):
        received.append(payload)
        bridge.resolve_canvas_command(payload['bridge_key'], {'ok': True, 'project_id': 'p', 'canvas_id': 'c', 'results': [{'node_id': 'analysis', 'content': 'actual saved scene'}]}, bridge_dir=directory)
        return True
    await dispatch_pending_bridge_messages(username='actor', scope=ChatScope('project','p','freezone','c','main'), turn_id='t', bridge_dirs=[directory], send=browser, emitted_bridge_keys=set())
    assert received[0]['type'] == frame
    result = await asyncio.to_thread(bridge.wait_canvas_context_result, 'k', 0.2, bridge_dir=directory)
    assert result['results'][0]['content'] == 'actual saved scene'
    with bridge._bridge_db(directory) as conn:
        assert conn.execute('SELECT delivery_attempts FROM canvas_command_messages').fetchone()[0] == 1


@pytest.mark.asyncio
async def test_scope_delivery_chooses_one_tab_and_reports_absence():
    from novelvideo.chat.live_events import deliver_chat_scope_event, register_chat_websocket, unregister_chat_websocket
    scope = ChatScope('project','p','freezone','c','main')
    class Socket:
        def __init__(self): self.frames = []
        async def send_json(self, payload): self.frames.append(payload)
    sockets = [Socket(), Socket()]
    assert await deliver_chat_scope_event(username='actor', scope=scope, payload={}) == 'no_connection'
    try:
        for socket in sockets: await register_chat_websocket(socket, username='actor', scope=scope)
        assert await deliver_chat_scope_event(username='actor', scope=scope, payload={'type':'execute'}) == 'sent'
        assert sum(len(socket.frames) for socket in sockets) == 1
    finally:
        for socket in sockets: await unregister_chat_websocket(socket)

@pytest.mark.asyncio
async def test_failed_send_releases_lease_and_receipt_rejects_other_turn(tmp_path):
    from novelvideo.chat.canvas_tool_session import dispatch_pending_bridge_messages, validate_bridge_receipt
    directory = tmp_path / 'bridge'
    bridge.put_pending_canvas_context(key='k', project_id='p', canvas_id='c', requests=[], envelope={'requests': []}, bridge_dir=directory)
    async def offline(frame): return False
    scope = ChatScope('project','p','freezone','c','main')
    await dispatch_pending_bridge_messages(username='actor', scope=scope, turn_id='t', bridge_dirs=[directory], send=offline, emitted_bridge_keys=set())
    assert bridge.read_bridge_message('k', bridge_dir=directory)['transport_status'] == 'pending'
    async def sent(frame): return True
    await dispatch_pending_bridge_messages(username='actor', scope=scope, turn_id='t', bridge_dirs=[directory], send=sent, emitted_bridge_keys=set())
    from fastapi import HTTPException
    for change in [{'turn_id':'old'}, {'username':'other'}, {'agent_id':'other'}, {'canvas_id':'other'}, {'project_id':'other'}]:
        identity = dict(username='actor', turn_id='t', agent_id='main', canvas_id='c', project_id='p')
        identity.update(change)
        with pytest.raises(HTTPException): validate_bridge_receipt('k', bridge_dir=directory, **identity)
    validate_bridge_receipt('k', bridge_dir=directory, username='actor', turn_id='t', agent_id='main', canvas_id='c', project_id='p')


@pytest.mark.asyncio
async def test_lost_continuation_lease_does_not_deliver_pending_bridge_message(tmp_path):
    from novelvideo.chat.canvas_tool_session import dispatch_pending_bridge_messages

    directory = tmp_path / 'bridge'
    bridge.put_pending_canvas_command(key='stale-command', project_id='p', canvas_id='c',
                                      commands=[], envelope={'commands': []}, bridge_dir=directory)
    sent = []

    async def send(frame):
        sent.append(frame)
        return True

    await dispatch_pending_bridge_messages(username='actor',
        scope=ChatScope('project', 'p', 'freezone', 'c', 'main'), turn_id='t',
        bridge_dirs=[directory], send=send, emitted_bridge_keys=set(),
        lease_valid=lambda: False)
    assert sent == []
    assert bridge.read_bridge_message('stale-command', bridge_dir=directory)['transport_status'] == 'pending'


@pytest.mark.asyncio
async def test_lease_lost_while_sending_rejects_late_receipt(tmp_path):
    from fastapi import HTTPException
    from novelvideo.chat.canvas_tool_session import dispatch_pending_bridge_messages, validate_bridge_receipt

    directory = tmp_path / 'bridge'
    scope = ChatScope('project', 'p', 'freezone', 'c', 'main')
    bridge.put_pending_canvas_command(key='late-send', project_id='p', canvas_id='c',
        commands=[], envelope={'commands': []}, bridge_dir=directory)
    lease = [True]
    emitted = set()

    async def send(frame):
        lease[0] = False
        return True

    await dispatch_pending_bridge_messages(username='actor', scope=scope, turn_id='t',
        bridge_dirs=[directory], send=send, emitted_bridge_keys=emitted,
        scoped_executor=False, lease_valid=lambda: lease[0])
    assert emitted == set()
    assert bridge.read_bridge_message('late-send', bridge_dir=directory)['transport_status'] == 'pending'
    with pytest.raises(HTTPException):
        validate_bridge_receipt('late-send', bridge_dir=directory, username='actor',
            project_id='p', canvas_id='c', agent_id='main', turn_id='t')


@pytest.mark.asyncio
async def test_background_session_stops_after_its_continuation_lease_is_lost(tmp_path, monkeypatch):
    from novelvideo.api.routes import chat
    from novelvideo.chat.canvas_tool_session import CanvasToolSession
    from novelvideo.chat.execution_context import AgentExecutionContext
    from novelvideo.project_context import ProjectContext

    project = ProjectContext('p', 'p', 'user', 'u', 'actor', 'u', 'actor', (),
                             'owner', 'local', tmp_path, tmp_path, tmp_path, True)
    scope = ChatScope('project', 'p', 'freezone', 'c', 'main')
    directory = tmp_path / 'bridge'
    monkeypatch.setattr(chat, '_canvas_bridge_dir', lambda *args, **kwargs: directory)
    lease_valid = [True]
    sent = []

    async def send(frame):
        sent.append(frame)
        return True

    async with CanvasToolSession(
        execution_context=AgentExecutionContext.from_project_scope(scope=scope, project=project),
        project=project, scope=scope, turn_id='t', send=send,
        lease_valid=lambda: lease_valid[0],
    ) as session:
        lease_valid[0] = False
        bridge.put_pending_canvas_command(key='late-command', project_id='p', canvas_id='c',
                                          commands=[], envelope={'commands': []}, bridge_dir=directory)
        await asyncio.wait_for(session.task, timeout=2)
        assert session.delivery_status == 'lease_lost'
        assert sent == []
        assert bridge.read_bridge_message('late-command', bridge_dir=directory)['transport_status'] == 'pending'


@pytest.mark.asyncio
async def test_background_session_rejects_lost_lease_before_agent_turn(tmp_path):
    from novelvideo.chat.canvas_tool_session import CanvasToolSession
    from novelvideo.chat.execution_context import AgentExecutionContext
    from novelvideo.project_context import ProjectContext

    project = ProjectContext('p', 'p', 'user', 'u', 'actor', 'u', 'actor', (),
                             'owner', 'local', tmp_path, tmp_path, tmp_path, True)
    scope = ChatScope('project', 'p', 'freezone', 'c', 'main')
    with pytest.raises(ValueError, match='lease'):
        async with CanvasToolSession(
            execution_context=AgentExecutionContext.from_project_scope(scope=scope, project=project),
            project=project, scope=scope, turn_id='t', lease_valid=lambda: False,
        ):
            pytest.fail('a lost owner must not start a hosted tool session')

@pytest.mark.asyncio
async def test_background_session_round_trip_through_http_receipt(tmp_path, monkeypatch):
    for name,value in {'USERNAME': 'actor', 'PROJECT_ID': 'p', 'CANVAS_ID': 'c', 'AGENT_ID': 'main', 'TURN_ID': 'background'}.items():
        monkeypatch.setenv("DRAMACLAW_"+name,value)
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from novelvideo.api.routes import chat
    from novelvideo.chat.canvas_tool_session import CanvasToolSession
    from novelvideo.chat.execution_context import AgentExecutionContext
    from novelvideo.chat import live_events
    from novelvideo.project_context import ProjectContext
    project = ProjectContext('p','p','user','u','actor','u','actor',(), 'owner','local',tmp_path,tmp_path,tmp_path,True)
    scope = ChatScope('project','p','freezone','c','main')
    directory = tmp_path / 'bridge'
    monkeypatch.setattr(chat, '_canvas_bridge_dir', lambda *args, **kwargs: directory)
    async def state(*args): return tmp_path
    async def context(*args): return project
    monkeypatch.setattr(chat, '_bridge_project_state_dir', state)
    monkeypatch.setattr(chat, '_project_context_for_scope', context)
    application = FastAPI()
    application.include_router(chat.router)
    application.dependency_overrides[chat.get_api_user] = lambda: {'username': 'actor'}
    completed = asyncio.Event()
    async with AsyncClient(transport=ASGITransport(app=application), base_url='http://test') as client:
        class Socket:
            async def send_json(self, frame):
                response = await client.post('/chat/canvas-context-tool-result', json={
                    'bridge_key': frame['bridge_key'], 'turn_id': frame['turn_id'], 'project_id':'p',
                    'canvas_id':'c', 'agent_id':'main', 'responses':[{'type':'node_detail', 'data':{'nodes':[{'id':'analysis','type':'referenceAnalysisNode','data':{'content':'saved original scene'}}]}}]})
                assert response.status_code == 200, response.text
                completed.set()
        socket = Socket()
        await live_events.register_chat_websocket(socket, username='actor', scope=scope)
        try:
            async with CanvasToolSession(execution_context=AgentExecutionContext.from_project_scope(scope=scope, project=project), project=project, scope=scope, turn_id='background') as session:
                bridge.put_pending_canvas_context(key='background-k', project_id='p', canvas_id='c', requests=[{'type':'node_detail','node_id':'analysis'}], envelope={'schema_version':'canvas_context_request.v1','canvas_id':'c','requests':[{'type':'node_detail','node_id':'analysis'}]}, bridge_dir=directory)
                await asyncio.wait_for(completed.wait(), 2)
                result = await asyncio.to_thread(bridge.wait_canvas_context_result, 'background-k', 0.2, bridge_dir=directory)
                assert result['responses'][0]['data']['nodes'][0]['data']['content'] == 'saved original scene'
                from novelvideo.chat.dramaclaw_mcp import _normalize_structured_result
                from test_freezone_plugin import _load_plugin_module
                import sys
                with monkeypatch.context() as registry_patch:
                    registry_patch.setitem(sys.modules, "tools", sys.modules.get("tools"))
                    registry_patch.setitem(sys.modules, "tools.registry", sys.modules.get("tools.registry"))
                    plugin = _load_plugin_module()
                normalized = _normalize_structured_result(plugin._output_schema('freezone_get_node_detail'), result)
                assert normalized['read_consistency'] == 'current_browser'
                assert normalized['source'] == 'current_browser'
                assert result['source'] == 'current_browser'
                assert result['read_consistency'] == 'current_browser'
                assert session.receipts()[0]['result'] == result
            assert session.task.done()
        finally:
            await live_events.unregister_chat_websocket(socket)

@pytest.mark.asyncio
async def test_new_turn_does_not_adopt_old_or_prior_bound_requests(tmp_path):
    import time
    from novelvideo.chat.canvas_tool_session import dispatch_pending_bridge_messages
    directory = tmp_path / 'bridge'
    scope = ChatScope('project','p','freezone','c','main')
    for key in ['old', 'bound']:
        bridge.put_pending_canvas_context(key=key, project_id='p', canvas_id='c', requests=[], envelope={'requests': []}, bridge_dir=directory)
    started_at = time.time()
    bridge.mark_bridge_message_delivered('bound', consumer_id='old-consumer', bridge_dir=directory, binding={'username':'actor','project_id':'p','canvas_id':'c','agent_id':'main','turn_id':'prior'})
    bridge.release_bridge_delivery('bound', consumer_id='old-consumer', bridge_dir=directory)
    received = []
    async def browser(frame): received.append(frame); return True
    await dispatch_pending_bridge_messages(username='actor', scope=scope, turn_id='current', bridge_dirs=[directory], send=browser, emitted_bridge_keys=set(), started_at=started_at)
    assert received == []
    # Even with a broad age window an explicitly bound prior turn is never adopted.
    await dispatch_pending_bridge_messages(username='actor', scope=scope, turn_id='current', bridge_dirs=[directory], send=browser, emitted_bridge_keys=set(), started_at=0)
    assert [frame['bridge_key'] for frame in received] == ['old']
    assert bridge.read_bridge_message('bound', bridge_dir=directory)['delivery_binding']['turn_id'] == 'prior'

@pytest.mark.asyncio
async def test_executor_stays_on_first_tab_after_expiry_and_fences_transfer(tmp_path, monkeypatch):
    for name,value in {'USERNAME': 'actor', 'PROJECT_ID': 'p', 'CANVAS_ID': 'owner-c', 'AGENT_ID': 'main', 'TURN_ID': 't'}.items():
        monkeypatch.setenv("DRAMACLAW_"+name,value)
    from novelvideo.chat.canvas_tool_session import dispatch_pending_bridge_messages, validate_bridge_receipt
    from novelvideo.chat import live_events
    from fastapi import HTTPException
    directory = tmp_path / 'bridge'
    scope = ChatScope('project','p','freezone','owner-c','main')
    bridge.put_pending_canvas_context(key='owner-k', project_id='p', canvas_id='owner-c', requests=[], envelope={'requests':[]}, bridge_dir=directory)
    class Socket:
        def __init__(self): self.frames=[]
        async def send_json(self, frame): self.frames.append(frame)
    first, second = Socket(), Socket()
    async def send(frame): return await live_events.deliver_chat_scope_event(username='actor', scope=scope, payload=frame) == 'sent'
    async def dispatch():
        await dispatch_pending_bridge_messages(username='actor', scope=scope, turn_id='t', bridge_dirs=[directory], send=send, emitted_bridge_keys=set(), scoped_executor=True)
    def expire():
        with bridge._bridge_db(directory) as conn: conn.execute('UPDATE canvas_command_messages SET lease_expires_at=0')
    try:
        await live_events.register_chat_websocket(first, username='actor', scope=scope)
        await dispatch()
        await live_events.register_chat_websocket(second, username='actor', scope=scope)
        expire()
        await dispatch()
        assert len(first.frames) == 1 and second.frames == []
        old_key = first.frames[0]['bridge_key']
        await live_events.unregister_chat_websocket(first)
        await dispatch()
        assert len(second.frames) == 1
        new_key = second.frames[0]['bridge_key']
        assert new_key != old_key
        identity=dict(bridge_dir=directory, username='actor', project_id='p', canvas_id='owner-c', agent_id='main', turn_id='t')
        with pytest.raises(HTTPException): validate_bridge_receipt(old_key, **identity)
        with pytest.raises(HTTPException): validate_bridge_receipt('owner-k', **identity)
        validate_bridge_receipt(new_key, **identity)
    finally:
        await live_events.unregister_chat_websocket(first)
        await live_events.unregister_chat_websocket(second)


@pytest.mark.asyncio
async def test_skill_studio_frame_supports_frontend_identity_http_callback(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from novelvideo.api.routes import chat
    from novelvideo.chat.canvas_tool_session import dispatch_pending_bridge_messages
    directory=tmp_path/'bridge'
    monkeypatch.setattr(chat, '_canvas_bridge_dir', lambda *args, **kwargs: directory)
    async def state(*args): return tmp_path
    monkeypatch.setattr(chat, '_bridge_project_state_dir', state)
    application=FastAPI(); application.include_router(chat.router)
    application.dependency_overrides[chat.get_api_user]=lambda: {'username':'actor'}
    bridge.put_pending_skill_studio_event(key='studio', project_id='p', canvas_id='c', event={'type':'question','question_id':'q'}, bridge_dir=directory)
    async with AsyncClient(transport=ASGITransport(app=application), base_url='http://test') as client:
        async def frontend(frame):
            # use-superchat spreads event then overwrites identity from frame.
            event={**frame['event'], 'project_id':frame.get('project_id'), 'canvas_id':frame['canvas_id'], 'agent_id':frame['agent_id'], 'turn_id':frame['turn_id'], 'bridge_key':frame['bridge_key']}
            response=await client.post('/chat/skill-studio-tool-result', json={**event,'action':'answer','answers':{'q':'actual answer'},'ok':True})
            assert response.status_code == 200, response.text
            return True
        await dispatch_pending_bridge_messages(username='actor', scope=ChatScope('project','p','freezone','c','main'), turn_id='t', bridge_dirs=[directory], send=frontend, emitted_bridge_keys=set())
    assert bridge.read_bridge_message('studio', bridge_dir=directory)['result']['ok'] is True

@pytest.mark.asyncio
async def test_external_poll_cannot_reclaim_owned_canvas_command(tmp_path, monkeypatch):
    from novelvideo.api.routes import chat
    directory=tmp_path/'bridge'
    async def state(*args): return tmp_path
    monkeypatch.setattr(chat, '_bridge_project_state_dir', state)
    monkeypatch.setattr(chat, '_candidate_canvas_bridge_dirs_for_scope', lambda *args: [directory])
    for key in ['owned', 'external']:
        bridge.put_pending_canvas_command(key=key, project_id='p', canvas_id='c', commands=[], envelope={'commands':[],'external_mcp_command':True}, bridge_dir=directory)
    bridge.mark_bridge_message_delivered('owned', consumer_id='browser', bridge_dir=directory, binding={'username':'actor','project_id':'p','canvas_id':'c','agent_id':'main','turn_id':'t'}, execution_token='owner-token')
    with bridge._bridge_db(directory) as conn: conn.execute("UPDATE canvas_command_messages SET lease_expires_at=0 WHERE bridge_key='owned'")
    result=await chat.list_pending_canvas_commands(chat.PendingCanvasCommandsIn(project_id='p',canvas_id='c'), user={'username':'actor'})
    assert [frame['bridge_key'] for frame in result['data']['frames']] == ['external']
    assert bridge.read_bridge_message('owned', bridge_dir=directory)['execution_token'] == 'owner-token'

@pytest.mark.asyncio
async def test_background_session_reports_no_connection(tmp_path, monkeypatch):
    for name,value in {'USERNAME': 'offline-actor', 'PROJECT_ID': 'offline-p', 'CANVAS_ID': 'offline-c', 'AGENT_ID': 'main', 'TURN_ID': 't'}.items():
        monkeypatch.setenv("DRAMACLAW_"+name,value)
    from novelvideo.api.routes import chat
    from novelvideo.chat.canvas_tool_session import CanvasToolSession
    from novelvideo.chat.execution_context import AgentExecutionContext
    from novelvideo.project_context import ProjectContext
    project=ProjectContext('offline-p','p','user','u','offline-actor','u','offline-actor',(),'owner','local',tmp_path,tmp_path,tmp_path,True)
    scope=ChatScope('project','offline-p','freezone','offline-c','main')
    directory=tmp_path/'bridge'
    monkeypatch.setattr(chat, '_canvas_bridge_dir', lambda *args, **kwargs: directory)
    async with CanvasToolSession(execution_context=AgentExecutionContext.from_project_scope(scope=scope,project=project),project=project,scope=scope,turn_id='t') as session:
        bridge.put_pending_canvas_context(key='offline',project_id='offline-p',canvas_id='offline-c',requests=[],envelope={'requests':[]},bridge_dir=directory)
        await asyncio.sleep(0.05)
        assert session.delivery_status == 'no_connection'
        assert bridge.read_bridge_message('offline',bridge_dir=directory)['transport_status'] == 'pending'


@pytest.mark.asyncio
async def test_terminal_executor_pruned_but_waiting_executor_retained(tmp_path, monkeypatch):
    for name,value in {'USERNAME': 'cleanup-actor', 'PROJECT_ID': 'cleanup-p', 'CANVAS_ID': 'cleanup-c', 'AGENT_ID': 'main', 'TURN_ID': 't'}.items():
        monkeypatch.setenv("DRAMACLAW_"+name,value)
    from novelvideo.chat.canvas_tool_session import dispatch_pending_bridge_messages
    from novelvideo.chat import live_events
    directory=tmp_path/'bridge'
    scope=ChatScope('project','cleanup-p','freezone','cleanup-c','main')
    class Socket:
        async def send_json(self, frame): pass
    socket=Socket()
    async def send(frame): return await live_events.deliver_chat_scope_event(username='cleanup-actor',scope=scope,payload=frame) == 'sent'
    async def dispatch():
        await dispatch_pending_bridge_messages(username='cleanup-actor',scope=scope,turn_id='t',bridge_dirs=[directory],send=send,emitted_bridge_keys=set(),scoped_executor=True)
    try:
        await live_events.register_chat_websocket(socket,username='cleanup-actor',scope=scope)
        for key in ['completed','waiting','cancelled']:
            bridge.put_pending_canvas_context(key=key,project_id='cleanup-p',canvas_id='cleanup-c',requests=[],envelope={'requests':[]},bridge_dir=directory)
        await dispatch()
        bridge.resolve_canvas_context('completed',{'ok':True},bridge_dir=directory)
        bridge.resolve_canvas_context('cancelled',{'ok':False,'cancelled':True},bridge_dir=directory)
        await dispatch()
        prefix=live_events._scope_key('cleanup-actor',scope)
        retained=[key[-1] for key in live_events._execution_owners if key[:-1] == prefix]
        assert retained == ['waiting']
    finally:
        await live_events.unregister_chat_websocket(socket)

@pytest.mark.asyncio
async def test_missing_durable_row_drops_executor_and_rejects_alias(tmp_path):
    from fastapi import HTTPException
    from novelvideo.chat.canvas_tool_session import prune_terminal_executors, validate_bridge_receipt
    from novelvideo.chat import live_events
    scope=ChatScope('project','missing-p','freezone','missing-c','main')
    directory=tmp_path/'bridge'
    class Socket:
        async def send_json(self, frame): pass
    socket=Socket()
    try:
        await live_events.register_chat_websocket(socket, username='missing-actor',scope=scope)
        _,token,_=await live_events.reserve_chat_executor(username='missing-actor',scope=scope,bridge_key='missing-k')
        await prune_terminal_executors(username='missing-actor',scope=scope,bridge_dirs=[directory])
        assert (*live_events._scope_key('missing-actor',scope),'missing-k') not in live_events._execution_owners
        with pytest.raises(HTTPException):
            validate_bridge_receipt(f'missing-k.executor.{token}',bridge_dir=directory,username='missing-actor',project_id='missing-p',canvas_id='missing-c',agent_id='main',turn_id='t')
    finally:
        await live_events.unregister_chat_websocket(socket)


def test_absent_executor_alias_cannot_create_legacy_result(tmp_path):
    from fastapi import HTTPException
    from novelvideo.chat.canvas_tool_session import validate_bridge_receipt
    with pytest.raises(HTTPException):
        validate_bridge_receipt('absent.executor.token',bridge_dir=tmp_path/'bridge',username='actor',project_id='p',canvas_id='c',agent_id='main',turn_id='t')
