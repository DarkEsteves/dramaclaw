"""Resource recovery guidance does not repair or approve a failed native edit."""
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from test_freezone_plugin import _load_plugin_module, _restore_tools_registry_modules
from test_material_work import plan_payload
from novelvideo.freezone.remix.production_commands import apply_production_command
from novelvideo.freezone.remix.store import RemixArtifactStore


def command(kind='bind_media'):
    return {'type': 'production_plan_command', 'node_id': 'plan-node', 'command': {
        'command_id': 'native-bind', 'plan_ref': {'artifact_id': 'plan', 'revision': 1},
        'kind': kind, 'target_id': 'picture', 'payload': {'resource_id': 'UNREGISTERED_CANVAS_NODE'},
    }}


def failure(error='resource UNREGISTERED_CANVAS_NODE not found', **overrides):
    return {'ok': False, 'tool_call_status': 'failed', 'canvas_apply_status': 'failed',
            'applied': False, 'cancelled': False, 'errors': [error],
            'project_id': 'project', 'canvas_id': 'canvas', **overrides}


def nonbinding_production_batch():
    edit = command('set_goal')
    edit['command']['payload'] = {'goal': 'Actual authorized goal'}
    batch = deepcopy(edit)
    batch['command'].update(kind='batch', target_id='plan', payload={'commands': [
        {'kind': 'set_goal', 'target_id': 'plan', 'payload': {'goal': 'Actual authorized goal'}}]})
    return edit, batch


@pytest.mark.parametrize('batch', [False, True])
def test_real_unregistered_resource_apply_failure_has_static_existing_material_recovery(tmp_path, batch):
    plugin = _load_plugin_module()
    store = RemixArtifactStore(tmp_path, 'project')
    saved = store.create('production_plan.v2', plan_payload(), [])
    edit = command()
    native = edit['command']
    native['plan_ref'] = {'artifact_id': saved['id'], 'revision': saved['revision']}
    if batch:
        native['kind'] = 'batch'
        native['target_id'] = 'plan'
        native['payload'] = {'commands': [{'kind': 'bind_media', 'target_id': 'picture',
                                          'payload': {'resource_id': 'UNREGISTERED_CANVAS_NODE'}}]}
    before = {str(path): path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    with pytest.raises(ValueError, match='resource .* not found') as rejected:
        apply_production_command(store, native)
    resolved = failure(str(rejected.value))
    original = deepcopy(resolved)
    summary = plugin._summarize_canvas_command_result(resolved, bridge_key='real-bridge', commands=[edit])
    instruction = summary['agent_instruction']
    for required in ('resource_id', 'saved plan resource', 'node_id', 'filename stem',
                     'freezone_prepare_material_work', 'source=existing', 'no recipe_id',
                     'freezone_review_material_candidate', 'freezone_accept_material_candidate'):
        assert required in instruction
    assert 'authenticated' in instruction and 'do not regenerate' in instruction
    assert 'UNREGISTERED_CANVAS_NODE' not in instruction
    assert len(instruction) < 2000
    assert summary['ok'] is False and summary['applied'] is False
    assert summary['tool_call_status'] == 'failed' and summary['canvas_apply_status'] == 'failed'
    assert summary['errors'] == original['errors'] and resolved == original
    assert store.read_latest(saved['id'])['revision'] == saved['revision']
    assert before == {str(path): path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}


@pytest.mark.parametrize('resolved,commands', [
    (failure('resource metadata differs from project media'), [command()]),
    (failure('placement picture not found'), [command()]),
    (failure('revision conflict'), [command()]),
    (failure(canvas_apply_status='timeout', cancelled=True), [command()]),
    (failure(canvas_apply_status='read_failed'), [command()]),
    (failure(cancelled=True), [command()]),
    (failure(), [{'type': 'run_node_action', 'node_id': 'video', 'action': 'generate_video'}]),
    (failure(), [nonbinding_production_batch()[0], {'type': 'run_node_action', 'node_id': 'video', 'action': 'generate_video'}]),
    (failure(), [nonbinding_production_batch()[1], {'type': 'run_node_action', 'node_id': 'video', 'action': 'generate_video'}]),
    (failure('resource unrelated-provider-cache not found'), [command()]),
])
def test_other_failures_and_nonproduction_commands_keep_existing_diagnostic(resolved, commands):
    plugin = _load_plugin_module()
    original = deepcopy(resolved)
    summary = plugin._summarize_canvas_command_result(resolved, bridge_key='bridge', commands=commands)
    assert summary['agent_instruction'] == 'Canvas command result has been summarized. Do not ask for or print the full commands.'
    assert summary['ok'] is False
    assert resolved == original


def test_successful_production_edit_keeps_native_result_and_instruction():
    plugin = _load_plugin_module()
    resolved = {'ok': True, 'tool_call_status': 'completed', 'canvas_apply_status': 'applied',
                'applied': True, 'cancelled': False, 'errors': [], 'agent_instruction': 'native success',
                'project_id': 'project', 'canvas_id': 'canvas', 'applied_count': 1}
    summary = plugin._summarize_canvas_command_result(resolved, bridge_key='bridge', commands=[command()])
    assert summary['ok'] is True and summary['applied'] is True
    assert 'freezone_prepare_material_work' not in summary['agent_instruction']
    assert summary['errors'] == [] and summary['applied_count'] == 1


def test_metadata_explains_registered_resources_and_native_existing_candidate_acceptance():
    plugin = _load_plugin_module()
    tool = next(schema for name, schema, _ in plugin.TOOLS if name == 'freezone_prepare_production_edit')
    description = tool['description']
    payload = tool['parameters']['properties']['command']['properties']['payload']['description']
    for text in (description, payload):
        assert 'resource_id' in text and 'saved plan resource' in text
        assert 'node_id' in text and 'filename stem' in text
        assert 'source=existing' in text and 'no recipe_id' in text
        assert 'freezone_review_material_candidate' in text and 'freezone_accept_material_candidate' in text
        assert 'resource.id' in text and 'resource' in text and 'media_hash' in text
        assert 'payload.resource' in text and 'does not register' in text and 'id-only' in text
    schema = Draft202012Validator(tool['parameters'])
    schema.validate({'node_id': 'plan-node', 'command': command()['command']})
    assert 'additionalProperties' not in tool['parameters']['properties']['command']['properties']['payload']


def test_prepare_edit_forwards_original_command_through_same_required_approval(monkeypatch):
    plugin = _load_plugin_module()
    monkeypatch.setenv('DRAMACLAW_PROJECT_ID', 'project')
    monkeypatch.setenv('DRAMACLAW_CANVAS_ID', 'canvas')
    observed = []
    native = command()['command']
    original = deepcopy(native)
    monkeypatch.setattr(plugin, '_emit_canvas_commands', lambda *args, **kwargs: observed.append((args, kwargs)) or {'ok': False})
    monkeypatch.setattr(plugin, '_request', lambda *args, **kwargs: pytest.fail('guidance must not probe/register/apply automatically'))
    result = plugin._handle_prepare_production_edit({'node_id': 'plan-node', 'command': native})
    assert result == {'ok': False}
    assert observed == [(('project', 'canvas', [{'type': 'production_plan_command', 'node_id': 'plan-node', 'command': original}]),
                         {'slim_result': True, 'require_canvas_receipt': True})]
    assert native == original
