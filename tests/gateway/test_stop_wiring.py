"""Every in-fork stopper writes the stop record before it delivers the stop (t_4feca434).

The record and its schema are ``gateway/stop_attribution.py`` (t_4f9976aa). This suite is the
wiring contract: one case per call site, each driving the REAL function that performs the stop
(signals and service-manager calls are stubbed) and asserting exactly ONE
``gateway.stop_requested`` row, with a ``stop_id``, the ruled ``kind``/``delivery`` and an
honest ``lull_gate``. A stopper that stops writing the row fails its own case.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import status
from gateway import stop_attribution as sa

_DEAD_PID = 2 ** 22 + 4242


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def _rows(home: Path, tag: str = "gateway.stop_requested") -> list[dict]:
    path = home / "logs" / "gateway-exit-diag.log"
    if not path.exists():
        return []
    return [r for r in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
            if r.get("tag") == tag]


def _one(home: Path) -> dict:
    rows = _rows(home)
    assert len(rows) == 1, f"expected exactly one stop_requested row, got {rows}"
    row = rows[0]
    assert row.get("stop_id"), "every stop_requested row carries a stop_id"
    requester = row["requester"]
    assert requester.get("kind"), "requester kind is required"
    assert requester.get("lull_gate") in sa.LULL_GATES, "lull_gate is stated, never omitted"
    return row


# ── the call sites ───────────────────────────────────────────────────────────

def _cli_stop(home: Path, monkeypatch) -> dict:
    from hermes_cli import gateway_profile_lifecycle as gpl
    monkeypatch.setattr(sys, "argv", ["hermes", "gateway", "stop"])
    assert gpl.mark_planned_stop(os.getpid()) is not None
    return _one(home)


def _systemd_stop(home: Path, monkeypatch) -> dict:
    from hermes_cli import gateway as gw
    monkeypatch.setattr(gw, "_systemd_scope_preamble", lambda *a, **k: False)
    monkeypatch.setattr(gw.gateway_service_owner, "refuse_foreign_systemd_unit", lambda *a, **k: None)
    monkeypatch.setattr(gw, "_sync_hermes_home_from_systemd_unit", lambda *a, **k: None)
    monkeypatch.setattr(gw, "_run_systemctl", lambda *a, **k: None)
    monkeypatch.setattr(status, "get_running_pid", lambda *a, **k: os.getpid())
    gw.systemd_stop()
    return _one(home)


def _launchd_stop(home: Path, monkeypatch) -> dict:
    from hermes_cli import gateway as gw
    from hermes_cli import gateway_launchd as gl
    monkeypatch.setattr(gl.gateway_service_owner, "refuse_foreign_launchd_plist", lambda *a, **k: None)
    monkeypatch.setattr(status, "get_running_pid", lambda *a, **k: os.getpid())

    def _bootout_fails_unloaded(*a, **k):
        raise subprocess.CalledProcessError(3, "launchctl")

    monkeypatch.setattr(gl.subprocess, "run", _bootout_fails_unloaded)
    monkeypatch.setattr(gl, "_launchd_error_indicates_unloaded", lambda exc: True)
    monkeypatch.setattr(gl, "_gw", lambda: SimpleNamespace(
        _resolved_launchd_domain="gui/1",
        _profile_suffix=lambda: "",
        _mark_planned_stop=gw._mark_planned_stop,
        _CAPTURE_TEXT={},
        _wait_for_gateway_exit=lambda **k: True))
    gl.launchd_stop()
    return _one(home)


def _s6_stop(home: Path, monkeypatch) -> dict:
    from hermes_cli import service_manager as sm
    manager = sm.S6ServiceManager(home / "s6")
    monkeypatch.setattr(manager, "_supervised_pid", lambda name: os.getpid())
    monkeypatch.setattr(manager, "_run_svc", lambda *a, **k: None)
    monkeypatch.setattr(sm, "_write_gateway_desired_state", lambda *a, **k: None)
    manager.stop("gateway-default")
    return _one(home)


def _replace_takeover(home: Path, monkeypatch) -> dict:
    """The INCOMING gateway (this process) records the takeover of the victim (this same pid is
    patched as the victim only for identity purposes — the requester is always this process)."""
    from gateway import run, run_bootstrap
    from gateway import status_scoped_locks
    monkeypatch.setattr(run, "_replace_target_belongs_to_other_profile", lambda pid: False)
    monkeypatch.setattr(status, "get_process_start_time", lambda pid: 111.0)
    monkeypatch.setattr(status, "write_takeover_marker", lambda *a, **k: True)
    monkeypatch.setattr(status, "_snapshot_gateway_children", lambda pid: [])
    monkeypatch.setattr(status, "terminate_pid", lambda *a, **k: None)
    monkeypatch.setattr(status, "remove_pid_file", lambda *a, **k: None)
    monkeypatch.setattr(status, "reap_gateway_children", lambda *a, **k: None)
    monkeypatch.setattr(run, "_wait_for_pid_exit", _async_true)
    monkeypatch.setattr(run, "get_hermes_home", lambda: home)
    monkeypatch.setattr(run, "_clear_takeover_marker_quiet", lambda *a, **k: None)
    monkeypatch.setattr(status_scoped_locks, "release_all_scoped_locks", lambda **k: 0)

    assert asyncio.run(run_bootstrap._start_gateway_replace_existing_instance(_DEAD_PID, True)) is True
    row = _one(home)
    assert row["target"]["pid"] == _DEAD_PID, "the row names the VICTIM as the target"
    assert row["requester"]["pid"] == os.getpid(), "the requester is the INCOMING process"
    assert row["requester"]["ppid"] == os.getppid()
    assert isinstance(row["requester"]["argv"], list) and row["requester"]["argv"]
    return row


async def _async_true(*a, **k) -> bool:
    return True


def _runtime_replace(home: Path, monkeypatch) -> dict:
    from hermes_cli import gateway_runtime_replace as grr
    monkeypatch.setattr(status, "get_process_start_time", lambda pid: None)
    assert grr._write_planned_stop(home, _DEAD_PID, 222.0)
    return _one(home)


def _update_marker(home: Path, monkeypatch) -> dict:
    from hermes_cli.update_cmd_windows import _write_update_planned_stop_marker
    assert _write_update_planned_stop_marker(home, os.getpid(), stage="unit_test_stage") is True
    row = _one(home)
    assert "hermes update" in row["requester"]["reason_text"]
    assert "unit_test_stage" in row["requester"]["reason_text"]
    return row


def _update_launchd(home: Path, monkeypatch) -> dict:
    from hermes_cli import update_cmd_posix_pause as upp
    monkeypatch.setattr(upp.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stderr="", stdout=""))
    monkeypatch.setattr(upp, "_wait_gone", lambda *a, **k: set())
    upp._stop_job({"home": str(home), "pid": os.getpid(), "domain": "gui/1",
                   "label": "ai.hermes.gateway", "ct": None})
    return _one(home)


def _update_sigterm(home: Path, monkeypatch) -> dict:
    from hermes_cli import update_cmd_posix_pause as upp
    monkeypatch.setattr(upp.os, "kill", lambda pid, sig: None)
    upp._stop_at_once({"home": str(home), "pid": os.getpid(), "ct": None})
    return _one(home)


def _sweep_sigterm(home: Path, monkeypatch) -> dict:
    from hermes_cli import gateway as gw
    monkeypatch.setattr(gw, "find_gateway_pids", lambda *a, **k: [os.getpid()])
    monkeypatch.setattr(gw, "terminate_pid", lambda *a, **k: None)
    assert gw.kill_gateway_processes() == 1
    return _one(home)


def _host_stop(home: Path, monkeypatch) -> dict:
    from hermes_cli import gateway as gw
    monkeypatch.setattr(gw, "kill_gateway_processes", lambda *a, **k: 0)
    monkeypatch.setattr(gw, "terminate_pid", lambda *a, **k: None)
    assert gw._stop_host_multiplexer(SimpleNamespace(pid=os.getpid())) == 1
    return _one(home)


def _profile_stop(home: Path, monkeypatch) -> dict:
    from hermes_cli import profiles
    profile_dir = home / "profiles" / "p"
    profile_dir.mkdir(parents=True)
    (profile_dir / "gateway.pid").write_text(json.dumps(
        {"pid": os.getpid(), "start_time": 111.0, "hermes_home": str(profile_dir)}), encoding="utf-8")
    monkeypatch.setattr(status, "recorded_gateway_home_conflicts", lambda *a, **k: False)
    monkeypatch.setattr(status, "terminate_pid", lambda *a, **k: None)
    monkeypatch.setattr(profiles, "_wait_then_force_kill", lambda *a, **k: True)
    profiles._stop_gateway_process(profile_dir)
    return _one(home)


_CASES = {
    # case: (driver, expected kind, expected delivery)
    "cli_stop": (_cli_stop, "cli_stop", "marker"),
    "systemd_stop": (_systemd_stop, "supervisor", "systemd"),
    "launchd_stop": (_launchd_stop, "supervisor", "launchd"),
    "s6_stop": (_s6_stop, "supervisor", "s6"),
    "replace_takeover": (_replace_takeover, "replace_takeover", "marker"),
    "runtime_replace": (_runtime_replace, "replace_takeover", "marker"),
    "update_marker": (_update_marker, "update", "marker"),
    "update_launchd": (_update_launchd, "update", "launchd"),
    "update_sigterm": (_update_sigterm, "update", "sigterm"),
    "sweep_sigterm": (_sweep_sigterm, "operator", "sigterm"),
    "host_stop": (_host_stop, "operator", "sigterm"),
    "profile_stop": (_profile_stop, "operator", "sigterm"),
}


@pytest.mark.parametrize("case", sorted(_CASES))
def test_every_stopper_writes_one_stop_request_before_delivering(case, home, monkeypatch):
    driver, kind, delivery = _CASES[case]
    row = driver(home, monkeypatch)
    assert row["requester"]["kind"] == kind, (case, row["requester"])
    assert row["delivery"]["method"] == delivery, (case, row["delivery"])
    assert row["requester"]["lull_gate"] == "not_consulted", (
        f"{case}: no lull gate runs on this path, so it must be honest about that")
    assert row["target"]["pid"] > 0


def test_a_marker_cannot_be_written_without_a_requester(home):
    """The writer is the only door: an unattributed marker is refused, so a stop whose requester
    row is absent cannot be constructed through these APIs."""
    from gateway.stop_attribution import capture_requester, new_stop_id
    assert status.write_planned_stop_marker(os.getpid(), requester={}, stop_id=new_stop_id()) is False
    assert status.write_planned_stop_marker(
        os.getpid(), requester=capture_requester(kind="cli_stop"), stop_id="") is False


def test_cli_stop_reason_text_comes_from_argv(home, monkeypatch):
    from hermes_cli import gateway_profile_lifecycle as gpl
    monkeypatch.setattr(sys, "argv", ["hermes", "gateway", "stop", "--all"])
    gpl.mark_planned_stop(os.getpid())
    row = _one(home)
    assert "gateway" in row["requester"]["reason_text"] and "stop" in row["requester"]["reason_text"]
    assert row["requester"]["argv"][:3] == ["hermes", "gateway", "stop"]


def test_s6_restart_is_attributed_before_the_signal(home, monkeypatch):
    from hermes_cli import service_manager as sm
    manager = sm.S6ServiceManager(home / "s6")
    monkeypatch.setattr(manager, "_supervised_pid", lambda name: os.getpid())
    monkeypatch.setattr(manager, "_run_svc", lambda *a, **k: None)
    monkeypatch.setattr(sm, "_write_gateway_desired_state", lambda *a, **k: None)
    manager.restart("gateway-default")
    row = _one(home)
    assert row["requester"]["kind"] == "supervisor"
    assert row["delivery"]["method"] == "s6"
    assert row["requester"]["reason_code"] == "service_restart"
