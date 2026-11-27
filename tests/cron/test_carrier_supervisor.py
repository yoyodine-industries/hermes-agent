"""The carrier supervisor: file-backed output and a terminal receipt that outlive the scheduler.

Design: card ``t_9fbcfe35``, unit U2.  The defect is measured, not hypothetical - the 02:15
cross-markets departure was an orphan after the gateway restarted at 02:43, its writes went into a
pipe nobody read, and the attempt died as ``unknown`` with one log line.

What is pinned here:

* the ORDINARY path is byte-identical to today's direct ``Popen`` (stdout, stderr, the carrier's
  exit code, and the ``(success, output)`` the scheduler consumes),
* the receipt reaches ``running`` (naming the live carrier) and then ``exited`` with its code,
* **the gate**: a supervisor whose reader is gone (closed pipe = the scheduler died) still records
  a terminal receipt and still exits with the carrier's code - the EPIPE must change nothing,
* a scheduler timeout is recorded on the receipt (``timeout_s`` + the terminating signal) before
  the SIGKILL, and the caller still returns failure,
* the kill switch restores the direct path with no receipt and no captured files,
* a SIGKILLed carrier leaves a COMPLETE, parseable terminal receipt (nothing half-written).

Every test runs against a temp ``HERMES_HOME``: the host's live cron store and receipts directory
are never read or written.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SUPERVISOR = REPO_ROOT / "cron" / "carrier_supervisor.py"

JOB_ID = "job-under-test"
EXECUTION_ID = "eid-under-test"
IDENTITY = {"HERMES_CRON_JOB_ID": JOB_ID, "HERMES_CRON_EXECUTION_ID": EXECUTION_ID}


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """An isolated profile home; the supervisor and the scheduler both resolve it from here."""
    root = tmp_path / "home"
    (root / "scripts").mkdir(parents=True)
    (root / "cron").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("HERMES_CRON_CARRIER_RECEIPTS", raising=False)
    return root


def _script(home: Path, name: str, body: str) -> Path:
    path = home / "scripts" / name
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _job(**overrides) -> dict:
    job = {"id": JOB_ID}
    job.update(overrides)
    return job


def _run(home: Path, name: str, *, identity=None, job=None) -> tuple[bool, str]:
    from cron.scheduler_script import _run_job_script

    return _run_job_script(name, identity=identity, job=job or _job())


def _receipt_path(home: Path, execution_id: str = EXECUTION_ID) -> Path:
    return home / "cron" / "receipts" / f"{execution_id}.json"


def _maybe_receipt(home: Path, execution_id: str = EXECUTION_ID):
    path = _receipt_path(home, execution_id)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def _receipt(home: Path, execution_id: str = EXECUTION_ID) -> dict:
    path = _receipt_path(home, execution_id)
    assert path.exists(), f"no receipt at {path}"
    return json.loads(path.read_text(encoding="utf-8"))  # complete JSON = not half-written


def _captured(home: Path, suffix: str, execution_id: str = EXECUTION_ID) -> Path:
    return home / "cron" / "runs" / f"{execution_id}.{suffix}"


def _wait_for(probe, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = probe()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition was never met")


def _spawn_supervisor(
    home: Path, script: Path, *, execution_id: str = EXECUTION_ID
) -> subprocess.Popen:
    """Popen the supervisor exactly as the wiring does: by absolute path, carrier argv after ``--``."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("HERMES_CRON_")}
    env.update({
        "HERMES_HOME": str(home),
        "HERMES_CRON_JOB_ID": JOB_ID,
        "HERMES_CRON_EXECUTION_ID": execution_id,
        "PYTHONPATH": str(REPO_ROOT),
    })
    return subprocess.Popen(
        [sys.executable, str(SUPERVISOR), "--", "bash", str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(script.parent),
    )


def _running_with_carrier_pid(home: Path, supervisor_pid: int):
    record = _maybe_receipt(home)
    if not record or record.get("status") != "running":
        return None
    pid = record.get("carrier_pid")
    if pid in (None, supervisor_pid):
        return None
    return record


# ── 1. The ordinary path is byte-identical ────────────────────────────────────────────────


@pytest.mark.parametrize("exit_code", [0, 4])
def test_ordinary_path_matches_the_direct_popen_byte_for_byte(home, exit_code):
    body = (
        "echo 'first line'\n"
        "echo 'a warning on stderr' >&2\n"
        "echo 'second line'\n"
        f"exit {exit_code}\n"
    )
    _script(home, "report.sh", body)

    direct = _run(home, "report.sh")  # no identity = today's direct Popen path
    supervised = _run(home, "report.sh", identity=dict(IDENTITY))

    assert supervised == direct
    assert _captured(home, "out").read_bytes() == b"first line\nsecond line\n"
    assert _captured(home, "err").read_bytes() == b"a warning on stderr\n"


def test_success_output_is_the_carriers_own_stdout(home):
    _script(home, "ok.sh", "echo 'RAM 92% on host'\n")
    success, output = _run(home, "ok.sh", identity=dict(IDENTITY))
    assert success is True
    assert output == "RAM 92% on host"


# ── 2. running -> exited, naming the live carrier ─────────────────────────────────────────


def test_receipt_goes_running_with_the_live_carrier_pid_then_exited(home):
    from gateway.status import _pid_exists, get_process_start_time

    script = _script(home, "slow.sh", "sleep 1.0\necho 'finished'\n")
    proc = _spawn_supervisor(home, script)
    try:
        running = _wait_for(lambda: _running_with_carrier_pid(home, proc.pid))
        carrier_pid = int(running["carrier_pid"])
        assert _pid_exists(carrier_pid), "the receipt must name a live carrier"
        assert running["carrier_started_at"] == get_process_start_time(carrier_pid)
        assert running["status"] == "running"
        assert running["job_id"] == JOB_ID
        assert running["stdout_path"] == str(_captured(home, "out"))
        assert running["finished_at"] is None

        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:  # pragma: no cover - only on a hung supervisor
            proc.kill()
            proc.wait(timeout=10)

    receipt = _receipt(home)
    assert receipt["status"] == "exited"
    assert receipt["exit_code"] == 0
    assert receipt["finished_at"]
    assert receipt["carrier_pid"] == carrier_pid
    assert _captured(home, "out").read_bytes() == b"finished\n"


# ── 3. THE GATE: a dead scheduler must not change the outcome ─────────────────────────────


def test_closed_reader_still_records_a_terminal_receipt(home):
    script = _script(home, "late.sh", "sleep 0.8\necho 'late output that nobody reads'\n")
    proc = _spawn_supervisor(home, script)
    _wait_for(lambda: _running_with_carrier_pid(home, proc.pid))

    # The scheduler is gone: its read end of the supervisor's stdout is closed mid-run.
    proc.stdout.close()

    assert proc.wait(timeout=30) == 0, "a BROKEN PIPE must not change the carrier's exit code"
    receipt = _receipt(home)
    assert receipt["status"] == "exited"
    assert receipt["exit_code"] == 0
    assert _captured(home, "out").read_bytes() == b"late output that nobody reads\n"


def test_closed_reader_preserves_a_non_zero_exit_code(home):
    script = _script(home, "late-fail.sh", "sleep 0.8\necho 'boom' >&2\nexit 7\n")
    proc = _spawn_supervisor(home, script)
    _wait_for(lambda: _running_with_carrier_pid(home, proc.pid))
    proc.stdout.close()
    assert proc.wait(timeout=30) == 7
    receipt = _receipt(home)
    assert receipt["status"] == "exited"
    assert receipt["exit_code"] == 7
    assert _captured(home, "err").read_bytes() == b"boom\n"


# ── 4. The scheduler's own budget is recorded on the receipt ──────────────────────────────


def test_scheduler_timeout_is_recorded_and_the_caller_fails(home):
    _script(home, "hang.sh", "sleep 60\n")
    started = time.monotonic()
    success, output = _run(
        home, "hang.sh", identity=dict(IDENTITY), job=_job(script_timeout_seconds=1)
    )
    assert time.monotonic() - started < 30
    assert success is False
    assert "timed out after 1s" in output

    receipt = _receipt(home)
    assert receipt["status"] == "exited"
    assert receipt["timeout_s"] == 1
    assert receipt["signal"] == "SIGKILL"
    assert receipt["exit_code"] == -signal.SIGKILL
    assert receipt["finished_at"]


# ── 5. Kill switch: the direct path, byte for byte, with nothing written ──────────────────


def test_kill_switch_restores_the_direct_path_and_writes_nothing(home, monkeypatch):
    _script(home, "report.sh", "echo 'out'\necho 'err' >&2\nexit 5\n")
    direct = _run(home, "report.sh", identity=None)

    monkeypatch.setenv("HERMES_CRON_CARRIER_RECEIPTS", "0")
    switched = _run(home, "report.sh", identity=dict(IDENTITY))

    assert switched == direct
    assert not _receipt_path(home).exists()
    assert not _captured(home, "out").exists()
    assert not _captured(home, "err").exists()


def test_without_an_execution_id_the_carrier_runs_with_no_receipt(home):
    _script(home, "plain.sh", "echo 'plain'\n")
    assert _run(home, "plain.sh", identity={"HERMES_CRON_JOB_ID": JOB_ID}) == (True, "plain")
    assert not _receipt_path(home).exists()


# ── 6. A SIGKILLed carrier leaves a complete terminal receipt ─────────────────────────────


def test_sigkilled_carrier_leaves_a_complete_terminal_receipt(home):
    _script(home, "killed.sh", "echo 'about to die'\nkill -9 $$\n")
    success, output = _run(home, "killed.sh", identity=dict(IDENTITY))

    assert success is False
    # 128 + SIGKILL: the shell convention for a process that died by signal.
    assert "Script exited with code 137" in output

    receipt = _receipt(home)  # json.loads raises on anything half-written
    assert receipt["status"] == "exited"
    assert receipt["exit_code"] == -signal.SIGKILL
    assert receipt["finished_at"]
    assert _captured(home, "out").read_bytes() == b"about to die\n"


# ── The timeout record never overwrites the carrier's own terminal truth ──────────────────


def test_record_timeout_does_not_overwrite_an_exited_receipt(home):
    from cron import carrier_receipts, carrier_supervisor

    _script(home, "quick.sh", "echo 'done'\n")
    assert _run(home, "quick.sh", identity=dict(IDENTITY)) == (True, "done")
    before = _receipt(home)
    assert before["status"] == "exited" and before["exit_code"] == 0

    wrote = carrier_supervisor.record_timeout(IDENTITY, 1)

    assert wrote is False
    after = carrier_receipts.read_receipt(EXECUTION_ID)
    assert after is not None
    assert after["status"] == "exited"
    assert after["exit_code"] == 0
    assert after["timeout_s"] is None
