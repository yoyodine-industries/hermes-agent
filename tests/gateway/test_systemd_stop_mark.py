"""Supervisor-ordered stops exit cleanly instead of logging a failure.

Raw ``systemctl restart/stop`` sends SIGTERM with no marker, so the gateway
used to classify it as an unexpected kill (exit 1, ``Failed with result
exit-code``). The generated unit now marks the stop via ``ExecStop=`` first,
through the attributed stop-request door (requester + stop_id).
"""

from __future__ import annotations

import pytest

from gateway import systemd_stop_mark


def _spy(monkeypatch) -> list[tuple[int, dict]]:
    """Capture every ``write_stop_request`` call (pid + attribution kwargs)."""
    calls: list[tuple[int, dict]] = []

    def _record(pid, **kwargs):
        calls.append((pid, kwargs))
        return "stopid"

    monkeypatch.setattr("gateway.stop_attribution.write_stop_request", _record)
    return calls


def test_stop_mark_writes_planned_stop_for_supervised_pid(monkeypatch):
    """MAINPID (or an explicit argv PID) is marked; the helper owns identity AND attribution."""
    calls = _spy(monkeypatch)
    monkeypatch.setenv("MAINPID", "4242")
    assert systemd_stop_mark.main(["systemd_stop_mark"]) == 0
    assert [pid for pid, _ in calls] == [4242]
    _pid, kwargs = calls[0]
    # A supervisor stop is not anonymous: delivery names systemd, and the lull gate is honest.
    assert kwargs["delivery"] == "systemd"
    assert kwargs["kind"] in ("operator", "launchd")
    assert kwargs["lull_gate"] == "not_consulted"

    calls.clear()
    monkeypatch.delenv("MAINPID")
    assert systemd_stop_mark.main(["systemd_stop_mark", "7777"]) == 0
    assert [pid for pid, _ in calls] == [7777]


def test_stop_mark_never_blocks_restart_and_unit_wires_exec_stop(monkeypatch):
    """Unknown PIDs mark nothing but still exit 0; the unit runs the marker."""
    calls = _spy(monkeypatch)
    for argv, env in ((["m"], None), (["m", "0"], None), (["m", "abc"], None)):
        if env is None:
            monkeypatch.delenv("MAINPID", raising=False)
        assert systemd_stop_mark.main(argv) == 0
    assert calls == []

    def _boom(_pid, **_kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr("gateway.stop_attribution.write_stop_request", _boom)
    monkeypatch.setenv("MAINPID", "4242")
    assert systemd_stop_mark.main(["m"]) == 0

    from hermes_cli import gateway as gateway_cli

    unit = gateway_cli.generate_systemd_unit(system=False)
    # Both modules run through the installation launcher (`hermes --run-module <mod>`).
    assert "ExecStop=-" in unit and "gateway.systemd_stop_mark" in unit
    # The cgroup reaper still runs after the main process exits.
    assert "ExecStopPost=-" in unit and "gateway.cgroup_cleanup" in unit
    assert unit.index("systemd_stop_mark") < unit.index("cgroup_cleanup")
