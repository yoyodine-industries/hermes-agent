"""Dispatcher must leave per-task diagnostics for unknown assignees (#122422).

A card assigned to a profile that does not exist lands in the aggregate
``skipped_nonspawnable`` bucket with no per-task event, so ``show``/``tail``
never explain why the card sits in ``ready`` forever.
"""
from __future__ import annotations

from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


def _isolated_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_unknown_assignee_skip_writes_per_task_event(tmp_path, monkeypatch):
    """RED (#122422): the skip must append a per-task board event naming the
    missing profile, so ``tail``/``show`` reveal why the card never spawns."""
    _isolated_home(tmp_path, monkeypatch)
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="demo", assignee="no-such-profile")
        res = kbd.dispatch_once(conn, dry_run=False)
        kinds = [(e.kind, e.payload) for e in kb.list_events(conn, tid)]
        task = kb.get_task(conn, tid)
    assert res.skipped_nonspawnable == [tid]
    assert task is not None and task.status == "ready"
    matches = [p for (k, p) in kinds if k == "skipped_nonspawnable"]
    assert matches, f"no per-task skip event, kinds={[k for k, _ in kinds]}"
    assert isinstance(matches[0], dict) and matches[0].get("assignee") == "no-such-profile"


def test_unknown_assignee_skip_event_is_written_once(tmp_path, monkeypatch):
    """The condition never expires on its own: repeated ticks must not append a
    row each (one per minute forever; one per foreign home on a shared board),
    and a dry-run tick writes nothing."""
    _isolated_home(tmp_path, monkeypatch)
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="demo", assignee="no-such-profile")
        kbd.dispatch_once(conn, dry_run=True)
        assert "skipped_nonspawnable" not in [e.kind for e in kb.list_events(conn, tid)]
        for _ in range(3):
            kbd.dispatch_once(conn, dry_run=False)
        kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert kinds.count("skipped_nonspawnable") == 1


# ── The belt: the bucket ACTS (card t_c8ff9bc1) ─────────────────────────────
#
# Ruling §4 on ops/t_5b9dbe02: the create-time gate is the primary enforcement,
# but the dispatcher's skip branch must ALSO act on a bucket it still sees (rows
# already on a board). It files ONE deduped escalation card per (board, assignee),
# routed by the same deterministic seat resolver the stall escalation uses.
# Names DECLARED in ``kanban.control_plane_assignees`` are exempt.


_ESCALATION_KEY = "kanban-nonspawnable:default:no-such-profile"


def _arm_escalation(monkeypatch):
    """A live seat resolves; nothing is DECLARED, so the bucket is not exempt."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name == "ops-stl")
    monkeypatch.setattr(kbd, "_kanban_config", lambda: {"default_assignee": "ops-stl"})
    monkeypatch.setattr(kb, "control_plane_assignee_names", lambda: frozenset())


def _escalation_rows(conn):
    return conn.execute(
        "SELECT id, assignee, created_by, status FROM tasks WHERE idempotency_key = ?",
        (_ESCALATION_KEY,),
    ).fetchall()


def test_real_tick_files_exactly_one_escalation_card_idempotently(tmp_path, monkeypatch):
    """A real tick on an undeclared, non-live assignee files ONE card, and three
    ticks still leave exactly one (idempotency key ``kanban-nonspawnable:*``)."""
    _isolated_home(tmp_path, monkeypatch)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="demo", assignee="no-such-profile")
        _arm_escalation(monkeypatch)
        res = None
        for _ in range(3):
            res = kbd.dispatch_once(conn, dry_run=False, spawn_fn=lambda *a, **k: 4242)
        rows = _escalation_rows(conn)
    assert res is not None and res.skipped_nonspawnable == [tid]
    assert len(rows) == 1, f"expected exactly one escalation card, got {len(rows)}"
    assert rows[0]["assignee"] == "ops-stl"
    assert rows[0]["created_by"] == "kanban-dispatcher"


def test_dry_run_tick_files_nothing(tmp_path, monkeypatch):
    _isolated_home(tmp_path, monkeypatch)
    with kbc.connect() as conn:
        kb.create_task(conn, title="demo", assignee="no-such-profile")
        _arm_escalation(monkeypatch)
        kbd.dispatch_once(conn, dry_run=True, spawn_fn=lambda *a, **k: 4242)
        rows = _escalation_rows(conn)
    assert rows == []


def test_declared_control_plane_name_is_exempt(tmp_path, monkeypatch):
    """A declared control-plane pull lane's skip is the expected steady state, so
    it is NOT escalated."""
    _isolated_home(tmp_path, monkeypatch)
    from hermes_cli import profiles
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="demo", assignee="no-such-profile")
        monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
        monkeypatch.setattr(kbd, "_kanban_config", lambda: {"default_assignee": "ops-stl"})
        monkeypatch.setattr(
            kb, "control_plane_assignee_names", lambda: frozenset({"no-such-profile"}),
        )
        res = kbd.dispatch_once(conn, dry_run=False, spawn_fn=lambda *a, **k: 4242)
        rows = _escalation_rows(conn)
    assert res.skipped_nonspawnable == [tid]
    assert rows == []


def test_no_seat_still_never_breaks_the_tick(tmp_path, monkeypatch):
    """When no escalation seat resolves, the tick still completes (log-only)."""
    _isolated_home(tmp_path, monkeypatch)
    from hermes_cli import profiles
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="demo", assignee="no-such-profile")
        monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
        monkeypatch.setattr(kbd, "_kanban_config", lambda: {})
        monkeypatch.setattr(kb, "control_plane_assignee_names", lambda: frozenset())
        res = kbd.dispatch_once(conn, dry_run=False, spawn_fn=lambda *a, **k: 4242)
        rows = _escalation_rows(conn)
    assert res.skipped_nonspawnable == [tid]
    assert rows == []
