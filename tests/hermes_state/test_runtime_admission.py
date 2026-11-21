"""Transactional contracts against exclusively owned temporary profile databases."""
import sqlite3

import pytest

from hermes_state import SessionDB


def store(tmp_path):
    db = SessionDB(db_path=tmp_path / 'state.db')
    db.create_session('s', source='test')
    # This is the baseline behavior assertion, before importing the new API.
    assert db._conn.execute("SELECT name FROM sqlite_master WHERE name='runtime_epoch'").fetchone(), 'canonical runtime schema missing'
    return db


def test_admission_retry_claim_and_restart_are_fenced(tmp_path):
    db = store(tmp_path)
    import hermes_state_runtime as rt
    from gateway.session_admission import admission_fingerprint
    assert admission_fingerprint(canonical_target='s', payload={'a': 1, 'b': 2}) == admission_fingerprint(canonical_target='s', payload={'b': 2, 'a': 1})
    with pytest.raises(ValueError):
        admission_fingerprint(canonical_target='s', payload={'a': float('nan')})
    try:
        epoch = rt.begin_runtime_epoch(db, instance_id='boot')
        args = dict(epoch=epoch, principal_id='human', session_id='s', request_id='one', payload={'text': 'hello'})
        first = rt.admit_session_input(db, **args)
        assert rt.admit_session_input(db, **args) == first
        for change in ({'payload': {'text': 'changed'}}, {'intent': 'steer'}):
            with pytest.raises(rt.RuntimeStoreError) as exc:
                rt.admit_session_input(db, **(args | change))
            assert exc.value.reason == 'admission_conflict'
        second = rt.admit_session_input(db, **(args | {'request_id': 'two'}))
        third = rt.admit_session_input(db, **(args | {'principal_id': 'other'}))
        assert first['seq'] < second['seq'] < third['seq']
        claimed = rt.claim_session_input(db, epoch=epoch, session_id='s')
        assert claimed['admission_id'] == first['admission_id'] and claimed['status'] == 'started'
        assert rt.claim_session_input(db, epoch=epoch, session_id='s') is None
        with pytest.raises(rt.RuntimeStoreError) as exc:
            rt.settle_session_input(db, epoch=epoch, admission_id=claimed['admission_id'], generation=claimed['generation'] + 1, outcome='completed')
        assert exc.value.reason == 'stale_generation'
        rt.settle_session_input(db, epoch=epoch, admission_id=claimed['admission_id'], generation=claimed['generation'], outcome='completed')
        running = rt.claim_session_input(db, epoch=epoch, session_id='s')
        assert running['admission_id'] == second['admission_id'] and running['generation'] > claimed['generation']
        db.close()
        db = SessionDB(db_path=tmp_path / 'state.db')
        new_epoch = rt.begin_runtime_epoch(db, instance_id='replacement')
        assert new_epoch > epoch
        # The bump released the stale claim in its own transaction; the sweep is a no-op.
        assert rt.get_session_admission(db, admission_id=second['admission_id'])['status'] == 'terminal'
        assert rt.recover_session_inputs(db, epoch=new_epoch) == 0
        abandoned = rt.get_session_admission(db, admission_id=second['admission_id'])
        assert abandoned['status'] == 'terminal' and abandoned['outcome'] == 'interrupted', \
            'a stale-epoch turn is terminal-abandoned by the sweep, never left as a permanent latch'
        with pytest.raises(rt.RuntimeStoreError) as exc:
            rt.settle_session_input(db, epoch=epoch, admission_id=running['admission_id'], generation=running['generation'], outcome='completed')
        assert exc.value.reason == 'stale_epoch'
        assert rt.cancel_session_input(db, epoch=new_epoch, admission_id=third['admission_id'])['outcome'] == 'cancelled'
        assert [r['admission_id'] for r in rt.list_session_admissions(db, session_id='s')] == []
        assert db._conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            db._execute_write(lambda conn: conn.execute("DELETE FROM sessions WHERE id='s'"))
    finally:
        db.close()


def test_sqlite_failure_never_acknowledges_or_partially_claims(tmp_path):
    db = store(tmp_path)
    import hermes_state_runtime as rt
    try:
        epoch = rt.begin_runtime_epoch(db, instance_id='boot')
        args = dict(epoch=epoch, principal_id='human', session_id='s', request_id='one', payload={'text': 'hello'})
        db._conn.execute("CREATE TRIGGER refuse_admission BEFORE INSERT ON session_admissions BEGIN SELECT RAISE(ABORT, 'full fixture'); END")
        with pytest.raises(sqlite3.IntegrityError, match='full fixture'):
            rt.admit_session_input(db, **args)
        assert rt.list_session_admissions(db, session_id='s') == []
        db._conn.execute('DROP TRIGGER refuse_admission')
        row = rt.admit_session_input(db, **args)
        db._conn.execute("CREATE TRIGGER refuse_claim BEFORE UPDATE ON session_admissions BEGIN SELECT RAISE(ABORT, 'claim fixture'); END")
        with pytest.raises(sqlite3.IntegrityError, match='claim fixture'):
            rt.claim_session_input(db, epoch=epoch, session_id='s')
        assert db.get_session('s')['runtime_generation'] == 0
        assert rt.get_session_admission(db, admission_id=row['admission_id'])['status'] == 'queued'
        db._conn.execute('DROP TRIGGER refuse_claim')
        with sqlite3.connect(tmp_path / 'state.db', timeout=0) as peer:
            assert peer.execute('SELECT admission_id FROM session_admissions').fetchone()[0] == row['admission_id']
            peer.execute('BEGIN IMMEDIATE')
            db._WRITE_PATIENCE_S = 0
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                rt.admit_session_input(db, **(args | {'request_id': 'busy'}))
            peer.rollback()
        assert len(rt.list_session_admissions(db, session_id='s')) == 1
    finally:
        db.close()


def test_a_stale_epoch_unknown_row_never_latches_the_target(tmp_path):
    """The live latch, pinned: unknown(owner_epoch=K-1) + queued, runtime_epoch=K."""
    db = store(tmp_path)
    import hermes_state_runtime as rt
    try:
        epoch = rt.begin_runtime_epoch(db, instance_id='boot')
        head = rt.admit_session_input(db, epoch=epoch, principal_id='human', session_id='s',
                                      request_id='dead', payload={'text': 'dead turn'})
        queued = rt.admit_session_input(db, epoch=epoch, principal_id='human', session_id='s',
                                        request_id='behind', payload={'text': 'stranded'})
        claimed = rt.claim_session_input(db, epoch=epoch, session_id='s')
        assert claimed['admission_id'] == head['admission_id']
        # The bump: the runtime reloads and the in-flight claim keeps the old epoch.
        new_epoch = rt.begin_runtime_epoch(db, instance_id='reload')
        assert new_epoch > epoch
        # A CURRENT-epoch unknown still refuses, but names the seq and what clears it.
        db._execute_write(lambda conn: conn.execute(
            "UPDATE session_admissions SET status='unknown',outcome=NULL WHERE admission_id=?",
            (head['admission_id'],)))
        with pytest.raises(rt.RuntimeStoreError) as exc:
            rt.claim_session_input(db, epoch=new_epoch, session_id='s')
        assert exc.value.reason == 'unknown_execution'
        assert f"seq={head['seq']}" in (exc.value.detail or '')
        assert 'cleared_by=settle_session_input|epoch_sweep' in (exc.value.detail or '')
        # Stale epoch: the sweep releases it, and the row behind it is claimable in the same pass.
        assert rt.recover_session_inputs(db, epoch=new_epoch) == 1
        assert rt.get_session_admission(db, admission_id=head['admission_id'])['status'] == 'terminal'
        assert rt.claim_session_input(db, epoch=new_epoch, session_id='s')['admission_id'] == queued['admission_id']
    finally:
        db.close()


def test_the_epoch_bump_itself_releases_the_latch(tmp_path):
    """Clause 7: the bump is the event that reconciles, not a sweep that may never run.

    The reload path bumps the epoch and then relies on a separate sweep to release the rows it
    just made stale. Any bump that returns without that sweep (the dashboard's offline epoch is
    one) leaves an in-flight claim latched, and the message behind it is unclaimable for ever.
    """
    db = store(tmp_path)
    import hermes_state_runtime as rt
    try:
        epoch = rt.begin_runtime_epoch(db, instance_id='boot')
        head = rt.admit_session_input(db, epoch=epoch, principal_id='human', session_id='s',
                                      request_id='dead', payload={'text': 'dead turn'})
        queued = rt.admit_session_input(db, epoch=epoch, principal_id='human', session_id='s',
                                        request_id='behind', payload={'text': 'stranded'})
        assert rt.claim_session_input(db, epoch=epoch, session_id='s')['admission_id'] == head['admission_id']
        # The bump and NOTHING ELSE: no recover_session_inputs call in this test.
        new_epoch = rt.begin_runtime_epoch(db, instance_id='reload')
        assert new_epoch > epoch
        abandoned = rt.get_session_admission(db, admission_id=head['admission_id'])
        assert (abandoned['status'], abandoned['outcome']) == ('terminal', 'interrupted'), \
            'the bump must abandon its own in-flight rows in the same transaction'
        assert rt.claim_session_input(db, epoch=new_epoch, session_id='s')['admission_id'] == queued['admission_id'], \
            'a queued row behind a bumped claim is claimable without waiting for a sweep'
        # The later sweep is a no-op, not the release edge.
        assert rt.recover_session_inputs(db, epoch=new_epoch) == 0
    finally:
        db.close()
