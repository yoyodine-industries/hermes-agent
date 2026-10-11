"""Global emergency stop (`hermes pause` / `hermes resume`) — agent/estop.py.

The ESTOP sentinel is a resumable pause for NEW work only: cron dispatch,
kanban dispatch, and new gateway turns are halted while it is engaged; work
already in flight is never touched. Removing the sentinel (`hermes resume`)
restores normal operation with no restart.

Ported from: gastownhall/gastown estop.go (MIT); related prior art: #26778
(/panic — kill/exit semantics, deliberately different) and #44617
(interrupt in-flight cron — deliberately NOT done here).
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

import pytest

from agent import estop


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Point HERMES_HOME at a temp dir and reset estop module log state."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    estop._logged_components.clear()
    return tmp_path


def _legacy_total(reason="ops incident", **extra) -> str:
    """Write the PRE-RULING total-halt body, and return its handle.

    Arming a `mode: estop` hold is not a supported operation any more (operator ruling
    2026-10-10, card t_e04b4c81): the fleet-wide stop is retired, so no verb may create this
    state — including the tests. The GATE still has to behave on one, because a total hold can
    be sitting on disk from older bytes and the boundary statement calls it an alarm to LIFT,
    so these tests set that legacy state up the way a pre-ruling install left it (a whole-body
    sentinel, no registry, no provenance) and lift it BY HANDLE.
    """
    body = {"reason": reason, "engaged_at": datetime.now(UTC).isoformat()}
    body.update(extra)
    estop.sentinel_path().write_text(json.dumps(body) + "\n", encoding="utf-8")
    held = [hold for hold in estop.holds() if (hold.get("reason") or "") == reason]
    return held[0]["handle"] if held else ""


# ── sentinel create / remove ────────────────────────────────────────────────


def test_a_legacy_total_body_still_reads_as_a_hold(hermes_home):
    assert estop.is_engaged() is False
    _legacy_total("ops incident")
    assert (hermes_home / "ESTOP").exists()
    assert estop.is_engaged() is True


def test_the_alarm_is_lifted_by_handle(hermes_home):
    handle = _legacy_total("ops incident")
    assert estop.release(handle=handle).released is True
    assert not (hermes_home / "ESTOP").exists()
    assert estop.is_engaged() is False
    # A bare disengage has no hold of its own to lift, and reports that (never a silent success).
    assert estop.disengage() is False


def test_reason_and_timestamp_stored(hermes_home):
    _legacy_total("runaway cron fan-out")
    state = estop.get_state()
    assert state is not None
    assert state["reason"] == "runaway cron fan-out"
    assert state["engaged_at"]  # ISO timestamp string

    raw = json.loads((hermes_home / "ESTOP").read_text(encoding="utf-8"))
    assert raw["reason"] == "runaway cron fan-out"




def test_corrupt_sentinel_still_engages(hermes_home):
    """A hand-touched/corrupt ESTOP file must still pause (fail safe)."""
    (hermes_home / "ESTOP").write_text("not json", encoding="utf-8")
    assert estop.is_engaged() is True
    state = estop.get_state()
    assert state is not None
    assert state.get("reason") is None


# ── paused notice for new gateway turns ─────────────────────────────────────




def test_paused_reply_surfaces_reason_and_resume_hint(hermes_home):
    handle = _legacy_total("deploy window")
    notice = estop.paused_reply()
    assert notice is not None
    assert "paused" in notice.lower()
    assert "deploy window" in notice
    assert handle in notice, "the notice NAMES the hold, so it can be lifted by hand"




# ── check_paused: cheap gate + log-once ─────────────────────────────────────




# ── cron scheduler integration ──────────────────────────────────────────────


def test_cron_tick_skips_dispatch_when_engaged(hermes_home, monkeypatch):
    from cron import scheduler

    calls = []

    def _fake_get_due_jobs():
        calls.append(1)
        return []

    monkeypatch.setattr(scheduler, "get_due_jobs", _fake_get_due_jobs)

    _legacy_total("test")
    assert scheduler.tick(verbose=False) == 0
    assert calls == [], "engaged ESTOP must skip the due-job scan entirely"


def test_cron_tick_resumes_after_disengage(hermes_home, monkeypatch):
    from cron import scheduler

    calls = []

    def _fake_get_due_jobs():
        calls.append(1)
        return []

    monkeypatch.setattr(scheduler, "get_due_jobs", _fake_get_due_jobs)

    handle = _legacy_total("cron window")
    scheduler.tick(verbose=False)
    assert calls == []

    estop.release(handle=handle)
    scheduler.tick(verbose=False)
    assert calls == [1], "resume must restore normal cron dispatch"


# ── kanban dispatcher integration ───────────────────────────────────────────


def test_kanban_dispatch_blocked_when_engaged(hermes_home):
    from gateway.kanban_watchers_common import _kanban_dispatch_allowed

    assert _kanban_dispatch_allowed() is True
    handle = _legacy_total("test")
    assert _kanban_dispatch_allowed() is False
    estop.release(handle=handle)
    assert _kanban_dispatch_allowed() is True


# ── gateway turn-start integration ──────────────────────────────────────────


class _FakeSource:
    platform = None
    chat_id = "c1"
    user_id = "u1"
    user_name = "user"
    chat_type = "dm"
    profile = None


class _FakeEvent:
    internal = False
    text = "hello"

    def __init__(self):
        self.source = _FakeSource()

    def get_command(self) -> str | None:
        return None


@pytest.mark.asyncio
async def test_gateway_new_turn_gets_paused_reply(hermes_home):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._is_user_authorized = lambda source: True  # bare-instance stub
    _legacy_total("maintenance")
    reply = await runner._handle_message(_FakeEvent())
    assert reply is not None
    assert "paused" in reply.lower()
    assert "maintenance" in reply


@pytest.mark.asyncio
async def test_gateway_internal_events_bypass_estop(hermes_home):
    """Internal events (in-flight work completions) must NOT be paused."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    _legacy_total("maintenance")
    event = _FakeEvent()
    event.internal = True
    # An internal event proceeds past the estop gate; the bare runner then
    # blows up further down the pipeline on missing attributes — that error
    # (anything but a paused reply) proves the gate let it through.
    try:
        reply = await runner._handle_message(event)
    except Exception:
        return
    assert reply is None or "paused" not in (reply or "").lower()


# ── CLI: hermes pause / hermes resume ───────────────────────────────────────


def test_cli_pause_refuses_the_fleet_wide_form_and_writes_nothing(hermes_home, capsys):
    from hermes_cli.subcommands.pause import cmd_pause

    rc = cmd_pause(argparse.Namespace(reason="ops incident"))
    assert rc == 2, "a bare `hermes pause` is not a supported operation"
    assert estop.is_engaged() is False
    out = capsys.readouterr().out
    assert "set-dispatch" in out and "cron pause" in out and "--lockdown" in out


def test_cli_pause_refuses_idempotently(hermes_home, capsys):
    from hermes_cli.subcommands.pause import cmd_pause

    assert cmd_pause(argparse.Namespace(reason=None)) == 2
    assert cmd_pause(argparse.Namespace(reason=None)) == 2
    assert estop.is_engaged() is False
    assert not estop.sentinel_path().exists()


def test_cli_resume_lifts_the_hold_by_handle(hermes_home, capsys):
    from hermes_cli.subcommands.pause import cmd_resume

    handle = _legacy_total("deploy window")
    rc = cmd_resume(argparse.Namespace(handle=handle))
    assert rc == 0
    assert estop.is_engaged() is False
    assert "released" in capsys.readouterr().out.lower()






# ── hermes status surfacing ─────────────────────────────────────────────────


def test_status_line_when_held_names_the_scope(hermes_home):
    from hermes_cli.status import _estop_status_line

    assert _estop_status_line() is None
    handle = _legacy_total("ops")
    line = _estop_status_line()
    assert line is not None
    assert "HOLD" in line and handle in line
    assert "ops" in line
    assert "global emergency stop" not in line, "the status line names the SCOPE, not the fleet"
    estop.release(handle=handle)
    assert _estop_status_line() is None


# ── post-merge audit fixes (#81148 follow-up) ───────────────────────────────


def test_is_engaged_fails_safe_on_stat_error(hermes_home, monkeypatch):
    """A stat failure must report ENGAGED (fail safe) — the pause has to
    hold even when HERMES_HOME is misbehaving, matching the module's
    corrupt-sentinel doctrine."""
    class _BoomPath:
        def exists(self):
            raise OSError("permission denied")

    monkeypatch.setattr(estop, "sentinel_path", lambda: _BoomPath())
    assert estop.is_engaged() is True


class _FakeCmdEvent(_FakeEvent):
    text = "/status"

    def get_command(self):
        return "status"

    def get_command_args(self):
        return ""


@pytest.mark.asyncio
async def test_gateway_slash_commands_bypass_estop(hermes_home):
    """Recognized slash commands must pass the estop gate — /pause off is
    the in-band resume path for messaging-only users, and /status, /help
    and friends must keep working while paused."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._is_user_authorized = lambda source: True
    _legacy_total("maintenance")
    # The command proceeds past the estop gate; the bare runner then blows
    # up further down on missing attributes — anything but the hold
    # notice proves the gate let it through.
    try:
        reply = await runner._handle_message(_FakeCmdEvent())
    except Exception:
        return
    assert reply is None or "LEGACY fleet-wide halt" not in (reply or "")


class _FakePauseEvent(_FakeEvent):
    def __init__(self, args=""):
        super().__init__()
        self._args = args
        self.text = f"/pause {args}".strip()

    def get_command(self):
        return "pause"

    def get_command_args(self):
        return self._args


@pytest.mark.asyncio
async def test_gateway_pause_command_refuses_and_pause_off_is_the_release(hermes_home):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)

    # THE ARM DIRECTION IS RETIRED: the unscoped (and only) form refuses, naming the controls.
    reply = await runner._handle_pause_command(_FakePauseEvent("deploy window"))
    assert "set-dispatch" in reply and "cron pause" in reply and "lockdown" in reply
    assert estop.is_engaged() is False

    # `/pause off` still lifts the CALLER's OWN hold.
    handle = estop.arm(owner="wind-down", mode=estop.MODE_LOCKDOWN, ttl="30m",
                       allow={"profiles": ["platform-coder"]}).handle
    reply = await runner._handle_pause_command(_FakePauseEvent("off"))
    assert "resumed" in reply.lower()
    assert estop.is_engaged() is False
    assert handle

    reply = await runner._handle_pause_command(_FakePauseEvent("off"))
    assert "no hold is in force" in reply.lower()


def test_pause_command_registered_for_gateway():
    from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, resolve_command

    cmd = resolve_command("pause")
    assert cmd is not None and cmd.name == "pause"
    assert "pause" in GATEWAY_KNOWN_COMMANDS
    # Must be dispatchable while an agent is running (in-band emergency stop).
    assert cmd.busy_policy == "dispatch"


def test_profile_gateway_honors_canonical_root_estop(tmp_path, monkeypatch):
    """fleet-analyst-class: HERMES_HOME is a profile dir; pause lives at root.

    A process launched with HERMES_HOME=~/.hermes/profiles/fleet-analyst must
    still treat ~/.hermes/ESTOP as engaged. Otherwise `hermes pause` is not
    a global emergency stop (t_7b65ff88).
    """
    root = tmp_path / "hermes-root"
    profile = root / "profiles" / "fleet-analyst"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    estop._logged_components.clear()

    assert estop.is_engaged() is False
    (root / "ESTOP").write_text("{\"reason\": \"thundering herd\"}\n", encoding="utf-8")
    assert estop.is_engaged() is True
    assert estop.paused_reply() is not None
    assert "fleet-wide halt" in estop.paused_reply()
    # A profile-local hold is independent of the root's — and is armed through the ENGINE (the
    # only hold shape left is a scoped one).
    handle = estop.arm(owner="wind-down", mode=estop.MODE_LOCKDOWN, ttl="30m",
                       allow={"profiles": ["fleet-analyst"]}).handle
    assert (profile / "ESTOP").exists()
    assert estop.is_engaged() is True
    (root / "ESTOP").unlink()
    assert estop.is_engaged() is True  # still held by profile sentinel
    assert estop.release(handle=handle).released is True
    assert estop.is_engaged() is False
