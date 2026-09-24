"""A card body must be complete text: refuse one that ends in a truncation marker.

Regression for the live-board defect in which card ``t_928b510b`` was stored with its body
cut mid-sentence at 218 chars, tail ``...[truncated]``. The stub read exactly like a
complete spec, so its implementer could not act on it and had to reconstruct the operative
requirements from a comment thread plus a design doc.

The marker is written upstream of the store — the agent abbreviating its own tool
arguments, which is why no storage path is the fault — so the store is where the fragment
is made impossible. Two contracts are asserted:

* text longer than the boundary the defect sat under round-trips byte-for-byte through
  every path a card body can arrive on (create, comment, triage body), and
* a body whose last non-space characters are a marker is REFUSED and stores nothing,
  while a body that merely quotes one mid-text stays legal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

MARK = "...[truncated]"

# Deliberately past the 223-char boundary the live defect sat under.
LONG_BODY = (
    "OPERATOR DIRECTIVE (Rob): treat the update like a BLUE-GREEN DEPLOY. Maximize the "
    "non-critical update and pull-request work OFFLINE, outside any lockout window, and "
    "keep the lockout itself as short as the copy and the flip allow. Rollback is a swap "
    "back to the previous slot, and the hold covers ONLY the copy and the flip."
)
assert len(LONG_BODY) > 223


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _task(conn, task_id: str):
    task = kb.get_task(conn, task_id)
    assert task is not None
    return task


def test_long_body_round_trips_whole_through_every_write_path(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="spec", body=LONG_BODY, assignee="worker")
        assert _task(conn, tid).body == LONG_BODY

        kb.add_comment(conn, tid, "worker", LONG_BODY)
        assert [c.body for c in kb.list_comments(conn, tid)] == [LONG_BODY]

        triage_id = kb.create_task(conn, title="triage stub", body="stub", assignee="worker", triage=True)
        assert kb.specify_triage_task(conn, triage_id, body=LONG_BODY, author="worker") is True
        assert _task(conn, triage_id).body == LONG_BODY


def test_body_ending_in_a_truncation_marker_is_refused_and_stores_nothing(kanban_home):
    fragment = LONG_BODY[:204] + MARK
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError, match="truncated"):
            kb.create_task(conn, title="spec", body=fragment, assignee="worker")
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_comment_and_triage_body_refuse_a_trailing_marker(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="spec", body=LONG_BODY, assignee="worker")
        with pytest.raises(ValueError, match="truncated"):
            kb.add_comment(conn, tid, "worker", LONG_BODY[:115] + MARK)
        assert kb.list_comments(conn, tid) == []

        triage_id = kb.create_task(conn, title="triage stub", body=LONG_BODY, assignee="worker", triage=True)
        with pytest.raises(ValueError, match="truncated"):
            kb.specify_triage_task(conn, triage_id, body=LONG_BODY[:157] + MARK, author="worker")
        unchanged = _task(conn, triage_id)
        assert (unchanged.body, unchanged.status) == (LONG_BODY, "triage")


def test_a_body_quoting_a_marker_mid_text_is_still_stored_whole(kanban_home):
    quoted = (
        "Defect: a spec was stored cut mid-sentence, tail "
        "`minimize the lockout to ju" + MARK + "`. The implementer could not act on it."
    )
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="defect report", body=quoted, assignee="worker")
        assert _task(conn, tid).body == quoted
