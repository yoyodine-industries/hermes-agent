"""Invariant E: a BOT filing is a CHILD or KEYED (``hermes_cli/kanban_filing_shape.py``).

Operator ruling 2026-10-22, folded into card ``t_add7b0e9`` (rate-matched door M1). The defect
the gate removes is measured, not argued: the platform board carried 2,091
``pr-approval-refusal-*`` cards over 1,206 distinct (repo, PR) pairs, and 258 of one bot's 623
weekly filings had neither a parent link nor an idempotency key -- the two shapes a repeat can
be recognised by. A prompt rule decays (the diddle prompt's own child-or-keyed rule is PR
#1887); the store has to refuse the shape.

Proven TWO-SIDED here, like the other invariants' suite: the violating filing is refused BY
NAME and the legitimate one passes, because a gate a caller cannot act on is a dead end.

This suite covers the two acceptance clauses of the ruling that live at the STORE:
  * a second filing naming the same key returns the FIRST card id and creates nothing;
  * a bot filing with neither a parent nor a key is refused by name.
"""

from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A temp HERMES_HOME with its own board, no profiles, and no inherited worker session."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_WORKSPACE"):
        monkeypatch.delenv(var, raising=False)
    db_path = kb.kanban_db_path(board="default")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return tmp_path


@pytest.fixture
def conn(home):
    with kbc.connect_closing() as c:
        yield c


def _rows(conn, where: str, params=()) -> list:
    return [r["id"] for r in conn.execute(f"SELECT id FROM tasks WHERE {where}", params)]


# The refusal is caught by CONTRACT (``invariant`` + ``cause``), never by class identity: this
# suite sandboxes homes and sys.modules, so the class object a module holds at import is not
# guaranteed to be the one the running kernel raises.
class _Refusal:
    def __init__(self, cause=None) -> None:
        self.cause = cause
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is None:
            raise AssertionError("expected an invariant-E refusal; none was raised")
        if not isinstance(exc, ValueError):
            return False
        if getattr(exc, "invariant", None) != "filing_shape":
            return False
        if self.cause is not None and getattr(exc, "cause", None) != self.cause:
            return False
        self.value = exc
        return True


def _refusal(cause=None) -> _Refusal:
    return _Refusal(cause)


# ── the top-level bot filing ────────────────────────────────────────────────

def test_a_bot_top_level_filing_with_no_key_is_refused_by_name(conn):
    with _refusal("unkeyed_top_level") as excinfo:
        kb.create_task(conn, title="unrecognisable", created_by="sdlc")
    message = str(excinfo.value)
    # The refusal must name the shape, the reason, and BOTH honest paths.
    assert "sdlc" in message
    assert "idempotency_key" in message
    assert "parents=[" in message
    assert "Nothing was written" in message
    assert _rows(conn, "title = ?", ("unrecognisable",)) == []


def test_a_bot_top_level_filing_with_a_key_passes(conn):
    tid = kb.create_task(conn, title="keyed", created_by="sdlc", idempotency_key="sdlc:x")
    assert tid.startswith("t_")
    assert _rows(conn, "idempotency_key = ?", ("sdlc:x",)) == [tid]


def test_a_second_filing_of_a_key_returns_the_first_card_and_creates_nothing(conn):
    """The ruling's first acceptance clause: a re-filing is an ANSWER, not a second card."""
    first = kb.create_task(conn, title="once", created_by="pr-approval",
                           idempotency_key="pr-approval:refusal:a#1:stale")
    second = kb.create_task(conn, title="once (again)", created_by="pr-approval",
                            idempotency_key="pr-approval:refusal:a#1:stale")
    assert second == first, "a duplicate key must answer with the FIRST card id"
    assert _rows(conn, "idempotency_key = ?", ("pr-approval:refusal:a#1:stale",)) == [first]
    assert conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"] == 1


def test_a_human_filing_needs_neither_parent_nor_key(conn):
    """The operator proposes work by typing it: no stamp, no key, still legal."""
    assert kb.create_task(conn, title="typed by hand").startswith("t_")
    assert kb.create_task(conn, title="typed by hand, stamped", created_by="user").startswith("t_")


def test_an_unstamped_filing_inside_a_worker_session_is_a_bot_filing(conn, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_parent")
    with _refusal("unkeyed_top_level"):
        kb.create_task(conn, title="from a worker")
    assert _rows(conn, "title = ?", ("from a worker",)) == []


# ── the child filing ───────────────────────────────────────────────────────

def test_a_child_of_a_live_parent_is_linked_and_passes(conn):
    parent = kb.create_task(conn, title="parent", created_by="sdlc", idempotency_key="sdlc:p")
    child = kb.create_task(conn, title="child", created_by="sdlc", parents=[parent])
    links = conn.execute(
        "SELECT child_id FROM task_links WHERE parent_id = ?", (parent,),
    ).fetchall()
    assert [r["child_id"] for r in links] == [child]


def test_a_child_of_an_archived_parent_is_refused_by_name(conn):
    parent = kb.create_task(conn, title="parent", created_by="sdlc", idempotency_key="sdlc:p2")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (parent,))
    with _refusal("terminal_parent") as excinfo:
        kb.create_task(conn, title="orphan", created_by="sdlc", parents=[parent])
    message = str(excinfo.value)
    assert parent in message and "archived" in message
    assert "idempotency_key" in message          # the honest path beside the wall
    assert _rows(conn, "title = ?", ("orphan",)) == []


def test_a_child_of_a_done_parent_is_allowed_and_promotes(conn):
    """``done`` is NOT a refusal: recompute_ready promotes the child at birth (fan-in)."""
    parent = kb.create_task(conn, title="parent", created_by="sdlc", idempotency_key="sdlc:p3")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
    child = kb.create_task(conn, title="late child", created_by="sdlc", parents=[parent])
    assert kb.get_task(conn, child).status == "ready"


def test_a_child_naming_no_card_is_refused_and_names_the_id(conn):
    with _refusal("unknown_parent") as excinfo:
        kb.create_task(conn, title="dangling", created_by="sdlc", parents=["t_deadbeef"])
    assert "t_deadbeef" in str(excinfo.value)
    assert _rows(conn, "title = ?", ("dangling",)) == []


def test_the_refusal_is_a_validation_error_not_an_internal_one(conn):
    """The CLI funnels ``ValueError`` as a validation refusal; the tool surface reads it too."""
    with pytest.raises(ValueError) as excinfo:
        kb.create_task(conn, title="boom", created_by="tip-guard")
    assert not isinstance(excinfo.value, (SystemExit, AssertionError))
    assert "UNKEYED TOP-LEVEL" in str(excinfo.value)


# ── the key's SHAPE: armed per family (platform-stl, ESC-ef2af6848e602c8b) ──
#
# The ruling's second clause is a RATCHET, not a switch: a family whose keys are already shaped
# (or that migrates under a bridge) refuses an unshaped key NOW, a family with legacy keys is in
# warn mode until its date, and a family nobody has seen before is armed at birth -- there is no
# legacy to bridge, so the cost of compliance on day one is zero.

def test_an_armed_family_filing_an_unshaped_key_is_refused_by_name(conn):
    with _refusal("unshaped_key") as excinfo:
        kb.create_task(conn, title="unshaped", created_by="pr-approval",
                       idempotency_key="pr-approval-refusal-135")
    message = str(excinfo.value)
    # The decision requires the refusal to name the FAMILY, the offending KEY and the SHAPE it
    # wanted -- so the remedy is a rewrite, not "disable the rule".
    assert "pr-approval" in message
    assert "pr-approval-refusal-135" in message
    assert "<area>:<condition>" in message
    assert "Nothing was written" in message
    assert _rows(conn, "title = ?", ("unshaped",)) == []


def test_a_bare_counter_is_not_a_condition(conn):
    from hermes_cli import kanban_filing_shape as fs
    assert fs.key_shape_problem("pr-approval-refusal-135") is not None
    assert fs.key_shape_problem("pr-approval:135") is not None
    assert fs.key_shape_problem(":condition") is not None
    assert fs.key_shape_problem("area:") is not None
    assert fs.key_shape_problem("yaan-platform#1891:refusal") is None
    assert fs.key_shape_problem("board:migration-drift") is None


def test_a_warn_mode_family_files_and_the_pass_being_counted(conn, monkeypatch):
    """Warn mode SUCCEEDS the filing -- and records it, because a silent pass proves nothing."""
    from hermes_cli import kanban_filing_shape as fs
    monkeypatch.setitem(fs.WARN_UNTIL, "deploy-train", "2999-01-01")
    tid = kb.create_task(conn, title="warned", created_by="deploy-train",
                         idempotency_key="deploy-train-42")
    assert tid.startswith("t_")
    report = fs.warn_report(conn)
    assert report["total"] == 1
    assert report["families"][0]["family"] == "deploy-train"
    assert "deploy-train-42" in report["families"][0]["samples"]


def test_the_warn_mode_arms_on_its_date(conn):
    """Before the date the key passes; on the date the same key is refused by name."""
    from hermes_cli import kanban_filing_shape as fs
    fs.gate_filing_shape(conn, idempotency_key="deploy-train-42", created_by="deploy-train",
                         today="2026-10-31")
    with pytest.raises(ValueError) as excinfo:
        fs.gate_filing_shape(conn, idempotency_key="deploy-train-42", created_by="deploy-train",
                             today="2026-11-01")
    assert getattr(excinfo.value, "cause", None) == "unshaped_key"


def test_a_family_nobody_has_seen_is_armed_at_birth(conn):
    with _refusal("unshaped_key"):
        kb.create_task(conn, title="new bot", created_by="brand-new-bot",
                       idempotency_key="bare-label")
    assert _rows(conn, "title = ?", ("new bot",)) == []
