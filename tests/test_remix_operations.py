from pathlib import Path

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


def test_missing_output_file_invalidates_completed_reuse(tmp_path: Path) -> None:
    store = RemixOperationStore(tmp_path, "demo")
    output = tmp_path / "output.mp4"
    output.write_bytes(b"video")
    operation = store.start(idempotency_key="generate-a", input_refs=refs(), producer_version="recipe.v1", normalized_parameters={}, node_revision=1)
    assert store.complete(operation["operation_id"], output_refs=[{"project_path": "output.mp4"}], current_input_refs=refs(), current_node_revision=1)["status"] == "completed"
    output.unlink()

    retried = store.start(idempotency_key="generate-a", input_refs=refs(), producer_version="recipe.v1", normalized_parameters={}, node_revision=1)

    assert retried["operation_id"] != operation["operation_id"]


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
