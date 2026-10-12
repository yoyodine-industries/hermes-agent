"""Age-aware dispatch ordering and the never-attempted (zero-run) ready signal.

Both faults this file pins are SILENT today:

* ``priority DESC, created_at ASC`` is a bounded-priority, unbounded-time queue.
  A bottom-rung card (P3 = 0) waits behind every P0/P1 arrival that keeps
  coming, and nothing reports the wait — the card is simply never claimed.
* A card whose assignee is not a live profile is bucketed
  ``skipped_nonspawnable`` on every tick, and the bad-tick health rule
  SUPPRESSES the stuck diagnostic for that bucket, so the card sits ``ready``
  with zero ``task_runs`` rows and no alert ever fires: the board looks idle,
  not broken.

Contracts pinned here: the single effective-priority order both order sites
read, the zero-run probe, the operator line it drives, and its export through
``board_stats``.

Port note (card t_d6ae62e7 PART C / create-default): the September revision of
this file also pinned create-time parking of an unspawnable assignee and a
schema/create default of P2. Both are EXCLUDED by the ruling — the create-time
guard is superseded by the live Invariant B door
(``kanban_gate_invariants.gate_assignee``, called from ``create_task``) and the
static default by the per-board birth policy
(``kanban_priority_policy`` + ``board.json.priority_policy``). Neither is ported.
"""
from __future__ import annotations

import contextlib
import time
from pathlib import Path

import pytest


@pytest.fixture()
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb

    kb.init_db()
    return home


@pytest.fixture()
def all_assignees_spawnable(monkeypatch):
    """Every ``profile_exists`` probe answers True (the dispatcher's spawn gate)."""
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


@contextlib.contextmanager
def _open():
    """A kanban connection for the current board (closed on exit)."""
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect_closing() as conn:
        yield conn


def _board(conn) -> None:
    from hermes_cli import kanban_db as kb

    kb.create_board(slug="default", name="Age priority tests")


def _plant_card(
    card_id: str, *, title: str, assignee, status: str = "ready", seconds_ago: float = 0.0
) -> None:
    """Insert a card row directly.

    The zero-run probe must catch cards that reach the board by ANY route — a
    restore, an import, a hand-edited DB, a card filed before the assignee's
    profile was retired — so these tests plant the row instead of going through
    ``create_task`` (which cannot refuse the card, because control-plane lanes
    pull their work with ``claim_task`` and hold no profile of their own).
    """
    with _open() as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (card_id, title, assignee, status, int(time.time() - seconds_ago)),
        )
        conn.commit()


def _age_card(card_id: str, seconds: float) -> None:
    """Plant a card's creation time ``seconds`` in the past — waiting time made real."""
    with _open() as conn:
        conn.execute(
            "UPDATE tasks SET created_at = ? WHERE id = ?",
            (int(time.time() - seconds), card_id),
        )
        conn.commit()


def _fake_spawn(*args, **kwargs) -> int:
    return 4242


# ---------------------------------------------------------------------------
# Ordering: waiting time is a first-class rank input
# ---------------------------------------------------------------------------


def test_effective_priority_credits_one_rung_per_hour_up_to_a_cap():
    from hermes_cli import kanban_db as kb

    now = 1_000_000.0
    # One rung per hour of waiting.
    assert kb.effective_priority(0, now - 3600, now=now) == 1
    assert kb.effective_priority(0, now - 2 * 3600, now=now) == 2
    # A bottom-rung card that has waited three hours TIES a fresh top-rung card
    # (0 + 3 == 3 + 0) and the tie breaks on created_at, so the waiter wins.
    assert kb.effective_priority(0, now - 3 * 3600, now=now) == kb.effective_priority(
        3, now, now=now
    )
    # Clamped: past the cap, priority differences are erased and it is pure FIFO.
    assert kb.priority_age_bonus(now - 99 * 3600, now=now) == kb.PRIORITY_AGE_BONUS_CAP
    # Fresh (or future-dated) cards rank on their declared priority alone.
    assert kb.effective_priority(2, now, now=now) == 2
    assert kb.priority_age_bonus(now + 60, now=now) == 0
    assert kb.priority_age_bonus(None, now=now) == 0


def test_waited_bottom_rung_card_is_claimed_before_a_fresh_top_rung_card(
    kanban_home, all_assignees_spawnable
):
    """The dispatcher's ready lane, the default listing order, and the claim itself."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    with _open() as conn:
        _board(conn)
        fresh_top = kb.create_task(
            conn, title="fresh P0", assignee="platform-coder", priority=3
        )
        waited_bottom = kb.create_task(
            conn, title="waited P3", assignee="platform-coder", priority=0
        )
    _age_card(waited_bottom, 4 * 3600)
    assert fresh_top != waited_bottom

    expected = [waited_bottom, fresh_top]
    with _open() as conn:
        # (1) the lane the dispatcher picks spawns from ...
        assert [row["id"] for row in kbd._lane_rows(conn, "ready")] == expected
        # (2) ... and the default order every CLI/API listing reads.
        assert [t.id for t in kb.list_tasks(conn, status="ready")] == expected
        # (3) End to end: the single spawn slot goes to the card that has waited.
        result = kbd.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False, max_spawn=1)
    assert [spawned[0] for spawned in result.spawned] == [waited_bottom]
    with _open() as conn:
        assert [t.id for t in kb.list_tasks(conn, status="running")] == [waited_bottom]
        assert [t.id for t in kb.list_tasks(conn, status="ready")] == [fresh_top]


def test_declared_priority_still_wins_within_the_same_age(kanban_home):
    """Age is a rank input, not a replacement: equal age ⇒ declared priority."""
    from hermes_cli import kanban_db as kb

    with _open() as conn:
        _board(conn)
        low = kb.create_task(conn, title="P3", priority=0)
        high = kb.create_task(conn, title="P0", priority=3)
        mid = kb.create_task(conn, title="P1", priority=2)
        # All three are the same age, so declared priority alone decides. (The
        # priority a card is BORN with is the per-board birth policy's call and
        # is deliberately not pinned here — ruling t_d76922e9.)
        assert [t.id for t in kb.list_tasks(conn, status="ready")] == [high, mid, low]


# ---------------------------------------------------------------------------
# The zero-run signal: ready, never attempted, past the alert age
# ---------------------------------------------------------------------------


def test_zero_run_probe_reports_a_dead_lane_card_and_names_it(kanban_home, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import profiles

    monkeypatch.setattr(
        profiles, "profile_exists", lambda name: name != "yoyodine-majordomo"
    )
    with _open() as conn:
        _board(conn)
        kb.create_task(conn, title="just filed", assignee="platform-coder")
    _plant_card("t_dead_lane", title="never runs", assignee="yoyodine-majordomo", seconds_ago=4 * 3600)
    dead = "t_dead_lane"

    with _open() as conn:
        probe = kb.zero_run_ready(conn)
        assert probe["count"] == 1
        assert probe["oldest_task_id"] == dead
        assert probe["oldest_assignee"] == "yoyodine-majordomo"
        assert probe["oldest_age_seconds"] >= 4 * 3600 - 5
        # The flag that separates "starved board" from "nothing will EVER run it".
        assert probe["oldest_spawnable"] is False
        # A freshly filed card is not a stall: the signal waits for the alert age.
        assert kb.zero_run_ready(conn, min_age_seconds=10 ** 9)["count"] == 0
        # ... and it is silent once nothing is stuck.
        conn.execute("DELETE FROM tasks WHERE id = ?", (dead,))
        conn.commit()
        assert kb.zero_run_ready(conn)["count"] == 0


def test_zero_run_probe_ignores_a_card_something_already_attempted(kanban_home):
    """An old card WITH a run row is a different (already-reported) problem."""
    from hermes_cli import kanban_db as kb

    with _open() as conn:
        _board(conn)
        tried = kb.create_task(conn, title="tried", assignee=None)
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at) "
            "VALUES (?, 'platform-coder', 'released', ?)",
            (tried, int(time.time())),
        )
        conn.commit()
    _age_card(tried, 4 * 3600)

    with _open() as conn:
        assert kb.zero_run_ready(conn)["count"] == 0


def test_board_stats_exports_the_zero_run_probe(kanban_home, monkeypatch):
    """The ops-API / board-stats export carries the signal, not just the log line."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with _open() as conn:
        _board(conn)
    _plant_card("t_retired_lane", title="dead", assignee="retired-lane", seconds_ago=4 * 3600)
    dead = "t_retired_lane"

    with _open() as conn:
        stats = kb.board_stats(conn)
    assert stats["zero_run_ready"]["count"] == 1
    assert stats["zero_run_ready"]["oldest_task_id"] == dead
    assert stats["zero_run_ready"]["oldest_spawnable"] is False


def test_stuck_ready_message_names_the_card_and_the_real_failure_mode():
    """The line an operator (or the gateway log) actually sees.

    Port note: the September revision drove this line from
    ``DispatcherHealth.observe_tick(stuck=...)``. That class was absent from the
    live line when this suite was ported (lost at the same 2026-09-23 checkout
    switch as the age bonus — see card t_d6ae62e7); card t_0cc0c4cb carried the
    class and its telemetry back onto the live line, leaving its wiring into the
    health surfaces to a platform-stl ruling. The message contract is what the
    probe owns, so it is pinned directly here; the live health surfaces (the
    gateway watcher and ``_cmd_daemon``) call it verbatim.
    """
    from hermes_cli import kanban_db_dispatch as kbd

    dead_lane = {
        "count": 2,
        "oldest_task_id": "t_abc123",
        "oldest_assignee": "yoyodine-majordomo",
        "oldest_age_seconds": 4 * 3600,
        "oldest_spawnable": False,
    }
    msg = kbd.stuck_ready_message(dead_lane)
    assert "t_abc123" in msg
    assert "yoyodine-majordomo" in msg
    assert "skipped_nonspawnable" in msg  # names the real failure mode
    assert "240 min" in msg

    # A spawnable assignee means the fault is elsewhere: say so, don't blame
    # the profile.
    stalled_only = dict(dead_lane, oldest_assignee="platform-coder", oldest_spawnable=True)
    other = kbd.stuck_ready_message(stalled_only)
    assert "skipped_nonspawnable" not in other
    assert "starved" in other

    # The board slug (supplied by the multi-board probe) is carried into the line.
    assert "kanban dispatcher[ops]" in kbd.stuck_ready_message(dict(dead_lane, board="ops"))
    # An unassigned card is labelled, not blank.
    assert "(unassigned)" in kbd.stuck_ready_message(dict(dead_lane, oldest_assignee=None))


# ---------------------------------------------------------------------------
# Operator surfaces: each one must name the stuck card, not just count it
# ---------------------------------------------------------------------------


def test_stats_names_a_never_attempted_card(kanban_home, monkeypatch, capsys):
    """``hermes kanban stats`` is an operator surface too: it must name the card.

    A count alone leaves the operator hunting; the line carries the id, the
    assignee, the age, and — for the dead-lane case — the actual remedy.
    """
    import argparse

    from hermes_cli import kanban as kb_cli
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with _open() as conn:
        _board(conn)
    _plant_card(
        "t_dead_lane", title="never runs", assignee="retired-lane", seconds_ago=4 * 3600
    )

    assert kb_cli._cmd_stats(argparse.Namespace(json=False, board=None)) == 0
    out = capsys.readouterr().out
    assert "t_dead_lane" in out
    assert "retired-lane" in out
    assert "never launch a worker" in out
