"""Tree-skew actor RETIREMENT: the doors that close a guard whose episode has CLEARED (DEC-3).

The retirement half of the dispatcher's tree-skew block lived inside
``hermes_cli/kanban_db_dispatch.py`` until card t_028ad743. It moved out for two reasons:

* the facade is over its size cap and may only go down, and
* retirement grew a THIRD responsibility -- BOUNDED retries with a durable, one-time give-up --
  which does not belong in the tick's spine.

WHAT THIS MODULE OWNS
---------------------
A card filed by :func:`hermes_cli.kanban_db_dispatch._file_tick_yield_actor` is a RECORD of one
stall episode. The first tick that serves normally again is PROOF the episode is over, so its
actors are closed here, deterministically, through the KERNEL's own doors (``unblock_task`` then
``complete_task``) -- never by writing ``status`` directly.

THREE SHAPES THE OLD DOORS COULD NOT CLOSE (the measured defect, card t_028ad743)
-------------------------------------------------------------------------------
1. ``scheduled`` / ``blocked``. ``complete_task`` refuses a card that is not in a completable
   status (``CompletionRefusal(cause='not_running')``). The broad door unblocked first; the narrow
   "the guard cleared" door did not, so every ``scheduled`` guard printed
     ``the cleared tree-skew guard t_XXXXXXXX was not closed (CompletionRefusal(cause='not_running'...))``
   on every tick, forever, and stayed live.
2. A card filed on the FALLBACK board (the ops board was unopenable). The narrow door reached it
   but could not close it; the broad door deliberately refuses to act off the actor board. Nothing
   could ever close it.
3. A card filed WITHOUT the ``tree_skew_actor_filed`` event (a legacy/fallback filing). The narrow
   door selects by that event, the broad one by an exact title -- so a card carrying our own
   idempotency key but neither marker was invisible to both and immortal.

IDENTITY IS THE KEY
-------------------
``idempotency_key LIKE 'tree-skew:%'`` is what ``_file_tick_yield_actor`` mints and nothing else
writes, so it -- not a title that a seat may edit, and not an event an older revision never
appended -- is how a card is recognised as ours. The liveness fence and the ``running`` exclusion
still decide whether it may be touched.

BORROWED NAMES ARE RESOLVED AT CALL TIME
----------------------------------------
``_kb``, ``_pid_liveness``, ``WORKER_DEAD``, ``_ops_board_connection``, ``_event_payload`` and
every ``TICK_YIELD_*`` constant are read through :func:`_facade` INSIDE each
call. The tests and the acceptance surfaces patch those on ``hermes_cli.kanban_db_dispatch`` (a
module-level copy would silently ignore the patch and act on the live fleet's board), and the
facade imports this module at import time, so a top-level import back would be a cycle.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Optional

#: Statuses a served tick may not touch: terminal for the purpose of a close, plus ``running``
#: (DEC-3.6 -- a card under a live claim is somebody's work, and a close is not ours to attempt).
_TERMINAL_OR_CLAIMED = ("done", "archived", "failed", "cancelled", "running")


def _facade():
    """The dispatcher module, at CALL time (see the module docstring)."""
    from hermes_cli import kanban_db_dispatch as facade

    return facade


def _db():
    """``hermes_cli.kanban_db`` at CALL time: a module-level import here would close a cycle.

    ``kanban_db`` imports a list of the dispatcher's names at its OWN tail, so the dispatcher and
    this sibling may only reach back into it after both modules are fully initialised.
    """
    from hermes_cli import kanban_db as kb

    return kb


def _tree_skew_actor_pid_from_key(key: str) -> Optional[int]:
    """The filing pid a LEGACY key carries, else ``None``.

    Legacy shape ``tree-skew:<sig>:<pid>:<ord>``. The content signature itself contains a colon
    (``tree-content:<hex>``), so the key is parsed from the TAIL: the last two fields are the
    ``pid``/``ord`` pair when BOTH are digits and the ordinal is small (an occurrence count, never
    an epoch). A WINDOW key (``tree-skew:<sig>:<first_tick>``) ends in a 10-digit epoch, which is
    not a plausible ordinal, so a window key never reads as legacy.
    """
    parts = str(key or "").split(":")
    if len(parts) < 4:
        return None
    tail_pid, ordinal = parts[-2], parts[-1]
    if not (tail_pid.isdigit() and ordinal.isdigit()):
        return None
    if int(ordinal) > 9999 or int(tail_pid) <= 0:
        return None
    return int(tail_pid)


def _latest_actor_payload(conn: sqlite3.Connection, task_id: str) -> dict:
    """The newest ``tree_skew_actor_filed`` payload for this card, or ``{}``.

    Absence is EXPECTED, not exceptional: a card filed by an older revision (or on the fallback
    board) may carry the key and no event, and it is still ours to retire.
    """
    try:
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? "
            "ORDER BY id DESC LIMIT 1", (task_id, _facade().TICK_YIELD_ACTOR_EVENT),
        ).fetchone()
    except Exception:  # health: allow BLE001 -- an unreadable log means "no evidence", not a crash
        return {}
    payload = _facade()._event_payload(row["payload"] if row is not None else None)
    return payload


def _tree_skew_actor_provenance(conn: sqlite3.Connection, task_id: str, key: str) -> dict:
    """``{"filing_pid", "escalation_ref"}`` for one actor card.

    The pid comes from the legacy key's own field first (it is what the card was MINTED with), then
    from the ``tree_skew_actor_filed`` event payload, else ``None`` -- which DEC-3.3 treats as
    retire-eligible ONLY because the caller is a gate-CLEAR tick and ``running`` is excluded.
    """
    info: dict = {"filing_pid": _tree_skew_actor_pid_from_key(key), "escalation_ref": None}
    payload: dict = {}
    try:
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? "
            "ORDER BY id DESC LIMIT 1", (task_id, _facade().TICK_YIELD_ACTOR_EVENT),
        ).fetchone()
    except Exception:  # health: allow BLE001 -- no log, no evidence; the key still parses
        row = None
    if row is not None:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if info["filing_pid"] is None:
        try:
            info["filing_pid"] = int(str(payload.get("boot_pid")))
        except (TypeError, ValueError):
            info["filing_pid"] = None
    ref = payload.get("escalation_ref")
    info["escalation_ref"] = str(ref) if ref else None
    return info


def _retire_event_count(conn: sqlite3.Connection, task_id: str, kind: str) -> int:
    """How many ``kind`` events this card carries (0 when the log cannot be read)."""
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = ?", (task_id, kind),
        ).fetchone()
    except Exception:  # health: allow BLE001 -- an unreadable log must never stop a sweep
        return 0
    try:
        return int(row[0]) if row is not None else 0
    except (TypeError, ValueError, IndexError):
        return 0


def _fenced_by_a_live_peer(provenance: dict, payload: dict) -> Optional[int]:
    """The filing pid when a LIVE foreign process still owns this card, else ``None``.

    The liveness fence DEC-3.3 applies: a card filed by a frozen peer that is still ALIVE belongs
    to that peer's episode, and retiring it would read green over a frozen gateway -- the exact
    false green this whole block exists to prevent.
    """
    f = _facade()
    frozen_pid = provenance.get("filing_pid")
    try:
        if payload.get("boot_pid") is not None:
            frozen_pid = int(payload["boot_pid"])
    except (TypeError, ValueError):
        pass
    if frozen_pid is None or frozen_pid == os.getpid():
        return None
    try:
        alive = f._pid_liveness(frozen_pid) != f.WORKER_DEAD
    except Exception:  # health: allow BLE001 -- an unprobeable pid is treated as ALIVE (fail closed)
        alive = True
    return frozen_pid if alive else None


def _record_retire_refusal(
    conn: sqlite3.Connection, task_id: str, *, reason: str, kind: str, detail: dict,
) -> None:
    """Write one refusal ``kind`` event on the card. Best effort: the close already failed."""
    try:
        with _db().write_txn(conn):
            _db()._append_event(conn, task_id, kind, {"reason": reason, **detail})
    except Exception as exc:  # health: allow BLE001 -- a lost audit row must not stop the tick
        _db()._log.warning(
            "kanban dispatcher: could not record %s on tree-skew card %s: %s", kind, task_id, exc,
        )


def _retired_result_line(key: str, provenance: dict) -> str:
    """The ONE wording every automatic close leaves in ``tasks.result``.

    Both doors write it (the narrow "the guard cleared" door and the broad actor sweep): a reader
    of the row must not have to know which door ran to see WHEN the episode was cleared and by
    whom -- and which card of which key it was.
    """
    cleared_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
    return (
        f"[retired] episode cleared at {cleared_at}: a normal dispatcher tick after the frozen "
        f"process this card named (pid {provenance.get('filing_pid')}) went away, so the reload "
        f"landed and the tree-skew gate is clear. retired_by_pid={os.getpid()} "
        f"filing_pid={provenance.get('filing_pid')} key={key}"
    )


def _retirement_close_kwargs(key: str, provenance: dict, payload: dict) -> dict:
    """The ONE declaration every automatic close carries: ``result``, ``summary``, the evidence.

    BOTH doors write it (card t_028ad743, review D1). The broad actor sweep used to pass
    ``result`` alone, and with the board's evidence gate at its default ``refuse`` the kernel
    answered ``CompletionRefusal(cause='no_evidence')`` -- so a card the sweep had CORRECTLY judged
    cleared could never be closed, and every refusal spent one of the card's bounded attempts on a
    defect of our own making. One helper, so the two doors cannot drift apart again.

    ``class='none'`` is the honest class: this is board repair with no run behind it. The proof is
    a verified normal dispatcher tick after the frozen pid stopped serving, and the kernel records
    the declaration as an ``evidence_none`` event on the card.
    """
    return {
        "result": _retired_result_line(key, provenance),
        "summary": (
            "Closed by the dispatcher's OWN tick: the tree-skew stall this card carried is"
            " over. A dispatcher process served a normal tick after the frozen process it"
            f" named (pid {provenance.get('filing_pid')}) went away, so the reload landed"
            " and the gate is clear -- no lane action is owed on this card."
        ),
        "metadata": {
            "evidence": {
                "class": "none",
                "why": (
                    "board repair with no run behind it: the proof is a verified normal "
                    "dispatcher tick after the frozen pid (proven dead) stopped serving"
                ),
            },
            "tree_skew_cleared": {
                "frozen_pid": provenance.get("filing_pid"),
                "disk_fingerprint": payload.get("disk_fingerprint"),
                "boot_fingerprint": payload.get("boot_fingerprint"),
                "observed_by_pid": os.getpid(),
                "at": int(time.time()),
            },
        },
    }


def _retire_one_tree_skew_actor(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    status: str,
    key: str,
    provenance: dict,
    reason: str,
    close_kwargs: dict,
    log_what: str,
    attempted: Optional[set] = None,
) -> str:
    """Close ONE actor card through the kernel's doors; bounded, and LOUD when it will not close.

    EVERY non-terminal status is taken to a shape the kernel can COMPLETE, using the kernel's own
    door for that shape -- never by writing ``status`` directly: ``todo`` through
    ``promote_task``, ``blocked``/``scheduled`` through ``unblock_task``, then ``complete_task``.
    The old narrow door took no such step, so a cleared guard in ``scheduled``/``blocked`` was
    refused with ``cause='not_running'`` on every tick and stayed live (card t_028ad743).

    THE BOUND: a refusal is recorded ON THE CARD, so it survives a respawned dispatcher. At
    ``TICK_YIELD_RETIRE_MAX_ATTEMPTS`` ONE ``tree_skew_actor_retire_failed`` event names the last
    refusal and the card leaves every sweep for good -- an audit finding instead of a per-tick
    siren. It counts the KERNEL's refusals, not our plumbing (review D1): ``attempted`` is the set
    the CALLER shares between both doors for one tick, so a card both doors can see (on a tick
    whose board IS the actor board) is tried ONCE and a refusal is not counted twice. Returns
    ``"retired"``, ``"refused"``, ``"failed"`` (gave up on this attempt), ``"gave_up"`` (a prior
    give-up owns it) or ``"deduped"`` (the other door already tried it this tick).
    """
    f = _facade()
    if _retire_event_count(conn, task_id, f.TICK_YIELD_RETIRE_FAILED_EVENT):
        return "gave_up"
    if attempted is not None:
        if task_id in attempted:
            return "deduped"
        attempted.add(task_id)
    if status == "todo":
        # ``todo`` is not completable and not unblockable: the kernel's door for it is
        # ``promote_task`` (refused while a parent is unfinished -- which is then a REAL refusal and
        # counts toward the bound).
        try:
            promoted, why = _db().promote_task(
                conn, task_id, actor="kanban-dispatcher",
                reason="tree-skew episode cleared: this guard is a record, not work",
            )
            if not promoted:
                _db()._log.warning(
                    "%s: cannot promote %s out of todo: %s", log_what, task_id, why,
                )
        except Exception as exc:  # health: allow BLE001 -- the close below is the real attempt
            _db()._log.warning("%s: could not promote %s out of todo: %s", log_what, task_id, exc)
    elif status in ("blocked", "scheduled"):
        try:
            _db().unblock_task(conn, task_id)
        except Exception as exc:  # health: allow BLE001 -- the close below is the real attempt
            _db()._log.warning("%s: could not unblock %s (%s): %s", log_what, task_id, status, exc)
    try:
        verdict = _db().complete_task(conn, task_id, fire_lifecycle_hook=False, **close_kwargs)
        ok = verdict is True
        cause = None if ok else (getattr(verdict, "cause", None) or "refused")
        detail = None if ok else (getattr(verdict, "detail", None) or str(verdict))
    except Exception as exc:  # health: allow BLE001 -- a close is never allowed to break a tick
        ok, cause, detail = False, "exception", str(exc)
    if not ok:
        attempts = _retire_event_count(conn, task_id, f.TICK_YIELD_RETIRE_REFUSED_EVENT) + 1
        gave_up = attempts >= int(f.TICK_YIELD_RETIRE_MAX_ATTEMPTS)
        kind = f.TICK_YIELD_RETIRE_FAILED_EVENT if gave_up else f.TICK_YIELD_RETIRE_REFUSED_EVENT
        _record_retire_refusal(conn, task_id, reason=reason, kind=kind, detail={
            "attempt": attempts,
            "cause": cause,
            "detail": detail,
            "status": status,
            "key": key,
            "retired_by_pid": os.getpid(),
            "filing_pid": provenance.get("filing_pid"),
            "at": int(time.time()),
        })
        if gave_up:
            _db()._log.error(
                "%s: GIVING UP on %s after %d attempts (status=%s, last refusal %s=%s). The card "
                "keeps a %s event and leaves the sweep; this is now a board finding, not a tick",
                log_what, task_id, attempts, status, cause, detail, kind,
            )
            return "failed"
        _db()._log.warning(
            "%s: could not retire %s (attempt %d/%d): %s=%s -- the card keeps its status and the "
            "sweep tries again next tick", log_what, task_id, attempts,
            int(f.TICK_YIELD_RETIRE_MAX_ATTEMPTS), cause, detail,
        )
        return "refused"
    try:
        with _db().write_txn(conn):
            _db()._append_event(conn, task_id, f.TICK_YIELD_RETIRED_EVENT, {
                "reason": reason,
                "retired_by_pid": os.getpid(),
                "filing_pid": provenance.get("filing_pid"),
                "key": key,
                "escalation_ref": provenance.get("escalation_ref"),
            })
    except Exception as exc:  # health: allow BLE001 -- the close landed; the note is best-effort
        _db()._log.warning(
            "%s: retired %s but could not record the retirement event: %s", log_what, task_id, exc,
        )
    return "retired"


def _retire_tree_skew_actors(
    conn: sqlite3.Connection, *, board: Optional[str] = None, attempted: Optional[set] = None,
) -> int:
    """Close every live actor card on the actor board whose episode has CLEARED (DEC-3).

    A tick that serves normally is PROOF the episode is over. Selection is bounded
    (``TICK_YIELD_RETIRE_BATCH``) and per tick, so a 51-card backlog drains over a couple of ticks
    instead of walking the board in one.

    Selection is by the KEY, not by the exact title it was filed with: a card a seat re-titled is
    still the record of our stall, and requiring ``title = TICK_YIELD_ACTOR_TITLE`` was one of the
    two reasons a disguised guard was immortal (card t_028ad743).

    ``attempted`` is the per-tick set shared with the narrow door (review D1): the run that calls
    both doors hands the same set in, so a card both of them can see is attempted once, not twice.
    """
    f = _facade()
    target = f.actor_board_slug(conn)
    actor_conn, filed_on, close_it = f._ops_board_connection(conn)
    if filed_on != target:
        # The actor board could not be opened. Retiring on the board that merely ticked would be a
        # different claim (those cards are not ours to close), so say so and serve the tick.
        _db()._log.warning(
            "kanban dispatcher: tree-skew actor retirement skipped -- the actor board %s is "
            "unavailable (fell back to %s)", target, filed_on,
        )
        if close_it:
            try:
                actor_conn.close()
            except Exception as exc:  # health: allow BLE001 -- closing a scratch connection never matters to a tick
                _db()._log.debug("kanban dispatcher: closing the actor board failed: %s", exc)
        return 0
    counts = {"retired": 0, "refused": 0, "skipped": 0, "failed": 0, "gave_up": 0, "deduped": 0}
    try:
        placeholders = ", ".join("?" for _ in f.TICK_YIELD_RETIRE_SKIP_STATUSES)
        rows = actor_conn.execute(
            "SELECT id, status, idempotency_key FROM tasks "
            "WHERE created_by = 'kanban-dispatcher' AND idempotency_key LIKE ? || '%' "
            f"AND status NOT IN ({placeholders}) "
            "ORDER BY rowid LIMIT ?",
            (f.TICK_YIELD_ACTOR_IDEMPOTENCY_PREFIX, *f.TICK_YIELD_RETIRE_SKIP_STATUSES,
             f.TICK_YIELD_RETIRE_BATCH),
        ).fetchall()
        for row in rows:
            task_id = row["id"]
            key = row["idempotency_key"] or ""
            payload = _latest_actor_payload(actor_conn, task_id)
            provenance = _tree_skew_actor_provenance(actor_conn, task_id, key)
            if _fenced_by_a_live_peer(provenance, payload) is not None:
                counts["skipped"] += 1
                continue
            outcome = _retire_one_tree_skew_actor(
                actor_conn,
                task_id,
                status=row["status"],
                key=key,
                provenance=provenance,
                reason="gate-clear",
                log_what="kanban dispatcher: tree-skew actor",
                close_kwargs=_retirement_close_kwargs(key, provenance, payload),
                attempted=attempted,
            )
            counts[outcome] = counts.get(outcome, 0) + 1
    except Exception as exc:  # health: allow BLE001 -- retirement must never take a tick down
        _db()._log.warning("kanban dispatcher: tree-skew actor retirement failed: %s", exc)
    finally:
        if close_it:
            try:
                actor_conn.close()
            except Exception as exc:  # health: allow BLE001 -- closing a scratch connection never matters to a tick
                _db()._log.debug("kanban dispatcher: closing the actor board failed: %s", exc)
    if any(counts.values()):
        _db()._log.info(
            "kanban dispatcher: tree-skew actor retirement on %s: retired=%d refused=%d "
            "skipped_live=%d gave_up_now=%d already_given_up=%d deduped=%d", filed_on,
            counts["retired"], counts["refused"], counts["skipped"], counts["failed"],
            counts["gave_up"], counts["deduped"],
        )
    return counts["retired"]


def _close_cleared_tick_yield_cards(
    conn: sqlite3.Connection, *, attempted: Optional[set] = None,
) -> list:
    """Close every guard card whose stall is OVER, on the proof that THIS tick is serving.

    "Cleared" is never inferred from the ABSENCE of refusals -- the dispatcher that would refuse is
    precisely the one that is gone. The PROOF is the caller's placement (card t_028ad743): this runs
    only after ``_raise_if_tree_skewed`` has passed (the tree this process imported IS the checkout
    on disk) and only while the board's dispatch lock is held, i.e. on a tick that is serving. A
    REFUSED tick never gets here, and a lock-skipped tick never gets here.

    IT MUST RUN BEFORE THE TICK CLAIMS ANYTHING. Placed after the tick body (where it used to be),
    the tick's own reconcile released and CLAIMED a fallback-filed guard first: the card came back
    ``running``, the kernel refused the close, and the duplicate re-dispatched to its lane on every
    tick while never being retired.

    TWO boards, two different claims, both proof-driven:

    * the OPS board -- where the acting cards are filed; and
    * the board that TICKED, for cards carrying our own ``tree-skew:`` key. A guard filed on the
      FALLBACK board (the ops board was unopenable) is ours by its key, and before card t_028ad743
      the narrow door was the only one that could reach it -- and could not close it, so nothing
      ever did.

    ``running`` cards are excluded from selection outright: a live claim is somebody's work, and
    ``complete_task`` would refuse it anyway (which must never be counted as a card's failed
    retirement attempt).

    ``attempted`` is the per-tick set shared with the broad actor sweep (review D1), so both doors
    together make at most ONE attempt per card per tick.
    """
    f = _facade()
    closed: list = []
    swept: set = set()
    ops_conn, ops_board, close_it = f._ops_board_connection(conn)
    try:
        closed.extend(_close_cleared_cards_on(ops_conn, attempted=attempted))
        if ops_board:
            swept.add(ops_board)
    finally:
        if close_it:
            try:
                ops_conn.close()
            except Exception as exc:  # health: allow BLE001 -- closing a scratch connection never matters to a tick
                _db()._log.debug("kanban dispatcher: closing the ops board failed: %s", exc)
    try:
        ticking_board = _db().board_for_connection(conn)
    except Exception as exc:  # health: allow BLE001 -- an unnamed board means "skip the extra sweep"
        ticking_board = None
        _db()._log.warning(
            "kanban dispatcher: cannot name the board that ticked (%s); only the ops board %s was "
            "swept for cleared guards", exc, ops_board,
        )
    if ticking_board and ticking_board not in swept:
        closed.extend(_close_cleared_cards_on(conn, attempted=attempted))
    return closed


def _close_cleared_cards_on(
    conn: sqlite3.Connection, *, attempted: Optional[set] = None,
) -> list:
    """The body of :func:`_close_cleared_tick_yield_cards` against one board.

    Selection is the card's own KEY (``tree-skew:`` -- what ``_file_tick_yield_actor`` mints and
    nothing else writes) OR the ``tree_skew_actor_filed`` event, so a card filed without the event
    (an older revision, the fallback board) is still recognised. Cards carrying a
    ``tree_skew_actor_retire_failed`` event have already GIVEN UP and are not touched again.
    """
    f = _facade()
    try:
        rows = conn.execute(
            "SELECT t.id AS id, t.status AS status, t.idempotency_key AS key, t.rowid AS rowid "
            "FROM tasks t "
            "WHERE t.status NOT IN (?, ?, ?, ?, ?) AND ("
            "  t.idempotency_key LIKE ? || '%' OR EXISTS ("
            "    SELECT 1 FROM task_events e WHERE e.task_id = t.id AND e.kind = ?)) "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM task_events x WHERE x.task_id = t.id AND x.kind = ?) "
            "ORDER BY t.rowid LIMIT ?",
            (*_TERMINAL_OR_CLAIMED, f.TICK_YIELD_ACTOR_IDEMPOTENCY_PREFIX,
             f.TICK_YIELD_ACTOR_EVENT, f.TICK_YIELD_RETIRE_FAILED_EVENT,
             f.TICK_YIELD_RETIRE_BATCH),
        ).fetchall()
    except Exception as exc:  # health: allow BLE001 -- no board history, nothing to close
        _db()._log.debug("kanban dispatcher: cleared-guard sweep could not read the board: %s", exc)
        return []
    closed = []
    for row in rows:
        card_id, status, key = row["id"], row["status"], row["key"] or ""
        payload = _latest_actor_payload(conn, card_id)
        provenance = _tree_skew_actor_provenance(conn, card_id, key)
        if _fenced_by_a_live_peer(provenance, payload) is not None:
            continue
        outcome = _retire_one_tree_skew_actor(
            conn,
            card_id,
            status=status,
            key=key,
            provenance=provenance,
            reason="gate-clear",
            log_what="kanban dispatcher: the cleared tree-skew guard",
            close_kwargs=_retirement_close_kwargs(key, provenance, payload),
            attempted=attempted,
        )
        if outcome != "retired":
            continue
        closed.append(card_id)
        _db()._log.info(
            "kanban dispatcher: closed tree-skew guard %s (was %s) -- a normal tick after its "
            "filing pid cleared it", card_id, status,
        )
    return closed


def _substrate_close_kwargs(record: dict) -> dict:
    """The declaration every automatic substrate-guard close carries (same shape as the skew one).

    ``class='none'`` is the honest class: board repair with no run behind it. The proof is a
    verified normal tick -- this sweep only runs from a tick that PASSED the substrate door and
    holds the board's dispatch lock -- plus the recovery receipt the design requires
    (``cleared_at`` / ``duration_s`` / the cleared matrix), which this close quotes verbatim.
    """
    matrix = record.get("cleared_matrix") or record.get("matrix") or []
    released = record.get("released") or []
    return {
        "result": (
            "substrate readable again -- the admission guard was cleared by a normal tick"
        ),
        "summary": (
            "Closed by the dispatcher's OWN tick: the substrate admission episode this card "
            f"carried is over (cleared_at {record.get('cleared_at')}, "
            f"duration {record.get('duration_s')}s). A tick probed every declared root, every "
            "reading came back ok, and work is being admitted again -- no lane action is owed on "
            "this card."
        ),
        "metadata": {
            "evidence": {
                "class": "none",
                "why": (
                    "board repair with no run behind it: the proof is a verified normal tick "
                    "whose own probe cleared the substrate, recorded as cleared_at/duration_s "
                    "on the boot-volume authority record"
                ),
            },
            "substrate_recovery": {
                "cleared_at": record.get("cleared_at"),
                "duration_s": record.get("duration_s"),
                "episode": record.get("episode"),
                "sig": record.get("sig"),
                "cleared_matrix": matrix,
                "released": released,
                "observed_by_pid": os.getpid(),
                "at": int(time.time()),
            },
        },
    }


def _close_cleared_substrate_cards(conn: sqlite3.Connection, *, attempted: Optional[set] = None) -> list:
    """Close every substrate-admission guard card on ONE board whose episode is over.

    Selection is the card's own KEY (``substrate-denied:`` -- what ``_file_tick_yield_actor`` mints
    for this kind and nothing else writes) OR the ``substrate_admission_actor_filed`` event, so a
    card filed without the event is still recognised. ``running`` is excluded outright (a live
    claim is somebody's work, and the kernel would refuse the close anyway), and a card carrying
    ``..._retire_failed`` has already GIVEN UP and is not touched again.
    """
    f = _facade()
    try:
        rows = conn.execute(
            "SELECT t.id AS id, t.status AS status, t.idempotency_key AS key, t.rowid AS rowid "
            "FROM tasks t "
            "WHERE t.status NOT IN (?, ?, ?, ?, ?) AND ("
            "  t.idempotency_key LIKE ? || '%' OR EXISTS ("
            "    SELECT 1 FROM task_events e WHERE e.task_id = t.id AND e.kind = ?)) "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM task_events x WHERE x.task_id = t.id AND x.kind = ?) "
            "ORDER BY t.rowid LIMIT ?",
            (*_TERMINAL_OR_CLAIMED, f.SUBSTRATE_ADMISSION_ACTOR_IDEMPOTENCY_PREFIX,
             f.SUBSTRATE_ADMISSION_ACTOR_EVENT, f.TICK_YIELD_RETIRE_FAILED_EVENT,
             f.TICK_YIELD_RETIRE_BATCH),
        ).fetchall()
    except Exception as exc:  # health: allow BLE001 -- no board history, nothing to close
        _db()._log.debug(
            "kanban dispatcher: substrate-guard sweep could not read the board: %s", exc,
        )
        return []
    if not rows:
        return []
    record = f._sa.read_status()
    close_kwargs = _substrate_close_kwargs(record)
    closed: list = []
    for row in rows:
        card_id, status, key = row["id"], row["status"], row["key"] or ""
        provenance = _tree_skew_actor_provenance(conn, card_id, key)
        outcome = _retire_one_tree_skew_actor(
            conn,
            card_id,
            status=status,
            key=key,
            provenance=provenance,
            reason="substrate-clear",
            log_what="kanban dispatcher: the cleared substrate-admission guard",
            close_kwargs=close_kwargs,
            attempted=attempted,
        )
        if outcome != "retired":
            continue
        closed.append(card_id)
        _db()._log.info(
            "kanban dispatcher: closed substrate-admission guard %s (was %s) -- a normal tick "
            "probed the substrate and found every root readable", card_id, status,
        )
    return closed


def _retire_cleared_substrate_cards(conn: sqlite3.Connection, *, board: Optional[str] = None) -> list:
    """DOOR 3's retirement (design §3.7, card ``t_0ae49984``): close the guards, write the receipt.

    Runs from ``dispatch_once`` beside the skew retirement, under the dispatch lock and BEFORE the
    claim phase -- the same placement proof: a tick that reached here passed the substrate door, so
    the episode is over. Cards are CLOSED FIRST so the recovery receipt the record carries can name
    what was released; the record is only cleared when something was actually retired (or when the
    record itself is stale), never inferred from the absence of refusals.

    THE STALE CLAUSE (review round 1, card ``t_253a5e55``). A ``yielded: true`` record nobody has
    rewritten for a whole ``STALE_AFTER`` window is not a live episode: its writer stopped while
    THIS tick probed healthy. That shape must not latch, and only this door may break it --
    :func:`substrate_access.heartbeat` is a no-op on ANY open episode precisely so it cannot
    pre-empt the act below. The sibling marker ``dispatcher_substrate_yield.json`` is ABSENT in the
    plainest case (an episode that filed no card: measured live 2026-10-29), and
    ``_clear_tick_yield_status`` reads that sibling FIRST, so it answers ``False`` and no receipt is
    ever written; the receipt is therefore written here, off the record's own staleness, naming the
    cards this sweep actually released (``released: []`` when the episode filed none -- the latch is
    broken, and nothing is claimed as released).

    Fault-isolated: a close that raises is logged and the tick carries on.
    """
    f = _facade()
    record = f._sa.read_status()
    if not record.get("yielded"):
        return []
    stale = f._sa.episode_stale(record)
    attempted: set = set()
    closed: list = []
    ops_conn, ops_board, close_it = f._ops_board_connection(conn)
    try:
        closed.extend(_close_cleared_substrate_cards(ops_conn, attempted=attempted))
    except Exception as exc:  # health: allow BLE001 -- a close is never allowed to break a tick
        _db()._log.warning(
            "kanban dispatcher: substrate guard close failed on %s: %s", board, exc, exc_info=True,
        )
    finally:
        if close_it:
            try:
                ops_conn.close()
            except Exception:  # health: allow BLE001 -- closing a scratch connection never matters
                pass
    try:
        ticking_board = _db().board_for_connection(conn)
    except Exception:  # health: allow BLE001 -- an unnamed board means "skip the extra sweep"
        ticking_board = None
    if ticking_board and ticking_board != ops_board:
        try:
            closed.extend(_close_cleared_substrate_cards(conn, attempted=attempted))
        except Exception as exc:  # health: allow BLE001
            _db()._log.warning(
                "kanban dispatcher: substrate guard close failed on ticking board %s: %s",
                ticking_board, exc,
            )
    retired = f._clear_tick_yield_status(kind=f.SUBSTRATE_ADMISSION_KIND, released=closed)
    if stale and not retired:
        # The record IS the latch here: it is flagged and nothing has refreshed it for the reader's
        # whole window, while this tick just passed the substrate door. Break it with the receipt
        # (never a bare overwrite), naming what this sweep released.
        #
        # ``retired`` is False for two reasons and they are ONE finding: there is no sibling marker
        # at all (the measured shape), or the sibling names a writer that still looks alive. Either
        # way the RECORD has not been rewritten for the whole window, so that writer is not ticking,
        # and this tick's own healthy probe is the proof the episode is over.
        try:
            f._sa.record_cleared(door="dispatcher", released=list(closed))
            _db()._log.info(
                "kanban dispatcher: closed the STALE substrate episode on %s -- released %d guard "
                "card(s); nothing had rewritten the record for the reader's staleness window",
                board, len(closed),
            )
        except Exception as exc:  # health: allow BLE001 -- evidence, never a tick's work
            _db()._log.warning(
                "kanban dispatcher: could not write the stale episode's recovery receipt: %s", exc,
            )
    return closed


def _retire_cleared_tree_skew_cards(conn: sqlite3.Connection, *, board: Optional[str] = None) -> list:
    """THE one entry the tick calls: retire the guards whose episode is over, before the claim.

    Both doors run here, each with its own fault isolation -- neither may take a tick down:

    * the NARROW door (a guard whose frozen process is gone) on the ops board AND on the board
      that is ticking, and
    * the BROAD actor sweep on the actor board, when this tick just retired an episode record or
      when this tick IS the actor board (whose live backlog long outlived the episode it records).

    The CALLER owns the proof: this is called only from a tick that passed ``_raise_if_tree_skewed``
    and holds the board's dispatch lock, and BEFORE the tick claims or spawns anything (card
    t_028ad743 -- after the claim phase a fallback-filed guard came back ``running`` and no door
    could ever close it).

    ONE per-tick ``attempted`` set is shared by both doors (review D1): where the doors overlap --
    a tick whose board IS the actor board -- a card gets ONE attempt, so ``..._retire_refused``
    counts the kernel's refusals and the give-up bound is spent at the rate it names, not twice.
    """
    f = _facade()
    attempted: set = set()
    cleared = f._clear_tick_yield_status()
    try:
        closed = _close_cleared_tick_yield_cards(conn, attempted=attempted)
    except Exception as exc:  # health: allow BLE001 -- a close is never allowed to break a tick
        _db()._log.warning(
            "kanban dispatcher: tree-skew guard close failed on %s: %s", board, exc, exc_info=True,
        )
        closed = []
    if board == f.actor_board_slug(conn) or cleared:
        try:
            _retire_tree_skew_actors(conn, board=board, attempted=attempted)
        except Exception as exc:  # health: allow BLE001 -- retirement never breaks a tick
            _db()._log.warning(
                "kanban dispatcher: tree-skew actor retirement failed on %s: %s", board, exc,
            )
    return closed
