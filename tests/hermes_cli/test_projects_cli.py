"""Tests for the `hermes project` CLI dispatch (hermes_cli/projects_cmd)."""

from __future__ import annotations

import argparse

import pytest

from hermes_cli import projects_cmd
from hermes_cli import projects_db as pdb


def _run(argv):
    """Build the project subparser, parse argv, and dispatch. Returns rc."""
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    p = projects_cmd.build_parser(sub)
    p.set_defaults(func=projects_cmd.projects_command)
    args = parser.parse_args(["project", *argv])
    return projects_cmd.projects_command(args)


def test_create_list_show(capsys, tmp_path):
    assert _run(["create", "My App", str(tmp_path), "--use"]) == 0
    out = capsys.readouterr().out
    assert "Created project" in out

    with pdb.connect_closing() as conn:
        projects = pdb.list_projects(conn)
        assert len(projects) == 1
        assert projects[0].name == "My App"
        # --use set it active.
        assert pdb.get_active_id(conn) == projects[0].id

    assert _run(["list"]) == 0
    assert "my-app" in capsys.readouterr().out

    assert _run(["show", "my-app"]) == 0
    assert "My App" in capsys.readouterr().out




def test_rename_and_archive(tmp_path):
    _run(["create", "Old Name", str(tmp_path)])
    assert _run(["rename", "old-name", "New Name"]) == 0
    with pdb.connect_closing() as conn:
        assert pdb.get_project(conn, "old-name").name == "New Name"

    assert _run(["archive", "old-name"]) == 0
    with pdb.connect_closing() as conn:
        assert pdb.list_projects(conn) == []
        assert len(pdb.list_projects(conn, include_archived=True)) == 1

    assert _run(["restore", "old-name"]) == 0
    with pdb.connect_closing() as conn:
        assert len(pdb.list_projects(conn)) == 1


# --- refusals: a typo must not become a row --------------------------------------------
#
# Four answers that used to be success-shaped, each leaving registry rot behind: a
# project created on a folder that does not exist, a folder claimed by two projects, a
# primary repointed at a vanished folder, and ``bind-board`` printing ``Bound …`` for a
# board nobody ever created. Every one refuses at the DOOR, before the write, naming what
# it could not resolve — in the store's own shape (``project: …`` on stderr, exit 2).


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An isolated per-profile registry AND kanban root, so the counts mean what they say."""
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(h))
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(var, raising=False)
    return h


def _dir(path) -> str:
    """A real directory on disk — the fixture these doors actually need."""
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _folders(slug: str) -> list:
    with pdb.connect_closing() as conn:
        return [f.path for f in pdb.get_project(conn, slug).folders]


def test_create_refuses_a_primary_path_that_does_not_exist(home, tmp_path, capsys):
    ghost = str(tmp_path / "ghost-repo")  # never created
    assert _run(["create", "Ghost", ghost]) == 2
    captured = capsys.readouterr()
    assert ghost in captured.err
    assert "does not exist" in captured.err
    assert "Created project" not in captured.out
    with pdb.connect_closing() as conn:
        assert pdb.list_projects(conn, include_archived=True) == []

    # `--primary` is the same door, and a project with NO folders is still legal.
    assert _run(["create", "Also Ghost", "--primary", ghost, "--slug", "also-ghost"]) == 2
    assert ghost in capsys.readouterr().err
    assert _run(["create", "No Folders"]) == 0


def test_add_folder_refuses_a_path_that_is_not_a_directory(home, tmp_path, capsys):
    repo = _dir(tmp_path / "repo")
    assert _run(["create", "Widget", repo, "--slug", "widget"]) == 0
    capsys.readouterr()

    missing = str(tmp_path / "typo-folder")
    assert _run(["add-folder", "widget", missing]) == 2
    captured = capsys.readouterr()
    assert missing in captured.err
    assert "does not exist" in captured.err
    assert "Added" not in captured.out
    assert _folders("widget") == [repo]  # the refused path never became a row

    # A file is not a folder either.
    a_file = tmp_path / "a-file.txt"
    a_file.write_text("x")
    assert _run(["add-folder", "widget", str(a_file)]) == 2
    assert str(a_file) in capsys.readouterr().err
    assert _folders("widget") == [repo]


def test_add_folder_refuses_a_folder_another_project_already_owns(home, tmp_path, capsys):
    repo_a = _dir(tmp_path / "a")
    repo_b = _dir(tmp_path / "b")
    assert _run(["create", "Alpha", repo_a, "--slug", "alpha"]) == 0
    assert _run(["create", "Beta", repo_b, "--slug", "beta"]) == 0
    capsys.readouterr()

    assert _run(["add-folder", "beta", repo_a]) == 2
    captured = capsys.readouterr()
    assert "already belongs to project 'alpha'" in captured.err
    assert "Added" not in captured.out
    assert _folders("beta") == [repo_b]  # one directory, one owner

    # Re-adding a folder the project already has stays a no-op, not a refusal.
    assert _run(["add-folder", "beta", repo_b]) == 0
    assert _folders("beta") == [repo_b]


def test_set_primary_refuses_a_folder_that_has_gone(home, tmp_path, capsys):
    import shutil

    repo = _dir(tmp_path / "repo")
    other = _dir(tmp_path / "other")
    assert _run(["create", "Widget", repo, other, "--slug", "widget"]) == 0
    shutil.rmtree(other)
    capsys.readouterr()

    assert _run(["set-primary", "widget", other]) == 2
    captured = capsys.readouterr()
    assert other in captured.err
    assert "does not exist" in captured.err
    assert "Set primary" not in captured.out
    with pdb.connect_closing() as conn:
        assert pdb.get_project(conn, "widget").primary_path == repo


def test_bind_board_refuses_a_board_slug_with_no_store(home, tmp_path, capsys):
    repo = _dir(tmp_path / "repo")
    assert _run(["create", "Widget", repo, "--slug", "widget"]) == 0
    capsys.readouterr()

    assert _run(["bind-board", "widget", "no-such-board"]) == 2
    captured = capsys.readouterr()
    assert "no-such-board" in captured.err
    assert "does not exist" in captured.err
    assert "Bound" not in captured.out
    with pdb.connect_closing() as conn:
        assert pdb.get_project(conn, "widget").board_slug is None  # the row was not written


def test_a_real_folder_and_a_real_board_are_still_accepted(home, tmp_path, capsys):
    """The refusals must not close the doors they guard."""
    from hermes_cli import kanban_db as kb

    repo = _dir(tmp_path / "repo")
    extra = _dir(tmp_path / "extra")
    assert _run(["create", "Widget", repo, "--slug", "widget"]) == 0
    assert _run(["add-folder", "widget", extra]) == 0
    kb.create_board("scoped", name="Scoped")

    assert _run(["bind-board", "widget", "scoped"]) == 0
    with pdb.connect_closing() as conn:
        proj = pdb.get_project(conn, "widget")
        assert proj.board_slug == "scoped"
        assert sorted(f.path for f in proj.folders) == sorted([repo, extra])
    assert kb.read_board_metadata("scoped")["default_workdir"] == repo

    # `default` always exists, and unbinding still clears the key.
    assert _run(["bind-board", "widget", "default"]) == 0
    assert _run(["bind-board", "widget"]) == 0
    with pdb.connect_closing() as conn:
        assert pdb.get_project(conn, "widget").board_slug is None




