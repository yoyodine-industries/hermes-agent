"""``hermes gateway admission``: the supported door for clearing a FOSSIL admission.

A session's FIFO is claimable only while no row for it is ``started`` or ``unknown``. A ``started``
claim whose turn died is unreachable: the owner that could settle it is gone (generation and epoch
are its own), and ``cancel_session_input`` refuses a ``started`` row outright — so the session
stays paused forever, with the lost input still first in its queue.

``retire`` resolves one such row through the live owner's fenced ``prompt.resolve_unknown`` — the
same control ``/discard`` and ``hermes sessions discard`` use, never a direct store write. The row
settles ``interrupted`` (the lost input is not replayed), a reason is REQUIRED and recorded with it,
and a row this owner epoch is still running is refused, naming the live turn lease that proves it.
"""
from __future__ import annotations

import sys


def _admission(admission_id):
    """The admission row, read read-only from this profile's store.

    The owner re-validates everything it acts on (status, generation, epoch) inside its own write
    transaction, so a stale read here can only produce a refusal, never a wrong retirement.
    """
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB
    db = SessionDB(db_path=get_hermes_home() / 'state.db', read_only=True)
    try:
        with db._read_ctx() as conn:
            row = conn.execute(
                'SELECT admission_id, target_session_id, status, outcome, owner_epoch, generation '
                'FROM session_admissions WHERE admission_id=?', (admission_id,)).fetchone()
        return dict(row) if row is not None else None
    finally:
        db.close()


def _refusal_text(exc) -> str:
    data = getattr(exc, 'data', None) or {}
    field, detail = data.get('field'), data.get('detail')
    if detail:
        return f"{exc} ({field}: {detail})" if field else f"{exc} ({detail})"
    return str(exc)


async def _retire(args):
    from hermes_cli.gateway_client import GatewayClientError, connect_gateway
    admission_id = args.admission_id
    if not (args.reason or '').strip():
        raise GatewayClientError('--reason is required: the retirement is recorded with it')
    row = _admission(admission_id)
    if row is None:
        raise GatewayClientError(f'No admission {admission_id} in this profile\'s store')
    if row['status'] == 'terminal':
        print(f"{admission_id} is already terminal (outcome={row['outcome']}); nothing to retire.")
        return 0
    if row['status'] == 'queued':
        raise GatewayClientError(
            f'{admission_id} is queued, not blocking its session: cancel it from the session instead')
    if not args.yes:
        if not sys.stdin.isatty():
            print('Refusing to retire without confirmation on a non-interactive stdin; pass --yes.',
                  file=sys.stderr)
            return 2
        print(f"Retire {admission_id} on session {row['target_session_id']} "
              f"(status={row['status']}, owner_epoch={row['owner_epoch']})? "
              "It settles interrupted; the input is not replayed.")
        try:
            if input('Proceed? [y/N] ').strip().lower() not in {'y', 'yes'}:
                print('Cancelled; nothing was retired.')
                return 1
        except EOFError:
            print('Cancelled; nothing was retired.')
            return 1
    async with connect_gateway() as client:
        snapshot = await client.rpc('session.resume', session_id=row['target_session_id'])
        session_id = snapshot.get('session_id') or snapshot['stored_session_id']
        # The owner stamped this generation at the claim; the snapshot carries the live one.
        live = {item.get('admission_id'): item for item in snapshot.get('pending', [])}.get(admission_id)
        generation = (live or {}).get('execution_generation') or row['generation']
        receipt = await client.rpc('prompt.resolve_unknown', session_id=session_id,
                                   admission_id=admission_id, execution_generation=generation,
                                   reason=' '.join(args.reason.split()))
    print(f"Retired {admission_id} on session {session_id}: status={receipt.get('status')} "
          f"outcome={receipt.get('outcome')}. The input is not replayed.")
    return 0


def cmd_admission(args) -> int:
    import asyncio
    from websockets.exceptions import WebSocketException
    from hermes_cli.gateway_client import GatewayClientError, GatewayRPCError
    command = getattr(args, 'admission_command', None)
    if command != 'retire':
        print('hermes gateway admission: expected a subcommand (retire)', file=sys.stderr)
        return 2
    try:
        return asyncio.run(_retire(args))
    except GatewayRPCError as exc:
        print('Refused: ' + _refusal_text(exc), file=sys.stderr)
        return 1
    except (GatewayClientError, OSError, TimeoutError, WebSocketException) as exc:
        message = str(exc) if isinstance(exc, GatewayClientError) else 'Gateway connection/read failed'
        print('Error: ' + message, file=sys.stderr)
        return 1


def add_admission_parser(gateway_subparsers) -> None:
    from hermes_cli.subcommands._shared import add_yes_flag
    parser = gateway_subparsers.add_parser(
        'admission', help='Retire a fossil session admission that pauses a session FIFO',
        description="A 'started' or 'unknown' admission pauses its session: nothing behind it can be "
                    "claimed, and nothing can settle a row whose owning process generation is gone. "
                    "retire is the supported door for such a row.")
    admission_subparsers = parser.add_subparsers(dest='admission_command')
    retire = admission_subparsers.add_parser(
        'retire', help='Retire a non-terminal admission whose owning process generation is gone',
        description="Resolve one admission through the live owner's fenced acknowledgement: it settles "
                    "'interrupted' and the session's FIFO resumes. The lost input is not replayed. "
                    "A row this owner epoch is still running is refused, naming its live turn lease.")
    retire.add_argument('--id', dest='admission_id', required=True, metavar='ADMISSION_ID',
                        help='The admission id to retire (the blocking error names it)')
    retire.add_argument('--reason', required=True, metavar='TEXT',
                        help='Why this admission is retired; recorded with the retirement')
    add_yes_flag(retire, 'Do not ask for confirmation (required when stdin is not a TTY)')
