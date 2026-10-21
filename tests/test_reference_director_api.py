from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from novelvideo.freezone.remix.production_review import REVIEW_CATEGORIES
from novelvideo.freezone.remix.reference_commission import create_reference_commission
from novelvideo.freezone.remix.store import RemixArtifactStore


@pytest.fixture
def setup_api(monkeypatch, tmp_path):
    from novelvideo.api.auth import get_api_user
    from novelvideo.api.routes import freezone, reference_director as routes, remix
    from novelvideo.freezone import canvas_store
    from novelvideo.chat.reference_remix_continuation import coordinator
    ctx = SimpleNamespace(project_id="p", state_dir=tmp_path, output_dir=tmp_path,
                          requester_user_id="u", requester_username="alice", owner_username="alice")
    async def resolve(*args, **kwargs):
        return ctx, "alice", "demo", tmp_path, str(tmp_path)
    async def runs(**kwargs):
        return []
    monkeypatch.setattr(freezone, "_resolve_freezone_project", resolve)
    monkeypatch.setattr(coordinator, "list", runs)
    monkeypatch.setattr(coordinator.store, "production_for_actor", lambda *args: [])
    store = RemixArtifactStore(tmp_path, "p", created_by="u")
    async def api_store(*args, **kwargs):
        return store
    monkeypatch.setattr(remix, "_store", api_store)
    canvas = {"project_id": "p", "canvas_id": "c", "revision": 1,
              "nodes": [{"id": "analysis", "type": "videoStoryNode", "position": {"x": 0, "y": 0}, "data": {"rawResult": {"summary": "reference"}}}], "edges": []}
    canvas_store.save_canvas(tmp_path, "c", base_revision=None, build_payload=lambda _: deepcopy(canvas))
    commission = create_reference_commission(store, canvas_id="c", requester_user_id="u", analysis_node_id="analysis", goal="产品介绍", analysis_evidence={"task_id": "real-task-fixture", "result_fingerprint": "fixture"})
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    app.include_router(remix.router, prefix="/api/v1")
    app.dependency_overrides[get_api_user] = lambda: {"username": "alice", "user_id": "u", "role": "owner"}
    with TestClient(app) as client:
        yield client, store, commission, app


BASE = "/api/v1/projects/p/freezone/canvases/c/director"


def test_unsaved_canvas_has_empty_director_inventory_without_creating_canvas(setup_api):
    from novelvideo.freezone import canvas_store

    client, store, _, _ = setup_api
    response = client.get(BASE.replace("/canvases/c/", "/canvases/user_alice_unsaved/") + "/state")
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"commissions": [], "decisions": [], "material_work": [],
                                       "speech_corrections": [], "material_reviews": [], "output_reviews": []}
    assert canvas_store.read_canvas(store.project_dir, "user_alice_unsaved") is None
    saved = client.get(BASE + "/state")
    assert len(saved.json()["data"]["commissions"]) == 1


def test_unsaved_canvas_director_cannot_create_a_decision(setup_api):
    client, _, commission, _ = setup_api
    response = client.post(BASE.replace("/canvases/c/", "/canvases/user_alice_unsaved/") + "/decisions",
                           json={"commission_ref": commission["commission_ref"], "command_id": "unsaved",
                                 "kind": "direction", "proposal": {"target_goal": "产品介绍"}, "summary": "复刻方向"})
    assert response.status_code == 404


def test_material_work_error_detail_is_bounded_and_actionable():
    from novelvideo.api.routes.reference_director import _material_work_error_detail

    error = ValueError("steps[0] generation source is invalid")
    detail = _material_work_error_detail(error, request_id="req-123")

    assert detail == {
        "code": "material_work_request_invalid",
        "phase": "material_plan_drafting",
        "message": "steps[0] generation source is invalid",
        "errors": [{"path": "material_work", "message": "steps[0] generation source is invalid"}],
        "next_action": "correct_material_request",
        "retryable": False,
        "side_effects": "none",
        "request_id": "req-123",
    }


def test_material_work_internal_error_is_safe_and_traceable():
    from novelvideo.api.routes.reference_director import _material_work_internal_error

    assert _material_work_internal_error(request_id="req-456") == {
        "code": "material_work_internal_error",
        "phase": "material_plan_drafting",
        "message": "素材工作包保存失败，服务端未能完成处理",
        "next_action": "inspect_request_id_and_correct_material_request",
        "retryable": False,
        "side_effects": "none",
        "request_id": "req-456",
    }


@pytest.mark.parametrize("canvas_patch,status", [({"project_id": "other"}, 404),
                                                  ({"access_model": "private", "owner_principal_id": "other"}, 403)])
def test_empty_director_inventory_does_not_bypass_saved_canvas_scope(setup_api, monkeypatch, canvas_patch, status):
    from novelvideo.freezone import canvas_store

    client, store, _, _ = setup_api
    canvas = canvas_store.read_canvas(store.project_dir, "c")
    monkeypatch.setattr(canvas_store, "read_canvas", lambda *args: {**canvas, **canvas_patch})
    assert client.get(BASE + "/state").status_code == status


def confirm_direction(client, commission):
    response = client.post(BASE + "/decisions", json={"commission_ref": commission["commission_ref"], "command_id": "direction-1", "kind": "direction", "proposal": {"target_goal": "产品介绍", "preserve_replace": []}, "summary": "制作产品介绍"})
    assert response.status_code == 200, response.text
    decision = response.json()["data"]["decision_ref"]
    response = client.post(BASE + "/decisions/" + decision["artifact_id"] + "/confirm")
    assert response.status_code == 200, response.text
    return response.json()["data"]["commission"]


def test_direction_confirmation_is_user_only_and_persistent(setup_api):
    from novelvideo.api.auth import get_api_user
    client, _, commission, app = setup_api
    response = client.post(BASE + "/decisions", json={"commission_ref": commission["commission_ref"], "command_id": "d", "kind": "direction", "proposal": {"target_goal": "产品介绍"}, "summary": "具体方向"})
    decision = response.json()["data"]["decision_ref"]
    app.dependency_overrides[get_api_user] = lambda: {"credential_kind": "agent_session", "username": "alice", "user_id": "u", "role": "owner"}
    assert client.post(BASE + "/decisions/" + decision["artifact_id"] + "/confirm").status_code == 403
    app.dependency_overrides[get_api_user] = lambda: {"username": "alice", "user_id": "u", "role": "owner"}
    confirmed = client.post(BASE + "/decisions/" + decision["artifact_id"] + "/confirm")
    assert confirmed.status_code == 200
    assert confirmed.json()["data"]["commission"]["phase"] == "drafting_plan"
    assert client.get(BASE + "/state").json()["data"]["decisions"] == []
    assert client.post(BASE + "/decisions/" + decision["artifact_id"] + "/confirm").status_code == 200


def test_ee_disabled_continuation_preserves_manual_decision_endpoint(setup_api, monkeypatch):
    from novelvideo.chat.reference_remix_continuation import coordinator

    monkeypatch.setenv('ST_CONTROL_PLANE_DSN', 'postgresql://configured')
    monkeypatch.delenv('ST_REFERENCE_REMIX_AUTO_CONTINUATION', raising=False)
    monkeypatch.setattr(coordinator.store, 'production_for_actor',
                        lambda *args: pytest.fail('manual decisions must not open continuation state'))
    client, _, commission, _ = setup_api
    confirmed = confirm_direction(client, commission)
    assert confirmed['phase'] == 'drafting_plan'


def test_ee_disabled_continuation_does_not_enroll_manual_advancement(setup_api, monkeypatch):
    from novelvideo.freezone.remix import production_advancement

    monkeypatch.setenv('ST_CONTROL_PLANE_DSN', 'postgresql://configured')
    monkeypatch.delenv('ST_REFERENCE_REMIX_AUTO_CONTINUATION', raising=False)
    observed = []
    async def advance(_ctx, **kwargs):
        observed.append(kwargs)
        return {'advancement': {'action': None}}
    monkeypatch.setattr(production_advancement, 'advance_production', advance)
    client, _, commission, _ = setup_api
    response = client.post(BASE + '/advance-production', json={
        'commission_ref': commission['commission_ref'],
        'expected_projection_revision': 'a' * 64,
        'command_id': 'manual-advance',
    })
    assert response.status_code == 200, response.text
    assert observed[0]['automation_enrollment_command'] is None


def test_direct_plan_and_review_then_material_work(setup_api):
    client, store, commission, _ = setup_api
    commission = confirm_direction(client, commission)
    direct = client.post(BASE + "/direct-plan", json={"commission_ref": commission["commission_ref"], "goal": "产品介绍", "output": {"width": 1280, "height": 720, "fps_num": 24, "fps_den": 1}, "command_id": "direct-1"})
    assert direct.status_code == 200, direct.text
    created = direct.json()["data"]
    from novelvideo.freezone import canvas_store
    actual_canvas = canvas_store.read_canvas(store.project_dir, "c")
    assert created["canvas_apply_status"] == "direct_applied"
    assert created["applied"] is True
    assert created["project_id"] == "p" and created["canvas_id"] == "c"
    assert created["revision"] == actual_canvas["revision"]
    assert "bridge_key" not in created
    repeated = client.post(BASE + "/direct-plan", json={"commission_ref": commission["commission_ref"], "goal": "产品介绍", "output": {"width": 1280, "height": 720, "fps_num": 24, "fps_den": 1}, "command_id": "direct-1"})
    assert repeated.json()["data"]["revision"] == actual_canvas["revision"]
    edits = client.post(BASE + "/production-batch", json={"plan_ref": created["plan_ref"], "command_id": "write-1", "commands": [{"kind": "insert_beat", "target_id": "intro", "payload": {"index": 0, "beat": {"id": "intro", "purpose": "提问", "duration_frames": 72, "turns": []}}}, {"kind": "upsert_placement", "target_id": "primary", "payload": {"placement": {"id": "primary", "kind": "visual", "role": "primary", "track_id": "main", "start": {"kind": "beat", "ref_id": "intro", "edge": "start"}, "end": {"kind": "beat", "ref_id": "intro", "edge": "end"}}}}]})
    assert edits.status_code == 200, edits.text
    ref = edits.json()["data"]["plan_ref"]
    reviewed = client.post(BASE + "/plan-review", json={"commission_ref": commission["commission_ref"], "plan_ref": ref, "findings": [{"category": category, "entity_kind": "plan", "entity_id": "plan", "verdict": "pass", "reason": "已核对当前方案与确认方向", "evidence": ["方案无台词，使用一段产品特写；目标和横屏24fps设置匹配"]} for category in REVIEW_CATEGORIES]})
    assert reviewed.status_code == 200, reviewed.text
    commission = reviewed.json()["data"]["commission"]
    assert commission["phase"] == "awaiting_plan_confirmation"
    proposed = client.post(BASE + "/decisions", json={"commission_ref": commission["commission_ref"], "command_id": "plan-ok", "kind": "plan", "proposal": {"plan_ref": commission["plan_ref"]}, "summary": "一段产品画面，无台词"}).json()["data"]
    confirmed = client.post(BASE + "/decisions/" + proposed["decision_ref"]["artifact_id"] + "/confirm")
    assert confirmed.status_code == 200, confirmed.text
    commission = confirmed.json()["data"]["commission"]
    work = client.post(BASE + "/material-work", json={"commission_ref": commission["commission_ref"], "plan_ref": ref, "command_id": "materials-1", "requirements": [{"id": "picture", "placement_ids": ["primary"], "description": "产品特写", "media_kind": "image", "final_step_id": "image"}], "steps": [{"id": "image", "label": "产品图", "media_kind": "image", "source": "upload"}]})
    assert work.status_code == 200, work.text
    assert work.json()["data"]["ready_step_ids"] == ["image"]
    assert len(store.read(created["plan_ref"]["artifact_id"], 2)["payload"]["beats"]) == 1
    generated = client.post(BASE + "/material-work", json={
        "commission_ref": commission["commission_ref"], "plan_ref": ref,
        "command_id": "generated-without-choices",
        "requirements": [{"id": "new-image", "placement_ids": ["primary"],
                          "description": "产品图片", "media_kind": "image", "final_step_id": "image-gen"}],
        "steps": [{"id": "image-gen", "label": "产品图", "media_kind": "image", "source": "generate",
                   "recipe_id": "general-image", "prompt": "产品照片"}],
    })
    assert generated.status_code == 200, generated.text
    readiness = generated.json()["data"]["generation_readiness"]
    assert readiness["confirmed_choices"] is False
    assert readiness["ready_for_native_generation"] is False
    assert readiness["missing_choices"]["image"] == [
        "model", "aspect_ratio", "resolution"]
    assert generated.json()["data"]["next_action"] == "request_user_clarification"
    assert "不要重复保存工作包或自动重试" in generated.json()["data"]["agent_instruction"]

    invalid_duration = client.post(BASE + "/material-work", json={
        "commission_ref": commission["commission_ref"], "plan_ref": ref,
        "command_id": "generated-invalid-duration",
        "requirements": [{"id": "new-video", "placement_ids": ["primary"],
                          "description": "产品视频", "media_kind": "video", "final_step_id": "video-gen"}],
        "steps": [{"id": "video-gen", "label": "产品视频", "media_kind": "video", "source": "generate",
                   "recipe_id": "general-video", "prompt": "产品演示", "duration_sec": 2}],
    })
    assert invalid_duration.status_code == 422, invalid_duration.text
    assert invalid_duration.json()["detail"]["code"] == "material_duration_invalid"


def test_task_plan_review_rejects_removed_provenance_and_stale_node(setup_api, monkeypatch):
    from novelvideo.api.routes import reference_director as routes
    from novelvideo.freezone import canvas_store
    from novelvideo.freezone.remix import plan_review_settlement

    _, store, commission, _ = setup_api
    ctx = SimpleNamespace(project_id='p', state_dir=store.project_dir,
                          requester_user_id='u')
    payload = {'schema_version': 2, 'goal': '产品介绍',
        'output': {'width': 1280, 'height': 720, 'fps_num': 24, 'fps_den': 1},
        'beats': [], 'provenance': {'node_target': {
            'canvas_id': 'c', 'node_id': 'plan', 'node_revision': 1, 'goal': '产品介绍'}}}
    plan = store.create('production_plan.v2', payload, [])
    canvas = canvas_store.read_canvas(store.project_dir, 'c')
    canvas['nodes'].append({'id': 'plan', 'type': 'scriptNode', 'position': {'x': 100, 'y': 0},
        'data': {'remixRole': 'production_plan', 'remixNodeRevision': 1,
                 'processingPayload': {'topic': '产品介绍'},
                 'productionPlanV2Ref': {'artifactId': plan['id'], 'revision': 1}}})
    canvas['edges'].append({'id': 'analysis-plan', 'source': 'analysis', 'target': 'plan'})
    canvas_store.save_canvas(store.project_dir, 'c', base_revision=canvas['revision'],
                             build_payload=lambda _: canvas)
    monkeypatch.setattr(plan_review_settlement, 'submit_plan_review',
                        lambda *args, **kwargs: pytest.fail('stale plan must not reach review'))
    revised = store.revise(plan['id'], 1, {**plan['payload'], 'provenance': {}}, [])
    with pytest.raises(Exception) as missing:
        routes._review_plan_on_current_canvas(ctx, store, 'c', commission,
            {'artifact_id': plan['id'], 'revision': revised['revision']}, [])
    assert getattr(missing.value, 'status_code', None) == 409
    canvas = canvas_store.read_canvas(store.project_dir, 'c')
    canvas['nodes'][-1]['data']['remixNodeRevision'] = 2
    canvas_store.save_canvas(store.project_dir, 'c', base_revision=canvas['revision'],
                             build_payload=lambda _: canvas)
    with pytest.raises(Exception) as stale:
        routes._review_plan_on_current_canvas(ctx, store, 'c', commission,
            {'artifact_id': plan['id'], 'revision': 1}, [])
    assert getattr(stale.value, 'status_code', None) == 409


def test_plan_review_accepts_guidance_line_that_the_signed_goal_omits(setup_api, monkeypatch):
    """A node topic carrying host guidance still names the same creative goal."""
    from novelvideo.api.routes import reference_director as routes
    from novelvideo.freezone import canvas_store
    from novelvideo.freezone.remix import plan_review_settlement

    _, store, commission, _ = setup_api
    ctx = SimpleNamespace(project_id='p', state_dir=store.project_dir,
                          requester_user_id='u')
    goal = '产品介绍'
    payload = {'schema_version': 2, 'goal': goal,
        'output': {'width': 1280, 'height': 720, 'fps_num': 24, 'fps_den': 1},
        'beats': [], 'provenance': {'node_target': {
            'canvas_id': 'c', 'node_id': 'plan', 'node_revision': 1, 'goal': goal}}}
    plan = store.create('production_plan.v2', payload, [])
    canvas = canvas_store.read_canvas(store.project_dir, 'c')
    canvas['nodes'].append({'id': 'plan', 'type': 'scriptNode', 'position': {'x': 100, 'y': 0},
        'data': {'remixRole': 'production_plan', 'remixNodeRevision': 1,
                 'processingPayload': {'topic': goal + '\n声音方式：自动判断'},
                 'productionPlanV2Ref': {'artifactId': plan['id'], 'revision': 1}}})
    canvas['edges'].append({'id': 'analysis-plan', 'source': 'analysis', 'target': 'plan'})
    canvas_store.save_canvas(store.project_dir, 'c', base_revision=canvas['revision'],
                             build_payload=lambda _: canvas)
    captured = {}
    def submit(*args, **kwargs):
        captured.update(kwargs)
        return {'review_ref': {'artifact_id': 'review', 'revision': 1}}
    monkeypatch.setattr(plan_review_settlement, 'submit_plan_review', submit)
    routes._review_plan_on_current_canvas(ctx, store, 'c', commission,
        {'artifact_id': plan['id'], 'revision': 1}, [])
    assert captured['plan_ref']['artifact_id'] == plan['id']


@pytest.mark.parametrize('delivered', [False, True])
def test_generated_plan_review_saves_with_task_evidence_and_waits_for_user(setup_api, delivered):
    from novelvideo.freezone import canvas_store
    from novelvideo.freezone.remix.operations import RemixOperationStore

    client, store, commission, _ = setup_api
    commission = confirm_direction(client, commission)
    goal = '产品介绍\n声音方式：自动判断'
    operations = RemixOperationStore(store.project_dir, 'p')
    operation = operations.start(idempotency_key='review-generated-plan', input_refs=[],
        producer_version='remix-process.v1', node_revision=1, canvas_id='c', node_id='plan',
        normalized_parameters={'action': 'generate_production_plan_draft',
                               'payload': {'topic': goal, 'requirements': {}}})
    operation_id = operation['operation_id']
    fence = operations.claim_submission(operation_id, owner='worker')
    operations.bind_task(operation_id, task_id='generated-plan-task', owner='worker', fencing_token=fence)
    plan = store.create('production_plan.v2', {
        'schema_version': 2, 'goal': goal,
        'output': {'width': 1280, 'height': 720, 'fps_num': 24, 'fps_den': 1},
        'beats': [{'id': 'intro', 'purpose': '产品介绍', 'duration_frames': 48}],
        'provenance': {'operation_id': operation_id, 'requirements': {}, 'node_target': {
            'canvas_id': 'c', 'node_id': 'plan', 'node_revision': 1, 'goal': goal}},
    }, [])
    ref = {'artifact_id': plan['id'], 'revision': 1}
    operations.publish_result_unverified(operation_id, output_refs=[ref])
    if delivered:
        operations.confirm(operation_id, current_input_refs=[], current_node_revision=1,
                           canvas_id='c', node_id='plan')
    canvas = canvas_store.read_canvas(store.project_dir, 'c')
    canvas['nodes'].append({'id': 'plan', 'type': 'scriptNode', 'data': {
        'remixRole': 'production_plan', 'remixNodeRevision': 1,
        'processingPayload': {'topic': goal, 'requirements': {}},
        'productionPlanV2Ref': {'artifactId': plan['id'], 'revision': 1},
    }})
    canvas['edges'].append({'id': 'analysis-plan', 'source': 'analysis', 'target': 'plan'})
    canvas_store.save_canvas(store.project_dir, 'c', base_revision=canvas['revision'],
                             build_payload=lambda _: canvas)
    findings = [{'category': category, 'entity_kind': 'beat', 'entity_id': 'intro',
                 'verdict': 'pass', 'reason': '产品介绍方向已落实到开场段落',
                 'evidence': ['intro 的 purpose 为产品介绍']} for category in REVIEW_CATEGORIES]

    response = client.post(BASE + '/plan-review', json={
        'commission_ref': commission['commission_ref'], 'plan_ref': ref, 'findings': findings})

    assert response.status_code == 200, response.text
    result = response.json()['data']
    assert result['review']['status'] == 'passed'
    assert result['commission']['phase'] == 'awaiting_plan_confirmation'
    assert result['decision']['status'] == 'pending'
    assert store.read(result['review_ref']['artifact_id'], result['review_ref']['revision'])['payload']['status'] == 'passed'
    # Recording the review neither confirms the plan nor submits material generation.
    assert result['commission'].get('work_ref') is None
    assert operations.get(operation_id)['task_id'] == 'generated-plan-task'


def test_plan_review_unexpected_failure_logs_exact_refs(setup_api, monkeypatch, caplog):
    import json
    import logging
    from novelvideo.freezone.remix import plan_review_settlement

    client, _, commission, _ = setup_api
    commission = confirm_direction(client, commission)
    direct = client.post(BASE + '/direct-plan', json={
        'commission_ref': commission['commission_ref'], 'goal': '产品介绍',
        'output': {'width': 1280, 'height': 720, 'fps_num': 24, 'fps_den': 1},
        'command_id': 'diagnostic-plan',
    })
    assert direct.status_code == 200, direct.text
    plan_ref = direct.json()['data']['plan_ref']

    def fail_review(*_args, **_kwargs):
        raise AttributeError('private user content')

    monkeypatch.setattr(plan_review_settlement, 'submit_plan_review', fail_review)
    with caplog.at_level(logging.ERROR, logger='novelvideo.api.reference_operation_logging'):
        with pytest.raises(AttributeError, match='private user content'):
            client.post(BASE + '/plan-review', json={
                'commission_ref': commission['commission_ref'],
                'plan_ref': plan_ref, 'findings': [],
            })
    records = [json.loads(row.message) for row in caplog.records
               if row.name == 'novelvideo.api.reference_operation_logging'
               and 'current_plan_ref' in json.loads(row.message)]
    assert records[-1]['commission_ref']['artifact_id'] == commission['commission_ref']['artifact_id']
    assert records[-1]['current_plan_ref']['artifact_id'] == plan_ref['artifact_id']
    assert records[-1]['error_code'] == 'AttributeError'
    assert 'private user content' not in caplog.text


@pytest.mark.asyncio
async def test_material_work_reads_only_saved_choices_for_current_commission(monkeypatch, tmp_path):
    from novelvideo.api.routes.reference_director import _saved_material_generation_choices
    from novelvideo.chat.reference_remix_continuation import coordinator
    from novelvideo.freezone import workflow_drafts
    from novelvideo.freezone.remix.reference_parameter_receipts import generation_requirements_digest

    ctx = SimpleNamespace(project_id="project", state_dir=tmp_path,
                          requester_user_id="user")
    commission = {"commission_ref": {"artifact_id": "current"},
                  "direction": {"target_goal": "product film"},
                  "direction_revision": 1, "production_scope": "editable"}
    run = {"commission_ref": {"artifact_id": "current"}, "agent_id": "agent",
           "status": "running", "inputs": {"video_model": "chosen-model"}}
    scope = {"project_id": "project", "canvas_id": "canvas", "agent_id": "agent",
             "requester_user_id": "user", "commission_id": "current"}
    receipt = {"bridge_key": "card", "clarification_id": "question", "scope": scope,
               "generation_requirements_digest": generation_requirements_digest(commission, run["inputs"]),
               "event": {"questions": [{"id": "video_model"}]},
               "result": {"ok": True, "status": "clarification_frontend_result",
                          "answers": {"video_model": "chosen-model"}}}
    run["generation_parameter_receipts"] = [receipt]
    monkeypatch.setattr(coordinator.store, "list_for_actor", lambda *_: [run])
    monkeypatch.setattr(workflow_drafts, "read_reference_parameter_submissions", lambda **_: [])

    assert await _saved_material_generation_choices(ctx, "canvas", commission) == {
        "video_model": "chosen-model"}
    monkeypatch.setattr(coordinator.store, "list_for_actor", lambda *_: [
        run, {**run, "run_id": "another-run"}])
    assert await _saved_material_generation_choices(ctx, "canvas", commission) == {}
    monkeypatch.setattr(coordinator.store, "list_for_actor", lambda *_: [run])
    assert await _saved_material_generation_choices(ctx, "canvas", {
        **commission, "direction_revision": 2}) == {}
    monkeypatch.setattr(coordinator.store, "list_for_actor", lambda *_: [
        {**run, "commission_ref": {"artifact_id": "other"}}])
    assert await _saved_material_generation_choices(ctx, "canvas", commission) == {}


def test_direct_plan_accepts_agent_integer_fps_and_saves_v2_rate(setup_api):
    client, store, commission, _ = setup_api
    commission = confirm_direction(client, commission)
    response = client.post(BASE + "/direct-plan", json={"commission_ref": commission["commission_ref"],
        "goal": "产品介绍", "output": {"width": 1080, "height": 1920, "fps": 30}, "command_id": "agent-fps"})
    assert response.status_code == 200, response.text
    ref = response.json()["data"]["plan_ref"]
    assert store.read(ref["artifact_id"], ref["revision"])["payload"]["output"] == {
        "width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1, "background": "#000000"}


def test_agent_cannot_create_plan_nodes_without_canvas_approval(setup_api):
    from novelvideo.api.auth import get_api_user
    from novelvideo.freezone import canvas_store
    client, store, commission, app = setup_api
    commission = confirm_direction(client, commission)
    app.dependency_overrides[get_api_user] = lambda: {"credential_kind": "agent_session", "username": "alice", "user_id": "u", "role": "owner"}
    response = client.post(BASE + "/direct-plan", json={"commission_ref": commission["commission_ref"],
        "goal": "产品介绍", "output": {"width": 1080, "height": 1920, "fps": 30}, "command_id": "must-confirm"})
    assert response.status_code == 409
    assert "workflow" in response.text.lower() or "确认" in response.text
    assert not any(node.get("data", {}).get("remixRole") == "production_plan" for node in canvas_store.read_canvas(store.project_dir, "c")["nodes"])


def test_direct_plan_rejects_invalid_output_as_client_error(setup_api):
    client, _, commission, _ = setup_api
    commission = confirm_direction(client, commission)
    response = client.post(BASE + "/direct-plan", json={"commission_ref": commission["commission_ref"],
        "goal": "产品介绍", "output": {"width": 1080, "height": 1920, "fps": "fast"}, "command_id": "invalid-fps"})
    assert response.status_code == 422


def test_malformed_requests_and_forged_user_event_rejected(setup_api):
    client, _, commission, _ = setup_api
    assert client.post(BASE + "/plan-review", json={}).status_code == 422
    assert client.post(BASE + "/material-work", json={}).status_code == 422
    assert client.post(BASE + "/advance", json={"commission_ref": commission["commission_ref"], "event": {"type": "direction_confirmed", "direction": {"target_goal": "invented"}}}).status_code == 403


def test_protected_decisions_cannot_be_saved_via_generic_artifact_api(setup_api):
    client, _, commission, _ = setup_api
    response = client.post("/api/v1/projects/p/freezone/remix/artifacts", json={"schema": "reference_commission.v1", "payload": commission, "upstream_refs": []})
    assert response.status_code == 422
    response = client.put("/api/v1/projects/p/freezone/remix/artifacts/" + commission["commission_ref"]["artifact_id"], json={"expected_revision": 1, "payload": {**commission, "phase": "completed"}, "upstream_refs": []})
    assert response.status_code == 422
