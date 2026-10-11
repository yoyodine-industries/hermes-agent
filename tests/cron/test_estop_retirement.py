"""The ESTOP RETIREMENT (card `t_e04b4c81`, operator ruling 2026-10-10 via `t_9d108a04`).

A hold is LEGAL iff it names a scope — lanes and/or a bounded lease. An unscoped, unbounded
hold is LEGACY STATE AND AN ALARM, never a control. These tests hold the boundary at its four
load-bearing points:

1. `mode: estop` is not a supported operation — bare `hermes pause` refuses (rc≠0, nothing
   written), and the HOLD refuses too, so no surface can re-create one;
2. a scoped (`--lockdown --allow-profile … --ttl …`) hold arms, admits its lanes, holds the
   others per item, and its lease expires ITS OWN hold;
3. recovery never needs the operator and never needs `--all`: R1 (a bot's handle is nameable,
   and a bot never wears `operator`), R2 (a bot-armed hold claiming `operator` is liftable by
   an admitted ops venue, rc=0) and R3 (every release records its `cause` and its `via`);
4. the surface cannot re-create the outage: `/pause` refuses the unscoped form, and the status
   line / paused notice name the SCOPE, never a fleet-wide "Hermes is paused".

Every check runs in a private HERMES_HOME against the bytes under test; no live sentinel is
touched.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime

import pytest

from agent import estop


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Point HERMES_HOME at a temp dir and reset the module's per-engagement log state."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_ESTOP_ACTOR", raising=False)
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    estop._logged_components.clear()
    estop._lockdown_logged.clear()
    estop._expired_logged.clear()
    estop._unattributed_logged.clear()
    return tmp_path


def _pause_args(**overrides) -> argparse.Namespace:
    args = dict(reason=None, lockdown=False, allow_profile=None, allow_user=None, ttl=None,
                order=None, history=False, history_limit=20, json=False)
    args.update(overrides)
    return argparse.Namespace(**args)


def _resume_args(**overrides) -> argparse.Namespace:
    args = dict(handle=None, owner=None, all=False, json=False)
    args.update(overrides)
    return argparse.Namespace(**args)


def _scoped_arm(handle_owner="wind-down", lanes=("platform-coder",), ttl="45m") -> str:
    """Arm a real scoped hold through the engine, as a prod-owned runner does."""
    return estop.arm(
        owner=handle_owner, mode=estop.MODE_LOCKDOWN, ttl=ttl, actor="bot:runner",
        allow={"profiles": list(lanes)},
    ).handle


def _legacy_total_body(reason="thundering herd") -> str:
    """Write a PRE-RULING sentinel body (the shape older bytes left) and return its handle.

    No verb may arm this any more, so the legacy algebra the gate still has to READ is set up
    the way a pre-ruling install left it: a whole-body sentinel with no registry and no
    provenance. It reads as ONE hold — owner `unverified`, mode `estop`, TOTAL — i.e. the alarm
    the boundary statement describes.
    """
    body = {"reason": reason, "engaged_at": datetime.now(UTC).isoformat()}
    estop.sentinel_path().write_text(json.dumps(body) + "\n", encoding="utf-8")
    held = [hold for hold in estop.holds() if (hold.get("reason") or "") == reason]
    return held[0]["handle"] if held else ""


def _bot_armed_operator_hold(**fields) -> dict:
    """A PRE-D4 registry entry, SIGNED with this home's key so it reads as verified.

    R2 targets exactly this record: a BOT-armed hold that still carries `owner=operator`,
    because it was written before D4 renamed a claim it may not wear. Written by hand here
    because no verb can produce it any more.
    """
    entry = {
        "handle": f"operator#{int(datetime.now(UTC).timestamp() * 1_000_000)}",
        "owner": "operator",
        "actor": "bot:probe",
        "mode": estop.MODE_LOCKDOWN,
        "engaged_at": datetime.now(UTC).isoformat(),
        "expires_at": None,
        "allow": {"profiles": ["platform-coder"]},
    }
    entry.update(fields)
    entry["sig"] = estop._sign_fields(estop._canonical_fields(entry), estop.sentinel_path())
    estop.sentinel_path().write_text(
        json.dumps({"schema": estop.SCHEMA_VERSION, "holds": [entry]}, indent=2) + "\n",
        encoding="utf-8",
    )
    return entry


def _release_rows() -> list:
    """Every row that records a LIFT of a hold.

    The deadman lift carries the ENGINE half's shape on the converged line — an ``expire`` row
    (ruling t_b70753e9 round 2, shape 1: D5 lists ``expire`` beside ``release{cause}``) — so a
    reader asking "what lifted this scope, and why" accepts both events. The assertions below
    are about the CAUSE a lift leaves behind, never about the event's spelling.
    """
    return [row for row in estop.read_events() if row.get("event") in ("release", "expire")]


# ── 1. `mode: estop` is not a supported operation ───────────────────────────


def test_the_hold_refuses_a_fleet_wide_arm_and_records_the_refusal(hermes_home):
    with pytest.raises(estop.EstopTotalHaltRetired) as excinfo:
        estop.arm(mode=estop.MODE_ESTOP, actor="bot:probe")

    message = str(excinfo.value)
    assert "by scope" in message
    assert "set-dispatch" in message and "cron pause" in message and "--lockdown" in message
    assert not estop.sentinel_path().exists(), "a refused arm must write no hold"
    rows = [row for row in estop.read_events() if row.get("event") == "refused"]
    assert rows and rows[-1]["reason"] == "unsupported-fleet-wide-halt"


def test_the_default_mode_arms_nothing_and_a_typo_is_not_a_total_halt(hermes_home):
    """`mode=None` (the old default) and an unrecognised mode are both refused."""
    for mode in (None, "estop", "lockdwn", "total"):
        with pytest.raises(estop.EstopTotalHaltRetired):
            estop.acquire(mode=mode, actor="bot:probe")
    with pytest.raises(estop.EstopTotalHaltRetired):
        estop.engage(actor="bot:probe")  # the gateway `/pause` entry point
    assert not estop.sentinel_path().exists()


def test_cli_bare_pause_refuses_and_names_the_three_scoped_controls(hermes_home, capsys):
    from hermes_cli.subcommands.pause import cmd_pause

    rc = cmd_pause(_pause_args(reason="ops incident"))

    out = capsys.readouterr().out
    assert rc != 0, "a bare `hermes pause` must not succeed"
    assert rc == 2
    assert not estop.sentinel_path().exists(), "no total-halt hold may be written"
    assert "Refusing" in out
    assert "kanban boards set-dispatch" in out
    assert "cron pause" in out
    assert "--lockdown --allow-profile" in out


def test_cli_pause_with_only_an_allow_profile_still_refuses_and_points_at_lockdown(
    hermes_home, capsys,
):
    from hermes_cli.subcommands.pause import cmd_pause

    rc = cmd_pause(_pause_args(allow_profile=["platform-coder"], ttl="30m"))

    out = capsys.readouterr().out
    assert rc == 2
    assert not estop.sentinel_path().exists()
    assert "--lockdown" in out, "the refusal must name the one form that DOES scope a hold"


# ── 2. the scoped hold still works ──────────────────────────────────────────


def test_lockdown_arms_admits_its_lane_and_holds_every_other_per_item(
    hermes_home, capsys, monkeypatch,
):
    from hermes_cli.subcommands.pause import cmd_pause

    monkeypatch.setattr("hermes_cli.subcommands.pause._profile_is_real", lambda name: True)
    handle = _scoped_arm()  # the shape the CLI arms, through the engine

    assert estop.work_admitted("platform-coder") is True
    assert estop.work_admitted("defcon") is False, "a held lane is held PER ITEM"
    assert estop.work_admitted("") is False, "an unassigned card is never a lane"
    assert estop.check_paused("cron", estop.logger) is False, "the tick still RUNS when scoped"

    rc = cmd_pause(_pause_args(lockdown=True, allow_profile=["platform-coder"], ttl="5m",
                               reason="card rehearsal"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "scope: hold " in out and "platform-coder" in out
    assert estop.dominant_hold()["mode"] == estop.MODE_LOCKDOWN
    assert handle  # the engine armed one too; both are independent holds


def test_a_scoped_holds_lease_expires_its_own_hold_only(hermes_home):
    short = _scoped_arm("window-job", ttl="1s")
    long = _scoped_arm("yoyoflow:critical-section", ttl="45m")

    time.sleep(1.1)

    assert estop.is_engaged() is True, "a deadman lifts ITS hold, never the fleet's"
    live = [hold["handle"] for hold in estop.holds()]
    assert short not in live and long in live
    assert "deadman" in [row.get("cause") for row in _release_rows()], (
        "an expiry is a RELEASE of a scope and must be observable (D5/R3)"
    )


# ── 3. recovery: nameable handles, ops-liftable bot holds, causes ───────────


def test_an_unresolved_bot_handle_is_nameable_never_bot_unknown(hermes_home, monkeypatch):
    monkeypatch.setenv("HERMES_CRON_JOB", "probe")  # an unattended marker
    monkeypatch.delenv("HERMES_PROFILE", raising=False)

    handle = estop.actor_handle()

    assert handle.startswith("bot:unresolved:")
    assert handle != "bot:unknown"
    assert f":{os.getpid()}@" in handle, "the pid names the process; the boot id scopes it"
    assert estop._ACTOR_RE.match(handle), "a handle must stay printable and shell-safe"
    monkeypatch.setenv("HERMES_PROFILE", "ops-seat")
    assert estop.actor_handle() == "bot:ops-seat", "a resolvable profile still names the lane"


def test_a_bot_never_wears_the_operator_name(hermes_home, monkeypatch):
    monkeypatch.setenv("HERMES_CRON_JOB", "probe")
    monkeypatch.setenv("HERMES_ESTOP_ACTOR", "operator")

    handle = estop.actor_handle()
    assert handle != estop.ACTOR_OPERATOR, "an unattended caller never wears `operator`"

    result = estop.arm(owner="operator", mode=estop.MODE_LOCKDOWN, actor=handle,
                       allow={"profiles": ["platform-coder"]})
    assert result.owner == handle
    assert result.claimed_owner == "operator" and result.renamed is True
    hold = estop.holds()[0]
    assert hold["owner"] != estop.ACTOR_OPERATOR and hold["actor"] == handle


def test_the_history_printer_renders_every_row(hermes_home, capsys):
    """The history IS the ops seat's recovery surface — every row must RENDER.

    A format spec that raises is invisible until someone needs the record (this one was: it
    shipped in the base of this card and only showed up in the real run), so the row shapes an
    ops seat actually reads are asserted here: the arm row and the release row with its cause.
    """
    from hermes_cli.subcommands.pause import _print_history

    estop.arm(owner="wind-down", mode=estop.MODE_LOCKDOWN, ttl="45m", actor="bot:runner",
              allow={"profiles": ["platform-coder"]})
    assert _print_history(10, False) == 0
    out = capsys.readouterr().out
    assert "handle=" in out and "lanes=[platform-coder]" in out

    estop.release(owner="bot:runner", actor="bot:ops-seat")
    assert _print_history(10, False) == 0
    out = capsys.readouterr().out
    assert "release" in out and "cause=owner" in out and "via=ops-venue" in out


def test_a_bot_armed_hold_wearing_operator_is_liftable_by_an_ops_venue(
    hermes_home, capsys, monkeypatch,
):
    from hermes_cli.subcommands.pause import cmd_resume

    entry = _bot_armed_operator_hold()
    monkeypatch.setenv("HERMES_ESTOP_ACTOR", "bot:ops-seat")  # an admitted ops venue
    assert estop.is_engaged() is True

    rc = cmd_resume(_resume_args(owner="operator"))

    out = capsys.readouterr().out
    assert rc == 0, f"the ops seat must lift it without `--all` and without the operator: {out}"
    assert estop.is_engaged() is False
    assert not estop.sentinel_path().exists()
    rows = _release_rows()
    assert rows and rows[-1]["handle"] == entry["handle"]
    assert rows[-1]["cause"] == "owner" and rows[-1]["via"] == "ops-venue"


def test_a_hold_the_operator_really_armed_still_refuses_a_bot(hermes_home):
    """D7 is untouched: R2 is about a bot's CLAIM, not about the operator's own hold."""
    _bot_armed_operator_hold(actor="operator")

    result = estop.release(owner="operator", actor="bot:ops-seat")

    assert result.forbidden is True and result.released is False
    assert estop.is_engaged() is True
    # ...and it is still liftable BY HANDLE, which is the ops seat's path for it.
    assert estop.release(handle=estop.holds()[0]["handle"], actor="bot:ops-seat").released is True


def test_resume_all_is_retired_as_a_recovery_path(hermes_home, capsys, monkeypatch):
    from hermes_cli.subcommands.pause import cmd_resume

    _scoped_arm()
    monkeypatch.setenv("HERMES_ESTOP_ACTOR", "bot:ops-seat")

    rc = cmd_resume(_resume_args(all=True))

    out = capsys.readouterr().out
    assert rc != 0, "`--all` must no longer be an exit"
    assert "RETIRED" in out
    assert "resume --handle" in out and "set-dispatch" in out and "cron pause" in out
    assert estop.is_engaged() is True, "a refused `--all` lifts nothing"
    engine = estop.release(all_holds=True, actor="bot:ops-seat")
    assert engine.forbidden is True and engine.released is False


def test_resume_all_lifts_only_a_pre_ruling_total_hold_and_logs_it(
    hermes_home, capsys, monkeypatch,
):
    from hermes_cli.subcommands.pause import cmd_resume

    legacy = _legacy_total_body()
    scoped = _scoped_arm()
    assert estop.is_engaged() is True and estop.read_state().total is True
    monkeypatch.setenv("HERMES_ESTOP_ACTOR", "bot:ops-seat")
    # The handle of an unverified (legacy) entry is CONTENT-derived, and another arm rewrites
    # the file around it — so read it live, as the ops seat would from `hermes pause --history`.
    legacy_live = [hold["handle"] for hold in estop.holds() if estop.is_legacy_total(hold)]
    assert legacy_live

    rc = cmd_resume(_resume_args(all=True))

    out = capsys.readouterr().out
    assert rc == 0, out
    live = [hold["handle"] for hold in estop.holds()]
    assert legacy_live[0] not in live, "the pre-ruling alarm is lifted"
    assert scoped in live, "another holder's scoped hold is NOT `--all`'s to lift"
    assert "RETIRED" in out and "legacy" in out.lower()
    rows = _release_rows()
    assert rows[-1]["cause"] == "all" and rows[-1]["handle"] == legacy_live[0]
    assert legacy


def test_every_release_form_records_a_cause(hermes_home):
    """R3: `handle`, `owner` and `deadman` releases all leave a readable cause."""
    by_handle = estop.arm(owner="ops-head", mode=estop.MODE_LOCKDOWN, ttl="45m", actor="bot:a",
                          allow={"profiles": ["platform-coder"]}).handle
    assert estop.release(handle=by_handle, actor="bot:ops-seat").released is True

    # D4: the OWNER is the actor that armed it — a claim like "wind-down" is recorded, not worn.
    estop.arm(owner="wind-down", mode=estop.MODE_LOCKDOWN, ttl="45m", actor="bot:b",
              allow={"profiles": ["platform-coder"]})
    assert estop.release(owner="bot:b", actor="bot:ops-seat").released is True

    estop.arm(owner="window-job", mode=estop.MODE_LOCKDOWN, ttl="1s", actor="bot:c",
              allow={"profiles": ["platform-coder"]})
    time.sleep(1.1)
    estop.is_engaged()  # the gate call is what retires an expired hold

    causes = [row.get("cause") for row in _release_rows()]  # release{cause} rows + the `expire` row
    assert "handle" in causes and "owner" in causes and "deadman" in causes
    assert by_handle


# ── 4. the surface must not re-create the outage ────────────────────────────


class _FakePauseEvent:
    internal = False
    source = None

    def __init__(self, args: str = ""):
        self._args = args
        self.text = f"/pause {args}".strip()

    def get_command(self):
        return "pause"

    def get_command_args(self):
        return self._args


@pytest.mark.asyncio
async def test_gateway_pause_refuses_the_unscoped_form(hermes_home):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    reply = await runner._handle_pause_command(_FakePauseEvent("deploy window"))

    assert "set-dispatch" in reply and "cron pause" in reply
    assert "lockdown" in reply
    assert estop.is_engaged() is False
    assert not estop.sentinel_path().exists(), "a chat line may not arm a hold"


@pytest.mark.asyncio
async def test_gateway_pause_off_names_the_by_handle_release_for_a_foreign_hold(
    hermes_home, monkeypatch,
):
    from gateway.run import GatewayRunner

    entry = _bot_armed_operator_hold()
    monkeypatch.setenv("HERMES_ESTOP_ACTOR", "bot:ops-seat")

    runner = object.__new__(GatewayRunner)
    reply = await runner._handle_pause_command(_FakePauseEvent("off"))

    assert entry["handle"] in reply
    assert "resume --handle" in reply
    assert estop.is_engaged() is True, "a foreign hold is not lifted from the chat line"


def test_the_notice_and_the_status_line_name_the_scope(hermes_home, monkeypatch):
    from hermes_cli.status import _estop_status_line

    handle = _scoped_arm("wind-down", lanes=("platform-coder", "platform-stl"), ttl="45m")

    notice = estop.paused_reply()
    line = _estop_status_line()
    assert notice is not None and line is not None
    for text in (notice, line):
        assert handle in text, "the holder's handle must be nameable from the notice"
        assert "platform-coder" in text and "platform-stl" in text
        assert "Hermes is paused" not in text, "never a fleet-wide pause claim"
        assert "hermes resume --handle" in text
    assert "paused" in notice.lower() and "HOLD" in line


def test_a_legacy_total_hold_is_shown_as_an_alarm_not_as_a_control(hermes_home):
    from hermes_cli.status import _estop_status_line

    handle = _legacy_total_body("thundering herd")

    notice = estop.paused_reply()
    line = _estop_status_line()
    for text in (notice, line):
        assert handle in text
        assert "LEGACY" in text, "a total hold can only be pre-ruling state now"
        assert "hermes resume --handle" in text


# ── the C2 guard: a bounce does NOT clear a hold ────────────────────────────


def test_c2_a_hold_armed_by_another_process_survives_a_fresh_interpreter(hermes_home):
    """C2: no release-on-restart. The hold is FILESYSTEM state, written out of process, and a
    brand-new interpreter (what a gateway bounce is) reads it as engaged."""
    code = (
        "from agent.estop import MODE_LOCKDOWN, arm, is_engaged;"
        "h = arm(owner='wind-down', mode=MODE_LOCKDOWN, ttl='30m', actor='bot:runner',"
        " allow={'profiles': ['platform-coder']}).handle;"
        "print(h, is_engaged())"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(estop.__file__)))),
        env={**os.environ},
    )
    assert proc.returncode == 0, proc.stderr
    handle, engaged = proc.stdout.split()
    assert engaged == "True"

    # A reload re-executes every module-level statement: nothing at import clears a hold.
    importlib.reload(estop)
    assert estop.is_engaged() is True
    assert handle in [hold["handle"] for hold in estop.holds()]
    assert estop.paused_reply() is not None
