"""SQLite-backed Kanban board shared across profiles (the cross-profile coordination primitive).

Lives under the shared Hermes root: ``default`` board DB at ``<root>/kanban.db`` (pre-boards
back-compat), other boards at ``<root>/kanban/boards/<slug>/``; a worker on one board never sees
another. Board resolution: ``board=`` arg > ``HERMES_KANBAN_BOARD`` > ``HERMES_KANBAN_DB`` (pins the
file path) > ``<root>/kanban/current`` > ``default`` — but only for unfenced callers; the dispatcher
injects these into workers, and dispatched workers (``HERMES_KANBAN_TASK``), delegated children and
board-enumerating machine flows (gateway notifier/watcher/dispatcher, ``pin_first_board_resolution``)
always resolve through the pin, so workers physically cannot see other boards. The model-facing tools
(``tools/kanban_tools``) additionally REFUSE an explicit ``board=`` that resolves to a different board
than the pinned DB (:func:`assert_board_matches_pin`) instead of silently serving the pin — a read from
the wrong board is indistinguishable from success.
Concurrency: WAL + ``BEGIN IMMEDIATE`` + compare-and-swap on ``tasks.status``/``claim_lock`` —
SQLite serializes writers so one claimer wins, losers see zero rows (no retries, no distributed
locks). Schema: tasks, task_links, task_comments, task_events, task_runs, attachments, notify subs.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import logging
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from hermes_cli.kanban_workflow import DEFAULT_STATUSES as VALID_STATUSES
from toolsets import get_toolset_names

_log = logging.getLogger(__name__)


# --- Shared micro-helpers (row access, JSON, env, git) ---

def _lossy_text(value: Any) -> Any:
    """``bytes`` -> ``str`` with U+FFFD for undecodable sequences; anything else passes through.

    Installed as every board connection's ``text_factory`` (a TEXT cell holding
    invalid UTF-8 otherwise aborts the whole ``fetchall`` with "Could not decode
    to UTF-8") and applied to BLOB-typed cells in the ``from_row`` constructors
    (task, comment, event, run), so one
    corrupt row degrades to replacement characters instead of taking the board
    listing down."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _row_get(row: Any, col: str, default: Any = None) -> Any:
    """``row[col]`` tolerant of the column being absent from the SELECT / schema."""
    if row is None or col not in row.keys():
        return default
    return row[col]


def _json_or(value: Any, default: Any = None) -> Any:
    """Decode a JSON text column; any decode failure or empty value yields ``default``."""
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _json_dict(value: Any) -> dict:
    """Decode a JSON text column that must be an object; anything else yields ``{}``."""
    parsed = _json_or(value, {})
    return parsed if isinstance(parsed, dict) else {}


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    """Integer env override: absent/empty/non-integer/below ``minimum`` falls back to ``default``."""
    raw = os.environ.get(name, "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            return default
        if parsed >= minimum:
            return parsed
    return default


def _git_out(cwd: Path, *args: str, timeout: int = 30) -> Optional[str]:
    """Run ``git -C cwd args`` and return stripped stdout, or ``None`` on any failure / empty output."""
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None


# --- Constants ---

VALID_INITIAL_STATUSES = {"running", "blocked"}

# Statuses no worker will ever be launched in again: the card is closed, so the
# ``(assignee, skills)`` pair cannot fail at dispatch any more. Read by the sites
# that judge that pair to tell attribution on a finished card from handing a name
# to a worker that cannot load it. Distinct from ``goals._KANBAN_TERMINAL_STATUSES``,
# which names the worker-driven GOAL-LOOP terminators (``blocked``/``review`` are
# terminators there and very much still dispatchable here).
TERMINAL_STATUSES = {"done", "archived"}

# Typed block reasons (routing in ``_route_block``); ``None`` = legacy un-typed.
VALID_BLOCK_KINDS = {"dependency", "needs_input", "capability", "transient"}

# Same-reason block -> unblock -> re-block cycles before routing to ``triage``.
# Counts unblock recurrences, NOT dispatcher failures (``DEFAULT_FAILURE_LIMIT``).
BLOCK_RECURRENCE_LIMIT = 2
VALID_WORKSPACE_KINDS = {"scratch", "worktree", "dir"}

# --- The due contract (``due_at``) -----------------------------------------
#
# ``due_at`` (absolute epoch seconds) is the clock fence a parked card can carry:
# ``schedule_task(due_at=...)`` fences a ``scheduled`` card, ``block_task(due_at=...)``
# fences a ``blocked`` one. The sanctioned verbs that express it are
# ``hermes kanban schedule --due`` / ``hermes kanban block --due``.
#
# TWO rules this contract exists to protect, both load-bearing:
#
# * **Omitting a due time IS the human hold.** A card with ``due_at IS NULL``
#   waits for a person, not a clock -- ``kanban_top_level_gate`` parks
#   ``GATE-TOP-LEVEL-ONLY`` cards exactly that way, and the dispatcher never
#   promotes ``scheduled``. No verb may silently turn a human hold into a timed
#   hold: the fence is only ever written when a caller asks for it.
# * **ONE releaser.** The release leg is the deployed ``maintenance-sweep``
#   workflow's ``scheduled_wake`` node. Nothing in this process ticks, so this
#   module ships no waker and no automatic unblock path.


class _UnsetType:
    """Sentinel for ``schedule_task(due_at=...)`` / ``block_task(due_at=...)``.

    Omitting ``due_at`` (the default, ``UNSET``) leaves whatever fence the card
    already carries. Passing ``None`` explicitly CLEARS it -- parking a card by
    hand means "no clock". Collapsing the two would make every legacy
    ``schedule_task`` call silently disarm a self-waking card.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"


UNSET = _UnsetType()

# What a release does when the fence falls inside a reserved execution band.
# ``defer`` holds the release to the end of the band; ``ambient`` releases anyway,
# for a wake that is a lightweight check and has to move around the clock.
VALID_DUE_WINDOW_POLICIES = ("defer", "ambient")
DEFAULT_DUE_WINDOW_POLICY = "defer"

# The refusal for a fence the board cannot carry, worded ONCE. The driver error it
# replaces -- ``OperationalError: no such column: due_at`` -- names neither the
# cause nor the remedy, and it fires on a fence-FREE park too, because the probe
# row named the fence column as well as the write.
_FENCE_COLUMN_ABSENT = (
    "this board cannot hold a clock fence: no due_at column on tasks; it is "
    "un-migrated — open it through kanban_db_connect.connect() (which migrates it) "
    "or park without --due"
)


def _tasks_columns(conn: sqlite3.Connection) -> set[str]:
    """The ``tasks`` columns the board ACTUALLY carries (PRAGMA, never assumed).

    The same probe the deployed release leg uses
    (``kanban_unblocker_nodes._env.task_columns``), for the same reason: the fence
    columns (``due_at`` / ``due_window_policy``) belong to the dispatcher's
    time-fenced-block half, and a store built without that half has no such column,
    so ANY statement that names them unconditionally dies with ``no such column``.
    A board reached through ``kanban_db_connect.connect`` always carries them (its
    ``init_db`` migrates them in via ``_LATER_TASK_COLUMNS``); a raw handle on an
    un-migrated board does not.
    """
    return {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}


def _fence_guard(conn: sqlite3.Connection, due_at: Any) -> set[str]:
    """Refuse a clock fence the board cannot hold; return its ``tasks`` columns.

    Raises :class:`ValueError` naming the cause and the remedy when the caller
    ASKED for a fence (``due_at is not UNSET``) and the store has no ``due_at``
    column -- raised HERE so the caller reads a refusal, not the driver's bare
    ``OperationalError``. A park with NO fence asks for no clock, needs no fence
    column, and is never refused here.
    """
    columns = _tasks_columns(conn)
    if due_at is not UNSET and "due_at" not in columns:
        raise ValueError(_FENCE_COLUMN_ABSENT)
    return columns


def _present_columns(columns: set[str], names: Iterable[str]) -> str:
    """``names`` filtered to those the board HAS, comma-joined for a SELECT list.

    The read-side half of the rule :func:`_fence_guard` enforces on the write side:
    a SELECT that names an absent column raises exactly as an UPDATE would, so a
    fence-free park must not name the fence column at all.
    """
    return ", ".join(name for name in names if name in columns)


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    """One board-level ``kanban_meta`` value, or ``None`` when unset."""
    row = conn.execute("SELECT value FROM kanban_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else None


def get_meta_int(conn: sqlite3.Connection, key: str, default=None):
    """``get_meta`` as an int; a missing or unparsable value reads as ``default``."""
    raw = get_meta(conn, key)
    try:
        return int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default


def normalize_reasoning_effort(effort: Optional[str]) -> Optional[str]:
    """``VALID_REASONING_EFFORTS`` or ``"none"`` (thinking off), case-insensitive;
    empty/None = inherit the profile's own effort (NULL). Anything else raises —
    a typo'd level must not quietly hand the task back to the profile default."""
    from hermes_constants import VALID_REASONING_EFFORTS

    value = str(effort or "").strip().lower()
    if not value:
        return None
    if value == "none" or value in VALID_REASONING_EFFORTS:
        return value
    allowed = ", ".join(("none", *VALID_REASONING_EFFORTS))
    raise ValueError(f"reasoning_effort must be one of {allowed}, got {effort!r}")


KNOWN_TOOLSET_NAMES = frozenset(name.casefold() for name in get_toolset_names())
_IS_WINDOWS = sys.platform == "win32"
KANBAN_ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024  # one cap for dashboard, tools and CLI


def _assert_not_delegated_child_mutation(path: str | Path | None = None) -> None:
    """Reject Kanban mutations from ``delegate_task`` child contexts.

    The tool/CLI fast-fail guards are UX, not a trust boundary (a child can shell
    out or import this module); the invariant lives here so every ``write_txn``
    user and board-metadata mutator fails closed before touching durable state.
    *path* is the board DB / metadata root being mutated; ``None`` means the
    lineage's own board (``kanban_home()``).
    """
    from agent.delegation_context import kanban_path_is_fenced

    if kanban_path_is_fenced(kanban_home() if path is None else path):
        raise PermissionError("delegate_task child contexts cannot mutate Kanban tasks or boards")


def _fire_kanban_lifecycle_hook(event: str, task_id: str, **fields: Any) -> None:
    """Best-effort lifecycle hook. Call AFTER the write txn commits (plugins never
    run under the SQLite write lock, always see durable state); failures are
    swallowed so an observer can never break a transition."""
    try:
        from hermes_cli.lifecycle import invoke_hook

        invoke_hook(event, task_id=task_id, profile_name=_hook_profile_name(), **fields)
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("kanban lifecycle hook %s failed: %s", event, exc)


def _fire_task_hook(event: str, task: Optional[Task], task_id: str, run_id: Optional[int], **fields: Any) -> None:
    """Lifecycle hook for a task transition; ``assignee`` from the (possibly missing) row."""
    _fire_kanban_lifecycle_hook(
        event, task_id, board=get_current_board(),
        assignee=task.assignee if task else None, run_id=run_id, **fields,
    )


def _hook_profile_name() -> str:
    """Active profile for hook payloads; ``"default"`` when it cannot be resolved."""
    from hermes_cli.profiles import get_active_profile_name

    try:
        return get_active_profile_name()
    except Exception:
        return "default"


def _kanban_observer_consumed(event: str) -> bool:
    """Hot-path short-circuit: skip payload assembly when nothing subscribes.
    Inspection failure counts as unconsumed (dropping an observer is always safe)."""
    try:
        from hermes_cli.lifecycle import has_hook

        return has_hook(event)
    except Exception:  # pragma: no cover - defensive
        return False


def _fire_worker_spawned_hook(
    conn: sqlite3.Connection, task: Task, workspace_path: str, pid: Optional[int], *,
    board: Optional[str] = None,
) -> None:
    """``on_kanban_worker_spawned`` AFTER the PID is durably persisted; best-effort."""
    if not _kanban_observer_consumed("on_kanban_worker_spawned"):
        return
    try:
        _fire_kanban_lifecycle_hook(
            "on_kanban_worker_spawned", task.id, board=board or get_current_board(),
            assignee=task.assignee, run_id=_current_run_id(conn, task.id),
            worker_pid=int(pid) if pid else None, workspace_path=str(workspace_path),
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("kanban worker spawned hook failed: %s", exc)


def notify_task_updated(
    conn: sqlite3.Connection, task_id: str, changed_fields: Iterable[str], *,
    board: Optional[str] = None,
) -> None:
    """``on_kanban_task_updated`` AFTER a non-lifecycle task mutation commits
    (also for direct-SQL surfaces like dashboard field editors).
    ``changed_fields`` carries field NAMES only, never values."""
    if not _kanban_observer_consumed("on_kanban_task_updated"):
        return
    try:
        row = conn.execute(
            "SELECT assignee, current_run_id FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        _fire_kanban_lifecycle_hook(
            "on_kanban_task_updated", task_id, board=board or get_current_board(),
            assignee=row["assignee"] if row else None,
            run_id=row["current_run_id"] if row else None, changed_fields=list(changed_fields),
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("kanban task updated hook failed: %s", exc)


# DispatchResult counters whose non-zero value means the tick did something.
_TICK_ACTIVITY_FIELDS = (
    "spawned", "reclaimed", "promoted", "reconciled_orphans", "reaped_terminal_workers", "crashed", "stale",
    "timed_out", "auto_blocked", "rate_limited", "auto_assigned_default",
    "respawn_guarded", "skipped_per_profile_capped", "skipped_unassigned",
    "skipped_nonspawnable", "skipped_self_review",
)


def _fire_dispatch_tick_hook(
    result: DispatchResult, *, board: Optional[str] = None, dry_run: bool = False,
) -> None:
    """``on_kanban_dispatch_tick`` — strictly AFTER ``_dispatch_tick_lock`` is
    released so a slow subscriber cannot stall a sibling dispatcher.

    Re-port of PR #56066 per the #64231 batch disposition: renamed to the taxonomy form and called by
    ``dispatch_once`` strictly AFTER ``_dispatch_tick_lock`` has been released — the original fired inside
    the lock, so a slow subscriber could extend the single-writer critical section and stall a sibling
    dispatcher's tick. Observer-only and fully best-effort: any subscriber failure is swallowed.
    """
    if not _kanban_observer_consumed("on_kanban_dispatch_tick"):
        return
    try:
        from hermes_cli.lifecycle import invoke_hook

        profile_name = _hook_profile_name()
        if board is None:
            try:
                board = get_current_board()
            except Exception:
                board = None
        outcome = "ok"
        if result.skipped_locked:
            outcome = "skipped_locked"
        elif getattr(result, "tick_yielded", ""):
            # A refused tick is its own outcome -- never "idle" (which reads as a healthy quiet
            # loop). Ruling t_8fed34c8: a yielding ticker must be distinguishable from a healthy
            # one at every status surface, this observer included.
            outcome = "yielded_tree_skew"
        elif not any(getattr(result, f) for f in _TICK_ACTIVITY_FIELDS):
            outcome = "idle"
        invoke_hook(
            "on_kanban_dispatch_tick", board=board, profile_name=profile_name,
            dry_run=bool(dry_run), outcome=outcome, result=result,
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("kanban dispatch tick hook failed: %s", exc)


# Claim window before the next tick reclaims a running task; long workers
# ``heartbeat_claim`` or raise it via HERMES_KANBAN_CLAIM_TTL_SECONDS.
DEFAULT_CLAIM_TTL_SECONDS = 15 * 60

# A live PID with a heartbeat older than this is wedged and reclaimed anyway
# (``_touch_activity`` keeps genuinely active workers fresh).
# If a worker's PID is still alive but its ``last_heartbeat_at`` is older than this when
# ``release_stale_claims`` runs, treat the worker as wedged and reclaim regardless of PID liveness (#29747
# gap 3). This catches the logic-loop case where the process is technically running but not making
# observable progress. ``_touch_activity`` bridges chunk-level liveness into ``last_heartbeat_at`` via
# #31752, so any genuinely active worker keeps its heartbeat fresh as a side effect of normal API traffic.
DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS = 60 * 60

# Grace when a host-local worker survived termination (e.g. parked in D state
# under memory.high, SIGKILL pending): releasing now would spawn a duplicate.
RECLAIM_DEFER_GRACE_SECONDS = 120


def _resolve_claim_ttl_seconds(ttl_seconds: Optional[int] = None) -> int:
    """Explicit ``ttl_seconds`` > ``HERMES_KANBAN_CLAIM_TTL_SECONDS`` > default."""
    if ttl_seconds is not None:
        return max(1, int(ttl_seconds))

    return _env_int("HERMES_KANBAN_CLAIM_TTL_SECONDS", DEFAULT_CLAIM_TTL_SECONDS, minimum=1)


# ``detect_crashed_workers`` skips ``_pid_alive`` this long after start: the
# fork -> /proc window can report a fresh worker dead.
DEFAULT_CRASH_GRACE_SECONDS = 30

# Worker exit "provider rate-limited": released WITHOUT counting a failure (the
# breaker must never trip on a throttle). 75 == BSD EX_TEMPFAIL.
KANBAN_RATE_LIMIT_EXIT_CODE = 75

# Worker exit "provider rejected the configuration": credential revoked (401/403), model gone
# (404), TLS chain broken — a retry cannot fix it, so the dispatcher parks the card blocked on
# the FIRST occurrence instead of spending ``failure_limit`` identical spawns. 78 == BSD EX_CONFIG.
KANBAN_TERMINAL_PROVIDER_EXIT_CODE = 78

# Worker exit "the profile state DB's write lock was held by a peer": the turn was aborted because
# the profile state DB's single SQLite write lock was held by a peer process (another worker of the
# same lane, the gateway, the dashboard, or a maintenance checkpoint/VACUUM) past the transcript
# write patience. Infrastructure, NOT the card — the dispatcher re-queues the card WITHOUT consuming
# its give-up budget, so a busy store can never park a card whose own work was fine.
# 76 is unused elsewhere in this tree (75 = rate limit, 78 = terminal provider).
KANBAN_STORAGE_LOCKED_EXIT_CODE = 76


def _resolve_crash_grace_seconds() -> int:
    """``HERMES_KANBAN_CRASH_GRACE_SECONDS`` (0 = immediate, for tests) else default."""
    return _env_int("HERMES_KANBAN_CRASH_GRACE_SECONDS", DEFAULT_CRASH_GRACE_SECONDS)


def _resolve_rate_limit_cooldown_seconds() -> int:
    """``HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS`` (0 = next tick, for tests) else default."""
    return _env_int("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS)


def _resolve_storage_locked_cooldown_seconds() -> int:
    """``HERMES_KANBAN_STORAGE_LOCKED_COOLDOWN_SECONDS`` (0 = next tick, for tests) else default."""
    return _env_int(
        "HERMES_KANBAN_STORAGE_LOCKED_COOLDOWN_SECONDS", DEFAULT_STORAGE_LOCKED_COOLDOWN_SECONDS,
    )


# build_worker_context() caps, sized for a ~100k-char prompt with headroom.
_CTX_MAX_PRIOR_ATTEMPTS = 10      # most recent N prior runs shown in full
_CTX_MAX_COMMENTS       = 30      # most recent N comments shown in full
_CTX_MAX_FIELD_BYTES    = 4 * 1024   # per summary/error/metadata/result
_CTX_MAX_BODY_BYTES     = 8 * 1024   # per task.body (opening post)
_CTX_MAX_COMMENT_BYTES  = 2 * 1024   # per comment


def _relative_age(ts: Optional[int], now: Optional[int] = None) -> str:
    """``just now`` / ``18h ago`` / ``3d ago``; "" for a missing/invalid ts. An LLM
    reads a bare absolute timestamp as current fact — the relative age is what
    prompts a worker to re-verify stale sibling work."""
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return ""
    if now is None:
        now = int(time.time())
    delta = now - ts
    if delta < 60:  # includes negative = clock skew across machines; never claim "in the future"
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


# --- Paths ---

DEFAULT_BOARD = "default"
_CURRENT_BOARD_OVERRIDE: ContextVar[str | None] = ContextVar(
    "hermes_kanban_current_board_override", default=None,
)
# Machine flows that enumerate boards (gateway notifier / watcher / dispatcher
# ticks) resolve board paths env-pin-first — see pin_first_board_resolution().
_PIN_FIRST_BOARD_RESOLUTION: ContextVar[bool] = ContextVar(
    "hermes_kanban_pin_first_board_resolution", default=False,
)


@contextlib.contextmanager
def pin_first_board_resolution():
    """Resolve board paths env-pin-first for machine flows that enumerate boards.

    The gateway notifier, per-subscription cursor writes and the embedded
    dispatcher poll every board slug from ``list_boards()`` — the slug is not
    caller intent, it is an iteration variable. On a box whose environment pins
    ``HERMES_KANBAN_DB`` (the dispatcher default) every slug must map to that
    one pinned file: the notifier dedupes resolved DB paths, and per-slug
    paths read empty boards nobody writes, silently killing wake
    notifications. Explicit cross-board intent (CLI ``--board``, a model
    tool's ``board=``) is a USER property and must never run inside this
    context; with no pin set this context changes nothing.
    """
    token = _PIN_FIRST_BOARD_RESOLUTION.set(True)
    try:
        yield
    finally:
        _PIN_FIRST_BOARD_RESOLUTION.reset(token)


@contextlib.contextmanager
def scoped_current_board(slug: str):
    """Pin the active board for the current context only."""
    token: Token[str | None] = _CURRENT_BOARD_OVERRIDE.set(slug)
    try:
        yield
    finally:
        _CURRENT_BOARD_OVERRIDE.reset(token)


# Slug = directory name: strict enough to stop traversal / separators, loose
# enough for kebab-case. Display names (spaces, emoji) live in board.json.
_BOARD_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,63}$")


def _normalize_board_slug(slug: Optional[str]) -> Optional[str]:
    """Lowercase + strip a slug; validate; return ``None`` for empty."""
    s = str(slug).strip().lower() if slug is not None else ""
    if not s:
        return None
    if not _BOARD_SLUG_RE.match(s):
        raise ValueError(
            f"invalid board slug {slug!r}: must be 1-64 chars, lowercase "
            f"alphanumerics / hyphens / underscores, not starting with '-' or '_'"
        )
    return s


def _slug_or_default(board: Optional[str]) -> str:
    return _normalize_board_slug(board) or DEFAULT_BOARD


def _require_slug(slug: str) -> str:
    normed = _normalize_board_slug(slug)
    if not normed:
        raise ValueError("board slug is required")
    return normed


def kanban_home() -> Path:
    """``HERMES_KANBAN_HOME`` else ``get_default_hermes_root()``. Shared across
    profiles BY DESIGN: resolving through the active profile's HERMES_HOME would
    fork the board per profile and break the dispatcher/worker handoff."""
    override = os.environ.get("HERMES_KANBAN_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root()


def boards_root() -> Path:
    """``<root>/kanban/boards`` — parent of the *additional* named boards.
    ``default`` is deliberately not here (its DB stays at ``<root>/kanban.db``)."""
    return kanban_home() / "kanban" / "boards"


def current_board_path() -> Path:
    """``<root>/kanban/current`` — one-line slug written by ``boards switch``; absent = ``default``."""
    return kanban_home() / "kanban" / "current"


def get_current_board() -> str:
    """Active slug: context override -> ``HERMES_KANBAN_BOARD`` -> ``<root>/kanban/current``
    (only while that board exists) -> ``DEFAULT_BOARD``. A malformed/stale slug
    falls through — the dispatcher must never crash on a hand-edited file."""
    def _existing(candidate: str) -> Optional[str]:
        if not candidate:
            return None
        try:
            normed = _normalize_board_slug(candidate)
        except ValueError:
            return None
        if not (normed and board_exists(normed)):
            return None
        # An archived board leaves a tombstone board.json; a stale `current`
        # file or HERMES_KANBAN_BOARD pointing at one falls through to
        # default rather than resurrecting it (#43243).
        if read_board_metadata(normed).get("archived"):
            return None
        return normed

    for candidate in (
        (_CURRENT_BOARD_OVERRIDE.get() or "").strip(),
        os.environ.get("HERMES_KANBAN_BOARD", "").strip(),
    ):
        found = _existing(candidate)
        if found:
            return found
    try:
        f = current_board_path()
        if f.exists():
            # utf-8-sig read fix (ours): tolerate BOM-persisted current-board files.
            val = f.read_text(encoding="utf-8-sig").strip()
            if val:
                try:
                    normed = _normalize_board_slug(val)
                    if normed and board_exists(normed):
                        return normed
                except ValueError:
                    pass
    except OSError:
        pass
    return DEFAULT_BOARD


def set_current_board(slug: str) -> Path:
    """Persist ``slug`` as the active board; returns the file written. Does NOT
    check the board exists — callers do (so ``boards switch <typo>`` errors)."""
    _assert_not_delegated_child_mutation()
    normed = _require_slug(slug)
    path = current_board_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(normed + "\n", encoding="utf-8")
    return path


def clear_current_board() -> None:
    """Remove ``<root>/kanban/current`` so the active board reverts to ``default``."""
    _assert_not_delegated_child_mutation()
    with contextlib.suppress(FileNotFoundError):
        current_board_path().unlink()


def board_dir(board: Optional[str] = None) -> Path:
    """``<root>/kanban/boards/<slug>/``. For ``default`` this holds metadata
    only (board.json, workspaces/, logs/) — its DB stays at ``<root>/kanban.db``
    for back-compat (:func:`kanban_db_path`).
    """
    return boards_root() / _slug_or_default(board)


def board_exists(board: Optional[str] = None) -> bool:
    """Board has a ``board.json`` on disk; ``default`` always exists."""
    slug = _slug_or_default(board)
    if slug == DEFAULT_BOARD:
        return True
    return _dir_holds_board(board_dir(slug))


def board_is_registered(board: Optional[str] = None) -> bool:
    """``board`` exists because someone REGISTERED it, not because a filing minted it.

    ``default`` is always registered. Every other slug is registered iff its directory
    carries ``board.json`` — ``create_board`` always writes that file, while the filing
    path's creating connect() mints only ``kanban.db``. So this is the probe a FILING
    uses to refuse a slug nobody registered. On this tree :func:`board_exists` already
    requires ``board.json`` (#43243), so the two agree; the name is kept because the
    filing path and its tests ask for the registration question by name.
    """
    return board_exists(board)


class BoardResolutionError(ValueError):
    """A board could not be resolved to exactly ONE store, so nothing was resolved.

    Raised instead of guessing. Canonical board resolution never falls back to the
    current board, never falls back to ``default``, and never hands back a would-be
    path under ``boards/<slug>/`` for a board nobody registered: a caller that asked
    for a board by name gets that board's store or a refusal naming the board.

    Subclasses ``ValueError`` so the CLI surfaces it as a usage error. Callers that
    mean "does this board exist" ask :func:`board_exists` instead — that is the
    probe; this is the refusal.
    """


def unregistered_board_refusal(slug: str) -> str:
    """THE refusal text for a caller-named board nobody registered.

    ONE definition, read by every door into a store — the tool board resolver
    (``tools.kanban_tools._board``) and the CLI ``--board`` guard — so the messages
    cannot drift apart. A duplicated string here is a drift bug: the doors would then
    disagree about which slug is refused and why.
    """
    return (
        f"refusing to file on board {slug!r}: no board by that name is "
        f"registered under {boards_root()} (no board.json). A filing never "
        f"mints a board, so a name-shaped slug — a lane handle or a profile "
        f"display name — cannot silently become an estate. Register it with "
        f"`hermes kanban boards create {slug}`, or omit `board` to file on "
        f"the source card's board (else {DEFAULT_BOARD!r})."
    )


def _require_registered_board(board: str) -> str:
    """Normalise an explicit board name and refuse it unless it is REGISTERED.

    The ONE chokepoint an explicit caller-supplied slug passes through before a
    creating ``connect()`` can turn it into a store. ``board=None``/empty never
    reaches here — that means "the active board" and keeps the ambient chain.
    """
    normed = _require_slug(str(board))
    if not board_is_registered(normed):
        raise BoardResolutionError(unregistered_board_refusal(normed))
    return normed


def assert_board_registered(board: Optional[str]) -> Optional[str]:
    """Refuse a caller-named board that is not REGISTERED; return its normalised slug.

    ``None``/empty means "the active board" and is returned unchanged (``None``),
    so the ambient chain (env pin -> current -> default) still applies to a
    source-less call. An explicit name must resolve to a registered board or raise
    :class:`BoardResolutionError` — the *same* refusal the filing path raises — so no
    handler reachable from a caller-supplied slug can MINT a store: the creating seam
    (:func:`kanban_db_connect.connect`) would otherwise satisfy any slug-shaped string
    by writing ``boards/<slug>/``, which is how a lane display handle silently became
    an estate. Callers that meant a probe ask :func:`board_is_registered` instead.
    """
    if not board:
        return None
    return _require_registered_board(str(board))




def _explicit_board_slug(board: Optional[str]) -> Optional[str]:
    """Explicit caller intent: a direct ``board=`` argument, else the scoped
    ``--board`` context (CLI ``hermes kanban --board``, dashboard plugin_api);
    ``None`` when the caller expressed neither."""
    if board is not None:
        return _normalize_board_slug(board)
    # A caller-scoped board (CLI `hermes kanban --board B ...`, dashboard
    # plugin_api) is explicit intent just like a direct board= argument —
    # without this a worker-pinned HERMES_KANBAN_DB silently outranks --board
    # (os-reviewer P1 on PR#107195 / t_11c4afd8).
    ctx = (_CURRENT_BOARD_OVERRIDE.get() or "").strip()
    if ctx:
        try:
            return _normalize_board_slug(ctx)
        except ValueError:
            return None
    return None


def _explicit_board_intent_pinned(board: Optional[str] = None) -> bool:
    """Whether an explicit ``board=`` (or scoped ``--board``) must still resolve
    through the ``HERMES_KANBAN_DB``-style env pins instead of its own board dir.

    True for machine flows wrapped in :func:`pin_first_board_resolution` and
    for every execution the dispatcher fences: its own workers (they carry
    ``HERMES_KANBAN_TASK``) and delegated children / descendants (the
    ``HERMES_DELEGATED_CHILD_CONTEXT`` marker). The pins ARE the "workers
    physically cannot see other boards" isolation (5ec6baa), and
    ``agent.delegation_context.kanban_path_is_fenced`` checks the pinned path /
    fenced root — an explicit board that resolved elsewhere would also escape
    that fence.

    *board* is the name the caller expressed, when it has one: a name that resolves to NO
    registered board is not a pin violation (nothing to pin, no data to protect), so it answers
    ``False`` and the caller's own board resolution produces the honest
    ``board '<x>' does not exist`` (ruling t_ecd51c9c). The machine-flow pin is judged before
    the name, because that pin is the flow's own resolution, not an explicit caller's intent.
    """
    if _PIN_FIRST_BOARD_RESOLUTION.get():
        return True
    from agent.delegation_context import explicit_board_intent_is_pinned
    return explicit_board_intent_is_pinned(board)


def _slug_board_path(slug: str, default_parts: tuple[str, ...], leaf: str) -> Path:
    """``default`` -> legacy ``<root>/<default_parts>``, else ``board_dir(slug)/leaf``."""
    if slug == DEFAULT_BOARD:
        return kanban_home().joinpath(*default_parts)
    return board_dir(slug) / leaf


def _pinned_board_slug(pinned: Path, default_parts: tuple[str, ...], leaf: str) -> Optional[str]:
    """Best-effort slug a pinned path names (``None`` when it is not canonical)."""
    try:
        resolved = pinned.resolve()
    except OSError:
        return None
    home = kanban_home()
    if resolved == home.joinpath(*default_parts).resolve():
        return DEFAULT_BOARD
    try:
        rel = resolved.relative_to(boards_root().resolve())
    except ValueError:
        return None
    parts = rel.parts
    return parts[0] if len(parts) == 2 and parts[1] == leaf else None


def assert_board_matches_pin(board: Optional[str]) -> None:
    """Refuse an explicit ``board=`` slug that resolves to a different board than the
    pinned DB (``HERMES_KANBAN_DB``), instead of silently serving it from the pin — a
    read from the wrong board is indistinguishable from success. No-op when the board
    is omitted or the process is not DB-pinned. Called by the model-facing tools.
    """
    if not board:
        return
    override = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if not override:
        return
    slug = _normalize_board_slug(board)
    if slug is None:
        return
    pinned = Path(override).expanduser()
    natural = _slug_board_path(slug, ("kanban.db",), "kanban.db")
    if natural.resolve() != pinned.resolve():
        pinned_name = _pinned_board_slug(pinned, ("kanban.db",), "kanban.db")
        pinned_label = f"board {pinned_name!r}" if pinned_name else str(pinned)
        raise ValueError(
            f"refusing to target board {slug!r}: this process is pinned to "
            f"{pinned_label} (HERMES_KANBAN_DB)"
        )


def board_env_pin() -> tuple[Optional[str], str]:
    """The board this PROCESS is pinned to by the environment, and what pins it.

    ``("slug", "HERMES_KANBAN_DB")`` when the pin names a board, ``(None, var)`` when a pin
    exists but names no board (a non-canonical store file), ``(None, "")`` when the process
    carries no board pin at all. ``HERMES_KANBAN_DB`` (a store file) outranks
    ``HERMES_KANBAN_BOARD`` (a slug) — the same order :func:`_board_path` and
    :func:`get_current_board` resolve in, so the answer describes the pin that actually wins.
    """
    override = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if override:
        pinned = _pinned_board_slug(Path(override).expanduser(), ("kanban.db",), "kanban.db")
        return pinned, "HERMES_KANBAN_DB"
    slug = os.environ.get("HERMES_KANBAN_BOARD", "").strip()
    if slug:
        try:
            normed = _normalize_board_slug(slug)
        except ValueError:
            normed = None
        if normed:
            return normed, "HERMES_KANBAN_BOARD"
    return None, ""


def assert_board_matches_env_pin(board: Optional[str]) -> Optional[str]:
    """Refuse an explicit ``board=`` that contradicts this process's board PIN.

    A board pin is a FENCE, not a default: it keeps a dispatched worker on the board it
    holds a claim on (the dispatcher injects ``HERMES_KANBAN_DB``) and an operator's shell on
    the board they selected (``HERMES_KANBAN_BOARD``). Both pins work by resolving EVERY slug
    to the pinned store, so an explicit board naming a DIFFERENT board used to be served from
    the pinned one — ``hermes kanban --board platform show <id>`` answered "no such task"
    while the write verbs mutated the pinned board, and ``boards show platform`` rendered the
    platform board's description above the pinned store's path and counts (measured
    2026-10-27, card t_398313ec). A read from the wrong board is indistinguishable from
    success, so the fence must SAY so: raise :class:`ValueError` carrying the same sentence
    the model-facing tools use for this condition, naming both boards.

    No-op when the caller named no board, or when this process carries no board pin — a
    top-level session's ``--board X`` is its own intent and keeps resolving to X's store.
    ALSO a no-op where the pin does not actually override intent: the refusal exists only
    because the resolver would otherwise serve the pinned store, and
    :func:`_explicit_board_intent_pinned` is the ONE place that decides that. Judging the
    pin without it refuses a shape the resolver serves correctly: a shell that merely
    carries ``HERMES_KANBAN_BOARD`` (an operator's selected board — every ``hermes kanban
    watch`` banner case, and the live-claim provenance harness) still resolves ``--board
    beta`` to beta's own store, and ``boards`` still needs per-slug counts. Refusing there
    would be a false refusal on a resolution that is already right — the fence only has to
    speak where the pin WINS.
    Returns the normalised slug for callers that want it.
    """
    normed = _normalize_board_slug(board) if board else None
    if not normed:
        return None
    if not _explicit_board_intent_pinned(normed):
        # Unfenced: ``--board`` outranks the pin (``_board_path``), so no contradiction exists.
        return normed
    pinned_slug, pin_var = board_env_pin()
    if pin_var == "HERMES_KANBAN_DB":
        # The file pin's own comparison (canonical-path aware, same sentence as the tools).
        assert_board_matches_pin(normed)
        return normed
    if pinned_slug and pinned_slug != normed:
        raise ValueError(
            f"refusing to target board {normed!r}: this process is pinned to "
            f"board {pinned_slug!r} (HERMES_KANBAN_BOARD)"
        )
    return normed


def _board_path(
    env_var: Optional[str], board: Optional[str], default_parts: tuple[str, ...], leaf: str,
) -> Path:
    """Shared resolver. An explicit ``board=`` argument — or the scoped
    ``--board`` context (:func:`scoped_current_board`) — outranks the ``env_var``
    pin ONLY where no fence applies (see :func:`_explicit_board_intent_pinned`):
    user-facing cross-board intent (CLI ``--board``, a model tool's ``board=``)
    is honored, but machine flows that enumerate boards (gateway notifier /
    watcher / dispatcher ticks) and dispatched or delegated workers keep
    resolving through the pin. Without explicit intent the ``env_var`` override
    pins the file, else legacy ``<root>/<default_parts>`` for the ``default``
    board, else ``board_dir(slug)/leaf``. Where the pin IS authoritative this
    resolver still returns the pinned file; the model-facing tools refuse a
    contradicting explicit ``board=`` up front via
    :func:`assert_board_matches_pin`, so the pin never silently retargets a
    tool call.
    """
    pin = os.environ.get(env_var, "").strip() if env_var else ""
    slug = _explicit_board_slug(board)
    # The RESOLVER stays pin-first for every named slug, registered or not: it answers "where
    # would this board's store be", never "does this board exist" (that question belongs to the
    # caller's validation, and asking it here would let a fence/refusal look like a path lookup).
    # The resolution rule of t_ecd51c9c lives one layer up, in the GATE that decides whether a
    # name CONTRADICTS the pin: :func:`assert_board_matches_env_pin` passes the name through.
    if pin and (slug is None or _explicit_board_intent_pinned()):
        return Path(pin).expanduser()
    if slug is None:
        slug = get_current_board()
    return _slug_board_path(slug, default_parts, leaf)


def kanban_db_path(board: Optional[str] = None) -> Path:
    """``kanban.db`` path: ``HERMES_KANBAN_DB`` pins it (injected into workers);
    ``default`` -> ``<root>/kanban.db`` (back-compat), else the board dir."""
    return _board_path("HERMES_KANBAN_DB", board, ("kanban.db",), "kanban.db")


def workspaces_root(board: Optional[str] = None) -> Path:
    """Per-board scratch workspace root (``HERMES_KANBAN_WORKSPACES_ROOT`` wins);
    ``default`` keeps the legacy ``<root>/kanban/workspaces/``."""
    return _board_path("HERMES_KANBAN_WORKSPACES_ROOT", board, ("kanban", "workspaces"), "workspaces")


def attachments_root(board: Optional[str] = None) -> Path:
    """Per-board attachments root (``HERMES_KANBAN_ATTACHMENTS_ROOT`` wins). Workers
    read attachments by absolute path, so remote terminal backends must mount it."""
    return _board_path("HERMES_KANBAN_ATTACHMENTS_ROOT", board, ("kanban", "attachments"), "attachments")


def task_attachments_dir(task_id: str, board: Optional[str] = None) -> Path:
    """Return the per-task attachment directory ``<root>/<task_id>/``."""
    return attachments_root(board=board) / task_id


def worker_logs_dir(board: Optional[str] = None) -> Path:
    """Per-board worker log dir (logs follow the board so ``hermes kanban log``
    is unambiguous when two boards share a task id)."""
    return _board_path(None, board, ("kanban", "logs"), "logs")


def board_store_path(board: Optional[str] = None) -> Path:
    """``board``'s OWN store, resolved from the kanban home - **never** the env pins.

    The pin (``HERMES_KANBAN_DB``) is a property of the CALLING PROCESS, not of the slug
    asked about: under a pin :func:`kanban_db_path` answers the PINNED file for every slug,
    so a cross-board probe that asks it "where is board *platform*?" is handed defcon's
    store and then reports whichever slug it happened to try first (measured 2026-10-13:
    every defcon card answered ``'default'``). Use :func:`kanban_db_path` for "the store
    MY process writes to"; use this one for "the store this SLUG names" - and if that
    board's file is not there, the caller must answer "cannot be told" rather than
    fabricate a slug (design ruling D3, card t_2187558e).
    """
    slug = board if board else get_current_board()
    return _slug_board_path(slug, ("kanban.db",), "kanban.db")


def board_for_store_path(path: Any) -> Optional[str]:
    """The slug of the board whose canonical store IS ``path``, else ``None``.

    The single layout->board rule (``<root>/kanban.db`` = ``default``, else
    ``<root>/kanban/boards/<slug>/<leaf>``), shared by :func:`board_for_connection`
    and by anything that must ask "is this file a board store, and whose?".
    """
    if not path:
        return None
    try:
        resolved = Path(path).resolve()
        if resolved == (kanban_home() / "kanban.db").resolve():
            return DEFAULT_BOARD
        rel = resolved.relative_to(boards_root().resolve())
    except (OSError, ValueError):
        return None
    return rel.parts[0] if len(rel.parts) > 1 else None


def board_for_connection(conn: sqlite3.Connection) -> Optional[str]:
    """The slug of the board ``conn`` is open against, or ``None`` when it cannot be told.

    A filing must be bounded by the board its OWN connection belongs to: a caller holding
    a ``--board`` override is not on ``get_current_board()``, and ``HERMES_KANBAN_DB`` can
    pin a database no board claims (workers are spawned with exactly that). So the slug is
    read off the open file's path, never off the ambient current board.
    """
    path = ""
    for _seq, name, file in conn.execute("PRAGMA database_list"):
        if name == "main" and file:
            path = file
    return board_for_store_path(path)


def source_board_for_task(task_id: Optional[str]) -> Optional[str]:
    """The board holding ``task_id``, probed across every registered board.

    A card fired from another card lands on the SAME board as the card that fired
    it, so the source card's board is read from the board STORES (a fact) rather
    than from any pin or name in the callers' environment. ``None`` when the id
    names no card on any board; the caller then keeps the ambient board.
    """
    if not task_id:
        return None
    try:
        # Lazy: kanban_register imports this module at module level, so the probe
        # is imported here rather than at the top (no import cycle).
        from hermes_cli.kanban_register import find_card_board
        return find_card_board(task_id)
    except Exception:
        return None


def board_for_fired_card(explicit: Optional[str] = None, *,
                         source_task_id: Optional[str] = None) -> Optional[str]:
    """The board a newly FIRED card lands on: explicit > SOURCE CARD > ambient.

    THE RULE (operator standing order, 2026-09-29): a card fired from another card
    lands on the SAME board as the card that fired it. The SOURCE card's board
    therefore outranks the ambient board - ``HERMES_KANBAN_BOARD`` / the
    ``<root>/kanban/current`` pointer - which is a property of the CALLING PROCESS,
    not of the work, and is only a fallback for a filing with NO source card: a
    loop, a cron row, a sweep. It can be stale or simply wrong (measured
    2026-09-29: three cards fired from a defcon card landed on a lane-named board
    nobody grooms, invisibly to every board that gets read). The source card's
    board is the work's own estate, so it wins unconditionally.

    An EXPLICIT board must resolve to a REGISTERED board or the filing is REFUSED
    (:class:`BoardResolutionError`) — a filing never MINTs a board. A name-shaped
    slug is exactly the case this refusal exists for: registration is deliberate
    (``create_board`` writes ``board.json``), minting is not, so a caller that means
    a board registers it first or omits ``board`` to inherit the source/default
    board.

    Returns ``None`` when neither an explicit board nor a source board applies,
    which is what keeps the ambient chain (``env pin -> current -> default``)
    byte-for-byte as it was for every source-less caller.
    """
    if explicit:
        # The ONE refusal (shared with the tool board resolver via
        # :func:`unregistered_board_refusal`) — a duplicated message would let the
        # two doors into a store drift.
        return _require_registered_board(str(explicit))
    return source_board_for_task(source_task_id)


def board_has_live_claims(board: Optional[str] = None) -> bool:
    """True when the board's store carries at least one LIVE claim.

    LIVE is the dispatcher's own reading: ``status = 'running'`` with an unexpired
    ``claim_expires`` and a recorded ``worker_pid``. Used by the estate teardown to
    refuse while a worker is still running on the board (ruling t_fcf7a321,
    Decision 2). Any read failure is ``False`` (the caller's other refusals still
    apply); this never opens a write path.
    """
    slug = _slug_or_default(board)
    try:
        db = kanban_db_path(board=slug)
    except Exception:
        return False
    if not db.exists():
        return False
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
        try:
            row = conn.execute(
                "SELECT 1 FROM tasks WHERE status = 'running' AND worker_pid IS NOT NULL "
                "  AND claim_expires IS NOT NULL AND claim_expires > ? LIMIT 1",
                (int(time.time()),),
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except Exception:
        return False


# --- Data classes ---

@dataclass
class Task:
    """In-memory view of a row from the ``tasks`` table."""

    id: str
    title: str
    body: Optional[str]
    assignee: Optional[str]
    status: str
    priority: int
    created_by: Optional[str]
    created_at: int
    started_at: Optional[int]
    completed_at: Optional[int]
    workspace_kind: str
    workspace_path: Optional[str]
    claim_lock: Optional[str]
    claim_expires: Optional[int]
    tenant: Optional[str]
    branch_name: Optional[str] = None
    project_id: Optional[str] = None
    result: Optional[str] = None
    idempotency_key: Optional[str] = None
    # Column semantics: see SCHEMA_SQL.
    consecutive_failures: int = 0
    worker_pid: Optional[int] = None
    last_failure_error: Optional[str] = None
    max_runtime_seconds: Optional[int] = None
    last_heartbeat_at: Optional[int] = None
    current_run_id: Optional[int] = None
    workflow_template_id: Optional[str] = None
    current_step_key: Optional[str] = None
    skills: Optional[list] = None            # None = defaults only; [] = explicitly none
    model_override: Optional[str] = None
    provider_override: Optional[str] = None  # provider ``model_override`` belongs to
    reasoning_effort: Optional[str] = None   # VALID_REASONING_EFFORTS | "none"; NULL = profile's
    # Breaker trip count; None -> ``kanban.failure_limit`` -> DEFAULT_FAILURE_LIMIT.
    max_retries: Optional[int] = None
    # ``/goal``-style loop: a judge re-checks each turn IN THE SAME SESSION until
    # done / budget exhausted (-> kanban_block); ``goal_max_turns`` None -> goals default.
    goal_mode: bool = False
    goal_max_turns: Optional[int] = None
    session_id: Optional[str] = None         # originating HERMES_SESSION_ID; NULL from CLI/dashboard
    # VALID_BLOCK_KINDS or None (legacy); kept across unblock so a same-kind re-block reads as a loop.
    block_kind: Optional[str] = None
    block_recurrences: int = 0               # unblock-loop counter, see BLOCK_RECURRENCE_LIMIT
    completion_contract: Optional[str] = None
    # Absolute epoch seconds at which a ``scheduled``/``blocked`` card becomes DUE.
    # The RELEASE leg is the deployed ``maintenance-sweep`` workflow's
    # ``scheduled_wake`` node (``scripts/kanban_unblocker_nodes/scheduled_wake.py``) --
    # nothing in this tree ticks. NULL = parked with no clock fence, i.e. the human
    # hold, which is a first-class state and never auto-released.
    due_at: Optional[int] = None
    # VALID_DUE_WINDOW_POLICIES; NULL reads as "defer". Recorded with the fence so a
    # release can say which policy the fence was armed under.
    due_window_policy: Optional[str] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Task:
        g = lambda col, default=None: _lossy_text(_row_get(row, col, default))
        parsed = _json_or(g("skills"))
        skills_value = [str(s) for s in parsed if s] if isinstance(parsed, list) else None
        return cls(
            **{col: _lossy_text(row[col]) for col in _TASK_REQUIRED_COLUMNS},
            **{col: g(col) for col in _TASK_OPTIONAL_COLUMNS},
            **{col: g(col) or None for col in _TASK_EMPTY_IS_NULL_COLUMNS},
            # Pre-migration fallbacks (spawn_failures / last_spawn_error) are only
            # reachable on a DB never opened since the rename migration landed.
            consecutive_failures=g("consecutive_failures", g("spawn_failures", 0)),
            last_failure_error=g("last_failure_error", g("last_spawn_error")),
            skills=skills_value,
            goal_mode=bool(g("goal_mode")),
            block_recurrences=int(g("block_recurrences") or 0),
        )


# Columns every schema version has (KeyError if the SELECT omitted them).
_TASK_REQUIRED_COLUMNS = (
    "id", "title", "body", "assignee", "status", "priority", "created_by", "created_at",
    "started_at", "completed_at", "workspace_kind", "workspace_path", "claim_lock", "claim_expires",
)
# Later-added columns read as NULL when absent from the row.
_TASK_OPTIONAL_COLUMNS = (
    "branch_name", "project_id", "tenant", "result", "idempotency_key", "worker_pid",
    "max_runtime_seconds", "last_heartbeat_at", "current_run_id", "workflow_template_id",
    "current_step_key", "max_retries", "session_id", "completion_contract", "due_at",
)
# Text columns where "" is stored/read as "not set".
_TASK_EMPTY_IS_NULL_COLUMNS = (
    "model_override", "provider_override", "reasoning_effort", "goal_max_turns", "block_kind",
    "due_window_policy",
)


@dataclass
class Run:
    """One attempt at a task (``task_runs`` row): opened on claim, closed on
    complete/block/crash/timeout/reclaim; carries the handoff summary."""

    id: int
    task_id: str
    profile: Optional[str]
    step_key: Optional[str]
    status: str
    claim_lock: Optional[str]
    claim_expires: Optional[int]
    worker_pid: Optional[int]
    max_runtime_seconds: Optional[int]
    last_heartbeat_at: Optional[int]
    started_at: int
    ended_at: Optional[int]
    outcome: Optional[str]
    summary: Optional[str]
    metadata: Optional[dict]
    error: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Run:
        return cls(
            **{
                col: _lossy_text(row[col]) for col in (
                    "task_id", "profile", "step_key", "status", "claim_lock", "claim_expires",
                    "worker_pid", "max_runtime_seconds", "last_heartbeat_at", "outcome", "summary", "error",
                )
            },
            id=int(row["id"]),
            started_at=int(row["started_at"]),
            ended_at=_opt_int(row["ended_at"]),
            metadata=_json_or(_lossy_text(row["metadata"])),
        )


@dataclass
class Comment:
    id: int
    task_id: str
    author: str
    body: str
    created_at: int

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> Comment:
        return cls(
            id=r["id"], task_id=r["task_id"], author=_lossy_text(r["author"]),
            body=_lossy_text(r["body"]), created_at=r["created_at"],
        )


@dataclass
class Attachment:
    """In-memory view of a row from the ``task_attachments`` table."""

    id: int
    task_id: str
    filename: str
    stored_path: str
    content_type: Optional[str]
    size: int
    uploaded_by: Optional[str]
    created_at: int

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> Attachment:
        return cls(
            id=r["id"], task_id=r["task_id"], filename=r["filename"],
            stored_path=r["stored_path"], content_type=r["content_type"],
            size=r["size"] or 0, uploaded_by=r["uploaded_by"], created_at=r["created_at"],
        )


@dataclass
class Event:
    id: int
    task_id: str
    kind: str
    payload: Optional[dict]
    created_at: int
    run_id: Optional[int] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Event:
        run_id = _row_get(row, "run_id")
        return cls(
            id=row["id"], task_id=row["task_id"], kind=_lossy_text(row["kind"]),
            payload=_json_or(_lossy_text(row["payload"])), created_at=row["created_at"], run_id=_opt_int(run_id),
        )


# --- Schema ---

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id                   TEXT PRIMARY KEY,
    title                TEXT NOT NULL,
    body                 TEXT,
    assignee             TEXT,
    status               TEXT NOT NULL,
    priority             INTEGER DEFAULT 0,
    created_by           TEXT,
    created_at           INTEGER NOT NULL,
    started_at           INTEGER,
    completed_at         INTEGER,
    workspace_kind       TEXT NOT NULL DEFAULT 'scratch',
    workspace_path       TEXT,
    branch_name          TEXT,
    -- Optional link to a first-class Project (hermes_cli/projects_db). When set,
    -- the task's worktree is anchored under the project's primary repo with a
    -- deterministic branch name instead of a random wt/<task-id> fallback.
    project_id           TEXT,
    claim_lock           TEXT,
    claim_expires        INTEGER,
    tenant               TEXT,
    result               TEXT,
    idempotency_key      TEXT,
    -- Unified consecutive-failure counter. Incremented on spawn
    -- failure, timeout, or crash; reset only on successful completion.
    -- The circuit breaker in _record_task_failure trips when this
    -- exceeds DEFAULT_FAILURE_LIMIT consecutive non-successes.
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    worker_pid           INTEGER,
    -- Restart-stable fingerprint of worker_pid ("<boot/instantiation epoch>|<start time>",
    -- kanban_db_dispatch._process_fingerprint) recorded at spawn: liveness and kills require pid
    -- AND fingerprint to agree, so a PID recycled after a reboot is never read as our worker or
    -- signalled. NULL = legacy row (pre-fingerprint spawn); 'unverified' = capture failed at
    -- spawn (held while live, never signalled). Column keeps its INTEGER affinity for the
    -- start-time-only integer values older rows carry.
    worker_started_at    INTEGER,
    -- Short excerpt of the most recent failure's error text.
    last_failure_error   TEXT,
    max_runtime_seconds  INTEGER,
    last_heartbeat_at    INTEGER,
    -- Pointer into task_runs for the currently-active run (NULL if no
    -- run is in-flight). Denormalised for cheap reads.
    current_run_id       INTEGER,
    -- Forward-compat for v2 workflow routing. In v1 the kernel writes
    -- these when the task is opted into a template but otherwise ignores
    -- them; the dispatcher doesn't consult them for routing yet.
    workflow_template_id TEXT,
    current_step_key     TEXT,
    -- Force-loaded skills for the worker on this task, stored as JSON.
    -- Passed to the worker via `--skills`. NULL or empty array = no extras.
    skills               TEXT,
    -- Per-task model override. When set, the dispatcher passes -m <model>
    -- to the worker, overriding the profile's default model. NULL = use
    -- the profile default.
    model_override       TEXT,
    -- Provider the model override belongs to. When set (alongside
    -- model_override), the dispatcher passes --provider <name> so the
    -- worker resolves the model against the right backend instead of the
    -- profile's configured provider. NULL = profile provider.
    provider_override    TEXT,
    -- Per-task reasoning effort for the worker (minimal|low|medium|high|
    -- xhigh|max|ultra, or 'none' for thinking off). When set, the dispatcher
    -- passes --reasoning <level> so the worker runs at that depth regardless
    -- of the profile's agent.reasoning_effort. NULL = profile setting.
    reasoning_effort     TEXT,
    -- Per-task override for the consecutive-failure circuit breaker.
    -- The value is the failure count at which the breaker trips — e.g.
    -- ``max_retries=1`` blocks on the first failure. NULL (the common
    -- case) falls through to the dispatcher-level ``kanban.failure_limit``
    -- config and then ``DEFAULT_FAILURE_LIMIT``.
    max_retries          INTEGER,
    -- When 1, the dispatched worker runs in a Ralph-style goal loop: an
    -- auxiliary judge re-evaluates the worker's response against the
    -- card title/body after each turn and feeds a continuation prompt
    -- back into the SAME session until the judge agrees the work is done
    -- or ``goal_max_turns`` is exhausted. NULL/0 = classic single-shot
    -- worker (the default).
    goal_mode            INTEGER NOT NULL DEFAULT 0,
    -- Goal-loop turn budget for ``goal_mode`` workers. NULL = use the
    -- goals-engine default.
    goal_max_turns       INTEGER,
    -- Originating chat/agent session id when the task was created from
    -- inside an agent loop that propagated ``HERMES_SESSION_ID``. NULL
    -- for tasks created from the CLI, dashboard, or any path that doesn't
    -- set the env var, and for an id with no ``sessions`` row in this
    -- profile's state.db (kanban_create verifies before stamping). Indexed
    -- so per-session list queries stay cheap on larger boards.
    session_id           TEXT,
    -- Typed block reason set by ``block_task`` (one of VALID_BLOCK_KINDS, or
    -- NULL for legacy/un-typed blocks). Drives routing: ``dependency`` never
    -- sits in ``blocked`` (goes to ``todo`` for parent-gating); the others go
    -- to ``blocked`` for a human. Preserved across unblock so a re-block for
    -- the SAME kind can be recognised as a loop.
    block_kind           TEXT,
    -- Unblock-loop counter. Incremented each time a task is re-blocked for the
    -- same truly-blocked reason after having been unblocked. When it reaches
    -- BLOCK_RECURRENCE_LIMIT the task is routed to ``triage`` instead of
    -- ``blocked`` so a cron can't spin it forever. Reset to 0 only on a
    -- successful completion — NOT on unblock (resetting on unblock is exactly
    -- the amnesia that let the loop run unbounded).
    block_recurrences    INTEGER NOT NULL DEFAULT 0,
    -- Absolute epoch seconds at which a ``scheduled``/``blocked`` card becomes DUE.
    -- Set by ``schedule_task(due_at=...)`` / ``hermes kanban schedule --due`` and by
    -- ``block_task(due_at=...)`` / ``hermes kanban block --due``. The RELEASE leg is
    -- the deployed ``maintenance-sweep`` workflow's ``scheduled_wake`` node; nothing
    -- in this process ticks. NULL = parked with no clock fence (the human hold --
    -- waiting on a person, not on time), which is never auto-released.
    due_at               INTEGER,
    -- One of VALID_DUE_WINDOW_POLICIES; NULL reads as 'defer'. Recorded with the
    -- fence so the release leg can say which policy the fence was armed under.
    due_window_policy    TEXT
);

CREATE TABLE IF NOT EXISTS task_links (
    parent_id  TEXT NOT NULL,
    child_id   TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id)
);

CREATE TABLE IF NOT EXISTS task_comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    author     TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS task_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    run_id     INTEGER,
    kind       TEXT NOT NULL,
    payload    TEXT,
    created_at INTEGER NOT NULL
);

-- Historical attempt record. Each time the dispatcher claims a task, a
-- new row is created here; claim state, PID, heartbeat, runtime cap,
-- and structured summary all live on the run, not the task. Multiple
-- rows per task id when the task was retried after crash/timeout/block.
-- v2 of the kanban schema will use ``step_key`` to drive per-stage
-- workflow routing; in v1 the column is nullable and unused (kernel
-- ignores it).
CREATE TABLE IF NOT EXISTS task_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             TEXT NOT NULL,
    profile             TEXT,
    step_key            TEXT,
    status              TEXT NOT NULL,
    -- status: running | done | blocked | crashed | timed_out | failed | released
    claim_lock          TEXT,
    claim_expires       INTEGER,
    worker_pid          INTEGER,
    -- Spawn-time start fingerprint of worker_pid (see tasks.worker_started_at). Retained with
    -- worker_pid after the run ends so a worker that outlives its terminal transition can
    -- still be found and reaped; NULL = legacy row, never signalled.
    worker_started_at   INTEGER,
    max_runtime_seconds INTEGER,
    last_heartbeat_at   INTEGER,
    started_at          INTEGER NOT NULL,
    ended_at            INTEGER,
    outcome             TEXT,
    -- outcome: completed | blocked | crashed | timed_out | spawn_failed |
    --          gave_up | reclaimed | (null while still running)
    summary             TEXT,
    metadata            TEXT,
    error               TEXT
);

-- Files attached to a task (PDFs, images, source documents). The blob
-- lives on disk under ``attachments_root(board)/<task_id>/<stored_name>``;
-- this row carries metadata + the absolute ``stored_path`` so the
-- dashboard can list/download and ``build_worker_context`` can surface
-- the absolute path to the worker (which has full file-tool access). See
-- #35338.
CREATE TABLE IF NOT EXISTS task_attachments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL,
    filename     TEXT NOT NULL,
    stored_path  TEXT NOT NULL,
    content_type TEXT,
    size         INTEGER NOT NULL DEFAULT 0,
    uploaded_by  TEXT,
    created_at   INTEGER NOT NULL
);

-- Subscription from a gateway source (platform + chat + thread) to a
-- task. The gateway's kanban-notifier watcher tails task_events and
-- pushes ``completed`` / ``blocked`` / ``spawn_auto_blocked`` events to
-- the original requester so human-in-the-loop workflows close the loop.
CREATE TABLE IF NOT EXISTS kanban_notify_subs (
    task_id       TEXT NOT NULL,
    platform      TEXT NOT NULL,
    chat_id       TEXT NOT NULL,
    thread_id     TEXT NOT NULL DEFAULT '',
    user_id       TEXT,
    user_id_alt   TEXT,
    chat_type     TEXT,
    notifier_profile TEXT,
    delivery_mode TEXT NOT NULL DEFAULT 'notify',
    delivery_metadata TEXT,
    created_at    INTEGER NOT NULL,
    last_event_id INTEGER NOT NULL DEFAULT 0,
    last_ping_event_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, platform, chat_id, thread_id)
);

-- The designation ledger: the cards ALLOWED to hold a reserved-tranche priority, one row per
-- designated card (no new database - the board's own store). ``hermes kanban defcon`` is the
-- only writer, and the opt-in tranche storage guard refuses a tranche value on a card with no
-- live row here. ``priority`` is the card's ORDINARY priority: what a revoke restores.
CREATE TABLE IF NOT EXISTS priority_designations (
    task_id       TEXT PRIMARY KEY,
    board         TEXT NOT NULL,
    priority      INTEGER NOT NULL,
    authority     TEXT,
    reason        TEXT NOT NULL,
    designated_at TEXT NOT NULL,
    revoked_at    TEXT
);

CREATE TABLE IF NOT EXISTS kanban_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status          ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_links_child           ON task_links(child_id);
CREATE INDEX IF NOT EXISTS idx_links_parent          ON task_links(parent_id);
CREATE INDEX IF NOT EXISTS idx_comments_task         ON task_comments(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_task           ON task_events(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_runs_task             ON task_runs(task_id, started_at);
CREATE INDEX IF NOT EXISTS idx_runs_status           ON task_runs(status);
CREATE INDEX IF NOT EXISTS idx_attachments_task      ON task_attachments(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_notify_task           ON kanban_notify_subs(task_id);
-- NOTE: `idx_tasks_due` is deliberately NOT here. `executescript` parses every
-- statement against the LIVE schema, so a CREATE INDEX over `due_at` aborts init
-- on a legacy board that predates the column, before the ALTER TABLE pass can add
-- it. It is created with the other additive-column indexes at the end of
-- `_migrate_add_optional_columns` (hermes_cli/kanban_db_connect.py).
"""


# --- ID generation ---

def _new_task_id() -> str:
    """``t_`` + 4 hex bytes (collision ~1e-3 at 100k tasks; 2 bytes would hit 50%
    by 10k). Idempotency belongs to ``idempotency_key``, not id uniqueness."""
    return "t_" + secrets.token_hex(4)


def _claimer_id() -> str:
    """Return a ``host:pid`` string that identifies this claimer."""
    import socket
    try:
        host = socket.gethostname() or "unknown"
    except Exception:
        host = "unknown"
    return f"{host}:{os.getpid()}"


def _host_prefix() -> str:
    """``"<host>:"`` prefix shared by every claim lock issued from this host."""
    return f"{_claimer_id().split(':', 1)[0]}:"


# --- Task creation / mutation ---

def _validate_model_override(model: Optional[str], provider: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Strip both; a provider without a model is rejected (a bare ``--provider``
    would re-resolve the profile's model against another backend — exactly
    the mismatch the override exists to kill)."""
    model = (model or "").strip() or None
    provider = (provider or "").strip() or None
    if provider and not model:
        raise ValueError("provider_override requires a model_override")
    return model, provider


def _canonical_assignee(assignee: Optional[str]) -> Optional[str]:
    """Lowercase-assignee normalization for Kanban rows (dashboard/CLI parity)."""
    if assignee is None:
        return None
    from hermes_cli.profiles import normalize_profile_name

    return normalize_profile_name(assignee)


def assignee_is_spawnable(assignee: Optional[str]) -> bool:
    """Can the dispatcher launch a worker for ``assignee`` TODAY?

    Deliberately the dispatcher's own predicate — ``profile_exists`` from
    ``hermes_cli.profiles``, resolved at call time through a lazy import so the
    dispatcher's monkeypatched seam and this one are the same seam — NOT a
    private directory check that could disagree with what
    ``kanban_db_dispatch`` actually spawns. Fails OPEN (returns ``True``) when
    the profiles module is unimportable or raises, matching the dispatcher's
    trust-the-operator fallback. An unassigned card is also ``True`` here: the
    dispatcher buckets that case separately (``skipped_unassigned``) and
    ``kanban.default_assignee`` may still resolve it.
    """
    name = _canonical_assignee(assignee)
    if not name:
        return True
    try:
        from hermes_cli.profiles import profile_exists
    except Exception:
        return True
    try:
        return bool(profile_exists(name))
    except Exception:
        return True


def _resolve_project_link(
    conn: sqlite3.Connection, project_id: Optional[str], project_source_task_id: Optional[str],
    workspace_kind: str, workspace_path: Optional[str],
) -> tuple[Optional[str], Any, Optional[str], str]:
    """``(project_id, project_obj, project_repo, workspace_kind)`` for ``create_task``.

    A project-linked task is anchored to the project's primary repo as a
    worktree with a deterministic branch (slug + task id). Projects live in the
    creator's per-profile projects.db, but the stored repo path is absolute so
    the cross-profile dispatcher needs no projects.db access. ``project_repo``
    is set when the worktree path must still be derived from the new task id.
    """
    project_id = (str(project_id).strip() or None) if project_id is not None else None
    if not project_id:
        return None, None, None, workspace_kind
    from hermes_cli import projects_db as _pdb

    project_repo: Optional[str] = None
    try:
        with _pdb.connect_closing() as _pconn:
            project_obj = _pdb.get_project(_pconn, project_id)
    except Exception:
        project_obj = None
    if project_obj is None and project_source_task_id:
        project_obj, project_repo = _project_from_source_task(
            conn, _pdb, project_id, str(project_source_task_id),
        )
        if project_obj is not None and workspace_kind == "scratch":
            workspace_kind = "worktree"
    if project_obj is None:
        # Unresolvable id/slug: drop the link (never a dangling reference,
        # never a crash) and create an ordinary scratch task.
        return None, None, None, workspace_kind
    # Canonicalise (a slug may have been passed) and anchor the worktree
    # under the project's primary repo.
    if workspace_kind == "scratch" and project_obj.primary_path:
        workspace_kind = "worktree"
    if workspace_kind == "worktree" and workspace_path is None and project_obj.primary_path:
        # A primary that is an EXISTING folder but not a git repo can never anchor a worktree
        # (t_e5ff985b): the card would be born with an impossible
        # ``<primary>/.worktrees/<id>`` and die at its first claim. Refuse at BIRTH — the
        # honest answer names the project and the fix instead of filing a card that can never
        # run. A primary that does not exist YET is left to the claim seam (which blocks the
        # card on its first failure and annotates it), because registering a project before
        # cloning its repo is a legitimate order; registering a plain folder as its repo is not.
        from pathlib import Path as _Path

        from hermes_cli import kanban_db_workspace as _wsm

        _primary = _Path(str(project_obj.primary_path)).expanduser()
        if _primary.exists():
            _wsm.assert_repo_anchor(
                _primary,
                project_id=project_obj.id,
                project_slug=project_obj.slug,
            )
        # Concrete path is deferred to the insert loop: a fresh
        # ``<repo>/.worktrees/<task-id>`` keyed on the new task id.
        project_repo = str(project_obj.primary_path)
    return project_obj.id, project_obj, project_repo, workspace_kind


def _warn_unresolved_project_link(project_id: str, *, source: str) -> dict:
    """The warning + the ``created``-event note for a project link this registry cannot resolve.

    ``_resolve_project_link`` drops such a link by design (never a dangling reference, never a
    crash) — but the registry is PER-PROFILE (``$HERMES_HOME/projects.db``), so the drop is easy
    to walk into: a caller that passed ``--project``, or a board scoped to a project another
    profile's registry holds, gets an ordinary scratch card and — before this seam — never
    learned that its link did not land. Measured on the workstream board: 13 of 23 rows left
    ``project_id`` NULL against 10 carrying the project.

    A silent drop is a silent failure, so the drop STAYS and is made LOUD: a stderr warning that
    names the ident, the registry that was searched and the two honest paths, plus the fields the
    ``created`` event carries (``project_link_unresolved``, ``..._source``, ``..._registry``).
    Never raises — a warning must never be the thing that fails a filing.
    """
    try:
        from hermes_cli import projects_db as _pdb

        registry = str(_pdb.projects_db_path())
    except Exception:
        registry = "this profile's projects.db"
    asked_as = "--project" if source == "argument" else "the board's default project"
    print(
        f"warning: kanban: {asked_as} {project_id!r} is not in this profile's project registry "
        f"({registry}); the card was filed WITHOUT the project link. Create the project in this "
        f"profile (`hermes project create <name> --slug {project_id}`) or link by SLUG from the "
        f"registry that owns it (`hermes project list`).",
        file=sys.stderr,
    )
    _log.warning(
        "kanban: unresolved project link %r (source=%s) - card filed unscoped", project_id, source,
    )
    return {
        "project_link_unresolved": project_id,
        "project_link_source": source,
        "project_link_registry": registry,
    }


def _project_from_source_task(
    conn: sqlite3.Connection, _pdb: Any, project_id: str, source_task_id: str,
) -> tuple[Any, Optional[str]]:
    """Recover a Project (and its repo) from a canonical project-linked
    worktree task on this board. Worker profiles have their own projects.db
    while the Kanban DB is shared, so this carries the repo + branch
    convention forward without opening the creator's store and without
    reusing the source task's literal worktree path. ``(None, None)`` when
    the source task is not a ``<repo>/.worktrees/<id>`` project worktree."""
    source_task = get_task(conn, source_task_id)
    if not (
        source_task is not None
        and source_task.project_id == project_id
        and source_task.workspace_kind == "worktree"
        and source_task.workspace_path
    ):
        return None, None
    source_path = Path(source_task.workspace_path)
    if not (
        source_path.is_absolute()
        and source_path.name == source_task.id
        and source_path.parent.name == ".worktrees"
    ):
        return None, None
    project_slug = None
    if source_task.branch_name:
        prefix, separator, leaf = source_task.branch_name.partition("/")
        if separator and (leaf == source_task.id or leaf.startswith(f"{source_task.id}-")):
            with contextlib.suppress(ValueError):
                project_slug = _pdb.normalize_slug(prefix)
    if project_slug is None:
        with contextlib.suppress(ValueError):
            project_slug = _pdb.normalize_slug(project_id)
    if not project_slug:
        return None, None
    project_repo = str(source_path.parent.parent)
    project_obj = _pdb.Project(
        id=project_id, slug=project_slug, name=project_slug, created_at=0, primary_path=project_repo,
    )
    return project_obj, project_repo


def _normalize_task_skills(skills: Optional[Iterable[str]]) -> Optional[list[str]]:
    """Strip/dedupe a skills list. Commas are refused (a comma-joined string must
    not land in one argv slot); toolset names are rejected all at once because
    agents that confuse the two usually pass several."""
    if skills is None:
        return None
    cleaned: list[str] = []
    seen: set[str] = set()
    toolset_typos: list[str] = []
    for s in skills:
        if not s:
            continue
        name = str(s).strip()
        if not name:
            continue
        if "," in name:
            raise ValueError(
                f"skill name cannot contain comma: {name!r} "
                f"(pass a list of separate names instead of a comma-joined string)"
            )
        if name.casefold() in KNOWN_TOOLSET_NAMES:
            toolset_typos.append(name)
            continue
        if name in seen:
            continue
        seen.add(name)
        cleaned.append(name)
    if toolset_typos:
        quoted = ", ".join(repr(n) for n in toolset_typos)
        noun = "is a toolset name" if len(toolset_typos) == 1 else "are toolset names"
        raise ValueError(
            f"{quoted} {noun}, not skill name(s). "
            "Put toolsets in the assignee profile's `toolsets:` config "
            "instead of per-task skills. Skills are named skill bundles "
            "(e.g. `blogwatcher`, `github-code-review`); toolsets are runtime "
            "capabilities (e.g. `web`, `browser`, `terminal`)."
        )
    return cleaned


def _unloadable_task_skills(skills: Iterable[str], assignee: Optional[str]) -> list[dict[str, str]]:
    """``skills`` entries a worker launched as *assignee* could not load.

    A worker resolves ``--skills`` against ITS OWN profile's skill directories and exits
    on ``Unknown skill(s)`` before the agent starts, so a card carrying one of these
    names spends an attempt and auto-blocks with the reason only the worker log shows.
    Ask the worker's own question instead (``agent.skill_commands``).

    Empty when the question cannot be answered truthfully: an unassigned card cannot
    dispatch at all (``assign_task`` attaches the profile), and an unresolvable profile
    home is that profile's own error — neither has a registry to judge names against.
    """
    if not skills or not assignee:
        return []
    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from agent.skill_commands import preload_skill_problems

    try:
        profile_home = resolve_profile_env(normalize_profile_name(assignee))
    except Exception:  # health: allow BLE001 -- fail-open: an unresolvable profile home is that profile's own error, so no registry exists to judge these names against and the gate stays silent
        return []
    override = set_hermes_home_override(profile_home)
    try:
        return preload_skill_problems(list(skills))
    finally:
        reset_hermes_home_override(override)


def _unloadable_skills_message(problems: list[dict[str, str]], assignee: Optional[str]) -> str:
    """Refusal text for skill names the assignee profile's worker would exit on."""
    quoted = ", ".join(repr(problem["identifier"]) for problem in problems)
    noun = "skill" if len(problems) == 1 else "skills"
    lines = [
        f"{noun} {quoted} cannot be loaded by a worker for assignee {assignee!r}, "
        "so the task would fail on 'Unknown skill(s)' before the agent starts."
    ]
    for problem in problems:
        closest = problem.get("closest") or ""
        lines.append(
            f"  {problem['identifier']}: {problem.get('error') or 'not registered for that profile'}"
            + (f" Closest name(s): {closest}." if closest else "")
        )
    lines.append(f"List that profile's skills with `hermes -p {assignee} skills list`.")
    return "\n".join(lines)


def _task_skill_names(skills: Any) -> list[str]:
    """Skill names off a task row's JSON ``skills`` column (or a raw list of names)."""
    parsed = skills if isinstance(skills, (list, tuple)) else _json_or(skills)
    return [str(name) for name in parsed if name] if isinstance(parsed, list) else []


def _refuse_unloadable_skills(skills: Any, assignee: Optional[str]) -> None:
    """Raise :class:`ValueError` for stored skill names *assignee*'s worker cannot load.

    ``create_task`` asks this where the card is created; every site that ATTACHES an
    assignee afterwards asks it again (``assign_task``, ``request_review``, the
    dispatcher's ``kanban.default_assignee`` fallback), because the pair can only be
    judged once the profile is known. A card created with no assignee therefore
    reaches a worker with its names unjudged and spends its attempts on
    ``Unknown skill(s)`` — the failure the create-time check exists to prevent.
    """
    names = _task_skill_names(skills)
    if not names:
        return
    problems = _unloadable_task_skills(names, assignee)
    if problems:
        raise ValueError(_unloadable_skills_message(problems, assignee))


def _resolve_operator_ask(conn: sqlite3.Connection, *, board: Optional[str] = None,
                          parents: Any = (), body: Optional[str] = "",
                          serves: Any = None) -> Any:
    """The :class:`~hermes_cli.kanban_register.AskRef` a filing is stamped with, or ``None``.

    Reads only what the caller and THIS board can answer: an explicit ``serves``, the
    worker-session env, the body's own stamp, then the parents — never another board's
    rows, except through the cross-board probe that resolves a reference naming a card
    elsewhere (the case this whole feature exists for, and only for a ref the caller
    named outright). Nothing here may fail a FILING: a reference that names no card is
    warned about and carried into the ``created`` event as ``operator_ask_unresolved``,
    so it is visible to the roll-up and to whoever reads the card, rather than fatal.

    Restored from the pre-update carrier (card ``t_ac98fc08``) after the 2026-09-29 04:48
    ``hermes-update`` R5 round-trip left this seam a documented stand-in returning ``None``.
    """
    from hermes_cli import kanban_register as reg

    if serves is not None and reg.parse_ref(serves) is None:
        # A malformed reference is a CALLER error, refused here before any write: a
        # guessed id would poison the roll-up, and the caller is the only one who can
        # fix it. (Syntactically valid but unknown ids stay a warning - see below.)
        raise ValueError(
            f"serves must be a card id or <register>/<ask>, got {serves!r}; a card id "
            f"is 't_' + hex (e.g. t_fc615201). Nothing was created."
        )
    env = os.environ.get(reg.ENV_VAR)
    try:
        ref = reg.resolve_for_create(
            conn, board=board, parents=parents, body=body, explicit=serves, env=env,
            find_card=reg.find_card_board if (serves or env) else None,
        )
    except Exception as exc:  # a filing outranks a stamp: never fail the create
        _log.warning("operator-ask resolution failed for a filing: %s", exc)
        return None
    if ref is not None and ref.unresolved:
        _log.warning(
            "no operator-ask stamp on this filing: %r does not resolve to an operator ask "
            "(a register card id, or a card in service of one) - the card is filed "
            "un-stamped and reported by `hermes kanban rollup`",
            ref.unresolved,
        )
    return ref


def operator_ask_event(ask_ref: Any, ask_pair: Optional[tuple[str, str]]) -> dict:
    """The ``created`` event fields recording the ask, or ``{}`` on a card with none."""
    if ask_ref is None:
        return {}
    if ask_pair:
        return {"operator_ask": f"{ask_pair[0]}/{ask_pair[1]}", "operator_ask_source": ask_ref.source}
    if ask_ref.unresolved:
        return {"operator_ask_unresolved": ask_ref.unresolved}
    return {}


#: What a filing does when the key it carries is HELD BY A TOMBSTONE (an ``archived``
#: card). Named, validated, and OPT-IN: the default is what every caller on the host does
#: today, and a default flip is refused (ruling on card ``t_2bc058fc``, leg B
#: ``t_0f97637f``) because it would turn every re-filing caller into a silent no-op.
#:
#:   * ``recreate`` -- mint a second card under the same key (today's behaviour);
#:   * ``return_tombstone`` -- hand back the archived card's id and insert NOTHING;
#:   * ``reopen`` -- hand back that card AND move it back to a live status through
#:     :func:`reopen_task`, one audited act, inserting nothing.
#:
#: Measured on the live ``platform`` board 2026-10-28: 17,309 keyed rows over 14,752
#: distinct keys -- 2,557 duplicate keyed rows -- and 1,357 keys holding BOTH an archived
#: card and a later one: keys that came back from the dead and were re-filed beside
#: their own tombstone.
ARCHIVED_KEY_POLICIES = ("recreate", "return_tombstone", "reopen")
DEFAULT_ARCHIVED_KEY_POLICY = "recreate"


def _norm_archived_key_policy(policy: Any) -> str:
    """The policy NAME, or :class:`ValueError` naming the one that was asked for.

    ``None`` is "the caller did not ask", so it takes the default. Anything else must be
    one of :data:`ARCHIVED_KEY_POLICIES`: guessing here would either file the twin this
    seam exists to prevent or answer a filing with a finished card, and neither is a
    thing to decide on a caller's behalf.
    """
    name = DEFAULT_ARCHIVED_KEY_POLICY if policy is None else policy
    normalized = name.strip().lower() if isinstance(name, str) else name
    if normalized not in ARCHIVED_KEY_POLICIES:
        raise ValueError(
            "archived_key_policy %r is not a policy: one of %s (default %r). Refused by "
            "name rather than guessed." % (
                policy, ", ".join(repr(p) for p in ARCHIVED_KEY_POLICIES),
                DEFAULT_ARCHIVED_KEY_POLICY))
    return normalized


def _archived_key_authority(policy: str, actor: Optional[str],
                            reason: Optional[str]) -> tuple[str, str]:
    """``(actor, reason)`` for a ``reopen`` filing, or the blank pair for any other.

    The reopen lands on ANOTHER card's history, so the kernel takes the authority from
    the caller and REFUSES to invent it: a reopen with no actor is anonymous and one with
    no reason is silent, and both outlive the session that caused them.
    """
    if policy != "reopen":
        return "", ""
    who = (actor or "").strip()
    why = (reason or "").strip()
    if not who:
        raise ValueError(
            "archived_key_policy='reopen' needs archived_key_actor: the reopen is an "
            "AUDITED act on the archived card's own history, and the kernel will not "
            "invent who asked for it.")
    if not why:
        raise ValueError(
            "archived_key_policy='reopen' needs archived_key_reason: the reopened card's "
            "comment carries it verbatim, and an unexplained reopen is a silent one.")
    return who, why


def _keyed_live_task(conn: sqlite3.Connection, key: str) -> Optional[str]:
    """The newest non-archived card holding ``key`` -- the dedupe every caller has today.

    ``done`` is LIVE for this purpose and always has been: the historical lookup was
    ``status != 'archived'``, and the archived-key seam must not quietly change which
    finished cards answer a filing.
    """
    row = conn.execute(
        "SELECT id FROM tasks WHERE idempotency_key = ? "
        "AND status != 'archived' "
        "ORDER BY created_at DESC LIMIT 1", (key,),
    ).fetchone()
    return str(row["id"]) if row else None


def _keyed_archived_task(conn: sqlite3.Connection, key: str) -> Optional[str]:
    """The newest ARCHIVED card holding ``key`` -- the tombstone that frees the key today."""
    row = conn.execute(
        "SELECT id FROM tasks WHERE idempotency_key = ? "
        "AND status = 'archived' "
        "ORDER BY created_at DESC LIMIT 1", (key,),
    ).fetchone()
    return str(row["id"]) if row else None


def create_task(
    conn: sqlite3.Connection, *, title: str, body: Optional[str] = None,
    assignee: Optional[str] = None, created_by: Optional[str] = None,
    workspace_kind: Optional[str] = None, workspace_path: Optional[str] = None,
    branch_name: Optional[str] = None, tenant: Optional[str] = None, priority: int = 0,
    parents: Iterable[str] = (), triage: bool = False, idempotency_key: Optional[str] = None,
    max_runtime_seconds: Optional[int] = None, skills: Optional[Iterable[str]] = None,
    max_retries: Optional[int] = None, model_override: Optional[str] = None,
    provider_override: Optional[str] = None, reasoning_effort: Optional[str] = None,
    goal_mode: bool = False, goal_max_turns: Optional[int] = None, initial_status: str = "running",
    session_id: Optional[str] = None, board: Optional[str] = None, project_id: Optional[str] = None,
    project_source_task_id: Optional[str] = None,
    creator_task_id: Optional[str] = None,
    completion_contract: Optional[str] = None,
    serves: Optional[str] = None,
    allow_off_board_ask: bool = False,
    from_inbox: Optional[str] = None,
    from_inbox_item: Optional[str] = None,
    archived_key_policy: Optional[str] = None,
    archived_key_actor: Optional[str] = None,
    archived_key_reason: Optional[str] = None,
) -> str:
    """Create a task (optionally under ``parents``); returns its id.

    Status: ``ready`` unless a parent is not ``done`` (``todo``); ``triage=True``
    forces ``triage``; ``initial_status="blocked"`` parks it for human ops.
    ``idempotency_key``: an existing non-archived task with the key is returned
    instead of a duplicate. ``archived_key_policy`` is what a filing does when the key
    is held by an ARCHIVED card instead: ``"recreate"`` (the default, and today's
    behaviour -- the archived card does NOT answer, so a second card is minted under
    the same key), ``"return_tombstone"`` (hand back the archived card's id and insert
    nothing) or ``"reopen"`` (that card, moved back to a live status through
    :func:`reopen_task` in one audited act, inserting nothing). An unknown policy name
    is REFUSED by name rather than guessed; ``"reopen"`` additionally requires
    ``archived_key_actor`` and ``archived_key_reason``, because the reopen lands on
    another card's history and neither the actor nor the reason may be invented here.
    A LIVE card under the key always wins, whatever the policy: the seam only decides
    what a TOMBSTONE means.
    A key that legitimately recurs must carry its GENERATION in the key itself -- the
    ``...:g<N>`` vocabulary the SDLC door already mints (``<base>:g1``, ``<base>:g2``,
    card ``t_9f61f782``) -- so a recurrence files a NEW key instead of asking a dedupe
    setting to pretend the same incident is a different one. A generation-less key that
    recurs is a NAMING bug in the caller; no policy here papers over it.
    ``max_runtime_seconds``: cap before the dispatcher
    SIGTERMs and re-queues. ``model_override``/``provider_override`` pin the
    worker model (provider requires model); ``reasoning_effort`` is independent.
    ``creator_task_id``: inherit durable session/subscriptions independently of
    dependency edges; an explicit ``session_id`` still wins.
    ``project_source_task_id``: cross-profile fallback when ``project_id`` is not
    in the active profile's projects.db — see ``_resolve_project_link``.
    ``workspace_kind=None`` (omitted) inherits a project-scoped board's project;
    an explicit ``"scratch"`` or ``project_id=""`` is a request for no project.
    ``serves``: the operator ask this card is filed in service of, as
    ``<register>`` or ``<register>/<ask>`` — see
    ``hermes_cli/kanban_register.py``. The card is stamped in its body at this
    seam (the one moment every filing surface reaches); omitting it still
    inherits an ask from the body, the worker's session env or the parents, in
    that order. ``allow_off_board_ask``: the deliberate escape hatch for a NEW
    ask filed off its register's board (the ASK-HOME rule) — admitted and
    recorded on the card rather than refused.
    ``from_inbox``/``from_inbox_item``: the seat (``<domain>/<profile>``) whose inbox the
    card is filed FROM, and the item's own words as that seat holds them. The
    ``## Inbox item`` block is composed into the body HERE — the one moment every filing
    surface reaches — and the filing is REFUSED (``kanban_inbox.InboxRefusal``, a
    ``ValueError``) before any write when the seat file does not exist or no longer holds
    the item. One gate, two doors: the CLI's ``--from-inbox`` and the ``kanban_create``
    tool both land on this seam, so neither can launder provenance the other checks.
    """
    # THE INBOX-PROVENANCE SEAM (DESIGN section 4 of the seat-directory convention; card
    # t_2081f599 block 4). It runs FIRST, before every other seam that reads the body (the
    # priority policy, the operator-ask stamp) and long before the INSERT, so a filing that
    # cannot prove the seat holds the item leaves the board untouched — the drop is caught
    # one step earlier than the nightly audit that would otherwise find it days later.
    if from_inbox or from_inbox_item:
        from hermes_cli.kanban_inbox import InboxRefusal, with_inbox_provenance

        if not from_inbox:
            raise InboxRefusal(
                "from_inbox_item needs from_inbox: provenance names the SEAT that holds the "
                "item (--from-inbox <domain>/<profile>)"
            )
        if not from_inbox_item:
            raise InboxRefusal(
                "from_inbox needs from_inbox_item: the item's own words are the proof the "
                "seat holds it (--from-inbox-item \"<the item as the seat writes it>\")"
            )
        body = with_inbox_provenance(body, from_inbox, from_inbox_item)

    from hermes_cli.kanban_db_graph import initial_task_state, inherit_creator_origin
    from hermes_cli.kanban_pr_acceptance import needs_repository_checks, validate_contract
    from hermes_cli.kanban_register import (
        OFF_BOARD_ASK_EVENT, apply_stamp, guard_new_ask_home, normalise_body,
    )
    from hermes_cli import kanban_gate_invariants as _gates

    completion_contract = validate_contract(completion_contract)
    model_override, provider_override = _validate_model_override(model_override, provider_override)
    reasoning_effort = normalize_reasoning_effort(reasoning_effort)
    assignee = _canonical_assignee(assignee)
    # THE CONTRACT-VERIFICATION SEAM (cards t_b58332a4 -> t_4bec5cfe). A card born with an
    # ``OWNER/REPO`` / PR-URL contract on a repository that requires NO status checks used to be
    # downgraded to ``local-only`` here, because acceptance answered ``classification=missing``
    # with ``required=[]`` and complete_task parked it with the work already done. That downgrade
    # was one half of a mutual dead end (card t_9082c751): the gate then refused the downgraded
    # card whose own metadata recorded the PR it published. Acceptance now accepts the PR's MERGED
    # state as the evidence, so the declaration is KEPT and only the measured state is recorded.
    # ``landed`` (the deploy-proof class) and every non-repo contract never enter this path, so a
    # card that carries a deployed unit keeps its deploy-proof gate untouched.
    contract_resolution = None
    if needs_repository_checks(completion_contract):
        # A missing/stale resolver module must never fail a FILING (t_b58332a4 review item B):
        # an ImportError here would refuse every OWNER/REPO card, which is worse than the park
        # this seam prevents. Keep the declared contract and warn.
        try:
            from hermes_cli import kanban_contract_resolve as _contracts
        except Exception as _import_exc:
            _log.warning("contract resolution unavailable, keeping declared completion_contract "
                         "%s: %s", completion_contract, _import_exc)
            _contracts = None
        if _contracts is not None:
            _contract_profile_home = None
            try:
                from hermes_cli.kanban_pr_acceptance import _assignee_profile_home
                _contract_profile_home = _assignee_profile_home(assignee)
            except Exception:
                _contract_profile_home = None
            try:
                contract_resolution = _contracts.resolve_contract_at_set(
                    completion_contract, profile_home=_contract_profile_home)
            except Exception as _resolve_exc:  # a resolution fault must never fail a filing
                _log.warning("contract resolution skipped for %s: %s", completion_contract,
                             _resolve_exc)
                contract_resolution = None
            if contract_resolution is not None and contract_resolution.changed:
                _log.warning("completion_contract %s -> %s: %s", completion_contract,
                             contract_resolution.contract, contract_resolution.reason)
                completion_contract = contract_resolution.contract
            elif contract_resolution is not None and contract_resolution.reason:
                # The declaration is KEPT and the measured state is recorded: a repository that
                # requires no checks (or whose checks APIs are gated) satisfies a PR contract by
                # MERGE STATE, so nothing is stripped for it any more (t_9082c751).
                _log.info("completion_contract %s kept as declared (state=%s, repo=%s): %s",
                          completion_contract, contract_resolution.state,
                          contract_resolution.repo, contract_resolution.reason)
    # INVARIANT D at its earliest door (operator ruling 2026-10-05, card t_5bbf4e80): the
    # card's ATTRIBUTION (`created_by`) must name a LIVE fleet identity. A card filed under a
    # DELETED profile is MISATTRIBUTED -- its provenance points at nothing and no lane can be
    # asked to own it. A DENYLIST -- a non-profile producer origin (hermes_pr_update, deploy-train,
    # sdlc, ...) is not a profile and passes.
    _gates.gate_created_by(created_by, board=board, where="kanban_create")
    if not title or not title.strip():
        raise ValueError("title is required")
    if initial_status not in VALID_INITIAL_STATUSES:
        raise ValueError(f"initial_status must be one of {sorted(VALID_INITIAL_STATUSES)}")
    if assignee:
        # INVARIANT B at its earliest door (design record platform-stl t_6747232b): an assignee
        # that resolves to no profile on disk and to no declared pull lane can never be spawned
        # and never reports a failure - the card strands silently. Measured before this seam:
        # 361 rows on handles that resolve nowhere, 15 cards stranded on two retired lanes.
        # The refusal names both honest paths (assign a real profile, or declare the lane).
        _gates.gate_assignee(assignee, board=board, where="kanban_create")
    # ONE RESOLUTION PER FILING (design ruling, card t_2187558e D1): the DESTINATION board -
    # the board the row actually lands on - is computed once, here, and every board-scoped read
    # below uses it. `board` first (the caller's declared target: the two model-facing doors fill
    # it from the source card and fail closed when it contradicts the pin); then the OPEN FILE's
    # own path, which is the only answer that cannot be stale in a DB-pinned process - a worker
    # spawned with HERMES_KANBAN_DB pointing at defcon has a defcon connection and may have any
    # ambient `current` pointer at all; the ambient pointer only decides for a store whose path
    # names no board. One filing, one slug, so the metadata chain and the birth-priority chain
    # cannot disagree.
    dest_board = board or board_for_connection(conn) or get_current_board()
    # A project-scoped board anchors every new task to its project's repo
    # (deterministic worktree + branch) without each surface repeating it.
    # An explicit ``scratch`` (or ``project_id=""``) is a request for no project:
    # it must not be upgraded to a worktree in the board's repo (#106342).
    project_source = "argument"
    if project_id is None and workspace_kind != "scratch":
        try:
            project_id = (_board_meta_for(dest_board).get("project_id") or "").strip() or None
        except Exception:
            pass
        if project_id:
            project_source = "board"
    if workspace_kind is None:
        workspace_kind = "scratch"
    if workspace_kind not in VALID_WORKSPACE_KINDS:
        raise ValueError(
            f"workspace_kind must be one of {sorted(VALID_WORKSPACE_KINDS)}, "
            f"got {workspace_kind!r}"
        )
    if branch_name is not None:
        branch_name = str(branch_name).strip() or None
    if branch_name and workspace_kind != "worktree":
        raise ValueError("branch_name is only valid for worktree workspaces")

    project_requested = project_id
    project_id, project_obj, project_repo, workspace_kind = _resolve_project_link(
        conn, project_id, project_source_task_id, workspace_kind, workspace_path
    )
    parents = tuple(p for p in parents if p)
    skills_list = _normalize_task_skills(skills)
    if skills_list:
        # Refuse a name no worker for this assignee can load, rather than letting the
        # card spend an attempt and auto-block on a failure only the worker log shows.
        unloadable = _unloadable_task_skills(skills_list, assignee)
        if unloadable:
            raise ValueError(_unloadable_skills_message(unloadable, assignee))

    # Idempotency check BEFORE the write txn (no lock held); a concurrent-create
    # race may insert twice, the next lookup stabilises on the newest.
    #
    # THE ARCHIVED-KEY SEAM (ruling t_2bc058fc, card t_0f97637f). `status != 'archived'`
    # FREED the key of every archived card, so the same incident was re-filed beside its
    # own tombstone (2,557 duplicate keyed rows on the live `platform` board, 1,357 key
    # groups holding an archived row, measured 2026-10-28). The policy decides what a
    # TOMBSTONE means and nothing else: a live card still answers first, and a policy
    # other than the default only runs when a caller asked for it BY NAME.
    if idempotency_key:
        archive_policy = _norm_archived_key_policy(archived_key_policy)
        reopen_actor, reopen_reason = _archived_key_authority(
            archive_policy, archived_key_actor, archived_key_reason)
        live_hit = _keyed_live_task(conn, idempotency_key)
        if live_hit:
            return live_hit
        if archive_policy != DEFAULT_ARCHIVED_KEY_POLICY:
            tombstone = _keyed_archived_task(conn, idempotency_key)
            if tombstone:
                if archive_policy == "return_tombstone":
                    # The identity is HELD: nothing is inserted, and the caller gets the
                    # card that already carries it rather than a twin of it.
                    return tombstone
                reopened, why_not, _info = reopen_task(
                    conn, tombstone, actor=reopen_actor, reason=reopen_reason)
                if not reopened:
                    # The card moved between this probe and the door. A live card under
                    # the key answers the filing just as well; anything else is a
                    # REFUSAL the caller must see -- minting a twin here is the defect.
                    again = _keyed_live_task(conn, idempotency_key)
                    if again:
                        return again
                    raise ValueError(
                        "archived_key_policy='reopen' could not reopen %s (key %r): %s"
                        % (tombstone, idempotency_key, why_not or "refused"))
                return tombstone

    # An unresolvable project link is DROPPED either way (never a dangling reference, never a
    # crash), but a silent drop is a silent failure: the caller believes the card is grouped
    # and it is not. Warn on stderr AND carry the finding into the ``created`` event, so the
    # filing CLI's caller learns it at the terminal and the card records it durably. Placed
    # after the idempotency early-return: a deduped call creates no card to warn about.
    project_unresolved = (
        _warn_unresolved_project_link(project_requested, source=project_source)
        if project_requested and project_id is None else {}
    )

    # THE BIRTH SEAM (REQUIRED 1, card t_2187558e). A board-scoped policy applied here cannot be
    # routed around: the tool verb, the CLI, a decomposer and another lane's agent all reach this
    # INSERT. The verdict is the DESTINATION board's (`dest_board`, resolved above from the
    # CONNECTION), never the ambient board's - the whole defect this restores was a policy asked
    # about the wrong board. `None` provenance is the inert case: an unwired board keeps the
    # caller's value and the event payload it has always written, byte for byte.
    # The REQUESTED value is kept before the policy touches it: the above-tranche
    # door below judges the CALLER'S number, so a wired board's clamp can never
    # silently absorb (and hide) an over-claim (card t_2e107499).
    requested_priority = int(priority)
    priority, policy_provenance = _apply_board_priority_policy(
        priority, assignee=assignee, board=dest_board, title=title, body=body,
    )

    # THE OPERATOR-ASK SEAM (card t_8ca4b5a0, operator ask 2026-09-27) AND ITS ASK-HOME DOOR
    # (card t_abefe660, operator ask 2026-10-04). Both apply HERE, at the one moment every
    # filing surface reaches, so a reference stamped at birth cannot be routed around by a
    # caller that forgot. Resolution reads only what the caller and THIS board can answer (an
    # explicit ``serves``, the worker-session env, the body's own stamp, then the parents) and
    # never fails a FILING: a reference that names no card is warned about and carried into the
    # ``created`` event as ``operator_ask_unresolved``. A card that IS a new ask must be filed
    # on its register's own board or its register's roll-up never sees it - refused LOUDLY
    # before any write; the hatch ``allow_off_board_ask`` admits a deliberate cross-board filing
    # and returns a record the created txn writes as a task_events row.
    _ask_board = board or board_for_connection(conn)
    ask_ref = _resolve_operator_ask(
        conn, board=_ask_board, parents=parents, body=body, serves=serves,
    )
    ask_off_board = guard_new_ask_home(
        ask_ref, board=_ask_board, title=title,
        allow_off_board=allow_off_board_ask, caller=created_by,
    )

    # THE ABOVE-TRANCHE DOOR (operator ruling 2026-09-28, card t_6ce41549; restored by card
    # t_2e107499). Applies at the same seam, on the REQUESTED value and for EVERY board: the
    # ceiling is not a board's property, it is the scale's. `dest_board` - the one slug resolved
    # from the CONNECTION above (ruling t_2187558e D1) - is what the deviation row is filed
    # against and what the marker's designation read is answered from.
    priority, above_tranche_record = apply_above_tranche_guard(
        conn, requested_priority, priority, board=dest_board, lane=created_by, title=title,
        body=body, ask_ref=ask_ref, tenant=tenant,
    )
    policy_provenance = _merge_policy_record(policy_provenance, above_tranche_record)

    now = int(time.time())

    # Only persistent kinds inherit the board ``default_workdir``: a scratch
    # task inheriting it would point cleanup at the user's source tree.
    if workspace_path is None and project_repo is None and workspace_kind in {"dir", "worktree"}:
        board_default = _board_meta_for(dest_board).get("default_workdir")
        if board_default:
            workspace_path = str(board_default)

    # Retry once on the extremely unlikely id collision.
    for attempt in range(2):
        task_id = _new_task_id()
        try:
            # allow_nested: graph builders compose create_task under one outer
            # commit so the dispatcher never sees a half-built graph.
            with write_txn(conn, allow_nested=True):
                task_status, tenant = initial_task_state(conn, parents, initial_status, triage, tenant)
                # THE TOP-LEVEL-ONLY GATE, create door (ruling t_efa908e8, card
                # t_0d8f71ae). A card whose whole DoD needs a genuine top-level
                # context declares it with a line-leading `GATE-TOP-LEVEL-ONLY`;
                # the dispatcher must never spawn a worker for it (a dispatched
                # worker is structurally fenced from the work). FILING stays legal
                # -- proposing work is legal, and refusing it would delete the
                # record -- so the card is PARKED `scheduled` (due_at stays NULL,
                # a fresh insert), a status `recompute_ready` never promotes (it
                # promotes only `todo`/`blocked`), hence stably non-dispatchable.
                # The RUN door re-checks the card's LIVE text (an amendment can add
                # the marker after filing). See hermes_cli/kanban_top_level_gate.py.
                from hermes_cli.kanban_top_level_gate import declares as _top_level_declares
                top_level_parked = task_status == "ready" and _top_level_declares(title, body)
                if top_level_parked:
                    task_status = "scheduled"
                # Project worktree: fresh dir under the repo + deterministic
                # branch, instead of the random ``wt/<id>`` worker fallback.
                if project_obj is not None and workspace_kind == "worktree":
                    if project_repo and not workspace_path:
                        workspace_path = os.path.join(project_repo, ".worktrees", task_id)
                    if not branch_name:
                        branch_name = _project_branch_name(project_obj, task_id, title)

                # The stamp rides the INSERT: the card carries its ask from birth, in the
                # same transaction that creates the row, so no reader ever sees an
                # in-service card without it and no second writer has to add it later.
                # With NO ask pair the body is STILL normalised: a bytes body passed
                # through verbatim landed as a BLOB, and the decompose seam dropped a
                # non-str body entirely - so both no-ask fallbacks share
                # ``normalise_body`` (the public name for the register module's lossy
                # ``_text``) instead of each deciding for itself.
                ask_pair = ask_ref.for_card(task_id) if ask_ref is not None else None
                card_body = apply_stamp(body, *ask_pair) if ask_pair else normalise_body(body)
                conn.execute(
                    """
                    INSERT INTO tasks (
                        id, title, body, assignee, status, priority,
                        created_by, created_at, workspace_kind, workspace_path,
                        branch_name, project_id, tenant, idempotency_key,
                        max_runtime_seconds,
                        skills, max_retries, model_override, provider_override,
                        reasoning_effort,
                        goal_mode, goal_max_turns, session_id, completion_contract
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id, title.strip(), card_body, assignee, task_status, priority,
                        created_by, now, workspace_kind, workspace_path,
                        branch_name, project_id, tenant, idempotency_key,
                        _opt_int(max_runtime_seconds),
                        json.dumps(skills_list) if skills_list is not None else None,
                        _opt_int(max_retries), model_override, provider_override, reasoning_effort,
                        1 if goal_mode else 0, _opt_int(goal_max_turns), session_id, completion_contract,
                    ),
                )
                for pid in parents:
                    _link(conn, pid, task_id, cause="create")
                _append_event(
                    conn,
                    task_id,
                    "created",
                    {
                        "assignee": assignee,
                        "status": task_status,
                        "parents": list(parents),
                        "creator_task_id": creator_task_id,
                        "tenant": tenant,
                        "workspace_kind": workspace_kind,
                        "workspace_path": workspace_path,
                        "branch_name": branch_name,
                        "project_id": project_id,
                        **project_unresolved,
                        "skills": list(skills_list) if skills_list else None,
                        "goal_mode": bool(goal_mode) or None,
                        "model_override": model_override,
                        "provider_override": provider_override,
                        **(operator_ask_event(ask_ref, ask_pair)),
                        # The policy's own record, verbatim, and only on a card it moved: an
                        # unchanged card and every board without a policy keep the event
                        # payload they have always written.
                        **({"priority_policy": policy_provenance} if policy_provenance else {}),
                    },
                )
                # The measured reason travels WITH the card (t_b58332a4 -> t_4bec5cfe): the author
                # sees WHY a declared repo contract became local-only, rather than discovering a
                # silent downgrade. Only written when the contract actually moved; an unreadable
                # repository keeps its declaration and writes no row.
                if contract_resolution is not None and contract_resolution.changed:
                    _append_event(
                        conn, task_id, "contract_changed",
                        {
                            "old": contract_resolution.declared,
                            "new": contract_resolution.contract,
                            "state": contract_resolution.state,
                            "repo": contract_resolution.repo,
                            "branch": contract_resolution.branch,
                            "reason": contract_resolution.reason,
                            "actor": created_by or "user",
                        },
                    )
                # The hatch's own record, so an off-board NEW ask that was let through on
                # purpose is visible on the card rather than silent (a row naming the caller).
                if ask_off_board is not None:
                    _append_event(conn, task_id, OFF_BOARD_ASK_EVENT, ask_off_board)
                if task_status == "blocked":
                    _append_event(
                        conn,
                        task_id,
                        "blocked",
                        {"reason": "initial_status", "status": "blocked", "actor": created_by or "user"},
                    )
                if top_level_parked:
                    # Named on the card so the author sees why their card is not
                    # dispatchable; the RUN door writes the one comment naming the
                    # sanctioned top-level routes. This event is the durable record
                    # of the create-door park.
                    _append_event(
                        conn, task_id, "top_level_gate_parked",
                        {
                            "reason": "declared",
                            "marker": "GATE-TOP-LEVEL-ONLY",
                            "status": "scheduled",
                            "actor": created_by or "user",
                        },
                    )
                if task_status == "todo":
                    # Parked behind an open parent: record why, exactly as
                    # link_tasks does, so the board never shows an unexplained todo.
                    gating = [p for p in parents if _task_status(conn, p) not in ("done", "archived")]
                    if gating:
                        _append_event(
                            conn,
                            task_id,
                            "dependency_wait",
                            {"reason": "parent_not_done", "parent": gating[0]},
                        )
                # ACK-edge: the originating channel hears a child BLOCK, not just the fan-in.
                inherit_creator_origin(conn, task_id, creator_task_id, created_at=now)
                _inherit_notify_subs(conn, task_id, parents, created_at=now)
            return task_id
        except sqlite3.IntegrityError:
            if attempt == 1:
                raise
    raise RuntimeError("unreachable")


def create_task_reported(
    conn: sqlite3.Connection, *, profiles: Optional[Iterable[str]] = None, **kwargs,
) -> tuple[str, LinkOutcome]:
    """Create a task through :func:`create_task`; return it with its dependency receipt.

    The same single write path, so ``create_task`` keeps its id-shaped contract for callers
    that only need one, and this door answers the question a FILER has — will this card run
    by itself? ``profiles`` is the caller's live-profile list for the receipt's dead-assignee
    probe (``None`` = not injected: skipped, never guessed); the board is the caller's own
    ``board=``, never the ambient one.
    """
    task_id = create_task(conn, **kwargs)
    return task_id, created_outcome(conn, task_id, profiles=profiles, board=kwargs.get("board"))


def _board_meta_for(board: Optional[str]) -> dict:
    return read_board_metadata(board if board else get_current_board())


def _apply_board_priority_policy(
    requested: int, *, assignee: Optional[str], board: Optional[str],
    title: str, body: Optional[str],
) -> tuple[int, Optional[dict]]:
    """``(priority, provenance)`` for a card being created, policy applied at birth.

    ``provenance`` is ``None`` - and ``requested`` comes back unchanged - when the board
    carries no ``priority_policy``, which is what keeps this seam inert for every board
    that never opted in. When a policy IS configured it decides the stored value, and the
    policy's own record rides back for the ``created`` event, so a banding decision can be
    read back off the card.

    ``board`` is the DESTINATION board, resolved by ``create_task`` from the CONNECTION
    (design ruling, card t_2187558e D1) - never the ambient board. Do not reach for
    ``get_current_board()`` here: in a DB-pinned process that is a different board from the
    one the row lands on, which is exactly how a below-floor filing came to be refused by
    the destination board's own trigger.

    Nothing here may fail a filing. The policy is consulted through
    ``kanban_priority_policy.priority_for_create``, which answers an unusable policy with
    the caller's value plus an ``unavailable`` record instead of an exception: a broken
    policy must be VISIBLE, not fatal. The numbers (lane ordering, the band table, the
    clauses) belong to the policy module; this module owns the moment and the seam.
    """
    policy = _policy_module()

    try:
        spec = _board_meta_for(board).get(policy.POLICY_KEY)
    except Exception:
        # A board whose metadata cannot be read has no policy to honour; the
        # ``project_id``/``default_workdir`` reads degrade the same way.
        return requested, None
    if spec is None:
        return requested, None
    verdict = policy.priority_for_create(
        requested,
        assignee=assignee or "",
        board=board or get_current_board(),
        title=title,
        body=body or "",
        spec=spec,
    )
    if verdict is None:
        return requested, None
    # R1: the domain bounds every wired board, whatever the policy answered - including a
    # policy that could not be used, whose verdict is the filer's own value. The clamp is
    # merged into that same record, so the event carries it and no second provenance key is
    # invented. (``create_task`` and the decomposer both arrive here, so a fan-out is bounded
    # by the same one call a filing makes.)
    #
    # The board's FLOOR rides the clamp so a value the policy lifted to a floor ON the tranche
    # boundary (``TRANCHE_FLOOR``) is admitted instead of clamped back into the ordinary domain.
    # It is read here, once; a floor that cannot be read degrades to ``None`` because this seam
    # is fail-open by contract (no create may fail on a policy's account) and a missing floor
    # only costs the boundary lift its exemption - the card still lands, in-domain.
    floor = None
    if policy.board_is_wired(spec):
        try:
            floor = policy.board_floor(spec, board or get_current_board())
        except Exception:
            floor = None
    return policy.clamp_to_domain(verdict.applied, verdict.record, floor)


# --------------------------------------------------------------------------------------------
# THE GATE INVARIANT - a card must never rank below a card it gates.
#
# Operator directive, 2026-09-29 (card t_ef547958). Priority picks the SPAWN ORDER and nothing
# else; a gated card waits in `todo` until its parent is `done`. So a gate filed low does not
# merely run late - it freezes every card behind it, whatever those cards were designated. Three
# gates at p=0 were holding cards at p=999999 in the reserved tranche, and the operator spent an
# evening hand-lifting them. The fleet enforced the band CENTRE at the create seam and nothing at
# all about RELATIONS, because a relation is created after birth and a policy never sees it.
#
# THE RULE. For every edge parent -> child over `task_links`, `parent.priority >= child.priority`
# holds while the parent is not terminal. The requirement is TRANSITIVE and it is a MAXIMUM: a
# card's gate floor is the highest priority among the OPEN work it still gates, walking down only
# through open nodes - a `done` intermediary waits for nothing, so nothing behind it is waiting on
# this card either. ``gate_need()`` is that one definition; everything else here calls it.
#
# RESTORED 2026-10-13 (card t_dcd61b4d). This block and the `gates report|reconcile` verb that
# drives it were carried by the retired untracked-override set and were LOST with it: the write
# doors kept refusing (their module is live) while the MEASUREMENT and REPAIR surface raised
# AttributeError, so a violation of the rank invariant was invisible - the exact "a rule that
# cannot be measured silently fails" class the directive exists to end.
#
# THE SEAMS (relations change after birth, so one seam cannot hold this):
#   * ``_link`` - the ONE edge primitive (``create_task``, ``link_tasks`` and the triage
#     decomposer all insert edges through it): a new edge can leave the parent below the card it
#     now gates, so the child's whole ancestor chain is re-decided, nearest first.
#   * ``edit_task`` - a re-rank changes a VALUE in both directions.
# Nothing else needs a seam: completing, archiving or unlinking only RELAXES the invariant.
#
# THE LIFT IS ONE-WAY. It raises. It never lowers, never demotes the gated card and never refuses
# a filing: demoting or rejecting the child would hide the gate instead of honouring it.
# ``min(need, MAX_PRIORITY)`` is the only bound - a child left above the scale's top by an
# unguarded raw write is capped, and the cap is RECORDED (``capped_from``) rather than written
# silently.
#
# THE RESERVED TRANCHE. A gate whose child holds 999999 must itself hold 999999, which is inside
# the band that is "designated, never requested". The lift takes the designation route the door
# already provides - ledger row FIRST (the storage guard aborts a tranche value with no live
# designation, so the order is the door's, not a choice made here), ``authority="gate-lift"`` and
# a reason naming the CHILD - and then writes the exact value, because 990000 is the
# designation's own value and the invariant needs the child's.
#
# THE PASS. Seams hold every write that goes through them; nothing holds a value written around
# them (the fleet's raw-SQL re-rank lever, a restored backup). ``reconcile_gate_priorities()`` is
# the deterministic pass that normalises those and STATES its counts - it is what the board-health
# DAG runs, so the answer to "is the invariant held" is a run record, never a card comment.
# --------------------------------------------------------------------------------------------

#: The authority every lift records, on the event and on the designation it may need: one string
#: for "the RELATION moved this card - not a lane, not the operator, not a band".
GATE_AUTHORITY = "gate-lift"

#: A parent in one of these states holds nothing: the invariant is over the work that is still
#: WAITING, so a terminal parent is never lifted and a walk never descends through one.
GATE_TERMINAL_STATUSES = ("done", "archived")


def _gate_children(conn: sqlite3.Connection, task_id: str) -> list:
    """The card's children - ``(id, priority, status)`` - in id order, so the walk is stable."""
    return conn.execute(
        "SELECT c.id AS id, c.priority AS priority, c.status AS status "
        "  FROM task_links l JOIN tasks c ON c.id = l.child_id "
        " WHERE l.parent_id = ? ORDER BY c.id",
        (task_id,),
    ).fetchall()


def gate_need(conn: sqlite3.Connection, task_id: str) -> Optional[tuple]:
    """``(value, gate_id)`` - the priority ``task_id`` MUST hold, and the open card that sets it.

    ``None`` is the negative case and the reason this is safe to call everywhere: a card with no
    children - or whose children are all delivered - gates nothing that is still waiting, so it
    has no floor and nothing can touch it.

    The walk descends through OPEN nodes only: a ``done`` intermediary is delivered work, and
    nothing behind it is waiting on this card either, so the chain stops there. Cycle-safe
    (``seen`` starts with the card itself - a graph that somehow closed a loop still terminates),
    and the answer is the MAXIMUM over open descendants, since a card with several children must
    outrank all of them. Ties break on the lowest card id so the record is REPRODUCIBLE rather
    than merely correct.
    """
    best_value: Optional[int] = None
    best_id: Optional[str] = None
    seen = {task_id}
    stack = [task_id]
    while stack:
        node = stack.pop()
        for row in _gate_children(conn, node):
            child_id = row["id"]
            if child_id in seen:
                continue
            seen.add(child_id)
            if row["status"] in GATE_TERMINAL_STATUSES:
                continue
            value = int(row["priority"] or 0)
            if best_value is None or value > best_value or (
                    value == best_value and str(child_id) < str(best_id)):
                best_value, best_id = value, child_id
            stack.append(child_id)
    if best_value is None:
        return None
    return best_value, best_id


def _gate_ancestors_nearest_first(conn: sqlite3.Connection, task_id: str) -> list:
    """``task_id``, then its parents, then theirs - the order the lift must run in.

    Nearest first because a lift only RAISES: repairing an edge changes the requirement of every
    card above it, so one pass in this order leaves each card holding the number the settled graph
    needs. The card itself is included - it may be a parent of something too. Breadth-first with a
    seen-set, so a diamond or a cycle cannot repeat or hang.
    """
    order: list = []
    seen = {task_id}
    frontier = [task_id]
    while frontier:
        nxt: list = []
        for node in frontier:
            order.append(node)
            for row in conn.execute(
                    "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id",
                    (node,)):
                parent = row["parent_id"]
                if parent in seen:
                    continue
                seen.add(parent)
                nxt.append(parent)
        frontier = nxt
    return order


def _gate_designation_reason(gate_id: str, gate_priority: int, cause: str) -> str:
    """The ladder's audit answer: WHICH card this one gates, and which seat asked for the lift."""
    return (f"gate lift: this card gates {gate_id} at {int(gate_priority)}, so it cannot rank "
            f"below it (cause: {cause})")


def _gate_lift_one(conn: sqlite3.Connection, task_id: str, *, need: tuple, cause: str,
                   board: Optional[str] = None) -> Optional[dict]:
    """Raise ONE card to its gate floor. Returns the lift record, or ``None`` when it is already
    there / terminal / gone.

    Never lowers (a card above its floor is left alone), never refuses, and never writes a value
    above the scale's top without recording it.
    """
    policy = _designation_policy()
    need_value, need_id = int(need[0]), need[1]
    row = conn.execute("SELECT priority, status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None or row["status"] in GATE_TERMINAL_STATUSES:
        return None
    before = int(row["priority"] or 0)
    target = min(need_value, int(policy.MAX_PRIORITY))
    if target <= before:
        return None
    slug = board or board_for_connection(conn) or ""
    tranche = target >= int(policy.TRANCHE_FLOOR)
    if tranche:
        # Ledger row FIRST: the armed storage guard aborts a tranche value with no live
        # designation, so the designation door opens before this writes the value.
        designate_priority(conn, task_id, reason=_gate_designation_reason(need_id, need_value, cause),
                           authority=GATE_AUTHORITY, board=slug or None, nested=True)
        if target != int(policy.DESIGNATED_PRIORITY):
            with write_txn(conn, allow_nested=True):
                conn.execute("UPDATE tasks SET priority = ? WHERE id = ?", (target, task_id))
    else:
        with write_txn(conn, allow_nested=True):
            conn.execute("UPDATE tasks SET priority = ? WHERE id = ?", (target, task_id))
    payload = {
        "priority": target,
        "before": before,
        "cause": cause,
        "gate": need_id,
        "gate_priority": need_value,
        "tranche": tranche,
    }
    if target != need_value:
        payload["capped_from"] = need_value
    _append_event(conn, task_id, "reprioritized", payload)
    return {
        "task_id": task_id, "board": slug, "before": before, "now": target, "cause": cause,
        "gate": need_id, "gate_priority": need_value, "tranche": tranche,
        "capped_from": need_value if target != need_value else None,
        "designation": _gate_designation_reason(need_id, need_value, cause) if tranche else None,
    }


def lift_gate_chain(conn: sqlite3.Connection, task_id: str, *, cause: str = "edit",
                    board: Optional[str] = None) -> list:
    """THE SEAM: hold the invariant for everything ``task_id`` changed, nearest first.

    Call it inside the txn that made the change - after an edge is inserted, after a re-rank is
    written - so the stored state is never observably wrong: a reader either sees the edge and the
    floor together, or neither. The card itself is re-decided first (an edit can leave it under
    its own children), then each ancestor in turn, each against the graph as it stands by then.
    Returns the lifts it made, oldest first; an empty list is the normal, clean case.
    """
    slug = board or board_for_connection(conn) or ""
    lifts: list = []
    for node in _gate_ancestors_nearest_first(conn, task_id):
        if _task_status(conn, node) in GATE_TERMINAL_STATUSES:
            continue
        need = gate_need(conn, node)
        if need is None:
            continue
        record = _gate_lift_one(conn, node, need=need, cause=cause, board=slug)
        if record is not None:
            lifts.append(record)
    return lifts


def gate_violations(conn: sqlite3.Connection) -> list:
    """Every edge whose parent ranks below the card it gates - the invariant, MEASURED.

    The measurement is the graph, never a remembered number: this is what the pass reports before
    and after, so "0" is a query result rather than a claim.
    """
    rows = conn.execute(
        "SELECT l.parent_id AS parent_id, l.child_id AS child_id, "
        "       p.priority AS parent_priority, c.priority AS child_priority, "
        "       p.status AS parent_status "
        "  FROM task_links l "
        "  JOIN tasks p ON p.id = l.parent_id "
        "  JOIN tasks c ON c.id = l.child_id "
        " WHERE p.status NOT IN ('done', 'archived') AND p.priority < c.priority "
        " ORDER BY l.parent_id, l.child_id",
    ).fetchall()
    return [dict(row) for row in rows]


def reconcile_gate_priorities(conn: sqlite3.Connection, *, board: Optional[str] = None,
                              cause: str = "reconcile") -> dict:
    """THE PASS: raise every gate on this board to the cards it still holds, and state the counts.

    Deterministic and idempotent - a second run over an unchanged board lifts nothing and returns
    the same zero, which is the property that makes it safe to run on a schedule. It works from
    the VIOLATIONS rather than from every card, so a clean board costs one query. A lift that
    raises is recorded in ``failed`` and the pass continues: one un-liftable card must not hide
    the state of the other fifty. ``violations_after`` is re-measured from the graph - if it is
    not 0 the record says so instead of the pass claiming success.
    """
    before = gate_violations(conn)
    lifts: list = []
    failed: list = []
    for row in before:
        try:
            lifts.extend(lift_gate_chain(conn, row["parent_id"], cause=cause, board=board))
        except Exception as exc:  # one card must not abort the sweep
            failed.append({"task_id": row["parent_id"], "edge": [row["parent_id"], row["child_id"]],
                           "error": f"{type(exc).__name__}: {exc}"})
    after = gate_violations(conn)
    return {
        "board": board or board_for_connection(conn) or "",
        "violations_before": len(before),
        "violations": before,
        "lifts": lifts,
        "lifts_total": len(lifts),
        "tranche_lifts": len([l for l in lifts if l.get("tranche")]),
        "failed": failed,
        "violations_after": len(after),
        "remaining": after,
    }

def _project_branch_name(project_obj: Any, task_id: str, title: Optional[str]) -> Optional[str]:
    from hermes_cli import projects_db as _pdb

    try:
        return _pdb.branch_name_for(project_obj, task_id, title=title or "")
    except Exception:
        return None


def _link(conn: sqlite3.Connection, parent_id: str, child_id: str, *,
          cause: str = "link") -> None:
    """Insert one edge, then hold the gate invariant that edge creates.

    This is the ONE edge primitive: ``create_task`` (parents passed at filing), ``link_tasks``
    and the triage decomposer all arrive here, which is why the seam sits here rather than three
    times above it. The edge is written first and the lift runs in the same transaction, so the
    stored graph is never observably "an edge whose parent ranks below the card it gates";
    either the caller's write commits with the lift or neither does.

    ``cause`` is only the record's answer to "which seat asked for this" (``create`` / ``link`` /
    ``decompose``); the rule is the same in all three and it never depends on the caller.
    """
    conn.execute(
        "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
        (parent_id, child_id),
    )
    lift_gate_chain(conn, child_id, cause=cause)


def _missing_task_ids(conn: sqlite3.Connection, ids: Iterable[str]) -> list[str]:
    """Subset of ``ids`` (order kept) with no ``tasks`` row."""
    ids = list(ids)
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT id FROM tasks WHERE id IN ({placeholders})", ids).fetchall()
    present = {r["id"] for r in rows}
    return [p for p in ids if p not in present]


def _inherit_notify_subs(
    conn: sqlite3.Connection, child_id: str, parents: Iterable[str], *,
    created_at: Optional[int] = None,
) -> None:
    """Copy parents' notify subscriptions to a child, cursor caught up to the
    child's current event so a late ``link_tasks`` never replays history.

    Single owner of inheritance (create_task, link_tasks, decompose). It must
    copy EVERY routing/delivery column: dropping ``chat_type`` made DM-originated
    completions wake a fresh group session instead of the originating DM.

    Omitting columns here silently degrades routing: a DM-originated child completion falls back to
    chat_type='group' and wakes a fresh group-scoped session instead of the originating DM (issue #73030).
    """
    parent_ids = tuple(dict.fromkeys(p for p in parents if p))
    if not parent_ids:
        return
    row = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS cursor FROM task_events WHERE task_id = ?", (child_id,),
    ).fetchone()
    cursor = int(row["cursor"] if row is not None else 0)
    placeholders = ",".join("?" * len(parent_ids))
    conn.execute(
        f"""
        INSERT OR IGNORE INTO kanban_notify_subs
            (task_id, platform, chat_id, thread_id, user_id, user_id_alt,
             chat_type, notifier_profile, delivery_mode, delivery_metadata,
             created_at, last_event_id)
        SELECT ?, platform, chat_id, thread_id, user_id, user_id_alt,
               COALESCE(chat_type, 'dm'), notifier_profile,
               COALESCE(delivery_mode, 'notify'), delivery_metadata, ?, ?
          FROM kanban_notify_subs
         WHERE task_id IN ({placeholders})
        """,
        (child_id, int(created_at if created_at is not None else time.time()), cursor, *parent_ids),
    )


def get_task(conn: sqlite3.Connection, task_id: str) -> Optional[Task]:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return Task.from_row(row) if row else None


# Canonical sort-order mappings for ``hermes kanban list --sort``.
# Each value is a raw SQL fragment appended after ``ORDER BY``.
# --- Priority + age-aware dispatch ordering --------------------------------
#
# ``priority`` is claimed-first-ordered by DESC: 3 = P0 (top), 2 = P1, 1 = P2,
# 0 = P3 (bottom). Declared priority alone starves low-priority work: on a
# loaded board a P3 never overtakes the steady arrival of P0-P1 cards, so it
# can sit ready indefinitely. Every order site therefore sorts on the
# EFFECTIVE priority, which credits waiting time:
#
#     effective = priority + MIN(CAP, MAX(0, floor(wait_seconds / STEP)))
#
# With STEP = 1h a bottom-rung card gains a rung an hour and overtakes a fresh
# top-rung card after 3h; once CAP = 6 is reached priority differences are
# erased and the tie falls through to ``created_at ASC`` (plain FIFO). Wait
# time is measured from ``created_at``, so the order is stable while a card
# waits and identical across processes and boards.
PRIORITY_AGE_BONUS_STEP_SECONDS = 3600
PRIORITY_AGE_BONUS_CAP = 6


def priority_age_bonus(created_at: Optional[float], *, now: Optional[float] = None) -> int:
    """Waiting-time credit for one card: ``floor(wait / STEP)`` clamped to ``[0, CAP]``."""
    if created_at is None:
        return 0
    at = time.time() if now is None else float(now)
    waited = at - float(created_at)
    if waited <= 0:
        return 0
    return int(min(PRIORITY_AGE_BONUS_CAP, waited // PRIORITY_AGE_BONUS_STEP_SECONDS))


def effective_priority(
    priority: Optional[int], created_at: Optional[float], *, now: Optional[float] = None,
) -> int:
    """``priority`` plus :func:`priority_age_bonus` — Python mirror of
    :func:`effective_priority_order_sql`, for tests and single-row reasoning."""
    return int(priority or 0) + priority_age_bonus(created_at, now=now)


def effective_priority_order_sql(now: Optional[float] = None) -> tuple[str, list[Any]]:
    """``(order_by_body, params)`` shared by EVERY ready/review order site.

    The dispatcher lane order (``kanban_db_dispatch._lane_rows``) and
    :func:`list_tasks` both call this one helper so the two can never drift.
    ``now`` defaults to ``time.time()``; pass it explicitly for deterministic
    tests.
    """
    at = time.time() if now is None else float(now)
    expr = (
        f"(priority + MIN({int(PRIORITY_AGE_BONUS_CAP)}, "
        f"MAX(0, CAST((? - created_at) / {int(PRIORITY_AGE_BONUS_STEP_SECONDS)} AS INTEGER))))"
    )
    return f"{expr} DESC, created_at ASC", [at]


VALID_SORT_ORDERS: dict[str, str] = {
    "created": "created_at ASC, id ASC",
    "created-desc": "created_at DESC, id DESC",
    "priority": "priority DESC, created_at ASC",
    "priority-desc": "priority ASC, created_at ASC",
    "status": "status ASC, created_at ASC",
    "assignee": "assignee ASC, created_at ASC",
    "title": "title ASC, id ASC",
    "updated": "started_at DESC NULLS LAST, created_at DESC",
    "completed-desc": "completed_at DESC NULLS LAST, id DESC",
}


def list_tasks(
    conn: sqlite3.Connection, *, assignee: Optional[str] = None, status: Optional[str] = None,
    tenant: Optional[str] = None, session_id: Optional[str] = None, include_archived: bool = False,
    limit: Optional[int] = None, order_by: Optional[str] = None,
    workflow_template_id: Optional[str] = None, current_step_key: Optional[str] = None,
    now: Optional[float] = None,
) -> list[Task]:
    if status is not None and status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}")
    query = "SELECT * FROM tasks WHERE 1=1"
    params: list[Any] = []
    for col, val in (
        ("assignee", _canonical_assignee(assignee)), ("status", status), ("tenant", tenant),
        ("session_id", session_id), ("workflow_template_id", workflow_template_id),
        ("current_step_key", current_step_key),
    ):
        if val is not None:
            query += f" AND {col} = ?"
            params.append(val)
    if not include_archived and status != "archived":
        query += " AND status != 'archived'"
    if order_by is not None:
        order_by = order_by.strip().lower()
        if order_by not in VALID_SORT_ORDERS:
            raise ValueError(f"order_by must be one of {sorted(VALID_SORT_ORDERS.keys())}")
        query += f" ORDER BY {VALID_SORT_ORDERS[order_by]}"
    else:
        # Age-aware default order, shared with the dispatcher lane order so the
        # two cannot drift (see ``effective_priority_order_sql``).
        _order_body, _order_params = effective_priority_order_sql(now)
        query += f" ORDER BY {_order_body}"
        params.extend(_order_params)
    if limit:
        query += f" LIMIT {int(limit)}"
    rows = conn.execute(query, params).fetchall()
    return [Task.from_row(r) for r in rows]


def assign_task(conn: sqlite3.Connection, task_id: str, profile: Optional[str]) -> bool:
    """Assign/reassign; raises RuntimeError while the task is running under a claim,
    and ValueError when a worker for *profile* could not load the task's skills —
    except on a card in a terminal status (``done``/``archived``), which no worker
    will run again and can therefore be assigned for attribution.
    """
    profile = _canonical_assignee(profile)
    # INVARIANT B at the OTHER door (ruling ESC-01650 / platform/t_52183ba9 item 3): create refuses
    # an assignee that resolves to no profile on disk and to no declared pull lane (`:1947`,
    # `where="kanban_create"`), but assign did not -- so a card could be handed to a handle that
    # can never be spawned, strand silently, and never be reported as failed. Same rule, same call;
    # this one is refuse-class and needs no soak, because create already refuses it and the
    # asymmetry is a defect rather than a policy. `None`/empty (an unassign) stands down: unassigning
    # a card is legal, and a terminal card can still be re-attributed, since a roster handle that
    # exists passes and only a handle that resolves NOWHERE is refused.
    if profile and str(profile).strip():
        from hermes_cli import kanban_gate_invariants as _gates
        _gates.gate_assignee(profile, board=board_for_connection(conn), where="kanban_assign")
    with write_txn(conn):
        row = conn.execute(
            "SELECT status, claim_lock, assignee, skills FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row:
            return False
        if row["claim_lock"] is not None and row["status"] == "running":
            raise RuntimeError(
                f"cannot reassign {task_id}: currently running (claimed). "
                "Wait for completion or reclaim the stale lock first."
            )
        # A card created with no assignee carried its skills past the create-time
        # check (nothing to judge them against); judge them against the profile
        # being attached here, before it can reach a worker — and before the
        # failure streak and the assignee are written. A card in a terminal status
        # is not a dispatch: no worker will load those names again, so the pair
        # cannot fail on ``Unknown skill(s)``. Judging it there would only make a
        # finished (or archived) card un-attributable.
        if row["status"] not in TERMINAL_STATUSES:
            _refuse_unloadable_skills(row["skills"], profile)
        if row["assignee"] != profile:
            # The failure streak is per task/profile; a new profile starts fresh.
            conn.execute(
                "UPDATE tasks SET assignee = ?, consecutive_failures = 0, "
                "last_failure_error = NULL WHERE id = ?", (profile, task_id),
            )
        else:
            conn.execute("UPDATE tasks SET assignee = ? WHERE id = ?", (profile, task_id))
        # ``from`` lets the respawn guard tell a real handoff (dev→closer) from
        # a no-op re-assign or an unassign, which must not lift ``active_pr``.
        _append_event(
            conn, task_id, "assigned", {"assignee": profile, "from": row["assignee"]},
        )
    # Observer fires AFTER commit so subscribers see durable state.
    notify_task_updated(conn, task_id, ("assignee",))
    return True


def set_model_override(
    conn: sqlite3.Connection, task_id: str, model: Optional[str], provider: Optional[str] = None,
) -> bool:
    """Set (empty ``model`` clears BOTH) the per-task model/provider override.
    Allowed while ``running``: it applies on the NEXT dispatch, which is the
    rate-limit-recovery flow (set, then reclaim/retry)."""
    model, provider = _validate_model_override(model, provider)
    return _set_task_override(
        conn, task_id,
        "UPDATE tasks SET model_override = ?, provider_override = ? WHERE id = ?", (model, provider),
        "model_override_set", {"model": model, "provider": provider},
        ("model_override", "provider_override"), archived_msg="cannot set model override",
    )


def _set_task_override(
    conn: sqlite3.Connection, task_id: str, sql: str, params: tuple, event_kind: str, payload: dict,
    changed_fields: tuple[str, ...], *, archived_msg: str,
) -> bool:
    """Per-task override write: refuse archived tasks, record ``event_kind``,
    then fire the task-updated observer AFTER commit (RFC #58548)."""
    with write_txn(conn):
        status = _task_status(conn, task_id)
        if status is None:
            return False
        if status == "archived":
            raise RuntimeError(f"{archived_msg} on archived task {task_id}")
        conn.execute(sql, (*params, task_id))
        _append_event(conn, task_id, event_kind, payload)
    notify_task_updated(conn, task_id, changed_fields)
    return True


def set_reasoning_effort(conn: sqlite3.Connection, task_id: str, effort: Optional[str]) -> bool:
    """Set (empty clears; ``"none"`` pins thinking OFF) the per-task reasoning
    effort. Independent of the model override so clearing one never resets the
    other; applies on the NEXT dispatch, so settable while running."""
    effort = normalize_reasoning_effort(effort)
    return _set_task_override(
        conn, task_id, "UPDATE tasks SET reasoning_effort = ? WHERE id = ?", (effort,),
        "reasoning_effort_set", {"reasoning_effort": effort},
        ("reasoning_effort",), archived_msg="cannot set reasoning effort",
    )


# --- Links ---

def link_tasks(
    conn: sqlite3.Connection,
    parent_id: str,
    child_id: str,
    *,
    expected_child_run_id: Optional[int] = None,
) -> bool:
    """Link ``parent_id -> child_id``. Returns True when the link gated a
    ``ready`` child back to ``todo`` (the new parent is not yet terminal), so
    callers can surface the demotion instead of a silent status flip.

    A running child cannot normally be gated retroactively, so reject the edge
    rather than record a dependency that did not constrain the active run. The
    owning worker may link its own active run for a subsequent dependency-block
    handoff by supplying its trusted ``expected_child_run_id``.

    The write lives in :mod:`hermes_cli.kanban_db_links` (``link_tasks_reported``, bound at
    the tail import), the one path the receipt doors share; this wrapper keeps the boolean
    contract.
    """
    return link_tasks_reported(
        conn, parent_id, child_id, expected_child_run_id=expected_child_run_id).gated


def _linked_ids(conn: sqlite3.Connection, want: str, where: str, task_id: str) -> list[str]:
    rows = conn.execute(
        f"SELECT {want} FROM task_links WHERE {where} = ? ORDER BY {want}", (task_id,)
    ).fetchall()
    return [r[want] for r in rows]


def parent_ids(conn: sqlite3.Connection, task_id: str) -> list[str]:
    return _linked_ids(conn, "parent_id", "child_id", task_id)


def child_ids(conn: sqlite3.Connection, task_id: str) -> list[str]:
    return _linked_ids(conn, "child_id", "parent_id", task_id)


def task_graph_contexts(conn: sqlite3.Connection, task_ids: Iterable[str]) -> dict[str, dict]:
    """Bulk-load compact direct graph state for graph-aware diagnostics."""
    ordered_ids = list(dict.fromkeys(str(task_id) for task_id in task_ids if task_id))
    contexts = {task_id: {"parents": [], "children": []} for task_id in ordered_ids}
    if not ordered_ids:
        return contexts

    placeholders = ",".join("?" for _ in ordered_ids)
    for bucket, own, other in (("parents", "child_id", "parent_id"), ("children", "parent_id", "child_id")):
        for row in conn.execute(
            f"SELECT l.{own} AS owner_id, t.id, t.title, t.status "
            f"FROM task_links l JOIN tasks t ON t.id = l.{other} "
            f"WHERE l.{own} IN ({placeholders}) ORDER BY l.{own}, t.id", tuple(ordered_ids),
        ).fetchall():
            contexts[row["owner_id"]][bucket].append(
                {"id": row["id"], "title": row["title"], "status": row["status"]}
            )
    return contexts


def task_graph_context(conn: sqlite3.Connection, task_id: str) -> dict:
    """Return compact direct parent/child state for one task."""
    return task_graph_contexts(conn, [task_id])[task_id]


# --- Comments & events ---

def add_comment(conn: sqlite3.Connection, task_id: str, author: str, body: str) -> int:
    if not body or not body.strip():
        raise ValueError("comment body is required")
    if not author or not author.strip():
        raise ValueError("comment author is required")
    now = int(time.time())
    # ``allow_nested=True``: graph builders (kanban_swarm blackboard seeding)
    # compose comment writes under one outer commit.
    with write_txn(conn, allow_nested=True):
        _require_task(conn, task_id)
        cur = conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, ?, ?, ?)", (task_id, author.strip(), body.strip(), now),
        )
        _append_event(conn, task_id, "commented", {"author": author, "len": len(body)})
        return int(cur.lastrowid or 0)


def _require_task(conn: sqlite3.Connection, task_id: str) -> None:
    if not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
        raise ValueError(f"unknown task {task_id}")


def _task_rows(conn: sqlite3.Connection, table: str, task_id: str, order: str) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT * FROM {table} WHERE task_id = ? ORDER BY {order}", (task_id,)
    ).fetchall()


def list_comments(conn: sqlite3.Connection, task_id: str) -> list[Comment]:
    return [Comment.from_row(r) for r in _task_rows(conn, "task_comments", task_id, "created_at ASC")]


def list_comments_after(
    conn: sqlite3.Connection, task_id: str, *, after_id: int = 0
) -> list[Comment]:
    """Comments with ``id > after_id`` — keyed on rowid, not ``created_at``, so a
    same-second burst is never skipped (live worker comment bridge)."""
    rows = conn.execute(
        "SELECT id, task_id, author, body, created_at FROM task_comments "
        "WHERE task_id = ? AND id > ? ORDER BY id ASC", (task_id, int(after_id)),
    ).fetchall()
    return [Comment.from_row(r) for r in rows]


# --- Attachments ---

class AttachmentTooLarge(ValueError):
    """Attachment over the size cap. A ``ValueError`` so generic 400 handlers
    still catch it while the tool/CLI can give a 413-style message."""


def _safe_attachment_name(raw: str) -> str:
    """Client filename -> safe basename: strip directories (both separators),
    control chars and leading dots (no dotfiles, no traversal); ValueError when
    nothing usable remains. Only ever joined under the per-task attachments dir."""
    name = (raw or "").replace("\\", "/").split("/")[-1].strip()
    name = "".join(ch for ch in name if ch.isprintable() and ch not in "\x00").strip()
    name = name.lstrip(".").strip()
    if not name:
        raise ValueError("invalid attachment filename")
    return name[:200]


def _collision_free_path(dest_dir: Path, safe_name: str) -> Path:
    """``foo.pdf`` -> ``foo.pdf``, ``foo (1).pdf``, ... first one that doesn't exist."""
    stem, dot, ext = safe_name.partition(".")
    candidate = safe_name
    n = 1
    while (dest_dir / candidate).exists():
        candidate = f"{stem} ({n}){dot}{ext}"
        n += 1
    return dest_dir / candidate


def store_attachment_bytes(
    conn: sqlite3.Connection, task_id: str, filename: str, data: bytes, *,
    content_type: Optional[str] = None, uploaded_by: Optional[str] = None,
    board: Optional[str] = None, max_bytes: Optional[int] = None,
) -> int:
    """Single attachment write path (dashboard, tools, CLI): size cap, safe
    basename, collision-free blob under :func:`task_attachments_dir`, then the
    metadata row. Raises :class:`AttachmentTooLarge` / ``ValueError``; a blob
    whose row insert fails is removed before re-raising. Returns the new id."""
    if max_bytes is None:
        max_bytes = KANBAN_ATTACHMENT_MAX_BYTES
    if len(data) > max_bytes:
        raise AttachmentTooLarge(f"attachment exceeds {max_bytes // (1024 * 1024)} MB limit")
    safe_name = _safe_attachment_name(filename)
    dest_dir = task_attachments_dir(task_id, board=board)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = _collision_free_path(dest_dir, safe_name)
    dest_path.write_bytes(data)
    try:
        return add_attachment(
            conn, task_id, filename=dest_path.name, stored_path=str(dest_path.resolve()),
            content_type=content_type, size=len(data), uploaded_by=uploaded_by,
        )
    except Exception:
        # Don't leave an orphan blob if the metadata insert fails (most
        # commonly: the task id doesn't exist).
        with contextlib.suppress(OSError):
            dest_path.unlink(missing_ok=True)
        raise


def add_attachment(
    conn: sqlite3.Connection, task_id: str, *, filename: str, stored_path: str,
    content_type: Optional[str] = None, size: int = 0, uploaded_by: Optional[str] = None,
) -> int:
    """Record the metadata row (+ ``attached`` event) for a blob the caller already wrote."""
    if not filename or not filename.strip():
        raise ValueError("attachment filename is required")
    if not stored_path or not stored_path.strip():
        raise ValueError("attachment stored_path is required")
    now = int(time.time())
    with write_txn(conn):
        _require_task(conn, task_id)
        cur = conn.execute(
            "INSERT INTO task_attachments "
            "(task_id, filename, stored_path, content_type, size, uploaded_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, filename.strip(), stored_path, content_type, int(size), uploaded_by, now),
        )
        _append_event(
            conn, task_id, "attached",
            {"filename": filename.strip(), "size": int(size), "by": uploaded_by},
        )
        return int(cur.lastrowid or 0)


def list_attachments(conn: sqlite3.Connection, task_id: str) -> list[Attachment]:
    return [Attachment.from_row(r) for r in _task_rows(conn, "task_attachments", task_id, "created_at ASC, id ASC")]


def get_attachment(conn: sqlite3.Connection, attachment_id: int) -> Optional[Attachment]:
    r = conn.execute("SELECT * FROM task_attachments WHERE id = ?", (attachment_id,)).fetchone()
    return None if r is None else Attachment.from_row(r)


def delete_attachment(conn: sqlite3.Connection, attachment_id: int) -> Optional[Attachment]:
    """Delete the row (source of truth) and best-effort its blob; None when no row matched."""
    with write_txn(conn):
        att = get_attachment(conn, attachment_id)
        if att is None:
            return None
        conn.execute("DELETE FROM task_attachments WHERE id = ?", (attachment_id,))
        has_remaining_blob_reference = conn.execute(
            "SELECT 1 FROM task_attachments WHERE stored_path = ? LIMIT 1",
            (att.stored_path,),
        ).fetchone() is not None
        _append_event(conn, att.task_id, "attachment_removed", {"filename": att.filename})
    if not has_remaining_blob_reference:
        with contextlib.suppress(OSError):
            p = Path(att.stored_path)
            if p.is_file():
                p.unlink()
    return att


def list_events(conn: sqlite3.Connection, task_id: str) -> list[Event]:
    return [Event.from_row(r) for r in _task_rows(conn, "task_events", task_id, "created_at ASC, id ASC")]


def _insert_comment(
    conn: sqlite3.Connection, task_id: str, author: str, body: str, created_at: int,
) -> None:
    """Raw comment INSERT for callers already inside a write txn (``add_comment``
    opens its own txn and emits ``commented``)."""
    conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) "
        "VALUES (?, ?, ?, ?)", (task_id, author, body, created_at),
    )


# --- The designation door: the reserved tranche's ONE legitimate entrance ---------------
#
# A card in ``990000..999999`` is DESIGNATED, never requested: the board's armed storage guard
# refuses such a value unless a LIVE ``priority_designations`` row backs it, and that row is
# this door's first write (the card is parked at ``DESIGNATED_PRIORITY`` = 990000 afterwards,
# which a later ``edit --priority`` may raise inside the tranche).
#
# These functions are ported VERBATIM (bodies unchanged) from the recovered admission stack on
# ``card/t_44485b2b`` / PR yoyodine-industries/hermes-agent#78, which is the canonical copy.
# They ride the LIVE TREE because the 2026-10-10 ``hermes update`` half-applied its autostash
# restore and dropped them: the guard stayed armed while its door disappeared, so no ask and no
# SEV1 could be ranked (card t_38e29432). When the stack itself is restored, DELETE this block -
# it is the one small delta, and the only name that differs is ``_designation_policy`` where the
# stack has ``_policy_module`` (same job: the board's ``kanban_priority_policy`` module).

def _policy_module():
    """The board-scoped priority domain module (``kanban_priority_policy``), imported lazily.

    THE door's own reader of the domain numbers: ``TRANCHE_FLOOR`` is what the ledger
    row is written against and ``DESIGNATED_PRIORITY`` is where the card is parked.
    """
    from hermes_cli import kanban_priority_policy as policy

    return policy


#: The recovered admission stack's own name for the same reader (see the block comment
#: above): kept as an alias so the ported bodies need no renaming.
_designation_policy = _policy_module


def board_priority_policy(board: Optional[str] = None) -> Optional[dict]:
    """The normalised ``priority_policy`` for ``board``, or ``None`` when it has none.

    The read-only half of the birth seam: what a policy's spec actually resolves to,
    validated exactly as ``create_task`` validates it, so a caller can report or assert
    a board's wiring without filing a card. Raises ``PolicyError`` for a configured but
    unusable spec - the same refusal a filing would get.
    """
    policy = _policy_module()

    raw = read_board_metadata(board if board else get_current_board()).get(policy.POLICY_KEY)
    return policy.normalize_spec(raw)


#: The storage guard's two triggers, present only on a board that opted in.
PRIORITY_TRANCHE_TRIGGERS = (
    "tasks_priority_tranche_guard_insert",
    "tasks_priority_tranche_guard_update",
)

#: A DECLARED SEV1 line, byte-identical in convention to the fleet's band module
#: (``yaan-platform/scripts/kanban_priority_bands.py::SEV1_DECLARATION_RE``): a labelled
#: line, never a value - an undeclared card sitting at the top is an over-claim, not an SEV.
#:
#: The two alternations are DATA because the storage guard has to read the same line in SQL
#: (``_sql_tranche_entitlement``): SQLite has no regular expressions, so the guard's test is
#: a second reading of this one line.
SEV1_LABELS = ("severity", "sev", "critical", "priority[- ]class")
SEV1_VALUES = ("sev[- ]?1", "critical")

SEV1_DECLARATION_RE = re.compile(
    r"(?im)^[ \t>*\-]*({})[ \t]*[:=][ \t]*({})\b".format("|".join(SEV1_LABELS), "|".join(SEV1_VALUES)))

#: The SEV1 line as the STORAGE GUARD reads it (``_sql_tranche_entitlement``): upper-cased
#: prefixes and values, in the same order. Fewer terms than the regex's alternatives because
#: GLOB is a wildcard match - ``SEV`` reaches ``SEVERITY`` through the wildcard after it, and
#: ``PRIORITY`` covers ``priority[- ]class`` the same way. Wider on both axes is deliberate: a
#: guard that admits MORE than the doors write costs nothing (see that function), and a guard
#: that admits less is an aborted dispatcher tick.
_SQL_SEV1_LABELS = ("SEV", "CRITICAL", "PRIORITY")
_SQL_SEV1_VALUES = ("SEV1", "SEV-1", "SEV 1", "CRITICAL")

#: The ``Operator-ask: <register>/<ask>`` line the register writes
#: (``kanban_register._STAMP_RE``): one line, no matter how long the body is.
_ASK_STAMP_RE = re.compile(r"^[ \t]*Operator-ask:[ \t]*(\S+)[ \t]*$", re.MULTILINE)


def sev1_declared(text: Optional[str]) -> bool:
    """Is a SEV1 DECLARED on a labelled line in *text*? (the fleet's SEV marker)"""
    return bool(SEV1_DECLARATION_RE.search(text or ""))


def tranche_entitlement(
    conn: sqlite3.Connection, *, board: Optional[str] = None, task_id: Optional[str] = None,
    title: Optional[str] = "", body: Optional[str] = "",
) -> str:
    """The card's OWN evidence that it may hold a reserved-tranche priority - ``""`` if none.

    THE PREDICATE THE STORAGE GUARD IMPLEMENTS IN SQL (see ``_sql_tranche_entitlement``), and
    the one every door that may WRITE INTO the band has to read, because door 3 aborts the
    write otherwise and an abort inside the dispatcher's reclaim phase takes the whole
    board's dispatching down with it (measured 2026-10-01: ``defcon`` spawned nothing for
    6.5 hours).

    Why the card's OWN evidence, and nothing inherited. The marker is "the card's own
    evidence: an operator-ask ref, a live designation, or a declared SEV1 line", and a
    database trigger can only read the row it is about to write: it cannot walk a parent
    chain, cannot resolve a register reference that lives on another board, and cannot
    re-derive anything. The one inherited form the wider resolver also accepts - an ask
    inherited from a parent - is materialised on the card at birth: the create seam stamps
    every filing that resolved an ask, from any source, in the same INSERT (``apply_stamp``).
    A row that carries no stamp of its own therefore carries no ask.

    Returns the marker's NAME, in the resolver's order, so the record says which evidence
    admitted the row: ``"operator-ask-stamp"``, ``"designation"`` or ``"sev1"``.
    """
    if _ASK_STAMP_RE.search(body or ""):
        return "operator-ask-stamp"
    if task_id and is_priority_designated(conn, task_id, board=board):
        return "designation"
    if sev1_declared("{}\n{}".format(title or "", body or "")):
        return "sev1"
    return ""


def _sql_tranche_entitlement(ident: str, title: str, body: str) -> str:
    """The storage layer's reading of :func:`tranche_entitlement`, over one row.

    ``ident`` / ``title`` / ``body`` are SQL expressions naming the row's columns
    (``NEW.id`` / ``NEW.title`` / ``NEW.body`` inside a trigger).

    Two readings of one line, and they are not the same text, so the SHAPE matters:

    * the register's stamp is read by its KEYWORD (``INSTR(body, 'Operator-ask:')``). That is
      a superset of the register's own line regex - it does not require the line to start
      with the keyword - and it is a superset in the direction that matters: a guard may
      admit MORE than the doors write (the evidence it reads is text in the card's own row,
      so whoever can write the row can write the stamp line too), and may never admit LESS,
      because less is an abort inside the dispatcher's reclaim phase and an aborted tick
      dispatches nothing at all (measured 2026-10-01: 6.5 h, 452 ready cards, zero spawns on
      ``defcon``).
    * the declared SEV1 line is read the same way, as ``label ... [: or =] ... value`` with
      only the ORDER required. The line-anchored form is NOT expressible here: GLOB's
      ``[...]`` class has no zero-repetition form, so "a line that begins with optional
      indentation" cannot be written at all - and the live boards do carry an indented
      declaration (measured 2026-10-02: 1 of 47 is ``'    Severity: SEV1'``), which the
      anchored form would miss.
    """
    nl = "char(10)"
    framed = f"UPPER(COALESCE({title}, '') || {nl} || COALESCE({body}, '') || {nl})"
    word_end = "[^A-Za-z0-9_]*"
    designation = (f"EXISTS (SELECT 1 FROM priority_designations d WHERE d.task_id = {ident} "
                   "AND d.revoked_at IS NULL)")
    stamp = f"INSTR(COALESCE({body}, ''), 'Operator-ask:') > 0"
    sev1 = " OR ".join(
        f"{framed} GLOB ('*{label}*[:=]*{value}{word_end}')"
        for label in _SQL_SEV1_LABELS for value in _SQL_SEV1_VALUES)
    return f"(({stamp}) OR ({designation}) OR ({sev1}))"


def priority_tranche_trigger_ddl(floor: Optional[int] = None) -> tuple:
    """DDL for the storage guard: refuse a reserved-tranche priority, and a below-floor one.

    The bound lives in the DATABASE rather than in a caller because the fleet's own authoring
    skill documents raw SQL as its re-rank lever - a kernel-level bound alone would be advice.
    Opt-in per board, applied by the same wiring step that writes ``priority_policy``, so an
    unwired board and every test board stay byte-identical.

    ``floor`` is a board's floor (``kanban_priority_policy.board_floor``) and adds one clause
    to each trigger: a write that lands a card BELOW the floor is refused too. Two properties
    of that clause are deliberate:

    * it fires on a CHANGE only (``NEW.priority <> OLD.priority``), because a no-op write that
      re-sets a value already below the floor must not abort - a generic field-update path on a
      pre-existing below-floor card would otherwise break, which is exactly the state a board
      is in while its tail is being drained;
    * it is BAKED IN at arm time, so a board whose floor changed is re-armed by its wiring
      action (``sync_priority_tranche_guard``) rather than by a redeploy of the module it
      reads - a stored trigger cannot re-read a file. ``floor is None`` renders the DDL this
      function has always rendered, byte for byte.

    The tranche clause's PREDICATE is the marker set the doors read
    (``tranche_entitlement``): the value is refused unless the row carries its OWN evidence -
    a live designation, the register's stamp line, or a declared SEV1 line.
    """
    policy = _policy_module()
    # A board's declared floor is a legitimate value even when it sits ON the tranche
    # boundary: a row written AT the floor is the board's own declaration, so the tranche
    # clause must not fire on it.
    on_tranche_boundary = floor is not None and int(floor) == policy.TRANCHE_FLOOR
    boundary_clause = f" AND NEW.priority <> {int(floor)}" if on_tranche_boundary else ""
    live = (
        f"NEW.priority BETWEEN {policy.TRANCHE_FLOOR} AND {policy.TRANCHE_TOP} AND NOT "
        f"({_sql_tranche_entitlement('NEW.id', 'NEW.title', 'NEW.body')}){boundary_clause}"
    )
    message = (f"priority {policy.TRANCHE_FLOOR}-{policy.TRANCHE_TOP} is DESIGNATED, never "
               "requested: use 'hermes kanban defcon designate'")
    # The message is prose and carries a quote (the verb the caller should run); a SQL string
    # literal ends at the first one, so double it before it goes into the DDL.
    message = message.replace("'", "''")
    change = ""
    insert_floor = ""
    if floor is not None:
        # A CHANGE guard, not a state guard: a no-op write that re-sets a value already below
        # the floor must not abort, or every generic field-update path on a pre-existing
        # below-floor card would break while that board's tail is being drained.
        change = f"(NEW.priority <> OLD.priority AND NEW.priority < {int(floor)})"
        # An INSERT has no OLD row, so there the floor clause is a state clause: a card may not
        # be born below the floor either.
        insert_floor = f"(NEW.priority < {int(floor)})"
        # Both clauses sit in ONE trigger per event, so the refusal has to say WHICH one fired:
        # a value inside the reserved tranche is the designation message, anything else is the
        # floor's. ``floor is None`` renders the literal this function has always rendered.
        floor_message = (f"priority is below this board's floor {int(floor)}: no card may sit "
                         "there - file it on its own board, or use the designation door if it "
                         "must outrank this board")
        quoted_floor = floor_message.replace("'", "''")
        message = (f"CASE WHEN NEW.priority BETWEEN {policy.TRANCHE_FLOOR} AND "
                   f"{policy.TRANCHE_TOP} THEN '{message}' ELSE '{quoted_floor}' END")
    else:
        # No floor: the argument is the tranche literal, quoted exactly as before.
        message = f"'{message}'"
    body = f"BEGIN\n  SELECT RAISE(ABORT, {message});\nEND;"
    if floor is not None:
        when_insert = f"({live}) OR {insert_floor}"
        when_update = f"({live}) OR {change}"
    else:
        when_insert = when_update = live
    return (
        f"CREATE TRIGGER IF NOT EXISTS {PRIORITY_TRANCHE_TRIGGERS[0]}\nBEFORE INSERT ON tasks\nFOR EACH ROW\nWHEN {when_insert}\n{body}",
        f"CREATE TRIGGER IF NOT EXISTS {PRIORITY_TRANCHE_TRIGGERS[1]}\nBEFORE UPDATE OF priority ON tasks\nFOR EACH ROW\n"
        f"WHEN {when_update}\n{body}",
    )


def _connect_board(board: Optional[str] = None):
    """A connection to *board*'s own DB (open/init belongs to the connect module)."""
    from hermes_cli import kanban_db_connect as kbc

    return kbc.connect_closing(board=board)


def priority_tranche_guards(conn: sqlite3.Connection) -> list:
    """The storage-guard triggers present on this connection's DB (``[]`` when unarmed)."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name IN (?, ?)",
        PRIORITY_TRANCHE_TRIGGERS,
    ).fetchall()
    return sorted(row["name"] for row in rows)


def _board_priority_floor(board: Optional[str] = None) -> Optional[int]:
    """The floor *board*'s policy states, or ``None`` when that board has none.

    Read at the WIRING moment rather than per write: the floor is baked into the armed
    triggers, so nothing on the write path has to load a policy file. Unusable wiring raises
    :class:`PolicyError`, which is what stops a board being armed with a floor nobody can read.
    """
    policy = _policy_module()
    slug = board if board else get_current_board()
    spec = _board_meta_for(board).get(policy.POLICY_KEY)
    if not policy.board_is_wired(spec):
        return None
    return policy.board_floor(spec, slug)


def arm_priority_tranche_guard(board: Optional[str] = None) -> list:
    """Apply the storage guard to *board*; the triggers present afterwards.

    RE-ARMING REPLACES the triggers rather than leaving them alone. The board's floor is baked
    into the DDL, so a wiring action has to be able to put a different floor in place - a
    ``CREATE TRIGGER IF NOT EXISTS`` on its own would silently keep whatever floor the board
    was first armed with.
    """
    _assert_not_delegated_child_mutation(kanban_db_path(board=board))
    floor = _board_priority_floor(board)
    with _connect_board(board) as conn:
        for name in PRIORITY_TRANCHE_TRIGGERS:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        for statement in priority_tranche_trigger_ddl(floor):
            conn.execute(statement)
        return priority_tranche_guards(conn)


def disarm_priority_tranche_guard(board: Optional[str] = None) -> list:
    """Drop the storage guard from *board* - the unwired board's behaviour, exactly."""
    _assert_not_delegated_child_mutation(kanban_db_path(board=board))
    with _connect_board(board) as conn:
        for name in PRIORITY_TRANCHE_TRIGGERS:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        return priority_tranche_guards(conn)


def sync_priority_tranche_guard(board: Optional[str] = None) -> str:
    """Make *board*'s storage guard follow its ``priority_policy`` key; returns its state.

    ``"armed"`` when the board is wired (guard in place), ``"unarmed"`` when it is not (no
    guard, so the board behaves exactly as it did before this seam existed). The wiring action
    calls this, so the guard can never drift from the key that turns the domain on.
    """
    policy = _policy_module()
    spec = _board_meta_for(board).get(policy.POLICY_KEY)
    if policy.board_is_wired(spec):
        arm_priority_tranche_guard(board)
        return "armed"
    disarm_priority_tranche_guard(board)
    return "unarmed"


def _refuse_priority_outside_domain(conn: sqlite3.Connection, priority: int, *,
                                    board: Optional[str] = None,
                                    task_id: Optional[str] = None) -> None:
    """Door 2: a re-rank outside the ordinary domain is REFUSED - on EVERY board.

    Refused rather than clamped, because a deliberate re-rank is a deliberate act and silently
    altering it hides the caller's bug - which is also what keeps a sweep from writing a bogus
    target.

    THE DOMAIN HALF IS UNCONDITIONAL: the domain is the KERNEL's, not a board's, so a value
    outside ``ORDINARY_MIN..ORDINARY_MAX`` cannot be stored whether or not the board carries a
    ``priority_policy``. The original gating left every unwired board open, which is how
    ``defcon`` came to hold 101 rows at 1100000.

    THE BOARD'S OWN FLOOR is read BEFORE the tranche branch, and a row AT it is admitted WITHOUT
    a marker: placing a card on its floor is the board's own act, not a request for the reserved
    band. That is what makes a floor ON the tranche boundary (``TRANCHE_FLOOR``) usable - the one
    floor inside the band - while every OTHER value in the reserved tranche stays designation-only,
    on every board. The floor read is deliberately NOT wrapped: a floor the board's wiring cannot
    answer is unusable wiring and surfaces here, exactly as :func:`board_floor` documents.
    """
    policy = _policy_module()
    value = int(priority)
    slug: Optional[str] = None
    spec: Any = None
    try:
        slug = board_for_connection(conn) or board
        spec = _board_meta_for(slug).get(policy.POLICY_KEY)
    except Exception:
        # "Cannot tell which board this is" stays inert for the FLOOR half: a metadata read
        # that failed must not become a refusal. The domain half below does not need it.
        spec = None
    if value > int(policy.MAX_PRIORITY):
        # ABOVE THE CEILING is the CEILING DOOR's call, not this one's: a value over
        # ``MAX_PRIORITY`` reaching here is the marker-carrying lift that door has already
        # judged, and re-deciding it here would leave that exemption unreachable.
        return
    floor: Optional[int] = None
    if policy.board_is_wired(spec):
        floor = policy.board_floor(spec, slug or "")
    if not policy.in_ordinary(value):
        if floor is not None and value == floor:
            return
        # Inside the reserved tranche - the band the asks and SEVs hold - reachable only by a
        # card carrying its OWN evidence, read by the SAME predicate the storage guard enforces
        # (``tranche_entitlement``), so this door's "allowed" cannot become the database's
        # "aborted".
        in_tranche = policy.in_tranche(value)
        marker = ""
        if in_tranche and task_id:
            text = _task_text(conn, task_id)
            marker = tranche_entitlement(
                conn, board=slug, task_id=task_id, title="", body=text,
            )
        if not (in_tranche and marker):
            raise policy.PriorityOutOfDomain(
                f"priority {value} is outside the ordinary domain "
                f"({policy.ORDINARY_MIN}..{policy.ORDINARY_MAX}): {policy.domain_reason(value)}"
            )
    if floor is not None and value < floor:
        raise policy.PriorityOutOfDomain(
            f"priority {value} is below this board's floor {floor}: cards on "
            f"{slug or 'this board'} are filed at the top band "
            f"({floor}..{policy.ORDINARY_MAX}) - file it on its own board, or use the "
            "designation door if it must outrank them"
        )


def _task_text(conn: sqlite3.Connection, task_id: str) -> str:
    """``title + body`` of an existing card, or ``""`` - the marker's own evidence."""
    row = conn.execute("SELECT title, body FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return ""
    return "{}\n{}".format((row["title"] or ""), (row["body"] or ""))


#: The identity every deviation row carries - the guard, never the filer.
ABOVE_TRANCHE_GUARD_IDENTITY = "kanban-priority-guard"

#: Prefix of the public deviation rows this guard files (one per distinct violation).
ABOVE_TRANCHE_DEVIATION_PREFIX = "PRIORITY OVER-CLAIM (above the reserved tranche)"


def above_tranche_marker(
    conn: sqlite3.Connection, *, board: Optional[str] = None,
    task_id: Optional[str] = None, title: Optional[str] = "", body: Optional[str] = "",
    ask_ref: Optional[Any] = None,
) -> str:
    """Which MARKER admits this card above the reserved tranche - ``""`` when it has none.

    Read-only, and the same answer for a filing (title/body/``ask_ref``) and for a row that
    already exists (``task_id`` reads the designation ledger). Deterministic order, so the
    record is reproducible: the filing's own ask reference, then the register's stamp, then a
    live designation, then a declared SEV1.
    """
    ref = ask_ref
    if ref is not None and not getattr(ref, "unresolved", "") and getattr(ref, "register", None):
        # The source is part of the label: an explicit `serves=`, the worker's env, a body
        # stamp and a parent chain are different evidence, and the record must say which.
        return "operator-ask-ref:%s" % (getattr(ref, "source", "") or "ref")
    if _ASK_STAMP_RE.search(body or ""):
        return "operator-ask-stamp"
    if task_id and is_priority_designated(conn, task_id, board=board):
        return "designation"
    if sev1_declared("{}\n{}".format(title or "", body or "")):
        return "sev1"
    return ""


ABOVE_TRANCHE_ALTERNATIVES = (
    "Legitimate move 1 - file it INSIDE the domain: any value up to %d (the maximum band top) "
    "is accepted as filed, and that is where a lane's own work belongs. "
    "Legitimate move 2 - if the card IS the operator's ask (a card under the register, or one "
    "filed in service of it) or a DECLARED SEV1, CARRY THE MARKER and it is admitted into the "
    "reserved tranche at %d: pass `serves=` / write the `Operator-ask: <register>/<ask>` line / "
    "declare `Severity: SEV1` on a labelled line. "
    "Legitimate move 3 - a card that must RANK inside the tranche is DESIGNATED, and that is a "
    "host/operator act: `hermes kanban defcon designate <id> --reason ...` (see "
    "hermes_cli/kanban_register.py)."
)


class AboveTrancheRefused(ValueError):
    """Raised when a filing or a re-rank claims a value above the reserved tranche.

    Carries the structured facts a caller needs to fix the call rather than the guard:
    ``lane`` (the filing lane, by name), ``attempted_priority``, ``board``, ``marker`` (always
    empty on this path), ``deviation_task_id`` (the public refusal row) and ``deviation_error``.
    """

    def __init__(self, message: str, *, lane: str = "", title: str = "",
                 attempted_priority: int = 0, board: str = "", ceiling: int = 0,
                 deviation_task_id: Optional[str] = None,
                 deviation_error: Optional[str] = None):
        super().__init__(message)
        self.lane = lane or ""
        self.title = title or ""
        self.attempted_priority = int(attempted_priority or 0)
        self.board = board or ""
        self.ceiling = int(ceiling or 0)
        self.deviation_task_id = deviation_task_id
        self.deviation_error = deviation_error


def above_tranche_refusal_message(
    title, lane, attempted_priority, ceiling, deviation_task_id=None, deviation_error=None,
) -> str:
    """The refusal text; returned verbatim by every surface (tool, CLI, kernel)."""
    who = lane or "an unidentified caller"
    return " ".join([
        f"refused: priority {int(attempted_priority)} is above the reserved top tranche "
        f"({int(ceiling)} is the maximum). Attempted title {title!r}, filer {who}. "
        f"NOTHING WAS CREATED - no task row and no event.",
        "Why: the operator's asks and SEVs hold the reserved top tranche "
        "(operator ruling 2026-09-28), nothing else is admitted above them, and a lane's own "
        "work outranking an ask is what the ruling ends. The value is not clamped silently: a "
        "hand-lift above the ceiling is the door that put 52 rows over the ask band on `defcon` "
        "(measured 2026-09-28).",
        ABOVE_TRANCHE_ALTERNATIVES % (_ordinary_edge(), ceiling),
    ] + ([
        f"Enforcement: the attempt is escalated and the deviation is filed on this board "
        f"against the filing lane by name - task {deviation_task_id}. Fix the call, not the "
        f"guard."
    ] if deviation_task_id else [
        "Enforcement: the deviation row could NOT be filed"
        + (f" ({deviation_error})" if deviation_error else "")
        + " - the refusal stands regardless; report this to platform-stl."
    ]))


def _ordinary_edge() -> int:
    """The ordinary domain's edge, read through the policy module (never a second copy)."""
    return int(_policy_module().ORDINARY_MAX)


def _above_tranche_deviation_key(board, lane, priority, title) -> str:
    """One durable deviation row per distinct violation - never a card per retry."""
    from hashlib import sha1

    digest = sha1((title or "").strip().encode("utf-8")).hexdigest()[:12]
    return f"above-tranche-refused:{board or ''}:{lane or 'unknown'}:{int(priority)}:{digest}"


def file_above_tranche_deviation(
    conn: sqlite3.Connection, *, board: Optional[str] = None, lane: Optional[str] = None,
    title: Optional[str] = "", attempted_priority: int = 0, tenant: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """File the public refusal row for an above-tranche attempt. Returns ``(task_id, error)``.

    Never raises: the refusal must not depend on its own side effect, so a filing failure is
    carried in the refusal message (and on the exception) instead of being swallowed or turning
    the refusal into an error. The row is filed against the FILING LANE by name.
    """
    policy = _policy_module()
    who = (lane or "").strip()
    assignee = _canonical_assignee(who) if who else None
    if not assignee:
        # An unidentified caller still gets a public row; the ops head owns the chase.
        assignee = "default"
    attempt = " ".join((title or "").split())[:120] or "(untitled)"
    body = "\n".join([
        "**Enforcement record - a filing above the reserved tranche was REFUSED at the "
        "create path.**",
        "",
        f"* **filing lane (by name):** `{who or 'unidentified caller - row filed to default'}`",
        f"* **attempted priority:** `{int(attempted_priority)}` "
        f"(the ceiling is `{policy.MAX_PRIORITY}`, the reserved tranche is "
        f"`{policy.TRANCHE_FLOOR}..{policy.TRANCHE_TOP}`)",
        f"* **attempted title:** `{attempt}`",
        "* **enforcement action:** REFUSED. Nothing was created - no task row and no `created` "
        "event.",
        "* **standard:** the operator's asks and SEVs hold the reserved top tranche; nothing "
        "else is admitted above them (operator ruling 2026-09-28, design record platform-stl "
        "`t_6ce41549`).",
        f"* **what to do instead:** {ABOVE_TRANCHE_ALTERNATIVES % (policy.ORDINARY_MAX, policy.TRANCHE_TOP)}",
        "",
        "This row is the escalation of the attempt AND the public reprimand: filed on the board "
        "the attempt targeted, against the filing lane by name, in the open.",
        "",
        "The lane owns the correction: (a) the **fix** - file inside the domain, carry the ask "
        "marker if the card IS the operator's ask or a declared SEV1, or ask the ops head to "
        "designate it; (b) the **cost** - an above-tranche filing is refused, and repeats "
        "collapse onto this same row; (c) the **acceptance** - reply on this card naming which "
        "of the three moves the lane used, and re-run the call.",
    ])
    try:
        tid = create_task(
            conn,
            title=f"{ABOVE_TRANCHE_DEVIATION_PREFIX}: {who or 'unidentified lane'} attempted "
                  f"p{int(attempted_priority)} on {attempt!r}",
            body=body,
            assignee=assignee,
            board=board,
            tenant=tenant,
            created_by=ABOVE_TRANCHE_GUARD_IDENTITY,
            idempotency_key=_above_tranche_deviation_key(board, who, attempted_priority, title),
        )
    except Exception as exc:  # the record's failure must never replace the refusal
        return None, f"{type(exc).__name__}: {exc}"
    return str(tid), None


def _refuse_above_tranche(
    conn: sqlite3.Connection, *, board: Optional[str], lane: Optional[str],
    title: Optional[str], attempted_priority: int, tenant: Optional[str] = None,
) -> AboveTrancheRefused:
    """File the deviation, then build the refusal. The refusal is unconditional."""
    policy = _policy_module()
    deviation_id, deviation_error = file_above_tranche_deviation(
        conn, board=board, lane=lane, title=title,
        attempted_priority=attempted_priority, tenant=tenant)
    return AboveTrancheRefused(
        above_tranche_refusal_message(
            title, lane, attempted_priority, policy.MAX_PRIORITY,
            deviation_id, deviation_error),
        lane=lane or "", title=title or "", attempted_priority=attempted_priority,
        board=board or "", ceiling=policy.MAX_PRIORITY,
        deviation_task_id=deviation_id, deviation_error=deviation_error,
    )


def apply_above_tranche_guard(
    conn: sqlite3.Connection, requested: int, applied: int, *, board: Optional[str] = None,
    lane: Optional[str] = None, title: Optional[str] = "", body: Optional[str] = "",
    ask_ref: Optional[Any] = None, tenant: Optional[str] = None,
) -> tuple[int, Optional[dict]]:
    """The create seam's above-tranche half: ``(priority, record)``.

    Reads the REQUESTED value (not the board policy's output) so a wired board's clamp cannot
    silently absorb an over-claim, and runs for every board - the ceiling is not a board's
    property, it is the scale's. A clean filing comes back with the applied value and ``None``,
    so a correctly filed card keeps today's byte-identical event.
    """
    policy = _policy_module()
    ceiling = int(policy.MAX_PRIORITY)
    asked = int(requested)
    if asked <= ceiling:
        return int(applied), None
    marker = above_tranche_marker(
        conn, board=board, title=title, body=body, ask_ref=ask_ref)
    if not marker:
        raise _refuse_above_tranche(
            conn, board=board, lane=lane, title=title, attempted_priority=asked,
            tenant=tenant,
        )
    return ceiling, {
        "above_tranche": {
            "requested": asked,
            "applied": ceiling,
            "marker": marker,
            "reason": (
                f"carries the {marker} marker: admitted into the reserved tranche at its top "
                f"({ceiling}); the scale ends there"
            ),
            "bounds": [int(policy.TRANCHE_FLOOR), ceiling],
        },
    }


def _merge_policy_record(record: Optional[dict], extra: Optional[dict]) -> Optional[dict]:
    """Merge the above-tranche record into the one the ``created`` event carries, or ``None``.

    When the above-tranche door placed the card, the ordinary-domain clamp clause (if the board
    policy had one) no longer describes what happened - it would carry a second, contradicting
    ``applied`` value - so it is dropped rather than left to be read as the outcome.
    """
    if not extra:
        return record
    merged = dict(record or {})
    merged.update(extra)
    if "above_tranche" in merged:
        merged["applied"] = merged["above_tranche"]["applied"]
        merged["clamped"] = True
        merged.pop("domain", None)
    return merged


def _refuse_above_tranche_rerank(
    conn: sqlite3.Connection, task_id: str, priority: int, *, board: Optional[str] = None,
) -> None:
    """Door 2b: a RE-RANK above the ceiling is refused unless the CARD carries a marker.

    Deliberately not gated on the board's ``priority_policy``, unlike the ordinary-domain door
    above it: the board-gated version is inert everywhere the domain is unwired, and the
    hand-lift it would have caught is how 52 rows came to sit over the asks on ``defcon``
    (measured 2026-09-28). The refusal names the card, the ceiling and the three legitimate
    moves; the card's own lane already owns it, so no deviation row is filed - the caller is
    standing at the card.
    """
    policy = _policy_module()
    value = int(priority)
    if value <= int(policy.MAX_PRIORITY):
        return
    slug = board or board_for_connection(conn)
    row = conn.execute(
        "SELECT title, body FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no such task {task_id} on this board")
    ask_ref = _resolve_operator_ask(
        conn, board=slug, parents=(task_id,), body=row["body"] or "", serves=None,
    )
    marker = above_tranche_marker(
        conn, board=slug, task_id=task_id, title=row["title"] or "", body=row["body"] or "",
        ask_ref=ask_ref,
    )
    if marker:
        return
    raise AboveTrancheRefused(
        " ".join([
            f"refused: priority {value} is above the reserved top tranche "
            f"({int(policy.MAX_PRIORITY)} is the maximum) and {task_id} carries no "
            f"operator-ask/SEV marker, so the re-rank would put plain lane work over the "
            f"operator's asks. Nothing was written.",
            "Why: the operator's asks and SEVs hold the reserved top tranche (operator ruling "
            "2026-09-28); a non-ask card already above it is lowered, not argued with, and "
            "every board carries the same ceiling because the hand-lift is door-inert "
            "nowhere. A card that must rank in the tranche is DESIGNATED - "
            "`hermes kanban defcon designate <id> --reason ...`.",
            ABOVE_TRANCHE_ALTERNATIVES % (int(policy.ORDINARY_MAX), int(policy.TRANCHE_TOP)),
        ]),
        attempted_priority=value, board=slug or "", ceiling=int(policy.MAX_PRIORITY),
    )


def demote_above_tranche(
    conn: sqlite3.Connection, *, board: Optional[str] = None, reason: str = "",
) -> list[dict]:
    """THE REPAIR PASS: lower every row holding a value above the ceiling into its class ceiling.

    Deterministic, idempotent, board-local, and keyed on the SAME predicate the STORAGE guard
    reads (``tranche_entitlement`` - the row's own evidence, which is the only evidence a trigger
    can see):

    * a row carrying that evidence (the register's stamp - which every ask-filed card carries -
      a live designation, or a declared SEV1) belongs INSIDE the reserved tranche, so it is
      lowered to ``TRANCHE_TOP``: the top of the scale, but not above it;
    * anything else is the class the ruling bans above the asks, so it is lowered to
      ``ORDINARY_MAX`` - exactly where the create door files a card that claims too much today.

    Each lowering is recorded on the card as a ``priority_demoted`` event, so the repair is
    visible to the lane whose card moved, and a clean board costs one SELECT. Returns the ledger
    (``task_id``, ``was``, ``now``, ``marker``) for the tick that ran it.

    THE LANDING VALUE IS THE STORAGE GUARD'S PREDICATE (``tranche_entitlement``), not a wider
    reading of the same row: a value written into the reserved band on evidence the guard cannot
    see is a write the guard aborts, and an abort here used to take the whole board's dispatching
    with it (2026-10-01). A row whose marker is only inherited - an ask on an ancestor, which
    every card born under an ask carries in its own body as the register's stamp - is therefore
    landed in the ordinary domain, which is also where the guard will accept it.

    ONE ROW CANNOT STOP THE REST. Each lowering is its own transaction, and a refused write is
    reported as a ledger row carrying ``error`` (with ``now`` ``None``) instead of raising: this
    pass is a best-effort repair of an invariant that is already broken, and the rows after a
    refused one still deserve the repair. A ledger row with an ``error`` is NOT a success - the
    dispatcher records it and logs it.

    THE ORDINARY LANDING MUST BE A VALUE THE BOARD'S OWN GUARD ACCEPTS. A board may declare a
    floor ABOVE ``ORDINARY_MAX`` - ``defcon`` declares 990000, which is where every ordinary card
    on that board belongs (its birth door lifts below-floor filings there) - and the armed floor
    clause refuses any write below it. Landing at ``ORDINARY_MAX`` there is a write the guard
    aborts, so the pass could not repair the very board the ruling is about (measured
    2026-10-15: 13 unmarked rows at 1000000, every one of them unrepairable and one ERROR line
    per tick). The landing is therefore ``max(ORDINARY_MAX, the board's floor)``; on a board
    that declares no floor it stays ``ORDINARY_MAX``, byte-identical to the ported behaviour.
    The floor read is fail-soft like everything else here: a floor that cannot be answered
    leaves the ordinary edge, because a best-effort repair must never raise.
    """
    policy = _policy_module()
    slug = board or board_for_connection(conn) or ""
    ceiling = int(policy.MAX_PRIORITY)
    ordinary = int(policy.ORDINARY_MAX)
    try:
        floor = _board_priority_floor(slug or None)
    except Exception:
        floor = None
    if floor is not None:
        ordinary = max(ordinary, int(floor))
    rows = conn.execute(
        "SELECT id, priority, assignee, title, body FROM tasks "
        "WHERE priority > ? AND status != 'archived' ORDER BY priority DESC, created_at ASC",
        (ceiling,),
    ).fetchall()
    ledger: list[dict] = []
    for row in rows:
        marker = tranche_entitlement(
            conn, board=slug, task_id=row["id"], title=row["title"] or "",
            body=row["body"] or "",
        )
        now = ceiling if marker else ordinary
        was = int(row["priority"])
        if now == was:
            continue
        try:
            with write_txn(conn):
                conn.execute("UPDATE tasks SET priority = ? WHERE id = ?", (now, row["id"]))
                _append_event(conn, row["id"], "priority_demoted", {
                    "from": was,
                    "to": now,
                    "ceiling": ceiling,
                    "marker": marker,
                    "reason": reason or (
                        "above the reserved tranche: {}".format(
                            f"marker {marker} - lowered to the tranche top" if marker
                            else "no operator-ask/SEV marker - lowered into the ordinary domain")),
                })
        except sqlite3.Error as exc:
            # Not swallowed: the row is returned with the refusal and logged at ERROR, and the
            # rest of the pass continues (one unrepairable row must not strand every other one).
            detail = f"{type(exc).__name__}: {exc}"
            _log.error(
                "kanban: the above-tranche repair could not lower %s on board %s (%s -> %s, "
                "marker %r): %s",
                row["id"], slug or "-", was, now, marker, detail,
            )
            ledger.append({"task_id": row["id"], "was": was, "now": None, "marker": marker,
                           "error": detail})
            continue
        ledger.append({"task_id": row["id"], "was": was, "now": now, "marker": marker})
    return ledger

def priority_designation(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    """The card's designation row - live or revoked - or ``None``."""
    row = conn.execute(
        "SELECT task_id, board, priority, authority, reason, designated_at, revoked_at "
        "FROM priority_designations WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def is_priority_designated(conn: sqlite3.Connection, task_id: str, *,
                           board: Optional[str] = None) -> bool:
    """Is *task_id* in the reserved tranche BY DESIGNATION? (the guard's predicate)

    The exemption the storage guard and the fleet's sweep both read: a row here, not revoked,
    is what makes a tranche priority legitimate for one card - and nothing else does.
    """
    sql = "SELECT 1 FROM priority_designations WHERE task_id = ? AND revoked_at IS NULL"
    params: list = [task_id]
    if board:
        sql += " AND board = ?"
        params.append(board)
    return conn.execute(sql, params).fetchone() is not None


def _designation_stamp(now: object = None) -> str:
    """An ISO-8601 UTC stamp: the ledger's two stamp columns are TEXT, so the audit reads them."""
    return str(now) if now else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def designate_priority(conn: sqlite3.Connection, task_id: str, *, reason: str,
                       authority: Optional[str] = None, board: Optional[str] = None,
                       now: object = None, nested: bool = False) -> dict:
    """The designation door: ledger row FIRST, then the card into the reserved tranche.

    In that order because the storage guard refuses a tranche value with no live designation -
    the trigger is what makes "only this door" true rather than advisory. The ledger keeps the
    card's ordinary priority, so revoke returns the card exactly to the rest it left instead of
    to a value re-derived afterwards.

    ``nested`` is the gate lift's opt-in: the relation invariant is held INSIDE the txn that
    created the edge (``_link``) or wrote the re-rank (``edit_task``), so the door has to run
    under an open outer transaction. It is explicit - never inferred from
    ``conn.in_transaction`` - because every other caller must keep failing loudly on an
    accidental nest. This is the one door whose whole effect is rows in the caller's own txn:
    no post-commit side effect can fire while an outer txn can still roll back.
    """
    policy = _designation_policy()
    _assert_not_delegated_child_mutation(kanban_db_path(board=board))
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("a designation needs a reason (it is the audit answer)")
    current = conn.execute("SELECT priority FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if current is None:
        raise ValueError(f"no such task {task_id} on this board")
    slug = board or board_for_connection(conn) or get_current_board()
    stamp = _designation_stamp(now)
    with write_txn(conn, allow_nested=nested):
        conn.execute(
            "INSERT INTO priority_designations "
            "(task_id, board, priority, authority, reason, designated_at, revoked_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL) "
            "ON CONFLICT(task_id) DO UPDATE SET board = excluded.board, "
            "priority = excluded.priority, authority = excluded.authority, "
            "reason = excluded.reason, designated_at = excluded.designated_at, revoked_at = NULL",
            (task_id, slug, int(current["priority"]), authority, reason, stamp),
        )
        conn.execute(
            "UPDATE tasks SET priority = ? WHERE id = ?", (policy.DESIGNATED_PRIORITY, task_id),
        )
        _append_event(conn, task_id, "reprioritized", {
            "priority": policy.DESIGNATED_PRIORITY, "designation": "designated",
            "authority": authority, "reason": reason,
        })
    return {
        "task_id": task_id, "board": slug, "priority": policy.DESIGNATED_PRIORITY,
        "restore_priority": int(current["priority"]), "authority": authority, "reason": reason,
        "designated_at": stamp, "revoked_at": None,
    }


def _release_designation_rows(conn: sqlite3.Connection, task_id: str, *, reason: str = "",
                              cause: str = "revoke", now: object = None) -> Optional[dict]:
    """Release a card's LIVE designation inside the CALLER's open txn; ``None`` if it holds none.

    THE ONE RELEASE. The clamp that keeps the armed storage guard from holding the exit shut,
    the ``reprioritized`` event and the retained designation reason/authority are decided HERE
    and nowhere else. The caller owns the transaction and any post-commit work (the door's gate
    lift); nothing here opens one.
    """
    row = priority_designation(conn, task_id)
    if row is None or row["revoked_at"]:
        return None
    policy = _policy_module()
    stored = int(row["priority"])
    # The floor is read FIRST so the clamp admits a floor ON the tranche boundary: releasing a
    # designation whose stored value is exactly the board's declared floor returns the card to
    # that floor rather than clamping it down into the ordinary domain.
    floor, floor_error = None, None
    try:
        floor = _board_priority_floor(row["board"])
    except Exception as exc:
        floor_error = " ".join(str(exc).split()) or exc.__class__.__name__
    restore, _ = policy.clamp_to_domain(stored, None, floor)
    if floor is not None and restore < floor:
        restore = int(floor)
    correction = None
    if restore != stored:
        correction = {
            "stored": stored,
            "applied": restore,
            "floor": floor,
            "reason": (policy.domain_reason(stored) if not policy.in_ordinary(stored)
                       else f"below the board's floor {floor}"),
        }
    reason = (reason or "").strip()
    stamp = _designation_stamp(now)
    conn.execute("UPDATE priority_designations SET revoked_at = ? WHERE task_id = ?",
                 (stamp, task_id))
    conn.execute("UPDATE tasks SET priority = ? WHERE id = ?", (restore, task_id))
    payload = {"priority": restore, "designation": "revoked", "reason": reason}
    if cause != "revoke":
        # Name the seam on the event: the register's audit reads causes, and an archive-driven
        # release must be distinguishable from the operator door in the card's own history.
        payload["cause"] = cause
    if correction is not None:
        payload["correction"] = correction
    if floor_error is not None:
        payload["floor_unavailable"] = floor_error
    _append_event(conn, task_id, "reprioritized", payload)
    return {
        "task_id": task_id, "board": row["board"], "restored_priority": restore,
        "stored_priority": stored, "correction": correction,
        "authority": row["authority"], "reason": row["reason"],
        "designated_at": row["designated_at"], "revoked_at": stamp, "revoke_reason": reason,
    }


def revoke_priority_designation(conn: sqlite3.Connection, task_id: str, *, reason: str = "",
                                now: object = None) -> Optional[dict]:
    """Leave the tranche: annotate the row, restore the card's own ordinary priority.

    ``None`` when the card carries no LIVE designation - revoking is a no-op on a card that was
    never designated, never a silent re-rank of an ordinary card. The row keeps the DESIGNATION's
    reason and authority (that is what the audit query reads); the revoke's own reason rides the
    ``reprioritized`` event and the returned row, so nothing about the act is lost.

    THE RESTORED VALUE IS CLAMPED (door 5). The ledger records the priority the card held when it
    was designated - whatever that was, including a value that is not legal on the board by the
    time someone revokes. The live rows include hand-written RESERVED-TRANCHE values, written by
    raw SQL before any guard existed; writing one back would be refused by the armed storage
    guard inside the very transaction that sets ``revoked_at``, so the designation could never be
    released at all - the guard would hold the exit shut. The restore is therefore clamped to
    what the board allows AFTER the revocation: inside the ordinary domain (so never a tranche
    value), and never below a floored board's floor. When the clamp moves the value the correction
    rides the ``reprioritized`` event AND the returned row, so a ledger that no longer means what
    it says is visible rather than silent.

    A floor that cannot be READ does not stop the revoke: the domain clamp above is unconditional,
    so nothing here can write a tranche value back, and the unreadable floor is recorded on the
    event rather than swallowed.

    THE GATE HALF runs after the release, in its own txn: the ledger's value is where the card
    RESTS, not what it may HOLD, so a gate that still holds designated work is re-lifted by the
    relation - a revoke never leaves the board out of order, and never silently either.
    """
    _assert_not_delegated_child_mutation(kanban_db_path())
    row = priority_designation(conn, task_id)
    if row is None or row["revoked_at"]:
        return None
    with write_txn(conn):
        record = _release_designation_rows(conn, task_id, reason=reason, now=now)
    if record is None:  # lost a race with another releaser between the look and the txn
        return None
    gate_lifts = lift_gate_chain(conn, task_id, cause="revoke", board=record["board"])
    if gate_lifts:
        record["gate_lifts"] = gate_lifts
    return record


def _append_event(
    conn: sqlite3.Connection, task_id: str, kind: str, payload: Optional[dict] = None, *,
    run_id: Optional[int] = None,
) -> None:
    """Insert an event row inside the caller's txn; ``run_id`` groups it by attempt (NULL = task-scoped)."""
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?)", (task_id, run_id, kind, _json_or_null(payload), int(time.time())),
    )



# Terminal run outcomes that are NOT failures: the attempt ended deliberately —
# a success, a handoff, or a park the card's own workflow chose. A failure
# streak and its corrective text describe a FAILED ATTEMPT, never the card, so
# one of these must retire both when it closes a run. Otherwise a stale
# rate-limit / auth wall left by an earlier attempt keeps
# ``kanban_db_dispatch.check_respawn_guard`` from ever spawning the recovered
# card — the chicken-and-egg where the spawn that would clear the wall is the
# spawn the wall blocks.
#
# A POSITIVE allow-list on purpose: a newly added FAILURE outcome can never
# clear the streak by accident (fail closed). ``reclaimed`` is deliberately
# absent — a reclaimed/orphaned run is not a success, and ``complete_task``
# reconciles that case itself.
_NON_FAILURE_RUN_OUTCOMES = frozenset({
    "completed",
    "review_requested",
    "changes_requested",
    "blocked",
    "dependency_wait",
    "scheduled",
})


def _clear_failure_streak(conn: sqlite3.Connection, task_id: str) -> None:
    """Retire ``consecutive_failures`` / ``last_failure_error`` for a non-failure outcome.

    A bare UPDATE that must run inside the caller's transaction — unlike
    ``kanban_db_dispatch._clear_failure_counter``, which opens its own and so
    cannot be called from here. The failed attempt's diagnosis survives on its
    ``task_runs.error`` and its ``gave_up`` / ``timed_out`` / ... event; only
    the task-row copy the respawn guard reads is retired.
    """
    conn.execute(
        "UPDATE tasks SET consecutive_failures = 0, last_failure_error = NULL WHERE id = ?",
        (task_id,),
    )


def _end_run(
    conn: sqlite3.Connection, task_id: str, *, outcome: str, summary: Optional[str] = None,
    error: Optional[str] = None, metadata: Optional[dict] = None, status: Optional[str] = None,
    keep_attached: bool = False,
) -> Optional[int]:
    """Close the active run (``status`` defaults to ``outcome``) and clear
    ``current_run_id``; None when no run was active (never-claimed task).

    ``keep_attached=True`` closes the run but leaves it as the card's owner.
    A review handoff is the case: the run ENDS (it handed off), yet the worker
    that filed it must keep the licence to return, re-block or re-request the
    card. Clearing ``current_run_id`` there hands the implementer a card it can
    no longer act on — the wedge the named-reviewer refusal exists to prevent,
    reached through the back door.

    ``worker_pid`` / ``worker_started_at`` / ``claim_lock`` stay on the closed
    row: they are the only evidence left of the OS process once the task row
    is wiped, and :func:`kanban_db_dispatch.reap_terminal_workers` needs them
    to end a worker that survived its own terminal transition."""
    now = int(time.time())
    run_id = _current_run_id(conn, task_id)
    if run_id is None:
        return None
    conn.execute(
        """
        UPDATE task_runs
           SET status        = ?,
               outcome       = ?,
               summary       = ?,
               error         = ?,
               metadata      = ?,
               ended_at      = ?,
               claim_expires = NULL
         WHERE id = ?
           AND ended_at IS NULL
        """,
        (status or outcome, outcome, summary, error, _json_or_null(metadata), now, run_id),
    )
    if not keep_attached:
        conn.execute("UPDATE tasks SET current_run_id = NULL WHERE id = ?", (task_id,))
    if outcome in _NON_FAILURE_RUN_OUTCOMES:
        _clear_failure_streak(conn, task_id)
    return run_id


def _first_line(text: Optional[str], limit: int) -> str:
    """First non-blank-stripped line of ``text`` capped at ``limit`` chars; "" when empty."""
    lines = (text or "").strip().splitlines()
    return lines[0][:limit] if lines else ""


def _opt_int(value: Any) -> Optional[int]:
    """``int(value)`` or ``None`` when ``value`` is ``None`` (NULL column passthrough)."""
    return int(value) if value is not None else None


def _json_or_null(obj: Any) -> Optional[str]:
    """JSON text for a payload/metadata column; falsy -> NULL."""
    return json.dumps(obj, ensure_ascii=False) if obj else None


def _task_status(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Current ``tasks.status`` for ``task_id``, or ``None`` when no such row."""
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return row["status"] if row else None


def _current_run_id(conn: sqlite3.Connection, task_id: str) -> Optional[int]:
    row = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return int(row["current_run_id"]) if row and row["current_run_id"] else None


# Distinguishes "caller named the acting profile" (which may legitimately be
# None for an unassigned card) from "read the card's current assignee".
_UNSET: Any = object()


def _end_or_synthesize_run(
    conn: sqlite3.Connection, task_id: str, *, outcome: str, status: str,
    summary: Optional[str] = None, metadata: Optional[dict] = None, synthesize: bool,
    profile: Any = _UNSET, keep_attached: bool = False,
) -> Optional[int]:
    """:func:`_end_run`; when no run was active and ``synthesize`` holds, record a
    zero-duration run instead so the handoff fields survive in attempt history.
    ``profile`` overrides the profile read off the task row for the synthesized
    run — transitions that reassign the task (e.g. review handoff) pass the
    acting profile captured before the rewrite. ``keep_attached`` is
    :func:`_end_run`'s: the closed run stays the card's owner (review handoff)."""
    run_id = _end_run(conn, task_id, outcome=outcome, status=status, summary=summary,
                      metadata=metadata, keep_attached=keep_attached)
    if run_id is None and synthesize:
        run_id = _synthesize_ended_run(conn, task_id, outcome=outcome, summary=summary, metadata=metadata, profile=profile)
        # ``_end_run`` was a no-op (never-claimed task), so its failure-streak
        # clear never ran; do it on the synthesized path too. A reclaimed run
        # can leave the streak set on a card that is NOT claimed when a human
        # requests review / blocks it, and the guard would then wedge the card.
        if outcome in _NON_FAILURE_RUN_OUTCOMES:
            _clear_failure_streak(conn, task_id)
    return run_id


def _synthesize_ended_run(
    conn: sqlite3.Connection, task_id: str, *, outcome: str, summary: Optional[str] = None,
    error: Optional[str] = None, metadata: Optional[dict] = None,
    profile: Any = _UNSET,
) -> int:
    """Zero-duration closed run for a terminal transition on a never-claimed
    task, so the handoff fields aren't silently dropped (``_end_run`` is a
    no-op then). ``started_at == ended_at`` keeps elapsed stats honest. Does
    NOT touch the tasks row.

    ``profile`` overrides the profile read off the task row: transitions that
    reassign the task (e.g. review handoff) pass the acting profile captured
    before the rewrite, so the run names the actor, not the new assignee."""
    now = int(time.time())
    trow = conn.execute(
        "SELECT assignee, current_step_key FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if profile is _UNSET:
        profile = trow["assignee"] if trow else None
    step_key = trow["current_step_key"] if trow else None
    cur = conn.execute(
        """
        INSERT INTO task_runs (
            task_id, profile, step_key,
            status, outcome,
            summary, error, metadata,
            started_at, ended_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id, profile, step_key, outcome, outcome, summary, error, _json_or_null(metadata),
            now, now,
        ),
    )
    return int(cur.lastrowid or 0)


# --- Dependency resolution (todo -> ready) ---

def _has_sticky_block(conn: sqlite3.Connection, task_id: str) -> bool:
    """True when the newest ``blocked``/``unblocked``/``gave_up`` event says the
    block must wait for an operator: an explicit ``kanban_block`` (#28712), or a
    breaker trip ``_record_task_failure`` stamped ``sticky`` — the clean-exit
    protocol-violation budget or a systemic same-error wave. Those trip on a
    policy independent of ``consecutive_failures``, so ``recompute_ready``'s
    counter check cannot see them — without this the trip is promoted back to
    ``ready`` in the same tick and the card respawns forever. A plain
    (unified-budget) ``gave_up`` carries no marker and is judged by the counter,
    so raising ``failure_limit`` or ``assign_task`` to a fresh profile still
    releases it; a task with no such event at all (direct DB edit) auto-recovers.
    """
    row = conn.execute(
        "SELECT kind FROM task_events "
        "WHERE task_id = ? AND kind IN ('blocked', 'unblocked') "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if row and row["kind"] == "blocked":
        return True
    trip = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = 'gave_up' AND id > COALESCE("
        "  (SELECT MAX(id) FROM task_events WHERE task_id = ? AND kind = 'unblocked'), 0) "
        "ORDER BY id DESC LIMIT 1", (task_id, task_id),
    ).fetchone()
    return bool(trip) and bool(_json_dict(trip["payload"]).get("sticky"))


def _latest_event(
    conn: sqlite3.Connection, task_id: str, kind: str, run_id: Optional[int] = None,
) -> Optional[sqlite3.Row]:
    """Newest ``task_events`` row of ``kind`` (optionally scoped to one run)."""
    sql = "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?"
    params: tuple[Any, ...] = (task_id, kind)
    if run_id is not None:
        sql += " AND run_id = ?"
        params = (*params, int(run_id))
    return conn.execute(sql + " ORDER BY id DESC LIMIT 1", params).fetchone()


def _resume_status_from_events(conn: sqlite3.Connection, task_id: str) -> str:
    """``review`` when the newest lifecycle event carries a review
    ``resume_status``/``retry_status``/``source_status``, else ``ready`` (legacy)."""
    row = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind IN ("
        "'blocked', 'block_loop_detected', 'dependency_wait', 'gave_up', "
        "'unblocked', 'changes_requested', 'review_reopened', 'status', 'reclaimed', "
        "'stale', 'timed_out', 'crashed', 'spawn_failed', 'rate_limited'"
        ") ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    payload = _json_dict(_row_get(row, "payload"))
    for key in ("resume_status", "retry_status", "source_status"):
        if payload.get(key) == "review":
            return "review"
    return "ready"


def recompute_ready(conn: sqlite3.Connection, failure_limit: int | None = None) -> int:
    """Promote ``todo``/``blocked`` tasks whose parents are all done/archived;
    returns the count. Opens its own IMMEDIATE txn — call OUTSIDE any write txn.

    ``blocked`` is skipped when sticky (explicit ``kanban_block``) or when
    ``consecutive_failures`` reached the limit (else the breaker could never
    trip). Limit order matches ``_record_task_failure``: ``max_retries`` >
    ``failure_limit`` > ``DEFAULT_FAILURE_LIMIT``.

    1. The most recent block event was a worker-initiated ``kanban_block`` — those stay blocked until an
    explicit ``kanban_unblock`` (#28712).
    """
    if failure_limit is None:
        failure_limit = DEFAULT_FAILURE_LIMIT
    promoted = 0
    with write_txn(conn):
        todo_rows = conn.execute(
            "SELECT id, status, consecutive_failures, max_retries "
            "FROM tasks WHERE status IN ('todo', 'blocked')"
        ).fetchall()
        for row in todo_rows:
            task_id = row["id"]
            cur_status = row["status"]
            if cur_status == "blocked" and _has_sticky_block(conn, task_id):
                # Explicit human-intervention block; only ``unblock_task`` may exit it.
                continue
            parents = conn.execute(
                "SELECT t.status FROM tasks t "
                "JOIN task_links l ON l.parent_id = t.id "
                "WHERE l.child_id = ?", (task_id,),
            ).fetchall()
            if all(p["status"] in ("done", "archived") for p in parents):
                resume_status = _resume_status_from_events(conn, task_id)
                if cur_status == "blocked":
                    # At the breaker limit, no auto-recovery (else block ->
                    # recover -> respawn -> exhaust -> block forever). The
                    # counter is preserved so it accumulates across cycles.
                    failures = int(row["consecutive_failures"] or 0)
                    task_limit = row["max_retries"]
                    effective_limit = (
                        int(task_limit) if task_limit is not None
                        else int(failure_limit)
                    )
                    if failures >= effective_limit:
                        continue
                    conn.execute(
                        "UPDATE tasks SET status = ? "
                        "WHERE id = ? AND status = 'blocked'", (resume_status, task_id),
                    )
                else:
                    conn.execute(
                        "UPDATE tasks SET status = ? WHERE id = ? AND status = 'todo'",
                        (resume_status, task_id),
                    )
                _append_event(
                    conn, task_id, "promoted",
                    {"status": resume_status} if resume_status != "ready" else None,
                )
                promoted += 1
    return promoted


# --- Claim / complete / block ---

def _parents_satisfied(conn: sqlite3.Connection, task_id: str) -> bool:
    """Return whether every direct parent is terminal for dependency gating."""
    return conn.execute(
        "SELECT 1 FROM task_links l "
        "JOIN tasks p ON p.id = l.parent_id "
        "WHERE l.child_id = ? "
        "AND p.status NOT IN ('done', 'archived') LIMIT 1", (task_id,),
    ).fetchone() is None


def unsatisfied_parents(conn: sqlite3.Connection, task_id: str) -> list[tuple[str, str]]:
    """``(parent_id, status)`` for every direct parent :func:`_parents_satisfied`
    still counts as open (``done`` / ``archived`` release the child), in id
    order, so a refusal or a board view can name the blockers instead of the
    caller guessing. Read-only."""
    rows = conn.execute(
        "SELECT p.id, p.status FROM task_links l "
        "JOIN tasks p ON p.id = l.parent_id "
        "WHERE l.child_id = ? AND p.status NOT IN ('done', 'archived') "
        "ORDER BY p.id", (task_id,),
    ).fetchall()
    return [(row["id"], row["status"]) for row in rows]


def _claim_and_open_run(
    conn: sqlite3.Connection, task_id: str, source_status: str, lock: str, expires: int, now: int,
    *, event_extra: Optional[dict] = None,
) -> Optional[int]:
    """CAS ``source_status -> running``, open a run row, emit ``claimed``; None
    when the CAS lost. Caller holds the txn."""
    cur = conn.execute(
        f"""
        UPDATE tasks
           SET status        = 'running',
               claim_lock    = ?,
               claim_expires = ?,
               started_at    = COALESCE(started_at, ?)
         WHERE id = ?
           AND status = '{source_status}'
           AND claim_lock IS NULL
        """,
        (lock, expires, now, task_id),
    )
    if cur.rowcount != 1:
        return None
    trow = conn.execute(
        "SELECT assignee, max_runtime_seconds, current_step_key "
        "FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    run_cur = conn.execute(
        """
        INSERT INTO task_runs (
            task_id, profile, step_key, status,
            claim_lock, claim_expires, max_runtime_seconds,
            started_at
        ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?)
        """,
        (
            task_id, trow["assignee"] if trow else None, trow["current_step_key"] if trow else None,
            lock, expires, trow["max_runtime_seconds"] if trow else None, now,
        ),
    )
    run_id = run_cur.lastrowid
    conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (run_id, task_id))
    _append_event(
        conn, task_id, "claimed",
        {"lock": lock, "expires": expires, "run_id": run_id, **(event_extra or {})}, run_id=run_id,
    )
    return run_id


def claim_task(
    conn: sqlite3.Connection, task_id: str, *, ttl_seconds: Optional[int] = None,
    claimer: Optional[str] = None,
) -> Optional[Task]:
    """Atomically transition ``ready -> running``.

    Returns the claimed ``Task`` on success, ``None`` if the task was
    already claimed (or is not in ``ready`` status).
    """
    now = int(time.time())
    lock = claimer or _claimer_id()
    expires = now + _resolve_claim_ttl_seconds(ttl_seconds)
    with write_txn(conn):
        # Single enforcement point: never ready -> running with an undone
        # parent, whichever writer set 'ready'. Demote to 'todo';
        # recompute_ready re-promotes when the parents finish.
        if not _parents_satisfied(conn, task_id):
            conn.execute(
                "UPDATE tasks SET status = 'todo' "
                "WHERE id = ? AND status = 'ready'", (task_id,),
            )
            _append_event(conn, task_id, "claim_rejected", {"reason": "parents_not_done"})
            return None
        # Close a leaked prior run so the CAS below doesn't strand it.
        _reclaim_dangling_run(
            conn, task_id, statuses=("ready",), now=now, note="invariant recovery on re-claim",
        )
        run_id = _claim_and_open_run(conn, task_id, "ready", lock, expires, now)
        if run_id is None:
            return None
        claimed = get_task(conn, task_id)
    _fire_task_hook("kanban_task_claimed", claimed, task_id, run_id)
    return claimed


def claim_review_task(
    conn: sqlite3.Connection, task_id: str, *, ttl_seconds: Optional[int] = None,
    claimer: Optional[str] = None,
) -> Optional[Task]:
    """Atomic ``review -> running`` (None when lost). Parents are re-checked
    (one may have reopened meanwhile) and a NEW run tracks the reviewer
    separately from the implementer."""
    now = int(time.time())
    lock = claimer or _claimer_id()
    expires = now + _resolve_claim_ttl_seconds(ttl_seconds)
    with write_txn(conn):
        if not _parents_satisfied(conn, task_id):
            demoted = conn.execute(
                "UPDATE tasks SET status = 'todo' "
                "WHERE id = ? AND status = 'review' AND claim_lock IS NULL", (task_id,),
            )
            if demoted.rowcount == 1:
                _append_event(
                    conn, task_id, "dependency_wait",
                    {"reason": "parent_reopened", "source_status": "review"},
                )
            return None
        run_id = _claim_and_open_run(
            conn, task_id, "review", lock, expires, now, event_extra={"source_status": "review"},
        )
        if run_id is None:
            return None
        return get_task(conn, task_id)


def _retry_status_for_run(
    conn: sqlite3.Connection, task_id: str, run_id: Optional[int] = None,
) -> str:
    """``review`` when the run's ``claimed`` event says ``source_status=review``,
    else ``ready`` — one place, so crash/timeout/reclaim can't silently turn a
    reviewer run into an implementation run."""
    if run_id is None:
        run_id = _current_run_id(conn, task_id)
    if run_id is None:
        return "ready"
    event = _latest_event(conn, task_id, "claimed", run_id)
    payload = _json_dict(_row_get(event, "payload"))
    return "review" if payload.get("source_status") == "review" else "ready"


# Run outcome -> lifecycle status a goal loop should report for a handed-off run.
_RUN_OUTCOME_TERMINAL_STATUS = {
    "completed": "done",
    "review_requested": "review",
    "changes_requested": "changes_requested",
    "blocked": "blocked",
    "dependency_wait": "blocked",
}


def goal_run_status(
    conn: sqlite3.Connection, task_id: str, expected_run_id: Optional[int] = None,
) -> Optional[str]:
    """Lifecycle status as seen by ONE run: terminal handoffs bind to that run,
    any other ownership loss is ``superseded`` — otherwise an old goal loop
    would read the successor's live ``running`` and mutate it."""
    task = get_task(conn, task_id)
    if task is None:
        return None
    if expected_run_id is not None:
        row = conn.execute(
            "SELECT outcome FROM task_runs WHERE id = ? AND task_id = ?",
            (int(expected_run_id), task_id),
        ).fetchone()
        outcome = str(row["outcome"]) if row and row["outcome"] is not None else None
        terminal_status = _RUN_OUTCOME_TERMINAL_STATUS.get(outcome)
        if terminal_status is not None:
            return terminal_status
        if outcome is not None or task.current_run_id != int(expected_run_id):
            return "superseded"
    if task.status in {"ready", "todo"}:
        event = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1", (task_id,),
        ).fetchone()
        if event and event["kind"] == "changes_requested":
            return "changes_requested"
    return task.status


def heartbeat_claim(
    conn: sqlite3.Connection, task_id: str, *, ttl_seconds: Optional[int] = None,
    claimer: Optional[str] = None,
) -> bool:
    """Extend a running claim; True if we still own it."""
    expires = int(time.time()) + _resolve_claim_ttl_seconds(ttl_seconds)
    lock = claimer or _claimer_id()
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET claim_expires = ? "
            "WHERE id = ? AND status = 'running' AND claim_lock = ?", (expires, task_id, lock),
        )
        if cur.rowcount != 1:
            return False
        _extend_run_claim(conn, task_id, expires)
        return True


def _extend_run_claim(conn: sqlite3.Connection, task_id: str, expires: int) -> Optional[int]:
    """Mirror a task claim extension onto its active run row; returns that run id."""
    run_id = _current_run_id(conn, task_id)
    if run_id is not None:
        conn.execute("UPDATE task_runs SET claim_expires = ? WHERE id = ?", (expires, run_id))
    return run_id


def release_stale_claims(
    conn: sqlite3.Connection, *, signal_fn=None, failure_limit: Optional[int] = None,
) -> int:
    """Reclaim ``running`` tasks whose claim expired; returns the count reclaimed.

    Every reclaim that actually releases a claim is a non-success attempt and
    is booked through ``_record_task_failure`` (#111306): a claim that expired
    without a worker ever spawning otherwise loops claim -> reclaim -> claim
    with ``consecutive_failures`` stuck at 0, so the breaker never trips.
    ``reclaim_task`` (operator path) deliberately resets the counter instead.

    A host-local worker that is still alive gets its claim *extended* instead
    (a slow model can sit longer than the TTL inside one tool-free call, so no
    heartbeat) — unless ``last_heartbeat_at`` is older than
    ``DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS`` (wedged; ``_touch_activity``
    keeps any genuinely active worker fresh). Safe to call often.

    Reclaiming a live worker mid-flight produces the spawn- then-immediately-reclaim loop seen on slow
    models that spend longer than ``DEFAULT_CLAIM_TTL_SECONDS`` inside a single tool-free LLM call (#23025):
    no tool calls means no ``kanban_heartbeat``, even though the subprocess is healthy.
    Backstop (#29747 gap 3): if the worker's PID is still alive but its ``last_heartbeat_at`` is stale by
    more than ``DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS`` (1h), the worker has been making no observable
    progress and we reclaim anyway — even if ``_pid_alive`` is still true. This catches the
    wedged-in-a-logic-loop case where the process is technically running but accomplishing nothing.
    ``_touch_activity`` (run_agent.py) bridges chunk-level liveness into ``last_heartbeat_at`` via #31752,
    so any genuinely active worker keeps its heartbeat fresh as a side effect of normal API traffic.
    ``enforce_max_runtime`` and ``detect_crashed_workers`` remain the upper bounds for genuinely wedged or
    dead workers.
    """
    now = int(time.time())
    reclaimed = 0
    host_prefix = _host_prefix()
    stale = conn.execute(
        "SELECT id, claim_lock, worker_pid, worker_started_at, claim_expires, last_heartbeat_at, "
        "       assignee "
        "FROM tasks "
        "WHERE status = 'running' AND claim_expires IS NOT NULL "
        "  AND claim_expires < ?", (now,),
    ).fetchall()
    for row in stale:
        host_local = (row["claim_lock"] or "").startswith(host_prefix)
        hb = row["last_heartbeat_at"]
        # Backstop: a heartbeat older than the max-stale threshold means no
        # observable progress — reclaim even if the PID is alive (logic loop).
        heartbeat_stale = hb is not None and (now - int(hb)) > DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS
        started_at = _row_get(row, "worker_started_at")
        # ``not_dead``, not ``alive``: an unprovable worker must HOLD its claim too. Releasing it
        # beside a process that may still be running is how a duplicate is spawned next to it
        # (#123811); the worker's own fence still lets it close the card.
        if (host_local and row["worker_pid"] and _worker_not_dead(row["worker_pid"], started_at)
                and not heartbeat_stale):
            _extend_live_stale_claim(conn, row, now)
            continue

        termination = _terminate_reclaimed_worker(
            row["worker_pid"], row["claim_lock"], signal_fn=signal_fn, started_at=started_at,
        )
        # A live worker of ours must keep its claim (else a duplicate spawns beside it).
        if _worker_survived_termination(termination):
            _defer_reclaim_for_live_worker(
                conn, row["id"], row["claim_lock"], now, termination,
                reason="ttl_expired_worker_alive",
            )
            continue
        with write_txn(conn):
            retry_status = _retry_status_for_run(conn, row["id"])
            cur = conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL "
                "WHERE id = ? AND status = 'running' AND claim_lock IS ? "
                "AND claim_expires IS NOT NULL AND claim_expires < ? "
                # A worker that registered its own pid since the SELECT keeps its claim.
                "AND worker_pid IS ?",
                (retry_status, row["id"], row["claim_lock"], now, row["worker_pid"]),
            )
            if cur.rowcount != 1:
                continue
            run_id = _record_reclaim(
                conn, row["id"], termination,
                error=f"stale_lock={row['claim_lock']}",
                payload={
                    "stale_lock": row["claim_lock"],
                    "worker_pid": _opt_int(row["worker_pid"]),
                    "claim_expires": int(row["claim_expires"]),
                    "last_heartbeat_at": _opt_int(row["last_heartbeat_at"]),
                    "now": now,
                    "host_local": host_local,
                    "heartbeat_stale": bool(heartbeat_stale),
                    "retry_status": retry_status,
                },
            )
            reclaimed += 1
        # Own txn, after the reclaim commit (same shape as ``enforce_max_runtime``):
        # the run ended without a verdict, so it counts toward the breaker and a
        # trip flips the task to ``blocked`` + ``gave_up`` on top of ``reclaimed``.
        _record_task_failure(
            conn, row["id"], f"stale_lock={row['claim_lock']}",
            outcome="reclaimed", failure_limit=failure_limit,
            release_claim=False, end_run=False,
            event_payload_extra={"worker_pid": _opt_int(row["worker_pid"]), "retry_status": retry_status},
        )
        # Post-commit observer; every non-reclaim branch ``continue``d above.
        if _kanban_observer_consumed("on_kanban_worker_stale_claim"):
            _fire_kanban_lifecycle_hook(
                "on_kanban_worker_stale_claim", row["id"], board=get_current_board(),
                assignee=row["assignee"], run_id=run_id, worker_pid=_opt_int(row["worker_pid"]),
                heartbeat_stale=bool(heartbeat_stale), retry_status=retry_status,
            )
    return reclaimed


def _record_reclaim(
    conn: sqlite3.Connection, task_id: str, termination: dict, *, error: str, payload: dict,
) -> Optional[int]:
    """Close the active run as ``reclaimed`` and emit the ``reclaimed`` event
    (payload merged with the termination report). Caller holds the txn."""
    run_id = _end_run(
        conn, task_id, outcome="reclaimed", status="reclaimed", error=error, metadata=termination,
    )
    payload.update(termination)
    _append_event(conn, task_id, "reclaimed", payload, run_id=run_id)
    return run_id


def _extend_live_stale_claim(conn: sqlite3.Connection, row: sqlite3.Row, now: int) -> None:
    """TTL-expired claim whose host-local worker is alive: extend instead of
    reclaiming (``claim_extended`` event). CAS on the same expired lock so a
    concurrent reclaimer wins cleanly."""
    new_expires = now + _resolve_claim_ttl_seconds()
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET claim_expires = ? "
            "WHERE id = ? AND status = 'running' "
            "  AND claim_lock IS ? "
            "  AND claim_expires IS NOT NULL "
            "  AND claim_expires < ?", (new_expires, row["id"], row["claim_lock"], now),
        )
        if cur.rowcount != 1:
            return
        run_id = _extend_run_claim(conn, row["id"], new_expires)
        _append_event(
            conn, row["id"], "claim_extended",
            {
                "reason": "pid_alive",
                "worker_pid": int(row["worker_pid"]),
                "claim_lock": row["claim_lock"],
                "claim_expires_was": int(row["claim_expires"]),
                "claim_expires_now": new_expires,
                "last_heartbeat_at": _opt_int(row["last_heartbeat_at"]),
            },
            run_id=run_id,
        )


def reclaim_task(
    conn: sqlite3.Connection, task_id: str, *, reason: Optional[str] = None, signal_fn=None,
) -> bool:
    """Operator reclaim regardless of TTL: release the claim, restore the source
    phase, reset the failure counter. False when not running."""
    row = conn.execute(
        "SELECT status, claim_lock, worker_pid, worker_started_at FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if not row:
        return False
    if row["status"] != "running" and row["claim_lock"] is None:
        # Nothing to reclaim — already ready / blocked / done.
        return False
    prev_lock = row["claim_lock"]
    termination = _terminate_reclaimed_worker(
        row["worker_pid"], prev_lock, signal_fn=signal_fn, started_at=row["worker_started_at"])
    with write_txn(conn):
        retry_status = _retry_status_for_run(conn, task_id)
        cur = conn.execute(
            "UPDATE tasks SET status = ?, claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL "
            "WHERE id = ? AND status IN ('running', 'ready', 'blocked') "
            "AND claim_lock IS ?", (retry_status, task_id, prev_lock),
        )
        if cur.rowcount != 1:
            return False
        _record_reclaim(
            conn, task_id, termination,
            error=f"manual_reclaim: {reason}" if reason else f"manual_reclaim lock={prev_lock}",
            payload={"manual": True, "reason": reason, "prev_lock": prev_lock, "retry_status": retry_status},
        )
    # Operator intervention = fresh retry budget (own txn, runs after commit).
    _clear_failure_counter(conn, task_id)
    return True


def reassign_task(
    conn: sqlite3.Connection, task_id: str, profile: Optional[str], *, reclaim_first: bool = False,
    reason: Optional[str] = None,
) -> bool:
    """Reassign (None unassigns); a running task is refused unless
    ``reclaim_first`` releases its claim — the "this profile's model is broken" path."""
    if reclaim_first:
        # Safe to call even if nothing to reclaim.
        reclaim_task(conn, task_id, reason=reason or "reassign")
    # assign_task handles its own txn + the still-running guard.
    try:
        return assign_task(conn, task_id, profile)
    except RuntimeError:
        # Task is still running and reclaim_first was False; caller
        # needs to decide whether to retry with reclaim.
        return False


def _verify_created_cards(
    conn: sqlite3.Connection, completing_task_id: str, claimed_ids: Iterable[str],
) -> tuple[list[str], list[str]]:
    """Partition ``claimed_ids`` into (verified, phantom). Verified = the row
    exists AND ``created_by`` is the completing task's assignee or id, OR the
    card is linked as its child (created elsewhere, attached by the worker).
    Never mutates."""
    ordered = list(dict.fromkeys(str(x).strip() for x in (claimed_ids or []) if str(x).strip()))
    if not ordered:
        return [], []

    row = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (completing_task_id,)).fetchone()
    if row is None:
        # Completing task not found — nothing resolves.
        return [], ordered
    completing_assignee = row["assignee"]

    # Batch-fetch existence + created_by in one query.
    placeholders = ",".join(["?"] * len(ordered))
    rows = conn.execute(
        f"SELECT id, created_by FROM tasks WHERE id IN ({placeholders})", tuple(ordered),
    ).fetchall()
    found = {r["id"]: r["created_by"] for r in rows}

    # Pull the set of cards linked as children of the completing task.
    # Cheap: one query, indexed on parent_id.
    linked_children: set[str] = set(child_ids(conn, completing_task_id))

    verified: list[str] = []
    phantom: list[str] = []
    for cid in ordered:
        created_by = found.get(cid)
        trusted = created_by is not None and (
            (completing_assignee and created_by == completing_assignee)
            or created_by == completing_task_id
            or cid in linked_children
        )
        (verified if trusted else phantom).append(cid)
    return verified, phantom


# Matches ``kanban_create`` (12 hex) and ``_new_task_id`` (8 hex) ids; 8+ for forward compat.
_TASK_ID_PROSE_RE = re.compile(r"\bt_[a-f0-9]{8,}\b")


def _scan_prose_for_phantom_ids(conn: sqlite3.Connection, text: str) -> list[str]:
    """``t_<hex>`` references in ``text`` that don't resolve to a task (deduped; advisory)."""
    if not text:
        return []
    return _missing_task_ids(conn, dict.fromkeys(_TASK_ID_PROSE_RE.findall(text)))


class HallucinatedCardsError(ValueError):
    """``complete_task`` refused: ``created_cards`` has ids that don't exist or
    weren't created by this worker (``.phantom``). A ``ValueError`` so tool
    error handlers treat it as recoverable."""

    def __init__(self, phantom: list[str], completing_task_id: str):
        self.phantom = list(phantom)
        self.completing_task_id = completing_task_id
        super().__init__(
            f"completion blocked: claimed created_cards that do not exist "
            f"or were not created by this worker: {', '.join(phantom)}"
        )


class EmptyCompletionError(ValueError):
    """``complete_task`` refused: no substantive ``result``, ``summary``, or
    stored result. A ``ValueError`` so tool error handlers treat it as
    recoverable. Review approvals are exempt (the human is the record)."""

    def __init__(self, task_id: str):
        self.task_id = task_id
        super().__init__(
            f"completion blocked: {task_id} has no result or summary evidence"
        )


class ArtifactPreservationError(RuntimeError):
    """Raised when a declared scratch deliverable cannot be preserved."""


class LiveClaimError(ValueError):
    """``complete_task`` refused: the task is ``running`` under a claim that may still
    protect a live run, and the caller neither owns its run (``expected_run_id``)
    nor passed ``force``. Completing anyway would close the worker's run row
    underneath a process that is still executing. A ``ValueError`` so tool error
    handlers treat it as recoverable. ``verdict`` carries the tri-state liveness
    answer (``alive`` / ``unknown``) so a caller can render the right escape
    (see :func:`live_claim_refusal`)."""

    def __init__(self, task_id: str, *, verdict: str = "alive",
                 run_id: Optional[int] = None, detail: Optional[str] = None):
        # ``verdict`` defaults to the proven-live answer (``WORKER_ALIVE``), which is resolved at
        # call time in :func:`live_claim_refusal`; the literal keeps this default off the
        # dispatch import that lands at the bottom of the module.
        self.task_id = task_id
        self.verdict = verdict
        self.run_id = run_id
        super().__init__(detail or live_claim_refusal(task_id, verdict=verdict, run_id=run_id))


def live_claim_refusal(task_id: str, *, verdict: str, run_id: Optional[int] = None) -> str:
    """The ONE refusal text for a transition refused because the claim may protect a run.

    ``verdict`` is the tri-state liveness answer. ``alive`` is proof that a worker
    process owns the run, so both escapes are offered (own the run, or force).
    ``unknown`` is the cannot-certify case and must NOT offer ``force``: forcing
    there closes the very run the fence exists to protect, which is how a live
    worker's run was closed underneath it (#123811). The run-naming escape is
    offered in both cases — it is the one that is always safe.
    """
    if verdict == WORKER_UNKNOWN:
        owns = (f"Pass expected_run_id={int(run_id)} (your run id)" if run_id
                else "Pass expected_run_id (your run id)")
        return (
            f"{task_id} is running and the worker's process identity cannot be read on this host, "
            f"so the claim may be live. Nothing was written. {owns} "
            f"to close the run you hold; do NOT force this card — a live "
            f"worker's run would be closed underneath it."
        )
    return (
        f"{task_id} is running under a live worker claim; pass expected_run_id "
        "(worker ownership) or force=True (explicit operator override) instead "
        "of closing the live run"
    )


def live_row_refusal(conn: sqlite3.Connection, task_id: str, *, caller_run_id: Optional[int] = None,
                     verb: str = "complete") -> str:
    """Why a transition was refused, read from the LIVE row — never from ``last_failure_error``.

    ``tasks.last_failure_error`` is durable and describes a run that is OVER. Presenting it as the
    current reason is how a two-day-old crash string answered a live call and sent the operator
    chasing a stale run (#123811). The live row answers instead: what the card IS now, which run
    owns it, and whether the caller's own run is superseded by a newer attempt or was closed by the
    infrastructure — in which case the way back is named. Crash text is quoted only as history, and
    labelled as such.
    """
    task = get_task(conn, task_id)
    if task is None:
        return (f"could not {verb} {task_id}: no such card "
                "(unknown id, stale run, or already terminal)")
    parts = [f"could not {verb} {task_id}: the live row is status={getattr(task, 'status', '?')}"]
    owner = _opt_int(getattr(task, "current_run_id", None))
    if owner is not None:
        parts.append(f"run {owner} owns it")
        if caller_run_id is not None and int(caller_run_id) != owner:
            parts.append(
                f"your run {int(caller_run_id)} is SUPERSEDED by it — a newer attempt owns the "
                f"card, so closing it from here would land on that attempt"
            )
    else:
        parts.append("no run owns it")
        disowned = _newest_disowned_run(conn, task_id)
        if disowned is not None:
            parts.append(
                f"run {disowned} was closed by the INFRASTRUCTURE, not by its worker (a reclaim), "
                f"so this attempt is recoverable: pass expected_run_id={disowned} and the "
                f"transition is recorded as a recovery"
            )
    history = (getattr(task, "last_failure_error", None) or "").strip()
    if history:
        parts.append(f"for history only — NOT the current reason: {history!r}")
    return "; ".join(parts)


COMPLETION_REFUSAL_CAUSES = (
    "unknown_id",                  # no such task on this board
    "not_running",                 # status or run changed under the caller
    "parent_gate_unsatisfied",     # a parent is not terminal yet
    "acceptance_refusal",          # the PR acceptance receipt is not green
    "record_acceptance_refusal",   # the card moved while its receipt was collected
    "hallucinated_created_cards",  # created_cards names cards this worker did not create
    "deferral_not_a_child",        # deferred_children names a card that is not this card's child
    "deferral_needs_reason",       # a deferral must say WHY the child is not carrying this DoD
    # The landing-carrier clause (t_88f6cb12): a review close that DECLARES a deliverable PR
    # must name its landing leg. ``_gate_landing_carrier`` owns both causes; ``landing_state_unknown``
    # is the fail-closed channel when the forge cannot answer AND nothing is declared.
    "landing_carrier_missing",     # declared PR is unmerged with no carrier/exemption on any board
    "landing_state_unknown",       # the landing state of the declared PR cannot be judged
    # The dirty-unit-checkout fence (t_b151a247): the card worked inside a protected
    # deployment source (canonical unit checkout) that is dirty. ``done`` must not close
    # over bytes that exist only in a working tree -- gate.py check 1 refuses every
    # deploy sourced from that checkout, so the card would read done while prod stalls.
    "dirty_unit_checkout",         # the checkout the card worked in has uncommitted paths
    # The card was never in a status the completion door accepts (card t_012fb74e): ``triage``,
    # ``todo``/``scheduled`` are the seat-scoped door's remit, and the terminal pair belongs to
    # ``hermes kanban reopen``. Distinct from ``not_running``, which is a run that moved.
    "status_not_completable",      # the card never held a completable status
)


class CompletionRefusal:
    """Why :func:`complete_task` refused, as data instead of a bare ``False``.

    Falsy, so every existing ``if not complete_task(...)`` caller keeps its meaning, while
    the operator-facing surfaces can name the *cause* — a bool cannot tell a mistyped id
    from a completion contract no retry can satisfy, and that ambiguity is how a card sits
    un-completable with no signal. :class:`HallucinatedCardsError` stays an exception
    (existing callers catch it) and carries the same cause name as a class attribute.
    """

    __slots__ = ("cause", "detail")

    def __init__(self, cause: str, detail: str):
        if cause not in COMPLETION_REFUSAL_CAUSES:
            raise ValueError(f"unknown completion refusal cause: {cause!r}")
        self.cause = cause
        self.detail = detail

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return f"CompletionRefusal(cause={self.cause!r}, detail={self.detail!r})"


# The operator's RELEASE door, spelled the way every live surface spells it
# (``hermes_cli/kanban_proof_gate.py``, ``tools/kanban_tools.py``): a mis-authored
# ``completion_contract`` is re-scoped by an OWNER act, never by a worker and never by a direct
# DB write (ruling t_90767078, D3/D4). A refusal that names it must never name a dead end.
CONTRACT_RELEASE_VERB = "hermes kanban set-contract"

# Classifications that mean NO VERDICT EXISTS on the PR because the transport failed: the
# forge could not be READ. A verdict the forge actually returned (``pending``, ``failure``,
# ``stale``, a ``missing`` required check) is NOT here -- those keep refusing.
#
# ``auth`` is deliberately NOT here (flagged to platform-stl, card t_c4a9d940): the ruling
# named ``infra``, and ``auth`` is a DIFFERENT animal -- the assignee profile's login, which an
# operator changes in a minute, and whose refusal already names that fix. Its door is also
# pinned by its own tests (#122689: an assigned card's acceptance must never fall through to
# the ambient login), so widening the degrade to it is a design ruling, not this card's
# implementation detail.
_UNVERIFIED_CLASSIFICATIONS = frozenset({"infra"})


def _acceptance_unverified(receipt: Mapping[str, Any], contract: str) -> Optional[str]:
    """The WARN text when the forge could not be READ -- else ``None`` (card ``t_c4a9d940``).

    A close door that cannot accept evidence it can verify is as wrong as one that accepts
    evidence it cannot. ``infra`` establishes nothing about the PR: ``gh`` is absent or the
    network is down. Refusing on that forever turns a transient transport failure into a card
    whose work IS landed with no way to say so (measured 2026-10-19: one card refused five
    times and parked ``blocked``/``capability`` while its PR was merged).

    So such a completion is ACCEPTED with the receipt recorded ``degraded`` and a WARN row on
    the event log: recorded, unverified -- never reported green (the receipt keeps
    ``ok: False``). This is "could not check never collapses into a pass", applied in the
    direction that does not deadlock.
    """
    classification = receipt.get("classification")
    if classification in _UNVERIFIED_CLASSIFICATIONS:
        return (
            f"PR acceptance could not be read ({classification}) for contract {contract!r}: "
            f"this close is recorded as UNVERIFIED, not green. {receipt.get('detail', '')}"
        ).strip()
    if receipt.get("cause") == "contract_unreadable":
        # The load path repairs these (``repair_unreadable_contract``); reaching here means a
        # concurrent change slipped past it. Degrade rather than fail forever.
        return (
            f"The persisted completion contract {contract!r} is not a readable declaration, so "
            "no acceptance verdict exists: this close is recorded as UNVERIFIED, not green."
        )
    return None


def _contract_is_unsatisfiable(receipt: Mapping[str, Any]) -> bool:
    """True when NO retry can pass: a repository contract its repository cannot check.

    ``collect_acceptance`` NO LONGER produces this shape: a repository that declares no
    required checks resolves to ``pending`` (open) or ``success`` (merged) instead, because
    MERGE STATE is its acceptance evidence (ruling 2026-10-18, card ``t_9082c751``). The
    predicate is kept for receipts PERSISTED before that ruling, so a card parked on one can
    still be recognised and released by the PR-aware remedy in
    :func:`_unsatisfiable_contract_release`.

    The discriminator is the receipt's own data, never a guess: ``required`` is absent
    entirely on a receipt that never reached the repository (a declaration/PR mismatch),
    and non-empty when a required check simply has not reported yet.
    """
    return receipt.get("classification") == "missing" and receipt.get("required") == []


def _unsatisfiable_contract_release(
    task_id: str, contract: str, receipt: Optional[Mapping[str, Any]] = None,
) -> str:
    """The remedy a refusal about an unsatisfiable contract must NAME, in one sentence.

    A card that has ALREADY PUBLISHED is never told to use ``local-only`` (ruling
    2026-10-18, card ``t_9082c751``): that is the claim its own record refutes, and
    offering it was half of a mutual dead end -- the landing fence refused ``local-only``
    beside a publication while this door refused every PR contract. Such a card is
    offered the route that exists: the PR's MERGED state.
    """
    published = bool((receipt or {}).get("pr_url") or (receipt or {}).get("published_pr"))
    if published:
        return (
            f"The contract {contract!r} waits on repository checks this repository does not "
            f"declare, so there is nothing to wait for. A PR contract is satisfied by the PR's "
            f"MERGED state: merge the PR this card published, then retry completion -- the card "
            f"already refuted any claim that nothing was published, so the merge is the only "
            f"remaining step."
        )
    return (
        f"The contract {contract!r} requires repository checks, but the repository declares "
        f"none, so no retry can satisfy it. Release the declaration with "
        f"`{CONTRACT_RELEASE_VERB} {task_id} local-only --reason '<why no repository checks>'` "
        f"if the unit genuinely needs no repository checks, or set the exact PR URL it lands on "
        f"-- a repository that requires no checks accepts the PR's MERGED state."
    )


class ProofGateError(ValueError):
    """``complete_task`` refused: the card is authored ``landed`` and its handoff's proof
    does not cover the deployed artifact — a run dated before the landing, no run at all,
    or an artifact that is not the bytes the handoff declared. ``.clause`` names the clause
    (see ``hermes_cli/kanban_proof_gate.py``), the audit event
    ``completion_blocked_proof_gate`` carries the resolved facts, and the task itself is
    NOT mutated. A ``ValueError`` so tool error handlers treat it as recoverable."""

    cause = "proof_gate"

    def __init__(self, task_id: str, verdict):
        from hermes_cli.kanban_proof_gate import render

        self.task_id = task_id
        self.clause = getattr(verdict, "cause", None) or "proof_gate"
        self.cause = self.clause
        self.detail = getattr(verdict, "detail", "")
        self.facts = getattr(verdict, "facts", {})
        super().__init__(render(task_id, verdict))


def _gate_deferred_children(
    task_id: str, declared: Optional[Mapping[str, Any]],
) -> Any:
    """Validate a closing card's EXPLICIT deferral declarations: ``{child_id: why}``.

    A declaration that states no reason is an unexplained exemption, so it is refused with
    the same vocabulary as every other completion refusal (``deferral_needs_reason``). The
    shape is checked HERE as well as in the tool layer, so a direct kernel caller cannot
    land a bodyless exemption.

    Returns the cleaned ``{child_id: why}`` mapping, or a falsy :class:`CompletionRefusal`.

    NOTE (this tree): the kernel carries no closure-floor child promotion yet, so a
    declaration has nothing to hold a child OUT of — it is RECORDED (``priority_deferrals``
    on the ``completed`` event) so the declaration is never silently dropped, and it is
    honoured by construction. Child-ness is therefore not adjudicated here: the promotion
    that a deferral exempts a child from does not exist on this kernel yet.
    """
    if not declared:
        return {}
    if not isinstance(declared, Mapping):
        raise ValueError(
            "deferred_children must be a mapping of {child id: why the child does not carry "
            "this card's remaining DoD}, got " + type(declared).__name__
        )
    cleaned: dict[str, str] = {}
    for child, why in declared.items():
        if not isinstance(why, str) or not why.strip():
            return CompletionRefusal(
                "deferral_needs_reason",
                f"deferred_children[{str(child)!r}] states no reason: a deferral must say WHY "
                f"the child does not carry this card's remaining DoD",
            )
        cleaned[str(child)] = why.strip()
    return cleaned


# Reasons a run row's ``metadata['reason']`` can carry that mean the run was abandoned by an
# INFRASTRUCTURE event rather than finished by its worker: a reclaim (orphan reconciliation,
# stale-claim TTL, crash sweep) or the runtime limit. Written by the reclaim paths in
# ``kanban_db_dispatch``. A fence that deletes or rewrites history must find one of these before it
# may proceed — an attempt the worker itself recorded is never "falsified evidence".
DISOWNED_RUN_REASONS = frozenset({
    "orphaned_running",
    "stale_lock",
    "crashed_worker",
    "timed_out",
    "ttl_expired_worker_alive",
    "heartbeat_stale_worker_alive",
})


def _run_was_disowned(conn: sqlite3.Connection, task_id: str, run_id: Optional[int]) -> bool:
    """True when the recorded run itself says an infrastructure event abandoned it.

    Reads ``task_runs.metadata['reason']`` — written by every reclaim path — and accepts only
    :data:`DISOWNED_RUN_REASONS`. A run closed by the worker (or by anything else) is NOT
    disowned, and every recovery that would rewrite or drop its record must then do nothing.
    """
    if not run_id:
        return False
    row = conn.execute(
        "SELECT metadata FROM task_runs WHERE id = ? AND task_id = ?",
        (int(run_id), task_id),
    ).fetchone()
    if row is None:
        return False
    raw = _row_get(row, "metadata")
    if not raw:
        return False
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return False
    if not isinstance(parsed, dict):
        return False
    reason = parsed.get("reason")
    if not reason or reason not in DISOWNED_RUN_REASONS:
        return False
    # Terminal-only, and never the spawner's record: a ``spawn_failed`` run is the HOST's evidence
    # that it refused to start an attempt, and the respawn cooldown reads it. Overwriting that row
    # would erase a cooldown and let a failing card spin, so the premise excludes it by name.
    state = conn.execute(
        "SELECT outcome, status FROM task_runs WHERE id = ? AND task_id = ?",
        (int(run_id), task_id),
    ).fetchone()
    if state is None:
        return False
    if _row_get(state, "status") == "running" or _row_get(state, "outcome") == "spawn_failed":
        return False
    return True


def _newest_disowned_run(conn: sqlite3.Connection, task_id: str) -> Optional[int]:
    """Id of the card's NEWEST run when it is proven abandoned by infrastructure, else ``None``.

    "The card's last run" is the row that stands as its last word on the attempt. It is NOT
    ``tasks.current_run_id``: an infrastructure reclaim NULLs that column and leaves the abandoned
    row standing, which is exactly why one authority decides whether that row is disowned.
    """
    newest = conn.execute(
        "SELECT id FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    run_id = _opt_int(_row_get(newest, "id")) if newest is not None else None
    return run_id if _run_was_disowned(conn, task_id, run_id) else None


def _reconcile_owned_run(
    conn: sqlite3.Connection, task_id: str, *, disowned_run_id: Optional[int], summary: str = "",
) -> Optional[int]:
    """Record the empty ``completed`` run a recovery owes the attempt history.

    ``summary=""`` is deliberate and is the whole point: the recovery attributes NOTHING to the
    worker — no prose, no metadata about work, and ``started_at == ended_at`` (via
    :func:`_synthesize_ended_run`) so elapsed stats are not inflated either. The caller holds the
    transaction; the failure counter is cleared by :func:`_synthesize_empty_completion_run` outside it.
    """
    return _synthesize_ended_run(
        conn, task_id, outcome="completed", summary=summary,
        metadata={
            "recovery": "reclaim",
            "recovered_from_run_id": _opt_int(disowned_run_id),
            "infra_reclaimed": True,
        },
    )


def _synthesize_empty_completion_run(
    conn: sqlite3.Connection, task_id: str, *, disowned_run_id: Optional[int] = None,
) -> Optional[int]:
    """The empty completion run that stops an abandoned attempt standing as the card's last word.

    A reclaim closes the abandoned run as a FAILURE (``reclaimed`` / ``crashed`` / ``timed_out``).
    When the card then completes with no handoff fields of its own, that failed row is the attempt
    history's last word — "the card ran on a worker that crashed" beside a delivered artifact — and
    the reclaim also left ``consecutive_failures`` incremented against a worker that never failed.
    This records the recovery honestly (see :func:`_reconcile_owned_run`); the CALLER clears that
    falsified counter after its transaction commits (``complete_task`` already does on success —
    ``_clear_failure_counter`` opens its own transaction and must never run under an open one).

    GUARDED, in both directions. It refuses unless the run it is reconciling is PROVEN disowned
    (:func:`_run_was_disowned`, :data:`DISOWNED_RUN_REASONS`): history that a worker recorded is not
    ours to rewrite. It also refuses beside a live claim — a card whose worker is still running is
    mid-attempt, not recovered. Returns the synthesized run id, or ``None`` when it did nothing.
    """
    if disowned_run_id is None:
        # "The card's last run": the row that would stand as its last word on the attempt. It is
        # NOT ``tasks.current_run_id`` — a terminal run leaves that NULL, which is exactly why the
        # abandoned row survives a completion unless something reconciles it.
        disowned_run_id = _newest_disowned_run(conn, task_id)
        if disowned_run_id is None:
            return None
    if not _run_was_disowned(conn, task_id, disowned_run_id):
        return None
    trow = conn.execute(
        "SELECT status, claim_lock, worker_pid, worker_started_at, current_run_id FROM tasks "
        "WHERE id = ?", (task_id,),
    ).fetchone()
    if trow is not None and _claim_is_live(trow):
        return None
    run_id = _reconcile_owned_run(conn, task_id, disowned_run_id=disowned_run_id, summary="")
    _append_event(
        conn, task_id, "recovery_reconciled",
        {"disowned_run_id": _opt_int(disowned_run_id), "synthesized_run_id": _opt_int(run_id)},
        run_id=run_id,
    )
    return run_id


# --- the closure floor: a completing card's DoD-carrying children --------------------------
#
# The operator's prioritisation standard: a card that carries a higher-priority card's
# remaining DoD must never sit below it, because priority governs spawn ORDER - so a closure
# filed low starves the chain it gates (five live violations measured 2026-09-28).
#
# The enforcement node is the COMPLETION, deliberately and nowhere else:
#
# * completion is the only moment the carrying relationship is OBSERVABLE. At birth a child of
#   a high-priority card is indistinguishable from a follow-up filed against a card whose DoD
#   is already delivered, so lifting every child at birth would inflate ordinary work - which
#   the standard explicitly forbids. A child filed low BEFORE its parent closes is the failure
#   mode measured live, and the parent's closure is when the kernel can see that the child
#   carries the parent's remaining DoD;
# * completion is the moment the closing card can DECLARE a deferral. That declaration is
#   explicit and recorded (``deferred_children``), never inferred from a low priority.
#
# The lift rides the completion's OWN write txn (the caller holds it), so a promotion can
# never be lost by a crash between the closing write and the lift, and anything that refuses
# the lift rolls the completion back with it.

def _claim_liveness(trow) -> str:
    """Tri-state liveness of a ``running`` task's claim: ``alive`` / ``dead`` / ``unknown``.

    ``dead`` means nothing protects the run: the task is not running, holds no claim
    lock, recorded no worker process, or its worker process is proven gone/recycled.
    ``unknown`` means the claim may still protect a live worker whose identity cannot
    be certified (see ``kanban_db_dispatch._worker_liveness``). TTL expiry is
    deliberately not consulted: ``reclaim_stale_tasks`` extends, not reclaims, the claim
    of a live worker, so the process is the liveness authority here too.
    """
    if (trow is None or trow["status"] != "running" or trow["claim_lock"] is None
            or not _row_get(trow, "worker_pid")):
        return WORKER_DEAD
    return _worker_liveness(_row_get(trow, "worker_pid"), _row_get(trow, "worker_started_at"))
def _claim_is_live(trow) -> bool:
    """True when a ``running`` task's claim is PROVEN to protect a live worker process.

    This is the predicate that REFUSES a transition. An unprovable (``unknown``) claim is
    deliberately not enough to refuse — see :func:`_close_run_fence` — but it IS enough to
    keep the claim from being released (:func:`_worker_not_dead`, used by
    ``release_stale_claims`` / ``_reclaim_dead_workers``).
    """
    return _claim_liveness(trow) == WORKER_ALIVE


def _run_guard_sql(run_id: Optional[int]) -> tuple[str, tuple]:
    """The run-ownership CAS every run-closing UPDATE carries.

    ``run_id`` is the run the caller read inside this transaction — the one it named, or the
    card's active run when it named none. A card with NO active run is matched by
    ``current_run_id IS NULL``. The guard is what stops a transition landing on a run the caller
    never read; without it a stale actor's write silently closes its successor's run, which is how
    a run was requeued while its worker held it (#123811).

    A caller that NAMES a run is admitted on the SUPERSEDED/UN-OWNED split: while
    ``current_run_id`` is NULL **and its run is the card's newest**, the write lands. That is the
    half an infrastructure reclaim creates — it NULLs ``current_run_id`` and closes the run
    underneath a worker that is still executing, and exact equality alone fenced that worker, the
    only legitimate owner, out of its own card permanently. A SUPERSEDED caller (a newer run
    exists, so ``MAX(id)`` is not its run) is still refused by the same clause.
    """
    if run_id is None:
        return " AND current_run_id IS NULL", ()
    return (
        " AND (current_run_id = ? OR (current_run_id IS NULL AND ? = ("
        "SELECT MAX(id) FROM task_runs WHERE task_id = tasks.id)))",
        (int(run_id), int(run_id)),
    )


def _close_run_fence(
    trow, task_id: str, *, expected_run_id: Optional[int], force: bool,
) -> tuple[bool, str, str, tuple]:
    """The ONE fence for closing a run: ``(allowed, verdict, guard_sql, guard_params)``.

    ``verdict`` is the tri-state answer that decided it (``""`` when the caller named the
    run, so the liveness question was never asked). Callers render their own refusal text;
    an ``unknown`` verdict must render one that offers ``expected_run_id`` and NOT ``force``
    (:func:`live_claim_refusal`).

    Both directions live here so no path can check one and silently skip the other:

    * the caller NAMED a run — admitted, and the UPDATE is made conditional on it, so a
      mismatch is refused by the write instead of landing on a successor's run;
    * the caller named NONE — the claim's liveness decides. A claim PROVEN to protect a
      live worker is refused (something else owns the run). Anything else is admitted WITH
      the guard: refusing on an unprovable claim would turn away the worker that owns the
      card, and the guard already keeps the write off any run the caller did not read.
    """
    if trow is None:
        # Let the UPDATE decide (it cannot match a missing task); never invent a refusal.
        return True, "", *_run_guard_sql(expected_run_id)
    current_run_id = _row_get(trow, "current_run_id")
    if force:
        return True, "", *_run_guard_sql(
            int(expected_run_id) if expected_run_id is not None else current_run_id)
    if expected_run_id is not None:
        return True, "", *_run_guard_sql(int(expected_run_id))
    verdict = _claim_liveness(trow)
    if verdict == WORKER_ALIVE:
        return False, verdict, "", ()
    return True, "", *_run_guard_sql(current_run_id)


def _park_unsatisfiable_contract(
    conn: sqlite3.Connection, task_id: str, contract: str, *,
    receipt: Optional[Mapping[str, Any]] = None,
    expected_run_id: Optional[int] = None,
) -> bool:
    """Park a card whose repository contract NO retry can satisfy: ``blocked`` / ``capability``.

    ``capability``, not ``transient``: the fence cannot clear on its own, so the failure breaker
    must not respawn the worker against the same verdict. The reason names the contract AND the
    release door, because the card's own record is what a human reads next. The receipt decides
    which release door is offered: a card that has published is never sent back to ``local-only``
    (t_9082c751).
    """
    return bool(block_task(
        conn, task_id, kind="capability",
        reason=f"completion contract {contract!r} is unsatisfiable. "
               + _unsatisfiable_contract_release(task_id, contract, receipt),
        expected_run_id=expected_run_id,
    ))


def _not_completable_refusal(conn: sqlite3.Connection, task_id: str) -> CompletionRefusal:
    """The honest cause when a card was never in a status the completion door accepts.

    ``prepare_acceptance`` returns ``False`` for two different facts: the card is not in a
    completable status at all, or its run moved under the caller. Only the second is a race, so
    only the second is ``not_running``; the first is ``status_not_completable`` and names the
    status plus the act that owns it, because "retry" against a ``triage`` card can never
    succeed (card t_012fb74e).
    """
    # Late imports: this module is the facade the door and the matrix import, so neither can be
    # a module-level dependency here.
    from hermes_cli.kanban_exits import owning_door
    from hermes_cli.kanban_pr_acceptance_store import _COMPLETABLE

    status = _task_status(conn, task_id)
    if status is None:
        return CompletionRefusal("unknown_id", f"no task {task_id} on this board")
    if status in _COMPLETABLE or status in ("done", "archived"):
        # A completable status that moved, or a card that already reached the terminal pair:
        # both are "it moved while you were closing it", which is what ``not_running`` means
        # (D3, tests/hermes_cli/test_kanban_completion_contract_release.py).
        return CompletionRefusal(
            "not_running",
            f"{task_id} left a completable status while it was being closed (it is {status!r})",
        )
    door = owning_door(status)
    return CompletionRefusal(
        "status_not_completable",
        f"{task_id} is {status!r}, which the completion door does not accept (it accepts "
        f"{', '.join(sorted(_COMPLETABLE))}). The act that exits {status!r} is "
        f"{door or 'undocumented -- the status has no door'}",
    )


def complete_task(
    conn: sqlite3.Connection, task_id: str, *, result: Optional[str] = None,
    summary: Optional[str] = None, metadata: Optional[dict] = None,
    created_cards: Optional[Iterable[str]] = None, expected_run_id: Optional[int] = None,
    fire_lifecycle_hook: bool = True, force: bool = False,
    deferred_children: Optional[Mapping[str, Any]] = None,
) -> bool | CompletionRefusal:
    """``running|ready|blocked|review -> done``; records ``result``.

    ``ready`` is accepted for manual CLI completion, ``review`` for human
    approval. A ``running`` task under a live claim is only completed with
    proof of ownership (``expected_run_id``) or ``force=True`` (explicit
    operator override) — otherwise :class:`LiveClaimError`, the same fence
    :func:`request_review` applies. With no active run the handoff fields survive via
    :func:`_synthesize_ended_run`. ``summary`` (defaults to ``result``) and
    ``metadata`` land on the closing run for :func:`build_worker_context`.
    ``created_cards`` are verified first — a phantom id raises
    :class:`HallucinatedCardsError` after an auditable event; afterwards the
    prose is scanned for unresolvable ``t_<hex>`` refs (advisory event only).
    Completions from non-review statuses need evidence: a stripped ``result``
    or ``summary``, or a stripped result already stored on the card. Empty or
    whitespace-only evidence raises :class:`EmptyCompletionError` after an
    auditable event. Approving a card out of ``review`` stays exempt.
    """
    now = int(time.time())
    # WHY a completion was refused, as DATA, never a bare ``False``: a bool cannot tell a
    # mistyped id from a contract no retry can satisfy, and that ambiguity is how a card sits
    # un-completable with no signal (spec:
    # tests/hermes_cli/test_kanban_completion_contract_release.py; ruling t_90767078). Every
    # refusal below returns a falsy :class:`CompletionRefusal`, so every existing
    # ``if not complete_task(...)`` caller keeps its meaning.
    if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
        return CompletionRefusal("unknown_id", f"no task {task_id} on this board")
    # Cheap pre-check; re-checked inside the txn to close the parent-reopen race.
    if not _parents_satisfied(conn, task_id):
        return CompletionRefusal(
            "parent_gate_unsatisfied",
            f"{task_id} cannot complete: parent(s) "
            + ", ".join(f"{pid} ({status})" for pid, status in unsatisfied_parents(conn, task_id))
            + " are not terminal (done or archived)",
        )
    # A closing card's EXPLICIT deferral declarations. Validated here (not only in the tool
    # layer) so a direct kernel caller cannot land a bodyless exemption; recorded on the
    # ``completed`` event below. See _gate_deferred_children for this tree's promotion gap.
    deferrals = _gate_deferred_children(task_id, deferred_children)
    if isinstance(deferrals, CompletionRefusal):
        return deferrals
    from hermes_cli.kanban_pr_acceptance_store import prepare_acceptance, record_acceptance
    verified_cards = _gate_created_cards(conn, task_id, created_cards, summary or result)
    _gate_empty_completion(conn, task_id, result=result, summary=summary)
    # A card that worked in a dirty deployment source cannot close over bytes that
    # exist only in its working tree (t_b151a247). Refused before any acceptance work.
    checkout_refusal = _gate_unit_checkout_clean(conn, task_id, force=force)
    if isinstance(checkout_refusal, CompletionRefusal):
        return checkout_refusal
    metadata = _merge_completion_prose_artifacts(
        conn, task_id, metadata, summary=summary, result=result,
    )
    # The LANDED fence travels with the completion door (ruling t_90767078, D1/D2): a card
    # authored ``landed`` is judged by the deploy-proof gate BEFORE any acceptance clause,
    # and the PR-acceptance clause is inapplicable to it. Exempting ``landed`` from PR
    # acceptance WITHOUT this call is a silent free pass — strictly worse than today's
    # unclosable card — so the two halves ship as one unit.
    _gate_deploy_proof(conn, task_id, metadata)
    # INVARIANT A at the transition (design record platform-stl t_43be623a, ruling ESC-01650 /
    # platform/t_88cbc437 -> t_52183ba9): a completion must DECLARE the evidence behind it. The
    # seam sat here in the home of record and was dropped when this file became a tracked member
    # of the ``overrides/live`` generation, which left ``kanban_gate_invariants
    # .gate_completion_evidence`` with no call site at all: the door, its kill switch and its
    # event vocabulary were dead code and the invariant was asserted nowhere that runs.
    #
    # Position is load-bearing and unchanged from the home of record: AFTER ``_gate_deploy_proof``
    # (the ``landed`` contract owns its own ``metadata.proof`` block, and the evidence door returns
    # early for it rather than judging the same claim twice) and BEFORE ``prepare_acceptance`` (a
    # completion that declares no evidence is refused before an acceptance receipt is written).
    # ``force`` is the operator's override and is passed through so it records ``evidence_waived``
    # instead of refusing; a lane cannot reach that (``force`` is the operator path only).
    # ARMED IN ``measure`` per the ruling: the door measures and records, the refusal is switched
    # on per board when the soak shows it survivable. Flipping it here would be a fleet-wide close
    # behaviour change, which is the STL's call, not this seam's.
    from hermes_cli import kanban_gate_invariants as _gates
    _gates.gate_completion_evidence(conn, task_id, metadata, force=force)
    handoff_summary = summary if summary is not None else result
    acceptance = prepare_acceptance(conn, task_id, expected_run_id, metadata)
    if acceptance is False:
        # ``prepare_acceptance`` returns False when the card is not in a COMPLETABLE status --
        # i.e. it never held one, which is a different fact from a run that ended underneath
        # the caller. Reporting both as ``not_running`` ("left a completable status") was a
        # lie: a card in ``triage``/``scheduled`` never left anything, and the worker was told
        # to retry a race instead of being told which door owns the status (card t_012fb74e).
        return _not_completable_refusal(conn, task_id)
    if acceptance is not None and not acceptance[1].get("ok"):
        # The acceptance receipt is not green. TWO outcomes, decided by whether a verdict
        # EXISTS (R1, card t_c4a9d940):
        #  * the forge answered and the answer is not acceptance -> refuse. The DECLARATION is
        #    the fence, so nothing is traded away to make it pass: the refusal names its own
        #    remedy, and an UNSATISFIABLE contract parks the card so no retry respawns against
        #    the same verdict. The receipt is persisted first -- in its own txn, because
        #    ``block_task`` takes one too -- so a refusal always stays auditable on the event
        #    log. Nothing else is written: the card is never completed and its declaration
        #    never moves (ruling t_90767078, D4).
        #  * the forge could not be read (``infra``) -> degrade and fall through to the
        #    completion below, recorded UNVERIFIED. A card whose work is landed must not be
        #    unclosable because a subprocess failed.
        receipt = acceptance[1]
        contract = conn.execute(
            "SELECT completion_contract FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0]
        warn = _acceptance_unverified(receipt, contract)
        if warn is not None:
            # R1 (card t_c4a9d940): no verdict exists on the PR, so this is DEGRADED, not
            # refused -- the receipt still travels to the event log (``ok: False``, plus
            # ``degraded``) and ``record_acceptance`` writes the WARN row under the terminal
            # write txn. Falling through is deliberate: the fences AFTER acceptance (the
            # landing carrier, the deploy proof inside it, the run-ownership guard) all still
            # run, so an unread forge never buys a close over an unlanded PR's landing leg.
            receipt["degraded"] = True
            receipt["warn"] = warn
            receipt["contract"] = contract
        else:
            with write_txn(conn):
                record_acceptance(conn, task_id, acceptance)
            detail = (
                f"PR acceptance {receipt.get('classification')} of contract {contract!r}: "
                f"{receipt.get('detail', '')} {receipt.get('recovery', '')}"
            ).strip()
            if _contract_is_unsatisfiable(receipt):
                _park_unsatisfiable_contract(
                    conn, task_id, contract, receipt=receipt,
                    expected_run_id=expected_run_id,
                )
                detail = f"{detail} {_unsatisfiable_contract_release(task_id, contract, receipt)}"
            if force:
                # R3 (card t_c4a9d940): ``--force`` overrides the live-claim guard ONLY. It
                # used to pass through silently and leave the operator guessing which half it
                # had won, so a forced refusal now says which.
                detail = (
                    f"{detail} --force does not bypass PR acceptance: it overrides the "
                    "live-claim guard only, and this card's contract still needs its evidence."
                )
            return CompletionRefusal("acceptance_refusal", detail)
    # The LANDING-CARRIER fence (t_88f6cb12, ruling t_9333ca24). A review close that DECLARES a
    # deliverable PR must name its landing leg: the landing is deliberately a SEPARATE card, and
    # nothing checked it was ever created, so approved work closed green and stranded. It runs
    # AFTER the acceptance clause -- a PR with red CI is told CI is red, and a close acceptance
    # already refused never costs a forge read -- and still BEFORE the write txn, so a refusal
    # leaves the card byte-untouched and hands the worker the clause that failed.
    landing_refusal = _gate_landing_carrier(conn, task_id, metadata)
    if landing_refusal is not None:
        return landing_refusal
    # THE DISCHARGE GATE (design record platform-stl t_0608ce6d, card t_9a2aaaea). Both halves
    # judge the ARGUMENT a completion closes on, at the seam, BEFORE the card is written:
    #   E -- every ``t_<hex>`` id the handoff cites must exist on SOME board (a phantom ref is a
    #        fabricated record; ``unknown`` never refuses, because a store that will not open is
    #        a coverage gap and not a verdict);
    #   F -- a completion that argues its premise is VOID must carry the measurement it is void
    #        by, and the kernel evaluates every measurement itself.
    # Placed exactly here, with the other completion gates: after the landing carrier (a close
    # that already failed land is answered by that clause) and before the write txn, so a
    # refusal leaves the card byte-untouched and hands the worker the cause that failed.
    gate_completion_discharge(
        conn, task_id, summary=summary, result=result, metadata=metadata,
        verified_cards=verified_cards, force=force,
    )
    with write_txn(conn):
        # Hard invariant even for human review approval: a parent may have
        # reopened while this task waited.
        if not _parents_satisfied(conn, task_id):
            return CompletionRefusal(
                "parent_gate_unsatisfied",
                f"{task_id} cannot complete: parent(s) "
                + ", ".join(f"{pid} ({status})"
                            for pid, status in unsatisfied_parents(conn, task_id))
                + " are not terminal (done or archived)",
            )
        if acceptance is not None and not record_acceptance(conn, task_id, acceptance):
            return CompletionRefusal(
                "not_running",
                f"{task_id} left a completable status while it was being closed",
            )
        trow = conn.execute(
            "SELECT status, claim_lock, worker_pid, worker_started_at, current_run_id "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        prior_status = trow["status"] if trow else None
        # Refuse to close a worker's run without proof of ownership (expected_run_id) or an
        # explicit human override (force=True); the claim's liveness decides whether a caller
        # that names NO run may close at all, and the guard below keeps any admitted write off a
        # run this caller did not read. See _close_run_fence.
        allowed, _refusal, guard_sql, guard_params = _close_run_fence(
            trow, task_id, expected_run_id=expected_run_id, force=force,
        )
        if not allowed:
            _verdict = _claim_liveness(trow)
            _run = _opt_int(_row_get(trow, "current_run_id") if trow is not None else None)
            raise LiveClaimError(
                task_id, verdict=_verdict, run_id=_run,
                # An ``unknown`` verdict must never be rendered with the proven-live text: that
                # text offers ``force=True``, which is exactly the wrong instruction when the
                # claim may protect a live worker.
                detail=(live_claim_refusal(task_id, verdict=_verdict, run_id=_run)
                        if _verdict == WORKER_UNKNOWN else None),
            )
        sql = """
                UPDATE tasks
                   SET status       = 'done',
                       result       = ?,
                       completed_at = ?,
                       claim_lock   = NULL,
                       claim_expires= NULL,
                       worker_pid   = NULL,
                       block_kind   = NULL,
                       block_recurrences = 0
                 WHERE id = ?
                   AND status IN ('running', 'ready', 'blocked', 'review')
                """
        params: tuple = (result, now, task_id)
        sql += guard_sql
        params = (*params, *guard_params)
        if conn.execute(sql, params).rowcount != 1:
            # The UPDATE is guarded on a completable status, so a zero rowcount is either a
            # status this door never accepts (``triage``/``todo``/``scheduled``) or a run that
            # moved under the caller. They are different facts and get different causes —
            # "left a completable status" was printed for both, telling a worker holding a
            # ``triage`` card to retry a race that never happened (card t_012fb74e).
            return _not_completable_refusal(conn, task_id)
        if isinstance(metadata, dict):
            _stage_completion_artifacts(conn, task_id, metadata, now)
        run_id = _end_run(
            conn, task_id, outcome="completed", status="done", summary=handoff_summary,
            metadata=metadata,
        )
        # Never-claimed task: synthesize a run so the handoff fields survive.
        if run_id is None and (summary or metadata or result or prior_status == "review"):
            synth_summary, synth_metadata = handoff_summary, metadata
            if prior_status == "review" and not synth_summary and not synth_metadata:
                synth_summary = _REVIEW_APPROVED_NOTE
                synth_metadata = {"source_status": "review", "approval": "manual"}
            # No run was open when this completion landed. Either the card was never claimed, or an
            # infrastructure reclaim closed the run and NULLed ``current_run_id`` — the un-owned
            # branch admits. In the second case the run this lands on must SAY it is the recovery:
            # the crashed row is left standing as history, and this one names it (#123811).
            disowned = _newest_disowned_run(conn, task_id)
            if disowned is not None:
                synth_metadata = {
                    **(synth_metadata if isinstance(synth_metadata, dict) else {}),
                    "recovered_from_run_id": disowned,
                    "infra_reclaimed": True,
                }
            run_id = _synthesize_ended_run(
                conn, task_id, outcome="completed", summary=synth_summary, metadata=synth_metadata,
            )
        event_summary = handoff_summary
        if prior_status == "review" and not event_summary:
            event_summary = _REVIEW_APPROVED_NOTE
        completed_payload = _completed_event_payload(result, event_summary, verified_cards, metadata)
        if deferrals:
            completed_payload["priority_deferrals"] = deferrals
        _append_event(
            conn, task_id, "completed",
            completed_payload,
            run_id=run_id,
        )
    _flag_phantom_prose_refs(conn, task_id, run_id, summary, result, verified_cards)
    # Success wipes the breaker counter (history stays on the event log).
    _clear_failure_counter(conn, task_id)
    recompute_ready(conn)  # separate txn so children see ``done``
    _cleanup_workspace(conn, task_id)
    _done_task = get_task(conn, task_id)
    if fire_lifecycle_hook:
        _fire_task_hook("kanban_task_completed", _done_task, task_id, run_id, summary=handoff_summary)
    return True


_REVIEW_APPROVED_NOTE = "Review approved without additional evidence."


def _gate_created_cards(
    conn: sqlite3.Connection, task_id: str, created_cards: Optional[Iterable[str]], preview_text: Optional[str],
) -> list[str]:
    """Verify ``created_cards`` BEFORE the main write txn; returns the verified
    ids. A phantom id is recorded in its own tiny txn (auditable) then raised
    as :class:`HallucinatedCardsError` without touching task state."""
    if not created_cards:
        return []
    verified_cards, phantom_cards = _verify_created_cards(conn, task_id, created_cards)
    if phantom_cards:
        with write_txn(conn):
            _append_event(
                conn, task_id, "completion_blocked_hallucination",
                {
                    "phantom_cards": phantom_cards,
                    "verified_cards": verified_cards,
                    "summary_preview": _first_line(preview_text, 200) or None,
                },
            )
        raise HallucinatedCardsError(phantom_cards, task_id)
    return verified_cards


def _substantive_text(value: Optional[str]) -> bool:
    return bool(value is not None and str(value).strip())


def _gate_deploy_proof(
    conn: sqlite3.Connection,
    task_id: str,
    metadata: Optional[dict],
) -> None:
    """Refuse a ``landed`` card whose proof does not cover the deployed artifact.

    The firing path (not a sweep): this runs before the completion write txn, so a
    refusal leaves the card exactly as it was, records an auditable event, and hands the
    worker the clause that failed. Cards authored ``local-only`` (or with a PR contract)
    are untouched — the fence is the contract, never the lane, the board, or the assignee.

    RESTORED by card t_28dcede4 (ruling t_90767078, D1/D2). The clause engine
    (``hermes_cli/kanban_proof_gate.py``) and this fence were both live in this tree at
    16:02 EDT 2026-10-10 (``migrations/t_6a54c8da`` wrote ``proof_gate_admitted``), and
    the 19:08:56 compose of ``kanban_db.py`` from ``origin/main`` dropped the fence while
    ``ProofGateError`` stayed defined and caught — leaving a ``landed`` card closable by
    no door at all (raised nowhere, so nothing adjudicated a landed claim).
    """
    row = conn.execute(
        "SELECT completion_contract FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return
    contract = row["completion_contract"] if "completion_contract" in row.keys() else None
    # The import is GUARDED. ``hermes_cli/kanban_proof_gate.py`` is a live-only override
    # whose bytes the update rig's pin table records (``scripts/hermes_update_nodes.py``,
    # ``UNTRACKED_OVERRIDES``), so a tree that has lost it must refuse only a ``landed``
    # card — not every completion on the board. Confine the import behind the contract
    # test: a non-landed contract returns WITHOUT the module, while a missing module on a
    # ``landed`` card still fails closed (re-raises), so the landed fence never silently
    # admits a completion it cannot evaluate.
    try:
        from hermes_cli import kanban_proof_gate
    except Exception:
        if (contract or "local-only").strip().lower() != "landed":
            return
        raise
    if not kanban_proof_gate.is_landed_contract(contract):
        return
    proof = metadata.get("proof") if isinstance(metadata, dict) else None
    verdict = kanban_proof_gate.evaluate(proof, conn=conn)
    with write_txn(conn):
        _append_event(
            conn, task_id, "proof_gate_admitted" if verdict.ok else "completion_blocked_proof_gate",
            {
                "contract": contract,
                "cause": verdict.cause,
                "detail": verdict.detail,
                "facts": verdict.facts,
            },
        )
    if not verdict.ok:
        raise ProofGateError(task_id, verdict)


def _gate_landing_carrier(
    conn: sqlite3.Connection,
    task_id: str,
    metadata: Optional[dict],
) -> Optional[CompletionRefusal]:
    """Refuse an APPROVAL close whose DECLARED deliverable PR has no landing leg.

    ``review -> done`` is the fleet's most-run transition, and the landing leg is
    deliberately a SEPARATE card (``[land] <repo>#<n> for <card>``) — but nothing
    checked that the second card was ever created. When it was skipped the approved
    work had no owner and no road to prod while the board read green: the
    no-silent-failures shape, at the one transition the fleet runs most often
    (measured 2026-10-11: 538 review closes in 7 days, 396 declaring a deliverable
    PR, 48 declared PRs OPEN at the forge with no carrier on any board).

    The clause is ``hermes_cli/kanban_landing_carrier.py`` (declarations only, never
    prose — prose matching measured 16 false positives in 18 hits; carriers by
    declaration, by ``task_links`` child, or by the doctrine's ``[land``/
    ``[owner act]`` title shape on any board). It runs AFTER ``prepare_acceptance``
    and before the completion write txn -- so a close that already failed its
    acceptance clause is answered by that clause and never costs a forge read --
    records ONE auditable event, and returns a falsy
    :class:`CompletionRefusal` — so every existing ``if not complete_task(...)``
    caller keeps its meaning and the card is left byte-untouched.

    NOT bypassable by ``force`` (consistent with ``_gate_deploy_proof``): neither
    ``--force`` nor the dashboard's drag-to-done is a door around a
    declaration-based fence. The escapes are DECLARED and recorded
    (``landing_card`` / ``landing_not_owed``), and a landed revision that resolves in
    the deploy ledger closes on evidence. A bare ``landing_deferred`` is NOT an escape
    (tightened 2026-10-28): it must name a ``t_...`` carrier card and that card must
    name the PR, so "owed, not yet carried" keeps a live card on the board.

    ONE LANDING SURFACE: when upstream #123429's ``hermes_cli/kanban_landing.py``
    (landed-evidence gate) is carried into the live install, this clause belongs
    INSIDE that module's clause set and this call site is replaced by its own —
    two landing gates must never both run (ruling R3).

    A missing clause module fails CLOSED (a kernel that cannot judge the landing
    state must not admit the close blind) while keeping a no-network escape open.

    WHAT TRIGGERS IT is an explicit publication declaration -- ``published_pr`` or an
    exact PR-URL contract -- never a mere mention (``pr`` / ``pr_url`` / ``pr_number``):
    those name the PR a card published as readily as one it only cited, and judging
    them refused honest closes (ruling 2026-10-18, card ``t_9082c751``).
    """
    try:
        from hermes_cli import kanban_landing_carrier as clause
    except Exception as exc:  # a live override that is absent = an install defect, not a card's
        # No deferral waiver here (tightened 2026-10-28): a bare landing_deferred used to
        # short-circuit this fail-closed refusal, which is how a close went through in the
        # one state where nobody could judge the landing at all.
        return CompletionRefusal(
            "landing_state_unknown",
            f"{task_id}: the landing-carrier clause (hermes_cli/kanban_landing_carrier.py) is not "
            f"loadable in this install ({type(exc).__name__}), so the landing state of a declared "
            f"deliverable PR cannot be judged and the close is refused rather than admitted blind. "
            f"Restore that module through the sanctioned update route / live override set. A "
            f"landing deferral is not a waiver: name the carrier card with "
            f"metadata.landing_card=\"t_...\" once the clause is loadable again.",
        )
    verdict = clause.evaluate(
        conn=conn, task_id=task_id, metadata=metadata if isinstance(metadata, dict) else None,
    )
    if verdict is None or not verdict.refused:
        return None
    with write_txn(conn):
        _append_event(
            conn, task_id, "completion_blocked_landing_carrier",
            {"cause": verdict.cause, "detail": verdict.detail, "facts": verdict.facts},
        )
    return CompletionRefusal(verdict.cause, verdict.detail)


def _gate_empty_completion(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    result: Optional[str],
    summary: Optional[str],
) -> None:
    """Refuse a completion that would leave the card with no evidence.

    Review approvals are exempt: a human vouches for the card and
    ``_REVIEW_APPROVED_NOTE`` is the documented record.
    """
    row = conn.execute(
        "SELECT status, result FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return
    if row["status"] == "review":
        return
    stored = row["result"]
    if _substantive_text(result) or _substantive_text(summary) or _substantive_text(stored):
        return
    with write_txn(conn):
        _append_event(
            conn, task_id, "completion_blocked_empty_result",
            {
                "result_preview": _first_line(result, 200) or None,
                "summary_preview": _first_line(summary, 200) or None,
            },
        )
    raise EmptyCompletionError(task_id)


def _gate_unit_checkout_clean(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    force: bool = False,
) -> Optional[CompletionRefusal]:
    """Refuse ``done`` when the card worked in a protected unit checkout that is dirty.

    Card t_b151a247, design decision 1 (platform-stl): a card cannot close over bytes
    that exist only in a working tree. ``done`` requires
    ``git status --porcelain --untracked-files=normal`` = 0 on the checkout the run
    worked in — the SAME command ``gate.py`` check 1 runs, so the completion boundary
    and the deploy gate cannot disagree about what "dirty" means. The refusal names the
    paths, because a card that cannot see why it was refused will be re-run blind.

    SCOPE, deliberately narrow — this must never become a fleet-wide deadlock:

    * only a card that HAS a workspace path is examined, and only when that path sits
      inside a SHARED SOURCE root as the shared-tree guard declares them (read from the
      guard itself, see :mod:`hermes_cli.kanban_protected_trees`). A card in its own
      scratch dir, a linked worktree or a lane tree is untouched by this gate;
    * a LIVE root is out of scope: bytes there are drift, found by the census, and a
      card does not work in a deployment target at all;
    * the sweep is per-CHECKOUT, not fleet-wide: a dirty unrelated checkout can never
      refuse a card that did not work in it (the dirty-sweep + board-refusals DAG is
      the layer that files those).

    FAIL-OPEN, like the guard that owns the rule: an unresolvable guard, no declared
    roots, a missing workspace, or any git failure means NO refusal. A broken perimeter
    must never brick the whole fleet's completions.

    ``force`` is the operator door (``hermes kanban complete --force``).
    """
    if force:
        return None
    row = conn.execute(
        "SELECT status, workspace_path FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    workspace = row["workspace_path"]
    if not workspace:
        return None
    from hermes_cli import kanban_protected_trees as protected

    root = protected.shared_source_root(workspace)
    if root is None:
        return None
    checkout = os.path.realpath(os.path.expanduser(workspace))
    dirty = _checkout_dirty_paths(checkout)
    if not dirty:
        return None
    with write_txn(conn):
        _append_event(
            conn, task_id, "completion_blocked_dirty_unit_checkout",
            {
                "checkout": checkout,
                "protected_root": root,
                "path_count": len(dirty),
                "paths": dirty[:50],
            },
        )
    named = ", ".join(dirty[:_DIRTY_PATHS_NAMED])
    more = "" if len(dirty) <= _DIRTY_PATHS_NAMED else f" (+{len(dirty) - _DIRTY_PATHS_NAMED} more)"
    return CompletionRefusal(
        "dirty_unit_checkout",
        f"{task_id} worked in the protected unit checkout {checkout}, which is DIRTY "
        f"({len(dirty)} path(s): {named}{more}). done must not close over uncommitted "
        f"bytes: every deploy sourced from {root} is refused on check 1 until the tree "
        f"reads `git status --porcelain --untracked-files=normal` = 0. Carry the bytes "
        f"through a branch/PR and work in a card worktree (or move them to your own tree "
        f"with `{_reconcile_remedy(root)}`), then retry completion.",
    )


#: How many dirty paths a refusal names. Bounded so the message (which a model reads and
#: a human quotes) stays readable; the event carries up to 50.
_DIRTY_PATHS_NAMED = 8

#: Timeout on the porcelain read: an advisory limit on a completion path, never a
#: correctness knob. A timeout is a "could not tell" and therefore no refusal.
_DIRTY_STATUS_TIMEOUT_S = 15


def _reconcile_remedy(root: str) -> str:
    """The guard's own lossless repair verb, named as the guard spells it."""
    from hermes_cli import kanban_protected_trees as protected

    guard = protected.guard_script() or "guard-shared-trees.py"
    return f"python3 {guard} --reconcile {root} -- <paths>"


def _checkout_dirty_paths(checkout: str) -> list[str]:
    """Paths ``git status --porcelain --untracked-files=normal`` reports, or ``[]``.

    An EMPTY list means "clean OR could not tell" — the caller treats both as no
    refusal (fail-open). Paths are the strings the deploy gate itself sees: relative to
    the CHECKOUT root (git normalises that even when ``checkout`` is a subdirectory),
    with a wholly-new directory collapsed to that directory because the flag is
    ``--untracked-files=normal``. Naming them any other way here would describe the dirt
    differently from the gate that refuses the deploy over it.
    """
    try:
        if not os.path.isdir(checkout):
            return []
        result = subprocess.run(
            ["git", "-C", checkout, "status", "--porcelain", "--untracked-files=normal"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_DIRTY_STATUS_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        # Not a git checkout (or git refused): nothing this gate can assert.
        return []
    paths: list[str] = []
    for line in (result.stdout or "").splitlines():
        if not line.strip():
            continue
        # Porcelain v1: two status columns, a space, then the path (a rename reads
        # ``old -> new``; the NEW path is the one that exists in the tree).
        entry = line[3:] if len(line) > 3 else line.strip()
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        paths.append(entry.strip().strip('"'))
    return paths


def _stage_completion_artifacts(
    conn: sqlite3.Connection, task_id: str, metadata: dict, now: int, *,
    uploaded_by: str = "kanban_complete",
) -> list[Path]:
    """Copy scratch artifacts to the attachments dir and record each as an
    attachment row; returns the copies so the caller can discard them if its
    transaction rolls back."""
    _persist_scratch_completion_artifacts(conn, task_id, metadata)
    staged = [Path(stored_path) for stored_path in metadata.pop("_staged_artifacts", [])]
    for path in staged:
        _insert_completion_attachment(
            conn, task_id, filename=path.name, stored_path=str(path),
            size=path.stat().st_size, created_at=now, uploaded_by=uploaded_by,
        )
    return staged


def _cleaned_artifact_paths(metadata: Any) -> list[str]:
    """Non-blank string paths declared in ``metadata["artifacts"]``."""
    if not isinstance(metadata, dict):
        return []
    raw = metadata.get("artifacts")
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(p).strip() for p in raw if isinstance(p, str) and str(p).strip()]


def _completed_event_payload(
    result: Optional[str], event_summary: Optional[str], verified_cards: list[str], metadata: Any,
) -> dict:
    """``completed`` event payload: first summary line (400 chars) so gateway
    notifiers / dashboard WS render without a second round-trip; verified
    cards; and ``metadata["artifacts"]`` promoted so the notifier can upload
    them as native attachments without fetching the run row."""
    # Mirror CLI's _show_voice_status: include STT/TTS provider availability so the user can tell at a
    # glance *why* voice mode isn't working ("STT provider: MISSING ..." is the common case). ``record_key``
    # mirrors the configured ``voice.record_key`` so the TUI can both bind it (frontend
    # ``isVoiceToggleKey``) and display it in /voice status — previously the TUI hardcoded Ctrl+B and
    # ignored the config (#18994).
    payload: dict = {
        "result_len": len(result) if result else 0,
        "summary": _first_line(event_summary, 400) or None,
    }
    if verified_cards:
        payload["verified_cards"] = verified_cards
    if isinstance(metadata, dict):
        cleaned = _cleaned_artifact_paths(metadata)
        if cleaned:
            payload["artifacts"] = cleaned
    return payload


def _flag_phantom_prose_refs(
    conn: sqlite3.Connection, task_id: str, run_id: Optional[int],
    summary: Optional[str], result: Optional[str], verified_cards: list[str],
) -> None:
    """Advisory post-commit scan of summary+result for ``t_<hex>`` references that NO
    reachable board carries; emits ``suspected_hallucinated_references`` in its own txn so
    the completion is already durable. Never blocks.

    The RESOLVER is not here: ``hermes_cli/kanban_completion_refs.py`` owns the one tri-state
    resolver the discharge gate also judges with (design record platform-stl t_0608ce6d), so
    "phantom" means one thing on this host. Delegated rather than duplicated for exactly that
    reason - two resolvers would drift, and the drift is invisible until a real card is called
    a fabrication.
    """
    flag_phantom_prose_refs(conn, task_id, run_id, summary, result, verified_cards)


def _merge_completion_prose_artifacts(
    conn: sqlite3.Connection, task_id: str, metadata: Optional[dict], *, summary: Optional[str],
    result: Optional[str],
) -> Optional[dict]:
    """Legacy workers named deliverables only by absolute path in prose; add
    those that exist under the scratch workspace to ``metadata["artifacts"]``
    before cleanup can erase them."""
    workspace = _scratch_workspace(conn, task_id)
    if workspace is None:
        return metadata
    if not _is_managed_scratch_path(workspace):
        return metadata
    text = "\n".join(part for part in (summary, result) if part)
    if not text:
        return metadata
    prefix = re.escape(str(workspace))
    discovered: list[str] = []
    for match in re.finditer(prefix + r"(?:[/\\][^\s`\"'<>]+)", text):
        raw = match.group(0).rstrip(".,;:!?)]}")
        candidate = Path(raw)
        if candidate.is_file():
            discovered.append(str(candidate))
    if not discovered:
        return metadata
    updated = dict(metadata) if isinstance(metadata, dict) else {}
    existing = updated.get("artifacts")
    merged = list(existing) if isinstance(existing, (list, tuple)) else []
    seen = {str(path) for path in merged}
    for path in discovered:
        if path not in seen:
            merged.append(path)
            seen.add(path)
    updated["artifacts"] = merged
    return updated


def _persist_scratch_completion_artifacts(
    conn: sqlite3.Connection, task_id: str, metadata: dict,
) -> None:
    """Copy scratch-workspace completion artifacts before cleanup removes them."""
    raw_artifacts = metadata.get("artifacts")
    if not isinstance(raw_artifacts, (list, tuple)):
        return

    workspace = _scratch_workspace(conn, task_id)
    if workspace is None:
        return
    is_managed, board = _managed_scratch_path_info(workspace)
    if not is_managed:
        return

    try:
        workspace_root = workspace.resolve()
    except OSError:
        return

    attachment_dir = task_attachments_dir(task_id, board=board)
    persisted: list[str] = []
    used_destinations: set[Path] = set()
    changed = False

    def _discard_copies() -> None:
        _discard_staged_copies(used_destinations, attachment_dir)

    for item in raw_artifacts:
        artifact = str(item).strip() if isinstance(item, str) else ""
        if not artifact:
            continue
        src = Path(artifact).expanduser()
        try:
            resolved_src = src.resolve()
        except OSError:
            persisted.append(artifact)
            continue

        if not resolved_src.is_relative_to(workspace_root):
            persisted.append(artifact)
            continue

        problem = None
        if not src.is_file():
            problem = f"declared scratch artifact is unavailable or not a regular file: {artifact}"
        elif resolved_src.stat().st_size > KANBAN_ATTACHMENT_MAX_BYTES:
            problem = (
                f"declared scratch artifact exceeds the "
                f"{KANBAN_ATTACHMENT_MAX_BYTES}-byte limit: {artifact}"
            )
        if problem:
            _discard_copies()
            raise ArtifactPreservationError(problem)

        dest: Optional[Path] = None
        try:
            attachment_dir.mkdir(parents=True, exist_ok=True)
            dest = _unique_attachment_path(attachment_dir, resolved_src.name, used_destinations)
            _copy_capped(resolved_src, dest, artifact)
        except Exception as exc:
            if dest is not None:
                with contextlib.suppress(OSError):
                    dest.unlink(missing_ok=True)
            _discard_copies()
            if isinstance(exc, ArtifactPreservationError):
                raise
            raise ArtifactPreservationError(
                f"could not preserve declared scratch artifact {artifact}: {exc}"
            ) from exc
        used_destinations.add(dest)
        persisted.append(str(dest.resolve()))
        changed = True

    if changed:
        metadata["artifacts"] = persisted
        metadata["_staged_artifacts"] = [
            path for path in persisted if path.startswith(str(attachment_dir.resolve()))
        ]


def _discard_staged_copies(copies: Iterable[Path], attachment_dir: Path) -> None:
    """Remove staged attachment copies whose DB rows never committed; a leaked
    copy would make the retry stage ``name_1.ext`` next to an orphan."""
    for copied in copies:
        with contextlib.suppress(OSError):
            Path(copied).unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        attachment_dir.rmdir()


def _copy_capped(src: Path, dest: Path, artifact: str) -> None:
    """Chunked copy that aborts if the file grows past the attachment cap mid-copy."""
    with src.open("rb") as source_file, dest.open("xb") as destination_file:
        copied = 0
        while chunk := source_file.read(1024 * 1024):
            copied += len(chunk)
            if copied > KANBAN_ATTACHMENT_MAX_BYTES:
                raise ArtifactPreservationError(
                    f"declared scratch artifact grew beyond the size limit: {artifact}"
                )
            destination_file.write(chunk)


def _insert_completion_attachment(
    conn: sqlite3.Connection, task_id: str, *, filename: str, stored_path: str, size: int,
    created_at: int, uploaded_by: str = "kanban_complete",
) -> None:
    """Record a worker-produced artifact in the existing attachment table."""
    conn.execute(
        "INSERT INTO task_attachments "
        "(task_id, filename, stored_path, content_type, size, uploaded_by, created_at) "
        "VALUES (?, ?, ?, NULL, ?, ?, ?)",
        (task_id, filename, stored_path, size, uploaded_by, created_at),
    )
    _append_event(conn, task_id, "attached", {"filename": filename, "size": size, "by": uploaded_by})


def _unique_attachment_path(directory: Path, filename: str, used: set[Path]) -> Path:
    """Return a non-conflicting path under ``directory`` for ``filename``."""
    safe_name = Path(filename).name or "artifact"
    stem, suffix = Path(safe_name).stem or "artifact", Path(safe_name).suffix
    candidate = directory / safe_name
    idx = 1
    while candidate in used or candidate.exists():
        candidate = directory / f"{stem}_{idx}{suffix}"
        idx += 1
    return candidate


def edit_task(
    conn: sqlite3.Connection, task_id: str, *, title: Optional[str] = None,
    body: Optional[str] = None, priority: Optional[int] = None,
    result: Optional[str] = None, summary: Optional[str] = None,
    metadata: Optional[dict] = None, board: Optional[str] = None,
) -> bool:
    """Edit task fields, optionally backfilling a completed task's result."""
    if priority is not None:
        # Door 2b of the above-tranche guard (card t_2e107499, operator ruling 2026-09-28): the
        # CEILING, deliberately NOT policy-gated - the hand-lift above it is how the class was
        # created on ``defcon``. Runs BEFORE the domain door below, which returns early for any
        # value over ``MAX_PRIORITY`` (the marker-carrying lift is this door's call, not its).
        _refuse_above_tranche_rerank(conn, task_id, priority, board=board)
        # Door 2 of the priority domain (card t_ecbfb34b): a re-rank outside the ordinary
        # domain, or below the destination board's own declared floor, is REFUSED before
        # anything is written. The DOMAIN half is the KERNEL's - unconditional on an unwired
        # board too, because a wiring gate here is how ``defcon`` came to hold 101 rows at
        # 1100000. The FLOOR half is the board's own clause.
        _refuse_priority_outside_domain(conn, priority, board=board, task_id=task_id)
    changed_fields = [
        field for field, value in (("title", title), ("body", body), ("priority", priority))
        if value is not None
    ]
    with write_txn(conn):
        status = _task_status(conn, task_id)
        if status is None or (result is not None and status != "done"):
            return False
        gate_floor = None
        if priority is not None:
            # The GATE half of a re-rank, decided BEFORE the value is written: a card that holds
            # other cards cannot be re-ranked below them (the gated card is never demoted, so the
            # ask is raised to the floor and the event says so). Doored values were checked above,
            # against the requested number - the floor is a kernel relation, not a band.
            need = gate_need(conn, task_id)
            if need is not None and int(priority) < int(need[0]):
                policy = _policy_module()
                target = min(int(need[0]), int(policy.MAX_PRIORITY))
                if target >= int(policy.TRANCHE_FLOOR):
                    designate_priority(
                        conn, task_id,
                        reason=_gate_designation_reason(need[1], need[0], "edit"),
                        authority=GATE_AUTHORITY,
                        board=board or board_for_connection(conn) or None, nested=True,
                    )
                priority = target
                gate_floor = {"gate": need[1], "gate_priority": int(need[0]), "cause": "edit"}
                if target != int(need[0]):
                    gate_floor["capped_from"] = int(need[0])
        assignments = []
        params = []
        for field, value in (("title", title), ("body", body), ("priority", priority)):
            if value is not None:
                assignments.append(f"{field} = ?")
                params.append(value)
        if result is not None:
            assignments.append("result = ?")
            params.append(result)
            changed_fields.append("result")
        if not assignments:
            return False
        conn.execute(
            f"UPDATE tasks SET {', '.join(assignments)} WHERE id = ?",
            (*params, task_id),
        )
        if priority is not None:
            payload: dict = {"priority": priority}
            if gate_floor is not None:
                payload["gate"] = gate_floor
            _append_event(conn, task_id, "reprioritized", payload)
            # A RAISED card may now outrank what it waits on: lift the ancestors it overtook.
            # Same txn - the graph is never observably out of order.
            lift_gate_chain(conn, task_id, cause="edit", board=board)
        if result is None:
            non_priority_fields = [field for field in changed_fields if field != "priority"]
            if non_priority_fields:
                _append_event(conn, task_id, "edited", {"fields": non_priority_fields})
        else:
            handoff_summary = summary if summary is not None else result
            changed_fields.append("summary")
            if metadata is not None:
                changed_fields.append("metadata")
            run = conn.execute(
            """
            SELECT id FROM task_runs
             WHERE task_id = ?
               AND outcome = 'completed'
             ORDER BY COALESCE(ended_at, started_at, 0) DESC, id DESC
             LIMIT 1
            """,
            (task_id,),
        ).fetchone()
            if run is None:
                run_id = _synthesize_ended_run(
                    conn, task_id, outcome="completed", summary=handoff_summary, metadata=metadata,
                )
            else:
                run_id = int(run["id"])
                conn.execute("UPDATE task_runs SET summary = ? WHERE id = ?", (handoff_summary, run_id))
                if metadata is not None:
                    conn.execute(
                        "UPDATE task_runs SET metadata = ? WHERE id = ?",
                        (json.dumps(metadata, ensure_ascii=False), run_id),
                    )
            _append_event(
                conn, task_id, "edited",
                {
                    "fields": ["result", "summary"] + (["metadata"] if metadata is not None else []),
                    "result_len": len(result) if result else 0,
                    "summary": _first_line(handoff_summary, 400) or None,
                },
                run_id=run_id,
            )
    notify_task_updated(conn, task_id, changed_fields, board=board)
    return True


def retarget_task(
    conn: sqlite3.Connection, task_id: str, *, project: Optional[str] = None,
    workspace_kind: Optional[str] = None, workspace_path: Optional[str] = None,
    branch_name: Optional[str] = None, reason: Optional[str] = None,
    actor: Optional[str] = None, force: bool = False, board: Optional[str] = None,
) -> Optional[dict]:
    """Re-point a card's project + workspace - the RECOVER door for a mis-born card.

    ``create_task`` fixes a card's binding at birth, so before this verb the only
    ways to correct a card born into the wrong repo were archive+recreate (which
    severs whatever linkage the card carries - SDLC flow items, parent edges) or
    hand-written SQL. This is the missing move: it re-resolves ``project_id`` /
    ``workspace_kind`` / ``workspace_path`` / ``branch_name`` in ONE recorded step,
    appends a ``retargeted`` event carrying the before/after, and works on any
    non-terminal card - crucially a ``blocked``/``ready``/``todo`` one, the state a
    card sits in while somebody notices it is bound to the wrong tree.

    ``project`` - a project id or slug to re-anchor to (its ``primary_path`` and a
    deterministic branch), or ``""``/``"none"`` to CLEAR the link so the card owns
    no repo. Unlike ``create_task``, which drops an unresolvable project silently, a
    NAMED-but-unknown project is REFUSED: a retarget that quietly kept the old
    binding would defeat the point of the verb.

    A card with a LIVE worker claim is refused unless ``force=True`` - its worker is
    already sitting in the old tree, and re-pointing the binding under it would race.

    Returns ``None`` for an unknown id, else ``{"task_id", "old", "new",
    "changed"}``. Raises ``ValueError`` on a refusal (the CLI renders it).
    """
    task = get_task(conn, task_id)
    if task is None:
        return None
    status = (task.status or "").strip().lower()
    if status in ("done", "archived"):
        raise ValueError(
            f"cannot retarget {task_id}: task is {status!r} (terminal) - a binding is "
            f"only correctable while the card can still be worked")
    if task.claim_lock and int(task.claim_expires or 0) > int(time.time()) and not force:
        raise ValueError(
            f"cannot retarget {task_id}: a worker holds a live claim (claim_lock="
            f"{task.claim_lock}); reclaim the card first, or pass --force if the worker "
            f"is gone")

    requested = None if project is None else str(project).strip()
    clear_project = requested is not None and requested.lower() in ("", "none")
    project_obj = None
    if requested and not clear_project:
        from hermes_cli import projects_db as _pdb

        try:
            with _pdb.connect_closing() as _pconn:
                project_obj = _pdb.get_project(_pconn, requested)
        except Exception:
            project_obj = None
        if project_obj is None:
            raise ValueError(
                f"project {requested!r} not found (see `hermes project list`); nothing "
                f"was written - name a project that exists, or 'none' to clear the link")

    old = {
        "project_id": task.project_id,
        "workspace_kind": task.workspace_kind,
        "workspace_path": task.workspace_path,
        "branch_name": task.branch_name,
    }

    if clear_project and workspace_kind is None and workspace_path is None:
        # Detaching the project with no explicit workspace: the old worktree belonged
        # to the project being detached, so fall back to scratch.
        workspace_kind = "scratch"

    new_kind = workspace_kind if workspace_kind is not None else (task.workspace_kind or "scratch")
    if project_obj is not None and not (workspace_kind is not None or workspace_path is not None) \
            and new_kind == "scratch":
        # Naming a project re-anchors the card: mirror create_task's project rule (a
        # project turns a scratch card into a worktree under its repo). An explicitly
        # named workspace wins, exactly as it does on create.
        new_kind = "worktree"
    if new_kind not in VALID_WORKSPACE_KINDS:
        raise ValueError(
            f"workspace_kind must be one of {sorted(VALID_WORKSPACE_KINDS)}, got {new_kind!r}")
    new_path = workspace_path
    new_branch = branch_name
    if new_kind == "scratch":
        new_path = None
        new_branch = None
    elif new_kind == "dir":
        new_path = new_path or task.workspace_path
        if not new_path:
            raise ValueError("workspace_kind 'dir' needs an explicit workspace path")
        new_branch = None
    else:  # worktree
        if not new_path:
            if project_obj is not None and project_obj.primary_path:
                # The recovery door must not re-point a card onto the same impossible anchor
                # it is fixing (t_e5ff985b): retarget is what a lane reaches for AFTER a
                # spawn_failed, so it is the last place that may accept a non-repo primary.
                from hermes_cli import kanban_db_workspace as _wsm

                _wsm.assert_repo_anchor(
                    project_obj.primary_path,
                    task_id=task_id,
                    project_id=project_obj.id,
                    project_slug=project_obj.slug,
                )
                new_path = os.path.join(str(project_obj.primary_path), ".worktrees", task_id)
            elif project_obj is None and task.workspace_kind == "worktree" and task.workspace_path:
                new_path = task.workspace_path
            else:
                raise ValueError(
                    "workspace_kind 'worktree' needs --project (to anchor under its repo) "
                    "or an explicit --workspace worktree:<path>")
        if not new_branch and project_obj is not None:
            new_branch = _project_branch_name(project_obj, task_id, task.title)
        if not new_branch:
            new_branch = task.branch_name

    new_project_id = (None if clear_project
                      else (project_obj.id if project_obj is not None else task.project_id))
    new = {
        "project_id": new_project_id,
        "workspace_kind": new_kind,
        "workspace_path": new_path,
        "branch_name": new_branch,
    }
    changed = old != new

    with write_txn(conn):
        conn.execute(
            "UPDATE tasks SET project_id = ?, workspace_kind = ?, workspace_path = ?, "
            "branch_name = ? WHERE id = ?",
            (new_project_id, new_kind, new_path, new_branch, task_id),
        )
        if changed:
            _append_event(conn, task_id, "retargeted", {
                "old": old,
                "new": new,
                "reason": (reason or "").strip() or None,
                "actor": actor or "unknown",
            })
    if changed:
        notify_task_updated(
            conn, task_id,
            ["project_id", "workspace_kind", "workspace_path", "branch_name"],
            board=board,
        )
    return {"task_id": task_id, "old": old, "new": new, "changed": changed}


def block_task(
    conn: sqlite3.Connection, task_id: str, *, reason: Optional[str] = None,
    kind: Optional[str] = None, expected_run_id: Optional[int] = None,
    waits_on: Iterable[str] = (),
    due_at: Any = UNSET, window_policy: Optional[str] = None,
) -> bool:
    """``running``/``ready`` -> ``blocked`` (or ``todo`` / ``triage``, see
    :func:`_route_block`). ``kind='dependency'`` with no incomplete parent is
    re-kinded to ``needs_input`` (sticky) so ``recompute_ready`` cannot
    promote it into a context-free respawn. ``transient`` still counts
    toward the loop breaker so a forever-flaky task escalates. True on any
    transition.

    An already-``blocked`` card that the failure breaker parked UNTYPED
    (``block_kind IS NULL``, no live run) is classified in place when *kind*
    is supplied: ``block_kind``/``block_recurrences`` are set and a ``blocked``
    audit event is appended, while status, failure evidence and the terminal
    runs stay exactly as the breaker left them. A typed block, a card with a
    live run, or a kind-less call on a blocked card are still refused.

    ``waits_on`` DECLARES the dependency edge: every card named is validated
    (exists on this board, no cycle) through the live
    :mod:`hermes_cli.kanban_gate_invariants` gate and, after the transition,
    written as a parent edge. A ``kind='dependency'`` block whose reason names a
    card must carry that edge — prose alone names a wait the board's own
    machinery cannot read.

    ``due_at`` (epoch seconds) arms a CLOCK FENCE on the block: the deployed
    ``maintenance-sweep`` ``scheduled_wake`` node releases the card once that time
    has passed, so a time-fenced hold comes back on its own. Omit it (the default,
    ``UNSET``) for an ordinary block -- a hold with no clock, never auto-released;
    a hold waiting on a person stays a hold. Pass ``None`` to clear a fence.
    ``window_policy`` records the band policy the fence was armed under ("defer"
    by default) and, like a policy everywhere else in this contract, needs a fence
    to apply to.

    A ``dependency`` wait cannot carry a fence: it waits on an open parent, not on
    a clock, so ``kind='dependency'`` with a due time is refused.

    On a board that cannot hold a fence at all -- its ``tasks`` table predates the
    fence columns and was never migrated -- a fence is refused by name (see
    :func:`_fence_guard`) rather than dying on ``no such column: due_at``, and a
    fence-free block works there exactly as it does everywhere else.
    """
    if kind is not None and kind not in VALID_BLOCK_KINDS:
        raise ValueError(f"block kind must be one of {sorted(VALID_BLOCK_KINDS)} or None")
    # INVARIANT C at the write door (design record platform-stl t_6747232b): a block that names
    # the card it waits on must carry the EDGE, not just the prose. The board resumes a card on
    # its PARENTS finishing, so a prose-only wait is invisible to that machinery - the card sits
    # blocked after the work it named shipped, and only a human reading the reason takes it off
    # the shelf (measured: t_9e9ea756). ``waits_on`` is the structured form: it creates the edge.
    # A kind='dependency' block whose reason names a card, with no edge and no waits_on, is
    # refused with the fix in the message. Nothing is written by a refusal.
    from hermes_cli import kanban_gate_invariants as _gates
    declared_parents = _gates.gate_dependency(conn, task_id, reason, kind, waits_on)
    if window_policy is not None and window_policy not in VALID_DUE_WINDOW_POLICIES:
        raise ValueError(
            f"window_policy must be one of {sorted(VALID_DUE_WINDOW_POLICIES)}"
        )
    if kind == "dependency" and due_at is not UNSET and due_at is not None:
        raise ValueError(
            "a dependency wait cannot carry a due time: 'dependency' waits on an open "
            "parent, not on a clock, so --due is refused with --kind dependency"
        )
    # Probe the board BEFORE the write: the fence column is named by the probe row
    # only when the store carries it, and a fence the store cannot carry is refused
    # here by name instead of by driver error.
    columns = _fence_guard(conn, due_at)
    with write_txn(conn):
        probe_cols = _present_columns(columns, (
            "status", "block_kind", "block_recurrences", "due_at", "claim_lock",
            "worker_pid", "worker_started_at", "current_run_id",
        ))
        cur_row = conn.execute(
            f"SELECT {probe_cols} FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if cur_row is None:
            return False
        if (window_policy is not None and due_at is UNSET
                and _row_get(cur_row, "due_at") is None):
            # Same refusal as schedule_task: an arm policy with no fence to apply
            # it to is dead state that no releaser will ever read.
            raise ValueError(
                "window_policy needs a due time: pass due_at as well "
                "(CLI: --due ... --window-policy ...)"
            )
        # The breaker (``_record_task_failure``) parks cards ``blocked`` with no
        # ``block_kind`` and no ``blocked`` event -- the policy is the
        # supervisor's, not the kernel's -- but the transition guard below only
        # matches running/ready, so that policy could never be attached later
        # (#117363). Classify in place; never re-type or flap status. A caller
        # asserting run ownership (``expected_run_id``) cannot own a parked
        # card -- its run is over -- so it is refused like any stale worker.
        if cur_row["status"] == "blocked":
            if kind is None or expected_run_id is not None or _row_get(cur_row, "block_kind") is not None:
                return False
            classified = conn.execute(
                "UPDATE tasks SET block_kind = ?, block_recurrences = 1 "
                "WHERE id = ? AND status = 'blocked' AND block_kind IS NULL "
                "AND current_run_id IS NULL",
                (kind, task_id),
            ).rowcount
            if classified != 1:
                return False
            _append_event(conn, task_id, "blocked", {
                "kind": kind, "reason": reason, "classified_in_place": True,
            })
            return True
        source_status = _retry_status_for_run(conn, task_id) if cur_row["status"] == "running" else "ready"
        requested_kind = kind
        rekind_reason = None
        # ``dependency`` only waits on incomplete parents. A worker filing that
        # kind with none open would park in ``todo`` and ``recompute_ready``
        # would promote+respawn it context-free on the next tick. Re-kind to
        # ``needs_input`` so it is sticky until a human unblocks. A ``waits_on``
        # declaration counts as an open parent even before its edge exists - the
        # edges are written just below, inside this same function.
        if kind == "dependency" and not declared_parents and _parents_satisfied(conn, task_id):
            kind = "needs_input"
            rekind_reason = "no_open_parent"
        new_status, event_kind, set_sql, params, payload = _route_block(
            kind, reason, source_status, prev_kind=_row_get(cur_row, "block_kind"),
            prev_recurrences=int(_row_get(cur_row, "block_recurrences") or 0),
        )
        if rekind_reason:
            payload["requested_kind"] = requested_kind
            payload["rekind_reason"] = rekind_reason
        if due_at is not UNSET:
            # The fence rides the SAME update and the SAME event as the block, so a
            # releaser reading either sees the hold and its clock together.
            due_value = None if due_at is None else int(due_at)
            policy_value = (
                None if due_at is None else (window_policy or DEFAULT_DUE_WINDOW_POLICY)
            )
            set_sql = f"{set_sql},\n                       due_at = ?, due_window_policy = ?"
            params = (*params, due_value, policy_value)
            payload["due_at"] = due_value
            payload["window_policy"] = policy_value
        sql = f"""
                UPDATE tasks
                   SET status        = '{new_status}',
                       claim_lock    = NULL,
                       claim_expires = NULL,
                       worker_pid    = NULL,
                       {set_sql}
                 WHERE id = ?
                   AND status IN ('running', 'ready')
                """
        params = (*params, task_id)
        # The same fence as complete_task/request_review: a caller that names no run may only
        # park a card whose claim does not protect a live worker, and every admitted write is
        # made conditional on the run this caller read. See _close_run_fence.
        allowed, _fence_verdict, guard_sql, guard_params = _close_run_fence(
            cur_row, task_id, expected_run_id=expected_run_id, force=False,
        )
        if not allowed:
            _log.warning(
                "kanban: refusing to block %s — %s", task_id,
                live_claim_refusal(task_id, verdict=_fence_verdict,
                                   run_id=_opt_int(_row_get(cur_row, "current_run_id"))),
            )
            return False
        sql += guard_sql
        params = (*params, *guard_params)
        if conn.execute(sql, params).rowcount != 1:
            return False
        run_id = _end_or_synthesize_run(
            conn, task_id, outcome="blocked", status="blocked", summary=reason, synthesize=bool(reason),
        )
        _append_event(conn, task_id, event_kind, payload, run_id=run_id)
        blocked_task = get_task(conn, task_id)
    # The ``waits_on`` edges land HERE, after the transition, never before it: ``link_tasks``
    # demotes a ``ready`` child to ``todo`` when its new parent is not terminal, and the block's
    # own guarded UPDATE above only matches ``running``/``ready`` - linking first would make the
    # block match nothing and return False. Post-transition the child is already exactly where
    # the link would have put it, so the link only records the edge (and its ``linked`` event).
    for parent_id in declared_parents:
        try:
            link_tasks(conn, parent_id, task_id)
        except (ValueError, sqlite3.Error) as exc:
            # Never silent: the block landed, the edge it declared did not, and the card says
            # so in its own history (the dependency family in `kanban gates report` also names
            # it, because a blocked card that names a card with no edge is a violation).
            with write_txn(conn):
                _append_event(conn, task_id, "dependency_edge_refused",
                              {"parent": parent_id, "error": str(exc)})
    _fire_task_hook("kanban_task_blocked", blocked_task, task_id, run_id, reason=reason)
    return True


def _route_block(
    kind: Optional[str], reason: Optional[str], source_status: str, *,
    prev_kind: Optional[str], prev_recurrences: int,
) -> tuple[str, str, str, tuple, dict]:
    """``(new_status, event_kind, set_sql, params, payload)`` for :func:`block_task`.

    ``dependency`` never enters the human ``blocked`` bucket: it waits in
    ``todo`` for ``recompute_ready``, so a cron never sees a dependency-wait
    as something to "unblock". Callers that pass ``dependency`` with no
    incomplete parent are re-kinded to ``needs_input`` before this runs
    (see :func:`block_task`). Every other kind counts unblock-loop
    recurrences: block_task only fires from running/ready (AFTER an unblock
    returned the task to the pool), so a stored ``block_kind`` equal to the
    incoming one means blocked -> unblocked -> re-block for the same cause
    (un-typed None compares equal to a prior un-typed block). At
    ``BLOCK_RECURRENCE_LIMIT`` the task routes to ``triage`` for a human.
    """
    payload = {"reason": reason, "kind": kind, "source_status": source_status}
    if kind == "dependency":
        return "todo", "dependency_wait", "block_kind    = ?", (kind,), payload
    recurrences = prev_recurrences + 1 if prev_kind == kind else 1
    set_sql = "block_kind    = ?,\n                       block_recurrences = ?"
    payload = {"reason": reason, "kind": kind, "recurrences": recurrences, "source_status": source_status}
    if recurrences >= BLOCK_RECURRENCE_LIMIT:
        payload["limit"] = BLOCK_RECURRENCE_LIMIT
        return "triage", "block_loop_detected", set_sql, (kind, recurrences), payload
    return "blocked", "blocked", set_sql, (kind, recurrences), payload


def redact_review_value(value: Any) -> Any:
    """Redact secrets at the domain boundary for durable review handoffs."""
    if isinstance(value, str):
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(value, force=True)
    if isinstance(value, dict):
        return {key: redact_review_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_review_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_review_value(item) for item in value)
    return value


def request_review(
    conn: sqlite3.Connection, task_id: str, *, summary: Optional[str] = None,
    metadata: Optional[dict] = None, reviewer: Optional[str] = None,
    expected_run_id: Optional[int] = None, force: bool = False, with_reason: bool = False,
):
    """``running``/``ready`` -> ``review``; never touches block recurrence accounting.

    Implementer and reviewer are recorded on the event so requested changes
    route back to the right profile; ``reviewer`` reassigns the task, and on
    re-review defaults to the latest ``changes_requested`` provenance. A live
    claim is only cleared with proof of ownership (``expected_run_id``) or
    ``force=True``. Returns ``bool``, or ``(ok, reason)`` with ``with_reason``.

    ``metadata["artifacts"]`` names the handoff's deliverable
    files; a review handoff is the last implementer transition, and the
    *reviewer's* completion is what cleans the managed scratch workspace up, so
    the files are staged into the task's durable attachments dir here and the
    staged paths ride the ``review_requested`` payload for the notifier to
    upload. A declared artifact that cannot be preserved raises
    :class:`ArtifactPreservationError`, rolling the whole transition back: the
    task stays ``running`` and retryable, with no attachments and no event.
    """

    def _ret(ok: bool, reason: Optional[str] = None):
        return (ok, reason) if with_reason else ok

    summary = redact_review_value(summary)
    metadata = redact_review_value(metadata)
    # Declared (metadata["artifacts"]) and prose-referenced files
    # must be durable BEFORE anything can clean the scratch workspace up: for a
    # review-bound card the reviewer's completion is the cleanup trigger.
    metadata = _merge_completion_prose_artifacts(conn, task_id, metadata, summary=summary, result=None)
    now = int(time.time())
    # Staged copies live outside the txn: a rollback after staging must not
    # leave orphans that make the retry stage ``name_1.ext`` beside them.
    staged_copies: list[Path] = []
    try:
        with write_txn(conn):
            if not _parents_satisfied(conn, task_id):
                return _ret(False, "parent dependencies are not satisfied")
            trow = conn.execute(
                "SELECT assignee, status, claim_lock, current_run_id, worker_pid, "
                "worker_started_at, skills FROM tasks WHERE id = ?", (task_id,),
            ).fetchone()
            if trow is None:
                return _ret(False, "task not found")
            # Refuse to clear a live worker's claim without proof of ownership
            # (expected_run_id) or an explicit human override (force=True);
            # the same fence as complete_task (_close_run_fence).
            allowed, fence_verdict, guard_sql, guard_params = _close_run_fence(
                trow, task_id, expected_run_id=expected_run_id, force=force,
            )
            if not allowed:
                if fence_verdict == WORKER_UNKNOWN:
                    return _ret(False, live_claim_refusal(
                        task_id, verdict=fence_verdict,
                        run_id=_opt_int(_row_get(trow, "current_run_id")),
                    ))
                return _ret(
                    False, "task is running under a live claim; pass expected_run_id "
                    "(worker ownership) or force=True (explicit operator "
                    "override) instead of clearing the live run's claim",
                )
            if reviewer is None:
                reviewer = _prior_reviewer(conn, task_id)
                if reviewer is False:
                    return _ret(
                        False, "re-review has no durable reviewer provenance (the "
                        "latest changes_requested event is missing or "
                        "malformed); pass reviewer= explicitly",
                    )
            reviewer = _canonical_assignee(reviewer)
            if reviewer is not None:
                # The reviewer takes the card over, so the skill pair is judged here
                # exactly as assign_task judges it: refusing keeps the card in the
                # implementer's hands instead of handing the reviewer a session that
                # aborts on "Unknown skill(s)".
                try:
                    _refuse_unloadable_skills(trow["skills"], reviewer)
                except ValueError as exc:
                    return _ret(False, str(exc))
            # The actor is the run that did the work. ``assignee`` is the actor
            # only while a worker holds the card; on a never-claimed card it is
            # whoever the operator assigned -- possibly the reviewer itself,
            # which is what ``kanban create --assignee <reviewer>`` followed by
            # ``request-review`` produces. Recording the reviewer as its own
            # implementer is worse than recording nothing: request_changes()
            # routes on this field, and it already refuses a handoff that
            # carries no implementer provenance.
            implementer = None
            if trow["current_run_id"] is not None:
                arow = conn.execute(
                    "SELECT profile FROM task_runs WHERE id = ?",
                    (trow["current_run_id"],),
                ).fetchone()
                implementer = arow["profile"] if arow else None
            if implementer is None and trow["assignee"] != reviewer:
                implementer = trow["assignee"]
            assignee_sql = ", assignee = ?" if reviewer is not None else ""
            params: tuple[Any, ...] = (
                *(() if reviewer is None else (reviewer,)), task_id, *guard_params,
            )
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status        = 'review',
                       claim_lock    = NULL,
                       claim_expires = NULL,
                       worker_pid    = NULL
                """ + assignee_sql + """
                 WHERE id = ?
                   AND status IN ('running', 'ready')
                """ + guard_sql,
                params,
            )
            if cur.rowcount != 1:
                return _ret(
                    False, "task is not in running/ready (or expected_run_id did not match the current run)",
                )
            if isinstance(metadata, dict):
                staged_copies = _stage_completion_artifacts(
                    conn, task_id, metadata, now, uploaded_by="kanban_request_review",
                )
            run_id = _end_or_synthesize_run(
                conn, task_id, outcome="review_requested", status="review",
                summary=summary, metadata=metadata, synthesize=bool(summary or metadata),
                profile=implementer,
            )
            payload: dict = {
                "summary": _first_line(summary, 400) or None,
                "implementer": implementer,
                "reviewer": reviewer,
            }
            staged = _cleaned_artifact_paths(metadata)
            if staged:
                payload["artifacts"] = staged
            _append_event(conn, task_id, "review_requested", payload, run_id=run_id)
    except Exception:
        if staged_copies:
            _discard_staged_copies(staged_copies, staged_copies[0].parent)
        raise
    return _ret(True)


def review_implementer(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Implementer recorded by the latest ``review_requested`` event, else ``None``.

    ``request_review`` stamps the task's assignee at handoff time as
    ``implementer`` on that event, and only reassigns the row when a distinct
    ``reviewer`` is named — so for a card parked in ``review`` this is the
    durable author provenance. The dispatcher uses it to refuse to spawn an
    implementer as its own reviewer. ``None`` means the card never recorded one
    (no ``review_requested`` event, or a payload without a usable value);
    callers must read that as "unknown", never as "distinct".
    """
    review_event = _latest_event(conn, task_id, "review_requested")
    handoff = _json_dict(_row_get(review_event, "payload"))
    return _nonblank_str(handoff.get("implementer"))


def _prior_reviewer(conn: sqlite3.Connection, task_id: str):
    """Reviewer recorded by the latest ``changes_requested`` run's event.
    ``None`` = first review (no such run); ``False`` = a run exists but its
    provenance is missing/malformed."""
    changes_run = conn.execute(
        "SELECT id FROM task_runs "
        "WHERE task_id = ? AND outcome = 'changes_requested' "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if changes_run is None:
        return None
    changes_event = _latest_event(conn, task_id, "changes_requested", changes_run["id"])
    reviewer = _json_dict(_row_get(changes_event, "payload")).get("reviewer")
    return reviewer if isinstance(reviewer, str) and reviewer.strip() else False


def _nonblank_str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value.strip() else None


def request_changes(
    conn: sqlite3.Connection, task_id: str, *, reason: str, expected_run_id: Optional[int] = None,
) -> tuple[bool, Optional[str]]:
    """Close an active reviewer run (claimed from ``review``) and hand the task
    back to the implementer from the latest ``review_requested`` event, parent
    gating reapplied. Returns ``(ok, implementer | reason)``."""
    reason = str(redact_review_value(reason or "")).strip()
    if not reason:
        return False, "reason is required"

    with write_txn(conn):
        task_row = conn.execute(
            "SELECT status, assignee, current_run_id FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if task_row is None:
            return False, "task not found"
        current_run_id = task_row["current_run_id"]
        if task_row["status"] != "running" or current_run_id is None:
            return False, "task is not in an active review run"
        if expected_run_id is not None and int(current_run_id) != int(expected_run_id):
            return False, "run_id mismatch"

        claimed_event = _latest_event(conn, task_id, "claimed", current_run_id)
        claimed_payload = _json_dict(_row_get(claimed_event, "payload"))
        if claimed_payload.get("source_status") != "review":
            return False, "active run was not claimed from review"

        requested_event = _latest_event(conn, task_id, "review_requested")
        if requested_event is None:
            return False, "no prior review_requested event"
        implementer = _nonblank_str(_json_dict(requested_event["payload"]).get("implementer"))
        if implementer is None:
            return False, "review handoff has no valid implementer provenance"
        reviewer = _canonical_assignee(_nonblank_str(task_row["assignee"]))

        new_status = _landing_status_after_parents(conn, task_id)
        # consecutive_failures deliberately PRESERVED: a review transition is
        # not evidence the pathology cleared; only complete_task resets it.
        cur = conn.execute(
            """
            UPDATE tasks
               SET status = ?,
                   assignee = COALESCE(?, assignee),
                   claim_lock = NULL,
                   claim_expires = NULL,
                   worker_pid = NULL, worker_started_at = NULL
             WHERE id = ? AND status = 'running' AND current_run_id = ?
            """,
            (new_status, implementer, task_id, int(current_run_id)),
        )
        if cur.rowcount != 1:
            return False, "task changed during review handoff"
        run_id = _end_run(
            conn, task_id, outcome="changes_requested", status=new_status, summary=reason,
        )
        _append_event(
            conn,
            task_id,
            "changes_requested",
            {
                "reason": reason,
                "implementer": implementer,
                "reviewer": reviewer,
                "status": new_status,
            },
            run_id=run_id,
        )
    return True, implementer


def promote_task(
    conn: sqlite3.Connection, task_id: str, *, actor: str, reason: Optional[str] = None,
    dry_run: bool = False,
) -> tuple[bool, Optional[str]]:
    """Operator promotion ``todo``/``blocked`` -> ``ready`` with an audit event.
    Refused while a parent is unfinished; ``dry_run`` only validates.
    Returns ``(ok, reason)``."""
    cur_status = _task_status(conn, task_id)
    if cur_status is None:
        return False, f"task {task_id} not found"

    if cur_status not in ("todo", "blocked"):
        return False, (
            f"task {task_id} is {cur_status!r}; promote only applies to "
            f"'todo' or 'blocked'"
        )

    # No override: claim_task demotes ready -> todo on an undone parent whichever
    # writer set 'ready', so a forced promotion would only report a success the
    # first claim silently reverts (#106195). The dependency itself is the knob.
    parents = conn.execute(
        "SELECT t.id, t.status FROM tasks t "
        "JOIN task_links l ON l.parent_id = t.id "
        "WHERE l.child_id = ?", (task_id,),
    ).fetchall()
    unsatisfied = [p["id"] for p in parents if p["status"] not in ("done", "archived")]
    if unsatisfied:
        return False, (
            f"unsatisfied parent dependencies: {', '.join(unsatisfied)} "
            f"(the ready -> running claim re-checks parents, so promotion cannot "
            f"bypass them; complete the parents or drop the link with "
            f"`hermes kanban unlink <parent_id> {task_id}`)"
        )

    if dry_run:
        return True, None

    with write_txn(conn):
        upd = conn.execute(
            "UPDATE tasks SET status = 'ready' "
            "WHERE id = ? AND status IN ('todo', 'blocked')", (task_id,),
        )
        if upd.rowcount != 1:
            return False, f"task {task_id} status changed during promotion"
        _append_event(conn, task_id, "promoted_manual", {"actor": actor, "reason": reason})

    return True, None


#: The statuses the operator's REOPEN door owns. A finished card is the ONLY state this verb
#: is for: ``blocked``/``scheduled`` resume through :func:`unblock_task`, ``review`` through
#: :func:`reopen_review_task`, and a live card needs no door at all. Anything else is refused
#: BY NAME with the verb that owns its state, so a caller is routed rather than guessed at.
_REOPENABLE_STATUSES = frozenset({"done", "archived"})

#: The landings a reopen may target. ``ready`` is the ordinary one (and re-gates to ``todo``
#: while a parent is unfinished); ``todo`` is the same landing asked for EXPLICITLY - the card
#: must not be dispatchable yet regardless of its parent graph (the caller is parking work it
#: knows is not ready to be picked up, e.g. a card whose parents are all closed but whose item
#: has not been verified); ``blocked`` is the PARKED landing a caller uses when the card's WORK
#: IS DONE and it must not go back in front of a worker -- the flow's repair of a hand-closed
#: card. ``recompute_ready`` never promotes a blocked card, so the park is sticky.
_REOPEN_DESTINATIONS = frozenset({"ready", "todo", "blocked"})


def reopen_task(
    conn: sqlite3.Connection, task_id: str, *, actor: str, reason: str = "",
    dest_status: str = "ready", block_kind: Optional[str] = None,
    dry_run: bool = False,
) -> tuple[bool, Optional[str], dict]:
    """Operator REOPEN: a ``done``/``archived`` card -> a LIVE status, in one audited act.

    THE door out of a finished card. Before it existed the only writers that could undo a
    completion were the dashboard's private ``_set_status_direct`` and forbidden raw SQL, so
    every other actor -- the scheduled SDLC ``sync`` repair above all -- held a card it could
    DETECT and not move.

    ONE transaction does the whole act; NOTHING is written on a refusal:

      1. the status is re-read INSIDE the write transaction and must still be
         ``done``/``archived`` -- a card that moved in between is refused by name, never
         flipped from a stale verdict;
      2. a leaked run is reclaimed, keeping the ``current_run_id IS NULL <=> run terminal``
         invariant every other status flip keeps;
      3. ``completed_at``/``result``/the block counters/the failure breaker are cleared and
         the card is landed per ``dest_status``: ``ready`` (``todo`` while a parent is
         unfinished -- the same re-gate ``unblock`` uses), ``todo`` (landed as asked: it must
         not be dispatchable yet) or ``blocked``;
      4. every ``ready``/``review``/``running``/``done`` DESCENDANT is retracted to ``todo``
         and re-gated by :func:`invalidate_descendants_for_parent_reopen` -- a reopened
         ancestor means its descendants' completions no longer rest on it;
      5. a ``reopened`` audit event, the legacy ``status`` event the live feed and the sweeps
         read, and a comment carry the actor and the reason.

    ``dest_status="blocked"`` is the PARKED landing, and it is deliberate: a caller that
    reopens a card whose WORK IS DONE (the flow's repair of a hand-closed card) must not put
    it back in front of a worker. ``blocked`` is also sticky -- ``recompute_ready`` never
    promotes a blocked card -- so the park survives every dispatch tick. ``block_kind`` names
    why the card is parked (``external`` for a repair the flow owns).

    ``reason`` is recorded verbatim and is NOT required by the kernel: the CLI collects it
    when given, and the SDLC repair always passes one, but a bare ``reopen <id>`` is legal and
    the comment then carries no reason. ``dry_run`` validates and writes NOTHING -- not even
    an event. Returns ``(ok, why-not, info)`` where ``info`` is
    ``{"from_status", "to_status"}`` on success and ``{}`` otherwise.
    """
    reason = (reason or "").strip()
    actor = (actor or "").strip() or "operator"
    dest_status = (dest_status or "ready").strip().lower()

    status = _task_status(conn, task_id)
    if status is None:
        return False, f"task {task_id} not found", {}
    if dest_status not in _REOPEN_DESTINATIONS:
        return False, (
            f"dest_status {dest_status!r} is not a reopen landing (one of "
            f"{'/'.join(sorted(_REOPEN_DESTINATIONS))})"), {}
    if status not in _REOPENABLE_STATUSES:
        return False, (
            f"task {task_id} is {status!r}; reopen only applies to a finished card "
            f"('done'/'archived'). A 'blocked'/'scheduled' card resumes with `unblock`, a "
            f"'review' card with `reopen-review`; a live card needs no door"), {}
    if dry_run:
        return True, None, {
            "from_status": status,
            "to_status": _reopen_landing(conn, task_id, dest_status),
        }

    terminations: list[tuple[Optional[int], Optional[str], Optional[int]]] = []
    with write_txn(conn):
        # The verdict is re-read INSIDE the write txn: a card that moved between the check
        # above and here is refused by name rather than flipped from a stale read.
        now = int(time.time())
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return False, f"task {task_id} not found", {}
        if row["status"] not in _REOPENABLE_STATUSES:
            return False, f"task {task_id} moved to {row['status']!r} during reopen", {}
        prior = row["status"]
        _reclaim_dangling_run(
            conn, task_id, statuses=tuple(_REOPENABLE_STATUSES), now=now,
            note="invariant recovery on reopen",
        )
        new_status = _reopen_landing(conn, task_id, dest_status)
        cur = conn.execute(
            "UPDATE tasks SET status = ?, completed_at = NULL, result = NULL, "
            "block_kind = ?, block_recurrences = 0, consecutive_failures = 0, "
            "last_failure_error = NULL, claim_lock = NULL, claim_expires = NULL, "
            "worker_pid = NULL, worker_started_at = NULL, current_run_id = NULL, "
            "due_at = NULL, due_window_policy = NULL "
            "WHERE id = ? AND status IN ('done', 'archived')",
            (new_status, (block_kind or "external") if new_status == "blocked" else None,
             task_id),
        )
        if cur.rowcount != 1:
            return False, f"task {task_id} status changed during reopen", {}
        # Composed under THIS txn (`allow_nested`), so the flip and the retractions commit
        # together; the worker kills are drained after commit (audit trail before death).
        drain = invalidate_descendants_for_parent_reopen(conn, task_id, author=actor)
        terminations = list(drain["terminations"])
        _append_event(conn, task_id, "reopened", {
            "actor": actor, "reason": reason, "prior_status": prior,
            "status": new_status,
        })
        # The legacy event every live-feed consumer and sweep already reads.
        _append_event(conn, task_id, "status", {
            "status": new_status, "reason": "reopened",
            "previous_status": prior,
        })
        if new_status == "blocked":
            # The SAME event shape `block_task` writes: `_has_sticky_block` reads the newest
            # 'blocked' event, so this is what makes the parked landing survive
            # `recompute_ready` (a bare status write would be promoted back to ready in the
            # same tick and the repair would re-dispatch a worker onto finished work).
            _append_event(conn, task_id, "blocked", {
                "kind": block_kind or "external",
                "reason": reason or "reopened and parked by the flow",
                "source": "reopen",
            })
        _insert_comment(
            conn, task_id, actor,
            f"REOPENED from '{prior}' to '{new_status}' by {actor}"
            + (f": {reason}" if reason else ""), now,
        )
    for pid, claim_lock, started_at in terminations:
        _terminate_reclaimed_worker(pid, claim_lock, started_at=started_at)
    return True, None, {"from_status": prior, "to_status": new_status}


def _reclaim_dangling_run(
    conn: sqlite3.Connection, task_id: str, *, statuses, now: int, note: str,
) -> None:
    """Close a leaked open run before a status flip so the invariant
    ``current_run_id IS NULL <=> run row terminal`` holds; no-op normally."""
    placeholders = ", ".join("?" for _ in statuses)
    stale = conn.execute(
        f"SELECT current_run_id FROM tasks WHERE id = ? AND status IN ({placeholders})",
        (task_id, *statuses),
    ).fetchone()
    if stale and stale["current_run_id"]:
        conn.execute(
            """
            UPDATE task_runs
               SET status = 'reclaimed', outcome = 'reclaimed',
                   summary = COALESCE(summary, ?),
                   ended_at = ?,
                   claim_lock = NULL, claim_expires = NULL, worker_pid = NULL
             WHERE id = ? AND ended_at IS NULL
            """,
            (note, now, int(stale["current_run_id"])),
        )


def _reopen_landing(conn: sqlite3.Connection, task_id: str, dest_status: str) -> str:
    """The status a reopen lands on: the requested one, or the parent re-gate.

    ``blocked`` and ``todo`` are landed AS ASKED (both are non-dispatchable, so neither can be
    promoted back into a worker's queue by ``recompute_ready``); ``ready`` is re-gated to
    ``todo`` while a parent is unfinished, exactly as ``unblock`` does.
    """
    if dest_status in ("blocked", "todo"):
        return dest_status
    return _landing_status_after_parents(conn, task_id)


def _landing_status_after_parents(conn: sqlite3.Connection, task_id: str) -> str:
    """``ready`` if every parent is terminal else ``todo`` — the re-gate shared by
    unblock/reopen so neither can spawn a child whose upstream is unfinished."""
    return "ready" if _parents_satisfied(conn, task_id) else "todo"


def unblock_task(
    conn: sqlite3.Connection, task_id: str, *, extra_event: Optional[dict] = None,
) -> bool:
    """``blocked``/``scheduled`` -> its resumable phase (parent re-gated; ``review``
    when that is where it left off), closing any leaked run first.

    ``extra_event`` merges into the ``unblocked`` event payload, so a caller that
    released the card for a reason of its own (a due-fence releaser, say) leaves
    that reason on the board's event stream without a second event.
    """
    now = int(time.time())
    with write_txn(conn):
        resume_status = (
            _resume_status_from_events(conn, task_id)
            if _task_status(conn, task_id) == "blocked"
            else "ready"
        )
        _reclaim_dangling_run(
            conn, task_id, statuses=("blocked", "scheduled"), now=now,
            note="invariant recovery on unblock",
        )
        # Re-gate on parent completion before restoring the source phase.
        landing_status = _landing_status_after_parents(conn, task_id)
        new_status = (
            "review"
            if landing_status == "ready" and resume_status == "review"
            else landing_status
        )
        # ``block_kind``/``block_recurrences`` deliberately survive the unblock:
        # resetting them is the amnesia that let cron-unblock <-> re-block loop
        # unbounded; only complete_task clears them. ``consecutive_failures``
        # (the dispatcher's spawn/crash counter) IS reset — a deliberate unblock
        # is a fresh start for the retry budget. The due columns are cleared
        # because the card is no longer clock-fenced: leaving a stale ``due_at``
        # behind would re-release (or falsely alarm about) a card already running.
        cur = conn.execute(
            "UPDATE tasks SET status = ?, current_run_id = NULL, "
            "consecutive_failures = 0, last_failure_error = NULL, "
            "due_at = NULL, due_window_policy = NULL "
            "WHERE id = ? AND status IN ('blocked', 'scheduled')", (new_status, task_id),
        )
        if cur.rowcount != 1:
            return False
        payload = (
            {"status": new_status, "resume_status": resume_status}
            if new_status != "ready" or resume_status != "ready"
            else None
        )
        if extra_event:
            payload = {**(payload or {}), **extra_event}
        _append_event(conn, task_id, "unblocked", payload)
        return True


#: Statuses after which a card's declaration is FROZEN: ``set_contract`` refuses a terminal card
#: (the release is a re-scope of live work, never a rewrite of history). Same four the workspace
#: reaper uses (``kanban_db_workspace._TERMINAL_STATUSES_SQL``).
_CONTRACT_TERMINAL_STATUSES = frozenset({"done", "archived", "failed", "cancelled"})


def set_contract(
    conn: sqlite3.Connection, task_id: str, contract: str, *,
    reason: Optional[str] = None, actor: Optional[str] = None,
) -> bool:
    """Re-scope a card's ``completion_contract`` — the operator's RELEASE door.

    ``hermes kanban set-contract <id> <contract> --reason <why>`` is the release named by three
    live surfaces that had no verb behind them: the proof gate's refusal
    (``kanban_proof_gate.py``), the agent-tool refusal (``tools/kanban_tools.py``) and the
    estate-mutating fence, where the verb is already listed. A mis-authored declaration — a
    repository that requires no status checks, a superseded PR head, a ``landed`` assigned by
    routing with no unit to land — otherwise strands the card forever with no door out, and a
    refusal that names a non-existent remedy is worse than no remedy at all.

    ``contract`` is validated through
    :func:`hermes_cli.kanban_pr_acceptance.validate_contract` PLUS the deploy-proof class
    ``landed`` (``kanban_proof_gate.LANDED_CONTRACT`` — the value that clause owns; a ``landed``
    declaration is authorable and releasable by this same door, and the sibling that teaches
    ``validate_contract`` about it only makes this branch redundant). ``reason`` is REQUIRED and
    must be non-blank: the release is recorded WITH why it happened.

    Returns ``True`` on success and ``False`` for an unknown id. Raises :class:`ValueError` for a
    blank reason or an invalid contract, and :class:`RuntimeError` for a terminal card. On
    success the new value is written and a ``contract_changed`` audit event records the OLD value,
    the NEW value, the reason and the actor. NOTHING is written on a refusal: every validation
    precedes the transaction, and the terminal check raises inside it before any UPDATE.
    """
    from hermes_cli.kanban_pr_acceptance import validate_contract

    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(
            "set_contract requires a non-blank reason: the release records WHY the contract is "
            "being re-scoped (hermes kanban set-contract <id> <contract> --reason '<why>')")
    reason = reason.strip()
    if not isinstance(contract, str):
        raise ValueError("completion_contract must be a string")
    from hermes_cli.kanban_proof_gate import is_landed_contract
    if is_landed_contract(contract):
        # The deploy-proof class. Accepting it here is what makes ``landed`` AUTHORABLE and
        # RELEASABLE by one door; it is deliberately not routed through the PR validator, which
        # refuses it (the sibling card teaches it the value).
        new_contract = "landed"
    else:
        new_contract = validate_contract(contract)

    with write_txn(conn):
        row = conn.execute(
            "SELECT status, completion_contract FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return False
        if row["status"] in _CONTRACT_TERMINAL_STATUSES:
            raise RuntimeError(
                f"task {task_id} is terminal ({row['status']}); a terminal card's "
                f"completion_contract is frozen and cannot be re-scoped")
        conn.execute(
            "UPDATE tasks SET completion_contract = ? WHERE id = ?", (new_contract, task_id))
        _append_event(conn, task_id, "contract_changed", {
            "old": row["completion_contract"], "new": new_contract,
            "reason": reason, "actor": actor,
        })
    return True


def reopen_review_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """``review`` -> ``ready``/``todo`` so the implementer re-runs on the new
    comments; restores the implementer from the ``review_requested`` event.
    Preserves ``consecutive_failures`` and the block loop counter (review is
    not a block; only :func:`complete_task` clears them)."""
    now = int(time.time())
    with write_txn(conn):
        _reclaim_dangling_run(
            conn, task_id, statuses=("review",), now=now,
            note="invariant recovery on review reopen",
        )
        new_status = _landing_status_after_parents(conn, task_id)
        review_event = _latest_event(conn, task_id, "review_requested")
        handoff = _json_dict(_row_get(review_event, "payload"))
        implementer = _nonblank_str(handoff.get("implementer"))
        params: tuple[Any, ...] = (new_status, *((implementer,) if implementer else ()), task_id)
        cur = conn.execute(
            # consecutive_failures deliberately PRESERVED: review reopen is not
            # a success signal; only complete_task resets the breaker (#35072).
            "UPDATE tasks SET status = ?, current_run_id = NULL, "
            "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL "
            + (", assignee = ?" if implementer else "")
            + " WHERE id = ? AND status = 'review'",
            params,
        )
        if cur.rowcount != 1:
            return False
        payload: dict[str, Any] = {"status": new_status}
        if implementer:
            payload["implementer"] = implementer
        _append_event(
            conn, task_id, "review_reopened", payload if payload != {"status": "ready"} else None,
        )
        return True


def invalidate_descendants_for_parent_reopen(
    conn: sqlite3.Connection, task_id: str, *, author: str,
) -> dict[str, Any]:
    """THE done-reopen invalidation: every ``ready``/``review``/``running``/``done``
    descendant of a reopened ancestor is demoted to ``todo`` and re-gated.
    Every surface that reopens a done task (dashboard PATCH/drag) routes here.

    Composes under the caller's txn (``allow_nested=True``) so the flip and the
    retractions commit atomically. Each descendant gets a
    ``descendant_invalidated`` event, the legacy ``status`` event the live feed
    renders, and a comment naming the ancestor. Running descendants are closed
    ``reclaimed`` and their workers killed strictly post-commit (audit trail
    before death) — when composed, the CALLER must drain ``terminations``
    after its own commit. ``consecutive_failures`` resets (deliberate operator
    action), the opposite of :func:`reopen_review_task`.

    Returns ``{"invalidated": [{id, prior_status, new_status, resume_status}],
    "terminations": [(worker_pid, claim_lock, worker_started_at)]}``.
    """
    caller_owns_txn = bool(conn.in_transaction)
    now = int(time.time())
    invalidated: list[dict[str, Any]] = []
    terminations: list[tuple[Optional[int], Optional[str], Optional[int]]] = []
    with write_txn(conn, allow_nested=True):
        rows = conn.execute(
            """
            WITH RECURSIVE descendants(id) AS (
                SELECT child_id FROM task_links WHERE parent_id = ?
                UNION
                SELECT l.child_id
                FROM task_links l
                JOIN descendants d ON d.id = l.parent_id
            )
            SELECT t.id, t.status, t.current_run_id, t.worker_pid, t.claim_lock, t.worker_started_at
            FROM descendants d
            JOIN tasks t ON t.id = d.id
            ORDER BY t.id
            """,
            (task_id,),
        ).fetchall()
        for row in rows:
            previous_status = row["status"]
            if previous_status not in {"ready", "review", "running", "done"}:
                continue
            resume_status = "ready"
            run_id = None
            if previous_status == "review":
                resume_status = "review"
            elif previous_status == "running":
                resume_status = _retry_status_for_run(conn, row["id"], row["current_run_id"])
                terminations.append((row["worker_pid"], row["claim_lock"], row["worker_started_at"]))
                run_id = _end_run(
                    conn, row["id"], outcome="reclaimed", status="todo",
                    summary=f"ancestor {task_id} reopened",
                )
            # consecutive_failures = 0: deliberate operator reset — see
            # docstring for why this diverges from reopen_review_task.
            conn.execute(
                "UPDATE tasks SET status = 'todo', completed_at = NULL, "
                "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
                "current_run_id = NULL, consecutive_failures = 0 WHERE id = ?", (row["id"],),
            )
            entry = {
                "id": row["id"], "prior_status": previous_status,
                "new_status": "todo", "resume_status": resume_status,
            }
            _append_event(
                conn, row["id"], "descendant_invalidated",
                {"ancestor": task_id, **{k: v for k, v in entry.items() if k != "id"}},
                run_id=run_id,
            )
            # Legacy 'status' event so existing live-feed consumers still see
            # the move without learning the new event kind.
            _append_event(
                conn, row["id"], "status",
                {
                    "status": "todo", "reason": "ancestor_reopened", "parent": task_id,
                    "previous_status": previous_status, "resume_status": resume_status,
                },
                run_id=run_id,
            )
            _insert_comment(
                conn, row["id"], author, f"Invalidated: ancestor {task_id} was reopened; "
                f"retracted from '{previous_status}' to 'todo' "
                f"(will resume via '{resume_status}').", now,
            )
            invalidated.append(entry)
    if not caller_owns_txn:
        # Standalone: committed above, audit trail durable, safe to kill now.
        # Composed calls leave this to the caller post-commit.
        for pid, claim_lock, started_at in terminations:
            _terminate_reclaimed_worker(pid, claim_lock, started_at=started_at)
    return {"invalidated": invalidated, "terminations": terminations}


def specify_triage_task(
    conn: sqlite3.Connection, task_id: str, *, title: Optional[str] = None,
    body: Optional[str] = None, assignee: Optional[str] = None, author: Optional[str] = None,
) -> bool:
    """Update title/body/assignee (when given) and move ``triage -> todo`` in one
    txn; False when not in triage. Lands in ``todo`` (not ``ready``) so parent
    gating still applies; the audit comment is written only when a field changed.
    """
    if title is not None and not title.strip():
        raise ValueError("title cannot be blank")
    assignee = _canonical_assignee(assignee)
    with write_txn(conn):
        existing = conn.execute(
            "SELECT title, body, assignee FROM tasks WHERE id = ? AND status = 'triage'",
            (task_id,),
        ).fetchone()
        if existing is None:
            return False
        sets: list[str] = ["status = 'todo'"]
        params: list[Any] = []
        changed_fields: list[str] = []
        if title is not None and title.strip() != (existing["title"] or ""):
            sets.append("title = ?")
            params.append(title.strip())
            changed_fields.append("title")
        if body is not None and (body or "") != (existing["body"] or ""):
            sets.append("body = ?")
            params.append(body)
            changed_fields.append("body")
        if assignee is not None and assignee != (existing["assignee"] or None):
            sets.append("assignee = ?")
            params.append(assignee)
            changed_fields.append("assignee")
        params.append(task_id)
        cur = conn.execute(
            f"UPDATE tasks SET {', '.join(sets)} "
            f"WHERE id = ? AND status = 'triage'", tuple(params),
        )
        if cur.rowcount != 1:
            return False
        if changed_fields and author and author.strip():
            # Not add_comment (own txn + 'commented' event); 'specified' below records it.
            _insert_comment(
                conn, task_id, author.strip(),
                "Specified — updated " + ", ".join(changed_fields) + " and promoted to todo.",
                int(time.time()),
            )
        _append_event(
            conn, task_id, "specified",
            {"changed_fields": changed_fields} if changed_fields else None,
        )
    # Own IMMEDIATE txn (outside the one above): a parent-free specified task
    # flips to 'ready' now instead of idling until the next tick.
    recompute_ready(conn)
    return True


def archive_task(conn: sqlite3.Connection, task_id: str, *, signal_fn=None) -> bool:
    """Archive a task; a *running* task's host-local worker is terminated.

    Clearing ``worker_pid`` in the DB alone left the OS process running past its
    own archive — it kept executing (and pushing work) against a task nothing
    tracked anymore (#76196). Snapshot pid+claim inside the archive txn so the
    kill is contingent on THIS caller winning the archive transition (a losing
    concurrent archiver must never signal the pid); the kill itself runs after
    commit — ``_poll_worker_exit`` can wait ~5 s and must not hold the write
    lock. Post-release kill is safe here because ``archived`` is terminal: no
    dispatcher can spawn a duplicate worker off the released claim. The
    termination outcome lands as its own ``archive_worker_termination`` event so
    the ``archived`` event stays atomic with the status flip.
    """
    with write_txn(conn):
        row = conn.execute(
            "SELECT status, claim_lock, worker_pid, worker_started_at FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if not row:
            return False
        was_running = row["status"] == "running"
        prev_pid, prev_lock, prev_started = row["worker_pid"], row["claim_lock"], row["worker_started_at"]
        cur = conn.execute(
            "UPDATE tasks SET status = 'archived', "
            "    claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL "
            "WHERE id = ? AND status != 'archived'", (task_id,),
        )
        if cur.rowcount != 1:
            return False
        # Archived mid-run (dashboard): close the run so history isn't orphaned.
        run_id = _end_run(
            conn, task_id, outcome="reclaimed", status="reclaimed",
            summary="task archived with run still active",
        )
        _append_event(conn, task_id, "archived", None, run_id=run_id)
    if was_running:
        termination = _terminate_reclaimed_worker(prev_pid, prev_lock, signal_fn=signal_fn, started_at=prev_started)
        with write_txn(conn):
            _append_event(conn, task_id, "archive_worker_termination", termination, run_id=run_id)
    # ``archived`` parents no longer block children; promote them now.
    recompute_ready(conn)
    # Reap the workspace on archive too (never-completed tasks kept it forever).
    _cleanup_workspace(conn, task_id)
    return True


def _delete_task_relations(conn: sqlite3.Connection, task_id: str) -> None:
    """Delete every row referencing ``task_id`` (schema has no ON DELETE CASCADE)."""
    conn.execute("DELETE FROM task_links WHERE parent_id = ? OR child_id = ?", (task_id, task_id))
    for table in ("task_comments", "task_events", "task_runs", "kanban_notify_subs"):
        conn.execute(f"DELETE FROM {table} WHERE task_id = ?", (task_id,))


def delete_archived_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Hard-delete an ARCHIVED task (+ related rows); anything else must be
    archived first so data loss takes two deliberate actions."""
    with write_txn(conn):
        if _task_status(conn, task_id) != "archived":
            return False
        _delete_task_relations(conn, task_id)
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return cur.rowcount == 1


def delete_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Hard-delete a task and its related rows in one txn; False when not found."""
    with write_txn(conn):
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        if cur.rowcount != 1:
            return False
        _delete_task_relations(conn, task_id)
    recompute_ready(conn)
    return True


def schedule_task(
    conn: sqlite3.Connection, task_id: str, *, reason: Optional[str] = None,
    expected_run_id: Optional[int] = None,
    due_at: Any = UNSET, window_policy: Optional[str] = None,
) -> bool:
    """Park in ``scheduled`` (waiting on time, not a human; not dispatchable)
    until a releaser re-gates it.

    ``due_at`` (epoch seconds) arms the card's clock fence: the deployed
    ``maintenance-sweep`` ``scheduled_wake`` node releases it once that time has
    passed. Omit ``due_at`` (the default, ``UNSET``) and the card keeps whatever
    fence it has -- typically none, which is the human hold. Pass ``None``
    explicitly to CLEAR the fence; the verb must never convert a human hold into
    a timed hold on its own.

    ``window_policy`` records what the release should do when the fence falls
    inside a reserved execution band -- ``"defer"`` (the default once ``due_at``
    is set) holds it to the band's close, ``"ambient"`` releases anyway. A policy
    with no fence to apply it to is dead state, so it is refused.

    Re-parking an already-``scheduled`` card is allowed, and is how a deferral
    moves the same card forward; that path synthesizes no run (the card was never
    claimed in the first place).

    On a board that cannot hold a fence at all -- its ``tasks`` table predates the
    fence columns and was never migrated -- a fence is refused by name (see
    :func:`_fence_guard`) rather than dying on ``no such column: due_at``, and a
    fence-free park works there exactly as it does everywhere else.
    """
    if window_policy is not None and window_policy not in VALID_DUE_WINDOW_POLICIES:
        raise ValueError(
            f"window_policy must be one of {sorted(VALID_DUE_WINDOW_POLICIES)}"
        )
    # Probe the board BEFORE the write: the probe row names the fence column only
    # when the store carries it, and a fence the store cannot carry is refused here
    # by name instead of by driver error.
    columns = _fence_guard(conn, due_at)
    with write_txn(conn):
        row = conn.execute(
            f"SELECT {_present_columns(columns, ('status', 'due_at'))} FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        was_scheduled = row is not None and row["status"] == "scheduled"
        # A band policy with nothing to apply it to is dead state no release will
        # ever read: refuse it instead of writing an inert column.
        if (window_policy is not None and due_at is UNSET
                and (row is None or _row_get(row, "due_at") is None)):
            raise ValueError(
                "window_policy needs a due time: pass due_at as well "
                "(CLI: --due ... --window-policy ...)"
            )
        sets = [
            "status = 'scheduled'", "claim_lock = NULL", "claim_expires = NULL",
            "worker_pid = NULL",
        ]
        set_params: list[Any] = []
        if due_at is not UNSET:
            sets.append("due_at = ?")
            set_params.append(None if due_at is None else int(due_at))
            if due_at is None:
                sets.append("due_window_policy = NULL")
            else:
                sets.append("due_window_policy = ?")
                set_params.append(window_policy or DEFAULT_DUE_WINDOW_POLICY)
        elif window_policy is not None:
            sets.append("due_window_policy = ?")
            set_params.append(window_policy)
        params: list[Any] = [*set_params, task_id]
        sql = f"""
            UPDATE tasks
               SET {', '.join(sets)}
             WHERE id = ?
               AND status IN ('todo', 'ready', 'running', 'blocked', 'scheduled')
        """
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params.append(int(expected_run_id))
        if conn.execute(sql, params).rowcount != 1:
            return False
        run_id = _end_or_synthesize_run(
            conn, task_id, outcome="scheduled", status="scheduled", summary=reason,
            synthesize=bool(reason) and not was_scheduled,
        )
        payload: dict = {"reason": reason}
        if "due_at" in columns and (due_at is not UNSET or window_policy is not None):
            # Re-read rather than echo the arguments: the event has to say what
            # the card is now waiting on, including an inherited fence.
            final = conn.execute(
                f"SELECT {_present_columns(columns, ('due_at', 'due_window_policy'))} "
                "FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            payload["due_at"] = _row_get(final, "due_at")
            payload["window_policy"] = _row_get(final, "due_window_policy")
        _append_event(conn, task_id, "scheduled", payload, run_id=run_id)
        return True


# --- Worker context builder (what a spawned worker sees) ---

def build_worker_context(conn: sqlite3.Connection, task_id: str) -> str:
    """Everything a worker should read about its task: header, body,
    attachments, prior attempts, done-parent handoffs, the assignee's recent
    work, comments. Lists are tail-capped and fields char-capped
    (``_CTX_MAX_*``) so the prompt stays bounded on pathological boards."""
    task = get_task(conn, task_id)
    if not task:
        raise ValueError(f"unknown task {task_id}")
    # One clock reading so every relative age in this rendering agrees.
    now = int(time.time())
    lines: list[str] = []
    _ctx_header(lines, task, cap_defaulted=_runtime_cap_was_defaulted(conn, task_id))
    _ctx_attachments(lines, list_attachments(conn, task_id))
    _ctx_prior_attempts(lines, conn, task_id, now)
    _ctx_parent_results(lines, conn, task_id, now)
    _ctx_role_history(lines, conn, task, now)
    _ctx_comments(lines, list_comments(conn, task_id), now)
    return "\n".join(lines).rstrip() + "\n"


def _ctx_cap(s: Optional[str], limit: int = _CTX_MAX_FIELD_BYTES) -> str:
    """Truncate to ``limit`` chars with a visible ellipsis."""
    if not s:
        return ""
    s = s.strip()
    if len(s) <= limit:
        return s
    return s[:limit] + f"… [truncated, {len(s) - limit} chars omitted]"


def _ctx_stamp(ts: int, now: int) -> str:
    """``YYYY-MM-DD HH:MM`` plus a relative age when one is available."""
    disp = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    age = _relative_age(ts, now)
    return f"{disp}, {age}" if age else disp


def _ctx_metadata_line(metadata: Any) -> Optional[str]:
    if not metadata:
        return None
    try:
        return f"_metadata_: `{_ctx_cap(json.dumps(metadata, ensure_ascii=False, sort_keys=True))}`"
    except Exception:
        return None


def _ctx_tail(items: list, cap: int, noun: str) -> tuple[list, Optional[str]]:
    """Keep the newest ``cap`` items; describe the omitted head, if any."""
    omitted = max(0, len(items) - cap)
    if not omitted:
        return items, None
    return items[-cap:], (
        f"_({omitted} earlier {noun}{'s' if omitted != 1 else ''} "
        f"omitted; showing most recent {cap})_"
    )


def _runtime_cap_was_defaulted(conn: sqlite3.Connection, task_id: str) -> bool:
    """True when the card's ``max_runtime_seconds`` was stamped by the dispatcher.

    Slot recycling (ruling ``t_b2865b89`` §4) writes a ``runtime_cap_defaulted`` event beside the
    stamp (``kanban_db_dispatch._stamp_default_max_runtime_seconds``), so this audit trail is the
    ONE durable record distinguishing a cap the AUTHOR set from a cap the SCHEDULER imposed. The
    worker prompt must not claim a terminal timeout off the latter: ``_ctx_header`` would otherwise
    tell a defaulted worker ``Terminal timeout: 7170s`` and it would start a command it is killed
    in. Fails closed to "explicit" (the pre-existing rendering) on any read error.
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'runtime_cap_defaulted' "
            "LIMIT 1",
            (task_id,),
        ).fetchone()
    except Exception:
        return False
    return row is not None


def _ctx_header(lines: list[str], task: Task, *, cap_defaulted: bool = False) -> None:
    lines.append(f"# Kanban task {task.id}: {task.title}")
    lines.append("")
    lines.append(f"Assignee: {task.assignee or '(unassigned)'}")
    lines.append(f"Status:   {task.status}")
    if task.tenant:
        lines.append(f"Tenant:   {task.tenant}")
    lines.append(f"Workspace: {task.workspace_kind} @ {task.workspace_path or '(unresolved)'}")
    if task.max_runtime_seconds is not None:
        # The cap itself is always worth showing (a defaulted card really IS reaped at it), but the
        # TERMINAL TIMEOUT is a property of the author's budget, not the scheduler's: a cap nobody
        # authored widens nothing (``_dispatch_lane_task`` pins the explicit value back onto the
        # Task it spawns), so claiming a terminal timeout here would be a lie.
        lines.append(f"Max runtime: {task.max_runtime_seconds}s")
        if not cap_defaulted:
            terminal_timeout = _worker_terminal_timeout_env(
                task.max_runtime_seconds, os.environ.get("TERMINAL_TIMEOUT"),
            )
            effective_terminal_timeout = terminal_timeout or os.environ.get("TERMINAL_TIMEOUT")
            if effective_terminal_timeout:
                lines.append(f"Terminal timeout: {effective_terminal_timeout}s")
    if task.branch_name:
        lines.append(f"Branch:   {task.branch_name}")
    lines.append("")
    if task.body and task.body.strip():
        lines.append("## Body")
        lines.append(_ctx_cap(task.body, _CTX_MAX_BODY_BYTES))
        lines.append("")


def _ctx_attachments(lines: list[str], attachments: list[Attachment]) -> None:
    """Absolute on-disk paths so the worker's file tools read them directly
    (remote terminal backends need the attachments dir mounted)."""
    if not attachments:
        return
    lines.append("## Attachments")
    lines.append(
        "Files attached to this task. Read them with the file/terminal "
        "tools at the absolute paths below:"
    )
    for att in attachments:
        size_kb = max(1, (att.size + 1023) // 1024) if att.size else 0
        size_str = f", {size_kb} KB" if size_kb else ""
        ctype = f", {att.content_type}" if att.content_type else ""
        lines.append(f"- `{att.filename}`{ctype}{size_str} → `{att.stored_path}`")
    lines.append("")


def _ctx_prior_attempts(lines: list[str], conn: sqlite3.Connection, task_id: str, now: int) -> None:
    """Closed runs on this task (the active run is this worker), newest
    ``_CTX_MAX_PRIOR_ATTEMPTS`` in full, older ones as a one-line marker."""
    all_prior = [r for r in list_runs(conn, task_id) if r.ended_at is not None]
    shown, omitted_note = _ctx_tail(all_prior, _CTX_MAX_PRIOR_ATTEMPTS, "attempt")
    if not shown:
        return
    first_shown_idx = len(all_prior) - len(shown) + 1
    lines.append("## Prior attempts on this task")
    if omitted_note:
        lines.append(omitted_note)
    for offset, run in enumerate(shown):
        profile = run.profile or "(unknown)"
        outcome = run.outcome or run.status
        lines.append(
            f"### Attempt {first_shown_idx + offset} — {outcome} ({profile}, {_ctx_stamp(run.started_at, now)})"
        )
        if run.summary and run.summary.strip():
            lines.append(_ctx_cap(run.summary))
        if run.error and run.error.strip():
            lines.append(f"_error_: {_ctx_cap(run.error)}")
        meta_line = _ctx_metadata_line(run.metadata)
        if meta_line:
            lines.append(meta_line)
        lines.append("")


def _ctx_parent_results(lines: list[str], conn: sqlite3.Connection, task_id: str, now: int) -> None:
    """Done-parent handoffs: newest ``completed`` run's summary+metadata,
    falling back to ``task.result`` for pre-runs-table data. Stamped with a
    relative age so the worker re-verifies stale upstream results."""
    parent_rows = conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id", (task_id,),
    ).fetchall()
    wrote_header = False
    for pid in (r["parent_id"] for r in parent_rows):
        pt = get_task(conn, pid)
        if not pt or pt.status != "done":
            continue
        runs = [r for r in list_runs(conn, pid) if r.outcome == "completed"]
        runs.sort(key=lambda r: r.started_at, reverse=True)
        run = runs[0] if runs else None
        if not wrote_header:
            lines.append("## Parent task results")
            lines.append(
                "_Handoffs from upstream tasks, captured when each parent "
                "completed (see age below). These are point-in-time "
                "snapshots, not live state — if a result drives your "
                "current work and it's not recent, re-verify against the "
                "source before acting on it as current._"
            )
            wrote_header = True
        done_ts = run.ended_at if run is not None and run.ended_at else (pt.completed_at or None)
        age = _relative_age(done_ts, now)
        lines.append(f"### {pid}" + (f" (completed {age})" if age else ""))
        if run is not None and run.summary and run.summary.strip():
            lines.append(_ctx_cap(run.summary))
        elif pt.result:
            lines.append(_ctx_cap(pt.result))
        else:
            lines.append("(no result recorded)")
        meta_line = _ctx_metadata_line(run.metadata) if run is not None else None
        if meta_line:
            lines.append(meta_line)
        lines.append("")


def _ctx_role_history(lines: list[str], conn: sqlite3.Connection, task: Task, now: int) -> None:
    """The assignee's 5 most recent completed runs on OTHER tasks — implicit
    role continuity without wiring anything into SOUL.md / MEMORY.md."""
    if not task.assignee:
        return
    role_rows = conn.execute(
        "SELECT t.id, t.title, r.summary, r.ended_at "
        "FROM task_runs r JOIN tasks t ON r.task_id = t.id "
        "WHERE r.profile = ? AND r.task_id != ? "
        "  AND r.outcome = 'completed' "
        "ORDER BY r.ended_at DESC LIMIT 5", (task.assignee, task.id),
    ).fetchall()
    if not role_rows:
        return
    lines.append(f"## Recent work by @{task.assignee}")
    for row in role_rows:
        first = _first_line(row["summary"], 200) or "(no summary)"
        lines.append(
            f"- {row['id']} — {row['title']} ({_ctx_stamp(int(row['ended_at']), now)}): {first}"
        )
    lines.append("")


def _ctx_comments(lines: list[str], comments: list[Comment], now: int) -> None:
    """Newest ``_CTX_MAX_COMMENTS`` comments. The explicit "comment from
    worker" framing stops an operator-controlled HERMES_PROFILE like
    "hermes-system" being read as a system directive above an
    attacker-influenceable body (defense-in-depth)."""
    shown, omitted_note = _ctx_tail(comments, _CTX_MAX_COMMENTS, "comment")
    if not shown:
        return
    lines.append("## Comment thread")
    if omitted_note:
        lines.append(omitted_note)
    for c in shown:
        # Render author with explicit "comment from worker" framing so operator-controlled HERMES_PROFILE
        # values like "hermes-system" or "operator" can't be misread by the next worker as a system
        # directive above the (attacker-influenceable) comment body. Defense-in-depth — the LLM-controlled
        # author-forgery surface was already closed in #22435. See #22452.
        safe_author = (c.author or "").replace("`", "")
        lines.append(f"comment from worker `{safe_author}` at {_ctx_stamp(c.created_at, now)}:")
        lines.append(_ctx_cap(c.body, _CTX_MAX_COMMENT_BYTES))
        lines.append("")


# --- Stats + SLA helpers ---

# A ready card with no ``task_runs`` row has never been attempted by anything:
# not the dispatcher (it starts a run the moment it claims one) and not a
# terminal pulling via ``claim_task``. Past this age that is always a fault — a
# dead assignee the dispatcher buckets ``skipped_nonspawnable`` (the bucket
# health telemetry suppresses the stuck diagnostic for), a starved board, or a
# worker pool that never polls — so every health surface reports it by name
# instead of looking healthy.
ZERO_RUNS_ALERT_AGE_SECONDS = 30 * 60


def zero_run_ready(
    conn: sqlite3.Connection, *, now: Optional[float] = None,
    min_age_seconds: float = ZERO_RUNS_ALERT_AGE_SECONDS,
) -> dict:
    """Stuck-ready probe: ``ready``, unclaimed, never attempted, past ``min_age_seconds``.

    Returns ``{"count", "oldest_task_id", "oldest_assignee",
    "oldest_age_seconds", "oldest_spawnable"}`` — always a dict, so a caller
    reports "0" rather than guessing. ``oldest_spawnable`` is False when the
    oldest card's assignee is not a live profile, i.e. nothing will EVER run it
    however long the board waits. Unassigned cards are included (they also never
    run on their own) and are distinguishable by ``oldest_assignee`` being None.
    """
    at = time.time() if now is None else float(now)
    rows = conn.execute(
        "SELECT t.id, t.assignee, t.created_at FROM tasks t "
        "WHERE t.status = 'ready' AND t.claim_lock IS NULL AND t.created_at <= ? "
        "AND NOT EXISTS (SELECT 1 FROM task_runs r WHERE r.task_id = t.id) "
        "ORDER BY t.created_at ASC",
        (at - float(min_age_seconds),),
    ).fetchall()
    if not rows:
        return {
            "count": 0,
            "oldest_task_id": None,
            "oldest_assignee": None,
            "oldest_age_seconds": None,
            "oldest_spawnable": None,
        }
    oldest = rows[0]
    return {
        "count": len(rows),
        "oldest_task_id": oldest["id"],
        "oldest_assignee": oldest["assignee"],
        "oldest_age_seconds": int(at - float(oldest["created_at"])),
        "oldest_spawnable": assignee_is_spawnable(oldest["assignee"]),
    }


def board_stats(conn: sqlite3.Connection) -> dict:
    """Per-status + per-assignee counts, the oldest ``ready`` age, and the
    stuck-ready probe (see :func:`zero_run_ready`) — the board-health surface
    behind ``hermes kanban stats`` and the dashboard API."""
    by_status: dict[str, int] = {}
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM tasks "
        "WHERE status != 'archived' GROUP BY status"
    ):
        by_status[row["status"]] = int(row["n"])

    by_assignee = _counts_by_assignee(conn)

    oldest_row = conn.execute(
        "SELECT MIN(created_at) AS ts FROM tasks WHERE status = 'ready'"
    ).fetchone()
    now = int(time.time())
    oldest_ready_age = (
        (now - int(oldest_row["ts"]))
        if oldest_row and oldest_row["ts"] is not None else None
    )

    return {
        "by_status": by_status,
        "by_assignee": by_assignee,
        "oldest_ready_age_seconds": oldest_ready_age,
        "zero_run_ready": zero_run_ready(conn, now=now),
        "now": now,
    }


def _counts_by_assignee(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """``{assignee: {status: n}}`` over non-archived tasks."""
    counts: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT assignee, status, COUNT(*) AS n FROM tasks "
        "WHERE status != 'archived' AND assignee IS NOT NULL "
        "GROUP BY assignee, status"
    ):
        counts.setdefault(row["assignee"], {})[row["status"]] = int(row["n"])
    return counts


def _to_epoch(val) -> Optional[int]:
    """Epoch seconds from int/float/numeric string/ISO-8601; None for empty/invalid."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val)
    s = str(val).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        pass
    # ISO-8601 fallback (e.g. '2026-05-10T15:00:00Z')
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(s)
        return int(dt.timestamp())
    except (ValueError, OSError):
        return None


def task_age(task: Task) -> dict:
    """Return age metrics for a single task. All values are seconds or None."""
    now = int(time.time())
    _c = _to_epoch(task.created_at)
    _s = _to_epoch(task.started_at)
    _co = _to_epoch(task.completed_at)
    return {
        "created_age_seconds": now - _c if _c is not None else None,
        "started_age_seconds": now - _s if _s is not None else None,
        "time_to_complete_seconds": _co - (_s or _c) if _co is not None else None,
    }


# --- Retention + garbage collection ---

def _retention_seconds(older_than_seconds: int) -> int:
    """Normalise a gc retention window, rejecting negatives.

    Shared by both gc sweeps: a negative window puts the cutoff in the future,
    so "older than cutoff" would match every row / file instead of none —
    refuse before any sweep runs.
    """
    older_than_seconds = int(older_than_seconds)
    if older_than_seconds < 0:
        raise ValueError(
            f"older_than_seconds must be >= 0, got {older_than_seconds!r}: "
            "a negative retention selects everything."
        )
    return older_than_seconds


def gc_events(conn: sqlite3.Connection, *, older_than_seconds: int = 30 * 24 * 3600) -> int:
    """Prune old done/archived events, retaining decomposition identity until task deletion.

    ``older_than_seconds=0`` means everything older than now; the CLI maps
    ``--event-retention-days 0`` to "disabled" before calling this.
    """
    cutoff = int(time.time()) - _retention_seconds(older_than_seconds)
    with write_txn(conn):
        cur = conn.execute(
            "DELETE FROM task_events WHERE created_at < ? AND kind != 'decomposed' AND task_id IN "
            "(SELECT id FROM tasks WHERE status IN ('done', 'archived'))", (cutoff,),
        )
    return int(cur.rowcount or 0)


def gc_worker_logs(*, older_than_seconds: int = 30 * 24 * 3600, board: Optional[str] = None) -> int:
    """Delete worker log files older than the cutoff on one board; returns the count.

    ``older_than_seconds=0`` means everything older than now; the CLI maps
    ``--log-retention-days 0`` to "disabled" before calling this.
    """
    older_than_seconds = _retention_seconds(older_than_seconds)
    log_dir = worker_logs_dir(board=board)
    if not log_dir.exists():
        return 0
    cutoff = time.time() - older_than_seconds
    removed = 0
    for p in log_dir.iterdir():
        with contextlib.suppress(OSError):
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
    return removed


# --- Worker log accessor ---

def worker_log_path(task_id: str, *, board: Optional[str] = None) -> Path:
    """Worker log path (may not exist). The dispatcher always passes ``board``
    explicitly to avoid resolution ambiguity."""
    return worker_logs_dir(board=board) / f"{task_id}.log"


def read_worker_log(
    task_id: str, *, tail_bytes: Optional[int] = None, board: Optional[str] = None,
) -> Optional[str]:
    """Worker log text (last ``tail_bytes`` when set); None when the file is missing."""
    path = worker_log_path(task_id, board=board)
    if not path.exists():
        return None
    try:
        if tail_bytes is None:
            return path.read_text(encoding="utf-8-sig", errors="replace")
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                # Skip the partial first line unless the window has no newline
                # at all (readline() would eat everything).
                probe = f.tell()
                if not f.readline().endswith(b"\n") and f.tell() >= size:
                    f.seek(probe)
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return None


# --- Assignee enumeration (known profiles + per-profile board stats) ---

def list_profiles_on_disk() -> list[str]:
    """Profiles with a ``config.yaml`` plus the implicit ``default``; reads paths
    directly to avoid importing ``hermes_cli.profiles`` at startup."""
    try:
        from hermes_constants import get_default_hermes_root
        default_root = get_default_hermes_root()
        profiles_dir = default_root / "profiles"
    except Exception:
        return []

    names: set[str] = set()
    if default_root.exists():
        names.add("default")
    if profiles_dir.is_dir():
        try:
            names.update(e.name for e in profiles_dir.iterdir() if e.is_dir() and (e / "config.yaml").is_file())
        except OSError:
            pass
    return sorted(names)


def known_assignees(conn: sqlite3.Connection) -> list[dict]:
    """``{"name", "on_disk", "counts"}`` for every on-disk profile or task
    assignee, so a fresh profile appears in pickers before it has a task."""
    on_disk = set(list_profiles_on_disk())
    counts = _counts_by_assignee(conn)
    return [
        {"name": name, "on_disk": name in on_disk, "counts": counts.get(name, {})}
        for name in sorted(on_disk | set(counts))
    ]


# --- Runs (attempt history on a task) ---

def list_runs(
    conn: sqlite3.Connection, task_id: str, *, include_active: bool = True,
    state_type: Optional[str] = None, state_name: Optional[str] = None,
) -> list[Run]:
    """Runs in start order; ``include_active=False`` = closed only; ``state_type``
    (``status``/``outcome``) + ``state_name`` filter together."""
    if (state_type is None) ^ (state_name is None):
        raise ValueError("state_type and state_name must both be set or both omitted")
    if state_type is not None and state_type not in ("status", "outcome"):
        raise ValueError("state_type must be 'status' or 'outcome'")
    q = "SELECT * FROM task_runs WHERE task_id = ?"
    params: list[Any] = [task_id]
    if not include_active:
        q += " AND ended_at IS NOT NULL"
    if state_type is not None:
        q += f" AND {state_type} = ?"
        params.append(state_name)
    q += " ORDER BY started_at ASC, id ASC"
    rows = conn.execute(q, params).fetchall()
    return [Run.from_row(r) for r in rows]


def get_run(conn: sqlite3.Connection, run_id: int) -> Optional[Run]:
    row = conn.execute("SELECT * FROM task_runs WHERE id = ?", (int(run_id),)).fetchone()
    return Run.from_row(row) if row else None


def latest_run(conn: sqlite3.Connection, task_id: str) -> Optional[Run]:
    """Return the most recent run regardless of outcome (active or closed)."""
    row = conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? "
        "ORDER BY started_at DESC, id DESC LIMIT 1", (task_id,),
    ).fetchone()
    return Run.from_row(row) if row else None


def latest_summary(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Newest non-empty run summary, or None. Workers hand off via ``summary`` and
    leave ``tasks.result`` NULL, so views need this or a done task looks empty."""
    row = conn.execute(
        "SELECT summary FROM task_runs "
        "WHERE task_id = ? AND summary IS NOT NULL AND summary != '' "
        "ORDER BY COALESCE(ended_at, started_at) DESC, id DESC LIMIT 1", (task_id,),
    ).fetchone()
    return row["summary"] if row else None


def latest_summaries(conn: sqlite3.Connection, task_ids: Iterable[str]) -> dict[str, str]:
    """``{task_id: newest non-empty run summary}`` in one query (window function,
    SQLite >= 3.25); tasks without a summary are omitted."""
    ids = list(task_ids)
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT task_id, summary FROM (
            SELECT task_id, summary,
                   ROW_NUMBER() OVER (
                       PARTITION BY task_id
                       ORDER BY COALESCE(ended_at, started_at) DESC, id DESC
                   ) AS rn
              FROM task_runs
             WHERE task_id IN ({placeholders})
               AND summary IS NOT NULL AND summary != ''
        ) WHERE rn = 1
        """,
        ids,
    ).fetchall()
    return {r["task_id"]: r["summary"] for r in rows}


def current_run_started_ats(conn: sqlite3.Connection, task_ids: Iterable[str]) -> dict[str, int]:
    """``{task_id: started_at of the run ``tasks.current_run_id`` points at}``
    in one query; tasks with no active run (NULL or dangling pointer) are omitted."""
    ids = list(task_ids)
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT t.id AS task_id, r.started_at AS started_at FROM tasks t "
        "JOIN task_runs r ON r.id = t.current_run_id "
        f"WHERE t.id IN ({placeholders})",
        ids,
    ).fetchall()
    return {r["task_id"]: r["started_at"] for r in rows}


# --- Split modules (imported at the tail: they import this module as ``_kb``) ---
from hermes_cli.kanban_completion_refs import (
    PhantomRefsRefused,
    VoidClaimRefused,
    VoidClaimUnmeasured,
    discharge_gate_mode,
    flag_phantom_prose_refs,
    gate_completion_discharge,
    gate_completion_refs,
    gate_void_claim,
    resolve_refs as resolve_completion_refs,
)
from hermes_cli.kanban_db_boards import (
    _default_board_display_name,
    board_dispatch_enabled,
    board_is_archived_stub,
    list_dispatch_boards,
    _dir_holds_board,
    board_metadata_path,
    create_board,
    list_boards,
    read_board_metadata,
    remove_board,
    write_board_metadata,
)
from hermes_cli.kanban_db_links import (
    LinkOutcome,
    ParentEdgeState,
    _would_cycle,
    created_outcome,
    link_tasks_reported,
    receipt_fields,
    unlink_tasks,
)
from hermes_cli.kanban_db_connect import (
    _INITIALIZED_PATHS,
    init_db,
    write_txn,
)
from hermes_cli.kanban_db_workspace import (
    _cleanup_workspace,
    _is_managed_scratch_path,
    _managed_scratch_path_info,
    _scratch_workspace,
)
from hermes_cli.kanban_db_dispatch import (
    DEFAULT_FAILURE_LIMIT,
    DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
    DEFAULT_STORAGE_LOCKED_COOLDOWN_SECONDS,
    DispatchResult,
    WORKER_ALIVE,
    WORKER_DEAD,
    WORKER_UNKNOWN,
    _clear_failure_counter,
    _defer_reclaim_for_live_worker,
    _pid_alive,
    _record_task_failure,
    _terminate_reclaimed_worker,
    _worker_alive,
    _worker_liveness,
    _worker_not_dead,
    _worker_survived_termination,
    _worker_terminal_timeout_env,
)
