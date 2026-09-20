"""Age-aware dispatch ordering, the never-attempted (zero-run) ready signal, and
the create-time parking of an assignee the dispatcher can never spawn for.

Both faults this file pins are SILENT today:

* ``priority DESC, created_at ASC`` is a bounded-priority, unbounded-time queue.
  A bottom-rung card (P3 = 0) waits behind every P0/P1 arrival that keeps
  coming, and nothing reports the wait — the card is simply never claimed.
* A card whose assignee is not a live profile is bucketed
  ``skipped_nonspawnable`` on every tick, and dispatch health SUPPRESSES the
  stuck diagnostic for that bucket, so the card sits ``ready`` with zero
  ``task_runs`` rows and no alert ever fires: the board looks idle, not broken.

Contracts pinned here: the single effective-priority order both order sites
read, the default priority (normal, never the bottom rung), the zero-run probe
and the health line it drives, its export through ``board_stats``, and
create-time parking of an unspawnable assignee.
"""
from __future__ import annotations

import contextlib
import json
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
        normal = kb.create_task(conn, title="unstated")
        # create_task's default must be NORMAL (P2 = 1), never the bottom rung:
        # an unstated priority is not a statement of low priority.
        unstated = kb.get_task(conn, normal)
        assert unstated is not None and unstated.priority == 1
        assert [t.id for t in kb.list_tasks(conn, status="ready")] == [high, normal, low]


def test_schema_default_priority_is_normal_not_bottom_rung(kanban_home):
    with _open() as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at) VALUES (?, ?, 'ready', ?)",
            ("t_raw_default", "row inserted without a priority", int(time.time())),
        )
        conn.commit()
        value = conn.execute(
            "SELECT priority FROM tasks WHERE id = 't_raw_default'"
        ).fetchone()[0]
    assert value == 1


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


def test_dispatch_health_warns_on_stuck_ready_and_clears_when_healthy():
    """The line an operator (or the gateway log) actually sees."""
    from hermes_cli import kanban_db_dispatch as kbd

    health = kbd.DispatcherHealth()
    stuck = {
        "count": 2,
        "oldest_task_id": "t_abc123",
        "oldest_assignee": "yoyodine-majordomo",
        "oldest_age_seconds": 4 * 3600,
        "oldest_spawnable": False,
    }
    report = health.observe_tick([], stuck=stuck, now=1000.0)
    assert report is not None
    assert report.level == "warning"
    assert "t_abc123" in report.message
    assert "yoyodine-majordomo" in report.message
    assert "skipped_nonspawnable" in report.message  # names the real failure mode

    # A spawnable assignee means the fault is elsewhere: say so, don't blame
    # the profile.
    health.reset()
    stalled_only = dict(stuck, oldest_assignee="platform-coder", oldest_spawnable=True)
    spawnable_report = health.observe_tick([], stuck=stalled_only, now=2000.0)
    assert spawnable_report is not None
    assert "skipped_nonspawnable" not in spawnable_report.message

    # Healthy board: nothing to say.
    health.reset()
    assert health.observe_tick([], stuck={"count": 0}, now=3000.0) is None


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


# ---------------------------------------------------------------------------
# Create-time signal: park the card the dispatcher could never spawn for
# ---------------------------------------------------------------------------


def test_create_task_parks_an_unspawnable_assignee_with_a_named_event(
    kanban_home, monkeypatch
):
    """A card for a lane with no profile is created — but never silently.

    Refusing the create outright would break the control-plane lanes that
    legitimately pull their work with ``claim_task`` and carry no profile, so the
    fault is PARKED BY NAME on the card instead: a durable event that says which
    lane, which dispatcher bucket it lands in, and the two fixes. The
    never-attempted probe escalates it if nobody acts.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import profiles

    monkeypatch.setattr(
        profiles, "profile_exists", lambda name: name != "yoyodine-majordomo"
    )
    with _open() as conn:
        _board(conn)
        doomed = kb.create_task(conn, title="doomed", assignee="yoyodine-majordomo")
        events = [
            dict(row)
            for row in conn.execute(
                "SELECT kind, payload FROM task_events WHERE task_id = ?", (doomed,)
            ).fetchall()
        ]
        # A real profile, and an unassigned card parked for triage, stay clean.
        real = kb.create_task(conn, title="real lane", assignee="platform-coder")
        unassigned = kb.create_task(conn, title="triage me")
        clean = conn.execute(
            "SELECT COUNT(*) AS n FROM task_events WHERE task_id IN (?, ?) "
            "AND kind = 'assignee_unspawnable'",
            (real, unassigned),
        ).fetchone()["n"]
        # ... and the card is still a normal, claimable card.
        assert {t.id for t in kb.list_tasks(conn, status="ready")} == {
            doomed,
            real,
            unassigned,
        }

    assert [e["kind"] for e in events].count("assignee_unspawnable") == 1
    payload = json.loads(
        next(e["payload"] for e in events if e["kind"] == "assignee_unspawnable")
    )
    assert payload["assignee"] == "yoyodine-majordomo"  # names the lane
    assert "skipped_nonspawnable" in payload["reason"]  # names the dispatcher bucket
    assert "reassign" in payload["fix"]  # names the fix
    assert "claim_task" in payload["fix"]  # ... and the lane that keeps working
    assert clean == 0


# ---------------------------------------------------------------------------
# The head-of-line probe: one rank per board
#
# ``gateway.kanban_watchers_dispatcher.tick_once`` allocates the one host-wide
# spawn budget in board visit order, so it needs a single number per board — and
# that number has to come from the SAME order the spawn loops use, waiting time
# included, or a board holding a week-old P3 card keeps losing to a board with
# one fresh P0 card.
# ---------------------------------------------------------------------------


def _plant_ranked(card_id: str, *, priority: int, seconds_ago: float, assignee):
    """A ready card with an exact effective priority (awaited: no workers)."""
    import time as _time

    created = int(_time.time() - seconds_ago)
    with _open() as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, priority, created_at) "
            "VALUES (?, ?, ?, 'ready', ?, ?)",
            (card_id, card_id, assignee, priority, created),
        )
        conn.commit()


def test_head_of_line_priority_ranks_a_board_by_its_most_starved_card(
    kanban_home, monkeypatch
):
    """A week of waiting outranks any declared priority (the age bonus, capped)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    _plant_ranked("t_old_backlog", priority=0, seconds_ago=7 * 24 * 3600, assignee="alice")
    _plant_ranked("t_fresh_p0", priority=3, seconds_ago=0, assignee="alice")

    with _open() as conn:
        rank = kbd.head_of_line_priority(conn)
        assert rank == kb.PRIORITY_AGE_BONUS_CAP  # 0 + capped bonus ≠ 3
        assert rank > kb.effective_priority(3, None)  # ... and beats the fresh P0

    # Drop the backlog card: the fresh P0 becomes the head of line.
    with _open() as conn:
        conn.execute("DELETE FROM tasks WHERE id = 't_old_backlog'")
        conn.commit()
        assert kbd.head_of_line_priority(conn) == 3

        # Order is the LANE order, not another sort: the head changes when a
        # higher-ranked card arrives, and the probe follows it.
        conn.execute(
            "UPDATE tasks SET priority = 0 WHERE id = 't_fresh_p0'"
        )
        conn.commit()
        assert kbd.head_of_line_priority(conn) == 0


def test_head_of_line_priority_is_none_when_no_card_could_be_spawned(
    kanban_home, monkeypatch
):
    """No spawnable card ⇒ no rank. Unranked is not the same as unvisited."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: name == "alice")
    with _open() as conn:
        assert kbd.head_of_line_priority(conn) is None
        # Claimed work is not waiting for a worker.
        claimed = kb.create_task(conn, title="claimed", assignee="alice", priority=3)
        assert kb.claim_task(conn, claimed) is not None
        assert kbd.head_of_line_priority(conn) is None
        # Neither is a control-plane lane (pulls work with claim_task)...
        kb.create_task(conn, title="control-plane", assignee="orion-cc", priority=3)
        assert kbd.head_of_line_priority(conn) is None
        # ... nor a card nobody owns.
        kb.create_task(conn, title="unassigned", priority=3)
        assert kbd.head_of_line_priority(conn) is None

        # One real card, and the board has a rank again.
        real = kb.create_task(conn, title="real", assignee="alice", priority=2)
        assert kbd.head_of_line_priority(conn) == 2
        assert kbd.spawnable_pending_ids(conn) == [real]
