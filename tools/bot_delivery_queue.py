"""Durable receiver-side delivery queue for peer sends (spec §2).

A busy target's peer DM is *accepted* and queued here (``result: "receipt"``);
the **receiver** -- never the sender -- waits for a free turn slot and retries
with backoff. Nothing in this module blocks a sender for more than
``receipt_after_seconds``.

Storage mirrors the atomic-write / rename-claim idiom of
``tools.bot_live_delivery``: a per-home ``_FileLock`` plus
``tempfile`` + ``fsync`` + ``os.replace`` writes, so a crash can never strand a
half-written record.

Layout (``<home>/runtime/bot_delivery``)::

    queue/<delivery_id>.json      status queued | running
    claimed/<delivery_id>.json    atomically renamed here before the turn
    settled/<delivery_id>.json    immutable audit trail

Record lifecycle is ENUM B (7 values); the sender-visible result is ENUM A
(5 values). ``attempts`` counts turn executions only -- a contended lease
increments ``reoffer_count`` instead, so contention never consumes an attempt.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from hermes_cli.active_sessions import _FileLock

logger = logging.getLogger(__name__)

DELIVERY_DIR_NAME = "bot_delivery"
QUEUE_DIR = "queue"
CLAIMED_DIR = "claimed"
SETTLED_DIR = "settled"

# --- ENUM B: durable record lifecycle (7 values, no "target_busy").
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DELIVERED = "delivered"
STATUS_FAILED = "failed"
STATUS_EXPIRED = "expired"
STATUS_CANCELLED = "cancelled"
STATUS_AMBIGUOUS = "ambiguous"

STATUSES = (
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_DELIVERED,
    STATUS_FAILED,
    STATUS_EXPIRED,
    STATUS_CANCELLED,
    STATUS_AMBIGUOUS,
)

TERMINAL_STATUSES = frozenset(
    {
        STATUS_DELIVERED,
        STATUS_FAILED,
        STATUS_EXPIRED,
        STATUS_CANCELLED,
        STATUS_AMBIGUOUS,
    }
)

# --- ENUM A: sender-visible result (5 values).
RESULT_DELIVERED = "delivered"
RESULT_RECEIPT = "receipt"
RESULT_FAILED = "failed"
RESULT_REFUSED = "refused"
RESULT_UNKNOWN = "unknown"

RESULTS = (
    RESULT_DELIVERED,
    RESULT_RECEIPT,
    RESULT_FAILED,
    RESULT_REFUSED,
    RESULT_UNKNOWN,
)

OBJECT_NAME = "hermes.peer.send_result"

#: Envelope field order (§1.2/§1.3) -- exactly 24 fields, ``object`` first.
#: ``holder`` (the call-time holder observation, §2) is appended LAST so every field
#: before it keeps its index and "no field order, no extras" stays true by construction.
ENVELOPE_FIELDS = (
    "object",
    "result",
    "status",
    "status_detail",
    "send_id",
    "delivery_id",
    "idempotency_key",
    "replayed",
    "peer",
    "profile",
    "session_id",
    "queue_position",
    "attempts",
    "queued_seconds",
    "waited_seconds",
    "busy",
    "retryable",
    "retry_after_seconds",
    "reply",
    "error",
    "reason",
    "detail",
    "at",
    "holder",
)

#: Durable queue-record field order (§2.3) -- exactly 20 fields.
RECORD_FIELDS = (
    "delivery_id",
    "idempotency_key",
    "fingerprint",
    "sender_profile",
    "target_profile",
    "target_session_id",
    "message",
    "status",
    "status_detail",
    "attempts",
    "reoffer_count",
    "queue_position",
    "created_at",
    "updated_at",
    "claimed_at",
    "attempts_log",
    "reply",
    "error",
    "reason",
    "sequence",
)

# --- exact user-facing strings (§3.4); asserted verbatim by the tests.
DETAIL_RECEIPT = "accepted and queued — do NOT resend; receipt is retained"
#: The acceptance notice for a delivery queued behind a full AGENT-WORK cap (t_376b3fbd):
#: names the cause, so the sender does not read a saturated gateway as a busy target.
DETAIL_ADMISSION_CAP = (
    "accepted and queued — the gateway's agent-work concurrency cap is full; "
    "the turn runs when a slot frees. Do NOT resend; receipt is retained"
)
DETAIL_HELD_LIVE = (
    "Delivery remains pending or its outcome is unknown. "
    "Do not resend; receipt is retained."
)
STATUS_DETAIL_TARGET_BUSY = "target_busy: turn slot held by another turn; delivery queued"
STATUS_DETAIL_LIVE_OWNER = "live_owner_present"
STATUS_DETAIL_LEASE_CONTENDED = "lease_contended"
#: A claim recovered as a LOST LEASE, dead-lettered: the turn that took the claim
#: never ended it, ``2 x`` the queue TTL has passed with no live process holding the
#: seat, and by then the record's own delivery deadline is gone too -- so it is
#: settled ``failed`` and its sender told, never re-offered.
STATUS_DETAIL_LEASE_DEAD_LETTER = (
    "lease_lost: the claim outlived 2x the queue TTL with no live holder; not delivered"
)
STATUS_DETAIL_QUEUE_FULL = "queue_full"
#: A queued delivery whose target slot is FREE -- a different cause from
#: TARGET_BUSY, and the one a receipt must not misreport (VERIFICATION D2).
STATUS_DETAIL_BACKLOG = (
    "target_free: turn slot free, delivery queued behind earlier deliveries"
)
#: A delivery ACCEPTED while the gateway's AGENT-WORK cap is full (t_376b3fbd): the turn
#: waits for a concurrency slot, not for another turn to release the (free) target slot.
#: A third cause, distinct from both TARGET_BUSY and BACKLOG: the receipt must not claim
#: either, and the record is finished by the drainer, not by a resend.
STATUS_DETAIL_ADMISSION_CAP = (
    "admission_cap: gateway agent-work concurrency cap reached; "
    "delivery queued for the drainer"
)

#: Which cap :func:`check_capacity`/:func:`admit` refused on (VERIFICATION D6).
LIMIT_PER_PROFILE = "per_profile"
LIMIT_PER_SENDER = "per_sender"

REASON_QUEUED_EXPIRED = "queued_expired"
#: A claim whose lease ran out: the turn it was taken for never
#: settled it and no live process holds the seat. Terminal only -- a lost lease is
#: past its delivery deadline by construction, so it is dead-lettered under this
#: reason, never re-offered.
REASON_LEASE_LOST = "lease_lost"

#: An over-age record whose expiry found it NEVER OFFERED to a turn (``attempts == 0``).
#: t_43f11055 (b): such a record was dead-lettered by the expiry gate while the DRAIN
#: gate was deferring it -- 112 of the fleet's 116 expired records carry attempts=0.
#: Calling that ``queued_expired`` blames the lane's queue for the drain's own
#: deferral; this reason is the fact the strand census counts, and the one an expiry
#: files no notice for (c), so an unoffered expiry can never feed a notice back into
#: the same busy spool.
REASON_NEVER_OFFERED = "never_offered"

#: `retryable(envelope)` is derived from the *envelope reason* only. It is
#: unrelated to ``retry_action()`` (agent auto-retry) -- never derive one from
#: the other.
RETRYABLE_REASONS = frozenset({"queue_full", "runtime_offline"})

QUEUE_FULL_RETRY_AFTER_SECONDS = 60.0

_DELIVERY_ID_RE = re.compile(r"[0-9a-f]{32,64}")

#: Spec §2.4 defaults. Read live from ``bot_mode`` config via :func:`_cfg`.
_DEFAULTS: dict[str, float] = {
    "receipt_after_seconds": 15,
    "delivery_queue_ttl_seconds": 1800,
    "delivery_queue_max_per_profile": 32,
    "delivery_queue_max_per_sender": 8,
    "delivery_retry_base_seconds": 2,
    "delivery_sweep_seconds": 30,
    "delivery_max_turn_attempts": 3,
    "delivery_probe_base_seconds": 0.5,
    "delivery_probe_max_seconds": 2.0,
    "delivery_probe_jitter": 0.2,
    "lease_probe_seconds": 2,
    "dedup_window_seconds": 900,
}


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------
def _cfg(key: str, *, loader: str = "load_config_readonly") -> Any:
    """Read one ``bot_mode`` setting, falling back to the spec default."""
    value = None
    try:  # pragma: no cover - exercised through the accessors
        import hermes_cli.config as _config

        getter = getattr(_config, loader, None) or _config.load_config
        cfg = getter() or {}
        value = (cfg.get("bot_mode") or {}).get(key)
    except Exception:
        value = None
    if value is None:
        return _DEFAULTS[key]
    return value


def receipt_after_seconds() -> float:
    return float(_cfg("receipt_after_seconds"))


def queue_ttl_seconds() -> float:
    return float(_cfg("delivery_queue_ttl_seconds"))


def max_per_profile() -> int:
    return int(_cfg("delivery_queue_max_per_profile"))


def max_per_sender() -> int:
    return int(_cfg("delivery_queue_max_per_sender"))


def max_turn_attempts() -> int:
    return int(_cfg("delivery_max_turn_attempts"))


def sweep_seconds() -> float:
    return float(_cfg("delivery_sweep_seconds"))


def retry_base_seconds() -> float:
    return float(_cfg("delivery_retry_base_seconds"))


def lease_probe_seconds() -> float:
    return float(_cfg("lease_probe_seconds"))


# --------------------------------------------------------------------------
# backoff curves (§2.6)
# --------------------------------------------------------------------------
def probe_delay(n: int, *, rng: Any = random) -> float:
    """Lease-probe loop delay: ``uniform(0.8, 1.2) * min(2.0, 0.5 * 2**n)``."""
    base = float(_cfg("delivery_probe_base_seconds"))
    cap = float(_cfg("delivery_probe_max_seconds"))
    jitter = float(_cfg("delivery_probe_jitter"))
    return rng.uniform(1.0 - jitter, 1.0 + jitter) * min(cap, base * (2 ** n))


def drainer_delay(n: int, *, rng: Any = random) -> float:
    """Queue-drain loop delay: ``uniform(0.5, 1.0) * min(30, 2 * 2**n)``."""
    base = float(_cfg("delivery_retry_base_seconds"))
    cap = float(_cfg("delivery_sweep_seconds"))
    return rng.uniform(0.5, 1.0) * min(cap, base * (2 ** n))


# --------------------------------------------------------------------------
# observed turn-slot state (spec 3.1; VERIFICATION D2/D7)
# --------------------------------------------------------------------------
def _log(event: str, record: dict[str, Any] | None = None, **fields: Any) -> None:
    """One log line per delivery decision.

    The drain is a background path: without these, production has no way to
    tell an accepted-but-waiting delivery from a dropped one (VERIFICATION D5).
    """
    parts = []
    if record is not None:
        parts.append(f"delivery_id={record.get('delivery_id')}")
        for key_ in ("status", "target_profile", "sender_profile"):
            if record.get(key_):
                parts.append(f"{key_}={record.get(key_)}")
    parts.extend(f"{key_}={value}" for key_, value in sorted(fields.items()))
    logger.info("bot_delivery %s %s", event, " ".join(parts))


def busy_from_detail(record: dict[str, Any]) -> bool:
    """Whether the record's own ``status_detail`` says the slot is held.

    Deriving ``busy`` from the same field the envelope prints is what stops a
    replayed envelope from contradicting its own cause (VERIFICATION D7).

    A live-owner hold is a HELD slot too (P12): the target's Bot Chat is owned by
    another surface, so the receipt for it must report ``busy: true`` — exactly as
    the target_busy receipt does (VERIFICATION D2).
    """
    return record.get("status_detail") in (STATUS_DETAIL_TARGET_BUSY, STATUS_DETAIL_LIVE_OWNER)


def slot_held(home: str | os.PathLike[str], profile: str,
              session: str | None = None) -> bool:
    """Whether the ``(profile, session)`` turn slot is currently held by another turn.

    The lock is the durable relay lock, so this sees a holder that is not a
    queue consumer at all -- a desktop or card session mid-turn holds the same
    slot. Imported lazily because :mod:`tools.bot_relay` imports this module.
    Never raises: an unreadable lock reads as free (a probe failure must not
    expire a delivery, and it cannot authorise a claim either).

    ``session`` is the transcript the delivery is bound for — a queued DM always
    targets the profile's Bot Chat — so the probe asks about THAT transcript, not
    the whole seat (``t_47082c1d``).
    """
    try:
        from tools.bot_mode_probe import _hermes_root
        from tools.bot_relay import BOT_CHAT_SESSION_KEY, TurnBusyError, acquire_delivery_turn_lock

        key = BOT_CHAT_SESSION_KEY if session is None else session
        try:
            with acquire_delivery_turn_lock(_hermes_root(Path(home)), profile, key):
                return False
        except TurnBusyError:
            return True
    except Exception:  # pragma: no cover - defensive
        return False


def observed_detail(home: str | os.PathLike[str], profile: str) -> str:
    """The honest ``status_detail`` for a queued delivery, observed now."""
    if slot_held(home, profile):
        return STATUS_DETAIL_TARGET_BUSY
    return STATUS_DETAIL_BACKLOG


# --------------------------------------------------------------------------
# the AGENT-WORK cap, as expiry must see it (t_43f11055 a)
# --------------------------------------------------------------------------
#: The drain defers on the agent-work cap (the same gate the 429 asks) and on a held
#: slot; expiry used to ask ONLY the slot. So while the cap was full the drain offered
#: nothing, queue age kept accruing, and the same tick's expiry dead-lettered records
#: with ``attempts=0`` -- 112 live records, the fleet's whole expired set.
#:
#: The cap is process state (in-flight turns), so the process that owns it registers a
#: probe here ONCE; every expiry caller -- the 30s gateway sweep AND the hourly chore --
#: then asks the same gate the drain asks instead of a per-caller guess.
_AGENT_CAP_PROBE: Any = None


def register_agent_cap_probe(probe: Any) -> None:
    """Register (or clear, with ``None``) this process's view of the agent-work cap."""
    global _AGENT_CAP_PROBE
    _AGENT_CAP_PROBE = probe


def agent_cap_has_room(probe: Any = None) -> bool:
    """Whether the AGENT-WORK cap allows one more turn to START.

    ``None`` (no probe registered and none handed in) is UNKNOWN, and unknown is not
    full: a CLI or a test has no view of the cap and must not be handed a fleet-wide
    "full" it cannot measure. Only the process that owns the cap can answer, and it
    registers :func:`register_agent_cap_probe`. Never raises -- an unreadable cap
    must not dead-letter a record, exactly as an unreadable lock reads as free.
    """
    fn = _AGENT_CAP_PROBE if probe is None else probe
    if not callable(fn):
        return True
    try:
        return bool(fn())
    except Exception:  # a probe failure is not an expiry verdict
        return True


# --------------------------------------------------------------------------
# the claim LEASE
# --------------------------------------------------------------------------
#: A claim is a lease with an EXPIRY: ``2 x`` the queue TTL. Past it, with no live
#: holder naming the seat, the claim is unauthorised -- the freeze it bought is void
#: (``queued_seconds``), the census counts it as ``claimed_stuck``, and the sweep
#: recovers it: dead-lettered and its sender told (never re-offered, since a lost
#: lease is past its delivery deadline by construction). Not a config key: the
#: ruling derives it from the TTL so the two can never drift apart.
LEASE_TTL_MULTIPLIER = 2.0


def lease_expiry_seconds() -> float:
    """A claim's own expiry: ``LEASE_TTL_MULTIPLIER x`` the queue TTL (3600s at 1800s)."""
    return LEASE_TTL_MULTIPLIER * queue_ttl_seconds()


def claim_age_seconds(record: dict[str, Any], *, now_ns: int | None = None) -> float | None:
    """Seconds since the claim was taken; ``None`` when the record carries no claim.

    ``claimed_at`` is the written fact; an older/spooled record without it falls back
    to the open attempt's ``started_at``, which is the same moment by construction.
    """
    moments = time.time_ns() if now_ns is None else int(now_ns)
    claimed = record.get("claimed_at")
    if not claimed:
        entry = _open_attempt(record) or {}
        claimed = entry.get("started_at")
    try:
        started = int(claimed or 0)
    except (TypeError, ValueError):
        return None
    if started <= 0:
        return None
    return max(0.0, (moments - started) / 1e9)


def lease_holder(home: str | os.PathLike[str], profile: str,
                 session: str | None = None) -> dict[str, Any] | None:
    """The holder record naming the process that took ``profile``'s turn, or ``None``.

    The SAME observation the re-offer path already makes (``bot_relay``
    ``read_turn_lock_holder``): the lockfile's own record, with its pid liveness
    PROBED, never trusted. Imported lazily because :mod:`tools.bot_relay` imports this
    module, and never raising: an unreadable lockfile is "nobody named", not an error.
    ``session`` defaults to the Bot Chat transcript this queue delivers into (``t_47082c1d``).
    """
    try:
        from tools.bot_mode_probe import _hermes_root
        from tools.bot_relay import BOT_CHAT_SESSION_KEY, read_turn_lock_holder

        return read_turn_lock_holder(_hermes_root(Path(home)), profile,
                                     BOT_CHAT_SESSION_KEY if session is None else session)
    except Exception:  # pragma: no cover - defensive
        return None


def lease_holder_alive(home: str | os.PathLike[str], profile: str) -> bool:
    """Whether a LIVE process is named as the seat's turn holder.

    Only an explicit ``alive: true`` counts. An absent, torn or pid-less record names
    nobody, and an unprobeable pid is not evidence of a turn: none of them defends a
    claim, so the lease rule treats all of them as "no live holder". That direction is
    deliberate -- a stuck record is the defect this exists to end, while a genuinely
    running turn publishes its own holder record.
    """
    holder = lease_holder(home, profile)
    return bool(holder) and holder.get("alive") is True


def lease_lost(record: dict[str, Any], *, home: str | os.PathLike[str],
               now_ns: int | None = None) -> bool:
    """Whether an open attempt is an UNAUTHORISED claim (a lost lease).

    All three must hold: the record is not terminal, it carries an attempt with no
    ``ended_at`` (a claim nobody closed), that claim is older than
    :func:`lease_expiry_seconds`, and no live process holds the target's turn. A live
    holder keeps the record exactly as it is, however old (ruling (c)); an attempt
    under the expiry is never touched either.
    """
    if record.get("status") in TERMINAL_STATUSES:
        return False
    if _open_attempt(record) is None:
        return False
    age = claim_age_seconds(record, now_ns=now_ns)
    if age is None or age <= lease_expiry_seconds():
        return False
    return not lease_holder_alive(home, record.get("target_profile") or "")


# --------------------------------------------------------------------------
# user-facing strings
# --------------------------------------------------------------------------
def expired_detail(ttl_seconds: float | None = None) -> str:
    ttl = queue_ttl_seconds() if ttl_seconds is None else ttl_seconds
    return f"queued {int(ttl)}s without a free turn slot; not delivered"


def never_offered_detail(ttl_seconds: float | None = None) -> str:
    """The honest detail for an expiry that found the record never offered to a turn."""
    ttl = queue_ttl_seconds() if ttl_seconds is None else ttl_seconds
    return (
        f"queued {int(ttl)}s and expired without ever being offered to a turn; "
        f"the drain never offered it"
    )


def expiry_classification(
    record: dict[str, Any], ttl_seconds: float | None = None
) -> tuple[str, str]:
    """The ONE classifier for an over-age record: ``(reason, status_detail)``.

    Both expiry paths ask this -- :func:`mark_expired` and the sweep -- so a record
    that was accepted and never OFFERED to a turn (``attempts == 0``) is
    ``never_offered`` on either path, never ``queued_expired`` (t_43f11055 b).
    """
    if int(record.get("attempts") or 0) <= 0:
        return REASON_NEVER_OFFERED, never_offered_detail(ttl_seconds)
    return REASON_QUEUED_EXPIRED, expired_detail(ttl_seconds)


def queue_full_detail(
    per_profile: int | None = None,
    retry_after: float | None = None,
    *,
    limit_kind: str = LIMIT_PER_PROFILE,
    limit: int | None = None,
) -> str:
    """Name the cap that actually refused the send (VERIFICATION D6).

    A send refused by the per-sender cap was previously reported as
    ``queue full (32 per profile)`` -- an operator would raise the wrong limit.
    """
    if limit is not None:
        cap = int(limit)
    elif per_profile is None:
        cap = max_per_profile()
    else:
        cap = int(per_profile)
    after = QUEUE_FULL_RETRY_AFTER_SECONDS if retry_after is None else retry_after
    scope = "per profile" if limit_kind == LIMIT_PER_PROFILE else "pending from this sender"
    return (
        f"queue full ({cap} {scope}); no reservation was taken — "
        f"safe to retry with the same idempotency_key after {int(after)}s"
    )


def unknown_detail(idempotency_key: str | None = None) -> str:
    key = idempotency_key or "<idempotency_key>"
    return f"outcome unknown; a resend must reuse idempotency_key {key}"


def build_notification_text(record: dict[str, Any]) -> str:
    """Held-delivery notice for the target's live session (§3.5)."""
    if record.get("status_detail") == STATUS_DETAIL_LIVE_OWNER:
        return DETAIL_HELD_LIVE
    if record.get("status_detail") == STATUS_DETAIL_ADMISSION_CAP:
        return DETAIL_ADMISSION_CAP
    status = record.get("status")
    if status == STATUS_EXPIRED:
        # An expiry prints the CLASSIFICATION it settled with -- ``never_offered`` for a
        # record no turn was ever offered (t_43f11055 b) -- never a blanket "no free slot".
        return record.get("status_detail") or expired_detail()
    if status == STATUS_AMBIGUOUS:
        return unknown_detail(record.get("idempotency_key"))
    if record.get("reason") == "queue_full":
        return queue_full_detail()
    return DETAIL_RECEIPT


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------
class QueueFullError(RuntimeError):
    """Raised by :func:`admit` when the per-profile or per-sender cap is met.

    ``QUEUE_FULL`` is the only new failure reason; there is no ``target_busy``
    reason anywhere in the vocabulary.
    """

    reason = "queue_full"

    def __init__(
        self,
        profile: str,
        *,
        limit_kind: str = LIMIT_PER_PROFILE,
        limit: int | None = None,
        message: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.profile = profile
        self.limit_kind = limit_kind
        self.limit = limit
        self.retry_after_seconds = (
            QUEUE_FULL_RETRY_AFTER_SECONDS
            if retry_after_seconds is None
            else float(retry_after_seconds)
        )
        super().__init__(
            message or queue_full_detail(limit_kind=limit_kind, limit=limit)
        )


# --------------------------------------------------------------------------
# validation + paths
# --------------------------------------------------------------------------
def validate_idempotency_key(key: Any) -> str:
    """Normalize and validate an idempotency key (mirrors the CLI validator)."""
    text = ("" if key is None else str(key)).strip()
    if not text or len(text) > 255 or re.search(r"[\r\n\x00]", text):
        raise ValueError("idempotency key must be 1-255 characters without control newlines")
    return text


def validate_delivery_id(delivery_id: Any) -> str:
    text = "" if delivery_id is None else str(delivery_id)
    if not _DELIVERY_ID_RE.fullmatch(text):
        raise ValueError("delivery_id must be 32-64 lowercase hex characters")
    return text


def delivery_root(home: str | os.PathLike[str]) -> Path:
    return Path(home).resolve() / "runtime" / DELIVERY_DIR_NAME


def has_spool(home: str | os.PathLike[str]) -> bool:
    """Whether this home ALREADY has a delivery spool (never creates one).

    The drainer walks every profile home on the install, most of which never
    carried a delivery; asking first keeps a sweep from planting an empty spool --
    and chmod'ing it -- in a profile that does not use Bot Mode.
    """
    return delivery_root(home).is_dir()


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:  # pragma: no cover - platform specific
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - platform specific
        pass
    finally:
        os.close(fd)


@contextmanager
def _locked(home: str | os.PathLike[str]) -> Iterator[Path]:
    root = delivery_root(home)
    for sub in (QUEUE_DIR, CLAIMED_DIR, SETTLED_DIR):
        (root / sub).mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(root, 0o700)
    except OSError:  # pragma: no cover
        pass
    lock_path = root / ".lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    with _FileLock(lock_path):
        yield root


def _write(path: Path, record: dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def _read(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None


def _list(root: Path, sub: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((root / sub).glob("*.json")):
        record = _read(path)
        if isinstance(record, dict):
            records.append(record)
    return records


def _find(root: Path, delivery_id: str) -> dict[str, Any] | None:
    for sub in (QUEUE_DIR, CLAIMED_DIR, SETTLED_DIR):
        record = _read(root / sub / f"{delivery_id}.json")
        if record is not None:
            return record
    return None


def _high_water(root: Path) -> int:
    best = 0
    for sub in (QUEUE_DIR, CLAIMED_DIR, SETTLED_DIR):
        for record in _list(root, sub):
            marker = record.get("sequence", record.get("created_at", 0))
            try:
                marker = int(marker)
            except (TypeError, ValueError):
                marker = 0
            best = max(best, marker)
    return best


def _pending(root: Path, *, target_profile: str | None = None,
             sender_profile: str | None = None) -> int:
    count = 0
    for sub in (QUEUE_DIR, CLAIMED_DIR):
        for record in _list(root, sub):
            if record.get("status") in TERMINAL_STATUSES:
                continue
            if target_profile is not None and record.get("target_profile") != target_profile:
                continue
            if sender_profile is not None and record.get("sender_profile") != sender_profile:
                continue
            count += 1
    return count


# --------------------------------------------------------------------------
# timestamps / derived fields
# --------------------------------------------------------------------------
def queued_seconds(record: dict[str, Any], *, now_ns: int | None = None) -> float:
    """Seconds the record spent *waiting* (sum of its queued intervals).

    An open attempt freezes the reading (a turn is running right now: no further
    queued time accrues). That freeze is a CLAIM on the record's time, and a claim
    cannot outlive its LEASE: past :func:`lease_expiry_seconds`
    the frozen reading is void and the waiting accrues again. The holder is not
    consulted here -- a claim that old is no evidence of a turn whichever process
    names it -- while the destructive recovery (and the ``claimed_stuck`` counter)
    still require that no live holder stands behind it (:func:`lease_lost`).
    """
    now_ns = time.time_ns() if now_ns is None else int(now_ns)
    cursor = int(record.get("created_at") or 0)
    total = 0
    for entry in sorted(
        record.get("attempts_log") or [], key=lambda e: (e.get("attempt") or 0)
    ):
        started = entry.get("started_at")
        if started and started > cursor:
            total += started - cursor
        ended = entry.get("ended_at")
        if ended is None:
            if (claim_age_seconds(record, now_ns=now_ns) or 0.0) > lease_expiry_seconds():
                total += max(0, now_ns - int(started or cursor))
            # else: a turn is running within its lease -- no further queued time accrues
            return total / 1e9
        cursor = max(cursor, ended)
    if record.get("status") not in TERMINAL_STATUSES:
        total += max(0, now_ns - cursor)
    return total / 1e9


def _queue_position(root: Path, record: dict[str, Any]) -> int | None:
    if record.get("status") != STATUS_QUEUED:
        return None
    sequence = record.get("sequence", 0)
    ahead = 0
    for other in _list(root, QUEUE_DIR):
        if other.get("target_profile") != record.get("target_profile"):
            continue
        if other.get("status") != STATUS_QUEUED:
            continue
        if other.get("delivery_id") == record.get("delivery_id"):
            continue
        if other.get("sequence", 0) < sequence:
            ahead += 1
    return ahead + 1


def _result_for_status(status: str) -> str:
    if status == STATUS_DELIVERED:
        return RESULT_DELIVERED
    if status in (STATUS_FAILED, STATUS_EXPIRED, STATUS_CANCELLED):
        return RESULT_FAILED
    if status == STATUS_AMBIGUOUS:
        return RESULT_UNKNOWN
    return RESULT_RECEIPT  # queued | running


def _open_attempt(record: dict[str, Any]) -> dict[str, Any] | None:
    for entry in reversed(record.get("attempts_log") or []):
        if entry.get("ended_at") is None:
            return entry
    return None


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------
def admit(
    home: str | os.PathLike[str],
    *,
    sender_profile: str,
    target_profile: str,
    target_session_id: str | None,
    idempotency_key: str,
    fingerprint: str,
    delivery_id: str,
    message: str,
    sequence_hint: int | None = None,
    status_detail: str | None = None,
    now_ns: int | None = None,
) -> dict[str, Any]:
    """Admit one delivery into the queue, or return the existing record.

    Idempotent on ``delivery_id``: a repeat admit returns the stored record and
    writes nothing (one record, one sequence). Raises :class:`QueueFullError`
    when either capacity cap would be exceeded.
    """
    key = validate_idempotency_key(idempotency_key)
    key_id = validate_delivery_id(delivery_id)
    if not isinstance(message, str):
        raise ValueError("message must be a string")
    now_ns = time.time_ns() if now_ns is None else int(now_ns)

    with _locked(home) as root:
        existing = _find(root, key_id)
        if existing is not None:
            return dict(existing)

        if _pending(root, target_profile=target_profile) >= max_per_profile():
            raise QueueFullError(
                target_profile,
                limit_kind=LIMIT_PER_PROFILE,
                limit=max_per_profile(),
            )
        if (
            _pending(root, target_profile=target_profile, sender_profile=sender_profile)
            >= max_per_sender()
        ):
            raise QueueFullError(
                target_profile,
                limit_kind=LIMIT_PER_SENDER,
                limit=max_per_sender(),
            )

        sequence = max(int(sequence_hint or 0), _high_water(root) + 1)
        record: dict[str, Any] = {
            "delivery_id": key_id,
            "idempotency_key": key,
            "fingerprint": fingerprint,
            "sender_profile": sender_profile,
            "target_profile": target_profile,
            "target_session_id": target_session_id,
            "message": message,
            "status": STATUS_QUEUED,
            "status_detail": status_detail or STATUS_DETAIL_TARGET_BUSY,
            "attempts": 0,
            "reoffer_count": 0,
            "queue_position": 0,
            "created_at": now_ns,
            "updated_at": now_ns,
            "claimed_at": None,
            "attempts_log": [],
            "reply": None,
            "error": None,
            "reason": None,
            "sequence": sequence,
        }
        record["queue_position"] = _queue_position(root, record) or 1
        _write(root / QUEUE_DIR / f"{key_id}.json", record)
        _log("admitted", record, queue_position=record["queue_position"])
        return dict(record)


def claim_next(
    home: str | os.PathLike[str],
    *,
    target_profile: str,
    lease_ok: bool = False,
    now_ns: int | None = None,
) -> dict[str, Any] | None:
    """Claim the oldest queued record for ``target_profile`` (FIFO).

    With ``lease_ok=False`` (the lease probe did not grant a turn) the oldest
    record is left ``queued`` and only ``reoffer_count`` is bumped -- attempts
    are never consumed by contention. Returns ``None`` in that case and when
    the queue is empty for the profile.
    """
    now_ns = time.time_ns() if now_ns is None else int(now_ns)
    with _locked(home) as root:
        queued = [
            record
            for record in _list(root, QUEUE_DIR)
            if record.get("target_profile") == target_profile
            and record.get("status") == STATUS_QUEUED
        ]
        if not queued:
            return None
        record = min(
            queued,
            key=lambda r: (r.get("sequence", r.get("created_at", 0)), r["delivery_id"]),
        )

        if not lease_ok:
            updated = dict(record)
            updated["reoffer_count"] = int(updated.get("reoffer_count", 0)) + 1
            updated["updated_at"] = now_ns
            updated["status_detail"] = STATUS_DETAIL_LEASE_CONTENDED
            _write(root / QUEUE_DIR / f"{record['delivery_id']}.json", updated)
            _log("reoffer", updated, reoffer_count=updated["reoffer_count"])
            return None

        updated = dict(record)
        updated["status"] = STATUS_RUNNING
        updated["attempts"] = int(updated.get("attempts", 0)) + 1
        updated["claimed_at"] = now_ns
        updated["updated_at"] = now_ns
        updated["queue_position"] = None
        log = list(updated.get("attempts_log") or [])
        log.append(
            {
                "attempt": updated["attempts"],
                "started_at": now_ns,
                "ended_at": None,
                "status": STATUS_RUNNING,
                "reason": None,
            }
        )
        updated["attempts_log"] = log
        os.replace(
            root / QUEUE_DIR / f"{record['delivery_id']}.json",
            root / CLAIMED_DIR / f"{record['delivery_id']}.json",
        )
        _write(root / CLAIMED_DIR / f"{record['delivery_id']}.json", updated)
        _fsync_dir(root / QUEUE_DIR)
        _log("claimed", updated, attempt=updated["attempts"])
        return dict(updated)


def requeue(
    home: str | os.PathLike[str], delivery_id: str, *, now_ns: int | None = None
) -> dict[str, Any]:
    """Return a claimed record to ``queued``.

    ``attempts`` is unchanged; ``reoffer_count`` increments. Already-queued
    records are returned untouched so the call is idempotent.
    """
    key_id = validate_delivery_id(delivery_id)
    now_ns = time.time_ns() if now_ns is None else int(now_ns)
    with _locked(home) as root:
        queued_path = root / QUEUE_DIR / f"{key_id}.json"
        existing = _read(queued_path)
        if existing is not None:
            return dict(existing)

        claimed_path = root / CLAIMED_DIR / f"{key_id}.json"
        record = _read(claimed_path)
        if record is None:
            raise FileNotFoundError(f"no claimed record for {key_id}")

        updated = dict(record)
        updated["status"] = STATUS_QUEUED
        updated["reoffer_count"] = int(updated.get("reoffer_count", 0)) + 1
        updated["claimed_at"] = None
        updated["updated_at"] = now_ns
        updated["status_detail"] = STATUS_DETAIL_LEASE_CONTENDED
        entry = _open_attempt(updated)
        if entry is not None:
            entry["ended_at"] = now_ns
            entry["status"] = STATUS_QUEUED
            entry["reason"] = "requeued"
        updated["sequence"] = int(updated.get("sequence", 0))
        os.replace(claimed_path, queued_path)
        updated["queue_position"] = _queue_position(root, updated) or 1
        _write(queued_path, updated)
        _fsync_dir(root / CLAIMED_DIR)
        _log("requeued", updated, reoffer_count=updated["reoffer_count"])
        return dict(updated)


def requeue_unstarted(
    home: str | os.PathLike[str], delivery_id: str, *, now_ns: int | None = None
) -> dict[str, Any]:
    """Return a claimed record to ``queued`` without charging the attempt.

    The in-call lease-contention path (spec §2.7 step 2, §4.9): the turn was
    never paid for, so the claim's ``attempts += 1`` and its still-open
    ``attempts_log`` entry are rolled back -- contention must never consume an
    attempt. ``reoffer_count`` increments; already-queued records are untouched.
    """
    key_id = validate_delivery_id(delivery_id)
    now_ns = time.time_ns() if now_ns is None else int(now_ns)
    with _locked(home) as root:
        queued_path = root / QUEUE_DIR / f"{key_id}.json"
        existing = _read(queued_path)
        if existing is not None:
            return dict(existing)

        claimed_path = root / CLAIMED_DIR / f"{key_id}.json"
        record = _read(claimed_path)
        if record is None:
            raise FileNotFoundError(f"no claimed record for {key_id}")

        updated = dict(record)
        updated["attempts"] = max(0, int(updated.get("attempts", 0)) - 1)
        log = list(updated.get("attempts_log") or [])
        if log and log[-1].get("ended_at") is None:
            log.pop()
        updated["attempts_log"] = log
        updated["status"] = STATUS_QUEUED
        updated["reoffer_count"] = int(updated.get("reoffer_count", 0)) + 1
        updated["claimed_at"] = None
        updated["status_detail"] = STATUS_DETAIL_LEASE_CONTENDED
        updated["updated_at"] = now_ns
        updated["sequence"] = int(updated.get("sequence", 0))
        os.replace(claimed_path, queued_path)
        updated["queue_position"] = _queue_position(root, updated) or 1
        _write(queued_path, updated)
        _fsync_dir(root / CLAIMED_DIR)
        _log("requeued", updated, reoffer_count=updated["reoffer_count"])
        return dict(updated)


def settle(
    home: str | os.PathLike[str],
    delivery_id: str,
    *,
    status: str,
    reply: str | None = "",
    error: str | None = "",
    reason: str | None = "",
    status_detail: str | None = None,
    now_ns: int | None = None,
) -> dict[str, Any]:
    """Move a claimed record to ``settled/`` with a terminal status."""
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"status must be terminal, got {status!r}")
    key_id = validate_delivery_id(delivery_id)
    now_ns = time.time_ns() if now_ns is None else int(now_ns)
    with _locked(home) as root:
        settled_path = root / SETTLED_DIR / f"{key_id}.json"
        existing = _read(settled_path)
        if existing is not None:
            if existing.get("status") != status:
                raise ValueError(
                    f"{key_id} already settled as {existing.get('status')!r}"
                )
            return dict(existing)

        claimed_path = root / CLAIMED_DIR / f"{key_id}.json"
        record = _read(claimed_path)
        if record is None:
            raise FileNotFoundError(f"no claimed record for {key_id}")

        updated = dict(record)
        updated["status"] = status
        updated["updated_at"] = now_ns
        updated["status_detail"] = status_detail
        updated["queue_position"] = None
        if reply not in (None, ""):
            updated["reply"] = reply
        if error not in (None, ""):
            updated["error"] = error
        if reason not in (None, ""):
            updated["reason"] = reason
        entry = _open_attempt(updated)
        if entry is not None:
            entry["ended_at"] = now_ns
            entry["status"] = status
            entry["reason"] = reason or None
        os.replace(claimed_path, settled_path)
        _write(settled_path, updated)
        _fsync_dir(root / CLAIMED_DIR)
        _log("settled", updated, attempt=updated.get("attempts"))
        return dict(updated)


def mark_expired(
    home: str | os.PathLike[str], delivery_id: str, *, now_ns: int | None = None
) -> dict[str, Any]:
    """Settle an over-age ``queued`` record as ``expired``.

    The classification comes from :func:`expiry_classification`, the same one the
    sweep asks: a record this never offered to a turn expires as ``never_offered``
    (t_43f11055 b).
    """
    key_id = validate_delivery_id(delivery_id)
    now_ns = time.time_ns() if now_ns is None else int(now_ns)
    with _locked(home) as root:
        queued_path = root / QUEUE_DIR / f"{key_id}.json"
        record = _read(queued_path)
        if record is None:
            settled = _read(root / SETTLED_DIR / f"{key_id}.json")
            if settled is not None:
                return dict(settled)
            raise FileNotFoundError(f"no queued record for {key_id}")
        updated = dict(record)
        updated["status"] = STATUS_EXPIRED
        updated["reason"], updated["status_detail"] = expiry_classification(record)
        updated["updated_at"] = now_ns
        updated["queue_position"] = None
        os.replace(queued_path, root / SETTLED_DIR / f"{key_id}.json")
        _write(root / SETTLED_DIR / f"{key_id}.json", updated)
        _fsync_dir(root / QUEUE_DIR)
        return dict(updated)


def mark_contended(
    home: str | os.PathLike[str],
    delivery_id: str,
    *,
    status_detail: str | None = None,
    now_ns: int | None = None,
) -> dict[str, Any] | None:
    """Stamp an observed contended turn slot onto a still-queued delivery.

    Admission enqueues before it probes the turn lock, so the receipt's
    ``status_detail`` is only known once the synchronous window closes.
    Contention is never an attempt: ``attempts`` and ``reoffer_count`` are left
    alone. Returns the updated record, or ``None`` when it is no longer queued.
    """
    key_id = validate_delivery_id(delivery_id)
    now_ns = time.time_ns() if now_ns is None else int(now_ns)
    with _locked(home) as root:
        path = root / QUEUE_DIR / f"{key_id}.json"
        record = _read(path)
        if record is None:
            return None
        if record.get("status") != STATUS_QUEUED:
            return dict(record)
        record["status_detail"] = status_detail or STATUS_DETAIL_TARGET_BUSY
        record["updated_at"] = now_ns
        _write(path, record)
        return dict(record)


def reoffer_ambiguous(
    home: str | os.PathLike[str], delivery_id: str, *, now_ns: int | None = None
) -> dict[str, Any] | None:
    """Re-offer a settled ``ambiguous`` record, once, under the same delivery_id.

    The crash-mid-turn rule permits exactly ONE recovery replay, only under the
    same idempotency key, and never a second one. Returns the re-queued record, or
    ``None`` when the delivery is not re-offerable (already re-offered once, not
    ``ambiguous``, or unknown). ``attempts`` is incremented by the claim that
    follows, never by the re-offer itself.
    """
    key_id = validate_delivery_id(delivery_id)
    now_ns = time.time_ns() if now_ns is None else int(now_ns)
    with _locked(home) as root:
        settled_path = root / SETTLED_DIR / f"{key_id}.json"
        record = _read(settled_path)
        if record is None or record.get("status") != STATUS_AMBIGUOUS:
            return None
        if int(record.get("reoffer_count", 0)) >= 1:
            return None
        updated = dict(record)
        updated["status"] = STATUS_QUEUED
        updated["reoffer_count"] = int(record.get("reoffer_count", 0)) + 1
        updated["status_detail"] = STATUS_DETAIL_TARGET_BUSY
        updated["updated_at"] = now_ns
        entry = _open_attempt(updated)
        if entry is not None:
            entry["ended_at"] = now_ns
            entry["status"] = STATUS_QUEUED
            entry["reason"] = "recovery_replay"
        updated["queue_position"] = 0
        queued_path = root / QUEUE_DIR / f"{key_id}.json"
        _write(queued_path, updated)
        settled_path.unlink(missing_ok=True)
        _fsync_dir(root / SETTLED_DIR)
        updated["queue_position"] = _queue_position(root, updated) or 1
        _write(queued_path, updated)
        return dict(updated)


def read_record(
    home: str | os.PathLike[str], delivery_id: str
) -> dict[str, Any] | None:
    """Read a record from whichever lifecycle directory holds it."""
    key_id = validate_delivery_id(delivery_id)
    with _locked(home) as root:
        return _find(root, key_id)


def cleanup_bot_delivery_queue(max_age_hours: float | None = None) -> int:
    """Hourly housekeeping hook (spec 3.1 trigger 4): sweep EVERY lane's queue.

    ``max_age_hours`` exists only for signature parity with the other
    ``cleanup_*`` chores; the queue's own TTL governs expiry.

    A peer delivery is admitted into its TARGET lane's own home
    (``profiles/<lane>/runtime/bot_delivery``), so sweeping only the default home
    left a named lane's backlog with no expiry and no orphan-claim recovery at all
    (card t_8a7a47d7).
    """
    del max_age_hours
    try:
        from tools.bot_mode_probe import _default_home, _delivery_homes, _hermes_root

        root = _hermes_root(Path(_default_home()))
        return sum(
            sweep_delivery_queue(profile_home)
            for _name, profile_home in _delivery_homes(root)
        )
    except Exception:  # pragma: no cover - housekeeping must not raise
        logger.debug("bot_delivery sweep failed", exc_info=True)
        return 0


def _settle_lost_lease(root: Path, record: dict[str, Any], *, now_ns: int) -> dict[str, Any]:
    """Dead-letter a leaked claim as ``failed`` / ``lease_lost`` (caller holds the lock).

    The open attempt is CLOSED with the same reason: an attempt left open is the thing
    the census cannot see, so this recovery must not leave the very shape it repairs.
    """
    updated = dict(record)
    delivery_id = updated["delivery_id"]
    updated["status"] = STATUS_FAILED
    updated["reason"] = REASON_LEASE_LOST
    updated["status_detail"] = STATUS_DETAIL_LEASE_DEAD_LETTER
    updated["updated_at"] = now_ns
    updated["queue_position"] = None
    entry = _open_attempt(updated)
    if entry is not None:
        entry["ended_at"] = now_ns
        entry["status"] = STATUS_FAILED
        entry["reason"] = REASON_LEASE_LOST
    settled_path = root / SETTLED_DIR / f"{delivery_id}.json"
    os.replace(root / CLAIMED_DIR / f"{delivery_id}.json", settled_path)
    _write(settled_path, updated)
    _fsync_dir(root / CLAIMED_DIR)
    return dict(updated)


#: Sentinel for ``on_lease_lost``: by default the sweep FILES the sender's notice. A
#: delivery dead-lettered without a word to its sender is the silent failure this
#: repair exists to kill, so silence is opt-in (tests), never the default.
_NOTICE_BY_DEFAULT = object()


def sweep_delivery_queue(
    home: str | os.PathLike[str],
    *,
    now_ns: int | None = None,
    slot_held_fn: Any = None,
    agent_cap_has_room_fn: Any = None,
    expire: bool = True,
    on_expired: Any = None,
    on_lease_lost: Any = _NOTICE_BY_DEFAULT,
) -> int:
    """Recover orphaned claims and leaked leases, then (``expire=True``) expire over-age records.

    Runs hourly and on every 30s drainer tick (VERIFICATION D1 trigger 4).
    Returns the number of records it acted on.

    An over-age record is expired only while its target's turn slot is FREE **and**
    the AGENT-WORK cap has room -- the SAME two deferrals the drain applies before it
    will offer a record to a turn (t_43f11055 a). A target mid-turn (a desktop session
    can hold the slot for a whole lease wait) must not have its inbound delivery expire
    underneath it: the slot holder is exactly the turn that will drain it next. And a
    full cap is a FLEET-WIDE condition, not this record's outcome: while the drain is
    deferring on it, queue age must keep counting without dead-lettering what the drain
    will offer the moment the cap frees a place -- ``age_kept_slot_held`` /
    ``age_kept_agent_cap``.

    ``agent_cap_has_room_fn`` is how a caller hands in its view of the cap; omitted, the
    probe this process registered (:func:`register_agent_cap_probe`) answers, and a
    process with neither answers "room" -- unknown is not full, exactly as the drain
    treats an unreadable gate.

    Expiry is classified by :func:`expiry_classification`: a record NEVER offered to a
    turn (``attempts == 0``) expires as ``never_offered``, which is the fact the strand
    census counts and which files NO notice (t_43f11055 b/c).

    ``expire=False`` runs the recovery passes ONLY (orphaned claims + lost leases).
    The drainer takes that form before its drain and the full form after it (card
    t_8a7a47d7): a record that was never offered to a turn (``attempts == 0``) must
    get its first -- and on that tick, only -- chance to run before TTL can
    dead-letter it, or the backlog is reaped the same tick the drainer finally
    reaches the lane.

    A CLAIM IS A LEASE WITH AN EXPIRY: a record in ``claimed/``
    whose attempt never ended, whose claim is older than
    :func:`lease_expiry_seconds` (= ``2 x`` the queue TTL), and whose seat has no
    LIVE holder is a lost lease. It is dead-lettered ``failed`` / ``lease_lost``,
    its open attempt is closed, and its sender is told.

    A lost lease is PAST ITS OWN DELIVERY DEADLINE BY CONSTRUCTION: the lease is
    only lost once the claim is older than ``2 x`` TTL, so the waiting has accrued
    past the TTL (see :func:`queued_seconds`, which voids the freeze past the
    lease) and pass 2 would reap the record in this same tick anyway. So it is
    NEVER re-offered -- resurrecting it would deliver hours- or days-old peer work,
    and the truthful outcome is the terminal one.

    A record with a live holder, or a young claim, is untouched: a turn that is
    genuinely running keeps its record exactly as it is.

    ``on_expired`` / ``on_lease_lost`` are called once per record AFTER this home's
    lock is released, so a callback may write into another home (the sender's
    failure notice) without deadlocking this one. A callback that raises is logged
    and never aborts the sweep. ``on_lease_lost`` defaults to
    :func:`notice_lease_lost_to_sender`; pass ``None`` to suppress it.
    """
    now_ns = time.time_ns() if now_ns is None else int(now_ns)
    ttl = queue_ttl_seconds()
    held = slot_held if slot_held_fn is None else slot_held_fn
    cap_has_room = agent_cap_has_room(agent_cap_has_room_fn)
    actions = 0
    expired: list[dict[str, Any]] = []
    lost: list[dict[str, Any]] = []
    with _locked(home) as root:
        # 1. a claim renamed the file but crashed before rewriting it
        for record in _list(root, CLAIMED_DIR):
            if record.get("status") != STATUS_QUEUED:
                continue
            delivery_id = record["delivery_id"]
            os.replace(
                root / CLAIMED_DIR / f"{delivery_id}.json",
                root / QUEUE_DIR / f"{delivery_id}.json",
            )
            _write(root / QUEUE_DIR / f"{delivery_id}.json", record)
            actions += 1

        # 1b. a claim whose LEASE ran out: no lived attempt, no live holder.
        # Read AFTER pass 1, so a torn claim recovered above is never judged twice.
        for record in _list(root, CLAIMED_DIR):
            if not lease_lost(record, home=home, now_ns=now_ns):
                continue
            # A lost lease is PAST ITS OWN DEADLINE BY CONSTRUCTION (lease = 2 x TTL,
            # and the lease is only lost once the waiting has accrued past the TTL),
            # so resurrection is never on the table: delivering hours-old peer work,
            # or re-offering it for pass 2 to reap in this same tick with a notice
            # saying it was never read, are both wrong. Dead-letter it, and tell the
            # sender -- the terminal reason is the truthful one.
            updated = _settle_lost_lease(root, record, now_ns=now_ns)
            _log("lease_lost", updated, action="failed", reason=REASON_LEASE_LOST,
                 reoffer_count=int(record.get("reoffer_count", 0)),
                 claim_age_seconds=round(claim_age_seconds(record, now_ns=now_ns) or 0, 1))
            lost.append(updated)
            actions += 1

        # 2. expire records that waited past the TTL
        if expire and ttl > 0:
            for record in _list(root, QUEUE_DIR):
                if record.get("status") != STATUS_QUEUED:
                    continue
                if queued_seconds(record, now_ns=now_ns) <= ttl:
                    continue
                if not cap_has_room:
                    # The cap is fleet-wide: the drain is deferring this record for a
                    # condition that is not the record's fault and will clear. Age
                    # keeps counting; the expiry does not (t_43f11055 a).
                    _log("age_kept_agent_cap", record)
                    continue
                if held(home, record.get("target_profile") or ""):
                    _log("age_kept_slot_held", record)
                    continue
                updated = dict(record)
                updated["status"] = STATUS_EXPIRED
                updated["reason"], updated["status_detail"] = expiry_classification(
                    record, ttl
                )
                updated["updated_at"] = now_ns
                updated["queue_position"] = None
                path = root / QUEUE_DIR / f"{updated['delivery_id']}.json"
                os.replace(path, root / SETTLED_DIR / f"{updated['delivery_id']}.json")
                _write(root / SETTLED_DIR / f"{updated['delivery_id']}.json", updated)
                _log("expired", updated, reason=updated["reason"])
                expired.append(updated)
                actions += 1
    if on_expired is not None:
        for record in expired:
            try:
                on_expired(record)
            except Exception:  # a notice must never abort the sweep
                logger.exception("bot_delivery expired-notice failed: %s", record.get("delivery_id"))
    notifier = notice_lease_lost_to_sender if on_lease_lost is _NOTICE_BY_DEFAULT else on_lease_lost
    if notifier is not None:
        for record in lost:
            try:
                notifier(record)
            except Exception:  # a notice must never abort the sweep
                logger.exception("bot_delivery lease-lost notice failed: %s",
                                 record.get("delivery_id"))
    _log("swept", None, actions=actions, ttl_seconds=ttl, expired=len(expired),
         lease_lost=len(lost))
    return actions


def queued_target_profiles(home: str | os.PathLike[str]) -> list[str]:
    """Distinct target profiles that have a queued delivery in this home's queue.

    Trigger 3 has to ask "what can be drained?" without a profile in hand
    (VERIFICATION D1).
    """
    with _locked(home) as root:
        profiles: list[str] = []
        for record in _list(root, QUEUE_DIR):
            profile = str(record.get("target_profile") or "")
            if profile and profile not in profiles:
                profiles.append(profile)
    return profiles


# --------------------------------------------------------------------------
# stranded census (DoD 4 of the peer-delivery project; card t_703af8d1)
# --------------------------------------------------------------------------
#: The census' own field set. ``stranded`` is the alarm's number; the rest is
#: evidence a reader needs to act without any other tooling.
#: ``claimed_stuck`` is a first-class counter since the lost-lease repair: a claim whose
#: lease ran out is a strand that ``pending_over_ttl`` structurally could not see
#: (an open attempt froze its reading), so it is counted on its own -- never merely
#: inferred from the over-TTL term.
_CENSUS_COUNT_KEYS = ("queued", "pending_over_ttl", "claimed_stuck", "expired", "ambiguous",
                      "failed", "delivered", "stranded")
_CENSUS_AGE_KEYS = ("oldest_age_seconds", "oldest_delivery_id", "oldest_path")


def _empty_census_seat() -> dict[str, Any]:
    seat: dict[str, Any] = {key: 0 for key in _CENSUS_COUNT_KEYS}
    seat.update({key: None for key in _CENSUS_AGE_KEYS})
    return seat


def _waited_seconds(record: dict[str, Any], *, now_ns: int) -> float:
    """How long a record waited: the queued intervals it accumulated (§2.6).

    For a terminal record this is the whole wait up to its settlement; for a
    pending one, the wait so far. Never negative, never raising -- a malformed
    record must not be able to break the census.
    """
    try:
        return max(0.0, float(queued_seconds(record, now_ns=now_ns)))
    except Exception:  # pragma: no cover - defensive
        return 0.0


def _note_oldest(seat: dict[str, Any], record: dict[str, Any], path: Path, *,
                 now_ns: int) -> None:
    age = _waited_seconds(record, now_ns=now_ns)
    if seat["oldest_age_seconds"] is None or age > float(seat["oldest_age_seconds"]):
        seat["oldest_age_seconds"] = age
        seat["oldest_delivery_id"] = str(record.get("delivery_id") or "")
        seat["oldest_path"] = str(path)


def seat_strand_census(home: str | os.PathLike[str], *, now_ns: int | None = None) -> dict[str, Any]:
    """One home's stranded-delivery census, read WITHOUT taking the spool lock.

    The count is the rule the peer-delivery project ruled (card t_703af8d1):

        stranded = queued records that waited past the queue TTL
                 + settled ``expired`` records (accepted and never delivered)
                 + CLAIMS WHOSE LEASE WAS LOST (``claimed_stuck``)

    ``claimed_stuck`` is a claim recovered by :func:`lease_lost`: an attempt nobody
    ended, older than ``2 x`` the TTL, with no live holder. It is counted there and
    NOT in ``pending_over_ttl``, so the two terms never describe one record twice --
    the stuck term is the one that can see a record the freeze hid.

    ``ambiguous`` -- the crash-mid-turn "outcome unknown", an HONEST unknown own
    terminal state, not a strand -- and ``failed`` are counted and REPORTED but
    never added to ``stranded``: they are different defects and nothing here
    decides their fix. The census reads with the same atomic-write/rename idiom
    the spool uses (temp file + ``os.replace``), so a reader sees whole records
    only and a concurrent drain cannot tear one.
    """
    now_ns = time.time_ns() if now_ns is None else int(now_ns)
    root = delivery_root(home)
    ttl = queue_ttl_seconds()
    seat = _empty_census_seat()
    for sub in (QUEUE_DIR, CLAIMED_DIR):
        for record in _list(root, sub):
            if record.get("status") in TERMINAL_STATUSES:
                continue
            seat["queued"] += 1
            path = root / sub / f"{record.get('delivery_id')}.json"
            if lease_lost(record, home=home, now_ns=now_ns):
                seat["claimed_stuck"] += 1
                _note_oldest(seat, record, path, now_ns=now_ns)
                continue
            if queued_seconds(record, now_ns=now_ns) > ttl:
                seat["pending_over_ttl"] += 1
                _note_oldest(seat, record, path, now_ns=now_ns)
    for record in _list(root, SETTLED_DIR):
        status = str(record.get("status") or "")
        if status in seat:
            seat[status] += 1
        if status == STATUS_EXPIRED:
            _note_oldest(
                seat, record,
                root / SETTLED_DIR / f"{record.get('delivery_id')}.json", now_ns=now_ns,
            )
    seat["stranded"] = (
        int(seat["pending_over_ttl"]) + int(seat["expired"]) + int(seat["claimed_stuck"])
    )
    return seat


def spool_homes(root_home: str | os.PathLike[str]) -> list[tuple[str, Path]]:
    """``(seat, home)`` for the routable seat and EVERY live named profile.

    The same identity predicate ``profile list`` uses (``bot_mode_probe._roster``):
    infra dirs and tombstones are not lanes, and a marker-carrying directory whose
    name is not a profile id is not one either.
    """
    from tools.bot_mode_probe import _roster

    return [(str(name), Path(home)) for name, home in _roster(Path(root_home))]


def _sum_census_seats(seats: dict[str, dict[str, Any]]) -> dict[str, Any]:
    totals = _empty_census_seat()
    for seat in seats.values():
        for key in _CENSUS_COUNT_KEYS:
            totals[key] += int(seat.get(key) or 0)
        age = seat.get("oldest_age_seconds")
        if age is not None and (totals["oldest_age_seconds"] is None
                                or float(age) > float(totals["oldest_age_seconds"])):
            totals["oldest_age_seconds"] = float(age)
            totals["oldest_delivery_id"] = seat.get("oldest_delivery_id")
            totals["oldest_path"] = seat.get("oldest_path")
    return totals


def strand_census(root_home: str | os.PathLike[str], *,
                  now_ns: int | None = None) -> dict[str, Any]:
    """The FLEET census: every spool under ``root_home``, plus the totals.

    Reproducible by one command (the alarm module's ``__main__`` prints exactly
    this), so the number in a log line can always be re-measured by hand.
    """
    now_ns = time.time_ns() if now_ns is None else int(now_ns)
    seats = {seat: seat_strand_census(home, now_ns=now_ns)
             for seat, home in spool_homes(root_home)}
    return {
        "root": str(Path(root_home)),
        "ttl_seconds": queue_ttl_seconds(),
        "now_ns": now_ns,
        "seats": seats,
        "totals": _sum_census_seats(seats),
    }


def stranded_breakdown(census: dict[str, Any]) -> str:
    """``seat:stranded`` for every seat with a non-zero count, sorted, or ``none``."""
    parts = [
        f"{seat}:{int(data.get('stranded') or 0)}"
        for seat, data in sorted((census.get("seats") or {}).items())
        if int(data.get("stranded") or 0)
    ]
    return ",".join(parts) or "none"


def check_capacity(
    home: str | os.PathLike[str],
    *,
    target_profile: str,
    sender_profile: str,
) -> None:
    """Raise :class:`QueueFullError` if one more delivery would exceed a cap.

    Admission checks capacity *before* it reserves an idempotency row, and an
    overflow may take no reservation at all -- so the check has to be reachable
    without :func:`admit`'s write.
    """
    with _locked(home) as root:
        if _pending(root, target_profile=target_profile) >= max_per_profile():
            raise QueueFullError(
                target_profile,
                limit_kind=LIMIT_PER_PROFILE,
                limit=max_per_profile(),
            )
        if (
            _pending(root, target_profile=target_profile, sender_profile=sender_profile)
            >= max_per_sender()
        ):
            raise QueueFullError(
                target_profile,
                limit_kind=LIMIT_PER_SENDER,
                limit=max_per_sender(),
            )


def queue_depth(home: str | os.PathLike[str], target_profile: str | None = None) -> int:
    """Number of not-yet-terminal records (optionally for one profile)."""
    with _locked(home) as root:
        return _pending(root, target_profile=target_profile)


# --------------------------------------------------------------------------
# sender-visible FAILURE for a record that expired unread (card t_8a7a47d7 b)
# --------------------------------------------------------------------------
#: Producer origin of an undelivered notice. Not a profile: no reply is routed
#: to it, and the drainer never files a notice about a notice.
EXPIRED_NOTICE_SENDER = "bot-delivery-drainer"

#: Why the notice itself was queued -- it is a fresh delivery, not a backlog one.
STATUS_DETAIL_UNDELIVERED_NOTICE = (
    "undelivered: the peer delivery this notice reports expired before any turn read it"
)


def expired_notice_key(delivery_id: str) -> str:
    """The idempotency key of the ONE failure notice owed for ``delivery_id``."""
    return f"expired-notice:{delivery_id}"


def _profile_home(profile: str) -> Path | None:
    """The home of ``profile`` on this install, or None when it is not a profile."""
    try:
        from hermes_cli.profiles import get_profile_dir

        return Path(get_profile_dir(profile))
    except Exception:  # an unknown sender is not a sweep failure
        return None


def _never_offered_failure_message(
    record: dict[str, Any], delivery_id: str, target: str, now_ns: int | None
) -> str:
    """What the sender is told when its delivery expired without ever being offered."""
    waited = int(queued_seconds(record, now_ns=now_ns))
    return (
        f"[peer-delivery FAILED] your delivery {delivery_id} to {target} was NEVER read: "
        f"it waited {waited}s queued and expired without ever being offered to a turn "
        f"(the drain never offered it). The earlier 'queued' receipt is NOT a pending "
        f"promise and nothing is still in flight. Escalate (the escalate tool) or resend "
        f"if this still matters."
    )


def _deliver_into_live_context(
    home: str | os.PathLike[str], message: str, *, record: dict[str, Any]
) -> bool:
    """Hand a failure notice to the sender's live session -- its next turn context.

    Deliberately NOT a queued turn (t_43f11055 c): a live-mailbox delivery is claimed by
    the session that is already running, so it can never grow the queue it reports on and
    can never itself expire. Returns False -- never raises -- when the sender has no
    advertised live consumer; the caller then keeps the failure in the log.
    """
    try:
        from tools.bot_live_delivery import (
            deliver_to_live_owner,
            find_canonical_live_owner,
        )

        owner = find_canonical_live_owner(Path(home))
        if owner is None:
            return False
        raw = str(record.get("delivery_id") or "")
        notice_id = hashlib.sha256(f"{raw}:never-offered".encode()).hexdigest()
        deliver_to_live_owner(
            Path(home), owner, message, delivery_id=notice_id,
            notification_category="diagnostic",
        )
        return True
    except Exception:  # a notice must never break the sweep
        logger.debug("bot_delivery never-offered live delivery failed", exc_info=True)
        return False


def notice_expired_to_sender(
    record: dict[str, Any],
    *,
    home: str | os.PathLike[str] | None = None,
    now_ns: int | None = None,
) -> dict[str, Any] | None:
    """File a FAILURE notice in the SENDER's own spool for an expired record.

    Card t_8a7a47d7 (b): a sender's receipt said "queued -- the receiver re-offers
    it". When the record expires unread that promise was false and the sender had
    no way to learn it; the receipt is long gone, so the text sat invisible until a
    human read the queue by hand. File ONE delivery into the sender's own lane so
    the failure is READ by that lane's turn -- deterministically, with no LLM in
    the loop -- which is the sender's cue to escalate.

    ``home`` is the sender's own home. Resolve it by profile only when omitted, and
    only from outside any lane's runtime scope (``get_profile_dir`` resolves against
    the ambient default home). Idempotent on the expired record: one notice per
    delivery. Returns the admitted record, or None when no notice is owed -- no
    sender, a notice about a notice, an unknown profile, or a full sender queue.

    An expiry of a record that was NEVER OFFERED to a turn files NO queued notice
    (t_43f11055 c): that failure is the drain's, and queueing a fresh turn behind the
    same busy lane is how a notice-of-a-notice loop forms -- 5 such notice records sat
    in platform-stl's settled set. The failure is logged here with the sender, the
    target and the delivery id, and the record itself settles as ``never_offered``,
    which is the reason the strand census counts and escalates from the board. No new
    queue record is ever created on that path, so the loop cannot form.
    """
    delivery_id = str(record.get("delivery_id") or "")
    sender = str(record.get("sender_profile") or "")
    target = str(record.get("target_profile") or "")
    if not delivery_id or not sender or sender == EXPIRED_NOTICE_SENDER:
        return None
    if int(record.get("attempts") or 0) <= 0 or record.get("reason") == REASON_NEVER_OFFERED:
        # (c) An UNOFFERED expiry owes no queued turn: queueing one behind the same busy
        # lane is how the notice-of-a-notice loop formed. The sender still learns the
        # delivery died -- in its NEXT TURN CONTEXT if it has a live session, otherwise
        # in the gateway log where the failure is searchable by sender and id. Either
        # way nothing new can be queued, so nothing new can expire.
        failure_home = Path(home) if home is not None else _profile_home(sender)
        message = _never_offered_failure_message(record, delivery_id, target, now_ns)
        delivered = (
            failure_home is not None
            and _deliver_into_live_context(failure_home, message, record=record)
        )
        _log(
            "expired_never_offered_failure",
            record,
            sender=sender,
            target=target,
            waited_seconds=int(queued_seconds(record, now_ns=now_ns)),
            delivered_to="live_context" if delivered else "log",
            notice="none",
        )
        return None
    if home is None:
        home = _profile_home(sender)
        if home is None:
            _log("expired_notice_skipped", record, reason="unknown_sender_profile")
            return None
    waited = int(queued_seconds(record, now_ns=now_ns))
    message = (
        f"[peer-delivery FAILED] your delivery {delivery_id} to {target} was NEVER read: "
        f"it waited {waited}s queued and expired still unread after "
        f"{int(record.get('attempts') or 0)} turn attempt(s). "
        f"The earlier 'queued' receipt is NOT a pending promise. Escalate (the escalate "
        f"tool) or resend if this still matters."
    )
    key = expired_notice_key(delivery_id)
    # A deterministic id keeps the notice single-writer: a re-run of the sweep
    # re-admits nothing.
    notice_id = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    try:
        admitted = admit(
            home,
            sender_profile=EXPIRED_NOTICE_SENDER,
            target_profile=sender,
            target_session_id=None,
            idempotency_key=key,
            fingerprint=hashlib.sha256(message.encode("utf-8")).hexdigest(),
            delivery_id=notice_id,
            message=message,
            status_detail=STATUS_DETAIL_UNDELIVERED_NOTICE,
        )
    except QueueFullError:
        _log("expired_notice_refused", record, reason=STATUS_DETAIL_QUEUE_FULL, target=sender)
        return None
    _log("expired_notice", record, notice_id=admitted.get("delivery_id"), target=sender)
    return admitted


def lease_lost_notice_key(delivery_id: str) -> str:
    """The idempotency key of the ONE failure notice owed for a lost lease."""
    return f"lease-lost-notice:{delivery_id}"


def notice_lease_lost_to_sender(
    record: dict[str, Any],
    *,
    home: str | os.PathLike[str] | None = None,
    now_ns: int | None = None,
) -> dict[str, Any] | None:
    """File a FAILURE notice in the SENDER's own spool for a dead-lettered claim.

    A claim whose lease ran out is a delivery that was accepted and
    never made -- eleven of them sat for up to 181h with nobody told. The receipt the
    sender holds said "queued -- the receiver re-offers it", which stopped being true
    when the lease was lost, and a ``settled/failed`` file nobody reads is a silent
    failure. So the repair tells the SENDER, deterministically and with no LLM in the
    loop, through the same door the expiry notice uses: ONE delivery into the sender's
    own lane, read by that lane's next turn, which is the sender's cue to resend.

    Idempotent on the delivery (one notice per lost lease). Returns the admitted
    record, or ``None`` when no notice is owed -- no sender, a notice about a notice,
    an unknown profile, or a full sender queue.
    """
    delivery_id = str(record.get("delivery_id") or "")
    sender = str(record.get("sender_profile") or "")
    target = str(record.get("target_profile") or "")
    if not delivery_id or not sender or sender == EXPIRED_NOTICE_SENDER:
        return None
    if home is None:
        home = _profile_home(sender)
        if home is None:
            _log("lease_lost_notice_skipped", record, reason="unknown_sender_profile")
            return None
    claimed = int(claim_age_seconds(record, now_ns=now_ns) or 0)
    message = (
        f"[peer-delivery FAILED] your delivery {delivery_id} to {target} was NEVER delivered: "
        f"it was claimed for {claimed}s, the claim's lease was lost (no live holder) and the "
        f"record was dead-lettered as lease_lost. The earlier 'queued' receipt is NOT a "
        f"pending promise. Resend if this still matters."
    )
    key = lease_lost_notice_key(delivery_id)
    notice_id = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    try:
        admitted = admit(
            home,
            sender_profile=EXPIRED_NOTICE_SENDER,
            target_profile=sender,
            target_session_id=None,
            idempotency_key=key,
            fingerprint=hashlib.sha256(message.encode("utf-8")).hexdigest(),
            delivery_id=notice_id,
            message=message,
            status_detail=STATUS_DETAIL_UNDELIVERED_NOTICE,
        )
    except QueueFullError:
        _log("lease_lost_notice_refused", record, reason=STATUS_DETAIL_QUEUE_FULL, target=sender)
        return None
    _log("lease_lost_notice", record, notice_id=admitted.get("delivery_id"), target=sender)
    return admitted


# --------------------------------------------------------------------------
# envelope construction (§1.2/§1.3)
# --------------------------------------------------------------------------
def retryable(envelope: dict[str, Any]) -> bool:
    """Whether the *sender* may retry this failed send as-is.

    Derived from the envelope reason only; unrelated to ``retry_action()``
    (the receiver's own agent auto-retry), which must never be derived from it.
    """
    if envelope.get("result") != RESULT_FAILED:
        return False
    return envelope.get("reason") in RETRYABLE_REASONS


def build_envelope(
    record: dict[str, Any],
    *,
    send_id: str | None = None,
    busy: bool | None = None,
    waited_seconds: float = 0.0,
    replayed: bool = False,
    holder: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the 24-field sender-visible envelope for a durable record.

    ``busy`` defaults to what the record's own ``status_detail`` asserts, so a
    replayed envelope cannot contradict the cause it prints (VERIFICATION D7)
    and a receipt for a free-slot backlog never claims the slot was held (D2).
    ``holder`` is the OBSERVED holder of a held slot — a call-time observation like
    ``waited_seconds``, never persisted (§2) and never part of ``status_detail``, which
    ``busy_from_detail`` derives ``busy`` from by equality. Unknown ⇒ ``None``, never ``{}``.
    """
    status = record.get("status") or STATUS_QUEUED
    if busy is None:
        busy = busy_from_detail(record)
    result = _result_for_status(status)
    reason = record.get("reason")
    is_retryable = result == RESULT_FAILED and reason in RETRYABLE_REASONS
    retry_after: float | None = None
    if is_retryable and reason == "queue_full":
        retry_after = QUEUE_FULL_RETRY_AFTER_SECONDS
    elif is_retryable:
        retry_after = record.get("retry_after_seconds")

    if record.get("status_detail") == STATUS_DETAIL_LIVE_OWNER:
        detail: str | None = DETAIL_HELD_LIVE
    elif status == STATUS_EXPIRED:
        detail = record.get("status_detail") or expired_detail()
    elif status == STATUS_AMBIGUOUS:
        detail = unknown_detail(record.get("idempotency_key"))
    elif reason == "queue_full":
        # Report the cap that ACTUALLY refused the send (VERIFICATION D6): the
        # per-sender cap (8) used to be reported as the per-profile cap (32), so an
        # operator would raise the wrong limit. Absent the fired limit, the
        # per-profile reading is the historical one.
        detail = queue_full_detail(
            limit_kind=str(record.get("limit_kind") or LIMIT_PER_PROFILE),
            limit=record.get("limit"))
    elif result == RESULT_RECEIPT:
        detail = DETAIL_RECEIPT
    else:
        detail = None

    envelope = {
        "object": OBJECT_NAME,
        "result": result,
        "status": status,
        "status_detail": record.get("status_detail"),
        "send_id": send_id or uuid.uuid4().hex,
        "delivery_id": record.get("delivery_id"),
        "idempotency_key": record.get("idempotency_key"),
        "replayed": bool(replayed),
        "peer": record.get("peer"),
        "profile": record.get("target_profile"),
        "session_id": record.get("target_session_id"),
        "queue_position": (
            record.get("queue_position") if status == STATUS_QUEUED else None
        ),
        "attempts": int(record.get("attempts", 0)),
        "queued_seconds": round(queued_seconds(record), 3),
        "waited_seconds": float(waited_seconds),
        "busy": bool(busy),
        "retryable": is_retryable,
        "retry_after_seconds": retry_after,
        "reply": record.get("reply") if result == RESULT_DELIVERED else None,
        "error": (
            record.get("error")
            if result in (RESULT_FAILED, RESULT_REFUSED)
            else None
        ),
        "reason": (
            reason if result in (RESULT_FAILED, RESULT_REFUSED) else None
        ),
        "detail": detail,
        "at": int(time.time()),
        "holder": holder or None,
    }
    return {field: envelope[field] for field in ENVELOPE_FIELDS}


def build_receipt(
    record: dict[str, Any],
    *,
    busy: bool | None = None,
    replayed: bool = False,
    waited_seconds: float | None = None,
    holder: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The acceptance receipt for a queued delivery (``result: receipt``).

    ``busy`` is the OBSERVED slot state (VERIFICATION D2): a receipt handed out
    while the target's turn slot is free must not report it as held. ``holder`` is the
    OBSERVED holder of a held slot (§2): it rides the envelope's ``holder`` field AND a
    fragment appended at the END of the human ``detail``, because the Desktop forwards only
    ``data.reason`` and the refusal TEXT — the text is the channel that reaches a sender.
    An unknown holder appends nothing, so every live substring survives verbatim.
    """
    waited = receipt_after_seconds() if waited_seconds is None else waited_seconds
    envelope = build_envelope(
        record,
        send_id=uuid.uuid4().hex,
        busy=busy,
        waited_seconds=waited,
        replayed=replayed,
        holder=holder,
    )
    envelope["result"] = RESULT_RECEIPT
    envelope["detail"] = build_notification_text(record)
    if holder:
        # Lazy: tools.bot_relay imports this module at module level (a top-level back-import
        # would be a cycle), and the wording lives there so receipt and refusal cannot drift.
        from tools.bot_relay import holder_fragment

        fragment = holder_fragment(holder)
        if fragment:
            envelope["detail"] = f"{envelope['detail']} — {fragment}" if envelope["detail"] else fragment
    if record.get("status") != STATUS_RUNNING:
        envelope["attempts"] = 0
    return envelope


def build_delivered_envelope(
    record: dict[str, Any], *, replayed: bool = False, waited_seconds: float = 0.0
) -> dict[str, Any]:
    return build_envelope(
        record, busy=False, waited_seconds=waited_seconds, replayed=replayed
    )
