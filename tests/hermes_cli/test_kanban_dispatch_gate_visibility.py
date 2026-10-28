"""The run gate must be READABLE: every held card names the gate, its value, and itself.

Ruling: card ``t_843cb0a9`` (operator SEV1 — "dispatch spawns 0 with every skip bucket
empty"). The measured live defect was NOT a broken candidate filter: the board was at
its own ``max_spawn`` ceiling, ``_tick_spawn_budget`` declined the whole tick, and
``dispatch --dry-run --json`` published none of the two fields that recorded it
(``spawn_budget_blocked`` / ``deferred_board_capped``). From every operator surface a
saturated board was indistinguishable from a wedged one.

Three behaviour contracts:
1. a budget-held tick NAMES the cards it held, in a bucket that is published, together
   with the cap that held them and the measured running count (``spawn_budget``);
2. the cap the environment implies ACROSS boards (``max_spawn`` x dispatch boards)
   against ``max_in_progress`` is a NUMBER a reader can find (``measure_host_budget``);
3. a slot that frees is used by the head card on the very next pass — demonstrated,
   never inferred.
"""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_ops


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


def _seed_running(n: int, board=None) -> tuple[list, list]:
    """``n`` claimed (running) cards, plus ``n`` ready ones; returns both id lists."""
    running: list = []
    ready: list = []
    with kbc.connect(board=board) as conn:
        for i in range(n):
            tid = kb.create_task(conn, title=f"run{i}", assignee="alice")
            assert kb.claim_task(conn, tid) is not None
            running.append(tid)
        for i in range(n):
            ready.append(kb.create_task(conn, title=f"ready{i}", assignee="alice"))
    return running, ready


# ---------------------------------------------------------------------------
# 1. A budget-held tick names the gate, its value and the cards it held
# ---------------------------------------------------------------------------


def test_max_spawn_block_measures_the_cap_it_hit(kanban_home, all_assignees_spawnable):
    running, ready = _seed_running(2)

    spawns: list = []
    with kbc.connect() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), max_spawn=2)

    assert spawns == []
    assert res.spawn_budget_blocked == "max_spawn"
    # The cards the ceiling held are NAMED...
    assert set(res.deferred_board_capped) == set(ready)
    # ...and the cap that held them is a measured number, not a name alone.
    assert res.spawn_budget_detail["blocked_by"] == "max_spawn"
    assert res.spawn_budget_detail["cap"] == 2
    assert res.spawn_budget_detail["board_running"] == len(running)
    assert res.spawn_budget_detail["deferred"] == len(ready)


def test_in_progress_block_names_the_cards_it_held(kanban_home, all_assignees_spawnable):
    """The HOST-wide gate used to record a bare name: no bucket, no card ids."""
    running, ready = _seed_running(1)

    spawns: list = []
    with kbc.connect() as conn:
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_spawn=8, max_in_progress=1,
        )

    assert spawns == []
    assert res.spawn_budget_blocked == "max_in_progress"
    assert set(res.deferred_in_progress_capped) == set(ready)
    assert res.spawn_budget_detail["blocked_by"] == "max_in_progress"
    assert res.spawn_budget_detail["cap"] == 1
    assert res.spawn_budget_detail["host_running"] == len(running)
    assert res.spawn_budget_detail["host_cap"] == 1


def test_describe_suppression_counts_the_host_deferral():
    """A capped host must never read as an idle queue on the health line."""
    res = kbd.DispatchResult()
    res.deferred_in_progress_capped = ["t_1", "t_2"]
    assert "in_progress_deferred=2" in kbd.describe_suppression([res])


# ---------------------------------------------------------------------------
# 2. dispatch --dry-run --json publishes it (the operator surface from the card)
# ---------------------------------------------------------------------------


def _dispatch_args(**overrides) -> Namespace:
    values = dict(json=True, dry_run=True, max=None, failure_limit=3)
    values.update(overrides)
    return Namespace(**values)


def test_cli_json_publishes_the_held_gate(kanban_home, all_assignees_spawnable, capsys):
    _running, ready = _seed_running(2)

    rc = kanban_ops._cmd_dispatch(_dispatch_args(max=2))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["spawned"] == []
    # The two fields the live SEV1 could not see:
    assert payload["spawn_budget_blocked"] == "max_spawn"
    assert set(payload["deferred_board_capped"]) == set(ready)
    # ...plus the measured cap behind them.
    assert payload["spawn_budget"]["blocked_by"] == "max_spawn"
    assert payload["spawn_budget"]["cap"] == 2
    assert payload["spawn_budget"]["board_running"] == 2


def test_cli_text_output_names_the_held_cards(kanban_home, all_assignees_spawnable, capsys):
    _running, ready = _seed_running(2)

    kanban_ops._cmd_dispatch(_dispatch_args(json=False, max=2))
    out = capsys.readouterr().out

    held_lines = [line for line in out.splitlines() if line.startswith("Held (")]
    assert held_lines, f"no Held line in:\n{out}"
    line = held_lines[0]
    assert "max_spawn=2" in line
    assert "2 running" in line
    for tid in ready:
        assert tid in out


def test_cli_json_also_reports_spawn_failures_and_tick_yield(
    kanban_home, all_assignees_spawnable, capsys
):
    """The other two silent zeros of the same class ride the same payload."""
    kanban_ops._cmd_dispatch(_dispatch_args())
    payload = json.loads(capsys.readouterr().out)
    for key in ("spawn_failed", "tick_yielded", "deferred_host_capped", "deferred_in_progress_capped"):
        assert key in payload


# ---------------------------------------------------------------------------
# 3. The host ceiling is a measured number (max_spawn x boards vs max_in_progress)
# ---------------------------------------------------------------------------


def test_host_ceiling_reconciles_boards_against_max_in_progress(kanban_home):
    kb.create_board("financially")
    kb.create_board("research")

    measured = kbd.measure_host_budget(max_spawn=8, max_in_progress=8)

    assert set(measured["boards"]) == {kb.DEFAULT_BOARD, "financially", "research"}
    assert measured["max_spawn"] == 8
    assert measured["max_in_progress"] == 8
    assert measured["ceiling_sum"] == 24          # 3 boards x 8, the number that was implicit
    assert measured["oversubscribed"] is True
    assert measured["unreachable_slots"] == 16    # 24 - 8: capacity no schedule can use


def test_host_ceiling_fits_when_per_board_caps_are_set(kanban_home):
    kb.create_board("financially")
    kb.create_board("research")

    measured = kbd.measure_host_budget(
        max_spawn=8,
        max_in_progress=8,
        ceilings={kb.DEFAULT_BOARD: 3, "financially": 3, "research": 2},
    )

    assert measured["ceiling_sum"] == 8
    assert measured["oversubscribed"] is False
    assert measured["unreachable_slots"] == 0


def test_host_ceiling_is_unmeasured_without_a_host_cap(kanban_home):
    """No cap is not oversubscription: report none rather than invent one."""
    measured = kbd.measure_host_budget(max_spawn=8, max_in_progress=None)
    assert measured["oversubscribed"] is False
    assert measured["unreachable_slots"] is None


def test_cli_json_reports_the_host_ceiling(kanban_home, all_assignees_spawnable, capsys):
    kb.create_board("financially")
    _seed_running(2)

    kanban_ops._cmd_dispatch(_dispatch_args(max=2))
    payload = json.loads(capsys.readouterr().out)

    assert payload["host_budget"]["max_spawn"] == 2
    assert payload["host_budget"]["ceiling_sum"] == 4      # 2 boards x max_spawn 2


def test_cli_text_output_flags_an_oversubscribed_host(
    kanban_home, all_assignees_spawnable, capsys, monkeypatch
):
    from hermes_cli import config as hermes_config

    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda *a, **kw: {"kanban": {"max_spawn": 8, "max_in_progress": 8}},
    )
    kb.create_board("financially")

    kanban_ops._cmd_dispatch(_dispatch_args(json=False))
    out = capsys.readouterr().out

    host_lines = [line for line in out.splitlines() if line.startswith("Host ceiling:")]
    assert host_lines, f"no Host ceiling line in:\n{out}"
    assert "2 x max_spawn=8 = 16" in host_lines[0]
    assert "max_in_progress=8" in host_lines[0]
    assert "8 slot(s)" in host_lines[0]


# ---------------------------------------------------------------------------
# 4. A freed slot is used by the head card on the NEXT pass (demonstrated)
# ---------------------------------------------------------------------------


def test_a_freed_slot_starts_the_head_card_on_the_next_pass(
    kanban_home, all_assignees_spawnable
):
    running, ready = _seed_running(2)

    spawns: list = []
    with kbc.connect() as conn:
        first = kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), max_spawn=2)
        assert first.spawned == []
        assert set(first.deferred_board_capped) == set(ready)

        # A worker finishes: one slot frees, nothing else changes.
        conn.execute("update tasks set status='done' where id = ?", (running[0],))
        conn.commit()

        second = kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), max_spawn=2)

    assert len(spawns) == 1
    assert second.spawned[0][0] in ready
    assert second.spawn_budget_blocked is None
