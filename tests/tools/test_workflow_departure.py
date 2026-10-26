"""A workflow departure is detached: no turn, session or shutdown sweep may reap it (t_4b9843a4).

The defect: a yoyoflow run launched from a gateway-served turn was a child of that turn, so a
sanctioned bounce killed a live 32-minute production flight and left a bare
``interrupted (SystemExit(143))`` on its record. The fix has two halves, and both are pinned
here: the carrier invocation is DETECTED and spawned detached, and the sweep machinery refuses
to reap a departure even when it is handed one.
"""

import pytest

from tools import workflow_departure as wd
from tools.process_registry import ProcessRegistry, ProcessSession, _CHECKPOINT_FIELDS


# ---------------------------------------------------------------- detection / gate

@pytest.mark.parametrize("command", [
    "python3 -u -m yoyoflow.run --workflow sdlc --config x.yaml",
    "/opt/hermes_prod/yaan-platform/shared-venv/bin/python -m yoyoflow.run -m node",
    "bash /opt/hermes_prod/yaan-platform/cron/sdlc.sh",
    "sh cron/deploy-train.sh --now",
    "bash /opt/hermes_prod/yaan-platform/scripts/sdlc-shim.sh",
    "cd /opt/hermes_prod/yaan-platform && bash scripts/sdlc-admit-shim.sh",
])
def test_carrier_invocations_are_departures(command):
    assert wd.is_departure_command(command) is True


@pytest.mark.parametrize("command", [
    "",
    "ls -la",
    "grep -rn 'yoyoflow.run' /opt/hermes_prod",          # reading about a carrier
    "cat /opt/hermes_prod/yaan-platform/cron/sdlc.sh",     # inspecting a shim
    "tail -f /opt/hermes_prod/run_details/runner.log",
    "rg 'bash cron/' scripts/",
])
def test_reading_about_a_carrier_is_not_a_departure(command):
    assert wd.is_departure_command(command) is False


def test_detach_gate_is_on_by_default_and_can_be_killed(monkeypatch):
    monkeypatch.delenv(wd.ENV_GATE, raising=False)
    assert wd.detach_enabled() is True
    for value in ("0", "false", "NO", "off"):
        monkeypatch.setenv(wd.ENV_GATE, value)
        assert wd.detach_enabled() is False
    monkeypatch.setenv(wd.ENV_GATE, "1")
    assert wd.detach_enabled() is True


def test_marker_env_stamps_without_mutating_the_source():
    base = {"PATH": "/usr/bin"}
    marked = wd.marker_env(base)
    assert marked[wd.ENV_MARKER] == "1" and base == {"PATH": "/usr/bin"}
    assert wd.is_departure_env(marked) is True
    assert wd.is_departure_env(base) is False
    assert wd.is_departure_env({wd.ENV_MARKER: "0"}) is False


def test_patterns_are_overridable(monkeypatch, tmp_path):
    monkeypatch.setenv(wd.ENV_PATTERNS, r"my-carrier\.py")
    assert wd.is_departure_command("python my-carrier.py --tick") is True
    assert wd.is_departure_command("bash cron/sdlc.sh") is False
    spec = tmp_path / "patterns.txt"
    spec.write_text("# comment\nrun-flight\\.sh\n")
    monkeypatch.setenv(wd.ENV_PATTERNS, str(spec))
    assert wd.departure_patterns() == (r"run-flight\.sh",)


# ---------------------------------------------------------------- sweep exemptions

def _session(sid, *, departure=False, persist=False) -> ProcessSession:
    s = ProcessSession(id=sid, command="bash cron/sdlc.sh", task_id="session-a", started_at=0.0)
    s.departure = departure
    s.persist_on_release = persist
    return s


def _collect_kills(registry):
    calls = []

    def fake_kill(session_id, **kwargs):
        calls.append(session_id)
        registry._running[session_id].exited = True
        return {"status": "killed"}

    registry.kill_process = fake_kill
    return calls


@pytest.mark.parametrize("source", [
    "kill_all", "gateway_shutdown", "post-interrupt", "final-cleanup", "agent_close",
    "gateway_turn_timeout", "cli.stop", "test",
])
def test_no_sweep_reaps_a_departure(source):
    """Every bulk sweep -- shutdown included -- leaves a production flight alone."""
    registry = ProcessRegistry()
    departure = _session("proc_departure", departure=True)
    volatile = _session("proc_volatile")
    registry._running[departure.id] = departure
    registry._running[volatile.id] = volatile
    calls = _collect_kills(registry)

    assert registry.kill_all("session-a", source=source) == 1
    assert calls == ["proc_volatile"]
    assert departure.exited is False


def test_persist_on_release_alone_still_dies_at_shutdown():
    """The pre-existing rule is unchanged: persist survives lifecycle, not a bounce."""
    registry = ProcessRegistry()
    persisted = _session("proc_persisted", persist=True)
    registry._running[persisted.id] = persisted
    calls = _collect_kills(registry)
    assert registry.kill_all("session-a", source="gateway_shutdown") == 1
    assert calls == ["proc_persisted"]


def test_departure_flag_is_carried_in_the_crash_checkpoint():
    assert "departure" in _CHECKPOINT_FIELDS


# ---------------------------------------------------------------- tool routing

def test_planner_detaches_a_carrier_invocation(monkeypatch):
    from tools.terminal_tool import _plan_execution

    monkeypatch.delenv(wd.ENV_GATE, raising=False)
    plan = _plan_execution("bash cron/sdlc.sh", task_id=None, timeout=None,
                           background=False, _host_local=True)
    assert plan.departure is True


def test_planner_leaves_ordinary_commands_foreground(monkeypatch):
    from tools.terminal_tool import _plan_execution

    monkeypatch.delenv(wd.ENV_GATE, raising=False)
    plan = _plan_execution("ls -la", task_id=None, timeout=None,
                           background=False, _host_local=True)
    assert plan.departure is False


def test_kill_switch_runs_the_departure_as_an_ordinary_command(monkeypatch):
    from tools.terminal_tool import _plan_execution

    monkeypatch.setenv(wd.ENV_GATE, "0")
    plan = _plan_execution("bash cron/sdlc.sh", task_id=None, timeout=None,
                           background=False, _host_local=True)
    assert plan.departure is False


def test_a_self_backgrounded_carrier_is_still_a_departure(monkeypatch):
    """terminal(background=True) on a carrier is the same flight: it must not be reapable."""
    from tools.terminal_tool import _plan_execution

    monkeypatch.delenv(wd.ENV_GATE, raising=False)
    plan = _plan_execution("bash cron/sdlc.sh", task_id=None, timeout=None,
                           background=True, _host_local=True)
    assert plan.departure is True


def test_departure_note_is_attached_and_errors_are_left_alone():
    import json

    from tools.terminal_tool import _with_departure_note

    ok = _with_departure_note(json.dumps({"output": "Background process started", "exit_code": 0}))
    data = json.loads(ok)
    assert data["departure"] is True and "WORKFLOW DEPARTURE" in data["departure_note"]

    err = json.dumps({"error": "boom"})
    assert _with_departure_note(err) == err
