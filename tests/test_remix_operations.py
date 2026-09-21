from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from novelvideo.freezone.remix.operations import RemixOperationStore


def refs(revision: int = 1) -> list[dict]:
    return [{"artifact_id": "artifact-a", "revision": revision, "project_id": "demo"}]


def test_retry_with_same_idempotency_key_returns_same_operation(tmp_path: Path) -> None:
    store = RemixOperationStore(tmp_path, "demo")
    first = store.start(idempotency_key="generate-a", input_refs=refs(), producer_version="recipe.v1", normalized_parameters={"size": "720p"}, node_revision=3)
    retried = store.start(idempotency_key="generate-a", input_refs=refs(), producer_version="recipe.v1", normalized_parameters={"size": "720p"}, node_revision=3)

    assert retried["operation_id"] == first["operation_id"]
    assert retried["status"] == "pending"


def test_reusing_key_for_different_request_is_conflict(tmp_path: Path) -> None:
    store = RemixOperationStore(tmp_path, "demo")
    store.start(idempotency_key="same", input_refs=refs(), producer_version="render.v1", normalized_parameters={"quality": "high"}, node_revision=3)

    with pytest.raises(ValueError, match="Idempotency key"):
        store.start(idempotency_key="same", input_refs=refs(2), producer_version="render.v1", normalized_parameters={"quality": "high"}, node_revision=3)


def test_start_exposes_whether_request_claimed_new_operation(tmp_path: Path) -> None:
    store = RemixOperationStore(tmp_path, "demo")
    first = store.start(idempotency_key="same", input_refs=refs(), producer_version="render.v1", normalized_parameters={}, node_revision=3)
    retried = store.start(idempotency_key="same", input_refs=refs(), producer_version="render.v1", normalized_parameters={}, node_revision=3)

    assert first["created"] is True
    assert retried["created"] is False


def test_regenerate_creates_new_operation(tmp_path: Path) -> None:
    store = RemixOperationStore(tmp_path, "demo")
    first = store.start(idempotency_key="generate-a", input_refs=refs(), producer_version="recipe.v1", normalized_parameters={}, node_revision=1)
    again = store.start(idempotency_key="generate-a", input_refs=refs(), producer_version="recipe.v1", normalized_parameters={}, node_revision=1, regenerate=True)

    assert again["operation_id"] != first["operation_id"]


def test_unknown_provider_outcome_stays_pending_reconciliation(tmp_path: Path) -> None:
    store = RemixOperationStore(tmp_path, "demo")
    operation = store.start(idempotency_key="generate-a", input_refs=refs(), producer_version="recipe.v1", normalized_parameters={}, node_revision=1)

    unknown = store.provider_outcome_unknown(operation["operation_id"], provider_task_id="provider-1")

    assert unknown["status"] == "pending_reconciliation"
    assert unknown["provider_task_id"] == "provider-1"


def test_late_result_with_stale_input_or_node_is_not_applied(tmp_path: Path) -> None:
    store = RemixOperationStore(tmp_path, "demo")
    operation = store.start(idempotency_key="generate-a", input_refs=refs(), producer_version="recipe.v1", normalized_parameters={}, node_revision=2)

    stale = store.complete(operation["operation_id"], output_refs=[], current_input_refs=refs(2), current_node_revision=2)

    assert stale["status"] == "stale"
    operation = store.start(idempotency_key="generate-b", input_refs=refs(), producer_version="recipe.v1", normalized_parameters={}, node_revision=2)
    assert store.complete(operation["operation_id"], output_refs=[], current_input_refs=refs(), current_node_revision=3)["status"] == "stale"


def test_missing_output_file_requires_explicit_regenerate_without_new_paid_operation(tmp_path: Path) -> None:
    store = RemixOperationStore(tmp_path, "demo")
    output = tmp_path / "output.mp4"
    output.write_bytes(b"video")
    operation = store.start(idempotency_key="generate-a", input_refs=refs(), producer_version="recipe.v1", normalized_parameters={}, node_revision=1)
    assert store.complete(operation["operation_id"], output_refs=[{"project_path": "output.mp4"}], current_input_refs=refs(), current_node_revision=1)["status"] == "completed"
    output.unlink()

    retried = store.start(idempotency_key="generate-a", input_refs=refs(), producer_version="recipe.v1", normalized_parameters={}, node_revision=1)

    assert retried["operation_id"] == operation["operation_id"]
    assert retried["created"] is False
    assert retried["status"] == "stale"
    assert retried["stale_reason"] == "output_unavailable"
    assert retried["result_available"] is False
    assert retried["output_reusable"] is False
    regenerated = store.start(idempotency_key="generate-a", input_refs=refs(), producer_version="recipe.v1", normalized_parameters={}, node_revision=1, regenerate=True)
    assert regenerated["operation_id"] != operation["operation_id"]
    assert regenerated["created"] is True


def test_cross_project_references_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Cross-project"):
        RemixOperationStore(tmp_path, "demo").start(idempotency_key="generate-a", input_refs=[{"artifact_id": "x", "revision": 1, "project_id": "other"}], producer_version="recipe.v1", normalized_parameters={}, node_revision=1)


def test_runner_result_waits_for_node_confirmation(tmp_path: Path) -> None:
    store = RemixOperationStore(tmp_path, "demo")
    operation = store.start(idempotency_key="render", input_refs=refs(), producer_version="render.v1", normalized_parameters={}, node_revision=3)
    output = tmp_path / "output.mp4"
    output.write_bytes(b"video")

    finished = store.publish_result_unverified(operation["operation_id"], output_refs=[{"project_path": "output.mp4"}])
    retried = store.start(idempotency_key="render", input_refs=refs(), producer_version="render.v1", normalized_parameters={}, node_revision=3)

    assert finished["status"] == "needs_confirmation"
    assert finished["output_refs"] == [{"project_path": "output.mp4"}]
    assert retried["created"] is False
    assert retried["status"] == "needs_confirmation"


def test_reload_reconciles_bound_task_without_replacement(tmp_path: Path) -> None:
    store = RemixOperationStore(tmp_path, "demo")
    operation = store.start(idempotency_key="reload", input_refs=refs(), producer_version="render.v2", normalized_parameters={}, node_revision=4, input_media_hashes=["sha256:" + "a" * 64])
    store.bind_task(operation["operation_id"], task_id="task-1", provider_task_id="provider-1")
    recovered = store.start(idempotency_key="reload", input_refs=refs(), producer_version="render.v2", normalized_parameters={}, node_revision=4, input_media_hashes=["sha256:" + "a" * 64])
    assert recovered["created"] is False and recovered["operation_id"] == operation["operation_id"]
    assert recovered["task_id"] == "task-1" and recovered["provider_task_id"] == "provider-1"


def test_exact_media_hash_is_part_of_reuse_contract(tmp_path: Path) -> None:
    store = RemixOperationStore(tmp_path, "demo")
    store.start(idempotency_key="media", input_refs=refs(), producer_version="render.v2", normalized_parameters={}, node_revision=4, input_media_hashes=["sha256:" + "a" * 64])
    with pytest.raises(ValueError, match="Idempotency key"):
        store.start(idempotency_key="media", input_refs=refs(), producer_version="render.v2", normalized_parameters={}, node_revision=4, input_media_hashes=["sha256:" + "b" * 64])


def test_regenerate_retains_superseded_operation_history(tmp_path: Path) -> None:
    store = RemixOperationStore(tmp_path, "demo")
    first = store.start(idempotency_key="regen", input_refs=refs(), producer_version="render.v2", normalized_parameters={}, node_revision=4)
    second = store.start(idempotency_key="regen", input_refs=refs(), producer_version="render.v2", normalized_parameters={}, node_revision=4, regenerate=True)
    assert store.get(first["operation_id"])["superseded_by"] == second["operation_id"]
    assert second["supersedes"] == first["operation_id"]
    assert {item["operation_id"] for item in store.list()} >= {first["operation_id"], second["operation_id"]}


def test_provider_timeout_and_cancellation_reconcile_before_retry(tmp_path: Path) -> None:
    store = RemixOperationStore(tmp_path, "demo")
    operation = store.start(idempotency_key="provider", input_refs=refs(), producer_version="render.v2", normalized_parameters={}, node_revision=4)
    store.provider_outcome_unknown(operation["operation_id"], provider_task_id="provider-1")
    recovered = store.start(idempotency_key="provider", input_refs=refs(), producer_version="render.v2", normalized_parameters={}, node_revision=4)
    assert recovered["status"] == "pending_reconciliation" and recovered["created"] is False
    cancelled = store.cancel(operation["operation_id"], reason="user_cancelled")
    assert cancelled["status"] == "cancelled" and cancelled["cancel_reason"] == "user_cancelled"


def test_simultaneous_tabs_claim_one_operation(tmp_path: Path) -> None:
    def claim() -> dict:
        return RemixOperationStore(tmp_path, "demo").start(idempotency_key="two-tabs", input_refs=refs(), producer_version="render.v2", normalized_parameters={"quality": "high"}, node_revision=4)
    with ThreadPoolExecutor(max_workers=2) as executor:
        claimed = list(executor.map(lambda _: claim(), range(2)))
    assert len({item["operation_id"] for item in claimed}) == 1
    assert sorted(item["created"] for item in claimed) == [False, True]


def test_unknown_provider_outcome_can_be_reconciled_without_resubmission(tmp_path: Path) -> None:
    store = RemixOperationStore(tmp_path, "demo")
    (tmp_path / "provider.mp4").write_bytes(b"video")
    operation = store.start(idempotency_key="unknown", input_refs=refs(), producer_version="render.v2", normalized_parameters={}, node_revision=4)
    store.provider_outcome_unknown(operation["operation_id"], provider_task_id="provider-1")
    reconciled = store.publish_result_unverified(operation["operation_id"], output_refs=[{"project_path": "provider.mp4"}])
    assert reconciled["status"] == "needs_confirmation" and reconciled["provider_task_id"] == "provider-1"


def test_late_result_for_cancelled_or_superseded_operation_stays_in_history(tmp_path: Path) -> None:
    store = RemixOperationStore(tmp_path, "demo")
    (tmp_path / "late.mp4").write_bytes(b"video")
    cancelled = store.start(idempotency_key="cancel-late", input_refs=refs(), producer_version="render.v2", normalized_parameters={}, node_revision=4)
    store.cancel(cancelled["operation_id"], reason="user_cancelled")
    assert store.publish_result_unverified(cancelled["operation_id"], output_refs=[{"project_path": "late.mp4"}])["status"] == "stale"
    first = store.start(idempotency_key="superseded-late", input_refs=refs(), producer_version="render.v2", normalized_parameters={}, node_revision=4)
    store.start(idempotency_key="superseded-late", input_refs=refs(), producer_version="render.v2", normalized_parameters={}, node_revision=4, regenerate=True)
    late = store.publish_result_unverified(first["operation_id"], output_refs=[{"project_path": "late.mp4"}])
    assert late["status"] == "stale" and late["stale_reason"] == "superseded"
    assert late["output_refs"] == [{"project_path": "late.mp4"}]


def test_submission_claim_is_atomic_and_releasable(tmp_path: Path) -> None:
    store = RemixOperationStore(tmp_path, "demo")
    operation = store.start(idempotency_key="submit", input_refs=refs(), producer_version="render.v2", normalized_parameters={}, node_revision=1)
    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(lambda owner: RemixOperationStore(tmp_path, "demo").claim_submission(operation["operation_id"], owner=owner), ["tab-a", "tab-b"]))
    assert sorted(claims) == [False, True]
    winner = "tab-a" if claims[0] else "tab-b"
    store.release_submission(operation["operation_id"], owner=winner)
    assert store.claim_submission(operation["operation_id"], owner="retry") is True


def test_confirmation_claim_serializes_cancel_and_unknown_outcome(tmp_path: Path) -> None:
    store = RemixOperationStore(tmp_path, "demo")
    (tmp_path / "result.mp4").write_bytes(b"video")
    operation = store.start(idempotency_key="confirm-race", input_refs=refs(), producer_version="render.v2", normalized_parameters={}, node_revision=1)
    store.publish_result_unverified(operation["operation_id"], output_refs=[{"project_path": "result.mp4"}])

    claimed = store.claim_confirmation(operation["operation_id"], canvas_id="canvas", node_id="node")
    assert claimed["status"] == "confirming"
    with pytest.raises(ValueError, match="confirmation"):
        store.cancel(operation["operation_id"], reason="user_cancelled")
    with pytest.raises(ValueError, match="confirmation"):
        store.provider_outcome_unknown(operation["operation_id"], provider_task_id="provider")
    completed = store.finish_confirmation(operation["operation_id"])
    assert completed["status"] == "completed"


def test_terminal_operations_reject_illegal_state_rewrites(tmp_path: Path) -> None:
    store = RemixOperationStore(tmp_path, "demo")
    operation = store.start(idempotency_key="terminal", input_refs=refs(), producer_version="render.v2", normalized_parameters={}, node_revision=1)
    store.cancel(operation["operation_id"], reason="user_cancelled")
    with pytest.raises(ValueError, match="transition"):
        store.fail(operation["operation_id"], reason="too late")
    with pytest.raises(ValueError, match="transition"):
        store.provider_outcome_unknown(operation["operation_id"], provider_task_id="provider")
    with pytest.raises(ValueError, match="transition"):
        store.bind_task(operation["operation_id"], task_id="late-task")
    with pytest.raises(ValueError, match="transition"):
        store.complete(operation["operation_id"], output_refs=[], current_input_refs=refs(), current_node_revision=1)


def test_cancellation_claim_blocks_confirmation_and_late_output_stays_stale(tmp_path: Path) -> None:
    store = RemixOperationStore(tmp_path, "demo")
    (tmp_path / "late.mp4").write_bytes(b"video")
    operation = store.start(idempotency_key="cancel-claim", input_refs=refs(), producer_version="render.v2", normalized_parameters={}, node_revision=1)
    store.claim_cancellation(operation["operation_id"], reason="user_cancelled", owner="test-request")
    with pytest.raises(ValueError, match="not available"):
        store.claim_confirmation(operation["operation_id"], canvas_id="", node_id="")
    late = store.publish_result_unverified(operation["operation_id"], output_refs=[{"project_path": "late.mp4"}])
    assert late["status"] == "stale" and late["stale_reason"] == "cancelled"
    assert store.finish_cancellation(operation["operation_id"])["status"] == "stale"


def test_restart_recovers_confirmation_claim_from_canvas_or_expired_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import novelvideo.freezone.remix.operations as module
    store = RemixOperationStore(tmp_path, "demo")
    (tmp_path / "result.mp4").write_bytes(b"video")
    operation = store.start(idempotency_key="confirm-restart", input_refs=refs(), producer_version="render.v2", normalized_parameters={}, node_revision=1)
    store.publish_result_unverified(operation["operation_id"], output_refs=[{"project_path": "result.mp4"}])
    monkeypatch.setattr(module.time, "time", lambda: 100.0)
    store.claim_confirmation(operation["operation_id"], canvas_id="canvas", node_id="node")
    assert store.recover_confirmation(operation["operation_id"], applied_to_canvas=True, lease_seconds=60)["status"] == "completed"

    second = store.start(idempotency_key="confirm-expired", input_refs=refs(), producer_version="render.v2", normalized_parameters={}, node_revision=1)
    store.publish_result_unverified(second["operation_id"], output_refs=[{"project_path": "result.mp4"}])
    store.claim_confirmation(second["operation_id"], canvas_id="canvas", node_id="node")
    monkeypatch.setattr(module.time, "time", lambda: 161.0)
    assert store.recover_confirmation(second["operation_id"], applied_to_canvas=False, lease_seconds=60)["status"] == "needs_confirmation"


def test_cancel_uncertain_never_releases_to_prior_state(tmp_path: Path) -> None:
    store = RemixOperationStore(tmp_path, "demo")
    operation = store.start(idempotency_key="cancel-unknown", input_refs=refs(), producer_version="render.v2", normalized_parameters={}, node_revision=1)
    store.claim_cancellation(operation["operation_id"], reason="user_cancelled", owner="test-request")
    unknown = store.cancellation_outcome_unknown(operation["operation_id"], reason="backend timeout")
    assert unknown["status"] == "pending_reconciliation"
    assert unknown["reconciliation_reason"] == "backend timeout"


def test_cancel_restart_reconciliation_has_an_expiring_single_owner_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import novelvideo.freezone.remix.operations as module
    store = RemixOperationStore(tmp_path, "demo")
    operation = store.start(idempotency_key="cancel-lease", input_refs=refs(), producer_version="render.v2", normalized_parameters={}, node_revision=1)
    monkeypatch.setattr(module.time, "time", lambda: 100.0)
    store.claim_cancellation(operation["operation_id"], reason="user_cancelled", owner="worker-a")
    assert not store.claim_cancellation_reconciliation(operation["operation_id"], owner="worker-b", lease_seconds=60)
    monkeypatch.setattr(module.time, "time", lambda: 161.0)
    assert store.claim_cancellation_reconciliation(operation["operation_id"], owner="worker-b", lease_seconds=60)


def test_late_result_after_uncertain_cancel_is_retained_only_as_stale_history(tmp_path: Path) -> None:
    store = RemixOperationStore(tmp_path, "demo")
    (tmp_path / "late-after-timeout.mp4").write_bytes(b"video")
    operation = store.start(idempotency_key="cancel-timeout-late", input_refs=refs(), producer_version="render.v2", normalized_parameters={}, node_revision=1)
    store.claim_cancellation(operation["operation_id"], reason="user_cancelled", owner="request-a")
    store.cancellation_outcome_unknown(operation["operation_id"], reason="backend timeout")

    late = store.publish_result_unverified(operation["operation_id"], output_refs=[{"project_path": "late-after-timeout.mp4"}])

    assert late["status"] == "stale"
    assert late["stale_reason"] == "cancelled"
    assert late["output_refs"] == [{"project_path": "late-after-timeout.mp4"}]
    with pytest.raises(ValueError, match="not available"):
        store.claim_confirmation(operation["operation_id"], canvas_id="", node_id="")
