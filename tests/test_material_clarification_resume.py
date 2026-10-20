"""The host keeps one real parameter card wait without blocking its tool call."""
import time
from types import SimpleNamespace

import pytest

from novelvideo.chat.service import _bind_server_observed_agent_product_execution
from novelvideo.freezone import agent_product_operations as operations
from test_agent_product_turn_settlement import _admitted


@pytest.mark.asyncio
async def test_host_binds_only_scoped_saved_parameter_wait(tmp_path, monkeypatch):
    from novelvideo.api.routes import chat
    from novelvideo.freezone import canvas_command_bridge

    operation, _, _ = _admitted(tmp_path, turn_id='reference-remix-business-a')
    saved = {
        'key': 'bridge-a', 'kind': 'clarification_event',
        'project_id': 'project-a', 'canvas_id': 'canvas-a',
        'created_at': time.time(), 'transport_status': 'pending',
        'producer_binding': {
            'username': 'alice', 'project_id': 'project-a', 'canvas_id': 'canvas-a',
            'agent_id': 'main', 'turn_id': 'reference-remix-business-a',
        },
        'event': {'type': 'assistant.clarification.request',
                  'clarification_id': 'clarify-a',
                  'questions': [{'id': 'image_model'}]},
    }
    monkeypatch.setattr(chat, '_canvas_bridge_dir', lambda *_args, **_kwargs: tmp_path)
    monkeypatch.setattr(canvas_command_bridge, 'read_bridge_message',
                        lambda *_args, **_kwargs: saved)
    event = SimpleNamespace(
        type='tool_updated', status='completed', error=None,
        name='dramaclaw.freezone_request_user_clarification',
        structured={'ok': True, 'status': 'waiting_user',
                    'bridge_key': 'bridge-a', 'clarification_id': 'clarify-a'},
        output=None,
    )
    await _bind_server_observed_agent_product_execution(
        event, project_dir=tmp_path, project_state_dir=tmp_path,
        business_turn_id='reference-remix-business-a', expected_project_id='project-a',
        expected_canvas_id='canvas-a', requester_username='alice', agent_id='main',
    )
    stored = operations.read_agent_product_operation(
        project_dir=tmp_path, operation_id=operation['operation_id'])
    assert stored['waiting_receipt']['id'] == 'bridge-a'
    assert stored['waiting_reason'] == 'user_confirmation'

    saved['producer_binding']['turn_id'] = 'other-turn'
    event.structured['bridge_key'] = 'bridge-b'
    await _bind_server_observed_agent_product_execution(
        event, project_dir=tmp_path, project_state_dir=tmp_path,
        business_turn_id='reference-remix-business-a', expected_project_id='project-a',
        expected_canvas_id='canvas-a', requester_username='alice', agent_id='main',
    )
    assert operations.read_agent_product_operation(
        project_dir=tmp_path, operation_id=operation['operation_id'],
    )['waiting_receipt']['id'] == 'bridge-a'
