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


def test_deferred_must_name_a_real_carrier_and_the_clause_reads_it_as_carried(board):
    """Tightened 2026-10-28: a deferral is a POINTER to a carrier card, never a waiver.

    The door still records the declaration; the clause honours it only through the card
    it names, which must exist and name the same PR.
    """
    kb, conn = board
    target = _declaring_card(kb, conn)
    carrier = kb.create_task(conn, title=f"[land] {PR_URL} for {target}", initial_status="running")
    deferred = f"the config half must ride the next deploy train, carried by {carrier}"

    _record(conn, target, verdict="landing_deferred", reason=deferred)

    assert _newest_metadata(kb, conn, target)["landing_deferred"] == deferred
    from hermes_cli import kanban_landing_carrier as clause

    verdict = clause.evaluate(
        conn=conn, task_id=target, forge=lambda _repo, _num: "OPEN", candidates=[]
    )
    assert verdict is not None and verdict.refused is False
    assert verdict.facts["carriers"], "the named card must be the carrier the close rides"
    assert carrier in " ".join(verdict.facts["carriers"].values())


def test_deferred_naming_no_card_is_refused_by_the_door(board):
    """A deferral that names no carrier card is the silent door the tightening closed."""
    from hermes_cli.kanban_landing_verdict import LandingVerdictRefused

    kb, conn = board
    target = _declaring_card(kb, conn)

    with pytest.raises(LandingVerdictRefused):
        _record(conn, target, verdict="landing_deferred",
                reason="the config half must ride the next deploy train, later")


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
    assert receipt["tier"] == 2, "the bare-number shape is the reconciler's, so tier 2"
    assert receipt["keys"] == ["pr_number", "repo"], "the pair is what resolved it"
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
    _kb, conn = board

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

    # R2 (review round 1, card t_f1dc66dc): the ruling promises the audit row records
    # WHICH tier/keys resolved -- that promise must hold on a REPEAT too, not only on the
    # first write, or a verdict's provenance disappears exactly where it is re-exercised.
    noop_payload = json.loads(_events(conn, caller, "landing_verdict_noop")[0]["payload"])
    assert noop_payload["declared_by"] == {"tier": 1, "keys": ["published_pr"]}


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


# ---------------------------------------------------------------------------
# ONE DECLARATION SURFACE (ruling 2026-10-23, card t_f1dc66dc)
#
# The door must accept every declaration a carrier can be FILE from, because the
# reconciler files carriers off the wider key set. Measured on t_45655ee5: carrier
# t_f4349dba was filed from ``metadata.pr`` while this door refused the same card as
# declaring nothing. The fleet's looser keys are tier 2; ``published_pr`` keeps tier 1.
# ---------------------------------------------------------------------------

FLEET_PR = "yoyodine-industries/yaan-platform#1944"


def _declaring_in(kb, conn, metadata, title="declares in a fleet key"):
    """A closed card declaring exactly ``metadata`` on its run -- the reconciler's shape."""
    target = kb.create_task(conn, title=title, assignee="platform-coder")
    kb._synthesize_ended_run(
        conn, target, outcome="completed", summary="closed; this PR is the deliverable",
        metadata=metadata,
    )
    return target


def test_a_fleet_looser_key_declares_at_tier_two_and_the_receipt_says_so(board):
    """AC1: the ONLY declaration is run metadata ``{"pr": ...}`` -- accepted, tier 2 / ``pr``."""
    kb, conn = board
    target = _declaring_in(kb, conn, {"pr": FLEET_PR})

    receipt = _record(conn, target)

    assert receipt["changed"] is True
    assert receipt["tier"] == 2, "the fleet's looser key is a tier-2 declaration"
    assert receipt["keys"] == ["pr"], "the receipt names WHICH key resolved"
    assert receipt["prs"] == [FLEET_PR]
    assert _newest_metadata(kb, conn, target)["landing_not_owed"] == REASON

    # The audit row carries the same fact, so the record is not only in the caller's hand.
    payload = json.loads(_events(conn, target, "landing_verdict_recorded")[0]["payload"])
    assert payload["declared_by"] == {"tier": 2, "keys": ["pr"]}

    # And the clause reads the escape the door wrote: the close is admitted.
    from hermes_cli import kanban_landing_carrier as clause

    verdict = clause.evaluate(
        conn=conn, task_id=target, forge=lambda _repo, _num: "OPEN", candidates=[],
    )
    assert verdict is not None and verdict.refused is False
    assert verdict.facts["declaration_tier"] == 2
    assert verdict.facts["prs"][0]["state"] == "landing_not_owed"


def test_published_pr_still_resolves_at_tier_one_over_a_conflicting_pr(board):
    """AC2: precedence -- the kernel's own publication key wins, and the looser key is not read."""
    kb, conn = board
    target = _declaring_in(
        kb, conn, {"published_pr": PR_URL, "pr": "other-org/other-repo#999"},
        title="declares both keys",
    )

    receipt = _record(conn, target)

    assert receipt["tier"] == 1
    assert receipt["keys"] == ["published_pr"]
    assert receipt["prs"] == ["yoyodine-industries/yaan-hermes-config#63"]
    assert "other-org/other-repo#999" not in receipt["prs"]

    from hermes_cli import kanban_landing_carrier as clause

    verdict = clause.evaluate(
        conn=conn, task_id=target, forge=lambda _repo, _num: "OPEN", candidates=[],
    )
    assert verdict is not None
    assert verdict.facts["declaration_tier"] == 1
    assert [entry["ref"] for entry in verdict.facts["prs"]] == [
        "yoyodine-industries/yaan-hermes-config#63"
    ]


def test_the_refusal_names_the_surface_the_reconciler_reads(board):
    """AC3: a card declaring nothing still refuses -- and the remedy names the ONE surface.

    No stale "``pr`` is not a declaration" wording: anything the reconciler can file a
    carrier from is named here, because anything it can file from is what this door must
    be able to discharge.
    """
    kb, conn = board
    target = _declaring_in(kb, conn, {}, title="local-only work")

    with pytest.raises(ValueError) as refused:
        _record(conn, target)

    message = str(refused.value)
    assert "declares no deliverable PR" in message
    for key in ("published_pr", "pr_url", "pr_number", "merge_pr", "pull_request"):
        assert key in message, f"the refusal must name the reconciler's key {key}: {message}"
    assert "tier 2" in message and "tier 1" in message
    assert "repo/repository/destination_repo/target_repo" in message
    assert "Nothing was written" in message
