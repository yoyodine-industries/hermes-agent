"""The cron store's write-time carrier gate (card t_a38b4a2f).

The gate judges the file a row's ``script``/``monitor_script`` resolves to BEFORE the
row is persisted. The predicate is the control plane's ``carrier.py``; these tests use
the live one and skip where it is absent (nothing here re-implements it).

Every test asserts a real outcome of ``cron.jobs`` — the two write chokepoints the
CLI, the ``cronjob`` tool and the web route all funnel through — plus the ledger row
that records what happened.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cron import carrier_gate, jobs

CONTROL_PLANE = Path(
    os.environ.get(carrier_gate.CONTROL_PLANE_ENV) or carrier_gate.DEFAULT_CONTROL_PLANE
)

pytestmark = pytest.mark.skipif(
    not (CONTROL_PLANE / carrier_gate.PREDICATE_NAME).is_file(),
    reason="carrier control plane (%s) is not on this host" % CONTROL_PLANE,
)

#: A shadow launcher: logic, a `cd`, a command substitution, and a line naming the
#: sandbox. Shape only - the target is never run by a test.
SHADOW = """#!/bin/bash
# oneshot sweep launcher
cd /opt/hermes_sandbox || exit 1
out=$(/opt/hermes_sandbox/yaan-majordomo/cron/oneshot-sweep.sh 2>&1) || exit 1
echo "$out"
"""

#: A shadow-shaped script that is SAFE TO RUN: it names the sandbox (so the contract
#: reads it as a preserved shadow) and prints its own path, which is how the
#: resolution-agreement test proves which bytes ran.
RUNNABLE_SHADOW = """#!/bin/bash
cd /opt/hermes_sandbox || exit 1
echo "FIRED $(basename "$0")"
"""

#: A shipped carrier: declarations plus ONE exec whose target is a prod path. The name
#: matches a manifest carrier slot (``include scripts/deploy-train-shim.sh -> deploy-train.sh``),
#: which is what registers it for C1/C2/C3.
SHIPPED_SHIM = """#!/bin/bash
exec /opt/hermes_prod/yaan-platform/deploy-train.sh "$@"
"""

#: A worker: not a carrier at all. It is a Python data-processing row that legitimately
#: names the sandbox (the measured class-wide boundary: C1 reads shell launchers).
PY_WORKER = '''"""nightly audit - reads the sandbox and prod roots."""
ROOTS = ["/opt/hermes_prod", "/opt/hermes_sandbox"]
print("audit", ROOTS)
'''


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A hermetic HERMES_HOME with a scripts dir, the store, and the ledger.

    PATH is filtered of the real Hermes home's entries: the fork's suite carries a
    tripwire (``tests/home_io_guard.py``) that fails any test whose subprocess env
    points at the real home, and the shell this suite runs from has the live
    ``.hermes/bin`` on PATH. (Pre-existing tests that run a script fail on that same
    tripwire on this host, e.g. ``TestRunJobScript::test_successful_script``.)
    """
    hermes_home = tmp_path / "home"
    (hermes_home / "scripts").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("HERMES_CARRIER_GATE", raising=False)
    monkeypatch.setenv(carrier_gate.CONTROL_PLANE_ENV, str(CONTROL_PLANE))
    real_entry = str(Path.home() / ".hermes")
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join(
            entry for entry in os.environ.get("PATH", "").split(os.pathsep)
            if entry and real_entry not in entry
        ),
    )
    return hermes_home


def seed(home: Path, name: str, body: str) -> Path:
    path = home / "scripts" / name
    path.write_text(body)
    path.chmod(0o755)
    return path


def ledger(home: Path) -> list:
    path = home / "logs" / carrier_gate.LEDGER_NAME
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def store(home: Path):
    # `use_cron_store` takes a HERMES HOME: the store itself lands in <home>/cron.
    return jobs.use_cron_store(home)


def create(script, **kwargs):
    kwargs.setdefault("prompt", "canary")
    return jobs.create_job(schedule="every 1h", script=script, **kwargs)


def test_shadow_script_row_is_refused_and_nothing_is_persisted(home):
    seed(home, "oneshot-sweep.sh", SHADOW)
    with store(home):
        with pytest.raises(carrier_gate.CarrierRefused) as excinfo:
            create("oneshot-sweep.sh")
        # Refusal names the clause, the line and the line text (the card's acceptance).
        message = str(excinfo.value)
        assert "C1" in message
        assert "/opt/hermes_sandbox" in message
        assert "oneshot-sweep.sh" in message
        # The write itself did not happen.
        assert jobs.load_jobs() == []
    row, = ledger(home)
    assert row["outcome"] == "refused"
    # Every sandbox reference is listed, not just the first (C1 is a per-line fix).
    assert row["clauses"] == ["C1", "C1"]
    assert row["tool"] == "cron.create"
    assert row["script"] == "oneshot-sweep.sh"
    assert row["resolved"].endswith("/scripts/oneshot-sweep.sh")
    assert row["class"] == "preserved-shadow"


def test_shipped_carrier_row_is_allowed(home):
    seed(home, "deploy-train.sh", SHIPPED_SHIM)
    with store(home):
        job = create("deploy-train.sh")
        assert job["id"]
        assert [row["id"] for row in jobs.load_jobs()] == [job["id"]]
    row, = ledger(home)
    assert row["outcome"] == "clean"
    assert row["class"] == "registered-carrier"
    assert row["clauses"] == []


def test_python_worker_is_out_of_class_and_allowed(home):
    """A .py row that names the sandbox is not a carrier: reported, never refused."""
    seed(home, "db-registry-audit.py", PY_WORKER)
    with store(home):
        job = create("db-registry-audit.py")
    row, = ledger(home)
    assert row["outcome"] == "clean"
    assert row["class"] == "out-of-class"
    assert job["id"]


def test_worker_shell_row_is_not_refused(home):
    """The measured false-positive class: a `-shim.sh`-less worker is judged by C1 only."""
    seed(home, "board-store-backup.sh", "#!/bin/bash\ncd /Users/hermes_user || exit 0\necho backup\n")
    with store(home):
        job = create("board-store-backup.sh")
    row, = ledger(home)
    assert row["outcome"] == "clean"
    assert row["class"] == "out-of-class"
    assert job["id"]


def test_resolution_agrees_with_the_scheduler_and_the_judged_bytes_are_the_executed_bytes(home):
    """Criterion 3: the gate judges the file the scheduler runs - same path, same bytes."""
    from cron.scheduler_script import _resolve_script_path, _run_job_script

    written = seed(home, "agreement-probe.sh", RUNNABLE_SHADOW)
    scheduler_path, err = _resolve_script_path("agreement-probe.sh")
    assert err is None and scheduler_path == written

    with store(home):
        # Judged through the gate (C1: it names the sandbox, so the row is refused)...
        with pytest.raises(carrier_gate.CarrierRefused):
            create("agreement-probe.sh")
    row, = ledger(home)
    assert row["resolved"] == str(scheduler_path)

    # ...and the fire path still runs exactly those bytes, with the gate never consulted.
    ok, output = _run_job_script("agreement-probe.sh")
    assert ok, output
    assert "FIRED agreement-probe.sh" in output


def test_preexisting_shadow_row_still_fires(home):
    """Criterion 4: the gate is on the write path only; bytes already in the store run."""
    from cron.scheduler_script import _run_job_script

    seed(home, "oneshot-sweep.sh", RUNNABLE_SHADOW)
    preexisting = {
        "id": "deadbeef0001", "name": "legacy shadow", "prompt": "", "script": "oneshot-sweep.sh",
        "schedule": {"kind": "interval", "every": 3600, "display": "every 1h"},
        "enabled": True, "state": "scheduled",
    }
    with store(home):
        jobs.save_jobs([preexisting])
        assert [row["id"] for row in jobs.load_jobs()] == ["deadbeef0001"]
        ok, output = _run_job_script("oneshot-sweep.sh")
    assert ok, output
    assert "FIRED oneshot-sweep.sh" in output
    # A fire writes no ledger row: the gate is not on the run path.
    assert ledger(home) == []


def test_update_job_refuses_and_persists_nothing(home):
    seed(home, "oneshot-sweep.sh", SHADOW)
    with store(home):
        job = create(None, name="scriptless")
        assert job["script"] in (None, "")
        with pytest.raises(carrier_gate.CarrierRefused):
            jobs.update_job(job["id"], {"script": "oneshot-sweep.sh"})
        assert jobs.load_jobs()[0]["script"] in (None, "")
    row, = ledger(home)
    assert row["outcome"] == "refused" and row["tool"] == "cron.update"


def test_update_job_refuses_re_setting_the_same_shadow(home):
    """`hermes cron edit --script oversight-loop.sh` on a legacy row writes the same shadow value."""
    seed(home, "oneshot-sweep.sh", SHADOW)
    with store(home):
        jobs.save_jobs([{
            "id": "deadbeef0003", "name": "legacy", "prompt": "", "script": "oneshot-sweep.sh",
            "schedule": {"kind": "interval", "every": 3600, "display": "every 1h"},
            "enabled": False, "state": "paused",
        }])
        with pytest.raises(carrier_gate.CarrierRefused):
            jobs.update_job("deadbeef0003", {"script": "oneshot-sweep.sh"})
        assert jobs.load_jobs()[0]["script"] == "oneshot-sweep.sh"
    row, = ledger(home)
    assert row["outcome"] == "refused"


def test_fire_path_bookkeeping_updates_never_meet_the_gate(home):
    """The three fire-path update_job callers pass only bookkeeping fields.

    cron/scheduler.py:3058,3072, cron/scheduler_delivery.py:1296 and cron/monitor.py:176 all
    write delivery/monitor state on a row mid-fire. The card's hard constraint is that the
    gate lives on the WRITE path only: a row that predates the gate must still fire, so these
    shapes must pass a row whose script carries shadow bytes - and record nothing.
    """
    seed(home, "oneshot-sweep.sh", SHADOW)
    with store(home):
        jobs.save_jobs([{
            "id": "deadbeef0002", "name": "legacy", "prompt": "", "script": "oneshot-sweep.sh",
            "schedule": {"kind": "interval", "every": 3600, "display": "every 1h"},
            "enabled": True, "state": "scheduled",
        }])
        jobs.update_job("deadbeef0002", {"last_delivery_error": "gateway shutdown"})
        jobs.update_job("deadbeef0002", {"last_delivery_queued": None})
        jobs.update_job("deadbeef0002", {"monitor_state": {"last_output_hash": "abc"}})
        row = jobs.load_jobs()[0]
        assert row["script"] == "oneshot-sweep.sh"
        assert row["last_delivery_error"] == "gateway shutdown"
    assert ledger(home) == []


def test_unresolved_script_is_recorded_unverified_not_refused(home):
    with store(home):
        job = create("no-such-script.sh")
    row, = ledger(home)
    assert row["outcome"] == "unverified"
    assert "unresolved" in row["why"]
    assert job["id"]


def test_missing_control_plane_is_recorded_unverified_and_allows(home, monkeypatch, capsys):
    seed(home, "oneshot-sweep.sh", SHADOW)
    monkeypatch.setenv(carrier_gate.CONTROL_PLANE_ENV, str(home / "no-control-plane"))
    with store(home):
        job = create("oneshot-sweep.sh")
    row, = ledger(home)
    assert row["outcome"] == "unverified"
    assert "not readable" in row["why"]
    assert job["id"]
    assert "UNVERIFIED" in capsys.readouterr().err


def test_waiver_is_allowed_and_recorded(home, monkeypatch):
    seed(home, "oneshot-sweep.sh", SHADOW)
    monkeypatch.setenv(carrier_gate.GATE_ENV, "off")
    with store(home):
        job = create("oneshot-sweep.sh")
    row, = ledger(home)
    assert row["outcome"] == "waived"
    assert job["id"]


def test_monitor_script_is_gated_too(home):
    """A monitor_script is the same door one field over: the run path resolves it identically."""
    seed(home, "oneshot-sweep.sh", SHADOW)
    with store(home):
        with pytest.raises(carrier_gate.CarrierRefused):
            create(None, monitor_script="oneshot-sweep.sh", no_agent=False, prompt="canary")
    row, = ledger(home)
    assert row["field"] == "monitor_script"
    assert row["outcome"] == "refused"
