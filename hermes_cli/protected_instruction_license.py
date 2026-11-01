"""The DURABLE consent license for the ``project-instruction-file`` class (t_28bbf346).

The protected-instruction gate (``tools/file_tools_write_guards.py``) used to be a
ONE-OPERATION approval with nothing persisted, so it fails closed to "no interactive user
or gateway is present" for every unattended worker. An operator who had already granted
the write could not make it take effect: the consent was durable, the door was not. This
module is the door's durable half.

THE RULING (platform-stl, design authority, carried by ``t_28bbf346``):

1. ``project-instruction-file`` is a member of the consent-gated surface class.
2. The gate is licensed by a DURABLE ``approved`` row in the approvals store, FOUND BY
   ``card_id`` -- the id of the card the write is being done under. Never by a reference
   appearing in card prose: ``_card_text`` reads a card's title and body only, so a
   consent recorded in a COMMENT never licenses (measured on the live failure).
   **And the card id is the SESSION's, never the caller's**: it is resolved here from the
   live context (``HERMES_KANBAN_TASK``, the same reader the consent-surface clause uses),
   so no argument, and therefore no tool call, can name a card that carries a broad approved
   row. A caller that names a different card REFUSES (``card_mismatch``): the parameter can
   only demote a write, never license one. Without a live card there is no license at all.
3. A row's scope is read as ``project-instruction-file:<repo>``, where the repo is the one
   the file lives in (the trading patch is ``project-instruction-file:yaan-trading-strategy``).
   The broad ``project-instruction-file`` scope covers every repo.
4. ONE approval licenses ONE file class in ONE repo. Fail-closed stays: no matching
   approved row means the write is refused exactly as it is today, and the refusal names
   the row shape to file.
5. The live per-operation human prompt stays available as the fallback, so this module
   only ever answers "is there a durable license?" -- it never prompts, never refuses, and
   never writes.

WHY A SEPARATE MODULE. The decision needs I/O the pure surface classifier forbids: git (to
name the repo) and the approvals store. Keeping it here means the classifier stays pure, the
gate in ``tools/`` stays a thin caller, and the whole rule is unit-testable against a
throwaway git repo and a throwaway approvals DB.

WHAT IT DOES NOT DO. It does not add ``project-instruction-file`` to
``hermes_cli.consent_surface.SURFACES``. That set feeds the FILE/TERMINAL/EXEC doors of
design ``t_cb9259fc``, which REFUSE outright; this class must not become a hard refusal,
because ruling 5 keeps the human prompt as the fallback for an unlicensed write. So the
token shares the surface vocabulary and its coverage rule (``hermes-config:<profile>``'s
sibling) without joining the hard-refusal doors.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "LICENSING_STATUSES",
    "SURFACE",
    "LicenseVerdict",
    "DEFAULT_APPROVALS_DB",
    "assess",
    "format_receipt",
    "license_for",
    "live_card_id",
    "normalise_repo",
    "repo_identity",
    "scope_token",
]

#: The class name, and the prefix of every scope token it is licensed under.
SURFACE = "project-instruction-file"

#: The only row status that licenses a change (same set the consent-surface clause uses).
LICENSING_STATUSES: tuple[str, ...] = ("approved",)

#: The approvals store, shared with ``hermes_cli.kanban_consent_gate`` (same default, same
#: env override) so a host that moves one moves both.
DEFAULT_APPROVALS_DB = Path("/opt/hermes_prod/yoyodine-web-services/approvals.db")
APPROVALS_DB_ENV = "KANBAN_APPROVALS_DB"

#: A card id is opaque, but it is never empty and never whitespace.
_CARD_ID = re.compile(r"^[A-Za-z0-9._:-]+$")

#: Everything a repo leaf may not carry. GitHub repo names are ``[A-Za-z0-9._-]``.
_UNSAFE = re.compile(r"[^a-z0-9._-]+")

#: ``git`` is a local read-only call on a directory we were handed; cap it so a hung
#: filesystem (a stale network mount) cannot wedge a write.
_GIT_TIMEOUT_S = 5.0


# --- the token ---------------------------------------------------------------


def normalise_repo(value: object) -> str:
    """The repo half of the scope token for a name, path or git URL.

    ``git@github.com:yoyodine/yaan-trading-strategy.git``, an absolute path to the
    checkout and a bare name all collapse to ``yaan-trading-strategy``: the leaf, lower
    case, ``.git`` stripped. Anything that collapses to nothing returns ``""`` -- the
    caller treats that as "cannot tell", never as a match-everything token.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if text.endswith(".git"):
        text = text[: -len(".git")]
    text = text.rstrip("/")
    # A URL keeps its leaf after the last separator of either spelling.
    leaf = re.split(r"[/\\:]", text)[-1] if re.search(r"[/\\:]", text) else text
    return _UNSAFE.sub("-", leaf.strip().lower()).strip("-_.")


def scope_token(repo: object) -> str:
    """``project-instruction-file:<repo>`` -- the token a covering row must name."""
    leaf = normalise_repo(repo)
    return f"{SURFACE}:{leaf}" if leaf else ""


# --- the repo a file lives in ------------------------------------------------


def _git(cwd: str, *args: str) -> Optional[str]:
    """Stripped stdout of one read-only ``git`` call, or ``None`` (never raises)."""
    try:
        done = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return (done.stdout or "").strip() or None


def repo_identity(path: object) -> Optional[str]:
    """The repo ``path`` lives in, as the scope-token half; ``None`` when it cannot be told.

    The identity is the checkout's git TOP LEVEL, named by its ``origin`` remote when one is
    configured and by the top-level directory's own name otherwise. The remote matters: the
    trading patch is written in a per-card worktree whose DIRECTORY is ``ts-t27f67857``, and
    a token built from the directory name would license nothing anyone could file.

    Resolution walks up from the file to the nearest directory that EXISTS, so a create (the
    file is not there yet) resolves exactly like an edit. A path outside any git repository
    returns ``None``: an unidentifiable repo is not a licence, and the caller fails closed.
    """
    raw = str(path or "").strip()
    if not raw:
        return None
    target = Path(os.path.expanduser(raw))
    start = target if target.is_dir() else target.parent
    while True:
        if start.exists():
            break
        if start == start.parent:
            return None
        start = start.parent
    top = _git(str(start), "rev-parse", "--show-toplevel")
    if not top:
        return None
    remote = _git(top, "config", "--get", "remote.origin.url")
    return normalise_repo(remote) or normalise_repo(os.path.basename(top)) or None


# --- the verdict -------------------------------------------------------------


@dataclass(frozen=True)
class LicenseVerdict:
    """Whether the durable license exists, and how to file one when it does not.

    ``cause`` is the machine half (logged and asserted on); ``tokens`` are the scope tokens
    still unmatched; ``rows`` are the card's own rows that were read -- approved ones when
    the cause is a scope miss, so the refusal can name them.
    """

    licensed: bool
    cause: str
    tokens: tuple[str, ...] = ()
    rows: tuple[int, ...] = ()
    detail: str = ""

    def hint(self, card_id: object = "") -> str:
        """The row shape to file -- appended to the gate's refusal, so an ask lands correctly.

        Silence here would leave a filer guessing which of ``card_id``, ``status``, scope or
        store the refusal was about; the whole point of the ruling is that the ask can be
        filed from the refusal itself.
        """
        card = str(card_id or "").strip() or "(no card)"
        wanted = list(self.tokens) or [f"{SURFACE}:<repo>"]
        if self.licensed:  # pragma: no cover - a licensed write is not refused
            return ""
        scope_lines = [f"  scope   = {wanted[0]}"]
        scope_lines += [f"            {extra}" for extra in wanted[1:]]
        lines = [
            "TO LICENSE THIS WRITE (no prompt is coming -- this run is unattended): file ONE",
            "approvals row and it licenses the write on the next try:",
            f"  card_id = {card}            (the card the write is being done under)",
            "  status  = approved          (a 'pending' or 'asked' row never licenses)",
            *scope_lines,
            f"  idempotency_key = '<project>\\x1f{wanted[0]}'",
            "One approved row licenses ONE file class in ONE repo; the broad scope "
            f"'{SURFACE}' covers every repo.",
        ]
        if self.cause == "no_card":
            lines.insert(0, "WHY: this session is not running under a kanban card, so there is no "
                            "card_id to find a license by. A card-scoped run is required.")
        elif self.cause == "no_repo":
            lines.insert(0, "WHY: the target is not inside a git repository, so the repo half of the "
                            "scope token cannot be named.")
        elif self.cause == "no_row":
            lines.insert(0, "WHY: the approvals store holds NO row whose card_id is this card "
                            f"({card}) -- nothing was ever filed for it.")
        elif self.cause == "no_approved_row":
            lines.insert(0, "WHY: this card has row(s) "
                            + (", ".join(f"APR-{n:04d}" for n in self.rows) or "(none)")
                            + " but none of them is approved.")
        elif self.cause == "scope_not_covered":
            lines.insert(0, "WHY: this card's approved row(s) "
                            + (", ".join(f"APR-{n:04d}" for n in self.rows) or "(none)")
                            + " declare a narrower or different scope than "
                            + ", ".join(self.tokens) + ".")
        elif self.cause == "store_unreadable":
            lines.insert(0, "WHY: the approvals store could not be read"
                            + (f" ({self.detail})" if self.detail else "") + ".")
        elif self.cause == "no_paths":
            lines.insert(0, "WHY: no target path was supplied, so no scope could be named.")
        if self.detail and self.cause != "store_unreadable":
            lines.insert(1, f"DETAIL: {self.detail}")
        return "\n".join(lines)


# --- the store ---------------------------------------------------------------


def _approvals_path(explicit: object = None) -> Path:
    if explicit:
        return Path(str(explicit))
    return Path(os.environ.get(APPROVALS_DB_ENV) or DEFAULT_APPROVALS_DB)


def _rows_for_card(path: Path, card_id: str) -> list[dict]:
    """Read-only: every approvals row filed under ``card_id``, newest id last.

    Read-only on purpose: a guard must never create, migrate or groom the operator's consent
    store. An unreadable store RAISES -- "cannot tell" is not "no row", and the caller's
    fail-closed direction is the refusal.

    A store that is merely IDLE is readable, and that is what the fallbacks are for. The
    approvals store is a WAL store: its ``-wal``/``-shm`` sidecars come and go with its
    writers, and a read-only open of a sidecar-less store has to BUILD the ``-shm`` -- which
    sqlite refuses where the store's directory is not writable by us, and on some builds
    refuses outright (``unable to open database file`` and ``attempt to write a readonly
    database``, both measured against the live store's shape). That refusal used to be
    reported as ``store_unreadable``, refusing a write the operator HAD licensed. The order
    below is the whole rule:

    * sidecars absent (idle): retry ``?immutable=1``, which needs no sidecar and no write access.
    * a NON-EMPTY ``-wal``: read a COPY of the store and its sidecars. ``immutable=1`` here
      would answer from the last checkpoint -- missing, and for a license still LICENSING, rows
      in the un-checkpointed tail. That is the unsafe direction, so it is never taken.
    * genuinely unreadable (corrupt, absent, not readable by this user): every arm raises and
      the caller reports ``store_unreadable`` exactly as before.
    """
    try:
        return _fetch_rows(path, card_id)
    except sqlite3.OperationalError:
        if _wal_bytes(path):
            with _snapshot(path) as copy:
                return _fetch_rows(copy, card_id)
        return _fetch_rows(path, card_id, immutable=True)


def _fetch_rows(store: Path, card_id: str, *, immutable: bool = False) -> list[dict]:
    """One read-only connect on ``store``; the query is what fails on a bad store."""
    uri = Path(store).resolve().as_uri() + ("?immutable=1" if immutable else "?mode=ro")
    con = sqlite3.connect(uri, uri=True, timeout=2.0)
    try:
        con.row_factory = sqlite3.Row
        con.text_factory = lambda b: b.decode("utf-8", "replace")
        rows = con.execute(
            "SELECT id, status, decision, reason, task, context, idempotency_key, superseded_by "
            "FROM approvals WHERE card_id = ? ORDER BY id",
            (card_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()


def _wal_bytes(path: Path) -> int:
    """Size of the ``-wal`` sidecar, ``0`` when there is none (or it is empty)."""
    try:
        return Path(f"{path}-wal").stat().st_size
    except OSError:
        return 0


@contextmanager
def _snapshot(path: Path):
    """Copy the store and its live sidecars into a private temp dir; yields the copy.

    Pages, then the write-ahead log, then the index: the copy is then a store sqlite can
    recover normally. ``mkdtemp`` is 0700 and the ``finally`` below removes it -- the consent
    store is the operator's, so even a transient copy is private and short-lived.
    """
    workdir = Path(tempfile.mkdtemp(prefix="approvals-read-"))
    try:
        copy = workdir / path.name
        shutil.copy2(path, copy)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{path}{suffix}")
            if sidecar.exists():
                shutil.copy2(sidecar, f"{copy}{suffix}")
        yield copy
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _scope_of_row(row: dict) -> str:
    """The scope a row DECLARES: the half of ``idempotency_key`` after the project separator."""
    key = str(row.get("idempotency_key") or "").strip()
    return " ".join(key.split("\x1f")[-1].split()).lower() if key else ""


def _key_covers(covering: str, token: str) -> bool:
    """Does ``covering`` cover ``token``?

    ONE definition of coverage -- ``approvals_core._key_covers``: a scope covers itself and
    every sub-scope after a ``:``, and never the other way round (so the broad
    ``project-instruction-file`` covers every repo, and a repo token covers only itself).
    The local form is the same two-line rule, used when the ops repo is not on this host.
    """
    try:
        from hermes_cli.consent_surface_guard import _key_covers as shared

        return bool(shared(covering, token))
    except Exception:  # the guard module is optional for a partial install
        return token == covering or token.startswith(covering + ":")


def _row_covers(row: dict, token: str) -> bool:
    """Coverage for one row: its DECLARED scope, else the literal token in its own text.

    A row with no ``idempotency_key`` predates the scope vocabulary and declares its scope in
    prose; the FULL token is accepted there (``project-instruction-file:yaan-trading-strategy``
    written out). The bare surface word is deliberately NOT accepted from prose: it would
    license every repo from a sentence, which is exactly the widening this card forbids.
    """
    scope = _scope_of_row(row)
    if scope:
        return _key_covers(scope, token)
    haystack = " \n ".join(
        str(row.get(field) or "") for field in ("decision", "reason", "task", "context")
    ).lower()
    return bool(token) and token.lower() in haystack


def _is_licensing(row: dict) -> bool:
    if str(row.get("status") or "").strip().lower() not in LICENSING_STATUSES:
        return False
    if str(row.get("superseded_by") or "").strip():
        return False
    return True


def _statuses(rows: Sequence[dict]) -> str:
    return ", ".join(
        f"APR-{int(row['id']):04d}={str(row.get('status') or '?').strip() or '?'}" for row in rows
    )


# --- the license -------------------------------------------------------------


def live_card_id() -> str:
    """The card this session is being done under, from the LIVE context -- never an argument.

    ``HERMES_KANBAN_TASK`` is set by the dispatcher when it spawns a worker, and it is the one
    value a tool call cannot choose: the tool layer resolves it from the session, so an agent
    calling ``write_file`` has no field to name a card with. The consent-surface clause reads
    the same context through ``GuardContext.from_env``, and this resolves it the same way so
    the two doors can never disagree about who owns a write.

    ``""`` when no card owns the session (a console operator IS the consent authority) -- the
    caller reads that as "no license", never as "no gate".
    """
    try:
        from hermes_cli.consent_surface_guard import GuardContext

        card = getattr(GuardContext.from_env(), "task_id", None)
    except Exception:  # larger surface unavailable: the env var IS the context
        card = None
    return str(card or os.environ.get("HERMES_KANBAN_TASK") or "").strip()


def _usable_card(card: str) -> bool:
    """A real card id: not empty, not the guards' ``default`` sentinel, not junk."""
    return bool(card) and card.lower() != "default" and bool(_CARD_ID.match(card))


def license_for(
    paths: Iterable[object],
    *,
    card: object = None,
    declared: object = None,
    approvals_db: object = None,
) -> LicenseVerdict:
    """Is there a durable license covering EVERY one of ``paths``? Never raises.

    THE CARD IS THE SESSION'S, NOT THE CALLER'S. ``card`` is a stand-in for the live context
    and exists for tests and diagnostics only; production reads :func:`live_card_id`. A caller
    that names a card cannot license anything: whatever it passes is checked against the live
    context in ``declared`` and a disagreement REFUSES (``card_mismatch``), so the parameter
    can only ever demote a write, never license one. Without a live card there is no license
    at all -- an argument cannot manufacture one.

    ONE approval licenses ONE file class in ONE repo, so a multi-file patch needs a covering
    row for each protected file it names -- a patch cannot slip a second repo's instruction
    file through on the first repo's license.
    """
    tokens: list[str] = []
    for path in paths or ():
        if not path:
            continue
        repo = repo_identity(path)
        if not repo:
            return LicenseVerdict(False, "no_repo", tokens=tuple(tokens),
                                  detail=f"{path}: not inside a git repository")
        token = scope_token(repo)
        if token not in tokens:
            tokens.append(token)
    tokens_t = tuple(tokens)
    if not tokens_t:
        return LicenseVerdict(False, "no_paths")

    live = str(card if card is not None else live_card_id()).strip()
    if not _usable_card(live):
        # ``default`` is the guards' "no kanban card owns this session" spelling. A console
        # operator IS the consent authority and is never gated; the license keys on a real
        # card id, and inventing one would license a run nobody filed for.
        return LicenseVerdict(False, "no_card", tokens=tokens_t)

    asked = str(declared or "").strip()
    if _usable_card(asked) and asked != live:
        # A caller raised a card id that is not the one this session runs under. The live
        # context wins and the write is refused -- this is the forge check, and its message
        # names the card that was ASKED FOR so the mismatch is visible, never silently
        # substituted.
        return LicenseVerdict(
            False, "card_mismatch", tokens=tokens_t,
            detail=f"the write is being done under {live}; the caller named {asked}")

    try:
        rows = _rows_for_card(_approvals_path(approvals_db), live)
    except Exception as exc:  # unreadable store -> fail closed, named cause
        return LicenseVerdict(False, "store_unreadable", tokens=tokens_t,
                              detail=f"{_approvals_path(approvals_db)}: {type(exc).__name__}: {exc}")

    if not rows:
        return LicenseVerdict(False, "no_row", tokens=tokens_t)
    approved = [row for row in rows if _is_licensing(row)]
    if not approved:
        return LicenseVerdict(
            False, "no_approved_row", tokens=tokens_t,
            rows=tuple(int(row["id"]) for row in rows), detail=_statuses(rows),
        )

    unmatched = tuple(token for token in tokens_t
                      if not any(_row_covers(row, token) for row in approved))
    covering = tuple(int(row["id"]) for row in approved)
    if unmatched:
        return LicenseVerdict(False, "scope_not_covered", tokens=unmatched, rows=covering,
                              detail="this card's rows: " + _statuses(approved))
    return LicenseVerdict(True, "licensed", tokens=tokens_t, rows=covering)


def assess(paths: Iterable[object], *, declared: object = None,
           approvals_db: object = None) -> tuple[LicenseVerdict, str]:
    """``(verdict, live card)`` -- the one call the gate needs; never raises.

    Takes NO card argument on purpose: the card is resolved here, from the live context, so
    nothing that calls this can name one. ``declared`` is the tool layer's own idea of the
    card, and it may only ever demote (``card_mismatch``).
    """
    card = live_card_id()
    try:
        return license_for(paths, declared=declared, approvals_db=approvals_db), card
    except Exception as exc:  # a guard must never take a tool call down
        logger.exception("protected-instruction license: evaluation failed; failing closed")
        return LicenseVerdict(False, "store_unreadable",
                              detail=f"{type(exc).__name__}: {exc}"), card


def format_receipt(verdict: LicenseVerdict, card_id: object = "") -> str:
    """One line naming what licensed the write -- the receipt for the log and the tests."""
    rows = ", ".join(f"APR-{n:04d}" for n in verdict.rows) or "(none)"
    return (f"protected-instruction write LICENSED by {rows} for card {card_id} "
            f"over {', '.join(verdict.tokens)}")
