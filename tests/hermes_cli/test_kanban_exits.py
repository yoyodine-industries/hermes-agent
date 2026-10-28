"""The status × door rail (card ``t_012fb74e``): no status may be born without a door.

WHY THIS FILE IS THE POINT OF THE CARD. The defect that produced it was not a bug in one
status transition — it was that ``triage`` and ``scheduled`` had NO act a seat could reach, and
nothing in the codebase noticed. A fix that only adds the door re-creates the defect the day
someone adds a status. So this file asserts the MATRIX, not the door:

* every status in ``DEFAULT_WORKFLOW`` (+ ``archived``) has at least one exit;
* every status has a machine exit (worker or orchestrator) unless it is one of the two declared
  human-only terminal statuses, whose CLI remedy must exist in the REAL parser;
* every exit names a tool that is registered, or a kernel function that resolves;
* every worker-seat door is a tool the worker's own toolset actually serves, gated by
  ``_check_kanban_mode`` (an unregistered tool is not a door);
* every tool the kanban toolset exposes is either in the matrix or explicitly declared as a
  non-exit — so the next tool that moves a card cannot be added without a decision;
* the door's ``ROUTES`` equal the derived ``DOOR_SOURCES``.

The last one is the rail with teeth: ``ROUTES`` is checked against ``DOOR_SOURCES`` at import
(``kanban_adjudicate`` raises), which is what makes a new status un-importable rather than
merely un-doored.
"""
from __future__ import annotations

import argparse

import pytest

from hermes_cli.kanban_exits import (
    DOOR_SOURCES,
    EXITS,
    HUMAN,
    ORCHESTRATOR,
    WORKER,
    resolve_kernel,
)
from hermes_cli.kanban_workflow import DEFAULT_STATUSES

#: Statuses whose only doors are a human at the CLI. Declared, not inferred: the rail asserts
#: this is the COMPLETE set, so a third status quietly losing its machine door turns red.
HUMAN_ONLY_STATUSES = frozenset({"done", "archived"})

#: Tools that annotate or read and never move a card. Explicit so that adding a tool which
#: does move one — and forgetting to place it in the matrix — fails the both-way test.
NOT_A_STATUS_EXIT = frozenset({
    "kanban_show",          # read
    "kanban_list",          # read (orchestrator)
    "kanban_heartbeat",     # liveness only
    "kanban_comment",       # annotates
    "kanban_attach",        # annotates
    "kanban_attach_url",    # annotates
    "kanban_attachments",   # read
    "kanban_create",        # mints a card; exits nothing
    "kanban_link",          # edges only
    "kanban_record_landing_verdict",  # a verdict on a DECLARING card; moves nothing
})


def _kanban_tools():
    """Import the tool module (registration is a side effect) and return its tables."""
    from tools.registry import invalidate_check_fn_cache

    from tools import kanban_tools as kt

    invalidate_check_fn_cache()
    return kt


def _tool_names(kt) -> set[str]:
    return {name for name, _schema, _handler, _emoji in kt._TOOLS}


def _cli_verbs() -> set[str]:
    """The verbs ``hermes kanban`` actually registers, read from the real parser."""
    from hermes_cli import kanban_parser

    root = argparse.ArgumentParser()
    kanban = kanban_parser.build_parser(root.add_subparsers())
    actions = [a for a in kanban._actions if isinstance(a, argparse._SubParsersAction)]
    assert actions, "the kanban parser declares no subcommands"
    return set(actions[0].choices)


# --------------------------------------------------------------------------- coverage


def test_every_status_has_a_door():
    """The whole matrix: a status with no exit is the defect this card closes."""
    assert set(EXITS) == set(DEFAULT_STATUSES)

    empty = [status for status, exits in EXITS.items() if not exits]
    assert empty == [], f"statuses with no exit: {empty}"


def test_every_status_has_a_machine_exit_except_the_declared_human_only_ones():
    """A seat must be able to act without a human, unless the status is terminal."""
    machine = {
        status: [e for e in exits if e.seat in (WORKER, ORCHESTRATOR)]
        for status, exits in EXITS.items()
    }
    human_only = {status for status, exits in machine.items() if not exits}

    assert human_only == set(HUMAN_ONLY_STATUSES), (
        "human-only statuses changed: "
        f"unexpected={sorted(human_only - HUMAN_ONLY_STATUSES)} "
        f"newly-machined={sorted(HUMAN_ONLY_STATUSES - human_only)}"
    )


def test_the_door_owns_exactly_the_derived_statuses():
    """``DOOR_SOURCES`` is a derivation, not a hand-kept list."""
    from hermes_cli.kanban_pr_acceptance_store import _COMPLETABLE

    assert DOOR_SOURCES == frozenset(DEFAULT_STATUSES) - set(_COMPLETABLE) - HUMAN_ONLY_STATUSES
    assert DOOR_SOURCES == frozenset({"triage", "todo", "scheduled"})


def test_the_door_routes_cover_every_source_and_nothing_else():
    """Import-time parity with the door; also proves the door module loads at all."""
    from hermes_cli.kanban_adjudicate import ROUTES

    assert set(ROUTES) == DOOR_SOURCES


def test_every_door_source_has_a_worker_door():
    """The class guarantee: the three un-doored statuses are door sources, worker-reachable."""
    for status in DOOR_SOURCES:
        assert any(e.seat == WORKER and e.verb == "kanban_adjudicate" for e in EXITS[status]), (
            f"{status} is a door source with no kanban_adjudicate exit"
        )


# --------------------------------------------------------------------------- resolvability


def test_every_named_cli_verb_is_registered():
    """A human remedy the parser does not serve is a wish, not a door."""
    registered = _cli_verbs()
    missing = sorted({
        e.verb[4:] for exits in EXITS.values() for e in exits
        if e.verb.startswith("cli:") and e.verb[4:] not in registered
    })
    assert missing == [], f"cli verbs named but not registered: {missing}"


def test_every_named_kernel_function_resolves():
    missing = sorted({
        f"{e.verb} -> {e.kernel}" for exits in EXITS.values() for e in exits
        if e.kernel and resolve_kernel(e.kernel) is None
    })
    assert missing == [], f"exits naming a kernel function that does not exist: {missing}"


def test_every_worker_door_is_a_registered_worker_tool():
    """An unregistered tool is not a door (t_765136ce, the same rule for the landing verdict)."""
    kt = _kanban_tools()
    names = _tool_names(kt)
    from tools.registry import registry

    worker_verbs = {e.verb for exits in EXITS.values() for e in exits if e.seat == WORKER}
    assert worker_verbs, "no worker doors at all"

    unregistered = sorted(worker_verbs - names)
    assert unregistered == [], f"worker doors that are not tools: {unregistered}"

    orchestrator_only = sorted(worker_verbs & set(kt._ORCHESTRATOR_TOOLS))
    assert orchestrator_only == [], (
        f"worker doors a worker cannot see (orchestrator-only tools): {orchestrator_only}"
    )

    wrongly_gated = sorted(
        verb for verb in worker_verbs
        if getattr(registry.get_entry(verb), "check_fn", None) is not kt._check_kanban_mode
    )
    assert wrongly_gated == [], f"worker doors not gated by _check_kanban_mode: {wrongly_gated}"


def test_every_tool_is_placed_in_the_matrix_or_declared_a_non_exit():
    """Both ways: the matrix cannot drift from the toolset, in either direction."""
    kt = _kanban_tools()
    tools = _tool_names(kt)
    matrix = {e.verb for exits in EXITS.values() for e in exits if not e.verb.startswith("cli:")}

    unplaced = sorted(tools - matrix - NOT_A_STATUS_EXIT)
    assert unplaced == [], (
        f"tools that neither move a card nor are declared non-exits: {unplaced} — place each in "
        f"EXITS (if it is a status exit) or in NOT_A_STATUS_EXIT"
    )

    phantom = sorted(matrix - tools)
    assert phantom == [], f"exits naming tools that are not registered: {phantom}"

    stale = sorted(NOT_A_STATUS_EXIT - tools)
    assert stale == [], f"NOT_A_STATUS_EXIT names tools that no longer exist: {stale}"


def test_the_seats_are_the_three_known_seats():
    seats = {e.seat for exits in EXITS.values() for e in exits}
    assert seats == {WORKER, ORCHESTRATOR, HUMAN}


def test_owning_door_sends_a_reader_to_a_real_act():
    """The refusal text's door must be the same one the matrix names."""
    from hermes_cli.kanban_exits import owning_door

    for status in DEFAULT_STATUSES:
        door = owning_door(status)
        assert door, f"{status} has no printable door"
        if status in DOOR_SOURCES:
            assert "kanban_adjudicate" in door


@pytest.mark.parametrize("status", sorted(HUMAN_ONLY_STATUSES))
def test_human_only_statuses_name_a_cli_remedy(status):
    verbs = {e.verb for e in EXITS[status] if e.seat == HUMAN and e.verb.startswith("cli:")}
    assert verbs, f"{status} is human-only with no CLI remedy"
    assert {v[4:] for v in verbs} <= _cli_verbs()
