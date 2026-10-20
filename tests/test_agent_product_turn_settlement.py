from __future__ import annotations

from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from novelvideo.freezone import agent_product_operations as operations
from novelvideo.freezone.agent_product_operations import (
    bind_agent_product_model_execution,
    bind_agent_product_task,
    create_agent_product_operation,
    finish_agent_product_operation,
    read_agent_product_operation,
)
from novelvideo.project_context import ProjectContext
from novelvideo.task_state import TaskStateManager


def _admitted(
    tmp_path: Path, *, turn_id: str = "turn-a", bind_turn: bool = True
) -> tuple[dict, TaskStateManager, ProjectContext]:
    ctx = ProjectContext(
        project_id="project-a",
        project_name="project",
        owner_type="user",
        owner_id="user-a",
        owner_username="alice",
        requester_user_id="user-a",
        requester_username="alice",
        requester_principals=(("user", "user-a"),),
        effective_role="owner",
        home_node_id="local",
        output_dir=tmp_path,
        state_dir=tmp_path,
        runtime_dir=tmp_path,
        is_home_node=True,
    )
    operation = create_agent_product_operation(
        project_dir=tmp_path,
        project_id=ctx.project_id,
        product_kind="workflow_result",
        idempotency_key="attempt-a",
        generation_session_id="generation-a",
        canvas_id="canvas-a",
    )
    manager = TaskStateManager()
    task = manager.create_task_for_project(
        ctx,
        operation["task_type"],
        0,
        scope=operation["operation_id"],
        metadata={"operation_id": operation["operation_id"], "backend": "celery"},
        status="running",
    )
    bind_agent_product_task(
        project_dir=tmp_path,
        operation_id=operation["operation_id"],
        task_id=task.task_id,
        root_task_id=task.task_id,
    )
    if bind_turn:
        operations.bind_agent_product_turn_admission(
            project_dir=tmp_path,
            operation_id=operation["operation_id"],
            expected_task_id=task.task_id,
            turn_id=turn_id,
            tool_call_id="admit-call",
        )
    return operation, manager, ctx


def _task(manager: TaskStateManager, ctx: ProjectContext, operation: dict):
    return manager.get_task_for_project(
        ctx, operation["task_type"], 0, scope=operation["operation_id"]
    )


@pytest.mark.asyncio
async def test_reserved_tool_admission_binds_business_turn_before_exit(tmp_path):
    from novelvideo.chat.service import _bind_server_observed_agent_product_execution

    operation, manager, ctx = _admitted(tmp_path, turn_id="business-a", bind_turn=False)
    task = _task(manager, ctx, operation)
    event = SimpleNamespace(
        type="tool_updated", name="dramaclaw.freezone_begin_agent_product_generation",
        turn_id="runtime-a", call_id="admit-call", status="completed", error=None,
        structured={"ok": True, "status": "reserved",
                    "operation_id": operation["operation_id"], "task_id": task.task_id},
        output=None,
    )
    await _bind_server_observed_agent_product_execution(
        event, project_dir=tmp_path, project_state_dir=tmp_path,
        business_turn_id="business-a",
    )
    settled = operations.settle_agent_product_turn(
        project_dir=tmp_path, turn_id="business-a", outcome="failed",
    )
    assert settled[0]["status"] == "failed"
    assert _task(manager, ctx, operation).status == "failed"


def test_failed_turn_terminalizes_admission_and_placeholder_task(tmp_path):
    operation, manager, ctx = _admitted(tmp_path)
    assert _task(manager, ctx, operation).status == "running"

    settled = operations.settle_agent_product_turn(
        project_dir=tmp_path, turn_id="turn-a", outcome="failed"
    )

    assert settled[0]["status"] == "failed"
    assert (
        read_agent_product_operation(
            project_dir=tmp_path, operation_id=operation["operation_id"]
        )["status"]
        == "failed"
    )
    assert _task(manager, ctx, operation).status == "failed"
    assert _task(manager, ctx, operation).error


def test_verified_parameter_wait_transfers_only_after_answer_and_expires(tmp_path, monkeypatch):
    import time

    operation, manager, ctx = _admitted(tmp_path)
    now = time.time()
    receipt = {
        'kind': 'clarification_bridge', 'id': 'bridge-a',
        'clarification_id': 'clarify-a', 'event_fingerprint': 'event-a',
        'expires_at': now + 600, 'project_id': 'project-a', 'canvas_id': 'canvas-a',
    }
    operations.record_agent_product_turn_wait(
        project_dir=tmp_path, operation_id=operation['operation_id'],
        expected_task_id=_task(manager, ctx, operation).task_id, turn_id='turn-a',
        waiting_reason='user_confirmation', receipt_ref=receipt,
    )
    assert operations.settle_agent_product_turn(
        project_dir=tmp_path, turn_id='turn-a', outcome='completed',
    )[0]['waiting_receipt'] == receipt
    assert operations.transfer_agent_product_clarification_wait(
        project_dir=tmp_path, old_turn_id='turn-a', new_turn_id='turn-b',
        bridge_key='wrong', clarification_id='clarify-a',
        project_id='project-a', canvas_id='canvas-a',
    ) == []
    assert operations.transfer_agent_product_clarification_wait(
        project_dir=tmp_path, old_turn_id='turn-a', new_turn_id='turn-b',
        bridge_key='bridge-a', clarification_id='clarify-a',
        project_id='project-a', canvas_id='canvas-a',
    ) == [operation['operation_id']]
    assert operations.settle_agent_product_turn(
        project_dir=tmp_path, turn_id='turn-a', outcome='completed',
    ) == []
    assert operations.settle_agent_product_turn(
        project_dir=tmp_path, turn_id='turn-b', outcome='failed',
    )[0]['status'] == 'failed'
    assert _task(manager, ctx, operation).status == 'failed'

    next_operation, next_manager, next_ctx = _admitted(tmp_path / 'expired')
    operations.record_agent_product_turn_wait(
        project_dir=tmp_path / 'expired', operation_id=next_operation['operation_id'],
        expected_task_id=_task(next_manager, next_ctx, next_operation).task_id,
        turn_id='turn-a',
        waiting_reason='user_confirmation', receipt_ref=receipt,
    )
    monkeypatch.setattr(operations.time, 'time', lambda: now + 601)
    assert operations.settle_agent_product_turn(
        project_dir=tmp_path / 'expired', turn_id='turn-a', outcome='interrupted',
    )[0]['status'] == 'failed'


def test_same_turn_retry_can_deliver_before_exit_without_second_result(tmp_path):
    operation, manager, ctx = _admitted(tmp_path)
    # A failed tool attempt must leave the admission intact for a corrected call.
    assert (
        read_agent_product_operation(
            project_dir=tmp_path, operation_id=operation["operation_id"]
        )["status"]
        == "reserved"
    )
    task_id = _task(manager, ctx, operation).task_id
    bind_agent_product_model_execution(
        project_dir=tmp_path,
        operation_id=operation["operation_id"],
        model_call_id="agent-turn:turn-a:tool:fixed",
        executed_at=1.0,
        source="server_observed_agent_turn",
        turn_id="turn-a",
        tool_call_id="fixed",
    )
    delivered = finish_agent_product_operation(
        project_dir=tmp_path,
        operation_id=operation["operation_id"],
        outcome="delivered",
        expected_task_id=task_id,
        result_ref={"kind": "workflow_draft", "id": "draft-a"},
    )

    settled = operations.settle_agent_product_turn(
        project_dir=tmp_path, turn_id="turn-a", outcome="completed"
    )
    assert delivered["status"] == settled[0]["status"] == "delivered"
    assert _task(manager, ctx, operation).status == "completed"


def test_admission_receipt_must_match_persisted_task(tmp_path):
    operation, manager, ctx = _admitted(tmp_path)
    with pytest.raises(ValueError, match="task identity mismatch"):
        operations.bind_agent_product_turn_admission(
            project_dir=tmp_path,
            operation_id=operation["operation_id"],
            expected_task_id="unrelated",
            turn_id="turn-b",
            tool_call_id="forged",
        )
    assert _task(manager, ctx, operation).status == "running"
    assert (
        operations.settle_agent_product_turn(
            project_dir=tmp_path, turn_id="turn-b", outcome="failed"
        )
        == []
    )


def test_cancelled_turn_is_idempotent_and_fences_replaced_task(tmp_path):
    operation, manager, ctx = _admitted(tmp_path)
    first = operations.settle_agent_product_turn(
        project_dir=tmp_path, turn_id="turn-a", outcome="cancelled"
    )
    second = operations.settle_agent_product_turn(
        project_dir=tmp_path, turn_id="turn-a", outcome="cancelled"
    )
    assert first[0]["status"] == second[0]["status"] == "cancelled"
    assert _task(manager, ctx, operation).status == "cancelled"


def test_user_wait_survives_turn_exit_and_stale_task_sweep(tmp_path, monkeypatch):
    operation, manager, ctx = _admitted(tmp_path)
    task_id = _task(manager, ctx, operation).task_id
    operations.record_agent_product_turn_wait(
        project_dir=tmp_path,
        operation_id=operation["operation_id"],
        expected_task_id=task_id,
        turn_id="turn-a",
        waiting_reason="user_confirmation",
        receipt_ref={"kind": "canvas_confirmation", "id": "confirm-a"},
    )
    settled = operations.settle_agent_product_turn(
        project_dir=tmp_path, turn_id="turn-a", outcome="completed"
    )
    assert settled[0]["waiting_reason"] == "user_confirmation"
    assert settled[0]["waiting_receipt"]["id"] == "confirm-a"
    assert (
        read_agent_product_operation(
            project_dir=tmp_path, operation_id=operation["operation_id"]
        )["status"]
        == "reserved"
    )
    assert read_agent_product_operation(
        project_dir=tmp_path, operation_id=operation["operation_id"]
    )["waiting_receipt"] == {"kind": "canvas_confirmation", "id": "confirm-a"}
    monkeypatch.setattr(TaskStateManager, "CELERY_STALE_TTL", 1)
    with sqlite3.connect(tmp_path / "data.db") as conn:
        conn.execute(
            "UPDATE task_states SET updated_at = '2000-01-01T00:00:00.000000Z' WHERE task_id = ?",
            (task_id,),
        )
    manager.sweep_stale_tasks_for_project(ctx)
    assert _task(manager, ctx, operation).status == "running"


def test_repeated_settlement_repairs_task_write_interruption(tmp_path, monkeypatch):
    operation, manager, ctx = _admitted(tmp_path)
    from novelvideo import task_state

    original = task_state.reconcile_agent_product_task_for_operation

    def interrupted(*, project_dir, operation):
        raise OSError("task store unavailable")

    monkeypatch.setattr(
        task_state, "reconcile_agent_product_task_for_operation", interrupted
    )
    with pytest.raises(OSError, match="task store unavailable"):
        operations.settle_agent_product_turn(
            project_dir=tmp_path, turn_id="turn-a", outcome="failed"
        )
    assert (
        read_agent_product_operation(
            project_dir=tmp_path, operation_id=operation["operation_id"]
        )["status"]
        == "failed"
    )
    assert _task(manager, ctx, operation).status == "running"
    monkeypatch.setattr(
        task_state, "reconcile_agent_product_task_for_operation", original
    )
    assert _task(manager, ctx, operation).status == "failed"


def test_replaced_task_is_not_terminalized_by_old_admission(tmp_path):
    operation, manager, ctx = _admitted(tmp_path)
    with sqlite3.connect(tmp_path / "data.db") as conn:
        conn.execute(
            "UPDATE task_states SET task_id = 'new-task' WHERE task_id = ?",
            (_task(manager, ctx, operation).task_id,),
        )
    with pytest.raises(ValueError, match="task identity mismatch"):
        operations.settle_agent_product_turn(
            project_dir=tmp_path, turn_id="turn-a", outcome="failed"
        )
    assert _task(manager, ctx, operation).status == "running"


def test_delivery_racing_with_turn_exit_wins_over_failure(tmp_path, monkeypatch):
    operation, manager, ctx = _admitted(tmp_path)
    task_id = _task(manager, ctx, operation).task_id
    bind_agent_product_model_execution(
        project_dir=tmp_path,
        operation_id=operation["operation_id"],
        model_call_id="agent-turn:turn-a:tool:result",
        executed_at=1.0,
        source="server_observed_agent_turn",
        turn_id="turn-a",
        tool_call_id="result",
    )
    original = operations.finish_agent_product_operation

    def deliver_first(**kwargs):
        original(
            project_dir=tmp_path,
            operation_id=operation["operation_id"],
            outcome="delivered",
            expected_task_id=task_id,
            result_ref={"kind": "workflow_draft", "id": "draft-a"},
        )
        return original(**kwargs)

    monkeypatch.setattr(operations, "finish_agent_product_operation", deliver_first)
    settled = operations.settle_agent_product_turn(
        project_dir=tmp_path, turn_id="turn-a", outcome="failed"
    )
    assert settled[0]["status"] == "delivered"
    assert _task(manager, ctx, operation).status == "completed"


def test_crashed_turn_recovery_keeps_saved_delivery_and_wait(tmp_path):
    operation, manager, ctx = _admitted(tmp_path)
    task_id = _task(manager, ctx, operation).task_id
    bind_agent_product_model_execution(
        project_dir=tmp_path,
        operation_id=operation["operation_id"],
        model_call_id="agent-turn:turn-a:tool:result",
        executed_at=1.0,
        source="server_observed_agent_turn",
        turn_id="turn-a",
        tool_call_id="result",
    )
    finish_agent_product_operation(
        project_dir=tmp_path,
        operation_id=operation["operation_id"],
        outcome="delivered",
        expected_task_id=task_id,
        result_ref={"kind": "workflow_draft", "id": "draft-a"},
    )
    recovered = operations.reconcile_expired_agent_product_turns(
        project_dir=tmp_path, expired_turn_ids=["turn-a"]
    )
    assert recovered[0]["status"] == "delivered"
    assert _task(manager, ctx, operation).status == "completed"

    other = create_agent_product_operation(
        project_dir=tmp_path,
        project_id="project-a",
        product_kind="workflow_result",
        idempotency_key="attempt-b",
        generation_session_id="generation-b",
        canvas_id="canvas-a",
    )
    other_task = manager.create_task_for_project(
        ctx,
        other["task_type"],
        0,
        scope=other["operation_id"],
        metadata={"operation_id": other["operation_id"], "backend": "celery"},
        status="running",
    )
    bind_agent_product_task(
        project_dir=tmp_path,
        operation_id=other["operation_id"],
        task_id=other_task.task_id,
        root_task_id=other_task.task_id,
    )
    operations.bind_agent_product_turn_admission(
        project_dir=tmp_path,
        operation_id=other["operation_id"],
        expected_task_id=other_task.task_id,
        turn_id="turn-b",
        tool_call_id="admit-b",
    )
    operations.record_agent_product_turn_wait(
        project_dir=tmp_path,
        operation_id=other["operation_id"],
        expected_task_id=other_task.task_id,
        turn_id="turn-b",
        waiting_reason="user_confirmation",
        receipt_ref={"kind": "canvas_confirmation", "id": "confirm-b"},
    )
    recovered_wait = operations.reconcile_expired_agent_product_turns(
        project_dir=tmp_path, expired_turn_ids=["turn-b"]
    )
    assert recovered_wait[0]["waiting_reason"] == "user_confirmation"
    assert _task(manager, ctx, other).status == "running"


def test_native_wait_requires_an_active_same_project_task(tmp_path):
    operation, manager, ctx = _admitted(tmp_path)
    waiter_task_id = _task(manager, ctx, operation).task_id
    native = manager.create_task_for_project(
        ctx, "freezone_image_gen", 0, scope="native-a", status="running"
    )
    receipt = {
        "kind": "project_task",
        "id": native.task_id,
        "task_type": native.task_type,
        "scope": native.scope,
    }
    operations.record_agent_product_turn_wait(
        project_dir=tmp_path,
        operation_id=operation["operation_id"],
        expected_task_id=waiter_task_id,
        turn_id="turn-a",
        waiting_reason="native_task",
        receipt_ref=receipt,
    )
    settled = operations.settle_agent_product_turn(
        project_dir=tmp_path, turn_id="turn-a", outcome="completed"
    )
    assert settled[0]["waiting_receipt"] == receipt
    assert _task(manager, ctx, operation).status == "running"

    another = create_agent_product_operation(
        project_dir=tmp_path,
        project_id="project-a",
        product_kind="workflow_result",
        idempotency_key="attempt-native-b",
        generation_session_id="generation-native-b",
        canvas_id="canvas-a",
    )
    another_task = manager.create_task_for_project(
        ctx, another["task_type"], 0, scope=another["operation_id"], status="running"
    )
    bind_agent_product_task(
        project_dir=tmp_path,
        operation_id=another["operation_id"],
        task_id=another_task.task_id,
        root_task_id=another_task.task_id,
    )
    operations.bind_agent_product_turn_admission(
        project_dir=tmp_path,
        operation_id=another["operation_id"],
        expected_task_id=another_task.task_id,
        turn_id="turn-b",
        tool_call_id="admit-b",
    )
    with pytest.raises(ValueError, match="active native task"):
        operations.record_agent_product_turn_wait(
            project_dir=tmp_path,
            operation_id=another["operation_id"],
            expected_task_id=another_task.task_id,
            turn_id="turn-b",
            waiting_reason="native_task",
            receipt_ref={**receipt, "id": "missing-task"},
        )


def test_late_delivery_keeps_existing_credit_reconciliation_path(tmp_path):
    operation, manager, ctx = _admitted(tmp_path)
    task_id = _task(manager, ctx, operation).task_id
    manager.fail_task_for_project(
        ctx,
        operation["task_type"],
        0,
        scope=operation["operation_id"],
        expected_task_id=task_id,
        error="awaiting late result",
        metadata={
            "error_code": "AGENT_PRODUCT_SETTLEMENT_PENDING",
            "feature_credit_reservation_id": "reservation-a",
        },
    )
    bind_agent_product_model_execution(
        project_dir=tmp_path,
        operation_id=operation["operation_id"],
        model_call_id="agent-turn:turn-a:tool:result",
        executed_at=1.0,
        source="server_observed_agent_turn",
        turn_id="turn-a",
        tool_call_id="result",
    )
    finish_agent_product_operation(
        project_dir=tmp_path,
        operation_id=operation["operation_id"],
        outcome="delivered",
        expected_task_id=task_id,
        result_ref={"kind": "workflow_draft", "id": "draft-a"},
    )
    settled = operations.settle_agent_product_turn(
        project_dir=tmp_path, turn_id="turn-a", outcome="completed"
    )
    assert settled[0]["status"] == "delivered"
    assert _task(manager, ctx, operation).status == "failed"
    assert (
        _task(manager, ctx, operation).metadata["error_code"]
        == "AGENT_PRODUCT_SETTLEMENT_PENDING"
    )


def test_failed_turn_with_credit_reservation_marks_credit_reconciliation(tmp_path):
    operation, manager, ctx = _admitted(tmp_path)
    task_id = _task(manager, ctx, operation).task_id
    manager.update_progress_for_project(
        ctx,
        operation["task_type"],
        0,
        scope=operation["operation_id"],
        expected_task_id=task_id,
        metadata={"feature_credit_reservation_id": "reservation-a"},
    )
    operations.settle_agent_product_turn(
        project_dir=tmp_path, turn_id="turn-a", outcome="failed"
    )
    task = _task(manager, ctx, operation)
    assert task.status == "failed"
    assert task.metadata["settlement_status"] == "awaiting_credit_reconciliation"


def test_crash_recovery_closes_native_wait_after_native_task_fails(tmp_path):
    operation, manager, ctx = _admitted(tmp_path)
    waiter_task_id = _task(manager, ctx, operation).task_id
    native = manager.create_task_for_project(
        ctx, "freezone_image_gen", 0, scope="native-a", status="running"
    )
    operations.record_agent_product_turn_wait(
        project_dir=tmp_path,
        operation_id=operation["operation_id"],
        expected_task_id=waiter_task_id,
        turn_id="turn-a",
        waiting_reason="native_task",
        receipt_ref={
            "kind": "project_task",
            "id": native.task_id,
            "task_type": native.task_type,
            "scope": native.scope,
        },
    )
    manager.fail_task_for_project(
        ctx,
        native.task_type,
        0,
        scope=native.scope,
        expected_task_id=native.task_id,
        error="provider failed",
    )
    recovered = operations.reconcile_expired_agent_product_turns(
        project_dir=tmp_path, expired_turn_ids=["turn-a"]
    )
    assert recovered[0]["status"] == "failed"
    assert _task(manager, ctx, operation).status == "failed"
