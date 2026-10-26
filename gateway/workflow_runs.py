"""Census and attribution for production workflow runs a gateway bounce can reap.

The drain counts *agent turns* (in-flight cron jobs, api_server runs, deferred workers).
A yoyoflow workflow run is none of those: when one was launched from a gateway-served turn
it is a child of the gateway's process tree, and a sanctioned bounce reaps it mid-flight
while the drain reports "0 in-flight cron job(s)". That is a false green, and it is how a
32-minute production flight died at 00:14:04 on 2026-10-21 with nothing but a bare
``interrupted (SystemExit(143))`` on its record (t_4b9843a4).

This module gives the drain the two things it was missing:

* **a census** -- :func:`running_runs` reads the yoyoflow ledger's ``running`` rows, so the
  drain can say "N in-flight workflow run(s)" instead of reporting silence;
* **an actor** -- :func:`stamp_reaped` writes the bounce into the record of every counted
  run whose process the sweep actually killed, so the failure actor has something
  attributable to classify instead of an unattributed interruption.

The ledger is read/written best-effort and never gates a shutdown: an unreadable or absent
ledger yields an empty census, and a refused write is logged, not raised. Detaching the
departure (``tools.workflow_departure``) is what keeps a run out of this path at all; this
module is what makes the kill-switched path honest.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

#: The yoyoflow run ledger. ``YOYOFLOW_STATE_DB`` wins when a host or fixture points
#: elsewhere; otherwise the deployed prod store is used when it exists.
ENV_LEDGER = "YOYOFLOW_STATE_DB"
DEFAULT_LEDGER = "/opt/hermes_prod/run_details/yoyoflow-state.db"

#: Anchored phrase the failure actor matches on. Do not reword without updating it.
BOUNCE_PREFIX = "interrupted by gateway bounce"

_SQLITE_TIMEOUT_S = 1.0


@dataclass(frozen=True)
class RunningRun:
    """One in-flight workflow run, as the ledger records it."""

    id: str
    workflow: str
    pid: int
    started_at: str

    def __str__(self) -> str:  # pragma: no cover - diagnostics only
        return f"{self.workflow}:{self.id} (pid {self.pid})"


def ledger_path() -> Optional[str]:
    """Path to the workflow run ledger, or None when this host has none."""
    explicit = (os.environ.get(ENV_LEDGER) or "").strip()
    if explicit:
        return explicit if os.path.exists(explicit) else None
    return DEFAULT_LEDGER if os.path.exists(DEFAULT_LEDGER) else None


def _connect(path: str, *, read_only: bool) -> sqlite3.Connection:
    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=_SQLITE_TIMEOUT_S)
    else:
        conn = sqlite3.connect(path, timeout=_SQLITE_TIMEOUT_S)
    with suppress(Exception):
        conn.execute(f"PRAGMA busy_timeout = {int(_SQLITE_TIMEOUT_S * 1000)}")
    return conn


def running_runs(ledger: Optional[str] = None) -> list[RunningRun]:
    """The ledger's in-flight workflow runs (empty when unreadable/absent)."""
    path = ledger or ledger_path()
    if not path or not os.path.exists(path):
        return []
    try:
        conn = _connect(path, read_only=True)
        try:
            rows = conn.execute(
                "SELECT id, workflow_name, owner_pid, started_at FROM runs WHERE status = 'running'"
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:  # absent schema, locked, corrupt: a census is not a gate
        logger.debug("workflow run census unavailable (%s): %s", path, exc)
        return []
    runs: list[RunningRun] = []
    for run_id, workflow, pid, started_at in rows:
        try:
            pid_int = int(pid or 0)
        except (TypeError, ValueError):
            pid_int = 0
        runs.append(RunningRun(
            id=str(run_id or ""), workflow=str(workflow or ""), pid=pid_int,
            started_at=str(started_at or ""),
        ))
    return runs


def in_flight_count(ledger: Optional[str] = None) -> int:
    """How many workflow runs are in flight right now."""
    return len(running_runs(ledger))


def census_fragment(count: int) -> str:
    """The drain's own words for a workflow census, so logs read as one sentence."""
    return f"{count} in-flight workflow run(s)"


def bounce_actor_text(*, pid: int, started: str, reason: str) -> str:
    """The actor sentence stamped onto a record this gateway reaped."""
    return f"{BOUNCE_PREFIX} (pid {pid}, started {started}, reason {reason})"


def _pid_alive(pid: int) -> bool:
    """True when *pid* is a live, non-zombie process (our own liveness evidence)."""
    if not pid or pid <= 0:
        return False
    try:
        import psutil
    except Exception:
        psutil = None
    if psutil is not None:
        try:
            proc = psutil.Process(pid)
            return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stamp_reaped(
    runs: Sequence[RunningRun], *, pid: int, started: str, reason: str,
    ledger: Optional[str] = None, settle_s: float = 2.0,
    alive: Optional[Callable[[int], bool]] = None,
) -> list[str]:
    """Name the bounce on every counted run that this sweep actually reaped.

    Evidence, never inference: a run is stamped only when its ``owner_pid`` was alive at
    census time and is gone now. *settle_s* is a bounded wait for the engine to write its
    own abort line first, so the bounce is prefixed onto that line instead of racing it.
    """
    path = ledger or ledger_path()
    if not path or not runs:
        return []
    is_alive = alive or _pid_alive
    deadline = time.monotonic() + max(0.0, settle_s)
    dead = [r for r in runs if not is_alive(r.pid)]
    if not dead:
        return []
    # Bounded settle: a SIGTERMed engine writes `run aborted: ...` within milliseconds.
    # Waiting for the row to leave 'running' keeps both facts on the record.
    while time.monotonic() < deadline:
        try:
            conn = _connect(path, read_only=True)
            try:
                placeholders = ",".join("?" * len(dead))
                pending = {
                    row[0] for row in conn.execute(
                        f"SELECT id FROM runs WHERE status = 'running' AND id IN ({placeholders})",
                        [r.id for r in dead],
                    ).fetchall()
                }
            finally:
                conn.close()
        except Exception:
            pending = set()
        if not pending:
            break
        time.sleep(0.05)
    text = bounce_actor_text(pid=pid, started=started, reason=reason)
    stamped: list[str] = []
    try:
        conn = _connect(path, read_only=False)
        try:
            for run in dead:
                row = conn.execute(
                    "SELECT status, error FROM runs WHERE id = ?", (run.id,)
                ).fetchone()
                if row is None:
                    continue
                status, existing = row[0], (row[1] or "")
                if BOUNCE_PREFIX in existing:
                    continue
                error = f"{text}; {existing}" if existing else text
                conn.execute("UPDATE runs SET error = ? WHERE id = ?", (error, run.id))
                stamped.append(run.id)
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("could not stamp %d reaped workflow run(s) in %s: %s", len(dead), path, exc)
        return []
    if stamped:
        logger.warning(
            "stamped %d reaped workflow run(s) as %s (pid %d)", len(stamped), BOUNCE_PREFIX, pid,
        )
    return stamped


def now_utc_iso() -> str:
    """Wall-clock stamp for the actor sentence (UTC, second precision)."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def reaped_or_none(runs: Iterable[RunningRun], *, alive: Optional[Callable[[int], bool]] = None
                   ) -> list[RunningRun]:
    """The runs among *runs* whose process is gone (used by tests and diagnostics)."""
    is_alive = alive or _pid_alive
    return [r for r in runs if not is_alive(r.pid)]
