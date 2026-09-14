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

from hermes_cli.kanban_db_dispatch import (
    DispatcherHealth,
    DispatchResult,
    PendingWork,
    tick_deferral_reason,
    tick_did_work,
)


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
