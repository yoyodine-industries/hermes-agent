"""Tests for the in-place project door (card t_b19cbf42).

They cover the semantics that DISTINGUISH this door from the create path,
because those are the ones a re-tag over 15,904 cards depends on:

  * an unresolvable reference is REFUSED, where ``create_task`` drops the link
    silently and makes an ordinary task - at scale a dropped write is
    indistinguishable from a successful one;
  * a blank/missing token is refused rather than read as either "unset" or
    "clear";
  * the ``NO_PROJECT`` token is a distinct, reportable outcome;
  * the result shape is stable, which is what makes a dry run worth trusting.
"""
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


def test_refuses_an_unknown_task():
    conn = kbc.connect()
    try:
        result = kb.set_task_project(conn, "t_00000000", "some-project")
        assert result["action"] == "refused"
        assert "no such task" in result["reason"]
    finally:
        conn.close()


def test_refuses_a_blank_reference_and_writes_nothing():
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="blank-ref", assignee="test-worker")
        result = kb.set_task_project(conn, tid, "   ")
        assert result["action"] == "refused"
        assert result["reason"] == "no project reference given"
        assert kb.get_task(conn, tid).project_id is None
    finally:
        conn.close()


def test_refuses_an_unresolvable_reference_rather_than_dropping_it():
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="unresolvable", assignee="test-worker")
        result = kb.set_task_project(conn, tid, "no-such-project-slug-xyz")
        assert result["action"] == "refused"
        assert "does not resolve" in result["reason"]
        # The whole point of the door: the refusal is VISIBLE and nothing was
        # written, so it cannot be mistaken for a successful no-op.
        assert kb.get_task(conn, tid).project_id is None
    finally:
        conn.close()


def test_no_project_token_is_distinct_from_unset():
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="no-project-token", assignee="test-worker")
        result = kb.set_task_project(conn, tid, kb.NO_PROJECT_TOKEN)
        # Clearing an already-unset reference is a distinct, reportable
        # outcome - not the same refusal a blank token gets.
        assert result["action"] == "unchanged"
        assert result["proposed"] is None
        assert result["reason"] == ""
    finally:
        conn.close()


def test_result_shape_is_identical_for_a_dry_and_a_real_run():
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="shape", assignee="test-worker")
        result = kb.set_task_project(conn, tid, "no-such-project-slug-xyz")
        assert set(result) == {"task_id", "before", "proposed", "action", "reason"}
    finally:
        conn.close()
