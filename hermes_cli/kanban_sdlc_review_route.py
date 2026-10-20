"""The SDLC review-route clause: a card that names an SDLC item standing at or above
``approved`` and below ``accepted`` may not be closed by its AUTHOR LANE without a
completed review run.

WHY THIS EXISTS (measured 2026-10-19, card ``t_eb5bf72d``; the same shape had already
cost a landing on ``t_854029d1`` / PR #100).

``sdlc_nodes.py::require_acceptance`` reads the acceptance verdict from the SDLC item's
OWN card -- the successful ``task_runs`` rows of the design authority, excluding the
author lane. The kernel let that card reach ``done`` from ``running`` with no review at
all, and once a card is ``done`` there is NO sanctioned door that puts a reviewer's run
on it: ``request-review`` requires ``running``/``ready``, ``reopen-review`` requires
``review``, and ``edit`` cannot set status. The item then stands at ``approved`` with its
acceptance attempt refused ``self_acceptance`` ("the only candidate verdict on card X is
the author lane's own"), and the PR cannot land -- ``land.sh``'s ``sdcl-item``-class leg
(rule 4, row 1) demands the item reached ``accepted`` for that PR.

Measured, in order, on ``t_4ae7ebdf`` / PR #1624:

1. card ``t_4ae7ebdf`` (board ``skills-refactor``) = ``done``, assignee ``platform-coder``;
   item ``sdlc-platform-t-4ae7ebdf`` stood at ``approved`` with reviewers
   ``[platform-stl]``;
2. ``advance --item ... --to accepted`` => refused ``self_acceptance``;
3. ``reopen-review t_4ae7ebdf`` => refused "not in review?";
4. ``request-review t_4ae7ebdf --reviewer platform-stl --force`` => refused "task is not
   in running/ready".

The repair on that day was a SECOND card (``t_99ff0b4b``) plus a second submission item
for the same PR/sha, so the acceptance run existed somewhere. That path works but it mints
a duplicate item per PR and asks the reviewer to know the deadlock exists. THIS clause
stops the deadlock being CREATED: the refusal arrives at the completion that would strand
the item, and it names the act that is owed -- ``request-review``.

WHAT IT IS. A clause, not a second system. ``hermes_cli/kanban_db.py::_gate_sdlc_review_route``
consults it from inside ``complete_task``'s completion-gate family, before the completion
write txn, beside ``_gate_deploy_proof`` and ``_gate_landing_carrier``. It never raises
and never mutates the card: it returns a :class:`Verdict` (as data) or ``None``. The gate
records ONE auditable event (``completion_blocked_sdlc_review``) and leaves the card
byte-untouched, so every existing ``if not complete_task(...)`` caller keeps its meaning.

WHAT MAKES A CARD AN SDLC CARD: the flow's own contract comment, never prose. Every
transition the SDLC flow commits writes
``SDLC stage=<stage> item=<item> at=<iso> by=sdlc evidence=<ref>`` as a comment on the
card (``sdlc_nodes.py::stage_contract``) -- birth included -- so the NEWEST such comment
is the item's live stage, read from the same store the board already owns. A card with no
such comment is outside this clause BY CONSTRUCTION, which is what keeps ordinary
platform, research and design cards untouched. Nothing is inferred from a title, a body,
a PR ref, or a label.

THE RULE. The close is refused when ALL of:

  * the newest SDLC contract comment names a stage at or ABOVE ``approved`` (the item has
    passed the review gate, so the acceptance verdict is what is owed next) and BELOW
    ``accepted`` (an item that already reached acceptance needs no further review door);
  * the closer is the item's AUTHOR LANE -- the card's recorded ``review_requested``
    implementer when it has one, else the card's own assignee lane;
  * the card carries NO completed run by any lane other than the author (the same
    successful-attempt set ``sdlc_nodes.reviewers()`` counts), i.e. no review has ever
    been recorded on this card.

A close out of ``review`` is the review verdict itself and is never this clause's
business. An UNKNOWN closer (no owning run and no actor named -- a dashboard drag or a
direct kernel call) is not refused: the clause refuses a WRITE-OFF by the author, and
guessing the author from nothing is exactly the inference the operator standard forbids.
The consequence, stated so it is not a surprise: this clause stops the WORKER door that
creates the strand, and the repair for an already-stranded card is
``hermes kanban reopen-review <card> --reason ... --reviewer <lane>`` (the other half of
card ``t_eb5bf72d``), which puts a review state and a reviewer on the card so the
acceptance run can exist.

STAGE VOCABULARY. TWO item machines are live on the boards at once, so BOTH are ranked and
each is read off its OWN transition rules, never by analogy between them:

  * the legacy machine, verbatim from ``yaan-platform/scripts/sdlc_nodes.py::STAGES`` as
    origin/main carries it (draft -> specified -> implemented -> in_review -> approved ->
    changes_requested -> accepted -> merged -> registered -> deployed -> verified ->
    released -> closed): a review leaves the item at ``approved``, ``advance --to accepted``
    writes ``accepted``;
  * the design-of-record yoyoflow machine (inception -> requirements -> code-complete ->
    approved-pr-submitted -> pr-accepted -> slotted-for-train -> deployed ->
    verified-in-prod -> announced -> retired): ``code-complete -> approved-pr-submitted``
    is the REVIEW-GATED transition (the one that requires the reviewer refs), so
    ``code-complete`` is where the verdict is owed and ``pr-accepted`` is its acceptance.

A stage string outside both machines is NOT ranked: the clause returns ``None``, because
refusing on a word this kernel does not know is a guess wearing a fence's clothes.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Optional

#: The LEGACY item machine, in order, verbatim from the live
#: ``yaan-platform/scripts/sdlc_nodes.py::STAGES`` as origin/main carries it (draft ->
#: specified -> implemented -> in_review -> approved -> changes_requested -> accepted ->
#: merged -> registered -> deployed -> verified -> released -> closed). Kept as a literal
#: (not imported from the flow): the kernel must judge a card the same way whether or not
#: a yaan-platform checkout is present, and a vocabulary change there is a deliberate edit
#: here -- which is the point.
STAGES = (
    "draft", "specified", "implemented", "in_review", "approved",
    "changes_requested", "accepted", "merged", "registered", "deployed",
    "verified", "released", "closed",
)

#: A close at or above this stage owes an acceptance verdict (the review gate has passed).
GUARD_FROM = "approved"

#: The stage at which the item no longer needs a review door: ``advance --to accepted``
#: has already read the verdict, and everything past it is proof/landing work.
ACCEPTED_AT = "accepted"

#: The DESIGN-OF-RECORD machine, the yoyoflow ``sdlc`` DAG's own stages
#: (``sdlc_nodes.py`` STAGES on the design side: inception -> requirements -> code-complete
#: -> approved-pr-submitted -> pr-accepted -> slotted-for-train -> deployed ->
#: verified-in-prod -> announced -> retired). Both machines are LIVE (the contract comments
#: on the boards carry both vintages), so a fence that ranks only one of them leaves a
#: whole vocabulary unguarded -- measured 2026-10-19: 3 cards stamped ``requirements``,
#: 1 ``retired``.
STAGES_YOYOFLOW = (
    "inception", "requirements", "code-complete", "approved-pr-submitted",
    "pr-accepted", "slotted-for-train", "deployed", "verified-in-prod",
    "announced", "retired",
)

#: ``(name, stages, guard_from, accepted_at)`` per machine. The band is read off EACH
#: machine's own transition rules, never by analogy between them:
#:
#:  * legacy: ``approved`` is the stage a review leaves the item in, ``accepted`` the one
#:    ``advance --to accepted`` writes after reading the verdict;
#:  * yoyoflow: ``code-complete -> approved-pr-submitted`` is the REVIEW-GATED transition
#:    (``sdlc_nodes.py::review_refs`` / ruling s2.10 -- it is the transition that requires
#:    the reviewer refs), so ``code-complete`` is where the verdict is owed, and
#:    ``pr-accepted`` is that machine's acceptance.
LADDERS: tuple = (
    ("legacy", STAGES, GUARD_FROM, ACCEPTED_AT),
    ("yoyoflow", STAGES_YOYOFLOW, "code-complete", "pr-accepted"),
)

#: The ONE spelling of "this attempt succeeded", the same tuple
#: ``sdlc_nodes.py::GOOD_OUTCOMES`` counts reviewers with. A policy that COUNTS attempts
#: and the gate that JUDGES them must agree on what an attempt was.
GOOD_OUTCOMES = ("ok", "done", "success", "completed", "complete")

#: ``sdlc_nodes.py::stage_contract`` writes exactly this line shape. Anchored at the start
#: of the comment so a quote of the line inside a review note is not read as a contract.
CONTRACT_RE = re.compile(r"^SDLC stage=(?P<stage>\S+) item=(?P<item>\S+)")

#: How many of the card's newest comments are scanned for the contract line. A card's
#: comment thread is short between transitions; 200 is generous and bounds the read.
COMMENT_SCAN = 200

CAUSE = "sdlc_review_required"
EVENT = "completion_blocked_sdlc_review"


@dataclass
class Verdict:
    """What the clause decided, as data (never an exception, never a write)."""

    refused: bool
    cause: str = ""
    detail: str = ""
    facts: dict = field(default_factory=dict)


def _rank(stage: Optional[str]) -> Optional[int]:
    try:
        return STAGES.index(str(stage))
    except (ValueError, TypeError):
        return None


def _band(stage: Optional[str]):
    """``(ladder_name, rank, floor, accepted_rank)`` for the machine that owns ``stage``.

    ``None`` when no live machine knows the word: refusing on a stage this kernel cannot
    rank would be a guess, and the operator standard says a fence does not guess.
    """
    word = str(stage) if stage is not None else ""
    for name, stages, guard_from, accepted_at in LADDERS:
        if word in stages:
            return (name, stages.index(word), stages.index(guard_from),
                    stages.index(accepted_at), guard_from, accepted_at)
    return None


def item_contract(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    """The card's NEWEST ``SDLC stage=... item=...`` comment, or ``None``.

    Newest wins: every transition writes one, so the latest line is the item's live stage.
    Reads ``task_comments`` -- a row the kernel already owns -- and never the flow's own
    state directory, so the clause judges a card identically on any host/board.
    """
    try:
        rows = conn.execute(
            "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id DESC LIMIT ?",
            (task_id, COMMENT_SCAN),
        ).fetchall()
    except sqlite3.Error:
        return None
    for row in rows:
        body = row["body"] if isinstance(row, sqlite3.Row) else row[0]
        match = CONTRACT_RE.match((body or "").strip())
        if match:
            return {"stage": match.group("stage"), "item": match.group("item")}
    return None


def author_lane(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """The lane this card's work belongs to: the review handoff's implementer, else assignee.

    ``request_review`` records the implementer on the ``review_requested`` event and then
    REASSIGNS the card to the reviewer, so once a card has been routed the assignee is no
    longer the author and the event is the durable provenance. A card that was never
    routed has neither, and the assignee it was created for IS the author lane.
    """
    try:
        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'review_requested' "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if event is not None:
            raw = event["payload"] if isinstance(event, sqlite3.Row) else event[0]
            payload = json.loads(raw) if raw else {}
            implementer = (payload or {}).get("implementer")
            if isinstance(implementer, str) and implementer.strip():
                return implementer.strip()
        row = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (task_id,)).fetchone()
    except (sqlite3.Error, ValueError):
        return None
    if row is None:
        return None
    assignee = row["assignee"] if isinstance(row, sqlite3.Row) else row[0]
    return assignee.strip() if isinstance(assignee, str) and assignee.strip() else None


def review_profiles(conn: sqlite3.Connection, task_id: str, author: Optional[str]) -> list:
    """Profiles with a SUCCESSFUL run on this card, the author lane excluded.

    The kernel twin of ``sdlc_nodes.py::reviewers()`` -- same table, same outcome
    vocabulary -- so this gate and the acceptance door that consumes it cannot disagree
    about whether a review happened.
    """
    placeholders = ",".join("?" for _ in GOOD_OUTCOMES)
    try:
        rows = conn.execute(
            "SELECT DISTINCT profile FROM task_runs WHERE task_id = ? "
            f"AND outcome IN ({placeholders}) AND profile IS NOT NULL",
            (task_id, *GOOD_OUTCOMES),
        ).fetchall()
    except sqlite3.Error:
        return []
    seen = []
    for row in rows:
        profile = row["profile"] if isinstance(row, sqlite3.Row) else row[0]
        if not isinstance(profile, str) or not profile.strip():
            continue
        profile = profile.strip()
        if author and profile == author:
            continue
        if profile not in seen:
            seen.append(profile)
    return seen


def closer(conn: sqlite3.Connection, task_id: str, *, actor: Optional[str] = None,
           expected_run_id: Optional[int] = None) -> Optional[str]:
    """Which lane is closing the card, as far as the kernel can PROVE it.

    ``actor`` (the CLI's own profile) first; then the owning run's profile -- the worker
    path, where ``kanban_complete`` passes ``expected_run_id``; then the card's current
    run. ``None`` means "not provable", and the clause never guesses.
    """
    if isinstance(actor, str) and actor.strip():
        return actor.strip()
    for run_id in (expected_run_id, None):
        try:
            if run_id is not None:
                row = conn.execute(
                    "SELECT profile FROM task_runs WHERE id = ? AND task_id = ?",
                    (run_id, task_id),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT r.profile FROM tasks t JOIN task_runs r ON r.id = t.current_run_id "
                    "WHERE t.id = ?",
                    (task_id,),
                ).fetchone()
        except sqlite3.Error:
            return None
        if row is not None:
            profile = row["profile"] if isinstance(row, sqlite3.Row) else row[0]
            if isinstance(profile, str) and profile.strip():
                return profile.strip()
        if run_id is None:
            break
    return None


def evaluate(
    *,
    conn: sqlite3.Connection,
    task_id: str,
    actor: Optional[str] = None,
    expected_run_id: Optional[int] = None,
    status: Optional[str] = None,
    contract: Optional[dict] = None,
) -> Optional[Verdict]:
    """``None`` = the close is not this clause's business; else a :class:`Verdict`.

    The keyword overrides (``status`` / ``contract``) exist so a test can drive the clause
    over its REAL door without staging rows it does not mean to assert on.
    """
    try:
        row = conn.execute("SELECT status, assignee FROM tasks WHERE id = ?", (task_id,)).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    status = status if status is not None else (
        row["status"] if isinstance(row, sqlite3.Row) else row[0]
    )
    # A close out of ``review`` IS the review verdict: the acceptance door reads that run.
    if status == "review":
        return None
    contract = contract if contract is not None else item_contract(conn, task_id)
    if not contract:
        return None
    stage, item = contract.get("stage"), contract.get("item")
    band = _band(stage)
    if band is None:
        return None
    ladder, rank, floor, ceiling, guard_word, accepted_word = band
    if rank < floor or rank >= ceiling:
        return None
    author = author_lane(conn, task_id)
    who = closer(conn, task_id, actor=actor, expected_run_id=expected_run_id)
    if not author or not who or who != author:
        return None
    reviewers = review_profiles(conn, task_id, author)
    if reviewers:
        return None
    detail = (
        f"{task_id} names SDLC item {item!r}, which stands at stage {stage!r} -- at or above "
        f"{guard_word!r} and below {accepted_word!r} in the {ladder} item machine -- and this "
        f"close is by the item's author lane {author!r}: the card carries no completed run by "
        f"any other lane. Closing it would strand the item, because the acceptance verdict is "
        f"read from THIS card's own runs and a done card has no review door (request-review "
        f"needs running/ready, reopen-review needs review). Nothing changed. Request the review "
        f"that is owed instead: `hermes kanban request-review {task_id} --reviewer <the design "
        f"authority for {item}>` -- then complete it from review, which records the verdict."
    )
    return Verdict(
        refused=True,
        cause=CAUSE,
        detail=detail,
        facts={
            "item": item,
            "stage": stage,
            "ladder": ladder,
            "author_lane": author,
            "closer": who,
            "reviewers": reviewers,
        },
    )
