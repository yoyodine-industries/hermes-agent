"""``hermes sessions retire``: retire a ``started`` admission that NO LIVE OWNER holds.

The gap this closes, measured on 2026-11-16: an admission left ``started`` by a turn whose process
is gone blocks its session's whole FIFO - ``claim_session_input`` refuses the head while any row is
``started`` - and ``hermes sessions discard`` accepts only ``unknown`` rows, so the ops seat had the
sanctioned tool in hand and nothing lawful to run. 55 DM messages sat behind one such row.

This is the same gateway control as ``/discard`` (``prompt.retire_orphaned``), fenced the way the
unknown control is fenced and fenced the way a SLOW TURN stays safe:

  * the operator names the session, the admission AND the ``generation`` it is retiring; a row that
    has since been claimed again carries a different generation and is REFUSED;
  * the gateway refuses while a turn lease is unexpired or a worker execution is registered or
    running, and names the holder - proof of life, never a timeout guess;
  * the lost input is not replayed and nothing is requeued: the row becomes terminal with the
    outcome ``orphan_retired:<seq>`` so the ledger keeps its pointer.
"""
from __future__ import annotations

import sys


def _started(snapshot):
    return [row for row in snapshot.get('pending', []) if row.get('status') == 'started']


async def _retire(args):
    from hermes_cli.gateway_client import GatewayClientError, connect_gateway
    from hermes_cli.sessions_discard import _resume
    async with connect_gateway() as client:
        snapshot = await _resume(client, args.session)
        session_id = snapshot.get('session_id') or snapshot['stored_session_id']
        rows = [row for row in _started(snapshot) if row['admission_id'] == args.admission]
        if not rows:
            shown = ", ".join(f"{row['admission_id']}@{row.get('execution_generation')}"
                             for row in _started(snapshot)) or "none"
            raise GatewayClientError(
                f"Not a started admission of this session: {args.admission} (started: {shown})")
        row = rows[0]
        if row.get('execution_generation') != args.generation:
            raise GatewayClientError(
                f"Generation mismatch: {args.admission} is generation {row.get('execution_generation')}, "
                f"you named {args.generation}. Re-read the row; a claim since then owns it.")
        if not args.yes:
            if not sys.stdin.isatty():
                print("Refusing to retire without confirmation on a non-interactive stdin; pass --yes.", file=sys.stderr)
                return 2
            print(f"Retire {args.admission} (generation {args.generation}) on session {session_id}? "
                  "The turn is NOT replayed; its input stays in the transcript to resend.")
            try:
                if input("Proceed? [y/N] ").strip().lower() not in {"y", "yes"}:
                    print("Cancelled; nothing was retired.")
                    return 1
            except EOFError:
                return 1
        result = await client.rpc("prompt.retire_orphaned", session_id=session_id,
                                  admission_id=args.admission, generation=args.generation)
        print(f"Retired {args.admission} ({result.get('outcome')}); the session's queue may advance.")
        for line in (result.get('evidence') or []):
            print(f"  evidence: {line}")
        return 0


def add_retire_parser(sessions_subparsers) -> None:
    from hermes_cli.subcommands._shared import add_yes_flag
    parser = sessions_subparsers.add_parser(
        "retire", help="Retire a 'started' admission whose owner is gone (it blocks the session)",
        description="An admission left 'started' by a turn whose process is gone blocks the whole "
            "session: the next message cannot be claimed. `sessions discard` handles 'unknown' only. "
            "This retires the row WITHOUT replaying the turn, and REFUSES while a live turn lease or "
            "worker holds it, naming the holder.")
    parser.add_argument("session", help="Session ID or title")
    parser.add_argument("--admission", required=True, metavar="ID",
        help="The started admission to retire (required: this never retires a session's other rows)")
    parser.add_argument("--generation", required=True, type=int, metavar="N",
        help="The generation the row currently carries (from `hermes sessions list`); a mismatch is refused")
    add_yes_flag(parser, "Do not ask for confirmation (required when stdin is not a TTY)")


def cmd_retire(args) -> int:
    import asyncio
    from hermes_cli.gateway_client import GatewayClientError
    try:
        return asyncio.run(_retire(args))
    except GatewayClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
