"""The ``show`` surface must REPORT the block state, in both renderings (card t_67ed1645).

THE DEFECT. The block is legible in the store and illegible at the door: a card carrying
``status='blocked'``, ``block_kind='needs_input'`` and a full-sentence reason in its
``blocked`` event printed only ``status: blocked`` - no kind, no reason, no recurrences -
and ``show --json`` contained no key with "block" in it at all. A seat reading the
canonical surface saw an unexplained block, filed an escalation, and filed it again after
that one was closed (ESC-03418, then a second row for the SAME unchanged wait).

WHAT THESE TESTS PIN. Not "show exits 0": the CONTENT of both renderings, and the
distinction the card exists for -

* the KIND, and the different ways a human-ask park and a dependency wait read;
* the REASON, in words, taken from the block event (never from the title);
* the recurrences count and the block's age;
* the untyped failure-breaker park, whose only reason lives in ``last_failure_error``;
* a card that is NOT blocked carries no block object, even though ``block_kind``
  deliberately survives an unblock.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

#: The words a real block carries. Deliberately a full sentence naming a decision and who
#: owes it, because that is what a title never says (t_561c1f8a's reason, verbatim).
REASON = (
    "Operator consent required: the deliverable is GO/NO-GO on writing three live "
    "SOUL.md files, which my seat may propose but never apply"
)


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    return home


def _running_task(conn, title: str = "a parked card") -> str:
    """Create a card and drive it to ``running``, the state ``block_task`` acts on."""
    tid = kb.create_task(conn, title=title, assignee="platform-coder")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="platform-coder") is not None
    return tid


def _blocked_needs_input(conn) -> str:
    tid = _running_task(conn)
    assert kb.block_task(conn, tid, reason=REASON, kind="needs_input")
    return tid


def _show_json(task_id: str) -> dict:
    return json.loads(kanban_cli.run_slash(f"show {task_id} --json"))


def _show_text(task_id: str) -> str:
    return kanban_cli.run_slash(f"show {task_id}")


# ---------------------------------------------------------------------------
# --json carries the kind and the reason
# ---------------------------------------------------------------------------


def test_show_json_carries_block_kind_reason_and_recurrences(kanban_home):
    """The regression the card asks for: the JSON itself carries kind AND reason."""
    with kbc.connect_closing() as conn:
        tid = _blocked_needs_input(conn)

    block = _show_json(tid)["block"]

    assert block is not None, "a blocked card must render a block object"
    assert block["kind"] == "needs_input"
    assert block["reason"] == REASON
    assert block["recurrences"] == 1
    assert block["since"] and block["event"] == "blocked"


def test_show_json_task_row_carries_the_block_columns(kanban_home):
    """A reader that greps the JSON for "block" must find the task's own columns too."""
    with kbc.connect_closing() as conn:
        tid = _blocked_needs_input(conn)

    task = _show_json(tid)["task"]

    assert task["block_kind"] == "needs_input"
    assert task["block_recurrences"] == 1


def test_list_json_carries_the_block_kind(kanban_home):
    """``list --json`` is the other surface a seat scans; it must name the kind as well."""
    with kbc.connect_closing() as conn:
        tid = _blocked_needs_input(conn)

    rows = json.loads(kanban_cli.run_slash("list --json"))
    row = next(r for r in rows if r["id"] == tid)

    assert row["block_kind"] == "needs_input"


# ---------------------------------------------------------------------------
# Text rendering: kind, age, recurrences and the reason IN WORDS
# ---------------------------------------------------------------------------


def test_show_text_renders_kind_reason_recurrences_and_age(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _blocked_needs_input(conn)

    out = _show_text(tid)

    assert "block:" in out
    assert "needs_input" in out
    assert "a human must answer" in out
    assert REASON in out, "the reason must be the block event's words, not the title"
    assert "recurrences 1" in out
    assert "since " in out and "ago)" in out
    # The title is a work description; it is NOT the reason and must not stand in for it.
    assert f"reason: {REASON}" in out


def test_show_never_prints_a_bare_blocked_without_a_reason(kanban_home):
    """The defect signature: "status: blocked" with nothing that says why."""
    with kbc.connect_closing() as conn:
        tid = _blocked_needs_input(conn)

    out = _show_text(tid)

    assert "status:    blocked" in out
    assert "reason:" in out
    assert "NONE RECORDED" not in out


# ---------------------------------------------------------------------------
# A dependency wait must not read like a human ask
# ---------------------------------------------------------------------------


def test_dependency_park_renders_differently_from_needs_input(kanban_home):
    """A parent-gated wait resumes on its own; a human ask does not. Say which."""
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="the parent whose output is needed")
        tid = _running_task(conn, title="the waiting child")
        assert kb.block_task(
            conn, tid, kind="dependency", reason="needs the parent's output first",
            waits_on=[parent],
        )

    block = _show_json(tid)["block"]
    assert block["kind"] == "dependency"
    assert block["waits_on"] == [parent]
    assert block["event"] == "dependency_wait"

    out = _show_text(tid)
    assert "dependency" in out
    assert "resumes on its own" in out
    assert parent in out
    # ... and the human-ask wording is absent, so the two cannot be confused.
    assert "a human must answer" not in out


def test_needs_input_and_dependency_text_differ(kanban_home):
    with kbc.connect_closing() as conn:
        who = _blocked_needs_input(conn)
        parent = kb.create_task(conn, title="parent")
        waiting = _running_task(conn, title="waiting")
        assert kb.block_task(conn, waiting, kind="dependency",
                             reason="parent first", waits_on=[parent])

    assert _show_text(who) != _show_text(waiting)


# ---------------------------------------------------------------------------
# The untyped failure-breaker park
# ---------------------------------------------------------------------------


def test_untyped_park_reports_the_failure_error_as_the_reason(kanban_home):
    """The breaker parks ``blocked`` with no kind and no event - but it DOES say why it
    gave up, and that text is the only reason such a card has."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="died on the runner")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='blocked', block_kind=NULL, block_recurrences=0, "
                "consecutive_failures=1, last_failure_error=? WHERE id=?",
                ("worker exited 137 after 3 retries", tid),
            )
        block = kb.block_state(conn, tid)

    assert block is not None
    assert block["kind"] is None
    assert block["reason"] == "worker exited 137 after 3 retries"

    out = _show_text(tid)
    assert "untyped" in out
    assert "worker exited 137 after 3 retries" in out


def test_block_with_no_event_and_no_error_says_so_out_loud(kanban_home):
    """Never a silent blank: an unexplained park is REPORTED as unexplained."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="parked by hand")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))

    assert "NONE RECORDED" in _show_text(tid)


# ---------------------------------------------------------------------------
# Not blocked -> no block object (block_kind survives an unblock by design)
# ---------------------------------------------------------------------------


def test_unblocked_card_reports_no_block(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _blocked_needs_input(conn)
        assert kb.unblock_task(conn, tid) is not None
        task = kb.get_task(conn, tid)
        assert task.block_kind == "needs_input", "the column survives the unblock"
        assert task.status != "blocked"

    assert _show_json(tid)["block"] is None
    assert "block:" not in _show_text(tid)


def test_an_ordinary_card_reports_no_block(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="nothing wrong here")

    assert _show_json(tid)["block"] is None
    assert "block:" not in _show_text(tid)
