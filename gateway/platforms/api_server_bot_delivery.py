"""Receiver-side drainer for peer deliveries (spec §3.1 triggers 1-4).

``_bot_send_turn`` in ``api_server.py`` runs the delivery its own request was
admitted for. This module owns the other half of the acceptance promise: a
delivery that was accepted with ``receipt`` because the target's turn slot was
busy must still RUN once that slot frees. Without it the queue is write-only --
the live build held 53 ``queued`` records with a full body each and no code path
that advanced or expired a single one (VERIFICATION D1).

Four triggers converge here, all under the target's durable turn lock so a
drained delivery can never race the lock holder any more than a live request can:

1. admission -- :func:`drain_under_lock` keeps draining the queue under the lock
   the caller just acquired, until the caller's own delivery is at the head (or
   the caller's promised window elapses -- the caller then keeps its receipt).
2. turn end -- the same loop is what runs after each turn, so an idle target with
   a backlog drains it back-to-back instead of idling between chores.
3. the 30s sweep -- :func:`sweep_loop`, started beside ``_sweep_orphaned_runs``.
4. the hourly chore -- ``bot_delivery_queue.cleanup_bot_delivery_queue``.

The turn lock may be held by a UI/desktop session, not just a card worker (the
live build reproduced a 1800.3s lease wait against a desktop holder), so nothing
here expires or abandons a record merely because its target is mid-turn: the
holder is the one that will drain it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

from tools import bot_delivery_queue as delivery_queue

#: One log line per delivery decision, in the queue module's own format.
log_delivery_event = delivery_queue._log

logger = logging.getLogger(__name__)

#: Triggers 1-2 run inside a caller's HTTP window: stop *starting* new turns once
#: the caller's promised window has elapsed. A turn already running is never
#: abandoned (it is the delivery that was promised).
IN_CALL_DRAIN_MIN_SECONDS = 1.0

#: Trigger 3's budget per PROFILE per tick is deliberately ABSENT now: a lane is
#: drained back-to-back until one of its stop conditions fires (queue empty, slot
#: held, lease-contended). A wall-clock slice only ever bounded how many turns a
#: lane STARTED, never how long one turn ran, so an idle lane's head waited behind
#: every other lane's slow turns -- hours of it, while its own slot sat free. Since
#: each lane now runs on its own task, no lane can spend another lane's time, and
#: there is nothing left for a budget to protect.

#: Roster rotation cursor: the next tick starts at the next lane, so a full tick
#: never leaves the same lane last twice in a row. In-memory: losing it costs one
#: tick of ordering, never a delivery (every lane is visited every tick).
_roster_offset = 0


def delivery_run_kwargs(record: dict[str, Any]) -> Dict[str, Any]:
    """Run kwargs for a delivery with no live request behind it (§2.5).

    A drained record carries its own message and target session, so the turn is
    replayed into that session with the session's stored runtime selection --
    there is no sender body to re-read and no request-scoped model lock to honour.
    """
    return {
        "user_message": record.get("message") or "",
        "ephemeral_system_prompt": None,
        "session_id": record.get("target_session_id"),
        "gateway_session_key": None,
        "route": None,
        "session_model": None,
        "requested_runtime": {},
        "route_source": "global",
        "confirmed_runtime_lock": False,
    }


def _canonical_bot_chat_tip(home: Path) -> str:
    """Current compression tip of the lane's canonical ``Bot Chat`` session.

    A peer DM is always addressed to the target lane's Bot Chat, so its current
    compression tip is the lane's live (or default) delivery session. Resolved
    fresh from ``state.db`` because a sender may have resolved the tip before the
    Bot Chat compressed again, leaving its pinned session a dead parent.
    """
    from tools.bot_mode_probe import BOT_CHAT_TITLE

    state = Path(home).resolve() / "state.db"
    if not state.is_file():
        return ""
    try:
        from hermes_state import SessionDB

        db = SessionDB(db_path=state, read_only=True)
    except Exception:
        return ""
    try:
        row = db.get_session_by_title(BOT_CHAT_TITLE)
        if not row:
            return ""
        return str(db.get_compression_tip(row["id"]) or "")
    except Exception:
        return ""
    finally:
        db.close()


def resolve_delivery_session(
    home: Path,
    record: dict[str, Any],
    *,
    tip_fn: Any = None,
) -> str:
    """Resolve the session a drained record's turn should run in (DoD #2).

    A record pins ``target_session_id`` as the sender resolved it at admit time.
    A lane that has since compressed its Bot Chat leaves that pinned session a
    dead parent with no live owner; running the turn there dead-letters the
    delivery. Resolve forward to the lane's current Bot Chat tip (its live, or
    default, session) when the pinned session is not that tip, and keep the
    pinned session otherwise (including when no tip can be proven -- degrade,
    never drop the delivery).
    """
    pinned = str(record.get("target_session_id") or "")
    if not pinned:
        return ""
    get_tip = _canonical_bot_chat_tip if tip_fn is None else tip_fn
    try:
        tip = str(get_tip(home) or "")
    except Exception:
        tip = ""
    if not tip or tip == pinned:
        return pinned
    log_delivery_event("resessioned", record, from_session=pinned, to_session=tip)
    return tip


def _failure_reason(error: str) -> str:
    """Classify a turn failure with the fork's shared reason table."""
    try:
        from tools.bot_failure_reasons import UNKNOWN, classify_agent_error
    except Exception:
        return "unknown"
    try:
        return classify_agent_error(error)
    except Exception:
        return UNKNOWN


def _finalize_reply(payload: Any) -> str:
    """Resolve a turn's final response into the reply text stored on the record."""
    if not isinstance(payload, dict):
        return ""
    try:
        from gateway.platforms.api_server import _resolve_media_to_data_urls

        return _resolve_media_to_data_urls(payload.get("final_response", "") or "")
    except Exception:
        return str(payload.get("final_response", "") or "")


def _already_delivered(history: Any, record: dict[str, Any]) -> bool:
    """Is this record's message already a user turn in ``history``?

    Only a RE-OFFERED record can match: recovery hands a record back to the
    queue whose turn may have reached the session before its process died, while
    a first-time drain has not put its message into the session yet. Compared
    exactly (modulo trailing whitespace) because the drained turn passes
    ``record["message"]`` through as the user message verbatim.
    """
    needle = str(record.get("message") or "").rstrip()
    if not needle:
        return False
    for entry in history or ():
        if not isinstance(entry, dict) or entry.get("role") != "user":
            continue
        content = entry.get("content")
        if isinstance(content, list):
            content = "".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict)
            )
        if str(content or "").rstrip() == needle:
            return True
    return False


@contextmanager
def target_profile_scope(adapter: Any, record: dict[str, Any]) -> Iterator[None]:
    """Run a DRAINED turn inside its target lane's own runtime scope.

    A drained record has no live request behind it, so nothing has scoped the
    turn: ``_api_request_profile`` is unset, and the ``_profile_scope(None)`` that
    ``_run_agent`` applies for an unset profile enters the DEFAULT profile's scope
    whenever ``multiplex_profiles`` is on. The lane's own session id would then be
    applied against the DEFAULT home's ``state.db``: the record still settles
    ``delivered``, but the lane's Bot Chat never sees the message and the sender's
    text is echoed into a brand-new "Message from ..." session in the default home.

    Set the request profile to the record's target lane for the duration of the
    turn -- and enter that lane's scope, so every home-relative read in between
    (the session history included) resolves to the lane that owns the record --
    then restore both. The target profile is the lane the record was admitted into
    (``_bot_send_home``), so scope and ``home`` agree by construction; ``"default"``
    resolves back to the default home in ``get_profile_dir``, so a default-lane
    record is scoped exactly as before.
    """
    from gateway.platforms.api_server import _api_request_profile

    profile = str(record.get("target_profile") or "")
    token = _api_request_profile.set(profile)
    try:
        with adapter._profile_scope(profile):
            yield
    finally:
        _api_request_profile.reset(token)


async def run_record(
    adapter: Any,
    home: Path,
    record: dict[str, Any],
    *,
    ctx: Optional[Dict[str, Any]] = None,
    waited: float = 0.0,
) -> Tuple[dict[str, Any], Optional[str]]:
    """Run one CLAIMED record's turn and settle it. Returns ``(record, reply)``.

    ``ctx`` is the live request's context when the record belongs to the caller
    (its body-selected runtime wins); a drained record passes ``ctx=None``.

    A contended session lease is never surfaced to a sender and never charged an
    attempt (§2.7 step 2): the record goes back to ``queued`` for the next free
    slot (``status: queued``, so the caller keeps its receipt).
    """
    delivery_id = str(record.get("delivery_id"))
    if ctx is None:
        # Drained records may pin a dead session: the sender resolved the Bot Chat
        # tip before it compressed again. Deliver into the lane's live (or default)
        # session instead of dead-lettering the turn forever (DoD #2).
        session_id = resolve_delivery_session(home, record)
        kwargs = delivery_run_kwargs(record)
        kwargs["session_id"] = session_id
        # ...and nothing has scoped the turn to that lane: a drained record has no
        # request behind it, so enter its target profile's scope for the turn
        # itself. Otherwise the lane's session id is applied to the DEFAULT home.
        scope = target_profile_scope(adapter, record)
    else:
        session_id = str(record.get("target_session_id") or "")
        kwargs = dict(ctx.get("run_kwargs") or {})
        kwargs["session_id"] = session_id
        # The caller's own request already set the profile it was routed to.
        scope = nullcontext()
    started = time.monotonic()
    log_delivery_event("turn_start", record, drained=ctx is None)
    try:
        with scope:
            history = await adapter._conversation_history_for_session(session_id)
            if ctx is None and _already_delivered(history, record):
                # A recovered record can re-offer a turn that already ran: the
                # process died after its message reached the session but before
                # the record settled. The history is the receipt, so settle it
                # delivered instead of answering the sender a second time.
                settled = delivery_queue.settle(
                    home,
                    delivery_id,
                    status=delivery_queue.STATUS_DELIVERED,
                    reply="",
                )
                adapter._bot_send_record_status(
                    delivery_id,
                    settled,
                    result=delivery_queue.RESULT_DELIVERED,
                    reply="",
                )
                log_delivery_event(
                    "turn_already_delivered",
                    settled,
                    seconds=round(time.monotonic() - started, 3),
                )
                return settled, ""
            result, _usage = await adapter._run_agent(
                conversation_history=history,
                lease_wait_seconds=delivery_queue.lease_probe_seconds(),
                **kwargs,
            )
    except Exception as exc:  # noqa: BLE001 - any turn failure settles the record
        logger.exception("[api_server] peer delivery turn failed: %s", delivery_id)
        error = str(exc) or exc.__class__.__name__
        settled = delivery_queue.settle(
            home,
            delivery_id,
            status=delivery_queue.STATUS_FAILED,
            reply=None,
            error=error,
            reason=_failure_reason(error),
        )
        adapter._bot_send_record_status(
            delivery_id, settled, result=delivery_queue.RESULT_FAILED, error=error
        )
        log_delivery_event("turn_failed", settled, seconds=round(time.monotonic() - started, 3))
        return settled, None

    lease_timed_out = (
        isinstance(result, dict)
        and bool(result.get("failed"))
        and "lease" in str(result.get("error") or "").lower()
    )
    if lease_timed_out:
        requeued = delivery_queue.requeue_unstarted(home, delivery_id)
        adapter._bot_send_record_status(
            delivery_id, requeued, result=delivery_queue.RESULT_RECEIPT
        )
        log_delivery_event("turn_lease_contended", requeued, waited_seconds=waited)
        return requeued, None

    reply = _finalize_reply(result)
    settled = delivery_queue.settle(
        home, delivery_id, status=delivery_queue.STATUS_DELIVERED, reply=reply
    )
    adapter._bot_send_record_status(
        delivery_id, settled, result=delivery_queue.RESULT_DELIVERED, reply=reply
    )
    log_delivery_event("turn_end", settled, seconds=round(time.monotonic() - started, 3))
    return settled, reply


async def drain_under_lock(
    adapter: Any,
    *,
    home: Path,
    target_profile: str,
    delivery_id: str,
    ctx: Dict[str, Any],
    waited: float,
) -> Any:
    """Triggers 1-2: drain the queue under the turn lock this caller just took.

    HEAD-FIRST, always. Handing a foreign head back with another receipt is what
    wedged the live backlog (VERIFICATION D1): the record's own request is long
    gone, so nothing would ever pick it up again. The caller's delivery keeps its
    place in FIFO order and receives the receipt the caller was promised once its
    window elapses.
    """
    started = time.monotonic()
    window = delivery_queue.receipt_after_seconds() - waited
    deadline = started + max(IN_CALL_DRAIN_MIN_SECONDS, window)
    drained = 0
    while True:
        record = delivery_queue.claim_next(home, target_profile=target_profile, lease_ok=True)
        if record is None:
            break
        record_id = str(record.get("delivery_id"))
        if record_id == delivery_id:
            settled, _reply = await run_record(adapter, home, record, ctx=ctx, waited=waited)
            elapsed = time.monotonic() - started
            if str(settled.get("status")) == delivery_queue.STATUS_QUEUED:
                return adapter._bot_send_receipt(
                    delivery_id, settled, waited=elapsed, home=home
                )
            return adapter._bot_send_json(
                delivery_queue.build_envelope(settled, waited_seconds=elapsed)
            )
        log_delivery_event("draining_ahead", record, drained=drained + 1)
        await run_record(adapter, home, record)
        drained += 1
        if time.monotonic() >= deadline:
            break

    current = delivery_queue.read_record(home, delivery_id)
    if current is None:
        logger.error("[api_server] peer delivery vanished mid-flight: %s", delivery_id)
        from gateway.platforms.api_server import _error_response

        return _error_response(
            "Delivery is already being handled.", 409, code="delivery_in_progress"
        )
    return adapter._bot_send_receipt(
        delivery_id, current, waited=time.monotonic() - started, home=home
    )


async def _drain_profile(
    adapter: Any,
    *,
    root: Path,
    profile_home: Path,
    profile: str,
) -> int:
    """Drain ONE lane's queue back-to-back until it can make no more progress.

    Runs to completion inside a single task: a lane's records stay strictly FIFO
    and are never drained twice, while every other lane runs on its own task.
    Three stop conditions, all of them LANE-LOCAL -- the queue came back empty, the
    lane's slot is held by another turn, or a settled status came back ``queued``
    (lease-contended). There is deliberately no wall-clock slice: it bounded how
    many turns a lane started, never how long one ran, so a slow lane left its
    backlog for the next tick while other lanes waited behind it.
    """
    from tools.bot_relay import TurnBusyError, acquire_turn_lock

    drained = 0
    while True:
        try:
            with acquire_turn_lock(root, profile, timeout_seconds=0):
                record = delivery_queue.claim_next(
                    profile_home, target_profile=profile, lease_ok=True
                )
                if record is None:
                    # A lane with queued records that still came back
                    # unclaimable is the exact state that used to vanish
                    # without a trace -- name the lane and its depth.
                    log_delivery_event(
                        "drain_nothing_claimable",
                        None,
                        target=profile,
                        home=str(profile_home),
                        reason="claim_empty_while_queued",
                        queued=delivery_queue.queue_depth(profile_home, profile),
                    )
                    break
                settled, _reply = await run_record(adapter, profile_home, record)
                if str(settled.get("status")) == delivery_queue.STATUS_QUEUED:
                    # A lease-contended requeue: the turn never started, so
                    # the record is back in the queue, still claimable, with
                    # `reoffer_count` as the loop's only progress signal.
                    # Re-claiming it here re-runs the same un-runnable turn
                    # until the slice expires (28 claims in one live tick,
                    # each reported as drained). Leave it for the next tick.
                    log_delivery_event(
                        "drain_lease_contended", settled, target=profile
                    )
                    break
                drained += 1
                log_delivery_event("drained", settled, drained=drained)
        except TurnBusyError:
            log_delivery_event(
                "drain_deferred", None, target=profile, reason="slot_held"
            )
            break
    return drained


async def drain_once(adapter: Any, home: Path) -> int:
    """Run queued deliveries for every profile whose slot is free.

    Returns the number of turns actually run. A slot held by any other turn (a
    desktop session, a card worker) is skipped, never fought for -- the holder
    drains the queue when it releases.

    ``home`` locates the root; the drainer enumerates the WHOLE roster (default
    + every named profile) because a peer delivery is admitted into its TARGET
    lane's own home (``_bot_send_home`` resolves the request-scoped profile to
    ``profiles/<lane>``), not the default home. Draining only ``home`` left a
    named lane's backlog invisible (the live sweep logged ``actions=0`` while
    two lanes held 13 queued records).

    Every lane holding a backlog is drained on its OWN task, concurrently. The
    roster used to be one sequential pass -- ``for lane: take its slot, run its
    turn`` -- so a lane whose turn was slow (a 28-minute delivery was observed
    live, or a lease wait, or a desktop-held slot) held the loop for the whole
    turn and every lane behind it went unserved, however idle and however deep
    its own backlog. Slots are independent (one lockfile per profile), so the
    lanes are independent too: each task claims and runs under its own lane's
    lock, and no lane can spend another lane's time. The roster start rotates
    one lane per tick, so the lane a full tick started last is the lane the next
    tick starts first.
    """
    from tools.bot_mode_probe import _hermes_root, _roster

    global _roster_offset

    root = _hermes_root(home)
    roster = _roster(root)
    n = len(roster) or 1
    offset = _roster_offset % n
    ordered = roster[offset:] + roster[:offset]
    _roster_offset = (offset + 1) % n

    # One task per (profile, home) pair holding a backlog, decided up front so
    # every lane is scheduled together and none waits for another to finish.
    lanes = [
        (profile_home, profile)
        for _name, profile_home in ordered
        for profile in delivery_queue.queued_target_profiles(profile_home)
    ]
    if not lanes:
        return 0

    async def lane_task(profile_home: Path, profile: str) -> int:
        try:
            return await _drain_profile(
                adapter, root=root, profile_home=profile_home, profile=profile
            )
        except asyncio.CancelledError:
            # Gateway shutdown: let the cancel propagate, taking this lane's turn
            # with it, rather than leaving a turn task running after the gather.
            raise
        except Exception:  # noqa: BLE001 - one lane must not strand the others
            logger.exception(
                "[api_server] lane drain failed (lane=%s home=%s)",
                profile,
                profile_home,
            )
            return 0

    # Each lane task returns its own subtotal: a shared counter would lose the
    # += of any lane that was awaiting its turn when another lane finished.
    return sum(
        await asyncio.gather(*(lane_task(h, p) for h, p in lanes))
    )


async def sweep_loop(adapter: Any) -> None:
    """Trigger 3: every ``sweep_seconds``, recover expiries then drain free slots."""
    from tools.bot_mode_probe import _default_home, _hermes_root, _roster

    home = Path(_default_home())
    root = _hermes_root(home)
    logger.info("[api_server] bot delivery drainer started (home=%s)", home)

    async def tick() -> None:
        # Recover orphaned claims / expire over-age records in EVERY lane's
        # home first, so the drain below sees the real queue (and never
        # expires a busy target's). The drainer then enumerates the same
        # roster, so a named lane's backlog is never invisible to the sweep.
        for _name, profile_home in _roster(root):
            delivery_queue.sweep_delivery_queue(profile_home)
        drained = await drain_once(adapter, home)
        if drained:
            log_delivery_event("sweep_drained", None, drained=drained)

    while True:
        try:
            # Tick BEFORE sleeping: a restart leaves its interrupted claims in
            # claimed/ with senders still waiting, so recovery has to fire on
            # start-up, not one sweep_seconds later.
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a sweep failure must never kill the loop
            logger.exception("[api_server] bot delivery sweep failed")
        await asyncio.sleep(delivery_queue.sweep_seconds())
