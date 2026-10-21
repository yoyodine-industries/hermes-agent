"""Will this parent FINISH on its own? — the per-edge promotion state of a dependency.

A link gates a child behind a parent, but a gate is not a promise. The child's promotion is
contingent on the PARENT finishing, and a parent parked in ``review``, ``triage`` or
``blocked`` — or one whose assignee is not a live profile — will never move until somebody
acts. The link door therefore has to say, in the parent's own terms, whether the wait it just
recorded can end by itself.

This module is PURE and stdlib-only on purpose: it is the one place that decides what a
parent's state means, it takes the clock as data (``due_at`` on the row), and it can be
exactly mirrored by any other implementation. The ordered tuple :data:`PARENT_STATE_CODES` is
the contract: a consumer that switches on strings dispatches off these codes, never off
prose, and never off a status string the board may grow later (that is ``other``).

Text is user-facing: one sentence, always third-person singular, appended to a parent's id.
``self_moves`` is True only for a parent that is already moving (``running``) or already
moved (``done``); everything else is named by the reason it will not finish by itself.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Mapping, Optional

#: Every code :func:`parent_promotion_state` can return, in classifier order. ``other`` is
#: terminal: an unknown status is reported as itself, never guessed into a known branch.
PARENT_STATE_CODES = (
    "running",
    "done",
    "blocked",
    "review",
    "triage",
    "scheduled",
    "unassigned",
    "dead-assignee",
    "ready",
    "todo",
    "other",
)


@dataclass(frozen=True)
class ParentState:
    """One parent edge's promotion state: what will make this parent finish.

    ``code`` is one of :data:`PARENT_STATE_CODES`; ``text`` is the sentence a reader gets
    after the parent's id (``"<id> " + text``); ``self_moves`` says whether the child's
    promotion can happen with nobody touching this parent.
    """

    code: str
    text: str
    self_moves: bool


def _epoch(value) -> Optional[float]:
    """A ``due_at``-shaped value as epoch seconds, or None when it carries no clock.

    Mirrors the board's own tolerance: epoch numbers pass through, ISO-8601 (with ``Z``,
    ``+HHMM`` or a naive stamp) and the naive SQL timestamp shape are parsed as UTC.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+0000")
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            parsed = _dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.UTC)
        return parsed.timestamp()
    return None


def _clock_text(value) -> str:
    """A ``due_at`` as an ISO-8601 UTC clock, or "" when it carries none."""
    ts = _epoch(value)
    if ts is None:
        return ""
    return _dt.datetime.fromtimestamp(ts, _dt.UTC).strftime("%Y-%m-%dT%H:%M:%S+0000")


def _field(row, key):
    """One column of a ``sqlite3.Row`` / mapping / object — absent reads as None.

    ``sqlite3.Row`` has no ``.get`` (and raises ``IndexError``, not ``KeyError``), so the
    classifier takes rows and dicts through this one accessor instead of guessing the type.
    """
    if isinstance(row, Mapping):
        return row.get(key)
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return getattr(row, key, None)


def parent_promotion_state(parent, *, profiles=None) -> ParentState:
    """The promotion state of ONE parent row: what will make it finish.

    ``parent`` is anything a row-like accessor reads (a ``sqlite3.Row`` or a mapping):
    ``status``, ``assignee``, ``block_kind``, ``current_run_id``, ``due_at``. ``profiles`` is
    the caller-supplied set/list of live profile names: **passing None means "not injected"**
    and skips the dead-assignee probe entirely rather than guessing a fleet from disk.

    An unknown status falls through to ``other`` — a code consumer never has to learn a
    status the board grew later, it just reports the wait as unresolvable-by-itself.
    """
    status = str(_field(parent, "status") or "")
    assignee = str(_field(parent, "assignee") or "").strip()
    if status == "running":
        run = _field(parent, "current_run_id")
        suffix = "" if run in (None, "") else f" (run {run})"
        return ParentState("running", f"is RUNNING{suffix}", True)
    if status == "done":
        return ParentState("done", "is DONE (this edge is already satisfied)", True)
    if status == "blocked":
        kind = str(_field(parent, "block_kind") or "").strip()
        suffix = "" if not kind else f" (block_kind={kind})"
        return ParentState(
            "blocked",
            f"is BLOCKED{suffix}: a release has to clear that park before it can run",
            False)
    if status == "review":
        return ParentState(
            "review", "is in REVIEW: a reviewer has to accept it before it can finish", False)
    if status == "triage":
        return ParentState(
            "triage", "sits in TRIAGE: it has to be specified before anything runs it", False)
    if status == "scheduled":
        due = _clock_text(_field(parent, "due_at"))
        suffix = "" if not due else f" until {due}"
        return ParentState(
            "scheduled",
            f"is SCHEDULED{suffix}: nothing runs it before that clock",
            False)
    if not assignee:
        return ParentState(
            "unassigned", "has NO ASSIGNEE, so no dispatcher will ever pick it up", False)
    if profiles is not None and assignee not in profiles:
        return ParentState(
            "dead-assignee",
            f"is assigned to `{assignee}`, which is not a live profile, so no dispatcher "
            "will pick it up", False)
    if status == "ready":
        return ParentState(
            "ready",
            f"is READY and nothing has claimed it (assignee `{assignee}`): no run is coming",
            False)
    if status == "todo":
        return ParentState(
            "todo",
            f"is itself WAITING in `todo` behind its own parents (assignee `{assignee}`)",
            False)
    return ParentState(
        "other",
        f"is `{status or 'unknown'}`, which is not a state that finishes on its own",
        False)
