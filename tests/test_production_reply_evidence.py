from types import SimpleNamespace

import pytest

from novelvideo.chat.runtime_event_evidence import (
    _codex_freezone_is_non_canvas_write_event, _codex_freezone_non_canvas_receipt,
)
from novelvideo.chat.operation_outcome import logical_operation_key


def event(payload):
    return SimpleNamespace(name='freezone_prepare_production_nodes', status='completed',
                           structured=payload, output=None, error=None,
                           input={'commission_ref': {'artifact_id': 'commission', 'revision': 2},
                                  'goal': '访谈完整目标', 'run_after_create': True})


def test_prepare_failure_is_execution_attempt_before_any_canvas_write():
    assert _codex_freezone_is_non_canvas_write_event(event({'ok': False, 'error': 'bad reference'}))


def test_admission_alone_is_not_native_preparation_receipt():
    assert _codex_freezone_non_canvas_receipt(event({'ok': True, 'project_id': 'p', 'canvas_id': 'c'}),
                                             expected_project='p', expected_canvas='c') is None


def test_saved_native_draft_is_valid_without_canvas_apply():
    result = {'ok': True, 'project_id': 'p', 'canvas_id': 'c', 'draft_id': 'draft',
              'revision': 1, 'run_after_create': True,
              'native_plan_stage': {'state': 'awaiting_canvas_receipt', 'draft_id': 'draft', 'slots': []}}
    assert _codex_freezone_non_canvas_receipt(event(result), expected_project='p', expected_canvas='c') == result


def test_native_retry_identity_keeps_commission_goal_and_execution_policy():
    args = event({}).input
    def key(value):
        return logical_operation_key('freezone_prepare_production_nodes', value, project='p', canvas='c', agent='main')
    assert key(args)
    assert key(args) != key({**args, 'run_after_create': False})
    assert key(args) != key({**args, 'goal': '另一个目标'})
    assert key(args) != key({**args, 'commission_ref': {'artifact_id': 'other', 'revision': 2}})


def test_native_final_reply_uses_host_receipt_instead_of_model_success():
    from novelvideo.chat.canvas_outcome import production_reply_from_evidence
    assert production_reply_from_evidence({'ok': False, 'error': 'analysis binding missing'}) == '作品方案操作未完成：analysis binding missing'
    receipt = {'ok': True, 'canvas_apply_status': 'applied', 'applied': True, 'bridge_key': 'real',
               'project_id': 'p', 'canvas_id': 'c'}
    assert '已生成' not in production_reply_from_evidence(receipt)
    assert '受理' in production_reply_from_evidence(receipt)


def test_drafting_phase_without_task_cannot_project_running():
    from novelvideo.freezone.remix.production_state import native_plan_execution_status
    assert native_plan_execution_status({'state': 'unprepared', 'slots': []}) == 'waiting_agent'
    assert native_plan_execution_status({'state': 'running', 'slots': [{'state': 'running', 'task_id': None}]}) != 'running'
    assert native_plan_execution_status({'state': 'running', 'slots': [{'state': 'running', 'task_id': 'actual'}]}) == 'running'


async def _native_chat(monkeypatch, tmp_path, results):
    import json
    from novelvideo.chat import service, hermes_sdk
    from novelvideo.chat.store import ChatScope
    monkeypatch.setenv('NOVELVIDEO_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('NOVELVIDEO_RUNTIME_DIR', str(tmp_path / 'runtime'))
    prompts, visible = [], []
    async def authorize(**kwargs):
        return None
    async def token(*args, **kwargs):
        return 'token'
    class Auth:
        async def revoke_agent_session(self, token):
            pass
    class Thread:
        async def stream(self, prompt):
            prompts.append(prompt)
            yield SimpleNamespace(type='thread_started', thread_id='thread', turn_id='t')
            for index, payload in enumerate(results):
                arguments = None
                if isinstance(payload, tuple):
                    payload, arguments = payload
                tool = event(payload)
                if arguments is not None:
                    tool.input = arguments
                tool.type, tool.call_id, tool.text = 'tool_updated', str(index), 'native result'
                yield tool
            yield SimpleNamespace(type='turn_completed', thread_id='thread', turn_id='t',
                                  status='completed', disposition='completed', error=None)
            yield SimpleNamespace(type='complete', thread_id='thread', text=json.dumps({
                'message': 'Everything generated successfully', 'mode': 'read_only', 'canvas_receipts': []}))
    monkeypatch.setattr(service, 'authorize_hermes_launch', authorize)
    monkeypatch.setattr(service, '_create_page_agent_session_token', token)
    monkeypatch.setattr(service, '_build_codex_thread', lambda *args, **kwargs: Thread())
    monkeypatch.setattr(service, 'get_auth_session_port', lambda: Auth())
    monkeypatch.setattr(hermes_sdk, '_issue_turn_capability', lambda **kwargs: None)
    async def collect(value):
        visible.append(value)
    result = await service._stream_assistant_reply_codex(
        'admin', 'p', 'Prepare production', collect,
        project_state_dir=tmp_path/'state/admin/p', tool_mode='freezone_canvas',
        surface_context={'freezone_canvas_id': 'c'},
        store_scope=ChatScope(kind='project', id='p', surface='freezone', canvas_id='c',
                             agent_id='main', state_dir=str(tmp_path/'state/admin/p')),
        turn_id='business', route_prompt='Prepare production')
    return result, prompts, visible


@pytest.mark.anyio
async def test_chat_native_prepare_failure_is_recorded_separately_from_prose(monkeypatch, tmp_path):
    result, prompts, visible = await _native_chat(monkeypatch, tmp_path, [{'ok': False, 'error': 'bad binding'}])
    assert 'Everything generated' in result['content']
    outcome = result['metadata']['operation_outcome']
    assert outcome['blocking_call_ids'] == ['0']
    assert outcome['operations'][0]['status'] == 'failed'
    assert outcome['operations'][0]['error_code'] == 'bad binding'
    assert any(item.get('type') == 'tool_updated' and item.get('text') == 'bad binding' for item in visible)
    assert len(prompts) == 1


@pytest.mark.anyio
async def test_chat_native_creation_keeps_prose_and_verified_operation(monkeypatch, tmp_path):
    result, prompts, visible = await _native_chat(monkeypatch, tmp_path, [{
        'ok': True, 'project_id': 'p', 'canvas_id': 'c', 'canvas_apply_status': 'applied',
        'applied': True, 'bridge_key': 'real-receipt'}])
    assert 'Everything generated' in result['content']
    outcome = result['metadata']['operation_outcome']
    assert outcome['blocking_call_ids'] == []
    assert all(item['verified'] and item['status'] == 'completed' for item in outcome['operations'])
    assert len(prompts) == 1


@pytest.mark.anyio
async def test_chat_corrected_same_request_clears_old_prepare_failure(monkeypatch, tmp_path):
    result, prompts, visible = await _native_chat(monkeypatch, tmp_path, [
        {'ok': False, 'error': 'bad binding'},
        {'ok': True, 'project_id': 'p', 'canvas_id': 'c', 'canvas_apply_status': 'applied',
         'applied': True, 'bridge_key': 'real-receipt'}])
    outcome = result['metadata']['operation_outcome']
    assert outcome['blocking_call_ids'] == []
    assert {item['call_id'] for item in outcome['operations']} == {'1'}


@pytest.mark.anyio
async def test_chat_saved_draft_receipt_is_not_generation_evidence(monkeypatch, tmp_path):
    result, prompts, visible = await _native_chat(monkeypatch, tmp_path, [{
        'ok': True, 'project_id': 'p', 'canvas_id': 'c', 'draft_id': 'draft', 'revision': 1,
        'run_after_create': True,
        'native_plan_stage': {'state': 'awaiting_canvas_receipt', 'draft_id': 'draft', 'slots': []}}])
    outcome = result['metadata']['operation_outcome']
    assert outcome['blocking_call_ids'] == []
    assert len(outcome['operations']) == 1
    assert outcome['operations'][0]['category'] == 'artifact'
    assert outcome['operations'][0]['verified'] is True
    assert len(prompts) == 1


@pytest.mark.anyio
async def test_chat_unrelated_success_cannot_clear_prepare_failure(monkeypatch, tmp_path):
    unrelated = {**event({}).input, 'commission_ref': {'artifact_id': 'other', 'revision': 2}}
    result, _, _ = await _native_chat(monkeypatch, tmp_path, [
        {'ok': False, 'error': 'original binding failure'},
        ({'ok': True, 'project_id': 'p', 'canvas_id': 'c', 'canvas_apply_status': 'applied',
          'applied': True, 'bridge_key': 'unrelated-receipt'}, unrelated)])
    outcome = result['metadata']['operation_outcome']
    assert outcome['blocking_call_ids'] == ['0']
    assert any(item['call_id'] == '0' and item['error_code'] == 'original binding failure'
               for item in outcome['operations'])
    assert any(item['call_id'] == '1' and item['status'] == 'completed'
               for item in outcome['operations'])
