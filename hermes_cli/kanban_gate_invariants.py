"""kanban_gate_invariants — the invariants the board enforces at its own write doors.

Three classes of defect used to be visible only in a report a human had to read and then act
on by hand. Each is now (a) MEASURED by ``hermes kanban gates report``, (b) REFUSED at the
seam that can produce it, and (c) repaired, where the repair is deterministic, by
``hermes kanban gates reconcile``:

``assignee``   (Invariant B) a row may not be assigned to a handle that cannot run.
``dependency`` (Invariant C) a block that names the card it waits on must carry the edge.
``evidence``   (Invariant A) a completion must declare the evidence behind it.

The four rules that shape every choice here:

1. **The disk is truth, never a name list.** Whether a handle can run is answered by the
   profiles on disk plus a lane registry ON DISK — not by a hardcoded set of names that a
   rename silently invalidates. A profile that appears on disk resolves immediately; a lane
   that is pulled instead of spawned is DECLARED, in
   ``<kanban_home>/kanban/terminal_lanes`` or the board's own ``terminal_lanes`` key.
2. **A violation is refused, not warned about.** The predecessor of this module warned at
   create time (``assignee_not_spawnable``) and the measured result was 361 rows assigned to
   handles that resolve nowhere and 15 cards stranded on two retired lanes. A report that
   changes nothing is not an enforcement mechanism.
3. **A wall is not a gate.** Every refuse has a documented, cheap, honest path beside it:
   declare the lane, alias the handle, pass ``waits_on``, declare the evidence class. The
   refusal message names that path; a refusal a caller cannot act on is a dead end.
4. **No silent failure, and no silent waiver.** A gate switched to measure mode records the
   switch on every card it lets through (``evidence_gate_measure``), so a disabled gate is
   visible in the card's own history instead of reading exactly like a clean pass.

Public surface: :func:`gate_assignee`, :func:`gate_dependency`, :func:`gate_completion_evidence`
(the three write doors), :func:`report` / :func:`reconcile` (the ``hermes kanban gates`` verb),
and the three ``*_violations`` measurements ``report`` is built from.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Optional

__all__ = [
    "AssigneeRefused", "DependencyRefused", "EvidenceRefused",
    "assignee_violations", "dependency_violations", "evidence_violations",
    "gate_assignee", "gate_dependency", "gate_completion_evidence",
    "report", "reconcile", "terminal_lanes", "resolve_assignee",
    "evidence_gate_mode", "parse_evidence", "EVIDENCE_SHAPE",
]

# A card id, anywhere in prose. Deliberately the same shape the rest of the kernel uses for
# an unresolvable reference in a handoff.
CARD_REF_RE = re.compile(r"\bt_[0-9a-f]{6,}\b")

# --- board/disk vocabulary ---------------------------------------------------------------

ASSIGNEE_ALIASES_KEY = "assignee_aliases"
TERMINAL_LANES_KEY = "terminal_lanes"
EVIDENCE_GATE_KEY = "evidence_gate"
EVIDENCE_GATE_SINCE_KEY = "evidence_gate_since"

#: ``<kanban_home>/kanban/terminal_lanes`` — one lane name per line, ``#`` starts a comment.
TERMINAL_LANES_FILE = "terminal_lanes"
#: ``<kanban_home>/kanban/assignee_aliases.json`` — ``{"<retired>": "<the lane that owns it>"}``.
ASSIGNEE_ALIASES_FILE = "assignee_aliases.json"
#: ``<kanban_home>/kanban/evidence_gate`` — ``refuse`` (default) or ``measure``.
EVIDENCE_GATE_FILE = "evidence_gate"

#: Kill switch for Invariant A. ``measure`` disables the refusal and keeps the measurement.
EVIDENCE_GATE_ENV = "HERMES_KANBAN_EVIDENCE_GATE"
EVIDENCE_GATE_MODES = ("refuse", "measure")
EVIDENCE_GATE_DEFAULT = "refuse"

EVIDENCE_CLASSES = ("run", "probe", "none")
EVIDENCE_SHAPE = (
    'metadata={"evidence": {"class": "run", "run": {"store": "yoyoflow"|"card", "id": <id>}}} '
    '| {"class": "probe", "probe": {"at": "<iso8601>", "result": "ok", '
    '"observations": ["<behaviour exercised>"]}} '
    '| {"class": "none", "why": "<one line: why this work has no run behind it>"}'
)

#: The event kinds that make a completion's evidence class readable back off the card. A
#: completion is A-clean iff it carries at least one of these (written by the write door).
EVIDENCE_EVENT_KINDS = (
    "evidence_declared",     # class run/probe, resolved and green
    "evidence_none",         # class none, with the stated why
    "evidence_waived",       # operator force
    "evidence_exempt",       # a review approval (a judgement on someone else's claim)
    "proof_gate_admitted",   # a `landed` card whose proof block passed the existing gate
    "evidence_gate_measure",  # the gate was in measure mode: recorded, never silent
)

#: Statuses a stranded row is worth acting on.
OPEN_STATUSES = ("ready", "todo", "running", "review", "blocked", "triage")


# --- refusals -----------------------------------------------------------------------------
#
# All three are ``ValueError`` subclasses on purpose: the CLI funnels ValueError as a
# VALIDATION refusal (`kanban: <message>`) rather than an internal error, and the same text
# reaches the kanban_* tools. The precedent is ``CreatedBlockedRefused``.

class GateRefused(ValueError):
    """Base: a write door refused because an invariant would have been violated."""

    invariant = "gate"

    def __init__(self, message: str, *, task_id: Optional[str] = None,
                 facts: Optional[dict] = None) -> None:
        super().__init__(message)
        self.task_id = task_id
        self.facts = facts or {}


class AssigneeRefused(GateRefused):
    """Invariant B: the assignee resolves to nothing that can run."""

    invariant = "assignee"


class DependencyRefused(GateRefused):
    """Invariant C: a block names a dependency it does not carry an edge for."""

    invariant = "dependency"


class EvidenceRefused(GateRefused):
    """Invariant A: the completion carries no evidence it can point at."""

    invariant = "evidence"

    def __init__(self, message: str, *, cause: str = "no_evidence", **kw) -> None:
        super().__init__(message, **kw)
        self.cause = cause


# --- small helpers ------------------------------------------------------------------------

def _kb():
    """The kernel module, imported lazily (``kanban_db`` imports this module's hooks)."""
    from hermes_cli import kanban_db as kb
    return kb


def _kanban_home() -> Path:
    return _kb().kanban_home()


def _board_meta(board: Optional[str]) -> dict:
    try:
        return _kb().read_board_metadata(board) or {}
    except Exception:  # a malformed board.json must never take the write door down
        return {}


def _registry_names(value: Any) -> list[str]:
    """``"a b"`` / ``["a", "b"]`` / ``"a,b"`` -> ``["a", "b"]``, lowercased."""
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p for p in re.split(r"[,\s]+", value) if p]
    elif isinstance(value, Iterable):
        parts = [str(p) for p in value]
    else:
        return []
    return [p.strip().lower() for p in parts if p.strip()]


def _read_names_file(path: Path) -> list[str]:
    try:
        if not path.is_file():
            return []
        out = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.split("#", 1)[0].strip().lower()
            if line:
                out.append(line)
        return out
    except OSError:
        return []


def _read_json_file(path: Path) -> dict:
    try:
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json_file(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# --- Invariant B: the assignee can run -----------------------------------------------------

def terminal_lanes(board: Optional[str] = None) -> set:
    """Lanes a board may assign to WITHOUT a profile on disk: a declared, pull-based lane.

    Read from disk, never from a name list: ``<kanban_home>/kanban/terminal_lanes`` (one name
    per line) plus the board's own ``terminal_lanes`` key in ``board.json``. Declaring a lane
    here is the honest path for a control-plane/terminal assignee that pulls its own work
    instead of being spawned; a handle declared in NEITHER place is a typo or a retired lane,
    and that is exactly the class this invariant exists to catch.
    """
    names = set(_read_names_file(_kanban_home() / "kanban" / TERMINAL_LANES_FILE))
    names.update(_registry_names(_board_meta(board).get(TERMINAL_LANES_KEY)))
    return names


def profile_roster() -> list:
    """The profiles that exist on disk, minus the implicit root profile.

    ``list_profiles_on_disk`` always includes ``default`` — the root profile, present even in a
    bare ``$HOME/.hermes`` — so it is not evidence of a roster. This is the set the assignee
    invariant judges against: a board with no roster has nothing to judge BY, and a gate that
    refuses on an empty roster would refuse every name in every isolated harness while proving
    nothing. An empty roster makes the family stand down, loudly (``gates report`` prints the
    roster size as ``armed``), never silently.
    """
    try:
        names = set(_kb().list_profiles_on_disk())
    except Exception:
        return []
    names.discard("default")
    return sorted(names)


def resolve_assignee(name: Optional[str], *, board: Optional[str] = None) -> tuple[str, str]:
    """``("profile"|"terminal"|"unresolvable", why)`` for an assignee handle.

    Both resolvable halves are answered from the disk: the profile set comes from
    :func:`kanban_db.list_profiles_on_disk` (a ``profiles/<name>/config.yaml`` plus the
    implicit ``default``), and the terminal half from :func:`terminal_lanes`. Nothing here
    consults a remembered list of names, so a rename is picked up on the next call.
    """
    handle = str(name or "").strip().lower()
    if not handle:
        return ("unresolvable", "no assignee handle at all")
    if handle in terminal_lanes(board):
        return ("terminal", "declared pull lane (terminal_lanes, on disk)")
    on_disk = set()
    try:
        on_disk = set(_kb().list_profiles_on_disk())
    except Exception:
        on_disk = set()
    if handle in on_disk:
        return ("profile", "profile on disk")
    return ("unresolvable",
            "no profile directory on disk and no terminal_lanes declaration")


def gate_assignee(name: Optional[str], *, board: Optional[str] = None,
                  where: str = "assign") -> None:
    """Invariant B at the write door: refuse an assignee that cannot run.

    ``where`` names the seam for the refusal text (``create`` / ``assign`` / ``dispatch``).
    ``None``/empty is not an assignee and is left alone: unassigning a card is legal.

    Stand-down: with no profile roster and no declared lanes on disk there is nothing to judge
    BY, so the gate lets the write through and the board says so in ``gates report``
    (``armed.assignee_roster``). Production always has a roster; an isolated harness does not.
    """
    if name is None or not str(name).strip():
        return
    if not profile_roster() and not terminal_lanes(board):
        return
    kind, why = resolve_assignee(name, board=board)
    if kind != "unresolvable":
        return
    known = sorted(set(_kb().list_profiles_on_disk()) | terminal_lanes(board))
    raise AssigneeRefused(
        f"{where}: assignee {name!r} cannot run — {why}. "
        f"A card assigned to a handle that resolves nowhere is never dispatched and never "
        f"reported as failed: it strands silently on the board. "
        f"Fix it one of two ways: (a) assign to a profile that exists on disk "
        f"({', '.join(known)}); (b) if {name!r} is a lane that PULLS its own work instead of "
        f"being spawned, declare it — one name per line in "
        f"{_kanban_home() / 'kanban' / TERMINAL_LANES_FILE}, or the board's own "
        f"\"{TERMINAL_LANES_KEY}\" list in board.json. Nothing was written.",
        facts={"assignee": name, "where": where, "resolution": kind, "detail": why},
    )


def assignee_aliases(board: Optional[str] = None) -> dict:
    """``{"<retired handle>": "<the lane that owns its work>"}``, declared on disk.

    The honest path for a handle that is gone for good: a card stranded on it is REPAIRED
    (reassigned, attributed) by ``gates reconcile`` instead of being re-typed by hand. Board
    scope overrides the shared file.
    """
    aliases = {}
    for source in (_read_json_file(_kanban_home() / "kanban" / ASSIGNEE_ALIASES_FILE),
                   _board_meta(board).get(ASSIGNEE_ALIASES_KEY) or {}):
        if isinstance(source, dict):
            for old, new in source.items():
                old_l, new_l = str(old).strip().lower(), str(new or "").strip().lower()
                if old_l and new_l:
                    aliases[old_l] = new_l
    return aliases


def assignee_violations(conn: sqlite3.Connection, board: Optional[str] = None) -> list:
    """Every row whose assignee cannot run — MEASURED from the rows and the disk.

    Stands down (returns nothing) when the disk holds no roster and no declared lane: there is
    nothing on this machine to judge a handle BY, so every row would read as a violation and
    the family would be noise. ``gates report`` prints the roster size either way.
    """
    declared = terminal_lanes(board)
    roster = set(profile_roster())
    if not roster and not declared:
        return []
    on_disk = set()
    try:
        on_disk = set(_kb().list_profiles_on_disk())
    except Exception:
        on_disk = set()
    try:
        rows = conn.execute(
            "SELECT id, status, assignee, priority FROM tasks "
            " WHERE assignee IS NOT NULL AND TRIM(assignee) <> '' "
            " ORDER BY id",
        ).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for row in rows:
        handle = str(row["assignee"]).strip().lower()
        if handle in on_disk or handle in declared:
            continue
        out.append({
            "task_id": row["id"],
            "status": row["status"],
            "assignee": row["assignee"],
            "open": row["status"] in OPEN_STATUSES,
            "resolution": "unresolvable",
        })
    return out


# --- Invariant C: the block carries the edge ----------------------------------------------

def _card_exists(conn: sqlite3.Connection, task_id: str) -> bool:
    try:
        return conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is not None
    except sqlite3.Error:
        return False


def _parent_ids(conn: sqlite3.Connection, task_id: str) -> set:
    try:
        return {r["parent_id"] for r in conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ?", (task_id,)).fetchall()}
    except sqlite3.Error:
        return set()


def _last_event_payload(conn: sqlite3.Connection, task_id: str, kind: str) -> Optional[dict]:
    try:
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? "
            " ORDER BY created_at DESC, id DESC LIMIT 1", (task_id, kind)).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        data = json.loads(row["payload"]) if row["payload"] else None
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _block_state(conn: sqlite3.Connection, task_id: str) -> tuple[str, str]:
    """``(reason, kind)`` for a blocked card: its columns, else its newest ``blocked`` event."""
    reason, kind = "", ""
    try:
        row = conn.execute(
            "SELECT block_kind FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is not None and "block_kind" in row.keys():
            kind = str(row["block_kind"] or "").strip().lower()
    except sqlite3.Error:
        pass
    payload = _last_event_payload(conn, task_id, "blocked") or {}
    reason = str(payload.get("reason") or "")
    if not kind:
        kind = str(payload.get("kind") or payload.get("block_kind") or "").strip().lower()
    return reason, kind


def named_card_refs(text: Optional[str]) -> list:
    """Every ``t_<hex>`` card id in a block reason, in first-seen order, de-duplicated."""
    seen, out = set(), []
    for match in CARD_REF_RE.finditer(text or ""):
        ref = match.group(0)
        if ref not in seen:
            seen.add(ref)
            out.append(ref)
    return out


def dependency_violations(conn: sqlite3.Connection, board: Optional[str] = None) -> list:
    """Every BLOCKED card that names the card it waits on but carries no edge to it.

    The window this closes: a block written in prose (``"blocked on t_9e9ea756 finishing"``)
    with no edge. The board's own dependency machinery resumes a card when its PARENTS finish,
    so a prose-only wait is invisible to it — the card sits blocked after the thing it waited
    on shipped, and only a human reading the reason can take it off the shelf. ``board`` is
    accepted for signature parity; the measurement is always the one connection's board.
    """
    try:
        rows = conn.execute(
            "SELECT id, status FROM tasks WHERE status = 'blocked' ORDER BY id").fetchall()
    except sqlite3.Error:
        return []
    out = []
    for row in rows:
        task_id = row["id"]
        reason, kind = _block_state(conn, task_id)
        refs = named_card_refs(reason)
        if not refs:
            continue
        parents = _parent_ids(conn, task_id)
        unedged, unknown, shipped = [], [], []
        for ref in refs:
            if ref == task_id:
                continue
            if not _card_exists(conn, ref):
                unknown.append(ref)
                continue
            if ref in parents:
                continue
            unedged.append(ref)
            dep = conn.execute("SELECT status FROM tasks WHERE id = ?", (ref,)).fetchone()
            if dep is not None and dep["status"] in ("done", "archived"):
                shipped.append(ref)
        if not (unedged or unknown):
            continue
        out.append({
            "task_id": task_id,
            "block_kind": kind or None,
            "named": refs,
            "unedged": unedged,
            "unknown_refs": unknown,
            "dependencies_shipped": shipped,
            "reason_preview": " ".join(reason.split())[:200],
        })
    return out


def gate_dependency(conn: sqlite3.Connection, task_id: str, reason: Optional[str],
                    kind: Optional[str], waits_on: Any = None) -> list:
    """Invariant C at the write door: a block that names what it waits on carries the edge.

    ``waits_on`` is the STRUCTURED form and the only one that creates an edge: every named
    card must exist on this board, and each becomes a parent of the blocked card (the blocked
    card waits on it, so it is the gate). A ``kind='dependency'`` block that names no card at
    all, or a NAMED card that does not exist, is refused — nothing is written.

    A prose-only reference in the reason is refused for a ``dependency`` block (that block is
    CLAIMING to wait on a card) and is left alone for any other kind (a card id in a
    ``needs_input`` reason is not a dependency claim); both cases are measured by
    :func:`dependency_violations`, so neither is silent.
    """
    kb = _kb()
    named = _named_list(waits_on)
    kind_l = str(kind or "").strip().lower()
    if named:
        missing = [ref for ref in named if not _card_exists(conn, ref)]
        if missing:
            raise DependencyRefused(
                f"block {task_id}: waits_on names {', '.join(missing)}, which "
                f"{'is' if len(missing) == 1 else 'are'} not on this board. An edge to a card "
                f"that does not exist can never resolve, so the block would strand exactly the "
                f"way a prose-only block does. Fix the id, or drop it from waits_on. "
                f"Nothing was written.",
                task_id=task_id, facts={"waits_on": named, "missing": missing})
        for ref in named:
            try:
                if kb._would_cycle(conn, ref, task_id):
                    raise DependencyRefused(
                        f"block {task_id}: waits_on={ref!r} would create a cycle ({task_id} "
                        f"already gates {ref}). Nothing was written.",
                        task_id=task_id, facts={"waits_on": named, "cycle": ref})
            except sqlite3.Error:
                pass
        # Validation only: the EDGE is written by block_task itself, after the transition, so
        # the link cannot demote the card out from under the block's own guarded UPDATE.
        return named
    refs = named_card_refs(reason)
    if kind_l == "dependency" and refs:
        raise DependencyRefused(
            f"block {task_id}: kind='dependency' and the reason names {', '.join(refs)}, but "
            f"the block carries no edge — a prose-only wait is invisible to the board's own "
            f"dependency machinery, so this card would sit blocked after that work shipped. "
            f"Re-run with waits_on=[{', '.join(repr(r) for r in refs)}] (the edge is then "
            f"created and the block runs), or drop kind='dependency' if this is not a wait on "
            f"those cards. Nothing was written.",
            task_id=task_id, facts={"named": refs, "kind": kind_l})
    return []


def _named_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        raw = [p for p in re.split(r"[,\s]+", value) if p]
    elif isinstance(value, Iterable):
        raw = [str(p) for p in value]
    else:
        return []
    out = []
    for ref in (r.strip() for r in raw):
        if ref and ref not in out:
            out.append(ref)
    return out


# --- Invariant A: the completion declares its evidence --------------------------------------

def evidence_gate_mode(board: Optional[str] = None) -> str:
    """``refuse`` (default) or ``measure`` — the kill switch, read from disk.

    Precedence: the environment (``HERMES_KANBAN_EVIDENCE_GATE``) over the shared file
    ``<kanban_home>/kanban/evidence_gate`` over the board's ``evidence_gate`` key. An
    unrecognised value is IGNORED, never silently read as a waiver: a typo must not disable a
    gate. ``measure`` keeps the whole measurement and drops only the refusal, and every card
    it lets through records ``evidence_gate_measure``, so the switch is visible on the card.
    """
    env = os.environ.get(EVIDENCE_GATE_ENV, "").strip().lower()
    if env:
        if env in ("measure", "report", "warn", "off", "disabled"):
            return "measure"
        if env in ("refuse", "enforce", "on"):
            return "refuse"
    for source in (_read_names_file(_kanban_home() / "kanban" / EVIDENCE_GATE_FILE),
                   _registry_names(_board_meta(board).get(EVIDENCE_GATE_KEY))):
        for value in source:
            if value in EVIDENCE_GATE_MODES:
                return value
    return EVIDENCE_GATE_DEFAULT


def evidence_gate_since(board: Optional[str] = None) -> Optional[int]:
    """Epoch the evidence class started being MEASURED on this board, or ``None``.

    Without a stamp the family reports zero and says it is unarmed: a board with ten years of
    completions predating the gate must not read as ten years of violations. ``reconcile``
    stamps it, so the measurement starts at the moment the gate is armed.
    """
    raw = _board_meta(board).get(EVIDENCE_GATE_SINCE_KEY)
    try:
        if raw not in (None, ""):
            return int(raw)
    except (TypeError, ValueError):
        pass
    names = _read_names_file(_kanban_home() / "kanban" / EVIDENCE_GATE_SINCE_KEY)
    if names:
        try:
            return int(names[0])
        except ValueError:
            return None
    return None


def parse_evidence(value: Any) -> tuple:
    """``(class, payload, why)`` for a declared evidence block; raises on a malformed one.

    The declaration is deliberately small, because a gate whose honest path costs an argument
    is a gate that gets routed around: a run (``{"run": {"store": ..., "id": ...}}``), a live
    probe (``{"probe": {...}}``), or a one-line ``none`` with its ``why``.
    """
    if not isinstance(value, dict):
        raise EvidenceRefused(
            f"evidence must be an object; {EVIDENCE_SHAPE}", cause="evidence_malformed")
    declared = str(value.get("class") or "").strip().lower()
    if declared not in EVIDENCE_CLASSES:
        raise EvidenceRefused(
            f"evidence.class {value.get('class')!r} is not one of "
            f"{', '.join(EVIDENCE_CLASSES)}; {EVIDENCE_SHAPE}",
            cause="evidence_malformed")
    if declared == "none":
        why = " ".join(str(value.get("why") or "").split())
        if len(why) < 8:
            raise EvidenceRefused(
                "evidence.class='none' needs a real ``why`` — one line naming why this work "
                "has no run behind it (a skill edit, an investigation, board hygiene). An "
                "empty waiver is a silent failure with extra steps, so it is refused: "
                f"{EVIDENCE_SHAPE}",
                cause="evidence_malformed")
        return ("none", {}, why)
    payload = value.get(declared)
    if not isinstance(payload, dict) or not payload:
        payload = {k: v for k, v in value.items() if k != "class"}
    if not isinstance(payload, dict) or not payload:
        raise EvidenceRefused(
            f"evidence.class={declared!r} carries no {declared} payload; {EVIDENCE_SHAPE}",
            cause="evidence_malformed")
    # Both spellings are accepted, and normalised to the proof gate's record shape: the nested
    # ``{"class": "run", "run": {...}}``, and the flat ``{"class": "run", "store": ..., "id": ...}``.
    if declared not in payload:
        payload = {declared: payload}
    return (declared, payload, "")


def gate_completion_evidence(conn: sqlite3.Connection, task_id: str,
                             metadata: Optional[dict], *, force: bool = False) -> None:
    """Invariant A at the transition: a completion must declare the evidence behind it.

    Judged by CLASS, and every class is answered from a store, never from the handoff's prose:

    * ``run`` / ``probe`` — resolved through ``kanban_proof_gate``. An unresolvable run
      (``run_unknown``) or a run that is not green (``run_not_green``) is refused. A card turn
      is evidence about the card, not about live bytes.
    * ``none`` — the honest path for work that legitimately has no run behind it (a skill edit,
      an investigation, board hygiene). It costs one line, and it is RECORDED, with its why,
      on the card: a waiver is visible, not silent.
    * no declaration at all — refused, unless the card's own contract already makes it a
      ``landed`` claim (``_gate_deploy_proof`` owns that case and refuses for the same class
      of reason), or the gate is in ``measure`` mode (the kill switch), which is recorded.

    Structural exemptions: an approval out of ``review`` (a judgement on someone else's claim,
    whose own completion paid this gate) and ``force=True`` (the operator's override). Neither
    is a lane exemption — no lane may exempt itself, ``default`` included.
    """
    kb = _kb()
    row = conn.execute(
        "SELECT status, completion_contract FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return
    prior_status = row["status"]
    contract = row["completion_contract"] if "completion_contract" in row.keys() else None
    metadata = metadata if isinstance(metadata, dict) else {}

    from hermes_cli import kanban_proof_gate as proof_gate

    def record(kind: str, payload: dict) -> None:
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, kind, payload)

    if force:
        record("evidence_waived", {"cause": "force",
                                   "detail": "operator override: the completion gate was "
                                             "bypassed explicitly by the caller"})
        return
    if prior_status == "review":
        record("evidence_exempt", {"cause": "review_approval"})
        return

    declared = metadata.get("evidence")
    if declared is None:
        if proof_gate.is_landed_contract(contract) or metadata.get("proof"):
            # The landed contract owns this claim: _gate_deploy_proof judges the proof block
            # and refuses with the same class of reason. Judging it twice would only replace
            # a clause-specific refusal with a generic one.
            return
        if evidence_gate_mode() == "measure":
            record("evidence_gate_measure", {
                "detail": "no evidence declared, and the gate is in measure mode",
                "shape": EVIDENCE_SHAPE,
            })
            return
        record("completion_blocked_evidence", {"cause": "no_evidence", "mode": "refuse"})
        raise EvidenceRefused(
            f"complete {task_id}: REFUSED — this completion declares no evidence. "
            f"A self-report is not evidence, so the kernel will not close a card on prose "
            f"alone. Declare the class: {EVIDENCE_SHAPE}. Use class='none' with a one-line "
            f"``why`` for work that genuinely has no run behind it (a skill edit, an "
            f"investigation, board hygiene) — that path is deliberately cheap and it is "
            f"recorded on the card. Nothing was written; the card is exactly as it was.",
            cause="no_evidence", task_id=task_id)

    try:
        cls, payload, why = parse_evidence(declared)
    except EvidenceRefused as exc:
        record("completion_blocked_evidence",
               {"cause": exc.cause, "detail": str(exc), "declared": declared})
        raise
    if cls == "none":
        record("evidence_none", {"why": why, "shape": EVIDENCE_SHAPE})
        return

    verdict = proof_gate.witness(payload, conn=conn)
    record("evidence_declared" if verdict.ok else "completion_blocked_evidence", {
        "class": cls,
        "cause": verdict.cause,
        "detail": verdict.detail,
        "facts": verdict.facts,
    })
    if not verdict.ok:
        raise EvidenceRefused(
            f"complete {task_id}: REFUSED — the declared {cls} is not evidence this board can "
            f"resolve ({verdict.cause}): {verdict.detail}",
            cause=str(verdict.cause or "evidence_unresolved"), task_id=task_id,
            facts=verdict.facts)


def evidence_violations(conn: sqlite3.Connection, board: Optional[str] = None) -> list:
    """DONE cards, since the gate was armed, whose completion carries no evidence record.

    Measured from the card's own events: a completion is A-clean iff it wrote one of
    :data:`EVIDENCE_EVENT_KINDS`. Rows completed before the stamp are out of scope by
    construction — the stamp is what makes the class measurable instead of reading every
    historical completion as a violation.
    """
    since = evidence_gate_since(board)
    if since is None:
        return []
    placeholders = ", ".join("?" for _ in EVIDENCE_EVENT_KINDS)
    try:
        rows = conn.execute(
            "SELECT id, completed_at, assignee FROM tasks "
            " WHERE status = 'done' AND completed_at IS NOT NULL AND completed_at >= ? "
            "   AND NOT EXISTS (SELECT 1 FROM task_events e WHERE e.task_id = tasks.id "
            f"      AND e.kind IN ({placeholders})) "
            " ORDER BY completed_at DESC",
            (since, *EVIDENCE_EVENT_KINDS)).fetchall()
    except sqlite3.Error:
        return []
    return [{"task_id": r["id"], "completed_at": r["completed_at"],
             "assignee": r["assignee"], "cause": "no_evidence_record"} for r in rows]


# --- the pass: report + reconcile -----------------------------------------------------------

def _families(conn: sqlite3.Connection, board: Optional[str]) -> dict:
    # ``rank`` is measured by the rank-gate seam in kanban_db when that seam is present. It is
    # reported as UNAVAILABLE (never as a silent zero) when it is not, so a caller can tell a
    # clean board from a board whose rank invariant this build cannot measure.
    kb = _kb()
    measure = getattr(kb, "gate_violations", None)
    families = {
        "assignee": assignee_violations(conn, board),
        "dependency": dependency_violations(conn, board),
        "evidence": evidence_violations(conn, board),
    }
    if measure is not None:
        families = {"rank": measure(conn), **families}
    return families


def _counts(families: dict) -> dict:
    """Per-family counts; ``open`` counts only rows a lane is still waiting on."""
    rank = families.get("rank")
    return {
        "rank": ({"total": len(rank), "open": len(rank)} if rank is not None
                 else {"total": None, "open": None, "unavailable": True}),
        "assignee": {
            "total": len(families["assignee"]),
            "open": len([r for r in families["assignee"] if r.get("open")]),
        },
        "dependency": {"total": len(families["dependency"]),
                       "open": len(families["dependency"])},
        "evidence": {"total": len(families["evidence"]),
                     "open": len(families["evidence"])},
    }


def _board_slug(conn: sqlite3.Connection) -> str:
    """The board slug from the kanban_db accessor when present, else "" (unbound).

    The rank seam owns that accessor; a build without it still reports the other three
    families, bounded by whatever board slug the caller supplied.
    """
    fn = getattr(_kb(), "board_for_connection", None)
    if fn is None:
        return ""
    try:
        return fn(conn) or ""
    except Exception:
        return ""


def report(conn: sqlite3.Connection, board: Optional[str] = None) -> dict:
    """Measure all four invariants on this board. Read-only; changes nothing."""
    board = board or _board_slug(conn)
    families = _families(conn, board)
    counts = _counts(families)
    total_open = sum(c["open"] for c in counts.values() if isinstance(c["open"], int))
    return {
        "board": board,
        "action": "report",
        "armed": {"evidence_gate": evidence_gate_mode(board),
                  "evidence_since": evidence_gate_since(board),
                  "assignee_roster": len(profile_roster()),
                  "terminal_lanes": len(terminal_lanes(board))},
        "counts": counts,
        "violations_total": sum(c["total"] for c in counts.values()
                                if isinstance(c["total"], int)),
        "open_total": total_open,
        "violations": {
            "rank": families.get("rank", []),
            "assignee": families["assignee"],
            "dependency": families["dependency"],
            "evidence": families["evidence"],
        },
    }


def reconcile(conn: sqlite3.Connection, board: Optional[str] = None, *,
              cause: str = "reconcile") -> dict:
    """Repair what is deterministic, then RE-MEASURE. Never guesses.

    * ``assignee`` — a stranded row whose handle is declared in an alias map is REASSIGNED to
      the lane that owns it (attributed ``assignee_aliased``); one with no declared owner is
      left exactly as it is and named in ``unrepaired``. Re-typing it would be a guess about
      who owns the work.
    * ``dependency`` — a ``kind='dependency'`` block that names an existing card gets the edge
      it is missing (``dependency_edge_created``), which is what lets the board's own
      dependency machinery resume it. A block of any other kind is named, not modified: a card
      id in a ``needs_input`` reason is not a claim of what the card waits on.
    * ``evidence`` — nothing to repair retroactively; the pass stamps the arming instant
      (``evidence_gate_since``) when it is absent, so the family starts being measurable.

    ``violations_after`` is re-measured from the rows, so a pass that fixed nothing says so
    rather than claiming success.
    """
    kb = _kb()
    board = board or _board_slug(conn)
    before = _families(conn, board)
    repaired, unrepaired = [], []
    aliases = assignee_aliases(board)

    for row in before["assignee"]:
        if not row.get("open"):
            continue
        target = aliases.get(str(row["assignee"]).strip().lower())
        if not target or resolve_assignee(target, board=board)[0] == "unresolvable":
            unrepaired.append({**row, "why": "no alias declares an owner for this handle"})
            continue
        try:
            kb.assign_task(conn, row["task_id"], target)
            with kb.write_txn(conn):
                kb._append_event(conn, row["task_id"], "assignee_aliased",
                                 {"from": row["assignee"], "to": target, "cause": cause})
            repaired.append({"task_id": row["task_id"], "from": row["assignee"], "to": target})
        except Exception as exc:  # one row must not abort the sweep
            unrepaired.append({**row, "why": "%s: %s" % (type(exc).__name__, exc)})

    for row in before["dependency"]:
        refs = list(row.get("unedged") or [])
        if str(row.get("block_kind") or "") != "dependency" or not refs:
            unrepaired.append({**row, "why": "not a kind='dependency' block; the edge it waits "
                                             "on is the owner's call, not the pass's"})
            continue
        created = []
        for ref in refs:
            try:
                kb.link_tasks(conn, ref, row["task_id"])
                created.append(ref)
            except Exception as exc:
                unrepaired.append({**row, "why": "link %s: %s" % (ref, exc)})
        if created:
            with kb.write_txn(conn):
                kb._append_event(conn, row["task_id"], "dependency_edge_created",
                                 {"parents": created, "cause": cause})
            repaired.append({"task_id": row["task_id"], "edges": created})

    stamped = None
    if evidence_gate_since(board) is None:
        stamped = int(time.time())
        try:
            path = _kanban_home() / "kanban" / EVIDENCE_GATE_SINCE_KEY
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("%d\n" % stamped, encoding="utf-8")
        except OSError:
            stamped = None

    after = _families(conn, board)
    counts_after = _counts(after)
    return {
        "board": board,
        "action": "reconcile",
        "cause": cause,
        "counts_before": _counts(before),
        "counts_after": counts_after,
        "violations_before": {k: len(v) for k, v in before.items()},
        "violations": after,
        "violations_after": {k: len(v) for k, v in after.items()},
        "open_total_after": sum(c["open"] for c in counts_after.values()
                                if isinstance(c["open"], int)),
        "repaired": repaired,
        "unrepaired": unrepaired,
        "evidence_gate_since": evidence_gate_since(board),
        "stamped_evidence_since": stamped,
        "armed": {"evidence_gate": evidence_gate_mode(board)},
    }


def describe(record: dict) -> str:
    """The human read of a :func:`report` / :func:`reconcile` record."""
    lines = []
    counts = record.get("counts_after") or record.get("counts") or {}
    board = record.get("board") or "default"
    if record.get("action") == "reconcile":
        lines.append("Gate reconcile on board %r: %s."
                     % (board, ", ".join("%s %d before -> %d after"
                                         % (name, (record.get("counts_before") or {}).get(name, {}).get("total", 0),
                                            (counts.get(name) or {}).get("total", 0))
                                         for name in ("rank", "assignee", "dependency", "evidence"))))
        for row in record.get("repaired", []):
            lines.append("  repaired %s: %s" % (row.get("task_id"), row))
        for row in record.get("unrepaired", [])[:25]:
            lines.append("  UNREPAIRED %s (%s): %s"
                         % (row.get("task_id"), row.get("assignee") or row.get("block_kind") or "",
                            row.get("why")))
        if record.get("stamped_evidence_since"):
            lines.append("  armed the evidence family at %d (evidence_gate_since)"
                         % record["stamped_evidence_since"])
        return "\n".join(lines)
    total = record.get("open_total", 0)
    if total == 0:
        lines.append("Gate invariants hold on board %r: no open violation in rank, assignee, "
                     "dependency or evidence." % board)
    else:
        lines.append("Gate invariants: %d open violation(s) on board %r." % (total, board))
    for name in ("rank", "assignee", "dependency", "evidence"):
        rows = (record.get("violations") or {}).get(name) or []
        if not rows:
            continue
        lines.append("%s: %d" % (name, len(rows)))
        for row in rows[:50]:
            if name == "assignee":
                lines.append("  %s (%s) assignee=%r resolves nowhere"
                             % (row["task_id"], row["status"], row["assignee"]))
            elif name == "dependency":
                lines.append("  %s blocked, names %s with no edge%s"
                             % (row["task_id"], ", ".join(row.get("named") or []),
                                " (dependency already shipped: %s)"
                                % ", ".join(row.get("dependencies_shipped") or [])
                                if row.get("dependencies_shipped") else ""))
            elif name == "evidence":
                lines.append("  %s done with no evidence record" % row["task_id"])
            else:
                lines.append("  %s (p=%s) below %s (p=%s)"
                             % (row.get("parent_id"), row.get("parent_priority"),
                                row.get("child_id"), row.get("child_priority")))
    return "\n".join(lines)
