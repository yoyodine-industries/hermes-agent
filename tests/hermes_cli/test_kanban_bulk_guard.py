"""The bulk-action gate (card t_bf9605f8) — regression tests.

These are the tests the operator asked for, stated as behaviour:

* an unapproved bulk write is REFUSED, with a named cause and a plain-words remedy;
* the authorized ask profile may not ALSO record an approval (one party cannot be both sides);
* an ask filed by any other profile is refused;
* a write to a LIVE board store from a scratch path stays prohibited outright;
* an approval binds the EXACT action — it cannot be transplanted to another parameter set;
* every verb in the estate's mutating fence that is bulk/destructive is classified here, so a
  future bulk verb cannot ship ungated (the anti-recurrence guardrail).

The ledger is redirected into ``tmp_path`` and the preimage runner is injected, so nothing here
touches a live board store, a live ledger, or the deployed snapshot unit.
"""

from __future__ import annotations

import argparse
import inspect
import json
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_bulk_guard as kbg
from hermes_cli import tree_identity as kbt

GREEN = "green"  # a stand-in approach text that three parties agree on


@pytest.fixture()
def board_home(tmp_path, monkeypatch):
    """A registered ``ops`` board in a throwaway hermes root — never the estate's own store."""
    root = tmp_path / "home"
    board_dir = root / "kanban" / "boards" / "ops"
    board_dir.mkdir(parents=True)
    (board_dir / "kanban.db").write_bytes(b"")
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    return root


@pytest.fixture()
def ledger(tmp_path, monkeypatch, board_home):
    """An isolated ledger directory (never the host's own), with a resolvable target store."""
    root = tmp_path / "bulk_actions"
    monkeypatch.setenv(kbg.LEDGER_DIR_ENV, str(root))
    return root / kbg.LEDGER_FILENAME


@pytest.fixture()
def snapshot():
    """A verified snapshot receipt, so the snapshot clause passes and the others are exercised."""
    return {"path": "/tmp/preimages/x.db", "sha256": "0" * 64, "quick_check": "ok"}


def _gc_args(**over):
    ns = argparse.Namespace(kanban_action="gc", board="ops", approval=None,
                            event_retention_days=30, log_retention_days=30)
    for key, value in over.items():
        setattr(ns, key, value)
    return ns


def _ask(board="ops", verb="gc", scope=None, approach=GREEN, actor=kbg.AUTHORIZED_ASK_PROFILE,
         apr="APR-0001", apr_status="approved"):
    scope = scope or kbg.classify("gc", {"event_retention_days": 30, "log_retention_days": 30})
    return kbg.record_ask(board=board, verb=verb, scope=scope, approach=approach, actor=actor,
                          apr_ref=apr, apr_status=apr_status)


def _approve(digest, actor, approach=GREEN):
    kbg.record_approval(actor=actor, digest=digest, approach=approach, reason="checked")


def _evaluate(**over):
    args = dict(board="ops", verb="gc",
                scope=kbg.classify("gc", {"event_retention_days": 30, "log_retention_days": 30}),
                approval="d", snapshot={"path": "/tmp/x.db", "quick_check": "ok"})
    args.update(over)
    return kbg.evaluate(**args)


# --- 1. unapproved => REFUSED ---------------------------------------------------------------

def test_unapproved_bulk_write_is_refused(ledger, snapshot, monkeypatch):
    monkeypatch.setattr(kbg, "snapshot_precondition", lambda *a, **k: snapshot)
    with pytest.raises(kbg.BulkActionRefused) as exc:
        kbg.gate_cli(_gc_args())
    assert exc.value.cause == "bulk_unapproved"
    assert str(exc.value).startswith(kbg.REFUSAL_PREFIX)
    assert "Required:" in str(exc.value)
    assert "bulk-approvals ask" in str(exc.value)


def test_single_card_write_is_not_gated(monkeypatch):
    """The rule is about BULK: a plain single-card write is untouched."""
    monkeypatch.setattr(kbg, "snapshot_precondition", lambda *a, **k: {"quick_check": "ok"})
    assert kbg.gate_cli(argparse.Namespace(kanban_action="comment", task_id="t_1")) is None
    assert kbg.classify("archive", {"task_ids": ["t_1"]}) is None
    assert kbg.classify("block", {"ids": ["t_1"]}) is None


# --- 2. one party cannot be on both sides ----------------------------------------------------

def test_the_ask_author_may_not_also_approve(ledger, snapshot, monkeypatch):
    """Operator directive 2026-10-03: a single authorized agent authorizes (no quorum), but the
    ask profile is still not a valid approver — one party cannot be both sides of the decision."""
    digest = _ask()  # filed as kbg.AUTHORIZED_ASK_PROFILE
    _approve(digest, kbg.AUTHORIZED_ASK_PROFILE)
    monkeypatch.setattr(kbg, "snapshot_precondition", lambda *a, **k: snapshot)
    with pytest.raises(kbg.BulkActionRefused) as exc:
        kbg.gate_cli(_gc_args(approval=digest))
    assert exc.value.cause == "approval_duplicate_actor"


# --- 3. ask + two distinct approvals => admitted -------------------------------------------

def test_two_bot_approvals_admit_the_action(ledger, snapshot, monkeypatch):
    digest = _ask()
    _approve(digest, "platform-coder")
    _approve(digest, "research-stl")
    monkeypatch.setattr(kbg, "snapshot_precondition", lambda *a, **k: snapshot)
    facts = kbg.gate_cli(_gc_args(approval=digest))
    assert facts["digest"] == digest
    assert facts["approvers"] == ["platform-coder", "research-stl"]


def test_the_record_is_readable_after_admission(ledger, snapshot):
    digest = _ask(apr="APR-0132")
    _approve(digest, "platform-coder")
    _approve(digest, "platform-worker")
    rows = kbg.bundle_for(digest)
    assert [r["role"] for r in rows] == ["ask", "approval", "approval"]
    assert rows[0]["apr_ref"] == "APR-0132"
    assert {r["approach"] for r in rows} == {GREEN}
    assert kbg.read_ledger()[0]["actor"] == kbg.AUTHORIZED_ASK_PROFILE


# --- 4. the approval binds the EXACT action -------------------------------------------------

def test_approval_does_not_transplant_to_another_parameter_set(ledger, snapshot, monkeypatch):
    """An approval for `gc 30/30` must never admit `gc 0/0` (the incident's shape)."""
    digest = _ask()
    _approve(digest, "platform-coder")
    _approve(digest, "research-stl")
    monkeypatch.setattr(kbg, "snapshot_precondition", lambda *a, **k: snapshot)
    with pytest.raises(kbg.BulkActionRefused) as exc:
        kbg.gate_cli(_gc_args(approval=digest, event_retention_days=0, log_retention_days=0))
    assert exc.value.cause == "bulk_unapproved"  # a different digest: no approval names it
    assert kbg.scope_digest("ops", "gc", "a") != kbg.scope_digest("ops", "gc", "b")
    assert kbg.scope_digest("ops", "gc", "a") != kbg.scope_digest("research", "gc", "a")


def test_boards_delete_scope_distinguishes_archive_from_purge():
    archive = kbg.classify_board_action("rm", {"slug": "ops", "delete": False})
    purge = kbg.classify_board_action("rm", {"slug": "ops", "delete": True})
    assert archive != purge


# --- 5. the ask is the AUTHORIZED profile's, and it names a decided APR ----------------------

def test_ask_from_an_unauthorized_profile_is_refused(ledger, snapshot, monkeypatch):
    digest = _ask(actor="platform-coder")  # a real profile, but not the authorized ask profile
    _approve(digest, "research-stl")
    _approve(digest, "platform-worker")
    monkeypatch.setattr(kbg, "snapshot_precondition", lambda *a, **k: snapshot)
    with pytest.raises(kbg.BulkActionRefused) as exc:
        kbg.gate_cli(_gc_args(approval=digest))
    assert exc.value.cause == "approval_not_ops_head"


def test_ask_without_a_decided_apr_is_refused(ledger, snapshot, monkeypatch):
    digest = _ask(apr_status="pending")
    _approve(digest, "platform-coder")
    _approve(digest, "research-stl")
    monkeypatch.setattr(kbg, "snapshot_precondition", lambda *a, **k: snapshot)
    with pytest.raises(kbg.BulkActionRefused) as exc:
        kbg.gate_cli(_gc_args(approval=digest))
    assert exc.value.cause == "approval_apr_unresolved"


def test_approvals_must_name_the_ask_approach(ledger, snapshot, monkeypatch):
    digest = _ask()
    _approve(digest, "platform-coder", approach="a DIFFERENT plan")
    _approve(digest, "research-stl")
    monkeypatch.setattr(kbg, "snapshot_precondition", lambda *a, **k: snapshot)
    with pytest.raises(kbg.BulkActionRefused) as exc:
        kbg.gate_cli(_gc_args(approval=digest))
    assert exc.value.cause == "approval_approach_mismatch"


def test_the_same_profile_cannot_approve_twice(ledger, snapshot, monkeypatch):
    digest = _ask()
    _approve(digest, "platform-coder")
    _approve(digest, "platform-coder")
    monkeypatch.setattr(kbg, "snapshot_precondition", lambda *a, **k: snapshot)
    with pytest.raises(kbg.BulkActionRefused) as exc:
        kbg.gate_cli(_gc_args(approval=digest))
    assert exc.value.cause == "approval_duplicate_actor"


def test_unknown_approver_profile_is_refused(ledger, snapshot):
    digest = _ask()
    _approve(digest, "nobody-at-all")
    _approve(digest, "platform-coder")
    with pytest.raises(kbg.BulkActionRefused) as exc:
        _evaluate(approval=digest, known_profiles=["platform-coder", "platform-stl"],
                  snapshot=snapshot)
    assert exc.value.cause == "approval_unknown_actor"


# --- 6. the verified-snapshot precondition --------------------------------------------------

def test_missing_snapshot_refuses_the_action(ledger, snapshot):
    digest = _ask()
    _approve(digest, "platform-coder")
    _approve(digest, "research-stl")

    def _no_unit(_store):
        raise kbg.BulkActionRefused("snapshot_missing", "no snapshot unit deployed",
                                    remedy="deploy the board-store snapshot unit")

    with pytest.raises(kbg.BulkActionRefused) as exc:
        _evaluate(approval=digest, snapshot=None, snapshot_runner=_no_unit)
    assert exc.value.cause == "snapshot_missing"


def test_admission_records_the_snapshot_path_on_the_audit_row(ledger, snapshot, monkeypatch):
    digest = _ask()
    _approve(digest, "platform-coder")
    _approve(digest, "research-stl")
    monkeypatch.setattr(kbg, "snapshot_precondition", lambda *a, **k: snapshot)
    facts = kbg.gate_cli(_gc_args(approval=digest))
    assert facts["snapshot"]["path"] == snapshot["path"]
    audit = [json.loads(line) for line in kbg.audit_path().read_text().splitlines() if line]
    assert audit[-1]["outcome"] == "admitted"
    assert audit[-1]["snapshot"] == snapshot["path"]
    assert audit[-1]["approvers"] == ["platform-coder", "research-stl"]


def test_refusal_also_leaves_an_audit_row(ledger, snapshot, monkeypatch):
    monkeypatch.setattr(kbg, "snapshot_precondition", lambda *a, **k: snapshot)
    with pytest.raises(kbg.BulkActionRefused):
        kbg.gate_cli(_gc_args())
    audit = [json.loads(line) for line in kbg.audit_path().read_text().splitlines() if line]
    assert audit[-1]["outcome"] == "refused"
    assert audit[-1]["cause"] == "bulk_unapproved"


# --- 7. a scratch path may never write a live board store ----------------------------------

def test_write_to_a_live_board_store_from_a_scratch_path_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv(kbg.LEDGER_DIR_ENV, str(tmp_path / "ledger"))
    live_root = tmp_path / "live"
    (live_root / "kanban" / "boards" / "ops").mkdir(parents=True)
    live_store = live_root / "kanban" / "boards" / "ops" / "kanban.db"
    live_store.write_bytes(b"")
    from hermes_cli import kanban_scratch as kscratch

    monkeypatch.setattr(kscratch, "live_kanban_root", lambda: live_root)
    with pytest.raises(kbg.BulkActionRefused) as exc:
        kbg.assert_write_destination(live_store, board="research", run_root=tmp_path)
    assert exc.value.cause == "destination_live_store"
    assert "LIVE board store" in str(exc.value)


# --- 8. anti-recurrence: the classification covers the estate's mutating fence ---------------

def test_every_gated_verb_is_a_real_mutating_verb():
    for verb in kbg.GATED_VERBS:
        if verb.startswith("boards-"):
            continue
        assert verb in kbt.ESTATE_MUTATING_ACTIONS, f"{verb} is not an estate-mutating verb"


def test_the_destructive_verbs_of_the_fence_are_all_classified():
    """The guardrail: a verb the estate treats as mutating AND destructive cannot be ungated."""
    destructive = {"gc", "repair", "archive", "swarm", "boards-rm", "boards-delete", "boards-import"}
    for verb in destructive:
        assert verb in kbg.GATED_VERBS or f"boards-{verb.split('-')[-1]}" in kbg.GATED_VERBS, verb


def test_bulk_approvals_is_itself_fenced_as_a_mutating_verb():
    assert "bulk-approvals" in kbt.ESTATE_MUTATING_ACTIONS


def test_registry_and_parser_agree():
    """Every classified CLI verb is in the parser tree — a typo cannot silently ungate one."""
    from hermes_cli import kanban_parser

    names = {name for name, _kw, _args, _children in kanban_parser._SPECS}
    for verb in kbg.BULK_SURFACES:
        assert verb in names, f"{verb} is classified but not a CLI verb"
    from hermes_cli.kanban import _HANDLERS

    assert "bulk-approvals" in _HANDLERS
    assert set(kbg.APPROVAL_HANDLERS) == {"ask", "approve", "list", "show"}


def test_no_verb_is_classified_that_the_parser_does_not_have():
    """The guard never invents a surface, and a NON-bulk verb is never gated (no false refusal)."""
    from hermes_cli import kanban_parser

    names = {name for name, _kw, _args, _children in kanban_parser._SPECS}
    declared = set(kbg.BULK_SURFACES)
    assert declared <= names, f"classified but not a verb: {sorted(declared - names)}"
    board_children = {c[0] for name, _kw, _args, children in kanban_parser._SPECS
                      if name == "boards" and children for c in children[1]}
    board_aliases = {a for name, _kw, _args, children in kanban_parser._SPECS
                     if name == "boards" and children
                     for c in children[1] for a in ((c[1] or {}).get("aliases") or [])}
    unknown = set(kbg.BULK_BOARD_ACTIONS) - (board_children | board_aliases)
    assert not unknown, f"board sub-action classified but not in the tree: {sorted(unknown)}"
    for verb in names - set(kbg.BULK_SURFACES):
        assert kbg.classify(verb, {}) is None, f"{verb} must not be gated on empty params"
    for verb, params in (("block", {"ids": []}), ("block", {"ids": ["t_a"]}),
                         ("archive", {"task_ids": ["t_a"]}), ("specify", {}),
                         ("decompose", {}),
                         ("reassign", {"ids": ["t_a"], "reclaim_first": True}),
                         ("reassign", {"ids": ["t_a", "t_b"], "reclaim_first": False})):
        assert kbg.classify(verb, params) is None, f"{verb}{params} is not a bulk action"


# --- 9. wiring: the gate sits on the ONE mutator seam -----------------------------------------

def test_the_mutator_seam_calls_the_gate():
    """The gate is wired into ``kanban._dispatch`` — the same seam the tree-skew fence uses.

    A verb added to ``BULK_SURFACES`` is therefore gated by construction; the seam call is the
    wiring the incident needed, and its absence is what this test catches.
    """
    from hermes_cli import kanban

    source = inspect.getsource(kanban.kanban_command)
    assert "kbg.gate_cli(args)" in source
    assert "kbg.BulkActionRefused" in source


def test_every_gated_verb_accepts_the_approval_flag():
    """Each gated verb's parser really carries ``--approval`` (the operator's handle)."""
    import argparse

    from hermes_cli.kanban_parser import build_parser

    parser = argparse.ArgumentParser(prog="hermes")
    build_parser(parser.add_subparsers(dest="command"))
    for verb in ("block", "schedule", "promote"):
        args = parser.parse_args(["kanban", verb, "t_x", "--approval", "deadbeef"])
        assert args.approval == "deadbeef", f"{verb} accepts --approval"
    for verb in ("gc", "repair", "specify", "decompose"):
        args = parser.parse_args(["kanban", verb, "--approval", "deadbeef"])
        assert args.approval == "deadbeef", f"{verb} accepts --approval"
    swarm = parser.parse_args(["kanban", "swarm", "goal", "--verifier", "v", "--synthesizer", "s",
                               "--approval", "deadbeef"])
    assert swarm.approval == "deadbeef"
    arch = parser.parse_args(["kanban", "archive", "t_x", "--approval", "deadbeef"])
    assert arch.approval == "deadbeef"
    boards = parser.parse_args(["kanban", "boards", "rm", "probe", "--approval", "deadbeef"])
    assert boards.approval == "deadbeef"
    reassign = parser.parse_args(["kanban", "reassign", "t_x", "newprofile", "--reclaim",
                                  "--approval", "deadbeef"])
    assert reassign.approval == "deadbeef", "reassign accepts --approval"


# --- 10. the reclaim-reassign surface (card t_f1f22f8c) ----------------------------------------

def test_reassign_is_bulk_only_with_reclaim_over_more_than_one_id():
    """Both conditions are load-bearing — losing either silently ungates a destructive path."""
    assert kbg.classify("reassign", {"ids": ["t_a", "t_b"], "reclaim_first": True}) is not None
    # A plain multi-id assign terminates nothing: NOT bulk.
    assert kbg.classify("reassign", {"ids": ["t_a", "t_b"], "reclaim_first": False}) is None
    # One id is the ordinary single-card recovery: NOT bulk.
    assert kbg.classify("reassign", {"ids": ["t_a"], "reclaim_first": True}) is None
    assert kbg.classify("reassign", {"ids": [], "reclaim_first": True}) is None
    assert kbg.classify("reassign", {}) is None


def test_reassign_scope_binds_the_exact_id_set():
    """The scope IS the binding: a different id set is a different action, so a bundle cannot transplant."""
    a = kbg.classify("reassign", {"ids": ["t_a", "t_b"], "reclaim_first": True})
    b = kbg.classify("reassign", {"ids": ["t_a", "t_c"], "reclaim_first": True})
    assert a and b and a != b
    assert "reclaim=true" in a


def test_reassign_cli_params_are_single_id_and_never_bulk():
    """The CLI verb names ONE card, so its params are single-id by construction (never bulk)."""
    ns = argparse.Namespace(task_id="t_a", reclaim=True)
    params = kbg.params_from_args("reassign", ns)
    assert params == {"ids": ["t_a"], "reclaim_first": True}
    assert kbg.classify("reassign", params) is None
    # A cardless namespace yields an empty id set, still not bulk.
    empty = kbg.params_from_args("reassign", argparse.Namespace(task_id=None, reclaim=True))
    assert empty == {"ids": [], "reclaim_first": True}
    assert kbg.classify("reassign", empty) is None
