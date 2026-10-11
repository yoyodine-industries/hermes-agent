"""Global emergency stop (ESTOP) — a resumable pause for NEW work only.

``hermes pause`` writes a sentinel at ``$HERMES_HOME/ESTOP``; ``hermes resume``
removes it. While it exists the cron scheduler, kanban dispatcher and new gateway
turns skip work; in-flight work is never killed. The check is one or two uncached
``os.stat`` calls (process home + fleet root when they differ). The body is optional
JSON ``{"reason", "engaged_at", "expires_at", "allow"}``.

An EMPTY sentinel is an ALARM, not a hold: a bare ``touch ~/.hermes/ESTOP`` carries no
attribution, and this module's own registry write is atomic (``os.replace``), so a
zero-byte file can only come from outside it — it is refused, reported loudly once per
engagement, and does NOT pause. Every NON-EMPTY body keeps the fail-SAFE reading: a
corrupt or legacy body we cannot parse still holds, as a total halt.

Arming and lifting are FENCED at the hold itself by :func:`arm_admission`: a delegated
child context and a dispatched kanban worker may neither arm nor lift, whatever surface
they call through (the CLI, the gateway, or a direct ``acquire()``/``release()``). The
venue fence lives HERE, not on one command's argument parsing.

Two fields extend the primitive, both so an unattended hold cannot strand the fleet:

``expires_at``
    A DEADMAN (ISO-8601). Past that instant the sentinel is NOT engaged, so a window
    job that dies between arm and release cannot freeze the fleet forever. The lift is
    logged once, loudly, per engagement, and the dead file is retired (a re-arm starts
    a fresh engagement).
``allow``
    ``{"user_ids": [...], "profiles": [...]}`` — who keeps working THROUGH the pause.
    Identity first: ``user_ids`` is the operator's authenticated id. ``profiles`` is a
    secondary key for a maintenance lane, because a profile name is whatever the
    operator happens to be chatting through at 00:00. Absent allowlist = nobody exempt.

``mode``
    What the pause STOPS. ``estop`` (the default, and the meaning of every pre-mode
    sentinel) is a TOTAL halt: cron, kanban dispatch and new gateway turns all stop.
    ``lockdown`` is a SCOPED halt: the dispatch surfaces keep running and admit only the
    lanes named in ``allow.profiles``, so the lanes an operator needs in order to end an
    incident keep working while everything else is frozen. Admission is keyed on the LANE
    (profile), never on a board — see :func:`work_admitted`.

The sentinel is a REF-COUNTED HOLD REGISTRY, not a boolean, because two unrelated
mechanisms write it: the operator's ``hermes pause`` and an unattended holder (a
yoyoflow critical section's ``enter``/``exit``). A boolean let either one release the
other — an ops-head lull hold expiring at 02:09:52 lifted a DEFCON pause it never owned,
and a `hermes resume` released a maintenance hold in the same stroke. So:

* every hold carries its own ``owner`` (its handle), ``mode``, ``reason`` and optional
  ``expires_at``; the file holds ALL of them;
* the EFFECTIVE state is the union — any live ``estop`` hold ⇒ total halt; only live
  ``lockdown`` holds ⇒ allowlist admission; no live hold ⇒ clear. Per-mode COUNTS are
  the state; owners are attribution;
* :func:`release` releases ONE handle (or one owner's holds) and never another holder's
  entry, and a release naming a stale handle FAILS LOUDLY rather than no-op'ing (a
  workflow that believes it holds a scope another holder's expiry already removed would
  otherwise proceed unscoped);
* an expired hold stops counting for the state and is retired, so a holder that dies
  without releasing cannot pause the fleet forever.

Ported from gastownhall/gastown estop.go (MIT).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

# Same profile-aware / fleet-root resolvers the file-safety guards use (fail-open to ~/.hermes).
from agent.file_safety import _hermes_home_path as _hermes_home, _hermes_root_path as _canonical_root

SENTINEL_NAME = "ESTOP"

# ---- hold registry ---------------------------------------------------------------
# Schema 2 = ``{"schema": 2, "holds": [hold, ...]}``. A body with no ``holds`` key is a
# pre-registry sentinel (``hermes pause`` of the day) and reads as ONE hold: owner
# ``operator``, mode ``estop`` (total), its own reason/expires_at/allow. A ZERO-BYTE file
# is not a body at all — it is an unattributed alarm (``DEFECT_UNATTRIBUTED_EMPTY``) that
# holds nothing.
SCHEMA_VERSION = 3
HOLDS_KEY = "holds"

# Every hold carries BOTH an ``actor`` (the process that armed it, resolved from the
# environment by :func:`actor_handle`) and an ``owner`` (the holder handle a release targets).
# A hold may only wear a name its actor is entitled to; see :func:`arm`.
ACTOR_OPERATOR = "operator"
# The name an entry wears when its provenance cannot be verified. It is not a holder: nothing
# can be released "by owner" against it, which is why the operator's resume clears these.
ACTOR_UNVERIFIED = "unverified"

# Provenance (D2): an HMAC-SHA256 over the hold's canonical fields, keyed by a per-home key
# beside the sentinel. The key is 32 random bytes, never printed, never logged, never in an
# exception message. A hold is verified with the key BESIDE ITS OWN SENTINEL PATH, never "our"
# key: the canonical-root sentinel can carry an entry signed by the root home's key.
KEY_NAME = ".estop-key"
KEY_BYTES = 32
SIG_PREFIX = "hmac-sha256:"

# The append-only ledger (D6). One JSONL line per event, one ``write()`` per line, trimmed to
# the last LEDGER_MAX_EVENTS rows once the file passes LEDGER_MAX_BYTES.
LEDGER_NAME = ".estop-events.jsonl"
LEDGER_MAX_EVENTS = 500
LEDGER_MAX_BYTES = 512 * 1024

# D1/D2 — a run-owned hold is a LEASE. ``expires_at`` is never optional for one, and the
# renewal its owning run performs is the ONLY liveness assertion (a live section renews, a
# dead one cannot), so *expiry equals owner-dead within one lease period*. The default comes
# from the section's declared budget (``--budget``), never from a guess: a bound that is too
# small fails SAFE (the section renews or re-arms), an ABSENT bound fails OPEN — 2026-10-10.
DEFAULT_SECTION_BUDGET_S = 30 * 60
# The environment a run-owned arm takes its owning run from, when the caller names none (D1).
RUN_ID_ENV = ("YOYOFLOW_RUN_ID", "HERMES_UPDATE_RUN_ID")
KANBAN_TASK_ENV = "HERMES_KANBAN_TASK"

# D3 — the independent reaper resolves a hold's owning run against the run ledgers the runner
# ALREADY writes, so no new state is introduced:
#   * the shared yoyoflow run store (``runs.id/status/finished_at``);
#   * the JSONL run ledgers under ``<home>/run_details`` — one object per run carrying
#     ``run``/``id`` and ``status`` — which is where a hermes-update run records itself.
# Absent a row for the owner the LEASE (D2) is the only bound: unresolvable is never terminal.
RUN_STORE_DB = "/opt/hermes_prod/run_details/yoyoflow-state.db"
RUN_LEDGER_DIRNAME = "run_details"
RUN_LEDGER_GLOB = "*.jsonl"
RUN_LEDGER_MAX_FILES = 20
#: Statuses that mean "this run is OVER". A status in neither set is UNKNOWN and is never read
#: as terminal — releasing a hold on a status we do not understand would fail OPEN.
RUN_TERMINAL_STATUSES = frozenset({
    "completed", "complete", "done", "success", "succeeded", "ok", "green",
    "failed", "failure", "error", "errored", "dead", "cancelled", "canceled", "aborted",
    "killed", "terminated", "timeout", "timed_out", "skipped", "orphaned", "expired",
})
RUN_ACTIVE_STATUSES = frozenset({
    "running", "started", "starting", "pending", "queued", "launched", "active",
    "in_progress", "in-progress", "waiting", "held", "draining",
})

# D5 — every release names WHY it happened, so the released scope stays legible after the
# sentinel body (and with it the lane list) is gone.
RELEASE_CAUSES = ("explicit", "handle", "owner", "all", "operator-resume", "deadman", "orphan")

# Environment markers that say "this process is unattended" (D3 step 4). Presence of any of
# them, like a non-TTY stdin, denies the ``operator`` name.
UNATTENDED_ENV_MARKERS = (
    "HERMES_CRON_JOB", "HERMES_CRON_JOB_ID", "HERMES_CRON_RUN",
    "YOYOFLOW_RUN_ID", "YOYOFLOW_WORKFLOW", "YOYOFLOW_NODE",
    "HERMES_UPDATE_WINDOW", "HERMES_UPDATE_RUN_ID",
    "HERMES_ESTOP_UNATTENDED",
)
# Actor handles are printable, bounded and shell-safe (D3 step 3).
_ACTOR_RE = re.compile(r"^[A-Za-z0-9:_.@#/-]{1,120}$")

MODE_ESTOP = "estop"
MODE_LOCKDOWN = "lockdown"
MODES = (MODE_ESTOP, MODE_LOCKDOWN)
DEFAULT_MODE = MODE_ESTOP

# Owner key of the operator's own pause. The LIBRARY default for ``engage()``/``acquire()``
# (back-compat); the CLI resolves its owner from the actor instead (D4/D7).
DEFAULT_OWNER = "operator"

# Defect lines (fail-closed rows): each is loud on purpose — a scoped stop we cannot read is
# never silently a total one, and an empty allowlist is never silently "everyone admitted".
DEFECT_EMPTY_ALLOW = "lockdown names no allow.profiles — every lane is held"
DEFECT_UNKNOWN_MODE = "unrecognised hold mode — read as a total halt"
DEFECT_UNREADABLE = "sentinel body is not usable JSON — read as a total halt, no exemptions"
# The ONE defect that is deliberately NOT fail-closed: a zero-byte sentinel carries no
# attribution, and only this module's atomic registry write creates the file, so a bare
# `touch` is an ALARM the operator must act on — never a hold.
DEFECT_UNATTRIBUTED_EMPTY = (
    "sentinel is an EMPTY file (a bare touch) — unattributed, so NO hold: reported, not armed. "
    "Arm with `hermes pause`."
)
DEFECT_STAT_ERROR = "sentinel could not be stat'ed — read as a total halt (fail safe)"
# D5: an entry whose provenance cannot be verified is DEMOTED, never dropped. It holds (mode
# estop, TOTAL), it may not choose which lanes keep running, and it is attributable to nobody.
DEFECT_UNVERIFIED = (
    "hold carries no valid provenance (armed by hand, by another home, or by pre-landing "
    "code) — read as a TOTAL halt, not an operator hold"
)

logger = logging.getLogger(__name__)

# Per-component "logged already for this engagement" flags: log once per engagement, not per tick.
_log_lock = threading.Lock()
_logged_components: set[str] = set()
# Components already told that they are running SCOPED under a lockdown, so the scoped
# notice is one line per engagement too (the tick itself keeps running).
_lockdown_logged: set[str] = set()
# Sentinel path -> engagement key (mtime_ns:size) of the last logged EXPIRY, so the
# deadman's lift is one loud line per engagement rather than one per check.
_expired_logged: dict[str, str] = {}
# Sentinel path -> engagement key (mtime_ns:size) of the last REPORTED empty (unattributed)
# sentinel, so the alarm is one loud line per engagement rather than one per check.
_unattributed_logged: dict[str, str] = {}
# Handle -> engagement of the last REPORTED run-owned hold with no owner binding (D3), so the
# reaper's "I cannot resolve this owner" line is one per engagement, not one per tick.
_unbound_logged: dict[str, str] = {}
# Per-component snapshot of the hold that PAUSED it, kept while the pause is logged and read
# back on the resume transition, so the tick can log "dispatch resumed …" with the hold that
# ended and how it ended. "dispatch paused" had no counterpart before this (card t_f88ddb45).
_last_holds: dict[str, dict] = {}

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$")
_DURATION_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value: Any) -> Optional[int]:
    """Seconds for ``45m`` / ``90m`` / ``2h`` / ``90`` / ``90``-as-int; None when unusable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else None
    match = _DURATION_RE.match(str(value))
    if not match:
        return None
    seconds = int(match.group(1)) * _DURATION_UNITS[match.group(2)]
    return seconds or None


def sentinel_path() -> Path:
    """Path of the ESTOP sentinel this process would write on `hermes pause`."""
    return _hermes_home() / SENTINEL_NAME


def _candidate_sentinel_paths() -> list:
    """Profile home first, then the fleet root if it is a different directory: a profile
    gateway (HERMES_HOME=~/.hermes/profiles/<n>) must still honor an operator's ~/.hermes/ESTOP."""
    primary = sentinel_path()
    try:
        root = _canonical_root() / SENTINEL_NAME
    except Exception:
        return [primary]
    try:
        distinct = root.resolve() != primary.resolve()
    except Exception:
        # Non-Path test doubles fail .resolve(); plain equality still dedupes.
        distinct = root != primary
    return [primary, root] if distinct else [primary]


def _read_payload(path: Path) -> Optional[dict]:
    """Parsed sentinel body, or None when absent/unreadable/not a JSON object.

    None means "no usable body", which every caller must treat as ENGAGED (fail safe) —
    it is never a reason to lift the pause.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, AttributeError):
        return None
    return raw if isinstance(raw, dict) else None


def _parse_stamp(value: Any) -> Optional[datetime]:
    """Aware datetime for an ISO-8601 stamp; None when absent or unparsable (a naive
    stamp is read as UTC). Unparsable NEVER means expired — the pause stays held."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _normalize_allow(allow: Any) -> dict:
    """``{"user_ids": [...], "profiles": [...]}`` with string values and no empties."""
    if not isinstance(allow, dict):
        return {}
    normalized: dict = {}
    for key in ("user_ids", "profiles"):
        value = allow.get(key)
        if isinstance(value, (str, int, float)):
            value = [value]
        entries = [str(item).strip() for item in value or [] if str(item).strip()]
        if entries:
            normalized[key] = entries
    return normalized


def _resolve_expiry(value: Any) -> Optional[datetime]:
    """Absolute expiry from an ISO-8601 string or an aware datetime; None otherwise."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return _parse_stamp(value)


def _engagement_key(path: Path) -> str:
    """Identity of the CURRENT engagement on disk: a re-engage rewrites the file and so
    earns a fresh key (and a fresh loud line)."""
    try:
        stat = path.stat()
        return f"{stat.st_mtime_ns}:{stat.st_size}"
    except (OSError, AttributeError, TypeError, ValueError):
        return "unknown"


# ---------------------------------------------------------------------------------
# Hold registry: read the effective state, acquire a hold, release a hold.
# ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class EstopState:
    """The EFFECTIVE state: the union of every live hold on the candidate sentinels.

    ``holds`` are live only (expired holds in ``expired``, kept for the release/refusal
    message). ``mode`` is derived from the per-mode COUNTS, never from one chosen hold:
    any live ``estop`` hold means a total halt, so one operator pause among a maintenance
    section's holds cannot be read as "scoped".
    """

    holds: tuple = ()
    expired: tuple = ()
    defect: Optional[str] = None

    @property
    def engaged(self) -> bool:
        return bool(self.holds)

    @property
    def counts(self) -> dict:
        counts = {MODE_ESTOP: 0, MODE_LOCKDOWN: 0}
        for hold in self.holds:
            counts[hold["mode"]] = counts.get(hold["mode"], 0) + 1
        return counts

    @property
    def total(self) -> bool:
        return self.counts[MODE_ESTOP] > 0

    @property
    def mode(self) -> Optional[str]:
        if not self.holds:
            return None
        return MODE_ESTOP if self.total else MODE_LOCKDOWN

    @property
    def allow_profiles(self) -> frozenset:
        """Lanes admitted for WORK: the union over live holds (used only under lockdown)."""
        return frozenset(
            profile
            for hold in self.holds
            for profile in hold["allow"].get("profiles", [])
        )

    @property
    def allow_user_ids(self) -> frozenset:
        """Authenticated ids still SERVED through the pause (never admits work)."""
        return frozenset(
            user_id for hold in self.holds for user_id in hold["allow"].get("user_ids", [])
        )

    @property
    def owners(self) -> list:
        return [hold["owner"] for hold in self.holds]

    def as_dict(self) -> dict:
        """Machine read of the effective state (``hermes estop state --json`` shape)."""
        return {
            "engaged": self.engaged,
            "mode": self.mode,
            "counts": self.counts,
            "holds": [dict(hold) for hold in self.holds],
            "expired": [dict(hold) for hold in self.expired],
            "allow": {
                "profiles": sorted(self.allow_profiles),
                "user_ids": sorted(self.allow_user_ids),
            },
            "defect": self.defect,
            "sentinel": str(sentinel_path()),
        }

    def describe(self) -> str:
        """One line for a tick's suppression line / `hermes status` / a log message."""
        counts = self.counts
        if not self.engaged:
            return "clear"
        if self.total:
            owners = ", ".join(self.owners)
            return f"{MODE_ESTOP} x{counts[MODE_ESTOP]} (total, no exemptions) [{owners}]"
        lanes = ", ".join(sorted(self.allow_profiles)) or "(none)"
        return (
            f"{MODE_LOCKDOWN} x{counts[MODE_LOCKDOWN]} — lanes admitted: {lanes}"
            f"; held: every other lane [{', '.join(self.owners)}]"
        )


@dataclass(frozen=True)
class ReleaseResult:
    """Outcome of :func:`release`. ``stale`` is the loud path: a handle that names no live
    hold (unknown, already released, or expired) must be told so, never silently ignored.

    ``forbidden`` is the D7 path: the caller is admitted, but what it asked for is not its to
    lift (the operator's hold, or ``--all``). ``handles`` names exactly what went away, so a
    caller can report it instead of guessing; ``unverified_cleared`` counts the unattributable
    holds the operator's own resume cleared; ``ledger`` is False when the release could NOT be
    recorded (D8's gate reads those records, so it must never fail silently).
    """

    released: bool
    stale: bool = False
    cleared: bool = False
    message: str = ""
    remaining: tuple = ()
    forbidden: bool = False
    handles: tuple = ()
    actor: str = ""
    unverified_cleared: int = 0
    ledger: bool = True
    #: D5 — WHY this release happened, from :data:`RELEASE_CAUSES` (None when nothing was
    #: released). The released scope has to stay readable after the sentinel body is gone.
    cause: Optional[str] = None

    @property
    def remaining_owners(self) -> list:
        return [hold["owner"] for hold in self.remaining]


@dataclass(frozen=True)
class RenewResult:
    """Outcome of :func:`renew` — the liveness proof of a run-owned hold (D2).

    ``stale`` is the loud path, exactly as in :func:`release`: a handle that names no live hold
    must be TOLD, never silently renewed into the void (a run that believes it renewed a lease
    it does not hold would let its section run unbounded). ``expires_at`` is the deadline the
    hold now carries, ``ttl_s`` the lease it was set to, and ``ledger`` False when the
    ``renew`` row could not be written.
    """

    handle: str
    renewed: bool = False
    stale: bool = False
    message: str = ""
    expires_at: Optional[str] = None
    ttl_s: Optional[int] = None
    owner_run: Optional[str] = None
    actor: str = ""
    ledger: bool = True


@dataclass(frozen=True)
class ArmResult:
    """What one acquisition actually LANDED (D1/D4/D8) — the facts a CLI reports.

    ``renamed`` is true when the body's claim was not the actor's to wear and the hold was
    recorded under the actor's own name instead; ``claimed_owner`` then carries the claim it
    refused. ``after_resume`` is the timestamp of the operator resume this arm overrode (D8),
    or None. ``ledger`` is False when the arm could not be recorded in the ledger.
    """

    handle: str
    owner: str
    actor: str
    mode: str
    path: str
    order: Optional[str] = None
    claimed_owner: Optional[str] = None
    renamed: bool = False
    after_resume: Optional[str] = None
    verified: bool = True
    ttl_s: Optional[int] = None
    ledger: bool = True
    #: D1 — the run this hold is bound to (None only for a hold that names no run at all,
    #: which is now the operator's own arm and nothing else).
    owner_run: Optional[str] = None
    #: True when the deadline came from the section budget rather than the caller's ``--ttl``.
    lease_default: bool = False


class EstopWriteError(OSError):
    """A hold could not be recorded. Raised rather than swallowed: a pause the operator
    believes is armed but that never reached the disk is the worst failure this module has."""


class EstopRefusal(EstopWriteError):
    """This caller is not an admitted arm/lift venue (cooperative fence)."""


class EstopOrderRequired(EstopWriteError):
    """A third-party arm after the operator's resume that carries no recorded order (D8).

    Distinct from ``EstopRefusal`` because the remedy differs: the venue is admitted, what is
    missing is the operator's authority. ``message`` is the three-part refusal from
    :func:`order_refusal`, and the caller MUST treat the pause as NOT armed."""


class EstopUnboundRun(EstopWriteError):
    """A run-owned arm that cannot name its owning run (D1) — sibling of
    :class:`EstopOrderRequired`.

    The venue is admitted and the moment may well be right to stop; what is missing is the
    binding that lets the hold be resolved — and released — without the owning run's own code
    ever running again. The wrong moment to STOP is never refused; the wrong moment to stop
    *unboundedly and anonymously* is: an unattributed hold is exactly the one that outlived
    its run on 2026-10-10 and had to be lifted by hand. Nothing is written to the sentinel."""


def _normalize_owner(owner: Any) -> str:
    text = str(owner).strip() if owner is not None else ""
    return " ".join(text.split()) or DEFAULT_OWNER


def _normalize_mode(value: Any) -> tuple:
    """``(mode, unknown)`` — anything that is not ``lockdown`` reads as a TOTAL halt.

    Fail SAFE, never fail open: an absent, misspelled or non-string mode must freeze the
    fleet the way a bare ``hermes pause`` does, never arm a scoped stop by accident.
    """
    if isinstance(value, str) and value.strip().lower() == MODE_LOCKDOWN:
        return MODE_LOCKDOWN, False
    if value is None or (isinstance(value, str) and value.strip().lower() == MODE_ESTOP):
        return MODE_ESTOP, False
    return MODE_ESTOP, True


def _handle_for(owner: str, engaged_at: datetime) -> str:
    """One acquisition's id: ``owner#<epoch-micros>``. Distinct per acquisition (so the
    same owner twice is two holds), printable so ``hermes resume --handle`` can name it."""
    return f"{owner}#{int(engaged_at.timestamp() * 1_000_000)}"


# ---------------------------------------------------------------------------------
# Attribution (D1/D3), provenance (D2), the ledger (D6) and the order gate (D8).
# ---------------------------------------------------------------------------------


def _stdin_is_tty() -> bool:
    """True only for an interactive terminal; anything else is an unattended caller."""
    try:
        return bool(sys.stdin) and bool(sys.stdin.isatty())
    except Exception:
        return False


def _unattended_markers() -> list:
    """The unattended environment markers present in this process (D3 step 4)."""
    return [name for name in UNATTENDED_ENV_MARKERS if str(os.environ.get(name) or "").strip()]


_BOOT_ID: Optional[str] = None


def _read_boot_id() -> str:
    """This boot's id (D6). A machine fact, read once per process and never guessed."""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(
                ["sysctl", "-n", "kern.bootsessionuuid"],
                capture_output=True, text=True, timeout=5,
            )
            value = str(out.stdout or "").strip()
        else:
            value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except Exception:
        return "unknown"
    return value if value and _ACTOR_RE.match(value) else "unknown"


def _boot_id() -> str:
    """Cached :func:`_read_boot_id` — a boot id cannot change while we run."""
    global _BOOT_ID
    if _BOOT_ID is None:
        _BOOT_ID = _read_boot_id()
    return _BOOT_ID


def actor_handle() -> str:
    """The handle of the process arming or lifting a hold (D3), first match wins:

    1. the worker of a dispatched kanban card -> ``card:<task_id>``;
    2. a delegated child -> ``delegated:<parent>``;
    3. ``HERMES_ESTOP_ACTOR`` when it is charset-valid and is not ``operator``;
    4. an unattended caller (a marker present, or stdin is not a TTY) -> ``bot:<profile>``;
    5. otherwise -> ``operator``.

    ``HERMES_ESTOP_ACTOR=operator`` is honoured ONLY when the worker/delegated tests and the
    marker test are all clear AND stdin is a TTY: the operator's name is reachable from an
    interactive operator session, never as a default. The claim is LOCAL and
    environment-derived (same-uid code can forge it — the stated trust boundary); what it buys
    is that no unattended path wears the operator's name by accident.
    """
    task = ""
    delegated = ""
    try:
        from agent.delegation_context import (
            DELEGATED_CHILD_ENV_MARKER, is_delegated_child_process_context, owned_kanban_task,
        )

        task = owned_kanban_task()
        if not task and is_delegated_child_process_context():
            marker = str(os.environ.get(DELEGATED_CHILD_ENV_MARKER) or "").strip()
            delegated = marker if marker and marker != "1" else "unknown"
    except Exception:  # never block a pause on an import probe
        task, delegated = "", ""
    if task:
        return f"card:{task}"
    if delegated:
        return f"delegated:{delegated}"
    claimed = str(os.environ.get("HERMES_ESTOP_ACTOR") or "").strip()
    if claimed and not _ACTOR_RE.match(claimed):
        claimed = ""
    if claimed and claimed != ACTOR_OPERATOR:
        return claimed
    if claimed == ACTOR_OPERATOR and not _unattended_markers() and _stdin_is_tty():
        return ACTOR_OPERATOR
    if _unattended_markers() or not _stdin_is_tty():
        # D6 — an unattended caller with no resolvable profile is NAMED, never muddled into
        # ``bot:unknown``: a hold has to be liftable by whoever can read its handle off the
        # sentinel, and an unnameable one had to be cleared by hand (2026-10-10, twice).
        profile = str(os.environ.get("HERMES_PROFILE") or "").strip()
        if profile:
            return f"bot:{profile}"
        return f"bot:unresolved:{os.getpid()}@{_boot_id()}"
    return ACTOR_OPERATOR


def _actor_run(actor: str) -> Optional[str]:
    """The run an actor handle ALREADY names, when it names one (D1).

    ``card:<task>``, ``delegated:<parent>`` and ``bot:<lane>:<run>`` (the update's own handle
    shape) are run-scoped: the handle is its own owner binding, so such a hold is resolvable
    without any caller having to pass one. ``bot:<profile>`` names a LANE, not a run, and
    ``bot:unresolved:<pid>@<boot>`` names a PROCESS — neither is a run. A run-shaped handle
    with an EMPTY run segment (``bot:hermes-update:``, the shape a caller produces when its
    run-id variable is unset) resolves NOTHING, which is exactly the arm D1 refuses.
    """
    text = str(actor or "").strip()
    if text.startswith(("card:", "delegated:")):
        return text
    parts = text.split(":")
    if _is_run_scoped(text) and len(parts) >= 3 and parts[2].strip():
        return text
    return None


def _clean_run_id(value: Any) -> Optional[str]:
    """A printable, bounded run id, or None when there is nothing usable to bind to."""
    text = str(value).strip() if value is not None else ""
    return text if text and _ACTOR_RE.match(text) else None


def owner_run_for(actor: str, owner_run: Any = None) -> Optional[str]:
    """The owning run a hold is bound to (D1): the caller's value, else the run id in the
    environment, else the run the ACTOR handle itself names.

    ``None`` means "this arm cannot name the run it belongs to", which is a refusal for a
    run-scoped arm (see :func:`arm`) and is why nothing here ever guesses: a binding that does
    not name the real owner would release the WRONG hold.
    """
    for candidate in (owner_run, *(os.environ.get(name) for name in RUN_ID_ENV),
                      os.environ.get(KANBAN_TASK_ENV)):
        resolved = _clean_run_id(candidate)
        if resolved:
            return resolved
    return _actor_run(actor)


def _is_run_scoped(actor: str) -> bool:
    """True when the arm belongs to a RUN rather than to a lane or the operator itself (D1).

    A run-scoped arm must carry its ``owner_run``: it is the arm that will be released by a
    process that may never run again, so an unbound one is refused loudly rather than landing
    as tonight's did. ``bot:hermes-update:`` (a run-shaped handle whose run segment is empty)
    is run-scoped and resolves nothing — the refusal case D1 exists for.
    """
    text = str(actor or "").strip()
    if text.startswith(("card:", "delegated:")):
        return True
    return (text.startswith("bot:") and not text.startswith("bot:unresolved:")
            and len(text.split(":")) >= 3)


def key_path(sentinel: Optional[Path] = None) -> Path:
    """The provenance key beside a sentinel (D2): ``<home>/.estop-key``."""
    return Path(sentinel if sentinel is not None else sentinel_path()).parent / KEY_NAME


def _load_key(sentinel: Path, *, create: bool = False) -> Optional[bytes]:
    """The provenance key beside ``sentinel``; None when absent (and not to be created) or
    when it cannot be created/read.

    The key is never printed, never logged and never placed in an exception message. A key
    that cannot be created does NOT refuse the arm: the hold lands unsigned with a loud
    warning, because a provenance failure must never stop the fleet being parked.
    """
    path = key_path(sentinel)
    try:
        data = path.read_bytes()
        if data:
            return data
    except OSError:
        pass
    if not create:
        return None
    fd = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            return path.read_bytes() or None
        except OSError:
            return None
    except OSError:
        logger.warning(
            "ESTOP: no provenance key could be created at %s — holds armed here are UNSIGNED "
            "and will read as 'unverified'", path,
        )
        return None
    try:
        data = secrets.token_bytes(KEY_BYTES)
        os.write(fd, data)
        return data
    except OSError:
        return None
    finally:
        if fd is not None:
            with suppress(OSError):
                os.close(fd)


def _canonical(handle: Any, owner: Any, actor: Any, mode: Any, reason: Any,
               engaged_at: Any, expires_at: Any, allow: Any, owner_run: Any = None) -> str:
    """The exact text the D2 signature covers. Editing ANY signed field invalidates that
    entry, and only that entry. ``owner_run`` (D1) is signed because it is what the reaper
    resolves ownership against: an unsigned binding could be rewritten to release a hold."""
    return "\n".join([
        "estop", str(SCHEMA_VERSION), str(handle or ""), str(owner or ""), str(actor or ""),
        str(mode or ""), str(reason or ""), str(engaged_at or ""), str(expires_at or ""),
        str(owner_run or ""),
        json.dumps(_normalize_allow(allow), sort_keys=True, separators=(",", ":")),
    ])


def _canonical_fields(source: dict) -> dict:
    """The signed subset of an entry, normalised identically on the write and the read path,
    so an entry this module wrote re-verifies byte-for-byte."""
    return {
        "handle": str(source.get("handle") or "").strip(),
        "owner": _normalize_owner(source.get("owner")),
        "actor": str(source.get("actor") or "").strip(),
        "mode": _normalize_mode(source.get("mode"))[0],
        "reason": source.get("reason") or "",
        "engaged_at": source.get("engaged_at") or "",
        "expires_at": source.get("expires_at") or "",
        "allow": source.get("allow"),
        "owner_run": source.get("owner_run") or "",
    }


def _load_key_verified(source: dict, sentinel: Path) -> bool:
    """True when ``source`` carries a signature that verifies under the key BESIDE ITS OWN
    sentinel path (never "our" key — see D2)."""
    sig = source.get("sig")
    if not isinstance(sig, str) or not sig.startswith(SIG_PREFIX):
        return False
    try:
        key = _load_key(sentinel)
    except Exception:
        # A sentinel path that cannot even be introspected has no key beside it: unverified.
        return False
    if not key:
        return False
    expected = hmac.new(
        key, _canonical(**_canonical_fields(source)).encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(sig[len(SIG_PREFIX):], expected)


def _sign_fields(fields: dict, sentinel: Path) -> Optional[str]:
    """The signature for a hold about to be written to ``sentinel``, or None (unsigned) when
    the key cannot be created."""
    key = _load_key(sentinel, create=True)
    if not key:
        return None
    digest = hmac.new(key, _canonical(**fields).encode("utf-8"), hashlib.sha256).hexdigest()
    return SIG_PREFIX + digest


def _content_handle(source: dict) -> str:
    """``unverified#<sha256(canonical body)[:12]>`` (D5): derived from the BODY, so a
    byte-identical rewrite keeps the same handle and ``resume --handle`` can name it (the
    mtime-keyed ``legacy-<...>`` handle could not)."""
    body = json.dumps({
        "owner": _normalize_owner(source.get("owner")),
        "actor": str(source.get("actor") or "").strip() or None,
        "mode": _normalize_mode(source.get("mode"))[0],
        "reason": source.get("reason") or None,
        "engaged_at": source.get("engaged_at") or None,
        "expires_at": source.get("expires_at") or None,
        "allow": _normalize_allow(source.get("allow")),
    }, sort_keys=True, separators=(",", ":"))
    return f"{ACTOR_UNVERIFIED}#{hashlib.sha256(body.encode('utf-8')).hexdigest()[:12]}"


def ledger_path(sentinel: Optional[Path] = None) -> Path:
    """The append-only ledger BESIDE a sentinel (D6): ``<home>/.estop-events.jsonl``."""
    return Path(sentinel if sentinel is not None else sentinel_path()).parent / LEDGER_NAME


def _ledger_files() -> list:
    """Every candidate sentinel's ledger, so a profile process still sees the canonical root's
    history (the operator's resume lives there) and vice versa."""
    seen, out = set(), []
    for candidate in _candidate_sentinel_paths():
        path = ledger_path(Path(candidate))
        if str(path) not in seen:
            seen.add(str(path))
            out.append(path)
    return out


def _trim_ledger(path: Path) -> None:
    """Keep the last LEDGER_MAX_EVENTS rows once the file passes LEDGER_MAX_BYTES (D6)."""
    try:
        if path.stat().st_size <= LEDGER_MAX_BYTES:
            return
        rows = path.read_text(encoding="utf-8", errors="replace").splitlines()[-LEDGER_MAX_EVENTS:]
        tmp = path.with_name(path.name + ".trim")
        tmp.write_text("\n".join(rows) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def engage(reason: Optional[str] = None) -> Path:
    """Create the ESTOP sentinel. Idempotent; re-engaging updates the file."""
    path = sentinel_path()
    payload = {"engaged_at": datetime.now(UTC).isoformat(), "reason": reason or None}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError:
        with suppress(OSError):  # Best effort: an empty/partial sentinel still pauses (fail safe).
            path.touch(exist_ok=True)
    return path


def record_event(event: str, *, sentinel: Optional[Path] = None, **fields) -> bool:
    """Append ONE ledger row: one ``write()`` of one line, O_APPEND (D6).

    BEST-EFFORT by design: a ledger failure is logged and reported (False), and never blocks a
    hold or a release.
    """
    path = ledger_path(sentinel)
    row = {"at": datetime.now(UTC).isoformat(), "event": event}
    row.update({key: value for key, value in fields.items() if value is not None})
    line = (json.dumps(row, sort_keys=True) + "\n").encode("utf-8")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError:
        logger.warning(
            "ESTOP: the ledger at %s could not be written — event '%s' is NOT recorded",
            path, event,
        )
        return False
    _trim_ledger(path)
    return True
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError:
        logger.warning(
            "ESTOP: the ledger at %s could not be written — event '%s' is NOT recorded",
            path, event,
        )
        return False
    _trim_ledger(path)
    return True


def read_events(limit: Optional[int] = None) -> list:
    """Every ledger row across the candidate sentinels, oldest first (D6)."""
    events = []
    for path in _ledger_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("event"):
                events.append(row)
    events.sort(key=lambda row: str(row.get("at") or ""))
    return events[-limit:] if limit else events


ORDER_ENV = "HERMES_ESTOP_ORDER"


def _last_operator_release(events: list) -> Optional[dict]:
    """The newest ``release`` event by the operator (or one that released an operator hold)."""
    for row in reversed(events):
        if row.get("event") != "release":
            continue
        if str(row.get("actor") or "") == ACTOR_OPERATOR:
            return row
        if str(row.get("owner") or "") == ACTOR_OPERATOR:
            return row
    return None


def _armed_since(events: list, resume_at: str) -> Optional[dict]:
    """The newest ``arm*`` event NEWER than ``resume_at``, or None."""
    for row in reversed(events):
        if str(row.get("at") or "") <= resume_at:
            return None
        if str(row.get("event") or "").startswith("arm"):
            return row
    return None


def order_gate(actor: str, order: Optional[str], events: Optional[list] = None) -> tuple:
    """``(proceed, kind, resume_at)`` for an arm by ``actor`` — D8, keyed on the OPERATOR's
    resume only.

    ``kind``: ``clear`` (no operator resume on record, or a hold has been armed since it — the
    fleet is parked, not resuming), ``operator`` (the act IS the order), ``ordered`` (a
    third-party re-arm carrying a recorded order) or ``refused`` (a third-party re-arm after an
    operator resume that carries none).
    """
    rows = read_events() if events is None else events
    resume = _last_operator_release(rows)
    if resume is None:
        return True, "clear", ""
    resume_at = str(resume.get("at") or "")
    if _armed_since(rows, resume_at) is not None:
        return True, "clear", resume_at
    if actor == ACTOR_OPERATOR:
        return True, "operator", resume_at
    if order:
        return True, "ordered", resume_at
    return False, "refused", resume_at


def order_refusal(actor: str, resume_at: str) -> str:
    """The D8 refusal, naming the resume, the actor and the two ways forward."""
    return (
        f"{actor} carries no order for a re-arm after the operator's resume at {resume_at}.\n"
        f"   An arm here would resurrect a stop the operator lifted, silently. Two ways forward:\n"
        f"   (1) carry the authority: `hermes pause --reason \"...\" --order \"<who ordered it>\"` "
        f"(or HERMES_ESTOP_ORDER=<authority>);\n"
        f"   (2) have the operator run the pause — their own arm IS the order."
    )


def unbound_run_refusal(actor: str) -> str:
    """The D1 refusal: a run-scoped arm that cannot name its run is not recorded at all.

    Names the three ways to bind, because the remedy is a one-line fix in the caller and the
    alternative is tonight's hold: armed, unbounded in fact, and unattributable to any run the
    fleet could resolve.
    """
    return (
        f"{actor} names a run but resolves no run id — refusing to arm an unbound hold.\n"
        f"   A hold a run owns is released by the INDEPENDENT reaper, which resolves the run in\n"
        f"   its ledger; a hold with no run to resolve is the one that stranded the fleet on\n"
        f"   2026-10-10 (armed 04:31, never released, ~2h of fleet-wide pause). Two ways forward:\n"
        f"   (1) arm from the owning run so the binding resolves itself: YOYOFLOW_RUN_ID, "
        f"HERMES_UPDATE_RUN_ID, or the run segment of the venue handle (`bot:<lane>:<run>`);\n"
        f"   (2) pass it explicitly: `arm(..., owner_run=\"<run-id>\", budget=\"30m\")`."
    )


def _hold_from_entry(entry: Any, *, path: Path) -> dict:
    """One normalised hold from a registry entry (or from a pre-registry whole body).

    D1: an entry carries ``actor`` (the process that wrote it) AND ``owner`` (the holder a
    release targets). D5: an entry whose provenance cannot be verified is DEMOTED, never
    dropped — owner ``unverified``, mode ``estop`` (a TOTAL halt), ``allow`` IGNORED (an
    unauthenticated body may not choose which lanes keep running), ``verified`` False,
    ``claimed_owner`` whatever the body claimed, and the ``DEFECT_UNVERIFIED`` line. Engagement
    survives in every case: a hold we cannot attribute still holds the fleet, as a total halt.

    Verification uses the key BESIDE THIS SENTINEL (D2), so a canonical-root hold stays
    verifiable from a profile home (and, verified from a home whose key differs, reads
    unverified — which still holds).
    """
    payload = entry if isinstance(entry, dict) else {}
    verified = _load_key_verified(payload, path)
    body_owner = _normalize_owner(payload.get("owner"))
    actor = str(payload.get("actor") or "").strip() or None
    if verified:
        mode, unknown = _normalize_mode(payload.get("mode"))
        owner = body_owner
        allow = _normalize_allow(payload.get("allow"))
        handle = str(payload.get("handle") or "").strip() or _content_handle(payload)
        # D4: ``claimed_owner`` records the name an actor was NOT entitled to wear. A hold that
        # was legitimately recorded under its owner's own name has no refused claim.
        claimed_owner = payload.get("claimed_owner") or None
    else:
        mode, unknown, owner, allow = MODE_ESTOP, False, ACTOR_UNVERIFIED, {}
        handle = _content_handle(payload)
        # D5: the demoted entry keeps what the BODY claimed, which is exactly the attribution
        # question an operator needs answered when they find it.
        claimed_owner = body_owner
    engaged_at = payload.get("engaged_at") or None
    expires_at = payload.get("expires_at") or None
    sig = payload.get("sig")
    return {
        "handle": handle,
        "owner": owner,
        "actor": actor,
        "verified": verified,
        "claimed_owner": claimed_owner,
        "mode": mode,
        "unknown_mode": unknown,
        "reason": payload.get("reason") or None,
        "engaged_at": engaged_at,
        "expires_at": expires_at,
        "allow": allow,
        "expired": _is_past(expires_at),
        "path": str(path),
        "venue": payload.get("venue") or None,
        # D1: the run this hold belongs to — the field the INDEPENDENT reaper resolves. Carried
        # even on a demoted entry: what the body claimed is exactly what an operator needs.
        "owner_run": str(payload.get("owner_run") or "").strip() or None,
        # D2: the last renewal, so a reader can see HOW stale the liveness proof is.
        "renewed_at": payload.get("renewed_at") or None,
        # Carried verbatim so a release that rewrites the file around a KEPT co-holder
        # preserves that holder's provenance instead of re-signing it with our key.
        "sig": sig if isinstance(sig, str) else None,
    }


def _read_holds(path: Path) -> tuple:
    """``(holds, defect)`` for one sentinel path. ``defect`` is the fail-SAFE reason a present
    sentinel could not be read as a fully-authenticated body — a ``stat`` error, an
    unreadable/corrupt body, an entry that is not an object, or an entry with no valid
    provenance. All of them still yield a hold (mode ``estop``, TOTAL): a state we cannot read
    is never a licence to run, only a recorded defect.

    The one exception is a ZERO-BYTE sentinel: it carries no attribution, so it yields NO
    hold and ``DEFECT_UNATTRIBUTED_EMPTY`` — an alarm, reported, not a pause.
    """
    try:
        present = path.exists()
    except (OSError, AttributeError):
        # `os.stat` raised: engaged, with no body to attribute the hold to.
        return [_hold_from_entry({}, path=path)], DEFECT_STAT_ERROR
    if not present:
        return [], None
    try:
        if path.stat().st_size == 0:
            # A bare ``touch``: NO attribution, and only this module's atomic registry write
            # (``os.replace``) creates this file, so a zero-byte one came from outside it.
            # An alarm, NOT a hold — the caller reports it once per engagement (see
            # ``_report_unattributed``) and leaves the file for the operator to see.
            return [], DEFECT_UNATTRIBUTED_EMPTY
    except (OSError, AttributeError):
        pass  # no size to read: fall through to today's fail-safe body read below
    payload = _read_payload(path)
    if payload is None:
        # Corrupt / non-empty unreadable body: no provenance to verify, so it reads as a
        # TOTAL halt — exactly the pre-provenance meaning — plus the defect line.
        return [_hold_from_entry({}, path=path)], DEFECT_UNREADABLE
    entries = payload.get(HOLDS_KEY)
    if entries is None:
        # Pre-registry whole body: ONE hold, and unverified unless it carries a signature.
        holds = [_hold_from_entry(payload, path=path)]
    elif not isinstance(entries, list):
        return [_hold_from_entry({}, path=path)], DEFECT_UNREADABLE
    else:
        holds = [_hold_from_entry(entry, path=path) for entry in entries if isinstance(entry, dict)]
        if not holds and entries:
            return [_hold_from_entry({}, path=path)], DEFECT_UNREADABLE
    return holds, (DEFECT_UNVERIFIED if any(not hold["verified"] for hold in holds) else None)


def _report_unattributed(path: Path) -> None:
    """Say ONCE per engagement that a zero-byte sentinel is not a pause.

    Loud and repeated-across-engagements on purpose: every human who sees ``~/.hermes/ESTOP``
    reads it as a halt, so an unattributed EMPTY file has to be visibly refused rather than
    silently honoured. Deduped on the module's existing (path, engagement key) pattern, so
    the alarm is one line per engagement instead of one per gate check.
    """
    key = _engagement_key(path)
    with _log_lock:
        first_report = _unattributed_logged.get(str(path)) != key
        _unattributed_logged[str(path)] = key
    if first_report:
        logger.warning(
            "ESTOP: unattributed EMPTY sentinel at %s is NOT a hold — no pause is in force. %s",
            path, DEFECT_UNATTRIBUTED_EMPTY,
        )


def _is_past(value: Any, now: Optional[datetime] = None) -> bool:
    """True only for a PARSABLE stamp in the past — an unparsable expiry never lifts a hold."""
    stamp = _parse_stamp(value)
    if stamp is None:
        return False
    return (now or datetime.now(UTC)) >= stamp


def read_state(now: Optional[datetime] = None) -> EstopState:
    """Read every candidate sentinel ONCE and return the union of live holds.

    Callers that gate many items in one tick (a dispatch tick, a cron tick) must read the
    state here and pass it down, so one tick can never see two different modes.
    """
    live: list = []
    expired: list = []
    defect: Optional[str] = None
    for path in _candidate_sentinel_paths():
        holds, path_defect = _read_holds(path)
        if path_defect:
            if path_defect == DEFECT_UNATTRIBUTED_EMPTY:
                _report_unattributed(path)
            defect = defect or path_defect
        for hold in holds:
            if hold["unknown_mode"]:
                defect = defect or DEFECT_UNKNOWN_MODE
            if hold["expired"]:
                expired.append(hold)
            else:
                live.append(hold)
    state = EstopState(holds=tuple(live), expired=tuple(expired), defect=defect)
    if live and not state.total and not state.allow_profiles:
        # Row E: a lockdown that names no lane holds EVERYONE — and says so, loudly.
        # (The arming verb refuses to write this state; this is the belt-and-braces read.)
        state = EstopState(
            holds=state.holds, expired=state.expired,
            defect=state.defect or DEFECT_EMPTY_ALLOW,
        )
    return state


def engagement_key() -> str:
    """Identity of the CURRENT engagement on disk: ``mtime_ns:size`` of the home sentinel.

    A re-arm rewrites the file and so earns a fresh key. Consumers that must record a
    policy hold ONCE per engagement (the dispatcher's starvation event, a cron deferral
    line) dedupe on this, so a card held for hours carries one row, not one per tick.
    """
    return _engagement_key(sentinel_path())


def holds() -> list:
    """The live holds, as plain dicts (attribution view for status/CLI)."""
    return [dict(hold) for hold in read_state().holds]


def work_admitted(profile: Optional[str], board: Optional[str] = None, state: Optional[EstopState] = None) -> bool:
    """May this LANE start NEW work right now?

    ``board`` is accepted so the dispatch seam and the cron seam call ONE function, and is
    IGNORED: the lock is on the lane, never on where a card sits. Filing work on a
    "critical" board grants nothing; an allowlisted lane is admitted on every board.
    """
    if state is None:
        state = read_state()
    if not state.engaged:
        return True
    if state.total:
        return False
    return _is_lane_allowed(profile, state)


def _is_lane_allowed(profile: Optional[str], state: EstopState) -> bool:
    """Lockdown admission: the lane must be on the union allowlist. Normalisation is the
    runtime's own profile-id rule — trimmed, case-folded; an unknown id grants nothing.

    A hold with no lane at all (an unassigned card) is NOT a lane and is never admitted:
    "unknown ids grant nothing" applies to the empty id too.
    """
    lane = str(profile or "").strip().lower()
    if not lane:
        return False
    allowed = {str(entry).strip().lower() for entry in state.allow_profiles}
    return lane in allowed


def tick(now: Optional[datetime] = None) -> EstopState:
    """The GATE's own housekeeping: retire what expired, reap what is orphaned, return state.

    D3/D5 — the release leg must ride the surface that GATES the fleet's work, never a verb
    somebody has to remember to run. **The measured defect this exists for:** ``reap_orphans``
    was reachable only from :func:`is_engaged`, and ``is_engaged`` was reachable only from the
    pause CLI — while every surface that actually holds work back read the state WITHOUT
    reaping (:func:`check_paused` for the cron tick, the gateway's kanban watchers and the
    cron webhook; the kanban dispatcher's own tick seam). A leaked hold therefore parked the
    fleet for its whole lease and left a ledger ending at ``arm`` unless a human happened to
    run ``hermes pause``. Every one of those seams calls THIS now.

    One read, then one write path: the returned state is the state AFTER the reaping, so a
    tick can never gate on a hold it has just released.
    """
    state = read_state()
    for hold in list(state.expired):
        _retire_hold(hold)
    if state.holds and reap_orphans(state, now):
        state = read_state()
    return state


def is_engaged() -> bool:
    """True while ANY live hold sits on a candidate sentinel; fail SAFE (True) on stat
    errors. Expired holds are retired here, so a holder that died without releasing cannot
    park the fleet forever. A SCOPED (all-lockdown) state is still engaged — the caller
    that wants "may new work start at all" wants :func:`work_admitted` instead.

    D3 — the INDEPENDENT REAPER rides :func:`tick`, so a run-owned hold whose owner is
    terminal in a run ledger is released from OUTSIDE the run that armed it. This entry point
    serves the status/CLI readers; the seams that actually gate work reach the same reaper
    through :func:`check_paused` and the dispatcher's tick read (see :func:`tick`).
    """
    return tick().engaged


def _retire_hold(hold: dict) -> None:
    """Drop ONE expired hold, reporting it once per (handle, expiry) and keeping the file
    when other holders are still live — the 02:09:52 defect was an expiry that took a pause
    it did not own with it. D5 — the lift is ALSO written to the ledger: a hold that
    self-lifted must not leave a ledger ending at ``arm`` (tonight's log shape, and the
    reason no log-keyed stale-hold detector could ever have seen it). UNFENCED by
    :func:`arm_admission`: an expiry is the engine's own act, never a caller's release."""
    key = f"{hold['handle']}@{hold.get('expires_at')}"
    with _log_lock:
        first_report = _expired_logged.get(hold["handle"]) != key
        _expired_logged[hold["handle"]] = key
    removed = _remove_holds([hold])
    if first_report:
        record_event(
            "expire", sentinel=Path(hold.get("path") or sentinel_path()),
            path=hold.get("path"), handle=hold["handle"], owner=hold.get("owner"),
            actor=hold.get("actor"), mode=hold.get("mode"), cause="deadman",
            owner_run=hold.get("owner_run"), expires_at=hold.get("expires_at"),
            removed=bool(removed),
        )
        logger.warning(
            "ESTOP hold '%s' (%s) EXPIRED at its deadman TTL — that hold is released%s. "
            "%s",
            hold["handle"], hold.get("owner"),
            "" if removed else " (the entry could not be rewritten; it stays inert)",
            "Other holds remain: " + ", ".join(sorted(read_state().owners)) if read_state().engaged
            else "No holds remain — dispatch resumes.",
        )


def _write_registry(path: Path, holds_list: list) -> bool:
    """Persist ``holds_list`` to ``path``; remove the file when nothing is left to hold.

    A file with no holds must not linger: a zero-byte sentinel is an unattributed ALARM that
    would confuse every reader (see ``DEFECT_UNATTRIBUTED_EMPTY``), so leaving it behind would
    leave a bare ``touch`` lying around. Returns False when the write failed, which callers
    report — never silently.
    """
    if not holds_list:
        removed = True
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except (OSError, AttributeError):
            removed = False
        with _log_lock:
            _expired_logged.pop(str(path), None)
            _unattributed_logged.pop(str(path), None)
        return removed
    payload = {
        "schema": SCHEMA_VERSION,
        HOLDS_KEY: [
            {
                key: value
                for key, value in (
                    ("handle", hold.get("handle")),
                    ("owner", hold.get("owner")),
                    # D1: WHO armed it, and what the body claimed, both on the record.
                    ("actor", hold.get("actor")),
                    ("claimed_owner", hold.get("claimed_owner")),
                    ("mode", hold.get("mode")),
                    ("reason", hold.get("reason")),
                    ("engaged_at", hold.get("engaged_at")),
                    ("expires_at", hold.get("expires_at")),
                    ("allow", hold.get("allow") or None),
                    ("venue", hold.get("venue")),
                    # D1/D2: the run this hold belongs to (the reaper's binding) and the last
                    # renewal. Both must survive every rewrite or a co-holder's release would
                    # strip the binding that keeps the hold resolvable.
                    ("owner_run", hold.get("owner_run")),
                    ("renewed_at", hold.get("renewed_at")),
                    # D2: the provenance signature, written VERBATIM when the hold brought one
                    # (a keeper we did not author keeps its own), else dropped.
                    ("sig", hold.get("sig")),
                )
                if value not in (None, "", {}, [])
            }
            for hold in holds_list
        ],
    }
    # Top-level mirror of the DOMINANT hold: the pre-registry body shape, so a reader that
    # predates the registry (and the shipped sentinel-body contract) still reads the pause it
    # expects. Written, never read back — `read_state` prefers ``holds`` when it is present.
    effective = _dominant(holds_list)
    payload["mode"] = effective.get("mode") or MODE_ESTOP
    for key in ("reason", "engaged_at", "expires_at"):
        if effective.get(key) not in (None, ""):
            payload[key] = effective[key]
    if effective.get("allow"):
        payload["allow"] = effective["allow"]
    tmp_name = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace: a reader must never see a half-written registry, because a
        # truncated body reads as a TOTAL hold — the gate's scope flipping mid-incident.
        with tempfile.NamedTemporaryFile(
            "w", dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp",
            delete=False, encoding="utf-8",
        ) as handle:
            tmp_name = handle.name
            handle.write(json.dumps(payload, indent=2) + "\n")
        os.replace(tmp_name, path)
    except OSError:
        if tmp_name:
            with suppress(OSError):
                os.unlink(tmp_name)
        return False
    return True


def _remove_holds(targets: list) -> bool:
    """Remove the given holds (matched by handle) from their own sentinel files, one
    rewrite per file, leaving every OTHER holder's entry byte-identical."""
    by_path: dict = {}
    for hold in targets:
        by_path.setdefault(hold.get("path") or str(sentinel_path()), set()).add(hold["handle"])
    ok = True
    for raw_path, handles in by_path.items():
        path = Path(raw_path)
        existing, defect = _read_holds(path)
        if defect in (DEFECT_UNREADABLE, DEFECT_STAT_ERROR):
            # The BODY could not be read entry-wise, so it cannot be edited entry-wise either:
            # drop the whole file so the release is real (the held lanes were never the
            # unreadable file's to keep). A per-ENTRY defect (DEFECT_UNVERIFIED for a hold with
            # no valid provenance) is NOT this case — those entries are readable dicts, and
            # wiping the file would destroy the co-holders that verified beside them.
            ok = _write_registry(path, []) and ok
            continue
        kept = [
            {
                key: hold.get(key)
                for key in ("handle", "owner", "actor", "claimed_owner", "mode", "reason",
                            "engaged_at", "expires_at", "allow", "venue", "sig",
                            "owner_run", "renewed_at")
            }
            for hold in existing
            if hold["handle"] not in handles
        ]
        ok = _write_registry(path, kept) and ok
    return ok


# ---------------------------------------------------------------------------------
# D3 — the INDEPENDENT reaper: resolve a run-owned hold's owner in the run ledgers.
# It lives HERE, in the engine's read path, and never inside the run a hold guards.
# ---------------------------------------------------------------------------------


def _run_ledger_files() -> list:
    """The JSONL run ledgers beside this home — newest first, bounded (a tick surface cannot
    walk an unbounded directory)."""
    try:
        root = _hermes_home() / RUN_LEDGER_DIRNAME
        files = [path for path in root.glob(RUN_LEDGER_GLOB) if path.is_file()]
    except (OSError, ValueError):
        return []
    try:
        files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        pass
    return files[:RUN_LEDGER_MAX_FILES]


def _run_ledger_row(owner_ids: set) -> Optional[tuple]:
    """``(status, finished, source)`` for the first ledger row naming one of ``owner_ids``."""
    for path in _run_ledger_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            named = {str(row.get(key) or "").strip()
                     for key in ("run", "id", "run_id", "owner_run")}
            if not (named & owner_ids):
                continue
            status = str(row.get("status") or "").strip().lower()
            if status or row.get("finished_at"):
                return status, bool(row.get("finished_at")), str(path)
    return None


def _run_store_row(owner_ids: set) -> Optional[tuple]:
    """``(status, finished, source)`` from the shared yoyoflow run store, when it knows one of
    the ids. An unreadable store is NOT a reason to lift a hold — it returns None."""
    try:
        db = Path(RUN_STORE_DB)
        if not db.is_file():
            return None
    except OSError:
        return None
    try:
        import sqlite3  # lazy: the engine's import stays light until a run-owned hold exists

        with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2) as conn:
            placeholders = ",".join("?" for _ in owner_ids)
            row = conn.execute(
                f"SELECT status, finished_at FROM runs WHERE id IN ({placeholders}) "
                "ORDER BY started_at DESC LIMIT 1",
                tuple(sorted(owner_ids)),
            ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    return str(row[0] or "").strip().lower(), bool(row[1]), str(db)


def _run_is_terminal(status: str, finished: bool) -> bool:
    """Statuses we do not understand are NEVER terminal: releasing on one would fail OPEN."""
    if status in RUN_ACTIVE_STATUSES:
        return False
    if status in RUN_TERMINAL_STATUSES:
        return True
    return bool(finished)


def run_outcome(owner_ids) -> Optional[tuple]:
    """``(status, terminal, source)`` for a run the fleet's ledgers know, else None.

    None is the important half (D3): ABSENT a row for the owner, the lease (D2) is the only
    bound. Nothing here guesses that a missing row means a dead run — that would release a
    live section the moment a runner had not written its row yet.
    """
    ids = {str(value).strip() for value in owner_ids if str(value or "").strip()}
    if not ids:
        return None
    found = _run_ledger_row(ids) or _run_store_row(ids)
    if found is None:
        return None
    status, finished, source = found
    return status, _run_is_terminal(status, finished), source


def _hold_owner_ids(hold: dict) -> list:
    """Every identifier a run ledger might know this hold's owner by: the bound ``owner_run``
    and the ``actor`` that armed it (the update's own handle IS the run it belongs to)."""
    return [value for value in (hold.get("owner_run"), hold.get("actor")) if value]


def _is_run_owned(hold: dict) -> bool:
    """A hold a RUN owns, rather than the operator's own panic button.

    Bound (``owner_run`` present) it is resolvable; unbound it is an orphan of an unknown run
    and only the lease bounds it (D3). A hold armed by hand and read back ``unverified`` is
    nobody's run and is left to the operator.
    """
    if hold.get("owner_run"):
        return True
    actor = str(hold.get("actor") or "")
    return bool(actor) and not actor.startswith(ACTOR_OPERATOR)


def _lease_deadline(hold: dict) -> Optional[datetime]:
    """When the DEFAULT lease of an unbound run-owned hold runs out: ``engaged_at`` plus the
    section budget. None when the hold names no usable engage time — then it is only reported."""
    engaged = _parse_stamp(hold.get("engaged_at"))
    if engaged is None:
        return None
    return engaged + timedelta(seconds=DEFAULT_SECTION_BUDGET_S)


def _release_orphan(hold: dict, *, cause: str, detail: str) -> bool:
    """Release ONE hold the reaper resolved, and say so in the ledger (D5 — the whole point:
    tonight's hold was released by hand BECAUSE nothing wrote the row nobody could act on)."""
    if not _remove_holds([hold]):
        logger.error(
            "ESTOP: orphan hold '%s' (%s) could not be released — it is still in force (%s)",
            hold.get("handle"), hold.get("owner"), detail,
        )
        return False
    record_event(
        "release", sentinel=Path(hold.get("path") or sentinel_path()),
        path=hold.get("path"), handle=hold["handle"], owner=hold.get("owner"),
        actor=hold.get("actor"), mode=hold.get("mode"), cause=cause,
        owner_run=hold.get("owner_run"), reaper=True,
        allow=hold.get("allow") or None,
        allow_profiles=list((hold.get("allow") or {}).get("profiles") or []),
    )
    logger.warning(
        "ESTOP hold '%s' (%s) was ORPHANED — %s. Released by the independent reaper "
        "(cause %s).", hold.get("handle"), hold.get("owner"), detail, cause,
    )
    return True


def _report_unbound_run(hold: dict, now: datetime) -> bool:
    """A run-owned hold with NO ``owner_run`` is an orphan of an UNKNOWN run (D3): the default
    lease measured from its own engage time is the only bound it can be given, and it is
    REPORTED either way — once per engagement, so a tick does not become a log flood."""
    deadline = _lease_deadline(hold)
    if deadline is not None and now >= deadline:
        return _release_orphan(
            hold, cause="orphan",
            detail=("it names no owner_run and its default lease from "
                    f"{hold.get('engaged_at')} ran out"),
        )
    key = f"{hold.get('handle')}@{hold.get('engaged_at')}"
    with _log_lock:
        first_report = _unbound_logged.get(str(hold.get("handle"))) != key
        _unbound_logged[str(hold.get("handle"))] = key
    if first_report:
        logger.warning(
            "ESTOP hold '%s' (%s) names NO owner_run: it is an orphan of an unknown run, so "
            "only its default lease bounds it (until %s). Arm with an owner binding so the "
            "reaper can resolve it.",
            hold.get("handle"), hold.get("owner"),
            deadline.isoformat() if deadline else "no usable engaged_at — UNBOUNDED",
        )
    return False


def reap_orphans(state: Optional[EstopState] = None,
                 now: Optional[datetime] = None) -> list:
    """Release every run-owned hold whose owning run is OVER, from outside that run (D3).

    Called from :func:`tick` — and :func:`tick` is what the surfaces that GATE work run:
    :func:`check_paused` (the cron scheduler tick, the gateway's kanban watchers, the cron
    webhook) and the kanban dispatcher's own tick seam. It is deliberately NOT part of the
    update DAG and cannot be stopped by the hold it releases (it never consults
    :func:`arm_admission`): the release leg must not be inside the thing it protects.
    """
    state = read_state() if state is None else state
    moment = now or datetime.now(UTC)
    released = []
    for hold in list(state.holds):
        if not _is_run_owned(hold):
            continue
        outcome = run_outcome(_hold_owner_ids(hold))
        if outcome is not None and outcome[1]:
            status, _terminal, source = outcome
            if _release_orphan(
                hold, cause="orphan",
                detail=(f"its run {hold.get('owner_run') or hold.get('actor')} is "
                        f"{(status or 'finished')} in {source}"),
            ):
                released.append(hold)
            continue
        if not hold.get("owner_run"):
            if _report_unbound_run(hold, moment):
                released.append(hold)
    return released


def arm_admission(action: str = "arm") -> tuple:
    """``(admitted, venue, refusal)`` for the CALLER of an arm/lift verb.

    Cooperative fence, not confinement: a delegated child process and a
    dispatched kanban worker may neither arm nor lift a hold. Interactive
    operators, the ops head on its own shell, and prod-owned runners are admitted.

    This lives on the HOLD (here), not on one CLI verb's argument surface, so every
    in-tree caller — ``hermes pause``, the gateway's ``/pause`` path, any python import
    calling :func:`acquire`/:func:`release` — is fenced by the same rule. ``action``
    names the verb the caller is attempting (``arm``/``lift``) for the venue label.
    """
    try:
        from agent.delegation_context import (
            is_delegated_child_process_context,
            owned_kanban_task,
        )
    except Exception:
        # Never block the operator's panic button on an import error.
        return (True, "unknown", None)
    if is_delegated_child_process_context():
        return (False, "fenced:delegated-child", "this is a delegated child context")
    task = owned_kanban_task()
    if task:
        return (False, f"fenced:kanban-worker({task})", f"this shell owns kanban task {task}")
    return (True, "interactive", None)


def arm(
    owner: str = DEFAULT_OWNER,
    mode: Any = None,
    reason: Optional[str] = None,
    ttl: Any = None,
    expires_at: Any = None,
    allow: Optional[dict] = None,
    replace_owner: bool = False,
    actor: Optional[str] = None,
    order: Optional[str] = None,
    owner_run: Any = None,
    budget: Any = None,
) -> ArmResult:
    """Add ONE hold to this home's sentinel and report what actually landed.

    ``owner`` is the holder's identity. ``actor`` is WHO is arming (defaults to
    :func:`actor_handle`, the environment-derived handle); ``order`` is the authority for a
    third-party re-arm after the operator's resume (D8). Two acquisitions by the same owner
    are TWO holds unless ``replace_owner`` is set, which is what makes a repeated
    ``hermes pause`` idempotent instead of piling up.

    D4 — the hold may only wear a name its actor is entitled to. The operator's own pause is
    recorded as ``operator``; any other actor gets the hold recorded under the actor's OWN
    handle, with the body's claim kept in ``claimed_owner``. The arm is never refused for
    that reason, it is renamed (there is no wrong moment to stop the fleet).

    D2 — the entry is signed with the key beside this sentinel. If no key can be created the
    hold still lands, UNSIGNED and read back as unverified (a total halt): a provenance
    failure must never stop the fleet being parked.

    D8 — a third-party arm after the OPERATOR's resume, with no recorded hold armed since,
    requires ``order``. Without it the arm is REFUSED with :class:`EstopOrderRequired` and a
    ``refused`` ledger row; nothing is written to the sentinel.

    ``mode`` defaults to ``estop`` (a total halt); ``lockdown`` scopes the stop to the lanes
    in ``allow["profiles"]``. Pre-existing holds — including a pre-registry sentinel body,
    which reads as unverified and TOTAL — are preserved.

    D1 — a hold names the RUN it belongs to (``owner_run``: the caller's value, else
    ``YOYOFLOW_RUN_ID``/``HERMES_UPDATE_RUN_ID``/the kanban card id, else the run the actor
    handle itself names). A run-scoped arm (``bot:<lane>:<run>``, ``card:*``, ``delegated:*``)
    that cannot resolve one is REFUSED with :class:`EstopUnboundRun` and a ``refused`` ledger
    row — nothing is written to the sentinel.

    D2 — the deadline is not optional. A caller's ``ttl`` wins; without one the ENGINE applies
    a default lease from the section's declared ``budget`` (:data:`DEFAULT_SECTION_BUDGET_S`),
    so no hold is ever unbounded. Renewal (:func:`renew`) is the liveness proof.

    FENCED: an unadmitted venue (a delegated child, a dispatched kanban worker) is refused
    here, at the hold itself — see :func:`arm_admission`. The refusal is a raised
    :class:`EstopRefusal`, so no caller can arm by accident on a fenced surface.
    """
    admitted, venue, refusal = arm_admission("arm")
    if not admitted:
        raise EstopRefusal(f"Refusing to arm the emergency stop: {refusal}.")
    actor_id = str(actor).strip() if actor else actor_handle()
    target = sentinel_path()
    resolved_mode, _unknown = _normalize_mode(mode)
    now = datetime.now(UTC)
    claim = _normalize_owner(owner)
    if actor_id == ACTOR_OPERATOR:
        owner_id, claimed_owner, renamed = claim, None, False
    else:
        owner_id, claimed_owner, renamed = actor_id, claim, claim != actor_id
    seconds = parse_duration(ttl)
    budget_s = parse_duration(budget)
    # D1 — the ownership binding, BEFORE anything is written: an arm that cannot name its run
    # is the one that outlives it (2026-10-10), so it is refused rather than recorded.
    bound_run = owner_run_for(actor_id, owner_run)
    if bound_run is None and _is_run_scoped(actor_id):
        detail = unbound_run_refusal(actor_id)
        recorded = record_event(
            "refused", sentinel=target, path=str(target), action="arm", actor=actor_id,
            mode=resolved_mode, reason="unbound-run", ttl_s=seconds,
        )
        raise EstopUnboundRun(
            detail + ("" if recorded
                      else "\n   (the ledger could not be written — this refusal is NOT recorded)")
        )
    # D2 — the lease. Never None for a run-owned hold; the section budget is the default, so a
    # bound that is too small fails SAFE and an absent one (tonight's failure) cannot recur.
    # D8 — the order gate, BEFORE anything is written.
    proceed, gate_kind, resume_at = order_gate(actor_id, order)
    if not proceed:
        recorded = record_event(
            "refused", sentinel=target, path=str(target), action="arm", actor=actor_id,
            mode=resolved_mode, reason="no-order-after-operator-resume", order=order or None,
        )
        raise EstopOrderRequired(
            order_refusal(actor_id, resume_at) + (
                "" if recorded else "\n   (the ledger could not be written — this refusal is NOT recorded)"
            )
        )
    if actor_id == ACTOR_OPERATOR:
        recorded_order = f"operator:interactive@{now.isoformat()}"
    else:
        recorded_order = str(order).strip() if order else None
    expiry = _resolve_expiry(expires_at)
    # D2 — the lease. A caller's --ttl/--expires-at wins; WITHOUT one the engine applies the
    # section's declared budget (default 30m) so that ``expires_at`` is never None. Expiry is
    # what bounds a holder that dies silently: a bound too small fails SAFE (the section
    # renews or re-arms), an absent bound failed OPEN on 2026-10-10.
    lease_default = False
    recorded_ttl = seconds
    if expiry is None:
        lease_seconds = seconds or budget_s or DEFAULT_SECTION_BUDGET_S
        expiry = now + timedelta(seconds=lease_seconds)
        recorded_ttl = lease_seconds
        lease_default = seconds is None
    handle = _handle_for(owner_id, now)
    existing, _defect = _read_holds(target)
    kept = [hold for hold in existing if not (replace_owner and hold["owner"] == owner_id)]
    entry = {
        "handle": handle,
        "owner": owner_id,
        "actor": actor_id,
        "claimed_owner": claimed_owner,
        "mode": resolved_mode,
        "reason": reason or None,
        "engaged_at": now.isoformat(),
        "expires_at": expiry.isoformat() if expiry is not None else None,
        "allow": _normalize_allow(allow),
        "venue": venue,
        # D1 — the binding the INDEPENDENT reaper resolves the hold against. Signed with the
        # rest of the entry, so it cannot be rewritten to release somebody else's hold.
        "owner_run": bound_run,
    }
    # D2 — sign the entry EXACTLY as it will be written, so a later read recomputes the same
    # canonical text. None means "no key beside this sentinel": unsigned, still a hold.
    entry["sig"] = _sign_fields(_canonical_fields(entry), target)
    kept.append(entry)
    if not _write_registry(target, kept):
        logger.error("ESTOP hold %s could not be written to %s", handle, target)
        if len(kept) == 1:
            # Nothing else is held on this sentinel. A zero-byte file is NOT a hold any more
            # (it is an unattributed alarm), so the old ``touch()`` last resort would now
            # silently leave the fleet RUNNING — the worst failure this module has. Write a
            # MINIMAL but ATTRIBUTED registry body straight to the path instead.
            try:
                target.write_text(
                    json.dumps(
                        {
                            "schema": SCHEMA_VERSION,
                            HOLDS_KEY: [
                                {
                                    key: value
                                    for key, value in entry.items()
                                    if key in ("handle", "owner", "actor", "claimed_owner",
                                               "mode", "venue", "engaged_at", "sig", "reason",
                                               "expires_at", "allow", "owner_run")
                                    and value not in (None, "", {}, [])
                                }
                            ],
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
            except OSError as exc:
                raise EstopWriteError(
                    f"hold {handle} could not be recorded at {target} — the pause is NOT in force"
                ) from exc
        else:
            # Other holders exist and the registry cannot be rewritten around them:
            # refuse loudly rather than report a hold that is not in force.
            raise EstopWriteError(
                f"hold {handle} could not be recorded at {target} — the pause is NOT in force"
            )
    # G1 (t_00425393): the append-only record must answer WHAT was held and OVER WHICH LANES.
    # The sentinel body carries ``allow.profiles`` — the very lane list the dispatcher reads —
    # but a lift DELETES that body, so without the lane list on the arm rows the released scope
    # is unrecoverable. Carry the normalised ``allow`` VERBATIM (as written in the state) plus a
    # flat ``allow_profiles`` list, so a lockdown armed with N lanes is legible in the ledger.
    ledger_allow = entry["allow"] or None
    ledger_profiles = list((entry["allow"] or {}).get("profiles") or [])
    # D6 — the ledger. Best-effort, but the caller is TOLD when it failed.
    ledger_ok = record_event(
        "arm", sentinel=target, path=str(target), handle=handle, owner=owner_id, actor=actor_id,
        claimed_owner=claimed_owner, mode=resolved_mode, reason=reason or None,
        order=recorded_order, ttl_s=recorded_ttl, verified=entry["sig"] is not None,
        owner_run=bound_run, lease_default=lease_default or None,
        allow=ledger_allow, allow_profiles=ledger_profiles,
    )
    if renamed:
        ledger_ok = record_event(
            "arm_renamed", sentinel=target, path=str(target), handle=handle, owner=owner_id,
            actor=actor_id, claimed_owner=claimed_owner, mode=resolved_mode, reason=reason or None,
            allow=ledger_allow, allow_profiles=ledger_profiles,
        ) and ledger_ok
    if gate_kind == "ordered":
        ledger_ok = record_event(
            "arm_after_resume", sentinel=target, path=str(target), handle=handle, owner=owner_id,
            actor=actor_id, claimed_owner=claimed_owner, mode=resolved_mode, reason=reason or None,
            order=recorded_order, after_resume=resume_at,
            allow=ledger_allow, allow_profiles=ledger_profiles,
        ) and ledger_ok
    return ArmResult(
        handle=handle,
        owner=owner_id,
        actor=actor_id,
        mode=resolved_mode,
        path=str(target),
        order=recorded_order,
        claimed_owner=claimed_owner,
        renamed=renamed,
        after_resume=resume_at if gate_kind == "ordered" else None,
        verified=entry["sig"] is not None,
        ttl_s=recorded_ttl,
        ledger=ledger_ok,
        owner_run=bound_run,
        lease_default=lease_default,
    )


def acquire(
    owner: str = DEFAULT_OWNER,
    mode: Any = None,
    reason: Optional[str] = None,
    ttl: Any = None,
    expires_at: Any = None,
    allow: Optional[dict] = None,
    replace_owner: bool = False,
    actor: Optional[str] = None,
    order: Optional[str] = None,
    owner_run: Any = None,
    budget: Any = None,
) -> str:
    """Add ONE hold to this home's sentinel and return its handle.

    Thin back-compat wrapper over :func:`arm`, which carries the policy (attribution,
    provenance, the order gate, the ownership binding, the lease) and the facts; this keeps
    the long-standing ``-> str`` contract for in-tree callers that only need the handle.
    """
    return arm(
        owner=owner,
        mode=mode,
        reason=reason,
        ttl=ttl,
        expires_at=expires_at,
        allow=allow,
        replace_owner=replace_owner,
        actor=actor,
        order=order,
        owner_run=owner_run,
        budget=budget,
    ).handle


def renew(
    handle: str,
    ttl: Any = None,
    budget: Any = None,
    actor: Optional[str] = None,
) -> RenewResult:
    """Push a live hold's lease forward — the liveness proof of a run-owned hold (D2).

    A run-owned hold is a LEASE: renewal is the only assertion that its owner is still alive
    and still needs the section guarded, so *expiry means owner-dead within one lease period*.
    The new deadline is set from ``ttl``, else from ``budget``,
    else :data:`DEFAULT_SECTION_BUDGET_S` — and it never moves BACKWARD (a renewal cannot
    shorten a lease it holds).

    Renewal is fenced like an arm or a lift (:func:`arm_admission`): a delegated child or a
    dispatched kanban worker may not keep a stop alive any more than it may start one.

    A handle naming no live hold is reported ``stale`` and NOT created: a run that renews a
    lease it does not hold would go on believing its section was guarded.
    """
    wanted = str(handle or "").strip()
    if not wanted:
        return RenewResult(handle="", message="renew needs a handle to renew")
    admitted, _venue, refusal = arm_admission("renew")
    if not admitted:
        raise EstopRefusal(f"Refusing to renew a hold: {refusal}.")
    actor_id = str(actor).strip() if actor else actor_handle()
    state = read_state()
    target = next((hold for hold in state.holds if hold["handle"] == wanted), None)
    if target is None:
        expired = [hold for hold in state.expired if hold["handle"] == wanted]
        if expired:
            detail = (f"handle '{wanted}' already expired at {expired[0].get('expires_at')} — "
                      f"re-arm the section instead of renewing it")
        else:
            held = ", ".join(f"{hold['handle']} ({hold['mode']})" for hold in state.holds) or "none"
            detail = f"handle '{wanted}' names no live hold — live holds: {held}"
        return RenewResult(handle=wanted, stale=True, message=detail, actor=actor_id,
                           owner_run=None)
    now = datetime.now(UTC)
    seconds = parse_duration(ttl) or parse_duration(budget) or DEFAULT_SECTION_BUDGET_S
    current = _resolve_expiry(target.get("expires_at"))
    deadline = now + timedelta(seconds=seconds)
    if current is not None and current > deadline:
        deadline, seconds = current, int((current - now).total_seconds())
    sentinel = Path(target.get("path") or sentinel_path())
    entry = dict(target)
    entry["expires_at"] = deadline.isoformat()
    entry["renewed_at"] = now.isoformat()
    # Re-sign ONLY what we authored: an entry whose provenance did not verify must stay
    # unverified, or a renewal would launder a hold we never issued into a signed one.
    if entry.get("verified"):
        entry["sig"] = _sign_fields(_canonical_fields(entry), sentinel)
    existing, _defect = _read_holds(sentinel)
    updated = [entry if hold["handle"] == wanted else hold for hold in existing]
    if not _write_registry(sentinel, updated):
        return RenewResult(handle=wanted, message=(
            f"the renewal of '{wanted}' could not be written — the hold still expires at "
            f"{target.get('expires_at')}"
        ), expires_at=target.get("expires_at"), actor=actor_id,
            owner_run=target.get("owner_run"))
    ledger_ok = record_event(
        "renew", sentinel=sentinel, path=target.get("path"), handle=wanted,
        owner=target.get("owner"), actor=actor_id, mode=target.get("mode"),
        owner_run=target.get("owner_run"), ttl_s=seconds, expires_at=deadline.isoformat(),
        verified=bool(entry.get("verified")),
    )
    return RenewResult(
        handle=wanted, renewed=True, expires_at=deadline.isoformat(), ttl_s=seconds,
        owner_run=target.get("owner_run"), actor=actor_id, ledger=ledger_ok,
        message=f"renewed '{wanted}' until {deadline.isoformat()} (lease {seconds}s)",
    )


def release(
    handle: Optional[str] = None,
    owner: Optional[str] = None,
    actor: Optional[str] = None,
    all_holds: bool = False,
) -> ReleaseResult:
    """Lift a hold — by exact ``handle``, by ``owner``, or every hold with ``all_holds``.

    A release acts on exactly the holds it names and never touches another holder's entry.
    ``actor`` is WHO is lifting (defaults to :func:`actor_handle`); D7 gives that teeth:

    * a bare release (neither handle nor owner) lifts the CALLER'S OWN holds, and when the
      caller IS the operator it also clears the UNVERIFIED holds — nothing else can name them;
    * ``owner=operator`` from any other actor is REFUSED (``forbidden``): a bot may not
      silently resume the fleet the operator stopped. It serves the window without lifting it
      (a lockdown hold is scoped), or escalates — the hold's TTL is the backstop;
    * ``all_holds`` is the operator's own emergency exit and is refused to every other actor;
    * ``handle=`` is unchanged, and now also names an ``unverified#...`` hold.

    A handle that names no LIVE hold (unknown, already released, or expired) is reported as
    ``stale`` so the caller can fail loudly: a workflow that released into the void believes
    it holds a scope it no longer has.

    Each released hold is recorded in the ledger (D6) AFTER its file write succeeds, because
    D8's re-arm gate reads those rows; ``ReleaseResult.ledger`` is False when a row could not
    be written. D5 — every row names its ``cause`` (:data:`RELEASE_CAUSES`): a released scope
    stays readable after the sentinel body, the lane list and the deadline are all gone.

    FENCED: an unadmitted venue (a delegated child, a dispatched kanban worker) cannot lift
    a hold here — see :func:`arm_admission`. The internal deadman expiry path does NOT come
    through this function (``_retire_hold`` -> ``_remove_holds``), so an expiry is never
    fenced by the caller's venue.
    """
    admitted, _venue, refusal = arm_admission("lift")
    if not admitted:
        raise EstopRefusal(f"Refusing to lift a hold: {refusal}.")
    actor_id = str(actor).strip() if actor else actor_handle()
    state = read_state()
    unverified_cleared = 0
    if all_holds:
        if actor_id != ACTOR_OPERATOR:
            return ReleaseResult(
                False,
                forbidden=True,
                message=(
                    f"refusing to lift EVERY hold: {actor_id} is not the operator.\n"
                    f"   `--all` is the operator's own exit. Lift your own hold by handle, or "
                    f"ask the operator to run it."
                ),
                remaining=state.holds,
                actor=actor_id,
            )
        targets = list(state.holds)
        label = "every hold"
        cause = "all"
    elif handle is not None:
        wanted = str(handle).strip()
        live = [hold for hold in state.holds if hold["handle"] == wanted]
        if not live:
            expired = [hold for hold in state.expired if hold["handle"] == wanted]
            if expired:
                detail = f"handle '{wanted}' already expired at {expired[0].get('expires_at')}"
            else:
                held = ", ".join(f"{hold['handle']} ({hold['mode']})" for hold in state.holds) or "none"
                detail = f"handle '{wanted}' names no live hold — live holds: {held}"
            return ReleaseResult(False, stale=True, message=detail, remaining=state.holds,
                                 actor=actor_id)
        targets = live
        label = wanted
        cause = "handle"
    else:
        owner_id = actor_id if owner is None else _normalize_owner(owner)
        if owner_id == ACTOR_OPERATOR and actor_id != ACTOR_OPERATOR:
            return ReleaseResult(
                False,
                forbidden=True,
                message=(
                    f"refusing to lift the operator's hold: {actor_id} is not the operator.\n"
                    f"   A bot may not resume the fleet the operator stopped. Serve the window "
                    f"without lifting it (a lockdown hold is scoped to its allowlist), or "
                    f"escalate to the operator — the hold's TTL is the backstop."
                ),
                remaining=state.holds,
                actor=actor_id,
            )
        targets = [hold for hold in state.holds if hold["owner"] == owner_id]
        if actor_id == ACTOR_OPERATOR and owner_id == ACTOR_OPERATOR:
            # D7 — the operator's own resume clears the UNVERIFIED holds too: nothing else can
            # name them, and an unattributable hold is not any holder's scope to keep.
            named = {hold["handle"] for hold in targets}
            extra = [
                hold for hold in state.holds
                if hold["owner"] == ACTOR_UNVERIFIED and hold["handle"] not in named
            ]
            unverified_cleared = len(extra)
            targets = targets + extra
        label = f"owner '{owner_id}'"
        # D5 — WHY. A bare lift is the caller releasing its OWN hold (``explicit``); the
        # operator resuming their own stop is the ``operator-resume`` the ledger names for it;
        # anything else naming an owner is an ``owner`` release.
        cause = ("explicit" if owner is None
                 else "operator-resume" if actor_id == ACTOR_OPERATOR
                 else "owner")
        if not targets:
            held = ", ".join(sorted(state.owners)) or "none"
            return ReleaseResult(
                False,
                message=f"no live hold owned by '{owner_id}' — live holders: {held}",
                remaining=state.holds,
                actor=actor_id,
            )
    if not _remove_holds(targets):
        return ReleaseResult(
            False,
            message=f"release of {label} could not be written — the hold is still in force",
            remaining=state.holds,
            actor=actor_id,
        )
    ledger_ok = True
    for target in targets:
        # G1 (t_00425393): the release row names the handle (already) AND the lane list of the
        # hold it lifted, so the released scope stays recoverable after the sentinel body — and
        # with it ``allow.profiles`` — is gone.
        target_allow = target.get("allow") or None
        ledger_ok = record_event(
            "release", sentinel=Path(target.get("path") or sentinel_path()),
            path=target.get("path"), handle=target["handle"], owner=target.get("owner"),
            actor=actor_id, mode=target.get("mode"), verified=bool(target.get("verified")),
            cause=cause, owner_run=target.get("owner_run"),
            allow=target_allow, allow_profiles=list((target_allow or {}).get("profiles") or []),
        ) and ledger_ok
    after = read_state()
    released_count = len(targets)
    message = f"released {label} ({released_count} hold{'s' if released_count != 1 else ''})"
    if unverified_cleared:
        message += f" + {unverified_cleared} unverified hold{'s' if unverified_cleared != 1 else ''}"
    if after.engaged:
        message += " — still held by " + ", ".join(sorted(after.owners))
    else:
        message += " — the fleet is clear"
    return ReleaseResult(
        True,
        cleared=not after.engaged,
        message=message,
        remaining=after.holds,
        handles=tuple(hold["handle"] for hold in targets),
        actor=actor_id,
        unverified_cleared=unverified_cleared,
        ledger=ledger_ok,
        cause=cause,
    )


def engage(
    reason: Optional[str] = None,
    allow: Optional[dict] = None,
    ttl: Any = None,
    expires_at: Any = None,
    mode: Any = None,
    owner: str = DEFAULT_OWNER,
    replace_owner: bool = True,
    actor: Optional[str] = None,
    order: Optional[str] = None,
    owner_run: Any = None,
    budget: Any = None,
) -> Path:
    """Arm a hold on the ESTOP sentinel and return the sentinel path.

    The panic-button entry point: ``owner`` defaults to ``operator`` (the library default, kept
    for back-compat) and mode ``estop`` (a total halt) unless ``mode="lockdown"`` is given. It
    is idempotent — re-engaging REPLACES that OWNER's entry — and it leaves every OTHER
    holder's entry byte-identical, which is what stops a maintenance hold and the operator's
    pause from releasing each other.

    WHO the hold is recorded under is decided by :func:`actor_handle` / ``actor`` (D3/D4): a
    caller that is not the operator does not get to wear the operator's name, and a
    third-party re-arm after the operator's resume needs ``order`` (D8 —
    :class:`EstopOrderRequired`). Call :func:`arm` when you need those facts reported.

    ``allow`` is ``{"user_ids": [...], "profiles": [...]}`` (either key optional, values
    coerced to strings); ``ttl`` accepts ``45m``/``90m``/``2h``/seconds and sets the
    deadman, or pass ``expires_at`` (ISO-8601 / aware datetime) directly, or let the engine
    apply the section ``budget`` (D2). An unusable ttl still engages — it never half-arms a
    pause — and is reported by the CLI.
    """
    acquire(
        owner=owner,
        mode=mode,
        reason=reason,
        ttl=ttl,
        expires_at=expires_at,
        allow=allow,
        replace_owner=replace_owner,
        actor=actor,
        order=order,
        owner_run=owner_run,
        budget=budget,
    )
    return sentinel_path()


def disengage(owner: Optional[str] = None, handle: Optional[str] = None,
              actor: Optional[str] = None) -> bool:
    """Release the caller's own hold (or the one named by ``handle``).

    True when the CALLER'S hold was released — NOT when the fleet is clear: a co-holder's
    section survives, and that is the point (releasing the operator's pause must not lift an
    ops-head maintenance scope, nor the reverse). Callers that report "resumed" to a human
    want :func:`release` instead, whose result names the remaining holders.

    A bare call lifts the hold owned by the CALLER's actor (D4: the same actor that
    ``engage()`` recorded), so ``engage(); disengage()`` stay a matched pair from any venue.
    """
    result = release(handle=handle, owner=None if handle else owner, actor=actor)
    if result.stale:
        logger.warning("ESTOP release refused: %s", result.message)
    elif result.released and result.remaining:
        logger.info("ESTOP: %s", result.message)
    return bool(result.released)


def _dominant(holds_list: list) -> dict:
    """The hold a single-hold view should show: a total hold wins (it governs the fleet),
    otherwise the most recently engaged lockdown hold."""
    for hold in holds_list:
        if hold.get("mode") == MODE_ESTOP:
            return hold
    return sorted(holds_list, key=lambda hold: hold.get("engaged_at") or "")[-1]


def _dominant_hold(state: EstopState) -> dict:
    """The hold a single-hold view should show: a total hold wins (it governs the fleet),
    otherwise the most recently engaged lockdown hold."""
    return _dominant(list(state.holds))


def get_state() -> Optional[dict]:
    """Legacy single-hold view of the pause, or None when the fleet is clear.

    Keeps the shipped shape (``{"reason", "engaged_at", "expires_at", "allow"}``) for
    existing callers, and adds the registry facts (``mode``/``counts``/``holders``/
    ``defect``). New code reads the union with :func:`read_state` and decides admission with
    :func:`work_admitted`; this function never decides anything.
    """
    state = read_state()
    if not state.engaged:
        return None
    hold = _dominant_hold(state)
    return {
        "reason": hold.get("reason"),
        "engaged_at": hold.get("engaged_at"),
        "expires_at": hold.get("expires_at"),
        "allow": hold.get("allow") or {},
        "mode": hold.get("mode"),
        "counts": state.counts,
        "holders": state.owners,
        "defect": state.defect,
    }


def is_allowed(
    user_id: Optional[str] = None,
    profile: Optional[str] = None,
    state: Optional[Any] = None,
) -> bool:
    """True when a live hold's allowlist admits this authenticated identity.

    PRIMARY key is ``user_id`` (identity survives a platform/profile change); ``profile``
    is the secondary key for a maintenance lane. The allowlists of ALL live holds are
    UNIONed, so two concurrent holds cannot hide each other's exemptions. No allowlist — or
    an unreadable sentinel — admits nobody. Accepts an :class:`EstopState` (preferred, and
    what a tick should pass down) or the legacy ``get_state()`` dict.
    """
    if state is None:
        state = read_state()
    if isinstance(state, EstopState):
        allow_users = {str(entry) for entry in state.allow_user_ids}
        allow_profiles = {str(entry) for entry in state.allow_profiles}
    else:
        allow = (state or {}).get("allow") or {}
        allow_users = {str(entry) for entry in (allow.get("user_ids") or [])}
        allow_profiles = {str(entry) for entry in (allow.get("profiles") or [])}
    if user_id is not None and str(user_id) in allow_users:
        return True
    return bool(profile) and str(profile) in allow_profiles


def paused_reply() -> Optional[str]:
    """Short user-facing notice for new gateway turns, or None if not paused."""
    state = read_state()
    if not state.engaged:
        return None
    hold = _dominant_hold(state)
    tag = f" ({hold['reason']})" if hold.get("reason") else ""
    until = f" Auto-resumes {hold['expires_at']}." if hold.get("expires_at") else ""
    if state.total:
        return (
            f"⏸️ Hermes is paused{tag}. New work is on hold; run `hermes resume` to pick "
            f"things back up.{until}"
        )
    lanes = ", ".join(sorted(state.allow_profiles)) or "(no lane — every lane is held)"
    return (
        f"⏸️ Hermes is in lockdown{tag}: {lanes} keep working; every other lane's cards and "
        f"cron jobs are held until the lockdown lifts.{until}"
    )


def _resume_note(prior: Optional[dict]) -> str:
    """HOW the hold that paused a component went away, from the estop ledger (best effort).

    ``check_paused`` logged "dispatch paused …" but nothing when the sentinel cleared, so the
    gateway log could not say the fleet had RESUMED, which hold ended, or whether anything
    actually released it (card t_f88ddb45).  A recorded ``release`` newer than the pause wins;
    otherwise the hold's own declared expiry; otherwise the sentinel was removed with no event
    on record.  A ledger read must never break a tick, so it is guarded.
    """
    prior = prior or {}
    since = str(prior.get("logged_at") or "")
    try:
        rows = read_events()
    except Exception:
        rows = []
    for row in reversed(rows):
        if str(row.get("event") or "") != "release":
            continue
        if str(row.get("at") or "") <= since:
            break
        who = str(row.get("actor") or row.get("owner") or "unknown")
        handle = row.get("handle")
        return f"released by {who}{f' (handle {handle})' if handle else ''}"
    if prior.get("expires_at"):
        return f"auto-resumed at its declared expiry ({prior['expires_at']})"
    return "cleared with no release event on record (sentinel removed out-of-band)"


def check_paused(component: str, logger: logging.Logger) -> bool:
    """True only when NEW WORK must stop ENTIRELY — i.e. any live ``estop`` hold.

    Logs once per engagement per component (re-armed after a full release). Under a scoped
    ``lockdown`` (only lockdown holds live) this returns False on purpose: the tick RUNS,
    admits the allowlisted lanes, and the per-card / per-job lane gate holds everything
    else — which is what keeps a scoped stop from looking like an idle board.

    D3/D5 — this is a GATE, so it runs the gate's housekeeping (:func:`tick`) rather than a
    bare read: an expired hold is retired and an orphaned one is released HERE, on this tick,
    by the fleet's own machinery. A hold that leaked out of a dead run must not have to wait
    for a human to notice (2026-10-10: it waited ~2h).
    """
    state = tick()
    if not state.engaged:
        with _log_lock:
            was_paused = component in _logged_components or component in _lockdown_logged
            prior = _last_holds.pop(component, None)
            _logged_components.discard(component)
            _lockdown_logged.discard(component)
        if was_paused:
            # The counterpart to "dispatch paused": say the fleet RESUMED, which hold ended,
            # and how it ended (card t_f88ddb45).  Built outside the lock -- it reads the
            # ledger -- so a slow ledger never serialises the tick.
            reason = (prior or {}).get("reason")
            suffix = f" (reason: {reason})" if reason else ""
            logger.info(
                "%s dispatch resumed — the emergency stop that paused it%s is no longer "
                "armed: %s (%s)",
                component, suffix, _resume_note(prior), sentinel_path(),
            )
        return False
    if state.total:
        with _log_lock:
            first = component not in _logged_components
            _logged_components.add(component)
            _lockdown_logged.discard(component)
            if first:
                held = _dominant_hold(state)
                _last_holds[component] = {
                    "mode": "estop", "reason": held.get("reason"),
                    "expires_at": held.get("expires_at"),
                    "logged_at": datetime.now(UTC).isoformat(),
                }
        if first:
            hold = _dominant_hold(state)
            suffix = f" (reason: {hold.get('reason')})" if hold.get("reason") else ""
            until = f" [auto-resumes {hold.get('expires_at')}]" if hold.get("expires_at") else ""
            logger.info(
                "%s dispatch paused by global emergency stop x%d — no exemptions%s%s; "
                "holders: %s — release yours with `hermes resume --handle <handle>` (%s)",
                component, state.counts[MODE_ESTOP], suffix, until,
                ", ".join(state.owners), sentinel_path(),
            )
        return True
    with _log_lock:
        first = component not in _lockdown_logged
        _lockdown_logged.add(component)
        _logged_components.discard(component)
        if first:
            held = _dominant_hold(state)
            _last_holds[component] = {
                "mode": "lockdown", "reason": held.get("reason"),
                "expires_at": held.get("expires_at"),
                "logged_at": datetime.now(UTC).isoformat(),
            }
    if first:
        lanes = ", ".join(sorted(state.allow_profiles)) or "(none)"
        logger.info(
            "%s runs SCOPED by lockdown x%d — lanes admitted: %s; every other lane's work is "
            "held per card/job, not silently skipped. Release with `hermes resume --handle "
            "<handle>` (%s)",
            component, state.counts[MODE_LOCKDOWN], lanes, sentinel_path(),
        )
    return False
