"""Ready-lane fairness (ruling ``t_b2865b89``, F1 of the spawn-slot work).

The dispatcher's READY lane is one list ordered ``priority DESC, created_at
ASC``. Walking it straight hands every free slot to whichever lanes filed the
highest cards, so a lane whose work is merely *ordinarily* ranked — and is
therefore always below the designated tranche — never starts while a designated
lane has a queue. ``_lane_fair_ready_order`` orders the SAME rows in three
passes instead:

1. **designated reach** — one slot per lane whose head card is designated
   (``>= TRANCHE_FLOOR``), capped by ``budget - designated_pool_reserve``;
2. **lane reach** — one slot per lane the first pass did not serve;
3. **fill** — the remaining budget in global priority order.

Lanes are served least-recently-run first: a lane with no ``task_runs`` row at
all sorts before every lane that has one. These tests pin that pass semantics
and the two knobs, ``kanban.lane_fair_spawn`` and
``kanban.designated_pool_reserve``.

Hermetic: every board here lives under a per-test ``HERMES_HOME``.
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

ORDINARY = 500000

_LANES = ("alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta")
_BASE_TS = 1_700_000_000


@pytest.fixture()
def env(monkeypatch):
    """Fresh HERMES_HOME with a profile dir per lane, plus the kanban modules.

    Mirrors ``tests/hermes_cli/test_kanban_per_profile_cap.py``: a bare
    directory is not a profile, so each lane gets a ``config.yaml`` marker. The
    kanban path pins from the *worker* environment are cleared explicitly — the
    hermetic conftest fixture already does this, and a scratch board must never
    resolve to the operator's live board even if that fixture changes.
    """
    test_home = tempfile.mkdtemp(prefix="kanban_lane_fair_spawn_test_")
    for prof in (*_LANES, "default"):
        os.makedirs(os.path.join(test_home, "profiles", prof), exist_ok=True)
        with open(os.path.join(test_home, "profiles", prof, "config.yaml"), "w") as fh:
            fh.write("{}\n")
    monkeypatch.setenv("HERMES_HOME", test_home)
    for name in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME",
                 "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(name, raising=False)
    for mod in list(sys.modules.keys()):
        if (mod.startswith("hermes_cli") or mod.startswith("hermes_state")
                or mod == "hermes_constants"):
            del sys.modules[mod]
    from hermes_cli import (kanban_db, kanban_db_connect, kanban_db_dispatch,
                            kanban_priority_policy)
    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
    return kanban_db, kanban_db_connect, kanban_db_dispatch, kanban_priority_policy


def _fake_spawn(*args, **kwargs):
    return 12345


def _open(env, board=None):
    _, kbc, _, _ = env
    return kbc.connect_closing(board=board)


def _mk(kb, conn, title, assignee, *, priority=ORDINARY, created_at=None):
    """Create a ready card. ``created_at`` is stamped so the global order is
    deterministic — SQLite stores it in whole seconds, so several cards created
    in one test would otherwise tie."""
    tid = kb.create_task(conn, title=title, assignee=assignee, priority=priority)
    if created_at is not None:
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (created_at, tid))
        conn.commit()
    return tid


def _designated(kb, conn, tid):
    kb.designate_priority(conn, tid, reason="lane-fairness test")
    return tid


def _history_lane_run(conn, lane, *, started_at):
    """Seed one ``task_runs`` row for *lane* without touching the ready lane.

    The run hangs off a separate, already-``done`` card: the ready card in that
    lane must keep an empty run history of its own, because a completed run *on
    the ready card* trips the ``recent_success`` respawn guard and the card
    could then never spawn.
    """
    from hermes_cli import kanban_db as kb
    hist = kb.create_task(conn, title=f"{lane} history", assignee=lane)
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (hist,))
    conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome) "
        "VALUES (?, ?, 'done', ?, ?, 'completed')",
        (hist, lane, started_at, started_at),
    )
    conn.commit()


def _dispatch(conn, *, budget, lane_fair_spawn=True, designated_pool_reserve=1):
    from hermes_cli import kanban_db_dispatch as kbd
    return kbd.dispatch_once(
        conn, spawn_fn=_fake_spawn, dry_run=True, max_spawn=budget,
        lane_fair_spawn=lane_fair_spawn,
        designated_pool_reserve=designated_pool_reserve,
    )


def _ids(res):
    return [tid for tid, _lane, _ws in res.spawned]


def _by_lane(res):
    out: dict[str, list] = {}
    for tid, lane, _ws in res.spawned:
        out.setdefault(lane, []).append(tid)
    return out


def _today(conn):
    from hermes_cli import kanban_db_dispatch as kbd
    return [r["id"] for r in kbd._lane_rows(conn, "ready")]


# ── T1 ─────────────────────────────────────────────────────────────────────


def test_b1_ordinary_lane_is_starved_on_the_live_board(env):
    """One designated lane with a deep queue + one lane holding a single
    ordinary card, budget 2: the ordinary lane gets ZERO slots with the
    allocator off — the live-board shape that filed ``t_b2865b89`` — and one
    slot with it on."""
    kb, _kbc, _kbd, policy = env
    with _open(env) as conn:
        for i in range(6):
            _designated(kb, conn, _mk(kb, conn, f"d{i}", "alpha",
                                      priority=policy.DESIGNATED_PRIORITY,
                                      created_at=_BASE_TS + i))
        ordinary = _mk(kb, conn, "ordinary", "beta", created_at=_BASE_TS + 100)

    with _open(env) as conn:
        starved = _by_lane(_dispatch(conn, budget=2, lane_fair_spawn=False))
    assert len(starved.get("alpha", [])) == 2, "baseline: both slots go to the designated lane"
    assert starved.get("beta", []) == [], "baseline: the ordinary lane starves"

    with _open(env) as conn:
        fair = _by_lane(_dispatch(conn, budget=2))
    assert len(fair.get("alpha", [])) == 1
    assert fair.get("beta", []) == [ordinary], "the ordinary lane is no longer starved"


# ── T2 ─────────────────────────────────────────────────────────────────────


def test_b2_budget_6_two_designated_lanes_both_half_one_ordinary_reaches(env):
    """Two lanes of designated cards + a third lane holding one ordinary card,
    budget 6: all three lanes are served, and the ordinary card — the third
    lane's head — is one of them."""
    kb, _kbc, _kbd, policy = env
    with _open(env) as conn:
        for i in range(3):
            _designated(kb, conn, _mk(kb, conn, f"a{i}", "alpha",
                                      priority=policy.DESIGNATED_PRIORITY,
                                      created_at=_BASE_TS + i))
        for i in range(3):
            _designated(kb, conn, _mk(kb, conn, f"b{i}", "beta",
                                      priority=policy.DESIGNATED_PRIORITY,
                                      created_at=_BASE_TS + 10 + i))
        ordinary = _mk(kb, conn, "g-ordinary", "gamma", created_at=_BASE_TS + 50)

    with _open(env) as conn:
        res = _dispatch(conn, budget=6)

    spawned = _ids(res)
    assert len(spawned) == 6
    assert set(_by_lane(res)) == {"alpha", "beta", "gamma"}, "every lane is reached"
    # Pass 1 gives alpha and beta their heads, so pass 2 places the ordinary
    # card third — the third lane is not left for the fill pass.
    assert spawned[2] == ordinary, "the ordinary card is the third lane's head"


def test_b2_designated_tier_respects_the_reserve_ceiling(env):
    """The same shape, counted: exactly ``budget - designated_pool_reserve``
    designated cards run and the rest of the budget goes to ordinary work."""
    kb, _kbc, _kbd, policy = env
    with _open(env) as conn:
        designated = []
        for lane in ("alpha", "beta"):
            for i in range(3):
                designated.append(_designated(
                    kb, conn, _mk(kb, conn, f"{lane}{i}", lane,
                                  priority=policy.DESIGNATED_PRIORITY,
                                  created_at=_BASE_TS + len(designated))))
        ordinary = _mk(kb, conn, "g-ordinary", "gamma", created_at=_BASE_TS + 50)

    with _open(env) as conn:
        res = _dispatch(conn, budget=6, designated_pool_reserve=1)

    spawned = _ids(res)
    assert len(spawned) == 6
    assert sum(1 for tid in spawned if tid in designated) == 5, (
        "the designated tier holds at most budget - designated_pool_reserve")
    assert ordinary in spawned


# ── T3 ─────────────────────────────────────────────────────────────────────


def test_b3_lru_rotation_serves_every_lane_within_three_ticks(env):
    """Five lanes, budget 2 per tick, with a non-empty LRU fixture: the two
    lanes with no run history at all go first, then the least-recently-run
    lanes, so three successive ticks reach all five and none is served three
    times."""
    kb, _kbc, _kbd, _policy = env
    lanes = ("alpha", "beta", "gamma", "delta", "epsilon")
    with _open(env) as conn:
        for i, lane in enumerate(lanes):
            _mk(kb, conn, f"{lane}-work", lane, created_at=_BASE_TS + i)
        for lane, ts in (("alpha", 100), ("beta", 200), ("gamma", 300)):
            _history_lane_run(conn, lane, started_at=ts)

    served: list[str] = []

    # Tick 1 — the two lanes with no task_runs row at all sort first.
    with _open(env) as conn:
        tick1 = [lane for _tid, lane, _ws in _dispatch(conn, budget=2).spawned]
    assert set(tick1) == {"delta", "epsilon"}
    served.extend(tick1)
    with _open(env) as conn:
        for lane in tick1:
            _history_lane_run(conn, lane, started_at=1000)

    # Tick 2 — the oldest run history now leads.
    with _open(env) as conn:
        tick2 = [lane for _tid, lane, _ws in _dispatch(conn, budget=2).spawned]
    assert set(tick2) == {"alpha", "beta"}
    served.extend(tick2)
    with _open(env) as conn:
        for lane in tick2:
            _history_lane_run(conn, lane, started_at=2000)

    # Tick 3 — the only lane not yet served this round.
    with _open(env) as conn:
        tick3 = [lane for _tid, lane, _ws in _dispatch(conn, budget=2).spawned]
    assert "gamma" in tick3
    served.extend(tick3)

    assert set(served) == set(lanes), "all five lanes are served within three ticks"
    assert max(served.count(lane) for lane in lanes) <= 2, "no lane is served three times"


# ── T4 ─────────────────────────────────────────────────────────────────────


def test_b4_knob_off_allocation_order_is_byte_identical_to_priority_order(env):
    """``kanban.lane_fair_spawn=false`` allocates in ``priority DESC, created_at
    ASC`` — the pre-fairness order, byte for byte — and the fixture is
    discriminating: the same board with the knob on does NOT produce it."""
    kb, _kbc, _kbd, policy = env
    with _open(env) as conn:
        _designated(kb, conn, _mk(kb, conn, "d0", "alpha",
                                  priority=policy.DESIGNATED_PRIORITY,
                                  created_at=_BASE_TS + 0))
        _designated(kb, conn, _mk(kb, conn, "d1", "alpha",
                                  priority=policy.DESIGNATED_PRIORITY,
                                  created_at=_BASE_TS + 1))
        _mk(kb, conn, "b0", "beta", created_at=_BASE_TS + 2)
        _mk(kb, conn, "b1", "beta", created_at=_BASE_TS + 3)
        _mk(kb, conn, "a-ordinary", "alpha", created_at=_BASE_TS + 4)

    with _open(env) as conn:
        today = _today(conn)
        off = _ids(_dispatch(conn, budget=3, lane_fair_spawn=False))

    assert len(today) == 5
    assert off == today[:3]

    with _open(env) as conn:
        on = _ids(_dispatch(conn, budget=3, lane_fair_spawn=True))
    assert on != off, "the fixture must discriminate fair ordering from the raw walk"
    assert on[1] == today[2], "fair order reaches beta's head (second lane) second"


# ── T6 ─────────────────────────────────────────────────────────────────────


def test_b6_with_no_reach_pressure_the_fill_pass_keeps_global_priority_order(env):
    """With no reach pressure the fill pass adds cards in global priority order:
    a single-lane board is ordered exactly as the raw walk, a board whose lanes
    each contribute one card is ordered exactly as today, and once every lane is
    reached the tail is the raw walk order minus the reach heads."""
    kb, kbc, _kbd, policy = env

    # (a) Single-lane board — the fair order IS the raw walk.
    with _open(env) as conn:
        _designated(kb, conn, _mk(kb, conn, "s0", "alpha",
                                  priority=policy.DESIGNATED_PRIORITY, created_at=_BASE_TS + 0))
        _mk(kb, conn, "s1", "alpha", created_at=_BASE_TS + 1)
        _mk(kb, conn, "s2", "alpha", created_at=_BASE_TS + 2)
    with _open(env) as conn:
        today_single = _today(conn)
        single = _ids(_dispatch(conn, budget=6))
    assert single == today_single, "single-lane board: every lane is reached, order untouched"

    # (b) One card per lane, budget >= L — order exactly as today.
    kb.create_board(slug="one_each", name="One each")
    with kbc.connect_closing(board="one_each") as conn:
        _mk(kb, conn, "one-alpha", "alpha", created_at=_BASE_TS + 100)
        _mk(kb, conn, "one-beta", "beta", created_at=_BASE_TS + 101)
        _mk(kb, conn, "one-gamma", "gamma", created_at=_BASE_TS + 102)
    with kbc.connect_closing(board="one_each") as conn:
        today_one = _today(conn)
        one_each = _ids(_dispatch(conn, budget=6))
    assert one_each == today_one, "budget >= L with one card per lane: order exactly as today"

    # (c) Every lane reached, budget left over: the tail keeps global priority order.
    with _open(env) as conn:
        _mk(kb, conn, "t-alpha-2", "alpha", created_at=_BASE_TS + 200)
        _mk(kb, conn, "t-beta-2", "beta", created_at=_BASE_TS + 201)
        _mk(kb, conn, "one-beta-2", "beta", created_at=_BASE_TS + 202)
    with _open(env) as conn:
        today = _today(conn)
        spawned = _ids(_dispatch(conn, budget=99))
    assert sorted(spawned) == sorted(today), "budget >= L: nothing is dropped from the tick"
    heads, seen = [], set()
    lane_of = {r["id"]: r["assignee"] for r in
               _lane_rows_with_lane(env, "default")}
    for tid in spawned:
        if lane_of[tid] not in seen:
            seen.add(lane_of[tid])
            heads.append(tid)
    tail = [tid for tid in spawned if tid not in set(heads)]
    assert tail == [tid for tid in today if tid in set(tail)], (
        "the fill pass keeps global priority order")


def _lane_rows_with_lane(env, board):
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing(board=board) as conn:
        return conn.execute(
            "SELECT id, assignee FROM tasks WHERE status = 'ready' AND claim_lock IS NULL"
        ).fetchall()


# ── T7 ─────────────────────────────────────────────────────────────────────


def test_b7_all_designated_board_gets_the_whole_budget(env):
    """The reserve ceiling does NOT fire when no ordinary work waits: an
    all-designated board still gets every slot."""
    kb, _kbc, _kbd, policy = env
    with _open(env) as conn:
        for lane in ("alpha", "beta", "gamma"):
            for i in range(2):
                _designated(kb, conn, _mk(
                    kb, conn, f"{lane}{i}", lane,
                    priority=policy.DESIGNATED_PRIORITY,
                    created_at=_BASE_TS + 10 * (ord(lane[0]) - 96) + i))

    with _open(env) as conn:
        res = _dispatch(conn, budget=4, designated_pool_reserve=1)
    assert len(res.spawned) == 4, (
        "all four slots are used — none held back for ordinary work that does not exist")


# ── T8 ─────────────────────────────────────────────────────────────────────


def test_b8_a_never_served_lane_sorts_first(env):
    """A lane with no ``task_runs`` row precedes every served lane whatever the
    head priorities are — in the ordinary tier and in the designated tier."""
    kb, _kbc, _kbd, policy = env

    # Ordinary tier: alpha's head outranks beta's, but beta has never run.
    with _open(env) as conn:
        high = _mk(kb, conn, "alpha-high", "alpha", priority=ORDINARY + 100,
                   created_at=_BASE_TS + 0)
        _history_lane_run(conn, "alpha", started_at=1_000_000)
        low = _mk(kb, conn, "beta-low", "beta", priority=ORDINARY, created_at=_BASE_TS + 1)
    with _open(env) as conn:
        assert _ids(_dispatch(conn, budget=1)) == [low], "never-served lane first"

    # Designated tier: the same rule inside the tranche.
    with _open(env) as conn:
        conn.execute("UPDATE tasks SET status = 'done' WHERE id IN (?, ?)", (high, low))
        conn.commit()
        _designated(kb, conn, _mk(kb, conn, "alpha-d", "alpha",
                                  priority=policy.DESIGNATED_PRIORITY, created_at=_BASE_TS + 2))
        _history_lane_run(conn, "alpha", started_at=2_000_000)
        beta_d = _designated(kb, conn, _mk(kb, conn, "beta-d", "beta",
                                           priority=policy.DESIGNATED_PRIORITY,
                                           created_at=_BASE_TS + 3))
    with _open(env) as conn:
        assert _ids(_dispatch(conn, budget=1)) == [beta_d], (
            "a never-served designated lane precedes a served one")


# ── T9 / T10 — pass-2 fall-through (review round 1, ruling §7.2) ────────────


def test_b9_two_designated_heads_still_reach_both_lanes(env):
    """Two lanes whose HEAD is a designated card, each also holding ordinary
    work, budget 2 with reserve 1: the ceiling lets exactly one designated card
    run, so the second lane must be reached through its ORDINARY card. A reach
    pass that tests only each lane's head skips beta entirely and spends the
    second slot on alpha's next card — P-Reach false — which is the review
    round-1 defect (ruling t_b2865b89 §7.2)."""
    kb, _kbc, _kbd, policy = env
    with _open(env) as conn:
        a_d = _designated(kb, conn, _mk(kb, conn, "a-d", "alpha",
                                        priority=policy.DESIGNATED_PRIORITY,
                                        created_at=_BASE_TS + 0))
        b_d = _designated(kb, conn, _mk(kb, conn, "b-d", "beta",
                                        priority=policy.DESIGNATED_PRIORITY,
                                        created_at=_BASE_TS + 1))
        a_o = _mk(kb, conn, "a-o", "alpha", created_at=_BASE_TS + 10)
        b_o = _mk(kb, conn, "b-o", "beta", created_at=_BASE_TS + 11)

    with _open(env) as conn:
        res = _dispatch(conn, budget=2, designated_pool_reserve=1)

    assert set(_ids(res)) == {a_d, b_o}, (
        "one designated (the LRU-first lane's head) + the other lane's ordinary card")
    by_lane = _by_lane(res)
    assert by_lane["alpha"] == [a_d], "the first lane spends the ceiling slot"
    assert by_lane["beta"] == [b_o], (
        "the second lane is reached through its ordinary card, not skipped")
    assert b_d not in _ids(res) and a_o not in _ids(res), "budget is exactly 2"


def test_b10_pass_two_never_exceeds_the_designated_ceiling(env):
    """The reach passes never spend more than ``budget - designated_pool_reserve``
    designated slots while ordinary ready work waits — counted over the WHOLE
    tick, pass 1 and pass 2 together. A fall-through that took a designated card
    in pass 2 would breach the floor."""
    kb, kbc, _kbd, policy = env

    def _board(slug, budget, reserve, depth):
        kb.create_board(slug=slug, name=slug)
        with kbc.connect_closing(board=slug) as conn:
            designated: list[str] = []
            ordinary: list[str] = []
            for lane in ("alpha", "beta"):
                for i in range(depth):
                    designated.append(_designated(
                        kb, conn, _mk(kb, conn, f"{lane}-d{i}", lane,
                                      priority=policy.DESIGNATED_PRIORITY,
                                      created_at=_BASE_TS + 10 * len(designated))))
                ordinary.append(_mk(kb, conn, f"{lane}-o", lane,
                                    created_at=_BASE_TS + 1000))
        with kbc.connect_closing(board=slug) as conn:
            res = _dispatch(conn, budget=budget, designated_pool_reserve=reserve)
        spawned = _ids(res)
        assert len(spawned) == budget, slug
        assert sum(1 for tid in spawned if tid in designated) <= budget - reserve, (
            f"{slug}: designated tier stays within budget - designated_pool_reserve")
        assert any(tid in spawned for tid in ordinary), (
            f"{slug}: ordinary work is not starved by the ceiling")

    # Two designated heads, budget 2, reserve 1 → at most one designated card.
    _board("ceiling_two", budget=2, reserve=1, depth=2)
    # Deeper designated queues, budget 4, reserve 1 → at most three designated.
    _board("ceiling_four", budget=4, reserve=1, depth=2)


def test_b11_five_designated_head_lanes_all_reached_within_three_ticks(env):
    """The P-Reach tick bound in the designated-head shape: five lanes, each
    holding a designated head plus ordinary work, budget 2 → every lane is
    reached within ``ceil(5/2) = 3`` successive ticks as run history advances.
    The head-only reach pass reached only 3 of 5 here (reviewer round-1 probe);
    the control with all-ordinary heads reaches 5 of 5."""
    kb, _kbc, _kbd, policy = env
    lanes = ("alpha", "beta", "gamma", "delta", "epsilon")
    with _open(env) as conn:
        for i, lane in enumerate(lanes):
            _designated(kb, conn, _mk(kb, conn, f"{lane}-d", lane,
                                      priority=policy.DESIGNATED_PRIORITY,
                                      created_at=_BASE_TS + i))
            for k in range(2):
                _mk(kb, conn, f"{lane}-o{k}", lane,
                    created_at=_BASE_TS + 100 + 10 * i + k)

    served: list[str] = []
    for tick in range(3):
        with _open(env) as conn:
            reached = [lane for _tid, lane, _ws in _dispatch(conn, budget=2).spawned]
        assert len(reached) == 2, f"tick {tick}: the budget is spent"
        served.extend(reached)
        with _open(env) as conn:
            for lane in reached:
                _history_lane_run(conn, lane, started_at=1000 * (tick + 1))

    assert set(served) == set(lanes), (
        "every lane with ready work and no running card is reached within "
        "ceil(5/2)=3 ticks")


# ── knobs ──────────────────────────────────────────────────────────────────


def test_knobs_parse_with_production_defaults(env):
    """``lane_fair_spawn`` defaults true and ``designated_pool_reserve`` to 1; a
    malformed value fails open to the default instead of raising into a tick."""
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["kanban"]["lane_fair_spawn"] is True
    assert DEFAULT_CONFIG["kanban"]["designated_pool_reserve"] == 1

    assert kbd.lane_fair_ready_config({}) == (True, 1)
    assert kbd.lane_fair_ready_config({"lane_fair_spawn": False}) == (False, 1)
    assert kbd.lane_fair_ready_config({"lane_fair_spawn": "off"}) == (False, 1)
    assert kbd.lane_fair_ready_config({"lane_fair_spawn": "bogus"}) == (True, 1)
    assert kbd.lane_fair_ready_config({"designated_pool_reserve": 0}) == (True, 0)
    assert kbd.lane_fair_ready_config({"designated_pool_reserve": -3}) == (True, 1)
