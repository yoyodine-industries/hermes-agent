"""A sanctioned bounce must not reap a live production workflow run (t_4b9843a4).

Real processes, a real ledger, the real shutdown sweep. Two arms:

* **detached** (default): the run is spawned as a workflow departure, the gateway's own kill
  sweep runs over it, and the run survives to its own terminal state with a complete row;
* **detach disabled** (``HERMES_WORKFLOW_DETACH=0``): the run is an ordinary child, the sweep
  reaps it, and the drain says ``1 in-flight workflow run(s)`` while the run's record names the
  bounce as its actor instead of leaving a bare ``interrupted (SystemExit(143))``.

On 2026-10-21 the drain reported ``0 in-flight cron job(s)`` while exactly this happened to a
32-minute sdlc flight: no census, no actor, nothing for the failure actor to classify.
"""

import json
import os
import sqlite3
import sys
import textwrap
import time

import pytest

from gateway import workflow_runs as wfr
from gateway.run import GatewayRunner
from gateway.run_shutdown import GatewayShutdownMixin
from tools import workflow_departure as wd
from tools.process_registry import process_registry

LEDGER_SCHEMA = """
CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    workflow_name TEXT,
    status TEXT,
    owner_pid INTEGER,
    started_at TEXT,
    error TEXT
)
"""

#: A stand-in for the yoyoflow engine: it registers as the run's owner, aborts like the engine
#: does on SIGTERM (status failed + the bare SystemExit(143) line), and otherwise completes.
RUNNER = textwrap.dedent(
    """
    import os, signal, sqlite3, sys, time

    ledger, run_id, hold_s = sys.argv[1], sys.argv[2], float(sys.argv[3])

    def write(sql, *args):
        conn = sqlite3.connect(ledger, timeout=5)
        try:
            conn.execute(sql, args)
            conn.commit()
        finally:
            conn.close()

    write("UPDATE runs SET status='running', owner_pid=? WHERE id=?", os.getpid(), run_id)

    def _term(signum, frame):
        write("UPDATE runs SET status='failed', error=? WHERE id=?",
              "run aborted: interrupted (SystemExit(143))", run_id)
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, _term)
    time.sleep(hold_s)
    write("UPDATE runs SET status='done', error=NULL WHERE id=?", run_id)
    """
)


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    # The local environment prepends the `hermes` launcher dir to PATH; keep that resolution
    # away from the developer's real install (the suite refuses real-home I/O by design).
    monkeypatch.setenv("PATH", os.pathsep.join([str(tmp_path / "bin"), "/usr/bin", "/bin"]))
    from tools.environments import local as local_env
    monkeypatch.setattr(local_env, "_HERMES_BIN_DIR", None, raising=False)
    path = tmp_path / "yoyoflow-state.db"
    conn = sqlite3.connect(path)
    conn.executescript(LEDGER_SCHEMA)
    conn.execute(
        "INSERT INTO runs (id, workflow_name, status, owner_pid, started_at, error) "
        "VALUES ('20261020-1941', 'sdlc', 'queued', 0, '2026-10-20T23:41:27+00:00', NULL)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv(wfr.ENV_LEDGER, str(path))
    monkeypatch.delenv(wd.ENV_GATE, raising=False)
    return path


def _row(path, run_id="20261020-1941"):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT status, owner_pid, error FROM runs WHERE id=?", (run_id,)
        ).fetchone()
    finally:
        conn.close()


def _start_runner(tmp_path, path, *, hold_s=6.0, departure=True):
    script = tmp_path / "fake_runner.py"
    script.write_text(RUNNER)
    command = f"{sys.executable} {script} {path} 20261020-1941 {hold_s}"
    env_vars = {wd.ENV_MARKER: "1"} if departure else {}
    session = process_registry.spawn_local(
        command=command, cwd=str(tmp_path), task_id="fixture-bounce",
        session_key="fixture", env_vars=env_vars, departure=departure,
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        status, pid, _ = _row(path)
        if status == "running" and pid:
            return session
        time.sleep(0.05)
    raise AssertionError("the fixture runner never registered as the run's owner")


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_census_counts_the_ledger_running_rows(ledger):
    session = _start_runner(ledger.parent, ledger)
    try:
        census = wfr.running_runs()
        assert [r.id for r in census] == ["20261020-1941"]
        assert census[0].workflow == "sdlc" and census[0].pid
        assert wfr.in_flight_count() == 1
        assert wfr.census_fragment(1) == "1 in-flight workflow run(s)"
    finally:
        process_registry.kill_process(session.id)


def test_census_is_a_census_not_a_gate(tmp_path, monkeypatch):
    monkeypatch.setenv(wfr.ENV_LEDGER, str(tmp_path / "does-not-exist.db"))
    assert wfr.running_runs() == []
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a sqlite file at all" * 100)
    monkeypatch.setenv(wfr.ENV_LEDGER, str(broken))
    assert wfr.running_runs() == []


def test_detached_departure_survives_the_gateway_kill_sweep(ledger):
    """Arm 1: the run outlives the bounce and its row completes on its own."""
    session = _start_runner(ledger.parent, ledger, hold_s=2.5)
    census = wfr.running_runs()
    assert [r.id for r in census] == ["20261020-1941"]

    GatewayShutdownMixin._stop_kill_tool_subprocesses("fixture-bounce")

    assert session.exited is False, "the gateway sweep reaped a detached departure"
    assert _alive(census[0].pid), "the departure's process was killed by the bounce"
    assert _wait(lambda: _row(ledger)[0] == "done", timeout=15), _row(ledger)
    assert _row(ledger)[2] is None


def test_drain_names_an_undetached_run_and_the_record_names_the_bounce(ledger, caplog, monkeypatch):
    """Arm 2: detach off. The drain says so, and the record gets the actor."""
    monkeypatch.setenv(wd.ENV_GATE, "0")
    session = _start_runner(ledger.parent, ledger, hold_s=30.0, departure=False)
    census = wfr.running_runs()
    assert len(census) == 1
    runner_pid = census[0].pid

    # The drain's own sentence: a real `_stop_drain_active_work` on a sized double.
    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner.adapters = {}
    runner._cron_drain_timeout = 0

    async def _fake_drain(timeout, cron_timeout):
        return [], False

    runner._drain_active_agents = _fake_drain
    ctx = GatewayShutdownMixin._StopContext(
        deferred_count=lambda: 0, started_at=time.monotonic()
    )
    import asyncio

    with caplog.at_level("INFO", logger="gateway.run"):
        asyncio.run(GatewayShutdownMixin._stop_drain_active_work(runner, 1.0, ctx))
    assert any("1 in-flight workflow run(s)" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]

    # ... and the real sweep, then the real attribution pass.
    GatewayShutdownMixin._stop_kill_tool_subprocesses("fixture-bounce")
    assert _wait(lambda: not _alive(runner_pid), timeout=10), "the undetached run was not reaped"

    runner._workflow_census_snapshot = census
    runner._workflow_bounce_started = "2026-10-21T00:13:33+00:00"
    stamped = runner._stamp_reaped_workflow_runs("Gateway shutting down", settle_s=1.0)
    assert stamped == ["20261020-1941"]

    status, _, error = _row(ledger)
    assert status == "failed"
    assert error.startswith("interrupted by gateway bounce (")
    assert f"pid {os.getpid()}" in error
    assert "started 2026-10-21T00:13:33+00:00" in error
    assert "reason Gateway shutting down" in error
    # The engine's own line is kept, never erased: the actor is prefixed onto it.
    assert "SystemExit(143)" in error


def test_a_surviving_run_is_never_stamped(ledger):
    """Attribution is evidence, not inference: a live run's record stays untouched."""
    session = _start_runner(ledger.parent, ledger, hold_s=30.0)
    try:
        census = wfr.running_runs()
        assert wfr.stamp_reaped(
            census, pid=os.getpid(), started="2026-10-21T00:13:33+00:00",
            reason="Gateway shutting down", settle_s=0.0,
        ) == []
        assert _row(ledger)[2] is None
    finally:
        process_registry.kill_process(session.id)


def test_stamp_is_idempotent(ledger):
    session = _start_runner(ledger.parent, ledger, hold_s=30.0, departure=False)
    census = wfr.running_runs()
    process_registry.kill_process(session.id)
    assert _wait(lambda: not _alive(census[0].pid), timeout=10)
    first = wfr.stamp_reaped(census, pid=1234, started="t0", reason="Gateway shutting down", settle_s=0.5)
    assert first == ["20261020-1941"]
    assert wfr.stamp_reaped(census, pid=1234, started="t0", reason="Gateway shutting down", settle_s=0.0) == []
    assert _row(ledger)[2].count("interrupted by gateway bounce") == 1
