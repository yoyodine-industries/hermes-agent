"""The SDLC review-route clause (``hermes_cli/kanban_sdlc_review_route.py``) over the REAL door.

Card ``t_eb5bf72d`` (design authority platform-stl; scope amendment agreed 2026-10-19,
comment on the card). The measured failure this covers:

1. card ``t_4ae7ebdf`` was CLOSED ``done`` by its own author lane with no review run;
2. its SDLC item stood at ``approved`` with reviewers ``[platform-stl]``;
3. ``advance --to accepted`` refused ``self_acceptance`` -- the acceptance verdict is read
   from the item's own card's runs, and the only run there was the author's;
4. ``reopen-review`` refused ("not in review?"), ``request-review`` refused ("not in
   running/ready"): a done card had NO door that puts a reviewer on it;
5. the repair was a duplicate card plus a duplicate submission item for the same PR
   (``t_99ff0b4b``), and the PR could not land until it existed.

So the tests below assert two halves: the completion that CREATES that state is refused
(and its refusal is legible at the tool door the worker actually uses), and a card that is
already terminal can be routed into review with a recorded reason.

Everything runs through the production doors -- ``kanban_db.complete_task`` for the kernel,
``tools.kanban_tools._handle_complete`` for the tool the worker calls, and
``kanban_db.reopen_review_task`` / ``kanban._cmd_reopen_review`` for the repair -- against a
real board store and a real schema. Nothing is stubbed: this clause has no transport seam.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_sdlc_review_route as clause

ITEM = "sdlc-platform-t-4ae7ebdf"
AUTHOR = "platform-coder"
REVIEWER = "platform-stl"


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    """An isolated kanban root with a real (empty) board store."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(var, raising=False)
    import hermes_constants

    hermes_constants._default_hermes_root_memo = None
    kb._INITIALIZED_PATHS.clear()
    kbc.init_db()
    return home


@pytest.fixture
def conn(fresh_home):
    connection = kbc.connect()
    yield connection
    connection.close()


def _task(conn, task_id):
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def _status(conn, task_id) -> str:
    return _task(conn, task_id)["status"]


def _snapshot(conn, task_id):
    return tuple(_task(conn, task_id))


def _event_kinds(conn, task_id) -> list[str]:
    return [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (task_id,))]


def _blocked(conn, task_id) -> list[dict]:
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
        (task_id, clause.EVENT),
    ).fetchall()
    return [json.loads(row["payload"] or "{}") for row in rows]


def _sdlc_card(conn, *, stage="approved", item=ITEM, assignee=AUTHOR, status="running") -> str:
    """A card the SDLC flow has stamped with a contract comment -- the flow's own link."""
    task_id = kb.create_task(conn, title=f"implement {item}", initial_status="running",
                             assignee=assignee)
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
    conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
        (task_id, "sdlc",
         f"SDLC stage={stage} item={item} at=2026-10-19T09:10:50-0400 by=sdlc "
         f"evidence=card:{task_id}", int(time.time())),
    )
    conn.commit()
    return task_id


def _review_run(conn, task_id, profile, outcome="completed") -> None:
    """A finished run by ``profile`` -- what ``sdlc_nodes.reviewers()`` counts."""
    conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome) "
        "VALUES (?, ?, 'done', ?, ?, ?)",
        (task_id, profile, int(time.time()) - 30, int(time.time()) - 20, outcome),
    )
    conn.commit()


def _owning_run(conn, task_id, profile) -> int:
    """The author's LIVE run, and the card owner it denotes (the worker path)."""
    cur = conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, started_at) VALUES (?, ?, 'running', ?)",
        (task_id, profile, int(time.time())),
    )
    run_id = int(cur.lastrowid)
    conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (run_id, task_id))
    conn.commit()
    return run_id


# --------------------------------------------------------------------------- #
# the refusal: the completion that strands an item
# --------------------------------------------------------------------------- #


def test_author_close_of_an_approved_item_is_refused(conn):
    """THE measured shape: item at ``approved``, author closes, no review on the card."""
    task_id = _sdlc_card(conn, stage="approved")
    before = _snapshot(conn, task_id)

    refusal = kb.complete_task(conn, task_id, summary="shipped it", actor=AUTHOR)

    assert isinstance(refusal, kb.CompletionRefusal)
    assert refusal.cause == "sdlc_review_required"
    assert ITEM in refusal.detail
    assert "'approved'" in refusal.detail
    assert f"hermes kanban request-review {task_id}" in refusal.detail
    # Byte-untouched, still the author's card, NO completed event, ONE audit event.
    assert _snapshot(conn, task_id) == before
    assert _status(conn, task_id) == "running"
    assert "completed" not in _event_kinds(conn, task_id)
    events = _blocked(conn, task_id)
    assert len(events) == 1
    assert events[0]["cause"] == "sdlc_review_required"
    assert events[0]["facts"] == {
        "item": ITEM, "stage": "approved", "ladder": "legacy", "author_lane": AUTHOR,
        "closer": AUTHOR, "reviewers": [],
    }


def test_the_worker_path_is_refused_through_the_owning_run(conn):
    """No ``actor`` named: the closer is proved from the run that owns the card."""
    task_id = _sdlc_card(conn)
    run_id = _owning_run(conn, task_id, AUTHOR)

    refusal = kb.complete_task(conn, task_id, summary="shipped it", expected_run_id=run_id)

    assert isinstance(refusal, kb.CompletionRefusal)
    assert refusal.cause == "sdlc_review_required"
    assert _status(conn, task_id) == "running"


def test_a_force_close_is_still_refused(conn):
    """``force`` releases a stale claim; it never declares a review owed nowhere."""
    task_id = _sdlc_card(conn)

    refusal = kb.complete_task(conn, task_id, summary="shipped it", actor=AUTHOR, force=True)

    assert isinstance(refusal, kb.CompletionRefusal)
    assert refusal.cause == "sdlc_review_required"
    assert _status(conn, task_id) == "running"


def test_changes_requested_is_also_guarded(conn):
    """A bounced item waits on the author's fix AND a fresh review -- same strand risk."""
    task_id = _sdlc_card(conn, stage="changes_requested")

    refusal = kb.complete_task(conn, task_id, summary="fixed it", actor=AUTHOR)

    assert isinstance(refusal, kb.CompletionRefusal)
    assert refusal.cause == "sdlc_review_required"


def test_the_tool_door_renders_the_cause_and_the_owed_act(conn, monkeypatch):
    """The WORKER is the door this fence refuses: it must read the cause, not a run-ownership
    sentence (the same defect the landing fence fixed on ``t_88f6cb12``)."""
    from tools import kanban_tools as kt

    task_id = _sdlc_card(conn)
    _owning_run(conn, task_id, AUTHOR)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)

    out = json.loads(kt._handle_complete({"task_id": task_id, "summary": "shipped it"}))

    err = out.get("error") or ""
    assert "sdlc_review_required" in err, err
    assert ITEM in err, err
    assert f"hermes kanban request-review {task_id}" in err, err
    assert "no run owns it" not in err, err


# --------------------------------------------------------------------------- #
# what the clause must NOT touch
# --------------------------------------------------------------------------- #


def test_a_completed_review_run_admits_the_author_close(conn):
    """A review was recorded on THIS card: the acceptance verdict exists."""
    task_id = _sdlc_card(conn)
    _review_run(conn, task_id, REVIEWER)

    assert kb.complete_task(conn, task_id, summary="done", actor=AUTHOR) is True
    assert _status(conn, task_id) == "done"


def test_a_close_out_of_review_is_the_verdict_itself(conn):
    """The reviewer's approval closes FROM review -- never this clause's business."""
    task_id = _sdlc_card(conn)
    assert kb.request_review(conn, task_id, summary="ready for review") is True
    assert _status(conn, task_id) == "review"

    assert kb.complete_task(conn, task_id, summary="approved", actor=REVIEWER) is True
    assert _status(conn, task_id) == "done"


def test_an_item_at_accepted_needs_no_review_door(conn):
    """Past ``accepted`` the verdict has been read; completion is proof/landing work."""
    task_id = _sdlc_card(conn, stage="accepted")

    assert kb.complete_task(conn, task_id, summary="landed", actor=AUTHOR) is True


def test_an_item_below_approved_is_left_alone(conn):
    """``implemented``/``in_review`` is the flow's own implement-then-review handoff."""
    for stage in ("draft", "specified", "implemented", "in_review"):
        task_id = _sdlc_card(conn, stage=stage)
        assert kb.complete_task(conn, task_id, summary="work done", actor=AUTHOR) is True, stage


def test_a_plain_card_is_never_touched(conn):
    """No SDLC contract comment = outside the clause by construction."""
    task_id = kb.create_task(conn, title="ordinary platform card", initial_status="running",
                             assignee=AUTHOR)

    assert kb.complete_task(conn, task_id, summary="done", actor=AUTHOR) is True


def test_an_unknown_stage_vocabulary_is_not_refused(conn):
    """A stage this kernel cannot rank is a guess, and a fence does not guess."""
    task_id = _sdlc_card(conn, stage="quarantined")

    assert kb.complete_task(conn, task_id, summary="done", actor=AUTHOR) is True


def test_the_design_of_record_ladder_is_guarded_at_its_own_review_gate(conn):
    """The yoyoflow machine owes its verdict at ``code-complete`` (the review-gated
    ``code-complete -> approved-pr-submitted`` transition), not at the legacy ``approved``.

    Both vocabularies are LIVE on the boards (measured 2026-10-19: cards stamped
    ``requirements`` and ``retired``), so a fence that ranked only one left a whole
    vocabulary unguarded.
    """
    task_id = _sdlc_card(conn, stage="code-complete")

    refusal = kb.complete_task(conn, task_id, summary="shipped it", actor=AUTHOR)

    assert isinstance(refusal, kb.CompletionRefusal)
    assert refusal.cause == "sdlc_review_required"
    assert "'code-complete'" in refusal.detail and "'pr-accepted'" in refusal.detail
    assert _blocked(conn, task_id)[0]["facts"]["ladder"] == "yoyoflow"


def test_the_design_of_record_ladder_stops_at_its_own_acceptance(conn):
    """``pr-accepted`` is that machine's acceptance: past it no review door is owed.

    ``approved-pr-submitted`` is still INSIDE the band -- the review has happened, but the
    ACCEPTANCE verdict (read off this card's runs) is what ``pr-accepted`` needs, exactly
    as the legacy ladder keeps ``approved`` guarded.
    """
    guarded = _sdlc_card(conn, stage="approved-pr-submitted")
    assert isinstance(
        kb.complete_task(conn, guarded, summary="shipped it", actor=AUTHOR),
        kb.CompletionRefusal,
    )

    for stage in ("pr-accepted", "announced", "retired"):
        task_id = _sdlc_card(conn, stage=stage)
        assert kb.complete_task(conn, task_id, summary="done", actor=AUTHOR) is True, stage


def test_an_unprovable_closer_is_not_refused(conn):
    """No owning run, no actor (a dashboard close): the clause never guesses at authorship."""
    task_id = _sdlc_card(conn)

    assert kb.complete_task(conn, task_id, summary="closed by a human") is True


def test_a_quote_of_the_contract_line_is_not_a_contract(conn):
    """Anchored at the start of the comment: a review note quoting the line is not a stamp."""
    task_id = kb.create_task(conn, title="ordinary card", initial_status="running", assignee=AUTHOR)
    conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
        (task_id, REVIEWER,
         f"quoting the flow: 'SDLC stage=approved item={ITEM}' — see the thread", int(time.time())),
    )
    conn.commit()

    assert kb.complete_task(conn, task_id, summary="done", actor=AUTHOR) is True


# --------------------------------------------------------------------------- #
# the repair half: a DONE card can be routed into review, with a reason
# --------------------------------------------------------------------------- #


def _done_card(conn, *, stage="approved") -> str:
    task_id = _sdlc_card(conn, stage=stage)
    assert kb.complete_task(conn, task_id, summary="closed too early",
                            actor=None) is True  # unprovable closer: the pre-fix shape
    assert _status(conn, task_id) == "done"
    return task_id


def test_a_done_card_reopens_into_review_with_reason_and_reviewer(conn):
    """The door the measured failure did not have: done -> review, reviewer assigned."""
    task_id = _done_card(conn)

    assert kb.reopen_review_task(
        conn, task_id, reason="the acceptance verdict is still owed on this card",
        reviewer=REVIEWER) is True

    row = _task(conn, task_id)
    assert row["status"] == "review"
    assert row["assignee"] == REVIEWER
    assert row["current_run_id"] is None
    assert row["completed_at"] is None

    events = [json.loads(r["payload"]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'review_reopened' "
        "ORDER BY id", (task_id,))]
    assert events == [{
        "status": "review", "from_status": "done",
        "reason": "the acceptance verdict is still owed on this card",
        "reviewer": REVIEWER, "implementer": AUTHOR,
    }]
    # The reopen is RECORDED on the card too, so a reader of the thread sees why.
    bodies = [r["body"] for r in conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id", (task_id,))]
    assert any("REOPENED FROM DONE" in b and "acceptance verdict" in b for b in bodies), bodies


def test_a_done_reopen_without_a_reason_is_refused(conn):
    """A terminal reopen is never silent: no reason, no write."""
    task_id = _done_card(conn)
    before = _snapshot(conn, task_id)

    assert kb.reopen_review_task(conn, task_id, reason="   ", reviewer=REVIEWER) is False
    assert kb.reopen_review_task(conn, task_id, reason="owed", reviewer=None) is False
    assert _snapshot(conn, task_id) == before
    assert "review_reopened" not in _event_kinds(conn, task_id)


def test_a_done_reopen_to_the_author_lane_is_refused(conn):
    """A self-review cannot produce the verdict the card is sent for."""
    task_id = _done_card(conn)

    assert kb.reopen_review_task(conn, task_id, reason="owed", reviewer=AUTHOR) is False
    assert _status(conn, task_id) == "done"


def test_the_review_path_is_unchanged(conn):
    """``review -> ready`` still restores the implementer and needs no reason."""
    task_id = _sdlc_card(conn)
    assert kb.request_review(conn, task_id, summary="ready") is True

    assert kb.reopen_review_task(conn, task_id) is True

    row = _task(conn, task_id)
    assert row["status"] in ("ready", "todo")
    assert row["assignee"] == AUTHOR


def test_the_cli_reopen_door_is_recorded_and_its_refusals_are_legible(conn, capsys):
    """``hermes kanban reopen-review``: the done path needs BOTH flags and says so."""
    from hermes_cli import kanban as kbcli

    task_id = _done_card(conn)

    rc = kbcli._cmd_reopen_review(SimpleNamespace(task_ids=[task_id], reason=None, reviewer=None))
    assert rc == 1
    err = capsys.readouterr().err
    assert "requires a recorded --reason" in err, err
    assert _status(conn, task_id) == "done"

    rc = kbcli._cmd_reopen_review(
        SimpleNamespace(task_ids=[task_id], reason=None, reviewer=REVIEWER))
    assert rc == 1
    assert "requires a recorded --reason" in capsys.readouterr().err

    rc = kbcli._cmd_reopen_review(
        SimpleNamespace(task_ids=[task_id], reason="verdict still owed", reviewer=None))
    assert rc == 1
    assert "requires --reviewer" in capsys.readouterr().err

    rc = kbcli._cmd_reopen_review(SimpleNamespace(
        task_ids=[task_id], reason="verdict still owed", reviewer=REVIEWER))
    assert rc == 0
    assert "Reopened" in capsys.readouterr().out
    assert _status(conn, task_id) == "review"


# --------------------------------------------------------------------------- #
# the clause's own arithmetic (the vocabulary it refuses on)
# --------------------------------------------------------------------------- #


def test_the_guarded_band_is_approved_up_to_accepted():
    floor, ceiling = clause.STAGES.index(clause.GUARD_FROM), clause.STAGES.index(clause.ACCEPTED_AT)
    guarded = clause.STAGES[floor:ceiling]
    assert guarded == ("approved", "changes_requested"), guarded
    assert clause.STAGES.index("implemented") < floor
    assert clause.STAGES.index("accepted") == ceiling


def test_the_good_outcome_vocabulary_matches_the_acceptance_door():
    """This gate and ``sdlc_nodes.reviewers()`` must count the same attempts."""
    from pathlib import Path as _P

    nodes = _P("/opt/hermes_sandbox/platform/repos/yaan-platform/scripts/sdlc_nodes.py")
    if not nodes.exists():
        pytest.skip("yaan-platform checkout absent")
    text = nodes.read_text(encoding="utf-8", errors="replace")
    marker = "GOOD_OUTCOMES"
    assert marker in text
    for outcome in clause.GOOD_OUTCOMES:
        assert f'"{outcome}"' in text, outcome
