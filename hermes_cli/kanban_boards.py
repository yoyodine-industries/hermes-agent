"""``hermes kanban boards …`` — board directories, the ``current`` pointer and ``board.json``.
Filesystem-only, so every action works before ``kanban init`` and must ignore the shared
``--board`` task-routing override.
"""

from __future__ import annotations

import argparse
from typing import Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_output import _err, _fmt_counts, _json_out


def _dispatch_boards(args: argparse.Namespace) -> int:
    """``hermes kanban boards <action>`` — filesystem-only, so it works before ``kanban init``."""
    sub = getattr(args, "boards_action", None) or "list"
    handler = _BOARD_HANDLERS.get(sub)
    if handler is None:
        return _err(f"kanban boards: unknown action {sub!r}", 2)
    return handler(args)


def _board_task_counts(slug: str) -> dict[str, int]:
    """``{status: count}`` for a board. Safe to call on an empty DB."""
    try:
        if not kb.kanban_db_path(board=slug).exists():
            return {}
        with kbc.connect_closing(board=slug) as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall()
        return {r["status"]: int(r["n"]) for r in rows}
    except Exception:
        return {}


def _board_slug_arg(args: argparse.Namespace, cmd: str, *, must_exist: bool) -> tuple[Optional[str], int]:
    """Normalize ``args.slug`` for a ``boards`` subcommand; ``(slug, 0)`` or ``(None, rc)``."""
    try:
        normed = kb._normalize_board_slug(args.slug)
    except ValueError as exc:
        return None, _err(f"kanban boards {cmd}: {exc}", 2)
    if must_exist:
        if not normed or not kb.board_exists(normed):
            return None, _err(f"kanban boards {cmd}: board {args.slug!r} does not exist")
    elif not normed:
        return None, _err(f"kanban boards {cmd}: slug is required", 2)
    return normed, 0


def _cmd_boards_list(args: argparse.Namespace) -> int:
    boards = kb.list_boards(include_archived=bool(getattr(args, "all", False)))
    current = kb.get_current_board()
    for b in boards:
        b["is_current"] = (b["slug"] == current)
        b["counts"] = _board_task_counts(b["slug"])
        b["total"] = sum(b["counts"].values())
    if _json_out(args, boards):
        return 0
    if not boards:
        print("(no boards — create one with `hermes kanban boards create <slug>`)")
        return 0
    print(f"{'':2s}  {'SLUG':24s}  {'NAME':28s}  COUNTS")
    for b in boards:
        marker = "●" if b["is_current"] else " "
        name = (b.get("name") or "") + (" [archived]" if b.get("archived") else "")
        print(f"{marker:2s}  {b['slug']:24s}  {name:28s}  {_fmt_counts(b['counts'] or {}, '(empty)')}")
    print(f"\nCurrent board: {current}")
    if len(boards) > 1:
        print("Switch boards with `hermes kanban boards switch <slug>`.")
    return 0


def _cmd_boards_create(args: argparse.Namespace) -> int:
    normed, rc = _board_slug_arg(args, "create", must_exist=False)
    if rc:
        return rc
    already = kb.board_exists(normed) and normed != kb.DEFAULT_BOARD
    meta = kb.create_board(
        normed, name=args.name, description=args.description, icon=args.icon, color=args.color,
        default_workdir=args.default_workdir,
        dispatch=False if getattr(args, "no_dispatch", False) else None,
    )
    print(f"Board {meta['slug']!r} {'already exists' if already else 'created'}.\n"
          f"  Display name: {meta.get('name', '')}\n"
          f"  DB path:      {meta['db_path']}")
    if meta.get("dispatch", True) is False:
        print("  Dispatch:     DISABLED — this is an estate/scratch board; the "
              "dispatcher will not serve it (safe by construction).")
    if getattr(args, "switch", False):
        kb.set_current_board(meta["slug"])
        print(f"  Switched to {meta['slug']!r}.")
    else:
        print(f"  Use `hermes kanban boards switch {meta['slug']}` to make it current.")
    return 0


def _cmd_boards_rm(args: argparse.Namespace) -> int:
    # `boards delete <slug>` (alias) never sets args.delete because --delete belongs to the 'rm'
    # subparser only; treat the alias as `rm --delete`.
    # See #23139.
    force_delete = getattr(args, "delete", False) or getattr(args, "boards_action", "") == "delete"
    # Capture the estate declaration BEFORE the directory moves, so the handler can report which
    # record the single-actor teardown left behind (ruling t_fcf7a321, Decision 2).
    estate_declared = False
    if not force_delete:
        try:
            from hermes_cli import kanban_bulk_guard as kbg

            estate_declared = kbg._estate_state(str(args.slug or ""))[0]
        except Exception:
            estate_declared = False
    try:
        res = kb.remove_board(args.slug, archive=not force_delete)
    except ValueError as exc:
        return _err(f"kanban boards rm: {exc}")
    if res["action"] == "archived":
        print(f"Board {res['slug']!r} archived → {res['new_path']}\n"
              "Recover by moving the directory back to <root>/kanban/boards/<slug>/ "
              "and deleting the archived tombstone board.json left in its place.")
        if estate_declared:
            from hermes_cli import kanban_bulk_guard as kbg

            print(f"Estate teardown recorded (actor + reason + verified preimage): "
                  f"{kbg.ledger_path()} · {kbg.audit_path()}")
    else:
        print(f"Board {res['slug']!r} deleted.")
    return 0


def _cmd_boards_switch(args: argparse.Namespace) -> int:
    normed, rc = _board_slug_arg(args, "switch", must_exist=False)
    if rc:
        return rc
    if not kb.board_exists(normed):
        return _err(
            f"kanban boards switch: board {normed!r} does not exist. "
            f"Create it with `hermes kanban boards create {normed}`."
        )
    # An archived board has a tombstone board.json; switching to it would pin
    # `current` at a dead slug (#43243).
    if kb.read_board_metadata(normed).get("archived"):
        return _err(f"kanban boards switch: board {normed!r} is archived.")
    kb.set_current_board(normed)
    print(f"Active board is now {normed!r}.")
    return 0


def _cmd_boards_show(args: argparse.Namespace) -> int:
    # ``show`` takes an optional slug and defaults to the current board, which is what the
    # ``boards current`` alias has always meant.
    args.slug = args.slug or kb.get_current_board()
    normed, rc = _board_slug_arg(args, "show", must_exist=True)
    if rc:
        return rc
    slug = str(normed)
    meta = kb.read_board_metadata(slug)
    counts = _board_task_counts(slug)
    try:
        policy = kb.board_priority_policy(slug)
    except Exception as exc:
        # Reportable, not fatal: a malformed spec is exactly what a reader needs to see,
        # and ``create_task`` is where it refuses cards.
        policy = None
        malformed = str(exc)
    else:
        malformed = ""
    if _json_out(args, {
        "slug": slug,
        "name": meta.get("name", ""),
        "description": meta.get("description", ""),
        "db_path": meta["db_path"],
        "counts": counts,
        "tasks": sum(counts.values()),
        "priority_policy": policy,
        "priority_policy_error": malformed or None,
        "operator_register": meta.get("operator_register"),
        "dispatch": bool(meta.get("dispatch", True)),
    }):
        return 0
    print(f"Board: {slug}" + ("  (current)" if slug == kb.get_current_board() else ""))
    print(f"  Display name: {meta.get('name', '')}")
    if meta.get("description"):
        print(f"  Description:  {meta['description']}")
    print(f"  DB path:      {meta['db_path']}\n"
          f"  Tasks:        {sum(counts.values())} total" + (f" ({_fmt_counts(counts)})" if counts else ""))
    if malformed:
        print("  Priority policy: CONFIGURED BUT UNUSABLE - filings on this board fail:")
        print(f"    {malformed}")
    else:
        print(f"  Priority policy: {_describe_priority_policy(policy)}")
    register = meta.get("operator_register")
    print(f"  Operator register: {register or 'not set'}"
          + ("  (`hermes kanban rollup`)" if register else ""))
    if meta.get("dispatch", True) is False:
        print("  Dispatch:     DISABLED — estate/scratch board; the dispatcher "
              "will not serve it.")
    return 0


def _cmd_boards_set_operator_register(args: argparse.Namespace) -> int:
    """Point a board at its operator-ask register (or clear it with no argument)."""
    normed, rc = _board_slug_arg(args, "set-operator-register", must_exist=True)
    if rc:
        return rc
    from hermes_cli import kanban_register as kr

    task_id = (getattr(args, "task_id", None) or "").strip()
    try:
        # Validated on the way in (the writer's job) and reported against the cards that
        # exist, because the value is a REFERENCE: a typo here would silently orphan the
        # whole tree somebody files next, and the person wiring it is the only one who can
        # fix it cheaply. Nothing is written on refusal.
        meta = kb.write_board_metadata(normed, operator_register=task_id)
    except ValueError as exc:
        return _err(f"kanban boards set-operator-register: {exc}", 2)
    register = meta.get(kr.META_KEY)
    if not register:
        if _json_out(args, {"board": normed, "operator_register": None}):
            return 0
        print(f"Board {normed!r} operator register cleared.\n"
              f"  `hermes kanban rollup` on this board now needs a register id.")
        return 0
    holder = kr.find_card_board(register)
    if _json_out(args, {"board": normed, "operator_register": register, "register_board": holder}):
        return 0
    print(f"Board {normed!r} operator register set to {register}.")
    if holder:
        print(f"  The register card lives on board {holder!r} "
              "(a reference crosses boards; a parent edge cannot).")
        print(f"  Roll it up with: hermes kanban rollup {register}")
    else:
        print(f"  ⚠ no card {register} on any board: cards are still stamped with it, "
              "and the roll-up will report it as an unresolved reference until it exists.")
    return 0


def _cmd_boards_rename(args: argparse.Namespace) -> int:
    normed, rc = _board_slug_arg(args, "rename", must_exist=True)
    if rc:
        return rc
    meta = kb.write_board_metadata(normed, name=args.name)
    print(f"Board {normed!r} renamed to {meta['name']!r}.")
    return 0


def _cmd_boards_set_default_workdir(args: argparse.Namespace) -> int:
    normed, rc = _board_slug_arg(args, "set-default-workdir", must_exist=True)
    if rc:
        return rc
    new_val = kb.write_board_metadata(normed, default_workdir=args.path).get("default_workdir")
    if new_val:
        print(f"Board {normed!r} default workdir set to {new_val!r}.")
    else:
        print(f"Board {normed!r} default workdir cleared.")
    return 0


def _cmd_boards_set_dispatch(args: argparse.Namespace) -> int:
    """Admit or exclude a board from the dispatcher's set.

    ``off`` marks an ESTATE/scratch board: the dispatcher's own enumeration
    (:func:`kanban_db.list_dispatch_boards`) drops it and its per-tick spawn guard
    refuses it, so a card filed there can never become lane work — which is what
    redirecting a SEV1 to a "scratch" board actually required (card t_17c9c847).
    """
    normed, rc = _board_slug_arg(args, "set-dispatch", must_exist=True)
    if rc:
        return rc
    state = str(getattr(args, "state", "") or "").strip().lower()
    if state not in ("on", "off"):
        return _err("kanban boards set-dispatch: state must be 'on' or 'off'", 2)
    enabled = state == "on"
    meta = kb.write_board_metadata(normed, dispatch=enabled)
    admitted = bool(meta.get("dispatch", True))
    if _json_out(args, {"board": normed, "dispatch": admitted}):
        return 0
    if enabled:
        print(f"Board {normed!r}: dispatch ENABLED — the dispatcher serves it again.")
    else:
        print(f"Board {normed!r}: dispatch DISABLED — the dispatcher will not serve it "
              f"(estate/scratch board, out of the dispatch set by construction).")
    return 0


def _describe_priority_policy(spec: Optional[dict]) -> str:
    if not spec:
        return "none (cards keep the priority their filer asks for)"
    return f"{spec['module']} :: {spec['function']}()"


def _guard_line(state: str, kpp) -> str:
    """One line describing the board's tranche storage guard, for the wiring output."""
    if state == "armed":
        return ("armed — a reserved-tranche priority (%d-%d) is refused by the database "
                "unless the card carries a live designation"
                % (kpp.TRANCHE_FLOOR, kpp.TRANCHE_TOP))
    return "unarmed — no policy is wired, so nothing bounds a priority at the storage layer"


def _cmd_boards_set_priority_policy(args: argparse.Namespace) -> int:
    normed, rc = _board_slug_arg(args, "set-priority-policy", must_exist=True)
    if rc:
        return rc
    from hermes_cli import kanban_priority_policy as kpp

    if not args.module:
        kb.write_board_metadata(normed, priority_policy="")
        # Door 3 follows the key: clearing the policy disarms the storage guard, so a cleared
        # board is byte-identical to one that never carried a policy.
        guard = kb.sync_priority_tranche_guard(normed)
        if _json_out(args, {"board": normed, "priority_policy": "", "storage_guard": guard}):
            return 0
        print(f"Board {normed!r} priority policy cleared.\n"
              f"  Cards keep the priority their filer asks for.\n"
              f"  Storage guard: {_guard_line(guard, kpp)}")
        return 0
    spec = {"module": args.module, "function": args.function or kpp.DEFAULT_FUNCTION}
    try:
        # Validate by LOADING it: the CLI runs where the wiring is being written, so a
        # typo'd path or a missing callable is refused here, once, instead of failing
        # every filing on the board afterwards. Nothing is written on refusal.
        normalised = kpp.normalize_spec(spec) or spec
        kpp.load_callable(normalised)
        # The board's FLOOR is validated the same way and for the same reason: it is BAKED
        # into the armed storage guard, so a floor that cannot be read has to be refused while
        # someone is looking at the wiring rather than at the first write afterwards. A policy
        # with no ``band_floor`` answers None - the no-floor case, which is not an error.
        kpp.board_floor(normalised, normed or "")
    except kpp.PolicyError as exc:
        return _err(f"kanban boards set-priority-policy: {exc} — nothing written", 2)
    kb.write_board_metadata(normed, priority_policy=normalised)
    guard = kb.sync_priority_tranche_guard(normed)
    if _json_out(args, {"board": normed, "priority_policy": normalised, "storage_guard": guard}):
        return 0
    print(f"Board {normed!r} priority policy set to "
          f"{normalised['module']} :: {normalised['function']}().\n"
          f"  Cards filed on this board are born in the band it assigns them; a policy "
          f"that cannot be used fails the filing rather than landing an unbanned card.\n"
          f"  Storage guard: {_guard_line(guard, kpp)}")
    return 0


def _cmd_boards_export(args: argparse.Namespace) -> int:
    from hermes_cli import kanban_transfer
    from hermes_cli.sizefmt import format_bytes

    slug = args.slug or kb.get_current_board()
    output = args.output or f"{slug}.tar.gz"
    try:
        res = kanban_transfer.export_board(
            slug, output, include_attachments=not args.no_attachments, include_logs=args.include_logs,
        )
    except (OSError, ValueError) as exc:
        return _err(f"kanban boards export: {exc}")
    if _json_out(args, res):
        return 0
    counts = res["counts"]
    print(f"Exported board {res['board']!r} → {res['archive']}\n"
          f"  Size:        {format_bytes(res['size'])}\n"
          f"  Tasks:       {counts['tasks']}\n"
          f"  Comments:    {counts['task_comments']}\n"
          f"  Attachments: {counts['attachment_files']}\n"
          "Import it with `hermes kanban boards import <archive>`.")
    return 0


def _cmd_boards_import(args: argparse.Namespace) -> int:
    from hermes_cli import kanban_transfer

    try:
        res = kanban_transfer.import_board(args.archive, args.as_slug, activate=args.switch,
                                           approval=getattr(args, "approval", "") or "")
    except (OSError, ValueError) as exc:
        return _err(f"kanban boards import: {exc}")
    if _json_out(args, res):
        return 0
    print(f"Imported board {res['board']!r} ({res['name']}).")
    if res["renamed"]:
        print(f"  Renamed from {res['requested_board']!r} — that slug was taken.")
    print(f"  Path:  {res['path']}\n  Tasks: {res['counts']['tasks']}")
    for warning in res["warnings"]:
        print(f"  Note:  {warning}")
    if res["activated"]:
        print(f"  Active board is now {res['board']!r}.")
    else:
        print(f"  Switch to it with `hermes kanban boards switch {res['board']}`.")
    return 0


_BOARD_HANDLERS = {
    "list": _cmd_boards_list, "ls": _cmd_boards_list,
    "create": _cmd_boards_create, "new": _cmd_boards_create,
    "rm": _cmd_boards_rm, "remove": _cmd_boards_rm, "delete": _cmd_boards_rm,
    "switch": _cmd_boards_switch, "use": _cmd_boards_switch,
    "show": _cmd_boards_show, "current": _cmd_boards_show,
    "rename": _cmd_boards_rename,
    "set-default-workdir": _cmd_boards_set_default_workdir,
    "set-dispatch": _cmd_boards_set_dispatch,
    "set-priority-policy": _cmd_boards_set_priority_policy,
    "set-operator-register": _cmd_boards_set_operator_register,
    "export": _cmd_boards_export,
    "import": _cmd_boards_import,
}
