"""The session-store executor's contract, pinned where it is decided.

Every session-store call and every ``worker.persist`` receipt used to run on
``asyncio.to_thread`` — the event loop's DEFAULT executor, shared with every other blocking
call in the process. One gateway owner serves all profiles, so when that pool was saturated
the owner could not answer any lane's receipt inside the worker's 20 s budget:

    worker_receipt_timeout: owner busy -> append_message failed: owner_unavailable
      -> persistence_degraded -> "Hermes couldn't save this conversation"

``gateway/session_executor.py`` replaces that with one process-wide, bounded, NAMED pool.
These tests pin the four promises the fix is judged on:

1. a ticket's future resolves only AFTER the write function returns (and the write functions
   commit inside themselves), so a receipt is never answered before the durable write;
2. a wedged write occupies its own worker and cannot stop a second write completing;
3. a full queue is a NAMED refusal carrying the queue depth and the wait — never a hang,
   never an unbounded queue, never a silent drop;
4. every wait has a deadline that says settlement is UNKNOWN (the write may still commit),
   and ``mode="default"`` restores the old behaviour for rollback.
"""
import asyncio
import threading
import time

import pytest

from gateway import session_executor
from gateway.session_executor import (
    DEADLINE, SATURATED, SessionStoreExecutor, SessionStoreRefused, reset_for_tests,
    run_session_write,
)
from hermes_state_runtime import RuntimeStoreError


@pytest.fixture(autouse=True)
def _isolated_executor(monkeypatch):
    """Every test gets its own process-wide state; nothing leaks between cases."""
    for name in ('HERMES_SESSION_STORE_EXECUTOR', 'HERMES_SESSION_STORE_WORKERS',
                 'HERMES_SESSION_STORE_QUEUE', 'HERMES_SESSION_STORE_DEADLINE_S'):
        monkeypatch.delenv(name, raising=False)
    reset_for_tests()
    yield
    reset_for_tests()


def _gate():
    """A write whose inside is controlled by the test: proves the wait, not a sleep, blocks."""
    started, release = threading.Event(), threading.Event()
    events = []

    def blocked_write(label):
        started.set()
        assert release.wait(10), 'test never released the blocked write'
        events.append((label, 'ran'))
        return label

    return started, release, blocked_write, events


def test_a_wedged_write_does_not_stop_another_write_reaching_the_store():
    started, release, blocked, events = _gate()
    executor = SessionStoreExecutor(workers=2, queue_depth=4, deadline_s=5.0)

    def other_write():
        events.append(('other', 'committed'))
        return 'ok'

    async def scenario():
        wedged = executor.submit(blocked, 'wedged', task='wedged')
        assert await asyncio.to_thread(started.wait, 5), 'the wedged write never started'
        # Same executor, different worker: the ordinary save still gets served.
        result = await executor.run(other_write, task='other')
        assert result == 'ok'
        assert ('other', 'committed') in events
        assert not wedged.done(), 'the wedged write must still be the one holding its worker'
        release.set()
        assert await asyncio.wait_for(asyncio.wrap_future(wedged), 5) == 'wedged'

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        executor.close()


def test_the_promise_resolves_after_the_write_returns_and_never_before():
    executor = SessionStoreExecutor(workers=1, queue_depth=2, deadline_s=5.0)
    observed = []

    def write(records):
        time.sleep(0.15)          # stand-in for a commit
        records.append('committed')

    async def scenario():
        task = asyncio.ensure_future(executor.run(write, observed, task='commit-shape'))
        await asyncio.sleep(0.05)
        assert observed == [], 'the ticket resolved before the write finished'
        assert not task.done(), 'the ticket resolved before the write finished'
        await task
        assert observed == ['committed']

    try:
        asyncio.run(scenario())
    finally:
        executor.close()


def test_a_full_queue_refuses_by_name_with_depth_and_wait_never_hangs():
    started, release, blocked, _ = _gate()
    executor = SessionStoreExecutor(workers=1, queue_depth=1, deadline_s=5.0)

    async def scenario():
        first = executor.submit(blocked, 'first', task='first')
        assert await asyncio.to_thread(started.wait, 5)
        executor.submit(blocked, 'queued', task='queued')   # fills the single queue slot
        refusal_started = time.monotonic()
        with pytest.raises(SessionStoreRefused) as caught:
            executor.submit(blocked, 'refused', task='refused')
        elapsed = time.monotonic() - refusal_started
        refusal = caught.value
        assert refusal.code == SATURATED
        assert refusal.task == 'refused'
        # The refusal names what a caller needs to act: how deep the queue was and how long the
        # oldest waiter had been waiting. Nothing was reserved (settled is False), so a retry
        # is safe and the caller is told so by ``retryable``.
        assert refusal.queue_depth == 1
        assert refusal.workers == 1
        assert refusal.wait_ms >= 0.0
        assert refusal.settled is False and refusal.retryable is True
        assert 'queue_depth=1' in str(refusal)
        assert elapsed < 0.5, f'a refusal must be immediate, took {elapsed:.3f}s'
        snapshot = executor.snapshot()
        assert snapshot['saturated'] == 1 and snapshot['queue_capacity'] == 1
        release.set()
        await asyncio.wait_for(asyncio.wrap_future(first), 5)

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        executor.close()


def test_a_passed_deadline_refuses_and_says_settlement_is_unknown():
    release, ran = threading.Event(), threading.Event()
    events = []
    executor = SessionStoreExecutor(workers=1, queue_depth=2, deadline_s=0.1)

    def write():
        ran.set()
        release.wait(5)
        events.append('committed')

    async def scenario():
        with pytest.raises(SessionStoreRefused) as caught:
            await executor.run(write, task='slow')
        refusal = caught.value
        assert refusal.code == DEADLINE
        # Not retryable in place: the write may still commit, so resubmitting blindly is how a
        # slow write becomes a duplicate one. The caller must re-ask idempotently instead.
        assert refusal.settled is None and refusal.retryable is False
        assert 'deadline=' in refusal.detail

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        executor.close()
    assert ran.is_set() and events == ['committed'], 'the deadline must not cancel the write itself'


def test_run_retries_a_saturation_refusal_while_the_deadline_allows():
    started, release, blocked, _ = _gate()
    executor = SessionStoreExecutor(workers=1, queue_depth=1, deadline_s=5.0, attempts=10)
    calls = []

    def small_write():
        calls.append('ran')
        return 'ok'

    async def scenario():
        first = executor.submit(blocked, 'first', task='first')
        assert await asyncio.to_thread(started.wait, 5)
        executor.submit(blocked, 'queued', task='queued')
        task = asyncio.ensure_future(executor.run(small_write, task='retried'))
        await asyncio.sleep(0.2)      # let the first attempt refuse and back off
        release.set()
        assert await asyncio.wait_for(task, 5) == 'ok'
        await asyncio.wait_for(asyncio.wrap_future(first), 5)
        assert calls == ['ran']

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        executor.close()


def test_worker_refusal_is_translated_into_the_wire_vocabulary_with_the_depth():
    """``worker.persist`` must surface a NAMED reason, not 'owner busy' or a bare timeout."""
    from gateway.session_worker import _durable_write

    started, release, blocked, _ = _gate()
    executor = SessionStoreExecutor(workers=1, queue_depth=1, deadline_s=5.0)
    original = session_executor._executor
    session_executor._executor = executor
    try:
        executor.submit(blocked, 'first', task='first')
        assert started.wait(5)
        executor.submit(blocked, 'queued', task='queued')

        async def scenario():
            with pytest.raises(RuntimeStoreError) as caught:
                await _durable_write(lambda: 'never', task='worker.persist.transcript.append')
            exc = caught.value
            assert exc.reason == SATURATED
            assert exc.field == 'session_store_executor'
            assert 'queue_depth=1' in exc.detail and 'task=worker.persist.transcript.append' in exc.detail
            assert exc.retry_after is not None

        asyncio.run(scenario())
    finally:
        release.set()
        session_executor._executor = original
        executor.close()


def test_async_session_store_offloads_to_the_dedicated_named_threads():
    from gateway.session import AsyncSessionStore

    threads = []

    class _Store:
        config = None

        def append_message(self, *args, **kwargs):
            threads.append(threading.current_thread().name)
            return 'stored'

    async def scenario():
        store = AsyncSessionStore(_Store())
        return await store.append_message('s', 'user', 'x')

    try:
        assert asyncio.run(scenario()) == 'stored'
    finally:
        reset_for_tests()
    assert threads and threads[0].startswith('hermes-session-store'), threads


def test_mirror_off_routing_save_through_the_pool_writes_no_mirror(tmp_path, caplog):
    """Ruling item (b): with `gateway.write_sessions_json: false` the save still commits to state.db
    and never pays the mirror rewrite (measured 277 ms/session creation: 274.8 ms dumps + 1.9 ms
    fsync). The mirror is derived data; state.db is the only primary.
    """
    from gateway.config import GatewayConfig, Platform
    from gateway.session import AsyncSessionStore, SessionSource, SessionStore

    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig(write_sessions_json=False))
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='chat-1', chat_name='Alice',
                           chat_type='dm', user_id='user-1')
    try:
        with caplog.at_level('INFO', logger='gateway.session'):
            async def scenario():
                return await AsyncSessionStore(store).get_or_create_session(source)

            entry = asyncio.run(scenario())
        assert entry is not None and entry.session_id
        assert not (tmp_path / 'sessions.json').exists()
        assert not [r for r in caplog.records if 'sessions.json mirror: wrote' in r.getMessage()]
    finally:
        store._db.close()


def test_mode_default_restores_the_pre_change_executor(monkeypatch):
    monkeypatch.setenv('HERMES_SESSION_STORE_EXECUTOR', 'default')
    reset_for_tests()
    seen = []

    def write():
        seen.append(threading.current_thread().name)
        return 'ok'

    async def scenario():
        return await run_session_write(write, task='rollback')

    try:
        assert asyncio.run(scenario()) == 'ok'
    finally:
        reset_for_tests()
    assert session_executor._executor is None, 'rollback must not create the dedicated pool'
    assert seen and not seen[0].startswith('hermes-session-store'), seen


def test_configure_reads_the_gateway_config_key_once(monkeypatch):
    class _Config:
        session_store_executor = 'default'

    assert session_executor.configure(_Config()) == 'default'
    # Sticky for the process: a later caller cannot flip a running executor underneath it.
    assert session_executor.configure(None) == 'default'


def test_close_on_a_full_queue_is_bounded_not_a_hang():
    """Shutdown must not wait forever for a queue slot: the join is bounded, the drain is best-effort."""
    started, release, blocked, _ = _gate()
    executor = SessionStoreExecutor(workers=1, queue_depth=1, deadline_s=5.0)
    executor.submit(blocked, 'first', task='first')
    assert started.wait(5)
    executor.submit(blocked, 'queued', task='queued')
    began = time.monotonic()
    executor.close()                      # the queue is full AND the only worker is wedged
    elapsed = time.monotonic() - began
    release.set()
    assert elapsed < 7.0, f'close() took {elapsed:.1f}s on a full queue'
