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


def test_the_lane_card_is_filed_once_and_refreshed_by_key(tmp_path, monkeypatch):
    from hermes_cli import kanban_db
    from hermes_cli import kanban_db_connect
    from hermes_state_admission_frontier import (CARD_KEY_PREFIX, DrainReport, Stranded,
                                                 file_lane_card)

    calls = []

    class _Conn:
        def __enter__(self):
            return object()

        def __exit__(self, *exc_info):
            return False

    monkeypatch.setattr(kanban_db_connect, 'connect_closing', lambda **kwargs: _Conn())
    monkeypatch.setattr(kanban_db, 'create_task',
                        lambda conn, **kwargs: calls.append(('create', kwargs['idempotency_key']))
                        or 'card-1')
    monkeypatch.setattr(kanban_db, 'add_comment',
                        lambda conn, task_id, author, body: calls.append(('comment', task_id, body)))

    report = DrainReport(lane='platform-stl', stranded=Stranded(3, 9493, 10083, 754.0),
                         frontier_before=9492, frontier_after=9492)
    assert file_lane_card(report) == 'card-1'
    assert file_lane_card(report) == 'card-1'  # the second pass refreshes, it does not re-file
    assert calls[0] == ('create', CARD_KEY_PREFIX + 'platform-stl')
    assert [call[0] for call in calls] == ['create', 'comment', 'create', 'comment']
    assert 'stranded=3' in calls[-1][2] and 'oldest=9493' in calls[-1][2]


def _mark(db, admission_id, status):
    """Put a row in a given status directly: the states a crash or a boot recovery leaves behind."""
    db._execute_write(lambda conn: conn.execute(
        'UPDATE session_admissions SET status=? WHERE admission_id=?', (status, admission_id)))


def test_a_lane_that_cannot_advance_says_so_with_its_census_and_one_card(tmp_path):
    """An ``unknown`` row freezes the FIFO by design; the freeze must never be SILENT.

    Production shape (measured on four lanes): one lost turn sits ``unknown`` at a low seq, every
    later admission keeps counting as queued behind it, and the drain refuses every pass without a
    word. The lane is not fixed here - only its owner may settle an unknown execution - but the pass
    now reports it and files the lane's card.
    """
    from hermes_state_admission_frontier import blocking_rows, lane_blocked

    db, epoch = _store(tmp_path)
    rows = [_admit(db, epoch, f'r{n}') for n in range(4)]
    _settle_all(db, epoch)
    _requeue_legacy(db, rows[0]['admission_id'])           # stranded shape, as an older release left it
    _requeue_legacy(db, rows[2]['admission_id'])
    _mark(db, rows[1]['admission_id'], 'unknown')          # the lost turn that pins the lane

    lines, cards = [], []

    def filer(report):
        cards.append(report)
        return 'card-1'

    line = lane_blocked(db, 's', filer=filer, out=lines.append, now=1000.0)

    assert line and 'blocked=unknown@2' in line
    assert 'stranded=2' in line and 'oldest=1' in line, line   # the census, not a log line alone
    assert len(lines) == 1 and cards and cards[0].blocked == 'unknown@2'

    # Same blocker, same interval: the card is refreshed but the log is not spammed per arriving message.
    lane_blocked(db, 's', filer=filer, out=lines.append, now=1030.0)
    assert len(lines) == 1 and len(cards) == 2
    # A DIFFERENT blocker reports at once - it is new information.
    _mark(db, rows[1]['admission_id'], 'terminal')   # the owner settled the lost turn
    _mark(db, rows[2]['admission_id'], 'unknown')
    lane_blocked(db, 's', filer=filer, out=lines.append, now=1035.0)
    assert len(lines) == 2 and 'blocked=unknown@3' in lines[-1]

    # Once nothing pins the lane, there is nothing to report and the drain claims the OLDEST row
    # first - the stranded rows are retried, never skipped.
    _mark(db, rows[2]['admission_id'], 'queued')
    assert lane_blocked(db, 's', filer=filer, out=lines.append, now=1040.0) is None
    seen = []
    drain_pass(db, epoch=epoch, session_id='s', run=lambda row: seen.append(row['seq']), filer=False,
               out=lines.append)
    assert seen == [1, 3], seen


def test_the_claim_path_reports_a_wedge_instead_of_freezing_silently():
    """The reporter must be ON the refusal paths, not merely available to them.

    A unit test of ``lane_blocked`` proves the census; this proves the drain calls it where the
    freeze happens, for both blockers (an ``unknown`` row raising, and an unowned head pausing).
    """
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / 'gateway' / 'session_authority.py'
    tree = ast.parse(source.read_text())

    def method(name):
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return node
        raise AssertionError(f'{name} not found')

    def called(node, attr):
        return [n.lineno for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == attr]

    claim = method('_claim_next')
    refused = [n.lineno for n in ast.walk(claim)
               if isinstance(n, ast.Raise) and 'unknown_execution' in ast.dump(n)]
    assert refused, 'the unknown-row refusal is gone: has the freeze been removed entirely?'
    reports = called(claim, '_report_blocked_drain')
    assert reports and min(reports) < min(refused), (reports, refused)

    drain = method('_drain')
    pauses = [n.lineno for n in ast.walk(drain)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == '_pause'
              and 'session_busy' in ast.dump(n)]
    assert pauses, 'the busy pause is gone from the drain'
    reports = called(drain, '_report_blocked_drain')
    assert reports and reports[0] < min(pauses), (reports, pauses)

    helper = method('_report_blocked_drain')
    assert 'lane_blocked' in ast.dump(helper)


def _claimed_generation(db, admission_id):
    """`claim_session_input(head=...)` answers a boolean; the generation it stamped is on the row."""
    with db._read_ctx() as conn:
        return conn.execute('SELECT generation FROM session_admissions WHERE admission_id=?',
                            (admission_id,)).fetchone()['generation']


# ============================ platform-stl's item 4 (card t-76c210202a) ========================
def test_the_FRONTIER_LINE_distinguishes_empty_from_ignored_and_tells_waiting_from_a_hole(tmp_path):
    """A line without `oldest` cannot distinguish "queue empty" from "queue ignored" - and this
    store carries no per-row instant at all, so a queue's age is only knowable from what a pass
    records about it."""
    from hermes_state_admission_frontier import _frontier_line

    db, epoch = _store(tmp_path)
    # THE RULED SHAPE (platform-stl, t-76c210202a): the SELECTOR's population is `queued_status`,
    # `below_frontier` is the count a frontier-derived predicate cannot express, and both are
    # printed with the store and the instant - a count without those is not a measurement.
    empty = _frontier_line(db, 's', now=1000.0, store='X')
    assert empty.startswith("queued_status=0 below_frontier=0 seq=-..- blocked=- store=X "
                            "instant=1970-01-01T00:16:40Z"), empty
    assert empty.endswith("oldest=-@epoch=- live_epoch=%s skipped_since=- started=-" % epoch), empty

    first = _admit(db, epoch, 'r1')
    second = _admit(db, epoch, 'r2')
    line = _frontier_line(db, 's', now=1000.0, store='X')
    assert line.startswith(f"queued_status=2 below_frontier=0 seq={first['seq']}..{second['seq']} "
                           "blocked=- store=X"), line
    assert line.endswith(f"oldest={first['seq']}@epoch={epoch} live_epoch={epoch} "
                         f"skipped_since={first['seq']}@0s started=-"), line

    # same head, a minute later: the AGE rises on an unchanged seq - waiting, visible as a number
    later = _frontier_line(db, 's', now=1060.0)
    assert f"skipped_since={first['seq']}@60s" in later, later
    # the head is TAKEN and the frontier moves to the next row: a hole would look like the age
    # climbing on the SAME seq, which is the distinction this line exists to make
    rt.claim_session_input(db, epoch=epoch, session_id='s', head=first['admission_id'])
    moved = _frontier_line(db, 's', now=1120.0)
    assert f"oldest={second['seq']}@epoch={epoch}" in moved and "@0s " in moved, moved
    assert f"started={first['seq']}" in moved, moved


def test_eligibility_is_recorded_PER_EPOCH_so_a_returning_row_cannot_re_emit_for_ever(tmp_path):
    """Eligibility per ROW lets a quarantined row come back (a requeued steer lands at its own
    seq), fail the same way and be quarantined again: one emission per pass, for ever."""
    from hermes_state_admission_frontier import (QUARANTINE_AGE_S, epoch_quarantined,
                                                 mark_epoch_quarantined, quarantine_unknown)

    db, epoch = _store(tmp_path)
    assert epoch_quarantined(db, 's', epoch) is None

    first = _admit(db, epoch, 'r1')
    second = _admit(db, epoch, 'r2')
    rt.claim_session_input(db, epoch=epoch, session_id='s')
    # an UNKNOWN head: the writer that claimed it is gone (its epoch is older), so the row's own
    # outcome is unknown and the census reports it as such
    db._execute_write(lambda conn: conn.execute(
        "UPDATE session_admissions SET status='unknown' WHERE admission_id=?",
        (first['admission_id'],)))
    t0 = time.time()
    quarantine_unknown(db, 's', store='x', now=t0, out=lambda line: None)   # stamps the age
    q1 = quarantine_unknown(db, 's', store='x', now=t0 + QUARANTINE_AGE_S + 1,
                            out=lambda line: None)
    assert q1 is not None
    record = epoch_quarantined(db, 's', epoch)
    assert record is not None and record['rows'] == 1, record
    assert record['marked_by'] == 'quarantine_unknown', record

    # the SAME epoch again: the second row is a COUNT on one record, not a new alarm
    mark_epoch_quarantined(db, 's', epoch, by='quarantine_unknown', row_id=second['admission_id'])
    record = epoch_quarantined(db, 's', epoch)
    assert record['rows'] == 2, record
    assert record['first_row'] == first['admission_id'], record


def test_a_pass_claims_the_FIFO_HEAD_not_any_row(tmp_path):
    """181-taken-32-left in one band is what a pass that takes any row produces. The pass claims
    min(seq) per session: the OLDEST queued row is the one that runs, or the queue is a lottery."""
    from hermes_state_admission_frontier import drain_pass

    db, epoch = _store(tmp_path)
    rows = [_admit(db, epoch, 'r%d' % n) for n in range(3)]
    claimed = []

    def run(row):
        claimed.append(row['seq'])

    report = drain_pass(db, epoch=epoch, session_id='s', run=run, now=time.time,
                        out=lambda line: None)
    assert claimed == [r['seq'] for r in rows], (claimed, [r['seq'] for r in rows])
    assert report.line(), "the pass states its frontier"


def test_a_SKIPPED_queued_row_is_counted_below_the_frontier_and_expires_only_after_the_wake(tmp_path):
    """The row that does not announce itself (platform-stl, t-76c210202a item 2).

    `status='queued'` below the highest terminal seq is the 32-row shape: a frontier-derived
    predicate reports the queue as empty while deliverable rows sit in it, so `below_frontier` is a
    printed count - and the row gets the SAME quarantine entry as an `unknown`, with two guards: it
    never expires while the lane is blocked, and the wake has first refusal.
    """
    from hermes_state_admission_frontier import (QUARANTINE_AGE_S, _frontier_line,
                                                 quarantine_skipped)

    db, epoch = _store(tmp_path)
    r1 = _admit(db, epoch, 'r1')          # seq 1 - the one that will be skipped
    r2 = _admit(db, epoch, 'r2')
    r3 = _admit(db, epoch, 'r3')

    # an OUT-OF-SEQUENCE completion: a later row reaches terminal, so the frontier moves ABOVE r1
    # while r1 is still queued. This is written directly because `claim_session_input` is FIFO by
    # construction (it refuses a non-head) - the state is reachable by event ordering, not by claim.
    def complete_later(conn):
        conn.execute("UPDATE session_admissions SET status='terminal', outcome='ok' "
                     "WHERE admission_id=?", (r2['admission_id'],))

    db._execute_write(complete_later)
    with db._read_ctx() as conn:
        r2_seq = conn.execute('SELECT seq FROM session_admissions WHERE admission_id=?',
                              (r2['admission_id'],)).fetchone()['seq']
    assert r2_seq > r1['seq']

    line = _frontier_line(db, 's', now=1000.0, store='X')
    assert 'queued_status=2' in line, line
    assert 'below_frontier=1' in line, line        # r1 is below the frontier, r3 above it

    # GUARD 1: while the lane is BLOCKED the queued row is waiting, not skipped - nothing expires
    claimed = rt.claim_session_input(db, epoch=epoch, session_id='s', head=r1['admission_id'])
    assert isinstance(claimed, dict) and claimed['admission_id'] == r1['admission_id'], claimed
    assert quarantine_skipped(db, 's', store='X', now=1000.0, out=lambda l: None) is None
    assert quarantine_skipped(db, 's', store='X', now=1000.0 + QUARANTINE_AGE_S + 60,
                              out=lambda l: None) is None
    with db._read_ctx() as conn:
        assert conn.execute("SELECT status FROM session_admissions WHERE admission_id=?",
                            (r1['admission_id'],)).fetchone()['status'] == 'started'
    # the block clears and the row is queued again - claimable, and a pass has had its chance to
    # take it, which is the only condition under which "skipped" is a fact about the DRAIN
    def requeue(conn):
        conn.execute("UPDATE session_admissions SET status='queued', outcome=NULL "
                     "WHERE admission_id=?", (r1['admission_id'],))

    db._execute_write(requeue)
    line = _frontier_line(db, 's', now=1000.0, store='X')
    assert 'below_frontier=1' in line and 'blocked=-' in line, line

    # GUARD 2: the first pass only STAMPS the age - a fresh skip is not an expired one
    t0 = 5000.0
    assert quarantine_skipped(db, 's', store='X', now=t0, out=lambda l: None) is None
    lines = []
    moved = quarantine_skipped(db, 's', store='X', now=t0 + QUARANTINE_AGE_S + 1,
                               out=lines.append)

    assert moved is not None and moved['status_at_quarantine'] == 'queued', moved
    with db._read_ctx() as conn:
        live = conn.execute("SELECT status, outcome FROM session_admissions WHERE admission_id=?",
                            (r1['admission_id'],)).fetchone()
    assert live['status'] == 'terminal' and live['outcome'] == f"skipped_queued:{r1['seq']}", live
    assert lines and 'status_at_quarantine=queued' in lines[0], lines
    assert 'below_frontier=yes' in lines[0] and 'unit=seconds' in lines[0], lines
    # and the count is back to zero: the alarm is answered by the same act that repairs it
    assert 'below_frontier=0' in _frontier_line(db, 's', now=t0 + QUARANTINE_AGE_S + 2, store='X')
