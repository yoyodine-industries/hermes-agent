"""Invariant E: a BOT filing is a CHILD or KEYED -- the store refuses the shape.

WHY THIS EXISTS (operator ruling 2026-10-22, card ``t_add7b0e9`` / rate-matched door M1). The
board already carries the two mechanisms that make a filing re-recognisable --
``tasks.idempotency_key`` and ``task_links(parent_id -> child_id)`` -- and the filing surfaces
used them only by luck. Measured on the platform board over 7 days by ``created_by``:

  created_by        cards   linked as child   with a key
  auto-decomposer     244        244 (100%)       0     (a child, correctly)
  platform-stl        623        142  (23%)     365 (59%) -> 258 UNKEYED
  deploy-train        342        115  (34%)     342 (100%)
  sdlc                237          0   (0%)     237 (100%)
  tip-guard           205          0   (0%)     205 (100%)
  landcastle          140          0   (0%)     140 (100%)

So ONE bot files every card as a child and the rest file top-level. The difference is not style:
a top-level UNKEYED card is a condition the store cannot recognise the second time, so the same
condition is filed again on every pass -- 2,091 ``pr-approval-refusal-*`` cards over 1,206
distinct (repo, PR) pairs, one PR carrying 135. A prompt rule decays (the diddle prompt's own
child-or-keyed rule landed as PR #1887, and a rewrite can drop it); the STORE must refuse the
shape.

THE RULE, and each half is answered by the row itself, never by prose:

1. A filing WITH parents is a CHILD: every parent must EXIST and be NON-ARCHIVED. A link to an
   archived card is a dependency that never resolves -- no promotion reads an archived parent,
   so the child sits in ``todo`` for good. ``done`` is deliberately NOT refused:
   ``recompute_ready`` promotes a child of a finished parent to ``ready`` at birth (the fan-in
   pattern), so a done parent costs the child nothing.
2. A filing WITHOUT parents is TOP-LEVEL and, when a BOT files it, must carry a non-empty
   ``idempotency_key``. ``create_task`` reads that key before it inserts and returns the FIRST
   card's id, so a re-filing is an ANSWER, not a second card.
3. A HUMAN filing (no stamp, or the ``user`` stamp the operator's own CLI writes) is left alone:
   the operator proposes work by typing it.

WHY A REFUSAL AND NOT A WARNING: the invariants module beside this one learned this the hard way
-- its predecessor warned at create and 361 rows landed on handles that resolve nowhere. The
refusal names the exact key to add and the exact parent to link, so a caller has a cheap honest
path.

NOT ENFORCED, deliberately: the key's internal SHAPE. The ruling prescribes
``<repo>#<n>:<condition>`` / ``<area>:<condition>``; measured on the same board, four live bot
families key in OTHER shapes (deploy-train 162 of 177, hermes_pr_update 60, landcastle 53,
workflow-currency 22 -- no colon), so a shape refusal would stop those surfaces' filing in one
tick. That narrowing belongs to platform-stl, which migrates those families' keys first; this
gate enforces the half that is safe today and that removes the measured duplicate mechanism.

NOT ENFORCED IN THE MEASURE-AND-REPAIR PASS either: a repair would have to INVENT the key or the
parent a card was never filed with, and a guessed key is worse than the duplicate it papers over.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Iterable, Optional

#: The stamp the operator's own interactive CLI writes. A filing under it is a HUMAN filing.
HUMAN_CREATOR = "user"

#: The env var a dispatcher-spawned worker carries. A filing made INSIDE a worker session is a
#: bot filing even when the caller forgot ``created_by`` -- measured: 33 blank-stamp cards.
BOT_SESSION_ENV = "HERMES_KANBAN_TASK"


class FilingShapeRefused(ValueError):
    """Invariant E: a BOT filing is neither a CHILD nor KEYED, so a second one is invisible.

    A ``ValueError`` subclass on purpose, like every other write-door refusal: the CLI funnels
    ``ValueError`` as a VALIDATION refusal (``kanban: <message>``) and the same text reaches the
    ``kanban_*`` tools, so no surface reports a crash where the caller only mis-shaped a call.
    """

    invariant = "filing_shape"

    def __init__(self, message: str, *, cause: str = "unkeyed_top_level",
                 task_id: Optional[str] = None, facts: Optional[dict] = None) -> None:
        super().__init__(message)
        self.cause = cause
        self.task_id = task_id
        self.facts = facts or {}


def is_bot_filing(created_by: Optional[str]) -> bool:
    """True when this filing was made by a BOT rather than the operator.

    ``created_by`` names the producer (``sdlc``, ``deploy-train``, ``pr-approval``, a profile
    id ...). Unset/blank is not an attribution -- but a filing made INSIDE a worker session is a
    bot's, so the worker-session env answers for the anonymous case.
    """
    stamp = str(created_by or "").strip().lower()
    if stamp:
        return stamp != HUMAN_CREATOR
    return bool(os.environ.get(BOT_SESSION_ENV))


def gate_filing_shape(conn: sqlite3.Connection, *, parents: Iterable[str] = (),
                      idempotency_key: Optional[str] = None, created_by: Optional[str] = None,
                      where: str = "create") -> None:
    """The write door: a BOT filing must be a CHILD or carry a KEY.

    ``parents`` is the filing's parent ids (a child), ``idempotency_key`` its key. A child is
    checked for existence and archived-ness; a top-level bot filing without a key is REFUSED by
    name. The duplicate case needs no branch here: ``create_task`` reads the key before it
    inserts and returns the FIRST card's id, so a re-filing creates nothing.
    """
    ids = tuple(str(p).strip() for p in (parents or ()) if str(p or "").strip())
    if ids:
        try:
            rows = {row["id"]: row["status"] for row in conn.execute(
                "SELECT id, status FROM tasks WHERE id IN ("
                + ",".join("?" * len(ids)) + ")", ids,
            )}
        except sqlite3.Error:
            # An unreadable read is not evidence of a bad link: the caller's own parent check
            # (``kanban_db_graph.initial_task_state``) still runs and still names an unknown id.
            return
        missing = [pid for pid in ids if pid not in rows]
        if missing:
            raise FilingShapeRefused(
                f"{where}: parent(s) {', '.join(missing)} name no card on this board -- a "
                "child's link must point at a card that EXISTS, or the dependency it declares "
                "is a lie and the child runs unblocked. File it top-level WITH an "
                "idempotency_key, or name the live parent. Nothing was written.",
                cause="unknown_parent",
                facts={"where": where, "parents": list(ids), "missing": missing},
            )
        dead = [(pid, rows[pid]) for pid in ids if rows[pid] == "archived"]
        if dead:
            detail = ", ".join(f"{pid} ({status})" for pid, status in dead)
            raise FilingShapeRefused(
                f"{where}: parent(s) {detail} are ARCHIVED -- a child of an archived card is a "
                "dependency that can never resolve: no promotion reads an archived parent, so "
                "the card would sit in `todo` for good. Link the LIVE card that owns the work, "
                "or file this top-level WITH an idempotency_key. Nothing was written.",
                cause="terminal_parent",
                facts={"where": where, "parents": [p for p, _ in dead]},
            )
        return
    if not is_bot_filing(created_by):
        return
    if str(idempotency_key or "").strip():
        return
    raise FilingShapeRefused(
        f"{where}: an UNKEYED TOP-LEVEL filing by {created_by or 'a bot session'} is the shape "
        "the board cannot recognise the second time -- nothing links it to a parent and no key "
        "names its condition, so the same condition is filed again on every pass (measured: "
        "2,091 `pr-approval-refusal-*` cards over 1,206 distinct (repo, PR) pairs). File it as "
        "a CHILD (`parents=[<the card this work serves>]`) or give it an `idempotency_key` "
        "naming what it is about (`<repo>#<n>:<condition>`, `<area>:<condition>`) -- a second "
        "filing of that key returns the first card and creates nothing. Nothing was written.",
        cause="unkeyed_top_level",
        facts={"where": where, "created_by": created_by},
    )
