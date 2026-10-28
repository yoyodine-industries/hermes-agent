"""Board default project: the two CLI seams that write it, and the LOUD drop.

Card ``t_d87cad1d`` (workstream ``skills-refactor``, spec
``/opt/hermes_sandbox/platform/skills-refactor/WORKSTREAM-PATTERN.md`` §3 — gaps G3/G3b/G4).

``create_task`` already READS ``board.json:project_id`` to scope every new task on a board
to a project (deterministic worktree + branch under the project's primary repo). Two
pieces were missing:

* **G3 / G3b** — no CLI verb WROTE that key. ``hermes project bind-board`` wrote only
  ``board.json:default_workdir``, so "the project groups the cards" rested on every
  creator remembering ``--project`` (measured on the workstream board: 13 of 23 rows left
  ``NULL`` against 10 carrying the project).
* **G4** — an unresolvable ``--project`` (the registry is per-profile,
  ``$HERMES_HOME/projects.db``) was dropped with no warning, no event and no comment.
  A silent drop is a silent failure: the caller believes the card is grouped and it is not.

The link stays a DROP either way — never a dangling row, never a crash. What changes is
that the drop is now loud (stderr + a ``project_link_unresolved`` note on the ``created``
event).
"""

from __future__ import annotations

import argparse
import subprocess
import os
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import projects_cmd
from hermes_cli import projects_db as pdb


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An isolated Hermes home: the kanban store AND the per-profile projects registry."""
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(h))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants

        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return h


def _project(*argv) -> int:
    """Drive the real ``hermes project …`` parser into ``projects_command``."""
    root = argparse.ArgumentParser(prog="hermes")
    p = projects_cmd.build_parser(root.add_subparsers(dest="command"))
    p.set_defaults(func=projects_cmd.projects_command)
    return projects_cmd.projects_command(root.parse_args(["project", *argv]))


def _board(*argv) -> int:
    """Drive the real ``hermes kanban boards …`` parser through ``kanban_command``."""
    root = argparse.ArgumentParser(prog="hermes", add_help=False)
    kc.build_parser(root.add_subparsers(dest="command"))
    return kc.kanban_command(root.parse_args(["kanban", "boards", *argv]))


def _create(argv) -> int:
    """Drive the real ``hermes kanban create …`` parser into ``_cmd_create``."""
    root = argparse.ArgumentParser(prog="hermes", add_help=False)
    kc.build_parser(root.add_subparsers(dest="command"))
    return kc._cmd_create(root.parse_args(["kanban", "create", *argv]))


def _created_payload(task_id: str) -> dict:
    with kbc.connect_closing() as conn:
        for event in kb.list_events(conn, task_id):
            if event.kind == "created":
                return dict(event.payload or {})
    return {}


def _git_repo(path):
    """A project primary is a GIT REPO, not merely a folder (spawn gate, t_e5ff985b).

    A worktree card may no longer be born under a plain folder, so every project these cases
    bind a board to must be a real repository. An empty ``git init`` is enough.
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True, text=True)
    return path


def _new_project(tmp_path, slug="widget"):
    repo = _git_repo(tmp_path / "repo")
    assert _project("create", "Widget", str(repo), "--slug", slug) == 0
    with pdb.connect_closing() as conn:
        return pdb.get_project(conn, slug), repo


# --- G3: `hermes project bind-board` writes BOTH keys ---------------------------------


def test_bind_board_writes_default_workdir_and_the_project_SLUG(home, tmp_path, capsys):
    proj, repo = _new_project(tmp_path)
    assert _project("bind-board", "widget", "default") == 0
    meta = kb.read_board_metadata("default")
    assert meta["project_id"] == proj.slug  # the SLUG: stable across profile registries
    assert meta["default_workdir"] == str(repo)


def test_unbind_clears_the_board_project_and_leaves_the_workdir(home, tmp_path):
    _new_project(tmp_path)
    assert _project("bind-board", "widget", "default") == 0
    assert _project("bind-board", "widget") == 0  # no board = unbind
    meta = kb.read_board_metadata("default")
    assert meta["project_id"] is None
    assert meta["default_workdir"] is not None  # the workdir write is not touched


def test_a_card_created_on_the_bound_board_inherits_the_project_with_no_flag(home, tmp_path, capsys):
    """The acceptance case: bind the board, then file WITHOUT ``--project``."""
    proj, repo = _new_project(tmp_path)
    assert _project("bind-board", "widget", "default") == 0
    before = kb.read_board_metadata("default")

    assert _create(["inherit me", "--workspace", "worktree"]) == 0
    capsys.readouterr()

    with kbc.connect_closing() as conn:
        task = kb.list_tasks(conn)[-1]
    assert task.project_id == proj.id
    assert task.workspace_kind == "worktree"
    assert task.workspace_path == os.path.join(str(repo), ".worktrees", task.id)
    assert task.branch_name and task.branch_name.startswith(f"{proj.slug}/")
    assert kb.read_board_metadata("default")["project_id"] == before["project_id"] == "widget"


# --- G3b: the direct writer on the board surface ---------------------------------------


def test_boards_set_project_sets_and_clears(home, tmp_path, capsys):
    proj, _repo = _new_project(tmp_path)
    kb.create_board("scoped", name="Scoped")
    assert _board("set-project", "scoped", "widget") == 0
    assert kb.read_board_metadata("scoped")["project_id"] == proj.slug
    assert _board("set-project", "scoped", "--clear") == 0
    assert kb.read_board_metadata("scoped")["project_id"] is None


def test_boards_set_project_resolves_an_id_and_stores_the_slug(home, tmp_path, capsys):
    proj, _repo = _new_project(tmp_path)
    kb.create_board("scoped", name="Scoped")
    assert _board("set-project", "scoped", proj.id) == 0
    assert kb.read_board_metadata("scoped")["project_id"] == proj.slug


def test_boards_set_project_refuses_an_unknown_project(home, capsys):
    kb.create_board("scoped", name="Scoped")
    assert _board("set-project", "scoped", "no-such-project") == 2
    err = capsys.readouterr().err
    assert "no-such-project" in err
    assert str(pdb.projects_db_path()) in err
    assert kb.read_board_metadata("scoped")["project_id"] is None


# --- G4: an unresolvable --project warns instead of dropping in silence ----------------


def test_an_unresolvable_project_warns_and_is_recorded_on_the_created_event(home, capsys):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="orphan", project_id="p_deadbeef",
                             workspace_kind="worktree")
        task = kb.get_task(conn, tid)
    assert task.project_id is None  # still a drop; never a dangling reference

    err = capsys.readouterr().err
    assert "p_deadbeef" in err
    assert str(pdb.projects_db_path()) in err
    assert "slug" in err

    payload = _created_payload(tid)
    assert payload["project_link_unresolved"] == "p_deadbeef"
    assert payload["project_link_source"] == "argument"


def test_the_cli_create_surfaces_the_warning_on_the_terminal(home, capsys):
    assert _create(["orphan", "--project", "p_deadbeef", "--workspace", "worktree"]) == 0
    captured = capsys.readouterr()
    assert "p_deadbeef" in captured.err  # the CLI must say it, not only the event log
    with kbc.connect_closing() as conn:
        tid = kb.list_tasks(conn)[-1].id
    assert _created_payload(tid)["project_link_unresolved"] == "p_deadbeef"


def test_an_unresolvable_BOARD_project_warns_too(home, capsys):
    """A board scoped to a project this profile's registry does not hold is the same drop."""
    kb.create_board("scoped", name="Scoped", project_id="p_gone")
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", board="scoped", workspace_kind="worktree")
    captured = capsys.readouterr()
    assert "p_gone" in captured.err
    payload = _created_payload(tid)
    assert payload["project_link_unresolved"] == "p_gone"
    assert payload["project_link_source"] == "board"


def test_a_resolvable_project_warns_about_nothing(home, tmp_path, capsys):
    _new_project(tmp_path)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="grouped", project_id="widget")
    assert capsys.readouterr().err == ""
    assert "project_link_unresolved" not in _created_payload(tid)


# --- G4c: a REFUSED board write is not a missing board ---------------------------------


def test_bind_board_reports_a_refused_board_write(home, tmp_path, monkeypatch, capsys):
    """Fail-soft is for a board that does not RESOLVE; a fence refusal is not that.

    Measured 2026-10-13 from a dispatched worker's own session (this card's session): the
    verb printed ``Bound widget -> board default`` and wrote NOTHING — ``write_board_metadata``
    had been refused by the delegated-child fence and the swallow dressed it as success.
    """
    _new_project(tmp_path)

    def _refuse(*_a, **_k):
        raise PermissionError("delegate_task child contexts cannot mutate Kanban tasks or boards")

    monkeypatch.setattr(kb, "write_board_metadata", _refuse)
    assert _project("bind-board", "widget", "default") == 1
    captured = capsys.readouterr()
    assert "Bound" not in captured.out
    assert "did NOT land" in captured.err
    assert captured.err.count("project:") == 1, captured.err
    assert kb.read_board_metadata("default")["project_id"] is None


def test_unbind_reports_a_refused_board_write(home, tmp_path, monkeypatch, capsys):
    _new_project(tmp_path)
    assert _project("bind-board", "widget", "default") == 0

    def _refuse(*_a, **_k):
        raise PermissionError("delegate_task child contexts cannot mutate Kanban tasks or boards")

    monkeypatch.setattr(kb, "write_board_metadata", _refuse)
    assert _project("bind-board", "widget") == 1
    captured = capsys.readouterr()
    assert "Unbound" not in captured.out
    assert "did NOT land" in captured.err
    assert captured.err.count("project:") == 1, captured.err


# --- the acceptance's own proof: `hermes kanban show <id>` names the project -----------


def test_kanban_show_names_the_project_the_card_carries(home, tmp_path, capsys):
    proj, _repo = _new_project(tmp_path)
    assert _create(["scoped", "--project", "widget", "--workspace", "worktree"]) == 0
    capsys.readouterr()
    with kbc.connect_closing() as conn:
        tid = kb.list_tasks(conn)[-1].id

    root = argparse.ArgumentParser(prog="hermes", add_help=False)
    kc.build_parser(root.add_subparsers(dest="command"))
    assert kc.kanban_command(root.parse_args(["kanban", "show", tid])) == 0
    out = capsys.readouterr().out
    line = next((l for l in out.splitlines() if l.strip().startswith("project:")), "")
    assert proj.id in line, out


# --- the settled ruling: the portability note belongs in the `--project` help --------


def test_the_project_flag_help_names_the_portable_handle_and_the_loud_drop(capsys):
    """``hermes kanban create --help`` is the surface a filing worker actually reads.

    The card's settled ruling puts the slug-portability guidance HERE, not only in the
    runtime warning a caller meets after the card is already filed: an id from another
    profile's per-profile registry is exactly the trap G4 closes.
    """
    root = argparse.ArgumentParser(prog="hermes", add_help=False)
    kc.build_parser(root.add_subparsers(dest="command"))
    with pytest.raises(SystemExit):
        root.parse_args(["kanban", "create", "--help"])
    text = " ".join(capsys.readouterr().out.split()).lower()
    assert "--project" in text
    assert "slug" in text, text
    assert "registry" in text, text
    assert "dropped" in text, text
