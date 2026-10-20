from types import SimpleNamespace

import pytest

from novelvideo.chat import live_events
from novelvideo.chat.store import ChatScope


@pytest.mark.asyncio
async def test_reference_events_only_reach_the_current_actor_canvas_and_agent():
    received = [[], [], [], [], []]
    sockets = [SimpleNamespace(send_json=None) for _ in received]
    # WebSocket objects are identity-hashable; retain this property in the fake.
    class Socket:
        def __init__(self, sink):
            self.sink = sink
        async def send_json(self, payload):
            self.sink.append(payload)
    sockets = [Socket(sink) for sink in received]
    scopes = [ChatScope("project", "p", "freezone", "a", "main"),
              ChatScope("project", "p", "freezone", "b", "main"),
              ChatScope("project", "p", "freezone", "a", "other"),
              ChatScope("project", "p", "director"),
              ChatScope("project", "p", "freezone", "a", "main")]
    try:
        for index, socket in enumerate(sockets):
            await live_events.register_chat_websocket(socket, username="other" if index == 4 else "actor", scope=scopes[index])
        assert hasattr(live_events, "broadcast_chat_scope_event"), "freezone completion events require scoped fan-out"
        await live_events.broadcast_chat_scope_event(username="actor", scope=scopes[0], payload={"type": "reference.remix.status"})
        assert received == [[{"type": "reference.remix.status"}], [], [], [], []]
        await live_events.broadcast_project_chat_event(username="actor", project_id="p", payload={"type": "director.auto.status"})
        assert received[3] == [{"type": "director.auto.status"}]
    finally:
        for socket in sockets:
            await live_events.unregister_chat_websocket(socket)
