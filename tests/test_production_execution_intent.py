"""Persisted production intent survives edits and receipt recovery."""

from types import SimpleNamespace

import pytest

from novelvideo.freezone.agent_workflows.graph import build_workflow_graph_commands
from novelvideo.freezone.agent_workflows.reference_production import (
    compile_reference_production_plan, native_entry_intent,
)
from novelvideo.freezone.remix.production_node_entry import ProductionNodesRequest
from novelvideo.freezone.remix.native_plan_stage import read_native_plan_stage
from novelvideo.freezone.workflow_drafts import (
    create_workflow_draft, patch_workflow_draft, read_workflow_draft,
)


@pytest.mark.parametrize("run", [True, False])
@pytest.mark.parametrize("count", [1, 3])
@pytest.mark.parametrize("scope", ["仅作品方案", "完整视频"])
def test_persisted_native_execution_matrix_after_patch(tmp_path, run, count, scope):
    binding = dict(project_id="p", canvas_id="c", requester_user_id="u",
                   commission_id="commission", analysis_node_id="analysis",
                   direction_revision=1, direction_fingerprint="confirmed",
                   stage="production_plans")
    commission = {**binding, "production_scope": scope, "phase": "drafting_plan",
                  "commission_ref": {"artifact_id": "commission", "revision": 1}}
    request = ProductionNodesRequest(commission_ref=commission["commission_ref"],
                                    goal="保持叙事目标和对白", run_after_create=run, plan_count=count)
    intent = native_entry_intent(request, commission)
    compiled = compile_reference_production_plan(
        intent, {"id": "reference-video-remix", "version": "2.0"},
        intent["inputs"], request.goal,
    )
    compiled.update(skill_id="reference-video-remix", reference_binding=binding)
    draft = create_workflow_draft(project_dir=tmp_path, project_id="p", canvas_id="c",
                                  intent=intent, compiled=compiled, run_after_create=run)
    patched, error = patch_workflow_draft(project_dir=tmp_path, canvas_id="c",
        draft_id=draft["draft_id"], expected_revision=1, intent=intent, compiled=compiled,
        last_changes={"title": "clarified"})
    assert error is None and patched["revision"] == 2
    saved, error = read_workflow_draft(project_dir=tmp_path, canvas_id="c", draft_id=draft["draft_id"])
    assert error is None and saved["run_after_create"] is run
    built = build_workflow_graph_commands({"plan": saved["compiled"]["plan"],
        "run_after_create": saved["run_after_create"], "workflow_instance_id": saved["draft_id"]})
    assert built["ok"], built
    runs = [command for command in built["commands"] if command["type"] == "run_workflow"]
    assert len(runs) == int(run)
    if run:
        assert runs[0]["node_ids"] == [f"production_plan_{index}" for index in range(1, count + 1)]
        assert runs[0]["direction"] == "node"
    for command in built["commands"]:
        if command["type"] == "create_node" and command["node_type"] == "scriptNode":
            assert command["data"]["processingAction"] == "generate_production_plan_draft"
            assert command["data"]["processingPayload"]["topic"] == request.goal
    stage = read_native_plan_stage(
        ctx=SimpleNamespace(state_dir=tmp_path, project_id="p", requester_user_id="u"),
        canvas={"nodes": [], "edges": []}, commission=commission)
    assert stage["run_after_create"] is run
    assert stage["draft_id"] == saved["draft_id"]


@pytest.mark.parametrize("status", ["running", "failed"])
def test_lost_submission_response_recovers_existing_run(tmp_path, status):
    from novelvideo.freezone.workflow_runs import (
        create_workflow_run, list_workflow_runs, update_workflow_run,
    )

    submission = dict(project_dir=tmp_path, project_id="p", canvas_id="c", actor_id="u",
        idempotency_key="approved-bridge-plan", actions=[{
            "node_id": "original-plan", "action": "generate_production_plan_draft",
            "node_revision": 1, "payload": {"topic": "approved goal"}}])
    accepted = create_workflow_run(**submission)
    update_workflow_run(project_dir=tmp_path, canvas_id="c", run_id=accepted["run_id"],
        action_updates=[{"node_id": "original-plan", "action": "generate_production_plan_draft",
                         "status": status}], status=status)
    recovered = create_workflow_run(**submission)
    assert recovered["run_id"] == accepted["run_id"]
    assert recovered["status"] == status
    assert recovered["actions"][0]["node_id"] == "original-plan"
    assert len(list_workflow_runs(project_dir=tmp_path, canvas_id="c")) == 1
