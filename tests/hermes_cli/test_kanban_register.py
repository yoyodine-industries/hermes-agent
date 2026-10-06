"""Operator-ask register: a card names the ask it serves, and the ask rolls up across boards.

The defect (operator ask 2026-09-27, card t_8ca4b5a0): work filed in service of an operator
request had no way to say so when it landed on ANOTHER board, because a kanban dependency
edge cannot cross boards. ``defcon`` held the requests; the work sat on ``ops``; the
register's tree showed neither the ops half nor the deep chains that grew under it.

The fix carries a REFERENCE instead of an edge: ``Operator-ask: <register>/<ask>`` stamped
into the card's body at the create seam (``kanban_db.create_task``), which every filing
surface - the tool verb, the CLI, a decomposer, another lane's agent - reaches. The stamp is
inherited by children, decomposition children and a worker's own session, and
``hermes kanban rollup`` closes the graph under BOTH relations (references on any board,
edges within a board) so the register sees the whole tree.

What these tests pin, in the order the feature is layered:

* the stamp text - one line, anchored, exactly one per body;
* reference parsing and the ``<register>`` / ``<register>/<ask>`` forms;
* the board designation that anchors ``rollup`` with no argument;
* resolution precedence at the create seam (explicit > env > body > parents), and the
  cross-board single-id probe;
* what ``create_task`` writes - the stamped body, the ``created`` event, and the inertness
  the seam promises when no ask is in play (byte-identical body, untouched event payload);
* decomposition children on a card in service of an ask;
* the value the dispatcher exports into a worker's session;
* the roll-up walk itself: cross-board references, edge closure, inherited asks, and a
  reference naming a card no board holds reported rather than dropped.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure the worktree (not a stale global clone) is first on sys.path.
_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb  # noqa: E402
from hermes_cli import kanban_db_connect as kbc  # noqa: E402
from hermes_cli import kanban_db_graph as kbg  # noqa: E402
from hermes_cli import kanban_register as kr  # noqa: E402

REG_BOARD = "reg-board"
OPS_BOARD = "ops-board"


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


@pytest.fixture
def boards(home):
    """Two registered boards: the register's home, and a second board work is filed on."""
    for slug in (REG_BOARD, OPS_BOARD):
        kb.create_board(slug, name=slug)
    return REG_BOARD, OPS_BOARD


# ------------------------------------------------------------------ small helpers


def _new(board, **kw):
    with kbc.connect_closing(board=board) as conn:
        return kb.create_task(conn, **kw)


def _task(board, task_id):
    with kbc.connect_closing(board=board) as conn:
        return kb.get_task(conn, task_id)


def _body(board, task_id):
    return _task(board, task_id).body


def _created_payload(board, task_id):
    with kbc.connect_closing(board=board) as conn:
        for event in kb.list_events(conn, task_id):
            if event.kind == "created":
                return event.payload or {}
    return {}


def _events(board, task_id, kind):
    with kbc.connect_closing(board=board) as conn:
        return [e for e in kb.list_events(conn, task_id) if e.kind == kind]


def _count(board):
    conn = kr.open_board_ro(board)
    assert conn is not None
    try:
        return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    finally:
        conn.close()


def _designate(board, register_id):
    kb.write_board_metadata(board, operator_register=register_id)


@pytest.fixture
def tree(boards):
    """The live shape: a designated register, an ask under it, a deep child, off-board work.

    ``reg`` - the register card, on ``reg-board``, designated for that board.
    ``ask`` - a child of the register: a NEW ask (it carries its own id as the ask half).
    ``deep`` - a child of ``ask``, filed with no reference at all, so it is in service only
      by inheritance - the "deep chains" half of the defect.
    ``off`` - a card on ``ops-board`` filed with an explicit cross-board reference.
    ``off_child`` - a child of ``off``, also on ``ops-board``, filer naming nothing.
    ``unrelated`` - a card on ``ops-board`` in service of nothing.
    """
    reg = _new(REG_BOARD, title="Operator request register", body="the register",
               assignee="default")
    _designate(REG_BOARD, reg)
    ask = _new(REG_BOARD, title="ask one", body="an ask", assignee="default", parents=[reg])
    deep = _new(REG_BOARD, title="deep work", body="deep", assignee="platform-coder",
                parents=[ask])
    off = _new(OPS_BOARD, title="off-board work", body="ops side", assignee="platform-coder",
               serves=f"{reg}/{ask}")
    off_child = _new(OPS_BOARD, title="off-board child", body="more ops work",
                     assignee="platform-worker", parents=[off])
    unrelated = _new(OPS_BOARD, title="unrelated work", body="no ask",
                     assignee="platform-worker")
    return {
        "reg": reg, "ask": ask, "deep": deep,
        "off": off, "off_child": off_child, "unrelated": unrelated,
    }


# ------------------------------------------------------------------- the stamp line


class TestStampText:
    def test_apply_stamp_puts_the_stamp_last_and_it_parses(self):
        body = kr.apply_stamp("the work", "t_fc615201", "t_8ca4b5a0")
        assert kr.parse_stamp(body) == ("t_fc615201", "t_8ca4b5a0")
        assert body.splitlines()[-1] == "Operator-ask: t_fc615201/t_8ca4b5a0"
        assert body.startswith("the work")

    def test_apply_stamp_on_an_empty_body_is_just_the_stamp(self):
        assert kr.apply_stamp(None, "t_fc615201", "t_8ca4b5a0") == (
            "Operator-ask: t_fc615201/t_8ca4b5a0"
        )
        assert kr.apply_stamp("", "t_fc615201", "t_8ca4b5a0") == (
            "Operator-ask: t_fc615201/t_8ca4b5a0"
        )

    def test_re_stamping_replaces_rather_than_stacks(self):
        """One card, one ask: a second stamp must not leave the roll-up two answers."""
        body = kr.apply_stamp("work", "t_fc615201", "t_8ca4b5a0")
        body = kr.apply_stamp(body, "t_fc615201", "t_11112222")
        assert kr.parse_stamp(body) == ("t_fc615201", "t_11112222")
        assert body.count(kr.STAMP_PREFIX) == 1

    def test_apply_stamp_is_idempotent(self):
        once = kr.apply_stamp("work", "t_fc615201", "t_8ca4b5a0")
        assert kr.apply_stamp(once, "t_fc615201", "t_8ca4b5a0") == once

    def test_strip_stamp_removes_only_the_stamp_line(self):
        body = kr.apply_stamp("line one\nline two", "t_fc615201", "t_8ca4b5a0")
        assert kr.strip_stamp(body) == "line one\nline two"

    def test_prose_quoting_the_format_mid_line_is_not_a_stamp(self):
        body = "see `Operator-ask: t_fc615201/t_8ca4b5a0` in the card for details"
        assert kr.parse_stamp(body) is None

    def test_no_stamp_parses_to_none(self):
        assert kr.parse_stamp(None) is None
        assert kr.parse_stamp("") is None
        assert kr.parse_stamp("plain body") is None

    def test_stamp_line_shape_is_the_rendered_form(self):
        assert kr.stamp_line("t_fc615201", "t_8ca4b5a0") == (
            "Operator-ask: t_fc615201/t_8ca4b5a0"
        )


class TestParseRef:
    def test_full_pair(self):
        assert kr.parse_ref("t_fc615201/t_8ca4b5a0") == ("t_fc615201", "t_8ca4b5a0")

    def test_bare_card_id_has_no_ask_half(self):
        assert kr.parse_ref("t_fc615201") == ("t_fc615201", None)

    def test_surrounding_whitespace_is_tolerated(self):
        assert kr.parse_ref("  t_fc615201/t_8ca4b5a0  ") == ("t_fc615201", "t_8ca4b5a0")

    @pytest.mark.parametrize("bad", ["", "   ", None, "ops/ask", "t_ZZZZ", "register"])
    def test_garbage_is_refused(self, bad):
        assert kr.parse_ref(bad) is None

    def test_is_task_id_gate(self):
        assert kr.is_task_id("t_fc615201")
        assert not kr.is_task_id("fc615201")
        assert not kr.is_task_id("t_")
        assert not kr.is_task_id(None)


# --------------------------------------------------------------- board designation


class TestBoardDesignation:
    def test_designation_round_trips(self, boards):
        reg_board, _ = boards
        reg = _new(reg_board, title="register", assignee="default")
        assert kr.register_for_board(reg_board) is None
        _designate(reg_board, reg)
        assert kr.register_for_board(reg_board) == reg

    def test_clearing_the_designation(self, boards):
        reg_board, _ = boards
        reg = _new(reg_board, title="register", assignee="default")
        _designate(reg_board, reg)
        kb.write_board_metadata(reg_board, operator_register="")
        assert kr.register_for_board(reg_board) is None

    def test_writer_refuses_a_value_that_is_not_a_card_id(self, boards):
        reg_board, _ = boards
        with pytest.raises(ValueError):
            kb.write_board_metadata(reg_board, operator_register="register-not-a-card")

    def test_reader_never_raises_on_a_malformed_stored_value(self, boards, monkeypatch):
        """A reader must not raise: a bad value in board.json reads as 'no designation'."""
        reg_board, _ = boards
        monkeypatch.setattr(
            kb, "read_board_metadata", lambda *a, **k: {kr.META_KEY: "not-a-card"}
        )
        assert kr.register_for_board(reg_board) is None


# ------------------------------------------------------------------- resolution


class TestResolveForCreate:
    def test_no_ask_in_play_resolves_to_nothing(self, tree):
        with kbc.connect_closing(board=OPS_BOARD) as conn:
            assert kr.resolve_for_create(
                conn, board=OPS_BOARD, parents=[], body="plain", explicit=None, env=None
            ) is None

    def test_explicit_wins_over_env_body_and_parents(self, tree):
        reg, ask = tree["reg"], tree["ask"]
        with kbc.connect_closing(board=OPS_BOARD) as conn:
            ref = kr.resolve_for_create(
                conn, board=OPS_BOARD, parents=[tree["off"]],
                body=kr.apply_stamp("x", reg, tree["deep"]),
                explicit=f"{reg}/t_11112222", env=f"{reg}/{ask}",
            )
        assert (ref.register, ref.ask, ref.source) == (reg, "t_11112222", "explicit")

    def test_env_wins_over_body_and_parents(self, tree):
        reg, ask = tree["reg"], tree["ask"]
        with kbc.connect_closing(board=OPS_BOARD) as conn:
            ref = kr.resolve_for_create(
                conn, board=OPS_BOARD, parents=[tree["off"]],
                body=kr.apply_stamp("x", reg, tree["deep"]), explicit=None,
                env=f"{reg}/{ask}",
            )
        assert (ref.register, ref.ask, ref.source) == (reg, ask, "env")

    def test_a_body_that_already_carries_a_stamp_is_honoured(self, tree):
        reg, deep = tree["reg"], tree["deep"]
        with kbc.connect_closing(board=OPS_BOARD) as conn:
            ref = kr.resolve_for_create(
                conn, board=OPS_BOARD, parents=[],
                body=kr.apply_stamp("re-filed", reg, deep), explicit=None, env=None,
            )
        assert (ref.register, ref.ask, ref.source) == (reg, deep, "body")

    def test_parents_are_the_last_resort_and_inherit_the_ask(self, tree):
        reg, ask = tree["reg"], tree["ask"]
        with kbc.connect_closing(board=REG_BOARD) as conn:
            ref = kr.resolve_for_create(
                conn, board=REG_BOARD, parents=[ask], body="no stamp here",
                explicit=None, env=None,
            )
        assert (ref.register, ref.ask, ref.source) == (reg, ask, "parents")

    def test_a_child_of_the_register_is_a_new_ask(self, tree):
        """The ask half is left empty so the created card fills in its own id."""
        reg = tree["reg"]
        with kbc.connect_closing(board=REG_BOARD) as conn:
            ref = kr.resolve_for_create(
                conn, board=REG_BOARD, parents=[reg], body="a new ask",
                explicit=None, env=None,
            )
            answered = ref.for_card("t_abcdef123456")
        assert (ref.register, ref.ask, ref.source) == (reg, None, "parents")
        assert answered == (reg, "t_abcdef123456")

    def test_a_malformed_reference_is_refused_before_any_write(self, tree):
        with kbc.connect_closing(board=OPS_BOARD) as conn:
            before = _count(OPS_BOARD)
            with pytest.raises(ValueError):
                kb.create_task(conn, title="bad ref", assignee="platform-coder",
                               serves="register-not-a-card/one")
        assert _count(OPS_BOARD) == before

    def test_a_reference_to_a_card_nobody_holds_is_carried_unresolved(self, tree):
        """A BARE id that names no card is not evidence of an ask: report, never invent."""
        with kbc.connect_closing(board=OPS_BOARD) as conn:
            ref = kr.resolve_for_create(
                conn, board=OPS_BOARD, parents=[], body=None,
                explicit="t_deadbeef", env=None,
            )
        assert ref.unresolved == "t_deadbeef"
        assert ref.for_card("t_x") is None

    def test_a_two_part_reference_is_taken_at_face_value(self, tree):
        """``<register>/<ask>`` is the filer's own statement: no lookup, so a pair naming
        an ask no board holds is still stamped - and reported by the roll-up walk."""
        reg = tree["reg"]
        with kbc.connect_closing(board=OPS_BOARD) as conn:
            ref = kr.resolve_for_create(
                conn, board=OPS_BOARD, parents=[], body=None,
                explicit=f"{reg}/t_deadbeef", env=None,
            )
        assert (ref.register, ref.ask, ref.source) == (reg, "t_deadbeef", "explicit")
        assert ref.unresolved is None
        assert ref.for_card("t_x") == (reg, "t_deadbeef")


# --------------------------------------------------------------- the create seam


class TestCreateSeam:
    def test_explicit_serves_stamps_the_body_and_records_the_event(self, tree):
        reg, ask = tree["reg"], tree["ask"]
        card = _new(OPS_BOARD, title="filed against ask one", body="the work",
                    assignee="platform-coder", serves=f"{reg}/{ask}")
        assert kr.parse_stamp(_body(OPS_BOARD, card)) == (reg, ask)
        payload = _created_payload(OPS_BOARD, card)
        assert payload["operator_ask"] == f"{reg}/{ask}"
        assert payload["operator_ask_source"] == "explicit"

    def test_naming_the_register_alone_fills_in_the_new_card_as_the_ask(self, tree):
        reg = tree["reg"]
        card = _new(REG_BOARD, title="a fresh request", body="what the operator asked",
                    assignee="default", serves=reg)
        assert kr.parse_stamp(_body(REG_BOARD, card)) == (reg, card)

    def test_a_child_inherits_its_parents_ask(self, tree):
        reg, ask = tree["reg"], tree["ask"]
        card = _new(OPS_BOARD, title="follow-up", body="more", assignee="platform-coder",
                    parents=[tree["off"]])
        assert kr.parse_stamp(_body(OPS_BOARD, card)) == (reg, ask)

    def test_a_single_card_id_on_another_board_resolves_through_the_probe(self, tree):
        """The cross-board case: the filer names the ask's card, not the pair."""
        reg, ask = tree["reg"], tree["ask"]
        card = _new(OPS_BOARD, title="short form", body="more", assignee="platform-coder",
                    serves=ask)
        assert kr.parse_stamp(_body(OPS_BOARD, card)) == (reg, ask)

    def test_the_worker_session_env_is_inherited_with_no_argument_at_all(self, tree, monkeypatch):
        reg, ask = tree["reg"], tree["ask"]
        monkeypatch.setenv(kr.ENV_VAR, f"{reg}/{ask}")
        card = _new(OPS_BOARD, title="filed by a worker", body="work",
                    assignee="platform-worker")
        assert kr.parse_stamp(_body(OPS_BOARD, card)) == (reg, ask)
        assert _created_payload(OPS_BOARD, card)["operator_ask_source"] == "env"

    def test_an_unknown_but_wellformed_reference_does_not_fail_the_filing(self, tree):
        """The pair is the filer's statement, so the card IS stamped - and the roll-up is
        what reports the ask no board holds, rather than the filing being refused."""
        reg = tree["reg"]
        card = _new(OPS_BOARD, title="unknown ask", body="work",
                    assignee="platform-coder", serves=f"{reg}/t_deadbeef")
        payload = _created_payload(OPS_BOARD, card)
        assert payload["operator_ask"] == f"{reg}/t_deadbeef"
        assert "operator_ask_unresolved" not in payload
        # The card is still filed; the roll-up is what reports the dangling reference.
        assert kr.rollup(reg).unresolved == [f"{reg}/t_deadbeef"]

    def test_a_bare_id_naming_no_card_files_unstamped_and_records_the_finding(self, tree):
        card = _new(OPS_BOARD, title="dangling id", body="work",
                    assignee="platform-coder", serves="t_deadbeef")
        assert kr.parse_stamp(_body(OPS_BOARD, card)) is None
        assert _body(OPS_BOARD, card) == "work"
        assert _created_payload(OPS_BOARD, card)["operator_ask_unresolved"] == "t_deadbeef"

    def test_with_no_ask_in_play_the_seam_is_inert(self, tree):
        """The promise the seam makes to every existing caller: nothing changes."""
        body = "exactly\nthis body\n"
        before = _count(OPS_BOARD)
        card = _new(OPS_BOARD, title="unrelated", body=body, assignee="platform-worker")
        assert _count(OPS_BOARD) == before + 1
        assert _body(OPS_BOARD, card) == body
        payload = _created_payload(OPS_BOARD, card)
        assert not [k for k in payload if "operator_ask" in k]


class TestDecompositionInheritance:
    def test_children_of_a_card_in_service_carry_the_same_ask(self, tree):
        reg, ask = tree["reg"], tree["ask"]
        root = _new(REG_BOARD, title="triage this", body="needs fan-out",
                    assignee="platform-stl", triage=True, serves=f"{reg}/{ask}")
        with kbc.connect_closing(board=REG_BOARD) as conn:
            children = kbg.decompose_triage_task(
                conn, root, root_assignee="platform-stl",
                children=[
                    {"title": "child one", "assignee": "platform-coder"},
                    {"title": "child two", "assignee": "platform-worker"},
                ],
            )
        assert isinstance(children, list) and len(children) == 2
        for child in children:
            assert kr.parse_stamp(_body(REG_BOARD, child)) == (reg, ask)


# ------------------------------------------------ a body the store hands back unreadable
#
# A card body can reach a writer as BYTES: sqlite returns a BLOB body as bytes (the same
# measured fact ``_text`` exists for - see test_kanban_list_undecodable_text.py), and a
# caller may hand one in directly. The write seams passed that value straight to the stamp
# regex, which raises ``TypeError: cannot use a string pattern on a bytes-like object`` -
# so the card never lands. The normalisation lives at the WRITER (``strip_stamp``), the one
# place BOTH seams route through (``kanban_db.create_task`` and
# ``kanban_db_graph._insert_decomposed_child``), rather than being duplicated at the two
# call sites: one place, stated in the code.


class TestBytesBodyNormalisation:
    def test_strip_stamp_accepts_the_shapes_the_store_hands_back(self):
        assert kr.strip_stamp("a body\n\nOperator-ask: t_fc615201/t_8ca4b5a0") == "a body"
        assert kr.strip_stamp(b"a body") == "a body"
        assert kr.strip_stamp(None) == ""

    def test_apply_stamp_decodes_bytes_lossily_exactly_as_text_does(self):
        # Invalid UTF-8 is decoded with replacement, never raised - the same contract
        # ``_text`` gives the read path.
        stamped = kr.apply_stamp(b"work\xff", "t_fc615201", "t_8ca4b5a0")
        assert isinstance(stamped, str)
        assert kr.parse_stamp(stamped) == ("t_fc615201", "t_8ca4b5a0")

    def test_create_task_with_a_bytes_body_and_an_ask_pair_stores_text(self, tree):
        # THE REPRO (card t_0f375ea2): a bytes body plus a resolvable ask pair used to
        # die in ``apply_stamp`` before the INSERT.
        reg, ask = tree["reg"], tree["ask"]
        card = _new(OPS_BOARD, title="bytes body", body=b"the work",
                    assignee="platform-coder", serves=f"{reg}/{ask}")
        body = _body(OPS_BOARD, card)
        assert isinstance(body, str)
        assert kr.parse_stamp(body) == (reg, ask)
        assert body == kr.apply_stamp("the work", reg, ask)
        with kbc.connect_closing(board=OPS_BOARD) as conn:
            assert conn.execute(
                "SELECT typeof(body) FROM tasks WHERE id = ?", (card,)
            ).fetchone()[0] == "text"

    def test_create_task_with_a_str_body_and_an_ask_pair_stores_text(self, tree):
        # The control: the same call with the str body already worked.
        reg, ask = tree["reg"], tree["ask"]
        card = _new(OPS_BOARD, title="str body", body="the work",
                    assignee="platform-coder", serves=f"{reg}/{ask}")
        body = _body(OPS_BOARD, card)
        assert body == kr.apply_stamp("the work", reg, ask)
        with kbc.connect_closing(board=OPS_BOARD) as conn:
            assert conn.execute(
                "SELECT typeof(body) FROM tasks WHERE id = ?", (card,)
            ).fetchone()[0] == "text"

    def test_a_decomposed_child_with_a_bytes_body_reaches_the_seam_as_text(self, tree):
        # THE SIBLING (kanban_db_graph._insert_decomposed_child): the same bytes body on a
        # card whose ancestor carries a resolvable ask, driven through the real fan-out.
        reg, ask = tree["reg"], tree["ask"]
        root = _new(REG_BOARD, title="triage this", body="needs fan-out",
                    assignee="platform-stl", triage=True, serves=f"{reg}/{ask}")
        with kbc.connect_closing(board=REG_BOARD) as conn:
            children = kbg.decompose_triage_task(
                conn, root, root_assignee="platform-stl",
                children=[{"title": "bytes child", "assignee": "platform-coder",
                           "body": b"child bytes"}],
            )
        assert isinstance(children, list) and len(children) == 1
        child = children[0]
        body = _body(REG_BOARD, child)
        assert isinstance(body, str)
        assert kr.parse_stamp(body) == (reg, ask)
        assert body == kr.apply_stamp("child bytes", reg, ask)
        with kbc.connect_closing(board=REG_BOARD) as conn:
            assert conn.execute(
                "SELECT typeof(body) FROM tasks WHERE id = ?", (child,)
            ).fetchone()[0] == "text"

    # --- the NO-ask-pair FALLBACKS (card t_4576e74f): the same non-str-body class, the
    # residual left by the fix above. With no ask pair to stamp, the two seams disagreed
    # about a non-str body: the create seam passed it through verbatim (stored as a BLOB),
    # while the decompose seam wrote ``body if isinstance(body, str) else None`` and
    # SILENTLY DROPPED it. Both fallbacks now route through
    # ``kanban_register.normalise_body`` (the public name for the module's lossy ``_text``),
    # so the same bytes in give the same text out at either seam.

    def test_create_task_with_a_bytes_body_and_no_ask_pair_stores_text(self, boards):
        # No register is designated on OPS_BOARD and no serves/parents/env is in play, so
        # this drives the plain no-ask fallback rather than ``apply_stamp``.
        card = _new(OPS_BOARD, title="no-ask bytes body", body=b"the work",
                    assignee="platform-coder")
        assert _body(OPS_BOARD, card) == "the work"
        with kbc.connect_closing(board=OPS_BOARD) as conn:
            assert conn.execute(
                "SELECT typeof(body) FROM tasks WHERE id = ?", (card,)
            ).fetchone()[0] == "text"

    def test_a_decomposed_child_with_a_bytes_body_and_no_ask_pair_keeps_the_body(self, boards):
        root = _new(OPS_BOARD, title="triage this", body="needs fan-out",
                    assignee="platform-stl", triage=True)
        with kbc.connect_closing(board=OPS_BOARD) as conn:
            children = kbg.decompose_triage_task(
                conn, root, root_assignee="platform-stl",
                children=[{"title": "no-ask bytes child", "assignee": "platform-coder",
                           "body": b"child bytes"}],
            )
        assert isinstance(children, list) and len(children) == 1
        child = children[0]
        # Pre-fix the drop was silent: the row existed with body NULL.
        assert _body(OPS_BOARD, child) == "child bytes"
        with kbc.connect_closing(board=OPS_BOARD) as conn:
            assert conn.execute(
                "SELECT typeof(body) FROM tasks WHERE id = ?", (child,)
            ).fetchone()[0] == "text"


# ---------------------------------------------------------------------- ask-home


class TestAskHomeGuard:
    """ASK-HOME (card t_abefe660): a NEW operator ask lands on its register's own board.

    Only a NEW ask (self-stamped ``Operator-ask: <register>/<own id>``) is gated; a card
    that INHERITS an ask id by lineage is untouched.
    """

    def test_a_new_ask_on_the_registers_board_is_allowed(self, tree):
        # (i) the register's own board accepts a fresh self-stamped ask
        reg = tree["reg"]
        card = _new(REG_BOARD, title="a fresh request", body="what the operator asked",
                    assignee="default", serves=reg)
        assert kr.parse_stamp(_body(REG_BOARD, card)) == (reg, card)

    def test_a_new_ask_off_the_registers_board_is_refused_and_names_the_fix(self, tree):
        # (ii) the refusal names the title, both boards and the remedy; nothing is written
        reg = tree["reg"]
        before = _count(OPS_BOARD)
        with pytest.raises(kr.OperatorAskOffBoardError) as excinfo:
            _new(OPS_BOARD, title="misplaced ask", body="request",
                 assignee="platform-coder", serves=reg)
        msg = str(excinfo.value)
        assert "misplaced ask" in msg
        assert OPS_BOARD in msg and REG_BOARD in msg
        assert f"--board {REG_BOARD}" in msg
        assert _count(OPS_BOARD) == before

    def test_the_hatch_admits_the_off_board_ask_and_records_it(self, tree):
        # (iii) kwarg hatch: the filing lands, stamped, and the escape is on the record
        reg = tree["reg"]
        card = _new(OPS_BOARD, title="deliberate cross-board ask", body="request",
                    assignee="platform-coder", serves=reg, created_by="platform-coder",
                    allow_off_board_ask=True)
        assert kr.parse_stamp(_body(OPS_BOARD, card)) == (reg, card)
        records = _events(OPS_BOARD, card, kr.OFF_BOARD_ASK_EVENT)
        assert len(records) == 1
        payload = records[0].payload or {}
        assert payload["register"] == reg
        assert payload["register_board"] == REG_BOARD
        assert payload["board"] == OPS_BOARD
        assert payload["caller"] == "platform-coder"
        assert payload["title"] == "deliberate cross-board ask"

    def test_the_env_hatch_admits_it_too(self, tree, monkeypatch):
        # (iii-b) HERMES_KANBAN_ALLOW_OFF_BOARD_ASK=1 is the CLI route
        reg = tree["reg"]
        monkeypatch.setenv(kr.ALLOW_ENV_VAR, "1")
        card = _new(OPS_BOARD, title="env hatch", body="request",
                    assignee="platform-coder", serves=reg)
        assert kr.parse_stamp(_body(OPS_BOARD, card)) == (reg, card)
        assert _events(OPS_BOARD, card, kr.OFF_BOARD_ASK_EVENT)

    def test_an_inherited_ask_is_not_refused_on_any_board(self, tree):
        # (iv) inheritance off-board (the pr-evergreen / reconcile shape) is never gated
        reg, ask = tree["reg"], tree["ask"]
        by_pair = _new(OPS_BOARD, title="inherited pair", body="w",
                       assignee="platform-coder", serves=f"{reg}/{ask}")
        assert kr.parse_stamp(_body(OPS_BOARD, by_pair)) == (reg, ask)
        by_lineage = _new(OPS_BOARD, title="inherited lineage", body="w",
                          assignee="platform-worker", parents=[tree["off"]])
        assert kr.parse_stamp(_body(OPS_BOARD, by_lineage)) == (reg, ask)
        on_home = _new(REG_BOARD, title="inherited on home", body="w",
                       assignee="platform-worker", parents=[tree["ask"]])
        assert kr.parse_stamp(_body(REG_BOARD, on_home)) == (reg, ask)

    def test_with_no_register_configured_nothing_is_refused(self, boards):
        # (v) no designation anywhere -> the guard is inert, even for a named reference
        reg_board, ops_board = boards
        plain = _new(reg_board, title="not a register", body="l", assignee="default")
        assert kr.register_board(plain) is None
        card = _new(ops_board, title="no register configured", body="w",
                    assignee="platform-coder", serves=plain)
        assert kr.parse_stamp(_body(ops_board, card)) is None

    def test_the_decompose_seam_runs_the_same_guard(self, boards, monkeypatch):
        # (the second writer) a decomposed child that IS a new ask is gated there too. The
        # create path cannot produce this board mismatch (a new-ask root already sits on
        # its register's board), so the mismatch is forced by reporting a different home.
        reg_board, ops_board = boards
        reg = _new(reg_board, title="register", body="the register",
                   assignee="default", triage=True)
        _designate(reg_board, reg)
        monkeypatch.setattr(kr, "register_board", lambda _register: ops_board)
        with kbc.connect_closing(board=reg_board) as conn:
            with pytest.raises(kr.OperatorAskOffBoardError) as excinfo:
                kbg.decompose_triage_task(
                    conn, reg, root_assignee="platform-stl",
                    children=[{"title": "child", "assignee": "platform-coder"}],
                )
        msg = str(excinfo.value)
        assert reg_board in msg and ops_board in msg


# ------------------------------------------------------------------- the worker env


class TestWorkerEnvExport:
    def test_the_register_card_exports_the_register_alone(self, tree):
        assert kr.env_ref_for_worker(REG_BOARD, tree["reg"]) == tree["reg"]

    def test_an_ask_card_exports_the_pair(self, tree):
        reg, ask = tree["reg"], tree["ask"]
        assert kr.env_ref_for_worker(REG_BOARD, ask) == f"{reg}/{ask}"

    def test_an_unstamped_deep_child_resolves_through_its_ancestors(self, tree):
        reg, ask = tree["reg"], tree["ask"]
        assert kr.env_ref_for_worker(REG_BOARD, tree["deep"]) == f"{reg}/{ask}"

    def test_a_card_in_service_of_nothing_exports_nothing(self, tree):
        """An env var that is always set would stamp every card on the host."""
        assert kr.env_ref_for_worker(OPS_BOARD, tree["unrelated"]) is None

    def test_an_off_board_card_writes_its_own_stamp_into_the_env(self, tree):
        reg, ask = tree["reg"], tree["ask"]
        assert kr.env_ref_for_worker(OPS_BOARD, tree["off"]) == f"{reg}/{ask}"


# ------------------------------------------------------------------------ the walk


class TestRollup:
    def test_the_register_card_is_on_no_ask_group_and_the_tree_is_complete(self, tree):
        reg, ask, deep = tree["reg"], tree["ask"], tree["deep"]
        result = kr.rollup(reg)
        assert result.register == reg
        assert result.register_board == REG_BOARD
        assert result.register_status
        assert [a.id for a in result.asks] == [ask, reg]
        assert {(c.board, c.id) for c in result.cards} == {
            (REG_BOARD, ask), (REG_BOARD, deep), (OPS_BOARD, tree["off"]),
            (OPS_BOARD, tree["off_child"]),
        }
        assert tree["unrelated"] not in {c.id for c in result.cards}

    def test_the_off_board_card_is_reached_by_reference_and_marked_as_such(self, tree):
        reg = tree["reg"]
        result = kr.rollup(reg)
        off = next(c for c in result.cards if c.id == tree["off"])
        assert off.board == OPS_BOARD
        assert off.via == "ref"
        assert off.ask == tree["ask"]

    def test_an_edge_child_of_the_off_board_card_is_reached_by_edge(self, tree):
        reg = tree["reg"]
        result = kr.rollup(reg)
        child = next(c for c in result.cards if c.id == tree["off_child"])
        assert child.via == "edge"
        assert child.ask == tree["ask"]
        # Depth comes from the same edge: a real, in-board parent is one level up, not a
        # second `~` row at the top of the group. (Regression: id sort order used to
        # decide this.)
        assert child.depth == 2

    def test_the_deep_chain_is_attached_to_its_ask_by_edge(self, tree):
        reg = tree["reg"]
        result = kr.rollup(reg)
        deep = next(c for c in result.cards if c.id == tree["deep"])
        assert deep.via == "edge"
        assert deep.depth >= 2

    def test_counts_cover_both_boards(self, tree):
        reg = tree["reg"]
        result = kr.rollup(reg)
        assert result.by_board == {REG_BOARD: 2, OPS_BOARD: 2}
        assert set(result.boards) >= {REG_BOARD, OPS_BOARD}

    def test_a_reference_naming_a_card_no_board_holds_is_reported(self, tree):
        reg = tree["reg"]
        _new(OPS_BOARD, title="dangling", body="work", assignee="platform-coder",
             serves=f"{reg}/t_deadbeef")
        result = kr.rollup(reg)
        assert result.unresolved == [f"{reg}/t_deadbeef"]
        assert "unresolved reference(s): 1" in kr.render_rollup(result)

    def test_with_no_argument_the_boards_designation_anchors_the_walk(self, tree):
        reg = tree["reg"]
        assert kr.rollup(None, board=REG_BOARD).register == reg

    def test_with_neither_a_register_nor_a_designation_it_refuses(self, tree):
        reg = tree["reg"]
        with pytest.raises(ValueError):
            kr.rollup(None, board=OPS_BOARD)
        with pytest.raises(ValueError):
            kr.rollup("not-a-card-id")

    def test_an_empty_register_reads_as_empty_not_as_an_error(self, boards):
        reg_board, _ = boards
        lonely = _new(reg_board, title="register with nothing under it", assignee="default")
        result = kr.rollup(lonely)
        assert result.cards == []
        assert "nothing in service of this register yet" in kr.render_rollup(result)

    def test_an_archived_card_is_not_in_service(self, tree):
        """A retired card must not appear as live work in the operator's roll-up."""
        reg, off = tree["reg"], tree["off"]
        with kbc.connect_closing(board=OPS_BOARD) as conn:
            conn.execute("UPDATE tasks SET status = 'archived' WHERE id = ?", (off,))
            conn.commit()
        result = kr.rollup(reg)
        assert off not in {c.id for c in result.cards}


class TestRollupViews:
    def test_the_human_view_names_the_board_of_a_cross_board_row(self, tree):
        text = kr.render_rollup(kr.rollup(tree["reg"]))
        assert f"{tree['off']}" in text
        assert OPS_BOARD in text

    def test_the_machine_view_carries_the_same_walk(self, tree):
        reg = tree["reg"]
        payload = kr.rollup_json(kr.rollup(reg))
        assert payload["register"] == reg
        assert payload["cards"] == 4
        assert payload["asks"] == 1
        assert payload["by_board"] == {REG_BOARD: 2, OPS_BOARD: 2}
        assert payload["unresolved"] == []
        cards = [c for group in payload["groups"] for c in group["cards"]]
        assert {c["board"] for c in cards} == {REG_BOARD, OPS_BOARD}
        # The register's own group is the header, not one of the ask groups.
        assert all(group["ask"] != reg for group in payload["groups"])

    def test_dumps_is_json(self):
        import json

        assert isinstance(json.loads(kr.dumps(kr.Rollup(
            register="t_fc615201", register_board=None, register_title="",
            register_status="done", register_assignee=None, boards=[],
        ))), dict)


# ---------------------------------------------------------------- rewrite seams
#
# A card's stamp is how ``_cards_of_board`` knows it is in service, so a seam that
# REWRITES a body must carry the card's ask with it. Measured live 2026-09-29: the
# probe card t_0f589e4a lost its stamp to a specify and vanished from the roll-up.


def _load_dashboard_plugin(monkeypatch):
    """``plugins/kanban/dashboard/plugin_api.py`` by path, as the sibling dashboard tests do."""
    # Fastapi pulls pydantic, whose plugin loader enumerates entry points of every
    # installed distribution. A clone tested against the live install's venv therefore
    # reads the real ``hermes-agent`` egg-info, which tests/home_io_guard.py refuses -
    # a fact about the venv, not about the dashboard seam under test.
    monkeypatch.setenv("PYDANTIC_DISABLE_PLUGINS", "__all__")
    pytest.importorskip("fastapi")
    import importlib.util
    import sys

    path = (
        Path(__file__).resolve().parents[2]
        / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    )
    spec = importlib.util.spec_from_file_location("kanban_plugin_restamp_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _edit(board, task_id, **kw):
    with kbc.connect_closing(board=board) as conn:
        return kb.edit_task(conn, task_id, board=board, **kw)


def _triage_card(board, body, **kw):
    return _new(board, title="probe", body=body, assignee="platform-worker",
                triage=True, **kw)


class TestEditRestamp:
    def test_edit_keeps_the_ask_the_card_was_filed_for(self, tree):
        reg, ask, off = tree["reg"], tree["ask"], tree["off"]
        assert kr.parse_stamp(_body(OPS_BOARD, off)) == (reg, ask)

        assert _edit(OPS_BOARD, off, body="reworded ops work")

        assert _body(OPS_BOARD, off) == kr.apply_stamp("reworded ops work", reg, ask)

    def test_the_edited_card_and_its_children_stay_in_the_rollup(self, tree):
        reg, off, off_child = tree["reg"], tree["off"], tree["off_child"]
        _edit(OPS_BOARD, off, body="reworded ops work")

        cards = {c.id for c in kr.rollup(reg).cards}
        assert off in cards
        assert off_child in cards  # in service by edge alone

    def test_the_stamp_line_is_replaced_not_stacked(self, tree):
        reg, ask, off = tree["reg"], tree["ask"], tree["off"]
        _edit(OPS_BOARD, off, body="one")

        assert _body(OPS_BOARD, off).count(kr.STAMP_PREFIX) == 1
        assert kr.parse_stamp(_body(OPS_BOARD, off)) == (reg, ask)

    def test_a_rewrite_that_carries_its_own_stamp_is_left_alone(self, tree):
        """A caller that stamped its own text means it: the seam does not overrule it."""
        reg, off = tree["reg"], tree["off"]
        body = kr.apply_stamp("hand-written", reg, tree["deep"])

        assert _edit(OPS_BOARD, off, body=body)

        assert kr.parse_stamp(_body(OPS_BOARD, off)) == (reg, tree["deep"])

    def test_a_card_that_serves_nothing_is_not_invented_a_stamp(self, tree):
        unrelated = tree["unrelated"]

        assert _edit(OPS_BOARD, unrelated, body="plain rewrite")

        assert _body(OPS_BOARD, unrelated) == "plain rewrite"
        assert kr.parse_stamp(_body(OPS_BOARD, unrelated)) is None

    def test_an_unstamped_card_inherits_the_lineage_it_already_had(self, tree):
        """A legacy card (filed before the stamp existed) is re-stamped from its lineage."""
        reg, ask, deep = tree["reg"], tree["ask"], tree["deep"]
        with kbc.connect_closing(board=REG_BOARD) as conn:
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET body = ? WHERE id = ?", ("deep, filed long ago", deep),
                )
        assert kr.parse_stamp(_body(REG_BOARD, deep)) is None

        assert _edit(REG_BOARD, deep, body="deep, reworded")

        assert kr.parse_stamp(_body(REG_BOARD, deep)) == (reg, ask)


class TestSpecifyRestamp:
    def test_specifying_a_triage_card_keeps_its_ask(self, tree):
        reg, ask = tree["reg"], tree["ask"]
        card = _triage_card(OPS_BOARD, "probe body", serves=f"{reg}/{ask}")
        assert kr.parse_stamp(_body(OPS_BOARD, card)) == (reg, ask)

        with kbc.connect_closing(board=OPS_BOARD) as conn:
            assert kb.specify_triage_task(
                conn, card, title="probe, specified", body="GOAL\ndo the thing",
            )

        assert _body(OPS_BOARD, card) == kr.apply_stamp("GOAL\ndo the thing", reg, ask)
        assert card in {c.id for c in kr.rollup(reg).cards}

    def test_a_specify_that_only_restates_the_body_keeps_the_bytes(self, tree):
        """Restating a body without its stamp must not rewrite the card out of service."""
        reg, ask = tree["reg"], tree["ask"]
        card = _triage_card(OPS_BOARD, "probe body", serves=f"{reg}/{ask}")
        stored = _body(OPS_BOARD, card)

        with kbc.connect_closing(board=OPS_BOARD) as conn:
            assert kb.specify_triage_task(conn, card, body=kr.strip_stamp(stored))

        assert _body(OPS_BOARD, card) == stored
        assert card in {c.id for c in kr.rollup(reg).cards}

    def test_specifying_a_card_that_serves_nothing_invents_no_stamp(self, tree):
        card = _triage_card(OPS_BOARD, "nothing yet")

        with kbc.connect_closing(board=OPS_BOARD) as conn:
            assert kb.specify_triage_task(conn, card, body="GOAL\nplain work")

        assert _body(OPS_BOARD, card) == "GOAL\nplain work"
        assert kr.parse_stamp(_body(OPS_BOARD, card)) is None


class TestDashboardPatchRestamp:
    def test_patching_a_body_keeps_the_stamp(self, tree, monkeypatch):
        from types import SimpleNamespace

        reg, ask, off = tree["reg"], tree["ask"], tree["off"]
        plugin_api = _load_dashboard_plugin(monkeypatch)

        with kbc.connect_closing(board=OPS_BOARD) as conn:
            plugin_api._patch_title_body(
                conn, off, SimpleNamespace(title=None, body="dashboard reword"), OPS_BOARD,
            )

        assert _body(OPS_BOARD, off) == kr.apply_stamp("dashboard reword", reg, ask)
