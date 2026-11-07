"""A substrate-import death one second AFTER spawn is the tree's, not the card's (t_44d3bb59).

Measured instance (card ``t_3915f433``, an operator consent card at priority 999999)::

    spawned      pid 38635
    crashed      pid 38635  worker_output "python3: Error while finding module specification
                 for 'hermes_cli.main' (ModuleNotFoundError: No module named 'hermes_cli')"
    gave_up      {"failures": 1, "effective_limit": 1, ... "trigger_outcome": "crashed",
                  "sticky": true}

The spawn seam already classifies exactly that failure as ``substrate_import`` and excuses the card.
The crash path read only the EXIT STATUS, folded the worker's words into the error TEXT, and charged
the card -- so one broken interpreter consumed an operator card and parked it sticky for 33.3 h.

What is asserted here:

* the measured death, replayed through the real reclaim sweep, leaves ``consecutive_failures``
  unmoved, files no ``gave_up``, and books the run AND the event as
  ``infrastructure``/``substrate_import`` with the ``substrate_signature`` on both -- with the
  worker's own output kept, unchanged, as the evidence;
* the retry is spaced by ``check_respawn_guard``'s own ``substrate_cooldown`` -- the same branch the
  spawn seam uses, not a second cooldown ladder;
* a genuine card crash (a traceback in the CARD's workspace, no tree-import evidence) still spends
  the budget and still trips the breaker at ``DEFAULT_FAILURE_LIMIT``;
* the output reader never widens its reach: a missing THIRD-PARTY module stays the card's;
* three such deaths in one tick neither refuse the sweep (a substrate fault repeats every tick;
  refusing would freeze the rows as ``running`` with dead pids) nor go unrecorded -- the lane
  actor is filed once for the signature.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from hermes_cli import kanban_crash_sweep_guard as guard
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

#: The worker output measured on card t_3915f433, verbatim.
MEASURED_OUTPUT = (
    "python3: Error while finding module specification for 'hermes_cli.main' "
    "(ModuleNotFoundError: No module named 'hermes_cli')"
)
#: ``hermes_cli`` is a package THIS tree ships, so the death is the tree's.
MEASURED_SIGNATURE = "substrate_import:hermes_cli/__init__.py:hermes_cli"


# ---------------------------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB and no crash grace."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A scratch dispatching tree that ships ``hermes_cli`` -- never the real one."""
    root = tmp_path / "dispatch-tree"
    package = root / "hermes_cli"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "main.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    monkeypatch.setattr(kbd, "_dispatching_tree_root", lambda: root)
    return root


def _dead_pid() -> int:
    """A pid that is definitely not alive (scan downward for a number nothing owns)."""
    for candidate in range(99999, 90000, -1):
        if not kb._pid_alive(candidate):
            return candidate
    raise AssertionError("no free pid on this host")


def _comparable_fingerprint() -> str:
    """A worker-start fingerprint of the SAME identity generation as this process's writer."""
    shape = guard.identity_shape(kbd._process_fingerprint(os.getpid()))
    family = guard.shape_family(shape)
    if family == guard.IDENTITY_SHAPE_BOOT_WITNESS:
        return "bootsession:TEST|179000000000|1234"
    if family == guard.IDENTITY_SHAPE_COMPOSED:
        return "ref|1234"
    return "|179000000000"


def _claimed_running(conn, *, title="job", assignee="worker") -> str:
    """A ``running`` card with a dead worker pid and a backdated start."""
    tid = kb.create_task(conn, title=title, assignee=assignee)
    kb.claim_task(conn, tid)
    pid = _dead_pid()
    kbd._set_worker_pid(conn, tid, pid)
    old = int(time.time()) - 3600
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_started_at = ?, started_at = ?, claim_expires = ? WHERE id = ?",
            (_comparable_fingerprint(), old, old, tid),
        )
        conn.execute(
            "UPDATE task_runs SET started_at = ? WHERE id = "
            "(SELECT current_run_id FROM tasks WHERE id = ?)",
            (old, tid),
        )
    return tid


def _write_worker_log(tid: str, text: str) -> None:
    path = kb.worker_log_path(tid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _task_row(conn, tid):
    return conn.execute(
        "SELECT status, block_kind, consecutive_failures, last_failure_error FROM tasks WHERE id = ?",
        (tid,),
    ).fetchone()


def _runs(conn, tid):
    return conn.execute(
        "SELECT id, outcome, status, error, metadata FROM task_runs WHERE task_id = ? ORDER BY id",
        (tid,),
    ).fetchall()


def _event_count(conn, tid, kind) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM task_events WHERE task_id = ? AND kind = ?", (tid, kind),
    ).fetchone()
    return int(row["c"])


def _events(conn, tid, kind):
    return conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id", (tid, kind),
    ).fetchall()


# ---------------------------------------------------------------------------------------------
# DoD 1 -- the measured death, replayed
# ---------------------------------------------------------------------------------------------


def test_post_spawn_substrate_death_is_not_charged(kanban_home, tree, monkeypatch):
    """t_3915f433 replayed: the budget is untouched, no gave_up, signature on run and event."""
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    with kbc.connect() as conn:
        tid = _claimed_running(conn)
        _write_worker_log(tid, MEASURED_OUTPUT)

        reclaimed = kbd.detect_crashed_workers(conn)

        # Not a crash: the sweep's own return stays crashed-only, and the class has its
        # own visibility surface on the tick result.
        assert reclaimed == []
        assert getattr(kbd.detect_crashed_workers, "_last_substrate_import", []) == [tid]

        row = _task_row(conn, tid)
        assert row["status"] == "ready"
        assert row["block_kind"] is None
        assert row["consecutive_failures"] == 0          # the card's retry budget is UNTOUCHED
        assert row["last_failure_error"].startswith("tree-import-inconsistency:")
        assert "hermes_cli/__init__.py" in row["last_failure_error"]

        # No gave_up anywhere on the card: the breaker never saw this death.
        assert _event_count(conn, tid, "gave_up") == 0
        assert _event_count(conn, tid, "blocked") == 0

        runs = _runs(conn, tid)
        assert [r["outcome"] for r in runs] == ["spawn_failed"]
        metadata = json.loads(runs[0]["metadata"])
        assert metadata["infrastructure"] is True
        assert metadata["substrate_import"] is True
        assert metadata["substrate_signature"] == MEASURED_SIGNATURE
        assert metadata["worker_output"] == MEASURED_OUTPUT      # the evidence, unchanged

        # The EVENT carries the same class and the same key.
        events = _events(conn, tid, "spawn_failed")
        assert len(events) == 1
        payload = json.loads(events[0]["payload"])
        assert payload["infrastructure"] is True
        assert payload["substrate_import"] is True
        assert payload["substrate_signature"] == MEASURED_SIGNATURE
        assert payload["worker_output"] == MEASURED_OUTPUT

        # Spaced by the guard's OWN substrate branch -- the one the spawn seam already uses.
        assert kbd.check_respawn_guard(conn, tid) == "substrate_cooldown"

        # A second tick charges nothing either: the card is still retryable.
        kbd.detect_crashed_workers(conn)
        assert _task_row(conn, tid)["consecutive_failures"] == 0


def test_classifier_books_the_death_as_infrastructure(kanban_home, tree):
    """The driver-level pin: ``_classify_dead_worker`` itself, no sweep involved."""
    with kbc.connect() as conn:
        tid = _claimed_running(conn)
        _write_worker_log(tid, MEASURED_OUTPUT)
        before = _task_row(conn, tid)

        dead = kbd._classify_dead_worker(
            conn.execute("SELECT worker_pid FROM tasks WHERE id = ?", (tid,)).fetchone()[0],
            conn.execute("SELECT claim_lock FROM tasks WHERE id = ?", (tid,)).fetchone()[0],
            task_id=tid,
        )

        assert dead.substrate_import is True
        assert dead.substrate_signature == MEASURED_SIGNATURE
        assert dead.event_kind == "spawn_failed"
        assert dead.run_outcome == "spawn_failed"
        assert dead.event_payload["worker_output"] == MEASURED_OUTPUT
        assert dead.error_text.startswith("tree-import-inconsistency:")
        # Classifying books NOTHING: the card's budget is the same row afterwards.
        after = _task_row(conn, tid)
        assert after["consecutive_failures"] == before["consecutive_failures"]
        assert after["status"] == before["status"]

    # The SPAWN door is pinned by tests/hermes_cli/test_kanban_spawn_substrate_guard.py; here the
    # two doors are shown to agree on ONE real failure (see the next test).


def test_one_failure_two_doors_agree_on_class_and_key(tmp_path, monkeypatch):
    """The mirror half: the SAME failure read at spawn time and from the dead worker's text.

    A real ``ImportError`` (real frames, a real provider under the tree) is classified by the
    spawn seam; the traceback it renders is then read by the dead-worker door. One class, one
    signature -- which is the whole point: the asymmetry was that only one door existed.
    """
    tree = tmp_path / "probe-tree"
    package = tree / "probe_postspawn_pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "provider.py").write_text("PROBE_VALUE = 1\n", encoding="utf-8")
    (package / "importer.py").write_text(
        "from probe_postspawn_pkg.provider import PROBE_MISSING_SYMBOL\n", encoding="utf-8")
    monkeypatch.setattr(kbd, "_dispatching_tree_root", lambda: tree)

    import importlib
    import sys
    import traceback

    root = str(tree)
    if root not in sys.path:
        sys.path.insert(0, root)
    for name in ("probe_postspawn_pkg", "probe_postspawn_pkg.provider",
                 "probe_postspawn_pkg.importer"):
        sys.modules.pop(name, None)
    rendered = ""
    exc = None
    try:
        importlib.import_module("probe_postspawn_pkg.importer")
    except ImportError as failure:
        exc = failure
        rendered = traceback.format_exc()
    assert exc is not None, "the probe importer unexpectedly succeeded"

    expected = "substrate_import:probe_postspawn_pkg/provider.py:PROBE_MISSING_SYMBOL"
    assert kbd.classify_spawn_failure(exc, tree=tree) == kbd.SPAWN_FAILURE_SUBSTRATE_IMPORT
    assert kbd.substrate_import_signature(exc, tree=tree) == expected

    found = kbd.substrate_import_in_worker_output(rendered, tree=tree)
    assert found is not None
    signature, naming = found
    assert signature == expected
    assert naming.startswith(kbd.TREE_IMPORT_INCONSISTENCY)
    assert "probe_postspawn_pkg/provider.py" in naming


# ---------------------------------------------------------------------------------------------
# DoD 2 -- the control: a genuine card crash is still charged, and still trips
# ---------------------------------------------------------------------------------------------


CARD_CRASH = (
    "Traceback (most recent call last):\n"
    '  File "/private/tmp/card-workspace/app.py", line 7, in <module>\n'
    "    main()\n"
    "ValueError: the card's own bug\n"
)


def test_a_genuine_card_crash_still_trips_the_breaker(kanban_home, tree):
    """No tree-import evidence -> the card pays, and ``DEFAULT_FAILURE_LIMIT`` still trips."""
    assert kbd.DEFAULT_FAILURE_LIMIT == 2
    with kbc.connect() as conn:
        tid = _claimed_running(conn, title="card work")
        _write_worker_log(tid, CARD_CRASH)

        assert kbd.detect_crashed_workers(conn) == [tid]
        assert getattr(kbd.detect_crashed_workers, "_last_substrate_import", []) == []
        row = _task_row(conn, tid)
        assert row["consecutive_failures"] == 1
        assert row["status"] == "ready"
        assert _event_count(conn, tid, "gave_up") == 0
        assert _runs(conn, tid)[0]["outcome"] == "crashed"

        # Second identical death: the breaker trips exactly at the limit.
        kb.claim_task(conn, tid)
        pid = _dead_pid()
        kbd._set_worker_pid(conn, tid, pid)
        old = int(time.time()) - 3600
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET worker_started_at = ?, started_at = ?, claim_expires = ? WHERE id = ?",
                (_comparable_fingerprint(), old, old, tid),
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ? WHERE id = "
                "(SELECT current_run_id FROM tasks WHERE id = ?)",
                (old, tid),
            )
        _write_worker_log(tid, CARD_CRASH)

        assert kbd.detect_crashed_workers(conn) == [tid]
        row = _task_row(conn, tid)
        assert row["status"] == "blocked"
        assert row["consecutive_failures"] >= kbd.DEFAULT_FAILURE_LIMIT
        assert _event_count(conn, tid, "gave_up") == 1


def test_the_reader_never_widens_its_reach(tree):
    """Only a name THIS tree ships is attributed; everything else stays the card's."""
    # A missing third-party dependency: no source under the tree.
    assert kbd.substrate_import_in_worker_output(
        "ModuleNotFoundError: No module named 'definitely_not_installed_pkg_xyz'", tree=tree,
    ) is None
    # A ``cannot import name`` with no under-tree importer frame is not attributable either.
    assert kbd.substrate_import_in_worker_output(
        "ImportError: cannot import name 'PROBE' from 'hermes_cli.main'", tree=tree,
    ) is None
    # The importer frame IS under the tree -> the tree's own disagreement, with the same key
    # shape the exception detector produces.
    found = kbd.substrate_import_in_worker_output(
        "Traceback (most recent call last):\n"
        f'  File "{tree}/hermes_cli/main.py", line 3, in <module>\n'
        "    from hermes_cli.main import PROBE\n"
        "ImportError: cannot import name 'PROBE' from 'hermes_cli.main'",
        tree=tree,
    )
    assert found is not None
    assert found[0] == "substrate_import:hermes_cli/main.py:PROBE"
    # An ordinary traceback carries no import evidence at all.
    assert kbd.substrate_import_in_worker_output(CARD_CRASH, tree=tree) is None


# ---------------------------------------------------------------------------------------------
# Fleet shape -- three such deaths in one tick
# ---------------------------------------------------------------------------------------------


def test_three_deaths_neither_refuse_the_sweep_nor_go_unrecorded(kanban_home, tree):
    """A substrate fault repeats every tick: it must be released, not turned into a livelock."""
    with kbc.connect() as conn:
        ids = [_claimed_running(conn, title=f"job {i}", assignee="worker") for i in range(3)]
        for tid in ids:
            _write_worker_log(tid, MEASURED_OUTPUT)

        assert kbd.detect_crashed_workers(conn) == []      # nothing counted as an R4 closure
        assert sorted(getattr(kbd.detect_crashed_workers, "_last_substrate_import", [])) == sorted(ids)
        assert getattr(kbd.detect_crashed_workers, "_last_crash_sweep_abort", None) is None
        for tid in ids:
            row = _task_row(conn, tid)
            assert (row["status"], row["consecutive_failures"]) == ("ready", 0)
            assert _event_count(conn, tid, "gave_up") == 0

        # ...and the LANE is named: one acting card for the shared signature, not three.
        actors = conn.execute(
            "SELECT id, assignee FROM tasks WHERE idempotency_key LIKE 'substrate:%'"
        ).fetchall()
        assert len(actors) == 1
        assert actors[0]["assignee"] == kbd.SUBSTRATE_ACTOR_ASSIGNEE
        events = conn.execute(
            "SELECT payload FROM task_events WHERE kind = 'substrate_actor_filed'"
        ).fetchall()
        assert len(events) == 1
        payload = json.loads(events[0]["payload"])
        assert payload["signature"] == MEASURED_SIGNATURE
        assert sorted(payload["affected"]) == sorted(ids)
