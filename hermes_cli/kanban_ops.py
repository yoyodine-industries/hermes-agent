"""Dispatcher and maintenance verbs for ``hermes kanban``: ``dispatch``,
``daemon`` (deprecated standalone loop), ``tail``/``watch`` event streaming,
``gc`` and ``repair``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli.kanban_output import _err, _fmt_ts, _print_json


def _kanban_config() -> dict:
    """``config.yaml`` ``kanban:`` section, or ``{}`` when config can't be loaded."""
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        return (cfg.get("kanban", {}) if isinstance(cfg, dict) else {}) or {}
    except Exception:
        return {}


def _poll_loop(interval: float, tick) -> int:
    """Run ``tick()`` every ``interval`` seconds (floor 0.1) until Ctrl-C."""
    try:
        while True:
            tick()
            time.sleep(max(0.1, interval))
    except KeyboardInterrupt:
        print("\n(stopped)")
        return 0


def _cmd_tail(args: argparse.Namespace) -> int:
    last_id = 0
    print(f"Tailing events for {args.task_id}. Ctrl-C to stop.")

    def tick():
        nonlocal last_id
        with kbc.connect_closing() as conn:
            events = kb.list_events(conn, args.task_id)
        for e in events:
            if e.id > last_id:
                pl = f" {e.payload}" if e.payload else ""
                print(f"[{_fmt_ts(e.created_at)}] {e.kind}{pl}", flush=True)
                last_id = e.id

    return _poll_loop(args.interval, tick)


def _max_spawn_by_board(_kanban_cfg: dict) -> dict:
    """``kanban.max_spawn_by_board`` as ``{slug: int}``, junk dropped silently.

    Read here for the MEASUREMENT only: the CLI never passes these ceilings to
    ``dispatch_once`` (that is the gateway dispatcher's job, and it resolves and
    warns about the same map). A value that is not a positive int can never be a
    ceiling, so it is left out rather than coerced into one.
    """
    raw = _kanban_cfg.get("max_spawn_by_board") if isinstance(_kanban_cfg, dict) else None
    if not isinstance(raw, dict):
        return {}
    ceilings: dict = {}
    for slug, value in raw.items():
        if isinstance(value, bool):
            continue
        try:
            ival = int(value)
        except (TypeError, ValueError):
            continue
        if ival < 1 or ival != value:
            continue
        ceilings[str(slug)] = ival
    return ceilings


def _held_lines(res: object, host_budget: dict) -> list[str]:
    """Every card a gate held this tick, named on one line, or ``[]``.

    The SEV1 (card ``t_843cb0a9``): a board at its ceiling and a wedged dispatcher
    printed EXACTLY the same output, because the budget gate's two fields were
    recorded on the result and never published. A held card is the estate's
    normal loaded state, so it prints as a deferral with the cap that held it —
    never as silence, and never as a failure.
    """
    lines: list[str] = []
    detail = getattr(res, "spawn_budget_detail", None) or {}
    gate = getattr(res, "spawn_budget_blocked", None)

    board_ids = list(getattr(res, "deferred_board_capped", None) or [])
    host_ids = list(getattr(res, "deferred_in_progress_capped", None) or [])
    share_ids = list(getattr(res, "deferred_host_capped", None) or [])

    def _cards(ids: list) -> str:
        shown = ", ".join(ids[:12])
        more = "" if len(ids) <= 12 else f" (+{len(ids) - 12} more)"
        return f" — {shown}{more}" if ids else ""

    if gate == "max_spawn":
        lines.append(
            f"Held (board ceiling max_spawn={detail.get('cap')} met: "
            f"{detail.get('board_running')} running on this board): "
            f"{len(board_ids)} ready card(s){_cards(board_ids)}"
        )
    elif gate == "max_in_progress":
        lines.append(
            f"Held (host cap max_in_progress={detail.get('host_cap')} met: "
            f"{detail.get('host_running')} running host-wide, "
            f"{detail.get('board_running')} on this board): "
            f"{len(host_ids)} ready card(s){_cards(host_ids)}"
        )
    elif board_ids:
        lines.append(
            f"Held (board ceiling reached): {len(board_ids)} ready card(s){_cards(board_ids)}"
        )
    if share_ids:
        lines.append(
            f"Held (no host slot for this board this tick — another board drew "
            f"the share): {len(share_ids)} ready card(s){_cards(share_ids)}"
        )
    for task_id, assignee, reason in getattr(res, "spawn_failed", None) or []:
        lines.append(f"Spawn failed: {task_id} ({assignee}): {reason}")
    yielded = getattr(res, "tick_yielded", "")
    if yielded:
        lines.append(f"Tick yielded: {yielded}")
    if host_budget.get("oversubscribed"):
        boards = host_budget.get("boards") or []
        lines.append(
            f"Host ceiling: {len(boards)} x max_spawn={host_budget.get('max_spawn')} = "
            f"{host_budget.get('ceiling_sum')} against max_in_progress="
            f"{host_budget.get('max_in_progress')} — "
            f"{host_budget.get('unreachable_slots')} slot(s) are unreachable by config; "
            f"set kanban.max_spawn_by_board to fit."
        )
    return lines


def _cmd_dispatch(args: argparse.Namespace) -> int:
    # Honour kanban.default_assignee, kanban.max_in_progress,
    # kanban.max_in_progress_per_profile and kanban.max_spawn with the same
    # semantics as the gateway dispatch path.
    max_spawn_by_board: dict = {}
    try:
        from hermes_cli.config import load_config
        _cfg = load_config()
        _kanban_cfg = _cfg.get("kanban", {}) if isinstance(_cfg, dict) else {}
        default_assignee = (_kanban_cfg.get("default_assignee") or "").strip() or None
        max_in_progress_per_profile = kbd._positive_int(
            _kanban_cfg.get("max_in_progress_per_profile"), None
        )
        # Memory-derived default when unset — same fallback the gateway applies.
        max_in_progress = kbd.resolve_max_in_progress(
            kbd._positive_int(_kanban_cfg.get("max_in_progress"), None)
        )
        # CLI --max is the more explicit signal, so it wins over kanban.max_spawn.
        cli_max = getattr(args, "max", None)
        max_spawn = (
            cli_max if cli_max is not None else kbd._positive_int(_kanban_cfg.get("max_spawn"), None)
        )
        max_spawn_by_board = _max_spawn_by_board(_kanban_cfg)
    except Exception:
        default_assignee = max_in_progress_per_profile = max_in_progress = None
        max_spawn = getattr(args, "max", None)
    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(
            conn,
            dry_run=args.dry_run,
            max_spawn=max_spawn,
            max_in_progress=max_in_progress,
            failure_limit=getattr(args, "failure_limit", kbd.DEFAULT_FAILURE_LIMIT),
            default_assignee=default_assignee,
            max_in_progress_per_profile=max_in_progress_per_profile,
        )
    # The CONFIG's own arithmetic: max_spawn is per board, max_in_progress is per
    # host, so the fleet advertises more slots than it can ever hold. Measured
    # here so the numbers ride every dispatch output instead of living in a
    # reader's head (card t_843cb0a9 requirement 2).
    host_budget = kbd.measure_host_budget(
        max_spawn=max_spawn,
        max_in_progress=max_in_progress,
        ceilings=max_spawn_by_board,
    )
    if getattr(args, "json", False):
        _print_json({
            **{k: getattr(res, k)
               for k in ("reclaimed", "crashed", "timed_out", "stale", "auto_blocked", "promoted",
                         "reaped_terminal_workers")},
            "spawned": [
                {"task_id": tid, "assignee": who, "workspace": ws} for (tid, who, ws) in res.spawned
            ],
            "skipped_unassigned": res.skipped_unassigned,
            "skipped_nonspawnable": res.skipped_nonspawnable,
            "skipped_self_review": [
                {"task_id": tid, "reason": reason}
                for (tid, reason) in res.skipped_self_review
            ],
            "skipped_per_profile_capped": [
                {"task_id": tid, "assignee": who, "current": current}
                for (tid, who, current) in res.skipped_per_profile_capped
            ],
            "auto_assigned_default": res.auto_assigned_default,
            "respawn_guarded": [
                {"task_id": tid, "reason": reason}
                for (tid, reason) in res.respawn_guarded
            ],
            "rate_limited": res.rate_limited,
            "skipped_locked": res.skipped_locked,
            "skipped_board_disabled": res.skipped_board_disabled,
            "memory_pressure": res.memory_pressure,
            # The budget gates. These were recorded on the result and never
            # published, so a saturated board reported every bucket empty: the
            # tick that spawns nothing for want of capacity now names the gate,
            # the ceiling that was met, the measured running count, and the cards
            # it held (card t_843cb0a9).
            "spawn_budget_blocked": res.spawn_budget_blocked,
            "spawn_budget": res.spawn_budget_detail,
            "deferred_board_capped": res.deferred_board_capped,
            "deferred_host_capped": res.deferred_host_capped,
            "deferred_in_progress_capped": res.deferred_in_progress_capped,
            "spawn_failed": [
                {"task_id": tid, "assignee": who, "reason": why}
                for (tid, who, why) in res.spawn_failed
            ],
            "skipped_top_level_only": getattr(res, "skipped_top_level_only", []),
            "tick_yielded": res.tick_yielded,
            "host_budget": host_budget,
        }, ascii=True)
        return 0
    if res.skipped_board_disabled:
        # The loudest possible zero: this tick did not run because the board it
        # resolved to is an estate/scratch board (card t_17c9c847). Silence here
        # is what made a redirect to a "scratch" board look like isolation.
        print("Refused:      board is NOT dispatch-enabled (estate/scratch board) — "
              "no card on it can be spawned; `hermes kanban boards set-dispatch "
              "<slug> on` admits it again.")
        return 0
    print(f"Reclaimed:    {res.reclaimed}")
    if res.reaped_terminal_workers:
        print(f"Reaped workers of finished tasks: {', '.join(res.reaped_terminal_workers)}")
    for label, items in (
        ("Crashed:     ", res.crashed),
        ("Timed out:   ", res.timed_out),
        ("Stale:       ", res.stale),
        ("Auto-blocked:", res.auto_blocked),
    ):
        print(f"{label} {len(items)}")
        if items:
            print(f"  {', '.join(items)}")
    print(f"Promoted:     {res.promoted}")
    print(f"Spawned:      {len(res.spawned)}")
    tag = " (dry)" if args.dry_run else ""
    for tid, who, ws in res.spawned:
        print(f"  - {tid}  ->  {who}  @ {ws or '-'}{tag}")
    if res.auto_assigned_default:
        print(
            f"Auto-assigned to kanban.default_assignee={default_assignee!r}: "
            f"{', '.join(res.auto_assigned_default)}"
        )
    if res.skipped_unassigned:
        print(f"Skipped (unassigned): {', '.join(res.skipped_unassigned)}")
    for tid, who, current in res.skipped_per_profile_capped:
        print(f"Deferred ({who} at per-profile cap, {current} running): {tid}")
    if res.skipped_nonspawnable:
        print(
            f"Skipped (non-spawnable assignee — terminal lane, OK): "
            f"{', '.join(res.skipped_nonspawnable)}"
        )
    for tid, reason in res.skipped_self_review:
        if reason == "implementer_unknown":
            print(
                f"Parked in review (no implementer provenance to prove a "
                f"distinct reviewer): {tid}"
            )
        else:
            print(
                f"Parked in review (assignee is the implementer - name a "
                f"distinct reviewer): {tid}"
            )
    for tid, reason in res.respawn_guarded:
        print(f"Guarded ({reason}): {tid}")
    if res.rate_limited:
        print(f"Rate-limited (released to ready, no failure counted): {', '.join(res.rate_limited)}")
    if res.skipped_locked:
        print("Skipped: another dispatcher holds this board's lock (no writes this tick)")
    if res.memory_pressure:
        print(f"Memory pressure {res.memory_pressure}: new workers restricted this tick")
    for line in _held_lines(res, host_budget):
        print(line)
    return 0


_DAEMON_DEPRECATED = (
    "hermes kanban daemon: DEPRECATED — the dispatcher now runs\ninside the gateway. To use "
    "kanban:\n\n    hermes gateway start       # starts the gateway + embedded dispatcher\n\nReady "
    "tasks will be picked up on the next dispatcher tick\n(default: every 60 seconds). Configure "
    "via config.yaml:\n\n    kanban:\n      dispatch_in_gateway: true      # default\n      "
    "dispatch_interval_seconds: 60\n      failure_limit: 2              # consecutive non-success "
    "attempts before auto-block\n\nRunning both the gateway AND this standalone daemon will\nrace "
    "for claims. If you truly need the old standalone\ndaemon (no gateway available), rerun with "
    "--force."
)


def _daemon_health_step(
    health: kbd.DispatcherHealth,
    board: str,
    res: object,
    pending: Optional[kbd.PendingWork] = None,
    now: Optional[float] = None,
) -> Optional[kbd.HealthReport]:
    """One standalone-daemon tick through the shared health tracker.

    Module-level and importable ON PURPOSE (card t_bda9ac11, D7). The tracker
    itself is unit-tested elsewhere, but what was broken on the live line is the
    WIRING: ``observe_tick`` iterates its first argument, so a bare
    ``DispatchResult`` raises ``TypeError: 'DispatchResult' object is not
    iterable`` inside ``collect_spawn_failures`` — and ``run_daemon`` wraps
    ``on_tick(res)`` in ``contextlib.suppress(Exception)``, which turns that into
    a CLI telemetry surface that is silently dead. The tick's result MUST go in
    board-tagged (``[(board, res)]``), and this seam is what a test can hold.

    Returns the report to emit, or ``None`` to stay silent.
    """
    return health.observe_tick([(board, res)], pending=pending, now=now)


def _cmd_daemon(args: argparse.Namespace) -> int:
    """Deprecated — the dispatcher now runs inside the gateway. Kept so old
    scripts/systemd units get a clear migration message; ``--force`` (hidden
    from --help) keeps the standalone loop for hosts that truly cannot run the
    gateway. The default path exits 2 so nobody accidentally runs two
    dispatchers against the same kanban.db."""
    if not getattr(args, "force", False):
        return _err(_DAEMON_DEPRECATED, 2)

    # Init before printing "started" so the DB path is right and init errors
    # surface immediately.
    kb.init_db()

    pidfile = getattr(args, "pidfile", None)
    if pidfile:
        try:
            Path(pidfile).parent.mkdir(parents=True, exist_ok=True)
            Path(pidfile).write_text(str(os.getpid()), encoding="utf-8")
        except OSError as exc:
            print(f"warning: could not write pidfile {pidfile}: {exc}", file=sys.stderr)

    verbose = bool(getattr(args, "verbose", False))
    print(
        f"Kanban dispatcher running STANDALONE via --force (interval={args.interval}s, "
        f"pid={os.getpid()}). Ctrl-C to stop. NOTE: if a gateway is also running with "
        f"dispatch_in_gateway=true (default), you have two dispatchers racing for claims.",
        file=sys.stderr,
    )

    # Health telemetry (shared with the gateway watcher via DispatcherHealth): a
    # warning here must mean the dispatcher is BROKEN, not merely busy. Capacity
    # deferrals (kanban.max_spawn / max_in_progress / per-profile cap), guards
    # (respawn, board-held, memory pressure) and work that just arrived are the
    # steady state on a loaded host and stay silent; a card claimed but never
    # launched warns at once, naming board, profile, task and reason.
    HEALTH_WINDOW = 6  # ticks (default 30s at interval=5)
    health = kbd.DispatcherHealth(window=HEALTH_WINDOW)
    # The board this daemon ticks: the health line's label and the pending
    # probe's board. `run_daemon` connects with no board kwarg, so the current
    # board IS the daemon's board.
    health_board = getattr(args, "board", None)
    try:
        health_board = health_board or kb.get_current_board()
    except Exception:
        health_board = health_board or kb.DEFAULT_BOARD
    last_stuck_probe_at = 0

    def _oldest_pending():
        """Longest-waiting spawnable card on this board, ``None`` when nothing is."""
        try:
            with kbc.connect_closing() as conn:
                task_id, age = kbd.oldest_spawnable_pending(conn)
        except Exception:
            return None
        if not task_id:
            return None
        return kbd.PendingWork(
            board=health_board, task_id=task_id, age_seconds=age
        )

    def _zero_run_ready():
        """Stuck-ready probe on this board: ready cards nothing has attempted."""
        try:
            with kbc.connect_closing() as conn:
                stats = kbd.zero_run_ready(conn)
        except Exception:
            return None
        return stats if int(stats.get("count") or 0) else None

    def _on_tick(res):
        nonlocal last_stuck_probe_at
        report = _daemon_health_step(
            health, health_board, res, _oldest_pending(), time.time()
        )
        if report is not None:
            # One vocabulary, owned by kanban_db_dispatch: the tracker's own
            # message, plus the live line's "held back" annotation on warnings.
            note = (
                kbd.suppression_note([(health_board, res)])
                if report.level == "warning"
                else ""
            )
            print(
                f"[{_fmt_ts(int(time.time()))}] {report.message}{note}",
                file=sys.stderr if report.level == "warning" else sys.stdout,
                flush=True,
            )
        # Stuck-ready probe, on its own (throttled) cadence: ready cards nothing has
        # EVER attempted. The dead-assignee case it exists for is bucketed
        # ``skipped_nonspawnable``, which the stall rule deliberately ignores — so
        # the tracker is silent on exactly the fault that matters most.
        now_stuck = int(time.time())
        if now_stuck - last_stuck_probe_at >= 300:
            last_stuck_probe_at = now_stuck
            stuck = _zero_run_ready()
            if stuck:
                print(
                    f"[{_fmt_ts(now_stuck)}] WARN {kbd.stuck_ready_message(stuck)}",
                    file=sys.stderr, flush=True,
                )
        if not verbose:
            return
        did_work = (
            res.reclaimed or res.crashed or res.timed_out or res.promoted
            or res.spawned or res.auto_blocked or res.stale
        )
        if did_work:
            print(
                f"[{_fmt_ts(int(time.time()))}] reclaimed={res.reclaimed} "
                f"crashed={len(res.crashed)} timed_out={len(res.timed_out)} stale={len(res.stale)} "
                f"promoted={res.promoted} spawned={len(res.spawned)} "
                f"auto_blocked={len(res.auto_blocked)}",
                flush=True,
            )

    try:
        kbd.run_daemon(
            interval=args.interval,
            max_spawn=args.max,
            failure_limit=getattr(args, "failure_limit", kbd.DEFAULT_FAILURE_LIMIT),
            on_tick=_on_tick,
        )
    finally:
        if pidfile:
            try:
                Path(pidfile).unlink()
            except OSError:
                pass
    print("(dispatcher stopped)")
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    """Live-stream task_events to the terminal."""
    kinds = {k.strip() for k in args.kinds.split(",") if k.strip()} if args.kinds else None
    print(f"Watching kanban events (initial board '{kb.get_current_board()}'). Ctrl-C to stop.", flush=True)
    # Seed cursor at the latest id so we don't replay history.
    with kbc.connect_closing() as conn:
        cursor = int(conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM task_events").fetchone()["m"])

    def tick():
        nonlocal cursor
        with kbc.connect_closing() as conn:
            rows = conn.execute(
                "SELECT e.id, e.task_id, e.kind, e.payload, e.created_at,        t.assignee, "
                "t.tenant FROM task_events e LEFT JOIN tasks t ON t.id = e.task_id WHERE e.id > ? "
                "ORDER BY e.id ASC LIMIT 200",
                (cursor,),
            ).fetchall()
        for r in rows:
            cursor = max(cursor, int(r["id"]))
            if (kinds and r["kind"] not in kinds) or (args.assignee and r["assignee"] != args.assignee) \
                    or (args.tenant and r["tenant"] != args.tenant):
                continue
            try:
                payload = json.loads(r["payload"]) if r["payload"] else None
            except Exception:
                payload = None
            pl = f" {payload}" if payload else ""
            print(
                f"[{_fmt_ts(r['created_at'])}] {r['task_id']:10s} "
                f"{r['kind']:18s} (@{r['assignee'] or '-'}){pl}",
                flush=True,
            )

    return _poll_loop(args.interval, tick)


def _cmd_gc(args: argparse.Namespace) -> int:
    """Remove archived tasks' scratch workspaces, old events, and old worker logs."""
    import shutil
    event_days = getattr(args, "event_retention_days", 30)
    log_days = getattr(args, "log_retention_days", 30)
    if event_days < 0 or log_days < 0:
        return _err("kanban gc: retention days must be >= 0 (0 disables that sweep)", 2)
    scratch_root = kb.workspaces_root()
    removed_ws = 0
    with kbc.connect_closing() as conn:
        rows = conn.execute(
            "SELECT id, workspace_kind, workspace_path, branch_name FROM tasks "
            "WHERE status = 'archived'"
        ).fetchall()
        for row in rows:
            if row["workspace_kind"] == "worktree":
                # Backstop for worktrees that escaped the completion/archive hook.
                # Same safety predicate: only clean, fully-pushed worktrees go.
                wt_path = row["workspace_path"]
                if wt_path and Path(wt_path).is_dir():
                    if kbw._defer_shared_worktree_cleanup(conn, row["id"], wt_path):
                        continue
                    kbw._cleanup_worktree_workspace(row["id"], wt_path, row["branch_name"])
                    if not Path(wt_path).is_dir():
                        removed_ws += 1
                continue
            if row["workspace_kind"] != "scratch":
                continue
            path = Path(row["workspace_path"] or (scratch_root / row["id"]))
            # Same containment predicate as completion cleanup (#28818): strictly below a
            # managed root, never the root itself (which holds every task's scratch dir).
            # Cheap existence/symlink check first: most rows were already cleaned at
            # completion, and rmtree refuses a symlink (so it must not be counted).
            if not path.is_dir() or path.is_symlink() or not kbw._is_managed_scratch_path(path):
                continue
            if kbw._defer_shared_workspace_cleanup(conn, row["id"], path):
                continue
            shutil.rmtree(path, ignore_errors=True)
            if not path.exists():
                removed_ws += 1

    removed_events = 0
    if event_days:
        with kbc.connect_closing() as conn:
            removed_events = kb.gc_events(conn, older_than_seconds=event_days * 24 * 3600)
    removed_logs = kb.gc_worker_logs(older_than_seconds=log_days * 24 * 3600) if log_days else 0
    print(f"GC complete: {removed_ws} workspace(s), "
          f"{removed_events} event row(s), {removed_logs} log file(s) removed")
    return 0


def _cmd_repair(args: argparse.Namespace) -> int:
    """Integrity check + narrow index-REINDEX auto-repair. Dispatched BEFORE
    the auto ``kb.init_db()`` (init refuses corrupt DBs). Exit 0 = healthy /
    repaired / no DB file, 1 = still corrupt."""
    try:
        report = kbc.repair_db()
    except Exception as exc:  # locked/busy probe, unexpected I/O
        return _err(f"kanban repair: {exc}")

    if getattr(args, "json", False):
        _print_json({
            "status": report.status,
            "db_path": str(report.db_path),
            "messages": report.messages,
            "post_repair_messages": report.post_repair_messages,
            "backup_path": str(report.backup_path) if report.backup_path else None,
            "reindexed": report.reindexed,
        }, ascii=True)
        return 0 if report.status in {"ok", "repaired", "missing"} else 1

    if report.status == "missing":
        print(f"No kanban DB at {report.db_path} — nothing to repair.")
        return 0
    if report.status == "ok":
        print(f"{report.db_path}: integrity_check ok — no repair needed.")
        return 0
    if report.status == "repaired":
        print(f"{report.db_path}: repaired.")
        print(f"  reindexed: {', '.join(report.reindexed)}")
        if report.backup_path:
            print(f"  pre-repair backup: {report.backup_path}")
        print("  integrity_check now ok.")
        return 0
    # still corrupt
    def err(line: str) -> None:
        print(line, file=sys.stderr)

    err(f"{report.db_path}: CORRUPT.")
    for line in (report.messages or [])[:10]:
        err(f"  {line}")
    if report.reindexed:
        err(f"  REINDEX ({', '.join(report.reindexed)}) attempted but integrity_check is still failing:")
        for line in (report.post_repair_messages or [])[:10]:
            err(f"    {line}")
    else:
        err("  Not an index-only failure — automatic REINDEX repair does not apply (fail-closed).")
    if report.backup_path:
        err(f"  corrupt copy quarantined at: {report.backup_path}")
    err(
        "  Recover manually (copy kanban.db aside FIRST, then run "
        "`sqlite3 <copy> \".recover\"` into a fresh file — never against "
        "the live path, a WAL-reset-vulnerable sqlite3 CLI can corrupt it "
        "further) or move the file aside to start a new board."
    )
    return 1
