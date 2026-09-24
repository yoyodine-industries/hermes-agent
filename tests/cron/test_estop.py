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
import logging
from datetime import datetime, timedelta, timezone

import pytest

from agent import estop


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Point HERMES_HOME at a temp dir and reset estop module log state."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    estop._logged_components.clear()
    return tmp_path


# ── sentinel create / remove ────────────────────────────────────────────────


def test_engage_creates_sentinel_and_is_engaged(hermes_home):
    assert estop.is_engaged() is False
    estop.engage()
    assert (hermes_home / "ESTOP").exists()
    assert estop.is_engaged() is True


def test_disengage_removes_sentinel(hermes_home):
    estop.engage()
    assert estop.disengage() is True
    assert not (hermes_home / "ESTOP").exists()
    assert estop.is_engaged() is False
    # Disengaging when not engaged is a no-op that reports False.
    assert estop.disengage() is False


def test_reason_and_timestamp_stored(hermes_home):
    estop.engage(reason="runaway cron fan-out")
    state = estop.get_state()
    assert state is not None
    assert state["reason"] == "runaway cron fan-out"
    assert state["engaged_at"]  # ISO timestamp string

    raw = json.loads((hermes_home / "ESTOP").read_text(encoding="utf-8"))
    assert raw["reason"] == "runaway cron fan-out"


def test_get_state_none_when_disengaged(hermes_home):
    assert estop.get_state() is None


def test_corrupt_sentinel_still_engages(hermes_home):
    """A hand-touched/corrupt ESTOP file must still pause (fail safe)."""
    (hermes_home / "ESTOP").write_text("not json", encoding="utf-8")
    assert estop.is_engaged() is True
    state = estop.get_state()
    assert state is not None
    assert state.get("reason") is None


# ── paused notice for new gateway turns ─────────────────────────────────────


def test_paused_reply_none_when_disengaged(hermes_home):
    assert estop.paused_reply() is None


def test_paused_reply_surfaces_reason_and_resume_hint(hermes_home):
    estop.engage(reason="deploy window")
    notice = estop.paused_reply()
    assert notice is not None
    assert "paused" in notice.lower()
    assert "deploy window" in notice
    assert "hermes resume" in notice


def test_paused_reply_without_reason(hermes_home):
    estop.engage()
    notice = estop.paused_reply()
    assert notice is not None
    assert "paused" in notice.lower()
    assert "hermes resume" in notice


# ── check_paused: cheap gate + log-once ─────────────────────────────────────


def test_check_paused_logs_once_per_engagement(hermes_home, caplog):
    logger = logging.getLogger("test.estop.component")
    estop.engage()
    with caplog.at_level(logging.INFO, logger=logger.name):
        assert estop.check_paused("cron", logger) is True
        assert estop.check_paused("cron", logger) is True
        assert estop.check_paused("cron", logger) is True
    paused_logs = [r for r in caplog.records if "paused" in r.getMessage().lower()]
    assert len(paused_logs) == 1

    # Resume then re-engage → logs once more (transition-based, not forever).
    caplog.clear()
    estop.disengage()
    with caplog.at_level(logging.INFO, logger=logger.name):
        assert estop.check_paused("cron", logger) is False
        estop.engage()
        assert estop.check_paused("cron", logger) is True
        assert estop.check_paused("cron", logger) is True
    paused_logs = [r for r in caplog.records if "paused" in r.getMessage().lower()]
    assert len(paused_logs) == 1


# ── cron scheduler integration ──────────────────────────────────────────────


def test_cron_tick_skips_dispatch_when_engaged(hermes_home, monkeypatch):
    from cron import scheduler

    calls = []

    def _fake_get_due_jobs():
        calls.append(1)
        return []

    monkeypatch.setattr(scheduler, "get_due_jobs", _fake_get_due_jobs)

    estop.engage(reason="test")
    assert scheduler.tick(verbose=False) == 0
    assert calls == [], "engaged ESTOP must skip the due-job scan entirely"


def test_cron_tick_resumes_after_disengage(hermes_home, monkeypatch):
    from cron import scheduler

    calls = []

    def _fake_get_due_jobs():
        calls.append(1)
        return []

    monkeypatch.setattr(scheduler, "get_due_jobs", _fake_get_due_jobs)

    estop.engage()
    scheduler.tick(verbose=False)
    assert calls == []

    estop.disengage()
    scheduler.tick(verbose=False)
    assert calls == [1], "resume must restore normal cron dispatch"


# ── kanban dispatcher integration ───────────────────────────────────────────


def test_kanban_dispatch_blocked_when_engaged(hermes_home):
    from gateway.kanban_watchers_common import _kanban_dispatch_allowed

    assert _kanban_dispatch_allowed() is True
    estop.engage(reason="test")
    assert _kanban_dispatch_allowed() is False
    estop.disengage()
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


@pytest.mark.asyncio
async def test_gateway_new_turn_gets_paused_reply(hermes_home):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._is_user_authorized = lambda source: True  # bare-instance stub
    estop.engage(reason="maintenance")
    reply = await runner._handle_message(_FakeEvent())
    assert reply is not None
    assert "paused" in reply.lower()
    assert "maintenance" in reply


@pytest.mark.asyncio
async def test_gateway_internal_events_bypass_estop(hermes_home):
    """Internal events (in-flight work completions) must NOT be paused."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    estop.engage()
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


def test_cli_pause_engages_with_reason(hermes_home, capsys):
    from hermes_cli.subcommands.pause import cmd_pause

    rc = cmd_pause(argparse.Namespace(reason="ops incident"))
    assert rc == 0
    assert estop.is_engaged() is True
    assert estop.get_state()["reason"] == "ops incident"
    assert "paused" in capsys.readouterr().out.lower()


def test_cli_pause_idempotent(hermes_home, capsys):
    from hermes_cli.subcommands.pause import cmd_pause

    assert cmd_pause(argparse.Namespace(reason=None)) == 0
    assert cmd_pause(argparse.Namespace(reason=None)) == 0
    assert estop.is_engaged() is True


def test_cli_resume_disengages(hermes_home, capsys):
    from hermes_cli.subcommands.pause import cmd_pause, cmd_resume

    cmd_pause(argparse.Namespace(reason=None))
    rc = cmd_resume(argparse.Namespace())
    assert rc == 0
    assert estop.is_engaged() is False
    assert "resumed" in capsys.readouterr().out.lower()


def test_cli_resume_when_not_paused(hermes_home, capsys):
    from hermes_cli.subcommands.pause import cmd_resume

    rc = cmd_resume(argparse.Namespace())
    assert rc == 0
    assert "not paused" in capsys.readouterr().out.lower()


def test_builtin_subcommands_include_pause_resume():
    from hermes_cli.main import _BUILTIN_SUBCOMMANDS

    assert "pause" in _BUILTIN_SUBCOMMANDS
    assert "resume" in _BUILTIN_SUBCOMMANDS


# ── hermes status surfacing ─────────────────────────────────────────────────


def test_status_line_when_paused(hermes_home):
    from hermes_cli.status import _estop_status_line

    assert _estop_status_line() is None
    estop.engage(reason="ops")
    line = _estop_status_line()
    assert line is not None
    assert "paused" in line.lower()
    assert "ops" in line
    estop.disengage()
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
    estop.engage(reason="maintenance")
    # The command proceeds past the estop gate; the bare runner then blows
    # up further down on missing attributes — anything but the paused
    # notice proves the gate let it through.
    try:
        reply = await runner._handle_message(_FakeCmdEvent())
    except Exception:
        return
    assert reply is None or "hermes is paused" not in (reply or "").lower()


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
async def test_gateway_pause_command_engages_and_resumes(hermes_home):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)

    reply = await runner._handle_pause_command(_FakePauseEvent("deploy window"))
    assert "paused" in reply.lower()
    assert estop.is_engaged() is True
    assert estop.get_state()["reason"] == "deploy window"

    # Re-issuing without args reports already-paused instead of clobbering.
    reply = await runner._handle_pause_command(_FakePauseEvent(""))
    assert "already paused" in reply.lower()

    reply = await runner._handle_pause_command(_FakePauseEvent("off"))
    assert "resumed" in reply.lower()
    assert estop.is_engaged() is False

    reply = await runner._handle_pause_command(_FakePauseEvent("off"))
    assert "wasn't paused" in reply.lower()


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
    assert "paused" in estop.paused_reply().lower()
    # Profile-local engage still works and is independent.
    estop.engage(reason="local")
    assert (profile / "ESTOP").exists()
    assert estop.is_engaged() is True
    (root / "ESTOP").unlink()
    assert estop.is_engaged() is True  # still held by profile sentinel
    estop.disengage()
    assert estop.is_engaged() is False


# ── single-user mode: allowlist + deadman TTL ────────────────────────────────

OPERATOR = "operator-uid-7"


def _stamp(offset_seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


@pytest.mark.parametrize(
    "value,expected",
    [("45m", 2700), ("90m", 5400), ("2h", 7200), ("15s", 15), ("90", 90),
     (45, 45), ("", None), ("banana", None), (None, None), (0, None), (True, None)],
)
def test_parse_duration_contract(value, expected):
    assert estop.parse_duration(value) == expected


def test_expired_sentinel_is_not_engaged_and_logs_one_loud_line(hermes_home, caplog):
    """The deadman: a window job that dies between arm and release must not strand the
    fleet — past expires_at the pause is gone, reported ONCE, and the dead file retired."""
    estop.engage(reason="update window", ttl="1s")
    assert estop.is_engaged() is True

    (hermes_home / "ESTOP").write_text(
        json.dumps({"reason": "update window", "expires_at": _stamp(-30)}), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert [estop.is_engaged() for _ in range(4)] == [False, False, False, False]
    assert len([r for r in caplog.records if "EXPIRED" in r.getMessage()]) == 1
    assert estop.paused_reply() is None
    assert not (hermes_home / "ESTOP").exists(), "the expired sentinel must not be left behind"

    # A fresh engagement is a fresh event: arm, expire, and it reports again (not swallowed
    # by the previous engagement's "already reported" state).
    estop.engage(reason="second window", ttl="1s")
    (hermes_home / "ESTOP").write_text(json.dumps({"expires_at": _stamp(-5)}), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert estop.is_engaged() is False
    assert len([r for r in caplog.records if "EXPIRED" in r.getMessage()]) == 2


def test_expiry_reaches_the_cron_consumer(hermes_home, monkeypatch):
    """Every consumer gates on is_engaged(), so the deadman must lift cron dispatch too."""
    from cron import scheduler

    calls = []

    def _fake_get_due_jobs():
        calls.append(1)
        return []

    monkeypatch.setattr(scheduler, "get_due_jobs", _fake_get_due_jobs)
    estop.engage(ttl="1s")
    scheduler.tick(verbose=False)
    assert calls == []

    (hermes_home / "ESTOP").write_text(json.dumps({"expires_at": _stamp(-1)}), encoding="utf-8")
    scheduler.tick(verbose=False)
    assert calls == [1], "the deadman must lift the hold, not just the gateway notice"


def test_unreadable_expiry_stays_engaged(hermes_home):
    """Fail safe both ways: an empty (touched) file and a junk stamp both still pause."""
    (hermes_home / "ESTOP").write_text("", encoding="utf-8")
    assert estop.is_engaged() is True

    (hermes_home / "ESTOP").write_text(json.dumps({"expires_at": "not-a-date"}), encoding="utf-8")
    assert estop.is_engaged() is True


def test_ttl_and_allowlist_round_trip_through_the_sentinel(hermes_home):
    estop.engage(reason="window", allow={"user_ids": [OPERATOR], "profiles": ["platform-stl"]}, ttl="45m")

    state = estop.get_state()
    assert state["allow"] == {"user_ids": [OPERATOR], "profiles": ["platform-stl"]}
    remaining = (datetime.fromisoformat(state["expires_at"]) - datetime.now(timezone.utc)).total_seconds()
    assert 40 * 60 < remaining <= 45 * 60, "--ttl must be honored as a wall-clock deadline"


def test_is_allowed_reads_the_allowlist_by_identity_then_profile(hermes_home):
    estop.engage(allow={"user_ids": [OPERATOR], "profiles": ["platform-stl"]})

    assert estop.is_allowed(OPERATOR) is True
    assert estop.is_allowed("someone-else", "platform-stl") is True
    assert estop.is_allowed("someone-else", "other-lane") is False
    estop.disengage()
    assert estop.is_allowed(OPERATOR) is False, "no sentinel admits nobody"


def test_cli_pause_arms_ttl_and_allowlist(hermes_home, capsys):
    from hermes_cli.subcommands.pause import cmd_pause

    rc = cmd_pause(argparse.Namespace(
        reason="update window", allow_user=[OPERATOR], allow_profile=["platform-stl"], ttl="45m"))
    assert rc == 0
    state = estop.get_state()
    assert state["allow"] == {"user_ids": [OPERATOR], "profiles": ["platform-stl"]}
    assert state["expires_at"]
    out = capsys.readouterr().out
    assert OPERATOR in out and "platform-stl" in out and "deadman" in out


def test_cli_pause_refuses_an_unusable_ttl_without_arming(hermes_home, capsys):
    from hermes_cli.subcommands.pause import cmd_pause

    rc = cmd_pause(argparse.Namespace(
        reason=None, allow_user=None, allow_profile=None, ttl="soon"))
    assert rc == 2
    assert estop.is_engaged() is False, "a rejected --ttl must not leave a half-armed pause"
    assert "Invalid" in capsys.readouterr().out


def test_status_line_renders_deadman_and_allowlist(hermes_home):
    from hermes_cli.status import _estop_status_line

    estop.engage(reason="ops", allow={"user_ids": [OPERATOR]}, ttl="45m")
    line = _estop_status_line()
    assert "ops" in line and OPERATOR in line and "deadman" in line


# ── run identity + release cause (t_5abfda01) ────────────────────────────────
#
# A hold that comes back is worth exactly as much as the record of HOW it came back:
# `lease_expiry` means the releasing run died before its exit path ran, and that is the one
# state worth paging on. So the sentinel names the run that armed it, and every release
# (deadman or caller) writes a dated record beside the sentinel it removed.

RUN_ID = "20260923-2200-winddown"
SECOND_RUN = "20260923-2300-winddown"


def test_run_id_round_trips_through_the_sentinel(hermes_home):
    """"Which run armed this" must outlive the arming process — it is read off the sentinel."""
    estop.engage(reason="update window", run_id=RUN_ID, ttl="45m")

    assert estop.get_state()["run_id"] == RUN_ID
    raw = json.loads((hermes_home / "ESTOP").read_text(encoding="utf-8"))
    assert raw["run_id"] == RUN_ID


def test_unidentified_arm_reports_no_run_rather_than_a_wrong_one(hermes_home):
    """A hand-armed pause has no run; recording a stand-in would be a false provenance."""
    estop.engage(reason="manual")
    assert estop.get_state()["run_id"] is None


def test_no_release_record_before_any_pause(hermes_home):
    """The record is written by a RELEASE — a home that never held a pause reports nothing."""
    assert estop.get_last_release() is None


def test_release_records_the_cause_and_the_arming_run(hermes_home):
    estop.engage(reason="update window", run_id=RUN_ID, ttl="45m")

    assert estop.disengage(released_by="node") is True
    record = estop.get_last_release()
    assert record["released_by"] == "node"
    assert record["run_id"] == RUN_ID
    assert record["reason"] == "update window"
    assert record["engaged_at"] and record["released_at"]
    assert estop.is_engaged() is False


def test_deadman_expiry_records_lease_expiry(hermes_home):
    """The state worth paging on: the holder died and never reached its release."""
    estop.engage(reason="update window", run_id=RUN_ID, ttl="45m")
    (hermes_home / "ESTOP").write_text(
        json.dumps({"reason": "update window", "run_id": RUN_ID, "expires_at": _stamp(-30)}),
        encoding="utf-8")

    assert estop.is_engaged() is False
    record = estop.get_last_release()
    assert record["released_by"] == "lease_expiry"
    assert record["run_id"] == RUN_ID


def test_rearm_between_the_expiry_read_and_the_recheck_keeps_the_hold(hermes_home, monkeypatch):
    """The retire's own re-check exists so a re-arm landing mid-retire loses nothing — and a
    hold that is still live must not be reported as a deadman return either."""
    estop.engage(reason="update window", run_id=RUN_ID, ttl="45m")
    sentinel = hermes_home / "ESTOP"
    sentinel.write_text(
        json.dumps({"reason": "update window", "run_id": RUN_ID, "expires_at": _stamp(-30)}),
        encoding="utf-8")

    real_read = estop._read_payload
    rearmed = []

    def _read_dead_then_rearm(path):
        payload = real_read(path)
        if not rearmed:  # the re-arm lands between this read and the retire's key re-check
            rearmed.append(1)
            estop.engage(reason="second window", run_id=SECOND_RUN, ttl="45m")
        return payload

    monkeypatch.setattr(estop, "_read_payload", _read_dead_then_rearm)
    estop._retire_expired(sentinel)

    assert sentinel.exists(), "the re-armed hold must survive the deadman's retire"
    assert estop.get_last_release() is None, (
        "no hold came back here: a lease_expiry record would report a deadman return that "
        "never happened")
    assert estop.is_engaged() is True
    assert estop.get_state()["run_id"] == SECOND_RUN


def test_a_caller_cannot_claim_lease_expiry(hermes_home):
    """Only the deadman may write the paging state, and a refused cause must lift NOTHING."""
    estop.engage(ttl="45m")

    with pytest.raises(ValueError):
        estop.disengage(released_by="lease_expiry")
    assert estop.is_engaged() is True
    assert estop.get_last_release() is None


def test_release_records_land_beside_every_sentinel_removed(tmp_path, monkeypatch):
    """A profile-home release also lifts the fleet root, so a reader at EITHER path sees it."""
    root = tmp_path / "hermes-root"
    profile = root / "profiles" / "ops"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    estop._logged_components.clear()
    estop.engage(run_id=RUN_ID)
    (root / "ESTOP").write_text(json.dumps({"run_id": "root-run"}), encoding="utf-8")

    assert estop.disengage(released_by="node") is True
    assert (profile / "ESTOP.release.json").exists()
    assert (root / "ESTOP.release.json").exists()


def test_cli_pause_records_the_arming_run(hermes_home, capsys):
    from hermes_cli.subcommands.pause import cmd_pause

    rc = cmd_pause(argparse.Namespace(
        reason="update window", allow_user=None, allow_profile=None, ttl="45m", run_id=RUN_ID))
    assert rc == 0
    assert estop.get_state()["run_id"] == RUN_ID
    assert RUN_ID in capsys.readouterr().out


def test_cli_resume_records_its_cause(hermes_home, capsys):
    from hermes_cli.subcommands.pause import cmd_resume

    estop.engage(run_id=RUN_ID, ttl="45m")
    assert cmd_resume(argparse.Namespace(by="on_exit")) == 0
    record = estop.get_last_release()
    assert record["released_by"] == "on_exit" and record["run_id"] == RUN_ID


def test_cli_resume_defaults_to_manual(hermes_home, capsys):
    """A bare `hermes resume` is not a node — claiming one would hide the exit-path signal."""
    from hermes_cli.subcommands.pause import cmd_resume

    estop.engage(ttl="45m")
    assert cmd_resume(argparse.Namespace()) == 0
    assert estop.get_last_release()["released_by"] == "manual"


def test_cli_resume_rejects_an_unknown_cause_without_lifting(hermes_home, capsys):
    from hermes_cli.subcommands.pause import cmd_resume

    estop.engage(ttl="45m")
    assert cmd_resume(argparse.Namespace(by="banana")) == 2
    assert estop.is_engaged() is True, "a rejected --by must not half-release the pause"
    assert "Invalid" in capsys.readouterr().out


def test_status_line_surfaces_a_deadman_release(hermes_home):
    """`hermes status` is where an operator looks: a deadman return must not look clean."""
    from hermes_cli.status import _estop_last_release_line

    estop.engage(run_id=RUN_ID, ttl="45m")
    (hermes_home / "ESTOP").write_text(
        json.dumps({"run_id": RUN_ID, "expires_at": _stamp(-30)}), encoding="utf-8")

    assert estop.is_engaged() is False
    line = _estop_last_release_line()
    assert RUN_ID in line and "lease expiry" in line


def test_status_line_reports_no_release_on_a_clean_home(hermes_home):
    from hermes_cli.status import _estop_last_release_line

    assert _estop_last_release_line() is None
