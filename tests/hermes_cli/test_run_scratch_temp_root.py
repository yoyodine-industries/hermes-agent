"""A dispatched worker's run-scoped temp root must survive hermes-agent's OWN guards.

``kanban_db_dispatch._default_spawn`` points ``TMPDIR``/``TMP``/``TEMP`` at
``<HERMES_HOME>/cache/scratch/kanban-run-<task>-<run>``, and ``<HERMES_HOME>`` is
``~/.hermes/profiles/<lane>`` — inside the guarded production root. Two mechanisms in this
suite's own machinery used to throw that root away, so hermes-agent kept writing pytest
basetemps into the shared account temp root the run root exists to keep them out of
(measured 2026-10-05: ~17 GiB there, 98% ``pytest-of-hermes_user/`` basetemps):

1. ``tests/conftest.py`` deleted the temp var when it resolved inside a guarded root;
2. ``tests/home_io_guard.py`` refused every write under a guarded root.

``cache/scratch`` is a runtime cache the worker is MEANT to write and is already reaped by
the 24 h idle prune — unlike the state (sessions, kanban stores, memories, config) the
guard exists to protect. Both exemptions are scoped to it, and every positive case below
has a negative control beside it so a too-broad exemption cannot pass silently.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def guarded_home(tmp_path, monkeypatch):
    """A disposable directory registered as the guarded real Hermes root.

    Mirrors ``test_real_home_tripwire.py``'s ``protected_home``: writes happen BEFORE the
    root is registered so the fixture's own setup is not what trips the guard.
    """
    from tests import conftest

    root = tmp_path / "protected"
    root.mkdir()
    (root / "file.txt").write_text("unchanged", encoding="utf-8")
    (root / "sessions").mkdir()
    monkeypatch.setattr(conftest, "_REAL_HERMES_ROOT_CANDIDATES", [root])
    yield root
    monkeypatch.setattr(conftest, "_REAL_HERMES_ROOT_CANDIDATES", [])


def _guard(root):
    from tests.home_io_guard import HomeIOGuard

    return HomeIOGuard(lambda: [root])


# ── I/O guard: cache/scratch is permitted, everything else under the root still refused ──

def test_guard_permits_writes_under_the_root_scratch(guarded_home):
    guard = _guard(guarded_home)
    scratch = guarded_home / "cache" / "scratch" / "kanban-run-t_x-1"
    scratch.mkdir(parents=True)
    guard.check(scratch, destructive=True)
    guard.check(scratch / "work.txt", destructive=True)


def test_guard_permits_profile_scratch_under_the_production_root(guarded_home):
    guard = _guard(guarded_home)
    scratch = (guarded_home / "profiles" / "platform-coder" / "cache" / "scratch"
               / "kanban-run-t_x-1")
    scratch.mkdir(parents=True)
    guard.check(scratch, destructive=True)
    guard.check(scratch / "work.txt", destructive=True)


def test_guard_still_refuses_state_beside_the_scratch(guarded_home):
    """Negative control: the exemption is scoped to ``cache/scratch`` only.

    State under the guarded root, a non-scratch sibling under the SAME profile, and a
    ``cache/`` entry that is not ``scratch`` all keep being refused.
    """
    guard = _guard(guarded_home)
    for state in (
        guarded_home / "sessions" / "state.db",
        guarded_home / "profiles" / "platform-coder" / "config.yaml",
        guarded_home / "cache" / "other" / "x",
        guarded_home / "cache" / "scratchy" / "x",
    ):
        with pytest.raises(AssertionError, match="REAL hermes home"):
            guard.check(state, destructive=True)
    # The ancestor allowance is for metadata/non-destructive calls only: DELETING the
    # chain dir is still refused.
    with pytest.raises(AssertionError, match="REAL hermes home"):
        guard.check(guarded_home / "cache", destructive=True)
    with pytest.raises(AssertionError, match="REAL hermes home"):
        guard.check(guarded_home / "profiles" / "platform-coder" / "cache", destructive=True)


# ── conftest strip: the run scratch survives, a state-root temp var still goes ──

def _run_conftest_probe(tmp_path, home, tmpdir_value, probe_body):
    """Run one probe test in a subprocess whose conftest is the tree under test."""
    probe = tmp_path / "test_probe.py"
    probe.write_text(textwrap.dedent(probe_body), encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if k not in ("TMPDIR", "TMP", "TEMP", "HERMES_SCRATCH_DIR",
                        "HERMES_TEST_SANDBOX_HOME", "HERMES_HOME")}
    env.update(HERMES_HOME=str(home), TMPDIR=str(tmpdir_value))
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "tests.conftest",
         "-p", "no:cacheprovider", "-q", str(probe)],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=120,
    )


def test_run_scoped_tmpdir_survives_the_conftest_strip(tmp_path):
    """The dispatcher's run root (no HERMES_SCRATCH_DIR marker) is honored, not scrubbed."""
    home = tmp_path / "home"
    run_root = home / "cache" / "scratch" / "kanban-run-t_x-1"
    run_root.mkdir(parents=True)
    result = _run_conftest_probe(tmp_path, home, run_root, """
        import os
        import tempfile
        from pathlib import Path

        def test_temp_root_follows_the_run_scratch():
            run_root = Path({run_root!r}).resolve()
            assert Path(os.environ["TMPDIR"]).resolve() == run_root
            assert Path(tempfile.gettempdir()).resolve() == run_root
            with tempfile.TemporaryDirectory() as made:
                assert Path(made).resolve().is_relative_to(run_root)
        """.format(run_root=str(run_root)))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


def test_state_root_tmpdir_is_still_stripped(tmp_path):
    """Negative control: a temp var under the home but NOT under cache/scratch is stripped."""
    home = tmp_path / "home"
    state = home / "state"
    state.mkdir(parents=True)
    result = _run_conftest_probe(tmp_path, home, state, """
        import os
        from pathlib import Path

        def test_state_temp_root_is_stripped():
            home = Path({home!r}).resolve()
            value = os.environ.get("TMPDIR", "")
            assert value, "the conftest must re-pin TMPDIR after stripping"
            assert not Path(value).resolve().is_relative_to(home)
        """.format(home=str(home)))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
