"""Board-path resolution when an env var pins a path (``HERMES_KANBAN_DB`` …).

The dispatcher injects a worker's own store as a path pin. That pin must answer
for the board it belongs to and for no other: while it answered for every board,
``hermes kanban --board <other> …`` read the pinned store instead, so a
dispatched worker could not see or act on any other board ("no such task"), and
cross-board readers silently got the wrong data. Boards are also matched by
resolved DB path (``count_running_tasks_other_boards``), so a pin that collapsed
every board onto one path disabled that host-wide cap.
"""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME, no prior kanban state, no kanban env pins."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_ATTACHMENTS_ROOT",
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
    return home


@pytest.fixture
def pinned_default(fresh_home, monkeypatch):
    """The shape the dispatcher injects for a worker on the ``default`` board."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(fresh_home / "kanban.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    return fresh_home / "kanban.db"


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
        timeout=60,
    )


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

class TestPinnedBoardPath:
    def test_explicit_board_wins_over_a_pin_that_belongs_to_another_board(
        self, fresh_home, pinned_default,
    ):
        """The reported defect: a pinned worker asking for another board."""
        kb.create_board("research")
        assert kb.kanban_db_path(board="research") == (
            fresh_home / "kanban" / "boards" / "research" / "kanban.db"
        )
        # The pin still owns the board it belongs to.
        assert kb.kanban_db_path(board="default") == pinned_default

    def test_pin_belongs_to_its_board_even_without_the_env_slug(
        self, fresh_home, monkeypatch,
    ):
        """Path attribution alone (no ``HERMES_KANBAN_BOARD``) is enough."""
        kb.create_board("research")
        kb.create_board("yoyoflow")
        pin = fresh_home / "kanban" / "boards" / "research" / "kanban.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(pin))
        assert kb.kanban_db_path() == pin
        assert kb.kanban_db_path(board="research") == pin
        assert kb.kanban_db_path(board="yoyoflow") == (
            fresh_home / "kanban" / "boards" / "yoyoflow" / "kanban.db"
        )
        assert kb.kanban_db_path(board="default") == fresh_home / "kanban.db"

    def test_unattributable_pin_stays_authoritative(
        self, fresh_home, tmp_path, monkeypatch,
    ):
        """A path naming no board's canonical store is an operator-supplied
        location (scratch/test harness) — a slug argument never overrides it."""
        forced = tmp_path / "forced.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(forced))
        assert kb.kanban_db_path() == forced
        assert kb.kanban_db_path(board="research") == forced

    def test_unset_pin_is_unchanged(self, fresh_home):
        kb.create_board("research")
        assert kb.kanban_db_path() == fresh_home / "kanban.db"
        assert kb.kanban_db_path(board="research") == (
            fresh_home / "kanban" / "boards" / "research" / "kanban.db"
        )

    def test_scoped_current_board_counts_as_explicit(self, fresh_home, pinned_default):
        """``hermes kanban --board X …`` installs the scoped current board."""
        kb.create_board("research")
        with kb.scoped_current_board("research"):
            assert kb.kanban_db_path() == (
                fresh_home / "kanban" / "boards" / "research" / "kanban.db"
            )
            assert kb.kanban_db_path(board="default") == pinned_default
        assert kb.kanban_db_path() == pinned_default

    def test_workspaces_and_attachments_roots_follow_the_same_rule(
        self, fresh_home, monkeypatch,
    ):
        kb.create_board("research")
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
        monkeypatch.setenv(
            "HERMES_KANBAN_WORKSPACES_ROOT", str(fresh_home / "kanban" / "workspaces"),
        )
        monkeypatch.setenv(
            "HERMES_KANBAN_ATTACHMENTS_ROOT", str(fresh_home / "kanban" / "attachments"),
        )
        assert kb.workspaces_root(board="research") == (
            fresh_home / "kanban" / "boards" / "research" / "workspaces"
        )
        assert kb.attachments_root(board="research") == (
            fresh_home / "kanban" / "boards" / "research" / "attachments"
        )
        assert kb.task_attachments_dir("t_1", board="research") == (
            fresh_home / "kanban" / "boards" / "research" / "attachments" / "t_1"
        )
        assert kb.workspaces_root(board="default") == fresh_home / "kanban" / "workspaces"

    def test_every_board_resolves_to_its_own_store_under_a_pin(
        self, fresh_home, pinned_default,
    ):
        """Regression for the host-wide cap: boards are matched by resolved DB
        path, so one shared path made every board look like the pinned one."""
        kb.create_board("research")
        kb.create_board("yoyoflow")
        paths = {m["slug"]: kb.kanban_db_path(board=m["slug"]) for m in kb.list_boards()}
        assert sorted(paths) == ["default", "research", "yoyoflow"]
        assert len(set(paths.values())) == 3

    def test_pin_does_not_hide_another_boards_cards(self, fresh_home, pinned_default):
        """End to end at the DB layer: the other board's store is reachable."""
        kb.create_board("yoyoflow")
        with kbc.connect(board="default") as conn:
            kb.create_task(conn, title="default card", assignee="dev")
        with kbc.connect(board="yoyoflow") as conn:
            other = kb.create_task(conn, title="yoyoflow card", assignee="dev")
        with kbc.connect(board="yoyoflow") as conn:
            assert kb.get_task(conn, other) is not None
        with kbc.connect(board="default") as conn:
            assert kb.get_task(conn, other) is None


# ---------------------------------------------------------------------------
# CLI surface (the card's reproduction)
# ---------------------------------------------------------------------------

class TestPinnedBoardPathCLI:
    def test_board_flag_reaches_another_board_from_a_pinned_shell(self, fresh_home):
        kb.create_board("yoyoflow")
        with kbc.connect(board="yoyoflow") as conn:
            tid = kb.create_task(conn, title="yoyoflow card", assignee="dev")
        with kbc.connect(board="default") as conn:
            default_tid = kb.create_task(conn, title="default card", assignee="dev")

        env = {
            "HERMES_HOME": str(fresh_home),
            "HERMES_KANBAN_DB": str(fresh_home / "kanban.db"),
            "HERMES_KANBAN_BOARD": "default",
        }
        res = _cli(["--board", "yoyoflow", "show", tid], env_extra=env)
        assert res.returncode == 0, res.stderr
        assert tid in res.stdout

        # …and the pinned board's own card is not reachable through the other board.
        res = _cli(["--board", "yoyoflow", "show", default_tid], env_extra=env)
        assert res.returncode != 0
        assert default_tid in (res.stdout + res.stderr)

    def test_board_list_counts_stay_per_board_from_a_pinned_shell(self, fresh_home):
        kb.create_board("yoyoflow")
        with kbc.connect(board="yoyoflow") as conn:
            kb.create_task(conn, title="yoyoflow card", assignee="dev")
            kb.create_task(conn, title="yoyoflow card 2", assignee="dev")
        with kbc.connect(board="default") as conn:
            kb.create_task(conn, title="default card", assignee="dev")

        env = {
            "HERMES_HOME": str(fresh_home),
            "HERMES_KANBAN_DB": str(fresh_home / "kanban.db"),
            "HERMES_KANBAN_BOARD": "default",
        }
        res = _cli(["--board", "yoyoflow", "list", "--json"], env_extra=env)
        assert res.returncode == 0, res.stderr
        assert "yoyoflow card" in res.stdout
        assert "default card" not in res.stdout
