"""The landing-verdict door (``hermes_cli/kanban_landing_verdict.py``, card t_765136ce).

What these tests pin, in the order the card states them:

  * the verdict lands where BOTH readers look (run metadata on the card that DECLARES the
    PR, not on the carrier) and the completion clause's own predicate accepts it;
  * validation is the clause's predicate, not a second dialect: bodyless, too short, and
    "not owed" without a referent are refused exactly as a completion refuses them;
  * merge-never-replace: sibling keys on the run survive, ``tasks.result`` is untouched
    unless the caller passes one;
  * a target that does not declare a PR, or is archived, is refused -- a verdict there is a
    silent no-op;
  * the audit is on both ends, and a repeat is idempotent (no second target event);
  * the completion GATE is unchanged: the same declaring card is still refused a close
    before the verdict and admitted after it -- which is the whole point of the door.
"""
from __future__ import annotations

import json

import pytest

PR_URL = "https://github.com/yoyodine-industries/yaan-hermes-config/pull/63"
REASON = "superseded by yoyodine-industries/yaan-hermes-config#64, which lands the same rule for both lanes"


@pytest.fixture
def board(monkeypatch, tmp_path):
    """An isolated board home; no fence marker, so writes are ordinary board writes."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    from pathlib import Path as _Path

    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        yield kb, conn
    finally:
        conn.close()


def _declaring_card(kb, conn, *, pr=PR_URL, extra=None, title="closed: declares the PR"):
    """A closed card whose run declares ``pr`` -- the shape the reconciler reads."""
    tid = kb.create_task(conn, title=title, assignee="platform-coder")
    metadata = {"published_pr": pr, **(extra or {})}
    kb._synthesize_ended_run(
        conn, tid, outcome="completed", summary="done; the PR is the deliverable",
        metadata=metadata,
    )
    return tid


def _open_card(kb, conn, title="a carrier"):
    """A card in play, used as the CALLER's own card (the second audit end)."""
    tid = kb.create_task(conn, title=title, assignee="platform-coder")
    kb.claim_task(conn, tid)
    return tid


def _newest_metadata(kb, conn, tid):
    row = conn.execute(
        "SELECT metadata FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (tid,)
    ).fetchone()
    return json.loads(row["metadata"]) if row and row["metadata"] else {}


def _events(conn, tid, kind=None):
    sql = "SELECT kind, payload FROM task_events WHERE task_id = ?"
    args = [tid]
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    return conn.execute(sql + " ORDER BY id", args).fetchall()


def _record(conn, target, verdict="landing_not_owed", reason=REASON, **kw):
    from hermes_cli.kanban_landing_verdict import record_landing_verdict

    return record_landing_verdict(conn, target, verdict, reason, **kw)


# ---------------------------------------------------------------------------
# The happy path: recorded where both readers look, with the clause's blessing
# ---------------------------------------------------------------------------

def test_records_the_verdict_on_the_declaring_card(board):
    kb, conn = board
    target = _declaring_card(kb, conn)

    receipt = _record(conn, target, caller_task_id=_open_card(kb, conn))

    assert receipt["changed"] is True
    assert receipt["verdict"] == "landing_not_owed"
    assert _newest_metadata(kb, conn, target)["landing_not_owed"] == REASON
    assert [e["kind"] for e in _events(conn, target, "landing_verdict_recorded")] == ["landing_verdict_recorded"]


def test_the_clause_reads_the_recorded_verdict_as_exempt(board):
    """The recorded value is what the CLAMP reads -- same key, same card, same predicate."""
    kb, conn = board
    target = _declaring_card(kb, conn)
    _record(conn, target)

    from hermes_cli import kanban_landing_carrier as clause

    verdict = clause.evaluate(
        conn=conn, task_id=target, forge=lambda _repo, _num: "OPEN",
        candidates=[],
    )
    assert verdict is not None
    assert verdict.refused is False
    assert verdict.facts["prs"][0]["state"] == "landing_not_owed"
    assert verdict.facts["prs"][0]["reason"] == REASON


def test_deferred_is_recorded_and_deferred_is_the_clause_class(board):
    kb, conn = board
    target = _declaring_card(kb, conn)
    deferred = "the config half must ride the next deploy train, tracked on t_12ab34cd"

    _record(conn, target, verdict="landing_deferred", reason=deferred)

    assert _newest_metadata(kb, conn, target)["landing_deferred"] == deferred
    from hermes_cli import kanban_landing_carrier as clause

    verdict = clause.evaluate(
        conn=conn, task_id=target, forge=lambda _repo, _num: "OPEN", candidates=[]
    )
    assert verdict is not None and verdict.refused is False
    assert verdict.facts["prs"][0]["state"] == "deferred"


def test_merge_preserves_every_existing_key_and_leaves_result_alone(board):
    kb, conn = board
    target = _declaring_card(kb, conn, extra={"changed_files": ["config/x.yaml"], "worker_session_id": "s1"})
    conn.execute("UPDATE tasks SET result = 'the handoff line' WHERE id = ?", (target,))
    conn.commit()

    before = _newest_metadata(kb, conn, target)
    _record(conn, target)
    after = _newest_metadata(kb, conn, target)

    assert after["published_pr"] == PR_URL
    assert after["changed_files"] == before["changed_files"]
    assert after["worker_session_id"] == "s1"
    assert after["landing_not_owed"] == REASON
    result = conn.execute("SELECT result FROM tasks WHERE id = ?", (target,)).fetchone()["result"]
    assert result == "the handoff line", "tasks.result must be untouched unless the caller passes one"


def test_result_is_written_only_when_the_caller_passes_one(board):
    kb, conn = board
    target = _declaring_card(kb, conn)

    _record(conn, target, result="closed: landing not owed (superseded)")

    row = conn.execute("SELECT result FROM tasks WHERE id = ?", (target,)).fetchone()
    assert row["result"] == "closed: landing not owed (superseded)"


def test_bare_pr_number_with_a_repo_key_counts_as_a_declaration(board):
    """The reconciler resolves ``pr_number`` + ``repo``; the door must not disagree with it."""
    kb, conn = board
    target = kb.create_task(conn, title="bare number declaration", assignee="platform-coder")
    kb._synthesize_ended_run(
        conn, target, outcome="completed", summary="declares #63",
        metadata={"pr_number": 63, "repo": "yoyodine-industries/yaan-hermes-config"},
    )

    receipt = _record(conn, target)

    assert receipt["changed"] is True
    assert _newest_metadata(kb, conn, target)["landing_not_owed"] == REASON


# ---------------------------------------------------------------------------
# Refusals -- every one of them a silent no-op this door exists to prevent
# ---------------------------------------------------------------------------

def test_bodyless_reason_is_refused(board):
    kb, conn = board
    target = _declaring_card(kb, conn)

    with pytest.raises(ValueError, match="bodyless"):
        _record(conn, target, reason="   ")

    assert "landing_not_owed" not in _newest_metadata(kb, conn, target)
    assert _events(conn, target, "landing_verdict_recorded") == []


def test_short_reason_is_refused_for_deferred(board):
    kb, conn = board
    target = _declaring_card(kb, conn)

    with pytest.raises(ValueError, match=">= 20 characters"):
        _record(conn, target, verdict="landing_deferred", reason="later")

    assert "landing_deferred" not in _newest_metadata(kb, conn, target)


def test_not_owed_reason_without_a_referent_is_refused(board):
    """Substantive prose naming nothing is exactly the silent door: same predicate as a close."""
    kb, conn = board
    target = _declaring_card(kb, conn)
    prose = "this landing is not owed any more because the change was superseded last week"

    with pytest.raises(ValueError, match="naming no referent"):
        _record(conn, target, reason=prose)

    assert _events(conn, target, "landing_verdict_recorded") == []


def test_unknown_verdict_is_refused(board):
    kb, conn = board
    target = _declaring_card(kb, conn)

    with pytest.raises(ValueError, match="verdict must be one of"):
        _record(conn, target, verdict="landing_ok")

    assert _events(conn, target, "landing_verdict_recorded") == []


def test_a_card_that_declares_no_pr_is_refused(board):
    kb, conn = board
    target = kb.create_task(conn, title="local-only work", assignee="platform-coder")
    kb._synthesize_ended_run(conn, target, outcome="completed", summary="no PR here")

    with pytest.raises(ValueError, match="declares no deliverable PR"):
        _record(conn, target)

    assert _events(conn, target, "landing_verdict_recorded") == []


def test_a_bare_number_without_a_repo_key_is_not_a_declaration(board):
    """``pr_number`` alone resolves to nothing: the reconciler cannot read it either."""
    kb, conn = board
    target = kb.create_task(conn, title="number, no repo", assignee="platform-coder")
    kb._synthesize_ended_run(
        conn, target, outcome="completed", summary="63", metadata={"pr_number": 63}
    )

    with pytest.raises(ValueError, match="declares no deliverable PR"):
        _record(conn, target)


def test_an_archived_target_is_refused(board):
    """The reconciler skips archived cards, so a verdict written there is invisible."""
    kb, conn = board
    target = _declaring_card(kb, conn)
    conn.execute("UPDATE tasks SET status = 'archived' WHERE id = ?", (target,))
    conn.commit()

    with pytest.raises(ValueError, match="archived"):
        _record(conn, target)

    assert "landing_not_owed" not in _newest_metadata(kb, conn, target)


def test_a_missing_target_is_refused(board):
    kb, conn = board

    with pytest.raises(ValueError, match="does not exist"):
        _record(conn, "t_deadbeef")


def test_the_child_context_fence_still_refuses_the_write(board, monkeypatch):
    """This door adds no hole to the fence: a delegate_task child is refused, as everywhere."""
    kb, conn = board
    target = _declaring_card(kb, conn)
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(kb.kanban_home()))

    with pytest.raises(PermissionError, match="delegate_task child"):
        _record(conn, target)

    assert "landing_not_owed" not in _newest_metadata(kb, conn, target)


# ---------------------------------------------------------------------------
# Both audit ends, and idempotence
# ---------------------------------------------------------------------------

def test_the_audit_lands_on_both_cards(board):
    kb, conn = board
    target = _declaring_card(kb, conn)
    caller = _open_card(kb, conn)
    caller_run = kb._current_run_id(conn, caller)

    _record(conn, target, caller_task_id=caller, caller_run_id=caller_run, board="scratch")

    target_events = _events(conn, target, "landing_verdict_recorded")
    caller_events = _events(conn, caller, "landing_verdict_recorded")
    assert len(target_events) == 1 and len(caller_events) == 1
    assert json.loads(target_events[0]["payload"])["outcome"] == "recorded"
    caller_payload = json.loads(caller_events[0]["payload"])
    assert caller_payload["target_task_id"] == target
    assert caller_payload["verdict"] == "landing_not_owed"


def test_a_repeat_is_idempotent_and_records_only_a_noop_on_the_caller(board):
    kb, conn = board
    target = _declaring_card(kb, conn)
    caller = _open_card(kb, conn)
    caller_run = kb._current_run_id(conn, caller)

    first = _record(conn, target, caller_task_id=caller, caller_run_id=caller_run)
    second = _record(conn, target, caller_task_id=caller, caller_run_id=caller_run)

    assert first["changed"] is True
    assert second["changed"] is False
    assert len(_events(conn, target, "landing_verdict_recorded")) == 1, "no duplicate target event"
    assert len(_events(conn, caller, "landing_verdict_noop")) == 1
    assert len(_events(conn, caller, "landing_verdict_recorded")) == 1


# ---------------------------------------------------------------------------
# The gate is unchanged: refused before, admitted after -- over the REAL door
# ---------------------------------------------------------------------------

def test_the_completion_gate_is_not_weakened(board, monkeypatch):
    kb, conn = board
    from hermes_cli import kanban_landing_carrier as clause

    monkeypatch.setattr(clause, "_default_forge", lambda _assignee: (lambda _r, _n: "OPEN"))

    target = kb.create_task(conn, title="[land] card under review", assignee="platform-coder")
    kb.claim_task(conn, target)
    kb.request_review(
        conn, target, summary="please review the patch", metadata={"published_pr": PR_URL},
    )

    # 1. A close that declares an unlanded PR is still refused, verdict or no verdict,
    #    and ``force`` still does not open it.
    refused = kb.complete_task(conn, target, result="merged", force=True)
    assert refused is not None and getattr(refused, "refused", True)
    assert getattr(refused, "cause", "") == "landing_carrier_missing"

    # 2. Record the verdict through the door...
    _record(conn, target)

    # 3. ...and the SAME close now goes through, because the clause reads the escape.
    admitted = kb.complete_task(conn, target, result="merged")
    assert admitted is True, f"the close was still refused: {admitted!r}"
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (target,)).fetchone()
    assert row["status"] == "done"
