"""``hermes kanban set-title`` / ``set-body`` — the field editors the CLI was missing.

After creation the only writer of ``tasks.title``/``tasks.body`` was the dashboard's PATCH
route (direct SQL in the plugin), so neither an operator nor an agent had a CLI mechanic to
correct a mis-titled card. Both verbs call ``kanban_db.patch_task_text``, which is the ONE
writer for those columns: it strips a stored title, refuses a blank one, records the
``edited`` audit event, and fires the post-commit observer with field NAMES only.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

ROOT = Path(__file__).parents[2]


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _card(conn, title: str = "old title", body: str = "old body") -> str:
    tid = kb.create_task(conn, title=title, assignee="alice", body=body)
    return tid


def _row(conn, tid: str):
    return conn.execute("SELECT title, body, status, assignee FROM tasks WHERE id = ?", (tid,)).fetchone()


def _edits(conn, tid: str) -> list:
    return list(
        conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'edited' ORDER BY id", (tid,)
        )
    )


def test_set_title_renames_the_card_and_records_the_edit(kanban_home):
    """Only the title moves; the body, status and assignee are left alone, and the rename
    lands on the audit trail as one ``edited`` event."""
    with kbc.connect_closing() as conn:
        tid = _card(conn)
        before = _row(conn, tid)

    kc.run_slash(f"set-title {tid} a clearer name")

    with kbc.connect_closing() as conn:
        after = _row(conn, tid)
        edits = _edits(conn, tid)
    assert after["title"] == "a clearer name"
    assert (after["body"], after["status"], after["assignee"]) == (
        before["body"], before["status"], before["assignee"]
    )
    assert len(edits) == 1
    assert edits[0]["payload"] is None, "the audit event carries no field values"


def test_set_body_replaces_the_body_and_an_empty_body_clears_it(kanban_home):
    """Body text may be empty — clearing it is the documented way to drop a stale body,
    and it is distinct from ``set-title``'s refusal of a blank title."""
    with kbc.connect_closing() as conn:
        tid = _card(conn)

    kc.run_slash(f"set-body {tid} corrected body text")
    with kbc.connect_closing() as conn:
        assert _row(conn, tid)["body"] == "corrected body text"

    kc.run_slash(f"set-body {tid}")
    with kbc.connect_closing() as conn:
        assert _row(conn, tid)["body"] == ""
        assert len(_edits(conn, tid)) == 2


def test_set_title_refuses_a_blank_title_and_an_unknown_id(kanban_home):
    """A blank title is refused by the domain layer (the same rule the dashboard route
    enforces) and an unknown id changes nothing."""
    with kbc.connect_closing() as conn:
        tid = _card(conn, title="keep me")

    out = kc.run_slash(f"set-title {tid}")
    assert "title cannot be empty" in out
    with kbc.connect_closing() as conn:
        assert _row(conn, tid)["title"] == "keep me"
        assert _edits(conn, tid) == []

    out = kc.run_slash("set-title t_00000000 nope")
    assert "t_00000000" in out, "the refusal names the id that did not resolve"


def test_set_title_takes_a_bulk_id_list(kanban_home):
    with kbc.connect_closing() as conn:
        ids = [kb.create_task(conn, title=f"card {i}", assignee="alice") for i in range(3)]

    kc.run_slash(f"set-title {ids[0]} swept --ids {ids[1]} {ids[2]}")

    with kbc.connect_closing() as conn:
        assert [_row(conn, i)["title"] for i in ids] == ["swept", "swept", "swept"]


def test_patch_task_text_is_the_single_writer_for_title_and_body(kanban_home):
    """The mutator the CLI verbs and the dashboard route share: an unknown id writes
    nothing, one call can set both fields, and asking for neither field is a no-op."""
    with kbc.connect_closing() as conn:
        tid = _card(conn, title="before", body="keep")

        assert kb.patch_task_text(conn, "t_00000000", title="nope") is False
        assert _edits(conn, "t_00000000") == []

        assert kb.patch_task_text(conn, tid, title="after", body="changed") is True
        assert kb.patch_task_text(conn, tid) is True  # nothing asked for: no write, no event

        row = _row(conn, tid)
        assert (row["title"], row["body"]) == ("after", "changed")
        assert len(_edits(conn, tid)) == 1

        with pytest.raises(ValueError, match="title cannot be empty"):
            kb.patch_task_text(conn, tid, title="   ")
        assert _row(conn, tid)["title"] == "after"


def test_set_body_runs_as_a_real_process(kanban_home):
    """End-to-end through ``hermes kanban``, not just the in-process slash entry."""
    with kbc.connect_closing() as conn:
        tid = _card(conn, body="stale")

    env = os.environ.copy()
    env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", "set-body", tid, "fresh", "body"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, check=False, timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    with kbc.connect_closing() as conn:
        assert _row(conn, tid)["body"] == "fresh body"
