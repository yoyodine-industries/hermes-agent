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
escape is a NAMED CARRIER (``landing_card`` / a deferral naming a ``t_...`` card),
and both refusal texts name it. The forge is still consulted only when no carrier
is declared.

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
  * **carried** -- THE KEY FIRST, PROSE AS THE LEGACY FALLBACK (card
    ``t_4a87bb75``; ruling ``t_2bc058fc``): (a) a card on ANY board whose
    ``idempotency_key`` is ``landing-carrier:<owner>/<repo>#<N>`` -- ARCHIVED
    INCLUDED, because a carrier that has served its landing is archived by the
    board sweep and its key is a RECORD that the landing was carried, not an
    absence; (a') the read-only legacy alias ``landing-reconcile:<repo>#<N>`` the
    landing flow has filed since 2026-10, accepted ONLY when that card also names
    the full ``owner/repo#N`` (the alias is owner-less, and
    ``hermes-agent#101`` is measurably the fork or upstream); or (b)
    ``metadata.landing_card: t_...`` resolving to a card naming the same
    ``owner/repo#N``; or (c) a ``task_links`` CHILD of this card whose
    title/body names it (the doctrine chain review -> ``[land*]`` ->
    deploy/verify); or (d) any card on ANY board whose title begins
    ``[land`` / ``[owner act]`` and which names it. Not title-prefix alone:
    measured ``[owner act] Land t_0051db75 ...`` (``t_ecc636a4``) is a real
    carrier a ``^\\[land`` filter misses.

    The prose paths are KEPT as the legacy fallback, never deleted: 521 of the 801
    measured carrier receipts are keyless, and a keyless carrier must not start
    failing closes (the fence is fed by what the fleet already filed). Every path
    above is an EXISTENCE read -- "does a carrier exist for this ref" -- so every
    one of them admits an archived card; the LIVE-card filter belongs to callers
    that will ACT on a card (``kanban_db.create_task``'s idempotency dedupe), not
    to this question.
  * **stamp** -- a run declares the landed revision (``merged_as`` / ``merge_sha``
    / ``landed_sha`` / ``deploy_stamp``) and it resolves to an ADMITTED row in the
    deploy ledger. This is the DEPLOY STAMP leg: the artifact on the dest names the
    commit, so the close is evidence, not intent.
  * **deferred, but only to a NAMED CARRIER** -- ``metadata.landing_deferred``
    must name the card that carries the landing (a ``t_...`` id) and that card
    must itself exist and name the same ``owner/repo#N``. A bare reason is not an
    escape: "owed, not yet carried" IS the open state, so a close that wants to
    move on files a carrier card and names it. (Tightened 2026-10-28 by operator
    order: three cards closed "done" in one day on a >=20-character reason, with
    their PRs unmerged and the defects they targeted still live.)

Anything else is refused ``landing_carrier_missing`` with the remedy inline: the
PR, the exact ``kanban_create`` spec for the carrier, both escapes, the doctrine
reference.

FAIL-CLOSED WITHOUT DEADLOCK. The forge is consulted only when neither a carrier
nor a deferral is declared; when it cannot be reached the refusal is
``landing_state_unknown`` on the SAME channel and its remedy needs no network.
NOT bypassable by ``force`` (consistent with ``_gate_deploy_proof``) -- ``--force``
and the dashboard's drag-to-done are not doors around a declaration-based fence;
the recorded declaration is the escape and it is recorded.

A CARRIER STAMPS ITS VERDICT (card ``t_1e8fe1f9``, design ``t_1746ba2e``). The clause
above VERIFIES a landing at the close door but STAMPS nothing, so the verified fact
survived only as prose: 237 land-shaped ``done`` cards carried an empty ``result``
since 10-05, while the PR pointer sat in the run SUMMARY and in ad-hoc metadata keys
(``merge_sha`` x12, ``merge_commit`` x4, ``merged_sha``, ``merge``,
``landing_receipt`` x2, ``landing_not_owed`` x1, measured over one day) that no audit
can read deterministically. Prose is not evidence -- this module's own measurement
above is 16 false positives in 18 hits -- so the fix is a NAMED key, not a text rule.
A card that is ITSELF a carrier (by TITLE shape alone, never body prose, and never a
second landing gate) must therefore close with ``metadata["landing_verdict"]``, a dict
holding exactly one of THREE states:

  * ``{"state": "landed", "ref": "<owner/repo#N>", "merge_sha": "<hex>"}`` -- the
    landing HAPPENED, at a commit: admitted only when this clause's OWN resolution of
    that landing AGREES (the forge reports the PR merged and its ``merge_commit_sha``
    is that commit, or an admitted deploy-ledger row carries it as its
    ``upstream_pin``). Disagreement, or a shape that cannot resolve to a PR + commit,
    -> ``landing_verdict_mismatch``; an unreachable forge with no ledger pin ->
    ``landing_state_unknown``, the same fail-closed channel as above.
  * ``{"state": "not_owed", "ref": "<owner/repo#N or the superseded card id>",
    "reason": "<text>"}`` -- admitted only when the existing
    :func:`is_named_exemption` passes, i.e. the exemption is substantive AND names
    what rides it. Otherwise -> ``landing_verdict_unsubstantiated``.
  * ``{"state": "deferred", "ref": "<owner/repo#N>", "carrier": "t_..."}`` -- the
    obligation has MOVED to a card, and the CARD is what makes it durable (design
    amendment 2026-10-29, which SUPERSEDES ``t_1e8fe1f9`` item 2's narrower two-state
    contract). Live already admits a prose deferral ONLY when it names a card that
    EXISTS and itself names the same PR (:func:`is_named_carrier_deferral` +
    ``_find_carrier``, tightened 2026-10-28 by operator order); refusing the same
    proved pointer because it arrives as a named key rather than as prose would
    false-refuse honest closes -- the opposite failure of the silent door this
    contract exists to close. Admitted only when the named card exists and names the
    ref, i.e. exactly the verification a prose deferral already gets. Otherwise ->
    ``landing_verdict_unsubstantiated``.

Anything else, or a stamp that is not a dict, -> ``landing_verdict_missing``. A bare prose
``landing_deferred`` is NOT a verdict: the 2026-10-28 rule is unchanged, and a carrier
that wants to defer NAMES its carrier card.

THE ORDERING is part of the contract, because it is what keeps this addition ADDITIVE. A
carrier that STAMPS a verdict is judged on the stamp FIRST: the stamp is the strongest
record the clause can read, and judging the declaration first would refuse
``landing_carrier_missing`` a card that IS the carrier and has already proved its own
landing at its own close. A carrier that stamped NOTHING falls through to the declaration
path UNCHANGED -- the populated-record half (``hermes_cli/kanban_landing_verdict.py``, card
``t_765136ce``, already live) records its verdict as a named declaration, and those closes
must keep passing exactly as they do today. Only a carrier whose close records NOTHING AT
ALL is refused ``landing_verdict_missing``: that is the EMPTY record this half of the card
owns, and it is the measured class -- 237 land-shaped ``done`` cards with an empty
``result``, the PR pointer surviving only in prose.

The three causes are registered into the kernel's ``COMPLETION_REFUSAL_CAUSES`` at
IMPORT (:func:`_register_completion_causes`), so ``hermes_cli/kanban_db.py`` stays
byte-identical: ONE landing surface, no kernel edit (ruling R3). Non-carrier cards
(``[review]``, ``[deploy]``, ``[fix]``, design cards) are UNTOUCHED -- the requirement
is scoped to :data:`CARRIER_TITLE_PATTERNS` alone, and a card outside that scope falls
through to the declaration path unchanged. The verdict is READ back from the run
metadata the close already writes, so the clause still never writes and never raises.
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
    "CARRIER_TITLE_PATTERNS",
    "CAUSE_CARRIER_MISSING",
    "CAUSE_DEFERRED_KEYS",
    "CAUSE_NOT_OWED_KEYS",
    "CAUSE_STATE_UNKNOWN",
    "CAUSE_VERDICT_MISMATCH",
    "CAUSE_VERDICT_MISSING",
    "CAUSE_VERDICT_UNSUBSTANTIATED",
    "LANDING_VERDICT_KEY",
    "VERDICT_CAUSES",
    "declared_prs",
    "evaluate",
    "is_carrier_title",
    "is_named_exemption",
    "landing_lane_hint",
    "normalize_ref",
]

CAUSE_CARRIER_MISSING = "landing_carrier_missing"
CAUSE_STATE_UNKNOWN = "landing_state_unknown"
# The CARRIER VERDICT causes (card t_1e8fe1f9, design t_1746ba2e). They are legal
# ``CompletionRefusal`` causes because this module REGISTERS them into the kernel's cause
# tuple at import time (:func:`_register_completion_causes`) -- the kernel's own
# ``_gate_landing_carrier`` imports this module and THEN builds the refusal from
# ``verdict.cause``, so the registration lands before the lookup and NO kernel edit is
# needed (``hermes_cli/kanban_db.py`` stays byte-identical; ruling R3, ONE landing surface).
CAUSE_VERDICT_MISSING = "landing_verdict_missing"
CAUSE_VERDICT_MISMATCH = "landing_verdict_mismatch"
CAUSE_VERDICT_UNSUBSTANTIATED = "landing_verdict_unsubstantiated"
VERDICT_CAUSES = (
    CAUSE_VERDICT_MISSING,
    CAUSE_VERDICT_MISMATCH,
    CAUSE_VERDICT_UNSUBSTANTIATED,
)

#: The ONE key a carrier close stamps. Named, not inferred: a key an audit can read.
LANDING_VERDICT_KEY = "landing_verdict"

# The two RECORDED escapes. ``landing_deferred`` says "owed, not yet carried" and
# only counts when it NAMES the carrier card (see ``is_named_carrier_deferral``);
# ``landing_not_owed`` says "this PR is not the landing" and must name the
# replacement.
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
# the verdict contract: WHICH cards owe one (card t_1e8fe1f9, design t_1746ba2e)
# --------------------------------------------------------------------------- #

#: The title shapes that make a card ITSELF a carrier -- the verdict requirement's whole
#: scope. Deterministic and TITLE-ONLY: body prose is never read here, because prose
#: matching measured 16 false positives in 18 hits (see the module docstring).
#:
#: A LEADING BRACKET TAG DECIDES, whatever follows it (ruling 2, review 2026-10-18):
#:   * ``[land]`` / ``[land + deploy]`` / ``[land+deploy]``  -> carrier (``land`` prefix)
#:   * ``[owner act] ...``                                   -> carrier
#:   * ``[reconcile]`` / ``[fix]`` / ``[deploy]`` / ...       -> NOT a carrier, whatever
#:     follows -- so a card whose SUBJECT is landing carriers (``[reconcile] ... files
#:     TWIN carriers: ... a landing carrier whose title spells ...``, card t_24f43788)
#:     is not itself judged a landing.
#: Without a leading tag, two shapes remain, both ANCHORED at the title's start:
#:   * ``Land yaan-platform#1484 ...``                     -> ``^\s*land\b``; spelled
#:     case-insensitively so the running-text form and the doctrine's capitalised form
#:     are ONE shape rather than two
#:   * ``Owner-lane landing step for approved tip ...``    -> ``^\s*(?:owner[- ]lane\s+)?
#:     landing (?:step|carrier)\b`` (card ``t_f749d28e``). ANCHORED, not ``\blanding
#:     (?:step|carrier)\b``: the unanchored breadth protected no real card while it
#:     already mis-scoped t_24f43788, whose prose says "a landing carrier".
_LEADING_TAG = re.compile(r"^\s*\[\s*([^\]]*?)\s*\]")
_CARRYING_TAG = re.compile(r"^(?:land|owner act)\b", re.IGNORECASE)

CARRIER_TITLE_PATTERNS = (
    re.compile(r"^\s*land\b", re.IGNORECASE),
    re.compile(r"^\s*(?:owner[- ]lane\s+)?landing (?:step|carrier)\b", re.IGNORECASE),
)


def _leading_tag(title: str) -> Optional[str]:
    """The card title's leading ``[...]`` tag, or ``None`` when the title has none."""
    match = _LEADING_TAG.match(title)
    return match[1] if match else None


def is_carrier_title(title: Any) -> bool:
    """True when a card's TITLE makes it a carrier -- the verdict requirement's scope.

    Title only, by design: the shapes above are the doctrine's own words for a carrier
    card, and a body scan is the prose trigger the module's docstring rules out. A
    leading bracket tag is DECISIVE in both directions: ``[land]`` / ``[owner act]`` make
    a carrier, any other tag makes a non-carrier even if the rest of the title says
    "landing step". Only an untagged title reaches the anchored running-text shapes.
    """
    text = title if isinstance(title, str) else ""
    tag = _leading_tag(text)
    if tag is not None:
        return bool(_CARRYING_TAG.match(tag))
    return any(pattern.search(text) for pattern in CARRIER_TITLE_PATTERNS)


def _refs_in_title(title: Any) -> list[str]:
    """``owner/repo#N`` refs named in a title -- FACTS, never a gate.

    A title is not a declaration (declarations are metadata, above), so this decides
    nothing. It is carried into the verdict's facts so a refused carrier close shows the
    reader what the card itself says it lands.
    """
    text = title if isinstance(title, str) else ""
    out: list[str] = []
    for match in _PR_SHORT.finditer(text):
        ref = f"{match[1]}#{int(match[2])}"
        if ref not in out:
            out.append(ref)
    return out


#: Module-visible registration state: True once this module's three causes are legal
#: ``CompletionRefusal`` causes, False on the ONE path where there is no kernel to
#: register against. A reader (or a test) can tell "registered" from "silently skipped"
#: without reconstructing the import graph.
CAUSES_REGISTERED = False


def _register_completion_causes() -> None:
    """Make the verdict causes legal ``CompletionRefusal`` causes WITHOUT a kernel edit.

    ``hermes_cli/kanban_db.py::CompletionRefusal`` validates its cause against
    ``COMPLETION_REFUSAL_CAUSES`` at CONSTRUCTION time, and its ``_gate_landing_carrier``
    imports THIS module before it builds the refusal from ``verdict.cause`` -- so
    extending that tuple from here is enough and the kernel file stays byte-identical
    (design items 1 and 5: one file, no kernel edit). Idempotent.

    The catch is NARROW on purpose (review ruling 1a): a bare ``except Exception`` would
    also hide the one case that matters -- a kernel whose tuple cannot be widened, where
    a carrier close would CRASH inside a gate that promises never to raise, instead of
    being refused with a cause. Only ``ImportError`` (no kernel to register against, a
    kernel that will fail closed on its own import) is swallowed, and it leaves
    ``CAUSES_REGISTERED`` False; anything else propagates loudly. The post-condition is
    verified rather than assumed, so a widening that silently did not take is reported.
    """
    global CAUSES_REGISTERED
    CAUSES_REGISTERED = False
    try:
        from hermes_cli import kanban_db as kernel
    except ImportError:
        return

    causes = tuple(getattr(kernel, "COMPLETION_REFUSAL_CAUSES", ()) or ())
    missing = tuple(cause for cause in VERDICT_CAUSES if cause not in causes)
    if missing:
        kernel.COMPLETION_REFUSAL_CAUSES = causes + missing
    registered = tuple(getattr(kernel, "COMPLETION_REFUSAL_CAUSES", ()) or ())
    if not all(cause in registered for cause in VERDICT_CAUSES):
        raise RuntimeError(
            "kanban_landing_carrier: could not register the verdict causes "
            f"{VERDICT_CAUSES!r} into the kernel's COMPLETION_REFUSAL_CAUSES; a carrier "
            "close would raise 'unknown completion refusal cause' instead of refusing"
        )
    CAUSES_REGISTERED = True


_register_completion_causes()


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


def is_named_carrier_deferral(reason: Any) -> bool:
    """True when a deferral NAMES the card that carries the landing.

    Tightened 2026-10-28 by operator order. The old predicate was a length floor
    (``>= MIN_REASON``), which made "the reconciler has not filed the carrier yet"
    a valid reason to close a card whose PR was unmerged and whose defect was still
    live. A deferral is not evidence; the carrier card is. So the reason must name
    a ``t_...`` id, and ``evaluate`` then demands that card exist and name the same
    PR (``_find_carrier``) before the close is admitted.
    """
    if not isinstance(reason, str):
        return False
    text = reason.strip()
    if len(text) < MIN_REASON:
        return False
    return bool(_CARD_ID.search(text))


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


# "Does a carrier exist for this ref" -- the CARRIER EXISTENCE READ, and the only
# question this module asks of a card. ARCHIVED IS INCLUDED on purpose (card
# ``t_4a87bb75``): a carrier that has served its landing is archived by the board
# sweep, and a tombstone whose key still names the PR is a RECORD that the landing
# was carried, not an absence.
#
# THIS IS A POLICY REVERSAL, NOT AN ALIGNMENT -- measured at the base commit
# ``161899e0c8``: ALL THREE existence reads carried ``status != 'archived'``
# (``_CARRIER_TITLE_FILTER``, ``_card_by_id``, ``_child_cards``) and
# ``_candidate_cards`` used that same filter, so an archived carrier was refused
# EVERYWHERE, uniformly. The base never answered this question two ways; it answered
# it consistently on the wrong premise ("archived" is not evidence that a landing was
# NOT carried). This change admits archived receivers on all three paths on purpose.
#
# BLAST RADIUS, in one sentence: every archived ``[land ...]`` / ``[owner act]``
# receiver on every board now satisfies this fence, and what still stops a false DONE
# is the merge/stamp legs and the deploy-ledger admission -- NOT this read.
_CARRIER_TITLE_FILTER = "(title LIKE '[land%' OR title LIKE '[owner act%')"


def _candidate_cards(stores: Optional[list[tuple[Path, str]]] = None) -> list[_Card]:
    """Cards on any board that could BE a carrier, by the doctrine's title shape."""
    out: list[_Card] = []
    for path, board in stores if stores is not None else _board_stores():
        for row in _rows(path, f"SELECT id, title, body FROM tasks WHERE {_CARRIER_TITLE_FILTER}"):
            out.append(_Card(row["id"], row["title"] or "", row["body"] or "", board))
    return out


def _card_by_id(task_id: str, stores: Optional[list[tuple[Path, str]]] = None) -> Optional[_Card]:
    """The card with this id on any board -- ARCHIVED INCLUDED (an existence read).

    The status filter came off with ``_CARRIER_TITLE_FILTER``'s (card
    ``t_4a87bb75``): a caller here is asking whether a carrier exists for a ref, and
    naming an archived carrier is a DECLARATION that the landing was carried. The
    live-card filter stays where a caller will ACT on the card -- see
    ``kanban_db.create_task``'s idempotency dedupe, which must not return an archived
    row to a caller about to work it.
    """
    for path, board in stores if stores is not None else _board_stores():
        rows = _rows(
            path,
            "SELECT id, title, body FROM tasks WHERE id = ?",
            (task_id,),
        )
        if rows:
            row = rows[0]
            return _Card(row["id"], row["title"] or "", row["body"] or "", board)
    return None


def _child_cards(conn: sqlite3.Connection, task_id: str) -> list[_Card]:
    """This card's ``task_links`` children (the doctrine chain's second edge).

    Links are intra-board by construction (``task_links`` is per-store), so the
    children are read off the closing card's own connection. ARCHIVED INCLUDED, for
    the same reason as ``_card_by_id``: a child that named the PR and has since been
    archived still records that the landing was carried.
    """
    try:
        rows = conn.execute(
            "SELECT t.id, t.title, t.body FROM task_links l JOIN tasks t ON t.id = l.child_id "
            "WHERE l.parent_id = ?",
            (task_id,),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [
        _Card(row["id"], row["title"] or "", row["body"] or "", "this board")
        for row in rows
    ]


# --------------------------------------------------------------------------- #
# the carrier IDENTITY: one key per landing
# --------------------------------------------------------------------------- #

_CARRIER_KEY_PREFIX = "landing-carrier:"
_CARRIER_KEY_LEGACY_PREFIX = "landing-reconcile:"


def _carrier_key(ref: str) -> str:
    """The ONE identity a carrier card carries: ``landing-carrier:<owner>/<repo>#<N>``.

    Owner-qualified on purpose. The flow's own key shape
    (:func:`_carrier_key_legacy`) is owner-less, and this estate holds both
    ``yoyodine-industries/hermes-agent#101`` and ``NousResearch/hermes-agent#101`` --
    an owner-less key cannot tell them apart, so it can never be the identity. No
    sha, no timestamp, no run id: the key names the PR's LANDING, which is one act,
    so a second close about the same PR FINDS the same carrier instead of filing
    another one (measured: 521 of 801 carrier receipts keyless, 32 of the 33 born in
    the last 24h -- ruling on card ``t_2bc058fc``).
    """
    return f"{_CARRIER_KEY_PREFIX}{ref}"


def _carrier_key_legacy(ref: str) -> str:
    """The older, owner-less alias the landing flow has filed: ``landing-reconcile:<repo>#<N>``.

    Read-only, and never sufficient on its own: it is owner-less, so a hit is
    accepted only when the card it found ALSO names the full ``owner/repo#N``
    (:func:`_find_carrier`). No migration -- the keyed rows the flow already wrote
    keep satisfying the fence exactly as they do today, and every NEW carrier
    carries :func:`_carrier_key`.
    """
    repo, _, number = ref.partition("#")
    owner, _, name = repo.partition("/")
    return f"{_CARRIER_KEY_LEGACY_PREFIX}{(name or owner)}#{number}"


def _cards_by_key(key: str, stores: Optional[list[tuple[Path, str]]] = None) -> Optional[_Card]:
    """The card holding ``key`` on any board store -- ARCHIVED INCLUDED.

    Identity resolves across EVERY store the same way the prose scan already does
    ("any board"), and the newest row wins per store -- ``create_task``'s own dedupe
    orders that way, so a re-filed duplicate can never hide the keyed row that
    actually landed. A store whose schema predates ``idempotency_key`` yields no
    rows instead of raising: ``_rows`` swallows ``sqlite3.Error``, and this clause
    never raises.
    """
    for path, board in stores if stores is not None else _board_stores():
        rows = _rows(
            path,
            "SELECT id, title, body FROM tasks WHERE idempotency_key = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (key,),
        )
        if rows:
            row = rows[0]
            return _Card(row["id"], row["title"] or "", row["body"] or "", board)
    return None


def _find_carrier(
    ref: str,
    *,
    conn: sqlite3.Connection,
    task_id: str,
    landing_cards: Iterable[str],
    candidates: Optional[list[_Card]] = None,
    stores: Optional[list[tuple[Path, str]]] = None,
) -> Optional[str]:
    """A carrier for ``ref``, described, or None. Never raises.

    Resolution ORDER (card ``t_4a87bb75``): the canonical key, then the flow's legacy
    alias, then the three prose paths UNCHANGED. Identity before prose because a key
    is a DECLARATION -- the card states which landing it carries -- while a prose
    match is an inference over text. The prose paths stay as the legacy fallback so
    the measured keyless carriers keep satisfying the fence; nothing here deletes
    them.
    """
    key = _carrier_key(ref)
    card = _cards_by_key(key, stores)
    if card is not None:
        return (
            f"the keyed carrier card {card.task_id} on board {card.board} "
            f"(idempotency_key {key})"
        )
    legacy = _carrier_key_legacy(ref)
    card = _cards_by_key(legacy, stores)
    if card is not None and (_names_ref(card.title, ref) or _names_ref(card.body, ref)):
        return (
            f"the legacy-keyed carrier card {card.task_id} on board {card.board} "
            f"(idempotency_key {legacy}, and the card names {ref})"
        )
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


def _forge_pull(repo: str, number: str, *, profile_home: Optional[str] = None) -> dict:
    """The forge's own PR object for one PR. Raises :class:`ForgeUnreachable`.

    ONE transport: :func:`forge_state` and :func:`forge_merge_sha` are two READINGS of
    this single call, so the carrier verdict's agreement check costs the close the same
    one forge read the declaration path already pays, and nothing about ``gh`` is
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
    return pr


def _pr_state(pr: dict) -> str:
    """``\"merged\"`` / ``\"open\"`` / ``\"closed\"`` from a PR object. Raises ForgeUnreachable."""
    state = str(pr.get("state") or "").lower()
    if state == "merged" or pr.get("merged") is True or pr.get("merged_at"):
        return "merged"
    if state == "open":
        return "open"
    if state == "closed":
        return "closed"
    raise ForgeUnreachable(f"unknown forge state {state!r}")


def forge_state(repo: str, number: str, *, profile_home: Optional[str] = None) -> str:
    """``"merged"`` / ``"open"`` / ``"closed"`` for one PR. Raises ForgeUnreachable.

    Reuses the PR-acceptance module's ``gh`` path verbatim: the same
    ``gh api`` call, the same per-profile identity resolution (``#122689`` -- an
    ambient login cannot read another org's private repos), the same
    credential-free failure reporting. Nothing about the forge transport is
    reinvented here.
    """
    return _pr_state(_forge_pull(repo, number, profile_home=profile_home))


def forge_merge_sha(
    repo: str, number: str, *, profile_home: Optional[str] = None
) -> Optional[str]:
    """The MERGE COMMIT of a merged PR, or ``None`` when the PR is not merged.

    Raises :class:`ForgeUnreachable` on the same channel as :func:`forge_state`. This is
    the clause's OWN resolution of "landed": a stamped verdict's ``merge_sha`` is admitted
    only when it IS this commit (design t_1746ba2e item 2), so a worker cannot record a
    landed verdict for a commit the forge never merged.
    """
    pr = _forge_pull(repo, number, profile_home=profile_home)
    if _pr_state(pr) != "merged":
        return None
    sha = pr.get("merge_commit_sha")
    return sha.strip() if isinstance(sha, str) and sha.strip() else None


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


def _ledger_pin_names(sha: Any) -> bool:
    """Does an ADMITTED deploy-ledger row carry this commit as its ``upstream_pin``?

    The other column of the same rows :func:`ledger_resolves` already reads: the reader
    and the row predicates are the deploy-proof gate's own (one ledger, one parser), and
    ``upstream_pin`` is the pin the train records for the commit it installed. An
    unreadable ledger resolves nothing -- the forge leg still decides.
    """
    sha_text = str(sha or "").strip().lower()
    if len(sha_text) < 7:
        return False
    try:
        from hermes_cli import kanban_proof_gate as pg

        for row in pg._ledger_rows(pg._ledger_path(None)):
            if not pg._is_admitted(row):
                continue
            if pg._ref_names(sha_text, row.get("upstream_pin")):
                return True
    except Exception:
        return False
    return False


def _merge_sha_declared(mds: Iterable[dict]) -> Optional[str]:
    """The landed revision a run declares -- merge OR deploy stamp.

    ``landed_sha`` / ``deploy_stamp`` are the dest-side names (the landing
    reconciler and the deploy train write them); ``merged_as`` / ``merge_sha`` the
    review-side ones. All four resolve the same way, through ``ledger_resolves``.
    """
    for md in mds:
        for key in ("merged_as", "merge_sha", "landed_sha", "deploy_stamp"):
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


def _carrier_create_spec(ref: str, task_id: str) -> str:
    """The paste-ready create for ``ref``'s carrier -- the KEY is inside the spec.

    The remedy is the one instruction the whole fleet copies, so the identity has to
    live in the call itself rather than in a sentence beside it: a lane that retypes
    this text must not be able to drop the key. Measured before card ``t_4a87bb75``:
    521 of 801 carrier receipts keyless, 32 of the 33 born in the last 24h, every one
    of them compliant with the spec this function printed.
    """
    return (
        f"kanban_create(title=\"{_carrier_title(ref, task_id)}\", "
        f"assignee=\"{landing_lane_hint(ref.partition('#')[0])}\", "
        f"parents=[\"{task_id}\"], idempotency_key=\"{_carrier_key(ref)}\")"
    )


# The remedy's blocks are SEPARATE STRINGS on purpose (hotspot note on card
# ``t_4a87bb75``): leg C2 (``t_c83a87dd``) amends the ESCAPE paragraph to name its
# one-step `--mint-carrier` route, and that has to be a text swap here rather than a
# second rewrite of ``_render_remedy``. One string per remedy line, no inline
# conditionals woven through them.
_CARRIER_ESCAPE_HEADER = "Escapes (recorded in this card's metadata):"
_CARRIER_ESCAPE_LINES = (
    "  * name the landing carrier: metadata.landing_card=\"t_...\" (or a linked child card) — "
    "any board's card keyed landing-carrier:<owner>/<repo>#<N> (the spec above writes it), "
    "AND ARCHIVED IS ENOUGH: a served carrier is usually archived, and its key still records "
    "the landing. Keyless carriers still pass by prose: the alias "
    "landing-reconcile:<repo>#<N> naming the full ref, or ANY \"[land ...\" / "
    "\"[owner act]\" card naming it (archived included — every existence read is "
    "status-agnostic). If the landing is owed but not yet carried, THIS is the "
    "escape: file that carrier with the key and name it;",
    "  * or if the PR is superseded or re-cut, say what it rides: "
    "metadata.landing_not_owed=\"<the replacement it rides>\" (repo#N, a card id or a URL).",
)
_CARRIER_NO_DEFERRAL_LINE = (
    "A deferral is NOT a third escape: metadata.landing_deferred must name a t_... carrier "
    "card that names the PR — a bare reason cannot close an unmerged-PR card (2026-10-28). "
    "A close rides EVIDENCE: the forge's MERGED state, or a landed revision "
    "(merged_as / merge_sha / landed_sha / deploy_stamp) matching a deploy-ledger row."
)
_CARRIER_DOCTRINE_LINE = (
    "Doctrine: a close whose declared PR is unmerged must name its landing carrier on EVERY "
    "transition, worker closes included (t_9333ca24; t_88f6cb12; widened t_502a28bd; identity "
    "keyed by t_4a87bb75). `--force` does not bypass a declaration fence."
)


def _render_remedy(task_id: str, entries: list[tuple[str, str]]) -> str:
    """The refusal, naming verbatim what the close owes. One remedy per PR."""
    lines: list[str] = []
    for ref, state in entries:
        lines.append(
            f"  * {ref} — the forge reports it {state}, and no landing carrier names it. "
            f"Create one: {_carrier_create_spec(ref, task_id)}"
        )
    return (
        f"{task_id} closes declaring {len(entries)} deliverable PR(s) with no landing leg, so the "
        f"published work would be stranded with the board green:\n"
        + "\n".join(lines)
        + f"\n{_CARRIER_ESCAPE_HEADER}\n"
        + "\n".join(_CARRIER_ESCAPE_LINES)
        + f"\n{_CARRIER_NO_DEFERRAL_LINE}\n"
        + _CARRIER_DOCTRINE_LINE
    )


def _carrier_title(ref: str, task_id: str) -> str:
    return f"[land] {ref} for {task_id}"


# --------------------------------------------------------------------------- #
# the carrier verdict (card t_1e8fe1f9, design t_1746ba2e; amendment 2026-10-29)
# --------------------------------------------------------------------------- #

#: A commit id, or the unique-enough prefix the fleet actually records. The accepted
#: shape names ``<40hex>``; a >=7-char prefix is admitted and compared as a prefix,
#: because the tokens landed runs record are 7-8 hex (``merge_sha`` x12 / ``merge_commit``
#: x4 measured over one day) and refusing those would refuse the very closes this
#: contract exists to make auditable.
_SHA_TEXT = re.compile(r"^[0-9a-f]{7,40}$")


def _normalize_sha(value: Any) -> Optional[str]:
    """A lowercase 7-40 hex commit id, or None when the value is not one."""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    return text if _SHA_TEXT.match(text) else None


def _sha_agrees(given: Any, actual: Any) -> bool:
    """Does the verdict's commit id name the SAME commit the clause resolved?"""
    left, right = _normalize_sha(given), _normalize_sha(actual)
    if not left or not right:
        return False
    return left == right or right.startswith(left) or left.startswith(right)


def _latest_verdict(mds: Iterable[dict]) -> Any:
    """The LAST verdict stamped on the card: the closing call's own metadata wins.

    Read newest-last like every other declaration, but a retried card must be judged by
    the stamp it is closing WITH, never by a stale one left by an earlier attempt.
    """
    latest: Any = None
    for md in mds:
        if isinstance(md, dict) and LANDING_VERDICT_KEY in md:
            latest = md.get(LANDING_VERDICT_KEY)
    return latest


def _render_verdict_shapes() -> str:
    """All THREE accepted shapes, verbatim, so a refused close is always satisfiable."""
    return (
        "The verdict is metadata on the CLOSE -- the same kanban_complete call that closes the card:\n"
        '  kanban_complete(metadata={"landing_verdict": {"state": "landed", '
        '"ref": "yoyodine-industries/yaan-platform#1497", "merge_sha": "<40hex>"}})\n'
        '  kanban_complete(metadata={"landing_verdict": {"state": "not_owed", '
        '"ref": "<owner/repo#N or the superseded card id>", '
        '"reason": "<text naming the replacement, at least 20 characters>"}})\n'
        '  kanban_complete(metadata={"landing_verdict": {"state": "deferred", '
        '"ref": "<owner/repo#N>", "carrier": "t_..."}})\n'
        f"``deferred`` is admitted only when that carrier card EXISTS and itself names the "
        f"same ``owner/repo#N`` -- the same verification a prose ``{LANDING_VERDICT_KEY}``-free "
        f"``landing_deferred`` already gets. A bare prose ``landing_deferred`` is NOT a "
        f"verdict: since 2026-10-28 a deferral must point at a card.\n"
    )


def _render_verdict_missing(task_id: str, title: str, state: str) -> str:
    if state:
        why = (
            f"the verdict on this close says state={state!r}, which is NOT a terminal verdict "
            f"for a carrier -- a carrier that has not landed says ``not_owed`` and names what "
            f"rides it, and a carrier whose landing has MOVED says ``deferred`` and names the "
            f"card that carries it (the bare reason was never enough: 2026-10-28)"
        )
    else:
        why = "no ``metadata[\"landing_verdict\"]`` was recorded on this close"
    return (
        f"{task_id}: this card is a CARRIER -- its title {title!r} matches the doctrine's carrier "
        f"shape -- and a carrier close must STAMP the landing verdict, but {why}.\n"
        + _render_verdict_shapes()
    )


def _render_verdict_mismatch(task_id: str, ref: str, given: str, resolution: dict) -> str:
    return (
        f"{task_id}: the close stamped state=landed for {ref} at {given!r}, but this clause's OWN "
        f"resolution of that landing does NOT agree: the forge reports {resolution.get('forge')!r} "
        f"with merge commit {resolution.get('forge_merge_commit_sha')!r}, and no admitted "
        f"deploy-ledger row carries that commit as its ``upstream_pin``. A landed verdict is "
        f"admitted only when the landing ACTUALLY happened at the commit it names -- stamping a "
        f"verdict the clause cannot corroborate is exactly the prose this contract replaces.\n"
        + _render_verdict_shapes()
    )


def _render_verdict_unsubstantiated(task_id: str, state: str, why: str) -> str:
    return (
        f"{task_id}: the close stamped state={state!r}, but the exemption is not VERIFIED: {why} "
        f"A bodyless, one-word or unpointed exemption is the silent door this clause exists to "
        f"close -- whatever its state, it must name the thing the landing now rides.\n"
        + _render_verdict_shapes()
    )


def _render_verdict_unknown(task_id: str, ref: str, merge_sha: str, error: str) -> str:
    return (
        f"{task_id}: the close stamped state=landed for {ref} at {merge_sha!r}, but the landing "
        f"state could not be read from the forge ({error}) and no admitted deploy-ledger row "
        f"corroborates that commit, so the verdict cannot be corroborated and the close is "
        f"refused rather than admitted blind. Nothing has been changed on the card: retry the "
        f"close when the forge is reachable, or -- if the landing is genuinely not owed -- close "
        f"with the ``not_owed`` shape below.\n"
        + _render_verdict_shapes()
    )


def _default_pr_facts(assignee: Optional[str]) -> Callable[[str, str], dict]:
    """The production forge READ for one carrier verdict (state + merge commit).

    Same identity resolution and the same unreachable channel as :func:`_default_forge`:
    an unresolvable assignee profile is reported UNREACHABLE rather than answered with
    the ambient login, which is the identity leak ``#122689`` closed.
    """
    failure: Optional[str] = None
    profile_home: Optional[str] = None
    try:
        profile_home = _profile_home(assignee)
    except Exception as exc:
        failure = f"the assignee profile {assignee!r} cannot be resolved ({type(exc).__name__})"

    def _call(repo: str, number: str) -> dict:
        if failure:
            raise ForgeUnreachable(failure)
        return _forge_pull(repo, number, profile_home=profile_home)

    return _call


def _named_carrier_verified(
    carrier_id: str, ref: str, stores: Optional[list[tuple[Path, str]]] = None
) -> bool:
    """Does the card NAMED by a ``deferred`` verdict exist and carry ``ref``?

    Exactly the verification live already applies to a prose deferral: ``evaluate`` feeds
    the ids a deferral names into ``_find_carrier``'s ``landing_cards`` leg, which admits a
    card only when it EXISTS (archived included -- an existence read) and its title or body
    names the same ``owner/repo#N``. Scoped to the NAMED id on purpose: a verdict that
    points at ``t_a`` must not be admitted because some OTHER card carries the landing.
    """
    card = _card_by_id(carrier_id, stores)
    if card is None:
        return False
    return _names_ref(card.title, ref) or _names_ref(card.body, ref)


def _evaluate_carrier(
    *,
    task_id: str,
    title: str,
    status: Optional[str],
    contract: Optional[str],
    assignee: Optional[str],
    mds: list[dict],
    pr_facts: Optional[Callable[[str, str], dict]] = None,
    stores: Optional[list[tuple[Path, str]]] = None,
) -> Verdict:
    """The verdict requirement for a CARRIER close. Never raises, never writes.

    A carrier is the card that OWNS a landing, so its close is the one place the landing
    fact is known for certain -- and the one place it must be STAMPED, or an audit is
    left with prose. The requirement is scoped by :func:`is_carrier_title` alone, so
    every non-carrier card never reaches this function.
    """
    facts: dict = {
        "carrier_scope": True,
        "carrier_title": title,
        "status": status,
        "contract": contract,
        "title_refs": _refs_in_title(title),
        "verdict": None,
        "prs": [],
    }
    raw = _latest_verdict(mds)
    if isinstance(raw, dict):
        facts["verdict"] = {
            key: raw[key] for key in ("state", "ref", "merge_sha", "reason", "carrier")
            if key in raw
        }
    if not isinstance(raw, dict):
        return Verdict(
            refused=True,
            cause=CAUSE_VERDICT_MISSING,
            detail=_render_verdict_missing(task_id, title, ""),
            facts=facts,
        )

    state = str(raw.get("state") or "").strip().lower()
    facts["verdict_state"] = state
    if state == "landed":
        return _evaluate_landed(
            task_id=task_id, raw=raw, assignee=assignee,
            pr_facts=pr_facts, facts=facts,
        )
    if state == "not_owed":
        reason = raw.get("reason")
        ref_text = raw.get("ref")
        named_ref = isinstance(ref_text, str) and bool(ref_text.strip())
        if not named_ref or not is_named_exemption(reason):
            why = (
                f"reason={reason!r}, ref={ref_text!r}. ``not_owed`` is admitted only when the "
                f"reason NAMES what rides the landing -- a ``repo#N`` ref, a card id or a URL -- "
                f"as well as being at least {MIN_REASON} characters, and the ``ref`` names the "
                f"replacement (or the superseded card)."
            )
            return Verdict(
                refused=True,
                cause=CAUSE_VERDICT_UNSUBSTANTIATED,
                detail=_render_verdict_unsubstantiated(task_id, state, why),
                facts=facts,
            )
        facts["outcome"] = "not_owed"
        facts["outcome_ref"] = str(ref_text).strip()
        facts["outcome_reason"] = str(reason).strip()
        return Verdict(refused=False, facts=facts)

    if state == "deferred":
        # THE THIRD TERMINAL STATE (design amendment 2026-10-29). The landing has MOVED to
        # a card, and the CARD -- not the sentence -- is the evidence. Admitted only on the
        # same existence + names-the-ref verification a prose deferral gets.
        ref = normalize_ref(raw.get("ref"))
        carrier_id = raw.get("carrier")
        carrier_text = carrier_id.strip() if isinstance(carrier_id, str) else ""
        named = bool(carrier_text) and bool(_CARD_ID.fullmatch(carrier_text))
        verified = bool(named and ref and _named_carrier_verified(carrier_text, ref, stores))
        if verified:
            facts["outcome"] = "deferred"
            facts["outcome_ref"] = ref
            facts["outcome_carrier"] = carrier_text
            return Verdict(refused=False, facts=facts)
        if not named:
            why = (
                f"carrier={carrier_id!r} names no ``t_...`` card. A ``deferred`` verdict is a "
                f"deferral to a CARD -- the card id is the whole evidence, and a bare reason "
                f"cannot close a carrier (the 2026-10-28 rule)."
            )
        elif ref is None:
            why = (
                f"ref={raw.get('ref')!r} names no ``owner/repo#N``, so the carrier card "
                f"{carrier_text} cannot be checked against the landing it is supposed to carry."
            )
        else:
            why = (
                f"the named card {carrier_text} does not carry {ref}: it is not on any board of "
                f"this install, or it does not name {ref} in its title or body. The card IS the "
                f"evidence for a deferral, so it has to exist and name the landing it took over."
            )
        return Verdict(
            refused=True,
            cause=CAUSE_VERDICT_UNSUBSTANTIATED,
            detail=_render_verdict_unsubstantiated(task_id, state, why),
            facts=facts,
        )

    # Anything else -- a dict with no state, a mistyped state, the retired prose shape.
    return Verdict(
        refused=True,
        cause=CAUSE_VERDICT_MISSING,
        detail=_render_verdict_missing(task_id, title, state),
        facts=facts,
    )


def _evaluate_landed(
    *,
    task_id: str,
    raw: dict,
    assignee: Optional[str],
    pr_facts: Optional[Callable[[str, str], dict]],
    facts: dict,
) -> Verdict:
    """``state=landed`` admitted only when the clause's OWN resolution agrees."""
    ref = normalize_ref(raw.get("ref"))
    given = _normalize_sha(raw.get("merge_sha"))
    resolution: dict = {
        "forge": None,
        "forge_merge_commit_sha": None,
        "forge_error": None,
        "ledger_upstream_pin": False,
    }
    facts["resolution"] = resolution
    if ref is None or given is None:
        # A shape that cannot resolve to a PR + commit cannot be corroborated at all.
        facts["outcome"] = "unresolvable_shape"
        return Verdict(
            refused=True,
            cause=CAUSE_VERDICT_MISMATCH,
            detail=(
                f"{task_id}: the close stamped state=landed, but the verdict is not a resolvable "
                f"landing: ref={raw.get('ref')!r} names no owner/repo#N, and "
                f"merge_sha={raw.get('merge_sha')!r} is not a 7-40 hex commit id, so there is "
                f"nothing the clause can corroborate. A landed verdict names the PR it landed AND "
                f"the merge commit.\n" + _render_verdict_shapes()
            ),
            facts=facts,
        )

    repo, _, number = ref.partition("#")
    forge_state_value: Optional[str] = None
    forged_sha: Optional[str] = None
    forge_error: Optional[str] = None
    try:
        data = (pr_facts or _default_pr_facts(assignee))(repo, number)
        if not isinstance(data, dict):
            raise ForgeUnreachable("malformed forge response")
        forge_state_value = _pr_state(data)
        raw_sha = data.get("merge_commit_sha")
        forged_sha = raw_sha.strip() if isinstance(raw_sha, str) and raw_sha.strip() else None
    except ForgeUnreachable as exc:
        forge_error = str(exc)
    except Exception as exc:  # the clause never raises: any transport fault is unreachable
        forge_error = type(exc).__name__
    resolution["forge"] = forge_state_value
    resolution["forge_merge_commit_sha"] = forged_sha
    resolution["forge_error"] = forge_error

    agreed = forge_state_value == "merged" and _sha_agrees(given, forged_sha)
    ledger = False if agreed else _ledger_pin_names(given)
    resolution["ledger_upstream_pin"] = ledger
    facts["prs"].append(
        {"ref": ref, "state": "landed", "merge_sha": given,
         "agreed_by": "forge" if agreed else ("deploy-ledger upstream_pin" if ledger else None)}
    )
    if agreed or ledger:
        facts["outcome"] = "landed"
        return Verdict(refused=False, facts=facts)
    if forge_error is not None:
        facts["outcome"] = "unverifiable"
        return Verdict(
            refused=True,
            cause=CAUSE_STATE_UNKNOWN,
            detail=_render_verdict_unknown(task_id, ref, given, forge_error),
            facts=facts,
        )
    facts["outcome"] = "mismatch"
    return Verdict(
        refused=True,
        cause=CAUSE_VERDICT_MISMATCH,
        detail=_render_verdict_mismatch(task_id, ref, given, resolution),
        facts=facts,
    )


def evaluate(
    *,
    conn: sqlite3.Connection,
    task_id: str,
    metadata: Optional[dict] = None,
    status: Optional[str] = None,
    contract: Optional[str] = None,
    assignee: Optional[str] = None,
    forge: Optional[Callable[[str, str], str]] = None,
    pr_facts: Optional[Callable[[str, str], dict]] = None,
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
    facts that event has to name, so the refusal stays auditable.

    ONE addition, and it is still declaration-shaped: a CARD THAT IS ITSELF A CARRIER
    (:func:`is_carrier_title`, decided by the card's TITLE) that STAMPS a
    ``landing_verdict`` is judged on that stamp BEFORE the deliverable-PR test -- the stamp
    is the strongest record there is, and a carrier that has proved its own landing must
    not be refused for want of a carrier. A carrier that stamps NOTHING takes the
    declaration path UNCHANGED, and is refused ``landing_verdict_missing`` only when that
    path records nothing at all (card ``t_1e8fe1f9``, amended 2026-10-29 to admit a named
    ``deferred`` carrier as the third terminal state).

    The keyword overrides (``status``/``contract``/``assignee``/``forge``/``pr_facts``/
    ``stores``/``candidates``) exist so a test can drive the clause over its REAL door
    without a live forge or the operator's boards.
    """
    row = conn.execute(
        "SELECT status, assignee, completion_contract, title FROM tasks WHERE id = ?",
        (task_id,),
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
    title = row["title"] if "title" in keys else None

    runs = _run_metadata(conn, task_id)
    mds = _all_metadata(metadata, runs)

    # THE CARRIER VERDICT, and its ORDERING (card t_1e8fe1f9; the re-cut's one substantive
    # design point). A carrier that STAMPS a verdict is judged on it FIRST: the stamp is the
    # strongest record the clause can read, and judging the declaration first would refuse a
    # carrier that has already proved its landing at its own close ("no carrier" for a card
    # that IS the carrier). A carrier that stamped NOTHING falls through to the declaration
    # path UNCHANGED -- the populated-record half (``kanban_landing_verdict.py``, card
    # ``t_765136ce``) writes its record as a named declaration and must keep working -- and
    # only when the declaration path finds nothing at all is the record EMPTY, which is the
    # class this card exists to close.
    carrier_title = is_carrier_title(title)
    title_text = title if isinstance(title, str) else ""
    stamped = isinstance(_latest_verdict([md for md in mds if isinstance(md, dict)]), dict)
    if carrier_title and stamped:
        return _evaluate_carrier(
            task_id=task_id,
            title=title_text,
            status=status,
            contract=contract,
            assignee=assignee,
            mds=[md for md in mds if isinstance(md, dict)],
            pr_facts=pr_facts,
            stores=stores,
        )

    tier, refs, keys_seen = declared_prs(metadata=metadata, runs_metadata=runs, contract=contract)
    if not refs:
        if carrier_title:
            # NOTHING declared and no verdict: the empty record, on the card that owns the
            # landing. This is the measured class (237 land-shaped ``done`` cards, empty
            # ``result``, the PR pointer only in prose).
            return _evaluate_carrier(
                task_id=task_id,
                title=title_text,
                status=status,
                contract=contract,
                assignee=assignee,
                mds=[md for md in mds if isinstance(md, dict)],
                pr_facts=pr_facts,
                stores=stores,
            )
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
    deferred = _declared_reason(DEFERRED_KEYS, mds, is_named_carrier_deferral)
    not_owed = _declared_reason(NOT_OWED_KEYS, mds, is_named_exemption)
    merge_sha = _merge_sha_declared(mds)
    landing_cards: list[str] = []
    for md in mds:
        value = md.get("landing_card")
        if isinstance(value, str) and value.strip():
            landing_cards.append(value.strip())
        # A deferral is a POINTER to the carrier, never a waiver: the card it names
        # is verified below exactly like metadata.landing_card (exists, names the PR).
        reason = md.get("landing_deferred")
        if isinstance(reason, str) and is_named_carrier_deferral(reason):
            landing_cards.extend(_CARD_ID.findall(reason))

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
            # A deferral that survived the carrier lookup named a card which does NOT
            # name this PR: recorded as a fact and refused only if the forge also says
            # unmerged -- the forge still decides, a named-but-wrong card does not.
            entry["deferred_named"] = deferred
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
                    f"the card. This remedy needs no network: file the landing carrier card and name "
                    f"it with metadata.landing_card=\"t_...\" (a bare landing_deferred reason no "
                    f"longer closes a card, 2026-10-28); if the PR is superseded or re-cut, record "
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
