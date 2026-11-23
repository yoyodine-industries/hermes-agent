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


class _Owner:
    """A scripted owner that answers PER METHOD, so a fixture can express a late ACK and a fast
    read without pretending the mutation was re-sent."""

    def __init__(self, receipts):
        self.receipts = {name: list(steps) for name, steps in receipts.items()}
        self.sent = []
        self.ws = types.SimpleNamespace(close=lambda: None)

    def send(self, request_id, message):
        self.sent.append(json.loads(message))

    def receive(self, timeout):
        request = self.sent[-1]
        steps = self.receipts[request['method']]
        step = steps.pop(0) if len(steps) > 1 else steps[0]
        if isinstance(step, BaseException):
            raise step
        return {**step, 'id': request['id']}

    def methods(self):
        return [request['method'] for request in self.sent]


def _owner(monkeypatch, receipts):
    rpc = WorkerRPC('/nonexistent-home')
    owner = _Owner(receipts)
    monkeypatch.setattr(rpc, '_connection', lambda: owner)
    monkeypatch.setattr(worker_store, 'RECEIPT_BACKOFF_SECONDS', (0.01,))
    return rpc, owner


ADOPT_SCOPE = {'profile_id': 'p', 'session_id': 's', 'execution_id': 'w', 'generation': 0,
               'pid': 424242, 'birth': 100.5, 'secret': 'x' * 32}
ADOPTED_ROW = {'execution_id': 'w', 'session_id': 's', 'kind': 'compute', 'owner_epoch': 7,
               'generation': 0, 'status': 'running', 'last_sequence': 0}


def _reply(result):
    return {'jsonrpc': '2.0', 'result': result}


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


def test_a_joined_worker_is_kept_when_its_adopt_ack_is_lost(monkeypatch, caplog):
    """L1: the 75s patience covers only replay-safe methods, so a non-replay-safe miss used to
    raise on the FIRST 20s budget - discarding a worker the owner had already joined. A late ACK
    plus an answering read must COMPLETE the turn, with the read named in the log."""
    rpc, owner = _owner(monkeypatch, {
        'worker.adopt': [TimeoutError('worker_receipt_timeout')],
        'worker.status': [_reply(ADOPTED_ROW)],
    })
    monkeypatch.setattr(worker_store, 'RECEIPT_PATIENCE_SECONDS', 5.0)

    assert rpc('worker.adopt', **ADOPT_SCOPE) == ADOPTED_ROW
    # The mutation went out ONCE; what followed was the read, carrying the same producer claim.
    assert owner.methods() == ['worker.adopt', 'worker.status']
    assert owner.sent[1]['params'] == ADOPT_SCOPE
    assert 'worker_receipt_confirmed_by_read' in caplog.text
    assert 'this receipt is lost' not in caplog.text


def test_a_lost_register_ack_confirmed_by_read_never_resends_the_claim(monkeypatch, caplog):
    rpc, owner = _owner(monkeypatch, {
        'worker.register': [TimeoutError('worker_receipt_timeout')],
        'worker.status': [_reply({**ADOPTED_ROW, 'status': 'registered'})],
    })
    monkeypatch.setattr(worker_store, 'RECEIPT_PATIENCE_SECONDS', 5.0)

    result = rpc('worker.register', kind='compute', **ADOPT_SCOPE)
    assert result['status'] == 'registered'
    assert owner.methods() == ['worker.register', 'worker.status']
    # `kind` is not part of the producer claim the read proves.
    assert owner.sent[1]['params'] == ADOPT_SCOPE


def test_a_read_that_says_not_committed_retries_once_then_fails_classified(monkeypatch, caplog):
    rpc, owner = _owner(monkeypatch, {
        'worker.adopt': [TimeoutError('worker_receipt_timeout')],
        'worker.status': [_reply({**ADOPTED_ROW, 'status': 'registered'})],
    })
    monkeypatch.setattr(worker_store, 'RECEIPT_PATIENCE_SECONDS', 5.0)
    from hermes_state_errors import classify_persistence_error

    with pytest.raises(TimeoutError, match='worker_receipt_timeout') as failure:
        rpc('worker.adopt', **ADOPT_SCOPE)
    # Exactly one retry of the mutation - the one case where re-issuing is idempotent.
    assert owner.methods().count('worker.adopt') == 2
    assert classify_persistence_error(failure.value) == 'receipt_timeout'
    assert 'never blindly' in caplog.text


def test_an_owner_that_answers_neither_the_call_nor_the_read_is_a_receipt_timeout(monkeypatch, caplog):
    """The read is the fallback, not a second wall: an owner silent for both must surface a
    CLASSIFIED receipt_timeout (harness loss), never a bare `unknown` - and still never a resend."""
    rpc, owner = _owner(monkeypatch, {
        'worker.register': [TimeoutError('worker_receipt_timeout')],
        'worker.status': [TimeoutError('worker_receipt_timeout')],
    })
    monkeypatch.setattr(worker_store, 'RECEIPT_PATIENCE_SECONDS', 0.35)
    from hermes_state_errors import classify_persistence_error

    with pytest.raises(TimeoutError, match='worker_receipt_timeout') as failure:
        rpc('worker.register', kind='compute', **ADOPT_SCOPE)
    assert classify_persistence_error(failure.value) == 'receipt_timeout'
    assert owner.methods()[0] == 'worker.register'
    assert set(owner.methods()[1:]) == {'worker.status'}  # the read may be re-asked; the claim never
    assert 'worker_receipt_read_unanswered' in caplog.text
    assert 'this receipt is lost' not in caplog.text


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
