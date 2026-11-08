"""Authoring identity for kanban writes (regression: every comment authored "default").

A gateway-served session has NO ``HERMES_PROFILE`` in ``os.environ`` (the served profile
lives in the session ContextVar) and its ``HERMES_HOME`` is the DEFAULT root, so the CLI
fallback named ``default`` for the author of every comment it wrote. The resolution rule
under test (``hermes_cli.profiles.resolve_acting_profile_name``):

    HERMES_PROFILE_NAME -> HERMES_PROFILE -> bound session profile (HERMES_SESSION_PROFILE)
    -> profile id derived from the active HERMES_HOME -> fallback

The child-process half of the fix (exporting the session profile as ``HERMES_PROFILE``
next to the profile-scoped ``HERMES_HOME``) is covered in
``tests/tools/test_build_subprocess_env.py``; the tool-path half in
``tests/tools/test_kanban_tools.py``.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def clean_identity_env(monkeypatch, tmp_path):
    """Env with no identity export and a DEFAULT-shaped HERMES_HOME (gateway-like)."""
    monkeypatch.delenv("HERMES_PROFILE_NAME", raising=False)
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.delenv("HERMES_SESSION_PROFILE", raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _bind(profile: str):
    from gateway.session_context import set_session_vars
    return set_session_vars(profile=profile)


def _unbind(tokens):
    from gateway.session_context import clear_session_vars, reset_session_vars
    clear_session_vars(tokens)
    reset_session_vars()  # back to "never bound here" so later tests keep the os.environ mirror


# ---------------------------------------------------------------------------
# Resolver order
# ---------------------------------------------------------------------------

def test_resolver_prefers_the_bound_session_profile_over_the_default_home(
    clean_identity_env,
):
    """The regression: home-derived name only says ``default`` for a served profile."""
    from hermes_cli.profiles import get_active_profile_name, resolve_acting_profile_name

    assert get_active_profile_name() == "default"  # the old answer, from HERMES_HOME
    tokens = _bind("ops-coder")
    try:
        assert resolve_acting_profile_name("user") == "ops-coder"
    finally:
        _unbind(tokens)


def test_resolver_env_exports_win_over_the_bound_session(clean_identity_env, monkeypatch):
    """An explicit export wins, in the documented order: NAME then PROFILE then session."""
    from hermes_cli.profiles import resolve_acting_profile_name

    tokens = _bind("session-profile")
    try:
        assert resolve_acting_profile_name() == "session-profile"
        monkeypatch.setenv("HERMES_PROFILE", "dispatched-worker")
        assert resolve_acting_profile_name() == "dispatched-worker"
        monkeypatch.setenv("HERMES_PROFILE_NAME", "lane-name")
        assert resolve_acting_profile_name() == "lane-name"
    finally:
        _unbind(tokens)


def test_resolver_falls_back_to_the_home_derived_profile(clean_identity_env, monkeypatch,
                                                         tmp_path):
    """No env export and no session bound (``hermes -p X kanban comment``): the
    profile-scoped HERMES_HOME alone still names X."""
    from hermes_cli.profiles import resolve_acting_profile_name

    prof = tmp_path / ".hermes" / "profiles" / "ops-coder"
    prof.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(prof))
    assert resolve_acting_profile_name("user") == "ops-coder"


def test_resolver_never_raises(monkeypatch):
    from hermes_cli.profiles import resolve_acting_profile_name

    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.delenv("HERMES_PROFILE_NAME", raising=False)
    assert resolve_acting_profile_name("fallback") == "fallback"


# ---------------------------------------------------------------------------
# CLI surface (hermes_cli/kanban.py) — the author written by ``kanban comment``
# ---------------------------------------------------------------------------

def test_cli_profile_author_uses_the_bound_session_profile(clean_identity_env):
    from hermes_cli.kanban import _profile_author

    tokens = _bind("ops-coder")
    try:
        assert _profile_author() == "ops-coder"
    finally:
        _unbind(tokens)


def test_cli_profile_author_keeps_the_dispatcher_pin(clean_identity_env, monkeypatch):
    """The kanban dispatcher pins HERMES_PROFILE on spawned workers; that stays the name."""
    from hermes_cli.kanban import _profile_author

    monkeypatch.setenv("HERMES_PROFILE", "platform-coder")
    assert _profile_author() == "platform-coder"


def test_cli_comment_records_the_session_profile_as_the_author(
    clean_identity_env, monkeypatch, tmp_path
):
    """End of the CLI path: the row actually written to the board carries the name."""
    import argparse

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli.kanban import _cmd_comment

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="cli-author", assignee="platform-coder")
    finally:
        conn.close()

    tokens = _bind("ops-coder")
    try:
        rc = _cmd_comment(argparse.Namespace(
            task_id=tid, text=["hello", "board"], author=None, max_len=None))
        assert rc == 0
    finally:
        _unbind(tokens)

    conn = kbc.connect()
    try:
        rows = kb.list_comments(conn, tid)
    finally:
        conn.close()
    assert [(r.author, r.body) for r in rows] == [("ops-coder", "hello board")]
