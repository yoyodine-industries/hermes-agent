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
        """``HERMES_KANBAN_DB`` pins the file regardless of ``board=`` arg for every
        execution the dispatcher fences (the 5ec6baa multi-boards isolation: workers
        physically cannot see other boards): its dispatched workers (``HERMES_KANBAN_TASK``)
        and delegated children / descendants (``HERMES_DELEGATED_CHILD_CONTEXT``). An
        explicit board that outranked the pin would also escape
        ``kanban_path_is_fenced``, which checks the pinned path / fenced root. Outside
        those fences an explicit board is the caller's own intent and wins (see
        ``test_explicit_board_trumps_env_var_db_override`` below)."""
        forced = tmp_path / "custom.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(forced))
        assert kb.kanban_db_path() == forced
        assert kb.kanban_db_path(board=None) == forced
        # Dispatched worker identity: the pin still fences explicit board intent.
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fence_probe")
        assert kb.kanban_db_path(board="ignored") == forced
        # Delegated children / spawned descendants are fenced the same way.
        from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER
        monkeypatch.setenv(DELEGATED_CHILD_ENV_MARKER, "1")
        assert kb.kanban_db_path(board="ignored") == forced

    def test_assert_board_matches_pin_refuses_mismatch(self, fresh_home, monkeypatch):
        """Regression (7654ac4bfe): an explicit board= that resolves to a different board
        than the pinned DB is refused, not silently served — a read from the wrong board
        is indistinguishable from success."""
        pinned = kb.kanban_db_path(board="ops")
        monkeypatch.setenv("HERMES_KANBAN_DB", str(pinned))
        with pytest.raises(ValueError, match="refusing to target board 'financially'"):
            kb.assert_board_matches_pin("financially")

    def test_assert_board_matches_pin_names_both_boards(self, fresh_home, monkeypatch):
        """The refusal message names both the requested and the pinned board."""
        pinned = kb.kanban_db_path(board="ops")
        monkeypatch.setenv("HERMES_KANBAN_DB", str(pinned))
        with pytest.raises(ValueError) as excinfo:
            kb.assert_board_matches_pin("financially")
        msg = str(excinfo.value)
        assert "financially" in msg
        assert "ops" in msg

    def test_assert_board_matches_pin_accepts_match(self, fresh_home, monkeypatch):
        """A board= naming the pinned board is a no-op."""
        pinned = kb.kanban_db_path(board="ops")
        monkeypatch.setenv("HERMES_KANBAN_DB", str(pinned))
        kb.assert_board_matches_pin("ops")  # no raise

    def test_assert_board_matches_pin_noop_without_pin(self, fresh_home):
        """No pin set -> no-op, even for an explicit board=."""
        kb.assert_board_matches_pin(None)  # no raise
        kb.assert_board_matches_pin("financially")  # no raise

    def test_assert_board_matches_pin_refuses_noncanonical_pin(self, fresh_home, tmp_path, monkeypatch):
        """A non-canonical pinned path still refuses a board= naming another board."""
        forced = tmp_path / "custom.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(forced))
        with pytest.raises(ValueError, match="refusing to target board 'default'"):
            kb.assert_board_matches_pin("default")

    def test_env_var_db_override_wins_when_board_not_passed(self, fresh_home, tmp_path, monkeypatch):
        """``HERMES_KANBAN_DB`` pins the file when no explicit ``board=`` is given
        (back-compat for dispatcher-spawned workers with no board override)."""
        forced = tmp_path / "custom.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(forced))
        assert kb.kanban_db_path() == forced
        assert kb.kanban_db_path(board=None) == forced

    def test_explicit_board_trumps_env_var_db_override(self, fresh_home, tmp_path, monkeypatch):
        """Documented priority (module docstring, predates this test): explicit
        ``board=`` arg > ``HERMES_KANBAN_BOARD`` > ``HERMES_KANBAN_DB`` > current >
        default — for UNFENCED callers. An explicit ``board=`` must resolve to that
        board's own path even when ``HERMES_KANBAN_DB`` pins a different file: this is
        what makes cross-board ``kanban_create(board=...)`` / ``kanban_show(board=...)``
        work from a user-facing session instead of silently landing on the pinned board
        (t_3f1c63a5). Fenced callers (dispatched workers, delegated children) keep the
        pinned path — see ``test_env_var_db_override_still_wins`` above."""
        forced = tmp_path / "custom.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(forced))
        p = kb.kanban_db_path(board="atm10-server")
        assert p == fresh_home / "kanban" / "boards" / "atm10-server" / "kanban.db"
        assert p != forced


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
        # Regression for #23833: a poll loop that re-creates a just-removed
        # board must get a fresh schema-init pass — if _INITIALIZED_PATHS
        # still contained the resolved path, the CREATE TABLE pass would be
        # skipped and downstream readers hit `no such table: task_events`.
        # (Since #43243 connect() itself refuses to recreate a removed board,
        # so the re-creation goes through create_board, as it should.)
        kb.create_board("recycle")
        # First connect populates _INITIALIZED_PATHS for this DB.  Use
        # connect_closing: `with connect() as conn` does NOT close the fd, and
        # on Windows an open connection locks kanban.db so remove_board's
        # rename below fails with WinError 5/32.
        with kbc.connect_closing(board="recycle") as conn:
            kb.create_task(conn, title="t1", assignee="dev")
        db_path = kb.board_dir("recycle") / "kanban.db"
        assert str(db_path.resolve()) in kb._INITIALIZED_PATHS

        kb.remove_board("recycle", archive=archive)
        # remove_board must drop the cache entry so a re-create through
        # connect() gets a fresh schema-init pass.
        assert str(db_path.resolve()) not in kb._INITIALIZED_PATHS

        # Simulate the board being re-created at the same slug: the schema
        # must be re-applied on the fresh DB file.
        kb.create_board("recycle")
        with kbc.connect_closing(board="recycle") as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert "task_events" in tables
        assert "tasks" in tables



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
    """Run ``hermes kanban …`` with PYTHONPATH pinned to the worktree.

    The caller's board pins are DROPPED first: a developer shell (and every dispatched worker
    shell) carries ``HERMES_KANBAN_DB``/``HERMES_KANBAN_BOARD``, and those are FENCES — under a
    pin an explicit ``--board`` is refused (card t_398313ec), so inheriting them would silently
    turn every ``--board`` case below into the refusal case. A test that wants a pin sets it in
    ``env_extra``.
    """
    env = dict(os.environ)
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_DELEGATED_CHILD_CONTEXT",
    ):
        env.pop(var, None)
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
# A board pin is a LOUD fence (card t_398313ec)
# ---------------------------------------------------------------------------

def _titles(env: dict, board: str) -> list[str]:
    """Titles on ``board``, read with NO pin in the environment."""
    r = _cli(["--board", board, "list", "--json"], env_extra=env)
    assert r.returncode == 0, r.stderr
    return [t["title"] for t in json.loads(r.stdout)]


class TestPinnedBoardOverrideRefusal:
    """``--board <other>`` under a board pin must REFUSE, not silently serve the pin.

    Measured 2026-10-27 (card t_398313ec): in a shell carrying
    ``HERMES_KANBAN_DB=<research store>``, ``hermes kanban --board platform show <id>``
    answered ``no such task`` for a card that exists, ``boards show platform`` printed the
    platform board's description over the research store's path and counts, and ``boards``
    showed IDENTICAL counts for every slug. The dangerous direction is the write one: the
    mutation landed on the pinned board while the operator believed they named ``--board``.
    """

    @pytest.fixture
    def two_boards(self, tmp_path):
        home = tmp_path / "kenv"
        home.mkdir()
        env = {"HERMES_HOME": str(home), "HERMES_KANBAN_HOME": str(home)}
        for slug in ("alpha", "beta"):
            created = _cli(["boards", "create", slug], env_extra=env)
            assert created.returncode == 0, created.stderr
        ids = {}
        for slug in ("alpha", "beta"):
            created = _cli(["--board", slug, "create", f"{slug} card", "--json"], env_extra=env)
            assert created.returncode == 0, created.stderr
            ids[slug] = json.loads(created.stdout)["id"]
        return {
            "home": home,
            "env": env,
            "ids": ids,
            "alpha_db": home / "kanban" / "boards" / "alpha" / "kanban.db",
            "beta_db": home / "kanban" / "boards" / "beta" / "kanban.db",
        }

    def _pinned(self, case, slug: str = "alpha", *, fenced: bool = True) -> tuple[Path, dict]:
        """The FENCED worker shape by default: a board pin plus ``HERMES_KANBAN_TASK``.

        ``fenced`` is what decides whether the pin OVERRIDES an explicit ``--board`` at all
        (``kanban_db._explicit_board_intent_pinned``), and therefore whether a contradicting
        slug is a contradiction: a dispatched worker carries ``HERMES_KANBAN_TASK`` and is
        served the pinned store, so naming another board is a refusal; an OPERATOR shell that
        merely has ``HERMES_KANBAN_BOARD`` set still resolves ``--board`` to that board's own
        store, so there is nothing to refuse. Pass ``fenced=False`` for the operator shape.
        """
        pinned_db = case[f"{slug}_db"]
        assert pinned_db.exists()
        env = {**case["env"], "HERMES_KANBAN_DB": str(pinned_db)}
        if fenced:
            env["HERMES_KANBAN_TASK"] = "t_syntheticworker"
        return pinned_db, env

    def test_show_under_pin_refuses_and_names_both_boards(self, two_boards):
        pinned_db, env = self._pinned(two_boards, "alpha")
        mtime = pinned_db.stat().st_mtime_ns

        r = _cli(["--board", "beta", "show", two_boards["ids"]["beta"]], env_extra=env)

        assert r.returncode != 0, "a refusal must be a non-zero exit"
        blob = r.stdout + r.stderr
        # The sentence the model-facing tools already use for this condition, naming BOTH.
        assert "refusing to target board 'beta'" in blob
        assert "pinned to board 'alpha'" in blob
        assert "HERMES_KANBAN_DB" in blob
        # ... naming the REMEDY, not just the condition.
        assert "env -u HERMES_KANBAN_DB" in blob
        # Not the silent-serve symptom: it must not answer for the pinned store.
        assert "no such task" not in blob
        # Nothing read, nothing written, on the board it refused to consult.
        assert pinned_db.stat().st_mtime_ns == mtime

    def test_create_under_pin_refuses_and_lands_on_neither_board(self, two_boards):
        pinned_db, env = self._pinned(two_boards, "alpha")
        mtime = pinned_db.stat().st_mtime_ns

        r = _cli(["--board", "beta", "create", "must not land"], env_extra=env)

        assert r.returncode != 0
        assert "refusing to target board 'beta'" in r.stderr
        assert "pinned to board 'alpha'" in r.stderr
        # The whole point: the write did not land on the pinned board (or anywhere).
        assert pinned_db.stat().st_mtime_ns == mtime
        assert _titles(two_boards["env"], "alpha") == ["alpha card"]
        assert _titles(two_boards["env"], "beta") == ["beta card"]

    def test_complete_under_pin_refuses_and_leaves_the_card_open(self, two_boards):
        _, env = self._pinned(two_boards, "alpha")

        r = _cli(["--board", "beta", "complete", two_boards["ids"]["beta"],
                  "--summary", "must not land"], env_extra=env)

        assert r.returncode != 0
        assert "refusing to target board 'beta'" in r.stderr
        assert "pinned to board 'alpha'" in r.stderr
        shown = _cli(["--board", "beta", "show", two_boards["ids"]["beta"]],
                     env_extra=two_boards["env"])
        assert shown.returncode == 0, shown.stderr
        assert "done" not in shown.stdout.split("\n")[0]

    def test_boards_show_under_pin_refuses_instead_of_rendering(self, two_boards):
        _, env = self._pinned(two_boards, "alpha")

        r = _cli(["boards", "show", "beta"], env_extra=env)

        assert r.returncode != 0
        assert "refusing to target board 'beta'" in r.stderr
        assert "pinned to board 'alpha'" in r.stderr
        assert "env -u HERMES_KANBAN_DB" in r.stderr
        # No half-and-half table: neither the pinned store's path nor its counts rendered
        # under beta's metadata.
        assert "DB path" not in r.stdout
        assert "Tasks:" not in r.stdout

    def test_boards_show_without_a_slug_reports_the_pinned_board(self, two_boards):
        """Bare ``boards show`` must not print the ambient ``current`` board's metadata."""
        pinned_db, env = self._pinned(two_boards, "alpha")
        assert _cli(["boards", "switch", "beta"],
                    env_extra={"HERMES_HOME": str(two_boards["home"]),
                               "HERMES_KANBAN_HOME": str(two_boards["home"])}).returncode == 0

        r = _cli(["boards", "show"], env_extra=env)

        assert r.returncode == 0, r.stderr
        assert "alpha" in r.stdout.split("\n")[0]
        assert str(pinned_db) in r.stdout

    def test_boards_list_reads_counts_only_for_the_pinned_board(self, two_boards):
        _, env = self._pinned(two_boards, "alpha")

        r = _cli(["boards", "list", "--json"], env_extra=env)

        assert r.returncode == 0, r.stderr
        by_slug = {b["slug"]: b for b in json.loads(r.stdout)}
        assert by_slug["alpha"]["total"] == 1
        # beta's store was not read, so no counts may be printed under its slug.
        assert by_slug["beta"]["counts"] is None
        assert by_slug["beta"]["total"] is None

        text = _cli(["boards"], env_extra=env)
        assert text.returncode == 0, text.stderr
        assert "(not read — pinned)" in text.stdout
        assert "pinned to" in text.stdout and "HERMES_KANBAN_DB" in text.stdout

    def test_hermes_kanban_board_pin_also_refuses_a_contradicting_override(self, two_boards):
        env = {**two_boards["env"], "HERMES_KANBAN_BOARD": "alpha",
               "HERMES_KANBAN_TASK": "t_syntheticworker"}

        refused = _cli(["--board", "beta", "create", "must not land"], env_extra=env)
        assert refused.returncode != 0
        assert "refusing to target board 'beta'" in refused.stderr
        assert "pinned to board 'alpha'" in refused.stderr
        assert "HERMES_KANBAN_BOARD" in refused.stderr

        # The pin's own board is not a contradiction.
        allowed = _cli(["--board", "alpha", "create", "second alpha card"], env_extra=env)
        assert allowed.returncode == 0, allowed.stderr

    def test_no_pin_still_honours_board_override(self, two_boards):
        """The regression guard against over-fixing this into "never honour --board"."""
        assert _titles(two_boards["env"], "beta") == ["beta card"]
        r = _cli(["--board", "beta", "show", two_boards["ids"]["beta"]],
                 env_extra=two_boards["env"])
        assert r.returncode == 0, r.stderr
        assert "beta card" in r.stdout
        # ... and the override follows the SLUG, not the current-board pointer.
        assert "Board: beta" in r.stdout or "beta" in r.stdout

    def test_operator_pin_shape_keeps_resolving_as_before(self, two_boards):
        """A pin that does NOT override intent must not start refusing.

        The refusal only exists where the pin actually WINS (``_explicit_board_intent_pinned``:
        ``HERMES_KANBAN_TASK`` / the delegated-child marker / the resolver ContextVar). An
        operator shell that merely carries ``HERMES_KANBAN_BOARD`` still resolves ``--board`` to
        that board's own store, so nothing is misrouted and nothing may be refused — this is the
        shape behind ``test_kanban_watch_banner.py::test_watch_names_resolved_board`` and the
        live-claim provenance harness. If this test ever needs the refusal, the fix has grown
        past the bug it was filed for.
        """
        pinned_db, env = self._pinned(two_boards, "alpha", fenced=False)
        assert "HERMES_KANBAN_TASK" not in env

        # A read of the OTHER board is served from the other board's own store.
        shown = _cli(["--board", "beta", "show", two_boards["ids"]["beta"]], env_extra=env)
        assert shown.returncode == 0, shown.stderr
        assert "beta card" in shown.stdout

        # ... and a write lands where the operator named it, not on the pin.
        created = _cli(["--board", "beta", "create", "operator named beta"], env_extra=env)
        assert created.returncode == 0, created.stderr
        assert _titles(two_boards["env"], "beta") == ["beta card", "operator named beta"]
        assert _titles(two_boards["env"], "alpha") == ["alpha card"]

        # `boards show <other>` renders that board's OWN store — never the pinned one.
        meta = _cli(["boards", "show", "beta"], env_extra=env)
        assert meta.returncode == 0, meta.stderr
        assert str(two_boards["beta_db"]) in meta.stdout
        assert str(pinned_db) not in meta.stdout
        assert "2 total" in meta.stdout

        # ... and `boards` keeps its per-slug counts (all four lines were identical before the
        # 2026-10-27 fix ONLY because each line read the pinned store; here each line reads
        # its own store, which is correct and must be shown).
        listing = _cli(["boards", "list", "--json"], env_extra=env)
        assert listing.returncode == 0, listing.stderr
        by_slug = {b["slug"]: b for b in json.loads(listing.stdout)}
        assert by_slug["alpha"]["total"] == 1
        assert by_slug["beta"]["total"] == 2
        text = _cli(["boards"], env_extra=env)
        assert text.returncode == 0, text.stderr
        assert "(not read — pinned)" not in text.stdout


# ---------------------------------------------------------------------------
# Archived / deleted board resurrection (#43243)
# ---------------------------------------------------------------------------

class TestBoardResurrection:
    """Read/watch paths must not resurrect archived or deleted boards.

    Stale dashboard tabs, gateway notifiers and event-stream pollers can hand
    ``connect(board=<slug>)`` a slug whose board was archived (moved to
    ``_archived/`` with an ``archived`` tombstone) or hard-deleted
    (``rmtree``).  ``connect`` must refuse to recreate the directory/DB, and
    ``list_boards`` must not surface a DB-only stub as an active board.
    """

    def test_connect_does_not_recreate_archived_board(self, fresh_home):
        kb.create_board("gone")
        kb.remove_board("gone", archive=True)
        tombstone = kb.board_metadata_path("gone")
        assert tombstone.exists() and kb.read_board_metadata("gone")["archived"] is True
        with pytest.raises(ValueError, match="archived"):
            kbc.connect(board="gone")
        assert not (kb.board_dir("gone") / "kanban.db").exists()

    def test_connect_does_not_recreate_deleted_board(self, fresh_home):
        kb.create_board("nuked")
        kb.remove_board("nuked", archive=False)
        assert not kb.board_dir("nuked").exists()
        with pytest.raises(ValueError, match="does not exist"):
            kbc.connect(board="nuked")
        assert not kb.board_dir("nuked").exists()

    def test_connect_still_opens_live_board(self, fresh_home):
        kb.create_board("live")
        with kbc.connect_closing(board="live") as conn:
            conn.execute("SELECT 1").fetchone()
        assert kb.board_exists("live")

    def test_list_boards_ignores_db_only_stub(self, fresh_home):
        # Simulate the historical stub shape: kanban.db with no board.json.
        stub = kb.board_dir("ghost")
        stub.mkdir(parents=True)
        (stub / "kanban.db").touch()
        assert "ghost" not in [b["slug"] for b in kb.list_boards()]
        assert "ghost" not in [b["slug"] for b in kb.list_boards(include_archived=True)]

    def test_archive_tombstone_keeps_name(self, fresh_home):
        kb.create_board("keepname", name="My Board")
        kb.remove_board("keepname", archive=True)
        meta = kb.read_board_metadata("keepname")
        assert meta["archived"] is True
        assert meta["name"] == "My Board"
        assert "keepname" in [b["slug"] for b in kb.list_boards(include_archived=True)]
        assert "keepname" not in [b["slug"] for b in kb.list_boards(include_archived=False)]


# ---------------------------------------------------------------------------
# Board inventory: every row carries its OWN board's store (card t_5723473b)
# ---------------------------------------------------------------------------

class TestBoardsListPerRowCounts:
    """``hermes kanban boards [list]`` enumerates the HOME, so each row's census and
    store path is the ROW's own board.

    Measured 2026-10-12: with ``HERMES_KANBAN_DB`` pinning ``defcon``, all six rows
    rendered defcon's identical census (``done=2653 …``) while ``platform``'s own
    store held 10,515 tasks — the row's count source resolved through the ambient
    pin (the very override a worker's env injects) instead of by slug. The rows are
    an inventory, not a routed task read, so the pin must not relabel them.
    """

    @staticmethod
    def _own_store(home: Path, slug: str) -> Path:
        """The store the documented layout puts on ``slug`` — spelled out HERE, so this
        module asserts the CLI's rendered contract instead of re-deriving it through the
        code under test (``default`` keeps the legacy ``<root>/kanban.db``)."""
        if slug == kb.DEFAULT_BOARD:
            return home / "kanban.db"
        return home / "kanban" / "boards" / slug / "kanban.db"

    @classmethod
    def _seed(cls, home: Path, slug: str, **by_status: int) -> None:
        """Create ``slug`` with ``status=n`` cards. ``initial_status="running"`` is the
        create-time admission for a card that ENTERS the flow, so a parentless one lands
        ``ready`` (``kanban_db.initial_task_state``); ``"blocked"`` parks it blocked."""
        kb.create_board(slug)
        with kbc.connect_closing(db_path=cls._own_store(home, slug), board=slug) as conn:
            for status, n in by_status.items():
                for i in range(n):
                    kb.create_task(conn, title=f"{slug}-{status}-{i}", assignee="dev",
                                   initial_status=status)

    @classmethod
    def _pin_a_worker_to(cls, home: Path, monkeypatch, slug: str) -> None:
        """A dispatched worker's env: the store pin AND the marker fencing board intent."""
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fence_probe")
        monkeypatch.setenv("HERMES_KANBAN_DB", str(cls._own_store(home, slug)))

    def test_each_rows_counts_and_store_come_from_its_own_board(self, fresh_home, monkeypatch, capsys):
        import argparse

        from hermes_cli import kanban_boards as kboards

        for slug, seeded in (("default", {"blocked": 1}), ("alpha", {"running": 2}),
                             ("beta", {"running": 5, "blocked": 1})):
            self._seed(fresh_home, slug, **seeded)
        self._pin_a_worker_to(fresh_home, monkeypatch, "beta")

        # The fence is untouched: the ROUTING resolver still honors the pin.
        assert kb.kanban_db_path(board="alpha") == self._own_store(fresh_home, "beta")
        assert self._own_store(fresh_home, "alpha") != self._own_store(fresh_home, "beta")

        rc = kboards._cmd_boards_list(argparse.Namespace(json=True, all=False))
        assert rc == 0
        rows = {r["slug"]: r for r in json.loads(capsys.readouterr().out)}

        assert rows["alpha"]["counts"] == {"ready": 2}
        assert rows["beta"]["counts"] == {"ready": 5, "blocked": 1}
        assert rows["default"]["counts"] == {"blocked": 1}
        assert rows["alpha"]["total"] == 2 and rows["beta"]["total"] == 6
        assert rows["alpha"]["db_path"] == str(self._own_store(fresh_home, "alpha"))
        assert rows["beta"]["db_path"] == str(self._own_store(fresh_home, "beta"))

    def test_rendered_rows_differ_and_name_their_own_census(self, fresh_home, monkeypatch, capsys):
        import argparse

        from hermes_cli import kanban_boards as kboards

        self._seed(fresh_home, "alpha", running=2)
        self._seed(fresh_home, "beta", running=6)
        self._pin_a_worker_to(fresh_home, monkeypatch, "beta")

        rc = kboards._cmd_boards_list(argparse.Namespace(json=False, all=False))
        assert rc == 0
        rendered = {}
        for line in capsys.readouterr().out.splitlines():
            fields = line.split()
            if fields and fields[0] in ("alpha", "beta"):
                rendered[fields[0]] = line
        assert set(rendered) == {"alpha", "beta"}
        assert rendered["alpha"] != rendered["beta"]
        assert "ready=2" in rendered["alpha"], rendered["alpha"]
        assert "ready=6" in rendered["beta"], rendered["beta"]

    def test_a_board_with_no_store_reads_as_empty_not_as_the_pinned_board(self, fresh_home, monkeypatch, capsys):
        import argparse

        from hermes_cli import kanban_boards as kboards

        self._seed(fresh_home, "beta", running=4)
        kb.create_board("hollow")          # registered, live, but never written to
        self._own_store(fresh_home, "hollow").unlink()
        self._pin_a_worker_to(fresh_home, monkeypatch, "beta")

        rc = kboards._cmd_boards_list(argparse.Namespace(json=True, all=False))
        assert rc == 0
        rows = {r["slug"]: r for r in json.loads(capsys.readouterr().out)}
        assert rows["hollow"]["counts"] == {}
        assert rows["hollow"]["total"] == 0
        assert rows["beta"]["counts"] == {"ready": 4}
