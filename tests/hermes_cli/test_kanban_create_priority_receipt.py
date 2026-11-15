"""`hermes kanban create` PRINTS the priority decision from the record it wrote.

The card is filed per board, and the board's policy may place it somewhere other than the
number its filer typed. Two failures are pinned here, both measured on the live store before
the change:

* the verb printed ``Created <id>  (<status>, assignee=...)`` and NOTHING about the priority,
  so an ask of 890500 that landed at 599999 read exactly like an honoured one, and an ask of 1
  that landed at 590001 said only "Created";
* a reader that printed the BIRTH record as if it were the current value would print a number
  the card does not hold - 81 of 519 cards on the research board had been re-ranked after
  birth by the gate invariant, so the birth record and the row really do diverge.

Hence: the stored priority is on every create, the birth record is printed from the ``created``
event verbatim, and a value that moved after birth names the record that moved it - or says
that no such record exists, because an unaccounted value is the defect, not a detail.
"""

from __future__ import annotations

import argparse
import json

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_parser import build_parser

# A policy whose reason carries a NONCE: the printed reason must be the stored one, not text a
# reader-side recomputation could have produced.
NONCE = "band 700000-799999: placed at its rest floor"

BAND = '''
def band_birth(requested, assignee, board, title, body):
    requested = int(requested)
    applied = min(max(requested, 700000), 799999)
    return {"asked": requested, "applied": applied, "clamped": applied != requested,
            "queue_tier": "default", "rule": "floor" if applied != requested else "own",
            "reason": "%s"}
''' % NONCE


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_WORKSPACE", "HERMES_KANBAN_OPERATOR_ASK",
                "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_ADVISORY_SKILLS"):
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


def _wire(home, source=BAND):
    path = home / "band_policy.py"
    path.write_text(source, encoding="utf-8")
    kb.write_board_metadata("default", priority_policy={"module": str(path),
                                                        "function": "band_birth"})
    return path


def _create(argv):
    """Drive the real ``hermes kanban create`` parser into ``_cmd_create``."""
    root = argparse.ArgumentParser(prog="hermes")
    build_parser(root.add_subparsers())
    return kc._cmd_create(root.parse_args(["kanban", "create", *argv]))


def _only_task(conn):
    rows = conn.execute("SELECT id, priority FROM tasks").fetchall()
    assert len(rows) == 1, rows
    return rows[0]["id"], rows[0]["priority"]


def _birth(conn, task_id):
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'created' "
        "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    return json.loads(row["payload"]).get("priority_policy")


# --- the receipt on the create line ----------------------------------------------------

def test_create_prints_the_stored_priority_and_the_record_verbatim(conn, home, capsys):
    _wire(home)
    assert _create(["clamp probe", "--priority", "900000"]) == 0
    out = capsys.readouterr().out
    task_id, stored = _only_task(conn)
    # The store, not the ask, is what the line carries.
    assert stored == 799999
    assert f"priority={stored}" in out
    birth = _birth(conn, task_id)
    assert birth["asked"] == 900000 and birth["applied"] == stored
    # Every printed number comes FROM that record, verbatim - including the nonce reason, which
    # no reader could derive from the two numbers.
    assert f"priority record: asked 900000 -> born {stored}" in out
    assert "clamped yes" in out
    assert "rule floor" in out
    assert NONCE in out
    assert birth["reason"] in out


def test_create_prints_the_stored_priority_even_when_it_equals_the_ask(conn, home, capsys):
    _wire(home)
    assert _create(["honoured probe", "--priority", "750000"]) == 0
    out = capsys.readouterr().out
    task_id, stored = _only_task(conn)
    assert stored == 750000
    # Ask 2 on the card: "Created" is never the whole story about a field the caller set.
    assert "priority=750000" in out
    # The policy left the value alone, so no birth record was written - and none is invented.
    assert _birth(conn, task_id) is None
    assert "priority record:" not in out


def test_create_without_a_policy_still_prints_the_stored_priority(conn, home, capsys):
    assert _create(["plain probe", "--priority", "42"]) == 0
    out = capsys.readouterr().out
    _task_id, stored = _only_task(conn)
    assert stored == 42 and "priority=42" in out
    assert "priority record:" not in out


def test_json_carries_the_same_receipt(conn, home, capsys):
    _wire(home)
    assert _create(["json probe", "--priority", "1", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    task_id, stored = _only_task(conn)
    assert payload["priority_receipt"]["birth"] == _birth(conn, task_id)
    assert payload["priority_receipt"]["birth"]["applied"] == stored


# --- a value that moved AFTER birth ----------------------------------------------------

def test_a_move_after_birth_names_the_record_that_moved_it(conn, home):
    _wire(home)
    assert _create(["moved probe", "--priority", "1"]) == 0
    task_id, stored = _only_task(conn)
    assert stored == 700000
    # The gate invariant lifts a card above its parent after birth; that is a SECOND decision.
    conn.execute("UPDATE tasks SET priority = 790001 WHERE id = ?", (task_id,))
    kb._append_event(conn, task_id, "reprioritized", {
        "priority": 790001, "before": stored, "cause": "link",
        "gate": "t_parent0001", "gate_priority": 790001,
    })
    conn.commit()
    trail = kb.priority_receipt(conn, task_id, 790001)
    lines = kc._priority_receipt_lines(trail, 790001)
    assert lines[0].startswith("priority record: asked 1 -> born 700000")
    assert ("priority moved after birth: 700000 -> 790001 (cause link, gate t_parent0001 "
            "at 790001)") in lines[1]


def test_an_unrecorded_move_is_printed_as_unrecorded(conn, home):
    _wire(home)
    assert _create(["silent move probe", "--priority", "1"]) == 0
    task_id, stored = _only_task(conn)
    conn.execute("UPDATE tasks SET priority = 790777 WHERE id = ?", (task_id,))
    conn.commit()
    trail = kb.priority_receipt(conn, task_id, 790777)
    lines = kc._priority_receipt_lines(trail, 790777)
    assert "NO RECORD of the move" in lines[1]
    assert "790777" in lines[1]


def test_a_stale_reprioritization_is_not_offered_as_the_cause(conn, home):
    _wire(home)
    assert _create(["stale probe", "--priority", "1"]) == 0
    task_id, stored = _only_task(conn)
    kb._append_event(conn, task_id, "reprioritized", {"priority": 705000, "cause": "link",
                                                      "gate": "t_gone", "gate_priority": 705000})
    conn.commit()
    # The row still holds its birth value: the old re-rank names a number the card does not have.
    trail = kb.priority_receipt(conn, task_id, stored)
    assert trail["moved"] is None
    lines = kc._priority_receipt_lines(trail, stored)
    # The birth record is still true and still printed; only the CAUSE claim is withheld.
    assert lines and lines[0].startswith("priority record: asked 1 -> born 700000")
    assert not any("moved after birth" in line for line in lines)
