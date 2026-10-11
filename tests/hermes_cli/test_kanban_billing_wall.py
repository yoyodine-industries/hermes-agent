"""Provider BILLING walls (HTTP 402) must not be charged to the card.

The dispatcher reads a worker's exit status to decide whether the CARD failed or
the PROVIDER refused it. A credit wall that exits 0 (or is requeued like a
transient throttle) burns the card's retry budget on an empty provider account
and then blocks it with a failure reason that names the wrong problem; a credit
wall that is never recorded at all is invisible to the operator who has to top
the account up.
"""

from __future__ import annotations

import os

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    kb.init_db()
    return home


def _exited_status(code: int) -> int:
    """Raw wait-status for a WIFEXITED child with the given exit code."""
    return code << 8


def _running_task(conn, tid: str, pid: int) -> None:
    """Claim ``tid`` and point its claim at this host + a dead ``pid``."""
    host = kb._claimer_id().split(":", 1)[0]
    kb.claim_task(conn, tid, claimer=f"{host}:w{pid}")
    conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (pid, tid))
    conn.commit()


def test_billing_wall_parks_the_card_without_counting_a_failure(
    kanban_home, monkeypatch,
):
    """The 402 case end to end: the refusal is booked, the card parks, the
    failure counter is untouched, and the park survives ``recompute_ready``.

    Parking is the part that has to hold: a dispatcher park that leaves
    ``consecutive_failures`` at 0 is exactly what ``recompute_ready`` re-promotes
    to ``ready`` in the same tick, which would re-spawn the card straight into a
    provider account that still has no credit.
    """
    import hermes_cli.kanban_db as _kb
    from hermes_cli import kanban_db_dispatch as _kbd

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="billing-wall", assignee="a")
        _running_task(conn, tid, 61000)
        _kbd._record_worker_exit(
            61000, _exited_status(_kb.KANBAN_BILLING_EXHAUSTED_EXIT_CODE)
        )

        crashed = kbd.detect_crashed_workers(conn)

        # Not a crash, not a throttle: its own bucket.
        assert tid not in crashed
        assert tid not in getattr(_kbd.detect_crashed_workers, "_last_rate_limited", [])
        assert tid in getattr(
            _kbd.detect_crashed_workers, "_last_billing_exhausted", [],
        )

        task = kb.get_task(conn, tid)
        assert task.status == "blocked", "a credit wall waits for a human, not a retry"
        assert task.consecutive_failures == 0, (
            "the provider refused the run — the task was never tried"
        )
        # A DOCUMENTED park: block_kind is what keeps the disposition sweep from
        # reading the card as an undocumented blocker and draining it back into
        # the queue, and what routes it to the operator.
        assert task.block_kind == "capability"
        assert "provider credits" in (task.last_failure_error or "")

        outcomes = [
            row["outcome"] for row in conn.execute(
                "SELECT outcome FROM task_runs WHERE task_id = ?", (tid,),
            ).fetchall()
        ]
        assert "billing_exhausted" in outcomes
        assert "crashed" not in outcomes and "rate_limited" not in outcomes

        # The park is sticky — only an explicit unblock exits it.
        assert _kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, tid).status == "blocked"


def test_clean_exit_without_a_terminal_call_is_still_a_protocol_violation(
    kanban_home, monkeypatch,
):
    """rc=0 with the card still ``running`` keeps its own accounting.

    The billing carve-out must not have swallowed the ordinary
    worker-forgot-to-report case: that one still books ``crashed`` with the
    protocol-violation marker, still requeues below its budget, and still counts
    toward the streak.
    """
    import hermes_cli.kanban_db as _kb
    from hermes_cli import kanban_db_dispatch as _kbd

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="forgot-to-report", assignee="a")
        _running_task(conn, tid, 62000)
        _kbd._record_worker_exit(62000, _exited_status(0))

        assert tid in kbd.detect_crashed_workers(conn)
        assert tid not in getattr(
            _kbd.detect_crashed_workers, "_last_billing_exhausted", [],
        )

        run = conn.execute(
            "SELECT outcome, metadata FROM task_runs WHERE task_id = ?", (tid,),
        ).fetchone()
        assert run["outcome"] == "crashed"
        assert _kb._json_dict(run["metadata"]).get("protocol_violation") is True
        assert kb.get_task(conn, tid).status == "ready"


class _OneShotCli:
    """The slice of the interactive CLI that ``_run_single_query_mode`` touches."""

    class _Console:
        def print(self, *_args, **_kwargs):
            pass

    def __init__(self, turn_result):
        self._turn_result = turn_result
        self._single_query_mode = False
        self.console = self._Console()

    def _claim_active_session(self, *_args, **_kwargs):
        return True

    def chat(self, *_args, **_kwargs):
        # What the real ``chat`` leaves behind for the automation exit code.
        self._last_turn_result = self._turn_result

    def _print_exit_summary(self, **_kwargs):
        pass

    def _show_security_advisories(self):
        pass


@pytest.fixture
def one_shot(monkeypatch):
    """Non-quiet one-shot path (``chat -q``, the DISPATCHER's path), stubbed at
    the module seams it calls out to."""
    import cli

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_billing")
    monkeypatch.setattr(cli, "_should_seed_interactive", lambda *a, **k: False)
    monkeypatch.setattr(cli, "_collect_query_images", lambda query, image: (query, []))
    monkeypatch.setattr(cli, "_collect_kanban_task_images", lambda images: [])
    monkeypatch.setattr(cli, "_finalize_single_query", lambda _cli: None)
    yield cli
    os.environ.pop("HERMES_SINGLE_QUERY_SESSION", None)


def test_chat_q_billing_wall_exits_with_the_billing_sentinel(one_shot):
    """The regression: a worker the provider refused on credit exited 0.

    The sentinel is the only thing that tells the dispatcher the run was refused
    rather than forgotten, so it has to leave through the exit status the
    dispatcher reaps — and it has to be distinguishable from the throttle.
    """
    wall = {"failed": True, "failure_reason": "billing"}
    with pytest.raises(SystemExit) as excinfo:
        one_shot._run_single_query_mode(
            _OneShotCli(wall), "work kanban task t_billing", None, False, False,
        )
    assert excinfo.value.code == kb.KANBAN_BILLING_EXHAUSTED_EXIT_CODE

    # The throttle keeps its own sentinel; the two walls never share a bucket.
    throttle = {"failed": True, "failure_reason": "rate_limit"}
    with pytest.raises(SystemExit) as excinfo:
        one_shot._run_single_query_mode(
            _OneShotCli(throttle), "q", None, False, False,
        )
    assert excinfo.value.code == kb.KANBAN_RATE_LIMIT_EXIT_CODE

    # Success, and a failure the board books as an ordinary crash, keep the
    # non-quiet path's unchanged behaviour: the function returns and the process
    # leaves with its default status (0) — no new exit codes for non-wall runs.
    for result in ({"failed": False}, {"failed": True}, {"failed": True, "failure_reason": "context_length"}):
        _run = one_shot._run_single_query_mode(
            _OneShotCli(result), "q", None, False, False,
        )
        assert _run is None


def test_a_human_run_never_gets_a_dispatcher_sentinel(monkeypatch):
    """Outside a kanban worker the sentinels mean nothing to the caller, so the
    one-shot paths leave the exit status alone."""
    import cli

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    wall = {"failed": True, "failure_reason": "billing"}
    assert cli._kanban_wall_exit_code(wall) is None
    assert cli._kanban_wall_exit_code({"failed": False}) is None
    assert cli._kanban_wall_exit_code(None) is None

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_billing")
    assert cli._kanban_wall_exit_code(wall) == kb.KANBAN_BILLING_EXHAUSTED_EXIT_CODE
    assert cli._kanban_wall_exit_code(
        {"failed": True, "failure_reason": "context_length"}
    ) is None
