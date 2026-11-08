"""ONE promotion predicate, four readers — never four copies of it.

The board's promotion decision has been made in more than one place, from different
inputs: :func:`hermes_cli.kanban_db.recompute_ready` reads the GRAPH (all parents in
``done``/``archived``) and promotes, ``kanban unblock`` / ``kanban promote`` flip the
status with no work check at all, and the dependency notifier describes a promotion
from the row's status alone. That is how a notice can promise a promotion while the
row carries a live ``worker_pid`` and a 47-second-old heartbeat, and how a human
pressing ``unblock`` buys exactly the crash the automaton would have bought.

:func:`promote_state` is the one call all four make. It returns a
:class:`PromotionState`, whose ``outcome`` is always one of the ordered tuple
:data:`PROMOTION_OUTCOMES`, first match wins:

``IN_FLIGHT``       a live ``worker_pid`` and a heartbeat inside the window — a run is
                    on it, so nothing else may promote it.
``HUMAN_HOLD``      ``needs_input`` / sticky / a manual hold: a person has to act.
``SCHEDULED``       a reserved window or a ``due_at`` in the future.
``NOT_APPLICABLE``  no PR carrier on the record — the ONLY path on which graph-only
                    promotion survives, and the receipt says so.
``PROMOTABLE``      the tracker says the carrier is clean and near enough the base.
``NOT-PROMOTABLE``  the tracker says it is not (merged/closed/dirty/blocked/draft, or
                    behind beyond the declared threshold).
``UNCHECKED``       the tracker read failed, timed out or rate-limited.

**UNCHECKED refuses.** A tracker read that could not be made is a THIRD state, never a
fall-through: the only difference between ``UNCHECKED`` and ``NOT-PROMOTABLE`` is the
remedy a human is offered, never the outcome of the promotion. There is no ``|| true``,
no bare ``except`` that continues, and no default-to-clean anywhere in this module.

**"I did not ask" and "I could not ask" are different receipts.** A card with no PR is
``NOT-APPLICABLE``; a card with a PR whose tracker blew up is ``UNCHECKED``.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from hermes_cli.kanban_landing_carrier import declared_prs, normalize_ref

#: Every outcome, in classifier order. ``PROMOTABLE`` and ``NOT_APPLICABLE`` are the only
#: two on which a promotion may proceed.
PROMOTION_OUTCOMES = (
    "IN_FLIGHT",
    "HUMAN_HOLD",
    "SCHEDULED",
    "NOT_APPLICABLE",
    "PROMOTABLE",
    "NOT-PROMOTABLE",
    "UNCHECKED",
)

#: Outcomes that permit the promotion. NOT_APPLICABLE is the graph-only path and it is
#: deliberately admissible — but only because the receipt SAYS it was not asked.
PROMOTION_ALLOWED = ("PROMOTABLE", "NOT_APPLICABLE")

#: A heartbeat older than this is not a heartbeat: the pid may linger, the run is gone.
DEFAULT_HEARTBEAT_WINDOW_S = 600

#: How far behind the base a carrier may be and still be promoted. Declared, printed in
#: the receipt, and overridable so the number is never a mystery in a refusal.
DEFAULT_MAX_BEHIND = 100

#: Block kinds that are a human's decision, not a worker's. ``needs_input`` is the board's
#: own spelling; the rest are the manual-hold spellings the fleet writes. A bare ``manual``
#: block is NOT here on purpose: an operational park is not a human waiting on an answer.
HUMAN_HOLD_KINDS = ("needs_input", "manual_hold", "human_hold", "hold")


class TrackerError(RuntimeError):
    """The tracker could not answer. Never caught into a clean verdict."""


class PromotionRefused(RuntimeError):
    """A door refused a promotion/release, carrying the receipt that says why."""

    def __init__(self, state: "PromotionState"):
        super().__init__(state.receipt())
        self.state = state


@dataclass(frozen=True)
class PromotionState:
    """One promotion decision, with the evidence it was made from."""

    outcome: str
    reason: str
    fields: Mapping[str, Any] = field(default_factory=dict)
    pr: Optional[str] = None
    behind_by: Optional[int] = None
    threshold: int = DEFAULT_MAX_BEHIND
    measured_at: float = 0.0

    @property
    def promotable(self) -> bool:
        """True only for the two admissible outcomes — UNCHECKED is NOT one of them."""
        return self.outcome in PROMOTION_ALLOWED

    def as_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "reason": self.reason,
            "pr": self.pr,
            "fields": dict(self.fields),
            "behind_by": self.behind_by,
            "threshold": self.threshold,
            "measured_at": self.measured_at,
            "promotable": self.promotable,
        }

    def receipt(self) -> str:
        """One line a reader can act on: outcome, why, and the evidence behind it."""
        bits = [f"promote_state={self.outcome}", f"reason={self.reason}"]
        if self.pr:
            bits.append(f"pr={self.pr}")
        if self.fields:
            shape = ",".join(f"{k}={v}" for k, v in sorted(self.fields.items()) if v is not None)
            if shape:
                bits.append(f"tracker[{shape}]")
        if self.behind_by is not None:
            bits.append(f"behind_by={self.behind_by}/{self.threshold}")
        bits.append(f"measured_at={int(self.measured_at)}")
        return " ".join(bits)


def heartbeat_window_s() -> int:
    """The liveness window for IN_FLIGHT, from env with a declared default."""
    raw = os.environ.get("HERMES_KANBAN_INFLIGHT_HEARTBEAT_S")
    try:
        return max(1, int(raw)) if raw not in (None, "") else DEFAULT_HEARTBEAT_WINDOW_S
    except (TypeError, ValueError):
        return DEFAULT_HEARTBEAT_WINDOW_S


def behind_threshold() -> int:
    """The behind-by threshold, from env with a declared default."""
    raw = os.environ.get("HERMES_KANBAN_PROMOTE_MAX_BEHIND")
    try:
        return max(0, int(raw)) if raw not in (None, "") else DEFAULT_MAX_BEHIND
    except (TypeError, ValueError):
        return DEFAULT_MAX_BEHIND


def _field(row, key, default=None):
    """One column of a row / mapping / object — absent reads as the default."""
    if row is None:
        return default
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return getattr(row, key, default)


def pid_alive(pid) -> bool:
    """Is this pid a live process? A pid that is gone is not a claim."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def _numeric(value) -> Optional[float]:
    """Epoch seconds from an int, float or numeric string; None otherwise."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pr_ref(task) -> tuple[Optional[str], int, list[str]]:
    """-> ``(ref, tier, keys_seen)`` from the card's own PR declaration.

    Reuses :func:`kanban_landing_carrier.declared_prs` — the fleet's ONE reading of a PR
    declaration — so the promotion gate and the landing gate can never disagree about
    whether a card named a PR.
    """
    metadata = _field(task, "metadata")
    if metadata is None:
        metadata = _field(task, "delivery_metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            metadata = None
    runs_metadata = _field(task, "runs_metadata") or ()
    if isinstance(runs_metadata, Mapping):
        runs_metadata = [runs_metadata]
    contract = _field(task, "completion_contract")

    def _dicts(items) -> list:
        out = []
        for item in items:
            if isinstance(item, str):
                try:
                    item = json.loads(item)
                except (TypeError, ValueError):
                    continue
            if isinstance(item, Mapping):
                out.append(dict(item))
        return out

    tier, refs, keys = declared_prs(
        metadata=dict(metadata) if isinstance(metadata, Mapping) else None,
        runs_metadata=_dicts(runs_metadata),
        contract=contract if isinstance(contract, str) else None,
    )
    ref = normalize_ref(refs[0]) if refs else None
    return ref, tier, list(keys)


def gh_tracker(ref: str, *, timeout: float = 25.0) -> dict:
    """The real tracker: ``gh pr view`` plus a compare for ``behind_by``.

    EVERY failure raises :class:`TrackerError` — a non-zero exit, unparseable JSON, a
    timeout, a missing ``gh``. The caller turns that into ``UNCHECKED``, which refuses.
    """
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", ref, "--json",
             "state,mergeable,mergeStateStatus,headRefOid,headRefName,baseRefName"],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise TrackerError(f"gh not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise TrackerError(f"gh pr view {ref} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise TrackerError(
            f"gh pr view {ref} exited {proc.returncode}: {(proc.stderr or '').strip()[:400]}")
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError as exc:
        raise TrackerError(f"gh pr view {ref} returned unparseable JSON: {exc}") from exc
    if not isinstance(data, dict) or not data:
        raise TrackerError(f"gh pr view {ref} returned no object")

    base = data.get("baseRefName") or "main"
    head = data.get("headRefName")
    repo = ref.rsplit("#", 1)[0]
    behind = None
    if head:
        try:
            cmp_proc = subprocess.run(
                ["gh", "api", f"repos/{repo}/compare/{base}...{head}", "--jq", ".behind_by"],
                capture_output=True, text=True, timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise TrackerError(f"gh not found: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise TrackerError(f"gh api compare {ref} timed out after {timeout}s") from exc
        if cmp_proc.returncode != 0:
            raise TrackerError(
                f"gh api compare {base}...{head} exited {cmp_proc.returncode}: "
                f"{(cmp_proc.stderr or '').strip()[:400]}")
        try:
            behind = int((cmp_proc.stdout or "").strip())
        except ValueError as exc:
            raise TrackerError(f"gh api compare returned a non-integer: {exc}") from exc
    return {
        "state": data.get("state"),
        "mergeable": data.get("mergeable"),
        "mergeStateStatus": data.get("mergeStateStatus"),
        "headRefOid": data.get("headRefOid"),
        "headRefName": head,
        "baseRefName": base,
        "behind_by": behind,
    }


#: Merge states that are a refusal regardless of ``mergeable``.
_BLOCKING_STATES = ("DIRTY", "BLOCKED", "DRAFT", "BEHIND", "UNKNOWN")


#: Sentinel for "the caller did not choose a tracker" — resolved at CALL time so the module
#: function :func:`gh_tracker` can be swapped (tests, and any future transport).
_DEFAULT_TRACKER = object()


def _active_tracker():
    """The tracker this process should use: the module's, or none when disabled by env.

    ``HERMES_KANBAN_PROMOTE_TRACKER=none`` disables the transport, which yields
    ``UNCHECKED`` on any card that names a PR — never a clean pass.
    """
    if os.environ.get("HERMES_KANBAN_PROMOTE_TRACKER", "").strip().lower() == "none":
        return None
    return gh_tracker


def promote_state(
    task,
    *,
    tracker: Any = _DEFAULT_TRACKER,
    now: Optional[float] = None,
    threshold: Optional[int] = None,
) -> PromotionState:
    """The ONE promotion decision for one card.

    ``task`` is a row (``sqlite3.Row``, mapping or object) carrying the columns the
    decision needs: ``status``, ``worker_pid``, ``last_heartbeat_at``, ``due_at``,
    ``block_kind``, ``metadata`` and optionally ``runs_metadata``. ``tracker`` is a callable
    ``ref -> mapping`` — tests inject a fake and never touch the network; passing ``None``
    means "no tracker is wired", which is ``UNCHECKED``, not a clean pass.
    """
    now = time.time() if now is None else float(now)
    threshold = behind_threshold() if threshold is None else int(threshold)
    if tracker is _DEFAULT_TRACKER:
        tracker = _active_tracker()
    status = str(_field(task, "status") or "")

    # 1. IN_FLIGHT — a live claim beats every other reading. The graph does not know this;
    #    the row does.
    pid = _field(task, "worker_pid")
    hb = _numeric(_field(task, "last_heartbeat_at"))
    window = heartbeat_window_s()
    if pid and pid_alive(pid):
        if hb is not None and (now - hb) <= window:
            return PromotionState(
                "IN_FLIGHT",
                f"worker_pid {int(pid)} is alive and its heartbeat is {int(now - hb)}s old "
                f"(window {window}s): a run is on this card, so nothing may promote it",
                fields={"worker_pid": int(pid), "heartbeat_age_s": int(now - hb)},
                threshold=threshold, measured_at=now)
        age = "no heartbeat recorded" if hb is None else f"heartbeat {int(now - hb)}s old"
        return PromotionState(
            "IN_FLIGHT",
            f"worker_pid {int(pid)} is still alive but {age} (window {window}s): a stale "
            f"claim is the dispatcher's to reap, not a promotion's to overwrite",
            fields={"worker_pid": int(pid), "heartbeat_age_s": None if hb is None else int(now - hb)},
            threshold=threshold, measured_at=now)

    # 2. HUMAN_HOLD — needs_input / sticky / a manual hold.
    kind = str(_field(task, "block_kind") or "").strip()
    sticky = _field(task, "sticky")
    if kind in HUMAN_HOLD_KINDS or (sticky not in (None, 0, "", False)):
        why = f"block_kind={kind}" if kind else "sticky"
        return PromotionState(
            "HUMAN_HOLD",
            f"{why}: a person has to clear this, and no automaton may promote past it",
            fields={"block_kind": kind or None, "sticky": sticky},
            threshold=threshold, measured_at=now)

    # 3. SCHEDULED — a reserved window or a future clock.
    due = _numeric(_field(task, "due_at"))
    if status == "scheduled" or (due is not None and due > now):
        return PromotionState(
            "SCHEDULED",
            f"scheduled until {int(due) if due else 'an unstated clock'}: nothing runs it "
            f"before that clock",
            fields={"due_at": None if due is None else int(due)},
            threshold=threshold, measured_at=now)

    # 4. The tracker — only reachable for a card that carries a PR.
    ref, tier, keys = _pr_ref(task)
    if not ref:
        # NOT_APPLICABLE is the ONE surviving graph-only path, and it SAYS so: "I did not
        # ask" is a receipt, never a silent pass.
        return PromotionState(
            "NOT_APPLICABLE",
            "no PR carrier on this record (no published_pr / pr / pr_url / pr_number / "
            "pull_request / exact PR-URL completion_contract): graph-only promotion applies "
            "and no tracker was consulted",
            fields={"declared_keys": ",".join(keys) or None, "tier": tier},
            threshold=threshold, measured_at=now)

    if tracker is False:
        # A ROW-ONLY reader (the dependency notifier) is not asking the tracker a question —
        # a carrier never stops a card being claimed, so its readiness is decided by the row.
        # That is "I did not ask", stated as such, never a clean pass shared with "I could not
        # ask" (``UNCHECKED``).
        return PromotionState(
            "NOT_APPLICABLE",
            f"row-only reader: PR {ref} is declared but the carrier was not consulted "
            f"(notifier scope)",
            pr=ref, threshold=threshold, measured_at=now)
    if tracker is None:
        return PromotionState(
            "UNCHECKED",
            f"PR {ref} is declared but no tracker is wired: an unread carrier refuses",
            pr=ref, threshold=threshold, measured_at=now)

    try:
        fields = dict(tracker(ref) or {})
    except TrackerError as exc:
        return PromotionState("UNCHECKED", str(exc), pr=ref, threshold=threshold,
                              measured_at=now)
    except Exception as exc:  # noqa: BLE001 - ANY tracker failure is UNCHECKED, never clean
        return PromotionState(
            "UNCHECKED", f"tracker raised {type(exc).__name__}: {exc}", pr=ref,
            threshold=threshold, measured_at=now)

    behind = fields.get("behind_by")
    try:
        behind = None if behind is None else int(behind)
    except (TypeError, ValueError):
        return PromotionState(
            "UNCHECKED", f"tracker returned a non-integer behind_by={behind!r}", pr=ref,
            fields=fields, threshold=threshold, measured_at=now)

    state = str(fields.get("state") or "").upper()
    mergeable = str(fields.get("mergeable") or "").upper()
    state_status = str(fields.get("mergeStateStatus") or "").upper()

    if state and state != "OPEN":
        return PromotionState(
            "NOT-PROMOTABLE", f"PR {ref} is {state}, not OPEN: there is nothing left to "
            f"promote onto it", pr=ref, fields=fields, behind_by=behind,
            threshold=threshold, measured_at=now)

    if mergeable != "MERGEABLE":
        return PromotionState(
            "NOT-PROMOTABLE", f"PR {ref} mergeable={mergeable or 'absent'}, not MERGEABLE",
            pr=ref, fields=fields, behind_by=behind, threshold=threshold, measured_at=now)

    if state_status in _BLOCKING_STATES:
        return PromotionState(
            "NOT-PROMOTABLE", f"PR {ref} mergeStateStatus={state_status}: the carrier is not "
            f"clean enough to promote", pr=ref, fields=fields, behind_by=behind,
            threshold=threshold, measured_at=now)

    if behind is not None and behind > threshold:
        return PromotionState(
            "NOT-PROMOTABLE", f"PR {ref} is behind_by={behind} > threshold {threshold}",
            pr=ref, fields=fields, behind_by=behind, threshold=threshold, measured_at=now)

    return PromotionState(
        "PROMOTABLE",
        f"PR {ref} is OPEN, mergeable and {('behind by %d' % behind) if behind is not None else 'at an unmeasured distance'} "
        f"(threshold {threshold})",
        pr=ref, fields=fields, behind_by=behind, threshold=threshold, measured_at=now)


def _sticky_block(conn, task_id: str) -> bool:
    """True when a breaker trip stamped ``sticky`` — a human must clear this card.

    The KERNEL's ``_has_sticky_block`` also returns True for any plain ``blocked`` event,
    because ``recompute_ready`` must never auto-promote a card a person just blocked. That
    is a different question from this one: a blocked card is not a promotion CANDIDATE, so
    treating every block as a HUMAN_HOLD here would make ``unblock`` refuse the very door
    that releases an operational park. What is a human hold is an explicit ``needs_input``
    (``block_kind``) or a give-up the breaker stamped ``sticky`` — those two, and only those.
    """
    trip = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'gave_up' AND id > "
        "COALESCE((SELECT MAX(id) FROM task_events WHERE task_id = ? AND kind = 'unblocked'),0) "
        "ORDER BY id DESC LIMIT 1", (task_id, task_id)).fetchone()
    if not trip:
        return False
    try:
        payload = json.loads(trip["payload"] or "{}")
    except (TypeError, ValueError):
        return False
    return bool(isinstance(payload, dict) and payload.get("sticky"))


def promote_state_for(conn, task_id: str, **kwargs) -> PromotionState:
    """The same decision, read from the board for ``task_id`` (row + run metadata)."""
    row = conn.execute(
        "SELECT id, status, worker_pid, last_heartbeat_at, due_at, block_kind, "
        "       completion_contract, current_run_id "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return PromotionState("UNCHECKED", f"task {task_id} not found", measured_at=time.time())
    task = dict(row)
    task["sticky"] = _sticky_block(conn, task_id)
    runs_meta: list = []
    try:
        for r in conn.execute(
                "SELECT metadata FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 5",
                (task_id,)).fetchall():
            raw = r["metadata"]
            if raw:
                try:
                    runs_meta.append(json.loads(raw))
                except (TypeError, ValueError):
                    continue
    except Exception:  # noqa: BLE001 - a board without a runs table has no run metadata
        runs_meta = []
    task["runs_metadata"] = runs_meta
    return promote_state(task, **kwargs)
