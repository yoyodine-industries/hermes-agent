"""A run-owned hold is a LEASED, OWNER-BOUND hold, and an INDEPENDENT reaper resolves it.

migrations/t_b70753e9 — ruling D1/D2/D3/D5 of ``RULING-leaked-hold-class.md`` (t_e4da9955).
The clauses locked down here are the engine half of the property "an armed critical-section
hold can never outlive its owning run":

  * **D1** a run-scoped arm that cannot name its run is REFUSED (:class:`EstopUnboundRun`) and
    nothing is written to the sentinel;
  * **D2** the deadline is not optional — with no ``--ttl`` the SECTION BUDGET applies, so
    tonight's ``expires_at=None`` arm is unreachable;
  * **D2** ``renew()`` pushes the deadline forward and writes a ``renew`` row — renewal is the
    only liveness assertion, so expiry means owner-dead within one lease period;
  * **D3** the reaper RELEASES a hold whose ``owner_run`` is terminal in a run ledger, from
    OUTSIDE the owning run, and it rides the surfaces that GATE work — ``check_paused()``
    (the cron scheduler tick, the gateway's kanban watchers, the cron webhook) and the kanban
    dispatcher's own tick read. Measuring it through the pause CLI is what let the defect
    through the first time: the CLI is not on any production tick surface.
  * **D5** the deadman lift writes its row (the measured gap: ``['arm','arm_renamed']`` with no
    lift, which is why no log-keyed detector could see 2026-10-10), and every release names its
    ``cause``.

ISOLATION: a private ``HERMES_HOME`` per test, with ``Path.home`` pinned away from the real
home so the fleet-root candidate never resolves outside the temp tree (the same contract the
lockdown suite uses).  Nothing here touches live fleet state.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pytest

from agent import estop

#: The owning run's actor handle — the update's own shape, ``bot:<lane>:<run>``.
RUN = "bot:hermes-update:RUN-0001"
OTHER_RUN = "bot:hermes-update:RUN-0002"


@pytest.fixture(autouse=True)
def run_context(monkeypatch):
    """An UNATTENDED RUN venue: the venue fences are clear and the actor is the update's own
    handle. A dispatched worker is fenced from arm/lift by ``arm_admission`` by design, and a
    suite run from inside one inherits ``HERMES_KANBAN_TASK``, so it is cleared here."""
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv(DELEGATED_CHILD_ENV_MARKER, raising=False)
    monkeypatch.delenv("YOYOFLOW_RUN_ID", raising=False)
    monkeypatch.delenv("HERMES_UPDATE_RUN_ID", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "platform-coder")
    monkeypatch.setenv("HERMES_ESTOP_ACTOR", RUN)
    monkeypatch.setattr(estop, "_stdin_is_tty", lambda: False)


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Isolated ``HERMES_HOME``, with the fleet root kept out of the candidate path list."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nobody")
    return home


def _rows(handle: str) -> list:
    return [row for row in estop.read_events() if row.get("handle") == handle]


def _event_row(handle: str, event: str) -> dict:
    """The ONE row a transition writes for ``handle`` (exactly-one-row-per-transition)."""
    return next(row for row in _rows(handle) if row["event"] == event)


def _run_ledger(home: Path, *rows: dict) -> Path:
    """Record run rows where the runner already writes them: ``<home>/run_details/*.jsonl``."""
    path = home / estop.RUN_LEDGER_DIRNAME / "update-runs.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _hold(handle: str) -> dict:
    for hold in estop.read_state().holds:
        if hold["handle"] == handle:
            return hold
    raise AssertionError(f"hold {handle!r} is not live: {[h['handle'] for h in estop.read_state().holds]}")


def _arm(**kwargs) -> estop.ArmResult:
    """Arm the RUN-OWNED LEASE this suite measures — as a SCOPED hold.

    CONVERGED LINE (card t_871b1d96): the retirement half (t_e04b4c81) retires ``mode: estop``,
    so a hold is LEGAL iff it names a scope and a bare ``arm()`` is refused with
    :class:`estop.EstopTotalHaltRetired` before anything is written. The clauses below (D1
    binding, D2 lease + budget, D3 reaper, D5 rows) are properties of a run-owned hold, not of
    the retired total-halt mode, so every arm here goes through this helper and names a scope.
    The lane allowlist is deliberately one the suite's own lane is NOT on, so "the gate still
    holds this lane" stays a real measurement (``work_admitted``), never a vacuous one.
    """
    kwargs.setdefault("mode", estop.MODE_LOCKDOWN)
    kwargs.setdefault("allow", {"profiles": ["default"]})
    return estop.arm(**kwargs)


# ── D1: no unbound, anonymous hold ──────────────────────────────────────────


def test_a_run_scoped_arm_that_cannot_name_its_run_is_refused(hermes_home, monkeypatch):
    """A run-shaped venue handle with an empty run segment — what a caller produces when its
    run-id variable is unset — arms NOTHING and says why."""
    monkeypatch.setenv("HERMES_ESTOP_ACTOR", "bot:hermes-update:")

    with pytest.raises(estop.EstopUnboundRun) as excinfo:
        _arm(reason="wind-down hold, TTL 90m", ttl="90m")

    assert "unbound" in str(excinfo.value).lower()
    assert not estop.sentinel_path().exists(), "the refused arm still wrote a sentinel"
    refusals = [row for row in estop.read_events()
                if row.get("event") == "refused" and row.get("reason") == "unbound-run"]
    assert refusals, "the refusal is not recorded in the ledger (D5)"


def test_the_owning_run_is_written_on_the_hold_and_on_the_arm_row(hermes_home):
    """D1: a run-scoped arm carries the binding the reaper resolves — on the body AND the row."""
    res = _arm(reason="wind-down hold, TTL 90m", ttl="90m")

    assert res.owner_run == RUN
    assert _hold(res.handle)["owner_run"] == RUN
    arm_row = _event_row(res.handle, "arm")
    assert arm_row["owner_run"] == RUN


def test_the_owner_run_comes_from_the_environment_when_the_caller_names_none(hermes_home, monkeypatch):
    """A yoyoflow node names its run in the environment, not in the handle."""
    monkeypatch.setenv("HERMES_ESTOP_ACTOR", "bot:platform-coder")
    monkeypatch.setenv("YOYOFLOW_RUN_ID", "20261010-1200-9")

    res = _arm(reason="critical section", ttl="10m")

    assert res.owner_run == "20261010-1200-9"
    assert _hold(res.handle)["owner_run"] == "20261010-1200-9"


# ── D2: the lease ───────────────────────────────────────────────────────────


def test_the_section_budget_is_the_default_lease(hermes_home):
    """With no --ttl the engine applies the declared budget, never None: tonight's arm shape
    (``expires_at=None ttl_s=None``) is unreachable."""
    res = _arm(reason="wind-down hold, TTL 90m")

    assert res.ttl_s == estop.DEFAULT_SECTION_BUDGET_S
    assert res.lease_default is True
    assert _hold(res.handle)["expires_at"], "the default lease left the hold unbounded"
    arm_row = _event_row(res.handle, "arm")
    assert arm_row["ttl_s"] == estop.DEFAULT_SECTION_BUDGET_S
    assert arm_row["lease_default"] is True


def test_a_declared_budget_sizes_the_lease(hermes_home):
    res = _arm(reason="wind-down hold", budget="2m", mode=estop.MODE_LOCKDOWN,
               allow={"profiles": ["default"]})

    assert res.ttl_s == 120
    assert res.lease_default is True


def test_an_explicit_ttl_still_wins(hermes_home):
    res = _arm(reason="window", ttl="90m")

    assert res.ttl_s == 5400
    assert res.lease_default is False


def test_renew_pushes_the_deadline_forward_and_records_it(hermes_home):
    """Renewal is the liveness proof: it must move the deadline, stamp the hold and leave a
    row, or a live section has no way to say it is still alive."""
    res = _arm(reason="wind-down hold", ttl="60s")
    before = _hold(res.handle)["expires_at"]

    renewed = estop.renew(res.handle, ttl="10m")

    assert renewed.renewed is True
    assert renewed.stale is False
    hold = _hold(res.handle)
    assert hold["expires_at"] > before, "renew did not push the deadline forward"
    assert hold["renewed_at"], "renew did not stamp the hold"
    row = _event_row(res.handle, "renew")
    assert row["ttl_s"] == 600
    assert row["expires_at"] == hold["expires_at"]
    assert row["owner_run"] == RUN


def test_renew_never_shortens_a_lease_it_holds(hermes_home):
    res = _arm(reason="wind-down hold", ttl="30m")
    before = _hold(res.handle)["expires_at"]

    estop.renew(res.handle, ttl="1m")

    assert _hold(res.handle)["expires_at"] == before


def test_renew_of_a_stale_handle_is_reported_and_creates_nothing(hermes_home):
    stale = estop.renew("bot:hermes-update:NOPE#1", ttl="10m")

    assert stale.renewed is False
    assert stale.stale is True
    assert not estop.sentinel_path().exists()


# ── D3: the independent reaper ──────────────────────────────────────────────


def test_a_hold_whose_run_is_terminal_is_released_by_the_reaper(hermes_home):
    """2026-10-10, reproduced: the run armed the guard and never released it. The run ledger's
    terminal row is the evidence, and the release leg is NOT in that run."""
    res = _arm(reason="wind-down hold, TTL 90m")
    assert estop.is_engaged() is True

    _run_ledger(hermes_home, {"run": RUN, "section": "hermes-update wind_down", "status": "dead"})

    assert estop.is_engaged() is False, "the leaked hold still pauses the fleet"
    release = [r for r in _rows(res.handle) if r["event"] == "release"]
    assert release and release[0]["cause"] == "orphan"
    assert release[0]["reaper"] is True
    assert not estop.sentinel_path().exists()


def test_a_hold_whose_run_is_still_running_is_left_alone(hermes_home):
    """The reaper resolves; it never guesses. A live section keeps its hold."""
    res = _arm(reason="wind-down hold")

    _run_ledger(hermes_home, {"run": RUN, "status": "running"})

    assert estop.is_engaged() is True
    assert _hold(res.handle)["handle"] == res.handle
    assert not [r for r in _rows(res.handle) if r["event"] == "release"]


def test_a_run_the_ledgers_do_not_know_is_bounded_by_its_lease_alone(hermes_home):
    """Absent a row for the owner, the LEASE is the only bound (D3) — the lease, not a guess
    that a missing row means a dead run."""
    res = _arm(reason="wind-down hold", ttl="30m")

    _run_ledger(hermes_home, {"run": OTHER_RUN, "status": "dead"})

    assert estop.is_engaged() is True
    assert not [r for r in _rows(res.handle) if r["event"] == "release"]


def test_the_reaper_releases_through_a_co_holders_hold(hermes_home):
    """The reaper releases ONE hold and leaves a co-holder's entry byte-identical."""
    orphan = _arm(reason="update window", ttl="30m")
    keeper = _arm(reason="ops window", ttl="30m", actor="bot:ops-head:OPS-9",
                       mode=estop.MODE_LOCKDOWN, allow={"profiles": ["default"]})

    _run_ledger(hermes_home, {"run": RUN, "status": "completed"})

    assert estop.is_engaged() is True, "the co-holder's hold was released with the orphan's"
    holders = {hold["handle"] for hold in estop.read_state().holds}
    assert keeper.handle in holders and orphan.handle not in holders


# ── D5: every transition observable ─────────────────────────────────────────


def test_the_deadman_lift_writes_an_expire_row(hermes_home):
    """The measured gap: a hold that self-lifted left a ledger ending at ``arm``."""
    res = _arm(reason="observable clause", ttl="1s")
    time.sleep(1.5)

    assert estop.is_engaged() is False

    lifts = [r for r in _rows(res.handle) if r["event"] == "expire"]
    assert lifts, "the deadman lift is INVISIBLE in the ledger"
    assert lifts[0]["cause"] == "deadman"


def test_every_release_names_its_cause(hermes_home):
    """``cause ∈ {explicit, handle, owner, operator-resume}`` — the scope stays readable after
    the sentinel body (and with it the lane list) is gone.

    CONVERGED (t_871b1d96): the ``all`` cause is NOT reachable from this suite's arms any more.
    The retirement half retires ``release(all_holds=True)`` as a recovery path — it lifts only a
    PRE-RULING ``mode: estop`` hold, which the converged line can no longer arm — so the ``all``
    cause is measured where it is still reachable, on a hand-written legacy entry, by
    ``tests/cron/test_estop_retirement.py``.
    """
    by_handle = _arm(reason="wind-down hold", ttl="30m")
    estop.release(handle=by_handle.handle, actor=RUN)
    assert _event_row(by_handle.handle, "release")["cause"] == "handle"

    by_owner = _arm(reason="wind-down hold", ttl="30m")
    estop.release(owner=RUN, actor=RUN)
    assert _event_row(by_owner.handle, "release")["cause"] == "owner"

    bare = _arm(reason="wind-down hold", ttl="30m")
    estop.release(actor=RUN)
    assert _event_row(bare.handle, "release")["cause"] == "explicit"

    resumed = _arm(reason="operator stop", ttl="30m", actor="operator", owner="operator")
    estop.release(owner="operator", actor="operator")
    assert _event_row(resumed.handle, "release")["cause"] == "operator-resume"


# ── D3/D5 through the PRODUCTION GATE, not the CLI ──────────────────────────
#
# The review defect this section exists for: `reap_orphans` was reachable only from
# `is_engaged()`, and `is_engaged()` only from the pause CLI — while every surface that GATES
# work called `check_paused()` / the dispatcher's tick read and did not reap. The hold was
# therefore released only if somebody happened to run the CLI, and the fleet stayed held for
# the whole lease. Each clause below is measured through the seam that actually holds work
# back, with the gate's own tick as the only actor.


def _gate_logger():
    return logging.getLogger("estop.gate.test")


def test_the_cron_gate_reaps_an_orphaned_hold_on_its_own_tick(hermes_home):
    """Control, then defect: while the run is live the gate holds the lane; once the run's
    ledger row is terminal, the NEXT gate tick releases the hold by itself — no CLI, no human.

    CONVERGED (t_871b1d96): the only hold this line arms is SCOPED, and ``check_paused`` (the
    cron tick's own call) answers for a TOTAL halt — so the control is the per-lane gate the
    tick threads down (``work_admitted``), measured through the same tick.
    """
    res = _arm(reason="wind-down hold")
    assert estop.check_paused("gate-test", _gate_logger()) is False, (
        "a scoped hold is admitted to run the tick; it holds lanes, not the fleet")
    assert estop.work_admitted("platform-coder") is False, (
        "the control is vacuous: a live run-owned hold must hold its lane")

    _run_ledger(hermes_home, {"run": RUN, "status": "dead"})

    assert estop.check_paused("gate-test", _gate_logger()) is False
    assert estop.work_admitted("platform-coder") is True, (
        "the cron gate still held the lane after its owner's run ended (the defect)")
    release = [r for r in _rows(res.handle) if r["event"] == "release"]
    assert release and release[0]["cause"] == "orphan" and release[0]["reaper"] is True, (
        "the gate released the hold without writing the orphan row (D5)")
    assert not estop.sentinel_path().exists()


def test_the_kanban_dispatch_tick_reaps_and_readmits_in_one_read(hermes_home):
    """The dispatcher's tick seam reads the state ONCE and threads it down every lane decision;
    that read must reap too, and the state handed to the lanes must be the POST-reap state."""
    from hermes_cli import kanban_db_dispatch

    res = _arm(reason="wind-down hold")
    state = kanban_db_dispatch._estop_state_for_tick()
    assert kanban_db_dispatch._lane_admitted(state, "platform-coder", None) is False, (
        "the control is vacuous: a live hold must refuse the lane")

    _run_ledger(hermes_home, {"run": RUN, "status": "dead"})

    state = kanban_db_dispatch._estop_state_for_tick()
    assert kanban_db_dispatch._lane_admitted(state, "platform-coder", None) is True, (
        "the dispatch tick still refused the lane after its owner's run ended (the defect)")

    release = [r for r in _rows(res.handle) if r["event"] == "release"]
    assert release and release[0]["cause"] == "orphan"
    assert not estop.sentinel_path().exists()


def test_the_gate_writes_the_deadman_row_it_retires(hermes_home):
    """D5 shares D3's reachability gap: the lift row is written by whichever surface retires
    the hold, so a deadman expiry on a live tick must not leave a ledger ending at `arm`.

    The control reads the sentinel WITHOUT reaping (``_hold`` -> ``read_state``): calling any
    reaping surface before the tick would write the very row this test exists to attribute to
    the GATE.
    """
    res = _arm(reason="observable clause", ttl="1s")
    assert _hold(res.handle)["handle"] == res.handle, (
        "the control is vacuous: the hold must be armed before its lease expires")
    time.sleep(1.5)

    assert estop.check_paused("gate-test", _gate_logger()) is False

    lifts = [r for r in _rows(res.handle) if r["event"] == "expire"]
    assert lifts, "the gate retired the hold without recording it (the measured D5 gap)"
    assert lifts[0]["cause"] == "deadman"
