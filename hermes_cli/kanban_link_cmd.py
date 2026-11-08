"""``hermes kanban link`` — the CLI door onto the link receipt.

The verb used to print "Linked a -> b" and (when the child was demoted) a note about the
gate. Neither says whether anything will ever move the parent: a link into a card parked in
``review``, ``blocked`` or behind a dead assignee is a silent park, so the door now prints the
per-parent promotion state and the standing-parent warning on stderr
(``hermes_cli.kanban_db_links`` owns the classification; stdout stays ``Linked a -> b``).

Split out of ``hermes_cli.kanban``; the verb's own helpers are reached late-bound via ``_kc``
(import-cycle breaking) so monkeypatching ``kanban.<name>`` keeps working.
"""

from __future__ import annotations

import argparse
import os
import sys

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db as kb
from hermes_cli.profiles import list_profile_names


def cmd_link(args: argparse.Namespace) -> int:
    """Link two cards and report what the new dependency set can do on its own."""
    # A worker linking its own running card (dependency-block handoff) proves
    # ownership with its run id; linking a foreign task never needs one.
    expected_child_run_id = (
        _kc._worker_run_id_for(args.child_id)
        if args.child_id == os.environ.get("HERMES_KANBAN_TASK") else None)
    with kbc.connect_closing() as conn:
        outcome = kb.link_tasks_reported(
            conn, args.parent_id, args.child_id,
            expected_child_run_id=expected_child_run_id,
            profiles=list_profile_names(), board=kb.get_current_board())
    print(f"Linked {args.parent_id} -> {args.child_id}")
    if outcome.gated:
        print(
            f"Note: {args.child_id} was ready and is now todo — parent "
            f"{args.parent_id} is not done yet. The ready -> running claim "
            f"re-checks parents, so the child only runs after the parent "
            f"completes; use `hermes kanban unlink {args.parent_id} {args.child_id}` "
            f"to run it now."
        )
    # `Linked` stays stdout (machine-parsable); the promotion receipt goes to stderr, where
    # a human reads the warning that the card is parked behind a parent nobody will finish.
    if outcome.parents:
        print(f"Parent state ({len(outcome.parents)} edge(s)):", file=sys.stderr)
        for edge in outcome.parents:
            print(f"  {edge.id}  {edge.code}  {edge.text}", file=sys.stderr)
    if outcome.warning:
        print(outcome.warning, file=sys.stderr)
    return 0


# Late-bound origin namespace (see module docstring); imported LAST so this
# module is fully populated before ``kanban`` imports from it.
from hermes_cli import kanban as _kc
