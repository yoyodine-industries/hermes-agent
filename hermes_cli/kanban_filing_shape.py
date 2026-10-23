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

THE KEY'S SHAPE, and this half is a RATCHET rather than a switch (platform-stl decision
ESC-ef2af6848e602c8b, 2026-10-22). An armed family's key must be ``<area>:<condition>``: at least
one colon, both sides non-empty, and the right side must NAME A PREDICATE -- a bare counter is not
a condition (``pr-approval-refusal-135`` is the 2,091-card shape this invariant exists to kill).
Arming is PER FAMILY, because a global switch is an outage that teaches every owner to hate the
invariant: ``pr-approval``, ``sdlc`` and ``tip-guard`` are armed now (the first migrates its keys
under a pre-stamp bridge, the other two already key in that shape), five families are in WARN MODE
until their date (the filing SUCCEEDS and is RECORDED -- a silent pass is what the decision
refuses), and any family not named there is ARMED AT BIRTH, because a new surface has no legacy
to bridge and the cost of compliance on day one is zero. Keys already written are NEVER re-judged
and no card is ever re-keyed by this gate: the ratchet governs filings made after a family arms.

NOT ENFORCED IN THE MEASURE-AND-REPAIR PASS either: a repair would have to INVENT the key or the
parent a card was never filed with, and a guessed key is worse than the duplicate it papers over.
"""

from __future__ import annotations

import datetime
import os
import re
import sqlite3
from typing import Iterable, Optional

#: The stamp the operator's own interactive CLI writes. A filing under it is a HUMAN filing.
HUMAN_CREATOR = "user"

#: The env var a dispatcher-spawned worker carries. A filing made INSIDE a worker session is a
#: bot filing even when the caller forgot ``created_by`` -- measured: 33 blank-stamp cards.
BOT_SESSION_ENV = "HERMES_KANBAN_TASK"

#: Families in WARN MODE until their shape clause arms (platform-stl, ESC-ef2af6848e602c8b).
#: Measured base: deploy-train 162 of 177 keys had no colon, hermes_pr_update 60/60, landcastle
#: 53/53, workflow-currency 22/22, sdlc-events 19/19 -- a shape refusal today would stop ~330
#: filings a week across five live surfaces, so each is given its migration window and a counter.
#: A family NOT listed here is armed at birth. ``pr-approval``, ``sdlc`` and ``tip-guard`` are
#: therefore armed now, which is what the decision requires of them.
WARN_UNTIL: dict[str, str] = {
    "deploy-train": "2026-11-01",
    "hermes_pr_update": "2026-11-01",
    "landcastle": "2026-11-01",
    "workflow-currency": "2026-11-01",
    "sdlc-events": "2026-11-01",
}

#: Where a warn-mode filing is RECORDED. The decision requires the pass to COUNT: a shape rule
#: that silently passes until it starts refusing is unfalsifiable, so every unshaped key a warn
#: family files lands here and ``warn_report`` prints it beside the gate violations.
WARN_TABLE = "filing_shape_warn"

#: The shape a key must have once its family is armed.
KEY_SHAPE = "<area>:<condition>"

_BARE_COUNTER = re.compile(r"-?\d+")


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


def filing_family(created_by: Optional[str]) -> str:
    """The FILING FAMILY a stamp belongs to -- the unit the shape clause is armed per.

    ``created_by`` already names the producer (``deploy-train``, ``landcastle``, ``pr-approval``,
    a profile id), and the operator's measurement of who files what was taken by that column, so
    the family is that stamp lowercased. An unattributed filing is its own family: it is armed,
    because a nameless producer cannot be granted a migration window nobody would ever check.
    """
    return str(created_by or "").strip().lower() or "anonymous"


def key_shape_problem(key: str) -> Optional[str]:
    """Why this key is NOT ``<area>:<condition>``, or ``None`` when it is.

    The right of the colon must name a PREDICATE. A bare counter is the shape this whole invariant
    exists to kill -- ``pr-approval-refusal-135`` says which pass filed the card and says nothing
    about what condition it is about, so the same condition can be filed again under the next
    number. ``repo#1891:refusal`` is shaped; ``pr-approval:135`` is not.
    """
    text = str(key or "").strip()
    if ":" not in text:
        return "no colon -- there is no condition, only a label"
    area, _, condition = text.rpartition(":")
    if not area.strip():
        return "the area left of the colon is empty"
    if not condition.strip():
        return "the condition right of the colon is empty"
    if _BARE_COUNTER.fullmatch(condition.strip()):
        return "the text right of the colon is a bare counter, not a condition"
    return None


def shape_armed(family: str, today: Optional[str] = None) -> bool:
    """True when this family's shape clause is armed (refuses); False while it is in warn mode.

    ``today`` is an ISO date; tests pass one so the ratchet is proven rather than waited for.
    """
    until = WARN_UNTIL.get(str(family or "").strip().lower())
    if not until:
        return True
    now = str(today or "").strip() or datetime.date.today().isoformat()
    return now >= until


def _record_warn(conn: sqlite3.Connection, *, family: str, key: str, where: str,
                 problem: str) -> None:
    """Record an unshaped key filed by a warn-mode family. Never raises into the caller.

    A warn mode that only prints is unfalsifiable, so the pass must COUNT. A bookkeeping failure
    must not refuse a filing the decision says must SUCCEED, so this swallows its own errors.
    """
    try:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {WARN_TABLE} ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, family TEXT NOT NULL, key TEXT NOT NULL, "
            "problem TEXT NOT NULL, where_ TEXT, created_at TEXT NOT NULL DEFAULT "
            "(strftime('%Y-%m-%dT%H:%M:%fZ','now')))"
        )
        conn.execute(
            f"INSERT INTO {WARN_TABLE} (family, key, problem, where_) VALUES (?, ?, ?, ?)",
            (family, key, problem, where),
        )
    except sqlite3.Error:
        return


def warn_report(conn: sqlite3.Connection) -> dict:
    """Per-family count of unshaped keys filed while in warn mode -- the ratchet's evidence.

    Read-only and empty when nothing has been recorded (or the table does not exist yet), so it is
    safe to call from the gates report on every board.
    """
    try:
        rows = conn.execute(
            f"SELECT family, COUNT(*) AS n, MIN(created_at) AS first_at, MAX(created_at) AS last_at "
            f"FROM {WARN_TABLE} GROUP BY family ORDER BY n DESC"
        ).fetchall()
        samples: dict[str, list[str]] = {}
        for row in conn.execute(
            f"SELECT family, key, problem FROM {WARN_TABLE} ORDER BY id DESC LIMIT 200"
        ).fetchall():
            bucket = samples.setdefault(row["family"], [])
            if len(bucket) < 3 and row["key"] not in bucket:
                bucket.append(row["key"])
    except sqlite3.Error:
        return {"families": [], "total": 0, "armed_at": dict(WARN_UNTIL)}
    families = [
        {"family": row["family"], "count": int(row["n"]), "first_at": row["first_at"],
         "last_at": row["last_at"], "samples": samples.get(row["family"], [])}
        for row in rows
    ]
    return {"families": families,
            "total": sum(item["count"] for item in families),
            "armed_at": dict(WARN_UNTIL)}


def gate_filing_shape(conn: sqlite3.Connection, *, parents: Iterable[str] = (),
                      idempotency_key: Optional[str] = None, created_by: Optional[str] = None,
                      where: str = "create", today: Optional[str] = None) -> None:
    """The write door: a BOT filing must be a CHILD, or carry a KEY of the right SHAPE.

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
    key = str(idempotency_key or "").strip()
    if not key:
        raise FilingShapeRefused(
            f"{where}: an UNKEYED TOP-LEVEL filing by {created_by or 'a bot session'} is the "
            "shape the board cannot recognise the second time -- nothing links it to a parent "
            "and no key names its condition, so the same condition is filed again on every pass "
            "(measured: 2,091 `pr-approval-refusal-*` cards over 1,206 distinct (repo, PR) "
            "pairs). File it as a CHILD (`parents=[<the card this work serves>]`) or give it an "
            "`idempotency_key` naming what it is about (`<repo>#<n>:<condition>`, "
            "`<area>:<condition>`) -- a second filing of that key returns the first card and "
            "creates nothing. Nothing was written.",
            cause="unkeyed_top_level",
            facts={"where": where, "created_by": created_by},
        )
    # A key is necessary, and once its family is ARMED it must also be SHAPED -- the right of the
    # colon names the PREDICATE, so a re-run of the same condition lands on the same key instead
    # of on the next counter.
    problem = key_shape_problem(key)
    if problem is None:
        return
    family = filing_family(created_by)
    if not shape_armed(family, today):
        _record_warn(conn, family=family, key=key, where=where, problem=problem)
        return
    raise FilingShapeRefused(
        f"{where}: family `{family}` filed key `{key}`, which is not the shape `{KEY_SHAPE}` -- "
        f"{problem}. The right of the colon must name the CONDITION the card is about (e.g. "
        "`repo#1891:refusal`, `board:migration-drift`), never a counter that a re-run can step "
        "past. Rewrite the key. Nothing was written.",
        cause="unshaped_key",
        facts={"where": where, "created_by": created_by, "family": family, "key": key,
               "shape": KEY_SHAPE, "problem": problem},
    )
