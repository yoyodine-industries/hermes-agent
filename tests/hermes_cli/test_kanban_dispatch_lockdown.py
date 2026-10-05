"""The lane-scoped DEFCON gate at the dispatch seam — the four cases the card names.

The rule: under a lockdown, admission is decided by the LANE (the profile), never by the
board the card happens to sit on. Each assertion below is therefore made on BOTH boards, and
each FAILS against the prior board-keyed form, which granted throughput by board: any card on
the exempt board ran, whatever lane the card needed.

* C1 a starved lane's card on the OPS board does not spawn
* C2 a starved lane's card on the DEFCON board does not spawn
* C3 an allowlisted lane spawns on the DEFCON board
* C4 an allowlisted lane spawns on the OPS board

Visibility is half the requirement: a starved card must RECORD why, naming the profile, or the
non-spawn is the silent failure that let three consecutive train failures go unseen.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent import estop
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture(autouse=True)
def operator_context(monkeypatch):
    """This suite arms holds, so it must not run inside a dispatched worker's inherited env."""
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv(DELEGATED_CHILD_ENV_MARKER, raising=False)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB and no live hold."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nobody")
    estop._logged_components.clear()
    kb.init_db()
    return home


def _arm_lockdown(profiles) -> str:
    # `actor=` is the ops head's own venue: a hold is recorded under the name its ACTOR is
    # entitled to wear (holder attribution), and this suite speaks for ops-head.
    return estop.acquire(
        owner="ops-head", actor="ops-head", mode=estop.MODE_LOCKDOWN, reason="ops window",
        allow={"profiles": list(profiles)})


def _spawn_recorder(spawns: list):
    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242

    return fake_spawn


def _events(conn, task_id: str, kind: str = "skipped_lockdown") -> list:
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
        (task_id, kind),
    ).fetchall()
    return [json.loads(row["payload"]) for row in rows]


# ── C1 + C2: a starved lane is refused wherever the card sits ───────────────


def test_a_starved_lane_is_refused_on_every_board(kanban_home, all_assignees_spawnable):
    kb.create_board("ops")
    _arm_lockdown(["default"])
    spawns = []

    with kbc.connect() as conn:
        defcon_tid = kb.create_task(conn, title="starved on defcon", assignee="yoyoflow")
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_recorder(spawns))
        assert res.spawned == []
        assert res.skipped_lockdown == [(defcon_tid, "yoyoflow")]
        assert kb.get_task(conn, defcon_tid).status == "ready", "refused BEFORE the claim"

    with kbc.connect(board="ops") as conn:
        ops_tid = kb.create_task(conn, title="starved on ops", assignee="yoyoflow")
        res = kbd.dispatch_once(conn, board="ops", spawn_fn=_spawn_recorder(spawns))
        assert res.spawned == []
        assert res.skipped_lockdown == [(ops_tid, "yoyoflow")], (
            "the ops board is NOT a bypass — a board-keyed form would have spawned this card")
        assert kb.get_task(conn, ops_tid).status == "ready"

    assert spawns == [], "neither board may spawn a starved lane's card"


# ── C3 + C4: an allowlisted lane is admitted wherever the card sits ─────────


def test_an_allowlisted_lane_spawns_on_both_boards(kanban_home, all_assignees_spawnable):
    kb.create_board("ops")
    _arm_lockdown(["default"])
    spawns = []

    with kbc.connect() as conn:
        defcon_tid = kb.create_task(conn, title="defcon card", assignee="default")
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_recorder(spawns))
        assert [t for t, _who, _ws in res.spawned] == [defcon_tid]
        assert res.skipped_lockdown == []

    with kbc.connect(board="ops") as conn:
        ops_tid = kb.create_task(conn, title="ops card", assignee="default")
        res = kbd.dispatch_once(conn, board="ops", spawn_fn=_spawn_recorder(spawns))
        assert [t for t, _who, _ws in res.spawned] == [ops_tid], (
            "the ops board is not the only place an admitted lane may run")
        assert res.skipped_lockdown == []

    assert sorted(spawns) == sorted([defcon_tid, ops_tid])


def test_the_typo_lane_is_held_even_on_the_board_it_was_meant_for(
    kanban_home, all_assignees_spawnable,
):
    """The 03:3x incident: ``--allow-profile platform-stll`` armed a stop that held the lane
    it meant to admit. The gate must fail CLOSED on the unknown id, on every board."""
    kb.create_board("ops")
    _arm_lockdown(["default", "platform-stll"])

    with kbc.connect(board="ops") as conn:
        tid = kb.create_task(conn, title="meant to be admitted", assignee="platform-stl")
        res = kbd.dispatch_once(conn, board="ops", spawn_fn=_spawn_recorder([]))
    assert res.spawned == []
    assert res.skipped_lockdown == [(tid, "platform-stl")]


# ── a total pause runs scoped: the floor spawns, everyone else is held ───────


def test_a_total_pause_runs_the_dispatcher_and_refuses_the_non_floor_lane(
    kanban_home, all_assignees_spawnable,
):
    """A total pause is NOT a whole-tick halt: the dispatcher RUNS and refuses per CARD lane,
    so the standing platform floor keeps spawning while every other lane's card is held and
    RECORDED on the card."""
    from gateway.kanban_watchers_common import _kanban_dispatch_allowed

    kb.create_board("ops")
    estop.acquire(owner="operator", mode=estop.MODE_ESTOP, reason="total halt")
    assert _kanban_dispatch_allowed() is True, "a total hold runs the dispatcher, scoped by lane"

    # A non-floor lane is refused per CARD, on the default board...
    with kbc.connect() as conn:
        held = kb.create_task(conn, title="total halt card", assignee="research-stl")
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_recorder([]))
        assert res.spawned == [] and res.skipped_lockdown == [(held, "research-stl")]

    # ...while a floor lane still spawns, on the ops board.
    spawns = []
    with kbc.connect(board="ops") as conn:
        floor_tid = kb.create_task(conn, title="total halt ops floor card", assignee="default")
        res = kbd.dispatch_once(conn, board="ops", spawn_fn=_spawn_recorder(spawns))
        assert [t for t, _who, _ws in res.spawned] == [floor_tid]
        assert res.skipped_lockdown == []
    assert spawns == [floor_tid]


def test_a_lockdown_keeps_the_tick_running(kanban_home):
    """A scoped lockdown — like a total hold — reaches the per-card gate; neither halts the
    dispatcher wholesale. Only a component with no floor work halts entirely."""
    from gateway.kanban_watchers_common import _kanban_dispatch_allowed

    _arm_lockdown(["default"])
    assert _kanban_dispatch_allowed() is True


# ── visible starvation: recorded, named, once per engagement ────────────────


def test_the_refusal_is_recorded_on_the_card_once_per_engagement(
    kanban_home, all_assignees_spawnable,
):
    handle = _arm_lockdown(["default"])
    engagement = estop.engagement_key()

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="starved", assignee="yoyoflow")
        for _ in range(3):
            res = kbd.dispatch_once(conn, spawn_fn=_spawn_recorder([]))
            assert res.skipped_lockdown == [(tid, "yoyoflow")]

        events = _events(conn, tid)
        assert len(events) == 1, "one record per card per engagement, never one per tick"
        assert events[0]["profile"] == "yoyoflow"
        assert events[0]["mode"] == estop.MODE_LOCKDOWN
        assert events[0]["engagement"] == engagement
        assert events[0]["owners"] == ["ops-head"]
        assert events[0]["allow_profiles"] == ["default"]

    # A RE-ARM is a new decision: it earns a fresh record.
    estop.release(handle=handle)
    _arm_lockdown(["default"])
    assert estop.engagement_key() != engagement

    with kbc.connect() as conn:
        kbd.dispatch_once(conn, spawn_fn=_spawn_recorder([]))
        events = _events(conn, tid)
        assert len(events) == 2
        assert events[1]["engagement"] == estop.engagement_key()


def test_dry_run_refuses_identically_and_writes_no_record(
    kanban_home, all_assignees_spawnable,
):
    _arm_lockdown(["default"])
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="starved", assignee="yoyoflow")
        res = kbd.dispatch_once(conn, dry_run=True)
        assert res.skipped_lockdown == [(tid, "yoyoflow")]
        assert _events(conn, tid) == []


# ── the held card is late, never lost ───────────────────────────────────────


def test_the_held_card_spawns_once_the_lift_lands(kanban_home, all_assignees_spawnable):
    handle = _arm_lockdown(["platform-stl"])
    spawns = []

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="held then freed", assignee="yoyoflow")
        assert kbd.dispatch_once(conn, spawn_fn=_spawn_recorder(spawns)).spawned == []

    assert estop.release(handle=handle).released is True

    with kbc.connect() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_recorder(spawns))
        assert [t for t, _who, _ws in res.spawned] == [tid]
        assert res.skipped_lockdown == []


def test_a_co_holders_scope_still_holds_the_lane(kanban_home, all_assignees_spawnable):
    """Lifting one hold must not resume what another still holds."""
    estop.acquire(owner="yoyoflow:critical-section", mode=estop.MODE_LOCKDOWN,
                  allow={"profiles": ["platform-stl"]})
    operator = estop.engage(reason="operator stop", mode=estop.MODE_LOCKDOWN,
                            allow={"profiles": ["default"]})

    with kbc.connect() as conn:
        starved = kb.create_task(conn, title="held by the co-holder", assignee="yoyoflow")
        admitted = kb.create_task(conn, title="admitted by the operator", assignee="default")
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_recorder([]))
        assert [t for t, _who, _ws in res.spawned] == [admitted]
        assert res.skipped_lockdown == [(starved, "yoyoflow")]

    estop.release(owner="operator")
    assert estop.is_engaged() is True
    assert operator.exists()


# ── the record is a property of the HOLD, not of the scan position ──────────
#
# The lane scan records a hold only for the candidates the tick actually VISITS, so every way
# a tick can stop before reaching a held card left it unrecorded: the cap, memory pressure,
# the ready lane's review reservation, a budget a higher-ranked admitted card consumed. The
# acceptance probe runs ONE tick — the very tick whose budget can swallow the scan — so each
# case below FAILS without the pre-budget sweep (§6.10).


def test_a_capped_tick_still_records_the_hold(kanban_home, all_assignees_spawnable):
    """The cap returns the tick before ANY candidate is scanned."""
    _arm_lockdown(["default"])
    spawns = []

    with kbc.connect() as conn:
        busy = kb.create_task(conn, title="busy", assignee="default")
        assert kb.claim_task(conn, busy) is not None
        held = kb.create_task(conn, title="starved behind the cap", assignee="yoyoflow")
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_recorder(spawns), max_spawn=1)

        assert res.spawned == [] and spawns == []
        assert res.skipped_lockdown == [(held, "yoyoflow")]
        events = _events(conn, held)
        assert len(events) == 1 and events[0]["profile"] == "yoyoflow"
        fetched = kb.get_task(conn, held)
        assert fetched is not None and fetched.status == "ready", "held, never lost"


def test_the_review_reservation_does_not_hide_a_hold(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """The reservation pins ``ready_budget`` to 0: the ready loop breaks before the scan."""
    import hermes_cli.config as cfgmod

    monkeypatch.setattr(
        cfgmod, "load_config", lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )
    _arm_lockdown(["default"])
    spawns = []

    with kbc.connect() as conn:
        held = kb.create_task(conn, title="starved behind the reservation", assignee="yoyoflow")
        # The held lane's own handoff, reviewed by an admitted lane: a review row with real
        # provenance, so the review loop really dispatches it (no hand-parked status).
        review = kb.create_task(conn, title="review me", assignee="yoyoflow")
        assert kb.request_review(conn, review, summary="handoff", reviewer="default") is True
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_recorder(spawns), max_spawn=1)

        assert [t for t, _who, _ws in res.spawned] == [review], "an admitted lane still spawns"
        assert res.skipped_lockdown == [(held, "yoyoflow")]
        assert len(_events(conn, held)) == 1


def test_a_budget_spent_before_the_held_card_still_records_the_hold(
    kanban_home, all_assignees_spawnable,
):
    """One shared slot, taken by a higher-ranked admitted card: the loop breaks on it."""
    _arm_lockdown(["default"])
    spawns = []

    with kbc.connect() as conn:
        admitted = kb.create_task(conn, title="admitted", assignee="default", priority=10)
        held = kb.create_task(conn, title="starved behind the budget", assignee="yoyoflow")
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_recorder(spawns), max_spawn=1)

        assert [t for t, _who, _ws in res.spawned] == [admitted]
        assert res.skipped_lockdown == [(held, "yoyoflow")]
        assert len(_events(conn, held)) == 1
# ── the record NAMES the board the tick ran against (card t_7ecf084a) ───────


def test_the_record_names_the_board_of_the_cli_tick(
    kanban_home, all_assignees_spawnable,
):
    """``hermes kanban --board <slug> dispatch`` scopes the board and calls the
    dispatcher with NO ``board=`` argument, so the tick reads the slug off the open
    store. The ``skipped_lockdown`` row must still name it — the defect was
    ``"board": null`` on every row written this way (card t_7ecf084a)."""
    import argparse

    from hermes_cli import kanban as kb_cli

    kb.create_board("ops")
    _arm_lockdown(["default"])

    with kb.scoped_current_board("ops"):
        with kbc.connect_closing() as conn:
            tid = kb.create_task(conn, title="held on ops", assignee="yoyoflow")
        # max=0 refuses every spawn; the lockdown sweep runs BEFORE the budget, so
        # the refusal is still recorded and nothing is actually started.
        rc = kb_cli._cmd_dispatch(
            argparse.Namespace(dry_run=False, max=0, failure_limit=2, json=False)
        )
        assert rc == 0
        with kbc.connect_closing() as conn:
            events = _events(conn, tid)

    assert events, "the held card must carry a skipped_lockdown row"
    assert events[-1]["board"] == "ops", (
        "the payload must name the board the CLI tick ran against"
    )


def test_the_record_names_the_board_without_a_board_flag(
    kanban_home, all_assignees_spawnable,
):
    """A board-scoped connection with no ``board=`` argument (the CLI path) names
    the board it is on; the default board names itself ``"default"``, not null."""
    kb.create_board("ops")
    _arm_lockdown(["default"])

    with kbc.connect(board="ops") as conn:
        ops_tid = kb.create_task(conn, title="held on ops", assignee="yoyoflow")
        kbd.dispatch_once(conn, spawn_fn=_spawn_recorder([]))
        assert _events(conn, ops_tid)[-1]["board"] == "ops"

    with kbc.connect() as conn:
        main_tid = kb.create_task(conn, title="held on default", assignee="yoyoflow")
        kbd.dispatch_once(conn, spawn_fn=_spawn_recorder([]))
        assert _events(conn, main_tid)[-1]["board"] == "default"
