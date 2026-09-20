"""Board visit order IS the host-budget allocation policy (D1/D3 follow-up).

`kanban.max_in_progress` is HOST-wide: every board draws from one budget and
`tick_once` hands the free slots to whatever board it reaches first. With the
board list in filesystem order that is an accident of naming — a board whose
slug sorts late loses every race to an earlier board however long its cards have
waited, and nothing ever reports the wait. These tests pin the policy: the board
with the most starved head of line is visited first, and an unrankable board is
visited LAST, never skipped (reclaim and promotion work is board-local).

The rank itself — head-of-line effective priority against a real board DB — is
pinned in `tests/hermes_cli/test_kanban_age_priority.py`; here the probe is
mostly stubbed so the ORDER is what is exercised.
"""

from __future__ import annotations

from types import SimpleNamespace

from gateway import kanban_watchers_dispatcher as kwd


def _dispatcher():
    settings = kwd._DispatcherSettings(60.0, None, None, 2, 0, True, None, None)
    return kwd._KanbanDispatcher(SimpleNamespace(DEFAULT_BOARD="default"), settings)


# ---------------------------------------------------------------------------
# The order itself
# ---------------------------------------------------------------------------


def test_most_starved_board_is_visited_first():
    """Descending head-of-line effective priority."""
    assert kwd.order_boards_by_head_priority(
        {"ops": 1, "research": 4, "financially": 2}
    ) == ["research", "financially", "ops"]


def test_equal_heads_are_ordered_by_board_name():
    """Deterministic: the same fleet is visited in the same order every tick."""
    assert kwd.order_boards_by_head_priority({"ops": 3, "zzz": 3, "aaa": 3}) == [
        "aaa", "ops", "zzz",
    ]


def test_unrankable_boards_are_visited_last_and_never_dropped():
    """Nothing spawnable (or a failed probe) means "no rank", not "no tick"."""
    assert kwd.order_boards_by_head_priority(
        {"ops": None, "aaa": None, "research": 2}
    ) == ["research", "aaa", "ops"]


# ---------------------------------------------------------------------------
# The wiring: probe each board, then visit in rank order
# ---------------------------------------------------------------------------


def test_tick_once_visits_the_board_with_the_oldest_head_of_line_first(monkeypatch):
    """Boards A (head 5) and B (head 1): A is visited first, and C still gets a turn."""
    disp = _dispatcher()
    monkeypatch.setattr(disp, "_board_slugs", lambda: ["ops", "research", "financially"])
    # "financially" has nothing spawnable — the probe returns None.
    monkeypatch.setattr(
        disp, "head_of_line_priority",
        lambda slug: {"ops": 1, "research": 5}.get(slug),
    )
    visited: list = []
    monkeypatch.setattr(
        disp, "tick_once_for_board", lambda slug: visited.append(slug) or f"res:{slug}"
    )

    results = disp.tick_once()

    assert visited == ["research", "ops", "financially"]
    assert results == [
        ("research", "res:research"),
        ("ops", "res:ops"),
        ("financially", "res:financially"),
    ]


def test_the_order_costs_one_probe_per_board(monkeypatch):
    """O(num_boards) probes, never a cross-DB merge of every card."""
    disp = _dispatcher()
    monkeypatch.setattr(disp, "_board_slugs", lambda: ["ops", "research", "peer-delivery"])
    probed: list = []
    monkeypatch.setattr(
        disp, "head_of_line_priority", lambda slug: probed.append(slug) or 1
    )
    monkeypatch.setattr(disp, "tick_once_for_board", lambda slug: None)

    disp.tick_once()

    assert probed == ["ops", "research", "peer-delivery"]


def test_a_failed_probe_is_a_ranking_miss_not_a_lost_tick(monkeypatch):
    """An unreadable board is still visited — dispatch_once owns its own locks."""
    disp = _dispatcher()

    def boom(board=None):
        raise RuntimeError("no such board")

    monkeypatch.setattr(kwd, "_kbc", lambda: SimpleNamespace(connect=boom))
    assert disp.head_of_line_priority("ghost") is None


def test_the_probe_reads_a_real_board_end_to_end(monkeypatch, tmp_path):
    """Not a stub: the real connection, query and effective-priority rank."""
    from pathlib import Path

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import profiles

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    kb.init_db()

    with kbc.connect() as conn:
        kb.create_task(conn, title="waiting", assignee="platform-coder", priority=2)

    assert _dispatcher().head_of_line_priority("default") == 2


def test_a_week_old_backlog_board_is_visited_before_a_fresh_p0_board(
    monkeypatch, tmp_path
):
    """The acceptance case, end to end: two real board DBs, one real order.

    A board holding a 7-day-old priority-0 card must be visited before a board
    holding a fresh priority-3 one, because visit order is what hands out the
    host-wide spawn budget — filesystem order gave the fresh card the slot.
    """
    import time as _time
    from pathlib import Path

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import profiles

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    kb.init_db()
    kb.create_board("second")

    with kbc.connect() as conn:
        kb.create_task(conn, title="fresh p0", assignee="worker", priority=3)
    with kbc.connect(board="second") as conn:
        backdrop = kb.create_task(
            conn, title="week-old backlog", assignee="worker", priority=0
        )
        conn.execute(
            "UPDATE tasks SET created_at = ? WHERE id = ?",
            (int(_time.time()) - 7 * 24 * 3600, backdrop),
        )
        conn.commit()

    disp = _dispatcher()
    monkeypatch.setattr(disp, "_board_slugs", lambda: ["default", "second"])

    assert disp.head_of_line_priority("default") == 3
    assert disp.head_of_line_priority("second") == kb.PRIORITY_AGE_BONUS_CAP  # 0 + capped bonus
    assert disp.board_visit_order() == ["second", "default"]
