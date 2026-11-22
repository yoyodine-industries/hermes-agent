"""The delivery budget belongs in the CLAIM — the verb that increments it — not only in release().

A cap inside ``release_completion_delivery`` bounds the retries a LIVE consumer performs. A consumer
that dies holding the claim never releases, so nothing post-hoc can bound it: the only verb that can
is the one that increments ``delivery_attempts``. These tests exercise that claim predicate against a
real temp ``state.db`` (the module's own writer path), and then the census that makes a value the
guard forbids impossible to mislabel as "cause unknown".
"""

import logging
import sqlite3
import time

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools import async_delegation as ad

CAP = ad._MAX_DELIVERY_ATTEMPTS


@pytest.fixture
def home(tmp_path):
    """A profile home of its own, bound for code running outside a turn (as a delivery loop does)."""
    path = tmp_path / "home"
    path.mkdir()
    ad._reset_for_tests()
    token = set_hermes_home_override(str(path))
    try:
        yield path
    finally:
        ad._reset_for_tests()
        reset_hermes_home_override(token)


def _row(home_path, delegation_id):
    conn = sqlite3.connect(home_path / "state.db")
    try:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("SELECT * FROM async_delegations WHERE delegation_id=?",
                                 (delegation_id,)).fetchone())
    finally:
        conn.close()


def _set(home_path, delegation_id, **columns):
    conn = sqlite3.connect(home_path / "state.db")
    try:
        conn.execute(f"UPDATE async_delegations SET {', '.join(f'{k}=?' for k in columns)} "
                     "WHERE delegation_id=?", (*columns.values(), delegation_id))
        conn.commit()
    finally:
        conn.close()


def _pending(home_path, delegation_id="deleg-1", *, attempts=0, delivery_state="pending",
             completed_ago_h=1.0, **extra):
    """A terminal, still-undelivered completion row, then pinned to the shape under test."""
    now = time.time()
    ad._persist_dispatch({"delegation_id": delegation_id, "session_key": "cli",
                          "dispatched_at": now - 120})
    ad._persist_completion({"type": "async_delegation", "delegation_id": delegation_id,
                            "status": "completed", "completed_at": now - completed_ago_h * 3600},
                           {"status": "completed"})
    _set(home_path, delegation_id, delivery_attempts=attempts, delivery_state=delivery_state, **extra)
    return delegation_id


def test_claim_at_the_budget_is_refused_and_converges_the_row_to_dropped(home, caplog):
    _pending(home, "deleg-at-cap", attempts=CAP)
    with caplog.at_level(logging.WARNING, logger="tools.async_delegation"):
        assert ad.claim_completion_delivery("deleg-at-cap", "c1") is False
    row = _row(home, "deleg-at-cap")
    assert row["delivery_state"] == "dropped"       # converged in the claim's own transaction
    assert row["delivery_attempts"] == CAP          # refused, never incremented past the budget
    assert row["delivery_claim"] is None and row["delivered_at"] is None
    assert "deleg-at-cap" in caplog.text and str(CAP) in caplog.text


def test_claim_below_the_budget_still_succeeds(home):
    _pending(home, "deleg-under-cap", attempts=CAP - 1)
    assert ad.claim_completion_delivery("deleg-under-cap", "c1") is True
    row = _row(home, "deleg-under-cap")
    assert row["delivery_state"] == "pending"
    assert row["delivery_attempts"] == CAP
    assert row["delivery_claim"] == "c1"


def test_a_dead_claimant_loop_can_never_carry_the_counter_past_the_budget(home):
    """Claim without ever releasing, over >= cap*3 iterations: the loop the release cap cannot see."""
    _pending(home, "deleg-dead-loop")
    observed = []
    for i in range(CAP * 3):
        claimed = ad.claim_completion_delivery("deleg-dead-loop", f"dead-claimant-{i}")
        row = _row(home, "deleg-dead-loop")
        observed.append(row["delivery_attempts"])
        assert row["delivery_attempts"] <= CAP, f"attempt {i} carried the counter past the cap: {observed}"
        if claimed:
            assert row["delivery_state"] == "pending"
            # The claimant dies holding the claim and nothing releases it; only the lease lapses.
            _set(home, "deleg-dead-loop", delivery_claimed_at=time.time() - ad._CLAIM_LEASE_S - 1)
        else:
            assert row["delivery_state"] == "dropped"
    assert len(observed) >= CAP * 3
    assert max(observed) == CAP
    final = _row(home, "deleg-dead-loop")
    assert (final["delivery_state"], final["delivery_attempts"]) == ("dropped", CAP)


def test_an_already_over_cap_row_is_refused_without_incrementing_further(home):
    """The incident row's shape: 54 attempts. The claim must not add a 55th."""
    _pending(home, "deleg-over", attempts=CAP + 46)
    assert ad.claim_completion_delivery("deleg-over", "c1") is False
    row = _row(home, "deleg-over")
    assert row["delivery_attempts"] == CAP + 46
    assert row["delivery_state"] == "dropped"


def test_census_names_all_four_drop_causes_never_one_number(home):
    _pending(home, "b", attempts=CAP, delivery_state="dropped")              # the cap fired
    _pending(home, "r", attempts=0, delivery_state="dropped",
             completed_ago_h=49.0)                                           # never claimed, past the replay age
    _pending(home, "o", attempts=2, delivery_state="dropped")                # claimed under budget, target gone
    _pending(home, "u", attempts=0, delivery_state="dropped",
             completed_ago_h=1.0)                                            # no accounting evidence either way
    report = ad.delivery_drop_census()
    assert report["ok"] is True
    assert report["dropped_total"] == 4
    assert report["counts"] == {"dropped_at_boundary": 1, "dropped_owner_gone": 1,
                                "dropped_replay_age": 1, "dropped_unknown_cause": 1}
    text = ad.format_delivery_drop_census(report)
    for cause in ad._DROP_CAUSES:
        assert cause in text
    assert str(home / "state.db") in text and f"cap {CAP}" in text


def test_census_reports_an_over_cap_row_as_impossible_not_unknown(home):
    _pending(home, "deleg_b1d6ed9b", attempts=54, state="unknown", completed_ago_h=5.31 * 24)
    report = ad.delivery_drop_census()
    assert report["ok"] is False
    assert report["over_cap"]["count"] == 1
    assert report["over_cap"]["max"] == 54
    assert report["over_cap"]["rows"][0]["delegation_id"] == "deleg_b1d6ed9b"
    # The impossible value is NOT laundered into a cause: no class absorbs it.
    assert sum(report["counts"].values()) == 0
    assert report["counts"]["dropped_unknown_cause"] == 0
    text = ad.format_delivery_drop_census(report)
    assert ad._OVER_CAP_NOTE in text
    assert "deleg_b1d6ed9b" in text


def test_assert_check_fails_loudly_over_the_cap_and_passes_below_it(home, caplog, capsys):
    _pending(home, "deleg-over", attempts=CAP + 1)
    with caplog.at_level(logging.ERROR, logger="tools.async_delegation"):
        report = ad.assert_delivery_attempts_invariant()
    assert report["ok"] is False
    assert "invariant FAILED" in caplog.text
    assert ad._main(["--assert"]) == 1
    assert "OVER CAP" in capsys.readouterr().out

    _set(home, "deleg-over", delivery_attempts=CAP, delivery_state="dropped")
    assert ad._main(["--assert"]) == 0
    assert ad._main(["--census", "--json"]) == 0
    assert '"dropped_at_boundary": 1' in capsys.readouterr().out


def test_census_is_bound_to_the_profile_ledger_it_runs_under(home, tmp_path):
    _pending(home, "deleg-here", attempts=CAP)
    other = tmp_path / "other-home"
    other.mkdir()
    token = set_hermes_home_override(str(other))
    try:
        report = ad.delivery_drop_census()
    finally:
        reset_hermes_home_override(token)   # back to THIS profile's home; never leave an override armed
    assert report["dropped_total"] == 0 and report["ok"] is True
    assert str(other / "state.db") in report["store"]


def test_the_worker_ledger_claim_enforces_the_same_budget(tmp_path):
    """The worker-process claim is a second implementation of the same verb: same predicate, same drop."""
    from gateway.session_worker_delegation import _claim
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        ad._initialize_schema(conn)
        now = time.time()
        conn.execute("INSERT INTO async_delegations (delegation_id, origin_session, parent_session_id, state, "
                     "dispatched_at, updated_at, delivery_state, delivery_attempts, owner_execution_id) "
                     "VALUES ('deleg-worker','cli','parent','completed',?,?,'pending',?,'exec-1')",
                     (now - 60, now, CAP))
        assert _claim(None, conn, "parent", {"delegation_id": "deleg-worker", "claim_id": "c1"},
                      execution_id="exec-1") == {"value": False}
        row = conn.execute("SELECT delivery_state, delivery_attempts FROM async_delegations "
                           "WHERE delegation_id='deleg-worker'").fetchone()
        assert (row["delivery_state"], row["delivery_attempts"]) == ("dropped", CAP)
        # One attempt below the budget is still claimable there too.
        conn.execute("UPDATE async_delegations SET delivery_state='pending', delivery_attempts=? "
                     "WHERE delegation_id='deleg-worker'", (CAP - 1,))
        assert _claim(None, conn, "parent", {"delegation_id": "deleg-worker", "claim_id": "c2"},
                      execution_id="exec-1") == {"value": True}
    finally:
        conn.close()
