"""A test must never reach a LIVE per-profile project store (t_7213e415).

``projects.db`` is per-profile live state: ``$HERMES_HOME/projects.db`` — the
launch home's store, and one for every ``profiles/<lane>`` home. A pytest run
launched from a worker session (``HERMES_HOME=<root>/profiles/platform-coder``)
once left a project row in platform-coder's REAL registry whose only folder was
``…/pytest-2664/test_both_verbs_pass_the_regis0/sandbox/demo/alpha`` — a pytest
``tmp_path``. The row survived for days because a test that can pass while
writing production state is not failing anywhere.

There are two halves to the repair, and this file is the canary for both:

* **the knob.** ``hermes project`` resolves its store through
  ``get_hermes_home()``, i.e. ``HERMES_HOME`` — *not* ``HOME``. Sandboxing the
  wrong one is how the leak slipped through, so the assertion is spelled out.
* **the guard.** The autouse ``_forbid_production_project_store_writes`` fixture
  in ``tests/conftest.py`` fingerprints every production project store (and its
  ``-wal``/``-shm`` sidecars) around each test and fails the one that changed
  one, whatever mechanism reached it — in-process call or CLI subprocess.

The guard's own tests are marked ``allow_real_project_store_io`` so they are
never the thing they measure; every write in this file targets ``tmp_path``.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from hermes_cli import projects_db
from tests import conftest as suite_conftest

pytestmark = pytest.mark.allow_real_project_store_io

REPO_ROOT = Path(__file__).resolve().parent.parent


# ──────────────────────────── the knob ─────────────────────────────


def test_the_project_verb_reads_HERMES_HOME_not_HOME(tmp_path, monkeypatch):
    """``HERMES_HOME`` is the knob, and ``HOME`` is not: the store is exactly
    ``$HERMES_HOME/projects.db`` no matter where HOME points.

    A test that sandboxes HOME and leaves HERMES_HOME alone still resolves the
    real store — the wrong-knob failure this file exists to catch.
    """
    sandbox = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(sandbox))
    assert projects_db.projects_db_path() == sandbox / "projects.db"

    monkeypatch.setenv("HOME", str(tmp_path / "home-only"))
    assert projects_db.projects_db_path() == sandbox / "projects.db", "HOME moved the store"

    # Unset, it falls back to the platform default root — which this session
    # sandboxes. It must never be the operator's own home.
    monkeypatch.delenv("HERMES_HOME", raising=False)
    fallback = projects_db.projects_db_path()
    assert fallback.name == "projects.db"
    inherited_home = Path(suite_conftest._PRE_SANDBOX_HERMES_HOME or "/")
    assert not fallback.is_relative_to(inherited_home), f"fell back to the inherited home: {fallback}"


def test_a_profile_shaped_HERMES_HOME_is_where_the_lane_store_lives(tmp_path, monkeypatch):
    """The leaked shape: a per-profile home is just an HERMES_HOME with a
    ``projects.db`` in it — and it must be the *sandbox* one in a test."""
    lane_home = tmp_path / "root" / "profiles" / "platform-coder"
    lane_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(lane_home))

    store = projects_db.projects_db_path()
    assert store == lane_home / "projects.db"
    assert store.is_relative_to(tmp_path), "the store must resolve inside tmp_path"
    assert not store.is_relative_to(Path(os.environ["HOME"]) / ".hermes")


# ──────────────────────────── the guard ────────────────────────────


def test_the_guard_is_armed_for_every_test(request, tmp_path):
    """A guard that watches nothing is a false green: prove it is installed and
    that it watches the live layout it claims to."""
    assert "_forbid_production_project_store_writes" in request.fixturenames
    real_roots = [Path(root) for root in suite_conftest._REAL_HERMES_ROOT_CANDIDATES]
    assert real_roots, "the real Hermes root must be captured before the sandbox"

    # Fingerprints are stat'd with the HomeIOGuard suspended, which is how the
    # fixture can measure the real tree without reading live state.
    watched = suite_conftest._project_store_fingerprints()
    for root in real_roots:
        assert str(root / "projects.db") in watched, f"{root} is live state but unguarded"

    # Per-lane stores come from the same captured roots, discovered by globbing
    # the layout (never the real tree).
    fake_root = tmp_path / "root"
    lane_store = fake_root / "profiles" / "some-lane" / "projects.db"
    lane_store.parent.mkdir(parents=True)
    lane_store.touch()
    assert suite_conftest._project_store_paths_for_roots([fake_root]) == [
        fake_root / "projects.db",
        lane_store,
    ]


def test_the_guard_detects_a_write_to_a_watched_store(tmp_path, monkeypatch):
    """The detector itself: a real ``hermes project`` write shows up as a change,
    and a read-only open does not."""
    store = tmp_path / "live" / "projects.db"
    store.parent.mkdir()
    with projects_db.connect_closing(store) as conn:
        projects_db.create_project(conn, name="Seed", folders=[str(tmp_path / "seed")])

    monkeypatch.setattr(suite_conftest, "_production_project_store_paths", lambda: [store])

    before = suite_conftest._project_store_fingerprints()
    with projects_db.connect_closing(store) as conn:
        projects_db.create_project(conn, name="Leaked", folders=[str(tmp_path / "leaked")])
    after = suite_conftest._project_store_fingerprints()

    changed = suite_conftest._project_store_changes(before, after)
    assert str(store) in changed
    message = suite_conftest._project_store_failure(changed, "tests/x.py::test_y")
    assert "LIVE Hermes project store" in message
    assert str(store) in message
    assert "HERMES_HOME" in message

    # Control: reading the same store changes nothing, so the guard is a write
    # detector rather than a "touched the file at all" alarm.
    stable = suite_conftest._project_store_fingerprints()
    with projects_db.connect_closing(store) as conn:
        projects_db.list_projects(conn)
    assert suite_conftest._project_store_changes(stable, suite_conftest._project_store_fingerprints()) == []


def test_the_guard_watches_the_sidecars_a_wal_write_lands_in(tmp_path, monkeypatch):
    """A WAL commit can leave the main file untouched; the fingerprint must
    include ``-wal``/``-shm`` or a real write goes unseen."""
    store = tmp_path / "live" / "projects.db"
    store.parent.mkdir()
    with projects_db.connect_closing(store) as conn:
        projects_db.create_project(conn, name="Seed", folders=[str(tmp_path / "seed")])

    monkeypatch.setattr(suite_conftest, "_production_project_store_paths", lambda: [store])
    fingerprints = suite_conftest._project_store_fingerprints()
    assert f"{store}-wal" in fingerprints and f"{store}-shm" in fingerprints


# ───────────────────── the guard fires on a real run ─────────────────────


def _run_child_pytest(tmp_path: Path, test_body: str, store: Path) -> subprocess.CompletedProcess:
    """Run ``test_body`` in a CHILD pytest that loads this repo's conftest.

    The child test lives outside ``tests/``, so the conftest is loaded as a
    plugin (``-p tests.conftest``) and its autouse guard applies exactly as it
    does to every real test file. The child's own conftest redirects the guard
    at a throwaway store, so nothing outside ``tmp_path`` is at risk.
    """
    child = tmp_path / "child"
    child.mkdir()
    (child / "test_child.py").write_text(textwrap.dedent(test_body), encoding="utf-8")
    (child / "conftest.py").write_text(
        textwrap.dedent(
            """
            import os
            from pathlib import Path

            from tests import conftest as suite_conftest

            suite_conftest._production_project_store_paths = lambda: [Path(os.environ["FAKE_STORE"])]
            """
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["FAKE_STORE"] = str(store)
    env.pop("PYTEST_ADDOPTS", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(child / "test_child.py"),
         "-q", "-p", "no:cacheprovider", "-p", "tests.conftest"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=300, check=False,
    )


def test_a_test_that_writes_a_watched_store_fails_the_run(tmp_path):
    """END TO END: the fixture fails a test that writes the store it watches.

    This is the control for the whole file — if the guard is removed or
    weakened, the child run below passes and this test fails.
    """
    store = tmp_path / "live" / "projects.db"
    store.parent.mkdir()
    store.touch()

    result = _run_child_pytest(
        tmp_path,
        """
        import os, sqlite3
        from pathlib import Path

        def test_writes_the_live_store():
            store = Path(os.environ["FAKE_STORE"])
            conn = sqlite3.connect(store)
            conn.execute("create table if not exists projects (slug text)")
            conn.execute("insert into projects values ('alpha')")
            conn.commit()
            conn.close()
        """,
        store,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, f"the guard did not fail the writing test:\n{output}"
    assert "LIVE Hermes project store" in output
    assert str(store) in output


def test_a_test_that_leaves_the_watched_store_alone_passes(tmp_path):
    """The other half of the control: the guard is not simply failing every
    child run — an untouched store passes."""
    store = tmp_path / "live" / "projects.db"
    store.parent.mkdir()
    store.touch()

    result = _run_child_pytest(
        tmp_path,
        """
        def test_reads_nothing():
            assert True
        """,
        store,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, f"the guard false-positived on an untouched store:\n{output}"
    assert "LIVE Hermes project store" not in output


# ─────────────── the inherited identity that outlived the sandbox ───────────────


def test_the_inherited_profile_identity_does_not_survive_conftest():
    """``HERMES_PROFILE`` names a profile whose home is resolved from the REAL
    root — the sandbox cannot redirect it, so conftest drops it.

    Fails if the scrub is deleted: the worker-launched run that leaked ``alpha``
    carried ``HERMES_PROFILE=platform-coder`` into every child process.
    """
    assert hasattr(suite_conftest, "INHERITED_PROFILE_ENV"), (
        "tests/conftest.py no longer scrubs the inherited profile identity"
    )
    for name, inherited in suite_conftest.INHERITED_PROFILE_ENV.items():
        assert os.environ.get(name) != inherited, (
            f"{name}={inherited!r} still resolves a real profile home in this session"
        )
