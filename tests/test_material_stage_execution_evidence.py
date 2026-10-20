"""Native material wrappers bind only real server-observed admitted tool calls."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from test_agent_product_operations import _create
from novelvideo.chat import service, tool_policy
from novelvideo.freezone.agent_product_operations import read_agent_product_operation, finish_agent_product_operation


MATERIAL_TOOLS = ('freezone_prepare_material_work', 'freezone_prepare_material_stage')


def event(tool, operation, **overrides):
    values = dict(type='tool_started', name='dramaclaw.' + tool, status='pending', error=None,
        turn_id='actual-server-turn', call_id='actual-server-call', structured=None, output=None,
        input={'operation_id': operation['operation_id'], 'work_ref': {'artifact_id': 'saved-work', 'revision': 2},
               'model_call_id': 'caller-claim', 'turn_id': 'forged-turn', 'tool_call_id': 'forged-call'})
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize('tool', MATERIAL_TOOLS)
@pytest.mark.parametrize('phase', ['tool_started', 'tool_updated'])
async def test_real_material_call_binds_the_existing_trusted_execution_path(tmp_path, tool, phase):
    operation = _create(tmp_path, key=tool + phase)
    current = event(tool, operation)
    if phase == 'tool_updated':
        current.type = phase
        current.status = 'completed'
        current.structured = {'ok': True}
    original = deepcopy(current.input)
    await service._bind_server_observed_agent_product_execution(current, project_dir=tmp_path, project_state_dir=tmp_path, business_turn_id='actual-server-turn')
    saved = read_agent_product_operation(project_dir=tmp_path, operation_id=operation['operation_id'])
    assert saved['model_evidence'] == {'model_call_id': 'agent-turn:actual-server-turn:tool:actual-server-call',
        'executed_at': saved['model_evidence']['executed_at'], 'source': 'server_observed_agent_turn',
        'turn_id': 'actual-server-turn', 'tool_call_id': 'actual-server-call'}
    assert saved['model_evidence']['executed_at'] > 0
    assert saved['status'] == operation['status'] and saved['result_ref'] == {}
    assert current.input == original


@pytest.mark.asyncio
@pytest.mark.parametrize('tool', MATERIAL_TOOLS)
@pytest.mark.parametrize('change', ['failed', 'error', 'no-success', 'no-turn', 'no-call', 'no-operation', 'response-only'])
async def test_incomplete_or_failed_material_events_do_not_bind(tmp_path, tool, change):
    operation = _create(tmp_path, key=tool + change)
    current = event(tool, operation)
    if change == 'failed': current.type, current.status, current.structured = 'tool_updated', 'failed', {'ok': True}
    elif change == 'error': current.error = {'message': 'native failed'}
    elif change == 'no-success': current.type, current.status, current.structured = 'tool_updated', 'completed', {'ok': False}
    elif change == 'no-turn': current.turn_id = ''
    elif change == 'no-call': current.call_id = None
    elif change == 'no-operation': current.input.pop('operation_id')
    else:
        current.type, current.status = 'tool_updated', 'completed'
        current.input = {'work_ref': {'artifact_id': 'saved-work', 'revision': 2}}
        current.structured = {'ok': True, 'operation_id': operation['operation_id'], 'model_call_id': 'caller-claim'}
    await service._bind_server_observed_agent_product_execution(current, project_dir=tmp_path, project_state_dir=tmp_path, business_turn_id='actual-server-turn')
    assert not read_agent_product_operation(project_dir=tmp_path, operation_id=operation['operation_id'])['model_evidence']


@pytest.mark.asyncio
@pytest.mark.parametrize('tool', MATERIAL_TOOLS)
@pytest.mark.parametrize('change', ['foreign-state', 'terminal'])
async def test_existing_state_root_and_terminal_operation_guards_remain(tmp_path, tool, change):
    own = tmp_path / 'own'
    operation = _create(own, key=tool + change)
    state = own
    if change == 'terminal':
        finish_agent_product_operation(project_dir=own, operation_id=operation['operation_id'], outcome='cancelled', expected_task_id='')
    else: state = tmp_path / 'peer'
    await service._bind_server_observed_agent_product_execution(event(tool, operation), project_dir=own, project_state_dir=state, business_turn_id='actual-server-turn')
    assert not read_agent_product_operation(project_dir=own, operation_id=operation['operation_id'])['model_evidence']


@pytest.mark.asyncio
@pytest.mark.parametrize('tool', ['freezone_begin_agent_product_generation', 'freezone_get_material_work', 'unknown_material_tool'])
async def test_admission_reads_and_unknown_tools_are_not_execution_evidence(tmp_path, tool):
    operation = _create(tmp_path, key=tool)
    await service._bind_server_observed_agent_product_execution(event(tool, operation), project_dir=tmp_path, project_state_dir=tmp_path, business_turn_id='actual-server-turn')
    assert not read_agent_product_operation(project_dir=tmp_path, operation_id=operation['operation_id'])['model_evidence']


def test_workflow_result_observer_has_exact_shared_five_tool_set():
    expected = {'freezone_prepare_workflow', 'freezone_prepare_workflow_draft', 'freezone_prepare_workflow_plan_draft', *MATERIAL_TOOLS}
    assert tool_policy.AGENT_WORKFLOW_RESULT_TOOLS == expected
    assert service._AGENT_WORKFLOW_RESULT_TOOLS is tool_policy.AGENT_WORKFLOW_RESULT_TOOLS
    assert tool_policy.AGENT_PRODUCT_RESULT_TOOLS == expected | {'freezone_put_agent_catalog_skill', 'freezone_put_agent_catalog_recipe'}
