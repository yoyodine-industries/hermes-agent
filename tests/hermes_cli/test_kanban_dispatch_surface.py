"""The dispatch-surface contract: READY means "will be dispatched", and filing is governed.

Three rules, one promise, and the tests that keep the promise from becoming a number on a
dashboard:

* **Rule 1 — per-assignee ready cap.** A card born ``ready`` on a lane already at its cap is
  born ``todo`` WITH a comment naming the cap; a freed slot promotes exactly the
  highest-ranked (priority desc, created_at asc) waiting card; an over-cap board is brought
  back to its cap by DEMOTING the tail of that rank; and the depth is REPORTED as
  ``ready/cap`` per lane, because an unreadable cap is a silent throttle.
* **Rule 2 — filing dedupe.** The same title (ids/counts/timestamps removed) filed twice is
  ONE card with ``occurrences=2`` and a comment naming the emitter — while a genuinely
  different card with similar wording is NOT refused.
* **Rule 3 — emitter quota.** Past N filings per window, an emitter's filings land on a
  SINGLE rolling card per emitter, occurrence-counted.

The tests drive the real kernel door (``create_task``), not the helpers, because the point
of placing the rules there is that no surface can route around them.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_boards as kbb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_filing_dedupe as kfd
from hermes_cli import kanban_filing_policy as kfp
from hermes_cli import kanban_ready_cap as krc


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A temp HERMES_HOME: its own board, no policy of any kind."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_WORKSPACE", "HERMES_KANBAN_WORKSPACES_ROOT",
                krc.DISABLE_ENV, kfd.DISABLE_ENV):
        monkeypatch.delenv(var, raising=False)
    db_path = kb.kanban_db_path(board="default")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return tmp_path


@pytest.fixture
def conn(home):
    with kbc.connect_closing() as c:
        yield c


def declare(board_json: dict, home) -> None:
    """Write the board's own metadata (the cap/quota are DATA, so the test writes data)."""
    path = kbb.board_metadata_path("default")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(board_json), encoding="utf-8")


def status_of(conn, task_id: str) -> str:
    return conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]


def occurrences(conn, task_id: str) -> int:
    return int(conn.execute(
        "SELECT occurrences FROM tasks WHERE id = ?", (task_id,)).fetchone()[0])


def comments(conn, task_id: str) -> list[str]:
    return [row[0] for row in conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id", (task_id,))]


# --- Rule 1: the cap -------------------------------------------------------------------

def test_three_filings_at_cap_are_todo_with_a_comment_naming_the_cap(conn, home):
    declare({"ready_cap": 1}, home)
    first = kb.create_task(conn, title="occupies the slot", assignee="lane-a",
                           created_by="emitter-a")
    assert status_of(conn, first) == "ready"

    held = [
        kb.create_task(conn, title=f"waiting {word}", assignee="lane-a", created_by="emitter-a")
        for word in ("alpha", "bravo", "charlie")
    ]
    assert held, "the filings must produce cards, not refusals"
    for task_id in held:
        assert status_of(conn, task_id) == "todo", "a card at cap must be born todo"
        note = comments(conn, task_id)
        assert note, "the cap hold must be explained on the card"
        assert "cap" in note[-1] and "1/1" in note[-1], note[-1]
        assert "todo" in note[-1]


def test_freeing_a_slot_promotes_exactly_the_highest_priority_todo(conn, home):
    declare({"ready_cap": 1}, home)
    ready_card = kb.create_task(conn, title="the slot", assignee="lane-b",
                                created_by="emitter-b")
    low = kb.create_task(conn, title="low priority wait", assignee="lane-b",
                         created_by="emitter-b", priority=0)
    high = kb.create_task(conn, title="high priority wait", assignee="lane-b",
                          created_by="emitter-b", priority=9)
    assert status_of(conn, low) == status_of(conn, high) == "todo"

    # Free the slot the way the kernel does, then let the ONE promotion pass run.
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (ready_card,))
    conn.commit()
    kb.recompute_ready(conn)

    assert status_of(conn, high) == "ready", "the highest-ranked waiting card promotes"
    assert status_of(conn, low) == "todo", "the cap is honoured during promotion too"


def test_reconcile_demotes_the_tail_of_the_rank_and_reports_depth(conn, home):
    # Filed with the cap DISABLED: this is the state the live board is in — surplus ready
    # rows that accumulated before any cap existed.
    declare({"ready_cap": 0}, home)
    ids = [
        kb.create_task(conn, title=f"ready card {word}", assignee="lane-c",
                       created_by="emitter-c", priority=n)
        for n, word in enumerate(("alpha", "bravo", "charlie", "delta"))
    ]
    assert krc.lane_depths(conn)["lane-c"] == 4
    # ...then the cap is declared and the tick's reconciliation pass runs.
    declare({"ready_cap": 2}, home)

    result = kb.reconcile_ready_depth(conn, board="default")
    conn.commit()

    assert len(result["demoted"]) == 2
    still_ready = {row[0] for row in conn.execute(
        "SELECT id FROM tasks WHERE status = 'ready'")}
    # The rank head survives: priority 3 and 2 stay ready; 1 and 0 are the demoted tail.
    assert still_ready == {ids[3], ids[2]}

    report = kb.ready_depth_report(conn, board="default")
    line = krc.format_depth_report(report)
    assert "lane-c 2/2" in line
    assert report["total_ready"] == 2


def test_a_disabled_cap_changes_nothing(conn, home, monkeypatch):
    declare({"ready_cap": 0}, home)
    ids = [kb.create_task(conn, title=f"uncapped {word}", assignee="lane-d",
                          created_by="emitter-d")
           for word in ("alpha", "bravo", "charlie")]
    assert all(status_of(conn, task_id) == "ready" for task_id in ids)
    assert krc.cap_for("default") == 0
    assert krc.format_depth_report(krc.depth_report(conn)) == ""


# --- Rule 2: dedupe --------------------------------------------------------------------

def test_same_title_twice_is_one_card_with_two_occurrences(conn, home):
    declare({}, home)  # defaults: dedupe on
    first = kb.create_task(conn, title="Nightly report filed nothing", assignee="lane-e",
                           created_by="dag-runner", body="the tick was dead for 34 hours")
    second = kb.create_task(conn, title="Nightly report filed nothing", assignee="lane-e",
                            created_by="dag-runner")

    assert kfp.is_refusal(second), "a duplicate filing is REFUSED, not turned into a card"
    assert second.record.get("duplicate_of") == first
    assert second.record.get("occurrences") == 2
    rows = conn.execute(
        "SELECT id FROM tasks WHERE title = 'Nightly report filed nothing'").fetchall()
    assert len(rows) == 1, "the duplicate must not birth a second card"
    assert occurrences(conn, first) == 2
    assert any("dag-runner" in body and "occurrence 2" in body
               for body in comments(conn, first)), comments(conn, first)


def test_normalisation_strips_only_ids_counts_and_timestamps(conn, home):
    assert krc and kfd  # module import is part of the surface
    a = kfd.normalise_title("Fix parser crash: 3 failures at 2026-11-01T22:41:00Z (t_abc12345)")
    b = kfd.normalise_title("Fix parser crash: 7 failures at 2026-11-19T01:02:03Z (t_deadbeef)")
    assert a == b
    # ...and a genuinely different card keeps its identity.
    different = kfd.normalise_title("Fix parser crash on an empty body")
    assert different != a


def test_a_different_card_with_a_similar_title_is_not_refused(conn, home):
    declare({}, home)
    one = kb.create_task(conn, title="parser drops the trailing newline", assignee="lane-f",
                         created_by="emitter-f")
    two = kb.create_task(conn, title="parser drops the leading newline", assignee="lane-f",
                         created_by="emitter-f")
    assert one != two
    assert status_of(conn, two) == "ready", "a distinct filing must be a real card"
    assert occurrences(conn, one) == 1


def test_a_closed_card_is_not_a_dedupe_target(conn, home):
    declare({}, home)
    first = kb.create_task(conn, title="retry the deploy", assignee="lane-g",
                           created_by="emitter-g")
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (first,))
    conn.commit()
    again = kb.create_task(conn, title="retry the deploy", assignee="lane-g",
                           created_by="emitter-g")
    assert again != first, "the same work after the first card CLOSED is new work"
    assert status_of(conn, again) == "ready"


# --- Rule 3: emitter quota -------------------------------------------------------------

def test_emitter_past_quota_rolls_up_into_one_card(conn, home):
    declare({"emitter_quota": {"max": 2, "window_hours": 24}}, home)
    born = [
        kb.create_task(conn, title=f"flood {word}", assignee="lane-h", created_by="broken-dag")
        for word in ("alpha", "bravo")
    ]
    third = kb.create_task(conn, title="flood charlie", assignee="lane-h",
                           created_by="broken-dag")
    fourth = kb.create_task(conn, title="flood delta", assignee="lane-h",
                            created_by="broken-dag")

    fifth = kb.create_task(conn, title="flood echo", assignee="lane-h",
                           created_by="broken-dag")
    assert fifth == third, "and it keeps absorbing"
    assert third == fourth, "past the quota every filing lands on the SAME rolling card"
    assert third not in born, "and the landing card is not one of the quota's own cards"
    assert occurrences(conn, third) == 3  # the filing that opened it + two absorbed
    rollup_title = conn.execute(
        "SELECT title FROM tasks WHERE id = ?", (third,)).fetchone()[0]
    assert "emitter quota" in rollup_title
    assert "broken-dag" in rollup_title


def test_quota_does_not_touch_a_different_emitter(conn, home):
    declare({"emitter_quota": {"max": 1, "window_hours": 24}}, home)
    kb.create_task(conn, title="a one", assignee="lane-i", created_by="emitter-i1")
    other = kb.create_task(conn, title="b one", assignee="lane-i", created_by="emitter-i2")
    assert status_of(conn, other) == "ready", "one emitter's quota must not gag another"
