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
    estop._lockdown_logged.clear()
    estop._last_holds.clear()
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




def test_corrupt_sentinel_still_engages(hermes_home):
    """A hand-touched/corrupt ESTOP file must still pause (fail safe)."""
    (hermes_home / "ESTOP").write_text("not json", encoding="utf-8")
    assert estop.is_engaged() is True
    state = estop.get_state()
    assert state is not None
    assert state.get("reason") is None


# ── paused notice for new gateway turns ─────────────────────────────────────




def test_paused_reply_surfaces_reason_and_resume_hint(hermes_home):
    estop.engage(reason="deploy window")
    notice = estop.paused_reply()
    assert notice is not None
    assert "paused" in notice.lower()
    assert "deploy window" in notice




# ── check_paused: cheap gate + log-once ─────────────────────────────────────


def test_resume_transition_is_logged_with_the_hold_and_how_it_ended(hermes_home, caplog):
    """Card t_f88ddb45 item 2: the counterpart to "dispatch paused" exists.

    When the sentinel clears the tick must say that dispatch RESUMED, name the hold that
    ended, and say how it ended -- before this the resume transition was silent, and a
    reader could not tell an orderly release from a hand-deleted sentinel.
    """
    estop.engage(reason="nightly-window")
    with caplog.at_level(logging.INFO):
        assert estop.check_paused("kanban", estop.logger) is True
    assert [r.getMessage() for r in caplog.records if "dispatch paused" in r.getMessage()], \
        "the pause line is still emitted"
    caplog.clear()
    with caplog.at_level(logging.INFO):
        estop.disengage()
        assert estop.check_paused("kanban", estop.logger) is False
    resumed = [r.getMessage() for r in caplog.records if "dispatch resumed" in r.getMessage()]
    assert resumed, "the resume transition must be logged: %s" % [r.getMessage() for r in caplog.records][-4:]
    assert "nightly-window" in resumed[-1], "the resume names the hold that ended: %s" % resumed[-1]
    assert "kanban" in resumed[-1], resumed[-1]


def test_resume_names_the_release_when_one_is_on_the_ledger(hermes_home, caplog):
    """A recorded release is reported as such -- "released by <actor> …", not out-of-band."""
    estop.engage(reason="window-7")
    with caplog.at_level(logging.INFO):
        estop.check_paused("cron", estop.logger)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        estop.disengage()
        assert estop.check_paused("cron", estop.logger) is False
    line = [r.getMessage() for r in caplog.records if "dispatch resumed" in r.getMessage()][-1]
    assert "released by" in line, line
    assert "window-7" in line, line
    assert "out-of-band" not in line, "a recorded release must not read as a hand-delete: %s" % line


def test_no_resume_line_when_nothing_was_paused(hermes_home, caplog):
    """The gate stays silent on a component it never paused (every normal tick)."""
    with caplog.at_level(logging.INFO):
        assert estop.check_paused("webhook", estop.logger) is False
        assert estop.check_paused("webhook", estop.logger) is False
    assert not [r for r in caplog.records if "dispatch resumed" in r.getMessage()]


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

    def get_command(self) -> str | None:
        return None


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
    """A bare (empty) file is an ALARM, not a hold; a junk stamp still pauses (fail safe)."""
    (hermes_home / "ESTOP").write_text("", encoding="utf-8")
    assert estop.is_engaged() is False, "a zero-byte sentinel is unattributed — never a hold"
    assert estop.read_state().defect == estop.DEFECT_UNATTRIBUTED_EMPTY
    assert (hermes_home / "ESTOP").exists(), "the alarm file is left for the operator to see"

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


# ── holder attribution, provenance and the re-arm order gate (D1–D12) ────────
#
# The ESTOP venue the design fixed: WHO armed a hold (D1/D3), whether the bytes on disk can
# be TRUSTED (D2/D5), what a resume may lift (D7), and whether a re-arm after the operator's
# resume carries authority (D8). These cases exercise the live line's own semantics; where
# the design text and the live line disagreed, the test says so in its docstring (case 11).


def _be_operator(monkeypatch):
    """Simulate an interactive OPERATOR session: the only venue that may wear 'operator'."""
    for name in (*estop.UNATTENDED_ENV_MARKERS, "HERMES_KANBAN_TASK",
                 "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_ESTOP_ACTOR", "operator")
    monkeypatch.setattr(estop, "_stdin_is_tty", lambda: True)


def _be_bot(monkeypatch, name="bot:probe"):
    """Simulate an unattended caller (a yoyoflow window, the gateway): D3 steps 3–4."""
    for env_name in (*estop.UNATTENDED_ENV_MARKERS, "HERMES_KANBAN_TASK",
                     "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setenv("HERMES_ESTOP_ACTOR", name)
    monkeypatch.setattr(estop, "_stdin_is_tty", lambda: False)


@pytest.fixture
def as_operator(monkeypatch):
    _be_operator(monkeypatch)
    return "operator"


@pytest.fixture
def as_bot(monkeypatch):
    _be_bot(monkeypatch)
    return "bot:probe"


def _namespace(**overrides):
    """A `hermes pause`/`resume` namespace with every flag those verbs read."""
    base = {
        "reason": None, "allow_user": None, "allow_profile": None, "ttl": None,
        "lockdown": False, "order": None, "json": False,
        "handle": None, "owner": None, "all": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _body(hermes_home):
    return json.loads((hermes_home / "ESTOP").read_text(encoding="utf-8"))


def _entry(hermes_home, owner):
    return next(e for e in _body(hermes_home)["holds"] if e.get("owner") == owner)


def _hold(state, owner):
    return next(h for h in state.holds if h["owner"] == owner)


def test_a_release_lifts_only_the_hold_it_names(hermes_home, monkeypatch):
    """Case 1 — two holders, both directions, and `is_engaged()` while anything holds.

    A release is bound to the exact ENTRY (its handle), not to `HERMES_HOME` and not to the
    caller's idea of who holds what: the operator's hold survives a co-holder's release, the
    co-holder's survives the operator's, and a hold with no valid provenance survives both
    (only the operator's OWN resume clears it). The surviving entry must come through the
    rewrite byte-identical — a release that invalidates a co-holder's signature would widen a
    scoped lockdown into a total halt.
    """
    _be_operator(monkeypatch)
    operator_handle = estop.arm(owner="operator", reason="operator stop").handle
    _be_bot(monkeypatch, "bot:window")
    window_handle = estop.arm(owner="ops-head", reason="window").handle

    # (a) the operator releases the WINDOW's hold: its own survives, still verified.
    released = estop.release(handle=window_handle)
    assert released.released is True and released.handles == (window_handle,)
    assert estop.is_engaged() is True, "one hold released, another still holds the fleet"
    state = estop.read_state()
    assert [h["handle"] for h in state.holds] == [operator_handle]
    assert _hold(state, "operator")["verified"] is True, "provenance survives the rewrite"

    # (b) the mirror: the window releases the operator's hold? Refused (D7) — see case 6.
    #     What it CAN do is release its OWN entry, by the actor name its arm was recorded
    #     under (D4), and the operator's entry is untouched.
    _be_operator(monkeypatch)
    estop.arm(owner="operator", reason="operator stop again", replace_owner=True)
    _be_bot(monkeypatch, "bot:window")
    estop.arm(owner="ops-head", reason="window again")
    assert estop.read_state().engaged is True
    assert _entry(hermes_home, "bot:window") is not None, "the co-holder's entry is on disk"
    released = estop.release(owner="bot:window")
    assert released.handles and released.handles[0].startswith("bot:window#")
    assert [h["owner"] for h in estop.read_state().holds] == ["operator"]

    # (c) a co-holder's release leaves an unattributable hold standing: it is nobody's scope,
    #     and only the operator's OWN resume clears it — while another ACTOR's hold survives
    #     that resume, so the fleet stays parked until it is named (D7).
    body = _body(hermes_home)
    body["holds"].append({"owner": "operator", "mode": "estop", "reason": "hand-written"})
    (hermes_home / "ESTOP").write_text(json.dumps(body, indent=2), encoding="utf-8")
    assert estop.read_state().owners == ["operator", "unverified"]

    _be_bot(monkeypatch, "bot:other")
    estop.arm(owner="co-holder", reason="co")
    released = estop.release(owner="bot:other")
    assert released.released is True and len(released.handles) == 1
    assert estop.read_state().owners == ["operator", "unverified"], "both survive"

    _be_bot(monkeypatch, "bot:window")
    estop.arm(owner="ops-head", reason="another window")
    resumed = estop.release(actor="operator")
    assert resumed.unverified_cleared == 1
    assert estop.read_state().engaged is True, "another actor's hold is not the operator's"
    assert estop.read_state().owners == ["bot:window"]
    assert estop.release(all_holds=True, actor="operator").released is True
    assert estop.is_engaged() is False


def test_operator_pause_and_resume_round_trip(hermes_home, as_operator, capsys):
    """Case 2 — the operator's own round trip through the CLI: armed AS operator, verified,
    resumed by a bare `hermes resume`, and BOTH acts recorded in the ledger."""
    from hermes_cli.subcommands.pause import cmd_pause, cmd_resume

    assert cmd_pause(_namespace(reason="operator stop")) == 0
    state = estop.read_state()
    assert state.engaged is True
    assert _hold(state, "operator")["actor"] == "operator"
    assert _hold(state, "operator")["verified"] is True
    assert not _hold(state, "operator")["claimed_owner"]
    assert [row["event"] for row in estop.read_events()] == ["arm"]
    out = capsys.readouterr().out
    assert "handle:" in out and "hermes resume --handle" in out, "the human is told the handle"
    assert _hold(state, "operator")["handle"] in out

    assert cmd_resume(_namespace()) == 0
    assert estop.is_engaged() is False
    rows = estop.read_events()
    assert [row["event"] for row in rows] == ["arm", "release"]
    assert rows[-1]["actor"] == "operator" and rows[-1]["owner"] == "operator"


def test_the_incidents_hand_written_body_reads_unverified_total(hermes_home, capsys):
    """Case 3 — the INCIDENT's exact bytes: a valid pre-registry body claiming to be the
    operator. It must read as unverified/TOTAL (owner `unverified`, mode estop, allow
    ignored, DEFECT_UNVERIFIED), be nameable by a CONTENT-stable handle, and still hold the
    fleet. The old mtime-keyed `legacy-<...>` handle must be refused as stale, not reported
    as "not paused" — that false green is what let a closed incident read as open."""
    from hermes_cli.status import _estop_status_line
    from hermes_cli.subcommands.pause import cmd_resume

    incident = {
        "owner": "operator",
        "mode": "estop",
        "reason": "RE-ARMED by ops head",
        "engaged_at": "2026-09-27T19:34:50.000000+00:00",
    }
    (hermes_home / "ESTOP").write_text(json.dumps(incident), encoding="utf-8")
    state = estop.read_state()
    assert state.engaged is True and state.total is True
    hold = state.holds[0]
    assert hold["owner"] == estop.ACTOR_UNVERIFIED
    assert hold["claimed_owner"] == "operator"
    assert hold["verified"] is False
    assert hold["mode"] == estop.MODE_ESTOP
    assert hold["allow"] == {}
    assert state.allow_profiles == frozenset() and state.allow_user_ids == frozenset()
    assert state.defect == estop.DEFECT_UNVERIFIED
    assert hold["handle"].startswith("unverified#")
    assert hold["handle"] in _estop_status_line(), "status must name the handle to release"

    # Stable across a BYTE-IDENTICAL rewrite (the mtime-keyed handle was not).
    (hermes_home / "ESTOP").write_text(json.dumps(incident), encoding="utf-8")
    assert estop.read_state().holds[0]["handle"] == hold["handle"]

    # The handle the incident's false green was built on is gone — and says so (rc 2).
    assert cmd_resume(_namespace(handle="operator#legacy-123456:789")) == 2
    assert "names no live hold" in capsys.readouterr().out
    assert estop.is_engaged() is True, "a refused handle must not lift anything"

    assert estop.release(handle=hold["handle"], actor="operator").released is True
    assert estop.is_engaged() is False


def test_an_unattended_arm_cannot_wear_the_operators_name(hermes_home, as_bot):
    """Case 4 — the window armed twice (pause + the enrolment re-arm), each claiming
    `owner=operator`, from a non-operator actor. One hold, owned by the ACTOR, the claim
    recorded as `claimed_owner`, and NOTHING on disk owned by 'operator'. Because D4 renames
    before `replace_owner` matches, the second arm replaces the first instead of stacking."""
    first = estop.arm(owner="operator", reason="window A", actor=as_bot, replace_owner=True)
    second = estop.arm(owner="operator", reason="window B", actor=as_bot, replace_owner=True)
    assert first.owner == as_bot and second.owner == as_bot
    assert first.claimed_owner == "operator" and second.claimed_owner == "operator"
    assert first.renamed is True and second.renamed is True
    assert first.handle != second.handle

    entries = _body(hermes_home)["holds"]
    assert [entry["owner"] for entry in entries] == [as_bot]
    assert all(entry["owner"] != "operator" for entry in entries)
    assert [row["event"] for row in estop.read_events()] == ["arm", "arm_renamed"] * 2

    # And the top-level mirror (the shipped sentinel-body contract) still parses as a hold.
    body = _body(hermes_home)
    assert body["mode"] == estop.MODE_ESTOP and body["reason"] == "window B"
    assert estop.read_state().engaged is True


def test_a_signed_lockdown_survives_the_round_trip(hermes_home, as_bot, monkeypatch):
    """Case 5 — a signed LOCKDOWN hold, read back by a later process, still enforces the scope
    it wrote: mode, lanes, the turn allowlist, and `work_admitted` for a lane inside and
    outside it. Provenance does not cost the lockdown its teeth."""
    _be_bot(monkeypatch, "bot:window")
    outcome = estop.arm(
        owner="ops-head", mode="lockdown", reason="train window", ttl="45m",
        allow={"profiles": ["platform-stl"], "user_ids": [OPERATOR]},
    )
    assert outcome.mode == estop.MODE_LOCKDOWN and outcome.verified is True
    raw = _entry(hermes_home, "bot:window")
    assert raw["mode"] == estop.MODE_LOCKDOWN and raw["sig"].startswith("hmac-sha256:")
    assert raw["actor"] == "bot:window"

    state = estop.read_state()
    assert state.total is False and state.engaged is True
    assert state.allow_profiles == frozenset({"platform-stl"})
    assert state.allow_user_ids == frozenset({OPERATOR})
    assert estop.work_admitted("platform-stl", state=state) is True
    assert estop.work_admitted("platform-coder", state=state) is False
    assert estop.is_allowed(OPERATOR) is True
    assert estop.is_allowed("someone-else", "platform-coder") is False

    # A co-holder's TOTAL hold still wins over the lockdown (scope is a union, min is TOTAL).
    estop.arm(owner="operator", reason="total stop", actor="operator")
    assert estop.read_state().total is True
    assert estop.work_admitted("platform-stl") is False


def test_a_bot_cannot_lift_the_operators_hold(hermes_home, monkeypatch, capsys):
    """Case 6 — D7: while the operator holds the fleet, an unattended actor's `hermes resume`
    lifts NOTHING (rc 3, the live holder named), and naming the operator's hold explicitly is
    REFUSED (rc 6) — `--owner operator` and `--all` alike. The hold stays live and no release
    is recorded. The actor can still lift its own hold by handle."""
    from hermes_cli.subcommands.pause import cmd_resume

    _be_operator(monkeypatch)
    operator_handle = estop.arm(owner="operator", reason="operator stop").handle
    _be_bot(monkeypatch, "bot:window")

    # A bare resume binds to the CALLER (fault 3's fix): it cannot reach the operator's hold.
    assert cmd_resume(_namespace()) == 3
    out = capsys.readouterr().out
    assert "no live hold owned by 'bot:window'" in out
    assert "operator" in out and "this is NOT a resume" in out
    assert estop.is_engaged() is True
    assert [row["event"] for row in estop.read_events()] == ["arm"], "a refusal is not a release"

    # Naming the operator's hold — or every hold — is refused outright (D7).
    assert cmd_resume(_namespace(owner="operator")) == 6
    assert "refusing to lift the operator's hold" in capsys.readouterr().out
    assert cmd_resume(_namespace(all=True)) == 6
    assert "refusing to lift EVERY hold" in capsys.readouterr().out
    assert estop.is_engaged() is True
    assert [h["handle"] for h in estop.read_state().holds] == [operator_handle]
    assert [row["event"] for row in estop.read_events()] == ["arm"]

    # Its OWN hold is still its own to lift, by handle.
    own = estop.arm(owner="ops-head", reason="window").handle
    assert cmd_resume(_namespace(handle=own)) == 0
    assert estop.is_engaged() is True, "the operator's hold is still standing"


def test_a_repeat_pause_replaces_only_the_callers_entry(hermes_home, monkeypatch):
    """Case 7 — `hermes pause` twice from the same venue, with a co-holder present: the
    caller's entry is REPLACED, the co-holder's entry is untouched, and the top-level shape
    still says what the older readers expect."""
    from hermes_cli.subcommands.pause import cmd_pause

    _be_bot(monkeypatch, "bot:window")
    co = estop.arm(owner="co-holder", actor="bot:other", reason="co")
    before = _body(hermes_home)
    co_before = _entry(hermes_home, "bot:other")

    assert cmd_pause(_namespace(reason="window A")) == 0
    assert cmd_pause(_namespace(reason="window B")) == 0
    entries = _body(hermes_home)["holds"]
    assert len(entries) == 2, "each actor holds once"
    assert len([e for e in entries if e["owner"] == "bot:window"]) == 1
    assert _entry(hermes_home, "bot:other") == co_before, "the co-holder's entry is untouched"
    assert co.handle == co_before["handle"]
    assert _body(hermes_home)["schema"] == estop.SCHEMA_VERSION
    assert _body(hermes_home)["mode"] == estop.MODE_ESTOP
    assert isinstance(before["holds"], list) and isinstance(before["engaged_at"], str)


def test_tampering_an_entry_flips_that_entry_to_unverified(hermes_home, as_bot, monkeypatch):
    """Case 8 — D5: flipping an entry's owner, or bumping its expires_at, invalidates THAT
    entry only. The untouched sibling still verifies, and the total scope is unaffected (the
    demoted entry holds as a TOTAL halt, so tampering can never WIDEN a lockdown either)."""
    _be_bot(monkeypatch, "bot:window")
    estop.arm(owner="ops-head", mode="lockdown", reason="window",
              allow={"profiles": ["platform-stl"]})
    estop.arm(owner="operator", reason="operator stop", actor="operator")

    body = _body(hermes_home)
    tampered = next(e for e in body["holds"] if e["owner"] == "operator")
    tampered["expires_at"] = "2099-01-01T00:00:00+00:00"
    (hermes_home / "ESTOP").write_text(json.dumps(body, indent=2), encoding="utf-8")

    state = estop.read_state()
    assert state.engaged is True and state.total is True
    window = _hold(state, "bot:window")
    assert window["verified"] is True, "the sibling entry is untouched and still verifies"
    assert window["mode"] == estop.MODE_LOCKDOWN, "its scope is intact"
    demoted = [h for h in state.holds if h["verified"] is False]
    assert len(demoted) == 1
    assert demoted[0]["owner"] == estop.ACTOR_UNVERIFIED
    assert demoted[0]["claimed_owner"] == "operator"
    assert demoted[0]["mode"] == estop.MODE_ESTOP, "an unverifiable entry is a TOTAL halt"
    assert state.defect == estop.DEFECT_UNVERIFIED

    # Flipping the owner (instead of the deadline) is the same story.
    body = _body(hermes_home)
    body["holds"] = [e for e in body["holds"] if e["owner"] == "bot:window"]
    (hermes_home / "ESTOP").write_text(json.dumps(body, indent=2), encoding="utf-8")
    assert estop.read_state().total is False
    demoted_again = estop.read_state()
    assert [h["owner"] for h in demoted_again.holds] == ["bot:window"]
    assert demoted_again.holds[0]["verified"] is True


def test_a_rearm_after_the_operators_resume_needs_an_order(hermes_home, monkeypatch, capsys):
    """Case 9 — D8: the operator resumes, then a third party arms. With no order the arm is
    REFUSED (rc=5, nothing written, a `refused` row recorded); with one it lands, is recorded
    as `arm_after_resume`, and is alerted. The operator's OWN arm needs no order — it IS the
    order. (`_file_ops_alert` is stubbed: a unit test must never file a card on the live ops
    board. Its failure path is asserted separately below.)"""
    from hermes_cli.subcommands import pause as pause_mod

    _be_operator(monkeypatch)
    assert pause_mod.cmd_pause(_namespace(reason="operator stop")) == 0
    assert pause_mod.cmd_resume(_namespace()) == 0
    _be_bot(monkeypatch, "bot:window")

    capsys.readouterr()
    assert pause_mod.cmd_pause(_namespace(reason="re-arm")) == 5
    out = capsys.readouterr().out
    assert "carries no order" in out
    assert "hermes pause --reason" in out and "have the operator" in out
    assert estop.is_engaged() is False, "a refused re-arm writes NOTHING"
    assert [row["event"] for row in estop.read_events()][-1] == "refused"

    alerts = []

    def _spy(**kwargs):
        alerts.append(kwargs)
        return {"filed": True, "id": "t_probe", "detail": ""}

    monkeypatch.setattr(pause_mod, "_file_ops_alert", _spy)
    capsys.readouterr()
    assert pause_mod.cmd_pause(_namespace(reason="re-arm", order="ops head, defcon t_probe")) == 0
    out = capsys.readouterr().out
    assert "RE-ARM over the operator's resume" in out and "t_probe" in out
    assert len(alerts) == 1 and alerts[0]["actor"] == "bot:window"
    assert estop.read_state().engaged is True
    rows = estop.read_events()
    kinds = [row["event"] for row in rows]
    assert "arm_after_resume" in kinds, kinds
    rearm = next(row for row in rows if row["event"] == "arm_after_resume")
    assert rearm["order"] == "ops head, defcon t_probe"
    assert rearm["after_resume"], "the resume it overrode is on the row"
    assert rearm["actor"] == "bot:window"

    # (3) the gate stays shut only until the next arm lands; a LATER re-arm (after the operator
    #     resumes again) is what fires the alert, and an alert that cannot be filed is RECORDED
    #     without ever changing the exit status.
    _be_operator(monkeypatch)
    assert pause_mod.cmd_resume(_namespace(all=True)) == 0
    monkeypatch.setattr(pause_mod, "_file_ops_alert",
                        lambda **kw: {"filed": False, "id": "", "detail": "board unreachable"})
    _be_bot(monkeypatch, "bot:window")
    capsys.readouterr()
    assert pause_mod.cmd_pause(_namespace(reason="re-arm 2", order="ops head")) == 0
    out = capsys.readouterr().out
    assert "could NOT be filed" in out and "board unreachable" in out
    alert_rows = [row for row in estop.read_events() if row["event"] == "alert"]
    assert alert_rows and alert_rows[-1]["filed"] is False
    assert "board unreachable" in alert_rows[-1]["detail"]

    # (4) the operator's own arm after their resume is not gated at all (their act IS the
    #     order), and their resume is the only thing that re-opens the gate for a third party.
    _be_operator(monkeypatch)
    assert pause_mod.cmd_resume(_namespace(all=True)) == 0
    monkeypatch.setattr(pause_mod, "_file_ops_alert",
                        lambda **kw: {"filed": True, "id": "t_probe2", "detail": ""})
    assert pause_mod.cmd_pause(_namespace(reason="operator re-arm")) == 0
    assert estop.read_events()[-1]["event"] == "arm"
    assert not estop.read_events()[-1].get("after_resume")

    assert pause_mod.cmd_resume(_namespace()) == 0
    _be_bot(monkeypatch, "bot:window")
    capsys.readouterr()
    assert pause_mod.cmd_pause(_namespace(reason="after the operator", order="ops head")) == 0
    assert "t_probe2" in capsys.readouterr().out


def test_the_ledger_records_one_line_per_event_and_trims(hermes_home, as_operator):
    """Case 10 — D6: the ledger is append-only, ONE line per event (never a partial write),
    and trims itself to the last 500 rows once it passes 512 KB, so a busy fleet cannot grow
    it without bound."""
    estop.arm(owner="operator", reason="a")
    estop.arm(owner="ops-head", actor="bot:probe", reason="b")
    estop.release(owner="bot:probe", actor="bot:probe")
    ledger = estop.ledger_path()
    lines = [line for line in ledger.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 4, "arm, arm(renamed), release"
    for line in lines:
        assert json.loads(line)["event"], "every line is a complete, parseable row"
    assert [row["event"] for row in estop.read_events()] == [
        "arm", "arm", "arm_renamed", "release"]

    # Trim only past the byte bound — a short history is never truncated.
    for index in range(estop.LEDGER_MAX_EVENTS + 50):
        estop.record_event("arm", sentinel=ledger, path=str(ledger), handle=f"x#{index}",
                           owner="probe", actor="bot:probe", detail="p" * 2000)
    assert ledger.stat().st_size > estop.LEDGER_MAX_BYTES
    rows = estop.read_events()
    assert len(rows) <= estop.LEDGER_MAX_EVENTS
    assert len([line for line in ledger.read_text(encoding="utf-8").splitlines() if line]) <= 500


def test_the_hold_path_never_consults_command_access_or_config(hermes_home, as_bot, monkeypatch):
    """Case 10 (b) — the source-diff assertion behind the 140 s hang: arming, reading and
    lifting a hold touch the sentinel, the ledger and the key and nothing else. Config and
    command-access loading are NOT on this path, so a slow or broken config cannot delay the
    emergency stop."""
    import hermes_cli.config as config

    def _boom(*args, **kwargs):
        raise AssertionError("the ESTOP path must not load config or command access")

    monkeypatch.setattr(config, "load_config", _boom)
    monkeypatch.setattr(config, "load_config_readonly", _boom)

    outcome = estop.arm(owner="ops-head", reason="window", ttl="45m")
    assert estop.is_engaged() is True and outcome.verified is True
    assert estop.read_state().engaged is True
    assert estop.release().released is True, "a bare release lifts the caller's own hold"
    assert estop.is_engaged() is False


def test_a_bare_touch_stays_an_alarm_and_a_corrupt_body_still_holds(hermes_home, as_operator):
    """Case 11 — the panic button, kept GREEN, and an honest divergence on the record.

    D5 says a bare `touch ~/.hermes/ESTOP` "must still HOLD ... exactly as today". 'Today' is
    not what it was: by the time D5 was written the live line had already made a ZERO-BYTE
    sentinel an unattributed ALARM — reported, left on disk, and NOT a hold — because only this
    module's atomic registry write creates that file, so a zero-byte one came from outside it.
    That guard is pinned by its own test on the live line and by the card's own case-11 wording
    ("keeps the bare-touch panic button green"), so it is preserved here, and what D5 actually
    protects — an unverifiable BODY must be demoted, never dropped — is asserted in full: a
    corrupt non-empty body still holds the fleet, as a TOTAL halt."""
    (hermes_home / "ESTOP").write_text("", encoding="utf-8")
    assert estop.is_engaged() is False, "a zero-byte sentinel is unattributed — never a hold"
    assert estop.read_state().defect == estop.DEFECT_UNATTRIBUTED_EMPTY
    assert (hermes_home / "ESTOP").exists(), "the alarm file is left for the operator to see"

    (hermes_home / "ESTOP").write_text("{not json", encoding="utf-8")
    state = estop.read_state()
    assert state.engaged is True and state.total is True
    assert state.holds[0]["owner"] == estop.ACTOR_UNVERIFIED
    assert state.holds[0]["verified"] is False
    assert state.defect == estop.DEFECT_UNREADABLE

    # A junk (but parseable) stamp still holds too, and the operator's own resume clears it.
    (hermes_home / "ESTOP").write_text(json.dumps({"expires_at": "not-a-date"}), encoding="utf-8")
    assert estop.is_engaged() is True
    assert estop.release(actor="operator").unverified_cleared == 1
    assert estop.is_engaged() is False


def test_a_worker_or_delegated_child_cannot_arm_or_lift(hermes_home, monkeypatch):
    """Case 12 — the venue fence and the corrupt-body clear. A dispatched kanban worker and a
    delegated child may neither arm nor lift (the lane being held must not release its own
    hold); the INTERNAL deadman expiry is not fenced, so a stranded hold still lifts itself."""
    _be_operator(monkeypatch)
    estop.arm(owner="operator", reason="operator stop")

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_probe")
    with pytest.raises(estop.EstopRefusal) as arm_refusal:
        estop.acquire(owner="ops-head")
    assert "kanban task t_probe" in str(arm_refusal.value)
    with pytest.raises(estop.EstopRefusal):
        estop.release(owner="operator")
    monkeypatch.delenv("HERMES_KANBAN_TASK")

    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    with pytest.raises(estop.EstopRefusal):
        estop.release(all_holds=True)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT")
    assert estop.is_engaged() is True, "refused writes leave the hold exactly as it was"

    # The deadman is not a caller: an expired hold lifts itself even from a fenced venue.
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    body = _body(hermes_home)
    body["holds"][0]["expires_at"] = _stamp(-1)
    (hermes_home / "ESTOP").write_text(json.dumps(body, indent=2), encoding="utf-8")
    assert estop.is_engaged() is False
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT")


def test_the_key_is_private_and_never_leaves_the_home(hermes_home, as_operator, capsys):
    """Provenance plumbing (D2): the key is 32 random bytes, mode 0600, beside the sentinel;
    it never appears in CLI output, and a hold signed by ANOTHER home's key reads unverified
    (and therefore still holds, as a TOTAL halt — an unverifiable key must not open the gate)."""
    estop.arm(owner="operator", reason="operator stop")
    key = estop.key_path()
    assert key.exists() and len(key.read_bytes()) == 32
    assert key.stat().st_mode & 0o777 == 0o600
    assert key.read_bytes() not in (b"", key.name.encode())
    out = capsys.readouterr().out
    assert key.read_bytes().hex() not in out

    # D2's rule is that the hold's OWN path decides the key, never "our" key: the same entry
    # verified beside the sentinel it was written to, and unverified (still a TOTAL halt)
    # beside a home whose key differs — which is what an unattributable hold looks like.
    entry = _body(hermes_home)["holds"][0]
    foreign = hermes_home / "foreign"
    foreign.mkdir()
    (foreign / ".estop-key").write_bytes(b"z" * 32)
    assert estop._load_key_verified(entry, hermes_home / "ESTOP") is True
    assert estop._load_key_verified(entry, foreign / "ESTOP") is False

