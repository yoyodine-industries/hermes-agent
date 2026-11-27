"""Dedicated bounded executor for session-store persistence.

WHY THIS EXISTS (measured, not inferred)
----------------------------------------
One gateway process (the "owner") serves every profile on the host, so a secondary's save is an
RPC to it. Every session-store call and every ``worker.persist`` receipt was offloaded with
``asyncio.to_thread(...)`` — that is the event loop's DEFAULT executor, a pool every other blocking
call in the process can saturate (a legacy mirror rewrite, an fsync, a plugin's blocking probe).
When the pool is busy the owner cannot answer a receipt inside the worker's 20 s budget:

    worker_receipt_timeout: owner busy -> append_message failed: owner_unavailable
      -> persistence_degraded -> "Hermes couldn't save this conversation"

The same failure lands in the same second across profiles with independent state.db files, which is
what a single shared blocker produces and what no per-profile threshold can produce.

THE SHAPE (this module is the single call site)
-----------------------------------------------
1. Session-store work runs on ONE process-wide executor with its own named threads. Nothing else
   shares it, so unrelated blocking calls cannot starve a save.
2. A ticket's future resolves only after the write function RETURNS — and the write functions here
   commit their SQLite transaction before returning (``SessionDB._execute_write``), so the receipt
   the worker waits for is answered after the durable write, never before. There is no early ack
   anywhere in ``worker.persist``: the JSON-RPC reply is built from the handler's return value.
3. Backpressure is explicit. The queue is BOUNDED; a full queue raises ``SessionStoreRefused`` with
   the code, the queue depth and the oldest queued write's age — never an unbounded queue, never a
   silent drop, never a hang.
4. Every wait has a deadline (``DEFAULT_DEADLINE_S``, under the worker's 20 s receipt budget), so one
   wedged write cannot hold the loop or the receipt forever. A deadline is NOT a cancellation: the
   thread keeps running and may still commit. The refusal says so (``settled=None``); callers must
   treat it as "settlement unknown" and re-ask idempotently, which the persist protocol already does
   (receipts dedupe on execution/sequence/digest).
5. ONE path for owner and secondaries: a secondary's ``worker.persist`` is served by the owner
   through this module, and the owner's own store writes go through this module too
   (``AsyncSessionStore`` -> ``run_session_write``). There is no second implementation.

ROLLBACK
--------
``gateway.session_store_executor: "default"`` in the root config (or
``HERMES_SESSION_STORE_EXECUTOR=default``) restores the previous behaviour exactly — every call
goes back to ``asyncio.to_thread`` on the default executor. It is one call site
(``run_session_write``), read once per process at first use.

HONEST LIMIT
------------
This removes the shared-pool and receipt-timing blocker. It does not remove synchronous SQLite work
that other code runs directly on the event loop; a stall caused by that work is a different defect.
"""

from __future__ import annotations

import asyncio
import collections
import contextvars
import logging
import os
import queue
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable, Optional

logger = logging.getLogger("gateway.session_executor")

# Sized for "a few concurrent turns, each writing once", not for throughput: a queue that never
# fills would hide the backpressure this module exists to make explicit.
DEFAULT_WORKERS = 4
DEFAULT_QUEUE_DEPTH = 32
# Must sit UNDER the worker's receipt budget (agent/runtime_session_store.RECEIPT_BUDGET_SECONDS =
# 20 s) so a refusal is *named* to the caller instead of surfacing later as a bare owner timeout.
DEFAULT_DEADLINE_S = 15.0
DEFAULT_ATTEMPTS = 2
RETRY_BACKOFF_S = 0.05
SLOW_WAIT_MS = 100.0
SLOW_WRITE_MS = 500.0

# Refusal codes. Named, stable, and compared as strings on the wire.
SATURATED = "session_store_saturated"
DEADLINE = "session_store_deadline"
CLOSED = "session_store_closed"


class SessionStoreRefused(RuntimeError):
    """A named refusal from the session-store executor.

    Every field a reader needs to act without guessing: which write was refused, how deep the queue
    was, how long the oldest queued write had been waiting, and — for a deadline — whether the write
    may still commit (``settled`` is None when settlement is unknown).
    """

    def __init__(self, code: str, *, task: str, queue_depth: int, wait_ms: float,
                 workers: int, settled: Optional[bool] = None, detail: str = "") -> None:
        self.code = code
        self.task = task
        self.queue_depth = queue_depth
        self.wait_ms = wait_ms
        self.workers = workers
        # None = settlement unknown (a deadline fired mid-write); False = nothing ran.
        self.settled = settled
        self.detail = detail
        super().__init__(
            f"{code}: task={task} queue_depth={queue_depth} wait={wait_ms:.0f}ms "
            f"workers={workers} settled={settled}{(' ' + detail) if detail else ''}"
        )

    @property
    def retryable(self) -> bool:
        """A refusal that proved nothing ran may be retried at once; a deadline may NOT be
        resubmitted blindly (the first attempt may have committed), only re-asked idempotently."""
        return self.settled is False


class _Ticket:
    __slots__ = ("args", "context", "enqueued_at", "fn", "future", "kwargs", "task")

    def __init__(self, future: Future, context, fn, args, kwargs, task: str) -> None:
        self.future = future
        self.context = context
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.enqueued_at = time.monotonic()
        self.task = task


class SessionStoreExecutor:
    """A bounded FIFO of session-store writes served by named daemon threads.

    A plain ``ThreadPoolExecutor`` will not do: its work queue is UNBOUNDED, so a slow store accepts
    work forever and the caller learns nothing until its receipt budget expires. Here ``put_nowait``
    against a fixed-size queue is the backpressure point.
    """

    def __init__(self, *, workers: int = DEFAULT_WORKERS, queue_depth: int = DEFAULT_QUEUE_DEPTH,
                 name: str = "hermes-session-store",
                 deadline_s: float = DEFAULT_DEADLINE_S,
                 attempts: int = DEFAULT_ATTEMPTS) -> None:
        self._workers = max(1, int(workers))
        self._queue_depth = max(1, int(queue_depth))
        self._name = name
        self._deadline_s = float(deadline_s)
        self._attempts = max(1, int(attempts))
        self._queue: queue.Queue[Optional[_Ticket]] = queue.Queue(maxsize=self._queue_depth)
        self._ages: collections.deque[float] = collections.deque()
        self._stats_lock = threading.Lock()
        self._stats: collections.Counter = collections.Counter()
        self._max_wait_ms = 0.0
        self._max_run_ms = 0.0
        self._closed = False
        self._threads: list[threading.Thread] = []
        for index in range(self._workers):
            # Daemon: a wedged write must never hold interpreter exit. Durability is the worker's
            # own journal's job (an unacknowledged persist is retained and re-asked), not ours.
            thread = threading.Thread(target=self._serve, name=f"{name}-{index}", daemon=True)
            thread.start()
            self._threads.append(thread)

    # -- introspection ---------------------------------------------------------------------
    @property
    def name(self) -> str:
        return self._name

    @property
    def workers(self) -> int:
        return self._workers

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    def oldest_wait_ms(self) -> float:
        with self._stats_lock:
            head = self._ages[0] if self._ages else None
        return 0.0 if head is None else (time.monotonic() - head) * 1000.0

    def snapshot(self) -> dict[str, Any]:
        """Counters + queue state, for a harness or a status line. Never raises."""
        with self._stats_lock:
            stats = dict(self._stats)
            max_wait, max_run = self._max_wait_ms, self._max_run_ms
        return {
            "name": self._name,
            "workers": self._workers,
            "queue_capacity": self._queue_depth,
            "queue_depth": self._queue.qsize(),
            "oldest_wait_ms": round(self.oldest_wait_ms(), 1),
            "max_wait_ms": round(max_wait, 1),
            "max_run_ms": round(max_run, 1),
            "submitted": stats.get("submitted", 0),
            "completed": stats.get("completed", 0),
            "failed": stats.get("failed", 0),
            "saturated": stats.get("saturated", 0),
            "deadlines": stats.get("deadlines", 0),
        }

    # -- submission ------------------------------------------------------------------------
    def submit(self, fn: Callable[..., Any], /, *args, task: Optional[str] = None,
               **kwargs) -> Future:
        """Queue *fn* and return its future. Raises ``SessionStoreRefused`` when the queue is full.

        The caller's contextvars are captured HERE (the submitter's thread), because the store
        resolves the owning profile's state.db from the context-local HERMES_HOME.
        """
        if self._closed:
            raise SessionStoreRefused(CLOSED, task=task or _task_name(fn), queue_depth=0,
                                      wait_ms=0.0, workers=self._workers, settled=False)
        future: Future = Future()
        ticket = _Ticket(future, contextvars.copy_context(), fn, args, kwargs, task or _task_name(fn))
        try:
            self._queue.put_nowait(ticket)
        except queue.Full:
            with self._stats_lock:
                self._stats["saturated"] += 1
            raise SessionStoreRefused(
                SATURATED, task=ticket.task, queue_depth=self._queue.qsize(),
                wait_ms=self.oldest_wait_ms(), workers=self._workers, settled=False,
                detail=f"queue_capacity={self._queue_depth}",
            ) from None
        with self._stats_lock:
            self._stats["submitted"] += 1
            self._ages.append(ticket.enqueued_at)
        return future

    async def run(self, fn: Callable[..., Any], /, *args, task: Optional[str] = None,
                  deadline_s: Optional[float] = None, attempts: Optional[int] = None,
                  **kwargs) -> Any:
        """Await one write, with explicit backpressure and a hard deadline.

        A saturation refusal proves nothing ran, so it is retried (bounded by ``attempts`` and the
        deadline). A deadline is NOT retried: the attempt may have committed, and resubmitting a
        write whose settlement is unknown is how a "slow write" becomes a duplicate one.
        """
        label = task or _task_name(fn)
        budget = self._deadline_s if deadline_s is None else float(deadline_s)
        tries = self._attempts if attempts is None else max(1, int(attempts))
        started = time.monotonic()
        last_refusal: Optional[SessionStoreRefused] = None
        for attempt in range(1, tries + 1):
            remaining = budget - (time.monotonic() - started)
            if remaining <= 0:
                break
            try:
                future = self.submit(fn, *args, task=label, **kwargs)
            except SessionStoreRefused as exc:
                last_refusal = exc
                if attempt >= tries:
                    raise
                # Nothing ran: wait a beat and re-queue while the deadline allows.
                await asyncio.sleep(min(RETRY_BACKOFF_S * attempt, max(remaining / 4, 0.001)))
                continue
            # asyncio.wait, NOT wait_for/wrap_future+timeout: a deadline must abandon the WAIT,
            # never the write. ``wait_for`` cancels the wrapped future on timeout, and a cancelled
            # future makes the serving thread's ``set_result`` raise InvalidStateError — which
            # would kill an executor worker at the exact moment the pool is already under load.
            wrapped = asyncio.wrap_future(future)
            done, _pending = await asyncio.wait({wrapped}, timeout=remaining)
            if done:
                return wrapped.result()
            _abandon(wrapped)
            with self._stats_lock:
                self._stats["deadlines"] += 1
            waited = (time.monotonic() - started) * 1000.0
            logger.warning(
                "session-store executor: %s exceeded its %.1fs deadline (waited %.0f ms, "
                "queue %d/%d); the write may still commit — re-ask it idempotently",
                label, budget, waited, self._queue.qsize(), self._queue_depth,
            )
            raise SessionStoreRefused(
                DEADLINE, task=label, queue_depth=self._queue.qsize(), wait_ms=waited,
                workers=self._workers, settled=None,
                detail=f"deadline={budget:.1f}s",
            )
        if last_refusal is not None:
            raise last_refusal
        raise SessionStoreRefused(DEADLINE, task=label, queue_depth=self._queue.qsize(),
                                  wait_ms=(time.monotonic() - started) * 1000.0,
                                  workers=self._workers, settled=False)

    # -- worker side -----------------------------------------------------------------------
    def _serve(self) -> None:
        while True:
            ticket = self._queue.get()
            if ticket is None:  # shutdown sentinel
                return
            wait_ms = (time.monotonic() - ticket.enqueued_at) * 1000.0
            with self._stats_lock:
                if self._ages:
                    self._ages.popleft()  # FIFO dequeue: the head IS this ticket's stamp
            started = time.monotonic()
            error: Optional[BaseException] = None
            try:
                result = ticket.context.run(ticket.fn, *ticket.args, **ticket.kwargs)
            except BaseException as exc:  # handed to the future, never swallowed here
                error = exc
                try:
                    ticket.future.set_exception(exc)
                except Exception:  # a waiter that gave up must never kill this worker
                    logger.debug("session-store executor: %s finished after its waiter left (error)",
                                 ticket.task)
            else:
                # The store's writes commit inside ``fn``; resolving here is what makes the
                # caller's receipt post-commit rather than post-enqueue.
                try:
                    ticket.future.set_result(result)
                except Exception:  # cancelled waiter: the write committed, the reply is gone
                    logger.info("session-store executor: %s committed after its waiter left",
                                ticket.task)
            run_ms = (time.monotonic() - started) * 1000.0
            self._record(ticket.task, wait_ms, run_ms, error)

    def _record(self, task: str, wait_ms: float, run_ms: float,
                error: Optional[BaseException]) -> None:
        with self._stats_lock:
            self._stats["failed" if error is not None else "completed"] += 1
            self._max_wait_ms = max(self._max_wait_ms, wait_ms)
            self._max_run_ms = max(self._max_run_ms, run_ms)
        # Same instrument shape as the mirror line: the cost is logged where it is paid, so a
        # contention report can quote it instead of inferring it from a later timeout.
        line = "session-store executor: %s ran %.1f ms (queued %.1f ms) on %s"
        if error is not None:
            logger.warning(line + " -> %s", task, run_ms, wait_ms, self._name, type(error).__name__)
        elif wait_ms >= SLOW_WAIT_MS or run_ms >= SLOW_WRITE_MS:
            logger.info(line, task, run_ms, wait_ms, self._name)
        else:
            logger.debug(line, task, run_ms, wait_ms, self._name)

    def close(self, *, wait: bool = True) -> None:
        """Stop accepting work and stop the threads (tests and clean shutdown)."""
        self._closed = True
        for _ in self._threads:
            try:
                self._queue.put_nowait(None)
            except queue.Full:  # a full queue is drained by the workers; nudge once more
                self._queue.put(None)
        if wait:
            for thread in self._threads:
                thread.join(5.0)


def _task_name(fn: Callable[..., Any]) -> str:
    return getattr(fn, "__qualname__", None) or getattr(fn, "__name__", None) or repr(fn)


#: Wrapped futures whose waiter timed out but whose write may still commit. Held until they
#: settle so the eventual result is not lost in a GC warning; the write is what matters, and the
#: caller has already been told its settlement is unknown.
_abandoned: set = set()


def _abandon(future) -> None:
    _abandoned.add(future)
    future.add_done_callback(_abandoned.discard)


# -- process-wide instance ------------------------------------------------------------------
_executor: Optional[SessionStoreExecutor] = None
_executor_lock = threading.Lock()
# "dedicated" (default) or "default" (rollback: asyncio.to_thread, i.e. the pre-change behaviour).
_mode: Optional[str] = None
_deadline_s = DEFAULT_DEADLINE_S


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def configure(config: Any = None) -> str:
    """Resolve the mode/sizing ONCE per process; later calls are no-ops.

    Order: ``HERMES_SESSION_STORE_EXECUTOR`` env > ``gateway.session_store_executor`` in the
    gateway config > ``"dedicated"``. The env var exists so the rollback is reachable without a
    config edit; the config key exists so it survives a restart without a shell.
    """
    global _mode, _deadline_s
    if _mode is not None:
        return _mode
    with _executor_lock:
        if _mode is not None:
            return _mode
        mode = str(os.environ.get("HERMES_SESSION_STORE_EXECUTOR", "") or "").strip().lower()
        if mode not in ("dedicated", "default"):
            mode = str(getattr(config, "session_store_executor", "") or "").strip().lower()
        _mode = mode if mode in ("dedicated", "default") else "dedicated"
        _deadline_s = _env_float("HERMES_SESSION_STORE_DEADLINE_S", DEFAULT_DEADLINE_S)
        logger.info(
            "session-store persistence: mode=%s workers=%s queue=%s deadline=%.1fs",
            _mode, _env_int("HERMES_SESSION_STORE_WORKERS", DEFAULT_WORKERS),
            _env_int("HERMES_SESSION_STORE_QUEUE", DEFAULT_QUEUE_DEPTH), _deadline_s,
        )
        return _mode


def session_store_executor() -> SessionStoreExecutor:
    """The one process-wide executor (created on first use with the resolved sizing)."""
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                configure()
                _executor = SessionStoreExecutor(
                    workers=_env_int("HERMES_SESSION_STORE_WORKERS", DEFAULT_WORKERS),
                    queue_depth=_env_int("HERMES_SESSION_STORE_QUEUE", DEFAULT_QUEUE_DEPTH),
                    deadline_s=_deadline_s,
                )
    return _executor


def executor_mode() -> str:
    return _mode or configure()


def reset_for_tests() -> None:
    """Drop the process-wide instance and mode (test isolation only)."""
    global _executor, _mode
    with _executor_lock:
        executor, _executor = _executor, None
        _mode = None
    if executor is not None:
        executor.close()


async def run_session_write(fn: Callable[..., Any], /, *args, task: Optional[str] = None,
                            **kwargs) -> Any:
    """THE single call site: run one session-store write off the event loop.

    Owner and secondaries both arrive here — a secondary's ``worker.persist`` is served by the
    owner's ``gateway.session_worker``, which calls this; the owner's own ``AsyncSessionStore``
    calls this. ``mode="default"`` falls back to ``asyncio.to_thread`` (the pre-change behaviour).
    """
    if executor_mode() == "default":
        return await asyncio.to_thread(fn, *args, **kwargs)
    return await session_store_executor().run(fn, *args, task=task, **kwargs)
