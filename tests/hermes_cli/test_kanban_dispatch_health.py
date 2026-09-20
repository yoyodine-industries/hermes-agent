"""Dispatcher health telemetry must warn on a BROKEN dispatcher, not a busy host.

Regression this pins down: the previous rule ("ready work exists and 0 workers
spawned for N consecutive ticks") is the steady state on a healthy host under
`kanban.max_spawn` / `kanban.max_in_progress`, because `_tick_spawn_budget`
declines an entire tick without recording any skip bucket — so a deep queue and a
wedged dispatcher produced the identical warning, forever, while the one state an
operator must act on (a claimed card whose worker could not be launched at all)
raised nothing.
"""

from __future__ import annotations

import pytest

from hermes_cli.kanban_db_dispatch import (
    DispatcherHealth,
    DispatchResult,
    PendingWork,
    tick_deferral_reason,
    tick_did_work,
)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB (the daemon entry point opens one)."""
    from pathlib import Path

    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _tick(**fields) -> list[tuple[str, DispatchResult]]:
    """One board's results for one tick."""
    return [("ops", DispatchResult(**fields))]


def _old_pending(task_id: str = "t_waiting", age_seconds: float = 3600.0) -> PendingWork:
    return PendingWork(board="ops", task_id=task_id, age_seconds=age_seconds)


def test_capacity_deferral_with_a_deep_queue_never_warns() -> None:
    """A full spawn budget is a decision, not a stall — however long it lasts."""
    health = DispatcherHealth(window=3)
    for tick in range(12):
        report = health.observe_tick(
            _tick(spawn_budget_blocked="max_spawn"),
            pending=_old_pending(),
            now=1_000.0 + tick,
        )
        assert report is None, f"tick {tick} warned on a capacity deferral: {report}"


def test_undeferrable_no_op_ticks_only_warn_once_work_is_old() -> None:
    """Nothing attempted, nothing deferred: warn, but only on genuinely old work."""
    health = DispatcherHealth(window=3)
    fresh = PendingWork(board="ops", task_id="t_new", age_seconds=60.0)
    for tick in range(10):
        assert health.observe_tick(_tick(), pending=fresh, now=1_000.0 + tick) is None

    old = _old_pending()
    assert health.observe_tick(_tick(), pending=old, now=2_000.0) is None
    assert health.observe_tick(_tick(), pending=old, now=2_001.0) is None
    report = health.observe_tick(_tick(), pending=old, now=2_002.0)
    assert report is not None and report.level == "warning"
    for token in ("ops", "t_waiting", "min"):
        assert token in report.message


def test_unlaunchable_worker_warns_at_once_then_respects_the_interval() -> None:
    """The actionable failure names board, profile, task and reason — then backs off."""
    health = DispatcherHealth()
    failed = _tick(spawn_failed=[("t_stuck", "yaan-coder", "no such profile")])
    pending = _old_pending("t_stuck")

    report = health.observe_tick(failed, pending=pending, now=500.0)
    assert report is not None and report.level == "warning"
    for token in ("ops", "t_stuck", "yaan-coder", "no such profile"):
        assert token in report.message

    assert health.observe_tick(failed, pending=pending, now=600.0) is None
    repeat = health.observe_tick(failed, pending=pending, now=900.0)
    assert repeat is not None and repeat.level == "warning"


def test_recovery_is_reported_once_and_stops() -> None:
    """Clearing a warning state emits exactly one info line, not one per tick."""
    health = DispatcherHealth()
    failed = _tick(spawn_failed=[("t_stuck", "yaan-coder", "boom")])
    pending = _old_pending("t_stuck")
    assert health.observe_tick(failed, pending=pending, now=500.0) is not None

    recovered = _tick(spawned=[("t_ok", "yaan-coder", "/w")])
    report = health.observe_tick(recovered, pending=PendingWork(), now=501.0)
    assert report is not None and report.level == "info"
    assert health.observe_tick(recovered, pending=PendingWork(), now=502.0) is None


def test_a_deferral_is_not_a_failure_and_work_is_not_a_deferral() -> None:
    """The classifier the rules lean on: deferrals and work are self-reported."""
    assert tick_deferral_reason(DispatchResult(spawn_budget_blocked="max_in_progress")) == (
        "spawn_budget:max_in_progress"
    )
    assert tick_deferral_reason(DispatchResult(memory_pressure="critical")) == (
        "memory_pressure:critical"
    )
    assert tick_deferral_reason(DispatchResult(skipped_unassigned=["t_a"])) == "unassigned"
    assert tick_deferral_reason(DispatchResult(spawn_failed=[("t_a", "p", "boom")])) is None

    assert tick_did_work(DispatchResult(spawned=[("t_a", "p", "/w")])) is True
    assert tick_did_work(DispatchResult(reclaimed=2)) is True
    assert tick_did_work(DispatchResult()) is False
    assert tick_did_work(None) is False


# ---------------------------------------------------------------------------
# The host cap is the ONE deferral that must eventually report
#
# Every other deferral bucket is decided by the deferred board's own config or
# state, so silence about it is honest. `kanban.max_in_progress` is a race with
# every OTHER board: a board can lose it for days, recording a deferral every
# tick and no skip bucket, while the queue behind it grows. These pin the
# bounded-age override that turns that indefinite starvation into a line.
# ---------------------------------------------------------------------------


def _starved(head: str = "t_head", **fields) -> list[tuple[str, DispatchResult]]:
    """One host-cap-deferred tick that names the card it was denied."""
    return _tick(
        spawn_budget_blocked="max_in_progress",
        deferred_host_capped=[head],
        **fields,
    )


def test_host_cap_starvation_warns_only_past_the_bounded_age() -> None:
    """A board that has lost the host budget for long enough gets named."""
    health = DispatcherHealth(host_cap_defer_seconds=600.0)
    starved = _starved()

    # Ten minutes of losing every race: still the loaded host's steady state.
    for tick in range(40):
        report = health.observe_tick(starved, pending=PendingWork(), now=1_000.0 + tick * 10)
        assert report is None, f"tick {tick} warned during a normal deferral: {report}"

    report = health.observe_tick(starved, pending=PendingWork(), now=2_000.0)
    assert report is not None and report.level == "warning"
    for token in ("ops", "t_head", "min"):
        assert token in report.message


def test_host_cap_starvation_clock_resets_when_the_board_gets_a_slot() -> None:
    """Starvation is CONSECUTIVE: a board given a slot is not still starving."""
    health = DispatcherHealth(host_cap_defer_seconds=600.0)
    assert health.observe_tick(_starved(), pending=PendingWork(), now=1_000.0) is None
    assert health.observe_tick(_starved(), pending=PendingWork(), now=1_500.0) is None

    # This board spawned: its deferral is over.
    spawned = _tick(spawned=[("t_head", "alice", "/w")])
    assert health.observe_tick(spawned, pending=PendingWork(), now=1_600.0) is None

    # Starved again — the earlier wait must not be inherited.
    assert health.observe_tick(_starved(), pending=PendingWork(), now=1_700.0) is None
    assert health.observe_tick(_starved(), pending=PendingWork(), now=2_100.0) is None
    report = health.observe_tick(_starved(), pending=PendingWork(), now=2_400.0)
    assert report is not None and "t_head" in report.message


def test_host_cap_deferral_with_no_named_cards_stays_silent() -> None:
    """No recorded victims means unproven starvation — never a warning.

    Covers the producers that record the cap without enumerating (a board with
    nothing spawnable, a write that failed): the rule needs evidence, so it
    cannot invent a starved board.
    """
    health = DispatcherHealth(host_cap_defer_seconds=10.0)
    for tick in range(50):
        report = health.observe_tick(
            _tick(spawn_budget_blocked="max_in_progress"),
            pending=_old_pending(),
            now=1_000.0 + tick * 100,
        )
        assert report is None


def test_host_cap_starvation_names_the_head_of_line_card() -> None:
    """The named card is the one the cap denied first — the head of line."""
    health = DispatcherHealth(host_cap_defer_seconds=600.0)
    starved = _starved(head="t_oldest")
    starved[0][1].deferred_host_capped.append("t_behind")
    assert health.observe_tick(starved, pending=PendingWork(), now=1_000.0) is None
    report = health.observe_tick(starved, pending=PendingWork(), now=2_000.0)
    assert report is not None
    assert "t_oldest" in report.message
    assert "t_behind" not in report.message


def test_host_cap_starvation_clears_like_any_other_state() -> None:
    """Returning to health emits exactly one info line, then silence."""
    health = DispatcherHealth(host_cap_defer_seconds=600.0)
    assert health.observe_tick(_starved(), pending=PendingWork(), now=1_000.0) is None
    assert health.observe_tick(_starved(), pending=PendingWork(), now=2_000.0) is not None

    recovered = _tick(spawned=[("t_head", "alice", "/w")])
    report = health.observe_tick(recovered, pending=PendingWork(), now=2_100.0)
    assert report is not None and report.level == "info"
    assert health.observe_tick(recovered, pending=PendingWork(), now=2_200.0) is None


def test_pause_clears_the_starvation_clock() -> None:
    """A paused dispatcher is not a starved board: no clock survives a pause."""
    health = DispatcherHealth(host_cap_defer_seconds=600.0)
    assert health.observe_tick(_starved(), pending=PendingWork(), now=1_000.0) is None
    health.pause()
    assert health.observe_tick(_starved(), pending=PendingWork(), now=5_000.0) is None


def test_the_standalone_daemon_feeds_the_tracker_labelled_results(
    kanban_home, monkeypatch
) -> None:
    """``hermes kanban dispatch --force`` must reach these rules at all.

    Regression: the standalone loop ticks ONE board and handed DispatcherHealth
    the bare ``DispatchResult``, so the first rule raised on
    ``for slug, result in board_results`` — and ``run_daemon``'s
    ``contextlib.suppress`` ate it. Every rule here (launch failures, stuck
    ready, host-cap starvation) was dead on that entry point while its own
    comment advertised them.
    """
    from types import SimpleNamespace

    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import kanban_ops

    seen: dict = {}
    result = kbd.DispatchResult(spawn_budget_blocked="max_in_progress")

    class _Tracker:
        def __init__(self, **kwargs) -> None:
            seen["init"] = kwargs

        def observe_tick(self, board_results=None, **kwargs):
            seen["board_results"] = board_results
            return None

    # kanban_ops resolves both through the dispatch module, and run_daemon is
    # what actually calls on_tick — patch where that call site reads them, or
    # the real daemon loop runs and the tick never happens.
    monkeypatch.setattr(kbd, "DispatcherHealth", _Tracker)
    monkeypatch.setattr(
        kbd, "run_daemon",
        lambda **kwargs: kwargs["on_tick"](result),  # one tick, then stop
    )

    args = SimpleNamespace(
        force=True, interval=5.0, max=None, failure_limit=2, board="ops",
        pidfile=None, verbose=False,
    )
    assert kanban_ops._cmd_daemon(args) == 0

    # The tracker got the shape it documents: (board, result) pairs.
    assert seen["board_results"] == [("ops", result)]
    assert DispatcherHealth().observe_tick(seen["board_results"]) is None  # never raises
