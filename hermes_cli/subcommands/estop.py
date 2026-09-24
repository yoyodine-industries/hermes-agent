"""``hermes estop state [--json]`` — a MACHINE read of the ESTOP sentinels.

``hermes status`` renders the pause for a human, and that rendering is not a contract: a
watcher that scrapes it stops matching after a cosmetic edit and reports green forever.
This subcommand emits the same state as data, for ops to consume.

**Read-only by construction.** Unlike ``agent.estop.is_engaged()`` — which lifts an expired
pause, logs it, and retires the dead sentinel — nothing here writes or removes anything. A
report must not mutate the state it reports: a sentinel past its ``expires_at`` (a hold that
outlived the run that armed it) is exactly what an ops watcher exists to see, so the reader
must never be the thing that clears the evidence.

Fields are read THROUGH ``agent.estop`` — its candidate path list, its payload reader, its
expiry predicate — never re-derived here, so this command composes with that module's own
evolution instead of pinning a second copy of the rule. On a tree whose estop predates the
deadman TTL there is no expiry predicate to consult, and the report says so itself:
``"semantics": "presence"``, any existing file engaged.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, cast


def _fallback_payload(path):
    """Minimal JSON-object read for a tree whose ``agent.estop`` predates the module's own
    payload reader (the pre-deadman build). Used only when the module exposes no reader, so
    the module stays the source of truth wherever it has one — and a body this install can
    actually read is never reported as corrupt."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, AttributeError):
        return None
    return raw if isinstance(raw, dict) else None


def _flat_allow(payload: dict, key: str) -> list:
    """A flat ``allow_user`` / ``allow_profile`` value as a list of non-empty strings.

    The sentinel schema keeps the allowlist nested (``allow.user_ids`` / ``allow.profiles``),
    so the flat keys are read as a tolerated alternative — a body written to either shape
    must never read back as "nobody is allowed".
    """
    value = payload.get(key)
    if isinstance(value, (str, int, float)):
        value = [value]
    return [str(item).strip() for item in value or [] if str(item).strip()]


def _sentinel_entries() -> tuple:
    """``(entries, semantics, body_reader)`` — one entry per candidate sentinel path."""
    from agent import estop

    read_payload = getattr(estop, "_read_payload", None)
    is_expired = getattr(estop, "_is_expired", None)
    normalize_allow = getattr(estop, "_normalize_allow", None)
    # Which rule produced ``engaged``: this build's deadman, or file presence alone.
    semantics = "ttl" if callable(is_expired) else "presence"
    reader = read_payload if callable(read_payload) else _fallback_payload

    entries = []
    for path in list(estop._candidate_sentinel_paths()):
        entry = {
            "path": str(path),
            "exists": False,
            "engaged": False,
            "body": "absent",
            "reason": None,
            "engaged_at": None,
            "expires_at": None,
            "allow_user": [],
            "allow_profile": [],
        }
        try:
            exists = bool(path.exists())
        except OSError:
            # A sentinel that cannot be stat'ed cannot be ruled out (the module's own
            # fail-safe), and an unreadable file is never reported as absent.
            entry.update(exists=None, engaged=True, body="unreadable")
            entries.append(entry)
            continue
        if not exists:
            entries.append(entry)
            continue

        entry["exists"] = True
        payload = cast(Optional[dict], reader(path))
        if payload is None:
            # Present with no usable JSON object: empty (``touch ~/.hermes/ESTOP``), truncated
            # or unparsable. Still a hold — the fact itself is reported.
            entry["body"] = "corrupt"
        else:
            entry["body"] = "json"
            entry["reason"] = payload.get("reason") or None
            entry["engaged_at"] = payload.get("engaged_at") or None
            entry["expires_at"] = payload.get("expires_at") or None
            allow = normalize_allow(payload.get("allow")) if callable(normalize_allow) else {}
            entry["allow_user"] = list(allow.get("user_ids") or []) or _flat_allow(
                payload, "allow_user")
            entry["allow_profile"] = list(allow.get("profiles") or []) or _flat_allow(
                payload, "allow_profile")
        entry["engaged"] = not (is_expired(path) if callable(is_expired) else False)
        entries.append(entry)
    return entries, semantics, "module" if callable(read_payload) else "fallback"


def _entry_line(entry: dict) -> str:
    if entry["body"] == "unreadable":
        return "UNREADABLE (stat failed) — held"
    if not entry["exists"]:
        return "absent"
    if entry["body"] == "corrupt":
        return "ENGAGED (corrupt/empty body: no reason or expiry readable)"
    bits = [entry["reason"] or "no reason given"]
    bits.append("engaged_at %s" % (entry["engaged_at"] or "unknown"))
    bits.append("expires_at %s" % (entry["expires_at"] or "none (no deadman)"))
    if entry["allow_user"] or entry["allow_profile"]:
        bits.append("allow user_ids=%s profiles=%s"
                    % (entry["allow_user"] or "[]", entry["allow_profile"] or "[]"))
    if not entry["engaged"]:
        bits.append("EXPIRED — the deadman has lifted the pause; watcher must report it")
    return "ENGAGED (%s)" % ", ".join(bits) if entry["engaged"] else "expired (%s)" % ", ".join(bits)


def cmd_estop(args: argparse.Namespace) -> int:
    """Report every candidate ESTOP sentinel. Exit 0 = the read succeeded (state is data)."""
    action = getattr(args, "estop_action", None)
    if action not in (None, "state"):
        print("usage: hermes estop state [--json]")
        return 2

    from agent.estop import sentinel_path

    entries, semantics, body_reader = _sentinel_entries()
    engaged = any(entry["engaged"] for entry in entries)

    if getattr(args, "as_json", False):
        print(json.dumps({
            "engaged": engaged,
            "semantics": semantics,
            "body_reader": body_reader,
            "read_at": datetime.now(timezone.utc).isoformat(),
            "hermes_home": str(sentinel_path().parent),
            "sentinels": entries,
        }, indent=2))
        return 0

    detail = "" if semantics == "ttl" else "  [presence semantics: no deadman on this build]"
    print("ESTOP: %s%s" % ("ENGAGED" if engaged else "not engaged", detail))
    for entry in entries:
        print("  %s\n    %s" % (entry["path"], _entry_line(entry)))
    return 0


def build_estop_parser(subparsers) -> None:
    """Attach the ``estop`` subcommand (and its ``state`` action) to ``subparsers``."""
    estop_parser = subparsers.add_parser(
        "estop", help="Emergency-stop (pause) state",
        description="Report the ESTOP sentinels this install would honor")
    estop_subparsers = estop_parser.add_subparsers(dest="estop_action")

    state = estop_subparsers.add_parser(
        "state", help="Report each candidate sentinel (machine-readable with --json)")
    state.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit JSON: {engaged, semantics, hermes_home, read_at, sentinels[]}, one "
             "sentinel entry per candidate path with path/engaged/reason/engaged_at/"
             "expires_at/allow_user/allow_profile. Read-only: never retires an expired "
             "sentinel, so a stale hold stays visible.")
    estop_parser.set_defaults(func=cmd_estop)
    state.set_defaults(func=cmd_estop)
