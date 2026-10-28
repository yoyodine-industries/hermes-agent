"""ONE seat-scoped exit door for the statuses the ordinary completion door cannot reach.

THE CLASS (ruling ``t_08079e45``). A lane lead could be handed a ruling on a card sitting in
``triage``, ``todo`` or ``scheduled`` and had no act available to it: ``complete_task``
accepts ``running``/``ready``/``blocked``/``review`` only, ``promote_task`` and
``specify_triage_task`` were reachable from an operator CLI only, and a worker calling them
from its own session hit the delegated-child fence. The card sat until a human happened past
it, and the lane reported "blocked" against a board that was not blocked — it was un-doored.

THE DOOR. :func:`adjudicate` takes the caller's own card and run as provenance and performs,
on ANOTHER card, the one exit that card's status has:

===============  =============================================  =====================
``triage``       ``specify_triage_task`` -> ``promote_task``     ``close`` / ``promote``
``todo``         ``promote_task``                                ``close`` / ``promote``
``scheduled``    ``unblock_task``                                ``close`` / ``promote``
===============  =============================================  =====================

then ``claim_task`` and ``complete_task`` when the disposition is ``close``. Every step is a
kernel call through a live row, so the door adds no status writer of its own and a card whose
state moved under it is caught by the kernel, not by a losing UPDATE.

WHY THE SEQUENCE RELEASES BEFORE IT ACTS: a card that is promoted or closed while still fenced
(``scheduled``) or unspecified (``triage``) is a state the board cannot show. The release is
the receipt — the ``adjudicated`` event records the status the card was actually in.

WHY ``claim_task`` IS NOT OPTIONAL: ``complete_task`` will complete a ``ready`` card without an
owner, which is right for a human CLI and wrong here. The door claims first and passes
``expected_run_id``, so the close is proof-of-ownership, not a race.

REFUSALS are data (:class:`AdjudicationRefused`, a ``ValueError`` so the tool layer renders it
as a recoverable tool error), in a fixed precedence: ``no_caller``, ``own_card``,
``caller_run_not_current``, ``target_unknown``, ``not_a_door_source``, ``status_moved``,
``lane_mismatch``, ``claim_lost``, ``gated`` (not a refusal — a receipt carrying the blocking
parents), ``gate_refused``. A refusal before the claim writes nothing; a refusal after it
closes the run it opened and returns the card to ``ready``.
"""
from __future__ import annotations

import time
from typing import Any, NoReturn, Optional

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_exits import DOOR_SOURCES

__all__ = ["ACTOR", "ACTOR_PREFIX", "ROUTES", "AdjudicationRefused", "adjudicate"]

#: Author recorded on the comment the door writes on the target.
ACTOR = "hermes-kanban-adjudicate"

#: The caller's card is recorded on every write as ``adjudicate:<caller card>`` — the claimer
#: and the promote actor both carry it, so a stale claim names the seat that took it.
ACTOR_PREFIX = "adjudicate:"

DISPOSITIONS = ("close", "promote")

#: Statuses a caller may name in ``expect_status``; the door owns nothing else.
DOOR_STATUSES = tuple(sorted(DOOR_SOURCES))


class AdjudicationRefused(ValueError):
    """A refusal with a machine-readable ``cause``; the message is for a human."""

    CAUSES = (
        "no_caller",
        "own_card",
        "caller_run_not_current",
        "target_unknown",
        "not_a_door_source",
        "status_moved",
        "lane_mismatch",
        "claim_lost",
        "gate_refused",
    )

    def __init__(self, cause: str, detail: str = ""):
        if cause not in self.CAUSES:
            raise ValueError(f"unknown adjudication refusal cause: {cause!r}")
        self.cause = cause
        self.detail = detail
        super().__init__(f"{cause}: {detail}" if detail else cause)


class _Gated:
    """``promote``/claim left the card in ``todo``: an unfinished parent is the reason.

    Not a refusal — the card took the only move available to it, and the caller is owed the
    parent ids so it can say what it is waiting on instead of reporting a bare failure.
    """

    def __init__(self, parents: list[tuple[str, str]]):
        self.parents = parents


def _refuse(cause: str, detail: str = "") -> NoReturn:
    raise AdjudicationRefused(cause, detail)


def _row(conn, task_id: str):
    return conn.execute(
        "SELECT id, status, project_id, tenant, claim_lock, claim_expires, current_run_id "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()


def _status(conn, task_id: str) -> str:
    return kb._task_status(conn, task_id) or ""


def _assert_caller(conn, caller_task_id: Optional[str],
                   caller_run_id: Optional[int]) -> str:
    """Ladder steps 1-3: the caller must be a live run on a card that is not the target."""
    if not caller_task_id:
        _refuse(
            "no_caller",
            "this door writes against the caller's own card and run; a session with no "
            "HERMES_KANBAN_TASK (an operator shell, a delegate_task child, a cron probe) has "
            "neither, and guessing an author is how an audit stops being one",
        )
    caller = _row(conn, caller_task_id)
    if caller is None:
        _refuse("caller_run_not_current", f"caller card {caller_task_id} is not on this board")
    if caller["status"] != "running":
        _refuse(
            "caller_run_not_current",
            f"caller card {caller_task_id} is {caller['status']!r}, not 'running': only a "
            f"live run can adjudicate another card",
        )
    if caller_run_id is None or caller["current_run_id"] != caller_run_id:
        _refuse(
            "caller_run_not_current",
            f"caller card {caller_task_id} is not owned by run {caller_run_id!r} "
            f"(current: {caller['current_run_id']!r})",
        )
    return caller_task_id


def _assert_target(conn, target: str, expect_status: str):
    """Ladder steps 4-6: the target exists, its status is the door's, and it has not moved."""
    target_row = _row(conn, target)
    if target_row is None:
        _refuse("target_unknown", f"no card {target} on this board")
    if expect_status not in DOOR_SOURCES:
        _refuse(
            "not_a_door_source",
            f"{expect_status!r} is not a status this door owns (it owns "
            f"{', '.join(DOOR_STATUSES)}); a {expect_status!r} card is closed with "
            f"kanban_complete, which already accepts it",
        )
    if target_row["status"] != expect_status:
        _refuse("status_moved", f"{target} is {target_row['status']!r} now, not {expect_status!r}")
    return target_row


def _assert_same_lane(caller_row, target_row) -> None:
    """Lane fields (``project_id``, ``tenant``) must agree when BOTH ends declare them.

    NULL never masks a mismatch: an unlinked fixture card and a linked project card share a
    lane (the fixture IS that project's work), while two different projects never do.
    """
    for field in ("project_id", "tenant"):
        mine, theirs = caller_row[field], target_row[field]
        if mine and theirs and mine != theirs:
            _refuse(
                "lane_mismatch",
                f"caller {field} {mine!r} and target {field} {theirs!r} differ; a seat may "
                f"only adjudicate inside its own lane",
            )


def _assert_no_live_claim(target_row) -> None:
    """Ladder step 8: never adjudicate a card another seat is working.

    Checked BEFORE the release, so the refusal writes nothing at all — a door that releases a
    card it cannot take is worse than one that refuses, because the release is state the caller
    cannot undo.
    """
    if not target_row["claim_lock"]:
        return
    expires = target_row["claim_expires"]
    if expires is None or expires > int(time.time()):
        _refuse(
            "claim_lost",
            f"{target_row['id']} carries a live claim ({target_row['claim_lock']!r}); this "
            f"door never pre-empts a seat that is already working the card",
        )


def _release(conn, target: str, status: str, *, title, body) -> None:
    """Move the card out of the status the completion door cannot reach.

    ``specify_triage_task`` and ``unblock_task`` are the kernel's own transitions, so the audit
    trail (``specified`` / ``unblocked``) is theirs and the door adds no second story.
    """
    if status == "triage":
        if not kb.specify_triage_task(conn, target, title=title, body=body, author=ACTOR):
            _refuse("status_moved", f"{target} left 'triage' before it could be specified")
    elif status == "scheduled":
        if not kb.unblock_task(conn, target):
            _refuse("status_moved", f"{target} left 'scheduled' before it could be released")


def _land_ready(conn, target: str, status: str, *, caller: str, title, body):
    """Release, then promote until the card is ``ready``. ``_Gated`` when a parent blocks it."""
    _release(conn, target, status, title=title, body=body)
    now = _status(conn, target)
    if now == "ready":
        # ``specify_triage_task`` runs ``recompute_ready`` itself: a parent-free specified card
        # is already dispatchable, and a second promote would refuse it.
        return "ready"
    if now != "todo":
        _refuse("status_moved", f"{target} landed in {now!r} after the release, not 'todo'")
    return _promote(conn, target, caller=caller)


def _promote(conn, target: str, *, caller: str):
    """``todo`` -> ``ready`` through the kernel's own promotion, or ``_Gated``."""
    ok, why = kb.promote_task(conn, target, actor=f"{ACTOR_PREFIX}{caller}")

    if ok:
        return "ready"
    parents = kb.unsatisfied_parents(conn, target)
    if parents and _status(conn, target) == "todo":
        return _Gated(parents)
    _refuse("status_moved", f"promote refused {target}: {why}")


def _claim(conn, target: str, *, caller: str):
    """Claim the card for this door and return its run id, or ``_Gated``/``claim_lost``."""
    if kb.claim_task(conn, target, claimer=f"{ACTOR_PREFIX}{caller}") is None:
        parents = kb.unsatisfied_parents(conn, target)
        if parents and _status(conn, target) == "todo":
            return _Gated(parents)
        _refuse(
            "claim_lost",
            f"{target} is no longer claimable (status {_status(conn, target)!r}); another "
            f"seat is working it, and this door never pre-empts a live claim",
        )
    run_id = kb._current_run_id(conn, target)
    if run_id is None:
        _refuse("claim_lost", f"{target} was claimed but carries no run id")
    return run_id


def _unwind(conn, target: str, *, caller: str, caller_run_id: Optional[int],
            disposition: str, from_status: str, detail: str) -> None:
    """A downstream gate refused the close: undo the claim in ONE txn, audit on the caller.

    Leaving the card ``running`` under a dead run would strand it off the dispatch queue, and
    an anonymous refusal is the silent-failure shape this class exists to end.
    """
    with kb.write_txn(conn):
        kb._end_run(conn, target, outcome="refused", status="ready", summary=detail)
        conn.execute(
            "UPDATE tasks SET status = 'ready', claim_lock = NULL, claim_expires = NULL, "
            "worker_pid = NULL, worker_started_at = NULL "
            "WHERE id = ? AND status = 'running'",
            (target,),
        )
        kb._append_event(conn, caller, "adjudication_refused", {
            "target_task_id": target,
            "disposition": disposition,
            "from_status": from_status,
            "cause": detail,
            "caller_run_id": caller_run_id,
            "at": int(time.time()),
        })


def _audit(conn, target: str, *, disposition: str, from_status: str, to_status: str,
           caller: str, caller_run_id: Optional[int], reason: str,
           gated_parents: Optional[list[tuple[str, str]]] = None) -> None:
    """The target's record of the act: one ``adjudicated`` event, one comment quoting it."""
    payload: dict[str, Any] = {
        "disposition": disposition,
        "from_status": from_status,
        "to_status": to_status,
        "caller_task_id": caller,
        "caller_run_id": caller_run_id,
        "reason": reason,
    }
    if gated_parents:
        payload["blocking_parents"] = [tid for tid, _status_ in gated_parents]
    with kb.write_txn(conn):
        kb._append_event(conn, target, "adjudicated", payload)
    text = (
        f"Adjudicated: {disposition} {from_status} -> {to_status} by `{caller}` "
        f"(run {caller_run_id}). Reason: {reason}"
    )
    if gated_parents:
        waiting = ", ".join(f"`{tid}` ({status})" for tid, status in gated_parents)
        text += f"\n\nStill gated by: {waiting}"
    kb.add_comment(conn, target, ACTOR, text)


def _receipt(target: str, disposition: str, from_status: str, caller: str,
             caller_run_id: Optional[int], *, to_status: str,
             run_id: Optional[int] = None,
             gated: Optional[list[tuple[str, str]]] = None) -> dict:
    return {
        "target_task_id": target,
        "disposition": disposition,
        "from_status": from_status,
        "to_status": to_status,
        "caller_task_id": caller,
        "caller_run_id": caller_run_id,
        "run_id": run_id,
        "gated": bool(gated),
        "parents": [tid for tid, _status_ in (gated or [])],
    }


def _route(conn, target: str, status: str, *, caller: str, title, body):
    """Every routed status lands through the same release -> promote prologue."""
    return _land_ready(conn, target, status, caller=caller, title=title, body=body)


#: status -> the release route. The keys are the door's remit, checked at IMPORT below.
ROUTES = {
    "triage": _route,
    "todo": _route,
    "scheduled": _route,
}

# A new status that is neither completable nor terminal, but has no route here, would be born
# un-doored — the exact defect this card closes. Raised rather than asserted so ``python -O``
# cannot strip the rail.
if set(ROUTES) != DOOR_SOURCES:
    raise RuntimeError(
        "kanban_adjudicate.ROUTES does not match kanban_exits.DOOR_SOURCES: "
        f"unrouted={sorted(DOOR_SOURCES - set(ROUTES))} "
        f"stale={sorted(set(ROUTES) - DOOR_SOURCES)}"
    )


def adjudicate(
    conn,
    target_task_id: str,
    *,
    disposition: str,
    expect_status: str,
    reason: str,
    result: Optional[str] = None,
    title: Optional[str] = None,
    body: Optional[str] = None,
    caller_task_id: Optional[str] = None,
    caller_run_id: Optional[int] = None,
) -> dict:
    """Exit ``target_task_id`` out of ``expect_status`` on behalf of the caller's live run.

    Returns the receipt (also written to the target as an ``adjudicated`` event). Raises
    :class:`AdjudicationRefused` on every refusal in the ladder.
    """
    if disposition not in DISPOSITIONS:
        raise ValueError(f"disposition must be one of {DISPOSITIONS}, got {disposition!r}")
    if not (reason or "").strip():
        raise ValueError("reason is required: the adjudication is an audit record, not a flag")

    caller = _assert_caller(conn, caller_task_id, caller_run_id)
    if target_task_id == caller:
        _refuse(
            "own_card",
            "a card adjudicates another card, never itself; a lane that may close its own "
            "card has no second pair of eyes",
        )
    target_row = _assert_target(conn, target_task_id, expect_status)
    _assert_same_lane(_row(conn, caller), target_row)
    _assert_no_live_claim(target_row)

    if disposition == "promote":
        # Never claimed: a promoted card belongs to the dispatch queue, not to this caller.
        landed = ROUTES[expect_status](conn, target_task_id, expect_status, caller=caller,
                                       title=title, body=body)
        gated = landed.parents if isinstance(landed, _Gated) else None
        to_status = "todo" if gated else "ready"
        _audit(conn, target_task_id, disposition=disposition, from_status=expect_status,
               to_status=to_status, caller=caller, caller_run_id=caller_run_id,
               reason=reason, gated_parents=gated)
        return _receipt(target_task_id, disposition, expect_status, caller, caller_run_id,
                        to_status=to_status, gated=gated)

    landed = ROUTES[expect_status](conn, target_task_id, expect_status, caller=caller,
                                   title=title, body=body)
    if isinstance(landed, _Gated):
        # A parent is open: the close cannot happen, but the card did take the release.
        _audit(conn, target_task_id, disposition=disposition, from_status=expect_status,
               to_status="todo", caller=caller, caller_run_id=caller_run_id,
               reason=reason, gated_parents=landed.parents)
        return _receipt(target_task_id, disposition, expect_status, caller, caller_run_id,
                        to_status="todo", gated=landed.parents)

    claimed = _claim(conn, target_task_id, caller=caller)
    if isinstance(claimed, _Gated):
        _audit(conn, target_task_id, disposition=disposition, from_status=expect_status,
               to_status="todo", caller=caller, caller_run_id=caller_run_id,
               reason=reason, gated_parents=claimed.parents)
        return _receipt(target_task_id, disposition, expect_status, caller, caller_run_id,
                        to_status="todo", gated=claimed.parents)

    try:
        closed = kb.complete_task(
            conn,
            target_task_id,
            result=result or reason,
            summary=reason,
            expected_run_id=claimed,
        )
    except ValueError as exc:
        detail = f"{type(exc).__name__}: {exc}"
        _unwind(conn, target_task_id, caller=caller, caller_run_id=caller_run_id,
                disposition=disposition, from_status=expect_status, detail=detail)
        _refuse("gate_refused", detail)

    if isinstance(closed, kb.CompletionRefusal):
        # The downstream refusal carries its cause as data; pass it through verbatim.
        detail = f"{closed.cause}: {closed.detail}"
        _unwind(conn, target_task_id, caller=caller, caller_run_id=caller_run_id,
                disposition=disposition, from_status=expect_status, detail=detail)
        _refuse("gate_refused", detail)
    if not closed:
        detail = "complete_task returned False without a cause"
        _unwind(conn, target_task_id, caller=caller, caller_run_id=caller_run_id,
                disposition=disposition, from_status=expect_status, detail=detail)
        _refuse("gate_refused", detail)

    _audit(conn, target_task_id, disposition=disposition, from_status=expect_status,
           to_status="done", caller=caller, caller_run_id=caller_run_id, reason=reason)
    return _receipt(target_task_id, disposition, expect_status, caller, caller_run_id,
                    to_status="done", run_id=claimed)
