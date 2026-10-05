"""A dispatched kanban worker must run under its OWN run-scoped temp root.

``_default_spawn`` builds the worker env from the dispatcher's ambient environment, so
``TMPDIR``/``TMP``/``TEMP`` crossed straight into the worker and every ``tempfile.mkdtemp()`` /
pytest basetemp it performed landed in the shared account temp root. Measured 2026-10-05: that
root held ~17 GiB and 98% of it was the pytest basetemp store (``pytest-of-hermes_user/``,
~30 generations/hour) — test execution had no run-scoped temp root at all.

The block under test writes ``<HERMES_HOME>/cache/scratch/kanban-run-<task>-<run>`` and points the
three temp vars at it. It is a TOP-LEVEL entry of the profile scratch so the existing 24h-idle
prune reaps it once the run ends. ``apply_scratch_tmp_env`` (the boot helper that re-derives a
Hermes-exported temp var) respects an already-set value, so an explicit run root sticks.

The capture harness mirrors ``test_kanban_worker_terminal_scope.py::_spawn_env_for_profile_b``:
``process_registry.systemd_user_bus_env`` is stubbed to save the env dict and raise, so
``_default_spawn`` builds the whole env and never spawns a worker.
"""
import os

import pytest

from hermes_cli import kanban_db_dispatch
from hermes_constants import apply_scratch_tmp_env


class _StopSpawn(Exception):
    """Abort ``_default_spawn`` after the env is built so no worker process is created."""


@pytest.fixture
def profile_b(tmp_path, monkeypatch):
    """A fake HOME so ``profiles/`` never resolves to the live install (see hermes-agent-dev)."""
    launch = tmp_path / "fakehome" / ".hermes"
    served = launch / "profiles" / "b"
    served.mkdir(parents=True)
    (served / "config.yaml").write_text(
        "terminal:\n  backend: docker\n  docker_image: b-image\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setenv("TERMINAL_ENV", "local")  # ambient launch-profile policy
    monkeypatch.setenv("TERMINAL_DOCKER_IMAGE", "launch-image")
    # An ambient temp root present, exactly as the gateway holds one: the block must REPLACE it
    # for a real run and must leave it alone when there is no run to scope.
    monkeypatch.setenv("TMPDIR", "/ambient/sentinel")
    monkeypatch.setenv("TMP", "/ambient/sentinel")
    monkeypatch.setenv("TEMP", "/ambient/sentinel")
    monkeypatch.delenv("HERMES_SCRATCH_DIR", raising=False)
    return served


def _spawn_env(monkeypatch, tmp_path, *, run_id):
    """Run ``_default_spawn`` far enough to capture the worker env, never spawning anything."""
    from hermes_cli.kanban_db import Task
    from tools import process_registry

    captured: list[dict] = []

    def _capture(env):
        captured.append(dict(env))
        raise _StopSpawn

    monkeypatch.setattr(process_registry, "systemd_user_bus_env", _capture)

    task = Task(
        id="t1", title="t", body=None, assignee="b", status="claimed", priority=0,
        created_by=None, created_at=0, started_at=None, completed_at=None,
        workspace_kind="dir", workspace_path=None, claim_lock=None, claim_expires=None,
        tenant=None, current_run_id=run_id)
    with pytest.raises(_StopSpawn):
        kanban_db_dispatch._default_spawn(task, str(tmp_path / "ws"))
    assert captured, "_default_spawn never built a worker env"
    return captured[0]


def test_worker_temp_root_is_run_scoped(profile_b, tmp_path, monkeypatch):
    """POSITIVE: a worker carrying a run id gets a per-run temp root under its profile scratch."""
    env = _spawn_env(monkeypatch, tmp_path, run_id=4242)
    run_tmp = os.path.join(
        str(profile_b), "cache", "scratch", "kanban-run-t1-4242")
    assert env["TMPDIR"] == env["TMP"] == env["TEMP"] == run_tmp, (
        f"worker temp vars are not the run-scoped root: "
        f"{{'TMPDIR': {env.get('TMPDIR')!r}, 'TMP': {env.get('TMP')!r}, 'TEMP': {env.get('TEMP')!r}}}")
    assert os.path.isdir(run_tmp), "the run-scoped temp root was not created"


def test_worker_without_a_run_is_not_given_a_scoped_temp_root(profile_b, tmp_path, monkeypatch):
    """NEGATIVE CONTROL: with ``current_run_id=None`` the block sets none of TMPDIR/TMP/TEMP.

    The ambient sentinels survive verbatim and no ``kanban-run-`` entry is created, so the
    positive case above is demonstrably the block's doing and not an artifact of env plumbing.
    """
    env = _spawn_env(monkeypatch, tmp_path, run_id=None)
    assert env["TMPDIR"] == env["TMP"] == env["TEMP"] == "/ambient/sentinel", (
        "the run-scoped block fired without a run id")
    scratch = profile_b / "cache" / "scratch"
    stray = [p.name for p in scratch.iterdir() if p.name.startswith("kanban-run-")] \
        if scratch.exists() else []
    assert stray == [], f"a kanban-run- entry was created without a run id: {stray}"


def test_the_boot_helper_does_not_override_the_run_scoped_temp_root(
        profile_b, tmp_path, monkeypatch):
    """STICKS: the value survives ``apply_scratch_tmp_env`` — it is not re-derived on boot."""
    env = _spawn_env(monkeypatch, tmp_path, run_id=4242)
    run_tmp = env["TMPDIR"]
    assert apply_scratch_tmp_env(env) is False, (
        "the boot helper would re-derive a Hermes-exported temp var over the run root")
    assert env["TMPDIR"] == run_tmp
