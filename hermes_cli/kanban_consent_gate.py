"""The consent-reference gate: a card may not claim consent it cannot show.

RULING (platform-stl, card ``t_e31d9241``). The defect class: a card whose title
or body asserts that the operator approved it, with no approval row behind the
claim, is believed by every downstream reader -- the dispatcher spawns it, the
lanes treat the consent as settled, and the record then says "approved" about
work nobody approved. Measured instance: card ``t_310fe3f8``
("Immediate liveness (operator approval attached): kanban.max_spawn=3 + gateway
restart") carried no reference, cited no row, and ran to ``done`` while the
open row for that exact consent (``APR-0284``) sat ``pending``.

THE INTERFACE, and which side moves.

* **Trigger ``claim``** -- the card ASSERTS consent (phrase-level, see CLAIM).
  * At the FILING door (:func:`hermes_cli.kanban_db.create_task`) the card is
    REFUSED: a claim that cannot be shown is a false statement in the record,
    and it is refused before any row is written. The filer's move is to cite the
    deciding row, or to rewrite the card as a request for consent.
  * At the RUN door (``kanban_db_dispatch.check_consent_guard``) the claim is
    re-evaluated live -- a row may have been decided, or revoked, since filing --
    and an unbacked claim is refused the spawn and parked ``needs_input``.
* **Trigger ``declared``** -- the card declares ITSELF consent-gated
  ("consent-gated", "needs the operator", "system change, needs Rob") without
  asserting consent. Filing it is legal: proposing work is not doing it. At the
  RUN door it is refused the spawn and parked ``needs_input`` until a ref
  resolves APPROVED -- i.e. the card is HELD, never run and never silently
  skipped. Filing is not refused: deleting the proposal loses the record of work
  the fleet intends to do.

THE REQUIREMENT in both cases: the card carries ``APR-NNNN`` and that reference
resolves, in the approvals store, to a row whose terminal status is ``approved``.
A ``superseded`` row follows ``superseded_by`` to its covering decision; a
``pending``, ``denied`` or ``responded`` row does NOT license a change
(``responded`` is a report, not a decision -- measured: APR-0375's reason is
"skills checkout invariant measured clean").

WHAT IS DELIBERATELY NOT A TRIGGER: the lexical "a write verb and a gated
surface on one line" class. Measured on the same 5,619-card population it fires
on 396 cards (78 undecided) and its live matches are overwhelmingly
PROHIBITIONS -- "PROHIBITED: config.yaml changes", "HARD STOPS: do not edit
config.yaml" -- so as a card-door trigger its precision is ~0 and it would refuse
exactly the cards that are most careful about consent. That class is enforced
where the write actually happens (the tool boundary), not from card prose.
Scope-binding (same board / same card / ancestor) was measured too and REJECTED:
legitimate historical references carry ``board=NULL, card_id=NULL``
(APR-0007/0024/0025) or were cited across boards (APR-0172, APR-0148), so the
clause would refuse honest cards.

FAIL-CLOSED, bounded. If the approvals store cannot be read, an ASSERTED claim
is refused (an unbacked claim must never be believed) and a declared consent-gated
card is refused the run. A card with neither trigger is untouched, so a store
outage cannot halt the fleet -- it can only stop consent-gated work.

Determinism: no inference, no model, no network -- one regex pass over the card's
own text and one read-only sqlite lookup. The store is never written here.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

__all__ = [
    "APPROVALS_DB_ENV",
    "DEFAULT_APPROVALS_DB",
    "LICENSING_STATUSES",
    "ConsentRefused",
    "RefState",
    "Verdict",
    "evaluate",
    "refusal_message",
    "render",
]

APPROVALS_DB_ENV = "KANBAN_APPROVALS_DB"
#: The ops approvals store (APR-NNNN rows), owned by yoyodine-web-services.
DEFAULT_APPROVALS_DB = Path("/opt/hermes_prod/yoyodine-web-services/approvals.db")
#: A reference licenses a change only when its TERMINAL row says so.
LICENSING_STATUSES = ("approved",)
_REF = re.compile(r"\bAPR-0*(\d{1,4})\b", re.I)
_SUPERSEDED_MAX_HOPS = 4

#: Phrase-level assertions of consent. Each pattern is here because it fires on
#: live cards; a bare "approval" is deliberately absent (76% of the population
#: mentions approval without asserting it).
CLAIM = tuple(re.compile(p, re.I) for p in (
    r"operator\s+(?:has\s+)?approved",
    r"approved\s+by\s+(?:the\s+)?(?:operator|rob\b|owner)",
    r"operator\s+(?:approved|consented|signed\s+off|authoris\w*|authoriz\w*)",
    r"approval\s+(?:attached|granted|received|obtained|in\s+hand)",
    r"consent\s+(?:granted|given|obtained|attached)",
    r"(?:with|under)\s+operator\s+(?:approval|authority|consent)",
    r"approved\s+work\s+order",
    r"(?:^|[\n\r*\-|#])\s*APPROVED\s*[:=]",
))

#: Self-declared consent gating. Exact by construction: only a card that says
#: consent is required is held, so the hostile reading of a broad rule -- hold
#: every card that mentions a live surface -- never happens.
DECLARED = tuple(re.compile(p, re.I) for p in (
    r"consent[- ]gated",
    r"\b(?:needs|need|requires|required|awaiting|pending)\s+(?:the\s+)?(?:operator|rob\b|owner)"
    r"(?:\s*'s)?\s*(?:approval|authority|consent|sign[- ]?off|say[- ]?so)",
    r"system\s+change\s*[,:(]\s*needs\s+rob",
))


class ConsentRefused(ValueError):
    """A card's consent claim carries no approval reference that resolves.

    Subclasses ``ValueError`` so every existing create-door caller -- the
    ``kanban_create`` tool, ``hermes kanban create``, sibling create paths --
    already renders it as a refusal instead of an unhandled crash.
    """


@dataclass(frozen=True)
class RefState:
    """One cited reference, resolved against the approvals store."""

    ref: str
    resolved: bool = False
    status: str = ""
    card_id: str = ""
    board: str = ""
    note: str = ""


@dataclass(frozen=True)
class Verdict:
    """The gate's deterministic answer for one card."""

    trigger: str = ""            # "" | "claim" | "declared"
    cause: str = ""              # "" when licensed
    detail: str = ""
    refs: tuple[RefState, ...] = field(default_factory=tuple)
    store_error: str = ""

    @property
    def licensed(self) -> bool:
        return not self.cause

    @property
    def refused(self) -> bool:
        return bool(self.cause)


def _approvals_path(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit)
    return Path(os.environ.get(APPROVALS_DB_ENV) or DEFAULT_APPROVALS_DB)


def _read_rows(path: Path, ids: Sequence[int]) -> dict:
    """Read-only lookup of the cited rows. Raises on an unreadable store."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    try:
        con.row_factory = sqlite3.Row
        out = {}
        for n in ids:
            row = con.execute(
                "SELECT id, status, card_id, board, superseded_by FROM approvals WHERE id = ?", (n,),
            ).fetchone()
            if row is not None:
                out[n] = dict(row)
        return out
    finally:
        con.close()


def _terminal(row: dict, fetch) -> dict:
    """Follow a ``superseded`` row to the decision that covers it."""
    hops = 0
    while (row.get("status") == "superseded" and hops < _SUPERSEDED_MAX_HOPS):
        m = _REF.search(str(row.get("superseded_by") or ""))
        if not m:
            break
        nxt = int(m.group(1))
        nxt_row = fetch(nxt)
        if nxt_row is None or nxt_row["id"] == row["id"]:
            break
        row = nxt_row
        hops += 1
    return row


def _resolve_refs(text: str, path: Path) -> tuple[tuple[RefState, ...], str]:
    ids = sorted({int(m.group(1)) for m in _REF.finditer(text)})
    if not ids:
        return (), ""
    try:
        rows = _read_rows(path, ids)
    except Exception as exc:  # unreadable store -> fail closed, named cause
        return (), f"{path}: {type(exc).__name__}: {exc}"

    def fetch(n: int) -> Optional[dict]:
        if n in rows:
            return rows[n]
        try:
            return _read_rows(path, [n]).get(n)
        except Exception:
            return None

    out = []
    for n in ids:
        ref = f"APR-{n:04d}"
        row = rows.get(n)
        if row is None:
            out.append(RefState(ref=ref, note="no such row in the approvals store"))
            continue
        status = str(row.get("status") or "").strip().lower()
        note = ""
        if status == "superseded":
            term = _terminal(row, fetch)
            note = f"superseded by {term.get('id')} ({str(term.get('status') or '').lower()})"
            row, status = term, str(term.get("status") or "").strip().lower()
            ref = f"APR-{int(row['id']):04d}"
        out.append(RefState(
            ref=ref, resolved=True, status=status,
            card_id=str(row.get("card_id") or ""), board=str(row.get("board") or ""), note=note,
        ))
    return tuple(out), ""


def evaluate(
    title: str,
    body: Optional[str] = None,
    *,
    approvals_db: Optional[str] = None,
) -> Verdict:
    """Judge one card's consent claim. Pure read; never writes anything.

    ``title``/``body`` are the card's own text -- the claim and the reference
    live in the card, so no board or store state is needed to find the trigger.
    """
    text = f"{title or ''}\n{body or ''}"
    trigger = ""
    if any(p.search(text) for p in CLAIM):
        trigger = "claim"
    elif any(p.search(text) for p in DECLARED):
        trigger = "declared"
    if not trigger:
        return Verdict()

    path = _approvals_path(approvals_db)
    refs, store_error = _resolve_refs(text, path)
    if store_error:
        return Verdict(
            trigger=trigger, cause="store_unreadable",
            detail=f"the approvals store could not be read ({store_error})", store_error=store_error,
        )
    if not refs:
        return Verdict(
            trigger=trigger, cause="no_ref",
            detail="no approval reference (APR-NNNN) appears anywhere on the card",
        )
    licensed = [r for r in refs if r.resolved and r.status in LICENSING_STATUSES]
    if licensed:
        return Verdict(trigger=trigger, refs=refs)
    if not any(r.resolved for r in refs):
        return Verdict(
            trigger=trigger, cause="unresolved_ref", refs=refs,
            detail="cited " + ", ".join(r.ref for r in refs) + " -- no such row in the approvals store",
        )
    return Verdict(
        trigger=trigger, cause="ref_not_approved", refs=refs,
        detail="cited " + ", ".join(f"{r.ref}={r.status}" for r in refs if r.resolved)
        + " -- only " + "/".join(LICENSING_STATUSES) + " licenses a change",
    )


#: What the filing lane does instead of asserting consent it cannot show.
MOVES = (
    "1. cite the deciding row on the card (`APPROVAL: APR-NNNN`) when it exists;",
    "2. file the row, then hold the card `blocked`/`needs_input` -- do not run it;",
    "3. drop the claim: rewrite the card as a REQUEST for consent, which is legal to file.",
)


def refusal_message(task_id: Optional[str], verdict: Verdict) -> str:
    """The operator-readable refusal: the cause, the evidence, and the moves."""
    who = f"card {task_id}: " if task_id else ""
    what = ("asserts operator consent" if verdict.trigger == "claim"
            else "declares itself consent-gated")
    lines = [
        f"CONSENT REFUSED ({verdict.cause}) -- {who}{what}, but {verdict.detail}.",
        "A card that claims consent must carry a reference that resolves to an APPROVED row "
        f"in the approvals store ({_approvals_path()}).",
    ]
    for r in verdict.refs:
        lines.append(f"  - {r.ref}: {r.status or 'unresolved'}"
                     + (f" ({r.note})" if r.note else ""))
    lines.append("Instead:")
    lines.extend(f"  {m}" for m in MOVES)
    return "\n".join(lines)


def render(task_id: Optional[str], verdict: Verdict) -> str:
    """Alias kept for the ``render()`` convention the proof gate uses."""
    return refusal_message(task_id, verdict)
