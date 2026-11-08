"""The iteration-budget wall: cause-faithful notices, the goal-loop arm, and the edit surface.

Three defects are pinned here, all of them instances of the same failure — a card died at its
iteration budget and nothing on a human surface said so:

1. ``_kb_timed_out`` (TUI) and ``_fmt_timed_out`` (gateway) rendered every ``outcome=timed_out`` run
   as a wall-clock timeout. A budget death carries no ``limit_seconds``, so the notice read
   ``timed out (max_runtime=0s)`` — a false diagnosis that points triage at a timeout config which
   had nothing to do with the failure, and the real counts (200/200) never reached the board.
   Root cause: the end-run branch of ``_record_task_failure`` dropped the caller's
   ``event_payload_extra``, so the cause never even reached the event.
2. There was no sanctioned way to give a card room: ``--goal``/``--goal-max-turns`` existed on
   ``create`` only, ``--max-runtime`` was absent from ``edit``, and the only fix was a hand-written
   SQL UPDATE.
3. A card whose work does not fit one run's iteration budget had to be noticed and repaired by hand.
   The dispatcher arms a bounded goal loop on the evidence the failed run recorded.

Every test states what it would catch; the red-before-green pairs were run against the unpatched
tree (see the task handoff).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _budget_death(conn, tid, *, used=200, total=200, unfinished=None):
    """A budget death exactly as the worker finalizer records it.

    The card is CLAIMED first because that is the real sequence: a dispatcher claims ``ready ->
    running`` and spawns the worker, and the finalizer's ``release_claim`` write only matches a
    running row.
    """
    kb.claim_task(conn, tid, claimer="test-worker")
    kbd._record_task_failure(
        conn, tid,
        error=f"Iteration budget exhausted ({used}/{total}) — task could not complete within "
              "the allowed iterations",
        outcome="timed_out", release_claim=True, end_run=True,
        event_payload_extra={
            "budget_used": used, "budget_max": total,
            **({"unfinished": unfinished} if unfinished else {}),
        },
    )


# ---------------------------------------------------------------------------
# 1. The cause reaches the event at all.
# ---------------------------------------------------------------------------

def test_non_tripping_failure_event_carries_the_callers_cause_detail(kanban_home):
    """The end-run branch must merge ``event_payload_extra``.

    Caught: the branch dropped it, so a budget death's event carried only
    ``{error, failures, retry_status}`` and every reader downstream — both notice formatters —
    could only guess the cause from ``limit_seconds``, which a budget death never sets.
    """
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="big card", assignee="alice")
        _budget_death(conn, tid, unfinished="wrote 3 of 7 files, tests not yet run")
        events = [e for e in kb.list_events(conn, tid) if e.kind == "timed_out"]
        assert events, "a failed run must leave a timed_out event"
        payload = events[-1].payload
        assert payload["budget_used"] == 200 and payload["budget_max"] == 200
        assert "wrote 3 of 7 files" in payload["unfinished"]
        # The run row the board reads back carries it too.
        runs = [r for r in kb.list_runs(conn, tid) if r.outcome == "timed_out"]
        assert runs and runs[-1].metadata.get("budget_max") == 200
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. The dispatcher arms a bounded goal loop on that evidence.
# ---------------------------------------------------------------------------

def test_budget_death_arms_a_bounded_goal_loop(kanban_home):
    """A card that died of iteration exhaustion gets room on its next dispatch.

    Caught: the retry was a single-shot run with the identical shape, so the fleet walked back into
    the same wall and needed a human to hand-edit the row.
    """
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="too big for one run", assignee="alice")
        _budget_death(conn, tid)
        armed = kbd.arm_goal_mode_after_budget_death(conn)
        task = kb.get_task(conn, tid)
        events = [e for e in kb.list_events(conn, tid) if e.kind == "goal_armed"]
    finally:
        conn.close()
    assert armed == [tid]
    assert task.goal_mode == 1
    assert task.goal_max_turns == kbd.DEFAULT_GOAL_ARM_TURNS
    assert task.max_runtime_seconds == kbd.DEFAULT_GOAL_ARM_MAX_RUNTIME_SECONDS
    # The arm is legible on the card, not a silent column flip.
    assert events and events[-1].payload["reason"] == "iteration_budget_exhausted"
    assert events[-1].payload["turns"] == kbd.DEFAULT_GOAL_ARM_TURNS


def test_arm_is_idempotent_and_spares_unrelated_failures(kanban_home):
    """Only a budget death arms, and it arms once.

    Caught: a blanket arm would pay a judge call per turn on every card in the fleet, and a
    re-arming pass would rewrite the room a human had deliberately set.
    """
    conn = kbc.connect()
    try:
        budget = kb.create_task(conn, title="budget", assignee="alice")
        _budget_death(conn, budget)
        other = kb.create_task(conn, title="crashed", assignee="alice")
        kb.claim_task(conn, other, claimer="test-worker")
        kbd._record_task_failure(
            conn, other, error="worker crashed (pid gone)", outcome="crashed",
            release_claim=True, end_run=True,
        )
        # A card whose author already chose its room must keep it.
        tuned = kb.create_task(
            conn, title="tuned", assignee="alice", goal_max_turns=12,
            max_runtime_seconds=900,
        )
        _budget_death(conn, tuned)

        first = kbd.arm_goal_mode_after_budget_death(conn)
        second = kbd.arm_goal_mode_after_budget_death(conn)
        other_task = kb.get_task(conn, other)
        tuned_task = kb.get_task(conn, tuned)
    finally:
        conn.close()
    assert set(first) == {budget, tuned}
    assert second == [], "a card already armed must not be re-armed"
    assert other_task.goal_mode in (0, None), "a crash is not a budget death"
    assert tuned_task.goal_max_turns == 12, "the author's turn budget wins"
    assert tuned_task.max_runtime_seconds == 900, "an existing cap is never overwritten"


def test_arm_can_be_disabled_by_config(kanban_home):
    """``kanban.goal_arm_on_budget_death: false`` restores the old single-shot retry."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="big", assignee="alice")
        _budget_death(conn, tid)
        armed = kbd.arm_goal_mode_after_budget_death(
            conn, kanban_cfg={"goal_arm_on_budget_death": False},
        )
        task = kb.get_task(conn, tid)
    finally:
        conn.close()
    assert armed == []
    assert not task.goal_mode


def test_dispatch_tick_reports_the_arm(kanban_home):
    """The tick counter is wired, so an armed card is not an invisible tick.

    Caught: a tick whose only activity was arming looked idle, and the gateway's summary log
    (``goal_armed=%d``) would have reported a zero it did not measure.
    """
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="big", assignee="alice")
        _budget_death(conn, tid)
        result = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242)
    finally:
        conn.close()
    assert tid in result.goal_armed


# ---------------------------------------------------------------------------
# 3. The cause is rendered, not guessed.
# ---------------------------------------------------------------------------

def test_tui_notice_names_the_iteration_budget_and_the_counts():
    from tui_gateway.session_notifications import _kb_timed_out

    text = _kb_timed_out(None, {"budget_used": 200, "budget_max": 200, "retry_status": "ready"}, "t")
    assert "iteration budget (200/200 turns)" in text
    assert "max_runtime=0s" not in text, "the wall clock must not be named for a budget death"


def test_tui_notice_shows_where_the_run_stopped_and_a_real_cap():
    from tui_gateway.session_notifications import _kb_timed_out

    with_stop = _kb_timed_out(
        None,
        {"budget_used": 200, "budget_max": 200, "unfinished": "3 of 7 files written\nmore text"},
        "t",
    )
    assert "3 of 7 files written" in with_stop
    assert "more text" not in with_stop, "one line only"

    reaped = _kb_timed_out(None, {"limit_seconds": 3600, "retry_status": "ready"}, "t")
    assert "3600s runtime cap" in reaped
    assert "ready" not in reaped and "re-queued" not in reaped
    # No ``unfinished`` key: an absent payload value must render NOTHING, not the literal "None".
    assert "None" not in reaped

    no_cap = _kb_timed_out(None, {}, "t")
    assert "no runtime cap set" in no_cap
    assert "None" not in no_cap


def test_tui_notice_does_not_claim_a_retry_that_did_not_happen():
    from tui_gateway.session_notifications import _kb_timed_out

    text = _kb_timed_out(None, {"budget_used": 5, "budget_max": 5, "retry_status": "review"}, "t")
    assert "re-queued as review" in text
    assert "will retry" not in text


def test_gateway_notice_names_the_iteration_budget_and_the_counts():
    from gateway.kanban_watchers_notifier import _EVENT_FORMATTERS

    class _Ev:
        payload = {"budget_used": 200, "budget_max": 200, "retry_status": "ready"}

    class _N:
        head = "H"

    msg, _, _ = _EVENT_FORMATTERS["timed_out"](_Ev(), _N())
    assert "its iteration budget (200/200 turns)" in msg
    assert "minute limit" not in msg


def test_gateway_notice_keeps_the_wall_clock_case_and_admits_an_unset_cap():
    from gateway.kanban_watchers_notifier import _EVENT_FORMATTERS

    class _Ev:
        def __init__(self, payload):
            self.payload = payload

    class _N:
        head = "H"

    reaped, _, _ = _EVENT_FORMATTERS["timed_out"](_Ev({"limit_seconds": 3600}), _N())
    assert "60-minute limit" in reaped
    unset, _, _ = _EVENT_FORMATTERS["timed_out"](_Ev({"budget_used": "?", "budget_max": None}), _N())
    assert "no runtime cap was set" in unset


def test_gateway_notice_renders_the_arm():
    from gateway.kanban_watchers_notifier import _EVENT_FORMATTERS

    class _Ev:
        payload = {"reason": "iteration_budget_exhausted", "turns": 5}

    class _N:
        head = "H"

    msg, _, _ = _EVENT_FORMATTERS["goal_armed"](_Ev(), _N())
    assert "5-turn goal loop" in msg


# ---------------------------------------------------------------------------
# 4. The sanctioned edit surface.
# ---------------------------------------------------------------------------

def test_edit_sets_and_clears_the_cards_room(kanban_home):
    """``edit`` must set goal loop, turn budget and runtime cap — and clear them.

    Caught: there was no surface at all for these three fields on an existing card; the case card
    needed a hand-written SQL UPDATE.
    """
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="room", assignee="alice")
        assert kb.edit_task(
            conn, tid, goal_mode=True, goal_max_turns=5, max_runtime_seconds=1800,
        )
        task = kb.get_task(conn, tid)
        assert (task.goal_mode, task.goal_max_turns, task.max_runtime_seconds) == (1, 5, 1800)
        events = [e for e in kb.list_events(conn, tid) if e.kind == "edited"]
        assert events[-1].payload["fields"] == [
            "goal_mode", "goal_max_turns", "max_runtime_seconds",
        ], "which fields changed must be recorded"
        assert events[-1].payload["values"]["max_runtime_seconds"] == 1800

        assert kb.edit_task(
            conn, tid, goal_mode=False, goal_max_turns=0, clear_max_runtime=True,
        )
        task = kb.get_task(conn, tid)
        assert (task.goal_mode, task.goal_max_turns, task.max_runtime_seconds) == (0, None, None)
    finally:
        conn.close()


def test_edit_leaves_room_alone_when_unasked(kanban_home):
    """An unrelated edit must not clear the room (tri-state, not boolean).

    Caught: ``store_true``'s ``False`` default would have turned the goal loop off on every
    ``--priority`` edit.
    """
    conn = kbc.connect()
    try:
        tid = kb.create_task(
            conn, title="room", assignee="alice", goal_mode=True, goal_max_turns=7,
            max_runtime_seconds=600,
        )
        assert kb.edit_task(conn, tid, priority=3)
        task = kb.get_task(conn, tid)
        assert (task.priority, task.goal_mode, task.goal_max_turns, task.max_runtime_seconds) == (
            3, 1, 7, 600,
        )
    finally:
        conn.close()


def test_parse_duration_normalises_a_degenerate_zero_cap():
    """``--max-runtime 0`` is not a duration; it must not store a cap that reaps instantly.

    Caught: the create path stored ``0``, and the worker prompt then read ``Max runtime: 0s``.
    """
    from hermes_cli.kanban import _parse_duration

    assert _parse_duration("0") is None
    assert _parse_duration("none") is None
    assert _parse_duration("30m") == 1800
    with pytest.raises(ValueError):
        _parse_duration("30 bananas")


def test_edit_help_lists_the_room_flags():
    """The three fields are discoverable on ``edit``, not only in the source."""
    import argparse
    import contextlib
    import io

    from hermes_cli.kanban_parser import build_parser

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    build_parser(parser.add_subparsers(dest="command"))
    buf = io.StringIO()
    with contextlib.suppress(SystemExit), contextlib.redirect_stdout(buf):
        parser.parse_args(["kanban", "edit", "--help"])
    help_text = buf.getvalue()
    for flag in ("--goal", "--no-goal", "--goal-max-turns", "--max-runtime"):
        assert flag in help_text, f"{flag} missing from `hermes kanban edit --help`"


def test_show_renders_the_room(kanban_home, capsys):
    """``show`` must display the cap: a stored-but-invisible field reads as a flag that did not stick.

    Caught: ``create --max-runtime 86400`` wrote the column and the human view never printed it, so
    the operator reasonably concluded the flag was broken.
    """
    from hermes_cli.kanban import _cmd_show

    conn = kbc.connect()
    try:
        tid = kb.create_task(
            conn, title="visible", assignee="alice", max_runtime_seconds=86400,
            goal_mode=True, goal_max_turns=5,
        )
    finally:
        conn.close()

    class _Args:
        task_id = tid
        json = False
        run_state = None
        run_state_latest = False

    assert _cmd_show(_Args()) == 0
    out = capsys.readouterr().out
    assert "max-runtime:86400s" in out.replace(" ", "")
    assert "goal-loop" in out and "max 5 turns" in out


def test_a_blocked_budget_death_is_armed_too(kanban_home):
    """The arm keys on the failure EVIDENCE, not on the phase the card was parked in.

    Caught: a budget death on a card's LAST permitted attempt trips the failure breaker, which parks
    the card ``blocked``; the arm read only ``ready``/``todo``, so the cards that had proved they
    needed room — and that the ready queue could no longer even offer — were the one class it skipped.
    """
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="blocked budget death", assignee="alice")
        kb.claim_task(conn, tid, claimer="test-worker")
        # force_trip mirrors the real breaker: the last permitted attempt died of iteration
        # exhaustion, so the card is parked ``blocked`` carrying the budget error as its evidence.
        kbd._record_task_failure(
            conn, tid,
            error="Iteration budget exhausted (200/200) — task could not complete within "
                  "the allowed iterations",
            outcome="timed_out", force_trip=True, release_claim=True, end_run=True,
        )
        parked = kb.get_task(conn, tid)
        assert parked.status == "blocked", "the breaker must have parked the card"
        assert parked.goal_mode in (0, None), "the seed must start un-armed"

        result = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242)
        task = kb.get_task(conn, tid)
        events = [e for e in kb.list_events(conn, tid) if e.kind == "goal_armed"]

        # Second tick: the row already carries goal_mode=1, so the WHERE clause excludes it.
        again = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242)
        task_after = kb.get_task(conn, tid)
    finally:
        conn.close()

    assert tid in result.goal_armed, "a blocked budget death must be armed on the tick"
    assert task.goal_mode == 1
    assert task.goal_max_turns == kbd.DEFAULT_GOAL_ARM_TURNS
    assert task.max_runtime_seconds == kbd.DEFAULT_GOAL_ARM_MAX_RUNTIME_SECONDS
    assert task.status == "blocked", "the arm must not unblock the card"
    assert events and events[-1].payload["reason"] == "iteration_budget_exhausted"
    assert again.goal_armed == [], "re-arming a parked card would flap it"
    assert task_after.goal_mode == 1
    assert task_after.status == "blocked"
