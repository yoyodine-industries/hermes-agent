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
import datetime
import json
import os
import re
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


FRONTIER_KEY = 'frontier:{session}'


def _iso(now: float) -> str:
    """The instant, in the one spelling the rest of the fleet prints: a count without its instant is
    a number that cannot be compared with the next one."""
    return datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _frontier_line(db, session_id: str, *, now: Optional[float] = None,
                   live_epoch: Optional[int] = None, store: str = '') -> str:
    """The line printed on EVERY pass, in the ruled shape:

        queued_status=<n> below_frontier=<m> seq=<min>..<max> blocked=<status>@<seq>
        store=<path> instant=<Z> oldest=<seq>@epoch=<e> live_epoch=<E>
        skipped_since=<seq>@<age>s started=<seq>

    ``queued_status`` is the SELECTOR's population (``status='queued'``, ordered by ``seq``);
    ``below_frontier`` counts the queued rows sitting BELOW the highest terminal seq - the rows a
    frontier-derived predicate cannot express and therefore reports as absent. ``m > 0`` is the
    alarm: it is the shape of a skip, and it reads 32 on the store that produced this ruling.

    ``skipped_since`` is this module's own durable observation, because ``session_admissions``
    carries no timestamp at all: an unchanged head shows a RISING age (waiting), a taken head moves
    the seq (progress), and the two are distinguishable only because a pass writes it down.
    """
    now = time.time() if now is None else now
    census = _lane_census(db, session_id, now=now)
    with db._read_ctx() as conn:
        oldest = conn.execute("SELECT seq, owner_epoch FROM session_admissions WHERE "
                              "target_session_id=? AND status='queued' ORDER BY seq LIMIT 1",
                              (session_id,)).fetchone()
        started = conn.execute("SELECT seq FROM session_admissions WHERE target_session_id=? AND "
                               "status='started' ORDER BY seq LIMIT 1", (session_id,)).fetchone()
        if live_epoch is None:
            live_epoch = _current_epoch(conn)
    key = FRONTIER_KEY.format(session=session_id)
    oldest_seq = None if oldest is None else int(oldest['seq'])

    def stamp(conn):
        saved = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
        if saved is not None:
            try:
                last = json.loads(saved[0])
            except ValueError:
                last = {}
            if oldest_seq is not None and last.get('oldest_seq') == oldest_seq:
                return float(last.get('first_seen') or now)
        conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE '
                     'SET value=excluded.value',
                     (key, json.dumps({'oldest_seq': oldest_seq, 'first_seen': now})))
        return float(now)

    first_seen = float(db._execute_write(stamp))
    age = 0.0 if oldest_seq is None else max(0.0, now - first_seen)
    span = ('-' if census['seq_min'] is None else census['seq_min'],
            '-' if census['seq_max'] is None else census['seq_max'])
    return (f"queued_status={census['queued_status']} below_frontier={census['below_frontier']} "
            f"seq={span[0]}..{span[1]} blocked={census['blocked'] or '-'} "
            f"store={store or _store_label(db)} instant={_iso(now)} "
            f"oldest={'-' if oldest_seq is None else oldest_seq}@epoch="
            f"{'-' if oldest is None else oldest['owner_epoch']} "
            f"live_epoch={'-' if live_epoch is None else live_epoch} "
            f"skipped_since="
            f"{'-' if oldest_seq is None else '%d@%.0fs' % (oldest_seq, age)} "
            f"started={'-' if started is None else started['seq']}")


def _lane_census(db, session_id: str, *, now: Optional[float] = None) -> dict:
    """``queued`` count, seq span, and the first blocker AS A RESOLVABLE KIND.

    R7: a ``started`` head is printed ``started(live)`` or ``started(orphan)`` with the epoch it was
    compared against, because the two resolve by DIFFERENT ACTS - one by finishing, one by the
    quarantine - and a census that prints them the same way invites the second act on the first row.
    """
    now = time.time() if now is None else now
    with db._read_ctx() as conn:
        # THE SELECTION PREDICATE IS `status='queued'` ORDERED BY `seq`, FULL STOP (platform-stl,
        # card t-76c210202a). A frontier-derived predicate - "queued means seq > max(terminal)" -
        # cannot express a row that was SKIPPED: it stays status='queued' below the frontier for
        # ever, and the predicate reports the queue as empty while 32 deliverable rows sit in it.
        # `below_frontier` is therefore a REPORTING metric, printed beside the status count because
        # the difference between them IS the defect signature.
        row = conn.execute(
            "SELECT (SELECT COUNT(*) FROM session_admissions WHERE target_session_id=? "
            "        AND status='queued') AS queued_status, "
            "       (SELECT COUNT(*) FROM session_admissions q WHERE q.target_session_id=? "
            "        AND q.status='queued' AND q.seq < (SELECT COALESCE(MAX(t.seq),0) FROM "
            "            session_admissions t WHERE t.target_session_id=? AND t.status='terminal')) "
            "        AS below_frontier, "
            "       (SELECT MIN(seq) FROM session_admissions WHERE target_session_id=? "
            "        AND status='queued') AS lo, "
            "       (SELECT MAX(seq) FROM session_admissions WHERE target_session_id=? "
            "        AND status='queued') AS hi",
            (session_id, session_id, session_id, session_id, session_id)).fetchone()
        blocked = conn.execute("SELECT status, seq, owner_epoch FROM session_admissions WHERE "
                               "target_session_id=? AND status IN ('started','unknown') "
                               "ORDER BY seq LIMIT 1", (session_id,)).fetchone()
        current = _current_epoch(conn)
        holder = None
        workers: list = []
        if blocked is not None and blocked['status'] == 'started':
            holder = _lease_holder(conn, session_id, now=now)
            workers = _live_workers(conn, session_id)
    kind = None
    if blocked is not None:
        if blocked is not None and blocked['status'] == 'started':
            owner_epoch = blocked['owner_epoch']
            stale_epoch = (owner_epoch is not None and current is not None
                           and int(owner_epoch) != int(current))
            holder_pid = _holder_pid((holder or {}).get('holder'))
            holder_alive = holder is not None and _pid_alive(holder_pid)
            # OBSERVED BEATS INFERRED: a pid that answers is live even when the epoch differs,
            # because a live holder cannot be a claim left by a dead run - while the epoch is
            # inferred. The epoch is still PRINTED, so a reader sees both facts.
            state = 'live' if (holder_alive or workers) else 'orphan'
            epoch_note = (f" epoch_differs={owner_epoch}!={current}" if stale_epoch else '')
            holder_note = (f" holder=pid:{holder_pid}:{'live' if holder_alive else 'dead'}"
                           if holder is not None else '')
            worker_note = f" workers={len(workers)}" if workers else ''
            kind = (f"started({state})@{blocked['seq']} epoch={owner_epoch} current={current}"
                    f"{epoch_note}{holder_note}{worker_note}")
        else:
            kind = f"unknown@{blocked['seq']}"
    queued_status = int((row['queued_status'] if row else 0) or 0)
    below = int((row['below_frontier'] if row else 0) or 0)
    return {'queued': queued_status,          # the count the SELECTOR uses
            'queued_status': queued_status,
            'below_frontier': below,          # the count that is > 0 when the drain is skipping
            'seq_min': None if not row or row['lo'] is None else int(row['lo']),
            'seq_max': None if not row or row['hi'] is None else int(row['hi']),
            'blocked': kind}


def _current_epoch(conn) -> Optional[int]:
    row = conn.execute('SELECT epoch FROM runtime_epoch WHERE singleton=1').fetchone()
    return int(row['epoch']) if row is not None else None


def _lease_holder(conn, session_id: str, *, now: float):
    row = conn.execute('SELECT holder, expires_at FROM session_turn_leases WHERE conversation_id=? '
                       'AND expires_at>?', (session_id, now)).fetchone()
    return dict(row) if row is not None else None


def _live_workers(conn, session_id: str) -> list:
    return [r['execution_id'] for r in conn.execute(
        "SELECT execution_id FROM worker_executions WHERE session_id=? AND status IN "
        "('registered','running') ORDER BY execution_id", (session_id,))]


def lease_liveness(db, session_id: str, *, owner_epoch: Optional[int],
                   now: Optional[float] = None) -> dict:
    """R7: is this ``started`` claim held by a LIVE turn, or by one that is demonstrably GONE?

    A ``started`` row held by a live turn and a ``started`` row held by a dead one are
    indistinguishable in a census built on AGE - the lane that prompted this ruling was running the
    very turn the census called a blocker, and an age rule alone would have expired it mid-flight
    and delivered its queue behind an answer still being written. So liveness is decided FIRST, from
    what a running turn actually holds, and age is only the second rung:

      * ``live`` when something is OBSERVED to hold the claim - an unexpired
        ``session_turn_leases`` row whose ``pid`` ANSWERS (``kill(pid, 0)``), or a
        ``worker_executions`` row in (registered, running). **Observed beats inferred**: a live
        holder wins even when ``owner_epoch`` differs, because a pid that answers cannot be a
        claim left by a dead run, while an epoch is only ever inferred from what the store has
        advanced to. Arriving at this the other way round is how a live turn is killed through the
        epoch arm instead of the age arm;
      * ``orphan`` when ``owner_epoch`` DIFFERS from the store's current one and nothing is
        observed - the run that wrote the claim is dead, whatever the clock says. The reason prints
        both values: a bare inequality is a verdict with no measurement behind it;
      * ``orphan`` when the probe answers ABSENT at the current epoch - claimed, and nothing holds it.

    A lease row whose pid does NOT answer is a LEAK, not evidence: the process is gone, so the
    epoch decides, and the reason says which pid failed to answer. A recycled pid can only ever
    produce a false ``live``, which merely leaves a stuck row stuck - the direction a retirement
    verb must fail in.

    The epoch compared is returned with the verdict, so a reader can see which rung fired.
    """
    now = time.time() if now is None else now
    with db._read_ctx() as conn:
        current = _current_epoch(conn)
        holder = _lease_holder(conn, session_id, now=now)
        workers = _live_workers(conn, session_id)
    holder_pid = _holder_pid((holder or {}).get('holder'))
    holder_alive = holder is not None and _pid_alive(holder_pid)
    verdict = {'epoch': owner_epoch, 'current_epoch': current,
               'holder': (holder or {}).get('holder'), 'holder_pid': holder_pid,
               'holder_alive': holder_alive, 'workers': workers}
    if holder_alive:
        return dict(verdict, state='live',
                    reason=f'a live turn holds the claim: pid={holder_pid} answers')
    if workers:
        return dict(verdict, state='live',
                    reason=f'a registered worker holds the claim: {",".join(workers)}')
    leak = ''
    if holder is not None:
        leak = (f' (the lease names pid={holder_pid}, which does not answer)'
                if holder_pid is not None else ' (the lease names no pid, so it cannot be probed)')
    if owner_epoch is not None and current is not None and int(owner_epoch) != int(current):
        return dict(verdict, state='orphan',
                    reason=f'owner_epoch differs: row={owner_epoch} store={current}, and nothing '
                           f'observed holds the claim{leak}')
    return dict(verdict, state='orphan',
                reason=f'no unexpired turn lease and no registered worker at the current '
                       f'epoch {current}{leak}')


def _holder_pid(holder) -> Optional[int]:
    """The pid inside a lease ``holder`` (``pid=35864:turn=…:platform=cli``), or None."""
    match = re.search(r'(?:^|:)pid=(\d+)', str(holder or ''))
    return int(match.group(1)) if match else None


def _pid_alive(pid: Optional[int]) -> bool:
    """Does this pid ANSWER on this host? Errs toward ALIVE: a retirement must never take a turn
    that might still be running, and a false ``alive`` only leaves a stuck row stuck."""
    if pid is None:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # it exists; it simply is not ours to signal
    except OSError:
        return True          # cannot tell -> assume the live one
    return True


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


def _observed_since_key(kind: str, admission_id: str) -> str:
    """The clock of ONE hour-glass: its KIND and its ROW.

    These used to be keyed by session alone, shared by ``unknown`` and ``started``, and every path
    that found no row of ITS OWN kind forgot the other kind's clock. A lane whose head alternates
    between the two - or merely has an ``unknown`` row waiting while a ``started`` pass runs -
    therefore reset ``first_seen`` on every pass and NEITHER population ever reached the threshold.
    That is a silent non-repair: the cadence looks busy and nothing ages out.
    """
    return f"{UNKNOWN_SINCE_PREFIX}{kind}:{admission_id}"


def _legacy_since_key(session_id: str) -> str:
    """The pre-split key, read-only: adopted on first sight so a live clock is not restarted."""
    return UNKNOWN_SINCE_PREFIX + session_id


def _forget_observation(db, kind: str, admission_id: str) -> None:
    """Forget ONLY the clock this path set. Never another kind's, never another row's."""
    db._execute_write(lambda conn: conn.execute(
        'DELETE FROM state_meta WHERE key=?', (_observed_since_key(kind, admission_id),)))


def _observe_since(db, *, kind: str, session_id: str, row: dict, now: float) -> float:
    """Stamp (or re-read) this row's first observation, and return the instant FIRST seen."""
    key = _observed_since_key(kind, row['admission_id'])
    legacy = _legacy_since_key(session_id)

    def stamp(conn):
        saved = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
        for candidate in ([saved] if saved is not None else []) + [
                conn.execute('SELECT value FROM state_meta WHERE key=?', (legacy,)).fetchone()]:
            if candidate is None:
                continue
            try:
                last = json.loads(candidate[0])
            except ValueError:
                continue
            if last.get('admission_id') == row['admission_id']:
                first = float(last.get('first_seen') or now)
                if candidate is not saved:      # adopted: keep the instant, move it to the new key
                    conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?) ON CONFLICT(key) '
                                 'DO UPDATE SET value=excluded.value',
                                 (key, json.dumps({'admission_id': row['admission_id'],
                                                   'first_seen': first})))
                return first
        conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE '
                     'SET value=excluded.value',
                     (key, json.dumps({'admission_id': row['admission_id'], 'first_seen': now})))
        return float(now)

    return float(db._execute_write(stamp))


def _observed_since_iso(now: float, first_seen: float) -> str:
    """The instant this module first SAW the row - the origin of the number, printed with it."""
    return datetime.datetime.fromtimestamp(first_seen, datetime.timezone.utc).strftime(
        '%Y-%m-%dT%H:%M:%SZ')


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
        # NOTHING TO FORGET: an empty pass of THIS kind says nothing about the other kind's clock.
        # Forgetting here is what made two populations share one hour-glass and neither fill.
        return None
    row = dict(row)

    first_seen = _observe_since(db, kind='unknown', session_id=session_id, row=row, now=now)
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
    mark_epoch_quarantined(db, session_id, moved.get('owner_epoch'), by='quarantine_unknown',
                           row_id=moved['admission_id'])
    _forget_observation(db, 'unknown', moved['admission_id'])
    out(f"quarantine: {QUARANTINE_OUTCOME_PREFIX}{moved['seq']} lane={session_id} "
        f"observed_age={moved['age_seconds']:.0f}s since={_observed_since_iso(now, first_seen)} "
        f"unit=seconds threshold={threshold_s:.0f}s "
        f"store={store} admission={moved['admission_id']}")
    return moved


SKIPPED_QUEUED_PREFIX = "skipped_queued:"


def quarantine_skipped(db, session_id: str, *, threshold_s: float = QUARANTINE_AGE_S,
                       store: str = '', now: Optional[float] = None,
                       out: Callable[[str], Any] = print) -> Optional[dict]:
    """MOVE a SKIPPED queued row into the same named quarantine - the disease that does not
    announce itself (platform-stl, t-76c210202a item 2).

    ``quarantine_unknown`` catches the rows that say they are broken. A skipped row says nothing: it
    is ``status='queued'``, it sits BELOW the highest terminal seq, and a predicate derived from the
    frontier cannot see it at all - so a drain built that way reports an empty queue while
    deliverable messages sit in it (32 of them on the store that produced the ruling). It is the
    same disease with a different status and it needs the same entry, or only the loud half of the
    population is ever repaired.

    TWO GUARDS, because these rows are DELIVERABLE MESSAGES and quarantine is the last resort:

      * the lane must be UNBLOCKED - while a ``started``/``unknown`` row exists, a queued row below
        the frontier is legitimately waiting behind a claim, not skipped, and expiring it would
        quarantine the queue itself;
      * the caller runs the WAKE first, so a row that a pass has just claimed is already delivered
        and never reaches here. What expires is a row that stayed queued THROUGH a pass that had
        every opportunity to take it - which is the only condition under which "skipped" is a fact
        about the drain rather than about the lane's traffic.

    The copy is verbatim with ``status_at_quarantine='queued'``; the live row becomes ``terminal``
    with outcome ``skipped_queued:<seq>`` so the FIFO can never loop over it again, and the age is
    measured from this module's own durable observation because ``session_admissions`` carries no
    timestamp at all.
    """
    now = time.time() if now is None else now
    with db._read_ctx() as conn:
        blocker = conn.execute("SELECT status, seq FROM session_admissions WHERE target_session_id=? "
                               "AND status IN ('started','unknown') ORDER BY seq LIMIT 1",
                               (session_id,)).fetchone()
        row = conn.execute(
            "SELECT * FROM session_admissions q WHERE q.target_session_id=? AND q.status='queued' "
            "AND q.seq < (SELECT COALESCE(MAX(t.seq),0) FROM session_admissions t "
            "             WHERE t.target_session_id=? AND t.status='terminal') ORDER BY q.seq LIMIT 1",
            (session_id, session_id)).fetchone()
    if blocker is not None or row is None:
        return None                      # nothing watched this pass: spend nobody else's clock
    row = dict(row)

    first_seen = _observe_since(db, kind='skipped_queued', session_id=session_id, row=row, now=now)
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
                      row['payload_digest'], row['intent'], 'queued', row.get('outcome'),
                      row.get('owner_epoch'), row.get('generation'), now, age, store))
        conn.execute('UPDATE session_admissions SET status=?, outcome=? WHERE admission_id=? '
                     'AND status=?',
                     ('terminal', f"{SKIPPED_QUEUED_PREFIX}{row['seq']}", row['admission_id'],
                      'queued'))
        return conn.execute(f'SELECT * FROM {QUARANTINE_TABLE} WHERE admission_id=?',
                            (row['admission_id'],)).fetchone()

    moved = dict(db._execute_write(move))
    mark_epoch_quarantined(db, session_id, moved.get('owner_epoch'), by='quarantine_skipped',
                           row_id=moved['admission_id'])
    _forget_observation(db, 'skipped_queued', moved['admission_id'])
    out(f"quarantine: {SKIPPED_QUEUED_PREFIX}{moved['seq']} lane={session_id} "
        f"status_at_quarantine=queued below_frontier=yes "
        f"observed_age={moved['age_seconds']:.0f}s since={_observed_since_iso(now, first_seen)} "
        f"unit=seconds threshold={threshold_s:.0f}s "
        f"store={store} admission={moved['admission_id']} principal={row['principal_id']}")
    return moved


def _skipped_since_key(session_id: str) -> str:
    """Retired: kept so a caller of the old name fails loudly rather than silently mis-keyed."""
    raise RuntimeError('_skipped_since_key was keyed by SESSION and shared by every skipped row; '
                       'use _observed_since_key("skipped_queued", admission_id)')


STARTED_ORPHAN_PREFIX = "started_orphaned:"


def quarantine_started(db, session_id: str, *, threshold_s: float = QUARANTINE_AGE_S,
                       store: str = '', now: Optional[float] = None,
                       out: Callable[[str], Any] = print) -> Optional[dict]:
    """R7: an ORPHANED ``started`` head expires into the same named quarantine - and ONLY when its
    owner is DEMONSTRABLY GONE. Age is never the sole rung.

    The counterexample that produced this ruling: a lane whose ``started`` head was the turn being
    run at that moment. An age rule alone would have expired it mid-flight, quarantined a live
    claim, and delivered the queue behind an answer that was still being written - overtaking a live
    turn, which is the one thing the freeze exists to prevent. So:

      * liveness FIRST (:func:`lease_liveness`): a row whose owner is alive is RETURNED UNTOUCHED and
        the pass prints nothing about it;
      * age SECOND, measured from this module's own durable observation (a lane blocked since T has
        been blocked for now - T, which is a fact of the store rather than a guess);
      * the copy is verbatim into the same quarantine table with ``status_at_quarantine='started'``,
        the live row becomes ``terminal`` with ``started_orphaned:<seq>``, and the line names the
        epoch it compared, so a reader can see which rung fired.
    """
    now = time.time() if now is None else now
    with db._read_ctx() as conn:
        row = conn.execute("SELECT * FROM session_admissions WHERE target_session_id=? AND "
                           "status='started' ORDER BY seq LIMIT 1", (session_id,)).fetchone()
    if row is None:
        return None                      # an empty pass does not spend another kind's clock
    row = dict(row)
    liveness = lease_liveness(db, session_id, owner_epoch=row.get('owner_epoch'), now=now)
    if liveness['state'] != 'orphan':
        # A live turn is a TURN, not a wedge. Never overtaken, and never even stamped as stranded.
        return None

    first_seen = _observe_since(db, kind='started', session_id=session_id, row=row, now=now)
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
                     ('terminal', f"{STARTED_ORPHAN_PREFIX}{row['seq']}",
                      row['admission_id'], 'started'))
        return conn.execute(f'SELECT * FROM {QUARANTINE_TABLE} WHERE admission_id=?',
                            (row['admission_id'],)).fetchone()

    moved = dict(db._execute_write(move))
    epoch = mark_epoch_quarantined(db, session_id, moved.get('owner_epoch'),
                                   by='quarantine_started', row_id=moved['admission_id'])
    _forget_observation(db, 'started', moved['admission_id'])
    out(f"quarantine: {STARTED_ORPHAN_PREFIX}{moved['seq']} lane={session_id} "
        f"observed_age={moved['age_seconds']:.0f}s since={_observed_since_iso(now, first_seen)} "
        f"unit=seconds threshold={threshold_s:.0f}s "
        f"epoch={liveness['epoch']} current_epoch={liveness['current_epoch']} "
        f"liveness='{liveness['reason']}' "
        f"principal={moved.get('principal_id')} epoch_rows={epoch['rows']} "
        f"store={store} admission={moved['admission_id']}")
    return moved


QUARANTINE_EPOCH_TABLE = 'session_quarantine_epochs'


def _ensure_quarantine_epoch_table(conn) -> None:
    conn.execute(f'CREATE TABLE IF NOT EXISTS {QUARANTINE_EPOCH_TABLE} ('
                 'session_id TEXT NOT NULL, owner_epoch INTEGER NOT NULL, '
                 'first_row TEXT, rows INTEGER NOT NULL DEFAULT 0, marked_at REAL NOT NULL, '
                 'marked_by TEXT NOT NULL, PRIMARY KEY (session_id, owner_epoch))')


def mark_epoch_quarantined(db, session_id: str, owner_epoch, *, by: str,
                           row_id: str = '') -> dict:
    """Record that a WHOLE EPOCH is quarantined, which is the unit eligibility is decided in.

    Eligibility per ROW is what lets a quarantined row come back: the row returns to the queue (a
    requeued steer lands at its own seq), is claimed, fails the same way, and is quarantined again -
    an emission per pass, for ever, from a defect nothing is learning from. The epoch is the run that
    wrote these claims; when that run is gone every claim it left is suspect, so one record per
    (session, epoch) makes the thousandth row a COUNT rather than a new alarm.
    """
    def write(conn):
        _ensure_quarantine_epoch_table(conn)
        conn.execute(f'INSERT INTO {QUARANTINE_EPOCH_TABLE} '
                     '(session_id, owner_epoch, first_row, rows, marked_at, marked_by) '
                     'VALUES (?,?,?,1,?,?) ON CONFLICT(session_id, owner_epoch) DO UPDATE SET '
                     'rows=rows+1, first_row=COALESCE(first_row, excluded.first_row)',
                     (session_id, int(owner_epoch), row_id or None, time.time(), by))
        return dict(conn.execute(f'SELECT * FROM {QUARANTINE_EPOCH_TABLE} WHERE session_id=? '
                                 'AND owner_epoch=?', (session_id, int(owner_epoch))).fetchone())
    return db._execute_write(write)


def epoch_quarantined(db, session_id: str, owner_epoch):
    """The existing record for this epoch, or None. Read BEFORE acting, so a pass can be silent."""
    if owner_epoch is None:
        return None
    with db._read_ctx() as conn:
        try:
            row = conn.execute(f'SELECT * FROM {QUARANTINE_EPOCH_TABLE} WHERE session_id=? AND '
                               'owner_epoch=?', (session_id, int(owner_epoch))).fetchone()
        except Exception:
            return None
    return dict(row) if row is not None else None


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

    A ``started(live)`` head is the one case that gets NO wake and NO quarantine: the turn is
    running, it will end, and ``wake_after_worker`` schedules the drain behind it. Falling through
    to ``wake`` here would make the cadence the siren it exists to silence - waking every busy lane
    on every floor into a drain that must refuse it, forever.
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
                # THE FRONTIER, ON EVERY PASS (platform-stl, t-76c210202a). One line with the
                # oldest queued seq, its epoch against the live epoch, how long it has been the
                # oldest, and the started row if any: "queue empty" and "queue ignored" print
                # differently here, which a count alone cannot do. A symptom described once and a
                # frontier printed every pass are not the same instrument.
                out(_frontier_line(db, session_id, now=None if now is None else now()))
                if not census['queued'] and not census['blocked']:
                    continue
                pass_now = None if now is None else now()
                blocked = str(census['blocked'] or '')
                moved = False
                if blocked.startswith('unknown@'):
                    moved = quarantine_unknown(db, session_id, store=_store_label(db),
                                               now=pass_now, out=out) is not None
                elif blocked.startswith('started(orphan)@'):
                    # R7: liveness FIRST, age SECOND - quarantine_started refuses a live owner.
                    moved = quarantine_started(db, session_id, store=_store_label(db),
                                               now=pass_now, out=out) is not None
                elif blocked.startswith('started(live)@'):
                    # A LIVE TURN NEEDS NO WAKE: it ends, and `wake_after_worker` schedules the
                    # drain that follows it. Waking here is the siren this loop was written to
                    # silence - every floor, every busy lane, into a drain that must refuse it.
                    continue
                if moved:
                    census = _lane_census(db, session_id, now=pass_now)   # the head MOVED
                    if not census['queued'] and not census['blocked']:
                        continue
                wake(session_id)
                # THE SKIPPED ROWS GET THEIR CHANCE HERE AND NOWHERE ELSE: the wake has just had
                # first refusal, so a row still queued below the frontier after it is one a pass
                # could not take, not one that is merely waiting. Until it expires, `below_frontier`
                # on the next line is the number that says the drain is skipping.
                if quarantine_skipped(db, session_id, store=_store_label(db), now=pass_now,
                                      out=out) is not None:
                    out(_frontier_line(db, session_id, now=pass_now))
            except Exception:  # health: allow BLE001 -- one unbounded lane must not stop the rest;
                # the exception is never swallowed silently, it is logged with the lane.
                if log is not None:
                    log(f'admission cadence: wake of {session_id} failed', )
