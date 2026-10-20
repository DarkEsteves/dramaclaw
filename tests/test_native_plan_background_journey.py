"""A native plan submission remains usable after the browser leaves the page."""

from types import SimpleNamespace

from novelvideo.api.routes.chat import CanvasCommandToolResultIn, _validate_native_processing_receipt
from novelvideo.api.routes.freezone import _with_native_processing_task_ids
from novelvideo.chat.reference_production_observer import production_event
from novelvideo.freezone import canvas_store
from novelvideo.freezone.remix.production_processing_settlement import settle_native_task_delivery
from novelvideo.freezone.remix.store import RemixArtifactStore
from novelvideo.freezone.workflow_runs import read_workflow_run

from test_production_processing_settlement import _saved_plan_run


def test_confirmed_goal_edit_keeps_same_native_plan_slot_ready_before_first_run(tmp_path, monkeypatch):
    from novelvideo.freezone.remix import native_plan_stage as stage
    from novelvideo.freezone import workflow_runs

    ctx = SimpleNamespace(state_dir=tmp_path, project_id='p', requester_user_id='u')
    commission = {'canvas_id': 'c', 'requester_user_id': 'u', 'phase': 'drafting_plan',
        'commission_ref': {'artifact_id': 'commission', 'revision': 2},
        'analysis_node_id': 'analysis', 'direction_revision': 1, 'direction_fingerprint': 'direction'}
    binding = stage._binding(ctx, commission)
    draft = {'draft_id': 'draft', 'status': 'confirmed', 'run_after_create': False,
        'compiled': {'reference_binding': binding, 'plan': {'nodes': [{
            'id': 'production_plan_1', 'stage': 'production_plans',
            'data': {'remixNodeRevision': 1, 'processingPayload': {'topic': '旧目标'}},
        }]}}}
    canvas = {'nodes': [{'id': 'plan', 'type': 'scriptNode', 'data': {
        'workflowInstanceId': 'draft', 'workflowPlanNodeId': 'production_plan_1',
        'remixNodeRevision': 2, 'processingPayload': {'topic': '改写台词'},
    }}], 'edges': []}
    monkeypatch.setattr(stage, 'list_native_plan_drafts', lambda **_: [draft])
    monkeypatch.setattr(stage, 'read_native_plan_claim', lambda **_: draft)
    monkeypatch.setattr(stage, 'read_native_plan_slot_bindings', lambda **_: {})
    monkeypatch.setattr(workflow_runs, 'list_workflow_runs', lambda **_: [])

    result = stage.read_native_plan_stage(ctx=ctx, canvas=canvas, commission=commission)
    assert result['state'] == 'ready_to_submit'
    assert result['next_action']['node_id'] == 'plan'
    assert stage.validate_bound_plan_action(stage=result, node_id='plan',
        action='generate_production_plan_draft') is None


def test_confirmed_constraint_edit_keeps_same_native_plan_slot_ready_before_first_run(tmp_path, monkeypatch):
    from novelvideo.freezone.remix import native_plan_stage as stage
    from novelvideo.freezone import workflow_runs

    ctx = SimpleNamespace(state_dir=tmp_path, project_id='p', requester_user_id='u')
    commission = {'canvas_id': 'c', 'requester_user_id': 'u', 'phase': 'drafting_plan',
        'commission_ref': {'artifact_id': 'commission', 'revision': 2},
        'analysis_node_id': 'analysis', 'direction_revision': 1, 'direction_fingerprint': 'direction'}
    binding = stage._binding(ctx, commission)
    draft = {'draft_id': 'draft', 'status': 'confirmed', 'run_after_create': False,
        'compiled': {'reference_binding': binding, 'plan': {'nodes': [{
            'id': 'production_plan_1', 'stage': 'production_plans',
            'data': {'remixNodeRevision': 1, 'processingPayload': {'topic': '黑咖啡'}},
        }]}}}
    canvas = {'nodes': [{'id': 'plan', 'type': 'scriptNode', 'data': {
        'workflowInstanceId': 'draft', 'workflowPlanNodeId': 'production_plan_1',
        'remixNodeRevision': 2, 'remixDirectionEdited': True,
        'processingPayload': {'topic': '黑咖啡',
                              'requirements': {'primary_shots': {'min': 5}}},
    }}], 'edges': []}
    monkeypatch.setattr(stage, 'list_native_plan_drafts', lambda **_: [draft])
    monkeypatch.setattr(stage, 'read_native_plan_claim', lambda **_: draft)
    monkeypatch.setattr(stage, 'read_native_plan_slot_bindings', lambda **_: {})
    monkeypatch.setattr(workflow_runs, 'list_workflow_runs', lambda **_: [])

    result = stage.read_native_plan_stage(ctx=ctx, canvas=canvas, commission=commission)
    assert result['state'] == 'ready_to_submit'
    assert result['next_action']['node_id'] == 'plan'


def test_submitted_plan_finishes_in_original_node_and_becomes_reviewable(tmp_path):
    ctx, run_id, ref, operation_id = _saved_plan_run(
        tmp_path, reconcile=False, metadata={"canvas_bridge_key": "bridge"},
    )
    run = read_workflow_run(project_dir=ctx.state_dir, canvas_id="default", run_id=run_id)
    response = _with_native_processing_task_ids(run, output_dir=ctx.output_dir, project_id=ctx.project_id)
    action = response["actions"][0]
    receipt = CanvasCommandToolResultIn(
        bridge_key="bridge", project_id="project", canvas_id="default", canvas_apply_status="accepted",
        workflow_run_id=run_id, operation_id=operation_id, task_id=action["task_id"],
        node_id="plan", action="generate_production_plan_draft", completion_owner="host", applied=True,
    )
    command = {"type": "run_node_action", "node_id": "plan", "action": "generate_production_plan_draft"}
    assert _validate_native_processing_receipt(ctx, receipt, command)["task_id"] == "task-id"
    assert canvas_store.read_canvas(ctx.state_dir, "default")["nodes"][0]["data"].get("productionPlanV2Ref") is None

    class Task:
        task_id = "task-id"
        status = "completed"
        result = {"artifact_ref": ref}
        error = None

    class Manager:
        def get_task_for_project(self, *_args, **_kwargs):
            return Task()

        def _task_key_for_state(self, _task):
            return "task:plan"

    delivery = settle_native_task_delivery(
        ctx, Manager(), payload={"workflow_run_id": run_id, "operation_id": operation_id},
        task_type="remix_generate_production_plan_draft", expected_task_id="task-id",
        episode=0, scope=operation_id,
    )
    assert delivery["published"] == ["plan"]
    canvas = canvas_store.read_canvas(ctx.state_dir, "default")
    canvas["nodes"].append({"id": "analysis", "type": "videoStoryNode", "data": {}})
    canvas["edges"].append({"source": "analysis", "target": "plan"})
    event = production_event(
        RemixArtifactStore(ctx.output_dir, ctx.project_id), canvas,
        {"canvas_id": "default", "analysis_node_id": "analysis", "phase": "drafting_plan",
         "commission_ref": {"artifact_id": "commission", "revision": 1}, "direction_fingerprint": ""},
        workflow_state_dir=ctx.state_dir,
    )
    assert event["event"]["type"] == "plan_ready"
    assert event["event"]["plan_ref"]["artifact_id"] == ref["artifact_id"]
