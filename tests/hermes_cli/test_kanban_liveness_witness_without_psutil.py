"""The liveness witness must be RECORDABLE on an interpreter that has no psutil.

Measured 2026-10-10 on yoyodine: the dispatcher runs in-process inside the gateway under
``tools/python-3.14.7…/bin/python3``, and the deployed "it passed" proof of the crash-sweep witness
was produced on ``venv/bin/python3`` — an interpreter that does not run the sweep, and that has
psutil. With psutil absent, ``gateway.status.get_process_uptime_fingerprint`` answered ``None``, so
``kanban_db_dispatch._process_fingerprint`` fell through to ``UNVERIFIED_WORKER_FINGERPRINT`` and the
restart-stable witness the sweep rests on was never written at the write site.

These tests take psutil away from the process exactly as that interpreter lacks it and assert the
fingerprint is still produced, still carries a witness and a non-zero boot reference, and still reads
``alive`` for the live process that recorded it.

On the pre-fix bytes the first assertion in ``test_the_fingerprint_survives_without_psutil`` fails —
that failure IS the regression (a deployed proof run under an interpreter with psutil cannot catch it).
"""

import contextlib
import os
import sys

import pytest

from gateway import status as gw_status
from hermes_cli import kanban_db_dispatch as kbd


@contextlib.contextmanager
def psutil_absent(monkeypatch):
    """``import psutil`` raises, as it does on the interpreter that runs the dispatcher.

    Both the import cache and the finder chain are taken away: every accessor here re-imports psutil
    per call, so a leftover ``sys.modules`` entry would answer for the module that is supposed to be
    missing and the test would prove nothing.
    """
    for name in [n for n in list(sys.modules) if n == "psutil" or n.startswith("psutil.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    class _Absent:
        def find_spec(self, name, path=None, target=None):
            if name == "psutil" or name.startswith("psutil."):
                raise ModuleNotFoundError(f"No module named {name!r} (psutil_absent)")
            return None

    monkeypatch.setattr(sys, "meta_path", [_Absent(), *sys.meta_path])
    yield


def test_the_fingerprint_survives_without_psutil(monkeypatch):
    """The write site's whole invariant, on the interpreter it actually runs under.

    ``_process_fingerprint`` is what ``_set_worker_pid`` persists. When it degrades to ``None`` the
    spawn records ``unverified`` and the sweep's identity component is inert — the defect.
    """
    pid = os.getpid()
    with psutil_absent(monkeypatch):
        assert gw_status._psutil_available() is False
        # The non-psutil start-time source answers on its own, on the psutil scale.
        start = gw_status._start_time_via_ps(pid)
        assert start is not None and start > 0

        fingerprint = kbd._process_fingerprint(pid)
        assert fingerprint is not None, "the witness is unrecordable without psutil"
        parts = kbd._fingerprint_parts(fingerprint)
        assert parts is not None, f"not a comparable fingerprint: {fingerprint!r}"
        witness, recorded_start, reference = parts
        assert witness, "the fingerprint carries no boot witness"
        assert recorded_start > 0, "the fingerprint carries no start half"
        assert reference > 0, "the fingerprint carries no boot reference"

        assert kbd._worker_liveness(pid, fingerprint) == kbd.WORKER_ALIVE
        assert kbd._worker_alive(pid, fingerprint) is True


def test_a_psutil_recorded_fingerprint_reads_alive_without_psutil(monkeypatch):
    """The real transition: rows recorded by a reading host are read by a psutil-less one."""
    pid = os.getpid()
    recorded = kbd._process_fingerprint(pid)
    assert recorded is not None, "precondition: this host records fingerprints with psutil"

    with psutil_absent(monkeypatch):
        current = kbd._process_fingerprint(pid)
        assert current is not None
        assert kbd._worker_liveness(pid, recorded) == kbd.WORKER_ALIVE
        assert kbd._worker_not_dead(pid, recorded) is True


@pytest.mark.skipif(sys.platform != "darwin", reason="kern.boottime is the macOS fallback reference")
def test_the_fallback_boot_reference_matches_the_psutil_one(monkeypatch):
    """The two sources must agree, because ``boot_reference_held`` allows 2 cs.

    A fallback reference that did not match would turn every cross-source comparison into ``unknown``
    — safe, but the witness would still never prove a recycled PID.
    """
    pid = os.getpid()
    recorded = gw_status.get_process_uptime_fingerprint(pid)
    assert recorded is not None
    recorded_reference = recorded[0]

    with psutil_absent(monkeypatch):
        assert gw_status._boot_reference_via_sysctl() is not None
        fallback = gw_status.get_process_uptime_fingerprint(pid)
    assert fallback is not None
    assert kbd.boot_reference_held(recorded_reference, fallback[0]) is True


def test_a_present_but_refusing_source_stays_none(monkeypatch):
    """The fallback is chosen on psutil being ABSENT, never on a read that failed.

    A source that is present and cannot read a process is a genuine "cannot certify identity"; the
    verdict for it is ``unknown`` and the claim holds (pinned in the witness suite's
    ``test_start_time_read_failure_is_unknown_and_holds_the_claim``). Papering it over with a second
    source would also silently change that verdict to ``alive``/``dead``.
    """
    monkeypatch.setattr(gw_status, "_get_process_start_time", lambda pid: None)
    assert gw_status.get_process_uptime_fingerprint(os.getpid()) is None
