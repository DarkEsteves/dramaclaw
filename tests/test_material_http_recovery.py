"""Material recovery must survive the real HTTP decoder, not just a mocked adapter."""
# ruff: noqa: F811
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from test_production_advancement_tools import http_adapter  # noqa: F401
from novelvideo.chat.agent_api_errors import _http_error_result
from novelvideo.freezone.remix.material_work import material_duration_issues


@pytest.mark.parametrize('wrapped', [False, True])
def test_duration_rejection_survives_http_transport(http_adapter, wrapped):
    plugin, calls, _, execute = http_adapter
    steps = [{'id': name, 'source': 'generate', 'media_kind': 'video'}
             for name in ('clip-1', 'clip-2', 'clip-3', 'clip-4', 'compose')]
    detail = {'code': 'material_duration_invalid', 'issues': material_duration_issues(steps),
              'message': '请逐个视频素材步骤填写模型可支持的 4～15 秒整数时长'}
    result = execute('freezone_prepare_material_work', 422, {'detail': detail} if wrapped else detail)
    assert result['code'] == detail['code']
    assert [row['step_id'] for row in result['issues']] == [row['id'] for row in steps]
    assert all(row['minimum'] == 4 and row['maximum'] == 15 for row in result['issues'])
    assert result['error'] == detail['message']
    assert 'generation_inputs' in result['agent_instruction']
    assert 'durationSec' in result['agent_instruction']
    assert 'compose' in result['agent_instruction']
    assert '还没有保存任何素材工作包' in result['agent_instruction']
    assert result['side_effects'] == 'none'
    assert 'work_ref' not in result
    assert len(calls) == 1
    Draft202012Validator(plugin._output_schema('freezone_prepare_material_work')).validate(result)


def test_speech_ownership_rejection_survives_http_transport(http_adapter):
    _, calls, _, execute = http_adapter
    detail = {'code': 'material_speech_source_conflict', 'placement_id': 'v1',
              'turn_ids': ['t1', 't2'], 'step_ids': ['clip-1'], 'message': 'Incomplete dialogue ownership'}
    result = execute('freezone_prepare_material_work', 422, {'detail': detail})
    for key in ('placement_id', 'turn_ids', 'step_ids'):
        assert result[key] == detail[key]
    assert 'dialogue_turn_ids' in result['agent_instruction']
    assert 'clip-1' in result['agent_instruction']
    assert len(calls) == 1


def test_duration_diagnostics_are_bounded_and_do_not_forward_private_fields():
    detail = {'code': 'material_duration_invalid', 'issues': [
        {'step_id': 'clip', 'minimum': 4, 'maximum': 15,
         'actual': {'api_key': 'private-value'}, 'message': 'Bearer private-value'},
        {'step_id': 'https://private.invalid/signed', 'minimum': 4},
        {'step_id': 'bad-bounds', 'minimum': True, 'maximum': 'private-value'},
    ] + [{'step_id': f'clip-{index}', 'minimum': 4, 'maximum': 15} for index in range(20)]}
    result = _http_error_result(422, json.dumps({'detail': detail}), 'Request failed')
    assert len(result['issues']) <= 8
    assert result['issues'][0] == {'step_id': 'clip', 'minimum': 4, 'maximum': 15}
    assert result['issues'][1] == {'step_id': 'bad-bounds'}
    assert 'private' not in json.dumps(result)
    assert len(json.dumps(result).encode()) <= 4096


def test_material_example_matches_tool_schema_and_duration_contract(http_adapter):
    plugin, _, _, _ = http_adapter
    root = Path(__file__).resolve().parents[1]
    paths = [root / prefix / 'references/reference-video-remix.md' for prefix in (
        'src/novelvideo/agent_skills/dramaclaw-workflows',
        'agent-kit/skills/dramaclaw-workflows', '.hermes/skills/workflows')]
    assert len({path.read_bytes() for path in paths}) == 1
    guide = paths[0].read_text()
    section = guide.split('<!-- native-material-author-example -->', 1)[1]
    args = json.loads(section.split('```json', 1)[1].split('```', 1)[0])
    schema = next(schema for name, schema, _ in plugin.TOOLS if name == 'freezone_prepare_material_work')
    validator = Draft202012Validator(schema['parameters'])
    validator.validate(args)
    assert material_duration_issues(args['steps']) == []
    assert 'native composition pipeline' in schema['description']
    assert 'generation_inputs.durationSec' in schema['description']
    assert 'speech_placement_id' in schema['description']
    args['steps'][-1]['duration_frames'] = 180
    with pytest.raises(ValidationError, match='Additional properties'):
        validator.validate(args)
    assert 'do not bind all' in guide
    assert 'only orders steps' in guide
