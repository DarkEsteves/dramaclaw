from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from novelvideo.api.auth import get_api_user
from novelvideo.api.routes import reference_director, freezone


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(reference_director.router)
    app.dependency_overrides[get_api_user] = lambda: {'username': 'u'}
    seen = []
    async def context(project, canvas_id, user, **kwargs):
        seen.append((project, canvas_id, kwargs))
        if kwargs.get('agent_id') != 'allowed':
            raise HTTPException(403, 'agent scope differs')
        return SimpleNamespace(project_id='p'), {}, None
    monkeypatch.setattr(reference_director, '_context', context)
    return TestClient(app), seen


def test_native_api_forwards_exact_request_after_scope_check(client, monkeypatch):
    from novelvideo.freezone.remix import production_node_entry
    api, seen = client
    received = []
    async def prepare(**kwargs):
        received.append(kwargs)
        return {'draft_id': 'existing-draft', 'revision': 2, 'native_plan_stage': {'state': 'ready_for_canvas_approval'}}
    monkeypatch.setattr(production_node_entry, 'prepare_production_nodes', prepare)
    request = {'commission_ref': {'artifact_id': 'commission', 'revision': 3, 'project_id': 'p'},
               'goal': '完整目标', 'run_after_create': True, 'plan_count': 2}
    result = api.post('/projects/p/freezone/canvases/c/director/production-nodes/prepare?agent_id=allowed', json=request)
    assert result.status_code == 200
    assert result.json()['data']['draft_id'] == 'existing-draft'
    assert received[0]['request'].model_dump() == request
    assert received[0]['agent_id'] == 'allowed'
    assert seen == [('p', 'c', {'agent_id': 'allowed'})]


def test_agent_credential_requires_bound_human_turn_at_native_entry(client, monkeypatch):
    from novelvideo.freezone.remix import production_node_entry
    api, _ = client
    api.app.dependency_overrides[get_api_user] = lambda: {
        'username': 'u', 'credential_kind': 'agent_session'}
    received = []
    async def prepare(**kwargs):
        received.append(kwargs)
        return {'draft_id': 'draft'}
    monkeypatch.setattr(production_node_entry, 'prepare_production_nodes', prepare)
    result = api.post('/projects/p/freezone/canvases/c/director/production-nodes/prepare?agent_id=allowed',
        headers={'X-DramaClaw-Turn-ID': 'human-turn'},
        json={'commission_ref': {'artifact_id': 'commission', 'revision': 1},
              'goal': '当前目标', 'run_after_create': False})
    assert result.status_code == 200
    assert received[0]['agent_request'] is True
    assert received[0]['turn_id'] == 'human-turn'


@pytest.mark.parametrize('extra', [{'run_after_create': 'false'}, {'run_after_create': False, 'compiled': {}}, {}])
def test_bad_native_input_never_reaches_preparation(client, extra):
    api, seen = client
    result = api.post('/projects/p/freezone/canvases/c/director/production-nodes/prepare',
                      json={'commission_ref': {}, 'goal': 'goal', **extra})
    assert result.status_code == 422
    assert seen == []


def test_wrong_agent_rejected_before_preparation(client):
    api, _ = client
    result = api.post('/projects/p/freezone/canvases/c/director/production-nodes/prepare?agent_id=other',
                      json={'commission_ref': {}, 'goal': 'goal', 'run_after_create': False})
    assert result.status_code == 403


@pytest.mark.asyncio
async def test_generic_workflow_still_requires_metered_operation(monkeypatch, tmp_path):
    async def resolve(*args, **kwargs):
        return SimpleNamespace(state_dir=tmp_path), 'u', 'p', tmp_path, tmp_path
    monkeypatch.setattr(freezone, '_resolve_freezone_project', resolve)
    monkeypatch.setattr(freezone, '_canvas_state_project_dir', lambda *_: tmp_path)
    monkeypatch.setattr(freezone, 'get_usage_meter', lambda: object())
    with pytest.raises(HTTPException) as error:
        await freezone.create_canvas_workflow_draft('p', 'c', {'intent': {'skill_id': 'reference-video-remix'}}, {'username': 'u'})
    assert error.value.status_code == 400
    assert 'operation_id is required' in error.value.detail
