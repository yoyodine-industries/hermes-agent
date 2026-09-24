"""Global emergency stop (ESTOP) — a resumable pause for NEW work only.

``hermes pause`` writes a sentinel at ``$HERMES_HOME/ESTOP``; ``hermes resume``
removes it. While it exists the cron scheduler, kanban dispatcher and new gateway
turns skip work; in-flight work is never killed. The check is one or two uncached
``os.stat`` calls (process home + fleet root when they differ). The body is optional
JSON ``{"reason", "engaged_at", "expires_at", "allow"}``; a corrupt/empty file still
counts as engaged (fail safe, e.g. ``touch ~/.hermes/ESTOP``).

Fields extend the primitive, so an unattended hold can neither strand the fleet nor come
back anonymously:

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
``run_id``
    WHICH run armed the hold, so one found on disk can be traced to its armer rather than
    to a timestamp. Absent for a hand-armed pause — never a fabricated stand-in.

Every release also writes a dated record (``ESTOP.release.json``, beside the sentinel it
removed) naming its CAUSE — ``node`` (a wind-down DAG node released it), ``on_exit`` (the
run's exit path did), ``manual`` (an operator ran ``hermes resume``) or ``lease_expiry``
(read via :func:`get_last_release`). Only the deadman may write ``lease_expiry``: it is the
one cause that means the releasing run died before its exit path ran, so it is the one
state worth paging on.

Ported from gastownhall/gastown estop.go (MIT).
"""

from __future__ import annotations

import json
import logging
import re
import threading
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

# Same profile-aware / fleet-root resolvers the file-safety guards use (fail-open to ~/.hermes).
from agent.file_safety import _hermes_home_path as _hermes_home, _hermes_root_path as _canonical_root

SENTINEL_NAME = "ESTOP"

# Release records are written BESIDE the sentinel they removed, so a reader at the profile home
# and one at the fleet root each see how the last hold on their path came back.
RELEASE_RECORD_NAME = "ESTOP.release.json"
# Causes a CALLER may record. ``lease_expiry`` is deliberately absent: only the deadman writes it
# (see :func:`disengage`), so a clean release can never masquerade as "the exit path never ran".
RELEASE_CAUSES = ("node", "on_exit", "manual")
LEASE_EXPIRY_CAUSE = "lease_expiry"
DEFAULT_RELEASE_CAUSE = "manual"

logger = logging.getLogger(__name__)

# Per-component "logged already for this engagement" flags: log once per engagement, not per tick.
_log_lock = threading.Lock()
_logged_components: set[str] = set()
# Sentinel path -> engagement key (mtime_ns:size) of the last logged EXPIRY, so the
# deadman's lift is one loud line per engagement rather than one per check.
_expired_logged: dict[str, str] = {}

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


def _release_record_path(path: Path) -> Path:
    """Where the record of ``path``'s release is written (beside the sentinel it removed)."""
    return path.with_name(RELEASE_RECORD_NAME)


def _normalize_run_id(run_id: Any) -> Optional[str]:
    """The arming run's id as a non-empty stripped string, or None (a blank id is no id)."""
    if run_id is None:
        return None
    text = str(run_id).strip()
    return text or None


def _read_payload(path: Path) -> Optional[dict]:
    """Parsed sentinel body, or None when absent/unreadable/not a JSON object.

    None means "no usable body", which every caller must treat as ENGAGED (fail safe) —
    it is never a reason to lift the pause.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
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
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


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
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return _parse_stamp(value)


def _engagement_key(path: Path) -> str:
    """Identity of the CURRENT engagement on disk: a re-engage rewrites the file and so
    earns a fresh key (and a fresh loud line)."""
    try:
        stat = path.stat()
        return f"{stat.st_mtime_ns}:{stat.st_size}"
    except (OSError, AttributeError, TypeError, ValueError):
        return "unknown"


def _is_expired(path: Path) -> bool:
    """True only for a sentinel whose body carries a PARSABLE ``expires_at`` in the past."""
    payload = _read_payload(path)
    if payload is None:
        return False  # corrupt/unreadable body: still engaged (fail safe)
    expires = _parse_stamp(payload.get("expires_at"))
    if expires is None:
        return False
    return datetime.now(timezone.utc) >= expires


def _retire_expired(path: Path) -> None:
    """Report a deadman expiry once, then remove the dead sentinel.

    The removal is guarded by a re-check of the engagement key, so a re-arm that lands
    between the expiry read and the unlink loses nothing and is not recorded as a release
    (the hold came back, so there was no return to report); any failure to remove is
    swallowed (the pause is already lifted — the leftover file must never re-hold it,
    because :func:`_is_expired` stays True).
    """
    key = _engagement_key(path)
    with _log_lock:
        first_report = _expired_logged.get(str(path)) != key
        _expired_logged[str(path)] = key
    if not first_report:
        return
    payload = _read_payload(path)
    removed = False
    try:
        if _engagement_key(path) == key:
            # Recorded only for the engagement actually being retired, and before the unlink:
            # the lift is already true (the stamp is in the past), so losing the bookkeeping
            # must not also lose the record of it — while a hold that re-armed is not a return
            # and must not be recorded as one.
            _write_release(path, LEASE_EXPIRY_CAUSE, payload)
            path.unlink()
            removed = True
    except (OSError, AttributeError, TypeError, ValueError):
        pass
    armed_by = payload.get("run_id") if payload else None
    logger.warning(
        "Global emergency stop at %s EXPIRED at its deadman TTL — the pause has lifted and "
        "dispatch resumes. The sentinel %s; run `hermes pause --ttl <dur>` to re-arm.%s",
        path, "was removed" if removed else "could not be removed (it stays inert)",
        f" Armed by run {armed_by}." if armed_by else "",
    )


def _write_release(path: Path, released_by: str, payload: Optional[dict]) -> None:
    """Record WHY a sentinel came back, beside the sentinel itself.

    The record names the run that ARMED the hold (the sentinel's own ``run_id``, normalised on
    the way in); a release has no run of its own to name, and inventing one would be a false
    provenance. Best effort and never raises: the release has already happened by the time this
    runs, so a failed bookkeeping write must not re-hold the pause.
    """
    armed = payload or {}
    record = {
        "released_by": released_by,
        "released_at": datetime.now(timezone.utc).isoformat(),
        "run_id": _normalize_run_id(armed.get("run_id")),
        "reason": armed.get("reason") or None,
        "engaged_at": armed.get("engaged_at") or None,
        "expires_at": armed.get("expires_at") or None,
    }
    try:
        _release_record_path(path).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    except OSError:
        logger.warning(
            "Could not write the release record for %s (cause: %s) — the pause is lifted regardless.",
            path, released_by)


def is_engaged() -> bool:
    """True if ANY candidate sentinel exists and is NOT past its ``expires_at``; fail SAFE
    (True) on stat errors. An expired sentinel lifts the pause, logs one loud line and is
    retired, so a crashed window job can never park the fleet."""
    saw_stat_error = False
    for path in _candidate_sentinel_paths():
        try:
            if not path.exists():
                continue
        except OSError:
            saw_stat_error = True
            continue
        if _is_expired(path):
            _retire_expired(path)
            continue
        return True
    return saw_stat_error


def engage(
    reason: Optional[str] = None,
    allow: Optional[dict] = None,
    ttl: Any = None,
    expires_at: Any = None,
    run_id: Any = None,
) -> Path:
    """Create the ESTOP sentinel. Idempotent; re-engaging rewrites the file.

    ``allow`` is ``{"user_ids": [...], "profiles": [...]}`` (either key optional, values
    coerced to strings); ``ttl`` accepts ``45m``/``90m``/``2h``/seconds and sets the
    deadman, or pass ``expires_at`` (ISO-8601 / aware datetime) directly. An unusable ttl
    still engages — it never half-arms a pause — and is reported by the CLI. ``run_id`` names
    the run arming the hold (the wind-down DAG passes its own); a blank one is stored as
    absent rather than invented.
    """
    path = sentinel_path()
    now = datetime.now(timezone.utc)
    expiry = _resolve_expiry(expires_at)
    if expiry is None:
        seconds = parse_duration(ttl)
        if seconds:
            expiry = now + timedelta(seconds=seconds)
    payload: dict = {"engaged_at": now.isoformat(), "reason": reason or None}
    identity = _normalize_run_id(run_id)
    if identity:
        payload["run_id"] = identity
    if expiry is not None:
        payload["expires_at"] = expiry.isoformat()
    normalized_allow = _normalize_allow(allow)
    if normalized_allow:
        payload["allow"] = normalized_allow
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError:
        with suppress(OSError):  # Best effort: an empty/partial sentinel still pauses (fail safe).
            path.touch(exist_ok=True)
    return path


def disengage(released_by: str = DEFAULT_RELEASE_CAUSE) -> bool:
    """Remove every visible sentinel (process-local and fleet-root), recording WHY.

    ``released_by`` is one of :data:`RELEASE_CAUSES`. :data:`LEASE_EXPIRY_CAUSE` is reserved
    for the deadman — a caller claiming it would erase the one state worth paging on — so it
    raises ``ValueError`` and lifts NOTHING. A record is written beside each sentinel actually
    removed, best effort: failing to write it never re-holds a pause that is already lifted.
    """
    if released_by not in RELEASE_CAUSES:
        raise ValueError(
            f"released_by must be one of {', '.join(RELEASE_CAUSES)} (got {released_by!r}); "
            f"{LEASE_EXPIRY_CAUSE!r} is written only by the deadman path")
    lifted = False
    for path in _candidate_sentinel_paths():
        payload = _read_payload(path)  # before the unlink: it carries the arming run's id
        try:
            path.unlink()
            lifted = True
        except (OSError, AttributeError):
            continue
        _write_release(path, released_by, payload)
        with _log_lock:
            _expired_logged.pop(str(path), None)
    return lifted


def get_state() -> Optional[dict]:
    """``{"reason", "engaged_at", "expires_at", "allow", "run_id"}`` or None when not engaged;
    an unreadable/corrupt body still reports engaged with the fields None/{}. An expired
    sentinel is NOT engaged."""
    if not is_engaged():
        return None
    state = {"reason": None, "engaged_at": None, "expires_at": None, "allow": {}, "run_id": None}
    found = False
    for path in _candidate_sentinel_paths():
        try:
            if not path.exists():
                continue
        except OSError:
            return state
        except AttributeError:
            continue
        found = True
        payload = _read_payload(path)
        if payload is not None:
            state = {
                "reason": payload.get("reason") or None,
                "engaged_at": payload.get("engaged_at") or None,
                "expires_at": payload.get("expires_at") or None,
                "allow": _normalize_allow(payload.get("allow")),
                "run_id": _normalize_run_id(payload.get("run_id")),
            }
            break
    return state if found else None


def get_last_release() -> Optional[dict]:
    """How the LAST hold on this home came back, or None if no release was ever recorded.

    ``{"released_by", "released_at", "run_id", "reason", "engaged_at", "expires_at"}``, read
    from the record beside each candidate sentinel (newest ``released_at`` wins, so a profile
    release that also lifted the fleet root still reports the most recent one). Diagnostics
    only — never a gate: a missing or unreadable record is skipped, never guessed.
    """
    records = []
    for path in _candidate_sentinel_paths():
        payload = _read_payload(_release_record_path(path))
        if payload and payload.get("released_by"):
            records.append(payload)
    if not records:
        return None
    return max(records, key=lambda record: str(record.get("released_at") or ""))


def is_allowed(
    user_id: Optional[str] = None,
    profile: Optional[str] = None,
    state: Optional[dict] = None,
) -> bool:
    """True when the ACTIVE sentinel's allowlist admits this authenticated identity.

    PRIMARY key is ``user_id`` (identity survives a platform/profile change); ``profile``
    is the secondary key for a maintenance lane. No allowlist — or an unreadable sentinel —
    admits nobody. Pass ``state`` when the caller already read it, to save one stat+read.
    """
    if state is None:
        state = get_state()
    if not state:
        return False
    allow = state.get("allow") or {}
    if user_id is not None and str(user_id) in (allow.get("user_ids") or []):
        return True
    return bool(profile) and str(profile) in (allow.get("profiles") or [])


def paused_reply() -> Optional[str]:
    """Short user-facing notice for new gateway turns, or None if not paused."""
    state = get_state()
    if state is None:
        return None
    tag = f" ({state['reason']})" if state.get("reason") else ""
    until = f" Auto-resumes {state['expires_at']}." if state.get("expires_at") else ""
    return (
        f"⏸️ Hermes is paused{tag}. New work is on hold; run `hermes resume` to pick "
        f"things back up.{until}"
    )


def check_paused(component: str, logger: logging.Logger) -> bool:
    """Return True when engaged, logging once per engagement per component (re-armed after a resume)."""
    if not is_engaged():
        with _log_lock:
            _logged_components.discard(component)
        return False
    with _log_lock:
        first = component not in _logged_components
        _logged_components.add(component)
    if first:
        state = get_state() or {}
        reason = state.get("reason")
        suffix = f" (reason: {reason})" if reason else ""
        until = f" [auto-resumes {state.get('expires_at')}]" if state.get("expires_at") else ""
        logger.info(
            "%s dispatch paused by global emergency stop%s%s — remove with `hermes resume` (%s)",
            component, suffix, until, sentinel_path(),
        )
    return True


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
import os  # noqa: F401,E402
# ---- END PLUGIN-COMPAT ----
