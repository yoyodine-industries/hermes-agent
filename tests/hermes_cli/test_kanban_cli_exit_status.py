"""Regression coverage for Kanban CLI process exit status propagation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[2]

# A filing must NAME its board (operator mandate, 2026-10-10, card t_b56e48c1):
# `default` is never a filing destination, so every e2e child addresses this
# registered board instead of falling to the ambient default.
BOARD = "exit-status-probe"


def _run_hermes(home: Path, *args: str, marker: bool = False) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["HERMES_KANBAN_HOME"] = str(home)
    for name in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
    ):
        env.pop(name, None)
    env["HERMES_KANBAN_BOARD"] = BOARD
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


def _register_board(home: Path, monkeypatch) -> None:
    """Register BOARD in the temp home; a filing never mints a board."""
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    kb.create_board(BOARD)


def test_delegated_child_kanban_cli_refusal_returns_nonzero_exit_status(tmp_path, monkeypatch):
    """A printed Kanban mutation refusal must not look like CLI success."""
    home = tmp_path / "hermes"
    home.mkdir()
    _register_board(home, monkeypatch)

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
