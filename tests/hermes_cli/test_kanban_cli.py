"""Tests for the kanban CLI surface (hermes_cli.kanban)."""

from __future__ import annotations

import argparse
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Workspace flag parsing
# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------
# run_slash smoke tests (end-to-end via the same entry both CLI and gateway use)
# ---------------------------------------------------------------------------



def test_kanban_list_json_includes_session_id(kanban_home):
    """JSON output exposes `session_id` so external clients (Scarf, web
    dashboards) don't need a side query to filter by chat session."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect() as conn:
        kb.create_task(
            conn, title="acp task", assignee="alice", session_id="acp-x"
        )
    raw = kc.run_slash("list --json")
    payload = json.loads(raw)
    assert any(
        row.get("title") == "acp task"
        and row.get("session_id") == "acp-x"
        for row in payload
    )


def test_run_slash_stats_text_includes_review_lane(kanban_home):
    """``stats`` text output must surface the ``review`` lane: board_stats() counts
    every non-archived status, so the ``By status:`` block must list ``review`` too
    or its count silently disappears while ``--json`` still shows it."""
    import re

    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="needs review", assignee="alice")
        assert kb.request_review(conn, tid, summary="s", force=True)

    assert re.search(r"review\s+1", kc.run_slash("stats"))
    assert json.loads(kc.run_slash("stats --json"))["by_status"].get("review") == 1


def test_kanban_show_json_includes_runtime_limit(kanban_home):
    with kbc.connect() as conn:
        bounded_id = kb.create_task(
            conn, title="bounded task", max_runtime_seconds=2700
        )
        uncapped_id = kb.create_task(conn, title="uncapped task")

    bounded = json.loads(kc.run_slash(f"show {bounded_id} --json"))
    uncapped = json.loads(kc.run_slash(f"show {uncapped_id} --json"))

    assert bounded["task"]["max_runtime_seconds"] == 2700
    assert uncapped["task"]["max_runtime_seconds"] is None


def test_kanban_show_text_renders_graph_with_open_connection(kanban_home):
    with kbc.connect_closing() as conn:
        parent_id = kb.create_task(conn, title="parent task")
        child_id = kb.create_task(conn, title="child task")
        kb.link_tasks(conn, parent_id=parent_id, child_id=child_id)

    output = kc.run_slash(f"show {child_id}")

    assert "child task" in output
    assert parent_id in output
    assert "Cannot operate on a closed database" not in output


def test_kanban_edit_updates_documented_task_fields(kanban_home):
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="old title", body="old body", priority=2)

    kc.run_slash(
        f"edit {task_id} --title 'new title' --body 'new body' --priority 70"
    )

    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)
    assert (task.title, task.body, task.priority) == ("new title", "new body", 70)
    assert any(event.kind == "reprioritized" for event in events)


def test_worker_link_preserves_foreign_child_rules(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        worker = kb.create_task(conn, title="worker")
        assert kb.claim_task(conn, worker, claimer="worker") is not None
        worker_run_id = kb.get_task(conn, worker).current_run_id
        parent = kb.create_task(conn, title="unfinished parent")
        ready_child = kb.create_task(conn, title="foreign ready child")
        running_child = kb.create_task(conn, title="foreign running child")
        assert kb.claim_task(conn, running_child, claimer="other") is not None

    monkeypatch.setenv("HERMES_KANBAN_TASK", worker)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(worker_run_id))

    assert kc._cmd_link(argparse.Namespace(
        parent_id=parent, child_id=ready_child,
    )) == 0
    with pytest.raises(ValueError, match="child is already running"):
        kc._cmd_link(argparse.Namespace(
            parent_id=parent, child_id=running_child,
        ))
    # Owner handoff: the worker links its own running card, proving ownership
    # with HERMES_KANBAN_RUN_ID — the one path _cmd_link forwards a run id for.
    assert kc._cmd_link(argparse.Namespace(
        parent_id=parent, child_id=worker,
    )) == 0

    with kbc.connect_closing() as conn:
        assert kb.parent_ids(conn, ready_child) == [parent]
        assert kb.parent_ids(conn, running_child) == []
        assert kb.parent_ids(conn, worker) == [parent]


def test_board_override_is_isolated_per_concurrent_call(kanban_home, monkeypatch):
    kb.create_board("alpha")
    kb.create_board("beta")

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)

    barrier = threading.Barrier(2)
    original_init_db = kb.init_db

    def slow_init_db(*args, **kwargs):
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        return original_init_db(*args, **kwargs)

    monkeypatch.setattr(kb, "init_db", slow_init_db)

    failures: list[str] = []

    def worker(board: str, title: str) -> None:
        args = parser.parse_args(["kanban", "--board", board, "create", title])
        rc = kc.kanban_command(args)
        if rc != 0:
            failures.append(f"{board}:{rc}")

    t1 = threading.Thread(target=worker, args=("alpha", "alpha-task"))
    t2 = threading.Thread(target=worker, args=("beta", "beta-task"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert failures == []

    with kbc.connect_closing(board="alpha") as conn:
        alpha_titles = [row.title for row in kb.list_tasks(conn, limit=100)]
    with kbc.connect_closing(board="beta") as conn:
        beta_titles = [row.title for row in kb.list_tasks(conn, limit=100)]

    assert alpha_titles == ["alpha-task"]
    assert beta_titles == ["beta-task"]


# ---------------------------------------------------------------------------
# Integration with the COMMAND_REGISTRY
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# reclaim + reassign CLI smoke tests
# ---------------------------------------------------------------------------

def test_run_slash_reclaim_running_task(kanban_home):
    import re
    import time
    import secrets
    from hermes_cli import kanban_db_connect as kbc

    out1 = kc.run_slash("create 'stuck worker task' --assignee broken-model")
    m = re.search(r"(t_[a-f0-9]+)", out1)
    assert m
    tid = m.group(1)

    # Simulate a running claim outside TTL.
    conn = kbc.connect()
    try:
        lock = secrets.token_hex(4)
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=? WHERE id=?",
            (lock, int(time.time()) + 3600, 4242, tid),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (tid, lock, int(time.time()) + 3600, 4242, int(time.time())),
        )
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (rid, tid))
        conn.commit()
    finally:
        conn.close()

    out = kc.run_slash(f"reclaim {tid} --reason 'test'")
    assert "Reclaimed" in out, out
    # Status back to ready.
    out2 = kc.run_slash(f"show {tid}")
    assert "ready" in out2.lower()




# ---------------------------------------------------------------------------
# /kanban specify — slash surface (same entry point CLI + gateway use)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /kanban help / no-args / unknown-action UX (issue #21794)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Invariant E: a FIXTURE filing may never land on the LIVE kanban home
# (card t_bba20dec -- 32 "fixture board" rows on the live ops board, 2026-10-05)
# ---------------------------------------------------------------------------

def _run_kanban_argv(argv):
    """Drive the REAL CLI seam (parser -> ``kanban_command``), not the handler alone."""
    from hermes_cli.kanban import kanban_command
    from hermes_cli.kanban_parser import build_parser

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    build_parser(parser.add_subparsers(dest="command"))
    return kanban_command(parser.parse_args(["kanban", *argv]))


@pytest.fixture
def live_board_home(tmp_path, monkeypatch):
    """A host whose LIVE kanban root is this test's own temp home (never the operator's).

    ``get_default_hermes_root()`` resolves the live root from ``HERMES_HOME`` (with the
    platform default as the native fallback), and the fixture guard deliberately does NOT
    honour ``HERMES_KANBAN_HOME`` -- that variable is what a harness uses to redirect
    itself. So pointing ``HERMES_HOME`` at a temp home makes the stores minted under it the
    LIVE stores for the duration of this test, which is exactly the shape of the defect.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_HOME", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    kb.create_board("ops")
    return home


def _fixture_rows(store: Path) -> list:
    """``created_by='fixture'`` rows actually present in ``store`` (read-only).

    A store with no ``tasks`` table (an archived/empty shell) carries no fixture rows.
    """
    if not store.exists():
        return []
    conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT id, title FROM tasks "
            "WHERE lower(trim(COALESCE(created_by, ''))) = 'fixture'"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def test_fixture_filing_onto_the_live_home_is_refused(live_board_home, capsys):
    """POSITIVE (the defect): ``--board ops --created-by fixture`` against the LIVE home.

    The 2026-10-05 leak minted exactly this command from stale harness copies; it must be
    refused with a non-zero rc and leave no row behind.
    """
    from hermes_cli.kanban_scratch import board_store

    rc = _run_kanban_argv(
        ["--board", "ops", "create", "fixture board", "--created-by", "fixture"]
    )

    assert rc == 1, f"a fixture filing onto the live home must exit 1, got {rc}"
    err = capsys.readouterr().err
    assert "fixture-on-live-board" in err and "LIVE kanban store" in err, err
    assert "HERMES_KANBAN_HOME" in err, "the refusal must name the remedy"
    assert len([ln for ln in err.splitlines() if ln.strip()]) == 1, f"one line, got: {err!r}"
    assert _fixture_rows(board_store(live_board_home, "ops")) == []


def test_fixture_filing_still_succeeds_against_a_throwaway_kanban_root(
    live_board_home, tmp_path, monkeypatch, capsys
):
    """NEGATIVE CONTROL: the same create, redirected at a throwaway root, still works.

    Without this the guard could pass by banning the marker outright instead of gating it
    on the destination store -- which would break every legitimate test harness.
    """
    from hermes_cli.kanban_scratch import board_store

    throwaway = tmp_path / "throwaway-kanban"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(throwaway))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    kb.create_board("ops")

    rc = _run_kanban_argv(
        ["--board", "ops", "create", "fixture board", "--created-by", "fixture"]
    )

    assert rc == 0, capsys.readouterr().err
    landed = _fixture_rows(board_store(throwaway, "ops"))
    assert len(landed) == 1 and landed[0][1] == "fixture board"
    # ...and the live store stayed clean through the whole exchange.
    assert _fixture_rows(board_store(live_board_home, "ops")) == []


@pytest.mark.allow_real_home_io  # READ-ONLY probe of the live boards -- that is the point
def test_no_fixture_rows_on_any_live_board_store():
    """The live-estate invariant: zero ``created_by='fixture'`` rows on ANY live store.

    Read-only, and resolved from the REAL host home (``Path.home()``, which the test suite
    deliberately does not redirect) rather than the test ``HERMES_HOME``: this is the
    regression detector for the whole class, so it must look where the operator's boards
    actually are. Skips cleanly on a host that carries no live board set.
    """
    from hermes_cli.kanban_scratch import live_board_stores

    root = Path.home() / ".hermes"
    stores = [p for p in sorted(live_board_stores(root)) if p.exists()]
    if not stores:
        pytest.skip(f"no live kanban board store on this host (looked under {root})")

    offenders = {str(store): rows for store in stores if (rows := _fixture_rows(store))}
    assert offenders == {}, f"fixture rows on live board stores: {offenders}"


