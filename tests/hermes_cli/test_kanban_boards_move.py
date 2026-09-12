"""Tests for the link-safe per-card move across boards.

``boards move`` used to relocate one card and silently sever every parent/child
link that touched it, which is unsafe: a child left with no parents on the
target auto-promotes to ``ready`` and the dispatcher spawns a worker for a card
whose dependency just landed on another board.

The contract these tests pin down:

* A move is **link-closed**: the requested card's undirected parent/child
  component (BFS over ``task_links``) is the unit of movement. Naming a linked
  card without ``--with-links`` refuses and names the size of the set; the
  operator either reruns with ``--with-links`` or unlinks first. No path through
  ``move_task`` severs an edge.
* Ids are **preserved** (same semantics as
  ``~/.hermes/kanban/migrations/migrate_apr25.py``), so tombstones and lineage
  stay coherent. A genuine id collision refuses; a collision that is really an
  interrupted move (the target row carries ``moved_in`` provenance for this
  source board and id) is **resumed** — the copy phase is skipped and the
  source removal finishes.
* Both boards get a WAL-safe backup first, the target write txn commits before
  any source delete, and every card in the set gets a ``moved_in`` /
  ``moved_out`` audit event.

Split out of ``test_kanban_boards.py`` (which keeps the board-lifecycle tests).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Ensure the worktree (not the stale global clone) is first on sys.path.
_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_move


# ---------------------------------------------------------------------------
# Fixture (copied verbatim from test_kanban_boards.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with no prior kanban state.

    The autouse hermetic conftest already nukes credentials + TZ; this
    fixture layers a per-test HERMES_HOME plus a path-init cache reset
    so each test sees a truly empty board set.
    """
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(var, raising=False)
    # Also reset hermes_constants cache so get_default_hermes_root() re-reads.
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    # Kanban module-level init cache must not leak between tests.
    kb._INITIALIZED_PATHS.clear()
    return home


# ---------------------------------------------------------------------------
# CLI helper (copied verbatim from test_kanban_boards.py)
# ---------------------------------------------------------------------------

def _cli(args: list[str], env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Run ``hermes kanban …`` with PYTHONPATH pinned to the worktree."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_WORKTREE)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban"] + args,
        env=env,
        capture_output=True,
        text=True,
        cwd=str(_WORKTREE),
        timeout=30,
    )


def _links(conn) -> set[tuple[str, str]]:
    """The board's whole parent/child edge set, read straight from the store."""
    return {
        (r["parent_id"], r["child_id"])
        for r in conn.execute("SELECT parent_id, child_id FROM task_links")
    }


def _kinds(conn, task_id: str) -> list[str]:
    return [
        r["kind"]
        for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
        )
    ]


# ---------------------------------------------------------------------------
# Per-card move across boards (``hermes kanban boards move``)
# ---------------------------------------------------------------------------

class TestBoardMove:
    def _seed(self, src: str = "src", dst: str = "dst") -> None:
        kb.create_board(src)
        kb.create_board(dst)
        # Give the target a DB + one pre-existing task so a target backup exists.
        with kbc.connect(board=dst) as conn:
            kb.create_task(conn, title="dst-existing", assignee="dev")

    def _diamond(self):
        """A -> B -> C plus D hanging off B (all four are one link component).

        Returns ``(a, b, c, d, edges)``. Four cards, three edges; the requested
        card's link-closed set is the whole thing.
        """
        with kbc.connect(board="src") as conn:
            a = kb.create_task(conn, title="A", assignee="dev")
            b = kb.create_task(conn, title="B", assignee="dev")
            c = kb.create_task(conn, title="C", assignee="dev")
            d = kb.create_task(conn, title="D", assignee="dev")
            kb.link_tasks(conn, a, b)
            kb.link_tasks(conn, b, c)
            kb.link_tasks(conn, b, d)
            edges = _links(conn)
        assert edges == {(a, b), (b, c), (b, d)}
        return a, b, c, d, edges


    def test_move_carries_relations_and_audits(self, fresh_home, tmp_path):
        self._seed()
        with kbc.connect(board="src") as conn:
            tid = kb.create_task(conn, title="move me", assignee="dev")
            kb.add_comment(conn, tid, "alice", "first comment")
            blob = tmp_path / "note.txt"
            blob.write_text("hello", encoding="utf-8")
            kb.add_attachment(
                conn, tid, filename="note.txt", stored_path=str(blob),
                content_type="text/plain", size=blob.stat().st_size, uploaded_by="tester",
            )
        res = kanban_move.move_task(tid, "dst", source_slug="src")
        new_id = res["to_task_id"]
        assert res["from_board"] == "src" and res["to_board"] == "dst"
        assert res["counts"]["comments"] == 1
        assert res["counts"]["attachments"] == 1
        assert res["counts"]["tasks"] == 1
        assert res["moved_task_ids"] == [tid]
        assert res["link_count"] == 0
        assert "severed_links" not in res
        # source is emptied; tombstone moved_out remains on the gone id.
        with kbc.connect(board="src") as conn:
            assert kb.get_task(conn, tid) is None
            assert kb.list_tasks(conn) == []
            kinds = [r["kind"] for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,))]
            assert "moved_out" in kinds
        # destination has the card + relations + moved_in audit.
        with kbc.connect(board="dst") as conn:
            moved = kb.get_task(conn, new_id)
            assert moved is not None and moved.title == "move me"
            assert [c.body for c in kb.list_comments(conn, new_id)] == ["first comment"]
            atts = kb.list_attachments(conn, new_id)
            assert [a.filename for a in atts] == ["note.txt"]
            assert Path(atts[0].stored_path).is_file()
            assert Path(atts[0].stored_path).read_text(encoding="utf-8") == "hello"
            kinds = [r["kind"] for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (new_id,))]
            assert "moved_in" in kinds
        assert res["backups"]["source"] and Path(res["backups"]["source"]).is_file()
        assert res["backups"]["target"] and Path(res["backups"]["target"]).is_file()


    def test_move_preserves_id_when_free(self, fresh_home):
        self._seed()
        with kbc.connect(board="src") as conn:
            tid = kb.create_task(conn, title="keep", assignee="dev")
        res = kanban_move.move_task(tid, "dst", source_slug="src")
        assert res["to_task_id"] == tid


    def test_move_refuses_when_links_exist(self, fresh_home):
        """No path may sever an edge: a linked card is refused, not unlinked."""
        self._seed()
        a, b, c, d, edges = self._diamond()

        with pytest.raises(ValueError) as excinfo:
            kanban_move.move_task(a, "dst", source_slug="src")
        msg = str(excinfo.value)
        assert "3" in msg, msg                     # three other cards in the set
        assert "--with-links" in msg, msg

        # Nothing moved, nothing severed, on either board.
        with kbc.connect(board="src") as conn:
            assert {t.id for t in kb.list_tasks(conn)} == {a, b, c, d}
            assert _links(conn) == edges
        with kbc.connect(board="dst") as conn:
            assert [t.title for t in kb.list_tasks(conn)] == ["dst-existing"]
            assert _links(conn) == set()


    def test_move_refuses_linked_card_even_when_asked_for_the_child(self, fresh_home):
        """The refusal is about the whole component, whichever end you name."""
        self._seed()
        a, b, c, d, edges = self._diamond()

        with pytest.raises(ValueError, match="--with-links"):
            kanban_move.move_task(c, "dst", source_slug="src")

        with kbc.connect(board="src") as conn:
            assert _links(conn) == edges


    def test_move_with_links_moves_whole_component(self, fresh_home):
        """A/B/C keep their edges, D travels with them, nothing dangles."""
        self._seed()
        a, b, c, d, edges = self._diamond()

        res = kanban_move.move_task(b, "dst", source_slug="src", with_links=True)

        assert sorted(res["moved_task_ids"]) == sorted([a, b, c, d])
        assert res["link_count"] == 3
        assert res["to_task_id"] == b and res["from_task_id"] == b
        assert res["counts"]["tasks"] == 4
        assert "severed_links" not in res

        with kbc.connect(board="dst") as conn:
            # The target graph for the set is identical to the source's, and D
            # was carried rather than orphaned.
            assert _links(conn) == edges
            assert {t.title for t in kb.list_tasks(conn)} == {"A", "B", "C", "D", "dst-existing"}
            for tid in (a, b, c, d):
                assert "moved_in" in _kinds(conn, tid)

        with kbc.connect(board="src") as conn:
            assert kb.list_tasks(conn) == []
            # Neither board may retain a dangling link row.
            assert _links(conn) == set()
            for tid in (a, b, c, d):
                assert "moved_out" in _kinds(conn, tid)


    def test_move_with_links_refuses_while_a_linked_card_is_running(self, fresh_home):
        """All-or-nothing: one running member blocks the whole component."""
        self._seed()
        a, b, c, d, edges = self._diamond()
        with kbc.connect(board="src") as conn:
            # `a` is the only claimable card (b/c/d are blocked by their parent),
            # and it is NOT the card we ask to move.
            assert kb.claim_task(conn, a) is not None

        with pytest.raises(ValueError, match="running"):
            kanban_move.move_task(b, "dst", source_slug="src", with_links=True)

        with kbc.connect(board="src") as conn:
            assert {t.id for t in kb.list_tasks(conn)} == {a, b, c, d}
            assert _links(conn) == edges
        with kbc.connect(board="dst") as conn:
            assert kb.get_task(conn, b) is None and kb.get_task(conn, c) is None
            assert _links(conn) == set()


    def test_move_with_links_refuses_while_a_linked_card_has_a_live_run(self, fresh_home):
        """A live run row (status=ready, open run) also blocks the component."""
        self._seed()
        with kbc.connect(board="src") as conn:
            a = kb.create_task(conn, title="A", assignee="dev")
            b = kb.create_task(conn, title="B", assignee="dev")
            kb.link_tasks(conn, a, b)
            with kbc.write_txn(conn):
                conn.execute(
                    "INSERT INTO task_runs (task_id, status, started_at) VALUES (?, 'running', ?)",
                    (b, 1),
                )

        with pytest.raises(ValueError, match="running"):
            kanban_move.move_task(a, "dst", source_slug="src", with_links=True)

        with kbc.connect(board="src") as conn:
            assert kb.get_task(conn, a) is not None and kb.get_task(conn, b) is not None


    def test_move_refuses_running(self, fresh_home):
        """A running (claimed) card is never moved, links or not."""
        self._seed()
        with kbc.connect(board="src") as conn:
            tid = kb.create_task(conn, title="busy", assignee="dev")
            assert kb.claim_task(conn, tid) is not None

        with pytest.raises(ValueError, match="running"):
            kanban_move.move_task(tid, "dst", source_slug="src")

        with kbc.connect(board="src") as conn:
            assert kb.get_task(conn, tid) is not None
        with kbc.connect(board="dst") as conn:
            assert kb.get_task(conn, tid) is None

    def test_move_refuses_foreign_id_collision(self, fresh_home):
        """A target row with the same id that is NOT our interrupted move."""
        kb.create_board("src")
        kb.create_board("dst")
        with kbc.connect(board="src") as conn:
            tid = kb.create_task(conn, title="orig", assignee="dev")
        with kbc.connect(board="dst") as conn:
            other = kb.create_task(conn, title="occupant", assignee="dev")
            with kbc.write_txn(conn):
                conn.execute("UPDATE tasks SET id = ? WHERE id = ?", (tid, other))

        with pytest.raises(ValueError, match="already exists") as excinfo:
            kanban_move.move_task(tid, "dst", source_slug="src")
        assert tid in str(excinfo.value)

        # Both sides are untouched: no silent id remap, no overwrite.
        with kbc.connect(board="src") as conn:
            assert (t := kb.get_task(conn, tid)) is not None and t.title == "orig"
        with kbc.connect(board="dst") as conn:
            assert [t.title for t in kb.list_tasks(conn)] == ["occupant"]


    def test_move_scrubs_local_state_and_parks_a_dir_card(self, fresh_home):
        """Item 6: machine-local state is scrubbed; dir cards park in triage."""
        self._seed()
        with kbc.connect(board="src") as conn:
            tid = kb.create_task(
                conn, title="local", assignee="dev",
                workspace_kind="worktree", workspace_path="/tmp/local-ws", branch_name="b",
            )
            with kbc.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET worker_pid = 4242, session_id = 'sess-1',"
                    " last_failure_error = 'boom', project_id = 'proj-1',"
                    " last_heartbeat_at = 123.0 WHERE id = ?",
                    (tid,),
                )

        res = kanban_move.move_task(tid, "dst", source_slug="src")

        assert res["parked_triage"] is True
        assert any("triage" in w for w in res["warnings"])
        with kbc.connect(board="dst") as conn:
            moved = kb.get_task(conn, tid)
            assert moved is not None and moved.id == tid
            assert moved.status == "triage"
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
            for col in (
                "claim_lock", "claim_expires", "worker_pid", "current_run_id",
                "last_heartbeat_at", "session_id", "workspace_path", "branch_name",
                "project_id", "last_failure_error",
            ):
                assert row[col] is None, col

    def test_move_resumes_after_interrupted_target_commit(self, fresh_home, monkeypatch):
        """Crash between the target commit and the source delete: rerun finishes it."""
        self._seed()
        with kbc.connect(board="src") as conn:
            a = kb.create_task(conn, title="A", assignee="dev")
            b = kb.create_task(conn, title="B", assignee="dev")
            kb.link_tasks(conn, a, b)
            kb.add_comment(conn, a, "alice", "carry me")

        real_remove = kanban_move._remove_from_source

        def _crash(*args, **kwargs):
            raise RuntimeError("simulated crash before the source delete")

        monkeypatch.setattr(kanban_move, "_remove_from_source", _crash)
        with pytest.raises(RuntimeError, match="simulated crash"):
            kanban_move.move_task(a, "dst", source_slug="src", with_links=True)
        monkeypatch.setattr(kanban_move, "_remove_from_source", real_remove)

        # Interrupted state: the target committed, the source still has the set.
        with kbc.connect(board="dst") as conn:
            assert {t.title for t in kb.list_tasks(conn)} == {"A", "B", "dst-existing"}
            assert _links(conn) == {(a, b)}
            assert "moved_in" in _kinds(conn, a)
        with kbc.connect(board="src") as conn:
            assert {t.id for t in kb.list_tasks(conn)} == {a, b}
            assert _links(conn) == {(a, b)}

        # Simply re-running the same command completes the move — no duplicates.
        res = kanban_move.move_task(a, "dst", source_slug="src", with_links=True)

        assert sorted(res["moved_task_ids"]) == sorted([a, b])
        assert res["to_task_id"] == a
        with kbc.connect(board="src") as conn:
            assert kb.list_tasks(conn) == []
            assert _links(conn) == set()
        with kbc.connect(board="dst") as conn:
            assert {t.title for t in kb.list_tasks(conn)} == {"A", "B", "dst-existing"}
            assert _links(conn) == {(a, b)}
            assert _kinds(conn, a).count("moved_in") == 1
            assert _kinds(conn, b).count("moved_in") == 1
            assert kb.get_task(conn, a).title == "A"
            assert [c.body for c in kb.list_comments(conn, a)] == ["carry me"]


    def test_move_via_cli(self, tmp_path):
        env = {"HERMES_HOME": str(tmp_path)}
        assert _cli(["boards", "create", "src"], env_extra=env).returncode == 0
        assert _cli(["boards", "create", "dst"], env_extra=env).returncode == 0
        r = _cli(["--board", "src", "create", "Move Me", "--assignee", "dev"], env_extra=env)
        assert r.returncode == 0, r.stderr
        tid = json.loads(_cli(["--board", "src", "list", "--json"], env_extra=env).stdout)[0]["id"]
        r = _cli(["boards", "move", tid, "--from", "src", "--to", "dst", "--json"], env_extra=env)
        assert r.returncode == 0, r.stderr
        data = json.loads(r.stdout)
        assert data["from_task_id"] == tid
        assert data["to_board"] == "dst"
        assert data["moved_task_ids"] == [tid]
        assert data["link_count"] == 0
        assert "severed_links" not in data
        src_list = json.loads(_cli(["--board", "src", "list", "--json"], env_extra=env).stdout)
        dst_list = json.loads(_cli(["--board", "dst", "list", "--json"], env_extra=env).stdout)
        assert src_list == []
        assert [t["title"] for t in dst_list] == ["Move Me"]


    def test_boards_move_with_links_via_cli(self, tmp_path):
        """``boards move <id> --with-links --json`` moves the whole component."""
        env = {"HERMES_HOME": str(tmp_path)}
        assert _cli(["boards", "create", "src"], env_extra=env).returncode == 0
        assert _cli(["boards", "create", "dst"], env_extra=env).returncode == 0
        ids = {}
        for title in ("A", "B", "C"):
            r = _cli(["--board", "src", "create", title, "--assignee", "dev"], env_extra=env)
            assert r.returncode == 0, r.stderr
        for t in json.loads(_cli(["--board", "src", "list", "--json"], env_extra=env).stdout):
            ids[t["title"]] = t["id"]
        a, b, c = ids["A"], ids["B"], ids["C"]
        for parent, child in ((a, b), (b, c)):
            r = _cli(["--board", "src", "link", parent, child], env_extra=env)
            assert r.returncode == 0, r.stderr

        r = _cli(
            ["boards", "move", a, "--from", "src", "--to", "dst", "--with-links", "--json"],
            env_extra=env,
        )
        assert r.returncode == 0, r.stderr
        data = json.loads(r.stdout)
        assert sorted(data["moved_task_ids"]) == sorted([a, b, c])
        assert data["to_task_id"] == a
        assert data["link_count"] == 2
        assert "severed_links" not in data

        src_list = json.loads(_cli(["--board", "src", "list", "--json"], env_extra=env).stdout)
        dst_titles = {
            t["title"] for t in json.loads(_cli(["--board", "dst", "list", "--json"], env_extra=env).stdout)
        }
        assert src_list == []
        assert dst_titles == {"A", "B", "C"}


    def test_boards_move_refuses_linked_card_via_cli(self, tmp_path):
        """The refusal is actionable from the CLI, and nothing moves."""
        env = {"HERMES_HOME": str(tmp_path)}
        assert _cli(["boards", "create", "src"], env_extra=env).returncode == 0
        assert _cli(["boards", "create", "dst"], env_extra=env).returncode == 0
        for title in ("A", "B"):
            r = _cli(["--board", "src", "create", title, "--assignee", "dev"], env_extra=env)
            assert r.returncode == 0, r.stderr
        ids = {
            t["title"]: t["id"]
            for t in json.loads(_cli(["--board", "src", "list", "--json"], env_extra=env).stdout)
        }
        assert _cli(["--board", "src", "link", ids["A"], ids["B"]], env_extra=env).returncode == 0

        r = _cli(["boards", "move", ids["A"], "--from", "src", "--to", "dst"], env_extra=env)
        assert r.returncode != 0
        assert "--with-links" in (r.stderr + r.stdout)

        src_list = json.loads(_cli(["--board", "src", "list", "--json"], env_extra=env).stdout)
        dst_list = json.loads(_cli(["--board", "dst", "list", "--json"], env_extra=env).stdout)
        assert {t["title"] for t in src_list} == {"A", "B"}
        assert dst_list == []


# ---------------------------------------------------------------------------
# Declared, audited severance (``--sever-edge PARENT:CHILD`` / ``--sever-reason``)
#
# The invariant stays: no move severs an edge on its own. But the live ops case
# is real — a whole initiative component whose only remaining tie is a gating
# edge pointing at a card that has to stay on the ops board — so severance is
# available *only* where the operator declares it, edge by edge, with a recorded
# reason, and only when the cut cannot auto-promote a card sitting in a
# dispatcher pool lane (``todo`` / ``ready`` / ``triage``). That is exactly the
# gate ``~/.hermes/kanban/migrations/migrate_apr25.py`` applies before it cuts.
# ---------------------------------------------------------------------------

_SEVER_REASON = "the gating card stays behind on the source board"


def _seed_boards() -> None:
    """``src`` + ``dst``, the target pre-seeded so a target backup exists."""
    kb.create_board("src")
    kb.create_board("dst")
    with kbc.connect(board="dst") as conn:
        kb.create_task(conn, title="dst-existing", assignee="dev")


def _card(title: str, *, status: str | None = None) -> str:
    """A ``src`` card, optionally forced into a column (synthetic ids only)."""
    with kbc.connect(board="src") as conn:
        tid = kb.create_task(conn, title=title, assignee="dev")
        if status is not None:
            with kbc.write_txn(conn):
                conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
    return tid


def _link(parent_id: str, child_id: str) -> None:
    with kbc.connect(board="src") as conn:
        kb.link_tasks(conn, parent_id, child_id)


def _status(task_id: str) -> str | None:
    with kbc.connect(board="src") as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return row["status"] if row else None


def _payloads(conn, task_id: str, kind: str) -> list[dict]:
    """Decoded payloads of every ``kind`` event on a card, in event order."""
    return [
        json.loads(r["payload"]) if r["payload"] else {}
        for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
            (task_id, kind),
        )
    ]


def _assert_untouched(a: str, b: str, *extra: str) -> None:
    """Nothing moved, nothing severed: both cards and their edge are intact."""
    with kbc.connect(board="src") as conn:
        assert {t.id for t in kb.list_tasks(conn)} == {a, b, *extra}
        assert _links(conn) == {(a, b)} if not extra else _links(conn)
    with kbc.connect(board="dst") as conn:
        assert [t.title for t in kb.list_tasks(conn)] == ["dst-existing"]
        assert _links(conn) == set()


class TestBoardMoveSeverance:
    """``--sever-edge`` is the only path that may cut an edge."""

    def test_sever_without_a_reason_refuses(self, fresh_home):
        """Severance is never silent: no reason, no cut."""
        _seed_boards()
        a = _card("A")
        b = _card("B")
        _link(a, b)

        with pytest.raises(ValueError, match="--sever-reason"):
            kanban_move.move_task(a, "dst", source_slug="src", sever_edges=[(a, b)])

        with kbc.connect(board="src") as conn:
            assert {t.id for t in kb.list_tasks(conn)} == {a, b}
            assert _links(conn) == {(a, b)}
        with kbc.connect(board="dst") as conn:
            assert [t.title for t in kb.list_tasks(conn)] == ["dst-existing"]

    def test_sever_refuses_an_undeclared_leaving_edge(self, fresh_home):
        """Every edge the move would leave behind has to be declared."""
        _seed_boards()
        a = _card("A")
        b = _card("B")
        c = _card("C")
        _link(a, b)
        _link(b, c)

        with pytest.raises(ValueError) as excinfo:
            kanban_move.move_task(
                a, "dst", source_slug="src",
                sever_edges=[(b, c)], sever_reason=_SEVER_REASON,
            )
        msg = str(excinfo.value)
        assert f"{a} -> {b}" in msg, msg
        assert f"--sever-edge {a}:{b}" in msg, msg  # the exact fix

        with kbc.connect(board="src") as conn:
            assert {t.id for t in kb.list_tasks(conn)} == {a, b, c}
            assert _links(conn) == {(a, b), (b, c)}
        with kbc.connect(board="dst") as conn:
            assert [t.title for t in kb.list_tasks(conn)] == ["dst-existing"]
            assert _links(conn) == set()

    def test_sever_refuses_a_declared_edge_that_is_not_a_link(self, fresh_home):
        """A typo'd edge is refused by name, not waved through."""
        _seed_boards()
        a = _card("A")
        b = _card("B")
        c = _card("C")
        _link(a, b)

        with pytest.raises(ValueError) as excinfo:
            kanban_move.move_task(
                a, "dst", source_slug="src",
                sever_edges=[(a, b), (b, c)], sever_reason=_SEVER_REASON,
            )
        msg = str(excinfo.value)
        assert f"{b} -> {c}" in msg, msg
        assert "not a" in msg, msg
        with kbc.connect(board="src") as conn:
            assert _links(conn) == {(a, b)}

    def test_sever_refuses_a_declared_edge_that_travels_with_the_set(self, fresh_home):
        """A redundant path keeps both ends inside the set — nothing is severed."""
        _seed_boards()
        a = _card("A")
        b = _card("B")
        c = _card("C")
        d = _card("D")
        _link(a, b)
        _link(a, c)
        _link(b, d)
        _link(c, d)

        with pytest.raises(ValueError) as excinfo:
            kanban_move.move_task(
                a, "dst", source_slug="src", with_links=True,
                sever_edges=[(c, d)], sever_reason=_SEVER_REASON,
            )
        msg = str(excinfo.value)
        assert f"{c} -> {d}" in msg, msg
        assert "travel" in msg or "move set" in msg, msg
        with kbc.connect(board="src") as conn:
            assert _links(conn) == {(a, b), (a, c), (b, d), (c, d)}

    def test_sever_refuses_when_the_leaving_child_is_in_a_pool_lane(self, fresh_home):
        """Cutting a parent's edge can satisfy a child's deps -> auto-promotion."""
        _seed_boards()
        p = _card("P")
        child = _card("Child")
        _link(p, child)  # a non-done parent parks the child in ``todo``
        assert _status(child) == "todo"

        with pytest.raises(ValueError) as excinfo:
            kanban_move.move_task(
                p, "dst", source_slug="src",
                sever_edges=[(p, child)], sever_reason=_SEVER_REASON,
            )
        msg = str(excinfo.value)
        assert child in msg and "todo" in msg, msg
        assert "auto-promot" in msg, msg

        with kbc.connect(board="src") as conn:
            assert {t.id for t in kb.list_tasks(conn)} == {p, child}
            assert _links(conn) == {(p, child)}

    def test_sever_refuses_when_the_moved_child_would_land_parentless(self, fresh_home):
        """The mirror hazard: the moved child loses its parent on the target."""
        _seed_boards()
        p = _card("P")
        child = _card("Child")
        _link(p, child)
        assert _status(child) == "todo"

        with pytest.raises(ValueError) as excinfo:
            kanban_move.move_task(
                child, "dst", source_slug="src",
                sever_edges=[(p, child)], sever_reason=_SEVER_REASON,
            )
        msg = str(excinfo.value)
        assert child in msg and "auto-promot" in msg, msg

        with kbc.connect(board="src") as conn:
            assert {t.id for t in kb.list_tasks(conn)} == {p, child}
            assert _links(conn) == {(p, child)}

    def test_sever_moves_the_card_and_audits_both_boards(self, fresh_home):
        """One declared cut: the card moves, the edge goes, both sides audited."""
        _seed_boards()
        p = _card("P")
        keep = _card("Keep", status="done")  # out of every pool lane
        _link(p, keep)

        res = kanban_move.move_task(
            p, "dst", source_slug="src",
            sever_edges=[(p, keep)], sever_reason=_SEVER_REASON,
        )

        assert res["moved_task_ids"] == [p]
        assert res["to_task_id"] == p
        assert res["link_count"] == 0
        assert res["severed_links"] == [[p, keep]]

        with kbc.connect(board="src") as conn:
            assert kb.get_task(conn, p) is None  # moved out
            assert _links(conn) == set()  # no dangling link row left behind
            assert kb.get_task(conn, keep) is not None  # stays put
            (cut,) = _payloads(conn, keep, "link_severed")
            # The source event describes the card that stays behind.
            assert cut["role"] == "child"
            assert cut["moved_task_id"] == p
            assert cut["moved_to_board"] == "dst"
            assert cut["reason"] == _SEVER_REASON
        with kbc.connect(board="dst") as conn:
            assert kb.get_task(conn, p) is not None
            assert _links(conn) == set()
            (cut,) = _payloads(conn, p, "link_severed")
            # The target event describes the card that arrived.
            assert cut["role"] == "parent"
            assert cut["outside_task_id"] == keep
            assert cut["from_board"] == "src"
            assert cut["reason"] == _SEVER_REASON

    def test_sever_with_links_cuts_the_set_at_the_declared_edge(self, fresh_home):
        """``--with-links`` plus a declared cut moves the near side only."""
        _seed_boards()
        a = _card("A")
        b = _card("B")
        c = _card("C")
        keep = _card("Keep", status="done")
        _link(a, b)
        _link(b, c)
        _link(c, keep)

        res = kanban_move.move_task(
            a, "dst", source_slug="src", with_links=True,
            sever_edges=[(c, keep)], sever_reason=_SEVER_REASON,
        )

        assert sorted(res["moved_task_ids"]) == sorted([a, b, c])
        assert res["link_count"] == 2
        assert res["severed_links"] == [[c, keep]]

        with kbc.connect(board="dst") as conn:
            assert _links(conn) == {(a, b), (b, c)}
            for tid in (a, b, c):
                assert "moved_in" in _kinds(conn, tid)
            assert kb.get_task(conn, keep) is None
        with kbc.connect(board="src") as conn:
            assert {t.id for t in kb.list_tasks(conn)} == {keep}
            assert _links(conn) == set()
            (cut,) = _payloads(conn, keep, "link_severed")
            assert cut["moved_task_id"] == c
            assert cut["moved_to_board"] == "dst"

    # -- CLI surface ------------------------------------------------------

    def test_sever_via_cli_records_and_reports(self, tmp_path, monkeypatch):
        env, a, b = _cli_seeded(tmp_path, monkeypatch)

        r = _cli(
            [
                "boards", "move", a, "--from", "src", "--to", "dst",
                "--sever-edge", f"{a}:{b}",
                "--sever-reason", _SEVER_REASON,
                "--json",
            ],
            env_extra=env,
        )
        assert r.returncode == 0, r.stderr

        data = json.loads(r.stdout)
        assert data["moved_task_ids"] == [a]
        assert data["severed_links"] == [[a, b]]
        assert data["link_count"] == 0

        src_titles = {
            t["title"]
            for t in json.loads(_cli(["--board", "src", "list", "--json"], env_extra=env).stdout)
        }
        dst_titles = {
            t["title"]
            for t in json.loads(_cli(["--board", "dst", "list", "--json"], env_extra=env).stdout)
        }
        assert src_titles == {"B"}
        assert dst_titles == {"A"}

    def test_sever_via_cli_requires_reason(self, tmp_path, monkeypatch):
        env, a, b = _cli_seeded(tmp_path, monkeypatch)

        r = _cli(
            ["boards", "move", a, "--from", "src", "--to", "dst", "--sever-edge", f"{a}:{b}"],
            env_extra=env,
        )
        assert r.returncode != 0
        assert "--sever-reason" in (r.stderr + r.stdout)

        src_titles = {
            t["title"]
            for t in json.loads(_cli(["--board", "src", "list", "--json"], env_extra=env).stdout)
        }
        assert src_titles == {"A", "B"}

    def test_sever_via_cli_rejects_a_malformed_edge_spec(self, tmp_path, monkeypatch):
        env, a, _b = _cli_seeded(tmp_path, monkeypatch)

        r = _cli(
            [
                "boards", "move", a, "--from", "src", "--to", "dst",
                "--sever-edge", "not-an-edge",
                "--sever-reason", _SEVER_REASON,
            ],
            env_extra=env,
        )
        assert r.returncode != 0
        assert "--sever-edge" in (r.stderr + r.stdout)
        assert "PARENT:CHILD" in (r.stderr + r.stdout)

    def test_move_still_reports_no_severance_when_none_was_declared(self, fresh_home):
        """The key is absent unless a cut happened (unchanged callers)."""
        _seed_boards()
        lone = _card("Lone")

        res = kanban_move.move_task(lone, "dst", source_slug="src")

        assert "severed_links" not in res


def _cli_seeded(tmp_path, monkeypatch) -> tuple[dict, str, str]:
    """CLI-built ``src``/``dst`` with an ``A -> B`` link.

    ``B`` is parked at ``done`` (via the same store the CLI writes) so the
    declared cut cannot auto-promote it: the pool-lane gate would otherwise
    refuse, which is the point of that gate.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()

    env = {"HERMES_HOME": str(tmp_path)}
    assert _cli(["boards", "create", "src"], env_extra=env).returncode == 0
    assert _cli(["boards", "create", "dst"], env_extra=env).returncode == 0
    for title in ("A", "B"):
        r = _cli(["--board", "src", "create", title, "--assignee", "dev"], env_extra=env)
        assert r.returncode == 0, r.stderr
    ids = {
        t["title"]: t["id"]
        for t in json.loads(_cli(["--board", "src", "list", "--json"], env_extra=env).stdout)
    }
    a, b = ids["A"], ids["B"]
    assert _cli(["--board", "src", "link", a, b], env_extra=env).returncode == 0
    with kbc.connect(board="src") as conn:
        with kbc.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (b,))
    return env, a, b
