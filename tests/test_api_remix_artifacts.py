from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest


BASE = "/api/v1/projects/demo/freezone/remix/artifacts"


def test_arrangement_cannot_bypass_canvas_confirmation_via_generic_action(client, monkeypatch):
    from novelvideo.api.routes import remix as routes
    calls = []
    def prepare(*args, **kwargs):
        calls.append(kwargs)
        raise AssertionError('must not enqueue an unbound arrangement')
    monkeypatch.setattr(routes, 'prepare_registered_action', prepare)
    api, _ = client
    response = api.post('/api/v1/projects/demo/freezone/remix/actions', json={
        'action': 'generate_production_plan_arrangement',
        'payload': {'plan_ref': {'artifact_id': 'plan', 'revision': 1}},
        'idempotency_key': 'bypass', 'node_revision': 1})
    assert response.status_code == 422
    assert response.json()['detail']['code'] == 'production_plan_canvas_action_required'
    assert calls == []


def evidence_payload() -> dict:
    return {
        "media_hash": "sha256:reference",
        "time_base": "1/90000",
        "duration_us": 1_000_000,
        "cuts": [],
        "representative_frames": [],
    }


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    from novelvideo.api.auth import get_api_user
    from novelvideo.api import egress_binding
    from novelvideo.api.routes import remix as routes

    role = {"value": "editor", "home": True}

    async def resolve(*, user, project_id, required_role):
        if project_id != "demo" or (required_role == "editor" and role["value"] != "editor"):
            raise HTTPException(403, "Access denied")
        return SimpleNamespace(
            output_dir=str(tmp_path), state_dir=tmp_path, project_id="demo",
            requester_user_id="alice-user", owner_username="alice",
            project_name="demo", is_home_node=role["home"], home_node_id="other", current_node_id="local",
        )

    def home(ctx, **kwargs):
        if not ctx.is_home_node:
            raise HTTPException(409, "Wrong home node")

    monkeypatch.setattr(routes, "resolve_project_context", resolve)
    monkeypatch.setattr(routes, "require_project_home_node", home)
    @asynccontextmanager
    async def local_egress_scope(**kwargs):
        yield None

    monkeypatch.setattr(egress_binding, "request_egress_scope", local_egress_scope)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    app.dependency_overrides[get_api_user] = lambda: {"username": "alice"}
    return TestClient(app), role


def test_artifact_create_read_and_revise(client):
    api, _ = client
    created = api.post(BASE, json={"schema": "reference_evidence.v1", "payload": evidence_payload(), "upstream_refs": []})
    assert created.status_code == 200
    artifact = created.json()["data"]
    url = f"{BASE}/{artifact['id']}"

    assert api.get(url, params={"revision": 1}).json()["data"] == artifact
    revised_payload = {**evidence_payload(), "duration_us": 2_000_000}
    revised = api.put(url, json={"expected_revision": 1, "payload": revised_payload, "upstream_refs": []})
    assert revised.status_code == 200
    assert revised.json()["data"]["revision"] == 2
    assert api.put(url, json={"expected_revision": 1, "payload": revised_payload, "upstream_refs": []}).status_code == 409
    assert api.get(url, params={"revision": 1}).json()["data"]["payload"] == evidence_payload()
    assert api.get(f"{url}/latest").json()["data"]["revision"] == 2


def test_artifact_api_enforces_acl_home_node_and_project_scope(client):
    api, role = client
    artifact = api.post(BASE, json={"schema": "reference_evidence.v1", "payload": evidence_payload(), "upstream_refs": []}).json()["data"]
    url = f"{BASE}/{artifact['id']}"

    role["value"] = "viewer"
    assert api.get(url, params={"revision": 1}).status_code == 200
    assert api.get(f"{url}/latest").status_code == 200
    assert api.post(BASE, json={"schema": "reference_evidence.v1", "payload": evidence_payload(), "upstream_refs": []}).status_code == 403
    assert api.put(url, json={"expected_revision": 1, "payload": evidence_payload(), "upstream_refs": []}).status_code == 403
    assert api.get(url.replace("/demo/", "/peer/"), params={"revision": 1}).status_code == 403
    role["home"] = False
    assert api.get(url, params={"revision": 1}).status_code == 409


def test_artifact_api_maps_validation_and_missing_artifacts(client):
    api, _ = client
    assert api.post(BASE, json={"schema": "unknown.v1", "payload": {}, "upstream_refs": []}).status_code == 422
    assert api.post(BASE, json={"schema": "reference_evidence.v1", "payload": {}, "upstream_refs": []}).status_code == 422
    assert api.get(f"{BASE}/unknown", params={"revision": 1}).status_code == 404


def test_saved_production_plan_compiles_through_public_api(client):
    from test_production_plan import media, plan

    api, _ = client
    payload = plan()
    payload["beats"][0]["visuals"][0]["media"] = media("still")
    created = api.post(BASE, json={"schema": "production_plan.v1", "payload": payload, "upstream_refs": []})
    assert created.status_code == 200
    artifact = created.json()["data"]
    ref = {"artifact_id": artifact["id"], "revision": artifact["revision"]}
    response = api.post("/api/v1/projects/demo/freezone/remix/production-plans/compile", json={"plan_ref": ref})
    assert response.status_code == 200
    compiled = response.json()["data"]
    assert compiled["manifest_version"] == 3
    assert compiled["source_versions"] == [ref]
    assert api.post(BASE, json={"schema": "composition_manifest.v1", "payload": compiled, "upstream_refs": [ref]}).status_code == 200


def test_v2_plan_revision_compiles_and_saves_versioned_manifest(client):
    from test_production_plan_v2 import _plan

    api, _ = client
    plan = _plan()
    created = api.post(BASE, json={"schema": "production_plan.v2", "payload": plan, "upstream_refs": []})
    assert created.status_code == 200, created.text
    artifact = created.json()["data"]
    ref = {"artifact_id": artifact["id"], "revision": artifact["revision"]}
    response = api.post("/api/v1/projects/demo/freezone/remix/production-plans/compile", json={"plan_ref": ref})
    assert response.status_code == 200, response.text
    manifest = response.json()["data"]
    assert manifest["manifest_version"] == 4
    assert manifest["source_versions"] == [ref]
    saved = api.post(BASE, json={"schema": "composition_manifest.v2", "payload": manifest, "upstream_refs": [ref]})
    assert saved.status_code == 200, saved.text


def test_v2_graphic_only_plan_projects_and_compiles_without_media(client):
    from test_production_plan_v2 import _anchor, _plan

    api, _ = client
    plan = _plan()
    plan["resources"] = []
    plan["placements"] = [{"id": "card", "kind": "graphic", "role": "effect", "track_id": "graphic:effect",
                           "graphic": {"template": "question_card", "text": "为什么？"},
                           "start": _anchor(0), "end": _anchor(60)}]
    created = api.post(BASE, json={"schema": "production_plan.v2", "payload": plan})
    assert created.status_code == 200, created.text
    saved = created.json()["data"]
    ref = {"artifact_id": saved["id"], "revision": saved["revision"]}
    root = "/api/v1/projects/demo/freezone/remix/production-plans"
    projected = api.post(f"{root}/project", json={"plan_ref": ref})
    assert projected.status_code == 200, projected.text
    assert projected.json()["data"]["ready"] is True
    compiled = api.post(f"{root}/compile", json={"plan_ref": ref})
    assert compiled.status_code == 200, compiled.text
    manifest = compiled.json()["data"]
    assert manifest["media_bindings"] == []
    assert manifest["graphic_events"][0]["text"] == "为什么？"
    persisted = api.post(BASE, json={"schema": "composition_manifest.v2", "payload": manifest,
                                     "upstream_refs": [ref]})
    assert persisted.status_code == 200, persisted.text


def test_v2_exact_frame_preview_uses_saved_revision_and_checked_media(client, monkeypatch, tmp_path):
    import hashlib
    from novelvideo.api.routes import remix as routes
    from test_production_plan_v2 import _plan

    api, role = client
    image = tmp_path / 'assets' / 'picture.png'
    image.parent.mkdir(parents=True)
    image.write_bytes(b'original image')
    plan = _plan()
    plan['output'].update(width=32, height=32)
    plan['resources'] = [plan['resources'][0]]
    plan['resources'][0]['media_hash'] = hashlib.sha256(image.read_bytes()).hexdigest()
    plan['placements'] = [plan['placements'][0]]
    created = api.post(BASE, json={'schema': 'production_plan.v2', 'payload': plan})
    assert created.status_code == 200, created.text
    saved = created.json()['data']
    ref = {'artifact_id': saved['id'], 'revision': 1}
    assert api.get(f"{BASE}/{saved['id']}", params={'revision': 1}).status_code == 200
    captured = []

    async def render(lease, manifest, *, media_paths, frame, output_path):
        captured.append((manifest, media_paths, frame))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b'PNG preview')
        return output_path

    monkeypatch.setattr(routes.get_preview_render_capacity(), 'render_frame', render)
    def prune_race(_cache_dir):
        raise OSError('another preview removed a cache entry')
    monkeypatch.setattr(routes, '_prune_preview_frames', prune_race)
    endpoint = '/api/v1/projects/demo/freezone/remix/production-plans/preview-frame'
    role['value'] = 'viewer'
    first = api.post(endpoint, json={'plan_ref': ref, 'frame': 12})
    assert first.status_code == 200, first.text
    assert first.headers['content-type'] == 'image/png'
    assert first.content == b'PNG preview'
    assert captured[0][0]['source_versions'] == [ref]
    assert captured[0][1]['picture'] == image
    assert captured[0][2] == 12
    assert api.post(endpoint, json={'plan_ref': ref, 'frame': 12}).status_code == 200
    assert len(captured) == 1  # project-scoped exact revision cache
    assert api.post(endpoint, json={'plan_ref': ref, 'frame': 240}).status_code == 422
    assert api.post(endpoint.replace('/demo/', '/peer/'), json={'plan_ref': ref, 'frame': 12}).status_code == 403
    image.write_bytes(b'changed image')
    assert api.post(endpoint, json={'plan_ref': ref, 'frame': 12}).status_code == 409
    assert len(captured) == 1
    assert api.post(endpoint, json={'plan_ref': {'artifact_id': 'missing', 'revision': 1}, 'frame': 12}).status_code == 404


def test_preview_capacity_is_acquired_before_media_hashing(client, monkeypatch):
    from novelvideo.api.routes import remix as routes
    from novelvideo.ports.preview_capacity import PreviewBusy
    from test_production_plan_v2 import _plan

    api, _ = client
    saved = api.post(BASE, json={'schema': 'production_plan.v2', 'payload': _plan()}).json()['data']
    ref = {'artifact_id': saved['id'], 'revision': 1}

    class BusyCapacity:
        def slot(self):
            raise PreviewBusy('busy')

    monkeypatch.setattr(routes, 'get_preview_render_capacity', lambda: BusyCapacity())
    monkeypatch.setattr(routes, 'project_production_plan_v2',
                        lambda *args, **kwargs: pytest.fail('media was hashed without capacity'))
    response = api.post('/api/v1/projects/demo/freezone/remix/production-plans/preview-frame',
                        json={'plan_ref': ref, 'frame': 12})
    assert response.status_code == 429


def test_preview_rejects_unbounded_output_before_capacity(client, monkeypatch):
    from novelvideo.api.routes import remix as routes
    from test_production_plan_v2 import _plan

    api, _ = client
    plan = _plan()
    plan['output'].update(width=100000, height=100000)
    saved = api.post(BASE, json={'schema': 'production_plan.v2', 'payload': plan}).json()['data']
    monkeypatch.setattr(routes, 'get_preview_render_capacity',
                        lambda: pytest.fail('oversized preview acquired capacity'))
    response = api.post('/api/v1/projects/demo/freezone/remix/production-plans/preview-frame',
                        json={'plan_ref': {'artifact_id': saved['id'], 'revision': 1}, 'frame': 0})
    assert response.status_code == 422


def test_preview_rejects_timing_expansion_after_compile(client, monkeypatch):
    from novelvideo.api.routes import remix as routes
    from test_production_plan_v2 import _plan

    api, _ = client
    created = api.post(BASE, json={'schema': 'production_plan.v2', 'payload': _plan()})
    assert created.status_code == 200, created.text
    saved = created.json()['data']
    monkeypatch.setattr(routes, 'project_production_plan_v2',
                        lambda *args, **kwargs: {'ready': True})
    monkeypatch.setattr(routes, 'compile_production_plan_v2',
                        lambda *args, **kwargs: {
                            'output_spec': {'duration_frames': 3_000_000},
                            'layers': [], 'media_bindings': [],
                        })
    response = api.post('/api/v1/projects/demo/freezone/remix/production-plans/preview-frame',
                        json={'plan_ref': {'artifact_id': saved['id'], 'revision': 1}, 'frame': 0})
    assert response.status_code == 422
    assert 'resource budget' in response.json()['detail']


def test_v1_plan_migration_is_explicit_and_keeps_original_revision(client):
    from test_production_plan import plan

    api, _ = client
    old = api.post(BASE, json={"schema": "production_plan.v1", "payload": plan(), "upstream_refs": []}).json()["data"]
    ref = {"artifact_id": old["id"], "revision": old["revision"]}
    migrated = api.post("/api/v1/projects/demo/freezone/remix/production-plans/migrate", json={"plan_ref": ref})
    assert migrated.status_code == 200, migrated.text
    new = migrated.json()["data"]
    assert new["schema"] == "production_plan.v2"
    assert new["payload"]["provenance"]["source_plan_ref"] == ref
    assert api.get(f"{BASE}/{old['id']}", params={"revision": 1}).json()["data"]["schema"] == "production_plan.v1"
    broken = api.post(BASE, json={"schema": "production_plan.v2", "payload": new["payload"], "upstream_refs": []})
    assert broken.status_code == 422
    assert "source_plan_ref" in broken.json()["detail"]


def test_production_plan_draft_and_compile_reject_incomplete_inputs(client):
    from test_production_plan import plan

    api, _ = client
    assert api.post("/api/v1/projects/demo/freezone/remix/production-plan-drafts", json={"topic": "  "}).status_code == 422
    artifact = api.post(BASE, json={"schema": "production_plan.v1", "payload": plan(), "upstream_refs": []}).json()["data"]
    ref = {"artifact_id": artifact["id"], "revision": artifact["revision"]}
    response = api.post("/api/v1/projects/demo/freezone/remix/production-plans/compile", json={"plan_ref": ref})
    assert response.status_code == 422
    assert "hook-picture" in response.json()["detail"]


@pytest.mark.parametrize("inputs", [
    {"analysis_context": {"events": [{"narrative": "开场"}]}},
    {"blueprint_ref": {"artifact_id": "old-blueprint", "revision": 1}},
    {"locked_turns": [{"speaker": "女", "text": "晚上喝咖啡吗？"}]},
])
def test_legacy_production_draft_directs_to_canvas_action_without_generation(client, monkeypatch, inputs):
    from novelvideo.api.routes import remix as routes
    calls = []
    async def draft(**kwargs):
        calls.append(kwargs)
        raise AssertionError("legacy route must not generate")
    monkeypatch.setattr(routes, "generate_production_plan_draft", draft)
    api, _ = client
    response = api.post("/api/v1/projects/demo/freezone/remix/production-plan-drafts",
        json={"topic": "制作短片", **inputs})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "production_plan_canvas_action_required"
    assert calls == []


def test_v2_projection_allows_incomplete_saved_revision_and_enforces_acl(client):
    from test_production_plan_v2 import _plan
    api, role = client
    plan = _plan()
    plan['placements'][0].pop('resource_id')
    artifact = api.post(BASE, json={'schema': 'production_plan.v2', 'payload': plan}).json()['data']
    ref = {'artifact_id': artifact['id'], 'revision': 1}
    url = '/api/v1/projects/demo/freezone/remix/production-plans/project'
    role['value'] = 'viewer'
    response = api.post(url, json={'plan_ref': ref})
    assert response.status_code == 200, response.text
    projection = response.json()['data']
    assert projection['plan_ref'] == ref
    assert projection['ready'] is False
    assert projection['placements'][0]['start_frame'] == 0
    assert projection['placements'][0]['resource_status'] == 'unbound'
    assert api.post(url.replace('/demo/', '/peer/'), json={'plan_ref': ref}).status_code == 403
    role['home'] = False
    assert api.post(url, json={'plan_ref': ref}).status_code == 409


def test_v2_revision_edit_preserves_old_projection_and_reports_conflict(client):
    from test_production_plan_v2 import _plan
    api, _ = client
    plan = _plan()
    artifact = api.post(BASE, json={'schema': 'production_plan.v2', 'payload': plan}).json()['data']
    url = f"{BASE}/{artifact['id']}"
    plan['placements'][0]['start'] = {'kind': 'program_frame', 'frame': 20}
    edit = {'expected_revision': 1, 'payload': plan, 'upstream_refs': []}
    assert api.put(url, json=edit).status_code == 200
    assert api.put(url, json=edit).status_code == 409
    endpoint = '/api/v1/projects/demo/freezone/remix/production-plans/project'
    for revision, start in [(1, 0), (2, 20)]:
        result = api.post(endpoint, json={'plan_ref': {'artifact_id': artifact['id'], 'revision': revision}})
        assert result.status_code == 200
        assert result.json()['data']['placements'][0]['start_frame'] == start


def test_v2_manifest_rejects_wrong_saved_source_revision(client):
    from test_production_plan_v2 import _plan
    api, _ = client
    plan = _plan()
    artifact = api.post(BASE, json={'schema': 'production_plan.v2', 'payload': plan}).json()['data']
    ref = {'artifact_id': artifact['id'], 'revision': 1}
    manifest = api.post('/api/v1/projects/demo/freezone/remix/production-plans/compile', json={'plan_ref': ref}).json()['data']
    plan['placements'][0]['start'] = {'kind': 'program_frame', 'frame': 10}
    assert api.put(f"{BASE}/{artifact['id']}", json={'expected_revision': 1, 'payload': plan, 'upstream_refs': []}).status_code == 200
    wrong_ref = {'artifact_id': artifact['id'], 'revision': 2}
    manifest['source_versions'] = [wrong_ref]
    response = api.post(BASE, json={'schema': 'composition_manifest.v2', 'payload': manifest, 'upstream_refs': [wrong_ref]})
    assert response.status_code == 422
    assert 'saved source revision' in response.text


def test_v2_render_requires_current_saved_canvas_bindings():
    from novelvideo.api.routes.remix import _validate_v2_canvas_bindings

    ref = {'artifact_id': 'plan', 'revision': 2}
    manifest = {'source_versions': [ref]}
    manifest_ref = {'artifact_id': 'manifest', 'revision': 1}
    resource = {'id': 'resource', 'node_id': 'media', 'media_hash': 'a' * 64,
                'media_path': 'images/a.png', 'media_kind': 'image', 'duration_us': 0, 'has_audio': False}
    plan = {'resources': [resource], 'placements': [{'resource_id': 'resource'}]}
    canvas = {'nodes': [
        {'id': 'plan-node', 'data': {'productionPlanV2Ref': {'artifactId': 'plan', 'revision': 2}}},
        {'id': 'compose', 'data': {'productionPlanNodeId': 'plan-node',
                                 'productionPlanV2Ref': {'artifactId': 'plan', 'revision': 2},
                                 'remixManifestRef': {'artifactId': 'manifest', 'revision': 1}}},
        {'id': 'media', 'data': {'imageUrl': '/api/v1/projects/demo/media/images/a.png',
                                 'remixMedia': {key: resource[key] for key in
                                            ('media_hash', 'media_path', 'media_kind', 'duration_us', 'has_audio')}}},
    ], 'edges': [{'source': 'plan-node', 'target': 'compose'}, {'source': 'media', 'target': 'plan-node'}]}
    _validate_v2_canvas_bindings(canvas, manifest, manifest_ref, plan, 'compose', 'demo')
    canvas['edges'].pop()
    with pytest.raises(ValueError, match='no longer connected'):
        _validate_v2_canvas_bindings(canvas, manifest, manifest_ref, plan, 'compose', 'demo')
    canvas['edges'].append({'source': 'media', 'target': 'plan-node'})
    canvas['nodes'][2]['data']['remixMedia']['media_hash'] = 'b' * 64
    with pytest.raises(ValueError, match='changed after binding'):
        _validate_v2_canvas_bindings(canvas, manifest, manifest_ref, plan, 'compose', 'demo')
    canvas['nodes'][2]['data']['remixMedia']['media_hash'] = 'a' * 64
    canvas['nodes'][2]['data']['imageUrl'] = '/api/v1/projects/demo/media/images/other.png'
    with pytest.raises(ValueError, match='source changed'):
        _validate_v2_canvas_bindings(canvas, manifest, manifest_ref, plan, 'compose', 'demo')
    canvas['nodes'][2]['data']['imageUrl'] = '/api/v1/projects/demo/media/images/a.png'
    canvas['nodes'][1]['data']['productionPlanV2Ref']['revision'] = 3
    with pytest.raises(ValueError, match='no longer references'):
        _validate_v2_canvas_bindings(canvas, manifest, manifest_ref, plan, 'compose', 'demo')


def test_v2_render_endpoint_rejects_missing_canvas(client):
    from test_production_plan_v2 import _plan
    api, _ = client
    plan = _plan()
    saved = api.post(BASE, json={'schema': 'production_plan.v2', 'payload': plan}).json()['data']
    ref = {'artifact_id': saved['id'], 'revision': 1}
    compiled = api.post('/api/v1/projects/demo/freezone/remix/production-plans/compile', json={'plan_ref': ref}).json()['data']
    manifest = api.post(BASE, json={'schema': 'composition_manifest.v2', 'payload': compiled, 'upstream_refs': [ref]}).json()['data']
    response = api.post(f"{BASE}/{manifest['id']}/render", json={
        'revision': 1, 'idempotency_key': 'v2-missing-canvas', 'node_revision': 1,
        'canvas_id': 'default', 'node_id': 'compose', 'input_fingerprint': 'graph',
    })
    assert response.status_code == 409, response.text
    assert 'Canvas must be saved' in response.text
    alternate = api.post('/api/v1/projects/demo/freezone/remix/actions', json={
        'action': 'render_manifest', 'payload': {'manifest_ref': {'artifact_id': manifest['id'], 'revision': 1}},
        'idempotency_key': 'v2-bypass', 'node_revision': 1,
    })
    assert alternate.status_code == 422, alternate.text
    assert 'canvas-bound render endpoint' in alternate.text


@pytest.mark.parametrize('change', ['disconnect', 'delete_node', 'hash', 'path', 'duration', 'stale', 'source_url'])
def test_v2_take_only_audio_requires_current_canvas_resource(change):
    from novelvideo.api.routes.remix import _validate_v2_canvas_bindings
    from novelvideo.freezone.remix.production_plan_v2 import compile_production_plan_v2
    from test_production_plan_v2 import _mainline_fixture

    plan = _mainline_fixture()
    take_resource_ids = {take['resource_id'] for take in plan['speech_takes']}
    plan['placements'] = [row for row in plan['placements'] if row.get('resource_id') not in take_resource_ids]
    manifest = compile_production_plan_v2(plan)
    assert manifest['audio_events']
    assert not any(row.get('resource_id') in take_resource_ids for row in plan['placements'])
    ref = {'artifact_id': 'plan', 'revision': 2}
    manifest['source_versions'] = [ref]
    manifest_ref = {'artifact_id': 'manifest', 'revision': 1}
    canvas = {'nodes': [
        {'id': 'plan-node', 'data': {'productionPlanV2Ref': {'artifactId': 'plan', 'revision': 2}}},
        {'id': 'compose', 'data': {'productionPlanNodeId': 'plan-node',
                                 'productionPlanV2Ref': {'artifactId': 'plan', 'revision': 2},
                                 'remixManifestRef': {'artifactId': 'manifest', 'revision': 1}}},
    ], 'edges': [{'source': 'plan-node', 'target': 'compose'}]}
    for resource in plan['resources']:
        media = {key: resource.get(key, False if key == 'has_audio' else None) for key in
                 ('media_hash', 'media_path', 'media_kind', 'duration_us', 'has_audio')}
        url_field = {'image': 'imageUrl', 'video': 'videoUrl', 'audio': 'audioUrl'}[resource['media_kind']]
        canvas['nodes'].append({'id': resource['node_id'], 'data': {
            url_field: f"/api/v1/projects/demo/media/{resource['media_path']}", 'remixMedia': media}})
        canvas['edges'].append({'source': resource['node_id'], 'target': 'plan-node'})
    _validate_v2_canvas_bindings(canvas, manifest, manifest_ref, plan, 'compose', 'demo')
    resource = next(row for row in plan['resources'] if row['id'] == plan['speech_takes'][0]['resource_id'])
    node = next(row for row in canvas['nodes'] if row['id'] == resource['node_id'])
    if change == 'disconnect':
        canvas['edges'] = [edge for edge in canvas['edges'] if edge['source'] != resource['node_id']]
    elif change == 'delete_node':
        canvas['nodes'].remove(node)
    elif change == 'hash':
        node['data']['remixMedia']['media_hash'] = 'f' * 64
    elif change == 'path':
        node['data']['remixMedia']['media_path'] = 'media/replaced.mp4'
    elif change == 'duration':
        node['data']['remixMedia']['duration_us'] += 1
    elif change == 'stale':
        node['data']['workflowResultStale'] = True
    elif change == 'source_url':
        node['data']['videoUrl'] = '/api/v1/projects/peer/media/secret.mp4'
    with pytest.raises(ValueError, match='Canvas media'):
        _validate_v2_canvas_bindings(canvas, manifest, manifest_ref, plan, 'compose', 'demo')
