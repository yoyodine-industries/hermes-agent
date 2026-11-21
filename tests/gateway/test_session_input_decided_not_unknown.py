"""A decided outcome is not `unknown`, and an undecided one is.

The session-input contract has three states a lost claim can be in, and only one of them is
`unknown`:

  * DECIDED - the owner that claimed it is gone (an epoch bump), so no process holds a result it
    could still publish. Leaving the row `unknown` minted a permanent per-target refusal out of an
    outcome already known: measured 2026-11-21 at roughly one row per 35 minutes across the ten
    stores (platform-worker's census), each one stranding its sender's queue behind a turn nobody
    can resend. The row ends `terminal`/`interrupted` with provenance, in the same transaction.
  * UNDECIDED, RESULT IN HAND - the settlement write failed but the live owner still holds the
    answer (`gateway/session_settlement_recovery.py`): `unknown` is correct and is the state
    `resolve_unknown_session_input` later commits the captured result from. Pinned below and by
    tests/gateway/test_live_settlement_recovery.py, which would fail if this were "decided" too.
  * UNDECIDED, ATTENDED - a person's turn lost across a restart: `unknown` stays, and the claim
    refuses with the row named, because only they can say whether to resend it.

Real ``SessionStore`` SQLite throughout; no store monkeypatching.
"""
import json

import pytest

from gateway.config import GatewayConfig
from gateway.session import SessionStore
from hermes_state_runtime import (RuntimeStoreError, admit_session_input, begin_runtime_epoch,
                                  claim_session_input, get_session_admission,
                                  recover_session_inputs)

PERSON = 'uid:502'


def _db(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    db = store._db
    db.create_session('s', source='gateway')
    return db


def _admit(db, epoch, *, finite, text):
    payload = {'text': text}
    if finite:
        payload['finite'] = True
    return admit_session_input(db, epoch=epoch, principal_id=PERSON, session_id='s',
                               request_id='req-%s' % text, payload=payload)


def _receipt(db, kind, admission_id):
    with db._read_ctx() as conn:
        row = conn.execute('SELECT value FROM state_meta WHERE key=?',
                           ('session_input.%s.v1.%s' % (kind, admission_id),)).fetchone()
    return json.loads(row[0]) if row else None


def _worker_row(db, *, execution_id, generation, status):
    db._execute_write(lambda conn: conn.execute(
        "INSERT INTO worker_executions(execution_id,session_id,kind,owner_epoch,generation,status,"
        "adoption_digest) VALUES(?,?,'compute',1,?,?, 'x')",
        (execution_id, 's', generation, status)))


def test_the_epoch_bump_decides_with_provenance_instead_of_minting_unknown(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    first = begin_runtime_epoch(db, instance_id='owner-a')
    head = _admit(db, first, finite=True, text='head')
    claim_session_input(db, epoch=first, session_id='s')          # started under owner-a

    second = begin_runtime_epoch(db, instance_id='owner-b')
    assert recover_session_inputs(db, epoch=second) == 1
    row = get_session_admission(db, admission_id=head['admission_id'])
    assert (row['status'], row['outcome']) == ('terminal', 'interrupted')
    receipt = _receipt(db, 'interruption', head['admission_id'])
    assert receipt == {'edge': 'epoch-bump', 'epoch': second, 'owner_epoch': first,
                       'generation': row['generation'], 'seq': row['seq'],
                       'outcome': 'interrupted'}
    # idempotent: a second recovery has nothing to decide
    assert recover_session_inputs(db, epoch=second) == 0


def test_a_started_row_of_the_current_epoch_is_not_decided_by_a_bump(tmp_path, monkeypatch):
    """The boundary: the bump touches LOST claims only, never the owner's own in-flight turn."""
    db = _db(tmp_path, monkeypatch)
    epoch = begin_runtime_epoch(db, instance_id='owner-a')
    head = _admit(db, epoch, finite=True, text='head')
    claim_session_input(db, epoch=epoch, session_id='s')

    assert recover_session_inputs(db, epoch=epoch) == 0
    row = get_session_admission(db, admission_id=head['admission_id'])
    assert (row['status'], row['outcome']) == ('started', None)
    assert _receipt(db, 'interruption', head['admission_id']) is None


def test_an_interrupted_admission_does_not_stay_latched_by_its_revoked_worker_row(tmp_path, monkeypatch):
    """The producer census's gap: the bump terminalised admissions but left worker rows `unknown`."""
    db = _db(tmp_path, monkeypatch)
    first = begin_runtime_epoch(db, instance_id='owner-a')
    head = _admit(db, first, finite=True, text='head')
    follower = _admit(db, first, finite=True, text='follower')
    claimed = claim_session_input(db, epoch=first, session_id='s')
    _worker_row(db, execution_id='exec-1', generation=claimed['generation'], status='unknown')

    second = begin_runtime_epoch(db, instance_id='owner-b')
    recover_session_inputs(db, epoch=second)
    # the lane advances: the decided turn is over, its worker row is history
    nxt = claim_session_input(db, epoch=second, session_id='s')
    assert nxt['admission_id'] == follower['admission_id']


def test_an_undecided_admission_with_an_unknown_worker_is_still_refused(tmp_path, monkeypatch):
    """The other half of the join: an unknown worker over a LIVE claim keeps the lane held."""
    db = _db(tmp_path, monkeypatch)
    epoch = begin_runtime_epoch(db, instance_id='owner-a')
    head = _admit(db, epoch, finite=False, text='head')
    claimed = claim_session_input(db, epoch=epoch, session_id='s')
    _worker_row(db, execution_id='exec-2', generation=claimed['generation'], status='unknown')
    db._execute_write(lambda conn: conn.execute(
        "UPDATE session_admissions SET status='unknown' WHERE admission_id=?", (head['admission_id'],)))

    with pytest.raises(RuntimeStoreError) as caught:
        claim_session_input(db, epoch=epoch, session_id='s')
    assert caught.value.reason == 'unknown_execution'
    assert caught.value.field == 'admission'          # the admission is the blunter reason
    assert caught.value.detail == 'seq=%d cleared_by=none' % head['seq']


def test_a_refusal_leaves_a_receipt_a_dashboard_can_read(tmp_path, monkeypatch):
    """A pause whose only record is an exception in a worker's stderr is a silent failure."""
    db = _db(tmp_path, monkeypatch)
    epoch = begin_runtime_epoch(db, instance_id='owner-a')
    head = _admit(db, epoch, finite=False, text='mine')
    claim_session_input(db, epoch=epoch, session_id='s')
    db._execute_write(lambda conn: conn.execute(
        "UPDATE session_admissions SET status='unknown' WHERE admission_id=?", (head['admission_id'],)))

    with pytest.raises(RuntimeStoreError):
        claim_session_input(db, epoch=epoch, session_id='s')
    receipt = _receipt(db, 'refusal', head['admission_id'])
    assert receipt['reason'] == 'unknown_execution' and receipt['field'] == 'admission'
    assert receipt['seq'] == head['seq'] and receipt['epoch'] == epoch
    assert receipt['session_id'] == 's'
