"""Dispatcher: crash/stale/orphan detection, failure accounting and the respawn circuit breaker, memory-aware concurrency caps, the one-shot ``dispatch_once`` pass, worker spawning (``_default_spawn``), worker-log rotation and the long-lived ``run_daemon`` loop.

Split out of ``hermes_cli.kanban_db``; origin-resident helpers are reached
late-bound via ``_kb`` (import-cycle breaking) so monkeypatching
``kanban_db.<name>`` keeps working.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Iterable
from typing import Mapping
from typing import Optional
from typing import TYPE_CHECKING

from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from hermes_cli.kanban_db import Task


# After this many consecutive non-success attempts on a task/profile the
# dispatcher parks the task in ``blocked`` with a reason — prevents retry storms.
DEFAULT_FAILURE_LIMIT = 2

# Worker log files larger than this at spawn time are rotated.
DEFAULT_LOG_ROTATE_BYTES = 2 * 1024 * 1024   # 2 MiB
DEFAULT_LOG_BACKUP_COUNT = 1

# Keep a little wall-clock budget for the worker to observe a terminal timeout
# and make a terminal board call (kanban_block/kanban_complete/kanban_request_review)
# before max_runtime_seconds kills it.
KANBAN_TERMINAL_TIMEOUT_GRACE_SECONDS = 30

# A healthy worker is still alive for a while after kanban_complete /
# kanban_request_review returns (final assistant turn, session persistence), so
# a run's retained worker is only reaped once ended_at is at least this old
# (two default dispatch ticks).
TERMINAL_WORKER_REAP_GRACE_SECONDS = 120

# ---------------------------------------------------------------------------
# Respawn guard constants
# ---------------------------------------------------------------------------

# Patterns in last_failure_error that indicate a quota / auth blocker.
# These errors won't resolve by retrying immediately — auto-block instead.
# The auth family is a curated list, not an open `auth\w*` stem: that stem
# also matched ordinary English words like "author"/"authored"/"authoring"/
# "authoritative" in worker progress prose, parking a healthy card forever
# (#117009).
_RESPAWN_BLOCKER_RE = re.compile(
    r"\b(quota|rate[\s_\-]?limit|429|403|"
    r"auth|authenticat(?:e|es|ed|ing|ion)|authoriz(?:e|es|ed|ing|ation)|"
    r"authoris(?:e|es|ed|ing|ation)|authz|"
    r"unauthorized|forbidden|billing|subscription|"
    r"access[\s_]denied|permission[\s_]denied|"
    r"invalid[\s_]api[\s_]key)\b",
    re.IGNORECASE,
)

# Within this window a completed run counts as "recent proof"; don't re-spawn.
_RESPAWN_GUARD_SUCCESS_WINDOW = 3600  # 1 hour

# Cooldown after a rate-limited (quota-wall) requeue before re-spawning. Without
# it the task would re-spawn on the very next tick and bounce off the same quota
# wall, burning a worker slot every tick for hours. Overridable via
# ``HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS``.
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 300  # 5 minutes

# Within this window a GitHub PR URL in a comment blocks re-spawn.
#
# DO NOT SHORTEN THIS. A shorter window re-spawns a worker against the PR it
# itself authored — the duplicate-work behaviour this guard exists to prevent.
# The 2026-09-26 field incident showed the window was not the mechanism of
# harm: three cards each cleared at their own 24h mark and the board-wide stall
# was their overlap. The fix was the predicate below (a deliberate re-queue
# lifts the guard) plus a board-health signal that distinguishes starved from
# idle. There is deliberately no env/config override for it: the guard's policy
# must stay evidenced per host, and a starvation must not be configurable away.
_RESPAWN_GUARD_PR_WINDOW = 86400  # 24 hours

_RESPAWN_GUARD_PR_URL_RE = re.compile(
    r"https?://github\.com/[^/\s]+/[^/\s]+/pull/\d+",
    re.IGNORECASE,
)

# The two respawn guards share ONE vocabulary for "somebody deliberately put
# this card back in the lane": a done->ready drag (`status`), a parent-completion
# re-promotion (`promoted`) and an operator/agent unblock (`unblocked`). Guard 3
# has always honoured these; guard 4 now does too. Without it a card merely
# NAMING somebody else's PR — a precondition, a parent's merge, the merge it
# exists to verify — sat held for a full 24h, and the only lifts guard 4 had
# (`assigned` / `changes_requested`) would have fabricated an ownership move or
# a review verdict that never happened.
_RESPAWN_GUARD_REQUEUE_KINDS = ("status", "promoted", "unblocked")

# Crash recovery, NOT a decision. Guard 3 accepts it; guard 4 deliberately does
# not — the worker that opened the PR is still not re-spawned against it. Both
# guards read their vocabulary from this module, so the divergence is a decision
# recorded in one place rather than drift between two SQL strings.
_RESPAWN_GUARD_RECOVERY_KINDS = ("reclaimed",)

# Guard 4's handoff kinds: an event naming the profile that must now work on
# THAT PR — an operator reassign, a reviewer's changes_requested, a review
# reopen.
_RESPAWN_GUARD_HANDOFF_KINDS = ("assigned", "changes_requested", "review_reopened")

# A hold EPISODE writes ONE `respawn_guarded` event, not one per tick. Guard 3
# and the self-review guard already work this way; guard 4 did not, and the
# signal became unreadable precisely because of its volume — 8837 `active_pr`
# events across 24 cards all-time, 412 of them for a single card in one 6.9h
# stall. Re-emit only when the reason changes or this interval elapses.
_RESPAWN_GUARD_EVENT_REPEAT_SECONDS = 3600  # 1 hour

# Events that END a hold episode: the card left the guarded state (spawned,
# claimed, completed, released) or its lane decision changed. A card held across
# N ticks therefore writes ONE event, and a card that leaves the queue and comes
# back starts a fresh episode.
_RESPAWN_GUARD_EPISODE_BREAK_KINDS = (
    "spawned", "claimed", "completed", "blocked", "unblocked", "promoted",
    "status", "assigned", "changes_requested", "review_reopened", "reclaimed",
)

# Consecutive ticks in which ready work exists and NOTHING spawnable could start
# before the stall escalates to a card (matches the gateway/daemon health
# window). The repeat interval, combined with the time-bucketed idempotency key
# on the card, bounds a long stall to one escalation per board per interval.
_STALL_ESCALATION_WINDOW = 6
_STALL_ESCALATION_REPEAT_SECONDS = 3600  # 1 hour

# Profile-name convention used to route an escalation: `<lane>-<role>`.
# A lane's design-authority seat is `<lane>-stl`.
_LANE_ROLE_SUFFIXES = ("-coder", "-worker", "-stl", "-sme", "-spec", "-lead")
_LANE_SEAT_SUFFIX = "-stl"


def _kind_clause(kinds: tuple[str, ...]) -> str:
    """``IN (?, ?)`` placeholder list for a fixed tuple of event kinds.

    Kinds come from module constants only (never from a caller), so the SQL text
    is fixed while every value still binds as a parameter.
    """
    return "(" + ", ".join("?" for _ in kinds) + ")"


@dataclass
class DispatchResult:
    """Outcome of a single ``dispatch`` pass.

    ``kanban.default_assignee`` applied this tick before spawning (#27145). Surfaces the auto-assignment to
    telemetry / CLI / dashboard so the operator can see when the dispatcher is acting on the fallback rule
    ``kanban.max_in_progress_per_profile`` (#21582). Each entry is ``(task_id, assignee,
    current_running_count)``. NOT an operator-actionable failure — the task will be picked up on a
    subsequent tick when the assignee has capacity. Separate bucket so telemetry / dashboards can show "this
    profile is busy" vs
    the board's dispatch lock (issue #35240). A losing dispatcher does no DB writes this tick — the lock
    holder is making progress on the same board. This is the steady-state signal that a single-writer guard
    is
    """

    reclaimed: int = 0
    promoted: int = 0
    reconciled_orphans: list[str] = field(default_factory=list)
    """``running`` cards requeued by :func:`reconcile_orphaned_running` (broken
    claim bookkeeping, dead/gone worker)."""
    reaped_terminal_workers: list[str] = field(default_factory=list)
    """Task ids whose worker outlived its closed run and was terminated by
    :func:`reap_terminal_workers`."""
    spawned: list[tuple[str, str, str]] = field(default_factory=list)
    """``(task_id, assignee, workspace_path)`` triples."""
    skipped_unassigned: list[str] = field(default_factory=list)
    """Ready task ids with no assignee at all — operator-actionable (usually a
    misfiled task waiting for routing)."""
    auto_assigned_default: list[str] = field(default_factory=list)
    """Unassigned task ids that had ``kanban.default_assignee`` applied this
    tick before spawning, so telemetry/CLI/dashboard can show the dispatcher
    acting on the fallback rule rather than explicit assignments."""
    skipped_nonspawnable: list[str] = field(default_factory=list)
    """Ready task ids whose assignee names a control-plane lane (e.g. a Claude
    Code terminal like ``orion-cc``), not a Hermes profile. Expected steady-state
    on multi-lane setups, NOT operator-actionable; tracked apart so health
    telemetry can tell "stuck" from "correctly idle"."""
    skipped_self_review: list[tuple[str, str]] = field(default_factory=list)
    """``(task_id, reason)`` review rows parked because the assignee is the
    card's OWN implementer — ``"assignee_is_implementer"`` (spawning it would
    hand the review skill to the bot that wrote the change) or
    ``"implementer_unknown"`` (no implementer provenance, so a distinct reviewer
    cannot be proven). Fail-closed: the row stays in ``review`` until a distinct
    reviewer is named. Needs a human, so it is bucketed apart from the
    "busy, retry later" per-profile deferrals."""
    skipped_per_profile_capped: list[tuple[str, str, int]] = field(default_factory=list)
    """``(task_id, assignee, current_running_count)`` deferred because the
    assignee is at ``kanban.max_in_progress_per_profile``. Picked up on a later
    tick; separate bucket so dashboards show "profile busy" vs "stuck"."""
    skipped_lockdown: list[tuple[str, str]] = field(default_factory=list)
    """``(task_id, assignee)`` deferred because a lane-scoped DEFCON lockdown holds that
    LANE. Board placement is not a factor: the same card spawns or not on ANY board. Each
    entry is recorded on the card as a ``skipped_lockdown`` task event (one row per card per
    engagement), because the silent non-spawn this bucket replaces is exactly how three
    consecutive train failures went unseen."""
    priority_demoted: list[tuple[str, int, int]] = field(default_factory=list)
    """The above-tranche repair pass's report (``t_6ce41549``): ``(task_id, was, now)`` for each
    row LOWERED into its class ceiling this tick. Empty in the steady state — a clean board
    costs one SELECT — and the durable per-card record is the ``priority_demoted`` event the
    pass writes, so a lane can see its own card was moved instead of finding out by order."""
    priority_demote_refused: list[str] = field(default_factory=list)
    """``"<task_id> (<was> -> <now>): <error>"`` for each row the above-tranche repair pass could
    NOT lower this tick. A refusal is NOT a success: the row still carries its over-claim, so it
    is named on the tick's own record and logged at ERROR. Never expected — the pass and the
    storage guard read one predicate (``kanban_db.tranche_entitlement``) — and this bucket is
    what makes a disagreement visible instead of silent."""
    priority_demote_error: Optional[str] = None
    """Set when the above-tranche repair pass itself raised instead of returning. The tick
    CONTINUES and dispatches anyway (2026-10-01: an abort in this pass took the whole ``defcon``
    board's dispatching down for 6.5 h, before the spawn loop was reached), so the failure — not
    the tick — is what this field reports."""
    crashed: list[str] = field(default_factory=list)
    """Task ids reclaimed because their worker PID disappeared."""
    auto_blocked: list[str] = field(default_factory=list)
    """Task ids auto-blocked by the spawn-failure circuit breaker."""
    timed_out: list[str] = field(default_factory=list)
    """Task ids whose workers exceeded ``max_runtime_seconds``."""
    goal_armed: list[str] = field(default_factory=list)
    """Task ids given a bounded goal loop this tick because their previous run died of ITERATION
    exhaustion (``arm_goal_mode_after_budget_death``). Deterministic — the trigger is the error the
    failed run itself recorded — and the per-card record is the ``goal_armed`` event; this bucket is
    what the tick summary counts."""
    stale: list[str] = field(default_factory=list)
    """Task ids reclaimed for no heartbeat within ``dispatch_stale_timeout_seconds``."""
    respawn_guarded: list[tuple[str, str]] = field(default_factory=list)
    """``(task_id, reason)`` skipped by the respawn guard: ``"blocker_auth"``
    (quota/auth error — also auto-blocked), ``"recent_success"`` (completed run
    within guard window), ``"active_pr"`` (GitHub PR URL in a recent comment)."""
    rate_limited: list[str] = field(default_factory=list)
    """Task ids whose workers bailed on a provider rate-limit / quota wall
    (EX_TEMPFAIL sentinel exit) and were released to ``ready`` WITHOUT counting
    a failure — a long quota window must never trip the circuit breaker."""
    skipped_locked: bool = False
    """True when another process held the board's dispatch lock: this tick did
    no DB writes; the lock holder is making progress on the same board."""
    memory_pressure: Optional[str] = None
    """Memory pressure that restricted this tick: ``"critical"`` (no new
    workers), ``"elevated"`` (at most one), ``None`` (no restriction).
    Reclaim/promotion bookkeeping still ran; deferred tasks stay queued."""
    deferred_host_capped: list[str] = field(default_factory=list)
    """Task ids the HOST-wide worker budget (``kanban.max_in_progress``) left
    unspawned this tick, head of line first. The board did not choose this and
    its own queue looks idle, so the tick has to say it out loud — see
    :class:`HostCapStarvationClock`."""
    deferred_board_capped: list[str] = field(default_factory=list)
    """Task ids this board's OWN spawn ceiling left unspawned this tick, head of
    line first. The ceiling is the board's ``kanban.max_spawn_by_board`` value,
    or the global ``kanban.max_spawn`` for a board that is not named. A ceiling
    can be deliberate (skewing bandwidth to another board), so this is NOT a
    starvation signal like :attr:`deferred_host_capped` — but from the outside
    the queue still looks idle, so the tick names what the ceiling held back."""
    skipped_board_disabled: bool = False
    """True when the tick refused to run AT ALL: the board it resolved to is not
    dispatch-enabled (an estate/rehearsal board, ``board.json`` ``"dispatch":
    false``). Nothing was claimed, promoted, reclaimed or spawned — the refusal
    happens BEFORE the lock, the reclaim phase and every claim, so the spawn path
    is never reached and no card on that board can become lane work (card
    t_17c9c847)."""


def describe_suppression(results: Iterable[Optional["DispatchResult"]]) -> str:
    """One line naming why the tick(s) held ready work back, or ``""``.

    ``active_pr=1, recent_success=2, rate_limited=1, skipped_locked=1,
    skipped_per_profile_capped=3, skipped_nonspawnable=12, skipped_unassigned=1,
    lockdown=2 (research-sme x2), memory_pressure=critical`` — the respawn-guard
    reasons counted per task plus EVERY tick-level hold. Feeds the "dispatcher
    stuck" warnings of the CLI daemon and the embedded gateway dispatcher, which
    otherwise report a bare zero-spawn count while ``hermes kanban tail`` is the
    only place the guard reason is written (#111910).

    Held-back work that no per-card refusal explains is named too: the host-wide
    ``host_cap_deferred`` and the board's own ``board_cap_deferred`` (its
    ``kanban.max_spawn_by_board`` ceiling, or the global ``kanban.max_spawn``).
    Both would otherwise read as an idle queue for the same reason the
    respawn-guard reasons above would.

    Naming every hold matters more than brevity: a line reading ``active_pr=3``
    while 400 ready rows sit in ``skipped_per_profile_capped`` — or 12 in
    ``skipped_nonspawnable`` — is not a report. It is how a starved board and a
    correctly idle one came to read the same (2026-09-26).
    """
    counts: dict[str, int] = {}
    held_lanes: dict[str, int] = {}
    pressure: Optional[str] = None
    for res in results:
        if res is None:
            continue
        for _task_id, reason in res.respawn_guarded:
            counts[reason] = counts.get(reason, 0) + 1
        if res.rate_limited:
            counts["rate_limited"] = counts.get("rate_limited", 0) + len(res.rate_limited)
        if res.skipped_locked:
            counts["skipped_locked"] = counts.get("skipped_locked", 0) + 1
        if res.skipped_board_disabled:
            # Board-level refusal: the board is an estate/scratch board and the
            # tick touched nothing (card t_17c9c847). Without this the refusal is
            # invisible to the "dispatcher stuck" line.
            counts["board_not_dispatch_enabled"] = counts.get("board_not_dispatch_enabled", 0) + 1
        if res.deferred_host_capped:
            # The HOST budget held these back: no per-card refusal of their own,
            # so without this the tick reads like an idle queue.
            counts["host_cap_deferred"] = counts.get("host_cap_deferred", 0) + len(res.deferred_host_capped)
        if res.deferred_board_capped:
            # The board's OWN ceiling held these back: like the host cap, no
            # per-card refusal exists, so a capped board would otherwise read
            # exactly like an idle one.
            counts["board_cap_deferred"] = counts.get("board_cap_deferred", 0) + len(res.deferred_board_capped)
        for bucket, bucket_rows in (
            ("skipped_per_profile_capped", res.skipped_per_profile_capped),
            ("skipped_nonspawnable", res.skipped_nonspawnable),
            ("skipped_unassigned", res.skipped_unassigned),
        ):
            if bucket_rows:
                counts[bucket] = counts.get(bucket, 0) + len(bucket_rows)
        for _task_id, who in res.skipped_lockdown:
            lane = str(who or "").strip() or "(unassigned)"
            held_lanes[lane] = held_lanes.get(lane, 0) + 1
        if res.memory_pressure:
            pressure = res.memory_pressure
    parts = [f"{k}={v}" for k, v in sorted(counts.items())]
    if held_lanes:
        # The lane-scoped DEFCON gate is a POLICY hold, not a fault: without this the
        # "dispatcher stuck" line for a fully-held tick reads like an idle queue and
        # sends the reader to profile health instead of the stop (v3 §6.4a). The
        # per-card record is the ``skipped_lockdown`` task event.
        named = ", ".join(f"{lane} x{n}" for lane, n in sorted(held_lanes.items()))
        parts.append(f"lockdown={sum(held_lanes.values())} ({named})")
    if pressure:
        parts.append(f"memory_pressure={pressure}")
    return ", ".join(parts)


# Bounded registry of recently-reaped worker exits, filled by the reap loop in
# ``dispatch_once`` and read by ``detect_crashed_workers`` to classify a dead-pid
# task. Entry: ``pid -> (raw_wait_status, reaped_at_epoch)``; raw status kept so
# both WIFEXITED/WEXITSTATUS and WIFSIGNALED can be consulted. Trimmed by age
# plus a total size cap. Process-local by nature (``waitpid`` only reaps our own
# children): a per-tick ``hermes kanban dispatch`` process finds it empty, so
# ``_classify_dead_worker_exit`` falls back to the exit trailer the worker
# leaves in its own log (``KANBAN_WORKER_EXIT_TRAILER``).
_RECENT_WORKER_EXIT_TTL_SECONDS = 600
_RECENT_WORKER_EXITS_MAX = 4096
_recent_worker_exits: "dict[int, tuple[int, float]]" = {}

# Windows has no ``waitpid(-1)``: a child's exit code is only recoverable
# through a live handle, so ``_default_spawn`` parks each worker's ``Popen``
# here (Windows only) and ``reap_worker_zombies`` polls it. Entry: ``pid -> Popen``.
_live_worker_procs: "dict[int, subprocess.Popen]" = {}


def _wait_status_from_returncode(returncode: int) -> int:
    """Encode a ``Popen.returncode`` in the wait-status layout the registry stores."""
    return (int(returncode) & 0xFF) << 8


def _record_worker_exit(pid: int, raw_status: int) -> None:
    """Record a reaped child's exit status; duplicate pids overwrite (latest wins)."""
    if not pid or pid <= 0:
        return
    now = time.time()
    _recent_worker_exits[int(pid)] = (int(raw_status), now)
    if len(_recent_worker_exits) > _RECENT_WORKER_EXITS_MAX // 2:
        cutoff = now - _RECENT_WORKER_EXIT_TTL_SECONDS
        for _pid in [p for p, (_s, t) in _recent_worker_exits.items() if t < cutoff]:
            _recent_worker_exits.pop(_pid, None)
    if len(_recent_worker_exits) > _RECENT_WORKER_EXITS_MAX:
        # Drop oldest half.
        ordered = sorted(_recent_worker_exits.items(), key=lambda kv: kv[1][1])
        for _pid, _ in ordered[: len(ordered) // 2]:
            _recent_worker_exits.pop(_pid, None)


def _classify_worker_exit(pid: int) -> "tuple[str, Optional[int]]":
    """``(kind, code)`` for a reaped worker PID: ``clean_exit`` (rc 0 while
    still ``running`` = protocol violation), ``rate_limited``
    (``KANBAN_RATE_LIMIT_EXIT_CODE``, never counts as a failure),
    ``nonzero_exit``, ``signaled`` (``code`` is the signal), ``unknown`` (pid
    not in the reap registry; ``code`` None)."""
    entry = _recent_worker_exits.get(int(pid))
    if entry is None:
        return ("unknown", None)
    raw, _ = entry
    # Bit-level POSIX wait-status decode instead of os.WIFEXITED/WEXITSTATUS/
    # WIFSIGNALED/WTERMSIG: those helpers do not exist on Windows, where the
    # registry is fed by reap_worker_zombies' Popen poll. Low 7 bits = signal
    # (0 = normal exit, 0x7F = stopped), bits 8-15 = exit code.
    raw = int(raw)
    signal_number = raw & 0x7F
    if signal_number == 0:
        return _exit_code_kind((raw >> 8) & 0xFF)
    if signal_number != 0x7F:
        return ("signaled", signal_number)
    return ("unknown", None)


def _exit_code_kind(code: int) -> "tuple[str, int]":
    """``(kind, code)`` for a worker's exit code, however it was observed."""
    if code == 0:
        return ("clean_exit", 0)
    if code == _kb.KANBAN_RATE_LIMIT_EXIT_CODE:
        return ("rate_limited", code)
    if code == _kb.KANBAN_TERMINAL_PROVIDER_EXIT_CODE:
        return ("terminal_provider", code)
    return ("nonzero_exit", code)


_EXIT_TRAILER_RE = re.compile(
    r"^" + re.escape(KANBAN_WORKER_EXIT_TRAILER) + r"(\d+)\s*$", re.MULTILINE,
)


def _worker_log_exit_code(task_id: str, board: Optional[str] = None) -> Optional[int]:
    """Exit code from the trailer the worker CLI wrote to its own log; None when absent.

    The durable twin of ``_recent_worker_exits``: written by the worker itself
    (``hermes_cli.quiet_single_query.exit_single_query``), so it is there whether
    or not the process running this sweep ever reaped the worker. Last trailer
    wins — the log is append-mode across re-runs.
    """
    try:
        raw = _kb.read_worker_log(task_id, tail_bytes=4000, board=board)
    except Exception:
        return None
    matches = _EXIT_TRAILER_RE.findall(raw or "")
    return int(matches[-1]) if matches else None


def reap_worker_zombies() -> "list[int]":
    """Reap exited workers without blocking; returns reaped PIDs. POSIX reaps
    every child via ``waitpid(-1)``; Windows polls the ``Popen`` handles
    parked by ``_default_spawn`` (the only way to learn a child's exit code
    there), so the rate-limit sentinel exit is classified on both hosts."""
    reaped: "list[int]" = []
    if _kb._IS_WINDOWS:
        for pid, proc in list(_live_worker_procs.items()):
            returncode = proc.poll()
            if returncode is None:
                continue
            _record_worker_exit(pid, _wait_status_from_returncode(returncode))
            _live_worker_procs.pop(pid, None)
            reaped.append(pid)
        return reaped
    try:
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid == 0:
                break
            _record_worker_exit(pid, status)
            reaped.append(pid)
    except Exception:
        pass
    return reaped


# ---------------------------------------------------------------------------
# Liveness: one witness, three answers
# ---------------------------------------------------------------------------
# A liveness read has to be able to answer "I cannot tell" (#123811). Reading a FAILED probe as death
# is what let a single dispatch tick declare six live workers dead — each recovery then spawned a
# duplicate beside the worker it had just abandoned. Every verdict here is one of the three below, and
# only ``WORKER_DEAD`` may release a claim or authorize a signal. ``WORKER_UNKNOWN`` holds the claim.
WORKER_ALIVE = "alive"
WORKER_UNKNOWN = "unknown"
WORKER_DEAD = "dead"

# A recorded fingerprint's start is expressed relative to a boot reference; two starts compare only
# while that reference holds. macOS moves ``kern.boottime`` with the system clock, shifting every start
# taken before the move by exactly the move — measured here as 800 cs (#124570). This tolerance covers
# only the centisecond rounding of two readers sharing one reference; a move of real size is far
# larger, and a move is not a difference (see ``boot_reference_held``).
BOOT_REFERENCE_TOLERANCE_CS = 2


def _exists_probe(pid: int) -> bool:
    """Is the PID number occupied (zombies included)?

    Goes through the ``kanban_db`` facade when it has been replaced — plugins and tests patch
    ``kanban_db._pid_alive`` to drive liveness, and the pre-existing code honoured that by calling
    ``_kb._pid_alive``. Doing that unconditionally would recurse here, because by default the facade IS
    this module's ``_pid_alive``; so only an OVERRIDE is consulted, and otherwise the probe is made
    directly.
    """
    facade = getattr(_kb, "_pid_alive", None)
    if facade is not None and facade is not _pid_alive:
        return bool(facade(int(pid)))
    from gateway.status import _pid_exists
    return bool(_pid_exists(int(pid)))


def _pid_liveness(pid: Optional[int]) -> str:
    """``alive`` / ``dead`` / ``unknown`` for a bare PID (no identity component).

    ``dead`` is reserved for proof: the OS says the number is unoccupied (``ESRCH``/``OpenProcess``
    failure) or the process is a zombie. A secondary probe that FAILED is not proof — macOS ``ps``
    exits non-zero for PIDs it cannot see, under load as well as after exit — so it answers
    ``unknown`` and leaves the caller's own evidence (a matching fingerprint) in charge. The probe
    timing out or raising keeps the ``kill(0)`` answer, which is the pre-existing behaviour.
    """
    if not pid or pid <= 0:
        return WORKER_DEAD
    if not _exists_probe(int(pid)):
        return WORKER_DEAD
    if sys.platform == "linux":
        try:
            with open(f"/proc/{int(pid)}/status", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("State:"):
                        # "State:\tZ (zombie)" → dead
                        if "Z" in line.split(":", 1)[1]:
                            return WORKER_DEAD
                        break
        except (FileNotFoundError, PermissionError, OSError):
            # proc entry gone → already reaped; treat as dead.
            pass
    elif sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(int(pid))],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', errors='replace',
                timeout=1,
                check=False,
            )
            if proc.returncode != 0:
                # A failed probe proves nothing: the number IS occupied (``_pid_exists`` said so
                # above), and "could not read the state" is not "the process is gone".
                return WORKER_UNKNOWN
            if "Z" in (proc.stdout or "").strip():
                return WORKER_DEAD
        except (OSError, subprocess.SubprocessError, TimeoutError):
            # If the secondary probe fails, keep the kill(0) answer.
            pass
    return WORKER_ALIVE


def _pid_alive(pid: Optional[int]) -> bool:
    """True only when ``pid`` is PROVEN to be a running process (verdict ``alive``).

    Callers asking "is this number still occupied?" must not use the negation: ``unknown`` reads as
    False here, so ``not _pid_alive(...)`` would turn an unreadable probe into proof of death.
    Compare the verdict instead — ``_pid_liveness(pid) == WORKER_DEAD``.
    """
    return _pid_liveness(pid) == WORKER_ALIVE


# ``worker_started_at`` value for a spawn whose fingerprint could not be captured. Distinct from the
# NULL legacy row (pre-fingerprint spawn): such a worker is held (its claim is never released beside
# the live PID) but NEVER signalled — missing process identity is refusal, not permission (#99558).
UNVERIFIED_WORKER_FINGERPRINT = "unverified"


def _process_fingerprint(pid: int) -> Optional[str]:
    """Restart-stable identity of a live process: ``"<boot witness>|<boot reference>|<start>"``.

    The witness names the boot itself (``gateway.status.host_boot_witness``); the start is centiseconds
    since boot and the reference is the value that start is expressed against
    (``gateway.status.get_process_uptime_fingerprint``). A reboot breaks the pair, and — unlike the
    absolute form — so does nothing else while the reference holds. The reference is recorded because
    it does NOT always hold: macOS moves ``kern.boottime`` with the system clock, which shifts every
    start taken before the move by exactly the move, and a comparator that cannot see the move reads
    that shift as a recycled PID (#124570). A platform whose start is already boot-relative records
    ``0``. ``None`` when a half is unreadable — including "the platform has no boot witness", whose only
    honest reading is inability to certify identity, never a difference.
    """
    from gateway.status import get_process_uptime_fingerprint, host_boot_witness
    witness = host_boot_witness()
    if not witness:
        return None
    fingerprint = get_process_uptime_fingerprint(int(pid))
    if fingerprint is None:
        return None
    reference, start = fingerprint
    return f"{witness}|{reference}|{start}"


def _fingerprint_parts(fingerprint: Any) -> Optional[tuple[str, int, Optional[int]]]:
    """``(witness, uptime-relative start, boot reference)`` for a fingerprint, else ``None``.

    ``None`` covers every shape that cannot be compared at all — a bare start tick (rows written before
    the witness existed) and a ``"|<start>"`` pair whose witness component is EMPTY (macOS rows written
    while ``current_instantiation_epoch`` had no answer there).

    A ``None`` REFERENCE (the two-field shape rows carry from before #124570) is different: it compares
    by exact match but never certifies a mismatch, because the reference it was built against is not in
    it — a mismatch there carries the same information as a moved reference, so a caller must read
    "unknown", never "recycled".
    """
    if not isinstance(fingerprint, str) or "|" not in fingerprint:
        return None
    witness, _, rest = fingerprint.partition("|")
    if not witness:
        return None
    fields = rest.split("|")
    try:
        if len(fields) == 1:
            return witness, int(fields[0]), None
        if len(fields) == 2:
            return witness, int(fields[1]), int(fields[0])
    except (TypeError, ValueError):
        return None
    return None


def _witnesses_agree(recorded: str, current: str) -> Optional[bool]:
    """True/False when two boot witnesses are comparable, ``None`` when they are not.

    Opaque witnesses (a boot id, a boot session UUID) must be EQUAL: they only ever change on a reboot.
    The boot-epoch fallback is the one form a clock step can move, so it is compared within
    ``START_TIME_DRIFT_TOLERANCE`` — the same tolerance the start component gets — instead of exactly.
    """
    from gateway.status import START_TIME_DRIFT_TOLERANCE
    if recorded == current:
        return True
    prefix = "boottime:"
    recorded_is_time = recorded.startswith(prefix)
    current_is_time = current.startswith(prefix)
    if recorded_is_time and current_is_time:
        try:
            gap = abs(int(recorded[len(prefix):]) - int(current[len(prefix):]))
        except (TypeError, ValueError):
            return None
        return gap <= START_TIME_DRIFT_TOLERANCE
    if recorded_is_time != current_is_time:
        # Mixed forms: one probe failed to yield its session id, so the witness changed SHAPE, not the
        # boot. "Cannot tell", never "different boot".
        return None
    return False


def _worker_liveness(pid: Optional[int], started_at) -> str:
    """The liveness verdict for a recorded worker: ``alive`` / ``dead`` / ``unknown``.

    ``dead`` needs proof of death (the PID is gone or a zombie) or proof of non-identity (the recorded
    fingerprint and the live reading disagree while the boot reference they are both expressed against
    still holds). Everything else is ``unknown``, and ``unknown`` never releases a claim: a reclaim that
    cannot prove the worker is gone must hold the claim and retry, because releasing it spawns a
    duplicate beside the process it abandoned. Fingerprints in an older shape — no witness, or no
    recorded reference to compare against — are ``unknown`` on a mismatch for the same reason:
    indistinguishable from a reference adjustment. Legacy NULL rows (no fingerprint at all) and
    deliberately UNVERIFIED spawns keep the existence answer they always had.

    A mismatch is only evidence while the reference holds. Measured 2026-09-27: macOS moved
    ``kern.boottime`` by 8.00 s, which put two live workers exactly 800 cs out of their own recorded
    start times (tolerance is 200 cs), and the sweep reclaimed both while they were running —
    ``dead`` there was a false verdict on a single reading, which is the defect this refuses
    (#124570).
    """
    if not pid or int(pid) <= 0:
        return WORKER_DEAD
    pid_state = _pid_liveness(int(pid))
    if pid_state == WORKER_DEAD:
        return WORKER_DEAD
    if started_at is None or started_at == UNVERIFIED_WORKER_FINGERPRINT:
        # Unchanged semantics today: these rows never had a comparable identity, so the
        # answer is the bare existence probe's, unprovable reading included.
        return WORKER_ALIVE if pid_state == WORKER_ALIVE else WORKER_DEAD
    parts = _fingerprint_parts(started_at)
    if parts is None:
        return WORKER_UNKNOWN
    witness, recorded_start, recorded_reference = parts
    from gateway.status import (
        get_process_uptime_fingerprint,
        host_boot_witness,
        start_time_fingerprints_match,
    )
    current_witness = host_boot_witness()
    if not current_witness:
        return WORKER_UNKNOWN
    same_boot = _witnesses_agree(witness, current_witness)
    if same_boot is None:
        return WORKER_UNKNOWN
    if not same_boot:
        # A different boot: the PID and the boot-relative tick can both recur.
        return WORKER_DEAD
    current = get_process_uptime_fingerprint(int(pid))
    if current is None:
        return WORKER_UNKNOWN
    current_reference, current_start = current
    if recorded_start <= 0 or current_start <= 0:
        return WORKER_UNKNOWN
    if start_time_fingerprints_match(recorded_start, current_start):
        return WORKER_ALIVE
    if not boot_reference_held(recorded_reference, current_reference):
        # The reference both readings are expressed against MOVED between the recording and this read,
        # so the start below is not the same measurement: two live workers were declared dead this way
        # (see the docstring). A comparison across a moved reference proves neither identity nor its
        # absence — hold, and let the TTL/backstop paths bound the wait.
        return WORKER_UNKNOWN
    return WORKER_DEAD


def boot_reference_held(
    recorded_reference: Optional[int], current_reference: Optional[int]
) -> bool:
    """True when both start readings were built against the same boot reference and may be compared.

    ``None`` (a fingerprint written before the reference was recorded) is never comparable: a mismatch
    there carries the same information as a moved reference, so the caller must read "hold", never
    "recycled". A small tolerance absorbs the centisecond rounding of the two readers sharing one
    ``kern.boottime``.
    """
    if recorded_reference is None or current_reference is None:
        return False
    return abs(int(recorded_reference) - int(current_reference)) <= BOOT_REFERENCE_TOLERANCE_CS


def _worker_alive(pid: Optional[int], started_at) -> bool:
    """True when ``pid`` is live AND is still the worker we spawned (verdict ``alive``).

    ``started_at`` is the fingerprint recorded by ``_set_worker_pid``; after a reboot (or any PID
    recycle) an unrelated process can own the number, so bare existence is never enough to extend a
    claim or to signal. For CLAIM PROTECTION use :func:`_worker_not_dead` — this one answers False for
    ``unknown``, which must never be read as "release it".
    """
    return _worker_liveness(pid, started_at) == WORKER_ALIVE


def _worker_not_dead(pid: Optional[int], started_at) -> bool:
    """Claim protection: keep the claim unless the worker is PROVEN dead (``alive`` or ``unknown``).

    This is the predicate every reclaim guard uses. Holding a claim beside a worker that may still be
    running costs a delayed retry; releasing it spawns a second worker on the same card, which is the
    duplication loop the reclaim exists to prevent.
    """
    return _worker_liveness(pid, started_at) != WORKER_DEAD


def _pid_recycled(pid: Optional[int], started_at) -> bool:
    """True when a live ``pid`` is PROVEN not to be the process fingerprinted at spawn.

    Signalling it would hit a stranger, and the reclaim may proceed: the worker we cared about is
    gone. An unprovable identity is NOT recycled (``unknown`` answers False here) — callers must
    consult the verdict, not this boolean, before releasing a claim.
    """
    return _worker_liveness(pid, started_at) == WORKER_DEAD


def _kill_fn(signal_fn) -> Optional[Callable[[int, int], None]]:
    """``signal_fn`` test hook, else ``os.kill`` when the platform has one."""
    if signal_fn is not None:
        return signal_fn
    return os.kill if hasattr(os, "kill") else None


def _poll_worker_exit(pid: int, started_at: Optional[int] = None) -> bool:
    """Poll ~5 s (10 x 0.5 s) for ``pid`` to die; True once it is PROVEN gone.

    An ``unknown`` read keeps polling instead of answering "exited": the caller uses True to conclude
    the worker is gone, and an unreadable probe is not that conclusion.
    """
    for _ in range(10):
        if _worker_liveness(pid, started_at) == WORKER_DEAD:
            return True
        time.sleep(0.5)
    return False


def _sigkill(kill, pid: int) -> bool:
    """Best-effort SIGKILL; True when the signal was delivered."""
    try:
        # signal.SIGKILL doesn't exist on Windows; SIGTERM maps to TerminateProcess.
        kill(int(pid), getattr(signal, "SIGKILL", signal.SIGTERM))
        return True
    except (ProcessLookupError, OSError):
        return False


def _terminate_reclaimed_worker(
    pid: Optional[int],
    claim_lock: Optional[str],
    *,
    signal_fn=None,
    started_at=None,
) -> dict[str, Any]:
    """Best-effort host-local worker termination for reclaim paths. ``started_at`` is the spawn-time
    fingerprint: when the live process no longer matches it, the PID was recycled and nothing is
    signalled — the worker is gone, which is what the reclaim wanted (``terminated`` = True). An
    UNVERIFIED spawn (fingerprint capture failed) that is still live is never signalled either, but
    it is reported as surviving (``signal_refused``) so the reclaim holds the claim instead of
    spawning a duplicate beside it."""
    info: dict[str, Any] = {
        "prev_pid": int(pid) if pid else None,
        "host_local": False,
        "termination_attempted": False,
        "terminated": False,
        "sigkill": False,
    }
    if not pid or pid <= 0 or not claim_lock:
        return info
    if not str(claim_lock).startswith(_kb._host_prefix()):
        return info
    info["host_local"] = True

    kill = _kill_fn(signal_fn)
    if kill is None:
        return info
    if started_at == UNVERIFIED_WORKER_FINGERPRINT:
        # Never signal by bare number: a dead PID is "gone" (reclaim proceeds), a live one is held.
        info["signal_refused"] = True
        info["terminated"] = _pid_liveness(pid) == WORKER_DEAD
        return info
    verdict = _worker_liveness(pid, started_at)
    if verdict == WORKER_UNKNOWN:
        # The recorded identity cannot be compared with the live process (see ``_worker_liveness``).
        # Signalling the bare number could hit whatever now owns it, and reporting ``terminated`` would
        # release the claim beside a worker that may still be running. Hold both; retry next tick.
        info["signal_refused"] = True
        info["liveness_unknown"] = True
        info["terminated"] = False
        return info

    if verdict == WORKER_DEAD and _pid_liveness(pid) == WORKER_ALIVE:
        # Occupied by a stranger (the PID was recycled): never signal it. Our worker is gone.
        info["terminated"] = True
        info["pid_recycled"] = True
        return info

    info["termination_attempted"] = True
    try:
        kill(int(pid), signal.SIGTERM)
    except ProcessLookupError:
        # Already gone = successful termination. Leaving terminated=False would
        # make the reclaim guard misread a dead worker as alive and defer forever.
        info["terminated"] = True
        return info
    except OSError:
        return info

    if _poll_worker_exit(pid, started_at):
        info["terminated"] = True
        return info
    if _worker_liveness(pid, started_at) == WORKER_ALIVE:
        if not _sigkill(kill, pid):
            return info
        info["sigkill"] = True
    info["terminated"] = _worker_liveness(pid, started_at) == WORKER_DEAD
    return info


def reap_terminal_workers(conn: sqlite3.Connection, *, signal_fn=None) -> list[str]:
    """End host-local workers that outlived their run (issue #111791) — a worker
    that called ``kanban_complete`` and then hung keeps its ``state.db`` sidecar
    fds open and no ``running``-only sweep can see it once ``tasks.worker_pid`` is
    cleared. Keys on the closed ``task_runs`` row's retained pid + spawn
    fingerprint: a legacy row (NULL fingerprint) or a recycled PID is never
    signalled; a pid that is simply gone just has its evidence cleared. A run
    that ended less than ``TERMINAL_WORKER_REAP_GRACE_SECONDS`` ago is left
    alone so a worker still finalising after its own transition is not killed.
    One row's failure (signal, /proc probe) is logged and skips only that row.
    Returns the task ids whose worker was terminated."""
    rows = conn.execute(
        "SELECT id, task_id, worker_pid, worker_started_at, claim_lock FROM task_runs "
        "WHERE ended_at IS NOT NULL AND ended_at <= ? "
        "AND worker_pid IS NOT NULL AND worker_started_at IS NOT NULL",
        (int(time.time()) - TERMINAL_WORKER_REAP_GRACE_SECONDS,),
    ).fetchall()
    host_prefix = _kb._host_prefix()
    reaped: list[str] = []
    for row in rows:
        try:
            _reap_terminal_worker_row(conn, row, host_prefix, signal_fn, reaped)
        except Exception:
            _kb._log.debug(
                "kanban dispatch: terminal worker reap failed for run %s (task %s)",
                row["id"], row["task_id"], exc_info=True,
            )
    return reaped


def _reap_terminal_worker_row(conn, row, host_prefix: str, signal_fn, reaped: list[str]) -> None:
    pid, fingerprint = int(row["worker_pid"]), row["worker_started_at"]
    if pid == os.getpid() or not str(row["claim_lock"] or "").startswith(host_prefix):
        return
    if fingerprint == UNVERIFIED_WORKER_FINGERPRINT and _kb._pid_alive(pid):
        return  # unproven identity: never signalled; its evidence is cleared once the pid is gone
    verdict = _worker_liveness(pid, fingerprint)
    if verdict == WORKER_UNKNOWN:
        # Cannot certify identity. Clearing this row's pid would destroy the only handle on a worker
        # that may still be running, so leave the evidence for the next tick instead.
        return
    alive = verdict == WORKER_ALIVE
    termination = None
    if alive:
        termination = _terminate_reclaimed_worker(
            pid, row["claim_lock"], signal_fn=signal_fn, started_at=fingerprint)
        if not termination["terminated"]:
            return  # still alive: try again next tick
    with _kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET worker_pid = NULL, worker_started_at = NULL "
            "WHERE id = ? AND worker_pid = ? AND worker_started_at = ?",
            (row["id"], pid, fingerprint),
        )
        if alive:
            _kb._append_event(
                conn, row["task_id"], "terminal_worker_reaped",
                {"pid": pid, "worker_started_at": fingerprint, **termination}, run_id=row["id"],
            )
    if alive:
        reaped.append(row["task_id"])


def _worker_survived_termination(termination: dict) -> bool:
    """True when we tried to kill our own host-local worker and it is still alive.

    Reclaiming then would release the claim and spawn a second worker while the
    first still runs — the duplication loop. Only host-local workers we actually
    signalled count; a non-local lock or no-op attempt (no ``os.kill``) must fall
    through to the normal release path since we cannot manage that worker anyway.
    """
    return bool(
        termination.get("host_local")
        and (termination.get("termination_attempted") or termination.get("signal_refused"))
        and not termination.get("terminated")
    )


def _defer_reclaim_for_live_worker(
    conn: sqlite3.Connection,
    task_id: str,
    claim_lock: Optional[str],
    now: int,
    termination: dict,
    *,
    reason: str,
) -> None:
    """Hold a claim whose worker survived termination instead of releasing it.

    Extends ``claim_expires`` by ``RECLAIM_DEFER_GRACE_SECONDS`` so the task
    stays ``running`` (no duplicate spawn) and records ``reclaim_deferred``.
    The next tick retries the kill; not spawning a duplicate is what lets the
    throttled worker finally die.
    """
    grace = now + _kb.RECLAIM_DEFER_GRACE_SECONDS
    with _kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET claim_expires = ? "
            "WHERE id = ? AND status = 'running' AND claim_lock IS ?",
            (grace, task_id, claim_lock),
        )
        if cur.rowcount != 1:
            return
        run_id = _kb._current_run_id(conn, task_id)
        if run_id is not None:
            conn.execute("UPDATE task_runs SET claim_expires = ? WHERE id = ?", (grace, run_id))
        payload = {"reason": reason, "claim_lock": claim_lock, "claim_expires_now": grace}
        payload.update(termination)
        _kb._append_event(conn, task_id, "reclaim_deferred", payload, run_id=run_id)


def heartbeat_worker(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    note: Optional[str] = None,
    expected_run_id: Optional[int] = None,
) -> bool:
    """Record a ``heartbeat`` event + touch ``last_heartbeat_at``.

    Liveness signal orthogonal to the PID check: a worker whose forked child
    (train loop, crawl) is stuck can still have a live Python process.
    Returns False if the task is not running or its claim expired.
    """
    now = int(time.time())
    with _kb.write_txn(conn):
        sql = "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ? AND status = 'running'"
        params: tuple = (now, task_id)
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params += (int(expected_run_id),)
        cur = conn.execute(sql, params)
        if cur.rowcount != 1:
            return False
        run_id = (
            int(expected_run_id)
            if expected_run_id is not None
            else _kb._current_run_id(conn, task_id)
        )
        if run_id is not None:
            conn.execute("UPDATE task_runs SET last_heartbeat_at = ? WHERE id = ?", (now, run_id))
        _kb._append_event(
            conn, task_id, "heartbeat",
            {"note": note} if note else None,
            run_id=run_id,
        )
    return True


def enforce_max_runtime(conn: sqlite3.Connection, *, signal_fn=None) -> list[str]:
    """Terminate workers whose per-task ``max_runtime_seconds`` has elapsed.

    SIGTERM, short grace, then SIGKILL. Emits ``timed_out`` and restores the
    task's source phase so the next tick re-spawns the same kind of worker —
    unless the circuit breaker already gave up, leaving it blocked. Host-local
    only (same reasoning as ``detect_crashed_workers``). ``signal_fn`` is a test hook.

    The population is gated on a POSITIVE cap. The column's ``0`` is the author's
    documented "no cap" opt-out (``hermes_cli/kanban.py::_parse_duration`` normalises
    the CLI's ``0`` to unset for exactly this reason, and the stamp in
    ``_stamp_default_max_runtime_seconds`` treats a stored ``0`` as the card's own
    value), so it is NOT a budget: with ``limit == 0`` the ``elapsed < limit`` guard
    below is false for ANY elapsed and a ``0``-cap card is reaped on the very next
    tick with ``limit_seconds 0``. Gating the SELECT on ``> 0`` repairs rows already
    stored as ``0`` rather than only future writes.
    """
    timed_out: list[str] = []
    now = int(time.time())
    host_prefix = _kb._host_prefix()

    rows = conn.execute(
        "SELECT t.id, t.worker_pid, t.worker_started_at, "
        "       COALESCE(r.started_at, t.started_at) AS active_started_at, "
        "       t.max_runtime_seconds, t.claim_lock "
        "FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running' AND t.max_runtime_seconds IS NOT NULL "
        "  AND t.max_runtime_seconds > 0 "
        "  AND COALESCE(r.started_at, t.started_at) IS NOT NULL "
        "  AND t.worker_pid IS NOT NULL"
    ).fetchall()
    for row in rows:
        lock = row["claim_lock"] or ""
        if not lock.startswith(host_prefix):
            continue
        # Runtime is per attempt: ``tasks.started_at`` records the FIRST start,
        # so retries must be measured from the active task_runs row.
        elapsed = now - int(row["active_started_at"])
        limit = int(row["max_runtime_seconds"])
        if elapsed < limit:
            continue

        pid = int(row["worker_pid"])
        tid = row["id"]
        started_at = _kb._row_get(row, "worker_started_at")
        verdict = _worker_liveness(pid, started_at)
        if verdict == WORKER_UNKNOWN:
            # Cannot certify identity: the number may belong to a stranger (no signal) and the worker
            # may still be running (no release beside it). Hold the claim; the next tick re-reads.
            _kb._log.warning("kanban: task %s worker pid %s exceeded max runtime but its process "
                             "identity cannot be read; held, not signalled", tid, pid)
            continue
        if started_at == UNVERIFIED_WORKER_FINGERPRINT and verdict != WORKER_DEAD:
            # Fingerprint capture failed at spawn: we cannot prove this live PID is our worker, so
            # it is neither signalled nor released beside (duplicate). It is reclaimed once it exits.
            _kb._log.warning("kanban: task %s worker pid %s exceeded max runtime but has no verified "
                             "identity; not signalled", tid, pid)
            continue
        # SIGTERM then SIGKILL after 5 s grace; workers wanting a cleaner
        # shutdown install their own SIGTERM handler. A recycled PID (fingerprint
        # mismatch) is never signalled: the worker is already gone.
        killed = False
        kill = _kill_fn(signal_fn)
        stranger = verdict == WORKER_DEAD and _pid_liveness(pid) == WORKER_ALIVE
        if kill is not None and not stranger:
            with contextlib.suppress(ProcessLookupError, OSError):
                kill(pid, signal.SIGTERM)
            # Short polling wait — no time.sleep on the write txn.
            _poll_worker_exit(pid, started_at)
            if _worker_liveness(pid, started_at) == WORKER_ALIVE:
                killed = _sigkill(kill, pid)

        error = f"elapsed {int(elapsed)}s > limit {limit}s"
        with _kb.write_txn(conn):
            retry_status = _kb._retry_status_for_run(conn, tid)
            cur = conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
                "last_heartbeat_at = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND worker_pid = ? AND claim_lock IS ?",
                (retry_status, tid, pid, row["claim_lock"]),
            )
            if cur.rowcount == 1:
                payload = {
                    "pid": pid,
                    "elapsed_seconds": int(elapsed),
                    "limit_seconds": limit,
                    "sigkill": killed,
                    "retry_status": retry_status,
                }
                run_id = _kb._end_run(
                    conn, tid, outcome="timed_out", status="timed_out",
                    error=error, metadata=payload,
                )
                _kb._append_event(conn, tid, "timed_out", payload, run_id=run_id)
                timed_out.append(tid)
        # Outside the write_txn above because ``_record_task_failure`` opens its
        # own. If the breaker trips this flips the task to ``blocked`` and emits
        # ``gave_up`` on top of the ``timed_out`` already emitted.
        if cur.rowcount == 1:
            _record_task_failure(
                conn, tid,
                error=error,
                outcome="timed_out",
                release_claim=False,
                end_run=False,
                event_payload_extra={"pid": pid, "sigkill": killed, "retry_status": retry_status},
            )
    return timed_out


# A running task with no heartbeat for this long is inactive regardless of
# ``dispatch_stale_timeout_seconds`` (spec: ">4h started + no commits in 1h").
_STALE_HEARTBEAT_GAP_SECONDS = 3600


def detect_stale_running(
    conn: sqlite3.Connection,
    *,
    stale_timeout_seconds: int = 0,
    signal_fn=None,
) -> list[str]:
    """Reclaim ``running`` tasks with no heartbeat progress; returns their ids.

    Stale = running longer than ``stale_timeout_seconds`` (active run's
    ``started_at``, else ``tasks.started_at``) AND ``last_heartbeat_at`` NULL or
    older than ``_STALE_HEARTBEAT_GAP_SECONDS``. Task returns to its source
    phase, run closes ``outcome='stale'``, a live host-local worker is killed.
    ``0`` disables the check; ``signal_fn`` is a test hook. Deliberately NOT
    counted via ``_record_task_failure``: an absent heartbeat is not a worker
    failure, and counting it would let long-running tasks trip the breaker.
    """
    if stale_timeout_seconds <= 0:
        return []

    now = int(time.time())
    reclaimed: list[str] = []

    rows = conn.execute(
        "SELECT t.id, t.worker_pid, t.worker_started_at, t.last_heartbeat_at, t.claim_lock, "
        "       COALESCE(r.started_at, t.started_at) AS active_started_at "
        "FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running'"
    ).fetchall()

    for row in rows:
        if row["active_started_at"] is None:
            continue
        elapsed = now - int(row["active_started_at"])
        if elapsed < stale_timeout_seconds:
            continue

        last_hb = row["last_heartbeat_at"]
        hb_age = (now - int(last_hb)) if last_hb is not None else None
        if hb_age is not None and hb_age < _STALE_HEARTBEAT_GAP_SECONDS:
            continue

        pid = row["worker_pid"]
        tid = row["id"]
        lock = row["claim_lock"] or ""

        termination = _kb._terminate_reclaimed_worker(
            pid, lock, signal_fn=signal_fn, started_at=_kb._row_get(row, "worker_started_at"))

        # Never release a claim while our own worker is still alive: that would
        # spawn a duplicate beside it. Hold the claim and retry next tick.
        if _worker_survived_termination(termination):
            _defer_reclaim_for_live_worker(
                conn, tid, lock, now, termination,
                reason="heartbeat_stale_worker_alive",
            )
            continue

        with _kb.write_txn(conn):
            retry_status = _kb._retry_status_for_run(conn, tid)
            cur = conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
                "last_heartbeat_at = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND claim_lock IS ?",
                (retry_status, tid, row["claim_lock"]),
            )
            if cur.rowcount != 1:
                continue

            payload = {
                "elapsed_seconds": int(elapsed),
                "last_heartbeat_at": _kb._opt_int(last_hb),
                "heartbeat_age_seconds": _kb._opt_int(hb_age),
                "timeout_seconds": stale_timeout_seconds,
                "pid": int(pid) if pid else None,
                "retry_status": retry_status,
            }
            payload.update(termination)

            run_id = _kb._end_run(
                conn, tid,
                outcome="stale", status="stale",
                error=(
                    f"no heartbeat for {int(hb_age)}s "
                    if hb_age is not None
                    else "no heartbeat ever"
                ) + f" after {int(elapsed)}s running",
                metadata=payload,
            )
            _kb._append_event(conn, tid, "stale", payload, run_id=run_id)
            reclaimed.append(tid)

    return reclaimed


def reconcile_orphaned_running(conn: sqlite3.Connection) -> list[str]:
    """Requeue ``running`` cards with broken claim bookkeeping; returns their ids.

    A task ``running`` with NULL ``claim_lock``/``claim_expires`` (crash
    mid-claim, manual SQL, DB restore) is a zombie forever: ``release_stale_claims``
    needs ``claim_expires``, ``detect_crashed_workers`` needs a host-local lock +
    pid, ``detect_stale_running`` is off by default. Orphans go back to ``ready``
    with a comment, leaked run closed, ``reconciled`` event; a row with a live
    host-local PID is deferred so no duplicate spawns beside it.
    """
    now = int(time.time())
    reconciled: list[str] = []
    rows = conn.execute(
        "SELECT id, claim_lock, claim_expires, worker_pid, worker_started_at FROM tasks "
        "WHERE status = 'running' "
        "  AND (claim_lock IS NULL OR claim_expires IS NULL)"
    ).fetchall()
    for row in rows:
        tid = row["id"]
        pid = row["worker_pid"]
        verdict = _worker_liveness(pid, _kb._row_get(row, "worker_started_at")) if pid else WORKER_DEAD
        if verdict != WORKER_DEAD:
            # Never requeue beside a live process — nor beside one whose identity cannot be read:
            # the reclaim would spawn a duplicate next to a worker that may still be running
            # (#123811). Retry next tick.
            _kb._log.debug(
                "kanban reconcile: task %s has broken claim bookkeeping but "
                "pid %s is %s on this host — deferring", tid, pid, verdict,
            )
            # Record the deferral. ``detect_crashed_workers`` refuses to pronounce a reclaimed run
            # a crash without a reason from DISOWNED_RUN_REASONS, and this event is what makes the
            # later recovery honest: without it the next sweep would read a live worker's card as
            # a dead one whose attempt must be written off.
            with _kb.write_txn(conn):
                _kb._append_event(
                    conn, tid, "reclaim_deferred",
                    {
                        "reason": "orphaned_running_worker_alive",
                        "liveness": verdict,
                        "claim_lock": row["claim_lock"],
                        "claim_expires": _kb._opt_int(row["claim_expires"]),
                        "worker_pid": int(pid) if pid else None,
                        "now": now,
                    },
                )
            continue
        with _kb.write_txn(conn):
            cur = conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
                "last_heartbeat_at = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND claim_lock IS ? AND claim_expires IS ?",
                (tid, row["claim_lock"], row["claim_expires"]),
            )
            if cur.rowcount != 1:
                continue
            payload = {
                "reason": "orphaned_running",
                "claim_lock": row["claim_lock"],
                "claim_expires": _kb._opt_int(row["claim_expires"]),
                "worker_pid": int(pid) if pid else None,
                "now": now,
            }
            run_id = _kb._end_run(
                conn, tid,
                outcome="reclaimed", status="reclaimed",
                error="orphaned running card (broken claim bookkeeping)",
                metadata=payload,
            )
            _kb._insert_comment(
                conn, tid, "dispatcher",
                "reconciliation: card was 'running' with no valid claim "
                "(dead/gone worker) — requeued to ready",
                now,
            )
            _kb._append_event(conn, tid, "reconciled", payload, run_id=run_id)
            # The reclaim has no worker evidence: nobody observed a crash, the card simply
            # lost its claim bookkeeping. Reconcile the run it closed with an empty completion so
            # that row is not the attempt history's last word, and so the failure counter the
            # recovery booked does not stand against a worker that never failed.
            _kb._synthesize_empty_completion_run(conn, tid, disowned_run_id=run_id)
            reconciled.append(tid)
        _kb._log.info(
            "kanban reconcile: requeued orphaned running task %s "
            "(claim_lock=%r, worker_pid=%r)", tid, row["claim_lock"], pid,
        )
    return reconciled


def _error_fingerprint(error_text: str) -> str:
    """Normalize an error message (strip PIDs, timestamps) so same-root-cause errors group."""
    fp = re.sub(r'\bpid \d+\b', 'pid N', error_text[:80])
    fp = re.sub(r'\b\d{10,}\b', '<TS>', fp)
    return fp.lower().strip()


# ~96% of "clean exit without a terminal tool call" tasks complete on a later
# run, so a protocol violation gets a bounded retry before the breaker trips.
# The budget is a violation-only STREAK (``_protocol_violation_streak``),
# independent of ``consecutive_failures``: other failure kinds neither consume
# nor extend it. Per-task ``max_retries`` overrides it.
_PROTOCOL_VIOLATION_FAILURE_LIMIT = 3

# Closed runs to walk when counting the streak; it trips at a handful anyway.
_PROTOCOL_VIOLATION_SCAN_LIMIT = 50


def _protocol_violation_streak(conn: sqlite3.Connection, task_id: str) -> int:
    """Count the task's trailing run of clean-exit protocol violations.

    Walks closed runs newest-first (including the one ``detect_crashed_workers``
    just closed). ``rate_limited`` runs are neutral and skipped (a quota wall
    says nothing about the task); any other closed run breaks the streak, so
    the budget counts ONLY protocol violations. Violations are recognized by the
    ``protocol_violation`` run-metadata marker, with the error text as fallback
    for runs recorded before the marker existed.
    """
    streak = 0
    rows = conn.execute(
        "SELECT outcome, error, metadata FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY id DESC LIMIT ?",
        (task_id, _PROTOCOL_VIOLATION_SCAN_LIMIT),
    ).fetchall()
    for row in rows:
        outcome = row["outcome"] or ""
        if outcome == "rate_limited":
            continue
        if outcome == "crashed" and (
            _kb._json_dict(row["metadata"]).get("protocol_violation")
            or "protocol violation" in (row["error"] or "")
        ):
            streak += 1
            continue
        break
    return streak


_PROTOCOL_VIOLATION_ERROR = (
    # Worker subprocess returned 0 but its task is still ``running`` in the DB — it exited without calling
    # ``kanban_complete`` / ``kanban_block`` / ``kanban_request_review``. Overwhelmingly the work itself succeeded and only the
    # paperwork was skipped, so a retry usually completes; the corrective sentence below is surfaced to the
    # retry worker via the prior-attempt error in ``build_worker_context`` (guidance approach from #61817).
    # Keep this short: ``_record_task_failure`` caps the stored error at 500 chars and the worker's own
    # last output (``_worker_final_output``, up to 400 chars) is appended after it — a longer preamble
    # truncates away the worker's explanation, which is the part the board and the retry worker need.
    "worker exited cleanly (rc=0) without kanban_complete, kanban_block "
    "or kanban_request_review — protocol violation. "
    "If the prior run already did the work, verify it and "
    "report it via kanban_complete (or kanban_request_review); "
    "a run without a terminal kanban call counts as failed no "
    "matter what it did."
)


# Rich panel/rule chrome around the rendered response, and the CLI's own preamble lines.
_LOG_CHROME = re.compile(r"[─━═╭╮╰╯│┃┌┐└┘]+|☤\s*Hermes")


def _exit_summary_marker() -> str:
    """The CLI exit-summary header (``cli_session_mixin.show_exit_summary``), in the active language."""
    from agent.i18n import t
    return t("cli.session.exit_resume_hint")


def _log_noise_prefixes() -> tuple[str, ...]:
    from agent.i18n import t
    return ("session_id:", "Query:", t("cli.chat.initializing_agent"))


def _worker_final_output(task_id: str, board: Optional[str] = None) -> str:
    """Best-effort read of a dead worker's last printed text, for the board diagnostic.

    A ``chat -q`` worker's stdout/stderr are redirected to its per-task log
    (``_default_spawn``), so when it exits without a terminal board call the
    reason is usually sitting there: the model's own explanation of why it could
    not comply (#88603), or the rendered provider error (#46593). The reap used to
    discard it in favour of a canned message on every retry. Trims the CLI exit
    summary, rule lines and the ``session_id:`` trailer; returns "" (never raises)
    on a missing/empty log.

    ``board`` must come from the dispatching tick: ambient current-board resolution
    is wrong for every board but the one the dispatcher thread happens to call
    "current", so the log would silently not be found.
    """
    try:
        raw = _kb.read_worker_log(task_id, tail_bytes=4000, board=board)
    except Exception:
        return ""
    if not raw:
        return ""
    raw = _EXIT_TRAILER_RE.sub("", raw)
    cut = raw.rfind(_exit_summary_marker())
    if cut != -1:
        raw = raw[:cut]
    lines = []
    for ln in raw.splitlines():
        ln = _LOG_CHROME.sub("", ln).strip()
        if ln and not ln.startswith(_log_noise_prefixes()):
            lines.append(ln)
    return " ".join(lines)[-400:]


@dataclass
class _DeadWorker:
    """How ``detect_crashed_workers`` should book one dead worker."""

    kind: str
    code: Optional[int]
    error_text: str
    event_kind: str
    event_payload: dict
    protocol_violation: bool = False
    rate_limited: bool = False
    terminal_provider: bool = False
    """``KANBAN_TERMINAL_PROVIDER_EXIT_CODE``: the provider rejected the worker's
    credential/model — trips the breaker on this first occurrence."""

    @property
    def run_outcome(self) -> str:
        # A rate-limited requeue is recorded as ``rate_limited`` so board history
        # doesn't show a phantom crash for a quota wall.
        return "rate_limited" if self.rate_limited else "crashed"


def _classify_dead_worker(
    pid: int, claimer: Optional[str], *, task_id: Optional[str] = None, board: Optional[str] = None,
) -> _DeadWorker:
    """Map a dead worker's reaped exit status to its reclaim bookkeeping.

    A clean exit or a crash carries the worker's own last output (``worker_output``
    in the event payload, appended to the error text) so the board and the retry
    worker see WHY instead of a bare label; a rate-limited requeue does not need it.
    """
    dead = _classify_dead_worker_exit(pid, claimer, task_id=task_id, board=board)
    if task_id and not dead.rate_limited:
        worker_output = _worker_final_output(task_id, board=board)
        if worker_output:
            dead.error_text += f" Worker's last output: {worker_output!r}"
            dead.event_payload["worker_output"] = worker_output
    return dead


def _classify_dead_worker_exit(
    pid: int,
    claimer: Optional[str],
    *,
    task_id: Optional[str] = None,
    board: Optional[str] = None,
) -> _DeadWorker:
    """Exit status -> reclaim bookkeeping, before the worker's own words are folded in.

    The reap registry only knows children of THIS process; a per-tick dispatcher
    reads the exit trailer the worker left in its log instead, so the same death
    gets the same booking (protocol violation / rate-limit requeue / crash) as
    under the gateway-embedded dispatcher. A worker that never reached its exit
    epilogue (killed, OOM) leaves no trailer and stays a plain crash.
    """
    kind, code = _classify_worker_exit(pid)
    if kind == "unknown" and task_id:
        logged = _worker_log_exit_code(task_id, board=board)
        if logged is not None:
            kind, code = _exit_code_kind(logged)
    if kind == "clean_exit":
        # rc=0 while still ``running``: usually the work succeeded and only the
        # paperwork was skipped; the corrective sentence reaches the retry
        # worker via ``build_worker_context``.
        return _DeadWorker(
            kind, code, _PROTOCOL_VIOLATION_ERROR, "protocol_violation",
            # ``protocol_violation`` is the durable marker for
            # _protocol_violation_streak: _end_run copies this payload into the
            # run metadata.
            {"pid": pid, "claimer": claimer, "exit_code": code, "protocol_violation": True},
            protocol_violation=True,
        )
    if kind == "rate_limited":
        # Quota wall — NOT a task failure. Release to the source phase and do
        # NOT count a failure so a long quota window can't trip the breaker.
        return _DeadWorker(
            kind, code,
            f"pid {pid} exited rate-limited (quota wall) — requeued without counting a failure",
            "rate_limited",
            {"pid": pid, "claimer": claimer, "exit_code": code},
            rate_limited=True,
        )
    if kind == "terminal_provider":
        # The worker classified its own provider failure as unhealable (credential
        # revoked, model gone): every further spawn would hit the same wall, so
        # ``_account_crashes`` trips the breaker now instead of after ``failure_limit``.
        return _DeadWorker(
            kind, code,
            f"pid {pid} exited on a terminal provider error (exit {code}): the provider rejected "
            "this profile's credential or model — fix the configuration, then unblock.",
            "crashed",
            {"pid": pid, "claimer": claimer, "exit_kind": kind, "exit_code": code, "terminal_provider": True},
            terminal_provider=True,
        )
    if kind == "nonzero_exit":
        error_text = f"pid {pid} exited with code {code}"
    elif kind == "signaled":
        error_text = f"pid {pid} killed by signal {code}"
    else:
        error_text = f"pid {pid} not alive"
    event_payload = {"pid": pid, "claimer": claimer}
    if code is not None and kind != "unknown":
        event_payload["exit_kind"] = kind
        event_payload["exit_code"] = code
    return _DeadWorker(kind, code, error_text, "crashed", event_payload)


@dataclass
class _CrashSweep:
    """Everything ``detect_crashed_workers`` collects inside its reclaim txn."""

    crashed: list[str] = field(default_factory=list)
    rate_limited: list[str] = field(default_factory=list)
    # ``(task_id, pid, claimer, dead_worker)``: accounted after the txn via
    # ``_record_task_failure`` (needs its own write_txn).
    crash_details: list[tuple[str, int, str, _DeadWorker]] = field(default_factory=list)
    # Worker-exit observer payloads, fired only after every reclaim/accounting
    # txn has committed.
    exited_hook_payloads: list[dict] = field(default_factory=list)


def _reclaim_dead_workers(conn: sqlite3.Connection, board: Optional[str] = None) -> _CrashSweep:
    """Release every host-local ``running`` task whose worker PID is dead."""
    sweep = _CrashSweep()
    with _kb.write_txn(conn):
        rows = conn.execute(
            "SELECT id, worker_pid, worker_started_at, claim_lock, started_at, assignee "
            "FROM tasks "
            "WHERE status = 'running' AND worker_pid IS NOT NULL"
        ).fetchall()
        host_prefix = _kb._host_prefix()
        for row in rows:
            lock = row["claim_lock"] or ""
            if not lock.startswith(host_prefix):
                continue
            # Launch-window grace so a freshly-spawned worker isn't reclaimed
            # before its PID is visible on /proc.
            started_at = _kb._row_get(row, "started_at")
            if started_at is not None and time.time() - started_at < _kb._resolve_crash_grace_seconds():
                continue
            if _worker_not_dead(row["worker_pid"], _kb._row_get(row, "worker_started_at")):
                # Alive, or unprovable. Only PROOF of death releases a claim or spawns beside it:
                # an unreadable start-time/probe read is not proof, and treating it as one put six
                # duplicate workers on six live cards in a single tick (#123811). Held rows are
                # re-read next tick, and the TTL/backstop paths bound the wait.
                continue

            pid = int(row["worker_pid"])
            dead = _classify_dead_worker(pid, row["claim_lock"], task_id=row["id"], board=board)
            retry_status = _kb._retry_status_for_run(conn, row["id"])
            dead.event_payload["retry_status"] = retry_status
            cur = conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND worker_pid = ? AND claim_lock IS ?",
                (retry_status, row["id"], pid, row["claim_lock"]),
            )
            if cur.rowcount != 1:
                continue
            run_id = _kb._end_run(
                conn, row["id"],
                outcome=dead.run_outcome, status=dead.run_outcome,
                error=dead.error_text,
                metadata=dict(dead.event_payload),
            )
            _kb._append_event(conn, row["id"], dead.event_kind, dead.event_payload, run_id=run_id)
            sweep.exited_hook_payloads.append({
                "task_id": row["id"],
                "assignee": row["assignee"],
                "run_id": run_id,
                "worker_pid": pid,
                "exit_kind": dead.kind,
                "exit_code": dead.code,
                "outcome": dead.run_outcome,
                "retry_status": retry_status,
            })
            if dead.rate_limited or dead.protocol_violation:
                # Stamp last_failure_error WITHOUT touching ``consecutive_failures``:
                # a rate-limited requeue must show ``check_respawn_guard`` a quota
                # blocker; a below-budget protocol violation never reaches
                # ``_record_task_failure`` (which stamps this column), yet the
                # board UI and retry worker need the corrective message.
                conn.execute(
                    "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
                    (dead.error_text[:500], row["id"]),
                )
            if dead.rate_limited:
                sweep.rate_limited.append(row["id"])
            else:
                sweep.crashed.append(row["id"])
                sweep.crash_details.append((row["id"], pid, row["claim_lock"], dead))
    return sweep


def _account_crashes(conn: sqlite3.Connection, crash_details: list) -> list[str]:
    """Count each crash against the breaker; returns the task ids it tripped.

    Protocol violations get a BOUNDED violation-only budget independent of
    ``consecutive_failures`` (per-task ``max_retries`` takes precedence);
    systemic same-error crashes (>= 3 identical fingerprints this tick) and
    terminal provider errors (credential revoked, model gone — a retry cannot
    heal them) trip immediately.
    """
    auto_blocked: list[str] = []
    fp_counts: dict[str, int] = {}
    for _, _, _, dead in crash_details:
        fp = _error_fingerprint(dead.error_text)
        fp_counts[fp] = fp_counts.get(fp, 0) + 1
    for tid, pid, claimer, dead in crash_details:
        error_text = dead.error_text
        if dead.protocol_violation:
            streak = _protocol_violation_streak(conn, tid)
            trow = conn.execute("SELECT max_retries FROM tasks WHERE id = ?", (tid,)).fetchone()
            if trow is None:
                continue  # task deleted mid-loop
            task_override = _kb._row_get(trow, "max_retries")
            violation_limit = (
                int(task_override) if task_override is not None else _PROTOCOL_VIOLATION_FAILURE_LIMIT
            )
            if streak < violation_limit:
                # Below budget: already back at ``ready`` with the error stamped.
                # No ``_record_task_failure`` — must not consume the unified budget.
                continue
            # ``force_trip``: the decision (incl. per-task ``max_retries``) was
            # already made against the violation streak above.
            tripped = _record_task_failure(
                conn, tid,
                error=error_text,
                outcome="crashed",
                failure_limit=violation_limit,
                force_trip=True,
                release_claim=False,
                end_run=False,
                event_payload_extra={
                    "pid": pid,
                    "claimer": claimer,
                    "protocol_violations": streak,
                    "protocol_violation_limit": violation_limit,
                },
            )
        elif dead.terminal_provider:
            # A retry cannot heal a revoked credential or a missing model, so
            # the whole ``failure_limit`` budget would be spent on identical
            # failures. ``force_trip`` blocks now, sticky: ``recompute_ready``
            # must not auto-resume it before the operator fixes the provider.
            tripped = _record_task_failure(
                conn, tid,
                error=error_text,
                outcome="crashed",
                force_trip=True,
                release_claim=False,
                end_run=False,
                event_payload_extra={"pid": pid, "claimer": claimer, "terminal_provider": True},
            )
        else:
            is_systemic = fp_counts.get(_error_fingerprint(error_text), 0) >= 3
            extra = {"pid": pid, "claimer": claimer}
            if is_systemic:
                # Trips at 1, below any ``failure_limit``: hold it for an operator.
                extra["sticky"] = True
            tripped = _record_task_failure(
                conn, tid,
                error=error_text,
                outcome="crashed",
                failure_limit=1 if is_systemic else None,
                release_claim=False,
                end_run=False,
                event_payload_extra=extra,
            )
        if tripped:
            auto_blocked.append(tid)
    return auto_blocked


def detect_crashed_workers(conn: sqlite3.Connection, board: Optional[str] = None) -> list[str]:
    """Reclaim ``running`` tasks whose worker PID is no longer alive.

    Restores the source phase immediately (no waiting for the claim TTL), for
    tasks claimed by *this host* only — other hosts' PIDs are meaningless.
    Clean exit while ``running`` is a protocol violation with a bounded
    violation-only retry budget; ``KANBAN_RATE_LIMIT_EXIT_CODE`` is a quota
    wall, released WITHOUT counting a failure and surfaced via the
    ``_last_rate_limited`` attribute (the return stays crashed-only).
    """
    sweep = _reclaim_dead_workers(conn, board=board)
    # Outside the main txn: account each crash and maybe trip the breaker.
    auto_blocked = _account_crashes(conn, sweep.crash_details) if sweep.crash_details else []
    # Side-channel attributes keep the public ``list[str]`` return stable;
    # ``dispatch_once`` reads them to populate ``DispatchResult``. Rate-limited
    # requeues did NOT count a failure and are NOT crashes.
    detect_crashed_workers._last_auto_blocked = auto_blocked  # type: ignore[attr-defined]
    detect_crashed_workers._last_rate_limited = sweep.rate_limited  # type: ignore[attr-defined]
    # Fired only now, after the reclaim txn AND breaker accounting have
    # committed, so subscribers always observe fully durable board state.
    if sweep.exited_hook_payloads and _kb._kanban_observer_consumed("on_kanban_worker_exited"):
        _board = _kb.get_current_board()
        for hook_fields in sweep.exited_hook_payloads:
            hook_fields = dict(hook_fields)
            _kb._fire_kanban_lifecycle_hook(
                # Kanban worker-lifecycle, task-mutation, and dispatcher-tick observers (RFC #58548,
                # accepted as the design basis in the #64231 batch disposition; on_kanban_dispatch_tick is
                # the re-port of PR #56066). All five are observers only: return values are ignored, and
                # every fire site is fully best-effort, so a broken callback can never break dispatch or a
                # task mutation. Cost rule: every call site short-circuits on has_hook(), so when nothing
                # subscribes no payload is built and the hot paths (each dispatcher tick, each task write)
                # pay one dict probe. WHICH PROCESS: worker spawn/exit/stale-claim and the dispatch tick
                # fire in the DISPATCHER process (gateway-embedded dispatcher or ``hermes kanban
                # dispatch``); on_kanban_task_updated fires in whichever process committed the mutation
                # (CLI, worker, or the gateway-embedded dashboard API). Common kwargs (task-scoped hooks):
                # task_id: str, profile_name: str, board: str | None, assignee: str | None, run_id: int |
                # None. on_kanban_worker_spawned fires after ``spawn_fn`` returns AND the worker PID (when
                # one was reported) is durably persisted, per the RFC timing contract; like
                # kanban_task_claimed it runs inside the board's dispatch lock, so callbacks must stay fast.
                # Adds: worker_pid: int | None, workspace_path: str. Privacy: workspace_path is a filesystem
                # path and may reveal project layout or usernames.
                "on_kanban_worker_exited",
                hook_fields.pop("task_id"),
                board=_board,
                **hook_fields,
            )
    return sweep.crashed


def _record_task_failure(
    conn: sqlite3.Connection,
    task_id: str,
    error: str,
    *,
    outcome: str,
    failure_limit: int = None,
    force_trip: bool = False,
    release_claim: bool = False,
    end_run: bool = False,
    event_payload_extra: Optional[dict] = None,
    infrastructure: bool = False,
) -> bool:
    """Record a non-success outcome and maybe trip the circuit breaker; every
    non-success path funnels through here so ``consecutive_failures`` stays
    consistent. Returns True when the task was auto-blocked.

    ``release_claim=True, end_run=True``: spawn-failure path (task still
    running with an open run — restore source phase or ``blocked``, release
    claim, close run). Both False: timeout/crash path (caller already restored
    the phase and closed the run; only the counter moves, a trip flips to
    ``blocked`` + ``gave_up``). Threshold: per-task ``max_retries`` >
    ``failure_limit`` > ``DEFAULT_FAILURE_LIMIT``. ``force_trip`` trips
    unconditionally (caller applied its own bounded-retry policy).

    ``infrastructure=True``: the host refused the spawn (no restart-safe scope,
    #114720) — nothing about the card ran, so the run and event are recorded
    with ``infrastructure: true`` but ``consecutive_failures`` is left alone and
    the breaker never trips; the card stays retryable and
    :func:`check_respawn_guard` spaces the retries.
    """
    if failure_limit is None:
        failure_limit = DEFAULT_FAILURE_LIMIT
    error = error[:500]
    with _kb.write_txn(conn):
        row = conn.execute(
            "SELECT consecutive_failures, status, max_retries, current_run_id "
            "FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None:
            return False
        retry_status = (
            _kb._retry_status_for_run(conn, task_id, row["current_run_id"])
            if release_claim
            else ("review" if row["status"] == "review" else "ready")
        )
        failures = int(row["consecutive_failures"]) + (0 if infrastructure else 1)

        # Per-task override wins over caller-supplied and default thresholds.
        task_override = _kb._row_get(row, "max_retries")
        if task_override is not None:
            effective_limit, limit_source = int(task_override), "task"
        else:
            effective_limit, limit_source = int(failure_limit), "dispatcher"

        if infrastructure or not (force_trip or failures >= effective_limit):
            if release_claim:
                # Spawn path: restore the claimed source phase + clear claim.
                conn.execute(
                    "UPDATE tasks SET status = ?, claim_lock = NULL, "
                    "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
                    "consecutive_failures = ?, last_failure_error = ? "
                    "WHERE id = ? AND status = 'running'",
                    (retry_status, failures, error, task_id),
                )
            else:
                conn.execute(
                    "UPDATE tasks SET consecutive_failures = ?, "
                    "last_failure_error = ? WHERE id = ?",
                    (failures, error, task_id),
                )
            # Timeout/crash path's caller already emitted its own event.
            if end_run:
                detail = {"failures": failures, "retry_status": retry_status}
                if infrastructure:
                    detail["infrastructure"] = True
                # The CALLER's cause detail rides the run and the event on this path too, not just on
                # the trip path below. A non-tripping budget death is the case that mattered: the
                # board showed ``outcome=timed_out`` with no way to tell an iteration-budget
                # exhaustion from a wall-clock reap, so the notice named the wrong cause (#card).
                if event_payload_extra:
                    detail.update(event_payload_extra)
                run_id = _kb._end_run(
                    conn, task_id, outcome=outcome, status=outcome, error=error, metadata=detail,
                )
                _kb._append_event(conn, task_id, outcome, {"error": error, **detail}, run_id=run_id)
            return False

        # Spawn path (release_claim) is still running and also clears claim
        # state; the timeout/crash path already did.
        conn.execute(
            "UPDATE tasks SET status = 'blocked', "
            + ("claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
               if release_claim else "")
            + "consecutive_failures = ?, last_failure_error = ? "
            "WHERE id = ? AND status IN ('running', 'ready', 'review')",
            (failures, error, task_id),
        )
        payload = {
            "failures": failures,
            "effective_limit": effective_limit,
            "limit_source": limit_source,
            "error": error,
            "trigger_outcome": outcome,
            "retry_status": retry_status,
        }
        run_id = None
        if end_run:
            # Only the spawn path has an open run to close.
            run_id = _kb._end_run(
                conn, task_id, outcome="gave_up", status="gave_up", error=error,
                metadata={
                    "failures": failures,
                    "trigger_outcome": outcome,
                    "effective_limit": effective_limit,
                    "limit_source": limit_source,
                    "retry_status": retry_status,
                },
            )
        if force_trip:
            # The caller applied its own bounded policy, so the counter cannot
            # judge this block: ``recompute_ready`` holds it for an operator.
            payload["sticky"] = True
        if event_payload_extra:
            payload.update(event_payload_extra)
        _kb._append_event(conn, task_id, "gave_up", payload, run_id=run_id)
        return True


def _set_worker_pid(conn: sqlite3.Connection, task_id: str, pid: int) -> None:
    """Record the spawned child's pid + its restart-stable fingerprint (``_process_fingerprint``), and
    emit a ``spawned`` event carrying them. The fingerprint is what lets every later liveness/kill
    decision tell OUR worker from a process that recycled the PID after a reboot. A failed capture is
    persisted as ``UNVERIFIED_WORKER_FINGERPRINT``, never NULL: NULL is the legacy pre-fingerprint row
    whose bare-PID kill authority a new spawn must not inherit."""
    started_at = _process_fingerprint(int(pid)) or UNVERIFIED_WORKER_FINGERPRINT
    with _kb.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_pid = ?, worker_started_at = ? WHERE id = ?",
                     (int(pid), started_at, task_id))
        run_id = _kb._current_run_id(conn, task_id)
        if run_id is not None:
            conn.execute("UPDATE task_runs SET worker_pid = ?, worker_started_at = ? WHERE id = ?",
                         (int(pid), started_at, run_id))
        _kb._append_event(conn, task_id, "spawned", {"pid": int(pid), "started_at": started_at}, run_id=run_id)


def adopt_worker_pid(conn: sqlite3.Connection, task_id: str, run_id: int, pid: int) -> bool:
    """Worker-side half of ``_set_worker_pid``, run by the worker before its first model call.

    A dispatcher killed between spawning the worker and ``_set_worker_pid`` leaves the run with no
    pid: no liveness check can see the worker, so a TTL expiry reclaims the card and spawns a second
    worker beside it. The worker fills the missing pid itself (``worker_registered``). False when
    ``run_id`` is no longer the card's live run: the card was reclaimed before this worker got here,
    and it must exit without working it."""
    started_at = _process_fingerprint(int(pid)) or UNVERIFIED_WORKER_FINGERPRINT
    with _kb.write_txn(conn):
        row = conn.execute("SELECT status, current_run_id, worker_pid, claim_lock FROM tasks WHERE id = ?",
                           (task_id,)).fetchone()
        if row is None or row["status"] != "running" or row["current_run_id"] != int(run_id):
            return False
        # Liveness checks are host-local: a pid from another host (or pid namespace) proves nothing here.
        if row["worker_pid"] is None and (row["claim_lock"] or "").startswith(_kb._host_prefix()):
            conn.execute("UPDATE tasks SET worker_pid = ?, worker_started_at = ? WHERE id = ?",
                         (int(pid), started_at, task_id))
            conn.execute("UPDATE task_runs SET worker_pid = ?, worker_started_at = ? WHERE id = ?",
                         (int(pid), started_at, int(run_id)))
            _kb._append_event(conn, task_id, "worker_registered", {"pid": int(pid), "started_at": started_at},
                              run_id=int(run_id))
    return True


def _clear_failure_counter(conn: sqlite3.Connection, task_id: str) -> None:
    """Reset the unified consecutive-failures counter.

    Called from ``complete_task`` on success. NOT called on spawn success: a
    spawn proves the worker could start, not that the run will succeed, so
    timeouts and crashes must accumulate across spawn boundaries.
    """
    with _kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 0, "
            "last_failure_error = NULL WHERE id = ?",
            (task_id,),
        )


def check_respawn_guard(
    conn: sqlite3.Connection, task_id: str, *, lane: str = "ready",
) -> Optional[str]:
    """Return a guard reason if ``task_id`` should NOT be re-spawned, else None.

    Called per ready/review row before any claim attempt. Priority order:
    ``"infrastructure_cooldown"`` (latest run is a ``spawn_failed`` the host
    refused — no restart-safe scope — within the cooldown; never counted),
    ``"rate_limit_cooldown"`` (latest run ``rate_limited`` within the cooldown;
    checked BEFORE ``blocker_auth`` because the requeue stamps a quota-flavored
    ``last_failure_error`` that would otherwise park the task forever — that
    path never increments ``consecutive_failures``), ``"blocker_auth"``
    (quota/auth pattern; the breaker still trips eventually), then for the
    ready lane only ``"recent_success"`` (completed run within the window, unless
    a re-queue event arrived after it — a deliberate re-run) and ``"active_pr"``
    (PR URL in a recent comment; re-spawning risks a duplicate PR — unless the
    card was deliberately re-queued (``status`` / ``promoted`` / ``unblocked``)
    or handed off (``assigned`` / ``changes_requested`` / ``review_reopened``)
    after that comment: the named profile must work on that PR, or a human asked
    for it again). The review lane skips the last two: they are the *inputs* to a
    review handoff. Stale / dead claim locks are NOT a guard reason — the reclaim
    passes own those.
    """
    row = conn.execute(
        "SELECT last_failure_error FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None

    now = int(time.time())

    # 1. Rate-limit cooldown — see docstring for why this precedes blocker_auth.
    #    LATEST run only: a newer crash/completion supersedes the rate-limit run.
    #    An infrastructure spawn refusal (#114720) shares the cooldown: the host
    #    condition is not the card's, so it retries forever, spaced, and never
    #    reaches the breaker.
    rl_cooldown = _kb._resolve_rate_limit_cooldown_seconds()
    latest_run = conn.execute(
        "SELECT outcome, ended_at, metadata FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY ended_at DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if latest_run is not None and latest_run["outcome"] == "spawn_failed":
        if rl_cooldown > 0 and _kb._json_dict(latest_run["metadata"]).get("infrastructure"):
            ended_at = latest_run["ended_at"]
            if ended_at is not None and (now - int(ended_at)) < rl_cooldown:
                return "infrastructure_cooldown"
    if latest_run is not None and latest_run["outcome"] == "rate_limited":
        if rl_cooldown <= 0:
            # Cooldown disabled — respawn immediately, skipping blocker_auth so
            # the stamped rate-limit text doesn't re-trap the task.
            return None
        ended_at = latest_run["ended_at"]
        if ended_at is not None and (now - int(ended_at)) < rl_cooldown:
            return "rate_limit_cooldown"
        # Cooldown elapsed — return early so blocker_auth doesn't catch the
        # stamped rate-limit text; this path intentionally retries forever
        # (spaced by the cooldown) until quota returns or a real run supersedes it.
        return None

    # 2. Quota / auth blocker: retrying immediately will not help.  A plain
    # crash is different: its persisted error includes the worker's last
    # captured output, which is context rather than a diagnosis and may contain
    # benign commands such as ``claude auth status`` (#117097).
    err = _kb._lossy_text(row["last_failure_error"])
    latest_outcome = latest_run["outcome"] if latest_run is not None else None
    if err and latest_outcome != "crashed" and _RESPAWN_BLOCKER_RE.search(err):
        return "blocker_auth"

    # Review-lane spawns stop here: a recent completed run and a fresh PR URL
    # are the canonical *inputs* to a review handoff, not duplicate-work signals.
    if lane == "review":
        return None

    # 3. Completed run within guard window. Exception: an explicit re-queue
    #    AFTER that success (done→ready drag, re-promotion, unblock, reclaim) is
    #    a deliberate "run it again" — otherwise a manual done→ready would sit
    #    silently held until the window elapses.
    cutoff = now - _RESPAWN_GUARD_SUCCESS_WINDOW
    recent_completed = conn.execute(
        "SELECT ended_at FROM task_runs "
        "WHERE task_id = ? AND outcome = 'completed' AND ended_at >= ? "
        "ORDER BY ended_at DESC LIMIT 1",
        (task_id, cutoff),
    ).fetchone()
    if recent_completed:
        completed_at = int(recent_completed["ended_at"] or 0)
        requeue_kinds = _RESPAWN_GUARD_REQUEUE_KINDS + _RESPAWN_GUARD_RECOVERY_KINDS
        requeued_after = conn.execute(
            "SELECT 1 FROM task_events "
            "WHERE task_id = ? AND created_at >= ? "
            f"AND kind IN {_kind_clause(requeue_kinds)} "
            "LIMIT 1",
            (task_id, completed_at, *requeue_kinds),
        ).fetchone()
        if not requeued_after:
            return "recent_success"

    # 4. GitHub PR URL in a recent comment — prior worker already opened a PR.
    #    Exceptions, and they are two DIFFERENT things:
    #      * a deliberate re-queue AFTER the newest PR comment (`status`,
    #        `promoted`, `unblocked`) — the queue owner asked for this card to
    #        run again, so it outranks the duplicate-PR risk. Without this a card
    #        that merely NAMES somebody else's PR (a precondition, a parent's
    #        merge, the merge it exists to verify) sits held for the full window
    #        and the only lifts left would fabricate an ownership move or a
    #        review verdict that never happened.
    #      * a handoff AFTER it (operator reassign, reviewer
    #        changes_requested, review reopen) — the named profile must now work
    #        on THAT PR (#111910).
    #    `reclaimed` is deliberately NOT accepted here: crash recovery is not a
    #    decision, so the worker that opened the PR is still not re-spawned
    #    against it. (Guard 3 DOES accept it — a crash during the success window
    #    would otherwise park that card forever.) That divergence is the
    #    decision; both guards read their vocabulary from this module.
    pr_cutoff = now - _RESPAWN_GUARD_PR_WINDOW
    for c in conn.execute(
        "SELECT body, created_at FROM task_comments "
        "WHERE task_id = ? AND created_at >= ? ORDER BY created_at DESC",
        (task_id, pr_cutoff),
    ).fetchall():
        body = _kb._lossy_text(c["body"])
        if not (body and _RESPAWN_GUARD_PR_URL_RE.search(body)):
            continue
        lift_kinds = _RESPAWN_GUARD_HANDOFF_KINDS + _RESPAWN_GUARD_REQUEUE_KINDS
        events = conn.execute(
            # Strictly after: a same-second tie stays guarded (fail closed).
            "SELECT kind, payload FROM task_events "
            "WHERE task_id = ? AND created_at > ? "
            f"AND kind IN {_kind_clause(lift_kinds)}",
            (task_id, int(c["created_at"] or 0), *lift_kinds),
        ).fetchall()
        for e in events:
            # A re-queue needs no payload inspection: the event kind IS the
            # decision ("run it again"), so there is nothing to fabricate.
            if e["kind"] in _RESPAWN_GUARD_REQUEUE_KINDS:
                return None
            if _is_handoff_event(e["kind"], e["payload"]):
                return None
        return "active_pr"

    return None


def _is_handoff_event(kind: str, payload: Optional[str]) -> bool:
    """Only an ``assigned`` event that moves the card to a DIFFERENT profile is
    a handoff. A no-op re-assign (dev→dev via CLI/dashboard/``reassign
    --reclaim``), an unassign, or the dispatcher's own
    ``kanban.default_assignee`` write would otherwise lift ``active_pr`` for
    the very implementer that opened the PR. Events without ``from`` (written
    before it was recorded) are not trusted as handoffs — fail closed."""
    if kind != "assigned":
        return True
    data = _kb._json_or(payload, {})
    if not isinstance(data, dict) or data.get("source") == "kanban.default_assignee":
        return False
    to = data.get("assignee")
    return bool(to) and "from" in data and data["from"] != to


def _append_respawn_guard_event(conn: sqlite3.Connection, task_id: str, reason: str) -> bool:
    """Record a ``respawn_guarded`` event for the CURRENT hold episode only.

    Returns True when a row was written. The callers already hold the write txn.

    The dispatcher evaluates the guard on every tick, so an unconditional write
    turns a held card into an event firehose: 8837 ``active_pr`` events over 24
    cards on one board, 412 of them for a single card in one 6.9h stall, which is
    how a full-board starvation read as noise instead of an alert. One event per
    episode is the readable contract, and it is what ``hermes kanban tail``
    already shows for the ``recent_success`` / self-review guards.

    A new event is written when:

      * the card has no such event yet (first hold), or
      * the reason changed (a different hold is now in force), or
      * the card left the guarded state since the last event — an
        ``_RESPAWN_GUARD_EPISODE_BREAK_KINDS`` event is newer, so this is a
        fresh episode — or
      * the re-notify interval elapsed, so a genuinely long stall keeps a
        heartbeat an operator polling ``tail`` can still see.
    """
    now = int(time.time())
    last = conn.execute(
        "SELECT id, created_at, payload FROM task_events "
        "WHERE task_id = ? AND kind = 'respawn_guarded' "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if last is not None:
        data = _kb._json_or(last["payload"], {})
        same_reason = isinstance(data, dict) and data.get("reason") == reason
        if same_reason and (now - int(last["created_at"] or 0)) < _RESPAWN_GUARD_EVENT_REPEAT_SECONDS:
            broke = conn.execute(
                "SELECT 1 FROM task_events "
                "WHERE task_id = ? AND id > ? "
                f"AND kind IN {_kind_clause(_RESPAWN_GUARD_EPISODE_BREAK_KINDS)} "
                "LIMIT 1",
                (task_id, int(last["id"]), *_RESPAWN_GUARD_EPISODE_BREAK_KINDS),
            ).fetchone()
            if not broke:
                return False
    _kb._append_event(conn, task_id, "respawn_guarded", {"reason": reason})
    return True


def _profile_exists_fn() -> Optional[Callable[[str], bool]]:
    """``hermes_cli.profiles.profile_exists``, or ``None`` when it cannot be
    imported (local import avoids a cycle; callers fall back to trusting the
    assignee).

    When ``kanban.dispatch_profiles`` is set (#110995) the returned predicate
    additionally requires the assignee to be listed, fail-closed — so a card
    assigned to ``default`` is only claimable by homes that opted into it.
    Foreign assignees land in the existing ``skipped_nonspawnable`` bucket.
    """
    try:
        from hermes_cli.profiles import normalize_profile_name, profile_exists
    except Exception:
        return None
    allowlist = _dispatch_profile_allowlist(normalize_profile_name)
    if allowlist is None:
        return profile_exists

    def _gated(name: str) -> bool:
        try:
            canon = normalize_profile_name(name)
        except ValueError:
            return False
        return canon in allowlist and bool(profile_exists(name))

    return _gated


def _dispatch_profile_allowlist(normalize_profile_name) -> Optional[frozenset]:
    """Per-home claim allowlist ``kanban.dispatch_profiles`` (#110995).

    On a shared board (one ``kanban.db`` mounted across several Hermes homes),
    every home's ``profile_exists`` returns True for ``default`` — the root
    profile every home has — so a card assigned to ``default`` is claimable by
    every home's dispatcher. A home opts out of foreign claims by declaring
    which assignees it may claim::

        kanban:
          dispatch_profiles: ["sage", "researcher"]   # or "sage,researcher"

    Returns ``None`` only when the key is absent from the user config (upstream
    behavior: any existing profile is claimable). A present value is
    fail-closed: an empty list, ``null`` or a bare ``dispatch_profiles:`` claims
    nothing. The user layer is read without the ``DEFAULT_CONFIG`` merge (whose
    ``None`` placeholder would make the key look present in every home), and a
    config read that raises also claims nothing — a corrupt config on a shared
    board must never widen this home's claim scope silently (#113620).
    """
    try:
        from hermes_cli.config_effective import load_user_config_effective
        kanban = (load_user_config_effective(fail_closed=True) or {}).get("kanban", {})
    except Exception as exc:
        _kb._log.warning(
            "kanban: could not read kanban.dispatch_profiles (%s: %s) — "
            "this home claims no cards until the config is readable",
            type(exc).__name__, exc,
        )
        return frozenset()
    if not isinstance(kanban, Mapping) or "dispatch_profiles" not in kanban:
        return None
    raw = kanban["dispatch_profiles"]
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        _kb._log.warning(
            "kanban: kanban.dispatch_profiles is present but empty — this home "
            "claims no cards; omit the key to allow any existing profile"
        )
        return frozenset()
    names = [str(n) for n in raw] if isinstance(raw, (list, tuple)) else str(raw).split(",")
    allowed = set()
    for n in names:
        try:
            allowed.add(normalize_profile_name(n))
        except ValueError:
            continue
    return frozenset(allowed)


def dispatch_profile_allowlist_summary() -> str:
    """Human-readable resolution of ``kanban.dispatch_profiles`` for this home.

    Surfaced by ``hermes kanban diagnostics`` so an operator on a shared board
    can see what a home believes it may claim (#113620): ``any`` (key absent),
    the sorted allowed names, or ``none (fail-closed: ...)``.
    """
    try:
        from hermes_cli.profiles import normalize_profile_name
    except Exception as exc:
        return f"none (fail-closed: profiles unavailable: {exc})"
    allowlist = _dispatch_profile_allowlist(normalize_profile_name)
    if allowlist is None:
        return "any"
    if allowlist:
        return ", ".join(sorted(allowlist))
    return ("none (fail-closed: kanban.dispatch_profiles is present but names no valid "
            "profile, or the config could not be read — omit the key to allow any)")


def _has_spawnable(conn: sqlite3.Connection, status: str) -> bool:
    rows = conn.execute(
        "SELECT DISTINCT assignee FROM tasks "
        "WHERE status = ? AND assignee IS NOT NULL AND claim_lock IS NULL",
        (status,),
    ).fetchall()
    if not rows:
        return False
    profile_exists = _profile_exists_fn()
    if profile_exists is None:
        # Can't introspect — assume spawnable, preserve legacy behavior.
        return True
    return any(profile_exists(row["assignee"]) for row in rows)


def has_spawnable_ready(conn: sqlite3.Connection) -> bool:
    """True iff a ready+assigned+unclaimed task maps to a real Hermes profile.

    Lets health telemetry tell "stuck" (``0 spawned`` with spawnable work) from
    "correctly idle" (only control-plane lanes waiting on ``claim_task``). Falls
    back to "any assigned" when ``profile_exists`` is unimportable.
    """
    return _has_spawnable(conn, "ready")


def has_spawnable_review(conn: sqlite3.Connection) -> bool:
    """:func:`has_spawnable_ready` for the review column."""
    return _has_spawnable(conn, "review")


def review_dispatch_enabled() -> bool:
    """Whether review tasks dispatch automatically. Default true (Hermes ships
    ``sdlc-review``); operators disable it for human-only review boards.
    """
    try:
        from hermes_cli.config import load_config
        return bool((load_config() or {}).get("kanban", {}).get("review_dispatch", True))
    except Exception:
        return True


# Skills injected into a REVIEW run on top of the card's own list. These names are written by the
# HARNESS, never by an operator at a prompt, so two rules hold (both enforced in _dispatch_lane_task
# and agent.skill_commands): (1) the name must be RESOLVED for the assignee's profile before spawn —
# an unresolvable injected name is skipped and recorded on the card, because the worker's preload
# loader raises ``Unknown skill(s)`` when nothing loaded and the run then dies at INIT, burning the
# card's failure budget without a line of substantive work; (2) the worker is told which names were
# injected (``HERMES_KANBAN_ADVISORY_SKILLS``) so its loader degrades an injected-but-missing name to
# a warning instead of a crash. Overridable per host with ``kanban.review_skills`` (``[]`` = none).
DEFAULT_REVIEW_SKILLS: tuple[str, ...] = ("sdlc-review",)


def _kanban_config() -> dict:
    """The ``kanban`` config block (empty when unreadable or malformed)."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        block = cfg.get("kanban") if isinstance(cfg, dict) else None
        return block if isinstance(block, dict) else {}
    except Exception:
        return {}


def review_injected_skills() -> tuple[str, ...]:
    """Skill names injected into a review run (``kanban.review_skills``; default ``sdlc-review``)."""
    configured = _kanban_config().get("review_skills", DEFAULT_REVIEW_SKILLS)
    if isinstance(configured, str):
        configured = [configured]
    if not isinstance(configured, (list, tuple)):
        return DEFAULT_REVIEW_SKILLS
    return tuple(str(name).strip() for name in configured if str(name).strip())


def _match_lane_skills(mapping: dict, assignee: Optional[str]) -> tuple[str, ...]:
    """Union the ``injected_skills`` buckets that apply to *assignee*, most specific first.

    Keys are lane ids, prefix globs (``platform-*``) or the ``"*"`` default floor. Buckets are
    UNIONED, so a lane bucket ADDS to the floor rather than replacing it. Prefix globs resolve
    longest-first, so a narrower pattern wins the ordering.
    """
    lane = (assignee or "").strip()
    order: list[str] = []
    if lane and lane in mapping:
        order.append(lane)
    if lane:
        order.extend(sorted(
            (k for k in mapping
             if isinstance(k, str) and k.endswith("*") and lane.startswith(k[:-1])),
            key=len, reverse=True,
        ))
    if "*" in mapping:
        order.append("*")
    names: list[str] = []
    for key in order:
        value = mapping.get(key)
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            continue
        for name in value:
            name = str(name).strip()
            if name and name not in names:
                names.append(name)
    return tuple(names)


def injected_skills_for(assignee: Optional[str]) -> tuple[str, ...]:
    """Skills injected into EVERY run of *assignee*'s lane (``kanban.injected_skills``).

    Shape: a lane-pattern -> skill-names mapping with ``"*"`` as the default floor; a flat list
    is the floor. An absent or empty value returns ``()``, so a host that configures nothing sees
    no behaviour change. Resolution happens at CLAIM time under the assignee's profile scope.
    """
    configured = _kanban_config().get("injected_skills")
    if isinstance(configured, str):
        return (configured.strip(),) if configured.strip() else ()
    if isinstance(configured, (list, tuple)):
        return tuple(n for n in (str(x).strip() for x in configured) if n)
    if isinstance(configured, dict):
        return _match_lane_skills(configured, assignee)
    return ()


def _profile_skill_resolvable(profile_home: Optional[str], name: str) -> bool:
    """Whether *name* would preload for the profile rooted at *profile_home*."""
    if not profile_home or not name:
        return False
    try:
        from agent.skill_commands import preload_skill_resolvable

        with _worker_profile_scope(profile_home):
            return bool(preload_skill_resolvable(name))
    except Exception as exc:
        _kb._log.debug("kanban dispatcher: skill probe for %r failed: %s", name, exc)
        return False


def lane_profile_home(assignee: Optional[str]) -> Optional[str]:
    """The profile home a worker for *assignee* will run in (None when unresolvable)."""
    try:
        from hermes_cli.profiles import normalize_profile_name, resolve_profile_env

        return str(resolve_profile_env(normalize_profile_name(assignee or "")))
    except Exception:
        return None


def resolve_lane_skills(
    assignee: Optional[str], names: Iterable[str],
) -> tuple[list[str], list[str]]:
    """Split *names* into ``(resolvable, unresolved)`` for the profile *assignee*.

    Resolution runs under the ASSIGNEE's profile scope through the same loader the worker's preload
    path uses, so "resolvable" means "loadable in the spawned worker" for everything the LANE owns.
    It is deliberately NOT the last word: a worker also sees project-tier skills from its workspace
    cwd, so a caller must never DELETE a requested name on this result alone — the dispatcher records
    the unresolved ones and flags them advisory, and the worker's own loader decides.
    """
    names = [name for name in dict.fromkeys(names or ()) if name]
    if not names:
        return [], []
    profile_home = lane_profile_home(assignee)
    resolvable: list[str] = []
    unresolved: list[str] = []
    for name in names:
        (resolvable if _profile_skill_resolvable(profile_home, name) else unresolved).append(name)
    return resolvable, unresolved


def resolve_review_injected_skills(assignee: Optional[str]) -> tuple[list[str], list[str]]:
    """The configured review skills, split into ``(injectable, skipped)`` for *assignee*."""
    return resolve_lane_skills(assignee, review_injected_skills())


def _record_skill_note(
    conn: sqlite3.Connection, task_id: str, event_kind: str, body: str, payload: dict,
) -> None:
    """Log AND record on the card — a skipped skill must never be a log line nobody reads."""
    _kb._log.warning("kanban dispatcher: %s (task %s)", body, task_id)
    try:
        _kb._append_event(conn, task_id, event_kind, payload)
    except Exception as exc:
        _kb._log.debug("kanban dispatcher: could not record %s on %s: %s", event_kind, task_id, exc)
    try:
        _kb.add_comment(conn, task_id, "dispatcher", f"**{body}**")
    except Exception as exc:
        _kb._log.debug("kanban dispatcher: could not comment %s on %s: %s", event_kind, task_id, exc)


def record_skipped_review_skills(
    conn: sqlite3.Connection, task_id: str, skipped: list[str], assignee: Optional[str],
) -> None:
    """Record, ON THE CARD, that a harness-injected review skill was not injected into this run."""
    names = ", ".join(skipped)
    _record_skill_note(
        conn, task_id, "review_skill_skipped",
        f"review skill injection skipped: {names} does not resolve for profile "
        f"{assignee or '?'} — this review run starts WITHOUT it. Install the skill in that "
        f"profile's skills dir (or set kanban.review_skills) to restore it.",
        {"skills": skipped, "assignee": assignee},
    )


def record_skipped_injected_skills(
    conn: sqlite3.Connection, task_id: str, skipped: list[str], assignee: Optional[str],
) -> None:
    """Record, ON THE CARD, that a harness-injected lane skill was not injected into this run."""
    names = ", ".join(skipped)
    _record_skill_note(
        conn, task_id, "injected_skill_skipped",
        f"injected skill(s) — {names} — do not resolve for profile {assignee or '?'}: this run "
        f"starts WITHOUT them. Install them in that profile's skills dir, or drop them from "
        f"kanban.injected_skills.",
        {"skills": skipped, "assignee": assignee},
    )


def record_unresolved_card_skills(
    conn: sqlite3.Connection, task_id: str, unresolved: list[str], assignee: Optional[str],
) -> None:
    """Record, ON THE CARD, that skills the card names do not resolve for the assignee's LANE."""
    names = ", ".join(unresolved)
    _record_skill_note(
        conn, task_id, "card_skill_unresolved",
        f"skill(s) requested by this card — {names} — do not resolve for profile "
        f"{assignee or '?'}: the worker starts WITHOUT them (they stay on the command line, so a "
        f"workspace-tier copy can still load) instead of dying at INIT. Add them to that profile's "
        f"skills dir, or drop them from the card.",
        {"skills": unresolved, "assignee": assignee},
    )


_review_skill_readiness_checked = False


def review_skill_readiness() -> list[dict]:
    """Per-profile resolution of the injected review skills: ``[{profile, resolved, missing}]``.

    Deterministic and read-only. A profile that cannot resolve an injected name is one whose review
    runs would start without that skill, so the dispatcher reports it at BOOT (once per process)
    instead of leaving each affected card to find out through two crashed runs.
    """
    report: list[dict] = []
    injected = review_injected_skills()
    if not injected:
        return report
    try:
        from hermes_cli.profiles import list_profile_names, normalize_profile_name, resolve_profile_env
    except Exception as exc:
        _kb._log.debug("kanban dispatcher: review-skill readiness unavailable: %s", exc)
        return report
    for profile in list_profile_names():
        try:
            home = str(resolve_profile_env(normalize_profile_name(profile)))
        except Exception:
            home = None
        missing = [name for name in injected if not _profile_skill_resolvable(home, name)]
        report.append({
            "profile": profile,
            "resolved": [name for name in injected if name not in missing],
            "missing": missing,
        })
    return report


def check_review_skill_readiness_once() -> None:
    """Warn once per process when a review-capable profile cannot resolve an injected review skill."""
    global _review_skill_readiness_checked
    if _review_skill_readiness_checked:
        return
    _review_skill_readiness_checked = True
    try:
        report = review_skill_readiness()
    except Exception as exc:
        _kb._log.debug("kanban dispatcher: review-skill readiness probe failed: %s", exc)
        return
    broken = [row for row in report if row["missing"]]
    if broken:
        _kb._log.warning(
            "kanban dispatcher: review-skill readiness: %s — review runs on those profiles start "
            "WITHOUT the missing skill(s). Install them in the profile's skills dir or set "
            "kanban.review_skills.",
            "; ".join(f"{row['profile']}: {', '.join(row['missing'])}" for row in broken),
        )


# --- Board health: starved must not look like idle -------------------------
#
# On 2026-09-26 a board held 24 active-PR-ready cards with nothing running for
# 6.9h after its last completion. Every surface read clean: the dispatcher
# logged ``0 spawned. Last tick held back: active_pr=3`` beside another lane's
# genuine warning, so a full stall looked exactly like a quiet board. The fix is
# a health READ that names the difference, plus an escalation that acts on it
# (never a longer list of WARNING lines in a log nobody is tailing).


@dataclass
class BoardHealth:
    """The ``(ready_total, spawnable, suppressed_by_reason)`` board-health tuple.

    ``ready_total``
        Every ``ready`` row, including rows the dispatcher would never spawn.
    ``spawnable``
        The subset it could claim this tick: an assignee that resolves to a real
        profile and no live claim lock.
    ``suppressed_by_reason``
        Of those, how many are held back and why (the respawn-guard vocabulary).
    ``unavailable_by_reason``
        Ready rows the queue never offers, named rather than dropped: no
        assignee, an assignee that is not a profile (a control-plane lane pulls
        those via ``claim_task``), a live claim lock, or the per-profile cap.
    ``starved``
        Spawnable rows exist and none of them can start. This — not an empty
        board — is the condition that was invisible.
    """

    ready_total: int = 0
    spawnable: int = 0
    suppressed_by_reason: dict[str, int] = field(default_factory=dict)
    unavailable_by_reason: dict[str, int] = field(default_factory=dict)
    starved: bool = False

    @property
    def suppressed(self) -> int:
        return sum(self.suppressed_by_reason.values())

    @property
    def startable(self) -> int:
        """``spawnable`` minus the rows the per-profile cap defers."""
        return max(0, self.spawnable - self.unavailable_by_reason.get("per_profile_capped", 0))

    @property
    def state(self) -> str:
        """``starved`` / ``dispatchable`` / ``idle`` — the one-word verdict."""
        if self.starved:
            return "starved"
        if self.startable > 0:
            return "dispatchable"
        return "idle"

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "starved": self.starved,
            "ready_total": self.ready_total,
            "spawnable": self.spawnable,
            "startable": self.startable,
            "suppressed": self.suppressed,
            "suppressed_by_reason": dict(sorted(self.suppressed_by_reason.items())),
            "unavailable_by_reason": dict(sorted(self.unavailable_by_reason.items())),
        }

    def describe(self) -> str:
        """One line that distinguishes a starved board from a quiet one."""
        parts = [
            f"state={self.state}",
            f"ready_total={self.ready_total}",
            f"spawnable={self.spawnable}",
            f"startable={self.startable}",
            f"suppressed={self.suppressed}",
        ]
        for label, bucket in (("suppressed_by_reason", self.suppressed_by_reason),
                              ("unavailable_by_reason", self.unavailable_by_reason)):
            if bucket:
                parts.append(label + "=" + ",".join(f"{k}:{v}" for k, v in sorted(bucket.items())))
        return " ".join(parts)


def _bump(bucket: dict[str, int], key: str) -> None:
    bucket[key] = bucket.get(key, 0) + 1


def board_health(conn: sqlite3.Connection, *, board: Optional[str] = None) -> BoardHealth:
    """Live board-health read of the ready lane — see :class:`BoardHealth`.

    Cheap enough for a CLI read and a dashboard poll: one scan of the ready
    column plus one guard evaluation per spawnable row. Deliberately computed
    from the ROWS, not from the last tick's ``DispatchResult``, because the
    surfaces that must tell starved from idle include processes that never ran a
    tick (``hermes kanban health``, the dashboard).

    ``board`` is accepted for the caller's convenience and for an honest
    ``board`` field in the escalation body; the connection is already the
    board's.
    """
    health = BoardHealth()
    rows = conn.execute(
        "SELECT id, assignee, claim_lock FROM tasks WHERE status = 'ready'"
    ).fetchall()
    health.ready_total = len(rows)
    if not rows:
        return health

    profile_exists = _profile_exists_fn()
    cap = configured_max_in_progress()
    running: dict[str, int] = {}
    if isinstance(cap, int) and cap > 0:
        for prow in conn.execute(
            "SELECT assignee, COUNT(*) AS n FROM tasks "
            "WHERE status = 'running' AND assignee IS NOT NULL GROUP BY assignee"
        ):
            running[prow["assignee"]] = int(prow["n"])

    for row in rows:
        assignee = row["assignee"]
        if not assignee:
            _bump(health.unavailable_by_reason, "unassigned")
            continue
        if profile_exists is not None and not profile_exists(assignee):
            # Control-plane lanes pull these themselves via ``claim_task`` —
            # correctly idle, not a stall.
            _bump(health.unavailable_by_reason, "not_a_profile")
            continue
        if row["claim_lock"] is not None:
            _bump(health.unavailable_by_reason, "claimed")
            continue
        health.spawnable += 1
        if isinstance(cap, int) and cap > 0 and running.get(assignee, 0) >= cap:
            _bump(health.unavailable_by_reason, "per_profile_capped")
            continue
        reason = check_respawn_guard(conn, row["id"], lane="ready")
        if reason is not None:
            _bump(health.suppressed_by_reason, reason)

    # Every row that COULD start is held back: the board is starved, whatever
    # the row count. One suppressed row and a hundred read identically.
    health.starved = health.startable > 0 and health.suppressed == health.startable
    return health


def _lane_of(assignee: str) -> Optional[str]:
    """The lane an assignee belongs to, by the ``<lane>-<role>`` profile
    convention (``platform-coder`` → ``platform``).

    ``None`` when the name carries no recognised role suffix: an ad-hoc profile
    must never be silently binned into a lane that does not exist.
    """
    name = (assignee or "").strip().lower()
    for suffix in _LANE_ROLE_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return None


def _stall_escalation_assignee(held_assignees: Iterable[str]) -> Optional[str]:
    """Deterministic escalation seat for a starved ready queue.

    The routing is a FUNCTION, never a judgement: the lane with the most held
    rows decides, ties break lexicographically, and the lane's design-authority
    seat is ``<lane>-stl``. A seat that this home does not actually have as a
    profile falls through to ``kanban.orchestrator_profile`` and then
    ``kanban.default_assignee`` — both already-configurable fleet seats, so no
    new knob is introduced. Filing a card assigned to a profile the home does not
    have would park it in ``skipped_nonspawnable`` forever: silent, which is the
    failure mode this escalation exists to close. ``None`` means no routable seat
    exists and the caller must shout in the log instead of filing.
    """
    per_lane: dict[str, int] = {}
    for assignee in held_assignees:
        lane = _lane_of(assignee)
        if lane:
            per_lane[lane] = per_lane.get(lane, 0) + 1
    ranked = sorted(per_lane.items(), key=lambda pair: (-pair[1], pair[0]))
    candidates = [f"{ranked[0][0]}{_LANE_SEAT_SUFFIX}"] if ranked else []
    cfg = _kanban_config()
    for key in ("orchestrator_profile", "default_assignee"):
        value = str(cfg.get(key) or "").strip()
        if value:
            candidates.append(value)
    profile_exists = _profile_exists_fn()
    for name in candidates:
        if profile_exists is None or profile_exists(name):
            return name
    return None


def _stall_hold(result: "DispatchResult") -> Optional[dict[str, list[str]]]:
    """Held task ids by reason when THIS tick had startable work and spawned
    nothing, else ``None``.

    A zero-spawn tick is not automatically a stall. ``skipped_locked`` (another
    dispatcher is mid-tick), ``memory_pressure`` (deliberate backpressure) and a
    ready queue that is only unassigned or non-profile rows (correctly idle) are
    all healthy. A stall needs rows the dispatcher WOULD have spawned and did
    not: a guard hold or the per-profile cap.
    """
    if result.spawned or result.skipped_locked or result.memory_pressure:
        return None
    held: dict[str, list[str]] = {}
    for task_id, reason in result.respawn_guarded:
        held.setdefault(reason, []).append(task_id)
    if result.skipped_per_profile_capped:
        held["per_profile_capped"] = [
            task_id for task_id, _assignee, _current in result.skipped_per_profile_capped
        ]
    return held or None


def _file_stall_escalation(
    conn: sqlite3.Connection,
    *,
    board: Optional[str],
    held: dict[str, list[str]],
    ticks: int,
    assignee: str,
    health: BoardHealth,
    now: float,
) -> Optional[str]:
    """File the stall-escalation card. Returns its task id, or ``None``.

    Body and title are built from counts only: no prose a human has to interpret
    and no inference, so the routing decision is reviewable after the fact. The
    idempotency key is bucketed by ``_STALL_ESCALATION_REPEAT_SECONDS``, which
    bounds a multi-day stall to one card per board per interval even if two
    dispatchers race the same tick.
    """
    reason_counts = {reason: len(ids) for reason, ids in held.items()}
    summary = ", ".join(f"{k}={v}" for k, v in sorted(reason_counts.items()))
    held_ids = sorted(task_id for ids in held.values() for task_id in ids)
    body = "\n".join([
        f"Board {board or _kb.DEFAULT_BOARD!r}: the dispatcher has held its ready "
        f"queue back for {ticks} consecutive ticks with nothing spawned.",
        "",
        f"health: {health.describe()}",
        f"held_by_reason: {summary}",
        f"held_tasks: {', '.join(held_ids[:40])}"
        + (f" (+{len(held_ids) - 40} more)" if len(held_ids) > 40 else ""),
        "",
        "No worker started on any of those ticks, so the board is starved, not "
        "idle — see `hermes kanban health` for the same read on demand.",
        "The cause is per-card suppression (respawn guard / per-profile cap), "
        "not profile health: every held row has a spawnable assignee.",
        "",
        "Deterministic routing: the held rows' lanes decide the seat, and the "
        "seat this card is assigned to is the lane design authority for the "
        "majority of them.",
    ])
    key = f"kanban-dispatch-stall:{board or _kb.DEFAULT_BOARD}:{int(now // _STALL_ESCALATION_REPEAT_SECONDS)}"
    try:
        task_id = _kb.create_task(
            conn,
            title=f"kanban: dispatcher stall on {board or _kb.DEFAULT_BOARD}"
                  f" — {ticks} ticks with ready work held back ({summary})",
            body=body,
            assignee=assignee,
            created_by="kanban-dispatcher",
            board=board,
            idempotency_key=key,
            workspace_kind="scratch",
        )
    except TypeError:
        # Older create_task without idempotency_key/workspace_kind: still file —
        # a stall must not go unreported because of a signature drift.
        task_id = _kb.create_task(
            conn,
            title=f"kanban: dispatcher stall on {board or _kb.DEFAULT_BOARD}"
                  f" — {ticks} ticks with ready work held back ({summary})",
            body=body,
            assignee=assignee,
            created_by="kanban-dispatcher",
            board=board,
        )
    return task_id


@dataclass
class _StallTracker:
    """Per-board consecutive-stall bookkeeping (process-local, like the
    tick-hook bridge: two dispatcher processes on one board cannot both hold the
    single-writer lock, and the idempotency key is the cross-process backstop)."""

    consecutive: int = 0
    last_escalation_at: float = 0.0
    last_escalation_task: Optional[str] = None


_stall_trackers: dict[Optional[str], _StallTracker] = {}


def reset_stall_tracker(board: Optional[str] = None) -> None:
    """Drop the process-local stall counter (used by tests and by ``--once``)."""
    if board is None:
        _stall_trackers.clear()
    else:
        _stall_trackers.pop(board, None)


def observe_dispatch_tick(
    conn: sqlite3.Connection,
    result: "DispatchResult",
    *,
    board: Optional[str] = None,
    dry_run: bool = False,
) -> Optional[str]:
    """Track consecutive stall ticks for one board and escalate deterministically.

    Called by :func:`dispatch_once` AFTER the single-writer lock is released, so
    filing a card can never extend the critical section. Returns the escalation
    task id when this tick filed one.

    Failing to file must never break a tick, so every filing error is logged and
    swallowed — the noise is worth less than the queue.
    """
    if dry_run or result.skipped_locked:
        return None
    tracker = _stall_trackers.setdefault(board, _StallTracker())
    held = _stall_hold(result)
    if held is None:
        tracker.consecutive = 0
        return None
    tracker.consecutive += 1
    if tracker.consecutive < _STALL_ESCALATION_WINDOW:
        return None
    now = time.time()
    if (now - tracker.last_escalation_at) < _STALL_ESCALATION_REPEAT_SECONDS:
        return None
    held_assignees: list[str] = []
    held_ids = [task_id for ids in held.values() for task_id in ids]
    if held_ids:
        placeholders = ", ".join("?" for _ in held_ids)
        for row in conn.execute(
            f"SELECT assignee FROM tasks WHERE id IN ({placeholders})", tuple(held_ids)
        ):
            if row["assignee"]:
                held_assignees.append(row["assignee"])
    assignee = _stall_escalation_assignee(held_assignees)
    health = board_health(conn, board=board)
    tracker.last_escalation_at = now
    if assignee is None:
        # No routable seat for this home: say so loudly rather than filing a card
        # that can never be spawned. This is the one path that stays a log line,
        # and it names the misconfiguration instead of the symptom.
        _kb._log.error(
            "kanban dispatch: board %r starved for %d consecutive ticks (%s) and no "
            "escalation seat resolves — set kanban.orchestrator_profile or "
            "kanban.default_assignee to an existing profile",
            board or _kb.DEFAULT_BOARD, tracker.consecutive, health.describe(),
        )
        return None
    try:
        task_id = _file_stall_escalation(
            conn, board=board, held=held, ticks=tracker.consecutive,
            assignee=assignee, health=health, now=now,
        )
    except Exception as exc:  # noqa: BLE001 — never break the tick over reporting
        _kb._log.error(
            "kanban dispatch: could not file the stall escalation for board %r (%s): %s",
            board or _kb.DEFAULT_BOARD, health.describe(), exc,
        )
        return None
    tracker.last_escalation_task = task_id
    _kb._log.warning(
        "kanban dispatch: board %r starved for %d consecutive ticks (%s); "
        "escalated to %s as %s",
        board or _kb.DEFAULT_BOARD, tracker.consecutive, health.describe(),
        assignee, task_id,
    )
    return task_id


# Memory-aware dispatch guard: an uncapped board once OOM'd a 1 GiB host. Two
# safeguards — a memory-DERIVED default cap when none is configured
# (``resolve_max_in_progress``) and a live memory-PRESSURE guard inside the
# tick (``_memory_pressure_level``) because a static cap can't see other
# tenants. Both fail open: non-Linux / read error → no cap / "unknown".

# Assumed per-worker footprint for the derived cap; deliberately conservative
# so the cap errs toward fewer workers on small VMs.
MEMORY_GUARD_MB_PER_WORKER = 512

# Derived default bounds: never below 2 (smallest VM must still progress),
# never above 8 (more fan-out must be explicit in config).
DERIVED_MAX_IN_PROGRESS_FLOOR = 2
DERIVED_MAX_IN_PROGRESS_CEILING = 8


def _system_memory_sample() -> dict:
    """Best-effort system memory snapshot (KiB values), ``{}`` when unknown.

    Local import keeps ``kanban_db`` importable without the gateway package.
    Module-level indirection is also the test seam — conftest patches this to
    ``{}`` so results don't depend on the CI runner's live memory.
    """
    try:
        from gateway.lifecycle_ledger import sample_memory
        return sample_memory() or {}
    except Exception:
        return {}


def derive_default_max_in_progress(sample: Optional[Mapping[str, Any]] = None) -> Optional[int]:
    """Memory-derived default for ``kanban.max_in_progress`` when unset:
    ``clamp(MemTotal / MEMORY_GUARD_MB_PER_WORKER, FLOOR, CEILING)``. Returns
    ``None`` (no cap) when total memory is unknown, so macOS/Windows dev
    machines are unaffected.
    """
    if sample is None:
        sample = _system_memory_sample()
    total_kib = sample.get("mem_total_kib")
    if isinstance(total_kib, bool) or not isinstance(total_kib, int) or total_kib <= 0:
        return None
    workers = (total_kib // 1024) // MEMORY_GUARD_MB_PER_WORKER
    return max(DERIVED_MAX_IN_PROGRESS_FLOOR, min(workers, DERIVED_MAX_IN_PROGRESS_CEILING))


def resolve_max_in_progress(configured: Optional[int]) -> Optional[int]:
    """Effective global concurrency cap: explicit config wins, else the
    memory-derived default. All config-parsing callers route through this so
    both paths agree.
    """
    if configured is not None:
        return configured
    return derive_default_max_in_progress()


def configured_max_in_progress() -> Optional[int]:
    """Read ``kanban.max_in_progress`` from config, or None when unset/invalid.

    Shared so every dispatch entry point agrees on "explicitly configured": a
    positive integer wins, anything else falls through to the derived default.
    """
    try:
        from hermes_cli.config import load_config_readonly
        raw = (load_config_readonly() or {}).get("kanban", {}).get("max_in_progress")
    except Exception:
        return None
    if raw is None:
        return None
    try:
        ival = int(raw)
    except (TypeError, ValueError):
        return None
    return ival if ival >= 1 else None


def lane_fair_ready_config(kanban_cfg: Optional[dict] = None) -> tuple[bool, int]:
    """``(lane_fair_spawn, designated_pool_reserve)`` from the ``kanban`` block.

    Production defaults are ``(True, 1)`` (``config_defaults.DEFAULT_CONFIG``).
    ``kanban_cfg=None`` loads config itself (mtime-cached), so the standalone
    daemon and ``hermes kanban dispatch`` resolve the fairness knobs exactly as
    the gateway dispatcher, which passes the merged block it already holds. A
    malformed value fails open to the default rather than disabling fairness or
    raising into a tick.
    """
    if kanban_cfg is None:
        try:
            from hermes_cli.config import load_config_readonly
            kanban_cfg = (load_config_readonly() or {}).get("kanban", {}) or {}
        except Exception:
            kanban_cfg = {}
    kanban_cfg = kanban_cfg or {}
    raw_fair = kanban_cfg.get("lane_fair_spawn", True)
    if isinstance(raw_fair, bool):
        lane_fair = raw_fair
    elif raw_fair is None:
        lane_fair = True
    else:
        lane_fair = str(raw_fair).strip().lower() not in {"0", "false", "no", "off", ""}
    reserve = _positive_int(kanban_cfg.get("designated_pool_reserve"), 1, minimum=0)
    return lane_fair, reserve


# ``max_runtime_seconds`` stamped onto a card that carries none, AT CLAIM TIME, so every running slot
# recycles within a bounded wall clock and ``enforce_max_runtime`` can always reap it (ruling
# t_b2865b89 §4: a NULL cap is never reaped). The value the CARD AUTHOR set always wins — including an
# explicit ``0``, which is the author's "unlimited" opt-out.
DEFAULT_MAX_RUNTIME_SECONDS = 7200


def default_max_runtime_seconds_config(kanban_cfg: Optional[dict] = None) -> int:
    """``kanban.default_max_runtime_seconds`` — the cap stamped onto a card that carries none.

    ``0`` disables the stamp entirely (no card behaviour changes at all). ``kanban_cfg=None`` loads
    config itself (mtime-cached), exactly as :func:`lane_fair_ready_config`, so the standalone daemon,
    ``hermes kanban dispatch`` and the in-gateway dispatcher all resolve the knob the same way. A
    malformed value fails open to the production default rather than raising into a tick or silently
    disabling slot recycling.
    """
    if kanban_cfg is None:
        try:
            from hermes_cli.config import load_config_readonly
            kanban_cfg = (load_config_readonly() or {}).get("kanban", {}) or {}
        except Exception:
            kanban_cfg = {}
    kanban_cfg = kanban_cfg or {}
    # 0 is a REAL value (no stamp), so it cannot go through ``_positive_int`` (minimum=1); only a
    # missing / non-integer / negative value falls back to the production default.
    try:
        parsed = int(kanban_cfg.get("default_max_runtime_seconds", DEFAULT_MAX_RUNTIME_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_MAX_RUNTIME_SECONDS
    return parsed if parsed >= 0 else DEFAULT_MAX_RUNTIME_SECONDS


def count_running_tasks(conn: sqlite3.Connection) -> int:
    """Number of tasks in ``status='running'``.

    Used by the multi-board sweep to count OTHER boards' workers against the
    host-level budget — the memory-derived cap bounds the machine, not the
    board. Fails open to 0 so a broken board doesn't brick dispatch on healthy ones.
    """
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status = 'running'"
            ).fetchone()[0]
        )
    except Exception:
        return 0


def count_running_tasks_other_boards(board: Optional[str] = None) -> int:
    """Total ``running`` tasks across every board EXCEPT ``board``.

    Caps bound the HOST, but each board's tick only sees its own DB; without
    this a derived cap of N gets multiplied by the number of active boards.
    Boards are matched by resolved DB path, so a board is never double-counted
    through another board's store. Fails open per board.
    """
    try:
        current_path = str(_kb.kanban_db_path(board=board).expanduser().resolve())
    except Exception:
        current_path = None
    try:
        boards = _kb.list_boards(include_archived=False)
    except Exception:
        return 0
    total = 0
    for meta in boards:
        slug = meta.get("slug") or _kb.DEFAULT_BOARD
        try:
            path = _kb.kanban_db_path(board=slug).expanduser()
            resolved = str(path.resolve())
            if current_path is not None and resolved == current_path:
                continue
            if not path.exists():
                continue
            other = _kbc.connect(board=slug)
            try:
                total += count_running_tasks(other)
            finally:
                with contextlib.suppress(Exception):
                    other.close()
        except Exception:
            continue
    return total


def _memory_pressure_level(sample: Optional[Mapping[str, Any]] = None) -> str:
    """Classify system memory pressure: ok/elevated/critical/unknown.

    Reuses :func:`gateway.memory_status.classify_pressure` so "critical" matches
    the dashboard banner and lifecycle-ledger OOM heuristics. ``unknown``
    (non-Linux, read failure) imposes no restriction — never brick dispatch
    where /proc is unavailable.
    """
    if sample is None:
        sample = _system_memory_sample()
    if not sample:
        return "unknown"
    try:
        from gateway.memory_status import classify_pressure
        return classify_pressure(sample.get("mem_available_kib"), sample.get("mem_total_kib"))
    except Exception:
        return "unknown"


def dispatch_once(
    conn: sqlite3.Connection,
    *,
    spawn_fn=None,
    ttl_seconds: Optional[int] = None,
    dry_run: bool = False,
    max_spawn: Optional[int] = None,
    max_in_progress: Optional[int] = None,
    failure_limit: int = DEFAULT_FAILURE_LIMIT,
    stale_timeout_seconds: int = 0,
    board: Optional[str] = None,
    default_assignee: Optional[str] = None,
    max_in_progress_per_profile: Optional[int] = None,
    reconcile_orphans: bool = True,
    host_budget_share: Optional[int] = None,
    lane_fair_spawn: bool = True,
    designated_pool_reserve: int = 1,
    default_max_runtime_seconds: Optional[int] = None,
) -> DispatchResult:
    """Run one dispatcher tick under the board's single-writer lock.

    Wraps :func:`_dispatch_once_locked` in the non-blocking :func:`_dispatch_tick_lock`
    so two dispatchers on one ``kanban.db`` never race a write tick on WAL
    frames. The loser returns an empty ``DispatchResult`` with
    ``skipped_locked=True`` and writes nothing; the lock is keyed on the
    resolved DB path so unrelated boards tick in parallel.

    A board that is not dispatch-enabled (an estate/rehearsal board) is refused
    BEFORE the lock, the reclaim phase and every claim: the tick returns
    ``DispatchResult(skipped_board_disabled=True)`` having touched nothing, so no
    phantom card on that board can be spawned even by a caller that bypassed the
    dispatcher's own enumeration (card t_17c9c847).
    """
    # Resolve the board this tick is actually against. The pin/name may be absent
    # (the CLI passes no board=), so read it off the open store — the same mapping
    # ``board_for_connection`` gives, never the ambient current board.
    resolved_board = board
    if not resolved_board:
        try:
            resolved_board = _kb.board_for_connection(conn)
        except Exception:
            resolved_board = None
    if resolved_board and not _kb.board_dispatch_enabled(resolved_board):
        result = DispatchResult(skipped_board_disabled=True)
        _kb._fire_dispatch_tick_hook(result, board=board, dry_run=dry_run)
        return result

    def _locked_tick() -> DispatchResult:
        return _dispatch_once_locked(
            conn,
            spawn_fn=spawn_fn,
            ttl_seconds=ttl_seconds,
            dry_run=dry_run,
            max_spawn=max_spawn,
            max_in_progress=max_in_progress,
            failure_limit=failure_limit,
            stale_timeout_seconds=stale_timeout_seconds,
            board=board,
            default_assignee=default_assignee,
            max_in_progress_per_profile=max_in_progress_per_profile,
            reconcile_orphans=reconcile_orphans,
            host_budget_share=host_budget_share,
            lane_fair_spawn=lane_fair_spawn,
            designated_pool_reserve=designated_pool_reserve,
            default_max_runtime_seconds=default_max_runtime_seconds,
        )

    try:
        db_path = _kb.kanban_db_path(board=board)
    except Exception:
        # Must not lose the tick — fall through to an unguarded dispatch.
        result = _locked_tick()
        _kb._fire_dispatch_tick_hook(result, board=board, dry_run=dry_run)
        observe_dispatch_tick(conn, result, board=board, dry_run=dry_run)
        return result
    with _kbc._dispatch_tick_lock(db_path) as held:
        if not held:
            result = DispatchResult(skipped_locked=True)
        else:
            result = _locked_tick()
            # Still under the dispatch lock: periodic PASSIVE WAL checkpoint.
            _kbc._maybe_checkpoint_wal(conn, db_path)
    # Lock released. Fire the tick observer strictly OUTSIDE the critical
    # section: a slow subscriber must never stall a sibling dispatcher's tick.
    _kb._fire_dispatch_tick_hook(result, board=board, dry_run=dry_run)
    # Board-health escalation also runs outside the lock: filing a card must
    # never extend the single-writer critical section, and a starved board must
    # escalate as a CARD (a routed, owned action) rather than one more WARNING
    # line in a log nobody is tailing.
    observe_dispatch_tick(conn, result, board=board, dry_run=dry_run)
    return result


def _call_spawn_fn(spawn_fn, task: Task, workspace: str, board: Optional[str]) -> Optional[int]:
    """Back-compat: older spawn_fn signatures (and test stubs) accept only
    ``(task, workspace)``; pass ``board`` only when the callable supports it."""
    import inspect
    try:
        sig = inspect.signature(spawn_fn)
        if "board" in sig.parameters:
            return spawn_fn(task, workspace, board=board)
        return spawn_fn(task, workspace)
    except (TypeError, ValueError):
        return spawn_fn(task, workspace)


def _self_review_reason(
    conn: sqlite3.Connection, task_id: str, assignee: str,
) -> Optional[str]:
    """Why ``assignee`` must not be spawned to review ``task_id``, else ``None``.

    ``"assignee_is_implementer"`` — the card's recorded implementer IS this
    assignee (canonicalised the same way ``request_review`` canonicalises a
    reviewer, so capitalization/case-only differences cannot slip past).
    ``"implementer_unknown"`` — the card carries no ``review_requested``
    implementer provenance at all, so a distinct reviewer cannot be PROVEN.
    Fail-closed: unknown provenance parks rather than trusts, since the default
    ``reviewer=None`` handoff records no reviewer and leaves the author as the
    assignee.
    """

    try:
        implementer = _kb.review_implementer(conn, task_id)
        if not implementer:
            return "implementer_unknown"
        if _kb._canonical_assignee(implementer) == _kb._canonical_assignee(assignee):
            return "assignee_is_implementer"
    except Exception:
        # Fail OPEN only on provenance we cannot read (old board schema, missing
        # task_events, a row shape we cannot normalise): a dispatch tick must not
        # die on the guard, and the previous behavior for an unreadable row was
        # to dispatch it. A readable-but-missing implementer still parks.
        return None
    return None


def _stamp_default_max_runtime_seconds(
    conn: sqlite3.Connection, task_id: str, *, limit: int,
) -> Optional[int]:
    """Write ``limit`` onto ``tasks.max_runtime_seconds`` when the card carries none.

    Slot recycling (ruling ``t_b2865b89`` §4): the runtime sweep is gated on ``max_runtime_seconds IS
    NOT NULL``, so a card with no cap is never reaped and its spawn slot never recycles. ``IS NULL``
    in the WHERE clause (rather than ``COALESCE``) is deliberate: it makes the stamp idempotent and
    gives a card's OWN value precedence — including an explicit ``0``, the author's unlimited
    opt-out — so re-claiming a card can never overwrite an author's budget.

    Returns the value written, or ``None`` when the card already carried one. Opens its own write txn
    (the caller holds no lock at this point) and records a ``runtime_cap_defaulted`` event, so the
    defaulted cap is auditable rather than a silent column mutation.
    """
    with _kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET max_runtime_seconds = ? "
            "WHERE id = ? AND max_runtime_seconds IS NULL",
            (int(limit), task_id),
        )
        if cur.rowcount != 1:
            return None
        _kb._append_event(conn, task_id, "runtime_cap_defaulted", {
            "max_runtime_seconds": int(limit),
            "source": "kanban.default_max_runtime_seconds",
        })
    return int(limit)


def _dispatch_lane_task(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    assignee: str,
    result: "DispatchResult",
    *,
    lane: str,
    dry_run: bool,
    ttl_seconds: Optional[int],
    board: Optional[str],
    failure_limit: int,
    spawn_fn,
    per_profile_cap: Optional[int],
    per_profile_running: dict[str, int],
    estop_state: Optional[Any] = None,
    default_max_runtime_seconds: int = 0,
) -> bool:
    """Guard, claim, resolve the workspace and spawn one ready/review row.
    Returns True when a spawn slot was consumed (real or ``dry_run``); every
    skip is recorded on ``result``.
    """
    task_id = row["id"]
    # Non-profile assignees (control-plane lanes that pull via ``claim_task``)
    # would fail ``hermes -p <assignee>`` at startup and loop ready→crash→ready
    # forever. Bucketed apart from skipped_unassigned: the operator cannot fix
    # it by assigning a profile, and health telemetry suppresses "stuck" for it.
    profile_exists = _profile_exists_fn()
    if profile_exists is not None and not profile_exists(assignee):
        result.skipped_nonspawnable.append(task_id)
        # Per-task diagnostic so ``show``/``tail`` name the missing profile instead of leaving
        # the card in ``ready`` with zero board evidence (#122422). Unlike a respawn guard the
        # condition never expires on its own, so write it once: a repeat only when something
        # else happened on the card since (reassign, comment) — not one row per tick forever,
        # and not one row per foreign home per tick on a shared board (#101015).
        if not dry_run:
            with _kb.write_txn(conn):
                last = conn.execute(
                    "SELECT kind, payload FROM task_events WHERE task_id = ? "
                    "ORDER BY created_at DESC, id DESC LIMIT 1", (task_id,)).fetchone()
                if (last is None or last["kind"] != "skipped_nonspawnable"
                        or last["payload"] != _kb._json_or_null({"assignee": assignee})):
                    _kb._append_event(conn, task_id, "skipped_nonspawnable", {"assignee": assignee})
        return False
    # Lane-scoped DEFCON gate: under a lockdown only the profiles on the estop
    # sentinel's allowlist may start work, and under a bare pause (mode estop)
    # nobody may. BOARD IS NOT A TERM — the same card is refused on every board,
    # which is what makes this lane-scoped instead of a board exemption. Placed
    # before every other guard so ``dry_run`` sees exactly the same decision, and
    # recorded on the card as a ``skipped_lockdown`` event (not once per tick,
    # see :func:`_record_lockdown_events`) because a silent non-spawn is a defect.
    if not _lane_admitted(estop_state, assignee, board):
        # Set-wise: the pre-budget sweep may have noted this same card already
        # (:func:`_note_lockdown_hold`), so a hold is never double-counted.
        _note_lockdown_hold(result, task_id, assignee)
        return False
    # Author exclusion: the review lane must never spawn a task's own
    # implementer with the review skill — the bot that wrote the change would be
    # grading (and could land) its own work. Fail-closed: park the row until a
    # distinct reviewer is named. Placed before the cap / respawn guards so
    # ``dry_run`` sees exactly the same decision. Deliberately no task event:
    # this guard can stay engaged for many ticks and an event per tick would
    # spam ``hermes kanban tail``.
    if lane == "review":
        self_review = _self_review_reason(conn, task_id, assignee)
        if self_review is not None:
            result.skipped_self_review.append((task_id, self_review))
            return False
    # Per-profile cap: one profile's local model / API quota / browser pool
    # must not be overwhelmed by a fan-out even with global headroom.
    if per_profile_cap is not None:
        current = per_profile_running.get(assignee, 0)
        if current >= per_profile_cap:
            result.skipped_per_profile_capped.append((task_id, assignee, current))
            return False
    guard_reason = check_respawn_guard(conn, task_id, lane=lane)
    if guard_reason is not None:
        result.respawn_guarded.append((task_id, guard_reason))
        # Event so ``hermes kanban tail`` shows why the task looks stuck —
        # written once per hold episode, not once per tick
        # (:func:`_append_respawn_guard_event`).
        # Honour kanban.default_assignee: when the dispatcher hits an unassigned ready task and an
        # operator-configured fallback exists, persist the assignment and proceed. This removes the
        # dashboard footgun where a task created without an assignee parks in 'ready' forever even though
        # the operator's intent ("default") was perfectly clear (#27145). Mutating the row (not just the
        # in-memory view) keeps diagnostics and the board state consistent: the task is now legitimately
        # owned by ``kanban.default_assignee``, not "unassigned but secretly routed".
        if not dry_run:
            with _kb.write_txn(conn):
                _append_respawn_guard_event(conn, task_id, guard_reason)
        return False

    # Consent gate (RUN door): a card that asserts consent it cannot show, or
    # that declares itself consent-gated with nothing behind it, is refused the
    # spawn and parked needs_input. Reported on the guard channel so
    # ``hermes kanban tail`` shows why this card stopped; the parking itself is
    # the durable record (event + one comment + block).
    consent_reason = check_consent_guard(conn, task_id, dry_run=dry_run)
    if consent_reason is not None:
        result.respawn_guarded.append((task_id, consent_reason))
        return False

    def _count_spawn(name: str) -> None:
        # Later rows in this tick respect the per-profile cap; subsequent
        # ticks re-query from the DB.
        if per_profile_cap is not None and name:
            per_profile_running[name] = per_profile_running.get(name, 0) + 1

    if dry_run:
        result.spawned.append((task_id, assignee, ""))
        _count_spawn(assignee)
        return True
    claim = _kb.claim_review_task if lane == "review" else _kb.claim_task
    claimed = claim(conn, task_id, ttl_seconds=ttl_seconds)
    if claimed is None:
        return False
    # SLOT RECYCLING (ruling t_b2865b89 §4). A card that carried no ``max_runtime_seconds`` is
    # stamped with the dispatcher default HERE — at claim time — so ``enforce_max_runtime`` can
    # always reap the slot instead of leaving the tick budget at 0 for hours.
    #
    # THE BOUNDARY (do not remove this pin): the DEFAULTED cap must never reach
    # ``_worker_terminal_timeout_env``. That call raises the child worker's ``TERMINAL_TIMEOUT`` to
    # ``cap - 30 s``; letting a scheduling default through would jump every lane in the fleet from
    # the generic terminal default to 7170 s, a behaviour change hidden inside a scheduling fix. So
    # the card's EXPLICIT pre-stamp value is captured first and pinned back onto the Task handed to
    # the spawn — the DB column gets the default, the spawned worker sees ONLY what the author set.
    explicit_max_runtime_seconds = claimed.max_runtime_seconds
    if default_max_runtime_seconds:
        _stamp_default_max_runtime_seconds(conn, task_id, limit=default_max_runtime_seconds)
    claimed.max_runtime_seconds = explicit_max_runtime_seconds
    try:
        resolved_branch_name = None
        if claimed.workspace_kind == "worktree":
            workspace, resolved_branch_name = _kbw._resolve_worktree_workspace(claimed, board=board)
        else:
            workspace = _kbw.resolve_workspace(claimed, board=board)
    except Exception as exc:
        if _record_task_failure(
            conn, claimed.id, f"workspace: {exc}",
            outcome="spawn_failed", failure_limit=failure_limit, release_claim=True, end_run=True,
        ):
            result.auto_blocked.append(claimed.id)
        return False
    _kbw.set_workspace_path(conn, claimed.id, str(workspace))
    if claimed.workspace_kind == "worktree":
        _kbw.set_branch_name(conn, claimed.id, resolved_branch_name or (claimed.branch_name or "").strip() or f"wt/{claimed.id}")
    _kbw._maybe_emit_scratch_tip(conn, claimed.id, claimed.workspace_kind)
    # Skills the CARD names are advisory, never fatal: the worker also sees project-tier skills from
    # its workspace cwd, so an unresolved name is RECORDED on the card and left on the command line
    # for the worker's own loader to judge (which warns instead of raising for such a name).
    card_skills = [name for name in (claimed.skills or []) if name]
    advisory_skills: list[str] = []
    if card_skills:
        _, unresolved_card_skills = resolve_lane_skills(claimed.assignee, card_skills)
        if unresolved_card_skills:
            record_unresolved_card_skills(conn, claimed.id, unresolved_card_skills, claimed.assignee)
            advisory_skills.extend(unresolved_card_skills)

    # Skills the HARNESS injects for this lane's run: the lane's own `kanban.injected_skills`
    # bucket for EVERY lane, plus the review list on a review run. The kanban lifecycle itself is
    # already in every worker's system prompt via KANBAN_GUIDANCE. Only names that resolve for the
    # assignee are injected — an unresolvable one is recorded on the card and SKIPPED, never handed
    # to the worker's preload loader, which raises ``Unknown skill(s)`` when nothing loaded and
    # kills the run at INIT (see DEFAULT_REVIEW_SKILLS).
    injected = list(injected_skills_for(claimed.assignee))
    if lane == "review":
        injected.extend(name for name in review_injected_skills() if name not in injected)
    if injected:
        injectable, skipped_skills = resolve_lane_skills(claimed.assignee, injected)
        if injectable:
            claimed.skills = list(dict.fromkeys([*(claimed.skills or []), *injectable]))
        if skipped_skills:
            record_skipped_injected_skills(conn, claimed.id, skipped_skills, claimed.assignee)
        # Every name the HARNESS chose for this run (injected or skipped) is advisory too, so an
        # injected name can never be the reason a run dies at INIT.
        advisory_skills.extend(injected)

    if advisory_skills:
        try:
            claimed.advisory_skills = tuple(dict.fromkeys(advisory_skills))  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - Task is a plain dataclass
            pass
    try:
        pid = _call_spawn_fn(spawn_fn if spawn_fn is not None else _default_spawn, claimed, str(workspace), board)
        if pid:
            _set_worker_pid(conn, claimed.id, int(pid))
        # Fires AFTER the PID (when reported) is durably persisted. Best-effort.
        _kb._fire_worker_spawned_hook(conn, claimed, str(workspace), pid, board=board)
        # consecutive_failures is deliberately NOT reset here: resetting on
        # spawn would let a task that keeps timing out loop forever. Cleared
        # only on successful completion (complete_task).
        result.spawned.append((claimed.id, claimed.assignee or "", str(workspace)))
        _count_spawn(claimed.assignee)
        return True
    except Exception as exc:
        from tools.process_registry import RestartSafeScopeUnavailable

        # The host refused the spawn (no restart-safe scope): nothing about the
        # card ran, so it must not spend the card's retry budget (#114720).
        infrastructure = isinstance(exc, RestartSafeScopeUnavailable)
        if infrastructure:
            _kb._log.warning("kanban dispatcher: spawn of %s deferred, host cannot place the worker: %s", claimed.id, exc)
        if _record_task_failure(
            conn, claimed.id, str(exc),
            outcome="spawn_failed", failure_limit=failure_limit, release_claim=True, end_run=True,
            infrastructure=infrastructure,
        ):
            result.auto_blocked.append(claimed.id)
        return False


def check_consent_guard(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    dry_run: bool = False,
    approvals_db: Optional[str] = None,
) -> Optional[str]:
    """The RUN door of the consent gate: refuse a run that claims consent it cannot show.

    Deterministic, like :func:`check_respawn_guard` -- one regex pass over the
    card's own text plus a read-only lookup in the approvals store
    (``hermes_cli/kanban_consent_gate.py``; ruling: card ``t_e31d9241``).

    Returns a reason string when the card must not run, else ``None``. A refused
    card is PARKED ``blocked``/``needs_input`` with one durable comment naming
    the cause and the moves -- never merely skipped: a skipped-but-ready card
    loops silently every tick, and a spawned consent-gated card is the defect
    this gate exists to stop. ``dry_run`` reports the refusal without writing.
    """
    from hermes_cli import kanban_consent_gate as _cg

    row = conn.execute(
        "SELECT title, body, status FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if row is None or row["status"] not in ("ready", "review"):
        return None
    verdict = _cg.evaluate(row["title"], row["body"], approvals_db=approvals_db)
    if not verdict.refused:
        return None
    reason = f"consent-refused:{verdict.cause}"
    if dry_run:
        return reason
    _park_for_consent(conn, task_id, verdict)
    return reason


def _park_for_consent(conn: sqlite3.Connection, task_id: str, verdict) -> None:
    """Record the refusal on the card, then hold it (``needs_input`` is sticky).

    The event dedupe keeps ONE comment per distinct cause: the guard runs every
    tick, and a comment per tick would bury the board. A store outage, or a
    failed write, must never take the dispatcher down with it -- the next tick
    retries, and the card is not spawned either way.
    """
    from hermes_cli import kanban_consent_gate as _cg

    payload = {
        "cause": verdict.cause,
        "trigger": verdict.trigger,
        "refs": [r.ref for r in verdict.refs],
    }
    try:
        with _kb.write_txn(conn):
            last = conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'consent_refused' "
                "ORDER BY created_at DESC, id DESC LIMIT 1", (task_id,),
            ).fetchone()
            if last is None or last["payload"] != _kb._json_or_null(payload):
                _kb._append_event(conn, task_id, "consent_refused", payload)
                _kb.add_comment(
                    conn, task_id, "dispatcher",
                    "CONSENT REFUSED before the run (not spawned).\n\n"
                    + _cg.refusal_message(task_id, verdict),
                )
    except Exception:
        _kb._log.debug(
            "kanban dispatch: consent refusal could not be recorded for %s", task_id, exc_info=True,
        )
    try:
        _kb.block_task(conn, task_id, reason=f"consent: {verdict.cause}", kind="needs_input")
    except Exception:
        _kb._log.debug(
            "kanban dispatch: consent refusal could not park %s", task_id, exc_info=True,
        )


def _apply_default_assignee(
    conn: sqlite3.Connection, task_id: str, assignee: str, *, dry_run: bool,
) -> bool:
    """Persist ``kanban.default_assignee`` on an unassigned ready row.

    Mutating the row keeps board state honest: the task is legitimately owned
    by the default, not "unassigned but secretly routed". ``dry_run`` reports
    without writing. Returns False when the write failed.
    """
    if dry_run:
        return True
    try:
        with _kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET assignee = ? WHERE id = ? "
                "AND (assignee IS NULL OR assignee = '')",
                (assignee, task_id),
            )
            _kb._append_event(
                conn, task_id, "assigned",
                {"assignee": assignee, "source": "kanban.default_assignee"},
            )
    except Exception:
        _kb._log.debug(
            "kanban dispatch: failed to apply default_assignee=%r to task %s",
            assignee, task_id, exc_info=True,
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Budget-death goal-loop arm
# ---------------------------------------------------------------------------

# The error text a kanban worker's finalizer records when its ITERATION budget, not a
# ``max_runtime_seconds`` wall, ended the run. It is the trigger for the arm below.
BUDGET_DEATH_ERROR_PREFIX = "Iteration budget exhausted"

# Bounded defaults for an armed loop: enough turns for a card that needs a second run's worth of
# work, few enough that an unresolved card cannot spin, and a wall written beside it so a card that
# carried no ``max_runtime_seconds`` is now reapable.
DEFAULT_GOAL_ARM_TURNS = 5
DEFAULT_GOAL_ARM_MAX_RUNTIME_SECONDS = 7200


def goal_arm_config(kanban_cfg: Optional[dict] = None) -> tuple[bool, int, int]:
    """``(enabled, turns, max_runtime_seconds)`` for the budget-death goal-loop arm.

    ``kanban.goal_arm_on_budget_death`` (default True) is the policy switch;
    ``kanban.goal_arm_turns`` (default :data:`DEFAULT_GOAL_ARM_TURNS`) bounds the loop;
    ``kanban.goal_arm_max_runtime_seconds`` (default :data:`DEFAULT_GOAL_ARM_MAX_RUNTIME_SECONDS`)
    is the wall written when the card carries none.
    """
    if kanban_cfg is None:
        try:
            from hermes_cli.config import load_config

            kanban_cfg = (load_config().get("kanban") or {})
        except Exception:
            kanban_cfg = {}
    kanban_cfg = kanban_cfg or {}
    enabled = kanban_cfg.get("goal_arm_on_budget_death")
    enabled = True if enabled is None else bool(enabled)
    turns = _positive_int(kanban_cfg.get("goal_arm_turns"), DEFAULT_GOAL_ARM_TURNS, minimum=1)
    wall = _positive_int(
        kanban_cfg.get("goal_arm_max_runtime_seconds"),
        DEFAULT_GOAL_ARM_MAX_RUNTIME_SECONDS,
        minimum=1,
    )
    return enabled, turns, wall


def arm_goal_mode_after_budget_death(
    conn: sqlite3.Connection, *, kanban_cfg: Optional[dict] = None,
) -> list[str]:
    """Give a bounded goal loop to every card that just died of ITERATION exhaustion.

    Where does "this card needs more than one run's worth of work" live? Not at create time — most
    cards fit, and a fleet-wide default pays a judge call per turn on every card to cover the few
    that do not. The dispatcher decides on EVIDENCE instead: the failed run recorded
    ``Iteration budget exhausted (used/max)``, so the card has proved the wall applies to it.

    The sweep therefore reads ``ready``, ``todo`` **and** ``blocked``: the phase a card is parked in
    is not evidence about its work. A card whose LAST permitted attempt died of iteration exhaustion
    is parked ``blocked`` by the failure breaker, so it carries exactly the recorded evidence this
    arm keys on while its ``status`` alone would exclude it — which made the arm a no-op for
    precisely the cards that had proved they needed room. ``blocked`` is a READ, never a write: this
    is purely a PROPERTY write. The arm sets ``goal_mode`` / ``goal_max_turns`` / ``max_runtime_seconds``
    and nothing else, so a breaker-blocked card stays ``blocked`` with its ``block_kind`` and
    ``last_failure_error`` intact and simply carries the loop for whenever a human or the disposition
    sweep resumes it. The arm never unblocks.

    The arm is deterministic (a column read, no inference), idempotent (``goal_mode`` flips once and
    the WHERE clause then excludes the row), and reversible through the sanctioned surface
    (``hermes kanban edit <id> --no-goal``). It writes the SAME columns ``--goal`` writes, so every
    downstream reader — ``_spawn_worker``'s env construction, the goal loop's turn budget — sees a
    card the operator could have configured by hand, and the ``goal_armed`` event records why.
    """
    enabled, turns, wall = goal_arm_config(kanban_cfg)
    if not enabled:
        return []
    armed: list[str] = []
    rows = conn.execute(
        "SELECT id, goal_max_turns, max_runtime_seconds, consecutive_failures, last_failure_error "
        "FROM tasks "
        "WHERE status IN ('ready', 'todo', 'blocked') AND COALESCE(goal_mode, 0) = 0 "
        "  AND last_failure_error LIKE ?",
        (BUDGET_DEATH_ERROR_PREFIX + "%",),
    ).fetchall()
    for row in rows:
        tid = row["id"]
        with _kb.write_txn(conn):
            # COALESCE keeps whatever the card already carries: a turn budget the author set on a
            # single-shot card is respected, and the wall is only supplied where there is none.
            cur = conn.execute(
                "UPDATE tasks SET goal_mode = 1, "
                "       goal_max_turns = COALESCE(goal_max_turns, ?), "
                "       max_runtime_seconds = COALESCE(max_runtime_seconds, ?) "
                "WHERE id = ? AND COALESCE(goal_mode, 0) = 0",
                (turns, wall, tid),
            )
            if cur.rowcount != 1:
                continue
            _kb._append_event(conn, tid, "goal_armed", {
                "reason": "iteration_budget_exhausted",
                "turns": turns,
                "max_runtime_seconds": wall,
                "consecutive_failures": _kb._row_get(row, "consecutive_failures"),
                "error": (row["last_failure_error"] or "")[:200],
            })
        armed.append(tid)
        _kb._log.info(
            "kanban: armed a %d-turn goal loop on %s (its run died of iteration exhaustion); "
            "wall %ss unless the card set one", turns, tid, wall,
        )
    return armed


def _run_reclaim_phase(
    conn: sqlite3.Connection,
    result: DispatchResult,
    *,
    stale_timeout_seconds: int,
    failure_limit: int,
    reconcile_orphans: bool,
    board: Optional[str] = None,
) -> None:
    """Reclaim stale/orphaned/crashed/timed-out running tasks, then promote."""
    reap_worker_zombies()
    result.reaped_terminal_workers = reap_terminal_workers(conn)
    result.reclaimed = _kb.release_stale_claims(conn, failure_limit=failure_limit)
    if reconcile_orphans:
        result.reconciled_orphans = reconcile_orphaned_running(conn)
    result.stale = detect_stale_running(conn, stale_timeout_seconds=stale_timeout_seconds)
    result.crashed = detect_crashed_workers(conn, board=board)
    # Side-channel attributes (see detect_crashed_workers); rate-limited tasks
    # went back to ``ready`` and the respawn guard defers them until quota clears.
    result.auto_blocked.extend(getattr(detect_crashed_workers, "_last_auto_blocked", []))
    result.rate_limited.extend(getattr(detect_crashed_workers, "_last_rate_limited", []))
    result.timed_out = enforce_max_runtime(conn)
    # A card that just died of iteration exhaustion has proved its work does not fit one run's
    # budget, so it gets a bounded goal loop BEFORE this tick's spawn decision: the pass runs here,
    # after the failure was recorded and before promotion, so the retry cannot walk back into the
    # same wall with the same single-shot shape (see arm_goal_mode_after_budget_death).
    result.goal_armed = arm_goal_mode_after_budget_death(conn)
    result.promoted = _kb.recompute_ready(conn, failure_limit=failure_limit)
    # Door 3 of the above-tranche guard (t_6ce41549): a row ABOVE the reserved tranche is
    # LOWERED into its class ceiling, board-locally, on every tick. Deterministic and
    # idempotent, and placed after promotion because a promoted row is exactly the kind of row
    # a later door could have written above the ceiling: the invariant is restored before the
    # spawn decision is taken, never after it.
    result.priority_demoted = []
    result.priority_demote_refused = []
    try:
        demoted = _kb.demote_above_tranche(conn, board=board)
    except Exception as exc:  # noqa: BLE001 - a repair must never stop the board dispatching
        # THE PASS CANNOT TAKE THE TICK DOWN (measured 2026-10-01: an abort in this pass — door 3
        # refusing the pass's own write — killed every `defcon` tick for 6.5 h, and the tick died
        # BEFORE the spawn loop, so 452 ready cards spawned nothing). Whatever the guard and the
        # doors disagree about, the repair of an already-broken invariant is not what stops the
        # board's dispatching: it is recorded on the tick's own result and logged at ERROR.
        demoted = []
        result.priority_demote_error = "%s: %s" % (type(exc).__name__, exc)
        _kb._log.error(
            "kanban: the above-tranche repair pass FAILED on board %s; the tick continues and "
            "dispatches from a board still carrying an over-claim: %s",
            board or "-", result.priority_demote_error,
        )
    result.priority_demoted = [
        (row["task_id"], row["was"], row["now"])
        for row in demoted if row.get("now") is not None
    ]
    result.priority_demote_refused = [
        "%s (%s -> %s): %s" % (row["task_id"], row["was"], row["now"], row.get("error"))
        for row in demoted if row.get("error")
    ]


def _tick_spawn_budget(
    conn: sqlite3.Connection,
    result: DispatchResult,
    *,
    max_spawn: Optional[int],
    max_in_progress: Optional[int],
    board: Optional[str],
    host_budget_share: Optional[int] = None,
) -> tuple[bool, Optional[int]]:
    """``(may_spawn, spawn_budget)`` for this tick; ``budget None`` = uncapped.

    ``max_spawn`` is a live per-board concurrency cap (running + this tick's
    spawns), not a per-tick budget — a per-tick reading would grow concurrency
    by N every tick. ``max_in_progress`` is a HOST-level cap: running workers on
    every other board count against the same budget, else N boards multiply the
    cap by N — exactly the fan-out the memory-derived default exists to prevent.
    ``host_budget_share`` is this board's slice of the FREE host slots for this
    tick, allocated across the boards by the gateway dispatcher
    (``gateway.kanban_watchers_dispatcher.host_budget_shares``): the host cap
    still bounds the fleet, and the share stops one board's queue from taking
    the slots every other board is waiting for. ``None`` = no share was
    allocated, which is every single-board caller (the CLI daemon and
    ``hermes kanban dispatch``).
    """
    # Count already-running tasks so max_spawn enforces concurrency, not a
    # per-tick budget: "running" tasks stay running until the worker makes a terminal
    # board call (kanban_complete/kanban_block/kanban_request_review) or the TTL reclaims them.
    running_count = 0
    spawn_budget: Optional[int] = None
    if max_spawn is not None or max_in_progress is not None:
        running_count = count_running_tasks(conn)

    # Both ready and review loops consume from the same budget.
    if max_spawn is not None:
        if running_count >= max_spawn:
            # The board is at its OWN ceiling (its per-board value, or the
            # global max_spawn). Nothing starts until it drains, and from the
            # outside the queue looks idle — name what the ceiling held back so
            # the tick is reportable, never a silent no-op.
            result.deferred_board_capped = spawnable_pending_ids(conn)
            return False, None
        spawn_budget = max_spawn - running_count

    if max_in_progress is not None:
        total_running = running_count + count_running_tasks_other_boards(board)
        if total_running >= max_in_progress:
            # The HOST budget is full. This board's refusal is not its own doing
            # and its queue looks idle from the outside, so name what the cap
            # deferred: the tick has to be reportable (HostCapStarvationClock).
            result.deferred_host_capped = spawnable_pending_ids(conn)
            return False, None
        remaining = max_in_progress - total_running
        if spawn_budget is None or spawn_budget > remaining:
            spawn_budget = remaining

    # This board's SHARE of the host budget, handed out by the gateway
    # dispatcher. 0 means more boards had startable work than there were free
    # slots, so this board waits a tick: a rotation, not a fault, and recorded
    # the same way so a board that keeps losing the race is still visible.
    if host_budget_share is not None:
        share = max(int(host_budget_share), 0)
        if share <= 0:
            result.deferred_host_capped = spawnable_pending_ids(conn)
            return False, None
        if spawn_budget is None or spawn_budget > share:
            spawn_budget = share

    # Memory-pressure guard: a static cap can't see the host's actual state.
    # critical -> spawn nothing this tick; elevated -> at most one new worker.
    # Reclaim/promotion already ran, so bookkeeping stays live; deferred tasks
    # wait for a later tick. "unknown" imposes no restriction.
    pressure = _memory_pressure_level()
    if pressure == "critical":
        result.memory_pressure = pressure
        _kb._log.warning(
            "kanban dispatch: system memory pressure is critical; "
            "spawning no new workers this tick (deferred, not dropped)"
        )
        return False, None
    if pressure == "elevated":
        result.memory_pressure = pressure
        if spawn_budget is None or spawn_budget > 1:
            _kb._log.warning(
                "kanban dispatch: system memory pressure is elevated; "
                "limiting to at most 1 new worker this tick"
            )
            spawn_budget = 1
    return True, spawn_budget


def _lane_rows(conn: sqlite3.Connection, status: str) -> list[sqlite3.Row]:
    """Unclaimed rows of one lane in dispatch order."""
    return conn.execute(
        "SELECT id, assignee, priority FROM tasks "
        f"WHERE status = '{status}' AND claim_lock IS NULL "
        "ORDER BY priority DESC, created_at ASC"
    ).fetchall()


def _lane_fair_ready_order(
    conn: sqlite3.Connection,
    ready_rows: list[sqlite3.Row],
    *,
    budget: Optional[int],
    designated_pool_reserve: int = 1,
    per_profile_cap: Optional[int] = None,
    per_profile_running: Optional[dict[str, int]] = None,
) -> list[sqlite3.Row]:
    """Order the ready lane so no LANE starves behind another lane's priority.

    INTRA-BOARD ONLY. This never ranks one board against another: the cross-board
    rotation keeps reading ``_lane_rows`` order through ``head_of_line_priority``
    and ``spawnable_pending_ids``, which is why ``_lane_rows`` itself must not
    change.

    ``ready_rows`` is already the dispatch order (``priority DESC, created_at
    ASC``). Walking it straight hands every free slot to whichever lanes filed
    the highest cards, so a lane whose work is merely ordinarily ranked — and
    therefore always below the designated tranche (``>= TRANCHE_FLOOR``) — never
    starts while a designated lane has a queue. This orders the SAME rows in
    three passes instead:

    * **designated reach** — one slot per lane whose head card is designated, in
      least-recently-run order, capped by ``max(budget - designated_pool_reserve,
      0)`` so the tranche cannot hold every slot while ordinary work waits;
    * **lane reach** — one slot per lane not served by the first pass, same
      order; a lane whose head is a designated card already refused by the
      ceiling is still reached through the first row of it the room test
      admits, so the lane (not the head) is what a reach pass serves;
    * **fill** — the remaining budget in global priority order.

    Lanes are served least-recently-run first: a lane with no ``task_runs`` row
    at all sorts BEFORE every lane that has one, then the oldest
    ``started_at`` wins. Ties keep the lane's first-seen (priority) order, so
    the result is deterministic. WITHIN a lane the order is untouched — the
    tier still decides WHICH card of a lane runs.

    Every pass skips a row whose assignee is already at
    ``kanban.max_in_progress_per_profile``; ``per_profile_running`` already
    counts this tick's own allocations (see ``_dispatch_lane_task``), so a lane
    at its cap is not "reached" and cannot spend the slot a capped neighbour
    would waste. The designated ceiling is lifted when NO spawnable ordinary
    row waits (an all-designated board still gets the whole budget).

    Returned rows are the allocation order first, then every remaining row in
    its original order: a row the passes could not place is still tried by the
    caller, never silently dropped from the tick. ``budget None`` = uncapped —
    the reach order still applies an ordering, but nothing is held back.
    """
    if not ready_rows:
        return ready_rows
    from hermes_cli import kanban_priority_policy as policy

    tranche_floor = int(policy.TRANCHE_FLOOR)
    reserve = designated_pool_reserve if isinstance(designated_pool_reserve, int) else 1
    reserve = max(reserve, 0)
    running = dict(per_profile_running or {})

    def _lane_of(row: sqlite3.Row) -> str:
        # No assignee is its own bucket: the caller may still resolve it through
        # kanban.default_assignee, and an unassigned card must not be able to
        # consume a named lane's reach.
        return row["assignee"] or ""

    def _designated(row: sqlite3.Row) -> bool:
        return int(row["priority"] or 0) >= tranche_floor

    def _at_cap(lane: str) -> bool:
        if per_profile_cap is None or not lane:
            return False
        return running.get(lane, 0) >= per_profile_cap

    # Lanes in lane-local order: ready_rows is priority DESC, created_at ASC, so
    # the first row seen for a lane is that lane's head.
    lanes: dict[str, list[sqlite3.Row]] = {}
    for row in ready_rows:
        lanes.setdefault(_lane_of(row), []).append(row)

    # ONE read-only query: the newest run start per lane. A lane that never ran
    # has no row and ranks before every lane that has.
    last_run: dict[str, int] = {}
    for r in conn.execute(
        "SELECT profile, MAX(started_at) AS last FROM task_runs "
        "WHERE profile IS NOT NULL GROUP BY profile"
    ):
        last_run[str(r["profile"])] = int(r["last"] or 0)

    def _lru_key(lane: str) -> tuple[int, int]:
        if lane in last_run:
            return (1, last_run[lane])
        return (0, 0)

    lru_lanes = sorted(lanes, key=_lru_key)  # stable: ties keep priority order
    designated_lanes = [lane for lane in lru_lanes if _designated(lanes[lane][0])]

    # The ceiling only exists while a spawnable ORDINARY row waits: if every
    # waiting row is designated (or the ordinary ones are all capped), holding a
    # slot back would idle it for nobody.
    ordinary_waiting = any(
        not _designated(row) and not _at_cap(_lane_of(row)) for row in ready_rows
    )
    designated_cap: Optional[int] = None if budget is None else budget
    if budget is not None and ordinary_waiting:
        designated_cap = max(budget - reserve, 0)

    ordered: list[sqlite3.Row] = []
    placed: set[str] = set()
    served: set[str] = set()
    spent = 0
    designated_spent = 0

    def _room(row: sqlite3.Row) -> bool:
        if budget is not None and spent >= budget:
            return False
        if _at_cap(_lane_of(row)):
            return False
        if designated_cap is not None and _designated(row) and designated_spent >= designated_cap:
            return False
        return True

    def _take(row: sqlite3.Row) -> None:
        nonlocal spent, designated_spent
        lane = _lane_of(row)
        ordered.append(row)
        placed.add(row["id"])
        served.add(lane)
        spent += 1
        if per_profile_cap is not None and lane:
            running[lane] = running.get(lane, 0) + 1
        if _designated(row):
            designated_spent += 1

    def _has_budget() -> bool:
        return budget is None or spent < budget

    # Pass 1 — designated reach: one slot per designated lane, LRU first.
    for lane in designated_lanes:
        if not _has_budget():
            break
        head = lanes[lane][0]
        if _room(head):
            _take(head)
    # Pass 2 — lane reach: one slot per lane pass 1 did not serve. A lane's
    # HEAD can be refused here without the lane being skipped: when a designated
    # head's ceiling slot is already spent but the lane also holds ordinary work,
    # reach the lane through the first row of it that ``_room`` admits. Testing
    # only the head would drop the lane for the whole reach phase and hand the
    # slot back to a lane already served — two lanes with designated heads and
    # ordinary work waiting would then never both be reached at budget 2
    # (ruling t_b2865b89 P-Reach, §7.2).
    for lane in lru_lanes:
        if not _has_budget():
            break
        if lane in served:
            continue
        for row in lanes[lane]:
            if _room(row):
                _take(row)
                break
    # Pass 3 — fill: the rest of the budget in global priority order.
    for row in ready_rows:
        if not _has_budget():
            break
        if row["id"] in placed:
            continue
        if _room(row):
            _take(row)

    ordered.extend(row for row in ready_rows if row["id"] not in placed)
    return ordered


def _spawnable_lane_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Ready (and, when enabled, review) rows a worker could start, head first.

    "Spawnable" is the dispatcher's own gate as far as a read-only pass can
    apply it: review rows only when review dispatch is on, and never a card
    without an assignee or assigned to a profile this host cannot spawn — those
    wait for ROUTING, not for a free slot.
    """
    rows = _lane_rows(conn, "ready")
    if review_dispatch_enabled():
        rows = rows + _lane_rows(conn, "review")
    profile_exists = _profile_exists_fn()
    if profile_exists is None:
        return [row for row in rows if row["assignee"]]
    return [row for row in rows if row["assignee"] and profile_exists(row["assignee"])]


def spawnable_pending_ids(conn: sqlite3.Connection) -> list[str]:
    """Ids of this board's spawnable cards waiting for a worker, head of line first.

    Everything named here is something a free slot would really have started,
    which is what makes a host-cap deferral reportable: "6 cards were ready and
    nothing could start" is actionable, "the queue is non-empty" is not.
    """
    return [row["id"] for row in _spawnable_lane_rows(conn)]


def spawnable_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """This board's spawnable cards (``id``/``assignee``/``priority``), head first.

    The gateway dispatcher allocates the host budget by ranking EVERY card on
    EVERY board (``gateway.kanban_watchers_dispatcher.host_budget_shares_by_priority``),
    so it needs the board's own ``priority DESC, created_at ASC`` row order — the
    same order :func:`spawnable_pending_ids` projects to ids. Read-only.
    """
    return _spawnable_lane_rows(conn)


def head_of_line_priority(conn: sqlite3.Connection) -> Optional[int]:
    """Priority of the card this board would spawn next, or ``None``.

    This lineage has no age-aware effective priority: ``_lane_rows`` orders a
    lane by ``priority DESC, created_at ASC``, so the head card's own priority
    IS the priority of the work this board would start next — ranking boards by
    it ranks them by exactly the order their own cards will be spawned in.

    ``None`` means nothing on this board could be started by a worker. A caller
    allocating a SHARED budget must still visit such a board (reclaim,
    promotion, decomposition and health work is board-local); it just cannot
    rank it.
    """
    rows = _spawnable_lane_rows(conn)
    if not rows:
        return None
    return int(rows[0]["priority"])


def _current_board_label() -> str:
    """The board a board-less tick resolves to (standalone daemon), never raising."""
    try:
        return str(_kb.get_current_board() or _kb.DEFAULT_BOARD)
    except Exception:
        return _kb.DEFAULT_BOARD


def total_running_all_boards() -> Optional[int]:
    """Running tasks across EVERY board — the host's whole occupancy.

    This is the number a caller subtracts from the host cap to find the free
    slots, so a partial answer is worse than none: ``None`` means at least one
    board could not be read and the caller must fall back to its previous
    behaviour instead of handing out slots it cannot prove are free. Boards are
    matched by resolved DB path, so ``HERMES_KANBAN_DB`` (every board pinned to
    one file) is counted once, exactly like ``count_running_tasks_other_boards``.
    """
    try:
        boards = _kb.list_boards(include_archived=False)
    except Exception:
        return None
    seen: set[str] = set()
    total = 0
    for meta in boards:
        slug = meta.get("slug") or _kb.DEFAULT_BOARD
        try:
            path = _kb.kanban_db_path(board=slug).expanduser()
            resolved = str(path.resolve())
            if resolved in seen or not path.exists():
                continue
            seen.add(resolved)
            conn = _kbc.connect(board=slug)
            try:
                total += count_running_tasks(conn)
            finally:
                with contextlib.suppress(Exception):
                    conn.close()
        except Exception:
            # Unknown occupancy: refuse to allocate rather than under-count.
            return None
    return total


HEALTH_HOST_CAP_DEFER_SECONDS = 3600.0
"""How long ONE board may stay deferred by the host budget before it is reported."""


def host_cap_deferral_message(
    board: Optional[str], task_id: str, age_seconds: float, waiting: int
) -> str:
    """Operator-facing line for a board that keeps losing the host budget."""
    minutes = int(max(age_seconds, 0.0) // 60)
    where = f"kanban dispatcher[{board}]" if board else "kanban dispatcher"
    return (
        f"{where}: board {board or '?'} has been deferred by the host-wide "
        f"kanban.max_in_progress budget for {minutes}m — {waiting} card(s) waiting, "
        f"head of line {task_id}. Another board holds the slots that this board's "
        f"queue is waiting for: raise kanban.max_in_progress, or accept the wait."
    )


class HostCapStarvationClock:
    """Ages host-budget deferral per board and names the starvation.

    ``kanban.max_in_progress`` is HOST-wide: every board draws from one budget,
    so a board that loses the race has no refusal of its own, no error, and
    nothing in its own DB — it looks exactly like an idle board. This clock
    turns that tick-level fact into a line the operator can act on once the
    deferral stops being transient (``defer_seconds``, default one hour).

    Per BOARD, not per card: the head of line rotates, and a clock that reset
    with it would never fire on a board starved for hours. The clock runs while
    a board keeps appearing in a deferred tick and restarts as soon as the board
    spawns or is not deferred, because an old deferral is not evidence of a
    current problem. A tick that records the cap without naming a card (nothing
    spawnable) is left alone: unproven starvation is not a warning.
    """

    def __init__(self, defer_seconds: float = HEALTH_HOST_CAP_DEFER_SECONDS) -> None:
        self.defer_seconds = float(defer_seconds)
        self._since: dict[str, float] = {}

    def reset(self) -> None:
        """Forget every clock (a paused dispatcher defers nothing)."""
        self._since.clear()

    def observe(self, board_results, now: Optional[float] = None) -> Optional[str]:
        """The one starvation line that is due, or ``None``. Never raises."""
        at = time.time() if now is None else float(now)
        waiting: dict[str, int] = {}
        heads: dict[str, str] = {}
        for slug, result in board_results or ():
            deferred = list(getattr(result, "deferred_host_capped", None) or [])
            if not deferred:
                continue
            key = str(slug or "?")
            waiting[key] = len(deferred)
            heads.setdefault(key, str(deferred[0]))
            self._since.setdefault(key, at)
        for key in [k for k in self._since if k not in waiting]:
            self._since.pop(key, None)
        overdue = [
            (key, at - since)
            for key, since in self._since.items()
            if at - since > self.defer_seconds
        ]
        if not overdue:
            return None
        key, age = max(overdue, key=lambda entry: entry[1])
        return host_cap_deferral_message(key, heads.get(key, "?"), age, waiting.get(key, 0))


_ESTOP_IMPORT_WARNED = False


def _estop_module():
    """``agent.estop``, or None when it cannot be imported — logged once, FAIL-OPEN.

    Deliberately fail-open: an import error in the gate must never halt the fleet's dispatch
    (the shipped cron/turn readers take the same view), but it must never be silent either —
    the operator has to know the gate is not in force.
    """
    global _ESTOP_IMPORT_WARNED
    try:
        from agent import estop  # noqa: PLC0415 - optional-runtime import, guarded by design
        return estop
    except Exception as exc:  # pragma: no cover - import guard
        if not _ESTOP_IMPORT_WARNED:
            _ESTOP_IMPORT_WARNED = True
            logger.error(
                "kanban dispatch: agent.estop is unavailable (%s) — the lane-scoped DEFCON "
                "gate is NOT in force this tick", exc,
            )
        return None


def _estop_state_for_tick() -> Optional[Any]:
    """The tick's ONE estop read, or None when the gate is unavailable (fail-open).

    Read once and threaded down through every lane decision so one tick can never see two
    different modes (a re-arm landing mid-tick must not half-gate the board).
    """
    estop = _estop_module()
    if estop is None:
        return None
    try:
        return estop.read_state()
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("kanban dispatch: could not read the ESTOP sentinel (%s)", exc)
        return None


def _lane_admitted(estop_state: Optional[Any], assignee: str, board: Optional[str]) -> bool:
    """May this LANE start work under the tick's estop state? True when no gate is in force."""
    if estop_state is None:
        return True
    estop = _estop_module()
    if estop is None:
        return True
    return bool(estop.work_admitted(assignee, board=board, state=estop_state))


def _any_spawnable_review(
    conn: sqlite3.Connection,
    review_rows: list[sqlite3.Row],
    *,
    per_profile_cap: Optional[int] = None,
    per_profile_running: Optional[dict[str, int]] = None,
    estop_state: Optional[Any] = None,
) -> bool:
    """Mirror review dispatch gates before reserving ready-lane capacity.

    Unavailable profile metadata retains the historic fail-open behavior. A
    review row that :func:`_dispatch_lane_task` would refuse this tick — its
    assignee already at the per-profile cap, respawn-guarded, held by a
    lane-scoped DEFCON lockdown, or the card's own implementer — cannot consume
    the reservation, so it must not withhold capacity from an otherwise ready
    task (one such row would pin ``ready_budget`` to 0).
    """
    if not review_rows:
        return False
    profile_exists = _profile_exists_fn()
    running = per_profile_running or {}
    for row in review_rows:
        assignee = row["assignee"]
        if not assignee:
            continue
        if profile_exists is not None and not profile_exists(assignee):
            continue
        # Board is deliberately None: it is not a term in the admission predicate, the LANE is.
        if not _lane_admitted(estop_state, assignee, None):
            continue
        if _self_review_reason(conn, row["id"], assignee) is not None:
            continue
        if per_profile_cap is not None and running.get(assignee, 0) >= per_profile_cap:
            continue
        if check_respawn_guard(conn, row["id"], lane="review") is None:
            return True
    return False


def _resolve_default_assignee(default_assignee: Optional[str]) -> Optional[str]:
    """``kanban.default_assignee`` when it names a real profile this home may
    claim (``kanban.dispatch_profiles`` gated, same predicate as the spawn
    gate). Otherwise ``None`` so an unassigned shared-board card is never
    written to. When the profiles module isn't importable trust the
    operator's config: the downstream check still buckets a missing profile
    as nonspawnable."""
    name = (default_assignee or "").strip() or None
    if name:
        profile_exists = _profile_exists_fn()
        if profile_exists is not None and not profile_exists(name):
            return None
    return name


def _record_lockdown_events(
    conn: sqlite3.Connection,
    result: DispatchResult,
    estop_state: Optional[Any],
    *,
    dry_run: bool,
    board: Optional[str],
) -> None:
    """Record ONE ``skipped_lockdown`` event per held card per ENGAGEMENT.

    Visibility is the requirement: a card a lane-scoped stop refused must say so ON THE CARD,
    naming the profile, because a silent non-spawn is the defect this replaces. The dispatcher
    refuses the same card on every tick, so the dedupe key is the sentinel's engagement
    identity (``mtime_ns:size`` of the body): a re-arm earns a fresh row, a long hold does not
    become a firehose. The newest such event per card is read in ONE query for the whole tick,
    never one query per held card.

    ``dry_run`` writes nothing — the bucket still fills, so a dry run reports the same
    decision without touching the board.
    """
    if dry_run or estop_state is None or not result.skipped_lockdown:
        return
    estop = _estop_module()
    if estop is None:
        return
    task_ids = sorted({task_id for task_id, _profile in result.skipped_lockdown})
    placeholders = ", ".join("?" for _ in task_ids)
    newest: dict[str, dict] = {}
    # ASC + overwrite keeps the NEWEST row per card in one pass over the whole tick's set.
    for row in conn.execute(
        "SELECT task_id, payload FROM task_events "
        f"WHERE kind = 'skipped_lockdown' AND task_id IN ({placeholders}) "
        "ORDER BY id ASC",
        task_ids,
    ):
        data = _kb._json_or(row["payload"], {})
        if isinstance(data, dict):
            newest[row["task_id"]] = data
    engagement = estop.engagement_key()
    reasons = [hold.get("reason") for hold in estop_state.holds if hold.get("reason")]
    common = {
        "engagement": engagement,
        "mode": estop_state.mode,
        "board": board,
        "owners": sorted(estop_state.owners),
        "allow_profiles": sorted(estop_state.allow_profiles),
        "reason": reasons[-1] if reasons else None,
    }
    with _kb.write_txn(conn):
        for task_id, profile in result.skipped_lockdown:
            last = newest.get(task_id)
            if (
                isinstance(last, dict)
                and last.get("engagement") == engagement
                and last.get("profile") == profile
            ):
                continue
            _kb._append_event(
                conn, task_id, "skipped_lockdown", dict(common, profile=profile),
            )


def _note_lockdown_hold(result: DispatchResult, task_id: str, assignee: str) -> None:
    """Note one held ``(card, lane)`` — once per tick, whatever seam saw it.

    Two seams report a hold (the pre-budget sweep and the lane scan) and both see the SAME
    card, so the bucket is a SET: :func:`describe_suppression` counts entries, and a
    duplicate would double-count one hold in the starvation line.
    """
    entry = (task_id, assignee)
    if entry not in result.skipped_lockdown:
        result.skipped_lockdown.append(entry)


def _lockdown_sweep(
    conn: sqlite3.Connection,
    result: DispatchResult,
    estop_state: Optional[Any],
    *,
    dry_run: bool = False,
    board: Optional[str] = None,
) -> None:
    """Record EVERY ready/review card a lane-scoped gate holds, whatever the budget does.

    The scan in :func:`_dispatch_lane_task` records a hold only for the candidates THIS tick
    actually visits, so a held card is silently unrecorded whenever no candidate exists at
    all: the board or host concurrency cap, memory pressure, the ready lane's review
    reservation (``ready_budget = 0``), or a budget a higher-ranked admitted card consumed
    first. The arm banner promises the opposite (every other lane's cards and jobs are HELD
    and recorded, never silently skipped), and the probe that reads the record runs a single
    tick — the very tick whose budget can swallow the scan.

    So the record is a property of the HOLD, not of the scan position: enumerate the lane,
    ask the SAME admission predicate, write through :func:`_record_lockdown_events`. Called
    after the reclaim phase (a row promoted to ``ready`` by this tick is swept too) and
    before :func:`_tick_spawn_budget`, i.e. before every early return. No gate in force, no
    query; ``dry_run`` fills the same bucket and writes no row.

    ``review`` rows are swept even when review dispatch is disabled: the LANE is still not
    admitted, and the record must not hinge on an unrelated config flag.
    """
    if estop_state is None:
        return
    # Same precedence as the scan: a lane that is not a real profile is bucketed
    # ``skipped_nonspawnable`` there, so it must never also count as lockdown-held.
    profile_exists = _profile_exists_fn()
    for status in ("ready", "review"):
        for row in _lane_rows(conn, status):
            assignee = row["assignee"]
            if not assignee or _lane_admitted(estop_state, assignee, board):
                continue
            if profile_exists is not None and not profile_exists(assignee):
                continue
            _note_lockdown_hold(result, row["id"], assignee)
    _record_lockdown_events(conn, result, estop_state, dry_run=dry_run, board=board)


# The dispatch lock has been released here. Fire the tick observer strictly OUTSIDE the single-writer
# critical section (#56066 sweeper finding / #64231 disposition): a slow subscriber must never extend the
# lock hold and stall a sibling dispatcher's tick.
def _dispatch_once_locked(
    conn: sqlite3.Connection,
    *,
    spawn_fn=None,
    ttl_seconds: Optional[int] = None,
    dry_run: bool = False,
    max_spawn: Optional[int] = None,
    max_in_progress: Optional[int] = None,
    failure_limit: int = DEFAULT_FAILURE_LIMIT,
    stale_timeout_seconds: int = 0,
    board: Optional[str] = None,
    default_assignee: Optional[str] = None,
    max_in_progress_per_profile: Optional[int] = None,
    reconcile_orphans: bool = True,
    host_budget_share: Optional[int] = None,
    lane_fair_spawn: bool = True,
    designated_pool_reserve: int = 1,
    default_max_runtime_seconds: Optional[int] = None,
) -> DispatchResult:
    """One dispatcher tick: reclaim stale/crashed running tasks, promote
    todo -> ready, then atomically claim each spawnable ready/review row and
    call ``spawn_fn(task, workspace_path, board) -> Optional[int]``, recording
    the PID so later ticks catch crashes before the TTL. Cap semantics:
    :func:`_tick_spawn_budget`. ``lane_fair_spawn`` /
    ``designated_pool_reserve`` shape the READY lane's order only:
    :func:`_lane_fair_ready_order`. ``default_max_runtime_seconds`` (None =
    resolve ``kanban.default_max_runtime_seconds`` now) is stamped onto a card
    that carries no cap at claim time; ``0`` disables the stamp."""
    if default_max_runtime_seconds is None:
        default_max_runtime_seconds = default_max_runtime_seconds_config()
    result = DispatchResult()
    # The lane gate for THIS tick: one read, threaded into every decision below.
    estop_state = _estop_state_for_tick()
    _run_reclaim_phase(
        conn, result, stale_timeout_seconds=stale_timeout_seconds,
        failure_limit=failure_limit, reconcile_orphans=reconcile_orphans, board=board,
    )
    # Record EVERY held card BEFORE the budget can return early, so a tick that spawns
    # nothing (cap, memory pressure, review reservation, budget consumed) still says why
    # per card. After the reclaim phase, so a row promoted this tick is swept too.
    _lockdown_sweep(conn, result, estop_state, dry_run=dry_run, board=board)
    may_spawn, spawn_budget = _tick_spawn_budget(
        conn, result, max_spawn=max_spawn, max_in_progress=max_in_progress, board=board,
        host_budget_share=host_budget_share,
    )
    if not may_spawn:
        return result

    ready_rows = _lane_rows(conn, "ready")
    # Review rows are enumerated up front so the budget split can see whether
    # review work exists at all.
    review_rows = _lane_rows(conn, "review") if review_dispatch_enabled() else []
    if review_rows:
        # Boot-time self-check (once per process, cached): a review-capable profile that cannot
        # resolve an injected review skill must be visible BEFORE its cards discover it through two
        # crashed runs. Cheap and read-only; the per-spawn gate in _dispatch_lane_task is the
        # backstop.
        check_review_skill_readiness_once()
    # Per-profile cap. Deferred tasks go to skipped_per_profile_capped, not
    # skipped_unassigned — "busy, retry later" differs from "needs routing".
    # Resolved BEFORE the review reservation so the reservation can see which
    # review rows the lane loop would refuse this tick.
    per_profile_cap = max_in_progress_per_profile if (
        # Per-profile concurrency cap (#21582): when set, track how many workers each assignee already has
        # in flight, and refuse to spawn when this would push that assignee past the cap. Prevents fan-out
        # workloads from melting a single profile's local model / API quota / browser pool while leaving
        # other profiles idle.
        isinstance(max_in_progress_per_profile, int)
        and max_in_progress_per_profile > 0
    ) else None
    per_profile_running: dict[str, int] = {}
    if per_profile_cap is not None:
        for prow in conn.execute(
            "SELECT assignee, COUNT(*) AS n FROM tasks "
            "WHERE status = 'running' AND assignee IS NOT NULL "
            "GROUP BY assignee"
        ):
            per_profile_running[prow["assignee"]] = int(prow["n"])
    # Review-lane reservation: the ready loop runs first and would otherwise
    # consume the ENTIRE shared budget, starving reviews under a sustained ready
    # backlog. When spawnable review work exists and there is any budget, hold
    # one slot back.
    ready_budget = spawn_budget
    if spawn_budget is not None and spawn_budget > 0 and _any_spawnable_review(
        conn, review_rows,
        per_profile_cap=per_profile_cap, per_profile_running=per_profile_running,
        estop_state=estop_state,
    ):
        ready_budget = max(spawn_budget - 1, 0)
    lane_kwargs: dict[str, Any] = dict(
        dry_run=dry_run, ttl_seconds=ttl_seconds, board=board,
        failure_limit=failure_limit, spawn_fn=spawn_fn,
        per_profile_cap=per_profile_cap, per_profile_running=per_profile_running,
        estop_state=estop_state,
        default_max_runtime_seconds=default_max_runtime_seconds,
    )
    default_assignee = _resolve_default_assignee(default_assignee)
    # Fair lane ordering (ruling t_b2865b89): the fairness passes only REORDER the
    # ready rows — the budget, the per-profile cap and every other guard still
    # apply in the loop below. Knob off returns ``ready_rows`` untouched, so the
    # tick is byte-identical to the straight priority walk.
    if lane_fair_spawn:
        ready_order = _lane_fair_ready_order(
            conn, ready_rows,
            budget=ready_budget,
            designated_pool_reserve=designated_pool_reserve,
            per_profile_cap=per_profile_cap,
            per_profile_running=per_profile_running,
        )
    else:
        ready_order = ready_rows
    spawned = 0
    for row in ready_order:
        if ready_budget is not None and spawned >= ready_budget:
            break
        row_assignee = row["assignee"]
        if not row_assignee:
            # Honour kanban.default_assignee so an unassigned task doesn't
            # park in 'ready' forever.
            if not default_assignee or not _apply_default_assignee(
                conn, row["id"], default_assignee, dry_run=dry_run,
            ):
                result.skipped_unassigned.append(row["id"])
                continue
            row_assignee = default_assignee
            result.auto_assigned_default.append(row["id"])
        if _dispatch_lane_task(conn, row, row_assignee, result, lane="ready", **lane_kwargs):
            spawned += 1

    # A review agent (sdlc-review) approves (→ done) or requests changes
    # (→ ready/todo). Review spawns share max_spawn with ready tasks. The loop
    # checks the FULL shared ``spawn_budget`` — the reservation above caps the
    # ready lane, it grants no extra capacity here.
    for row in review_rows:
        if spawn_budget is not None and spawned >= spawn_budget:
            break
        if not row["assignee"]:
            result.skipped_unassigned.append(row["id"])
            continue
        if _dispatch_lane_task(conn, row, row["assignee"], result, lane="review", **lane_kwargs):
            spawned += 1
    _record_lockdown_events(conn, result, estop_state, dry_run=dry_run, board=board)
    return result


def _positive_int(value: Any, default: int, *, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def worker_log_rotation_config(kanban_cfg: Optional[dict] = None) -> tuple[int, int]:
    """Return ``(rotate_bytes, backup_count)`` for worker log rotation.
    Defaults: rotate at 2 MiB, keep one backup (``.log.1``); both overridable
    from ``config.yaml``.
    """
    if kanban_cfg is None:
        try:
            from hermes_cli.config import load_config

            kanban_cfg = (load_config().get("kanban") or {})
        except Exception:
            kanban_cfg = {}
    kanban_cfg = kanban_cfg or {}
    max_bytes = _positive_int(kanban_cfg.get("worker_log_rotate_bytes"), DEFAULT_LOG_ROTATE_BYTES, minimum=1)
    backup_count = _positive_int(kanban_cfg.get("worker_log_backup_count"), DEFAULT_LOG_BACKUP_COUNT, minimum=0)
    return max_bytes, backup_count


def _rotated_log_path(log_path: Path, generation: int) -> Path:
    return log_path.with_suffix(log_path.suffix + f".{generation}")


def _rotate_worker_log(
    log_path: Path,
    max_bytes: int,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
) -> None:
    """Rotate ``<log>`` when it exceeds ``max_bytes``: ``<log>`` → ``<log>.1``,
    older generations shift up to ``backup_count``.
    """
    try:
        if not log_path.exists() or log_path.stat().st_size <= max_bytes:
            return
        backup_count = _positive_int(backup_count, DEFAULT_LOG_BACKUP_COUNT, minimum=0)
        if backup_count == 0:
            log_path.unlink()
            return
        oldest = _rotated_log_path(log_path, backup_count)
        with contextlib.suppress(OSError):
            if oldest.exists():
                oldest.unlink()
        for generation in range(backup_count - 1, 0, -1):
            src = _rotated_log_path(log_path, generation)
            if not src.exists():
                continue
            with contextlib.suppress(OSError):
                src.rename(_rotated_log_path(log_path, generation + 1))
        log_path.rename(_rotated_log_path(log_path, 1))
    except OSError:
        pass


def _module_hermes_argv() -> list[str]:
    """Interpreter-bound Hermes CLI invocation (``hermes_cli.main`` is the
    console-script target — there is no top-level ``hermes`` package)."""
    return [sys.executable, "-m", "hermes_cli.main"]


def _propagate_module_import_root(cmd: list[str], env: dict[str, str]) -> None:
    """Put the running install's package root on a module-form worker's path.

    ``_resolve_hermes_argv`` proves ``hermes_cli`` importable in THIS process,
    where a store-python shim has the repo root on ``sys.path`` in-process;
    the spawned child runs the bare ``sys.executable`` from the task workspace
    with a scrubbed ``PYTHONPATH`` and cannot import the package the parent
    just proved importable — it dies before any work and the board
    auto-blocks (#122299, #122487, #122500). Same-interpreter child, so the
    root is version-safe to propagate; ``hermes_cli.main``'s own bootstrap
    then owns dependency activation as usual. A resolved shim path owns its
    imports and is left alone. Same pin cron's external worker uses (#112729).
    """
    if cmd[1:3] != ["-m", "hermes_cli.main"]:
        return
    from cron.scheduler_worker_env import pin_hermes_tree_on_pythonpath

    pin_hermes_tree_on_pythonpath(env, Path(__file__).resolve().parents[1])


def _absolute_hermes_path(path: str) -> str:
    """Return an absolute filesystem path for a resolved Hermes shim."""
    expanded = os.path.expanduser(path)
    return expanded if os.path.isabs(expanded) else os.path.abspath(expanded)


def _looks_like_path(value: str) -> bool:
    """Return true when a command override is an explicit path, not a name."""
    expanded = os.path.expanduser(value)
    return (
        expanded.startswith("~")
        or os.path.isabs(expanded)
        or bool(os.path.dirname(expanded))
        or "\\" in expanded
        or bool(re.match(r"^[A-Za-z]:", expanded))
    )


def _is_windows_batch_shim(path: str) -> bool:
    """Return true for Windows shell/batch shims that should not be argv[0]."""
    return path.lower().endswith((".cmd", ".bat"))


def _path_search_names(command: str) -> list[str]:
    """Return executable names to try for an unqualified command."""
    if not _kb._IS_WINDOWS or os.path.splitext(command)[1]:
        return [command]
    raw = os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
    return [command + ext for ext in raw.split(";") if ext]


def _safe_which_no_cwd(command: str) -> Optional[str]:
    """Resolve a bare command from PATH without implicit current-dir search.

    On Windows ``shutil.which`` may search the current directory before PATH
    for bare names — unsafe for a dispatcher. Only explicit PATH entries are
    considered; empty / ``.`` entries are skipped.
    """
    for raw_dir in os.environ.get("PATH", "").split(os.pathsep):
        if not raw_dir or raw_dir == ".":
            continue
        directory = os.path.expanduser(raw_dir)
        for name in _path_search_names(command):
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate) and (_kb._IS_WINDOWS or os.access(candidate, os.X_OK)):
                return candidate
    return None


def _hermes_path_argv(path: str) -> list[str]:
    """argv for a resolved Hermes executable path. Windows batch shims
    (``.cmd``/``.bat``) are unsafe as argv[0] because the argument vector
    includes task-derived values; prefer the module form."""
    if _kb._IS_WINDOWS and _is_windows_batch_shim(path):
        return _module_hermes_argv()
    return [_absolute_hermes_path(path)]


def _resolve_hermes_argv() -> list[str]:
    """Resolve the ``hermes`` invocation as argv for ``Popen``: ``$HERMES_BIN``
    (path-like -> absolute; bare names keep PATH semantics, never a
    same-directory file), then the running interpreter's ``sys.executable -m
    hermes_cli.main`` (exactly this install; also covers shim-less cron,
    systemd ``User=``, launchd), then ``which("hermes")`` (Windows: safe PATH
    search, batch shims fall back to the module form) only when ``hermes_cli``
    is not importable. The module argv must win over PATH: a PATH-first lookup
    lets an attacker-planted ``hermes`` shadow the running install (#111569).
    Mirrors ``gateway.run._resolve_hermes_bin``; local because ``hermes_cli``
    sits below ``gateway`` in the dependency order.
    """
    import importlib.util
    import shutil

    env_bin = os.environ.get("HERMES_BIN", "").strip()
    if env_bin:
        if _looks_like_path(env_bin):
            return _hermes_path_argv(env_bin)
        resolved_env_bin = _safe_which_no_cwd(env_bin)
        if resolved_env_bin:
            return _hermes_path_argv(resolved_env_bin)
        return _module_hermes_argv()

    try:
        if importlib.util.find_spec("hermes_cli") is not None:
            return _module_hermes_argv()
    except Exception:
        pass

    hermes_bin = _safe_which_no_cwd("hermes") if _kb._IS_WINDOWS else shutil.which("hermes")
    if hermes_bin:
        return _hermes_path_argv(hermes_bin)
    return _module_hermes_argv()


def _worker_terminal_timeout_env(
    explicit_max_runtime_seconds: Optional[int],
    current_timeout: Optional[str],
) -> Optional[str]:
    """Return a worker-scoped TERMINAL_TIMEOUT override, if needed.

    Takes the card's EXPLICIT ``max_runtime_seconds`` — the budget its author set — and NEVER the
    dispatcher's ``kanban.default_max_runtime_seconds``: that default is a scheduling device stamped
    onto a card at claim so the runtime sweep can reap the slot, not a budget anyone authored, and
    letting it through here would silently raise every worker's terminal command timeout from the
    generic default to ``default - 30 s``. When the explicit cap exceeds the terminal tool's default
    timeout, raise only the child's default so a long command isn't killed by the generic one first.
    """
    if explicit_max_runtime_seconds is None:
        return None
    try:
        runtime = int(explicit_max_runtime_seconds)
    except (TypeError, ValueError):
        return None
    if runtime <= 0:
        return None

    desired = max(1, runtime - KANBAN_TERMINAL_TIMEOUT_GRACE_SECONDS)
    try:
        existing = int(str(current_timeout).strip()) if current_timeout else 0
    except (TypeError, ValueError):
        existing = 0
    if existing >= desired:
        return None
    return str(desired)


@contextlib.contextmanager
def _worker_profile_scope(hermes_home: str, *, bind_home: bool = True):
    """Bind an assigned profile's runtime scope (secrets + terminal policy, optionally home) for
    one dispatch-side read or spawn-env build.

    The dispatcher runs detached from any turn, so nothing binds a profile for it: ``load_config``,
    the toolset probes' ``get_secret`` reads and ``build_subprocess_env``'s passthrough resolution
    all fall back to the LAUNCH profile's ambient ``os.environ`` / ``TERMINAL_*``. Binding was
    previously conditional on ``is_multiplex_active()``, so on a single-profile host a worker for
    profile B was built entirely from the dispatcher's own environment.

    ``bind_home=False`` for the spawn-env build: which variables may cross into a child is the
    DISPATCHER's ``terminal.env_passthrough`` policy (#109494, read through the home override) —
    only their VALUES come from the assignee's scope, so that branch binds the secret scope alone.
    Toolset resolution binds the home and the terminal policy, as it always has.

    The secret mapping is never widened: a profile that is not this process's own home gets its own
    ``.env`` + external sources ONLY, while the launch home keeps its established
    env-over-``.env`` precedence (``launch_secret_scope``) so systemd / ``op run`` injection still
    resolves for a standalone dispatcher.
    """
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    from hermes_constants import get_process_hermes_home, reset_hermes_home_override, set_hermes_home_override
    from tools.terminal_scope import install_profile_terminal_scope, reset_terminal_scope
    from tui_gateway.launch_profile_policy import launch_secret_scope, launch_terminal_env

    home = Path(hermes_home)
    is_launch_home = str(home.resolve()) == str(Path(get_process_hermes_home()).resolve())
    home_token = secret_token = terminal_token = None
    try:
        home_token = set_hermes_home_override(str(home)) if bind_home else None
        secret_token = set_secret_scope(
            launch_secret_scope(home) if is_launch_home else build_profile_secret_scope(home),
            profile_home=None if is_launch_home else str(home))
        terminal_token = install_profile_terminal_scope(
            home, env_overlay=launch_terminal_env() if is_launch_home else None) if bind_home else None
        yield
    finally:
        if terminal_token is not None:
            reset_terminal_scope(terminal_token)
        if secret_token is not None:
            reset_secret_scope(secret_token)
        if home_token is not None:
            reset_hermes_home_override(home_token)


def _resolve_worker_cli_toolsets(hermes_home: Optional[str]) -> Optional[list[str]]:
    """Return the assigned profile's effective CLI toolsets for a worker.

    Resolved at dispatch time and passed as an explicit ``--toolsets`` pin so
    worker startup cannot fall back to a stale root/active-profile config or a
    profile whose top-level ``toolsets`` is only the kanban orchestrator
    surface. ``model_tools`` still appends the task-scoped kanban lifecycle
    tools when ``HERMES_KANBAN_TASK`` is set.
    """
    if not hermes_home:
        return None
    try:
        from hermes_cli.config import load_config
        from hermes_cli.tools_config import _get_platform_tools

        with _worker_profile_scope(hermes_home):
            cfg = load_config()
            toolsets = sorted(_get_platform_tools(cfg, "cli"))
        return toolsets or None
    except Exception as exc:
        _kb._log.debug(
            "kanban worker: could not resolve CLI toolsets for HERMES_HOME=%r (%s)",
            hermes_home,
            exc,
        )
        return None


_retagged_workspace_roots: set[str] = set()


def _retag_legacy_worker_sessions(workspaces_root_path: str) -> None:
    """Reclaim pre-tag worker rows in state.db so they leave the session lists.

    Best-effort: the durable gate is ``state_meta`` in
    ``retag_kanban_worker_sessions``; the in-process set avoids reopening
    state.db on every spawn. A tick must never fail because a session DB was
    busy or missing.
    """
    if workspaces_root_path in _retagged_workspace_roots:
        return
    try:
        from hermes_state_registry import acquire, release_or_close

        # Inside the gateway the dispatcher shares the process's registry handle; a bare
        # SessionDB() here was one more writer connection on the same state.db (#100896).
        db = acquire()
        try:
            db.retag_kanban_worker_sessions(workspaces_root_path)
        finally:
            release_or_close(db)
        _retagged_workspace_roots.add(workspaces_root_path)
    except Exception as exc:
        _kb._log.debug("kanban worker: legacy session retag skipped (%s)", exc)


def _worker_argv(task: Task, profile_arg: str, hermes_home: Optional[str]) -> list[str]:
    """Build the ``hermes -p <profile> --cli ... chat -q ...`` worker command."""
    cmd = [
        *_resolve_hermes_argv(),
        "-p", profile_arg,
        # A worker must NEVER boot the interactive TUI: its no-TTY bail-out
        # exits 0 without doing the task → "protocol violation" every attempt.
        "--cli",
        # Workers run under a profile-scoped HERMES_HOME and so see that
        # profile's shell-hook allowlist; pass --accept-hooks explicitly so
        # configured hooks still register.
        "--accept-hooks",
    ]
    # One `--skills X` pair per name: easier to read in `ps` and avoids quoting
    # ambiguity if a skill name contains unusual chars.
    for sk in task.skills or ():
        if sk:
            cmd.extend(["--skills", sk])
    if task.model_override:
        cmd.extend(["-m", task.model_override])
        # Pin the provider too so the worker resolves the model against the
        # intended backend (model X with provider Y is the classic board-stall).
        if task.provider_override:
            cmd.extend(["--provider", task.provider_override])
    # Independent of the model override — a task can run the profile's own
    # model at a different depth.
    if task.reasoning_effort:
        cmd.extend(["--reasoning", task.reasoning_effort])
    worker_toolsets = _resolve_worker_cli_toolsets(hermes_home)
    if worker_toolsets:
        cmd.extend(["--toolsets", ",".join(worker_toolsets)])
    cmd.extend(["chat", "-q", f"work kanban task {task.id}"])
    # goal_mode rides the same `-q` path: cli.py runs the judge loop there too, so the
    # worker log keeps its live tool feed (forcing -Q blanked it).
    return cmd


def _open_worker_log(task: Task, board: Optional[str]):
    """Append-mode per-task log (a re-run on unblock appends, never overwrites),
    rotated first. Anchored at the board root (not the shared kanban root) so
    `hermes kanban log` reads its own file and boards sharing task ids don't
    collide."""
    log_dir = _kb.worker_logs_dir(board=board)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task.id}.log"
    rotate_bytes, backup_count = worker_log_rotation_config()
    _rotate_worker_log(log_path, rotate_bytes, backup_count)
    return open(log_path, "ab")


def _restart_safe_worker_argv(task: Task, command: list[str]) -> list[str]:
    """Wrap a systemd-hosted dispatcher's worker in the shared restart-safe scope.

    Kanban workers are long-lived agentic runs that outlive the dispatcher
    tick, so they never take cron's degraded mode under the managed gateway:
    ``require_restart_safe_scope=True`` makes the helper raise
    ``RestartSafeScopeUnavailable`` there (an infrastructure spawn failure the
    dispatcher does not charge to the card). Under any other systemd unit
    (``Type=oneshot`` dispatch timers, #113612) ``outlives_parent=True`` gets the
    worker its own scope so the unit's cgroup teardown cannot kill it.
    """
    from tools.process_registry import restart_safe_gateway_child_argv

    if task.current_run_id is None:
        # Outside managed systemd this is harmless, but a managed dispatch must
        # never mint an untraceable worker.  Check topology through the shared
        # helper first, using a placeholder suffix that cannot be launched.
        dispatch = restart_safe_gateway_child_argv(
            command,
            unit_suffix=f"kanban-{task.id}-run-missing",
            require_restart_safe_scope=True,
            outlives_parent=True,
        )
        if dispatch.mode != "in_process":
            raise RuntimeError(
                "cannot create restart-safe systemd scope for Kanban worker: "
                "the claimed task has no current run id"
            )
        return command

    return restart_safe_gateway_child_argv(
        command,
        unit_suffix=f"kanban-{task.id}-run-{task.current_run_id}",
        require_restart_safe_scope=True,
        outlives_parent=True,
    ).argv


def _operator_ask_env(board: Optional[str], task: Task) -> Optional[str]:
    """``HERMES_KANBAN_OPERATOR_ASK`` for a worker about to run ``task``, or ``None``.

    Returns ``<register>/<ask>`` when the card serves an ask, ``<register>`` when the card
    IS the board's register (each card it files is then a fresh ask under that register),
    and ``None`` otherwise — the common case. Never raises: a dispatcher that cannot answer
    this question must still spawn the worker, because the stamp is bookkeeping and the run
    is the work.
    """
    from hermes_cli import kanban_register as reg

    try:
        return reg.env_ref_for_worker(board, task.id, getattr(task, "body", None))
    except Exception as exc:  # pragma: no cover - defensive, see docstring
        logger.warning("operator-ask env for %s could not be resolved: %s", task.id, exc)
        return None


def _default_spawn(task: Task, workspace: str, *, board: Optional[str] = None) -> Optional[int]:
    """Fire-and-forget ``hermes -p <profile> chat -q ...`` subprocess.

    Returns the child's PID so the dispatcher can detect crashes before the
    claim TTL expires; completion is still observed via the worker's own
    ``complete`` / ``block`` transitions. ``board`` pins the child's
    ``HERMES_KANBAN_DB`` / ``HERMES_KANBAN_BOARD`` / workspaces_root to the
    board the task was claimed from, so workers cannot see other boards.
    """
    if not task.assignee:
        raise ValueError(f"task {task.id} has no assignee")

    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env

    profile_arg = normalize_profile_name(task.assignee)

    from agent.secret_scope import is_multiplex_active
    from tools.environments.local import _is_routed_home, build_subprocess_env, strip_launch_profile_env

    try:
        profile_home = resolve_profile_env(profile_arg)
    except FileNotFoundError:
        # No profile dir (isolated test fixtures) — the CLI resolves it from
        # HERMES_PROFILE (set below) instead.
        profile_home = None

    # Scrub for a ROUTED home, not only under multiplex: the authority test is "does this worker act
    # for another profile", exactly as served_profile_child_env decides it (tools/environments/local.py).
    # Gating on the gateway-wide flag left B's worker inheriting the dispatcher's own OPENAI_API_KEY and
    # systemd-injected tokens on every single-profile host.
    routed = bool(profile_home) and _is_routed_home(profile_home)
    # build_subprocess_env's secret scrub resolves terminal.env_passthrough vars through get_secret(),
    # which without a bound scope reads the LAUNCH profile's ambient environment for a worker spawned
    # on B's behalf (and raises under multiplex) — so bind B's secret scope around the build.
    with (_worker_profile_scope(profile_home, bind_home=False) if profile_home
          else contextlib.nullcontext()):
        env = build_subprocess_env(
            scrub_secrets=is_multiplex_active() or routed,
            inherit_profile_home=True,
        )
    # The dispatcher is detached from every conversation; its worker must never
    # inherit routing mirrored by a previous gateway turn.
    from gateway.session_context import _VAR_MAP
    for key in _VAR_MAP:
        env.pop(key, None)

    # Inject HERMES_HOME so the worker reads the profile-scoped config.yaml:
    # without it the child's get_hermes_home() falls back to the DEFAULT
    # profile root because `hermes -p` applies its override before
    # hermes_constants is imported.
    if profile_home:
        env["HERMES_HOME"] = profile_home
        # A multiplexer dispatching for another profile must not hand it the launch
        # profile's .env settings / TERMINAL_* policy — a standalone dispatcher never would.
        strip_launch_profile_env(env, profile_home)
    if task.tenant:
        env["HERMES_TENANT"] = task.tenant
    env["HERMES_KANBAN_TASK"] = task.id
    env["HERMES_KANBAN_WORKSPACE"] = workspace
    # The operator ask this card serves (card t_8ca4b5a0), so every card this worker files
    # is stamped at the create path without the agent remembering to say so - the defect
    # this closes was that the register tree was rebuilt from memory, and memory is what
    # fails. Set only when the card actually serves one: an always-present variable would
    # stamp the whole host. A worker running the register card itself inherits the register
    # id alone, which makes each card it files a fresh ask under it.
    ask_env = _operator_ask_env(board, task)
    if ask_env:
        from hermes_cli import kanban_register as reg

        env[reg.ENV_VAR] = ask_env
    # Skill names the HARNESS pre-flagged as ADVISORY for this run: the review skills it injected, and
    # any card-requested name it could not resolve for this lane. The worker's preload loader treats an
    # advisory name as non-fatal — it warns and continues instead of raising ``Unknown skill(s)`` and
    # killing the run at INIT.
    advisory_skills = tuple(getattr(task, "advisory_skills", ()) or ())
    if advisory_skills:
        from agent.skill_commands import ADVISORY_SKILLS_ENV as _advisory_skills_env

        env[_advisory_skills_env] = ",".join(advisory_skills)
    # Tag the session `kanban` so session-browsing surfaces filter it out by
    # source instead of rendering one sidebar row per attempt.
    env["HERMES_SESSION_SOURCE"] = "kanban"
    # TERMINAL_CWD takes precedence over process cwd in file_tools and
    # build_context_files_prompt; without it relative writes land in the gateway
    # user's home and workers load the gateway's AGENTS.md. file_tools rejects
    # relative / sentinel values, so only set a real absolute directory.
    # Pin TERMINAL_CWD to the task's workspace so the worker's file tools and context-file loader anchor on
    # the workspace, not whatever cwd the dispatching gateway happened to export. The worker subprocess is
    # already launched with cwd=workspace, but TERMINAL_CWD takes precedence over the process cwd in both
    # file_tools._resolve_base_dir (#41312 — relative write_file paths were landing in the gateway user's
    # home) and build_context_files_prompt (#34619 — workers loaded the dispatching gateway's AGENTS.md
    # instead of the task's). Setting it to the workspace fixes both: the workspace is where the task's work
    # actually happens.
    if workspace and os.path.isabs(workspace) and os.path.isdir(workspace):
        env["TERMINAL_CWD"] = workspace
    if task.branch_name:
        env["HERMES_KANBAN_BRANCH"] = task.branch_name
    if task.current_run_id is not None:
        env["HERMES_KANBAN_RUN_ID"] = str(task.current_run_id)
    if task.claim_lock:
        env["HERMES_KANBAN_CLAIM_LOCK"] = task.claim_lock
    # Goal-loop mode (Ralph-style /goal judge loop in cli.py quiet-mode path).
    # Only set when enabled so non-goal tasks keep a clean env.
    if task.goal_mode:
        env["HERMES_KANBAN_GOAL_MODE"] = "1"
        if task.goal_max_turns is not None:
            env["HERMES_KANBAN_GOAL_MAX_TURNS"] = str(int(task.goal_max_turns))
    for var in ("TERMINAL_TIMEOUT", "TERMINAL_MAX_FOREGROUND_TIMEOUT"):
        # The card's EXPLICIT cap only. ``kanban.default_max_runtime_seconds`` is stamped onto the
        # card at claim so the runtime sweep can reap the slot; it is NOT an author budget and must
        # never widen a worker's terminal timeout (ruling t_b2865b89 §4). ``_dispatch_lane_task``
        # pins the pre-stamp explicit value onto the Task it hands here for exactly this reason.
        override = _worker_terminal_timeout_env(task.max_runtime_seconds, env.get(var))
        if override is not None:
            env[var] = override
    # Pin the board DB + workspaces root so the worker's kanban paths still
    # match after `hermes -p` rewrites HERMES_HOME (symlink / Docker layouts).
    env["HERMES_KANBAN_DB"] = str(_kb.kanban_db_path(board=board))
    env["HERMES_KANBAN_WORKSPACES_ROOT"] = str(_kb.workspaces_root(board=board))
    _retag_legacy_worker_sessions(env["HERMES_KANBAN_WORKSPACES_ROOT"])
    # Board slug — defense-in-depth pin if a path is resolved without the
    # DB / workspaces env vars.
    env["HERMES_KANBAN_BOARD"] = _kb._normalize_board_slug(board) or _kb.get_current_board()
    # kanban_comment reads HERMES_PROFILE for its default author; `-p` alone
    # doesn't set the env var.
    env["HERMES_PROFILE"] = profile_arg
    # This is the grant boundary: the dispatcher assigned this new worker's task.
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER
    env.pop(DELEGATED_CHILD_ENV_MARKER, None)
    # `--cli` is the highest-precedence TUI override; dropping HERMES_TUI covers
    # older hermes builds on PATH that predate the flag's precedence.
    env.pop("HERMES_TUI", None)

    cmd = _worker_argv(task, profile_arg, env.get("HERMES_HOME"))
    # The module argv must carry the import context that made it resolvable:
    # the shim's in-process path injection is invisible to the bare child.
    _propagate_module_import_root(cmd, env)
    # A worker spawned by a managed systemd gateway must leave the gateway's
    # cgroup before startup; otherwise restarting the service kills the worker
    # that is performing the handoff.
    cmd = _restart_safe_worker_argv(task, cmd)
    from tools.process_registry import systemd_user_bus_env
    env = systemd_user_bus_env(env)
    log_f = _open_worker_log(task, board)
    try:
        proc = subprocess.Popen(  # noqa: S603 -- argv is a fixed list built above
            cmd,
            cwd=workspace if os.path.isdir(workspace) else None,
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            creationflags=subprocess.CREATE_NO_WINDOW if _kb._IS_WINDOWS else 0,
        )
    except FileNotFoundError:
        log_f.close()
        raise RuntimeError(
            "`hermes` executable not found on PATH. "
            "Install Hermes Agent or activate its venv before running the kanban dispatcher."
        )
    # Intentionally NOT closing log_f: the child keeps writing after return;
    # the OS-level FD stays open in the child until it exits.
    if _kb._IS_WINDOWS:
        _live_worker_procs[proc.pid] = proc
    return proc.pid


# ---------------------------------------------------------------------------
# Long-lived dispatcher daemon
# ---------------------------------------------------------------------------

def run_daemon(
    *,
    interval: float = 60.0,
    max_spawn: Optional[int] = None,
    failure_limit: int = DEFAULT_FAILURE_LIMIT,
    stop_event=None,
    on_tick=None,
) -> None:
    """Run the dispatcher in a loop until interrupted.

    Calls :func:`dispatch_once` every ``interval`` seconds; exits cleanly on
    SIGINT / SIGTERM so it is systemd-friendly. ``stop_event`` and ``on_tick``
    are test hooks. Each tick resolves ``kanban.max_in_progress`` exactly like
    the gateway dispatcher and ``hermes kanban dispatch`` — the standalone
    daemon must not be the one uncapped entry point.
    """
    import threading

    # The host budget is raced by every board; a single-board daemon cannot
    # share it, but it can still say when its OWN queue keeps losing the race.
    host_cap_clock = HostCapStarvationClock()

    if stop_event is None:
        stop_event = threading.Event()

    def _handle(_signum, _frame):
        stop_event.set()

    # Install handlers only on the main thread — tests call this inline from
    # worker threads and signal() would raise there.
    if threading.current_thread() is threading.main_thread():
        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(sig, _handle)

    while not stop_event.is_set():
        try:
            # Re-resolved every tick (config load is mtime-cached) so operator
            # edits apply without a restart.
            max_in_progress = resolve_max_in_progress(configured_max_in_progress())
            lane_fair_spawn, designated_pool_reserve = lane_fair_ready_config()
            default_max_runtime_seconds = default_max_runtime_seconds_config()
            with contextlib.closing(_kbc.connect()) as conn:
                res = dispatch_once(
                    conn,
                    max_spawn=max_spawn,
                    max_in_progress=max_in_progress,
                    failure_limit=failure_limit,
                    lane_fair_spawn=lane_fair_spawn,
                    designated_pool_reserve=designated_pool_reserve,
                    default_max_runtime_seconds=default_max_runtime_seconds,
                )
            if on_tick is not None:
                with contextlib.suppress(Exception):
                    on_tick(res)
            starved = host_cap_clock.observe([(_current_board_label(), res)])
            if starved:
                _kb._log.warning("%s", starved)
        except Exception:
            # Don't let any single tick kill the daemon.
            import traceback
            traceback.print_exc()
        stop_event.wait(timeout=interval)


# Late-bound origin namespace (see module docstring); imported LAST so this
# module is fully populated before ``kanban_db`` imports from it.
from hermes_cli import kanban_db as _kb  # noqa: E402
from hermes_cli import kanban_db_connect as _kbc  # noqa: E402
from hermes_cli import kanban_db_workspace as _kbw  # noqa: E402
