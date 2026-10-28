"""Inbox -> card provenance at the DOOR (DESIGN section 4 of the seat-directory convention).

A card filed FROM a seat's inbox must carry the item's own words, or the item->card linkage
cannot be checked after the item is struck. The gate lives in ``kanban_db.create_task`` —
the one moment EVERY filing surface reaches — so these tests drive BOTH doors through it:
the CLI's ``--from-inbox`` and the ``kanban_create`` tool. Both must refuse the same
filing, for the same reason, with nothing written to the board.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_inbox as ki


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """A throwaway estate root: one seat that holds an item, and nothing else."""
    root = tmp_path / "fleet"
    seat = root / "platform" / "seat"
    seat.mkdir(parents=True)
    (seat / "TASKS.md").write_text(
        "# seat inbox\n\n"
        "| state | item | card |\n"
        "| --- | --- | --- |\n"
        "| \u25d0 | **wire the   thing** | `t_aaaa0001` |\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ki, "INBOX_ROOTS", (str(root),))
    return root


def _create(argv):
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    args = root.parse_args(["kanban", "create", *argv])
    return kc._cmd_create(args)


def _bodies():
    with kbc.connect_closing() as conn:
        return [t.body for t in kb.list_tasks(conn)]


def _tool(args):
    from tools import kanban_tools as kt

    return json.loads(kt._handle_create(args))


# --- the CLI door ---------------------------------------------------------------------

def test_the_block_is_written_first_and_quotes_the_item(kanban_home, fleet, capsys):
    rc = _create([
        "PROBE", "--from-inbox", "platform/seat", "--from-inbox-item", "wire the thing",
        "--body", "the card's own opening post",
    ])
    capsys.readouterr()
    assert rc == 0
    (body,) = _bodies()
    assert body.startswith("## Inbox item\n\ninbox: platform/seat\n> wire the thing\n"), body
    assert body.rstrip().endswith("the card's own opening post"), body


def test_a_missing_seat_file_refuses_the_filing(kanban_home, fleet, capsys):
    rc = _create([
        "PROBE", "--from-inbox", "platform/ghost", "--from-inbox-item", "wire the thing",
    ])
    err = capsys.readouterr().err
    assert rc == 2
    assert _bodies() == []
    assert "no seat inbox at" in err and "platform/ghost/TASKS.md" in err, err


def test_an_item_the_seat_no_longer_holds_is_refused(kanban_home, fleet, capsys):
    """The drop, one step earlier: the seat file is the proof, and it fails closed."""
    rc = _create([
        "PROBE", "--from-inbox", "platform/seat",
        "--from-inbox-item", "an item that was struck last week",
    ])
    err = capsys.readouterr().err
    assert rc == 2
    assert _bodies() == []
    assert "the item is not in" in err and "one step earlier" in err, err


def test_the_item_matches_through_markdown_and_whitespace_noise(kanban_home, fleet, capsys):
    rc = _create([
        "PROBE", "--from-inbox", "platform/seat", "--from-inbox-item", "wire the thing",
    ])
    capsys.readouterr()
    assert rc == 0
    (body,) = _bodies()
    # the BLOCK quotes the caller's words verbatim, not the file's markdown
    assert "> wire the thing\n" in body and "**" not in body.split("> ", 1)[1].split("\n", 1)[0]


def test_from_inbox_without_the_item_words_refuses(kanban_home, fleet, capsys):
    rc = _create(["PROBE", "--from-inbox", "platform/seat"])
    err = capsys.readouterr().err
    assert rc == 2
    assert _bodies() == []
    assert "needs --from-inbox-item" in err or "from_inbox_item" in err, err


def test_the_item_flag_alone_refuses(kanban_home, fleet, capsys):
    rc = _create(["PROBE", "--from-inbox-item", "wire the thing"])
    err = capsys.readouterr().err
    assert rc == 2
    assert _bodies() == []
    assert "from_inbox" in err, err


# --- the kernel gate is the ONE gate (both doors land on it) ---------------------------

def test_create_task_refuses_and_writes_nothing(kanban_home, fleet):
    """The gate is in the kernel, not the CLI — so a caller reaching create_task directly
    (the tool door, a DAG filer) is refused exactly as the CLI is."""
    with kbc.connect_closing() as conn:
        with pytest.raises(ki.InboxRefusal) as excinfo:
            kb.create_task(conn, title="PROBE", assignee="platform-coder",
                           from_inbox="platform/ghost", from_inbox_item="wire the thing")
        # a ValueError on purpose: every door's refusal handling already maps it
        assert isinstance(excinfo.value, ValueError)
        assert "platform/ghost/TASKS.md" in str(excinfo.value)
    assert _bodies() == []


def test_the_tool_door_reports_the_refusal_as_a_tool_error(kanban_home, fleet):
    out = _tool({
        "title": "PROBE", "assignee": "platform-coder",
        "from_inbox": "platform/seat", "from_inbox_item": "an item that was struck last week",
    })
    assert "error" in out, out
    assert "kanban_create" in out["error"] and "one step earlier" in out["error"], out
    assert _bodies() == []


def test_the_tool_door_writes_the_same_block(kanban_home, fleet):
    out = _tool({
        "title": "PROBE", "assignee": "platform-coder",
        "from_inbox": "platform/seat", "from_inbox_item": "wire the thing",
        "body": "filed from the tool",
    })
    assert out.get("ok") is True, out
    (body,) = _bodies()
    assert body.startswith("## Inbox item\n\ninbox: platform/seat\n> wire the thing\n"), body
    assert body.rstrip().endswith("filed from the tool"), body


def test_a_refiled_body_does_not_carry_two_blocks(kanban_home, fleet):
    """A retry that hands back the same opening post must not stack a second block the
    audit would then have to disambiguate."""
    once = ki.with_inbox_provenance("opening post", "platform/seat", "wire the thing")
    twice = ki.with_inbox_provenance(once, "platform/seat", "wire the thing")
    assert once == twice
    assert twice.count(ki.HEADING) == 1


def test_an_empty_item_refuses(kanban_home, fleet):
    with kbc.connect_closing() as conn:
        with pytest.raises(ki.InboxRefusal):
            kb.create_task(conn, title="PROBE", assignee="platform-coder",
                           from_inbox="platform/seat", from_inbox_item="   ")
    assert _bodies() == []


def test_a_seat_that_is_not_domain_slash_profile_refuses(kanban_home, fleet):
    with kbc.connect_closing() as conn:
        with pytest.raises(ki.InboxRefusal):
            kb.create_task(conn, title="PROBE", assignee="platform-coder",
                           from_inbox="platform", from_inbox_item="wire the thing")
    assert _bodies() == []
