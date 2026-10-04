"""Embedded kanban dispatcher: settings resolution and per-tick board work.

``GatewayKanbanWatchersMixin._kanban_dispatcher_watcher`` owns the loop,
the singleton lock and the health telemetry; everything that only needs the
``kanban_db`` module and the resolved settings lives here.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from gateway.kanban_watchers_common import _board_slugs, _positive_int_setting, logger


def _kbc():
    from hermes_cli import kanban_db_connect
    return kanban_db_connect


def _kbd():
    from hermes_cli import kanban_db_dispatch
    return kanban_db_dispatch


def order_boards_by_head_priority(priorities: dict) -> list:
    """Boards in visit order: head-of-line priority first, ties by board name.

    ``priorities`` maps a board slug to the priority of the card that board
    would spawn next (``None`` when nothing on it could be started). Rankless
    boards sort LAST but are never dropped — reclaim, promotion, decomposition
    and health bookkeeping is board-local, so every board still gets its tick.
    """
    ranked = sorted(
        (slug for slug, priority in priorities.items() if priority is not None),
        key=lambda slug: (-int(priorities[slug]), slug),
    )
    unranked = sorted(slug for slug, priority in priorities.items() if priority is None)
    return ranked + unranked


def host_budget_shares(free: Optional[int], boards_in_order: list) -> dict:
    """How many new workers each board may start this tick, from one budget.

    ``free`` is the host-wide free slot count at the start of the tick;
    ``None`` (no cap derived, or the count could not be read) hands out no
    shares at all, so a caller keeps its whole-budget behaviour instead of
    allocating slots nobody proved were free. Every board with startable work
    takes ONE guaranteed slot in visit order until the budget is gone; the
    remainder goes to the board visited first, which keeps the head of line
    ranked first inside a tick without letting it eat the whole budget.
    """
    if free is None or not boards_in_order:
        return {}
    shares: dict[str, int] = {}
    remaining = int(free)
    for slug in boards_in_order:
        if remaining <= 0:
            break
        shares[slug] = 1
        remaining -= 1
    if remaining > 0:
        head = boards_in_order[0]
        shares[head] = shares.get(head, 0) + remaining
    return shares


def host_budget_shares_by_ceiling(
    free: Optional[int], boards_in_order: list, ceilings: Optional[dict] = None
) -> dict:
    """``host_budget_shares`` with per-board ``kanban.max_spawn_by_board`` ceilings.

    The remainder is handed out by ascending CEILING: a board that named a
    ceiling may not be given more than it can use, so the surplus flows to the
    next board instead of being stranded on the head (which is what left a
    capped board below its own ceiling while the capped head held the rest). An
    unnamed board has no ceiling, so the surplus still reaches it; with no
    ceilings configured every board is unbounded, the ordering reduces to the
    visit order, and the allocation is the historical one exactly (the head
    takes the whole remainder).
    """
    if free is None or not boards_in_order:
        return {}
    caps = dict(ceilings or {})
    shares: dict[str, int] = {}
    remaining = int(free)
    for slug in boards_in_order:
        if remaining <= 0:
            break
        shares[slug] = 1
        remaining -= 1
    if remaining <= 0:
        return shares
    position = {slug: index for index, slug in enumerate(boards_in_order)}
    # Ascending ceiling, then visit order: the most constrained board is served
    # first, the head wins ties (and therefore takes an unbounded remainder).
    by_ceiling = sorted(
        boards_in_order,
        key=lambda slug: (caps.get(slug, float("inf")), position[slug]),
    )
    for slug in by_ceiling:
        if remaining <= 0:
            break
        allocated = shares.get(slug, 0)
        ceiling = caps.get(slug)
        room = remaining if ceiling is None else max(int(ceiling) - allocated, 0)
        give = min(remaining, room)
        if give > 0:
            shares[slug] = allocated + give
            remaining -= give
    return shares


_CORRUPT_DB_MARKERS = ("file is not a database", "database disk image is malformed")


@dataclass
class _DispatcherSettings:
    """``kanban.*`` dispatch settings, read once at boot (restart to apply)."""

    interval: float
    max_spawn: Any
    max_in_progress: Optional[int]
    failure_limit: int
    stale_timeout_seconds: int
    reconcile_orphans: bool
    default_assignee: Optional[str]
    max_in_progress_per_profile: Optional[int]
    lane_fair_spawn: bool = True
    designated_pool_reserve: int = 1
    # Per-board spawn ceilings (board slug -> positive int) from
    # ``kanban.max_spawn_by_board``. A board named here uses its own ceiling
    # instead of the global ``max_spawn``; every other board keeps ``max_spawn``.
    max_spawn_by_board: dict = field(default_factory=dict)

    def max_spawn_for_board(self, slug: str) -> Any:
        """The spawn ceiling to apply to *slug* this tick.

        Resolution lives here, at the per-tick call site, so the budget function
        (``_tick_spawn_budget``) stays pure and every board agrees on the
        precedence: a board named in ``kanban.max_spawn_by_board`` uses its own
        value, any other board keeps the global ``kanban.max_spawn``.
        """
        if slug in self.max_spawn_by_board:
            return self.max_spawn_by_board[slug]
        return self.max_spawn


def _parse_max_spawn_by_board(kanban_cfg: dict, kb: Any) -> dict:
    """Parse ``kanban.max_spawn_by_board``: board slug -> positive-int ceiling.

    Boards not named keep the global ``kanban.max_spawn``, so this map is purely
    additive. A non-mapping value, a value below 1, and a slug that names no
    board in the inventory are each IGNORED with a warning — a typo must be
    visible, never fatal and never silently ineffective. An unreadable board
    inventory leaves the map as configured: an entry for a board nothing serves
    simply never matches, so a read failure can never drop a real ceiling.
    """
    raw = kanban_cfg.get("max_spawn_by_board")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        logger.warning(
            "kanban dispatcher: kanban.max_spawn_by_board=%r is not a mapping; ignoring", raw
        )
        return {}
    known: Optional[set] = None
    try:
        known = {b.get("slug") or kb.DEFAULT_BOARD for b in kb.list_boards()}
    except Exception:  # noqa: BLE001 - an unreadable inventory is not fatal
        logger.debug(
            "kanban dispatcher: could not enumerate boards to validate max_spawn_by_board",
            exc_info=True,
        )
    parsed: dict[str, int] = {}
    for slug, value in raw.items():
        key = str(slug).strip()
        try:
            ceiling = int(value)
        except (TypeError, ValueError):
            logger.warning(
                "kanban dispatcher: invalid kanban.max_spawn_by_board[%r]=%r; ignoring", slug, value
            )
            continue
        if not key or ceiling < 1:
            logger.warning(
                "kanban dispatcher: kanban.max_spawn_by_board[%r]=%r must name a board with a "
                "positive ceiling; ignoring", slug, value
            )
            continue
        if known is not None and key not in known:
            logger.warning(
                "kanban dispatcher: kanban.max_spawn_by_board names unknown board %r; ignoring", key
            )
            continue
        parsed[key] = ceiling
    if parsed:
        logger.info("kanban dispatcher: max_spawn_by_board=%s", parsed)
    return parsed


def _resolve_dispatcher_settings(kanban_cfg: dict, kb: Any) -> _DispatcherSettings:
    """Parse and log the dispatcher settings in their established order."""
    try:
        interval = float(kanban_cfg.get("dispatch_interval_seconds", 60) or 60)
    except (ValueError, TypeError):
        logger.warning("kanban dispatcher: invalid dispatch_interval_seconds=%r, using default 60",
                       kanban_cfg.get("dispatch_interval_seconds"))
        interval = 60.0
    interval = max(interval, 1.0)  # sanity floor — tighter than this is a footgun

    max_spawn = kanban_cfg.get("max_spawn")
    if max_spawn is not None:
        logger.info("kanban dispatcher: max_spawn=%s", max_spawn)

    # Per-board overrides of the ceiling above (operator ask: skew bandwidth to
    # one board without a second host-wide dial). Parsed once at boot alongside
    # the rest; every other board keeps ``max_spawn``.
    max_spawn_by_board = _parse_max_spawn_by_board(kanban_cfg, kb)

    # Cap simultaneously running tasks so slow workers don't pile up and time
    # out. Explicit config wins; otherwise a memory-derived default (unbounded
    # fan-out swap-thrashes small hosts), or None where total memory can't be read.
    max_in_progress = _positive_int_setting(kanban_cfg, "max_in_progress")
    effective_max_in_progress = _kbd().resolve_max_in_progress(max_in_progress)
    if max_in_progress is None and effective_max_in_progress is not None:
        logger.info(
            "kanban dispatcher: kanban.max_in_progress unset; using "
            "memory-derived default max_in_progress=%d "
            "(set kanban.max_in_progress in config.yaml to override)",
            effective_max_in_progress,
        )

    raw_failure_limit = kanban_cfg.get("failure_limit", kb.DEFAULT_FAILURE_LIMIT)
    try:
        failure_limit = int(raw_failure_limit)
    except (TypeError, ValueError):
        logger.warning("kanban dispatcher: invalid kanban.failure_limit=%r; using default %d",
                       raw_failure_limit, kb.DEFAULT_FAILURE_LIMIT)
        failure_limit = kb.DEFAULT_FAILURE_LIMIT
    if failure_limit < 1:
        logger.warning("kanban dispatcher: kanban.failure_limit=%r is below 1; using default %d",
                       raw_failure_limit, kb.DEFAULT_FAILURE_LIMIT)
        failure_limit = kb.DEFAULT_FAILURE_LIMIT

    # 0 disables stale detection.
    raw_stale = kanban_cfg.get("dispatch_stale_timeout_seconds", 0)
    try:
        stale_timeout_seconds = int(raw_stale or 0)
    except (TypeError, ValueError):
        logger.warning("kanban dispatcher: invalid kanban.dispatch_stale_timeout_seconds=%r; "
                       "disabling stale detection", raw_stale)
        stale_timeout_seconds = 0

    # Fallback profile for tasks created without an assignee (e.g. via the
    # dashboard). Empty (the schema default) keeps skipping them.
    # When set, the dispatcher applies it to unassigned ready tasks instead of skipping them indefinitely
    # (#27145). Empty string (the schema default) means "no fallback, keep skipping" — backward-compatible
    # with existing installs.
    default_assignee = (kanban_cfg.get("default_assignee") or "").strip() or None
    if default_assignee:
        logger.info("kanban dispatcher: default_assignee=%r (unassigned ready tasks "
                    "will route to this profile)", default_assignee)

    # Fair ready-lane ordering (ruling t_b2865b89). Parsed once at boot with the
    # other dispatch settings; the dispatcher module owns the parse so every
    # entry point (gateway, CLI, standalone daemon) agrees on the defaults.
    _fair_lane_spawn, _designated_pool_reserve = _kbd().lane_fair_ready_config(kanban_cfg)
    if not _fair_lane_spawn:
        logger.info("kanban dispatcher: lane_fair_spawn=false; ready lane uses the plain "
                    "priority order")
    elif _designated_pool_reserve:
        logger.info("kanban dispatcher: lane_fair_spawn on, designated_pool_reserve=%d",
                    _designated_pool_reserve)

    return _DispatcherSettings(
        interval=interval,
        max_spawn=max_spawn,
        max_in_progress=effective_max_in_progress,
        failure_limit=failure_limit,
        stale_timeout_seconds=stale_timeout_seconds,
        # Requeue 'running' cards with broken claim bookkeeping (zombie-card
        # reconciliation); false keeps orphans frozen for manual forensics.
        reconcile_orphans=bool(kanban_cfg.get("reconcile_orphans", True)),
        default_assignee=default_assignee,
        # Per-profile concurrency cap: no single profile's local model / API
        # quota / browser pool gets overwhelmed by a fan-out.
        max_in_progress_per_profile=_positive_int_setting(kanban_cfg, "max_in_progress_per_profile"),
        # Fair ready-lane ordering (ruling t_b2865b89) and the slot count the
        # designated tranche holds back for ordinary lanes. Parsed by the
        # dispatcher module so every entry point agrees on the defaults.
        lane_fair_spawn=_fair_lane_spawn,
        designated_pool_reserve=_designated_pool_reserve,
        # Per-board overrides of max_spawn (slug -> ceiling); unnamed boards keep
        # max_spawn. Resolved per tick by max_spawn_for_board().
        max_spawn_by_board=max_spawn_by_board,
    )


class _KanbanDispatcher:
    """Per-tick board work for the embedded dispatcher (runs in worker threads).

    Boards are enumerated every tick so a board created mid-run is picked up
    without a restart. Corrupt-looking board DBs are quarantined per
    fingerprint and retried after ``CORRUPT_BOARD_RETRY_AFTER_SECONDS``:
    transient WAL/open races can look like "malformed" for one tick.
    """

    CORRUPT_BOARD_RETRY_AFTER_SECONDS = 300

    def __init__(self, kb: Any, settings: _DispatcherSettings) -> None:
        self.kb = kb
        self.settings = settings
        self.disabled_corrupt_boards: dict[str, tuple[tuple[str, int | None, int | None], float]] = {}
        # The visit order starts one board further along after every tick: when
        # more boards have work than the host budget has free slots, the board
        # left out this tick is a different board next tick.
        self.rotation_cursor = 0
        # Ages per-board host-budget deferral across ticks, so a board losing the
        # race for over an hour says so instead of looking idle.
        self.host_cap_starvation = _kbd().HostCapStarvationClock()

    def _board_slugs(self) -> list:
        """Boards the dispatcher may serve — estate/scratch boards are EXCLUDED.

        ``list_dispatch_boards`` is the dispatcher's OWN enumeration (card
        t_17c9c847): it drops a board whose ``board.json`` carries
        ``"dispatch": false`` at the source, so a rehearsal estate never reaches
        ``tick_once_for_board`` and its phantom cards are never spawned. When that
        reader is absent (a kb double) or faults, the plain list is filtered
        through the same chokepoint rather than visited whole — the estate half of
        the guard survives.
        """
        reader = getattr(self.kb, "list_dispatch_boards", None)
        if callable(reader):
            try:
                return [
                    b.get("slug") or self.kb.DEFAULT_BOARD
                    for b in reader()
                ]
            except Exception:
                pass
        slugs = _board_slugs(self.kb)
        enabled = getattr(self.kb, "board_dispatch_enabled", None)
        if not callable(enabled):
            # A kb object with neither method (a double, or an older surface):
            # there is no admission flag to read, so keep the plain list.
            return slugs
        out: list = []
        for slug in slugs:
            try:
                if enabled(slug):
                    out.append(slug)
            except Exception:
                continue  # unreadable admission = not admitted
        return out

    def head_of_line_priority(self, slug: str) -> Optional[int]:
        """Priority of the card this board would spawn next, or ``None``.

        One read-only connect per board — never a cross-board merge of every
        card. A board that cannot be read is a RANKING MISS, not a failed tick:
        ``None`` sorts it last, and it is still visited.
        """
        conn = None
        try:
            conn = _kbc().connect(board=slug)
            return _kbd().head_of_line_priority(conn)
        except Exception:
            return None
        finally:
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.close()

    def _head_of_line_priorities(self) -> dict:
        """``{slug: head-of-line priority or None}`` — one probe per board."""
        return {slug: self.head_of_line_priority(slug) for slug in self._board_slugs()}

    def board_visit_order(self, priorities: Optional[dict] = None, *, rotate: bool = True) -> list:
        """This tick's board order: head-of-line rank, rotated by one board.

        Rotation moves only where the order STARTS, so the priority rank inside
        a tick is untouched and every board still gets a turn.
        """
        if priorities is None:
            priorities = self._head_of_line_priorities()
        order = order_boards_by_head_priority(priorities)
        if rotate and len(order) > 1:
            offset = self.rotation_cursor % len(order)
            self.rotation_cursor += 1
            return order[offset:] + order[:offset]
        return order

    def free_host_budget(self) -> Optional[int]:
        """Host-wide free worker slots at the start of a tick, or ``None``.

        ``None`` when no cap is derived (uncapped dispatch) or when the
        host-wide running count cannot be read: shares are only handed out
        against a count that could actually be established.
        """
        cap = self.settings.max_in_progress
        if cap is None:
            return None
        total = _kbd().total_running_all_boards()
        if total is None:
            return None
        return max(int(cap) - int(total), 0)

    def board_db_fingerprint(self, slug: str) -> tuple[str, int | None, int | None]:
        path = self.kb.kanban_db_path(slug)
        try:
            resolved = str(path.expanduser().resolve())
        except Exception:
            resolved = str(path)
        try:
            stat = path.stat()
        except OSError:
            return (resolved, None, None)
        return (resolved, stat.st_mtime_ns, stat.st_size)

    def is_corrupt_board_db_error(self, exc: Exception) -> bool:
        if isinstance(exc, _kbc().KanbanDbCorruptError):
            return True
        return isinstance(exc, sqlite3.DatabaseError) and any(m in str(exc).lower() for m in _CORRUPT_DB_MARKERS)

    def _quarantine_lifted(self, slug: str, fingerprint: tuple) -> bool:
        """Return False while *slug* stays quarantined; lift (and log) otherwise."""
        disabled_entry = self.disabled_corrupt_boards.get(slug)
        if disabled_entry is None:
            return True
        disabled_fingerprint, disabled_at = disabled_entry
        age = time.monotonic() - disabled_at
        if disabled_fingerprint == fingerprint and age < self.CORRUPT_BOARD_RETRY_AFTER_SECONDS:
            return False
        if disabled_fingerprint == fingerprint:
            logger.info("kanban dispatcher: board %s database fingerprint unchanged "
                        "after %.0fs quarantine; retrying dispatch", slug, age)
        else:
            logger.info("kanban dispatcher: board %s database changed; retrying dispatch", slug)
        self.disabled_corrupt_boards.pop(slug, None)
        return True

    def tick_once_for_board(self, slug: str, host_budget_share: Optional[int] = None) -> Optional[object]:
        """Run one dispatch_once for a specific board.

        The per-board DB is opened explicitly so boards never share a
        connection or claim across each other.

        ``host_budget_share`` caps how many workers THIS tick may start on the
        board (its slice of the free host slots). ``None`` means no share was
        allocated and only the host-wide cap applies — the behaviour every
        caller that does not share a budget keeps.
        """
        conn = None
        fingerprint = self.board_db_fingerprint(slug)
        if not self._quarantine_lifted(slug, fingerprint):
            return None
        # ``interval`` is not a dispatch_once kwarg, and the per-board ceiling is
        # resolved here (once per tick) rather than passed as the raw map: the
        # board gets its own value from kanban.max_spawn_by_board, or the global
        # kanban.max_spawn when it is not named.
        kwargs = {
            k: v for k, v in asdict(self.settings).items()
            if k not in ("interval", "max_spawn_by_board")
        }
        kwargs["max_spawn"] = self.settings.max_spawn_for_board(slug)
        if host_budget_share is not None:
            kwargs["host_budget_share"] = int(host_budget_share)
        try:
            # No explicit init_db(): connect() runs the migration once per
            # process (see the matching note in the notifier collector).
            conn = _kbc().connect(board=slug)
            return _kbd().dispatch_once(conn, board=slug, **kwargs)
        except Exception as exc:
            if self.is_corrupt_board_db_error(exc):
                self.disabled_corrupt_boards[slug] = (fingerprint, time.monotonic())
                logger.error(
                    "kanban dispatcher: board %s database %s is not a valid "
                    "SQLite database; pausing dispatch for this board until "
                    "the file changes, the gateway restarts, or the "
                    "quarantine timer expires. Move or restore the file, "
                    "then run `hermes kanban init` if you need a fresh board.",
                    slug, fingerprint[0],
                )
                return None
            logger.exception("kanban dispatcher: tick failed on board %s", slug)
            return None
        finally:
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.close()

    def tick_once(self) -> list[tuple[str, Optional[object]]]:
        """Run one dispatch_once per board. Returns (slug, result) pairs.

        The host-wide budget is SHARED, not raced. Boards are visited in
        head-of-line priority order rotated one board per tick, and the free
        host slots are handed out one guaranteed slot per board with startable
        work, the remainder to the board visited first. Each board may then
        start at most its share this tick, so a board with a deep queue can no
        longer take the slot another board's queue is waiting for. A board
        deferred by the host budget for over an hour is reported
        (``HostCapStarvationClock``).

        When ``kanban.max_spawn_by_board`` names a ceiling, the remainder is
        handed out by ascending ceiling instead, so a capped board's surplus
        flows on rather than being stranded on the head.
        """
        priorities = self._head_of_line_priorities()
        order = self.board_visit_order(priorities)
        with_work = [slug for slug in order if priorities.get(slug) is not None]
        free = self.free_host_budget()
        # ``free is None`` = no allocation was possible (uncapped, or the free
        # slot count could not be read): every board keeps its whole-budget
        # behaviour. Otherwise a board with work that drew no slot gets 0, not
        # ``None`` — it must not spend the slots the other boards were given.
        if self.settings.max_spawn_by_board:
            # A per-board ceiling is configured: allocate by ascending ceiling
            # so a capped board's slack reaches the next one instead of being
            # stranded on the head.
            shares = host_budget_shares_by_ceiling(
                free, with_work, self.settings.max_spawn_by_board
            )
        else:
            shares = host_budget_shares(free, with_work)
        results: list[tuple[str, Optional[object]]] = []
        for slug in order:
            share = shares.get(slug, 0) if (free is not None and slug in with_work) else None
            results.append((slug, self.tick_once_for_board(slug, host_budget_share=share)))
        starved = self.host_cap_starvation.observe(results)
        if starved:
            logger.warning("%s", starved)
        return results

    def ready_nonempty(self) -> bool:
        """Is there a ready+assigned+unclaimed task on ANY board the dispatcher would spawn for?

        Control-plane lanes (e.g. ``orion-cc``) are pulled by terminals via
        ``claim_task`` and never spawnable — a queue full of those is
        "correctly idle", not "stuck". The review column is probed only when
        review dispatch is on (same gate as the dispatcher): a task waiting
        for a human reviewer is idle, not stuck.
        """
        kbd = _kbd()
        _review_probe = kbd.review_dispatch_enabled()
        for slug in self._board_slugs():
            conn = None
            try:
                conn = _kbc().connect(board=slug)
                if kbd.has_spawnable_ready(conn) or (_review_probe and kbd.has_spawnable_review(conn)):
                    return True
            except Exception:
                continue
            finally:
                if conn is not None:
                    with contextlib.suppress(Exception):
                        conn.close()
        return False

    def auto_decompose_tick(self, auto_decompose_per_tick: int) -> int:
        """Auto-decompose up to N triage tasks across all boards into ready workgraphs.

        Runs before dispatch fans out; the per-tick cap keeps a bulk triage
        load from burst-spending the aux LLM. Returns the number decomposed.
        """
        try:
            from hermes_cli import kanban_decompose as _decomp
        except Exception as exc:  # pragma: no cover
            logger.warning("kanban auto-decompose: import failed (%s); skipping", exc)
            return 0
        attempted = 0
        successes = 0
        with _default_profile_secret_scope():
            for slug in self._board_slugs():
                if attempted >= auto_decompose_per_tick:
                    break
                # Pin the board via env for the call: the decomposer connects
                # with no board kwarg (same pattern as the dashboard specify endpoint).
                prev_env = os.environ.get("HERMES_KANBAN_BOARD")
                try:
                    os.environ["HERMES_KANBAN_BOARD"] = slug
                    try:
                        triage_ids = _decomp.list_triage_ids()
                    except Exception as exc:
                        logger.debug("kanban auto-decompose: list_triage_ids failed on board %s (%s)", slug, exc)
                        triage_ids = []
                    for tid in triage_ids:
                        if attempted >= auto_decompose_per_tick:
                            break
                        attempted += 1
                        successes += self._decompose_one(_decomp, slug, tid)
                finally:
                    if prev_env is None:
                        os.environ.pop("HERMES_KANBAN_BOARD", None)
                    else:
                        os.environ["HERMES_KANBAN_BOARD"] = prev_env
        return successes

    @staticmethod
    def _decompose_one(_decomp: Any, slug: str, tid: str) -> int:
        """Decompose one triage task; returns 1 on success, 0 otherwise."""
        try:
            outcome = _decomp.decompose_task(tid, author="auto-decomposer")
        except Exception:
            logger.exception("kanban auto-decompose: decompose_task crashed on %s", tid)
            return 0
        if not outcome.ok:
            # Common no-op reasons (no aux client) must not spam logs every tick.
            logger.debug("kanban auto-decompose [%s]: %s skipped: %s", slug, tid, outcome.reason)
            return 0
        if outcome.fanout and outcome.child_ids:
            logger.info("kanban auto-decompose [%s]: %s → %d children", slug, tid, len(outcome.child_ids))
        else:
            logger.info("kanban auto-decompose [%s]: %s → single task (no fanout)", slug, tid)
        return 1


@contextlib.contextmanager
def _default_profile_secret_scope():
    """Install the gateway launch profile's secret scope while multiplexing is on.

    The tick runs via ``_to_thread_process_service`` in a fresh context, so no
    per-turn scope exists and ``get_secret`` fails closed. The decomposer's aux
    LLM reads ``auxiliary.*`` from ``get_hermes_home()``, so its credentials come
    from that same home. No-op for single-profile gateways.
    """
    from agent.secret_scope import (
        build_profile_secret_scope, is_multiplex_active, reset_secret_scope, set_secret_scope)
    from hermes_constants import get_hermes_home

    if not is_multiplex_active():
        yield
        return
    token = set_secret_scope(
        build_profile_secret_scope(Path(get_hermes_home())), profile_home=str(get_hermes_home()))
    try:
        yield
    finally:
        reset_secret_scope(token)


def _log_spawn_results(results: Optional[list]) -> bool:
    """Log per-board spawn summaries; returns whether any board spawned."""
    any_spawned = False
    for slug, res in (results or []):
        if res is None:
            continue
        # A repair that could not run, or could not lower a row, is logged whether or not this
        # board spawned: the tick's spawn line is quiet by default, and a board whose over-claim
        # repair silently stopped repairing is exactly the false green this fleet bans.
        refused = list(getattr(res, "priority_demote_refused", ()) or ())
        if getattr(res, "priority_demote_error", None) or refused:
            logger.error(
                "kanban dispatcher [%s]: above-tranche repair incomplete (%d row(s) still above "
                "the ceiling%s): %s",
                slug, len(refused),
                ", the pass itself failed" if getattr(res, "priority_demote_error", None) else "",
                getattr(res, "priority_demote_error", None) or "; ".join(refused[:3]),
            )
        if getattr(res, "spawned", None):
            any_spawned = True
            # Quiet by default: an idle gateway stays silent.
            logger.info(
                "kanban dispatcher [%s]: spawned=%d reclaimed=%d "
                "crashed=%d timed_out=%d goal_armed=%d promoted=%d auto_blocked=%d",
                slug, len(res.spawned), res.reclaimed,
                len(res.crashed) if hasattr(res.crashed, "__len__") else 0,
                len(res.timed_out) if hasattr(res.timed_out, "__len__") else 0,
                len(getattr(res, "goal_armed", ()) or ()),
                res.promoted,
                len(res.auto_blocked) if hasattr(res.auto_blocked, "__len__") else 0,
            )
    return any_spawned
