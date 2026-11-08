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
# The two LIVE refusals the classifier has to know about (card t_bda9ac11 D4)
#
# The tracker replaced the bad-ticks rule in BOTH loops, so every refusal the
# live line records has to survive the swap as an EXPLAINED tick. A refusal the
# classifier cannot name is counted as an unexplained no-op, and six of them
# with a card older than the stall age accuse the healthy gate itself.
# ---------------------------------------------------------------------------


def test_a_skew_yielded_tick_is_explained_and_never_reaches_the_stall_warning() -> None:
    """The content-skew gate's refusal (ruling t_8fed34c8) is a DECISION, not a stall."""
    assert tick_deferral_reason(
        DispatchResult(tick_yielded="tree-skew-yield: content mismatch")
    ) == "tick_yielded"

    health = DispatcherHealth(window=3)
    yielded = _tick(tick_yielded="tree-skew-yield: content mismatch")
    old = _old_pending()
    for tick in range(20):
        report = health.observe_tick(yielded, pending=old, now=1_000.0 + tick)
        assert report is None, f"tick {tick} accused the skew gate of stalling: {report}"


def test_a_board_disabled_tick_is_explained_and_never_reaches_the_stall_warning() -> None:
    """A board refused as not dispatch-enabled (t_17c9c847) is a decision too."""
    assert tick_deferral_reason(DispatchResult(skipped_board_disabled=True)) == "board_disabled"

    health = DispatcherHealth(window=3)
    disabled = _tick(skipped_board_disabled=True)
    old = _old_pending()
    for tick in range(20):
        report = health.observe_tick(disabled, pending=old, now=1_000.0 + tick)
        assert report is None, f"tick {tick} accused a disabled board of stalling: {report}"


def test_a_board_that_lost_the_host_budget_race_is_explained() -> None:
    """No host-budget share this tick is a ROTATION, and the tick recorded it as one."""
    if not hasattr(DispatchResult(), "deferred_host_capped"):
        # A base that predates the host-budget split never sets the field, so the
        # classifier's rule is inert on it (`getattr` falls through) and there is
        # nothing to pin. On a tree that carries the split, this runs.
        pytest.skip("this base predates the host-budget split; the rule is inert here")

    assert tick_deferral_reason(DispatchResult(deferred_host_capped=["t_a"])) == (
        "host_cap_deferred"
    )

    health = DispatcherHealth(window=3)
    rotated = _tick(deferred_host_capped=["t_waiting"])
    old = _old_pending()
    for tick in range(20):
        report = health.observe_tick(rotated, pending=old, now=1_000.0 + tick)
        assert report is None, f"tick {tick} called a rotation a stall: {report}"


def test_positive_control_a_real_stall_still_warns_after_the_default_window() -> None:
    """Six unexplained no-op ticks and work older than the stall age still WARN."""
    health = DispatcherHealth(window=6)
    old = _old_pending()
    reports = [health.observe_tick(_tick(), pending=old, now=1_000.0 + t) for t in range(6)]
    assert reports[:5] == [None] * 5
    stalled = reports[5]
    assert stalled is not None and stalled.level == "warning"
    for token in ("6 consecutive ticks", "t_waiting", "ops", "min"):
        assert token in stalled.message


# ---------------------------------------------------------------------------
# The surface seams
#
# The tracker is unit-tested above; what actually broke on the live line was the
# WIRING between a surface and it. `observe_tick` iterates its argument, so a
# bare `DispatchResult` raises `TypeError: ... object is not iterable` inside
# `collect_spawn_failures`, and `run_daemon` swallows the `on_tick` exception —
# leaving the CLI telemetry silently dead. These tests exercise the seams, so
# that class of bug fails a test instead of going quiet.
# ---------------------------------------------------------------------------


def test_the_daemon_surface_seam_warns_for_a_spawn_failed_tick_and_never_raises() -> None:
    """The CLI tick->report step passes board-tagged results, not a bare result."""
    from hermes_cli.kanban_ops import _daemon_health_step

    failed = DispatchResult(spawn_failed=[("t_stuck", "yaan-coder", "no such profile")])
    report = _daemon_health_step(
        DispatcherHealth(), "ops", failed, _old_pending("t_stuck"), now=500.0
    )
    assert report is not None and report.level == "warning"
    for token in ("ops", "t_stuck", "yaan-coder", "no such profile"):
        assert token in report.message

    # A clean tick through the same seam stays silent — and does not raise, which
    # is exactly what a bare DispatchResult would do.
    assert _daemon_health_step(DispatcherHealth(), "ops", DispatchResult(), None, now=501.0) is None


def test_the_gateway_probe_names_the_longest_waiter_and_never_invents_one(monkeypatch) -> None:
    """`oldest_pending` reports WHICH card waited; an unreadable board adds nothing."""
    from gateway import kanban_watchers_dispatcher as kwd

    class _Kbd:
        DEFAULT_BOARD = "default"

        @staticmethod
        def review_dispatch_enabled() -> bool:
            return False

        @staticmethod
        def oldest_spawnable_pending(conn, include_review: bool = True):
            return conn.reply

        @staticmethod
        def PendingWork(**kwargs):
            return PendingWork(**kwargs)

    class _Kbc:
        @staticmethod
        def connect(board=None):
            if board == "unreadable":
                raise RuntimeError("board DB unavailable")

            class _Conn:
                reply = ("t_waiting", 3600.0) if board == "ops" else (None, 0.0)

                def close(self) -> None:
                    pass

            return _Conn()

    class _Kb:
        DEFAULT_BOARD = "default"

        @staticmethod
        def list_dispatch_boards():
            return [{"slug": "ops"}, {"slug": "unreadable"}]

    monkeypatch.setattr(kwd, "_kbd", lambda: _Kbd)
    monkeypatch.setattr(kwd, "_kbc", lambda: _Kbc)

    settings = kwd._DispatcherSettings(
        interval=1.0,
        max_spawn=None,
        max_in_progress=None,
        failure_limit=2,
        stale_timeout_seconds=0,
        reconcile_orphans=True,
        default_assignee=None,
        max_in_progress_per_profile=None,
    )
    pending = kwd._KanbanDispatcher(_Kb(), settings).oldest_pending()

    assert pending.pending is True
    assert (pending.board, pending.task_id, pending.age_seconds) == ("ops", "t_waiting", 3600.0)


def test_the_gateway_loop_retires_bad_ticks_for_the_shared_tracker(monkeypatch, caplog) -> None:
    """Drive the REAL gateway loop: the union port, not just the tracker.

    What survives the swap is checked here rather than claimed: the shared
    tracker decides (a claimed-but-unlaunchable card warns, a board-disabled tick
    does not), ``_log_tick_yields`` still runs for the skew gate's own line, the
    PART B stuck-ready probe still gets its cadence, ``ready_nonempty`` is no
    longer read from the health path, and a paused dispatcher stays silent.
    """
    import asyncio
    import logging

    from gateway import kanban_watchers as kw

    script: list = []
    allowed = {"value": True}
    stub_box: dict = {}

    class _Kb:
        DEFAULT_BOARD = "default"
        DEFAULT_FAILURE_LIMIT = 2

    class _Runner(kw.GatewayKanbanWatchersMixin):
        def __init__(self, ticks: int) -> None:
            self._running = True
            self._ticks = ticks
            self._slept = 0

        def _kanban_dispatcher_boot(self):
            return (lambda: {"kanban": {"auto_decompose": False}}, _Kb(), {})

        async def _sleep_between_ticks(self, interval: float) -> None:
            self._slept += 1
            if self._slept >= self._ticks:
                self._running = False

    class _StubDispatcher:
        def __init__(self, kb, settings) -> None:
            self.calls: list = []
            stub_box["d"] = self

        def auto_decompose_tick(self, per_tick: int) -> int:
            self.calls.append("auto_decompose")
            return 0

        def tick_once(self) -> list:
            self.calls.append("tick_once")
            return script.pop(0) if script else []

        def zero_run_ready(self):
            self.calls.append("zero_run_ready")

        def oldest_pending(self):
            self.calls.append("oldest_pending")
            return PendingWork(board="ops", task_id="t_waiting", age_seconds=3600.0)

        def ready_nonempty(self) -> bool:
            self.calls.append("ready_nonempty")
            raise AssertionError("the retired bad-ticks rule is still read here")

    async def _inline(func, *args):
        # The zombie reaper touches the live board; every other offload runs
        # inline so the test exercises the loop's own wiring.
        if getattr(func, "__name__", "") == "reap_worker_zombies":
            return []
        return func(*args)

    async def _no_sleep(_delay):
        return None

    def _drive(batches):
        script.clear()
        script.extend(batches)
        runner = _Runner(len(batches))
        asyncio.run(runner._kanban_dispatcher_watcher())
        return stub_box["d"]

    monkeypatch.setattr(
        kw, "_KanbanDispatcher", lambda kb, settings: _StubDispatcher(kb, settings)
    )
    monkeypatch.setattr(kw, "_to_thread_process_service", _inline)
    monkeypatch.setattr(kw.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(kw, "_kanban_dispatch_allowed", lambda: allowed["value"])

    with caplog.at_level(logging.INFO, logger="gateway.run"):
        # A claimed card whose worker could not be launched: the one signal an
        # operator must act on, silent on the live line, warns on the first tick.
        stub = _drive(
            [[("ops", DispatchResult(spawn_failed=[("t_stuck", "yaan-coder", "no such profile")]))]]
        )
        messages = [record.getMessage() for record in caplog.records]
        assert any(
            "worker spawn failure" in message and "t_stuck" in message for message in messages
        ), messages
        assert "oldest_pending" in stub.calls
        assert "ready_nonempty" not in stub.calls
        assert "zero_run_ready" in stub.calls  # PART B keeps its own cadence
        caplog.clear()

        # Six skew-yielded ticks with old work waiting: the healthy gate is a
        # decision, so the tracker stays silent through the real loop — while the
        # gate's own refusal line (ruling t_8fed34c8) is still emitted.
        _drive([[("ops", DispatchResult(tick_yielded="tree-skew-yield: content mismatch"))]] * 6)
        skew = [r.getMessage() for r in caplog.records]
        assert not any("consecutive ticks spawned nothing" in m for m in skew), skew
        assert any("tick REFUSED" in m for m in skew), skew
        caplog.clear()

        # A board refused as not dispatch-enabled is a decision too.
        _drive([[("ops", DispatchResult(skipped_board_disabled=True))]] * 6)
        disabled = [r.getMessage() for r in caplog.records]
        assert not any("consecutive ticks spawned nothing" in m for m in disabled), disabled
        caplog.clear()

        # Six unexplained no-op ticks still warn, naming the card that waited.
        _drive([[("ops", DispatchResult())]] * 6)
        stalled = [r.getMessage() for r in caplog.records]
        assert any("consecutive ticks spawned nothing" in m and "t_waiting" in m for m in stalled), (
            stalled
        )
        caplog.clear()

        # `hermes pause`: an idle dispatcher, not a stall.
        allowed["value"] = False
        paused = _drive([[("ops", DispatchResult(spawn_failed=[("t_stuck", "p", "boom")]))]])
        paused_lines = [r.getMessage() for r in caplog.records]
        assert not any(
            "spawn failure" in m or "spawned nothing" in m for m in paused_lines
        ), paused_lines
        assert paused.calls == []
