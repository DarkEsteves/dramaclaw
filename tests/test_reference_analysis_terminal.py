from novelvideo.chat import hermes_sdk
from novelvideo.chat.runtime_port import ChatBackendEvent


def submission(event):
    handler = getattr(hermes_sdk, "_reference_analysis_submission", None)
    assert handler is not None, "reference analysis terminal receipt handler missing"
    return handler(event)


def test_submitted_reference_analysis_ends_turn_with_identity():
    event = ChatBackendEvent(
        type="tool_updated",
        status="completed",
        name="freezone_start_reference_analysis",
        structured={
            "ok": True,
            "submitted": True,
            "run_id": "run",
            "analysis_node_id": "analysis",
        },
    )
    assert submission(event) == event.structured


def test_failed_or_other_tool_does_not_stop_turn():
    for name, result in [
        ("freezone_start_reference_analysis", {"ok": False, "submitted": True}),
        ("freezone_start_reference_analysis", {"ok": True, "submitted": True}),
        (
            "freezone_run_node_action",
            {
                "ok": True,
                "submitted": True,
                "run_id": "run",
                "analysis_node_id": "analysis",
            },
        ),
    ]:
        assert (
            submission(
                ChatBackendEvent(type="tool_updated", name=name, structured=result)
            )
            is None
        )


def test_in_progress_receipt_cannot_end_current_turn():
    event = ChatBackendEvent(
        type="tool_updated",
        status="in_progress",
        name="freezone_start_reference_analysis",
        structured={
            "ok": True,
            "submitted": True,
            "run_id": "old",
            "analysis_node_id": "old",
        },
    )
    assert submission(event) is None


def test_cached_receipt_from_another_request_cannot_end_turn():
    event = ChatBackendEvent(
        type="tool_updated",
        status="completed",
        name="freezone_start_reference_analysis",
        input={"source_node_id": "source", "idempotency_key": "new"},
        structured={
            "ok": True,
            "submitted": True,
            "run_id": "old",
            "analysis_node_id": "old",
            "idempotency_key": "old",
            "run": {"source_node_id": "source"},
        },
    )
    assert submission(event) is None


def test_native_router_receipt_is_translated_to_reference_tool(monkeypatch):
    from pathlib import Path

    thread = hermes_sdk.HermesSdkThread(
        cli_path=Path("hermes"),
        cwd=Path("."),
        env={},
        model=None,
        username="user",
        session_id="session",
    )
    receipt = {
        "ok": True,
        "status": "reference_analysis_submitted",
        "submitted": True,
        "run_id": "run",
        "analysis_node_id": "analysis",
        "idempotency_key": "request",
        "run": {"source_node_id": "source"},
    }
    monkeypatch.setattr(
        hermes_sdk,
        "_load_recent_freezone_tool_result",
        lambda *args, **kwargs: (
            receipt if args[1] == "freezone_start_reference_analysis" else None
        ),
    )

    def frame(update):
        return {"method": "session/update", "params": {"update": update}}

    started = thread._translate_notification(
        frame(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "call",
                "title": "tool_call",
                "rawInput": {
                    "name": "freezone_start_reference_analysis",
                    "arguments": {
                        "source_node_id": "source",
                        "idempotency_key": "request",
                    },
                },
            }
        ),
        "turn",
    )
    completed = thread._translate_notification(
        frame(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "call",
                "status": "completed",
            }
        ),
        "turn",
    )
    assert started.name == "freezone_start_reference_analysis"
    assert completed.input == {"source_node_id": "source", "idempotency_key": "request"}
    assert submission(completed) == receipt
    assert completed.lifecycle_only is False
