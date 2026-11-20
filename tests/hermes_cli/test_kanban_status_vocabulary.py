"""``tasks.status`` is ONE vocabulary, and the board refuses anything else.

Measured origin (financially board, card t_58e17240, 2026-11-07): a row whose
status read ``in_progress`` - a value from a SIBLING vocabulary
(``tools/todo_tool.py``, the SDLC stages) - with no ``status`` event on the
ledger, an ISO-8601 string in the INTEGER ``started_at`` column, and no claim,
run or heartbeat. Every verb that switches on status was blind to it, so five
refusals in a row could not move it.

These tests pin the three halves of the fix:

1. WRITE: every write path refuses a value outside ``DEFAULT_STATUSES`` with a
   message naming the value and the legal set - the kernel seam, the create
   seam, the list filter, the dashboard's direct writer, and (the class that
   actually produced the row) a raw sqlite3 write that never passes a seam.
2. TYPE: an epoch column refuses a non-integer value at the same boundary.
3. READ + REPAIR: unmodelled rows render as unmodelled (never in a live column),
   the census reports them, and ``normalise`` writes the status and its audit
   event in one transaction.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB and no live-board pointers."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "platform-coder")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(var, raising=False)
    from hermes_cli import kanban_db as kb

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _new_card(kb, kbc, title="a card") -> str:
    with kbc.connect() as conn:
        task = kb.create_task(
            conn, title=title, assignee="platform-coder", created_by="platform-coder",
        )
        # create_task returns the row on some paths and its id on others
        return task.id if hasattr(task, "id") else str(task)


def _plant_unmodelled(kb, kbc, task_id: str, status: str = "in_progress",
                      started_at=None) -> None:
    """Write the historical defect the way it was written: past every seam.

    The triggers are dropped for the write and re-armed afterwards, exactly as
    a pre-fix process (or a raw sqlite3 writer) reached the row.
    """
    with kbc.connect() as conn:
        conn.execute("DROP TRIGGER IF EXISTS tasks_status_vocabulary_update")
        conn.execute("DROP TRIGGER IF EXISTS tasks_epoch_type_update")
        conn.execute(
            "UPDATE tasks SET status = ?, started_at = ? WHERE id = ?",
            (status, started_at, task_id),
        )
        conn.commit()
        kbc._ensure_write_guards(conn)


# ---------------------------------------------------------------------------
# The vocabulary itself
# ---------------------------------------------------------------------------

def test_validate_status_names_the_value_and_the_legal_set() -> None:
    from hermes_cli import kanban_workflow as wf

    with pytest.raises(wf.InvalidStatus) as excinfo:
        wf.validate_status("in_progress", where="kanban create")
    message = str(excinfo.value)
    assert "'in_progress'" in message
    assert "ready" in message and "blocked" in message and "archived" in message
    assert isinstance(excinfo.value, ValueError)
    # A modelled status is returned unchanged (the seam is a filter, not a mapper).
    assert wf.validate_status("review") == "review"


def test_render_status_marks_an_unmodelled_value_as_unmodelled() -> None:
    from hermes_cli import kanban_workflow as wf

    assert wf.render_status("blocked") == "blocked"
    rendered = wf.render_status("in_progress")
    assert rendered.startswith(wf.UNMODELLED)
    assert "'in_progress'" in rendered
    assert wf.is_modelled("blocked") and not wf.is_modelled("in_progress")


# ---------------------------------------------------------------------------
# 1. Every write path refuses
# ---------------------------------------------------------------------------

def test_create_seam_refuses_an_unmodelled_initial_status(kanban_home: Path) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        with pytest.raises(kb.InvalidStatus) as excinfo:
            kb.create_task(conn, title="carries todo vocabulary",
                           created_by="platform-coder", initial_status="in_progress")
        assert "'in_progress'" in str(excinfo.value)
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE title = 'carries todo vocabulary'"
        ).fetchone()[0] == 0


def test_list_filter_refuses_an_unmodelled_status(kanban_home: Path) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        with pytest.raises(kb.InvalidStatus) as excinfo:
            kb.list_tasks(conn, status="in_progress")
        assert "'in_progress'" in str(excinfo.value)


def _dashboard_api():
    """Import the dashboard plugin without letting pydantic's entry-point scan
    touch the real hermes home (the test harness guards that path)."""
    import pydantic.plugin._loader as _loader

    _loader.get_plugins = lambda: {}
    from plugins.kanban.dashboard import plugin_api

    return plugin_api


def test_dashboard_direct_writer_refuses(kanban_home: Path) -> None:
    plugin_api = _dashboard_api()

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    tid = _new_card(kb, kbc)
    with kbc.connect() as conn:
        with pytest.raises(kb.InvalidStatus):
            plugin_api._set_status_direct(conn, tid, "in_progress")
        assert kb.get_task(conn, tid).status != "in_progress"


def test_raw_insert_of_an_unmodelled_status_is_refused(kanban_home: Path) -> None:
    """The class that produced the origin row: a writer that passes no seam."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError) as excinfo:
            conn.execute(
                "INSERT INTO tasks (id, title, status, priority, created_by, created_at) "
                "VALUES ('t_raw_insert', 'bypass', 'in_progress', 0, 'x', 1)"
            )
        assert "workflow vocabulary" in str(excinfo.value)


def test_raw_update_to_an_unmodelled_status_is_refused(kanban_home: Path) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    tid = _new_card(kb, kbc)
    with kbc.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError) as excinfo:
            conn.execute("UPDATE tasks SET status = 'in_progress' WHERE id = ?", (tid,))
        assert "workflow vocabulary" in str(excinfo.value)
        assert kb.get_task(conn, tid).status == "ready"


def test_raw_null_status_is_refused(kanban_home: Path) -> None:
    """``NULL NOT IN (...)`` is NULL, not false: a bare NOT IN let NULL through."""
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError) as excinfo:
            conn.execute(
                "INSERT INTO tasks (id, title, status, priority, created_by, created_at) "
                "VALUES ('t_null_status', 'null', NULL, 0, 'x', 1)"
            )
        assert "workflow vocabulary" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 2. Epoch columns refuse the wrong storage type
# ---------------------------------------------------------------------------

def test_raw_float_epoch_seconds_are_accepted(kanban_home: Path) -> None:
    """Epoch seconds may be REAL (``time.time()`` is a float); TEXT is the defect."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, created_by, created_at, "
            "started_at) VALUES ('t_float', 'float', 'ready', 0, 'x', 1, 1791332147.5)"
        )
        assert conn.execute(
            "SELECT typeof(started_at) FROM tasks WHERE id = 't_float'").fetchone()[0] == "real"
        assert kb.status_vocabulary_census(conn)["type_defects"] == []

def test_raw_iso_started_at_insert_is_refused(kanban_home: Path) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError) as excinfo:
            conn.execute(
                "INSERT INTO tasks (id, title, status, priority, created_by, created_at, "
                "started_at) VALUES ('t_iso', 'iso', 'ready', 0, 'x', 1, "
                "'2026-11-07T07:43:01-05:00')"
            )
        assert "numeric epoch seconds" in str(excinfo.value)


def test_raw_iso_started_at_update_is_refused(kanban_home: Path) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    tid = _new_card(kb, kbc)
    with kbc.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE tasks SET started_at = '2026-11-07T07:43:01-05:00' WHERE id = ?",
                (tid,),
            )


# ---------------------------------------------------------------------------
# 3. Census + render: unmodelled is unmodelled, never a live column
# ---------------------------------------------------------------------------

def test_census_reports_the_unmodelled_row_and_the_type_defect(kanban_home: Path) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    tid = _new_card(kb, kbc)
    _plant_unmodelled(kb, kbc, tid, started_at="2026-11-07T07:43:01-05:00")
    with kbc.connect() as conn:
        census = kb.status_vocabulary_census(conn)
    assert census["ok"] is False
    assert {"id": tid, "status": "in_progress"} in census["unmodelled"]
    assert any(d["id"] == tid and d["column"] == "started_at"
               for d in census["type_defects"])


def test_list_and_show_render_the_value_as_unmodelled(kanban_home: Path) -> None:
    from hermes_cli import kanban as cli
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    tid = _new_card(kb, kbc, title="stranded row")
    _plant_unmodelled(kb, kbc, tid)
    listed = cli.run_slash("list")
    assert "unmodelled 'in_progress'" in listed
    assert f"? {tid}" not in listed  # the old unknown-status marker must be gone
    shown = cli.run_slash(f"show {tid}")
    assert "unmodelled 'in_progress'" in shown


def test_dashboard_never_buckets_the_row_into_a_live_column(kanban_home: Path) -> None:
    plugin_api = _dashboard_api()

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    tid = _new_card(kb, kbc, title="dashboard defect")
    _plant_unmodelled(kb, kbc, tid)
    payload = plugin_api.get_board(tenant=None, include_archived=False, board=None,
                                  workflow_template_id=None, current_step_key=None)
    for column in payload["columns"]:
        assert all(card["id"] != tid for card in column["tasks"]), column["name"]
    assert [card["id"] for card in payload["unmodelled"]] == [tid]
    assert payload["unmodelled"][0]["unmodelled_status"] == "in_progress"
    assert payload["unmodelled"][0]["status"] == "in_progress"


# ---------------------------------------------------------------------------
# 4. One audited normalisation verb
# ---------------------------------------------------------------------------

def test_normalise_writes_status_and_event_in_one_transaction(kanban_home: Path) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    tid = _new_card(kb, kbc)
    _plant_unmodelled(kb, kbc, tid)
    with kbc.connect() as conn:
        result = kb.normalise_task_status(
            conn, tid, to="blocked", reason="value outside the model",
            evidence="kanban audit: in_progress", actor="platform-stl",
        )
        assert result["changed"] is True and result["event"] == "status_changed"
        card = kb.get_task(conn, tid)
        assert card.status == "blocked"
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'status_changed' "
            "ORDER BY id DESC LIMIT 1", (tid,)
        ).fetchone()
        assert row is not None, "the transition left no audit row (the origin defect)"
        import json as _json

        payload = _json.loads(row["payload"])
        assert payload["from"] == "in_progress"
        assert payload["to"] == "blocked"
        assert payload["by"] == "platform-stl"
        assert payload["reason"] and payload["evidence"]


def test_normalise_refuses_a_modelled_source(kanban_home: Path) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    tid = _new_card(kb, kbc)  # reads 'ready'
    with kbc.connect() as conn:
        with pytest.raises(ValueError) as excinfo:
            kb.normalise_task_status(conn, tid, to="blocked", reason="r", evidence="e",
                                     actor="platform-coder")
        assert "modelled status" in str(excinfo.value)
        assert kb.get_task(conn, tid).status == "ready"


def test_normalise_refuses_an_unmodelled_target_and_missing_audit_fields(kanban_home: Path) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    tid = _new_card(kb, kbc)
    _plant_unmodelled(kb, kbc, tid)
    with kbc.connect() as conn:
        with pytest.raises(kb.InvalidStatus):
            kb.normalise_task_status(conn, tid, to="in_progress", reason="r", evidence="e",
                                     actor="a")
        with pytest.raises(ValueError):
            kb.normalise_task_status(conn, tid, to="done", reason="r", evidence="e", actor="a")
        with pytest.raises(ValueError, match="--reason"):
            kb.normalise_task_status(conn, tid, to="ready", reason=" ", evidence="e", actor="a")
        with pytest.raises(ValueError, match="--evidence"):
            kb.normalise_task_status(conn, tid, to="ready", reason="r", evidence="", actor="a")
        assert kb.get_task(conn, tid).status == "in_progress"


def test_normalise_repairs_the_iso_started_at_in_the_same_audit_row(kanban_home: Path) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    tid = _new_card(kb, kbc)
    _plant_unmodelled(kb, kbc, tid, status="ready",
                      started_at="2026-11-07T07:43:01-05:00")
    with kbc.connect() as conn:
        result = kb.normalise_task_status(conn, tid, to="ready", reason="wrong storage type",
                                          evidence="kanban audit: started_at is text",
                                          actor="platform-coder")
        assert result["changed"] is False and result["event"] == "type_repaired"
        assert result["repaired"]["started_at"]["to"] > 1_000_000_000
        stored = conn.execute(
            "SELECT started_at, typeof(started_at) FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        assert stored[1] == "integer"
        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'type_repaired' "
            "ORDER BY id DESC LIMIT 1", (tid,)
        ).fetchone()
        assert event is not None
        import json as _json
        assert _json.loads(event["payload"])["repaired"]["started_at"]["from"] == (
            "2026-11-07T07:43:01-05:00")


# ---------------------------------------------------------------------------
# 5. The sweep instrument
# ---------------------------------------------------------------------------

def test_audit_verb_exits_nonzero_on_a_defect(kanban_home: Path, capsys) -> None:
    from hermes_cli import kanban as cli
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    tid = _new_card(kb, kbc)
    _plant_unmodelled(kb, kbc, tid)
    rc = cli._cmd_audit(argparse.Namespace(board=kb.get_current_board(), verbose=True, json=False))
    out = capsys.readouterr().out
    assert rc == 1
    assert tid in out and "in_progress" in out


def test_audit_verb_is_clean_on_a_modelled_board(kanban_home: Path, capsys) -> None:
    from hermes_cli import kanban as cli
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    _new_card(kb, kbc)
    rc = cli._cmd_audit(argparse.Namespace(board=kb.get_current_board(), verbose=False, json=False))
    assert rc == 0
    assert "clean" in capsys.readouterr().out
