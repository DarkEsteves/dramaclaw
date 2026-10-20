from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from novelvideo.freezone.remix import reference_commission as commission
from novelvideo.freezone.remix.store import RemixArtifactStore
from test_reference_commission import confirmed_plan, selectable_work, selection_canvas


BASE = '/projects/p/freezone/canvases/c/director'


@pytest.fixture
def selection_api(monkeypatch, tmp_path):
    from novelvideo.api.auth import get_api_user
    from novelvideo.api.routes import freezone, reference_director as routes
    from novelvideo.freezone import canvas_store
    ctx = SimpleNamespace(project_id='p', requester_user_id='u', requester_username='alice',
                          state_dir=tmp_path, output_dir=tmp_path)
    async def resolve(*args, **kwargs):
        return ctx, 'alice', 'demo', tmp_path, str(tmp_path)
    async def emit(*args, **kwargs):
        pass
    monkeypatch.setattr(freezone, '_resolve_freezone_project', resolve)
    monkeypatch.setattr(routes, '_emit_director_update', emit)
    store = RemixArtifactStore(tmp_path, 'p', created_by='u')
    c, p = confirmed_plan(store)
    work = selectable_work(store, p)
    canvas = {**selection_canvas(p), 'revision': 1}
    canvas_store.save_canvas(tmp_path, 'c', base_revision=None, build_payload=lambda _: deepcopy(canvas))
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_api_user] = lambda: {'username': 'alice', 'user_id': 'u', 'role': 'owner'}
    with TestClient(app) as client:
        yield client, store, c, p, work, app


def test_selection_endpoint_publishes_and_replays_exact_result(selection_api):
    client, store, c, p, work, _ = selection_api
    body = {'commission_ref': c['commission_ref'], 'work_ref': work, 'command_id': 'select-api'}
    response = client.post(BASE + '/select-material-work', json=body)
    assert response.status_code == 200, response.text
    result = response.json()['data']
    assert result['work_ref'] == {**work, 'project_id': 'p'}
    assert result['phase'] == 'preparing_materials'
    assert client.post(BASE + '/select-material-work', json=body).json() == response.json()
    other = selectable_work(store, p, 'other-api')
    assert client.post(BASE + '/select-material-work', json={**body, 'work_ref': other}).status_code == 409
    assert client.post(BASE + '/select-material-work', json={**body, 'work_ref': other, 'command_id': 'stale-api'}).status_code == 409
    assert commission.read_reference_commission(store, c['commission_ref'], latest=True)['commission_ref']['revision'] == result['commission_ref']['revision']


@pytest.mark.parametrize('patch,status', [({'project_id': 'other'}, 404),
                                         ({'access_model': 'private', 'owner_principal_id': 'other'}, 403)])
def test_selection_endpoint_rechecks_canvas_inside_mutex(selection_api, monkeypatch, patch, status):
    from novelvideo.freezone import canvas_store
    client, store, c, _, work, _ = selection_api
    original = canvas_store.read_canvas
    count = 0
    def changed_canvas(*args):
        nonlocal count
        count += 1
        canvas = original(*args)
        return canvas if count == 1 else {**canvas, **patch}
    monkeypatch.setattr(canvas_store, 'read_canvas', changed_canvas)
    response = client.post(BASE + '/select-material-work', json={
        'commission_ref': c['commission_ref'], 'work_ref': work, 'command_id': 'private'})
    assert response.status_code == status, response.text
    assert count == 2
    assert commission.read_reference_commission(store, c['commission_ref'], latest=True)['commission_ref'] == c['commission_ref']


def test_selection_endpoint_rejects_client_authorization_fields(selection_api):
    client, _, c, _, work, _ = selection_api
    assert client.post(BASE + '/select-material-work', json={'commission_ref': c['commission_ref'],
        'work_ref': work, 'command_id': 'forged', 'requester_user_id': 'u'}).status_code == 422
