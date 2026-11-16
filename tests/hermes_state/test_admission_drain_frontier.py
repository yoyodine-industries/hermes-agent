"""A drain pass never advances past an unsettled admission: scratch-store contracts.

The store is a throwaway ``SessionDB`` under ``tmp_path``; every assertion about a row reads its
status back from that store (a claim is an admission, never a delivery). The production shape this
guards - a ``queued`` row below a terminal one, left behind by a writer that moved admissions
without re-sequencing them - is built here the way an older release built it.
"""
import time

import hermes_state_runtime as rt
from hermes_state import SessionDB
from hermes_state_admission_frontier import (drain_pass, frontier_seq, stranded, stranded_rows)


def _store(tmp_path, *sessions):
    db = SessionDB(db_path=tmp_path / 'state.db')
    for session_id in sessions or ('s',):
        db.create_session(session_id, source='test')
    return db, rt.begin_runtime_epoch(db, instance_id='boot')


def _admit(db, epoch, request_id, session_id='s', principal='human'):
    return rt.admit_session_input(db, epoch=epoch, principal_id=principal, session_id=session_id,
                                  request_id=request_id, payload={'text': request_id})


def _statuses(db, session_id='s'):
    return {row['seq']: row['status']
            for row in rt.list_session_admissions(db, session_id=session_id, pending_only=False)}


def _settle_all(db, epoch, session_id='s'):
    while True:
        row = rt.claim_session_input(db, epoch=epoch, session_id=session_id)
        if not isinstance(row, dict):
            return
        rt.settle_session_input(db, epoch=epoch, admission_id=row['admission_id'],
                                generation=row['generation'], outcome='completed')


def _requeue_legacy(db, admission_id):
    """The pre-fix rehome shape: a settled row put back in the FIFO at its ORIGINAL seq."""
    db._execute_write(lambda conn: conn.execute(
        "UPDATE session_admissions SET status='queued',generation=NULL,owner_epoch=NULL "
        "WHERE admission_id=?", (admission_id,)))


def test_a_mid_list_failure_pins_the_frontier_and_is_retried_first(tmp_path):
    db, epoch = _store(tmp_path)
    rows = [_admit(db, epoch, f'r{n}') for n in range(3)]
    seen, lines = [], []

    def run(row):
        seen.append(row['seq'])
        if row['admission_id'] == rows[1]['admission_id']:
            raise RuntimeError('transcript write failed')

    report = drain_pass(db, epoch=epoch, session_id='s', run=run, filer=False, out=lines.append)

    # (a) no row is skipped, and nothing above the failure is claimed.
    assert seen == [rows[0]['seq'], rows[1]['seq']]
    status = _statuses(db)
    assert status[rows[0]['seq']] == 'terminal'
    assert status[rows[1]['seq']] == 'queued'  # read from the store: released, not settled
    assert status[rows[2]['seq']] == 'queued'
    # (b) the frontier stops BELOW the failure (it had reached the settled first row only).
    assert (report.frontier_before, report.frontier_after) == (rows[0]['seq'] - 1, rows[1]['seq'] - 1)
    assert report.stopped_at == rows[1]['seq']
    assert report.settled[0]['status'] == 'terminal'  # the status the report read from the store
    assert 'stranded=0' in lines[0]

    # The next pass retries the pinned row FIRST, then the row above it: nothing stays behind.
    second = drain_pass(db, epoch=epoch, session_id='s', run=lambda row: None, filer=False,
                        out=lines.append)
    assert [item['seq'] for item in second.settled] == [rows[1]['seq'], rows[2]['seq']]
    assert [item['status'] for item in second.settled] == ['terminal', 'terminal']
    assert _statuses(db) == {row['seq']: 'terminal' for row in rows}
    assert second.frontier_after == rows[2]['seq']
    print(f"pinned seq={report.stopped_at} retried+settled seq={second.settled[0]['seq']} "
          f"frontier={report.frontier_before}->{second.frontier_after}")


def test_stranded_rows_are_reported_with_the_oldest_age_and_file_the_lane_card(tmp_path):
    db, epoch = _store(tmp_path)
    head, above, in_flight = (_admit(db, epoch, 'stranded'), _admit(db, epoch, 'terminal'),
                              _admit(db, epoch, 'in-flight'))
    _settle_all(db, epoch)                      # seqs 1..3 all terminal: the lane has a frontier
    _requeue_legacy(db, head['admission_id'])   # the shape an older release left behind: seq 1 queued
    db._execute_write(lambda conn: conn.execute(
        "UPDATE session_admissions SET status='started',owner_epoch=?,generation=1 WHERE admission_id=?",
        (epoch, in_flight['admission_id'])))    # another writer's turn in flight blocks the FIFO
    assert rt.claim_session_input(db, epoch=epoch, session_id='s') is None

    lines, filed = [], []

    def filer(report):
        filed.append(report)
        return f'CARD-{len(filed)}'

    t0 = time.time()
    first = drain_pass(db, epoch=epoch, session_id='s', run=lambda row: None,
                       filer=filer, now=t0, out=lines.append)
    assert first.settled == []  # blocked: the stranded row and the rows around it stay queued
    assert first.stranded.count == 1 and first.stranded.oldest_seq == head['seq']
    assert first.stranded.above_seq == above['seq']
    assert 'stranded=1' in lines[0] and f'oldest={head["seq"]}' in lines[0]
    assert len(filed) == 1 and filed[0].stranded.oldest_seq == head['seq']  # card, not a log line
    assert first.card == 'CARD-1'

    second = drain_pass(db, epoch=epoch, session_id='s', run=lambda row: None,
                        filer=filer, now=t0 + 900, out=lines.append)
    assert 'stranded=1' in lines[1] and 'age=900s' in lines[1]
    assert second.stranded.age_seconds >= 900
    assert [row['seq'] for row in stranded_rows(db._conn, 's')] == [head['seq']]
    print(f"stranded={second.stranded.count} oldest={second.stranded.oldest_seq} "
          f"above={second.stranded.above_seq} age={lines[1].split('age=')[1].split()[0]}")


def test_rehomed_rows_take_a_seq_above_the_target_frontier(tmp_path):
    db, epoch = _store(tmp_path, 'old', 'new')
    moved_src = [_admit(db, epoch, 'a', session_id='old'), _admit(db, epoch, 'b', session_id='old')]
    _admit(db, epoch, 'x', session_id='new')
    _admit(db, epoch, 'y', session_id='new')
    _settle_all(db, epoch, session_id='new')
    frontier_before = frontier_seq(db._conn, 'new')

    moved = rt.rehome_queued_inputs(db, epoch=epoch, old_session_id='old', new_session_id='new',
                                   principal_prefix='human')

    assert moved == [row['admission_id'] for row in moved_src]      # FIFO order survives
    seqs = [row['seq'] for row in rt.list_session_admissions(db, session_id='new', pending_only=False)
            if row['admission_id'] in moved]
    assert seqs == [frontier_before + 1, frontier_before + 2]       # above the frontier, in order
    assert stranded(db, 'new').count == 0                           # nothing behind the frontier
    assert stranded_rows(db._conn, 'new') == []
    # The AUTOINCREMENT high-water moved with them: the next admission cannot collide.
    later = _admit(db, epoch, 'z', session_id='new')
    assert later['seq'] == frontier_before + 3
