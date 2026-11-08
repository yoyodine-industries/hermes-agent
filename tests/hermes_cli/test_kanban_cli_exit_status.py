"""Regression coverage for Kanban CLI process exit status propagation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]


def _run_hermes(home: Path, *args: str, marker: bool = False) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["HERMES_KANBAN_HOME"] = str(home)
    for name in (
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
    ):
        env.pop(name, None)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    if marker:
        env["HERMES_DELEGATED_CHILD_CONTEXT"] = "1"
    else:
        env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_delegated_child_kanban_cli_refusal_returns_nonzero_exit_status(tmp_path):
    """A printed Kanban mutation refusal must not look like CLI success."""
    home = tmp_path / "hermes"
    home.mkdir()

    created = _run_hermes(home, "kanban", "create", "exit status probe", "--json")
    assert created.returncode == 0, created.stderr
    task_id = json.loads(created.stdout)["id"]

    refused = _run_hermes(
        home,
        "kanban",
        "comment",
        task_id,
        "must be refused",
        marker=True,
    )

    assert refused.returncode == 1
    assert "delegate_task" in refused.stderr


def _arm_roster(home: Path) -> None:
    """A profile on disk: the roster the assignee invariant (invariant B) judges BY.

    With no profile and no declared pull lane the gate stands down — there is nothing to
    judge a handle against — so a refusal test has to arm it.
    """
    profile = home / "profiles" / "real-lane"
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "config.yaml").write_text("{}\n", encoding="utf-8")


@pytest.mark.parametrize("verb", ["assign", "reassign"])
def test_assign_verbs_refusal_is_a_message_with_rc_2_not_a_traceback(tmp_path, verb):
    """A handle that cannot run must not reach the user as a Python traceback.

    ``kanban_gate_invariants.AssigneeRefused`` is a ``ValueError``, and neither verb mapped
    it: the gate's own refusal text — which names the honest path (declare the lane, or assign
    a profile that exists) — was lost behind an uncaught exception. A refusal a caller cannot
    read is a wall, not a gate.
    """
    home = tmp_path / "hermes"
    home.mkdir()

    created = _run_hermes(home, "kanban", "create", "assign refusal probe", "--json")
    assert created.returncode == 0, created.stderr
    task_id = json.loads(created.stdout)["id"]

    _arm_roster(home)

    refused = _run_hermes(home, "kanban", verb, task_id, "gone-lane")
    assert refused.returncode == 2, (refused.stdout, refused.stderr)
    assert "kanban:" in refused.stderr
    assert "gone-lane" in refused.stderr
    assert "terminal_lanes" in refused.stderr  # the remedy, not just the verdict
    assert "Traceback" not in refused.stderr

    # Two-sided: a handle that CAN run still works at the same verb.
    accepted = _run_hermes(home, "kanban", verb, task_id, "real-lane")
    assert accepted.returncode == 0, (accepted.stdout, accepted.stderr)
