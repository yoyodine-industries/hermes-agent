"""Destructive bulk board actions are REFUSED without a recorded authorized ask.

Operator standing order (2026-09-27, DEFCON card ``t_bf9605f8``): a destructive BULK board action
requires the authorized ask recorded in the guard's ledger (operator directive 2026-10-03: the
``platform-stl`` seat files it, with no separate bot approvals; the ledger, destination claim and
verified snapshot remain mandatory). Doctrine alone already failed once —
on 2026-09-27 a bulk board write destroyed 42 MB of the ops board when a scratch copy resolved onto
the live store, and the worker never escalated because no rule told it to — so the rule ships WITH
this node, in the mutator path, not as prose.

Two fences, one decision point
------------------------------
This module is the DECISION, and ``kanban._dispatch`` is the single place it is consulted for CLI
verbs (the same seam :mod:`hermes_cli.tree_identity` uses for its tree-skew fence). A verb is gated
by being in :data:`BULK_SURFACES`; adding a bulk verb without adding its surface here is a wiring
defect that the parity test fails on (``tests/hermes_cli/test_kanban_bulk_guard.py``).

Refusal is fail-closed and NAMED: every refusal carries a stable ``cause`` token, a plain-words
statement of what is missing, and an audit row in the ledger directory. Nothing about the approval
is inferred — no verbal agreement, no "the operator said so on the call", no defaults.

What counts as a bulk/destructive board action (the classification)
------------------------------------------------------------------
* a whole-board sweep — ``gc`` (events, logs, archived workspaces), ``repair`` (quarantine +
  REINDEX), ``boards rm --delete`` (the board DIRECTORY), ``boards import`` (a whole board from an
  archive), ``swarm`` (N children in one call);
* a triage sweep — ``specify --all`` / ``decompose --all``;
* a multi-card write — ``--ids`` with more than one id on ``block`` / ``schedule`` / ``promote``;
* a cross-store write — anything whose destination is not the target board's own resolved store.

A single-card STATUS/write on the invoking board's own store is not bulk and stays ungated. That
carve-out is about cardinality, never about destruction: a HARD ROW DELETE (``purge``) is this class
whatever its count — one id or fifty — and NO surface is exempt from it. The dashboard's
``DELETE /tasks/{id}`` reaches the same store primitive as ``archive --rm`` and therefore reaches
this same decision (card t_1de635cd); it threads an ``approval`` digest like every other surface.
Cardinality is not the test for purge on purpose: the dashboard's multi-select delete fans out into
N single-id calls, so a "gate only MULTI-id purges" reading would be defeated by its own client.

Two layers of protection, deliberately independent
-------------------------------------------------
1. THIS guard — the approval bundle + the verified-snapshot precondition at the moment of the
   action. It binds the EXACT action (board + verb + canonical scope), so an approval for
   ``gc --event-retention-days 30`` can never admit ``--event-retention-days 0``.
2. The recurring verified snapshot DAG (``board-store-backup`` unit) whose destination assertion
   refuses to write anywhere but its own backup root. It is the recovery substrate for the class
   this guard cannot see (a destroyer that never passes through the mutator path at all).

The snapshot precondition is not optional: a destructive action with no VERIFIED snapshot of the
store it is about to write is refused (``snapshot_missing``), exactly as the operator's
2026-09-27 precondition requires. The guard takes and verifies the preimage itself when none is
fresh, so the precondition cannot be skipped by a slow nightly job.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

#: Stable, greppable prefix of every refusal. ``_err`` prints it verbatim, so a refusal is one
#: stderr line that starts with exactly this token.
REFUSAL_PREFIX = "bulk-guard refusal:"

#: The authorized seat's profile id — the actor whose ask authorizes a bulk action.
#: Operator directive 2026-10-03: the majordomo-era "ops head" role is retired; the platform STL
#: seat is the authorized agent for platform board operations.
AUTHORIZED_ASK_PROFILE = "platform-stl"

#: Distinct bot approvals required, in addition to the authorized ask. Operator directive
#: 2026-10-03: a single authorized agent authorizes. The ledger, destination claim and snapshot
#: precondition remain mandatory, so the failure mode the guard exists for is still covered.
REQUIRED_APPROVALS = 0

#: A snapshot older than this is not a precondition the operator would accept (the nightly unit
#: runs daily 00:35 local; 26 h tolerates one missed night plus a skewed clock). Older => the
#: guard TAKES a fresh preimage itself rather than refusing outright.
FRESH_SNAPSHOT_SECONDS = 26 * 3600

#: Ledger directory inside the kanban home: ``<kanban_home>/kanban/bulk_actions/``. It lives
#: OUTSIDE every board store on purpose — the record must survive destruction of the store it
#: gated (``boards rm --delete`` takes the board directory).
LEDGER_DIRNAME = "bulk_actions"
LEDGER_FILENAME = "approvals.jsonl"
AUDIT_FILENAME = "audit.jsonl"

#: Host-level escape hatches, test/host-injection only. The preimage runner is overridable so the
#: suite never shells out; ``BULK_GUARD_LEDGER_DIR`` relocates the ledger for a scratch run.
PREIMAGE_CMD_ENV = "BULK_GUARD_PREIMAGE_CMD"
LEDGER_DIR_ENV = "BULK_GUARD_LEDGER_DIR"

#: Default deployed entrypoint of the board-store snapshot unit (deploy train car ``platform``,
#: unit ``ops-scripts``). ``--preimage <store>`` copies, verifies and prints one JSON line.
DEFAULT_PREIMAGE_CMD = "~/.hermes/scripts/board-store-backup.sh"

#: The estate's approvals/APR store, resolved at ask-filing time (never at gate time, so the gate
#: has no dependency on a service being up).
APPROVALS_DB = "/opt/hermes_prod/yoyodine-web-services/approvals.db"


class BulkActionRefused(ValueError):
    """Raised by :func:`assert_bulk_approved` when the decision is REFUSE.

    ``str(exc)`` always starts with :data:`REFUSAL_PREFIX`; ``.cause`` is the stable token;
    ``.remedy`` states in plain words exactly what is required to proceed.
    """

    def __init__(self, cause: str, message: str, *, remedy: str, facts: Optional[dict] = None):
        self.cause = cause
        self.remedy = remedy
        self.facts: Dict[str, Any] = dict(facts or {})
        super().__init__(f"{REFUSAL_PREFIX} {cause}: {message} Required: {remedy}")


# --- the surfaces -----------------------------------------------------------------------

def _csv_ids(value: Optional[Sequence[str]]) -> str:
    return ",".join(sorted(str(v) for v in (value or ())))


#: verb -> (is_bulk(params) -> bool, scope(params) -> canonical string). The scope IS the binding:
#: the digest an approval names is computed from it, so a parameter change is a different action.
BULK_SURFACES: Dict[str, Dict[str, Callable]] = {
    "gc": {
        "bulk": lambda p: True,
        "scope": lambda p: (
            f"event-retention-days={p.get('event_retention_days', 30)} "
            f"log-retention-days={p.get('log_retention_days', 30)} workspaces=archived"
        ),
    },
    "repair": {
        "bulk": lambda p: True,
        "scope": lambda p: "quarantine-index-repair store=target",
    },
    "swarm": {
        "bulk": lambda p: True,
        "scope": lambda p: f"parent={p.get('task_id', '')} children={p.get('count', 'n')}",
    },
    "specify": {
        "bulk": lambda p: bool(p.get("all_triage")),
        "scope": lambda p: f"all={bool(p.get('all_triage'))} tenant={p.get('tenant') or '*'}"
                            f" ids={_csv_ids(p.get('ids'))}",
    },
    "decompose": {
        "bulk": lambda p: bool(p.get("all_triage")),
        "scope": lambda p: f"all={bool(p.get('all_triage'))} tenant={p.get('tenant') or '*'}"
                            f" ids={_csv_ids(p.get('ids'))}",
    },
    "archive": {
        "bulk": lambda p: bool(p.get("purge_ids")) or len(p.get("task_ids") or ()) > 1,
        "scope": lambda p: (f"archive={_csv_ids(p.get('task_ids'))} "
                            f"purge={_csv_ids(p.get('purge_ids'))}"),
    },
    # A bulk reassign that RECLAIMS first terminates every named card's live worker, so it is
    # destructive at the same grade as block/schedule/promote. Both conditions are required: a
    # reclaim of ONE card is the ordinary single-card recovery, and a plain multi-id assign
    # touches no reclaim primitive. (CLI `reassign` is single-id by construction, so only the
    # dashboard's bulk door ever reaches this surface.) Card t_f1f22f8c.
    "reassign": {
        "bulk": lambda p: bool(p.get("reclaim_first")) and len(p.get("ids") or ()) > 1,
        "scope": lambda p: f"ids={_csv_ids(p.get('ids'))} reclaim=true",
    },
}

#: The ``--ids`` bulk-mode verbs: bulk exactly when more than one id is named.
for _verb in ("block", "schedule", "promote"):
    BULK_SURFACES[_verb] = {
        "bulk": lambda p: len(p.get("ids") or ()) > 1,
        "scope": lambda p: f"ids={_csv_ids(p.get('ids'))}",
    }

#: ``hermes kanban boards …`` sub-actions that move or destroy a whole board.
BULK_BOARD_ACTIONS: Dict[str, Dict[str, Callable]] = {
    "rm": {"bulk": lambda p: True,
           "scope": lambda p: f"slug={p.get('slug')} "
                              f"mode={'DELETE' if p.get('delete') else 'archive'}"
                              f"{' estate' if p.get('estate') else ''}"},
    "remove": {"bulk": lambda p: True,
               "scope": lambda p: f"slug={p.get('slug')} "
                                  f"mode={'DELETE' if p.get('delete') else 'archive'}"
                                  f"{' estate' if p.get('estate') else ''}"},
    "delete": {"bulk": lambda p: True, "scope": lambda p: f"slug={p.get('slug')} mode=DELETE"},
    "import": {"bulk": lambda p: True, "scope": lambda p: f"archive={p.get('archive')}"},
}

#: Every verb this guard can gate (CLI actions). Kept as one tuple so a test can assert the
#: estate's mutating-verb fences are never WIDER than this classification without failing.
GATED_VERBS: Tuple[str, ...] = tuple(BULK_SURFACES) + tuple(
    f"boards-{a}" for a in BULK_BOARD_ACTIONS
)


def classify(verb: str, params: dict) -> Optional[str]:
    """The canonical scope of ``verb`` for ``params``, or ``None`` when it is not bulk."""
    surface = BULK_SURFACES.get(verb)
    if surface is None:
        return None
    if not surface["bulk"](params):
        return None
    return surface["scope"](params)


def classify_board_action(sub_action: Optional[str], params: dict) -> Optional[str]:
    """The canonical scope of ``hermes kanban boards <sub_action>``, or ``None``."""
    surface = BULK_BOARD_ACTIONS.get(sub_action or "")
    if surface is None:
        return None
    return surface["scope"](params)


# --- binding: the digest names the EXACT action -------------------------------------------

def scope_digest(board: str, verb: str, scope: str) -> str:
    """Stable digest of one exact action: board + verb + canonical scope.

    The digest is what an ask and its approvals name, so an approval CANNOT be transplanted:
    ``gc`` at 30 days and ``gc`` at 0 days are different digests, and so are two boards.
    """
    payload = f"board={board}\nverb={verb}\nscope={scope}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --- the ledger (outside every board store, append-only) -----------------------------------

def ledger_dir() -> Path:
    override = os.environ.get(LEDGER_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    from hermes_cli import kanban_db as kb

    return kb.kanban_home() / "kanban" / LEDGER_DIRNAME


def ledger_path() -> Path:
    return ledger_dir() / LEDGER_FILENAME


def audit_path() -> Path:
    return ledger_dir() / AUDIT_FILENAME


def read_ledger(path: Optional[Path] = None) -> List[dict]:
    """Every ledger row, oldest first. A malformed line is skipped, never fatal."""
    p = path or ledger_path()
    rows: List[dict] = []
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def record_ask(
    *,
    board: str,
    verb: str,
    scope: str,
    approach: str,
    actor: str = AUTHORIZED_ASK_PROFILE,
    apr_ref: str = "",
    apr_status: str = "",
    apr_decided_at: str = "",
    now: Optional[float] = None,
) -> str:
    """Record the authorized ask. Returns the digest any approvals must name."""
    digest = scope_digest(board, verb, scope)
    _append(ledger_path(), {
        "ts": time.time() if now is None else now,
        "role": "ask",
        "actor": actor,
        "board": board,
        "verb": verb,
        "scope": scope,
        "digest": digest,
        "approach": approach,
        "apr_ref": apr_ref,
        "apr_status": apr_status,
        "apr_decided_at": apr_decided_at,
    })
    return digest


def record_approval(
    *,
    actor: str,
    digest: str,
    approach: str,
    reason: str = "",
    now: Optional[float] = None,
) -> None:
    """Record one bot approval against ``digest``. The caller validates the actor first."""
    _append(ledger_path(), {
        "ts": time.time() if now is None else now,
        "role": "approval",
        "actor": actor,
        "digest": digest,
        "approach": approach,
        "reason": reason,
    })


def bundle_for(digest: str, path: Optional[Path] = None) -> List[dict]:
    return [row for row in read_ledger(path) if row.get("digest") == digest]


def _apr_numeric(ref: str) -> Optional[int]:
    """``APR-0132`` / ``apr 132`` / ``132`` -> ``132``; anything else -> None."""
    digits = ""
    for ch in reversed(ref):
        if ch.isdigit():
            digits = ch + digits
        elif ch in "-_# " and digits:
            break
        else:
            return None
    return int(digits) if digits else None


def resolve_apr(apr_ref: str, *, db_path: Optional[str] = None) -> Dict[str, Any]:
    """Resolve an APR row for the ASK, at FILING time (the gate itself never calls out).

    Returns ``{"ok": bool, "status": str, "decided_at": str, "title": str}``. A caller that cannot
    resolve the row must refuse to file the ask — the ask is the operator's decision, not a note.
    """
    ref = (apr_ref or "").strip()
    if not ref:
        return {"ok": False, "status": "", "decided_at": "", "title": ""}
    path = db_path or os.environ.get("BULK_GUARD_APPROVALS_DB", APPROVALS_DB)
    if not Path(path).exists():
        return {"ok": False, "status": "", "decided_at": "", "title": "",
                "error": f"approvals store not found: {path}"}
    try:
        conn = sqlite3.connect(path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(approvals)")}
            if "id" not in cols:
                return {"ok": False, "status": "", "decided_at": "", "title": "",
                        "error": "the approvals store has no approvals table"}
            row = None
            ref_id = _apr_numeric(ref)
            if ref_id is not None:
                row = conn.execute("SELECT * FROM approvals WHERE id = ?", (ref_id,)).fetchone()
            if row is None and "idempotency_key" in cols:
                row = conn.execute("SELECT * FROM approvals WHERE idempotency_key = ?",
                                   (ref,)).fetchone()
            if row is None and "escalation_id" in cols:
                row = conn.execute("SELECT * FROM approvals WHERE escalation_id = ?",
                                   (ref,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return {"ok": False, "status": "", "decided_at": "", "title": "", "error": str(exc)}
    if row is None:
        return {"ok": False, "status": "", "decided_at": "", "title": "",
                "error": f"no approvals row {ref!r}"}
    data = dict(row)
    status = str(data.get("decision") or data.get("status") or "")
    return {
        "ok": True,
        "status": status,
        "decided_at": str(data.get("decided_at") or ""),
        "title": str(data.get("title") or data.get("subject") or ""),
    }


# --- the snapshot precondition -------------------------------------------------------------

def _kanban_root_home() -> Path:
    from hermes_cli import kanban_db as kb

    return kb.kanban_home()


def snapshot_root() -> Path:
    override = os.environ.get("BOARD_STORE_BACKUP_ROOT", "").strip()
    if override:
        return Path(override).expanduser()
    return _kanban_root_home() / "backups" / "board-stores"


def _run_deployed_preimage(store_path: Path) -> dict:
    """Take + verify a preimage through the DEPLOYED snapshot unit, returning its receipt."""
    cmd = os.environ.get(PREIMAGE_CMD_ENV, DEFAULT_PREIMAGE_CMD)
    argv = [str(Path(cmd).expanduser()), "--preimage", str(store_path)]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        raise BulkActionRefused(
            "snapshot_missing",
            f"could not run the snapshot unit ({argv[0]}): {exc}",
            remedy=("deploy the board-store snapshot unit and take a verified preimage: "
                    f"`{argv[0]} --preimage {store_path}`"),
        )
    out = (proc.stdout or "").strip().splitlines()
    for line in reversed(out):
        line = line.strip()
        if line.startswith("{"):
            try:
                receipt = json.loads(line)
            except ValueError:
                continue
            if receipt.get("quick_check") == "ok" and receipt.get("path"):
                receipt["store"] = str(store_path)
                return receipt
    raise BulkActionRefused(
        "snapshot_unverified",
        f"the snapshot unit did not return a verified receipt (rc={proc.returncode}): "
        f"{(proc.stderr or proc.stdout or '').strip()[:300]}",
        remedy=f"verify the store by hand and rerun `{argv[0]} --preimage {store_path}`",
    )


def verified_snapshots(root: Optional[Path] = None) -> List[dict]:
    """Every verified preimage receipt on this host, newest first."""
    base = (root or snapshot_root()) / "preimages"
    out: List[dict] = []
    try:
        entries = sorted(base.glob("*.json"))
    except OSError:
        return out
    for path in entries:
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(row, dict) and row.get("quick_check") == "ok":
            out.append(row)
    out.sort(key=lambda r: float(r.get("ts") or r.get("created_at") or 0), reverse=True)
    return out


def snapshot_precondition(
    store_path: Path,
    *,
    runner: Optional[Callable[[Path], dict]] = None,
    root: Optional[Path] = None,
    now: Optional[float] = None,
) -> dict:
    """Guarantee a VERIFIED snapshot of ``store_path`` exists; return its receipt.

    Fresh verified preimage on disk => use it. Otherwise take one NOW through the deployed unit.
    Raises :class:`BulkActionRefused` (``snapshot_*``) when neither is possible — the action is
    then refused, never run unbacked.
    """
    target = Path(store_path).expanduser().resolve()
    stamp = time.time() if now is None else now
    for row in verified_snapshots(root):
        if Path(str(row.get("store", ""))).expanduser().resolve() != target:
            continue
        ts = float(row.get("ts") or row.get("created_at") or 0)
        if stamp - ts <= FRESH_SNAPSHOT_SECONDS:
            row.setdefault("store", str(target))
            row["fresh"] = True
            return row
    take = runner or _run_deployed_preimage
    receipt = take(target)
    receipt.setdefault("store", str(target))
    receipt["fresh"] = False
    return receipt


# --- destination claims ---------------------------------------------------------------------

def assert_write_destination(store_path: Path, *, board: str, run_root: Optional[Path] = None) -> Path:
    """Fail closed unless the write destination is the named board's own store, or a private run.

    Two prohibited destinations, both named: a live board store that is NOT the named target (the
    2026-09-27 class — a "scratch" write that resolved onto a live store), and any path outside the
    kanban home that this run cannot prove it owns.
    """
    from hermes_cli import kanban_scratch as ks

    resolved = Path(store_path).expanduser().resolve()
    live = ks.live_kanban_root()
    live_stores = {p.resolve() for p in ks.live_board_stores(live)}
    try:
        from hermes_cli import kanban_db as kb

        target = kb.kanban_db_path(board).resolve()
    except Exception:  # pragma: no cover - resolution failure is itself a refusal
        target = None
    if resolved in live_stores and resolved != target:
        raise BulkActionRefused(
            "destination_live_store",
            f"destination {resolved} is a LIVE board store and not the target board {board!r}",
            remedy=(f"name the board you mean (`--board {board}`) — a scratch run must write a "
                    f"private store under its own run root, never a live store"),
            facts={"destination": str(resolved), "board": board},
        )
    if target is not None and resolved != target and run_root is not None:
        try:
            resolved.relative_to(Path(run_root).expanduser().resolve())
        except ValueError:
            raise BulkActionRefused(
                "destination_unclaimed",
                f"destination {resolved} is neither {board!r}'s own store nor under the run root "
                f"{run_root}",
                remedy="write the board's own store, or a private store under this run's root",
                facts={"destination": str(resolved)},
            )
    return resolved


# --- the decision ---------------------------------------------------------------------------

def evaluate(
    *,
    board: str,
    verb: str,
    scope: str,
    approval: str,
    store_path: Optional[Path] = None,
    run_root: Optional[Path] = None,
    ledger: Optional[Path] = None,
    known_profiles: Optional[Iterable[str]] = None,
    snapshot: Optional[dict] = None,
    snapshot_runner: Optional[Callable[[Path], dict]] = None,
    now: Optional[float] = None,
    estate: bool = False,
    actor: str = "",
    reason: str = "",
) -> dict:
    """Admit or refuse, in a fixed clause order. Raises :class:`BulkActionRefused` on refusal.

    Order: destination claim -> snapshot precondition -> approval bundle (ask, APR, quorum). The
    order is the operator's remedy order: nothing else matters if the destination is wrong.

    ``estate=True`` is the ONE waiver (ruling t_fcf7a321, Decision 2): a board whose own
    ``board.json`` declares ``"dispatch": false`` is torn down SINGLE-ACTOR — the ask/APR clause
    is skipped, while the destination claim, the verified snapshot and the ledger/audit record
    are all kept. The caller has already refused on the marker/current-board/live-claim gates.
    """
    facts: Dict[str, Any] = {"board": board, "verb": verb, "scope": scope}
    if store_path is not None:
        assert_write_destination(store_path, board=board, run_root=run_root)
        facts["destination"] = str(Path(store_path).expanduser().resolve())

    digest = scope_digest(board, verb, scope)
    facts["digest"] = digest
    if estate:
        facts["estate"] = True
        facts["actor"] = actor or AUTHORIZED_ASK_PROFILE
        facts["reason"] = reason
        facts["approvers"] = []
        _conclude_snapshot_clause(
            facts, board=board, store_path=store_path, snapshot=snapshot,
            snapshot_runner=snapshot_runner, now=now,
        )
        _append(ledger if ledger is not None else ledger_path(), {
            "ts": time.time() if now is None else now,
            "role": "estate-teardown",
            "actor": facts["actor"],
            "board": board,
            "verb": verb,
            "scope": scope,
            "digest": digest,
            "reason": reason,
            "snapshot": (facts.get("snapshot") or {}).get("path", ""),
            "snapshot_sha256": (facts.get("snapshot") or {}).get("sha256", ""),
        })
        return facts

    rows = bundle_for(digest, ledger)
    if not rows:
        raise BulkActionRefused(
            "bulk_unapproved",
            f"{verb} ({scope}) on board {board!r} carries no approval record",
            remedy=_ask_remedy(board, verb, scope, digest),
            facts={**facts, "rows": 0},
        )
    asks = [r for r in rows if r.get("role") == "ask"]
    if not asks:
        raise BulkActionRefused(
            "approval_missing_ask",
            "approvals exist but no authorized ask is recorded for this exact action",
            remedy=_ask_remedy(board, verb, scope, digest),
            facts=facts,
        )
    ask = asks[-1]
    if str(ask.get("actor") or "") != AUTHORIZED_ASK_PROFILE:
        raise BulkActionRefused(
            "approval_not_ops_head",
            f"the ask was recorded by {ask.get('actor')!r}, not the authorized ask profile "
            f"({AUTHORIZED_ASK_PROFILE!r})",
            remedy=f"the ask must be filed as the authorized ask profile {AUTHORIZED_ASK_PROFILE!r}",
            facts={**facts, "ask_actor": ask.get("actor")},
        )
    if not str(ask.get("apr_ref") or "").strip() or str(ask.get("apr_status") or "") != "approved":
        raise BulkActionRefused(
            "approval_apr_unresolved",
            "the ask names no resolved, approved APR row",
            remedy=("file the ask with an approved APR: `hermes kanban bulk-approvals ask "
                    f"--board {board} --verb {verb} --scope \"{scope}\" --apr APR-XXXX "
                    "--approach \"…\"`"),
            facts={**facts, "apr_ref": ask.get("apr_ref"), "apr_status": ask.get("apr_status")},
        )

    approaches = {str(r.get("approach") or "") for r in rows}
    if len(approaches) != 1:
        raise BulkActionRefused(
            "approval_approach_mismatch",
            "the ask and the approvals do not name the same approach",
            remedy="re-record the approvals against the ask's exact approach text",
            facts={**facts, "approaches": sorted(approaches)},
        )
    facts["approach"] = approaches.pop()

    approvals = [r for r in rows if r.get("role") == "approval"]
    seen: List[str] = []
    for row in approvals:
        actor = str(row.get("actor") or "")
        if actor == AUTHORIZED_ASK_PROFILE:
            raise BulkActionRefused(
                "approval_duplicate_actor",
                f"{AUTHORIZED_ASK_PROFILE!r} filed both the ask and an approval",
                remedy="an approval must come from a profile other than the ask's",
                facts=facts,
            )
        if known_profiles is not None and actor not in set(known_profiles):
            raise BulkActionRefused(
                "approval_unknown_actor",
                f"{actor!r} is not a profile on this host",
                remedy="an approval must be filed by a real bot profile",
                facts={**facts, "actor": actor},
            )
        if actor in seen:
            raise BulkActionRefused(
                "approval_duplicate_actor",
                f"{actor!r} approved twice; each approval must come from a distinct profile",
                remedy="a different bot must record its own approval",
                facts={**facts, "repeated": actor},
            )
        seen.append(actor)
    if len(seen) < REQUIRED_APPROVALS:
        raise BulkActionRefused(
            "approval_quorum",
            f"{len(seen)} distinct approval(s) recorded, {REQUIRED_APPROVALS} required",
            remedy=(f"{REQUIRED_APPROVALS} distinct approval(s) are required: `hermes kanban "
                    f"bulk-approvals approve {digest} --approach \"{facts['approach']}\" "
                    f"--reason \"…\"` (at REQUIRED_APPROVALS=0 nothing further is needed)"),
            facts={**facts, "approvers": seen},
        )
    facts["approvers"] = seen

    # The snapshot clause runs LAST on purpose: it is the expensive one (it may take a preimage),
    # so an action that has no approval yet is refused before anything touches the store.
    _conclude_snapshot_clause(
        facts, board=board, store_path=store_path, snapshot=snapshot,
        snapshot_runner=snapshot_runner, now=now,
    )
    return facts


def _conclude_snapshot_clause(
    facts: dict,
    *,
    board: str,
    store_path: Optional[Path],
    snapshot: Optional[dict],
    snapshot_runner: Optional[Callable[[Path], dict]],
    now: Optional[float],
) -> None:
    """The shared snapshot clause: a VERIFIED preimage of the target store must exist.

    Shared by the approval path and the estate teardown path so the estate waiver can drop the
    ask/APR clauses WITHOUT dropping this control (ruling t_fcf7a321, Decision 2).
    """
    if snapshot is None:
        store = Path(store_path) if store_path is not None else _store_for(board, required=True)
        snapshot = snapshot_precondition(store, runner=snapshot_runner, now=now)
    claimed = str(snapshot.get("store") or "").strip()
    if claimed and store_path is not None:
        try:
            same = Path(claimed).expanduser().resolve() == Path(store_path).expanduser().resolve()
        except OSError:  # pragma: no cover - an unresolvable claim is not a match
            same = False
        if not same:
            raise BulkActionRefused(
                "snapshot_store_mismatch",
                f"the snapshot names store {claimed}, not {store_path}",
                remedy="take a verified snapshot of the store the action will write",
                facts={**facts, "snapshot_store": claimed},
            )
    facts["snapshot"] = {k: snapshot.get(k) for k in ("path", "sha256", "quick_check", "store")}
    return facts


def _store_for(board: str, *, required: bool = False) -> Optional[Path]:
    """The board's store, or None when the board is not registered.

    Best-effort by default: an unregistered board is the verb's own refusal to raise, not this
    guard's (``kanban_db.kanban_db_path`` raises :class:`BoardResolutionError`, which subclasses
    ``ValueError``, so the CLI already reports it as a usage error). ``required=True`` on the
    snapshot path, where a store is genuinely needed to take a preimage of.
    """
    from hermes_cli import kanban_db as kb

    try:
        return Path(kb.kanban_db_path(board))
    except Exception:
        if required:
            raise BulkActionRefused(
                "snapshot_missing",
                f"board {board!r} resolves to no store, so no snapshot of it can exist",
                remedy=(f"register the board first (`hermes kanban boards create {board}`) or "
                        f"name the board the action really targets"),
            )
        return None


def _ask_remedy(board: str, verb: str, scope: str, digest: str) -> str:
    """The ACTUAL bulk requirement (ruling t_fcf7a321, Decision 3): a single ask filed by the
    authorized profile, NO separate approvals (``REQUIRED_APPROVALS`` is 0), plus the ledger,
    destination claim and verified snapshot — and the ONE waiver, a declared estate board."""
    return (
        f"the authorized ask profile ({AUTHORIZED_ASK_PROFILE}) files the ask "
        f"(`hermes kanban bulk-approvals ask --board {board} "
        f"--verb {verb} --scope \"{scope}\" --apr APR-XXXX --approach \"…\"`); no separate bot "
        f"approvals are required (REQUIRED_APPROVALS={REQUIRED_APPROVALS}), and the ledger, the "
        f"destination claim and the verified snapshot still apply (digest {digest}). A board whose "
        f"own board.json declares \"dispatch\": false is the ONE waiver: tear it down single-actor "
        f"with `hermes kanban boards rm --estate {board}` (no ask, no APR — the verified preimage "
        f"and the ledger/audit row are still taken)."
    )


def assert_bulk_approved(
    *,
    board: str,
    verb: str,
    params: dict,
    approval: str = "",
    store_path: Optional[Path] = None,
    run_root: Optional[Path] = None,
    known_profiles: Optional[Iterable[str]] = None,
    snapshot: Optional[dict] = None,
    snapshot_runner: Optional[Callable[[Path], dict]] = None,
    now: Optional[float] = None,
    audit: bool = True,
) -> Optional[dict]:
    """Gate one CLI action. Returns admission facts, or ``None`` for a non-bulk action.

    Raises :class:`BulkActionRefused` on refusal and records an audit row either way.
    """
    scope = classify(verb, params)
    if scope is None:
        return None
    try:
        facts = evaluate(
            board=board, verb=verb, scope=scope, approval=approval, store_path=store_path,
            run_root=run_root, known_profiles=known_profiles, snapshot=snapshot,
            snapshot_runner=snapshot_runner, now=now,
        )
    except BulkActionRefused as exc:
        if audit:
            _append_audit(board, verb, scope, "refused", exc.cause, facts=exc.facts)
        raise
    if audit:
        _append_audit(board, verb, scope, "admitted", "ok", facts=facts)
    return facts


def _estate_state(board: str) -> "tuple[bool, str]":
    """``(declares_estate, why)`` for *board*'s OWN ``board.json``.

    The DECLARATION is read through ``kanban_db.read_board_metadata`` — one reader, never
    re-implemented. ``why`` only sharpens the refusal message for the absent/malformed stub a
    minted board (or a broken file) produces, since ``read_board_metadata`` deliberately
    synthesizes ``dispatch: True`` for both (ruling t_fcf7a321, Decision 2, refusal 7).
    """
    from hermes_cli import kanban_db as kb

    try:
        declared = kb.read_board_metadata(board).get("dispatch") is False
    except Exception:
        return False, "unreadable"
    if declared:
        return True, "declared"
    path = kb.board_metadata_path(board)
    if not path.exists():
        return False, "absent"
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False, "malformed"
    return False, "live" if isinstance(raw, dict) else "malformed"


def _is_current_board(board: str) -> bool:
    """True when *board* is the active board. Unprovable => True (fail closed)."""
    from hermes_cli import kanban_db as kb

    try:
        return kb.get_current_board() == kb._slug_or_default(board)
    except Exception:
        return True


def _board_has_live_claims(board: str) -> bool:
    """True when *board* carries a live worker claim. Unprovable => True (fail closed)."""
    from hermes_cli import kanban_db as kb

    try:
        return bool(kb.board_has_live_claims(board))
    except Exception:
        return True


def assert_board_action_approved(
    *,
    board: str,
    sub_action: str,
    params: dict,
    approval: str = "",
    known_profiles: Optional[Iterable[str]] = None,
    snapshot: Optional[dict] = None,
    snapshot_runner: Optional[Callable[[Path], dict]] = None,
    now: Optional[float] = None,
    audit: bool = True,
) -> Optional[dict]:
    """Gate a ``hermes kanban boards <sub_action>`` action (rm/delete/import).

    Archiving a board whose OWN ``board.json`` declares ``"dispatch": false`` runs as the
    single-actor estate teardown (ruling t_fcf7a321, Decision 2): the ask/APR clause is waived,
    while the destination claim + verified snapshot + ledger/audit row are kept and four
    fail-closed refusals guard the waiver (marker absent, current board, live claim, no reason).
    A hard delete (``--delete``) always keeps the FULL ceremony.
    """
    scope = classify_board_action(sub_action, params)
    if scope is None:
        return None
    verb = f"boards-{sub_action}"
    hard = bool(params.get("delete"))
    estate_asserted = bool(params.get("estate"))
    try:
        if estate_asserted and hard:
            raise BulkActionRefused(
                "estate_hard_delete",
                "`--estate` cannot be combined with `--delete`: a hard delete keeps the FULL "
                "approval ceremony",
                remedy="drop --delete (archive) or drop --estate and run the ask+snapshot ceremony",
                facts={"board": board},
            )
        estate = False
        if sub_action in ("rm", "remove") and not hard:
            estate, why = _estate_state(board)
            if estate_asserted and not estate:
                raise BulkActionRefused(
                    "estate_marker_absent",
                    f"board {board!r} declares no estate: its board.json is {why} (an estate is a "
                    "board.json carrying \"dispatch\": false)",
                    remedy=(f"declare it first (`hermes kanban boards set-dispatch {board} off`) "
                            "or run the ordinary ask+snapshot ceremony"),
                    facts={"board": board, "marker": why},
                )
            if estate:
                reason = str(params.get("reason") or "").strip()
                if not reason:
                    raise BulkActionRefused(
                        "estate_reason_missing",
                        f"estate teardown of {board!r} names no --reason",
                        remedy=("record WHY the estate is torn down: `hermes kanban boards rm "
                                f"--estate {board} --reason \"…\"` (it lands in the ledger and "
                                "the audit row)"),
                        facts={"board": board},
                    )
                if _is_current_board(board):
                    raise BulkActionRefused(
                        "estate_is_current_board",
                        f"{board!r} is the CURRENT board; archiving it races its own resurrection",
                        remedy="switch away first (`hermes kanban boards switch default`) and re-run",
                        facts={"board": board},
                    )
                if _board_has_live_claims(board):
                    raise BulkActionRefused(
                        "estate_live_claim",
                        f"{board!r} carries a LIVE claim (a running worker pins its store)",
                        remedy=("let the running worker finish (or reconcile the claim) before "
                                "tearing the estate down"),
                        facts={"board": board},
                    )
        facts = evaluate(
            board=board, verb=verb, scope=scope, approval=approval,
            store_path=Path(params["store"]) if params.get("store") else None,
            known_profiles=known_profiles, snapshot=snapshot,
            snapshot_runner=snapshot_runner, now=now,
            estate=estate, actor=str(params.get("actor") or "") or _current_actor(),
            reason=str(params.get("reason") or ""),
        )
    except BulkActionRefused as exc:
        if audit:
            _append_audit(board, verb, scope, "refused", exc.cause, facts=exc.facts)
        raise
    if audit:
        _append_audit(board, verb, scope, "admitted", "ok", facts=facts)
    return facts


def _append_audit(board: str, verb: str, scope: str, outcome: str, cause: str, *, facts: dict) -> None:
    _append(audit_path(), {
        "ts": time.time(),
        "outcome": outcome,
        "cause": cause,
        "board": board,
        "verb": verb,
        "scope": scope,
        "digest": scope_digest(board, verb, scope),
        "approvers": facts.get("approvers") or [],
        "snapshot": (facts.get("snapshot") or {}).get("path", ""),
        "estate": bool(facts.get("estate")),
        "actor": facts.get("actor") or "",
        "reason": facts.get("reason") or "",
    })


# --- the CLI seam ----------------------------------------------------------------------------

def params_from_args(action: str, args: Any) -> dict:
    """Extract the parameters the canonical scope is rendered from, for one CLI action."""
    get = lambda name, default=None: getattr(args, name, default)  # noqa: E731
    if action == "gc":
        return {
            "event_retention_days": get("event_retention_days", 30),
            "log_retention_days": get("log_retention_days", 30),
        }
    if action in ("specify", "decompose"):
        return {"all_triage": bool(get("all_triage")), "tenant": get("tenant"),
                "ids": [get("task_id")] if get("task_id") else []}
    if action in ("block", "schedule", "promote"):
        return {"ids": list(get("ids") or ())}
    if action == "reassign":
        # The CLI verb names ONE card; a reclaim-reassign over more than one id can only be
        # assembled by the dashboard, so this is non-bulk by cardinality (card t_f1f22f8c).
        return {"ids": [get("task_id")] if get("task_id") else [],
                "reclaim_first": bool(get("reclaim"))}
    if action == "archive":
        return {"task_ids": list(get("task_ids") or ()), "purge_ids": list(get("purge_ids") or ())}
    if action == "swarm":
        return {"task_id": get("task_id"), "count": "n"}
    return {}


def board_action_params_from_args(sub_action: str, args: Any) -> dict:
    get = lambda name, default=None: getattr(args, name, default)  # noqa: E731
    if sub_action in ("rm", "delete", "remove"):
        # `boards delete <slug>` (alias) never sets args.delete; it means the hard delete.
        hard = bool(get("delete")) or sub_action == "delete"
        return {"slug": get("slug"), "delete": hard,
                "estate": bool(get("estate")), "reason": str(get("reason") or "")}
    if sub_action == "import":
        return {"archive": get("archive")}
    return {}


def gate_cli(args: Any) -> Optional[dict]:
    """The single CLI seam: gate the dispatch of ``args`` when it is a bulk action.

    Called from ``kanban._dispatch`` for every kanban action (including ``boards …``), so a bulk
    verb is gated by construction rather than by remembering to call the guard in its handler.
    """
    action = getattr(args, "kanban_action", None)
    if not action or action == "bulk-approvals":
        return None
    approval = str(getattr(args, "approval", "") or "")
    from hermes_cli import kanban_db as kb

    if action == "boards":
        sub = getattr(args, "boards_action", None)
        board = str(getattr(args, "slug", "") or "")
        return assert_board_action_approved(
            board=board, sub_action=sub, params=board_action_params_from_args(sub, args),
            approval=approval,
        )
    try:
        board = str(getattr(args, "board", "") or "") or kb.get_current_board()
    except Exception:
        board = ""
    try:
        store = Path(kb.kanban_db_path(board))
    except ValueError:
        store = None  # unregistered board: the verb's own resolution is the refusal that matters
    return assert_bulk_approved(
        board=board, verb=action, params=params_from_args(action, args), approval=approval,
        store_path=store,
    )


def profiles_on_host() -> List[str]:
    """Profile ids that exist on this host (the estate's assignee trap: names are not ids)."""
    from hermes_constants import get_default_hermes_root

    profiles = Path(get_default_hermes_root()) / "profiles"
    try:
        return sorted(p.name for p in profiles.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return []


# --- CLI handlers (``hermes kanban bulk-approvals …``) ---------------------------------------

def _cmd_bulk_approvals_ask(args: Any) -> int:
    actor = str(getattr(args, "actor", "") or "") or _current_actor()
    if actor != AUTHORIZED_ASK_PROFILE:
        print(f"{REFUSAL_PREFIX} approval_not_ops_head: the ask must be filed as "
              f"the authorized ask profile {AUTHORIZED_ASK_PROFILE!r} (got {actor!r})")
        return 2
    apr = resolve_apr(str(getattr(args, "apr", "") or ""))
    if not apr["ok"] or apr["status"] != "approved":
        status = apr.get("status") or "unresolved"
        detail = apr.get("error") or f"APR status is {status!r}"
        print(f"{REFUSAL_PREFIX} approval_apr_unresolved: {detail}")
        return 2
    digest = record_ask(
        board=str(getattr(args, "board", "") or _current_board()), verb=args.verb,
        scope=args.scope, approach=args.approach, actor=actor,
        apr_ref=args.apr, apr_status=apr["status"], apr_decided_at=apr["decided_at"],
    )
    print(json.dumps({"digest": digest, "apr_ref": args.apr, "apr_status": apr["status"]},
                     sort_keys=True))
    return 0


def _cmd_bulk_approvals_approve(args: Any) -> int:
    actor = str(getattr(args, "actor", "") or "") or _current_actor()
    if actor == AUTHORIZED_ASK_PROFILE:
        print(f"{REFUSAL_PREFIX} approval_duplicate_actor: {AUTHORIZED_ASK_PROFILE!r} files the ask; "
              "an approval must come from another profile")
        return 2
    if actor not in profiles_on_host():
        print(f"{REFUSAL_PREFIX} approval_unknown_actor: {actor!r} is not a profile on this host")
        return 2
    rows = bundle_for(args.digest)
    if not rows:
        print(f"{REFUSAL_PREFIX} approval_unknown_digest: no ask names digest {args.digest}")
        return 2
    approach = str(rows[-1].get("approach") or "")
    if str(getattr(args, "approach", "") or "") != approach:
        print(f"{REFUSAL_PREFIX} approval_approach_mismatch: this approval does not name the "
              f"ask's approach. Required: --approach \"{approach}\"")
        return 2
    record_approval(actor=actor, digest=args.digest, approach=approach,
                    reason=str(getattr(args, "reason", "") or ""))
    print(json.dumps({"digest": args.digest, "actor": actor, "recorded": True}, sort_keys=True))
    return 0


def _cmd_bulk_approvals_list(args: Any) -> int:
    rows = read_ledger()
    if getattr(args, "json", False):
        print(json.dumps(rows, sort_keys=True))
        return 0
    for row in rows:
        print(f"{row.get('role'):8} {row.get('actor') or row.get('approvers')} "
              f"{(row.get('digest') or '')[:12]} {row.get('board') or ''} "
              f"{row.get('verb') or ''} {row.get('approach') or ''}")
    return 0


def _cmd_bulk_approvals_show(args: Any) -> int:
    rows = bundle_for(args.digest)
    if not rows:
        print(f"{REFUSAL_PREFIX} approval_unknown_digest: no ask names digest {args.digest}")
        return 2
    ask = next((r for r in rows if r.get("role") == "ask"), None)
    approvers = [str(r.get("actor")) for r in rows if r.get("role") == "approval"]
    out = {
        "digest": args.digest,
        "board": (ask or {}).get("board"),
        "verb": (ask or {}).get("verb"),
        "scope": (ask or {}).get("scope"),
        "approach": (ask or {}).get("approach"),
        "ask_actor": (ask or {}).get("actor"),
        "apr_ref": (ask or {}).get("apr_ref"),
        "apr_status": (ask or {}).get("apr_status"),
        "approvers": approvers,
        "quorum": len(set(approvers)) >= REQUIRED_APPROVALS,
        "rows": len(rows),
    }
    print(json.dumps(out, sort_keys=True, indent=2))
    return 0


def _current_actor() -> str:
    from hermes_cli.profiles import current_profile_name

    return current_profile_name("user") or "user"


def _current_board() -> str:
    from hermes_cli import kanban_db as kb

    return kb.get_current_board()


#: ``hermes kanban bulk-approvals <verb>`` -> handler. Dispatched from ``kanban._cmd_bulk_approvals``.
APPROVAL_HANDLERS = {
    "ask": _cmd_bulk_approvals_ask,
    "approve": _cmd_bulk_approvals_approve,
    "list": _cmd_bulk_approvals_list,
    "show": _cmd_bulk_approvals_show,
}
