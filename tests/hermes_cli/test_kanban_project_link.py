"""Kanban <-> Projects integration: project-linked tasks get a deterministic
worktree path + branch instead of the random ``wt/<task-id>`` fallback."""

from __future__ import annotations

import os
import subprocess

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import projects_db as pdb


def _git_accepts(name: str) -> bool:
    return subprocess.run(
        ["git", "check-ref-format", "--branch", name], capture_output=True
    ).returncode == 0


@pytest.fixture
def kanban_conn(tmp_path):
    c = kbc.connect(db_path=tmp_path / "kanban.db")
    try:
        yield c
    finally:
        c.close()


def _make_project(name="Web App", repo="/tmp/webapp"):
    with pdb.connect_closing() as pc:
        pid = pdb.create_project(pc, name=name, folders=[repo])
        return pdb.get_project(pc, pid)


def test_project_linked_task_gets_deterministic_worktree_and_branch(kanban_conn):
    proj = _make_project()
    tid = kb.create_task(kanban_conn, title="Add login", project_id=proj.slug)
    task = kb.get_task(kanban_conn, tid)

    assert task.project_id == proj.id
    assert task.workspace_kind == "worktree"
    # Worktree dir anchored under the project's primary repo, keyed on task id.
    assert task.workspace_path == os.path.join(proj.primary_path, ".worktrees", tid)
    # Deterministic branch: <slug>/<task-id>-<title-slug>. NOT a random wt/...
    assert task.branch_name == f"{proj.slug}/{tid}-add-login"
    assert not task.branch_name.startswith("wt/")


def test_explicit_branch_overrides_project_default(kanban_conn):
    proj = _make_project()
    tid = kb.create_task(
        kanban_conn,
        title="x",
        project_id=proj.slug,
        workspace_kind="worktree",
        branch_name="feature/custom",
    )
    task = kb.get_task(kanban_conn, tid)
    assert task.branch_name == "feature/custom"


def test_unlinked_task_unchanged(kanban_conn):
    tid = kb.create_task(kanban_conn, title="plain")
    task = kb.get_task(kanban_conn, tid)

    assert task.project_id is None
    assert task.workspace_kind == "scratch"
    # No branch is persisted — the worker still owns the wt/<id> fallback for
    # genuinely ad-hoc worktree tasks, but unlinked scratch tasks have none.
    assert task.branch_name is None


# --- t_2f637b62: the derived branch name must be a ref git ACCEPTS -----------
#
# The 40-char title slug kept whatever the title left behind, so an ordinary
# title produced a ref ``git check-ref-format`` rejects: the trailing ``.`` of a
# sentence, an embedded ``..``, a component ending in ``.lock``, or a cut landing
# right before a ``.``. ``git worktree add`` rejects the whole ref, so dispatch
# died with a ``workspace: ...`` failure before any checkout existed.

_TITLE_CASES = [
    "x.md",  # the shape the card names: a '.md' title
    "ends with dot.",
    "a..b",
    "name.lock",
    "Design amend skills write route package.",  # trailing '.' on a long title
    "Fix a..b range handling",  # embedded '..'
    "Regenerate name.lock checks",  # component ends in '.lock'
    "x" * 39 + ".md",  # 40-char cut lands right before a '.'
    "a" * 300,  # 300-char title: the cut must still land on a valid ref
    "...",  # only punctuation: nothing usable survives the slug
    "Ünïcode títle with émojis 🚀",  # non-ascii title
]


@pytest.mark.parametrize("title", _TITLE_CASES)
def test_derived_branch_name_is_a_ref_git_accepts(kanban_conn, title):
    proj = _make_project()
    tid = kb.create_task(kanban_conn, title=title, project_id=proj.slug)
    task = kb.get_task(kanban_conn, tid)

    assert task.branch_name.startswith(f"{proj.slug}/{tid}")
    assert _git_accepts(task.branch_name), task.branch_name
    # Stable: the sanitizer sees nothing left to rewrite.
    assert pdb.sanitize_branch_name(task.branch_name) == task.branch_name


def test_derivation_without_a_title_is_the_bare_project_branch(kanban_conn):
    proj = _make_project()
    name = pdb.branch_name_for(proj, "t_cccc0001", title="")

    assert name == f"{proj.slug}/t_cccc0001"
    assert _git_accepts(name)


def test_derived_branch_name_keeps_its_human_readable_slug(kanban_conn):
    proj = _make_project()
    tid = kb.create_task(
        kanban_conn, title="Design amend skills write route package.", project_id=proj.slug
    )
    task = kb.get_task(kanban_conn, tid)

    # Sanitized, not discarded: the slug survives, only the invalid '.' goes.
    assert task.branch_name == (
        f"{proj.slug}/{tid}-design-amend-skills-write-route-package"
    )


def test_derived_branch_name_falls_back_when_the_title_yields_nothing(kanban_conn):
    proj = _make_project()
    tid = kb.create_task(kanban_conn, title="...", project_id=proj.slug)
    task = kb.get_task(kanban_conn, tid)

    assert task.branch_name == f"{proj.slug}/{tid}"
    assert _git_accepts(task.branch_name)


# --- t_2500d9fb: the sanitizer's STATED contract, not just its production output
#
# Both production entry points were already safe, but the docstring claimed three
# properties it did not have, and a future direct caller would read the docstring:
#
#   * not idempotent — stripping a leading '-' AFTER the component rules exposed a
#     leading '.' or '/', which the next pass then stripped again;
#   * not "always a ref git accepts" — the exposed leading '.'/'/' is exactly a ref
#     git refuses;
#   * not byte-identical for a valid name — git forbids a dot only at the END of
#     the ref, while the code applied rstrip('.') per component.
#
# These pin the contract itself. Assertions go through git's own
# `check-ref-format`, never a hard-coded string.

_LEADING_DASH_COUNTEREXAMPLES = ["-.@", "-.a", "-/a", "-/.lock", "\x00..a"]


@pytest.mark.parametrize("raw", _LEADING_DASH_COUNTEREXAMPLES)
def test_sanitizer_output_is_a_fixpoint_git_accepts(raw):
    once = pdb.sanitize_branch_name(raw, fallback="wt/t_dddd0001")

    assert _git_accepts(once), once
    assert pdb.sanitize_branch_name(once, fallback="wt/t_dddd0001") == once


@pytest.mark.parametrize("raw", ["9./zb@1.0", "a./b", "0{./}z9"])
def test_sanitizer_leaves_a_git_valid_name_byte_identical(raw):
    # A non-final component ending in '.' is legal: only the ref's END may not be '.'.
    assert _git_accepts(raw), raw
    assert pdb.sanitize_branch_name(raw, fallback="wt/t_dddd0002") == raw


@pytest.mark.parametrize("raw", _LEADING_DASH_COUNTEREXAMPLES)
def test_dispatch_seam_repairs_each_counterexample_to_an_accepted_ref(raw):
    repaired = kbw.safe_branch_name(raw, task_id="t_dddd0003")

    assert _git_accepts(repaired), repaired


def test_dispatch_seam_fails_closed_when_git_cannot_verify_the_name():
    """A NUL byte cannot be passed to execve, so the probe RAISES.

    The old bare ``except`` returned True for that, so the unverified name went
    straight to ``git worktree add`` — the name is now repaired instead.
    """
    nul = "wt/t_dddd0004\x00tail"

    assert kbw._git_ref_accepted(nul) is False
    repaired = kbw.safe_branch_name(nul, task_id="t_dddd0004")
    assert repaired != nul
    assert _git_accepts(repaired), repaired
