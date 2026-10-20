from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
from types import SimpleNamespace
import pytest

# The plugin helper below installs a registry stub; reuse its cleanup fixture
# when calling it outside the module that defines the helper.
from test_freezone_plugin import _restore_tools_registry_modules  # noqa: F401

from novelvideo.freezone.workflow_drafts import (
    bind_workflow_draft_task,
    claim_workflow_draft_confirmation,
    create_workflow_draft,
    finish_workflow_draft_confirmation,
    workflow_drafts_db_path,
    cancel_workflow_draft,
)
from novelvideo.freezone.remix.native_plan_stage import read_native_plan_stage, validate_bound_plan_action


def _binding(commission="commission-1", direction=1):
    return {
        "project_id": "p", "canvas_id": "c", "requester_user_id": "u",
        "commission_id": commission, "analysis_node_id": "analysis",
        "direction_revision": direction, "direction_fingerprint": "fingerprint",
        "stage": "production_plans",
    }


def _compiled(binding, count=1):
    nodes = []
    for index in range(1, count + 1):
        nodes.extend([
            {"id": f"production_plan_{index}", "node_type": "scriptNode", "stage": "production_plans",
             "data": {"remixRole": "production_plan", "processingAction": "generate_production_plan_draft"}},
            {"id": f"composition_{index}", "node_type": "videoComposeNode", "stage": "composition", "data": {}},
        ])
    return {"ok": True, "skill_id": "reference-video-remix", "reference_binding": binding,
            "plan": {"nodes": nodes, "phases": ["production_plans", "composition"]}}


def _create(project_dir, binding, count=1):
    return create_workflow_draft(project_dir=project_dir, project_id="p", canvas_id="c",
                                 intent={"reference_commission_id": binding["commission_id"]},
                                 compiled=_compiled(binding, count))


def _confirm(project_dir, draft):
    claimed, error = claim_workflow_draft_confirmation(
        project_dir=project_dir, canvas_id="c", draft_id=draft["draft_id"], revision=1)
    assert error is None
    bind_workflow_draft_task(project_dir=project_dir, canvas_id="c", draft_id=draft["draft_id"],
                             task_id="task-1", root_task_id="task-1")
    finish_workflow_draft_confirmation(project_dir=project_dir, canvas_id="c", draft_id=draft["draft_id"],
                                       outcome="confirmed", expected_task_id="task-1")


def test_concurrent_prepare_claims_one_draft_and_cancel_releases_it(tmp_path):
    binding = _binding()
    with ThreadPoolExecutor(max_workers=2) as pool:
        drafts = list(pool.map(lambda _: _create(tmp_path, binding), range(2)))
    assert drafts[0]["draft_id"] == drafts[1]["draft_id"]
    cancelled, error = cancel_workflow_draft(project_dir=tmp_path, canvas_id="c",
        draft_id=drafts[0]["draft_id"], expected_revision=1)
    assert error is None and cancelled["status"] == "cancelled"
    assert _create(tmp_path, binding)["draft_id"] != drafts[0]["draft_id"]


def test_preclaim_legacy_draft_is_reused_instead_of_duplicated(tmp_path):
    binding = _binding()
    first = _create(tmp_path, binding)
    with sqlite3.connect(workflow_drafts_db_path(tmp_path)) as conn:
        conn.execute("DELETE FROM native_plan_claims")
    assert _create(tmp_path, binding)["draft_id"] == first["draft_id"]


def test_pending_canvas_delivery_prevents_second_plan_draft_turn(tmp_path):
    from novelvideo.chat.reference_production_observer import _plan_generation_awaiting_canvas_result

    binding = _binding()
    draft = _create(tmp_path, binding)
    claimed, error = claim_workflow_draft_confirmation(
        project_dir=tmp_path, canvas_id="c", draft_id=draft["draft_id"], revision=1,
    )
    assert error is None and claimed["status"] == "confirming"
    commission = {**binding, "commission_ref": {"artifact_id": binding["commission_id"]},
                  "phase": "drafting_plan"}
    assert _plan_generation_awaiting_canvas_result(
        {"project_id": "p", "nodes": [], "edges": []}, commission, tmp_path,
    ) is True


def test_ready_draft_requests_existing_canvas_approval_instead_of_second_card(tmp_path):
    binding = _binding()
    draft = _create(tmp_path, binding)
    ctx = SimpleNamespace(state_dir=tmp_path, project_id="p", requester_user_id="u")
    commission = {**binding, "commission_ref": {"artifact_id": binding["commission_id"]},
                  "phase": "drafting_plan"}
    stage = read_native_plan_stage(
        ctx=ctx, canvas={"project_id": "p", "nodes": [], "edges": []},
        commission=commission,
    )
    assert stage["draft_id"] == draft["draft_id"]
    assert stage["next_action"] == {
        "owner": "agent", "kind": "submit_canvas_approval",
        "tool": "freezone_confirm_workflow_draft",
        "draft_id": draft["draft_id"], "revision": draft["revision"],
    }


def test_failed_confirmation_task_without_graph_is_visible_without_recreating(tmp_path, monkeypatch):
    from novelvideo.chat.reference_production_observer import _plan_generation_awaiting_canvas_result
    from novelvideo.freezone.remix import native_plan_stage

    binding = _binding()
    draft = _create(tmp_path, binding)
    claimed, error = claim_workflow_draft_confirmation(
        project_dir=tmp_path, canvas_id="c", draft_id=draft["draft_id"],
        revision=1, now=1_000,
    )
    assert error is None and claimed is not None
    bind_workflow_draft_task(project_dir=tmp_path, canvas_id="c", draft_id=draft["draft_id"],
                             task_id="task-1", root_task_id="task-1")
    finish_workflow_draft_confirmation(project_dir=tmp_path, canvas_id="c",
        draft_id=draft["draft_id"], outcome="submitted", expected_task_id="task-1")
    monkeypatch.setattr(native_plan_stage, "get_task_manager", lambda: SimpleNamespace(
        get_task_for_project=lambda *_args, **_kwargs: SimpleNamespace(
            task_id="task-1", status="failed")), raising=False)
    ctx = SimpleNamespace(state_dir=tmp_path, project_id="p", requester_user_id="u")
    commission = {**binding, "commission_ref": {"artifact_id": binding["commission_id"]},
                  "phase": "drafting_plan"}
    canvas = {"project_id": "p", "nodes": [], "edges": []}
    stage = read_native_plan_stage(ctx=ctx, canvas=canvas, commission=commission)
    assert stage["state"] == "delivery_unverified"
    assert stage["next_action"]["kind"] == "inspect_delivery"
    assert _plan_generation_awaiting_canvas_result(canvas, commission, tmp_path) is False

    from novelvideo.chat.reference_production_observer import _native_delivery_failure_observation
    observation = _native_delivery_failure_observation(ctx, canvas, commission)
    assert observation["native_failure"] is True
    assert observation["event_key"].startswith("production:")
    assert "核对" in observation["notice"]


def test_claim_does_not_reuse_draft_with_changed_direction_fingerprint(tmp_path):
    _create(tmp_path, _binding())
    with pytest.raises(ValueError, match="binding_conflict"):
        _create(tmp_path, {**_binding(), "direction_fingerprint": "different"})
    ctx = SimpleNamespace(state_dir=tmp_path, project_id="p", requester_user_id="u")
    commission = {**_binding(), "direction_fingerprint": "different",
                  "commission_ref": {"artifact_id": "commission-1"}, "phase": "drafting_plan"}
    stage = read_native_plan_stage(ctx=ctx, canvas={"nodes": []}, commission=commission)
    assert stage["state"] == "binding_conflict"


def test_authoritative_canvas_receipt_persists_exact_slot_node_mapping(tmp_path):
    from novelvideo.freezone import workflow_drafts
    draft = _create(tmp_path, _binding(), 2)
    _confirm(tmp_path, draft)
    canvas = {"project_id": "p", "canvas_id": "c", "revision": 3, "nodes": [
        {"id": "plan-2", "data": {"workflowInstanceId": draft["draft_id"], "workflowPlanNodeId": "production_plan_2"}},
        {"id": "compose-1", "data": {"workflowInstanceId": draft["draft_id"], "workflowPlanNodeId": "composition_1"}},
        {"id": "plan-1", "data": {"workflowInstanceId": draft["draft_id"], "workflowPlanNodeId": "production_plan_1"}},
        {"id": "compose-2", "data": {"workflowInstanceId": draft["draft_id"], "workflowPlanNodeId": "composition_2"}},
    ]}
    workflow_drafts.record_native_plan_canvas_binding(project_dir=tmp_path, project_id="p", canvas_id="c",
        requester_user_id="u", draft_id=draft["draft_id"], task_id="task-1", canvas=canvas)
    assert workflow_drafts.read_native_plan_slot_bindings(project_dir=tmp_path, draft_id=draft["draft_id"]) == {
        "production_plan_1": {"plan_node_id": "plan-1", "compose_node_id": "compose-1"},
        "production_plan_2": {"plan_node_id": "plan-2", "compose_node_id": "compose-2"},
    }
    canvas["nodes"] = [{"id": "replacement", "data": {"workflowInstanceId": draft["draft_id"],
                         "workflowPlanNodeId": "production_plan_1"}},
                       *(node for node in canvas["nodes"] if node["id"] != "plan-1")]
    ctx = SimpleNamespace(state_dir=tmp_path, project_id="p", requester_user_id="u")
    commission = {**_binding(), "commission_ref": {"artifact_id": "commission-1"}, "phase": "drafting_plan"}
    stage = read_native_plan_stage(ctx=ctx, canvas=canvas, commission=commission)
    assert stage["slots"][0]["state"] == "binding_conflict"


def test_confirmed_empty_plan_uses_exact_bound_node_with_two_slots(tmp_path):
    from novelvideo.freezone.workflow_runs import (
        create_workflow_run, bind_workflow_action_processing_operation, update_workflow_run,
    )
    from novelvideo.freezone.remix.operations import RemixOperationStore
    binding = _binding()
    draft = _create(tmp_path, binding, 2)
    _confirm(tmp_path, draft)
    nodes = [
        {"id": "unrelated", "data": {"remixRole": "production_plan"}},
        {"id": "compose-2", "data": {"workflowInstanceId": draft["draft_id"], "workflowPlanNodeId": "composition_2"}},
        {"id": "original-plan", "data": {"workflowInstanceId": draft["draft_id"], "workflowPlanNodeId": "production_plan_1", "remixRole": "production_plan"}},
        {"id": "second-plan", "data": {"workflowInstanceId": draft["draft_id"], "workflowPlanNodeId": "production_plan_2", "remixRole": "production_plan"}},
        {"id": "compose-1", "data": {"workflowInstanceId": draft["draft_id"], "workflowPlanNodeId": "composition_1"}},
    ]
    ctx = SimpleNamespace(state_dir=tmp_path, output_dir=tmp_path, project_id="p", requester_user_id="u")
    commission = {**binding, "commission_ref": {"artifact_id": binding["commission_id"]}, "phase": "drafting_plan"}
    stage = read_native_plan_stage(ctx=ctx, canvas={"nodes": nodes}, commission=commission)
    assert [slot["plan_node_id"] for slot in stage["slots"]] == ["original-plan", "second-plan"]
    assert stage["slots"][0]["next_action"] == {
        "owner": "agent", "kind": "run_node_action", "tool": "freezone_run_node_action",
        "node_id": "original-plan", "action": "generate_production_plan_draft",
    }
    assert stage["slots"][0]["compose_node_id"] == "compose-1"
    run = create_workflow_run(project_dir=tmp_path, project_id="p", canvas_id="c",
        actor_id="u", actions=[{"node_id": "original-plan", "action": "generate_production_plan_draft",
                               "payload": {"topic": "new"}, "node_revision": 1}])
    operations = RemixOperationStore(tmp_path, "p")
    operation = operations.start(idempotency_key="stage-plan", input_refs=[], producer_version="test",
                                 normalized_parameters={"action": "generate_production_plan_draft"},
                                 node_revision=1, canvas_id="c", node_id="original-plan")
    token = operations.claim_submission(operation["operation_id"], owner="test")
    operations.bind_task(operation["operation_id"], task_id="real-task-id", owner="test", fencing_token=token)
    bind_workflow_action_processing_operation(project_dir=tmp_path, canvas_id="c", run_id=run["run_id"],
        node_id="original-plan", action="generate_production_plan_draft", operation_id=operation["operation_id"])
    update_workflow_run(project_dir=tmp_path, canvas_id="c", run_id=run["run_id"], action_updates=[
        {"node_id": "original-plan", "action": "generate_production_plan_draft", "status": "running",
         "task_key": "task-key-is-not-the-id"}])
    running = read_native_plan_stage(ctx=ctx, canvas={"nodes": nodes}, commission=commission)
    assert running["slots"][0]["run_id"] == run["run_id"]
    assert running["slots"][0]["task_id"] == "real-task-id"
    assert running["slots"][0]["next_action"]["kind"] == "await_completion_event"


@pytest.mark.asyncio
async def test_repeated_confirm_returns_original_native_receipt_without_requeue(tmp_path, monkeypatch):
    from novelvideo.api.routes import freezone

    binding = _binding()
    draft = _create(tmp_path, binding)
    _confirm(tmp_path, draft)
    async def resolve(*args):
        return SimpleNamespace(project_id="p", requester_user_id="u", state_dir=tmp_path), "u", "p", tmp_path, str(tmp_path)
    monkeypatch.setattr(freezone, "_resolve_freezone_project", resolve)
    result = await freezone.claim_canvas_workflow_draft("p", "c", draft["draft_id"],
        {"revision": 1}, {"user_id": "u"})
    assert result["ok"] is True
    assert result["data"]["draft_id"] == draft["draft_id"]
    assert result["data"]["task_id"] == "task-1"


def test_agent_plan_action_rejects_other_selected_plan_without_submitting(monkeypatch):
    from test_freezone_plugin import _load_plugin_module

    plugin = _load_plugin_module()
    monkeypatch.setattr(plugin, "_reference_analysis_current_state", lambda *args: {
        "ok": True, "data": {"branches": [{"phase": "drafting_plan",
            "native_plan_stage": {"state": "ready_to_submit", "slots": [{
                "slot_id": "production_plan_1", "plan_node_id": "original-plan",
                "state": "ready_to_submit"}]}}]}})
    submissions = []
    monkeypatch.setattr(plugin, "_single_write_command",
        lambda args, command: submissions.append(command) or {"ok": True})
    result = plugin._handle_run_node_action({"project_id": "p", "canvas_id": "c",
        "node_id": "selected-other-plan", "action": "generate_production_plan_draft"})
    assert result["code"] == "bound_plan_node_required"
    assert result["node_id"] == "original-plan"
    assert submissions == []


def test_agent_plan_action_rejects_stale_bound_node_version(monkeypatch):
    from test_freezone_plugin import _load_plugin_module

    plugin = _load_plugin_module()
    monkeypatch.setattr(plugin, "_reference_analysis_current_state", lambda *args: {
        "ok": True, "data": {"branches": [{"phase": "drafting_plan",
            "native_plan_stage": {"state": "resolve_conflict", "slots": [{
                "plan_node_id": "original-plan", "state": "resolve_conflict"}]}}]}})
    monkeypatch.setattr(plugin, "_single_write_command",
        lambda *args: pytest.fail("stale node must not be submitted"))
    result = plugin._handle_run_node_action({"project_id": "p", "canvas_id": "c",
        "node_id": "original-plan", "action": "generate_production_plan_draft"})
    assert result["code"] == "resolve_conflict"


def test_multiple_bound_slots_require_exact_slot_selection():
    result = validate_bound_plan_action(stage={"slots": [
        {"plan_node_id": "plan-a"}, {"plan_node_id": "plan-b"}]},
        node_id="other", action="generate_production_plan_draft")
    assert result == {"code": "binding_conflict", "candidate_node_ids": ["plan-a", "plan-b"]}


def test_changed_plan_node_revision_requires_conflict_resolution(tmp_path):
    binding = _binding()
    compiled = _compiled(binding)
    compiled["plan"]["nodes"][0]["data"]["remixNodeRevision"] = 1
    draft = create_workflow_draft(project_dir=tmp_path, project_id="p", canvas_id="c",
        intent={"reference_commission_id": binding["commission_id"]}, compiled=compiled)
    _confirm(tmp_path, draft)
    ctx = SimpleNamespace(state_dir=tmp_path, project_id="p", requester_user_id="u")
    commission = {**binding, "commission_ref": {"artifact_id": "commission-1"}, "phase": "drafting_plan"}
    canvas = {"nodes": [{"id": "plan-a", "data": {"workflowInstanceId": draft["draft_id"],
                      "workflowPlanNodeId": "production_plan_1", "remixNodeRevision": 2}},
                      {"id": "compose-a", "data": {"workflowInstanceId": draft["draft_id"],
                      "workflowPlanNodeId": "composition_1"}}]}
    from novelvideo.freezone.workflow_drafts import record_native_plan_canvas_binding
    with pytest.raises(ValueError, match="revision"):
        record_native_plan_canvas_binding(project_dir=tmp_path, project_id="p", canvas_id="c",
            requester_user_id="u", draft_id=draft["draft_id"], task_id="task-1",
            canvas={**canvas, "project_id": "p", "canvas_id": "c", "revision": 1})
    stage = read_native_plan_stage(ctx=ctx, canvas=canvas, commission=commission)
    assert stage["slots"][0]["state"] == "resolve_conflict"
    assert stage["slots"][0]["next_action"]["kind"] == "resolve_conflict"
    assert validate_bound_plan_action(stage=stage, node_id="plan-a",
        action="generate_production_plan_draft")["code"] == "resolve_conflict"


@pytest.mark.asyncio
async def test_authenticated_agent_wrong_bound_plan_is_rejected_before_run_creation(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.remix import reference_director
    from novelvideo.freezone.workflow_runs import list_workflow_runs

    binding = _binding()
    draft = _create(tmp_path, binding)
    _confirm(tmp_path, draft)
    canvas = {"nodes": [
        {"id": "plan-a", "data": {"workflowInstanceId": draft["draft_id"],
                                    "workflowPlanNodeId": "production_plan_1"}},
        {"id": "plan-b", "data": {"remixRole": "production_plan"}},
    ]}
    commission = {**binding, "commission_ref": {"artifact_id": binding["commission_id"]},
                  "phase": "drafting_plan"}
    ctx = SimpleNamespace(project_id="p", requester_user_id="u", state_dir=tmp_path,
                          output_dir=tmp_path, effective_role="editor")

    async def resolve(*_args):
        return ctx, "u", "p", tmp_path, str(tmp_path)

    monkeypatch.setattr(freezone, "_resolve_freezone_project", resolve)
    monkeypatch.setattr(freezone.canvas_store, "read_canvas", lambda *_args: canvas)
    monkeypatch.setattr(reference_director, "list_director_artifacts",
                        lambda *_args, **_kwargs: {"commissions": [commission]})
    monkeypatch.setattr(freezone, "get_task_backend",
                        lambda: pytest.fail("wrong plan must not enqueue"))
    body = {"actions": [{"node_id": "plan-b", "action": "generate_production_plan_draft",
                         "node_revision": 1, "payload": {"topic": "other"}}],
            "reference_commission_id": "forged-other-commission"}

    with pytest.raises(HTTPException) as error:
        await freezone.create_canvas_workflow_run("p", "c", body,
            {"id": "u", "credential_kind": "agent_session", "current_scope_kind": "project",
             "current_project_id": "p"})
    assert error.value.status_code == 409
    assert error.value.detail == {"code": "bound_plan_node_required", "node_id": "plan-a",
                                  "candidate_node_ids": ["plan-a"]}
    assert list_workflow_runs(project_dir=tmp_path, canvas_id="c") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("credential_kind,node_id", [
    ("agent_session", "plan-a"),
    ("browser_session", "plan-b"),
])
async def test_bound_plan_gate_allows_exact_agent_node_and_independent_manual_action(
    tmp_path, monkeypatch, credential_kind, node_id,
):
    from novelvideo.api.routes import freezone, remix
    from novelvideo.freezone.remix import reference_director

    binding = _binding()
    draft = _create(tmp_path, binding)
    _confirm(tmp_path, draft)
    canvas = {"nodes": [
        {"id": "plan-a", "data": {"workflowInstanceId": draft["draft_id"],
                                    "workflowPlanNodeId": "production_plan_1"}},
        {"id": "plan-b", "data": {"remixRole": "production_plan"}},
    ]}
    ctx = SimpleNamespace(project_id="p", requester_user_id="u", state_dir=tmp_path,
                          output_dir=tmp_path, effective_role="editor")

    async def resolve(*_args):
        return ctx, "u", "p", tmp_path, str(tmp_path)

    async def no_dispatch(*, run, **_kwargs):
        return run

    monkeypatch.setattr(freezone, "_resolve_freezone_project", resolve)
    monkeypatch.setattr(freezone.canvas_store, "read_canvas", lambda *_args: canvas)
    monkeypatch.setattr(reference_director, "list_director_artifacts",
        lambda *_args, **_kwargs: {"commissions": [{**binding,
            "commission_ref": {"artifact_id": binding["commission_id"]},
            "phase": "drafting_plan"}]})
    monkeypatch.setattr(remix, "prepare_registered_action", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(freezone, "get_usage_meter", lambda: freezone.NoOpUsageMeter())
    monkeypatch.setattr(freezone, "_dispatch_ready_remix_workflow_actions", no_dispatch)

    result = await freezone.create_canvas_workflow_run("p", "c",
        {"actions": [{"node_id": node_id, "action": "generate_production_plan_draft",
                      "node_revision": 1, "payload": {"topic": "other"}}]},
        {"id": "u", "credential_kind": credential_kind})
    assert result["data"]["actions"][0]["node_id"] == node_id


@pytest.mark.asyncio
@pytest.mark.parametrize("case,expected_detail", [
    ("wrong_bound", {"code": "bound_plan_node_required", "node_id": "plan-a",
                     "candidate_node_ids": ["plan-a"]}),
    ("wrong_actor", {"code": "canvas_bridge_scope_mismatch"}),
    ("wrong_project", {"code": "canvas_bridge_scope_mismatch"}),
    ("wrong_canvas", {"code": "canvas_bridge_scope_mismatch"}),
    ("wrong_command", {"code": "canvas_bridge_action_mismatch"}),
    ("wrong_workflow_target", {"code": "canvas_bridge_action_mismatch"}),
    ("unbound_workflow_scope", {"code": "canvas_bridge_action_mismatch"}),
    ("wrong_owner", {"code": "canvas_bridge_owner_mismatch"}),
])
async def test_browser_bridge_wrong_bound_plan_is_rejected_before_run_creation(
    tmp_path, monkeypatch, case, expected_detail,
):
    from fastapi import HTTPException
    from novelvideo.api.routes import chat, freezone
    from novelvideo.freezone import canvas_command_bridge
    from novelvideo.freezone.remix import reference_director
    from novelvideo.freezone.workflow_runs import list_workflow_runs

    binding = _binding()
    draft = _create(tmp_path, binding)
    _confirm(tmp_path, draft)
    canvas = {"nodes": [
        {"id": "plan-a", "data": {"workflowInstanceId": draft["draft_id"],
                                    "workflowPlanNodeId": "production_plan_1"}},
        {"id": "plan-b", "data": {"remixRole": "production_plan"}},
    ]}
    commission = {**binding, "commission_ref": {"artifact_id": binding["commission_id"]},
                  "phase": "drafting_plan"}
    ctx = SimpleNamespace(project_id="p", requester_user_id="u", state_dir=tmp_path,
                          output_dir=tmp_path, effective_role="editor")
    bridge_dir = tmp_path / "bridge"
    producer = {"username": "other" if case == "wrong_actor" else "alice",
                "project_id": "other" if case == "wrong_project" else "p",
                "canvas_id": "other" if case == "wrong_canvas" else "c",
                "agent_id": "main", "turn_id": "turn-1"}
    for name, value in producer.items():
        monkeypatch.setenv("DRAMACLAW_" + name.upper(), value)
    command = (
        {"type": "run_workflow", "node_ids": ["other"]}
        if case == "wrong_workflow_target" else
        {"type": "run_workflow", "scope": "selection"}
        if case == "unbound_workflow_scope" else
        {"type": "run_node_action",
         "node_id": "plan-a" if case == "wrong_command" else "plan-b",
         "action": "generate_production_plan_draft"}
    )
    canvas_command_bridge.put_pending_canvas_command(
        key="agent-plan-command", project_id=producer["project_id"],
        canvas_id=producer["canvas_id"],
        commands=[command], envelope={"commands": [command]}, bridge_dir=bridge_dir,
    )
    assert canvas_command_bridge.mark_bridge_message_delivered(
        "agent-plan-command", consumer_id="browser-1", binding=producer,
        execution_token="owner-token", bridge_dir=bridge_dir,
    )

    async def resolve(*_args):
        return ctx, "alice", "p", tmp_path, str(tmp_path)

    monkeypatch.setattr(freezone, "_resolve_freezone_project", resolve)
    monkeypatch.setattr(freezone.canvas_store, "read_canvas", lambda *_args: canvas)
    monkeypatch.setattr(chat, "_canvas_bridge_dir", lambda *_args, **_kwargs: bridge_dir)
    monkeypatch.setattr(reference_director, "list_director_artifacts",
                        lambda *_args, **_kwargs: {"commissions": [commission]})
    monkeypatch.setattr(freezone, "get_task_backend",
                        lambda: pytest.fail("wrong bridged plan must not enqueue"))

    with pytest.raises(HTTPException) as error:
        await freezone.create_canvas_workflow_run("p", "c", {
            "bridge_key": "agent-plan-command.executor.stale-token" if case == "wrong_owner"
                          else "agent-plan-command.executor.owner-token",
            "actions": [{"node_id": "plan-a" if case in {
                "wrong_workflow_target", "unbound_workflow_scope",
            } else "plan-b", "action": "generate_production_plan_draft",
                         "node_revision": 1, "payload": {"topic": "other"}}],
        }, {"id": "u", "username": "alice", "credential_kind": "browser_session"})
    assert error.value.status_code == 409
    assert error.value.detail == expected_detail
    assert list_workflow_runs(project_dir=tmp_path, canvas_id="c") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("has_continuation,expected_detail", [
    (True, {"code": "bound_plan_node_required", "node_id": "plan-a",
            "candidate_node_ids": ["plan-a"]}),
    (False, {"code": "binding_conflict", "candidate_node_ids": ["plan-a", "plan-b"]}),
])
async def test_browser_bridge_selects_producer_continuation_not_another_commission(
    tmp_path, monkeypatch, has_continuation, expected_detail,
):
    from fastapi import HTTPException
    from novelvideo.api.routes import chat, freezone
    from novelvideo.chat import reference_remix_continuation
    from novelvideo.freezone import canvas_command_bridge
    from novelvideo.freezone.remix import reference_director
    from novelvideo.freezone.workflow_runs import list_workflow_runs

    drafts = {}
    commissions = []
    for suffix in ("a", "b"):
        binding = _binding(f"commission-{suffix}")
        drafts[suffix] = _create(tmp_path, binding)
        _confirm(tmp_path, drafts[suffix])
        commissions.append({**binding, "commission_ref": {"artifact_id": binding["commission_id"]},
                            "phase": "drafting_plan"})
    canvas = {"nodes": [
        {"id": f"plan-{suffix}", "data": {
            "workflowInstanceId": drafts[suffix]["draft_id"],
            "workflowPlanNodeId": "production_plan_1",
        }} for suffix in ("a", "b")
    ]}
    ctx = SimpleNamespace(project_id="p", requester_user_id="u", state_dir=tmp_path,
                          output_dir=tmp_path, effective_role="editor")
    bridge_dir = tmp_path / "bridge"
    producer = {"username": "alice", "project_id": "p", "canvas_id": "c",
                "agent_id": "main", "turn_id": "turn-a"}
    for name, value in producer.items():
        monkeypatch.setenv("DRAMACLAW_" + name.upper(), value)
    command = {"type": "run_node_action", "node_id": "plan-b",
               "action": "generate_production_plan_draft"}
    canvas_command_bridge.put_pending_canvas_command(
        key="agent-plan-command", project_id="p", canvas_id="c",
        commands=[command], envelope={"commands": [command]}, bridge_dir=bridge_dir,
    )
    assert canvas_command_bridge.mark_bridge_message_delivered(
        "agent-plan-command", consumer_id="browser-1", binding=producer,
        execution_token="owner-token", bridge_dir=bridge_dir,
    )
    monkeypatch.setattr(reference_remix_continuation, "_state_root", lambda: tmp_path)
    if has_continuation:
        reference_remix_continuation.ReferenceRemixStore().create({
            "run_id": "continuation-a", "project_id": "p", "requester_user_id": "u",
            "canvas_id": "c", "agent_id": "main", "idempotency_key": "a",
            "status": "drafting_plan", "username": "alice", "resume_turn_id": "turn-a",
            "commission_ref": {"artifact_id": "commission-a", "revision": 1},
        })

    async def resolve(*_args):
        return ctx, "alice", "p", tmp_path, str(tmp_path)

    monkeypatch.setattr(freezone, "_resolve_freezone_project", resolve)
    monkeypatch.setattr(freezone.canvas_store, "read_canvas", lambda *_args: canvas)
    monkeypatch.setattr(chat, "_canvas_bridge_dir", lambda *_args, **_kwargs: bridge_dir)
    monkeypatch.setattr(reference_director, "list_director_artifacts",
                        lambda *_args, **_kwargs: {"commissions": commissions})
    monkeypatch.setattr(freezone, "get_task_backend",
                        lambda: pytest.fail("another commission's plan must not enqueue"))

    with pytest.raises(HTTPException) as error:
        await freezone.create_canvas_workflow_run("p", "c", {
            "bridge_key": "agent-plan-command.executor.owner-token",
            "actions": [{"node_id": "plan-b", "action": "generate_production_plan_draft",
                         "node_revision": 1, "payload": {"topic": "other"}}],
        }, {"id": "u", "username": "alice", "credential_kind": "browser_session"})
    assert error.value.status_code == 409
    assert error.value.detail == expected_detail
    assert list_workflow_runs(project_dir=tmp_path, canvas_id="c") == []


def _confirmed_plan_stage(tmp_path):
    from novelvideo.freezone.workflow_drafts import record_native_plan_canvas_binding

    binding = _binding()
    draft = _create(tmp_path, binding)
    _confirm(tmp_path, draft)
    canvas = {"nodes": [
        {"id": "plan-a", "data": {"workflowInstanceId": draft["draft_id"],
                                  "workflowPlanNodeId": "production_plan_1"}},
        {"id": "compose-a", "data": {"workflowInstanceId": draft["draft_id"],
                                     "workflowPlanNodeId": "composition_1"}},
    ]}
    record_native_plan_canvas_binding(
        project_dir=tmp_path, project_id="p", canvas_id="c", requester_user_id="u",
        draft_id=draft["draft_id"], task_id="task-1",
        canvas={**canvas, "project_id": "p", "canvas_id": "c", "revision": 1},
    )
    ctx = SimpleNamespace(state_dir=tmp_path, project_id="p", requester_user_id="u")
    commission = {**binding, "commission_ref": {"artifact_id": binding["commission_id"]},
                  "phase": "drafting_plan"}
    return ctx, canvas, commission


def _plan_attempt(tmp_path, *, result_ref=None, status="completed"):
    from novelvideo.freezone.workflow_runs import (
        create_workflow_run, update_workflow_run, workflow_runs_db_path,
    )

    run = create_workflow_run(project_dir=tmp_path, project_id="p", canvas_id="c", actor_id="u",
                              actions=[{"node_id": "plan-a", "action": "generate_production_plan_draft",
                                        "payload": {"topic": "new"}, "node_revision": 1}])
    if status == "completed":
        with sqlite3.connect(workflow_runs_db_path(tmp_path)) as connection:
            connection.execute(
                "UPDATE workflow_run_actions SET status='completed', artifact_status='valid', "
                "processing_operation_id=?, task_key=?, result_ref_json=? WHERE run_id=?",
                ("operation-" + run["run_id"], "task-" + run["run_id"],
                 json.dumps(result_ref), run["run_id"]),
            )
    else:
        update_workflow_run(project_dir=tmp_path, canvas_id="c", run_id=run["run_id"],
                            status="failed", action_updates=[{
                                "node_id": "plan-a", "action": "generate_production_plan_draft",
                                "status": "failed", "error": "retry failed"}])
    return run


def test_saved_plan_stage_reuses_caller_artifact_lock(tmp_path, monkeypatch):
    from test_production_commands import _plan
    from novelvideo.freezone.canvas_lock import CanvasLockBusy
    from novelvideo.freezone.remix.production_inventory import _LockedEvidenceStore
    from novelvideo.freezone.remix.store import RemixArtifactStore
    import novelvideo.freezone.remix.native_plan_stage as module

    ctx, canvas, commission = _confirmed_plan_stage(tmp_path)
    ctx.output_dir = tmp_path / "output"
    ctx.is_home_node = True
    store = RemixArtifactStore(ctx.output_dir, "p", created_by="u")
    saved = store.create("production_plan.v2", _plan(), [])
    _plan_attempt(tmp_path, result_ref={"artifact_id": saved["id"], "revision": 1})
    canvas["nodes"][0]["data"]["productionPlanV2Ref"] = {
        "artifactId": saved["id"], "revision": 1,
    }
    monkeypatch.setattr(module, "_bound_task_id", lambda *args: "completed-task")
    monkeypatch.setattr(module, "_bound_task_status", lambda *args: "completed")

    with store._locked() as guard:
        result = module.read_native_plan_stage(
            ctx=ctx, canvas=canvas, commission=commission,
            artifact_reader=_LockedEvidenceStore(store, guard),
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            competing_write = pool.submit(
                RemixArtifactStore(ctx.output_dir, "p", created_by="u").create,
                "production_plan.v2", _plan(), [],
            )
            with pytest.raises(CanvasLockBusy):
                competing_write.result(timeout=5)

    assert result["slots"][0]["state"] == "ready_for_review"
    assert result["next_action"]["kind"] == "review_production_plan"


def test_current_production_reads_saved_plan_under_existing_artifact_lock(tmp_path, monkeypatch):
    import asyncio
    from test_production_commands import _plan
    from novelvideo.freezone.remix.production_inventory import read_current_production
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.freezone.remix.reference_commission import (
        AuthenticatedDecisionEvidence, advance_reference_commission,
        create_reference_commission,
    )
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.freezone import canvas_store
    from novelvideo.project_context import ProjectContext
    from novelvideo.freezone.workflow_drafts import record_native_plan_canvas_binding
    import novelvideo.freezone.remix.native_plan_stage as module

    store = RemixArtifactStore(tmp_path / "output", "p", created_by="u")
    commission = create_reference_commission(
        store, canvas_id="c", requester_user_id="u", analysis_node_id="analysis",
        goal="replicate", analysis_evidence={"task_id": "analysis-task", "result_fingerprint": "hash"},
    )
    commission = advance_reference_commission(
        store, commission["commission_ref"],
        {"type": "direction_confirmed", "direction": {"target_goal": "replicate"}}, 1,
        host_evidence=AuthenticatedDecisionEvidence("p", "c", "u", "message"),
    )
    binding = {
        **_binding(commission=commission["commission_ref"]["artifact_id"]),
        "direction_fingerprint": commission["direction_fingerprint"],
    }
    draft = _create(tmp_path, binding)
    _confirm(tmp_path, draft)
    saved = store.create("production_plan.v2", _plan(), [])
    workflow = _plan_attempt(tmp_path, result_ref={"artifact_id": saved["id"], "revision": 1})
    operations = RemixOperationStore(store.project_dir, "p")
    operation = operations.start(
        idempotency_key="completed-plan", input_refs=[], producer_version="remix-process.v1",
        normalized_parameters={"action": "generate_production_plan_draft", "payload": {"topic": "new"}},
        node_revision=1, canvas_id="c", node_id="plan-a",
    )
    token = operations.claim_submission(operation["operation_id"], owner="test")
    operations.bind_task(operation["operation_id"], task_id="completed-task", owner="test", fencing_token=token)
    operations.publish_result_unverified(
        operation["operation_id"], output_refs=[{"artifact_id": saved["id"], "revision": 1}],
    )
    operations.claim_confirmation(operation["operation_id"], canvas_id="c", node_id="plan-a")
    operations.finish_confirmation(operation["operation_id"])
    from novelvideo.freezone.workflow_runs import workflow_runs_db_path
    with sqlite3.connect(workflow_runs_db_path(tmp_path)) as connection:
        connection.execute(
            "UPDATE workflow_run_actions SET processing_operation_id=? WHERE run_id=?",
            (operation["operation_id"], workflow["run_id"]),
        )
    canvas = {"project_id": "p", "canvas_id": "c", "revision": 1, "nodes": [
        {"id": "analysis", "type": "videoStoryNode", "data": {}},
        {"id": "plan-a", "type": "scriptNode", "data": {
            "workflowInstanceId": draft["draft_id"], "workflowPlanNodeId": "production_plan_1",
            "productionPlanV2Ref": {"artifactId": saved["id"], "revision": 1},
            "productionWorkflowAction": "generate_production_plan_draft",
            "productionWorkflowResultRef": {"artifactId": saved["id"], "revision": 1},
        }},
        {"id": "compose-a", "type": "videoComposeNode", "data": {
            "workflowInstanceId": draft["draft_id"], "workflowPlanNodeId": "composition_1",
        }},
    ], "edges": [{"source": "analysis", "target": "plan-a"}]}
    record_native_plan_canvas_binding(
        project_dir=tmp_path, project_id="p", canvas_id="c", requester_user_id="u",
        draft_id=draft["draft_id"], task_id="task-1", canvas=canvas,
    )
    monkeypatch.setattr(module, "_bound_task_id", lambda *args: "completed-task")
    monkeypatch.setattr(module, "_bound_task_status", lambda *args: "completed")

    current = read_current_production(
        store, canvas=canvas, canvas_id="c", agent_id="main", requester_user_id="u",
        runs=[], state_dir=tmp_path,
        commission_id=commission["commission_ref"]["artifact_id"],
    )

    assert current["native_plan_stage"]["state"] == "ready_for_review"
    assert current["next_action"]["kind"] == "review_production_plan"

    canvas_store.save_canvas(tmp_path, "c", base_revision=None, build_payload=lambda _: canvas)
    host = ProjectContext(
        "p", "p", "user", "u", "u", "u", "u", (("user", "u"),), "owner",
        "local", tmp_path / "output", tmp_path, tmp_path, True,
    )
    from novelvideo.freezone.remix.production_automation import _read_automation_state
    run = {"canvas_id": "c", "agent_id": "main", "commission_ref": commission["commission_ref"]}
    observed = _read_automation_state(host, run, [run])
    assert observed["native_plan_stage"]["state"] == "ready_for_review"
    from novelvideo.chat.reference_production_observer import production_event
    completion = production_event(store, canvas, commission, workflow_state_dir=tmp_path)
    assert completion["event"]["type"] == "plan_ready"

    from novelvideo.chat.reference_production_observer import ReferenceProductionObserver
    from novelvideo.chat.reference_remix_continuation import (
        ReferenceRemixCoordinator, ReferenceRemixStore,
    )
    from novelvideo.chat.store import ChatScope, ChatStore
    from novelvideo.freezone.remix.production_automation import enroll_production_automation
    from novelvideo.freezone.remix.reference_commission import read_reference_commission
    from novelvideo.api.routes import freezone

    enroll_production_automation(
        host, canvas_id="c", commission_ref=commission["commission_ref"],
        command_id="user-confirmed-direction",
    )
    monkeypatch.setenv("ST_REFERENCE_REMIX_AUTO_CONTINUATION", "1")
    queued_runs = []

    class Coordinator(ReferenceRemixCoordinator):

        async def _resolve_context(self, _run):
            return host

        def _assert_current(self, *_args, **_kwargs):
            return canvas_store.read_canvas(tmp_path, "c")

        @staticmethod
        def scope(_run):
            return ChatScope(kind="project", id="p", surface="freezone", canvas_id="c",
                             agent_id="main", state_dir=str(tmp_path))

        def _ensure_worker(self, queued):
            queued_runs.append(queued)

    continuation = {
        **run, "run_id": "continuation-1", "project_id": "p",
        "requester_user_id": "u", "username": "u",
        "analysis_node_id": "analysis", "commission_phase": "drafting_plan",
        "status": "drafting_plan", "commission_event_receipts": [],
        "idempotency_key": "one", "request_fingerprint": "one",
    }
    continuation_store = ReferenceRemixStore(tmp_path / "continuations.db")
    continuation_store.create(continuation)
    observer = ReferenceProductionObserver(Coordinator(continuation_store))
    async def no_cards(*_args, **_kwargs):
        return None
    observer._publish_cards = no_cards
    monkeypatch.setattr(
        "novelvideo.ports.get_task_backend",
        lambda: pytest.fail("completion observation must not start a generation task"),
    )
    monkeypatch.setattr(
        freezone, "get_task_backend",
        lambda: pytest.fail("native workflow recovery must not start a generation task"),
    )
    assert asyncio.run(observer.observe_run(continuation)) is True
    persisted = continuation_store.get(continuation["run_id"])
    assert len(persisted["commission_event_receipts"]) == 1
    assert persisted["commission_phase"] == "awaiting_plan_review"
    assert queued_runs and queued_runs[0]["commission_phase"] == "awaiting_plan_review"
    queued_commission = read_reference_commission(store, queued_runs[0]["commission_ref"], latest=True)
    assert queued_commission["plan_ref"]["artifact_id"] == saved["id"]

    restarted_observer = ReferenceProductionObserver(Coordinator(
        ReferenceRemixStore(tmp_path / "continuations.db")))
    restarted_observer._publish_cards = no_cards
    asyncio.run(restarted_observer.observe_run(persisted))
    scope = Coordinator.scope(persisted)
    notices = [message for message in ChatStore().list_messages("u", scope)
               if message["role"] == "system" and "作品方案" in message["content"]]
    assert len(notices) == 1
    assert notices[0]["reference_remix_run_id"] == continuation["run_id"]
    latest = read_reference_commission(store, commission["commission_ref"], latest=True)
    assert latest["phase"] == "awaiting_plan_review"


def test_completed_unpublished_plan_settlement_conflict_requires_resolution(tmp_path):
    from novelvideo.freezone.remix.production_processing_settlement import _record_conflict
    from novelvideo.freezone.workflow_runs import read_workflow_run

    ctx, canvas, commission = _confirmed_plan_stage(tmp_path)
    run = _plan_attempt(tmp_path, result_ref={"artifact_id": "plan-new", "revision": 1})
    action = read_workflow_run(project_dir=tmp_path, canvas_id="c", run_id=run["run_id"])["actions"][0]
    _record_conflict(ctx, canvas_id="c", run_id=run["run_id"], action=action,
                     reason="target_changed_or_stopped")

    slot = read_native_plan_stage(ctx=ctx, canvas=canvas, commission=commission)["slots"][0]
    assert slot["state"] == "resolve_conflict"
    assert slot["run_id"] == run["run_id"]
    assert slot["next_action"] == {"owner": "agent", "kind": "resolve_conflict", "node_id": "plan-a"}


def test_deleted_plan_node_with_settlement_conflict_requires_resolution(tmp_path):
    from novelvideo.freezone.remix.production_processing_settlement import _record_conflict
    from novelvideo.freezone.workflow_runs import read_workflow_run

    ctx, canvas, commission = _confirmed_plan_stage(tmp_path)
    run = _plan_attempt(tmp_path, result_ref={"artifact_id": "plan-new", "revision": 1})
    action = read_workflow_run(project_dir=tmp_path, canvas_id="c", run_id=run["run_id"])["actions"][0]
    _record_conflict(ctx, canvas_id="c", run_id=run["run_id"], action=action,
                     reason="target_changed_or_stopped")
    canvas["nodes"] = [node for node in canvas["nodes"] if node["id"] != "plan-a"]

    slot = read_native_plan_stage(ctx=ctx, canvas=canvas, commission=commission)["slots"][0]
    assert slot["state"] == "resolve_conflict"
    assert slot["next_action"]["kind"] == "resolve_conflict"


def test_old_plan_ref_does_not_hide_latest_failed_retry(tmp_path):
    ctx, canvas, commission = _confirmed_plan_stage(tmp_path)
    _plan_attempt(tmp_path, result_ref={"artifact_id": "plan-old", "revision": 1})
    canvas["nodes"][0]["data"]["productionPlanV2Ref"] = {"artifactId": "plan-old", "revision": 1}
    latest = _plan_attempt(tmp_path, status="failed")

    slot = read_native_plan_stage(ctx=ctx, canvas=canvas, commission=commission)["slots"][0]
    assert slot["run_id"] == latest["run_id"]
    assert slot["state"] == "failed"
    assert slot["next_action"] == {"owner": "agent", "kind": "inspect_failure"}


def test_old_plan_ref_does_not_hide_latest_conflicted_retry(tmp_path):
    from novelvideo.freezone.remix.production_processing_settlement import _record_conflict
    from novelvideo.freezone.workflow_runs import read_workflow_run

    ctx, canvas, commission = _confirmed_plan_stage(tmp_path)
    _plan_attempt(tmp_path, result_ref={"artifact_id": "plan-old", "revision": 1})
    canvas["nodes"][0]["data"]["productionPlanV2Ref"] = {"artifactId": "plan-old", "revision": 1}
    latest = _plan_attempt(tmp_path, result_ref={"artifact_id": "plan-new", "revision": 1})
    action = read_workflow_run(project_dir=tmp_path, canvas_id="c", run_id=latest["run_id"])["actions"][0]
    _record_conflict(ctx, canvas_id="c", run_id=latest["run_id"], action=action,
                     reason="target_changed_or_stopped")

    slot = read_native_plan_stage(ctx=ctx, canvas=canvas, commission=commission)["slots"][0]
    assert slot["run_id"] == latest["run_id"]
    assert slot["state"] == "resolve_conflict"
    assert slot["next_action"]["kind"] == "resolve_conflict"


def test_ready_for_review_requires_latest_result_ref_on_current_node(tmp_path, monkeypatch):
    import novelvideo.freezone.remix.native_plan_stage as module
    monkeypatch.setattr(module, '_bound_task_id', lambda *args: 'completed-task')
    monkeypatch.setattr(module, '_bound_task_status', lambda *args: 'completed')
    ctx, canvas, commission = _confirmed_plan_stage(tmp_path)
    _plan_attempt(tmp_path, result_ref={"artifact_id": "plan-new", "revision": 2})
    canvas["nodes"][0]["data"]["productionPlanV2Ref"] = {"artifactId": "plan-old", "revision": 1}

    stale = read_native_plan_stage(ctx=ctx, canvas=canvas, commission=commission)["slots"][0]
    assert stale["state"] == "resolve_conflict"
    assert stale["next_action"]["kind"] == "resolve_conflict"

    canvas["nodes"][0]["data"]["productionPlanV2Ref"] = {"artifactId": "plan-new", "revision": 2}
    current = read_native_plan_stage(ctx=ctx, canvas=canvas, commission=commission)["slots"][0]
    assert current["state"] == "ready_for_review"
    assert current["next_action"]["kind"] == "review_production_plan"


def test_previous_attempt_conflict_does_not_block_current_published_plan(tmp_path, monkeypatch):
    import novelvideo.freezone.remix.native_plan_stage as module
    monkeypatch.setattr(module, '_bound_task_id', lambda *args: 'completed-task')
    monkeypatch.setattr(module, '_bound_task_status', lambda *args: 'completed')
    from novelvideo.freezone.remix.production_processing_settlement import _record_conflict
    from novelvideo.freezone.workflow_runs import read_workflow_run

    ctx, canvas, commission = _confirmed_plan_stage(tmp_path)
    previous = _plan_attempt(tmp_path, result_ref={"artifact_id": "plan-old", "revision": 1})
    previous_action = read_workflow_run(project_dir=tmp_path, canvas_id="c",
                                        run_id=previous["run_id"])["actions"][0]
    _record_conflict(ctx, canvas_id="c", run_id=previous["run_id"], action=previous_action,
                     reason="target_changed_or_stopped")
    latest = _plan_attempt(tmp_path, result_ref={"artifact_id": "plan-new", "revision": 2})
    canvas["nodes"][0]["data"]["productionPlanV2Ref"] = {"artifactId": "plan-new", "revision": 2}

    slot = read_native_plan_stage(ctx=ctx, canvas=canvas, commission=commission)["slots"][0]
    assert slot["run_id"] == latest["run_id"]
    assert slot["state"] == "ready_for_review"


def test_replaced_plan_node_keeps_binding_conflict_over_old_settlement_conflict(tmp_path):
    from novelvideo.freezone.remix.production_processing_settlement import _record_conflict
    from novelvideo.freezone.workflow_runs import read_workflow_run

    ctx, canvas, commission = _confirmed_plan_stage(tmp_path)
    run = _plan_attempt(tmp_path, result_ref={"artifact_id": "plan-old", "revision": 1})
    action = read_workflow_run(project_dir=tmp_path, canvas_id="c", run_id=run["run_id"])["actions"][0]
    _record_conflict(ctx, canvas_id="c", run_id=run["run_id"], action=action,
                     reason="target_changed_or_stopped")
    canvas["nodes"][0]["id"] = "replacement-plan"

    slot = read_native_plan_stage(ctx=ctx, canvas=canvas, commission=commission)["slots"][0]
    assert slot["state"] == "binding_conflict"
    assert slot["next_action"] == {
        "owner": "agent", "kind": "resolve_binding_conflict",
        "candidate_node_ids": ["replacement-plan"],
    }


@pytest.mark.asyncio
async def test_duplicate_prepare_returns_bound_draft_before_recompiling(tmp_path, monkeypatch):
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from test_reference_workflow_gate import confirmed_direction

    store = RemixArtifactStore(tmp_path, "p", created_by="u")
    commission = confirmed_direction(store)
    binding = {**_binding(commission["commission_ref"]["artifact_id"]),
               "direction_fingerprint": commission["direction_fingerprint"]}
    first = _create(tmp_path, binding)
    async def resolve(*args):
        return SimpleNamespace(project_id="p", requester_user_id="u", state_dir=tmp_path,
                               output_dir=tmp_path), "u", "p", tmp_path, str(tmp_path)
    monkeypatch.setattr(freezone, "_resolve_freezone_project", resolve)
    monkeypatch.setattr(freezone, "get_usage_meter", lambda: freezone.NoOpUsageMeter())
    async def forbidden(*args, **kwargs):
        pytest.fail("duplicate prepare must reuse the saved draft before recompiling")
    monkeypatch.setattr(freezone, "_prepare_workflow_source", forbidden)
    result = await freezone.create_canvas_workflow_draft("p", "c",
        {"intent": {"reference_commission_id": binding["commission_id"]}}, {"user_id": "u"})
    assert result["data"]["draft_id"] == first["draft_id"]


def test_unprepared_stage_recommends_single_native_submission(tmp_path):
    binding = _binding()
    ctx = SimpleNamespace(state_dir=tmp_path, project_id="p", requester_user_id="u")
    commission = {**binding, "commission_ref": {"artifact_id": binding["commission_id"]},
                  "phase": "drafting_plan"}
    stage = read_native_plan_stage(ctx=ctx, canvas={"project_id": "p", "nodes": [], "edges": []},
                                   commission=commission)
    assert stage["state"] == "unprepared"
    assert stage["next_action"]["tool"] == "freezone_prepare_production_nodes"


@pytest.mark.parametrize('task_status,expected', [('queued', 'submitted'), ('running', 'running'), ('failed', 'failed')])
def test_native_stage_reports_actual_bound_task_not_workflow_running(tmp_path, monkeypatch, task_status, expected):
    from novelvideo.freezone.workflow_runs import create_workflow_run, update_workflow_run
    import novelvideo.freezone.remix.native_plan_stage as module
    draft = _create(tmp_path, _binding())
    _confirm(tmp_path, draft)
    canvas = {'nodes': [
        {'id': 'plan', 'data': {'workflowInstanceId': draft['draft_id'], 'workflowPlanNodeId': 'production_plan_1'}},
        {'id': 'compose', 'data': {'workflowInstanceId': draft['draft_id'], 'workflowPlanNodeId': 'composition_1'}}]}
    run = create_workflow_run(project_dir=tmp_path, project_id='p', canvas_id='c', actor_id='u',
                             actions=[{'node_id': 'plan', 'action': 'generate_production_plan_draft', 'payload': {}, 'node_revision': 1}])
    update_workflow_run(project_dir=tmp_path, canvas_id='c', run_id=run['run_id'], action_updates=[
        {'node_id': 'plan', 'action': 'generate_production_plan_draft', 'status': 'running'}])
    monkeypatch.setattr(module, '_bound_task_id', lambda *args: 'real-task')
    monkeypatch.setattr(module, '_bound_task_status', lambda *args: task_status, raising=False)
    ctx = SimpleNamespace(state_dir=tmp_path, project_id='p', requester_user_id='u')
    stage = read_native_plan_stage(ctx=ctx, canvas=canvas, commission={**_binding(),
        'commission_ref': {'artifact_id': 'commission-1'}, 'phase': 'drafting_plan'})
    assert stage['slots'][0]['state'] == expected


def test_saved_plan_without_successful_bound_task_waits_for_receipt(tmp_path):
    ctx, canvas, commission = _confirmed_plan_stage(tmp_path)
    _plan_attempt(tmp_path, result_ref={'artifact_id': 'plan-new', 'revision': 2})
    canvas['nodes'][0]['data']['productionPlanV2Ref'] = {'artifactId': 'plan-new', 'revision': 2}
    assert read_native_plan_stage(ctx=ctx, canvas=canvas, commission=commission)['slots'][0]['state'] == 'awaiting_canvas_receipt'


def test_outline_completion_offers_explicit_arrangement_instead_of_full_review(tmp_path, monkeypatch):
    import novelvideo.freezone.remix.native_plan_stage as module
    monkeypatch.setattr(module, '_bound_task_id', lambda *args: 'completed-task')
    monkeypatch.setattr(module, '_bound_task_status', lambda *args: 'completed')
    ctx, canvas, commission = _confirmed_plan_stage(tmp_path)
    _plan_attempt(tmp_path, result_ref={'artifact_id': 'outline-plan', 'revision': 1})
    canvas['nodes'][0]['data'].update(productionPlanV2Ref={'artifactId': 'outline-plan', 'revision': 1},
        productionPlanV2Draft={'authoring_stage': 'outline', 'placements': []})
    slot = read_native_plan_stage(ctx=ctx, canvas=canvas, commission=commission)['slots'][0]
    assert slot['state'] == 'outline_ready'
    assert slot['next_action']['action'] == 'generate_production_plan_arrangement'
    assert slot['next_action']['parameters'] == {'plan_ref': {'artifact_id': 'outline-plan', 'revision': 1}}


def test_edited_saved_outline_uses_current_revision_for_next_action(tmp_path, monkeypatch):
    import novelvideo.freezone.remix.native_plan_stage as module
    monkeypatch.setattr(module, '_bound_task_id', lambda *args: 'completed-task')
    monkeypatch.setattr(module, '_bound_task_status', lambda *args: 'completed')
    ctx, canvas, commission = _confirmed_plan_stage(tmp_path)
    original_claim = module.read_native_plan_claim
    def claim_with_revision(**kwargs):
        draft = original_claim(**kwargs)
        draft['compiled']['plan']['nodes'][0]['data']['remixNodeRevision'] = 1
        return draft
    monkeypatch.setattr(module, 'read_native_plan_claim', claim_with_revision)
    _plan_attempt(tmp_path, result_ref={'artifact_id': 'outline-plan', 'revision': 1})
    canvas['nodes'][0]['data'].update(remixNodeRevision=2, productionPlanV2Ref={'artifactId': 'outline-plan', 'revision': 2},
        productionPlanV2Draft={'authoring_stage': 'outline', 'placements': []})
    slot = read_native_plan_stage(ctx=ctx, canvas=canvas, commission=commission)['slots'][0]
    assert slot['state'] == 'outline_ready'
    assert slot['next_action']['parameters']['plan_ref']['revision'] == 2


def test_failed_arrangement_keeps_outline_and_reports_only_failed_action(tmp_path, monkeypatch):
    import novelvideo.freezone.remix.native_plan_stage as module
    import novelvideo.freezone.workflow_runs as runs_module
    ctx, canvas, commission = _confirmed_plan_stage(tmp_path)
    _plan_attempt(tmp_path, status='failed')
    original_runs = runs_module.list_workflow_runs
    def arrangement_runs(**kwargs):
        runs = original_runs(**kwargs)
        for run in runs:
            for action in run['actions']:
                action['action'] = 'generate_production_plan_arrangement'
        return runs
    monkeypatch.setattr(runs_module, 'list_workflow_runs', arrangement_runs)
    canvas['nodes'][0]['data'].update(productionPlanV2Ref={'artifactId': 'outline-plan', 'revision': 2},
        productionPlanV2Draft={'authoring_stage': 'outline', 'placements': []})
    slot = read_native_plan_stage(ctx=ctx, canvas=canvas, commission=commission)['slots'][0]
    assert slot['state'] == 'failed'
    assert slot['next_action']['kind'] == 'inspect_failure'
    assert slot['next_action']['action'] == 'generate_production_plan_arrangement'
    assert slot['plan_ref']['revision'] == 2


def test_arrangement_completion_after_outline_edits_can_be_reviewed(tmp_path, monkeypatch):
    import novelvideo.freezone.remix.native_plan_stage as module
    import novelvideo.freezone.workflow_runs as runs_module
    monkeypatch.setattr(module, '_bound_task_id', lambda *args: 'completed-task')
    monkeypatch.setattr(module, '_bound_task_status', lambda *args: 'completed')
    ctx, canvas, commission = _confirmed_plan_stage(tmp_path)
    _plan_attempt(tmp_path, result_ref={'artifact_id': 'plan', 'revision': 3})
    original_claim = module.read_native_plan_claim
    def claim_with_revision(**kwargs):
        draft = original_claim(**kwargs)
        draft['compiled']['plan']['nodes'][0]['data']['remixNodeRevision'] = 1
        return draft
    monkeypatch.setattr(module, 'read_native_plan_claim', claim_with_revision)
    original_runs = runs_module.list_workflow_runs
    def arrangement_runs(**kwargs):
        runs = original_runs(**kwargs)
        for run in runs:
            for action in run['actions']:
                action['action'] = 'generate_production_plan_arrangement'
        return runs
    monkeypatch.setattr(runs_module, 'list_workflow_runs', arrangement_runs)
    canvas['nodes'][0]['data'].update(remixNodeRevision=2,
        productionPlanV2Ref={'artifactId': 'plan', 'revision': 3},
        productionPlanV2Draft={'authoring_stage': 'arranged', 'placements': [{'id': 'p1'}]})
    slot = read_native_plan_stage(ctx=ctx, canvas=canvas, commission=commission)['slots'][0]
    assert slot['state'] == 'ready_for_review'


@pytest.mark.parametrize('conflict', [None, 'dirty', 'different_artifact', 'stale_saved_revision'])
def test_saved_manual_arrangement_after_outline_uses_normal_review(tmp_path, monkeypatch, conflict):
    from copy import deepcopy
    from test_production_commands import _plan
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.freezone.remix.production_commands import apply_production_command
    import novelvideo.freezone.remix.native_plan_stage as module

    monkeypatch.setattr(module, '_bound_task_id', lambda *args: 'completed-task')
    monkeypatch.setattr(module, '_bound_task_status', lambda *args: 'completed')
    ctx, canvas, commission = _confirmed_plan_stage(tmp_path)
    ctx.output_dir = tmp_path / 'output'
    store = RemixArtifactStore(ctx.output_dir, 'p', created_by='u')
    outline = _plan()
    placement = outline['placements'].pop()
    outline['authoring_stage'] = 'outline'
    saved = store.create('production_plan.v2', outline, [])
    _plan_attempt(tmp_path, result_ref={'artifact_id': saved['id'], 'revision': 1})
    apply_production_command(store, {'command_id': 'manual-arrangement', 'kind': 'upsert_placement',
        'target_id': placement['id'], 'payload': {'placement': placement},
        'plan_ref': {'artifact_id': saved['id'], 'revision': 1}})
    current = store.read_latest(saved['id'])
    if conflict == 'different_artifact':
        current = store.create('production_plan.v2', current['payload'], [])
    elif conflict == 'stale_saved_revision':
        store.revise(current['id'], current['revision'], deepcopy(current['payload']), [])
    original_claim = module.read_native_plan_claim
    def claim_with_revision(**kwargs):
        draft = original_claim(**kwargs)
        draft['compiled']['plan']['nodes'][0]['data']['remixNodeRevision'] = 1
        return draft
    monkeypatch.setattr(module, 'read_native_plan_claim', claim_with_revision)
    canvas['nodes'][0]['data'].update(remixNodeRevision=2,
        productionPlanV2Ref={'artifactId': current['id'], 'revision': current['revision']},
        productionPlanV2Draft=current['payload'], productionPlanV2Dirty=conflict == 'dirty')
    slot = read_native_plan_stage(ctx=ctx, canvas=canvas, commission=commission)['slots'][0]
    assert slot['state'] == ('resolve_conflict' if conflict else 'ready_for_review')
    if not conflict:
        assert slot['next_action']['kind'] == 'review_production_plan'
        assert slot['plan_ref']['revision'] == 2
