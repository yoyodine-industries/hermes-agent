"""The small set of deliveries the drainer files INTO a sender's own lane.

Two failures owe the sender a word, and both are reported here so the drainer's core
(``tools/bot_delivery_queue.py``) stays inside its line cap:

* an EXPIRED record the lane never read (t_8a7a47d7 b) -- the receipt the sender holds
  said "queued -- the receiver re-offers it", which stopped being true;
* a LOST LEASE (t_6e43ae0a) -- the claim was orphaned, so the payload either was never
  read (``never_offered`` / dead-lettered) or MAY have been read (``ambiguous``), and
  either way a ``settled/`` file nobody reads is a silent failure.

Both are deterministic: no LLM, one delivery per event, into the sender's own spool,
read by that lane's next turn. Both are idempotent on their own key, and neither can
feed the other -- :data:`EXPIRED_NOTICE_SENDER` is never sent a notice.

The queue module is imported INSIDE the functions, never at module scope: the queue
imports this module, so a top-level import here would be a cycle.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Why the notice itself was queued -- it is a fresh delivery, not a backlog one.
STATUS_DETAIL_UNDELIVERED_NOTICE = (
    "undelivered: the peer delivery this notice reports expired before any turn read it"
)


def expired_notice_key(delivery_id: str) -> str:
    """The idempotency key of the ONE failure notice owed for ``delivery_id``."""
    return f"expired-notice:{delivery_id}"


def lease_lost_notice_key(delivery_id: str) -> str:
    """The idempotency key of the ONE failure notice owed for a lost lease."""
    return f"lease-lost-notice:{delivery_id}"


def _profile_home(profile: str) -> Path | None:
    """The home of ``profile`` on this install, or None when it is not a profile."""
    try:
        from hermes_cli.profiles import get_profile_dir

        return Path(get_profile_dir(profile))
    except Exception:  # an unknown sender is not a sweep failure
        return None


def _never_offered_failure_message(
    record: dict[str, Any], delivery_id: str, target: str, now_ns: int | None
) -> str:
    """What the sender is told when its delivery expired without ever being offered."""
    from tools import bot_delivery_queue as q

    waited = int(q.queued_seconds(record, now_ns=now_ns))
    return (
        f"[peer-delivery FAILED] your delivery {delivery_id} to {target} was NEVER read: "
        f"it waited {waited}s queued and expired without ever being offered to a turn "
        f"(the drain never offered it). The earlier 'queued' receipt is NOT a pending "
        f"promise and nothing is still in flight. Escalate (the escalate tool) or resend "
        f"if this still matters."
    )


def _deliver_into_live_context(
    home: str | os.PathLike[str], message: str, *, record: dict[str, Any]
) -> bool:
    """Hand a failure notice to the sender's live session -- its next turn context.

    Deliberately NOT a queued turn (t_43f11055 c): a live-mailbox delivery is claimed by
    the session that is already running, so it can never grow the queue it reports on and
    can never itself expire. Returns False -- never raises -- when the sender has no
    advertised live consumer; the caller then keeps the failure in the log.
    """
    from tools import bot_delivery_queue as q

    try:
        from tools.bot_live_delivery import (
            deliver_to_live_owner,
            find_canonical_live_owner,
        )

        owner = find_canonical_live_owner(Path(home))
        if owner is None:
            return False
        raw = str(record.get("delivery_id") or "")
        notice_id = hashlib.sha256(f"{raw}:never-offered".encode()).hexdigest()
        deliver_to_live_owner(
            Path(home), owner, message, delivery_id=notice_id,
            notification_category="diagnostic",
        )
        return True
    except Exception:  # a notice must never break the sweep
        logger.debug("bot_delivery never-offered live delivery failed", exc_info=True)
        return False


def notice_expired_to_sender(
    record: dict[str, Any],
    *,
    home: str | os.PathLike[str] | None = None,
    now_ns: int | None = None,
) -> dict[str, Any] | None:
    """File a FAILURE notice in the SENDER's own spool for an expired record.

    Card t_8a7a47d7 (b): a sender's receipt said "queued -- the receiver re-offers
    it". When the record expires unread that promise was false and the sender had
    no way to learn it; the receipt is long gone, so the text sat invisible until a
    human read the queue by hand. File ONE delivery into the sender's own lane so
    the failure is READ by that lane's turn -- deterministically, with no LLM in
    the loop -- which is the sender's cue to escalate.

    ``home`` is the sender's own home. Resolve it by profile only when omitted, and
    only from outside any lane's runtime scope (``get_profile_dir`` resolves against
    the ambient default home). Idempotent on the expired record: one notice per
    delivery. Returns the admitted record, or None when no notice is owed -- no
    sender, a notice about a notice, an unknown profile, or a full sender queue.

    An expiry of a record that was NEVER OFFERED to a turn files NO queued notice
    (t_43f11055 c): that failure is the drain's, and queueing a fresh turn behind the
    same busy lane is how a notice-of-a-notice loop forms -- 5 such notice records sat
    in platform-stl's settled set. The failure is logged here with the sender, the
    target and the delivery id, and the record itself settles as ``never_offered``,
    which is the reason the strand census counts and escalates from the board. No new
    queue record is ever created on that path, so the loop cannot form.
    """
    from tools import bot_delivery_queue as q

    delivery_id = str(record.get("delivery_id") or "")
    sender = str(record.get("sender_profile") or "")
    target = str(record.get("target_profile") or "")
    if not delivery_id or not sender or sender == q.EXPIRED_NOTICE_SENDER:
        return None
    if int(record.get("attempts") or 0) <= 0 or record.get("reason") == q.REASON_NEVER_OFFERED:
        # (c) An UNOFFERED expiry owes no queued turn: queueing one behind the same busy
        # lane is how the notice-of-a-notice loop formed. The sender still learns the
        # delivery died -- in its NEXT TURN CONTEXT if it has a live session, otherwise
        # in the gateway log where the failure is searchable by sender and id. Either
        # way nothing new can be queued, so nothing new can expire.
        failure_home = Path(home) if home is not None else _profile_home(sender)
        message = _never_offered_failure_message(record, delivery_id, target, now_ns)
        delivered = (
            failure_home is not None
            and _deliver_into_live_context(failure_home, message, record=record)
        )
        q._log(
            "expired_never_offered_failure",
            record,
            sender=sender,
            target=target,
            waited_seconds=int(q.queued_seconds(record, now_ns=now_ns)),
            delivered_to="live_context" if delivered else "log",
            notice="none",
        )
        return None
    if home is None:
        home = _profile_home(sender)
        if home is None:
            q._log("expired_notice_skipped", record, reason="unknown_sender_profile")
            return None
    waited = int(q.queued_seconds(record, now_ns=now_ns))
    message = (
        f"[peer-delivery FAILED] your delivery {delivery_id} to {target} was NEVER read: "
        f"it waited {waited}s queued and expired still unread after "
        f"{int(record.get('attempts') or 0)} turn attempt(s). "
        f"The earlier 'queued' receipt is NOT a pending promise. Escalate (the escalate "
        f"tool) or resend if this still matters."
    )
    key = expired_notice_key(delivery_id)
    # A deterministic id keeps the notice single-writer: a re-run of the sweep
    # re-admits nothing.
    notice_id = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    try:
        admitted = q.admit(
            home,
            sender_profile=q.EXPIRED_NOTICE_SENDER,
            target_profile=sender,
            target_session_id=None,
            idempotency_key=key,
            fingerprint=hashlib.sha256(message.encode("utf-8")).hexdigest(),
            delivery_id=notice_id,
            message=message,
            status_detail=STATUS_DETAIL_UNDELIVERED_NOTICE,
        )
    except q.QueueFullError:
        q._log("expired_notice_refused", record, reason=q.STATUS_DETAIL_QUEUE_FULL,
               target=sender)
        return None
    q._log("expired_notice", record, notice_id=admitted.get("delivery_id"), target=sender)
    return admitted


def notice_lease_lost_to_sender(
    record: dict[str, Any],
    *,
    home: str | os.PathLike[str] | None = None,
    now_ns: int | None = None,
) -> dict[str, Any] | None:
    """File a FAILURE notice in the SENDER's own spool for a dead-lettered claim.

    A claim whose lease ran out is a delivery that was accepted and
    never made -- eleven of them sat for up to 181h with nobody told. The receipt the
    sender holds said "queued -- the receiver re-offers it", which stopped being true
    when the lease was lost, and a ``settled/failed`` file nobody reads is a silent
    failure. So the repair tells the SENDER, deterministically and with no LLM in the
    loop, through the same door the expiry notice uses: ONE delivery into the sender's
    own lane, read by that lane's next turn, which is the sender's cue to resend.

    It carries the TRUTH, which differs by case (ruling A5): a claim that never started a
    turn was NEVER delivered; a claim whose turn STARTED may have been read, so its notice
    says UNRESOLVED and names the SAME key as the one safe replay.

    Idempotent on the delivery (one notice per lost lease). Returns the admitted
    record, or ``None`` when no notice is owed -- no sender, a notice about a notice,
    an unknown profile, or a full sender queue.
    """
    from tools import bot_delivery_queue as q

    delivery_id = str(record.get("delivery_id") or "")
    sender = str(record.get("sender_profile") or "")
    target = str(record.get("target_profile") or "")
    if not delivery_id or not sender or sender == q.EXPIRED_NOTICE_SENDER:
        return None
    if record.get("reason") != q.REASON_LEASE_LOST:
        return None
    if record.get("status") not in (q.STATUS_FAILED, q.STATUS_AMBIGUOUS):
        return None
    if home is None:
        home = _profile_home(sender)
        if home is None:
            q._log("lease_lost_notice_skipped", record, reason="unknown_sender_profile")
            return None
    claimed = int(q.claim_age_seconds(record, now_ns=now_ns) or 0)
    if record.get("status") == q.STATUS_AMBIGUOUS:
        # The turn STARTED and never settled (ruling A5). Saying "NEVER delivered" here
        # would be a lie the sender acts on: the payload may have been read, so the honest
        # notice is "unknown", and the one safe replay is the SAME key.
        message = (
            f"[peer-delivery UNKNOWN] your delivery {delivery_id} to {target} is UNRESOLVED: "
            f"it was claimed for {claimed}s, the turn STARTED and never settled (the claim's "
            f"lease was lost), so the payload MAY have been read. The queue settled it as "
            f"ambiguous: resend under the SAME idempotency key to get the one permitted "
            f"replay -- a NEW key would deliver a second copy of a message that may already "
            f"have been read."
        )
    else:
        message = (
            f"[peer-delivery FAILED] your delivery {delivery_id} to {target} was NEVER delivered: "
            f"it was claimed for {claimed}s, the claim's lease was lost (no live holder) and the "
            f"record was dead-lettered as lease_lost. The earlier 'queued' receipt is NOT a "
            f"pending promise. Resend if this still matters."
        )
    key = lease_lost_notice_key(delivery_id)
    notice_id = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    try:
        admitted = q.admit(
            home,
            sender_profile=q.EXPIRED_NOTICE_SENDER,
            target_profile=sender,
            target_session_id=None,
            idempotency_key=key,
            fingerprint=hashlib.sha256(message.encode("utf-8")).hexdigest(),
            delivery_id=notice_id,
            message=message,
            status_detail=STATUS_DETAIL_UNDELIVERED_NOTICE,
        )
    except q.QueueFullError:
        q._log("lease_lost_notice_refused", record, reason=q.STATUS_DETAIL_QUEUE_FULL,
               target=sender)
        return None
    q._log("lease_lost_notice", record, notice_id=admitted.get("delivery_id"), target=sender)
    return admitted
