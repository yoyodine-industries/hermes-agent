"""Deterministic hard failure for an update autostash whose restore did NOT happen.

``hermes update`` stashes the local modifications before it moves the checkout and restores
them afterwards. When the restore conflicts it parks them instead (``update_cmd_stash._apply_stash``)
and the tree the fleet would restart onto is missing the local override set. Restarting onto that
tree is the event that turns a parked file into a live outage, so the run is a HARD FAILURE
(standing No-silent-failures rule):

* the fleet keeps serving what it has -- the ``restart`` stage is skipped, never run;
* a deterministic kanban card names the stash ref, the stashed-path count, the conflicted files
  and the exact re-apply command, on the board the platform lane reads, instead of relying on
  somebody re-reading the run's receipt later;
* the run's own verdict stays non-green: ``local_overrides_unrestored`` lands on the receipt and
  the outcome can never be ``success``, so a downstream verifier cannot paint the run green.

The verdict is DETERMINISTIC: it keys on the run's own ``local_changes_stash`` step result plus
the conflicted-file list recorded beside it -- never on inference, never on prose.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

_log = logging.getLogger(__name__)

#: Top-level receipt field carrying the hard failure. Written by
#: :func:`record_unrestored_local_changes`; read by :func:`unrestored_local_changes`,
#: ``update_receipt.UpdateReceipt.finalize`` and ``update_completion._complete_selected``.
FACT = "local_overrides_unrestored"

#: The step ``update_cmd_stash._record_stash_disposition`` records for every stash disposition.
STASH_STEP = "local_changes_stash"

#: Lane seat the remediation card is routed to: re-applying the overrides and restarting the
#: fleet is an operator-authorised ops action (the platform worker's seat), not a code change.
CARD_ASSIGNEE = "default"
CARD_CREATED_BY = "hermes-update"
#: Hard failure: sits above the lane's routine work.
CARD_PRIORITY = 900000


def _steps(receipt: Any) -> list[dict]:
    raw = receipt.get("steps") if isinstance(receipt, dict) else None
    return [row for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []


def last_stash_step(receipt: Any) -> Optional[dict]:
    """The LAST ``local_changes_stash`` step of the run, or ``None`` when the run has none."""
    rows = [row for row in _steps(receipt) if row.get("name") == STASH_STEP]
    return rows[-1] if rows else None


def unrestored_local_changes(receipt: Any) -> Optional[dict]:
    """The hard-failure block this run earned, or ``None``.

    Two deterministic facts, nothing inferred:

    * ``local_changes_stash`` is the run's own step result, and the LAST one is what the run
      settled on -- ``ok=True`` there (a later restore succeeded) is never a hard failure;
    * the ``local_overrides_unrestored`` fact is written only by ``update_cmd_stash`` on the
      UNSOLICITED park paths (a conflicted restore, an unknown untracked baseline). A park the
      user asked for -- ``--keep-stash``, a declined restore, a configured discard -- records
      ``ok=False`` as well and deliberately does NOT write the fact.
    """
    fact = receipt.get(FACT) if isinstance(receipt, dict) else None
    if not isinstance(fact, dict) or not fact.get("stash_ref"):
        return None
    step = last_stash_step(receipt)
    if step is not None and step.get("ok") is True:
        return None
    return fact


def from_request(request: Any) -> Optional[dict]:
    """The hard-failure block for a completion request, read from the live receipt when one is active.

    The completion child hydrates its receipt from ``request["receipt"]``; prefer the active
    receipt when this interpreter has one (it carries every later step the snapshot lacks).
    """
    from hermes_cli import update_receipt

    data = getattr(update_receipt._current.get(), "data", None)
    if not isinstance(data, dict):
        data = request.get("receipt") if isinstance(request, dict) else None
    return unrestored_local_changes(data)


def reapply_command(stash_ref: str) -> str:
    """The one command that puts the parked override set back (`--3way` survives the conflicts)."""
    return f"git stash show -p {stash_ref} | git apply --3way"


def record_unrestored_local_changes(
    stash_ref: str, *, file_count: Optional[int] = None, conflicted: str = "", detail: str = "",
) -> None:
    """Land the hard-failure fact on the run's receipt. No-op without a stash ref (nothing to name)."""
    if not stash_ref:
        return
    from hermes_cli.update_receipt import record_fact

    record_fact(FACT, {
        "stash_ref": str(stash_ref),
        "file_count": int(file_count) if file_count is not None else None,
        "conflicted": [line.strip() for line in str(conflicted).splitlines() if line.strip()],
        "detail": " ".join(str(detail or "").split())[:200],
        "reapply": reapply_command(str(stash_ref)),
    })


def card_title(hard: dict) -> str:
    count = hard.get("file_count")
    parked = f"{count} local modification(s)" if count is not None else "the local override set"
    return (f"Update autostash unrestored: {parked} NOT re-applied, fleet held "
            f"(stash {str(hard.get('stash_ref'))[:12]})")


def card_body(hard: dict) -> str:
    """The remediation card's body: deterministic facts, no prose a human must interpret."""
    stash_ref = str(hard.get("stash_ref") or "")
    count = hard.get("file_count")
    conflicted = [str(path) for path in (hard.get("conflicted") or [])]
    lines = [
        "`hermes update` stashed the local modification(s) before it moved the checkout and the",
        "restore did NOT happen, so the tree the fleet runs does NOT carry the local override set.",
        "The fleet restart was SKIPPED (a restart onto the stripped tree is the live outage) and the",
        "run's own verdict is not a success.",
        "",
        "Deterministic facts, from the run's own receipt:",
        f"- stash ref: `{stash_ref}`",
        f"- stashed paths: {count if count is not None else 'unknown'}",
        "- conflicted files:",
    ]
    lines += [f"    - `{path}`" for path in conflicted] or ["    - (none reported)"]
    if hard.get("detail"):
        lines.append(f"- disposition: {hard['detail']}")
    lines += [
        "",
        "Inspect: `git stash show --stat " + stash_ref + "`",
        "Re-apply: `" + reapply_command(stash_ref) + "`",
        "",
        "The stash is the only copy of the override set -- do not drop it. Re-applying it and",
        "restarting the fleet are operator-authorised ops actions (this card's seat).",
    ]
    return "\n".join(lines)


def idempotency_key(hard: dict) -> str:
    """One card per parked stash: a re-run of the guard must not file a second one."""
    return f"update-autostash-unrestored:{hard.get('stash_ref')}"


def file_card(hard: dict, *, board: Optional[str] = None) -> Optional[str]:
    """File the remediation card on the platform lane; its task id, or ``None`` when the board refused.

    Never raises: the code already moved, so an unwritable board is reported, never fatal, and
    the receipt's own ``local_overrides_unrestored`` fact still names the recovery.
    """
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        with kbc.connect_closing(board=board) as conn:
            return kb.create_task(
                conn, title=card_title(hard), body=card_body(hard),
                assignee=CARD_ASSIGNEE, created_by=CARD_CREATED_BY, priority=CARD_PRIORITY,
                workspace_kind="scratch", idempotency_key=idempotency_key(hard),
                completion_contract="local-only",
            )
    except Exception as exc:  # health: allow BLE001 -- post-commit: the receipt fact is the record
        _log.warning("Could not file the unrestored-autostash card: %s", exc)
        print(f"  ⚠ Could not file the unrestored-autostash card: {exc}")
        return None


def hold_fleet_and_file_card(hard: dict, *, board: Optional[str] = None) -> Optional[str]:
    """Skip the fleet restart, mark the stage, file the card, and say why. Returns the card id."""
    from hermes_cli import update_receipt

    stash_ref = str(hard.get("stash_ref") or "")
    count = hard.get("file_count")
    print()
    print("✗ Local modifications were stashed and NOT restored -- the tree is missing the local"
          " override set.")
    print(f"  Stash ref: {stash_ref}" + (f"  ({count} path(s))" if count is not None else ""))
    print("  The fleet is NOT restarted: a restart onto the stripped tree is the live outage.")
    update_receipt.record_skip("gateway_restart", f"local overrides unrestored (stash {stash_ref[:12]})")
    update_receipt.record_stage("restart", "skipped")
    task_id = file_card(hard, board=board)
    if task_id:
        print(f"  Remediation card filed: {task_id}")
    else:
        print("  ⚠ No remediation card could be filed; re-apply with: " + reapply_command(stash_ref))
    return task_id
