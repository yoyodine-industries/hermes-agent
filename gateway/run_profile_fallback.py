"""Diagnostics for the missing-profile fallback in ``_resolve_profile_home_for_source``.

A stale profile name — a retired lane's *handle* stamped where a profile name
belongs, or a routing target that no longer exists — reaches the resolver from
hot callers (watchers re-resolving a persisted source on a tick). The fallback
itself is correct; the problem is the logging: one WARNING per resolution buries
every other line in ``gateway.log`` while telling the operator nothing about
*which* record carries the stale name or who keeps re-resolving it.

So the first occurrence is logged with its provenance (explicit profile,
transport owner, session key, caller) and later occurrences are counted; every
``PROFILE_FALLBACK_REPORT_EVERY`` repeats an INFO line reports how many were
suppressed, keeping a hot caller visible without flooding the log.

Before reporting a name as missing, a bot-relay *lane handle* is aliased to the
profile actually serving it, so a retired handle is not reported as a missing
profile at all.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Per-process dedupe state: {(profile, "platform/chat_id"): suppressed_repeat_count}.
_profile_fallback_suppressed: Dict[Tuple[str, str], int] = {}
# Lane-handle aliases already reported, so a hot caller mentions each once.
_profile_alias_logged: set = set()

# How many suppressed repeats between INFO summary lines. The first occurrence is
# always a WARNING; the summary is deliberately infrequent (it exists so a storm
# that never stops is still visible at all).
PROFILE_FALLBACK_REPORT_EVERY = 100


def reset_profile_fallback_log_state() -> None:
    """Test seam: drop the per-process dedupe state for the fallback diagnostics."""
    _profile_fallback_suppressed.clear()
    _profile_alias_logged.clear()


def source_log_key(source: Any) -> str:
    """``platform/chat_id`` identity — the source half of the dedupe key."""
    return f"{getattr(source.platform, 'value', source.platform)}/{getattr(source, 'chat_id', '?')}"


def caller_label(depth: int = 2) -> str:
    """``file:line:function`` ``depth`` frames up, for the fallback diagnostics.

    The *caller* is the point of the diagnostic — it identifies the writer of a
    repeated line. ``sys._getframe`` does not materialise a stack trace, and this
    runs only on the fallback path.
    """
    try:
        frame = sys._getframe(depth)
        return f"{Path(frame.f_code.co_filename).name}:{frame.f_lineno}:{frame.f_code.co_name}"
    except Exception:
        return "unknown"


def owner_profile_label(owner: Any) -> Optional[str]:
    """String form of an adapter/transport owner profile (``None`` when absent).

    ``GatewayRunner._transport_owner`` returns ``(adapter, profile)`` for
    adapter-bound sources.
    """
    if isinstance(owner, tuple) and len(owner) > 1:
        owner = owner[1]
    for value in (owner, getattr(owner, "profile", None)):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def session_key_label(source: Any, profile: Optional[str]) -> str:
    """The session key this source resolves to under ``profile``.

    That key names the persisted record carrying the stale name, so it is what
    the operator has to fix — include it rather than making them reconstruct it.
    """
    try:
        from gateway.session import build_session_key
        return build_session_key(source, profile=profile)
    except Exception:
        return "?"


def profile_alias_from_roster(name: str) -> Optional[str]:
    """The profile serving a bot-relay *lane handle*, or ``None``.

    Retired lanes leave their **handle** where a profile name belongs (sources
    replayed from persisted routing keep ``origin.profile``), so a handle is
    resolved to its live profile before it is reported as a missing profile.
    """
    try:
        from tools.bot_mode_probe import _default_home, _hermes_root
        from tools.bot_relay import read_remote_roster
        roster = read_remote_roster(_hermes_root(Path(_default_home())))
    except Exception:
        logger.debug("bot-relay roster unavailable while aliasing profile %r", name, exc_info=True)
        return None
    wanted = name.strip().lstrip("@").lower()
    if not wanted:
        return None
    for row in roster or ():
        if not isinstance(row, dict):
            continue
        handle = str(row.get("handle") or "").strip().lstrip("@").lower()
        profile = str(row.get("profile") or "").strip()
        if profile and handle == wanted:
            return profile
    return None


def log_profile_fallback(
    profile: str, source: Any, *, explicit_profile: Optional[str],
    owner_profile: Optional[str], caller: str,
) -> None:
    """Log a missing-profile fallback once per (profile, source) per process.

    Later occurrences are counted; every ``PROFILE_FALLBACK_REPORT_EVERY``
    repeats an INFO line reports how many were suppressed, so a hot caller stays
    visible without flooding the log.
    """
    key = (profile, source_log_key(source))
    repeats = _profile_fallback_suppressed.get(key, 0)
    if repeats == 0:
        logger.warning(
            "Profile %r does not exist for source %s/%s (guild_id=%s), "
            "falling back to global HERMES_HOME "
            "[explicit_profile=%r owner_profile=%r session_key=%r caller=%s]",
            profile, source.platform.value, source.chat_id,
            getattr(source, "guild_id", None),
            explicit_profile, owner_profile, session_key_label(source, profile), caller)
    _profile_fallback_suppressed[key] = repeats + 1
    if repeats and repeats % PROFILE_FALLBACK_REPORT_EVERY == 0:
        logger.info(
            "Profile %r is still missing for %s/%s: %d repeated resolutions suppressed "
            "since the first warning (caller=%s)",
            profile, source.platform.value, source.chat_id, repeats, caller)


def log_profile_alias(handle: str, profile: str, source: Any) -> None:
    """Report a lane-handle → profile alias once per (handle, source) per process."""
    key = (handle, source_log_key(source))
    if key in _profile_alias_logged:
        return
    _profile_alias_logged.add(key)
    logger.info(
        "Resolved lane handle %r to profile %r via the bot-relay roster (%s/%s)",
        handle, profile, source.platform.value, source.chat_id)
