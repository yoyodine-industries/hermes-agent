"""A blocked card's MISSING reason is repaired IN PLACE — no unblock, no loop breaker.

The defect this guards: ``hermes kanban block`` refused an already-blocked card, so the
only route to a missing ``block_reason`` was unblock → block, which trips the block-loop
breaker and parks the card in ``triage`` carrying a LOOP verdict instead of the operator
dependency. The repair is ``set_block_reason`` (and ``block``/the tool falling back to
it), with the correction recorded as its own ``block_reason_set`` event so the card's
``blocked_at`` and the loop counter are untouched.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_diagnostics as kd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in (
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    return home


REASON = "Approve or defer the consent-gated converge before the 2026-11-30 window"


def _sealed_block(conn, title="sealed card"):
    """The real-world shape: ``hermes kanban block <id>`` with NO reason."""
    tid = kb.create_task(conn, title=title, assignee="worker")
    assert kb.block_task(conn, tid) is True
    assert kb.get_task(conn, tid).status == "blocked"
    assert kb.block_record(conn, tid) == ("", "")
    return tid


def _events(conn, tid):
    return kb.list_events(conn, tid)


def _blocked_at(conn, tid):
    """The card's blocked_at: its newest ``blocked`` event (nothing else sets it)."""
    stamps = [e.created_at for e in _events(conn, tid) if e.kind == "blocked"]
    assert stamps, "fixture must have a blocked event"
    return max(stamps)


def _diag_kinds(conn, tid):
    return [
        d.kind
        for d in kd.compute_task_diagnostics(
            kb.get_task(conn, tid), _events(conn, tid), kb.list_runs(conn, tid)
        )
    ]


def _time_out_once(conn, tid):
    """Park the card the way the dispatcher's failure breaker does (blocked + gave_up)."""
    kb.claim_task(conn, tid)
    kbd._set_worker_pid(conn, tid, os.getpid())
    started = int(time.time()) - 30
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET started_at = ? WHERE id = ?", (started, tid))
        conn.execute(
            "UPDATE task_runs SET started_at = ? "
            "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
            (started, tid),
        )
    assert tid in kbd.enforce_max_runtime(conn, signal_fn=lambda pid, sig: None)


# ---------------------------------------------------------------------------
# (a) the reason-only write sets the record and the card is STILL blocked
# ---------------------------------------------------------------------------


def test_reason_only_write_sets_reason_and_kind_and_stays_blocked(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _sealed_block(conn)
        blocked_at = _blocked_at(conn, tid)

        assert kb.set_block_reason(conn, tid, reason=REASON, kind="needs_input") is True

        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind == "needs_input"
        assert kb.block_record(conn, tid) == (REASON, "needs_input")
        # The card never left `blocked`, so `blocked_at` (the original event) is unchanged.
        assert _blocked_at(conn, tid) == blocked_at
        # The correction is a NEW event kind; history is not rewritten and no re-block is faked.
        kinds = [e.kind for e in _events(conn, tid)]
        assert kinds.count("blocked") == 1
        assert kinds.count("block_reason_set") == 1
        correction = [e for e in _events(conn, tid) if e.kind == kb.BLOCK_REASON_EVENT][-1]
        assert correction.payload["reason"] == REASON
        assert correction.payload["kind"] == "needs_input"
        assert correction.payload["previous_reason"] is None
        assert correction.payload["previous_kind"] is None
        assert correction.payload["in_place"] is True


def test_reason_only_write_replaces_a_wrong_typed_record(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="with a stale reason", assignee="worker")
        kbc_ok = kb.block_task(conn, tid, reason="stale wording", kind="needs_input")
        assert kbc_ok is True
        assert kb.block_record(conn, tid) == ("stale wording", "needs_input")

        assert kb.set_block_reason(conn, tid, reason=REASON) is True

        assert kb.get_task(conn, tid).status == "blocked"
        assert kb.block_record(conn, tid) == (REASON, "needs_input")
        correction = [e for e in _events(conn, tid) if e.kind == kb.BLOCK_REASON_EVENT][-1]
        assert correction.payload["previous_reason"] == "stale wording"
        assert correction.payload["previous_kind"] == "needs_input"
        assert correction.payload["kind_defaulted"] is False


def test_reason_only_write_defaults_an_untyped_breaker_park_to_needs_input(kanban_home, monkeypatch):
    """The failure breaker parks cards with ``block_kind IS NULL``; a reason repair must
    make the record answerable, so the sticky 'a human must act' class is the default —
    recorded as DEFAULTED, never presented as if the caller asserted it."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="breaker parked", assignee="worker",
                            max_runtime_seconds=1)
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
        _time_out_once(conn, tid)
        _time_out_once(conn, tid)
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind is None

        assert kb.set_block_reason(conn, tid, reason=REASON) is True

        assert kb.get_task(conn, tid).block_kind == "needs_input"
        correction = [e for e in _events(conn, tid) if e.kind == kb.BLOCK_REASON_EVENT][-1]
        assert correction.payload["kind_defaulted"] is True
        assert correction.payload["kind"] == "needs_input"


def test_reason_only_write_refuses_a_card_that_is_not_blocked(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="active", assignee="worker")
        before = _events(conn, tid)
        assert kb.set_block_reason(conn, tid, reason=REASON) is False
        assert [e.kind for e in _events(conn, tid)] == [e.kind for e in before]
        with pytest.raises(ValueError):
            kb.set_block_reason(conn, tid, reason="   ")
        with pytest.raises(ValueError):
            kb.set_block_reason(conn, tid, reason=REASON, kind="not-a-kind")


# ---------------------------------------------------------------------------
# (b) the same write twice moves nothing and never trips the loop detector
# ---------------------------------------------------------------------------


def test_reason_only_write_is_idempotent_and_never_trips_the_breaker(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _sealed_block(conn)
        blocked_at = _blocked_at(conn, tid)
        conn.execute("UPDATE tasks SET block_recurrences = 1 WHERE id = ?", (tid,))
        conn.commit()

        assert kb.set_block_reason(conn, tid, reason=REASON, kind="needs_input") is True
        after_first = [e.kind for e in _events(conn, tid)]
        assert kb.set_block_reason(conn, tid, reason=REASON, kind="needs_input") is True
        assert kb.set_block_reason(conn, tid, reason=REASON, kind="needs_input") is True

        task = kb.get_task(conn, tid)
        assert task.status == "blocked"          # never moved
        assert task.block_kind == "needs_input"
        assert task.block_recurrences == 1       # a correction is not a recurrence
        assert [e.kind for e in _events(conn, tid)] == after_first  # no duplicate audit
        assert [e.kind for e in _events(conn, tid)].count("triage") == 0
        assert _blocked_at(conn, tid) == blocked_at


# ---------------------------------------------------------------------------
# (c) a card that GENUINELY cycles still reaches triage with block_loop_detected
# ---------------------------------------------------------------------------


def test_genuine_block_cycle_after_a_correction_still_reaches_triage(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="genuinely cycling", assignee="worker")
        assert kb.block_task(conn, tid, reason="first", kind="needs_input") is True
        # The repair does not launder a real cycle: the counter is the breaker's subject.
        assert kb.set_block_reason(conn, tid, reason=REASON, kind="needs_input") is True
        assert kb.unblock_task(conn, tid) is True
        assert kb.get_task(conn, tid).status == "ready"

        assert kb.block_task(conn, tid, reason=REASON, kind="needs_input") is True

        task = kb.get_task(conn, tid)
        assert task.status == "triage"
        kinds = [e.kind for e in _events(conn, tid)]
        assert "block_loop_detected" in kinds
        assert task.block_recurrences >= kb.BLOCK_RECURRENCE_LIMIT


# ---------------------------------------------------------------------------
# (d) diagnostics report the class, and the repair clears it
# ---------------------------------------------------------------------------


def test_diagnostics_report_blocked_without_reason_and_the_repair_clears_it(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _sealed_block(conn)
        assert "blocked_without_reason" in _diag_kinds(conn, tid)

        assert kb.set_block_reason(conn, tid, reason=REASON, kind="needs_input") is True
        assert "blocked_without_reason" not in _diag_kinds(conn, tid)

        diag = [d for d in kd.compute_task_diagnostics(
            kb.get_task(conn, tid), _events(conn, tid), kb.list_runs(conn, tid))]
        assert diag == [] or all(d.kind != "blocked_without_reason" for d in diag)


def test_diagnostics_name_the_repair_command(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _sealed_block(conn)
        diag = [
            d for d in kd.compute_task_diagnostics(
                kb.get_task(conn, tid), _events(conn, tid), kb.list_runs(conn, tid)
            )
            if d.kind == "blocked_without_reason"
        ]
        assert len(diag) == 1
        rendered = " ".join(
            f"{a.label} {a.payload.get('command', '')}" for a in diag[0].actions
        )
        assert f"hermes kanban set-block-reason {tid}" in rendered


def test_diagnostics_exclude_cards_whose_record_carries_a_cause(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        # A breaker park writes its cause into `gave_up`: not a sealed block.
        parked = kb.create_task(conn, title="breaker parked", assignee="worker",
                                max_runtime_seconds=1)
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
        _time_out_once(conn, parked)
        _time_out_once(conn, parked)
        assert kb.get_task(conn, parked).status == "blocked"
        assert "blocked_without_reason" not in _diag_kinds(conn, parked)

        # A typed block with a reason on the blocked event: not this class either.
        typed = kb.create_task(conn, title="typed", assignee="worker")
        assert kb.block_task(conn, typed, reason=REASON, kind="needs_input") is True
        assert "blocked_without_reason" not in _diag_kinds(conn, typed)

        # A card the loop breaker routed to triage is not blocked.
        cycling = kb.create_task(conn, title="cycling", assignee="worker")
        assert kb.block_task(conn, cycling, reason="first", kind="needs_input") is True
        assert kb.unblock_task(conn, cycling) is True
        assert kb.block_task(conn, cycling, reason="first", kind="needs_input") is True
        assert kb.get_task(conn, cycling).status == "triage"
        assert "blocked_without_reason" not in _diag_kinds(conn, cycling)


# ---------------------------------------------------------------------------
# The CLI doors: `set-block-reason`, and `block` on an already-blocked card
# ---------------------------------------------------------------------------


def test_diagnostics_list_the_class_on_a_BOARD_WIDE_call(kanban_home):
    """The rule's whole point is the board-wide list, and `hermes kanban diagnostics`
    names EVERY card id: one `IN (?,?…)` past SQLite's variable cap raised "too many SQL
    variables" and made the surface (and this rule) unusable on a real board. Both bulk
    id loaders must chunk — exercised here with more ids than SQLite binds."""
    with kbc.connect_closing() as conn:
        sealed = _sealed_block(conn)
        other = kb.create_task(conn, title="unrelated", assignee="worker")
        ids = [f"t_{i:08x}" for i in range(1200)] + [sealed, other]

        rows = kc._rows_by_task(conn, "task_events", ids)
        assert rows[sealed] and [r["id"] for r in rows[sealed]] == sorted(
            r["id"] for r in rows[sealed])
        graph = kb.task_graph_contexts(conn, ids)
        assert graph[sealed] == {"parents": [], "children": []}
        assert "blocked_without_reason" in _diag_kinds(conn, sealed)


def test_cli_set_block_reason_records_in_place(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _sealed_block(conn)
        blocked_at = _blocked_at(conn, tid)

    out = kc.run_slash(f'set-block-reason {tid} "{REASON}" --kind needs_input')

    assert "Recorded block reason" in out, out
    assert "still blocked" in out.lower() or "blocked" in out, out
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind == "needs_input"
        assert kb.block_record(conn, tid) == (REASON, "needs_input")
        assert _blocked_at(conn, tid) == blocked_at
        # the comment trail says what happened, not that the card was blocked again
        comments = kb.list_comments(conn, tid)
        assert any(c.body.startswith("BLOCK REASON SET") for c in comments)


def test_cli_block_on_an_already_blocked_card_repairs_the_missing_reason(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _sealed_block(conn)
        blocked_at = _blocked_at(conn, tid)

    out = kc.run_slash(f'block {tid} "{REASON}" --kind needs_input')

    assert "Recorded block reason" in out, out
    assert "unblock loop detected" not in out, out
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind == "needs_input"
        assert kb.block_record(conn, tid) == (REASON, "needs_input")
        assert _blocked_at(conn, tid) == blocked_at
        assert [e.kind for e in _events(conn, tid)].count("unblocked") == 0


def test_cli_set_block_reason_on_a_card_that_is_not_blocked_fails_loudly(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="live card", assignee="worker")

    out = kc.run_slash(f'set-block-reason {tid} "{REASON}"')

    assert "cannot set a block reason" in out, out
    assert "not blocked" in out, out
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "ready"


def test_cli_set_block_reason_requires_a_reason(kanban_home):
    assert "needs the reason text" in kc.run_slash("set-block-reason t_deadbeef")


def test_cli_diagnostics_lists_the_sealed_block(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _sealed_block(conn, title="sealed for diagnostics")

    out = kc.run_slash("diagnostics")

    assert tid in out
    assert "Blocked with no reason recorded" in out
