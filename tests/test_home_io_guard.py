"""Controls for the home-I/O guard and the harness's hermes-bin hermeticity.

``tests/conftest.py::hermetic_hermes_bin_dir`` pins the ``hermes`` console-script dir
that every spawned local environment prepends to PATH to a per-session temp shim, so no
test reaches the host's live install. These controls are the counterweight: they prove
the pinning did not come from exempting the class ``tests/home_io_guard.py`` protects —
a write into a guarded home is still refused, and the PATH-entry exemption stays
metadata-only — and that the spawn path really resolves the temp shim.

Deliberately these never perform I/O against the real home: the guard's refusal is
observed at ``HomeIOGuard.check`` (the seam every installed wrapper calls) and, for the
write arm, end to end on a stand-in home installed exactly like a real one. A control
that actually attempted the write would write into the real home the day the guard broke.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tests.home_io_guard import HomeIOGuard


def _guard_roots() -> list[Path]:
    """The roots the autouse guard in tests/conftest.py is configured with.

    Read from the loaded conftest module (pytest may register it as ``tests.conftest``
    or ``conftest``); never re-import it, whose body sandboxes the environment.
    """
    for name in ("tests.conftest", "conftest"):
        module = sys.modules.get(name)
        roots = getattr(module, "_REAL_HERMES_ROOT_CANDIDATES", None)
        if roots:
            return list(roots)
    pytest.fail("tests/conftest.py did not capture a real hermes root to guard")


def test_the_real_home_is_a_guarded_root():
    """The configured roots still name the real platform home (not just a temp sandbox)."""
    from hermes_state_guard import _real_platform_state_root

    real = _real_platform_state_root()
    assert real is not None, "cannot resolve the real platform home on this host"
    roots = _guard_roots()
    assert any(str(Path(root).resolve()) == str(real) for root in roots), (
        f"the real home {real} is not a guarded root: {roots}")


def test_the_guard_refuses_a_write_into_the_real_home():
    """A destructive I/O against the real home is still refused (the class the guard exists for)."""
    root = _guard_roots()[0]
    target = Path(root) / "hermes-agent" / ".hermes" / "bin" / "conftest-control-probe"
    guard = HomeIOGuard(_guard_roots)
    with pytest.raises(AssertionError, match="REAL hermes home"):
        guard.check(os.fspath(target), destructive=True)


def test_the_guard_refuses_a_read_into_the_real_home():
    """Reads are guarded too: production state in a fixture is as much a leak as a write."""
    root = _guard_roots()[0]
    target = Path(root) / "hermes-agent" / ".hermes" / "bin" / "hermes"
    guard = HomeIOGuard(_guard_roots)
    with pytest.raises(AssertionError, match="REAL hermes home"):
        guard.check(os.fspath(target))


def test_the_installed_guard_refuses_open_for_write_under_its_root(tmp_path, monkeypatch):
    """End to end through the installed wrappers: ``open(<home>/f, "w")`` never lands."""
    home = tmp_path / "stand-in-home"
    home.mkdir()
    target = home / "written.txt"
    with monkeypatch.context() as installed:
        HomeIOGuard(lambda: [home]).install(installed)
        with pytest.raises(AssertionError, match="REAL hermes home"):
            open(target, "w", encoding="utf-8")
    # The wrappers are gone again: this stat is the outer guard's, showing nothing landed.
    assert not target.exists()


def test_the_path_entry_exemption_stays_metadata_only(tmp_path, monkeypatch):
    """A PATH entry under the home exempts command lookup, never a read or a write there.

    The guard lets a metadata probe of a PATH entry (and of its ancestors) through so
    ``shutil.which``/``realpath`` can find ``hermes`` under a home on PATH. That
    exemption must not widen into the entry's contents.
    """
    home = tmp_path / "home"
    bin_dir = home / "hermes-agent" / ".hermes" / "bin"
    bin_dir.mkdir(parents=True)
    shim = bin_dir / "hermes"
    shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    monkeypatch.setenv("PATH", os.pathsep.join([os.fspath(bin_dir), "/usr/bin"]))
    guard = HomeIOGuard(lambda: [home])
    guard.check(os.fspath(bin_dir), metadata=True)          # command lookup: allowed
    guard.check(os.fspath(home), metadata=True)             # ancestor walk: allowed
    with pytest.raises(AssertionError, match="REAL hermes home"):
        guard.check(os.fspath(shim))                        # reading the file: refused
    with pytest.raises(AssertionError, match="REAL hermes home"):
        guard.check(os.fspath(bin_dir), destructive=True)   # writing into it: refused


def test_the_spawn_path_resolves_the_hermes_bin_dir_to_the_temp_shim(hermetic_hermes_bin_dir):
    """Hermeticity: the bin dir the spawn path prepends is the temp shim, never the host install."""
    from tools.environments import local

    resolved = Path(local._resolve_hermes_bin_dir())
    assert resolved == Path(hermetic_hermes_bin_dir)
    assert any(p.name.startswith("hermes") for p in resolved.iterdir()), (
        f"the pinned bin dir carries no hermes console script: {list(resolved.iterdir())}")
    for root in _guard_roots():
        assert not resolved.is_relative_to(Path(root)), (
            f"the harness resolved a hermes bin dir inside a guarded home: {resolved}")


def test_a_spawned_environment_gets_the_temp_shim_on_path(hermetic_hermes_bin_dir, monkeypatch):
    """The env a local environment is spawned with carries the temp shim, so children run this checkout."""
    from tools.environments import local

    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    path = local._make_run_env({})["PATH"].split(os.pathsep)
    assert path[0] == os.fspath(hermetic_hermes_bin_dir), path


@pytest.mark.skipif(os.name == "nt", reason="the POSIX shim's exec line is what this asserts")
def test_the_shim_is_an_executable_hermes_that_runs_this_checkout(hermetic_hermes_bin_dir, tmp_path):
    """A child that reaches for ``hermes`` gets a working CLI from THIS tree, not the host's install."""
    import subprocess

    shim = Path(hermetic_hermes_bin_dir) / "hermes"
    assert os.access(shim, os.X_OK), f"{shim} is not executable"
    home = tmp_path / "shim-home"
    home.mkdir()
    result = subprocess.run(
        [os.fspath(shim), "--version"],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(tmp_path),
        # A scrubbed environment: if the shim needed the test process's PATH/PYTHONPATH it
        # would be resolving the host install after all.
        env={"PATH": "/usr/bin:/bin", "HOME": str(home), "HERMES_HOME": str(home)},
    )
    assert result.returncode == 0, result.stderr
    assert "Hermes Agent" in result.stdout
    assert os.fspath(Path(__file__).resolve().parent.parent) in result.stdout, (
        f"the shim ran a different install:\n{result.stdout}")
