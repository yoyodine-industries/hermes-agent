"""Invariant: ``done`` cannot close over bytes that exist only in a working tree.

Card t_b151a247. A worker used to be able to work inside the canonical unit checkout
(``/opt/hermes_sandbox/<repo>``), reach ``done``, and leave that tree dirty. ``gate.py``
check 1 then refuses EVERY deploy sourced from the checkout, with no waiver, and the
stall is silent: the card reads ``done`` while prod stops receiving.

So the completion boundary asks one question — is the tree this card worked in a
protected DEPLOYMENT SOURCE, and is it dirty? — where "dirty" is the deploy gate's own
command (``git status --porcelain --untracked-files=normal``), so the two boundaries can
never disagree about the word. The protected set comes from the shared-tree guard's own
declaration (``hermes_cli/kanban_protected_trees.py``), never a copy.

These tests pin both halves of that sentence: the refusal, and the hard limit on its
blast radius — a gate that refuses cards for someone else's dirty tree would be a worse
defect than the one it fixes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_protected_trees as protected


@pytest.fixture(autouse=True)
def _fresh_roots(monkeypatch, tmp_path):
    """No test may read the live host's guard declaration or a cached one."""
    protected.reset_cache()
    yield
    protected.reset_cache()


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Point the predicate at nothing by default: a test that wants protected roots
    # declares them explicitly (a fake guard, or the guard's own override).
    monkeypatch.setenv(protected.ENV_GUARD, str(tmp_path / "no-such-guard.py"))
    monkeypatch.delenv("GUARD_SHARED_TREES", raising=False)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kbc.connect() as c:
        yield c


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL,
    ).stdout


def _clean_repo(path: Path) -> Path:
    """A real git checkout with one commit and a clean tree."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    (path / "a.txt").write_text("a\n", encoding="utf-8")
    _git(path, "add", "a.txt")
    _git(
        path, "-c", "user.email=test@example.invalid", "-c", "user.name=Test",
        "commit", "-q", "-m", "init",
    )
    return path


def _declare_roots(monkeypatch, tmp_path: Path, roots: list[str]) -> None:
    """A guard file that declares ``roots`` — the real read path, without the estate."""
    guard = tmp_path / "guard-shared-trees.py"
    guard.write_text(
        "#!/usr/bin/env python3\n"
        f"_SHARED_SOURCE_ROOTS = {roots!r}\n"
        "_LIVE_ROOTS = []\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(0)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(protected.ENV_GUARD, str(guard))
    protected.reset_cache()


def _card(conn, workspace: Path | None, *, status: str = "running") -> str:
    tid = kb.create_task(
        conn, title="did work", assignee="coder",
        workspace_kind="dir" if workspace else None,
        workspace_path=str(workspace) if workspace else None,
        initial_status=status,
    )
    return tid


def _status(conn, tid: str) -> str:
    return conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0]


def test_untracked_file_in_unit_checkout_refuses_and_names_the_path(conn, tmp_path, monkeypatch):
    """The exact incident shape: a stray script written into the canonical checkout.

    ``script.py`` was never committed — the tree's HEAD is clean-looking while the bytes
    that gate.py refuses on are untracked, which is why the dirty test must be
    ``--untracked-files=normal`` and not a diff against HEAD.
    """
    repo = _clean_repo(tmp_path / "unit")
    _declare_roots(monkeypatch, tmp_path, [str(repo)])
    (repo / "script.py").write_text("print('stray')\n", encoding="utf-8")
    tid = _card(conn, repo)

    refusal = kb.complete_task(conn, tid, summary="wrote the fix")

    assert isinstance(refusal, kb.CompletionRefusal)
    assert refusal.cause == "dirty_unit_checkout"
    assert "script.py" in refusal.detail          # the path, so the card can act
    assert str(repo) in refusal.detail            # the checkout, so the lane knows where
    assert "gate.py" not in refusal.detail
    assert _status(conn, tid) != "done"           # nothing changed


def test_refusal_is_recorded_as_an_event(conn, tmp_path, monkeypatch):
    repo = _clean_repo(tmp_path / "unit")
    _declare_roots(monkeypatch, tmp_path, [str(repo)])
    (repo / "stray.txt").write_text("x\n", encoding="utf-8")
    tid = _card(conn, repo)

    kb.complete_task(conn, tid, summary="done")

    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
        (tid, "completion_blocked_dirty_unit_checkout"),
    ).fetchone()
    assert row is not None, "a refusal must leave an auditable trace"
    assert "stray.txt" in row[0]


def test_tracked_modification_also_refuses(conn, tmp_path, monkeypatch):
    repo = _clean_repo(tmp_path / "unit")
    _declare_roots(monkeypatch, tmp_path, [str(repo)])
    (repo / "a.txt").write_text("a\nb\n", encoding="utf-8")
    tid = _card(conn, repo)

    refusal = kb.complete_task(conn, tid, summary="done")

    assert refusal.cause == "dirty_unit_checkout"
    assert "a.txt" in refusal.detail


def test_clean_unit_checkout_completes(conn, tmp_path, monkeypatch):
    """The control for the incident: the same card against a clean checkout closes."""
    repo = _clean_repo(tmp_path / "unit")
    _declare_roots(monkeypatch, tmp_path, [str(repo)])
    tid = _card(conn, repo)

    assert kb.complete_task(conn, tid, summary="wrote the fix") is True
    assert _status(conn, tid) == "done"


def test_a_dirty_tree_outside_the_protected_set_is_untouched(conn, tmp_path, monkeypatch):
    """Blast radius: a card's own dirty scratch repo is its business, not the kernel's.

    ``done`` for a local-only card means the work exists in that tree; refusing it would
    re-create the incident's harm (silent stall) for every lane at once.
    """
    protected_repo = _clean_repo(tmp_path / "unit")
    _declare_roots(monkeypatch, tmp_path, [str(protected_repo)])
    own = _clean_repo(tmp_path / "scratch")
    (own / "wip.txt").write_text("still working\n", encoding="utf-8")
    tid = _card(conn, own)

    assert kb.complete_task(conn, tid, summary="local-only deliverable") is True


def test_an_unrelated_dirty_checkout_never_refuses_another_card(conn, tmp_path, monkeypatch):
    """No fleet-wide deadlock: the sweep is per-checkout, not a global sweep."""
    repo = _clean_repo(tmp_path / "unit")
    _declare_roots(monkeypatch, tmp_path, [str(repo)])
    (repo / "someone_elses_dirt.py").write_text("x\n", encoding="utf-8")
    tid = _card(conn, _clean_repo(tmp_path / "scratch"))

    assert kb.complete_task(conn, tid, summary="unrelated work") is True


def test_no_workspace_path_is_never_examined(conn, tmp_path, monkeypatch):
    repo = _clean_repo(tmp_path / "unit")
    _declare_roots(monkeypatch, tmp_path, [str(repo)])
    (repo / "dirt.py").write_text("x\n", encoding="utf-8")
    tid = _card(conn, None)

    assert kb.complete_task(conn, tid, summary="no workspace") is True


def test_subdirectory_workspace_names_the_dirt_the_deploy_gate_sees(conn, tmp_path, monkeypatch):
    """A card working in a SUBDIR of the checkout is still judged on the checkout.

    Porcelain paths are repo-root-relative and ``--untracked-files=normal`` collapses a
    wholly-new directory to that directory (``?? work/``) — the SAME strings ``gate.py``
    check 1 sees, which is the point: the refusal must not describe the dirt differently
    from the gate that will refuse the deploy because of it.
    """
    repo = _clean_repo(tmp_path / "unit")
    _declare_roots(monkeypatch, tmp_path, [str(repo)])
    lane = repo / "work" / "coder"
    lane.mkdir(parents=True)
    (lane / "draft.md").write_text("x\n", encoding="utf-8")
    (repo / "a.txt").write_text("a\nb\n", encoding="utf-8")
    tid = _card(conn, lane)

    refusal = kb.complete_task(conn, tid, summary="done")

    assert refusal.cause == "dirty_unit_checkout"
    assert "a.txt" in refusal.detail     # tracked edit, named as gate.py names it
    assert "work/" in refusal.detail     # new directory, collapsed as the gate sees it


def test_force_is_the_operator_door(conn, tmp_path, monkeypatch):
    repo = _clean_repo(tmp_path / "unit")
    _declare_roots(monkeypatch, tmp_path, [str(repo)])
    (repo / "stray.py").write_text("x\n", encoding="utf-8")
    tid = _card(conn, repo)

    assert kb.complete_task(conn, tid, summary="forced", force=True) is True


def test_the_guard_override_declares_the_roots(conn, tmp_path, monkeypatch):
    """The guard's own ``GUARD_SHARED_TREES`` override is honoured, so a fixture that
    re-points the guard re-points this predicate with it (one rule, one switch)."""
    repo = _clean_repo(tmp_path / "unit")
    monkeypatch.setenv("GUARD_SHARED_TREES", f'["{repo}"]')
    (repo / "stray.py").write_text("x\n", encoding="utf-8")
    tid = _card(conn, repo)

    assert kb.complete_task(conn, tid, summary="done").cause == "dirty_unit_checkout"


def test_an_unreadable_guard_fails_open(conn, tmp_path, monkeypatch):
    """A broken perimeter must never brick completions (the guard is fail-open too)."""
    repo = _clean_repo(tmp_path / "unit")
    monkeypatch.setenv(protected.ENV_GUARD, str(tmp_path / "gone.py"))
    (repo / "stray.py").write_text("x\n", encoding="utf-8")
    tid = _card(conn, repo)

    assert kb.complete_task(conn, tid, summary="done") is True


def test_a_workspace_git_cannot_read_fails_open(conn, tmp_path, monkeypatch):
    """A declared root that is not a git checkout tells this gate nothing — so it refuses
    nothing. Fail-open, or a mis-declared root would stall that lane's completions."""
    plain = tmp_path / "plain-root"
    plain.mkdir()
    _declare_roots(monkeypatch, tmp_path, [str(plain)])
    workspace = plain / "ws"
    workspace.mkdir()
    (workspace / "x.txt").write_text("x\n", encoding="utf-8")
    tid = _card(conn, workspace)

    assert kb.complete_task(conn, tid, summary="done") is True


def test_symlinked_workspace_is_judged_on_where_it_really_points(conn, tmp_path, monkeypatch):
    """Containment is resolved: a symlink into the checkout is still the checkout."""
    repo = _clean_repo(tmp_path / "unit")
    _declare_roots(monkeypatch, tmp_path, [str(repo)])
    (repo / "stray.py").write_text("x\n", encoding="utf-8")
    link = tmp_path / "link-to-unit"
    link.symlink_to(repo, target_is_directory=True)
    tid = _card(conn, link)

    assert kb.complete_task(conn, tid, summary="done").cause == "dirty_unit_checkout"
