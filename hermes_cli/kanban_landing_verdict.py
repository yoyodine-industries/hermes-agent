"""The landing-verdict writer: ONE door that records a landing verdict on the card that
DECLARES the PR, usable from a dispatched worker.

WHY THIS EXISTS (measured, card ``t_765136ce``). The landing-carrier clause
(``hermes_cli/kanban_landing_carrier.py``, ruling ``t_9333ca24``, widened ``t_502a28bd``)
gives a close exactly two recorded escapes, and the landing reconciler reads one of them
from the card that DECLARES the PR: ``metadata.landing_not_owed`` (``exempt``) and
``metadata.landing_deferred`` (``deferred``). A worker dispatched onto a ``[land]`` carrier
can *reach the right verdict* -- "this PR is superseded, the landing is not owed" -- and had
**no sanctioned way to write it where the reconciler reads it**:

  * the only writer was ``hermes kanban edit --metadata`` (``kanban_db.edit_task``), and that
    CLI is refused in a worker context by design: a worker's shell is a fenced descendant
    (``agent.delegation_context.kanban_path_is_fenced``), so every board mutation it shells
    out to is denied;
  * ``edit_task`` also REPLACES ``task_runs.metadata`` wholesale, so a caller must merge by
    hand -- a silent way to drop a run's handoff facts;
  * no ``kanban_*`` tool writes another card's run metadata (``kanban_complete`` /
    ``kanban_request_review`` are own-card, ``kanban_comment`` is prose).

The cost was a churn loop, not an inconvenience. ``landing_reconcile`` skips an ARCHIVED
carrier, so a declaring card held in ``carried`` by a carrier that board hygiene later
archives silently reverts to ``gap`` and a twin ``[land]`` card is filed again -- measured on
``t_c783ea81`` / carrier ``t_16f7fca0`` (skills-refactor), which is why the same card kept
being dispatched after its predecessor closed nothing.

THE SURFACE IS A TOOL, NOT A CLI VERB (design decision of record on the card: "choose the
cheaper one and say which"). The CLI verb is the WRONG side of the fence: ``hermes kanban …``
runs in the worker's shell, which is exactly the context the fence denies, so a verb would
have had to be carved out of ``_DELEGATED_CHILD_DENIED_ACTIONS`` *and* out of
``kanban_db.write_txn``'s child-context guard -- a hole in the guard, exploited by anything
else the worker shells out to. A ``kanban_*`` tool runs in the worker's own agent process,
where the fence does not apply, and can be gated per tool. Measured on this card: the tool
surface writes from a worker's agent process while the same process's shell is refused
``kanban: delegate_task child contexts cannot mutate Kanban tasks via the CLI``.

WHAT IT IS. A writer, not a gate. It never lowers the completion clause: ``_gate_landing_carrier``
keeps refusing a close that declares an unlanded PR with no exemption, and ``force`` still does
not open it. It only lets a worker put the ALREADY-VALID exemption where the clause and the
reconciler both read it.

VALIDATION IS THE CLAUSE'S OWN -- one validator, no second dialect. ``landing_not_owed`` is
judged by ``is_named_exemption`` (substantive, >= ``MIN_REASON``, and naming its replacement:
a ``repo#N`` ref, a card id or a URL); ``landing_deferred`` by the length floor the clause
applies to it (``>= MIN_REASON``). Those are the *exact* predicates
``kanban_landing_carrier.evaluate`` applies to the same values, so a reason this door accepts
is a reason a completion would accept, and one it refuses is one a completion would refuse.

REFUSALS (each one is a silent no-op this door exists to prevent):
  * ``reason`` bodyless, under the floor, or (``landing_not_owed``) naming no referent;
  * a target that does not exist, or that is ARCHIVED (the reconciler skips archived cards, so
    a verdict written there is invisible);
  * a target that DECLARES no deliverable PR at all -- a verdict on such a card discharges
    nothing while looking like a discharge. "Declares" is read with the clause's own key
    precedence (``published_pr`` at tier 1; the fleet's ``pr``/``pr_url``/``pr_number``/
    ``merge_pr``/``pull_request`` at tier 2 -- the reconciler's own key set, ruling
    ``t_f1dc66dc``; then an exact PR-URL ``completion_contract`` at tier 3) plus the
    bare-number + repo-key shape the reconciler resolves (``repo`` / ``repository`` /
    ``destination_repo`` / ``target_repo``), so the door and the reconciler agree on what a
    declaration IS.

MERGE, NEVER REPLACE. The verdict is merged into the NEWEST run's metadata; every existing key
on that run survives, and ``tasks.result`` is untouched unless the caller passes one. Writing
to the newest run is what the readers expect: ``kanban_landing_carrier._run_metadata`` reads
every run, and the reconciler's ``metadata_of`` merges newest-first -- so an older run's
verdict stays visible and a newer one shadows it.

AUDIT BOTH ENDS. One ``landing_verdict_recorded`` row on the target and one on the caller's own
card, so the act is visible in both histories without reading run JSON.

IDEMPOTENT. A re-run whose effective verdict (merged across the card's runs) already equals the
requested one changes nothing and appends no duplicate event on the target; the no-op is
recorded on the caller's card only, as ``landing_verdict_noop``.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Optional

__all__ = [
    "VERDICTS",
    "LandingVerdictRefused",
    "declaration",
    "declared_refs",
    "record_landing_verdict",
    "validate_reason",
]

#: The two RECORDED escapes the clause honours, and the only keys this door can write. The
#: key IS the verdict: one vocabulary, so a reader cannot be looking at a third word.
VERDICTS = ("landing_not_owed", "landing_deferred")

#: Bare-number declarations resolve their repository from one of these fields (the reconciler's
#: own resolution set, ``landing_reconcile_nodes.declared_ref``).
_REPO_KEYS = ("repo", "repository", "destination_repo", "target_repo")
_PR_KEYS = ("published_pr", "pr", "pr_url", "pr_number", "merge_pr", "pull_request")


class LandingVerdictRefused(ValueError):
    """The door refused; the message is the remedy the caller has to act on."""


def _clause():
    """The clause module, imported lazily (one validator; never a local re-implementation)."""
    from hermes_cli import kanban_landing_carrier as clause

    return clause


def _as_dict(raw: object) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def validate_reason(verdict: str, reason: object) -> str:
    """``reason`` stripped, or :class:`LandingVerdictRefused` -- the clause's own predicate."""
    if verdict not in VERDICTS:
        raise LandingVerdictRefused(
            f"verdict must be one of {', '.join(VERDICTS)}; got {verdict!r}. Nothing was written."
        )
    clause = _clause()
    text = reason.strip() if isinstance(reason, str) else ""
    if not text:
        raise LandingVerdictRefused(
            "reason is required: a bodyless verdict is the silent door this door exists to close. "
            f"Nothing was written. Say WHY it is {verdict}, and (for landing_not_owed) WHAT the "
            "landing rides instead -- a repo#N ref, a card id or a URL."
        )
    if verdict == "landing_not_owed":
        if not clause.is_named_exemption(text):
            raise LandingVerdictRefused(
                f"reason refused: landing_not_owed needs >= {clause.MIN_REASON} characters AND must "
                "name the replacement it rides (an owner/repo#N ref, a card id t_..., or a URL); got "
                f"{len(text)} character(s) naming no referent. Nothing was written. This is the "
                "same predicate a completion applies, so a reason refused here would be refused "
                "there too."
            )
        return text
    if len(text) < clause.MIN_REASON:
        raise LandingVerdictRefused(
            f"reason refused: landing_deferred needs >= {clause.MIN_REASON} characters; got "
            f"{len(text)}. Nothing was written. A deferral is the landing reconciler's input "
            "class -- it must say enough for the lane that picks it up."
        )
    return text


def _run_metadata(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT metadata FROM task_runs WHERE task_id = ? ORDER BY id DESC", (task_id,)
        ).fetchall()
    except sqlite3.Error:
        return []
    out: list[dict] = []
    for row in rows:
        raw = row["metadata"] if "metadata" in getattr(row, "keys", list)() else row[0]
        parsed = _as_dict(raw)
        if parsed:
            out.append(parsed)
    return out


def _effective(mds: list[dict], key: str) -> Optional[str]:
    """The verdict in force: newest run wins, exactly as both readers resolve it."""
    for md in mds:
        value = md.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _bare_number_with_repo(md: dict) -> bool:
    """A ``pr_number``-style declaration whose repository the reconciler resolves by KEY."""
    return bool(_bare_number_keys(md))


def _repo_key(md: dict) -> str:
    for key in _REPO_KEYS:
        value = md.get(key)
        if isinstance(value, str) and value.strip():
            return key
    return ""


def _bare_number_keys(md: dict) -> list[str]:
    """The keys that make a bare-number declaration resolvable, empty when it is not.

    Returned so the receipt can say WHICH surface resolved a low-confidence declaration:
    the number key alone resolves nothing (the repository is unknowable), so the pair is
    the declaration.
    """
    repo_key = _repo_key(md)
    if not repo_key:
        return []
    repo = md.get(repo_key)
    repo = repo.strip() if isinstance(repo, str) else ""
    if not repo or "/" not in repo or repo.startswith(("http://", "https://")):
        return []
    owner, _, name = repo.partition("/")
    if not owner or not name or "/" in name:
        return []
    for key in _PR_KEYS:
        value = md.get(key)
        if value is None or isinstance(value, bool):
            continue
        if str(value).strip().isdigit():
            return sorted([repo_key, key])
    return []


def declaration(
    *, metadata: Optional[dict], runs_metadata: list[dict], contract: Optional[str] = None
) -> tuple[int, list[str], list[str]]:
    """-> ``(tier, refs, keys)``: what this card declares, and WHICH surface resolved it.

    The tier is the clause's own (:func:`kanban_landing_carrier.declared_prs`): 1
    ``published_pr``, 2 the fleet's looser keys (``pr`` / ``pr_url`` / ``pr_number`` /
    ``merge_pr`` / ``pull_request``), 3 an exact PR-URL ``completion_contract``. Recording
    the tier is what makes a verdict written off a looser key visibly lower-confidence
    than one written off the kernel's own publication key (ruling ``t_f1dc66dc``).

    Two shapes count, because both are live in the fleet and the reconciler resolves both:
    a normalisable ref (PR URL or ``owner/repo#N``) found by the clause's own
    ``declared_prs`` precedence, and a bare number carrying its repository in a sibling key
    (``repo`` / ``repository`` / ``destination_repo`` / ``target_repo``) -- the same tier 2,
    with the pair of keys that resolved it.
    """
    clause = _clause()
    tier, refs, keys = clause.declared_prs(
        metadata=metadata, runs_metadata=runs_metadata, contract=contract
    )
    if refs:
        return tier, list(refs), list(keys)
    for md in ([metadata] if isinstance(metadata, dict) else []) + list(runs_metadata):
        bare_keys = _bare_number_keys(md)
        if bare_keys:
            return 2, ["<bare pr_number + repo key>"], bare_keys
    return 0, [], []


def declared_refs(
    *, metadata: Optional[dict], runs_metadata: list[dict], contract: Optional[str] = None
) -> list[str]:
    """The deliverable PRs this card declares; empty when it declares none.

    The refs only -- :func:`declaration` is the same read that also names the tier and keys.
    """
    _tier, refs, _keys = declaration(
        metadata=metadata, runs_metadata=runs_metadata, contract=contract
    )
    return refs


def record_landing_verdict(
    conn: sqlite3.Connection,
    target_task_id: str,
    verdict: str,
    reason: object,
    *,
    caller_task_id: Optional[str] = None,
    caller_run_id: Optional[int] = None,
    result: Optional[str] = None,
    board: Optional[str] = None,
) -> dict:
    """Record ``verdict``/``reason`` on the card that DECLARES the PR. Returns the receipt.

    Raises :class:`LandingVerdictRefused` (a ``ValueError``) on every refusal, always before
    the transaction opens, so nothing is half-written. The write itself rides
    ``kanban_db.write_txn`` -- the fence, the event log and the audit row are the kernel's,
    not this module's, and a delegate_task child context is refused by the same guard that
    refuses every other board write.
    """
    from hermes_cli import kanban_db as kb

    verdict = str(verdict or "").strip()
    text = validate_reason(verdict, reason)

    target = str(target_task_id or "").strip()
    if not target:
        raise LandingVerdictRefused("target_task_id is required: name the card that DECLARES the PR.")
    caller = str(caller_task_id or "").strip() or None
    if caller == target:
        caller = None

    task = kb.get_task(conn, target)
    if task is None:
        raise LandingVerdictRefused(f"{target} does not exist on this board. Nothing was written.")
    status = (task.status or "").strip().lower()
    if status == "archived":
        raise LandingVerdictRefused(
            f"{target} is archived: the landing reconciler skips archived cards, so a verdict "
            "written there would be invisible -- unarchive the declaring card first (the verdict "
            "belongs on the DECLARING card, not on the carrier). Nothing was written."
        )

    mds = _run_metadata(conn, target)
    tier, refs, keys = declaration(
        metadata=None,
        runs_metadata=list(reversed(mds)),  # newest-last, the clause's own order
        contract=getattr(task, "completion_contract", None),
    )
    if not refs:
        raise LandingVerdictRefused(
            f"{target} declares no deliverable PR, so a landing verdict on it discharges "
            "nothing. Declarations are read from the SAME surface the landing reconciler "
            "reads: metadata published_pr (tier 1), then the fleet's pr / pr_url / pr_number "
            "/ merge_pr / pull_request (tier 2), either as a PR URL or owner/repo#N (or a bare "
            "number with a repo/repository/destination_repo/target_repo field), then a PR-URL "
            "completion_contract. Nothing was written -- name the card whose own close "
            "declared the PR."
        )

    previous = _effective(mds, verdict)
    if previous == text and result is None:
        _record_noop(conn, caller, caller_run_id, target, verdict, text, board=board,
                     declared={"tier": tier, "keys": keys})
        return {
            "target": target, "verdict": verdict, "reason": text, "changed": False,
            "prs": refs, "tier": tier, "keys": keys, "caller": caller,
            "note": "the effective verdict already equals this one",
        }

    run_id = _newest_run_id(conn, target)
    with kb.write_txn(conn):
        if run_id is None:
            # A card that was never run still declares (an exact PR-URL contract, a metadata
            # handoff written by hand). Give the verdict a carrier run of its own rather than
            # dropping it on the floor; ``completed`` keeps the failure-streak logic honest
            # (_NON_FAILURE_RUN_OUTCOMES) for a card that has no attempts to fail.
            run_id = kb._synthesize_ended_run(
                conn, target, outcome="completed",
                summary=f"landing verdict recorded: {verdict}", metadata={verdict: text},
            )
            changed = True
        else:
            row = conn.execute("SELECT metadata FROM task_runs WHERE id = ?", (run_id,)).fetchone()
            current = _as_dict(row["metadata"] if row is not None else None)
            if current.get(verdict) != text:
                current[verdict] = text
                conn.execute(
                    "UPDATE task_runs SET metadata = ? WHERE id = ?",
                    (json.dumps(current, ensure_ascii=False), run_id),
                )
                changed = True
            else:
                changed = False
        if result is not None:
            conn.execute("UPDATE tasks SET result = ? WHERE id = ?", (result, target))
        declared = {"tier": tier, "keys": keys}
        _append_audit(
            conn, target, run_id, verdict, text, refs,
            caller=caller, board=board, then="recorded" if changed else "noop",
            declared=declared,
        )
        if caller:
            _append_audit(
                conn, caller, caller_run_id, verdict, text, refs,
                caller=None, board=board, then="recorded" if changed else "noop",
                target=target, declared=declared,
            )
    return {
        "target": target, "verdict": verdict, "reason": text, "changed": changed,
        "run_id": run_id, "prs": refs, "tier": tier, "keys": keys, "caller": caller,
    }


def _newest_run_id(conn: sqlite3.Connection, task_id: str) -> Optional[int]:
    try:
        row = conn.execute(
            "SELECT id FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,)
        ).fetchone()
    except sqlite3.Error:
        return None
    return int(row["id"]) if row is not None else None


def _append_audit(
    conn: sqlite3.Connection, task_id: str, run_id: Optional[int], verdict: str, reason: str,
    refs: list[str], *, caller: Optional[str], board: Optional[str], then: str,
    target: Optional[str] = None, declared: Optional[dict] = None,
) -> None:
    """One event row per end of the act -- the target's history and the caller's.

    ``declared`` records WHICH declaration surface resolved (tier + keys), so a verdict
    written off a looser fleet key is visibly lower-confidence in the audit than one taken
    off ``published_pr`` (ruling ``t_f1dc66dc``).
    """
    from hermes_cli import kanban_db as kb

    payload = {
        "verdict": verdict,
        "reason": reason,
        "prs": list(refs)[:5],
        "actor": "kanban_record_landing_verdict",
        "outcome": then,
    }
    if declared:
        payload["declared_by"] = dict(declared)
    if board:
        payload["board"] = board
    if caller:
        payload["caller_task_id"] = caller
    if target:
        payload["target_task_id"] = target
    kind = "landing_verdict_recorded" if then == "recorded" else "landing_verdict_noop"
    kb._append_event(conn, task_id, kind, payload, run_id=run_id)


def _record_noop(
    conn: sqlite3.Connection, caller: Optional[str], caller_run_id: Optional[int],
    target: str, verdict: str, reason: str, *, board: Optional[str],
    declared: Optional[dict] = None,
) -> None:
    """A repeat of an already-effective verdict: no target write, one recorded no-op.

    ``declared`` carries the same tier/keys the recorded path writes, so the ruling's
    "the receipt/audit row records WHICH tier/keys resolved" holds on EVERY write, not
    only the first one (review round 1, R2 on card ``t_f1dc66dc``).
    """
    from hermes_cli import kanban_db as kb

    if not caller:
        return
    with kb.write_txn(conn):
        kb._append_event(
            conn, caller, "landing_verdict_noop",
            {"verdict": verdict, "reason": reason, "target_task_id": target,
             "actor": "kanban_record_landing_verdict", "outcome": "noop",
             **({"declared_by": dict(declared)} if declared else {}),
             **({"board": board} if board else {})},
            run_id=caller_run_id,
        )
