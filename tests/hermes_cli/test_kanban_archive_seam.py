"""The archive seam: no anonymous archive, and the rehome link written BEFORE the flip.

Three defects measured on the ``platform`` board on 2026-10-29:

1. ``archive_task`` recorded NOTHING about why. 8,382 of 9,629 ``archived``
   events carried a ``null`` payload, so an archived card cannot be told apart
   from one archived by accident — and the record that would have caught 176
   items stranded by a closed card was never written.
2. ``hermes kanban archive <id> --reason ...`` — the form the estate's own remedy
   text instructs — was not a flag at all.
3. The link that says *where the work went* lived only in the estate's own
   database and was written AFTER the card flipped, so a crash between the two
   left a closed card with no record of its successor.

The contract under test (RULING t_8dd35753 §2):

* C1 — an archive REQUIRES a reason from a closed enum and records
  ``reason/actor/board/card/destination/key`` in the ``archived`` event.
* C2 — for ``rehomed``/``duplicate`` a destination is required; the
  ``rehome-<source_board>-<source_card>`` key is written on the destination and
  READ BACK before the source flips.
* C3 — a card whose body carries an ``sdlc/v1`` block at a non-terminal stage may
  not be archived without a destination (``reason="operator"`` is the override).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
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


def _receipt(conn, task_id):
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'archived' "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    assert row is not None, f"no archived event for {task_id}"
    assert row["payload"], "the archived event must carry a receipt, not null"
    return json.loads(row["payload"])


def _sdlc_body(*, stage="building", item="sdlc-platform-2f0dc6"):
    return (
        "Filed by the SDLC flow.\n\n"
        "<!-- sdlc/v1\n"
        f"item: {item}          domain: platform          role: coder\n"
        f"board: platform          lane: platform          stage: {stage}\n"
        "-->\n"
    )


# ---------------------------------------------------------------------------
# C1 — no anonymous archive
# ---------------------------------------------------------------------------

def test_archive_without_a_reason_is_refused_and_changes_nothing(conn):
    t = kb.create_task(conn, title="x")
    with pytest.raises(kb.ArchiveRefused) as exc:
        kb.archive_task(conn, t)
    assert exc.value.code == "reason_missing"
    assert exc.value.card == t
    # The refusal names the enum, so the caller can act without reading code.
    assert "duplicate" in str(exc.value) and "rehomed" in str(exc.value)
    assert kb.get_task(conn, t).status != "archived"
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'archived'", (t,)
    ).fetchone()[0] == 0


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_a_blank_reason_is_a_missing_reason(conn, blank):
    t = kb.create_task(conn, title="x")
    with pytest.raises(kb.ArchiveRefused) as exc:
        kb.archive_task(conn, t, reason=blank)
    assert exc.value.code == "reason_missing"


def test_a_reason_outside_the_enum_is_refused(conn):
    t = kb.create_task(conn, title="x")
    with pytest.raises(kb.ArchiveRefused) as exc:
        kb.archive_task(conn, t, reason="because I said so")
    assert exc.value.code == "reason_invalid"
    assert kb.get_task(conn, t).status != "archived"


def test_the_archived_event_carries_the_receipt(conn):
    t = kb.create_task(conn, title="x")
    assert kb.archive_task(conn, t, reason="generated-moot", actor="platform-coder") is True

    receipt = _receipt(conn, t)
    assert receipt == {
        "reason": "generated-moot",
        "actor": "platform-coder",
        "board": "default",
        "card": t,
        "destination": None,
        "key": None,
    }


def test_the_reason_is_normalised_to_its_token(conn):
    t = kb.create_task(conn, title="x")
    kb.archive_task(conn, t, reason="  SUPERSEDED ")
    assert _receipt(conn, t)["reason"] == "superseded"


# ---------------------------------------------------------------------------
# C2 — link before unlink
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reason", ["rehomed", "duplicate"])
def test_a_successor_reason_without_a_destination_is_refused(conn, reason):
    t = kb.create_task(conn, title="x")
    with pytest.raises(kb.ArchiveRefused) as exc:
        kb.archive_task(conn, t, reason=reason)
    assert exc.value.code == "destination_missing"
    assert kb.get_task(conn, t).status != "archived"


def test_malformed_destination_is_refused(conn):
    t = kb.create_task(conn, title="x")
    for bad in ("platform", "/t_abc", "platform/"):
        with pytest.raises(kb.ArchiveRefused) as exc:
            kb.archive_task(conn, t, reason="rehomed", destination=bad)
        assert exc.value.code == "destination_missing"


def test_rehome_writes_the_key_on_the_destination_and_records_it(conn):
    src = kb.create_task(conn, title="the one that gets superseded")
    dest = kb.create_task(conn, title="the one that inherits")
    assert kb.archive_task(conn, src, reason="rehomed", actor="platform-stl",
                           destination=f"default/{dest}") is True

    key = f"rehome-default-{src}"
    assert conn.execute(
        "SELECT idempotency_key FROM tasks WHERE id = ?", (dest,)
    ).fetchone()[0] == key
    receipt = _receipt(conn, src)
    assert receipt["reason"] == "rehomed"
    assert receipt["destination"] == {"board": "default", "card": dest}
    assert receipt["key"] == key


def test_cross_board_destination_carries_the_key_in_its_own_store(kanban_home, conn):
    """The destination can live on ANOTHER board — a different file on disk."""
    kb.create_board("platform")
    src = kb.create_task(conn, title="stranded on default")
    with kbc.connect(board="platform") as other:
        dest = kb.create_task(other, title="inherits on platform")

    assert kb.archive_task(conn, src, reason="rehomed",
                           destination=f"platform/{dest}") is True

    with kbc.connect(board="platform") as other:
        assert other.execute(
            "SELECT idempotency_key FROM tasks WHERE id = ?", (dest,)
        ).fetchone()[0] == f"rehome-default-{src}"
    # The receipt's board token is the SOURCE board, i.e. where the archived card was.
    assert _receipt(conn, src)["board"] == "default"
    assert kb.get_task(conn, src).status == "archived"


def test_a_missing_destination_card_is_refused(conn):
    t = kb.create_task(conn, title="x")
    with pytest.raises(kb.ArchiveRefused) as exc:
        kb.archive_task(conn, t, reason="rehomed", destination="default/t_nope")
    assert exc.value.code == "destination_missing"
    assert kb.get_task(conn, t).status != "archived"


def test_an_archived_destination_is_refused(conn):
    t = kb.create_task(conn, title="x")
    dead = kb.create_task(conn, title="already gone")
    kb.archive_task(conn, dead, reason="superseded")
    with pytest.raises(kb.ArchiveRefused) as exc:
        kb.archive_task(conn, t, reason="rehomed", destination=f"default/{dead}")
    assert exc.value.code == "destination_archived"
    assert kb.get_task(conn, t).status != "archived"


def test_a_destination_that_already_carries_another_key_is_refused(conn):
    """One card carries one key: a second rehome must not silently re-point it."""
    src = kb.create_task(conn, title="x")
    dest = kb.create_task(conn, title="y", idempotency_key="deploy-train-7")
    with pytest.raises(kb.ArchiveRefused) as exc:
        kb.archive_task(conn, src, reason="rehomed", destination=f"default/{dest}")
    assert exc.value.code == "destination_bearing_other_key"
    assert conn.execute(
        "SELECT idempotency_key FROM tasks WHERE id = ?", (dest,)
    ).fetchone()[0] == "deploy-train-7"
    assert kb.get_task(conn, src).status != "archived"


def test_a_rerun_adopts_the_key_a_crashed_attempt_left_behind(kanban_home, conn):
    """The crash window's benign half: the destination already names the source."""
    kb.create_board("platform")
    src = kb.create_task(conn, title="x")
    with kbc.connect(board="platform") as other:
        dest = kb.create_task(other, title="y")
        other.execute("UPDATE tasks SET idempotency_key = ? WHERE id = ?",
                      (f"rehome-default-{src}", dest))
    assert kb.archive_task(conn, src, reason="rehomed",
                           destination=f"platform/{dest}") is True
    assert kb.get_task(conn, src).status == "archived"


def test_a_failure_between_the_link_and_the_flip_leaves_the_source_live(kanban_home, conn,
                                                                      monkeypatch):
    """DoD: the injection point the sealed seam must survive.

    The link half is committed on the destination's own store; the flip then dies.
    The source must still be LIVE (archiving it is what the operator saw happen,
    so it must not be half-done) and the destination must already bear the key —
    which is a link that over-claims, the benign direction, and is why a re-run
    adopts rather than refuses.
    """
    kb.create_board("platform")
    src = kb.create_task(conn, title="x")
    with kbc.connect(board="platform") as other:
        dest = kb.create_task(other, title="y")

    def _boom(*_a, **_kw):
        raise RuntimeError("injected: the process died between the write-back and the flip")

    real_flip = kb._archive_flip
    monkeypatch.setattr(kb, "_archive_flip", _boom)
    with pytest.raises(RuntimeError):
        kb.archive_task(conn, src, reason="rehomed", destination=f"platform/{dest}")

    assert kb.get_task(conn, src).status != "archived"
    with kbc.connect(board="platform") as other:
        assert other.execute(
            "SELECT idempotency_key FROM tasks WHERE id = ?", (dest,)
        ).fetchone()[0] == f"rehome-default-{src}"

    # The re-run (same act) adopts the key it already wrote and completes. Restore
    # ONLY this symbol — ``monkeypatch.undo()`` would also undo the fixture's
    # HERMES_HOME sandbox and point the store back at the operator's real board.
    monkeypatch.setattr(kb, "_archive_flip", real_flip)
    assert kb.archive_task(conn, src, reason="rehomed",
                           destination=f"platform/{dest}") is True
    assert kb.get_task(conn, src).status == "archived"


# ---------------------------------------------------------------------------
# C3 — a bound item may not be orphaned (fail-closed proxy on the card body)
# ---------------------------------------------------------------------------

def test_sdlc_tag_record_reads_the_column_spelling():
    record = kb._sdlc_tag_record(_sdlc_body(stage="building"))
    assert record["item"] == "sdlc-platform-2f0dc6"
    assert record["stage"] == "building"
    assert record["board"] == "platform"


def test_sdlc_tag_record_reads_the_glued_spelling():
    body = "<!-- sdlc/v1\nitem:sdlc-x domain:platform stage:building\n-->\n"
    record = kb._sdlc_tag_record(body)
    assert record == {"item": "sdlc-x", "domain": "platform", "stage": "building"}


def test_sdlc_tag_record_is_none_without_a_block():
    assert kb._sdlc_tag_record("just a card") is None
    assert kb._sdlc_tag_record(None) is None


def test_a_card_bound_to_an_open_item_is_refused_without_a_destination(conn):
    t = kb.create_task(conn, title="bound", body=_sdlc_body(stage="building"))
    with pytest.raises(kb.ArchiveRefused) as exc:
        kb.archive_task(conn, t, reason="superseded")
    assert exc.value.code == "sdlc_bound_needs_destination"
    assert exc.value.items == ("sdlc-platform-2f0dc6",)
    # The refusal NAMES the item: a sweep reports it without re-reading the body.
    assert "sdlc-platform-2f0dc6" in str(exc.value)
    assert kb.get_task(conn, t).status != "archived"


def test_a_bound_card_passes_with_a_destination(conn):
    t = kb.create_task(conn, title="bound", body=_sdlc_body(stage="building"))
    dest = kb.create_task(conn, title="inherits")
    assert kb.archive_task(conn, t, reason="rehomed", destination=f"default/{dest}") is True
    assert kb.get_task(conn, t).status == "archived"


def test_a_bound_card_passes_on_operator_authority(conn):
    t = kb.create_task(conn, title="bound", body=_sdlc_body(stage="building"))
    assert kb.archive_task(conn, t, reason="operator") is True
    assert _receipt(conn, t)["reason"] == "operator"


def test_a_closed_item_does_not_hold_its_card(conn):
    t = kb.create_task(conn, title="bound", body=_sdlc_body(stage="closed"))
    assert kb.archive_task(conn, t, reason="superseded") is True


def test_an_unreadable_stage_fails_closed(conn):
    """A block whose stage cannot be read is NOT a terminal one."""
    t = kb.create_task(conn, title="bound",
                       body="<!-- sdlc/v1\nitem: sdlc-x domain: platform\n-->\n")
    with pytest.raises(kb.ArchiveRefused) as exc:
        kb.archive_task(conn, t, reason="superseded")
    assert exc.value.code == "sdlc_bound_needs_destination"


def test_a_card_without_a_block_is_untouched_by_the_guard(conn):
    t = kb.create_task(conn, title="plain")
    assert kb.archive_task(conn, t, reason="never-started") is True


# ---------------------------------------------------------------------------
# The CLI door (defect 2: the instructed form was not a flag)
# ---------------------------------------------------------------------------

def _archive_ns(*task_ids, reason=None, destination=None, actor=None, purge=None):
    return argparse.Namespace(
        task_ids=list(task_ids), purge_ids=purge, reason=reason,
        destination=destination, actor=actor,
    )


def test_cli_archive_without_a_reason_refuses_with_the_code(kanban_home, conn, capsys):
    t = kb.create_task(conn, title="x")
    rc = kb_cli._cmd_archive(_archive_ns(t))
    err = capsys.readouterr().err
    assert rc == 2
    assert "reason_missing" in err
    assert "duplicate" in err            # the enum, so the human can pick one
    assert kb.get_task(conn, t).status != "archived"


def test_cli_bulk_archive_without_a_reason_archives_nothing(kanban_home, conn, capsys):
    """The 163-card batch shape: refuse the whole selection, do not half-apply it."""
    ids = [kb.create_task(conn, title=f"x{i}") for i in range(3)]
    rc = kb_cli._cmd_archive(_archive_ns(*ids))
    assert rc == 2
    assert "reason_missing" in capsys.readouterr().err
    for tid in ids:
        assert kb.get_task(conn, tid).status != "archived"


def test_cli_archive_rejects_an_unknown_reason(kanban_home, conn, capsys):
    t = kb.create_task(conn, title="x")
    rc = kb_cli._cmd_archive(_archive_ns(t, reason="tidying up"))
    assert rc == 2
    assert "reason_invalid" in capsys.readouterr().err
    assert kb.get_task(conn, t).status != "archived"


def test_cli_archive_reason_flag_and_destination_write_the_key(kanban_home, conn, capsys):
    """The exact form the estate's remedy text instructs."""
    src = kb.create_task(conn, title="x")
    dest = kb.create_task(conn, title="y")
    rc = kb_cli._cmd_archive(_archive_ns(
        src, reason="rehomed", destination=f"default/{dest}", actor="platform-coder"))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert f"Archived {src} (rehomed)" in out
    assert conn.execute(
        "SELECT idempotency_key FROM tasks WHERE id = ?", (dest,)
    ).fetchone()[0] == f"rehome-default-{src}"
    assert _receipt(conn, src)["actor"] == "platform-coder"


def test_cli_archive_reads_a_trailing_reason_like_promote(kanban_home, conn, capsys):
    t = kb.create_task(conn, title="x")
    rc = kb_cli._cmd_archive(_archive_ns(t, "superseded"))
    assert rc == 0, capsys.readouterr().err
    assert _receipt(conn, t)["reason"] == "superseded"


def test_cli_archive_reports_a_refusal_per_card_and_fails_the_run(kanban_home, conn, capsys):
    ok_card = kb.create_task(conn, title="ok")
    bound = kb.create_task(conn, title="bound", body=_sdlc_body(stage="building"))
    rc = kb_cli._cmd_archive(_archive_ns(ok_card, bound, reason="superseded"))
    captured = capsys.readouterr()
    assert rc == 1
    assert f"Archived {ok_card} (superseded)" in captured.out
    assert "sdlc_bound_needs_destination" in captured.err
    assert kb.get_task(conn, ok_card).status == "archived"
    assert kb.get_task(conn, bound).status != "archived"
