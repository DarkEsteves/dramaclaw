"""Production edit retries settle only a saved correction of the rejected call."""

import pytest

from novelvideo.chat.operation_outcome import production_edit_retry_matches


def call(*, command=None, retry_of=None, canvas='c', node='node'):
    return {'project_id': 'p', 'canvas_id': canvas, 'node_id': node,
            'command': command or {'command_id': 'first',
                                    'plan_ref': {'artifact_id': 'plan', 'revision': 3},
                                    'kind': 'batch', 'target_id': 'plan', 'payload': {'commands': [
                                        {'kind': 'edit_turn', 'target_id': 'turn', 'payload': {'text': 'Keep this'}},
                                        {'kind': 'edit_beat', 'target_id': 'beat', 'payload': {'purpose': 'Keep that'}},
                                    ]}}, **({'retry_of': retry_of} if retry_of else {})}


def test_retry_preserves_every_existing_business_value_and_batch_order():
    failed = call()
    corrected = call(command={**failed['command'], 'command_id': 'second'})
    error = {'side_effects': 'none', 'error_path': 'payload.commands[0].payload.extra',
             'missing_fields': ['extra']}
    assert production_edit_retry_matches(failed, corrected, error)
    changed = call(command={**corrected['command'], 'payload': {'commands': [
        {**corrected['command']['payload']['commands'][0], 'payload': {'text': 'Changed'}},
        corrected['command']['payload']['commands'][1],
    ]}})
    assert not production_edit_retry_matches(failed, changed, error)
    shortened = call(command={**corrected['command'], 'payload': {'commands': [
        corrected['command']['payload']['commands'][0]]}})
    assert not production_edit_retry_matches(failed, shortened, error)
    reordered = call(command={**corrected['command'], 'payload': {'commands': list(reversed(
        corrected['command']['payload']['commands']))}})
    assert not production_edit_retry_matches(failed, reordered, error)
    assert not production_edit_retry_matches(failed, call(canvas='other'), error)
    assert not production_edit_retry_matches(failed, call(command={**corrected['command'],
        'plan_ref': {'artifact_id': 'plan', 'revision': 4}}), error)


def test_only_reported_missing_field_may_be_filled():
    failed = call(command={**call()['command'], 'kind': 'edit_turn', 'target_id': None,
                           'payload': {'text': 'Keep this'}})
    corrected = call(command={**failed['command'], 'command_id': 'second', 'target_id': 'turn'})
    error = {'side_effects': 'none', 'error_path': 'target_id', 'missing_fields': ['target_id']}
    assert production_edit_retry_matches(failed, corrected, error)
    assert not production_edit_retry_matches(failed, corrected, {**error, 'side_effects': 'unknown'})
    assert not production_edit_retry_matches(failed, call(command={**corrected['command'],
        'payload': {'text': 'Altered'}}), error)


def test_reported_numeric_type_correction_keeps_same_value():
    old = call(command={**call()['command'], 'kind': 'insert_beat', 'target_id': 'intro',
                        'payload': {'beat': {'id': 'intro', 'purpose': 'Keep this',
                                             'duration_frames': '72'}, 'index': 0}})
    new = call(command={**old['command'], 'command_id': 'second', 'payload': {
        'beat': {'id': 'intro', 'purpose': 'Keep this', 'duration_frames': 72}, 'index': 0}})
    error = {'side_effects': 'none', 'error_path': 'payload.beat.duration_frames',
             'missing_fields': []}
    assert production_edit_retry_matches(old, new, error)
    changed = call(command={**new['command'], 'payload': {
        'beat': {'id': 'intro', 'purpose': 'Keep this', 'duration_frames': 96}, 'index': 0}})
    assert not production_edit_retry_matches(old, changed, error)


def test_reported_payload_wrapper_preserves_existing_placement():
    placement = {'id': 'shot', 'kind': 'visual', 'role': 'primary', 'track_id': 'main'}
    old = call(command={**call()['command'], 'kind': 'upsert_placement',
                        'target_id': 'shot', 'payload': placement})
    corrected = call(command={**old['command'], 'command_id': 'second',
                              'payload': {'placement': placement}})
    error = {'side_effects': 'none', 'error_path': 'payload.placement',
             'missing_fields': ['placement']}
    assert production_edit_retry_matches(old, corrected, error)
    altered = call(command={**corrected['command'], 'payload': {'placement': {
        **placement, 'role': 'effect'}}})
    assert not production_edit_retry_matches(old, altered, error)


def test_plugin_returns_retry_token_only_for_no_effect_schema_rejection(monkeypatch):
    import json
    from novelvideo.chat import dramaclaw_mcp

    plugin = dramaclaw_mcp._plugin('freezone')
    monkeypatch.setattr(plugin, '_emit_canvas_commands', lambda *args, **kwargs: json.dumps({
        'ok': False, 'side_effects': 'none', 'error_path': 'target_id',
        'missing_fields': ['target_id']}))
    failed = call(command={**call()['command'], 'target_id': None})
    result = json.loads(plugin._handle_prepare_production_edit(failed))
    assert result['ok'] is False
    assert result['side_effects'] == 'none'
    assert isinstance(result['retry_token'], str) and result['retry_token']
    schema = next(schema for name, schema, _ in plugin.TOOLS
                  if name == 'freezone_prepare_production_edit')
    assert 'retry_of' in schema['parameters']['properties']
    assert 'retry_of' not in schema['parameters']['properties']['command']['properties']


def test_plugin_summary_keeps_exact_native_plan_receipt():
    from novelvideo.chat import dramaclaw_mcp

    plugin = dramaclaw_mcp._plugin('freezone')
    result = plugin._summarize_canvas_command_result({
        'ok': True, 'canvas_apply_status': 'applied', 'applied': True,
        'project_id': 'p', 'canvas_id': 'c', 'command_results': [{
            'type': 'production_plan_command', 'status': 'success',
            'output': {'plan_ref': {'artifact_id': 'plan', 'revision': 4},
                       'command_id': 'first',
                       'executed_commands': [{'kind': 'edit_turn', 'target_id': 'turn'},
                                             {'kind': 'edit_beat', 'target_id': 'beat'}]}}]},
        bridge_key='bridge', commands=[{'type': 'production_plan_command',
            'node_id': 'node', 'command': call()['command']}])
    assert result['production_plan_ref'] == {'artifact_id': 'plan', 'revision': 4}
    assert result['production_command_id'] == 'first'


def test_production_edit_receipt_requires_exact_saved_plan_revision():
    from types import SimpleNamespace
    from novelvideo.chat.runtime_event_evidence import production_edit_saved_receipt

    arguments = call()
    event = SimpleNamespace(name='freezone_prepare_production_edit', input=arguments,
                            status='completed', error=None, output=None,
                            structured={'ok': True, 'project_id': 'p', 'canvas_id': 'c',
                                        'canvas_apply_status': 'applied', 'applied': True,
                                        'bridge_key': 'bridge',
                                        'production_plan_ref': {'artifact_id': 'plan', 'revision': 4},
                                        'production_command_id': arguments['command']['command_id'],
                                        'production_command_steps': [
                                            {'kind': 'edit_turn', 'target_id': 'turn'},
                                            {'kind': 'edit_beat', 'target_id': 'beat'}]})
    assert production_edit_saved_receipt(event, expected_project='p', expected_canvas='c')
    event.structured['production_plan_ref']['revision'] = 5
    assert production_edit_saved_receipt(event, expected_project='p', expected_canvas='c') is None
    event.structured['production_plan_ref']['revision'] = 4
    event.structured['production_command_id'] = 'other'
    assert production_edit_saved_receipt(event, expected_project='p', expected_canvas='c') is None
    event.structured['production_command_id'] = arguments['command']['command_id']
    event.structured['canvas_apply_status'] = 'accepted'
    assert production_edit_saved_receipt(event, expected_project='p', expected_canvas='c') is None


def test_rejection_token_is_ignored_when_runtime_reports_an_error():
    from types import SimpleNamespace
    from novelvideo.chat.runtime_event_evidence import production_edit_rejection

    event = SimpleNamespace(name='freezone_prepare_production_edit', status='failed',
        error='transport lost', output=None, structured={'ok': False, 'project_id': 'p',
        'canvas_id': 'c', 'side_effects': 'none', 'retry_token': 'token',
        'error_path': 'target_id', 'missing_fields': ['target_id']})
    assert production_edit_rejection(event, expected_project='p', expected_canvas='c') is None
    event.error = None
    event.structured.update(error='tool_arguments_invalid', phase='tool_validation')
    assert production_edit_rejection(event, expected_project='p', expected_canvas='c') is event.structured


@pytest.mark.anyio
async def test_actual_mcp_schema_rejection_mints_host_scoped_retry_token(monkeypatch):
    from novelvideo.chat import dramaclaw_mcp

    monkeypatch.setenv('DRAMACLAW_TOOL_MODE', 'freezone_canvas')
    monkeypatch.setenv('DRAMACLAW_PROJECT_ID', 'trusted-project')
    monkeypatch.setenv('DRAMACLAW_CANVAS_ID', 'trusted-canvas')
    monkeypatch.setenv('DRAMACLAW_TURN_ID', 'trusted-turn')
    args = call(command={**call()['command'], 'target_id': None}, canvas='untrusted-canvas')
    result = await dramaclaw_mcp.call_tool('freezone_prepare_production_edit', args)
    assert result.isError
    payload = result.structuredContent
    assert payload['side_effects'] == 'none'
    assert payload['project_id'] == 'trusted-project'
    assert payload['canvas_id'] == 'trusted-canvas'
    assert payload['retry_token']
    monkeypatch.delenv('DRAMACLAW_TURN_ID')
    unbound = await dramaclaw_mcp.call_tool('freezone_prepare_production_edit', args)
    assert unbound.isError
    assert 'retry_token' not in unbound.structuredContent


@pytest.mark.anyio
@pytest.mark.parametrize('case', ['valid', 'changed_value', 'unlinked',
                                  'wrong_revision', 'separate_failure',
                                  'multi_field_chain', 'illegal_intermediate'])
async def test_service_settles_two_real_shape_rejections_after_saved_edit(monkeypatch, tmp_path, case):
    import json
    from types import SimpleNamespace
    from novelvideo.chat import service, hermes_sdk, dramaclaw_mcp
    from novelvideo.chat.store import ChatScope
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.freezone.remix.production_commands import apply_production_command
    from test_production_plan import native_plan

    monkeypatch.setenv('NOVELVIDEO_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('NOVELVIDEO_RUNTIME_DIR', str(tmp_path / 'runtime'))
    monkeypatch.setenv('DRAMACLAW_TOOL_MODE', 'freezone_canvas')
    monkeypatch.setenv('DRAMACLAW_PROJECT_ID', 'project-a')
    monkeypatch.setenv('DRAMACLAW_CANVAS_ID', 'canvas-a')
    monkeypatch.setenv('DRAMACLAW_TURN_ID', 'business-turn')
    async def authorize(**kwargs): return None
    async def token(*args, **kwargs): return 'agent-token'
    class Auth:
        async def revoke_agent_session(self, value): pass
    native_store = RemixArtifactStore(tmp_path / 'native-project', project_id='project-a',
                                      created_by='admin')
    saved = native_store.create('production_plan.v2', native_plan(), [])
    original_ref = {'artifact_id': saved['id'], 'revision': saved['revision']}
    bad_command = {'command_id': 'first', 'plan_ref': original_ref,
                   'kind': 'edit_beat', 'payload': {'purpose': 'Keep this'}}
    if case in {'multi_field_chain', 'illegal_intermediate'}:
        bad_command['payload'] = {'purpose': 'Keep this', 'duration_frames': '72'}
    def event(call_id, command, payload, retry_of=None, raw_result=None):
        return SimpleNamespace(type='tool_updated', text='', name='dramaclaw.freezone_prepare_production_edit',
            call_id=call_id, status=('failed' if payload.get('status') == 'tool_arguments_invalid' else 'completed'),
            error=None, output=({'content': [{'type': 'text', 'text': raw_result.content[0].text}]}
                                if raw_result is not None else None),
            input={'project_id': 'project-a', 'canvas_id': 'canvas-a', 'node_id': 'plan-node',
                   'command': command, **({'retry_of': retry_of} if retry_of else {})}, structured=payload)
    successful = {**bad_command, 'command_id': 'third', 'target_id': 'hook'}
    if case in {'multi_field_chain', 'illegal_intermediate'}:
        successful['payload'] = {'purpose': ('Altered' if case == 'illegal_intermediate' else 'Keep this'),
                                 'duration_frames': 72}
    native_receipt = apply_production_command(native_store, successful)
    assert native_receipt['command_id'] == successful['command_id']
    assert native_receipt['executed_commands'] == [{'kind': 'edit_beat', 'target_id': 'hook'}]
    assert native_store.read_latest(saved['id'])['revision'] == native_receipt['plan_ref']['revision']
    first_args = {'project_id': 'project-a', 'canvas_id': 'canvas-a',
                  'node_id': 'plan-node', 'command': bad_command}
    first_result = await dramaclaw_mcp.call_tool('freezone_prepare_production_edit', first_args)
    first_error = first_result.structuredContent
    second_command = {**bad_command, 'command_id': 'second'}
    if case in {'multi_field_chain', 'illegal_intermediate'}:
        second_command['target_id'] = 'hook'
    if case == 'illegal_intermediate':
        second_command['payload'] = {'purpose': 'Altered', 'duration_frames': '72'}
    second_result = await dramaclaw_mcp.call_tool(
        'freezone_prepare_production_edit', {**first_args,
        'command': second_command, 'retry_of': first_error['retry_token']})
    second_error = second_result.structuredContent
    assert first_error['side_effects'] == second_error['side_effects'] == 'none'
    if case in {'multi_field_chain', 'illegal_intermediate'}:
        assert first_error['error_path'] == 'target_id'
        assert second_error['error_path'] == 'payload.duration_frames'
    events = [event('bad-1', bad_command, first_error, raw_result=first_result),
              event('bad-2', second_command, second_error, first_error['retry_token'],
                    raw_result=second_result),
              event('good', successful, {'ok': True, 'project_id': 'project-a',
                  'canvas_id': 'canvas-a', 'canvas_apply_status': 'applied', 'applied': True,
                  'bridge_key': 'bridge-good', 'production_plan_ref': native_receipt['plan_ref'],
                  'production_command_id': native_receipt['command_id'],
                  'production_command_steps': native_receipt['executed_commands']},
                  second_error['retry_token'])]
    if case == 'changed_value':
        events[-1].input['command']['payload'] = {'purpose': 'Changed business value'}
    elif case == 'unlinked':
        events[-1].input['retry_of'] = 'unknown-token'
    elif case == 'wrong_revision':
        events[-1].structured['production_plan_ref']['revision'] += 1
    elif case == 'separate_failure':
        events.append(event('other', {**bad_command, 'target_id': 'other'},
                            {'ok': False, 'project_id': 'project-a', 'canvas_id': 'canvas-a',
                             'canvas_apply_status': 'failed', 'error': 'other edit failed'}))
    answer = json.dumps({'message': '作品方案已更新。', 'mode': 'mutation',
                         'canvas_receipts': [{'bridge_key': 'bridge-good', 'revision': None}]})
    class Thread:
        async def stream(self, prompt):
            yield SimpleNamespace(type='thread_started', thread_id='thread', turn_id='turn')
            for row in events:
                yield row
            yield SimpleNamespace(type='turn_completed', thread_id='thread', turn_id='turn',
                                  status='completed', disposition='completed', error=None)
            yield SimpleNamespace(type='complete', thread_id='thread', text=answer)
    monkeypatch.setattr(service, 'authorize_hermes_launch', authorize)
    monkeypatch.setattr(service, '_create_page_agent_session_token', token)
    monkeypatch.setattr(service, '_build_codex_thread', lambda *a, **k: Thread())
    monkeypatch.setattr(service, 'get_auth_session_port', lambda: Auth())
    monkeypatch.setattr(hermes_sdk, '_issue_turn_capability', lambda **k: None)
    seen = []
    async def collect(value): seen.append(value)
    scope = ChatScope(kind='project', id='project-a', surface='freezone', canvas_id='canvas-a',
                      agent_id='main', state_dir=str(tmp_path / 'state' / 'admin' / 'project-a'))
    result = await service._stream_assistant_reply_codex('admin', 'project-a', 'Edit', collect,
        project_state_dir=tmp_path / 'state' / 'admin' / 'project-a', tool_mode='freezone_canvas',
        surface_context={'freezone_canvas_id': 'canvas-a'}, store_scope=scope,
        turn_id='business-turn', route_prompt='Edit')
    recorded = [row for row in seen if row.get('type') == 'tool_updated']
    assert [row['call_id'] for row in recorded[:3]] == ['bad-1', 'bad-2', 'good']
    assert recorded[0]['result_json']['ok'] is False
    assert recorded[1]['result_json']['ok'] is False
    if case in {'valid', 'multi_field_chain'}:
        assert result['content'] == '作品方案已更新。'
    else:
        assert result['content'] != '作品方案已更新。'
        assert '未完成' in result['content']
