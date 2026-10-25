"""Read-only room surface for the gateway API: ``GET /api/rooms`` + ``GET /api/rooms/{id}/log``.

The desktop's Group Chat shelf needs to enumerate the hosted rooms the gateway is actually
running. It must NOT learn them from a client-side store (that is how a second writer — and a
duplicate room — gets created) and it must not be served by the in-process ``HostedRoomService``
either: that service is movable (``web_server`` starts it for dashboard mode,
``GatewayRunner._ensure_hosted_room_worker`` supervises it in the gateway), so a projection
taken from whichever process happens to own the worker goes blank the moment ownership moves
(``hosted_room_driver_leases.process_generation`` names the winner).

So both handlers project the STORE — ``hosted_rooms``, ``hosted_room_events`` and
``hosted_room_driver_leases`` via ``gateway.hosted_rooms.default_db_path()`` — and are served by
whichever process owns the API port. Nothing here mutates: room sends stay on the existing
dispatch surface (``api_server_room_dispatch``), so this route can never create a room.

Contract (signed off by the Platform design authority):

    GET /api/rooms?limit=&offset=
        {"object": "list", "now": <server clock>, "limit", "offset", "has_more",
         "data": [{"room_id", "name",
                   "seats": [{"handle", "member_id", "profile"}],
                   "seq": <latest allocated event seq>,
                   "last_event_at": <epoch seconds|null>,
                   "worker": "live" | "stale" | "down",
                   "worker_live": <worker == "live">,
                   "lease_age_s": <seconds since the lease row last renewed|null>,
                   "lease_expires_in_s": <seconds|null>,
                   "updated_at": <epoch seconds>}]}

    GET /api/rooms/{room_id}/log?since=<seq>&limit=
        {"object": "list", "events": [...], "cursor", "latest_seq", "has_more", "authority"}

Liveness is a lease-renewal age, never a TTL read and never a process-generation guess:

  * ``updated_at`` age <= 60s (2x the 30s renewal) -> ``live``;
  * 60-180s -> ``stale`` (rides out a gateway restart / supervisor cycle without flapping);
  * beyond 180s, no lease row, or a released lease -> ``down``.

A naive ``expires_at > now`` paints a healthy worker dead inside its renewal gap (measured on a
live room: ttl -4.2s while 34.2s into a normal renewal). ``process_generation`` is a fencing
token, not a heartbeat: rows whose generation lags a live worker are stale leftovers, so it is
deliberately absent from the predicate. Disbanded rooms are filtered server-side
(``hosted_rooms.list_rooms`` excludes ``disbanded_at`` rows) so the shelf never renders a
tombstone, and leases are re-read on every request — never cached — because a cached reading
lies in the other direction.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

from gateway import hosted_rooms
from gateway.hosted_rooms import (
    MAX_LOG_LIMIT,
    MAX_ROOM_LIST_LIMIT,
    HostedRoomError,
    RoomNotFoundError,
)

logger = logging.getLogger(__name__)

# 2x the 30s driver renewal, then a 3x window to ride out a restart before declaring the worker down.
LIVE_LEASE_AGE_S = 60.0
STALE_LEASE_AGE_S = 180.0

_DEFAULT_ROOM_PAGE = 50
_DEFAULT_LOG_PAGE = 100


def _http_routes(self: Any) -> list[tuple[str, str, Any]]:
    return [
        ("GET", "/api/rooms", self._handle_rooms_listing),
        ("GET", "/api/rooms/{room_id}/log", self._handle_room_log)]


def _error(message: str, status: int) -> web.Response:
    return web.json_response({"error": {"message": message, "type": "invalid_request_error"}}, status=status)


def _read_only_connection(db_path: Path) -> sqlite3.Connection:
    """Open the shared store WITHOUT a write path: this module must never mutate it."""
    return sqlite3.connect(f"{Path(db_path).as_uri()}?mode=ro", uri=True, timeout=5.0)


def _seat(member: Any) -> dict[str, Any] | None:
    """Project one ``members_json`` entry to the three fields the shelf needs.

    Dispatch keys on ``member_id``/``profile``; ``handle`` is only the display label (the
    platform worker's handle reads "platform-worker" while its member_id is "default").
    """
    if not isinstance(member, dict):
        return None
    handle = str(member.get("handle") or member.get("member_id") or member.get("id") or "")
    member_id = str(member.get("member_id") or member.get("id") or handle)
    profile = str(member.get("profile") or member_id)
    if not member_id:
        return None
    return {"handle": handle or member_id, "member_id": member_id, "profile": profile}


def _worker_state(lease: dict[str, Any] | None, now: float) -> dict[str, Any]:
    """Three-state worker liveness derived from the lease ROW's renewal age."""
    if not lease:
        return {"worker": "down", "worker_live": False, "lease_age_s": None, "lease_expires_in_s": None}
    updated_at = float(lease.get("updated_at") or 0.0)
    age = max(0.0, now - updated_at)  # a future stamp (clock skew) reads as just-renewed, never negative
    if lease.get("released_at") is not None:
        state = "down"   # a clean release is authoritative: the worker let go on purpose
    elif age <= LIVE_LEASE_AGE_S:
        state = "live"
    elif age <= STALE_LEASE_AGE_S:
        state = "stale"
    else:
        state = "down"
    return {
        "worker": state,
        "worker_live": state == "live",
        "lease_age_s": round(age, 1),
        "lease_expires_in_s": round(float(lease.get("expires_at") or 0.0) - now, 1),
    }


def _side_tables(db_path: Path, room_ids: list[str]) -> dict[str, dict[str, Any]]:
    """One read of the lease + last-event columns for this page of rooms."""
    leases: dict[str, dict[str, Any]] = {}
    last_event_at: dict[str, float | None] = {}
    if not room_ids:
        return {"leases": leases, "last_event_at": last_event_at}
    placeholders = ",".join("?" * len(room_ids))
    with closing(_read_only_connection(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        try:
            for row in conn.execute(
                f"""SELECT room_id, updated_at, expires_at, released_at FROM hosted_room_driver_leases
                     WHERE room_id IN ({placeholders})""", room_ids):
                leases[str(row["room_id"])] = {
                    "updated_at": row["updated_at"], "expires_at": row["expires_at"],
                    "released_at": row["released_at"]}
            for row in conn.execute(
                f"""SELECT room_id, MAX(created_at) AS last_event_at FROM hosted_room_events
                     WHERE room_id IN ({placeholders}) GROUP BY room_id""", room_ids):
                stamp = row["last_event_at"]
                last_event_at[str(row["room_id"])] = float(stamp) if stamp is not None else None
        except sqlite3.OperationalError:
            pass  # a store predating these tables reads as "no lease, no events", not an error
    return {"leases": leases, "last_event_at": last_event_at}


def _rooms_payload(*, limit: int, offset: int) -> dict[str, Any]:
    """The whole ``GET /api/rooms`` body — runs on a worker thread, never on the event loop."""
    now = time.time()
    db_path = Path(hosted_rooms.default_db_path())
    if not db_path.exists():
        return {"object": "list", "data": [], "limit": limit, "offset": offset, "has_more": False, "now": now}
    # include_disbanded=False is the server-side tombstone filter: the client never sees a
    # disbanded room, so it can neither flap on nor render one.
    rooms = hosted_rooms.list_rooms(db_path, include_disbanded=False, limit=limit, offset=offset)
    side = _side_tables(db_path, [str(room["room_id"]) for room in rooms])
    data: list[dict[str, Any]] = []
    for room in rooms:
        room_id = str(room["room_id"])
        members = room.get("members")
        seats = [seat for seat in (_seat(m) for m in (members if isinstance(members, list) else [])) if seat]
        data.append({
            "room_id": room_id,
            "name": str(room.get("name") or room_id),
            "seats": seats,
            # latest_seq is the store's own next_seq-1 projection; keep the fallback for older rows.
            "seq": int(room.get("latest_seq", int(room.get("next_seq") or 1) - 1)),
            "last_event_at": side["last_event_at"].get(room_id),
            **_worker_state(side["leases"].get(room_id), now),
            "updated_at": room.get("updated_at"),
        })
    return {
        "object": "list", "data": data, "limit": limit, "offset": offset,
        "has_more": len(rooms) >= limit, "now": now}


async def _handle_rooms_listing(self: Any, request: web.Request,
                                *, _openai_error: Callable[..., web.Response] | None = None,
                                _api_server: Any = None) -> web.Response:
    """``GET /api/rooms`` -- every live hosted room, its seats and its worker's lease state."""
    auth_err = self._check_auth(request)
    if auth_err:
        return auth_err
    limit = self._parse_nonnegative_int(request.query.get("limit"), default=_DEFAULT_ROOM_PAGE,
                                        maximum=MAX_ROOM_LIST_LIMIT) or _DEFAULT_ROOM_PAGE
    offset = self._parse_nonnegative_int(request.query.get("offset"), default=0, maximum=1_000_000)
    try:
        payload = await asyncio.to_thread(_rooms_payload, limit=limit, offset=offset)
    except (HostedRoomError, sqlite3.Error) as exc:
        # Degrade to an empty shelf rather than a 500: a store that cannot be read yet is not a
        # client error, and a broken shelf must never look like "no rooms exist".
        logger.warning("rooms listing unavailable: %s", exc)
        payload = {"object": "list", "data": [], "limit": limit, "offset": offset,
                   "has_more": False, "now": time.time(), "degraded": True}
    return web.json_response(payload)


async def _handle_room_log(self: Any, request: web.Request, *,
                           room_id: str | None = None,
                           _openai_error: Callable[..., web.Response] | None = None,
                           _api_server: Any = None) -> web.Response:
    """``GET /api/rooms/{room_id}/log`` -- the monotonic room-log delta after ``since``."""
    auth_err = self._check_auth(request)
    if auth_err:
        return auth_err
    # aiohttp hands a route handler only the request; the path parameter arrives here.
    room_id = room_id or request.match_info.get("room_id") or ""
    if not room_id:
        return _error("room_id is required", 400)
    raw_since = request.query.get("since") or request.query.get("since_seq") or "0"
    try:
        since_seq = int(raw_since)
    except (TypeError, ValueError):
        return _error("since must be a non-negative integer", 400)
    if since_seq < 0:
        return _error("since must be a non-negative integer", 400)
    limit = self._parse_nonnegative_int(request.query.get("limit"), default=_DEFAULT_LOG_PAGE,
                                        maximum=MAX_LOG_LIMIT) or _DEFAULT_LOG_PAGE
    try:
        page = await asyncio.to_thread(
            hosted_rooms.read_events, hosted_rooms.default_db_path(),
            room_id=room_id, since_seq=since_seq, limit=limit)
    except RoomNotFoundError:
        # Disbanded or unknown: one answer, so a tombstoned room is indistinguishable from a typo.
        return _error("hosted room not found", 404)
    except HostedRoomError as exc:
        return _error(str(exc), 400)
    return web.json_response({"object": "list", **page})
