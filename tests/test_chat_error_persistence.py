from __future__ import annotations

import pytest

from novelvideo.api.routes import chat as chat_routes
from novelvideo.chat.hermes_fork_requirement import HermesForkMissing
from novelvideo.chat.store import ChatScope, chat_store


def test_reasoning_required_error_has_actionable_chinese_message() -> None:
    exc = RuntimeError(
        "Reasoning is mandatory for this endpoint and cannot be disabled. "
        '({"provider_name":null})'
    )

    message = chat_routes._user_facing_chat_error(exc)

    assert "上游模型要求启用推理" in message
    assert "NewAPI" in message
    assert "provider_name" not in message


def test_hermes_fork_mismatch_is_not_reported_as_a_network_error() -> None:
    exc = HermesForkMissing(
        "the installed Hermes cannot carry a per-turn credential: "
        "/private/runtime/hermes. Every turn would report a connection error. "
        "API_KEY=secret-placeholder"
    )

    message = chat_routes._user_facing_chat_error(exc)

    assert "Hermes 运行环境不匹配" in message
    assert "配套版本" in message
    assert "模型连接失败" not in message
    assert "/private/runtime/hermes" not in message
    assert "secret-placeholder" not in message


@pytest.mark.parametrize("reason", ["connection error", "Connection refused"])
def test_real_connection_errors_keep_the_network_message(reason: str) -> None:
    message = chat_routes._user_facing_chat_error(RuntimeError(reason))

    assert message == "模型连接失败：当前无法连接上游模型服务，请检查服务状态后重试。"


@pytest.mark.asyncio
async def test_home_turn_error_is_persisted_once(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NOVELVIDEO_STATE_DIR", str(tmp_path / "state"))
    scope = ChatScope(kind="home")
    kwargs = {
        "user": {"username": "alice"},
        "username": "alice",
        "scope": scope,
        "turn_id": "turn-failed-1",
        "reason": "模型连接失败",
    }

    first = await chat_routes._persist_chat_turn_error(**kwargs)
    second = await chat_routes._persist_chat_turn_error(**kwargs)
    history = chat_store.list_messages("alice", scope)

    assert first is not None
    assert second is not None
    assert first["id"] == second["id"]
    assert len(history) == 1
    assert history[0]["turn_id"] == "turn-failed-1"
    assert history[0]["chat_error"] is True
    assert history[0]["content"] == (
        "本轮处理失败：模型连接失败\n\n请根据错误提示处理后重试。"
    )
