"""The three ways a leaked claim ends, kept out of the queue module's line budget.

A claim that outlives its lease ends in exactly one of three shapes (ruling
t_6e43ae0a):

* **never started** -- the lease died before any turn read the record, so it is
  RE-OFFERED once, under the same ``delivery_id`` and the same key (A2/A3);
* **turn started, outcome unknown** -- the turn began and never settled, so the
  honest state is ``ambiguous`` and the sender is told so, not told "never
  delivered" (A5);
* **legacy claim** -- no marker, so today's dead-letter behaviour is unchanged.

Every function here is called by the sweep with the home's lock ALREADY HELD, which
is why none of them opens one: taking ``_locked`` twice is the deadlock this split
had to avoid.

The queue module stays the owner of the record vocabulary and of the directory
primitives; this module only sequences them into one terminal shape. The queue
imports it INSIDE the sweep, so a module-scope import of the queue here is safe.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def settle_lost_lease(root: Path, record: dict[str, Any], *, now_ns: int) -> dict[str, Any]:
    """Dead-letter a leaked claim as ``failed`` / ``lease_lost`` (caller holds the lock).

    The open attempt is CLOSED with the same reason: an attempt left open is the thing
    the census cannot see, so this recovery must not leave the very shape it repairs.
    """
    from tools import bot_delivery_queue as q

    updated = dict(record)
    delivery_id = updated["delivery_id"]
    updated["status"] = q.STATUS_FAILED
    updated["reason"] = q.REASON_LEASE_LOST
    updated["status_detail"] = q.STATUS_DETAIL_LEASE_DEAD_LETTER
    updated["updated_at"] = now_ns
    updated["queue_position"] = None
    entry = q._open_attempt(updated)
    if entry is not None:
        entry["ended_at"] = now_ns
        entry["status"] = q.STATUS_FAILED
        entry["reason"] = q.REASON_LEASE_LOST
    settled_path = root / q.SETTLED_DIR / f"{delivery_id}.json"
    os.replace(root / q.CLAIMED_DIR / f"{delivery_id}.json", settled_path)
    q._write(settled_path, updated)
    q._fsync_dir(root / q.CLAIMED_DIR)
    return dict(updated)


def recover_unstarted_claim(
    root: Path, record: dict[str, Any], *, now_ns: int
) -> dict[str, Any]:
    """Re-offer a claim that provably never started a turn (ruling A3; caller holds the lock).

    The attempt is CLOSED with ``claim_never_started`` -- which is both the fact and the
    spent replay budget (``recovery_replays``) -- and the record goes back to ``queue/``
    with the SAME ``delivery_id``, ``idempotency_key``, ``sequence``, ``reoffer_count``,
    ``attempts`` and ``message``: nothing the sender holds stops being true, so no notice
    is filed. ``attempts`` is not decremented (a claim WAS taken; the closed entry says
    what became of it), and ``requeue_unstarted`` is deliberately not used here -- it pops
    the entry, erasing both the evidence and the budget.

    ``sequence`` is preserved, so FIFO order is preserved: the recovered record is the
    oldest in its lane and is next in line, which is what it should be.
    """
    from tools import bot_delivery_queue as q

    updated = dict(record)
    updated["attempts_log"] = [dict(e) for e in (record.get("attempts_log") or [])]
    delivery_id = updated["delivery_id"]
    updated["status"] = q.STATUS_QUEUED
    updated["claimed_at"] = None
    updated["status_detail"] = q.STATUS_DETAIL_RECOVERED
    updated["updated_at"] = now_ns
    entry = q._open_attempt(updated)
    if entry is not None:
        entry["ended_at"] = now_ns
        entry["status"] = q.STATUS_QUEUED
        entry["reason"] = q.CLAIM_NEVER_STARTED
    queued_path = root / q.QUEUE_DIR / f"{delivery_id}.json"
    os.replace(root / q.CLAIMED_DIR / f"{delivery_id}.json", queued_path)
    updated["sequence"] = int(updated.get("sequence", 0))
    updated["queue_position"] = q._queue_position(root, updated) or 1
    q._write(queued_path, updated)
    q._fsync_dir(root / q.CLAIMED_DIR)
    return dict(updated)


def settle_ambiguous_lease(
    root: Path, record: dict[str, Any], *, now_ns: int
) -> dict[str, Any]:
    """Settle a claim whose turn DID start as ``ambiguous`` (ruling A5; caller holds the lock).

    The same terminal shape :func:`settle` writes -- status, detail, closed attempt --
    applied in one step because the sweep already holds this home's lock and ``settle``
    would take it again. ``ambiguous`` is the honest state AND the recovery door: the
    sender's replay path (``api_server.py``) already consumes it by re-calling
    ``reoffer_ambiguous`` under the same idempotency key, which is exactly one replay.
    """
    from tools import bot_delivery_queue as q

    updated = dict(record)
    updated["attempts_log"] = [dict(e) for e in (record.get("attempts_log") or [])]
    delivery_id = updated["delivery_id"]
    updated["status"] = q.STATUS_AMBIGUOUS
    updated["reason"] = q.REASON_LEASE_LOST
    updated["status_detail"] = q.STATUS_DETAIL_AMBIGUOUS_LEASE
    updated["updated_at"] = now_ns
    updated["queue_position"] = None
    entry = q._open_attempt(updated)
    if entry is not None:
        entry["ended_at"] = now_ns
        entry["status"] = q.STATUS_AMBIGUOUS
        entry["reason"] = q.REASON_LEASE_LOST
    settled_path = root / q.SETTLED_DIR / f"{delivery_id}.json"
    os.replace(root / q.CLAIMED_DIR / f"{delivery_id}.json", settled_path)
    q._write(settled_path, updated)
    q._fsync_dir(root / q.CLAIMED_DIR)
    return dict(updated)
