"""Cross-sibling close: a lane's LEAD may close a SIBLING card on the same board through
the kernel's own completion path, audited on both ends.

WHY THIS EXISTS (measured, card ``t_fbc22829``). A lead dispatched onto its own card
frequently discovers that a *sibling* card's work is already done -- absorbed into the
lead's branch, superseded by a re-cut, or resolved by the same fix. The sibling then sits
``ready``/``blocked`` and the dispatcher keeps spawning workers onto work that no longer
needs doing (rework churn, duplicate PRs, and a lane's queue that never drains).

The lead could reach that conclusion, and had **no sanctioned way to act on it**:

  * ``kanban_complete`` is own-card only -- ``_enforce_worker_task_ownership`` refuses
    another task id, by design: a prompt-injected worker must not close a sibling's run;
  * ``kanban_unblock`` is orchestrator-only (hidden from task workers);
  * ``hermes kanban <verb>`` is refused inside a worker's shell (the delegation fence),
    and a raw status write is not a close at all -- it skips the completion clause.

WHAT IT IS. One door, :func:`close_sibling`, exposed to workers as the
``kanban_close_sibling`` tool. It does NOT write a status column: it calls
``kanban_db.complete_task`` for the sibling, so every completion gate the board owns --
the parent gate, the PR acceptance receipt, and the **landing-carrier clause** -- still
applies and can still refuse. A close this door cannot get past the clause is a close
nobody gets.

WHAT IT REFUSES (each is a silent no-op the door exists to prevent):
  * closing the caller's own card -- that is ``kanban_complete``'s job, with its own
    ownership proof; routing it here would launder a self-close past the run CAS;
  * a target that is unknown, ARCHIVED, or already ``done`` (nothing to close);
  * a target whose run is LIVE: ``complete_task``'s claim fence decides whether the claim
    protects a live worker, and a raised :class:`LiveClaimError` is reported as a refusal
    with its own remedy text -- never swallowed;
  * any :class:`CompletionRefusal` the kernel returns (parent gate, acceptance, landing
    carrier, ...), reported with its cause and detail intact;
  * a caller card that is not on this board (the audit would be written nowhere).

THE RECORD SAYS IT WAS DELEGATED. The stored ``result`` and the closing run's ``summary``
both carry the provenance -- ``closed by DELEGATED sibling close from <caller> (run <id>)``
-- so the sibling's history never reads as if its own worker closed it. One comment on the
sibling names the caller, authored ``hermes-kanban-close-sibling`` (the actor string the
landing-verdict precedent uses), and ``sibling_close_recorded`` lands on BOTH cards: the
act is visible in both histories without reading run JSON.

CHILDREN RE-GATED, NEVER ORPHANED. Closing a parent leaves its children: an incomplete
child (``todo``, ``ready`` or ``blocked``) is re-promoted to ``ready`` through the kernel's
own verbs (``promote_task`` / ``unblock_task``), so the ready -> running claim still
re-checks dependencies. A child whose OTHER parent is still unfinished does not move at
all and is reported ``gated`` with the blocking parent named.

A REFUSAL IS NOT A PARTIAL WRITE. Every refusal is raised before the close; when the kernel
refuses, the target keeps no event and no child is promoted. The refusal itself is recorded
on the CALLER's card (``sibling_close_refused``) -- a door that fails silently on the
caller's own history is how a lead retries the same impossible close forever.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

__all__ = [
    "ACTOR",
    "PLACEHOLDER",
    "CrossCloseRefused",
    "close_sibling",
]

#: The comment author and event actor for this door -- one string, so a reader can grep it.
ACTOR = "hermes-kanban-close-sibling"

#: Provenance clause appended to the stored result, always naming the caller.
PLACEHOLDER = "closed by DELEGATED sibling close"

#: Child statuses this door acts on; everything else is reported, never moved.
_MOVABLE_CHILD_STATUSES = ("todo", "blocked")

#: Terminal statuses: a child here is already closed, so there is nothing to re-gate.
_TERMINAL_STATUSES = ("done", "archived")


class CrossCloseRefused(ValueError):
    """The door refused; the message is the remedy the caller has to act on."""


def _as_dict(raw: object) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def _status(task: Any) -> str:
    return str(getattr(task, "status", "") or "").strip().lower()


def _child_rows(kb, conn: sqlite3.Connection, parent_id: str) -> list[dict]:
    """The parent's children, with the status each holds BEFORE the close."""
    rows: list[dict] = []
    for child_id in kb.child_ids(conn, parent_id):
        child = kb.get_task(conn, child_id)
        if child is None:
            continue
        rows.append({"task_id": child_id, "from": _status(child), "to": _status(child),
                     "action": "unchanged", "reason": ""})
    return rows


def _move_children(
    kb, conn: sqlite3.Connection, parent_id: str, *, before: list[dict],
    caller: Optional[str], caller_run_id: Optional[int], board: Optional[str],
) -> list[dict]:
    """Re-gate every incomplete child through the kernel; report what each one did.

    ``before`` is the pre-close snapshot: the completion's own parent-gate release may
    already have flipped a child to ``ready``, and a receipt that reported only the
    post-close status would hide that the child moved at all.

    A child with an unfinished OTHER parent is left exactly where it is and reported
    ``gated``: moving it would hand the dispatcher a card whose dependency is not met, and
    the ready -> running claim would only demote it again.
    """
    moved: list[dict] = []
    for row in before:
        child_id = row["task_id"]
        was = row["from"]
        if was in _TERMINAL_STATUSES:
            row.update(action="skipped", reason="already terminal")
            moved.append(row)
            continue
        gated = kb.unsatisfied_parents(conn, child_id)
        if gated:
            row.update(
                action="gated",
                reason="still gated by " + ", ".join(f"{pid} ({st})" for pid, st in gated),
            )
            row["to"] = _status(kb.get_task(conn, child_id))
            moved.append(row)
            continue
        now = _status(kb.get_task(conn, child_id))
        reason = f"{PLACEHOLDER} from {caller or 'an orchestrator'}"
        if now not in _MOVABLE_CHILD_STATUSES:
            # The kernel's own parent gate released it, or it is already in flight.
            if now != was:
                row.update(action="released", reason="the completion released it")
            else:
                row.update(action="unchanged", reason=f"already {now}")
            row["to"] = now
            moved.append(row)
            continue
        if now == "todo":
            ok, detail = kb.promote_task(conn, child_id, actor=ACTOR, reason=reason)
            row["action"] = "promoted" if ok else "refused"
            row["reason"] = "" if ok else (detail or "promote refused")
        else:  # blocked
            ok = kb.unblock_task(
                conn, child_id,
                extra_event={"actor": ACTOR, "reason": reason,
                             "parent_closed": parent_id,
                             **( {"caller_task_id": caller} if caller else {} ),
                             **( {"board": board} if board else {} )},
            )
            row["action"] = "unblocked" if ok else "refused"
            row["reason"] = "" if ok else "unblock refused (not blocked any more)"
        row["to"] = _status(kb.get_task(conn, child_id))
        moved.append(row)
    return moved


def _record_refusal(
    kb, conn: sqlite3.Connection, caller: Optional[str], caller_run_id: Optional[int],
    sibling_task_id: str, detail: str, *, board: Optional[str],
) -> None:
    """The refusal lands on the CALLER's card: the target stays byte-untouched."""
    if not caller:
        return
    with kb.write_txn(conn):
        kb._append_event(
            conn, caller, "sibling_close_refused",
            {"target_task_id": sibling_task_id, "reason": detail[:500], "actor": ACTOR,
             **( {"board": board} if board else {} )},
            run_id=caller_run_id,
        )


def close_sibling(
    conn: sqlite3.Connection,
    sibling_task_id: str,
    *,
    caller_task_id: Optional[str] = None,
    caller_run_id: Optional[int] = None,
    summary: Optional[str] = None,
    result: Optional[str] = None,
    board: Optional[str] = None,
) -> dict:
    """Close ``sibling_task_id`` as DELEGATED work from ``caller_task_id``; returns the receipt.

    Raises :class:`CrossCloseRefused` (a ``ValueError``) on every refusal, always before the
    close commits, so a refused call leaves the target and its children untouched. The
    material write is ``kanban_db.complete_task`` -- the kernel's own completion path, so
    the parent gate, the acceptance receipt and the landing-carrier clause all still apply.
    """
    from hermes_cli import kanban_db as kb

    target = str(sibling_task_id or "").strip()
    if not target:
        raise CrossCloseRefused(
            "sibling_task_id is required: name the card whose work this lane's lead absorbed. "
            "Nothing was closed."
        )
    caller = str(caller_task_id or "").strip() or None
    if caller == target:
        raise CrossCloseRefused(
            f"{target} is your OWN card: close it with kanban_complete, which carries the "
            "run-ownership proof this door must not launder. Nothing was closed."
        )
    if caller is not None and kb.get_task(conn, caller) is None:
        raise CrossCloseRefused(
            f"caller {caller} is not on this board, so the audit end of this close would be "
            "written nowhere. Nothing was closed."
        )

    task = kb.get_task(conn, target)
    if task is None:
        raise CrossCloseRefused(
            f"{target} does not exist on this board. Nothing was closed."
        )
    status = _status(task)
    if status == "archived":
        raise CrossCloseRefused(
            f"{target} is archived: there is nothing left to close. Reopen it first if the "
            "work really is still owed. Nothing was closed."
        )
    if status in _TERMINAL_STATUSES:
        raise CrossCloseRefused(
            f"{target} is already {status}: nothing to close. Nothing was closed."
        )

    body = (summary or "").strip() or (result or "").strip() or "absorbed by the sibling lane"
    provenance = f"{PLACEHOLDER} from {caller or 'an orchestrator'}"
    if caller_run_id is not None:
        provenance += f" (run {caller_run_id})"
    text = f"{body} — {provenance}."

    # Snapshot the children BEFORE the close: the completion's own parent-gate release can
    # flip a child to ready, and a receipt read only afterwards would hide that it moved.
    children_before = _child_rows(kb, conn, target)

    try:
        closed = kb.complete_task(
            conn, target, result=text, summary=text,
            metadata={"delegated_close_from": caller,
                      "delegated_close_run_id": caller_run_id},
        )
    except kb.LiveClaimError as exc:  # a live claim protects a live worker
        detail = f"{target} is running under a live claim: {exc}. Nothing was closed."
        _record_refusal(kb, conn, caller, caller_run_id, target, detail, board=board)
        raise CrossCloseRefused(detail) from exc
    if not closed:
        cause = getattr(closed, "cause", "unknown")
        detail = getattr(closed, "detail", str(closed))
        message = (
            f"{target} cannot be closed through the completion gate ({cause}): {detail}. "
            "Nothing was closed."
        )
        _record_refusal(kb, conn, caller, caller_run_id, target, message, board=board)
        raise CrossCloseRefused(message)

    warnings: list[str] = []
    children = _move_children(
        kb, conn, target, before=children_before,
        caller=caller, caller_run_id=caller_run_id, board=board,
    )
    run = kb.latest_run(conn, target)
    run_id = getattr(run, "id", None)

    comment = (
        f"Closed as DELEGATED work by sibling `{caller}`"
        + (f" (run {caller_run_id})" if caller_run_id is not None else "")
        + f".\n\n{body}"
        + ("\n\nChildren re-gated: " + ", ".join(
            f"`{c['task_id']}` {c['from']}→{c['to']} ({c['action']})" for c in children
        ) if children else "\n\nNo child cards.")
    )
    try:
        kb.add_comment(conn, target, ACTOR, comment)
    except Exception as exc:  # the close stands; say the audit leg did not
        warnings.append(f"comment not written: {exc}")

    try:
        with kb.write_txn(conn):
            kb._append_event(
                conn, target, "sibling_close_recorded",
                {"caller_task_id": caller, "caller_run_id": caller_run_id,
                 "children": [{k: c[k] for k in ("task_id", "from", "to", "action")}
                              for c in children],
                 "actor": ACTOR, "outcome": "recorded",
                 **( {"board": board} if board else {} )},
                run_id=run_id,
            )
            if caller:
                kb._append_event(
                    conn, caller, "sibling_close_recorded",
                    {"target_task_id": target, "target_run_id": run_id,
                     "children_moved": sum(1 for c in children
                                            if c["action"] in ("promoted", "unblocked")),
                     "actor": ACTOR, "outcome": "recorded",
                     **( {"board": board} if board else {} )},
                    run_id=caller_run_id,
                )
    except Exception as exc:
        warnings.append(f"audit event not written: {exc}")

    receipt = {
        "target": target, "status": _status(kb.get_task(conn, target) or task),
        "run_id": run_id, "result": text, "caller": caller, "children": children,
    }
    if warnings:
        receipt["warnings"] = warnings
    return receipt
