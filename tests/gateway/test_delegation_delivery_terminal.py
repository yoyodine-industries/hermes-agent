"""A durable completion whose target can never be persisted must end terminal, not drain forever.

The ``messages`` FK can never be satisfied for an api_server target with no ``sessions`` row, so
every drain re-runs the same impossible INSERT: the watcher must answer the event once (terminal),
settle its durable row, and stop. A retryable failure class that never resolves is bounded by the
requeue budget so no future error class can spin the drain either.
"""
import asyncio
import logging
import queue
import time
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.run import GatewayRunner
from hermes_state import SessionDB
from tools import async_delegation as delegation

#: Raw X-Hermes-Session-Id key of an api_server session that has no transcript row.
RAW_SID = "run_gone_target_0001"


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path, monkeypatch):
    """The drain reads tools.process_registry: keep the registry and its queue in tmp state."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import tools.process_registry as pr_module

    monkeypatch.setattr(pr_module, "CHECKPOINT_PATH", tmp_path / "processes.json")
    registry = pr_module.ProcessRegistry()
    monkeypatch.setattr(pr_module, "process_registry", registry)
    return registry


def _stop_after_sleeps(monkeypatch, runner, count):
    sleep_calls = 0

    async def _bounded_sleep(_delay):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= count:
            runner._running = False

    monkeypatch.setattr(asyncio, "sleep", _bounded_sleep)


def _completion(delegation_id: str) -> dict:
    return {
        "type": "async_delegation", "session_key": RAW_SID, "origin_session_id": RAW_SID,
        "delegation_id": delegation_id, "summary": delegation_id, "status": "completed",
        "dispatched_at": time.time(),
    }


def _runner(tmp_path):
    """Real runner, real api_server adapter, session DB with no session rows for RAW_SID."""
    runner = GatewayRunner(GatewayConfig())
    db = SessionDB(tmp_path / "api.db")
    adapter = APIServerAdapter(PlatformConfig())
    setattr(adapter, "_ensure_session_db", lambda: db)
    runner.adapters = {Platform.API_SERVER: adapter}
    runner._running = True
    return runner, adapter, db


@pytest.mark.asyncio
async def test_unpersistable_completion_is_terminal_and_logged_once(tmp_path, caplog):
    """The delivery seam answers the sentinel (not a retry) and names the session exactly once."""
    from gateway.run_notifications import TERMINAL_DELIVERY  # the sentinel the seam must answer with

    runner, adapter, db = _runner(tmp_path)
    caplog.set_level(logging.WARNING, logger="gateway.run")
    evt = _completion("terminal-direct")
    try:
        assert await runner._self_post_api_server(adapter, "RESULT", RAW_SID, evt) == TERMINAL_DELIVERY
        records = [r for r in caplog.records if RAW_SID in r.getMessage()]
        assert [r.levelname for r in records] == ["WARNING"]
        assert "no transcript row exists" in records[0].getMessage()
        assert db.get_messages(RAW_SID) == []
    finally:
        await runner._cancel_process_completion_batch_tasks()


@pytest.mark.asyncio
async def test_unpersistable_completion_settles_dropped_and_never_replays(
    tmp_path, caplog, monkeypatch, isolated_registry,
):
    """End to end through the watcher: one drain settles ``dropped``, the next has nothing to do."""
    runner, _adapter, _db = _runner(tmp_path)
    evt = _completion("terminal-e2e")
    delegation._persist_dispatch(evt)
    delegation._persist_completion(evt, {"status": "completed", "summary": evt["summary"]})
    assert delegation.get_durable_delegation(evt["delegation_id"])["delivery_state"] == "pending"
    isolated = queue.Queue()
    monkeypatch.setattr(isolated_registry, "completion_queue", isolated)
    isolated.put(evt)
    caplog.set_level(logging.WARNING, logger="gateway.run")
    try:
        _stop_after_sleeps(monkeypatch, runner, count=2)
        await runner._async_delegation_watcher(interval=0)
        # Answered, not retried: nothing left on the queue and the durable row is settled.
        assert isolated.empty()
        assert delegation.get_durable_delegation(evt["delegation_id"])["delivery_state"] == "dropped"
        assert len([r for r in caplog.records if RAW_SID in r.getMessage()]) == 1
        caplog.clear()
        runner._running = True  # re-arm: the sleep stub clears it to end one watcher pass
        _stop_after_sleeps(monkeypatch, runner, count=2)
        await runner._async_delegation_watcher(interval=0)
        assert not caplog.records
        assert isolated.empty()
        assert delegation.restore_undelivered_completions(queue.Queue()) == 0
        assert delegation.get_durable_delegation(evt["delegation_id"])["delivery_state"] == "dropped"
    finally:
        await runner._cancel_process_completion_batch_tasks()


@pytest.mark.asyncio
async def test_a_retryable_failure_is_bounded_by_the_requeue_budget(tmp_path, caplog, monkeypatch, isolated_registry):
    """No unclassifiable error class may spin the drain forever: the budget settles it as dropped."""
    from gateway import run_notifications

    runner, _adapter, _db = _runner(tmp_path)
    runner.adapters = {}  # no route at all: a plain retryable ``False`` every pass
    evt = _completion("terminal-budget")
    delegation._persist_dispatch(evt)
    delegation._persist_completion(evt, {"status": "completed", "summary": evt["summary"]})
    isolated = queue.Queue()
    monkeypatch.setattr(isolated_registry, "completion_queue", isolated)
    caplog.set_level(logging.WARNING, logger="gateway.run")
    try:
        isolated.put(evt)
        passes = 0
        # Safety bound: without the fix the event requeues forever, so cap the loop past the budget.
        while not isolated.empty() and passes <= run_notifications._MAX_COMPLETION_REQUEUES + 3:
            runner._running = True  # re-arm: the sleep stub clears it to end one watcher pass
            _stop_after_sleeps(monkeypatch, runner, count=2)
            await runner._async_delegation_watcher(interval=0)
            passes += 1
        assert isolated.empty()  # dropped, not requeued forever
        assert passes <= run_notifications._MAX_COMPLETION_REQUEUES
        dropped = [r for r in caplog.records if "delivery dropped after" in r.getMessage()]
        assert len(dropped) == 1 and RAW_SID in dropped[0].getMessage()
        assert delegation.get_durable_delegation(evt["delegation_id"])["delivery_state"] == "dropped"
        assert runner._completion_delivery_requeues == {}  # bounded: the identity goes with the drop
    finally:
        await runner._cancel_process_completion_batch_tasks()
