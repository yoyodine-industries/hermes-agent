"""Spawn gate: a workspace behind an UNMOUNTED volume is a deferral, not a card failure
(ruling t_f2c8b3fe §4, card t_6582ca32).

Measured incident this pins (2026-10-10): the sandbox volumes are symlinked into ``/opt``
(``/opt/hermes_sandbox -> /Volumes/yoyofs1/hermes_sandbox``). The volume went away for a while, the
link stayed, and every card whose workspace traversed it died twice inside ``resolve_workspace`` and
was auto-blocked -- host state charged to 26 cards as if each one were broken. Worse, the ledger
recorded the cause as "no repo at <path>", because with the volume gone every anchor lookup answers
"not a repo" and that verdict blocks on the FIRST attempt.

What is asserted here, all through the real seams (no re-implemented logic):

* ``container_reachable`` judges only a SYMLINK prefix, by whether its target is a directory -- an
  ordinary nonexistent parent is reachable (it is about to be created);
* nothing is created THROUGH a dangling link: ``resolve_workspace`` (scratch + dir) and the
  worktree materialiser both refuse with ``WorkspaceVolumeUnavailable`` and leave the disk alone;
* the one classifier verdicts it ``workspace_volume_unavailable`` -- infrastructure, and checked
  BEFORE the no-repo verdict that would otherwise block the card;
* the dispatcher records a DEFERRAL: claim released, run closed, retry budget untouched, the card
  still claimable, and a ``workspace_volume_unavailable`` event carrying ``deferral: true``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_workspace as kbw

MARKER = kbw.WORKSPACE_VOLUME_MARKER


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _dead_mount(tmp_path: Path, name: str = "vol") -> tuple[Path, Path]:
    """The defect's exact shape: a symlink whose target volume is NOT mounted.

    Returns ``(link, target)``. ``link`` exists (``is_symlink()`` is True); ``target`` never exists.
    """
    target = tmp_path / f"{name}-volume"
    assert not target.exists()
    link = tmp_path / name
    link.symlink_to(target, target_is_directory=True)
    return link, target


# --------------------------------------------------------------------------- predicate


def test_container_reachable_judges_only_symlink_prefixes(kanban_home, tmp_path):
    """A dangling SYMLINK is unreachable; an ordinary path that does not exist yet is fine."""
    dead, _dead_target = _dead_mount(tmp_path, "vol")
    live_target = tmp_path / "live-volume"
    live_target.mkdir()
    live = tmp_path / "live"
    live.symlink_to(live_target, target_is_directory=True)

    assert kbw.container_reachable(tmp_path / "never-created" / "workspaces" / "t_1") is True
    assert kbw.container_reachable(live / "workspaces" / "t_1") is True
    assert kbw.container_reachable(live_target / "workspaces" / "t_1") is True
    # The dead mount: False for the link itself, for any child, and for a nested path under it.
    assert kbw.container_reachable(dead) is False
    assert kbw.container_reachable(dead / "workspaces" / "t_1") is False


def test_no_workspace_is_created_through_a_dangling_link(kanban_home, tmp_path):
    """DoD 1: the three creators refuse, and NOTHING is created through the link."""
    dead, dead_target = _dead_mount(tmp_path, "vol")
    scratch_path = dead / "workspaces" / "t_vol0001"
    worktree_path = dead / "repo" / ".worktrees" / "t_vol0002"
    dir_path = dead / "shared" / "t_vol0003"

    with kbc.connect() as conn:
        scratch_id = kb.create_task(
            conn, title="scratch behind a dead mount",
            workspace_kind="scratch", workspace_path=str(scratch_path),
        )
        worktree_id = kb.create_task(
            conn, title="worktree behind a dead mount",
            workspace_kind="worktree", workspace_path=str(worktree_path),
        )
        dir_id = kb.create_task(
            conn, title="dir behind a dead mount",
            workspace_kind="dir", workspace_path=str(dir_path),
        )

        for tid, want in (
            (scratch_id, scratch_path), (worktree_id, worktree_path), (dir_id, dir_path),
        ):
            task = kb.get_task(conn, tid)
            with pytest.raises(kbw.WorkspaceVolumeUnavailable) as excinfo:
                kbw.resolve_workspace(task)
            err = excinfo.value
            assert isinstance(err, kbw.WorkspaceVolumeUnavailable)
            assert str(err).startswith(f"{MARKER}{want}")
            assert err.path == str(want)
            assert err.component == str(dead)     # names WHICH mount to remount
            assert err.task_id == tid

    # The link is still a link, and the volume was NOT conjured into existence by a mkdir.
    assert os.path.lexists(dead)
    assert dead.is_symlink()
    assert not dead_target.exists()
    assert not dead_target.exists(), "nothing may be created through a dangling symlink"
    assert not Path(os.path.realpath(dead)).exists()


def test_a_healthy_workspace_still_materialises(kanban_home, tmp_path):
    """Regression: the gate must not refuse a live volume (scratch + dir both still create)."""
    live_target = tmp_path / "live-volume"
    live_target.mkdir()
    live = tmp_path / "live"
    live.symlink_to(live_target, target_is_directory=True)

    with kbc.connect() as conn:
        scratch_id = kb.create_task(
            conn, title="healthy volume", workspace_kind="scratch",
            workspace_path=str(live / "workspaces" / "t_healthy"),
        )
        dir_id = kb.create_task(
            conn, title="healthy dir volume", workspace_kind="dir",
            workspace_path=str(live_target / "shared" / "t_healthy2"),
        )

        scratch = kbw.resolve_workspace(kb.get_task(conn, scratch_id))
        shared = kbw.resolve_workspace(kb.get_task(conn, dir_id))

    assert scratch == live / "workspaces" / "t_healthy"
    assert scratch.is_dir()
    assert shared == live_target / "shared" / "t_healthy2"
    assert shared.is_dir()


# --------------------------------------------------------------------------- classifier


def test_the_one_classifier_verdicts_a_dead_mount_as_infrastructure(kanban_home, tmp_path):
    """Deterministic: the exception class decides, never the message prose."""
    dead, _ = _dead_mount(tmp_path, "vol")
    exc = kbw.WorkspaceVolumeUnavailable(dead / "w", component=dead, task_id="t_x")

    assert kbd.classify_spawn_failure(exc) == kbd.SPAWN_FAILURE_WORKSPACE_VOLUME
    # The neighbouring verdicts are untouched -- a gone volume must NEVER read as "not a repo"
    # (which blocks on the first attempt) and an ordinary runtime error stays the card's.
    assert kbd.classify_spawn_failure(
        kbw.WorktreeAnchorNotRepo(tmp_path / "plain", task_id="t_x")
    ) == kbd.SPAWN_FAILURE_WORKSPACE_NOT_REPO
    assert kbd.classify_spawn_failure(
        RuntimeError(f"{MARKER}/x cannot be used")) == kbd.SPAWN_FAILURE_CARD


# --------------------------------------------------------------------------- dispatcher


def test_a_dead_mount_defers_the_card_without_spending_its_budget(
    kanban_home, all_assignees_spawnable, tmp_path,
):
    """DoD 3: deferral, not failure -- claim released, run closed, budget untouched, card claimable."""
    dead, dead_target = _dead_mount(tmp_path, "vol")

    def _must_not_spawn(task, workspace, board=None):  # pragma: no cover - seam fails first
        raise AssertionError("the spawn must never be reached: the volume is not mounted")

    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="deferred on a dead mount", assignee="a", workspace_kind="dir",
            workspace_path=str(dead / "workspaces" / "t_vol0004"),
        )
        res = kbd.dispatch_once(conn, spawn_fn=_must_not_spawn, failure_limit=5)

        row = conn.execute(
            "SELECT status, consecutive_failures, last_failure_error, claim_lock, worker_pid "
            "FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        assert res.spawned == []
        assert tid not in res.auto_blocked            # never parked on a host condition
        assert row["status"] == "ready"               # still claimable, not blocked
        assert row["consecutive_failures"] == 0       # the card spent NOTHING of its budget
        assert row["claim_lock"] is None              # claim released
        assert row["worker_pid"] is None
        assert row["last_failure_error"].startswith(MARKER)

        run = conn.execute(
            "SELECT status, outcome, error, metadata, ended_at FROM task_runs "
            "WHERE task_id = ? ORDER BY id DESC LIMIT 1", (tid,),
        ).fetchone()
        assert run["status"] == "spawn_failed" and run["outcome"] == "spawn_failed"
        assert run["ended_at"] is not None            # the run is CLOSED
        meta = json.loads(run["metadata"] or "{}")
        assert meta.get("infrastructure") is True
        assert meta.get("workspace_volume_unavailable") is True
        assert meta.get("workspace_volume_component") == str(dead)

        events = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id", (tid,),
        ).fetchall()
        deferrals = [e for e in events if e["kind"] == kbd.SPAWN_FAILURE_WORKSPACE_VOLUME]
        assert deferrals, "the deferral must be readable as itself in the card's events"
        payload = json.loads(deferrals[-1]["payload"] or "{}")
        assert payload.get("deferral") is True
        assert payload.get("component") == str(dead)

    # And the host condition left no artefact behind.
    assert not dead_target.exists()
