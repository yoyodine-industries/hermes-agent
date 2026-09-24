"""Scheduled, capped triage drain.

A deliberate, owned pass over the triage column - the counterpoint to the
gateway's 60s ``auto_decompose`` tick. Runs from cron (top-level, NOT a
dispatcher worker), disposes every parked triage card through the existing
``specify`` / ``decompose`` machinery, and enforces two deterministic caps that
the tick never had:

    per-card child cap   a single card fans out to at most N children
    per-run child cap    the whole run creates at most M children total

Disposition of each triage card is decided by the deterministic scope gate
(hermes_cli.kanban_scope), not LLM judgement:

    ok          scope declared and within threshold -> promote (dispatch whole)
    oversize    scope over threshold                 -> decompose into bounded children
    no_scope    no SCOPE line                        -> refuse with a reason comment
    unparseable SCOPE line present but unreadable    -> refuse with a reason comment

Children are created with ``auto_promote=False`` so they land ``todo``; the
dispatcher promotes and spawns them on its own schedule, subject to
``max_in_progress``, so the drain never bursts more workers than the board
allows. The run produces a machine-readable report (JSON) and a human log.

Caps default to 3 to match the operator's ``max_in_progress: 3``. All are
overridable via CLI flags / env.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_db_graph import decompose_triage_task
from hermes_cli.kanban_scope import scope_verdict
from hermes_cli.kanban_specify import (
    _call_aux,
    _extract_json_blob,
    _load_triage_task,
    _task_prompt_fields,
)
from hermes_cli.kanban_decompose import (
    _SYSTEM_PROMPT,
    _USER_TEMPLATE,
    _FENCE_RE,
    _apply_single,
    _clean_children,
    _format_roster,
    _load_routing,
    _profile_author,
)

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 30
DEFAULT_PER_CARD_CAP = 3
DEFAULT_PER_RUN_CAP = 3

# The decompose prompt's task-count guidance, overridden per-cap below. If the
# upstream string ever drifts, the replace is a no-op and the HARD cap below still
# enforces the limit, so the drain fails safe.
_TASK_COUNT_GUIDANCE = (
    "  - Use 2-6 tasks for normal work. Don't create 20 tiny tasks. Don't\n"
    "    cram everything into 1 task."
)


@dataclass
class DrainCaps:
    threshold: int = DEFAULT_THRESHOLD
    per_card_cap: int = DEFAULT_PER_CARD_CAP
    per_run_cap: int = DEFAULT_PER_RUN_CAP


@dataclass
class Action:
    task_id: str
    disposition: str  # accept | decompose | refuse
    reason: str = ""
    children_created: int = 0
    child_ids: list[str] = field(default_factory=list)
    comment_added: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _capped_system_prompt(cap: int) -> str:
    """The decompose system prompt with the per-card child cap injected."""
    guidance = (
        f"  - Use at most {cap} tasks. Never return more than {cap}. Group\n"
        "    related steps if the work is larger; a split above the cap is\n"
        "    refused and retried next pass."
    )
    return _SYSTEM_PROMPT.replace(_TASK_COUNT_GUIDANCE, guidance)


_REFUSE_PREFIX = "Triage drain: refused"


def _already_refused(task_id: str, author: str) -> bool:
    """True when the card's latest comment is already a drain-refuse note from
    this author, so a scheduled pass does not re-comment the same parked card
    every tick (e.g. a privileged step that legitimately stays in triage)."""
    try:
        with kbc.connect_closing() as conn:
            comments = kb.list_comments(conn, task_id)
    except Exception as exc:
        logger.warning("drain: comment check on %s failed: %s", task_id, exc)
        return False
    if not comments:
        return False
    last = comments[-1]
    return last.author == author and (last.body or "").startswith(_REFUSE_PREFIX)


def _refuse_comment_text(verdict_status: str) -> str:
    if verdict_status == "no_scope":
        return (
            "Triage drain: refused (no SCOPE line). Declare scope as a file count "
            "with exclusions, e.g. `SCOPE: 12 files under <path>, excluding .git/**`, "
            "then the next drain pass can size and route it."
        )
    if verdict_status == "unparseable":
        return (
            "Triage drain: refused (SCOPE line unparseable). Use the form "
            "`SCOPE: <N> files under <path>[, excluding <glob>, ...]`."
        )
    return "Triage drain: refused."


def _add_comment(task_id: str, author: str, body: str) -> bool:
    try:
        with kbc.connect_closing() as conn:
            kb.add_comment(conn, task_id, author, body)
        return True
    except Exception as exc:
        logger.warning("drain: comment on %s failed: %s", task_id, exc)
        return False


def _decompose_capped(
    task: kb.Task,
    routing,
    caps: DrainCaps,
    *,
    author: str,
    remaining_budget: int,
    timeout: int,
    dry_run: bool,
    force_fanout: bool = False,
) -> Action:
    """Run the LLM decompose for one card with the cap enforced. ``remaining_budget``
    is the number of children this run may still create; a fan-out larger than that
    is refused (retried next pass) rather than truncated. ``force_fanout`` marks an
    oversize card: a ``fanout=false`` from the decomposer is refused rather than
    dispatched whole."""
    task_id = task.id
    # Prompt with the natural per-card cap (never clamped to the run budget):
    # a card that genuinely needs N>remaining children must be refused and retried
    # whole, not truncated into a budget-shaped split the LLM invents.
    effective_cap = caps.per_card_cap

    raw, reason = _call_aux(
        "decompose", task_id, aux_task="kanban_decomposer",
        system=_capped_system_prompt(effective_cap),
        user=_USER_TEMPLATE.format(
            **_task_prompt_fields(task),
            roster=_format_roster(routing.roster),
            default_assignee=routing.default_assignee,
        ),
        max_tokens=4000, timeout=timeout, log=logger,
    )
    if raw is None:
        return Action(task_id, "refuse", f"decompose LLM failed: {reason}")

    parsed = _extract_json_blob(raw, _FENCE_RE)
    if parsed is None:
        return Action(task_id, "refuse", "decompose LLM returned malformed JSON")

    if not parsed.get("fanout"):
        if force_fanout:
            return Action(
                task_id, "refuse",
                "oversize card (scope above threshold) returned fanout=false; must split",
            )
        # Single unit: same effect as specify (tighten + promote).
        if dry_run:
            return Action(task_id, "accept", "single task (no fanout)", children_created=0)
        outcome = _apply_single(task, parsed, routing, author)
        return Action(task_id, "accept" if outcome.ok else "refuse", outcome.reason)

    raw_tasks = parsed.get("tasks") or []
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return Action(task_id, "refuse", "decomposer returned fanout=true with empty tasks list")

    children, reason = _clean_children(task_id, raw_tasks, routing)
    if reason:
        return Action(task_id, "refuse", reason)
    if len(children) > caps.per_card_cap:
        return Action(
            task_id, "refuse",
            f"decomposer proposed {len(children)} children, over per-card cap {caps.per_card_cap}",
        )
    if len(children) > remaining_budget:
        return Action(
            task_id, "refuse",
            f"decomposer proposed {len(children)} children, over remaining run budget {remaining_budget}",
        )

    if dry_run:
        return Action(
            task_id, "decompose", f"would decompose into {len(children)} children",
            children_created=len(children),
            child_ids=[c.get("title", "")[:40] for c in children],
        )
    try:
        with kbc.connect_closing() as conn:
            child_ids = decompose_triage_task(
                conn, task_id, root_assignee=routing.orchestrator,
                children=children, author=author, auto_promote=False,
            )
    except ValueError as exc:
        return Action(task_id, "refuse", f"DB rejected graph: {exc}")
    except Exception as exc:
        logger.exception("drain: DB error decomposing %s", task_id)
        return Action(task_id, "refuse", f"DB error: {type(exc).__name__}")
    if child_ids is None:
        return Action(task_id, "refuse", "task already decomposed or moved out of triage")
    return Action(
        task_id, "decompose", f"decomposed into {len(child_ids)} children (capped; land todo)",
        children_created=len(child_ids), child_ids=child_ids,
    )


def _list_triage_ids() -> list[str]:
    with kbc.connect_closing() as conn:
        rows = kb.list_tasks(conn, status="triage", limit=1000)
    return [r.id for r in rows]


def drain_triage(
    *,
    board: Optional[str] = None,
    caps: Optional[DrainCaps] = None,
    dry_run: bool = False,
    timeout: int = 180,
) -> dict:
    """Dispose every triage card on ``board``. Returns a report dict."""
    caps = caps or DrainCaps()
    author = _profile_author()
    routing = _load_routing()
    triage_ids = _list_triage_ids()

    report = {
        "board": str(kb.kanban_db_path(board=board)),
        "ts": int(time.time()),
        "dry_run": dry_run,
        "threshold": caps.threshold,
        "per_card_cap": caps.per_card_cap,
        "per_run_cap": caps.per_run_cap,
        "triage_count": len(triage_ids),
        "children_created": 0,
        "actions": [],
    }

    # Two passes so decompose children stay ``todo``: a bounded card's accept path
    # calls ``recompute_ready`` (inside ``specify_triage_task``), which promotes
    # every parent-free ``todo`` task — including children a prior card in this run
    # just decomposed — to ``ready``. Deferring decomposes until after the accepts
    # means nothing recomputes after the children are inserted, so they land (and
    # remain) ``todo`` as the operator requires. Load + classify first so the run
    # never mutates a card before its verdict is read.
    entries = []
    for tid in triage_ids:
        task, reason = _load_triage_task(tid)
        if task is None:
            entries.append((tid, None, None, reason))
            continue
        verdict = scope_verdict(task.body or "", caps.threshold)
        entries.append((tid, task, verdict, None))

    def _phase(entry) -> int:
        # accept (0) -> decompose (1) -> refuse (2); sort ascending.
        _tid, task, verdict, _reason = entry
        if task is None or verdict.status in ("no_scope", "unparseable"):
            return 2
        if verdict.status == "oversize":
            return 1
        return 0

    created_total = 0
    for tid, task, verdict, load_reason in sorted(entries, key=_phase):
        if task is None:
            report["actions"].append(Action(tid, "refuse", load_reason).to_dict())
            continue

        if verdict.status in ("no_scope", "unparseable"):
            comment = _refuse_comment_text(verdict.status)
            added = False
            if not dry_run:
                if _already_refused(tid, author):
                    comment += " (already refused by a prior pass; skipped re-comment)"
                else:
                    added = _add_comment(tid, author, comment)
            report["actions"].append(
                Action(tid, "refuse", comment, comment_added=added).to_dict()
            )
            continue

        # scoped (ok or oversize): run the capped LLM decomposer. A bounded card
        # may still fan out (a natural multi-unit split); an oversize card MUST.
        # The per-run budget is enforced inside _decompose_capped on the fan-out
        # result, so a single-task card (0 children) is never deferred by it.
        remaining = caps.per_run_cap - created_total
        action = _decompose_capped(
            task, routing, caps, author=author, remaining_budget=remaining,
            timeout=timeout, dry_run=dry_run,
            force_fanout=(verdict.status == "oversize"),
        )
        created_total += action.children_created
        report["actions"].append(action.to_dict())

    report["children_created"] = created_total
    return report


def _report_text(report: dict) -> str:
    lines = [
        f"triage drain  board={report['board']}  dry_run={report['dry_run']}",
        f"  threshold={report['threshold']}  per_card_cap={report['per_card_cap']}  "
        f"per_run_cap={report['per_run_cap']}",
        f"  triage cards: {report['triage_count']}   children created: {report['children_created']}",
    ]
    for a in report["actions"]:
        detail = a["reason"]
        if a["child_ids"]:
            detail += " -> " + ", ".join(a["child_ids"])
        lines.append(f"  [{a['disposition']:9}] {a['task_id']}: {detail}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Scheduled, capped kanban triage drain.")
    ap.add_argument("--board", default=None, help="Board slug (default: env-resolved).")
    ap.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD,
                    help=f"One-turn file-count threshold (default {DEFAULT_THRESHOLD}).")
    ap.add_argument("--per-card-cap", type=int, default=DEFAULT_PER_CARD_CAP,
                    help=f"Max children one card may fan out to (default {DEFAULT_PER_CARD_CAP}).")
    ap.add_argument("--per-run-cap", type=int, default=DEFAULT_PER_RUN_CAP,
                    help=f"Max children the whole run may create (default {DEFAULT_PER_RUN_CAP}).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute and print the disposition without writing.")
    ap.add_argument("--timeout", type=int, default=180, help="Aux LLM timeout seconds.")
    ap.add_argument("--json", action="store_true", help="Emit the report as JSON.")
    ap.add_argument("--log", default=None, help="Append the human log to this file.")
    args = ap.parse_args(argv)

    if args.board:
        # The reused decompose/specify helpers resolve the board from env.
        os.environ["HERMES_KANBAN_BOARD"] = args.board

    caps = DrainCaps(args.threshold, args.per_card_cap, args.per_run_cap)
    report = drain_triage(board=args.board, caps=caps, dry_run=args.dry_run,
                          timeout=args.timeout)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        text = _report_text(report)
        print(text)
        if args.log:
            with open(args.log, "a", encoding="utf-8") as fh:
                fh.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n{text}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
