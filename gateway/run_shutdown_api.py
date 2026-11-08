"""api_server run accounting for the shutdown drain (extracted from ``gateway.run_shutdown``).

The drain counts, interrupts and marks api_server work that lives outside ``_running_agents``.
Moved verbatim so the facade stops growing (FILE_LINES ratchet, t_4b9843a4); the bodies are
unchanged, so the moved units keep their caps and the log records keep their ``gateway.run`` name.
"""

from __future__ import annotations

import logging
from typing import Any

from gateway.config import Platform
from gateway.run_shutdown_session_end import GatewaySessionEndMixin

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.run")


class GatewayApiRunCensusMixin(GatewaySessionEndMixin):
    """Census/interrupt/mark hooks for api_server work outside ``_running_agents``."""

    def _api_server_hook(self, name: str, *args: Any) -> int:
        """Call the primary API-server adapter's ``name`` hook, clamped >= 0 (0 when the hook is absent).

        Only the primary API server owns the HTTP listener, so only it is a source of this work.
        """
        helper = getattr(getattr(self, "adapters", {}).get(Platform.API_SERVER), name, None)
        return max(0, int(helper(*args))) if callable(helper) else 0

    def _active_api_run_count(self) -> int:
        """API-server work that is outside ``_running_agents``."""
        try:
            return self._api_server_hook("active_agent_work_count")
        except Exception:
            return 0

    def _active_api_worker_count(self) -> int:
        """API-server executor threads still inside an agent turn (#116535).

        Module-level, like the cron registry above: the handler-side adapter count is already
        unreachable here (``adapters`` was cleared a phase earlier) and, worse, drops on handler
        cancellation while the worker thread lives on. Read live at the close gate instead of
        snapshotting.
        """
        try:
            from gateway.platforms.api_server_runs import api_worker_live_count
            return max(0, int(api_worker_live_count()))
        except Exception:
            return 0

    def _interrupt_api_server_runs(self, reason: str) -> int:
        """Interrupt API-server agents not in ``_running_agents`` (same set ``_active_api_run_count`` counts)."""
        try:
            return self._api_server_hook("interrupt_active_runs", reason)
        except Exception as exc:
            logger.debug("Failed interrupting api_server runs during shutdown: %s", exc)
            return 0

    def _mark_api_runs_shutdown_requested(self) -> int:
        """Persist the shutdown boundary on API runs before the drain can await."""
        try:
            return self._api_server_hook("mark_shutdown_requested")
        except Exception as exc:
            logger.debug("Failed marking api_server runs as shutdown-requested: %s", exc)
            return 0
