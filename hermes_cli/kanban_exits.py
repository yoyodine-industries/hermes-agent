"""The status × door matrix: every status a card can hold, and the act that exits it.

WHY THIS FILE EXISTS. Hermes shipped a whole class of cards that no seat could move: a
lane lead could be handed a ruling on a card sitting in ``triage`` or ``scheduled``, and the
only doors bound to those statuses were operator verbs (``hermes kanban specify``,
``hermes kanban unblock``) plus a dashboard drag — none of which an agent could reach. The
cards sat, and the lane reported "blocked" on a board that was not blocked, it was un-doored.
``t_08079e45`` ruled the class fixed by ONE seat-scoped exit door (``kanban_adjudicate``) plus
this enumeration, so the next status added to the workflow cannot be born without a door.

This module EXECUTES NOTHING. It is the single place that answers "what can leave status S,
and from which seat", read by three consumers that must not drift:

* ``hermes_cli/kanban_adjudicate.py`` — ``ROUTES`` must equal :data:`DOOR_SOURCES` at import,
  so a new status whose door the door does not route cannot even be imported.
* ``hermes_cli/kanban_db.py`` — :func:`owning_door`, so a refused completion names the act
  that owns the status instead of telling the reader the card "left a completable status".
* ``tests/hermes_cli/test_kanban_exits.py`` — the rail, which fails when a status has no
  door, when a door names a tool that does not exist, or when a tool that moves a card is
  missing from the matrix.

Seats, not agents: :data:`WORKER` is a card worker's own session, :data:`ORCHESTRATOR` a
session holding ``kanban_list``/``kanban_unblock``, :data:`HUMAN` a person at a CLI or the
dashboard. A status whose only doors are human is legal, but the rail resolves the named CLI
verb against the real parser, so "the operator can do it" cannot be wishful.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from hermes_cli.kanban_pr_acceptance_store import _COMPLETABLE
from hermes_cli.kanban_workflow import DEFAULT_STATUSES

__all__ = [
    "DOOR_SOURCES",
    "EXITS",
    "HUMAN",
    "ORCHESTRATOR",
    "WORKER",
    "Exit",
    "owning_door",
    "resolve_kernel",
]

#: The three seats a door can belong to.
WORKER = "worker"
ORCHESTRATOR = "orchestrator"
HUMAN = "human"


@dataclass(frozen=True)
class Exit:
    """One act that can move a card out of a status.

    ``verb`` is a registered agent tool name (``kanban_complete``) or a human surface
    spelled ``cli:<subcommand>``. ``kernel`` names the kernel function the act ultimately
    writes through; the rail resolves it with :func:`resolve_kernel`, so a door cannot cite
    a function that was renamed out from under it.
    """

    verb: str
    seat: str
    kernel: str = ""
    note: str = ""


def _exits() -> dict[str, tuple[Exit, ...]]:
    return {
        # -- the class this card exists for -------------------------------------
        # Neither status has an agent tool of its own; before `kanban_adjudicate` a worker
        # holding one could only escalate and wait for a human.
        "triage": (
            Exit("kanban_adjudicate", WORKER, "adjudicate",
                 "promote -> todo/ready, or close -> done, with the calling worker's run as provenance"),
            Exit("cli:specify", HUMAN, "specify_triage_task",
                 "writes the spec fields and moves triage -> todo"),
        ),
        "todo": (
            Exit("kanban_adjudicate", WORKER, "adjudicate",
                 "promote -> ready (or stay todo when a parent is open), or close -> done"),
            Exit("cli:promote", HUMAN, "promote_task", "todo -> ready when every parent is done"),
        ),
        "scheduled": (
            Exit("kanban_adjudicate", WORKER, "adjudicate",
                 "releases the schedule and closes, or releases it and promotes"),
            Exit("kanban_unblock", ORCHESTRATOR, "unblock_task", "returns scheduled -> ready/todo"),
            Exit("cli:unblock", HUMAN, "unblock_task", "the operator's release door"),
        ),
        # -- the ordinary lifecycle --------------------------------------------
        # `_COMPLETABLE`: complete_task accepts these four, which is also why they are NOT
        # door sources (a second door onto `complete` would be two spellings of one act).
        "ready": (
            Exit("kanban_complete", WORKER, "complete_task"),
            Exit("kanban_request_review", WORKER, "request_review"),
            Exit("kanban_block", WORKER, "block_task"),
            Exit("kanban_schedule", WORKER, "schedule_task"),
            Exit("kanban_close_sibling", WORKER, "complete_task",
                 "closes a SIBLING card as delegated work (the card must still be completable)"),
        ),
        "running": (
            Exit("kanban_complete", WORKER, "complete_task"),
            Exit("kanban_request_review", WORKER, "request_review", "the normal hand-off"),
            Exit("kanban_request_changes", WORKER, "request_changes",
                 "only from a claimed review run; returns the card to its implementer"),
            Exit("kanban_block", WORKER, "block_task"),
            Exit("kanban_schedule", WORKER, "schedule_task"),
            Exit("kanban_close_sibling", WORKER, "complete_task"),
        ),
        "blocked": (
            Exit("kanban_complete", WORKER, "complete_task", "a blocked card may still be finished"),
            Exit("kanban_unblock", ORCHESTRATOR, "unblock_task"),
            Exit("cli:unblock", HUMAN, "unblock_task"),
            Exit("kanban_close_sibling", WORKER, "complete_task"),
        ),
        "review": (
            Exit("kanban_complete", WORKER, "complete_task", "approve"),
            Exit("kanban_request_changes", WORKER, "request_changes", "send back to the implementer"),
            Exit("kanban_close_sibling", WORKER, "complete_task"),
        ),
        # -- terminal: human-only, and the rail proves the remedy is real --------
        "done": (
            Exit("cli:reopen", HUMAN, "reopen_task", "done -> todo; the only exit Hermes offers"),
        ),
        "archived": (
            Exit("cli:reopen", HUMAN, "reopen_task", "archived -> todo"),
        ),
    }


#: status -> the acts that can leave it. Keys MUST cover :data:`DEFAULT_STATUSES`.
EXITS: dict[str, tuple[Exit, ...]] = _exits()

#: The statuses the seat-scoped door owns: not completable, not terminal. This is the
#: derivation, not a hand-kept list — a status that becomes completable (or terminal) leaves
#: the door's remit automatically, and one that becomes neither must gain a route.
DOOR_SOURCES = frozenset(DEFAULT_STATUSES) - set(_COMPLETABLE) - {"done", "archived"}


def resolve_kernel(name: str) -> Optional[object]:
    """The kernel callable an :class:`Exit` names, or ``None`` when it does not resolve.

    Late imports on purpose: ``kanban_db`` imports this module for :func:`owning_door`, and
    the door imports both, so resolving at call time is what keeps the three importable.
    """
    if not name:
        return None
    from hermes_cli import kanban_adjudicate, kanban_db

    for module in (kanban_db, kanban_adjudicate):
        fn = getattr(module, name, None)
        if fn is not None:
            return fn
    return None


def owning_door(status: str) -> str:
    """The act that exits ``status``, spelled the way a refusal can tell the reader to use it.

    Ordered by seat: a worker's own door first (the seat that reads most refusals), then the
    orchestrator's, then the operator's. ``""`` when the status is unknown to the matrix —
    callers print their own fallback rather than inventing an act.
    """
    exits = EXITS.get(status) or ()
    for seat in (WORKER, ORCHESTRATOR, HUMAN):
        for exit_ in exits:
            if exit_.seat != seat:
                continue
            if exit_.verb.startswith("cli:"):
                return f"`hermes kanban {exit_.verb[4:]}`"
            return f"`{exit_.verb}`"
    return ""
