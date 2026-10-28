"""The substrate admission gate: probe classification, episode identity, and the three doors.

Card ``t_0ae49984`` (leg A), design ``t_72e9bf03`` (Design Authority ``platform-stl``).

What this file is allowed to prove, and what it deliberately cannot:

* **Classification is a unit.** ``EPERM``/``EACCES``/``EROFS`` on a REAL ``open(2)``/``write(2)`` is
  a DENIAL; ``EMFILE`` and friends are INDETERMINATE and never refuse (I4); and the F1 shape --
  ``stat`` answers fine while ``open`` is refused -- must land as DENIED, because that is the exact
  fault that fooled every existence check for 2 h 20 m.
* **Identity is the EPISODE.** The same cause with a different wall clock, different pid and
  different millisecond timings is ONE episode with ONE streak; a cleared-then-returned cause is a
  NEW window.
* **The doors hold, they do not fail.** Door 3 (per unit) leaves the card ``ready`` with no claim
  and no failure counter and counts it on the record as ``held``; the cron door 3 does the same per
  job, keeps readable siblings in the tick (I2), and skips-and-advances only past the map's own
  ``hold_catchup_hours``.

Everything runs on a sandbox home: ``HERMES_HOME``/``HERMES_KANBAN_HOME`` point at ``tmp_path``, so
the record lands in the scratch dir and the fleet's board and live record are unreachable.
"""

from __future__ import annotations

import builtins
import errno
import json
import os
import stat
import time

import pytest

from cron import scheduler_tick as tick
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import substrate_access as sa


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """A private home + a cold probe cache, so no reading is inherited from another test."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setattr(sa, "map_path", lambda path=None: tmp_path / "substrate_roots.yaml")
    sa.clear_cache()
    yield tmp_path
    sa.clear_cache()


@pytest.fixture
def denied_dir(tmp_path):
    """A real directory that refuses ``open(2)`` with EACCES for a non-root process."""
    target = tmp_path / "denied"
    target.mkdir()
    (target / "sentinel.txt").write_text("x", encoding="utf-8")
    os.chmod(target, 0)
    try:
        yield target
    finally:
        os.chmod(target, 0o755)


@pytest.fixture
def ok_dir(tmp_path):
    target = tmp_path / "readable"
    (target / "data").mkdir(parents=True)
    (target / "data" / "sentinel.txt").write_text("x", encoding="utf-8")
    return target


def _spec(path, caps=("r", "w"), tier="unit"):
    return sa.RootSpec(
        path=str(path), caps=tuple(caps), tier=tier, owner="test", label="test root",
    )


def _mk_reading(path, *, op="open", errno_name=None, ok=False, tier="unit", ms=0.5):
    return sa.RootReading(
        path=str(path), op=op, ok=ok, errno=errno_name, ms=ms, tier=tier,
    )


def _report(*readings, at=1_000.0, findings=None):
    return sa.ProbeReport(
        at=at, readings=list(readings), cached=False, findings=list(findings or []),
    )


# ------------------------------------------------------------------ classification (T1)

def test_a_real_refused_open_is_a_denial(denied_dir):
    readings = sa.probe(_spec(denied_dir))

    assert readings, "the probe must take at least one real reading"
    refusals = [r for r in readings if not r.ok]
    assert refusals, f"chmod 000 must refuse a real operation, got {[r.as_matrix_row() for r in readings]}"
    assert all(r.denied for r in refusals)
    assert {r.errno for r in refusals} <= {"EACCES", "EPERM"}


def test_a_readable_root_is_ok(ok_dir):
    readings = sa.probe(_spec(ok_dir))

    assert readings
    assert all(r.ok for r in readings), [r.as_matrix_row() for r in readings]


def test_emfile_is_indeterminate_and_never_refuses(tmp_path):
    """I4: transient failures are evidence, never a refusal -- a full fd table must not stop work."""
    reading = _mk_reading(tmp_path, errno_name="EMFILE")
    assert reading.verdict == "indeterminate"
    assert not reading.denied

    report = _report(reading)
    assert report.indeterminate == [reading]
    assert sa.refusal_for(report, tier="unit") is None
    assert sa.refusal_for(report, tier="global") is None


def test_f1_stat_answers_ok_while_open_is_refused(denied_dir, monkeypatch):
    """The measured fault: existence checks pass, the OPERATION is refused.

    This is the shape that made ``stat``/``exists``/``isdir`` a false green for 2 h 20 m, so the
    gate must decide on ``open(2)`` -- and it must still decide DENIED when a path ``os.stat``
    answers for is refused by the kernel.
    """
    assert os.stat(denied_dir).st_mode  # stat answers, exactly as it did during the fault
    assert os.path.isdir(str(denied_dir))

    real_scandir = os.scandir

    def _scandir(path, *args, **kwargs):
        if str(path).startswith(str(denied_dir)):
            raise PermissionError(errno.EPERM, "Operation not permitted", str(path))
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", _scandir)
    readings = sa.probe(_spec(denied_dir))

    assert any(r.denied and r.errno == "EPERM" for r in readings), [
        r.as_matrix_row() for r in readings
    ]


def test_only_the_global_tier_refuses_the_whole_tick(tmp_path):
    unit = _mk_reading(tmp_path, errno_name="EPERM", tier="unit")
    glob = _mk_reading(tmp_path, errno_name="EPERM", tier="global")

    assert sa.refusal_for(_report(unit), tier="global") is None
    assert sa.refusal_for(_report(unit), tier="unit"), "a unit denial holds its own unit"
    assert sa.refusal_for(_report(glob), tier="global")


def test_an_advisory_denial_refuses_nothing(tmp_path):
    advisory = _mk_reading(tmp_path, errno_name="EROFS", tier="advisory")
    assert sa.refusal_for(_report(advisory), tier="global") is None
    assert sa.refusal_for(_report(advisory), tier="unit") is None


# ------------------------------------------------------------------ identity + record

def test_signature_ignores_the_wall_clock_and_latency(tmp_path):
    """One cause is one signature: same path, same op, same errno -- whatever the timing."""
    first = _mk_reading(tmp_path, errno_name="EPERM", ms=0.001)
    later = _mk_reading(tmp_path, errno_name="EPERM", ms=9.5)

    assert sa.signature(_report(first, at=1.0)) == sa.signature(_report(later, at=99_999.0))
    assert sa.signature(_report(first, at=1.0))


def test_three_distinct_causes_are_three_signatures(tmp_path):
    a = _mk_reading(tmp_path / "a", errno_name="EPERM")
    b = _mk_reading(tmp_path / "b", errno_name="EROFS")
    c = _mk_reading(tmp_path / "c", op="write", errno_name="EACCES")

    signatures = {sa.signature(_report(r)) for r in (a, b, c)}
    assert len(signatures) == 3


def test_n_ticks_absorb_into_one_episode(tmp_path):
    report = _report(_mk_reading(tmp_path, errno_name="EPERM"))

    payloads = [sa.record(report, door="dispatcher", now=100.0 + i)[1] for i in range(3)]

    assert payloads[-1]["yielded"] is True
    assert payloads[-1]["streak"] == 3
    assert payloads[-1]["first_tick"] == 100.0
    assert len({p["episode"] for p in payloads}) == 1
    assert payloads[-1]["matrix"]


def test_clearing_writes_the_recovery_receipt_and_a_new_window_mints_a_new_key(tmp_path):
    report = _report(_mk_reading(tmp_path, errno_name="EPERM"))
    _, open_payload = sa.record(report, door="dispatcher", now=100.0)

    _, cleared = sa.record_cleared(door="dispatcher", released=["t_card"], now=160.0)

    assert cleared["yielded"] is False
    assert cleared["cleared_at"] is not None
    assert cleared["duration_s"] == 60.0
    assert cleared["released"] == ["t_card"]
    assert sa.read_status()["yielded"] is False

    _, again = sa.record(report, door="dispatcher", now=500.0)
    assert again["episode"] != open_payload["episode"], (
        "a stall that cleared and came back is a NEW window, never the old one's key"
    )


def test_a_unit_hold_is_counted_without_claiming_the_door_is_yielded(tmp_path):
    """Door 3 on a green global tier: the unit is named in ``held``, the door stays open."""
    report = _report(_mk_reading(tmp_path, errno_name="EPERM", tier="unit"))

    _, payload = sa.record(
        report, door="dispatcher", yielded=False,
        held=[{"task": "t_x", "root": str(tmp_path), "errno": "EPERM", "op": "open"}],
    )

    assert payload["yielded"] is False
    assert payload["held"][0]["task"] == "t_x"


# ------------------------------------------------------------------ door 3: the dispatcher

@pytest.fixture
def board(tmp_path):
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        yield conn
    finally:
        conn.close()


def _card(conn, *, workspace_path=None):
    """A card the dispatcher is willing to CLAIM: ``ready`` (created blocked, then promoted)."""
    task_id = kb.create_task(
        conn, title="needs a workspace", assignee="alice",
        workspace_kind="dir" if workspace_path else "scratch",
        workspace_path=str(workspace_path) if workspace_path else None,
        initial_status="blocked",
    )
    ok, why = kb.promote_task(conn, task_id, actor="test")
    assert ok, why
    return task_id


def _row(conn, task_id):
    return conn.execute(
        "SELECT status, claim_lock, claim_expires, consecutive_failures, worker_pid "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()


def test_a_card_on_a_denied_root_is_held_not_failed(board, denied_dir):
    """Door 3: not claimed, not failed, not blocked -- untouched -- and counted as held."""
    task_id = _card(board, workspace_path=denied_dir)
    before = _row(board, task_id)

    held = kbd._hold_substrate_denied(board, task_id)

    assert held is True, "a card whose own root is refused must be held"
    row = _row(board, task_id)
    assert row["status"] == before["status"], "a held card is NOT consumed, failed or blocked"
    assert not row["claim_lock"] and not row["claim_expires"]
    assert (row["consecutive_failures"] or 0) == 0, "I1: a hold never charges the card a failure"
    kinds = [r["kind"] for r in board.execute(
        "SELECT kind FROM task_events WHERE task_id = ?", (task_id,),
    ).fetchall()]
    assert kbd.SUBSTRATE_ADMISSION_HELD_EVENT in kinds
    comments = board.execute(
        "SELECT body FROM task_comments WHERE task_id = ?", (task_id,),
    ).fetchall()
    assert any("substrate" in (c["body"] or "").lower() for c in comments)


def test_a_readable_card_on_the_same_board_is_admitted(board, denied_dir, ok_dir):
    """I2: the blast radius is the unit. A sibling on a readable root passes the same door."""
    held_id = _card(board, workspace_path=denied_dir)
    free_id = _card(board, workspace_path=ok_dir)

    assert kbd._hold_substrate_denied(board, held_id) is True
    assert kbd._hold_substrate_denied(board, free_id) is False


def test_the_hold_is_recorded_once_per_episode_not_once_per_tick(board, denied_dir):
    task_id = _card(board, workspace_path=denied_dir)

    for _ in range(3):
        assert kbd._hold_substrate_denied(board, task_id) is True

    events = board.execute(
        "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ? AND kind = ?",
        (task_id, kbd.SUBSTRATE_ADMISSION_HELD_EVENT),
    ).fetchone()["n"]
    assert events == 1, "the dedupe is by payload: the same denial must not re-file every tick"
    record = sa.read_status()
    assert record["yielded"] is False, "a per-unit hold must not read as a whole-tick refusal"
    assert any(entry.get("task") == task_id for entry in record.get("held") or [])


def test_a_dry_run_hold_reports_but_writes_nothing(board, denied_dir):
    task_id = _card(board, workspace_path=denied_dir)

    assert kbd._hold_substrate_denied(board, task_id, dry_run=True) is True

    events = board.execute(
        "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ? AND kind = ?",
        (task_id, kbd.SUBSTRATE_ADMISSION_HELD_EVENT),
    ).fetchone()["n"]
    assert events == 0, "dry_run must not write the hold event"
    assert not (sa.read_status() or {}).get("held")


# ------------------------------------------------------------------ door 3: cron, per job

class _FakeSched:
    """The only thing ``_split_jobs_by_substrate`` needs from the cron scheduler module."""

    def __init__(self, lock_dir):
        self._lock_dir = lock_dir

    def _get_lock_paths(self):
        return (self._lock_dir, self._lock_dir / "cron.lock")


def test_a_cron_job_on_a_denied_root_is_held_and_not_advanced(denied_dir, ok_dir, tmp_path):
    sched = _FakeSched(tmp_path)
    held_job = {"id": "j-held", "name": "on the denied volume", "workdir": str(denied_dir)}
    free_job = {"id": "j-free", "name": "readable", "workdir": str(ok_dir)}

    admitted, held, overdue = tick._split_jobs_by_substrate(sched, [held_job, free_job])

    assert [j["id"] for j in admitted] == ["j-free"]
    assert [e["job"] for e in held] == ["j-held"]
    assert overdue == []
    assert held[0]["errno"] in {"EACCES", "EPERM"}
    assert held[0]["overdue_s"] is None


def test_an_absolute_script_path_is_probed_too(denied_dir, tmp_path):
    sched = _FakeSched(tmp_path)
    job = {"id": "j-script", "script": str(denied_dir / "run.sh")}

    admitted, held, _ = tick._split_jobs_by_substrate(sched, [job])

    assert admitted == []
    assert [e["job"] for e in held] == ["j-script"]
    assert str(denied_dir) in " ".join(held[0]["roots"])


def test_a_job_older_than_the_catchup_window_is_skipped_and_advanced(denied_dir, tmp_path):
    """A three-day outage must not burst-fire fifty DAGs into the recovery window."""
    sched = _FakeSched(tmp_path)
    import time as _time

    job = {
        "id": "j-stale",
        "workdir": str(denied_dir),
        "next_run_at": _time.time() - 7 * 3600,  # past the 6 h default
    }

    admitted, held, overdue = tick._split_jobs_by_substrate(sched, [job])

    assert [j["id"] for j in admitted] == ["j-stale"], "it advances (fires) rather than holding"
    assert held == []
    assert [e["job"] for e in overdue] == ["j-stale"]


def test_cron_substrate_denied_is_a_sibling_of_the_yield(denied_dir):
    from cron.scheduler import CronSubstrateDenied, CronTickYielded

    report = _report(_mk_reading(denied_dir, errno_name="EPERM", tier="global"))
    exc = CronSubstrateDenied(report)

    assert isinstance(exc, RuntimeError)
    assert not isinstance(exc, CronTickYielded), "the two refusals stay distinguishable"
    assert "substrate admission" in str(exc)
    assert "No job was advanced or marked failed" in str(exc)


def test_the_gate_is_off_when_the_kill_switch_is_set(monkeypatch):
    """Rollback step 1: the guard becomes a recorder and refuses nothing."""
    monkeypatch.setenv("HERMES_SUBSTRATE_GATE", "off")
    import importlib

    reloaded = importlib.reload(sa)
    try:
        assert reloaded.gate_enabled() is False
    finally:
        monkeypatch.delenv("HERMES_SUBSTRATE_GATE", raising=False)
        importlib.reload(sa)


# ---------------------------------------------------------------------------------------
# The ACTING CARD's body: it renders a PRE-leg-B record instead of dying on it
# ---------------------------------------------------------------------------------------

BLIND_ROWS = [
    {
        "hook": "guard-shared-trees",
        "row": "pre_tool_call[0]:write_file",
        "body": "/opt/hermes_prod/yaan-majordomo/ops-checks/guard-shared-trees.py",
        "since": "2026-10-26T16:02:00-04:00",
        "reason": "EPERM",
    },
    {
        "hook": "guard-shared-trees",
        "row": "pre_tool_call[1]:patch",
        "body": "/opt/hermes_prod/yaan-majordomo/ops-checks/guard-shared-trees.py",
        "since": "2026-10-26T16:02:00-04:00",
        "reason": "EPERM",
    },
]


def _record_document(**overrides):
    """A DENIED authority record. LEG A wrote ``guards_blind`` as the ROW LIST; leg B writes a count."""
    now = time.time()
    record = {
        "version": 2,
        "yielded": True,
        "kind": "substrate_denied",
        "door": "dispatcher",
        "sig": "cafe",
        "streak": 5,
        "first_tick": now - 600,
        "last_tick": now - 5,
        "threshold": 3,
        "episode": "cafe-1",
        "delivery": None,
        "matrix": [
            {
                "path": "/opt/hermes_prod/yaan-platform",
                "op": "open",
                "ok": False,
                "errno": "EPERM",
                "ms": 0.4,
                "tier": "global",
            }
        ],
        "held": [],
        "guards_blind": list(BLIND_ROWS),
        "guards_total": 5,
        "read_findings": [],
        "cleared_at": None,
        "duration_s": None,
        "recorded_at": now,
    }
    record.update(overrides)
    return record


def _write_record(**overrides):
    target = sa.status_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(_record_document(**overrides)), encoding="utf-8")
    return target


def _yielded(report=None):
    return kbd.DispatcherTickYielded(
        "sig-boot",
        "sig-disk",
        boot_pid=os.getpid(),
        kind=kbd.SUBSTRATE_ADMISSION_KIND,
        report=report,
    )


def _body(yielded=None):
    return kbd._substrate_admission_body(
        yielded or _yielded(),
        board="platform",
        status_path="/tmp/dispatcher_tick.json",
        streak=5,
        first_tick=1_700_000_000,
        last_tick=1_700_000_100,
        filed_on="platform",
        episode="cafe-1",
    )


def _units_section(body: str) -> str:
    """The ``## The affected units`` section alone, so a row assertion cannot be met by the matrix."""
    start = body.index("## The affected units")
    end = body.index("\n## ", start + 1)
    return body[start:end]


def test_the_affected_units_name_a_dispatcher_held_card():
    """The dispatcher's door 3 counts a CARD: ``{task, root, errno, op}`` and no ``unit`` key.

    Card ``t_0b56c6a8``: the row loop read ``unit``/``kind``/``reason`` -- keys no writer has
    ever emitted -- so the count line said 1 and the row under it said ``None (None): None``.
    """
    _write_record(held=[{
        "task": "t_abc123",
        "root": "/opt/hermes_prod/yaan-platform",
        "errno": "EPERM",
        "op": "open",
    }])
    section = _units_section(_body())

    assert "**1**" in section, "the count line was already right and stays right"
    assert "t_abc123" in section, "the held CARD is the unit a human must be able to name"
    assert "/opt/hermes_prod/yaan-platform" in section, "the root that answered the denial"
    assert "EPERM" in section and "open" in section, "the cause, as the probe measured it"
    assert "(None)" not in section


def test_the_affected_units_name_a_cron_held_job():
    """The cron door counts a JOB: ``{job, name, roots, errno, op, overdue_s}``, roots in a LIST."""
    _write_record(held=[{
        "job": "cron-42",
        "name": "nightly-train",
        "roots": ["/opt/hermes_prod/dag-a", "/opt/hermes_prod/dag-b"],
        "errno": "EPERM",
        "op": "readdir",
        "overdue_s": 7200,
    }])
    section = _units_section(_body())

    assert "**1**" in section
    for expected in ("cron-42", "nightly-train", "/opt/hermes_prod/dag-a", "/opt/hermes_prod/dag-b"):
        assert expected in section, f"the held job's identity/location must be named: {expected}"
    assert "EPERM" in section and "readdir" in section
    assert "(None)" not in section


def test_a_held_list_naming_both_writer_shapes_reads_each_row_on_its_own():
    """A held list naming BOTH door shapes reads each row on its own -- a robustness arm.

    Not a shape any writer produces today, and the name says ``list`` for that reason:
    ``substrate_access.record`` writes ``list(held or prior.get("held") or [])``, so a NON-EMPTY
    ``held`` REPLACES the prior list and the only carry-forward path (a writer passing no
    ``held``) reproduces the prior rows verbatim -- no record mixes the two doors today. The rows
    do arrive per door (dispatcher cards, cron jobs) and a future writer may differ from both, so
    the renderer is asserted against a list it has not been given before.
    """
    _write_record(held=[
        {"task": "t_card1", "root": "/opt/hermes_prod/x", "errno": "EPERM", "op": "open"},
        {"job": "cron-7", "name": "nightly", "roots": ["/opt/hermes_prod/y"],
         "errno": "EPERM", "op": "readdir", "overdue_s": 60},
    ])
    section = _units_section(_body())

    assert "**2**" in section
    assert "t_card1" in section and "cron-7" in section
    assert "/opt/hermes_prod/x" in section and "/opt/hermes_prod/y" in section
    assert "(None)" not in section


def test_an_unrecognised_held_row_renders_its_own_keys_and_a_non_dict_row_still_counts():
    """A shape nobody has written yet must read as itself, never as ``None`` -- and a row the
    reader cannot parse is still a HELD UNIT, so the count must not shrink to hide it."""
    _write_record(held=[{"whatever": 1}, "not-a-dict"])
    section = _units_section(_body())

    assert "**2**" in section, "a non-dict row is counted, not silently dropped"
    assert "whatever=1" in section, "an unknown shape is legible instead of blank"
    assert "not-a-dict" in section
    assert "(None)" not in section


def test_a_long_root_list_is_capped_and_the_elision_is_counted():
    """The matrix rows are bounded; a held row's roots are bounded the same way, and the elision
    is a NUMBER -- a truncated list that does not say it was truncated is the same lie."""
    roots = [f"/opt/hermes_prod/r{i}" for i in range(10)]
    _write_record(held=[{"job": "cron-9", "name": "n", "roots": roots,
                         "errno": "EPERM", "op": "readdir"}])
    section = _units_section(_body())

    assert "/opt/hermes_prod/r0" in section
    assert "/opt/hermes_prod/r9" not in section, "the list is capped, not printed whole"
    assert "+2 more" in section, "the reader is told the list was cut, and by how much"


def test_the_acting_body_renders_a_pre_leg_b_row_list_as_its_count():
    """The body is written INSIDE a live refusal, so it must not raise on a legacy record.

    Leg A wrote ``guards_blind`` as the row list; leg B (this branch) writes an int and
    keeps the rows in ``guards``. A bare ``int()`` on the old shape raised TypeError in the
    one path whose whole job is to explain the stall -- 2026-10-26, after which the hold was
    recorded (``held``, episode, matrix) and no card carried it: 33 units queued unread.
    """
    _write_record()
    body = _body()

    assert "**2 of 5" in body, "the list-shaped count is the number of blind rows"
    assert "guard-shared-trees" in body, "the legacy rows are the detail the card exists to carry"
    assert "EPERM" in body

    # A record that carries BOTH keys, the modern one empty, must still find the legacy rows.
    _write_record(guards=[], guards_blind=list(BLIND_ROWS))
    body_both = _body()
    assert "**2 of 5" in body_both
    assert "guard-shared-trees" in body_both


def test_the_acting_body_renders_the_shipped_count_shape_unchanged():
    _write_record(guards_blind=2, guards=list(BLIND_ROWS))
    body = _body()
    assert "**2 of 5" in body
    assert "guard-shared-trees" in body


@pytest.mark.parametrize(
    "overrides",
    [
        {"guards_blind": None, "guards_total": None},
        {"guards_blind": "2", "guards_total": "5"},
        {"guards_blind": 0, "guards_total": 0},
        {"guards_blind": [], "guards_total": 5},
        {"guards_blind": [{"row": "x"}], "held": [{"unit": "u"}, "not-a-dict"]},
    ],
)
def test_the_acting_body_never_raises_on_a_malformed_record(overrides):
    """A record is written by another process; a body that trusts its types loses the card."""
    _write_record(**overrides)
    body = _body()
    assert "Guards blind" in body
