"""The test-environment preflight answers the environment question, once.

Regression scope (card t_cf7c63c0): a shared venv lacking the runtime deps
produced 102 collection errors in tests/hermes_cli and 7 in tests/tools, all of
them true about the interpreter and all readable as failures of the tree. The
preflight turns that wall into one typed refusal naming every absent module.

These tests exercise the preflight directly; they never build a venv.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from tests import preflight

ROOT = Path(__file__).resolve().parents[1]


def test_complete_environment_checks_clean(monkeypatch):
    """A satisfiable declaration produces no refusal."""
    assert preflight.check() == ""


def test_one_absent_module_is_one_typed_line(monkeypatch):
    """The fixture: a module that cannot exist produces exactly one refusal naming it."""
    monkeypatch.setenv(preflight.EXTRA_REQUIRE_ENV, "no_such_module_t_cf7c63c0")
    line = preflight.check()

    assert line.startswith("error_class=%s " % preflight.ERROR_CLASS)
    assert "modules=no_such_module_t_cf7c63c0" in line
    # The line names its interpreter, so the verdict is attributable.
    assert "interpreter=%s" % sys.executable in line
    assert "\n" not in line


def test_several_absent_modules_are_named_together(monkeypatch):
    """Two gaps are still ONE refusal: the reader gets the whole install list."""
    monkeypatch.setenv(preflight.EXTRA_REQUIRE_ENV, "no_such_a_t_cf7c63c0,no_such_b_t_cf7c63c0")
    line = preflight.check()

    assert line.count("error_class=") == 1
    assert "modules=no_such_a_t_cf7c63c0,no_such_b_t_cf7c63c0" in line


def test_require_exits_typed_and_distinct(monkeypatch, capsys):
    """A runner can branch on the exit code: 3 is env-incomplete, not a test failure."""
    monkeypatch.setenv(preflight.EXTRA_REQUIRE_ENV, "no_such_module_t_cf7c63c0")
    with pytest.raises(SystemExit) as excinfo:
        preflight.require()

    assert excinfo.value.code == preflight.EXIT_ENV_INCOMPLETE
    assert preflight.ERROR_CLASS in capsys.readouterr().err


def test_extra_requirement_env_is_parsed_leniently(monkeypatch):
    """Spaces and empty segments from a shell never become phantom modules."""
    monkeypatch.setenv(preflight.EXTRA_REQUIRE_ENV, " pytest , ,sys ")
    assert [row.module for row in preflight.extra_required()] == ["pytest", "sys"]


def test_cli_reports_zero_when_complete():
    """The standalone entry point is usable from a lane's shell."""
    proc = subprocess.run(
        [sys.executable, "-m", "tests.preflight"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "preflight=ok" in proc.stdout
    assert "interpreter=" in proc.stdout


def test_every_declared_module_is_a_declared_dependency():
    """Contract: the declaration and the packaging cannot drift apart.

    Each row's distribution must be pinned in the repo's own dependency
    declaration (the ``test`` group or a runtime dependency), so the fix for a
    gap is always 'build the declared environment', never 'pip install whatever'.
    """
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = " ".join(data["project"]["dependencies"])
    groups = data.get("dependency-groups", {})
    for name in ("test", "dev"):
        declared += " " + " ".join(groups.get(name, []))
    for specs in data["project"].get("optional-dependencies", {}).values():
        declared += " " + " ".join(specs)

    def _norm(name: str) -> str:
        return re.sub(r"[-_.]+", "-", name).lower()

    declared_norm = _norm(declared)
    missing_rows = [
        row.distribution
        for row in preflight.DECLARED
        if _norm(row.distribution) not in declared_norm
    ]
    assert missing_rows == [], "not declared in pyproject: %s" % missing_rows
