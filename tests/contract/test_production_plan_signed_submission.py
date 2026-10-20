"""A bound plan draft crosses the real task signer and local worker."""

import asyncio
import time

import pytest

from novelvideo.api.routes.remix import prepare_registered_action
from novelvideo.freezone.remix.operations import RemixOperationStore
from novelvideo.freezone.remix.production_requirements import resolve_production_requirements
from novelvideo.ports import registry
from novelvideo.ports.local.tasks import InMemoryCancellationStore
from novelvideo.project_context import ProjectContext
from novelvideo.task_state import TaskStateManager
from test_production_requirements import commission_fixture


@pytest.mark.asyncio
async def test_bound_plan_draft_is_signed_executed_and_published_once(
    tmp_path, monkeypatch, signed_inline_backend,
):
    from novelvideo.task_backend.runners import remix as runner

    manager = TaskStateManager()
    monkeypatch.setattr(registry, "_PORTS", dict(registry._PORTS))
    registry.register_port("cancellation_store", InMemoryCancellationStore())
    monkeypatch.setattr("novelvideo.task_state._task_manager", manager)
    monkeypatch.setattr("novelvideo.ports.local.tasks.get_task_manager", lambda: manager)
    ctx = ProjectContext(
        "p", "p", "user", "u", "alice", "u", "alice", (("user", "u"),),
        "owner", "local", tmp_path, tmp_path, tmp_path, True,
    )
    store, commission, _ = commission_fixture(
        tmp_path, {"duration_us": {"max": 10_000_000}}
    )
    resolved = resolve_production_requirements(store, commission=commission)
    prepared = prepare_registered_action(
        store, "generate_production_plan_draft", {"topic": "黑咖啡", **resolved},
        idempotency_key="signed-execution", node_revision=1,
    )
    operations = RemixOperationStore(tmp_path, "p")
    operation = operations.start(
        idempotency_key="signed-execution", input_refs=prepared["input_refs"],
        producer_version=prepared["producer_version"],
        normalized_parameters=prepared["normalized_parameters"], node_revision=1,
    )
    model_calls = []

    async def generate(**kwargs):
        model_calls.append(kwargs)
        return {
            "schema_version": 2, "goal": kwargs["topic"],
            "output": {"width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1},
            "beats": [{"id": "hook", "purpose": "引出产品", "duration_frames": 90,
                       "turns": []}],
            "resources": [], "placements": [], "provenance": {},
        }

    monkeypatch.setattr(runner, "generate_production_plan_draft", generate)
    backend = signed_inline_backend()
    payload = {**prepared["task_payload"], "operation_id": operation["operation_id"]}
    queued = await backend.enqueue_project_task(
        ctx, product_surface="freezone", task_type=prepared["task_type"],
        queue_kind=prepared["queue_kind"], episode=0,
        scope=operation["operation_id"], payload=payload,
    )
    deadline = time.monotonic() + 5
    state = None
    while time.monotonic() < deadline:
        state = manager.get_task_for_project(
            ctx, prepared["task_type"], 0, scope=operation["operation_id"]
        )
        if state is not None and state.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.02)

    assert state is not None and state.status == "completed", state
    assert state.task_id == queued.task_state.task_id
    assert len(model_calls) == 1
    assert model_calls[0]["requirements"] == resolved["requirements"]
    saved_operation = operations.get(operation["operation_id"])
    assert len(saved_operation["output_refs"]) == 1
    artifact_ref = saved_operation["output_refs"][0]
    artifact = store.read(artifact_ref["artifact_id"], artifact_ref["revision"])
    assert artifact["payload"]["provenance"]["requirements_provenance"] == (
        resolved["requirements_provenance"]
    )
