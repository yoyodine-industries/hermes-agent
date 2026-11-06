"""THE ALIGNMENT-VEHICLE GATE: a card may not order a git act in a live tree unnamed.

Card t_f79daa7f (design platform-stl, ruled from t_c909ab62) second half.

THE CLASS. A card ordered a git act in the live serving tree - an alignment - and named no
vehicle that is allowed to perform it. The dispatched worker could only discover that after
dispatch, and had to come back for a ruling. The defect is at the CARD's creation, not in
the worker's afternoon: an instruction that no admitted writer can carry out is refused
where it is written.

THE RULE, deterministic - no inference, no model, no prose parsing beyond two fixed
grammars:

  1. the card NAMES a live tree (``HERMES_LIVE_TREE_ROOTS``, or the host defaults:
     ``~/.hermes/hermes-agent``, ``~/.hermes``, ``/opt/hermes_prod``, ``/opt/hermes``);
  2. the card ORDERS a MUTATING git act bound to that tree - either ``git -C <live
     root> <verb>``, or a ``git <verb>`` whose mention sits within a bounded window of the
     root's own mention. Read-only verbs (``status``, ``log``, ``diff``, ``rev-parse``,
     ``hash-object``, ...) are not acts: measuring a live tree is always allowed;
  3. the card NAMES an admitted vehicle - ``r5-install`` or ``hermes-update`` (the update
     chain). The live line is carried by the chain; a hand git op in a live tree is how the
     2026-10-29 bytes got served for four days.

Three ANDed conditions, each a fixed token list, so two people reading the same card reach
the same verdict. A card that fails condition 3 is REFUSED at creation with a line naming
the act, the tree and the two admitted vehicles.

WHY THE DOOR. The refusal travels the SAME channel as the filing-budget refusal
(``kanban_filing_policy.FilingRefusal``): ``create_task`` returns a falsy ``str`` whose
value is the line, the CLI prints it and exits 3, and the tool verb reports it in its error
envelope - no exception, no row, nothing half-written. A host whose cards never name a live
tree never matches: the gate is inert there, and an unreadable environment admits (the same
fail-open rule the filing policy follows - a door that cannot measure is not a door).

WHAT IT IS NOT. It does not judge whether the card is a good idea, does not read the
worker's intent, and does not touch the tree. It only refuses an instruction whose only
possible executor is one the estate does not admit.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from typing import Optional

log = logging.getLogger(__name__)

#: The host's live trees, colon-separated. Unset, the defaults below are used; a host with
#: none of those paths simply never matches a mention and the gate stays inert.
ENV_ROOTS = "HERMES_LIVE_TREE_ROOTS"
DEFAULT_ROOTS = (
    "~/.hermes/hermes-agent",
    "~/.hermes",
    "/opt/hermes_prod",
    "/opt/hermes",
)

#: The ONE admitted vehicle for an alignment of a live tree. Either spelling of the update
#: chain, or the words "update chain" themselves, is a declaration of the vehicle.
ADMITTED_VEHICLES = (
    "r5-install",
    "r5_install",
    "hermes-update",
    "hermes_update",
    "update chain",
    "update-chain",
)

#: Verbs that CHANGE a tree, a ref or the index. Read-only verbs are deliberately absent:
#: a card may always say "run `git status` in the live tree" - that is a measurement, and
#: this card's first half exists to make measurements cheap.
MUTATING_VERBS = frozenset({
    "reset", "checkout", "switch", "restore", "clean", "pull", "merge", "rebase",
    "cherry-pick", "revert", "commit", "push", "apply", "am", "stash", "update-ref",
    "worktree", "submodule", "clone", "init", "gc", "prune", "rm", "mv",
    "filter-branch", "replace",
})

#: ``git [ -C <path> ] [ -c k=v ... ] <verb>``. The verb is what decides.
_GIT_ACT = re.compile(
    r"\bgit\s+(?:-C\s*(?P<cwd>[^\s;&|`'\"]+)\s+)?(?:-[ck]\s+\S+\s+)*(?P<verb>[a-z][a-z-]*)",
    re.IGNORECASE,
)

#: How far from a live-root mention a bare ``git <verb>`` may sit and still be read as an
#: act ON that tree. One sentence's worth: the card says "reset the live tree
#: (~/.hermes/hermes-agent)", not "reset ... 40 lines later".
ROOT_WINDOW = 260


def _expand(path: str) -> str:
    return os.path.expanduser(path.strip()).rstrip("/") or "/"


def live_roots() -> tuple[str, ...]:
    """The host's live trees, expanded and de-duplicated, longest first.

    Longest first matters: ``~/.hermes/hermes-agent`` is INSIDE ``~/.hermes``, and a card
    naming the fork tree must report the specific root, not the home.
    """
    raw = os.environ.get(ENV_ROOTS)
    if raw is None:
        listed = list(DEFAULT_ROOTS)
    else:
        # An EXPLICIT empty value means "this host declares no live tree" - the same
        # fail-open a host without the paths gets from the mention grammar.
        listed = [p for p in re.split(r"[:;]", raw) if p.strip()]
    roots = {_expand(p) for p in listed}
    return tuple(sorted(roots, key=len, reverse=True))


def _mention_forms(root: str) -> tuple[str, ...]:
    """The spellings a card may use for one root: absolute, ``~/...`` and ``$HOME/...``."""
    home = _expand("~")
    forms = {root}
    if root.startswith(home + "/") or root == home:
        rel = root[len(home):].lstrip("/")
        forms.add("~/" + rel if rel else "~")
        forms.add("$HOME/" + rel if rel else "$HOME")
    return tuple(forms)


def _mentions(text: str, roots: tuple[str, ...]) -> list[tuple[int, str]]:
    """Every live-root mention as ``(position, root)``, longest root first at one spot."""
    found: list[tuple[int, str]] = []
    taken: list[tuple[int, int]] = []
    for root in roots:
        for form in _mention_forms(root):
            for match in re.finditer(re.escape(form), text):
                start, end = match.start(), match.end()
                if any(s <= start < e for s, e in taken):
                    continue          # already claimed by a longer root
                taken.append((start, end))
                found.append((start, root))
    return sorted(found)


def _under(path: str, roots: tuple[str, ...]) -> Optional[str]:
    """The live root `path` sits inside, or None."""
    try:
        resolved = _expand(os.path.abspath(os.path.expanduser(path)))
    except (OSError, ValueError):
        return None
    for root in roots:
        if resolved == root or resolved.startswith(root + os.sep):
            return root
    return None


def find_live_act(text: str, roots: tuple[str, ...]) -> Optional[dict]:
    """The first git act bound to a live tree, or None. Deterministic and bounded."""
    if not text:
        return None
    mentions = _mentions(text, roots)
    for match in _GIT_ACT.finditer(text):
        verb = (match.group("verb") or "").lower()
        if verb not in MUTATING_VERBS:
            continue
        cwd = match.group("cwd")
        if cwd:
            # An explicit target settles it: the act is on the tree it names.
            root = _under(cwd, roots)
            if root:
                return {"act": "git %s" % verb, "root": root, "via": "git -C %s" % cwd}
            continue                  # bound to some other tree - not this rule's business
        for start, root in mentions:
            if abs(start - match.start()) <= ROOT_WINDOW:
                return {"act": "git %s" % verb, "root": root, "via": "mention"}
    return None


def names_vehicle(text: str) -> bool:
    lowered = (text or "").lower()
    return any(token in lowered for token in ADMITTED_VEHICLES)


def evaluate(*, title: Optional[str] = None, body: Optional[str] = None,
             roots: Optional[tuple[str, ...]] = None) -> Optional[dict]:
    """The verdict record, or None when the card is admitted (or the host is inert)."""
    roots = roots if roots is not None else live_roots()
    if not roots:
        return None
    text = "%s\n%s" % (title or "", body or "")
    act = find_live_act(text, roots)
    if act is None:
        return None
    if names_vehicle(text):
        return None
    line = (
        "refused: alignment card names no admitted vehicle: %r orders `%s` in the live tree "
        "%s. The live line is carried by the update chain (r5-install / hermes-update) - "
        "name it as the vehicle, or move the act to a non-live tree."
        % ((title or "").strip()[:80], act["act"], act["root"])
    )
    return {"status": "refused", "gate": "alignment_vehicle", "line": line,
            "act": act["act"], "root": act["root"], "via": act["via"],
            "admitted_vehicles": list(ADMITTED_VEHICLES)}


def apply(*, title: Optional[str] = None, body: Optional[str] = None,
          roots: Optional[tuple[str, ...]] = None):
    """Consult the gate at the create door: ``(refusal, record)``.

    ``(None, None)`` is the inert host - nothing matched, nothing printed, nothing recorded.
    ``(refusal, None)`` means the create MUST NOT proceed; the line has been printed here,
    exactly as the filing-budget door does, and travels as the same answering ``str``.
    """
    record = evaluate(title=title, body=body, roots=roots)
    if record is None:
        return None, None
    from hermes_cli.kanban_filing_policy import FilingRefusal

    line = record["line"]
    print(line, file=sys.stderr)
    # INFO, not WARNING: the print IS the contract, and a host with a stderr handler on the
    # root logger would otherwise show a producer the same line twice.
    log.info("%s", line)
    return FilingRefusal(line, record), record
