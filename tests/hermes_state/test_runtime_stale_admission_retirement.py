"""A non-terminal admission from a dead process generation must not wedge a session forever.

Measured on the live store (2026-11-08): one ``started`` claim whose turn died sat in front of its
session's FIFO for days. Nothing could clear it — ``settle_session_input`` requires the old
generation AND epoch, ``cancel_session_input`` refuses a ``started`` row outright, and the claim
loop simply returns None — so every later message queued behind a ghost.
"""
import logging
import time

import pytest
from hermes_state import SessionDB
import hermes_state_runtime as rt
from gateway.session_stale_admissions import retire_stale_admissions_at_boot


def _submit(db, epoch, session_id, request_id, text='hello'):
    return rt.admit_session_input(db, epoch=epoch, principal_id='api', session_id=session_id,
                                  request_id=request_id, payload={'text': text})


def _claim(db, epoch, session_id):
    return rt.claim_session_input(db, epoch=epoch, session_id=session_id)


def test_boot_recovery_retires_a_stale_started_claim_so_the_fifo_resumes(tmp_path, caplog):
    """T1: a ``started`` row owned by a dead epoch is retired at boot, and the claim proceeds."""
    caplog.set_level(logging.INFO, logger='hermes_state_runtime')
    with SessionDB(tmp_path / 'state.db') as db:
        db.create_session('s1', source='api_server')
        epoch1 = rt.begin_runtime_epoch(db, instance_id='owner-1')
        lost = _submit(db, epoch1, 's1', 'r1', 'the turn that died')
        claimed = _claim(db, epoch1, 's1')
        assert claimed['admission_id'] == lost['admission_id']
        nxt = _submit(db, epoch1, 's1', 'r2', 'the message behind it')
        # A started head is a SILENT block: the claim returns None, not an error.
        assert _claim(db, epoch1, 's1') is None

        epoch2 = rt.begin_runtime_epoch(db, instance_id='owner-2')  # the owner restarted
        rt.recover_session_inputs(db, epoch=epoch2)  # fences the dead start as unknown
        assert rt.get_session_admission(db, admission_id=lost['admission_id'])['status'] == 'unknown'

        assert retire_stale_admissions_at_boot(db, epoch2) == 1

        row = rt.get_session_admission(db, admission_id=lost['admission_id'])
        assert (row['status'], row['outcome']) == ('terminal', 'interrupted')
        resumed = _claim(db, epoch2, 's1')
        assert resumed['admission_id'] == nxt['admission_id'] and resumed['status'] == 'started'

    entry = next(line for line in caplog.text.splitlines() if lost['admission_id'] in line)
    # The retirement is evidence: session, admission id, owner epoch and generation are all named.
    assert 'session=s1' in entry and 'owner_epoch=1' in entry
    assert f"generation={claimed['generation']}" in entry and 'reason=boot' in entry


def test_current_epoch_started_with_a_live_lease_is_left_alone_and_named(tmp_path):
    """T2: a live turn is never retired — recovery skips it and the verb refuses naming its lease."""
    with SessionDB(tmp_path / 'state.db') as db:
        db.create_session('s1', source='api_server')
        epoch = rt.begin_runtime_epoch(db, instance_id='owner-1')
        live = _submit(db, epoch, 's1', 'r1')
        claimed = _claim(db, epoch, 's1')
        with db._read_ctx() as conn:
            key = db._session_turn_lease_key_on_conn(conn, 's1')
        db._execute_write(lambda conn: conn.execute(
            'INSERT INTO session_turn_leases(conversation_id, holder, acquired_at, expires_at) VALUES(?,?,?,?)',
            (key, 'pid=4242:turn=live', time.time(), time.time() + 300)))

        # Boot recovery only touches rows from ANOTHER epoch: this one is current, so untouched.
        assert rt.retire_stale_admissions(db, epoch=epoch, reason='boot') == []

        with pytest.raises(rt.RuntimeStoreError) as refusal:
            rt.resolve_unknown_session_input(db, epoch=epoch, admission_id=live['admission_id'],
                                             generation=claimed['generation'])
        assert refusal.value.reason == 'admission_running'
        assert refusal.value.field == 'lease'
        assert 'pid 4242' in refusal.value.detail, refusal.value.detail
        assert rt.get_session_admission(db, admission_id=live['admission_id'])['status'] == 'started'


def test_unknown_row_resolves_through_the_same_verb_and_the_claim_proceeds(tmp_path):
    """T3: ``unknown`` (and a stale ``started``) retire through one verb: no guessing required."""
    with SessionDB(tmp_path / 'state.db') as db:
        db.create_session('s1', source='api_server')
        epoch = rt.begin_runtime_epoch(db, instance_id='owner-1')
        lost = _submit(db, epoch, 's1', 'r1')
        claimed = _claim(db, epoch, 's1')
        nxt = _submit(db, epoch, 's1', 'r2')
        # The shape a settlement that failed after the turn left a process writes:
        db._execute_write(lambda conn: conn.execute(
            "UPDATE session_admissions SET status='unknown' WHERE admission_id=?", (lost['admission_id'],)))
        with pytest.raises(rt.RuntimeStoreError) as blocked:
            _claim(db, epoch, 's1')
        assert blocked.value.reason == 'unknown_execution'

        resolved = rt.resolve_unknown_session_input(db, epoch=epoch, admission_id=lost['admission_id'],
                                                    generation=claimed['generation'])
        assert (resolved['status'], resolved['outcome']) == ('terminal', 'interrupted')
        resumed = _claim(db, epoch, 's1')
        assert resumed['admission_id'] == nxt['admission_id']


def test_the_blocking_refusal_names_the_admission_ids(tmp_path):
    """T4: the refusal carries the blocking id(s) in its machine-readable field, not prose."""
    with SessionDB(tmp_path / 'state.db') as db:
        db.create_session('s1', source='api_server')
        epoch = rt.begin_runtime_epoch(db, instance_id='owner-1')
        lost = _submit(db, epoch, 's1', 'r1')
        _claim(db, epoch, 's1')
        db._execute_write(lambda conn: conn.execute(
            "UPDATE session_admissions SET status='unknown' WHERE admission_id=?", (lost['admission_id'],)))

        with pytest.raises(rt.RuntimeStoreError) as refusal:
            _claim(db, epoch, 's1')
        assert refusal.value.reason == 'unknown_execution'
        assert refusal.value.field == 'admission_id'
        assert lost['admission_id'] in refusal.value.detail


def test_recovery_never_rewrites_terminal_history(tmp_path):
    """T5: no TERMINAL row is modified; the terminal set only grows by the rows it retires."""
    with SessionDB(tmp_path / 'state.db') as db:
        db.create_session('s1', source='api_server')
        epoch1 = rt.begin_runtime_epoch(db, instance_id='owner-1')
        done = _submit(db, epoch1, 's1', 'r1')
        claimed = _claim(db, epoch1, 's1')
        rt.settle_session_input(db, epoch=epoch1, admission_id=done['admission_id'],
                                generation=claimed['generation'], outcome='completed')
        # A second session keeps one fossil across the restart.
        db.create_session('s2', source='api_server')
        fossil = _submit(db, epoch1, 's2', 'r2')
        _claim(db, epoch1, 's2')

        def terminal_rows():
            with db._read_ctx() as conn:
                return {row[0]: (row[1], row[2]) for row in conn.execute(
                    "SELECT admission_id, status, outcome FROM session_admissions WHERE status='terminal'")}

        before = terminal_rows()
        assert before[done['admission_id']] == ('terminal', 'completed')
        epoch2 = rt.begin_runtime_epoch(db, instance_id='owner-2')
        rt.recover_session_inputs(db, epoch=epoch2)
        assert retire_stale_admissions_at_boot(db, epoch2) == 1
        after = terminal_rows()
        # Every row that was already terminal is byte-for-byte the same, and retirement only ADDS
        # the fossil it retired: the sweep never rewrites settlement history.
        assert {aid: state for aid, state in after.items() if aid in before} == before
        assert len(after) == len(before) + 1
        assert after[done['admission_id']] == before[done['admission_id']]
        assert rt.get_session_admission(db, admission_id=fossil['admission_id'])['outcome'] == 'interrupted'
