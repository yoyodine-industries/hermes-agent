"""Plumbing shared by the kanban notifier and dispatcher loops.

Thread offload, board enumeration, live-config coercers and the singleton
dispatcher lock live here so the notifier, dispatcher and mixin modules read
them from one place.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextvars import Context
from pathlib import Path
from typing import Any, Callable, Optional

from utils import env_bool

# Keep the logger name run.py used so extracted log records are unchanged.
logger = logging.getLogger("gateway.run")


def _run_in_fresh_context(func: Callable[..., Any], /, *args: Any) -> Any:
    """Run *func* in an empty ``Context`` so request-local ContextVars stay behind.

    ``asyncio.to_thread`` copies the caller's context; a lingering
    ``delegate_task`` child marker would make ``write_txn`` false-trip for
    these process-owned writers. An empty Context keeps the DB guard intact
    for real children without exempting dispatcher writes.
    """
    return Context().run(func, *args)


async def _to_thread_process_service(func: Callable[..., Any], /, *args: Any) -> Any:
    """Offload blocking process-service work without inheriting request ContextVars."""
    return await asyncio.to_thread(_run_in_fresh_context, func, *args)


def _list_boards(kb: Any) -> list:
    """Enumerate live boards; fall back to the default board when listing fails."""
    try:
        return kb.list_boards(include_archived=False)
    except Exception:
        return [kb.read_board_metadata(kb.DEFAULT_BOARD)]


def _board_slugs(kb: Any) -> list:
    return [b.get("slug") or kb.DEFAULT_BOARD for b in _list_boards(kb)]


def _positive_int_setting(kanban_cfg: dict, key: str) -> Optional[int]:
    """Parse an optional ``kanban.<key>`` int cap; None when unset or invalid (< 1 is invalid)."""
    raw = kanban_cfg.get(key)
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("kanban dispatcher: invalid kanban.%s=%r; ignoring", key, raw)
        return None
    if value < 1:
        logger.warning("kanban dispatcher: kanban.%s=%r is below 1; ignoring", key, raw)
        return None
    logger.info("kanban dispatcher: %s=%d", key, value)
    return value


def _resolve_auto_decompose_settings(load_config: Callable[[], Any]) -> "tuple[bool, int]":
    """Live (enabled, per_tick) auto-decompose settings, re-read every dispatcher tick.

    Fails safe: a config read error returns ``(False, 3)`` rather than
    re-enabling a feature the user turned off. ``per_tick`` is clamped to ``>= 1``.

    Read fresh from config on every dispatcher tick (#49638) so that flipping ``kanban.auto_decompose:
    false`` to STOP runaway fan-out takes effect on the next tick instead of requiring a gateway restart.
    Auto-decompose is a safety toggle — a user who sees it create and launch tasks they didn't intend
    reaches for this flag to halt it, and a stale boot-captured value silently ignoring that change is the
    bug reported in #49638.
    """
    try:
        cfg = load_config()
    except Exception:
        return False, 3
    kcfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    try:
        per_tick = int(kcfg.get("auto_decompose_per_tick", 3) or 3)
    except (TypeError, ValueError):
        per_tick = 3
    return bool(kcfg.get("auto_decompose", True)), max(per_tick, 1)


def _gc_retention_days() -> int:
    """``kanban.done_sub_retention_days`` (default 30; 0 disables), re-read per sweep; fails safe to 30."""
    try:
        from hermes_cli.config import load_config

        return int(((load_config() or {}).get("kanban") or {}).get("done_sub_retention_days", 30))
    except Exception:
        return 30


def _kanban_dispatch_allowed() -> bool:
    """False while the global emergency stop (`hermes pause`) is engaged.

    Checked every tick before spawning, so a pause applies on the next tick;
    in-flight workers are never touched. Fails open if estop is unimportable.
    """
    try:
        from agent.estop import check_paused
    except ImportError:
        return True
    return not check_paused("kanban", logger)


def _resolve_dispatch_paused(load_config: Callable[[], Any]) -> "tuple[bool, str]":
    """``(paused, control)`` for the scoped ``kanban.dispatch_paused`` halt.

    Read fresh from config on every dispatcher tick — never captured at boot — so
    flipping ``kanban.dispatch_paused`` takes effect on the NEXT tick without a gateway
    restart, and clearing it resumes just as fast. Same live-read shape and rationale as
    :func:`_resolve_auto_decompose_settings` (#49638): this is the toggle an operator
    reaches for to STOP runaway dispatch, so a stale boot-captured value silently
    ignoring that change is the bug being avoided.

    It is the scoped alternative to the global emergency stop: auto-decompose and new
    dispatch stop, while workers already running are left to finish.

    ``HERMES_KANBAN_DISPATCH_PAUSED`` is an internal bridge for test rigs and ops hooks
    that must force a halt without writing config; ``kanban.dispatch_paused`` is the
    supported control and the one a user should reach for.

    ``control`` names what is holding the pause, so the tick log says which knob to turn.

    Fails SAFE toward doing less: an unreadable config returns ``(True, ...)``. The boot
    path already treats an unreadable config as "do not dispatch", and resuming on a read
    error would undo a pause the operator set on purpose. Because the value is re-read
    every tick, the pause lifts by itself once config is readable again.
    """
    if env_bool("HERMES_KANBAN_DISPATCH_PAUSED"):
        return True, "the HERMES_KANBAN_DISPATCH_PAUSED bridge"
    try:
        cfg = load_config()
    except Exception as exc:
        logger.warning(
            "kanban dispatcher: cannot read config for kanban.dispatch_paused (%s); "
            "treating dispatch as paused",
            exc,
        )
        return True, "an unreadable config (failing safe)"
    kcfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    return bool(kcfg.get("dispatch_paused", False)), "kanban.dispatch_paused"


def _dispatch_halt_reason(estop_engaged: bool, paused: bool, control: str) -> Optional[str]:
    """One-line reason this tick will claim and spawn nothing, or None to dispatch.

    The global emergency stop is named first, and named even when the scoped
    ``kanban.dispatch_paused`` is also set: estop is the stronger signal and the scoped
    flag must never mask it in the log.
    """
    reasons = []
    if estop_engaged:
        reasons.append("global emergency stop (`hermes pause`) engaged")
    if paused:
        reasons.append(f"dispatch paused via {control}")
    return "; ".join(reasons) if reasons else None


def _acquire_singleton_lock(lock_path) -> "tuple[Optional[object], str]":
    """Take the exclusive, non-blocking advisory lock for the sole dispatcher.

    Only one gateway machine-wide may run the embedded dispatcher: concurrent
    dispatchers double reclaim frequency and claim events, and with
    ``wal_autocheckpoint=0`` concurrent manual checkpoints can corrupt index
    pages. ``dispatch_in_gateway`` is the primary control; this is the backstop.

    Returns ``(handle, "held")`` (release via :func:`_release_singleton_lock`),
    ``(None, "contended")`` when another process holds it (caller must NOT
    dispatch), or ``(None, "unavailable")`` when locking cannot be performed
    (caller falls back to config control).
    """
    try:
        from gateway.status import _try_acquire_file_lock  # deferred; same package
    except ImportError:
        return None, "unavailable"
    try:
        Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
        handle = open(str(lock_path), "a+", encoding="utf-8")
    except OSError:
        return None, "unavailable"
    if not _try_acquire_file_lock(handle):
        handle.close()
        return None, "contended"
    return handle, "held"


def _release_singleton_lock(handle) -> None:
    """Release a lock acquired via :func:`_acquire_singleton_lock`."""
    if handle is None:
        return
    with contextlib.suppress(Exception):
        from gateway.status import _release_file_lock

        _release_file_lock(handle)
    with contextlib.suppress(Exception):
        handle.close()
