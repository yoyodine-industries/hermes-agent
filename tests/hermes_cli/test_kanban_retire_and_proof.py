"""Retirement is not a completion, and a declared proof is executed at close.

The defect under test (ruling ESC-36b70d83894494d2, card t_1da139e3): the
platform board counted ``t_fd2c802f`` as DONE at 0s. Its handoff said
"SUPERSEDED by t_b3efee10", the sibling it deferred to then sat ``ready``, and
the pair wrote ZERO rows — yet the done count took the card, because ``complete``
was the only door a duplicate could use.

The contract:

* R1 — a handoff in the retire vocabulary that NAMES a card is refused with
  ``retire_is_not_a_completion``, and nothing about the card changes.
* R2 — the refusal is not a dead end: ``--supersede-ack <board>/<card>`` archives
  the duplicate in the same call, leaving it ``archived`` (excluded from done).
* R3 — a retirement may not point at a destination that is itself closed
  (``destination_closed``): a card cannot be retired by citing a card that is
  over.
* P1 — a card carrying ``proof: cmd=...`` / ``proof: path=...`` is judged by
  EXECUTING it; a negative answer refuses the close and changes nothing.
* P2 — a passing proof is recorded on the closing run and echoed in the result.
* P3 — ``proof_required: true`` with no proof line refuses with ``proof_missing``.
* P4 — every close records proven or unproven; the board's counts print both plus
  ``retired``, so a done count can never again be read as work performed.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pytest

from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_close_proof as proof
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_WORKSPACES_ROOT"):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


def _latest_run(conn, task_id):
    return conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()


def _run_meta(conn, task_id):
    row = _latest_run(conn, task_id)
    assert row is not None
    return json.loads(row["metadata"] or "{}")


def _events(conn, task_id, kind):
    return conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
        (task_id, kind),
    ).fetchall()


def _complete_ns(*task_ids, **over):
    ns = argparse.Namespace(
        task_ids=list(task_ids), result=None, summary=None, metadata=None,
        force=False, supersede_ack=None,
    )
    for key, value in over.items():
        setattr(ns, key, value)
    return ns


# ---------------------------------------------------------------------------
# R1 — a retire claim is refused at the completion door
# ---------------------------------------------------------------------------

def test_supersede_summary_is_refused_and_changes_nothing(conn):
    dup = kb.create_task(conn, title="record the peer review row")
    successor = kb.create_task(conn, title="the one that inherits it")

    out = kb.complete_task(
        conn, dup, summary=f"SUPERSEDED by {successor}",
        result=f"deferred to {successor}",
    )

    assert isinstance(out, kb.CompletionRefusal)
    assert out.cause == "retire_is_not_a_completion"
    # Nothing changed: the card is still open and no completion was recorded.
    assert kb.get_task(conn, dup).status == "ready"
    assert _latest_run(conn, dup) is None
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'completed'", (dup,),
    ).fetchone()[0] == 0
    # The refusal is self-describing: it names the card, the reason token and the door.
    assert successor in out.detail
    assert "hermes kanban archive" in out.detail
    assert "--reason superseded" in out.detail
    assert "--supersede-ack" in out.detail
    blocked = _events(conn, dup, "completion_blocked_retire")
    assert len(blocked) == 1
    payload = json.loads(blocked[0]["payload"])
    assert payload["cited_card"] == successor
    assert payload["phrase"] == "superseded"


@pytest.mark.parametrize("phrase", ["duplicate of", "folded into", "redirected to",
                                    "replaced by", "closed moot by"])
def test_every_retire_token_that_names_a_card_is_refused(conn, phrase):
    dup = kb.create_task(conn, title="dup")
    other = kb.create_task(conn, title="other")
    out = kb.complete_task(conn, dup, summary=f"{phrase.capitalize()} {other}")
    assert isinstance(out, kb.CompletionRefusal)
    assert out.cause == "retire_is_not_a_completion"
    assert kb.get_task(conn, dup).status == "ready"


def test_a_close_that_merely_mentions_a_card_is_not_refused(conn):
    """The refusal needs BOTH conditions, so ordinary handoffs still close."""
    t = kb.create_task(conn, title="write the peer-review runbook")
    other = kb.create_task(conn, title="the evidence card")
    done = kb.complete_task(
        conn, t, summary=f"Landed; evidence recorded on {other} and the row is live.",
    )
    assert done is True
    assert kb.get_task(conn, t).status == "done"


def test_retire_vocabulary_without_a_card_id_is_not_refused(conn):
    """No successor named = nothing to hand off to; the deterministic clause needs a card."""
    t = kb.create_task(conn, title="superseded generation")
    done = kb.complete_task(conn, t, summary="superseded by a newer generation of the tool")
    assert done is True


# ---------------------------------------------------------------------------
# R2 — supersede_ack performs the archive in the same call
# ---------------------------------------------------------------------------

def test_supersede_ack_archives_the_duplicate_and_leaves_the_successor_open(conn):
    successor = kb.create_task(conn, title="the real card")
    dup = kb.create_task(conn, title="the duplicate")
    board = kb._archive_source_board(conn)

    out = kb.complete_task(
        conn, dup, summary=f"Duplicate of {successor}",
        supersede_ack=f"{board}/{successor}", actor="platform-coder",
    )

    assert isinstance(out, kb.Retirement)
    assert out.reason == "duplicate"
    assert kb.get_task(conn, dup).status == "archived"
    assert kb.get_task(conn, successor).status == "ready"
    # The rehome key is written on the destination — the record a completion never wrote.
    held = conn.execute(
        "SELECT idempotency_key FROM tasks WHERE id = ?", (successor,),
    ).fetchone()[0]
    assert held == f"rehome-{board}-{dup}"
    receipt = json.loads(
        conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'archived' "
            "ORDER BY id DESC LIMIT 1", (dup,),
        ).fetchone()["payload"]
    )
    assert receipt["reason"] == "duplicate"
    assert receipt["destination"] == {"board": board, "card": successor}
    assert receipt["actor"] == "platform-coder"
    # And it is NOT in the done count.
    assert kb.board_stats(conn)["by_status"].get("done", 0) == 0


def test_supersede_ack_naming_an_archived_card_is_refused(conn):
    gone = kb.create_task(conn, title="already retired")
    dup = kb.create_task(conn, title="dup")
    board = kb._archive_source_board(conn)
    kb.archive_task(conn, gone, reason="operator", actor="test")

    out = kb.complete_task(conn, dup, summary=f"Superseded by {gone}",
                           supersede_ack=f"{board}/{gone}")
    assert isinstance(out, kb.CompletionRefusal)
    assert out.cause == "supersede_ack_refused"
    assert "destination_archived" in out.detail
    assert kb.get_task(conn, dup).status == "ready"


# ---------------------------------------------------------------------------
# R3 — a retirement may not point at a closed destination
# ---------------------------------------------------------------------------

def test_archiving_onto_a_done_destination_is_refused(conn):
    dest = kb.create_task(conn, title="finished successor")
    src = kb.create_task(conn, title="dup")
    assert kb.complete_task(conn, dest, summary="Landed and verified; artifact on disk.")
    board = kb._archive_source_board(conn)

    with pytest.raises(kb.ArchiveRefused) as exc:
        kb.archive_task(conn, src, reason="duplicate", destination=f"{board}/{dest}",
                        actor="test")
    assert exc.value.code == "destination_closed"
    assert "successor" in str(exc.value)
    # The source did not flip, and no rehome key was written on the closed card.
    assert kb.get_task(conn, src).status == "ready"
    assert conn.execute(
        "SELECT idempotency_key FROM tasks WHERE id = ?", (dest,),
    ).fetchone()[0] in (None, "")


def test_supersede_ack_naming_a_done_card_is_refused(conn):
    dest = kb.create_task(conn, title="finished")
    dup = kb.create_task(conn, title="dup")
    board = kb._archive_source_board(conn)
    assert kb.complete_task(conn, dest, summary="Landed; artifact verified on disk.")

    out = kb.complete_task(conn, dup, summary=f"Duplicate of {dest}",
                           supersede_ack=f"{board}/{dest}")
    assert isinstance(out, kb.CompletionRefusal)
    assert out.cause == "supersede_ack_refused"
    assert "destination_closed" in out.detail
    assert kb.get_task(conn, dup).status == "ready"


# ---------------------------------------------------------------------------
# P1/P2 — the declared proof is EXECUTED
# ---------------------------------------------------------------------------

def test_failing_cmd_proof_refuses_the_close_and_changes_nothing(conn, tmp_path):
    artifact = tmp_path / "not-yet.txt"
    t = kb.create_task(conn, title="emit the artifact",
                       body=f"proof: cmd=test -f {artifact}\n")
    out = kb.complete_task(conn, t, summary="wrote the artifact and verified it")
    assert isinstance(out, kb.CompletionRefusal)
    assert out.cause == "proof_failed"
    assert "test -f" in out.detail
    assert kb.get_task(conn, t).status == "ready"
    assert _latest_run(conn, t) is None
    blocked = _events(conn, t, "completion_blocked_proof")
    assert len(blocked) == 1
    payload = json.loads(blocked[0]["payload"])
    assert payload["cause"] == "proof_failed"
    assert payload["rc"] == 1


def test_passing_cmd_proof_closes_records_and_echoes(conn, tmp_path):
    artifact = tmp_path / "here.txt"
    artifact.write_text("row\n", encoding="utf-8")
    t = kb.create_task(conn, title="emit the artifact",
                       body=f"proof: cmd=test -f {artifact}\n\nhandoff prose\n")
    done = kb.complete_task(conn, t, summary="wrote the artifact and verified it")
    assert done is True
    task = kb.get_task(conn, t)
    assert task.status == "done"
    assert "[proof rc=0 cmd=test -f" in task.result
    meta = _run_meta(conn, t)
    assert meta["close_proof"]["verified"] is True
    assert meta["close_proof"]["kind"] == "cmd"
    assert meta["close_proof"]["rc"] == 0
    split = kb.board_stats(conn)["proof_split"]
    assert split["done"] == 1 and split["proven"] == 1 and split["unproven"] == 0


def test_expect_lets_a_negative_rc_be_the_pass_condition(conn):
    t = kb.create_task(conn, title="absence is the proof",
                       body="proof: cmd=! test -f /nonexistent/never expect=0\n")
    assert kb.complete_task(conn, t, summary="confirmed the file is absent.") is True


def test_path_proof_requires_a_match_of_the_minimum_size(conn, tmp_path):
    target = tmp_path / "out" / "report.json"
    target.parent.mkdir()
    t = kb.create_task(conn, title="write the report",
                       body=f"proof: path={target} min_bytes=5\n")
    assert isinstance(kb.complete_task(conn, t, summary="report written"),
                      kb.CompletionRefusal)          # the file does not exist yet
    target.write_text("{}\n", encoding="utf-8")     # 3 bytes: too small
    assert isinstance(kb.complete_task(conn, t, summary="report written"),
                      kb.CompletionRefusal)
    target.write_text('{"ok": true}\n', encoding="utf-8")
    assert kb.complete_task(conn, t, summary="report written") is True
    meta = _run_meta(conn, t)
    assert meta["close_proof"]["verified"] is True
    assert meta["close_proof"]["kind"] == "path"
    assert "[proof matches=1" in kb.get_task(conn, t).result


def test_a_glob_proof_passes_on_any_matching_file(conn, tmp_path):
    t = kb.create_task(conn, title="any log exists",
                       body=f"proof: path={tmp_path}/*.log\n")
    assert isinstance(kb.complete_task(conn, t, summary="log present"),
                      kb.CompletionRefusal)
    (tmp_path / "run.log").write_text("ok\n", encoding="utf-8")
    assert kb.complete_task(conn, t, summary="log present") is True


def test_a_malformed_proof_line_refuses_rather_than_being_ignored(conn):
    t = kb.create_task(conn, title="bad declaration", body="proof: whatever looks fine\n")
    out = kb.complete_task(conn, t, summary="done")
    assert isinstance(out, kb.CompletionRefusal)
    assert out.cause == "proof_failed"
    assert "cmd=" in out.detail and "path=" in out.detail


def test_a_timed_out_proof_is_a_failure_not_a_crash(conn):
    t = kb.create_task(conn, title="slow proof", body="proof: cmd=sleep 5 timeout=1\n")
    out = kb.complete_task(conn, t, summary="done")
    assert isinstance(out, kb.CompletionRefusal)
    assert out.cause == "proof_failed"
    assert "timed out after 1s" in out.detail


# ---------------------------------------------------------------------------
# P3 — proof_required
# ---------------------------------------------------------------------------

def test_proof_required_without_a_proof_line_is_refused(conn):
    t = kb.create_task(conn, title="must prove it", body="proof_required: true\n")
    out = kb.complete_task(conn, t, summary="done")
    assert isinstance(out, kb.CompletionRefusal)
    assert out.cause == "proof_missing"
    assert "proof:" in out.detail
    assert kb.get_task(conn, t).status == "ready"


def test_proof_required_can_be_declared_on_the_close(conn):
    t = kb.create_task(conn, title="caller requires proof")
    out = kb.complete_task(conn, t, summary="done", metadata={"proof_required": True})
    assert isinstance(out, kb.CompletionRefusal)
    assert out.cause == "proof_missing"


def test_operator_force_waives_a_missing_declaration_but_records_it(conn):
    t = kb.create_task(conn, title="must prove it", body="proof_required: true\n")
    assert kb.complete_task(conn, t, summary="closed by operator", force=True) is True
    meta = _run_meta(conn, t)
    assert meta["close_proof"]["verified"] is False
    assert "waived" in meta["close_proof"]


def test_force_does_not_waive_a_failed_proof(conn, tmp_path):
    t = kb.create_task(conn, title="must prove it",
                       body=f"proof: path={tmp_path}/absent.txt\n")
    out = kb.complete_task(conn, t, summary="done", force=True)
    assert isinstance(out, kb.CompletionRefusal)
    assert out.cause == "proof_failed"


# ---------------------------------------------------------------------------
# P4 — counts
# ---------------------------------------------------------------------------

def test_counts_carry_done_proven_unproven_retired(conn):
    plain = kb.create_task(conn, title="plain close")
    proven = kb.create_task(conn, title="proven close", body="proof: cmd=true\n")
    retired = kb.create_task(conn, title="retired")
    assert kb.complete_task(conn, plain, summary="closed without a proof")
    assert kb.complete_task(conn, proven, summary="closed with a proof")
    kb.archive_task(conn, retired, reason="operator", actor="test")

    split = kb.board_stats(conn)["proof_split"]
    assert split == {"done": 2, "proven": 1, "unproven": 1, "retired": 1}


# ---------------------------------------------------------------------------
# The CLI surface: the refusal's remedy is a real, accepted flag
# ---------------------------------------------------------------------------

def test_cli_complete_supersede_ack_performs_the_archive(conn, capsys):
    successor = kb.create_task(conn, title="real")
    dup = kb.create_task(conn, title="dup")
    board = kb._archive_source_board(conn)
    rc = kb_cli._cmd_complete(_complete_ns(
        dup, summary=f"Superseded by {successor}",
        supersede_ack=f"{board}/{successor}"))
    assert rc == 0
    assert kb.get_task(conn, dup).status == "archived"
    assert kb.get_task(conn, successor).status == "ready"
    # The RECEIPT must not say "Completed": the close archived the card and counted no work.
    printed = capsys.readouterr().out
    assert "Retired" in printed and "Completed" not in printed
    assert "NOT counted as done" in printed


def test_cli_complete_without_ack_exits_nonzero_and_names_the_remedy(conn, capsys):
    dup = kb.create_task(conn, title="dup")
    successor = kb.create_task(conn, title="real")
    rc = kb_cli._cmd_complete(_complete_ns(dup, summary=f"Duplicate of {successor}"))
    assert rc == 1
    err = capsys.readouterr()
    assert "retire_is_not_a_completion" in (err.out + err.err)
    assert kb.get_task(conn, dup).status == "ready"


def test_cli_refuses_supersede_ack_across_multiple_ids(conn):
    a = kb.create_task(conn, title="a")
    b = kb.create_task(conn, title="b")
    rc = kb_cli._cmd_complete(_complete_ns(a, b, summary="x", supersede_ack="platform/z"))
    assert rc == 2
    assert kb.get_task(conn, a).status != "archived"


# ---------------------------------------------------------------------------
# The vocabulary itself (unit level, no board)
# ---------------------------------------------------------------------------

def test_retire_claim_needs_both_a_phrase_and_a_card():
    assert proof.find_retire_claim("SUPERSEDED by t_1da139e3") == {
        "phrase": "superseded", "card": "t_1da139e3"}
    assert proof.find_retire_claim("superseded by a newer run") is None
    assert proof.find_retire_claim("see t_1da139e3 for the evidence") is None


def test_parse_proof_reads_markdown_shapes():
    spec = proof.parse_proof("- `proof: cmd=grep -c x /tmp/f expect=1 timeout=30`\n")
    assert spec["kind"] == "cmd"
    assert spec["cmd"] == "grep -c x /tmp/f"
    assert spec["expect"] == 1 and spec["timeout"] == 30
    spec = proof.parse_proof("> proof: path=/tmp/*.json min_bytes=10\n")
    assert spec["kind"] == "path"
    assert spec["path"] == "/tmp/*.json" and spec["min_bytes"] == 10
