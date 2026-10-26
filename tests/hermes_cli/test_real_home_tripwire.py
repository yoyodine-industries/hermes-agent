"""Real-home guards are exercised against disposable protected roots only."""
from __future__ import annotations

import io
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import textwrap

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def protected_home(tmp_path, monkeypatch):
    from tests import conftest

    root = tmp_path / "protected"
    root.mkdir()
    (root / "file.txt").write_text("unchanged", encoding="utf-8")
    (root / "empty").mkdir()
    monkeypatch.setattr(conftest, "_REAL_HERMES_ROOT_CANDIDATES", [root])
    yield root
    # Restore access before tmp_path/pytest cleanup even when an assertion fails.
    monkeypatch.setattr(conftest, "_REAL_HERMES_ROOT_CANDIDATES", [])


def _open_close(path):
    with open(path, encoding="utf-8"):
        pass


def _io_open_close(path):
    # io.open is a separate guarded entry point from builtins.open (HomeIOGuard wraps both);
    # bare `open` would only re-exercise the builtin-open case above.
    with io.open(path, encoding="utf-8"):  # noqa: UP020 — exercise io.open separately
        pass


def _os_open_close(path):
    descriptor = os.open(path, os.O_RDONLY)
    os.close(descriptor)


_OPERATIONS = {
    "builtin-open": _open_close,
    "io-open": _io_open_close,
    "os-open": _os_open_close,
    "read-text": lambda path: path.read_text(encoding="utf-8"),
    "write-text": lambda path: path.write_text("changed", encoding="utf-8"),
    "read-bytes": lambda path: path.read_bytes(),
    "write-bytes": lambda path: path.write_bytes(b"changed"),
    "stat": lambda path: path.stat(),
    "lstat": lambda path: path.lstat(),
    "unlink": lambda path: path.unlink(),
    "remove": lambda path: os.remove(path),
    "rename-out": lambda path: path.rename(path.parent.parent / "moved.txt"),
    "replace-out": lambda path: path.replace(path.parent.parent / "replaced.txt"),
    "mkdir": lambda path: (path.parent / "new").mkdir(),
    "makedirs": lambda path: os.makedirs(path.parent / "deep" / "child"),
    "rmdir": lambda path: (path.parent / "empty").rmdir(),
    "rmtree": lambda path: shutil.rmtree(path.parent),
    "listdir": lambda path: os.listdir(path.parent),
    "scandir": lambda path: list(os.scandir(path.parent)),
    "sqlite": lambda path: sqlite3.connect(path.parent / "state.db").close(),
}


@pytest.mark.parametrize("operation", _OPERATIONS, ids=_OPERATIONS)
def test_io_guard_denies_protected_roots_before_mutation(protected_home, operation):
    with pytest.raises((AssertionError, pytest.fail.Exception), match="REAL hermes home"):
        _OPERATIONS[operation](protected_home / "file.txt")


def test_rename_cannot_overwrite_a_protected_destination(protected_home, tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("external", encoding="utf-8")
    with pytest.raises((AssertionError, pytest.fail.Exception), match="REAL hermes home"):
        source.replace(protected_home / "file.txt")
    assert source.read_text(encoding="utf-8") == "external"


def test_installed_desktop_app_can_be_read_but_never_changed(tmp_path, monkeypatch):
    from tests import conftest

    app = tmp_path / "Applications" / "Hermes.app"
    (app / "Contents").mkdir(parents=True)
    plist = app / "Contents" / "Info.plist"
    plist.write_text("unchanged", encoding="utf-8")
    replacement = tmp_path / "Hermes.app.new"
    replacement.mkdir()
    changes = [lambda: shutil.rmtree(app), plist.unlink, lambda: replacement.replace(app),
               lambda: plist.write_text("clobbered", encoding="utf-8")]
    try:  # Windows without Developer Mode cannot create symlinks
        (tmp_path / "linked").symlink_to(app.parent, target_is_directory=True)
        changes.append(lambda: shutil.rmtree(tmp_path / "linked" / "Hermes.app"))
    except OSError:
        pass
    monkeypatch.setattr(conftest, "_REAL_INSTALLED_GUI_APPS", [os.path.normcase(os.path.realpath(app))])
    assert plist.read_text(encoding="utf-8") == "unchanged"
    for change in changes:
        with pytest.raises(pytest.fail.Exception, match="REAL installed Hermes desktop app"):
            change()
    assert plist.read_text(encoding="utf-8") == "unchanged"


def test_unprotected_paths_and_open_descriptors_still_work(protected_home, tmp_path):
    target = tmp_path / "allowed" / "file.txt"
    target.parent.mkdir()
    target.write_text("ok", encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "ok"
    fd = os.open(target, os.O_RDONLY)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        assert stream.read() == "ok"
    moved = target.replace(target.with_name("moved.txt"))
    assert list(moved.parent.iterdir()) == [moved]
    moved.unlink()
    moved.parent.rmdir()
    connection = sqlite3.connect(tmp_path / "allowed.db")
    connection.close()


@pytest.mark.allow_real_home_io
def test_explicit_opt_out_allows_only_the_disposable_canary(protected_home):
    target = protected_home / "file.txt"
    target.write_text("opted out", encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "opted out"


def test_close_keeps_a_reused_descriptors_new_owner(tmp_path, monkeypatch):
    from tests.home_io_guard import HomeIOGuard

    first, second = tmp_path / "first", tmp_path / "second"
    first.touch()
    second.touch()
    original_close = os.close
    reopened = []

    def close_and_reopen(fd):
        original_close(fd)
        reopened.append(os.open(second, os.O_RDONLY))

    guard = HomeIOGuard(list)
    try:
        with monkeypatch.context() as patcher:
            patcher.setattr(os, "close", close_and_reopen)
            guard.install(patcher)
            fd = os.open(first, os.O_RDONLY)
            os.close(fd)
            assert reopened == [fd], "the test must exercise descriptor reuse"
            assert guard.directories[fd] == second
    finally:
        for fd in reopened:
            original_close(fd)


def test_path_metadata_exemption_tracks_path_changes(protected_home, monkeypatch):
    from tests.home_io_guard import HomeIOGuard

    target = protected_home / "file.txt"
    guard = HomeIOGuard(lambda: [protected_home])
    monkeypatch.setenv("PATH", str(protected_home))
    guard.check(target, metadata=True)  # executable lookup, not a state read
    monkeypatch.setenv("PATH", str(protected_home.parent))
    with pytest.raises(AssertionError, match="REAL hermes home"):
        guard.check(target, metadata=True)


def test_relative_path_metadata_exemption_tracks_working_directory(protected_home, tmp_path, monkeypatch):
    from tests.home_io_guard import HomeIOGuard

    target = protected_home / "file.txt"
    guard = HomeIOGuard(lambda: [protected_home])
    other = tmp_path / "other"
    other.mkdir()
    (other / "nested").mkdir()
    monkeypatch.setenv("PATH", "../protected")
    monkeypatch.chdir(other)
    guard.check(target, metadata=True)
    monkeypatch.chdir(other / "nested")
    with pytest.raises(AssertionError, match="REAL hermes home"):
        guard.check(target, metadata=True)


def test_checkout_inside_a_guarded_root_is_not_hermes_state():
    """The default install checks the repo out INSIDE the home (install.sh:
    INSTALL_DIR=$HERMES_HOME/hermes-agent): the checkout, its .venv and test
    data are exempt even when the guarded root contains them; siblings under
    that root are still refused."""
    from tests.home_io_guard import HomeIOGuard

    guard = HomeIOGuard(lambda: [PROJECT_ROOT.parent])
    guard.check(PROJECT_ROOT / "tests" / "home_io_guard.py")
    guard.check(PROJECT_ROOT / ".venv" / "bin" / "python", metadata=True)
    with pytest.raises(AssertionError, match="REAL hermes home"):
        guard.check(PROJECT_ROOT.parent / "config.yaml")


def test_a_linked_worktree_git_dir_is_the_checkouts_own_bookkeeping(tmp_path):
    """`git worktree add` (the recipe every repair card prescribes: ``.worktrees/<card>``) replaces
    ``.git`` with a FILE holding ``gitdir: <dir>``, and that dir is
    ``<main>/.git/worktrees/<name>`` — INSIDE the guarded root in the default in-home layout. It
    holds this worktree's own HEAD/index/marker files: the operator's config, hooks and objects stay
    in the common dir one level up, which stays guarded.
    """
    from tests import home_io_guard

    checkout = tmp_path / "home" / "hermes-agent"
    checkout.mkdir(parents=True)
    assert home_io_guard._linked_worktree_git_dir(checkout) is None  # no .git at all
    (checkout / ".git").mkdir()
    assert home_io_guard._linked_worktree_git_dir(checkout) is None  # an ordinary clone
    (checkout / ".git").rmdir()

    git_dir = tmp_path / "home" / ".git" / "worktrees" / "t_card"
    git_dir.mkdir(parents=True)
    (checkout / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    assert home_io_guard._linked_worktree_git_dir(checkout) == git_dir.resolve()

    # Git writes an absolute pointer for `worktree add`; a relative one is relative to the checkout.
    (checkout / ".git").write_text("gitdir: ../.git/worktrees/t_card\n", encoding="utf-8")
    assert home_io_guard._linked_worktree_git_dir(checkout) == git_dir.resolve()


def test_the_worktree_git_dir_is_exempt_but_its_common_dir_is_not(tmp_path, monkeypatch):
    """The exemption is exactly the linked worktree's private dir, whose ``hermes-update-pull``
    marker the launch path stats. The common dir keeps the operator's config, hooks and objects, and
    an ANCESTOR of the private dir is not a stand-in for it.
    """
    from tests import home_io_guard

    home = tmp_path / "home"
    git_dir = home / ".git" / "worktrees" / "t_card"
    git_dir.mkdir(parents=True)
    (git_dir / "hermes-update-pull").write_text("pre", encoding="utf-8")
    (home / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    monkeypatch.setattr(home_io_guard, "_CHECKOUT_GIT_PREFIX_STRS",
                        (home_io_guard._normcase(os.fspath(git_dir.resolve())),))
    guard = home_io_guard.HomeIOGuard(lambda: [home])

    guard.check(git_dir / "hermes-update-pull", metadata=True)  # the marker probe at launch
    guard.check(git_dir / "hermes-update-pull")                 # ...and the read that follows
    for common in (home / ".git" / "config", home / ".git" / "worktrees", home / ".git"):
        with pytest.raises(AssertionError, match="REAL hermes home"):
            guard.check(common)


def test_the_payload_manifest_beside_a_checkout_is_read_but_never_written(tmp_path, monkeypatch):
    """pm's ``_payload_manifest`` reads ``<checkout>.parent/manifest.json`` at every launch and
    DISCARDS it unless its ``repo`` names this very tree. A read describes the checkout in hand (the
    same class as its .venv); a write would let a test corrupt the operator's install record.
    """
    from tests import home_io_guard

    home = tmp_path / "home"
    (home / "hermes-agent").mkdir(parents=True)
    manifest = home / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(home_io_guard, "_CHECKOUT_PAYLOAD_MANIFESTS",
                        frozenset({home_io_guard._normcase(os.fspath(manifest.resolve()))}))
    guard = home_io_guard.HomeIOGuard(lambda: [home])

    guard.check(manifest, metadata=True)
    guard.check(manifest)
    with pytest.raises(AssertionError, match="REAL hermes home"):
        guard.check(manifest, destructive=True)
    with pytest.raises(AssertionError, match="REAL hermes home"):
        guard.check(home / "sibling.json")


def test_running_from_a_linked_worktree_still_guards_the_common_dir():
    """Inverse of the exemption, on the REAL tree: when this suite runs from a linked worktree, that
    worktree's private git dir is exempt but the common dir holding the operator's config is not.
    Skipped when the run is not from a worktree (a plain checkout has no private dir to exempt).
    """
    from tests import home_io_guard

    git_dir = home_io_guard._worktree_git_dir
    if git_dir is None:
        pytest.skip("this run is not from a linked worktree")
    common_dir = git_dir.parents[1]
    guard = home_io_guard.HomeIOGuard(lambda: [common_dir])
    with pytest.raises(AssertionError, match="REAL hermes home"):
        guard.check(common_dir / "config")


def test_hermes_exported_scratch_tmp_is_not_the_test_temp_root(tmp_path):
    """A Hermes-launched shell hands pytest TMPDIR=<home>/cache/scratch (tagged by
    HERMES_SCRATCH_DIR). With that home guarded, honoring it would put the session
    sandbox, basetemp and every tempfile default inside the guarded root; the
    conftest must drop Hermes' own export before anything allocates temp space."""
    home = tmp_path / "home"
    scratch = home / "cache" / "scratch"
    scratch.mkdir(parents=True)
    probe = tmp_path / "test_probe.py"
    probe.write_text(textwrap.dedent(f"""
        import tempfile
        from pathlib import Path

        def test_temp_root_is_outside_the_honored_home():
            home = Path({str(home)!r}).resolve()
            assert not Path(tempfile.gettempdir()).resolve().is_relative_to(home)
            with tempfile.TemporaryDirectory() as made:
                assert not Path(made).resolve().is_relative_to(home)
        """), encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if k not in ("TMPDIR", "TMP", "TEMP", "HERMES_SCRATCH_DIR", "HERMES_TEST_SANDBOX_HOME")}
    env.update(HERMES_HOME=str(home), TMPDIR=str(scratch), HERMES_SCRATCH_DIR=str(scratch))
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "tests.conftest", "-p", "no:cacheprovider", "-q", str(probe)],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
