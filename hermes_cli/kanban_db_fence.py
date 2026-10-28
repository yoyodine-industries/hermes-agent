"""The Kanban write fence: ONE subject, ONE verdict, ONE refusal (card t_e88054a6).

WHY THIS MODULE EXISTS
----------------------
Three arms decide whether this process may write a board:

* the in-process ContextVar (``_DELEGATED_CHILD_CONTEXT``),
* the inherited ``HERMES_DELEGATED_CHILD_CONTEXT`` marker,
* the PROVENANCE arm (ruling t_fcf7a321) — an ancestor process holds a LIVE claim.

The first two are answered by ``kanban_path_is_fenced``; the third by
``ancestor_owns_live_kanban_claim``. Before this module the two answers lived in DIFFERENT
places and were consulted SEPARATELY: every durable write seam asked only the two cooperative
arms, while the CLI's ``--board`` fast-fail also asked provenance. A shell that stripped the
marker (``env -u``) was therefore refused at the CLI and ADMITTED at the durable seam behind it
— one fence, two answers. Measured at the pre-fix base:
``tests/hermes_cli/test_kanban_fence_provenance.py::test_provenance_arm_also_guards_the_durable_db_seam``
prints ``ADMITTED``: the write lands.

Here all three are asked by ONE function (:func:`fence_verdict`) about ONE subject
(:func:`fence_subject`), and every seam — :func:`hermes_cli.kanban_db_connect.write_txn`, the
board-metadata guards, ``designate_priority``/``revoke_priority_designation``, the tranche-guard
wiring and the CLI fast-fail — reaches the decision through those two, never through an arm.

THE SUBJECT IS THE WRITE, NEVER A NAME (rulings t_57e33100, t_63a0c2d9, t_ecd51c9c)
---------------------------------------------------------------------------------
A caller's positional path let a board NAME stand in for the subject, and
``kanban_db_path(board=<name>)`` answers the ENV-PINNED store whenever a worker's pin is
authoritative: the verdict then spoke about a store that was neither the write's nor the one the
invocation named. The subject is now what the write LANDS on — the connection's own file
(``kanban_db_connect._main_db_file``, the same file ``write_txn`` fences) or the board directory a
metadata write lands in. A name is consulted ONLY when the caller has no store to offer, and only
when it RESOLVES to a registered board: a name that names no board is no board for a verdict
either, so the caller's own ``board '<x>' does not exist`` is what it reads instead of this fence
speaking for (and masking) a board it never touched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

#: The COOPERATIVE arms' refusal (the ContextVar and the inherited marker): the caller was handed
#: the delegated-child state and kept it.
COOPERATIVE_REFUSAL_MESSAGE = "delegate_task child contexts cannot mutate Kanban tasks or boards"

#: The PROVENANCE arm's refusal. It lives next to the arm that raises it
#: (``agent.delegation_context.PROVENANCE_REFUSAL_MESSAGE``) and names the ACTUAL requirement plus
#: the ONE sanctioned release, because a refused worker that is merely told to stop reaches for an
#: ``env -u`` strip that would not have released it (ruling t_fcf7a321, Decision 1(b)).


def _resolve(path: object) -> Optional[Path]:
    """*path* as an absolute subject; ``None`` when it cannot be told. Never raises.

    An unresolvable subject is NO subject: the fence may only add a refusal it can prove, so a
    path it cannot read is not a board it can fence.
    """
    if path is None or path == "":
        return None
    try:
        return Path(str(path)).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _named_board_store(board: Optional[str]) -> Optional[Path]:
    """The store a NAMED board resolves to, or ``None`` when the name is no board.

    ``--board`` / ``board=`` names the board a command addresses; the ambient chain
    (``HERMES_KANBAN_DB`` -> ``HERMES_KANBAN_BOARD`` -> ``<kanban>/current``) resolves only the
    board that is ACTIVE in this process. Judging against the ambient board refuses a mutation
    targeting a board this lineage holds no claim on, and lets the verdict flip when
    ``<kanban>/current`` moves (card t_63a0c2d9).

    Existence is asked of the board REGISTRY first, because the store path cannot answer it: a
    worker's env pin legitimately overrides explicit board intent
    (``_explicit_board_intent_pinned``) and would hand back the PINNED board for a name that
    resolved elsewhere. THREE OUTCOMES, NOT TWO (ruling t_ecd51c9c): the fence may allow or deny
    the board the caller NAMED, never substitute another, so an unregistered name answers ``None``
    — no verdict — and the caller's own resolution produces the honest error. Falling back to the
    AMBIENT board did exactly that substitution: it judged the invocation against a board it never
    named, over-refusing a caller descended from a live worker there and MASKING the CLI's own
    ``board '<x>' does not exist`` (measured on the sdlc birth node: run 20261008-0844, gateway
    caller -> honest error, the item BIRTHS; run 20261008-0958, worker caller -> the fence message;
    cron 16b8c8db4ed7 failure_streak=10).
    """
    from hermes_cli import kanban_db as _kb

    try:
        slug = _kb._normalize_board_slug(board)
    except ValueError:  # a malformed slug is the caller's own error, not a fence subject
        return None
    try:
        if not slug or (slug != _kb.DEFAULT_BOARD and not _kb.board_exists(slug)):
            return None
        return _resolve(_kb.kanban_db_path(board=slug))
    except Exception:  # health: allow BLE001 -- fail-open: a resolver that raises would mask the caller's own board error
        # The subject resolver must never raise out of the seam: a path that cannot be told is no
        # subject (no verdict), never an exception a caller has to interpret.
        return None


def fence_subject(*, conn=None, store=None, board=None, root=None) -> Optional[Path]:
    """THE ONE SUBJECT: the store this write LANDS ON; a name only as the last fallback.

    *conn* — the connection the write goes through. Its file is read with
    ``kanban_db_connect._main_db_file``, the same expression ``write_txn`` fences, so the
    pre-check and the transaction can never speak about different stores.
    *store* — an explicit store path (a seam that has no connection yet).
    *root* — the board directory a board-METADATA write lands in (no store involved).
    *board* — a NAME; used only when nothing above could tell a store, and only when it resolves.
    With nothing given the subject is the ACTIVE board's store, which is what a caller holding no
    handle writes.

    A connection whose store cannot be told (an in-memory DB) falls through to the name it was
    given: the row it would write still NAMES that board.
    """
    if conn is not None:
        from hermes_cli.kanban_db_connect import _main_db_file

        subject = _resolve(_main_db_file(conn))
        if subject is not None:
            return subject
    if store is not None:
        return _resolve(store)
    if root is not None:
        return _resolve(root)
    if board is not None:
        return _named_board_store(board)
    from hermes_cli import kanban_db as _kb

    return _resolve(_kb.kanban_db_path())


def fence_verdict(subject: Optional[Path]) -> Optional[str]:
    """THE ONE VERDICT: all three arms, asked about ONE subject.

    ``None`` admits the write; otherwise the refusal the answering arm raises, in its own words —
    the cooperative arms say what the caller carried, the provenance arm says what the ancestry
    proves and names the one sanctioned release, so a reader is never told to strip a marker that
    would not have released it.

    No subject admits: a fence that cannot name what is being written cannot prove a refusal, and
    a silent refusal of nothing is how a fence starts refusing the wrong board (ruling t_ecd51c9c).
    """
    if subject is None:
        return None
    from agent.delegation_context import (
        PROVENANCE_REFUSAL_MESSAGE,
        ancestor_owns_live_kanban_claim,
        kanban_path_is_fenced,
    )

    if kanban_path_is_fenced(subject):
        return COOPERATIVE_REFUSAL_MESSAGE
    if ancestor_owns_live_kanban_claim(subject):
        return PROVENANCE_REFUSAL_MESSAGE
    return None


def is_delegated_child_mutation(**subject_kwargs) -> bool:
    """The bool door: the ONE verdict, for callers that report a refusal in their own words.

    The CLI fast-fail and ``kanban gates reconcile`` print an explanation and an exit code rather
    than letting a ``PermissionError`` escape; they ask here so their verdict is the durable
    seam's verdict, not a second opinion about the same write. Subject keywords are
    :func:`fence_subject`'s.
    """
    return fence_verdict(fence_subject(**subject_kwargs)) is not None


def _assert_not_delegated_child_mutation(*, conn=None, store=None, board=None, root=None) -> None:
    """The raising door: every durable write seam refuses here before touching durable state.

    The write door is the trust boundary (a child can shell out or import the DB module; the CLI
    fast-fail is UX), so the refusal is a ``PermissionError`` raised from inside the seam that
    writes. Subject keywords are :func:`fence_subject`'s — never a bare positional path, which is
    how a name or a pinned store used to become the subject by accident.
    """
    reason = fence_verdict(fence_subject(conn=conn, store=store, board=board, root=root))
    if reason is not None:
        raise PermissionError(reason)
