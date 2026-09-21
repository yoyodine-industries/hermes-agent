"""Regression tests for the review-lane self-review fix (``kanban.review_profile``).

A review row carries whatever assignee the card had when it entered the review
column; when nobody was reassigned at ``request_review`` that assignee is the
card's own implementer, so the review lane used to spawn the author to review
their own work. With ``kanban.review_profile`` set to an installed profile the
review lane spawns that profile instead — spawn-only, so the board keeps showing
the row's own assignee — and the key is ignored when it names nothing installed.

The key is a convenience, never the safety. The dispatch path is now fail-closed on
its own: when the reviewer it resolved to (from the key, or from the row) is the
card's own author, the spawn is REFUSED — config-free — and the card is marked
with the reason so the routing can be corrected.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def kanban_home(monkeypatch):
    """Fresh HERMES_HOME with a clean kanban DB and no ``config.yaml``."""
    test_home = tempfile.mkdtemp(prefix="kanban_review_profile_test_")
    monkeypatch.setenv("HERMES_HOME", test_home)
    # Force-reimport so the fresh HERMES_HOME (and an empty config cache) is used.
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli") or mod.startswith("hermes_state") or mod == "hermes_constants":
            del sys.modules[mod]
    from hermes_cli import kanban_db

    yield kanban_db, Path(test_home)


def _write_review_profile(home: Path, profile: str) -> None:
    home.joinpath("config.yaml").write_text(
        f'kanban:\n  review_profile: "{profile}"\n', encoding="utf-8",
    )


def _review_card(kb, kbc, *, assignee: str) -> str:
    """A claimed card handed to the review column with no reviewer reassigned,
    so its assignee is still the implementer — the self-review scenario."""
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="impl", assignee=assignee)
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        assert kb.request_review(
            conn, tid, summary="Implementation complete", expected_run_id=run_id,
        ) is True
        row = conn.execute("SELECT status, assignee FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert row["status"] == "review"
        assert row["assignee"] == assignee  # nobody was reassigned
        return tid


def _spawn_recorder(seen: list):
    def _spawn(task, workspace, *args, **kwargs):
        seen.append({"assignee": task.assignee, "skills": list(task.skills or [])})
        return 4242

    return _spawn


def _db_assignee(kbc, task_id: str) -> str:
    with kbc.connect_closing() as conn:
        return conn.execute(
            "SELECT assignee FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()["assignee"]


def test_review_profile_spawns_configured_profile_not_the_row_assignee(kanban_home):
    """Invariant: with a resolvable ``kanban.review_profile`` the review run
    spawns that profile — even when the row's assignee is not an installed
    profile at all — while the task's stored assignee stays untouched."""
    kb, home = kanban_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd

    _write_review_profile(home, "default")  # the default profile always exists
    task_id = _review_card(kb, kbc, assignee="implementer")  # not an installed profile
    seen: list = []

    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_recorder(seen), dry_run=False)

    assert [s["assignee"] for s in seen] == ["default"]
    # The review lane's force-loaded skill survives the override.
    assert "sdlc-review" in seen[0]["skills"]
    assert any(spawned[0] == task_id and spawned[1] == "default" for spawned in res.spawned)
    # Spawn-only: the board keeps showing the row's own assignee.
    assert _db_assignee(kbc, task_id) == "implementer"


@pytest.mark.parametrize("configured", [None, "not-an-installed-profile"])
def test_unset_or_unresolvable_review_profile_is_refused_not_spawned_as_the_author(
    kanban_home, configured
):
    """Fail-closed: unset (or unresolvable) ``kanban.review_profile`` falls back
    to the row's own assignee — and when that assignee IS the card's implementer,
    the spawn is refused instead of self-reviewing. Spawning the row's assignee
    here was the historical back-compat contract, and it is exactly the
    self-review this guard closes, so the contract is now a refusal."""
    kb, home = kanban_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd

    if configured is not None:
        _write_review_profile(home, configured)
    assert kbd.review_profile() == configured

    task_id = _review_card(kb, kbc, assignee="default")
    seen: list = []

    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_recorder(seen), dry_run=False)
        markers = _refusal_markers(conn, task_id)

    assert seen == []  # the author is never spawned as its own reviewer
    assert (task_id, "default") in res.self_review_refused
    assert not [s for s in res.spawned if s[0] == task_id]
    # Routing is left for the operator, with the reason on the card.
    assert _db_assignee(kbc, task_id) == "default"
    assert markers["events"] == [{"reviewer": "default", "author": "default",
                                  "reason": "reviewer_is_author"}]
    assert len(markers["comments"]) == 1
    assert "Refused to start a review run" in markers["comments"][0]["body"]
    assert "is the author of this card's change" in markers["comments"][0]["body"]


def _refusal_markers(conn, task_id: str) -> dict:
    """The refusal as it is visible ON the card: the structured event, plus the
    human-readable comment that carries the reason."""
    events = [
        json.loads(r["payload"] or "{}")
        for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'self_review_refused' "
            "ORDER BY id", (task_id,),
        ).fetchall()
    ]
    comments = [
        {"author": r["author"], "body": r["body"]}
        for r in conn.execute(
            "SELECT author, body FROM task_comments WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
    ]
    return {"events": events, "comments": comments}


def test_review_profile_naming_the_author_is_refused_too(kanban_home):
    """Invariant: setting the key does not buy a self-review. When
    ``kanban.review_profile`` itself resolves to the card's author, the spawn is
    refused exactly as it is for the fallback assignee — the guard judges the
    reviewer that WOULD run, whichever path chose it."""
    kb, home = kanban_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd

    _write_review_profile(home, "default")  # the default profile always exists
    assert kbd.review_profile() == "default"
    task_id = _review_card(kb, kbc, assignee="default")  # ...and it is the author
    seen: list = []

    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_recorder(seen), dry_run=False)
        markers = _refusal_markers(conn, task_id)

    assert seen == []
    assert (task_id, "default") in res.self_review_refused
    assert markers["events"] == [{"reviewer": "default", "author": "default",
                                  "reason": "reviewer_is_author"}]


def test_a_reassigned_reviewer_still_spawns(kanban_home):
    """Control: the guard refuses the author, never the review lane. The same
    handoff naming a different reviewer spawns that reviewer."""
    kb, home = kanban_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd

    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="impl", assignee="implementer")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        assert kb.request_review(
            conn, tid, summary="Implementation complete", reviewer="default",
            expected_run_id=run_id,
        ) is True
        seen: list = []
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_recorder(seen), dry_run=False)

    assert [s["assignee"] for s in seen] == ["default"]
    assert tid in [s[0] for s in res.spawned]
    assert tid not in [t[0] for t in res.self_review_refused]
