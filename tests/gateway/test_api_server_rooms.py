"""``GET /api/rooms`` and ``GET /api/rooms/{room_id}/log`` — the read-only room surface.

The contract these tests pin (Platform design authority, 2026-10-24):

  * the listing is a projection of the hosted-room STORE, so it survives a generational
    handover of the room worker (it must not read the in-process service);
  * ``worker`` is three-state and derived from the lease row's *renewal age*, never from
    ``expires_at > now`` (that paints a healthy worker dead inside its renewal gap) and never
    from ``process_generation`` (a fencing token, not a heartbeat);
  * disbanded rooms are filtered server-side;
  * nothing on this surface mutates: no route here can create or disband a room.
"""

from __future__ import annotations

import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway import hosted_room_driver as driver
from gateway import hosted_rooms as rooms
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter, cors_middleware

GATEWAY_ID = "gw-rooms-test"
PROCESS = "proc-rooms-test"
BASE_NOW = 1_800_000_000.0

_SEATS = [
    {"handle": "platform-worker", "member_id": "default", "profile": "default"},
    {"handle": "financially-coder", "member_id": "financially-coder", "profile": "financially-coder"},
]


def _adapter(api_key: str = "") -> APIServerAdapter:
    extra = {"key": api_key} if api_key else {}
    return APIServerAdapter(PlatformConfig(enabled=True, extra=extra))


def _app(adapter: APIServerAdapter) -> web.Application:
    """Register exactly the two room routes, so a failure here is a route failure."""
    app = web.Application(middlewares=[cors_middleware])
    app["api_server_adapter"] = adapter
    app.router.add_get("/api/rooms", adapter._handle_rooms_listing)
    app.router.add_get("/api/rooms/{room_id}/log", adapter._handle_room_log)
    return app


def _room(db_path, room_id: str = "room-live", name: str = "Financially Team") -> None:
    rooms.create_room(db_path, room_id=room_id, name=name, members=_SEATS,
                      authority_gateway_id=GATEWAY_ID, now=BASE_NOW)


def _lease(db_path, room_id: str = "room-live", *, age_s: float, ttl: int = 30,
           released: bool = False):
    """Acquire a lease as if it had last renewed ``age_s`` seconds ago."""
    at = time.time() - age_s
    lease = driver.acquire_lease(
        db_path, room_id=room_id, gateway_id=GATEWAY_ID, authority_epoch=1,
        process_generation=PROCESS, ttl_seconds=ttl, clock=lambda: at)
    if released:
        driver.release_lease(db_path, lease, clock=lambda: at)
    return lease


def _message(db_path, room_id: str = "room-live", *, seq_label: str = "one", text: str = "hello"):
    return rooms.append_event(
        db_path, room_id=room_id, event_id=f"evt-{seq_label}", kind="message.user",
        actor={"kind": "user", "id": "platform-coder", "profile": "platform-coder"},
        payload={"text": text}, authority_gateway_id=GATEWAY_ID, authority_epoch=1,
        now=BASE_NOW + 10)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A real hosted-room store (real schema, real writes) behind the route."""
    db_path = tmp_path / "shared-state.db"
    _room(db_path)
    monkeypatch.setattr(rooms, "default_db_path", lambda: db_path)
    return db_path


class TestRoomsListing:
    pytestmark = pytest.mark.asyncio

    async def test_lists_live_room_with_seats_seq_and_event_cursor(self, store):
        _lease(store, age_s=3.0)
        _message(store)
        async with TestClient(TestServer(_app(_adapter()))) as cli:
            resp = await cli.get("/api/rooms")
            assert resp.status == 200
            body = await resp.json()
        assert body["object"] == "list"
        assert body["has_more"] is False
        assert isinstance(body["now"], float)
        [room] = [r for r in body["data"] if r["room_id"] == "room-live"]
        assert room["name"] == "Financially Team"
        # handle is a display label; dispatch keys on member_id -> "platform-worker" is "default"
        assert room["seats"][0] == {"handle": "platform-worker", "member_id": "default", "profile": "default"}
        assert [s["profile"] for s in room["seats"]] == ["default", "financially-coder"]
        assert room["seq"] == 1
        assert room["last_event_at"] == pytest.approx(BASE_NOW + 10)
        assert room["worker"] == "live" and room["worker_live"] is True
        assert room["lease_age_s"] < 60

    async def test_disbanded_room_is_filtered_server_side(self, store):
        _room(store, room_id="room-gone", name="Gone")
        rooms.disband_room(store, room_id="room-gone", expected_gateway_id=GATEWAY_ID,
                           expected_epoch=1, now=BASE_NOW + 20)
        async with TestClient(TestServer(_app(_adapter()))) as cli:
            body = await (await cli.get("/api/rooms")).json()
        ids = [r["room_id"] for r in body["data"]]
        assert ids == ["room-live"]
        assert "room-gone" not in ids

    async def test_empty_store_reports_no_rooms(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rooms, "default_db_path", lambda: tmp_path / "absent.db")
        async with TestClient(TestServer(_app(_adapter()))) as cli:
            resp = await cli.get("/api/rooms")
            assert resp.status == 200
            body = await resp.json()
        assert body["data"] == [] and body["has_more"] is False


class TestWorkerLiveness:
    pytestmark = pytest.mark.asyncio

    async def test_renewal_age_within_60s_is_live(self, store):
        _lease(store, age_s=3.5)
        async with TestClient(TestServer(_app(_adapter()))) as cli:
            [room] = (await (await cli.get("/api/rooms")).json())["data"]
        assert (room["worker"], room["worker_live"]) == ("live", True)
        assert 3.0 <= room["lease_age_s"] <= 5.0

    async def test_expired_lease_with_fresh_renewal_is_still_live(self, store):
        """The counter-example to a TTL read: ttl -4.2s while 34.2s into a normal renewal."""
        _lease(store, age_s=34.2, ttl=30)
        async with TestClient(TestServer(_app(_adapter()))) as cli:
            [room] = (await (await cli.get("/api/rooms")).json())["data"]
        assert room["lease_expires_in_s"] < 0
        assert (room["worker"], room["worker_live"]) == ("live", True)

    async def test_renewal_between_60_and_180s_is_stale(self, store):
        _lease(store, age_s=90.0)
        async with TestClient(TestServer(_app(_adapter()))) as cli:
            [room] = (await (await cli.get("/api/rooms")).json())["data"]
        assert (room["worker"], room["worker_live"]) == ("stale", False)

    async def test_renewal_beyond_180s_is_down(self, store):
        _lease(store, age_s=200.0)
        async with TestClient(TestServer(_app(_adapter()))) as cli:
            [room] = (await (await cli.get("/api/rooms")).json())["data"]
        assert (room["worker"], room["worker_live"]) == ("down", False)

    async def test_previous_generation_lease_is_not_a_liveness_signal(self, store):
        """A stale row from a dead generation reads down on age alone — generation is ignored."""
        driver.acquire_lease(store, room_id="room-live", gateway_id=GATEWAY_ID, authority_epoch=1,
                             process_generation="dead-generation", ttl_seconds=30,
                             clock=lambda: time.time() - 4000)
        async with TestClient(TestServer(_app(_adapter()))) as cli:
            [room] = (await (await cli.get("/api/rooms")).json())["data"]
        assert room["worker"] == "down"
        assert room["lease_age_s"] > 180

    async def test_released_lease_is_down_even_when_recent(self, store):
        _lease(store, age_s=2.0, released=True)
        async with TestClient(TestServer(_app(_adapter()))) as cli:
            [room] = (await (await cli.get("/api/rooms")).json())["data"]
        assert (room["worker"], room["worker_live"]) == ("down", False)

    async def test_room_without_a_lease_row_reports_down(self, store):
        async with TestClient(TestServer(_app(_adapter()))) as cli:
            [room] = (await (await cli.get("/api/rooms")).json())["data"]
        assert room["worker"] == "down"
        assert room["lease_age_s"] is None and room["lease_expires_in_s"] is None


class TestRoomLogDelta:
    pytestmark = pytest.mark.asyncio

    async def test_since_zero_returns_events_and_cursor(self, store):
        _message(store, seq_label="one")
        _message(store, seq_label="two", text="world")
        async with TestClient(TestServer(_app(_adapter()))) as cli:
            resp = await cli.get("/api/rooms/room-live/log")
            assert resp.status == 200
            body = await resp.json()
        assert body["object"] == "list"
        assert [e["seq"] for e in body["events"]] == [1, 2]
        assert body["cursor"] == 2 and body["latest_seq"] == 2 and body["has_more"] is False
        assert body["authority"]["gateway_id"] == GATEWAY_ID

    async def test_since_returns_only_the_delta(self, store):
        _message(store, seq_label="one")
        _message(store, seq_label="two", text="world")
        async with TestClient(TestServer(_app(_adapter()))) as cli:
            body = await (await cli.get("/api/rooms/room-live/log?since=1")).json()
        assert [e["seq"] for e in body["events"]] == [2]
        assert body["cursor"] == 2

    async def test_disbanded_room_log_is_404(self, store):
        _room(store, room_id="room-gone", name="Gone")
        rooms.disband_room(store, room_id="room-gone", expected_gateway_id=GATEWAY_ID,
                           expected_epoch=1, now=BASE_NOW + 20)
        async with TestClient(TestServer(_app(_adapter()))) as cli:
            resp = await cli.get("/api/rooms/room-gone/log")
            assert resp.status == 404
            assert "not found" in (await resp.json())["error"]["message"]

    async def test_unknown_room_is_404(self, store):
        async with TestClient(TestServer(_app(_adapter()))) as cli:
            assert (await cli.get("/api/rooms/room-nope/log")).status == 404

    async def test_bad_since_is_400(self, store):
        async with TestClient(TestServer(_app(_adapter()))) as cli:
            assert (await cli.get("/api/rooms/room-live/log?since=abc")).status == 400


class TestSurface:

    @pytest.mark.asyncio
    async def test_routes_require_auth(self):
        async with TestClient(TestServer(_app(_adapter(api_key="sk-rooms")))) as cli:
            assert (await cli.get("/api/rooms")).status == 401
            assert (await cli.get("/api/rooms/room-live/log")).status == 401

    def test_routes_are_in_the_adapter_route_table(self):
        """Both routes must ride the real table, not a test-only app."""
        table = {(method, path) for method, path, _ in _adapter()._http_route_table()}
        assert ("GET", "/api/rooms") in table
        assert ("GET", "/api/rooms/{room_id}/log") in table

    def test_module_exposes_no_mutating_route(self):
        routes = {(method, path) for method, path, _ in _module_routes(_adapter())}
        assert all(method == "GET" for method, _ in routes)


def _module_routes(adapter):
    from gateway.platforms import api_server_rooms

    return api_server_rooms._http_routes(adapter)
