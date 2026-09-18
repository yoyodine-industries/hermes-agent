"""Tests for the capped triage drain: the burst caps and scope-gate routing.

Pure-logic tests only (no live DB, no LLM). The cap decisions are the part that
protects the board from a 13-child fan-out, so they are tested directly with the
aux-LLM call monkeypatched.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from hermes_cli import kanban_triage_drain as drain


def _fake_task() -> SimpleNamespace:
    return SimpleNamespace(id="t_x", title="Title", body="Body", assignee=None)


def _fake_routing() -> SimpleNamespace:
    return SimpleNamespace(
        orchestrator="default",
        default_assignee="default",
        roster=[],
        valid_names=set(),
    )


def _aux_json(n: int) -> str:
    tasks = [
        {"title": f"child {i}", "body": "b", "assignee": None, "parents": []}
        for i in range(n)
    ]
    return json.dumps({"fanout": True, "rationale": "x", "tasks": tasks})


def test_capped_system_prompt_injects_cap():
    out = drain._capped_system_prompt(3)
    assert "at most 3 tasks" in out
    assert "2-6 tasks" not in out


def test_decompose_refuses_over_per_card_cap(monkeypatch):
    monkeypatch.setattr(drain, "_call_aux", lambda *a, **k: (_aux_json(5), ""))
    caps = drain.DrainCaps(threshold=30, per_card_cap=3, per_run_cap=3)
    action = drain._decompose_capped(
        _fake_task(), _fake_routing(), caps, author="t",
        remaining_budget=3, timeout=10, dry_run=False,
    )
    assert action.disposition == "refuse"
    assert "per-card cap" in action.reason


def test_decompose_refuses_over_run_budget(monkeypatch):
    monkeypatch.setattr(drain, "_call_aux", lambda *a, **k: (_aux_json(3), ""))
    caps = drain.DrainCaps(threshold=30, per_card_cap=5, per_run_cap=2)
    action = drain._decompose_capped(
        _fake_task(), _fake_routing(), caps, author="t",
        remaining_budget=2, timeout=10, dry_run=False,
    )
    assert action.disposition == "refuse"
    assert "run budget" in action.reason


def test_decompose_respects_budget_when_under(monkeypatch):
    monkeypatch.setattr(drain, "_call_aux", lambda *a, **k: (_aux_json(2), ""))
    caps = drain.DrainCaps(threshold=30, per_card_cap=3, per_run_cap=3)
    # dry_run avoids the DB write; still exercises the cap math path.
    action = drain._decompose_capped(
        _fake_task(), _fake_routing(), caps, author="t",
        remaining_budget=3, timeout=10, dry_run=True,
    )
    # dry_run + fanout -> decompose path (children_created counts the fan-out).
    assert action.disposition == "decompose"
    assert action.children_created == 2


def test_refuse_comment_names_scope_rule():
    assert "SCOPE line" in drain._refuse_comment_text("no_scope")
    assert "SCOPE line" in drain._refuse_comment_text("unparseable")


def test_already_refused_matches_prior_drain_note(monkeypatch):
    import contextlib
    fake_comments = [
        SimpleNamespace(id=1, task_id="t_x", author="t", body="Triage drain: refused (no SCOPE line). Declare scope...", created_at=1),
    ]
    @contextlib.contextmanager
    def _fake_conn():
        yield object()
    monkeypatch.setattr(drain.kbc, "connect_closing", _fake_conn)
    monkeypatch.setattr(drain.kb, "list_comments", lambda conn, tid: fake_comments)
    assert drain._already_refused("t_x", "t") is True


def test_already_refused_false_when_last_comment_is_not_drain(monkeypatch):
    import contextlib
    fake_comments = [
        SimpleNamespace(id=1, task_id="t_x", author="someone", body="a normal note", created_at=1),
    ]
    @contextlib.contextmanager
    def _fake_conn():
        yield object()
    monkeypatch.setattr(drain.kbc, "connect_closing", _fake_conn)
    monkeypatch.setattr(drain.kb, "list_comments", lambda conn, tid: fake_comments)
    assert drain._already_refused("t_x", "t") is False


def test_already_refused_false_when_no_comments(monkeypatch):
    import contextlib
    @contextlib.contextmanager
    def _fake_conn():
        yield object()
    monkeypatch.setattr(drain.kbc, "connect_closing", _fake_conn)
    monkeypatch.setattr(drain.kb, "list_comments", lambda conn, tid: [])
    assert drain._already_refused("t_x", "t") is False



def test_oversize_single_is_refused(monkeypatch):
    # A declared-oversize card that the decomposer would dispatch whole (fanout
    # false) must NOT slip through as a single task: force_fanout refuses it.
    monkeypatch.setattr(
        drain, "_call_aux",
        lambda *a, **k: (json.dumps({"fanout": False, "rationale": "x"}), ""),
    )
    caps = drain.DrainCaps(threshold=30, per_card_cap=3, per_run_cap=3)
    action = drain._decompose_capped(
        _fake_task(), _fake_routing(), caps, author="t",
        remaining_budget=3, timeout=10, dry_run=False, force_fanout=True,
    )
    assert action.disposition == "refuse"
    assert "must split" in action.reason


def test_drain_orders_accepts_before_decomposes(monkeypatch):
    # A bounded card's accept path runs recompute_ready (inside specify), which
    # would promote a decompose child from an EARLIER card to 'ready'. So the
    # drain must process single-unit accepts BEFORE decomposes, leaving the
    # decompose children 'todo'. The triage list is given oversize-first on
    # purpose to prove the drain reorders, not the source list.
    seen = []

    monkeypatch.setattr(drain, "_profile_author", lambda: "t")
    monkeypatch.setattr(drain, "_load_routing", _fake_routing)
    monkeypatch.setattr(drain.kb, "kanban_db_path", lambda board=None: "/tmp/x.db")
    monkeypatch.setattr(drain, "_list_triage_ids", lambda: ["t_oversize", "t_ok"])

    tasks = {"t_oversize": "body OVER", "t_ok": "body fine"}

    def _load(tid):
        t = _fake_task()
        t.id = tid
        t.body = tasks[tid]
        return t, None

    monkeypatch.setattr(drain, "_load_triage_task", _load)
    monkeypatch.setattr(
        drain, "scope_verdict",
        lambda body, threshold: SimpleNamespace(status=("oversize" if "OVER" in body else "ok")),
    )

    def _decomp(task, routing, caps, author, remaining_budget, timeout,
                dry_run, force_fanout=False):
        seen.append(task.id)
        return drain.Action(task.id, "decompose" if force_fanout else "accept", "x")

    monkeypatch.setattr(drain, "_decompose_capped", _decomp)
    drain.drain_triage(dry_run=True)
    assert seen == ["t_ok", "t_oversize"]
