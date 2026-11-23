"""Block episodes: recurrences count per ADMITTED episode, cause-scoped.

Design of record: platform-stl ``t_5ecaf3e0`` (D1-D4), implemented here as the
kernel leg B. The defect these tests pin: ``_route_block`` used to increment
``block_recurrences`` on KIND equality alone, and ``unblock_task`` deliberately
preserves the counter -- so a card parked on one standing condition, released by
a sweep and re-parked on the SAME unresolved condition reached
``BLOCK_RECURRENCE_LIMIT`` and was routed to ``triage`` with a
``block_loop_detected`` event, though no run was ever attempted in between
(measured 2026-11-04: 671 of 748 live detections had no ``claimed``/``spawned``
event between the prior block and the detection).

D1 -- admission is the episode boundary (``claimed``/``spawned`` since the newest
routing event; ``unblocked`` is NOT admission).
D2 -- cause, not just kind (``reason_key``: volatile-free reason text).
D3 -- count ONE episode; the payload carries ``recurrence_basis`` and the
monotone, never-escalating ``no_admission_recurrences`` diagnostic.
D4 -- a ``dependency`` park never counts and stays in ``todo``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, title: str = "t") -> str:
    """Create a task and drive it to ``running`` (this emits the first ``claimed`` event)."""
    tid = kb.create_task(conn, title=title, assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    return tid


def _make_running_again(conn, tid: str) -> None:
    """A worker actually picks the card up: the ADMITTING transition."""
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None


def _block_events(conn, tid: str, kind: str = "block_loop_detected") -> list:
    return [e for e in kb.list_events(conn, tid) if e.kind == kind]


# ---------------------------------------------------------------------------
# D1: admission is the episode boundary
# ---------------------------------------------------------------------------


def test_no_admission_repeat_stays_blocked_and_never_mints_a_loop(kanban_home: Path) -> None:
    """blocked(A,R) -> unblocked -> blocked(A,R) with NO claim: same hold, not a loop."""
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(conn, tid, reason="awaiting operator approval", kind="needs_input")
        assert kb.unblock_task(conn, tid)          # a sweep releasing the standing hold
        assert kb.block_task(conn, tid, reason="awaiting operator approval", kind="needs_input")

        parked = kb.get_task(conn, tid)
        assert (parked.status, parked.block_kind, parked.block_recurrences) == (
            "blocked", "needs_input", 1)
        assert not _block_events(conn, tid), "no admission must never mint block_loop_detected"
        payload = _block_events(conn, tid, "blocked")[-1].payload
        assert payload["recurrence_basis"] == "no_admission"
        assert payload["recurrences"] == 1


def test_admitted_repeat_still_fires_the_real_catch(kanban_home: Path) -> None:
    """blocked(A,R) -> unblocked -> claimed -> blocked(A,R): a REAL loop, triaged."""
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(conn, tid, reason="same wall", kind="capability")
        assert kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)             # the card was ADMITTED
        assert kb.block_task(conn, tid, reason="same wall", kind="capability")

        assert kb.get_task(conn, tid).status == "triage"
        loop = _block_events(conn, tid)
        assert loop, "the real catch must still fire"
        payload = loop[-1].payload
        assert payload["recurrence_basis"] == "admitted"
        assert payload["recurrences"] == kb.BLOCK_RECURRENCE_LIMIT
        assert payload["no_admission_recurrences"] == 1


def test_spawned_event_is_admission_too(kanban_home: Path) -> None:
    """The dispatcher's ``spawned`` event admits a card exactly like ``claimed``."""
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(conn, tid, reason="same wall", kind="capability")
        assert kb.unblock_task(conn, tid)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "spawned", {"pid": 4242})
        assert kb.block_task(conn, tid, reason="same wall", kind="capability")
        assert kb.get_task(conn, tid).status == "triage"


def test_admission_helper_reads_the_newest_routing_event(kanban_home: Path) -> None:
    """``_admitted_since_last_routing_event`` is False after a release, True after a claim."""
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "blocked", {"kind": "needs_input", "reason": "r"})
        assert kb._admitted_since_last_routing_event(conn, tid) is False
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "unblocked", {})
        assert kb._admitted_since_last_routing_event(conn, tid) is False, (
            "unblocked is a boundary, never admission")
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "claimed", {})
        assert kb._admitted_since_last_routing_event(conn, tid) is True


# ---------------------------------------------------------------------------
# D2: cause, not just kind
# ---------------------------------------------------------------------------


def test_same_kind_different_reason_is_a_new_cause(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(conn, tid, reason="missing API token", kind="capability")
        assert kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        assert kb.block_task(conn, tid, reason="the build host is down", kind="capability")

        parked = kb.get_task(conn, tid)
        assert (parked.status, parked.block_recurrences) == ("blocked", 1)
        payload = _block_events(conn, tid, "blocked")[-1].payload
        assert payload["recurrence_basis"] == "new_cause"


def test_volatile_reason_tokens_do_not_split_one_cause(kanban_home: Path) -> None:
    """The same hold re-worded with a fresh date / id / sha is still ONE cause."""
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        first = "holding until 2026-11-14T17:40:29Z (card 552288, sha 1a2b3c4d5e)"
        second = "holding until 2026-11-15T09:00:00Z (card 573233, sha f00dface99)"
        assert kb.reason_key(first) == kb.reason_key(second)
        assert kb.block_task(conn, tid, reason=first, kind="needs_input")
        assert kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        assert kb.block_task(conn, tid, reason=second, kind="needs_input")
        assert kb.get_task(conn, tid).status == "triage"
        assert _block_events(conn, tid)[-1].payload["recurrence_basis"] == "admitted"


def test_reason_key_strips_volatiles_and_never_widens_on_empty() -> None:
    assert kb.reason_key(None) == kb.reason_key("") == ""
    assert kb.reason_key("  Blocked   BY\tthe  SWEEP ") == "blocked by the sweep"
    # a word spelled entirely in a-f is NOT a hex digest
    assert kb.reason_key("decade") == "decade"
    # digit runs and hex runs >= 6 are volatile
    assert kb.reason_key("waiting on pid 12345") == kb.reason_key("waiting on pid 99")
    assert kb.reason_key("release 0abc123def4") == kb.reason_key("release f00dface")
    # an EMPTY reason equals only another empty one: missing text never widens
    assert kb.reason_key("") != kb.reason_key("awaiting approval")
    # the key is the first 120 chars of the collapsed text
    assert len(kb.reason_key("x" * 400)) == 120


# ---------------------------------------------------------------------------
# D3: one episode per admission; the diagnostic never escalates
# ---------------------------------------------------------------------------


def test_five_no_admission_repeats_never_triage(kanban_home: Path) -> None:
    """Unbounded churn on a standing hold is a releaser question, not a human loop."""
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        for repeat in range(1, 6):
            assert kb.block_task(conn, tid, reason="operator consent pending", kind="needs_input")
            parked = kb.get_task(conn, tid)
            assert parked.status == "blocked", f"repeat {repeat} must not leave the queue"
            assert parked.block_recurrences == 1
            payload = _block_events(conn, tid, "blocked")[-1].payload
            assert payload["recurrence_basis"] == (
                "new_cause" if repeat == 1 else "no_admission")
            # the diagnostic counts THIS park's churn segment, never escalating
            assert payload["no_admission_recurrences"] == repeat
            if repeat < 5:
                assert kb.unblock_task(conn, tid)
        assert not _block_events(conn, tid)
        assert kb.get_task(conn, tid).status == "blocked"


def test_legacy_counter_heals_on_the_next_block(kanban_home: Path) -> None:
    """A card carrying a legacy ``block_recurrences>=2`` drops to 1 without a migration."""
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(conn, tid, reason="standing hold", kind="needs_input")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET block_recurrences = 5 WHERE id = ?", (tid,))
        assert kb.unblock_task(conn, tid)
        assert kb.block_task(conn, tid, reason="standing hold", kind="needs_input")
        parked = kb.get_task(conn, tid)
        assert (parked.status, parked.block_recurrences) == ("blocked", 1)
        assert _block_events(conn, tid, "blocked")[-1].payload["recurrence_basis"] == "no_admission"


# ---------------------------------------------------------------------------
# D4: a dependency park is never a loop
# ---------------------------------------------------------------------------


def test_dependency_block_never_counts(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="open-parent", assignee="worker")
        child = kb.create_task(conn, title="waiter", assignee="worker")
        kb.link_tasks(conn, parent_id=parent, child_id=child)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (child,))
        for _ in range(5):
            assert kb.block_task(conn, child, reason="waiting on parent", kind="dependency")
            parked = kb.get_task(conn, child)
            assert (parked.status, parked.block_kind, parked.block_recurrences) == (
                "todo", "dependency", 0)
            assert not _block_events(conn, child)
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='running' WHERE id=?", (child,))
