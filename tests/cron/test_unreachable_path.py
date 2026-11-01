"""The ``unreachable_path`` failure class ACTS: one card, routed, closed on recovery.

Card t_fb94d105. The defect these tests pin: a cron job that invokes a path that is no longer
there fails every tick, the incident ledger records it, and NOTHING reaches the lane that owns
the fix - and when several scripts of one unit moved, each failure risked its own card (the
measured estate symptom: dozens of cards for one broken unit).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cron import incidents
import cron.scheduler as sched
import cron.unreachable_path as up


# The shape a `no_agent` job's failure text actually takes: the wrapper's "Script exited ..."
# line, then the shell's own message naming the path it could not exec.
MISSING_PROD_SCRIPT = (
    "Script exited with code 127 | stderr: bash: line 50: exec "
    "/opt/hermes_prod/yaan-platform/cron/tip-guard.sh: No such file or directory"
)


def _point_db(monkeypatch, tmp_path):
    """Point the incident store at a throwaway executions.db (the scheduler's own shape)."""
    monkeypatch.setattr(incidents, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    return incidents


def _job(job_id="job-tip-guard", name="tip guard"):
    return {"id": job_id, "name": name}


class RecordingSink:
    """The stub sink: records what the act ASKED THE BOARD TO DO, in order."""

    def __init__(self, card_id="t_card00001"):
        self.card_id = card_id
        self.filed = []
        self.closed = []
        self.fail_with = None

    def file(self, *, board, idempotency_key, title, body, assignee):
        if self.fail_with is not None:
            raise self.fail_with
        self.filed.append(
            {
                "board": board,
                "idempotency_key": idempotency_key,
                "title": title,
                "body": body,
                "assignee": assignee,
            }
        )
        return self.card_id

    def close(self, card_id, *, board, comment, result):
        self.closed.append(
            {"card_id": card_id, "board": board, "comment": comment, "result": result}
        )
        return True


def _drive_scheduler_with(sink, monkeypatch):
    """Let the scheduler's REAL failure and recovery paths run, with the board swapped for ``sink``."""
    real_act = up.act_on_failure
    real_close = up.close_cards_for_recovered_job

    def _act(job_id, error, **kwargs):
        return real_act(job_id, error, sink=sink, **kwargs)

    def _close(job_id, **kwargs):
        return real_close(job_id, sink=sink, **kwargs)

    monkeypatch.setattr(up, "act_on_failure", _act)
    monkeypatch.setattr(up, "close_cards_for_recovered_job", _close)


# ── Classification ───────────────────────────────────────────────────────────────────────────


def test_the_missing_path_class_is_judged_before_the_keyword_table():
    # It ALSO contains "script", so the table would classify it away and the act would never fire.
    assert incidents._classify_failure_type(MISSING_PROD_SCRIPT) == incidents.UNREACHABLE_PATH
    # ...and a real script failure keeps the class it had.
    assert incidents._classify_failure_type("Script exited with code 1 | stderr: boom") == "script"


# The two stderr shapes MEASURED on the live line before this change (card t_fb94d105): the
# wrapper's own line, a `| stderr: |` double pipe, then the shell's message.
MEASURED_TIP_GUARD = (
    "Script exited with code 127 | stderr: | bash: "
    "/opt/hermes_prod/yaan-platform/cron/tip-guard.sh: No such file or directory"
)
MEASURED_VENV_EXEC = (
    "Script exited with code 127 | stderr: | bash: line 50: exec "
    "/opt/hermes_prod/shared-venv/bin/python3: No such file or directory"
)


@pytest.mark.parametrize(
    "error,path",
    [
        (MEASURED_TIP_GUARD, "/opt/hermes_prod/yaan-platform/cron/tip-guard.sh"),
        # The `exec <venv python>` shape carries a `bash: line 50: ` prefix the literal regex
        # did not cover; the class must not depend on the prefix being absent.
        (MEASURED_VENV_EXEC, "/opt/hermes_prod/shared-venv/bin/python3"),
    ],
)
def test_the_exact_measured_stderr_shapes_are_this_class(error, path):
    assert incidents._classify_failure_type(error) == incidents.UNREACHABLE_PATH
    assert incidents.captured_path(error) == path


def test_a_genuine_script_bug_and_a_genuine_config_error_keep_their_own_class():
    assert incidents._classify_failure_type(
        "Script exited with code 1 | stderr: Traceback (most recent call last): KeyError: 'x'"
    ) == "script"
    assert incidents._classify_failure_type(
        "Missing credential: OPENROUTER_API_KEY not configured"
    ) == "config"


def test_the_127_exit_alone_is_the_same_class():
    assert incidents.is_unreachable_path("exited 127 after 3s")
    assert incidents.is_unreachable_path("exit code 127")
    assert not incidents.is_unreachable_path("Script exited with code 1")


def test_the_path_is_captured_from_the_shell_message_not_swallowed():
    text = (
        "Script exited with code 127 | stderr: bash: "
        "/opt/hermes_prod/Research/TipGuard.sh: No such file or directory"
    )
    assert incidents.is_unreachable_path(text)
    # Case preserved: the store's classifier lowercases its own copy, and a path is not lowercase.
    assert incidents.captured_path(text) == "/opt/hermes_prod/Research/TipGuard.sh"
    assert up.captured_path(text) == "/opt/hermes_prod/Research/TipGuard.sh"


def test_a_127_that_names_its_path_as_command_not_found_still_routes():
    text = "Script exited with code 127 | stderr: /opt/hermes_prod/yaan-platform/bin/run.sh: command not found"
    assert up.captured_path(text) == "/opt/hermes_prod/yaan-platform/bin/run.sh"


# ── Routing ─────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path,owner,board",
    [
        ("/opt/hermes_prod/yaan-cross-markets-research/scripts/x.sh", "research-stl", "research"),
        ("/opt/hermes_prod/yaan-trading-strategy/scripts/x.py", "research-stl", "research"),
        ("/opt/hermes_prod/financially/data/x.csv", "financially-stl", "financially"),
        ("/opt/hermes_prod/yaan-platform/cron/tip-guard.sh", "platform-stl", "platform"),
        ("/opt/hermes_prod/yaan-unknown/x.sh", "platform-stl", "platform"),
        # Off-root: no component to route on, so the substrate's owner takes it.
        ("/Users/hermes_user/.hermes/x.py", "platform-stl", "platform"),
    ],
)
def test_each_path_routes_to_the_lane_that_owns_it(path, owner, board):
    assert up.owner_for_path(path) == owner
    assert up.board_for_owner(owner) == board


def test_no_card_is_routed_to_a_retired_board():
    # Measured 2026-11-01: defcon is `is_archived_stub=true` / `dispatch_enabled=false` (operator
    # ruling 2026-10-24: exactly three boards - financially, research, platform). A card filed
    # there is picked up by nothing, which is the silent failure this act exists to end.
    assert "defcon" not in up.BOARD_BY_OWNER.values()
    assert up.DEFAULT_BOARD in ("platform", "research", "financially")


# ── One card per (job, path root) ───────────────────────────────────────────────────────────


def test_one_card_per_job_and_path_root():
    a = "/opt/hermes_prod/yaan-platform/cron/tip-guard.sh"
    b = "/opt/hermes_prod/yaan-platform/cron/deploy-guard.sh"
    # Two scripts of ONE moved unit share a root: the unit broke, not the file name.
    assert up.path_root(a) == up.path_root(b) == "yaan-platform/cron"
    assert up.dedupe_key("job-1", up.path_root(a)) == "cron-unreachable-path:job-1:yaan-platform/cron"
    # Different jobs, or a different unit, still get their own card.
    assert up.dedupe_key("job-2", up.path_root(a)) != up.dedupe_key("job-1", up.path_root(a))


def test_twelve_repeat_failures_file_one_card_and_record_it_on_the_incident(monkeypatch, tmp_path):
    _point_db(monkeypatch, tmp_path)
    sink = RecordingSink()
    _drive_scheduler_with(sink, monkeypatch)
    job = _job()

    for _ in range(12):
        sched._upsert_incident_for_failure(job, MISSING_PROD_SCRIPT)

    assert len(sink.filed) == 1, sink.filed
    filed = sink.filed[0]
    assert filed["idempotency_key"] == "cron-unreachable-path:job-tip-guard:yaan-platform/cron"
    assert filed["assignee"] == "platform-stl"
    assert filed["board"] == "platform"
    assert "tip-guard.sh" in filed["title"]

    # The row is recovery's index: it names the card and the board that holds it.
    rows = incidents.list_incidents()
    assert len(rows) == 1
    assert rows[0]["failure_type"] == incidents.UNREACHABLE_PATH
    assert rows[0]["unreachable_card_id"] == "t_card00001"
    assert rows[0]["unreachable_card_board"] == "platform"


def test_a_distinct_failure_of_the_same_unit_files_the_same_key_again(monkeypatch, tmp_path):
    # A second, differently-worded failure mints a new incident signature - but it must still
    # ask for the SAME card, so the board's idempotency collapses it instead of adding one.
    _point_db(monkeypatch, tmp_path)
    sink = RecordingSink()
    _drive_scheduler_with(sink, monkeypatch)
    job = _job()

    sched._upsert_incident_for_failure(job, MISSING_PROD_SCRIPT)
    sched._upsert_incident_for_failure(
        job,
        "Script exited with code 127 | stderr: bash: line 51: "
        "/opt/hermes_prod/yaan-platform/cron/tip-guard.sh: No such file or directory",
    )

    assert len(sink.filed) == 2
    assert len({call["idempotency_key"] for call in sink.filed}) == 1


def test_other_failure_classes_are_left_to_their_own_owners(monkeypatch, tmp_path):
    _point_db(monkeypatch, tmp_path)
    sink = RecordingSink()
    _drive_scheduler_with(sink, monkeypatch)

    sched._upsert_incident_for_failure(_job(), "Script exited with code 1 | stderr: boom")

    assert sink.filed == []


# The estate's own number: 132 failures across 16 jobs. Sixteen jobs, one broken path each, with
# the failure count unevenly spread - the card count must be 16, and every card must name the
# lane that owns that tree.
_SCALE_PATHS = (
    ["/opt/hermes_prod/yaan-platform/cron/guard-%d.sh" % i for i in range(5)]
    + ["/opt/hermes_prod/yaan-cross-markets-research/cron/x-%d.sh" % i for i in range(3)]
    + ["/opt/hermes_prod/yaan-trading-strategy/scripts/y-%d.py" % i for i in range(2)]
    + ["/opt/hermes_prod/financially/scripts/z-%d.py" % i for i in range(6)]
)


def test_132_failures_across_16_jobs_file_at_most_16_cards(monkeypatch, tmp_path):
    assert len(_SCALE_PATHS) == 16
    _point_db(monkeypatch, tmp_path)
    sink = RecordingSink()
    _drive_scheduler_with(sink, monkeypatch)

    jobs = [{"id": "scale-job-%02d" % i, "name": "scale job %d" % i} for i in range(16)]
    # 132 failures: 8 each (128) plus one extra on the first four jobs.
    counts = [9, 9, 9, 9] + [8] * 12
    assert sum(counts) == 132

    for job, path, n in zip(jobs, _SCALE_PATHS, counts):
        error = (
            "Script exited with code 127 | stderr: | bash: %s: No such file or directory" % path
        )
        for _ in range(n):
            sched._upsert_incident_for_failure(job, error)

    assert len(sink.filed) == 16, len(sink.filed)
    assert len({call["idempotency_key"] for call in sink.filed}) == 16
    owners = sorted(call["assignee"] for call in sink.filed)
    assert owners.count("platform-stl") == 5
    assert owners.count("research-stl") == 5
    assert owners.count("financially-stl") == 6
    boards = sorted(call["board"] for call in sink.filed)
    assert boards.count("platform") == 5
    assert boards.count("research") == 5
    assert boards.count("financially") == 6



# ── Recovery ────────────────────────────────────────────────────────────────────────────────


def test_recovery_closes_the_card_and_clears_the_index(monkeypatch, tmp_path):
    _point_db(monkeypatch, tmp_path)
    sink = RecordingSink()
    _drive_scheduler_with(sink, monkeypatch)
    job = _job()

    sched._upsert_incident_for_failure(job, MISSING_PROD_SCRIPT)
    sched._resolve_incidents_for_recovered_job(job)

    assert len(sink.closed) == 1
    assert sink.closed[0]["card_id"] == "t_card00001"
    assert sink.closed[0]["board"] == "platform"
    assert "success" in sink.closed[0]["comment"].lower()
    # The index is spent: the next success costs one local read and no board access.
    assert incidents.open_unreachable_cards("job-tip-guard") == []
    # ...and the ledger still records that the incident happened.
    assert incidents.list_incidents(state="resolved")


def test_recovery_of_a_healthy_job_never_touches_the_board(monkeypatch, tmp_path):
    _point_db(monkeypatch, tmp_path)
    sink = RecordingSink()
    _drive_scheduler_with(sink, monkeypatch)

    sched._resolve_incidents_for_recovered_job(_job("job-never-broke"))

    assert sink.closed == []


# ── Failure-to-act is observable, never silent ─────────────────────────────────────────────


def test_a_filing_that_raises_is_recorded_on_the_incident_and_does_not_reach_the_scheduler(
    monkeypatch, tmp_path
):
    _point_db(monkeypatch, tmp_path)
    sink = RecordingSink()
    sink.fail_with = RuntimeError("board unreachable")
    _drive_scheduler_with(sink, monkeypatch)

    withheld, incident_id = sched._upsert_incident_for_failure(_job(), MISSING_PROD_SCRIPT)

    # The failure notice is delivered exactly as it was before the act existed.
    assert withheld is False
    row = incidents.get_incident(incident_id)
    assert row["unreachable_card_id"] is None
    assert "board unreachable" in row["unreachable_card_error"]


def test_a_failure_that_names_no_path_still_records_why_nothing_was_filed(monkeypatch, tmp_path):
    _point_db(monkeypatch, tmp_path)
    sink = RecordingSink()
    _drive_scheduler_with(sink, monkeypatch)
    job = _job()

    incident_id, _ = incidents.upsert_incident(job["id"], "exited 127 after 3s")
    up.act_on_failure(job["id"], "exited 127 after 3s", incident_id=incident_id)

    assert sink.filed == []
    row = incidents.get_incident(incident_id)
    assert row["unreachable_card_id"] is None
    assert "no path" in (row["unreachable_card_error"] or "")


# ── The real filing call, against a throwaway board ─────────────────────────────────────────


def _temp_board(tmp_path, monkeypatch):
    """A real kanban DB, so `KanbanSink` exercises the REAL create/complete calls."""
    from hermes_cli import kanban_db, kanban_db_connect, kanban_gate_invariants

    # The fleet's roster is a host fact, not a property of this seam.
    monkeypatch.setattr(kanban_gate_invariants, "gate_assignee", lambda *a, **k: None)
    # No board metadata -> no project inheritance, no priority policy: the test owns the store.
    monkeypatch.setattr(kanban_db, "read_board_metadata", lambda *a, **k: {})
    db_path = tmp_path / "throwaway-kanban.db"
    kanban_db.init_db(db_path)
    return lambda board: kanban_db_connect.connect(db_path=db_path, board="default"), db_path


def test_the_real_filing_call_holds_one_card_for_the_key(tmp_path, monkeypatch):
    connect, db_path = _temp_board(tmp_path, monkeypatch)
    sink = up.KanbanSink(connect=connect)
    kwargs = dict(
        board="default",
        idempotency_key="cron-unreachable-path:job-x:yaan-platform/cron",
        title="[UNREACHABLE-PATH] cron job job-x invokes /opt/hermes_prod/yaan-platform/cron/a.sh",
        body="body",
        assignee="platform-stl",
    )

    first = sink.file(**kwargs)
    second = sink.file(**kwargs)

    assert first and second and first == second
    conn = connect("default")
    try:
        rows = conn.execute(
            "SELECT id, status, idempotency_key FROM tasks WHERE idempotency_key = ?",
            (kwargs["idempotency_key"],),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1, [dict(row) for row in rows]
    # Filed to be DISPATCHED, not to sit in a running state nobody owns.
    assert rows[0]["status"] == "ready"


def test_the_real_close_completes_the_card_with_its_evidence(tmp_path, monkeypatch):
    connect, db_path = _temp_board(tmp_path, monkeypatch)
    sink = up.KanbanSink(connect=connect)
    card_id = sink.file(
        board="default",
        idempotency_key="cron-unreachable-path:job-y:yaan-platform/cron",
        title="[UNREACHABLE-PATH] cron job job-y invokes /opt/hermes_prod/yaan-platform/cron/b.sh",
        body="body",
        assignee="platform-stl",
    )

    assert sink.close(
        card_id,
        board="default",
        comment="the job ran ok",
        result="closed by cron: job job-y ran successfully",
    )

    conn = connect("default")
    try:
        row = conn.execute("SELECT status, result FROM tasks WHERE id = ?", (card_id,)).fetchone()
        comments = conn.execute(
            "SELECT body FROM task_comments WHERE task_id = ?", (card_id,)
        ).fetchall()
    finally:
        conn.close()
    assert row["status"] == "done"
    assert "ran successfully" in (row["result"] or "")
    assert any("ran ok" in c["body"] for c in comments)
    # A second close is a no-op, not an error: recovery may run again.
    assert sink.close(card_id, board="default", comment="again", result="again") is False
