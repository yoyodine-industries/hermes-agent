"""The PROJECT-LINK evidence surface of ``create_task``: what a card is linked to, and WHY.

A card's project decides its workspace (a deterministic ``<repo>/.worktrees/<id>`` worktree and a
branch under that repo), so a wrong link is not cosmetic: every later step -- edit, test, PR --
starts in the wrong tree. Measured on the ``skills-refactor`` board (card ``t_b407489b``): three
cards whose work was in ``hermes-agent`` were auto-linked to the ``skills-corpus`` project by the
board's default and filed under its worktree, silently, because nothing on the card said how the
link had been decided.

Two rules live here, and ``create_task`` calls both at the birth seam:

* **The link records its source.** Every link writes ``project_link_source`` onto the ``created``
  event -- ``explicit`` (the caller passed one), ``board`` (the board's default project),
  ``inherited`` (the project of the card this one was split from) or ``nearest`` (a match
  inferred from the card's own text). A deliberate link and a guessed one stop looking the same.
* **An inferred link the card contradicts is DROPPED, loudly.** When the only reason for the
  link is inference (``board``/``nearest``) and the card's title or body names a repository that
  belongs to a DIFFERENT registered project, the link is not applied: the card is filed without
  a project (exactly as the caller left it) and the drop is reported on stderr and on the
  ``created`` event, in the same shape ``warn_unresolved_project_link`` already uses for an id
  this profile's registry cannot resolve.

The mismatch test is mechanical -- path normalisation and registered-project identity only, no
prose reading, no scoring. When it cannot decide (no path token, a path no project owns, an
ambiguous name) it says NOTHING and the link stands: guessing is the defect, not the fix, and a
spurious warning on every card would train the reader to ignore the real one.

Registry note: ``projects.db`` is per profile, and a repo no project owns can never be matched
here. That is deliberate -- the remedy for "no project is anchored on this repo" is to register
one (card ``t_000d63fc``), not to teach this seam heuristics.

Detection is SEPARATE from output on purpose: ``create_task`` has to know before it resolves the
link (a contradicted project must not anchor a worktree), but must not PRINT before the
idempotency check (a deduped filing creates no card to warn about).
"""

from __future__ import annotations

import logging
import os
import re
import sys
from typing import Any, Optional

from hermes_cli import projects_db as _pdb

_log = logging.getLogger(__name__)

#: The source values ``project_link_source`` carries. Only the first three are PRODUCED today:
#: nothing in the kernel infers a project from a card's text, so ``nearest`` is the value a
#: content-inferred matcher would write when one exists (and the contradiction rule below already
#: judges it, so such a matcher needs no second gate).
LINK_EXPLICIT = "explicit"
LINK_BOARD = "board"
LINK_INHERITED = "inherited"
LINK_NEAREST = "nearest"
LINK_SOURCES = (LINK_EXPLICIT, LINK_BOARD, LINK_INHERITED, LINK_NEAREST)

#: Sources that are INFERRED rather than stated by the caller: a link from one of these may be
#: contradicted by the card's own text. An explicit link is never second-guessed, and an inherited
#: one carries the evidence of the card it was split from (design ruling, card t_f801b260 §4).
INFERRED_SOURCES = frozenset({LINK_BOARD, LINK_NEAREST})

#: A run of non-space text containing at least one ``/`` -- the only shape that can name a path.
#: A LEADING ``/`` or ``~`` is part of the token: without it an absolute path reads as a relative
#: one and its first segment ("private", "Users") is mistaken for a repo name.
_PATHISH = re.compile(r"[/~]*[A-Za-z0-9._@%+\-]+(?:/[A-Za-z0-9._@%+\-]+)+/?")
_TRAILING = ".,;:!?)]}»\"'`"


def registry_path() -> str:
    """This profile's project registry, for a warning that tells the reader where to look."""
    try:
        return str(_pdb.projects_db_path())
    except Exception as exc:  # health: allow BLE001 -- a warning must never fail a filing
        _log.warning("kanban: project registry path unreadable: %s", exc, exc_info=True)
        return "this profile's projects.db"


def _asked_as(source: str) -> str:
    if source == LINK_EXPLICIT:
        return "--project"
    if source == LINK_INHERITED:
        return "the source card's project"
    return "the board's default project"


def warn_unresolved_project_link(project_id: str, *, source: str) -> dict:
    """The warning + ``created``-event note for a project link this registry cannot resolve.

    The link is DROPPED by design (never a dangling reference, never a crash) -- but the registry
    is PER-PROFILE (``$HERMES_HOME/projects.db``), so the drop is easy to walk into: a caller that
    passed ``--project``, or a board scoped to a project another profile's registry holds, gets an
    ordinary scratch card. A silent drop is a silent failure, so the drop stays and is made LOUD:
    a stderr warning naming the ident, the registry searched and the two honest paths, plus the
    fields the ``created`` event carries (``project_link_unresolved``, ``..._source``,
    ``..._registry``). Never raises -- a warning must never be the thing that fails a filing.
    """
    registry = registry_path()
    print(
        f"warning: kanban: {_asked_as(source)} {project_id!r} is not in this profile's project "
        f"registry ({registry}); the card was filed WITHOUT the project link. Create the project "
        f"in this profile (`hermes project create <name> --slug {project_id}`) or link by SLUG "
        f"from the registry that owns it (`hermes project list`).",
        file=sys.stderr,
    )
    _log.warning(
        "kanban: unresolved project link %r (source=%s) - card filed unscoped", project_id, source,
    )
    return {
        "project_link_unresolved": project_id,
        "project_link_source": source,
        "project_link_registry": registry,
    }


def _norm(path: str) -> str:
    """``path`` as an absolute, un-trailing-slashed string: comparison is textual, never ``realpath``."""
    expanded = os.path.expanduser(str(path or "").strip())
    return os.path.normpath(expanded) if expanded else ""


def _owns(root: str, path: str) -> bool:
    stem = _norm(root).rstrip(os.sep)
    return bool(stem) and (path == stem or path.startswith(stem + os.sep))


def _own_roots(project: Any) -> list[str]:
    roots = [_norm(project.primary_path)] if project.primary_path else []
    roots.extend(_norm(folder.path) for folder in (getattr(project, "folders", None) or []))
    return [root for root in roots if root]


def _names(project: Any) -> set[str]:
    """The names a card could use to refer to this project's repo, without a full path."""
    names = {str(project.slug or "")}
    if project.primary_path:
        names.add(os.path.basename(_norm(project.primary_path)))
    return {name for name in names if name}


def _tokens(text: str) -> list[str]:
    """Every ``/``-bearing token in ``text``, trimmed of surrounding punctuation, in order."""
    seen: list[str] = []
    for match in _PATHISH.finditer(text or ""):
        token = match.group(0).strip(_TRAILING)
        if token and "/" in token and token not in seen:
            seen.append(token)
    return seen


def _owner_of_path(conn: Any, path: str) -> Optional[Any]:
    """The registered project whose folder holds ``path`` -- the mechanically decidable case."""
    try:
        return _pdb.project_for_path(conn, path)
    except Exception as exc:  # health: allow BLE001 -- a warning must never fail a filing
        _log.warning("kanban: project lookup failed for %r: %s", path, exc, exc_info=True)
        return None


def _conflict(conn: Any, candidate: Any, others: list[Any], token: str) -> Optional[dict]:
    """The conflict ``token`` states against ``candidate``, or ``None`` when it states none."""
    if token.startswith(("/", "~")):
        path = _norm(token)
        if not path:
            return None
        if any(_owns(root, path) for root in _own_roots(candidate)):
            return None  # the card points INTO the project it was linked to: consistent
        owner = _owner_of_path(conn, path)
        if owner is None or owner.id == candidate.id:
            return None  # no project owns it (or it is this one): cannot decide, stay silent
        return {"evidence": token, "kind": "path", "owner": owner}
    head = token.split("/", 1)[0]
    if not head or head in (".", "..") or head in _names(candidate):
        return None
    for other in others:
        if head in _names(other):
            return {"evidence": token, "kind": "repo-name", "owner": other}
    return None


def _public(project: Any) -> dict:
    return {"id": project.id, "slug": project.slug, "primary_path": _norm(project.primary_path or "")}


def contradiction_evidence(
    project_id: Optional[str], *, source: str, title: str, body: Optional[str] = None,
) -> dict:
    """The conflict an INFERRED link's card text states, as plain data; ``{}`` when none does.

    Detection only -- it writes nothing and prints nothing, so ``create_task`` can ask BEFORE it
    resolves the link (see the module docstring) and report only once it knows a card is created.
    Never raises: a warning must never be the thing that fails a filing.
    """
    if not project_id or source not in INFERRED_SOURCES:
        return {}
    tokens = _tokens(f"{title or ''}\n{body or ''}")
    if not tokens:
        return {}
    try:
        with _pdb.connect_closing() as conn:
            candidate = _pdb.get_project(conn, str(project_id))
            if candidate is None:
                return {}
            others = [p for p in _pdb.list_projects(conn) if p.id != candidate.id]
            for token in tokens:
                conflict = _conflict(conn, candidate, others, token)
                if conflict is not None:
                    return {
                        "candidate": _public(candidate),
                        "evidence": conflict["evidence"],
                        "kind": conflict["kind"],
                        "belongs_to": _public(conflict["owner"]),
                    }
    except Exception as exc:  # health: allow BLE001 -- a warning must never fail a filing
        _log.warning("kanban: project-link evidence check skipped: %s", exc, exc_info=True)
    return {}


def _board_project(board: Optional[str]) -> Optional[str]:
    """The board's declared default project (``board.json:project_id``), or ``None``."""
    from hermes_cli.kanban_db import _board_meta_for  # sibling -> facade, at call time

    try:
        return (_board_meta_for(board).get("project_id") or "").strip() or None
    except Exception as exc:  # health: allow BLE001 -- a warning must never fail a filing
        _log.warning("kanban: board project unreadable for %r: %s", board, exc, exc_info=True)
        return None


def plan_project_link(
    *, project_id: Optional[str], source_task_id: Optional[str], board: Optional[str],
    workspace_kind: Optional[str], title: str, body: Optional[str] = None,
) -> dict:
    """Decide the link BEFORE it is resolved: which id to resolve, its SOURCE, and any contradiction.

    Returns ``{"project_id", "requested", "source", "contradiction"}``. ``requested`` is what the
    caller or the board asked for (the ident a drop warning names); ``project_id`` is ``None`` when
    nothing was requested, or when an inferred link was contradicted -- in which case the card is
    filed exactly as an unscoped one.

    A board default applies only when the caller named no project AND did not ask for ``scratch``:
    an explicit ``scratch`` (or ``project_id=""``) is a request for NO project and must not be
    upgraded to a worktree in the board's repo (#106342).
    """
    source = LINK_EXPLICIT
    if project_id is None and workspace_kind != "scratch":
        project_id = _board_project(board)
        source = LINK_BOARD if project_id else LINK_EXPLICIT
    elif project_id is not None and source_task_id:
        # The card this one was filed from named the project (the split-card chain): a STATED
        # source, never an inference, so the contradiction rule leaves it alone.
        source = LINK_INHERITED
    contradiction = contradiction_evidence(project_id, source=source, title=title, body=body)
    return {
        "project_id": None if contradiction else project_id,
        "requested": project_id,
        "source": source,
        "contradiction": contradiction,
    }


def link_note(
    *, project_id: Optional[str], requested: Optional[str], source: str, contradiction: dict,
) -> dict:
    """The ``created``-event note for the link: its SOURCE, or the record of why it did not land.

    One note, one convention (``project_link_unresolved`` / ``..._source`` / ``..._registry``), so
    a dropped-and-contradicted link and a dropped-and-unresolvable one read the same on the card.
    """
    if contradiction:
        return contradicted_link_note(contradiction, source=source)
    if requested and project_id is None:
        return warn_unresolved_project_link(requested, source=source)
    if project_id:
        return {"project_link_source": source}
    return {}


def contradicted_link_note(evidence: dict, *, source: str) -> dict:
    """The stderr warning + ``created``-event note for a contradiction ``contradiction_evidence`` found."""
    if not evidence:
        return {}
    candidate = evidence["candidate"]
    owner = evidence["belongs_to"]
    registry = registry_path()
    print(
        f"warning: kanban: {_asked_as(source)} {candidate['slug']!r} resolves to project "
        f"{candidate['id']} ({candidate['primary_path'] or 'no primary folder'}), but this card "
        f"names {evidence['evidence']!r}, which belongs to project {owner['slug']!r} "
        f"({owner['primary_path'] or 'no primary folder'}): the card was filed WITHOUT the "
        f"project link. Pass --project explicitly if the card does belong to "
        f"{candidate['slug']!r}, or file it on the board of the repo it touches "
        f"(`hermes project list`, registry {registry}).",
        file=sys.stderr,
    )
    _log.warning(
        "kanban: inferred project link %r contradicted by %r (%s) - card filed unscoped",
        candidate["id"], evidence["evidence"], owner["slug"],
    )
    return {
        "project_link_unresolved": candidate["id"],
        "project_link_source": source,
        "project_link_registry": registry,
        "project_link_contradiction": evidence,
    }
