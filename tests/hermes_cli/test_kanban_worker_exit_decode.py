"""Worker exit classification must not depend on POSIX-only ``os.WIF*`` helpers.

``_classify_worker_exit`` feeds the dead-worker reclaim: a ``rate_limited``
verdict requeues the card without counting a failure, ``unknown`` counts as a
crash. Windows has neither ``os.WIFEXITED`` nor ``waitpid(-1)``, so both the
decode and the reaper's exit capture have a Windows-independent path.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd


def _spawn_exit(code: int) -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, "-c", f"raise SystemExit({code})"])
    proc.wait()
    return proc




@pytest.mark.platforms("windows")
def test_native_windows_reaper_and_decode(monkeypatch):
    """Native Windows, nothing patched: ``_IS_WINDOWS`` selects the Popen-poll
    reaper and the decode runs where ``os.WIFEXITED`` does not exist, so the
    rate-limit sentinel exit is a requeue, not a crash."""
    monkeypatch.setattr(kbd, "_live_worker_procs", {})
    monkeypatch.setattr(kbd, "_recent_worker_exits", {})
    assert not hasattr(os, "WIFEXITED")
    proc = _spawn_exit(kb.KANBAN_RATE_LIMIT_EXIT_CODE)
    kbd._live_worker_procs[proc.pid] = proc
    assert kbd.reap_worker_zombies() == [proc.pid]
    assert kbd._classify_worker_exit(proc.pid) == ("rate_limited", kb.KANBAN_RATE_LIMIT_EXIT_CODE)


def test_storage_locked_exit_gets_its_own_kind():
    """76 is the state-store lock-stop sentinel and must NOT decode as ``nonzero_exit``:
    every nonzero code the dispatcher could not name was what made a lock-stop read as a
    genuine crash of the card (``_reclaim_dead_workers`` -> ``consecutive_failures`` -> the
    give-up limit at 2)."""
    assert kb.KANBAN_STORAGE_LOCKED_EXIT_CODE == 76
    assert kbd._exit_code_kind(kb.KANBAN_STORAGE_LOCKED_EXIT_CODE) == ("storage_locked", 76)
    # The neighbouring sentinels still own their own kinds, and a bare failure still crashes.
    assert kbd._exit_code_kind(kb.KANBAN_RATE_LIMIT_EXIT_CODE) == ("rate_limited", 75)
    assert kbd._exit_code_kind(kb.KANBAN_TERMINAL_PROVIDER_EXIT_CODE) == ("terminal_provider", 78)
    assert kbd._exit_code_kind(1) == ("nonzero_exit", 1)
