"""Stop attribution — the four rows of one stop, threaded by ONE ``stop_id``.

Acceptance for the record (t_4f9976aa): ``grep -E '"tag": "gateway\\.(stop_requested|stop_seen|
forced_exit|previous_exit)"' ~/.hermes/logs/gateway-exit-diag.log`` must answer, for every stop,
who asked, why, which pid, which delivery and whether the lull gate was consulted — with one
``stop_id`` on all rows. These tests replay the measured 2026-11-27 sequence (marker for a pid,
drain wedge, forced exit, new boot) against a real ``HERMES_HOME``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from gateway import lifecycle_ledger, shutdown_forensics, status, stop_attribution as sa
from gateway.shutdown_watchdog import _mark_exited_quietly

_DEAD_PID = 2 ** 22 + 12345


def _records(home: Path) -> list[dict]:
    path = home / "logs" / "gateway-exit-diag.log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _by_tag(home: Path) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for record in _records(home):
        out.setdefault(record.get("tag", ""), []).append(record)
    return out


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


# ── the replay: marker → drain wedge → forced exit → new boot ────────────────

def test_today_sequence_writes_four_rows_with_one_stop_id(home: Path) -> None:
    """2026-11-27 replayed: the bounce door asks pid A to stop, the gateway accepts the marker,
    the drain wedges and the shutdown watchdog force-exits, then the next boot reports the life.
    All four rows share ONE stop_id and answer who/why/which-pid/which-delivery/lull-gate."""
    # Boot 1 — the victim life claims the lifecycle sentinel.
    assert lifecycle_ledger.record_startup(home=home) is None

    # 1. gateway.stop_requested — the REQUESTER writes it BEFORE delivering anything.
    stop_id = sa.write_stop_request(
        os.getpid(), kind="window", reason_code="APR-0927",
        reason_text="bounce in the next lull", lane="default", lull_gate="bypassed",
        delivery="marker", home=home)
    assert stop_id

    # 2. gateway.stop_seen — the gateway accepts the marker (the shutdown handler's step).
    marker = status.consume_planned_stop_request_for_self()
    assert marker is not None and marker["stop_id"] == stop_id
    seen = sa.announce_stop_seen(marker, received_signal=None, home=home)
    assert seen["stop_id"] == stop_id and seen["phase"] == "marker"

    # 3. gateway.forced_exit — the drain wedged; the shutdown watchdog stamps and hard-exits.
    _mark_exited_quietly(1, "shutdown_watchdog", active_agents=2, active_cron_jobs=1)

    # 4. gateway.previous_exit — the next boot reports the prior life.
    assert lifecycle_ledger.record_startup(home=home) is None

    rows = _by_tag(home)
    assert set(rows) == {"gateway.stop_requested", "gateway.stop_seen",
                         "gateway.forced_exit", "gateway.previous_exit"}
    assert all(len(v) == 1 for v in rows.values()), rows
    assert {v[0]["stop_id"] for v in rows.values()} == {stop_id}, "the four rows must share one stop_id"

    requested = rows["gateway.stop_requested"][0]
    assert requested["target"]["pid"] == os.getpid()
    assert requested["delivery"]["method"] == "marker"
    requester = requested["requester"]
    assert requester["kind"] == "window" and requester["lane"] == "default"
    assert requester["reason_code"] == "APR-0927"
    assert requester["lull_gate"] == "bypassed"
    assert requester["pid"] == os.getpid() and isinstance(requester["argv"], list)

    # The gateway quoted the requester verbatim and named the phase that saw it.
    assert rows["gateway.stop_seen"][0]["requester"] == requester
    assert rows["gateway.stop_seen"][0]["phase"] == "marker"

    forced = rows["gateway.forced_exit"][0]
    assert forced["exit_code"] == 1 and forced["reason"] == "shutdown_watchdog"
    assert forced["active_agents"] == 2 and forced["active_cron_jobs"] == 1

    previous = rows["gateway.previous_exit"][0]
    assert previous["exit_code"] == 1 and previous["exit_reason"] == "shutdown_watchdog"
    assert previous["pid"] == os.getpid()
    assert previous["exited_at"] and previous["started_at"]


def test_sigkill_after_the_request_still_leaves_rows_1_and_4(home: Path) -> None:
    """No exit path runs on SIGKILL: the request row (written first) and the next boot's
    previous-exit row must still exist, and the boot must recover the stop_id by correlating the
    dead pid with the still-unconsumed request."""
    assert lifecycle_ledger.record_startup(home=home) is None
    stop_id = sa.write_stop_request(_DEAD_PID, kind="maintenance", reason_code="window_bounce",
                                    reason_text="forced bounce", lull_gate="bypassed",
                                    delivery="sigterm", write_marker=False, home=home)
    # SIGKILL: the sentinel is still the dead life's claim, nothing was consumed or stamped.
    sentinel = json.loads((home / "state" / "gateway.lifecycle.json").read_text())
    sentinel.update({"pid": _DEAD_PID, "started_at": "2026-11-27T02:33:00+00:00",
                     "start_time": 1.0})
    (home / "state" / "gateway.lifecycle.json").write_text(json.dumps(sentinel))

    assert lifecycle_ledger.record_startup(home=home) is not None  # unclean report

    rows = _by_tag(home)
    assert "gateway.stop_requested" in rows and "gateway.previous_exit" in rows
    assert "gateway.stop_seen" not in rows and "gateway.forced_exit" not in rows
    assert rows["gateway.previous_exit"][0]["stop_id"] == stop_id
    assert rows["gateway.previous_exit"][0]["pid"] == _DEAD_PID
    assert rows["gateway.previous_exit"][0]["exit_reason"] == "unclean (no exit path ran)"


def test_a_signal_stop_correlates_the_requesters_row(home: Path) -> None:
    """A bare SIGTERM carries no payload: the gateway finds the unconsumed request for its pid, so
    ``stop_seen`` still names the requester and the same stop_id (and a second, re-issued stop
    cannot reuse the spent id)."""
    lifecycle_ledger.record_startup(home=home)
    stop_id = sa.write_stop_request(os.getpid(), kind="cli_stop", reason_code="gateway_stop",
                                    delivery="sigterm", write_marker=False, home=home)

    seen = sa.announce_stop_seen(None, received_signal=15, home=home)
    assert seen["stop_id"] == stop_id
    assert seen["phase"] == "signal:SIGTERM"
    assert seen["requester"]["kind"] == "cli_stop"

    # The request is spent: the same row cannot attribute a later, unrelated stop.
    assert sa.find_stop_request(os.getpid(), home=home) is None
    second = sa.announce_stop_seen(None, received_signal=15, home=home)
    assert second["stop_id"] != stop_id
    assert second["requester"]["kind"] == "unknown"


# ── the shutdown-context line ───────────────────────────────────────────────

def test_snapshot_classifies_a_marker_driven_stop(home: Path) -> None:
    """A marker-delivered stop has NO signal: the context must say ``signal=marker`` and carry the
    requester + stop_id, instead of the measured ``signal=UNKNOWN`` with no attribution."""
    stop_id = sa.write_stop_request(os.getpid(), kind="window", lane="default",
                                    reason_code="APR-0927", lull_gate="bypassed", home=home)
    ctx = shutdown_forensics.snapshot_shutdown_context(None)
    assert ctx["signal"] == "marker"
    assert ctx["stop_id"] == stop_id
    assert ctx["requester"]["kind"] == "window"
    assert ctx["planned_stop_attributed"] is True

    line = shutdown_forensics.format_context_for_log(ctx)
    assert "signal=marker" in line
    assert f"stop_id={stop_id}" in line
    assert "requester_kind=window" in line and "requester_lane=default" in line


def test_snapshot_keeps_the_real_signal_when_one_arrived(home: Path) -> None:
    stop_id = sa.write_stop_request(os.getpid(), kind="cli_stop", lane="default", home=home)
    ctx = shutdown_forensics.snapshot_shutdown_context(15)
    assert ctx["signal"] == "SIGTERM"
    assert ctx["stop_id"] == stop_id


def test_the_shutdown_handler_names_the_requester_on_its_context_line(home, monkeypatch, caplog):
    """End of the chain: the REAL shutdown handler consumes the door's marker, writes
    ``gateway.stop_seen`` with the same stop_id, and logs ``signal=marker ... stop_id=...
    requester_kind=... requester_lane=...`` — the line an operator reads during an outage."""
    import asyncio
    import logging

    from gateway import run, run_bootstrap

    stop_id = sa.write_stop_request(os.getpid(), kind="window", lane="default",
                                    reason_code="APR-0927", reason_text="bounce in the next lull",
                                    lull_gate="bypassed", home=home)

    class _Runner:
        _signal_initiated_shutdown = False

        def __init__(self) -> None:
            self.stopped = False

        async def stop(self) -> None:
            self.stopped = True

    runner = _Runner()
    monkeypatch.setattr(run, "_hermes_home", home)  # the detached ps diagnostic must not hit the real home
    handler = run_bootstrap._start_gateway_make_shutdown_signal_handler(runner, [False])

    async def _drive() -> None:
        handler(None)  # the planned-stop watcher's call shape
        await asyncio.sleep(0)

    with caplog.at_level(logging.WARNING):
        asyncio.run(_drive())

    assert runner.stopped is True
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Shutdown context:")]
    assert lines, [r.getMessage() for r in caplog.records]
    line = lines[-1]
    assert "signal=marker" in line
    assert f"stop_id={stop_id}" in line
    assert "requester_kind=window" in line and "requester_lane=default" in line
    assert "under_systemd=no" in line  # ppid==1 on darwin is NOT systemd

    seen = _by_tag(home)["gateway.stop_seen"][0]
    assert seen["stop_id"] == stop_id and seen["phase"] == "marker"
    assert seen["requester"]["kind"] == "window"
    assert lifecycle_ledger.read_sentinel_stop_id(home=home) == stop_id


def test_under_systemd_is_not_derived_from_ppid_one_on_darwin(monkeypatch) -> None:
    """A launchd LaunchDaemon is always re-parented to 1, so ``ppid == 1`` on darwin is NOT
    evidence of systemd (measured false on this host)."""
    monkeypatch.setattr(shutdown_forensics.os, "getppid", lambda: 1)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setattr(shutdown_forensics.sys, "platform", "darwin")
    assert shutdown_forensics.snapshot_shutdown_context(None)["under_systemd"] is False

    monkeypatch.setattr(shutdown_forensics.sys, "platform", "linux")
    assert shutdown_forensics.snapshot_shutdown_context(None)["under_systemd"] is True


# ── the record's own invariants ─────────────────────────────────────────────

def test_argv_secrets_are_redacted(home: Path) -> None:
    argv = ["hermes", "gateway", "stop", "--token", "hunter2", "API_KEY=sk-live-123",
            "PATH=/usr/bin", "--lane=default"]
    assert sa.redact_argv(argv) == ["hermes", "gateway", "stop", "--token", "<redacted>",
                                    "API_KEY=<redacted>", "PATH=/usr/bin", "--lane=default"]


def test_kind_and_lull_gate_are_bounded(home: Path) -> None:
    row = sa.capture_requester(kind="something_new", lull_gate="who_knows")
    assert row["kind"] == "unknown" and row["lull_gate"] == "not_consulted"
    stop_id = sa.write_stop_request(os.getpid(), kind="supervisor", delivery="s6", write_marker=False,
                                    home=home)
    record = _records(home)[0]
    assert record["tag"] == "gateway.stop_requested" and record["stop_id"] == stop_id
    assert record["requester"]["kind"] == "unknown"
    assert record["delivery"] == {"method": "s6", "raw": None}


def test_a_forensic_write_never_raises_into_the_stop_path(home: Path) -> None:
    """An unwritable log directory costs the record, never the stop."""
    blocked = home / "logs"
    blocked.mkdir()
    blocked.chmod(0o500)
    try:
        assert sa.write_stop_request(os.getpid(), kind="cli_stop", write_marker=False,
                                     home=home)  # returns the id regardless
        sa.record_forced_exit(1, "shutdown_watchdog", home=home)
        sa.record_previous_exit({"phase": "exited"}, home=home)
    finally:
        blocked.chmod(0o700)
    assert os.getpid() > 0  # reached: nothing raised


def test_marker_is_refused_without_attribution(home: Path) -> None:
    from gateway.stop_attribution import capture_requester, new_stop_id

    assert status.write_planned_stop_marker(os.getpid(), requester={}, stop_id="x") is False
    assert status.write_planned_stop_marker(
        os.getpid(), requester=capture_requester(kind="cli_stop"), stop_id="") is False
    ok = status.write_planned_stop_marker(
        os.getpid(), requester=capture_requester(kind="cli_stop"), stop_id=new_stop_id())
    assert ok is True
