"""The PROJECT-LINK evidence seam: every link says WHY, and a contradicted inference is dropped.

Card ``t_b407489b`` (parent design verdict ``t_f801b260`` §4): a card's project decides its
worktree and branch, and the ``skills-refactor`` board's default project auto-linked three
``hermes-agent`` cards to ``skills-corpus`` in silence. Two behaviours are pinned here:

* **the source** -- ``project_link_source`` on the ``created`` event says ``explicit`` /
  ``board`` / ``inherited`` / ``nearest``, so a deliberate link stops looking like a guess;
* **the refusal** -- an INFERRED link (``board``/``nearest``) that the card's own title/body
  contradict is not applied at all: no worktree under the wrong repo, and the drop is loud
  (stderr + ``project_link_unresolved`` / ``..._source`` / ``..._contradiction``).

The mismatch test is mechanical (path normalisation + registered-project identity). What it
cannot decide it does not report: a repo no registered project owns is silent here on purpose
(card ``t_000d63fc`` is the registry half of that fix).
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import projects_cmd
from hermes_cli import projects_db as pdb


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An isolated Hermes home: the kanban store AND the per-profile projects registry."""
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(h))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants

        hermes_constants._cached_hermes_root = None  # type: ignore[attr-defined]
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return h


def _project(*argv) -> int:
    root = argparse.ArgumentParser(prog="hermes")
    p = projects_cmd.build_parser(root.add_subparsers(dest="command"))
    p.set_defaults(func=projects_cmd.projects_command)
    return projects_cmd.projects_command(root.parse_args(["project", *argv]))


def _created_payload(task_id: str) -> dict:
    with kbc.connect_closing() as conn:
        for event in kb.list_events(conn, task_id):
            if event.kind == "created":
                return dict(event.payload or {})
    return {}


def _gi_repo(path):
    """A project primary must be a real GIT repo: a worktree card is anchored on one."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True, text=True)
    return path


def _new_project(tmp_path, slug):
    repo = _gi_repo(tmp_path / f"checkout-{slug}")
    assert _project("create", slug.title(), str(repo), "--slug", slug) == 0
    with pdb.connect_closing() as conn:
        return pdb.get_project(conn, slug), repo


@pytest.fixture
def two_projects(home, tmp_path):
    """``corpus`` (the board's default) and ``kernel`` (the repo the cards actually name)."""
    corpus, corpus_repo = _new_project(tmp_path, "corpus")
    kernel, kernel_repo = _new_project(tmp_path, "kernel")
    return corpus, corpus_repo, kernel, kernel_repo


# --- the refusal: an inferred link the card contradicts is dropped -----------------------


def test_a_board_default_link_the_title_contradicts_is_dropped_and_named(
    home, tmp_path, two_projects, capsys,
):
    corpus, _corpus_repo, kernel, kernel_repo = two_projects
    kb.create_board("scoped", name="Scoped", project_id=corpus.slug)
    named = f"{kernel_repo}/scripts/deploy.py"

    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title=f"patch {named}", board="scoped",
                             workspace_kind="worktree")
        task = kb.get_task(conn, tid)

    # DROPPED, not guessed at: no project, so no worktree and no branch under the wrong repo.
    assert task.project_id is None
    assert task.workspace_path is None
    assert task.branch_name is None

    # LOUD: both sides named, the evidence quoted, and the registry pointed at.
    err = capsys.readouterr().err
    assert named in err
    assert kernel.slug in err
    assert corpus.slug in err
    assert str(pdb.projects_db_path()) in err

    payload = _created_payload(tid)
    assert payload["project_link_unresolved"] == corpus.id
    assert payload["project_link_source"] == "board"
    contradiction = payload["project_link_contradiction"]
    assert contradiction["evidence"] == named
    assert contradiction["kind"] == "path"
    assert contradiction["belongs_to"]["slug"] == kernel.slug
    assert contradiction["candidate"]["slug"] == corpus.slug


def test_a_relative_repo_name_that_another_project_owns_is_a_contradiction(
    home, two_projects, capsys,
):
    corpus, _corpus_repo, kernel, _kernel_repo = two_projects
    kb.create_board("scoped", name="Scoped", project_id=corpus.slug)

    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="tidy the workstream docs",
                             body="see kernel/scripts/deploy.py for the shape",
                             board="scoped", workspace_kind="worktree")
        task = kb.get_task(conn, tid)

    assert task.project_id is None
    payload = _created_payload(tid)
    assert payload["project_link_contradiction"]["kind"] == "repo-name"
    assert payload["project_link_contradiction"]["belongs_to"]["slug"] == kernel.slug
    assert "kernel/scripts/deploy.py" in capsys.readouterr().err


def test_a_path_into_the_linked_project_itself_is_consistent(home, two_projects, capsys):
    corpus, corpus_repo, _kernel, _kernel_repo = two_projects
    kb.create_board("scoped", name="Scoped", project_id=corpus.slug)

    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title=f"tidy {corpus_repo}/docs/index.md",
                             board="scoped", workspace_kind="worktree")
        task = kb.get_task(conn, tid)

    assert task.project_id == corpus.id
    assert capsys.readouterr().err == ""
    assert "project_link_contradiction" not in _created_payload(tid)


def test_an_inferred_link_with_no_contradiction_stands_and_records_its_source(
    home, two_projects, capsys,
):
    corpus, _corpus_repo, _kernel, _kernel_repo = two_projects
    kb.create_board("scoped", name="Scoped", project_id=corpus.slug)

    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="tidy the corpus docs",
                             body="the index needs one pass", board="scoped",
                             workspace_kind="worktree")
        task = kb.get_task(conn, tid)

    assert task.project_id == corpus.id
    assert capsys.readouterr().err == ""
    payload = _created_payload(tid)
    assert payload["project_link_source"] == "board"
    assert "project_link_unresolved" not in payload


def test_a_repo_no_registered_project_owns_is_silent_not_a_contradiction(
    home, tmp_path, two_projects, capsys,
):
    """Cannot decide -> say nothing. The registry, not a heuristic, fixes the unowned repo."""
    corpus, _corpus_repo, _kernel, _kernel_repo = two_projects
    kb.create_board("scoped", name="Scoped", project_id=corpus.slug)
    unowned = f"{tmp_path}/elsewhere/hermes-agent/hermes_cli/x.py"

    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title=f"patch {unowned}", board="scoped",
                             workspace_kind="worktree")
        task = kb.get_task(conn, tid)

    assert task.project_id == corpus.id
    assert capsys.readouterr().err == ""
    assert _created_payload(tid)["project_link_source"] == "board"


def test_a_deduped_filing_does_not_warn_about_the_card_it_did_not_create(
    home, two_projects, capsys,
):
    corpus, _corpus_repo, _kernel, kernel_repo = two_projects
    kb.create_board("scoped", name="Scoped", project_id=corpus.slug)
    title = f"patch {kernel_repo}/scripts/deploy.py"

    with kbc.connect_closing() as conn:
        first = kb.create_task(conn, title=title, board="scoped", workspace_kind="worktree",
                               idempotency_key="dedupe-me")
    capsys.readouterr()  # the first filing's warning
    with kbc.connect_closing() as conn:
        again = kb.create_task(conn, title=title, board="scoped", workspace_kind="worktree",
                               idempotency_key="dedupe-me")
    assert again == first
    assert capsys.readouterr().err == ""


# --- the source: every link says how it happened ----------------------------------------


def test_an_explicit_project_is_never_second_guessed(home, two_projects, capsys):
    corpus, _corpus_repo, _kernel, kernel_repo = two_projects

    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title=f"patch {kernel_repo}/scripts/deploy.py",
                             project_id=corpus.slug, workspace_kind="scratch")
        task = kb.get_task(conn, tid)

    assert task.project_id == corpus.id
    assert capsys.readouterr().err == ""
    payload = _created_payload(tid)
    assert payload["project_link_source"] == "explicit"
    assert "project_link_contradiction" not in payload


def test_an_inherited_project_records_the_source_card_and_is_exempt(home, two_projects, capsys):
    corpus, _corpus_repo, _kernel, kernel_repo = two_projects
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", project_id=corpus.slug,
                                workspace_kind="scratch")
        child = kb.create_task(conn, title=f"child: patch {kernel_repo}/scripts/deploy.py",
                               project_id=corpus.id, project_source_task_id=parent,
                               workspace_kind="scratch")
        task = kb.get_task(conn, child)

    # A split card carries the evidence of the card it came from (design ruling t_f801b260 §4):
    # it is a STATED source, so the contradiction rule does not touch it.
    assert task.project_id == corpus.id
    assert capsys.readouterr().err == ""
    assert _created_payload(child)["project_link_source"] == "inherited"


def test_a_card_with_no_project_link_records_no_source(home, two_projects):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="unscoped", workspace_kind="scratch")
    payload = _created_payload(tid)
    assert "project_link_source" not in payload
    assert "project_link_unresolved" not in payload
