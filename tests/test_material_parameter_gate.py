"""Material generation requires a human parameter card, not complete node defaults."""
# ruff: noqa: F811
import copy
import json

import pytest

from test_final_material_bindings import material_api, _requirements, _BASE  # noqa: F401
from test_freezone_plugin import _ready_material_adapter
from novelvideo.freezone.remix.material_work import prepare_material_work


@pytest.mark.parametrize('tool_name', ['freezone_prepare_material_work', 'freezone_prepare_material_stage'])
@pytest.mark.parametrize('has_intent', [False, True])
def test_material_adapter_stops_before_draft_for_missing_user_choices(monkeypatch, tmp_path, tool_name, has_intent):
    plugin, handler, args, intent, requests, drafts = _ready_material_adapter(monkeypatch, tmp_path, tool_name)
    original = plugin._request
    missing = {'image': ['model', 'aspect_ratio', 'resolution'],
               'video': ['model', 'aspect_ratio', 'resolution']}

    def request(method, path, **kwargs):
        response = original(method, path, **kwargs)
        if path.endswith(('/material-work', '/material-stage')):
            response['data']['generation_readiness'] = {
                'ready_for_native_generation': False, 'missing_choices': copy.deepcopy(missing)}
            response['data']['workflow_intent'] = intent if has_intent else {'items': []}
        return response

    monkeypatch.setattr(plugin, '_request', request)
    result = json.loads(handler({**args, 'run_after_create': True, 'operation_id': 'admitted'}))
    assert result['status'] == 'clarification_required'
    assert result['code'] == 'generation_parameters_required'
    assert result['required_choices'] == missing
    assert result['work_ref']
    assert 'generation_required_choices' in result['agent_instruction']
    assert '视频时长按已确认方案逐段规划' in result['agent_instruction']
    assert drafts == []
    assert len(requests) == 1
    from jsonschema import Draft202012Validator
    Draft202012Validator(plugin._output_schema(tool_name)).validate(result)


@pytest.mark.parametrize('saved_choices', [None, {'image_model': 'old-model'}])
def test_stage_without_current_receipt_cannot_use_explicit_or_saved_choices(material_api, saved_choices):
    client, store, plan_ref, _, _, _ = material_api
    requirements, steps = _requirements('placement_ids', ('image',))
    steps[0]['generation_inputs'] = {'model': 'agent-model', 'aspectRatio': '9:16', 'size': '1k'}
    saved = prepare_material_work(store, plan_ref, requirements, steps, 'no-receipt',
                                  confirmed_choices=saved_choices)
    response = client.post(_BASE + '/material-stage', json={'work_ref': saved['work_ref']})
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert data['generation_readiness']['ready_for_native_generation'] is False
    assert data['generation_readiness']['missing_choices'] == {'image': ['model', 'aspect_ratio', 'resolution']}
    assert data['workflow_intent']['items'] == []
    assert data['work_ref'] == saved['work_ref']


def test_parameter_answer_resumes_same_work_with_plan_duration(material_api, monkeypatch):
    from novelvideo.api.routes import reference_material

    client, store, plan_ref, _, _, _ = material_api
    requirements, steps = _requirements('placement_ids', ('video',))
    steps[0]['generation_inputs'] = {'durationSec': 6}
    saved = prepare_material_work(store, plan_ref, requirements, steps, 'resume-same-work')
    first = client.post(_BASE + '/material-stage', json={'work_ref': saved['work_ref']}).json()['data']
    assert first['generation_readiness']['missing_choices']['video'] == ['model', 'aspect_ratio', 'resolution']

    async def choices(*args):
        return {'video_model': 'chosen-model', 'video_aspect_ratio': '9:16', 'video_resolution': '720p'}

    monkeypatch.setattr(reference_material, '_current_material_generation_choices', choices)
    response = client.post(_BASE + '/material-stage', json={'work_ref': saved['work_ref']})
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert data['generation_readiness']['ready_for_native_generation'] is True
    assert data['work_ref']['artifact_id'] == saved['work_ref']['artifact_id']
    assert data['work_ref']['revision'] == saved['work_ref']['revision'] + 1
    assert data['workflow_intent']['inputs']['video_model'] == 'chosen-model'
    assert data['workflow_intent']['items'][0]['durationSec'] == 6
    stored = store.read_latest(saved['work_ref']['artifact_id'])['payload']
    assert stored['confirmed_generation_choices']['video_model'] == 'chosen-model'
    from novelvideo.freezone.remix.material_work import material_workflow_intent
    assert material_workflow_intent(stored, store=store) == data['workflow_intent']
