"""A secondary profile's restored completion must drain in its OWNING profile's scope.

Root-scope drains (the supervised watcher and the startup ledger restore) resolved the target
session, the durable ledger and the delivery target from the ambient (default-profile) scope, so a
secondary profile's completion was looked up in the DEFAULT profile's state.db, classified
terminal, dropped, and its own ledger row stayed ``pending`` forever — the client never saw the
result. The restore path stamps the row's home in memory (``_owner_profile_home``, alongside
``restored``) and the drain must honor it; nothing else may redirect the lookup.
"""
import asyncio
import time

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.run import GatewayRunner
from hermes_state import SessionDB
from tools import async_delegation as delegation

#: Raw X-Hermes-Session-Id key of the api_server session owned by the SECONDARY profile.
SID = "run_secondary_owner_0001"
DID = "deleg-scope-owner-0001"


@pytest.fixture
def isolated_registry(tmp_path, monkeypatch):
    """The drain reads tools.process_registry: keep the registry and its queue in tmp state."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "root"))
    import tools.process_registry as pr_module

    monkeypatch.setattr(pr_module, "CHECKPOINT_PATH", tmp_path / "processes.json")
    registry = pr_module.ProcessRegistry()
    monkeypatch.setattr(pr_module, "process_registry", registry)
    return registry


def _homes(tmp_path, monkeypatch):
    """The launch (root) home plus a secondary profile home, each with its own state.db."""
    root = tmp_path / "root"
    secondary = root / "profiles" / "secondary"
    secondary.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root, secondary


def _seed_previous_process_completion(monkeypatch, secondary, registry):
    """Leave the secondary profile with a finished-but-undelivered completion, as a dead process did."""
    SessionDB(db_path=secondary / "state.db").create_session(SID, source="api_server")
    monkeypatch.setenv("HERMES_HOME", str(secondary))
    record = {
        "delegation_id": DID, "session_key": SID, "origin_session_id": SID,
        "goal": "work", "dispatched_at": time.time(),
    }
    delegation._persist_dispatch(record)
    delegation._push_completion_event(record, {"summary": "RESULT", "api_calls": 1}, "completed")
    live = registry.completion_queue.get_nowait()   # the enqueue the dead process never drained
    assert live["delegation_id"] == DID and not live.get("restored")


def _restored_event(monkeypatch, secondary, root, registry):
    """Restart replay: the restore path re-queues the row from the profile's OWN ledger."""
    monkeypatch.setenv("HERMES_HOME", str(secondary))
    assert delegation.restore_undelivered_completions(registry.completion_queue) == 1
    monkeypatch.setenv("HERMES_HOME", str(root))
    return registry.completion_queue.get_nowait()


def _runner():
    """Real runner and real api_server adapter: the adapter resolves its SessionDB per profile."""
    runner = GatewayRunner(GatewayConfig())
    runner.adapters = {Platform.API_SERVER: APIServerAdapter(PlatformConfig())}
    runner._running = True
    return runner


def _stop_after_sleeps(monkeypatch, runner, count):
    sleep_calls = 0

    async def _bounded_sleep(_delay):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= count:
            runner._running = False

    monkeypatch.setattr(asyncio, "sleep", _bounded_sleep)


async def _drain_once(monkeypatch, runner):
    """One watcher pass under the ROOT scope, where the supervised watcher actually runs."""
    runner._running = True   # re-arm: the sleep stub clears it to end one pass
    _stop_after_sleeps(monkeypatch, runner, count=2)
    await runner._async_delegation_watcher(interval=0)


def _ledger_state(monkeypatch, home, delegation_id):
    monkeypatch.setenv("HERMES_HOME", str(home))
    row = delegation.get_durable_delegation(delegation_id)
    return None if row is None else row["delivery_state"]


@pytest.mark.asyncio
async def test_restored_completion_drains_against_its_owning_profile(
    tmp_path, monkeypatch, isolated_registry,
):
    """The delivery lands in the owner's transcript and settles the owner's ledger row."""
    root, secondary = _homes(tmp_path, monkeypatch)
    _seed_previous_process_completion(monkeypatch, secondary, isolated_registry)
    evt = _restored_event(monkeypatch, secondary, root, isolated_registry)
    assert evt["restored"] is True
    assert evt["_owner_profile_home"] == str(secondary)

    runner = _runner()
    isolated_registry.completion_queue.put(evt)
    await _drain_once(monkeypatch, runner)

    assert isolated_registry.completion_queue.empty()   # answered once, never requeued
    owner_db = SessionDB(db_path=secondary / "state.db")
    try:
        rows = owner_db.get_messages(SID)
        assert [r["display_kind"] for r in rows] == ["async_delegation_complete"]
        assert "RESULT" in rows[0]["content"]
    finally:
        owner_db.close()
    default_db = SessionDB(db_path=root / "state.db")
    try:
        assert default_db.get_messages(SID) == []       # nothing leaked into the default profile
    finally:
        default_db.close()
    assert _ledger_state(monkeypatch, secondary, DID) == "delivered"


@pytest.mark.asyncio
async def test_an_owner_home_without_the_restore_stamp_is_ignored(
    tmp_path, monkeypatch, isolated_registry,
):
    """Only the restore path's own stamp redirects the lookup: an unstamped key is not evidence."""
    root, secondary = _homes(tmp_path, monkeypatch)
    _seed_previous_process_completion(monkeypatch, secondary, isolated_registry)
    evt = _restored_event(monkeypatch, secondary, root, isolated_registry)
    del evt["restored"]                 # a foreign/live event that merely claims an owner home

    runner = _runner()
    isolated_registry.completion_queue.put(evt)
    await _drain_once(monkeypatch, runner)

    owner_db = SessionDB(db_path=secondary / "state.db")
    try:
        assert owner_db.get_messages(SID) == []
    finally:
        owner_db.close()
    assert _ledger_state(monkeypatch, secondary, DID) == "pending"


@pytest.mark.asyncio
async def test_restored_completion_without_an_owner_stamp_is_not_redirected(
    tmp_path, monkeypatch, isolated_registry,
):
    """No stamp means no owner evidence: the drain keeps the pre-fix root-scope behavior."""
    root, secondary = _homes(tmp_path, monkeypatch)
    _seed_previous_process_completion(monkeypatch, secondary, isolated_registry)
    evt = _restored_event(monkeypatch, secondary, root, isolated_registry)
    evt.pop("_owner_profile_home", None)   # no owner evidence at all

    runner = _runner()
    isolated_registry.completion_queue.put(evt)
    await _drain_once(monkeypatch, runner)

    owner_db = SessionDB(db_path=secondary / "state.db")
    try:
        assert owner_db.get_messages(SID) == []
    finally:
        owner_db.close()
    assert _ledger_state(monkeypatch, secondary, DID) == "pending"
