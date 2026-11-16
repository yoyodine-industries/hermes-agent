"""An orphaned ``started`` admission stops the FIFO, and the sanctioned control could not clear it.

``claim_session_input`` refuses the head while any row of the session is ``started``, and
``resolve_unknown_session_input`` accepts only ``unknown``: a ``started`` row whose owner is gone had
no lawful clearing verb, and 55 messages sat behind one. ``retire_orphaned_session_input`` is that
verb, and these tests are its fence: it must clear a real orphan AND refuse every case where
something alive holds the row.
"""
import time

import pytest

from hermes_state import SessionDB
from hermes_state_runtime import (
    RuntimeStoreError, admit_session_input, begin_runtime_epoch, claim_session_input,
    orphaned_lease_evidence, retire_orphaned_session_input,
)

SESSION = 's'


def _store(tmp_path):
    db = SessionDB(db_path=tmp_path / 'state.db')
    db.create_session(SESSION, source='test')
    epoch = begin_runtime_epoch(db, instance_id='boot')
    return db, epoch


def _admit(db, epoch, request_id):
    return admit_session_input(db, epoch=epoch, principal_id='human', session_id=SESSION,
                               request_id=request_id, payload={'text': request_id})


def _claim(db, epoch, request_id='r-stuck'):
    """Admit one message and CLAIM it: a ``started`` row is created by a real claim, never by an
    UPDATE - the store CHECKs ``owner_epoch``/``generation`` with that status."""
    _admit(db, epoch, request_id)
    row = claim_session_input(db, epoch=epoch, session_id=SESSION)
    assert row and row['status'] == 'started', row
    return row


def _lease(db, *, expires_in=600.0, holder='pid=4242:turn=x:y:platform=cli'):
    now = time.time()
    db._execute_write(lambda conn: conn.execute(
        'INSERT INTO session_turn_leases (conversation_id, holder, acquired_at, expires_at) '
        'VALUES (?,?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET holder=excluded.holder,'
        'acquired_at=excluded.acquired_at,expires_at=excluded.expires_at',
        (SESSION, holder, now, now + expires_in)))


def _row(db, admission_id):
    return db._read_one('SELECT * FROM session_admissions WHERE admission_id=?', (admission_id,))


def test_an_orphaned_started_lease_is_retired_and_the_queue_advances(tmp_path):
    db, epoch = _store(tmp_path)
    stuck = _claim(db, epoch)
    behind = _admit(db, epoch, 'r-behind')
    # no lease, no worker: nothing alive owns the row, which is exactly the measured shape

    retired = retire_orphaned_session_input(db, epoch=epoch, session_id=SESSION,
                                           admission_id=stuck['admission_id'],
                                           generation=stuck['generation'])
    assert retired['verdict'] == 'retired', retired
    assert retired['status'] == 'terminal', retired
    assert retired['outcome'] == f"orphan_retired:{stuck['seq']}", retired
    assert retired['owner_epoch'] is None and retired['generation'] is None, retired
    assert retired['evidence'], retired   # the proof is reported, never assumed

    # the FIFO moves: the row behind it is claimable now
    next_row = claim_session_input(db, epoch=epoch, session_id=SESSION)
    assert next_row['admission_id'] == behind['admission_id'], next_row


def test_an_unexpired_turn_lease_makes_the_verb_REFUSE_and_name_the_holder(tmp_path):
    db, epoch = _store(tmp_path)
    stuck = _claim(db, epoch)
    _lease(db, expires_in=600.0, holder='pid=35864:turn=local:x1:platform=cli')

    with pytest.raises(RuntimeStoreError) as exc:
        retire_orphaned_session_input(db, epoch=epoch, session_id=SESSION,
                                      admission_id=stuck['admission_id'],
                                      generation=stuck['generation'])
    assert 'lease_live' in str(exc.value), exc.value
    assert 'pid=35864' in str(exc.value), exc.value   # the refusal names the holder
    assert _row(db, stuck['admission_id'])['status'] == 'started'


def test_an_EXPIRED_lease_is_a_leak_not_an_owner(tmp_path):
    db, epoch = _store(tmp_path)
    stuck = _claim(db, epoch)
    _lease(db, expires_in=-60.0)   # the store is full of these; presence alone proves nothing

    retired = retire_orphaned_session_input(db, epoch=epoch, session_id=SESSION,
                                           admission_id=stuck['admission_id'],
                                           generation=stuck['generation'])
    assert retired['verdict'] == 'retired', retired


def test_a_registered_worker_makes_the_verb_refuse(tmp_path):
    db, epoch = _store(tmp_path)
    stuck = _claim(db, epoch)
    db._execute_write(lambda conn: conn.execute(
        "INSERT INTO worker_executions (execution_id, session_id, kind, owner_epoch, generation,"
        " status, adoption_digest) VALUES ('w1', ?, 'child', ?, 1, 'running', 'd')",
        (SESSION, epoch)))

    with pytest.raises(RuntimeStoreError) as exc:
        retire_orphaned_session_input(db, epoch=epoch, session_id=SESSION,
                                      admission_id=stuck['admission_id'],
                                      generation=stuck['generation'])
    assert 'lease_live' in str(exc.value), exc.value
    assert 'w1' in str(exc.value), exc.value


def test_a_stale_generation_is_refused(tmp_path):
    db, epoch = _store(tmp_path)
    stuck = _claim(db, epoch)

    with pytest.raises(RuntimeStoreError) as exc:
        retire_orphaned_session_input(db, epoch=epoch, session_id=SESSION,
                                      admission_id=stuck['admission_id'],
                                      generation=stuck['generation'] + 1)
    assert 'stale_generation' in str(exc.value), exc.value
    assert _row(db, stuck['admission_id'])['status'] == 'started'


def test_another_sessions_name_retires_nothing(tmp_path):
    db, epoch = _store(tmp_path)
    stuck = _claim(db, epoch)

    with pytest.raises(RuntimeStoreError) as exc:
        retire_orphaned_session_input(db, epoch=epoch, session_id='some-other-session',
                                      admission_id=stuck['admission_id'],
                                      generation=stuck['generation'])
    assert 'session_mismatch' in str(exc.value), exc.value


def test_an_unknown_row_keeps_its_own_control(tmp_path):
    """``unknown`` is not this verb's population: it has a sanctioned control already."""
    db, epoch = _store(tmp_path)
    stuck = _claim(db, epoch)
    db._execute_write(lambda conn: conn.execute(
        "UPDATE session_admissions SET status='unknown' WHERE admission_id=?",
        (stuck['admission_id'],)))

    with pytest.raises(RuntimeStoreError) as exc:
        retire_orphaned_session_input(db, epoch=epoch, session_id=SESSION,
                                      admission_id=stuck['admission_id'],
                                      generation=stuck['generation'])
    assert 'not_started' in str(exc.value), exc.value


def test_retiring_twice_is_idempotent(tmp_path):
    db, epoch = _store(tmp_path)
    stuck = _claim(db, epoch)
    first = retire_orphaned_session_input(db, epoch=epoch, session_id=SESSION,
                                          admission_id=stuck['admission_id'],
                                          generation=stuck['generation'])
    again = retire_orphaned_session_input(db, epoch=epoch, session_id=SESSION,
                                          admission_id=stuck['admission_id'],
                                          generation=stuck['generation'])
    assert first['outcome'] == again['outcome'] == f"orphan_retired:{stuck['seq']}", (first, again)
    assert again.get('replay') is True, again


def test_evidence_names_both_objects_it_looked_at(tmp_path):
    db, epoch = _store(tmp_path)
    stuck = _claim(db, epoch)
    evidence = _row(db, stuck['admission_id'])

    with db._read_ctx() as conn:
        verdict = orphaned_lease_evidence(conn, evidence, now=time.time())
    assert verdict['orphaned'] is True, verdict
    assert len(verdict['evidence']) == 2, verdict
    assert any('session_turn_leases' in line for line in verdict['evidence']), verdict
    assert any('worker_executions' in line for line in verdict['evidence']), verdict
