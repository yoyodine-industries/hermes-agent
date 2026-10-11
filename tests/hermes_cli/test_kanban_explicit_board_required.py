"""Every card must NAME its board: `default` is never a filing destination.

Operator mandate, 2026-10-10 (card t_b56e48c1): *"nothing should ever be carded on
`default` because every card should require an explicit board."*

Measured defect this pins: `default` is the TERMINAL fallback of the board-resolution
chain (`_board_path` -> `get_current_board()` -> `DEFAULT_BOARD`), so a create that
named no board landed there silently — nobody chose the estate. The worked example is
`t_09523a19`: its creation event carries `creator_task_id: t_0da8148b`, and the child
landed on `default` because its PARENT's worker ran there.

Round-1 review (platform-stl) found the first cut tested PROVENANCE, not DESTINATION:
it allowed every route whose RESOLVED destination was still `default` — the store pin
`HERMES_KANBAN_DB`, a source card ON `default`, `HERMES_KANBAN_BOARD=default`, and an
explicit `board="default"`. Those four are the cases below (a)-(d); each asserts that
NOTHING lands on `default`.

What these cases pin down:

* a boardless `hermes kanban create` exits NON-ZERO, names the missing board, and
  writes NO row and NO store;
* the ``<root>/kanban/current`` pointer is NOT sufficient — the resolver still honours
  it (asserted, so the case cannot pass on a broken fixture) and the FILING is refused
  anyway (ruling platform-stl R1);
* `default` is not a destination by ANY route: the store pin, the source board, the
  env board name, or an explicit `board="default"` (ruling platform-stl R2);
* every genuine explicit source still files: a `board=` name, the in-process `--board`
  scope, `HERMES_KANBAN_BOARD`, `HERMES_KANBAN_DB` (the store pin the dispatcher
  injects into a spawned worker) and the source card's own board;
* the `kanban_create` TOOL is the second door into the same seam and refuses with the
  SAME text (one definition, two doors — a copy would drift).
"""
from __future__ import annotations

import argparse
import sqlite3

import pytest

from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

BOARD_A = "boarda"
BOARD_B = "boardb"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with two registered boards and NO ambient board named."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    # A dispatcher-spawned worker shell pins these; a test must not touch a live board.
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(var, raising=False)
    kb.create_board(BOARD_A)
    kb.create_board(BOARD_B)
    return home


def _create_args(**over):
    ns = argparse.Namespace(
        title="a filed card", body=None, body_file=None, workspace=None, branch=None,
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


def _default_ids(home):
    """Rows on the DEFAULT store, read WITHOUT opening it (so a probe mints nothing)."""
    db = home / "kanban.db"
    if not db.exists():
        return set()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {row[0] for row in conn.execute("select id from tasks")}
    finally:
        conn.close()


def _all_ids(home):
    found = set(_default_ids(home))
    for board in (BOARD_A, BOARD_B):
        found |= _ids_on(board)
    return found


def _pin_at_default_store(monkeypatch, home):
    """``HERMES_KANBAN_DB`` pinned at the DEFAULT store — the fallback in disguise."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))


def _board_ids(home, board):
    """Rows on ``board``'s own store, read WITHOUT the resolver (a pin misdirects it)."""
    db = home / "kanban" / "boards" / board / "kanban.db"
    if not db.exists():
        return set()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {row[0] for row in conn.execute("select id from tasks")}
    finally:
        conn.close()


# --- the refusal itself --------------------------------------------------------------------

def test_boardless_create_is_refused_and_writes_no_row(kanban_home, capsys):
    before = _all_ids(kanban_home)
    rc = kb_cli._cmd_create(_create_args())
    err = capsys.readouterr().err
    assert rc != 0, "a filing that names no board must exit non-zero"
    assert "names no board" in err, err
    assert "board" in err and "create" in err, err
    # NOTHING was filed, on any board.
    assert _all_ids(kanban_home) == before


def test_no_store_is_minted_for_a_refused_filing(kanban_home):
    """The refusal fires BEFORE the connect, so it cannot mint a store either."""
    rc = kb_cli._cmd_create(_create_args())
    assert rc != 0
    assert not (kanban_home / "kanban.db").exists(), \
        "a refused filing must not create the default store"


def test_the_current_pointer_is_not_sufficient(kanban_home, monkeypatch):
    """The ambient ``current`` pointer is a property of the PROCESS, not of the work.

    Asserted first that the resolver still honours it — otherwise this case would pass
    on a fixture that simply cannot see the pointer.
    """
    pointer = kanban_home / "kanban" / "current"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(BOARD_B + "\n", encoding="utf-8")
    assert kb.get_current_board() == BOARD_B, "fixture: the pointer must resolve first"
    assert pointer.exists()
    before = _all_ids(kanban_home)
    assert kb_cli._cmd_create(_create_args()) != 0
    assert _all_ids(kanban_home) == before
    assert _ids_on(BOARD_B) == set(), "the pointer must not choose the estate"


def test_the_refusal_text_names_the_way_out():
    text = kb.missing_filing_board_refusal()
    assert "--board" in text
    assert "HERMES_KANBAN_BOARD" in text
    # The refusal is ONE definition; both doors read it.
    assert kb.filing_board_refusal(None) == text


# --- R2: `default` is not a destination, by ANY route ---------------------------------------

def test_a_pin_onto_the_default_store_is_refused(kanban_home, monkeypatch):
    """(a) ``HERMES_KANBAN_DB`` pointing at the DEFAULT store is the fallback in disguise.

    This is the pin the dispatcher injects into every spawned worker — the exact route
    by which ``t_09523a19``'s child landed on ``default``.
    """
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kanban_home / "kanban.db"))
    before = _default_ids(kanban_home)
    rc = kb_cli._cmd_create(_create_args())
    assert rc != 0, "a pin onto the default store names no board"
    assert _default_ids(kanban_home) == before
    assert _ids_on(BOARD_A) == set() and _ids_on(BOARD_B) == set()


def test_a_source_card_on_default_is_not_a_licence(kanban_home, monkeypatch):
    """(b) A card ON ``default`` firing a child must NOT inherit ``default`` as its estate."""
    with kbc.connect(board=kb.DEFAULT_BOARD) as conn:
        source_id = kb.create_task(conn, title="the card on default",
                                   assignee="platform-coder")
    assert kb.source_board_for_task(source_id) == kb.DEFAULT_BOARD, "fixture"
    monkeypatch.setenv("HERMES_KANBAN_TASK", source_id)
    before = _default_ids(kanban_home)
    rc = kb_cli._cmd_create(_create_args())
    assert rc != 0, "a source card on default is not a filing destination"
    assert _default_ids(kanban_home) == before


def test_the_env_board_name_default_is_refused(kanban_home, monkeypatch):
    """(c) ``HERMES_KANBAN_BOARD=default`` names the fallback, not an estate."""
    monkeypatch.setenv("HERMES_KANBAN_BOARD", kb.DEFAULT_BOARD)
    before = _default_ids(kanban_home)
    rc = kb_cli._cmd_create(_create_args())
    assert rc != 0
    assert _default_ids(kanban_home) == before


def test_an_explicit_default_board_is_refused(kanban_home):
    """(d) An explicit ``board="default"`` is still not a destination."""
    before = _default_ids(kanban_home)
    rc = kb_cli._cmd_create(_create_args(board=kb.DEFAULT_BOARD))
    assert rc != 0
    assert _default_ids(kanban_home) == before


# --- round 2: the PIN decides — an env board NAME must not judge a pinned filing ------------
#
# Round-2 review (platform-stl, 2026-10-11): with ``HERMES_KANBAN_DB`` pinned at the DEFAULT
# store AND ``HERMES_KANBAN_BOARD`` naming a REGISTERED non-default board, the guard fell
# through to the env name and ALLOWED — while the creating ``connect()`` opens the PIN, so the
# row landed on ``default`` anyway (measured rc=0, +1 row). The destination is the pin (the
# resolver never consults the env name while a pin decides), so each case below asserts NO row
# on ``default`` and no row anywhere off the pin.

def test_a_pin_at_default_with_an_env_board_name_is_refused(kanban_home, monkeypatch):
    """The pin IS the destination; a registered env name must not save the filing."""
    _pin_at_default_store(monkeypatch, kanban_home)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", BOARD_B)
    before = _default_ids(kanban_home)
    assert kb_cli._cmd_create(_create_args()) != 0, \
        "the pin decides the destination; the env name must not override it"
    assert _default_ids(kanban_home) == before
    assert _board_ids(kanban_home, BOARD_A) == set()
    assert _board_ids(kanban_home, BOARD_B) == set()


def test_a_pin_at_default_with_an_env_board_name_is_refused_when_fenced(kanban_home, monkeypatch):
    """Same inside a dispatcher-owned worker (``HERMES_KANBAN_TASK`` set → intent is pinned)."""
    _pin_at_default_store(monkeypatch, kanban_home)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", BOARD_B)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_not_a_card")
    before = _default_ids(kanban_home)
    assert kb_cli._cmd_create(_create_args()) != 0
    assert _default_ids(kanban_home) == before
    assert _board_ids(kanban_home, BOARD_A) == set()
    assert _board_ids(kanban_home, BOARD_B) == set()


def test_a_pin_at_default_with_an_env_board_name_and_a_source_card_is_refused(
        kanban_home, monkeypatch):
    """A source card's board does not save a filing the pin lands on ``default`` either."""
    with kbc.connect(board=BOARD_A) as conn:
        source_id = kb.create_task(conn, title="the card that fires",
                                   assignee="platform-coder")
    assert _board_ids(kanban_home, BOARD_A) == {source_id}, "fixture: source on BOARD_A"
    _pin_at_default_store(monkeypatch, kanban_home)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", BOARD_B)
    monkeypatch.setenv("HERMES_KANBAN_TASK", source_id)
    before = _default_ids(kanban_home)
    assert kb_cli._cmd_create(_create_args()) != 0
    assert _default_ids(kanban_home) == before
    assert _board_ids(kanban_home, BOARD_A) == {source_id}, "the child must not land here"
    assert _board_ids(kanban_home, BOARD_B) == set()


def test_a_pin_at_default_with_an_unregistered_env_board_name_is_refused(kanban_home, monkeypatch):
    """An env name that is no board is not a destination — the resolver falls through it."""
    _pin_at_default_store(monkeypatch, kanban_home)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "not-a-registered-board")
    before = _default_ids(kanban_home)
    assert kb_cli._cmd_create(_create_args()) != 0
    assert _default_ids(kanban_home) == before
    assert _board_ids(kanban_home, BOARD_A) == set()
    assert _board_ids(kanban_home, BOARD_B) == set()


def test_an_unfenced_explicit_board_still_outranks_a_pin_at_default(kanban_home, monkeypatch):
    """Counterpart: an UNFENCED typed name keeps deciding (it is intent, not a fallback)."""
    _pin_at_default_store(monkeypatch, kanban_home)
    assert kb_cli._cmd_create(_create_args(board=BOARD_A)) == 0
    assert _board_ids(kanban_home, BOARD_A) != set()
    assert _default_ids(kanban_home) == set()


def test_a_fenced_explicit_board_follows_the_pin_onto_default_and_refuses(kanban_home, monkeypatch):
    """A fenced worker's typed name resolves THROUGH the pin, so the pin is the destination."""
    _pin_at_default_store(monkeypatch, kanban_home)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_not_a_card")
    assert kb_cli._cmd_create(_create_args(board=BOARD_A)) != 0
    assert _default_ids(kanban_home) == set()
    assert _board_ids(kanban_home, BOARD_A) == set()


def test_the_tool_refuses_a_pin_at_default_with_an_env_board_name(kanban_home, monkeypatch):
    """The second door reads the same verdict: the pin decides, the env name does not save it."""
    import json

    from tools import kanban_tools as kt

    _pin_at_default_store(monkeypatch, kanban_home)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", BOARD_B)
    before = _default_ids(kanban_home)
    payload = json.loads(kt._handle_create({"title": "filed by the tool",
                                            "assignee": "platform-coder",
                                            "triage": True}))
    assert payload.get("ok") is not True, payload
    assert _default_ids(kanban_home) == before
    assert _board_ids(kanban_home, BOARD_B) == set()


# --- every genuine explicit source still files ---------------------------------------------

def test_an_explicit_board_argument_still_files(kanban_home):
    assert kb_cli._cmd_create(_create_args(board=BOARD_B)) == 0
    assert _ids_on(BOARD_B) != set()
    assert _ids_on(BOARD_A) == set()
    assert _default_ids(kanban_home) == set()


def test_the_in_process_board_scope_still_files(kanban_home):
    """`hermes kanban --board <slug> create` — the CLI's typed-name scope."""
    with kb.scoped_current_board(BOARD_B):
        assert kb_cli._cmd_create(_create_args()) == 0
    assert _ids_on(BOARD_B) != set()
    assert _ids_on(BOARD_A) == set()


def test_the_env_board_name_still_files(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_BOARD", BOARD_B)
    assert kb_cli._cmd_create(_create_args()) == 0
    assert _ids_on(BOARD_B) != set()


def test_the_store_pin_still_files(kanban_home, monkeypatch):
    """`HERMES_KANBAN_DB` — the store pin the dispatcher injects into a worker."""
    monkeypatch.setenv(
        "HERMES_KANBAN_DB", str(kanban_home / "kanban" / "boards" / BOARD_B / "kanban.db"))
    assert kb_cli._cmd_create(_create_args()) == 0
    assert _ids_on(BOARD_B) != set()


def test_a_card_fired_from_a_card_still_files_on_the_source_board(kanban_home, monkeypatch):
    """The source card's board is the work's own estate (standing order 2026-09-29)."""
    with kbc.connect(board=BOARD_A) as conn:
        source_id = kb.create_task(conn, title="the card that fires",
                                   assignee="platform-coder")
    monkeypatch.setenv("HERMES_KANBAN_TASK", source_id)
    assert kb_cli._cmd_create(_create_args()) == 0
    assert _ids_on(BOARD_A) - {source_id}, "the fired card lands on the source board"


# --- the second door: the agent tool -------------------------------------------------------

def test_the_tool_refuses_a_boardless_create(kanban_home):
    import json

    from tools import kanban_tools as kt

    before = _all_ids(kanban_home)
    payload = json.loads(kt._handle_create({"title": "filed by the tool",
                                            "assignee": "platform-coder",
                                            "triage": True}))
    assert payload.get("ok") is not True, payload
    assert "names no board" in json.dumps(payload), payload
    assert _all_ids(kanban_home) == before


def test_the_tool_refuses_an_explicit_default_board(kanban_home):
    import json

    from tools import kanban_tools as kt

    before = _default_ids(kanban_home)
    payload = json.loads(kt._handle_create({"title": "filed by the tool",
                                            "assignee": "platform-coder",
                                            "triage": True,
                                            "board": kb.DEFAULT_BOARD}))
    assert payload.get("ok") is not True, payload
    assert "names no board" in json.dumps(payload), payload
    assert _default_ids(kanban_home) == before


def test_the_tool_still_files_with_an_explicit_board(kanban_home):
    import json

    from tools import kanban_tools as kt

    payload = json.loads(kt._handle_create({"title": "filed by the tool",
                                            "assignee": "platform-coder",
                                            "triage": True, "board": BOARD_B}))
    assert payload.get("ok") is True, payload
    assert _ids_on(BOARD_B) != set()
