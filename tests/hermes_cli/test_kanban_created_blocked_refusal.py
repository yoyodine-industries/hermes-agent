"""A card is NEVER created blocked (operator ruling 2026-09-27, card t_5c89c04e).

``initial_status="blocked"`` is refused at the create seam BEFORE any write, the attempt
is escalated, and the deviation is filed on the board against the creating lane by name.

Why the class is a defect and not a preference: a born-blocked card carries no
``block_kind`` and no reason, so it hides work from the dispatcher, leaves a row no
reader can diagnose, and the flag does not even hold (``recompute_ready`` promotes it).
Its signature is an ``task_events.kind='blocked'`` row whose payload carries
``reason='initial_status'`` — the created-blocked class the blocked-card workflow
(t_9ad5e246) dispositions, and the boundary this refusal must not re-generate.

What these tests pin down:

* the kernel seam refuses and creates NOTHING for the attempt (no row, no event);
* the refusal message carries the two documented legitimate moves;
* the deviation row is filed on the attempt's board, assigned to the lane BY NAME, and
  names the attempted title and the enforcement action;
* a repeat attempt collapses onto the same deviation row instead of spamming the board;
* the ``kanban_create`` tool answers with the refusal (the seam is the single door);
* the swarm root is no longer born blocked (the kernel's own caller was migrated, not
  exempted) and leaves no created-blocked signature behind.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "platform-worker")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # A dispatcher-spawned worker shell pins these; a test must not touch a live board.
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(var, raising=False)
    from hermes_cli import kanban_db as kb

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _created_blocked_events(conn) -> int:
    """The created-blocked signature: a blocked event carrying reason='initial_status'."""
    return conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE kind = 'blocked' "
        "AND payload LIKE '%initial_status%'"
    ).fetchone()[0]


def _rows_titled(conn, title: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE title = ?", (title,)
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# Door 1 — the create seam refuses, and creates nothing for the attempt
# ---------------------------------------------------------------------------


def test_the_seam_refuses_a_blocked_birth_and_creates_nothing(kanban_home: Path) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        with pytest.raises(kb.CreatedBlockedRefused) as excinfo:
            kb.create_task(
                conn, title="parked by hand", assignee="platform-worker",
                created_by="platform-worker", initial_status="blocked",
            )
        # A ValueError, so every surface reports a validation refusal, not a crash.
        assert isinstance(excinfo.value, ValueError)

        # The attempted card does not exist, and no created-blocked row was written.
        assert _rows_titled(conn, "parked by hand") == 0
        assert _created_blocked_events(conn) == 0


def test_the_refusal_carries_the_two_legitimate_moves(kanban_home: Path) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        with pytest.raises(kb.CreatedBlockedRefused) as excinfo:
            kb.create_task(
                conn, title="parked by hand", assignee="platform-worker",
                created_by="platform-worker", initial_status="blocked",
            )
        message = str(excinfo.value)
        assert "never created blocked" in message
        assert "NOTHING WAS CREATED" in message
        # Move 1: waiting on other work is a parent edge.
        assert "parents=[...]" in message
        assert "--parent" in message
        # Move 2: a real block found mid-run is a typed runtime block.
        assert "kanban_block(kind=" in message
        assert "--kind" in message and '"<reason>"' in message


# ---------------------------------------------------------------------------
# Door 3 — the escalation: the deviation is filed against the lane BY NAME
# ---------------------------------------------------------------------------


def test_the_refusal_files_the_deviation_against_the_creating_lane(kanban_home: Path) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        with pytest.raises(kb.CreatedBlockedRefused) as excinfo:
            kb.create_task(
                conn, title="gated child work", assignee="platform-worker",
                created_by="platform-coder", initial_status="blocked",
            )
        exc = excinfo.value
        assert exc.lane == "platform-coder"
        assert exc.deviation_task_id, exc.deviation_error
        assert exc.deviation_error is None

        row = kb.get_task(conn, exc.deviation_task_id)
        assert row is not None
        # Filed ON THE BOARD, against the creating lane BY NAME.
        assert row.assignee == "platform-coder"
        assert row.title.startswith(kb.BORN_BLOCKED_DEVIATION_PREFIX)
        # Recording the enforcement action and the attempted title.
        assert "platform-coder" in row.body
        assert "gated child work" in row.body
        assert "REFUSED" in row.body
        assert kb.BORN_BLOCKED_GUARD_IDENTITY in (row.created_by or "")
        # The reprimand is a live, dispatchable card — never another parked one.
        assert row.status in {"ready", "todo"}


def test_a_repeat_attempt_collapses_onto_the_same_deviation_row(kanban_home: Path) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        ids = []
        for _ in range(3):
            with pytest.raises(kb.CreatedBlockedRefused) as excinfo:
                kb.create_task(
                    conn, title="the same retry", assignee="platform-worker",
                    created_by="platform-coder", initial_status="blocked",
                )
            ids.append(excinfo.value.deviation_task_id)
        assert ids[0] and len(set(ids)) == 1, ids
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE title LIKE ?",
            (kb.BORN_BLOCKED_DEVIATION_PREFIX + "%",),
        ).fetchone()[0] == 1


def test_an_unidentified_caller_still_gets_a_public_row(kanban_home: Path) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        with pytest.raises(kb.CreatedBlockedRefused) as excinfo:
            kb.create_task(conn, title="anonymous attempt", initial_status="blocked")
        row = kb.get_task(conn, excinfo.value.deviation_task_id)
        assert row is not None
        assert row.assignee == "default"
        assert "unidentified" in row.body


# ---------------------------------------------------------------------------
# The single door — the tool surface answers with the seam's refusal
# ---------------------------------------------------------------------------


def test_the_tool_surface_answers_with_the_refusal_and_creates_nothing(kanban_home: Path) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    out = json.loads(kt._handle_create({
        "title": "tool attempt", "assignee": "platform-worker", "initial_status": "blocked",
    }))
    err = out.get("error") or json.dumps(out)
    assert "never created blocked" in err, err
    assert "parents=[...]" in err, err
    with kbc.connect() as conn:
        assert _rows_titled(conn, "tool attempt") == 0
        assert _created_blocked_events(conn) == 0


# ---------------------------------------------------------------------------
# The kernel's own caller was migrated, not exempted
# ---------------------------------------------------------------------------


def test_the_swarm_root_is_not_born_blocked(kanban_home: Path) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_swarm as ks

    with kbc.connect() as conn:
        created = ks.create_swarm(
            conn,
            goal="prove the swarm root is not born blocked",
            workers=[ks.SwarmWorkerSpec(
                profile="platform-worker", title="w1", body="do one thing")],
            verifier_assignee="platform-worker",
            synthesizer_assignee="platform-worker",
        )
        root = kb.get_task(conn, created.root_id)
        assert root is not None
        # Same post-commit state as before (done), reached without a blocked birth.
        assert root.status == "done"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'blocked'",
            (created.root_id,),
        ).fetchone()[0] == 0
        assert _created_blocked_events(conn) == 0


# ---------------------------------------------------------------------------
# The prose revision stamp — a stale row is SELF-IDENTIFYING (card t_d152a4c9)
#
# The prose stays instructive (it is what makes the reprimand actionable). What was
# missing: a deviation row and a refusal message said nothing about WHICH revision of
# that prose wrote them, so the two rows the first live probe minted (t_35fe9e40,
# t_ae48ce83) went on teaching `hermes kanban add` and a `block --reason` flag forever,
# indistinguishable from current text. These tests pin the stamp AND its detector.
# ---------------------------------------------------------------------------


def test_the_refusal_and_the_deviation_row_stamp_the_prose_revision(kanban_home: Path) -> None:
    """Both carriers of the enforcement prose stamp the revision they were written from."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        with pytest.raises(kb.CreatedBlockedRefused) as excinfo:
            kb.create_task(
                conn, title="unversioned attempt", assignee="platform-worker",
                created_by="platform-coder", initial_status="blocked",
            )
        exc = excinfo.value
        message = str(exc)
        row = kb.get_task(conn, exc.deviation_task_id)
        assert row is not None

        # The refusal every surface returns verbatim carries THIS revision's marker.
        assert kb.BORN_BLOCKED_PROSE_MARKER in message
        assert kb.born_blocked_prose_revision(message) == kb.BORN_BLOCKED_PROSE_REVISION
        assert kb.born_blocked_prose_is_current(message)

        # The same stamp on the DURABLE row body — the record outlives the message.
        body = row.body or ""
        assert kb.BORN_BLOCKED_PROSE_MARKER in body
        assert kb.born_blocked_prose_revision(body) == kb.BORN_BLOCKED_PROSE_REVISION
        assert kb.born_blocked_prose_is_current(body)


def test_an_unmarked_row_is_detectable_as_stale() -> None:
    """The pre-fix generation carries NO marker: r1, stale by construction — never 'current'."""
    from hermes_cli import kanban_db as kb

    # Verbatim shape of the pre-fix deviation body minted 2026-09-27: instructive prose,
    # no marker, so nothing said which revision wrote it.
    pre_fix_row = (
        "**Enforcement record - a born-blocked card was REFUSED at the create path.**\n"
        "* **what to do instead:** parents=[...] on kanban_create, or "
        "`hermes kanban add --parent <id>`\n"
    )
    assert kb.born_blocked_prose_revision(pre_fix_row) is None
    assert not kb.born_blocked_prose_is_current(pre_fix_row)


def test_an_older_revision_marker_is_detectable() -> None:
    """A row written from an EARLIER stamped revision reads as stale, not as current."""
    from hermes_cli import kanban_db as kb

    older = "text written from [born-blocked prose revision: r1]"
    assert kb.born_blocked_prose_revision(older) == "r1"
    assert not kb.born_blocked_prose_is_current(older)


def test_absent_or_empty_text_is_not_current() -> None:
    from hermes_cli import kanban_db as kb

    assert kb.born_blocked_prose_revision(None) is None
    assert kb.born_blocked_prose_revision("") is None
    assert not kb.born_blocked_prose_is_current(None)


def test_the_stamp_tracks_the_prose_it_versions() -> None:
    """The rendered marker cannot drift from the revision constant (one source of truth)."""
    from hermes_cli import kanban_db as kb

    assert kb.BORN_BLOCKED_PROSE_MARKER == (
        f"[born-blocked prose revision: {kb.BORN_BLOCKED_PROSE_REVISION}]"
    )


def test_the_deviation_prose_teaches_no_verb_that_does_not_exist(kanban_home: Path) -> None:
    """The class behind this card, measured on the ROW rather than on the constant."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        with pytest.raises(kb.CreatedBlockedRefused) as excinfo:
            kb.create_task(
                conn, title="prose check", assignee="platform-worker",
                created_by="platform-coder", initial_status="blocked",
            )
        row = kb.get_task(conn, excinfo.value.deviation_task_id)
        assert row is not None
        body = row.body or ""
        # `hermes kanban add` is not a command; the verb is `create`.
        assert "kanban add" not in body
        # `block`'s reason is POSITIONAL; there is no `--reason` flag on that subparser.
        assert "--reason" not in body
        # ...and the corrected move IS present, so the removal cannot pass by silence.
        assert 'hermes kanban block <id> "<reason>" --kind <k>' in body
