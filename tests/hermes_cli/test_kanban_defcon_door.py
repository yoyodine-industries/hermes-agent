"""The designation door (`hermes kanban defcon designate`) - the way into the reserved tranche.

Card t_38e29432: the armed storage guard on a board with a floor REFUSES a priority in
``990000..999999`` unless a LIVE ``priority_designations`` row backs it (or the card carries
an operator-ask / SEV1 marker), and its refusal names ``hermes kanban defcon designate``.
The 2026-10-10 ``hermes update`` half-applied its override restore and that verb was gone from
the running CLI, so every ask the fleet filed was refused its rank and every
``record_sev1_designation`` call (deploy_train_nodes, hermes_repair_nodes, pr_evergreen_nodes)
exited non-zero - the ledger row is the evidence the guard reads, and nothing else legitimises a
reserved-tranche value.

These tests arm the guard a floored board actually runs (the DDL baseline the fleet's armed
boards carry), so the door is exercised against the REAL refusal rather than a stub: a raw
tranche write is refused, the door's ledger row admits the raise, and the raise lands.

Ported with the door itself from the recovered admission stack
(PR yoyodine-industries/hermes-agent#78, carrier card t_44485b2b).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

DDL_BASELINE = Path(__file__).with_name("tranche_ddl_baseline.json")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A temp HERMES_HOME with its own board store and no wiring of any kind."""
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


def _arm(conn):
    """Install the board's own armed guard - the pair of triggers its floor was armed with.

    Armed AFTER the test's cards are filed: a board floored at the tranche boundary admits no
    new card below 990000, and these tests file their cards at an ordinary priority first.
    """
    ddl = json.loads(DDL_BASELINE.read_text(encoding="utf-8"))["990000"]
    conn.execute(ddl["insert"])
    conn.execute(ddl["update"])
    conn.commit()


def _run_kanban(argv):
    from hermes_cli.kanban import kanban_command
    from hermes_cli.kanban_parser import build_parser

    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers())
    return kanban_command(parser.parse_args(["kanban", *argv]))


def _json_tail(capsys):
    out = capsys.readouterr().out
    return json.loads(out[out.index("{"):])


def _priority(conn, task_id):
    return conn.execute("SELECT priority FROM tasks WHERE id = ?", (task_id,)).fetchone()["priority"]


def _live_row(conn, task_id):
    return conn.execute(
        "SELECT * FROM priority_designations WHERE task_id = ? AND revoked_at IS NULL", (task_id,)
    ).fetchone()


def test_the_armed_guard_refuses_a_raw_tranche_write(conn):
    """The defect's premise: without a live designation row the tranche is closed."""
    tid = kb.create_task(conn, title="ordinary", assignee="coder", priority=899998)
    _arm(conn)
    with pytest.raises(Exception) as excinfo:
        conn.execute("UPDATE tasks SET priority = 999999 WHERE id = ?", (tid,))
        conn.commit()
    assert "DESIGNATED, never requested" in str(excinfo.value)
    assert "hermes kanban defcon designate" in str(excinfo.value)
    assert _priority(conn, tid) == 899998
    assert _live_row(conn, tid) is None


def test_the_door_writes_the_ledger_row_then_the_raise_lands(conn, capsys):
    """The DoD, in one run: `defcon designate`, then `edit --priority 999999`."""
    tid = kb.create_task(conn, title="ordinary", assignee="coder", priority=899998)
    _arm(conn)

    assert _run_kanban(["defcon", "designate", tid, "--reason", "operator ask",
                        "--authority", "default", "--json"]) == 0
    row = _json_tail(capsys)
    assert row["task_id"] == tid
    assert row["priority"] == 990000
    assert row["restore_priority"] == 899998
    ledger = _live_row(conn, tid)
    assert ledger is not None
    assert ledger["authority"] == "default"
    assert ledger["reason"] == "operator ask"
    assert _priority(conn, tid) == 990000

    # The raise the door licenses: the guard admits it BECAUSE the row is live.
    assert _run_kanban(["edit", tid, "--priority", "999999"]) == 0
    assert _priority(conn, tid) == 999999
    assert _live_row(conn, tid) is not None


def test_the_raise_is_still_refused_without_the_door(conn):
    """The same raise on a card with no designation is refused - the door is not a bypass."""
    tid = kb.create_task(conn, title="ordinary", assignee="coder", priority=899998)
    _arm(conn)
    with pytest.raises(Exception):
        conn.execute("UPDATE tasks SET priority = 999999 WHERE id = ?", (tid,))
        conn.commit()
    assert _live_row(conn, tid) is None


def test_the_door_needs_a_reason_and_a_real_card(conn, capsys):
    tid = kb.create_task(conn, title="ordinary", assignee="coder", priority=7)
    assert _run_kanban(["defcon", "designate", tid]) == 2
    assert "--reason is required" in capsys.readouterr().err
    assert _run_kanban(["defcon", "designate", "t_not_a_card", "--reason", "defcon 1"]) == 1
    assert "no such task" in capsys.readouterr().err
    assert kb.priority_designation(conn, tid) is None


def test_the_door_is_closed_to_a_delegated_child(conn, monkeypatch, capsys):
    tid = kb.create_task(conn, title="ordinary", assignee="coder", priority=12345)
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(kb.kanban_home()))
    assert _run_kanban(["defcon", "designate", tid, "--reason", "self-service"]) != 0
    assert "delegate_task child" in capsys.readouterr().err
    with pytest.raises(PermissionError):
        kb.designate_priority(conn, tid, reason="self-service")
    assert kb.priority_designation(conn, tid) is None
    assert _priority(conn, tid) == 12345
