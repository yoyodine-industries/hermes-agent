"""``hermes kanban …`` — dispatch (``kanban_command``), task-verb handlers, ``run_slash`` for ``/kanban``.
DB work lives in ``kanban_db``; siblings: ``kanban_parser`` (argparse, re-exported ``build_parser``),
``kanban_output`` (text/--json), ``kanban_boards`` (``boards …``), ``kanban_ops`` (dispatch/daemon/
tail/watch/gc/repair).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import sys
import time
from pathlib import Path
from typing import Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_db_notify as kbn
from hermes_cli import kanban_swarm as ks
from hermes_cli import kanban_workflow
from hermes_cli import tree_identity as kbt
from hermes_cli import kanban_bulk_guard as kbg
from hermes_cli.kanban_output import (
    _ATTACHMENT_FIELDS, _RUNS_RUN_FIELDS, _SHOW_RUN_FIELDS, _bulk_apply, _err,
    _fmt_counts, _fmt_task_line, _fmt_ts, _json_out, _obj_dict, _print_json,
    _task_to_dict,
)
from hermes_cli.kanban_boards import _dispatch_boards
from hermes_cli.kanban_ops import (
    _cmd_daemon, _kanban_config, _cmd_dispatch, _cmd_gc, _cmd_repair, _cmd_tail, _cmd_watch,
)
from hermes_cli.kanban_parser import build_parser


# --- Flag parsing helpers ---

def _none_profile(value: str) -> Optional[str]:
    """``none`` / ``-`` / ``null`` mean "unassign"."""
    return None if value.lower() in {"none", "-", "null"} else value


def _parse_metadata_flag(raw: Optional[str]) -> tuple[Optional[dict], int]:
    """Parse ``--metadata`` JSON; returns ``(dict|None, rc)`` with rc=2 on error."""
    if not raw:
        return None, 0
    try:
        metadata = json.loads(raw)
        if not isinstance(metadata, dict):
            raise ValueError("must be a JSON object")
    except (ValueError, json.JSONDecodeError) as exc:
        return None, _err(f"kanban: --metadata: {exc}", 2)
    return metadata, 0


def _run_state_kwargs(args: argparse.Namespace, cmd: str) -> tuple[Optional[dict[str, str]], int]:
    """``--state-type``/``--state-name`` must be given together: ``(kwargs, 0)`` or ``(None, 2)``."""
    st = getattr(args, "state_type", None)
    sn = getattr(args, "state_name", None)
    if (st is None) != (sn is None):
        return None, _err(f"kanban {cmd}: pass both --state-type and --state-name, or omit both", 2)
    return ({} if st is None else {"state_type": st, "state_name": sn}), 0


def _parse_workspace_flag(value: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """``--workspace`` -> ``(kind, path|None)``: ``scratch``, ``worktree``, ``worktree:<p>``, ``dir:<p>``.
    Omitted -> ``(None, None)`` so ``create_task`` can tell "default" from an explicit scratch."""
    if not value:
        return (None, None)
    v = value.strip()
    if v in {"scratch", "worktree"}:
        return (v, None)
    for prefix, kind in (("dir:", "dir"), ("worktree:", "worktree")):
        if not v.startswith(prefix):
            continue
        path = v[len(prefix):].strip()
        if not path:
            raise argparse.ArgumentTypeError(f"--workspace {prefix} requires a path after the colon")
        return (kind, os.path.expanduser(path))
    raise argparse.ArgumentTypeError(f"unknown --workspace value {value!r}: use scratch, worktree, "
                                     "worktree:<path>, or dir:<path>")


def _parse_branch_flag(value: Optional[str]) -> Optional[str]:
    """Normalize an optional branch name from ``kanban create --branch``."""
    if value is None:
        return None
    branch = value.strip()
    if not branch:
        raise argparse.ArgumentTypeError("--branch requires a non-empty name")
    if branch.startswith("-"):
        raise argparse.ArgumentTypeError("--branch must not start with '-'")
    if any(ch.isspace() for ch in branch):
        raise argparse.ArgumentTypeError("--branch must not contain whitespace")
    return branch


def _check_dispatcher_presence(hermes_home: Optional[Path] = None) -> tuple[bool, str]:
    """``(running, message)`` for the "will anything dispatch this?" warning: True when a gateway is
    alive for this HERMES_HOME with ``kanban.dispatch_in_gateway`` on, else False + human guidance.
    Fails OPEN (probe/config errors -> ``(True, "")``) — a missed warning beats crying wolf.
    ``hermes_home`` scopes the probe to a profile dir (dashboard backend); CLI callers pass None.

    The dashboard plugin API passes it because the dashboard backend process can be running under a
    different HERMES_HOME than the profile the request targets, which otherwise produced a "no gateway is
    running" warning against a perfectly healthy profile gateway (#71211). CLI callers leave it ``None`` and
    keep the existing process-level behavior.
    """
    try:
        from gateway.status import resolve_gateway_liveness  # type: ignore

        # Same ladder as the dashboard status endpoints so PID-file-less / cross-container gateways
        # aren't misreported; use_cache=False because this one-shot probe must see the state now.
        liveness = resolve_gateway_liveness(profile_dir=hermes_home, use_cache=False)
    except Exception:
        return (True, "")  # can't probe — silent
    if liveness.probe_error:  # resolver swallows per-rung failures; "can't tell" != "no gateway"
        return (True, "")
    pid = liveness.pid
    # Even if the gateway is up, dispatch_in_gateway may be off (can't tell -> assume default).
    if pid and bool(_kanban_config().get("dispatch_in_gateway", True)):
        return (True, f"gateway pid={pid}, dispatch enabled")
    if pid:
        return (False, "Gateway is running but kanban.dispatch_in_gateway=false in "
                "config.yaml — the task will sit in 'ready' until you flip it "
                "back on and restart the gateway, OR run the legacy "
                "standalone daemon (`hermes kanban daemon --force`).")
    return (False, "No gateway is running — the task will sit in 'ready' until you "
            "start it. Run:\n    hermes gateway start\n"
            "The gateway hosts an embedded dispatcher (tick interval 60s by "
            "default); your task will be picked up on the next tick after "
            "the gateway comes up.")


# --- Command dispatch ---

def kanban_command(args: argparse.Namespace) -> int:
    """Entry point from ``hermes kanban …``; returns a shell-style exit code."""
    action = getattr(args, "kanban_action", None)
    if not action:
        parser = getattr(args, "_kanban_parser", None)
        if parser is not None:
            parser.print_help()
        else:
            print("usage: hermes kanban <action> [options]\n"
                  "Run 'hermes kanban --help' for the full list of actions.", file=sys.stderr)
        return 0

    # Fast-fail for UX only; the durable trust boundary is in kanban_db, since children can
    # import DB mutators directly.
    if _is_delegated_child_cli_mutation(args):
        return _err(_DELEGATED_CHILD_CLI_REFUSAL)

    # A verb that MOVES the estate must come from the tree that dispatches it: a second
    # checkout earlier on PATH runs a code line with none of the current guards (measured
    # 2026-09-27, card t_70e91ef2 — unguarded promote minted five cards), so it refuses
    # loudly here instead of landing unguarded. Reads are never checked, the dispatcher's own
    # in-process writes never pass through this seam, and there is no env var that waives the
    # check (card t_73155b4a) — a foreign tree must not write this board.
    skew_refusal = kbt.dispatching_tree_refusal(action, getattr(args, "boards_action", None))
    if skew_refusal:
        return _err(skew_refusal)

    # One seam for the operator's 2026-09-27 bulk-action rule (card t_bf9605f8): a destructive BULK
    # board action is REFUSED unless an authorized ask (the profile in
    # `kanban_bulk_guard.AUTHORIZED_ASK_PROFILE`; REQUIRED_APPROVALS is 0, so no second party is
    # needed) and a VERIFIED
    # snapshot of the target store all exist — and the approval binds this EXACT action (board +
    # verb + canonical scope), so it cannot be transplanted to a different parameter set. Gated by
    # classification, so a bulk verb is covered by construction (`kanban_bulk_guard.BULK_SURFACES`);
    # a non-bulk single-card write returns None here and is untouched. The ONE waiver is a declared
    # estate board (board.json `"dispatch": false`), torn down single-actor via
    # `hermes kanban boards rm --estate <slug>` with the ask/APR clause skipped and the destination
    # claim, verified snapshot and ledger/audit row kept (ruling t_fcf7a321, Decision 2).
    try:
        kbg.gate_cli(args)
    except kbg.BulkActionRefused as exc:
        return _err(str(exc), 1)

    # `boards …` manages board metadata and the current-board pointer itself, so it must ignore
    # the `--board` routing override (else `--board beta boards show` reports beta).
    if action == "boards":
        return _dispatch_boards(args)

    # `--board <slug>` scopes an explicit board NAME for the duration of this call: a name the
    # caller typed, which therefore outranks the ambient HERMES_KANBAN_DB path pin (the pin only
    # chooses "the active board" when nothing is named). Before 2026-09-27 the pin outranked the
    # scope, so `--board ops` inside a pinned worker still read the worker's own store
    # ("no such task", card t_d867ddbd).
    board_override = getattr(args, "board", None)
    board_scope = contextlib.nullcontext()
    if board_override:
        try:
            normed = kb._normalize_board_slug(board_override)
        except ValueError as exc:
            return _err(f"kanban: {exc}", 2)
        if not normed:
            return _err("kanban: --board requires a slug", 2)
        # Boards other than 'default' must already exist — typoed slugs would otherwise silently
        # create an empty board.
        if normed != kb.DEFAULT_BOARD and not kb.board_exists(normed):
            return _err(f"kanban: board {normed!r} does not exist. "
                        f"Create it with `hermes kanban boards create {normed}`.")
        board_scope = kb.scoped_current_board(normed)

    with board_scope:
        # `repair` dispatches BEFORE auto-init: on a corrupt DB init_db() itself raises
        # KanbanDbCorruptError, which would turn every repair into "could not initialize database".
        if action == "repair":
            return _cmd_repair(args)
        # init_db is idempotent (one sqlite_master SELECT when tables exist) and prevents
        # "no such table: tasks" on first use from a fresh HERMES_HOME.
        try:
            kb.init_db()
        except Exception as exc:
            return _err(f"kanban: could not initialize database: {exc}")

        handler = _HANDLERS.get(action)
        if not handler:
            return _err(f"kanban: unknown action {action!r}", 2)
        try:
            return int(handler(args) or 0)
        except (ValueError, RuntimeError, PermissionError) as exc:
            return _err(f"kanban: {exc}")


# --- Handlers ---

def _profile_author() -> str:
    """Best-effort author name for an interactive CLI call.

    Order (see :func:`hermes_cli.profiles.resolve_acting_profile_name`):
    ``HERMES_PROFILE_NAME`` -> ``HERMES_PROFILE`` -> the bound session profile
    (``HERMES_SESSION_PROFILE``) -> the profile id derived from the active ``HERMES_HOME``
    -> ``"user"``. A caller-supplied ``--author`` always wins (checked before this call).
    The session step is what a gateway-hosted ``hermes kanban comment`` needs: its
    ``HERMES_HOME`` is the DEFAULT root, so the home-derived name alone said
    ``default`` for every served profile.
    """
    try:
        from hermes_cli.profiles import resolve_acting_profile_name
        return resolve_acting_profile_name("user")
    except Exception:
        return "user"


_DELEGATED_CHILD_DENIED_ACTIONS: frozenset[str] = frozenset({
    "init", "create", "swarm", "assign", "reclaim", "reassign", "link", "unlink",
    "claim", "comment", "attach", "attach-rm", "complete", "edit", "block",
    # A worker must not relabel the fence that is holding it: the contract release is
    # top-level only (the write itself is also refused at the DB layer).
    "set-contract",
    "schedule", "unblock", "promote", "reopen", "archive", "dispatch", "daemon", "repair",
    "heartbeat", "notify-subscribe", "notify-unsubscribe", "specify", "decompose",
    "request-review", "request-changes", "reopen-review",
    # The designation door is a host/operator surface: a card may not designate ITSELF (the
    # write is independently refused at the DB layer too).
    "defcon",
    "gc",
})

_DELEGATED_CHILD_DENIED_BOARD_ACTIONS: frozenset[str] = frozenset({
    "create", "new", "rm", "remove", "delete", "switch", "use", "rename",
    "set-default-workdir", "set-operator-register", "set-priority-policy", "import",
})

#: Printed for BOTH cooperative arms and the provenance arm (ruling t_fcf7a321, Decision 1(b)):
#: a refused worker must learn the ACTUAL requirement and the ONE sanctioned release, never be
#: told to reach for an ``env -u`` strip.
_DELEGATED_CHILD_CLI_REFUSAL: str = (
    "kanban: delegate_task child contexts cannot mutate Kanban tasks via the CLI "
    "(and a shell that merely unsets HERMES_DELEGATED_CHILD_CONTEXT is still refused when it "
    "descends from a LIVE dispatched worker holding a claim on this board). Run the action from a "
    "top-level session; the one sanctioned non-top-level release is the estate path — a board "
    "whose own board.json declares \"dispatch\": false is torn down single-actor with "
    "`hermes kanban boards rm --estate <slug>`."
)


# The tree-identity fence (hermes_cli.tree_identity) answers a different question about the
# same writes — "is this CLI running the tree that DISPATCHES the board?" — and its own verb set
# is ``tree_identity.ESTATE_MUTATING_ACTIONS``, which must never be NARROWER than this one; that
# is what tests/hermes_cli/test_kanban_tree_identity.py asserts. Kept as an alias (not a copy) so
# this set cannot drift from the delegation fence, but it is NOT the tree fence's verb set: card
# t_73155b4a widened the tree fence with ``set-model``, which this set deliberately does not carry.
_TREE_SKEW_DENIED_ACTIONS: frozenset[str] = _DELEGATED_CHILD_DENIED_ACTIONS


def _fence_target_store(args: argparse.Namespace) -> Path:
    """The board store the invocation actually TARGETS.

    ``--board`` names the board a command addresses; the ambient chain
    (``HERMES_KANBAN_DB`` -> ``HERMES_KANBAN_BOARD`` -> ``<kanban>/current``) resolves only
    the board that is ACTIVE in this process. Judging the fast-fail against the ambient board
    refuses a mutation targeting a board this lineage holds no claim on, and lets the verdict
    flip when ``<kanban>/current`` moves (card t_63a0c2d9).

    The ``boards …`` actions are the exception: they ignore ``--board`` by construction (board
    metadata and the current-board pointer live at the root), so the caller keeps the ambient
    resolution for them.
    """
    named = getattr(args, "board", None)
    if named:
        try:
            return kb.kanban_db_path(board=named)
        except Exception:
            # An unregistered/malformed ``--board`` is the CLI's own validation to refuse a
            # moment later (:func:`kanban_command` validates it before dispatch); the fast-fail
            # must never raise from here. Fall through to the ambient board, which is exactly
            # what the pre-fix helper judged.
            pass
    return kb.kanban_db_path()


def _is_delegated_child_cli_mutation(args: argparse.Namespace) -> bool:
    """Fence a mutating CLI verb from a non-top-level session.

    Two cooperative arms (the ContextVar and the ``HERMES_DELEGATED_CHILD_CONTEXT`` marker,
    both inside :func:`kanban_path_is_fenced`) plus the PROVENANCE arm (ruling t_fcf7a321,
    Decision 1): a process descended from a LIVE dispatched worker is refused even after the
    marker has been stripped. Reads never reach this helper — the caller checks the action
    vocabulary first.

    Both arms are judged against the board the invocation TARGETS (:func:`_fence_target_store`),
    never the host-global ambient board: a mutation aimed at another board is not this lineage's
    to fence, and the verdict must not move when ``<kanban>/current`` does.
    """
    action = getattr(args, "kanban_action", None)
    if action == "boards":
        if (getattr(args, "boards_action", None) or "list") not in _DELEGATED_CHILD_DENIED_BOARD_ACTIONS:
            return False
    elif action not in _DELEGATED_CHILD_DENIED_ACTIONS:
        return False
    from agent.delegation_context import ancestor_owns_live_kanban_claim, kanban_path_is_fenced

    # ``boards …`` ignores ``--board``, so its target is the ambient board (as before).
    target = kb.kanban_db_path() if action == "boards" else _fence_target_store(args)
    if kanban_path_is_fenced(kb.kanban_home()) or kanban_path_is_fenced(target):
        return True
    # Durable arm: the marker is clearable, ancestry is not. Same fence, proven from the TARGET board.
    return ancestor_owns_live_kanban_claim(target)


def _joined_words(words) -> Optional[str]:
    """Free-text positional ``nargs="*"`` words -> stripped string, or None when absent."""
    return " ".join(words).strip() if words else None


def _stripped_or_none(value: Optional[str]) -> Optional[str]:
    """``None`` stays ``None``; otherwise strip, and treat the empty string as ``None``."""
    return None if value is None else (value.strip() or None)


def _ok_or_err(ok, fail: str, done: str) -> int:
    """Single-mutation handlers: print ``done`` (rc 0) or ``fail`` to stderr (rc 1)."""
    if not ok:
        return _err(fail)
    print(done)
    return 0


def _bulk_ids(args: argparse.Namespace) -> list[str]:
    """Positional ``task_id`` plus ``--ids`` extras (bulk verbs)."""
    return [args.task_id] + list(getattr(args, "ids", None) or [])


def _require_ids(args: argparse.Namespace) -> tuple[list[str], int]:
    """``args.task_ids`` -> ``(ids, 0)`` or ``([], 1)`` after printing the standard error."""
    ids = list(args.task_ids or [])
    if not ids:
        return ids, _err("at least one task_id is required")
    return ids, 0


def _parse_duration(val) -> Optional[int]:
    """``30s`` / ``5m`` / ``2h`` / ``1d`` or a raw integer → seconds; None for empty input or an
    explicit "no cap" (``none``/``off``/0 — a stored ``0`` would render as a ``0s`` cap that reaps
    every worker instantly, so it is normalised to "unset" instead); ValueError on malformed input."""
    if val is None or val == "":
        return None
    s = str(val).strip().lower()
    if s in {"none", "off", "unset"}:
        return None
    try:
        seconds = int(s)  # bare integer → seconds
    except ValueError:
        seconds = None
    if seconds is not None:
        if seconds <= 0:
            return None
        return seconds
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if not (s and s[-1] in units):
        raise ValueError(f"malformed duration {val!r} (expected 30s, 5m, 2h, 1d, or a number)")
    try:
        n = float(s[:-1])
    except ValueError as exc:
        raise ValueError(f"malformed duration {val!r}") from exc
    seconds = int(n * units[s[-1]])
    return seconds if seconds > 0 else None


def _cmd_init(args: argparse.Namespace) -> int:
    path = kb.init_db()
    print(f"Kanban DB initialized at {path}")
    print()
    # Profiles on disk == assignees already addressable.
    try:
        profiles = kb.list_profiles_on_disk()
    except Exception:
        profiles = []
    if profiles:
        print(f"Discovered {len(profiles)} profile(s) on disk; any of these can be an --assignee:")
        for name in profiles:
            print(f"  {name}")
    else:
        print("No profiles found under ~/.hermes/profiles/.\n"
              "Create one with `hermes -p <name> setup` before assigning tasks.")
    print(
        "\nNext step: start the gateway so ready tasks actually get picked up.\n"
        "  hermes gateway start\n\n"
        "The gateway hosts an embedded dispatcher that ticks every 60 seconds\n"
        "by default (config: kanban.dispatch_interval_seconds). Without a\n"
        "running gateway, tasks stay in 'ready' forever."
    )
    return 0


def _cmd_heartbeat(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        ok = kbd.heartbeat_worker(conn, args.task_id, note=getattr(args, "note", None),
                                 expected_run_id=_worker_run_id_for(args.task_id))
    return _ok_or_err(ok, f"cannot heartbeat {args.task_id} (not running?)",
                      f"Heartbeat recorded for {args.task_id}")


def _cmd_assignees(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        data = kb.known_assignees(conn)
    if _json_out(args, data):
        return 0
    if not data:
        print("(no assignees — create a profile with `hermes -p <name> setup`)")
        return 0
    print(f"{'NAME':20s}  {'ON DISK':8s}  COUNTS")
    for entry in data:
        on_disk = "yes" if entry["on_disk"] else "no"
        print(f"{entry['name']:20s}  {on_disk:8s}  {_fmt_counts(entry['counts'] or {}, '(idle)')}")
    return 0


def _cmd_create(args: argparse.Namespace) -> int:
    from agent.delegation_context import is_dispatcher_owned_worker_context
    from hermes_cli.kanban_pr_acceptance import needs_repository_checks

    body = args.body
    body_file = getattr(args, "body_file", None)
    if body is not None and body_file is not None:
        return _err("kanban: --body and --body-file are mutually exclusive", 2)
    if body_file is not None:
        try:
            body = sys.stdin.read() if body_file == "-" else Path(body_file).read_text(encoding="utf-8-sig")
        except OSError as exc:
            return _err(f"kanban: --body-file: {exc}", 2)

    try:
        ws_kind, ws_path = _parse_workspace_flag(args.workspace)
        branch_name = _parse_branch_flag(getattr(args, "branch", None))
    except argparse.ArgumentTypeError as exc:
        return _err(f"kanban: {exc}", 2)
    if branch_name and ws_kind != "worktree":
        return _err("kanban: --branch is only valid with --workspace worktree", 2)
    try:
        max_runtime = _parse_duration(getattr(args, "max_runtime", None))
    except ValueError as exc:
        return _err(f"kanban: --max-runtime: {exc}", 2)
    max_retries = getattr(args, "max_retries", None)
    if max_retries is not None and max_retries < 1:
        return _err(f"kanban: --max-retries must be >= 1 (got {max_retries}); "
                    "use 1 to trip on the first failure.", 2)
    # A card fired from another card lands on the SAME board as the card that fired
    # it (operator standing order): the source card's board outranks the ambient
    # board, which is only a fallback for a filing with no source card (a loop, a
    # cron row, a sweep). Read the source card's board from the board stores - the
    # ambient pin/pointer is a property of this process and can be stale or wrong.
    fired_board = kb.board_for_fired_card(
        getattr(args, "board", None),
        source_task_id=(os.environ.get("HERMES_KANBAN_TASK")
                        if is_dispatcher_owned_worker_context() else None))
    with kbc.connect_closing(board=fired_board) as conn:
        task_id = kb.create_task(
            conn, title=args.title, body=body, assignee=args.assignee,
            # The card's OWN board, not the ambient one: the priority policy, the
            # board floor and every board-scoped read below must see the board the
            # card actually lands on (a policy run against the ambient board computed
            # a value the destination board's floor then refused).
            board=fired_board,
            created_by=args.created_by or _profile_author(),
            workspace_kind=ws_kind, workspace_path=ws_path, branch_name=branch_name,
            project_id=getattr(args, "project", None), tenant=args.tenant, priority=args.priority,
            parents=tuple(args.parent or ()), triage=bool(getattr(args, "triage", False)),
            idempotency_key=getattr(args, "idempotency_key", None),
            max_runtime_seconds=max_runtime, skills=getattr(args, "skills", None) or None,
            max_retries=max_retries, model_override=getattr(args, "model_override", None),
            provider_override=getattr(args, "provider_override", None),
            goal_mode=bool(getattr(args, "goal_mode", False)),
            goal_max_turns=getattr(args, "goal_max_turns", None),
            completion_contract=getattr(args, "completion_contract", None),
            initial_status=getattr(args, "initial_status", "running"),
            serves=getattr(args, "serves", None),
            creator_task_id=(os.environ.get("HERMES_KANBAN_TASK")
                             if is_dispatcher_owned_worker_context() else None),
        )
        task = kb.get_task(conn, task_id)
    if getattr(args, "json", False):
        _print_json(_task_to_dict(task))
    else:
        print(f"Created {task_id}  ({task.status}, assignee={task.assignee or '-'})")
        _print_ask_line(task)
        if needs_repository_checks(task.completion_contract):
            # One line, on the create output (D4-iii): a checks-backed contract cannot be
            # satisfied by a repository that requires no checks, and the author is still the
            # cheapest person to fix it (a completion attempt parks such a card).
            print(f"  note: OWNER/REPO and PR-URL contracts require repository-required checks; "
                  f"a repo with none configured can never complete — use local-only for non-CI "
                  f"work (`hermes kanban set-contract {task_id} local-only --reason ...`, top-level only)")
        # Warn only for ready+assigned tasks that would sit without a dispatcher (triage/todo idle
        # by design, unassigned can't dispatch); skipped under --json so stdout stays parseable.
        if task.status == "ready" and task.assignee:
            running, message = _check_dispatcher_presence()
            if not running and message:
                print(f"\n⚠  {message}", file=sys.stderr)
    return 0


def _print_ask_line(task) -> None:
    """One line on create naming the operator ask the card was stamped with, if any.

    Printed so the stamper is visible at the moment of filing: a caller that expected a
    stamp and did not get one (an unresolvable reference) sees it here rather than
    discovering it weeks later in a roll-up that is missing a card.
    """
    from hermes_cli import kanban_register as kr

    pair = kr.parse_stamp(task.body)
    if pair:
        print(f"  serves operator ask {pair[0]}/{pair[1]}")
        return
    unresolved = _unresolved_ask_on_create(task.id)
    if unresolved:
        print(f"  ⚠ no operator-ask stamp: {unresolved!r} names no card on any board",
              file=sys.stderr)


def _unresolved_ask_on_create(task_id: str) -> Optional[str]:
    """The ``operator_ask_unresolved`` recorded on the card's ``created`` event, if any."""
    with kbc.connect_closing() as conn:
        for event in kb.list_events(conn, task_id):
            payload = getattr(event, "payload", None)
            if isinstance(payload, dict) and payload.get("operator_ask_unresolved"):
                return str(payload["operator_ask_unresolved"])
    return None


def _cmd_rollup(args: argparse.Namespace) -> int:
    """Every card in service of the operator register, across every board."""
    from hermes_cli import kanban_register as kr

    board = getattr(args, "board", None) or kb.get_current_board()
    try:
        result = kr.rollup(getattr(args, "register", None), board=board)
    except ValueError as exc:
        return _err(f"kanban rollup: {exc}", 2)
    if getattr(args, "json", False):
        _print_json(kr.rollup_json(result))
    else:
        print(kr.render_rollup(result))
    return 0


def _cmd_swarm(args: argparse.Namespace) -> int:
    try:
        workers = [ks.parse_worker_arg(raw) for raw in (args.worker or [])]
    except ValueError as exc:
        return _err(f"kanban swarm: {exc}", 2)
    if not workers:
        return _err("kanban swarm: at least one --worker is required", 2)
    with kbc.connect_closing() as conn:
        created = ks.create_swarm(
            conn, goal=args.goal, workers=workers, verifier_assignee=args.verifier,
            synthesizer_assignee=args.synthesizer, tenant=args.tenant,
            created_by=args.created_by or _profile_author(), priority=args.priority,
            idempotency_key=getattr(args, "idempotency_key", None),
        )
    if getattr(args, "json", False):
        _print_json(created.as_dict())
    else:
        print(f"Swarm root: {created.root_id}\n"
              "Workers: " + ", ".join(created.worker_ids) + "\n"
              f"Verifier: {created.verifier_id}\n"
              f"Synthesizer: {created.synthesizer_id}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    assignee = args.assignee
    if args.mine and not assignee:
        assignee = _profile_author()
    with kbc.connect_closing() as conn:
        # Cheap mini-dispatch so list reflects dependencies cleared since the last tick.
        # A FENCED caller (delegated child, or a shell descended from a LIVE dispatched worker —
        # ruling t_fcf7a321) lists READ-ONLY: ``recompute_ready`` is housekeeping the board's
        # OWNER performs every tick, and letting it refuse would fence a read verb, which the
        # fence must never do. ``connect`` already hands such a caller a read-only connection.
        try:
            kb.recompute_ready(conn)
        except PermissionError:
            pass
        tasks = kb.list_tasks(
            conn, assignee=assignee, status=args.status, tenant=args.tenant, session_id=args.session,
            include_archived=args.archived, order_by=getattr(args, "sort", None),
            workflow_template_id=args.workflow_template_id, current_step_key=args.current_step_key,
        )
    if _json_out(args, [_task_to_dict(t) for t in tasks]):
        return 0
    # Passive discoverability: only multi-board users see which board this is.
    try:
        all_boards = kb.list_boards(include_archived=False)
    except Exception:
        all_boards = []
    if len(all_boards) > 1:
        other_count = len(all_boards) - 1
        print(f"Board: {kb.get_current_board()} ({other_count} other board{'s' if other_count != 1 else ''} — "
              f"`hermes kanban boards list`)\n")
    if not tasks:
        print("(no matching tasks)")
        return 0
    for t in tasks:
        print(_fmt_task_line(t))
    return 0


def _print_diagnostics(diags, indent: str, *, with_kind: bool) -> None:
    """Shared human rendering for ``show`` and ``diagnostics`` (suggested actions only)."""
    sev_marker = {"warning": "⚠", "error": "!!", "critical": "!!!"}
    for d in diags:
        head = f"{d.kind}: {d.title}" if with_kind else d.title
        print(f"{indent}{sev_marker.get(d.severity, '?')} [{d.severity}] {head}")
        if d.data:
            bits = [f"{k}={','.join(str(x) for x in v)}" if isinstance(v, list) else f"{k}={v}"
                    for k, v in d.data.items()]
            if bits:
                print(f"{indent}   data: {' | '.join(bits)}")
        for a in d.actions:
            if a.suggested:
                print(f"{indent}   → {a.label}")


def _print_section(title: str, lines) -> None:
    """Blank line, ``title``, then each line (``show`` body sections)."""
    print()
    print(title)
    for line in lines:
        print(line)


def _cmd_show(args: argparse.Namespace) -> int:
    rsk, rc = _run_state_kwargs(args, "show")
    if rc:
        return rc
    graph = None
    # Read before the connection closes: the overdue diagnostic uses it to tell a
    # dead dispatcher tick apart from a waker that ran and refused to wake.
    due_waker_last_tick = None
    want_json = getattr(args, "json", False)
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, args.task_id)
        if not task:
            return _err(f"no such task: {args.task_id}")
        comments = kb.list_comments(conn, args.task_id)
        events = kb.list_events(conn, args.task_id)
        parents = kb.parent_ids(conn, args.task_id)
        children = kb.child_ids(conn, args.task_id)
        runs = kb.list_runs(conn, args.task_id, **rsk)
        # Workers hand off via task_runs.summary; tasks.result stays NULL unless set.
        latest_summary = kb.latest_summary(conn, args.task_id)
        if not want_json:
            graph = kb.task_graph_context(conn, task.id)
            due_waker_last_tick = kb.get_meta_int(conn, kb.META_DUE_WAKER_LAST_TICK)

    if want_json:
        _print_json({
            "task": _task_to_dict(task), "latest_summary": latest_summary, "parents": parents, "children": children,
            "comments": [_obj_dict(c, ("author", "body", "created_at")) for c in comments],
            "events": [_obj_dict(e, ("kind", "payload", "created_at", "run_id")) for e in events],
            "runs": [_obj_dict(r, _SHOW_RUN_FIELDS) for r in runs],
        })
        return 0

    def field(label: str, value) -> None:
        print(f"  {label + ':':<11}{value}")

    print(f"Task {task.id}: {task.title}")
    field("status", task.status)
    if task.due_at:
        policy = task.due_window_policy or kb.DEFAULT_DUE_WINDOW_POLICY
        field("due", f"{_fmt_ts(task.due_at)} ({policy})")
    elif task.status == "scheduled":
        # A parked card with no wake time is a wait only a human can end. Say so
        # on the card, so "scheduled forever" is visible rather than inferred.
        field("due", "none — no due time; wakes by hand only")
    field("assignee", task.assignee or "-")
    if task.tenant:
        field("tenant", task.tenant)
    field("workspace", f"{task.workspace_kind}" + (f" @ {task.workspace_path}" if task.workspace_path else ""))
    if task.branch_name:
        field("branch", task.branch_name)
    if task.skills:
        field("skills", ", ".join(task.skills))
    if task.model_override:
        _prov = f" (provider: {task.provider_override})" if task.provider_override else ""
        field("model", f"{task.model_override}{_prov}")
    # Effective retry threshold (task > config > default) explains auto-blocks.
    if task.max_retries is not None:
        print(f"  max-retries: {task.max_retries} (task)")
    else:
        cfg_val = _kanban_config().get("failure_limit")
        if cfg_val is not None and int(cfg_val) != kb.DEFAULT_FAILURE_LIMIT:
            print(f"  max-retries: {int(cfg_val)} (config kanban.failure_limit)")
        else:
            print(f"  max-retries: {kb.DEFAULT_FAILURE_LIMIT} (default)")
    # How much ROOM this card gets. Both were set-but-invisible: `create --max-runtime` wrote the
    # column and no human surface showed it, so it read as "the flag did not stick", and a goal loop
    # could be armed (by hand then, by the dispatcher now) with nothing on the card saying so.
    if task.max_runtime_seconds:
        field("max-runtime", f"{task.max_runtime_seconds}s")
    if getattr(task, "goal_mode", False):
        turns = getattr(task, "goal_max_turns", None)
        field("goal-loop", f"on (max {turns} turns)" if turns else "on (default turn budget)")
    field("created", f"{_fmt_ts(task.created_at)} by {task.created_by or '-'}")

    # Diagnostics up top so CLI users see distress signals before scrolling.
    from hermes_cli import kanban_diagnostics as kd
    diags = kd.compute_task_diagnostics(
        task, events, runs, graph=graph,
        config={"due_waker_last_tick": due_waker_last_tick},
    )
    if diags:
        print(f"\n  Diagnostics ({len(diags)}):")
        _print_diagnostics(diags, "    ", with_kind=False)
    if task.started_at:
        field("started", _fmt_ts(task.started_at))
    if task.completed_at:
        field("completed", _fmt_ts(task.completed_at))
    if parents:
        field("parents", ", ".join(parents))
    if children:
        field("children", ", ".join(children))
    if task.body:
        _print_section("Body:", [task.body])
    if task.result:
        _print_section("Result:", [task.result])
    elif latest_summary:
        _print_section("Latest summary:", [latest_summary])
    if comments:
        _print_section(f"Comments ({len(comments)}):",
                       (f"  [{_fmt_ts(c.created_at)}] {c.author}: {c.body}" for c in comments))
    if events:
        _print_section(f"Events ({len(events)}):", (
            f"  [{_fmt_ts(e.created_at)}]{f' [run {e.run_id}]' if e.run_id else ''} {e.kind}"
            f"{f' {e.payload}' if e.payload else ''}" for e in events[-20:]))
    if runs:
        print()
        print(f"Runs ({len(runs)}):")
        for r in runs:
            # Clamp to 0 so NTP backward-jumps don't print negative seconds.
            elapsed = max(0, r.ended_at - r.started_at) if r.ended_at else None
            el = f"{elapsed}s" if elapsed is not None else "active"
            outcome = r.outcome or r.status or "active"
            print(f"  #{r.id:<3} {outcome:<12} @{r.profile or '-'}  {el}  {_fmt_ts(r.started_at)}")
            if r.summary:
                print(f"        → {r.summary.splitlines()[0][:160]}")
            if r.error:
                print(f"        ! {r.error.splitlines()[0][:160]}")
    return 0


def _cmd_assign(args: argparse.Namespace) -> int:
    profile = _none_profile(args.profile)
    with kbc.connect_closing() as conn:
        ok = kb.assign_task(conn, args.task_id, profile)
    return _ok_or_err(ok, f"no such task: {args.task_id}",
                      f"Assigned {args.task_id} to {profile or '(unassigned)'}")


def _cmd_set_model(args: argparse.Namespace) -> int:
    model = args.model
    if model is not None and model.lower() in {"none", "-", "null", ""}:
        model = None
    provider = getattr(args, "provider", None)
    try:
        with kbc.connect_closing() as conn:
            ok = kb.set_model_override(conn, args.task_id, model, provider=provider)
    except (ValueError, RuntimeError) as exc:
        return _err(f"kanban: {exc}", 2)
    if not ok:
        return _err(f"no such task: {args.task_id}")
    if model:
        label = f"{provider}:{model}" if provider else model
        print(f"Set model override on {args.task_id}: {label} (applies on next dispatch)")
    else:
        print(f"Cleared model override on {args.task_id} (worker uses its profile default)")
    return 0


def _cmd_set_contract(args: argparse.Namespace) -> int:
    """Correct a task's completion contract: the release for a wrong or unsatisfiable one."""
    try:
        with kbc.connect_closing() as conn:
            ok = kb.set_contract(
                conn, args.task_id, args.contract, reason=args.reason,
                actor=getattr(args, "author", None) or _profile_author(),
            )
    except (ValueError, RuntimeError) as exc:
        return _err(f"kanban: {exc}", 2)
    if not ok:
        return _err(f"no such task: {args.task_id}")
    print(f"Set completion contract on {args.task_id}: {args.contract} "
          f"(recorded as contract_changed; a parked card stays parked — unblock it to resume)")
    return 0


def _cmd_reclaim(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        ok = kb.reclaim_task(conn, args.task_id, reason=getattr(args, "reason", None))
    return _ok_or_err(ok, f"cannot reclaim {args.task_id} (not running or unknown id)",
                      f"Reclaimed {args.task_id}")


def _cmd_reassign(args: argparse.Namespace) -> int:
    profile = _none_profile(args.profile)
    reclaim = bool(getattr(args, "reclaim", False))
    with kbc.connect_closing() as conn:
        ok = kb.reassign_task(conn, args.task_id, profile, reclaim_first=reclaim, reason=getattr(args, "reason", None))
    return _ok_or_err(
        ok,
        f"cannot reassign {args.task_id} (unknown id, or still running — pass --reclaim to release first)",
        f"Reassigned {args.task_id} to {profile or '(unassigned)'}" + (" (claim reclaimed)" if reclaim else ""),
    )


def _rows_by_task(conn, table: str, ids: list[str]) -> dict[str, list]:
    """``{task_id: [rows ordered by id]}`` for every id (empty list when none)."""
    by = {i: [] for i in ids}
    placeholders = ",".join(["?"] * len(ids))
    for row in conn.execute(f"SELECT * FROM {table} WHERE task_id IN ({placeholders}) ORDER BY id", tuple(ids)):
        by.setdefault(row["task_id"], []).append(row)
    return by


def _cmd_diagnostics(args: argparse.Namespace) -> int:
    """List active diagnostics on the board via the same rule engine the dashboard uses."""
    from hermes_cli import kanban_diagnostics as kd
    # Honour kanban.default_assignee as the fallback for unassigned ready tasks (#27145),
    # kanban.max_in_progress as the global concurrency cap (#33488), kanban.max_in_progress_per_profile as
    # the per-profile cap (#21582), and kanban.max_spawn as the per-tick spawn limit (#28805). Same
    # semantics as the gateway dispatch path so behavior matches whether the user runs the CLI directly or
    # relies on the gateway-embedded dispatcher.
    from hermes_cli.config import load_config

    diag_config = kd.config_from_runtime_config(load_config())

    with kbc.connect_closing() as conn:
        # Either one-task mode or fleet mode.
        if getattr(args, "task", None):
            task = kb.get_task(conn, args.task)
            if task is None:
                return _err(f"no such task: {args.task}")
            diags_by_task = {args.task: kd.compute_task_diagnostics(
                task, kb.list_events(conn, args.task), kb.list_runs(conn, args.task),
                graph=kb.task_graph_context(conn, args.task), config=diag_config)}
        else:
            # Fleet mode: pull all non-archived tasks + their events/runs.
            rows = list(conn.execute("SELECT * FROM tasks WHERE status != 'archived'").fetchall())
            ids = [r["id"] for r in rows]
            diags_by_task = {}
            if ids:
                ev_by = _rows_by_task(conn, "task_events", ids)
                run_by = _rows_by_task(conn, "task_runs", ids)
                graph_by = kb.task_graph_contexts(conn, ids)
                for r in rows:
                    tid = r["id"]
                    dl = kd.compute_task_diagnostics(r, ev_by.get(tid, []), run_by.get(tid, []),
                                                     graph=graph_by.get(tid), config=diag_config)
                    if dl:
                        diags_by_task[tid] = dl

        sev = getattr(args, "severity", None)
        if sev:
            floor = kd.SEVERITY_ORDER.index(sev)
            diags_by_task = {tid: kept for tid, dl in diags_by_task.items()
                             if (kept := [d for d in dl if kd.SEVERITY_ORDER.index(d.severity) >= floor])}

        # Map task_id → title/status/assignee for the table output.
        meta: dict[str, dict] = {}
        if diags_by_task:
            placeholders = ",".join(["?"] * len(diags_by_task))
            for r in conn.execute(f"SELECT id, title, status, assignee FROM tasks WHERE id IN ({placeholders})",
                                  tuple(diags_by_task.keys())):
                meta[r["id"]] = {k: r[k] for k in ("title", "status", "assignee")}

    # What this home believes it may claim on a shared board (#113620).
    allowlist = kbd.dispatch_profile_allowlist_summary()

    if getattr(args, "json", False):
        # Per-task rows unchanged; the home-scope allowlist rides as a trailing row
        # (task_id null) so existing `payload[0]["diagnostics"]` consumers keep working.
        _print_json([{"task_id": tid, **meta.get(tid, {}), "diagnostics": [d.to_dict() for d in dl]}
                     for tid, dl in diags_by_task.items()]
                    + [{"task_id": None, "dispatch_profiles": allowlist, "diagnostics": []}])
        return 0

    print(f"kanban.dispatch_profiles: {allowlist}")
    if not diags_by_task:
        print("No active diagnostics on this board.")
        return 0

    total = sum(len(dl) for dl in diags_by_task.values())
    print(f"{total} active diagnostic(s) across {len(diags_by_task)} task(s):\n")
    for tid, dl in diags_by_task.items():
        m = meta.get(tid, {})
        print(f"  {tid}  {m.get('status') or '?':8s}  @{m.get('assignee') or '(unassigned)':18s}  "
              f"{m.get('title') or '(untitled)'}")
        _print_diagnostics(dl, "    ", with_kind=True)
        print()
    return 0


def _cmd_link(args: argparse.Namespace) -> int:
    # A worker linking its own running card (dependency-block handoff) proves
    # ownership with its run id; linking a foreign task never needs one.
    expected_child_run_id = (
        _worker_run_id_for(args.child_id)
        if args.child_id == os.environ.get("HERMES_KANBAN_TASK") else None)
    with kbc.connect_closing() as conn:
        gated = kb.link_tasks(conn, args.parent_id, args.child_id,
                              expected_child_run_id=expected_child_run_id)
    print(f"Linked {args.parent_id} -> {args.child_id}")
    if gated:
        print(
            f"Note: {args.child_id} was ready and is now todo — parent "
            f"{args.parent_id} is not done yet. The ready -> running claim "
            f"re-checks parents, so the child only runs after the parent "
            f"completes; use `hermes kanban unlink {args.parent_id} {args.child_id}` "
            f"to run it now."
        )
    return 0


def _cmd_unlink(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        ok = kb.unlink_tasks(conn, args.parent_id, args.child_id)
    return _ok_or_err(ok, f"No such link: {args.parent_id} -> {args.child_id}",
                      f"Unlinked {args.parent_id} -> {args.child_id}")


def _cmd_claim(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        task = kb.claim_task(conn, args.task_id, ttl_seconds=args.ttl)
        if task is None:
            existing = kb.get_task(conn, args.task_id)
            if existing is None:
                return _err(f"no such task: {args.task_id}")
            return _err(f"cannot claim {args.task_id}: status={existing.status} "
                        f"lock={existing.claim_lock or '(none)'}")
        workspace = kbw.resolve_workspace(task)
        kbw.set_workspace_path(conn, task.id, str(workspace))
    print(f"Claimed {task.id}\nWorkspace: {workspace}")
    return 0


def _cmd_comment(args: argparse.Namespace) -> int:
    body = " ".join(args.text).strip()
    if args.max_len is not None:
        if args.max_len < 1:
            return _err("kanban: --max-len must be positive", 2)
        if len(body) > args.max_len:
            suffix = f"\n\n[trimmed to {args.max_len} chars by --max-len]"
            body = body[: max(0, args.max_len - len(suffix))].rstrip() + suffix
    author = args.author or _profile_author()
    with kbc.connect_closing() as conn:
        kb.add_comment(conn, args.task_id, author, body)
    print(f"Comment added to {args.task_id}")
    return 0


def _cmd_attach(args: argparse.Namespace) -> int:
    """Attach a local file via the shared ``store_attachment_bytes`` path (same 25 MB cap and name
    sanitisation as the dashboard upload and agent tool)."""
    import mimetypes
    _worker_run_id_for(args.task_id)

    src = Path(args.path).expanduser()
    if not src.is_file():
        return _err(f"kanban: no such file: {src}")
    data = src.read_bytes()
    name = args.name or src.name
    content_type = args.content_type or mimetypes.guess_type(name)[0]
    uploaded_by = args.author or _profile_author()
    try:
        with kbc.connect_closing() as conn:
            att_id = kb.store_attachment_bytes(conn, args.task_id, name, data, content_type=content_type,
                                               uploaded_by=uploaded_by)
    except kb.AttachmentTooLarge as exc:
        return _err(f"kanban: {exc}")
    print(f"Attached {name} to {args.task_id} (attachment {att_id}, {len(data)} bytes)")
    return 0


def _cmd_attachments(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        if kb.get_task(conn, args.task_id) is None:
            return _err(f"no such task: {args.task_id}")
        atts = kb.list_attachments(conn, args.task_id)
    if _json_out(args, [_obj_dict(a, _ATTACHMENT_FIELDS) for a in atts], ascii=True):
        return 0
    if not atts:
        print(f"No attachments on {args.task_id}")
        return 0
    print(f"Attachments on {args.task_id}:")
    for a in atts:
        ct = a.content_type or "-"
        print(f"  [{a.id}] {a.filename}  ({a.size} bytes, {ct}, by {a.uploaded_by or '-'})")
        print(f"        {a.stored_path}")
    return 0


def _cmd_attach_rm(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        removed = kb.delete_attachment(conn, args.attachment_id)
    if removed is None:
        return _err(f"no such attachment: {args.attachment_id}")
    print(f"Deleted attachment {args.attachment_id} ({removed.filename}) from {removed.task_id}")
    return 0


def _worker_run_id_for(task_id: str) -> Optional[int]:
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    if env_tid and env_tid != task_id:
        raise ValueError(f"worker is scoped to task {env_tid}; refusing to mutate {task_id}")
    raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    if os.environ.get("HERMES_KANBAN_TASK") != task_id or not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _goal_mode_handoff_rejection(task: Optional[kb.Task], evidence: str):
    """Goal judge for every terminal worker handoff (including review).

    Returns ``(verdict, reason_or_None)``: ``"done"`` allows; ``"blocked"`` = judge ruled the goal
    unachievable; ``"continue"``/``"wait"`` reject with the judge's reason. Judge failures allow
    the handoff (logged).

    See #100954.
    ``{"done", None}`` means the judge allows the handoff; anything else is a rejection whose verdict
    disambiguates the guidance the caller gives the worker (``continue`` = not done yet, ``blocked`` =
    judged unachievable — see #100954).
    """
    if task is None or not task.goal_mode:
        return ("done", None)
    try:
        from agent.auxiliary_client import get_text_auxiliary_client

        client, model = get_text_auxiliary_client("goal_judge")
    except Exception:
        client, model = None, None
    if client is None or not model:
        return ("done", None)

    from hermes_cli.goals import judge_goal

    verdict, reason, transport_failed = "done", "", False
    try:
        # Headless handoff checks run outside any agent turn: bind the per-task relay-affinity
        # scope (mirrors kanban_specify) so the relay does not reject the judge call (#113669).
        from agent.portal_tags import get_affinity_scope, reset_affinity_scope, set_affinity_scope
        affinity_token = None if get_affinity_scope() else set_affinity_scope(f"kanban:{task.id}")
        try:
            verdict, reason, _, _, transport_failed = judge_goal(
                goal=f"{task.title}\n\n{task.body or ''}".strip(),
                last_response=evidence.strip())
        finally:
            if affinity_token is not None:
                reset_affinity_scope(affinity_token)
    except Exception as judge_exc:
        import logging as _logging

        _logging.getLogger(__name__).warning("goal judge check failed, allowing lifecycle handoff: %s",
                                             judge_exc, exc_info=True)
    if transport_failed:
        # ``judge_goal`` fails open to ``continue`` on transport errors (relay 400, auth, timeout);
        # an unreachable judge is not a human "not done" and must not reject the handoff (#83610).
        import logging as _logging

        _logging.getLogger(__name__).warning("goal judge unreachable (%s), allowing lifecycle handoff", reason)
        return ("done", None)
    return (verdict, None if verdict == "done" else reason)


def _goal_gate_error(conn, tid: str, evidence: str, handoff: str, blocked_hint: str,
                     continue_hint: str) -> Optional[str]:
    """Goal-mode judge gate shared by ``complete`` / ``request-review`` (mirrors tools/kanban_tools.py);
    applied to every terminal handoff so request-review can't bypass it. Returns the error line, or
    None to allow."""
    verdict, rejection = _goal_mode_handoff_rejection(kb.get_task(conn, tid), evidence)
    if verdict == "blocked":
        return (f"kanban: goal {handoff} of {tid} rejected: judge ruled "
                f"the goal unachievable — {rejection}. {blocked_hint}")
    if rejection is not None:
        return f"kanban: goal {handoff} of {tid} rejected by judge: {rejection}. {continue_hint}"
    return None


def _cmd_complete(args: argparse.Namespace) -> int:
    """Mark one or more tasks done. Supports a single id or a list."""
    from hermes_cli import kanban_gate_invariants as _gate_inv
    ids, rc = _require_ids(args)
    if rc:
        return rc
    summary = getattr(args, "summary", None)
    raw_meta = getattr(args, "metadata", None)
    # Handoff fields are per-run; refuse to copy them across N runs.
    if len(ids) > 1 and (summary or raw_meta):
        return _err("kanban: --summary / --metadata are per-task and can't be used "
                    "with multiple ids (would apply the same handoff to every task). "
                    "Complete tasks one at a time, or drop the flags for the bulk close.", 2)
    metadata, rc = _parse_metadata_flag(raw_meta)
    if rc:
        return rc
    raw_evidence = getattr(args, "evidence", None)
    if raw_evidence:
        # The evidence gate's CLI door: a completion declares WHAT backs it. Parsed here so a
        # malformed declaration is a caller error at the prompt, not a refusal mid-sweep.
        if len(ids) > 1:
            return _err("kanban: --evidence is per-task and can't be used with multiple ids "
                        "(the same evidence cannot back N cards). Complete one at a time.", 2)
        from hermes_cli import kanban_gate_invariants as _gate_inv
        try:
            declared = json.loads(raw_evidence) if isinstance(raw_evidence, str) else raw_evidence
        except ValueError as exc:
            return _err(f"kanban: --evidence is not valid JSON: {exc}", 2)
        try:
            _gate_inv.parse_evidence(declared)
        except _gate_inv.EvidenceRefused as exc:
            return _err(f"kanban: --evidence: {exc}", 2)
        metadata = dict(metadata or {})
        metadata["evidence"] = declared
    # A deferral is per-task and per-child, and it is a DECLARATION: it needs a stated why.
    defer_ids = [str(x).strip() for x in (getattr(args, "defer_child", None) or []) if str(x).strip()]
    defer_reason = (getattr(args, "defer_reason", None) or "").strip()
    if defer_ids and len(ids) > 1:
        return _err("kanban: --defer-child is per-task and can't be used with multiple ids "
                    "(it would declare the same deferral for every task). Complete tasks one "
                    "at a time.", 2)
    if defer_ids and not defer_reason:
        return _err("kanban: --defer-child needs --defer-reason: a deferral states WHY the "
                    "child does not carry this card's remaining DoD (a low priority is never "
                    "read as intent).", 2)
    deferred_children = {child: defer_reason for child in defer_ids} or None
    fail_msg: dict[str, str] = {}
    with kbc.connect_closing() as conn:
        def op(tid):
            gate_err = _goal_gate_error(
                conn, tid, (summary or args.result or "").strip(), "completion",
                "Re-scope with kanban edit, or record the block with kanban block instead of completing.",
                "Provide evidence matching the task's acceptance criteria.")
            if gate_err:
                fail_msg[tid] = gate_err
                return False
            fail_msg[tid] = f"cannot complete {tid} (unknown id or terminal state)"
            try:
                done = kb.complete_task(conn, tid, result=args.result, summary=summary, metadata=metadata,
                                        expected_run_id=_worker_run_id_for(tid),
                                        deferred_children=deferred_children,
                                        force=bool(getattr(args, "force", False)))
            except kb.LiveClaimError:
                fail_msg[tid] = (f"cannot complete {tid}: a live worker is running it. Wait for the "
                                 f"worker, `hermes kanban reclaim {tid}` to release it, or re-run with "
                                 f"--force to close its run and complete anyway.")
                return False
            except kb.EmptyCompletionError as empty_err:
                fail_msg[tid] = (f"cannot complete {tid}: {empty_err}. Pass --result/--summary "
                                 f"describing what was done (an empty completion is not evidence).")
                return False
            except _gate_inv.GateRefused as gate_err:
                # An invariant refusal (evidence / assignee / dependency): the seam names the
                # invariant and the honest path, so it reads as a refusal, not a failed close.
                fail_msg[tid] = (f"cannot complete {tid} [{gate_err.invariant}"
                                 + (f"/{getattr(gate_err, 'cause', '')}" if getattr(gate_err, "cause", None) else "")
                                 + f"]: {gate_err}")
                return False
            except kb.HallucinatedCardsError as phantom:
                # The same refusal vocabulary as CompletionRefusal, but it stays an
                # exception: callers already catch it, and it must not read as a clean "no".
                fail_msg[tid] = f"cannot complete {tid} [{phantom.cause}]: {phantom}"
                return False
            except kb.ProofGateError as proof_err:
                # Deploy-proof gate clause (no_proof / proof_predates_landing / ...): the
                # fence is the card's ``landed`` contract, so name the clause, not "failed".
                fail_msg[tid] = f"cannot complete {tid} [{proof_err.clause}]: {proof_err}"
                return False
            if not done:
                # A typed refusal names its own cause; a bare False (no such id, or a
                # contract that cannot pass) must not be reported as a dependency problem.
                if isinstance(done, kb.CompletionRefusal):
                    fail_msg[tid] = f"cannot complete {tid} [{done.cause}]: {done.detail}"
                    return False
                blockers = kb.unsatisfied_parents(conn, tid)
                if blockers:
                    detail = ", ".join(f"{pid} ({status})" for pid, status in blockers)
                    fail_msg[tid] = (f"cannot complete {tid}: unsatisfied parent dependencies: {detail}; "
                                     f"complete the parents first, or `hermes kanban unlink <parent> {tid}`.")
            return done

        return _bulk_apply(ids, op, lambda tid: f"Completed {tid}", fail_msg.__getitem__)


def _cmd_edit(args: argparse.Namespace) -> int:
    result = getattr(args, "result", None)
    raw_metadata = getattr(args, "metadata", None)
    summary = getattr(args, "summary", None)
    title = getattr(args, "title", None)
    body = getattr(args, "body", None)
    priority = getattr(args, "priority", None)
    goal_mode = getattr(args, "goal_mode", None)
    goal_max_turns = getattr(args, "goal_max_turns", None)
    raw_max_runtime = getattr(args, "max_runtime", None)
    clear_failure = getattr(args, "clear_failure", False)
    if result is None and (summary is not None or raw_metadata is not None):
        return _err("kanban edit: --summary and --metadata require --result", 2)
    if goal_max_turns is not None and goal_max_turns < 0:
        return _err("kanban edit: --goal-max-turns must be 0 (clear) or a positive turn count", 2)
    try:
        max_runtime = _parse_duration(raw_max_runtime)
    except ValueError as exc:
        return _err(f"kanban edit: {exc}", 2)
    # ``None`` here means "clear the cap" whenever the flag was given at all, so the tri-state has to
    # be resolved from the RAW flag, not the parsed value: "--max-runtime none" and no flag at all
    # both parse to None, and only the first should write NULL.
    clear_max_runtime = raw_max_runtime is not None and max_runtime is None
    if all(value is None for value in (title, body, priority, result)) and not any((
        goal_mode is not None, goal_max_turns is not None, raw_max_runtime is not None,
        clear_failure,
    )):
        return _err(
            "kanban edit: provide --title, --body, --priority, --result, --goal/--no-goal, "
            "--goal-max-turns, --max-runtime, or --clear-failure", 2,
        )
    metadata, rc = _parse_metadata_flag(raw_metadata)
    if rc:
        return rc
    with kbc.connect_closing() as conn:
        ok = kb.edit_task(
            conn, args.task_id, title=title, body=body, priority=priority,
            result=result, summary=summary, metadata=metadata,
            goal_mode=goal_mode, goal_max_turns=goal_max_turns,
            max_runtime_seconds=max_runtime, clear_max_runtime=clear_max_runtime,
            clear_failure=clear_failure, actor=_profile_author(),
        )
    return _ok_or_err(
        ok,
        f"cannot edit {args.task_id} (unknown id, or --result used on a task that is not done)",
        f"Edited {args.task_id}{_edit_room_suffix(goal_mode, goal_max_turns, clear_max_runtime, raw_max_runtime, max_runtime)}"
        + ("  [failure streak cleared]" if clear_failure else ""),
    )


def _defcon_authority(args: argparse.Namespace) -> str:
    """Who is designating: ``--authority`` when given, else the acting profile."""
    return (getattr(args, "authority", None) or "").strip() or _profile_author()


def _cmd_defcon_designate(args: argparse.Namespace) -> int:
    """Put one card in the reserved tranche: ledger row first, then the priority."""
    reason = _stripped_or_none(getattr(args, "reason", None))
    if not reason:
        return _err("kanban defcon designate: --reason is required (it is the audit answer)", 2)
    with kbc.connect_closing() as conn:
        try:
            row = kb.designate_priority(
                conn, args.task_id, reason=reason, authority=_defcon_authority(args),
            )
        except (ValueError, PermissionError) as exc:
            return _err(f"kanban defcon designate: {exc}")
    if _json_out(args, row):
        return 0
    print(f"Designated {row['task_id']}: priority {row['priority']} on board {row['board']!r}.\n"
          f"  Authority: {row['authority']}\n"
          f"  Reason:    {row['reason']}\n"
          f"  Revoke restores priority {row['restore_priority']}.")
    return 0


def _cmd_defcon_revoke(args: argparse.Namespace) -> int:
    """Return a designated card to the ordinary priority it was holding."""
    reason = _stripped_or_none(getattr(args, "reason", None))
    if not reason:
        return _err("kanban defcon revoke: --reason is required (recorded on the event)", 2)
    with kbc.connect_closing() as conn:
        try:
            row = kb.revoke_priority_designation(conn, args.task_id, reason=reason)
        except (ValueError, PermissionError) as exc:
            return _err(f"kanban defcon revoke: {exc}")
    if row is None:
        return _err(f"kanban defcon revoke: {args.task_id} carries no live designation")
    if _json_out(args, row):
        return 0
    print(f"Revoked the designation on {row['task_id']}: back at priority "
          f"{row['restored_priority']} on board {row['board']!r}.\n"
          f"  It was designated at {row['designated_at']} by {row['authority'] or 'unknown'}: "
          f"{row['reason']}")
    return 0


_DEFCON_HANDLERS = {"designate": _cmd_defcon_designate, "revoke": _cmd_defcon_revoke}


def _cmd_bulk_approvals(args: argparse.Namespace) -> int:
    """``hermes kanban bulk-approvals <ask|approve|list|show>`` (decked in kanban_bulk_guard)."""
    handler = kbg.APPROVAL_HANDLERS.get(getattr(args, "bulk_approvals_action", None))
    if not handler:
        return _err("kanban bulk-approvals: unknown subcommand", 2)
    try:
        return int(handler(args) or 0)
    except kbg.BulkActionRefused as exc:
        return _err(str(exc), 1)


def _cmd_defcon(args: argparse.Namespace) -> int:
    """``hermes kanban defcon designate|revoke`` - the one door into the reserved tranche."""
    action = getattr(args, "defcon_action", None) or ""
    handler = _DEFCON_HANDLERS.get(action)
    if handler is None:
        return _err(f"kanban defcon: unknown action {action!r}; use designate or revoke", 2)
    return handler(args)
def _edit_room_suffix(goal_mode, goal_max_turns, clear_max_runtime, raw_max_runtime, max_runtime) -> str:
    """Confirm the room fields that were set, so the operator sees the value that LANDED.

    A silently-dropped flag is how ``--max-runtime`` came to be believed broken: the write worked and
    nothing on the human surface showed it. Echoing the stored value closes that loop.
    """
    parts: list[str] = []
    if goal_mode is True:
        parts.append(f"goal loop on{f' (max {goal_max_turns} turns)' if goal_max_turns else ''}")
    elif goal_mode is False:
        parts.append("goal loop off")
    elif goal_max_turns is not None and goal_max_turns > 0:
        parts.append(f"goal max turns {goal_max_turns}")
    if clear_max_runtime:
        parts.append("runtime cap cleared")
    elif max_runtime is not None:
        parts.append(f"max runtime {max_runtime}s")
    elif raw_max_runtime is not None:
        parts.append(f"max runtime {raw_max_runtime} (unchanged — it is not a duration)")
    return f" [{'; '.join(parts)}]" if parts else ""


def _cmd_retarget(args: argparse.Namespace) -> int:
    """Re-point a card's project + workspace — the recovery door for a mis-born card.

    Usable on a ``blocked``/``ready``/``todo`` card (the state a mis-born card is in
    while somebody notices it is bound to the wrong repo); refused on a terminal card
    and on a card another worker currently holds a live claim on (``--force``
    overrides the claim guard). The change is recorded as a ``retargeted`` event.
    """
    try:
        ws_kind, ws_path = _parse_workspace_flag(getattr(args, "workspace", None))
        branch = _parse_branch_flag(getattr(args, "branch", None))
    except argparse.ArgumentTypeError as exc:
        return _err(f"kanban retarget: {exc}", 2)
    if branch and ws_kind not in (None, "worktree"):
        return _err("kanban retarget: --branch is only valid with a worktree workspace", 2)
    with kbc.connect_closing() as conn:
        try:
            result = kb.retarget_task(
                conn, args.task_id,
                project=getattr(args, "project", None),
                workspace_kind=ws_kind, workspace_path=ws_path, branch_name=branch,
                reason=getattr(args, "reason", None),
                actor=getattr(args, "author", None) or _profile_author(),
                force=bool(getattr(args, "force", False)),
            )
        except ValueError as exc:
            return _err(f"kanban retarget: {exc}", 2)
    if result is None:
        return _err(f"cannot retarget {args.task_id}: unknown task id", 2)
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    old, new = result["old"], result["new"]
    if not result["changed"]:
        print(f"{args.task_id} already bound to {new['workspace_kind']}"
              f"{' ' + new['workspace_path'] if new['workspace_path'] else ''} "
              f"(project {new['project_id'] or '-'}) - no change")
        return 0
    print(f"Retargeted {args.task_id}: project {old['project_id'] or '-'} -> "
          f"{new['project_id'] or '-'}, workspace {old['workspace_kind']}"
          f"{' ' + old['workspace_path'] if old['workspace_path'] else ''} -> "
          f"{new['workspace_kind']}"
          f"{' ' + new['workspace_path'] if new['workspace_path'] else ''} "
          f"(recorded as a retargeted event; a parked card stays parked - unblock it to resume)")
    return 0


def _commented(conn, reason: Optional[str], author, prefix: str, op):
    """Wrap a per-task ``op`` so a ``reason`` is first recorded as a ``PREFIX: reason`` comment."""
    def run(tid):
        if reason:
            kb.add_comment(conn, tid, author, f"{prefix}: {reason}")
        return op(tid)
    return run


def _cmd_block(args: argparse.Namespace) -> int:
    from hermes_cli import kanban_gate_invariants as _gate_inv
    reason = _joined_words(args.reason)
    kind = getattr(args, "kind", None)
    raw_waits = getattr(args, "waits_on", None) or ""
    waits_on = [p.strip() for p in str(raw_waits).replace(" ", ",").split(",") if p.strip()]
    author = _profile_author()
    ids = _bulk_ids(args)
    suffix = f": {reason}" if reason else ""
    due_raw = getattr(args, "due", None)
    window_policy = getattr(args, "window_policy", None)
    if kind == "dependency" and (due_raw or window_policy):
        return _err("--due/--window-policy are meaningless on a dependency block: "
                    "it waits on parent completion, not a clock")
    due_at: Any = None
    if due_raw:
        from hermes_cli import kanban_due as kdue
        try:
            due_at = kdue.parse_due(due_raw)
        except ValueError as exc:
            return _err(f"--due {due_raw!r}: {exc}")
    due_note = ""
    if due_at is not None:
        due_note = (
            f" -- auto-release {_fmt_ts(int(due_at))} "
            f"({window_policy or kb.DEFAULT_DUE_WINDOW_POLICY})"
        )
    failures: dict[str, str] = {}
    with kbc.connect_closing() as conn:
        def ok_msg(tid):
            # Report where it landed: dependency blocks -> todo; a tripped
            # unblock-loop breaker PARKS the card in blocked (never triage, which
            # has no exit for a parked card) so name the park explicitly.
            landed = kb.get_task(conn, tid)
            where = landed.status if landed else "blocked"
            if where == "todo":
                return f"{tid} → todo (dependency wait){suffix}"
            if kind == "dependency" and where == "blocked":
                return f"Blocked {tid} as needs_input (no open parent to wait on){suffix}"
            if where == "blocked" and (landed.block_recurrences or 0) >= kb.BLOCK_RECURRENCE_LIMIT:
                # Only a typed owner-input block carries a question for a human.
                verdict = ("needs a human decision" if (landed.block_kind if landed else kind) == "needs_input"
                           else "orchestration attention needed")
                return (f"{tid} → blocked, parked (unblock loop detected after "
                        f"{landed.block_recurrences} same-kind re-blocks — {verdict}){suffix}")
            return f"Blocked {tid}{suffix}{due_note}"
        def op(tid: str) -> bool:
            try:
                return kb.block_task(
                    conn, tid, reason=reason, kind=kind, expected_run_id=_worker_run_id_for(tid),
                    waits_on=waits_on, due_at=due_at, window_policy=window_policy,
                )
            except ValueError as exc:  # e.g. --window-policy with no due time
                failures[tid] = f"{tid}: {exc}"
                return False

        op = _commented(conn, reason, author, "BLOCKED", op)
        try:
            return _bulk_apply(ids, op, ok_msg, lambda tid: failures.get(tid) or f"cannot block {tid}")
        except _gate_inv.GateRefused as gate_err:
            # Invariant C refused the block (a named dependency with no edge, or a waits_on
            # pointer that does not exist): nothing was written, and the message names the fix.
            print(f"cannot block [{gate_err.invariant}]: {gate_err}", file=sys.stderr)
            return 1


def _cmd_schedule(args: argparse.Namespace) -> int:
    reason = _joined_words(args.reason)
    author = _profile_author()
    ids = _bulk_ids(args)
    suffix = f": {reason}" if reason else ""
    due_raw = getattr(args, "due", None)
    clear_due = bool(getattr(args, "clear_due", False))
    window_policy = getattr(args, "window_policy", None)
    if due_raw and clear_due:
        return _err("--due and --clear-due are mutually exclusive")
    due_at: Any = kb.UNSET
    if clear_due:
        due_at = None
    elif due_raw:
        from hermes_cli import kanban_due as kdue
        try:
            due_at = kdue.parse_due(due_raw)
        except ValueError as exc:
            return _err(f"--due {due_raw!r}: {exc}")
    due_note = ""
    if due_at is not kb.UNSET:
        due_note = (
            f" — due {_fmt_ts(int(due_at))} "
            f"({window_policy or kb.DEFAULT_DUE_WINDOW_POLICY})"
            if due_at is not None else " — due time cleared (wakes by hand only)"
        )
    failures: dict[str, str] = {}

    def op(tid: str) -> bool:
        try:
            return kb.schedule_task(
                conn, tid, reason=reason, expected_run_id=_worker_run_id_for(tid),
                due_at=due_at, window_policy=window_policy,
            )
        except ValueError as exc:  # e.g. --window-policy with no due time
            failures[tid] = f"{tid}: {exc}"
            return False

    with kbc.connect_closing() as conn:
        op = _commented(conn, reason, author, "SCHEDULED", op)
        return _bulk_apply(ids, op, lambda tid: f"Scheduled {tid}{suffix}{due_note}",
                           lambda tid: failures.get(tid) or f"cannot schedule {tid}")


def _triage_exit_hint(conn, tid: str) -> str:
    """Suffix naming the supported exits when ``tid`` is sitting in ``triage``.

    ``triage`` must never be a one-way door (D4): a guard that refuses a row in
    that status still has to say which verb *does* apply, or the operator is back
    to hand-written SQL on the live board. Empty string for any other status, so
    a genuine unknown-id refusal stays honest.
    """
    row = kb.get_task(conn, tid)
    if row is None or row.status != "triage":
        return ""
    return (f" — triage is not a dead end: release it with `hermes kanban promote {tid}` "
            f"or close it with `hermes kanban complete {tid}`")


def _cmd_unblock(args: argparse.Namespace) -> int:
    if os.environ.get("HERMES_KANBAN_TASK"):
        return _err("kanban unblock is orchestrator-only; workers must hand off their assigned task")
    ids, rc = _require_ids(args)
    if rc:
        return rc
    reason = _stripped_or_none(getattr(args, "reason", None))
    author = _profile_author() if reason else None
    suffix = f": {reason}" if reason else ""
    with kbc.connect_closing() as conn:
        op = _commented(conn, reason, author, "UNBLOCK", lambda tid: kb.unblock_task(conn, tid))
        return _bulk_apply(ids, op, lambda tid: f"Unblocked {tid}{suffix}",
                           lambda tid: f"cannot unblock {tid} (not blocked/scheduled?)"
                                       + _triage_exit_hint(conn, tid))


def _cmd_reopen(args: argparse.Namespace) -> int:
    """``done``/``archived`` -> a LIVE status. THE sanctioned done->live door.

    SINGLE id by design: a multi-id reopen->ready is the bulk-guard ``promote``
    class, so the door stays one card per call (the SDLC flow's repair is one
    card per item). Never a raw SQL status write — this is the only CLI surface
    that reaches :func:`kanban_db.reopen_task`.
    """
    tid = args.task_id
    reason = _stripped_or_none(getattr(args, "reason", None))
    dest_status = _stripped_or_none(getattr(args, "to", None))
    block_kind = _stripped_or_none(getattr(args, "block_kind", None))
    dry_run = bool(getattr(args, "dry_run", False))
    actor = _profile_author()
    with kbc.connect_closing() as conn:
        ok, err, info = kb.reopen_task(
            conn, tid, actor=actor, reason=reason or "",
            dest_status=dest_status, block_kind=block_kind, dry_run=dry_run,
        )
        if getattr(args, "json", False):
            _print_json({"task_id": tid, "reopened": bool(ok), "dry_run": dry_run,
                         "to_status": (info or {}).get("to_status"), "error": err})
            return 0 if ok else 1
        if not ok:
            return _err(f"cannot reopen {tid}: {err or 'not done/archived?'}")
        tag = " (dry)" if dry_run else ""
        label = "Would reopen" if dry_run else "Reopened"
        suffix = f": {reason}" if reason else ""
        to_status = (info or {}).get("to_status") or dest_status or "ready"
        print(f"{label} {tid} -> {to_status}{tag}{suffix}")
    return 0


def _cmd_request_review(args: argparse.Namespace) -> int:
    tid = args.task_id
    summary = _stripped_or_none(getattr(args, "summary", None))
    metadata, rc = _parse_metadata_flag(getattr(args, "metadata", None))
    if rc:
        return rc
    with kbc.connect_closing() as conn:
        gate_err = _goal_gate_error(
            conn, tid, summary or "", "review handoff",
            "Record the block with kanban block instead of requesting review.",
            "Provide acceptance evidence matching the task.")
        if gate_err:
            return _err(gate_err)
        ok, reason = kb.request_review(
            conn, tid, summary=summary, metadata=metadata, reviewer=getattr(args, "reviewer", None),
            expected_run_id=_worker_run_id_for(tid), force=bool(getattr(args, "force", False)), with_reason=True)
        if not ok:
            return _err(f"cannot request review for {tid}: {reason or 'not running/ready?'}")
        persisted_run = kb.latest_run(conn, tid)
        display_summary = persisted_run.summary if persisted_run else None
        print(f"Requested review for {tid}" + (f": {display_summary}" if display_summary else ""))
    return 0


def _cmd_request_changes(args: argparse.Namespace) -> int:
    tid = args.task_id
    reason = " ".join(args.reason).strip()
    with kbc.connect_closing() as conn:
        ok, detail = kb.request_changes(conn, tid, reason=reason, expected_run_id=_worker_run_id_for(tid))
        if not ok:
            return _err(f"cannot request changes for {tid}: {detail or 'invalid review state'}")
        print(f"Requested changes for {tid}" + (f"; routed to {detail}" if detail else ""))
    return 0


def _cmd_reopen_review(args: argparse.Namespace) -> int:
    ids, rc = _require_ids(args)
    if rc:
        return rc
    reason = getattr(args, "reason", None)
    if reason is not None:
        reason = str(kb.redact_review_value(reason.strip())).strip() or None
    author = _profile_author() if reason else None
    suffix = f": {reason}" if reason else ""
    with kbc.connect_closing() as conn:
        def op(tid):
            if not kb.reopen_review_task(conn, tid):
                return False
            if reason:
                kb.add_comment(conn, tid, author or "operator", f"CHANGES REQUESTED: {reason}")
            return True

        return _bulk_apply(ids, op, lambda tid: f"Reopened {tid}{suffix}",
                           lambda tid: f"cannot reopen {tid} (not in review?)")


def _cmd_promote(args: argparse.Namespace) -> int:
    reason = _joined_words(args.reason)
    author = _profile_author()
    # Dedupe while preserving order; positional task_id always first.
    ids = list(dict.fromkeys(_bulk_ids(args)))
    dry_run = bool(args.dry_run)

    results: list[dict[str, object]] = []
    with kbc.connect_closing() as conn:
        for tid in ids:
            ok, err = kb.promote_task(conn, tid, actor=author, reason=reason, dry_run=dry_run)
            results.append({"task_id": tid, "promoted": ok, "dry_run": dry_run,
                            "reason": reason, "error": err})

    failed = [r for r in results if not r["promoted"]]
    if getattr(args, "json", False):
        # Single-id stays a flat object for back-compat; bulk emits a list.
        _print_json(results[0] if len(results) == 1 else results)
        return 0 if not failed else 1

    tag = " (dry)" if dry_run else ""
    label = "Would promote" if dry_run else "Promoted"
    suffix = f": {reason}" if reason else ""
    for r in results:
        if r["promoted"]:
            print(f"{label} {r['task_id']} -> ready{tag}{suffix}")
        else:
            print(f"cannot promote {r['task_id']}: {r['error']}", file=sys.stderr)
    return 0 if not failed else 1


def _cmd_archive(args: argparse.Namespace) -> int:
    ids = list(args.task_ids or [])
    purge_ids = list(getattr(args, "purge_ids", None) or [])
    if ids and purge_ids:
        return _err("choose either task_ids to archive or --rm archived task_ids")
    if not ids and not purge_ids:
        return _err("at least one task_id is required")
    with kbc.connect_closing() as conn:
        if purge_ids:
            # Store-layer re-assert (card t_bf9605f8): this is a bulk purge over N ids, so it does
            # not merely trust the dispatch seam's earlier check — it asserts the SAME admission
            # (same verb, same canonical scope, same digest) immediately before the delete loop, so
            # a caller that reaches this loop without passing the seam is refused here.
            kbg.assert_bulk_approved(
                board=str(getattr(args, "board", "") or "") or kb.get_current_board(),
                verb="archive", params={"task_ids": [], "purge_ids": purge_ids},
                approval=getattr(args, "approval", "") or "",
            )
            return _bulk_apply(purge_ids, lambda tid: kb.delete_archived_task(conn, tid), lambda tid: f"Deleted {tid}",
                               lambda tid: f"cannot delete {tid} (must already be archived)")
        return _bulk_apply(ids, lambda tid: kb.archive_task(conn, tid),
                           lambda tid: f"Archived {tid}", lambda tid: f"cannot archive {tid}")


def _cmd_stats(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        stats = kb.board_stats(conn)
    if _json_out(args, stats):
        return 0
    print("By status:")
    for k in kanban_workflow.DEFAULT_WORKFLOW.keys():
        print(f"  {k:8s}  {stats['by_status'].get(k, 0)}")
    if stats["by_assignee"]:
        print("\nBy assignee:")
        for who, counts in sorted(stats["by_assignee"].items()):
            print(f"  {who:20s}  {_fmt_counts(counts)}")
    age = stats["oldest_ready_age_seconds"]
    if age is not None:
        print(f"\nOldest ready task age: {int(age)}s")
    return 0


def _cmd_health(args: argparse.Namespace) -> int:
    """Is the ready queue moving, and if not why — the read that tells a starved
    board from an idle one, on demand (the dispatcher escalates it as a card).

    ``state`` is the one-word verdict: ``starved`` (spawnable rows exist and
    every one of them is held back), ``dispatchable`` (rows can start),
    ``idle`` (nothing to run). Exits 0 either way — this is a read, and callers
    that want to act on starvation parse ``--json``.
    """
    board = kb.get_current_board()
    with kbc.connect_closing() as conn:
        health = kbd.board_health(conn, board=board)
    if _json_out(args, {"board": board, **health.as_dict()}):
        return 0
    print(health.describe())
    print(f"  board: {board}")
    if health.state == "starved":
        print(f"  {health.suppressed} ready task(s) held back with nothing else to run — "
              "the dispatcher cannot start this board's queue.")
        print("  `hermes kanban tail <task-id>` on a held task shows the guard that holds it; "
              "a deliberate re-queue (`hermes kanban unblock` / `promote`, or a done→ready move) "
              "lifts it.")
    return 0


def _cmd_notify_subscribe(args: argparse.Namespace) -> int:
    delivery_metadata = {
        key: value
        for key, value in (
            ("parent_chat_id", getattr(args, "parent_chat_id", None)),
            ("guild_id", getattr(args, "guild_id", None)),
        )
        if value
    }
    with kbc.connect_closing() as conn:
        if kb.get_task(conn, args.task_id) is None:
            return _err(f"no such task: {args.task_id}")
        kbn.add_notify_sub(
            conn, task_id=args.task_id, platform=args.platform, chat_id=args.chat_id,
            chat_type=args.chat_type, thread_id=args.thread_id, user_id=args.user_id,
            user_id_alt=getattr(args, "user_id_alt", None),
            notifier_profile=args.notifier_profile or _profile_author(),
            delivery_mode=getattr(args, "delivery_mode", None),
            delivery_metadata=delivery_metadata or None,
        )
    print(f"Subscribed {args.platform}:{args.chat_id}" + (f":{args.thread_id}" if args.thread_id else "")
          + f" to {args.task_id}")
    return 0


def _cmd_notify_list(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        subs = kbn.list_notify_subs(conn, args.task_id)
    if _json_out(args, subs):
        return 0
    if not subs:
        print("(no subscriptions)")
        return 0
    for s in subs:
        thr = f":{s['thread_id']}" if s.get("thread_id") else ""
        dmode, ctype = s.get("delivery_mode") or "notify", s.get("chat_type") or "dm"
        extras = "".join((
            f"  owner={s['notifier_profile']}" if s.get("notifier_profile") else "",
            "" if ctype == "dm" else f"  chat_type={ctype}",
            f"  user_id_alt={s['user_id_alt']}" if s.get("user_id_alt") else "",
            "" if dmode == "notify" else f"  mode={dmode}",
        ))
        print(f"  {s['task_id']:10s}  {s['platform']}:{s['chat_id']}{thr}  (since event {s['last_event_id']}){extras}")
    return 0


def _cmd_notify_unsubscribe(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        ok = kbn.remove_notify_sub(conn, task_id=args.task_id, platform=args.platform, chat_id=args.chat_id,
                                  thread_id=args.thread_id)
    return _ok_or_err(ok, "(no such subscription)", f"Unsubscribed from {args.task_id}")


def _cmd_log(args: argparse.Namespace) -> int:
    content = kb.read_worker_log(args.task_id, tail_bytes=args.tail)
    if content is None:
        return _err(f"(no log for {args.task_id} — task may not have spawned yet)")
    sys.stdout.write(content)
    if not content.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def _cmd_runs(args: argparse.Namespace) -> int:
    """Show attempt history for a task."""
    rsk, rc = _run_state_kwargs(args, "runs")
    if rc:
        return rc
    with kbc.connect_closing() as conn:
        runs = kb.list_runs(conn, args.task_id, **rsk)
    if _json_out(args, [_obj_dict(r, _RUNS_RUN_FIELDS) for r in runs]):
        return 0
    if not runs:
        print(f"(no runs yet for {args.task_id})")
        return 0
    print(f"{'#':3s}  {'OUTCOME':12s}  {'PROFILE':16s}  {'ELAPSED':>8s}  STARTED")
    for i, r in enumerate(runs, 1):
        end = r.ended_at or int(time.time())
        # Clamp to 0 so NTP backward-jumps don't print negative durations.
        elapsed = max(0, end - r.started_at)
        el = f"{elapsed}s" if elapsed < 60 else f"{elapsed // 60}m" if elapsed < 3600 else f"{elapsed / 3600:.1f}h"
        outcome = r.outcome or ("(running)" if not r.ended_at else r.status)
        print(f"{i:3d}  {outcome:12s}  {(r.profile or '-'):16s}  {el:>8s}  {_fmt_ts(r.started_at)}")
        if r.summary:
            print(f"     → {r.summary.splitlines()[0][:100]}")
        if r.error:
            print(f"     ✖ {r.error[:100]}")
    return 0


def _cmd_context(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        text = kb.build_worker_context(conn, args.task_id)
    print(text)
    return 0


def _run_triage_sweep(args: argparse.Namespace, verb: str, mod, run_one, json_key: str,
                      json_fields: tuple[str, ...], human_ok) -> int:
    """Shared driver for ``specify`` / ``decompose``: validate ids (one task id XOR ``--all``), run
    ``run_one(tid, author=...)`` per id, print JSON or human lines, exit code."""
    all_flag = bool(getattr(args, "all_triage", False))
    author = getattr(args, "author", None) or _profile_author()
    want_json = bool(getattr(args, "json", False))
    tenant = getattr(args, "tenant", None)
    if args.task_id and all_flag:
        return _err("kanban: pass either a task id OR --all, not both", 2)
    if all_flag:
        ids = mod.list_triage_ids(tenant=tenant)
        if not ids:
            if want_json:
                print(json.dumps({json_key: 0, "total": 0}))
            else:
                print("No triage tasks" + (f" for tenant {tenant!r}" if tenant else "") + ".")
            return 0
    elif args.task_id:
        ids = [args.task_id]
    else:
        return _err(f"kanban: {verb} requires a task id or --all", 2)

    ok_count = 0
    for tid in ids:
        outcome = run_one(tid, author=author)
        if outcome.ok:
            ok_count += 1
        if want_json:
            print(json.dumps(_obj_dict(outcome, json_fields)))
        elif outcome.ok:
            print(human_ok(outcome))
        else:
            print(f"kanban: {verb} {outcome.task_id}: {outcome.reason}", file=sys.stderr)
    if not all_flag:
        return 0 if ok_count == 1 else 1
    # --all: exit 1 only when every candidate failed (honest signal for scripts).
    return 0 if (ok_count > 0 or not ids) else 1


def _retitled_suffix(outcome) -> str:
    return f" — retitled: {outcome.new_title!r}" if outcome.new_title else ""


def _cmd_specify(args: argparse.Namespace) -> int:
    """Spec a triage task (or all) via the auxiliary LLM, promote to todo."""
    from hermes_cli import kanban_specify as spec

    return _run_triage_sweep(args, "specify", spec, spec.specify_task, "specified",
                             ("task_id", "ok", "reason", "new_title"),
                             lambda o: f"Specified {o.task_id} → todo{_retitled_suffix(o)}")


def _decompose_ok_line(o) -> str:
    if o.fanout and o.child_ids:
        return (f"Decomposed {o.task_id} → {len(o.child_ids)} "
                f"children ({', '.join(o.child_ids)}); root promoted to todo")
    return f"Specified {o.task_id} → todo (no fanout){_retitled_suffix(o)}"


def _cmd_decompose(args: argparse.Namespace) -> int:
    """Fan a triage task (or all) out into child tasks via the auxiliary LLM."""
    from hermes_cli import kanban_decompose as decomp

    return _run_triage_sweep(args, "decompose", decomp, decomp.decompose_task, "decomposed",
                             ("task_id", "ok", "reason", "fanout", "child_ids", "new_title"), _decompose_ok_line)


def _cmd_gates(args: argparse.Namespace) -> int:
    """``hermes kanban gates report|reconcile`` — the relations the write seams enforce.

    FOUR invariant families, one verb: ``rank`` (no card ranks below a card it gates),
    ``assignee`` (no row is assigned to a handle that cannot run), ``dependency`` (no block
    waits on a card in prose with no edge) and ``evidence`` (no completion carries no evidence
    record). ``report`` is read-only and its EXIT CODE is the answer (0 = every invariant
    holds, 1 = at least one is violated), so a carrier can gate on it instead of parsing prose.
    ``reconcile`` is the deterministic pass: it repairs what has a deterministic repair, then
    RE-MEASURES, and a non-zero exit means the board is still out of order after the pass - a
    false green is the failure mode this exists to make impossible.
    """
    from hermes_cli import kanban_gate_invariants as _gate_inv
    action = (getattr(args, "action", None) or "report").strip().lower()
    if action not in ("report", "reconcile"):
        return _err("kanban gates: unknown action %r; use report or reconcile" % action, 2)
    if action == "reconcile" and _is_delegated_child_cli_mutation(args):
        # Reading is safe anywhere; repairing rewrites priorities and edges on boards the
        # caller may be judged on, so it is a host/operator act (the DB layer refuses it too).
        return _err(_DELEGATED_CHILD_CLI_REFUSAL)
    with kbc.connect_closing() as conn:
        if action == "report":
            violations = kb.gate_violations(conn)
            families = _gate_inv.report(conn)
            record: dict = {
                "board": kb.board_for_connection(conn) or "",
                "violations": violations,
                "violations_total": len(violations),
                "families": families["violations"],
                "counts": families["counts"],
                "families_total": families["violations_total"],
                "open_total": families["open_total"],
                "armed": families["armed"],
            }
            remaining = record["violations_total"] + families["open_total"]
        else:
            record = kb.reconcile_gate_priorities(conn)
            extra = _gate_inv.reconcile(conn, cause="gates reconcile")
            record["families"] = extra["violations"]
            record["counts"] = extra["counts_after"]
            record["counts_before_families"] = extra["counts_before"]
            record["repaired"] = extra["repaired"]
            record["unrepaired"] = extra["unrepaired"]
            record["families_before"] = extra["violations_before"]
            record["families_after"] = extra["violations_after"]
            record["open_total"] = extra["open_total_after"]
            record["armed"] = extra["armed"]
            record["evidence_gate_since"] = extra["evidence_gate_since"]
            record["reconcile_stamped_evidence_since"] = extra["stamped_evidence_since"]
            remaining = record["violations_after"] + extra["open_total_after"]
    if _json_out(args, record):
        return 0 if (remaining == 0 and not record.get("failed")) else 1
    if action == "report":
        families = record["families"] or {}
        rank_rows = families.get("rank") or record["violations"]
        open_total = record["violations_total"] + record["open_total"]
        if open_total == 0:
            print("Gate invariants hold on board %r: no open violation in rank, assignee, "
                  "dependency or evidence." % record["board"])
            return 0
        print("Gate invariants on board %r: %d open violation(s) "
              "(rank %d, assignee %d, dependency %d, evidence %d)."
              % (record["board"], open_total,
                 len(rank_rows), len(families.get("assignee") or []),
                 len(families.get("dependency") or []),
                 len(families.get("evidence") or [])))
        for name in ("rank", "assignee", "dependency", "evidence"):
            rows = rank_rows if name == "rank" else (families.get(name) or [])
            if not rows:
                continue
            print("%s: %d" % (name, len(rows)))
            for row in rows[:50]:
                if name == "rank":
                    print("  %s (p=%s) ranks below the card it gates, %s (p=%s)"
                          % (row["parent_id"], row["parent_priority"],
                             row["child_id"], row["child_priority"]))
                elif name == "assignee":
                    print("  %s (%s) assignee=%r resolves nowhere"
                          % (row["task_id"], row["status"], row["assignee"]))
                elif name == "dependency":
                    print("  %s blocked, names %s with no edge%s"
                          % (row["task_id"], ", ".join(row.get("named") or []),
                             " (already shipped: %s)"
                             % ", ".join(row.get("dependencies_shipped") or [])
                             if row.get("dependencies_shipped") else ""))
                else:
                    print("  %s done with no evidence record" % row["task_id"])
        if record["violations_total"] == 0 and record["open_total"] == 0:
            return 0
        return 1
    print("Gate reconcile on board %r: %d rank edge(s) before, %d lift(s), %d after; "
          "assignee/dependency/evidence %s before -> %s after."
          % (record["board"], record["violations_before"], record["lifts_total"],
             record["violations_after"],
             {k: v for k, v in (record.get("families_before") or {}).items()},
             {k: v for k, v in (record.get("families_after") or {}).items()}))
    for lift in record["lifts"]:
        print("  lifted %s: %d -> %d (gates %s at %d, cause %s%s)"
              % (lift["task_id"], lift["before"], lift["now"], lift["gate"],
                 lift["gate_priority"], lift["cause"],
                 ", designating it" if lift["tranche"] else ""))
    for row in record.get("repaired", []):
        print("  repaired %s" % row)
    for row in record.get("unrepaired", [])[:25]:
        print("  UNREPAIRED %s (%s): %s"
              % (row.get("task_id"), row.get("assignee") or row.get("block_kind") or "",
                 row.get("why")), file=sys.stderr)
    for bad in record["failed"]:
        print("  FAILED %s: %s" % (bad["task_id"], bad["error"]), file=sys.stderr)
    return 0 if (remaining == 0 and not record["failed"]) else 1


_HANDLERS = {
    "init": _cmd_init, "create": _cmd_create, "rollup": _cmd_rollup, "swarm": _cmd_swarm,
    "list": _cmd_list, "ls": _cmd_list, "show": _cmd_show,
    "assign": _cmd_assign, "set-model": _cmd_set_model,
    "set-contract": _cmd_set_contract,
    "reclaim": _cmd_reclaim, "reassign": _cmd_reassign,
    "diagnostics": _cmd_diagnostics, "diag": _cmd_diagnostics,
    "link": _cmd_link, "unlink": _cmd_unlink, "claim": _cmd_claim,
    "comment": _cmd_comment, "attach": _cmd_attach,
    "attachments": _cmd_attachments, "attach-rm": _cmd_attach_rm,
    "complete": _cmd_complete, "edit": _cmd_edit, "retarget": _cmd_retarget,
    "block": _cmd_block,
    "defcon": _cmd_defcon,
    "schedule": _cmd_schedule, "unblock": _cmd_unblock, "reopen": _cmd_reopen,
    "request-review": _cmd_request_review, "request-changes": _cmd_request_changes,
    "reopen-review": _cmd_reopen_review, "promote": _cmd_promote,
    "archive": _cmd_archive, "tail": _cmd_tail, "dispatch": _cmd_dispatch,
    "daemon": _cmd_daemon, "watch": _cmd_watch, "stats": _cmd_stats,
    "health": _cmd_health,
    "log": _cmd_log, "runs": _cmd_runs, "heartbeat": _cmd_heartbeat,
    "assignees": _cmd_assignees, "notify-subscribe": _cmd_notify_subscribe,
    "notify-list": _cmd_notify_list, "notify-unsubscribe": _cmd_notify_unsubscribe,
    "context": _cmd_context, "specify": _cmd_specify, "decompose": _cmd_decompose,
    "gc": _cmd_gc, "bulk-approvals": _cmd_bulk_approvals,
    # The gate invariant's pass. Read-only `report` stays available to every lane (it is how a
    # lane answers "is the board out of order?"); `reconcile` writes, and is refused to a
    # delegated child inside the handler.
    "gates": _cmd_gates,
}


# --- Slash-command entry point (used by /kanban from CLI and gateway) ---

_SLASH_KANBAN_HELP = """\
**/kanban** — manage the shared task board.

Common subcommands:
  `list` (alias `ls`)   List tasks on the current board
  `show <id>`           Task details + comments + events
  `stats`               Per-status / per-assignee counts
  `create <title>…`     Create a task (auto-subscribes you to events)
  `comment <id> <msg>`  Append a comment
  `attach <id> <path>`  Attach a local file; `attachments <id>` to list
  `complete <id>…`      Mark task(s) done
  `request-review <id>` Enter first-class review; `request-changes <id> <reason>` returns an active review to its implementer
  `block <id> [reason]` Mark blocked; `schedule <id> [reason]` parks time-delay work; `unblock <id>` to revive
  `assign <id> <profile>`  Reassign
  `boards list`         Show all boards
  `assignees`           Known profiles + counts
  `context <id>`        Full worker-context dump
  `runs <id>`           Attempt history
  `log <id>`            Worker log

Run `/kanban <subcommand> -h` for arguments. \
Read-only commands are safe while an agent is running.\
"""


def run_slash(rest: str) -> str:
    """Execute a ``/kanban …`` string (``rest`` = everything after ``/kanban``) and return captured
    stdout/stderr. Shared by the interactive CLI and the gateway so formatting is identical."""
    import io

    # Non-posix split (Windows) keeps backslashes as path separators but
    # leaves quote characters in the tokens — strip a fully wrapping pair
    # so `"my task"` reaches argparse as `my task`, not `"my task"`.
    tokens = []
    if rest and rest.strip():
        for tok in shlex.split(rest, posix=os.name == "posix"):
            if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ("'", '"'):
                tok = tok[1:-1]
            tokens.append(tok)

    # Bare ``/kanban`` or ``/kanban help`` / ``--help`` / ``-h`` / ``?``:
    # show the curated short-help block instead of dumping argparse's full
    # usage tree (which is enormous and reads as garbage in a chat
    # bubble).  Per-subcommand help still works via ``/kanban foo -h``.
    if not tokens or tokens[0] in {"help", "--help", "-h", "?"}:
        return _SLASH_KANBAN_HELP
    # build_parser() needs a subparsers action to attach to: build a throwaway one and drive
    # kanban_parser directly so usage/error text reads ``/kanban``.
    _wrap = argparse.ArgumentParser(prog="/kanban-wrap", add_help=False)
    _wrap.exit_on_error = False  # type: ignore[attr-defined]
    kanban_parser = build_parser(_wrap.add_subparsers(dest="_top"))
    kanban_parser.prog = "/kanban"
    kanban_parser.exit_on_error = False  # type: ignore[attr-defined]
    subparsers = [a for a in kanban_parser._actions if isinstance(a, argparse._SubParsersAction)]
    for _action in subparsers:
        for _name, _choice in _action.choices.items():
            _choice.prog = f"/kanban {_name}"
            _choice.exit_on_error = False  # type: ignore[attr-defined]

    def _usage_for_error() -> str:
        if tokens:
            for _action in subparsers:
                subparser = _action.choices.get(tokens[0])
                if subparser is not None:
                    return subparser.format_usage().rstrip()
        return kanban_parser.format_usage().rstrip()

    buf_out, buf_err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            args = kanban_parser.parse_args(tokens)
    except SystemExit as exc:
        out, err = buf_out.getvalue().rstrip(), buf_err.getvalue().rstrip()
        if exc.code in {0, None} and out:  # ``-h`` help dump
            return out
        body = err or out
        return f"⚠ /kanban usage error\n{body}" if body else "⚠ /kanban usage error"
    except argparse.ArgumentError as exc:
        return f"⚠ /kanban usage error\n{_usage_for_error()}\n{exc}"

    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
        try:
            kanban_command(args)
        except SystemExit:
            pass
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)

    out, err = buf_out.getvalue().rstrip(), buf_err.getvalue().rstrip()
    if err and out:
        return f"{out}\n{err}"
    return err if err else (out or "(no output)")
