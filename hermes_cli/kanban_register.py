"""The operator-ask register: the reference stamp, its resolution, and the roll-up.

WHY THIS EXISTS (operator ask 2026-09-27, register card ``t_fc615201``,
delivery card ``t_8ca4b5a0``)
---------------------------------------------------------------------------
Every card created in service of an operator request must be capturable under the
register card, so the whole body of work rolls up to the operator without anyone
reconstructing it from memory. Two structural facts broke the register tree and no
convention can fix either of them:

* **KANBAN EDGES CANNOT CROSS BOARDS** (measured). A card filed on the ``ops`` board
  can never be a child of a ``defcon`` card, so off-board work spawned by an operator
  ask is invisible to the register tree *by construction*.
* **DEEP CHAINS AND DECOMPOSITIONS GET THEIR OWN PARENTS**, so lineage runs three to
  six levels and a one-or-two-level walk finds almost nothing. Measured the same
  evening: the register held 27 direct children and 52 descendants, while 190 cards
  were created on ``defcon`` and 310 on ``ops`` that day.

The fix has three parts, and this module owns all three:

1. **A STAMP in the card's BODY** (``Operator-ask: <register>/<ask>``) - a reference
   that survives where an edge cannot exist, because it is text, not a row in this
   board's ``task_links``. The body is where a human reads the card too.
2. **THE STAMP IS APPLIED AT THE CREATE PATH**, never by a human remembering: a card
   inherits its ask from an explicit request, from the ask its own worker session is
   running (the dispatcher exports ``HERMES_KANBAN_OPERATOR_ASK``), from the body the
   filer supplied, or from its parents - in that order. Wired into the two insert
   seams (``kanban_db.create_task`` and ``kanban_db_graph._insert_decomposed_child``)
   it cannot be routed around by a caller that forgets.
3. **A ROLL-UP** (``hermes kanban rollup``) that walks edges *and* references across
   EVERY registered board and answers, in one command, what is in service of the
   register, with state and owner.

WHY THE REGISTER IS BOARD-SCOPED METADATA, NOT A SCHEMA CHANGE
-------------------------------------------------------------
The register card for a board is a board fact, so it lives where the board's other
policy facts live - ``board.json`` under ``operator_register`` - and is read with the
same ``read_board_metadata`` every other board-scoped reader uses. That keeps this
change out of the schema of every board's store (12 of them on this host) and gives
it the same blast radius as ``priority_policy``/``project_id``. Nothing here writes
to a board's DB: the stamp is the only durable artifact, and it is written by the
filer's own insert.

WHY THE ROLL-UP READS OTHER BOARDS READ-ONLY
--------------------------------------------
A roll-up is a READER. It opens every registered board's store with ``mode=ro`` (the
same idiom ``kanban_db_connect`` uses for fenced readers), skips a store that holds no
``tasks`` table instead of initializing it, and never migrates or backfills on another
board's behalf.

THE STAMP, EXACTLY
------------------
A single line, at the END of the body, one per card, idempotent::

    Operator-ask: t_fc615201/t_8ca4b5a0

* ``<register>`` - the register card (the board's ``operator_register``).
* ``<ask>`` - the register's DIRECT CHILD that captured this request; a card that is
  itself a fresh ask under the register carries its OWN id as the ask half, so every
  stamped card names the ask it serves without a second lookup.
* Ids are ``t_`` + hex, so ``/`` is unambiguous; the line is anchored-parsable
  (``^Operator-ask:``) and one line no matter how long the body is.
* At most ONE stamp per body: a stamp already present is replaced when a ref
  resolves, and left untouched when none does.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

# The line. Anchored at the start of a line so prose quoting the format (like this
# module's own docstring) is not mistaken for a stamp, and tolerant of surrounding
# blank lines / trailing spaces so a hand-edited body still parses.
STAMP_PREFIX = "Operator-ask:"
_STAMP_RE = re.compile(r"^[ \t]*Operator-ask:[ \t]*(\S+)[ \t]*$", re.MULTILINE)
_TASK_ID_RE = re.compile(r"^t_[0-9a-f]{4,32}$")

#: Board metadata key (``board.json``) naming the register card for that board.
META_KEY = "operator_register"

#: The environment variable the dispatcher exports into a worker whose card serves an
#: ask, so every card that worker files inherits it without the agent remembering.
ENV_VAR = "HERMES_KANBAN_OPERATOR_ASK"

#: The escape hatch, for the CLI: ``1`` admits a deliberate off-board NEW ask
#: (the ``allow_off_board_ask`` kwarg is the same hatch for a programmatic caller).
ALLOW_ENV_VAR = "HERMES_KANBAN_ALLOW_OFF_BOARD_ASK"

#: The ``task_events`` kind written when the hatch lets an off-board NEW ask through,
#: so the escape is on the record instead of silent.
OFF_BOARD_ASK_EVENT = "operator_ask_off_board"

#: How far the ancestor walk chases an unstamped card. Deep chains are the defect this
#: feature exists for, but a walk must still terminate on a corrupt (cyclic) chain.
MAX_ANCESTOR_WALK = 12


__all__ = [
    "ALLOW_ENV_VAR",
    "ENV_VAR",
    "META_KEY",
    "OFF_BOARD_ASK_EVENT",
    "STAMP_PREFIX",
    "AskRef",
    "OperatorAskOffBoardError",
    "Rollup",
    "RollupAsk",
    "RollupCard",
    "apply_stamp",
    "dumps",
    "env_ref_for_worker",
    "find_card_board",
    "guard_new_ask_home",
    "is_task_id",
    "open_board_ro",
    "parse_ref",
    "parse_stamp",
    "register_board",
    "register_for_board",
    "registered_boards",
    "render_rollup",
    "resolve_for_create",
    "resolve_for_task",
    "rollup",
    "rollup_json",
    "stamp_line",
    "strip_stamp",
]


# --------------------------------------------------------------------------- ids


def is_task_id(value: Any) -> bool:
    """True for a syntactically valid card id (``t_`` + hex)."""
    return bool(isinstance(value, str) and _TASK_ID_RE.match(value.strip()))


def parse_ref(raw: Any) -> Optional[tuple[str, Optional[str]]]:
    """``"t_reg/t_ask"`` / ``"t_x"`` -> ``(register, ask|None)``; ``None`` if unusable.

    Strict on purpose: a malformed reference is a caller error the caller must see
    (the CLI and the tool surface refuse it), not something to guess a card id out of.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    register, _, ask = text.partition("/")
    register = register.strip()
    ask = ask.strip()
    if not is_task_id(register):
        return None
    if ask and not is_task_id(ask):
        return None
    return register, (ask or None)


def _render(register: str, ask: Optional[str]) -> str:
    return f"{register}/{ask}" if ask else register


# -------------------------------------------------------------------------- stamp


def stamp_line(register: str, ask: str) -> str:
    """The canonical stamp line for the pair."""
    return f"{STAMP_PREFIX} {register}/{ask}"


def parse_stamp(body: Optional[str]) -> Optional[tuple[str, str]]:
    """``(register, ask)`` as stamped in ``body``, or ``None``.

    Tolerant of the ask half being absent from an older/hand-written stamp only in the
    sense that a malformed line is *reported* by the caller that needs it to resolve;
    a line whose halves are not card ids is not a stamp and is ignored here.
    """
    if not body:
        return None
    body = _text(body) or ""
    matches = _STAMP_RE.findall(body)
    if not matches:
        return None
    # The LAST match wins: the stamp is written as a card's last line, so on a body that
    # carries two (a hand-edit, or a quoted example) the appended one is the card's own.
    parsed = parse_ref(matches[-1])
    if not parsed:
        return None
    register, ask = parsed
    if ask is None:
        return None
    return register, ask


def strip_stamp(body: Optional[str | bytes]) -> str:
    """``body`` without its stamp line (and the blank line the writer added), stripped.

    ``body`` may arrive as ``bytes``: sqlite hands a BLOB body back undecodable (the same
    measured fact ``_text`` exists for), and a caller may pass one in directly. The
    normalisation lives HERE, at the one writer BOTH call sites (``create_task`` and the
    decompose insert) route through, so a body the read path already tolerates can never
    reach the stamp regex as bytes.
    """
    text = _text(body)
    if not text:
        return ""
    stripped = _STAMP_RE.sub("\n", text)
    # Collapse the newline the removal can leave behind, so a re-stamp is byte-stable
    # and a body with no stamp is returned unchanged.
    if stripped != text:
        stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    return stripped.strip()


def apply_stamp(body: Optional[str | bytes], register: str, ask: str) -> str:
    """``body`` carrying ``stamp_line(register, ask)`` as its last line (idempotent).

    Accepts the same ``str`` / ``bytes`` / ``None`` shapes as ``strip_stamp`` above.

    The stamp goes LAST: it is metadata about the card, and the opening of a body is
    the card's own argument for the reader. A body that already carries a stamp gets
    that stamp REPLACED - one card, one ask, no ambiguity for the roll-up.
    """
    base = strip_stamp(body)
    line = stamp_line(register, ask)
    return f"{base}\n\n{line}" if base else line


# ----------------------------------------------------------------- board metadata


def register_for_board(board: Optional[str]) -> Optional[str]:
    """The register card id designated for ``board``, or ``None``.

    A malformed value is reported as ``None`` here (a reader must not raise): the
    writer (``hermes kanban boards set-operator-register``) validates on the way in.
    """
    from hermes_cli.kanban_db import read_board_metadata

    try:
        raw = read_board_metadata(board).get(META_KEY)
    except Exception:
        return None
    return raw.strip() if isinstance(raw, str) and is_task_id(raw) else None


# ------------------------------------------------------------------- ask records


@dataclass(frozen=True)
class AskRef:
    """The ask a card serves, plus where the answer came from (for the event record)."""

    register: Optional[str]
    ask: Optional[str]
    source: str
    unresolved: Optional[str] = None

    def for_card(self, task_id: str) -> Optional[tuple[str, str]]:
        """The pair to stamp on ``task_id``: an absent ask half means ``task_id`` itself.

        A card filed directly under the register (``--serves <register>``, or a child of
        the register card) IS a new ask, so it carries its own id as the ask half: the
        stamp is then self-describing and no later lookup has to re-derive which child
        of the register started this work.
        """
        if not self.register:
            return None
        return self.register, (self.ask or task_id)


# ------------------------------------------------------------------- resolution


def _text(value) -> Optional[str]:
    """Whatever the store hands back, as text: ``str``, ``bytes`` or ``None``.

    Measured 2026-09-27 on the live boards: at least one row's body is not valid UTF-8,
    so sqlite returns it as ``bytes`` (see ``tests/hermes_cli/
    test_kanban_list_undecodable_text.py``). One unreadable body must never take the
    whole roll-up down, and it must still be VISIBLE - so it is decoded lossily and its
    stamp, if any, is read from the replacement text.
    """
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _row(conn: sqlite3.Connection, task_id: str) -> Optional[tuple[str, Optional[str]]]:
    """``(id, body)`` for a card, or ``None``. Positional, so the caller's row factory
    (``Row``, tuple or a lossy text wrapper) cannot decide whether this works."""
    try:
        row = conn.execute("SELECT id, body FROM tasks WHERE id = ?", (task_id,)).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return row[0], row[1]


def _parent_ids(conn: sqlite3.Connection, task_id: str) -> list[str]:
    try:
        rows = conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ?", (task_id,)
        ).fetchall()
    except sqlite3.Error:
        return []
    return [r[0] for r in rows if r[0]]


def resolve_for_task(
    conn: sqlite3.Connection, task_id: str, *, board: Optional[str] = None,
) -> Optional[tuple[str, str]]:
    """The pair a card ALREADY serves: its own stamp, else its register ancestor.

    Used for inheritance (a card born under a card) and by the dispatcher (a worker's
    own ask). The ancestor walk is what covers a card born before this feature existed:
    a legacy register child carries no stamp, but the register does - or is the board's
    designation - and the walk back to it yields ``(register, the child on the path)``.

    Terminates on a corrupt (cyclic) chain by refusing to revisit a node on its own
    path, and gives up past :data:`MAX_ANCESTOR_WALK` steps rather than reading a whole
    board to answer a question a missing stamp already answered badly.
    """

    def walk(current: str, depth: int, path: list[str]) -> Optional[tuple[str, str]]:
        # ``path`` holds the cards visited BEFORE ``current``, nearest first - so the walk
        # never rejects its own starting card (which is in the path by construction).
        if depth > MAX_ANCESTOR_WALK or not current or current in path:
            return None
        row = _row(conn, current)
        if row is not None:
            stamped = parse_stamp(row[1])
            if stamped:
                return stamped
        if register and current == register:
            # The register ends this branch. The ask is the register's direct child on
            # the path we came through - unless there is no path, which means the caller
            # is looking at the register itself and is about to create a brand-new ask
            # (the empty ask half the caller fills with the new card's id).
            return (register, path[-1]) if path else (register, "")
        for parent in _parent_ids(conn, current):
            answer = walk(parent, depth + 1, [*path, current])
            if answer:
                return answer
        return None

    register = register_for_board(board) if board else None
    return walk(task_id, 0, [])


def restamp_rewritten_body(
    conn: sqlite3.Connection, task_id: str, new_body: Optional[str], *,
    board: Optional[str] = None, previous: Optional[str] = None,
) -> Optional[str]:
    """``new_body`` carrying the ask the card ALREADY serves, or ``new_body`` itself.

    The INSERT seams stamp a card at birth (``kanban_db.create_task``, the decomposed
    children in ``kanban_db_graph``); the seams that REWRITE an existing body
    (``kanban_db.edit_task``, ``kanban_db.specify_triage_task``, the dashboard's PATCH
    title/body) wrote the caller's text verbatim. A stamp is how the roll-up knows a
    card is in service at all (``_cards_of_board`` reads bodies, not events), so an edit
    that dropped the line took the card - and, through the edges it anchors, everything
    filed below it - out of the register's roll-up, and stopped the worker's
    ``HERMES_KANBAN_OPERATOR_ASK`` export (:func:`env_ref_for_worker`): a card silently
    resolving out of an operator's view because someone rewrote its body. Measured live
    2026-09-29 on ``t_0f589e4a``, whose probe body lost its stamp at the specify seam.

    The pair is read from the body being REPLACED (``previous`` when the caller already
    holds it, else the stored row): once replacement text exists, the old body is the
    only place the card's own reference is written down. Only failing a stamp there does
    the lineage answer (:func:`resolve_for_task`) - the same inheritance a filing seam
    would have applied. A card that serves nothing is NOT invented a stamp: an ask
    resolves from evidence, never from a rewrite.
    """
    if new_body is None:
        return None
    if parse_stamp(new_body):
        # The caller stamped its own text. Replacing that would overrule a deliberate
        # reference, which is not this seam's business.
        return new_body
    if previous is None:
        row = _row(conn, task_id)
        previous = row[1] if row is not None else None
    pair = parse_stamp(previous) or resolve_for_task(conn, task_id, board=board)
    if not pair or not all(pair):
        # ``resolve_for_task`` answers ``(register, "")`` for the register itself - an
        # empty ask half is a caller about to open a NEW ask, never a stamp to inherit.
        return new_body
    return apply_stamp(new_body, *pair)


def resolve_for_create(
    conn: sqlite3.Connection, *, board: Optional[str], parents: Iterable[str] = (),
    body: Optional[str] = None, explicit: Optional[str] = None,
    env: Optional[str] = None, find_card: Optional[Any] = None,
) -> Optional[AskRef]:
    """The ask a card being created serves, resolved in a fixed, documented order.

    1. ``explicit`` - the filer said so (``--serves`` / the ``serves`` tool arg).
    2. ``env`` - the worker session runs the ask (``HERMES_KANBAN_OPERATOR_ASK``).
    3. ``body`` - the filer supplied a body that already carries a stamp (a re-filed or
       copied card), which is a deliberate statement about that text.
    4. ``parents`` - lineage: a card born under a card that serves an ask serves it too.
       This is the case the operator's defect named (deep chains and decompositions).

    Explicit first because it is the most deliberate; the session's ask above the body
    because a body copied from a sibling must not re-file this session's work under the
    sibling's ask; parents last because structure is inherited, not asserted.

    ``find_card`` (``(task_id) -> board|None``) lets the single-id form of an explicit
    or env reference resolve a card that lives on ANOTHER board - the cross-board case
    this whole feature is about - with the caller supplying the cross-board probe.
    """
    if explicit is not None:
        parsed = parse_ref(explicit)
        if parsed is None:
            return AskRef(None, None, "explicit", unresolved=str(explicit).strip())
        register, ask = parsed
        if ask:
            return AskRef(register, ask, "explicit")
        return _resolve_single_id(
            conn, register, source="explicit", board=board, find_card=find_card,
        )
    if env:
        parsed = parse_ref(env)
        if parsed is None:
            return None
        register, ask = parsed
        if ask:
            return AskRef(register, ask, "env")
        return _resolve_single_id(
            conn, register, source="env", board=board, find_card=find_card,
        )
    stamped = parse_stamp(body)
    if stamped:
        return AskRef(stamped[0], stamped[1], "body")
    for parent in parents:
        if not parent:
            continue
        resolved = resolve_for_task(conn, parent, board=board)
        if resolved:
            register, ask = resolved
            # ``ask == ""`` means the parent chain bottomed out at the register itself:
            # this card is the fresh ask and the caller fills its own id in.
            return AskRef(register, ask or None, "parents")
    return None


def _resolve_single_id(
    conn: sqlite3.Connection, ref: str, *, source: str, board: Optional[str],
    find_card: Optional[Any],
) -> AskRef:
    """Resolve ``--serves t_x`` / an env value naming one card, into a full pair.

    Three readings, in the order that keeps the filer's intent intact:

    * the card carries a stamp - this work serves the ask that card serves;
    * the card is a register's designated child (or sits under one) - the pair is that
      register and the register child on the path;
    * the card IS a board's designated register - a new ask is being captured, so the
      ask half is left empty and the created card fills in its own id.
    """
    register = register_for_board(board) if board else None
    row = _row(conn, ref)
    if row is not None:
        stamped = parse_stamp(row[1])
        if stamped:
            return AskRef(stamped[0], stamped[1], source)
        if register and ref == register:
            return AskRef(register, None, "register")
        resolved = resolve_for_task(conn, ref, board=board)
        if resolved:
            reg, ask = resolved
            return AskRef(reg, ask or None, source)
        # A card that exists but serves nothing and hangs off nothing in service: naming
        # it is not evidence that the new card serves an ask. Report it unresolved rather
        # than inventing an ask (a warn on the create path, a finding in the roll-up).
        return AskRef(None, None, source, unresolved=ref)
    if register and ref == register:
        # Off-board probe failed but this board's register was named directly.
        return AskRef(register, None, "register")
    if find_card is not None:
        other = find_card(ref)
        if other:
            resolved = _resolve_off_board(other, ref)
            if resolved is not None:
                return resolved
    return AskRef(None, None, source, unresolved=ref)


def _resolve_off_board(board: str, ref: str) -> Optional[AskRef]:
    """Resolve a reference through another board's store, read-only."""
    conn = open_board_ro(board)
    if conn is None:
        return None
    try:
        row = _row(conn, ref)
        if row is None:
            return None
        stamped = parse_stamp(row[1])
        if stamped:
            return AskRef(stamped[0], stamped[1], "cross-board")
        register = register_for_board(board)
        if register and ref == register:
            return AskRef(register, None, "register")
        resolved = resolve_for_task(conn, ref, board=board)
        if resolved:
            reg, ask = resolved
            return AskRef(reg, ask or None, "cross-board")
    finally:
        _close(conn)
    return None


# ------------------------------------------------------------------ ask-home guard


class OperatorAskOffBoardError(ValueError):
    """A NEW operator ask was being created on a board other than its register's.

    See :func:`guard_new_ask_home`. The ASK-HOME rule: a card that IS an operator ask -
    one that will be stamped ``Operator-ask: <register>/<own id>`` - must be created on
    the register's own board, or the register's roll-up never sees it (the register
    reads references, and a reference is a stamp, not an edge). A card that INHERITS an
    existing ask id by lineage is not this case, and is never refused. Refused before
    any row is written.
    """


def register_board(register: str) -> Optional[str]:
    """The board ``register`` calls home, or ``None`` when no board designates it.

    A register IS a board's own designation (``board.json`` ``operator_register``), so the
    home is the board that designates this id - resolved from the register id alone across
    every registered board, never hardcoded, so a second register on another board works
    unchanged. An id that no board designates is not a register, so it has no home.
    """
    if not is_task_id(register):
        return None
    for slug in registered_boards():
        try:
            if register_for_board(slug) == register:
                return slug
        except Exception:  # pragma: no cover - a reader must never raise
            continue
    return None


def _norm_board(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    return value.strip().lower() or None


def guard_new_ask_home(
    ask_ref: Optional[AskRef], *, board: Optional[str], title: str,
    allow_off_board: bool = False, caller: Optional[str] = None,
) -> Optional[dict]:
    """Refuse a NEW operator ask filed off its register's board; return the hatch record.

    THE ASK-HOME RULE. A card that IS an operator ask - one whose create call resolves to
    an ask under a register, i.e. one that will be stamped
    ``Operator-ask: <register>/<own id>`` (an :class:`AskRef` whose ``ask`` half is empty,
    so the card fills in its own id) - MUST be created on that register's OWN board. A
    card that INHERITS an existing ask id by lineage is NOT gated: pr-evergreen,
    reconcile, hazard/census and the escalation machinery all file flow envelopes
    off-board by inheriting an ask, so only a NEW ask is this guard's business.

    Returns ``None`` when there is nothing to do - not a new ask, already on the home
    board, or the register's board cannot be told - so the seam is inert for every filing
    that is not an off-board new ask. Returns the event payload ``dict`` when the hatch
    admits a deliberate cross-board filing (the caller appends the ``task_events`` row, so
    the escape is on the record rather than silent). Raises
    :class:`OperatorAskOffBoardError` on a refusal, BEFORE any write.
    """
    if ask_ref is None or not ask_ref.register or ask_ref.ask:
        return None  # only a NEW ask (self-stamped) is gated
    register = ask_ref.register
    home = register_board(register)
    if home is None:
        return None  # the register's board cannot be told: nothing to compare against
    current = _norm_board(board)
    if current is None or current == _norm_board(home):
        return None
    record = {
        "caller": caller or "unknown",
        "board": current,
        "register": register,
        "register_board": home,
        "title": (title or "").strip(),
    }
    if allow_off_board or os.environ.get(ALLOW_ENV_VAR, "").strip() == "1":
        return record
    raise OperatorAskOffBoardError(
        "this card is a NEW operator ask and must be filed on the register's own "
        "board; filing it here would put the ask where its register's roll-up does "
        "not look.\n"
        f"  card title:       {record['title']}\n"
        f"  being created on: {current}\n"
        f"  register:         {register} (its board: {home})\n"
        f"  remedy:           re-file with `--board {home}`\n"
        "If the cross-board filing is deliberate, re-run with allow_off_board_ask=True "
        f"(tool) or {ALLOW_ENV_VAR}=1 (CLI); the filing is then recorded in the card's "
        "events rather than silently."
    )


# --------------------------------------------------------------- board stores/ro


def open_board_ro(board: str) -> Optional[sqlite3.Connection]:
    """A read-only connection to ``board``'s store, or ``None`` when it has no tasks.

    Read-only is the contract: a roll-up (or a cross-board reference probe) must never
    initialize, migrate or repair somebody else's board.
    """
    from hermes_cli import kanban_db as kb

    try:
        path = kb.kanban_db_path(board=board)
    except Exception:
        return None
    if not Path(path).exists():
        return None
    try:
        conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    try:
        conn.text_factory = kb._lossy_text
    except Exception:  # pragma: no cover - defensive, mirrors kanban_db_connect
        pass
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()
    except sqlite3.Error:
        _close(conn)
        return None
    if row is None:
        _close(conn)
        return None
    return conn


def _close(conn: sqlite3.Connection) -> None:
    try:
        conn.close()
    except Exception:  # pragma: no cover - defensive
        pass


def registered_boards(*, include_archived: bool = True) -> list[str]:
    """Every registered board slug; ``default`` first, then by name.

    ``include_archived`` defaults to TRUE, unlike ``kanban boards list``: a board-level
    ``archived`` flag in ``board.json`` does not mean its cards are gone (measured
    2026-09-27: ``research`` and ``financially`` both carry ``archived: true`` while holding
    229 and 405 live cards), and a roll-up that skipped them would report a partial tree as
    if it were the whole one - the exact failure this feature exists to end. A board with no
    readable store is skipped by :func:`open_board_ro` anyway.
    """
    from hermes_cli import kanban_db as kb

    try:
        entries = kb.list_boards(include_archived=include_archived)
    except Exception:
        return [kb.DEFAULT_BOARD]
    return [str(e.get("slug")) for e in entries if e.get("slug")]


def find_card_board(task_id: str, *, boards: Optional[Iterable[str]] = None) -> Optional[str]:
    """The slug of the board holding ``task_id``, or ``None``. Broadest probe: all boards."""
    for slug in (boards if boards is not None else registered_boards()):
        conn = open_board_ro(slug)
        if conn is None:
            continue
        try:
            if _row(conn, task_id) is not None:
                return slug
        finally:
            _close(conn)
    return None


# ------------------------------------------------------------------- dispatcher


def env_ref_for_worker(board: Optional[str], task_id: str, body: Optional[str] = None) -> Optional[str]:
    """The ``HERMES_KANBAN_OPERATOR_ASK`` value for a worker running ``task_id``, or ``None``.

    ``<register>/<ask>`` for a card in service of an ask; ``<register>`` alone for a card
    that IS the register (every card that worker files is a fresh ask under it, and the
    create path fills in the ask half). ``None`` when the card serves no ask - which is
    the common case, and the reason this computes a value instead of always setting one:
    an env var that is always present would stamp every card on the host.

    The dispatcher calls this once per spawn, so the walk is bounded and gated: a card
    that already carries a stamp (every card born after this feature) costs one board
    read and no walk at all.
    """
    if not task_id:
        return None
    conn = open_board_ro(board) if board else None
    try:
        stamped = parse_stamp(body)
        if not stamped and conn is not None:
            row = _row(conn, task_id)
            stamped = parse_stamp(row[1]) if row is not None else None
        register = register_for_board(board)
        if register and task_id == register:
            return register
        if stamped:
            return _render(stamped[0], stamped[1])
        if conn is not None:
            resolved = resolve_for_task(conn, task_id, board=board)
            if resolved:
                reg, ask = resolved
                return _render(reg, ask) if ask else reg
    finally:
        if conn is not None:
            _close(conn)
    return None


# ----------------------------------------------------------------------- rollup


@dataclass
class RollupCard:
    board: str
    id: str
    title: str
    status: str
    assignee: Optional[str]
    ask: Optional[str]
    via: str
    depth: int = 1
    created_at: Optional[int] = None


@dataclass
class RollupAsk:
    id: str
    title: str
    status: str
    assignee: Optional[str]
    board: Optional[str]
    cards: list[RollupCard] = field(default_factory=list)


@dataclass
class Rollup:
    register: str
    register_board: Optional[str]
    register_title: str
    register_status: str
    register_assignee: Optional[str]
    boards: list[str]
    by_board: dict[str, int] = field(default_factory=dict)
    by_status: dict[str, int] = field(default_factory=dict)
    asks: list[RollupAsk] = field(default_factory=list)
    cards: list[RollupCard] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)


def _cards_of_board(slug: str) -> Optional[dict[str, dict]]:
    """Every live card of one board: ``id -> row dict``, or ``None`` if unreadable."""
    conn = open_board_ro(slug)
    if conn is None:
        return None
    try:
        try:
            rows = conn.execute(
                "SELECT id, title, status, assignee, created_at, body FROM tasks "
                "WHERE status != 'archived'"
            ).fetchall()
        except sqlite3.Error:
            rows = conn.execute("SELECT id, title, status, assignee, body FROM tasks").fetchall()
    finally:
        _close(conn)
    out: dict[str, dict] = {}
    for row in rows:
        keys = row.keys()
        out[row["id"]] = {
            "board": slug,
            "id": _text(row["id"]) or "",
            "title": _text(row["title"]) or "",
            "status": _text(row["status"]) or "",
            "assignee": _text(row["assignee"]),
            "created_at": row["created_at"] if "created_at" in keys else None,
            "stamp": parse_stamp(row["body"]),
        }
    return out


def _links_of_board(slug: str) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """``(children, parents)`` maps for one board's dependency edges."""
    conn = open_board_ro(slug)
    if conn is None:
        return {}, {}
    try:
        rows = conn.execute("SELECT parent_id, child_id FROM task_links").fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        _close(conn)
    children: dict[str, list[str]] = {}
    parents: dict[str, list[str]] = {}
    for row in rows:
        parent, child = row["parent_id"], row["child_id"]
        if parent and child:
            children.setdefault(parent, []).append(child)
            parents.setdefault(child, []).append(parent)
    return children, parents


def rollup(register: Optional[str] = None, *, board: Optional[str] = None) -> Rollup:
    """Everything in service of ``register`` (or ``board``'s designated register).

    The graph is closed under BOTH relations, per board:

    * **references** - any card whose stamp names this register, on any board, whether or
      not an edge could ever exist between it and the register's board;
    * **edges** - descendants, board by board, of anything already in service.

    A card with no stamp that hangs off a stamped card is still in service (that is the
    "deep chains" half of the defect), so its ask is inherited by walking its own parent
    chain. A stamp naming a register or ask that exists on no board is reported in
    ``unresolved`` rather than dropped: a reference nobody can follow is a finding.
    """
    from hermes_cli import kanban_db as kb

    slug_for_value = board if board else None
    root = register.strip() if isinstance(register, str) and register.strip() else None
    if root is None:
        root = register_for_board(slug_for_value or kb.get_current_board())
    if root is None:
        raise ValueError(
            "no operator register to roll up: pass a register id "
            "(`hermes kanban rollup <task-id>`) or designate one for the board with "
            "`hermes kanban boards set-operator-register <task-id>`"
        )
    if not is_task_id(root):
        raise ValueError(f"not a card id: {root!r}")

    boards = registered_boards()
    stores: dict[str, dict[str, dict]] = {}
    children: dict[str, dict[str, list[str]]] = {}
    parents: dict[str, dict[str, list[str]]] = {}
    for slug in boards:
        cards = _cards_of_board(slug)
        if cards is None:
            continue
        stores[slug] = cards
        children[slug], parents[slug] = _links_of_board(slug)

    # Locate the register itself (and its own row's state/owner for the header).
    register_board: Optional[str] = None
    register_row: Optional[dict] = None
    for slug, cards in stores.items():
        if root in cards:
            register_board, register_row = slug, cards[root]
            break
    if register_row is None and slug_for_value and slug_for_value in stores and root in stores.get(slug_for_value, {}):
        register_board = slug_for_value

    # In service: stamps first (the cross-board reach), then edge closure per board.
    in_service: dict[tuple[str, str], str] = {}
    unresolved: list[str] = []
    for slug, cards in stores.items():
        for card_id, card in cards.items():
            stamp = card["stamp"]
            if not stamp:
                continue
            if stamp[0] != root:
                continue
            in_service[(slug, card_id)] = "ref"
    # The register's own card is in service (it is the root of the tree).
    if register_board and root in stores.get(register_board, {}):
        in_service.setdefault((register_board, root), "register")

    frontier = list(in_service)
    walked: set[tuple[str, str]] = set()
    while frontier:
        slug, card_id = frontier.pop()
        if (slug, card_id) in walked:
            continue
        walked.add((slug, card_id))
        for child in children.get(slug, {}).get(card_id, []):
            # ``child`` is a card id, so the store (keyed by card id) is probed by it
            # alone. A stale edge to a card this board no longer holds is skipped, not
            # invented as a row.
            if child in stores.get(slug, {}):
                if (slug, child) not in in_service:
                    in_service[(slug, child)] = "edge"
                    frontier.append((slug, child))

    # A stamp naming a register/ask that no board holds is a finding, not a silent drop.
    all_ids = {card_id for cards in stores.values() for card_id in cards}
    for cards in stores.values():
        for card in cards.values():
            stamp = card["stamp"]
            if stamp and stamp[0] == root and stamp[1] not in all_ids:
                unresolved.append(_render(stamp[0], stamp[1]))
    unresolved = sorted(set(unresolved))

    # Ask assignment: the card's own stamp, else the ask of the nearest stamped/register
    # ancestor on its own board (a legacy chain), else the register's child on that path.
    asks: dict[str, RollupAsk] = {}
    memo: dict[tuple[str, str], Optional[str]] = {}

    def ask_of(slug: str, card_id: str, depth: int = 0) -> Optional[str]:
        key = (slug, card_id)
        if key in memo:
            return memo[key]
        if depth > MAX_ANCESTOR_WALK:
            return None
        memo[key] = None  # cycle guard: a corrupt chain resolves to nothing, never loops
        card = stores.get(slug, {}).get(card_id)
        if card is None:
            return None
        stamp = card["stamp"]
        if stamp and stamp[0] == root:
            memo[key] = stamp[1]
            return stamp[1]
        for parent in parents.get(slug, {}).get(card_id, []):
            if parent == root:
                memo[key] = card_id
                return card_id
            answer = ask_of(slug, parent, depth + 1)
            if answer:
                memo[key] = answer
                return answer
        return None

    # Depth within the ask group, from the edges only (a ref-attached card is depth 1).
    child_of: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for (slug, card_id), _via in in_service.items():
        for child in children.get(slug, {}).get(card_id, []):
            if (slug, child) in in_service:
                child_of.setdefault((slug, card_id), []).append((slug, child))

    result = Rollup(
        register=root,
        register_board=register_board,
        register_title=(register_row or {}).get("title", ""),
        register_status=(register_row or {}).get("status", "MISSING"),
        register_assignee=(register_row or {}).get("assignee"),
        boards=sorted(stores.keys()),
    )
    for (slug, card_id), via in sorted(in_service.items()):
        card = stores[slug][card_id]
        ask = ask_of(slug, card_id)
        if card_id == root:
            ask = root
        entry = asks.get(ask or "")
        if entry is None:
            ask_card = stores.get(slug, {}).get(ask or "")
            ask_board = slug if ask_card else None
            if ask and not ask_card:
                for other, cards in stores.items():
                    if ask in cards:
                        ask_card, ask_board = cards[ask], other
                        break
            entry = RollupAsk(
                id=ask or "",
                title=(ask_card or {}).get("title", "(ask card not on any board)"),
                status=(ask_card or {}).get("status", "MISSING"),
                assignee=(ask_card or {}).get("assignee"),
                board=ask_board,
            )
            asks[ask or ""] = entry
        if card_id != root:
            entry.cards.append(RollupCard(
                board=slug, id=card_id, title=card["title"], status=card["status"],
                assignee=card["assignee"], ask=ask, via=via, created_at=card["created_at"],
            ))

    # Sort each ask's cards into a depth-first tree: the ask first, then its children by
    # edge, then anything attached by reference (which has no edge to follow).
    ordered: list[RollupCard] = []
    for ask_id in sorted(asks, key=lambda a: (a == root, a)):
        entry = asks[ask_id]
        by_id = {(c.board, c.id): c for c in entry.cards}
        root_card = by_id.get((entry.board or "", ask_id))
        roots: list[tuple[str, str]] = []
        if root_card is not None:
            roots.append((root_card.board, root_card.id))
        # Edges to follow inside this ask group. Named apart from the per-board ``children``
        # map above: this one is keyed by (board, id) and holds only in-service cards.
        edges = child_of
        visited: set[tuple[str, str]] = set()

        def walk(key: tuple[str, str], depth: int, via: str) -> None:
            if key in visited or depth > 64:
                return
            visited.add(key)
            card = by_id.get(key)
            if card is None:
                return
            card.depth = depth
            card.via = via
            ordered.append(card)
            for child in sorted(edges.get(key, [])):
                walk(child, depth + 1, "edge")

        for key in roots:
            walk(key, 1, "ask")
        # Cards reached only by REFERENCE (another board, where an edge could not exist):
        # the rows the `~` marker flags. Walk the top-most of them FIRST, so a card that
        # hangs off one by a real, in-board EDGE keeps its edge - and its depth - instead
        # of being re-labelled a reference by the order ids happen to sort in. Measured
        # 2026-09-27: walking plain id order marked an ops subtree's second level `ref`
        # at depth 1, which is the one thing this marker must never claim.
        reference_roots = [
            key for key in by_id
            if key not in visited
            and not any(
                (key[0], parent) in by_id
                for parent in parents.get(key[0], {}).get(key[1], [])
            )
        ]
        for key in sorted(reference_roots):
            walk(key, 1, "ref")
        for key in sorted(by_id):
            if key not in visited:
                # A cycle among reference-attached cards (a corrupt chain): still shown,
                # and the walk's own visited guard keeps it finite.
                walk(key, 1, "ref")
        entry.cards = [c for c in ordered if c.ask == ask_id]
        ordered = []

    result.asks = [asks[a] for a in sorted(asks, key=lambda a: (a == root, a))]
    result.cards = [c for c in result.asks for c in c.cards]
    result.unresolved = unresolved
    for card in result.cards:
        result.by_board[card.board] = result.by_board.get(card.board, 0) + 1
        result.by_status[card.status] = result.by_status.get(card.status, 0) + 1
    return result


def _fmt_board_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none"


def render_rollup(result: Rollup, *, width: int = 64) -> str:
    """The human view: one command, everything in service, with state and owner."""
    lines: list[str] = []
    board = result.register_board or "?"
    lines.append(
        f"Operator-ask roll-up — register {result.register} [{result.register_status}] "
        f"on {board}, owner {result.register_assignee or '-'}"
    )
    if result.register_title:
        lines.append(f"  {result.register_title}")
    total_asks = len([a for a in result.asks if a.id != result.register])
    lines.append(
        f"  {len(result.cards)} card(s) in service, {total_asks} ask(s), "
        f"boards scanned: {len(result.boards)}"
    )
    lines.append(f"  by board:  {_fmt_board_counts(result.by_board)}")
    lines.append(f"  by status: {_fmt_board_counts(result.by_status)}")
    if result.unresolved:
        lines.append(
            f"  unresolved reference(s): {len(result.unresolved)} — "
            + ", ".join(result.unresolved[:6])
            + (" ..." if len(result.unresolved) > 6 else "")
        )
    for ask in result.asks:
        if ask.id == result.register:
            continue
        if ask.id:
            lines.append("")
            lines.append(
                f"ask {ask.id} [{ask.status}] {ask.assignee or '-'}"
                + (f" on {ask.board}" if ask.board else " (ask card not on any board)")
                + f" — {ask.title}"
            )
        for card in ask.cards:
            indent = "  " * (card.depth - 1)
            title = card.title if len(card.title) <= width else card.title[: width - 1] + "…"
            # ``~`` marks a row reached by REFERENCE (no edge could exist: another board).
            marker = "~" if card.via == "ref" else " "
            lines.append(
                f"  {indent}{marker} {card.board:9s} {card.id}  {card.status:10s} "
                f"{card.assignee or '-':16s} {title}"
            )
    if not result.cards:
        lines.append("")
        lines.append("  (nothing in service of this register yet)")
    return "\n".join(lines)


def rollup_json(result: Rollup) -> dict:
    """The machine view of the same walk."""
    return {
        "register": result.register,
        "register_board": result.register_board,
        "register_title": result.register_title,
        "register_status": result.register_status,
        "register_assignee": result.register_assignee,
        "boards_scanned": result.boards,
        "cards": len(result.cards),
        "asks": len([a for a in result.asks if a.id != result.register]),
        "by_board": result.by_board,
        "by_status": result.by_status,
        "unresolved": result.unresolved,
        "groups": [
            {
                "ask": a.id,
                "ask_board": a.board,
                "title": a.title,
                "status": a.status,
                "assignee": a.assignee,
                "cards": [
                    {
                        "board": c.board, "id": c.id, "title": c.title, "status": c.status,
                        "assignee": c.assignee, "via": c.via, "depth": c.depth,
                    }
                    for c in a.cards
                ],
            }
            for a in result.asks if a.id != result.register
        ],
    }


def dumps(result: Rollup) -> str:
    return json.dumps(rollup_json(result), indent=2)
