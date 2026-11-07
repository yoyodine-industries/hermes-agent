"""The reclaim sweep may not act on a liveness substrate it cannot vouch for.

Ruling ``t_63a20c59`` (platform-stl, 2026-10-10), carrier ``t_fc2cf9ee``.

**R4.** A sweep that would close ``>= 3`` runs ``crashed`` in ONE tick is a host-systemic signature,
not N card failures. It must refuse the whole write and fail LOUD. The measured census this pins is
the regression fixture: on 2026-10-10 ALL 21 ``crashed`` runs on board ``migrations`` fell into three
instants (8 at ``ended_at`` 14:20:37, 5 at 13:35:34, 8 at 00:01:23), every one carrying
``pid N not alive``, and pid 70697 was measured ALIVE eight minutes after the 14:20:37 sweep and
still writing the live tree. Not one of the 21 was a genuine crash.

**R6.** The identity shape the dispatcher WRITES and the shape it READS are recorded and compared on
every tick, and a row whose fingerprint was built by a different identity generation is HELD rather
than compared (R2 ``UNKNOWN -> HOLD``). Three incomparable shapes were measured in circulation at
once: ``'|<abs-epoch-cs>'``, ``None`` -> ``'unverified'``, and
``bootsession:<uuid>|<ref_cs>|<boot-relative start>``.
"""

import json
import os
import time

import pytest

from hermes_cli import kanban_crash_sweep_guard as guard
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


# ---------------------------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    # The generation stamp must land in the sandbox home, never the machine-global one.
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        yield conn
    finally:
        conn.close()


def _dead_pid() -> int:
    """A pid that is definitely not alive (scan downward for a number nothing owns)."""
    for candidate in range(99999, 90000, -1):
        if not kb._pid_alive(candidate):
            return candidate
    raise AssertionError("no free pid on this host")


def _writer_shape() -> str:
    return guard.identity_shape(kbd._process_fingerprint(os.getpid()))


def _comparable_fingerprint() -> str:
    """A fingerprint of the SAME generation as this process's writer, with a live-looking start."""
    shape = _writer_shape()
    family = guard.shape_family(shape)
    if family == guard.IDENTITY_SHAPE_BOOT_WITNESS:
        return "bootsession:TEST|179000000000|1234"
    if family == guard.IDENTITY_SHAPE_COMPOSED:
        return "ref|1234"
    return "|179000000000"  # epoch-cs (macOS: current_instantiation_epoch() is "")


def _incomparable_fingerprint() -> str:
    """A fingerprint built by a DIFFERENT identity generation than this process's writer."""
    shape = _writer_shape()
    if guard.shape_family(shape) == guard.IDENTITY_SHAPE_BOOT_WITNESS:
        return "|179000000000"                                   # epoch-cs/2, a different family
    return "bootsession:TEST|179000000000|1234"                  # boot-witness/3, a different family


def _claimed_running(conn, *, pid, started_at, claim_expires=None) -> str:
    """A ``running`` card claimed by a host-local worker whose pid/fingerprint we control."""
    tid = kb.create_task(conn, title="job", assignee="worker")
    kb.claim_task(conn, tid)
    kbd._set_worker_pid(conn, tid, pid)
    old = int(time.time()) - 3600
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_started_at = ?, started_at = ?, claim_expires = ? WHERE id = ?",
            (started_at, old, old if claim_expires is None else claim_expires, tid),
        )
        conn.execute(
            "UPDATE task_runs SET started_at = ? WHERE id = "
            "(SELECT current_run_id FROM tasks WHERE id = ?)",
            (old, tid),
        )
    return tid


def _task_row(conn, tid):
    return conn.execute(
        "SELECT status, worker_pid, worker_started_at, claim_lock FROM tasks WHERE id = ?", (tid,),
    ).fetchone()


def _run_rows(conn, tid):
    return conn.execute(
        "SELECT id, status, outcome, ended_at FROM task_runs WHERE task_id = ?", (tid,),
    ).fetchall()


def _event_count(conn, tid, kind) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM task_events WHERE task_id = ? AND kind = ?", (tid, kind),
    ).fetchone()
    return int(row["c"])


def _closure(task_id, *, kind="unknown", shape=None, **kwargs) -> guard.SweepClosure:
    return guard.SweepClosure(
        task_id=task_id, pid=1234, kind=kind, shape=shape or "epoch-cs/2", **kwargs,
    )


# ---------------------------------------------------------------------------------------------
# R4 -- the count guard itself
# ---------------------------------------------------------------------------------------------


def test_the_threshold_defaults_to_three(monkeypatch):
    monkeypatch.delenv(guard.MASS_CRASH_ABORT_THRESHOLD_ENV, raising=False)
    assert guard.mass_crash_threshold() == 3


def test_the_threshold_env_below_two_disables_the_guard(monkeypatch):
    monkeypatch.setenv(guard.MASS_CRASH_ABORT_THRESHOLD_ENV, "0")
    assert guard.mass_crash_threshold() == 0
    monkeypatch.setenv(guard.MASS_CRASH_ABORT_THRESHOLD_ENV, "7")
    assert guard.mass_crash_threshold() == 7


def test_an_unparseable_threshold_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv(guard.MASS_CRASH_ABORT_THRESHOLD_ENV, "lots")
    assert guard.mass_crash_threshold() == 3


def test_two_unexplained_deaths_are_not_a_host_signature():
    assessment = guard.assess_sweep(
        [_closure("t1"), _closure("t2")], write_shape="epoch-cs/2", now=1791659000,
    )
    assert assessment.refusal_needed is False
    assert assessment.verdict.count == 2


def test_three_unexplained_deaths_in_one_tick_are_refused():
    assessment = guard.assess_sweep(
        [_closure("t1"), _closure("t2"), _closure("t3")], write_shape="epoch-cs/2", now=1791659000,
    )
    assert assessment.refusal_needed is True
    assert assessment.verdict.count == 3
    assert assessment.verdict.task_ids == ("t1", "t2", "t3")


def test_the_signature_is_a_count_and_a_timestamp_and_nothing_else():
    assessment = guard.assess_sweep(
        [_closure(f"t{i}") for i in range(8)], write_shape="epoch-cs/2", now=1791658837,
    )
    assert assessment.verdict.ended_at == 1791658837
    assert assessment.verdict.signature == "mass-crash-sweep:count=8,ended_at=1791658837"
    assert assessment.verdict.signature.startswith(guard.MASS_CRASH_SIGNATURE_PREFIX)


@pytest.mark.parametrize("kind", sorted(guard.UNEXPLAINED_DEATH_KINDS))
def test_every_unexplained_death_kind_is_counted(kind):
    assessment = guard.assess_sweep(
        [_closure(f"t{i}", kind=kind) for i in range(3)], write_shape="epoch-cs/2",
    )
    assert assessment.verdict.count == 3


@pytest.mark.parametrize(
    "kwargs",
    [
        {"protocol_violation": True},
        {"terminal_provider": True},
        {"rate_limited": True},
    ],
    ids=["protocol-violation", "terminal-provider", "rate-limited"],
)
def test_evidence_carrying_verdicts_are_not_counted(kwargs):
    """A clean exit, a provider refusal and a quota wall are not a broken liveness witness."""
    assessment = guard.assess_sweep(
        [_closure(f"t{i}", **kwargs) for i in range(5)], write_shape="epoch-cs/2",
    )
    assert assessment.verdict.count == 0
    assert assessment.refusal_needed is False


def test_a_mixed_sweep_counts_only_the_unexplained_deaths():
    closures = [
        _closure("a"), _closure("b"),
        _closure("clean", protocol_violation=True),
        _closure("quota", rate_limited=True),
    ]
    assessment = guard.assess_sweep(closures, write_shape="epoch-cs/2")
    assert assessment.verdict.task_ids == ("a", "b")
    assert assessment.refusal_needed is False


def test_the_operator_override_lets_a_genuine_mass_crash_through(monkeypatch):
    monkeypatch.setenv(guard.MASS_CRASH_ABORT_THRESHOLD_ENV, "0")
    assessment = guard.assess_sweep(
        [_closure(f"t{i}") for i in range(21)], write_shape="epoch-cs/2",
    )
    assert assessment.refusal_needed is False
    assert assessment.verdict.count == 21  # measured, reported, not refused


def test_one_tick_is_one_ended_at(monkeypatch):
    """The ruling's two spellings -- ">= 3 in one tick" and ">= 3 at one ended_at" -- are one
    measurement: a tick stamps every closure with the same instant."""
    assessment = guard.assess_sweep(
        [_closure(f"t{i}") for i in range(3)], write_shape="epoch-cs/2", now=1791658837,
    )
    assert assessment.verdict.ended_at == 1791658837
    # every counted closure shares the tick instant by construction (the sweep writes one `now`)
    assert assessment.verdict.task_ids == ("t0", "t1", "t2")


# ---------------------------------------------------------------------------------------------
# R4 -- the 2026-10-10 census is the regression fixture
# ---------------------------------------------------------------------------------------------


#: `ended_at` -> how many runs the 2026-10-10 sweep closed at that instant. All 21 carried the same
#: evidence line, `pid N not alive`.
CENSUS_20261010 = {1791658837: 8, 1791656134: 5, 1791648083: 8}  # 14:20:37, 13:35:34, 00:01:23


def test_the_20261010_census_is_refused_at_every_one_of_its_three_instants():
    assert sum(CENSUS_20261010.values()) == 21, "the measured census is 21 runs, not fewer"
    for instant, size in CENSUS_20261010.items():
        closures = [_closure(f"census-{instant}-{i}", kind="unknown") for i in range(size)]
        assessment = guard.assess_sweep(closures, write_shape="epoch-cs/2", now=instant)
        assert assessment.refusal_needed is True, f"instant {instant} closed {size} runs"
        assert assessment.verdict.count == size
        assert assessment.verdict.task_ids == tuple(f"census-{instant}-{i}" for i in range(size))


def test_the_census_evidence_line_is_the_liveness_verdict():
    """The census's own words: a liveness verdict masquerading as N card failures."""
    assert kbd._classify_dead_worker_exit(_dead_pid(), "yoyodine:1").error_text.endswith(
        "not alive"
    )


# ---------------------------------------------------------------------------------------------
# R6 -- shapes, the hold, and the generation stamp
# ---------------------------------------------------------------------------------------------


def test_identity_shape_names_the_structure_not_the_value():
    assert guard.identity_shape(None) == "none"
    assert guard.identity_shape("") == "none"
    assert guard.identity_shape("unverified") == "unverified"
    assert guard.identity_shape(179036519519) == "legacy-int"
    assert guard.identity_shape("179041285600") == "legacy-int"
    assert guard.identity_shape("|179036519519") == "epoch-cs/2"
    assert guard.identity_shape("bootsession:U|5458133") == "boot-witness/2"
    assert guard.identity_shape("bootsession:U|179160482300|5458133") == "boot-witness/3"


def test_the_three_circulating_shapes_are_distinguishable():
    """Measured 2026-10-10: three incomparable shapes were live at once."""
    versions = {
        "|179036519519": "epoch-cs/2",                                  # .venv 3.11.16 + psutil
        "unverified": "unverified",                                     # 3.14 gateway, no psutil
        "bootsession:6FE94283|179160482300|5458133": "boot-witness/3",  # the live rows
    }
    tokens = {guard.identity_shape(sample) for sample in versions}
    assert len(tokens) == 3


def test_a_cross_generation_pair_is_incomparable():
    assert guard.shapes_comparable("boot-witness/2", "boot-witness/3") is False
    assert guard.shapes_comparable("epoch-cs/2", "boot-witness/2") is False
    assert guard.shapes_comparable("boot-witness/2", "boot-witness/2") is True
    # A row with no fingerprint, and the UNVERIFIED marker, keep the bare-existence rule.
    assert guard.shapes_comparable("boot-witness/2", "none") is True
    assert guard.shapes_comparable("boot-witness/2", "unverified") is True
    assert guard.shapes_comparable("epoch-cs/2", "legacy-int") is True


def test_a_row_from_another_generation_is_held_and_not_counted():
    """R2 UNKNOWN -> HOLD at row granularity: an incomparable row is neither reclaimed nor counted."""
    closures = [
        _closure("comparable-1", shape="epoch-cs/2"),
        _closure("comparable-2", shape="epoch-cs/2"),
        _closure("comparable-3", shape="epoch-cs/2"),
        _closure("foreign", shape="boot-witness/3"),
    ]
    assessment = guard.assess_sweep(closures, write_shape="epoch-cs/2")
    assert assessment.held == ("foreign",)
    assert assessment.held_shapes == ("boot-witness/3->epoch-cs/2",)
    assert assessment.verdict.count == 3  # the held row is not one of them


def test_the_hold_survives_the_threshold_override(monkeypatch):
    """The count guard is policy; an incomparable comparison is a fact about the values."""
    monkeypatch.setenv(guard.MASS_CRASH_ABORT_THRESHOLD_ENV, "0")
    assessment = guard.assess_sweep(
        [_closure("foreign", shape="boot-witness/3")], write_shape="epoch-cs/2",
    )
    assert assessment.refusal_needed is False
    assert assessment.held == ("foreign",)


def test_first_tick_records_the_generation_and_later_ticks_do_not_alert(tmp_path):
    path = tmp_path / "stamp.json"
    first = guard.check_deployed_generation(sample="|179036519519", path=path, now=1000)
    assert first.recorded is True
    assert first.alerting is False
    second = guard.check_deployed_generation(sample="|179036519520", path=path, now=2000)
    assert second.alerting is False
    assert guard.read_generation_stamp(path)["sample"] == "|179036519520"


def test_a_shape_change_is_detected_in_one_tick(tmp_path):
    path = tmp_path / "stamp.json"
    guard.check_deployed_generation(sample="|179036519519", path=path, now=1000)
    tick = guard.check_deployed_generation(sample="bootsession:U|a|b", path=path, now=2000)
    assert tick.alerting is True
    assert tick.reasons == ["identity-shape-changed:epoch-cs/2->boot-witness/3"]


def test_losing_psutil_alerts(tmp_path):
    path = tmp_path / "stamp.json"
    guard.check_deployed_generation(sample="|1", psutil=True, path=path, now=1000)
    tick = guard.check_deployed_generation(sample="|1", psutil=False, path=path, now=2000)
    assert "psutil-lost" in tick.reasons


def test_an_interpreter_minor_move_alerts_and_a_patch_bump_does_not(tmp_path):
    path = tmp_path / "stamp.json"
    guard.check_deployed_generation(sample="|1", interpreter="3.11.16", path=path, now=1000)
    patch = guard.check_deployed_generation(sample="|1", interpreter="3.11.9", path=path, now=2000)
    assert patch.alerting is False
    minor = guard.check_deployed_generation(sample="|1", interpreter="3.14.7", path=path, now=3000)
    assert minor.reasons == ["interpreter-changed:3.11->3.14"]


def test_losing_a_witness_symbol_alerts_but_gaining_one_does_not(tmp_path):
    path = tmp_path / "stamp.json"
    guard.check_deployed_generation(sample="|1", witness_symbols=[], path=path, now=1000)
    # A fix landing: the symbols ARRIVE. Not a regression.
    landed = guard.check_deployed_generation(
        sample="|1", witness_symbols=["_worker_liveness", "WORKER_ALIVE"], path=path, now=2000,
    )
    assert landed.alerting is False
    # Then a tree write drops them again -- the recurring regression.
    lost = guard.check_deployed_generation(sample="|1", witness_symbols=[], path=path, now=3000)
    assert lost.reasons == ["witness-bytes-lost:WORKER_ALIVE,_worker_liveness"]


def test_a_diverging_tick_keeps_reporting_and_does_not_overwrite_the_reference(tmp_path):
    path = tmp_path / "stamp.json"
    guard.check_deployed_generation(sample="|1", path=path, now=1000)
    for moment in (2000, 3000, 4000):
        tick = guard.check_deployed_generation(sample="bootsession:U|a|b", path=path, now=moment)
        assert tick.alerting is True
    assert guard.read_generation_stamp(path)["sample"] == "|1"  # still the reference


def test_an_unreadable_stamp_is_treated_as_absent(tmp_path):
    path = tmp_path / "stamp.json"
    path.write_text("{ not json", encoding="utf-8")
    tick = guard.check_deployed_generation(sample="|1", path=path, now=1000)
    assert tick.recorded is True
    assert tick.alerting is False


def test_witness_symbol_presence_reads_the_executing_module():
    present = guard.witness_symbols_present(kbd)
    for name in present:
        assert name in guard.WITNESS_SYMBOLS
        assert hasattr(kbd, name)
    # The tree has none of them today; the check reports that rather than guessing.
    assert isinstance(present, list)


# ---------------------------------------------------------------------------------------------
# the acceptance test: a synthetic tick that would close >= 3 runs writes NOTHING and alerts
# ---------------------------------------------------------------------------------------------


def test_a_synthetic_tick_that_would_close_three_runs_writes_nothing_and_alerts(board):
    conn = board
    fingerprint = _comparable_fingerprint()
    dead = _dead_pid()
    ids = [_claimed_running(conn, pid=dead, started_at=fingerprint) for _ in range(3)]
    before = {tid: _run_rows(conn, tid) for tid in ids}

    crashed = kbd.detect_crashed_workers(conn)

    # 1. NOTHING was closed.
    assert crashed == []
    for tid in ids:
        row = _task_row(conn, tid)
        assert row["status"] == "running", f"{tid} was released by a refused sweep"
        assert row["worker_pid"] == dead
        assert row["claim_lock"] is not None
        assert _run_rows(conn, tid) == before[tid], f"{tid}'s run was ended by a refused sweep"
    assert kbd.detect_crashed_workers._last_auto_blocked == []

    # 2. The decision is a count and a timestamp.
    abort = kbd.detect_crashed_workers._last_crash_sweep_abort
    assert abort is not None
    assert abort.count == 3
    assert abort.threshold == 3
    assert abort.signature.startswith("mass-crash-sweep:count=3,ended_at=")
    assert abort.task_ids == tuple(ids)

    # 3. It produces an alert: one acting card for the design authority ...
    actors = conn.execute(
        "SELECT id, assignee, title FROM tasks WHERE idempotency_key LIKE 'mass-crash:%'",
    ).fetchall()
    assert len(actors) == 1, "exactly one acting card per signature"
    assert actors[0]["assignee"] == guard.MASS_CRASH_ACTOR_ASSIGNEE
    assert "REFUSED" in actors[0]["title"]

    # 4. ... and per-card evidence, without touching any status.
    for tid in ids:
        assert _event_count(conn, tid, guard.MASS_CRASH_ABORT_EVENT) == 1
        assert _task_row(conn, tid)["status"] == "running"


def test_the_alert_is_idempotent_inside_the_hour_bucket(board, monkeypatch):
    conn = board
    # The seam stamps the TICK's own clock, so the clock must MOVE between the two ticks: two
    # ticks inside the same second mint the same `ended_at`, which makes a key built from the
    # tick's signature accidentally stable -- a wiring regression (passing `verdict.signature`
    # as the key again) would still file one card and this test would stay green. Advance one
    # dispatcher tick (63 s), staying inside the same hour bucket, so only a tick-INVARIANT key
    # can produce the one card the condition is entitled to.
    clock = {"now": _TICK_INSTANT}
    monkeypatch.setattr(time, "time", lambda: clock["now"])

    fingerprint = _comparable_fingerprint()
    dead = _dead_pid()
    for _ in range(3):
        _claimed_running(conn, pid=dead, started_at=fingerprint)

    kbd.detect_crashed_workers(conn)
    first = kbd.detect_crashed_workers._last_crash_sweep_abort
    clock["now"] = _TICK_INSTANT + _TICK_SECONDS
    kbd.detect_crashed_workers(conn)
    later = kbd.detect_crashed_workers._last_crash_sweep_abort

    # Two DIFFERENT instants, one hour bucket, one unchanged condition ...
    assert first is not None and later is not None
    assert first.ended_at != later.ended_at
    assert first.ended_at // _HOUR_SECONDS == later.ended_at // _HOUR_SECONDS
    assert first.count == later.count

    # ... and the card must NOT multiply.
    rows = conn.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE idempotency_key LIKE 'mass-crash:%'",
    ).fetchone()
    assert int(rows["c"]) == 1, "a persistent condition pages once per hour, not once per tick"


# ---------------------------------------------------------------------------------------------
# The acting card's dedupe key is the CONDITION: `<prefix><board>`
#
# Three generations of this key carried a READING. #182's predecessor keyed on `ended_at` (the
# dispatcher ticks every ~63 s, so every tick minted a key: one card per tick). #182 dropped
# `ended_at` but kept `count`, which moves inside one episode (measured 2026-11-02: 14 cards in 13
# minutes). 713e1e18 keyed on the episode's SHAPE inside an hour bucket, which restocks one card per
# tick-HOUR for a condition that never changed (measured on the board, card t_e217fc62).
#
# The operator ruling (2026-11-01) is that a standing card is keyed on the CONDITION and everything
# that varies per pass is the READING. So the key is the board alone, and the readings -- count,
# shape, tick instant -- live in the body, the per-tick event payload and ONE thread line per
# CHANGED reading. A card that finished is REOPENED, never duplicated and never silently swallowed.
# ---------------------------------------------------------------------------------------------

_TICK_INSTANT = 1791659000  # an arbitrary tick instant; the clock is not part of any key
_TICK_SECONDS = 63  # the dispatcher's measured tick interval
_HOUR_SECONDS = 3600


def _refused_sweep(conn, count, *, now):
    """File the actor card for a REFUSED sweep of ``count`` crashed rows at ``now``."""
    task_ids = [kb.create_task(conn, title="job", assignee="worker") for _ in range(count)]
    assessment = guard.assess_sweep(
        [_closure(tid) for tid in task_ids],
        write_shape="epoch-cs/2",
        now=now,
    )
    assert assessment.verdict.abort is True, "the fixture must produce a refusal"
    actor_id = guard.alert_sweep_guard(conn, assessment, tree="fixture-tree", now=now)
    assert actor_id is not None
    return assessment, actor_id


def _mass_crash_actors(conn):
    return conn.execute(
        "SELECT id, idempotency_key, title, body FROM tasks "
        "WHERE idempotency_key LIKE 'mass-crash:%' ORDER BY rowid",
    ).fetchall()


def _repeat_payloads(conn, task_id):
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY rowid",
        (task_id, guard.MASS_CRASH_SWEEP_REFUSED_AGAIN_EVENT),
    ).fetchall()
    return [json.loads(row["payload"]) for row in rows]


def _reading_lines(conn, task_id):
    """The reading lines this guard threaded onto a card, in order (the marker, then the reading)."""
    rows = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? ORDER BY created_at, id", (task_id,),
    ).fetchall()
    readings = []
    for row in rows:
        for line in str(row["body"] or "").splitlines():
            if guard._READING_MARKER in line:
                readings.append(line.split(guard._READING_MARKER, 1)[1].strip().split()[0])
    return readings


def test_one_episode_files_one_card_however_the_count_and_clock_move(board):
    """11 -> 5 -> 3 across two hours is ONE condition: one card, keyed on the board alone."""
    conn = board
    first, first_id = _refused_sweep(conn, 11, now=_TICK_INSTANT)
    second, second_id = _refused_sweep(conn, 5, now=_TICK_INSTANT + _TICK_SECONDS)
    third, third_id = _refused_sweep(conn, 3, now=_TICK_INSTANT + _HOUR_SECONDS)

    # The ticks differ in both the clock and the count, so the recorded signatures all differ ...
    assert len({first.verdict.signature, second.verdict.signature, third.verdict.signature}) == 3
    assert first.verdict.ended_at != third.verdict.ended_at
    assert (first.verdict.count, second.verdict.count, third.verdict.count) == (11, 5, 3)

    # ... and the card must NOT: one condition, one card, across the hour boundary too.
    assert {second_id, third_id} == {first_id}
    actors = _mass_crash_actors(conn)
    assert len(actors) == 1, "a sustained refusal is ONE card, not one per tick or per hour"
    key = actors[0]["idempotency_key"]
    assert "ended_at=" not in key, "the dedupe key must never carry the tick instant"
    assert "count=" not in key, "the dedupe key must never carry a count that moves inside a condition"
    assert key == "mass-crash:refused:default", key

    # The first tick filed the card, so THAT is the tick the card's own words report.
    assert "REFUSED: >= 11 runs" in actors[0]["title"]
    assert ">= 5 runs" not in actors[0]["title"]

    # The later ticks cost no cards but are not lost: each appends its own facts to that one card,
    # so the operator still sees 11 -> 5 -> 3 and the tick instants behind them.
    repeats = _repeat_payloads(conn, first_id)
    assert [p["count"] for p in repeats] == [5, 3]
    assert [p["ended_at"] for p in repeats] == [
        _TICK_INSTANT + _TICK_SECONDS, _TICK_INSTANT + _HOUR_SECONDS,
    ]
    assert all("held" in p and "held_shapes" in p for p in repeats)

    # And the THREAD carries the readings -- the one the card was filed on, then one line per
    # CHANGED count, never per tick.
    assert _reading_lines(conn, first_id) == ["count=11", "count=5", "count=3"]


def test_a_repeat_leaves_the_existing_card_in_whatever_state_it_holds(board):
    """The repeat trail is evidence, never a status change: a claimed card keeps its claim."""
    conn = board
    _, first_id = _refused_sweep(conn, 3, now=_TICK_INSTANT)
    conn.execute(
        "UPDATE tasks SET status = 'running', worker_pid = 4242, claim_lock = 'held' "
        "WHERE id = ?",
        (first_id,),
    )
    conn.commit()

    _, repeat_id = _refused_sweep(conn, 5, now=_TICK_INSTANT + _TICK_SECONDS)

    assert repeat_id == first_id
    row = conn.execute(
        "SELECT status, worker_pid, claim_lock FROM tasks WHERE id = ?", (first_id,),
    ).fetchone()
    assert (row["status"], row["worker_pid"], row["claim_lock"]) == ("running", 4242, "held")
    assert [p["count"] for p in _repeat_payloads(conn, first_id)] == [5]


def test_the_next_hour_does_NOT_file_a_second_card(board):
    """DELIBERATE INVERSION of the old hour-bucket rule (card t_e217fc62).

    This test used to assert that a tick in the NEXT hour filed a second card, because the hour
    bucket was part of the key. That rule is what restocked the board: one unchanged condition held
    ``mass-crash:mass-crash-sweep:count=49:platform:1793509200`` and its neighbours for as long as
    it lasted. The clock is not the condition, so the same refusal an hour later lands on the SAME
    card, with nothing deduped into it (the reading did not move), and there is still exactly ONE
    mass-crash card on the board.
    """
    conn = board
    _, first_id = _refused_sweep(conn, 3, now=_TICK_INSTANT)
    _, next_hour_id = _refused_sweep(conn, 3, now=_TICK_INSTANT + _HOUR_SECONDS)

    assert next_hour_id == first_id, "the hour is a reading, never part of the key"
    actors = _mass_crash_actors(conn)
    assert len(actors) == 1, "one condition, one card, however many hours it spans"
    assert actors[0]["idempotency_key"] == "mass-crash:refused:default"
    # The tick is still evidence: it lands in the event trail (payload.ended_at), not in a card.
    assert [p["ended_at"] for p in _repeat_payloads(conn, first_id)] == [
        _TICK_INSTANT + _HOUR_SECONDS,
    ]
    # An unchanged reading adds NO thread line: a sustained condition stays quiet.
    assert _reading_lines(conn, first_id) == ["count=3"]


def test_a_second_board_gets_its_own_card(board):
    """One card per board: a refusal on two boards is two conditions, not one."""
    conn = board
    task_ids = [kb.create_task(conn, title="job", assignee="worker") for _ in range(3)]
    assessment = guard.assess_sweep(
        [_closure(tid) for tid in task_ids], write_shape="epoch-cs/2", now=_TICK_INSTANT,
    )
    assert assessment.verdict.abort is True
    first_id = guard.alert_sweep_guard(
        conn, assessment, tree="fixture-tree", board="platform", now=_TICK_INSTANT,
    )
    second_id = guard.alert_sweep_guard(
        conn, assessment, tree="fixture-tree", board="other", now=_TICK_INSTANT,
    )

    assert first_id and second_id and first_id != second_id
    keys = {row["idempotency_key"] for row in _mass_crash_actors(conn)}
    assert keys == {"mass-crash:refused:platform", "mass-crash:refused:other"}


def test_a_HOLD_and_a_REFUSAL_on_one_board_are_two_conditions(board):
    """The key names the condition's KIND: a hold must not hide behind a refusal's card.

    Both are crash-sweep escalations on the same board, but they ask for different work (a hold
    says the liveness witness cannot be compared; a refusal says too many runs died at once). If
    they shared a key, whichever filed first would own the only card and the other condition's
    title, count and threshold would never reach the board.
    """
    conn = board
    dead = _dead_pid()
    _claimed_running(conn, pid=dead, started_at=_incomparable_fingerprint())
    kbd.detect_crashed_workers(conn)                       # files the HOLD actor
    _, refused_id = _refused_sweep(conn, 3, now=_TICK_INSTANT)

    keys = {row["idempotency_key"] for row in _mass_crash_actors(conn)}
    assert keys == {"mass-crash:held:default", "mass-crash:refused:default"}, keys
    hold_id = conn.execute(
        "SELECT id FROM tasks WHERE idempotency_key = 'mass-crash:held:default'",
    ).fetchone()["id"]
    assert hold_id != refused_id


def test_a_DONE_actor_card_is_reopened_when_the_condition_recurs(board):
    """A finished card still HOLDS its key, so a recurrence must re-arm it, not be swallowed.

    The kernel's dedupe fence is ``status != 'archived'``: a `done` card answers the existing-card
    lookup, so without this the condition pages ONCE in its life, a later escalation resolves to a
    card nobody is working, and the board looks served (card t_e217fc62).
    """
    conn = board
    _, actor_id = _refused_sweep(conn, 3, now=_TICK_INSTANT)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
            (_TICK_INSTANT, actor_id),
        )

    _, again_id = _refused_sweep(conn, 9, now=_TICK_INSTANT + _HOUR_SECONDS)

    assert again_id == actor_id, "the SAME card carries the recurring condition"
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (actor_id,)).fetchone()
    assert str(row["status"]) not in guard._TERMINAL_ACTOR_STATUSES, row["status"]
    assert len(_mass_crash_actors(conn)) == 1
    # The new reading is on the thread, so the reader sees 3 -> 9 on the card that was reopened.
    assert _reading_lines(conn, actor_id) == ["count=3", "count=9"]


def test_the_actor_card_still_evidence_the_tick_it_came_from(board):
    conn = board
    assessment, actor_id = _refused_sweep(conn, 3, now=_TICK_INSTANT)

    row = conn.execute("SELECT body FROM tasks WHERE id = ?", (actor_id,)).fetchone()
    assert f"tick instant (`ended_at`): {_TICK_INSTANT}" in row["body"]
    assert f"signature: `{assessment.verdict.signature}`" in row["body"]

    events = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
        (actor_id, guard.MASS_CRASH_ACTOR_EVENT),
    ).fetchall()
    assert len(events) == 1
    payload = json.loads(events[0]["payload"])
    assert payload["ended_at"] == _TICK_INSTANT
    assert payload["signature"] == assessment.verdict.signature
    assert payload["count"] == 3


def test_a_sweep_below_the_threshold_still_reclaims_normally(board):
    conn = board
    fingerprint = _comparable_fingerprint()
    dead = _dead_pid()
    ids = [_claimed_running(conn, pid=dead, started_at=fingerprint) for _ in range(2)]

    crashed = kbd.detect_crashed_workers(conn)

    assert sorted(crashed) == sorted(ids)
    for tid in ids:
        assert _task_row(conn, tid)["status"] != "running"
    assert kbd.detect_crashed_workers._last_crash_sweep_abort is None


def test_a_row_from_another_identity_generation_is_held_not_reclaimed(board):
    """The measured 2026-10-10 defect, at one row: the fingerprint is not comparable, so HOLD."""
    conn = board
    dead = _dead_pid()
    tid = _claimed_running(conn, pid=dead, started_at=_incomparable_fingerprint())

    crashed = kbd.detect_crashed_workers(conn)

    assert crashed == []
    row = _task_row(conn, tid)
    assert row["status"] == "running"
    assert row["worker_pid"] == dead
    assert kbd.detect_crashed_workers._last_crash_sweep_held == [tid]
    # A hold still alerts, because it means the dispatcher WRITES one shape and READS another.
    assert _event_count(conn, tid, guard.MASS_CRASH_ABORT_EVENT) == 0  # no refusal: nothing refused
    assert kbd.detect_crashed_workers._last_crash_sweep_abort is None


def test_a_held_sweep_alerts_without_claiming_a_refusal(board):
    """A hold is not a refusal: the acting card must not read "REFUSED: >= 0 runs"."""
    conn = board
    dead = _dead_pid()
    _claimed_running(conn, pid=dead, started_at=_incomparable_fingerprint())

    kbd.detect_crashed_workers(conn)

    actors = conn.execute(
        "SELECT title FROM tasks WHERE idempotency_key LIKE 'mass-crash:%'",
    ).fetchall()
    assert len(actors) == 1, "a held row still pages the design authority"
    title = actors[0]["title"]
    assert "HOLDING" in title
    assert "REFUSED" not in title


def test_the_reclaim_reports_the_refusal_on_the_dispatch_result(board, monkeypatch):
    """The tick itself carries the refusal, so a caller never has to read the log."""
    conn = board
    fingerprint = _comparable_fingerprint()
    dead = _dead_pid()
    # A claim that has NOT expired, so the sweep reaches the crash path instead of the TTL reclaim.
    future = int(time.time()) + 3600
    for _ in range(3):
        _claimed_running(conn, pid=dead, started_at=fingerprint, claim_expires=future)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    result = kbd._dispatch_once_locked(conn, ttl_seconds=None, stale_timeout_seconds=0)

    assert result.crashed == []
    assert result.crash_sweep_refused["count"] == 3
    assert result.crash_sweep_refused["threshold"] == 3
    assert result.crash_sweep_refused["signature"].startswith("mass-crash-sweep:count=3,ended_at=")


def test_the_tick_stamps_the_deployed_generation_once(board, tmp_path):
    conn = board
    result = kbd._dispatch_once_locked(conn, ttl_seconds=None, stale_timeout_seconds=0)
    assert result.identity_generation_alerts == []
    stamp = guard.read_generation_stamp()
    assert stamp is not None
    assert stamp["shape"] == _writer_shape()
    assert stamp["version"] == guard.GENERATION_STAMP_VERSION


def _foreign_shape() -> str:
    """An identity shape THIS process's writer does NOT produce -- a genuinely moved reference.

    Never hardcode one: the executing tree may already produce it (measured 2026-10-11: the live
    line's writer shape IS ``boot-witness/3``), which is exactly how this check went
    green-but-blind the moment the witness bytes landed.
    """
    writer = _writer_shape()
    for candidate in (
        "epoch-cs/2", "boot-witness/2", "boot-witness/3", "boot-witness/4", "composed/2",
    ):
        if candidate != writer:
            return candidate
    raise AssertionError("no foreign shape available; the writer vocabulary moved")


def _foreign_witness_symbols() -> list[str]:
    """A recorded witness set the executing module does NOT cover, derived the same way.

    Records what the tree HAS plus one symbol it does not, so the loss is real whether or not the
    witness bytes have landed yet.
    """
    present = sorted(guard.witness_symbols_present(kbd))
    absent = [name for name in sorted(guard.WITNESS_SYMBOLS) if name not in present]
    return present + (absent[:1] or ["worker_liveness_v0"])


@pytest.mark.parametrize(
    "witness_present", [True, False], ids=["witness-bytes-present", "witness-bytes-absent"],
)
def test_the_tick_alerts_when_the_deployed_generation_moves(
    board, tmp_path, monkeypatch, witness_present,
):
    """A regression is detected in ONE tick -- the whole point of R6.

    Parameterised over the witness-bytes state: the fixture must be RED without the R6 mechanism
    and GREEN with it in BOTH trees, because hardcoding a "foreign" generation the executing tree
    itself produces is how this test goes green-but-blind.
    """
    conn = board
    if not witness_present:
        # The pre-witness tree: the executing module carries none of the witness symbols.
        monkeypatch.setattr(guard, "witness_symbols_present", lambda module: [])
    kbd._dispatch_once_locked(conn, ttl_seconds=None, stale_timeout_seconds=0)
    stamp = guard.read_generation_stamp()
    if stamp is None:
        pytest.fail("the tick recorded no identity generation: the R6 leg is absent")
    stamp["shape"] = _foreign_shape()                        # the reference moves under the tick
    stamp["witness_symbols"] = _foreign_witness_symbols()    # ... and it recorded bytes we lost
    guard.write_generation_stamp(stamp)

    result = kbd._dispatch_once_locked(conn, ttl_seconds=None, stale_timeout_seconds=0)

    assert result.identity_generation_alerts, "a moved generation must alert in ONE tick"
    assert any(
        reason.startswith("identity-shape-changed:") for reason in result.identity_generation_alerts
    )
    assert any(
        reason.startswith("witness-bytes-lost:") for reason in result.identity_generation_alerts
    )
