"""In-process tick admission and dispatch; job execution stays in scheduler.py."""

import concurrent.futures
import contextlib

# job id -> the engagement key whose deferral was already reported. Pruned when the
# engagement changes, so the map is bounded by the number of due jobs, not by uptime.
_LOCKDOWN_REPORTED: dict = {}


def _lockdown_report_once(key: str, engagement: str, logger, message: str, *args) -> bool:
    """Log ``message`` once per ``(key, engagement)``; True when this call logged it.

    The dedupe every lockdown deferral shares: a refused card/job is refused on every tick,
    so an unconditional write would be a firehose — and a re-arm (new engagement) must be
    reported again, because it is a new decision.
    """
    if _LOCKDOWN_REPORTED.get(key) == engagement:
        return False
    _LOCKDOWN_REPORTED[key] = engagement
    logger.warning(message, *args)
    return True


def _tick_lane() -> "str | None":
    """The profile whose cron store this tick serves — the LANE a lockdown keys on.

    A cron job carries no profile field: the STORE is the profile (the ticker enters
    ``_profile_cron_scope(home)`` once per profile), so the tick's lane is the job's lane.
    ``None`` when it cannot be resolved, which grants nothing under a lockdown.
    """
    try:
        from hermes_cli.profiles import current_profile_name
    except ImportError:
        return None
    try:
        return current_profile_name()
    except Exception:  # pragma: no cover - defensive
        return None


def _apply_lockdown_lane_gate(due_jobs: list, estop_state, logger) -> tuple:
    """``(admitted, held_count)`` — the LANE-scoped DEFCON gate for cron.

    Under ``lockdown`` only the lanes on the sentinel's allowlist may fire: their jobs run,
    every other lane's job is HELD — never fired, never silently skipped. A held job is left
    due (the caller advances only the admitted ids), so it fires on the first tick after the
    lift instead of losing its slot, and the deferral is logged once per engagement naming
    the job and its profile. BOARD IS NOT A TERM: the same job is treated alike on any board.
    """
    if estop_state is None or not estop_state.engaged or estop_state.total:
        return due_jobs, 0
    from agent.estop import engagement_key, work_admitted

    lane = _tick_lane()
    engagement = engagement_key()
    for job_id in [key for key, seen in _LOCKDOWN_REPORTED.items() if seen != engagement]:
        _LOCKDOWN_REPORTED.pop(job_id, None)
    admitted: list = []
    for job in due_jobs:
        if work_admitted(lane, state=estop_state):
            admitted.append(job)
            continue
        job_id = str(job.get("id") or job.get("name") or "?")
        _lockdown_report_once(
            f"job:{job_id}", engagement, logger,
            "Cron job %s held by the lane-scoped lockdown — profile '%s' is not on the "
            "allowlist (admitted: %s); it stays due and fires on the first tick after the lift",
            job_id, lane or "(unresolved)",
            ", ".join(sorted(estop_state.allow_profiles)) or "(none)",
        )
    return admitted, len(due_jobs) - len(admitted)


def tick(verbose=True, adapters=None, loop=None, sync=True, *, can_dispatch=None):
    from hermes_cli.backend_retirement import retirement

    # Hold admission through the entire scan/advance/submit handoff. A predicate alone races
    # prepare after the check but before a due job enters the running-job ledger.
    with retirement.work() as admitted:
        if not admitted:
            return 0
        return _tick_admitted(verbose, adapters, loop, sync, can_dispatch=can_dispatch)


def _tick_admitted(
    verbose: bool = True, adapters=None, loop=None, sync: bool = True, *, can_dispatch=None):
    """Check and run all due jobs. File-locked so only one tick runs at a time (gateway ticker vs
    standalone daemon / manual tick). ``can_dispatch``: optional gate; false leaves due jobs for the
    next allowed tick. Returns the number of jobs executed (0 if another tick holds the lock)."""
    from cron import scheduler as _sched

    # Stale-code yield gate — BEFORE the lock race. A process whose checkout was updated under it
    # serves mixed sys.modules (jobs die on ImportErrors); if a fresher gateway holds the runtime
    # lock, ITS ticker dispatches. With no fresh holder (desktop-standalone) the tick proceeds.
    _skew = _sched._should_yield_tick_to_fresh_gateway()
    if _skew is not None:
        _sched._log_tick_yield_once(f"boot={_skew[0]} disk={_skew[1]}")
        raise _sched.CronTickYielded(_skew[0], _skew[1])

    lock_dir, lock_file = _sched._get_lock_paths()
    _sched._ensure_cron_dir(lock_dir)
    lock_fd = _sched._acquire_tick_lock(lock_file)
    if lock_fd is None:
        return 0

    try:
        # `hermes pause` ESTOP: skip dispatch, never touch in-flight runs; check_paused logs
        # once. TOTAL halt only — a LOCKDOWN is not a halt: the tick RUNS and refuses per LANE
        # below, so an allowlisted lane's jobs keep firing while every other lane's are held.
        with contextlib.suppress(ImportError):
            from agent.estop import check_paused as _estop_check_paused
            if _estop_check_paused("cron", _sched.logger):
                return 0

        _estop_state = None
        with contextlib.suppress(ImportError):
            from agent.estop import read_state as _estop_read_state
            _estop_state = _estop_read_state()

        if can_dispatch is not None and not can_dispatch():
            _sched.logger.debug("Cron dispatch paused while gateway drains existing work")
            return 0

        from cron.bot_chat_delivery import drain, drain_in_background
        if sync:
            drain()
        else:
            drain_in_background()
        _sched._maybe_reap_dead_owners()
        # Periodic worktree GC (6h, threaded) — the only sweep gateway-only boxes get.
        try:
            _sched._maybe_run_worktree_maintenance()
        except Exception as _wt_exc:
            _sched.logger.debug("Worktree maintenance dispatch failed: %s", _wt_exc)

        due_jobs = _sched.get_due_jobs()
        _sched._sweep_stale_inflight_for_tick(due_jobs)
        # Lane-scoped lockdown gate: the sweep above still sees every due job (it is about
        # in-flight runs, not admission); only DISPATCH is scoped to the allowlisted lanes.
        due_jobs, _lockdown_held = _apply_lockdown_lane_gate(due_jobs, _estop_state, _sched.logger)

        if not due_jobs:
            # Idle tick: skip config load + pool setup, but still reap crashed jobs' MCP orphans.
            if verbose and not _lockdown_held:
                # Idle tick: skip config load + pool partitioning entirely (#33612 — the gateway ticker
                # calls tick(verbose=False) every 60s, so idle ticks previously fell through to
                # load_config()). Still run the post-tick MCP orphan sweep: main intentionally sweeps on
                # idle ticks so orphaned stdio children from crashed jobs are reaped even when nothing is
                # due.
                _sched.logger.info("%s - No jobs due", _sched._hermes_now().strftime('%H:%M:%S'))
            _sched._sweep_mcp_orphans()
            return 0

        if verbose:
            _sched.logger.info("%s - %s job(s) due", _sched._hermes_now().strftime('%H:%M:%S'), len(due_jobs))

        # Advance next_run_at for recurring jobs FIRST, under the lock, before any execution
        # (at-most-once). Re-advancing running jobs keeps the grace window alive; mark_job_run
        # overwrites it on completion. Composes with the claim-time advance in claim_job_for_fire.
        # A job HELD by the lane-scoped lockdown is deliberately absent: leaving it due is what
        # makes it fire on the first tick after the lift instead of losing its period.
        _sched.advance_next_runs([job["id"] for job in due_jobs])

        _max_workers = _sched._resolve_max_parallel_workers()
        if verbose:
            _sched.logger.info(
                "Running %d job(s) in parallel (max_workers=%s)",
                len(due_jobs),
                _max_workers if _max_workers else "unbounded")

        def _process_job(job: dict) -> bool:
            return _sched._process_due_job(job, adapters, loop, verbose)

        # Persistent pool, non-blocking dispatch. Already-running jobs are skipped; mark_job_run
        # re-arms next_run_at on completion, so no catch-up queue is needed.
        _results: list = []
        _all_futures: list = []
        pool = _sched._get_parallel_pool(_max_workers)
        for job in due_jobs:
            fut = _sched._submit_with_guard(job, pool, _process_job)
            if fut is None:
                continue
            _all_futures.append(fut)
            if not sync:
                _results.append(True)  # optimistically counted

        if sync:
            for f in concurrent.futures.as_completed(_all_futures):
                try:
                    _results.append(f.result())
                except Exception as exc:
                    _sched.logger.error("Cron job future failed: %s", exc)
                    _results.append(False)
            _sched._sweep_mcp_orphans()
            return sum(_results)

        _sched._sweep_mcp_orphans_when_all_done(_all_futures)
        return sum(_results)
    finally:
        _sched._release_tick_lock(lock_fd)
