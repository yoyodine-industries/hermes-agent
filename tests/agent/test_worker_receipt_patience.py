"""A busy owner must never destroy a completed turn.

Measured fleet-wide 2026-11-14..22: every lane's managed worker died at the save step with
``WorkerPersistenceError: worker_receipt_timeout`` raised from ``flush_token_counts``. The
receipt budget is 20s and the owner's event loop can stall past it under load, so ONE missed
ACK made the store's failure sticky for the rest of the turn; the close-time flush then turned
it into a dead worker, the owner recorded ``worker_lost``, and the card was re-dispatched as if
nothing had run.

Two contracts are pinned here:

* a ``worker.persist`` receipt is re-asked inside a bounded, measured window (the replay is
  idempotent on ``(execution, sequence, digest)``, so it resolves the receipt the first copy
  committed instead of booking a second write);
* ``flush_token_counts`` keeps the in-process contract - bounded wait, ``False`` on timeout,
  never raises for a merely busy owner - and leaves the unacknowledged entries in the outbox.
"""
import json
import types

import pytest

from agent import runtime_session_store as worker_store
from agent.runtime_session_store import RuntimeSessionStore, WorkerPersistenceError, WorkerRPC

SCOPE = {'session_id': 's', 'execution_id': 'w', 'epoch': 1}


class _Link:
    """A worker RPC link whose receipts follow a scripted sequence."""

    def __init__(self, script):
        self.script = list(script)
        self.sent = []
        self.ws = types.SimpleNamespace(close=lambda: None)

    def send(self, request_id, message):
        self.sent.append(json.loads(message))

    def receive(self, timeout):
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, BaseException):
            raise step
        return step


def _rpc(monkeypatch, script):
    rpc = WorkerRPC('/nonexistent-home')
    link = _Link(script)
    monkeypatch.setattr(rpc, '_connection', lambda: link)
    monkeypatch.setattr(worker_store, 'RECEIPT_BACKOFF_SECONDS', (0.01,))
    return rpc, link


def test_receipt_timeout_is_re_asked_and_resolves(monkeypatch, caplog):
    reply = {'jsonrpc': '2.0', 'id': 1, 'result': {'ok': True}}
    rpc, link = _rpc(monkeypatch, [TimeoutError('worker_receipt_timeout'), reply])
    monkeypatch.setattr(worker_store, 'RECEIPT_PATIENCE_SECONDS', 5.0)

    assert rpc('worker.persist', sequence=1, operation='usage.main', payload={}) == {'ok': True}
    assert len(link.sent) == 2
    # The replay is the SAME request - same id, same sequence, same payload - so the owner's
    # (execution, sequence, digest) receipt resolves it instead of booking a second write.
    assert link.sent[0] == link.sent[1]
    assert 're-asking' in caplog.text


def test_busy_owner_past_the_patience_raises_a_classified_timeout(monkeypatch, caplog):
    rpc, link = _rpc(monkeypatch, [TimeoutError('worker_receipt_timeout')])
    monkeypatch.setattr(worker_store, 'RECEIPT_PATIENCE_SECONDS', 0.02)

    with pytest.raises(TimeoutError, match='worker_receipt_timeout'):
        rpc('worker.persist', sequence=1, operation='usage.main', payload={})
    assert len(link.sent) >= 2
    assert 'did not ACK' in caplog.text


def test_a_non_idempotent_method_is_never_re_asked(monkeypatch):
    rpc, link = _rpc(monkeypatch, [TimeoutError('worker_receipt_timeout')])
    monkeypatch.setattr(worker_store, 'RECEIPT_PATIENCE_SECONDS', 60.0)

    with pytest.raises(TimeoutError, match='worker_receipt_timeout'):
        rpc('worker.register', kind='compute')
    assert len(link.sent) == 1


def test_flush_drains_a_receipt_the_owner_acknowledges_late(tmp_path):
    calls = []

    def busy_then_ok(method, **params):
        calls.append(params)
        if len(calls) == 1:
            raise TimeoutError('worker_receipt_timeout')
        return {'count': 1, 'annotations': []}

    store = RuntimeSessionStore(busy_then_ok, SCOPE, tmp_path / 'private')
    try:
        with pytest.raises(TimeoutError):
            store.append_messages_batch('s', [{'role': 'user', 'content': 'x'}])
        assert store.failure == 'worker_receipt_timeout'
        assert store.flush_token_counts(timeout=2.0) is True
        assert store.failure is None
        assert json.loads(store.path.read_text())['pending'] == []
        assert calls[0]['sequence'] == calls[1]['sequence'] == 1
    finally:
        store.close()


def test_a_store_that_stays_busy_is_reported_not_raised_and_keeps_its_outbox(tmp_path):
    def always_busy(method, **params):
        raise TimeoutError('worker_receipt_timeout')

    store = RuntimeSessionStore(always_busy, SCOPE, tmp_path / 'private')
    with pytest.raises(TimeoutError):
        store.append_messages_batch('s', [{'role': 'user', 'content': 'kept'}])

    assert store.flush_token_counts(timeout=0.05) is False
    assert store.failure == 'worker_receipt_timeout'
    retained = json.loads(store.path.read_text())['pending']
    assert len(retained) == 1
    assert retained[0]['payload']['messages'][0]['content'] == 'kept'

    store.close()  # the completed turn is not the casualty: close must not raise
    assert json.loads(store.path.read_text())['pending'] == retained

    # The outbox a later drain replays - nothing is lost, nothing is silently re-queued.
    seen = []

    def ok(method, **params):
        seen.append(params)
        return {'count': 1, 'annotations': []}

    reopened = RuntimeSessionStore(ok, SCOPE, tmp_path / 'private')
    try:
        assert json.loads(reopened.path.read_text())['pending'] == []
        assert seen[0]['payload']['messages'][0]['content'] == 'kept'
    finally:
        reopened.close()


def test_a_permanent_failure_still_raises_as_persistence_error(tmp_path):
    def broken(method, **params):
        raise ValueError('permission_denied')

    store = RuntimeSessionStore(broken, SCOPE, tmp_path / 'private')
    try:
        with pytest.raises(ValueError):
            store.append_messages_batch('s', [{'role': 'user', 'content': 'x'}])
        with pytest.raises(WorkerPersistenceError, match='permission_denied'):
            store.flush_token_counts(timeout=0.05)
    finally:
        pass
