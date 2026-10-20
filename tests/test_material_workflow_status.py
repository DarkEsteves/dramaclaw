from novelvideo.freezone.remix.material_work import material_lifecycle_status


def _work(*statuses):
    return {
        "steps": [
            {
                "id": f"clip-{index}",
                "source": "generate",
                "media_kind": "video",
                "status": status,
                **({"candidates": [{"id": f"candidate-{index}"}]} if status in {"candidate", "accepted"} else {}),
                **({"accepted_resource": {"id": f"resource-{index}"}} if status == "accepted" else {}),
            }
            for index, status in enumerate(statuses)
        ]
    }


def test_saved_work_and_ready_draft_are_not_canvas_delivery_or_generation():
    state = material_lifecycle_status(_work("planned"), workflow_draft={"status": "ready"})

    assert state == {
        "material_work_status": "saved",
        "workflow_draft_status": "ready",
        "canvas_delivery_status": "awaiting_confirmation",
        "generation_status": "not_started",
    }


def test_delivery_receipt_is_required_before_canvas_is_delivered():
    state = material_lifecycle_status(
        _work("submitted"),
        workflow_draft={"status": "confirmed"},
        delivery_receipts=[{"graph_state": "delivered"}],
    )

    assert state["canvas_delivery_status"] == "delivered"
    assert state["generation_status"] == "submitted"


def test_generation_completion_requires_candidates_for_every_generated_step():
    partial = material_lifecycle_status(
        _work("candidate", "submitted"),
        workflow_draft={"status": "confirmed"},
        delivery_receipts=[{"graph_state": "delivered"}],
    )
    complete = material_lifecycle_status(
        _work("candidate", "accepted"),
        workflow_draft={"status": "confirmed"},
        delivery_receipts=[{"graph_state": "delivered"}],
    )

    assert partial["generation_status"] == "candidate_ready"
    assert complete["generation_status"] == "completed"


def test_operation_delivery_without_graph_receipt_does_not_claim_canvas_delivery():
    state = material_lifecycle_status(
        _work("submitted"),
        workflow_draft={"status": "submitted"},
        delivery_receipts=[],
    )

    assert state["canvas_delivery_status"] == "pending"
    assert state["generation_status"] == "submitted"
