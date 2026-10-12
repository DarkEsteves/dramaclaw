from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import fakeredis.aioredis
import pytest

from novelvideo.piko.distributed import CLIENT_STALE_SECONDS, DistributedPikoWorldHub
from novelvideo.piko.world import PikoCharacter


class FakeStore:
    def __init__(self) -> None:
        self.locations: list[tuple[str, str, float, float, str]] = []

    async def update_location(
        self, user_id: str, *, scene_id: str, x: float, y: float, facing: str
    ) -> None:
        self.locations.append((user_id, scene_id, x, y, facing))

    async def open_conversation(self, first_id: str, second_id: str) -> str:
        return "00000000-0000-0000-0000-000000000001"

    async def append_message(
        self, conversation_id: str, sender_id: str, body: str
    ) -> dict[str, str]:
        return {
            "id": "00000000-0000-0000-0000-000000000002",
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }


class FakeWebSocket:
    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.close_code: int | None = None

    async def send_json(self, payload: dict) -> None:
        self.frames.append(payload)

    async def close(self, code: int) -> None:
        self.close_code = code


def character(number: int) -> PikoCharacter:
    now = datetime.now(timezone.utc).isoformat()
    return PikoCharacter(
        id=f"00000000-0000-0000-0000-{number:012d}",
        user_id=f"user-{number}",
        nickname=f"玩家{number}",
        gender="female" if number % 2 else "male",
        bio="",
        scene_id="welcome-courtyard",
        position_x=1270,
        position_y=480,
        facing="south",
        created_at=now,
        updated_at=now,
    )


async def wait_for_frame(socket: FakeWebSocket, event_type: str) -> dict:
    for _ in range(100):
        for frame in socket.frames:
            if frame.get("type") == event_type:
                return frame
        await asyncio.sleep(0.01)
    raise AssertionError(f"missing frame {event_type}: {socket.frames!r}")


@pytest.mark.asyncio
async def test_players_and_chat_cross_realtime_server_processes() -> None:
    server = fakeredis.FakeServer()
    store = FakeStore()
    first_hub = DistributedPikoWorldHub(store, redis_url="redis://unused")  # type: ignore[arg-type]
    second_hub = DistributedPikoWorldHub(store, redis_url="redis://unused")  # type: ignore[arg-type]
    first_hub.redis = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    second_hub.redis = fakeredis.aioredis.FakeRedis(
        server=server, decode_responses=True
    )
    first_socket, second_socket = FakeWebSocket(), FakeWebSocket()

    try:
        first = await first_hub.join(
            first_socket, user_id="user-1", character=character(1)  # type: ignore[arg-type]
        )
        await second_hub.join(
            second_socket, user_id="user-2", character=character(2)  # type: ignore[arg-type]
        )
        joined = await wait_for_frame(first_socket, "player.joined")
        assert joined["player"]["nickname"] == "玩家2"

        await first_hub.handle(first, {"type": "chat.send", "body": "跨进程你好"})
        first_message = await wait_for_frame(first_socket, "chat.message")
        second_message = await wait_for_frame(second_socket, "chat.message")
        assert first_message == second_message
        assert second_message["message"]["body"] == "跨进程你好"
    finally:
        await first_hub.close()
        await second_hub.close()


@pytest.mark.asyncio
async def test_scene_change_broadcasts_and_persists_new_facing() -> None:
    server = fakeredis.FakeServer()
    store = FakeStore()
    first_hub = DistributedPikoWorldHub(store, redis_url="redis://unused")  # type: ignore[arg-type]
    second_hub = DistributedPikoWorldHub(store, redis_url="redis://unused")  # type: ignore[arg-type]
    first_hub.redis = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    second_hub.redis = fakeredis.aioredis.FakeRedis(
        server=server, decode_responses=True
    )
    first_socket, second_socket = FakeWebSocket(), FakeWebSocket()

    try:
        first = await first_hub.join(
            first_socket, user_id="user-1", character=character(1)  # type: ignore[arg-type]
        )
        observer = replace(character(2), scene_id="artisan-market")
        await second_hub.join(
            second_socket, user_id="user-2", character=observer  # type: ignore[arg-type]
        )
        second_socket.frames.clear()

        await first_hub.handle(
            first,
            {
                "type": "scene.join",
                "scene_id": "artisan-market",
                "x": 111,
                "y": 222,
                "facing": "west",
            },
        )

        joined = await wait_for_frame(second_socket, "player.joined")
        assert joined["player"]["facing"] == "west"
        assert store.locations[-1] == ("user-1", "artisan-market", 111, 222, "west")
    finally:
        await first_hub.close()
        await second_hub.close()


@pytest.mark.asyncio
async def test_stale_browser_is_removed_and_broadcasts_player_left() -> None:
    server = fakeredis.FakeServer()
    store = FakeStore()
    first_hub = DistributedPikoWorldHub(store, redis_url="redis://unused")  # type: ignore[arg-type]
    second_hub = DistributedPikoWorldHub(store, redis_url="redis://unused")  # type: ignore[arg-type]
    first_hub.redis = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    second_hub.redis = fakeredis.aioredis.FakeRedis(
        server=server, decode_responses=True
    )
    stale_socket, observer_socket = FakeWebSocket(), FakeWebSocket()

    try:
        stale = await first_hub.join(
            stale_socket, user_id="user-1", character=character(1)  # type: ignore[arg-type]
        )
        await second_hub.join(
            observer_socket, user_id="user-2", character=character(2)  # type: ignore[arg-type]
        )
        observer_socket.frames.clear()
        stale.last_seen_at -= CLIENT_STALE_SECONDS + 1

        await first_hub._heartbeat_once()

        left = await wait_for_frame(observer_socket, "player.left")
        assert left["character_id"] == stale.character_id
        assert stale_socket.close_code == 1001
        assert (
            await first_hub.redis.get(first_hub._player_key(stale.character_id)) is None
        )
    finally:
        await first_hub.close()
        await second_hub.close()


@pytest.mark.asyncio
async def test_concurrent_logins_keep_one_owner_and_stale_heartbeats_cannot_reclaim() -> None:
    server = fakeredis.FakeServer()
    hubs = [DistributedPikoWorldHub(FakeStore(), redis_url="redis://unused") for _ in range(2)]
    for hub in hubs:
        hub.redis = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    sockets = [FakeWebSocket(), FakeWebSocket()]
    try:
        players = await asyncio.gather(*(
            hub.join(socket, user_id="user-1", character=character(1))
            for hub, socket in zip(hubs, sockets)
        ))
        route = hubs[0]._route_key(character(1).id)
        owner = await hubs[0].redis.get(route)
        winner = next(index for index, player in enumerate(players) if player.connection_token == owner)
        loser = 1 - winner
        assert not await hubs[loser]._refresh_player(players[loser])
        assert await hubs[0].redis.get(route) == owner
        await hubs[loser]._heartbeat_once()
        await hubs[winner]._heartbeat_once()
        assert sockets[loser].close_code == 4001
        assert sockets[winner].close_code is None
        assert await hubs[0].redis.get(route) == owner
    finally:
        for hub in hubs:
            await hub.close()


@pytest.mark.asyncio
async def test_expired_presence_closes_with_a_reconnectable_code() -> None:
    hub = DistributedPikoWorldHub(FakeStore(), redis_url="redis://unused")
    hub.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    socket = FakeWebSocket()
    try:
        player = await hub.join(socket, user_id="user-1", character=character(1))
        await hub.redis.delete(hub._route_key(player.character_id))
        await hub._heartbeat_once()
        assert socket.close_code == 1001
        assert socket not in hub._players
    finally:
        await hub.close()


@pytest.mark.asyncio
async def test_delayed_replacement_event_cannot_close_the_new_connection() -> None:
    hub = DistributedPikoWorldHub(FakeStore(), redis_url="redis://unused")
    hub.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    old_socket, new_socket = FakeWebSocket(), FakeWebSocket()
    try:
        old = await hub.join(old_socket, user_id="user-1", character=character(1))
        current = await hub.join(new_socket, user_id="user-1", character=character(1))
        await hub._publish(
            "direct", {"type": "session.replaced"},
            target_character_id=old.character_id,
            target_instance=hub.instance_id,
            target_connection_token=old.connection_token,
        )
        await hub._publish("direct", {"type": "marker"}, target_character_id=current.character_id)
        await wait_for_frame(new_socket, "marker")
        assert new_socket.close_code is None
        assert await hub.redis.get(hub._route_key(current.character_id)) == current.connection_token
    finally:
        await hub.close()
