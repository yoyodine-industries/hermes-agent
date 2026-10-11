"""A card fired from another card lands on the SAME board as the card that fired it.

Operator standing order, 2026-09-29. Measured the same night: platform-stl filed three
follow-on cards while working DEFCON card ``t_c4a736d3``; none landed on defcon, because
the board a new card was filed on was taken from the CALLING PROCESS (``HERMES_KANBAN_BOARD``
/ the ``current`` pointer) rather than from the card that fired it.

These cases pin the rule itself: the SOURCE card's board outranks the ambient board, and
the ambient board stays the fallback for a filing with NO source card (a loop, a cron row,
a sweep).
"""
from __future__ import annotations

import argparse

import pytest

from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

SOURCE_BOARD = "srcboard"
WRONG_BOARD = "wrongboard"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    # Two boards: the source card's board, and the WRONG ambient board the process
    # happens to be pinned to.
    kb.create_board(SOURCE_BOARD)
    kb.create_board(WRONG_BOARD)
    with kbc.connect(board=SOURCE_BOARD) as conn:
        source_id = kb.create_task(conn, title="the card that fires", assignee="platform-coder")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", WRONG_BOARD)
    return home, source_id


def _create_args(**over):
    ns = argparse.Namespace(
        title="a fired card", body=None, body_file=None, workspace=None, branch=None,
        max_runtime=None, max_retries=None, assignee="platform-coder", created_by=None,
        tenant=None, priority=0, parent=[], triage=True, idempotency_key=None,
        skills=None, model_override=None, provider_override=None, goal_mode=False,
        goal_max_turns=None, completion_contract=None, initial_status="running",
        serves=None, project=None, json=True, board=None,
    )
    for key, value in over.items():
        setattr(ns, key, value)
    return ns


def _ids_on(board):
    with kbc.connect(board=board) as conn:
        return {t.id for t in kb.list_tasks(conn)}


# --- the resolver itself ------------------------------------------------------------------

def test_resolver_prefers_the_source_card_board(kanban_home):
    _home, source_id = kanban_home
    assert kb.source_board_for_task(source_id) == SOURCE_BOARD
    assert kb.board_for_fired_card(None, source_task_id=source_id) == SOURCE_BOARD


def test_resolver_keeps_an_explicit_board(kanban_home):
    _home, source_id = kanban_home
    assert kb.board_for_fired_card(WRONG_BOARD, source_task_id=source_id) == WRONG_BOARD


def test_resolver_returns_none_without_a_source_card(kanban_home):
    """A filing with no source card (a loop, a cron row, a sweep) keeps the ambient chain."""
    assert kb.board_for_fired_card(None) is None
    assert kb.source_board_for_task("t_00000000dead") is None


# --- the firing path: the CLI that files the card ----------------------------------------

def test_cli_create_from_a_card_lands_on_the_source_board(kanban_home, monkeypatch):
    _home, source_id = kanban_home
    monkeypatch.setenv("HERMES_KANBAN_TASK", source_id)
    assert kb_cli._cmd_create(_create_args()) == 0
    on_source, on_wrong = _ids_on(SOURCE_BOARD), _ids_on(WRONG_BOARD)
    fired = on_source - {source_id}
    assert len(fired) == 1, "the fired card must land on the source card's board"
    assert on_wrong == set(), "nothing may land on the ambient board"


def test_cli_create_without_a_source_card_still_uses_the_ambient_board(kanban_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert kb_cli._cmd_create(_create_args()) == 0
    assert _ids_on(WRONG_BOARD) != set()


def test_cli_explicit_board_still_wins(kanban_home, monkeypatch):
    _home, source_id = kanban_home
    monkeypatch.setenv("HERMES_KANBAN_TASK", source_id)
    assert kb_cli._cmd_create(_create_args(board=WRONG_BOARD)) == 0
    assert _ids_on(WRONG_BOARD) != set()
    assert _ids_on(SOURCE_BOARD) == {source_id}


# --- the firing path: the agent tool that files the card ---------------------------------

def test_tool_create_from_a_card_lands_on_the_source_board(kanban_home, monkeypatch):
    import json

    from tools import kanban_tools as kt

    _home, source_id = kanban_home
    monkeypatch.setenv("HERMES_KANBAN_TASK", source_id)
    payload = json.loads(kt._handle_create({
        "title": "fired by the tool", "assignee": "platform-coder",
        "triage": True}))
    assert payload.get("ok") is True, payload
    assert kb.source_board_for_task(payload["task_id"]) == SOURCE_BOARD
    assert _ids_on(WRONG_BOARD) == set()


# --- a filing never MINTS a board: an unregistered slug is refused, loudly -----------------
#
# Measured 2026-09-29: the lane display handle ``yoyodine-majordomo`` was not a profile id
# and not a board, yet three real cards landed on it because the creating connect() mints a
# store for any slug-shaped string. The phantom carried only ``kanban.db`` — no ``board.json``
# — so ``board_is_registered`` (create_board always writes the metadata) is the probe that
# tells a REGISTERED board from a mint, and the filing refuses the mint.

PHANTOM = "yoyodine-majordomo"  # a lane display handle, deliberately slug-shaped


def test_board_is_registered_distinguishes_a_mint_from_a_board(kanban_home):
    assert kb.board_is_registered(kb.DEFAULT_BOARD) is True
    assert kb.board_is_registered(SOURCE_BOARD) is True
    # A directory carrying only a store is what a filing MINTs; it is not a board.
    stub = kb.board_dir("minted-stub")
    stub.mkdir(parents=True)
    (stub / "kanban.db").write_bytes(b"")
    assert kb.board_exists("minted-stub") is True, "the looser probe admits the mint"
    assert kb.board_is_registered("minted-stub") is False


def test_resolver_refuses_an_unregistered_name_shaped_board(kanban_home):
    _home, source_id = kanban_home
    with pytest.raises(kb.BoardResolutionError) as excinfo:
        kb.board_for_fired_card(PHANTOM, source_task_id=source_id)
    message = str(excinfo.value)
    assert PHANTOM in message
    assert "registered" in message and "board.json" in message
    assert "never" in message and "mints a board" in message
    assert "hermes kanban boards create" in message
    assert not kb.board_dir(PHANTOM).exists(), "the refusal must not mint the board"


def test_resolver_refuses_a_minted_stub(kanban_home):
    _home, source_id = kanban_home
    stub = kb.board_dir("minted-stub")
    stub.mkdir(parents=True)
    (stub / "kanban.db").write_bytes(b"")
    with pytest.raises(kb.BoardResolutionError):
        kb.board_for_fired_card("minted-stub", source_task_id=source_id)


def test_cli_create_with_an_unregistered_board_is_refused_and_mints_nothing(kanban_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    before = {b["slug"] for b in kb.list_boards()}
    with pytest.raises(kb.BoardResolutionError):
        kb_cli._cmd_create(_create_args(board=PHANTOM))
    assert not kb.board_dir(PHANTOM).exists(), "no board directory may be created"
    assert {b["slug"] for b in kb.list_boards()} == before


def test_tool_create_with_an_unregistered_board_is_refused(kanban_home, monkeypatch):
    from tools import kanban_tools as kt

    monkeypatch.setenv("HERMES_KANBAN_TASK", kanban_home[1])
    result = kt._handle_create({
        "title": "must not land", "assignee": "platform-coder", "board": PHANTOM})
    assert "refusing to file on board" in result
    assert PHANTOM in result
    assert not kb.board_dir(PHANTOM).exists(), "the tool must not mint the board either"
    assert kb.source_board_for_task(kanban_home[1]) == SOURCE_BOARD


def test_cli_create_with_no_board_and_no_source_mints_nothing(kanban_home, monkeypatch):
    """A source-less filing falls to the documented default, never to a new board."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    before = {b["slug"] for b in kb.list_boards()}
    assert kb_cli._cmd_create(_create_args()) == 0
    assert {b["slug"] for b in kb.list_boards()} == before
    assert _ids_on(kb.DEFAULT_BOARD) != set(), "it landed on the documented default"


# --- the READ/tool door: ``_board()`` refuses an unregistered slug BEFORE connecting -------
#
# The filing path refuses an unregistered name, but the tool surface had a second door into
# a store: ``tools.kanban_tools._board()`` handed an explicit ``board`` straight to the
# creating ``kanban_db_connect.connect()``, which mints ``boards/<slug>/`` for any slug-shaped
# string. So a READ tool (``kanban_attachments``) could still resurrect the phantom. The fix
# resolves the explicit slug through ``kb.assert_board_registered`` — the ONE chokepoint, and
# the ONE shared message (:func:`kb.unregistered_board_refusal`).

def test_board_resolver_refuses_an_unregistered_name(kanban_home):
    """``assert_board_registered`` names the slug and refuses it, minting nothing."""
    with pytest.raises(kb.BoardResolutionError) as excinfo:
        kb.assert_board_registered(PHANTOM)
    message = str(excinfo.value)
    assert PHANTOM in message
    assert "registered" in message and "board.json" in message
    assert "mints a board" in message and "never" in message
    assert "hermes kanban boards create" in message
    assert not kb.board_dir(PHANTOM).exists(), "the refusal must not mint the board"


def test_board_resolver_keeps_the_ambient_chain_for_none(kanban_home):
    """``None``/empty means "the active board", so the ambient chain is untouched."""
    assert kb.assert_board_registered(None) is None
    assert kb.assert_board_registered("") is None


def test_board_resolver_returns_the_normed_slug_for_a_registered_board(kanban_home):
    assert kb.assert_board_registered(SOURCE_BOARD.upper()) == SOURCE_BOARD
    assert kb.assert_board_registered(kb.DEFAULT_BOARD) == kb.DEFAULT_BOARD


def test_tool_board_door_refuses_an_unregistered_name(kanban_home):
    """The tool resolver door refuses before ``connect()`` — nothing is minted."""
    from tools import kanban_tools as kt

    with pytest.raises(kb.BoardResolutionError) as excinfo:
        with kt._board(PHANTOM):
            pass
    assert PHANTOM in str(excinfo.value)
    assert not kb.board_dir(PHANTOM).exists(), "the tool door must not mint the board"


def test_tool_refusal_uses_the_same_message_as_the_filing_path(kanban_home):
    """ONE message, two doors: a duplicated string would be a drift bug."""
    from tools import kanban_tools as kt

    _home, source_id = kanban_home
    with pytest.raises(kb.BoardResolutionError) as filing:
        kb.board_for_fired_card(PHANTOM, source_task_id=source_id)
    with pytest.raises(kb.BoardResolutionError) as door:
        with kt._board(PHANTOM):
            pass
    assert str(door.value) == str(filing.value)


def test_read_tool_refuses_an_unregistered_board_and_mints_nothing(kanban_home, monkeypatch):
    """The exact tool call from the field: ``kanban_attachments`` cannot mint the phantom."""
    from tools import kanban_tools as kt

    _home, source_id = kanban_home
    monkeypatch.setenv("HERMES_KANBAN_TASK", source_id)
    before = {b["slug"] for b in kb.list_boards()}
    result = kt._handle_attachments({"task_id": source_id, "board": PHANTOM})
    assert "refusing to file on board" in result, result
    assert PHANTOM in result
    assert not kb.board_dir(PHANTOM).exists(), "the read tool must not mint the board"
    assert {b["slug"] for b in kb.list_boards()} == before, "list_boards gains no slug"


def test_read_tool_still_accepts_a_registered_board(kanban_home, monkeypatch):
    """The guard is a refusal, not a wall: a registered board reads exactly as before."""
    from tools import kanban_tools as kt

    _home, source_id = kanban_home
    monkeypatch.setenv("HERMES_KANBAN_TASK", source_id)
    result = kt._handle_attachments({"task_id": source_id, "board": SOURCE_BOARD})
    assert result.startswith("{") and '"ok": true' in result, result


def test_board_resolver_refuses_a_minted_stub_too(kanban_home):
    """Stricter than ``board_exists``: a ``kanban.db`` with no ``board.json`` is a mint."""
    stub = kb.board_dir("minted-stub")
    stub.mkdir(parents=True)
    (stub / "kanban.db").write_bytes(b"")
    assert kb.board_exists("minted-stub") is True
    with pytest.raises(kb.BoardResolutionError):
        kb.assert_board_registered("minted-stub")
