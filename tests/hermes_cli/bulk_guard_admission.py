"""Admit a bulk board action the way production does — for tests of gated verbs (card t_bf9605f8).

The operator's 2026-09-27 rule gates every destructive BULK board action behind an authorized ask
(the ``platform-stl`` seat, operator directive 2026-10-03; no separate bot approvals are required)
and a verified snapshot of the target store (see
``hermes_cli/kanban_bulk_guard.py`` and ``yaan-*`` doctrine: DESIGN.md §3–§8). A test that wants to
observe what a gated verb DOES (its JSON shape, its tenant filter, …) must therefore admit the
action the same way the estate does, instead of asserting the pre-rule behaviour.

This helper does exactly that against a throwaway ledger in ``tmp_path``: it never touches the
host's own ledger, and it injects the snapshot clause rather than shelling out to the deployed
snapshot unit. Nothing else is stubbed — the real ask, the real quorum and the real gate run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from hermes_cli import kanban_bulk_guard as kbg

#: The approach text the ask and both approvals agree on, word for word.
APPROACH = "test-runner: this sweep is safe, snapshot taken first"

APPROVERS = ("platform-coder", "research-stl")


def admit(
    monkeypatch,
    tmp_path: Path,
    *,
    board: str,
    verb: str,
    scope: Optional[str],
    approvers: Iterable[str] = APPROVERS,
    apr_ref: str = "APR-TEST",
) -> str:
    """Record an ask + quorum for ONE exact action and return the digest to pass to ``--approval``.

    ``scope`` must be the guard's OWN canonical scope for the action under test — derive it with
    ``kbg.classify(verb, params)`` / ``kbg.classify_board_action(sub_action, params)`` so the digest
    binds the exact action the test will run (an approval never transplants to another parameter set).
    """
    if not scope:
        raise AssertionError(f"`{verb}` is not classified as a bulk action — nothing to admit")
    monkeypatch.setenv(kbg.LEDGER_DIR_ENV, str(tmp_path / "bulk_actions"))
    monkeypatch.setattr(
        kbg, "snapshot_precondition",
        lambda *a, **k: {"path": str(tmp_path / "preimage.db"), "sha256": "0" * 64,
                         "quick_check": "ok"},
    )
    digest = kbg.record_ask(
        board=board, verb=verb, scope=scope, approach=APPROACH,
        actor=kbg.AUTHORIZED_ASK_PROFILE, apr_ref=apr_ref, apr_status="approved",
    )
    for actor in approvers:
        kbg.record_approval(actor=actor, digest=digest, approach=APPROACH, reason="checked")
    return digest
