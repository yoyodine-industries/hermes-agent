"""Context-local state for delegate_task child execution.

A Hermes process may itself be a Kanban dispatcher worker with HERMES_KANBAN_* in
os.environ. In-process delegate_task children and cron jobs fired via
``cronjob(action="run")`` are NOT dispatcher-owned, so identity gates must fail
closed for them without mutating the process-global environment.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator, Mapping, MutableMapping, overload

_DELEGATED_CHILD_CONTEXT: ContextVar[bool] = ContextVar("hermes_delegated_child_context", default=False)
# Any in-process execution that is NOT the dispatcher-owned worker (cron jobs). Kept separate
# so delegate_task-specific behaviour (subprocess env scrubbing, its error strings) is unchanged.
_NON_DISPATCHER_OWNED_CONTEXT: ContextVar[bool] = ContextVar("hermes_non_dispatcher_owned_context", default=False)

DELEGATED_CHILD_ENV_MARKER = "HERMES_DELEGATED_CHILD_CONTEXT"

KANBAN_ENV_KEYS: tuple[str, ...] = (
    "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK",
    "HERMES_KANBAN_GOAL_MODE", "HERMES_KANBAN_GOAL_MAX_TURNS",
)


@contextmanager
def delegated_child_context(session_id: str | None = None) -> Iterator[None]:
    """Mark child execution and isolate its task-local session identity. Even a context
    entered without an id must restore the parent's session ContextVar (child
    construction calls ``set_current_session_id``)."""
    token = _DELEGATED_CHILD_CONTEXT.set(True)
    try:
        from gateway.session_context import scoped_current_session_id  # lazy: it calls is_delegated_child_context()

        with scoped_current_session_id(session_id):
            yield
    finally:
        _DELEGATED_CHILD_CONTEXT.reset(token)


def is_delegated_child_context() -> bool:
    """Return True while code is running for a delegate_task child."""
    return bool(_DELEGATED_CHILD_CONTEXT.get())


def enter_non_dispatcher_owned_context() -> Token[bool]:
    """Token form of :func:`non_dispatcher_owned_context` for long try/finally scopes."""
    return _NON_DISPATCHER_OWNED_CONTEXT.set(True)


def exit_non_dispatcher_owned_context(token: Token[bool]) -> None:
    """Restore the flag saved by :func:`enter_non_dispatcher_owned_context`."""
    _NON_DISPATCHER_OWNED_CONTEXT.reset(token)


@contextmanager
def non_dispatcher_owned_context() -> Iterator[None]:
    """Mark in-process execution that does NOT own the dispatcher's Kanban task; without it
    a cron agent run inside a worker is misread as that worker (kanban toolset force-added,
    ``kanban_complete`` defaulting to its task). ContextVar-scoped rather than clearing
    os.environ, which the worker's claim heartbeat and concurrent readers share."""
    token = enter_non_dispatcher_owned_context()
    try:
        yield
    finally:
        exit_non_dispatcher_owned_context(token)


def is_dispatcher_owned_worker_context() -> bool:
    """The single predicate every ``HERMES_KANBAN_*`` identity gate should use."""
    return not (is_delegated_child_process_context() or _NON_DISPATCHER_OWNED_CONTEXT.get())


def board_name_resolves(board: str) -> bool:
    """Whether *board* names a REGISTERED board — the question the CLI's own ``--board``
    validation asks before it refuses ``board '<x>' does not exist``.

    ``default`` always resolves; any other slug resolves iff a ``board.json`` is registered under
    it (the registry probe, never the store path — a pin legitimately overrides that), and a
    malformed slug resolves nowhere. An error that is NOT an answer to the question (the registry
    cannot be imported at all) answers ``True``, so a caller asking "must this still go through the
    pin?" keeps its pin: an isolation pin is never dropped blind.
    """
    try:
        from hermes_cli import kanban_db as kb

        slug = kb._normalize_board_slug(board)
    except ValueError:
        return False
    except Exception:  # health: allow BLE001 -- an unreadable registry is not an answer to "does this name resolve"; the caller keeps its pin
        return True
    if not slug:
        return False
    try:
        return slug == kb.DEFAULT_BOARD or kb.board_exists(slug)
    except Exception:  # health: allow BLE001 -- same rule: an unreadable registry keeps the pin
        return True


def explicit_board_intent_is_pinned(board: str | None = None) -> bool:
    """Whether an explicit kanban ``board=`` argument must still resolve through
    the dispatcher-injected env pins (``HERMES_KANBAN_DB`` & friends) rather than
    its own board directory.

    True for in-process delegate children, descendants carrying
    :data:`DELEGATED_CHILD_ENV_MARKER`, and dispatched workers
    (``HERMES_KANBAN_TASK`` set). The pins are the "workers physically cannot
    see other boards" isolation, and :func:`kanban_path_is_fenced` checks the
    pinned path / fenced root — an explicit board that resolved elsewhere would
    also escape that fence. Outside these fences an explicit board is the
    caller's own intent and wins.

    A name that resolves to NO board is not a pin violation, so it is not pinned (ruling
    t_ecd51c9c): there is nothing to pin and no board's data to protect. That boundary protects
    data on boards that EXIST; refusing an unresolved name here would refuse one layer earlier
    than the store resolution the pin exists to protect, and re-mask the CLI's honest
    ``board '<x>' does not exist`` — teaching operators that a typo is a permission problem.
    """
    if board and not board_name_resolves(board):
        return False
    if _DELEGATED_CHILD_CONTEXT.get():
        return True
    if os.environ.get(DELEGATED_CHILD_ENV_MARKER):
        return True
    return bool((os.environ.get("HERMES_KANBAN_TASK") or "").strip())


def owned_kanban_task() -> str:
    """The board task this execution OWNS: ``HERMES_KANBAN_TASK`` for the dispatcher-owned
    worker, ``""`` otherwise. Tool access is not worker identity — a profile can expose the
    kanban toolset interactively, and children/cron runs inherit the env var — so every
    reader that turns the task id into worker behaviour (guidance, stop nudge, terminal
    outcomes) goes through this one helper."""
    if not is_dispatcher_owned_worker_context():
        return ""
    return (os.environ.get("HERMES_KANBAN_TASK") or "").strip()


def is_delegated_child_process_context() -> bool:
    """Return True in this process or a subprocess spawned by a child."""
    return bool(_DELEGATED_CHILD_CONTEXT.get()) or bool(os.environ.get(DELEGATED_CHILD_ENV_MARKER))


def _fenced_kanban_root() -> str:
    """The board root this process's Kanban lineage lives under (``kanban_home()``); ``"1"`` when it
    cannot be resolved, which readers treat as "fence every board" (the pre-path marker)."""
    try:
        from hermes_cli.kanban_db import kanban_home
        return str(kanban_home())
    except Exception:
        return "1"


def scrub_kanban_env(env: Mapping[str, str] | MutableMapping[str, str]) -> dict[str, str]:
    """Remove worker identity, retaining board/location and an inherited write fence.

    TASK absence alone would promote a descendant to an orchestrator. The marker
    survives later execs, including scripts that remove TASK themselves. This is
    cooperative runtime scoping, not confinement of code with direct SQLite access.

    The marker's value is the fenced board ROOT, so the fence applies to the lineage's
    board and not to every Kanban DB the descendant touches: a child running a repro
    against a temp ``HERMES_HOME`` got a silently read-only board there. An inherited
    path-valued marker is kept (a grandchild that moved HERMES_HOME must not re-fence
    onto its scratch root and unfence the real one).
    """
    cleaned = {k: v for k, v in env.items() if k not in KANBAN_ENV_KEYS}
    inherited = str(env.get(DELEGATED_CHILD_ENV_MARKER) or "")
    cleaned[DELEGATED_CHILD_ENV_MARKER] = inherited if inherited and inherited != "1" else _fenced_kanban_root()
    return cleaned


def kanban_path_is_fenced(path: os.PathLike[str] | str) -> bool:
    """Whether Kanban mutations at *path* (a board DB or board-metadata root) are denied for this
    process: always for an in-process delegate child (the parent's own board); for a spawned
    descendant only when *path* is the dispatcher-pinned ``HERMES_KANBAN_DB`` or lies under the
    fenced root the marker carries. A legacy ``"1"`` marker fences everything."""
    if _DELEGATED_CHILD_CONTEXT.get():
        return True
    marker = os.environ.get(DELEGATED_CHILD_ENV_MARKER, "")
    if not marker:
        return False
    if marker == "1":
        return True
    from pathlib import Path
    target = Path(path).expanduser().resolve()
    pinned = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if pinned and target == Path(pinned).expanduser().resolve():
        return True
    try:
        target.relative_to(Path(marker).expanduser().resolve())
    except ValueError:
        return False
    return True


# --- the ONE reviewed exception to the child-context Kanban write fence -------------------------
#
# WHY IT EXISTS. A `hermes peer dm` refusal is how a lane learns that the control channel
# between lanes is broken, and the context where it breaks is a DISPATCHER-SPAWNED WORKER:
# that worker's CLI inherits this marker AND descends from the live claim, so every board
# write is refused. Measured 2026-10-14 (card t_703af8d1): the refusal report printed
# "NO CARD FILED (the board write failed)" -- mis-attributing a fenced-context refusal as an
# I/O failure -- and the report of a fleet-wide DM outage vanished into a stderr line nobody
# reads. That is the exact failure the report existed to end.
#
# WHY THIS WRITE IS SAFE FROM A CHILD CONTEXT. The fence exists so a worker cannot MOVE the
# estate it holds a claim on -- re-rank it, complete its own card, dispatch itself, tear a
# board down. A refusal report is none of that: it is a NEW row on the OPS board, addressed
# to a lane other than the filer, idempotency-keyed so a repeat is a no-op, and it mutates
# no existing row. It is the REPORT of a failure, never an act on the estate.
#
# HOW IT IS KEPT NARROW. Not a fence flag, not an environment variable (a worker's shell can
# strip either): a ContextVar a caller MUST set explicitly, for one of the NAMED purposes
# below, around the single write. The write door logs every use at WARNING, so an abuse is
# visible rather than silent, and a purpose that is not named here is refused AT THE SEAM.
CHILD_KANBAN_WRITE_PURPOSES: tuple[str, ...] = ("peer-refusal-report",)
_CHILD_KANBAN_WRITE_PURPOSE: ContextVar[str] = ContextVar(
    "hermes_child_kanban_write_purpose", default=""
)


@contextmanager
def child_kanban_write_allowance(purpose: str) -> Iterator[str]:
    """Allow ONE named child-context Kanban write (the reviewed exception above).

    Set only around the single filing it is granted for; a purpose not in
    :data:`CHILD_KANBAN_WRITE_PURPOSES` is refused here, so the exception cannot be
    widened by a caller that merely wants a write.
    """
    if purpose not in CHILD_KANBAN_WRITE_PURPOSES:
        raise ValueError(
            f"unknown child-context kanban write purpose {purpose!r}; "
            f"named purposes: {', '.join(CHILD_KANBAN_WRITE_PURPOSES)}"
        )
    token = _CHILD_KANBAN_WRITE_PURPOSE.set(purpose)
    try:
        yield purpose
    finally:
        _CHILD_KANBAN_WRITE_PURPOSE.reset(token)


def child_kanban_write_purpose() -> str:
    """The purpose of the allowance in force, or ``""`` -- in which case the fence holds."""
    return str(_CHILD_KANBAN_WRITE_PURPOSE.get() or "")


# --- provenance arm (ruling t_fcf7a321, Decision 1) --------------------------------------------
#
# The ContextVar arm above and the marker arm inside ``kanban_path_is_fenced`` are COOPERATIVE:
# a worker's shell can strip ``HERMES_DELEGATED_CHILD_CONTEXT`` (the marker), and the process is
# then indistinguishable from a top-level CLI. Provenance is not cooperatively clearable — a
# descendant cannot change who its ancestors are — so the kanban seams consult this arm IN
# ADDITION to the two above. Nothing here narrows ``kanban_path_is_fenced`` for its non-kanban
# callers (cron / estop / code_exec / transports).

#: How long one live-claim read is reused inside a process. The CLI is short-lived (one read per
#: invocation); a long-lived process (the gateway) can only ever ADD this refusal for a caller
#: descended from a worker of ITS OWN board, which it never is, so a stale entry is inert there.
LIVE_CLAIM_PROBE_TTL_SECONDS = 15.0
_live_claim_cache: dict[str, tuple[float, tuple[tuple[int, object], ...]]] = {}
#: Ancestry is stable for the life of a process; computed once, capped so a pathological tree
#: cannot spin.
_MAX_ANCESTOR_HOPS = 64
_ancestor_cache: tuple[int, ...] | None = None

#: Named for every refusal this arm raises, so a worker learns the ONE sanctioned release rather
#: than reaching for an ``env -u`` strip (Decision 1(b)).
PROVENANCE_REFUSAL_MESSAGE = (
    "kanban: refusing — this process descends from a LIVE dispatched worker that holds a claim "
    "on this board, so it may not move the estate; a worker's shell cannot clear its own fence by "
    "unsetting HERMES_DELEGATED_CHILD_CONTEXT. Run the action from a top-level session, or use the "
    "one sanctioned non-top-level release: a board whose own board.json declares \"dispatch\": "
    "false is torn down single-actor with `hermes kanban boards rm --estate <slug>`."
)


def process_ancestors(pid: int | None = None) -> tuple[int, ...]:
    """PID ancestry of this process (or *pid*), nearest first, EXCLUDING the pid itself.

    ``psutil`` is already a dependency of ``hermes_cli.tree_identity``; when it is
    unavailable the chain is empty, which fences nothing (a fence that cannot see must not
    refuse).
    """
    global _ancestor_cache
    own = pid is None
    if own and _ancestor_cache is not None:
        return _ancestor_cache
    try:
        import psutil
    except Exception:  # health: allow BLE001 -- fail-open (t_fcf7a321): a fence that cannot see must not refuse, so no psutil means no ancestry
        return ()
    chain: list[int] = []
    try:
        current = int(os.getpid() if own else pid)
        for _ in range(_MAX_ANCESTOR_HOPS):
            parent = int(psutil.Process(current).ppid())
            if parent <= 1 or parent in chain or parent == current:
                break
            chain.append(parent)
            current = parent
    except Exception:  # health: allow BLE001, S110 -- fail-open (t_fcf7a321): a broken walk keeps the chain read so far; the fence may only add a refusal it can prove
        pass
    result = tuple(chain)
    if own:
        _ancestor_cache = result
    return result


def _resolve_claim_store(path: os.PathLike[str] | str | None) -> str | None:
    """The board DB whose live claims this arm consults.

    *path* is what the mutator named — a board DB, or a metadata root. A board DB is used as
    given; anything else falls back to the ACTIVE board, which is the board a worker's shell is
    pinned to (``scrub_kanban_env`` keeps ``HERMES_KANBAN_DB``).
    """
    from pathlib import Path

    if path is not None:
        candidate = Path(path).expanduser()
        if candidate.suffix == ".db":
            try:
                return str(candidate.resolve())
            except OSError:
                return None
    try:
        from hermes_cli import kanban_db as kb

        return str(kb.kanban_db_path().expanduser().resolve())
    except Exception:  # health: allow BLE001 -- fail-open (t_fcf7a321): an unresolvable board store leaves the arm blind, so it answers None, not a refusal
        return None


def live_claim_workers(path: os.PathLike[str] | str | None = None) -> tuple[tuple[int, object], ...]:
    """``(worker_pid, worker_started_at)`` for every LIVE claim on *path*'s board.

    LIVE is the dispatcher's own reading: ``status = 'running'`` with an unexpired
    ``claim_expires`` and a recorded ``worker_pid``. Read-only and bounded; any failure answers
    ``()`` — this arm may only ADD a refusal it can prove.
    """
    import sqlite3

    store = _resolve_claim_store(path)
    if not store:
        return ()
    now = time.monotonic()
    cached = _live_claim_cache.get(store)
    if cached is not None and (now - cached[0]) <= LIVE_CLAIM_PROBE_TTL_SECONDS:
        return cached[1]
    rows: tuple[tuple[int, object], ...] = ()
    try:
        conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True, timeout=1.0)
        try:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT worker_pid, worker_started_at FROM tasks "
                "WHERE status = 'running' AND worker_pid IS NOT NULL "
                "  AND claim_expires IS NOT NULL AND claim_expires > ?",
                (int(time.time()),),
            )
            rows = tuple(
                (int(r["worker_pid"]), r["worker_started_at"])
                for r in cur.fetchall() if r["worker_pid"]
            )
        finally:
            conn.close()
    except Exception:  # health: allow BLE001 -- fail-open (t_fcf7a321): an unreadable board DB answers "no live claims" and can only fail to ADD a refusal
        rows = ()
    _live_claim_cache[store] = (now, rows)
    return rows


def ancestor_owns_live_kanban_claim(path: os.PathLike[str] | str | None = None) -> bool:
    """The PROVENANCE arm of the fence (ruling t_fcf7a321, Decision 1).

    True when an ANCESTOR process of this one is the ``worker_pid`` of a LIVE claim on *path*'s
    board AND the recorded ``worker_started_at`` fingerprint still names that pid (the same
    PID-reuse guard the dispatcher trusts). Same-pid is never a match: a worker mutating
    IN-PROCESS is the ContextVar arm's job, not this one's — this arm exists for the SHELL a
    worker spawns. Reads are unaffected; callers gate only estate- mutating verbs.
    """
    ancestors = process_ancestors()
    if not ancestors:
        return False
    claims = live_claim_workers(path)
    if not claims:
        return False
    ancestor_set = set(ancestors)
    try:
        from hermes_cli.kanban_db_dispatch import _worker_not_dead
    except Exception:  # health: allow BLE001 -- fail-open (t_fcf7a321): without the dispatcher's own liveness check the claim is unprovable, so this arm stays silent
        return False
    for pid, started_at in claims:
        if pid in ancestor_set and _worker_not_dead(pid, started_at):
            return True
    return False


@overload
def delegated_child_subprocess_env(env: Mapping[str, str]) -> dict[str, str]: ...


@overload
def delegated_child_subprocess_env(env: None = None) -> dict[str, str] | None: ...


def delegated_child_subprocess_env(
    env: Mapping[str, str] | MutableMapping[str, str] | None = None,
) -> dict[str, str] | None:
    """Carry worker/delegate descendant denial across a real process spawn.

    Location and credentials are untouched; callers retain their existing secret policy.
    Dispatcher workers and supervised tool transports grant their own explicit scope.
    """
    if not (is_delegated_child_process_context() or os.environ.get("HERMES_KANBAN_TASK")
            or (env and (env.get("HERMES_KANBAN_TASK") or env.get(DELEGATED_CHILD_ENV_MARKER)))):
        return None if env is None else dict(env)
    return scrub_kanban_env(os.environ if env is None else env)
