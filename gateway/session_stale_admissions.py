"""Boot recovery for admissions whose owning process generation is gone.

``recover_session_inputs`` fences a ``started`` claim from a previous epoch as ``unknown`` so a
restart never replays it. That fence is right, but it leaves the row blocking its session: a
``started`` row cannot be settled by the new owner (it holds another epoch) and
``cancel_session_input`` refuses one outright, while an ``unknown`` head refuses every claim. One
turn that died mid-flight would therefore pause its session's FIFO forever.

The rows are not ambiguous: the epoch row advances once per owner start, so an admission stamped
with any other epoch is owned by a process that cannot exist any more. This step retires them
``interrupted`` — the same shape ``settle_recovered_unattended`` gives a lost automation turn, and
the shape a settled interrupted turn has — with the hedged transcript boundary so the person sees
the turn was lost rather than silently vanish. Input is never replayed; the FIFO simply resumes.

Runs after ``settle_recovered_unattended``: that step owns the unattended rows and writes the
automation copy, so what is left here is a person's lost turn or a claim whose turn died.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def retire_stale_admissions_at_boot(db, epoch) -> int:
    """Retire every non-terminal admission stamped by an epoch other than ``epoch``; return the count."""
    from gateway.session_ingress_media import release_admission_media
    from gateway.session_results import close_discarded_turn
    from hermes_state_runtime import retire_stale_admissions
    retired = retire_stale_admissions(
        db, epoch=epoch, reason='boot',
        # Same transaction as the retirement: a failure rolls the whole sweep back, so a session
        # is never left half-repaired (row retired, transcript boundary missing).
        _terminal_write=lambda conn, lost: close_discarded_turn(db, conn, lost))
    for entry in retired:
        release_admission_media(db, entry['admission_id'])
    if retired:
        logger.info('Boot recovery retired %d stale admission(s) as interrupted; their sessions can '
                    'claim their FIFOs again', len(retired))
    return len(retired)
