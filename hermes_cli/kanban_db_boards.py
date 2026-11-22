"""Board metadata (``board.json``) and board lifecycle management for the Kanban DB:
read/write of per-board display metadata, board creation/discovery/archival, and the
``board.json``-as-identity-marker rule.

Split out of ``hermes_cli.kanban_db``; origin-resident helpers are reached
late-bound via ``_kb`` (import-cycle breaking) so monkeypatching
``kanban_db.<name>`` keeps working.

Dispatch admission (card t_17c9c847) lives here too: ``board_dispatch_enabled`` is the
one chokepoint that decides whether the dispatcher may serve a board, and
``list_dispatch_boards`` is the dispatcher's OWN enumeration — ``list_boards`` stays the
full inventory for the CLI and the dashboard, so an estate board is still VISIBLE on the
board list, it is simply never spawned from.
"""

from __future__ import annotations

import json
import time
from typing import Any
from typing import Optional
from pathlib import Path


def _dir_holds_board(d: Path) -> bool:
    """The directory holds a board STORE — a ``kanban.db`` — or the ``board.json`` naming one.

    Two questions live here and they are NOT the same one (card t_b1719b4d):

    * ``board_exists`` asks "is there a board on disk at this slug?" — a directory holding
      either file answers yes, so a store whose metadata was deleted is still a board and a
      teardown that left nothing behind is not.
    * DISCOVERY asks "did somebody REGISTER this slug?" — :func:`list_boards` uses
      :func:`_dir_is_registered` (``board.json``), because a ``kanban.db``-only stub minted by
      a stale ``connect(board=slug)`` used to resurface in the board list as an empty active
      board (#43243).
    """
    return (d / "board.json").exists() or (d / "kanban.db").exists()


def _dir_is_registered(d: Path) -> bool:
    """``board.json`` is on disk: ``create_board`` wrote it, so the slug was REGISTERED.

    This is the discovery/registration probe (``list_boards``, ``board_is_registered``), kept
    separate from :func:`_dir_holds_board` so a filing-minted DB stub still never becomes a
    board anyone can enumerate (#43243).
    """
    return (d / "board.json").exists()



def board_metadata_path(board: Optional[str] = None) -> Path:
    """``board.json`` path — display metadata only; the directory slug is the identity."""
    return _kb.board_dir(_kb._slug_or_default(board)) / "board.json"


def _default_board_display_name(slug: str) -> str:
    """``atm10-server`` -> ``Atm10 Server``."""
    return " ".join(part.capitalize() for part in slug.replace("_", "-").split("-") if part) or slug


# --------------------------------------------------------------------------- #
# Dispatch admission (card t_17c9c847)
# --------------------------------------------------------------------------- #


def _board_dispatch_flag(value: Any = True) -> bool:
    """Coerce a ``board.json`` ``dispatch`` value to the admission bool.

    Absent, ``None`` or an unrecognised value means ADMITTED (``True``), so every
    board that predates the key keeps dispatching. Only an explicit false-y value
    (``false``, ``0``, ``"off"`` ...) takes a board out of the dispatch set.
    """
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    if isinstance(value, (int, float)):
        return bool(value)
    return True


def board_is_archived_stub(slug: str) -> bool:
    """A directory that is NOT a live board, but whose slug already has an ``_archived`` copy.

    The measured resurrection (card t_17c9c847): a live worker's env pins its board's
    store path, so the moment that board is archived the worker's next ``connect()`` (its
    own auto-heartbeat, a tool call) mints an EMPTY store back at the archived slug's
    directory. Two shapes are that mint, never a board somebody created:

    * a directory with no ``board.json`` at all (``create_board`` always writes one), and
    * the retired board's own tombstone — ``remove_board(archive=True)`` leaves an
      ``archived`` ``board.json`` behind — once a stale read path has minted a store
      beside it. A tombstone with no store is still the board's own record and stays
      listed; the tombstone PLUS a freshly minted ``kanban.db`` is the stub.

    An explicit ``create_board`` writes ``archived=False``, so a sanctioned re-create is
    never a stub.
    """
    try:
        meta_path = board_metadata_path(slug)
        if meta_path.exists():
            try:
                raw = json.loads(meta_path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                return False
            if not (isinstance(raw, dict) and raw.get("archived")):
                return False  # a live board
            if not (_kb.board_dir(slug) / "kanban.db").exists():
                return False  # the ordinary tombstone: still this board's record
    except Exception:
        return False
    root = _kb.boards_root() / "_archived"
    if not root.is_dir():
        return False
    prefix = f"{slug}-"
    try:
        for child in root.iterdir():
            if not child.name.startswith(prefix):
                continue
            # ``<slug>-<epoch>`` (and ``<slug>-<epoch>-<n>`` on a rapid re-archive).
            if child.name[len(prefix):].split("-")[0].isdigit():
                return True
    except OSError:
        return False
    return False


def board_dispatch_enabled(board: Optional[str] = None) -> bool:
    """May the dispatcher serve ``board``? ``False`` for an estate/scratch board.

    The one chokepoint read by the dispatcher's board enumeration
    (:func:`list_dispatch_boards`) AND by its per-tick spawn guard
    (``kanban_db_dispatch.dispatch_once``), so an estate board is safe by construction —
    including from a CLI ``hermes kanban --board <estate> dispatch`` that bypasses
    enumeration entirely (card t_17c9c847).

    The ``dispatch`` key defaults to ``True`` AT READ TIME and is never materialized into
    ``board.json`` by an unrelated write, so every board that predates the key keeps
    dispatching. Fails CLOSED for a resurrected archived stub: a minted directory is not a
    board, so re-opening an archived slug's path cannot re-admit it.
    """
    slug = _kb._slug_or_default(board)
    if board_is_archived_stub(slug):
        return False
    try:
        return _board_dispatch_flag(read_board_metadata(slug).get("dispatch", True))
    except Exception:
        return True


#: The longest a board NAME may be, in characters. The ``boards list`` NAME column is 28
#: wide and a display label is what the field is for; anything that does not fit a line is
#: PROSE, and prose about a board belongs in a FIELD (``sealed`` / ``sealed_reason`` /
#: ``description``), never in the name. Measured: the ``default`` board's name carried
#: "SEALED - not a fleet board (operator directive 2026-11-04)" (60 chars), so every reader
#: of the register — the dashboard, the tool list, an operator — was handed a policy
#: sentence where a name belongs (card t_62917d87).
MAX_BOARD_NAME_LEN = 40

#: What a refusal says about where the prose DOES belong. One sentence, because the refusal
#: is the only place a caller learns the field exists.
_BOARD_NAME_POLICY_HINT = (
    "A seal or a policy directive is a FIELD, not a name: write "
    "`hermes kanban boards seal <slug> --reason '<the directive>'` (board.json "
    "`sealed` + `sealed_reason`), or put it in `description`."
)


def validate_board_name(name: Any) -> str:
    """The board NAME a write may store, or a ``ValueError`` that REFUSES it.

    A name is a short display label, so this REFUSES rather than truncates: a silently
    shortened name is a different name, and the caller would never learn the register now
    says something they did not write. Three refusals, each with its own sentence:

    * empty / whitespace-only — there is no name to store, and substituting one would hide
      the empty value the caller passed;
    * a line break or control character — a name is one line, and a ``board.json`` whose
      name spans lines renders a broken column in every reader;
    * longer than :data:`MAX_BOARD_NAME_LEN` — that is prose, and
      :data:`_BOARD_NAME_POLICY_HINT` names the field it belongs in instead.

    Surrounding whitespace is trimmed (the label is what carries meaning), never interior
    text.
    """
    text = str(name).strip()
    if not text:
        raise ValueError(
            "a board name cannot be empty or whitespace-only — pass a display label "
            "(the default is the title-cased slug), or omit `name` to keep the current one"
        )
    control = sorted({ch for ch in text if ch in "\r\n\t" or ord(ch) < 32})
    if control:
        shown = ", ".join(repr(ch) for ch in control)
        raise ValueError(
            f"board name {text!r} contains a line break or control character ({shown}); "
            "a name is ONE line. " + _BOARD_NAME_POLICY_HINT
        )
    if len(text) > MAX_BOARD_NAME_LEN:
        raise ValueError(
            f"board name {text!r} is {len(text)} characters; a name is at most "
            f"{MAX_BOARD_NAME_LEN}. " + _BOARD_NAME_POLICY_HINT
        )
    return text


def _stores_as_name(name: Optional[str], slug: str) -> str:
    """The name a WRITE stores: validated when the caller supplied one, else the default label."""
    if name is None:
        return _default_board_display_name(slug)
    return validate_board_name(name)


def _display_name_of(meta: dict, slug: str) -> str:
    """``meta``'s stored name when it would pass validation, else the title-cased slug.

    Only a LEGACY record needs this (the tombstone ``remove_board`` writes re-stores the name
    the board already carries, and a name stored before validation existed must not fail an
    archive). It never invents a different name for a valid one.
    """
    try:
        return validate_board_name(meta.get("name") or "")
    except ValueError:
        return _default_board_display_name(slug)


def registered_board_slugs() -> list[str]:
    """Every slug this host has REGISTERED, as a filing may name it: ``default`` first, sorted.

    The registered set is the refusal's remedy (``kanban_db.unregistered_board_refusal``): a
    caller who names a slug nobody registered is told what IS registered, so the message is
    actionable instead of an instruction to run a CLI (card t_62917d87). One definition, read
    by that refusal and by no other enumeration — :func:`list_boards` stays the inventory
    (it includes archived records and display metadata), and the dispatcher's own set stays
    :func:`list_dispatch_boards`.

    A slug counts when its directory carries ``board.json`` (registration, not a filing-minted
    store: :func:`_dir_is_registered`), it is not an archived record, and it is not a
    resurrected stub. ``default`` is registered by construction and always first.
    """
    slugs = [_kb.DEFAULT_BOARD]
    root = _kb.boards_root()
    try:
        children = sorted(root.iterdir(), key=lambda p: p.name.lower()) if root.is_dir() else []
    except OSError:
        return slugs
    for child in children:
        if not child.is_dir():
            continue
        try:
            normed = _kb._normalize_board_slug(child.name)  # junk dirs are not boards
        except ValueError:
            continue
        if not normed or normed in slugs or not _dir_is_registered(child):
            continue
        try:
            if read_board_metadata(normed).get("archived") or board_is_archived_stub(normed):
                continue
        except Exception:
            continue
        slugs.append(normed)
    return slugs


def read_board_metadata(board: Optional[str] = None) -> dict:
    """``board.json`` merged over defaults, plus ``slug`` and ``db_path``. Never
    raises — a missing/malformed file yields the synthesized entry. The ``dispatch``
    default is applied at read time by :func:`board_dispatch_enabled`, not here: an
    unrelated write must leave a ``board.json`` that never touched the key alone.

    ``sealed``/``sealed_reason`` are the SEAL's home (card t_62917d87): a board that is not
    a fleet board says so in a field, so ``name`` stays a name in every reader. Both default
    at READ time (``False`` / ``""``) and are only materialized when a write sets them, the
    same contract ``dispatch`` has."""
    slug = _kb._slug_or_default(board)
    meta: dict[str, Any] = {
        "slug": slug,
        "name": _default_board_display_name(slug),
        "description": "",
        "icon": "",
        "color": "",
        "sealed": False,
        "sealed_reason": "",
        "default_workdir": None,
        # Project scope: new tasks inherit it (deterministic worktree + branch).
        "project_id": None,
        "created_at": None,
        "archived": False,
    }
    try:
        p = board_metadata_path(slug)
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                # Never let the metadata file claim a different slug than
                # its directory — trust the filesystem.
                raw["slug"] = slug
                meta.update(raw)
    except (OSError, json.JSONDecodeError):
        pass
    meta["db_path"] = str(_kb.kanban_db_path(slug))
    return meta


def write_board_metadata(
    board: Optional[str], *, name: Optional[str] = None, description: Optional[str] = None,
    icon: Optional[str] = None, color: Optional[str] = None, archived: Optional[bool] = None,
    default_workdir: Optional[str] = None, project_id: Optional[str] = None,
    dispatch: Optional[bool] = None, priority_policy: Any = None,
    sealed: Optional[bool] = None, sealed_reason: Optional[str] = None,
) -> dict:
    """Create/update ``board.json``; unmentioned fields are preserved, ``created_at``
    set on first write. ``project_id``/``default_workdir``: ``None`` = unchanged,
    "" = clear (``project_id`` is not validated here). ``dispatch``: ``None`` =
    unchanged — the synthesized ``True`` default is applied at READ time by
    :func:`board_dispatch_enabled`, so a board that never touched the key keeps its
    ``board.json`` byte-identical; ``False`` marks an estate/scratch board the dispatcher
    must not serve.

    ``priority_policy`` — the birth-time policy a board declares (the key
    ``kanban_priority_policy`` reads, card t_2187558e) — is tri-state too: ``None`` leaves
    the key EXACTLY as the file holds it (every board that predates the restore carries an
    orphaned copy, and an unrelated write must not clear or rewrite it), a value that is
    falsy per :func:`kanban_priority_policy.board_is_wired`/``normalize_spec`` (``""``,
    ``{}``) CLEARS it by removing the key, and a string or dict spec is STORED as given.

    Storing what it is given (rather than a re-normalised copy) is deliberate: this is the
    WRITER, and the verb that wires a board — ``hermes kanban boards set-priority-policy`` —
    is the surface that validates, because it is the one with somewhere to report a
    refusal ("nothing written", exit 2). A policy that cannot be loaded must stay
    storeable so the birth seam can be exercised against one and watch it report
    ``unavailable`` rather than fail the filing.

    ``name`` is the exception: it is VALIDATED here, not normalized (card t_62917d87). A
    value that is empty, spans a line, or runs past :data:`MAX_BOARD_NAME_LEN` raises
    ``ValueError`` and NOTHING is written — the refusal names the ``sealed`` /
    ``sealed_reason`` field a policy sentence belongs in. ``None`` keeps the stored name.

    ``sealed`` / ``sealed_reason`` are the seal's home: ``None`` leaves each as the file
    holds it, a bool stores it, and ``sealed_reason=""`` removes the key (the seal is the
    bool; the reason is its prose). Neither changes dispatch admission on its own —
    ``dispatch: false`` is the flag the dispatcher reads — so sealing a board is a
    declaration, never a silent change of behaviour."""
    _kb._assert_not_delegated_child_mutation()
    slug = _kb._slug_or_default(board)
    meta = read_board_metadata(slug)
    # db_path is derived on every read; never persist it into board.json.
    meta.pop("db_path", None)
    if name is not None:
        # REFUSES, never truncates (card t_62917d87): see validate_board_name. The raise is
        # before any mkdir/write below, so a refused name leaves board.json untouched.
        meta["name"] = validate_board_name(name)
    for key, value in (("description", description), ("icon", icon), ("color", color)):
        if value is not None:
            meta[key] = str(value)
    if archived is not None:
        meta["archived"] = bool(archived)
    if dispatch is not None:
        meta["dispatch"] = bool(dispatch)
    if sealed is not None:
        meta["sealed"] = bool(sealed)
    if sealed_reason is not None:
        reason = str(sealed_reason).strip()
        if reason:
            meta["sealed_reason"] = reason
        else:
            meta.pop("sealed_reason", None)
    if priority_policy is not None:
        if isinstance(priority_policy, dict) and not priority_policy:
            priority_policy = ""  # a spec that names nothing is the CLEAR, not a policy
        if isinstance(priority_policy, str) and not priority_policy.strip():
            meta.pop("priority_policy", None)
        else:
            meta["priority_policy"] = priority_policy
    for key, value in (("default_workdir", default_workdir), ("project_id", project_id)):
        if value is not None:
            meta[key] = str(value) if value else None
    if not meta.get("created_at"):
        meta["created_at"] = int(time.time())
    path = board_metadata_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    meta["db_path"] = str(_kb.kanban_db_path(slug))
    return meta


def create_board(
    slug: str, *, name: Optional[str] = None, description: Optional[str] = None,
    icon: Optional[str] = None, color: Optional[str] = None, default_workdir: Optional[str] = None,
    project_id: Optional[str] = None, dispatch: Optional[bool] = None,
    sealed: Optional[bool] = None, sealed_reason: Optional[str] = None,
) -> dict:
    """Create board dir + DB + metadata (``mkdir -p`` semantics: existing board returns its metadata).

    ``dispatch=False`` births an ESTATE/scratch board the dispatcher never serves (card
    t_17c9c847): the flag is written into ``board.json`` at creation, so the board is
    undispatchable by construction rather than by a teardown somebody has to remember.

    ``name`` is validated by :func:`write_board_metadata` (a policy sentence, a line break or
    an empty string is REFUSED, exit 2 at the CLI); ``sealed``/``sealed_reason`` declare a
    board that is not a fleet board in a FIELD, so ``name`` stays a name (card t_62917d87).
    """
    normed = _kb._require_slug(slug)
    # Explicit creation clears any archived tombstone at this slug (left by
    # remove_board(archive=True)) — otherwise _kb.init_db() below would rightly
    # refuse to recreate the archived board's DB (#43243).
    meta = write_board_metadata(
        normed, name=name, description=description, icon=icon, color=color,
        default_workdir=default_workdir, project_id=project_id, archived=False,
        dispatch=dispatch, sealed=sealed, sealed_reason=sealed_reason,
    )
    # Touch the DB so list_boards() sees it immediately.
    _kb.init_db(board=normed)
    return meta


def list_boards(*, include_archived: bool = True) -> list[dict]:
    """Metadata for every board: ``default`` first (always present), then
    ``boards/<slug>/`` dirs holding a ``kanban.db`` or ``board.json``, sorted.

    A resurrected archived STUB — a minted directory whose slug already has an
    ``_archived`` copy — is NOT a board and is skipped here, so the phantom a pinned
    worker re-creates cannot re-enter any enumeration
    (:func:`board_is_archived_stub`, card t_17c9c847)."""
    entries = [read_board_metadata(_kb.DEFAULT_BOARD)]
    seen = {_kb.DEFAULT_BOARD}
    root = _kb.boards_root()
    if root.is_dir():
        for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir():
                continue
            try:
                normed = _kb._normalize_board_slug(child.name)  # skip junk dirs, don't raise
            except ValueError:
                continue
            if not normed or normed in seen or not _dir_is_registered(child):
                continue
            if board_is_archived_stub(normed):
                continue
            meta = read_board_metadata(normed)
            if meta.get("archived") and not include_archived:
                continue
            entries.append(meta)
            seen.add(normed)
    return entries


def list_dispatch_boards() -> list[dict]:
    """Live boards the DISPATCHER may serve: non-archived AND dispatch-enabled.

    The dispatcher's own board enumeration (card t_17c9c847). :func:`list_boards` stays
    the full inventory for the CLI and the dashboard, so an estate board is still VISIBLE
    on the board list — it is simply never spawned from. Both halves are load-bearing: the
    ``dispatch`` flag closes the gap the archived-only filter left (``"each board has its
    own ... dispatcher loop"``), and :func:`board_dispatch_enabled` fails closed for a
    resurrected archived stub.
    """
    return [
        meta for meta in list_boards(include_archived=False)
        if board_dispatch_enabled(meta.get("slug") or _kb.DEFAULT_BOARD)
    ]


def remove_board(slug: str, *, archive: bool = True, leave_marker: bool = True) -> dict:
    """Archive (to ``boards/_archived/<slug>-<ts>/``) or delete a board;
    ``default`` cannot be removed. Returns ``{"slug", "action", "new_path"}``.

    ``leave_marker=False`` archives WITHOUT the ``archived`` tombstone: only a declared-estate
    teardown asks for that, because a torn-down estate must leave its slug free (t_b1719b4d).
    """
    _kb._assert_not_delegated_child_mutation()
    normed = _kb._require_slug(slug)
    if normed == _kb.DEFAULT_BOARD:
        raise ValueError("the 'default' board cannot be removed")
    d = _kb.board_dir(normed)
    if not d.exists():
        raise ValueError(f"board {normed!r} does not exist")

    # If the user removed the currently-active board, revert to default.
    if _kb.get_current_board() == normed:
        _kb.clear_current_board()

    # A concurrent connect() after the rename recreates an empty DB file; drop
    # the init cache first so the schema pass re-runs on it.
    _kb._INITIALIZED_PATHS.discard(str((d / "kanban.db").resolve()))

    if archive:
        # Capture display metadata before the move so the tombstone below keeps
        # the user's board name instead of falling back to a title-cased slug.
        prior_meta = read_board_metadata(normed)
        archive_root = _kb.boards_root() / "_archived"
        archive_root.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        target = archive_root / f"{normed}-{ts}"
        suffix = 1
        while target.exists():  # rapid double-archive
            target = archive_root / f"{normed}-{ts}-{suffix}"
            suffix += 1
        d.rename(target)
        # Leave an ``archived`` tombstone at the original slug. Stale dashboard
        # tabs / gateway pollers can keep calling connect(board=slug) after the
        # archive; without a marker the resurrect-guard cannot tell an archived
        # slug from a brand-new one and an empty board would reappear (#43243).
        # An ESTATE teardown (`leave_marker=False`) is the exception: the board is declared
        # un-dispatchable, the teardown is final, and the slug must be FREE afterwards — a
        # tombstone would keep `board_exists` answering yes for a board that is gone
        # (card t_b1719b4d).
        if leave_marker:
            write_board_metadata(
                normed,
                # A name stored BEFORE validation existed must not fail the archive: the
                # tombstone re-stores what the board carries when that still validates, else
                # the slug's own display label (never a truncation of the old prose).
                name=_display_name_of(prior_meta, normed),
                description=prior_meta.get("description"),
                icon=prior_meta.get("icon"),
                color=prior_meta.get("color"),
                default_workdir=prior_meta.get("default_workdir"),
                project_id=prior_meta.get("project_id"),
                sealed=True if prior_meta.get("sealed") else None,
                sealed_reason=(prior_meta.get("sealed_reason") or None),
                archived=True,
            )
        return {"slug": normed, "action": "archived", "new_path": str(target)}
    import shutil
    shutil.rmtree(d)
    return {"slug": normed, "action": "deleted", "new_path": ""}


# Late-bound origin namespace (see module docstring); imported LAST so this
# module is fully populated before ``kanban_db`` imports from it.
from hermes_cli import kanban_db as _kb
