"""New director tools route through runtime policy and authentic durable outcomes."""
from copy import deepcopy
import json

import pytest

from test_codex_director_receipts import codex_turn as codex_turn, tool
from test_freezone_plugin import _load_plugin_module, _restore_tools_registry_modules as _restore_tools_registry_modules
from test_production_advancement import production as production, current, passed
from novelvideo.chat import hermes_sdk, runtime_event_evidence as evidence, tool_policy


READ_TOOLS = ('freezone_get_production_progress', 'freezone_get_material_speech_review')
WRITE_TOOLS = ('freezone_select_material_work', 'freezone_advance_production')


def arguments(name):
    if name == 'freezone_advance_production':
        return {'commission_ref': {'artifact_id': 'commission', 'revision': 2},
                'expected_projection_revision': 'a' * 64, 'command_id': 'advance-1'}
    if name == 'freezone_select_material_work':
        return {'commission_ref': {'artifact_id': 'commission', 'revision': 2},
                'work_ref': {'artifact_id': 'work', 'revision': 3}, 'command_id': 'select-1'}
    if name == 'freezone_get_production_progress':
        return {'commission_id': 'commission'}
    return {'work_ref': {'artifact_id': 'work', 'revision': 3}, 'step_id': 'clip', 'node_id': 'candidate',
            'observation_ref': {'artifact_id': 'observation', 'revision': 1}}


def payload(name):
    result = {'ok': True, 'status': 'ready', 'project_id': 'project-a', 'canvas_id': 'canvas-a',
              'commission_ref': {'artifact_id': 'commission', 'revision': 3},
              'work_ref': {'artifact_id': 'work', 'revision': 3},
              'plan_ref': {'artifact_id': 'plan', 'revision': 4}, 'phase': 'preparing_materials'}
    if name == 'freezone_advance_production':
        result.update(schema_version='reference_production_state.v1',
            scope={'project_id': 'project-a', 'canvas_id': 'canvas-a', 'agent_id': 'main'},
            projection_revision='b' * 64, execution_status='waiting_agent',
            advancement={'action': {'owner': 'system', 'kind': 'bind_materials', 'action_id': 'binding:work'},
                         'replayed': False, 'result': {'plan_ref': deepcopy(result['plan_ref']), 'work_ref': deepcopy(result['work_ref'])}})
    return result


@pytest.mark.parametrize('name', READ_TOOLS + WRITE_TOOLS)
def test_published_tools_have_correct_runtime_classification(name):
    plugin = _load_plugin_module()
    assert name in {name for name, *_ in plugin.TOOLS}
    event = tool(name, {'ok': True})
    assert not evidence._codex_freezone_is_write_event(event)
    assert evidence._codex_freezone_is_non_canvas_write_event(event) is (name in WRITE_TOOLS)
    assert (name in tool_policy.FREEZONE_ARTIFACT_WRITE_TOOLS) is (name in WRITE_TOOLS)
    assert name not in tool_policy.AGENT_PRODUCT_RESULT_TOOLS


@pytest.mark.parametrize('name', READ_TOOLS + WRITE_TOOLS)
@pytest.mark.parametrize('encoded', [False, True])
def test_generic_tool_router_resolves_four_actual_names_without_losing_cas(name, encoded):
    args = arguments(name)
    envelope = {'name': name, 'arguments': json.dumps(args) if encoded else args}
    routed = hermes_sdk._reference_routed_tool('tool_call', json.dumps(envelope) if encoded else envelope)
    assert routed == (name, args)
    assert hermes_sdk._reference_routed_tool('freezone_get_node_detail', envelope) == ('freezone_get_node_detail', envelope)


@pytest.mark.parametrize('name', WRITE_TOOLS)
def test_actual_durable_write_result_requires_no_fabricated_canvas_receipt(name):
    native = payload(name)
    event = tool(name, native, arguments=arguments(name))
    assert evidence._codex_freezone_non_canvas_receipt(event, expected_project='project-a', expected_canvas='canvas-a') is native
    assert evidence._codex_freezone_write_receipt(event) is None
    assert 'canvas_receipt' not in native and 'canvas_apply_status' not in native


def test_revised_material_strategy_receipt_requires_same_work_and_new_revision():
    name = 'freezone_revise_material_strategy'
    assert name in tool_policy.FREEZONE_ARTIFACT_WRITE_TOOLS
    request = {'work_ref': {'artifact_id': 'work', 'revision': 3},
               'replacements': [{'id': 'clip'}], 'command_id': 'change-source'}
    native = {'ok': True, 'status': 'ready', 'project_id': 'project-a', 'canvas_id': 'canvas-a',
              'work_ref': {'artifact_id': 'work', 'revision': 4},
              'changed_step_ids': ['clip'], 'affected_step_ids': ['clip']}
    assert evidence._codex_freezone_non_canvas_receipt(tool(name, native, arguments=request),
        expected_project='project-a', expected_canvas='canvas-a') is native
    assert evidence._codex_freezone_non_canvas_receipt(tool(name, {**native,
        'work_ref': {'artifact_id': 'another', 'revision': 4}}, arguments=request),
        expected_project='project-a', expected_canvas='canvas-a') is None
    assert evidence._codex_freezone_non_canvas_receipt(tool(name, {**native,
        'work_ref': {'artifact_id': 'work', 'revision': 2}}, arguments=request),
        expected_project='project-a', expected_canvas='canvas-a') is None


def test_select_work_receipt_requires_its_current_plan_ref():
    native = payload('freezone_select_material_work')
    native.pop('plan_ref')
    assert evidence._codex_freezone_non_canvas_receipt(tool('freezone_select_material_work', native,
        arguments=arguments('freezone_select_material_work')), expected_project='project-a', expected_canvas='canvas-a') is None


@pytest.mark.parametrize('key', ['plan_ref', 'work_ref'])
def test_advance_refuses_action_result_from_another_current_work_or_plan(key):
    native = payload('freezone_advance_production')
    native['advancement']['result'][key]['artifact_id'] = 'another-' + key
    assert evidence._codex_freezone_non_canvas_receipt(tool('freezone_advance_production', native,
        arguments=arguments('freezone_advance_production')), expected_project='project-a', expected_canvas='canvas-a') is None


@pytest.mark.parametrize('name', WRITE_TOOLS)
@pytest.mark.parametrize('mutation', ['missing_ref', 'wrong_project', 'wrong_canvas', 'wrong_request', 'boolean_revision', 'failed'])
def test_new_write_receipt_rejects_missing_or_unrelated_evidence(name, mutation):
    native = payload(name)
    args = arguments(name)
    if mutation == 'missing_ref':
        native.pop('commission_ref')
    elif mutation == 'wrong_project':
        native['project_id'] = 'peer'
    elif mutation == 'wrong_canvas':
        native['canvas_id'] = 'peer'
    elif mutation == 'wrong_request':
        args['commission_ref']['artifact_id'] = 'other-commission'
    elif mutation == 'boolean_revision':
        native['work_ref']['revision'] = True
    else:
        native['ok'] = False
        native['error'] = 'native projection conflict'
    event = tool(name, native, arguments=args)
    assert evidence._codex_freezone_non_canvas_receipt(event, expected_project='project-a', expected_canvas='canvas-a') is None


@pytest.mark.parametrize('mutation', ['wrong_scope', 'missing_projection', 'invalid_request_projection', 'wrong_actor', 'missing_action_result', 'fake_result_ref', 'incomplete_task'])
def test_advance_requires_current_projection_actor_and_native_action_result(mutation):
    native = payload('freezone_advance_production')
    args = arguments('freezone_advance_production')
    if mutation == 'wrong_scope':
        native['scope']['canvas_id'] = 'peer'
    elif mutation == 'missing_projection':
        native.pop('projection_revision')
    elif mutation == 'invalid_request_projection':
        args['expected_projection_revision'] = 'guessed'
    elif mutation == 'wrong_actor':
        native['scope']['agent_id'] = 'peer-agent'
    elif mutation == 'missing_action_result':
        native['advancement'].pop('result')
    elif mutation == 'fake_result_ref':
        native['advancement']['result']['plan_ref']['project_id'] = 'peer'
    else:
        native['advancement']['result'] = {'operation_id': 'native-op', 'task_id': None}
    assert evidence._codex_freezone_non_canvas_receipt(tool('freezone_advance_production', native, arguments=args),
        expected_project='project-a', expected_canvas='canvas-a', expected_agent='main') is None


def test_no_action_waiting_user_is_valid_native_state_without_claiming_an_action():
    native = payload('freezone_advance_production')
    native['plan_ref'] = native['work_ref'] = None
    native['phase'] = 'awaiting_direction'
    native['execution_status'] = 'waiting_user'
    native['advancement'] = {'action': None, 'replayed': False}
    assert evidence._codex_freezone_non_canvas_receipt(tool('freezone_advance_production', native,
        arguments=arguments('freezone_advance_production')), expected_project='project-a', expected_canvas='canvas-a') is native


@pytest.mark.anyio
@pytest.mark.parametrize('name', WRITE_TOOLS)
async def test_codex_turn_accepts_durable_current_outcome_without_canvas_receipt(codex_turn, name):
    run, _ = codex_turn
    message = '当前素材工作已保存。'
    result = await run([tool(name, payload(name), arguments=arguments(name))], message)
    assert result == message


@pytest.mark.anyio
@pytest.mark.parametrize('name', WRITE_TOOLS)
async def test_codex_turn_refuses_unverified_new_tool_success(codex_turn, name):
    run, _ = codex_turn
    result = await run([tool(name, {'ok': True}, arguments=arguments(name))], '已经完成。')
    assert result == '已经完成。'
    outcome = run.last_result['metadata']['operation_outcome']
    assert outcome['blocking_call_ids']
    assert any(item['name'] == name and not item['verified'] for item in outcome['operations'])


@pytest.mark.anyio
async def test_codex_turn_uses_trusted_actor_for_production_receipt(codex_turn):
    run, _ = codex_turn
    native = payload('freezone_advance_production')
    native['scope']['agent_id'] = 'peer-agent'
    result = await run([tool('freezone_advance_production', native, arguments=arguments('freezone_advance_production'))], '已经完成。')
    assert result == '已经完成。'
    outcome = run.last_result['metadata']['operation_outcome']
    assert outcome['blocking_call_ids']
    assert any(item['name'] == 'freezone_advance_production' and not item['verified']
               for item in outcome['operations'])


@pytest.mark.anyio
async def test_codex_progress_read_with_zero_branches_is_not_a_write(codex_turn):
    run, _ = codex_turn
    native = {'ok': True, 'status': 'ready', 'project_id': 'project-a', 'canvas_id': 'canvas-a',
              'branches': [], 'selection_required': False, 'inventory_truncated': False}
    assert await run([tool('freezone_get_production_progress', native, arguments={})], '当前没有已保存的制作。') == '当前没有已保存的制作。'


def test_real_native_bind_outcome_verifies_same_work_without_browser_receipt(production):
    import asyncio
    from novelvideo.freezone.remix.production_advancement import advance_production
    ctx, _, _, _, _ = production
    passed(production)
    first = current(production)
    adopted = asyncio.run(advance_production(ctx, canvas_id='c', commission_ref=first['commission_ref'],
        expected_projection_revision=first['projection_revision'], command_id='runtime-adopt'))
    args = {'commission_ref': adopted['commission_ref'], 'expected_projection_revision': adopted['projection_revision'],
            'command_id': 'runtime-bind'}
    bound = asyncio.run(advance_production(ctx, canvas_id='c', **args))
    native = {**deepcopy(bound), 'ok': True, 'status': bound['execution_status'], 'project_id': 'p', 'canvas_id': 'c'}
    event = tool('freezone_advance_production', native, arguments=args)
    assert native['advancement']['action']['kind'] == 'bind_materials'
    assert evidence._codex_freezone_non_canvas_receipt(event, expected_project='p', expected_canvas='c', expected_agent='main') is native
    assert evidence._codex_freezone_write_receipt(event) is None
    assert native['work_ref']['artifact_id'] == first['work_ref']['artifact_id']
