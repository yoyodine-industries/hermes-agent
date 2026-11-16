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


# ------------------------------------------------ ruling 1's CALLER (the cadence, not a docstring)
def test_ONE_cadence_pass_quarantines_an_aged_unknown_head_and_delivers_what_was_behind_it(tmp_path):
    """The acceptance the review set: the quarantine has a CALLER, and the same pass drains.

    Without this the cadence is a siren - it wakes the lane every floor into a refusal that names a
    blocker nothing will ever move, and the queued rows behind it never move either.
    """
    import asyncio
    import pytest

    from hermes_state_admission_frontier import admission_cadence_loop, quarantine_unknown

    db, epoch = _store(tmp_path)
    stuck = _admit(db, epoch, 'r-stuck')
    behind = _admit(db, epoch, 'r-behind')
    _make_unknown(db, stuck['admission_id'])
    before = _row(db, stuck['admission_id'])

    t0 = time.time()
    # the row is ALREADY older than the threshold: one earlier look stamped its age, which is what
    # "a synthetic unknown row older than 1,800 s" means - the age lives in the store, not the test
    quarantine_unknown(db, 's', store=str(tmp_path / 'state.db'),
                       now=t0 - QUARANTINE_AGE_S - 1, out=lambda line: None)

    woke, lines = [], []

    async def sleep(_seconds):
        if woke:   # exactly ONE cadence pass, then leave the loop
            raise SystemExit
        await asyncio.sleep(0)

    def wake(session_id):
        woke.append(session_id)
        # the runner's wake, in miniature: the drain that follows the pass claims the next row
        db._execute_write(lambda conn: None)
        claimed = rt.claim_session_input(db, epoch=epoch, session_id=session_id)
        woke.append(claimed['admission_id'] if claimed else None)

    with pytest.raises(SystemExit):
        asyncio.run(admission_cadence_loop(db, lambda: ['s'], wake, interval_s=CADENCE_INTERVAL_S,
                                           sleep=sleep, now=lambda: t0, out=lines.append))

    # 1. the row MOVED, verbatim, with the store and the age it was moved at
    [copy] = _quarantine_rows(db)
    for column in ('seq', 'admission_id', 'request_id', 'principal_id', 'target_session_id',
                   'lineage_json', 'payload_json', 'payload_digest', 'intent', 'outcome',
                   'generation', 'owner_epoch'):
        assert copy[column] == before[column], (column, copy[column], before[column])
    assert copy['status_at_quarantine'] == 'unknown', copy
    assert copy['store'] == str(tmp_path / 'state.db'), copy
    assert copy['age_seconds'] >= QUARANTINE_AGE_S, copy

    # 2. the live row says where it went, and the FIFO may advance past it
    live = _row(db, stuck['admission_id'])
    assert live['status'] == 'terminal', live
    assert live['outcome'] == f"unknown_quarantined:{stuck['seq']}", live

    # 3. the SAME pass delivered what was behind it
    assert woke == ['s', behind['admission_id']], woke

    # 4. and the line that reports it carries the unit and the store
    assert lines, 'the quarantine line must be printed'
    line = next(l for l in lines if l.startswith('quarantine:'))
    assert line.startswith('quarantine: unknown_quarantined:'), line
    assert 'unit=seconds' in line and 'store=' in line, line


def test_the_cadence_does_not_quarantine_a_STARTED_head(tmp_path):
    """A `started` head is a different state with its own verb (t-3040729271); the cadence must
    wake and report, never retire it."""
    import asyncio
    import pytest

    from hermes_state_admission_frontier import admission_cadence_loop

    db, epoch = _store(tmp_path)
    claimed = rt.claim_session_input(db, epoch=epoch, session_id='s') if _admit(db, epoch, 'r1') else None
    assert claimed and claimed['status'] == 'started', claimed
    t0 = time.time()
    woke = []

    async def sleep(_seconds):
        if woke:
            raise SystemExit
        await asyncio.sleep(0)

    with pytest.raises(SystemExit):
        asyncio.run(admission_cadence_loop(db, lambda: ['s'], woke.append,
                                           interval_s=CADENCE_INTERVAL_S, sleep=sleep,
                                           now=lambda: t0 + QUARANTINE_AGE_S * 5,
                                           out=lambda line: None))
    assert woke == ['s'], woke                      # woken, and the blocker reported
    assert _quarantine_rows(db) == []               # but NOT retired by this path
    assert _row(db, claimed['admission_id'])['status'] == 'started'


# --------------------------------------------------- R7: a live turn is a turn, not a wedge
def _lease(db, *, expires_in=600.0, pid=None, session='s'):
    """A live turn's lease. The holder names a pid, and `lease_liveness` PROBES it, so the default
    is this very process - a fixture that named an arbitrary number would measure a dead one."""
    import os
    pid = os.getpid() if pid is None else pid
    holder = f'pid={pid}:turn=local:platform=cli'
    now = time.time()
    db._execute_write(lambda conn: conn.execute(
        'INSERT INTO session_turn_leases (conversation_id, holder, acquired_at, expires_at) '
        'VALUES (?,?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET holder=excluded.holder,'
        'acquired_at=excluded.acquired_at,expires_at=excluded.expires_at',
        (session, holder, now, now + expires_in)))


def _claim(db, epoch, request_id='r-head', session_id='s'):
    _admit(db, epoch, request_id, session_id=session_id)
    row = rt.claim_session_input(db, epoch=epoch, session_id=session_id)
    assert row and row['status'] == 'started', row
    return row


def test_a_LIVE_started_head_is_printed_as_started_live_and_is_never_quarantined(tmp_path):
    """The counterexample this ruling came from: the head IS the turn being run, and an age rule
    would have expired it mid-flight."""
    from hermes_state_admission_frontier import _lane_census, lease_liveness, quarantine_started

    db, epoch = _store(tmp_path)
    head = _claim(db, epoch)
    _lease(db, expires_in=7200.0)          # this turn is holding its lease
    t0 = time.time()

    liveness = lease_liveness(db, 's', owner_epoch=head['owner_epoch'], now=t0)
    assert liveness['state'] == 'live', liveness
    assert f"pid={__import__('os').getpid()}" in (liveness['holder'] or ''), liveness
    assert liveness['holder_alive'] is True, liveness

    census = _lane_census(db, 's', now=t0)
    assert census['blocked'].startswith('started(live)@'), census
    assert f"epoch={epoch} current={epoch}" in census['blocked'], census

    lines = []
    assert quarantine_started(db, 's', store='x', now=t0 + QUARANTINE_AGE_S * 5,
                              out=lines.append) is None
    assert _quarantine_rows(db) == [], 'a LIVE turn must never be quarantined'
    assert _row(db, head['admission_id'])['status'] == 'started'
    assert lines == [], 'and the pass says nothing about it'


def test_a_LEAKED_lease_is_not_observed_live__the_epoch_decides(tmp_path, monkeypatch):
    """A lease row left by a dead run proves nothing, and the pid probe is what says so: its pid
    does not answer, so the epoch - which DIFFERS - is the discriminator."""
    import hermes_state_admission_frontier as frontier

    monkeypatch.setattr(frontier, '_pid_alive', lambda pid: False)   # the process behind it is gone
    lease_liveness = frontier.lease_liveness

    db, epoch = _store(tmp_path)
    head = _claim(db, epoch)
    _lease(db, expires_in=7200.0, pid=999999)   # leaked by the run that died
    db._execute_write(lambda conn: conn.execute(
        "UPDATE session_admissions SET owner_epoch=? WHERE admission_id=?",
        (epoch - 1, head['admission_id'])))

    liveness = lease_liveness(db, 's', owner_epoch=epoch - 1, now=time.time())
    assert liveness['state'] == 'orphan', liveness
    assert liveness['holder_alive'] is False, liveness
    assert f'differs: row={epoch - 1} store={epoch}' in liveness['reason'], liveness
    assert 'pid=999999, which does not answer' in liveness['reason'], liveness


def test_ONE_cadence_pass_quarantines_an_ORPHANED_started_head_and_delivers_behind_it(tmp_path):
    """R7's second rung: liveness says the owner is gone, and only then does age decide."""
    import asyncio
    import pytest

    from hermes_state_admission_frontier import admission_cadence_loop, quarantine_started

    db, epoch = _store(tmp_path)
    head = _claim(db, epoch)
    behind = _admit(db, epoch, 'r-behind')
    before = _row(db, head['admission_id'])
    t0 = time.time()

    # no lease, no worker: the probe answers ABSENT at the current epoch -> orphan
    quarantine_started(db, 's', store=str(tmp_path / 'state.db'),
                       now=t0 - QUARANTINE_AGE_S - 1, out=lambda line: None)   # stamps the age
    woke, lines = [], []

    async def sleep(_seconds):
        if woke:
            raise SystemExit
        await asyncio.sleep(0)

    def wake(session_id):
        woke.append(session_id)
        claimed = rt.claim_session_input(db, epoch=epoch, session_id=session_id)
        woke.append(claimed['admission_id'] if claimed else None)

    with pytest.raises(SystemExit):
        asyncio.run(admission_cadence_loop(db, lambda: ['s'], wake, interval_s=CADENCE_INTERVAL_S,
                                           sleep=sleep, now=lambda: t0, out=lines.append))

    [copy] = _quarantine_rows(db)
    assert copy['status_at_quarantine'] == 'started', copy
    for column in ('seq', 'admission_id', 'request_id', 'payload_json', 'payload_digest',
                   'generation', 'owner_epoch'):
        assert copy[column] == before[column], (column, copy[column], before[column])
    live = _row(db, head['admission_id'])
    assert live['status'] == 'terminal' and live['outcome'] == f"started_orphaned:{head['seq']}"
    assert woke == ['s', behind['admission_id']], woke
    line = next(l for l in lines if l.startswith('quarantine:'))
    assert line.startswith('quarantine: started_orphaned:'), line
    assert 'unit=seconds' in line and 'current_epoch=' in line and 'store=' in line, line


def test_age_ALONE_never_quarantines_a_started_head(tmp_path):
    """R7: age is the second rung. An orphan younger than the floor is reported, not retired."""
    from hermes_state_admission_frontier import _lane_census, quarantine_started

    db, epoch = _store(tmp_path)
    head = _claim(db, epoch)
    t0 = time.time()

    lines = []
    assert quarantine_started(db, 's', store='x', now=t0, out=lines.append) is None
    assert _quarantine_rows(db) == [], 'young and orphaned is still not retired'
    assert _row(db, head['admission_id'])['status'] == 'started'
    assert _lane_census(db, 's', now=t0)['blocked'].startswith('started(orphan)@')


# ------------------------------------------------- r7 corrections, measured at the store


def test_an_OBSERVED_live_holder_beats_a_stale_epoch(tmp_path, monkeypatch):
    """OBSERVED beats INFERRED. A pid that answers cannot be a claim left by a dead run, while the
    epoch is inferred from what the store advanced to - so the epoch must not kill a live turn.

    This is the kill-a-live-turn failure arriving through the epoch arm instead of the age arm:
    a stale `owner_epoch` beside a live, unexpired lease held by a `running` process.
    """
    import os

    import hermes_state_admission_frontier as frontier

    monkeypatch.setattr(frontier, '_pid_alive', lambda pid: pid == os.getpid())

    db, epoch = _store(tmp_path)
    head = _claim(db, epoch)
    _lease(db, expires_in=7200.0)                       # this turn is holding its lease
    db._execute_write(lambda conn: conn.execute(
        "UPDATE session_admissions SET owner_epoch=? WHERE admission_id=?",
        (epoch - 1, head['admission_id'])))

    liveness = frontier.lease_liveness(db, 's', owner_epoch=epoch - 1, now=time.time())
    assert liveness['state'] == 'live', liveness
    assert 'pid=' in liveness['reason'] and 'answers' in liveness['reason'], liveness

    census = frontier._lane_census(db, 's')
    assert census['blocked'].startswith('started(live)@'), census
    assert f'epoch_differs={epoch - 1}!={epoch}' in census['blocked'], census   # both values, printed
    assert 'holder=pid:' in census['blocked'] and 'live' in census['blocked'], census


def test_the_pid_probe_answers_for_this_process_and_for_a_reaped_child():
    """The probe is a real syscall, not a hardcoded verdict: alive here, dead for a reaped child."""
    import os
    import subprocess

    from hermes_state_admission_frontier import _holder_pid, _pid_alive

    assert _pid_alive(os.getpid()) is True
    import sys

    child = subprocess.Popen([sys.executable, '-c', 'pass'])
    child.wait()
    assert _pid_alive(child.pid) is False, f'reaped child {child.pid} still answers'
    assert _holder_pid('pid=35864:turn=local:platform=cli') == 35864
    assert _holder_pid('turn=local:platform=cli') is None


def test_a_LIVE_head_is_NEVER_woken__zero_calls_no_stamp_and_no_quarantine_line(tmp_path):
    """THE BLOCKING ONE. A live head fell through to `wake()` on every floor - the cadence becoming
    the siren it was written to silence, on every busy lane, forever.

    A live turn ends, and `wake_after_worker` schedules the drain behind it; the cadence owes it
    nothing. Without this assertion the regression is invisible, because the failure looks like
    activity.
    """
    import asyncio

    import pytest

    from hermes_state_admission_frontier import (
        _observed_since_key,
        admission_cadence_loop,
    )

    db, epoch = _store(tmp_path)
    head = _claim(db, epoch)
    _lease(db, expires_in=7200.0)
    t0 = time.time()

    woke, lines = [], []

    async def sleep(_seconds):
        if lines:
            raise SystemExit
        await asyncio.sleep(0)

    def wake(session_id):
        woke.append(session_id)

    with pytest.raises(SystemExit):
        asyncio.run(admission_cadence_loop(db, lambda: ['s'], wake, interval_s=CADENCE_INTERVAL_S,
                                           sleep=sleep, now=lambda: t0, out=lines.append))

    assert woke == [], f'a live head was woken: {woke}'
    assert not [l for l in lines if l.startswith('quarantine:')], lines
    assert _row(db, head['admission_id'])['status'] == 'started'
    assert _quarantine_rows(db) == []
    with db._read_ctx() as conn:
        saved = conn.execute('SELECT value FROM state_meta WHERE key=?',
                             (_observed_since_key('started', head['admission_id']),)).fetchone()
    assert saved is None, 'a live head must not even be stamped as observed'
    assert any(l.startswith('frontier') or 'queued_status=' in l for l in lines), lines


def test_the_UNKNOWN_and_STARTED_clocks_do_not_spend_each_other(tmp_path):
    """ONE CLOCK, TWO POPULATIONS: the clocks used to be keyed by session, so a pass of one kind
    reset the other kind's `first_seen` and NEITHER population ever reached the threshold - a silent
    non-repair that looks like the cadence working.
    """
    from hermes_state_admission_frontier import quarantine_started, quarantine_unknown

    db, epoch = _store(tmp_path)
    unknown = _admit(db, epoch, 'r-unknown')
    _make_unknown(db, unknown['admission_id'])
    t0 = time.time()

    assert quarantine_unknown(db, 's', store='x', now=t0, out=lambda line: None) is None  # stamps t0

    # a STARTED head exists and is orphaned: its pass runs between two passes of the unknown path.
    # Stated in the store rather than claimed, because a claim is exactly what an `unknown` row
    # refuses - and the clock is what this test measures, not the claim.
    started = _admit(db, epoch, 'r-head')
    db._execute_write(lambda conn: conn.execute(
        "UPDATE session_admissions SET status='started', owner_epoch=?, generation=1 "
        "WHERE admission_id=?", (epoch, started['admission_id'])))
    assert quarantine_started(db, 's', store='x', now=t0 + 600, out=lambda line: None) is None

    lines = []
    moved = quarantine_unknown(db, 's', store='x', now=t0 + QUARANTINE_AGE_S + 1, out=lines.append)
    assert moved is not None, 'the unknown clock was reset by the started pass'
    assert moved['admission_id'] == unknown['admission_id']
    assert f'since={__import__("datetime").datetime.fromtimestamp(t0, __import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}' in lines[0], lines[0]


def test_the_PRINTED_age_NAMES_its_origin(tmp_path):
    """The number is the OBSERVER's age, not the row's: `session_admissions` carries no claim
    instant. A line reading `age=1801s` is read by every following reader as the turn's age, so the
    label names the origin and the instant is printed beside it.
    """
    from hermes_state_admission_frontier import quarantine_unknown

    db, epoch = _store(tmp_path)
    row = _admit(db, epoch, 'r-unknown')
    _make_unknown(db, row['admission_id'])
    t0 = time.time()
    lines = []

    assert quarantine_unknown(db, 's', store='x', now=t0, out=lines.append) is None
    assert quarantine_unknown(db, 's', store='x', now=t0 + QUARANTINE_AGE_S + 5,
                              out=lines.append) is not None

    line = lines[-1]
    assert 'observed_age=' in line and 'since=' in line, line
    assert ' age=' not in line, f'the observer\'s age is printed under the row\'s name: {line}'
    assert 'since=' + __import__('datetime').datetime.fromtimestamp(
        t0, __import__('datetime').timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ') in line, line
