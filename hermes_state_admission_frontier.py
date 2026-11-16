"""The admission frontier: what a DM drain pass may leave behind, and what it must never skip.

A session's FIFO is ordered by ``seq``. A pass may only move its high-water mark - the
*frontier* - to the highest seq whose every LOWER seq is terminal. A row in ``queued`` or
``started`` pins the frontier at its own ``seq - 1``: the pass stops there, retries that row on
the next pass, and never claims anything above it while it is unsettled.

A pending row that sits BELOW the frontier is *stranded*: a terminal row was written above it
(a transfer requeued into its old place, an admission rehomed by a compression reset), so a drain
that resumes from the frontier never returns to it while the row still counts as queued. This
module makes that shape impossible at the writer that creates it (see
``hermes_state_runtime.rehome_queued_inputs``) and, for rows already stranded by an older
release, VISIBLE: every pass prints ``stranded=<n> oldest=<seq> age=<s>`` and files/refreshes the
lane's card instead of only logging.

A receipt is an ADMISSION, not a delivery: :func:`drain_pass` reads the settled row back from the
store and reports the status it read. Nothing here calls a row sent on the strength of a claim.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from hermes_state_runtime import (claim_session_input, get_session_admission, release_session_input,
                                  settle_session_input)

#: Statuses that are not settled: either of them PINS the frontier at its own seq.
PENDING = ('queued', 'started')

#: ``state_meta`` key holding when a lane's oldest stranded row was first seen stranded.
STRANDED_SINCE_PREFIX = 'gateway.admission.stranded_since.v1.'

#: Idempotency key prefix of a lane's strand card: the same lane always refreshes its own card.
CARD_KEY_PREFIX = 'admission-strand:'


@dataclass
class Stranded:
    """The lane's stranded rows: how many, the oldest seq, the terminal seq above it, its age."""

    count: int = 0
    oldest_seq: Optional[int] = None
    above_seq: Optional[int] = None
    age_seconds: Optional[float] = None

    def __bool__(self) -> bool:
        return self.count > 0


@dataclass
class DrainReport:
    """One pass's evidence, in the terms the caller may report: statuses read from the store."""

    lane: str
    settled: list[dict[str, Any]] = field(default_factory=list)
    stopped_at: Optional[int] = None
    failure: Optional[str] = None
    frontier_before: int = 0
    frontier_after: int = 0
    stranded: Stranded = field(default_factory=Stranded)
    card: Optional[str] = None
    #: ``<status>@<seq>`` of the row that stops the lane claiming anything at all, when one does.
    blocked: Optional[str] = None
    #: The lane's census at the moment the pass looked: queued rows and their seq span. Printed on
    #: EVERY pass, because the frontier alone cannot distinguish "empty" from "55 waiting".
    queued: int = 0
    seq_min: Optional[int] = None
    seq_max: Optional[int] = None

    def line(self) -> str:
        """The pass's one report line: every number read from the store, never inferred."""
        span = (f'seq={self.seq_min}..{self.seq_max}' if self.seq_min is not None else 'seq=none')
        parts = [f'lane={self.lane}', f'settled={len(self.settled)}',
                 f'frontier={self.frontier_before}->{self.frontier_after}',
                 f'queued={self.queued}', span,
                 f'stranded={self.stranded.count}']
        if self.blocked:
            parts.append(f'blocked={self.blocked}')
        if self.stranded:
            parts += [f'oldest={self.stranded.oldest_seq}', f'above={self.stranded.above_seq}',
                      f'age={self.stranded.age_seconds:.0f}s']
        if self.stopped_at is not None:
            parts += [f'pinned={self.stopped_at}', f'failure={self.failure}']
        if self.card:
            parts.append(f'card={self.card}')
        return ' '.join(parts)


def frontier_seq(conn, session_id: str) -> int:
    """The highest seq whose every lower seq is terminal (0 for an empty or all-pending FIFO)."""
    pending = conn.execute("SELECT MIN(seq) FROM session_admissions WHERE target_session_id=? "
                           "AND status!='terminal'", (session_id,)).fetchone()[0]
    if pending is not None:
        return int(pending) - 1
    return int(conn.execute("SELECT COALESCE(MAX(seq),0) FROM session_admissions "
                            "WHERE target_session_id=?", (session_id,)).fetchone()[0] or 0)


def last_terminal_seq(conn, session_id: str) -> Optional[int]:
    value = conn.execute("SELECT MAX(seq) FROM session_admissions WHERE target_session_id=? "
                         "AND status='terminal'", (session_id,)).fetchone()[0]
    return None if value is None else int(value)


def stranded_rows(conn, session_id: str) -> list[dict]:
    """Pending rows BELOW the lane's last terminal row, oldest first (payload decoded)."""
    above = last_terminal_seq(conn, session_id)
    if above is None:
        return []
    rows = conn.execute("SELECT * FROM session_admissions WHERE target_session_id=? "
                        "AND status IN ('queued','started') AND seq<? ORDER BY seq",
                        (session_id, above)).fetchall()
    return [dict(row) | {'payload': json.loads(row['payload_json'])} for row in rows]


def stranded(db, session_id: str, *, now: Optional[float] = None) -> Stranded:
    """The lane's stranded census, aged from the first pass that saw the OLDEST row stranded.

    The store keeps no admission timestamp, so the age is the time since this lane's oldest
    stranded row was first observed: durable, monotone for as long as the row stays stranded, and
    reset the moment the oldest row changes or the lane is clean.
    """
    with db._read_ctx() as conn:
        rows = stranded_rows(conn, session_id)
        above = last_terminal_seq(conn, session_id)
    if not rows:
        _forget_stranded_since(db, session_id)
        return Stranded()
    stamp = _remember_stranded_since(db, session_id, int(rows[0]['seq']), now=now)
    age = max(0.0, (time.time() if now is None else now) - float(stamp))
    return Stranded(count=len(rows), oldest_seq=int(rows[0]['seq']), above_seq=above, age_seconds=age)


def _stranded_since_key(session_id: str) -> str:
    return STRANDED_SINCE_PREFIX + session_id


def _remember_stranded_since(db, session_id: str, oldest_seq: int, *, now: Optional[float]) -> float:
    key = _stranded_since_key(session_id)
    now = time.time() if now is None else now

    def write(conn):
        saved = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
        if saved is not None:
            stamp = json.loads(saved[0])
            if stamp.get('seq') == oldest_seq:
                return float(stamp['first_seen'])
        conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE '
                     'SET value=excluded.value', (key, json.dumps({'seq': oldest_seq, 'first_seen': now})))
        return float(now)
    return db._execute_write(write)


def _forget_stranded_since(db, session_id: str) -> None:
    key = _stranded_since_key(session_id)
    db._execute_write(lambda conn: conn.execute('DELETE FROM state_meta WHERE key=?', (key,)))


def drain_pass(db, *, epoch: int, session_id: str, run: Callable[[dict], Any], filer=None,
               now: Optional[float] = None, out: Callable[[str], Any] = print) -> DrainReport:
    """Drain ``session_id``'s FIFO in seq order, never advancing past an unsettled row.

    ``run(row)`` performs the turn's work for one claimed admission. When it raises, the claim is
    RELEASED back to ``queued`` at its own seq and the pass stops: the failure pins the frontier,
    no later row is claimed, and the next pass retries the very same row first. Every settled row
    is read back from the store, so ``report.settled`` names the status it read rather than
    assuming a claim was a delivery.

    ``stranded`` rows (pending below the lane's last terminal row, which a correct writer no longer
    creates) never go unreported: the pass prints them and files/refreshes the lane's card through
    ``filer`` (``None``: :func:`file_lane_card`, ``False``: card filing disabled).
    """
    census = _lane_census(db, session_id)
    report = DrainReport(lane=session_id, frontier_before=_frontier_now(db, session_id),
                         queued=census['queued'], seq_min=census['seq_min'],
                         seq_max=census['seq_max'], blocked=census['blocked'])
    while True:
        try:
            row = claim_session_input(db, epoch=epoch, session_id=session_id)
        except Exception as exc:  # health: allow BLE001 -- the claim's own refusal is evidence:
            # an `unknown` head makes the runtime refuse to claim AT ALL (RuntimeStoreError
            # 'unknown_execution'). That refusal is the blocker the pass must REPORT, not an
            # exception it may leak into the turn loop: the lane is frozen, the census line says
            # by which row, and the quarantine (below) is what ends the outage.
            if getattr(exc, 'reason', None) not in {'unknown_execution', 'session_busy'}:
                raise
            report.blocked = census['blocked'] or f"refused:{getattr(exc, 'reason', 'unknown')}"
            break
        if not isinstance(row, dict):
            break  # nothing queued (None) or the FIFO moved under us (False): re-read next pass
        try:
            run(row)
        except Exception as exc:  # health: allow BLE001 -- the pass's boundary: any turn failure must
            # release the claim and stop the pass (it is reported in `report.failure`), never escape
            # and leave the row `started`; the specific exception type is evidence, not a decision.
            release_session_input(db, epoch=epoch, admission_id=row['admission_id'],
                                  generation=row['generation'])
            report.stopped_at = int(row['seq'])
            report.failure = f'{type(exc).__name__}: {exc}' if str(exc) else type(exc).__name__
            break
        settled = settle_session_input(db, epoch=epoch, admission_id=row['admission_id'],
                                       generation=row['generation'], outcome='completed')
        read = get_session_admission(db, admission_id=settled['admission_id'])
        report.settled.append({'seq': int(row['seq']), 'admission_id': row['admission_id'],
                               'status': read['status'], 'outcome': read['outcome']})
    report.frontier_after = _frontier_now(db, session_id)
    report.stranded = stranded(db, session_id, now=now)
    after = _lane_census(db, session_id)
    report.queued, report.seq_min, report.seq_max = after['queued'], after['seq_min'], after['seq_max']
    report.blocked = after['blocked']
    # A PASS THAT FINDS NOTHING IS SILENT (ruling on t-76c210202a). The cadence calls this pass on a
    # timer, so a line per tick would become the flood that hides the next blocker; a line is written
    # when the pass has something a reader must act on - settled work, a blocker, a strand, a pin.
    if report.settled or report.blocked or report.stranded or report.stopped_at is not None:
        out(report.line())
    if report.stranded and filer is not False:
        report.card = (filer or file_lane_card)(report)
    return report


def _lane_census(db, session_id: str) -> dict:
    """``queued`` count and seq span of the lane's non-terminal rows, plus its first blocker."""
    with db._read_ctx() as conn:
        row = conn.execute("SELECT SUM(status='queued') AS queued, MIN(seq) AS lo, MAX(seq) AS hi "
                           "FROM session_admissions WHERE target_session_id=? AND status!='terminal'",
                           (session_id,)).fetchone()
        blocked = conn.execute("SELECT status, seq FROM session_admissions WHERE "
                               "target_session_id=? AND status IN ('started','unknown') "
                               "ORDER BY seq LIMIT 1", (session_id,)).fetchone()
    return {'queued': int((row['queued'] if row else 0) or 0),
            'seq_min': None if not row or row['lo'] is None else int(row['lo']),
            'seq_max': None if not row or row['hi'] is None else int(row['hi']),
            'blocked': (f"{blocked['status']}@{blocked['seq']}" if blocked else None)}


def _frontier_now(db, session_id: str) -> int:
    with db._read_ctx() as conn:
        return frontier_seq(conn, session_id)


def file_lane_card(report: DrainReport, *, board: Optional[str] = None) -> str:
    """File or refresh the lane's strand card: one card per lane, a comment per pass.

    Idempotent by construction: the card's ``idempotency_key`` is the lane's, so a second stranded
    pass comments on the card the first one filed instead of filing another.
    """
    from hermes_cli import kanban_db
    from hermes_cli.kanban_db_connect import connect_closing

    title = (f'DM drain BLOCKED on {report.lane}' if report.blocked
             else f'DM drain stranded on {report.lane}')
    body = (f'`{report.lane}` has {report.stranded.count} queued admission(s) below its terminal '
            f'frontier. Oldest seq={report.stranded.oldest_seq}, terminal seq above it '
            f'={report.stranded.above_seq}, stranded for {report.stranded.age_seconds:.0f}s.\n\n'
            f'Drain line: `{report.line()}`\n\n')
    if report.blocked:
        body += (f'The lane cannot claim ANY row while `{report.blocked}` is unsettled: the FIFO may '
                 f'not be overtaken past an execution whose outcome the runtime does not know. '
                 f'Resolve that row (``hermes state resolve-unknown``) or cancel it; the pass retries '
                 f'the head on the next turn. This card is the report - nothing else would say so.\n\n')
    else:
        body += (f'The drain retries the pinned row first each pass; settle it (or cancel it) and the '
                 f'row stops pinning the frontier.\n\n')
    card = f'[{report.line()}]'
    with connect_closing(board=board) as conn:
        task_id = kanban_db.create_task(conn, title=title, body=body, created_by='dm-drain',
                                        idempotency_key=CARD_KEY_PREFIX + report.lane,
                                        initial_status='blocked', board=board)
        kanban_db.add_comment(conn, task_id, 'dm-drain', card)
    return task_id


# --------------------------------------------------------------------------- #
# a lane that cannot advance at all
# --------------------------------------------------------------------------- #
#: ``state_meta`` key holding when the lane's blocker was last reported (the log throttle).
BLOCKED_REPORTED_PREFIX = 'gateway.admission.blocked_reported.v1.'

#: Seconds between two reports of the same blocker. The claim path runs this on every pass; a lane
#: wedged for hours must be visible without writing one log line per arriving message.
BLOCKED_REPORT_INTERVAL_S = 300.0


def blocking_rows(conn, session_id: str) -> list[dict]:
    """The lane's non-terminal rows that are not claimable FIFO heads, oldest first.

    ``queued`` is claimable; ``started`` and ``unknown`` are what pin a lane. ``unknown`` is the
    harder case: the runtime does not know whether that turn is still running, so the FIFO may not
    be advanced past it - the freeze is the safety property. What was missing is that the freeze was
    silent while messages kept queueing behind it.
    """
    rows = conn.execute("SELECT seq, status, admission_id, generation FROM session_admissions "
                        "WHERE target_session_id=? AND status IN ('started','unknown') ORDER BY seq",
                        (session_id,)).fetchall()
    return [dict(row) for row in rows]


def _report_due(db, session_id: str, blocker: dict, *, now: Optional[float]) -> bool:
    """True once per ``BLOCKED_REPORT_INTERVAL_S`` for the SAME blocker (a new one reports at once)."""
    key = BLOCKED_REPORTED_PREFIX + session_id
    now = time.time() if now is None else now

    def write(conn):
        saved = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
        if saved is not None:
            try:
                last = json.loads(saved[0])
            except ValueError:
                last = {}
            if (last.get('admission_id') == blocker['admission_id']
                    and now - float(last.get('at') or 0) < BLOCKED_REPORT_INTERVAL_S):
                return False
        conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE '
                     'SET value=excluded.value',
                     (key, json.dumps({'admission_id': blocker['admission_id'], 'at': now})))
        return True
    return db._execute_write(write)


def lane_blocked(db, session_id: str, *, filer=None, now: Optional[float] = None,
                 out: Callable[[str], Any] = print) -> Optional[str]:
    """A lane whose FIFO cannot advance SAYS SO: census line to ``out`` and one card. The line.

    Called from the claim path, where the lane has just found a non-claimable head. Returns None
    when nothing is blocking (the caller's freeze had another cause). The freeze itself is never
    lifted here: an unsettled execution must not be overtaken, and only its owner may settle it.
    """
    with db._read_ctx() as conn:
        blockers = blocking_rows(conn, session_id)
    if not blockers:
        return None
    census = stranded(db, session_id, now=now)
    report = DrainReport(lane=session_id, frontier_before=_frontier_now(db, session_id),
                         stranded=census, blocked=f"{blockers[0]['status']}@{blockers[0]['seq']}")
    if _report_due(db, session_id, blockers[0], now=now):
        out(report.line() + f' blocker_admission={blockers[0]["admission_id"]}'
                            f' blockers={len(blockers)}')
    if filer is not False:
        try:
            report.card = (filer or file_lane_card)(report)
        except Exception:  # health: allow BLE001 -- a board that cannot be written must not turn a
            # reporting path into a second failure; the log line above already carries the census.
            import logging
            logging.getLogger(__name__).warning(
                'Could not file the blocked-drain card for %s; the census line above carries the '
                'evidence', session_id, exc_info=True)
    return report.line()


# --------------------------------------------------------------------------- #
# the cadence: a settled blocker must not need an inbound event (ruling 2)
# --------------------------------------------------------------------------- #
#: ``state_meta`` key holding the lane's last cadence pass.
CADENCE_PREFIX = 'gateway.admission.cadence.v1.'

#: The floor between two passes for ONE lane. A floor, not a period: a busy turn loop may call
#: :func:`tick` as often as it likes and the store is read at most once per interval per lane.
CADENCE_INTERVAL_S = 30.0


def cadence_due(db, session_id: str, *, interval_s: float = CADENCE_INTERVAL_S,
                now: Optional[float] = None) -> bool:
    """True when this lane may take a pass now, and durably marks the pass as taken.

    Idempotent by construction: the stamp and the answer are written in ONE write, so two callers
    that race on the same lane cannot both be told ``True``. The stamp is the floor, which is what
    keeps a cadence owned by the turn loop from becoming a spin.
    """
    key = CADENCE_PREFIX + session_id
    now = time.time() if now is None else now

    def write(conn):
        saved = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
        if saved is not None:
            try:
                last = float(json.loads(saved[0]).get('at') or 0.0)
            except (ValueError, TypeError):
                last = 0.0
            if now - last < interval_s:
                return False
        conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE '
                     'SET value=excluded.value', (key, json.dumps({'at': now})))
        return True
    return bool(db._execute_write(write))


def tick(db, *, epoch: int, session_id: str, run: Callable[[dict], Any], filer=None,
         interval_s: float = CADENCE_INTERVAL_S, now: Optional[float] = None,
         out: Callable[[str], Any] = print) -> Optional[DrainReport]:
    """One bounded cadence tick for ``session_id``: no inbound event required.

    Called from the turn loop, so the answer to "may I pass now" is stored, not remembered: the
    process may be restarted, and a lane whose blocker was settled elsewhere must resume on the
    next tick without anybody sending it a message. Returns ``None`` when this lane is not due, or
    when the pass found nothing at all - a tick that says nothing is the normal case.
    """
    if not cadence_due(db, session_id, interval_s=interval_s, now=now):
        return None
    census = _lane_census(db, session_id)
    if not census['queued'] and not census['blocked']:
        return None
    report = drain_pass(db, epoch=epoch, session_id=session_id, run=run, filer=filer, now=now,
                        out=out)
    return report


# --------------------------------------------------------------------------- #
# the quarantine: a freeze must expire into a NAMED state (ruling 1)
# --------------------------------------------------------------------------- #
#: Table holding quarantined admissions, byte for byte, for their owner.
QUARANTINE_TABLE = 'session_admission_quarantine'

#: ``state_meta`` key holding when this lane's oldest ``unknown`` row was first seen unknown.
UNKNOWN_SINCE_PREFIX = 'gateway.admission.unknown_since.v1.'

#: The STATED AGE after which an ``unknown`` row is quarantined and its lane resumes. Seconds, and
#: the unit is carried in every line that prints it, because a number without its unit is the next
#: defect: 1,800 s = 30 min of messages queued behind one lost turn.
QUARANTINE_AGE_S = 1800.0

QUARANTINE_OUTCOME_PREFIX = 'unknown_quarantined:'


def _ensure_quarantine_table(conn) -> None:
    conn.execute(f'CREATE TABLE IF NOT EXISTS {QUARANTINE_TABLE} ('
                 'seq INTEGER PRIMARY KEY, admission_id TEXT NOT NULL UNIQUE, request_id TEXT, '
                 'principal_id TEXT, target_session_id TEXT, lineage_json TEXT, payload_json TEXT, '
                 'payload_digest TEXT, intent TEXT, status_at_quarantine TEXT, outcome TEXT, '
                 'owner_epoch INTEGER, generation INTEGER, quarantined_at REAL NOT NULL, '
                 'age_seconds REAL NOT NULL, store TEXT NOT NULL, resolution TEXT)')


def _unknown_since_key(session_id: str) -> str:
    return UNKNOWN_SINCE_PREFIX + session_id


def _forget_unknown_since(db, session_id: str) -> None:
    key = _unknown_since_key(session_id)
    db._execute_write(lambda conn: conn.execute('DELETE FROM state_meta WHERE key=?', (key,)))


def quarantine_unknown(db, session_id: str, *, threshold_s: float = QUARANTINE_AGE_S,
                       store: str = '', now: Optional[float] = None,
                       out: Callable[[str], Any] = print) -> Optional[dict]:
    """MOVE this lane's oldest ``unknown`` admission into quarantine once it is old enough.

    The FREEZE IS NOT LIFTED: unruly nothing is delivered out of order, nothing is settled on the
    owner's behalf, and the row is never re-emitted as a fresh turn. What expires is the OUTAGE -
    the lane stops being held hostage by one turn whose outcome this runtime will never learn.

    The copy is verbatim (every column, plus ``store`` and the age it was quarantined at), the live
    row becomes ``terminal`` with outcome ``unknown_quarantined:<seq>`` so the FIFO may advance and
    the ledger still names where the row went, and the owner's later resolution makes the copy
    ELIGIBLE (see :func:`mark_quarantine_eligible`) - never automatic, never silent.
    """
    now = time.time() if now is None else now
    with db._read_ctx() as conn:
        row = conn.execute("SELECT * FROM session_admissions WHERE target_session_id=? AND "
                           "status='unknown' ORDER BY seq LIMIT 1", (session_id,)).fetchone()
    if row is None:
        _forget_unknown_since(db, session_id)
        return None
    row = dict(row)

    key = _unknown_since_key(session_id)

    def stamp(conn):
        saved = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
        if saved is not None:
            try:
                last = json.loads(saved[0])
            except ValueError:
                last = {}
            if last.get('admission_id') == row['admission_id']:
                return float(last.get('first_seen') or now)
        conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE '
                     'SET value=excluded.value',
                     (key, json.dumps({'admission_id': row['admission_id'], 'first_seen': now})))
        return float(now)

    first_seen = float(db._execute_write(stamp))
    age = max(0.0, now - first_seen)
    if age < threshold_s:
        return None

    def move(conn):
        _ensure_quarantine_table(conn)
        conn.execute(f'INSERT OR IGNORE INTO {QUARANTINE_TABLE} (seq, admission_id, request_id, '
                     'principal_id, target_session_id, lineage_json, payload_json, payload_digest, '
                     'intent, status_at_quarantine, outcome, owner_epoch, generation, '
                     'quarantined_at, age_seconds, store, resolution) '
                     'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)',
                     (row['seq'], row['admission_id'], row['request_id'], row['principal_id'],
                      row['target_session_id'], row['lineage_json'], row['payload_json'],
                      row['payload_digest'], row['intent'], row['status'], row.get('outcome'),
                      row.get('owner_epoch'), row.get('generation'), now, age, store))
        conn.execute('UPDATE session_admissions SET status=?, outcome=?, owner_epoch=NULL, '
                     'generation=NULL WHERE admission_id=? AND status=?',
                     ('terminal', f"{QUARANTINE_OUTCOME_PREFIX}{row['seq']}",
                      row['admission_id'], 'unknown'))
        return conn.execute(f'SELECT * FROM {QUARANTINE_TABLE} WHERE admission_id=?',
                            (row['admission_id'],)).fetchone()

    moved = dict(db._execute_write(move))
    _forget_unknown_since(db, session_id)
    out(f"quarantine: {QUARANTINE_OUTCOME_PREFIX}{moved['seq']} lane={session_id} "
        f"age={moved['age_seconds']:.0f}s unit=seconds threshold={threshold_s:.0f}s unit=seconds "
        f"store={store} admission={moved['admission_id']}")
    return moved


def mark_quarantine_eligible(db, admission_id: str, *, by: str) -> bool:
    """An owner has resolved the quarantined turn: it becomes ELIGIBLE, and nothing else happens.

    Eligible is a state a human may act on; it is not a re-emission. Nothing is delivered, no new
    admission row is created, and the quarantined copy stays byte for byte what it was.
    """
    def write(conn):
        _ensure_quarantine_table(conn)
        cur = conn.execute(f'UPDATE {QUARANTINE_TABLE} SET resolution=? WHERE admission_id=? '
                           'AND resolution IS NULL', (by, admission_id))
        return cur.rowcount > 0
    return bool(db._execute_write(write))


def _store_label(db) -> str:
    """The lane's own store path, printed with every quarantined row: a line that names an age
    without the store it was measured in is the next defect."""
    path = getattr(db, 'db_path', None)
    return str(path) if path else ''


async def admission_cadence_loop(db, lanes, wake, *, interval_s: float = CADENCE_INTERVAL_S,
                                 sleep=None, now=None, log=None, out=print) -> None:
    """The per-lane cadence owned by the turn loop: a lane resumes with NO inbound event.

    A blocker settled elsewhere (its owner resolving an ``unknown`` turn, a worker ending) leaves
    messages queued behind a lane that nothing will touch again until somebody sends it another
    message. This loop closes that hole: every ``interval_s`` it asks each live lane whether the
    floor has passed, and if that lane has work it calls ``wake(session_id)`` - the runner's own
    ``wake_after_worker``, which schedules the drain that already exists. It adds no second turn
    loop and runs no turn itself.

    Bounded and idempotent: the interval is a FLOOR per lane (``cadence_due`` stamps it durably in
    the same write that answers), so a wake that finds nothing costs one read, and two loop
    instances cannot both pass a lane in one window. A lane with an empty queue and no blocker is
    skipped silently: a cadence that logs every tick is the flood that hides the next blocker.

    THIS IS THE CALLER OF THE QUARANTINE. When the lane's blocker is ``unknown``, the pass first
    calls :func:`quarantine_unknown` - the freeze expires into its named quarantine - and then wakes
    the lane unconditionally, so the same pass that retires the blocker delivers what was behind it.
    Without this, the cadence is a siren: it wakes the lane every floor into a refusal that names a
    blocker nothing will ever move. ``tick`` cannot be this place, because it takes ``run`` and a
    cadence has no turn to run.
    """
    sleep = sleep or asyncio.sleep
    while True:
        await sleep(interval_s)  # the floor first: at startup the platforms have not connected yet
        try:
            session_ids = list(lanes())
        except Exception:  # health: allow BLE001 -- a lane list that cannot be read must not kill
            # the cadence: the next tick tries again, and the loop's own liveness is the evidence.
            if log is not None:
                log('admission cadence: could not list lanes; retrying next tick')
            continue
        for session_id in session_ids:
            try:
                if not cadence_due(db, session_id, interval_s=interval_s,
                                   now=None if now is None else now()):
                    continue
                census = _lane_census(db, session_id)
                if not census['queued'] and not census['blocked']:
                    continue
                if str(census['blocked'] or '').startswith('unknown@'):
                    quarantine_unknown(db, session_id, store=_store_label(db),
                                       now=None if now is None else now(), out=out)
                    census = _lane_census(db, session_id)   # the quarantine MOVED the head
                    if not census['queued'] and not census['blocked']:
                        continue
                wake(session_id)
            except Exception:  # health: allow BLE001 -- one unbounded lane must not stop the rest;
                # the exception is never swallowed silently, it is logged with the lane.
                if log is not None:
                    log(f'admission cadence: wake of {session_id} failed', )
