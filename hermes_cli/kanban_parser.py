"""Argparse tree for ``hermes kanban …`` (``build_parser``).

The subcommand tree is declared as data — one ``_cmd(...)`` record per
subcommand holding its ``add_parser`` kwargs and an ordered tuple of
``add_argument`` specs — and materialised by ``_add_commands``. Order of
records and arguments is the order argparse renders in ``--help``.
"""

from __future__ import annotations

import argparse

from hermes_cli import kanban_bulk_guard as kbg
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_notify as kbn


def _arg(*flags: str, **kw):
    return (flags, kw)


def _cmd(name: str, args=(), *, children=None, **parser_kw):
    """``children`` = ``(dest, [specs])`` for a nested subparser group."""
    return (name, parser_kw, tuple(args), children)


def _add_commands(sub: argparse._SubParsersAction, specs) -> None:
    for name, parser_kw, args, children in specs:
        p = sub.add_parser(name, **parser_kw)
        for flags, kw in args:
            p.add_argument(*flags, **kw)
        if children:
            dest, child_specs = children
            _add_commands(p.add_subparsers(dest=dest), child_specs)


def _json_flag(**kw):
    return _arg("--json", action="store_true", **kw)


def _reason(help: str):
    return _arg("--reason", help=help)


def _nonnegative_int(value: str) -> int:
    """argparse type for retention days: a negative window builds a future cutoff
    that matches every row, so reject it at the CLI boundary before any sweep."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("retention days must be >= 0 (0 disables that sweep)")
    return parsed


def _run_state_args(type_help: str):
    return (
        _arg("--state-type", choices=("status", "outcome"), help=f"With --state-name: {type_help}"),
        _arg("--state-name", metavar="VALUE",
             help="With --state-type: keep runs whose column equals this value"),
    )


def _triage_sweep_args(verb: str, Verb: str, noun: str):
    """Shared ``specify`` / ``decompose`` arguments."""
    return (
        _arg("task_id", nargs="?", help=f"Task id to {verb} (required unless --all is given)"),
        _arg("--all", dest="all_triage", action="store_true", help=f"{Verb} every task currently in the triage column"),
        _arg("--tenant", help="When used with --all, restrict the sweep to this tenant"),
        _arg("--author",
             help=f"Author name recorded on the audit comment (default: $HERMES_PROFILE or '{noun}')"),
        _json_flag(help="Emit one JSON object per task on stdout"),
        _approval_flag(f"a --all {verb} sweep"),
    )


def _bulk_ids(verb: str):
    return _arg("--ids", nargs="+", help=f"Additional task ids to {verb} with the same reason (bulk mode)")


def _quorum_phrase() -> str:
    """The approval count this build enforces, in plain words (single source: the guard)."""
    n = kbg.REQUIRED_APPROVALS
    if n <= 0:
        return ("no separate bot approvals (the ledger, destination claim and verified "
                "snapshot still apply; the ONE waiver is a declared estate board — "
                "`hermes kanban boards rm --estate <slug>`)")
    return f"{n} distinct bot approval(s)"


def _approval_flag(what: str = "this action"):
    return _arg(
        "--approval", metavar="<digest>", default=None,
        help=f"The bulk-guard digest that authorizes {what} (from `hermes kanban "
             f"bulk-approvals ask`). Required for any destructive bulk action: an ask filed by "
             f"the authorized ask profile {kbg.AUTHORIZED_ASK_PROFILE!r} must name this exact "
             f"action -- {_quorum_phrase()} -- and a VERIFIED snapshot of the store must exist. "
             f"Without it the action is REFUSED.",
    )


_TASK_ID = _arg("task_id")
_TASK_IDS = _arg("task_ids", nargs="+")
_SLUG = _arg("slug")
_TENANT = _arg("--tenant", help="Tenant namespace")
_PRIORITY = _arg("--priority", type=int, default=0, help="Priority tiebreaker")
_RECLAIM_REASON = _reason("Human-readable reason (recorded on the reclaimed event)")
_NOTIFY_TARGET = (
    _arg("--platform", required=True),
    _arg("--chat-id", required=True),
    _arg("--thread-id"),
)
_STEP_HANDOFF = (
    _arg("--summary", help="Structured handoff summary. Falls back to --result if omitted."),
    _arg("--metadata", help="JSON dict of structured facts to store on the latest completed run."),
)

# The "how much room does this card get" fields: the goal loop and the wall-clock cap. On ``edit``
# every default MUST be ``None`` — ``store_true``'s False default would read as "turn the loop off"
# on an unrelated ``--priority`` edit and silently clear a card's loop. ``--no-goal`` is the explicit
# off switch that ambiguity needs. ``--max-runtime`` accepts the same durations as ``create`` plus
# ``none`` to clear the cap (a card with a stored ``0`` is not a thing: 0 means "no cap").
_BUDGET_FIELDS = (
    _arg("--goal", action="store_true", dest="goal_mode", default=None,
         help="Run the worker in a goal loop: after each turn a judge checks the "
              "response against the card title/body and, if not done, the worker "
              "keeps going in the same session until the judge agrees it's "
              "complete (or the turn budget runs out, which blocks the card for "
              "review). Best for open-ended cards one shot rarely finishes. The "
              "dispatcher also arms this itself on a card whose run died of "
              "iteration exhaustion."),
    _arg("--no-goal", action="store_false", dest="goal_mode",
         help="Turn the goal loop OFF for this card (single-shot worker per attempt)."),
    _arg("--goal-max-turns", type=int, metavar="N", dest="goal_max_turns",
         help="Turn budget for goal-loop workers (default 20). 0 clears it back to the default."),
    _arg("--max-runtime", dest="max_runtime",
         help="Per-task runtime cap: seconds (7200) or a duration (90s, 30m, 2h, 1d). "
              "'none' clears the cap. When exceeded the dispatcher SIGTERMs (then "
              "SIGKILLs) the worker and re-queues the card."),
)

_BOARD_SPECS = [
    _cmd("list", [
        _json_flag(),
        _arg("--all", action="store_true", help="Include archived boards too"),
    ], aliases=["ls"], help="List all boards with task counts"),
    _cmd("create", [
        _arg("slug", help="Board slug (kebab-case, e.g. atm10-server)"),
        _arg("--name", help="Human-readable display name (defaults to Title Case of slug)"),
        _arg("--description", help="Optional description"),
        _arg("--icon", help="Optional emoji or single-character icon for the dashboard"),
        _arg("--color", help="Optional hex color (e.g. '#8b5cf6') for the dashboard"),
        _arg("--switch", action="store_true", help="Switch to the new board after creating it"),
        _arg("--default-workdir", help="Default workspace path for tasks created on this board"),
        _arg("--no-dispatch", action="store_true", dest="no_dispatch",
             help="Mark an ESTATE/scratch board: write \"dispatch\": false into the "
                  "board's metadata so the dispatcher NEVER serves it. Use this for a "
                  "rehearsal board a SEV1/failure card may be redirected to — a redirect "
                  "to a normal board is not isolation, because the dispatcher serves every "
                  "non-archived board."),
    ], aliases=["new"], help="Create a new board"),
    _cmd("rm", [
        _SLUG,
        _arg("--delete", action="store_true",
             help="Hard-delete the board directory instead of archiving it. "
                  "Default is to move it to boards/_archived/ so it's recoverable."),
        _arg("--estate", action="store_true",
             help="Assert this board is a DECLARED ESTATE (its own board.json carries "
                  "\"dispatch\": false) and tear it down single-actor: the ask/APR clause is "
                  "waived while the destination claim, a VERIFIED preimage and a ledger/audit row "
                  "are still taken. Refuses if the marker is absent, the board is current, or a "
                  "live worker claim exists. Incompatible with --delete."),
        _arg("--reason", default="",
             help="Why the estate is being torn down (required with --estate; lands in the "
                  "ledger and the audit row)."),
        _approval_flag("moving or deleting a whole board"),
    ], aliases=["remove", "delete"], help="Archive (default) or delete a board"),
    _cmd("switch", [_SLUG], aliases=["use"], help="Set the active board for subsequent CLI calls"),
    _cmd("show", [_SLUG, _json_flag()], aliases=["current"], help="Print one board's "
         "record: the currently-active board, or the slug named"),
    _cmd("rename", [_SLUG, _arg("name", help="New display name")],
         help="Change a board's human-readable display name (slug is immutable)"),
    _cmd("set-default-workdir", [
        _SLUG,
        _arg("path", nargs="?", help="Absolute path to use as default workdir. Omit to clear."),
    ], help="Set the default workspace path for tasks on a board"),
    _cmd("set-dispatch", [
        _SLUG,
        _arg("state", choices=("on", "off"),
             help="'off' marks an estate/scratch board the dispatcher must never "
                  "serve; 'on' admits it again"),
        _json_flag(),
    ], help="Admit or exclude a board from the dispatcher's set "
            "(off = estate/scratch board, never served)"),
    _cmd("set-priority-policy", [
        _SLUG,
        _arg("--module", help="Absolute path to the .py module that owns this board's "
                              "priority policy"),
        _arg("--function", help="Callable in that module, invoked as "
                                "(requested, assignee, board, title, body). "
                                "Omit for the default."),
        _json_flag(),
    ], help="Set (or clear, by omitting --module) the board's card-priority policy "
            "(arms the reserved-tranche storage guard)"),
    _cmd("set-operator-register", [
        _SLUG,
        _arg("task_id", nargs="?", help="Card id of the board's operator register "
                                        "(omit to clear)"),
        _json_flag(),
    ], help="Set (or clear) the board's operator-ask register, the anchor "
            "`hermes kanban rollup` walks", description=(
        "The register card that captures operator requests for this board. Cards filed in "
        "service of an ask are stamped with it at the create path, and `hermes kanban "
        "rollup` walks it - by edge and by reference - across every board. Stored in the "
        "board's metadata, so it never touches a board's store schema."
    )),
    _cmd("export", [
        _arg("slug", nargs="?", help="Board to export (default: the current board)"),
        _arg("-o", "--output", help="Archive path (default: ./<slug>.tar.gz)"),
        _arg("--no-attachments", action="store_true", help="Skip attachment files, keeping the archive small"),
        _arg("--include-logs", action="store_true", help="Include per-task worker logs"),
        _json_flag(),
    ], help="Export a board to a portable .tar.gz archive", description=(
        "Package a board's tasks, comments, links, history, and file attachments into one archive "
        "that can be imported on another machine. Claims, worker PIDs, chat subscriptions, and "
        "paths belonging to this machine are stripped. Workspaces are never included — they are "
        "rebuilt on demand."
    )),
    _cmd("import", [
        _arg("archive", help="Path to the .tar.gz archive"),
        _arg("--as", dest="as_slug", help="Slug for the imported board (default: from the archive)"),
        _arg("--switch", action="store_true", help="Switch to the imported board afterwards"),
        _json_flag(),
        _approval_flag("loading a whole board from an archive"),
    ], help="Import a board archive as a new board", description=(
        "Import a .tar.gz produced by `hermes kanban boards export`. The board always lands as a "
        "NEW board — the slug gains a numeric suffix if it is already taken — so an import can "
        "never overwrite or merge into a board you already have."
    )),
]

# Top-level ``hermes kanban <action>`` records, in ``--help`` order.
_SPECS = [
    _cmd("init", help="Create kanban.db if missing (idempotent)"),
    _cmd("boards", children=("boards_action", _BOARD_SPECS),
         help="Manage kanban boards (one board per project / workstream)",
         description=(
             "Boards let you separate unrelated streams of work (projects, repos, domains) into "
             "isolated queues. Each board has its own DB, workspaces directory, and dispatcher "
             "loop — tasks on one board cannot collide with tasks on another. The first board is "
             "'default' and always exists."
         )),
    _cmd("create", [
        _arg("title", help="Task title"),
        _arg("--body", help="Optional opening post"),
        _arg("--body-file", metavar="PATH",
             help="Read the opening post from a file ('-' = stdin), so bodies with embedded "
                  "newlines or flag-like lines survive shell quoting. "
                  "Mutually exclusive with --body."),
        _arg("--assignee", help="Profile name to assign"),
        _arg("--parent", action="append", default=[], help="Parent task id (repeatable)"),
        _arg("--serves", metavar="REF",
             help="The operator ask this card is filed in service of: a register card id "
                  "(a new ask) or <register>/<ask>. Stamps the card's body with "
                  "`Operator-ask: <register>/<ask>` so it rolls up across boards, where a "
                  "parent edge cannot reach. Inherited automatically when the card has a "
                  "parent that serves an ask or is filed by a worker that does."),
        _arg("--workspace",
             help="scratch | worktree | worktree:<path> | dir:<path> (default: worktree "
                  "when the board has a default workdir or project, else scratch; an "
                  "explicit 'scratch' also opts out of a project-scoped board's project)"),
        _arg("--branch", help="Branch name for worktree tasks, e.g. wt/t6-wire"),
        _arg("--project",
             help="Link to a project (id or slug). Anchors the task's "
                  "worktree under the project's primary repo with a "
                  "deterministic branch. See `hermes project list`."),
        _TENANT,
        _PRIORITY,
        _arg("--triage", action="store_true",
             help="Park in triage — a specifier will flesh out the spec and promote to todo"),
        _arg("--idempotency-key",
             help="Dedup key. If a non-archived task with this key exists, "
                  "its id is returned instead of creating a duplicate."),
        _arg("--max-runtime",
             help="Per-task runtime cap. Accepts seconds (300) or durations (90s, "
                  "30m, 2h, 1d). When exceeded, the dispatcher SIGTERMs (then "
                  "SIGKILLs) the worker and re-queues the task."),
        _arg("--created-by", default="user", help="Author name recorded on the task (default: user)"),
        _arg("--skill", action="append", default=[], dest="skills",
             help="Skill to force-load into the worker (repeatable). The kanban "
                  "lifecycle is already injected automatically. Example: --skill "
                  "translation --skill github-code-review"),
        _arg("--max-retries", type=int, metavar="N",
             help="Per-task override for the consecutive-failure "
                  f"circuit breaker. Trip on the Nth failure — e.g. --max-retries 1 blocks on the "
                  f"first failure (no retries), --max-retries 3 allows two retries. Omit to use "
                  f"the dispatcher's kanban.failure_limit config (default "
                  f"{kb.DEFAULT_FAILURE_LIMIT})."),
        _arg("--model", dest="model_override",
             help="Pin the worker to this model (passed as -m <model>) without "
                  "changing the profile's configured model. Combine with --provider "
                  "when the model belongs to a different backend than the profile's default."),
        _arg("--provider", dest="provider_override",
             help="Provider the --model belongs to (passed as --provider <name> to "
                  "the worker). Requires --model."),
        _arg("--completion-contract", metavar="CONTRACT",
             help="local-only (default), OWNER/REPO for publication, or exact GitHub PR URL; required CI gates done."),
        _arg("--goal", action="store_true", dest="goal_mode",
             help="Run the worker in a goal loop: after each turn a judge checks the "
                  "response against the card title/body and, if not done, the worker "
                  "keeps going in the same session until the judge agrees it's "
                  "complete (or the turn budget runs out, which blocks the card for "
                  "review). Best for open-ended cards one shot rarely finishes."),
        _arg("--goal-max-turns", type=int, metavar="N", dest="goal_max_turns",
             help="Turn budget for --goal workers (default 20). Ignored without --goal."),
        _arg("--initial-status", choices=sorted(kb.VALID_INITIAL_STATUSES), default="running",
             help="Initial card status. 'blocked' is RECOGNISED AND REFUSED "
                  "(a card is never created blocked): wait on work with --parent, "
                  "or create the card and park a real blocker with "
                  "`kanban block <id> --kind <k> --reason ...`."),
        _json_flag(help="Emit JSON output"),
    ], help="Create a new task"),
    _cmd("swarm", [
        _arg("goal", help="Swarm goal / final outcome"),
        _arg("--worker", action="append", default=[], metavar="PROFILE:TITLE[:SKILL,SKILL]",
             help="Parallel worker card (repeatable)"),
        _arg("--verifier", required=True, help="Verifier profile"),
        _arg("--synthesizer", required=True, help="Synthesizer/writer profile"),
        _TENANT,
        _PRIORITY,
        _approval_flag("a swarm fan-out"),
        _arg("--created-by", help="Creator/anchor profile"),
        _arg("--idempotency-key", help="Dedup key for the root card"),
        _json_flag(help="Emit JSON output"),
    ], help="Create a Kanban Swarm v1 graph (parallel workers → verifier → synthesizer)"),
    _cmd("list", [
        _arg("--mine", action="store_true", help="Filter by $HERMES_PROFILE as assignee"),
        _arg("--assignee"),
        _arg("--status", choices=sorted(kb.VALID_STATUSES)),
        _arg("--tenant"),
        _arg("--session",
             help="Filter by originating chat/agent session id (set on tasks created from inside an ACP loop)"),
        _arg("--archived", action="store_true", help="Include archived tasks"),
        _json_flag(),
        _arg("--sort", choices=sorted(kb.VALID_SORT_ORDERS.keys()),
             help="Sort order for listed tasks (default: priority)"),
        _arg("--workflow-template-id", metavar="ID", help="Restrict to tasks with this workflow_template_id"),
        _arg("--step-key", dest="current_step_key", metavar="KEY",
             help="Restrict to tasks with this current_step_key"),
    ], aliases=["ls"], help="List tasks"),
    _cmd("show", [_TASK_ID, _json_flag(), *_run_state_args("filter listed runs by task_runs column")],
         help="Show a task with comments + events"),
    _cmd("assign", [_TASK_ID, _arg("profile", help="Profile name (or 'none' to unassign)")],
         help="Assign or reassign a task"),
    _cmd("set-model", [
        _TASK_ID,
        _arg("model", nargs="?", help="Model to pin the worker to (or 'none' to clear the override)"),
        _arg("--provider",
             help="Provider the model belongs to (worker is spawned with "
                  "--provider <name>). Cleared together with the model."),
    ], help="Set or clear a task's model/provider override (takes effect on the next dispatch)"),
    _cmd("set-contract", [
        _TASK_ID,
        _arg("contract",
             help="New completion contract: 'local-only', 'landed' (deploy-proof: the card "
                  "needs a run/probe dated after its landing), 'OWNER/REPO', or an exact GitHub PR URL"),
        _arg("--reason", required=True,
             help="Why the contract is changing (recorded on the contract_changed event; required)"),
        _arg("--author", help="Author name recorded on the change (default: $HERMES_PROFILE or 'user')"),
    ], help="Correct a task's completion contract — the release for a wrong or unsatisfiable one "
            "(top-level only; refused on done/archived cards)"),
    _cmd("reclaim", [_TASK_ID, _RECLAIM_REASON], help="Release an active worker claim on a running task"),
    _cmd("reassign", [
        _TASK_ID,
        _arg("profile", help="New profile name (or 'none' to unassign)"),
        _arg("--reclaim", action="store_true",
             help="Release any active claim before reassigning (required if task is running)"),
        _RECLAIM_REASON,
        _approval_flag("a bulk reclaim-reassign"),
    ], help="Reassign a task to a different profile, optionally reclaiming first"),
    _cmd("diagnostics", [
        _arg("--severity", choices=["warning", "error", "critical"],
             help="Only show diagnostics at or above this severity"),
        _arg("--task", help="Only show diagnostics for one task id"),
        _json_flag(help="Emit JSON (structured) instead of the default human table"),
    ], aliases=["diag"], help="List active diagnostics on the current board"),
    _cmd("link", [_arg("parent_id"), _arg("child_id")], help="Add a parent->child dependency"),
    _cmd("unlink", [_arg("parent_id"), _arg("child_id")], help="Remove a parent->child dependency"),
    _cmd("rollup", [
        _arg("register", nargs="?",
             help="Register card id (default: the current board's designated register)"),
        _json_flag(help="Emit the walk as JSON (groups, state, owner, unresolved refs)"),
    ], aliases=["operator-asks"], help="Everything in service of the operator register, across ALL boards",
        description=(
            "Walks the register's tree by DEPENDENCY EDGE and by the `Operator-ask:` reference "
            "every in-service card carries in its body, so work filed on another board - where "
            "an edge cannot exist - still rolls up. Prints each ask with its cards' state and "
            "owner, and reports a reference naming a card no board holds rather than dropping "
            "it. Set a board's register once with `hermes kanban boards "
            "set-operator-register <task-id>`, then `hermes kanban rollup` with no argument."
        )),
    _cmd("claim", [
        _TASK_ID,
        _arg("--ttl", type=int, default=kb.DEFAULT_CLAIM_TTL_SECONDS, help="Claim TTL in seconds (default: 900)"),
    ], help="Atomically claim a ready task (prints resolved workspace path)"),
    _cmd("comment", [
        _TASK_ID,
        _arg("text", nargs="+", help="Comment body"),
        _arg("--author", help="Author name (default: $HERMES_PROFILE or 'user')"),
        _arg("--max-len", type=int, help="Trim the stored comment body to this many characters"),
    ], help="Append a comment"),
    _cmd("attach", [
        _TASK_ID,
        _arg("path", help="Path to the local file to attach"),
        _arg("--content-type", help="MIME type (default: guessed from the file extension)"),
        _arg("--name", help="Stored filename (default: the source file's basename)"),
        _arg("--author", help="uploaded_by label (default: $HERMES_PROFILE or 'user')"),
    ], help="Attach a local file to a task"),
    _cmd("attachments", [_TASK_ID, _json_flag()], help="List a task's attachments"),
    _cmd("attach-rm", [_arg("attachment_id", type=int)], help="Delete an attachment by id"),
    _cmd("complete", [
        _arg("task_ids", nargs="+", help="One or more task ids (only --result applies to all of them)"),
        _arg("--result", help="Result summary"),
        _arg("--summary",
             help="Structured handoff summary for downstream tasks. Falls back to --result if omitted."),
        _arg("--metadata",
             help='JSON dict of structured facts (e.g. \'{"changed_files": [...], '
                  '"tests_run": 12}\'). Stored on the closing run.'),
        _arg("--force", action="store_true",
             help="Override the live-claim guard: complete a running, claimed task "
                  "even without owning its run (closes the worker's run). Also waives the "
                  "evidence gate, and the waiver is recorded on the card."),
        _arg("--evidence",
             help='The evidence this completion rests on, as JSON: '
                  '\'{"class": "run", "run": {"store": "yoyoflow", "id": 123}}\' | '
                  '\'{"class": "probe", "probe": {"at": "...", "result": "ok", '
                  '"observations": [...]}}\' | '
                  '\'{"class": "none", "why": "skill edit, no run behind it"}\'. Required by '
                  'the completion gate: class "run"/"probe" is resolved against the run store '
                  'and must be green; class "none" states why the work has no run.'),
        _arg("--defer-child", action="append", default=None, dest="defer_child",
             metavar="CHILD_ID",
             help="Declare a child that does NOT carry this card's remaining DoD, so the "
                  "completion does not lift it to this card's priority (repeatable; each "
                  "needs --defer-reason). Recorded on the child and on the completion."),
        _arg("--defer-reason",
             help="Why the cards named by --defer-child are deliberately deferred (required "
                  "with --defer-child; a low priority is never read as intent)."),
    ], help="Mark one or more tasks done"),
    _cmd("edit", [
        _TASK_ID,
        _arg("--title", help="Replace the task title"),
        _arg("--body", help="Replace the task body"),
        _arg("--priority", type=int, help="Replace the task priority"),
        _arg("--result", help="Backfilled task result text for a done task"),
        _arg("--clear-failure", action="store_true",
             help="Retire a stale last_failure_error / consecutive_failures streak "
                  "(recovery door for a card the respawn guard parks on an old failure)"),
        *_STEP_HANDOFF,
        *_BUDGET_FIELDS,
    ], help="Edit task fields or recovery fields on an already-completed task"),
    _cmd("defcon", children=("defcon_action", [
        _cmd("designate", [
            _TASK_ID,
            _reason("Why this card holds a reserved-tranche priority (the audit answer)"),
            _arg("--authority", help="Who is designating (default: the acting profile)"),
            _json_flag(),
        ], help="Designate a card: the ledger row first, then priority 990000"),
        _cmd("revoke", [
            _TASK_ID,
            _reason("Why the designation is being lifted (recorded on the event)"),
            _json_flag(),
        ], help="Revoke a designation; the card returns to its ordinary priority"),
    ]), help="The designation door - the ONLY way into the reserved priority tranche"),

    _cmd("retarget", [
        _TASK_ID,
        _arg("--project",
             help="Project id or slug to re-anchor the card to (its primary repo + a "
                  "deterministic worktree branch); 'none' clears the link. Must exist - "
                  "an unknown project is refused. See `hermes project list`."),
        _arg("--workspace",
             help="scratch | worktree | worktree:<path> | dir:<path> - re-resolve the "
                  "workspace kind/path. Omit to keep the current kind (a project anchors "
                  "a worktree under its repo)."),
        _arg("--branch", help="Branch name for a worktree binding"),
        _arg("--reason", required=True,
             help="Why the card is being re-pointed (recorded on the retargeted event)"),
        _arg("--author", help="Author recorded on the change (default: $HERMES_PROFILE or 'user')"),
        _arg("--force", action="store_true",
             help="Retarget even while a worker holds a live claim on the card"),
        _json_flag(),
    ], help="Re-point a card's project + workspace (the recovery door for a mis-born "
            "card) — usable on a blocked/ready card; refused on done/archived"),
    _cmd("block", [
        _TASK_ID,
        _arg("reason", nargs="*", help="Reason (also appended as a comment)"),
        _bulk_ids("block"),
        _approval_flag("a multi-card block (--ids with more than one id)"),
        _arg("--kind", choices=sorted(kb.VALID_BLOCK_KINDS),
             help="Typed block reason. 'dependency' waits in todo (auto-promoted when "
                  "parents finish, no human); 'needs_input'/'capability' go to "
                  "blocked for a human; 'transient' marks a maybe-flaky failure. "
                  "Repeated same-kind re-blocks after unblock route the task to "
                  "triage to break unblock loops. Omit for a generic block."),
        _arg("--waits-on", dest="waits_on",
             help="Comma-separated card ids this card WAITS ON: each becomes a parent edge, so "
                  "the board resumes the card when they finish. A kind='dependency' block "
                  "whose reason names a card without this is REFUSED (a prose-only wait is "
                  "invisible to the dependency machinery)."),
        _arg("--due", metavar="WHEN",
             help="Auto-release time for the hold: ISO-8601 local (2026-09-16T01:40), "
                  "an offset from now (+90m, +2h, +1d), or epoch seconds. The "
                  "dispatcher tick unblocks the task once it passes (deferred to "
                  "the close of a reserved execution band) -- no cron entry needed. "
                  "Applies to the 'blocked' outcome only (not a dependency wait or "
                  "a triage). Reason words must come BEFORE the flags."),
        _arg("--window-policy", choices=["defer", "ambient"], metavar="POLICY",
             help="What to do when the auto-release time lands inside an execution "
                  "band: 'defer' (default) holds the wake until the band closes; "
                  "'ambient' wakes anyway, for a wake that is a lightweight check "
                  "and has to tick around the clock."),
    ], help="Mark one or more tasks blocked"),
    _cmd("schedule", [
        _TASK_ID,
        _arg("reason", nargs="*", help="Reason/timing note (also appended as a comment)"),
        _arg("--due", metavar="WHEN",
             help="Due time: ISO-8601 local (2026-09-16T01:40), an offset from now "
                  "(+90m, +2h, +1d), or epoch seconds. The dispatcher tick wakes the "
                  "task once it passes (deferred to the close of a reserved "
                  "execution band) -- no cron entry needed. Reason words must come "
                  "BEFORE the flags."),
        _arg("--window-policy", choices=["defer", "ambient"], metavar="POLICY",
             help="What to do when the due time lands inside an execution band: "
                  "'defer' (default) holds the wake until the band closes; 'ambient' "
                  "wakes anyway, for a wake that is a lightweight check and has to "
                  "tick around the clock."),
        _arg("--clear-due", action="store_true",
             help="Drop the task's due time (it stays parked until woken by hand)."),
        _bulk_ids("schedule"),
        _approval_flag("a multi-card schedule (--ids with more than one id)"),
    ], help="Park one or more tasks in Scheduled (waiting on time, not human input)"),
    _cmd("unblock", [
        _reason("Optional reason/note — recorded as a comment before unblocking. Quote multi-word reasons."),
        _TASK_IDS,
    ], help="Return blocked/scheduled tasks to ready, or todo while parents remain open"),
    _cmd("reopen", [
        _TASK_ID,
        _arg("--reason", help="Reason/note recorded on the reopened event (quote multi-word reasons)."),
        _arg("--to", choices=["ready", "todo", "blocked"], metavar="STATUS",
             help="Landing status. Omit to re-gate on parent completion (ready, or todo "
                  "while parents remain open). 'blocked' parks the card (the parked policy "
                  "for a done->live repair that owes no work to a lane) — that needs "
                  "--block-kind."),
        _arg("--block-kind", dest="block_kind",
             help="Typed block reason when --to blocked (default: external)."),
        _arg("--dry-run", action="store_true",
             help="Validate the reopen without mutating state"),
        _json_flag(),
    ], help="THE done->live door: restore a done/archived task to a live status "
            "(repair/recovery; single id — a multi-id promote is the bulk class)"),
    _cmd("request-review", [
        _TASK_ID,
        _arg("--summary", help="What was implemented and how it was verified — shown to the reviewer."),
        _arg("--reviewer", help="Optional reviewer profile; reassigns the task before review dispatch."),
        _arg("--metadata", help="JSON object with structured reviewer handoff facts."),
        _arg("--force", action="store_true",
             help="Override the live-claim guard: move a running, claimed "
                  "task to review even without owning its run (clears the worker's claim)."),
    ], help="Move a task to 'review' (implementation done, awaiting review) — NOT a block"),
    _cmd("request-changes", [_TASK_ID, _arg("reason", nargs="+", help="Concrete changes required before re-review")],
         help="Reviewer verdict: return the active review run to its implementer"),
    _cmd("reopen-review", [
        _TASK_IDS,
        _reason("Optional reason/note — recorded as a comment before reopening. Quote multi-word reasons."),
    ], help="Send one or more review tasks back for changes (review -> ready/todo)"),
    _cmd("promote", [
        _TASK_ID,
        _arg("reason", nargs="*", help="Audit-trail reason (recorded on the task_events row)"),
        _bulk_ids("promote"),
        _approval_flag("a multi-card promote (--ids with more than one id)"),
        _arg("--dry-run", action="store_true", help="Validate the promotion without mutating state"),
        _arg("--json", dest="json", action="store_true", help="Emit machine-readable JSON result"),
    ], help="Manually move one or more todo/blocked tasks to ready (recovery path)"),
    _cmd("archive", [
        _arg("task_ids", nargs="*", help="Task ids to archive (default mode)"),
        _arg("--rm", dest="purge_ids", nargs="+",
             help="Permanently delete already-archived task ids from the board"),
        _approval_flag("a bulk archive or a purge (--rm)"),
    ], help="Archive one or more tasks"),
    _cmd("tail", [_TASK_ID, _arg("--interval", type=float, default=1.0)], help="Follow a task's event stream"),
    _cmd("dispatch", [
        _arg("--dry-run", action="store_true", help="Don't actually spawn processes; just print what would happen"),
        _arg("--max", type=int, help="Cap number of spawns this pass"),
        _arg("--failure-limit", type=int, default=kbd.DEFAULT_FAILURE_LIMIT,
             help=f"Auto-block a task after this many consecutive non-success attempts "
                  f"(spawn_failed, timed_out, or crashed; default: {kbd.DEFAULT_FAILURE_LIMIT})"),
        _json_flag(),
    ], help="One dispatcher pass: reclaim stale, promote ready, spawn workers"),
    _cmd("daemon", [
        _arg("--interval", type=float, default=60.0, help="Seconds between dispatch ticks (default: 60)"),
        _arg("--max", type=int, help="Cap number of spawns per tick"),
        _arg("--failure-limit", type=int, default=kbd.DEFAULT_FAILURE_LIMIT),
        _arg("--pidfile", help="Write the daemon's PID to this file on start"),
        _arg("--verbose", "-v", action="store_true", help="Log each tick's outcome to stdout"),
        # Escape hatch for hosts that truly cannot run the gateway; hidden from
        # --help so nobody casually keeps the double-dispatcher pattern alive.
        _arg("--force", action="store_true", help=argparse.SUPPRESS),
    ], help="DEPRECATED — dispatcher now runs in the gateway. Use `hermes gateway start`."),
    _cmd("watch", [
        _arg("--assignee", help="Only show events for tasks assigned to this profile"),
        _arg("--tenant", help="Only show events from tasks in this tenant"),
        _arg("--kinds",
             help="Comma-separated event kinds to include (e.g. 'completed,blocked,gave_up,crashed,timed_out')"),
        _arg("--interval", type=float, default=0.5, help="Poll interval in seconds (default: 0.5)"),
    ], help="Live-stream task_events to the terminal (Ctrl+C to exit)"),
    _cmd("stats", [_json_flag()], help="Per-status + per-assignee counts + oldest-ready age"),
    _cmd("notify-subscribe", [
        _TASK_ID,
        *_NOTIFY_TARGET,
        _arg("--user-id"),
        _arg("--user-id-alt"),
        _arg("--chat-type", choices=("dm", "group", "channel", "thread"),
             help="Originating source chat_type, recorded so the active-wake delivery "
                  "modes resolve the operator's real session. Omit to leave an "
                  "existing sub unchanged (new subs default to 'dm')."),
        _arg("--parent-chat-id",
             help="Parent channel ID for a thread or forum post, used for multiplex profile routing."),
        _arg("--guild-id",
             help="Discord guild ID, used for multiplex profile routing."),
        _arg("--notifier-profile",
             help="Profile gateway that owns/delivers this subscription (default: active profile)"),
        # choices: single source of truth shared with the DB/watcher enum.
        _arg("--delivery-mode", choices=kbn._NOTIFY_DELIVERY_MODES,
             help="How the kanban-notifier reacts to terminal events for this "
                  "subscription: 'notify' (passive message only; default), "
                  "'notify+wake' (message AND wake the destination gateway agent so "
                  "it reads the full board context and replies in its own voice), or "
                  "'wake' (wake the agent only, no passive message). Omit to leave an "
                  "existing subscription's mode unchanged (new subs default to 'notify')."),
    ], help="Subscribe a gateway source to a task's terminal events (used by /kanban subscribe in the gateway adapter)"),
    _cmd("notify-list", [_arg("task_id", nargs="?"), _json_flag()],
         help="List notification subscriptions (optionally for a single task)"),
    _cmd("notify-unsubscribe", [_TASK_ID, *_NOTIFY_TARGET], help="Remove a gateway subscription from a task"),
    _cmd("log", [_TASK_ID, _arg("--tail", type=int, help="Only print the last N bytes")],
         help="Print the worker log for a task (from <kanban-root>/kanban/logs/)"),
    _cmd("runs", [_TASK_ID, _json_flag(), *_run_state_args("filter runs by task_runs column")],
         help="Show attempt history for a task (one row per run: profile, outcome, elapsed, summary)"),
    _cmd("heartbeat", [
        _TASK_ID,
        _arg("--note", help="Optional short note attached to the heartbeat event"),
    ], help="Emit a heartbeat event for a running task (worker liveness signal)"),
    _cmd("assignees", [_json_flag()],
         help="List known profiles + per-profile task counts (union of ~/.hermes/profiles/ and current assignees on the board)"),
    _cmd("context", [_TASK_ID],
         help="Print the full context a worker sees for a task (title + body + parent results + comments)."),
    _cmd("specify", _triage_sweep_args("specify", "Specify", "specifier"),
         help="Flesh out a triage-column task into a concrete spec (title + "
              "body) and promote it to todo. Uses the auxiliary LLM "
              "configured under auxiliary.triage_specifier."),
    _cmd("decompose", _triage_sweep_args("decompose", "Decompose", "decomposer"),
         help="Decompose a triage-column task into a graph of child tasks "
              "routed to specialist profiles by description. Falls back "
              "to specify-style single-task promotion when the task "
              "doesn't benefit from fan-out. Uses auxiliary.kanban_decomposer."),
    _cmd("gc", [
        _arg("--event-retention-days", type=_nonnegative_int, default=30,
             help="Delete task_events older than N days for terminal tasks (default: 30; 0 disables)"),
        _arg("--log-retention-days", type=_nonnegative_int, default=30,
             help="Delete worker log files older than N days (default: 30; 0 disables)"),
        _approval_flag("a gc sweep (events, logs, archived workspaces)"),
    ], help="Garbage-collect archived-task workspaces, old events, and old logs"),
    _cmd("bulk-approvals", children=("bulk_approvals_action", [
        _cmd("ask", [
            _arg("--board", help="Board the action will run on (default: the current board)"),
            _arg("--verb", required=True,
                 help="The gated verb: gc, repair, swarm, specify, decompose, block, schedule, "
                      "promote, archive, or boards-<rm|delete|import>"),
            _arg("--scope", required=True,
                 help="The canonical scope string the gate renders for that action (printed on the "
                      "refusal). Ask for the EXACT action you mean: the digest binds it."),
            _arg("--approach", required=True,
                 help="The approach text the ask records; any approval must agree with it, "
                      "word for word."),
            _arg("--apr", required=True,
                 help="APR row ref in the approvals store; it must resolve and be decided 'approved'."),
            _arg("--actor", help=f"Actor filing the ask (must be the authorized ask profile {kbg.AUTHORIZED_ASK_PROFILE!r})"),
            _json_flag(),
        ], help="File the authorized ask for one exact bulk action; prints the digest"),
        _cmd("approve", [
            _arg("digest", help="The digest printed by `bulk-approvals ask`"),
            _arg("--approach", required=True, help="The ask's approach text, word for word"),
            _arg("--reason", help="Why this bot agrees the approach is safe"),
            _arg("--actor", help="Actor (default: $HERMES_PROFILE)"),
            _json_flag(),
        ], help=f"Record one bot approval against a digest; required approvals: {kbg.REQUIRED_APPROVALS}"),
        _cmd("list", [_json_flag()], help="List the approval ledger (asks and approvals)"),
        _cmd("show", [_arg("digest"), _json_flag()],
             help="Show one bundle: board, verb, scope, approach, ask actor, APR, approvers, quorum"),
    ]), help="Authorized asks and bot approvals that gate destructive bulk board actions"),
    _cmd("repair", [_json_flag(help="Emit the repair report as JSON"),
                    _approval_flag("a store quarantine + REINDEX")],
         help="Check kanban.db integrity and auto-repair index-only corruption",
         description=(
             "Runs PRAGMA integrity_check on the board's DB and reports the result. When the "
             "failure consists only of index-scoped errors ('wrong # of entries in index <name>' / "
             "'row N missing from index <name>'), the corrupt file is quarantined to a "
             ".corrupt.<hash>.bak sibling first and the damaged indexes are rebuilt with REINDEX — "
             "the same narrow auto-repair the connect-time guard applies. Any other corruption "
             "class is reported and left untouched (fail-closed). Exits 0 when the DB is healthy "
             "or was repaired, non-zero when it is still corrupt."
         )),
    _cmd("gates", [
        _arg("action", nargs="?", default="report", choices=("report", "reconcile"),
             help="report: measure the gate invariant, change nothing (default). "
                  "reconcile: lift every gate to the cards it still holds, then measure."),
        _json_flag(help="Emit the record as JSON (the counts, the lifts, the remaining edges)"),
    ],
         help="The gate invariant: a card must never rank below a card it gates",
         description=(
             "Holds one relation over the board's graph: for every edge parent -> child, the "
             "parent's priority is at least the child's while the parent is open, transitively "
             "through open nodes. The kernel applies it at every write seam (a filing that names "
             "parents, a new link, a re-rank, a designation release); this verb is the "
             "DETERMINISTIC PASS that normalises what those seams cannot reach - a value written "
             "by raw SQL, a restored backup - and states its counts. 'report' is read-only and "
             "exit 0/1 says whether the invariant holds; 'reconcile' lifts, then re-measures, so "
             "'violations_after' is a query result rather than a claim. A lift that has to enter "
             "the reserved tranche goes through the designation door first (authority "
             "'gate-lift'), and every lift is recorded on the card as a 'reprioritized' event "
             "carrying the edge that caused it, the value before and after, and the cause."
         )),
    _cmd("health", [_json_flag(help="Emit the health read as JSON")],
         help="Report whether the ready queue can move, and name what holds it back",
         description=(
             "The board-health read that tells a STARVED board from an idle one: ready_total "
             "(every ready row), spawnable (the rows the dispatcher could claim this tick), "
             "suppressed_by_reason (of those, how many are held back and why — the respawn-guard "
             "vocabulary), unavailable_by_reason (rows the queue never offers: unassigned, not a "
             "profile, claimed, per-profile cap) and state=starved|dispatchable|idle. A board "
             "whose every spawnable row is suppressed reports state=starved, whatever the row "
             "count; an empty board reports state=idle. Read-only, and the same tuple the "
             "dashboard renders and the dispatcher escalates as a card."
         )),
]


def build_parser(parent_subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Attach the ``kanban`` subcommand tree; returns the ``kanban`` parser."""
    kanban_parser = parent_subparsers.add_parser(
        "kanban",
        help="Multi-profile collaboration board (tasks, links, comments)",
        description="Durable SQLite-backed task board shared across Hermes profiles. "
                    "Tasks are claimed atomically, can depend on other tasks, and "
                    "are executed by a named profile in an isolated workspace. "
                    "See https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban.",
    )
    # --board scopes every subcommand to one board's DB; when omitted the
    # resolution is HERMES_KANBAN_BOARD, then the persisted current-board
    # file, then "default" (kanban_db.get_current_board()).
    kanban_parser.add_argument("--board", default=None, metavar="<slug>",
                               help="Board slug to operate on. Defaults to the current board (set "
                                    "via `hermes kanban boards switch <slug>` or the "
                                    "HERMES_KANBAN_BOARD env var). Use `hermes kanban boards "
                                    "list` to see all boards.")
    _add_commands(kanban_parser.add_subparsers(dest="kanban_action"), _SPECS)
    kanban_parser.set_defaults(_kanban_parser=kanban_parser)
    return kanban_parser
