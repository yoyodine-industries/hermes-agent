"""Rulings 1 and 2 for card t-76c210202a: quarantine with a stated age, and a bounded cadence.

Written against a throwaway ``SessionDB``; every assertion reads a row back from the store.
"""
import time

import hermes_state_runtime as rt
from hermes_state import SessionDB
from hermes_state_admission_frontier import (
    CADENCE_INTERVAL_S,
    QUARANTINE_AGE_S,
    cadence_due,
    drain_pass,
    mark_quarantine_eligible,
    quarantine_unknown,
    tick,
)


def _store(tmp_path, *sessions):
    db = SessionDB(db_path=tmp_path / 'state.db')
    for session_id in sessions or ('s',):
        db.create_session(session_id, source='test')
    return db, rt.begin_runtime_epoch(db, instance_id='boot')


def _admit(db, epoch, request_id, session_id='s'):
    return rt.admit_session_input(db, epoch=epoch, principal_id='human', session_id=session_id,
                                  request_id=request_id, payload={'text': request_id})


def _row(db, admission_id):
    """Read the row straight from the store: the runtime's own reader refuses while an `unknown`
    row exists, and the point of these tests is what the STORE holds, not what the reader allows."""
    with db._read_ctx() as conn:
        row = conn.execute('SELECT * FROM session_admissions WHERE admission_id=?',
                           (admission_id,)).fetchone()
    assert row is not None, admission_id
    return dict(row)


def _make_unknown(db, admission_id):
    """The runtime shape this guards: a claimed row whose execution outcome is unknown."""
    db._execute_write(lambda conn: conn.execute(
        "UPDATE session_admissions SET status='unknown' WHERE admission_id=?", (admission_id,)))


def _quarantine_rows(db):
    with db._read_ctx() as conn:
        try:
            return [dict(r) for r in conn.execute(
                'SELECT * FROM session_admission_quarantine ORDER BY seq')]
        except Exception:
            return []


# --------------------------------------------------------------------------- ruling 1


def test_an_unknown_row_is_quarantined_after_the_stated_age_and_the_lane_resumes(tmp_path):
    db, epoch = _store(tmp_path)
    stuck = _admit(db, epoch, 'r-stuck')
    behind = _admit(db, epoch, 'r-behind')
    _make_unknown(db, stuck['admission_id'])
    before = _row(db, stuck['admission_id'])   # the verbatim source the copy must reproduce
    lines = []
    store = str(tmp_path / 'state.db')

    # BEFORE the stated age: nothing moves, and the lane is still frozen.
    assert quarantine_unknown(db, 's', store=store, now=time.time(), out=lines.append) is None
    assert _row(db, stuck['admission_id'])['status'] == 'unknown'

    # AFTER it: the row MOVES - preserved byte for byte - and the lane can advance.
    later = time.time() + QUARANTINE_AGE_S + 1
    moved = quarantine_unknown(db, 's', store=store, now=later, out=lines.append)
    assert moved and moved['seq'] == stuck['seq'], moved

    original = _row(db, stuck['admission_id'])
    assert original['status'] == 'terminal', original
    assert original['outcome'] == f"unknown_quarantined:{stuck['seq']}", original

    [copy] = _quarantine_rows(db)
    for column in ('seq', 'admission_id', 'request_id', 'principal_id', 'target_session_id',
                   'lineage_json', 'payload_json', 'payload_digest', 'intent', 'outcome',
                   'generation', 'owner_epoch'):
        assert copy[column] == before[column], (column, copy[column], before[column])
    assert copy['status_at_quarantine'] == 'unknown'
    assert copy['store'] == store, copy
    assert copy['age_seconds'] >= QUARANTINE_AGE_S

    # the age CARRIES ITS UNIT AND ITS STORE, in the line a reader has
    line = lines[-1]
    assert f"unknown_quarantined:{stuck['seq']}" in line, line
    assert 'unit=seconds' in line and f'store={store}' in line, line

    # NOT an overtake: the quarantined turn was never delivered, and the row behind it now runs
    seen = []
    report = drain_pass(db, epoch=epoch, session_id='s', run=lambda row: seen.append(row['seq']),
                        filer=False, out=lines.append)
    assert seen == [behind['seq']], seen
    assert report.settled and report.settled[0]['seq'] == behind['seq'], report.settled


def test_quarantine_is_never_a_delivery_and_an_owner_resolution_only_makes_it_eligible(tmp_path):
    db, epoch = _store(tmp_path)
    stuck = _admit(db, epoch, 'r-stuck')
    _make_unknown(db, stuck['admission_id'])
    store = str(tmp_path / 'state.db')
    first = time.time()
    quarantine_unknown(db, 's', store=store, now=first, out=lambda line: None)   # stamps the age
    quarantine_unknown(db, 's', store=store, now=first + QUARANTINE_AGE_S + 1,
                       out=lambda line: None)

    [copy] = _quarantine_rows(db)
    assert copy['resolution'] is None, copy

    assert mark_quarantine_eligible(db, stuck['admission_id'], by='owner') is True
    [copy] = _quarantine_rows(db)
    assert copy['resolution'] == 'owner', copy
    # ELIGIBLE, nothing more: no new admission row, no re-emission, the lane stays quiet
    assert [r['admission_id'] for r in rt.list_session_admissions(
        db, session_id='s', pending_only=False)] == [stuck['admission_id']]
    assert _row(db, stuck['admission_id'])['status'] == 'terminal'


def test_a_started_row_is_not_quarantined_by_this_path(tmp_path):
    """Only an ``unknown`` execution is quarantined: a ``started`` turn is somebody's live work."""
    db, epoch = _store(tmp_path)
    row = _admit(db, epoch, 'r1')
    claimed = rt.claim_session_input(db, epoch=epoch, session_id='s')
    assert claimed and claimed['status'] == 'started', claimed
    assert quarantine_unknown(db, 's', store='x', now=time.time() + QUARANTINE_AGE_S + 1,
                              out=lambda line: None) is None
    assert _row(db, row['admission_id'])['status'] == 'started'


# --------------------------------------------------------------------------- ruling 2


def test_the_cadence_is_a_floor_not_a_spin(tmp_path):
    db, _epoch = _store(tmp_path)
    now = time.time()
    assert cadence_due(db, 's', now=now) is True
    assert cadence_due(db, 's', now=now + 1) is False       # same window: no second pass
    assert cadence_due(db, 's', now=now + CADENCE_INTERVAL_S + 0.1) is True


def test_a_tick_drains_with_no_inbound_event_and_prints_the_frontier(tmp_path):
    db, epoch = _store(tmp_path)
    for n in range(2):
        _admit(db, epoch, f'r{n}')
    lines = []
    seen = []
    report = tick(db, epoch=epoch, session_id='s', run=lambda row: seen.append(row['seq']),
                  filer=False, out=lines.append)
    assert seen == [1, 2], seen
    assert len(lines) == 1, lines
    line = lines[0]
    assert 'queued=' in line and 'seq=' in line and 'blocked=' not in line, line

    # idempotent: the very next tick inside the window does nothing and says nothing
    assert tick(db, epoch=epoch, session_id='s', run=lambda row: seen.append(row['seq']),
                filer=False, out=lines.append) is None
    assert len(lines) == 1, lines


def test_a_pass_that_finds_nothing_is_silent(tmp_path):
    db, epoch = _store(tmp_path)
    lines = []
    assert tick(db, epoch=epoch, session_id='s', run=lambda row: None, filer=False,
                out=lines.append) is None
    assert lines == [], lines


def test_the_blocked_status_is_printed_on_the_pass_that_sees_it(tmp_path):
    db, epoch = _store(tmp_path)
    stuck = _admit(db, epoch, 'r-stuck')
    _make_unknown(db, stuck['admission_id'])
    lines = []
    tick(db, epoch=epoch, session_id='s', run=lambda row: None, filer=False, out=lines.append)
    assert lines and f"blocked=unknown@{stuck['seq']}" in lines[-1], lines


def test_the_cadence_loop_wakes_a_lane_with_work_once_and_never_twice_in_a_floor(tmp_path):
    """The watcher's contract: a lane with queued work is woken on the floor, no more often."""
    import asyncio

    import pytest

    from hermes_state_admission_frontier import admission_cadence_loop

    db, epoch = _store(tmp_path)
    _admit(db, epoch, 'r1')
    wakes, ticks = [], []

    async def sleep(_seconds):
        ticks.append(1)
        if len(ticks) >= 3:
            raise SystemExit  # end the loop deterministically; no wall-clock sleep in a test
        await asyncio.sleep(0)

    with pytest.raises(SystemExit):
        asyncio.run(admission_cadence_loop(db, lambda: ['s'], wakes.append, interval_s=30.0,
                                           sleep=sleep))

    # three ticks inside ONE 30s floor: the lane is woken once, and the floor is what stops a spin
    assert ticks == [1, 1, 1], ticks
    assert wakes == ['s'], wakes


def test_the_cadence_loop_says_nothing_for_a_lane_with_nothing_waiting(tmp_path):
    import asyncio

    import pytest

    from hermes_state_admission_frontier import admission_cadence_loop

    db, _epoch = _store(tmp_path)
    wakes, ticks = [], []

    async def sleep(_seconds):
        ticks.append(1)
        if len(ticks) >= 2:
            raise SystemExit
        await asyncio.sleep(0)

    with pytest.raises(SystemExit):
        asyncio.run(admission_cadence_loop(db, lambda: ['s'], wakes.append, interval_s=30.0,
                                           sleep=sleep))
    assert wakes == [], wakes
