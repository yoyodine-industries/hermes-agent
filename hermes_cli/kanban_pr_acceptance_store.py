"""Persist acceptance with the same ownership snapshot as the terminal write."""
from __future__ import annotations

from hermes_cli.kanban_db_connect import write_txn
from hermes_cli.kanban_pr_acceptance import (
    collect_acceptance,
    contract_is_readable,
    pr_url_contract,
)

# The statuses a close can be attempted from; outside them ``prepare_acceptance`` answers
# ``False`` ("left a completable status") and nothing at all is written.
_COMPLETABLE = frozenset({"running", "ready", "blocked", "review"})


def _snapshot(conn, task_id):
    row = conn.execute("SELECT current_run_id, status, completion_contract FROM tasks WHERE id=?", (task_id,)).fetchone()
    return tuple(row) if row else None


def repair_unreadable_contract(conn, task_id, contract, run_id=None, published_pr=None) -> str:
    """R4 (card ``t_c4a9d940``): repair a stored contract no retry can satisfy, and say so.

    ``completion_contract`` is only ever validated on the way IN (``create_task``), so a board
    can hold a value this gate cannot read -- an EMPTY column, a bare space, a hand edit, a
    truncated write. The card that filed this ruling was measured with a column that read
    blank and refused every close for it, with a detail ("supply metadata.published_pr matching
    the persisted completion contract") that no metadata could ever satisfy, and with no verb
    that repairs it: ``kanban set-contract`` is an owner act, while the worker that hits the
    refusal is precisely the one that cannot use it.

    The repair target is the only declaration the CLOSE itself names:

    * ``local-only`` when the close declares no publication -- R4's letter: the value that
      claims nothing, which is all an unreadable contract says;
    * the exact PR URL when the close declares ``metadata.published_pr``. Repairing that to
      ``local-only`` would be a contradiction the landing carrier refuses outright ("do not
      re-declare it 'local-only': that is the claim its own record already refutes"), i.e. the
      same deadlock one fence later. The declared PR is truthful and satisfiable, and is the
      re-scope that clause's remedy tells an operator to make by hand.

    Returns the repaired value. The previous value goes on ``completion_contract_repaired``
    verbatim, with the ``warn`` a reader (or an audit) treats as the card's WARN row. This
    opens no door around the evidence fences: a repaired PR contract is then VERIFIED like any
    declared one, and the landing leg is still owed.
    """
    from hermes_cli.kanban_db import _append_event

    target = pr_url_contract(published_pr) or "local-only"
    empty = not contract
    reason = (
        "the stored completion_contract is EMPTY while a close is being attempted: nothing "
        "names a declaration, so no acceptance or proof contract is enforced and no metadata "
        "can satisfy the one the refusal claims"
        if empty else
        "the stored completion_contract is not a readable declaration (local-only, landed, "
        "OWNER/REPO or an exact PR URL); left in place it refuses every close with no verb "
        "that can repair it"
    )
    with write_txn(conn):
        conn.execute(
            "UPDATE tasks SET completion_contract = ? WHERE id = ?", (target, task_id))
        _append_event(conn, task_id, "completion_contract_repaired", {
            "previous": contract,
            "previous_repr": repr(contract),
            "repaired_to": target,
            "reason": reason,
            "warn": (
                f"The stored completion contract was {contract!r} -- not a declaration this "
                f"gate can enforce -- so it was repaired to {target!r}. "
                + ("This close carries no acceptance evidence: treat it as UNVERIFIED, not "
                   "green." if target == "local-only" else
                   "That PR is then verified like any declared contract; nothing is reported "
                   "green that the forge did not confirm.")
            ),
        }, run_id=run_id)
    return target


def prepare_acceptance(conn, task_id, expected_run_id, metadata):
    snapshot = _snapshot(conn, task_id)
    if snapshot is None:
        return False
    run_id, status, contract = snapshot
    completable = status in _COMPLETABLE and (
        expected_run_id is None or run_id == expected_run_id)
    unreadable = bool(contract) and not contract_is_readable(contract)
    if not contract or unreadable:
        # R4: repair at load, and only where a close is actually being attempted -- a terminal
        # card keeps its rows byte-for-byte. An EMPTY column is repaired too: nothing enforces
        # it today, but "empty while acceptance is being enforced" is the corrupt state the
        # ruling names, and an empty column cannot be told apart from a truncated one.
        if not completable:
            return False if unreadable else None
        published_pr = metadata.get("published_pr") if isinstance(metadata, dict) else None
        repaired = repair_unreadable_contract(
            conn, task_id, contract, run_id=run_id, published_pr=published_pr)
        if repaired == "local-only":
            return None
        # Repaired to the PR this close declares: verify it like any declared contract, off a
        # snapshot taken AFTER the repair (record_acceptance re-checks it under the write txn).
        snapshot = _snapshot(conn, task_id)
        contract = snapshot[2]
    # ``landed`` is the deploy-proof contract: its fence is ``_gate_deploy_proof`` in
    # complete_task, NOT repository-required CI, so it must not enter the GitHub path.
    # The PR clause could never pass on it (16 receipts, every one classification
    # ``missing``) -- see ruling t_90767078, D1/D2.
    if not contract or contract in ("local-only", "landed"):
        return None
    if not completable:
        return False
    published_pr = metadata.get("published_pr") if isinstance(metadata, dict) else None
    # The declaration is IMMUTABLE: the published PR is recorded on the receipt and the audit
    # event, never written back to the card. Rebinding froze a card to the first PR it ever
    # published, so a superseded PR left it unsatisfiable forever with no verb to release it.
    # A contract change is an OWNER act (``hermes kanban set-contract``), never a side effect
    # of a completion -- ruling t_90767078, D2/D4.
    # The assignee profile's gh login owns the repo: acceptance must not run as
    # the ambient login of whichever process completes the card (#122689).
    assignee = conn.execute("SELECT assignee FROM tasks WHERE id=?", (task_id,)).fetchone()["assignee"]
    return snapshot, collect_acceptance(contract, published_pr, assignee=assignee)


def record_acceptance(conn, task_id, acceptance):
    """Called under complete_task's write_txn, before its terminal UPDATE."""
    from hermes_cli.kanban_db import _append_event
    snapshot, receipt = acceptance
    if _snapshot(conn, task_id) != snapshot:
        return False
    _append_event(conn, task_id, "pr_acceptance", receipt, run_id=snapshot[0])
    if receipt.get("degraded"):
        # R1 (card t_c4a9d940): the forge could not be READ, so this close is recorded as
        # UNVERIFIED. The receipt above keeps ``ok: False`` -- nothing is ever reported green --
        # and this WARN row is what an audit scans for. No ``last_failure_error``: the card is
        # closing, and a failure line on a close is noise the next worker would act on.
        _append_event(conn, task_id, "pr_acceptance_unverified", {
            "classification": receipt.get("classification"),
            "contract": receipt.get("contract"),
            "pr_url": receipt.get("pr_url"),
            "published_pr": receipt.get("published_pr"),
            "warn": receipt.get("warn"),
            "detail": receipt.get("detail"),
        }, run_id=snapshot[0])
        return True
    if not receipt["ok"]:
        detail = f"PR acceptance {receipt['classification']}: {receipt.get('detail', '')} {receipt['recovery']}"
        conn.execute("UPDATE tasks SET last_failure_error=? WHERE id=?", (detail, task_id))
    return receipt["ok"]
