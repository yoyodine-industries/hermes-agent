"""``create_task`` refuses a non-live, undeclared assignee — fail-closed.

Ruling §4 on ops/t_5b9dbe02 (platform-stl, 2026-10-02). The named deterministic
enforcement point is ``hermes_cli/kanban_db.py::create_task`` — the single create
path every producer reaches (CLI ``hermes kanban create``, the ``kanban_create``
tool, the dispatcher's own filers, the DAG/train filers). It consults the SAME
predicate the dispatcher consults (``hermes_cli.profiles.profile_exists``) and
REJECTS the write for an unknown assignee. The declared escape is
``kanban.control_plane_assignees`` — default empty, and an unreadable config
claims nothing.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


def _isolated_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _no_declaration():
    return frozenset()


def _declares(*names):
    return lambda: frozenset(names)


def _live_only(*names):
    from hermes_cli import profiles
    return lambda name: name in names


def test_create_refuses_unknown_assignee_and_names_the_handle(tmp_path, monkeypatch):
    _isolated_home(tmp_path, monkeypatch)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: False)
    monkeypatch.setattr(kb, "control_plane_assignee_names", _no_declaration)
    with kbc.connect() as conn:
        with pytest.raises(ValueError) as excinfo:
            kb.create_task(conn, title="demo", assignee="ghost-profile")
        rows = conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
    message = str(excinfo.value)
    assert "ghost-profile" in message
    assert "kanban.control_plane_assignees" in message
    assert rows == 0, "a refused create must not write a row"


def test_create_accepts_a_live_profile(tmp_path, monkeypatch):
    _isolated_home(tmp_path, monkeypatch)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", _live_only("ops-stl"))
    monkeypatch.setattr(kb, "control_plane_assignee_names", _no_declaration)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="demo", assignee="ops-stl")
    assert tid


def test_create_accepts_a_declared_control_plane_name(tmp_path, monkeypatch):
    _isolated_home(tmp_path, monkeypatch)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: False)
    monkeypatch.setattr(kb, "control_plane_assignee_names", _declares("coder"))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="demo", assignee="coder")
    assert tid


def test_create_accepts_no_assignee_at_all(tmp_path, monkeypatch):
    """None is legal: the dispatcher's ``default_assignee`` fills it in."""
    _isolated_home(tmp_path, monkeypatch)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: False)
    monkeypatch.setattr(kb, "control_plane_assignee_names", _no_declaration)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="demo")
    assert tid


def test_create_refuses_when_the_declaration_list_is_absent(tmp_path, monkeypatch):
    """The REAL reader, against a config that carries no declaration: absent = empty."""
    _isolated_home(tmp_path, monkeypatch)
    monkeypatch.setattr(kb, "control_plane_assignee_names", kb._control_plane_assignees_from_config)
    monkeypatch.setattr(
        "hermes_cli.config_effective.load_user_config_effective",
        lambda *a, **k: {"kanban": {"dispatch_profiles": None}},
    )
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: False)
    assert kb.control_plane_assignee_names() == frozenset()
    with kbc.connect() as conn:
        with pytest.raises(ValueError):
            kb.create_task(conn, title="demo", assignee="ghost-profile")


def test_declaration_reader_is_fail_closed_when_the_config_cannot_be_read(
    tmp_path, monkeypatch,
):
    _isolated_home(tmp_path, monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("corrupt config")

    monkeypatch.setattr(
        "hermes_cli.config_effective.load_user_config_effective", _boom,
    )
    assert kb._control_plane_assignees_from_config() == frozenset()


def test_declaration_reader_parses_a_list_and_a_csv_string(tmp_path, monkeypatch):
    _isolated_home(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "hermes_cli.config_effective.load_user_config_effective",
        lambda *a, **k: {"kanban": {"control_plane_assignees": ["Coder", "owner-stl"]}},
    )
    assert kb._control_plane_assignees_from_config() == {"coder", "owner-stl"}
    monkeypatch.setattr(
        "hermes_cli.config_effective.load_user_config_effective",
        lambda *a, **k: {"kanban": {"control_plane_assignees": "coder, probe-unassignable"}},
    )
    assert kb._control_plane_assignees_from_config() == {"coder", "probe-unassignable"}


def test_declaration_reader_treats_an_empty_value_as_no_declaration(tmp_path, monkeypatch):
    _isolated_home(tmp_path, monkeypatch)
    for value in ([], None, "", "   "):
        monkeypatch.setattr(
            "hermes_cli.config_effective.load_user_config_effective",
            lambda *a, _v=value, **k: {"kanban": {"control_plane_assignees": _v}},
        )
        assert kb._control_plane_assignees_from_config() == frozenset()
