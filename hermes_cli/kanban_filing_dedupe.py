"""Rules 2 and 3 of the dispatch-surface contract: filing DEDUPE and an emitter QUOTA.

Rule 2 — FILING DEDUPE. A card that is the SAME CARD filed again is an occurrence, not a
second card. At creation the kernel computes a key from ``(board, assignee, normalised
title)`` and one from ``(board, emitter, body subject)``. If an OPEN card already holds a
key, the new filing is REFUSED and the occurrence is written ONTO the existing card — an
``occurrences`` counter plus a comment naming the emitter and the time. That is how a
RECURRING card is supposed to work: one card that says "seen 14 times", not fourteen cards
that each say "seen once" and each cost a dispatch slot.

Normalisation is deliberately NARROW: it strips ids, counts and timestamps only. Two
genuinely different cards that happen to share wording ("... in the parser") must never be
collapsed into one, so no stemming, no synonyms, no fuzzy distance — the same words in the
same order, with the volatile parts removed.

Rule 3 — EMITTER QUOTA. Every card records its emitter (``created_by``). A DAG or an
instrument may file at most N cards per window (default 5 per 24 h) without an explicit
override; beyond that the filings land on a SINGLE rolling card per emitter, with the
occurrences counted. This is the rate limit that stops one broken producer from filling the
board while a monitoring tick that has been dead for 34 hours files nothing at all.

Both rules live HERE rather than in each producer because they are enforced at the kernel's
one filing door (``kanban_db.create_task``): a producer that calls the CLI, the tool verb or
the store API cannot opt out by wiring, and "ungoverned" stops being the state a new surface
gets by doing nothing. This is the sibling of ``kanban_filing_policy`` (which governs the
POOL: how much work the estate may admit at all); this module governs IDENTITY (is this the
same card?) and RATE (who is flooding the door?).

CONFIGURATION IS DATA. Per board, ``board.json``:

    {"ready_cap": 50,
     "filing_dedupe": {"enabled": true},
     "emitter_quota": {"max": 5, "window_hours": 24, "overrides": ["<created_by>", ...]}}

Defaults apply when the keys are absent (dedupe on; quota 5 per 24 h) because the defect
these close is the DEFAULT state of every producer, not an opt-in. A host-wide hatch exists
for rollout: ``HERMES_KANBAN_FILING_RULES_DISABLED=1`` restores the pre-existing behaviour
byte for byte (nothing read beyond the env lookup, no key stamped, no counter touched).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import time
from typing import Any
from typing import Optional

_log = logging.getLogger(__name__)

#: Cards one emitter may BIRTH in one window before its filings roll up. Policy number,
#: read as data by :func:`quota_for`; never baked into a query.
DEFAULT_EMITTER_MAX = 5
DEFAULT_WINDOW_HOURS = 24

DISABLE_ENV = "HERMES_KANBAN_FILING_RULES_DISABLED"

#: Statuses that mean "this row is closed" to both rules. An occurrence on a closed card
#: would be written into a record nobody re-opens, so a closed card is not a match.
_CLOSED = ("done", "archived")

#: The marker segment that keeps an emitter's rolling card from ever colliding with a
#: real title key (a title cannot produce it: normalisation strips angle brackets).
_ROLLUP_MARKER = "emitter-rollup"

# --- normalisation ---------------------------------------------------------------------
#
# Ordered from most specific to least. Every pattern removes VOLATILE text only: a card id,
# a PR reference, a commit hash, a timestamp, a count. Nothing here rewrites words, so two
# different sentences stay different.

_ID_PATTERNS = (
    re.compile(r"\bt_[0-9a-f]{6,}\b", re.I),                    # kanban card ids
    re.compile(r"\bcard\s*#?\d+\b", re.I),                       # "card 1234"
    re.compile(r"\b(?:pr|issue|gh)[\s-]*#?\d+\b", re.I),         # "PR #299", "issue 12"
    re.compile(r"\b[a-z0-9_.-]+#\d+\b", re.I),                   # "repo#4280"
    re.compile(r"#\d+\b"),                                       # bare "#4280"
    re.compile(r"\b[0-9a-f]{7,40}\b", re.I),                     # commit hashes
)
_TIME_PATTERNS = (
    re.compile(r"\b\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:z|[+-]\d{2}:?\d{2})?\b", re.I),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm)?\b", re.I),
)
_COUNT_PATTERNS = (
    re.compile(r"\b\d+(?:\.\d+)?\s*%"),                          # "12%"
    re.compile(r"\b\d+(?:[.,]\d+)*\b"),                           # any remaining bare number
)


def normalise_title(title: Any) -> str:
    """The stable part of a title: ids, counts and timestamps removed, case folded.

    Deliberately conservative — it must NOT make two genuinely different cards equal.
    """
    text = str(title or "")
    for pattern in _ID_PATTERNS + _TIME_PATTERNS + _COUNT_PATTERNS:
        text = pattern.sub(" ", text)
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def body_subject(body: Any, *, limit: int = 200) -> str:
    """The BODY's subject: its first non-empty line, normalised.

    A producer that generates a card per episode usually varies only the title while the
    body's opening line states the same condition; this is the second identity the spec
    asks for. An empty/degenerate subject yields ``""`` and the body leg is skipped —
    never a key every bodyless filing would share.
    """
    for line in str(body or "").splitlines():
        cleaned = normalise_title(line)
        if cleaned:
            return cleaned[:limit]
    return ""


def _digest(*parts: Any) -> str:
    raw = "\x1f".join(str(part if part is not None else "") for part in parts)
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:32]


def dedupe_key(board: Optional[str], assignee: Optional[str], title: Any) -> str:
    """``(board, assignee, normalised title)`` — Rule 2's primary identity."""
    normalised = normalise_title(title)
    if not normalised:
        return ""
    return _digest("title", board or "", assignee or "", normalised)


def body_key(board: Optional[str], emitter: Optional[str], body: Any) -> str:
    """``(board, emitter, body subject)`` — Rule 2's second identity."""
    subject = body_subject(body)
    if not subject:
        return ""
    return _digest("body", board or "", emitter or "", subject)


def rollup_key(board: Optional[str], emitter: Optional[str]) -> str:
    """The key of an emitter's single rolling card."""
    return _digest("rollup", board or "", emitter or "")


# --- configuration ---------------------------------------------------------------------

def _rules_disabled() -> bool:
    return (os.environ.get(DISABLE_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


def _board_meta(board: Optional[str]) -> dict:
    from hermes_cli.kanban_db_boards import read_board_metadata

    try:
        return read_board_metadata(board) or {}
    except Exception:
        return {}


def dedupe_enabled(board: Optional[str]) -> bool:
    if _rules_disabled():
        return False
    leg = _board_meta(board).get("filing_dedupe")
    if isinstance(leg, dict) and leg.get("enabled") is False:
        return False
    return True


def quota_for(board: Optional[str]) -> tuple[int, int, tuple[str, ...]]:
    """``(max_per_window, window_hours, overrides)`` for ``board``; max ``0`` disables."""
    if _rules_disabled():
        return 0, DEFAULT_WINDOW_HOURS, ()
    leg = _board_meta(board).get("emitter_quota")
    maximum: Any = DEFAULT_EMITTER_MAX
    hours: Any = DEFAULT_WINDOW_HOURS
    overrides: tuple[str, ...] = ()
    if isinstance(leg, dict):
        maximum = leg.get("max", maximum)
        hours = leg.get("window_hours", hours)
        raw_overrides = leg.get("overrides") or ()
        if isinstance(raw_overrides, (list, tuple)):
            overrides = tuple(str(name).strip() for name in raw_overrides if str(name).strip())
    try:
        maximum = int(maximum)
    except (TypeError, ValueError):
        maximum = DEFAULT_EMITTER_MAX
    try:
        hours = int(hours)
    except (TypeError, ValueError):
        hours = DEFAULT_WINDOW_HOURS
    if hours <= 0:
        hours = DEFAULT_WINDOW_HOURS
    return (maximum if maximum > 0 else 0), hours, overrides


# --- occurrences -----------------------------------------------------------------------

def _open_match(conn: sqlite3.Connection, column: str, key: str) -> Optional[sqlite3.Row]:
    if not key:
        return None
    placeholders = ",".join("?" * len(_CLOSED))
    return conn.execute(
        f"SELECT id, title, occurrences FROM tasks WHERE {column} = ? "
        f"AND status NOT IN ({placeholders}) ORDER BY created_at DESC LIMIT 1",
        (key, *_CLOSED),
    ).fetchone()


def record_occurrence(
    conn: sqlite3.Connection, task_id: str, *, emitter: Optional[str], when: Optional[int] = None,
    reason: str = "duplicate filing",
) -> int:
    """Bump ``occurrences`` on an existing card and comment who filed it, and when.

    Returns the new count. Writes through the kernel's own comment door so the thread, the
    ``commented`` event and the notification path behave exactly as a hand-written comment.
    """
    from hermes_cli import kanban_db as _kb

    conn.execute(
        "UPDATE tasks SET occurrences = COALESCE(occurrences, 1) + 1 WHERE id = ?", (task_id,)
    )
    row = conn.execute("SELECT occurrences FROM tasks WHERE id = ?", (task_id,)).fetchone()
    count = int(row[0]) if row else 1
    stamp = int(when if when is not None else time.time())
    moment = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))
    _kb.add_comment(
        conn, task_id, emitter or "kanban",
        f"occurrence {count} ({reason}): filed by `{emitter or 'unknown'}` at {moment}.",
    )
    return count


# --- the one assessment -----------------------------------------------------------------

def assess(
    conn: sqlite3.Connection, *, board: Optional[str], assignee: Optional[str],
    created_by: Optional[str], title: str, body: Optional[str],
    idempotency_key: Optional[str] = None,
) -> dict:
    """Rules 2 + 3 for one filing, BEFORE any write.

    Returns a plain record; ``create_task`` owns every write. Keys:

    ``refusal``     the line to return instead of a card id (a duplicate), else ``None``;
    ``rollup_of``   an existing rolling card the filing was absorbed into, else ``None``;
    ``as_rollup``   True when THIS filing must be written as the emitter's rolling card;
    ``dedupe_key``/``body_key``  identities to stamp on a normally-born card;
    ``record``      what to carry on the ``created`` event / the refusal.
    """
    out: dict = {
        "refusal": None, "rollup_of": None, "as_rollup": False,
        "dedupe_key": "", "body_key": "", "record": {},
    }
    emitter = (created_by or "").strip() or None

    # RULE 2 — is this the same card?
    #
    # A caller that STATES its own identity (``idempotency_key``) has already answered this
    # question, and the kernel's own idempotency door answers it BETTER: it returns the
    # existing card's id instead of a refusal. Re-refusing here would break every producer
    # that keys its filings deliberately — the tree-skew guard, the crash sweep — and a
    # duplicate REFUSAL where an id was expected is a worse defect than a duplicate card.
    # So Rule 2 governs UNKEYED filings only; the quota below still governs the emitter.
    # It also governs only filings that NAME AN EMITTER: an occurrence is recorded against
    # a producer ("who filed it, and when"), and a filing with no ``created_by`` is a hand
    # filing — a human raising two cards that read alike is doing work, not flooding.
    if dedupe_enabled(board) and not idempotency_key and emitter:
        title_key = dedupe_key(board, assignee, title)
        subject_key = body_key(board, emitter, body)
        out["dedupe_key"], out["body_key"] = title_key, subject_key
        match = _open_match(conn, "dedupe_key", title_key)
        leg = "title"
        if match is None and subject_key:
            match = _open_match(conn, "body_key", subject_key)
            leg = "body"
        if match is not None:
            count = record_occurrence(
                conn, match["id"], emitter=emitter, reason=f"{leg} match"
            )
            out["refusal"] = (
                f"duplicate: this filing is the same card as {match['id']} "
                f"({leg} match, occurrences={count}). The occurrence was recorded on "
                f"{match['id']}; no new card was created."
            )
            out["record"] = {
                "duplicate_of": match["id"], "leg": leg, "occurrences": count,
                "emitter": emitter,
            }
            return out

    # RULE 3 — is this emitter flooding the door?
    maximum, hours, overrides = quota_for(board)
    if maximum and emitter and emitter not in overrides:
        window_start = int(time.time()) - hours * 3600
        filed = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE created_by = ? AND created_at >= ? "
            "AND status NOT IN (" + ",".join("?" * len(_CLOSED)) + ")",
            (emitter, window_start, *_CLOSED),
        ).fetchone()[0]
        if int(filed) >= maximum:
            key = rollup_key(board, emitter)
            existing = _open_match(conn, "dedupe_key", key)
            if existing is not None:
                count = record_occurrence(
                    conn, existing["id"], emitter=emitter, reason="emitter quota",
                )
                out["rollup_of"] = existing["id"]
                out["record"] = {
                    "quota": {"max": maximum, "window_hours": hours,
                              "filed": int(filed), "rollup": existing["id"],
                              "occurrences": count},
                }
                return out
            out["as_rollup"] = True
            out["dedupe_key"] = key
            out["record"] = {
                "quota": {"max": maximum, "window_hours": hours, "filed": int(filed),
                          "rollup": "new"},
            }
    return out


def rollup_card_text(conn: sqlite3.Connection, *, emitter: str, title: str,
                     body: Optional[str], maximum: int, hours: int) -> tuple[str, str]:
    """Title/body for an emitter's rolling card when this filing opens it."""
    heading = f"emitter quota: `{emitter}` exceeded {maximum} filings/{hours}h"
    detail = (
        f"`{emitter}` reached its filing quota ({maximum} cards per {hours} h). Further "
        f"filings from this emitter are recorded here as occurrences instead of new cards — "
        f"the work is not dropped, it is COLLAPSED, so a broken producer cannot fill the "
        f"board while it is broken.\n\n"
        f"First absorbed filing:\n\n- title: {title}\n- body:\n\n{body or '(none)'}\n\n"
        f"Raise or exempt this emitter with `emitter_quota` on the board's board.json."
    )
    return heading, detail
