"""``hermes kanban unblock`` must name the refused status's own way forward.

Only a ``blocked``/``scheduled`` card can be unblocked, so an operator who typed it on a
card in ``review`` got ``cannot unblock <id> (not blocked/scheduled?)``: a refusal that
named no exit. ``review`` has two supported exits — ``reopen-review`` (back to the
implementer) and ``reassign`` to a reviewer — and the hint naming them must stay
status-scoped: a ``done`` card's refusal must not advertise a ``review`` exit.
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


def _card(conn, status: str, title: str = "card") -> str:
    tid = kb.create_task(conn, title=title, assignee="alice")
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
    return tid


def _status(conn, tid: str) -> str:
    return conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()["status"]


def test_unblock_refusal_on_a_review_card_names_the_review_exit(kanban_home):
    """The refusal keeps its wording and gains the exit the status actually has."""
    with kbc.connect_closing() as conn:
        tid = _card(conn, "review", "awaiting review")

    out = kc.run_slash(f"unblock {tid}")

    assert "(not blocked/scheduled?)" in out, "the original refusal is preserved"
    assert "reopen-review" in out, "the refusal names the verb that releases a review card"
    assert "reassign" in out, "the second exit is a route to a reviewer"
    assert tid in out
    with kbc.connect_closing() as conn:
        assert _status(conn, tid) == "review", "a refusal moves nothing"


def test_the_exit_the_refusal_names_actually_releases_the_card(kanban_home):
    """A hint naming a verb that itself refuses is a lie: the named exit has to work on
    exactly the state the hint was printed for."""
    with kbc.connect_closing() as conn:
        tid = _card(conn, "review")

    assert "reopen-review" in kc.run_slash(f"unblock {tid}")
    kc.run_slash(f"reopen-review {tid}")

    with kbc.connect_closing() as conn:
        assert _status(conn, tid) != "review", "the exit the refusal named released the card"


def test_unblock_refusal_still_names_the_triage_exit(kanban_home):
    """``triage`` keeps the hint it already had (one status-aware helper, both statuses)."""
    with kbc.connect_closing() as conn:
        tid = _card(conn, "triage")

    out = kc.run_slash(f"unblock {tid}")

    assert "triage is not a dead end" in out
    assert "promote" in out and "complete" in out


def test_unblock_refusal_on_a_done_card_advertises_no_exit(kanban_home):
    """The hint is keyed on the status, not appended to every refusal: ``done`` has no
    exit out of it, so the refusal must not promise one."""
    with kbc.connect_closing() as conn:
        tid = _card(conn, "done")

    out = kc.run_slash(f"unblock {tid}")

    assert tid in out
    assert "reopen-review" not in out
    assert "triage is not a dead end" not in out


def test_review_exit_hint_reaches_a_real_process(kanban_home):
    """End-to-end through ``hermes kanban``, not just the in-process slash entry."""
    with kbc.connect_closing() as conn:
        tid = _card(conn, "review", "e2e")

    env = os.environ.copy()
    env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", "unblock", tid],
        cwd=str(ROOT), env=env, capture_output=True, text=True, check=False, timeout=60,
    )

    assert "reopen-review" in (proc.stdout + proc.stderr)
    with kbc.connect_closing() as conn:
        assert _status(conn, tid) == "review"
