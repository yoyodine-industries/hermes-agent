"""``hermes kanban boards …`` — board directories, the ``current`` pointer and ``board.json``.
Filesystem-only, so every action works before ``kanban init`` and must ignore the shared
``--board`` task-routing override.
"""

from __future__ import annotations

import argparse
from typing import Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_output import (
    BOARD_PIN_REMEDY, _err, _fmt_counts, _json_out,
)


def _dispatch_boards(args: argparse.Namespace) -> int:
    """``hermes kanban boards <action>`` — filesystem-only, so it works before ``kanban init``."""
    sub = getattr(args, "boards_action", None) or "list"
    handler = _BOARD_HANDLERS.get(sub)
    if handler is None:
        return _err(f"kanban boards: unknown action {sub!r}", 2)
    return handler(args)


def _board_task_counts(slug: str) -> dict[str, int]:
    """``{status: count}`` for a board's OWN store. Safe to call on an empty DB.

    The store is resolved from the SLUG (:func:`kanban_db.board_store_path`), never from
    :func:`kanban_db.kanban_db_path`: under a board pin (``HERMES_KANBAN_DB``) the latter
    answers the PINNED file for every slug, so counts printed under another board's slug were
    really the pinned board's — the same numbers on every line of ``boards`` (measured
    2026-10-27, card t_398313ec).

    The row's own store is also exposed beside these counts as ``db_path``, resolved the same
    way from the row's slug (card t_5723473b): the pin decides what a process may READ, never
    what a board's store IS, so an inventory names each row's own directory.
    """
    try:
        path = kb.board_store_path(slug)
        if not path.exists():
            return {}
        # `board=slug` rides along for the dead-board guard only; the path is the
        # resolved one, so the pin cannot retarget the read.
        with kbc.connect_closing(db_path=path, board=slug) as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall()
        return {r["status"]: int(r["n"]) for r in rows}
    except Exception:
        return {}


def _pin_note() -> str:
    """One line explaining which store a pinned ``boards`` listing is allowed to read."""
    pinned_slug, pin_var = kb.board_env_pin()
    where = f"board {pinned_slug!r}" if pinned_slug else "a store that names no board"
    return (
        f"Counts: this process is pinned to {where} ({pin_var}), so task counts are printed "
        "only for that board.\n"
        "Per-board counts: env -u HERMES_KANBAN_DB -u HERMES_KANBAN_BOARD hermes kanban boards"
    )


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
    fenced = kb._explicit_board_intent_pinned()
    pinned_slug, pin_var = kb.board_env_pin()
    for b in boards:
        b["is_current"] = (b["slug"] == current)
        # Where the pin OVERRIDES intent, a pinned process must not READ another board's store
        # (the pin is that isolation), and must not print this one's counts under another
        # board's slug either. Blank the counts for every board the process is not pinned to,
        # so a count is only ever shown beside the store it was taken from (measured
        # 2026-10-27, card t_398313ec). Where it does not override intent — the operator's own
        # selected board — every slug still resolves to its own store, so per-slug counts are
        # exactly right and stay as they were.
        readable = not (fenced and pin_var) or b["slug"] == pinned_slug
        counts = _board_task_counts(b["slug"]) if readable else None
        b["counts"] = counts
        b["total"] = sum(counts.values()) if counts is not None else None
        # The ROW's own store, resolved from its own slug (card t_5723473b): the pin decides
        # what this process may READ, above — it never relabels where a board's store IS, so
        # an inventory renders each row against its own directory. The row names a path; it
        # opens nothing, which is why the pin guard above does not gate this field.
        b["db_path"] = str(kb.board_store_path(b["slug"]))
    if _json_out(args, boards):
        return 0
    if not boards:
        print("(no boards — create one with `hermes kanban boards create <slug>`)")
        return 0
    print(f"{'':2s}  {'SLUG':24s}  {'NAME':28s}  COUNTS")
    for b in boards:
        marker = "●" if b["is_current"] else " "
        name = (b.get("name") or "") + (" [archived]" if b.get("archived") else "")
        cell = (_fmt_counts(b["counts"] or {}, "(empty)") if b["counts"] is not None
                else "(not read — pinned)")
        print(f"{marker:2s}  {b['slug']:24s}  {name:28s}  {cell}")
    print(f"\nCurrent board: {current}")
    if len(boards) > 1:
        print("Switch boards with `hermes kanban boards switch <slug>`.")
    if fenced and pin_var:
        print(f"\n{_pin_note()}")
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
        except Exception:  # health: allow BLE001 -- fail-open (t_fcf7a321): an unreadable estate declaration reports no estate record; the teardown is unchanged
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
    requested = getattr(args, "slug", None)
    fenced = kb._explicit_board_intent_pinned()
    pinned_slug, pin_var = kb.board_env_pin()
    if requested:
        # Same fence as `--board` (card t_398313ec): this handler ignores `--board` by
        # construction, but it prints a STORE (`DB path` + task counts) beside a board's
        # metadata. Under a pin that OVERRIDES intent the store is the pinned one, so
        # `boards show <other>` used to render <other>'s description and filename over the
        # pinned board's path and counts. A display that mixes two boards is worse than an
        # error: refuse instead. (`assert_board_matches_env_pin` is a no-op where the pin
        # does not override intent, and there the per-slug read below is already right.)
        try:
            kb.assert_board_matches_env_pin(requested)
        except ValueError as exc:
            return _err(f"kanban boards show: {exc}\n{BOARD_PIN_REMEDY}")
    elif fenced and pin_var and not pinned_slug:
        return _err(
            "kanban boards show: this process is pinned to a store that names no board "
            f"({pin_var}=…), so there is no board whose metadata matches it; "
            "run from a top-level session or clear the pin "
            "(`env -u HERMES_KANBAN_DB -u HERMES_KANBAN_BOARD hermes kanban boards show <slug>`)"
        )
    # No slug named: where the pin OVERRIDES intent, report the board this process is actually
    # pinned to, not the ambient `current` pointer — those can be different boards, and showing
    # the pointer's metadata beside the pin's store is the same mixed display. Where it does
    # not (an operator's own selected board), the pointer is still the answer, as before.
    slug = requested or (pinned_slug if fenced else None) or kb.get_current_board()
    meta = kb.read_board_metadata(slug)
    counts = _board_task_counts(slug)
    if _json_out(args, meta):
        return 0
    label = "Current board" if getattr(args, "slug", None) is None else "Board"
    print(f"{label}: {slug}\n  Display name: {meta.get('name', '')}")
    if meta.get("description"):
        print(f"  Description:  {meta['description']}")
    print(f"  DB path:      {meta['db_path']}\n"
          f"  Tasks:        {sum(counts.values())} total" + (f" ({_fmt_counts(counts)})" if counts else ""))
    if meta.get("dispatch", True) is False:
        print("  Dispatch:     DISABLED — estate/scratch board; the dispatcher "
              "will not serve it.")
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


def _cmd_boards_set_project(args: argparse.Namespace) -> int:
    """``hermes kanban boards set-project <board> <project|--clear>``.

    The direct writer for a board's DEFAULT PROJECT (``board.json:project_id``) — the same
    seam ``hermes project bind-board`` uses, so a board can be scoped without a project
    round-trip. ``create_task`` reads that key and scopes every new task on the board to the
    project (deterministic worktree + branch under its primary repo).

    The project must exist in **this profile's** registry (``$HERMES_HOME/projects.db``): a
    project written by another profile is invisible here, and a board default this profile
    cannot resolve scopes nothing. The stored value is the project's **slug** — a slug is
    stable across the per-profile registries, an id is not.
    """
    normed, rc = _board_slug_arg(args, "set-project", must_exist=True)
    if rc:
        return rc
    ident = (getattr(args, "project", None) or "").strip()
    clearing = bool(getattr(args, "clear", False))
    if clearing and ident:
        return _err("kanban boards set-project: pass a project or --clear, not both", 2)
    if clearing or not ident:
        kb.write_board_metadata(normed, project_id="")
        print(f"Board {normed!r}: default project cleared.")
        return 0
    from hermes_cli import projects_db as pdb

    with pdb.connect_closing() as conn:
        proj = pdb.get_project(conn, ident)
    if proj is None:
        return _err(
            f"kanban boards set-project: no such project {ident!r} in this profile's registry "
            f"({pdb.projects_db_path()}) — a project created by another profile is not visible "
            f"here. Create it with `hermes project create <name> --slug {ident}`, or pass a "
            f"slug this profile knows (`hermes project list`).", 2,
        )
    kb.write_board_metadata(normed, project_id=proj.slug)
    print(f"Board {normed!r}: default project {proj.slug!r} — new tasks on it inherit the "
          f"project (deterministic worktree + branch).")
    if not proj.primary_path:
        print("  note: the project has no primary folder, so tasks are grouped but keep "
              "the workspace they asked for.")
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


def _cmd_boards_set_priority_policy(args: argparse.Namespace) -> int:
    """Wire (or clear) a board's birth-time priority policy, and sync its storage guard.

    The WRITING half of the birth seam (card t_2187558e, REQUIRED 2). Three things happen
    here and nowhere else:

    * the module is VALIDATED by LOADING it (``load_callable``) and by READING the board's
      floor through it (``board_floor``). A policy that cannot be loaded, or whose floor
      cannot be read, is refused with *nothing written* - the writer stores what it is
      given, so this verb is the surface that has to refuse, and a refusal must leave no
      half-wired board behind;
    * the key is written through ``write_board_metadata(..., priority_policy=...)``, storing
      the NORMALISED spec so ``board.json`` holds the canonical shape (``{module, function}``);
    * the storage guard is synced to the key (``sync_priority_tranche_guard``) — armed for a
      wired board, dropped for an unwired one — so the guard can never drift from the key
      that turns the domain on. The floor is baked into the DDL at this moment.
    """
    from hermes_cli import kanban_priority_policy as kpp

    normed, rc = _board_slug_arg(args, "set-priority-policy", must_exist=True)
    if rc:
        return rc
    module = (getattr(args, "module", None) or "").strip()
    spec = None
    if module:
        try:
            spec = kpp.normalize_spec({
                "module": module,
                "function": (getattr(args, "function", None) or "").strip() or kpp.DEFAULT_FUNCTION,
            })
            kpp.load_callable(spec)                      # the callable must LOAD
            kpp.board_floor(spec, normed)                # and its floor must be READABLE
        except Exception as exc:
            return _err(f"kanban boards set-priority-policy: {exc} — nothing written", 2)
    try:
        kb.write_board_metadata(normed, priority_policy=spec if module else "")
        state = kb.sync_priority_tranche_guard(normed)
    except Exception as exc:
        return _err(f"kanban boards set-priority-policy: {exc} — nothing written", 2)
    payload = {
        "board": normed,
        "priority_policy": kb.board_priority_policy(normed),
        "storage_guard": state,
    }
    if _json_out(args, payload):
        return 0
    if module:
        print(f"Board {normed!r}: priority policy set to "
              f"{payload['priority_policy']['module']!r}"
              f"::{payload['priority_policy']['function']}.\n"
              f"  Birth priority is that policy's call from now on; the caller's value is "
              f"the ASK.\n"
              f"  Storage guard: {state}.")
    else:
        print(f"Board {normed!r}: priority policy cleared.\n"
              f"  Storage guard: {state} — the board behaves exactly as it did before it "
              f"ever opted in.")
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
        res = kanban_transfer.import_board(args.archive, args.as_slug, activate=args.switch)
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
    "set-project": _cmd_boards_set_project,
    "set-dispatch": _cmd_boards_set_dispatch,
    "set-priority-policy": _cmd_boards_set_priority_policy,
    "export": _cmd_boards_export,
    "import": _cmd_boards_import,
}
