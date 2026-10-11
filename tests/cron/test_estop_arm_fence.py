"""The ARMLIFT venue fence at the HOLD — ``agent/estop.py::arm_admission``.

The fence used to exist only on ``hermes pause``'s argument surface, so any OTHER caller of
the emergency stop — the gateway's ``/pause`` path (``gateway/run_busy.py`` -> ``engage``),
or any python import calling ``acquire()``/``release()`` — could arm or lift from a fenced
context (a delegated child, a dispatched kanban worker). These tests pin the fence to the
hold itself, and pin the zero-byte-sentinel contract that goes with it: a bare ``touch`` is
an ALARM, not a pause.
"""

from __future__ import annotations

import argparse
import json
import logging

import pytest

import agent.delegation_context as delegation_context
from agent import estop


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Point HERMES_HOME at a temp dir and reset estop module log state.

    The session is declared an interactive OPERATOR one (holder attribution): a hold is
    recorded under the name its actor is entitled to wear, and only that venue may wear
    ``operator``. Without it these tests would be unattended callers and their named holders
    would be recorded under the actor instead.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_ESTOP_ACTOR", "operator")
    monkeypatch.setattr(estop, "_stdin_is_tty", lambda: True)
    estop._logged_components.clear()
    estop._expired_logged.clear()
    estop._unattributed_logged.clear()
    return tmp_path


def _fence(monkeypatch, *, delegated=False, task=""):
    """Make ``arm_admission``'s two probes answer as told.

    It imports both helpers from ``agent.delegation_context`` at call time, so patching the
    MODULE ATTRIBUTES is what the fence actually sees.
    """
    monkeypatch.setattr(
        delegation_context, "is_delegated_child_process_context", lambda: delegated)
    monkeypatch.setattr(delegation_context, "owned_kanban_task", lambda: task)


def _pause_args(**overrides):
    args = dict(reason=None, lockdown=False, allow_profile=None, allow_user=None, ttl=None)
    args.update(overrides)
    return argparse.Namespace(**args)


# ── the fence, at the hold ──────────────────────────────────────────────────


def test_acquire_from_a_delegated_child_is_refused(hermes_home, monkeypatch):
    """A delegated child may not arm: the fence is on ``acquire``, not on a CLI flag."""
    _fence(monkeypatch, delegated=True)

    with pytest.raises(estop.EstopRefusal) as excinfo:
        estop.acquire(reason="probe")

    assert "Refusing to arm the emergency stop" in str(excinfo.value)
    assert "delegated child context" in str(excinfo.value)
    assert not (hermes_home / "ESTOP").exists(), "a refused arm must leave no sentinel"


def test_acquire_from_a_dispatched_worker_is_refused(hermes_home, monkeypatch):
    """The worker whose card a hold would starve can never be the thing that arms it."""
    _fence(monkeypatch, delegated=False, task="t_probe")

    with pytest.raises(estop.EstopRefusal) as excinfo:
        estop.acquire(reason="probe")

    assert "kanban task t_probe" in str(excinfo.value)
    assert not (hermes_home / "ESTOP").exists()


def test_engage_from_a_delegated_child_is_refused(hermes_home, monkeypatch):
    """``engage`` is the gateway's ``/pause`` path (gateway/run_busy.py) — fenced there too."""
    _fence(monkeypatch, delegated=True)

    with pytest.raises(estop.EstopRefusal):
        estop.engage(reason="gateway /pause")

    assert not (hermes_home / "ESTOP").exists()


def test_release_from_a_delegated_child_is_refused_and_the_hold_stands(hermes_home, monkeypatch):
    """A fenced caller cannot LIFT either — and a raw refusal leaves the live hold in force."""
    handle = estop.acquire(reason="ops")
    assert estop.is_engaged() is True

    _fence(monkeypatch, delegated=True)

    with pytest.raises(estop.EstopRefusal) as excinfo:
        estop.release(handle=handle)

    assert "Refusing to lift a hold" in str(excinfo.value)
    assert estop.is_engaged() is True, "a refused lift must never clear the pause"


def test_an_admitted_caller_arms_and_the_hold_carries_its_venue(hermes_home, monkeypatch):
    """Both probes negative -> interactive: the arm succeeds and the venue is recorded."""
    _fence(monkeypatch, delegated=False, task="")

    assert estop.arm_admission("arm") == (True, "interactive", None)
    estop.engage(reason="ops")

    holds = estop.read_state().holds
    assert len(holds) == 1
    assert holds[0]["owner"] == "operator"
    assert holds[0]["venue"] == "interactive"


def test_the_real_test_environment_is_an_admitted_venue(hermes_home):
    """No patch at all: an ordinary shell (no delegation/kanban env) is admitted and stamped."""
    assert estop.arm_admission() == (True, "interactive", None)

    handle = estop.acquire(reason="ops")
    assert estop.read_state().holds[0]["venue"] == "interactive"
    assert estop.release(handle=handle).released is True


# ── the zero-byte sentinel: an alarm, never a hold ──────────────────────────


def test_a_bare_touch_is_an_alarm_and_is_reported_once(hermes_home, caplog):
    """A zero-byte ``touch`` holds nothing, is left on disk, and says so exactly once."""
    sentinel = hermes_home / "ESTOP"
    sentinel.write_text("", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="agent.estop"):
        assert [estop.is_engaged() for _ in range(3)] == [False, False, False]
        assert estop.read_state().defect == estop.DEFECT_UNATTRIBUTED_EMPTY
        assert estop.get_state() is None, "nothing holds, so nothing is paused"

    assert sentinel.exists(), "the alarm file is the operator's to see, not ours to delete"
    reported = [r.getMessage() for r in caplog.records if "unattributed EMPTY sentinel" in r.getMessage()]
    assert len(reported) == 1, f"one loud line per engagement, got {reported}"


def test_a_non_empty_corrupt_body_still_holds(hermes_home):
    """Fail SAFE is untouched: a body we cannot parse is still a total halt."""
    (hermes_home / "ESTOP").write_text("not json", encoding="utf-8")

    assert estop.is_engaged() is True
    assert estop.read_state().defect == estop.DEFECT_UNREADABLE


def test_a_failed_registry_write_still_arms_with_an_attributed_body(hermes_home, monkeypatch):
    """The removed ``touch()`` last resort would now leave the fleet RUNNING (a zero-byte
    file is not a hold), so a failed registry write must land an ATTRIBUTED minimal body."""
    monkeypatch.setattr(estop, "_write_registry", lambda path, holds: False)

    handle = estop.acquire(reason="probe")

    assert estop.is_engaged() is True, "a failed registry write must never leave the fleet running"
    body = json.loads((hermes_home / "ESTOP").read_text(encoding="utf-8"))
    assert body["holds"][0]["handle"] == handle
    assert body["holds"][0]["owner"] == "operator"
    assert body["holds"][0]["venue"] == "interactive"
    assert estop.read_state().defect is None


# ── the CLI keeps its refusal shape (rc=3) if the hold refuses it ───────────


def test_cli_pause_reports_a_hold_layer_refusal_as_rc3(hermes_home, monkeypatch, capsys):
    """Backstop: a refusal raised by the HOLD itself is caught, printed with a leading ⛔.

    The hold-layer entry the CLI uses is ``agent.estop.arm`` (the facts-returning door;
    ``acquire`` is its ``-> handle`` back-compat wrapper), so that is what is made to refuse.
    """
    from hermes_cli.subcommands.pause import cmd_pause

    def _refuse(*args, **kwargs):
        raise estop.EstopRefusal(
            "Refusing to arm the emergency stop: this is a delegated child context.")

    monkeypatch.setattr(estop, "arm", _refuse)

    assert cmd_pause(_pause_args()) == 3
    assert capsys.readouterr().out.startswith("⛔ Refusing to arm the emergency stop:")
    assert not (hermes_home / "ESTOP").exists()


def test_cli_resume_reports_a_hold_layer_refusal_as_rc3(hermes_home, monkeypatch, capsys):
    """Backstop: a refused LIFT is rc=3 and the armed hold stays in force."""
    from hermes_cli.subcommands.pause import cmd_resume

    estop.engage(reason="ops")

    def _refuse(*args, **kwargs):
        raise estop.EstopRefusal("Refusing to lift a hold: this is a delegated child context.")

    monkeypatch.setattr(estop, "release", _refuse)

    assert cmd_resume(argparse.Namespace(handle=None, owner=None)) == 3
    assert capsys.readouterr().out.startswith("⛔ Refusing to lift a hold:")
    assert estop.is_engaged() is True
