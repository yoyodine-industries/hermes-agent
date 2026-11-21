"""An ``unknown`` turn nobody is waiting on settles as interrupted (#134889 GW-10, #139532).

A turn lost across an owner restart is fenced ``unknown`` and pauses its session's FIFO, because
only the person who sent it knows whether to resend it. An automation delivery has no such person:
a cron run (``CRON_PRINCIPAL_ID``), a process completion or heartbeat (``automation:<route>``,
``local_automation_v1``, a native envelope's ``automation`` block) or a webhook delivery
(``webhook_delivery``) or a Kanban card attempt (``kanban_card_v1``, set by the dispatcher). Pausing on it stalls every message queued behind it until an operator
notices (a Bot Chat sat ten hours behind one).

Such a row settles through the same fenced resolution a person's Discard uses
(``resolve_unknown_session_input``): terminal ``interrupted``, never requeued or replayed, with a
Hermes transcript notice committed in the same transaction. No second recovery store.
"""
from __future__ import annotations

import logging

from hermes_state_keys import CRON_PRINCIPAL_ID

logger = logging.getLogger(__name__)


def is_unattended(row) -> bool:
    """True for an admission no person submitted (so nobody can be asked to resend it)."""
    principal = row.get('principal_id') or ''
    if principal == CRON_PRINCIPAL_ID or principal.startswith('automation:'):
        return True
    payload = row.get('payload') or {}
    # A FINITE turn is a one-shot execution with no interactive continuation: `admit_finite`
    # refuses `unattended` unless `finite` is true, and the finite runner is the one-shot/API/
    # automation path. Measured 2026-11-21: every unknown admission on this host (16 rows across
    # 5 stores) was a seat-diddler pass submitted as `{"finite": true, ...}` with NO
    # `unattended` key, so this predicate answered "attended" for the whole population and the
    # release edge skipped every row it existed for. The marker that is always present is
    # `finite`; requiring the caller to also remember `unattended` made the edge unreachable.
    if payload.get('finite') is True:
        return True
    native = payload.get('native_text_v1') or {}
    # A webhook delivery: the provider already got its 202 and nobody can be asked to resend it.
    # A Kanban card attempt (#134889 KB-8): the dispatcher's marker, committed only by
    # ``SessionAuthority.submit(_kanban_card=...)``; a card row main minted without it still pauses.
    return ('local_automation_v1' in payload or 'kanban_card_v1' in payload
            or 'automation' in native or 'webhook_delivery' in native)


def _unattended_unknown(rows):
    return [row for row in rows if row['status'] == 'unknown' and is_unattended(row)]


def _decided_turns(db, epoch) -> list:
    """The claims the epoch bump DECIDED, whose transcript boundary has not been written yet.

    Card t-c60889271e: the bump now ends a lost claim in the same transaction it fences it, so the
    rows never pass through ``unknown`` - and the boot edge below is where the transcript boundary
    and the media release live. Without this pass a decided turn would vanish from the viewer with
    nothing said, which is the silent failure the notice exists to prevent.
    """
    import json

    from hermes_state_runtime import _INTERRUPTION_KEY
    with db._read_ctx() as conn:
        rows = conn.execute("SELECT key,value FROM state_meta WHERE key LIKE ?",
                            (_INTERRUPTION_KEY.replace('%s', '%'),)).fetchall()
    pending = []
    for key, value in rows:
        try:
            record = json.loads(value)
        except (TypeError, ValueError):
            continue
        if record.get('epoch') != epoch or record.get('notified'):
            continue
        pending.append(key[len('session_input.interruption.v1.'):])
    return pending


def _close_decided_turns(db, epoch) -> int:
    """Say, once, that a decided turn is over: the boundary a viewer shows and the media release."""
    import json as _json

    from agent.turn_failure_copy import AUTOMATION_INTERRUPTED_NOTICE
    from gateway.session_ingress_media import release_admission_media
    from gateway.session_results import close_discarded_turn
    from hermes_state_runtime import _INTERRUPTION_KEY, _row

    closed = 0
    for admission_id in _decided_turns(db, epoch):
        def write(conn, admission_id=admission_id):
            row = conn.execute('SELECT * FROM session_admissions WHERE admission_id=?',
                               (admission_id,)).fetchone()
            if row is None:
                return None
            row = _row(row)
            # The hedged copy (`notice=None`) for a person's turn: they may still resend it, and the
            # boundary must not claim more than we know. Automation gets the explicit notice.
            close_discarded_turn(db, conn, row, AUTOMATION_INTERRUPTED_NOTICE if is_unattended(row) else None)
            receipt = conn.execute('SELECT value FROM state_meta WHERE key=?',
                                   (_INTERRUPTION_KEY % admission_id,)).fetchone()
            if receipt is not None:
                record = _json.loads(receipt[0])
                record['notified'] = True
                conn.execute('UPDATE state_meta SET value=? WHERE key=?',
                             (_json.dumps(record, separators=(',', ':')), _INTERRUPTION_KEY % admission_id))
            return row
        row = db._execute_write(write)
        if row is not None:
            release_admission_media(db, admission_id)
            closed += 1
    return closed


def settle_recovered_unattended(db, epoch) -> int:
    """Boot recovery, right after ``recover_session_inputs`` fenced lost turns ``unknown`` and before
    any FIFO is scheduled: settle every unattended one. Synchronous; runs off the owner loop."""
    from agent.turn_failure_copy import AUTOMATION_INTERRUPTED_NOTICE
    from gateway.session_ingress_media import release_admission_media
    from gateway.session_results import close_discarded_turn
    from hermes_state_runtime import RuntimeStoreError, _row, resolve_unknown_session_input
    # The bump's own decided rows first: they never pass through `unknown`, and their transcript
    # boundary is written here (once, guarded by the receipt's `notified` flag).
    _close_decided_turns(db, epoch)
    with db._read_ctx() as conn:
        rows = [_row(r) for r in conn.execute(
            "SELECT * FROM session_admissions WHERE status='unknown' ORDER BY seq").fetchall()]
    settled = 0
    for row in _unattended_unknown(rows):
        try:
            resolve_unknown_session_input(
                db, epoch=epoch, admission_id=row['admission_id'], generation=row['generation'],
                _edge='boot',
                _terminal_write=lambda conn, lost: close_discarded_turn(db, conn, lost, AUTOMATION_INTERRUPTED_NOTICE))
        except RuntimeStoreError as exc:
            logger.warning('Lost automation admission %s left unknown: %s', row['admission_id'], exc.reason)
            continue
        release_admission_media(db, row['admission_id'])
        settled += 1
        logger.info('Lost automation admission %s on session %s settled interrupted (not replayed)',
                    row['admission_id'], row['target_session_id'])
    return settled


async def settle_unattended_unknown(authority, ref) -> bool:
    """The drain found an ``unknown`` head (fenced in this owner, e.g. a lost managed worker):
    settle the unattended ones through the owner's resolution. True when any settled, so the
    drain re-reads its FIFO instead of pausing; a person's ``unknown`` row still pauses it."""
    import asyncio

    from agent.turn_failure_copy import AUTOMATION_INTERRUPTED_NOTICE
    from hermes_state_runtime import RuntimeStoreError, list_session_admissions
    rows = await asyncio.to_thread(list_session_admissions, authority.db, session_id=ref.session_id)
    settled = False
    for row in _unattended_unknown(rows):
        try:
            await authority.resolve_unknown_as_owner(ref, row['admission_id'], row['generation'],
                                                     notice=AUTOMATION_INTERRUPTED_NOTICE)
        except RuntimeStoreError as exc:
            logger.warning('Lost automation admission %s left unknown: %s', row['admission_id'], exc.reason)
            continue
        settled = True
    return settled
