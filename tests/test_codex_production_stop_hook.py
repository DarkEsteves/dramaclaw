import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("business,expected", [("current", True), ("old", False)])
async def test_production_stop_is_persisted_only_for_the_exact_active_turn(monkeypatch, business, expected):
    from novelvideo.chat import service
    events = []
    monkeypatch.setattr(service, "_ACTIVE_CODEX_TURNS", {("alice", "scope"): ("native", "runtime", "current")})
    monkeypatch.setattr(service, "_load_active_codex_turns", lambda *args: {})
    def interrupt(*args):
        events.append("interrupt")
        return True
    monkeypatch.setattr(service, "interrupt_live_codex_turn", interrupt)
    async def before():
        events.append("persist_stop")
    result = await service.interrupt_active_codex_turn("alice", "scope", business, before_interrupt=before)
    assert result is expected
    assert events == (["persist_stop", "interrupt"] if expected else [])
