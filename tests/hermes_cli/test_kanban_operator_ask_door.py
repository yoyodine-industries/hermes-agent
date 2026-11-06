"""The operator-ask door must be runnable from a CLAIMED worker (card t_af03b766).

WHY
---
``admin_operator_card`` shells ``hermes kanban create`` / ``defcon designate`` / ``edit``, and the
delegated-child fence refused every one of them for a dispatched worker — so the door its own
description reserves for host-administration seats was unrunnable from those seats, and every
consent-gated card a seat parked needed a top-level session for the mechanical write.

The ruling grants ONE named exemption covering exactly three acts: the create that births the ask
at the target board's floor, the designation write that follows it, and the edit that raises THAT
card's priority into the operator band. It is BOUND to the card the same door invocation just
created (the door's request id, written onto the card as an ``Operator-ask-door:`` provenance
line), so it can never become "a worker may re-prioritise any card on its own board".

This suite drives the REAL CLI seam in a child process whose parent IS the live-claim
``worker_pid`` (the provenance arm), which is what a dispatched worker's shell looks like.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_operator_ask as koa

REPO = Path(__file__).resolve().parents[2]
BOARD = "ask-door-probe"
DOOR = "admin_operator_card"
REQUEST = "req-t_af03b766-0001"

#: One CLI invocation per call: the door's three acts are three real calls.
CREATE_PROBE = """\
import argparse
import json
import sys

from hermes_cli import kanban, kanban_parser

parser = argparse.ArgumentParser(prog="hermes")
sub = parser.add_subparsers(dest="command")
kanban_parser.build_parser(sub)
args = parser.parse_args(["kanban", *sys.argv[1:]])
rc = kanban.kanban_command(args)
raise SystemExit(rc)
"""

SANITY_TREE_PROBE = "import hermes_cli\nprint(hermes_cli.__file__)\n"


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_OPERATOR_ASK",
                "HERMES_KANBAN_ADVISORY_SKILLS", "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return root


def _board_db(root: Path, slug: str) -> Path:
    return kb.boards_root() / slug / "kanban.db"


def _write(path: Path, src: str) -> Path:
    path.write_text(src, encoding="utf-8")
    return path


def _child_env(root: Path, board: str, **extra) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("HERMES_KANBAN_") and not k.startswith("HERMES_DELEGATED")}
    env["HERMES_HOME"] = str(root)
    env["HERMES_KANBAN_HOME"] = str(root)
    env["HERMES_KANBAN_DB"] = str(_board_db(root, board))
    env["HERMES_KANBAN_BOARD"] = board
    env["PYTHONPATH"] = str(REPO)
    env.update(extra)
    return env


def _run(script: Path, root: Path, board: str, *argv, **extra):
    return subprocess.run(
        [sys.executable, str(script), "--board", board, *argv],
        env=_child_env(root, board, **extra), cwd=str(REPO),
        capture_output=True, text=True, timeout=120,
    )


def _fingerprint_of_this_process():
    from hermes_cli.kanban_db_dispatch import _process_fingerprint

    return _process_fingerprint(os.getpid())


def _make_board_with_live_claim(root: Path, *, worker_pid: int, fingerprint):
    """A board with one LIVE claim owned by *worker_pid* — the fence's provenance arm."""
    kb.create_board(BOARD, name=BOARD)
    db = _board_db(root, BOARD)
    conn = kbc.connect(db_path=db)
    try:
        task_id = kb.create_task(conn, title="owned by a live worker", assignee="default",
                                 board=BOARD)
        conn.execute(
            "UPDATE tasks SET status='running', worker_pid=?, worker_started_at=?, "
            "claim_lock=?, claim_expires=? WHERE id=?",
            (int(worker_pid), fingerprint, "test-host:probe",
             int(time.time()) + 3600, task_id),
        )
        conn.commit()
    finally:
        conn.close()
    from agent.delegation_context import _live_claim_cache

    _live_claim_cache.clear()
    return task_id


def _card(conn, task_id):
    conn.row_factory = __import__("sqlite3").Row
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def _created_id(stdout: str) -> str:
    text = stdout.strip()
    try:
        payload = json.loads(text)
        if isinstance(payload, dict) and "id" in payload:
            return payload["id"]
    except json.JSONDecodeError:
        pass
    match = re.search(r'"id"\s*:\s*"(t_[0-9a-f]+)"', text)
    assert match, f"no created id in stdout: {stdout!r}"
    return match.group(1)


@pytest.fixture
def fenced_board(home, tmp_path):
    """A board whose claim is held by THIS process, plus the CLI probe script."""
    claim_id = _make_board_with_live_claim(home, worker_pid=os.getpid(),
                                           fingerprint=_fingerprint_of_this_process())
    script = _write(tmp_path / "cli.py", CREATE_PROBE)
    return home, script, claim_id


def _create(script, root, *extra):
    res = _run(script, root, BOARD, "create", "OPERATOR ASK: probe", "--priority", "0",
               "--assignee", "default", "--json", *extra)
    return res


def _designate(script, root, card_id, *, request=REQUEST, card_arg=None):
    return _run(script, root, BOARD, "defcon", "designate", card_id,
                "--reason", "probe: the operator ask", "--authority", "default",
                "--operator-ask-door", DOOR, "--operator-ask-raise", request,
                "--operator-ask-card", card_arg or card_id, "--json")


def _raise(script, root, card_id, priority="999999", *, request=REQUEST, card_arg=None):
    return _run(script, root, BOARD, "edit", card_id, "--priority", priority,
                "--operator-ask-door", DOOR, "--operator-ask-raise", request,
                "--operator-ask-card", card_arg or card_id)


def test_the_child_tree_is_the_tree_under_test(tmp_path, home):
    script = _write(tmp_path / "which_tree.py", SANITY_TREE_PROBE)
    res = subprocess.run([sys.executable, str(script)], env=_child_env(home, BOARD),
                         cwd=str(REPO), capture_output=True, text=True, timeout=60)
    assert str(REPO) in res.stdout, res.stdout + res.stderr


def test_control_a_plain_create_is_still_fenced(fenced_board):
    """The harness can see the refusal: without the exemption the fence holds."""
    root, script, _claim = fenced_board
    res = _create(script, root)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "child contexts cannot mutate" in res.stderr


def test_the_full_sequence_succeeds_from_a_claimed_worker(fenced_board):
    """create -> designate -> raise, all admitted, ending in the operator band."""
    root, script, claim_id = fenced_board
    created = _create(script, root, "--operator-ask-door", DOOR,
                      "--operator-ask-raise", REQUEST)
    assert created.returncode == 0, created.stdout + created.stderr
    card_id = _created_id(created.stdout)

    designated = _designate(script, root, card_id)
    assert designated.returncode == 0, designated.stdout + designated.stderr

    raised = _raise(script, root, card_id)
    assert raised.returncode == 0, raised.stdout + raised.stderr

    conn = kbc.connect(db_path=_board_db(root, BOARD))
    try:
        row = _card(conn, card_id)
        assert int(row["priority"]) == 999999, dict(row)
        body = row["body"] or ""
        assert body.count("Operator-ask-door:") == 1, body  # the kernel writes ONE line
        fields = koa.parse_provenance(body)
        assert fields["door"] == DOOR
        # The card carries a DIGEST of the door's per-call id, never the id itself: the line is a
        # bind, not a key (review round 1, card t_af03b766).
        assert fields["request"] == koa.request_digest(REQUEST), fields
        assert REQUEST not in body, body
        assert fields["filed-by"] == "default" or fields["filed-by"]
        # The provenance names the claim whose ancestor pid would otherwise fence the call.
        assert fields["fenced-by"] == claim_id
        events = [r[0] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (card_id,))]
        assert "reprioritized" in events  # the kernel's own re-rank door wrote the lift
        assert conn.execute("SELECT COUNT(*) FROM priority_designations WHERE task_id = ?",
                            (card_id,)).fetchone()[0] == 1
    finally:
        conn.close()


def test_a_later_invocation_cannot_replay_the_digest_printed_on_the_card(fenced_board):
    """The bind stops the measured round-1 hole: a third process, same claimed worker, reading the
    card it did not create, must not be able to designate or raise it.

    The create writes only a DIGEST of its per-call request id, so presenting what the card
    displays digests to a different value and is refused — the exemption reaches only the card the
    SAME invocation made.
    """
    root, script, _claim = fenced_board
    created = _create(script, root, "--operator-ask-door", DOOR,
                      "--operator-ask-raise", REQUEST)
    assert created.returncode == 0, created.stdout + created.stderr
    card_id = _created_id(created.stdout)

    conn = kbc.connect(db_path=_board_db(root, BOARD))
    try:
        printed = koa.parse_provenance(_card(conn, card_id)["body"] or "")
        assert printed["request"] == koa.request_digest(REQUEST), printed
    finally:
        conn.close()

    # A THIRD invocation — no create of its own — presents exactly what the card displays.
    designated = _designate(script, root, card_id, request=printed["request"])
    assert designated.returncode == 2, designated.stdout + designated.stderr
    assert "carries no provenance" in designated.stderr, designated.stderr
    raised = _raise(script, root, card_id, request=printed["request"])
    assert raised.returncode == 2, raised.stdout + raised.stderr
    assert "carries no provenance" in raised.stderr, raised.stderr

    # Nothing moved: still at the floor, no designation ledger row.
    conn = kbc.connect(db_path=_board_db(root, BOARD))
    try:
        row = _card(conn, card_id)
        assert int(row["priority"]) == 0, dict(row)
        assert conn.execute("SELECT COUNT(*) FROM priority_designations WHERE task_id = ?",
                            (card_id,)).fetchone()[0] == 0
    finally:
        conn.close()


def test_a_create_body_may_not_carry_the_kernels_provenance_line(fenced_board, tmp_path):
    """A worker-supplied `Operator-ask-door:` line would let the card misstate who filed it and
    which claim it lifted (measured in round 1: parse returned filed-by=some-other-lane while the
    kernel's own line sat second). The create refuses such a body outright — nothing is carded.
    """
    root, script, _claim = fenced_board
    forged = ("Operator-ask-door: door=admin_operator_card request=req-forged "
              "filed-by=some-other-lane fenced-by=t_00000000 board=elsewhere")
    body_file = tmp_path / "forged_body.md"
    body_file.write_text(forged, encoding="utf-8")
    conn = kbc.connect(db_path=_board_db(root, BOARD))
    try:
        before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    finally:
        conn.close()

    for body_flag in (("--body", forged), ("--body-file", str(body_file))):
        res = _run(script, root, BOARD, "create", "spoofed ask", "--priority", "0",
                   *body_flag, "--operator-ask-door", DOOR,
                   "--operator-ask-raise", REQUEST)
        assert res.returncode == 2, res.stdout + res.stderr
        assert "may not be handed a body" in res.stderr, (body_flag[0], res.stderr)

    conn = kbc.connect(db_path=_board_db(root, BOARD))
    try:
        after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        assert after == before, (before, after)
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE body LIKE '%some-other-lane%'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_a_plain_edit_of_an_ordinary_card_is_still_fenced(fenced_board):
    """Acceptance bullet 2, the other half: the exemption covers ONE card, THIS invocation's.

    An ordinary card on the same board, raised into the band by an edit with NO exemption flags,
    still hits the fence's own message (round 1; previously verified in review).
    """
    root, script, _claim = fenced_board
    conn = kbc.connect(db_path=_board_db(root, BOARD))
    try:
        ordinary = kb.create_task(conn, title="an ordinary card", board=BOARD)
    finally:
        conn.close()
    res = _run(script, root, BOARD, "edit", ordinary, "--priority", "999999")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "child contexts cannot mutate" in res.stderr, res.stderr


def test_the_exemption_cannot_lift_a_different_card(fenced_board):
    """The bind: a card the door did not just create stays fenced by the exemption's own check."""
    root, script, _claim = fenced_board
    created = _create(script, root, "--operator-ask-door", DOOR,
                      "--operator-ask-raise", REQUEST)
    assert created.returncode == 0, created.stdout + created.stderr
    card_id = _created_id(created.stdout)

    # Another card on the same board, filed before the door's card.
    conn = kbc.connect(db_path=_board_db(root, BOARD))
    try:
        other = kb.create_task(conn, title="someone else's card", board=BOARD)
    finally:
        conn.close()

    res = _raise(script, root, other)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "carries no provenance" in res.stderr

    # The created card, but under a request id the create never wrote.
    res = _raise(script, root, card_id, request="req-somebody-else")
    assert res.returncode == 2, res.stdout + res.stderr
    assert "carries no provenance" in res.stderr


def test_a_demotion_is_not_a_raise(fenced_board):
    """The edit act is the RAISE: the exemption never lowers the card it is bound to."""
    root, script, _claim = fenced_board
    created = _create(script, root, "--operator-ask-door", DOOR,
                      "--operator-ask-raise", REQUEST)
    card_id = _created_id(created.stdout)
    assert _designate(script, root, card_id).returncode == 0
    res = _raise(script, root, card_id, priority="100")
    assert res.returncode == 2, res.stdout + res.stderr
    assert "may only RAISE" in res.stderr


def test_the_create_may_not_be_born_in_the_operator_band(fenced_board):
    root, script, _claim = fenced_board
    res = _run(script, root, BOARD, "create", "smuggled ask", "--priority", "995000",
               "--operator-ask-door", DOOR, "--operator-ask-raise", REQUEST)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "may not be BORN" in res.stderr


def test_a_partial_grant_is_refused_not_ignored(fenced_board):
    root, script, _claim = fenced_board
    res = _run(script, root, BOARD, "create", "half a grant", "--priority", "0",
               "--operator-ask-door", DOOR)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "--operator-ask-raise" in res.stderr


def test_an_unknown_door_is_refused(fenced_board):
    root, script, _claim = fenced_board
    res = _create(script, root, "--operator-ask-door", "some_other_tool",
                  "--operator-ask-raise", REQUEST)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "not an operator-ask door" in res.stderr


def test_the_exemption_does_not_cover_another_verb(fenced_board):
    """Only the three acts: complete/comment/claim stay fenced, with or without the flags."""
    root, script, claim_id = fenced_board
    for verb, argv in (
        ("comment", ["comment", claim_id, "hi"]),
        ("complete", ["complete", claim_id, "--summary", "x"]),
        ("claim", ["claim", claim_id]),
    ):
        # The fence, unchanged, for the ordinary invocation.
        res = _run(script, root, BOARD, *argv)
        assert res.returncode == 1, f"{verb}: {res.stdout}{res.stderr}"
        assert "child contexts cannot mutate" in res.stderr, f"{verb}: {res.stderr}"
        # And a grant-shaped set of flags on a verb outside the three acts is REFUSED, never
        # silently ignored: the flag vocabulary is not a general-purpose key.
        res = _run(script, root, BOARD, *argv, "--operator-ask-door", DOOR,
                   "--operator-ask-raise", REQUEST)
        assert res.returncode != 0, f"{verb}: {res.stdout}{res.stderr}"
        assert res.returncode == 2, f"{verb}: {res.stderr}"


def test_the_other_fence_arms_still_refuse_a_non_exempt_act(fenced_board):
    """The marker arm is untouched: a fenced worker's plain create is refused with the marker set."""
    root, script, _claim = fenced_board
    res = _run(script, root, BOARD, "create", "x",
               HERMES_DELEGATED_CHILD_CONTEXT="1")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "child contexts cannot mutate" in res.stderr


def test_the_exemption_is_reachable_from_exactly_one_call_site():
    """A second tool must not inherit the exemption by copying the flag or the call."""
    calls = {"grant_for": [], "scoped": []}
    for path in sorted(REPO.joinpath("hermes_cli").glob("*.py")) + sorted(
            REPO.joinpath("agent").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            owner = getattr(getattr(func, "value", None), "id", None)
            if isinstance(func, ast.Name) and name == "grant_for":
                calls["grant_for"].append(path)
            elif isinstance(func, ast.Attribute) and name == "grant_for" and owner == "koa":
                calls["grant_for"].append(path)
            elif isinstance(func, ast.Attribute) and name == "scoped" and owner == "koa":
                calls["scoped"].append(path)
    assert calls["grant_for"] == [REPO / "hermes_cli" / "kanban.py"], calls
    assert calls["scoped"] == [REPO / "hermes_cli" / "kanban.py"], calls
    # And the generic tool surface never imports the exemption module at all.
    for path in sorted((REPO / "tools").glob("*.py")):
        assert "kanban_operator_ask" not in path.read_text(encoding="utf-8"), path
