"""SQLite-backed Kanban board shared across profiles (the cross-profile coordination primitive).

Lives under the shared Hermes root: ``default`` board DB at ``<root>/kanban.db`` (pre-boards
back-compat), other boards at ``<root>/kanban/boards/<slug>/``; a worker on one board never sees
another. Board resolution: ``board=`` arg — and the in-process ``--board`` scope — > ``HERMES_KANBAN_DB``
(pins the ACTIVE board's file path for a spawned worker; honoured only when no board is named) >
``HERMES_KANBAN_BOARD`` > ``<root>/kanban/current`` > ``default``; the dispatcher injects these into
workers. An EXPLICIT board name always wins: no ambient pin may shadow it, so every board resolves to
exactly one store. A named board that is not registered RAISES :class:`BoardResolutionError` (naming
the board) rather than falling back to the current board or handing back a would-be path — an unknown,
unregistered or ambiguous board is refused, never guessed (2026-09-27, card t_d867ddbd).
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

VALID_STATUSES = {"triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done", "archived"}
# ``blocked`` stays a RECOGNISED token (so the CLI/schema reject nothing before the seam
# can answer) and is REFUSED at the create seam: a card is never created blocked
# (operator ruling 2026-09-27; see ``CREATED_BLOCKED_TOKEN`` near ``create_task``).
VALID_INITIAL_STATUSES = {"running", "blocked"}

# Typed block reasons (routing in ``_route_block``); ``None`` = legacy un-typed.
VALID_BLOCK_KINDS = {"dependency", "needs_input", "capability", "transient"}

# Same-reason block -> unblock -> re-block cycles before routing to ``triage``.
# Counts unblock recurrences, NOT dispatcher failures (``DEFAULT_FAILURE_LIMIT``).
BLOCK_RECURRENCE_LIMIT = 2
VALID_WORKSPACE_KINDS = {"scratch", "worktree", "dir"}


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


def _assert_not_delegated_child_mutation(path: "str | Path | None" = None) -> None:
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


def _fire_task_hook(event: str, task: Optional["Task"], task_id: str, run_id: Optional[int], **fields: Any) -> None:
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
    conn: sqlite3.Connection, task: "Task", workspace_path: str, pid: Optional[int], *,
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
    "timed_out", "goal_armed", "auto_blocked", "rate_limited", "auto_assigned_default",
    "respawn_guarded", "skipped_per_profile_capped", "skipped_unassigned",
    "skipped_nonspawnable", "skipped_self_review", "skipped_lockdown",
)


def _fire_dispatch_tick_hook(
    result: "DispatchResult", *, board: Optional[str] = None, dry_run: bool = False,
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


def _resolve_crash_grace_seconds() -> int:
    """``HERMES_KANBAN_CRASH_GRACE_SECONDS`` (0 = immediate, for tests) else default."""
    return _env_int("HERMES_KANBAN_CRASH_GRACE_SECONDS", DEFAULT_CRASH_GRACE_SECONDS)


def _resolve_rate_limit_cooldown_seconds() -> int:
    """``HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS`` (0 = next tick, for tests) else default."""
    return _env_int("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS)


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
        return normed if normed and board_exists(normed) else None

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
    """Board has ``board.json`` or ``kanban.db`` on disk; ``default`` always exists."""
    slug = _slug_or_default(board)
    if slug == DEFAULT_BOARD:
        return True
    return _dir_holds_board(board_dir(slug))


def _dir_holds_board(d: Path) -> bool:
    return (d / "board.json").exists() or (d / "kanban.db").exists()


def board_is_registered(board: Optional[str] = None) -> bool:
    """``board`` exists because someone REGISTERED it, not because a filing minted it.

    ``default`` is always registered. Every other slug is registered iff its
    directory carries ``board.json`` — ``create_board`` always writes that file,
    while the filing path's creating connect() (``kanban_db_path(create=True)``)
    mints only ``kanban.db``. So this is the probe a FILING uses to refuse a slug
    nobody registered; it is STRICTER than :func:`board_exists`, which admits a
    minted store.

    Same invariant :func:`board_is_archived_stub` relies on, and for the same
    reason: a directory with no ``board.json`` is a mint, never a board somebody
    created. Measured 2026-09-29 — the phantom ``yoyodine-majordomo`` board (a lane
    display handle, never a profile id) held three real cards and carried only
    ``kanban.db``, no ``board.json``.
    """
    slug = _slug_or_default(board)
    if slug == DEFAULT_BOARD:
        return True
    try:
        return (board_dir(slug) / "board.json").exists()
    except OSError:
        return False


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


def _pinned_active_store(
    env_var: str, leaf: str, default_parts: tuple[str, ...],
) -> Optional[Path]:
    """The store ``env_var`` pins for the ACTIVE board, or ``None`` when unset.

    The pin exists so a spawned worker's kanban paths still match the dispatcher's
    after ``hermes -p`` rewrites ``HERMES_HOME`` (symlink / Docker layouts), so it is
    consulted only when the caller named NO board — an explicit ``board=`` argument
    always wins (a pin that outranked it made every board resolve to one store).

    It stays a WORKER PIN by design, not a general path switch: the host tests and
    the fleet's grooming scripts legitimately point it at a store whose filename is
    not ``kanban.db`` (``apiserver.db``, ``triage-wake.db``, ...), and the 2026-09-27
    clobber's destination was a perfectly-shaped ``boards/<slug>/kanban.db`` — so a
    shape check on this pin buys nothing against the incident class while breaking
    36 tests that use the documented channel. What guards a WRITE is the destination
    guard, not a filename.
    """
    override = os.environ.get(env_var, "").strip()
    if not override:
        return None
    return Path(override).expanduser()


def _scoped_board_slug() -> Optional[str]:
    """The in-process board NAME scope (``--board`` routing / a tool's scoped board), or
    ``None``. A NAME the caller typed, so it outranks the ambient path pin exactly as a
    ``board=`` argument does — without this, ``--board ops`` inside a pinned worker still
    addressed the worker's own store (measured 2026-09-27: "no such task")."""
    raw = (_CURRENT_BOARD_OVERRIDE.get() or "").strip()
    if not raw:
        return None
    return _normalize_board_slug(raw)


def _store_path_for_slug(slug: str, default_parts: tuple[str, ...], leaf: str) -> Path:
    """The ONE layout rule for a board's canonical path: legacy ``<root>/<default_parts>``
    for the ``default`` board, else ``board_dir(slug)/leaf``. Takes an already-normalised
    slug and asks no questions — :func:`_board_path` is what decides WHICH slug (and
    refuses an unregistered one); metadata readers that must not raise call this directly."""
    if slug == DEFAULT_BOARD:
        return kanban_home().joinpath(*default_parts)
    return board_dir(slug) / leaf


def _board_path(
    env_var: Optional[str], board: Optional[str], default_parts: tuple[str, ...], leaf: str,
    *, create: bool = False,
) -> Path:
    """THE board-path resolver — every per-board path function goes through it.

    One decision, in this order: an EXPLICIT ``board=`` name (resolved on its own, so
    no ambient pin can shadow it) -> the in-process ``--board`` scope -> the ``env_var``
    pin for the active board -> ``HERMES_KANBAN_BOARD``/``<root>/kanban/current`` ->
    ``default`` -> the legacy ``<root>/<default_parts>`` layout for the ``default``
    board, else ``board_dir/leaf``.

    A named board that is not registered raises :class:`BoardResolutionError`; an empty
    name means "the active board", which is the only case where a pin applies.

    ``create=True`` is the WRITING seam (``connect``/``init_db``/``repair`` re-opening a
    board whose store is gone, regression #23833): the named board's own canonical store
    is returned even when nothing is registered yet. It is still only ever THAT board's
    file under ``boards_root()`` — never another board's store, never an arbitrary path.
    """
    slug = _normalize_board_slug(board)  # never repairs a malformed slug
    if slug is None:
        # A NAME the caller scoped in-process (`--board`, a tool's board) is a name they
        # typed, so it outranks the ambient path pin, which is only a fallback for
        # "whichever board is active here".
        slug = _scoped_board_slug()
    if slug is None:
        if env_var:
            pinned = _pinned_active_store(env_var, leaf, default_parts)
            if pinned is not None:
                return pinned
        slug = get_current_board()
    if not create and slug != DEFAULT_BOARD and not board_exists(slug):
        raise BoardResolutionError(
            f"no board {slug!r} under {boards_root()}: refusing to resolve a {leaf} path "
            f"for a board that is not registered (never falls back to the current board "
            f"{get_current_board()!r} or {DEFAULT_BOARD!r}). Register it with "
            f"`hermes kanban boards create {slug}`, or ask board_exists() if you meant a probe; "
            f"the creating seams (`connect`/`init_db`/`repair`) pass create=True."
        )
    return _store_path_for_slug(slug, default_parts, leaf)


def kanban_db_path(board: Optional[str] = None, *, create: bool = False) -> Path:
    """``kanban.db`` path. A named ``board`` always resolves to that board's own store
    (the ``HERMES_KANBAN_DB`` pin is honoured only when no board is named, and only for
    the board ``HERMES_KANBAN_BOARD`` agrees with); ``default`` -> ``<root>/kanban.db``
    (back-compat), else the board dir. An unregistered board raises
    :class:`BoardResolutionError` rather than resolving to a would-be path — pass
    ``create=True`` from a seam that is about to CREATE that board's own store."""
    return _board_path("HERMES_KANBAN_DB", board, ("kanban.db",), "kanban.db", create=create)


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

    The same rule is the one the yaan-platform resolver states as "the source
    card's board outranks ``registry.kanban-unblocker.domain_map``"; here the
    fallback it outranks is the ambient board rather than that row.

    An EXPLICIT board must resolve to a REGISTERED board or the filing is REFUSED
    (:class:`BoardResolutionError`) — a filing never MINTs a board. The creating
    connect() would otherwise satisfy any slug-shaped string by writing a store
    under ``boards/<slug>/``, which is how a lane display handle silently became
    an estate (measured 2026-09-29: the phantom ``yoyodine-majordomo`` board
    collected three real cards). A name-shaped slug is exactly the case this
    refusal exists for: registration is deliberate (``create_board`` writes
    ``board.json``), minting is not, so a caller that means a board registers it
    first or omits ``board`` to inherit the source/default board.

    Returns ``None`` when neither an explicit board nor a source board applies,
    which is what keeps the ambient chain (``env pin -> current -> default``)
    byte-for-byte as it was for every source-less caller.
    """
    if explicit:
        normed = _require_slug(str(explicit))
        if not board_is_registered(normed):
            raise BoardResolutionError(
                f"refusing to file on board {normed!r}: no board by that name is "
                f"registered under {boards_root()} (no board.json). A filing never "
                f"mints a board, so a name-shaped slug — a lane handle or a profile "
                f"display name — cannot silently become an estate. Register it with "
                f"`hermes kanban boards create {normed}`, or omit `board` to file on "
                f"the source card's board (else {DEFAULT_BOARD!r})."
            )
        return normed
    return source_board_for_task(source_task_id)


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


def board_metadata_path(board: Optional[str] = None) -> Path:
    """``board.json`` path — display metadata only; the directory slug is the identity."""
    return board_dir(_slug_or_default(board)) / "board.json"


def _default_board_display_name(slug: str) -> str:
    """``atm10-server`` -> ``Atm10 Server``."""
    return " ".join(part.capitalize() for part in slug.replace("_", "-").split("-") if part) or slug


def _board_dispatch_flag(value: Any = True) -> bool:
    """Coerce a ``board.json`` ``dispatch`` value to the admission bool.

    Absent, ``None`` or an unrecognised value means ADMITTED (``True``), so every
    board that predates the key keeps dispatching. Only an explicit false-y value
    (``false``, ``0``, ``"off"`` ...) takes a board out of the dispatch set.
    """
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    if isinstance(value, (int, float)):
        return bool(value)
    return True


def board_is_archived_stub(slug: str) -> bool:
    """A board DIRECTORY with no ``board.json`` whose slug has an ``_archived`` copy.

    The measured resurrection (card t_17c9c847): a live worker's env pins its
    board's store path, so the moment that board is archived the worker's next
    ``connect(create=True)`` (its own auto-heartbeat, a tool call) mints an EMPTY
    store back at the archived slug's directory. The mint carries no
    ``board.json``, so :func:`read_board_metadata` synthesizes ``archived=False``
    and :func:`list_boards` admits it — the archive is undone by the very worker
    it was clearing away. A directory that lacks the metadata of an archived slug
    is that mint, never a board somebody created (``create_board`` always writes
    ``board.json``), so it is not admitted to the dispatch set.
    """
    try:
        if (board_dir(slug) / "board.json").exists():
            return False
    except Exception:
        return False
    root = boards_root() / "_archived"
    if not root.is_dir():
        return False
    prefix = f"{slug}-"
    try:
        for child in root.iterdir():
            if not child.name.startswith(prefix):
                continue
            # ``<slug>-<epoch>`` (and ``<slug>-<epoch>-<n>`` on a rapid re-archive).
            if child.name[len(prefix):].split("-")[0].isdigit():
                return True
    except OSError:
        return False
    return False


def board_dispatch_enabled(board: Optional[str] = None) -> bool:
    """May the dispatcher serve ``board``? ``False`` for an estate/scratch board.

    The one chokepoint read by the dispatcher's board enumeration
    (:func:`list_dispatch_boards`) AND by its per-tick spawn guard
    (``kanban_db_dispatch.dispatch_once``), so an estate board is safe by
    construction — including from a CLI ``hermes kanban --board <estate> dispatch``
    that bypasses enumeration entirely (card t_17c9c847).

    Fails CLOSED for a resurrected archived stub: a minted directory is not a
    board, so re-opening an archived slug's path cannot re-admit it.
    """
    slug = _slug_or_default(board)
    if board_is_archived_stub(slug):
        return False
    try:
        return bool(read_board_metadata(slug).get("dispatch", True))
    except Exception:
        return True


def read_board_metadata(board: Optional[str] = None) -> dict:
    """``board.json`` merged over defaults, plus ``slug`` and ``db_path``. Never
    raises — a missing/malformed file yields the synthesized entry."""
    slug = _slug_or_default(board)
    meta: dict[str, Any] = {
        "slug": slug,
        "name": _default_board_display_name(slug),
        "description": "",
        "icon": "",
        "color": "",
        "default_workdir": None,
        # Project scope: new tasks inherit it (deterministic worktree + branch).
        "project_id": None,
        "created_at": None,
        "archived": False,
        # Dispatch admission (estate/rehearsal boards): absent => True, so every
        # board that predates the key is unchanged. ``False`` now reaches the
        # dispatcher's own board enumeration AND its per-tick spawn path, so a
        # rehearsal estate is safe by construction rather than by remembering to
        # archive it (card t_17c9c847).
        "dispatch": True,
    }
    try:
        p = board_metadata_path(slug)
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                # Never let the metadata file claim a different slug than
                # its directory — trust the filesystem.
                raw["slug"] = slug
                meta.update(raw)
    except (OSError, json.JSONDecodeError):
        pass
    # Normalise the flag: a hand-edited ``"false"``/``0`` is falsy-by-string and
    # would otherwise read as admitted. Anything except an explicit false-y value
    # admits the board (fail-open on admission only for a missing/garbled key,
    # which is what keeps every existing board dispatchable).
    meta["dispatch"] = _board_dispatch_flag(meta.get("dispatch"))
    # The board's OWN canonical store, by layout — never the ambient pin: this reader
    # must not raise (it runs while a board is still being created) and must not answer
    # another board's store, which is exactly what the pin used to make it do.
    meta["db_path"] = str(_store_path_for_slug(slug, ("kanban.db",), "kanban.db"))
    return meta


def write_board_metadata(
    board: Optional[str], *, name: Optional[str] = None, description: Optional[str] = None,
    icon: Optional[str] = None, color: Optional[str] = None, archived: Optional[bool] = None,
    default_workdir: Optional[str] = None, project_id: Optional[str] = None,
    priority_policy: Optional[Any] = None, operator_register: Optional[str] = None,
    dispatch: Optional[bool] = None,
) -> dict:
    """Create/update ``board.json``; unmentioned fields are preserved, ``created_at``
    set on first write. ``project_id``/``default_workdir``: ``None`` = unchanged,
    "" = clear (``project_id`` is not validated here). ``priority_policy``: ``None`` =
    unchanged, "" = clear, else the spec stored as given (validated on the read side, by
    ``kanban_priority_policy.normalize_spec``). ``operator_register``: ``None`` =
    unchanged, "" = clear, else the card id of the register for this board - the anchor
    ``hermes kanban rollup`` walks when no register is named; validated here because a
    reader must never have to (``kanban_register.parse_ref``). ``dispatch``: ``None`` =
    unchanged (and the synthesized ``True`` default is NOT materialized, so a board
    that never touched the key keeps its ``board.json`` byte-identical); ``False``
    marks an estate/scratch board the dispatcher must not serve."""
    _assert_not_delegated_child_mutation()
    slug = _slug_or_default(board)
    meta = read_board_metadata(slug)
    # db_path is derived on every read; never persist it into board.json.
    meta.pop("db_path", None)
    if name is not None:
        meta["name"] = str(name).strip() or _default_board_display_name(slug)
    for key, value in (("description", description), ("icon", icon), ("color", color)):
        if value is not None:
            meta[key] = str(value)
    if archived is not None:
        meta["archived"] = bool(archived)
    if dispatch is not None:
        meta["dispatch"] = bool(dispatch)
    elif meta.get("dispatch") is True:
        # ``read_board_metadata`` synthesizes ``dispatch: True``; an unchanged
        # board must not gain the key on an unrelated write (rename, workdir...).
        meta.pop("dispatch", None)
    for key, value in (("default_workdir", default_workdir), ("project_id", project_id)):
        if value is not None:
            meta[key] = str(value) if value else None
    if operator_register is not None:
        from hermes_cli import kanban_register as _register

        register_id = str(operator_register).strip()
        if not register_id:
            meta.pop(_register.META_KEY, None)
        elif not _register.is_task_id(register_id):
            raise ValueError(
                f"operator_register must be a card id ('t_' + hex), got {operator_register!r}; "
                "nothing was written"
            )
        else:
            meta[_register.META_KEY] = register_id
    if priority_policy is not None:
        from hermes_cli import kanban_priority_policy as _policy

        # A spec is an object, so it cannot ride the string-coercing loop above, and a
        # clear removes the key outright rather than leaving a null behind: an unwired
        # board.json must read exactly as it did before anything was set.
        if priority_policy:
            meta[_policy.POLICY_KEY] = priority_policy
        else:
            meta.pop(_policy.POLICY_KEY, None)
    if not meta.get("created_at"):
        meta["created_at"] = int(time.time())
    path = board_metadata_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    meta["db_path"] = str(_store_path_for_slug(slug, ("kanban.db",), "kanban.db"))
    return meta


def create_board(
    slug: str, *, name: Optional[str] = None, description: Optional[str] = None,
    icon: Optional[str] = None, color: Optional[str] = None, default_workdir: Optional[str] = None,
    project_id: Optional[str] = None, dispatch: Optional[bool] = None,
) -> dict:
    """Create board dir + DB + metadata (``mkdir -p`` semantics: existing board returns its metadata).

    ``dispatch=False`` births an ESTATE/scratch board the dispatcher never serves
    (card t_17c9c847): the flag is written into ``board.json`` at creation, so the
    board is undispatchable by construction rather than by a teardown somebody has
    to remember.
    """
    normed = _require_slug(slug)
    meta = write_board_metadata(
        normed, name=name, description=description, icon=icon, color=color,
        default_workdir=default_workdir, project_id=project_id, dispatch=dispatch,
    )
    # Touch the DB so list_boards() sees it immediately.
    init_db(board=normed)
    return meta


def list_boards(*, include_archived: bool = True) -> list[dict]:
    """Metadata for every board: ``default`` first (always present), then
    ``boards/<slug>/`` dirs holding a ``kanban.db`` or ``board.json``, sorted.

    A resurrected archived STUB — a minted directory with no ``board.json`` whose
    slug already has an ``_archived`` copy — is NOT a board and is skipped here,
    so the phantom a pinned worker re-creates cannot re-enter any enumeration
    (:func:`board_is_archived_stub`, card t_17c9c847)."""
    entries = [read_board_metadata(DEFAULT_BOARD)]
    seen = {DEFAULT_BOARD}
    root = boards_root()
    if root.is_dir():
        for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir():
                continue
            try:
                normed = _normalize_board_slug(child.name)  # skip junk dirs, don't raise
            except ValueError:
                continue
            if not normed or normed in seen or not _dir_holds_board(child):
                continue
            if board_is_archived_stub(normed):
                continue
            meta = read_board_metadata(normed)
            if meta.get("archived") and not include_archived:
                continue
            entries.append(meta)
            seen.add(normed)
    return entries


def list_dispatch_boards() -> list[dict]:
    """Live boards the DISPATCHER may serve: non-archived AND dispatch-enabled.

    The dispatcher's own board enumeration (card t_17c9c847). :func:`list_boards`
    stays the full inventory for the CLI and the dashboard, so an estate board is
    still VISIBLE on the board list — it is simply never spawned from. Both halves
    are load-bearing: the ``dispatch`` flag closes the gap the archived-only filter
    left (``"each board has its own ... dispatcher loop"``), and
    :func:`board_dispatch_enabled` fails closed for a resurrected archived stub.
    """
    return [
        meta for meta in list_boards(include_archived=False)
        if board_dispatch_enabled(meta.get("slug") or DEFAULT_BOARD)
    ]


def remove_board(slug: str, *, archive: bool = True) -> dict:
    """Archive (to ``boards/_archived/<slug>-<ts>/``) or delete a board;
    ``default`` cannot be removed. Returns ``{"slug", "action", "new_path"}``."""
    _assert_not_delegated_child_mutation()
    normed = _require_slug(slug)
    if normed == DEFAULT_BOARD:
        raise ValueError("the 'default' board cannot be removed")
    d = board_dir(normed)
    if not d.exists():
        raise ValueError(f"board {normed!r} does not exist")

    # If the user removed the currently-active board, revert to default.
    if get_current_board() == normed:
        clear_current_board()

    # A concurrent connect() after the rename recreates an empty DB file; drop
    # the init cache first so the schema pass re-runs on it.
    _INITIALIZED_PATHS.discard(str((d / "kanban.db").resolve()))

    if archive:
        archive_root = boards_root() / "_archived"
        archive_root.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        target = archive_root / f"{normed}-{ts}"
        suffix = 1
        while target.exists():  # rapid double-archive
            target = archive_root / f"{normed}-{ts}-{suffix}"
            suffix += 1
        d.rename(target)
        return {"slug": normed, "action": "archived", "new_path": str(target)}
    import shutil
    shutil.rmtree(d)
    return {"slug": normed, "action": "deleted", "new_path": ""}


def board_priority_policy(board: Optional[str] = None) -> Optional[dict]:
    """The normalised ``priority_policy`` for ``board``, or ``None`` when it has none.

    The read-only half of the birth seam: what a policy's spec actually resolves to,
    validated exactly as ``create_task`` validates it, so a caller can report or assert
    a board's wiring without filing a card. Raises ``PolicyError`` for a configured but
    unusable spec - the same refusal a filing would get.
    """
    from hermes_cli import kanban_priority_policy as policy

    raw = read_board_metadata(board if board else get_current_board()).get(policy.POLICY_KEY)
    return policy.normalize_spec(raw)

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

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Task":
        g = lambda col, default=None: _lossy_text(_row_get(row, col, default))  # noqa: E731
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
    "current_step_key", "max_retries", "session_id", "completion_contract",
)
# Text columns where "" is stored/read as "not set".
_TASK_EMPTY_IS_NULL_COLUMNS = (
    "model_override", "provider_override", "reasoning_effort", "goal_max_turns", "block_kind",
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
    def from_row(cls, row: sqlite3.Row) -> "Run":
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
    def from_row(cls, r: sqlite3.Row) -> "Comment":
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
    def from_row(cls, r: sqlite3.Row) -> "Attachment":
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
    def from_row(cls, row: sqlite3.Row) -> "Event":
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
    block_recurrences    INTEGER NOT NULL DEFAULT 0
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
-- designated card (R3 - no new database, the board's own store). ``hermes kanban defcon`` is
-- the only writer, and the opt-in tranche storage guard refuses a tranche value on a card with
-- no live row here. ``priority`` is the card's ORDINARY priority: what ``revoke`` restores.
CREATE TABLE IF NOT EXISTS priority_designations (
    task_id       TEXT PRIMARY KEY,
    board         TEXT NOT NULL,
    priority      INTEGER NOT NULL,
    authority     TEXT,
    reason        TEXT NOT NULL,
    designated_at TEXT NOT NULL,
    revoked_at    TEXT
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


#: Live-profile roster cache: ``(expires_at_monotonic, names)``. A create is not a
#: hot path but a drain can file cards in a loop, so the roster listing is cached
#: briefly rather than re-listed per card. Short TTL keeps a newly created profile
#: usable without restarting the gateway.
_LIVE_PROFILE_CACHE: tuple[float, Optional[set[str]]] = (0.0, None)
_LIVE_PROFILE_TTL_SECONDS = 30.0
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
        # Concrete path is deferred to the insert loop: a fresh
        # ``<repo>/.worktrees/<task-id>`` keyed on the new task id.
        project_repo = str(project_obj.primary_path)
    return project_obj.id, project_obj, project_repo, workspace_kind


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


# ---------------------------------------------------------------------------
# Born-blocked cards are REFUSED at the create path (operator ruling 2026-09-27)
#
# A card born ``blocked`` carries no block_kind and no reason: it hides work from the
# dispatcher (which only picks up ``ready``), leaves a row no reader can diagnose, and
# does not even hold - the next ``recompute_ready`` promotes it, so the flag buys a card
# with no blocker AND a status that lies. Measured when this landed: 51 created-blocked
# rows on ``ops``, 1 on ``defcon`` (``task_events.kind='blocked'`` + payload
# ``reason='initial_status'``). The ruling is absolute - no board, caller or
# configuration may make a new one. So this seam refuses the attempt, escalates it, and
# files the deviation against the creating lane BY NAME; nothing is inserted.
#
# The bypass (a raw ``INSERT INTO tasks`` with ``status='blocked'``) is deliberately NOT
# closed by an INSERT trigger: an unconditional trigger would also refuse a legitimate
# board restore / ``boards import``, which replays the legacy born-blocked rows. That
# class is REPORTED instead by the created-blocked regression watch (card t_9ad5e246),
# which files any created-blocked row appearing after this refusal landed. Design record:
# platform-stl card t_5c89c04e.
# ---------------------------------------------------------------------------

# The token stays RECOGNISED on every surface (schema enum, CLI choices) and is refused
# HERE, so the caller is answered with the two legitimate moves instead of argparse's
# bare "invalid choice: 'blocked'".
CREATED_BLOCKED_TOKEN = "blocked"
BORN_BLOCKED_DEVIATION_PREFIX = "DEVIATION (born-blocked refused)"
BORN_BLOCKED_GUARD_IDENTITY = "kanban-create-guard"

BORN_BLOCKED_ALTERNATIVES = (
    "Legitimate move 1 - WAITING on other work: create the card normally with its dependency "
    "edges - parents=[...] on kanban_create, or `hermes kanban create <title> --parent <id>`. "
    "The card is born `todo` and promotes itself when every parent is done; the parked state "
    "names what it waits on, so nothing is hidden. "
    "Legitimate move 2 - a REAL BLOCK found in the course of work: create the card normally, "
    "then block it with a kind and a reason - kanban_block(kind=\"dependency\", "
    "reason=\"...\") (or needs_input / capability / transient), or `hermes kanban block <id> "
    "\"<reason>\" --kind <k>`. A blocked card must always name its blocker."
)

#: THE PROSE REVISION - the fix for STALENESS, not for the prose (card t_d152a4c9).
#:
#: Both carriers below embed instructive prose (the corrected CLI moves), and a deviation row
#: is DURABLE and PUBLIC. The rows the first live probe minted (t_35fe9e40, t_ae48ce83,
#: 2026-09-27) keep the PRE-FIX text forever - `hermes kanban add` and a `block --reason` flag,
#: neither of which exists (re-verified live 2026-10-02: `hermes kanban add` answers "'add' is
#: not a `hermes kanban` command", and `block`'s reason is POSITIONAL). Nothing on those rows
#: said which revision of the prose they were written from, so a stale row was
#: indistinguishable from a current one, and a lane that read one learned two commands that do
#: not exist.
#:
#: So the prose stays instructive - it is what makes the reprimand actionable - and every
#: emission now STAMPS the revision it was written from:
#:
#:   * ``created_blocked_refusal_message`` (the refusal every surface returns verbatim), and
#:   * ``file_born_blocked_deviation`` (the deviation row body),
#:
#: both carry :data:`BORN_BLOCKED_PROSE_MARKER`, and :func:`born_blocked_prose_revision` reads
#: it back. UNMARKED TEXT IS THE r1 GENERATION - minted before this stamp existed - and reads
#: as ``None``: stale by construction, never silently "current". A marker naming a DIFFERENT
#: revision is equally detectable, so a row that outlives a prose change is self-identifying
#: rather than quietly wrong.
#:
#: BUMP THE REVISION whenever ``BORN_BLOCKED_ALTERNATIVES`` (or any other prose either carrier
#: embeds) changes. An enforcement record is EVIDENCE and is never rewritten in place: the two
#: pre-fix rows were ANNOTATED in thread with the corrected commands, exactly as this constant
#: expects the next stale generation to be.
BORN_BLOCKED_PROSE_REVISION = "r2"

#: The rendered marker, parsed back by :func:`born_blocked_prose_revision`.
BORN_BLOCKED_PROSE_MARKER = f"[born-blocked prose revision: {BORN_BLOCKED_PROSE_REVISION}]"

_BORN_BLOCKED_PROSE_RE = re.compile(r"\[born-blocked prose revision: (r\d+)\]")


def born_blocked_prose_revision(text: Optional[str]) -> Optional[str]:
    """The prose revision a refusal message or deviation body was written from.

    ``None`` means the text carries no marker at all - the r1 (pre-stamp) generation, i.e.
    stale by construction. A marker naming a DIFFERENT revision is equally detectable: the
    prose may have moved (a verb repaired, a flag dropped) since that text was written, so a
    reader must never treat it as current. This is the whole point of the stamp - an
    enforcement record outlives the revision that wrote it (card t_d152a4c9).
    """
    if not text:
        return None
    match = _BORN_BLOCKED_PROSE_RE.search(text)
    return match.group(1) if match else None


def born_blocked_prose_is_current(text: Optional[str]) -> bool:
    """True only when ``text`` carries THIS revision's marker (see the constant above)."""
    return born_blocked_prose_revision(text) == BORN_BLOCKED_PROSE_REVISION


class CreatedBlockedRefused(ValueError):
    """``initial_status='blocked'`` was refused by the create seam.

    A ``ValueError`` so every surface reports it as a validation refusal (the
    ``kanban_create`` tool prints ``kanban_create: <message>``; the CLI funnels the same
    text) rather than an internal error.

    Attributes: ``lane`` (the naming creator), ``title`` (the refused title),
    ``deviation_task_id`` (the public deviation row filed against the lane) and
    ``deviation_error`` (why filing it failed - the refusal never depends on it).
    """

    def __init__(self, message, *, lane="", title="",
                 deviation_task_id=None, deviation_error=None):
        super().__init__(message)
        self.lane = lane or ""
        self.title = title or ""
        self.deviation_task_id = deviation_task_id
        self.deviation_error = deviation_error


def created_blocked_refusal_message(
    title, lane, deviation_task_id=None, deviation_error=None,
) -> str:
    """The refusal text; returned verbatim by every surface (tool, CLI, kernel)."""
    who = lane or "an unidentified caller"
    lines = [
        f"refused: a card is never created blocked (initial_status='blocked'). "
        f"Attempted title {title!r}, creator {who}. NOTHING WAS CREATED - no task row, "
        f"no event, and no blocked event carrying reason='initial_status'.",
        "Why: a born-blocked card carries no block_kind and no reason, so it hides the "
        "work from the dispatcher and from every reader, and the flag does not even hold - "
        "the next recompute_ready() promotes it, leaving a card whose status lies. "
        "Operator ruling 2026-09-27; design record platform-stl card t_5c89c04e.",
        BORN_BLOCKED_ALTERNATIVES,
    ]
    if deviation_task_id:
        lines.append(
            f"Enforcement: the attempt is escalated and the deviation is filed on this "
            f"board against the creating lane by name - task {deviation_task_id}. "
            f"Fix the call, not the guard."
        )
    else:
        lines.append(
            "Enforcement: the deviation row could NOT be filed"
            + (f" ({deviation_error})" if deviation_error else "")
            + " - the refusal stands regardless; report this to platform-stl."
        )
    lines.append(
        f"{BORN_BLOCKED_PROSE_MARKER} - the revision of the enforcement prose above. "
        f"A message or deviation row that carries an OLDER marker, or none at all, is "
        f"STALE: check a command against `hermes kanban --help` before following it."
    )
    return " ".join(lines)


def _born_blocked_deviation_key(board, lane, title) -> str:
    """One durable deviation row per distinct violation - never a card per retry."""
    from hashlib import sha1

    digest = sha1((title or "").strip().encode("utf-8")).hexdigest()[:12]
    return f"born-blocked-refused:{board or ''}:{lane or 'unknown'}:{digest}"


def file_born_blocked_deviation(
    conn: sqlite3.Connection, *, board: Optional[str] = None, lane: Optional[str] = None,
    title: str = "", tenant: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """File the public deviation row for a refused born-blocked attempt.

    Returns ``(task_id, error)`` - exactly one of which is set. Never raises: the
    refusal must not depend on its own side effect, so a filing failure is carried in
    the refusal message (and on the exception) instead of being swallowed or turning
    the refusal into an error.
    """
    who = (lane or "").strip()
    assignee = _canonical_assignee(who) if who else None
    if not assignee:
        # An unidentified caller still gets a public row; the ops head owns the chase.
        assignee = "default"
    attempt = " ".join((title or "").split())[:120] or "(untitled)"
    body = "\n".join([
        "**Enforcement record - a born-blocked card was REFUSED at the create path.**",
        "",
        f"* **creator lane (by name):** `{who or 'unidentified caller - row filed to default'}`",
        f"* **attempted title:** `{attempt}`",
        "* **enforcement action:** REFUSED. Nothing was created - no task row, no `created` "
        "event, and no `blocked` event carrying `reason='initial_status'`.",
        "* **standard:** a card is never created blocked (`initial_status='blocked'`); "
        "operator ruling 2026-09-27, design record platform-stl `t_5c89c04e`.",
        f"* **what to do instead:** {BORN_BLOCKED_ALTERNATIVES}",
        f"* **enforcement prose revision:** `{BORN_BLOCKED_PROSE_REVISION}` "
        f"{BORN_BLOCKED_PROSE_MARKER} - the revision of the enforcement prose this row was "
        f"written from. A row carrying an OLDER marker, or none at all, is STALE - check a "
        f"command against `hermes kanban --help` before following it (the two pre-fix rows "
        f"`t_35fe9e40` and `t_ae48ce83` are annotated in thread for exactly this reason).",
        "",
        "This row is the escalation of the attempt AND the public reprimand: filed on the "
        "board the attempt targeted, against the creating lane by name, in the open.",
        "",
        "The lane owns the correction: (a) the **fix** - a parent edge for waiting, and "
        "`hermes kanban block <id> \"<reason>\" --kind <k>` for a real block, never a "
        "born-blocked card; "
        "(b) the **cost** - a born-blocked create is refused, and repeats collapse onto this "
        "same row; (c) the **acceptance** - reply on this card naming which of the two moves "
        "the lane used, and re-run the call.",
    ])
    try:
        tid = create_task(
            conn,
            title=f"{BORN_BLOCKED_DEVIATION_PREFIX}: {who or 'unidentified lane'} "
                  f"attempted {attempt!r}",
            body=body,
            assignee=assignee,
            board=board,
            tenant=tenant,
            created_by=BORN_BLOCKED_GUARD_IDENTITY,
            idempotency_key=_born_blocked_deviation_key(board, who, title),
        )
    except Exception as exc:  # the record's failure must never replace the refusal
        return None, f"{type(exc).__name__}: {exc}"
    return str(tid), None


def _refuse_created_blocked(
    conn: sqlite3.Connection, *, board: Optional[str], lane: Optional[str], title: str,
    tenant: Optional[str] = None,
) -> CreatedBlockedRefused:
    """File the deviation, then build the refusal. The refusal is unconditional."""
    deviation_id, deviation_error = file_born_blocked_deviation(
        conn, board=board, lane=lane, title=title, tenant=tenant)
    return CreatedBlockedRefused(
        created_blocked_refusal_message(title, lane, deviation_id, deviation_error),
        lane=lane or "", title=title or "",
        deviation_task_id=deviation_id, deviation_error=deviation_error,
    )


# THE ABOVE-TRANCHE GUARD (operator ruling 2026-09-28; design record platform-stl t_6ce41549)
#
# The operator's asks and SEVs hold the RESERVED TOP TRANCHÉ (kanban_priority_policy:
# TRANCHE_FLOOR..TRANCHE_TOP, and MAX_PRIORITY == TRANCHE_TOP). Nothing else is admitted above
# them, and no row anywhere carries a value above the ceiling. Two doors broke that, both of
# them board-independent, so the guard is board-independent too (neither of them is gated on a
# board's ``priority_policy``, which is exactly why every board was open):
#
#   * the FILING door: ``create_task`` clamped an out-of-domain request to the ordinary edge
#     only on a board that carries a policy; on a board with none the value landed verbatim;
#   * the RE-RANK door: ``edit_task --priority 1100000`` was likewise inert, and it is how the
#     class actually got created (measured 2026-09-28: 52 rows above the ceiling on ``defcon``,
#     47 of them at 1100000, plus 1 on ``ops`` - 10 of the 52 carry a live designation, none
#     carries an ask stamp, so the operator's asks were being outranked by plain lane work).
#
# The predicate is the card's OWN MARKER, read deterministically and never inferred from a
# value. A card that carries none may not claim a value above the ceiling; a card that carries
# one is the operator's ask (or a declared SEV1) and is never refused - it is landed at the top
# of the scale, TRANCHE_TOP, because the scale still ends there.
#
# The repair half is ``demote_above_tranche`` below: a row already above the ceiling is LOWERED
# (into its class ceiling) rather than argued with, board-locally, once per dispatcher tick.
# ---------------------------------------------------------------------------------------------

def _resolve_operator_ask(conn: sqlite3.Connection, *, board: Optional[str] = None,
                          parents: Any = (), body: Optional[str] = "",
                          serves: Any = None) -> Any:
    """The :class:`~hermes_cli.kanban_register.AskRef` a filing is stamped with, or ``None``.

    Reads only what the caller and THIS board can answer: an explicit ``serves``, the
    worker-session env, the body's own stamp, then the parents - never another board's
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
    except Exception as exc:  # pragma: no cover - defensive; a filing outranks a stamp
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


def operator_ask_event(ask_ref, ask_pair: Optional[tuple[str, str]]) -> dict:
    """The ``created`` event fields recording the ask, or ``{}`` on a card with none."""
    if ask_ref is None:
        return {}
    if ask_pair:
        return {"operator_ask": f"{ask_pair[0]}/{ask_pair[1]}", "operator_ask_source": ask_ref.source}
    if ask_ref.unresolved:
        return {"operator_ask_unresolved": ask_ref.unresolved}
    return {}


#: The identity every deviation row carries - the guard, never the filer.
ABOVE_TRANCHE_GUARD_IDENTITY = "kanban-priority-guard"

#: Prefix of the public deviation rows this guard files (one per distinct violation).
ABOVE_TRANCHE_DEVIATION_PREFIX = "PRIORITY OVER-CLAIM (above the reserved tranche)"

#: A DECLARED SEV1 line, byte-identical in convention to the fleet's band module
#: (``yaan-platform/scripts/kanban_priority_bands.py::SEV1_DECLARATION_RE``): a labelled line,
#: never a value - an undeclared card sitting at the top is an over-claim, not an SEV.
#:
#: The two alternations are DATA because the storage guard has to read the same line in SQL
#: (``_sql_tranche_entitlement``): SQLite has no regular expressions, so the guard's test is a
#: second reading of this one line. They are pinned against each other by
#: ``tests/hermes_cli/test_kanban_above_tranche_refusal.py::
#: test_the_storage_guard_admits_exactly_the_markers_the_doors_admit``, because a guard that
#: disagrees with the doors is how the whole ``defcon`` board stopped dispatching on
#: 2026-10-01 (the guard refused the repair pass's own write and the tick died with it).
SEV1_LABELS = ("severity", "sev", "critical", "priority[- ]class")
SEV1_VALUES = ("sev[- ]?1", "critical")

SEV1_DECLARATION_RE = re.compile(
    r"(?im)^[ \t>*\-]*(%s)[ \t]*[:=][ \t]*(%s)\b"
    % ("|".join(SEV1_LABELS), "|".join(SEV1_VALUES)))

#: The SEV1 line as the STORAGE GUARD reads it (``_sql_tranche_entitlement``): upper-cased
#: prefixes and values, in the same order. Fewer terms than the regex's alternatives because GLOB
#: is a wildcard match - ``SEV`` reaches ``SEVERITY`` through the wildcard after it, and
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
    if sev1_declared("%s\n%s" % (title or "", body or "")):
        return "sev1"
    return ""


def tranche_entitlement(
    conn: sqlite3.Connection, *, board: Optional[str] = None, task_id: Optional[str] = None,
    title: Optional[str] = "", body: Optional[str] = "",
) -> str:
    """The card's OWN evidence that it may hold a reserved-tranche priority - ``""`` if none.

    THE PREDICATE THE STORAGE GUARD IMPLEMENTS IN SQL (see ``_sql_tranche_entitlement``), and the
    one every door that may WRITE INTO the band has to read, because door 3 aborts the write
    otherwise and an abort inside the dispatcher's reclaim phase takes the whole board's
    dispatching down with it (measured 2026-10-01: ``defcon`` spawned nothing for 6.5 hours).

    Why the card's OWN evidence, and nothing inherited. R2's marker is "the card's own evidence:
    an operator-ask ref, a live designation, or a declared SEV1 line", and a database trigger can
    only read the row it is about to write: it cannot walk a parent chain, cannot resolve a
    register reference that lives on another board, and cannot re-derive anything. The one
    inherited form the wider resolver (``above_tranche_marker``) also accepts - an ask inherited
    from a parent - is materialised on the card at birth: the create seam stamps every filing
    that resolved an ask, from any source, in the same INSERT (``apply_stamp``; see
    ``create_task``). A row that carries no stamp of its own therefore carries no ask, and door 2
    and the repair pass no longer treat one as tranche-entitled - they answer what the storage
    layer will accept, so the two can never disagree about the same row.

    Returns the marker's NAME, in the resolver's order, so the record says which evidence
    admitted the row: ``"operator-ask-stamp"``, ``"designation"`` or ``"sev1"``.
    """
    if _ASK_STAMP_RE.search(body or ""):
        return "operator-ask-stamp"
    if task_id and is_priority_designated(conn, task_id, board=board):
        return "designation"
    if sev1_declared("%s\n%s" % (title or "", body or "")):
        return "sev1"
    return ""


def _sql_tranche_entitlement(ident: str, title: str, body: str) -> str:
    """The storage layer's reading of :func:`tranche_entitlement`, over one row.

    ``ident`` / ``title`` / ``body`` are SQL expressions naming the row's columns
    (``NEW.id`` / ``NEW.title`` / ``NEW.body`` inside a trigger; bound parameters in the test that
    pins this against :func:`tranche_entitlement`).

    Two readings of one line, and they are not the same text, so the SHAPE matters:

    * the register's stamp is read by its KEYWORD (``INSTR(body, 'Operator-ask:')``). That is a
      superset of the register's own line regex - it does not require the line to start with the
      keyword - and it is a superset in the direction that matters: a guard may admit MORE than
      the doors write (the evidence it reads is text in the card's own row, so whoever can write
      the row can write the stamp line too), and may never admit LESS, because less is an abort
      inside the dispatcher's reclaim phase and an aborted tick dispatches nothing at all
      (measured 2026-10-01: 6.5 h, 452 ready cards, zero spawns on ``defcon``).
    * the declared SEV1 line is read the same way, as ``label ... [: or =] ... value`` with only
      the ORDER required. The line-anchored form is NOT expressible here: GLOB's ``[...]`` class
      has no zero-repetition form (``[ \\t]*`` in GLOB means "one space or tab, then anything"),
      so "a line that begins with optional indentation" cannot be written at all - and the live
      boards do carry an indented declaration (measured 2026-10-02: 1 of 47 is
      ``'    Severity: SEV1'``), which the anchored form would miss and the pass would then abort
      on.
    """
    nl = "char(10)"
    framed = "UPPER(COALESCE(%s, '') || %s || COALESCE(%s, '') || %s)" % (title, nl, body, nl)
    word_end = "[^A-Za-z0-9_]*"
    designation = ("EXISTS (SELECT 1 FROM priority_designations d WHERE d.task_id = %s "
                   "AND d.revoked_at IS NULL)" % ident)
    stamp = "INSTR(COALESCE(%s, ''), 'Operator-ask:') > 0" % body
    sev1 = " OR ".join(
        "%s GLOB ('*%s*[:=]*%s%s')" % (framed, label, value, word_end)
        for label in _SQL_SEV1_LABELS for value in _SQL_SEV1_VALUES)
    return "((%s) OR (%s) OR (%s))" % (stamp, designation, sev1)


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
            "reason": "carries the %s marker: admitted into the reserved tranche at its top "
                      "(%d); the scale ends there" % (marker, ceiling),
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
        raise ValueError("no such task %s on this board" % task_id)
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
    """
    policy = _policy_module()
    slug = board or board_for_connection(conn) or ""
    ceiling = int(policy.MAX_PRIORITY)
    ordinary = int(policy.ORDINARY_MAX)
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
                        "above the reserved tranche: %s" % (
                            "marker %s - lowered to the tranche top" % marker if marker
                            else "no operator-ask/SEV marker - lowered into the ordinary domain")),
                })
        except sqlite3.Error as exc:
            # Not swallowed: the row is returned with the refusal and logged at ERROR, and the
            # rest of the pass continues (one unrepairable row must not strand every other one).
            detail = "%s: %s" % (type(exc).__name__, exc)
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
) -> str:
    """Create a task (optionally under ``parents``); returns its id.

    Status: ``ready`` unless a parent is not ``done`` (``todo``); ``triage=True``
    forces ``triage``; ``initial_status="blocked"`` is REFUSED - a card is never
    created blocked (see ``created_blocked_refusal_message`` and the guard section
    above this function). Use a parent edge to wait, or ``block_task`` with a kind to
    park a real blocker.
    ``idempotency_key``: an existing non-archived task with the key is returned
    instead of a duplicate. ``max_runtime_seconds``: cap before the dispatcher
    SIGTERMs and re-queues. ``model_override``/``provider_override`` pin the
    worker model (provider requires model); ``reasoning_effort`` is independent.
    ``creator_task_id``: inherit durable session/subscriptions independently of
    dependency edges; an explicit ``session_id`` still wins.
    ``project_source_task_id``: cross-profile fallback when ``project_id`` is not
    in the active profile's projects.db — see ``_resolve_project_link``.
    ``workspace_kind=None`` (omitted) inherits a project-scoped board's project;
    an explicit ``"scratch"`` or ``project_id=""`` is a request for no project.
    ``serves``: the operator ask this card is filed in service of, as ``<register>``
    or ``<register>/<ask>`` - see ``hermes_cli/kanban_register.py``. The card is
    stamped in its body at this seam (the one moment every filing surface reaches);
    omitting it still inherits an ask from the body, the worker's session env or the
    parents, in that order.
    """
    from hermes_cli.kanban_db_graph import initial_task_state, inherit_creator_origin
    from hermes_cli.kanban_pr_acceptance import needs_repository_checks, validate_contract
    from hermes_cli.kanban_register import apply_stamp
    from hermes_cli.kanban_consent_gate import (
        ConsentRefused, evaluate as _consent_evaluate, refusal_message as consent_refusal_message,
    )

    completion_contract = validate_contract(completion_contract)
    if needs_repository_checks(completion_contract):
        # Authoring hint: a checks-backed contract binds every completion to GitHub
        # evidence, and one whose repository requires no checks can never be satisfied
        # (complete_task parks the card rather than looping the worker).
        _log.warning(
            "completion_contract %s requires repository-required CI checks; a repository "
            "with none can never satisfy it (use local-only, or `hermes kanban "
            "set-contract` to release it)", completion_contract,
        )
    model_override, provider_override = _validate_model_override(model_override, provider_override)
    reasoning_effort = normalize_reasoning_effort(reasoning_effort)
    assignee = _canonical_assignee(assignee)
    if not title or not title.strip():
        raise ValueError("title is required")
    # Admission door of the consent gate (ruling, card t_e31d9241): a card that
    # ASSERTS operator consent with no reference behind it is refused here,
    # before any row is written -- the claim is a false statement in the record
    # and every downstream reader (dispatcher, lanes, disposition sweep) acts on
    # it. A card that merely DECLARES itself consent-gated is filable: proposing
    # work is legal, and the RUN door holds it (kanban_db_dispatch).
    _consent = _consent_evaluate(title, body)
    if _consent.trigger == "claim" and _consent.refused:
        raise ConsentRefused(consent_refusal_message(None, _consent))
    if initial_status not in VALID_INITIAL_STATUSES:
        raise ValueError(f"initial_status must be one of {sorted(VALID_INITIAL_STATUSES)}")
    if initial_status == CREATED_BLOCKED_TOKEN:
        # The seam every caller reaches: refuse BEFORE any write (no task row, no event,
        # no created-blocked regression signature), escalate the attempt, file the
        # deviation against the creating lane by name, then raise. Nothing is inserted
        # for the refused attempt itself - the deviation row is a separate, deliberate
        # public record (see the guard section above ``create_task``).
        raise _refuse_created_blocked(
            conn, board=board, lane=created_by, title=title, tenant=tenant,
        )
    # A project-scoped board anchors every new task to its project's repo
    # (deterministic worktree + branch) without each surface repeating it.
    # An explicit ``scratch`` (or ``project_id=""``) is a request for no project:
    # it must not be upgraded to a worktree in the board's repo (#106342).
    if project_id is None and workspace_kind != "scratch":
        try:
            project_id = (_board_meta_for(board).get("project_id") or "").strip() or None
        except Exception:
            pass
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

    project_id, project_obj, project_repo, workspace_kind = _resolve_project_link(
        conn, project_id, project_source_task_id, workspace_kind, workspace_path
    )
    parents = tuple(p for p in parents if p)
    skills_list = _normalize_task_skills(skills)

    # Idempotency check BEFORE the write txn (no lock held); a concurrent-create
    # race may insert twice, the next lookup stabilises on the newest.
    if idempotency_key:
        row = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ? "
            "AND status != 'archived' "
            "ORDER BY created_at DESC LIMIT 1", (idempotency_key,),
        ).fetchone()
        if row:
            return row["id"]

    # THE BIRTH SEAM. A board-scoped policy applied here cannot be routed around: the tool
    # verb, the CLI, a decomposer and another lane's agent all reach this INSERT.
    requested_priority = int(priority)
    priority, policy_provenance = _apply_board_priority_policy(
        priority, assignee=assignee, board=board, title=title, body=body,
    )

    # THE OPERATOR-ASK SEAM (card t_8ca4b5a0, operator ask 2026-09-27). Applies at the
    # SAME moment and for the SAME reason as the priority policy above: every filing
    # surface reaches this INSERT, so a reference stamped here cannot be routed around
    # by a caller that forgot - which is the whole point, because the register tree's
    # failure mode was exactly "nobody remembered". A worker inherits the ask its own
    # card serves (the dispatcher exports it), a child inherits its parents', a
    # redirect/decomposition inherits the body's, and a filer may name one outright.
    # With no ask in play this is inert: the body goes in byte-identical and the created
    # event keeps exactly the payload it carried before. Resolution reads only THIS
    # board's connection (parents and the board's designation); a reference that names
    # another board is taken at face value and reported by the roll-up.
    ask_ref = _resolve_operator_ask(
        conn, board=board or board_for_connection(conn), parents=parents, body=body,
        serves=serves,
    )

    # THE ABOVE-TRANCHE DOOR (operator ruling 2026-09-28, card t_6ce41549). Applies at the same
    # seam, on the REQUESTED value and for EVERY board.
    priority, above_tranche_record = apply_above_tranche_guard(
        conn, requested_priority, priority, board=board, lane=created_by, title=title,
        body=body, ask_ref=ask_ref, tenant=tenant,
    )
    policy_provenance = _merge_policy_record(policy_provenance, above_tranche_record)

    now = int(time.time())

    # Only persistent kinds inherit the board ``default_workdir``: a scratch
    # task inheriting it would point cleanup at the user's source tree.
    if workspace_path is None and project_repo is None and workspace_kind in {"dir", "worktree"}:
        board_default = _board_meta_for(board).get("default_workdir")
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
                ask_pair = ask_ref.for_card(task_id) if ask_ref is not None else None
                card_body = apply_stamp(body, *ask_pair) if ask_pair else body
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
                        "skills": list(skills_list) if skills_list else None,
                        "goal_mode": bool(goal_mode) or None,
                        "model_override": model_override,
                        "provider_override": provider_override,
                        # The policy's own record, verbatim, and only on a card it moved: an
                        # unchanged card and every board without a policy keep the event
                        # payload they have always written.
                        **(operator_ask_event(ask_ref, ask_pair)),
                        **({"priority_policy": policy_provenance} if policy_provenance else {}),
                    },
                )
                if task_status == "blocked":
                    _append_event(
                        conn,
                        task_id,
                        "blocked",
                        {"reason": "initial_status", "status": "blocked", "actor": created_by or "user"},
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

    Nothing here may fail a filing. The policy is consulted through
    ``kanban_priority_policy.priority_for_create``, which answers an unusable policy with
    the caller's value plus an ``unavailable`` record instead of an exception: a broken
    policy must be VISIBLE, not fatal. The numbers (lane ordering, the band table, the
    clauses) belong to the policy module; this module owns the moment and the seam.
    """
    from hermes_cli import kanban_priority_policy as policy

    try:
        spec = _board_meta_for(board).get(policy.POLICY_KEY)
    except Exception:
        # A board whose metadata cannot be read has no policy to honour; the
        # ``project_id``/``default_workdir`` reads below degrade the same way.
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
    return policy.clamp_to_domain(verdict.applied, verdict.record)


# --- the priority DOMAIN: the storage guard and the designation door ----------------------
#
# A card is filed inside the ordinary domain (R1); a wired board REFUSES a re-rank outside it
# (door 2); a raw SQL write into the reserved tranche is refused by a trigger (door 3); and
# ``hermes kanban defcon`` is the one writer that may put a card there (door 4). The numbers
# live in ``kanban_priority_policy`` - this block owns only WHERE they are enforced.

#: The board-level storage guard's triggers, by name. Per-DB and opt-in: they exist on a board
#: only while that board carries a ``priority_policy`` (see ``sync_priority_tranche_guard``).
PRIORITY_TRANCHE_TRIGGERS = (
    "tasks_priority_tranche_guard_insert",
    "tasks_priority_tranche_guard_update",
)


def _policy_module():
    from hermes_cli import kanban_priority_policy as policy

    return policy


def priority_tranche_trigger_ddl(floor: Optional[int] = None) -> tuple:
    """DDL for the storage guard: refuse a reserved-tranche priority, and a below-floor one.

    The bound lives in the DATABASE rather than in a caller because the fleet's own authoring
    skill documents raw SQL as its re-rank lever - a kernel-level bound alone would be advice.
    Opt-in per board, applied by the same wiring step that writes ``priority_policy``, so an
    unwired board and every test board stay byte-identical.

    ``floor`` is a board's floor (``kanban_priority_policy.board_floor``) and adds one clause
    to each trigger: a write that lands a card BELOW the floor is refused too. Two properties
    of that clause are deliberate and both are visible in the DDL below:

    * it fires on a CHANGE only (``NEW.priority <> OLD.priority``), because a no-op write that
      re-sets a value already below the floor must not abort - a generic field-update path on a
      pre-existing below-floor card would otherwise break, which is exactly the state a board
      is in while its tail is being drained;
    * it is BAKED IN at arm time, so a board whose floor changed is re-armed by its wiring
      action (``sync_priority_tranche_guard``) rather than by a redeploy of the module it reads
      - a stored trigger cannot re-read a file. ``floor is None`` renders the DDL this function
      has always rendered, byte for byte.

    The tranche clause's PREDICATE is the marker set the doors read
    (``tranche_entitlement``): the value is refused unless the row carries its OWN evidence -
    a live designation, the register's stamp line, or a declared SEV1 line. It used to read the
    designation ledger alone, which is the narrower reading, and that disagreement is what took
    the whole ``defcon`` board's dispatching down on 2026-10-01: the repair pass lowered a
    marker-carrying row to the tranche top, the guard refused its own class's write, and the abort
    propagated out of the reclaim phase before any spawn.
    """
    policy = _policy_module()
    live = (
        "NEW.priority BETWEEN %d AND %d AND NOT (%s)"
        % (policy.TRANCHE_FLOOR, policy.TRANCHE_TOP,
           _sql_tranche_entitlement("NEW.id", "NEW.title", "NEW.body"))
    )
    message = ("priority %d-%d is DESIGNATED, never requested: use 'hermes kanban defcon "
               "designate'" % (policy.TRANCHE_FLOOR, policy.TRANCHE_TOP))
    # The message is prose and carries a quote (the verb the caller should run); a SQL string
    # literal ends at the first one, so double it before it goes into the DDL.
    message = message.replace("'", "''")
    change = ""
    insert_floor = ""
    if floor is not None:
        # A CHANGE guard, not a state guard: a no-op write that re-sets a value already below
        # the floor must not abort, or every generic field-update path on a pre-existing
        # below-floor card would break while that board's tail is being drained.
        #
        # The two arms are INDEPENDENT and each is parenthesised: ``A AND B OR C`` parses as
        # ``(A AND B) OR C`` (AND binds tighter), which is the intent - but only because no value
        # can be both inside the tranche band and below the floor. Written out, the arms cannot
        # re-associate if a later clause is added, and a reader does not have to know SQLite's
        # precedence to see that the floor clause is not scoped by the tranche test.
        change = "(NEW.priority <> OLD.priority AND NEW.priority < %d)" % int(floor)
        # An INSERT has no OLD row, so there the floor clause is a state clause: a card may not
        # be born below the floor either.
        insert_floor = "(NEW.priority < %d)" % int(floor)
        # Both clauses sit in ONE trigger per event, so the refusal has to say WHICH one fired:
        # a value inside the reserved tranche is the designation message, anything else is the
        # floor's. (A caller that read the wrong remedy out of a refusal is worse off than one
        # that read no remedy at all.) ``floor is None`` renders the literal this function has
        # always rendered, byte for byte.
        floor_message = ("priority is below this board's floor %d: no card may sit there - file "
                         "it on its own board, or use the designation door if it must outrank "
                         "this board" % int(floor))
        message = ("CASE WHEN NEW.priority BETWEEN %d AND %d THEN '%s' ELSE '%s' END"
                   % (policy.TRANCHE_FLOOR, policy.TRANCHE_TOP, message,
                      floor_message.replace("'", "''")))
    else:
        # No floor: the argument is the tranche literal, quoted exactly as before.
        message = "'%s'" % message
    body = "BEGIN\n  SELECT RAISE(ABORT, %s);\nEND;" % message
    if floor is not None:
        when_insert = "(%s) OR %s" % (live, insert_floor)
        when_update = "(%s) OR %s" % (live, change)
    else:
        when_insert = when_update = live
    return (
        "CREATE TRIGGER IF NOT EXISTS %s\nBEFORE INSERT ON tasks\nFOR EACH ROW\nWHEN %s\n%s"
        % (PRIORITY_TRANCHE_TRIGGERS[0], when_insert, body),
        "CREATE TRIGGER IF NOT EXISTS %s\nBEFORE UPDATE OF priority ON tasks\nFOR EACH ROW\n"
        "WHEN %s\n%s" % (PRIORITY_TRANCHE_TRIGGERS[1], when_update, body),
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
    was first armed with. Dropping first is what makes ``set-priority-policy`` idempotent AND
    able to follow a changed floor.
    """
    _assert_not_delegated_child_mutation(kanban_db_path(board=board))
    floor = _board_priority_floor(board)
    with _connect_board(board) as conn:
        for name in PRIORITY_TRANCHE_TRIGGERS:
            conn.execute("DROP TRIGGER IF EXISTS %s" % name)
        for statement in priority_tranche_trigger_ddl(floor):
            conn.execute(statement)
        return priority_tranche_guards(conn)


def disarm_priority_tranche_guard(board: Optional[str] = None) -> list:
    """Drop the storage guard from *board* - the unwired board's behaviour, exactly."""
    _assert_not_delegated_child_mutation(kanban_db_path(board=board))
    with _connect_board(board) as conn:
        for name in PRIORITY_TRANCHE_TRIGGERS:
            conn.execute("DROP TRIGGER IF EXISTS %s" % name)
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

    THE DOMAIN HALF IS UNCONDITIONAL (operator standard, card t_ecbfb34b): the domain is the
    KERNEL's, not a board's, so a value outside ``ORDINARY_MIN..ORDINARY_MAX`` cannot be stored
    whether or not the board carries a ``priority_policy``. The original gating left every
    unwired board open, which is how ``defcon`` came to hold 101 rows at 1100000.

    ABOVE THE CEILING is the CEILING DOOR's call, not this one's.
    ``_refuse_above_tranche_rerank`` runs first on the same edit and refuses every card that
    carries no marker, so a value over
    ``MAX_PRIORITY`` arriving here has already been let through by the marker rule ("only the
    marker exempts a card"): the lift of a designated or stamped card, which the repair pass
    (``demote_above_tranche``) then brings back into the tranche. Re-deciding it here would leave
    that exemption unreachable, since no marked value above the ceiling could ever pass both
    doors. What this door owns, and still refuses, is the ordinary card placed over the asks and
    a reserved-band value reached WITHOUT a marker: a card that carries one (a live designation,
    the register's ask stamp, or a declared SEV1) may be placed inside
    ``TRANCHE_FLOOR..TRANCHE_TOP`` - the same predicate the create seam and the repair pass read.

    The FLOOR stays the BOARD's bound, and stays gated on its policy: it is the fleet band
    table's clause, not the scale's.
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
    if not policy.in_ordinary(value):
        # Above the ceiling the tranche door has already ruled: ``_refuse_above_tranche_rerank``
        # ran first on this same edit and refused every card that carries no marker, so a value
        # over ``MAX_PRIORITY`` here IS a marker-carrying card being lifted - the exemption the
        # standard grants ("a designated or stamped card may still be lifted"), recorded on the
        # card and lowered again by ``demote_above_tranche`` when the sweep runs. Refusing it
        # here as well would make that exemption unreachable, because no marked value above the
        # ceiling can pass both doors. The ceiling itself is not re-decided by this door.
        if value > int(policy.MAX_PRIORITY):
            return
        # Inside the reserved tranche, the band the asks and SEVs hold: reachable only by a card
        # carrying its OWN evidence, read by the SAME predicate the storage guard enforces
        # (``tranche_entitlement``) - not by the new value merely landing in the band, and not by
        # evidence inherited from an ancestor the trigger cannot see. Answering the guard's own
        # question is what keeps this door's "allowed" from becoming the database's "aborted".
        in_tranche = policy.in_tranche(value)
        marker = ""
        if in_tranche and task_id:
            text = _task_text(conn, task_id)
            marker = tranche_entitlement(
                conn, board=slug, task_id=task_id, title="", body=text,
            )
        if not (in_tranche and marker):
            raise policy.PriorityOutOfDomain(
                "priority %d is outside the ordinary domain (%d..%d): %s"
                % (value, policy.ORDINARY_MIN, policy.ORDINARY_MAX, policy.domain_reason(value))
            )
    if not policy.board_is_wired(spec):
        return
    floor = policy.board_floor(spec, slug or "")
    if floor is not None and value < floor:
        raise policy.PriorityOutOfDomain(
            "priority %d is below this board's floor %d: cards on %s are filed at the top "
            "band (%d..%d) - file it on its own board, or use the designation door if it must "
            "outrank them" % (value, floor, slug or "this board", floor, policy.ORDINARY_MAX)
        )


def _task_text(conn: sqlite3.Connection, task_id: str) -> str:
    """``title + body`` of an existing card, or ``""`` - the marker's own evidence."""
    row = conn.execute("SELECT title, body FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return ""
    return "%s\n%s" % ((row["title"] or ""), (row["body"] or ""))


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
    policy = _policy_module()
    _assert_not_delegated_child_mutation(kanban_db_path(board=board))
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("a designation needs a reason (it is the audit answer)")
    current = conn.execute("SELECT priority FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if current is None:
        raise ValueError("no such task %s on this board" % task_id)
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


def revoke_priority_designation(conn: sqlite3.Connection, task_id: str, *, reason: str = "",
                                now: object = None) -> Optional[dict]:
    """Leave the tranche: annotate the row, restore the card's own ordinary priority.

    ``None`` when the card carries no LIVE designation - revoking is a no-op on a card that was
    never designated, never a silent re-rank of an ordinary card. The row keeps the DESIGNATION's
    reason and authority (that is what the audit query reads); the revoke's own reason and
    authority ride the ``reprioritized`` event, so nothing about the act is lost.

    THE RESTORED VALUE IS CLAMPED (door 5). The ledger records the priority the card held when
    it was designated - whatever that was, including a value that is not legal on the board by
    the time someone revokes. On this fleet 18 of the live rows hold a RESERVED-TRANCHE value,
    hand written by raw SQL before any guard existed; writing one back would be refused by the
    armed storage guard inside the very transaction that sets ``revoked_at``, so the designation
    could never be released at all - the guard would hold the exit shut. The restore is
    therefore clamped to what the board allows AFTER the revocation: inside the ordinary domain
    (so never a tranche value), and never below a floored board's floor. When the clamp moves
    the value the correction rides the ``reprioritized`` event AND the returned row, so a
    ledger that no longer means what it says is visible rather than silent.

    A floor that cannot be READ does not stop the revoke: the domain clamp above is
    unconditional, so nothing here can write a tranche value back, and the unreadable floor is
    recorded on the event rather than swallowed. A revoke must never abort on the number the
    ledger happens to hold.

    THE GATE HALF runs after the release, in its own txn (see THE GATE INVARIANT below). The
    ledger's value is where the card RESTS, not what it may HOLD: a gate that still holds
    designated work is re-lifted by the relation, with the ``reprioritized`` gate event naming
    the child - so a revoke never leaves the board out of order, and never silently either.
    The lifts ride the returned row (``gate_lifts``) when there are any.
    """
    _assert_not_delegated_child_mutation(kanban_db_path())
    row = priority_designation(conn, task_id)

    if row is None or row["revoked_at"]:
        return None
    policy = _policy_module()
    stored = int(row["priority"])
    restore, _ = policy.clamp_to_domain(stored, None)
    floor, floor_error = None, None
    try:
        floor = _board_priority_floor(row["board"])
    except Exception as exc:
        floor_error = " ".join(str(exc).split()) or exc.__class__.__name__
    if floor is not None and restore < floor:
        restore = int(floor)
    correction = None
    if restore != stored:
        correction = {
            "stored": stored,
            "applied": restore,
            "floor": floor,
            "reason": (policy.domain_reason(stored) if not policy.in_ordinary(stored)
                       else "below the board's floor %d" % floor),
        }
    reason = (reason or "").strip()
    stamp = _designation_stamp(now)
    with write_txn(conn):
        conn.execute("UPDATE priority_designations SET revoked_at = ? WHERE task_id = ?",
                     (stamp, task_id))
        conn.execute("UPDATE tasks SET priority = ? WHERE id = ?", (restore, task_id))
        payload = {"priority": restore, "designation": "revoked", "reason": reason}
        if correction is not None:
            payload["correction"] = correction
        if floor_error is not None:
            payload["floor_unavailable"] = floor_error
        _append_event(conn, task_id, "reprioritized", payload)
    # Door 5 released the designation. The GATE half is applied immediately after, in its own
    # txn: a card that still holds designated work cannot sit in the ordinary band, so the
    # release runs first and the floor is re-applied - the two events say both happened, and
    # neither the release nor the floor can leave a stored graph out of order.
    gate_lifts = lift_gate_chain(conn, task_id, cause="revoke", board=row["board"])
    record = {
        "task_id": task_id, "board": row["board"], "restored_priority": restore,
        "stored_priority": stored, "correction": correction,
        "authority": row["authority"], "reason": row["reason"],
        "designated_at": row["designated_at"], "revoked_at": stamp, "revoke_reason": reason,
    }
    if gate_lifts:
        record["gate_lifts"] = gate_lifts
    return record

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
# WHERE IT LIVES, AND WHY NOT IN THE POLICY MODULE. ``kanban_priority_policy`` says a value is
# "decided at birth, not corrected afterwards", and the card asked for that premise to be either
# extended or corrected. It is CORRECTED, on one ground: a policy is a pure function of the FILING
# (``fn(requested, assignee, board, title, body)``) and it owns a board's BANDS. This rule needs
# the GRAPH, which no filing argument carries, and it bounds EVERY board whether or not one is
# wired - exactly as the priority DOMAIN does. So it sits with the domain, in the kernel, and the
# policy module's contract is unchanged: a policy still decides the value a card is BORN with and
# still decides nothing afterwards. The relation floor is applied AROUND the policy's verdict,
# never inside it, and the two never share a code path.
#
# THE SEAMS (relations change after birth, so one seam cannot hold this):
#   * ``_link`` - the ONE edge primitive (``create_task``, ``link_tasks`` and the triage
#     decomposer all insert edges through it): a new edge can leave the parent below the card it
#     now gates, so the child's whole ancestor chain is re-decided, nearest first.
#   * ``edit_task`` - a re-rank changes a VALUE in both directions: raising a card above its
#     parents lifts them, and a parent lowered under its own children is lifted back to its gate
#     floor in the same write, because the alternative is a stored state that breaks the
#     invariant until somebody notices.
#   * ``revoke_priority_designation`` - leaving the tranche writes the ledger's ordinary value
#     back, and a card that still gates designated work must not stay there: the release runs
#     first, the floor is applied after it, and the two events say so between them.
# Nothing else needs a seam: completing, archiving or unlinking only RELAXES the invariant - a
# closed parent constrains nobody, which is why the walk stops at one.
#
# THE LIFT IS ONE-WAY. It raises. It never lowers, never demotes the gated card and never refuses
# a filing: demoting or rejecting the child would hide the gate instead of honouring it, and
# "never demote or block the gated card" is the directive. ``min(need, MAX_PRIORITY)`` is the only
# bound - a child left above the scale's top by an unguarded raw write is capped, and the cap is
# RECORDED (``capped_from``) rather than written silently. ``demote_above_tranche`` owns that
# other half.
#
# THE RESERVED TRANCHE. A gate whose child holds 999999 must itself hold 999999, which is inside
# the band that is "designated, never requested". The lift takes the designation route the door
# already provides - ledger row FIRST (the storage guard aborts a tranche value with no live
# designation, so the order is the door's, not a choice made here), ``authority="gate-lift"`` and
# a reason naming the CHILD - and then writes the exact value, because 990000 is the
# designation's own value and the invariant needs the child's. A parent of designated work is a
# ladder case by definition; the alternative the card offered (cap at ORDINARY_MAX and report)
# would leave every reserved-band card held by a gate that reports itself as permanently
# unsatisfiable, which is the freeze the directive exists to remove. Every tranche lift is named
# in its event (``tranche``) and counted in the reconcile record, so the reserved band EXPANDING
# is never silent. ``revoke`` remains the release door: it restores the ledger's ordinary value
# and the floor is re-applied immediately after, so the invariant outlives the release.
#
# THE PASS. Seams hold every write that goes through them; nothing holds a value written around
# them (the fleet's raw-SQL re-rank lever, a restored backup). ``reconcile_gate_priorities()`` is
# the deterministic pass that normalises those and STATES its counts - it is what the board-sweep
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
    return ("gate lift: this card gates %s at %d, so it cannot rank below it (cause: %s)"
            % (gate_id, int(gate_priority), cause))


def _gate_lift_one(conn: sqlite3.Connection, task_id: str, *, need: tuple, cause: str,
                   board: Optional[str] = None) -> Optional[dict]:
    """Raise ONE card to its gate floor. Returns the lift record, or ``None`` when it is already
    there / terminal / gone.

    Never lowers (a card above its floor is left alone), never refuses, and never writes a value
    above the scale's top without recording it.
    """
    policy = _policy_module()
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
                           "error": "%s: %s" % (type(exc).__name__, exc)})
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

    This is the ONE edge primitive: ``create_task`` (parents passed at filing), ``link_tasks`` and
    the triage decomposer all arrive here, which is why the seam sits here rather than three times
    above it. The edge is written first and the lift runs in the same transaction, so the stored
    graph is never observably "an edge whose parent ranks below the card it gates"; either the
    caller's write commits with the lift or neither does.

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
        query += " ORDER BY priority DESC, created_at ASC"
    if limit:
        query += f" LIMIT {int(limit)}"
    rows = conn.execute(query, params).fetchall()
    return [Task.from_row(r) for r in rows]


def assign_task(conn: sqlite3.Connection, task_id: str, profile: Optional[str]) -> bool:
    """Assign/reassign; raises RuntimeError while the task is running under a claim."""
    profile = _canonical_assignee(profile)
    with write_txn(conn):
        row = conn.execute(
            "SELECT status, claim_lock, assignee FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row:
            return False
        if row["claim_lock"] is not None and row["status"] == "running":
            raise RuntimeError(
                f"cannot reassign {task_id}: currently running (claimed). "
                "Wait for completion or reclaim the stale lock first."
            )
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


def set_contract(
    conn: sqlite3.Connection, task_id: str, contract: str, *, reason: str,
    actor: str = "user",
) -> bool:
    """Correct a task's completion contract — the release for a wrong or unsatisfiable one.

    ``contract`` is ``local-only``, ``OWNER/REPO``, or an exact PR URL (same validation as
    create). Appends ``contract_changed {old, new, reason, actor}``; a refusal writes no
    event. ``reason`` is mandatory: re-declaring a contract changes what "green" means for
    the card, so it has to be defensible from the event log alone. Terminal cards
    (``done``/``archived``) are refused — their contract is frozen with the run that closed
    them. A delegated ``delegate_task`` child cannot reach this: ``write_txn`` refuses the
    mutation, deliberately, so a worker cannot relabel the fence holding it.
    """
    from hermes_cli.kanban_pr_acceptance import validate_contract

    value = validate_contract(contract)
    why = (reason or "").strip()
    if not why:
        raise ValueError("set_contract requires a non-empty reason")
    with write_txn(conn):
        row = conn.execute(
            "SELECT status, completion_contract FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None:
            return False
        if row["status"] in {"done", "archived"}:
            raise RuntimeError(
                f"cannot set completion contract on {row['status']} task {task_id}"
            )
        old = row["completion_contract"]
        conn.execute("UPDATE tasks SET completion_contract = ? WHERE id = ?", (value, task_id))
        _append_event(
            conn, task_id, "contract_changed",
            {"old": old, "new": value, "reason": why, "actor": actor or "user"},
        )
    notify_task_updated(conn, task_id, ("completion_contract",))
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
    """
    if parent_id == child_id:
        raise ValueError("a task cannot depend on itself")
    gated = False
    with write_txn(conn):
        missing = _missing_task_ids(conn, [parent_id, child_id])
        if missing:
            raise ValueError(f"unknown task(s): {', '.join(missing)}")
        child = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", (child_id,),
        ).fetchone()
        if child["status"] == "running" and (
            expected_child_run_id is None
            or child["current_run_id"] != expected_child_run_id
        ):
            raise ValueError(f"cannot link {parent_id} -> {child_id}: child is already running")
        if _would_cycle(conn, parent_id, child_id):
            raise ValueError(f"linking {parent_id} -> {child_id} would create a cycle")
        _link(conn, parent_id, child_id, cause="link")
        # If child was ready but parent is not yet terminal, demote child to todo
        # (archived counts as terminal, matching _parents_satisfied/recompute_ready).
        if _task_status(conn, parent_id) not in ("done", "archived"):
            cur = conn.execute(
                "UPDATE tasks SET status = 'todo' WHERE id = ? AND status = 'ready'",
                (child_id,),
            )
            gated = cur.rowcount == 1
            if gated:
                _append_event(
                    conn,
                    child_id,
                    "dependency_wait",
                    {"reason": "parent_not_done", "demoted": True, "parent": parent_id},
                )
        _append_event(
            conn,
            child_id,
            "linked",
            {"parent": parent_id, "child": child_id},
        )
        _inherit_notify_subs(conn, child_id, (parent_id,))
    return gated


def _would_cycle(conn: sqlite3.Connection, parent_id: str, child_id: str) -> bool:
    """True iff ``parent_id`` is already a descendant of ``child_id``."""
    seen = set()
    stack = [child_id]
    while stack:
        node = stack.pop()
        if node == parent_id:
            return True
        if node in seen:
            continue
        seen.add(node)
        rows = conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id = ?", (node,)
        ).fetchall()
        stack.extend(r["child_id"] for r in rows)
    return False


def unlink_tasks(conn: sqlite3.Connection, parent_id: str, child_id: str) -> bool:
    with write_txn(conn):
        cur = conn.execute(
            "DELETE FROM task_links WHERE parent_id = ? AND child_id = ?", (parent_id, child_id),
        )
        removed = cur.rowcount > 0
        if removed:
            _append_event(conn, child_id, "unlinked", {"parent": parent_id, "child": child_id})
    if removed:
        # Re-gate the child now (as complete_task/unblock_task do) instead of
        # leaving it in todo until the next tick.
        recompute_ready(conn)
    return removed


def _linked_ids(conn: sqlite3.Connection, want: str, where: str, task_id: str) -> list[str]:
    rows = conn.execute(
        f"SELECT {want} FROM task_links WHERE {where} = ? ORDER BY {want}", (task_id,)
    ).fetchall()
    return [r[want] for r in rows]


# Dependency edge removed — re-evaluate promotion eligibility for the child immediately. Matches the
# contract of complete_task and unblock_task; without this the child stays stuck in todo until the next
# dispatcher tick or a manual `hermes kanban recompute` (issue #22459).
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


def _append_event(
    conn: sqlite3.Connection, task_id: str, kind: str, payload: Optional[dict] = None, *,
    run_id: Optional[int] = None,
) -> None:
    """Insert an event row inside the caller's txn; ``run_id`` groups it by attempt (NULL = task-scoped)."""
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?)", (task_id, run_id, kind, _json_or_null(payload), int(time.time())),
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


def recompute_ready(conn: sqlite3.Connection, failure_limit: int = None) -> int:
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

    cause = "hallucinated_created_cards"

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


COMPLETION_REFUSAL_CAUSES = (
    "unknown_id",                  # no such task on this board
    "not_running",                 # status or run changed under the caller
    "parent_gate_unsatisfied",     # a parent is not terminal yet
    "acceptance_refusal",          # the PR acceptance receipt is not green
    "record_acceptance_refusal",   # the card moved while its receipt was collected
    "hallucinated_created_cards",  # created_cards names cards this worker did not create
    "deferral_not_a_child",        # deferred_children names a card that is not this card's child
    "deferral_needs_reason",       # a deferral must say WHY the child is not carrying this DoD
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


# The one release for a wrong or unsatisfiable contract, named on every refusal and park a
# contract caused: the operator is top-level, and a dispatched worker can only read its card.
SET_CONTRACT_HINT = (
    "A contract that no retry can satisfy is released by a top-level operator with "
    "`hermes kanban set-contract <task_id> local-only|OWNER/REPO|<PR URL> --reason \"...\"` "
    "(no board write from a dispatched worker — card the release instead)"
)


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

PRIORITY_PROMOTED_EVENT = "priority_promoted"
PRIORITY_DEFERRED_EVENT = "priority_deferred"


def _closure_floor_target(conn: sqlite3.Connection, parent_priority: int) -> Optional[int]:
    """The priority a completing card's DoD-carrying children may not sit below; None = stand down.

    Capped at ``policy.ORDINARY_MAX``: the reserved tranche is DESIGNATED per card and never
    inherited, so a designated parent lifts its children to the top of the ORDINARY domain.
    The cap is not taste - a wired board's storage guard ABORTS a tranche write that carries no
    designation row, and an inherited slot would take the completion down with it.

    ``None`` when even the capped floor sits below this board's own floor, or when that floor
    cannot be read: the board's floor is the stronger bound, and no priority nudge may fail the
    completion it rides. The floor is only read on a DB that actually carries the storage
    guard, so an unarmed board loads no policy file on the completion path.
    """
    from hermes_cli import kanban_priority_policy as policy

    target = min(int(parent_priority), policy.ORDINARY_MAX)
    floor: Optional[int] = None
    if priority_tranche_guards(conn):
        try:
            slug = board_for_connection(conn)
            if slug is None:
                # An armed store this DB cannot be named as: its baked floor is unreadable from
                # here, and guessing it is how a completion gets aborted.
                return None
            floor = _board_priority_floor(slug)
        except Exception:
            # An unreadable floor is the ARMING path's refusal (PolicyError there); on the
            # completion path it stands the promotion down rather than risk aborting the write.
            return None
    if floor is not None and target < int(floor):
        return None
    return target


def _gate_deferred_children(
    conn: sqlite3.Connection, task_id: str, deferred_children: Optional[Mapping[str, Any]],
) -> dict | CompletionRefusal:
    """Normalise the closing card's deferral declaration, or refuse one that declares nothing real.

    ``deferred_children`` is ``{child_id: why}``. A declaration is the ONLY way a child is held
    out of the closure floor: it is explicit, it names a card that really is this card's child,
    and it states a reason - an exemption nobody can read is not a declaration, and a low
    priority is never read as one. Runs BEFORE the write txn (like the other completion gates),
    so a refusal leaves the card exactly as it was, with an auditable event behind it.
    """
    if not deferred_children:
        return {}
    if not isinstance(deferred_children, Mapping):
        return CompletionRefusal(
            "deferral_not_a_child",
            f"deferred_children must be an object of {{child_id: why}}, got "
            f"{type(deferred_children).__name__}; a bare id list states no reason and is not a "
            f"declaration",
        )
    declared: dict[str, str] = {}
    for raw_id, raw_reason in deferred_children.items():
        child_id = str(raw_id).strip()
        if child_id:
            declared[child_id] = _first_line(str(raw_reason) if raw_reason is not None else "", 500)
    if not declared:
        return {}
    children = set(child_ids(conn, task_id))
    strangers = sorted(cid for cid in declared if cid not in children)
    reasonless = sorted(cid for cid, why in declared.items() if not why)
    if strangers or reasonless:
        with write_txn(conn):
            _append_event(
                conn, task_id, "completion_blocked_deferral",
                {"not_a_child": strangers, "without_reason": reasonless},
            )
        if strangers:
            return CompletionRefusal(
                "deferral_not_a_child",
                f"deferred_children names cards that are not children of {task_id}: "
                f"{', '.join(strangers)}; a deferral can only hold out a child this card's "
                f"completion would otherwise lift",
            )
        return CompletionRefusal(
            "deferral_needs_reason",
            f"deferred_children states no reason for {', '.join(reasonless)}; say WHY each child "
            f"does not carry {task_id}'s remaining DoD, as {{child_id: why}}",
        )
    return declared


def _promote_dod_children(
    conn: sqlite3.Connection, task_id: str, *, deferred: Mapping[str, str],
) -> list[dict]:
    """Lift every open child of *task_id* up to the closing card's own priority.

    The caller holds the completion's write txn: the promotion IS part of that write, not a
    follow-up job. Lift-only, so a child already at or above the target is untouched and a child
    is never moved above its parent; a card that carries nothing is not touched at all. Returns
    the moves, which the ``completed`` payload carries so a reader can see why a child moved.
    """
    row = conn.execute("SELECT priority FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return []
    parent_priority = int(row["priority"] or 0)
    target = _closure_floor_target(conn, parent_priority)
    if target is None:
        return []
    rows = conn.execute(
        "SELECT t.id AS child_id, t.priority AS priority FROM task_links l "
        "  JOIN tasks t ON t.id = l.child_id "
        " WHERE l.parent_id = ? AND t.status NOT IN ('done', 'archived') "
        " ORDER BY t.id",
        (task_id,),
    ).fetchall()
    moves: list[dict] = []
    for child in rows:
        child_id = child["child_id"]
        if child_id in deferred:
            continue
        current = int(child["priority"] or 0)
        if current >= target:
            continue
        payload: dict = {
            "parent": task_id,
            "parent_priority": parent_priority,
            "from": current,
            "to": target,
        }
        if target < parent_priority:
            payload["capped_at"] = target
        # Task-scoped on the CHILD: the promotion explains the child's own priority, and it is
        # the child's row that moved.
        conn.execute(
            "UPDATE tasks SET priority = ? WHERE id = ? AND priority < ?",
            (target, child_id, target),
        )
        _append_event(conn, child_id, PRIORITY_PROMOTED_EVENT, payload)
        moves.append({"child": child_id, "from": current, "to": target})
    return moves


def _record_deferred_children(
    conn: sqlite3.Connection, task_id: str, deferred: Mapping[str, str],
) -> list[dict]:
    """Record the closing card's deferral declaration on each child, inside the completion txn."""
    records: list[dict] = []
    for child_id, reason in sorted(deferred.items()):
        _append_event(conn, child_id, PRIORITY_DEFERRED_EVENT, {"parent": task_id, "reason": reason})
        records.append({"child": child_id, "reason": reason})
    return records


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
    ``deferred_children`` (``{child_id: why}``) is the closing card's EXPLICIT declaration that
    a child does NOT carry its remaining DoD. Every other open child is lifted to the closing
    card's own priority in this same transaction, and the promotion is recorded (see
    :func:`_promote_dod_children`); a declaration naming a card that is not this card's child,
    or stating no reason, is refused with the same vocabulary as every other refusal.
    Completions from non-review statuses need evidence: a stripped ``result``
    or ``summary``, or a stripped result already stored on the card. Empty or
    whitespace-only evidence raises :class:`EmptyCompletionError` after an
    auditable event. Approving a card out of ``review`` stays exempt.
    A refusal is a falsy :class:`CompletionRefusal` naming its ``cause`` (see
    :data:`COMPLETION_REFUSAL_CAUSES`) in ``detail`` terms a human can act on — an unknown
    id and an unsatisfiable contract are different problems and must not read alike. When
    the receipt proves the contract is UNSATISFIABLE (CI-backed, and the repository requires
    no checks at all) the card is parked ``blocked`` with kind ``capability`` as well as
    refused: a bare refusal only respawns the worker against a verdict no retry can change.
    """
    now = int(time.time())
    # Cheap pre-check; re-checked inside the txn to close the parent-reopen race.
    if not _parents_satisfied(conn, task_id):
        return _parent_gate_refusal(conn, task_id)
    from hermes_cli.kanban_pr_acceptance_store import prepare_acceptance, record_acceptance
    verified_cards = _gate_created_cards(conn, task_id, created_cards, summary or result)
    deferred = _gate_deferred_children(conn, task_id, deferred_children)
    if isinstance(deferred, CompletionRefusal):
        return deferred
    _gate_empty_completion(conn, task_id, result=result, summary=summary)
    metadata = _merge_completion_prose_artifacts(
        conn, task_id, metadata, summary=summary, result=result,
    )
    _gate_deploy_proof(conn, task_id, metadata)
    handoff_summary = summary if summary is not None else result
    acceptance = prepare_acceptance(conn, task_id, expected_run_id, metadata)
    if acceptance is False:
        # prepare_acceptance refuses for two different reasons; name the real one.
        status = _task_status(conn, task_id)
        if status is None:
            return CompletionRefusal("unknown_id", f"no task {task_id} on this board")
        return CompletionRefusal(
            "not_running",
            f"{task_id} is {status}: completion needs running|ready|blocked|review, and a "
            f"running card also needs the caller to own its run (expected_run_id) or --force",
        )
    if _unsatisfiable_acceptance(acceptance):
        return _park_unsatisfiable_contract(conn, task_id, acceptance)
    with write_txn(conn):
        # Hard invariant even for human review approval: a parent may have
        # reopened while this task waited.
        if not _parents_satisfied(conn, task_id):
            return _parent_gate_refusal(conn, task_id)
        if acceptance is not None and not record_acceptance(conn, task_id, acceptance):
            receipt = acceptance[1]
            if not receipt.get("ok"):
                return _acceptance_refusal(receipt)
            # Green receipt, refused write: the CARD moved (status/run/contract) mid-flight.
            return _record_acceptance_refusal(task_id)
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
        allowed, refusal, guard_sql, guard_params = _close_run_fence(
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
            return CompletionRefusal(
                "not_running",
                f"{task_id} left a completable status while it was being closed",
            )
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
            # un-owned branch admits. In the second case the run this lands on must SAY it is the
            # recovery: the crashed row is left standing as history, and this one names it (#123811).
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
        elif run_id is None:
            # No handoff fields to record, but the card's last run may be an ABANDONED attempt: a
            # reclaim closed it, not the worker, and left ``consecutive_failures`` incremented
            # against a worker that never failed. Reconcile it with an empty completion so attempt
            # history does not read "the card ran on a dead worker" beside delivered work, and so
            # the falsified counter goes with it. Guarded: it does nothing unless the run is proven
            # disowned (DISOWNED_RUN_REASONS) and no worker still holds the card.
            run_id = _synthesize_empty_completion_run(conn, task_id)
        event_summary = handoff_summary
        if prior_status == "review" and not event_summary:
            event_summary = _REVIEW_APPROVED_NOTE
        # The closure floor, INSIDE this txn: a child carrying this card's remaining DoD is
        # lifted to the card's own priority as part of the same write, and the closing card's
        # deferral declarations are recorded on the children they hold out.
        promotions = _promote_dod_children(conn, task_id, deferred=deferred)
        deferrals = _record_deferred_children(conn, task_id, deferred)
        completed_payload = _completed_event_payload(result, event_summary, verified_cards, metadata)
        if promotions:
            completed_payload["priority_promotions"] = promotions
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


def _parent_gate_refusal(conn: sqlite3.Connection, task_id: str) -> CompletionRefusal:
    """Name the open parents — a caller cannot tell a live dependency from a mistyped id."""
    blockers = unsatisfied_parents(conn, task_id)
    detail = ", ".join(f"{pid} ({status})" for pid, status in blockers) or "an unfinished parent"
    return CompletionRefusal(
        "parent_gate_unsatisfied",
        f"unsatisfied parent dependencies: {detail}; complete the parents first, or unlink a "
        f"parent that is no longer a dependency",
    )


def _acceptance_refusal(receipt: dict) -> CompletionRefusal:
    """Refuse a non-green PR acceptance receipt, naming the contract's release as well."""
    detail = (f"PR acceptance {receipt.get('classification', 'unknown')}: "
              f"{receipt.get('detail', '')} {receipt.get('recovery', '')}").strip()
    return CompletionRefusal("acceptance_refusal", f"{detail} {SET_CONTRACT_HINT}")


def _record_acceptance_refusal(task_id: str) -> CompletionRefusal:
    """The receipt was collected against a card that then moved (status/run/contract)."""
    return CompletionRefusal(
        "record_acceptance_refusal",
        f"{task_id} changed while its acceptance receipt was being collected; re-read the "
        f"card and retry, or reclaim it if a rival took the run",
    )


def _unsatisfiable_acceptance(acceptance) -> bool:
    """True when the receipt proves NO retry can pass.

    ``collect_acceptance`` reports ``missing`` both for a check that has not appeared yet
    (retryable) and for a repository that requires no checks at all. ``required == []`` is
    the second case, which is permanent for this contract: nothing the worker does can make
    a required check exist.
    """
    if acceptance is None:
        return False
    return acceptance[1].get("classification") == "missing" and acceptance[1].get("required") == []


def _park_unsatisfiable_contract(
    conn: sqlite3.Connection, task_id: str, acceptance,
) -> CompletionRefusal:
    """Record the receipt, park the card ``blocked``/``capability``, then refuse.

    A plain refusal is not terminal: the dispatcher still sees a claimable card, respawns
    the worker, and the same receipt comes back forever. Parking is what makes "this cannot
    be satisfied" durable, and the reason carries the release (``set-contract``) so the
    board itself says who can unstick it.
    """
    snapshot, receipt = acceptance
    contract = snapshot[2]
    from hermes_cli.kanban_pr_acceptance_store import _snapshot, record_acceptance

    with write_txn(conn):
        record_acceptance(conn, task_id, acceptance)
        # ``record_acceptance`` returns the receipt's ``ok`` (False for every refusal), so
        # "did the write land" is the snapshot comparison, never the return value.
        landed = _snapshot(conn, task_id) == snapshot
    if not landed:
        return _record_acceptance_refusal(task_id)
    reason = (
        f"completion contract {contract} cannot be satisfied: "
        f"{receipt.get('detail') or 'no repository-required checks are configured'} "
        f"{SET_CONTRACT_HINT}."
    )
    parked = block_task(conn, task_id, reason=reason, kind="capability")
    return CompletionRefusal(
        "acceptance_refusal",
        f"PR acceptance {receipt.get('classification', 'missing')}: "
        f"{receipt.get('detail', '')} "
        f"{'The card is parked blocked (capability) until the contract is released.' if parked else ''} "
        f"{SET_CONTRACT_HINT}",
    )


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
    """
    row = conn.execute(
        "SELECT completion_contract FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return
    contract = row["completion_contract"] if "completion_contract" in row.keys() else None
    from hermes_cli import kanban_proof_gate
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
    """Advisory post-commit scan of summary+result for unresolvable ``t_<hex>``
    references; emits ``suspected_hallucinated_references`` in its own txn so
    the completion is already durable. Never blocks."""
    scan_text = " ".join(filter(None, [summary, result]))
    if not scan_text:
        return
    phantom_refs = [p for p in _scan_prose_for_phantom_ids(conn, scan_text) if p not in set(verified_cards)]
    if phantom_refs:
        with write_txn(conn):
            _append_event(
                conn, task_id, "suspected_hallucinated_references",
                {"phantom_refs": phantom_refs, "source": "completion_summary"}, run_id=run_id,
            )


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
    goal_mode: Optional[bool] = None, goal_max_turns: Optional[int] = None,
    max_runtime_seconds: Optional[int] = None, clear_max_runtime: bool = False,
) -> bool:
    """Edit task fields, optionally backfilling a completed task's result.

    ``goal_mode``/``goal_max_turns``/``max_runtime_seconds`` set how much ROOM the card gets and are
    tri-state: ``None`` leaves the column alone, ``goal_max_turns=0`` clears it back to the default,
    and ``clear_max_runtime=True`` writes NULL (used by ``--max-runtime none``). These are the fields
    that decide whether a card dies at its iteration budget, so they need a sanctioned surface: the
    only previous way to set them was a hand-written SQL UPDATE (#card).
    """
    """Edit task fields, optionally backfilling a completed task's result."""
    if priority is not None:
        # Door 2b of the above-tranche guard (t_6ce41549): the CEILING, deliberately NOT
        # policy-gated - the hand-lift is how the class was created.
        _refuse_above_tranche_rerank(conn, task_id, priority, board=board)
        # Door 2: the ordinary DOMAIN, also unconditional as of card t_ecbfb34b. A wiring
        # gate here left every unwired board open; the domain is the kernel's.
        _refuse_priority_outside_domain(conn, priority, board=board, task_id=task_id)
    if body is not None:
        # A rewrite keeps the ask the card is already in service of: a body edit must not
        # silently resolve the card out of the register's roll-up
        # (kanban_register.restamp_rewritten_body).
        from hermes_cli.kanban_register import restamp_rewritten_body

        body = restamp_rewritten_body(conn, task_id, body, board=board)
    changed_fields = [
        field for field, value in (("title", title), ("body", body), ("priority", priority))
        if value is not None
    ]
    # Values worth echoing on the event: a goal loop or a cap turned on/off is a change a later reader
    # has to be able to date, and "fields" alone would not say WHICH way the switch went.
    room_values: dict = {}
    if goal_mode is not None:
        changed_fields.append("goal_mode")
        room_values["goal_mode"] = bool(goal_mode)
    if goal_max_turns is not None:
        changed_fields.append("goal_max_turns")
        room_values["goal_max_turns"] = goal_max_turns or None
    if max_runtime_seconds is not None or clear_max_runtime:
        changed_fields.append("max_runtime_seconds")
        room_values["max_runtime_seconds"] = max_runtime_seconds
    with write_txn(conn):
        status = _task_status(conn, task_id)
        if status is None or (result is not None and status != "done"):
            return False
        gate_floor = None
        if priority is not None:
            # The GATE half of a re-rank, decided BEFORE the value is written: a card that holds
            # other cards cannot be re-ranked below them (the child is never demoted, so the ask
            # is raised to the floor and the event says so). Doored values were checked above,
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
        if goal_mode is not None:
            assignments.append("goal_mode = ?")
            params.append(1 if goal_mode else 0)
        if goal_max_turns is not None:
            # 0 (or negative, rejected at the CLI) means "no explicit budget": the goal loop falls
            # back to its own default rather than running zero turns.
            assignments.append("goal_max_turns = ?")
            params.append(goal_max_turns if goal_max_turns > 0 else None)
        if max_runtime_seconds is not None or clear_max_runtime:
            assignments.append("max_runtime_seconds = ?")
            params.append(max_runtime_seconds if max_runtime_seconds else None)
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
                payload: dict = {"fields": non_priority_fields}
                if room_values:
                    payload["values"] = room_values
                _append_event(conn, task_id, "edited", payload)
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


def block_task(
    conn: sqlite3.Connection, task_id: str, *, reason: Optional[str] = None,
    kind: Optional[str] = None, expected_run_id: Optional[int] = None,
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
    """
    if kind is not None and kind not in VALID_BLOCK_KINDS:
        raise ValueError(f"block kind must be one of {sorted(VALID_BLOCK_KINDS)} or None")
    with write_txn(conn):
        cur_row = conn.execute(
            "SELECT status, block_kind, block_recurrences, current_run_id, claim_lock, worker_pid, "
            "worker_started_at FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if cur_row is None:
            return False
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
        # A handoff in flight is still the implementer's own card: the
        # ``review_requested`` run stays attached (``_end_run(keep_attached=True)``),
        # and the worker that filed it blocks the card to ask the reviewer a
        # question. ``source_status`` records 'review' so unblocking resumes the
        # review phase instead of a bare ``ready`` (which would re-spawn the
        # implementer's lane onto a card already handed off).
        if cur_row["status"] == "running":
            source_status = _retry_status_for_run(conn, task_id)
        elif cur_row["status"] == "review" and expected_run_id is not None:
            source_status = "review"
        else:
            source_status = "ready"
        requested_kind = kind
        rekind_reason = None
        # ``dependency`` only waits on incomplete parents. A worker filing that
        # kind with none open would park in ``todo`` and ``recompute_ready``
        # would promote+respawn it context-free on the next tick. Re-kind to
        # ``needs_input`` so it is sticky until a human unblocks.
        if kind == "dependency" and _parents_satisfied(conn, task_id):
            kind = "needs_input"
            rekind_reason = "no_open_parent"
        new_status, event_kind, set_sql, params, payload = _route_block(
            kind, reason, source_status, prev_kind=_row_get(cur_row, "block_kind"),
            prev_recurrences=int(_row_get(cur_row, "block_recurrences") or 0),
            triage_round_trips=triage_round_trips(conn, task_id),
        )
        if rekind_reason:
            payload["requested_kind"] = requested_kind
            payload["rekind_reason"] = rekind_reason
        sql = f"""
                UPDATE tasks
                   SET status        = '{new_status}',
                       claim_lock    = NULL,
                       claim_expires = NULL,
                       worker_pid    = NULL,
                       {set_sql}
                 WHERE id = ?
                """
        # The review-phase licence: a card in ``review`` whose attached run the
        # caller names is blockable by that run. The fence below keeps the write
        # pinned to the run the caller read, so this cannot reach a card under
        # someone else's handoff or an active review claim.
        sql += (
            "   AND (status IN ('running', 'ready') OR status = 'review')\n"
            if source_status == "review"
            else "   AND status IN ('running', 'ready')\n"
        )
        params = (*params, task_id)
        # The same fence as complete_task/request_review: a caller that names no run may only
        # park a card whose claim does not protect a live worker, and every admitted write is
        # made conditional on the run this caller read.
        allowed, _refusal, guard_sql, guard_params = _close_run_fence(
            cur_row, task_id, expected_run_id=expected_run_id, force=False,
        )
        if not allowed:
            _log.warning(
                "kanban: refusing to block %s — %s", task_id,
                live_claim_refusal(task_id, verdict=_claim_liveness(cur_row),
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
        if kind == "dependency":
            # Historical ordering: the dependency lane fires inside the txn.
            _fire_task_hook("kanban_task_blocked", blocked_task, task_id, run_id, reason=reason)
            return True
    _fire_task_hook("kanban_task_blocked", blocked_task, task_id, run_id, reason=reason)
    return True


def _route_block(
    kind: Optional[str], reason: Optional[str], source_status: str, *,
    prev_kind: Optional[str], prev_recurrences: int, triage_round_trips: int = 0,
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
    ``triage_round_trips`` is read from the card's own escalation history (see
    :func:`triage_round_trips`) and lands on the ``block_loop_detected`` payload, so
    a count that spans a triage trip is not read as N consecutive honest blocks.
    """
    payload = {"reason": reason, "kind": kind, "source_status": source_status}
    if kind == "dependency":
        return "todo", "dependency_wait", "block_kind    = ?", (kind,), payload
    recurrences = prev_recurrences + 1 if prev_kind == kind else 1
    set_sql = "block_kind    = ?,\n                       block_recurrences = ?"
    payload = {"reason": reason, "kind": kind, "recurrences": recurrences, "source_status": source_status}
    if recurrences >= BLOCK_RECURRENCE_LIMIT:
        payload["limit"] = BLOCK_RECURRENCE_LIMIT
        # ``recurrences`` alone reads as N consecutive honest blocks. Record how many
        # triage round-trips the count already spans, so the reader who re-blocks this
        # card after a trip cannot describe it as a state that never changed.
        payload["triage_round_trips"] = triage_round_trips
        return "triage", "block_loop_detected", set_sql, (kind, recurrences), payload
    return "blocked", "blocked", set_sql, (kind, recurrences), payload


# --- The triage escalation guard -------------------------------------------------------
#
# ``_route_block`` parks a repeating card in ``triage`` for a HUMAN. Every promotion path
# out of ``triage`` therefore has to refuse such a card: promoting it hands the same
# unchanged card — and the same failing context — straight back to the board, where it
# blocks again and manufactures a fresh graph of children on each pass. The promotion
# paths are ``specify_triage_task`` and ``kanban_db_graph.decompose_triage_task``; both
# call :func:`triage_escalation_refusal` inside their own write txn, so the refusal is
# atomic with the read that decided it and no caller can forget the guard.

TRIAGE_ESCALATION_EVENT_KIND = "block_loop_detected"
BLOCK_ROUTING_EVENT_KINDS = ("blocked", "dependency_wait", TRIAGE_ESCALATION_EVENT_KIND, "gave_up")
TRIAGE_ESCALATION_CAUSE = "block_loop_escalation"


class TriageEscalationRefusal:
    """Why a promotion out of ``triage`` refused: the block-loop breaker parked the card.

    Falsy, so every existing ``if not specify_triage_task(...)`` caller keeps its meaning,
    while the operator-facing surfaces can name the triggering event and the card. A bare
    ``False`` cannot tell "already promoted / moved out" from "escalated to a human", and
    that ambiguity is how the auto-decompose hook silently fanned out a card the board had
    already handed to a person.
    """

    __slots__ = ("task_id", "cause", "detail", "event_id", "payload")

    def __init__(self, task_id: str, event_id: int, payload: Optional[dict]):
        payload = payload or {}
        self.task_id = task_id
        self.cause = TRIAGE_ESCALATION_CAUSE
        self.event_id = event_id
        self.payload = payload
        self.detail = (
            f"{task_id} is parked in triage by the block-loop breaker (event {event_id} "
            f"'{TRIAGE_ESCALATION_EVENT_KIND}': kind={payload.get('kind')!r}, "
            f"recurrences={payload.get('recurrences')}/{payload.get('limit')}, "
            f"triage_round_trips={payload.get('triage_round_trips')}) — an escalation for a "
            f"human to dispose of with a board edit: archive the card, or move it out of the "
            f"triage column (to todo/ready). unblock / complete / reassign do not clear the "
            f"park — the card stays in triage. Promoting the unchanged card back onto the "
            f"board re-arms the block it was escalated for"
        )

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return f"TriageEscalationRefusal(task_id={self.task_id!r}, event_id={self.event_id!r})"


def triage_escalation_refusal(
    conn: sqlite3.Connection, task_id: str,
) -> Optional[TriageEscalationRefusal]:
    """The breaker's park for ``task_id`` when that is why it sits in ``triage``; else None.

    The predicate is the NEWEST block-routing event, never the merely-newest-one-that-happens
    -to-exist: a card that once escalated and later blocked again for another cause is
    triaged by whichever routing decision came last. Sticky by construction — nothing in the
    promotion paths clears the event, so a card stays refused until a human edits the column.
    """
    placeholders = ",".join("?" * len(BLOCK_ROUTING_EVENT_KINDS))
    row = conn.execute(
        f"SELECT id, kind, payload FROM task_events WHERE task_id = ? "
        f"AND kind IN ({placeholders}) ORDER BY id DESC LIMIT 1",
        (task_id, *BLOCK_ROUTING_EVENT_KINDS),
    ).fetchone()
    if row is None or row["kind"] != TRIAGE_ESCALATION_EVENT_KIND:
        return None
    return TriageEscalationRefusal(
        task_id, int(row["id"]), _json_or(_lossy_text(row["payload"])),
    )


def triage_round_trips(conn: sqlite3.Connection, task_id: str) -> int:
    """How many times this card has already been through triage and come back to work.

    Every prior ``block_loop_detected`` event is one COMPLETED round-trip: the card can only
    block again after it left ``triage``, whether the exit was the decomposer/specifier or a
    human editing the column directly. Counted *before* the current escalation is appended
    (``block_task`` reads it, then ``_route_block`` writes the new event), so the payload
    says how many times the board has already handed this card to a person.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = ?",
        (task_id, TRIAGE_ESCALATION_EVENT_KIND),
    ).fetchone()
    return int(row[0] or 0)


# ---------------------------------------------------------------------------
# The decompose RECORD guard (card t_9cb7f0b9)
#
# The escalation guard above answers ONE question: "did the block-loop breaker park this
# card in triage?". It does not answer "has this card's work already been decided?" — and
# the auto-decomposer claimed a review-blocked card out of triage whose newest comments said
# in as many words "the CODE HALF IS APPROVED at 3ce158d8" / "Do NOT decompose or
# re-implement this card", and minted four children off the root BODY alone: work a
# reviewer had already ruled on, invented a second time by an LLM that never read the
# thread.
#
# So the decomposer READS THE RECORD — the card's comments and its run history — and
# REFUSES, DETERMINISTICALLY (a fixed marker table over a bounded window; no inference, no
# prose in the triage prompt), when ANY of these hold:
#
#   live_run       a live run owns the card (status 'running', or a held claim)
#   in_review      the card sits in the review column, or its newest review-lifecycle event
#                  is a REQUEST (handed to a reviewer, with no decision since)
#   approved       the newest comments carry the estate's APPROVED verdict
#   superseded     the newest comments carry a superseded / withdrawn / do-not-implement note
#   live_artifact  the newest comments name a live branch (wt|fix|feat/<task-id>-*) or a PR
#
# The refusal is RECORDED as a ``decompose_refused`` event carrying every matched cause, the
# matched line and its comment id, so a blocked loop stays VISIBLE instead of being papered
# over with invented work. Sticky by construction — nothing in the promotion paths clears
# it, so a human disposes of the card with a board edit (archive it, or move it out of the
# triage column), exactly like the escalation park.
# ---------------------------------------------------------------------------

DECOMPOSE_REFUSAL_EVENT_KIND = "decompose_refused"
# How far back into the thread the marker scan reads. The deciding note on a card the board
# has just parked is at the tail; an unbounded scan would key on a superseded verdict the
# card has since moved past.
DECOMPOSE_RECORD_SCAN_DEPTH = 5

DECOMPOSE_CAUSE_LIVE_RUN = "live_run"
DECOMPOSE_CAUSE_IN_REVIEW = "in_review"
DECOMPOSE_CAUSE_APPROVED = "approved"
DECOMPOSE_CAUSE_SUPERSEDED = "superseded"
DECOMPOSE_CAUSE_LIVE_ARTIFACT = "live_artifact"

# A review handoff leaves the card with a reviewer; these kinds settle it. The newest
# lifecycle event decides, so a review that was handed back (or closed) is not a park.
REVIEW_REQUEST_EVENT_KINDS = ("review_requested", "review_reopened")
REVIEW_SETTLED_EVENT_KINDS = ("changes_requested", "completed", "archived")
REVIEW_LIFECYCLE_EVENT_KINDS = REVIEW_REQUEST_EVENT_KINDS + REVIEW_SETTLED_EVENT_KINDS

# The estate's verdict vocabulary. APPROVED is case-sensitive on purpose: the review lanes
# write the verdict in caps, and "needs an approval"/"the approval is pending" must not
# refuse a card that is still open. Branch/PR markers are the estate's own naming
# (``fix/t_<cardid>-<slug>``), so a card whose thread names one has a live artifact.
_DECOMPOSE_RECORD_MARKERS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    (DECOMPOSE_CAUSE_APPROVED, re.compile(r"\bAPPROVED\b")),
    (
        DECOMPOSE_CAUSE_SUPERSEDED,
        re.compile(
            r"\bsuperseded\b|\bwithdrawn\b"
            r"|\bDo NOT\s+(?:decompose|re-?implement|implement|start)\b",
            re.IGNORECASE,
        ),
    ),
    (
        DECOMPOSE_CAUSE_LIVE_ARTIFACT,
        re.compile(
            r"\b(?:wt|fix|feat|test|chore|release)/t_[A-Za-z0-9_.-]+"
            r"|https?://github\.com/[^\s/]+/[^\s/]+/pull/\d+"
        ),
    ),
)


def _matched_line(body: str, match: "re.Match[str]") -> str:
    """The whole line of ``body`` the marker matched, stripped and clipped for a payload."""
    start = body.rfind("\n", 0, match.start()) + 1
    end = body.find("\n", match.end())
    line = body[start:end if end != -1 else len(body)].strip()
    return line[:240]


class DecomposeRefusal:
    """Why the auto-decomposer refused: the card's RECORD says the work is already decided.

    Falsy, like :class:`TriageEscalationRefusal`, so every existing ``if not
    decompose_triage_task(...)`` caller keeps its meaning while an operator-facing surface
    can name the causes and the rows that carried them. A bare ``False``/``None`` cannot
    tell "nothing to decompose" from "already approved" — and that ambiguity is exactly how
    a review-blocked card got fanned out into fresh children.
    """

    __slots__ = ("task_id", "causes", "matches", "detail")

    def __init__(self, task_id: str, matches: list[dict]):
        self.task_id = task_id
        self.matches = matches
        # One entry per cause, newest evidence first, so the payload stays readable.
        causes: list[str] = []
        for match in matches:
            cause = str(match.get("cause"))
            if cause not in causes:
                causes.append(cause)
        self.causes = causes
        evidence = "; ".join(
            f"{m['cause']} @ "
            + (
                f"comment {m['comment_id']} ({m.get('author')}): {m.get('line')!r}"
                if m.get("comment_id") is not None
                else f"event {m.get('event_id')} '{m.get('event_kind')}'"
                if m.get("event_id") is not None
                else f"tasks.{m.get('field')}={m.get('value')!r}"
            )
            for m in matches
        )
        self.detail = (
            f"{task_id} is refused auto-decomposition: the card's RECORD says the work is "
            f"already decided (causes: {', '.join(causes)}). A card carrying an approval, a "
            f"superseded note or a live branch/PR — or one that sits in review, or that a "
            f"live run owns — is not a triage card: decomposing it mints children off the "
            f"unchanged root BODY, which is work invented a second time. Evidence: "
            f"{evidence}. Disposals: archive the card, or move it out of the triage column "
            f"(to todo/ready). unblock / complete / reassign do not clear this refusal"
        )

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return f"DecomposeRefusal(task_id={self.task_id!r}, causes={self.causes!r})"


def decompose_refusal(
    conn: sqlite3.Connection, task_id: str, *,
    scan_depth: int = DECOMPOSE_RECORD_SCAN_DEPTH,
) -> Optional[DecomposeRefusal]:
    """The RECORD refusal for ``task_id``, or None when the card is a plain triage card.

    Read-only: it answers from ``tasks`` (live claim / review column), ``task_events`` (the
    review lifecycle) and the newest ``scan_depth`` comments (the verdict vocabulary). The
    caller records the refusal; see :func:`decompose_refusal_guard`.
    """
    row = conn.execute(
        "SELECT status, claim_lock, current_run_id, worker_pid FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None

    matches: list[dict] = []
    seen: set[str] = set()

    def _add(cause: str, **evidence: Any) -> None:
        if cause in seen:
            return
        seen.add(cause)
        matches.append({"cause": cause, **evidence})

    status = row["status"]
    if status == "running" or row["claim_lock"]:
        _add(
            DECOMPOSE_CAUSE_LIVE_RUN, field="status", value=status,
            run_id=row["current_run_id"], worker_pid=row["worker_pid"],
            claim_lock=row["claim_lock"],
        )
    if status == "review":
        _add(DECOMPOSE_CAUSE_IN_REVIEW, field="status", value=status)
    else:
        placeholders = ",".join("?" * len(REVIEW_LIFECYCLE_EVENT_KINDS))
        event = conn.execute(
            f"SELECT id, kind FROM task_events WHERE task_id = ? "
            f"AND kind IN ({placeholders}) ORDER BY id DESC LIMIT 1",
            (task_id, *REVIEW_LIFECYCLE_EVENT_KINDS),
        ).fetchone()
        if event is not None and event["kind"] in REVIEW_REQUEST_EVENT_KINDS:
            _add(
                DECOMPOSE_CAUSE_IN_REVIEW, event_id=int(event["id"]),
                event_kind=str(event["kind"]),
            )

    comments = conn.execute(
        "SELECT id, author, body FROM task_comments WHERE task_id = ? "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (task_id, int(scan_depth)),
    ).fetchall()
    for comment in comments:
        body = _lossy_text(comment["body"]) or ""
        if not isinstance(body, str):
            continue
        for cause, pattern in _DECOMPOSE_RECORD_MARKERS:
            if cause in seen:
                continue
            match = pattern.search(body)
            if match is None:
                continue
            _add(
                cause, comment_id=int(comment["id"]),
                author=_lossy_text(comment["author"]), line=_matched_line(body, match),
            )

    if not matches:
        return None
    return DecomposeRefusal(task_id, matches)


def record_decompose_refusal(
    conn: sqlite3.Connection, refusal: DecomposeRefusal, *, author: Optional[str] = None,
) -> Optional[int]:
    """Append the ``decompose_refused`` event inside the caller's txn; the new id, or None.

    None means the newest recorded refusal already carries this cause set: the decomposer is
    retried by the tick, the CLI sweep and the dashboard, and the record must not grow one
    row per attempt over an unchanged card. No txn of its own — the callers (the promotion
    paths) are already inside one, or open one around it.
    """
    previous = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
        (refusal.task_id, DECOMPOSE_REFUSAL_EVENT_KIND),
    ).fetchone()
    if previous is not None:
        payload = _json_or(_lossy_text(previous["payload"]), {}) or {}
        if list(payload.get("causes") or []) == list(refusal.causes):
            return None
    _append_event(
        conn, refusal.task_id, DECOMPOSE_REFUSAL_EVENT_KIND,
        {
            "causes": refusal.causes,
            "matches": refusal.matches,
            "detail": refusal.detail,
            "author": author,
        },
    )
    row = conn.execute("SELECT last_insert_rowid()").fetchone()
    return int(row[0]) if row is not None else None


def decompose_refusal_guard(
    conn: sqlite3.Connection, task_id: str, *, author: Optional[str] = None,
) -> Optional[TriageEscalationRefusal | DecomposeRefusal]:
    """The refusal that keeps ``task_id`` in ``triage``, and record it; else None.

    The ONE entrance both promotion paths use, so neither can forget a guard. The escalation
    park comes first and is returned WITHOUT a new event — its own ``block_loop_detected``
    payload already carries the reason and the operator's disposal instructions. Anything
    else the record refuses is recorded here (idempotently).
    """
    escalation = triage_escalation_refusal(conn, task_id)
    if escalation is not None:
        return escalation
    refusal = decompose_refusal(conn, task_id)
    if refusal is None:
        return None
    record_decompose_refusal(conn, refusal, author=author)
    return refusal


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
                "worker_started_at FROM tasks WHERE id = ?", (task_id,),
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
            if not _nonblank_str(reviewer):
                reviewer = _prior_reviewer(conn, task_id)
                if reviewer is False:
                    return _ret(
                        False, "re-review has no durable reviewer provenance (the "
                        "latest changes_requested event is missing or "
                        "malformed); pass reviewer= explicitly",
                    )
                if reviewer is None:
                    # A FIRST review must name its reviewer. Leaving it unnamed does not
                    # resolve to "whoever ought to review": the card still carries the
                    # implementer as its assignee, so the handoff would record
                    # ``reviewer: None`` and leave the review lane with nothing to spawn
                    # but the lane it just came from (the self-review guard can only park
                    # that row). Refuse HERE, where the implementer can still name one;
                    # a re-review keeps the durable default above.
                    return _ret(
                        False, "a review request must name its reviewer: pass "
                        "reviewer=<profile>, a lane distinct from the implementer. "
                        "An unnamed handoff records reviewer: None and leaves the "
                        "implementer holding the card, so the review lane has no "
                        "reviewer it can spawn",
                    )
            reviewer = _canonical_assignee(reviewer)
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
            # The guard the fence decided: the run this caller read, or ``current_run_id IS NULL``
            # when it named none and the card had no active run. Never empty — an unguarded UPDATE
            # here is what let a transition land on a successor's run (#123811).
            #
            # A card already in ``review`` accepts a re-request ONLY from the run that
            # handed it off (the caller named it, and the fence pinned the write to it):
            # that is how an implementer CORRECTS its own handoff — it named the wrong
            # reviewer — instead of completing a card no reviewer ever looked at. Naming
            # no run keeps the narrow set, so a blind re-stamp cannot rewrite a card the
            # review lane may already be spawning.
            source_sql = (
                "   AND (status IN ('running', 'ready') OR status = 'review')\n"
                if expected_run_id is not None
                else "   AND status IN ('running', 'ready')\n"
            )
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
                """ + source_sql + guard_sql,
                params,
            )
            if cur.rowcount != 1:
                return _ret(
                    False, live_row_refusal(conn, task_id, caller_run_id=expected_run_id,
                                            verb="request review"),
                )
            if isinstance(metadata, dict):
                staged_copies = _stage_completion_artifacts(
                    conn, task_id, metadata, now, uploaded_by="kanban_request_review",
                )
            run_id = _end_or_synthesize_run(
                conn, task_id, outcome="review_requested", status="review",
                summary=summary, metadata=metadata, synthesize=bool(summary or metadata),
                profile=implementer, keep_attached=True,
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
    ``reviewer`` is named -- so for a card parked in ``review`` this is the
    durable author provenance. The dispatcher uses it to refuse to spawn an
    implementer as its own reviewer. ``None`` means the card never recorded one
    (no ``review_requested`` event, or a payload without a usable value);
    callers must read that as "unknown", never as "distinct". Shared by
    :func:`request_changes` and :func:`reopen_review_task`, which both route on
    this field.
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
        if task_row["status"] == "review":
            # The AUTHOR's withdrawal. The handoff keeps the implementing run
            # attached (``_end_run(keep_attached=True)``), and that run — and no
            # other — may take the card back out of review before a reviewer
            # claims it: the workspace is gone and nobody has started reading, so
            # the honest move is to take the handoff back rather than to complete
            # a card no reviewer looked at. Same landing status, same wake event
            # for the implementer's lane, marked ``withdrawn_by: implementer`` so a
            # reader can tell the author's withdrawal from a reviewer's verdict.
            if (
                current_run_id is None or expected_run_id is None
                or int(current_run_id) != int(expected_run_id)
            ):
                return False, "task is not in an active review run"
            handoff = _json_dict(
                _row_get(_latest_event(conn, task_id, "review_requested"), "payload"))
            implementer = review_implementer(conn, task_id) or _canonical_assignee(
                _nonblank_str(task_row["assignee"]))
            if implementer is None:
                return False, "review handoff has no valid implementer provenance"
            new_status = _landing_status_after_parents(conn, task_id)
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status = ?,
                       assignee = COALESCE(?, assignee),
                       current_run_id = NULL,
                       claim_lock = NULL,
                       claim_expires = NULL,
                       worker_pid = NULL, worker_started_at = NULL
                 WHERE id = ? AND status = 'review' AND current_run_id = ?
                """,
                (new_status, implementer, task_id, int(current_run_id)),
            )
            if cur.rowcount != 1:
                return False, "task changed during review handoff"
            _append_event(
                conn,
                task_id,
                "changes_requested",
                {
                    "reason": reason,
                    "implementer": implementer,
                    "reviewer": _nonblank_str(handoff.get("reviewer")),
                    "status": new_status,
                    "withdrawn_by": "implementer",
                },
                run_id=int(current_run_id),
            )
            return True, implementer
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
        implementer = review_implementer(conn, task_id)
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


def _landing_status_after_parents(conn: sqlite3.Connection, task_id: str) -> str:
    """``ready`` if every parent is terminal else ``todo`` — the re-gate shared by
    unblock/reopen so neither can spawn a child whose upstream is unfinished."""
    return "ready" if _parents_satisfied(conn, task_id) else "todo"


def unblock_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """``blocked``/``scheduled`` -> its resumable phase (parent re-gated; ``review``
    when that is where it left off), closing any leaked run first."""
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
        # is a fresh start for the retry budget.
        #
        # A card resuming back to ``review`` keeps the run that owned it when it
        # was blocked: the implementer's handoff run. Clearing it here reopened the
        # wedge ``request_review`` closes — the worker that blocked its own review
        # card to ask a question got it back with no owner, so it could no longer
        # return, re-block or re-request it. The run stays CLOSED (it handed off);
        # it is only re-attached as the card's owner, the state a handoff leaves.
        restore_run_id = None
        if new_status == "review":
            row = conn.execute(
                "SELECT run_id FROM task_events WHERE task_id = ? AND run_id IS NOT NULL "
                "AND kind IN ('blocked', 'dependency_wait', 'block_loop_detected', 'gave_up') "
                "ORDER BY id DESC LIMIT 1", (task_id,),
            ).fetchone()
            restore_run_id = int(row["run_id"]) if row is not None else None
        cur = conn.execute(
            "UPDATE tasks SET status = ?, current_run_id = ?, "
            "consecutive_failures = 0, last_failure_error = NULL "
            "WHERE id = ? AND status IN ('blocked', 'scheduled')",
            (new_status, restore_run_id, task_id),
        )
        if cur.rowcount != 1:
            return False
        _append_event(
            conn, task_id, "unblocked",
            (
                {"status": new_status, "resume_status": resume_status}
                if new_status != "ready" or resume_status != "ready"
                else None
            ),
        )
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
        implementer = review_implementer(conn, task_id)
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
) -> bool | TriageEscalationRefusal:
    """Update title/body/assignee (when given) and move ``triage -> todo`` in one
    txn; False when not in triage. Lands in ``todo`` (not ``ready``) so parent
    gating still applies; the audit comment is written only when a field changed.

    A card the block-loop breaker parked refuses with a
    :class:`TriageEscalationRefusal` (falsy) and stays in ``triage``: specification
    cannot fix a card whose state has not changed since the block it was escalated
    for, and promoting it re-arms the loop.
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
        refusal = triage_escalation_refusal(conn, task_id)
        if refusal is not None:
            return refusal
        if body is not None:
            # Specifying a card rewrites its body; the ask the card was filed for travels
            # with it (kanban_register.restamp_rewritten_body). A "specify" that merely
            # restated the body without its stamp must not resolve the operator's ask.
            from hermes_cli.kanban_register import restamp_rewritten_body

            body = restamp_rewritten_body(
                conn, task_id, body, previous=existing["body"],
            )
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
) -> bool:
    """Park in ``scheduled`` (waiting on time, not a human; not dispatchable)
    until ``unblock_task`` re-gates it."""
    with write_txn(conn):
        params: list[Any] = [task_id]
        sql = """
            UPDATE tasks
               SET status       = 'scheduled',
                   claim_lock   = NULL,
                   claim_expires= NULL,
                   worker_pid   = NULL
             WHERE id = ?
               AND status IN ('todo', 'ready', 'running', 'blocked')
        """
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params.append(int(expected_run_id))
        if conn.execute(sql, params).rowcount != 1:
            return False
        run_id = _end_or_synthesize_run(
            conn, task_id, outcome="scheduled", status="scheduled", summary=reason, synthesize=bool(reason),
        )
        _append_event(conn, task_id, "scheduled", {"reason": reason}, run_id=run_id)
        return True


def explicit_max_runtime_seconds(conn: sqlite3.Connection, task: Task) -> Optional[int]:
    """The ``max_runtime_seconds`` the CARD AUTHOR set — never the dispatcher's default.

    The dispatcher stamps ``kanban.default_max_runtime_seconds`` onto a card that carries none, at
    CLAIM time, so its runtime sweep can always reap a running slot (ruling ``t_b2865b89`` §4). The
    card's own column therefore no longer separates an author's budget from that scheduling default.
    ``_claim_and_open_run`` copies the column onto the run row BEFORE the stamp, so the active run is
    the canonical record: this returns ``None`` for a defaulted card — whose worker must keep the
    generic terminal timeout — and the author's value otherwise. Falls back to the card's column when
    there is no run row yet (a task that was never claimed still carries its own value).
    """
    run_id = task.current_run_id
    if run_id is not None:
        row = conn.execute(
            "SELECT max_runtime_seconds FROM task_runs WHERE id = ?", (int(run_id),)
        ).fetchone()
        if row is not None:
            return _row_get(row, "max_runtime_seconds")
    return task.max_runtime_seconds


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
    _ctx_header(
        lines, task,
        explicit_max_runtime_seconds=explicit_max_runtime_seconds(conn, task),
    )
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


def _ctx_header(
    lines: list[str], task: Task, *, explicit_max_runtime_seconds: Optional[int] = None,
) -> None:
    lines.append(f"# Kanban task {task.id}: {task.title}")
    lines.append("")
    lines.append(f"Assignee: {task.assignee or '(unassigned)'}")
    lines.append(f"Status:   {task.status}")
    if task.tenant:
        lines.append(f"Tenant:   {task.tenant}")
    lines.append(f"Workspace: {task.workspace_kind} @ {task.workspace_path or '(unresolved)'}")
    if task.max_runtime_seconds is not None:
        lines.append(f"Max runtime: {task.max_runtime_seconds}s")
    # The terminal timeout is derived from the card's EXPLICIT cap ONLY. A card that carries no cap
    # of its own gets the dispatcher's ``default_max_runtime_seconds`` stamped onto it at claim so
    # the runtime sweep can reap the slot; that default is not an author budget and must NEVER be
    # reported as the worker's terminal timeout — the child env does not receive it either (ruling
    # t_b2865b89 §4). Claiming a cap would tell the worker to run a command it will be killed in.
    if explicit_max_runtime_seconds is not None:
        terminal_timeout = _worker_terminal_timeout_env(
            explicit_max_runtime_seconds, os.environ.get("TERMINAL_TIMEOUT"),
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

def board_stats(conn: sqlite3.Connection) -> dict:
    """Per-status + per-assignee counts and the oldest ``ready`` age (staleness signal)."""
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
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
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
from hermes_cli.kanban_db_connect import (  # noqa: E402
    _INITIALIZED_PATHS,
    init_db,
    write_txn,
)
from hermes_cli.kanban_db_workspace import (  # noqa: E402
    _cleanup_workspace,
    _is_managed_scratch_path,
    _managed_scratch_path_info,
    _scratch_workspace,
)
from hermes_cli.kanban_db_dispatch import (  # noqa: E402
    DEFAULT_FAILURE_LIMIT,
    DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
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
