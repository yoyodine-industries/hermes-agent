"""Board health: a ready card whose workspace can NEVER resolve is counted (card t_15dce81a).

The incident this pins (measured 2026-10-18): a project whose ``primary`` folder was a program's
STATE directory — a plain folder, never a repo — made every card born ``workspace_kind=worktree``
inherit ``<state-dir>/.worktrees/<id>``. The spawn seam refused it, the dispatcher charged the
card twice, and the board sat at READY=43 with READY_LAG>8h while nothing could start: nothing in
the snapshot said "these cards are impossible", only that they were ready and unattended.

The spawn gate (card t_e5ff985b) fixes the birth and the claim; this module pins the BOARD-level
half — a named, counted line in the health snapshot — plus the rule behind it. Everything runs
through the real seams: the real ``resolve_workspace`` predicate, the real diagnostics engine and
the real ``hermes kanban diagnostics`` code path.

What is asserted:

* a ``ready`` worktree card anchored on a plain folder produces a ``workspace_cannot_resolve``
  diagnostic that names the anchor, the marker and the retarget repair;
* a card anchored on a real repo produces none (no false positive on the healthy shape);
* the snapshot prints ``workspace_unresolvable: N ready card(s) whose workspace cannot resolve``
  and names the cards;
* the count is a property of the BOARD, not of the ``--severity`` slice;
* once the spawn gate blocks the card it leaves the count — the card itself now carries the
  refusal and names the repair, so it is not "ready and unattended" any more.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_diagnostics as kd

KIND = "workspace_cannot_resolve"
MARKER = kbw.SPAWN_FAILED_NO_REPO_PREFIX


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture(autouse=True)
def _clear_anchor_cache():
    """The repo probe is memoised per anchor; a test never inherits another's verdict."""
    kd._ANCHOR_HAS_REPO_CACHE.clear()
    yield
    kd._ANCHOR_HAS_REPO_CACHE.clear()


def _make_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(root)],
                   check=True, capture_output=True, text=True)
    (root / "README.md").write_text("repo\n", encoding="utf-8")
    for args in (("add", "-A"), ("commit", "-m", "init")):
        subprocess.run(
            ["git", "-C", str(root), "-c", "user.name=Health Test",
             "-c", "user.email=health@example.com", "-c", "commit.gpgsign=false", *args],
            check=True, capture_output=True, text=True,
        )
    return root


def _make_plain_folder(root: Path) -> Path:
    """The defect's exact shape: a real folder (state/docs) that is not a repo."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "LOOPING.md").write_text("state, not source\n", encoding="utf-8")
    return root


def _dead_card(conn, folder: Path, title: str = "dead anchor") -> str:
    """A ready worktree card in the shape the 2026-10-18 incident produced."""
    return kb.create_task(
        conn, title=title, assignee="a", workspace_kind="worktree",
        workspace_path=str(folder / ".worktrees" / "t_deadbeef"),
    )


def _diagnostics_for(conn, tid: str, cfg: dict | None = None) -> list:
    return kd.compute_task_diagnostics(
        kb.get_task(conn, tid), kb.list_events(conn, tid), kb.list_runs(conn, tid),
        config=cfg or {},
    )


def _snapshot(capsys, severity=None) -> str:
    from hermes_cli import kanban as kanban_cli

    assert kanban_cli._cmd_diagnostics(
        argparse.Namespace(task=None, severity=severity, json=False)
    ) == 0
    return capsys.readouterr().out


# --------------------------------------------------------------------------- the rule


def test_a_ready_card_on_a_plain_folder_is_a_named_critical_diagnostic(kanban_home, tmp_path):
    """The diagnostic must say WHICH anchor and WHICH command fixes it."""
    folder = _make_plain_folder(tmp_path / "platform" / "state-program")
    with kbc.connect() as conn:
        tid = _dead_card(conn, folder)
        diags = [d for d in _diagnostics_for(conn, tid) if d.kind == KIND]

    assert len(diags) == 1, [d.kind for d in _diagnostics_for(conn, tid)]
    diag = diags[0]
    assert diag.severity == "critical"          # it can never run; not a warning
    assert diag.data["anchor"] == str(folder / ".worktrees" / "t_deadbeef")
    assert diag.detail.startswith(f"{MARKER}{folder / '.worktrees' / 't_deadbeef'}")
    assert f"hermes kanban retarget {tid}" in diag.detail
    assert any(a.suggested and "--workspace worktree:" in a.payload.get("command", "")
               for a in diag.actions)


def test_a_card_on_a_real_repo_is_not_flagged(kanban_home, tmp_path):
    """No false positive: <repo>/.worktrees/<id> is the healthy shape (path need not exist)."""
    repo = _make_repo(tmp_path / "repo")
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="healthy", assignee="a", workspace_kind="worktree",
            workspace_path=str(repo / ".worktrees" / "t_healthy"),
        )
        assert [d.kind for d in _diagnostics_for(conn, tid)] == []


def test_a_scratch_card_is_never_flagged(kanban_home, tmp_path):
    """The rule only speaks for the kind that materialises a worktree."""
    folder = _make_plain_folder(tmp_path / "plain")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="scratch", assignee="a",
                             workspace_kind="scratch", workspace_path=str(folder / "ws"))
        assert [d.kind for d in _diagnostics_for(conn, tid)] == []


def test_a_path_less_card_is_judged_by_the_board_default_workdir(kanban_home, tmp_path):
    """A worktree card with no path of its own anchors on the board's default_workdir."""
    folder = _make_plain_folder(tmp_path / "board-default")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="anchored on the board", assignee="a",
                             workspace_kind="worktree")
        assert conn.execute("SELECT workspace_path FROM tasks WHERE id = ?", (tid,)).fetchone()[0] is None
        assert [d.kind for d in _diagnostics_for(conn, tid)] == []   # no default known: silent
        diags = [d for d in _diagnostics_for(conn, tid, {"board_default_workdir": str(folder)})
                 if d.kind == KIND]
    assert len(diags) == 1
    assert diags[0].data["anchor"] == str(folder)


def test_repairing_the_folder_clears_the_report(kanban_home, tmp_path, monkeypatch):
    """Turning the anchor into a repo must stop the report — within the memo window, not forever.

    The dashboard runs this rule inside a LONG-LIVED gateway process, so a verdict cached for the
    process lifetime would keep naming a project the operator already repaired. The probe is
    memoised for ``_ANCHOR_REPO_TTL`` seconds and then re-asked.
    """
    folder = _make_plain_folder(tmp_path / "plain")
    with kbc.connect() as conn:
        tid = _dead_card(conn, folder)
        assert [d.kind for d in _diagnostics_for(conn, tid) if d.kind == KIND]
        subprocess.run(["git", "init", "-q", str(folder)], check=True, capture_output=True, text=True)
        # Inside the window the cached verdict stands (one probe per anchor per run).
        assert [d.kind for d in _diagnostics_for(conn, tid) if d.kind == KIND]
        # Past it, the probe re-runs and the repair is believed.
        monkeypatch.setattr(kd, "_ANCHOR_REPO_TTL", 0.0)
        assert [d.kind for d in _diagnostics_for(conn, tid) if d.kind == KIND] == []


# --------------------------------------------------------------------------- the health line


def test_the_snapshot_counts_the_card_and_names_it(kanban_home, tmp_path, capsys):
    """DoD: 'the health line counts it' — visible without reading a single run."""
    folder = _make_plain_folder(tmp_path / "platform" / "state-program")
    with kbc.connect() as conn:
        tid = _dead_card(conn, folder)

    out = _snapshot(capsys)
    assert ("workspace_unresolvable: 1 ready card(s) whose workspace cannot resolve "
            f"({tid})") in out


def test_a_healthy_board_reports_zero_rather_than_going_quiet(kanban_home, tmp_path, capsys):
    """A snapshot always carries the line: absent must mean 0, never 'unknown'."""
    repo = _make_repo(tmp_path / "repo")
    with kbc.connect() as conn:
        kb.create_task(conn, title="healthy", assignee="a", workspace_kind="worktree",
                       workspace_path=str(repo / ".worktrees" / "t_healthy"))
    out = _snapshot(capsys)
    assert "workspace_unresolvable: 0 ready card(s) whose workspace cannot resolve" in out


def test_the_count_survives_a_narrower_severity_filter(kanban_home, tmp_path, capsys):
    """The count is the BOARD's property, not the slice the operator asked to see."""
    folder = _make_plain_folder(tmp_path / "plain")
    with kbc.connect() as conn:
        tid = _dead_card(conn, folder)
    # The row itself is critical, so a warning-only listing hides it...
    out = _snapshot(capsys, severity="warning")
    # ...but the board-level line still reports it.
    assert "workspace_unresolvable: 1 ready card(s)" in out
    assert f"({tid})" in out


def test_a_blocked_card_leaves_the_count(kanban_home, tmp_path, capsys, all_assignees_spawnable):
    """Once the spawn gate blocks it, the refusal lives ON the card, so it is not 'ready and
    unattended' — the count is for cards the board still believes are waiting their turn."""
    folder = _make_plain_folder(tmp_path / "plain")

    def _must_not_spawn(task, workspace, board=None):  # pragma: no cover - seam fails first
        raise AssertionError("the spawn must never be reached: the anchor is not a repo")

    with kbc.connect() as conn:
        tid = _dead_card(conn, folder)
        kbd.dispatch_once(conn, spawn_fn=_must_not_spawn, failure_limit=5)
        status = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0]

    assert status == "blocked"
    out = _snapshot(capsys)
    assert "workspace_unresolvable: 0 ready card(s)" in out
