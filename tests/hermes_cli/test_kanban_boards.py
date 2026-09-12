"""Tests for the multi-board kanban layer (``hermes kanban boards …``).

Covers the pieces added when boards became a first-class concept:

* Slug validation and normalisation.
* Path resolution for ``default`` (legacy ``<root>/kanban.db``) vs
  named boards (``<root>/kanban/boards/<slug>/kanban.db``).
* Current-board persistence via ``<root>/kanban/current`` and
  ``HERMES_KANBAN_BOARD`` env var.
* ``connect(board=)`` isolation — writes on one board don't leak.
* ``create_board`` / ``list_boards`` / ``remove_board`` round trip.
* CLI surface: ``hermes kanban boards list/create/switch/rm``.
* ``_default_spawn`` injects ``HERMES_KANBAN_BOARD`` into worker env.
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
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_move


# ---------------------------------------------------------------------------
# Fixture
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
# Slug validation
# ---------------------------------------------------------------------------

class TestSlugValidation:
    @pytest.mark.parametrize("good", [
        "default", "atm10-server", "hermes-agent", "proj_1", "a",
        "very-long-but-still-ok-slug-with-hyphens-and-numbers-1234",
    ])
    def test_accepts_valid(self, good):
        assert kb._normalize_board_slug(good) == good


    def test_empty_returns_none(self):
        assert kb._normalize_board_slug(None) is None
        assert kb._normalize_board_slug("") is None
        assert kb._normalize_board_slug("   ") is None


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

class TestPathResolution:
    def test_default_board_legacy_path(self, fresh_home):
        """The default board's DB lives at ``<root>/kanban.db`` for back-compat."""
        assert kb.kanban_db_path() == fresh_home / "kanban.db"
        assert kb.kanban_db_path(board="default") == fresh_home / "kanban.db"

    def test_named_board_under_boards_dir(self, fresh_home):
        p = kb.kanban_db_path(board="atm10-server")
        assert p == fresh_home / "kanban" / "boards" / "atm10-server" / "kanban.db"


    def test_env_var_db_override_still_wins(self, fresh_home, tmp_path, monkeypatch):
        """``HERMES_KANBAN_DB`` pins a store that belongs to no board outright."""
        forced = tmp_path / "custom.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(forced))
        assert kb.kanban_db_path() == forced
        assert kb.kanban_db_path(board="ignored") == forced


    def test_env_var_db_override_yields_to_an_explicit_other_board(
        self, fresh_home, monkeypatch,
    ):
        """A pin that belongs to ``default`` answers for ``default`` only —
        otherwise ``--board <other>`` silently reads the pinned store."""
        kb.create_board("research")
        monkeypatch.setenv("HERMES_KANBAN_DB", str(fresh_home / "kanban.db"))
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
        assert kb.kanban_db_path(board="research") == (
            fresh_home / "kanban" / "boards" / "research" / "kanban.db"
        )
        assert kb.kanban_db_path(board="default") == fresh_home / "kanban.db"



# ---------------------------------------------------------------------------
# Current-board resolution
# ---------------------------------------------------------------------------

class TestCurrentBoard:



    def test_stale_file_pointer_falls_back_to_default(self, fresh_home):
        current = fresh_home / "kanban" / "current"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.write_text("missing-board\n", encoding="utf-8")

        assert kb.get_current_board() == "default"
        assert not kb.board_exists("missing-board")
        assert [b["slug"] for b in kb.list_boards()] == ["default"]



    def test_kanban_db_path_reads_current(self, fresh_home):
        """kanban_db_path() with no args respects the on-disk pointer."""
        kb.create_board("my-proj")
        kb.set_current_board("my-proj")
        expected = fresh_home / "kanban" / "boards" / "my-proj" / "kanban.db"
        assert kb.kanban_db_path() == expected


# ---------------------------------------------------------------------------
# Board CRUD
# ---------------------------------------------------------------------------

class TestBoardCRUD:






    @pytest.mark.parametrize("archive", [True, False])
    def test_remove_clears_init_cache_for_recreated_db(self, fresh_home, archive):
        # Regression for #23833: poll loops that call connect(board=slug) right
        # after remove_board() recreate an empty kanban.db at the same path
        # (connect() does mkdir(exist_ok=True)). If _INITIALIZED_PATHS still
        # contains the resolved path, the CREATE TABLE pass is skipped and
        # downstream readers hit `no such table: task_events`.
        kb.create_board("recycle")
        # First connect populates _INITIALIZED_PATHS for this DB.
        with kbc.connect(board="recycle") as conn:
            kb.create_task(conn, title="t1", assignee="dev")
        db_path = kb.board_dir("recycle") / "kanban.db"
        assert str(db_path.resolve()) in kb._INITIALIZED_PATHS

        kb.remove_board("recycle", archive=archive)
        # remove_board must drop the cache entry so a re-create through
        # connect() gets a fresh schema-init pass.
        assert str(db_path.resolve()) not in kb._INITIALIZED_PATHS

        # Simulate the event-stream poll: re-open the same slug. connect()
        # recreates the directory + empty .db; the schema must be re-applied.
        with kbc.connect(board="recycle") as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert "task_events" in tables
        assert "tasks" in tables

    def test_rename_updates_metadata(self, fresh_home):
        kb.create_board("slug-immutable")
        kb.write_board_metadata("slug-immutable", name="New Display Name")
        assert kb.read_board_metadata("slug-immutable")["name"] == "New Display Name"
        # Slug must not change.
        assert kb.board_exists("slug-immutable")


# ---------------------------------------------------------------------------
# Connection isolation
# ---------------------------------------------------------------------------

class TestConnectionIsolation:
    def test_tasks_do_not_leak_across_boards(self, fresh_home):
        kb.create_board("alpha")
        kb.create_board("beta")

        with kbc.connect(board="alpha") as conn:
            kb.create_task(conn, title="alpha-task-1", assignee="dev")
            kb.create_task(conn, title="alpha-task-2", assignee="dev")

        with kbc.connect(board="beta") as conn:
            kb.create_task(conn, title="beta-only", assignee="dev")

        with kbc.connect(board="alpha") as conn:
            a = kb.list_tasks(conn)
        with kbc.connect(board="beta") as conn:
            b = kb.list_tasks(conn)
        with kbc.connect(board="default") as conn:
            d = kb.list_tasks(conn)

        assert {t.title for t in a} == {"alpha-task-1", "alpha-task-2"}
        assert {t.title for t in b} == {"beta-only"}
        assert d == []

    def test_connect_without_args_uses_current(self, fresh_home):
        kb.create_board("curr")
        kb.set_current_board("curr")
        with kbc.connect() as conn:
            kb.create_task(conn, title="implicit", assignee="x")
        with kbc.connect(board="curr") as conn:
            tasks = kb.list_tasks(conn)
        assert [t.title for t in tasks] == ["implicit"]

    def test_connect_env_var_overrides_current(self, fresh_home, monkeypatch):
        kb.create_board("persist")
        kb.create_board("envwin")
        kb.set_current_board("persist")
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "envwin")
        with kbc.connect() as conn:
            kb.create_task(conn, title="via-env", assignee="x")
        with kbc.connect(board="envwin") as conn:
            assert [t.title for t in kb.list_tasks(conn)] == ["via-env"]
        with kbc.connect(board="persist") as conn:
            assert kb.list_tasks(conn) == []


# ---------------------------------------------------------------------------
# Worker spawn env injection
# ---------------------------------------------------------------------------

class TestWorkerSpawnEnv:
    """Ensure the dispatcher pins ``HERMES_KANBAN_BOARD`` / DB / workspaces on spawn.

    We monkey-patch ``subprocess.Popen`` to capture the child env without
    actually spawning anything.
    """

    def test_default_spawn_sets_env_vars(self, fresh_home, monkeypatch):
        captured = {}

        class FakeProc:
            pid = 12345

        def fake_popen(cmd, *args, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env", {})
            return FakeProc()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        kb.create_board("spawntest")

        task = kb.Task(
            id="t_abc",
            title="worker test",
            body=None,
            assignee="teknium",
            status="ready",
            priority=0,
            created_by="user",
            created_at=0,
            started_at=None,
            completed_at=None,
            workspace_kind="scratch",
            workspace_path=None,
            claim_lock=None,
            claim_expires=None,
            tenant=None,
        )

        kbd._default_spawn(task, str(fresh_home / "ws"), board="spawntest")

        env = captured["env"]
        assert env["HERMES_KANBAN_BOARD"] == "spawntest"
        assert env["HERMES_KANBAN_TASK"] == "t_abc"
        # DB path should match the per-board DB, not the legacy default.
        expected_db = fresh_home / "kanban" / "boards" / "spawntest" / "kanban.db"
        assert env["HERMES_KANBAN_DB"] == str(expected_db)
        expected_ws = fresh_home / "kanban" / "boards" / "spawntest" / "workspaces"
        assert env["HERMES_KANBAN_WORKSPACES_ROOT"] == str(expected_ws)


# ---------------------------------------------------------------------------
# CLI surface
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


class TestCLI:
    def test_boards_list_default_only(self, tmp_path):
        env = {"HERMES_HOME": str(tmp_path)}
        res = _cli(["boards", "list", "--json"], env_extra=env)
        assert res.returncode == 0, res.stderr
        data = json.loads(res.stdout)
        slugs = [b["slug"] for b in data]
        assert slugs == ["default"]
        assert data[0]["is_current"] is True


    def test_per_board_task_isolation_via_cli(self, tmp_path):
        env = {"HERMES_HOME": str(tmp_path)}
        assert _cli(["boards", "create", "projA"], env_extra=env).returncode == 0
        assert _cli(["boards", "create", "projB"], env_extra=env).returncode == 0

        # Create one task on each via --board.
        r = _cli(["--board", "projA", "create", "Task A", "--assignee", "dev"], env_extra=env)
        assert r.returncode == 0, r.stderr
        r = _cli(["--board", "projB", "create", "Task B", "--assignee", "dev"], env_extra=env)
        assert r.returncode == 0, r.stderr

        # list on each board only shows its own.
        listA = _cli(["--board", "projA", "list", "--json"], env_extra=env)
        listB = _cli(["--board", "projB", "list", "--json"], env_extra=env)
        listD = _cli(["list", "--json"], env_extra=env)

        titlesA = [t["title"] for t in json.loads(listA.stdout)]
        titlesB = [t["title"] for t in json.loads(listB.stdout)]
        titlesD = [t["title"] for t in json.loads(listD.stdout)]

        assert titlesA == ["Task A"]
        assert titlesB == ["Task B"]
        assert titlesD == []


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


    def test_move_reassigns_id_on_collision(self, fresh_home):
        kb.create_board("src")
        kb.create_board("dst")
        with kbc.connect(board="src") as conn:
            tid = kb.create_task(conn, title="orig", assignee="dev")
        with kbc.connect(board="dst") as conn:
            other = kb.create_task(conn, title="occupant", assignee="dev")
            with kbc.write_txn(conn):
                conn.execute("UPDATE tasks SET id = ? WHERE id = ?", (tid, other))
        res = kanban_move.move_task(tid, "dst", source_slug="src")
        assert res["to_task_id"] != tid
        with kbc.connect(board="dst") as conn:
            ids = {t.id for t in kb.list_tasks(conn)}
            assert len(ids) == 2
            assert tid in ids and res["to_task_id"] in ids


    def test_move_refuses_running(self, fresh_home):
        self._seed()
        with kbc.connect(board="src") as conn:
            tid = kb.create_task(conn, title="busy", assignee="dev")
            assert kb.claim_task(conn, tid) is not None
        with pytest.raises(ValueError, match="running"):
            kanban_move.move_task(tid, "dst", source_slug="src")
        # card still on source, untouched
        with kbc.connect(board="src") as conn:
            assert kb.get_task(conn, tid) is not None


    def test_move_severs_links_and_regates_child(self, fresh_home):
        self._seed()
        with kbc.connect(board="src") as conn:
            parent = kb.create_task(conn, title="parent", assignee="dev")
            child = kb.create_task(conn, title="child", assignee="dev")
            kb.link_tasks(conn, parent, child)
            assert (c := kb.get_task(conn, child)) is not None and c.status == "todo"
        res = kanban_move.move_task(parent, "dst", source_slug="src")
        assert res["severed_links"] == 1
        with kbc.connect(board="src") as conn:
            assert kb.get_task(conn, parent) is None
            assert (c := kb.get_task(conn, child)) is not None and c.status == "ready"
            assert conn.execute("SELECT * FROM task_links").fetchall() == []
        with kbc.connect(board="dst") as conn:
            assert (m := kb.get_task(conn, res["to_task_id"])) is not None and m.title == "parent"
            assert conn.execute("SELECT * FROM task_links").fetchall() == []


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
        src_list = json.loads(_cli(["--board", "src", "list", "--json"], env_extra=env).stdout)
        dst_list = json.loads(_cli(["--board", "dst", "list", "--json"], env_extra=env).stdout)
        assert src_list == []
        assert [t["title"] for t in dst_list] == ["Move Me"]




