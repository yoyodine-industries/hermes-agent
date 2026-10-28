"""Workflow-run census and attribution for the shutdown drain (t_4b9843a4).

Extracted from ``gateway.run_shutdown`` (a 2.3k-line facade that may not grow) so the drain's
knowledge of PRODUCTION workflow runs lives in one topical sibling: the pre-drain census, the
sentence it logs, and the attribution pass that names a bounce on the runs the sweep reaped.

A yoyoflow run is not an agent turn, a cron job or an api_server run, so none of the drain's
own counts see it: on 2026-10-21 the drain reported "0 in-flight cron job(s)" while reaping a
live 32-minute production flight. Everything here is best-effort and never a gate -- a missing
or unreadable ledger must never fail a shutdown.
"""

from __future__ import annotations

import logging
import os

from gateway.run_shutdown_api import GatewayApiRunCensusMixin

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.run")


class GatewayWorkflowCensusMixin(GatewayApiRunCensusMixin):
    """The drain's view of production workflow runs: census, count words, attribution."""

    def _workflow_runs_in_flight(self) -> list:
        """The pre-drain census: ``running`` ledger rows, each with its owner's liveness.

        Read once per shutdown and kept on the instance -- the census must be the state BEFORE
        the kill sweep, so the attribution pass knows which runs were live when we started
        signalling. ``RunningRun.pid_alive`` is that evidence and never changes afterwards.
        ``running_runs`` is total (an unreadable ledger reads as no runs).
        """
        cached = getattr(self, "_workflow_census_snapshot", None)
        if cached is not None:
            return cached
        from gateway.workflow_runs import now_utc_iso, running_runs
        if not getattr(self, "_workflow_bounce_started", ""):
            self._workflow_bounce_started = now_utc_iso()
        self._workflow_census_snapshot = running_runs()
        return self._workflow_census_snapshot

    def _workflow_runs_now(self) -> list:
        """A FRESH census, read after the sweep (what is still in flight right now)."""
        from gateway.workflow_runs import running_runs
        return running_runs()

    def _workflow_census_at_start(self) -> tuple[int, int, str]:
        """``(live flights, orphaned rows, log sentence)`` from the PRE-drain snapshot.

        Live flights are what the drain is talking about; orphaned rows -- a ``running`` record
        whose owner is already gone, left by a SIGKILL/OOM/reboot -- are named beside the count
        instead of being counted, because reporting one as in flight asserts work that is not in
        the air (the same false green as the original "0 in-flight cron job(s)").
        """
        from gateway.workflow_runs import census_fragment, live_runs, orphaned_runs
        runs = self._workflow_runs_in_flight()
        live = len(live_runs(runs))
        orphans = len(orphaned_runs(runs))
        return live, orphans, census_fragment(live, orphans)

    def _workflow_census_now(self) -> tuple[int, int]:
        """``(live flights, orphaned rows)`` NOW -- for the drain-done line."""
        from gateway.workflow_runs import live_runs, orphaned_runs
        runs = self._workflow_runs_now()
        return len(live_runs(runs)), len(orphaned_runs(runs))

    def _workflow_census_fragment(self) -> str:
        """The live-flight sentence for a drain that is timing out.

        Live count takes the floor of the snapshot (a run that died between the two reads is
        still one the drain saw) while orphans come from the freshest read.
        """
        from gateway.workflow_runs import census_fragment, live_runs, orphaned_runs
        snapshot = self._workflow_runs_in_flight()
        now = self._workflow_runs_now()
        live = max(len(live_runs(snapshot)), len(live_runs(now)))
        return census_fragment(live, len(orphaned_runs(now or snapshot)))

    def _stamp_reaped_workflow_runs(self, reason: str, *, settle_s: float = 2.0) -> list:
        """Name this bounce on every counted run the kill sweep actually reaped.

        Detaching the departure (``tools.workflow_departure``) is what normally keeps a run out
        of the sweep's reach; this is the kill-switched path made honest -- a run whose process
        we killed gets ``interrupted by gateway bounce (pid ..., started ..., reason ...)`` on
        its record instead of a bare ``SystemExit(143)``. The snapshot's per-row liveness is what
        makes that evidence: a row already orphaned before the bounce is never named a victim,
        and a run that reached its own terminal outcome is never rewritten.
        """
        runs = getattr(self, "_workflow_census_snapshot", None) or []
        if not runs:
            return []
        from gateway.workflow_runs import stamp_reaped
        try:
            return stamp_reaped(
                runs,
                pid=os.getpid(),
                started=getattr(self, "_workflow_bounce_started", "") or "",
                reason=reason,
                settle_s=settle_s,
            )
        except Exception as exc:  # health: allow BLE001 -- fail-open (t_4b9843a4): a stamping bug must never take a shutdown down; the traceback is logged, never hidden
            logger.warning("workflow run attribution failed: %s", exc, exc_info=True)
            return []
