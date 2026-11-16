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

    def line(self) -> str:
        """The pass's one report line: every number read from the store, never inferred."""
        parts = [f'lane={self.lane}', f'settled={len(self.settled)}',
                 f'frontier={self.frontier_before}->{self.frontier_after}',
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
    report = DrainReport(lane=session_id, frontier_before=_frontier_now(db, session_id))
    while True:
        row = claim_session_input(db, epoch=epoch, session_id=session_id)
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
    out(report.line())
    if report.stranded and filer is not False:
        report.card = (filer or file_lane_card)(report)
    return report


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
