"""kanban_crash_sweep_guard — the reclaim sweep may not act on a liveness substrate it cannot vouch for.

One module, one purpose: the two clauses of the platform-stl ruling ``t_63a20c59`` (2026-10-10)
that stand between a *bad liveness evaluation* and a fleet-wide outage. Both are decided
DETERMINISTICALLY -- a count, a shape and a timestamp, never a model and never prose a reader has to
interpret. Carrier card: ``t_fc2cf9ee``.

R4 -- the mass-crash abort
==========================

A reclaim sweep that would close ``>= threshold`` (default 3) runs in ONE tick is a
**host-systemic signature, not N card failures**. The sweep must refuse the whole write, stay
handed-off (nothing booked, no run ended, no claim released) and fail LOUD: one stderr line with a
deterministic signature, one board event on every card it would have taken, and one acting card
filed to the design authority. The failure mode this exists to stop is measured: 2026-10-10, all 21
``crashed`` runs on board ``migrations`` fell into three instants (8 at ``ended_at`` 14:20:37, 5 at
13:35:34, 8 at 00:01:23) and every one carried ``pid N not alive``; pid 70697 was measured ALIVE
eight minutes after the 14:20:37 sweep and still writing the live tree. NOT ONE of the 21 was a
genuine crash. With this guard the reclaim would have refused all three sweeps and left 21 cards and
~21 live workers alone.

**Which closures are counted, and why the class is narrowed.** The counted class is the
*unexplained-death* family -- a closure whose verdict is that the process is gone but which carries
no evidence of its own:

* ``unknown`` (``pid N not alive`` -- the measured census),
* ``nonzero_exit`` (``pid N exited with code N``),
* ``signaled`` (``pid N killed by signal N``).

That is exactly the class in which a broken liveness witness masquerades as N card failures.
``protocol_violation`` (the worker exited rc=0 and only skipped its paperwork) and
``terminal_provider`` (the provider rejected the credential/model) each carry their own evidence and
their own bounded budget, ``rate_limited`` is not a crash at all, and ``substrate_import`` (the
worker's own output names a module THIS tree ships that its interpreter could not resolve) carries
the artifact and its hash. Counting those would refuse a
*legitimate* sweep of N cards whose workers each finished successfully -- the false-refusal storm
this guard must not become. The narrow class is named in code (:data:`UNEXPLAINED_DEATH_KINDS`), not
left to a reader of the sweep.

**One tick IS one ``ended_at``.** The ruling names two spellings of one signature: ">= 3 runs closed
``crashed`` in one tick, OR with one identical ``ended_at``". Within a single tick every closure is
stamped with the same instant by construction, so the two are one measurement and the guard is one
comparison. Nothing is inferred from card identity, prose or error text.

The abort is a verdict reached BEFORE the sweep writes, at the granularity of the sweep: a wrong
refusal costs a loud card and is reversible (raise the threshold, or disable it with
``KANBAN_MASS_CRASH_ABORT_THRESHOLD``), never a silent board mutation.

R6 -- the deployed-identity generation
=====================================

Deployment is the deliverable, and the *deployed* generation must be checked. This class has been
fixed and has recurred (t_1e6a58c3 2026-09-25, the 2026-09-26 six-worker tick, the 2026-09-29
regression window pinned by ``tests/hermes_cli/test_kanban_liveness_witness.py``, t_ae80d242,
2026-10-10) because each fix lived as an override or an in-memory build and vanished on the next tree
write or interpreter change.

Two mechanisms, both deterministic:

1. **The generation stamp.** The identity shape a dispatcher WRITES is stamped on disk
   (``<kanban_home>/kanban/dispatcher_identity_generation.json``) on the first tick and compared on
   every later tick. A divergence -- an interpreter move that changes the shape, a tree write that
   drops the witness bytes -- is detected in ONE tick and alerted, instead of being discovered by a
   false crash sweep hours later. The comparison is STRUCTURAL (:func:`shapes_comparable`): the
   interpreter tag and which probe spelled the witness are EVIDENCE, so a tick that produces the
   same field structure is the same generation whatever process ran it. Measured 2026-10-10, three
   incomparable shapes in circulation at once: ``'|<abs-epoch-cs>'`` (``.venv`` 3.11.16 + psutil),
   ``None`` -> ``'unverified'`` (the 3.14 gateway interpreter, no psutil), and
   ``bootsession:<uuid>|<ref_cs>|<boot-relative start>`` (the live rows).

2. **The per-row hold.** The same comparison is made against the fingerprints actually READ off the
   running rows. When the shape the sweep READS was built by a different identity generation than
   the shape it would WRITE, the two values were never comparable and the row is HELD -- not
   reclaimed, not counted, its worker's claim left alone. This is R2's ``UNKNOWN -> HOLD`` at the
   granularity of one row, and it is the clause the 2026-10-10 outage needed: the live rows carried
   a three-field ``bootsession:`` fingerprint while the tree wrote a two-field one, so
   ``_pid_recycled`` compared two different kinds of value, called the difference "recycled", and
   booked 21 live workers as crashed. A row with no fingerprint (legacy) or an ``unverified`` marker
   is NOT held -- the existing bare-existence rule owns those, and holding them would strand every
   pre-fingerprint card.

Boundaries, deliberately:

* **The refusal path never mutates board state.** A refused sweep writes nothing; the loud path is a
  log line, an evidence event per card it would have taken, and one acting card for the design
  authority. A board that refuses the actor card is logged with the key it could not file and never
  takes the dispatcher tick down with it.
* **The per-row hold is the narrow interlock, not a whole-sweep refusal.** It never strands a
  genuinely dead worker of the current generation, because such rows are comparable by definition.
* **No import of :mod:`hermes_cli.kanban_db` at module scope** (import-cycle discipline, as in
  :mod:`hermes_cli.tree_identity`); the two helpers that need it late-bind.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import time
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any
from typing import Iterable
from typing import Mapping
from typing import Optional

logger = logging.getLogger(__name__)

# --- identity shapes (shared by R4's hold and R6's stamp) ------------------------------------

IDENTITY_SHAPE_NONE = "none"
IDENTITY_SHAPE_UNVERIFIED = "unverified"
IDENTITY_SHAPE_BOOT_WITNESS = "boot-witness"
IDENTITY_SHAPE_EPOCH_CS = "epoch-cs"
IDENTITY_SHAPE_COMPOSED = "composed"
IDENTITY_SHAPE_LEGACY_INT = "legacy-int"
IDENTITY_SHAPE_OTHER = "other"

#: Families whose values are *identity-bearing*: a comparison between two of these is a real
#: liveness comparison only if their STRUCTURE is comparable (see :func:`shapes_comparable`).
#: ``none`` (a legacy row with no fingerprint) and ``unverified`` are deliberately excluded -- they
#: carry no identity to compare, and the existing bare-existence rule owns them.
IDENTITY_SHAPE_FAMILIES: frozenset[str] = frozenset({
    IDENTITY_SHAPE_BOOT_WITNESS, IDENTITY_SHAPE_EPOCH_CS, IDENTITY_SHAPE_COMPOSED,
})

#: Families that are the SAME structure wearing a different probe's answer:
#: ``<witness>|<reference_cs>|<start>``. Which probe filled the first field (a boot-session UUID,
#: the boot-epoch fallback) is a VALUE, not a structure -- and the value comparator already answers
#: UNKNOWN, never DEAD, for mixed witness forms -- so these two are interchangeable at equal field
#: counts. ``epoch-cs`` is NOT here: it is ``<empty witness>|<reference>`` with no start field, a
#: different structure that only compares with itself.
WITNESS_BEARING_FAMILIES: frozenset[str] = frozenset({
    IDENTITY_SHAPE_BOOT_WITNESS, IDENTITY_SHAPE_COMPOSED,
})


def shape_family(shape: str) -> str:
    """``"boot-witness/3"`` -> ``"boot-witness"``."""
    return str(shape).split("/", 1)[0]


def _shape_field_count(shape: str) -> Optional[int]:
    """``"boot-witness/3"`` -> ``3``; a token that names no field count -> ``None``."""
    _family, sep, count = str(shape).partition("/")
    if not sep:
        return None
    try:
        return int(count)
    except (TypeError, ValueError):
        return None


def identity_shape(sample: Any) -> str:
    """Classify one ``worker_started_at`` fingerprint into a comparable generation token.

    The token names the STRUCTURE, not the value -- the family and the FIELD COUNT, because the two
    generations measured on 2026-10-10 differ exactly there (``bootsession:<uuid>|<ref>|<start>``,
    three fields, versus ``bootsession:<uuid>|<start>``, two). Two fingerprints whose tokens differ
    were never comparable; the caller holds the row instead of comparing them.
    """
    if sample is None:
        return IDENTITY_SHAPE_NONE
    if isinstance(sample, bool):
        return IDENTITY_SHAPE_OTHER
    if isinstance(sample, int):
        return IDENTITY_SHAPE_LEGACY_INT
    text = str(sample)
    if text == "":
        return IDENTITY_SHAPE_NONE
    if text == IDENTITY_SHAPE_UNVERIFIED:
        return IDENTITY_SHAPE_UNVERIFIED
    fields = text.count("|") + 1
    if text.startswith("bootsession:"):
        return f"{IDENTITY_SHAPE_BOOT_WITNESS}/{fields}"
    if text.isdigit():
        return IDENTITY_SHAPE_LEGACY_INT
    if "|" in text:
        head, _, tail = text.partition("|")
        if head == "" and tail.isdigit():
            return f"{IDENTITY_SHAPE_EPOCH_CS}/{fields}"
        return f"{IDENTITY_SHAPE_COMPOSED}/{fields}"
    return IDENTITY_SHAPE_OTHER


def shapes_comparable(write_shape: str, read_shape: str) -> bool:
    """True when a liveness comparison between these two fingerprints means something.

    Comparability is STRUCTURAL, not equal-by-spelling. ``boot-witness/3`` and ``composed/3`` are
    both ``<witness>|<reference_cs>|<start>``: which probe answered the first field (a boot-session
    UUID, the boot-epoch fallback) is a VALUE inside the record, never a different structure. The
    value comparator already answers UNKNOWN -- never DEAD -- when two witnesses are not of a
    comparable form, so those two families are interchangeable at equal field counts.

    The field count IS the structure. ``epoch-cs/2`` vs ``boot-witness/3`` and ``boot-witness/2``
    vs ``boot-witness/3`` have different numbers of fields and stay incomparable -- that is the
    measured 2026-10-10 condition. ``epoch-cs`` (an empty witness and a reference, no start) only
    compares with itself. Anything involving ``none`` / ``unverified`` / ``legacy-int`` is left to
    the existing bare-existence rule.
    """
    write_shape = str(write_shape)
    read_shape = str(read_shape)
    if write_shape == read_shape:
        return True
    write_family = shape_family(write_shape)
    read_family = shape_family(read_shape)
    if write_family not in IDENTITY_SHAPE_FAMILIES or read_family not in IDENTITY_SHAPE_FAMILIES:
        return True
    if write_family not in WITNESS_BEARING_FAMILIES or read_family not in WITNESS_BEARING_FAMILIES:
        # ``epoch-cs`` is its own structure (its only comparable partner is itself, above).
        return False
    return _shape_field_count(write_shape) == _shape_field_count(read_shape)


# --- R4: the mass-crash abort ----------------------------------------------------------------

#: Default number of unexplained-death closures in ONE tick that makes a sweep host-systemic.
MASS_CRASH_ABORT_THRESHOLD_DEFAULT = 3

#: Operator override. ``0`` (or any value < 2) disables the guard entirely -- the documented
#: rollback for a wrong refusal. Read at decision time, so a long-lived dispatcher picks it up
#: without a restart.
MASS_CRASH_ABORT_THRESHOLD_ENV = "KANBAN_MASS_CRASH_ABORT_THRESHOLD"

#: Board event kind appended to every card a refused sweep would have taken. Evidence, never a
#: status change: the card keeps its ``running`` status and its claim.
MASS_CRASH_ABORT_EVENT = "crash_sweep_aborted"

#: Stable, greppable prefix of the deterministic refusal signature.
MASS_CRASH_SIGNATURE_PREFIX = "mass-crash-sweep:"

#: The design authority the acting card is filed to.
MASS_CRASH_ACTOR_ASSIGNEE = "platform-stl"
MASS_CRASH_ACTOR_TITLE = (
    "[guard] reclaim sweep REFUSED: >= {count} runs would close `crashed` in one tick"
)
#: A HOLD is not a refusal: nothing was refused, so the count is 0 and saying "REFUSED: >= 0 runs"
#: would be nonsense on the board. The hold is reported in its own words (R6/R2).
MASS_CRASH_HELD_ACTOR_TITLE = (
    "[guard] reclaim sweep HOLDING {held} row(s): identity generations not comparable"
)
MASS_CRASH_ACTOR_EVENT = "mass_crash_sweep_actor_filed"
#: The FAMILY prefix every crash-sweep acting card shares -- what the board's own lookups scan.
MASS_CRASH_ACTOR_IDEMPOTENCY_PREFIX = "mass-crash:"
#: The condition KINDS inside the family. A refusal (too many unexplained deaths) and a hold (the
#: sweep cannot compare identities) are DIFFERENT conditions on the same board, so they own
#: different standing cards -- naming the kind in the key is what keeps the fix from collapsing two
#: escalations into one card whose title only ever describes the first of them.
MASS_CRASH_REFUSED_CONDITION = "refused"
MASS_CRASH_HELD_CONDITION = "held"

#: Appended to the EXISTING actor card when a LATER tick refuses on the same board. The condition
#: is the same one, so it costs zero extra cards -- but the newer tick's facts (count, ended_at,
#: held rows) are still recorded, because the held count moves inside one condition (11 -> 5 -> 3)
#: and the operator needs to see it.
MASS_CRASH_SWEEP_REFUSED_AGAIN_EVENT = "sweep_refused_again"

#: The verdicts that mean "the process is gone" WITHOUT evidence of their own -- the only class a
#: broken liveness witness can forge. See the module docstring.
UNEXPLAINED_DEATH_KINDS = frozenset({"unknown", "nonzero_exit", "signaled"})

#: A card that still HOLDS its idempotency key but holds no live filing. The kernel's dedupe fence
#: is ``status != 'archived'``, so a card in any of these states answers the "does this key already
#: have a card" lookup while nobody is working it -- the condition is live again and the card has to
#: be RE-ARMED, never silently counted as "already paged".
_TERMINAL_ACTOR_STATUSES = frozenset({"done", "archived", "failed", "cancelled"})

#: The statuses :func:`hermes_cli.kanban_db.reopen_task` is the door out of (it refuses any other).
_REOPENABLE_ACTOR_STATUSES = frozenset({"done"})

#: The line a re-assertion appends to the card's thread. It carries the tick-INVARIANT part of the
#: condition (the count, and the held shapes when the tick held rows), never the tick instant: a
#: sustained refusal ticks about once a minute, and a comment per tick is the noise this change
#: removes. Delivered once per CHANGED reading, not once per tick.
_READING_MARKER = "crash-sweep reading:"


@dataclass(frozen=True)
class SweepClosure:
    """One run a reclaim sweep is about to close, built by the caller from the sweep's own rows."""

    task_id: str
    pid: Optional[int]
    kind: str
    shape: str = IDENTITY_SHAPE_NONE
    protocol_violation: bool = False
    terminal_provider: bool = False
    rate_limited: bool = False
    substrate_import: bool = False
    """The worker's own output attributed its death to a module THIS tree ships: the closure
    carries its evidence (provider path, hash), so it is not an unexplained death. Counting it
    would refuse the sweep on a fault that recurs every tick, freezing the rows as ``running``
    with dead pids instead of releasing them spaced."""
    claim_lock: Optional[str] = None

    @property
    def is_unexplained_death(self) -> bool:
        """True when this closure is the class a bad liveness evaluation can forge."""
        if (self.protocol_violation or self.terminal_provider or self.rate_limited
                or self.substrate_import):
            return False
        return self.kind in UNEXPLAINED_DEATH_KINDS

    def incomparable_with(self, write_shape: str) -> bool:
        """True when this row's fingerprint cannot be compared with the shape we would write."""
        return not shapes_comparable(write_shape, self.shape)


@dataclass(frozen=True)
class MassCrashVerdict:
    """The R4 count decision. Deterministic: a count, a threshold and one timestamp."""

    abort: bool
    count: int
    threshold: int
    ended_at: int
    signature: str
    task_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SweepAssessment:
    """The whole R4/R6 answer for one sweep: what to refuse, and what to hold."""

    verdict: MassCrashVerdict
    held: tuple[str, ...] = ()
    """Task ids whose read/write identity shapes are incomparable -- not reclaimed, not counted."""
    held_shapes: tuple[str, ...] = ()
    """Distinct ``"<read->write>"`` shape pairs behind :attr:`held`, for the evidence line."""

    @property
    def refusal_needed(self) -> bool:
        return self.verdict.abort

    @property
    def alerting(self) -> bool:
        return self.verdict.abort or bool(self.held)

    @property
    def signature(self) -> str:
        return self.verdict.signature


def mass_crash_threshold(environ: Optional[Mapping[str, str]] = None) -> int:
    """The configured threshold; a value below 2 disables the guard (operator override)."""
    raw = (environ or os.environ).get(MASS_CRASH_ABORT_THRESHOLD_ENV)
    if raw is None or raw == "":
        return MASS_CRASH_ABORT_THRESHOLD_DEFAULT
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning(
            "kanban crash-sweep guard: %s=%r is not an integer; using %d",
            MASS_CRASH_ABORT_THRESHOLD_ENV, raw, MASS_CRASH_ABORT_THRESHOLD_DEFAULT,
        )
        return MASS_CRASH_ABORT_THRESHOLD_DEFAULT
    return 0 if value < 2 else value


def sweep_signature(count: int, ended_at: int) -> str:
    """``mass-crash-sweep:count=<n>,ended_at=<epoch>`` -- the deterministic refusal identity."""
    return f"{MASS_CRASH_SIGNATURE_PREFIX}count={int(count)},ended_at={int(ended_at)}"


def assess_sweep(
    closures: Iterable[SweepClosure],
    *,
    write_shape: Optional[str] = None,
    threshold: Optional[int] = None,
    now: Optional[int] = None,
) -> SweepAssessment:
    """Decide, before any write, what this sweep may not do.

    Rows whose fingerprint was built by a different identity generation than ``write_shape`` are HELD
    (R6/R2) and excluded from the count; the remaining unexplained-death closures are counted and the
    sweep is refused in full at ``>= threshold`` (R4). A threshold below 2 disables only the count
    guard -- the per-row hold still applies, because an incomparable comparison is a fact about the
    values, not a policy setting.
    """
    effective = mass_crash_threshold() if threshold is None else int(threshold)
    ended_at = int(time.time() if now is None else now)
    shape = write_shape or IDENTITY_SHAPE_NONE

    held: list[str] = []
    pairs: list[str] = []
    counted: list[str] = []
    for closure in closures:
        if closure.incomparable_with(shape):
            held.append(closure.task_id)
            pair = f"{closure.shape}->{shape}"
            if pair not in pairs:
                pairs.append(pair)
            continue
        if closure.is_unexplained_death:
            counted.append(closure.task_id)

    abort = bool(effective >= 2 and len(counted) >= effective)
    return SweepAssessment(
        verdict=MassCrashVerdict(
            abort=abort,
            count=len(counted),
            threshold=effective,
            ended_at=ended_at,
            signature=sweep_signature(len(counted), ended_at),
            task_ids=tuple(counted),
        ),
        held=tuple(held),
        held_shapes=tuple(pairs),
    )


def refusal_lines(assessment: SweepAssessment) -> list[str]:
    """The LOUD one-liners. Deterministic; suitable for the log, the event payload and the card."""
    verdict = assessment.verdict
    lines: list[str] = []
    if verdict.abort:
        lines.append(
            f"{MASS_CRASH_SIGNATURE_PREFIX} REFUSING this reclaim sweep: {verdict.count} runs would "
            f"close `crashed` in one tick (threshold {verdict.threshold}, ended_at {verdict.ended_at})."
        )
        lines.append(
            "A sweep this size is a host-systemic signature, not N card failures -- a broken liveness "
            "witness, not N broken cards. NOTHING was written: no run ended, no claim released, no "
            "failure counted. Cards taken: " + (", ".join(verdict.task_ids) or "(none)") + "."
        )
    if assessment.held:
        lines.append(
            f"{MASS_CRASH_SIGNATURE_PREFIX} HOLDING {len(assessment.held)} row(s) whose identity "
            f"shape is not comparable with this dispatcher's: "
            + ", ".join(assessment.held_shapes)
            + "."
        )
        lines.append(
            "A liveness comparison across two identity generations is meaningless, so these rows are "
            "not reclaimed and not counted (R2 UNKNOWN -> HOLD). Cards held: "
            + ", ".join(assessment.held) + "."
        )
    return lines


def _bullets(values: Iterable[str], *, code: bool = True) -> str:
    items = list(values)
    if not items:
        return "(none)"
    return ", ".join(f"`{v}`" if code else v for v in items)


def refusal_body(assessment: SweepAssessment, *, board: Optional[str] = None, tree: Any = None) -> str:
    """The acting card's body: the counts, the shapes, the timestamp, the cards, the tree."""
    verdict = assessment.verdict
    resolved_tree = tree if tree is not None else dispatching_tree()
    lines = [
        "## Observed",
        "",
        "```",
        *refusal_lines(assessment),
        "```",
        "",
        "Filed by the dispatcher's mass-crash sweep guard (R4/R6 of ruling `t_63a20c59`) --",
        "deterministic, no model was consulted.",
        "",
        f"- refused: `{verdict.abort}`",
        f"- signature: `{verdict.signature}`",
        f"- counted unexplained-death closures: {verdict.count} (threshold {verdict.threshold})",
        f"- tick instant (`ended_at`): {verdict.ended_at}",
        f"- board: `{board or '(current)'}`",
        f"- tree: `{resolved_tree}`",
        "",
        f"- cards taken ({len(verdict.task_ids)}): {_bullets(verdict.task_ids)}",
        f"- rows held on an incomparable identity shape ({len(assessment.held)}): "
        + _bullets(assessment.held),
        f"- shape pairs held (`<read->write>`): {_bullets(assessment.held_shapes)}",
        "",
        "## What was NOT done",
        "",
        "No run was ended, no claim released, no failure counted, no circuit breaker tripped. The",
        "cards keep `running` and their workers keep their claims. A refusal is loud and reversible;",
        "a silent board mutation is not.",
        "",
        "## Operator move",
        "",
        "Confirm the liveness witness (R1-R3 of the ruling; `tests/hermes_cli/test_kanban_liveness_witness.py`)",
        "before re-arming the sweep. To accept a genuine mass crash, raise or disable the count guard",
        f"with `{MASS_CRASH_ABORT_THRESHOLD_ENV}` (a value < 2 disables it). The half that cannot be",
        "disabled from here is the identity-shape hold: two generations of fingerprint were never",
        "comparable, so re-land the witness bytes (or re-record the generation stamp) instead.",
    ]
    return "\n".join(lines)


def _board_key(conn: Any, board: Optional[str]) -> str:
    """The board slug the actor's idempotency key is scoped to (connection first, argument second)."""
    from hermes_cli import kanban_db as kb

    if board:
        try:
            explicit = kb._normalize_board_slug(board)
        except Exception:
            explicit = None
        if explicit:
            return explicit
    try:
        for row in conn.execute("PRAGMA database_list").fetchall():
            if str(row[1]) == "main" and row[2]:
                db_file = Path(str(row[2]))
                if db_file.parent == Path(kb.kanban_home()):
                    return "default"
                if db_file.parent.name:
                    return db_file.parent.name
    except Exception:
        pass
    try:
        return kb._slug_or_default(board)
    except Exception:
        return "default"


def _actor_key(prefix: str, board_key: str) -> str:
    """The STANDING CONDITION: `<prefix><board>` - one acting card per board, full stop.

    It used to be ``<prefix><key_signature>:<board>:<hour-bucket>``, and everything after the board
    was a READING: the condition's shape, the count, and the hour it happened in. Measured on the
    board (card t_e217fc62), that minted a fresh escalation per tick-hour for one unchanged
    condition - ``mass-crash:mass-crash-sweep:count=49:platform:1793509200`` and its neighbours are
    the same condition wearing a clock, one card per hour and a second card the moment the count
    moved 3 -> 5. The readings belong in the BODY, the per-tick event payload and the thread line
    (`_append_reading`), which is where they now live; the card is the condition, and it is re-armed
    (reopened + read) rather than duplicated when the condition recurs.
    """
    return f"{prefix}{board_key}"


def _reading(payload: dict) -> str:
    """The tick-INVARIANT reading a repeat tick adds to the one card (`""` when it carries none).

    The count moves inside one condition (11 -> 5 -> 3) and the held shape set is what a HOLD is
    about; the tick instant is not a reading anyone acts on, so it is deliberately absent.
    """
    parts = []
    if "count" in (payload or {}):
        try:
            parts.append(f"count={int(payload.get('count') or 0)}")
        except (TypeError, ValueError):
            parts.append("count=?")
    held = [str(shape) for shape in (payload or {}).get("held_shapes") or [] if str(shape)]
    if held:
        parts.append("held_shapes=" + ",".join(held))
    return " ".join(parts)


def _last_reading(conn: Any, task_id: str) -> str:
    """The last reading line this guard appended to ``task_id`` (`""` when there is none)."""
    try:
        row = conn.execute(
            "SELECT body FROM task_comments WHERE task_id = ? AND body LIKE ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (task_id, f"%{_READING_MARKER}%"),
        ).fetchone()
    except Exception:  # noqa: BLE001 - a thread that cannot be read is not a filing failure
        return ""
    if row is None:
        return ""
    for line in str(row["body"] or "").splitlines():
        if _READING_MARKER in line:
            # The reading is everything before the parenthetical facts, so a change in the tick
            # instant or the threshold is NOT a changed reading.
            return line.split(_READING_MARKER, 1)[1].split(" (", 1)[0].strip()
    return ""


def _append_reading(
    conn: Any, task_id: str, event_payload: dict, board_key: str, *, filed: bool = False,
) -> None:
    """Thread ONE reading line onto the card that carries the condition.

    ``filed`` marks the line as the reading the card was FILED on; otherwise it is a MOVED reading
    on a card that already existed. Skipped when the reading has not moved (including the
    re-assertion of the filing reading), so a sustained condition stays quiet. Best-effort - a
    thread write that fails is logged, never raised.
    """
    reading = _reading(event_payload)
    if not reading or _last_reading(conn, task_id) == reading:
        return
    from hermes_cli import kanban_db as kb

    payload = event_payload or {}
    note = (
        f"{_READING_MARKER} {reading} (threshold {int(payload.get('threshold') or 0)}, "
        f"tick {int(payload.get('ended_at') or 0)}, board `{board_key}`). "
        + ("The card was filed on this reading." if filed
           else "The condition is the SAME card; only the reading moved.")
    )
    try:
        kb.add_comment(conn, task_id, "kanban-dispatcher", note)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "kanban crash-sweep guard: could not append the reading to %s: %s", task_id, exc,
        )


def _reopen_actor(
    conn: Any, actor_id: str, status: str, *, prefix: str, signature: str,
) -> None:
    """RE-ARM a finished actor card for a recurring condition - never duplicate, never skip.

    The kernel hands a card back for a spent idempotency key whatever its status, so a `done` card
    would otherwise read as "already paged" for the life of the key: the condition recurs, no
    worker is spawned, and nothing on the board moves. `reopen_task` is the door out of `done`
    (audited, and it re-gates descendants); `failed`/`cancelled` have no such door, so the state is
    named in the log instead of being papered over.
    """
    if not actor_id:
        return
    if str(status or "") not in _REOPENABLE_ACTOR_STATUSES:
        logger.warning(
            "kanban crash-sweep guard: %s carries the recurring %s condition but is %r, which "
            "`reopen` does not cover; the card needs a human to re-arm it",
            actor_id, prefix, status,
        )
        return
    from hermes_cli import kanban_db as kb

    try:
        ok, why, _info = kb.reopen_task(
            conn, actor_id, actor="kanban-dispatcher",
            reason=f"{prefix} condition recurred: {signature}",
        )
        if not ok:
            logger.warning(
                "kanban crash-sweep guard: %s could not be reopened (%s); the condition is live "
                "again and the card is read-only", actor_id, why or "refused",
            )
    except Exception as exc:  # noqa: BLE001 - a refused reopen is DATA, never a crash
        logger.warning("kanban crash-sweep guard: reopening %s failed: %s", actor_id, exc)


def _append_repeat_evidence(
    conn: Any, task_id: str, event: str, payload: dict, board_key: str,
) -> None:
    """Record a repeated tick's facts on the card that already carries the episode.

    Evidence only: like :func:`record_refusal_evidence` this never mutates status, claim or run
    rows, and a board that refuses the write is logged rather than allowed to take the tick down.
    """
    from hermes_cli import kanban_db as kb

    try:
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, event, dict(payload, board=board_key))
    except Exception as exc:
        logger.warning(
            "kanban crash-sweep guard: could not append %s evidence to %s: %s", event, task_id, exc,
        )


def _file_actor(
    conn: Any,
    *,
    prefix: str,
    signature: str,
    title: str,
    body: str,
    event: str,
    event_payload: dict,
    board: Optional[str] = None,
    repeat_event: Optional[str] = None,
) -> Optional[str]:
    """File EXACTLY ONE acting card per board for a condition, idempotently, best-effort.

    The idempotency key (``<prefix><board>``) is the whole mechanism, and it names the STANDING
    CONDITION - not the tick, not the count, not the hour: N ticks, in ANY hour, at ANY count or
    shape, resolve to one card. Every earlier shape of this key carried a reading (``ended_at``,
    then ``count``, then ``signature``+hour bucket), and every one of them restocked the board for
    a condition that had not changed -- measured 2026-10-24 as 14 cards in 13 minutes, and again
    2026-11-01 as one card per tick-hour (card t_e217fc62).

    A key that ALREADY has a card is the same condition, and the card is RE-ARMED rather than
    duplicated: a finished card is reopened through the kernel's own door (`_reopen_actor`), the
    newer tick's facts are appended as ``repeat_event`` (a board event, never a status change), and
    a reading that MOVED is threaded onto the card once (`_append_reading`). A board that refuses
    the write is logged with the key it could not file and never takes the tick down.
    """
    from hermes_cli import kanban_db as kb

    board_key = _board_key(conn, board)
    key = _actor_key(prefix, board_key)
    try:
        existing = conn.execute(
            "SELECT id, status FROM tasks WHERE idempotency_key = ? AND status != 'archived' "
            "ORDER BY created_at DESC LIMIT 1",
            (key,),
        ).fetchone()
        if existing is not None:
            existing_id = existing["id"]
            status = str(existing["status"] or "")
            if status in _TERMINAL_ACTOR_STATUSES:
                _reopen_actor(conn, existing_id, status, prefix=prefix, signature=signature)
            if repeat_event:
                _append_repeat_evidence(conn, existing_id, repeat_event, event_payload, board_key)
            _append_reading(conn, existing_id, event_payload, board_key)
            return existing_id
        actor_id = kb.create_task(
            conn,
            title=title,
            body=body,
            assignee=MASS_CRASH_ACTOR_ASSIGNEE,
            created_by="kanban-dispatcher",
            idempotency_key=key,
        )
        with kb.write_txn(conn):
            kb._append_event(conn, actor_id, event, dict(event_payload, board=board_key))
        # The reading the card was FILED on also goes on the thread, so the "append only when it
        # moved" rule has a baseline that survives a restart: the first repeat tick after a
        # restart compares against the card's own last line, not against nothing.
        _append_reading(conn, actor_id, event_payload, board_key, filed=True)
        return actor_id
    except Exception as exc:
        logger.warning(
            "kanban crash-sweep guard: could not file the actor card for %s (key=%s): %s",
            signature, key, exc,
        )
        return None


def record_refusal_evidence(conn: Any, verdict: MassCrashVerdict) -> None:
    """Append the refusal event to EVERY card the sweep would have taken. Never mutates status."""
    from hermes_cli import kanban_db as kb

    payload = {
        "signature": verdict.signature,
        "count": verdict.count,
        "threshold": verdict.threshold,
        "ended_at": verdict.ended_at,
    }
    for task_id in verdict.task_ids:
        try:
            with kb.write_txn(conn):
                kb._append_event(conn, task_id, MASS_CRASH_ABORT_EVENT, dict(payload))
        except Exception as exc:
            logger.warning(
                "kanban crash-sweep guard: could not append refusal evidence to %s: %s", task_id, exc,
            )


def alert_sweep_guard(
    conn: Any,
    assessment: SweepAssessment,
    *,
    board: Optional[str] = None,
    tree: Any = None,
    now: Optional[int] = None,
) -> Optional[str]:
    """The whole loud path for a refused sweep or a held row: log, per-card evidence, one card.

    ``now`` is the caller's clock and is still accepted for it; nothing in the filing path keys on
    time any more -- the card is the CONDITION, so a tick in the next hour lands on the same card
    (that is the fix for card t_e217fc62).
    """
    if not assessment.alerting:
        return None
    for line in refusal_lines(assessment):
        logger.error("kanban crash-sweep guard: %s", line)
    if assessment.verdict.abort:
        record_refusal_evidence(conn, assessment.verdict)
    verdict = assessment.verdict
    actor_signature = verdict.signature if verdict.abort else (
        "held:" + ",".join(assessment.held_shapes)
    )
    # The KEY is the condition: the KIND and the board, and nothing else. Everything that used to
    # follow the board was a reading -- the tick instant (~63 s), the count (which moves inside one
    # episode, 11 -> 5 -> 3), the shape signature, the hour bucket -- and each one re-minted the
    # key, so one unchanged condition restocked the board (measured 2026-10-24: 14 cards in 13
    # minutes; measured 2026-11-01: one card per tick-hour, card t_e217fc62). The signature still
    # carries every one of those facts into the title, the body and the event payload, and a moved
    # reading is threaded onto the one card; the card itself is the condition. The KIND stays in the
    # key because a refusal and a hold are two conditions, not one.
    condition = (MASS_CRASH_REFUSED_CONDITION if verdict.abort else MASS_CRASH_HELD_CONDITION)
    return _file_actor(
        conn,
        prefix=f"{MASS_CRASH_ACTOR_IDEMPOTENCY_PREFIX}{condition}:",
        signature=actor_signature,
        repeat_event=MASS_CRASH_SWEEP_REFUSED_AGAIN_EVENT if verdict.abort else None,
        title=(
            MASS_CRASH_ACTOR_TITLE.format(count=verdict.count, threshold=verdict.threshold)
            if verdict.abort
            else MASS_CRASH_HELD_ACTOR_TITLE.format(held=len(assessment.held))
        ),
        body=refusal_body(assessment, board=board, tree=tree),
        event=MASS_CRASH_ACTOR_EVENT,
        event_payload={
            "signature": verdict.signature,
            "count": verdict.count,
            "threshold": verdict.threshold,
            "ended_at": verdict.ended_at,
            "task_ids": list(verdict.task_ids),
            "held": list(assessment.held),
            "held_shapes": list(assessment.held_shapes),
        },
        board=board,
    )


# --- R6: the deployed-identity generation stamp ----------------------------------------------

#: ``<kanban_home>/kanban/dispatcher_identity_generation.json`` -- machine-global, like the board
#: itself and like :func:`hermes_cli.tree_identity.record_path` (unchanged: this is a different
#: record, so it is a different file).
GENERATION_STAMP_FILENAME = "dispatcher_identity_generation.json"

GENERATION_STAMP_VERSION = 1

GENERATION_DIVERGENCE_EVENT = "identity_generation_divergence"
GENERATION_ACTOR_EVENT = "identity_generation_actor_filed"
GENERATION_ACTOR_IDEMPOTENCY_PREFIX = "identity-generation:"
GENERATION_ACTOR_TITLE = "[guard] dispatcher identity generation changed: {reasons}"

#: The witness symbols R1-R3 land. Recorded for evidence; only their LOSS alerts (an arrival is a
#: fix landing, not a regression).
WITNESS_SYMBOLS: tuple[str, ...] = (
    "_worker_liveness",
    "WORKER_ALIVE",
    "WORKER_DEAD",
    "WORKER_UNKNOWN",
    "host_boot_witness",
)


def dispatching_tree() -> Path:
    """The tree THIS guard is executing from (never a hardcoded path)."""
    return Path(__file__).resolve().parent.parent


def witness_symbols_present(module: Any) -> list[str]:
    """Which of :data:`WITNESS_SYMBOLS` the module the dispatcher executes actually carries."""
    return sorted(name for name in WITNESS_SYMBOLS if hasattr(module, name))


def stamp_path(path: Any = None) -> Path:
    """The generation stamp's path, or the explicit one a caller or a test supplies."""
    if path is not None:
        return Path(path)
    from hermes_cli import kanban_db as kb

    return kb.kanban_home() / "kanban" / GENERATION_STAMP_FILENAME


def _psutil_available() -> bool:
    """Is ``psutil`` importable in THIS interpreter? Measured, never assumed."""
    try:
        import importlib.util

        return importlib.util.find_spec("psutil") is not None
    except Exception:
        return False


def _interpreter_tag(version: Optional[str] = None) -> str:
    """``major.minor`` -- recorded as EVIDENCE, never compared (see :func:`compare_generation`).

    A patch bump and a minor move both stay out of the divergence reasons: what alerts is a change
    in the identity shape actually WRITTEN (a lost psutil, a lost witness symbol, an incomparable
    field structure). The tag names the process that produced the stamp when a human reads the card.
    """
    raw = version or platform.python_version()
    parts = str(raw).split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else str(raw)


def generation_stamp(
    *,
    sample: Any,
    tree: Any = None,
    interpreter: Optional[str] = None,
    psutil: Optional[bool] = None,
    witness_symbols: Optional[Iterable[str]] = None,
    now: Optional[int] = None,
) -> dict:
    """The deployed-generation record: the shape this process produces, and its substrate."""
    return {
        "version": GENERATION_STAMP_VERSION,
        "shape": identity_shape(sample),
        "sample": sample if isinstance(sample, (str, int)) else repr(sample),
        "interpreter": _interpreter_tag(interpreter),
        "interpreter_full": interpreter or platform.python_version(),
        "psutil": bool(_psutil_available() if psutil is None else psutil),
        "witness_symbols": sorted(witness_symbols or []),
        "tree": str(tree) if tree is not None else str(dispatching_tree()),
        "pid": os.getpid(),
        "recorded_at": int(time.time() if now is None else now),
    }


def compare_generation(recorded: Optional[dict], current: dict) -> list[str]:
    """Divergence reasons, in a deterministic order. Empty means the generation is unchanged.

    The comparison is STRUCTURAL. The shape reason fires only when the two fingerprints are not
    comparable (see :func:`shapes_comparable`) -- never when their tokens merely differ, because a
    token differing is not a different shape (``boot-witness/3`` and ``composed/3`` are the same
    three fields under two probes). A lost psutil and a LOST witness symbol alert; a GAINED witness
    symbol does not -- that is the R1-R3 fix landing.

    ``interpreter`` / ``interpreter_full`` are RECORDED and shown in any card body as EVIDENCE, but
    they are never a divergence reason. Measured 2026-11-07: the launchd entry interpreter is a
    supported entry point that self-relaunches into the pinned tool python and writes an IDENTICAL
    shape (same family, same fields, same reference, same psutil, 4/4 witness symbols). Alerting on
    the process that ticked was a false positive: comparability is a property of the WRITE SHAPE,
    not of the interpreter's version tag.
    """
    if not recorded:
        return []
    reasons: list[str] = []
    recorded_shape = str(recorded.get("shape") or IDENTITY_SHAPE_NONE)
    current_shape = str(current.get("shape") or IDENTITY_SHAPE_NONE)
    if not shapes_comparable(recorded_shape, current_shape):
        reasons.append(f"identity-shape-changed:{recorded_shape}->{current_shape}")
    if recorded.get("psutil") and not current.get("psutil"):
        reasons.append("psutil-lost")
    lost = sorted(set(recorded.get("witness_symbols") or []) - set(current.get("witness_symbols") or []))
    if lost:
        reasons.append("witness-bytes-lost:" + ",".join(lost))
    return reasons


def read_generation_stamp(path: Any = None) -> Optional[dict]:
    """The recorded generation, or ``None`` when absent, empty or unreadable (treated as absent)."""
    try:
        raw = stamp_path(path).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def write_generation_stamp(gen: dict, path: Any = None) -> Optional[Path]:
    """Record a generation. Best-effort: an unwritable kanban home must not stop a tick."""
    target = stamp_path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(gen, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, target)
        return target
    except (OSError, ValueError) as exc:
        logger.warning("kanban crash-sweep guard: could not record the identity generation: %s", exc)
        return None


@dataclass(frozen=True)
class GenerationTick:
    """One tick's answer to "is the deployed generation the one we recorded?"."""

    stamp: dict
    reasons: list[str] = field(default_factory=list)
    recorded: bool = False
    reference: Optional[dict] = None

    @property
    def alerting(self) -> bool:
        return bool(self.reasons)

    @property
    def signature(self) -> str:
        return "|".join(self.reasons)


def check_deployed_generation(
    *,
    sample: Any,
    tree: Any = None,
    interpreter: Optional[str] = None,
    psutil: Optional[bool] = None,
    witness_symbols: Optional[Iterable[str]] = None,
    path: Any = None,
    now: Optional[int] = None,
) -> GenerationTick:
    """Record the generation on the first tick; on every later tick compare and report divergence.

    A clean tick REFRESHES the reference, so the record tracks the latest known-good generation and a
    loss is measured against it. A diverging tick LEAVES the reference alone, so the divergence keeps
    being reported every tick until it is fixed -- detected in ONE tick, not discovered hours later by
    a false crash sweep.
    """
    current = generation_stamp(
        sample=sample, tree=tree, interpreter=interpreter, psutil=psutil,
        witness_symbols=witness_symbols, now=now,
    )
    recorded = read_generation_stamp(path)
    if recorded is None:
        write_generation_stamp(current, path)
        return GenerationTick(stamp=current, reasons=[], recorded=True, reference=None)
    reasons = compare_generation(recorded, current)
    if reasons:
        return GenerationTick(stamp=current, reasons=reasons, recorded=False, reference=recorded)
    write_generation_stamp(current, path)
    return GenerationTick(stamp=current, reasons=[], recorded=False, reference=recorded)


def divergence_body(tick: GenerationTick, *, board: Optional[str] = None, tree: Any = None) -> str:
    """The acting card's body for a generation divergence: both generations, side by side.

    The ``sample`` row is marked NOT COMPARED on purpose. Its last field is the process's own
    boot-relative start time, which differs by construction between any two processes, so a
    side-by-side of the raw values reads as a difference that is not one. What IS compared is the
    STRUCTURE (family, field count) and the substrate (psutil, witness symbols); a foreign tick of
    the same tree under a different interpreter that yields the same shape is not a deployment
    change, and it never reaches this card.
    """
    recorded = tick.reference or {}
    current = tick.stamp
    resolved_tree = tree if tree is not None else dispatching_tree()
    lines = [
        "## Observed",
        "",
        "```",
        "identity-generation divergence: " + ", ".join(tick.reasons),
        "```",
        "",
        "Filed by the dispatcher's deployed-identity generation check (R6 of ruling `t_63a20c59`) --",
        "deterministic, no model was consulted. The dispatcher WRITES one identity shape and READS",
        "another, so a liveness comparison across them is meaningless; the reclaim sweep is where that",
        "error becomes a fleet-wide outage.",
        "",
        f"- board: `{board or '(current)'}`",
        f"- tree: `{resolved_tree}`",
        "",
        "| field | recorded | current |",
        "| --- | --- | --- |",
    ]
    for key in ("shape", "interpreter", "interpreter_full", "psutil", "witness_symbols", "tree"):
        lines.append(f"| {key} | `{recorded.get(key)}` | `{current.get(key)}` |")
    # Evidence, NOT a comparison: the sample's last field is the process's own boot-relative
    # start, so two live processes always differ there by construction.
    lines.append(
        f"| sample (not compared) | `{recorded.get('sample')}` | `{current.get('sample')}` |"
    )
    lines += [
        "",
        "- `interpreter` / `interpreter_full` are EVIDENCE, never a divergence reason: this host's",
        "  supported entry interpreter relaunches into the pinned one and writes the same shape.",
        f"- recorded_at: {recorded.get('recorded_at')}",
        f"- observed_at: {current.get('recorded_at')}",
        "",
        "## Operator move",
        "",
        "Land the witness bytes (R1-R3) so the write and the read shape agree, or re-record the",
        "reference deliberately once the new generation is the intended one: remove",
        f"`{stamp_path()}` and let the next tick record it. Deleting the stamp is the",
        "acknowledgement; it does not touch board data.",
    ]
    return "\n".join(lines)


def record_divergence_evidence(conn: Any, tick: GenerationTick) -> None:
    """Append the divergence event to every card this process is running. Never mutates status."""
    from hermes_cli import kanban_db as kb

    payload = {
        "signature": tick.signature,
        "reasons": list(tick.reasons),
        "recorded": {
            k: (tick.reference or {}).get(k)
            for k in ("shape", "interpreter", "psutil", "witness_symbols")
        },
        "current": {
            k: tick.stamp.get(k) for k in ("shape", "interpreter", "psutil", "witness_symbols")
        },
    }
    for task_id in _cards_owned_by_this_process(conn):
        try:
            with kb.write_txn(conn):
                kb._append_event(conn, task_id, GENERATION_DIVERGENCE_EVENT, dict(payload))
        except Exception as exc:
            logger.warning(
                "kanban crash-sweep guard: could not append generation evidence to %s: %s",
                task_id, exc,
            )


def _cards_owned_by_this_process(conn: Any) -> list[str]:
    """Task ids whose recorded worker PID is THIS process -- the cards running on these bytes."""
    try:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE status = 'running' AND worker_pid = ?", (os.getpid(),),
        ).fetchall()
    except Exception:
        return []
    return [row["id"] for row in rows]


def alert_generation_divergence(
    conn: Any,
    tick: GenerationTick,
    *,
    board: Optional[str] = None,
    tree: Any = None,
    now: Optional[int] = None,
) -> Optional[str]:
    """The whole loud path for a divergence: log, per-card evidence, one acting card.

    ``now`` is the caller's clock and is still accepted for it; the filing path does not key on
    time (one card per condition, card t_e217fc62).
    """
    if not tick.alerting:
        return None
    signature = hashlib.sha256(tick.signature.encode("utf-8")).hexdigest()[:12]
    logger.error(
        "kanban crash-sweep guard: DEPLOYED IDENTITY GENERATION CHANGED (%s) -- recorded %s, "
        "current %s. A liveness comparison across the two shapes is meaningless; hold the reclaim.",
        tick.signature, tick.reference, tick.stamp,
    )
    record_divergence_evidence(conn, tick)
    return _file_actor(
        conn,
        prefix=GENERATION_ACTOR_IDEMPOTENCY_PREFIX,
        signature=signature,
        title=GENERATION_ACTOR_TITLE.format(reasons=", ".join(tick.reasons)),
        body=divergence_body(tick, board=board, tree=tree),
        event=GENERATION_ACTOR_EVENT,
        event_payload={
            "reasons": list(tick.reasons),
            "recorded": tick.reference,
            "current": tick.stamp,
        },
        board=board,
    )
