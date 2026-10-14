"""The ASK-HOME guard at the MOVE seam: a board move cannot misplace an operator ask.

The create seam refuses a NEW operator ask filed off its register's board
(``kanban_db.create_task`` -> ``kanban_register.guard_new_ask_home``). That guard cannot
see a card that ALREADY exists and is then RELOCATED, and a board move is exactly how an
ask ends up on a board its register's roll-up does not read - the residual class the
scheduled ``ask-placement-left-behind`` detector used to sweep up.

``kanban_move.move_task`` now asks the SAME module for the SAME rule
(``kanban_register.guard_ask_home_on_move``) before either board is written, so the rule is
stated once and both write seams reach it. These tests pin:

* a move that would take an ask off its register's board REFUSES, and nothing is written on
  either board (the card is still on the source, the target never gains the id);
* the refusal NAMES the register, its home board and the destination;
* the repair direction passes - an ask moved back onto its register's board is not refused;
* a card that only SERVES an ask (it inherits the stamp, it is not the ask) is not gated;
* the ``HERMES_KANBAN_ALLOW_OFF_BOARD_ASK=1`` hatch relocates deliberately and the escape
  is recorded on the card as an ``operator_ask_off_board`` event, never silently;
* the same refusal reaches the CLI, so the surface an operator actually types cannot
  produce the misplacement either.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Ensure the worktree (not the stale global clone) is first on sys.path.
_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb  # noqa: E402
from hermes_cli import kanban_db_connect as kbc  # noqa: E402
from hermes_cli import kanban_move  # noqa: E402
from hermes_cli import kanban_register as kr  # noqa: E402

HOME_BOARD = "ask-home"
OTHER_BOARD = "elsewhere"


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
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
        kr.ALLOW_ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants

        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - defensive
        pass
    kb._INITIALIZED_PATHS.clear()
    return home


def _new(board, **kw):
    with kbc.connect_closing(board=board) as conn:
        return kb.create_task(conn, **kw)


def _task_exists(board, task_id) -> bool:
    with kbc.connect_closing(board=board) as conn:
        return kb.get_task(conn, task_id) is not None


def _body(board, task_id):
    with kbc.connect_closing(board=board) as conn:
        return kb.get_task(conn, task_id).body


def _event_kinds(board, task_id) -> list[str]:
    with kbc.connect_closing(board=board) as conn:
        return [e.kind for e in kb.list_events(conn, task_id)]


def _designate(board, register_id) -> None:
    """Designate ``board``'s operator register the way the estate carries it: board.json.

    The board facade on this line carries no ``operator_register`` kwarg, so the
    designation is written as the same DATA production holds - ``board.json``'s
    ``operator_register`` key, which is what ``kanban_register.register_for_board`` reads
    (the live ``defcon`` board carries exactly this row). Nothing here reaches around the
    resolver: the guard under test resolves the home board through that key.
    """
    path = kb.board_metadata_path(board)
    meta = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    meta[kr.META_KEY] = register_id
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta), encoding="utf-8")


def _cli(args: list[str], env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Run ``hermes kanban …`` with PYTHONPATH pinned to the worktree."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_WORKTREE)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban"] + args,
        env=env,
        capture_output=True,
        text=True,
        cwd=str(_WORKTREE),
        timeout=30,
    )


@pytest.fixture
def seeded(fresh_home):
    """A designated register on ``ask-home``, a NEW ask under it, and a second board.

    ``register`` - the card ``ask-home`` designates as its operator register.
    ``ask``      - a child of the register: a NEW ask (its body stamps its own id as the
                   ask half), so it IS the ask the guard protects.
    ``helper``   - a card on ``elsewhere`` that only SERVES the ask (its stamp names the
                   ask's id, not its own), the class that must stay movable.
    """
    for slug in (HOME_BOARD, OTHER_BOARD):
        kb.create_board(slug, name=slug)
    register = _new(HOME_BOARD, title="Operator request register", body="the register",
                    assignee="default")
    _designate(HOME_BOARD, register)
    ask = _new(HOME_BOARD, title="ask one", body="an ask", assignee="default",
               parents=[register])
    helper = _new(OTHER_BOARD, title="serves the ask", body="ops side",
                  assignee="platform-coder", serves=f"{register}/{ask}")
    return {"register": register, "ask": ask, "helper": helper}


class TestMoveSeamRefusal:
    def test_a_move_off_the_register_board_refuses_and_writes_nothing(self, seeded):
        ask = seeded["ask"]
        with pytest.raises(kr.OperatorAskOffBoardError):
            kanban_move.move_task(
                ask, OTHER_BOARD, source_slug=HOME_BOARD, with_links=True,
            )
        # Nothing moved: the card is still home, the target never gained the id, and
        # neither board carries a moved_in / moved_out audit for it.
        assert _task_exists(HOME_BOARD, ask)
        assert not _task_exists(OTHER_BOARD, ask)
        assert "moved_in" not in _event_kinds(OTHER_BOARD, ask)
        assert "moved_out" not in _event_kinds(HOME_BOARD, ask)

    def test_the_refusal_names_the_register_its_home_and_the_destination(self, seeded):
        ask, register = seeded["ask"], seeded["register"]
        with pytest.raises(kr.OperatorAskOffBoardError) as excinfo:
            kanban_move.move_task(
                ask, OTHER_BOARD, source_slug=HOME_BOARD, with_links=True,
            )
        message = str(excinfo.value)
        assert ask in message
        assert register in message
        assert HOME_BOARD in message
        assert OTHER_BOARD in message

    def test_the_ask_home_rule_is_stated_once_for_both_seams(self, seeded):
        # The move seam asks the same module the create seam does: one rule, two doors.
        assert hasattr(kr, "guard_new_ask_home")
        assert hasattr(kr, "guard_ask_home_on_move")
        assert "guard_ask_home_on_move" in kr.__all__

    def test_a_linked_set_carrying_an_ask_is_refused_whole(self, seeded):
        # The ask plus a linked card travel together; the guard still sees the ask.
        ask = seeded["ask"]
        sibling = _new(HOME_BOARD, title="work under the ask", body="work",
                       assignee="platform-coder", parents=[ask])
        with pytest.raises(kr.OperatorAskOffBoardError):
            kanban_move.move_task(
                ask, OTHER_BOARD, source_slug=HOME_BOARD, with_links=True,
            )
        assert _task_exists(HOME_BOARD, ask)
        assert _task_exists(HOME_BOARD, sibling)
        assert not _task_exists(OTHER_BOARD, ask)

    def test_the_card_is_not_gated_where_it_lands_on_its_own_home(self, seeded):
        # The repair direction: an ask that IS off its home (moved there under the hatch)
        # is not refused when it is moved BACK to the board its register calls home.
        ask = seeded["ask"]
        os.environ[kr.ALLOW_ENV_VAR] = "1"
        try:
            kanban_move.move_task(
                ask, OTHER_BOARD, source_slug=HOME_BOARD, with_links=True,
            )
            assert _task_exists(OTHER_BOARD, ask)
            kanban_move.move_task(
                ask, HOME_BOARD, source_slug=OTHER_BOARD, with_links=True,
            )
        finally:
            os.environ.pop(kr.ALLOW_ENV_VAR, None)
        assert _task_exists(HOME_BOARD, ask)


class TestNotGated:
    def test_a_card_that_only_serves_an_ask_moves_freely(self, seeded):
        # ``helper`` inherits the ask's stamp; it is not the ask, so relocating it is
        # ordinary work (the discriminator the detector uses, reused here).
        helper = seeded["helper"]
        assert kr.parse_stamp(_body(OTHER_BOARD, helper)) == (
            seeded["register"], seeded["ask"],
        )
        res = kanban_move.move_task(helper, HOME_BOARD, source_slug=OTHER_BOARD)
        assert res["to_board"] == HOME_BOARD
        assert _task_exists(HOME_BOARD, helper)
        assert not _task_exists(OTHER_BOARD, helper)

    def test_a_card_with_no_stamp_moves_freely(self, seeded):
        plain = _new(HOME_BOARD, title="plain work", body="no ask", assignee="platform-coder")
        res = kanban_move.move_task(plain, OTHER_BOARD, source_slug=HOME_BOARD)
        assert res["to_board"] == OTHER_BOARD
        assert _task_exists(OTHER_BOARD, plain)


class TestHatch:
    def test_the_hatch_relocates_and_records_the_escape_on_the_card(
        self, seeded, monkeypatch,
    ):
        ask = seeded["ask"]
        monkeypatch.setenv(kr.ALLOW_ENV_VAR, "1")
        res = kanban_move.move_task(
            ask, OTHER_BOARD, source_slug=HOME_BOARD, with_links=True,
        )
        assert res["to_board"] == OTHER_BOARD
        assert _task_exists(OTHER_BOARD, ask)
        records = res["off_board_asks"]
        assert len(records) == 1 and records[0]["task_id"] == ask
        assert records[0]["register_board"] == HOME_BOARD
        # The escape is on the record, exactly as the create seam's hatch records it.
        assert kr.OFF_BOARD_ASK_EVENT in _event_kinds(OTHER_BOARD, ask)


class TestCliSurface:
    def test_the_cli_refuses_the_same_move(self, seeded):
        ask = seeded["ask"]
        proc = _cli(["boards", "move", ask, "--to", OTHER_BOARD, "--from", HOME_BOARD,
                     "--with-links"])
        assert proc.returncode != 0, proc.stdout + proc.stderr
        assert "operator ask" in (proc.stdout + proc.stderr)
        assert _task_exists(HOME_BOARD, ask)
        assert not _task_exists(OTHER_BOARD, ask)

    def test_the_cli_moves_an_unrelated_card(self, seeded):
        plain = _new(HOME_BOARD, title="plain work", body="no ask", assignee="platform-coder")
        proc = _cli(["boards", "move", plain, "--to", OTHER_BOARD, "--from", HOME_BOARD])
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert _task_exists(OTHER_BOARD, plain)
