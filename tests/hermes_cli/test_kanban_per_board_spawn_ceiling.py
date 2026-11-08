"""Per-board spawn ceilings: ``kanban.max_spawn_by_board``.

The operator ask this covers: ``kanban.max_spawn`` is ONE number applied to
every board's tick, so the only per-board lever is ``set-dispatch on|off`` —
all-or-nothing, and it also stops that board's reclaim/promotion. The fix is a
per-board CEILING resolved once per tick at the ``tick_once_for_board`` call
site: ``kanban.max_spawn`` stays the default for unnamed boards, an unknown slug
warns and is ignored, a board capped below its running count spawns nothing
(never killing a worker), and a spawn the ceiling held back is recorded as
``deferred_board_capped`` in the tick result and the run output.

These tests exercise the REAL worktree modules (the ``_dispatch`` / ``_tree``
precondition is asserted in ``test_imports_resolve_to_this_tree``).
"""

from __future__ import annotations

import json
import sqlite3
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_ops
from gateway import kanban_watchers_dispatcher as kwd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def all_assignees_spawnable(monkeypatch):
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


def _fake_spawn_factory(spawns: list):
    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    return fake_spawn


def _settings(**overrides):
    """A ``_DispatcherSettings`` with the real fields, overridable by name."""
    values = dict(
        interval=60.0, max_spawn=8, max_in_progress=None, failure_limit=3,
        stale_timeout_seconds=0, reconcile_orphans=True, default_assignee=None,
        max_in_progress_per_profile=None, lane_fair_spawn=True,
        designated_pool_reserve=1, max_spawn_by_board={},
    )
    values.update(overrides)
    return kwd._DispatcherSettings(**values)


def _running(board=None):
    with kbc.connect(board=board) as conn:
        return kbd.count_running_tasks(conn)


def _seed(board, n, priority=0):
    with kbc.connect(board=board) as conn:
        for i in range(n):
            kb.create_task(conn, title=f"{board}-{i}", assignee="alice", priority=priority)


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


def test_named_board_uses_its_ceiling_unnamed_keeps_global(kanban_home):
    kb.create_board("defcon")
    kb.create_board("ops")
    settings = kwd._resolve_dispatcher_settings(
        {"max_spawn": 8, "max_spawn_by_board": {"defcon": 10, "ops": 2}}, kb
    )
    assert settings.max_spawn_for_board("defcon") == 10
    assert settings.max_spawn_for_board("ops") == 2
    # Unnamed boards — including the root board — keep kanban.max_spawn.
    assert settings.max_spawn_for_board(kb.DEFAULT_BOARD) == 8
    assert settings.max_spawn_for_board("never-named") == 8


def test_unknown_slug_warns_and_is_ignored(kanban_home, caplog):
    kb.create_board("ops")
    with caplog.at_level("WARNING"):
        settings = kwd._resolve_dispatcher_settings(
            {"max_spawn": 8, "max_spawn_by_board": {"nosuchboard": 3, "ops": 2}}, kb
        )
    assert dict(settings.max_spawn_by_board) == {"ops": 2}
    assert settings.max_spawn_for_board("nosuchboard") == 8
    assert any("unknown board" in r.getMessage() and "nosuchboard" in r.getMessage()
               for r in caplog.records)


def test_invalid_values_and_slugs_are_ignored(kanban_home, caplog):
    kb.create_board("ops")
    kb.create_board("research")
    kb.create_board("virgo")
    kb.create_board("libra")
    with caplog.at_level("WARNING"):
        settings = kwd._resolve_dispatcher_settings(
            {
                "max_spawn": 8,
                "max_spawn_by_board": {
                    "ops": "lots",          # not an int
                    "defcon": 0,            # below 1
                    "research": 2.5,        # non-integral float: a typo, never truncated to 2
                    "virgo": float("inf"),  # non-finite float: must never reach int()
                    "libra": True,          # a bool is not a ceiling (int subclass)
                    "": 5,                  # empty slug
                    "ghost": None,          # not an int (and unknown)
                    "ops2": 3,              # unknown + valid -> also ignored
                },
            },
            kb,
        )
    assert dict(settings.max_spawn_by_board) == {}
    assert settings.max_spawn_for_board("ops") == 8
    for slug, value in (("research", 2.5), ("virgo", float("inf")), ("libra", True)):
        assert any(
            repr(slug) in r.getMessage() and repr(value) in r.getMessage()
            for r in caplog.records
        ), f"no warning naming {slug}={value!r}"


def test_non_mapping_is_ignored(kanban_home, caplog):
    with caplog.at_level("WARNING"):
        settings = kwd._resolve_dispatcher_settings(
            {"max_spawn": 8, "max_spawn_by_board": [1, 2, 3]}, kb
        )
    assert dict(settings.max_spawn_by_board) == {}
    assert any("not a mapping" in r.getMessage() for r in caplog.records)


def test_unreadable_board_inventory_keeps_the_map(kanban_home, monkeypatch):
    """A read failure must never drop a real ceiling — fail open, not closed."""
    monkeypatch.setattr(
        kb, "list_boards",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("cannot read boards")),
    )
    settings = kwd._resolve_dispatcher_settings(
        {"max_spawn": 8, "max_spawn_by_board": {"ghost": 4}}, kb
    )
    assert dict(settings.max_spawn_by_board) == {"ghost": 4}
    assert settings.max_spawn_for_board("ghost") == 4


def test_default_is_empty_and_behaviour_preserving(kanban_home):
    settings = kwd._resolve_dispatcher_settings({"max_spawn": 8}, kb)
    assert dict(settings.max_spawn_by_board) == {}
    for slug in ("ops", kb.DEFAULT_BOARD, "never-named"):
        # No map -> every board resolves to the SAME global ceiling the old
        # code passed verbatim, so nothing in the spawn decision moves.
        assert settings.max_spawn_for_board(slug) == 8


# ---------------------------------------------------------------------------
# Host-budget allocation: ceilings release their surplus, no map is unchanged
# ---------------------------------------------------------------------------


def _historical_allocation(free, boards):
    """The allocation BEFORE per-board ceilings: 1 each, remainder to the head."""
    if free is None or not boards:
        return {}
    shares: dict[str, int] = {}
    remaining = int(free)
    for slug in boards:
        if remaining <= 0:
            break
        shares[slug] = 1
        remaining -= 1
    if remaining > 0:
        shares[boards[0]] = shares.get(boards[0], 0) + remaining
    return shares


def test_share_allocation_is_identical_without_ceilings():
    """No configured ceiling -> byte-for-byte the historical allocation."""
    for boards in (["a"], ["a", "b"], ["a", "b", "c"], ["b", "a"], ["a", "b", "c", "d"]):
        for free in range(0, 14):
            assert kwd.host_budget_shares(free, boards) == _historical_allocation(free, boards)


def test_share_allocation_flows_the_surplus_by_ceiling():
    # The DoD scenario: ops may hold 2, so it is served first and defcon takes
    # the rest instead of stranding 8 slots on the head.
    assert kwd.host_budget_shares_by_ceiling(10, ["defcon", "ops"], {"defcon": 10, "ops": 2}) == {
        "defcon": 8,
        "ops": 2,
    }
    # A capped board releases what it cannot use; unbounded boards take the rest.
    assert kwd.host_budget_shares_by_ceiling(10, ["a", "b"], {"a": 2}) == {"a": 2, "b": 8}
    # A tight budget still gives one guaranteed slot per board with work.
    assert kwd.host_budget_shares_by_ceiling(2, ["a", "b"], {"a": 5, "b": 5}) == {"a": 1, "b": 1}


# ---------------------------------------------------------------------------
# Resolution happens at the tick_once_for_board call site
# ---------------------------------------------------------------------------


def test_tick_once_for_board_resolves_ceiling_per_board(kanban_home, monkeypatch):
    kb.create_board("ops")
    kb.create_board("defcon")
    captured: dict = {}

    def fake_dispatch_once(conn, *, board=None, **kwargs):
        captured[board] = kwargs
        return SimpleNamespace(spawned=[])

    settings = _settings(max_spawn_by_board={"ops": 2, "defcon": 10}, max_spawn=8)
    dispatcher = kwd._KanbanDispatcher(kb, settings)
    # Patch only the dispatch call; the constructor already consumed the real
    # module (HostCapStarvationClock).
    monkeypatch.setattr(kwd, "_kbd", lambda: SimpleNamespace(dispatch_once=fake_dispatch_once))

    dispatcher.tick_once_for_board("ops")
    dispatcher.tick_once_for_board("defcon")
    dispatcher.tick_once_for_board(kb.DEFAULT_BOARD)

    assert captured["ops"]["max_spawn"] == 2
    assert captured["defcon"]["max_spawn"] == 10
    assert captured[kb.DEFAULT_BOARD]["max_spawn"] == 8
    # The raw map is never forwarded as a dispatch_once kwarg (it is not one).
    for kwargs in captured.values():
        assert "max_spawn_by_board" not in kwargs
        assert "interval" not in kwargs


# ---------------------------------------------------------------------------
# The ceiling bounds a tick's spawns (dispatch_once level)
# ---------------------------------------------------------------------------


def test_ceiling_bounds_spawns(kanban_home, all_assignees_spawnable):
    with kbc.connect() as conn:
        for i in range(5):
            kb.create_task(conn, title=f"a{i}", assignee="alice")

    spawns: list = []
    with kbc.connect() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), max_spawn=2)

    assert len(spawns) == 2
    assert len(res.spawned) == 2
    # The board was not held back; it simply spawned up to its ceiling.
    assert res.deferred_board_capped == []


def test_ceiling_below_running_spawns_nothing_and_records_deferred(
    kanban_home, all_assignees_spawnable
):
    with kbc.connect() as conn:
        running = []
        for i in range(3):
            tid = kb.create_task(conn, title=f"run{i}", assignee="alice")
            assert kb.claim_task(conn, tid) is not None
            running.append(tid)
        ready = [kb.create_task(conn, title=f"r{i}", assignee="alice") for i in range(2)]

    spawns: list = []
    with kbc.connect() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), max_spawn=2)
        still_running = [
            row["id"] for row in conn.execute("select id from tasks where status='running'")
        ]

    assert spawns == []
    assert res.spawned == []
    # Never kills a worker to get under the ceiling.
    assert set(still_running) == set(running)
    # ...and the tick says what the ceiling held back instead of looking idle.
    assert set(res.deferred_board_capped) == set(ready)


def test_host_cap_still_wins_when_smaller(kanban_home, all_assignees_spawnable):
    with kbc.connect() as conn:
        for i in range(5):
            kb.create_task(conn, title=f"a{i}", assignee="alice")

    spawns: list = []
    with kbc.connect() as conn:
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_spawn=10, max_in_progress=4,
        )

    assert len(spawns) == 4
    assert len(res.spawned) == 4


def test_describe_suppression_names_the_board_cap():
    res = kbd.DispatchResult()
    res.deferred_board_capped = ["t_1", "t_2"]
    line = kbd.describe_suppression([res])
    assert "board_cap_deferred=2" in line


# ---------------------------------------------------------------------------
# Regression: a board at its ceiling still reclaims and promotes
# ---------------------------------------------------------------------------


def test_capped_board_still_promotes_its_children(kanban_home, all_assignees_spawnable):
    with kbc.connect() as conn:
        busy = kb.create_task(conn, title="busy", assignee="alice")
        assert kb.claim_task(conn, busy) is not None
        parent = kb.create_task(conn, title="parent", assignee="alice")
        child = kb.create_task(conn, title="child", assignee="alice", parents=(parent,))
        assert conn.execute(
            "select status from tasks where id = ?", (child,)
        ).fetchone()["status"] == "todo"
        conn.execute("update tasks set status='done' where id = ?", (parent,))
        conn.commit()

    spawns: list = []
    with kbc.connect() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), max_spawn=1)
        child_status = conn.execute(
            "select status from tasks where id = ?", (child,)
        ).fetchone()["status"]

    # The board is at its own ceiling, so nothing spawns...
    assert spawns == []
    assert res.spawned == []
    # ...but the tick is paused, not frozen: promotion still ran.
    assert child_status == "ready"
    assert child in res.deferred_board_capped


# ---------------------------------------------------------------------------
# Behaviour: per-board ceilings skew bandwidth across boards
# ---------------------------------------------------------------------------


def _settle(ceilings, host, ticks, *, defcon_prio=0, ops_prio=0):
    """Drive a two-board fleet and return the (defcon, ops) running trajectory."""
    kb.create_board("defcon")
    kb.create_board("ops")
    _seed("defcon", 30, defcon_prio)
    _seed("ops", 30, ops_prio)

    def fake_spawn(task, workspace, *, board=None):
        return 42

    orig = kbd._default_spawn
    kbd._default_spawn = fake_spawn
    try:
        settings = _settings(max_in_progress=host, max_spawn_by_board=ceilings)
        dispatcher = kwd._KanbanDispatcher(kb, settings)
        traj = []
        for _ in range(ticks):
            dispatcher.tick_once()
            traj.append((_running("defcon"), _running("ops")))
        return traj
    finally:
        kbd._default_spawn = orig


def test_ceilings_skew_bandwidth_and_respect_the_host_cap(
    kanban_home, all_assignees_spawnable
):
    """host cap 10, defcon ceiling 10, ops ceiling 2 -> defcon 8-10, ops 2."""
    traj = _settle({"defcon": 10, "ops": 2}, 10, 6)

    assert max(d for d, _ in traj) <= 10      # defcon's own ceiling bounds it
    assert max(o for _, o in traj) <= 2       # ops never exceeds its ceiling
    assert all(d + o <= 10 for d, o in traj)  # host cap is never breached

    final_defcon, final_ops = traj[-1]
    assert 8 <= final_defcon <= 10
    assert final_ops == 2


def test_a_board_ceiling_below_the_host_cap_still_bounds_that_board(
    kanban_home, all_assignees_spawnable
):
    """A low ceiling holds its board even with host headroom to spare."""
    traj = _settle({"defcon": 5, "ops": 2}, 10, 6)

    assert max(d for d, _ in traj) <= 5
    assert max(o for _, o in traj) <= 2
    final_defcon, final_ops = traj[-1]
    assert final_defcon == 5  # pinned to its own ceiling, not the host cap
    assert final_ops == 2


def test_no_map_configured_matches_a_direct_dispatch(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """With no map, a tick through the dispatcher == a direct dispatch_once."""
    kb.create_board("mapped")
    kb.create_board("control")
    _seed("mapped", 4)
    _seed("control", 4)

    def fake_spawn(task, workspace, *, board=None):
        return 42

    monkeypatch.setattr(kbd, "_default_spawn", fake_spawn)

    # The map is ABSENT: every board resolves to the global max_spawn.
    dispatcher = kwd._KanbanDispatcher(
        kb, _settings(max_in_progress=None, max_spawn=8, max_spawn_by_board={})
    )
    mapped_res = dispatcher.tick_once_for_board("mapped")

    spawns: list = []
    with kbc.connect(board="control") as conn:
        control_res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), board="control", max_spawn=8,
        )

    assert len(mapped_res.spawned) == len(control_res.spawned) == 4
    assert mapped_res.deferred_board_capped == []
    assert control_res.deferred_board_capped == []


# ---------------------------------------------------------------------------
# The DEFAULT arm: kanban.max_spawn_by_board["default"]
#
# The residual after the map landed (t_5e3fcfc9): a board the map does not NAME
# still drew the GLOBAL max_spawn, so the first new board could starve every
# board the operator had apportioned. `default: 0` is now expressible end to end
# — parsed by both readers, resolved by the gateway, counted by the host-budget
# measurement — and `default: -1` is still junk.
# ---------------------------------------------------------------------------


def test_max_spawn_for_board_resolves_all_four_arms(kanban_home):
    """The four arms of the resolution rule, each asserted on its own.

    (1) a NAMED board uses its own ceiling; (2) an unmapped board uses the
    ``default`` arm, 0 included; (3) a map with NO default arm keeps the global
    ``kanban.max_spawn`` (the compatibility arm); (4) a non-zero default bounds
    an unmapped board without touching a named one.
    """
    named_and_zero_default = _settings(
        max_spawn=8, max_spawn_by_board={"platform": 4, "default": 0}
    )
    assert named_and_zero_default.max_spawn_for_board("platform") == 4      # (1)
    assert named_and_zero_default.max_spawn_for_board("defcon") == 0        # (2)

    no_default_arm = _settings(max_spawn=8, max_spawn_by_board={"platform": 4})
    assert no_default_arm.max_spawn_for_board("defcon") == 8                # (3)
    assert no_default_arm.max_spawn_for_board(kb.DEFAULT_BOARD) == 8

    nonzero_default = _settings(
        max_spawn=8, max_spawn_by_board={"platform": 4, "default": 3}
    )
    assert nonzero_default.max_spawn_for_board("platform") == 4             # (4)
    assert nonzero_default.max_spawn_for_board("defcon") == 3

    assert _settings(max_spawn=8, max_spawn_by_board={}).max_spawn_for_board(
        "defcon"
    ) == 8                                                                   # (3), empty map


def test_default_zero_parses_while_a_named_zero_is_still_refused(kanban_home, caplog):
    """Zero is legal ONLY for the reserved key — the two rules are not one rule."""
    kb.create_board("defcon")
    with caplog.at_level("WARNING"):
        settings = kwd._resolve_dispatcher_settings(
            {"max_spawn": 8, "max_spawn_by_board": {"default": 0, "defcon": 0}}, kb
        )

    assert dict(settings.max_spawn_by_board) == {"default": 0}
    assert settings.max_spawn_for_board("defcon") == 0
    # ...and the NAMED board's zero was refused loudly, keeping the global cap.
    assert any(
        "positive ceiling" in r.getMessage() and "defcon" in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_negative_default_is_dropped_with_a_warning(kanban_home, caplog):
    kb.create_board("platform")
    with caplog.at_level("WARNING"):
        settings = kwd._resolve_dispatcher_settings(
            {"max_spawn": 8, "max_spawn_by_board": {"platform": 4, "default": -1}}, kb
        )

    assert dict(settings.max_spawn_by_board) == {"platform": 4}
    # A dropped default restores the compatibility arm, not a silent 0.
    assert settings.max_spawn_for_board("defcon") == 8
    assert any(
        "'default'" in r.getMessage() and "non-negative" in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_measurement_reader_agrees_with_the_enforcement_reader(kanban_home):
    """The dry-run's map and the gateway's map must be the SAME map.

    A measurement reader that dropped ``default: 0`` would report a budget the
    gateway never applies — the false green this card exists to close.
    """
    kb.create_board("platform")
    kb.create_board("defcon")
    maps = [
        {"default": 0, "platform": 4},
        {"default": 3},
        {"platform": 4},
        {"default": -1, "platform": 4},
        {"platform": 0, "defcon": 2},
        {"default": 0, "defcon": 0},
        {"platform": 2.5, "default": True},
        {"platform": "lots", "default": float("inf")},
    ]
    for mapped in maps:
        enforced = kwd._parse_max_spawn_by_board({"max_spawn_by_board": mapped}, kb)
        measured = kanban_ops._max_spawn_by_board({"max_spawn_by_board": mapped})
        assert measured == dict(enforced), mapped


def test_default_zero_an_unmapped_board_takes_no_slots(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """The card's DoD verbatim: an unmapped board at `default: 0` spawns nothing.

    Behavioural, not resolver-level: the tick runs the REAL dispatch gate with
    the ceiling the default arm resolved, and NAMES the cards it held.
    """
    kb.create_board("defcon")
    _seed("defcon", 5)
    monkeypatch.setattr(kbd, "_default_spawn", lambda task, workspace, *, board=None: 42)

    dispatcher = kwd._KanbanDispatcher(
        kb, _settings(max_spawn=8, max_spawn_by_board={"default": 0})
    )
    res = dispatcher.tick_once_for_board("defcon")

    assert res.spawned == []
    assert len(res.deferred_board_capped) == 5
    assert res.spawn_budget_blocked == "max_spawn"
    assert res.spawn_budget_detail["cap"] == 0


def test_without_the_default_arm_an_unmapped_board_keeps_the_global_max(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """Compatibility, ASSERTED: no default arm -> the unmapped board still runs."""
    kb.create_board("defcon")
    _seed("defcon", 5)
    monkeypatch.setattr(kbd, "_default_spawn", lambda task, workspace, *, board=None: 42)

    dispatcher = kwd._KanbanDispatcher(
        kb, _settings(max_spawn=8, max_spawn_by_board={"platform": 4})
    )
    res = dispatcher.tick_once_for_board("defcon")

    assert len(res.spawned) == 5          # the global max_spawn, unchanged
    assert res.spawn_budget_detail == {}


def test_default_zero_and_the_named_board_sharing_one_host(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """The DoD's fleet shape: platform at 4 while the unmapped board holds 0."""
    kb.create_board("platform")
    kb.create_board("defcon")
    _seed("platform", 10)
    _seed("defcon", 10)
    monkeypatch.setattr(kbd, "_default_spawn", lambda task, workspace, *, board=None: 42)

    dispatcher = kwd._KanbanDispatcher(
        kb,
        _settings(
            max_in_progress=8,
            max_spawn=8,
            max_spawn_by_board={"platform": 4, "default": 0},
        ),
    )
    for _ in range(4):
        dispatcher.tick_once()

    assert _running("platform") == 4
    assert _running("defcon") == 0
    assert _running("platform") + _running("defcon") <= 8


def test_the_ceiling_sum_counts_the_default(kanban_home):
    """The host-budget arithmetic includes the very value that decides starvation."""
    kb.create_board("platform")
    kb.create_board("defcon")

    measured = kbd.measure_host_budget(
        max_spawn=8,
        max_in_progress=8,
        ceilings={"platform": 4, "default": 0},
        boards=["platform", "defcon"],
    )
    assert measured["default_ceiling"] == 0
    assert measured["ceilings"] == {"platform": 4, "defcon": 0}
    assert "default" not in measured["ceilings"]      # a rule, never a board
    assert measured["ceiling_sum"] == 4
    assert measured["ceiling_sum_exceeds_cap"] is False
    assert measured["oversubscribed"] is False
    assert measured["unreachable_slots"] == 0

    # A default ABOVE the cap reaches the same arithmetic and flips it.
    over = kbd.measure_host_budget(
        max_spawn=8,
        max_in_progress=8,
        ceilings={"platform": 4, "default": 12},
        boards=["platform", "defcon"],
    )
    assert over["ceilings"] == {"platform": 4, "defcon": 12}
    assert over["ceiling_sum"] == 16
    assert over["ceiling_sum_exceeds_cap"] is True
    assert over["per_board_unreachable"] == {"defcon": 4}
    assert over["oversubscribed"] is True
    assert over["unreachable_slots"] == 4


def test_the_default_does_not_invent_a_measurement_without_a_host_cap(kanban_home):
    """None stays None: a default is not a cap and cannot make one measurable."""
    unmeasured = kbd.measure_host_budget(
        max_spawn=8,
        max_in_progress=None,
        ceilings={"default": 12},
        boards=["defcon"],
    )
    assert unmeasured["default_ceiling"] == 12
    assert unmeasured["ceiling_sum"] == 12
    assert unmeasured["ceiling_sum_exceeds_cap"] is None
    assert unmeasured["unreachable_slots"] is None
    assert unmeasured["oversubscribed"] is False


def test_cli_dry_run_reports_the_default_arm(
    kanban_home, all_assignees_spawnable, capsys, monkeypatch
):
    """The DRY-RUN carries the default, because the measurement reader kept it."""
    from hermes_cli import config as hermes_config

    kb.create_board("platform")
    kb.create_board("defcon")
    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda *a, **kw: {
            "kanban": {
                "max_spawn": 8,
                "max_in_progress": 8,
                "max_spawn_by_board": {"platform": 4, "default": 0},
            }
        },
    )

    rc = kanban_ops._cmd_dispatch(Namespace(json=True, dry_run=True, max=None, failure_limit=3))
    assert rc == 0
    host_budget = json.loads(capsys.readouterr().out)["host_budget"]

    assert host_budget["default_ceiling"] == 0
    assert host_budget["ceilings"]["platform"] == 4
    assert host_budget["ceilings"]["defcon"] == 0
    # One ceiling per enumerated board and no phantom entry: the fallback arm is
    # reported once, as default_ceiling, never as a board of its own. (The ROOT
    # board is literally `default`, so it appears here as a board — at the same
    # number the arm gives it.)
    assert set(host_budget["ceilings"]) == set(host_budget["boards"])
    # platform 4 + defcon 0 + the root board 0 (all inherited from `default: 0`)
    assert host_budget["ceiling_sum"] == 4
    assert host_budget["ceiling_sum_exceeds_cap"] is False
