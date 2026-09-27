"""Global emergency stop (ESTOP) — a resumable pause for NEW work only.

``hermes pause`` writes a sentinel at ``$HERMES_HOME/ESTOP``; ``hermes resume``
removes it. While it exists the cron scheduler, kanban dispatcher and new gateway
turns skip work; in-flight work is never killed. The check is one or two uncached
``os.stat`` calls (process home + fleet root when they differ). The body is optional
JSON ``{"reason", "engaged_at", "expires_at", "allow"}``; a corrupt/empty file still
counts as engaged (fail safe, e.g. ``touch ~/.hermes/ESTOP``).

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

import json
import logging
import os
import re
import tempfile
import threading
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

# Same profile-aware / fleet-root resolvers the file-safety guards use (fail-open to ~/.hermes).
from agent.file_safety import _hermes_home_path as _hermes_home, _hermes_root_path as _canonical_root

SENTINEL_NAME = "ESTOP"

# ---- hold registry ---------------------------------------------------------------
# Schema 2 = ``{"schema": 2, "holds": [hold, ...]}``. A body with no ``holds`` key is a
# pre-registry sentinel (``hermes pause`` of the day, or a bare ``touch``) and reads as ONE
# hold: owner ``operator``, mode ``estop`` (total), its own reason/expires_at/allow.
SCHEMA_VERSION = 2
HOLDS_KEY = "holds"

MODE_ESTOP = "estop"
MODE_LOCKDOWN = "lockdown"
MODES = (MODE_ESTOP, MODE_LOCKDOWN)
DEFAULT_MODE = MODE_ESTOP

# Owner key of the operator's own pause. A bare `hermes pause` writes (and `hermes resume`
# releases) exactly this hold, so the panic button stays one verb each way.
DEFAULT_OWNER = "operator"

# Defect lines (fail-closed rows): each is loud on purpose — a scoped stop we cannot read is
# never silently a total one, and an empty allowlist is never silently "everyone admitted".
DEFECT_EMPTY_ALLOW = "lockdown names no allow.profiles — every lane is held"
DEFECT_UNKNOWN_MODE = "unrecognised hold mode — read as a total halt"
DEFECT_UNREADABLE = "sentinel body is not usable JSON — read as a total halt, no exemptions"
DEFECT_STAT_ERROR = "sentinel could not be stat'ed — read as a total halt (fail safe)"

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

    ``utf-8-sig`` so a BOM left by a Windows editor does not turn the body into an
    unparsable one (a BOM'd ``expires_at`` would otherwise read as "no expiry").
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
    hold (unknown, already released, or expired) must be told so, never silently ignored."""

    released: bool
    stale: bool = False
    cleared: bool = False
    message: str = ""
    remaining: tuple = ()

    @property
    def remaining_owners(self) -> list:
        return [hold["owner"] for hold in self.remaining]


class EstopWriteError(OSError):
    """A hold could not be recorded. Raised rather than swallowed: a pause the operator
    believes is armed but that never reached the disk is the worst failure this module has."""


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


def _hold_from_entry(entry: Any, *, path: Path, index: int, legacy: bool = False) -> dict:
    """One normalised hold from a registry entry (or from a pre-registry whole body)."""
    payload = entry if isinstance(entry, dict) else {}
    mode, unknown = _normalize_mode(payload.get("mode"))
    owner = _normalize_owner(payload.get("owner") if payload.get("owner") is not None else DEFAULT_OWNER)
    engaged_at = payload.get("engaged_at") or None
    expires_at = payload.get("expires_at") or None
    handle = str(payload.get("handle") or "").strip()
    if not handle:
        stamp = _parse_stamp(engaged_at)
        if legacy:
            # A pre-registry sentinel has no owner but IS the operator's own pause; key it on
            # the file's engagement so the handle is stable across reads and releasable by name.
            handle = f"{owner}#legacy-{_engagement_key(path)}"
        else:
            handle = _handle_for(owner, stamp) if stamp else f"{owner}#{index}"
    return {
        "handle": handle,
        "owner": owner,
        "mode": mode,
        "unknown_mode": unknown,
        "reason": payload.get("reason") or None,
        "engaged_at": engaged_at,
        "expires_at": expires_at,
        "allow": _normalize_allow(payload.get("allow")),
        "expired": _is_past(expires_at),
        "path": str(path),
    }


def _read_holds(path: Path) -> tuple:
    """``(holds, defect)`` for one sentinel path. ``defect`` is the fail-SAFE reason a present
    sentinel could not be read as a body — a ``stat`` error, an unreadable/corrupt body, an
    entry that is not an object. All three still yield ONE hold (the operator's, TOTAL): a
    state we cannot read is never a licence to run, only a recorded defect.
    """
    try:
        present = path.exists()
    except (OSError, AttributeError):
        # `os.stat` raised: engaged, with no body to attribute the hold to.
        return [_hold_from_entry({}, path=path, index=0, legacy=True)], DEFECT_STAT_ERROR
    if not present:
        return [], None
    payload = _read_payload(path)
    if payload is None:
        # Empty / corrupt / ``touch``-ed: no owner to attribute, so it reads as the
        # operator's total hold — exactly today's meaning — plus the defect line.
        return [_hold_from_entry({}, path=path, index=0, legacy=True)], DEFECT_UNREADABLE
    entries = payload.get(HOLDS_KEY)
    if entries is None:
        return [_hold_from_entry(payload, path=path, index=0, legacy=True)], None
    if not isinstance(entries, list):
        return [_hold_from_entry({}, path=path, index=0, legacy=True)], DEFECT_UNREADABLE
    holds = [
        _hold_from_entry(entry, path=path, index=index)
        for index, entry in enumerate(entries)
        if isinstance(entry, dict)
    ]
    if not holds and entries:
        return [_hold_from_entry({}, path=path, index=0, legacy=True)], DEFECT_UNREADABLE
    return holds, None


def _is_past(value: Any, now: Optional[datetime] = None) -> bool:
    """True only for a PARSABLE stamp in the past — an unparsable expiry never lifts a hold."""
    stamp = _parse_stamp(value)
    if stamp is None:
        return False
    return (now or datetime.now(timezone.utc)) >= stamp


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


def is_engaged() -> bool:
    """True while ANY live hold sits on a candidate sentinel; fail SAFE (True) on stat
    errors. Expired holds are retired here, so a holder that died without releasing cannot
    park the fleet forever. A SCOPED (all-lockdown) state is still engaged — the caller
    that wants "may new work start at all" wants :func:`work_admitted` instead."""
    state = read_state()
    for hold in state.expired:
        _retire_hold(hold)
    return state.engaged


def _retire_hold(hold: dict) -> None:
    """Drop ONE expired hold, reporting it once per (handle, expiry) and keeping the file
    when other holders are still live — the 02:09:52 defect was an expiry that took a pause
    it did not own with it."""
    key = f"{hold['handle']}@{hold.get('expires_at')}"
    with _log_lock:
        first_report = _expired_logged.get(hold["handle"]) != key
        _expired_logged[hold["handle"]] = key
    removed = _remove_holds([hold])
    if first_report:
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

    A file with no holds must not linger: an empty sentinel reads as a total hold (fail
    safe), so leaving it behind would make a released pause stick forever. Returns False
    when the write failed, which callers report — never silently.
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
        return removed
    payload = {
        "schema": SCHEMA_VERSION,
        HOLDS_KEY: [
            {
                key: value
                for key, value in (
                    ("handle", hold.get("handle")),
                    ("owner", hold.get("owner")),
                    ("mode", hold.get("mode")),
                    ("reason", hold.get("reason")),
                    ("engaged_at", hold.get("engaged_at")),
                    ("expires_at", hold.get("expires_at")),
                    ("allow", hold.get("allow") or None),
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
        if defect:
            # An unreadable body cannot be edited entry-wise: drop the whole file so the
            # release is real (the held lanes were never the unreadable file's to keep).
            ok = _write_registry(path, []) and ok
            continue
        kept = [
            {
                key: hold.get(key)
                for key in ("handle", "owner", "mode", "reason", "engaged_at", "expires_at", "allow")
            }
            for hold in existing
            if hold["handle"] not in handles
        ]
        ok = _write_registry(path, kept) and ok
    return ok


def acquire(
    owner: str = DEFAULT_OWNER,
    mode: Any = None,
    reason: Optional[str] = None,
    ttl: Any = None,
    expires_at: Any = None,
    allow: Optional[dict] = None,
    replace_owner: bool = False,
) -> str:
    """Add ONE hold to this home's sentinel and return its handle.

    ``owner`` is the holder's identity — the operator's own pause uses ``operator``; an
    unattended holder passes its run id (``yoyoflow:<workflow>/enter#<run>``). Two
    acquisitions by the same owner are TWO holds unless ``replace_owner`` is set, which is
    what makes a repeated ``hermes pause`` idempotent instead of piling up.

    ``mode`` defaults to ``estop`` (a total halt); ``lockdown`` scopes the stop to the
    lanes in ``allow["profiles"]``. Pre-existing holds — including a pre-registry sentinel
    body, which reads as the operator's total hold — are preserved.
    """
    target = sentinel_path()
    resolved_mode, _unknown = _normalize_mode(mode)
    now = datetime.now(timezone.utc)
    owner_id = _normalize_owner(owner)
    expiry = _resolve_expiry(expires_at)
    if expiry is None:
        seconds = parse_duration(ttl)
        if seconds:
            expiry = now + timedelta(seconds=seconds)
    handle = _handle_for(owner_id, now)
    existing, _defect = _read_holds(target)
    kept = [hold for hold in existing if not (replace_owner and hold["owner"] == owner_id)]
    kept.append(
        {
            "handle": handle,
            "owner": owner_id,
            "mode": resolved_mode,
            "reason": reason or None,
            "engaged_at": now.isoformat(),
            "expires_at": expiry.isoformat() if expiry is not None else None,
            "allow": _normalize_allow(allow),
        }
    )
    if not _write_registry(target, kept):
        logger.error("ESTOP hold %s could not be written to %s", handle, target)
        if len(kept) == 1:
            # Nothing else is held on this sentinel: a bare file still reads as the
            # operator's TOTAL hold, so the panic button stays real even when its body
            # cannot be written. Silently returning here would leave the fleet RUNNING.
            try:
                target.touch(exist_ok=True)
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
    return handle


def release(handle: Optional[str] = None, owner: Optional[str] = None) -> ReleaseResult:
    """Release ONE hold — by exact ``handle``, or every live hold owned by ``owner``.

    Never touches another holder's entry. A handle that names no LIVE hold (unknown,
    already released, or expired) is reported as ``stale`` so the caller can fail loudly:
    a workflow that released into the void believes it holds a scope it no longer has.
    """
    state = read_state()
    if handle is not None:
        wanted = str(handle).strip()
        live = [hold for hold in state.holds if hold["handle"] == wanted]
        if not live:
            expired = [hold for hold in state.expired if hold["handle"] == wanted]
            if expired:
                detail = f"handle '{wanted}' already expired at {expired[0].get('expires_at')}"
            else:
                held = ", ".join(f"{hold['handle']} ({hold['mode']})" for hold in state.holds) or "none"
                detail = f"handle '{wanted}' names no live hold — live holds: {held}"
            return ReleaseResult(False, stale=True, message=detail, remaining=state.holds)
        targets = live
        label = wanted
    else:
        owner_id = _normalize_owner(owner)
        targets = [hold for hold in state.holds if hold["owner"] == owner_id]
        label = f"owner '{owner_id}'"
        if not targets:
            held = ", ".join(sorted(state.owners)) or "none"
            return ReleaseResult(
                False,
                message=f"no live hold owned by '{owner_id}' — live holders: {held}",
                remaining=state.holds,
            )
    if not _remove_holds(targets):
        return ReleaseResult(
            False,
            message=f"release of {label} could not be written — the hold is still in force",
            remaining=state.holds,
        )
    after = read_state()
    released_count = len(targets)
    message = f"released {label} ({released_count} hold{'s' if released_count != 1 else ''})"
    if after.engaged:
        message += " — still held by " + ", ".join(sorted(after.owners))
    else:
        message += " — the fleet is clear"
    return ReleaseResult(True, cleared=not after.engaged, message=message, remaining=after.holds)


def engage(
    reason: Optional[str] = None,
    allow: Optional[dict] = None,
    ttl: Any = None,
    expires_at: Any = None,
    mode: Any = None,
    owner: str = DEFAULT_OWNER,
    replace_owner: bool = True,
) -> Path:
    """Arm a hold on the ESTOP sentinel and return the sentinel path.

    This is the operator's own panic button: owner ``operator``, mode ``estop`` (a total
    halt) unless ``mode="lockdown"`` is given. It is idempotent — re-engaging REPLACES that
    owner's entry — and it leaves every OTHER holder's entry byte-identical, which is what
    stops a maintenance hold and the operator's pause from releasing each other.

    ``allow`` is ``{"user_ids": [...], "profiles": [...]}`` (either key optional, values
    coerced to strings); ``ttl`` accepts ``45m``/``90m``/``2h``/seconds and sets the
    deadman, or pass ``expires_at`` (ISO-8601 / aware datetime) directly. An unusable ttl
    still engages — it never half-arms a pause — and is reported by the CLI.
    """
    acquire(
        owner=owner,
        mode=mode,
        reason=reason,
        ttl=ttl,
        expires_at=expires_at,
        allow=allow,
        replace_owner=replace_owner,
    )
    return sentinel_path()


def disengage(owner: str = DEFAULT_OWNER, handle: Optional[str] = None) -> bool:
    """Release the operator's own hold (or the one named by ``handle``).

    True when the CALLER'S hold was released — NOT when the fleet is clear: a co-holder's
    section survives, and that is the point (releasing the operator's pause must not lift an
    ops-head maintenance scope, nor the reverse). Callers that report "resumed" to a human
    want :func:`release` instead, whose result names the remaining holders.
    """
    result = release(handle=handle, owner=None if handle else owner)
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


def check_paused(component: str, logger: logging.Logger) -> bool:
    """True only when NEW WORK must stop ENTIRELY — i.e. any live ``estop`` hold.

    Logs once per engagement per component (re-armed after a full release). Under a scoped
    ``lockdown`` (only lockdown holds live) this returns False on purpose: the tick RUNS,
    admits the allowlisted lanes, and the per-card / per-job lane gate holds everything
    else — which is what keeps a scoped stop from looking like an idle board.
    """
    state = read_state()
    if not state.engaged:
        with _log_lock:
            _logged_components.discard(component)
            _lockdown_logged.discard(component)
        return False
    if state.total:
        with _log_lock:
            first = component not in _logged_components
            _logged_components.add(component)
            _lockdown_logged.discard(component)
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
        lanes = ", ".join(sorted(state.allow_profiles)) or "(none)"
        logger.info(
            "%s runs SCOPED by lockdown x%d — lanes admitted: %s; every other lane's work is "
            "held per card/job, not silently skipped. Release with `hermes resume --handle "
            "<handle>` (%s)",
            component, state.counts[MODE_LOCKDOWN], lanes, sentinel_path(),
        )
    return False


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
import os  # noqa: F401,E402
# ---- END PLUGIN-COMPAT ----
