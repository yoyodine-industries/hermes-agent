"""The BELT projection: a card block enqueues its domain's maintenance DAG.

Enqueue-only by contract. One row is appended to the belt queue (a separate
store, ``belt.db``) and the hook returns: no board write, no model call, no
routing, no window gate. Routing is cheap I/O and belongs to the dispatcher, and
a projection that acted on the card here would put the maintenance decision
inside the board's own transition.

Registered through ``hermes_cli.observability``, so it runs in the process that
performs the transition — a block raised by a worker fires in the WORKER, which
is the one thing a per-profile plugin cannot cover.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

HANDLED_HOOKS = frozenset({"kanban_task_blocked"})


def handles_hook(hook_name: str) -> bool:
    return hook_name in HANDLED_HOOKS


def observe_lifecycle(hook_name: str, **kwargs: Any) -> None:
    """Append this block to the belt queue; never raise."""
    if not handles_hook(hook_name):
        return
    task_id = str(kwargs.get("task_id") or "")
    if not task_id:
        return
    from hermes_cli.belt_queue import enqueue_block

    enqueue_block(
        task_id=task_id,
        board=kwargs.get("board"),
        assignee=kwargs.get("assignee"),
        run_id=kwargs.get("run_id"),
        reason=kwargs.get("reason"),
        block_kind=kwargs.get("block_kind"),
        source_status=kwargs.get("source_status"),
    )
