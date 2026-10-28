"""The turn lock's GRANULARITY: one lock per (profile, session), never one per profile.

Ruled on card ``t_47082c1d``. The original lock (#93091) was keyed on the PROFILE alone, so every
surface that runs a turn as profile P — a Desktop session, a hosted room, a DM delivery — serialized
against each other even though they touch different transcripts. Measured 2026-10-23: a DM from
``default`` to ``platform-stl`` waited **1m59s / 34 re-offers**, every one refused
``bot turn lock busy: profile=platform-stl holder=<the Desktop turn>``, while that same profile was
running four other sessions happily.

The key is now ``(profile, session/room id)``. Everything else the lock does is unchanged: the flock,
the non-blocking delivery probe, the bounded retry and the holder record all stay — only the file the
flock is taken on narrows. What must NOT narrow with it is the rule the lock exists for: two
deliveries into the SAME Bot Chat still serialize.

PAIRED on purpose (the DoD leg platform-stl holds PR acceptance on):

* ``test_a_dm_does_not_wait_behind_another_session_of_the_same_profile`` — RED on the profile key,
  green on the session key. This is the defect.
* ``test_two_deliveries_into_the_same_bot_chat_still_serialize`` — green on BOTH keys. This is the
  guarantee the coarse lock was bought for; a fix that greens the first by dropping the lock reds
  this one.
"""

from __future__ import annotations

try:
    import fcntl  # POSIX-only; on Windows the module is skipped wholesale
except ImportError:  # pragma: no cover - Windows
    fcntl = None
import os
import threading
import time

import pytest

pytestmark = pytest.mark.platforms("posix")

from tools.bot_relay import (
    TurnBusyError,
    acquire_delivery_turn_lock,
    acquire_turn_lock,
    read_turn_lock_holder,
    turn_lock_path,
)

DESKTOP = "session:7f3a-desktop"
BOTCHAT = "session:19c4-botchat"
ROOM = "room:ops-standup"


@pytest.fixture
def root(tmp_path):
    # Keep lockfile paths SHORT (macOS-safe).
    r = tmp_path / "r"
    r.mkdir()
    return r


def _hold_flock(path, hold_event, release_event):
    """Grab one lockfile on a separate fd, signal, hold until told."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    hold_event.set()
    release_event.wait(timeout=10)
    os.close(fd)  # close releases the flock — process-death semantics


# ── the key SHAPE ───────────────────────────────────────────────────────────


def test_one_lockfile_per_session_and_the_profile_is_still_named(root):
    """Two sessions of one profile are two lockfiles, both attributable to the profile."""
    desktop = turn_lock_path(root, "ops", session=DESKTOP)
    botchat = turn_lock_path(root, "ops", session=BOTCHAT)
    assert desktop != botchat
    assert desktop.parent == botchat.parent, "both live under bot_relay/locks/"
    # The name still carries the profile: an operator reading the locks dir can attribute a lock
    # to a seat without opening it, and a long session id cannot push the profile out of the name.
    assert desktop.name.startswith("ops") and botchat.name.startswith("ops")
    assert len(desktop.name) <= 255 and len(botchat.name) <= 255, "macOS filename bound"


def test_the_same_session_is_the_same_lockfile_however_it_is_spelled(root):
    assert turn_lock_path(root, "ops", session=ROOM) == turn_lock_path(root, "ops", session=ROOM)
    # …and a session key that carries path separators cannot escape the locks dir.
    path = turn_lock_path(root, "ops", session="../etc/passwd")
    assert path.parent == turn_lock_path(root, "ops", session=DESKTOP).parent
    assert "/" not in path.name and "\\" not in path.name


# ── the pair ────────────────────────────────────────────────────────────────


def test_a_dm_does_not_wait_behind_another_session_of_the_same_profile(root):
    """THE DEFECT. A DM into the Bot Chat must not queue behind the Desktop session's turn.

    Measured 2026-10-23: 1m59s and 34 re-offers, every refusal naming the Desktop turn as holder.
    """
    held = threading.Event()
    release = threading.Event()
    t = threading.Thread(
        target=_hold_flock,
        args=(turn_lock_path(root, "ops", session=DESKTOP), held, release),
    )
    t.start()
    assert held.wait(timeout=5)
    try:
        start = time.monotonic()
        with acquire_turn_lock(root, "ops", timeout_seconds=0.5, session=BOTCHAT):
            pass
        # Generous upper bound for a loaded host — the point is only that the Bot Chat turn never
        # waited the busy Desktop session's budget out.
        assert time.monotonic() - start < 0.4, "the Bot Chat turn queued behind another session"
    finally:
        release.set()
        t.join(timeout=5)


def test_two_deliveries_into_the_same_bot_chat_still_serialize(root):
    """THE GUARANTEE. Concurrent turns on ONE transcript stay mutually exclusive."""
    held = threading.Event()
    release = threading.Event()
    t = threading.Thread(
        target=_hold_flock,
        args=(turn_lock_path(root, "ops", session=BOTCHAT), held, release),
    )
    t.start()
    assert held.wait(timeout=5)
    try:
        with pytest.raises(TurnBusyError) as excinfo:
            with acquire_turn_lock(root, "ops", timeout_seconds=0.15, session=BOTCHAT):
                pass  # pragma: no cover — must not acquire
        assert excinfo.value.reason == "target_busy"
        assert excinfo.value.profile == "ops"
    finally:
        release.set()
        t.join(timeout=5)


def test_the_delivery_probe_is_per_session_and_still_non_blocking(root):
    """§2.5 step 1 survives the narrower key: a held SAME-session turn refuses at once."""
    held = threading.Event()
    release = threading.Event()
    t = threading.Thread(
        target=_hold_flock,
        args=(turn_lock_path(root, "ops", session=BOTCHAT), held, release),
    )
    t.start()
    assert held.wait(timeout=5)
    try:
        started = time.monotonic()
        with pytest.raises(TurnBusyError):
            with acquire_delivery_turn_lock(root, "ops", session=BOTCHAT):
                pass  # pragma: no cover
        assert time.monotonic() - started < 1.0, "the delivery probe must never wait"
        # …and a different session is free right now.
        with acquire_delivery_turn_lock(root, "ops", session=DESKTOP):
            pass
    finally:
        release.set()
        t.join(timeout=5)


# ── attribution: the holder record follows the narrower key ──────────────────


def test_the_holder_record_names_the_session_and_is_not_shared_across_sessions(root):
    """A waiter must be able to say WHICH session wedged it, and never read another's record."""
    with acquire_turn_lock(root, "ops", timeout_seconds=5, session=DESKTOP):
        holder = read_turn_lock_holder(root, "ops", session=DESKTOP)
        assert holder is not None
        assert holder["pid"] == os.getpid()
        assert holder["profile"] == "ops"
        assert holder["session"] == DESKTOP
        # The other session's slot is untouched: no cross-talk between adjacent transcripts.
        assert read_turn_lock_holder(root, "ops", session=BOTCHAT) is None
    # The record is cleared at the end of the turn window, per session.
    assert read_turn_lock_holder(root, "ops", session=DESKTOP) is None


def test_the_refusal_names_the_session_it_was_waiting_on(root):
    """A refusal that names only the profile cannot route around the wedge (t_9acf5894 item 3)."""
    held = threading.Event()
    release = threading.Event()
    t = threading.Thread(
        target=_hold_flock,
        args=(turn_lock_path(root, "ops", session=BOTCHAT), held, release),
    )
    t.start()
    assert held.wait(timeout=5)
    try:
        with pytest.raises(TurnBusyError) as excinfo:
            with acquire_turn_lock(root, "ops", timeout_seconds=0.1, session=BOTCHAT):
                pass  # pragma: no cover
        assert excinfo.value.session == BOTCHAT
    finally:
        release.set()
        t.join(timeout=5)
