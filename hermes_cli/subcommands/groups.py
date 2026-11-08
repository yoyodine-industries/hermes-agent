"""``hermes groups`` — operate a Hermes Group Chat (hosted room) without a client.

A hosted room is not owned by Desktop, TUI or any WebSocket: its log, its roster and its
authority epoch live in the ROOT install's ``shared-state.db`` (``hosted_rooms.default_db_path()``,
``gateway/hosted_rooms.py:398``) — a coordination DB every process on the host may write. The
*turns* are driven by the hosted-room worker the **gateway** supervises
(``gateway/run_startup.py`` → ``_ensure_hosted_room_worker``), which re-reads the room bindings
on every poll (idle 5 s / active 0.25 s). So a room created from this CLI keeps running after the
operator's laptop, Desktop, and even the dashboard go away.

This verb therefore never speaks JSON-RPC, never starts a worker, never spawns a driver loop and
never takes a lease: it opens :class:`~tui_gateway.hosted_room_service.HostedRoomService` over the
store WITHOUT starting its runtime, and calls the same domain methods the ``groups.*`` RPC
handlers call (``service.create_room`` / ``service.send`` / ``gateway.hosted_rooms.*``). Reusing
the service object is mandatory rather than tidy: ``send`` resolves the room's authority epoch
through ``_owned_authority()`` before appending, so a hand-rolled ``append_event`` could write
under a stale epoch.

Because no worker lives in this process, the ``groups.create``/``groups.send`` RPC "room worker
unavailable" error shape (4123) is meaningless here and is never produced: creating a room on a
host whose gateway worker is down succeeds and simply waits. Check ``hermes groups state`` for
durable state, not for a driver verdict, and use the gateway's own logs if turns do not advance.

Exit codes: 0 success, 2 usage error or a refused/conflicting request, 1 unexpected failure.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import types
from pathlib import Path
from typing import Any

# Actor stamped on a CLI-authored user turn. It is the SAME actor the Desktop RPC path writes
# (``HostedRoomService.send``), so the room log has one shape regardless of which door was used.
CLI_ROOM_ID_PREFIX = "room-"


def _ok(message: str, *, payload: dict[str, Any] | None = None, as_json: bool) -> int:
    if as_json:
        print(json.dumps(payload or {"ok": True, "message": message}, indent=2, sort_keys=True))
    else:
        print(message)
    return 0


def _fail(message: str, *, as_json: bool, payload: dict[str, Any] | None = None) -> int:
    """Report a refused request on stderr (machine notices stay parseable on stdout)."""
    if as_json:
        print(json.dumps({"ok": False, **(payload or {}), "error": message}, indent=2, sort_keys=True))
    print(f"hermes groups: {message}", file=sys.stderr)
    return 2


def _stub_server() -> types.ModuleType:
    """An inert stand-in for the ``tui_gateway`` server module.

    ``HostedRoomService.__init__`` hands the module to ``HostedRoomServerRPC``, which resolves
    session handlers only when a *turn* runs — and this process never runs one (no
    ``service.start()``). An empty ``_methods`` map makes that guarantee explicit: if the CLI ever
    tried to drive a turn it would fail loudly instead of silently borrowing a caller's session.
    """
    module = types.ModuleType("hermes_groups_cli")
    module._methods = {}  # type: ignore[attr-defined]  # inert: only read by a turn, which never runs here
    return module


def _service(db_path: str | None = None):
    """Open the hosted-room service over the shared store, WITHOUT its worker runtime."""
    from tui_gateway.hosted_room_service import HostedRoomService

    return HostedRoomService(_stub_server(), db_path=db_path)


def _db_path(args: argparse.Namespace) -> Path:
    """The room store this verb operates on: this install's, unless explicitly overridden."""
    from gateway.hosted_rooms import default_db_path

    override = getattr(args, "db", None)
    return Path(override) if override else default_db_path()


def _parse_member(spec: str, index: int) -> dict[str, Any]:
    """``profile:handle[:display]`` → the roster object ``validate_roster`` accepts."""
    parts = spec.split(":", 2)
    if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
        raise ValueError(
            f"member {index} must be <profile>:<handle>[:<display name>], got {spec!r}")
    profile, handle = parts[0].strip(), parts[1].strip().lstrip("@")
    member: dict[str, Any] = {
        "member_id": profile,
        "profile": profile,
        "handle": handle,
    }
    if len(parts) == 3 and parts[2].strip():
        member["display_name"] = parts[2].strip()
    return member


def _room_id(requested: str | None) -> str:
    return (requested or f"{CLI_ROOM_ID_PREFIX}{secrets.token_hex(6)}").strip()


def _cmd_create(args: argparse.Namespace) -> int:
    members = [_parse_member(spec, index) for index, spec in enumerate(args.member)]
    room_id = _room_id(args.room_id)
    service = _service(getattr(args, "db", None))
    room = service.create_room(room_id=room_id, name=args.name, members=members)
    roster = ", ".join(f"@{m['handle']} ({m['profile']})" for m in room.get("members") or [])
    return _ok(
        f"created hosted room {room_id!r} ({room.get('name')}) with {len(room.get('members') or [])} "
        f"members: {roster}\n"
        f"the gateway's room worker picks it up on its next poll; drive it with "
        f"'hermes groups send {room_id} \"...\"'",
        payload={"room": room}, as_json=args.json)


def _cmd_send(args: argparse.Namespace) -> int:
    text = args.message
    if text is None and not sys.stdin.isatty():
        try:
            text = sys.stdin.read()
        except (OSError, ValueError):
            text = ""
    text = text or ""
    if not text.strip():
        return _fail("message text is empty", as_json=args.json)
    from gateway.hosted_rooms import user_event_id

    service = _service(getattr(args, "db", None))
    event = service.send(
        room_id=args.room_id,
        event_id=user_event_id(args.event_id or secrets.token_hex(16)),
        payload={"text": text, "thread_id": args.thread_id},
        actor_id="cli")
    return _ok(
        f"appended {event.get('kind')} seq={event.get('seq')} to {args.room_id!r}; "
        "the gateway's room worker will run the members' turns",
        payload={"event": event, "accepted": True}, as_json=args.json)


def _cmd_list(args: argparse.Namespace) -> int:
    from gateway.hosted_rooms import MAX_ROOM_LIST_LIMIT, list_rooms

    limit = args.limit or MAX_ROOM_LIST_LIMIT
    rooms = list_rooms(
        _db_path(args),
        include_disbanded=args.include_disbanded, limit=limit, offset=args.offset)
    if args.json:
        print(json.dumps({"rooms": rooms, "next_offset": args.offset + limit if len(rooms) == limit else None},
                         indent=2, sort_keys=True))
        return 0
    if not rooms:
        print("no hosted rooms on this install")
        return 0
    for room in rooms:
        members = len(room.get("members") or [])
        state = "disbanded" if room.get("disbanded_at") is not None else "live"
        print(f"{room.get('room_id')}  {state:9}  {members} members  {room.get('name')}")
    return 0


def _cmd_state(args: argparse.Namespace) -> int:
    from gateway.hosted_rooms import room_state

    db_path = _db_path(args)
    room = room_state(db_path, room_id=args.room_id, include_disbanded=args.include_disbanded)
    if args.json:
        print(json.dumps({"room": room}, indent=2, sort_keys=True))
        return 0
    print(f"room         {room.get('room_id')}")
    print(f"name         {room.get('name')}")
    print(f"authority    {room.get('authority_gateway_id')} epoch {room.get('authority_epoch')}")
    print(f"seq          {room.get('latest_seq', 0)} events")
    print(f"revision     {room.get('revision')}")
    print(f"disbanded    {'yes' if room.get('disbanded_at') is not None else 'no'}")
    for member in room.get("members") or []:
        display = member.get("display_name") or member.get("handle")
        print(f"  @{member.get('handle'):16} {member.get('profile'):20} {display}")
    return 0


def _cmd_log(args: argparse.Namespace) -> int:
    from gateway.hosted_rooms import read_events

    db_path = _db_path(args)
    page = read_events(
        db_path, room_id=args.room_id, since_seq=args.since, limit=args.limit,
        include_disbanded=args.include_disbanded)
    if args.json:
        print(json.dumps(page, indent=2, sort_keys=True))
        return 0
    for event in page.get("events") or []:
        actor = event.get("actor") or {}
        who = actor.get("display_name") or actor.get("handle") or actor.get("id") or actor.get("kind")
        payload = event.get("payload") or {}
        text = payload.get("text") or payload.get("reason") or payload.get("error") or ""
        print(f"[{event.get('seq')}] {event.get('kind'):24} {who}: {text}")
    return 0


def _cmd_disband(args: argparse.Namespace) -> int:
    """Tombstone a room. Live turns are NOT cancelled from here (this process owns no runtime):
    the store refuses while work is in flight, which is the safe answer for a headless verb."""
    from gateway.hosted_rooms import (
        disband_room, local_authority_gateway_id, room_state)

    db_path = _db_path(args)
    state = room_state(db_path, room_id=args.room_id, include_disbanded=True)
    gateway_id = local_authority_gateway_id()
    if str(state.get("authority_gateway_id")) != gateway_id:
        return _fail("this Group Chat is managed by another gateway", as_json=args.json)
    tombstone = disband_room(
        db_path, room_id=args.room_id, expected_gateway_id=gateway_id,
        expected_epoch=int(state.get("authority_epoch") or 1))
    return _ok(
        f"disbanded hosted room {args.room_id!r} at {tombstone.get('disbanded_at')}",
        payload={"tombstone": tombstone}, as_json=args.json)


_HANDLERS = {
    "create": _cmd_create,
    "send": _cmd_send,
    "list": _cmd_list,
    "state": _cmd_state,
    "log": _cmd_log,
    "disband": _cmd_disband,
}


def cmd_groups(args: argparse.Namespace) -> int:
    """Dispatch one ``hermes groups`` subcommand, mapping domain refusals to exit 2."""
    handler = _HANDLERS.get(getattr(args, "groups_action", None) or "")
    if handler is None:
        print("hermes groups: choose one of create, send, list, state, log, disband", file=sys.stderr)
        return 2
    from gateway.hosted_room_discussion import DiscussionValidationError
    from gateway.hosted_rooms import HostedRoomError

    try:
        return handler(args)
    except (HostedRoomError, DiscussionValidationError, ValueError) as exc:
        room_id = getattr(args, "room_id", None)
        return _fail(f"{room_id}: {exc}" if room_id else str(exc), as_json=getattr(args, "json", False))
    except BrokenPipeError:
        return 0


def build_groups_parser(subparsers) -> None:
    """Attach the ``groups`` subcommand (hosted Group Chat lifecycle, no client required)."""
    parser = subparsers.add_parser(
        "groups",
        help="Hosted Group Chat rooms (create/send/list/state/log/disband) without Desktop",
        description="Operate a hosted Group Chat room directly on this install's room store "
                    "(the ROOT shared-state.db). The room is driven by the GATEWAY's hosted-room "
                    "worker, so it keeps running with no Desktop, TUI or dashboard attached. "
                    "This verb starts no worker of its own and needs no dashboard credential.",
        epilog=(
            "Examples:\n"
            "  hermes groups create --name 'Platform Team' "
            "--member default:platform-worker --member platform-stl:stl\n"
            "  hermes groups send room-abc123 \"stand the room up on the gateway\"\n"
            "  hermes groups list\n"
            "  hermes groups state room-abc123\n"
            "  hermes groups log room-abc123 --since 0\n"
            "  hermes groups disband room-abc123\n"
            "\n"
            "A created room is durable: closing this shell, the Desktop client or the dashboard "
            "does not stop the members' turns. Exit codes: 0 success, 2 a refused request "
            "(bad roster, room conflict, unknown room), 1 unexpected failure."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    groups_sub = parser.add_subparsers(dest="groups_action")

    create_p = groups_sub.add_parser(
        "create", help="Create a hosted room (2-6 members, all local profiles)")
    create_p.add_argument("--name", required=True, help="Room display name")
    create_p.add_argument(
        "--member", action="append", required=True, metavar="PROFILE:HANDLE[:DISPLAY]",
        help="One roster member; repeat 2-6 times. The handle is the room @mention.")
    create_p.add_argument(
        "--room-id", default=None,
        help=f"Explicit room id (default: generated, {CLI_ROOM_ID_PREFIX}<12 hex>); creation is "
             "idempotent on this id")
    create_p.add_argument("--db", default=None, help="Override the room store path (default: this install's)")
    create_p.add_argument("--json", action="store_true", default=False, help="Emit a JSON result")

    send_p = groups_sub.add_parser(
        "send", help="Append one user message to a room; the gateway worker runs the turns")
    send_p.add_argument("room_id", help="Room id")
    send_p.add_argument("message", nargs="?", default=None, help="Message text (or stdin)")
    send_p.add_argument(
        "--thread-id", default="main", help="Discussion thread id (default: main)")
    send_p.add_argument(
        "--event-id", default=None,
        help="Stable retry key; reuse it to make a resend idempotent (default: random)")
    send_p.add_argument("--db", default=None, help="Override the room store path")
    send_p.add_argument("--json", action="store_true", default=False, help="Emit a JSON result")

    list_p = groups_sub.add_parser("list", aliases=["ls"], help="List hosted rooms")
    list_p.add_argument("--limit", type=int, default=None, help="Page size")
    list_p.add_argument("--offset", type=int, default=0, help="Rooms to skip")
    list_p.add_argument("--include-disbanded", action="store_true", default=False,
                        help="Include tombstoned rooms")
    list_p.add_argument("--db", default=None, help="Override the room store path")
    list_p.add_argument("--json", action="store_true", default=False, help="Emit a JSON result")

    state_p = groups_sub.add_parser("state", help="Show one room's authority and roster")
    state_p.add_argument("room_id", help="Room id")
    state_p.add_argument("--include-disbanded", action="store_true", default=False,
                         help="Allow a tombstoned room")
    state_p.add_argument("--db", default=None, help="Override the room store path")
    state_p.add_argument("--json", action="store_true", default=False, help="Emit a JSON result")

    log_p = groups_sub.add_parser("log", help="Read a room's event log delta")
    log_p.add_argument("room_id", help="Room id")
    log_p.add_argument("--since", type=int, default=0, dest="since",
                       help="Return events after this seq (default 0: from the start)")
    log_p.add_argument("--limit", type=int, default=100, help="Max events (default 100)")
    log_p.add_argument("--include-disbanded", action="store_true", default=False,
                       help="Allow a tombstoned room")
    log_p.add_argument("--db", default=None, help="Override the room store path")
    log_p.add_argument("--json", action="store_true", default=False, help="Emit a JSON result")

    disband_p = groups_sub.add_parser("disband", help="Permanently tombstone a room id")
    disband_p.add_argument("room_id", help="Room id")
    disband_p.add_argument("--db", default=None, help="Override the room store path")
    disband_p.add_argument("--json", action="store_true", default=False, help="Emit a JSON result")

    parser.set_defaults(func=cmd_groups)
