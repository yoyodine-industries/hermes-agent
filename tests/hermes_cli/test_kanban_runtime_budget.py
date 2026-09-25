"""Card runtime budget: default wall-clock cap, cap provenance, lane turn ceiling, CAP notices.

Implements the runtime half of ops task t_e7d0ee8f (design decisions D1-D11): a
card no longer runs unbounded by default, the dispatcher can tell a human's cap
from the fleet default, a killed attempt is never silent (CAP comment +
checkpoint verdict), and a worker is handed its own budget via ``--run-budget``
/ ``--max-turns`` / ``HERMES_KANBAN_MAX_*`` instead of inferring it.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated board + HERMES_HOME so the REAL config resolution runs."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kb.init_db()
    return home


def _spawn_task(kb_mod, *, assignee: str, **overrides):
    """A constructed Task (the input contract of _worker_argv/_default_spawn)."""
    fields = dict(
        id="t_spawn_budget",
        title="spawn budget",
        body=None,
        assignee=assignee,
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=7,
    )
    fields.update(overrides)
    return kb_mod.Task(**fields)


def _backdate_attempt(conn, tid: str, seconds: int) -> None:
    started = int(time.time()) - seconds
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET started_at = ? WHERE id = ?", (started, tid))
        conn.execute(
            "UPDATE task_runs SET started_at = ? "
            "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
            (started, tid),
        )


def _time_out_once(conn, tid: str, *, backdate_seconds: int = 30) -> None:
    """Claim, spawn, backdate the attempt past its cap, then run enforcement."""
    kb.claim_task(conn, tid)
    kbd._set_worker_pid(conn, tid, os.getpid())
    _backdate_attempt(conn, tid, backdate_seconds)
    assert tid in kbd.enforce_max_runtime(conn, signal_fn=lambda pid, sig: None)


def _cap_comments(conn, tid: str) -> list[str]:
    bodies = []
    for comment in kb.list_comments(conn, tid):
        body = comment.body or ""
        if body.startswith("CAP:"):
            bodies.append(body)
    return bodies


def _timed_out_payload(conn, tid: str) -> dict:
    events = [e for e in kb.list_events(conn, tid) if e.kind == "timed_out"]
    assert events, "no timed_out event recorded"
    payload = events[-1].payload
    assert isinstance(payload, dict)
    return payload


def _latest_run_id(conn, tid: str) -> int:
    run_id = conn.execute(
        "SELECT MAX(id) FROM task_runs WHERE task_id = ?", (tid,),
    ).fetchone()[0]
    assert run_id is not None
    return int(run_id)


# --- D1/D2: config shape + resolution ---------------------------------------

def test_config_defaults_ship_the_budget_keys(kanban_home):
    """The shipped defaults are live through the real config path, and the
    config table and the code constant cannot drift apart."""
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert kbd.DEFAULT_MAX_RUNTIME_SECONDS == 1200
    assert kbd.configured_default_max_runtime_seconds() == 1200
    assert DEFAULT_CONFIG["kanban"]["default_max_runtime_seconds"] == 1200
    assert DEFAULT_CONFIG["kanban"]["default_max_turns"] == kbd.DEFAULT_MAX_TURNS
    table = kbd.configured_default_max_turns()
    assert table == kbd.DEFAULT_MAX_TURNS
    assert (table["default"], table["worker"], table["coder"], table["stl"]) == (60, 60, 120, 120)


def test_turn_ceiling_resolution_prefers_exact_id_then_suffix(kanban_home):
    """Lane -> turns: exact profile id beats the role suffix, then ``default``."""
    assert kbd.resolve_default_max_turns("yoyodine-platform-worker") == 60
    assert kbd.resolve_default_max_turns("yoyodine-platform-coder") == 120
    assert kbd.resolve_default_max_turns("yoyodine-platform-stl") == 120
    assert kbd.resolve_default_max_turns("yoyodine-majordomo") == 120   # exact-id override
    assert kbd.resolve_default_max_turns("unknown-lane") == 60          # 'default'
    assert kbd.resolve_default_max_turns("yoyodine-platform-worker", 7) == 7  # per-card wins


def test_turn_ceiling_none_when_nothing_resolves(kanban_home, monkeypatch):
    monkeypatch.setattr(kbd, "configured_default_max_turns", lambda: None)
    monkeypatch.setattr(kbd, "DEFAULT_MAX_TURNS", {})
    assert kbd.resolve_default_max_turns("yoyodine-platform-coder") is None


# --- D3/D4: create-time materialization + provenance ------------------------

def test_create_task_stamps_resolved_default_cap(kanban_home):
    with kbc.connect() as conn:
        auto = kb.get_task(conn, kb.create_task(conn, title="auto", assignee="platform-coder"))
        explicit = kb.get_task(conn, kb.create_task(
            conn, title="explicit", assignee="platform-coder", max_runtime_seconds=42))
    assert (auto.max_runtime_seconds, auto.max_runtime_source) == (1200, "default")
    assert (explicit.max_runtime_seconds, explicit.max_runtime_source) == (42, "explicit")


def test_config_absent_leaves_new_cards_unbounded(kanban_home):
    """Grandfather (D5): an explicit ``null`` default leaves new cards unbounded.

    Driven through the real config file rather than a monkeypatch so it proves the
    shipped merge path honours the opt-out.
    """
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  default_max_runtime_seconds: null\n", encoding="utf-8",
    )
    assert kbd.configured_default_max_runtime_seconds() is None
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="unbounded", assignee="platform-coder")
        task = kb.get_task(conn, tid)
        assert (task.max_runtime_seconds, task.max_runtime_source) == (None, None)
        assert kbd.resolve_default_max_runtime_seconds(None, "default") is None
        kb.claim_task(conn, tid)
        kbd._set_worker_pid(conn, tid, os.getpid())
        _backdate_attempt(conn, tid, 10_000)
        assert kbd.enforce_max_runtime(conn, signal_fn=lambda pid, sig: None) == []
        assert kb.get_task(conn, tid).status == "running"


def test_legacy_null_row_reads_as_default_sourced(kanban_home):
    """A row predating the column: value decides provenance, NULL = fleet default."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="legacy", assignee="platform-coder")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET max_runtime_seconds = NULL, max_runtime_source = NULL "
                "WHERE id = ?", (tid,),
            )
        legacy = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert kb.task_max_runtime_source(legacy) == "default"
        # A row-level Row and a Task must agree on the rule.
        assert kb.task_max_runtime_source(kb.get_task(conn, tid)) == "default"
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET max_runtime_seconds = 900 WHERE id = ?", (tid,),
            )
        assert kb.task_max_runtime_source(kb.get_task(conn, tid)) == "explicit"


# --- D5: terminal timeout is only lifted for an explicit cap ----------------

def test_terminal_timeout_lifted_only_for_explicit_caps():
    assert kbd.KANBAN_TERMINAL_TIMEOUT_GRACE_SECONDS == 30
    assert kbd._worker_terminal_timeout_env(1200, "180", source="explicit") == "1170"
    assert kbd._worker_terminal_timeout_env(1200, "180", source="default") is None
    assert kbd._worker_terminal_timeout_env(1200, "180") is None
    assert kbd._worker_terminal_timeout_env(None, "180", source="explicit") is None
    # Already generous enough: no override either way.
    assert kbd._worker_terminal_timeout_env(1200, "5000", source="explicit") is None


# --- D6/D10: enforcement, CAP notice, breaker disposition -------------------

def test_enforce_bounds_a_legacy_null_cap_row(kanban_home, monkeypatch):
    """A NULL-cap card is capped by the fleet default and its kill is not silent."""
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="legacy overrun", assignee="platform-coder")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET max_runtime_seconds = NULL, max_runtime_source = NULL "
                "WHERE id = ?", (tid,),
            )
        _time_out_once(conn, tid, backdate_seconds=2000)

        task = kb.get_task(conn, tid)
        assert task.status == "ready", "a capped time-out returns to the retry phase"

        payload = _timed_out_payload(conn, tid)
        assert payload["limit_seconds"] == 1200
        assert payload["max_runtime_source"] == "default"
        assert payload["checkpoint_present"] is False
        assert 2000 <= payload["elapsed_seconds"] <= 2100

        run_id = _latest_run_id(conn, tid)
        elapsed = payload["elapsed_seconds"]
        body = _cap_comments(conn, tid)[-1]
        lines = body.splitlines()
        assert lines[0] == f"CAP: run {run_id} exhausted at {elapsed}s (limit 1200s, source=default)."
        assert lines[1] == f"CAP kind=wall source=default limit=1200s elapsed={elapsed}s run={run_id}"
        assert lines[2] == "checkpoint: absent"
        assert lines[3] == "NEXT: resume from the newest CHECKPOINT comment; do not restart."
        assert task.last_failure_error == f"cap=1200s source=default elapsed={elapsed}s"


def test_cap_notice_reports_the_checkpoint_it_found(kanban_home, monkeypatch):
    """The notice names the newest CHECKPOINT comment, and only a FIRST-LINE
    marker counts (deterministic string test, no inference)."""
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="resumable", assignee="platform-coder")
        # A comment that merely mentions the word is NOT a checkpoint.
        kb.add_comment(conn, tid, "platform-coder", "nothing durable here\nCHECKPOINT later")
        _time_out_once(conn, tid, backdate_seconds=2000)
        assert "checkpoint: absent" in _cap_comments(conn, tid)[-1]
        assert _timed_out_payload(conn, tid)["checkpoint_present"] is False

        checkpoint_id = kb.add_comment(
            conn, tid, "platform-coder", "CHECKPOINT\nbranch landed; next: run the unit tests",
        )
        _time_out_once(conn, tid, backdate_seconds=2000)
        body = _cap_comments(conn, tid)[-1]
        assert f"checkpoint: present (comment {checkpoint_id})" in body
        assert _timed_out_payload(conn, tid)["checkpoint_present"] is True


def test_default_cap_exhaustion_requeues_then_parks_typed(kanban_home, monkeypatch):
    """One exhaustion re-spawns immediately (no delayed retry schedule); the
    second parks the card with a deterministic kind and a stated fix."""
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="mis-scoped", assignee="platform-coder")

        _time_out_once(conn, tid, backdate_seconds=2000)
        first = kb.get_task(conn, tid)
        assert (first.status, first.block_kind) == ("ready", None)
        assert first.last_failure_error.startswith("cap=1200s source=default elapsed=")
        assert "mis-scoped" not in first.last_failure_error

        _time_out_once(conn, tid, backdate_seconds=2000)
        parked = kb.get_task(conn, tid)
        assert (parked.status, parked.block_kind) == ("blocked", "needs_input")
        assert "cap=1200s source=default elapsed=" in parked.last_failure_error
        assert "cap exhausted 2x" in parked.last_failure_error
        assert "mis-scoped" in parked.last_failure_error
        assert len(_cap_comments(conn, tid)) == 2
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert (kinds.count("timed_out"), kinds.count("gave_up")) == (2, 1)


def test_explicit_cap_trip_keeps_the_park_untyped(kanban_home, monkeypatch):
    """A human-set cap keeps the pre-change untyped park (the mis-scoped wording
    would be wrong for a card that already has an explicit cap)."""
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="deliberate", assignee="platform-coder",
                             max_runtime_seconds=1)
        _time_out_once(conn, tid)
        _time_out_once(conn, tid)
        parked = kb.get_task(conn, tid)
        assert (parked.status, parked.block_kind) == ("blocked", None)
        assert parked.last_failure_error.startswith("cap=1s source=explicit elapsed=")
        assert "mis-scoped" not in parked.last_failure_error


# --- D7/D8/D11: what the worker is handed -----------------------------------

def test_worker_argv_carries_the_lane_budget(kanban_home, monkeypatch, tmp_path):
    coder = _spawn_task(kb, assignee="yoyodine-platform-coder")
    worker = _spawn_task(kb, assignee="yoyodine-platform-worker")
    majordomo = _spawn_task(kb, assignee="yoyodine-majordomo")

    def _flag(cmd, name):
        assert name in cmd, f"{name} missing from {cmd}"
        return cmd[cmd.index(name) + 1]

    coder_cmd = kbd._worker_argv(coder, "yoyodine-platform-coder", None)
    worker_cmd = kbd._worker_argv(worker, "yoyodine-platform-worker", None)
    majordomo_cmd = kbd._worker_argv(majordomo, "yoyodine-majordomo", None)

    assert _flag(coder_cmd, "--max-turns") == "120"
    assert _flag(coder_cmd, "--run-budget") == str(kbd.DEFAULT_MAX_RUNTIME_SECONDS)
    assert _flag(worker_cmd, "--max-turns") == "60"
    assert _flag(majordomo_cmd, "--max-turns") == "120"
    # Both flags live on the `chat` subcommand, so they must ride after it.
    assert coder_cmd.index("--max-turns") > coder_cmd.index("chat")


def test_worker_argv_parses_through_the_real_cli(kanban_home, monkeypatch, tmp_path):
    """Integration contract: the budget flags must reach argparse' destinations."""
    from hermes_cli._parser import build_top_level_parser

    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    captured = {}

    class FakeProc:
        pid = 4246

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    (kanban_home / "profiles" / "elias").mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = _spawn_task(kb, assignee="elias", max_runtime_seconds=900,
                       max_runtime_source="explicit")
    kbd._default_spawn(task, str(workspace))

    parser, _subparsers, _chat_parser = build_top_level_parser()
    args = parser.parse_args(captured["cmd"][3:])
    assert args.max_turns == 60          # elias -> unknown lane -> 'default'
    assert args.run_budget == 900.0      # explicit per-card cap wins


def test_worker_env_states_the_budget_and_keeps_terminal_timeout_default(
    kanban_home, monkeypatch, tmp_path,
):
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    captured = {}

    class FakeProc:
        pid = 4247

    def fake_popen(cmd, *args, **kwargs):
        captured["env"] = dict(kwargs.get("env") or {})
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    (kanban_home / "profiles" / "elias").mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Fleet-default cap: the worker is told the budget but the terminal timeout
    # is NOT lifted (a hung command must not be able to eat the whole cap).
    kbd._default_spawn(_spawn_task(
        kb, assignee="elias", max_runtime_seconds=1200, max_runtime_source="default",
    ), str(workspace))
    env = captured["env"]
    assert env["HERMES_KANBAN_MAX_RUNTIME_SECONDS"] == "1200"
    assert env["HERMES_KANBAN_MAX_TURNS"] == "60"
    assert env.get("TERMINAL_TIMEOUT") == os.environ.get("TERMINAL_TIMEOUT")

    # Explicit cap: the terminal default is lifted to cap - grace.
    kbd._default_spawn(_spawn_task(
        kb, assignee="elias", max_runtime_seconds=900, max_runtime_source="explicit",
    ), str(workspace))
    assert captured["env"]["HERMES_KANBAN_MAX_RUNTIME_SECONDS"] == "900"
    assert captured["env"]["TERMINAL_TIMEOUT"] == "870"


def test_goal_mode_null_ceiling_gets_the_lane_ceiling(kanban_home, monkeypatch, tmp_path):
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    captured = {}

    class FakeProc:
        pid = 4248

    def fake_popen(cmd, *args, **kwargs):
        captured["env"] = dict(kwargs.get("env") or {})
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    (kanban_home / "profiles" / "elias").mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="goal", assignee="yoyodine-platform-coder",
                             goal_mode=True)
        task = kb.get_task(conn, tid)
    assert task.goal_max_turns is None

    kbd._default_spawn(task, str(workspace))
    env = captured["env"]
    assert env["HERMES_KANBAN_GOAL_MODE"] == "1"
    assert env["HERMES_KANBAN_GOAL_MAX_TURNS"] == "120"
    assert env["HERMES_KANBAN_MAX_TURNS"] == "120"


def test_worker_context_states_the_budget_and_checkpoint_duty(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ctx", assignee="yoyodine-platform-coder")
        ctx = kb.build_worker_context(conn, tid)

    assert "Max runtime: 1200s (source: default)" in ctx
    assert "## Budget" in ctx
    assert "Wall-clock cap: 1200s (source: default)" in ctx
    assert "Turn ceiling: 120" in ctx
    assert "CHECKPOINT" in ctx


# --- D9: the wrap-up notice carries the checkpoint duty for kanban workers ---

def test_wrapup_notice_is_gated_on_being_a_kanban_worker(monkeypatch):
    from agent.conversation_loop import RUN_BUDGET_WRAPUP_NOTICE, run_budget_wrapup_notice

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert run_budget_wrapup_notice() == RUN_BUDGET_WRAPUP_NOTICE

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_abc")
    notice = run_budget_wrapup_notice()
    assert notice.startswith(RUN_BUDGET_WRAPUP_NOTICE)
    assert "CHECKPOINT" in notice
    assert "kanban_comment" in notice
