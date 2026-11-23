"""A transient owner descriptor is waited out instead of killing a save that was never attempted.

The worker asks a descriptor the multiplexer rewrites on every owner restart. Asking it once
meant a restart landing on a persistence call read as ``owner_unavailable``: the write never
reached SQLite, and the death spent the caller's retry budget on it (measured: six card deaths
in one night, none of them preceded by a write-lock event).
"""
import time
from types import SimpleNamespace

import pytest

from agent import runtime_session_store as store


def _discovery(*states):
    """Discovery answering the given states in order, then repeating the last one forever."""
    answers = list(states)
    calls = []

    def call(home, timeout):
        calls.append(time.monotonic())
        state = answers.pop(0) if len(answers) > 1 else answers[0]
        return SimpleNamespace(state=state, endpoint=(object() if state == 'ready' else None))

    return call, calls


@pytest.fixture
def rpc(tmp_path, monkeypatch):
    # Bounded for the test, not for production: the point is the SHAPE - retried, then bounded.
    monkeypatch.setattr(store, 'DISCOVERY_PATIENCE_SECONDS', 0.6)
    monkeypatch.setattr(store, 'DISCOVERY_BACKOFF_SECONDS', (0.05,))
    return store.WorkerRPC(tmp_path)


def _patch_discovery(monkeypatch, call):
    from hermes_cli import gateway_runtime
    monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint', call)


def test_a_descriptor_that_flips_to_ready_mid_budget_is_waited_out(rpc, monkeypatch):
    call, calls = _discovery('down', 'starting', 'ready')
    _patch_discovery(monkeypatch, call)

    rpc._await_owner()

    assert len(calls) == 3, 'the not-ready answers must be retried, not taken as final'
    assert rpc.endpoint is not None


def test_a_permanently_absent_owner_fails_bounded_and_names_the_cause(rpc, monkeypatch):
    call, calls = _discovery('down')
    _patch_discovery(monkeypatch, call)

    started = time.monotonic()
    with pytest.raises(store.WorkerPersistenceError) as excinfo:
        rpc._await_owner()
    elapsed = time.monotonic() - started

    assert 'owner_unavailable' in str(excinfo.value)
    assert len(calls) > 1, 'a transient and an absent owner must not be the same code path'
    assert elapsed < 10.0, 'patience is a bound, not a hang'
    assert rpc.endpoint is None


def test_a_draining_owner_keeps_the_endpoint_it_already_reached(rpc, monkeypatch):
    """Drain is not absence: an owner already dialled stays usable while it finishes."""
    reached = object()
    rpc.endpoint = reached
    call, _ = _discovery('draining')
    _patch_discovery(monkeypatch, call)

    rpc._await_owner()

    assert rpc.endpoint is reached
