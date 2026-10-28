"""The cross-board probe must read each board's OWN store — never the process PIN.

The measured defect (2026-10-13, card t_5ca4531d, root cause confirmed on t_2187558e as
decision D3): ``HERMES_KANBAN_DB`` pins the store a CALLING PROCESS writes to, and
``kanban_db.kanban_db_path(<slug>)`` honours that pin regardless of the slug asked about.
So every registered slug resolved to the pinned file, ``kanban_register.open_board_ro``
opened the pinned store for every probe, and ``find_card_board`` answered the FIRST
registered slug (``default``) for a card that really lived on ``defcon``.

That wrong slug then travelled into the fired-card path
(``board_for_fired_card`` -> the create door), where the board METADATA (the floor /
``priority_policy``) is read for the answered board while the row is INSERTed into the
pinned one — which is why a defcon worker's own child was refused by defcon's armed
tranche guard ("priority is below this board's floor 990000"), and why the same
mis-resolution silently mis-reads the GROOMING policy even where no floor blocks the write.

What this module pins (the D3 property, from a pinned process):

* a pinned process still WRITES to its pin (``kanban_db_path()``) — the worker contract;
* a NAMED board still resolves to its own store, and the probe opens each slug's OWN
  file — no slug aliases another;
* ``find_card_board`` / ``source_board_for_task`` answer the board that REALLY holds the
  id, and ``None`` for an id no board holds (never a fabricated slug);
* the fired-card rule keeps its other two legs: an explicit board wins, and a filing with
  no source card still answers ``None`` (= the ambient chain, unchanged).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

# Ensure the worktree (not a stale global clone) is first on sys.path.
_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb  # noqa: E402
from hermes_cli import kanban_db_connect as kbc  # noqa: E402
from hermes_cli import kanban_register as kr  # noqa: E402

PINNED = "probe-pinned"
OTHER = "probe-other"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An isolated HERMES_HOME with no prior kanban state and no ambient pins."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_ATTACHMENTS_ROOT",
        kr.ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants

        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - defensive
        pass
    kb._INITIALIZED_PATHS.clear()
    return home


def _own_store(slug: str) -> Path:
    """``slug``'s OWN store from the LAYOUT (never through an env pin, and never through
    the helper the fix introduced — this module must fail on the pre-fix bytes for the
    behavioural reason, not because an accessor is missing)."""
    if slug == kb.DEFAULT_BOARD:
        return Path(kb.kanban_home() / "kanban.db").resolve()
    return Path(kb.board_dir(slug) / "kanban.db").resolve()


def _main_path(conn: sqlite3.Connection) -> Path:
    """The file ``conn`` is actually open against."""
    for _seq, name, file in conn.execute("PRAGMA database_list"):
        if name == "main" and file:
            return Path(file).resolve()
    raise AssertionError("connection has no main database file")


def _ids(conn: sqlite3.Connection) -> set:
    return {row[0] for row in conn.execute("SELECT id FROM tasks")}


@pytest.fixture
def pinned(tmp_path, monkeypatch, home):
    """The live worker shape: `default` + two named boards, then the PIN and the FENCE.

    Boards and their cards are laid down FIRST, with no pin in play, so the fixture
    cannot itself depend on the resolution under test. The pin and ``HERMES_KANBAN_TASK``
    (the worker FENCE, which is what makes an explicit board resolve through the pin) are
    then set exactly as the dispatcher sets them for a spawned worker — without the fence
    the aliasing does not reproduce, because a named board already outranks the pin.
    """
    kb.create_board("default", name="default")  # the live host always has one
    for slug in (PINNED, OTHER):
        kb.create_board(slug, name=slug, dispatch=False)

    with kbc.connect_closing(board=PINNED) as conn:
        pinned_card = kb.create_task(conn, title="on the pinned board", assignee="default")
    with kbc.connect_closing(board=OTHER) as conn:
        other_card = kb.create_task(conn, title="on the other board", assignee="default")

    monkeypatch.setenv("HERMES_KANBAN_DB", str(_own_store(PINNED)))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", PINNED)
    monkeypatch.setenv("HERMES_KANBAN_TASK", pinned_card)
    kb._INITIALIZED_PATHS.clear()
    return {"pinned": PINNED, "other": OTHER, "pinned_card": pinned_card,
            "other_card": other_card, "pinned_store": _own_store(PINNED)}


# --------------------------------------------------------------------------- the pin
# The pin still owns the ACTIVE board (a worker must keep writing to its own board).


def test_the_active_board_still_resolves_through_the_pin(pinned):
    assert kb.kanban_db_path() == pinned["pinned_store"]
    assert kb.get_current_board() == pinned["pinned"]


# ---------------------------------------------------------- no slug aliases another
# THE regression: under a pin, a probe must open each slug's OWN file, not the pinned one.


def test_every_registered_slug_probes_its_own_store_under_a_pin(pinned):
    """No slug's probe may answer the pinned file (the measured aliasing)."""
    seen = {}
    for slug in kr.registered_boards():
        conn = kr.open_board_ro(slug)
        if conn is None:
            continue
        try:
            seen[slug] = _main_path(conn)
        finally:
            conn.close()

    assert pinned["pinned"] in seen, "the board whose store exists must be probeable"
    assert pinned["other"] in seen, "the other board's store exists and must be probeable"

    aliased = {slug: path for slug, path in seen.items() if path == pinned["pinned_store"]}
    assert set(aliased) == {pinned["pinned"]}, (
        f"a pinned process aliased every slug to the pinned store: {aliased}; "
        f"each board must resolve to its own file ({seen})"
    )


def test_under_the_fence_a_named_board_still_resolves_through_the_pin(pinned):
    """The fence is deliberate and stays: a worker naming another board cannot re-point a
    WRITE at it (``explicit_board_intent_is_pinned``). The probe is the ONE exception —
    it must read the real home, which is what the rest of this module pins. Together the
    two rules are why the fix is a slug-strict probe and not a weakened fence."""
    assert kb.kanban_db_path(board=pinned["other"]) == pinned["pinned_store"]


def test_board_store_path_resolves_the_slug_never_the_pin(pinned):
    """The resolver the probe now uses: the SLUG's own file, pin ignored, layout-shaped."""
    for slug in (kb.DEFAULT_BOARD, pinned["pinned"], pinned["other"]):
        assert Path(kb.board_store_path(slug)).resolve() == _own_store(slug)


def test_the_probe_reads_the_other_boards_own_rows(pinned):
    """The strongest form: the connection IS the other board's file, with its rows."""
    conn = kr.open_board_ro(pinned["other"])
    assert conn is not None, "the other board's store exists"
    try:
        assert _main_path(conn) == _own_store(pinned["other"])
        assert _ids(conn) == {pinned["other_card"]}
    finally:
        conn.close()

    conn = kr.open_board_ro(pinned["pinned"])
    assert conn is not None
    try:
        assert _main_path(conn) == pinned["pinned_store"]
        assert _ids(conn) == {pinned["pinned_card"]}
    finally:
        conn.close()


# --------------------------------------------------------------- the probe's answers


def test_find_card_board_answers_the_real_home(pinned):
    assert kr.find_card_board(pinned["pinned_card"]) == pinned["pinned"]
    assert kr.find_card_board(pinned["other_card"]) == pinned["other"]


def test_source_board_for_task_answers_the_real_home(pinned):
    """A defcon-pinned worker's own card answers its OWN board, never `default`."""
    assert kb.source_board_for_task(pinned["pinned_card"]) == pinned["pinned"]
    assert kb.source_board_for_task(pinned["other_card"]) == pinned["other"]


def test_a_card_no_board_holds_answers_none(pinned):
    assert kb.source_board_for_task("t_no_such_card_anywhere") is None
    assert kr.find_card_board("t_no_such_card_anywhere") is None


def test_a_first_slug_miss_is_not_dressed_up_as_the_answer(pinned):
    """The pre-fix signature in one line: the answer is the card's home, not slug #0."""
    first = kr.registered_boards()[0]
    assert kb.source_board_for_task(pinned["other_card"]) == pinned["other"], (
        f"probe answered a slug that does not hold the card (first registered is {first!r})"
    )


# ------------------------------------------------------ the fired-card rule, all legs


def test_fired_card_lands_on_the_source_cards_board(pinned):
    assert kb.board_for_fired_card(None, source_task_id=pinned["pinned_card"]) == pinned["pinned"]
    assert kb.board_for_fired_card(None, source_task_id=pinned["other_card"]) == pinned["other"]


def test_an_explicit_board_still_wins(pinned):
    assert kb.board_for_fired_card(pinned["pinned"], source_task_id=pinned["other_card"]) == pinned["pinned"]


def test_no_source_card_still_answers_none(pinned):
    """The source-less fallback is untouched: the ambient chain decides (never a slug)."""
    assert kb.source_board_for_task(None) is None
    assert kb.board_for_fired_card(None, source_task_id=None) is None
