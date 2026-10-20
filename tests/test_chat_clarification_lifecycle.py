import pytest


@pytest.mark.anyio
async def test_pending_clarification_result_is_not_persisted_as_submitted(monkeypatch):
    from novelvideo.api.routes import chat

    payload = chat.ClarificationToolResultIn(
        bridge_key="generation-card",
        project_id="project",
        canvas_id="canvas",
        agent_id="main",
        turn_id="turn",
        tool_call_status="completed",
        clarification_status="pending_user_input",
        ok=True,
        action="submit",
        answers={},
    )
    events = []

    async def append(_username, _scope, _turn_id, event):
        events.append(event)

    async def project_context(*_args):
        return None

    monkeypatch.setattr(chat, "_project_context_for_interaction_result", project_context)
    monkeypatch.setattr(chat.chat_store, "append_ui_event_async", append)

    await chat._persist_clarification_result_ui_event(
        user={"username": "actor"}, username="actor", payload=payload,
    )

    assert events == [{
        "type": "assistant.clarification.request",
        "bridge_key": "generation-card",
        "project_id": "project",
        "canvas_id": "canvas",
        "agent_id": "main",
        "submitted": False,
        "action": "submit",
        "clarification_status": "pending_user_input",
        "answers": {},
        "skipped": False,
        "used_recommended": False,
        "anchor_text_prefix": None,
    }]
