"""The declared-gate edge gate: a card that says it waits on another CARD must be
WAITING on it -- the edge, not the prose, is what holds work back.

RULING (platform-stl, card ``t_26d2f957``). Measured instance: ruling card
``t_cd34976b`` filed three carriers for work that trims a 5.97 GB store and wrote
in its body that all three were "gated on operator consent t_a1e53cdd". On the
board the parent edge of each carrier was the RULING CARD (``done``), not the
consent card, so two of them sat ``ready`` and a dispatcher tick would have
claimed them. The gate existed in prose and nowhere in the graph: the claim and
the edge are maintained by two different acts, by two different agents, at two
different times, and NOTHING compared them. The failure mode is SILENT AND
PERMISSIVE -- an ungated carrier looks exactly like a gated one until it
dispatches. (Its mirror, a false NEGATIVE, is card ``t_75eb1fb4``: an archived
parent gating a child forever.)

THE DEFECT IS NOT "A CARD MENTIONS ANOTHER CARD". Measured on the live boards,
the naive rule -- any ``t_<id>`` in a dispatchable card's body that is not an
ancestor -- fires on 1,441 dispatchable cards, and its matches are overwhelmingly
cross-references: "see ``t_x`` for the ruling", "the window in ``t_y``",
"discharges ``t_z``". A door keyed on that would refuse or hold the fleet's
honest paperwork. What is a claim is the *predicate* form: a gate verb that
GOVERNS a card id -- "gated on ``t_x``", "blocked until ``t_x``", "waiting on
card ``t_x``", or the subject form "the consent card ``t_x`` is still blocked".
That tight form measures 17 unbacked cards on 2,775 dispatchable ones across six
boards (platform 13, financially 2, research 2, defcon/migrations/skills-refactor
0), and 15 of the 17 are genuine: the two exceptions are a sentence about a
SUPERSEDED card ("...which was filed with a parent edge that gated it behind
``t_x``") and a "held by <lane>)" whose card id is a later noun. 88% precision is
the wrong number for a REFUSAL at filing and the right number for a HOLD: the
precise, cheap action is to refuse the SPAWN and say which edge is missing, not
to refuse the FILING and lose the record of intended work. That is why this gate
lives at the RUN door and its read-only audit lives on the board.

SCOPE IS PART OF THE CLAIM, and getting it wrong in either direction breaks
something. The same hand repair that added the missing edges also parented the
two BUILD carriers on the consent card -- but ``t_6c45df81`` opens with "BUILD
AND PR NOW. DO NOT MERGE OR DEPLOY" and says its LANDING is gated on the answer.
A parent edge freezes the build the ruling itself ordered: the repair fixed a
permissive defect by creating the mirror false-negative one. So a claim carries a
scope, decided deterministically from the vocabulary of the paragraph that makes
it:

* ``start``  -- the card may not be CLAIMED until the named card is finished
  ("do not apply before", "run only after", "start when"). Enforced here: the
  dispatcher refuses the spawn and parks ``needs_input``.
* ``landing`` -- claiming is legal and wanted (build, open the PR); the LANDING is
  what must not happen ("do not merge", "landing gated on", "not before deploy").
  Not a spawn hold -- that would be the over-correction above. The audit reports
  it, and the durable representation is a parent edge on the card that LANDS,
  which in this fleet is a separate ``[land]`` carrier.

An ambiguous claim (both vocabularies, or neither) is ``start``: fail-closed on
the side where the harm is a data-destroying run, never on the side where the
harm is a card waiting for an edge someone can add in one comment.

WHAT IS NOT A TRIGGER, measured and rejected: a bare mention of a card id
(cross-references dominate, 1,441 hits); the noun class "gate PRs"/"gate.py"/
"GATE-TOP-LEVEL-ONLY" (fires on the fleet's own vocabulary); a claim whose named
card is already FINISHED (``done``/``archived``/``cancelled`` -- the precondition
is satisfied, the card may run) or unknown to the board (a prose artifact, not a
gate); and a claim naming the card itself.

Determinism: one regex pass over the card's own LIVE title/body plus one
read-only parent walk on the board's own connection. No model, no network, no
write. The module never mutates a card; ``evaluate`` answers, the caller acts.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

__all__ = [
    "DISPATCHABLE_STATUSES",
    "FINISHED_STATUSES",
    "GateClaim",
    "Verdict",
    "ancestors",
    "audit",
    "claims",
    "evaluate",
    "refusal_message",
    "render",
    "scope_of",
]

#: A card in one of these states can still be claimed by a dispatcher tick.
DISPATCHABLE_STATUSES = ("ready", "todo", "triage", "scheduled")
#: A named card in one of these states has already satisfied any gate it makes.
FINISHED_STATUSES = ("done", "archived", "cancelled")

_CARD_ID = r"`?\bt_[0-9a-f]{8}\b`?"
_ID = re.compile(_CARD_ID)

#: The PREDICATE form: a gate verb that governs the card id as its object.
#: The verb must be followed by a preposition -- that is what excludes the noun
#: class ("gate PRs", "gate.py", "gate-scope") that wrecked the loose trigger.
_PREDICATE = re.compile(
    r"\b(?P<verb>gated|gating|blocked|depend(?:s|ent)?|contingent|await(?:s|ing)?|"
    r"waiting|held|parented|sequenced|preconditioned)\b"
    r"(?:\s+\w+){0,3}?\s+(?:on|by|with|behind|upon|until|after|for)\b"
    r"(?P<tail>[^.;\n]{0,40}?)" + _CARD_ID,
    re.IGNORECASE,
)
#: The PROHIBITION form: "do not apply/merge/deploy ... before|until t_x". This is
#: the shape ``t_107f67d4`` uses ("Operator-gated ... do not apply before its
#: answer") and ``t_6c45df81`` repeats ("Do **not** merge or deploy before the
#: operator answers t_x"). The gate verb never governs the id here -- the
#: PROHIBITION does -- so it needs its own clause. The markdown-tolerant ``not``
#: is deliberate: the live card bolds it.
_PROHIBITION = re.compile(
    r"\bdo\s+\*{0,2}not\*{0,2}\s+"
    r"(?:apply|merge|land|deploy|ship|publish|release|start|run|execute|touch|"
    r"prune|delete|edit|write|install|change)\b"
    r"[^.;\n]{0,60}?\b(?:before|until|without)\b"
    r"[^.;\n]{0,40}?" + _CARD_ID,
    re.IGNORECASE,
)
#: The SUBJECT form: the named card is described as unfinished. This is the shape
#: the measured instance actually used -- "The operator consent card t_a1e53cdd
#: is still blocked" -- where the gate word never governs the id at all.
_SUBJECT = re.compile(
    _CARD_ID
    + r"(?P<tail>[^.;\n]{0,40}?)\b(?:is|are|stays?|remains?|sits?)\b"
      r"(?:\s+\w+){0,2}?\s+(?:still\s+)?(?:blocked|pending|unfinished|undecided|"
      r"open|awaiting|not\s+done|unanswered|unresolved)\b",
    re.IGNORECASE,
)
#: Bare "awaiting t_x" / "pending t_x" (the verb form without an object phrase).
_BARE = re.compile(r"\b(?:awaiting|pending|waiting\s+for)\s+" + _CARD_ID, re.IGNORECASE)

#: A negation in front of the gate word RELEASES the claim: "not gated on t_x",
#: "no longer blocked on t_x".
_NEGATION = re.compile(
    r"\b(?:not|never|no\s+longer|isn'?t|aren'?t|wasn'?t|is\s+not|are\s+not)\s*$", re.IGNORECASE
)
#: A sentence about a SUPERSEDED card describes THAT card's gates, not this one's.
_SUPERSEDES = re.compile(r"\bsupersed\w*", re.IGNORECASE)
#: The REVERSE direction: the named card is the one waiting on THIS card
#: ("t_x is blocked on this card", "t_x stays open until this lands"). Measured
#: false-positive class -- 7 of the first 21 start-holds were this shape -- so a
#: claim is dropped when the tail says the named card waits on us.
_REVERSE = re.compile(
    r"\b(?:on|until|by|behind)\s+(?:this|it|us)\b",
    re.IGNORECASE,
)
#: "a SEPARATE card gated on t_x" -- the claim is about a different card that this
#: prose happens to describe. Measured: it is how a decomposition section reads.
_OTHER_SUBJECT = re.compile(r"\b(?:separate|another|other|duplicate|predecessor)\s+card\b",
                            re.IGNORECASE)
#: NARRATION: the sentence is telling the reader about the board ("it is already
#: held by card t_x", "blocked upstream by t_y"), not instructing this worker.
#: Measured: this is the discriminator between the half of the syntactic hits that
#: are genuine gates and the half that are a status report.
_NARRATION = re.compile(
    r"\balready\b|\bupstream\b|\bnever\s+dispatches\b|\bstays?\s+open\b|"
    r"\bso\s+it\b|\bwhich\s+is\b|\bthanks\s+to\b",
    re.IGNORECASE,
)
#: CONSENT/AUTHORITY vocabulary. The RUN door holds only claims in this class; the
#: general "declared gate" class is reported by the audit and never freezes a run.
#: Measured basis: on 7,532 dispatchable cards the syntactic forms alone yield 8-12
#: start-scope hits of which roughly half describe another card's state, but the
#: consent-flavoured imperative subset is the one the measured instance populated
#: and the one whose failure destroys data.
_CONSENT_VOCAB = re.compile(
    r"(?:consent|approv\w*|sign[- ]?off|operator|\bowner\b|authoris\w*|authoriz\w*)",
    re.IGNORECASE,
)

#: Vocabulary that decides the claim's SCOPE. LANDING first: a paragraph that says
#: "do not merge or deploy before X" is a landing gate even when it also says the
#: word "build".
_LANDING_VOCAB = re.compile(
    r"\b(?:land|lands|landing|landed|merge[sd]?|merging|deploy\w*|ship\w*|"
    r"publish\w*|release[sd]?|go\s+live|cut\s*over)\b",
    re.IGNORECASE,
)
_START_VOCAB = re.compile(
    r"\b(?:start\w*|run\w*|ran|execut\w*|apply|applies|applied|applying|begin|"
    r"touch\w*|perform\w*|prune\w*|delet\w*|edit\w*|writ(?:e|es|ing)\b|install\w*)\b",
    re.IGNORECASE,
)

#: How much paragraph text around a claim decides its scope.
_SCOPE_WINDOW = 600


@dataclass(frozen=True)
class GateClaim:
    """One card id that this card's prose says it waits on, and at what scope."""

    card_id: str
    scope: str        # "start" | "landing"
    evidence: str     # the sentence, trimmed -- quoted back to the operator
    form: str = "predicate"   # "predicate" | "prohibition" | "subject" | "bare"
    weak: bool = False
    """True for a NARRATIVE mention rather than an instruction to this worker.

    Measured need: on 7,532 dispatchable cards the syntactic forms together yield
    12 start-scope hits, and HALF of them are prose about a different card's state
    ("the skill-caps class is already held by card t_x", "t_5967106c is gated on
    t_y, so it never dispatches"). Those describe the board to a reader; they do
    not tell THIS card to wait. So a weak claim is still REPORTED by the audit --
    recall is not lost, only the authority to park a card is withheld -- while the
    RUN door acts only on a strong one.
    """

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.card_id} ({self.scope})"


@dataclass(frozen=True)
class Verdict:
    """The gate's deterministic answer for one card."""

    task_id: str = ""
    claims: tuple[GateClaim, ...] = field(default_factory=tuple)
    unbacked: tuple[GateClaim, ...] = field(default_factory=tuple)
    finished: tuple[str, ...] = field(default_factory=tuple)
    unbacked_start: tuple[GateClaim, ...] = field(default_factory=tuple)
    unbacked_landing: tuple[GateClaim, ...] = field(default_factory=tuple)
    unbacked_weak: tuple[GateClaim, ...] = field(default_factory=tuple)

    @property
    def refused(self) -> bool:
        """True when a STRONG START-scope gate has no edge: refuse the spawn.

        A weak (narrative) claim never refuses a run -- it is reported instead, so
        the audit keeps the recall without giving a status report the authority to
        freeze another lane's card.
        """
        return bool(self.unbacked_start)

    @property
    def reported(self) -> bool:
        """True when ANY gate is unbacked -- the read-only audit's row."""
        return bool(self.unbacked)

    @property
    def cause(self) -> str:
        return "gate_edge_missing" if self.refused else ""


def _trim(text: str, start: int, end: int, *, pad: int = 90) -> str:
    lo = max(0, start - pad)
    hi = min(len(text), end + pad)
    return " ".join(text[lo:hi].split())


def scope_of(text: str, at: int) -> str:
    """``"landing"`` or ``"start"`` for the claim at ``at`` in ``text``.

    The SENTENCE that makes the claim decides, because that is where the speaker
    says what the gate is on. Measured need: the wider paragraph is not safe -- a
    bullet list is one "paragraph" to a blank-line splitter, so an Acceptance block
    that mentions "Run record names ..." flipped two LANDING claims to ``start``
    and would have frozen a build the ruling ordered. When the claim sentence
    carries no scope vocabulary at all, the enclosing paragraph (capped at
    ``_SCOPE_WINDOW``) is consulted, and only then does the answer default to
    ``start`` -- fail-closed, because a wrongly-released run can destroy data while
    a wrongly-held card costs one comment.
    """
    lo = max(0, at - _SCOPE_WINDOW)
    hi = min(len(text), at + _SCOPE_WINDOW)
    window = text[lo:hi]
    rel = at - lo

    def decide(chunk: str) -> str:
        landing = bool(_LANDING_VOCAB.search(chunk))
        start = bool(_START_VOCAB.search(chunk))
        if landing and not start:
            return "landing"
        if start and not landing:
            return "start"
        return ""  # neither, or both: undecided at this granularity

    # 1. the sentence that makes the claim
    sent_lo = max(window.rfind(c, 0, rel) for c in (". ", "\n", "; ")) + 1 if any(
        window.rfind(c, 0, rel) != -1 for c in (". ", "\n", "; ")) else 0
    ends = [window.find(c, rel) for c in (". ", "\n", "; ")]
    ends = [e for e in ends if e != -1]
    sent_hi = (min(ends) + 1) if ends else len(window)
    verdict = decide(window[sent_lo:sent_hi])
    if verdict:
        return verdict

    # 2. the enclosing paragraph
    para_start = window.rfind("\n\n", 0, rel)
    para_end = window.find("\n\n", rel)
    paragraph = window[para_start + 2 if para_start != -1 else 0:
                       para_end if para_end != -1 else len(window)]
    verdict = decide(paragraph)
    if verdict:
        return verdict

    # 3. fail-closed
    return "start"


def claims(text: Optional[str], *, own_id: Optional[str] = None) -> tuple[GateClaim, ...]:
    """Every gate claim in ``text``, deduped by card id (first claim wins).

    ``own_id`` drops a card that names itself -- a self-gate is not a dependency,
    it is a wording artifact.
    """
    text = text or ""
    found: dict[str, GateClaim] = {}
    for kind, rx in (("predicate", _PREDICATE), ("prohibition", _PROHIBITION),
                     ("subject", _SUBJECT), ("bare", _BARE)):
        for m in rx.finditer(text):
            idm = _ID.search(m.group(0))
            if idm is None:
                continue
            card_id = idm.group(0).strip("`")
            if own_id and card_id == own_id:
                continue
            # the negation must sit immediately before the claim, not anywhere on the line
            lead = text[max(0, m.start() - 16):m.start()]
            if _NEGATION.search(lead):
                continue
            evidence = _trim(text, m.start(), m.end())
            # a sentence about a superseded card states that card's gate, not ours
            if _SUPERSEDES.search(evidence):
                continue
            # the named card waiting on US is the reverse dependency, not our gate
            tail_after = text[m.end():m.end() + 48]
            if _REVERSE.search(tail_after) or _REVERSE.search(evidence):
                continue
            # "a separate card gated on t_x" is not a claim about this card
            if _OTHER_SUBJECT.search(evidence):
                continue
            if card_id in found:
                continue
            # A claim's AUTHORITY to park a card depends on whether the prose
            # instructs THIS worker or merely narrates the board.
            narrating = bool(_NARRATION.search(evidence))
            consentful = bool(_CONSENT_VOCAB.search(evidence)) or kind == "prohibition"
            weak = narrating or not consentful
            found[card_id] = GateClaim(
                card_id=card_id,
                scope=scope_of(text, m.start()),
                evidence=evidence,
                form=kind,
                weak=weak,
            )
    return tuple(found.values())


def ancestors(conn: sqlite3.Connection, task_id: str) -> set[str]:
    """Transitive parents of ``task_id`` on THIS board's connection.

    Iterative and cycle-safe: ``kanban_link`` refuses cycles, but a store written
    by hand (or by an older build) must never hang a dispatcher tick.
    """
    seen: set[str] = set()
    frontier = [task_id]
    while frontier:
        nxt: list[str] = []
        for cur in frontier:
            for (parent,) in conn.execute(
                "SELECT parent_id FROM task_links WHERE child_id = ?", (cur,)
            ):
                if parent and parent not in seen:
                    seen.add(parent)
                    nxt.append(parent)
        frontier = nxt
    return seen


def evaluate(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    title: Optional[str] = None,
    body: Optional[str] = None,
    status: Optional[str] = None,
) -> Verdict:
    """Judge one card's declared gates. Pure read; never writes anything.

    ``title``/``body``/``status`` are the caller's already-read LIVE row when it
    has one (the dispatcher does); otherwise the row is read here. The claim is
    always evaluated against the LIVE text and the LIVE edge table -- a card
    amended after filing is judged as it stands now, exactly as
    ``check_consent_guard`` re-evaluates its own trigger.
    """
    if title is None or body is None:
        row = conn.execute(
            "SELECT title, body, status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return Verdict(task_id=task_id)
        title, body = row["title"], row["body"]
        status = status if status is not None else row["status"]

    found = claims(f"{title or ''}\n{body or ''}", own_id=task_id)
    if not found:
        return Verdict(task_id=task_id)

    anc = ancestors(conn, task_id)
    unbacked: list[GateClaim] = []
    finished: list[str] = []
    for claim in found:
        if claim.card_id in anc:
            continue  # the edge exists: the gate is represented in the graph
        if task_id in ancestors(conn, claim.card_id):
            # this card is the named card's ANCESTOR: the prose is describing the
            # reverse dependency ("t_x is blocked on this card"), not our gate
            continue
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (claim.card_id,)
        ).fetchone()
        if row is None:
            continue  # not a card on this board: prose, not a gate
        if row["status"] in FINISHED_STATUSES:
            finished.append(claim.card_id)
            continue  # the precondition is satisfied; the card may run
        unbacked.append(claim)

    return Verdict(
        task_id=task_id,
        claims=found,
        unbacked=tuple(unbacked),
        finished=tuple(finished),
        unbacked_start=tuple(c for c in unbacked if c.scope == "start" and not c.weak),
        unbacked_landing=tuple(c for c in unbacked if c.scope == "landing" and not c.weak),
        unbacked_weak=tuple(c for c in unbacked if c.weak),
    )


def audit(
    conn: sqlite3.Connection,
    *,
    statuses: Sequence[str] = DISPATCHABLE_STATUSES,
    limit: Optional[int] = None,
) -> list[Verdict]:
    """Read-only census: every card in a claimable state with an unbacked gate.

    This is the read-only half of the guardrail -- cheap enough to run in the same
    tick a ruling files its carriers, which is exactly when the measured instance
    was created. Ordered ``start`` before ``landing`` (the ones that can actually
    be claimed into harm first), then by card id for a stable diff.
    """
    marks = ",".join("?" for _ in statuses)
    rows = conn.execute(
        f"SELECT id, title, body, status FROM tasks WHERE status IN ({marks}) "
        f"ORDER BY id",
        tuple(statuses),
    ).fetchall()
    out: list[Verdict] = []
    for row in rows:
        v = evaluate(
            conn, row["id"], title=row["title"], body=row["body"], status=row["status"],
        )
        if v.reported:
            out.append(v)
    out.sort(key=lambda v: (0 if v.refused else 1, v.task_id))
    return out[:limit] if limit is not None else out


def refusal_message(task_id: Optional[str], verdict: Verdict) -> str:
    """The operator-readable refusal: the claim, the missing edge, and the fix."""
    who = f"card {task_id}: " if task_id else ""
    lines = [
        f"GATE EDGE MISSING -- {who}this card declares that it waits on another card, "
        f"and the board does not represent it.",
        "",
        "A declared gate must be an EDGE: prose holds nobody back, and an ungated "
        "carrier looks exactly like a gated one until the dispatcher claims it.",
    ]
    for c in verdict.unbacked:
        lines.append(f"  - {c.card_id} ({c.scope} scope, {c.form}): \"{c.evidence}\"")
    for cid in verdict.finished:
        lines.append(f"  - {cid}: finished, so the gate is satisfied -- not a fault")
    lines += [
        "",
        "Instead, one of:",
        "  1. add the edge -- `parents=[...]` on the filing call, or `hermes kanban link`;",
        "  2. if the named card is finished, say so and this clears on the next tick;",
        "  3. if this card is really gated on LANDING rather than on starting, write "
        "that scope on the card AND put the edge on the `[land]` carrier that lands it.",
        "",
        "A `landing`-scope claim does NOT hold a spawn (building is wanted); the audit "
        "reports it, and the edge belongs on the card that performs the landing.",
    ]
    return "\n".join(lines)


def render(task_id: Optional[str], verdict: Verdict) -> str:
    """Alias kept for the ``render()`` convention the other kanban gates use."""
    return refusal_message(task_id, verdict)


def _main(argv: Optional[Iterable[str]] = None) -> int:  # pragma: no cover - operator CLI
    """``python -m hermes_cli.kanban_gate_edge <board-db>`` -- the read-only audit."""
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m hermes_cli.kanban_gate_edge",
        description="Report dispatchable cards whose declared gate has no edge.",
    )
    ap.add_argument("db", help="path to a board kanban.db")
    ap.add_argument("--all", action="store_true",
                    help="every card state, not only the claimable ones")
    args = ap.parse_args(list(argv) if argv is not None else None)

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        if args.all:
            rows = con.execute(
                "SELECT id, title, body, status FROM tasks ORDER BY id").fetchall()
            verdicts = []
            for row in rows:
                v = evaluate(con, row["id"], title=row["title"], body=row["body"],
                             status=row["status"])
                if v.reported:
                    verdicts.append(v)
        else:
            verdicts = audit(con)
    finally:
        con.close()

    start = [v for v in verdicts if v.refused]
    landing = [v for v in verdicts if v.reported and not v.refused]
    print(f"declared-gate audit: {len(verdicts)} card(s) with an unbacked gate "
          f"({len(start)} start-scope, {len(landing)} landing-scope)")
    for v in start + landing:
        scope = "start" if v.refused else "landing"
        ids = ", ".join(c.card_id for c in v.unbacked)
        print(f"  {v.task_id}  [{scope}]  missing edge -> {ids}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
