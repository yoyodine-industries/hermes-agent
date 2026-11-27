"""Diagnostics must distinguish policy rejection from inspected job properties.

Second contract (card t_a487ff8f): a guard refusal must be MACHINE-DISTINGUISHABLE from
the tool's own failure. The launchctl guard refused three separate probes before launchd
was ever asked, and because a refusal looked exactly like a launchctl command that had run
and failed, the harness was recorded as launchd's answer and a whole category of false
beliefs was built on it. Exit 78 (EX_CONFIG) plus the ``YOD-GUARD-REFUSED:`` marker line
mean "the command never ran" and nothing else.
"""

import json
import plistlib

from tools.terminal_tool_guards import (
    GUARD_REFUSED_EXIT_CODE,
    GUARD_REFUSED_MARKER,
    _blocked_json,
    gateway_lifecycle_block,
)


def test_bootstrap_rejection_does_not_invent_keepalive(tmp_path, monkeypatch):
    from tools import process_registry

    monkeypatch.setattr(process_registry, "_is_supervised_gateway_process", lambda: True)
    plist = tmp_path / "com.example.schedule.plist"
    plist.write_bytes(plistlib.dumps({
        "Label": "com.example.schedule",
        "ProgramArguments": ["/bin/true"],
        "RunAtLoad": False,
        "StartCalendarInterval": {"Hour": 9, "Minute": 0},
    }))
    blocked = gateway_lifecycle_block(
        command=f"launchctl bootstrap gui/501 {plist}",
        env=None, env_type="local", cwd=str(tmp_path), workdir=None,
        session_key="diagnostic-test",
    )
    assert blocked is not None
    result = json.loads(blocked)
    # A refusal is exit 78 + the marker, never a code a real launchctl run could return.
    assert result["exit_code"] == GUARD_REFUSED_EXIT_CODE == 78
    assert result["exit_code"] != 1
    assert result["guard_refused"] is True
    assert result["error"].startswith(GUARD_REFUSED_MARKER)
    assert GUARD_REFUSED_MARKER in result["output"]
    assert "launchctl-submit" in result["output"]
    # The sanctioned alternative is named, and the refusal says launchd was never asked.
    assert "separate shell outside the gateway" in result["error"]
    assert "launchd was never asked" in result["error"]
    # Nothing was measured about the job: no KeepAlive verdict is invented.
    assert "KeepAlive" in result["error"]


def test_interpreter_kill_rejection_names_the_owned_process_route(tmp_path, monkeypatch):
    """Scheduled-Task topology (#113667): only ``HERMES_SUPERVISED_CHILD`` marks the launch, and an
    image-name kill of the interpreter is refused with the ``proc_*`` / explicit-PID route named.
    Other images and explicit PIDs pass."""
    import os

    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.setenv("HERMES_SUPERVISED_CHILD", "1")
    for marker in ("INVOCATION_ID", "XPC_SERVICE_NAME", "HERMES_S6_SUPERVISED_CHILD", "HERMES_GATEWAY_EXTERNAL_SUPERVISOR"):
        monkeypatch.delenv(marker, raising=False)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda *a, **k: os.getpid())

    def run(command):
        return gateway_lifecycle_block(
            command=command, env=None, env_type="local", cwd=str(tmp_path), workdir=None,
            session_key="diagnostic-test",
        )

    blocked = json.loads(run("taskkill /F /IM python.exe 2>/dev/null | head -2"))
    assert blocked["exit_code"] == GUARD_REFUSED_EXIT_CODE
    assert blocked["guard_refused"] is True
    assert blocked["error"].startswith(GUARD_REFUSED_MARKER)
    assert "proc_" in blocked["error"] and "explicit PID" in blocked["error"]
    assert "proc_" in json.loads(run("pkill -9 python3"))["error"]
    assert run("taskkill /F /IM agent-browser.exe /T") is None
    assert run("taskkill /F /PID 46544") is None
    # Absence on the wrong side: a plain foreground `hermes gateway run` carries no launch marker.
    monkeypatch.delenv("HERMES_SUPERVISED_CHILD")
    assert run("pkill -9 python3") is None


def test_a_refusal_cannot_be_mistaken_for_a_tools_own_failure():
    """The property itself: exit 78 + the marker, and the tool's own failure carries neither."""
    from tools.terminal_tool import _error_json

    payload = json.loads(_blocked_json("Blocked: something", "blocked", guard="launchctl-submit"))
    assert payload["exit_code"] == GUARD_REFUSED_EXIT_CODE == 78
    assert payload["guard_refused"] is True
    assert payload["error"].startswith(GUARD_REFUSED_MARKER)
    assert GUARD_REFUSED_MARKER in payload["output"]

    failed = json.loads(_error_json("Command failed: EIO", exit_code=1))
    assert failed["exit_code"] == 1
    assert "guard_refused" not in failed
    assert GUARD_REFUSED_MARKER not in failed["error"]
    assert GUARD_REFUSED_MARKER not in failed["output"]
    # And the two envelopes are not interchangeable in either direction.
    assert _blocked_json("Blocked: something", "blocked") != _error_json(
        "Blocked: something", exit_code=1, status="blocked")


def test_the_marker_is_one_stable_line_whatever_guard_refused():
    """One carrier: any guard's refusal greps the same way and names its own guard."""
    for guard in ("launchctl-submit", "gateway-lifecycle", "gateway-lifecycle-scan",
                  "host-interpreter-kill", "self-repo", "locked-skill", "terminal"):
        payload = json.loads(_blocked_json("Blocked: detail", "blocked", guard=guard))
        assert payload["output"].splitlines() == [
            f"{GUARD_REFUSED_MARKER} {guard}: the command never ran"]
        assert payload["error"].splitlines()[0].startswith(GUARD_REFUSED_MARKER)
        assert payload["exit_code"] == GUARD_REFUSED_EXIT_CODE
