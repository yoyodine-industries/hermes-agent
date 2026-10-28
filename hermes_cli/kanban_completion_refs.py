"""The completion-seam DISCHARGE gate: a claim that closes a card must be checkable.

Design record: ``platform-stl`` card ``t_0608ce6d`` (implementation card ``t_9a2aaaea``).

Two gates, one resolver, one kill switch. Both judge the completion at the seam, BEFORE the
card is written, because a post-hoc advisory is not a gate: ``t_241c8074`` closed ``done`` on
"the unit this card carries no longer exists" while both flows it named were live in the deploy
source, and both premises it rested on were false. Nothing read the argument.

* **Gate E (refs).** A ``t_<hex>`` id named in the handoff must resolve to a card on SOME board.
  ``absent`` refuses (a fabricated or deleted citation is not a record), ``unknown`` never does
  (a sibling store that cannot be read is a coverage gap, not a verdict - measured 2026-10-24:
  a transient read-only open failure took four board stores out for four minutes).
* **Gate F (void claims).** A completion that argues its premise is void must carry the
  measurement it is void BY, and the KERNEL evaluates every measurement. The claim holds only
  if all of them hold.

``_flag_phantom_prose_refs`` in ``hermes_cli/kanban_db.py`` stays as the advisory ``measure``
arm and delegates here, so exactly one resolver decides what a phantom is.

Module split out of ``kanban_db`` for the same reason as its ``kanban_db_*`` siblings: the
kernel file is a hot path and this is a self-contained judgment. Origin-resident helpers are
reached late-bound through :func:`_kb` (import-cycle breaking), so monkeypatching
``kanban_db.<name>`` keeps working.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

# -- the module's public vocabulary ---------------------------------------------------------

GATE_FILE = "discharge_gate"
GATE_ENV = "HERMES_KANBAN_DISCHARGE_GATE"
GATE_MODES = ("refuse", "measure", "off")
GATE_DEFAULT = "refuse"

#: How long the whole sibling-store sweep may take. A completion must not stall on a board
#: whose store is wedged: past the deadline the unresolved ids are ``unknown`` (never absent).
REF_TIMEOUT_SECONDS = 2.0

#: Matches ``kanban_create`` (12 hex) and ``_new_task_id`` (8 hex) ids; 8+ for forward compat.
#: The SAME extraction the advisory scan always used - the ref set does not change, only the
#: resolver behind it.
_TASK_ID_PROSE_RE = re.compile(r"\bt_[a-f0-9]{8,}\b")

#: The void-claim trigger, copied verbatim from the design record (one compiled literal, no
#: model, no heuristics). Sizing on the live boards, last 30 days: 93 of 12,323 completions
#: (0.75%) match on the ``result`` line; a loose whole-summary vocabulary would have been 6.7%
#: (1,856) and is rejected as too blunt to gate.
VOID_CLAIM_RE = re.compile(
    r"\bpremise(?:s)?\s+(?:is\s+|are\s+)?void\b"
    r"|\bnothing\s+to\s+(?:repair|fix|do)\b"
    r"|\bno\s+longer\s+exists?\b"
    r"|\balready\s+gone\b"
    r"|\b(?:was|were|has\s+been|have\s+been|is|are)\s+deleted\b"
    r"|\b(?:does|do)\s+not\s+exist\b"
    r"|\bhas\s+been\s+removed\b"
    r"|\bno\s+longer\s+present\b", re.IGNORECASE)

#: Closed vocabulary of measurement kinds. Anything else is ``measurement_invalid``: a vocabulary
#: that accepts new kinds silently is a waiver by typo.
MEASUREMENT_KINDS = ("path_absent", "glob_absent", "git_absent", "card_terminal", "run_ok")

#: The block's own keys. Unknown keys are REFUSED - a typo must never silently waive a claim.
BLOCK_KEYS = ("claim", "measurements", "basis", "why")

#: The honest opt-out: a completion that merely mentions one of the phrases and is not closing
#: on it. It is recorded (``void_claim_disclaimed``) and counted; there is no silent pass.
DISCLAIMER_BASIS = "not_the_basis"
DISCLAIMER_WHY_MIN = 20

# -- the events the gate writes (the report's only source of truth) ---------------------------

EVENT_REFUSED_REFS = "completion_blocked_phantom_refs"
EVENT_UNVERIFIED_REFS = "phantom_refs_unverified"
EVENT_WAIVED_REFS = "phantom_refs_waived"
EVENT_REFUSED_VOID = "completion_blocked_void_claim"
EVENT_MEASURED_VOID = "void_claim_measured"
EVENT_DISCLAIMED_VOID = "void_claim_disclaimed"
EVENT_WAIVED_VOID = "void_claim_waived"
EVENT_MEASURE_MODE = "discharge_gate_measure"

_REF_EVENT_KINDS = (EVENT_REFUSED_REFS, EVENT_UNVERIFIED_REFS, EVENT_WAIVED_REFS)
_VOID_EVENT_KINDS = (EVENT_REFUSED_VOID, EVENT_MEASURED_VOID, EVENT_DISCLAIMED_VOID,
                     EVENT_WAIVED_VOID)

#: The refusals as first-class exceptions. ``ValueError`` subclasses, like
#: ``HallucinatedCardsError`` / ``EmptyCompletionError``: a tool error handler treats them as
#: recoverable, which is what makes a worker retry with a corrected handoff instead of dying.
class PhantomRefsRefused(ValueError):
    """``complete_task`` refused: the handoff cites card ids no board carries."""

    cause = "phantom_refs"

    def __init__(self, phantom: Iterable[str], *, boards_searched: Iterable[str] = (),
                 task_id: str = "") -> None:
        self.phantom = list(phantom)
        self.boards_searched = list(boards_searched)
        self.task_id = task_id
        boards = ", ".join(self.boards_searched) or "(no board store could be named)"
        super().__init__(
            f"complete {task_id}: REFUSED - this handoff cites card id(s) that exist on NO "
            f"reachable board: {', '.join(self.phantom)}. Boards searched: {boards}. "
            f"A citation is a record, not a flourish: cite the real card (naming the board it "
            f"lives on if it is not this one), drop the id, or pass force=True with the reason "
            f"on the record. Nothing was written; the card is exactly as it was."
        )


class VoidClaimUnmeasured(ValueError):
    """``complete_task`` refused: the completion rests on a void premise it did not measure."""

    cause = "void_claim_unmeasured"

    def __init__(self, detail: str, *, task_id: str = "", claim: str = "") -> None:
        self.task_id = task_id
        self.claim = claim
        super().__init__(
            f"complete {task_id}: REFUSED - {detail}. A card may not close on the claim that "
            f"its premise is void unless the KERNEL can check that: declare "
            f'metadata["premise_void"] = {{"claim": "<what is void>", "measurements": '
            f'[{{"kind": "path_absent", "target": "/abs/path"}}, ...]}} with kinds '
            f"{', '.join(MEASUREMENT_KINDS)}; every measurement must hold. If the phrase is "
            f"incidental and not the basis of this close, say so: "
            f'{{"basis": "{DISCLAIMER_BASIS}", "why": "<at least {DISCLAIMER_WHY_MIN} characters>"}}. '
            f"Nothing was written; the card is exactly as it was."
        )


class VoidClaimRefused(ValueError):
    """``complete_task`` refused: the ``premise_void`` block itself is malformed."""

    cause = "void_claim_invalid"

    def __init__(self, detail: str, *, task_id: str = "") -> None:
        self.task_id = task_id
        super().__init__(f"complete {task_id}: REFUSED - {detail}.")


# -- the kill switch -------------------------------------------------------------------------

#: ``{"key": (path, mtime_ns) | None, "mode": str}``. Read once per process and re-read only
#: when the file's mtime moves, so the switch costs one ``stat`` per completion, never a read.
_MODE_CACHE: dict[str, Any] = {"key": None, "mode": GATE_DEFAULT}


def _gate_file() -> Path:
    return _kb().kanban_home() / "kanban" / GATE_FILE


def discharge_gate_mode() -> str:
    """``refuse`` (default), ``measure`` (soak: record and admit) or ``off`` (silent).

    Precedence: the environment (``HERMES_KANBAN_DISCHARGE_GATE``) over the file
    ``<kanban_home>/kanban/discharge_gate``. An unrecognised value is IGNORED, never read as a
    waiver - a typo must not disable a gate, the same rule the evidence gate already holds.

    Rollback is ``echo off > <kanban_home>/kanban/discharge_gate``: no code change and no worker
    restart, because the file is mtime-checked on every call.
    """
    env = os.environ.get(GATE_ENV, "").strip().lower()
    if env in GATE_MODES:
        return env
    path = _gate_file()
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        # No file = the shipped default. Cache the absence too: a board with no switch must not
        # pay a failed ``stat`` + a full re-read on every completion.
        if _MODE_CACHE.get("key") is not None or _MODE_CACHE.get("mode") != GATE_DEFAULT:
            _MODE_CACHE["key"], _MODE_CACHE["mode"] = None, GATE_DEFAULT
        return GATE_DEFAULT
    if _MODE_CACHE.get("key") == key:
        return str(_MODE_CACHE["mode"])
    try:
        raw = path.read_text(encoding="utf-8").strip().lower()
    except OSError:
        raw = ""
    mode = raw if raw in GATE_MODES else GATE_DEFAULT
    _MODE_CACHE["key"], _MODE_CACHE["mode"] = key, mode
    return mode


# -- ref resolution: tri-state, never a bare boolean ------------------------------------------


@dataclass(frozen=True)
class RefResolution:
    """What the resolver could establish about ONE ``t_<hex>`` reference.

    ``state`` is deliberately three-valued. ``absent`` is a verdict and may refuse; ``unknown``
    is a coverage gap (a store that would not open, or the deadline) and must never refuse - a
    store outage is not a fabrication.
    """

    tid: str
    state: str                      # "present" | "absent" | "unknown"
    board: Optional[str]            # board the ref was found on
    boards_searched: tuple[str, ...]
    unknown_boards: tuple[str, ...]


def _kb():
    """The origin module, late-bound (import-cycle breaking; monkeypatch-friendly)."""
    from hermes_cli import kanban_db as kb
    return kb


def _connection_path(conn: sqlite3.Connection) -> Optional[Path]:
    """The store file ``conn`` is open against, or ``None`` for an in-memory connection."""
    try:
        for _seq, name, file in conn.execute("PRAGMA database_list"):
            if name == "main" and file:
                return Path(file).resolve()
    except sqlite3.Error:
        return None
    return None


def _board_store_paths() -> list[tuple[str, Path]]:
    """Every board store on this host, by slug: the ``default`` store plus ``boards/*/*.db``.

    The layout rule is ``kanban_db.board_for_store_path``'s inverse and is spelled HERE rather
    than through ``kanban_db_path(<slug>)`` on purpose: the env pin (``HERMES_KANBAN_DB``) is a
    property of the CALLING PROCESS, so under a worker's pin ``kanban_db_path`` answers the
    pinned file for EVERY slug - the probe would then search one store nine times and call the
    rest "absent" (measured 2026-10-13, card t_2187558e).

    ``boards/_archived/kanban.db`` falls out of the same glob: it is a store like any other, and
    a card archived into it is still a card that exists.
    """
    kb = _kb()
    out: list[tuple[str, Path]] = [(kb.DEFAULT_BOARD, kb.kanban_home() / "kanban.db")]
    try:
        root = kb.boards_root()
    except Exception:  # pragma: no cover - a home that cannot be resolved says so upstream
        return out
    for path in sorted(glob.glob(str(root / "*" / "kanban.db"))):
        store = Path(path)
        try:
            if not store.parent.is_dir():
                continue
        except OSError:  # pragma: no cover - defensive
            continue
        out.append((store.parent.name, store))
    return out


def resolve_refs(conn: sqlite3.Connection, tids: Iterable[str]) -> dict[str, RefResolution]:
    """Resolve each ``t_<hex>`` id to ``present`` / ``absent`` / ``unknown`` across the host.

    The completing connection's board is searched FIRST, in-process and without I/O (it is the
    board the card lives on, so it answers the overwhelming majority). Only ids that miss there
    cost a sibling open, and every sibling store is opened with a read-only URI and one
    ``SELECT id FROM tasks WHERE id IN (...)``: ``kanban_db.connect`` is never used on a board
    that is not the caller's, because a connect initialises, migrates and can write.

    A store that raises on open or read is recorded in ``unknown_boards`` and contributes NO
    evidence. ``absent`` therefore requires that every store TOUCHED answered - and an id is
    only ever ``absent`` while nothing is unknown.
    """
    ordered = [tid for tid in dict.fromkeys(tids) if tid]
    if not ordered:
        return {}
    kb = _kb()
    deadline = time.monotonic() + REF_TIMEOUT_SECONDS
    here = None
    try:
        here = kb.board_for_connection(conn)
    except Exception:  # pragma: no cover - defensive
        here = None
    own_path = _connection_path(conn)
    own_label = here or (str(own_path) if own_path else None)

    try:
        missing = list(kb._missing_task_ids(conn, ordered))
    except sqlite3.Error:  # pragma: no cover - a broken local store is not a verdict
        missing = list(ordered)
        searched: list[str] = []
        unknown: list[str] = [own_label or "<connection>"]
        found: dict[str, Optional[str]] = {}
        return {tid: RefResolution(tid, "unknown", None, tuple(searched), tuple(unknown))
                for tid in ordered}

    searched = [own_label] if own_label else []
    unknown: list[str] = []
    found: dict[str, Optional[str]] = {tid: here for tid in ordered if tid not in missing}
    remaining = missing
    timed_out = False

    for slug, path in _board_store_paths():
        if not remaining:
            break
        try:
            if own_path is not None and path.is_file() and path.resolve() == own_path:
                continue
            if not path.is_file():
                continue
        except OSError:  # pragma: no cover - defensive
            continue
        if time.monotonic() > deadline:
            timed_out = True
            break
        try:
            sibling = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True,
                                      timeout=0.4)
        except sqlite3.Error:
            unknown.append(slug)
            continue
        try:
            sibling.row_factory = sqlite3.Row
            # A store that OPENS but carries no ``tasks`` table is not an outage: it is a store
            # holding no cards, and such a store ANSWERS "not here" rather than "I could not be
            # read". The distinction is load-bearing, not pedantic: ``boards/_archived/kanban.db``
            # on this host is an initialised-but-empty husk with no ``tasks`` table, so counting a
            # schema-less store as ``unknown`` made EVERY phantom ref ``unknown`` and turned Gate
            # E's refusal arm into an advisory (measured live 2026-10-25 on card t_0608ce6d: with
            # the husk counted, t_deadbeefdeadbeef was ADMITTED as unverified). An actual open or
            # read failure still lands in ``unknown_boards`` below - the outage rule is untouched.
            if sibling.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'"
            ).fetchone() is None:
                searched.append(slug)
                continue
            still = list(kb._missing_task_ids(sibling, remaining))
            for tid in set(remaining) - set(still):
                found[tid] = slug
            remaining = still
            searched.append(slug)
        except sqlite3.Error:
            unknown.append(slug)
        finally:
            try:
                sibling.close()
            except Exception:  # pragma: no cover - defensive
                pass

    out: dict[str, RefResolution] = {}
    for tid in ordered:
        if tid in found:
            out[tid] = RefResolution(tid, "present", found[tid], tuple(searched), tuple(unknown))
            continue
        state = "unknown" if (unknown or timed_out) else "absent"
        out[tid] = RefResolution(tid, state, None, tuple(searched), tuple(unknown))
    return out


# -- the write seam --------------------------------------------------------------------------


def _record(conn: sqlite3.Connection, task_id: str, kind: str, payload: dict) -> None:
    """Append ONE event in its own tiny txn.

    The gates run BEFORE the completion's write txn, so the card is untouched when one refuses;
    the event is what makes the refusal auditable (the same shape ``_gate_deploy_proof`` and
    ``_gate_empty_completion`` already use).
    """
    kb = _kb()
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, kind, payload)


def _refs_in(summary: Optional[str], result: Optional[str],
             verified_cards: Iterable[str] = ()) -> list[str]:
    """The ``t_<hex>`` ids the handoff cites, minus the ones it just verified it created."""
    text = "\n".join(part for part in (result, summary) if part)
    if not text:
        return []
    verified = set(verified_cards or ())
    return [tid for tid in dict.fromkeys(_TASK_ID_PROSE_RE.findall(text)) if tid not in verified]


def gate_completion_refs(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    summary: Optional[str] = None,
    result: Optional[str] = None,
    verified_cards: Iterable[str] = (),
    force: bool = False,
) -> None:
    """Gate E: a cited card id that resolves nowhere refuses the completion.

    * ``present``  -> admit (the common case; zero sibling I/O).
    * ``absent``   -> refuse with :class:`PhantomRefsRefused` naming each id and the boards
                      searched, or - in ``measure`` mode - record and admit.
    * ``unknown``  -> NEVER refuse; record ``phantom_refs_unverified`` so resolver coverage gaps
                      are visible on the card and countable in ``kanban gates report``.
    * no refs      -> no-op, so the overwhelming majority of completions pay nothing.
    """
    mode = discharge_gate_mode()
    if mode == "off":
        return
    refs = _refs_in(summary, result, verified_cards)
    if not refs:
        return
    resolutions = resolve_refs(conn, refs)
    absent = [tid for tid in refs if resolutions[tid].state == "absent"]
    unknown = [tid for tid in refs if resolutions[tid].state == "unknown"]
    searched = list(resolutions[refs[0]].boards_searched)
    unknown_boards = list(resolutions[refs[0]].unknown_boards)

    if force:
        _record(conn, task_id, EVENT_WAIVED_REFS, {
            "cause": "force",
            "detail": "operator override: the discharge gate was bypassed explicitly by the caller",
            "absent": absent, "unknown": unknown, "boards_searched": searched,
        })
        return
    if unknown:
        _record(conn, task_id, EVENT_UNVERIFIED_REFS, {
            "unverified": unknown,
            "unknown_boards": unknown_boards,
            "boards_searched": searched,
            "detail": "a board store could not be read, so these refs are UNKNOWN, not absent",
        })
    if absent:
        payload: dict[str, Any] = {
            "phantom_refs": absent,
            "boards_searched": searched,
            "unknown_boards": unknown_boards,
        }
        if mode == "measure":
            payload["mode"] = "measure"
            _record(conn, task_id, EVENT_MEASURE_MODE, {"gate": "phantom_refs", **payload})
            return
        _record(conn, task_id, EVENT_REFUSED_REFS, payload)
        raise PhantomRefsRefused(absent, boards_searched=searched, task_id=task_id)


# -- gate F: a void claim must carry the measurement it is void by ----------------------------

_TARGET_MISSING = object()


def _first_line(text: Optional[str]) -> str:
    raw = (text or "").strip()
    return raw.splitlines()[0] if raw else ""


def _observed_path(target: str) -> tuple[bool, str]:
    """``path_absent``: the kernel asks the filesystem, with ``lexists`` (a dangling symlink IS
    a present path, and a publisher that copies the working tree would copy it)."""
    exists = os.path.lexists(target)
    return (not exists), ("present" if exists else "absent")


def _observed_glob(root: str, pattern: str) -> tuple[bool, str]:
    base = Path(root)
    if not base.is_dir():
        raise ValueError(f"root {root!r} is not an existing directory")
    matches = sorted(str(p) for p in base.glob(pattern))
    if matches:
        shown = ", ".join(matches[:3]) + (" ..." if len(matches) > 3 else "")
        return False, f"{len(matches)} match(es): {shown}"
    return True, "no match"


def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    # ``encoding`` pinned: the repo's text-mode rule (and it is right - a macOS default of
    # ascii would blow up on any path with a non-ascii byte in a commit message).
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=15, check=False)


def _observed_git_absent(repo: str, path: str) -> tuple[bool, str]:
    """``git_absent``: absent from ``HEAD`` AND from the working tree.

    Both halves are load-bearing. The publisher copies the WORKING TREE, so a file deleted from
    the index but still sitting untracked (or staged-deleted but present on disk) re-creates
    exactly what the card claimed was gone - the ``t_241c8074`` shape, measured live.
    """
    base = Path(repo)
    if not base.is_dir():
        raise ValueError(f"repo {repo!r} is not a directory")
    if shutil.which("git") is None:  # pragma: no cover - git is a hard dependency of the host
        raise ValueError("git is not on PATH")
    tracked = _git(repo, "cat-file", "-e", f"HEAD:{path}")
    worktree_present = os.path.lexists(str(base / path))
    if tracked.returncode == 0:
        sha = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip() or "?"
        return False, f"present at HEAD {sha}"
    if worktree_present:
        return False, "absent from HEAD but present in the working tree"
    return True, "absent from HEAD and the working tree"


def _observed_card_terminal(conn: sqlite3.Connection, card: str) -> tuple[bool, str]:
    """``card_terminal``: the cited card resolves present and is ``done`` or ``archived``."""
    resolution = resolve_refs(conn, [card])[card]
    if resolution.state == "unknown":
        why = ", ".join(resolution.unknown_boards) or "deadline"
        return False, f"unknown (could not read board store(s): {why})"
    if resolution.state == "absent":
        return False, "absent (no board carries this card)"
    board = resolution.board
    status = None
    # The completing board first, in-process: that is where the overwhelming majority of cited
    # cards live, and it costs one indexed read rather than a second connection.
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (card,)).fetchone()
    if row is not None:
        status = row["status"]
    elif board:
        try:
            from hermes_cli.kanban_register import open_board_ro

            sib = open_board_ro(str(board))
            if sib is not None:
                try:
                    row = sib.execute("SELECT status FROM tasks WHERE id = ?", (card,)).fetchone()
                    status = row["status"] if row is not None else None
                finally:
                    sib.close()
        except Exception:  # pragma: no cover - defensive
            status = None
    if status is None:
        return False, f"present on board {board!r} but its status could not be read"
    return status in ("done", "archived"), f"status {status} on board {board!r}"


def _observed_run(conn: sqlite3.Connection, rest: dict) -> tuple[bool, str]:
    """``run_ok``: delegated to the proof gate's witness test (same stores, same vocabulary)."""
    try:
        from hermes_cli import kanban_proof_gate as proof_gate
    except Exception as exc:
        raise ValueError(f"the proof gate is not loadable ({type(exc).__name__})") from exc
    verdict = proof_gate.witness(rest, conn=conn)
    if verdict.ok:
        return True, str(verdict.facts.get("witness") or "green")
    return False, f"{verdict.cause}: {verdict.detail}"


def _evaluate(conn: sqlite3.Connection, measurement: dict) -> tuple[bool, str, str]:
    """-> ``(holds, target, observed)`` for one measurement; raises ``ValueError`` when invalid."""
    kind = measurement.get("kind")
    if not isinstance(kind, str) or kind not in MEASUREMENT_KINDS:
        raise ValueError(
            f"measurement kind {kind!r} is not one of {', '.join(MEASUREMENT_KINDS)}")
    if kind == "path_absent":
        target = measurement.get("target")
        if not isinstance(target, str) or not os.path.isabs(target):
            raise ValueError(f"path_absent needs an absolute ``target``, got {target!r}")
        holds, observed = _observed_path(target)
        return holds, target, observed
    if kind == "glob_absent":
        root, pattern = measurement.get("root"), measurement.get("pattern")
        if not isinstance(root, str) or not isinstance(pattern, str) or not pattern:
            raise ValueError("glob_absent needs ``root`` and ``pattern`` strings")
        holds, observed = _observed_glob(root, pattern)
        return holds, f"{root}/{pattern}", observed
    if kind == "git_absent":
        repo, path = measurement.get("repo"), measurement.get("path")
        if not isinstance(repo, str) or not isinstance(path, str) or not path:
            raise ValueError("git_absent needs ``repo`` and ``path`` strings")
        holds, observed = _observed_git_absent(repo, path)
        return holds, f"{repo}:{path}", observed
    if kind == "card_terminal":
        card = measurement.get("card")
        if not isinstance(card, str) or not card.startswith("t_"):
            raise ValueError(f"card_terminal needs a ``card`` id, got {card!r}")
        holds, observed = _observed_card_terminal(conn, card)
        return holds, card, observed
    rest = {k: v for k, v in measurement.items() if k != "kind"}
    if not rest:
        raise ValueError("run_ok needs a run or probe payload beside ``kind``")
    if "run" not in rest and "probe" not in rest:
        # The flat spelling ``{"kind": "run_ok", "store": ..., "id": ...}`` is the same claim;
        # the witness test reads the nested record shape, so normalise rather than refuse.
        rest = {"run": rest}
    holds, observed = _observed_run(conn, rest)
    return holds, ", ".join(f"{k}={v}" for k, v in rest.items()), observed


def _invalid(detail: str, *, task_id: str) -> VoidClaimRefused:
    return VoidClaimRefused(
        f"{detail}. The schema is metadata[\"premise_void\"] = {{\"claim\": \"<what is void>\", "
        f"\"measurements\": [{{\"kind\": \"path_absent|glob_absent|git_absent|card_terminal|"
        f"run_ok\", ...}}]}} - every measurement must hold, and unknown keys are refused",
        task_id=task_id)


def gate_void_claim(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    summary: Optional[str] = None,
    result: Optional[str] = None,
    metadata: Optional[dict] = None,
    force: bool = False,
) -> None:
    """Gate F: a completion that argues its premise is void must measure that premise.

    A ``VOID_CLAIM_RE`` match on the ``result`` in full or the FIRST LINE of ``summary`` is the
    trigger. It is a trigger, not a verdict: on a match the completion must carry
    ``metadata["premise_void"]`` and the kernel evaluates every declared measurement. All hold ->
    admit and record ``void_claim_measured``. Any fails -> raise, naming claim, kind, target and
    the observed state, BEFORE anything is written.
    """
    mode = discharge_gate_mode()
    if mode == "off":
        return
    haystack = "\n".join(part for part in (result or "", _first_line(summary)) if part)
    if not haystack:
        return
    match = VOID_CLAIM_RE.search(haystack)
    if match is None:
        return
    quoted = _first_line(match.group(0))

    block = metadata.get("premise_void") if isinstance(metadata, dict) else None
    if force:
        _record(conn, task_id, EVENT_WAIVED_VOID, {
            "cause": "force", "matched": quoted,
            "detail": "operator override: the discharge gate was bypassed explicitly by the caller",
        })
        return
    if not isinstance(block, dict):
        payload = {"cause": "premise_void_missing", "matched": quoted, "mode": mode}
        if mode == "measure":
            payload["mode"] = "measure"
            _record(conn, task_id, EVENT_MEASURE_MODE, {"gate": "void_claims", **payload})
            return
        _record(conn, task_id, EVENT_REFUSED_VOID, payload)
        raise VoidClaimUnmeasured(
            f"this completion closes on a void claim ({quoted!r}) and declares no "
            f"measurement for it", task_id=task_id)

    unknown_keys = sorted(set(block) - set(BLOCK_KEYS))
    if unknown_keys:
        exc = _invalid(f"premise_void carries unknown key(s): {', '.join(unknown_keys)}",
                       task_id=task_id)
        _record(conn, task_id, EVENT_REFUSED_VOID, {
            "cause": "measurement_invalid", "matched": quoted, "detail": str(exc)})
        raise exc

    basis = block.get("basis")
    if basis is not None:
        if basis != DISCLAIMER_BASIS:
            exc = _invalid(f"premise_void.basis {basis!r} is not a recognised basis",
                           task_id=task_id)
            _record(conn, task_id, EVENT_REFUSED_VOID, {"cause": "measurement_invalid",
                                                        "matched": quoted, "detail": str(exc)})
            raise exc
        why = block.get("why")
        why_text = " ".join(why.split()) if isinstance(why, str) else ""
        if len(why_text) < DISCLAIMER_WHY_MIN:
            exc = _invalid(
                f"premise_void.basis={DISCLAIMER_BASIS!r} needs a real ``why`` of at least "
                f"{DISCLAIMER_WHY_MIN} characters (got {len(why_text)})", task_id=task_id)
            _record(conn, task_id, EVENT_REFUSED_VOID, {"cause": "measurement_invalid",
                                                        "matched": quoted, "detail": str(exc)})
            raise exc
        _record(conn, task_id, EVENT_DISCLAIMED_VOID, {
            "matched": quoted, "why": why_text,
            "detail": "the phrase is incidental and not the basis of this close",
        })
        return

    claim = block.get("claim")
    claim_text = " ".join(claim.split()) if isinstance(claim, str) else ""
    if not claim_text:
        exc = _invalid("premise_void carries no ``claim``", task_id=task_id)
        _record(conn, task_id, EVENT_REFUSED_VOID, {"cause": "measurement_invalid",
                                                    "matched": quoted, "detail": str(exc)})
        raise exc
    measurements = block.get("measurements")
    if not isinstance(measurements, list) or not measurements:
        exc = _invalid("premise_void carries no ``measurements``", task_id=task_id)
        _record(conn, task_id, EVENT_REFUSED_VOID, {"cause": "measurement_invalid",
                                                    "matched": quoted, "detail": str(exc)})
        raise exc

    observed: list[dict] = []
    failures: list[str] = []
    for measurement in measurements:
        if not isinstance(measurement, dict):
            exc = _invalid("every measurement must be an object", task_id=task_id)
            _record(conn, task_id, EVENT_REFUSED_VOID, {"cause": "measurement_invalid",
                                                        "matched": quoted, "detail": str(exc)})
            raise exc
        try:
            holds, target, state = _evaluate(conn, measurement)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            detail = str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__}: {exc}"
            refusal = _invalid(
                f"measurement {measurement.get('kind')!r} on {claim_text!r} is not measurable "
                f"({detail})", task_id=task_id)
            _record(conn, task_id, EVENT_REFUSED_VOID, {
                "cause": "measurement_invalid", "claim": claim_text, "matched": quoted,
                "kind": measurement.get("kind"), "detail": detail})
            raise refusal from exc
        row = {"kind": measurement.get("kind"), "target": target, "observed": state,
               "holds": holds}
        observed.append(row)
        if not holds:
            failures.append(f"{row['kind']} on {target}: {state}")

    if failures:
        payload = {"cause": "measurement_does_not_hold", "claim": claim_text,
                   "matched": quoted, "observed": observed}
        if mode == "measure":
            payload["mode"] = "measure"
            _record(conn, task_id, EVENT_MEASURE_MODE, {"gate": "void_claims", **payload})
            return
        _record(conn, task_id, EVENT_REFUSED_VOID, payload)
        raise VoidClaimUnmeasured(
            f"the void claim {claim_text!r} does not hold - " + "; ".join(failures),
            task_id=task_id, claim=claim_text)

    _record(conn, task_id, EVENT_MEASURED_VOID, {
        "claim": claim_text, "kinds": [row["kind"] for row in observed], "observed": observed,
    })


def gate_completion_discharge(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    summary: Optional[str] = None,
    result: Optional[str] = None,
    metadata: Optional[dict] = None,
    verified_cards: Iterable[str] = (),
    force: bool = False,
) -> None:
    """Both halves at the seam: Gate E (refs) then Gate F (void claims).

    Called from ``complete_task`` after ``_gate_landing_carrier`` and before the completion
    write txn, so a refusal leaves the card byte-untouched. ``force`` is the OPERATOR override
    (an explicit argument, exactly like the deploy-proof and landing-carrier gates); the evidence
    gate's own ``force`` semantics are untouched by this door.
    """
    gate_completion_refs(conn, task_id, summary=summary, result=result,
                         verified_cards=verified_cards, force=force)
    gate_void_claim(conn, task_id, summary=summary, result=result, metadata=metadata,
                    force=force)


# -- the advisory arm (measure mode of the old scan, rewired onto the one resolver) -----------


def flag_phantom_prose_refs(
    conn: sqlite3.Connection, task_id: str, run_id: Optional[int],
    summary: Optional[str], result: Optional[str], verified_cards: list[str],
) -> None:
    """Post-commit ADVISORY scan (``_flag_phantom_prose_refs``'s body, one resolver).

    Runs after the card is durable, so it never blocks; its payload names the boards searched
    and the boards it could not read, so an operator can tell a real cross-board card from an
    invented id without re-deriving the resolver.
    """
    refs = _refs_in(summary, result, verified_cards)
    if not refs:
        return
    if discharge_gate_mode() == "off":
        # ``off`` is SILENT for the whole gate, advisory arm included: an operator who turns the
        # gate off means "no discharge-gate event of any kind", not "keep writing one kind".
        return
    resolutions = resolve_refs(conn, refs)
    phantom = [tid for tid in refs if resolutions[tid].state == "absent"]
    unknown = [tid for tid in refs if resolutions[tid].state == "unknown"]
    if not phantom and not unknown:
        return
    first = resolutions[refs[0]]
    payload: dict[str, Any] = {
        "phantom_refs": phantom,
        "source": "completion_summary",
        "boards_searched": list(first.boards_searched),
    }
    if unknown:
        payload["unknown_boards"] = list(first.unknown_boards)
        payload["unverified"] = unknown
    matched = {tid: res.board for tid, res in resolutions.items()
               if res.state == "present" and res.board}
    if matched:
        payload["matched_elsewhere"] = matched
    kb = _kb()
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, "suspected_hallucinated_references", payload,
                         run_id=run_id)


# -- visibility: the two families, counted from task_events ----------------------------------

_FAMILY_LABELS = {
    EVENT_REFUSED_REFS: "refused",
    EVENT_UNVERIFIED_REFS: "unverified",
    EVENT_WAIVED_REFS: "waived",
    EVENT_REFUSED_VOID: "refused",
    EVENT_MEASURED_VOID: "measured",
    EVENT_DISCLAIMED_VOID: "disclaimed",
    EVENT_WAIVED_VOID: "waived",
}

_FAMILY_COUNTERS = {
    "phantom_refs": ("refused", "unverified", "measured", "waived"),
    "void_claims": ("refused", "measured", "disclaimed", "waived"),
}


def event_families(conn: sqlite3.Connection, *, limit: int = 20) -> dict:
    """The ``phantom_refs`` / ``void_claims`` families, counted from THIS board's events.

    A gate whose refusals nobody can count is a gate nobody can audit, so the report reads the
    gate's OWN event log rather than re-deriving anything: ``refused`` / ``unverified`` /
    ``measured`` / ``disclaimed`` / ``waived``, plus the ``measure``-mode admissions (the soak
    signal that says how loud the gate would be if armed).
    """
    kinds = (*_REF_EVENT_KINDS, *_VOID_EVENT_KINDS, EVENT_MEASURE_MODE)
    placeholders = ", ".join("?" for _ in kinds)
    counts = {name: {counter: 0 for counter in counters}
              for name, counters in _FAMILY_COUNTERS.items()}
    counts["phantom_refs"]["measure_mode"] = 0
    counts["void_claims"]["measure_mode"] = 0
    recent: list[dict] = []
    try:
        rows = conn.execute(
            "SELECT id, task_id, kind, payload, created_at FROM task_events "
            f"WHERE kind IN ({placeholders}) ORDER BY id DESC LIMIT 500", kinds,
        ).fetchall()
    except sqlite3.Error:
        return {"mode": discharge_gate_mode(), "families": counts, "recent": recent,
                "total": 0, "armed": False}

    for row in rows:
        kind = row["kind"]
        family = "void_claims" if kind in _VOID_EVENT_KINDS else "phantom_refs"
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except ValueError:
            payload = {}
        if kind == EVENT_MEASURE_MODE:
            # The measure-mode event is SHARED by both gates, so its family comes from the
            # payload, never from the kind: reading it off the kind would file every void-claim
            # soak under ``phantom_refs`` and leave the other family looking silent.
            gate = str(payload.get("gate") or "")
            if gate not in ("phantom_refs", "void_claims"):
                continue
            family = gate
            counter = "measure_mode"
        else:
            counter = _FAMILY_LABELS.get(kind)
        if counter and counter in counts[family]:
            counts[family][counter] += 1
        if len(recent) < limit:
            recent.append({"task_id": row["task_id"], "kind": kind, "counter": counter,
                           "created_at": row["created_at"]})
    return {
        "mode": discharge_gate_mode(),
        "families": counts,
        # Every recorded outcome, measure-mode admissions included: a soak that is not counted
        # reads as silence, which is exactly what the switch must not look like.
        "total": sum(sum(counts[family].values()) for family in counts),
        "recent": recent,
        "armed": True,
        "counters": {name: list(counters) for name, counters in _FAMILY_COUNTERS.items()},
    }
