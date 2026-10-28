"""The landing-carrier clause: a close that DECLARES a deliverable PR must show
where that PR lands -- on EVERY transition, not only the approval close.

THE TRIGGER IS THE DECLARATION + THE CLOSE (widened 2026-10-14, card `t_502a28bd`;
the rule is platform-stl's ruling of 2026-10-14, superseding the review-only scope
this clause shipped with on `t_88f6cb12`). Judging a close by its TRANSITION
(``if status != "review": return None``) is what let a card whose own completion
metadata recorded a PUBLISHED PR close ``local-only`` with no carrier and no owner
-- three measured orphans on 2026-10-14 (``yaan-hermes-skills#542``/``#545``,
``yaan-platform#1373``), each carried BY HAND tens of seconds AFTER the close the
clause never judged. The clause is now on the completion door for every
transition: a worker's own ``running -> done`` close that declares a PR owes the
same landing leg the approval close owes. A close that declares NOTHING is still
untouched BY CONSTRUCTION -- that property, not the transition, is what keeps
design, research and local-only cards outside this clause.

OBLIGATION COMES FROM A DECLARATION, IN TWO ORDERED TIERS (ruling of 2026-10-23,
card `t_f1dc66dc`, which SUPERSEDES the 2026-10-18 ruling on card `t_9082c751`).
Tier 1 is ``published_pr`` -- the kernel's own PR-acceptance key -- and it still
wins whenever it resolves. Tier 2 is the fleet's looser key set (``pr`` /
``pr_url`` / ``pr_number`` / ``merge_pr`` / ``pull_request``): byte-for-byte the
surface the landing reconciler reads (``scripts/landing_reconcile_nodes.py``,
``PR_KEYS``), so a carrier can never again be FILED from a declaration this clause
-- or the verdict door built on it -- cannot DISCHARGE. That hole was measured:
``t_45655ee5`` was review-closed with ``{"pr": "owner/repo#1944"}`` and contract
``local-only``, the reconciler filed carrier ``t_f4349dba`` off ``metadata.pr``, and
the ONE sanctioned way to discharge it refused because the 2026-10-18 rule read
``pr`` as a mention. Tier 3 is an exact PR-URL ``completion_contract``.

The looser keys are STILL recorded as mentions (:func:`mentioned_prs`, on the
verdict's ``mentions`` fact): a card that CITES a PR without publishing it is worth
keeping in the audit trail. Being a mention is simply no longer why a verdict is
refused. The 2026-10-18 ruling narrowed this door because judging those keys had
refused ``t_ee64588b`` (closed ``local-only`` with ``pr`` pointing at the PR it had
opened); the 2026-10-23 ruling fixes that at the surface instead -- the door now
reads exactly what the reconciler reads (ONE DECLARATION SURFACE), rather than
buying the door's comfort by blinding the reconciler to real declarations.

NO CONTRACT WORD IS A DEAD END (rewritten 2026-10-18, same card). This clause used
to justify its shape with "a PR-bearing contract is unsatisfiable fleet-wide,
because no yoyodine repo can declare repository-required checks". The premise was
true; the advice built on it was a trap, because the same record refused both
doors: ``local-only`` was refused here for contradicting the publication, and the
PR-URL contract was refused by ``kanban_pr_acceptance`` for the missing checks.
``collect_acceptance`` now accepts a PR contract whose repository declares no
checks on MERGE STATE, so a card that published has a REACHABLE route: re-scope
the declaration to the exact PR URL (an OWNER act, ``hermes kanban
set-contract``) and the card closes when the PR merges. The refusal names the
contradiction and offers BOTH viable remedies -- name the carrier, or re-scope to
the PR URL -- and never advises ``local-only`` to a card that has published.

ACCEPTED TRADE (stated here so it is not discovered as a surprise). A worker close
that declares a PR now costs ONE forge read where it used to cost none, and can be
refused ``landing_state_unknown`` when the forge is unreachable. The no-network
escape is the RECORDED ``landing_deferred``, and both refusal texts name it. The
forge is still consulted only when neither a carrier nor a deferral is declared.

WHY THIS EXISTS (measured, card `t_88f6cb12`, ruling on `t_9333ca24`). Approving a
card out of ``review`` is the fleet's most-run transition (538 closes in a 7-day
window, re-measured 2026-10-11 on all 8 board stores; 396 of them declaring a
deliverable PR). The landing leg is deliberately a SEPARATE card
(``[land] <repo>#<n> for <card>``), and nothing checked that the second card was
ever created. When it was skipped the approved work had no owner and no road to
prod while the board read green -- the no-silent-failures shape. Measured residue:
48 of the 407 declared PRs were OPEN at the forge with no carrier on any board
(defcon 38 / platform 7 / migrations 3), 6 of them spot-checked as genuinely open
on cards that already read ``done``.

WHAT IT IS. A clause, not a second system: it is consulted by
``hermes_cli/kanban_db.py::_gate_landing_carrier`` from inside
``complete_task``'s completion-gate family, before the completion write txn,
beside ``_gate_deploy_proof`` and ``prepare_acceptance``. It never raises and it
never mutates the card: it returns a :class:`CompletionRefusal` (as DATA) or
``None``. The gate records ONE auditable event
(``completion_blocked_landing_carrier``) and leaves the card byte-untouched, so
every existing ``if not complete_task(...)`` caller keeps its meaning.

ONE LANDING SURFACE, NEVER TWO (ruling R3). Upstream ``NousResearch/hermes-agent#123429``
adds ``hermes_cli/kanban_landing.py`` (a landed-EVIDENCE gate + stranded routing).
That PR was open and NOT carried into the live install when this module was
written (``platform/t_af8e19a6`` = todo), so this clause is implemented in the
same shape -- a clause module plus the ``_gate_*`` call site -- and the CARRY of
#123429 must fold it into ``kanban_landing.py``'s clause set rather than run
beside it: two landing gates must never both run. Nothing here duplicates
#123429's landed-evidence clauses; by its own text it leaves ``local-only`` cards
untouched and does not cover "no landing leg exists at all", which is this class.

WHAT COUNTS AS A DECLARATION (declarations only -- never prose, never a title
scan). Measured: matching PR refs in card TEXT gave 18 hits in 7 days of which 16
were references, not deliverables (``t_5d630ec4`` lists 61 open PRs; ``t_8f3be9f6``
says "do NOT ride yaan-platform#1118"), so a prose-triggered guard would refuse
legitimate closes. Precedence:

  1. ``metadata.published_pr`` on any run of the card (the kernel's own
     PR-acceptance key) -- tier 1, and it WINS over any tier-2 declaration;
  2. ``metadata.pr`` / ``pr_url`` / ``pr_number`` / ``merge_pr`` / ``pull_request``
     -- tier 2, the fleet's looser keys, the reconciler's ``PR_KEYS`` minus
     ``published_pr`` (see :data:`TIER_KEYS`);
  3. an exact PR-URL ``completion_contract`` -- tier 3.

A tier whose values normalise to no ``owner/repo#N`` (a bare ``pr_number`` with no
repository, prose) is not a declaration: no repository can be derived from it
without guesswork, and guessing is how a fence refuses an honest card -- so that
tier falls THROUGH to the next rather than refusing, and the declaration is
unusable, not damning. The tier-2 keys are ALSO recorded on the verdict's
``mentions`` fact (key, raw value, normalised ref) -- that record is REPORTING, not
judging, and it is no longer the reason a verdict is refused (see the ONE
DECLARATION SURFACE paragraph above). A card that declares nothing is untouched BY
CONSTRUCTION -- a design, research or local-only card is outside this clause.

THE RULE. For each declared PR the close must satisfy ONE of:

  * **landed** -- the forge says MERGED; or a run declares ``merged_as`` /
    ``merge_sha`` and it resolves to an admitted row in the deploy ledger
    (``/opt/hermes_prod/deploy-ledger.jsonl``); or the card declares
    ``landing_not_owed: "<reason>"`` naming the replacement (re-cut / superseded /
    rides another card's PR).
  * **carried** -- (a) ``metadata.landing_card: t_...`` resolving to a card naming
    the same ``owner/repo#N``; or (b) a ``task_links`` CHILD of this card whose
    title/body names it (the doctrine chain review -> ``[land*]`` ->
    deploy/verify); or (c) any non-archived card on ANY board whose title begins
    ``[land`` / ``[owner act]`` and which names it. Not title-prefix alone:
    measured ``[owner act] Land t_0051db75 ...`` (``t_ecc636a4``) is a real
    carrier a ``^\\[land`` filter misses.
  * **deferred** -- ``metadata.landing_deferred: "<reason>"`` (non-empty, >= 20
    chars). The RECORDED deferral is the landing reconciler's own input class, so
    it is visible and owned, never a silent door.

Anything else is refused ``landing_carrier_missing`` with the remedy inline: the
PR, the exact ``kanban_create`` spec for the carrier, both escapes, the doctrine
reference.

FAIL-CLOSED WITHOUT DEADLOCK. The forge is consulted only when neither a carrier
nor a deferral is declared; when it cannot be reached the refusal is
``landing_state_unknown`` on the SAME channel and its remedy needs no network.
NOT bypassable by ``force`` (consistent with ``_gate_deploy_proof``) -- ``--force``
and the dashboard's drag-to-done are not doors around a declaration-based fence;
the recorded declaration is the escape and it is recorded.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

__all__ = [
    "CAUSE_CARRIER_MISSING",
    "CAUSE_DEFERRED_KEYS",
    "CAUSE_NOT_OWED_KEYS",
    "CAUSE_STATE_UNKNOWN",
    "declared_prs",
    "evaluate",
    "is_named_exemption",
    "landing_lane_hint",
    "normalize_ref",
]

CAUSE_CARRIER_MISSING = "landing_carrier_missing"
CAUSE_STATE_UNKNOWN = "landing_state_unknown"

# The two RECORDED escapes. ``landing_deferred`` says "owed, not yet carried" and
# is the landing reconciler's input class; ``landing_not_owed`` says "this PR is
# not the landing" and must name the replacement.
DEFERRED_KEYS = ("landing_deferred",)
NOT_OWED_KEYS = ("landing_not_owed",)
# Kept as aliases for readability at call sites; both are the same tuple shapes.
CAUSE_NOT_OWED_KEYS = NOT_OWED_KEYS
CAUSE_DEFERRED_KEYS = DEFERRED_KEYS

MIN_REASON = 20

_PR_URL = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)"
)
_PR_SHORT = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)\b")
_CARD_ID = re.compile(r"\bt_[0-9a-f]{6,}\b")
_ANY_URL = re.compile(r"https?://\S+")

# The metadata keys that create a landing obligation, in PRECEDENCE ORDER (ruling
# 2026-10-23, card ``t_f1dc66dc`` -- ONE DECLARATION SURFACE). Tier 1 is the kernel's own
# PR-acceptance key, written when a card publishes its deliverable, and it still wins.
# Tier 2 is the fleet's looser key set -- the SAME set the landing reconciler reads
# (``landing_reconcile_nodes.PR_KEYS``) -- so a carrier can no longer be CREATED from a
# declaration this clause (and the verdict door built on it) can never DISCHARGE. Measured
# on ``t_45655ee5``, whose only declaration is ``metadata.pr``.
#
# This supersedes the 2026-10-18 ruling (card ``t_9082c751``) that read these keys as
# mentions only. Narrowing the reconciler instead was rejected: the fleet's closes really do
# declare in ``pr``, so narrowing would stop carriers being filed for genuinely unlanded PRs
# -- losing coverage to fix a door.
TIER_KEYS = (
    ("published_pr",),
    ("pr", "pr_url", "pr_number", "merge_pr", "pull_request"),
)

# Reported on the verdict's facts by :func:`mentioned_prs` as the audit trail of a value a
# card cited. Since ``t_f1dc66dc`` these keys are ALSO tier-2 declarations (judged above), so
# a mention is no longer why a verdict is refused -- the record is reporting, never judging.
MENTION_KEYS = ("pr", "pr_url", "pr_number")


# --------------------------------------------------------------------------- #
# declarations
# --------------------------------------------------------------------------- #


def normalize_ref(value: Any) -> Optional[str]:
    """``owner/repo#N`` from a declared value, or None when it names no repository.

    Accepts the two shapes the fleet actually writes (measured 2026-10-11 over all
    8 board stores): a GitHub PR URL, and the short ``owner/repo#N`` form. A bare
    number is NOT normalisable -- the repository it belongs to is not derivable
    from a declaration, and inventing one is how a fence refuses an honest card.
    """
    if isinstance(value, (bool, int)):
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    match = _PR_URL.search(text)
    if match:
        return f"{match[1]}#{int(match[2])}"
    match = _PR_SHORT.search(text)
    if match:
        return f"{match[1]}#{int(match[2])}"
    return None


def _run_metadata(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    """``task_runs.metadata`` for every run of the card, parsed (newest last).

    Declarations live on the CLOSING run, but a card is often retried: the run
    that opened the PR is frequently not the run that closes the card, so the
    declaration is read off ANY run (ruling R2).
    """
    try:
        rows = conn.execute(
            "SELECT metadata FROM task_runs WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
    except sqlite3.Error:
        return []
    out: list[dict] = []
    for row in rows:
        raw = row["metadata"] if "metadata" in row.keys() else row[0]
        if not raw:
            continue
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def _all_metadata(metadata: Optional[dict], runs: Iterable[dict]) -> list[dict]:
    out = [md for md in runs if isinstance(md, dict)]
    if isinstance(metadata, dict):
        out.append(metadata)
    return out


def declared_prs(
    *,
    metadata: Optional[dict],
    runs_metadata: Iterable[dict] = (),
    contract: Optional[str] = None,
) -> tuple[int, list[str], list[str]]:
    """-> ``(tier, refs, keys_seen)`` for the first declaration tier that resolves.

    Tier 1 ``published_pr`` (the kernel's publication key) beats tier 2, the fleet's
    looser keys ``pr`` / ``pr_url`` / ``pr_number`` / ``merge_pr`` / ``pull_request``
    (:data:`TIER_KEYS`, the reconciler's own key set), which in turn beats tier 3, an
    exact PR-URL ``completion_contract``. A tier whose values carry no repository (a
    bare number) falls THROUGH to the next tier rather than refusing: the declaration
    is unusable, not damning. Widening tier 2 with the reconciler's keys is the
    one-declaration-surface ruling (card ``t_f1dc66dc``): the verdict door can record
    an escape for every declaration a carrier can be filed from. The looser keys are
    still reported as MENTIONS by :func:`mentioned_prs` -- recording, not judging.
    """
    mds = _all_metadata(metadata, runs_metadata)
    for tier, keys in enumerate(TIER_KEYS, start=1):
        seen: list[str] = []
        refs: list[str] = []
        for md in mds:
            for key in keys:
                value = md.get(key)
                if value in (None, ""):
                    continue
                seen.append(key)
                ref = normalize_ref(value)
                if ref:
                    refs.append(ref)
        if refs:
            return tier, sorted(set(refs)), sorted(set(seen))
    ref = normalize_ref(contract)
    if ref:
        return 3, [ref], ["completion_contract"]
    return 0, [], []


def mentioned_prs(
    *,
    metadata: Optional[dict],
    runs_metadata: Iterable[dict] = (),
) -> list[dict]:
    """PRs a card NAMES, kept as a record -- reporting, never judging.

    ``pr`` / ``pr_url`` / ``pr_number`` are the fleet's looser keys: they carry the
    PR a card opened, and equally the PR it investigated, cited, or was told not to
    ride. Since the ONE DECLARATION SURFACE ruling (2026-10-23, card ``t_f1dc66dc``,
    which supersedes 2026-10-18 ``t_9082c751``) those same keys are ALSO tier-2
    DECLARATIONS, judged by :func:`declared_prs`. This function keeps the mention
    record (key, raw value, normalised ref) on the verdict's facts because a card
    that CITES a PR is worth keeping in the trail; it is no longer the reason a
    verdict is refused.
    """
    out: dict[tuple[str, str], dict] = {}
    for md in _all_metadata(metadata, runs_metadata):
        for key in MENTION_KEYS:
            value = md.get(key)
            if value in (None, ""):
                continue
            raw = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
            out[(key, raw)] = {"key": key, "value": raw, "ref": normalize_ref(value)}
    return sorted(out.values(), key=lambda item: (item["key"], item["value"]))


def is_named_exemption(reason: Any) -> bool:
    """True when a declared reason is substantive AND names its replacement.

    ``landing_not_owed`` is "the replacement it rides" -- a bodyless or one-word
    exemption is exactly the silent door this clause exists to close, so the
    reason must carry a referent (a ``repo#N`` ref, a card id, or a URL) as well as
    clearing the length floor.
    """
    if not isinstance(reason, str):
        return False
    text = reason.strip()
    if len(text) < MIN_REASON:
        return False
    return bool(_PR_SHORT.search(text) or _CARD_ID.search(text) or _ANY_URL.search(text))


def _declared_reason(keys: Iterable[str], mds: Iterable[dict], validator) -> Optional[str]:
    for md in mds:
        for key in keys:
            value = md.get(key)
            if isinstance(value, str) and validator(value):
                return value.strip()
    return None


# --------------------------------------------------------------------------- #
# carriers
# --------------------------------------------------------------------------- #


def _names_ref(text: Any, ref: str) -> bool:
    """Does this card text name ``owner/repo#N``? URL or short form, either place."""
    repo, _, number = ref.partition("#")
    if not repo or not number:
        return False
    pattern = re.compile(
        re.escape(repo) + r"(?:#|/pull/)" + re.escape(number) + r"(?![0-9])"
    )
    return bool(pattern.search(str(text or "")))


@dataclass
class _Card:
    task_id: str
    title: str
    body: str
    board: str


def _board_stores(root: Optional[Path] = None) -> list[tuple[Path, str]]:
    """Every board store of the CURRENT kanban root -- "any board", for carriers.

    Rooted at ``kanban_home()`` on purpose (unlike
    ``kanban_scratch.live_board_stores``, which deliberately ignores
    ``HERMES_KANBAN_HOME``): the carriers a card may rely on are the ones the
    install the card is closing on can dispatch from, and a test harness must see
    only its own boards -- never the operator's live ones.
    """
    from hermes_cli.kanban_db import kanban_home

    base = Path(root) if root else Path(kanban_home())
    stores: list[tuple[Path, str]] = [(base / "kanban.db", "default")]
    boards = base / "kanban" / "boards"
    try:
        children = sorted(boards.iterdir())
    except OSError:
        children = []
    for child in children:
        if child.is_dir():
            stores.append((child / "kanban.db", child.name))
    return stores


def _rows(path: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(str(path), timeout=5)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()


_CARRIER_TITLE_FILTER = (
    "status != 'archived' AND (title LIKE '[land%' OR title LIKE '[owner act%')"
)


def _candidate_cards(stores: Optional[list[tuple[Path, str]]] = None) -> list[_Card]:
    """Cards on any board that could BE a carrier, by the doctrine's title shape."""
    out: list[_Card] = []
    for path, board in stores if stores is not None else _board_stores():
        for row in _rows(path, f"SELECT id, title, body FROM tasks WHERE {_CARRIER_TITLE_FILTER}"):
            out.append(_Card(row["id"], row["title"] or "", row["body"] or "", board))
    return out


def _card_by_id(task_id: str, stores: Optional[list[tuple[Path, str]]] = None) -> Optional[_Card]:
    for path, board in stores if stores is not None else _board_stores():
        rows = _rows(
            path,
            "SELECT id, title, body FROM tasks WHERE id = ? AND status != 'archived'",
            (task_id,),
        )
        if rows:
            row = rows[0]
            return _Card(row["id"], row["title"] or "", row["body"] or "", board)
    return None


def _child_cards(conn: sqlite3.Connection, task_id: str) -> list[_Card]:
    """This card's ``task_links`` children (the doctrine chain's second edge).

    Links are intra-board by construction (``task_links`` is per-store), so the
    children are read off the closing card's own connection.
    """
    try:
        rows = conn.execute(
            "SELECT t.id, t.title, t.body FROM task_links l JOIN tasks t ON t.id = l.child_id "
            "WHERE l.parent_id = ? AND t.status != 'archived'",
            (task_id,),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [
        _Card(row["id"], row["title"] or "", row["body"] or "", "this board")
        for row in rows
    ]


def _find_carrier(
    ref: str,
    *,
    conn: sqlite3.Connection,
    task_id: str,
    landing_cards: Iterable[str],
    candidates: Optional[list[_Card]] = None,
    stores: Optional[list[tuple[Path, str]]] = None,
) -> Optional[str]:
    """A carrier for ``ref``, described, or None. Never raises."""
    for card_id in landing_cards:
        card = _card_by_id(card_id, stores)
        if card is not None and (_names_ref(card.title, ref) or _names_ref(card.body, ref)):
            return f"the declared landing_card {card_id}"
    for child in _child_cards(conn, task_id):
        if _names_ref(child.title, ref) or _names_ref(child.body, ref):
            return f"the linked child {child.task_id}"
    for card in candidates if candidates is not None else _candidate_cards(stores):
        if _names_ref(card.title, ref) or _names_ref(card.body, ref):
            return f"the carrier card {card.task_id} on board {card.board}"
    return None


# --------------------------------------------------------------------------- #
# landed: the forge and the ledger
# --------------------------------------------------------------------------- #


class ForgeUnreachable(RuntimeError):
    """The forge state of a declared PR could not be read (auth, network, 404)."""


def forge_state(repo: str, number: str, *, profile_home: Optional[str] = None) -> str:
    """``"merged"`` / ``"open"`` / ``"closed"`` for one PR. Raises ForgeUnreachable.

    Reuses the PR-acceptance module's ``gh`` path verbatim: the same
    ``gh api`` call, the same per-profile identity resolution (``#122689`` -- an
    ambient login cannot read another org's private repos), the same
    credential-free failure reporting. Nothing about the forge transport is
    reinvented here.
    """
    from hermes_cli.kanban_pr_acceptance import _GateAuthError, _api

    try:
        pr = _api(f"repos/{repo}/pulls/{int(number)}", profile_home=profile_home)
    except _GateAuthError as exc:
        raise ForgeUnreachable(str(exc)) from None
    except Exception as exc:  # OSError, CalledProcessError, JSON, KeyError, ...
        raise ForgeUnreachable(type(exc).__name__) from None
    if not isinstance(pr, dict):
        raise ForgeUnreachable("malformed forge response")
    state = str(pr.get("state") or "").lower()
    if state == "merged" or pr.get("merged") is True or pr.get("merged_at"):
        return "merged"
    if state == "open":
        return "open"
    if state == "closed":
        return "closed"
    raise ForgeUnreachable(f"unknown forge state {state!r}")


def _profile_home(assignee: Optional[str]) -> Optional[str]:
    """The assignee profile's gh identity, or None for an unassigned card.

    An UNASSIGNED card uses the ambient login (documented behaviour). An assignee
    that cannot be resolved is NOT a silent fall-through to the ambient login --
    that is the identity leak #122689 closed -- so it raises and the caller reports
    the unreachable channel instead.
    """
    if not assignee:
        return None
    from hermes_cli.kanban_pr_acceptance import _assignee_profile_home

    return _assignee_profile_home(assignee)


def ledger_resolves(sha: Any) -> bool:
    """Does an ADMITTED deploy-ledger row name this merge sha?

    The ledger reader and row predicates are the deploy-proof gate's own
    (``hermes_cli/kanban_proof_gate.py``) -- one ledger, one parser. An
    unreadable ledger (absent, unmounted) resolves nothing; that is a missing
    leg, not an error, and the forge leg still decides.
    """
    sha_text = str(sha or "").strip()
    if len(sha_text) < 7:
        return False
    try:
        from hermes_cli import kanban_proof_gate as pg

        for row in pg._ledger_rows(pg._ledger_path(None)):
            if not pg._is_admitted(row):
                continue
            if pg._ref_names(
                sha_text,
                row.get("landed_sha"),
                row.get("merge_sha"),
                row.get("sha"),
                row.get("merge_commit"),
                row.get("commit"),
            ):
                return True
    except Exception:
        return False
    return False


def _merge_sha_declared(mds: Iterable[dict]) -> Optional[str]:
    for md in mds:
        for key in ("merged_as", "merge_sha"):
            value = md.get(key)
            if isinstance(value, str) and len(value.strip()) >= 7:
                return value.strip()
    return None


# --------------------------------------------------------------------------- #
# the clause
# --------------------------------------------------------------------------- #


@dataclass
class Verdict:
    """The clause's answer as DATA, so a caller can render or log it without guessing."""

    refused: bool
    cause: str = ""
    detail: str = ""
    facts: dict = field(default_factory=dict)


def landing_lane_hint(repo: str) -> str:
    """The landing lane for a repo, as far as it is determinable from here.

    The fleet lane table lives with the OWNERS files and the reconciler (sibling
    card `t_a2c4ccc7`) resolves it deterministically; the kernel must not invent a
    mapping, so the remedy names the resolver instead of guessing a profile.
    """
    return f"the landing lane for {repo} (OWNERS / the fleet lane table)"


def _contract_names_no_pr(contract: Optional[str]) -> bool:
    """True when the card's completion contract declares that NOTHING was published.

    ``local-only`` is the fleet's word for it, but the test is structural, not a
    string match: a contract that normalises to no ``owner/repo#N`` names no PR.
    """
    return normalize_ref(contract) is None


def _ref_url(ref: str) -> str:
    """The exact PR URL for a normalised ``owner/repo#N`` ref (what ``set-contract`` takes)."""
    if "#" in ref:
        repo, _, number = ref.partition("#")
        return f"https://github.com/{repo}/pull/{number}"
    return ref


def _render_contradiction(contract: Optional[str], refs: list[str]) -> str:
    """The contradiction, named FIRST, when the contract word denies the publication.

    A card whose ``completion_contract`` is ``local-only`` asserts that nothing was
    published; the same close's metadata records a PR as published. Both cannot be
    true, and the operator reading the refusal has to see that BEFORE the remedy.

    It is named, not enforced: no close is refused on the contract word alone, and
    BOTH viable remedies are offered below -- name the landing carrier, or re-scope
    the declaration to the exact PR URL (an OWNER act, ``hermes kanban
    set-contract``). A PR-bearing contract is now reachable: when its repository
    declares no required checks, ``kanban_pr_acceptance.collect_acceptance`` accepts
    the MERGED state as the evidence (rewritten 2026-10-18, card ``t_9082c751``).
    What is never offered to a card that has published is ``local-only`` -- the
    state it has already falsified.
    Returns ``""`` when the contract already declares a PR.
    """
    if not _contract_names_no_pr(contract):
        return ""
    word = (contract or "").strip() or "<unset>"
    return (
        f"CONTRADICTION FIRST: this card's completion_contract is {word!r}, which declares that "
        f"nothing was published, while the card's own completion metadata declares "
        f"{', '.join(refs)} as published. Both cannot be true. Two remedies are open: name the "
        f"landing leg that carries it (below), or re-scope the declaration to the exact PR URL "
        f"with `hermes kanban set-contract <task> {_ref_url(refs[0]) if refs else '<pr-url>'}` -- a "
        f"repository that requires no checks is satisfied by the PR's MERGED state, so that "
        f"route closes when the PR lands. Do not re-declare it 'local-only': that is the claim "
        f"its own record already refutes.\n"
    )


def _render_remedy(task_id: str, entries: list[tuple[str, str]]) -> str:
    """The refusal, naming verbatim what the close owes. One remedy per PR."""
    lines: list[str] = []
    for ref, state in entries:
        repo = ref.partition("#")[0]
        lines.append(
            f"  * {ref} — the forge reports it {state}, and no landing carrier names it. "
            f"Create one: kanban_create(title=\"{_carrier_title(ref, task_id)}\", "
            f"assignee=\"{landing_lane_hint(repo)}\", parents=[\"{task_id}\"])."
        )
    return (
        f"{task_id} closes declaring {len(entries)} deliverable PR(s) with no landing leg, so the "
        f"published work would be stranded with the board green:\n"
        + "\n".join(lines)
        + "\nEscapes, both of them recorded declarations on this card (kernel metadata):\n"
        f"  * if a carrier already exists, name it: metadata.landing_card=\"t_...\" (or link it as a "
        f"child of this card) — a carrier is a card naming {_first_ref(entries)}, or any "
        f"non-archived card on any board titled \"[land ...\" / \"[owner act]\" that names it;\n"
        f"  * if the landing is genuinely not owed yet, record it: "
        f"metadata.landing_deferred=\"<why it is deferred, at least {MIN_REASON} characters>\" — the "
        f"recorded deferral is the landing reconciler's input class, so the work stays visible and "
        f"owned;\n"
        f"  * if the PR is superseded or re-cut, say what it rides: "
        f"metadata.landing_not_owed=\"<the replacement it rides>\" (a repo#N ref, a card id or a URL).\n"
        f"Doctrine: the landing-carrier rule — a close whose declared PR is unmerged must name "
        f"its landing carrier, on EVERY transition, worker closes included (design ruling on card "
        f"t_9333ca24; clause t_88f6cb12; widened 2026-10-14 by t_502a28bd). "
        f"`--force` does not bypass a declaration fence."
    )


def _carrier_title(ref: str, task_id: str) -> str:
    return f"[land] {ref} for {task_id}"


def _first_ref(entries: list[tuple[str, str]]) -> str:
    return entries[0][0] if entries else "<the declared PR>"


def evaluate(
    *,
    conn: sqlite3.Connection,
    task_id: str,
    metadata: Optional[dict] = None,
    status: Optional[str] = None,
    contract: Optional[str] = None,
    assignee: Optional[str] = None,
    forge: Optional[Callable[[str, str], str]] = None,
    stores: Optional[list[tuple[Path, str]]] = None,
    candidates: Optional[list[_Card]] = None,
) -> Optional[Verdict]:
    """``None`` = the close is not this clause's business; else a :class:`Verdict`.

    Judged on the DECLARATION, never on the transition: any close of a card whose
    row/runs declare a deliverable PR is judged -- whether it leaves ``review`` or
    is a worker closing its own card (widened 2026-10-14, card ``t_502a28bd``). A
    close that declares nothing returns ``None`` BY CONSTRUCTION, so design,
    research and local-only cards stay outside the clause. ``status`` is carried
    into the verdict's facts for the audit event; it no longer gates judgement.

    Never raises and never writes: the gate
    (``hermes_cli/kanban_db.py::_gate_landing_carrier``) turns a refused verdict into
    the ``CompletionRefusal`` the completion door returns and records the ONE audit
    event, exactly as it does for ``_gate_deploy_proof``. The verdict carries the
    facts that event has to name, so the refusal stays auditable. The keyword
    overrides (``status``/``contract``/``assignee``/``forge``/``stores``/``candidates``)
    exist so a test can drive the clause over its REAL door without a live forge or
    the operator's boards.
    """
    row = conn.execute(
        "SELECT status, assignee, completion_contract FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None:
        return None
    keys = row.keys()
    status = status if status is not None else (row["status"] if "status" in keys else None)
    contract = contract if contract is not None else (
        row["completion_contract"] if "completion_contract" in keys else None
    )
    assignee = assignee if assignee is not None else (
        row["assignee"] if "assignee" in keys else None
    )

    runs = _run_metadata(conn, task_id)
    mds = _all_metadata(metadata, runs)
    tier, refs, keys_seen = declared_prs(metadata=metadata, runs_metadata=runs, contract=contract)
    if not refs:
        return None

    facts: dict = {
        "declaration_tier": tier,
        "declaration_keys": keys_seen,
        # The transition the close came from, and the contract it carries: both are
        # audit facts now that judgement is declaration-driven rather than
        # transition-driven, and the contract is what the contradiction clause names.
        "status": status,
        "contract": contract,
        # PRs the card NAMED without declaring them: recorded, never judged.
        "mentions": mentioned_prs(metadata=metadata, runs_metadata=runs),
        "prs": [],
        "carriers": {},
    }
    deferred = _declared_reason(DEFERRED_KEYS, mds, lambda v: len(v.strip()) >= MIN_REASON)
    not_owed = _declared_reason(NOT_OWED_KEYS, mds, is_named_exemption)
    merge_sha = _merge_sha_declared(mds)
    landing_cards: list[str] = []
    for md in mds:
        value = md.get("landing_card")
        if isinstance(value, str) and value.strip():
            landing_cards.append(value.strip())

    unsatisfied: list[tuple[str, str]] = []
    for ref in refs:
        entry = {"ref": ref}
        carrier = _find_carrier(
            ref,
            conn=conn,
            task_id=task_id,
            landing_cards=landing_cards,
            candidates=candidates,
            stores=stores,
        )
        if carrier:
            entry["carrier"] = carrier
            facts["carriers"][ref] = carrier
            facts["prs"].append(entry)
            continue
        if not_owed:
            entry["state"] = "landing_not_owed"
            entry["reason"] = not_owed
            facts["prs"].append(entry)
            continue
        if merge_sha and ledger_resolves(merge_sha):
            entry["state"] = "landed"
            entry["merged_as"] = merge_sha
            facts["prs"].append(entry)
            continue
        if deferred:
            entry["state"] = "deferred"
            facts["prs"].append(entry)
            continue
        # No carrier, no recorded escape: the forge decides, and only now.
        repo, _, number = ref.partition("#")
        try:
            state = (forge or _default_forge(assignee))(repo, number)
        except ForgeUnreachable as exc:
            facts["prs"].append({**entry, "state": "unknown", "forge_error": str(exc)})
            return Verdict(
                refused=True,
                cause=CAUSE_STATE_UNKNOWN,
                detail=(
                    f"{task_id}: the landing state of the declared PR {ref} could not be read from "
                    f"the forge ({exc}), so the close cannot be admitted. Nothing has been changed on "
                    f"the card. This remedy needs no network: if the landing is owed but not yet "
                    f"carried, record metadata.landing_deferred=\"<why, at least {MIN_REASON} "
                    f"characters>\"; if a carrier already exists, name it with "
                    f"metadata.landing_card=\"t_...\"; if the PR is superseded or re-cut, record "
                    f"metadata.landing_not_owed=\"<the replacement it rides>\". (Doctrine: the "
                    f"landing-carrier rule, design ruling t_9333ca24.)"
                ),
                facts=facts,
            )
        entry["state"] = state
        facts["prs"].append(entry)
        if state == "merged":
            continue
        unsatisfied.append((ref, state))

    if not unsatisfied:
        return Verdict(refused=False, facts=facts)
    facts["unsatisfied"] = [ref for ref, _ in unsatisfied]
    return Verdict(
        refused=True,
        cause=CAUSE_CARRIER_MISSING,
        # The contradiction, when the card's own contract word denies the
        # publication its metadata records, is named BEFORE the remedy -- never
        # instead of it, never as a demand to flip the contract, and never with
        # "use local-only" as the remedy (a published card cannot).
        detail=(
            _render_contradiction(contract, [ref for ref, _ in unsatisfied])
            + _render_remedy(task_id, unsatisfied)
        ),
        facts=facts,
    )


def _default_forge(assignee: Optional[str]) -> Callable[[str, str], str]:
    """The production forge call for one card, resolved once.

    An unresolvable assignee profile is reported as an UNREACHABLE channel (never
    answered with the ambient login): the callable raises ``ForgeUnreachable`` on
    every call, so the card is refused ``landing_state_unknown`` -- the fail-closed
    channel whose remedy needs no network.
    """
    failure: Optional[str] = None
    profile_home: Optional[str] = None
    try:
        profile_home = _profile_home(assignee)
    except Exception as exc:
        failure = f"the assignee profile {assignee!r} cannot be resolved ({type(exc).__name__})"

    def _call(repo: str, number: str) -> str:
        if failure:
            raise ForgeUnreachable(failure)
        return forge_state(repo, number, profile_home=profile_home)

    return _call
