"""The status VOCABULARY boundary: validate at write, render unmodelled as unmodelled,
and ONE normalisation door that writes the status and its audit event together.

The incident this pins down (financially board t_58e17240, measured 2026-11-07): a writer that
was NOT the kernel wrote ``status='in_progress'`` into ``tasks`` -- a value outside
``kanban_workflow.DEFAULT_STATUSES`` -- and left NO ``task_events`` row, so the board could
neither render the card's state nor prove the transition had happened. Five verbs then refused
it (reclaim: not running; reopen: only finished; unblock: blocked/scheduled only; repair:
PRAGMA only; block: rc 1), because every one of them switches on a status this one is not.

The negative control that matters: a test covering only the CLI verb would pass by construction.
So every path is exercised here, INCLUDING a raw ``sqlite3`` write that never imports the kernel
-- that is the writer that produced the row.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolated HERMES_HOME with an empty, guard-installed kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "platform-coder")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(var, raising=False)
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    kb._INITIALIZED_PATHS.clear()
    kbc._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    yield kb, conn
    conn.close()


def _plant_unmodelled(kb, conn, *, status="in_progress", started_at=None):
    """Write a row the way the offending writer did: bypassing the kernel AND the guards.

    The guards are DROPPED for the write and re-installed after, which is exactly the
    "row that predates the guard" state a legacy board is in -- the point is that the REPAIR
    path works on such a row, not that one can be created through any door.
    """
    tid = kb.create_task(conn, title="legacy row", created_by="test")
    for name in (*kb.STATUS_GUARD_TRIGGERS, *kb.EPOCH_GUARD_TRIGGERS):
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    conn.execute(
        "UPDATE tasks SET status = ?, started_at = ? WHERE id = ?", (status, started_at, tid))
    conn.commit()
    kb.install_write_guards(conn)
    return tid


# --------------------------------------------------------------------------
# 1. VALIDATE AT WRITE -- every path, including the one that bypasses the kernel
# --------------------------------------------------------------------------

def test_create_seam_refuses_an_unmodelled_initial_status(board):
    kb, conn = board
    before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    with pytest.raises(ValueError):
        kb.create_task(conn, title="carries the todo vocabulary", initial_status="in_progress",
                       created_by="test")
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before


def test_claim_seam_refuses_an_unmodelled_source_status(board):
    """The CAS interpolates the source status into SQL, so it is validated at the seam too."""
    kb, conn = board
    tid = kb.create_task(conn, title="claim seam", created_by="test")
    with pytest.raises(kb.StatusRefusal) as exc:
        kb._claim_and_open_run(conn, tid, "in_progress", "lock", int(time.time()) + 60,
                               int(time.time()))
    assert "in_progress" in str(exc.value)
    assert "ready" in str(exc.value)          # the legal set is named, not just "invalid"


def test_raw_sql_update_is_refused_by_the_stored_trigger(board):
    """THE NEGATIVE CONTROL: a writer with no kernel in the loop (the measured one)."""
    kb, conn = board
    tid = kb.create_task(conn, title="raw writer", created_by="test")
    with pytest.raises(sqlite3.IntegrityError) as exc:
        conn.execute("UPDATE tasks SET status = 'in_progress' WHERE id = ?", (tid,))
    conn.rollback()
    assert "in_progress" in str(exc.value)
    assert "not a kanban status" in str(exc.value)
    assert kb.get_task(conn, tid).status == "ready"


def test_raw_sql_insert_is_refused_by_the_stored_trigger(board):
    _, conn = board
    now = int(time.time())
    with pytest.raises(sqlite3.IntegrityError) as exc:
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at) "
            "VALUES ('t_raw', 'raw insert', 'in_progress', ?)", (now,))
    conn.rollback()
    assert "in_progress" in str(exc.value)


def test_a_legacy_unmodelled_row_is_refused_by_every_verb(board):
    """The state itself: no verb switches on it, so every one of them declines BY NAME.

    Before the vocabulary guard these five falls-through returned ``False``/``rc 1`` with no
    sentence naming the value -- the operator saw a verb that "did nothing" on a card that was
    standing in no phase at all. Each refusal now carries the verb AND the value.
    """
    kb, conn = board
    tid = _plant_unmodelled(kb, conn)
    assert kb.get_task(conn, tid).status == "in_progress"

    for verb, call in (
        ("kanban_block", lambda: kb.block_task(conn, tid, reason="try")),
        ("kanban_unblock", lambda: kb.unblock_task(conn, tid)),
        ("kanban_reclaim", lambda: kb.reclaim_task(conn, tid)),
        ("kanban_schedule", lambda: kb.schedule_task(conn, tid, reason="try")),
    ):
        with pytest.raises(kb.StatusRefusal) as exc:
            call()
        assert verb in str(exc.value) and "in_progress" in str(exc.value)

    ok, why, _ = kb.reopen_task(conn, tid, dest_status="ready", reason="try", actor="test")
    assert ok is False and "in_progress" in why
    # ...and it has NOT been silently moved into a phase by any of those refusals.
    assert kb.get_task(conn, tid).status == "in_progress"


def test_write_guards_are_installed_on_a_fresh_board(board):
    kb, conn = board
    assert kb.write_guards_present(conn) == sorted(
        (*kb.STATUS_GUARD_TRIGGERS, *kb.EPOCH_GUARD_TRIGGERS))


def test_guard_ddl_does_not_break_a_normal_write(board):
    """A guard that refuses legal work would be worse than the defect."""
    kb, conn = board
    tid = kb.create_task(conn, title="normal lifecycle", created_by="test")
    assert kb.claim_task(conn, tid) is not None
    assert kb.block_task(conn, tid, reason="waiting on a person", kind="needs_input")
    assert kb.unblock_task(conn, tid)
    assert kb.complete_task(conn, tid, summary="done") is True
    assert kb.get_task(conn, tid).status in kb.VALID_STATUSES


# --------------------------------------------------------------------------
# 2. TYPE-CHECK the epoch columns
# --------------------------------------------------------------------------

def test_epoch_type_check_refuses_an_iso_string(board):
    kb, _ = board
    with pytest.raises(ValueError) as exc:
        kb.require_epoch("2026-11-07T07:43:01-05:00", column="started_at")
    assert "started_at" in str(exc.value)
    assert "epoch" in str(exc.value)
    # Numeric values pass through, floats truncate, None is a legal empty.
    assert kb.require_epoch(1757240000, column="started_at") == 1757240000
    assert kb.require_epoch(1757240000.9, column="started_at") == 1757240000
    assert kb.require_epoch(None, column="started_at") is None
    with pytest.raises(ValueError):
        kb.require_epoch(True, column="started_at")


def test_raw_iso_write_into_started_at_is_refused_by_the_trigger(board):
    kb, conn = board
    tid = kb.create_task(conn, title="epoch typing", created_by="test")
    with pytest.raises(sqlite3.IntegrityError) as exc:
        conn.execute("UPDATE tasks SET started_at = '2026-11-07T07:43:01-05:00' WHERE id = ?",
                     (tid,))
    conn.rollback()
    assert "started_at" in str(exc.value)


def test_census_reports_both_defect_classes(board):
    kb, conn = board
    tid = _plant_unmodelled(kb, conn, started_at="2026-11-07T07:43:01-05:00")
    found = kb.census_unmodelled_status(conn)
    # `status` stays the row's REAL status even when the defect is an epoch column: a consumer
    # that read the offending value out of `status` would treat a timestamp as a phase.
    assert {"id": tid, "status": "in_progress", "defect": "status",
            "value": "in_progress"} in found
    assert {"id": tid, "status": "in_progress", "defect": "started_at",
            "value": "2026-11-07T07:43:01-05:00"} in found


# --------------------------------------------------------------------------
# 4. THE NORMALISATION VERB -- status and its event, one transaction
# --------------------------------------------------------------------------

def test_normalise_writes_the_status_and_its_event_in_one_act(board):
    kb, conn = board
    tid = _plant_unmodelled(kb, conn)
    ok, why, info = kb.normalise_task(
        conn, tid, to="blocked", reason="written by a todo-vocabulary tool",
        evidence="financially t_58e17240", by="platform-coder", block_kind="needs_input")
    assert ok, why
    assert info == {"from_status": "in_progress", "to_status": "blocked",
                    "requested_to": "blocked"}
    task = kb.get_task(conn, tid)
    assert task.status == "blocked"
    events = kb.list_events(conn, tid)
    changed = [e for e in events if e.kind == "status_changed"]
    assert len(changed) == 1
    payload = changed[0].payload
    assert payload["from"] == "in_progress"
    assert payload["to"] == "blocked"
    assert payload["by"] == "platform-coder"
    assert payload["reason"] == "written by a todo-vocabulary tool"
    assert payload["evidence"] == "financially t_58e17240"
    # The legacy event the live feed and the sweeps read, and the comment for a human.
    assert [e for e in events if e.kind == "status"]
    assert any("NORMALISED" in c.body for c in kb.list_comments(conn, tid))


def test_normalise_is_the_only_door_out_and_it_is_auditable_in_order(board):
    """After the repair the card is a NORMAL modelled card again: the promotion path works."""
    kb, conn = board
    tid = _plant_unmodelled(kb, conn)
    assert kb.normalise_task(conn, tid, to="blocked", reason="repair", evidence="card")[0]
    assert kb.unblock_task(conn, tid) is True
    kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert kinds.index("status_changed") < kinds.index("unblocked")


def test_normalise_refuses_a_target_that_would_fabricate_work_state(board):
    kb, conn = board
    tid = _plant_unmodelled(kb, conn)
    for bad in ("done", "review", "running", "in_progress", ""):
        with pytest.raises(ValueError):
            kb.normalise_task(conn, tid, to=bad, reason="r", evidence="e")
    assert kb.get_task(conn, tid).status == "in_progress"


def test_normalise_requires_reason_and_evidence(board):
    kb, conn = board
    tid = _plant_unmodelled(kb, conn)
    with pytest.raises(ValueError):
        kb.normalise_task(conn, tid, to="todo", reason="  ", evidence="e")
    with pytest.raises(ValueError):
        kb.normalise_task(conn, tid, to="todo", reason="r", evidence="  ")
    assert kb.get_task(conn, tid).status == "in_progress"


def test_normalise_to_ready_regates_behind_an_unfinished_parent(board):
    kb, conn = board
    child = kb.create_task(conn, title="child", created_by="test", parents=())
    parent = kb.create_task(conn, title="parent", created_by="test")
    conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (parent, child))
    conn.commit()
    _drop = (*kb.STATUS_GUARD_TRIGGERS, *kb.EPOCH_GUARD_TRIGGERS)
    for name in _drop:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    conn.execute("UPDATE tasks SET status = 'in_progress' WHERE id = ?", (child,))
    conn.commit()
    kb.install_write_guards(conn)
    ok, _, info = kb.normalise_task(conn, child, to="ready", reason="repair", evidence="card")
    assert ok
    assert info["to_status"] == "todo"
    payload = next(e for e in kb.list_events(conn, child) if e.kind == "status_changed").payload
    assert payload["requested_to"] == "ready" and "regated" in payload


def test_normalise_is_a_no_op_on_an_already_modelled_card(board):
    kb, conn = board
    tid = kb.create_task(conn, title="healthy", created_by="test")
    ok, why, info = kb.normalise_task(conn, tid, to="ready", reason="r", evidence="e")
    assert ok is False and "already" in why and info == {}
    assert not [e for e in kb.list_events(conn, tid) if e.kind == "status_changed"]


# --------------------------------------------------------------------------
# 3. RENDER UNMODELLED AS UNMODELLED
# --------------------------------------------------------------------------

def test_render_helpers(board):
    kb, _ = board
    assert kb.render_status("ready") == "ready"
    assert kb.render_status("in_progress") == "UNMODELLED ('in_progress')"
    assert kb.is_modelled_status("ready") is True
    assert kb.is_modelled_status("in_progress") is False
    assert kb.is_modelled_status(None) is False


def test_list_line_prints_an_unmodelled_row_as_unmodelled(board):
    from hermes_cli.kanban_output import _fmt_task_line
    kb, conn = board
    tid = _plant_unmodelled(kb, conn)
    line = _fmt_task_line(kb.get_task(conn, tid))
    assert "UNMODELLED ('in_progress')" in line
    assert " in_progress " not in line          # never drawn as a live column value


def test_task_dict_carries_the_judgement_without_rewriting_the_raw_status(board):
    from hermes_cli.kanban_output import _task_to_dict
    kb, conn = board
    tid = _plant_unmodelled(kb, conn)
    d = _task_to_dict(kb.get_task(conn, tid))
    assert d["status"] == "in_progress"          # consumers keep parsing what they parsed
    assert d["status_modelled"] is False
    assert d["status_display"] == "UNMODELLED ('in_progress')"


def test_diagnostic_is_critical_and_names_the_repair_door(board):
    from hermes_cli import kanban_diagnostics as kd
    kb, conn = board
    tid = _plant_unmodelled(kb, conn)
    task = kb.get_task(conn, tid)
    diags = kd.compute_task_diagnostics(task, kb.list_events(conn, tid), [], now=int(time.time()))
    mine = [d for d in diags if d.kind == "unmodelled_status"]
    assert mine and mine[0].severity == "critical"
    assert "normalise" in mine[0].detail
    assert mine[0].data["status"] == "in_progress"


def _parse_printed_command(command: str):
    """Parse a printed ``hermes kanban ...`` command with the REAL CLI parser.

    A refusal that prints a command the parser rejects sends the operator down a dead end (the
    card id is positional; the first draft printed ``--card <id>``, which argparse refuses with
    ``unrecognized arguments``). The instructions are therefore parsed, not just grepped for.
    """
    import argparse
    import shlex

    from hermes_cli.kanban_parser import build_parser

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    build_parser(parser.add_subparsers(dest="command"))
    argv = shlex.split(command)
    assert argv[:2] == ["hermes", "kanban"], argv
    return parser.parse_args(argv[1:])


def test_every_printed_repair_command_parses_and_runs_as_printed(board):
    """The repair door the module TELLS you to use must be the one the parser accepts."""
    import re

    from hermes_cli import kanban_diagnostics as kd
    kb, conn = board
    tid = _plant_unmodelled(kb, conn)

    # 1. the refusal raised by every write seam
    refusal = str(kb.StatusRefusal("in_progress", where="kanban_block"))
    printed = re.search(r"`(hermes kanban normalise[^`]*)`", refusal)
    assert printed, refusal
    command = (printed.group(1)
               .replace("<id>", tid)
               .replace("<ready|blocked|todo>", "ready")
               .replace("<why>", "probe")
               .replace("<ref>", "probe"))
    args = _parse_printed_command(command)
    assert args.task_id == tid
    assert args.to == "ready" and args.reason == "probe" and args.evidence == "probe"

    # ...and the card's own contract spelling (`--card <id>`) must resolve to the same card:
    # the verb takes the id positionally, but the boundary document names the flag form, and an
    # operator who types either must land on the same repair.
    args = _parse_printed_command(
        f"hermes kanban normalise --card {tid} --to ready --reason probe --evidence probe")
    assert (args.task_id or args.card) == tid and args.to == "ready"

    # 2. the diagnostic's suggested action (the same door, machine-readable)
    task = kb.get_task(conn, tid)
    diags = kd.compute_task_diagnostics(task, kb.list_events(conn, tid), [], now=int(time.time()))
    action = next(a for d in diags if d.kind == "unmodelled_status" for a in d.actions)
    args = _parse_printed_command(action.payload["command"])
    assert args.task_id == tid and args.to == "ready"
    assert args.reason and args.evidence      # both required, both supplied


def test_dashboard_buckets_an_unmodelled_row_into_its_own_column(board, monkeypatch):
    """It is NOT drawn into a live column and NOT counted as work in progress."""
    from plugins.kanban.dashboard import plugin_api as api
    kb, conn = board
    tid = _plant_unmodelled(kb, conn)

    class _Ctx:
        def __enter__(self):
            return ("default", conn)

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(api, "_board_conn", lambda board=None: _Ctx())
    payload = api.get_board(tenant=None, include_archived=False, board=None,
                            workflow_template_id=None, current_step_key=None)
    by_name = {c["name"]: c["tasks"] for c in payload["columns"]}
    assert "unmodelled" in by_name
    assert [t["id"] for t in by_name["unmodelled"]] == [tid]
    assert by_name["unmodelled"][0]["status_display"] == "UNMODELLED ('in_progress')"
    for live in ("triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done"):
        assert tid not in [t["id"] for t in by_name.get(live, [])]
    assert payload["columns"][-1]["name"] == "unmodelled"     # trailing, never a live lane


def test_dashboard_omits_the_unmodelled_column_when_the_board_is_clean(board, monkeypatch):
    from plugins.kanban.dashboard import plugin_api as api
    kb, conn = board
    kb.create_task(conn, title="healthy", created_by="test")

    class _Ctx:
        def __enter__(self):
            return ("default", conn)

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(api, "_board_conn", lambda board=None: _Ctx())
    payload = api.get_board(tenant=None, include_archived=False, board=None,
                            workflow_template_id=None, current_step_key=None)
    assert "unmodelled" not in [c["name"] for c in payload["columns"]]


# --------------------------------------------------------------------------
# 5. CENSUS -- the finding the nightly sweep must raise, per board and card
# --------------------------------------------------------------------------

def test_census_is_clean_on_a_healthy_board(board):
    kb, conn = board
    kb.create_task(conn, title="healthy", created_by="test")
    assert kb.census_unmodelled_status(conn) == []
