"""Deterministic post-route: a decomposed child is routed by ROLE, never by tier.

Card t_87e4e2e2; design ruling ``migrations/t_d23cd91f`` §5 (platform-stl).

The defect, measured on the live instance: card ``t_bf0f2c47`` (deliverable = yoyoflow engine
code) was fanned out on 2026-10-15 by ``auto-decomposer`` into five children — ALL FIVE on the
ops seat, three of them pure engine code. ``kanban_decompose._clean_children`` normalises an
absent or unknown LLM assignee to ``kanban.default_assignee``, and ``_load_routing`` resolves
that with ``fallback=root_assignee`` — so routing degenerates to the TIER the card arrived on,
and a code child on the ops seat is a guaranteed hand-back (one dispatched ops run had to hand
a code card back).

This module is that correction, and it is DETERMINISTIC — the LLM is not trusted alone:

* ``is_ops_shaped(title)`` — the child's declared nature (its TITLE) is landing, deploying,
  verifying or ops work. Such a child is never re-routed, even when it names code: the ruling's
  own instance child ("Render elision envelopes in web.py/monitor.py and verify fleet_core
  path") names three code paths and is still the ops seat's. The check reads the title only, so
  a code child that merely *mentions* verifying inside its acceptance criteria is unaffected —
  the ruling's other instance children do exactly that and must stay code-shaped.
* ``code_shaped(child, parent_text=…)`` — the child names a code or test artefact: a path with
  a code/test extension, ``tests/``, a code symbol, or a file the parent's DoD/PR names.
* ``resolve_domain(…)`` — the domain stem, in the ruling's order: the repo the parent names →
  that repo's ``OWNERS`` (first non-comment line), or the upstream-foreign owner REGISTRY when
  the tree can never carry one; else the parent's own ``<stem>-<role>`` assignee; else the
  board's declared lane (board metadata ``owner_lane`` — no board declares one today, measured
  2026-10-16 on all nine boards, so this rung is inert until a board opts in); else unresolved.
* Re-route a code-shaped child OFF the ops seat to ``<stem>-coder`` when the roster holds it.
  Unresolved domain and exactly ONE ``*-coder`` in the roster: use it. Otherwise KEEP the LLM's
  pick and record ``routing_ambiguous`` — never guess, never a silent wrong assignment.

No profile name is hardcoded: the ops seat comes from the caller's resolved
``kanban.orchestrator_profile`` / ``kanban.default_assignee``, the candidates from the ROSTER,
and the domain from data (a repo's OWNERS, the owner registry, the parent's own assignee).

What this module does NOT do: the ``fanout=false`` single-task path
(``kanban_decompose._apply_single``) has no children to route, and the ruling scopes the
post-route to the fan-out between ``_clean_children`` and ``decompose_triage_task``.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Optional

from hermes_cli.kanban_db import _append_event, write_txn

logger = logging.getLogger(__name__)

# --- the fleet's owner sources (the same two `owners.sh` and `gate.py` read) ---------------
_CONTROL_PLANE_ENV = "YAAN_CONTROL_PLANE"
_DEFAULT_CONTROL_PLANE = "/opt/hermes_sandbox/yaan-repo-control"
_PROJECTS_CONF = "projects.conf"
_OWNER_REGISTRY = "upstream-foreign-owners.conf"

# ``<stem>-<role>``: the role words a lane profile can end in. ``platform-stl`` -> ``platform``.
_ROLE_WORDS = frozenset({"coder", "stl", "worker", "majordomo"})
_CODER_SUFFIX = "-coder"

# The ruling's code/test extensions, and nothing else: a `.md` deliverable is not code.
_CODE_EXTENSIONS = frozenset({"py", "sh", "ts", "tsx", "js", "sql", "json"})

# A path-ish token: at least one component and a final ``.<ext>``.
_PATH_TOKEN_RE = re.compile(r"(?<![\w-])([\w.+-]+(?:/[\w.+-]+)*\.[A-Za-z][A-Za-z0-9]*)")
# ``Class.method`` / ``module.symbol`` — a code artefact named by SYMBOL. Filtered in code:
# one component must be snake_case, or the leftmost must be Capitalised, so hosts
# (``github.com``), versions (``v1.2.3``) and dotted prose are not code signals.
_DOTTED_SYMBOL_RE = re.compile(r"(?<![\w.])([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)(?![\w.])")
_TESTS_DIR_RE = re.compile(r"(?<![\w.-])tests/")

# GitHub URL / scp-style remote / bare ``org/repo``; the ``.git`` suffix is optional.
_URL_REF_RE = re.compile(
    r"(?:github\.com[/:])([A-Za-z0-9][\w.-]*)/([A-Za-z0-9][\w.-]*?)(?:\.git)?"
    r"(?=$|[\s/)#'\"`,;])",
)
_SLUG_REF_RE = re.compile(r"(?<![\w./-])([A-Za-z0-9][\w.-]*)/([A-Za-z0-9][\w.-]*)(?![\w/.-])")

# The ops vocabulary, on the child's TITLE: the ruling's "land / deploy / verify / ops", plus
# the sibling verbs the same seat owns. Suffix forms are excluded on purpose — "verification"
# is a legitimate step of a test child (the ruling's own ``t_32d868b7`` is titled "...live-DB
# verification...") while a bare "verify" in the title is the ops seat's work.
_OPS_TITLE_RE = re.compile(
    r"(?<![\w-])("
    r"land|landing|merge|deploy|deployment|release|install|restart|rollout|publish|stamp"
    r"|prove|proven|verify|ops|monitor|monitoring"
    r")(?![\w-])",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# shape: is this child the ops seat's work, or code?
# --------------------------------------------------------------------------- #


def _code_paths(text: str) -> set[str]:
    """Path-ish tokens whose final extension is a code/test one."""
    out = set()
    for match in _PATH_TOKEN_RE.finditer(text):
        token = match.group(1)
        if token.rsplit(".", 1)[-1].lower() in _CODE_EXTENSIONS:
            out.add(token)
    return out


def _code_symbols(text: str) -> set[str]:
    """Dotted code symbols (``StateStore.set_node_outputs``), not hosts or versions."""
    out = set()
    for match in _DOTTED_SYMBOL_RE.finditer(text):
        token = match.group(1)
        parts = token.split(".")
        if any("_" in part for part in parts) or parts[0][:1].isupper():
            out.add(token)
    return out


def is_ops_shaped(title: str) -> bool:
    """True when the child's own TITLE declares landing / deploying / verifying / ops work."""
    return bool(_OPS_TITLE_RE.search(title or ""))


def code_shaped(child: dict, *, parent_text: str = "") -> bool:
    """True when the child names a code or test artefact the domain's coder should own.

    ``parent_text`` is the parent card's title+body: the ruling also counts "a file the
    parent's DoD/PR names" (matched on the basename, so a prefix difference is not a miss).
    """
    title = str(child.get("title") or "")
    if is_ops_shaped(title):
        return False
    body = str(child.get("body") or "")
    text = f"{title}\n{body}"
    if _code_paths(text) or _TESTS_DIR_RE.search(text) or _code_symbols(text):
        return True
    for parent_path in _code_paths(parent_text):
        if parent_path.rsplit("/", 1)[-1] in text:
            return True
    return False


# --------------------------------------------------------------------------- #
# domain: whose work is this?
# --------------------------------------------------------------------------- #


def domain_stem(profile: str) -> str:
    """``platform-stl`` -> ``platform``; a name with no ``<stem>-<role>`` shape -> ``""``."""
    stem, _, role = (profile or "").strip().rpartition("-")
    return stem if stem and role in _ROLE_WORDS else ""


def _control_plane_dir() -> Path:
    return Path(os.environ.get(_CONTROL_PLANE_ENV) or _DEFAULT_CONTROL_PLANE)


def _read_rows(path: Path) -> list[list[str]]:
    """Comment-stripped, whitespace-split rows; a missing file is simply no rows."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            rows.append(line.split())
    return rows


def _checkout_for_repo(leaf: str) -> Optional[Path]:
    """The control plane's own project -> canonical-checkout map (`projects.conf`)."""
    for row in _read_rows(_control_plane_dir() / _PROJECTS_CONF):
        if len(row) >= 2 and row[0] == leaf:
            return Path(row[1])
    return None


def _in_tree_owner(checkout: Optional[Path]) -> str:
    """OWNERS' first non-comment line, first token — exactly `owners.sh`'s rule."""
    if checkout is None:
        return ""
    for row in _read_rows(checkout / "OWNERS"):
        return row[0]
    return ""


def _registry_owner(slug: str) -> str:
    """The upstream-foreign owner registry: a repo whose tree can never carry OWNERS."""
    for row in _read_rows(_control_plane_dir() / _OWNER_REGISTRY):
        if len(row) >= 2 and row[0] == slug:
            return row[1]
    return ""


def repo_refs(text: str) -> list[str]:
    """``org/repo`` slugs the text names, in order and deduped (URLs, remotes, bare slugs)."""
    slugs: list[str] = []
    for org, repo in _URL_REF_RE.findall(text or ""):
        slugs.append(f"{org}/{repo.removesuffix('.git')}")
    for org, repo in _SLUG_REF_RE.findall(text or ""):
        if "." in repo:  # `yoyoflow/state.py` is a file component, never a repo leaf
            continue
        slugs.append(f"{org}/{repo}")
    out: list[str] = []
    for slug in slugs:
        if slug not in out:
            out.append(slug)
    return out


def resolve_domain(
    *, parent_text: str, parent_assignee: Optional[str], board: Optional[str] = None,
) -> tuple[str, str, str]:
    """``(stem, rule, why)`` for the fan-out; ``stem`` is ``""`` when unresolved."""
    for slug in repo_refs(parent_text):
        leaf = slug.split("/", 1)[1]
        owner = _in_tree_owner(_checkout_for_repo(leaf))
        source = "OWNERS"
        if not owner:
            owner = _registry_owner(slug)
            source = "owner registry"
        stem = domain_stem(owner)
        if stem:
            return stem, "repo-owners", f"repo {slug} {source} {owner}"
    stem = domain_stem(parent_assignee or "")
    if stem:
        return stem, "parent-assignee", f"parent assignee {parent_assignee}"
    lane = _board_lane(board)
    stem = domain_stem(lane)
    if stem:
        return stem, "board-lane", f"board {board} lane {lane}"
    return "", "unresolved", "no repo named, no parent lane, no board lane"


def _board_lane(board: Optional[str]) -> str:
    """The board's DECLARED owning lane, if it declares one (board.json ``owner_lane``)."""
    if not board:
        return ""
    try:
        from hermes_cli.kanban_db_boards import read_board_metadata

        meta = read_board_metadata(board)
    except Exception:  # a board that cannot be read is simply no lane
        return ""
    for key in ("owner_lane", "lane"):
        value = meta.get(key) if isinstance(meta, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# --------------------------------------------------------------------------- #
# the post-route
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RouteDecision:
    """What the machine decided for ONE child (``rule == ""``: it left the child alone)."""

    index: int
    title: str
    was: str
    became: str
    rule: str = ""
    why: str = ""
    ambiguous: str = ""

    @property
    def re_routed(self) -> bool:
        return bool(self.rule)


@dataclass
class RoutePlan:
    """One fan-out's decisions, plus the write-back of their record onto the children."""

    decisions: list[RouteDecision]

    @property
    def re_routed(self) -> list[RouteDecision]:
        return [d for d in self.decisions if d.re_routed]

    @property
    def ambiguous(self) -> list[RouteDecision]:
        return [d for d in self.decisions if d.ambiguous]

    def body_line(self, decision: RouteDecision) -> str:
        """The FIRST line of a re-routed child's body, so the worker sees the machine chose."""
        return f"ROUTED BY ROLE: {decision.why} -> {decision.became} (was {decision.was})"

    def record(self, conn, child_ids: Iterable[str], *, root_id: Optional[str] = None) -> None:
        """Record the machine's choice on each child it touched, in ONE write txn.

        ``child_ids`` is ``decompose_triage_task``'s return: child ids in input order. A child
        the machine left alone gets no event, so the record stays a record of decisions rather
        than a log of every routing.
        """
        pairs = [
            (cid, d) for cid, d in zip(child_ids, self.decisions) if d.re_routed or d.ambiguous
        ]
        if not pairs:
            return
        with write_txn(conn):
            for cid, decision in pairs:
                if decision.re_routed:
                    _append_event(
                        conn, cid, "routed_by_role",
                        {
                            "rule": decision.rule,
                            "why": decision.why,
                            "was": decision.was,
                            "became": decision.became,
                        },
                    )
                else:
                    _append_event(
                        conn, cid, "routing_ambiguous",
                        {
                            "child_id": cid,
                            "why": decision.ambiguous,
                            "was": decision.was,
                            "title": decision.title,
                        },
                    )
            if root_id and self.ambiguous:
                _append_event(
                    conn, root_id, "routing_ambiguous",
                    {
                        "children": [
                            {"child_id": cid, "why": d.ambiguous}
                            for cid, d in pairs if d.ambiguous
                        ],
                        "why": (
                            "the domain could not be resolved for a code-shaped child, so the "
                            "decomposer's pick was kept rather than guessed. Name the repo in the "
                            "card, or declare the board's owning lane, to re-route by role."
                        ),
                    },
                )


def route_children_by_role(
    children: list[dict],
    *,
    parent_title: str = "",
    parent_body: str = "",
    parent_assignee: Optional[str] = None,
    roster: Iterable[str] = (),
    ops_seats: Iterable[str] = (),
    board: Optional[str] = None,
) -> RoutePlan:
    """Re-route code-shaped children off the ops seat; mutate ``children`` in place.

    ``ops_seats``: the caller's RESOLVED ``kanban.orchestrator_profile`` /
    ``kanban.default_assignee`` (the tier the card arrived on). ``roster``: every valid
    assignee name. A child that is not on an ops seat is the LLM's to route and is untouched.
    """
    names = {str(name).strip() for name in roster if str(name).strip()}
    seats = {str(seat).strip() for seat in ops_seats if str(seat).strip()}
    coders = sorted(name for name in names if name.endswith(_CODER_SUFFIX))
    parent_text = f"{parent_title}\n{parent_body}"

    # The domain depends on the parent and the board only, so it is resolved once per fan-out.
    stem, rule, why = resolve_domain(
        parent_text=parent_text, parent_assignee=parent_assignee, board=board,
    )

    decisions: list[RouteDecision] = []
    for index, child in enumerate(children):
        was = str(child.get("assignee") or "").strip()
        title = str(child.get("title") or "").strip()
        decision = RouteDecision(index=index, title=title, was=was, became=was)
        if not was or was not in seats:
            decisions.append(decision)
            continue
        if not code_shaped(child, parent_text=parent_text):
            decisions.append(decision)
            continue

        coder = f"{stem}{_CODER_SUFFIX}" if stem else ""
        if coder and coder in names and coder != was:
            destination, rule_kind, phrase = coder, rule, why
        elif not coder and len(coders) == 1:
            destination, rule_kind = coders[0], "sole-coder"
            phrase = f"{why}; the roster's only {_CODER_SUFFIX} lane"
        else:
            reason = (
                f"domain {stem!r} has no {coder!r} in the roster"
                if coder
                else f"{why}; the roster holds {len(coders)} coder lanes"
            )
            decisions.append(replace(decision, ambiguous=reason))
            logger.info("route-role: child %r left on %r — %s", title, was, reason)
            continue

        child["assignee"] = destination
        body = str(child.get("body") or "")
        line = f"ROUTED BY ROLE: {phrase} -> {destination} (was {was})"
        child["body"] = f"{line}\n\n{body}" if body else f"{line}\n"
        decisions.append(replace(decision, became=destination, rule=rule_kind, why=phrase))
        logger.info(
            "route-role: child %r re-routed %r -> %r (%s)", title, was, destination, phrase,
        )
    return RoutePlan(decisions=decisions)
