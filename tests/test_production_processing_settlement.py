from novelvideo.freezone.workflow_runs import (
    create_workflow_run,
    read_workflow_run,
    reconcile_workflow_runs_with_tasks,
    update_workflow_run,
)
from types import SimpleNamespace
import hashlib
import json
import pytest

from novelvideo.freezone import canvas_store
from novelvideo.freezone.remix.operations import RemixOperationStore
from novelvideo.freezone.remix.store import RemixArtifactStore
from novelvideo.freezone.workflow_runs import bind_workflow_action_processing_operation


def test_failed_native_plan_blocks_dependents_and_finishes_run(tmp_path):
    run = create_workflow_run(
        project_dir=tmp_path,
        project_id="project",
        canvas_id="canvas",
        actor_id="user",
        actions=[
            {"node_id": "plan", "action": "generate_production_plan_draft",
             "node_revision": 1, "payload": {"topic": "coffee"}},
            {"node_id": "compose", "action": "compile_production_plan",
             "node_revision": 1, "payload": {"plan_ref": {"artifact_id": "placeholder", "revision": 1}},
             "depends_on": ["plan"]},
        ],
    )
    update_workflow_run(
        project_dir=tmp_path, canvas_id="canvas", run_id=run["run_id"],
        action_updates=[{"node_id": "plan", "action": "generate_production_plan_draft",
                         "status": "running", "task_key": "task:plan"}],
    )

    reconcile_workflow_runs_with_tasks(
        project_dir=tmp_path, canvas_id="canvas",
        tasks_by_key={"task:plan": {"task_id": "task-id", "status": "failed",
                                    "result": None, "error": "Request timed out."}},
    )

    settled = read_workflow_run(project_dir=tmp_path, canvas_id="canvas", run_id=run["run_id"])
    assert settled["status"] == "failed"
    assert [(action["node_id"], action["status"]) for action in settled["actions"]] == [
        ("plan", "failed"), ("compose", "blocked")]
    assert settled["actions"][0]["error"] == "Request timed out."


def _saved_plan_run(tmp_path, *, reconcile=True, metadata=None, plan_payload=None, publish_result=True):
    state_dir, output_dir = tmp_path / "state", tmp_path / "output"
    ctx = SimpleNamespace(project_id="project", requester_user_id="user",
                          state_dir=state_dir, output_dir=output_dir)
    canvas = canvas_store.default_canvas_payload(project_id="project", actor_id="user")
    canvas["nodes"] = [
        {"id": "plan", "type": "scriptNode", "data": {
            "remixRole": "production_plan", "remixNodeRevision": 1,
            "processingAction": "generate_production_plan_draft",
            "processingPayload": {"topic": "coffee"},
            "productionWorkflowSubmission": {
                "action": "generate_production_plan_draft",
                "payload": {"topic": "coffee", "analysis_context": {"summary": "scene"}},
                "requestPayload": {"topic": "coffee"},
                "nodeData": '{"action":"generate_production_plan_draft","payload":{"topic":"coffee"},"revision":1,"remixRole":"production_plan","productionPlanNodeId":null}',
                "planRef": None, "analysisData": None,
            },
        }},
        {"id": "compose", "type": "videoComposeNode", "data": {"productionPlanNodeId": "plan"}},
    ]
    canvas["edges"] = [{"source": "plan", "target": "compose"}]
    path = canvas_store.canvas_path(state_dir, "default")
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas_store.atomic_write_json(path, canvas)
    payload = {"topic": "coffee", "analysis_context": {"summary": "scene"}}
    run = create_workflow_run(
        project_dir=state_dir, project_id="project", canvas_id="default", actor_id="user",
        metadata=metadata,
        actions=[{"node_id": "plan", "action": "generate_production_plan_draft",
                  "node_revision": 1, "payload": payload}],
    )
    operations = RemixOperationStore(output_dir, "project")
    operation = operations.start(
        idempotency_key="plan-submit", input_refs=[], producer_version="remix-process.v1",
        normalized_parameters={"action": "generate_production_plan_draft", "payload": payload},
        node_revision=1, canvas_id="default", node_id="plan",
    )
    owner = "test"
    token = operations.claim_submission(operation["operation_id"], owner=owner)
    assert token is not None
    operations.bind_task(operation["operation_id"], task_id="task-id", owner=owner, fencing_token=token)
    bind_workflow_action_processing_operation(
        project_dir=state_dir, canvas_id="default", run_id=run["run_id"], node_id="plan",
        action="generate_production_plan_draft", operation_id=operation["operation_id"],
    )
    update_workflow_run(
        project_dir=state_dir, canvas_id="default", run_id=run["run_id"],
        action_updates=[{"node_id": "plan", "action": "generate_production_plan_draft",
                         "status": "running", "task_key": "task:plan"}],
    )
    artifact = RemixArtifactStore(output_dir, "project").create(
        "production_plan.v2",
        plan_payload or {"schema_version": 2, "goal": "coffee", "output": {"width": 1920, "height": 1080,
         "fps_num": 24, "fps_den": 1}, "beats": [{"id": "beat", "purpose": "coffee", "duration_frames": 48}]}, [],
    )
    ref = {"artifact_id": artifact["id"], "revision": artifact["revision"]}
    if publish_result:
        operations.publish_result_unverified(operation["operation_id"], output_refs=[ref])
    if reconcile:
        reconcile_workflow_runs_with_tasks(
            project_dir=state_dir, canvas_id="default", artifact_project_dir=output_dir,
            tasks_by_key={"task:plan": {"task_id": "task-id", "status": "completed",
                                        "result": {"artifact_ref": ref}}},
        )
    return ctx, run["run_id"], ref, operation["operation_id"]


def test_completed_native_plan_is_published_to_its_original_canvas_node(tmp_path):
    from novelvideo.freezone.remix.production_processing_settlement import settle_production_processing

    ctx, run_id, ref, operation_id = _saved_plan_run(tmp_path)
    result = settle_production_processing(ctx, canvas_id="default", run_id=run_id)

    canvas = canvas_store.read_canvas(ctx.state_dir, "default")
    plan = next(node for node in canvas["nodes"] if node["id"] == "plan")
    compose = next(node for node in canvas["nodes"] if node["id"] == "compose")
    assert result["published"] == ["plan"]
    assert plan["data"]["productionPlanV2Ref"] == {"artifactId": ref["artifact_id"], "revision": 1}
    assert plan["data"]["productionPlanV2Draft"]["goal"] == "coffee"
    assert compose["data"]["productionPlanV2Ref"] == plan["data"]["productionPlanV2Ref"]
    assert RemixOperationStore(ctx.output_dir, ctx.project_id).get(operation_id)['status'] == 'completed'
    assert settle_production_processing(ctx, canvas_id="default", run_id=run_id)["published"] == []


def test_plan_delivery_recovers_after_canvas_write_before_operation_completion(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.production_processing_settlement import settle_production_processing

    ctx, run_id, ref, operation_id = _saved_plan_run(tmp_path)
    finish = RemixOperationStore.finish_confirmation

    def interrupted(*args, **kwargs):
        raise OSError('interrupted after canvas write')

    monkeypatch.setattr(RemixOperationStore, 'finish_confirmation', interrupted)
    with pytest.raises(OSError, match='interrupted after canvas write'):
        settle_production_processing(ctx, canvas_id='default', run_id=run_id)
    saved = canvas_store.read_canvas(ctx.state_dir, 'default')
    assert saved['nodes'][0]['data']['productionPlanV2Ref'] == {
        'artifactId': ref['artifact_id'], 'revision': ref['revision']}
    monkeypatch.setattr(RemixOperationStore, 'finish_confirmation', finish)

    assert settle_production_processing(ctx, canvas_id='default', run_id=run_id)['published'] == []
    assert canvas_store.read_canvas(ctx.state_dir, 'default') == saved
    assert RemixOperationStore(ctx.output_dir, ctx.project_id).get(operation_id)['status'] == 'completed'


def test_plan_delivery_recovers_an_expired_claim_without_a_canvas_result(tmp_path, monkeypatch):
    from novelvideo.freezone.remix import operations as operation_module
    from novelvideo.freezone.remix.production_processing_settlement import settle_production_processing

    ctx, run_id, _, operation_id = _saved_plan_run(tmp_path)
    operations = RemixOperationStore(ctx.output_dir, ctx.project_id)
    claimed = operations.claim_confirmation(operation_id, canvas_id='default', node_id='plan')
    before = canvas_store.read_canvas(ctx.state_dir, 'default')
    assert settle_production_processing(ctx, canvas_id='default', run_id=run_id)['published'] == []
    assert canvas_store.read_canvas(ctx.state_dir, 'default') == before
    monkeypatch.setattr(operation_module.time, 'time', lambda: claimed['confirmation_claimed_at'] + 61)

    assert settle_production_processing(ctx, canvas_id='default', run_id=run_id)['published'] == ['plan']
    assert operations.get(operation_id)['status'] == 'completed'


def test_cancelled_plan_operation_does_not_publish_a_saved_result(tmp_path):
    from novelvideo.freezone.remix.production_processing_settlement import settle_production_processing

    ctx, run_id, _, operation_id = _saved_plan_run(tmp_path)
    operations = RemixOperationStore(ctx.output_dir, ctx.project_id)
    operations.cancel(operation_id, reason='user cancelled')
    before = canvas_store.read_canvas(ctx.state_dir, 'default')

    result = settle_production_processing(ctx, canvas_id='default', run_id=run_id)
    assert result['published'] == []
    assert result['conflicts'] == ['plan']
    assert canvas_store.read_canvas(ctx.state_dir, 'default') == before
    assert operations.get(operation_id)['status'] == 'cancelled'


@pytest.mark.parametrize('change_before_result', [False, True])
def test_native_draft_replaces_only_the_same_unchanged_empty_saved_plan(tmp_path, change_before_result):
    from novelvideo.freezone.remix.production_processing_settlement import settle_production_processing

    ctx, run_id, generated_ref, _ = _saved_plan_run(tmp_path)
    store = RemixArtifactStore(ctx.output_dir, ctx.project_id)
    empty = store.create('production_plan.v2', {
        'schema_version': 2, 'goal': 'coffee',
        'output': {'width': 1920, 'height': 1080, 'fps_num': 24, 'fps_den': 1},
        'beats': [], 'resources': [], 'placements': [],
    }, [])
    saved_ref = {'artifactId': empty['id'], 'revision': empty['revision']}
    canvas = canvas_store.read_canvas(ctx.state_dir, 'default')
    data = canvas['nodes'][0]['data']
    data['productionPlanV2Ref'] = saved_ref
    data['productionPlanV2Draft'] = empty['payload']
    data['productionWorkflowSubmission']['planRef'] = {
        'artifact_id': empty['id'], 'revision': empty['revision']}
    if change_before_result:
        data['productionPlanV2Draft'] = {**empty['payload'], 'goal': 'new human goal'}
    canvas_store.atomic_write_json(canvas_store.canvas_path(ctx.state_dir, 'default'), canvas)

    result = settle_production_processing(ctx, canvas_id='default', run_id=run_id)
    current = canvas_store.read_canvas(ctx.state_dir, 'default')['nodes'][0]['data']
    if change_before_result:
        assert result['conflicts'] == ['plan']
        assert current['productionPlanV2Ref'] == saved_ref
    else:
        assert result['published'] == ['plan']
        assert current['productionPlanV2Ref'] == {
            'artifactId': generated_ref['artifact_id'], 'revision': generated_ref['revision']}


def test_late_plan_result_cannot_replace_a_user_edit(tmp_path):
    from novelvideo.freezone.remix.production_processing_settlement import settle_production_processing

    ctx, run_id, _ref, _operation_id = _saved_plan_run(tmp_path)
    path = canvas_store.canvas_path(ctx.state_dir, "default")
    canvas = canvas_store.read_canvas(ctx.state_dir, "default")
    canvas["nodes"][0]["data"]["productionPlanV2Dirty"] = True
    canvas_store.atomic_write_json(path, canvas)

    result = settle_production_processing(ctx, canvas_id="default", run_id=run_id)

    assert result["conflicts"] == ["plan"]
    assert canvas_store.read_canvas(ctx.state_dir, "default") == canvas
    repeated = settle_production_processing(ctx, canvas_id="default", run_id=run_id)
    assert repeated["conflict_records"] == result["conflict_records"]


@pytest.mark.parametrize("change", ["removed", "replaced"])
def test_plan_result_cannot_publish_after_analysis_source_changes(tmp_path, change):
    from novelvideo.freezone.remix.production_processing_settlement import settle_production_processing

    ctx, run_id, _ref, _operation_id = _saved_plan_run(tmp_path)
    path = canvas_store.canvas_path(ctx.state_dir, "default")
    canvas = canvas_store.read_canvas(ctx.state_dir, "default")
    analysis_data = {"rawResult": {"summary": "source"}, "blueprintRef": None,
                     "blueprintDirty": False, "sourceVideoUrl": None}
    canvas["nodes"].append({"id": "analysis", "type": "videoStoryNode", "data": {
        "rawResult": {"summary": "source"}}})
    canvas["edges"].append({"source": "analysis", "target": "plan"})
    canvas["nodes"][0]["data"]["productionWorkflowSubmission"]["analysisNodeId"] = "analysis"
    canvas["nodes"][0]["data"]["productionWorkflowSubmission"]["analysisData"] = json.dumps(analysis_data)
    if change == "removed":
        canvas["edges"] = [edge for edge in canvas["edges"] if edge["source"] != "analysis"]
    else:
        canvas["nodes"].append({"id": "other-analysis", "type": "videoStoryNode", "data": {
            "rawResult": {"summary": "replacement"}}})
        canvas["edges"] = [{**edge, "source": "other-analysis"} if edge["source"] == "analysis" else edge
                           for edge in canvas["edges"]]
    canvas_store.atomic_write_json(path, canvas)

    result = settle_production_processing(ctx, canvas_id="default", run_id=run_id)

    assert result["conflicts"] == ["plan"]
    assert canvas_store.read_canvas(ctx.state_dir, "default") == canvas


def test_task_completion_settles_run_and_publishes_without_a_browser(tmp_path):
    from novelvideo.freezone.remix.production_processing_settlement import settle_native_task_delivery

    ctx, run_id, ref, operation_id = _saved_plan_run(tmp_path, reconcile=False)
    task = SimpleNamespace(task_id="task-id", status="completed", result={"artifact_ref": ref}, error=None)
    manager = SimpleNamespace(get_task_for_project=lambda *_args, **_kwargs: task,
                              _task_key_for_state=lambda _task: "task:plan")

    settle_native_task_delivery(
        ctx, manager, payload={"workflow_run_id": run_id, "operation_id": operation_id},
        task_type="remix_generate_production_plan_draft", expected_task_id="task-id",
        episode=0, scope=operation_id,
    )

    run = read_workflow_run(project_dir=ctx.state_dir, canvas_id="default", run_id=run_id)
    canvas = canvas_store.read_canvas(ctx.state_dir, "default")
    assert run["status"] == "completed"
    assert canvas["nodes"][0]["data"]["productionPlanV2Ref"] == {"artifactId": ref["artifact_id"], "revision": 1}


def test_audio_source_draft_is_published_only_after_saved_native_completion(tmp_path):
    from pathlib import Path
    from novelvideo.freezone.remix.production_processing_settlement import settle_native_task_delivery

    fixture = Path(__file__).parent / "fixtures" / "production-audio-source-plan.json"
    plan_payload = json.loads(fixture.read_text(encoding="utf-8"))
    ctx, run_id, ref, operation_id = _saved_plan_run(tmp_path, reconcile=False, plan_payload=plan_payload)
    task = SimpleNamespace(task_id="task-id", status="completed", result={"artifact_ref": ref}, error=None)
    manager = SimpleNamespace(get_task_for_project=lambda *_args, **_kwargs: task,
                              _task_key_for_state=lambda _task: "task:plan")
    before = canvas_store.read_canvas(ctx.state_dir, "default")
    assert "productionPlanV2Ref" not in before["nodes"][0]["data"]

    payload = {"workflow_run_id": run_id, "operation_id": operation_id}
    settle_native_task_delivery(ctx, manager, payload=payload,
                                task_type="remix_generate_production_plan_draft",
                                expected_task_id="task-id", episode=0, scope=operation_id)
    published = canvas_store.read_canvas(ctx.state_dir, "default")["nodes"][0]["data"]
    assert published["productionPlanV2Ref"] == {"artifactId": ref["artifact_id"], "revision": 1}
    assert [turn["speech_placement_id"] for turn in published["productionPlanV2Draft"]["beats"][0]["turns"]] == ["demo-video", "demo-video"]
    assert published["productionPlanV2Draft"]["placements"][0]["source_audio"] is True
    settle_native_task_delivery(ctx, manager, payload=payload,
                                task_type="remix_generate_production_plan_draft",
                                expected_task_id="task-id", episode=0, scope=operation_id)
    assert canvas_store.read_canvas(ctx.state_dir, "default")["nodes"][0]["data"] == published


def test_failed_native_draft_cannot_publish_unclaimed_artifact(tmp_path):
    from novelvideo.freezone.remix.production_processing_settlement import settle_native_task_delivery

    ctx, run_id, ref, operation_id = _saved_plan_run(tmp_path, reconcile=False, publish_result=False)
    task = SimpleNamespace(task_id="task-id", status="failed", result=None, error="model_output_limit")
    manager = SimpleNamespace(get_task_for_project=lambda *_args, **_kwargs: task,
                              _task_key_for_state=lambda _task: "task:plan")
    settle_native_task_delivery(ctx, manager,
                                payload={"workflow_run_id": run_id, "operation_id": operation_id},
                                task_type="remix_generate_production_plan_draft",
                                expected_task_id="task-id", episode=0, scope=operation_id)
    run = read_workflow_run(project_dir=ctx.state_dir, canvas_id="default", run_id=run_id)
    assert run["status"] == "failed"
    assert run["actions"][0]["error"] == "model_output_limit"
    canvas = canvas_store.read_canvas(ctx.state_dir, "default")
    assert "productionPlanV2Ref" not in canvas["nodes"][0]["data"]
    assert RemixArtifactStore(ctx.output_dir, ctx.project_id).read(ref["artifact_id"], 1)["id"] == ref["artifact_id"]


def test_plan_result_does_not_overwrite_compose_bound_to_another_plan(tmp_path):
    from novelvideo.freezone.remix.production_processing_settlement import settle_production_processing

    ctx, run_id, _ref, _operation_id = _saved_plan_run(tmp_path)
    path = canvas_store.canvas_path(ctx.state_dir, "default")
    canvas = canvas_store.read_canvas(ctx.state_dir, "default")
    canvas["nodes"][1]["data"]["productionPlanNodeId"] = "other-plan"
    canvas_store.atomic_write_json(path, canvas)

    result = settle_production_processing(ctx, canvas_id="default", run_id=run_id)

    assert result["conflicts"] == ["plan"]
    assert canvas_store.read_canvas(ctx.state_dir, "default") == canvas


def test_cancelled_plan_run_never_publishes_late_result(tmp_path):
    from novelvideo.freezone.remix.production_processing_settlement import settle_production_processing

    ctx, run_id, _ref, _operation_id = _saved_plan_run(tmp_path, reconcile=False)
    update_workflow_run(project_dir=ctx.state_dir, canvas_id="default", run_id=run_id,
                        status="cancelled")
    before = canvas_store.read_canvas(ctx.state_dir, "default")

    result = settle_production_processing(ctx, canvas_id="default", run_id=run_id)

    assert result["published"] == []
    assert canvas_store.read_canvas(ctx.state_dir, "default") == before


def test_plan_with_missing_confirmed_direction_draft_records_conflict(tmp_path):
    from novelvideo.freezone.remix.production_processing_settlement import (
        read_processing_settlement_conflicts, settle_production_processing,
    )

    ctx, run_id, _ref, operation_id = _saved_plan_run(tmp_path)
    path = canvas_store.canvas_path(ctx.state_dir, "default")
    canvas = canvas_store.read_canvas(ctx.state_dir, "default")
    canvas["nodes"][0]["data"].update(workflowInstanceId="missing_draft",
                                      workflowPlanNodeId="plan_template")
    canvas_store.atomic_write_json(path, canvas)

    result = settle_production_processing(ctx, canvas_id="default", run_id=run_id)

    assert result["conflicts"] == ["plan"]
    assert canvas_store.read_canvas(ctx.state_dir, "default") == canvas
    assert read_processing_settlement_conflicts(ctx, canvas_id="default", run_id=run_id) == [
        result["conflict_records"][0]]
    assert result["conflict_records"][0]["operation_id"] == operation_id


def test_plan_binding_rejects_another_actor_even_on_a_shared_canvas(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.production_processing_settlement import _plan_binding_current
    from novelvideo.freezone import workflow_drafts
    from novelvideo.freezone.remix import reference_commission
    from novelvideo.chat import reference_production_observer

    ctx = SimpleNamespace(project_id="project", requester_user_id="other-user",
                          state_dir=tmp_path / "state", output_dir=tmp_path / "output")
    node = {"id": "plan", "data": {"workflowInstanceId": "draft", "workflowPlanNodeId": "slot"}}
    draft = {"project_id": "project", "status": "confirmed", "compiled": {"reference_binding": {
        "project_id": "project", "canvas_id": "canvas", "requester_user_id": "owner",
        "commission_id": "commission", "direction_revision": 2,
    }}}
    monkeypatch.setattr(workflow_drafts, "read_workflow_draft", lambda **_kwargs: (draft, None))
    monkeypatch.setattr(reference_commission, "read_reference_commission", lambda *_args, **_kwargs: {
        "requester_user_id": "owner"})
    monkeypatch.setattr(reference_production_observer, "_bound_plan_node", lambda *_args: True)

    assert not _plan_binding_current(ctx, "canvas", node, RemixArtifactStore(ctx.output_dir, ctx.project_id))


def test_stopped_automation_operation_cannot_publish_a_plan(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.production_processing_settlement import settle_production_processing
    from novelvideo.freezone.remix import production_automation

    ctx, run_id, _ref, _operation_id = _saved_plan_run(tmp_path)
    before = canvas_store.read_canvas(ctx.state_dir, "default")
    monkeypatch.setattr(production_automation, "assert_operation_automation_current",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("production automation stopped")))

    result = settle_production_processing(ctx, canvas_id="default", run_id=run_id)

    assert result["conflicts"] == ["plan"]
    assert canvas_store.read_canvas(ctx.state_dir, "default") == before


def test_manual_plan_result_publishes_while_automation_remains_stopped(tmp_path, monkeypatch):
    from novelvideo.freezone.remix import production_automation, production_processing_settlement

    metadata = {"native_execution_source": "canvas_user", "native_execution_context": {
        "source": "canvas_user", "requester_user_id": "user", "project_id": "project",
        "canvas_id": "default",
    }}
    ctx, run_id, ref, _operation_id = _saved_plan_run(tmp_path, metadata=metadata)
    control = {"active": False, "generation": 3}
    monkeypatch.setattr(production_processing_settlement, "native_action_automation_control",
                        lambda *_args, **_kwargs: control)
    monkeypatch.setattr(production_automation, "assert_operation_automation_current",
                        lambda *_args, **_kwargs: None)

    result = production_processing_settlement.settle_production_processing(
        ctx, canvas_id="default", run_id=run_id)

    assert result["published"] == ["plan"]
    assert control == {"active": False, "generation": 3}
    canvas = canvas_store.read_canvas(ctx.state_dir, "default")
    assert canvas["nodes"][0]["data"]["productionPlanV2Ref"] == {
        "artifactId": ref["artifact_id"], "revision": ref["revision"],
    }


@pytest.mark.parametrize("kind", ["plan", "compile", "speech"])
def test_stopped_native_action_with_missing_operation_binding_cannot_publish(tmp_path, monkeypatch, kind):
    from novelvideo.freezone.remix import production_automation
    from novelvideo.freezone.remix import production_processing_settlement as settlement

    ctx, run_id = {
        "plan": lambda: _saved_plan_run(tmp_path)[:2],
        "compile": lambda: _saved_compile_run(tmp_path)[:2],
        "speech": lambda: _saved_speech_run(tmp_path)[:2],
    }[kind]()
    before = canvas_store.read_canvas(ctx.state_dir, "default")
    monkeypatch.setattr(settlement, "native_action_automation_control",
                        lambda *_args, **_kwargs: {"active": False, "generation": 1})
    monkeypatch.setattr(production_automation, "assert_operation_automation_current",
                        lambda *_args, **_kwargs: None)

    outcome = settlement.settle_production_processing(ctx, canvas_id="default", run_id=run_id)

    assert outcome["conflicts"] == ["compose" if kind == "compile" else "plan"]
    assert canvas_store.read_canvas(ctx.state_dir, "default") == before


def _completed_processing_run(ctx, *, node_id, action, node_revision, payload, artifact_ref):
    run = create_workflow_run(
        project_dir=ctx.state_dir, project_id=ctx.project_id, canvas_id="default",
        actor_id=ctx.requester_user_id,
        actions=[{"node_id": node_id, "action": action,
                  "node_revision": node_revision, "payload": payload}],
    )
    operations = RemixOperationStore(ctx.output_dir, ctx.project_id)
    operation = operations.start(
        idempotency_key=f"{action}-{node_id}", input_refs=[payload["plan_ref"]],
        producer_version="remix-process.v1",
        normalized_parameters={"action": action, "payload": payload},
        node_revision=node_revision, canvas_id="default", node_id=node_id,
    )
    token = operations.claim_submission(operation["operation_id"], owner="test")
    assert token is not None
    operations.bind_task(operation["operation_id"], task_id=f"task-{action}", owner="test", fencing_token=token)
    bind_workflow_action_processing_operation(
        project_dir=ctx.state_dir, canvas_id="default", run_id=run["run_id"],
        node_id=node_id, action=action, operation_id=operation["operation_id"],
    )
    update_workflow_run(
        project_dir=ctx.state_dir, canvas_id="default", run_id=run["run_id"],
        action_updates=[{"node_id": node_id, "action": action, "status": "running",
                         "task_key": f"task:{action}"}],
    )
    operations.publish_result_unverified(operation["operation_id"], output_refs=[artifact_ref])
    reconcile_workflow_runs_with_tasks(
        project_dir=ctx.state_dir, canvas_id="default", artifact_project_dir=ctx.output_dir,
        tasks_by_key={f"task:{action}": {"task_id": f"task-{action}",
                                        "status": "completed", "result": {"artifact_ref": artifact_ref}}},
    )
    return run["run_id"]


def _saved_compile_run(tmp_path):
    from novelvideo.freezone.remix.production_plan_v2 import compile_production_plan_v2

    ctx, _plan_run_id, plan_ref, _ = _saved_plan_run(tmp_path)
    canvas = canvas_store.read_canvas(ctx.state_dir, "default")
    canvas["nodes"][0]["data"].update(productionPlanV2Ref={"artifactId": plan_ref["artifact_id"], "revision": 1},
                                      productionPlanV2Draft=RemixArtifactStore(ctx.output_dir, ctx.project_id).read(plan_ref["artifact_id"], 1)["payload"])
    compose = canvas["nodes"][1]
    payload = {"plan_ref": plan_ref}
    compose["data"].update(processingAction="compile_production_plan", processingPayload={},
                           remixNodeRevision=2, productionPlanV2Ref={"artifactId": plan_ref["artifact_id"], "revision": 1})
    compose["data"]["productionWorkflowSubmission"] = {
        "action": "compile_production_plan", "payload": payload, "requestPayload": {},
        "nodeData": '{"action":"compile_production_plan","payload":{},"revision":2,"remixRole":null,"productionPlanNodeId":"plan"}',
        "planRef": plan_ref, "planData": None, "analysisData": None,
    }
    canvas_store.atomic_write_json(canvas_store.canvas_path(ctx.state_dir, "default"), canvas)
    plan = RemixArtifactStore(ctx.output_dir, ctx.project_id).read(plan_ref["artifact_id"], 1)["payload"]
    manifest = compile_production_plan_v2(plan)
    manifest["source_versions"] = [plan_ref]
    artifact = RemixArtifactStore(ctx.output_dir, ctx.project_id).create(
        "composition_manifest.v2", manifest, [plan_ref],
    )
    result_ref = {"artifact_id": artifact["id"], "revision": artifact["revision"]}
    run_id = _completed_processing_run(
        ctx, node_id="compose", action="compile_production_plan", node_revision=2,
        payload=payload, artifact_ref=result_ref,
    )
    return ctx, run_id, plan_ref, result_ref


def test_completed_compile_publishes_manifest_only_to_its_bound_compose_node(tmp_path):
    from novelvideo.freezone.remix.production_processing_settlement import settle_production_processing

    ctx, run_id, plan_ref, manifest_ref = _saved_compile_run(tmp_path)
    result = settle_production_processing(ctx, canvas_id="default", run_id=run_id)

    compose = canvas_store.read_canvas(ctx.state_dir, "default")["nodes"][1]["data"]
    assert result["published"] == ["compose"]
    assert compose["remixManifestRef"] == {"artifactId": manifest_ref["artifact_id"], "revision": 1}
    assert compose["productionPlanV2Ref"] == {"artifactId": plan_ref["artifact_id"], "revision": 1}
    assert compose["remixInputsCurrent"] is True
    assert settle_production_processing(ctx, canvas_id="default", run_id=run_id)["published"] == []


def test_compile_does_not_replace_a_changed_plan_or_user_manifest(tmp_path):
    from novelvideo.freezone.remix.production_processing_settlement import settle_production_processing

    ctx, run_id, _plan_ref, _manifest_ref = _saved_compile_run(tmp_path)
    path = canvas_store.canvas_path(ctx.state_dir, "default")
    canvas = canvas_store.read_canvas(ctx.state_dir, "default")
    canvas["nodes"][1]["data"]["remixManifestDirty"] = True
    canvas_store.atomic_write_json(path, canvas)
    result = settle_production_processing(ctx, canvas_id="default", run_id=run_id)
    assert result["conflicts"] == ["compose"]
    assert canvas_store.read_canvas(ctx.state_dir, "default") == canvas


def _saved_speech_run(tmp_path, *, status="completed", omit_group_ref=False):
    state_dir, output_dir = tmp_path / "state", tmp_path / "output"
    ctx = SimpleNamespace(project_id="project", requester_user_id="user",
                          state_dir=state_dir, output_dir=output_dir)
    media_hash = "a" * 64
    plan = {
        "schema_version": 2, "goal": "dialogue",
        "output": {"width": 1920, "height": 1080, "fps_num": 24, "fps_den": 1},
        "beats": [{"id": "beat", "purpose": "dialogue", "duration_frames": 120,
                   "turns": [{"id": "first", "speaker": "甲", "text": "你好"},
                             {"id": "second", "speaker": "乙", "text": "再见"}]}],
        "resources": [{"id": "media", "node_id": "video", "media_kind": "video",
                       "media_path": "media/talk.mp4", "media_hash": media_hash,
                       "duration_us": 4_000_000, "has_audio": True}],
        "placements": [{"id": "visual", "kind": "visual", "role": "primary",
                        "track_id": "visual", "resource_id": "media",
                        "start": {"kind": "beat", "ref_id": "beat", "edge": "start"},
                        "end": {"kind": "beat", "ref_id": "beat", "edge": "end"}}],
        "speech_takes": [{"id": "take", "beat_id": "beat", "resource_id": "media",
                          "turn_ids": ["first", "second"], "origin_placement_id": "visual",
                          "source_in_us": 0, "source_out_us": 4_000_000,
                          "speed_num": 1, "speed_den": 1, "lead_in_frames": 0}],
    }
    store = RemixArtifactStore(output_dir, "project")
    saved = store.create("production_plan.v2", plan, [])
    plan_ref = {"artifact_id": saved["id"], "revision": saved["revision"]}
    normalized_plan = saved["payload"]
    payload = {"media_path": "media/talk.mp4", "turn_id": "first", "take_id": "take",
               "plan_ref": plan_ref}
    canvas = canvas_store.default_canvas_payload(project_id="project", actor_id="user")
    canvas["nodes"] = [{"id": "plan", "type": "scriptNode", "data": {
        "remixRole": "production_plan", "remixNodeRevision": 3,
        "processingAction": "align_production_speech",
        "processingPayload": {"media_path": "media/talk.mp4", "turn_id": "first", "take_id": "take"},
        "productionPlanV2Ref": {"artifactId": saved["id"], "revision": 1},
        "productionPlanV2Draft": normalized_plan,
        "productionWorkflowSubmission": {
            "action": "align_production_speech", "payload": payload,
            "requestPayload": {"media_path": "media/talk.mp4", "turn_id": "first", "take_id": "take"},
            "nodeData": json.dumps({"action": "align_production_speech",
                                    "payload": {"media_path": "media/talk.mp4", "turn_id": "first", "take_id": "take"},
                                    "revision": 3, "remixRole": "production_plan",
                                    "productionPlanNodeId": None}),
            "planRef": plan_ref, "planData": json.dumps(normalized_plan), "analysisData": None,
        },
    }}]
    path = canvas_store.canvas_path(state_dir, "default")
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas_store.atomic_write_json(path, canvas)

    def word_artifact(turn_id, text, start_us, *, group=False):
        return {"language": "zh", "status": status,
                "media_hash": "sha256:" + media_hash, "plan_ref": plan_ref,
                "turn_id": turn_id, "placement_id": "visual",
                "source_in_us": 0, "source_out_us": 4_000_000,
                "transcript": text if status == "completed" else None,
                "text_hash": hashlib.sha256(text.encode()).hexdigest(),
                "words": ([{"text": text, "start_char": 0, "end_char": len(text),
                            "start_us": start_us, "end_us": start_us + 300_000}]
                          if status == "completed" else []),
                **({"group_turn_ids": ["first", "second"]} if group else {})}

    alignment_store = RemixArtifactStore(output_dir, "project", producer_version="remix-align-production-speech.v1")
    sibling = alignment_store.create("word_timeline.v1", word_artifact("second", "再见", 600_000), [plan_ref])
    sibling_ref = {"artifact_id": sibling["id"], "revision": sibling["revision"]}
    primary = word_artifact("first", "你好", 100_000, group=True)
    if status == "completed" and not omit_group_ref:
        primary["group_refs"] = [{"turn_id": "second", **sibling_ref}]
    result = alignment_store.create("word_timeline.v1", primary,
                                    [plan_ref, sibling_ref] if not omit_group_ref else [plan_ref])
    result_ref = {"artifact_id": result["id"], "revision": result["revision"]}
    run_id = _completed_processing_run(
        ctx, node_id="plan", action="align_production_speech", node_revision=3,
        payload=payload, artifact_ref=result_ref,
    )
    return ctx, run_id, plan_ref, result_ref


def test_completed_speech_group_publishes_all_turns_to_a_new_plan_revision(tmp_path):
    from novelvideo.freezone.remix.production_processing_settlement import settle_production_processing

    ctx, run_id, plan_ref, speech_ref = _saved_speech_run(tmp_path)
    outcome = settle_production_processing(ctx, canvas_id="default", run_id=run_id)

    data = canvas_store.read_canvas(ctx.state_dir, "default")["nodes"][0]["data"]
    assert outcome["published"] == ["plan"]
    assert data["productionPlanV2Ref"] == {"artifactId": plan_ref["artifact_id"], "revision": 2}
    assert data["productionWorkflowResultRef"] == {"artifactId": speech_ref["artifact_id"], "revision": 1}
    timings = [turn["speech_timing"] for turn in data["productionPlanV2Draft"]["beats"][0]["turns"]]
    assert [timing["words"][0]["text"] for timing in timings] == ["你好", "再见"]
    assert settle_production_processing(ctx, canvas_id="default", run_id=run_id)["published"] == []
    assert RemixArtifactStore(ctx.output_dir, ctx.project_id).read(plan_ref["artifact_id"], 2)["schema"] == "production_plan.v2"


def test_incomplete_speech_group_is_retained_as_conflict(tmp_path):
    from novelvideo.freezone.remix.production_processing_settlement import settle_production_processing

    ctx, run_id, plan_ref, _ = _saved_speech_run(tmp_path, omit_group_ref=True)
    before = canvas_store.read_canvas(ctx.state_dir, "default")
    outcome = settle_production_processing(ctx, canvas_id="default", run_id=run_id)

    assert outcome["conflicts"] == ["plan"]
    assert canvas_store.read_canvas(ctx.state_dir, "default") == before
    assert RemixArtifactStore(ctx.output_dir, ctx.project_id).read(plan_ref["artifact_id"], 1)


def test_speech_mismatch_is_review_evidence_and_does_not_accept_timing(tmp_path):
    from novelvideo.freezone.remix.production_processing_settlement import settle_production_processing

    ctx, run_id, plan_ref, speech_ref = _saved_speech_run(tmp_path, status="script_mismatch")
    outcome = settle_production_processing(ctx, canvas_id="default", run_id=run_id)

    data = canvas_store.read_canvas(ctx.state_dir, "default")["nodes"][0]["data"]
    assert outcome["published"] == ["plan"]
    assert data["productionPlanV2Ref"] == {"artifactId": plan_ref["artifact_id"], "revision": 1}
    assert data["productionSpeechReviews"]["first"]["status"] == "script_mismatch"
    assert data["productionWorkflowResultRef"] == {"artifactId": speech_ref["artifact_id"], "revision": 1}
    assert all(turn.get("speech_timing") is None for turn in data["productionPlanV2Draft"]["beats"][0]["turns"])


def test_native_publication_wakes_observer_only_for_new_saved_result(tmp_path, monkeypatch):
    from novelvideo.chat import reference_production_observer
    from novelvideo.freezone.remix.production_processing_settlement import settle_production_processing
    ctx, run_id, ref, operation_id = _saved_plan_run(tmp_path)
    wakes = []
    def wake():
        canvas = canvas_store.read_canvas(ctx.state_dir, 'default')
        assert canvas['nodes'][0]['data']['productionPlanV2Ref'] == {'artifactId': ref['artifact_id'], 'revision': ref['revision']}
        assert RemixOperationStore(ctx.output_dir, ctx.project_id).get(operation_id)['status'] == 'completed'
        wakes.append(True)
    monkeypatch.setattr(reference_production_observer, 'wake_local_observers', wake)
    assert settle_production_processing(ctx, canvas_id='default', run_id=run_id)['published'] == ['plan']
    assert wakes == [True]
    assert not settle_production_processing(ctx, canvas_id='default', run_id=run_id)['published']
    assert wakes == [True]


def _saved_arrangement_run(tmp_path):
    from copy import deepcopy
    from novelvideo.freezone.remix.production_processing_settlement import settle_production_processing
    ctx, first_run, base_ref, _ = _saved_plan_run(tmp_path)
    settle_production_processing(ctx, canvas_id='default', run_id=first_run)
    store = RemixArtifactStore(ctx.output_dir, ctx.project_id)
    canvas = canvas_store.read_canvas(ctx.state_dir, 'default')
    node = canvas['nodes'][0]
    action = 'generate_production_plan_arrangement'
    payload = {'plan_ref': base_ref}
    node['data']['productionWorkflowSubmission'].update(action=action, payload=payload,
        requestPayload=payload, planRef=node['data']['productionPlanV2Ref'])
    canvas_store.atomic_write_json(canvas_store.canvas_path(ctx.state_dir, 'default'), canvas)
    run = create_workflow_run(project_dir=ctx.state_dir, project_id=ctx.project_id,
        canvas_id='default', actor_id='user', actions=[{'node_id': 'plan', 'action': action,
        'node_revision': 1, 'payload': payload}])
    operations = RemixOperationStore(ctx.output_dir, ctx.project_id)
    operation = operations.start(idempotency_key='arrange-submit', input_refs=[base_ref],
        producer_version='remix-process.v1', normalized_parameters={'action': action, 'payload': payload},
        node_revision=1, canvas_id='default', node_id='plan')
    token = operations.claim_submission(operation['operation_id'], owner='test')
    operations.bind_task(operation['operation_id'], task_id='arrangement-task', owner='test', fencing_token=token)
    bind_workflow_action_processing_operation(project_dir=ctx.state_dir, canvas_id='default',
        run_id=run['run_id'], node_id='plan', action=action, operation_id=operation['operation_id'])
    update_workflow_run(project_dir=ctx.state_dir, canvas_id='default', run_id=run['run_id'],
        action_updates=[{'node_id': 'plan', 'action': action, 'status': 'running', 'task_key': 'task:arrange'}])
    draft = deepcopy(store.read(base_ref['artifact_id'], 1)['payload'])
    draft['authoring_stage'] = 'arranged'
    artifact = store.revise(base_ref['artifact_id'], 1, draft, [])
    ref = {'artifact_id': artifact['id'], 'revision': artifact['revision']}
    operations.publish_result_unverified(operation['operation_id'], output_refs=[ref])
    reconcile_workflow_runs_with_tasks(project_dir=ctx.state_dir, canvas_id='default', artifact_project_dir=ctx.output_dir,
        tasks_by_key={'task:arrange': {'task_id': 'arrangement-task', 'status': 'completed', 'result': {'artifact_ref': ref}}})
    return ctx, run['run_id'], ref


def test_arrangement_publishes_next_revision_to_same_plan_and_compose(tmp_path):
    from novelvideo.freezone.remix.production_processing_settlement import settle_production_processing
    ctx, run_id, ref = _saved_arrangement_run(tmp_path)
    result = settle_production_processing(ctx, canvas_id='default', run_id=run_id)
    assert result['published'] == ['plan']
    canvas = canvas_store.read_canvas(ctx.state_dir, 'default')
    expected = {'artifactId': ref['artifact_id'], 'revision': 2}
    assert [n['data']['productionPlanV2Ref'] for n in canvas['nodes']] == [expected, expected]
    assert settle_production_processing(ctx, canvas_id='default', run_id=run_id)['published'] == []


def test_arrangement_delivery_preserves_new_local_edits(tmp_path):
    from novelvideo.freezone.remix.production_processing_settlement import settle_production_processing
    ctx, run_id, ref = _saved_arrangement_run(tmp_path)
    canvas = canvas_store.read_canvas(ctx.state_dir, 'default')
    canvas['nodes'][0]['data']['productionPlanV2Draft']['goal'] = 'new local edit'
    canvas_store.atomic_write_json(canvas_store.canvas_path(ctx.state_dir, 'default'), canvas)
    result = settle_production_processing(ctx, canvas_id='default', run_id=run_id)
    assert result['published'] == []
    assert result['conflicts'] == ['plan']
    saved = canvas_store.read_canvas(ctx.state_dir, 'default')['nodes'][0]['data']
    assert saved['productionPlanV2Draft']['goal'] == 'new local edit'
    assert saved['productionPlanV2Ref']['revision'] == 1
