"""Behaviour contracts for ``hermes python`` (hermes_cli/subcommands/python.py).

``hermes python`` exists so "which interpreter runs this" is a DECLARATION that is
ASSERTED before a path is handed out. These tests pin the three things that make that
true: the resolution follows the host's config (not a path baked into a script), a role
that cannot satisfy its declaration refuses and names the miss, and an unconfigured role
is a refusal rather than a plausible default.
"""

from __future__ import annotations

import json
import shlex
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from hermes_cli.subcommands import python as python_cmd


def _config(role: str, interpreter: str, modules=("yaml",), min_version: str = "3.8"):
    return {"python": {"roles": {role: {
        "interpreter": interpreter, "min_version": min_version, "modules": list(modules),
    }}}}


def _interpreter_at(path: Path) -> Path:
    """An executable that stands in for a configured interpreter.

    A wrapper (not a symlink) because a bare symlink to a venv interpreter is not a usable
    python: the venv is found relative to the real path. The wrapper execs the interpreter
    running the tests, so the resolver's probe gets a truthful answer about that python.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\nexec %s "$@"\n' % shlex.quote(sys.executable))
    path.chmod(0o755)
    return path


def test_resolution_follows_the_configured_role_not_a_baked_in_path(tmp_path):
    """The mapping lives in config: change config, the resolved path changes with it."""
    first = _interpreter_at(tmp_path / "one" / "python")
    second = _interpreter_at(tmp_path / "two" / "python")

    resolved = python_cmd.resolve("ops", config=_config("ops", str(first), modules=()))
    assert resolved["interpreter"] == str(first)

    resolved = python_cmd.resolve("ops", config=_config("ops", str(second), modules=()))
    assert resolved["interpreter"] == str(second)
    assert resolved["interpreter"] != str(first)

    # A role the shipped defaults do not define exists only because config declares it.
    with pytest.raises(python_cmd.UnknownRoleError):
        python_cmd.resolve("fleet_extra", config=_config("ops", str(first), modules=()))
    resolved = python_cmd.resolve("fleet_extra", config=_config("fleet_extra", str(first),
                                                               modules=()))
    assert resolved["interpreter"] == str(first)


def test_a_role_that_cannot_satisfy_its_declaration_refuses_and_names_the_miss(monkeypatch,
                                                                              capsys):
    """A declared module that will not import is a MISS, never a silently blind run."""
    config = _config("ops", sys.executable, modules=["nosuchtool_xyz"])

    with pytest.raises(python_cmd.DeclarationError) as raised:
        python_cmd.resolve("ops", config=config)
    message = str(raised.value)
    assert sys.executable in message, "the refusal must name the interpreter it refused"
    assert "nosuchtool_xyz" in message, "the refusal must name the miss"

    # The caller's own declaration is asserted too, and it names the caller's miss.
    with pytest.raises(python_cmd.DeclarationError) as needed:
        python_cmd.resolve("ops", need=["alsomissing_xyz"],
                           config=_config("ops", sys.executable, modules=[]))
    assert "alsomissing_xyz" in str(needed.value)

    # A refusal reaches the shell as a non-zero exit with the reason on stderr — a script
    # that captured the path instead would be running blind.
    def _refuse(*_a, **_k):
        raise python_cmd.DeclarationError("role 'ops': interpreter %s is missing psutil" % sys.executable)

    monkeypatch.setattr(python_cmd, "resolve", _refuse)
    rc = python_cmd.cmd_python(Namespace(role="ops", need="", json=False, export=False))
    assert rc == python_cmd.EXIT_DECLARATION_FAILED
    captured = capsys.readouterr()
    assert captured.out.strip() == "", "a refused resolution must print no path"
    assert "psutil" in captured.err


def test_the_runtime_role_defaults_to_the_interpreter_running_this_cli():
    """`runtime` needs no pinned path: the CLI's own interpreter satisfies the tree."""
    resolved = python_cmd.resolve("runtime", config={})
    assert resolved["interpreter"] == sys.executable


def test_an_unconfigured_role_is_refused_rather_than_resolved_to_a_default():
    with pytest.raises(python_cmd.UnknownRoleError) as raised:
        python_cmd.resolve("no_such_role", config={})
    assert "no_such_role" in str(raised.value)

    rc = python_cmd.cmd_python(Namespace(role="no_such_role", need="", json=False,
                                        export=False))
    assert rc == python_cmd.EXIT_UNKNOWN_ROLE


def test_export_and_json_speak_the_shell_contract(monkeypatch, capsys):
    """`--export` is eval-able by a shell; `--json` carries the same path."""
    interpreter = sys.executable
    asserted = {"role": "runtime", "interpreter": interpreter, "version": "3.11.15",
                "min_version": "3.11", "modules": ["yaml"]}
    monkeypatch.setattr(python_cmd, "resolve", lambda *a, **k: dict(asserted))

    rc = python_cmd.cmd_python(Namespace(role="runtime", need="", json=False, export=True))
    assert rc == python_cmd.EXIT_OK
    assert capsys.readouterr().out.strip() == "export HERMES_PYTHON='%s'" % interpreter

    rc = python_cmd.cmd_python(Namespace(role="runtime", need="", json=False, export=False))
    assert rc == python_cmd.EXIT_OK
    assert capsys.readouterr().out.strip() == interpreter

    rc = python_cmd.cmd_python(Namespace(role="runtime", need="", json=True, export=False))
    assert rc == python_cmd.EXIT_OK
    assert json.loads(capsys.readouterr().out)["interpreter"] == interpreter
