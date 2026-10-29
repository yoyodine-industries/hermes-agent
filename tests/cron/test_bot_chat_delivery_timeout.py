"""A timed-out bot-chat delivery still owes the operator the output (t_e9d06d80).

Retired contract (the 2026-09-19 docgen-deadman fix, previously
``test_bot_chat_timeout_marker.py``): a timeout queued a SHORT marker whose own words were
"the full output could NOT be posted here". Measured 2026-10-23 on job 50b61ee711ae: the
operator's only copy of the diagnosis was a 280-char excerpt of it. Bounding the turn is
right; sacrificing the payload is not.

Invariants:
1. A timed-out CLI delivery queues the OUTPUT itself — head+tail trimmed to one postable
   message, naming `hermes cron runs` for the untrimmed text — and starts the drain so it
   lands now rather than on a later tick. It never queues a notice that the output failed.
2. Output that fits the window is queued whole, unmodified.
3. A timeout while draining that queued record queues nothing further (recognised by the
   record's own flag, never by its text): one failed attempt must not spawn a copy chain.
4. A turn report appearing in the kill window books the delivery instead of raising
   TimeoutExpired (a delivered turn must not be reported as lost).
"""
import subprocess
import threading
from unittest.mock import Mock

import pytest

from cron import scheduler_delivery as delivery


@pytest.fixture()
def cli_lane(tmp_path, monkeypatch):
    """Route _deliver_to_bot_chat straight into the CLI fallback lane."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        delivery, "_run_bot_chat_turn",
        Mock(side_effect=subprocess.TimeoutExpired(["hermes"], 180)))
    return tmp_path


def _capture_defer(monkeypatch):
    from cron import bot_chat_delivery as queue
    calls = []
    monkeypatch.setattr(queue, "defer", Mock(
        side_effect=lambda key, job, content, profile, home, **kw:
        calls.append(dict(key=key, content=content, kw=kw)) or {"id": key, "status": "queued"}))
    monkeypatch.setattr(queue, "drain_in_background", Mock())
    return queue, calls


def test_timeout_queues_the_trimmed_output(cli_lane, monkeypatch):
    queue, calls = _capture_defer(monkeypatch)

    head_line = "MISDIRECTED: the declared board 'platform' does not declare dispatch:true"
    tail_line = "heavy-pass-cadence: pass exited rc=1 card=t_64b1bb9d"
    payload = head_line + "\n" + "detail line\n" * 900 + tail_line
    job = {"id": "job-1", "name": "heavy-pass-cadence", "execution_id": "exec-1"}

    result = delivery._deliver_to_bot_chat(job, payload, "")

    assert len(calls) == 1
    key, queued, kw = calls[0]["key"], calls[0]["content"], calls[0]["kw"]
    assert len(key) == 64 and int(key, 16) >= 0  # a fresh hex delivery id, not "<key>-degraded"
    assert kw.get("degraded") is True
    # The diagnosis at the top and the exit line at the bottom both survive the trim.
    assert head_line in queued
    assert tail_line in queued
    assert "trimmed" in queued and "hermes cron runs" in queued and "job-1" in queued
    assert len(queued) <= (
        delivery._BOT_CHAT_TIMEOUT_TRIM_HEAD + delivery._BOT_CHAT_TIMEOUT_TRIM_TAIL + 200)
    # Never the old "your alert did not arrive" notice.
    assert "could NOT be posted" not in queued
    assert "DELIVERY DEGRADED" not in queued
    # It is a plain body: the standard cronjob header wraps it at delivery time.
    assert not queued.startswith("[")

    assert result is not None and "queued" in result and "could NOT" not in result
    queue.drain_in_background.assert_called_once()  # posted now, not on a later tick


def test_timeout_queues_short_output_whole(cli_lane, monkeypatch):
    _queue, calls = _capture_defer(monkeypatch)

    payload = "pass 20261023T081100Z.22848 exited rc=1\ncard=t_64b1bb9d\n"
    job = {"id": "job-2", "name": "short", "execution_id": "exec-2"}

    delivery._deliver_to_bot_chat(job, payload, "")

    assert len(calls) == 1
    assert calls[0]["content"] == payload.strip()
    assert calls[0]["kw"].get("degraded") is True


def test_drained_record_times_out_without_requeueing(cli_lane, monkeypatch):
    queue, calls = _capture_defer(monkeypatch)

    from hermes_state import SessionDB
    SessionDB(db_path=cli_lane / "state.db").close()  # a deferred target must have a state.db

    payload = "P1 findings: everything on fire\n" + "detail line\n" * 40
    record = {"id": "a" * 64, "home": str(cli_lane), "profile": "", "degraded": True}
    posted = []

    def capture_then_timeout(argv, env, report_file, timeout_s):
        with open(argv[argv.index("--query-file") + 1], encoding="utf-8") as fh:
            posted.append(fh.read())
        raise subprocess.TimeoutExpired(argv, timeout_s)

    monkeypatch.setattr(delivery, "_run_bot_chat_turn", capture_then_timeout)
    job = {"id": "job-3", "name": "docgen deadman", "execution_id": "exec-3"}

    result = delivery._deliver_to_bot_chat(job, payload, "", deferred=record)

    assert calls == []  # the retry of a retry must not queue a second copy
    assert result is not None and "the result is saved" in result
    # The drained record is delivered like any other output: one standard header (naming the
    # job) and the payload — no second, record-specific framing.
    assert len(posted) == 1
    assert posted[0].startswith('[Cronjob "docgen deadman" output — ')
    assert posted[0].count("[Cronjob") == 1
    assert posted[0].endswith("\n\n" + payload)
    queue.drain_in_background.assert_not_called()


class _FakeProc:
    """Blocks in communicate() until killed; exit code set by the report path."""

    def __init__(self, *args, **kwargs):
        self.pid = 4242
        self.returncode = None
        self.killed = threading.Event()

    def communicate(self):
        self.killed.wait()
        return "", ""

    def kill(self):
        self.returncode = -9
        self.killed.set()


def test_late_turn_report_books_delivery(tmp_path, monkeypatch):
    """Report appearing in the kill window = turn completed = booked, not a timeout."""
    from hermes_cli import quiet_single_query as qsq
    monkeypatch.setattr(delivery.subprocess, "Popen", _FakeProc)
    late_state = {}

    def fake_read(path, pid):
        # Only after the kill: simulate the report landing in the kill window.
        if pid == 4242 and late_state.get("killed"):
            return {"pid": pid, "exit_code": 0, "error": None}
        return None

    real_kill = _FakeProc.kill

    def kill_and_flag(self):
        real_kill(self)
        late_state["killed"] = True

    monkeypatch.setattr(_FakeProc, "kill", kill_and_flag)
    monkeypatch.setattr(qsq, "read_turn_report", fake_read)

    result = delivery._run_bot_chat_turn(["hermes"], {}, str(tmp_path / "r.json"), 0.4)
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 0
