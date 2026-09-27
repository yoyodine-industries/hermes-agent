"""The host worker budget is SHARED across boards, not raced (D1/D3 follow-up).

``kanban.max_in_progress`` is HOST-wide: every board draws from one budget and
``tick_once`` used to hand the free slots to whatever board it reached first, in
filesystem order. A board with a deep queue took every slot it could and a board
with one waiting card lost every race — the observed "ops starved while defcon
drained" symptom (2026-09-27) — and nothing said so, because the loser had no
refusal of its own.

These tests pin the whole policy:

* the rank: head-of-line priority, ties by board name, rankless boards LAST,
* the allocation: one guaranteed slot per board with work, remainder to the head,
* the rotation: the board left out this tick is served within ``len(boards)-1``,
* the probe: one read-only connect per board, a failure is a ranking miss,
* the report: a board the budget keeps deferring is logged, not silent.

The rank itself against a real board DB is exercised here too: this lineage has
no age-aware effective priority, so the head card's own ``priority`` IS the
order the board would spawn in, and that is what the probe returns.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import kanban_watchers_dispatcher as kwd
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def board_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB and spawnable assignees."""
    from hermes_cli import profiles

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    kb.init_db()
    return home


def _settings(**overrides):
    values = dict(
        interval=60.0,
        max_spawn=None,
        max_in_progress=None,
        failure_limit=2,
        stale_timeout_seconds=0,
        reconcile_orphans=True,
        default_assignee=None,
        max_in_progress_per_profile=None,
    )
    values.update(overrides)
    return kwd._DispatcherSettings(**values)


def _stub_dispatcher(**overrides):
    """A dispatcher over a stub ``kb`` — the probe and the tick are stubbed too."""
    return kwd._KanbanDispatcher(SimpleNamespace(DEFAULT_BOARD="default"), _settings(**overrides))


# ---------------------------------------------------------------------------
# The rank
# ---------------------------------------------------------------------------


def test_the_board_that_would_spawn_first_is_visited_first():
    assert kwd.order_boards_by_head_priority(
        {"ops": 1, "research": 4, "financially": 2}
    ) == ["research", "financially", "ops"]


def test_equal_heads_are_ordered_by_board_name():
    """Deterministic: the same fleet ranks the same way every tick."""
    assert kwd.order_boards_by_head_priority({"ops": 3, "zzz": 3, "aaa": 3}) == [
        "aaa",
        "ops",
        "zzz",
    ]


def test_unrankable_boards_are_visited_last_and_never_dropped():
    """Nothing spawnable (or a failed probe) means "no rank", not "no tick"."""
    assert kwd.order_boards_by_head_priority(
        {"ops": None, "aaa": None, "research": 2}
    ) == ["research", "aaa", "ops"]


# ---------------------------------------------------------------------------
# The allocation
# ---------------------------------------------------------------------------


def test_every_board_with_work_gets_a_slot_before_the_head_gets_the_rest():
    assert kwd.host_budget_shares(4, ["defcon", "ops"]) == {"defcon": 3, "ops": 1}


def test_more_boards_with_work_than_slots_leaves_the_tail_without_one():
    assert kwd.host_budget_shares(2, ["a", "b", "c"]) == {"a": 1, "b": 1}


def test_an_unknown_free_budget_hands_out_no_shares():
    """Uncapped, or a count that could not be read: no allocation was made."""
    assert kwd.host_budget_shares(None, ["a", "b"]) == {}
    assert kwd.host_budget_shares(0, ["a", "b"]) == {}


def test_a_single_board_takes_the_whole_budget():
    assert kwd.host_budget_shares(4, ["defcon"]) == {"defcon": 4}


# ---------------------------------------------------------------------------
# The wiring: rank, allocate, rotate, report
# ---------------------------------------------------------------------------


def test_tick_once_visits_the_ranked_board_first_and_still_visits_the_rest(monkeypatch):
    disp = _stub_dispatcher()
    monkeypatch.setattr(disp, "_board_slugs", lambda: ["ops", "research", "financially"])
    # "financially" has nothing spawnable — the probe returns None for it.
    monkeypatch.setattr(
        disp, "head_of_line_priority", lambda slug: {"ops": 1, "research": 5}.get(slug)
    )
    visited: list = []
    monkeypatch.setattr(
        disp,
        "tick_once_for_board",
        lambda slug, host_budget_share=None: visited.append(slug) or f"res:{slug}",
    )

    results = disp.tick_once()

    assert visited == ["research", "ops", "financially"]
    assert results == [
        ("research", "res:research"),
        ("ops", "res:ops"),
        ("financially", "res:financially"),
    ]


def test_tick_once_hands_each_board_its_share_of_the_free_host_budget(monkeypatch):
    """ACCEPTANCE: a deep queue no longer takes the slots another board needs.

    cap 4, two boards with work: the head board keeps the surplus (3) and the
    second board still gets a slot (1) — before this it drew nothing.
    """
    disp = _stub_dispatcher(max_in_progress=4)
    monkeypatch.setattr(disp, "_board_slugs", lambda: ["defcon", "ops"])
    monkeypatch.setattr(disp, "head_of_line_priority", lambda slug: 4 if slug == "defcon" else 1)
    monkeypatch.setattr(kbd, "total_running_all_boards", lambda: 0)
    seen: dict = {}
    monkeypatch.setattr(
        disp,
        "tick_once_for_board",
        lambda slug, host_budget_share=None: seen.setdefault(slug, host_budget_share),
    )

    disp.tick_once()

    assert seen == {"defcon": 3, "ops": 1}


def test_the_free_budget_backs_the_shares_off_by_every_running_worker(monkeypatch):
    """Free = cap - running: three workers already going leave one slot, not four."""
    disp = _stub_dispatcher(max_in_progress=4)
    monkeypatch.setattr(disp, "_board_slugs", lambda: ["defcon", "ops"])
    monkeypatch.setattr(disp, "head_of_line_priority", lambda slug: 1)
    monkeypatch.setattr(kbd, "total_running_all_boards", lambda: 3)
    seen: dict = {}
    monkeypatch.setattr(
        disp,
        "tick_once_for_board",
        lambda slug, host_budget_share=None: seen.setdefault(slug, host_budget_share),
    )

    disp.tick_once()

    assert seen == {"defcon": 1, "ops": 0}


def test_the_board_left_out_this_tick_is_served_within_a_board_or_two(monkeypatch):
    """ACCEPTANCE: 3 boards with work, 2 slots — every board gets a turn.

    One board must sit out a tick (the budget cannot cover three), but the
    rotation makes it a DIFFERENT board next tick, so no board waits twice.
    """
    disp = _stub_dispatcher(max_in_progress=2)
    monkeypatch.setattr(disp, "_board_slugs", lambda: ["aaa", "bbb", "ccc"])
    monkeypatch.setattr(disp, "head_of_line_priority", lambda slug: 1)
    monkeypatch.setattr(kbd, "total_running_all_boards", lambda: 0)
    served: list = []
    monkeypatch.setattr(
        disp,
        "tick_once_for_board",
        lambda slug, host_budget_share=None: served.append((slug, host_budget_share)),
    )

    left_out = []
    for _ in range(3):
        served.clear()
        disp.tick_once()
        left_out.append({slug for slug, share in served if share == 0})

    assert left_out == [{"ccc"}, {"aaa"}, {"bbb"}]


def test_a_board_with_nothing_to_start_draws_no_share(monkeypatch):
    """A rankless board is still visited, it just cannot hold a slot for later."""
    disp = _stub_dispatcher(max_in_progress=4)
    monkeypatch.setattr(disp, "_board_slugs", lambda: ["defcon", "ops"])
    monkeypatch.setattr(disp, "head_of_line_priority", lambda slug: 4 if slug == "defcon" else None)
    monkeypatch.setattr(kbd, "total_running_all_boards", lambda: 0)
    seen: dict = {}
    monkeypatch.setattr(
        disp,
        "tick_once_for_board",
        lambda slug, host_budget_share=None: seen.setdefault(slug, host_budget_share),
    )

    disp.tick_once()

    assert seen == {"defcon": 4, "ops": None}


def test_an_uncapped_dispatcher_passes_no_share_at_all(monkeypatch):
    """No cap derived (or unreadable): every board keeps its whole-budget behaviour."""
    disp = _stub_dispatcher()
    monkeypatch.setattr(disp, "_board_slugs", lambda: ["defcon", "ops"])
    monkeypatch.setattr(disp, "head_of_line_priority", lambda slug: 1)
    seen: dict = {}
    monkeypatch.setattr(
        disp,
        "tick_once_for_board",
        lambda slug, host_budget_share=None: seen.setdefault(slug, host_budget_share),
    )

    disp.tick_once()

    assert seen == {"defcon": None, "ops": None}


def test_an_unreadable_host_total_hands_out_no_shares(monkeypatch):
    disp = _stub_dispatcher(max_in_progress=4)
    monkeypatch.setattr(disp, "_board_slugs", lambda: ["defcon", "ops"])
    monkeypatch.setattr(disp, "head_of_line_priority", lambda slug: 1)
    monkeypatch.setattr(kbd, "total_running_all_boards", lambda: None)
    seen: dict = {}
    monkeypatch.setattr(
        disp,
        "tick_once_for_board",
        lambda slug, host_budget_share=None: seen.setdefault(slug, host_budget_share),
    )

    disp.tick_once()

    assert seen == {"defcon": None, "ops": None}


def test_tick_once_reports_a_board_the_host_budget_keeps_deferring(monkeypatch, caplog):
    """A starved board logs a line naming itself and the card it is waiting on."""
    disp = _stub_dispatcher(max_in_progress=1)
    disp.host_cap_starvation = kbd.HostCapStarvationClock(defer_seconds=0.0)
    monkeypatch.setattr(disp, "_board_slugs", lambda: ["defcon", "ops"])
    monkeypatch.setattr(disp, "head_of_line_priority", lambda slug: 1)
    monkeypatch.setattr(kbd, "total_running_all_boards", lambda: 0)

    def fake_tick(slug, host_budget_share=None):
        res = kbd.DispatchResult()
        if slug == "ops":
            res.deferred_host_capped = ["t_ops_head"]
        return res

    monkeypatch.setattr(disp, "tick_once_for_board", fake_tick)

    with caplog.at_level(logging.WARNING):
        disp.tick_once()
        disp.tick_once()

    assert "t_ops_head" in caplog.text
    assert "kanban.max_in_progress" in caplog.text


# ---------------------------------------------------------------------------
# The probe
# ---------------------------------------------------------------------------


def test_the_rank_costs_one_probe_per_board(monkeypatch):
    """O(num_boards) connects, never a cross-DB merge of every card."""
    disp = _stub_dispatcher()
    monkeypatch.setattr(disp, "_board_slugs", lambda: ["ops", "research", "peer-delivery"])
    probed: list = []
    monkeypatch.setattr(disp, "head_of_line_priority", lambda slug: probed.append(slug) or 1)
    monkeypatch.setattr(disp, "tick_once_for_board", lambda slug, host_budget_share=None: None)

    disp.tick_once()

    assert probed == ["ops", "research", "peer-delivery"]


def test_a_failed_probe_is_a_ranking_miss_not_a_lost_tick(monkeypatch):
    """An unreadable board is still visited — dispatch_once owns its own guards."""
    disp = _stub_dispatcher()

    def boom(board=None):
        raise RuntimeError("no such board")

    monkeypatch.setattr(kwd, "_kbc", lambda: SimpleNamespace(connect=boom))

    assert disp.head_of_line_priority("ghost") is None


def test_the_probe_reads_a_real_board_end_to_end(board_home):
    """Not a stub: a real connection, the real lane order, a real priority."""
    with kbc.connect() as conn:
        kb.create_task(conn, title="waiting", assignee="platform-coder", priority=2)
        kb.create_task(conn, title="lower", assignee="platform-coder", priority=1)

    assert _stub_dispatcher().head_of_line_priority("default") == 2


def test_the_probe_ignores_cards_no_worker_could_start(board_home):
    """Unassigned triage work is not a free-slot consumer, so it is not a rank."""
    with kbc.connect() as conn:
        kb.create_task(conn, title="needs routing", assignee=None, priority=4)

    assert _stub_dispatcher().head_of_line_priority("default") is None


def test_the_free_budget_subtracts_every_running_worker_on_the_host(board_home):
    kb.create_board("ops")
    with kbc.connect(board="ops") as conn:
        tid = kb.create_task(conn, title="busy", assignee="alice")
        assert kb.claim_task(conn, tid) is not None

    disp = kwd._KanbanDispatcher(kb, _settings(max_in_progress=4))

    assert disp.free_host_budget() == 3


def test_an_unreadable_board_still_reports_running_work_elsewhere(board_home, monkeypatch):
    """No board readable → the allocation is skipped rather than guessed."""
    monkeypatch.setattr(
        kb, "list_boards", lambda **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    disp = kwd._KanbanDispatcher(kb, _settings(max_in_progress=4))

    assert disp.free_host_budget() is None


# ---------------------------------------------------------------------------
# ACCEPTANCE, end to end through the real dispatch tick
# ---------------------------------------------------------------------------


def test_one_tick_now_starts_work_on_both_boards(board_home, monkeypatch):
    """The reported defect, end to end.

    Host cap 4, a deep queue on the head board and ONE waiting card on the
    other: the tick has to leave the second board a slot. Only the process
    launch is stubbed (``_default_spawn``); the claim, the budget arithmetic and
    both boards' DBs are real.
    """
    kb.create_board("ops")
    with kbc.connect() as conn:
        for n in range(6):
            kb.create_task(conn, title=f"deep-{n}", assignee="platform-worker", priority=4)
    with kbc.connect(board="ops") as conn:
        kb.create_task(conn, title="waiting", assignee="platform-worker", priority=1)

    spawned: list = []
    monkeypatch.setattr(
        kbd,
        "_default_spawn",
        lambda task, workspace, board=None: spawned.append((board, task.id)) or 4242,
    )

    disp = kwd._KanbanDispatcher(kb, _settings(max_in_progress=4))
    monkeypatch.setattr(disp, "_board_slugs", lambda: ["default", "ops"])
    results = disp.tick_once()

    per_board: dict = {}
    for board, _task_id in spawned:
        per_board[board] = per_board.get(board, 0) + 1

    assert [slug for slug, _ in results] == ["default", "ops"]
    assert per_board.get("ops") == 1, "the ops board was starved by the deep queue"
    assert per_board.get("default") == 3
    assert len(spawned) == 4, "the host cap must still bound the fleet"
    assert len(results[1][1].spawned) == 1
