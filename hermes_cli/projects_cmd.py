"""``hermes project`` CLI — manage first-class, multi-folder Projects."""

from __future__ import annotations

import argparse
import functools
import sys
from typing import Optional

from hermes_cli import projects_db as pdb


def build_parser(parent_subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Attach the ``project`` subcommand tree. Returns the top parser."""
    parser = parent_subparsers.add_parser(
        "project",
        help="Manage projects (named, multi-folder workspaces)",
        description=(
            "Projects are human-named workspaces that can span multiple "
            "folders / repos. They anchor desktop session grouping and, when "
            "bound to a kanban board, give tasks a deterministic worktree + "
            "branch convention. State is per-profile."
        ),
    )
    sub = parser.add_subparsers(dest="project_action")
    p_create = sub.add_parser("create", help="Create a new project")
    p_create.add_argument("name", help="Human name, e.g. 'Hermes Agent'")
    p_create.add_argument("folders", nargs="*", help="Folder paths to include (first = primary)")
    p_create.add_argument("--slug", default=None, help="Explicit slug override")
    p_create.add_argument("--primary", default=None, metavar="PATH", help="Primary repo path")
    for opt in ("--description", "--icon", "--color"):
        p_create.add_argument(opt, default=None)
    p_create.add_argument("--board", default=None, metavar="SLUG", help="Bind a kanban board")
    p_create.add_argument("--use", action="store_true", help="Set as the active project")
    p_list = sub.add_parser("list", aliases=["ls"], help="List projects")
    p_list.add_argument("--all", action="store_true", dest="include_archived", help="Include archived projects")

    def project_sub(name: str, help: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help)
        sp.add_argument("project", help="Project id or slug")
        return sp

    project_sub("show", "Show a project's details")
    p_add = project_sub("add-folder", "Add a folder to a project")
    p_add.add_argument("path", help="Folder path")
    p_add.add_argument("--label", default=None)
    p_add.add_argument("--primary", action="store_true", help="Mark as primary repo")
    project_sub("remove-folder", "Remove a folder from a project").add_argument("path", help="Folder path")
    project_sub("rename", "Rename a project").add_argument("name", help="New name")
    project_sub("set-primary", "Set the primary folder").add_argument("path", help="Folder path (must already be in project)")
    project_sub("set-about", "Set the project's about (one-line description)").add_argument(
        "text", help="New about text, stored verbatim (pass \"\" to clear it)"
    )
    p_use = sub.add_parser("use", help="Set the active project")
    p_use.add_argument("project", nargs="?", default=None, help="Project id or slug (omit to clear)")
    project_sub("archive", "Archive a project")
    project_sub("restore", "Restore an archived project")
    project_sub("bind-board", "Bind a kanban board to a project").add_argument(
        "board", nargs="?", default="", help="Board slug (omit to unbind)"
    )
    parser.set_defaults(_project_parser=parser)
    return parser


def projects_command(args: argparse.Namespace) -> int:
    """Entry point from ``hermes project …`` argparse dispatch."""
    action = getattr(args, "project_action", None)
    if not action:
        parser = getattr(args, "_project_parser", None)
        if parser is not None:
            parser.print_help()
        else:
            print("usage: hermes project <action> [options]\nRun 'hermes project --help' for the full list.", file=sys.stderr)
        return 0
    handler = _HANDLERS.get(action)
    if handler is None:
        print(f"Unknown project action: {action}", file=sys.stderr)
        return 1
    return handler(args)


def _err(message: str) -> int:
    print(f"project: {message}", file=sys.stderr)
    return 1


def _resolve(conn, ident: str):
    proj = pdb.get_project(conn, ident)
    if proj is None:
        _err(f"no such project: {ident}")
    return proj


def _db_command(fn):
    """Open the DB and run ``fn(args, conn)``; a ``str`` result is printed (rc 0), an ``int`` is the rc;
    a ``ValueError`` prints ``project: …`` and exits 2."""

    @functools.wraps(fn)
    def wrapper(args: argparse.Namespace) -> int:
        try:
            with pdb.connect_closing() as conn:
                out = fn(args, conn)
        except ValueError as exc:
            print(f"project: {exc}", file=sys.stderr)
            return 2
        if isinstance(out, str):
            print(out)
            return 0
        return out

    return wrapper


def _with_project(fn):
    """Like ``_db_command`` but also resolves ``args.project`` into ``fn(args, conn, proj)``."""

    @functools.wraps(fn)
    def wrapper(args: argparse.Namespace, conn):
        proj = _resolve(conn, args.project)
        return 1 if proj is None else fn(args, conn, proj)

    return _db_command(wrapper)


def _print_project(proj) -> None:
    print(f"{proj.slug}  [{proj.id}]{' (archived)' if proj.archived else ''}")
    print(f"  name:    {proj.name}")
    for label, value in (("about", proj.description), ("board", proj.board_slug), ("primary", proj.primary_path)):
        if value:
            print(f"  {label}:{' ' * (8 - len(label))}{value}")
    if proj.folders:
        print("  folders:")
        for f in proj.folders:
            print(f"   {' *' if f.is_primary else '  '} {f.path}{f' ({f.label})' if f.label else ''}")


@_db_command
def _cmd_create(args, conn) -> int:
    pid = pdb.create_project(
        conn, name=args.name, slug=args.slug, folders=args.folders, primary_path=args.primary,
        description=args.description, icon=args.icon, color=args.color, board_slug=args.board,
    )
    if args.use:
        pdb.set_active(conn, pid)
    proj = pdb.get_project(conn, pid)
    if proj is None:
        print("project: vanished after create", file=sys.stderr)
        return 2
    print(f"Created project {proj.slug} ({pid})")
    _print_project(proj)
    return 0


@_db_command
def _cmd_list(args, conn):
    active = pdb.get_active_id(conn)
    projs = pdb.list_projects(conn, include_archived=getattr(args, "include_archived", False))
    if not projs:
        return "No projects yet. Create one with `hermes project create <name>`."
    for p in projs:
        flags = " (archived)" if p.archived else ""
        print(f"{'*' if p.id == active else ' '} {p.slug:<24} {p.name}{flags}  [{len(p.folders)} folder(s)]")
    return 0


@_with_project
def _cmd_show(args, conn, proj) -> int:
    _print_project(proj)
    return 0


@_with_project
def _cmd_add_folder(args, conn, proj) -> str:
    path = pdb.add_folder(conn, proj.id, args.path, label=args.label, is_primary=args.primary)
    return f"Added {path} to {proj.slug}"


@_with_project
def _cmd_remove_folder(args, conn, proj):
    if not pdb.remove_folder(conn, proj.id, args.path):
        return _err(f"folder not in project: {args.path}")
    return f"Removed {args.path} from {proj.slug}"


@_with_project
def _cmd_rename(args, conn, proj) -> str:
    pdb.update_project(conn, proj.id, name=args.name)
    return f"Renamed {proj.slug} -> {args.name}"


@_with_project
def _cmd_set_primary(args, conn, proj):
    if not pdb.set_primary(conn, proj.id, args.path):
        return _err(f"'{args.path}' is not a folder of {proj.slug}; add it first with `hermes project add-folder`.")
    return f"Set primary of {proj.slug} -> {args.path}"


@_with_project
def _cmd_set_about(args, conn, proj) -> str:
    """``hermes project set-about <project> <text>`` — the post-create about editor.

    Stored verbatim (no strip, no rewriting) so ``show`` round-trips byte-exactly; ``""`` is
    legal and clears it, which makes ``_print_project`` drop the ``about:`` line.
    """
    pdb.update_project(conn, proj.id, description=args.text)
    return f"Set about of {proj.slug}"


@_db_command
def _cmd_use(args, conn):
    if not args.project:
        pdb.set_active(conn, None)
        return "Cleared active project"
    proj = _resolve(conn, args.project)
    if proj is None:
        return 1
    pdb.set_active(conn, proj.id)
    return f"Active project: {proj.slug}"


def _flag_command(op: str, verb: str):
    """Handler for ``pdb.<op>(conn, proj.id)`` followed by ``"<verb> <slug>"``."""
    return _with_project(lambda args, conn, proj: (getattr(pdb, op)(conn, proj.id), f"{verb} {proj.slug}")[1])


def _board_meta_writer(board: str):
    """``(slug, write_board_metadata)`` for a board that exists, else ``(None, None)``.

    Never raises: ``bind-board`` is a convenience for the human filing a workstream, so a
    board slug that matches nothing must not fail the project write (it stays fail-soft,
    exactly like the ``default_workdir`` write this seam already made).
    """
    try:
        from hermes_cli import kanban_db as kb

        slug = kb._normalize_board_slug(board)
        if slug and (slug == kb.DEFAULT_BOARD or kb.board_exists(slug)):
            return slug, kb.write_board_metadata
    except Exception:
        pass
    return None, None


def _board_project_write(board: str, **fields) -> Optional[str]:
    """Write ``board``'s metadata; return a refusal message, or ``None`` on success/skip.

    Fail-soft is the ruling for a board that does not RESOLVE (``_board_meta_writer`` gives
    ``(None, None)``): the project row is what the caller asked for and it must not fail on the
    board convenience. A REFUSAL is not a missing board. ``write_board_metadata`` calls
    ``_assert_not_delegated_child_mutation``, which refuses a board write from a session
    descended from a live dispatched worker — and swallowing that made ``bind-board`` print
    ``Bound … -> board …`` while nothing landed (measured 2026-10-13 from a dispatched worker's
    own session, i.e. the very lane that files workstream cards): a false green of exactly the
    class this card closes. A refusal comes back as a message the caller reports.
    """
    slug, write = _board_meta_writer(board)
    if write is None:
        return None
    try:
        write(slug, **fields)
    except PermissionError as exc:
        return str(exc) or "the board write was refused"
    except Exception:
        return None
    return None


def _clear_board_project(board: str, proj) -> Optional[str]:
    """Fail-soft: drop ``board``'s default project when it points at ``proj``."""
    slug, _write = _board_meta_writer(board)
    if slug is None:
        return None
    try:
        from hermes_cli import kanban_db as kb

        current = (kb.read_board_metadata(slug).get("project_id") or "").strip()
    except Exception:
        return None
    if current and current in {proj.slug, proj.id}:
        return _board_project_write(slug, project_id="")
    return None


def _board_refusal(proj, board: str, refusal: str) -> int:
    """The project row landed, the board link did not: say so and exit non-zero."""
    return _err(
        f"{proj.slug} -> board {board}: the project row was updated, but the board's "
        f"default project did NOT land \u2014 {refusal}"
    )


@_with_project
def _cmd_bind_board(args, conn, proj) -> int | str:
    """``hermes project bind-board <project> [board]`` — the board half of the link.

    Binding writes the board's DEFAULT PROJECT (``board.json:project_id``) beside the
    ``default_workdir`` it already wrote. ``create_task`` reads that key to scope every new
    task on the board to the project (deterministic worktree + branch), so "the project
    groups the cards" no longer rests on every creator remembering ``--project``. The value
    stored is the project's **slug**, never its ``p_…`` id: a slug is stable across the
    per-profile registries (``$HERMES_HOME/projects.db``), an id is not. Unbinding (no board
    argument) clears the key again. The board writes stay fail-soft for a board that does not
    resolve, but a REFUSED write is reported rather than dressed as success.
    """
    previous_board = (proj.board_slug or "").strip()
    board = (args.board or "").strip()
    pdb.update_project(conn, proj.id, board_slug=args.board)
    if not board:
        refusal = _clear_board_project(previous_board, proj)
        if refusal:
            return _board_refusal(proj, previous_board, refusal)
        return f"Unbound board from {proj.slug}"
    refusal = _board_project_write(
        board, default_workdir=proj.primary_path, project_id=proj.slug,
    )
    if previous_board and previous_board != board:
        _clear_board_project(previous_board, proj)
    if refusal:
        return _board_refusal(proj, board, refusal)
    return f"Bound {proj.slug} -> board {board}"


_HANDLERS = {
    "create": _cmd_create,
    "list": _cmd_list,
    "ls": _cmd_list,
    "show": _cmd_show,
    "add-folder": _cmd_add_folder,
    "remove-folder": _cmd_remove_folder,
    "rename": _cmd_rename,
    "set-primary": _cmd_set_primary,
    "set-about": _cmd_set_about,
    "use": _cmd_use,
    "archive": _flag_command("archive_project", "Archived"),
    "restore": _flag_command("restore_project", "Restored"),
    "bind-board": _cmd_bind_board,
}
