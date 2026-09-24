"""First-party Hermes observability integrations."""

from __future__ import annotations

import logging
from importlib import import_module
from typing import Any

logger = logging.getLogger(__name__)

#: Projections, in dispatch order. Each is a module exposing ``handles_hook`` and
#: ``observe_lifecycle``; adding a feature is a table entry, never a branch here.
#: ``observe_lifecycle`` is called on every projection for EVERY hook (each does
#: its own gating and may have per-event bookkeeping that is not a handler, e.g.
#: the shared-metrics consent reconcile), while ``handles_hook`` answers for the
#: caller's hot-path short-circuit.
_PROJECTIONS = ("kanban_unblocker", "relay_shared_metrics")


def _projection(module_name: str):
    return import_module(f"{__name__}.{module_name}")


def observe_lifecycle(hook_name: str, **kwargs: Any) -> None:
    """Dispatch a Hermes lifecycle event to built-in observability features."""
    for module_name in _PROJECTIONS:
        try:
            _projection(module_name).observe_lifecycle(hook_name, **kwargs)
        except Exception:
            logger.warning(
                "Built-in observability hook failed: %s (%s)", hook_name, module_name,
                exc_info=True,
            )


def handles_hook(hook_name: str) -> bool:
    """Return whether any built-in observability feature handles a hook.

    An unreadable projection counts as not-handling: dropping an observer is
    always safe, and one broken projection must not disable the others.
    """
    for module_name in _PROJECTIONS:
        try:
            if _projection(module_name).handles_hook(hook_name):
                return True
        except Exception:
            logger.warning(
                "Unable to inspect built-in observability projection: %s", module_name,
                exc_info=True,
            )
    return False
