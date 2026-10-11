"""Time-gated cards: the ``due_at`` clock fence and the surfaces that arm a card.

The contract these lock down: ``hermes kanban schedule <id> --due <when>`` (and
``hermes kanban block <id> --due <when>``, and the board API behind both) parks a
card with a due time. Omitting ``--due`` is the HUMAN HOLD -- a card waiting on a
person, not on a clock -- and no verb may turn one of those into the other.

What is NOT here, deliberately: no waker, and no dispatcher-tick release. The
RELEASE leg for a fenced card is the deployed prod ``maintenance-sweep``
workflow's ``scheduled_wake`` node, and two releasers for one cohort is the
two-implementations defect. A card whose fence passed and which was never
released is surfaced by the ``overdue_scheduled`` diagnostic instead.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_diagnostics as kd
from hermes_cli import kanban_due as kdue


@pytest.fixture(autouse=True)
def _writable_board(monkeypatch):
    """A dispatched worker inherits the delegated-child marker, which opens the
    board read-only. These tests write to it."""
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    c = kbc.connect()
    yield c
    c.close()


def _task(conn, title="card") -> str:
    return kb.create_task(conn, title=title, assignee="smoke")


def _get(conn, tid) -> kb.Task:
    task = kb.get_task(conn, tid)
    assert task is not None, tid
    return task



# ---------------------------------------------------------------------------
# Arming: schedule_task
# ---------------------------------------------------------------------------


def test_schedule_with_due_at_parks_the_card_and_records_the_time(conn):
    tid = _task(conn)
    due = int(time.time()) + 600

    assert kb.schedule_task(conn, tid, reason="wait for the band", due_at=due) is True

    task = _get(conn, tid)
    assert task.status == "scheduled"
    assert task.due_at == due
    assert task.due_window_policy == "defer"
    event = [e for e in kb.list_events(conn, tid) if e.kind == "scheduled"][-1]
    assert event.payload == {"reason": "wait for the band", "due_at": due,
                             "window_policy": "defer"}


def test_schedule_without_due_at_keeps_an_existing_due_time(conn):
    """``due_at`` omitted (kb.UNSET) must not silently disarm a self-waking card."""
    tid = _task(conn)
    due = int(time.time()) + 600
    kb.schedule_task(conn, tid, reason="armed", due_at=due)

    kb.schedule_task(conn, tid, reason="re-parked with a new note")

    assert _get(conn, tid).due_at == due


def test_schedule_due_none_clears_the_wake_time(conn):
    tid = _task(conn)
    kb.schedule_task(conn, tid, reason="armed", due_at=int(time.time()) + 600)

    kb.schedule_task(conn, tid, reason="park by hand", due_at=None)

    task = _get(conn, tid)
    assert task.status == "scheduled"
    assert task.due_at is None and task.due_window_policy is None


def test_window_policy_needs_a_due_time(conn):
    """A band policy with nothing to apply it to is dead state: refuse it."""
    tid = _task(conn)
    with pytest.raises(ValueError, match="window_policy"):
        kb.schedule_task(conn, tid, reason="policy only", window_policy="ambient")


def test_window_policy_can_be_re_polished_on_an_armed_card(conn):
    tid = _task(conn)
    kb.schedule_task(conn, tid, reason="armed", due_at=int(time.time()) + 600)

    assert kb.schedule_task(conn, tid, reason="tick around the clock",
                            window_policy="ambient") is True

    task = _get(conn, tid)
    assert task.due_window_policy == "ambient"


def test_unknown_window_policy_is_refused(conn):
    with pytest.raises(ValueError, match="window_policy"):
        kb.schedule_task(conn, _task(conn), reason="typo", due_at=1,
                         window_policy="deferr")


def test_schedule_unknown_card_reports_failure(conn):
    assert kb.schedule_task(conn, "t_missing", reason="x") is False


# ---------------------------------------------------------------------------
# A board that cannot hold a fence
#
# The fence columns (``due_at`` / ``due_window_policy``) are the dispatcher's
# time-fenced-block half. A store built without that half (the 2026-09-27
# ops/defcon rebuilds) has no such column, which is the state the deployed
# release leg's own ``task_columns`` probe exists to detect. On a connection
# that did NOT go through ``kanban_db_connect.connect()`` -- a raw driver
# handle, so nothing migrates the columns back in -- the write must either
# refuse BY NAME or work without the column; it must never surface the bare
# ``OperationalError: no such column: due_at``.
# ---------------------------------------------------------------------------


def _columnless_board(conn):
    """Rebuild the fixture's board into one whose ``tasks`` cannot hold a fence,
    and return a RAW driver handle on it.

    The handle is a bare ``sqlite3.connect`` -- NEVER ``kbc.connect()``, whose
    ``init_db`` migrates the fence columns straight back in. That raw handle on a
    board without the fence columns is the connection class the defect was
    measured on.
    """
    db_path = Path(next(
        r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"
    ))
    conn.execute("DROP INDEX IF EXISTS idx_tasks_due")
    conn.execute("ALTER TABLE tasks DROP COLUMN due_at")
    conn.execute("ALTER TABLE tasks DROP COLUMN due_window_policy")
    raw = sqlite3.connect(str(db_path), isolation_level=None)
    raw.row_factory = sqlite3.Row
    raw.text_factory = kb._lossy_text
    raw.execute("PRAGMA foreign_keys=ON")
    return raw


def test_fence_is_refused_by_name_where_the_board_cannot_hold_one(conn):
    """A fence the board cannot carry is REFUSED, naming cause and remedy.

    Pre-fix, on exactly this handle, both doors raised the driver's bare
    ``OperationalError: no such column: due_at``; measured here it is a
    ``ValueError`` that names the column, the migration and the way out.
    """
    tid = _task(conn)
    raw = _columnless_board(conn)
    try:
        assert "due_at" not in {row[1] for row in raw.execute("PRAGMA table_info(tasks)")}
        before = raw.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()["status"]
        due = int(time.time()) + 600

        for door, call in (
            ("schedule_task",
             lambda: kb.schedule_task(raw, tid, reason="armed", due_at=due)),
            ("block_task",
             lambda: kb.block_task(raw, tid, reason="held", kind="transient", due_at=due)),
        ):
            with pytest.raises(ValueError) as excinfo:
                call()
            message = str(excinfo.value)
            assert "cannot hold a clock fence" in message, (door, message)
            assert "no due_at column on tasks" in message, (door, message)
            assert "un-migrated" in message, (door, message)
            assert "kanban_db_connect.connect()" in message, (door, message)
            assert "park without --due" in message, (door, message)

        # A refusal writes nothing: the card is exactly where it was.
        assert raw.execute(
            "SELECT status FROM tasks WHERE id = ?", (tid,)
        ).fetchone()["status"] == before
    finally:
        raw.close()


def test_fence_free_park_works_where_the_board_cannot_hold_a_fence(conn):
    """A park that asks for no clock needs no fence column, and must not name one.

    Pre-fix BOTH doors died on the fence column inside their probe row even with
    ``due_at`` omitted (`OperationalError: no such column: due_at`), so the human
    hold -- the state that is most often the right one -- was unreachable on a
    board without the fence half.
    """
    parked = _task(conn, title="park by hand")
    held = _task(conn, title="block by hand")
    raw = _columnless_board(conn)
    try:
        assert "due_at" not in {row[1] for row in raw.execute("PRAGMA table_info(tasks)")}

        assert kb.schedule_task(raw, parked, reason="waiting on rob") is True
        assert raw.execute(
            "SELECT status FROM tasks WHERE id = ?", (parked,)
        ).fetchone()["status"] == "scheduled"
        event = [e for e in kb.list_events(raw, parked) if e.kind == "scheduled"][-1]
        assert event.payload == {"reason": "waiting on rob"}
        assert "due_at" not in event.payload

        assert kb.block_task(raw, held, reason="held by hand") is True
        assert raw.execute(
            "SELECT status FROM tasks WHERE id = ?", (held,)
        ).fetchone()["status"] == "blocked"
    finally:
        raw.close()


# ---------------------------------------------------------------------------
# Diagnostics: a card that should have woken and did not
# ---------------------------------------------------------------------------


def _overdue_diags(conn, tid, **cfg):
    task = _get(conn, tid)
    return [d for d in kd.compute_task_diagnostics(
        task, kb.list_events(conn, tid), kb.list_runs(conn, tid), config=cfg)
        if d.kind == "overdue_scheduled"]


def test_overdue_scheduled_card_is_reported(conn):
    tid = _task(conn)
    kb.schedule_task(conn, tid, reason="armed", due_at=int(time.time()) - 7200)

    diags = _overdue_diags(conn, tid, due_waker_last_tick=int(time.time()))

    assert len(diags) == 1
    assert diags[0].severity == "warning"
    assert "2h0m" in diags[0].title
    assert diags[0].actions and "unblock" in diags[0].actions[0].payload["command"]


def test_overdue_diagnostic_names_a_dead_dispatcher(conn):
    """The detail names the ONE releaser, and asserts nothing about a tick.

    The rule used to switch on a ``due_waker_last_tick`` heartbeat, but nothing
    in this tree -- and nothing in the deployed releaser -- ever wrote that key,
    so that branch could only ever emit a cause it could not read. It is retired
    (t_f3e89dbc): the detail names the dispatcher's non-role and the deployed
    ``maintenance-sweep`` ``scheduled_wake`` node, and claims no tick either way.
    """
    tid = _task(conn)
    kb.schedule_task(conn, tid, reason="armed", due_at=int(time.time()) - 7200)

    diags = _overdue_diags(conn, tid)

    assert len(diags) == 1
    assert "dispatcher" in diags[0].detail
    assert "scheduled_wake" in diags[0].detail
    assert "tick" not in diags[0].detail.lower()
    assert "due_waker_last_tick" not in diags[0].data


def test_overdue_diagnostic_escalates_when_long_stuck(conn):
    tid = _task(conn)
    kb.schedule_task(conn, tid, reason="armed", due_at=int(time.time()) - 30 * 3600)

    diags = _overdue_diags(conn, tid)

    assert diags and diags[0].severity == "error"


def test_recently_due_card_is_not_noisy(conn):
    """The wake runs on the tick: up to one interval late is normal."""
    tid = _task(conn)
    kb.schedule_task(conn, tid, reason="armed", due_at=int(time.time()) - 5)

    assert _overdue_diags(conn, tid) == []



def test_scheduled_card_without_a_due_time_is_not_reported(conn):
    """Parking on a human is a first-class state, not a fault."""
    tid = _task(conn)
    kb.schedule_task(conn, tid, reason="waiting on rob")

    assert _overdue_diags(conn, tid) == []


def test_overdue_kind_is_published_by_the_public_api(conn):
    """The surfaces match on ``kind``, so the parked-past-due card must NAME it.

    Driven through ``compute_task_diagnostics`` rather than read off a kinds
    list: the old ``DIAGNOSTIC_KINDS`` name now exists only in a plugin-compat
    block (revert-scheduled), and nothing in-tree may reach it.
    """
    tid = _task(conn)
    kb.schedule_task(conn, tid, reason="armed", due_at=int(time.time()) - 7200)

    diags = kd.compute_task_diagnostics(
        _get(conn, tid), kb.list_events(conn, tid), kb.list_runs(conn, tid),
        config={"due_waker_last_tick": int(time.time())})

    assert "overdue_scheduled" in {d.kind for d in diags}
    assert kd.DEFAULT_CONFIG["due_grace_seconds"] > 0
    assert kd.DEFAULT_CONFIG["due_stale_seconds"] > kd.DEFAULT_CONFIG["due_grace_seconds"]


# ---------------------------------------------------------------------------
# parse_due / CLI / show
# ---------------------------------------------------------------------------


def test_parse_due_accepts_iso_relative_and_epoch():
    now = 1_800_000_000
    assert kdue.parse_due("+30m", now=now) == now + 1800
    assert kdue.parse_due("+2h", now=now) == now + 7200
    assert kdue.parse_due("+1d", now=now) == now + 86400
    assert kdue.parse_due("+45s", now=now) == now + 45
    assert kdue.parse_due(str(now)) == now
    assert kdue.parse_due("2026-09-16T01:40") % 60 == 0
    assert kdue.parse_due("2026-09-16T01:40") == kdue.parse_due("2026-09-16 01:40")


def test_parse_due_rejects_nonsense():
    with pytest.raises(ValueError):
        kdue.parse_due("tomorrow")


def test_cli_schedule_due_arms_the_card(conn):
    tid = _task(conn)

    out = kc.run_slash(f"schedule {tid} boarding the window --due +2h")

    assert "due" in out.lower(), out
    task = _get(conn, tid)
    assert task.status == "scheduled"
    assert task.due_window_policy == "defer"
    assert task.due_at is not None and task.due_at > time.time()


def test_cli_schedule_due_and_clear_due_are_exclusive(conn):
    tid = _task(conn)

    out = kc.run_slash(f"schedule {tid} --due +2h --clear-due")

    assert "mutually exclusive" in out
    assert _get(conn, tid).status != "scheduled"


def test_cli_schedule_rejects_an_unparseable_due_time(conn):
    tid = _task(conn)

    out = kc.run_slash(f"schedule {tid} --due tomorrow")

    assert "due time" in out
    assert _get(conn, tid).status != "scheduled"


def test_cli_schedule_clear_due_disarms(conn):
    tid = _task(conn)
    kb.schedule_task(conn, tid, reason="armed", due_at=int(time.time()) + 600)

    kc.run_slash(f"schedule {tid} parked by hand --clear-due")

    task = _get(conn, tid)
    assert task.due_at is None and task.status == "scheduled"


def test_cli_schedule_window_policy_needs_a_due_time(conn):
    tid = _task(conn)

    out = kc.run_slash(f"schedule {tid} no due given --window-policy ambient")

    assert "due" in out.lower()
    assert _get(conn, tid).due_at is None


def test_cli_show_renders_the_due_time(conn):
    tid = _task(conn)
    kb.schedule_task(conn, tid, reason="armed", due_at=int(time.time()) + 3600)

    out = kc.run_slash(f"show {tid}")

    assert "due" in out.lower(), out


def test_cli_show_flags_a_parked_card_with_no_wake_time(conn):
    """A scheduled card with no due time wakes only by hand -- say so."""
    tid = _task(conn)
    kb.schedule_task(conn, tid, reason="waiting on rob")

    out = kc.run_slash(f"show {tid}")

    assert "no due time" in out.lower(), out


# ---------------------------------------------------------------------------
# block --due: a time-fenced hold that auto-releases
# ---------------------------------------------------------------------------


def test_block_with_due_at_parks_the_card_and_records_the_time(conn):
    tid = _task(conn)
    due = int(time.time()) + 600

    assert kb.block_task(conn, tid, reason="hold until the window", kind="transient",
                         due_at=due) is True

    task = _get(conn, tid)
    assert task.status == "blocked"
    assert task.due_at == due
    assert task.due_window_policy == "defer"
    event = [e for e in kb.list_events(conn, tid) if e.kind == "blocked"][-1]
    assert event.payload["due_at"] == due
    assert event.payload["window_policy"] == "defer"


def test_block_dependency_with_due_is_refused(conn):
    with pytest.raises(ValueError, match="dependency"):
        kb.block_task(conn, _task(conn), reason="wait", kind="dependency",
                      due_at=int(time.time()) + 600)


def test_block_window_policy_needs_a_due_time(conn):
    with pytest.raises(ValueError, match="window_policy"):
        kb.block_task(conn, _task(conn), reason="policy only", kind="transient",
                      window_policy="ambient")



def test_cli_block_due_arms_the_card(conn):
    tid = _task(conn)

    out = kc.run_slash(f"block {tid} hold until the window --due +2h")

    assert "auto-release" in out.lower(), out
    task = _get(conn, tid)
    assert task.status == "blocked"
    assert task.due_window_policy == "defer"
    assert task.due_at is not None and task.due_at > time.time()


def test_cli_block_dependency_rejects_due(conn):
    tid = _task(conn)

    out = kc.run_slash(f"block {tid} waiting on a parent --kind dependency --due +2h")

    assert "dependency" in out.lower()
    assert _get(conn, tid).status != "blocked"
