"""A board's priority policy is applied AT BIRTH (see kanban_priority_policy).

Three properties carry the design and none of them is visible from a happy-path filing: a
board with no policy must behave exactly as it did before the seam existed (the module is
not even loaded), a policy that cannot be honoured must NOT fail the filing (the card keeps
the value its filer asked for and the ``created`` event records the policy as
``unavailable``), and the second writer - a decomposer's child insert - must be banded by
the board ITS OWN connection is on rather than by whatever board is ambient. Everything
else here is the contract the fleet's band module is written against: argument order, the
record's shape, and the one call per filing.
"""

from __future__ import annotations

import argparse
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_graph as kbg
from hermes_cli import kanban_priority_policy as kpp


BAND = '''
def band_birth(requested, assignee, board, title, body):
    applied = min(int(requested), 800000)
    return {"asked": requested, "applied": applied, "clamped": applied != requested,
            "reason": "outside the band", "status": "done"}
'''

FIXED = '''
def band_birth(requested, assignee, board, title, body):
    return {"applied": 800000, "reason": "the board's tier"}
'''

RAISES = '''
def band_birth(requested, assignee, board, title, body):
    raise ValueError("policy exploded")
'''

NO_APPLIED = '''
def band_birth(requested, assignee, board, title, body):
    return {"asked": requested, "reason": "no opinion"}
'''

COUNTS = '''
import os

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calls.log")


def band_birth(requested, assignee, board, title, body):
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write("call\\n")
    return {"applied": int(requested) + 1, "reason": "one step up"}
'''

RECORDS_ARGS = '''
import json
import os

SEEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "args.json")


def band_birth(requested, assignee, board, title, body):
    with open(SEEN, "w", encoding="utf-8") as fh:
        json.dump([requested, assignee, board, title, body], fh)
    return {"applied": 7, "reason": "seven"}
'''


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A temp HERMES_HOME: its own boards, and no board wiring of any kind."""
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


def _policy_file(home, source, name="band_policy.py"):
    path = home / name
    path.write_text(source, encoding="utf-8")
    return path


def _wire(home, source, *, function="band_birth", board="default"):
    path = _policy_file(home, source)
    kb.write_board_metadata(board, priority_policy={"module": str(path), "function": function})
    return path


def _priority(conn, task_id):
    return conn.execute("SELECT priority FROM tasks WHERE id = ?", (task_id,)).fetchone()["priority"]


def _created_payload(conn, task_id):
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'created' "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    return json.loads(row["payload"])


def _task_count(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]


def _run_boards(argv):
    from hermes_cli.kanban import kanban_command
    from hermes_cli.kanban_parser import build_parser

    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers())
    return kanban_command(parser.parse_args(["kanban", "boards", *argv]))


# --- the inert case -------------------------------------------------------------------

def test_board_without_a_policy_is_inert(conn, home, monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("no board configures a policy: the module must never load")

    monkeypatch.setattr(kpp, "load_callable", _boom)
    tid = kb.create_task(conn, title="plain", assignee="coder", priority=12345)
    assert _priority(conn, tid) == 12345
    # The event is the one an unconfigured board has always written - no new key.
    assert "priority_policy" not in _created_payload(conn, tid)


def test_reader_reports_none_until_the_board_is_wired(conn, home):
    assert kb.board_priority_policy("default") is None
    path = _wire(home, BAND)
    assert kb.board_priority_policy("default") == {
        "module": str(path), "function": "band_birth",
    }


# --- the applied case -----------------------------------------------------------------

def test_policy_clamps_the_card_at_birth(conn, home):
    _wire(home, BAND)
    tid = kb.create_task(conn, title="urgent", assignee="coder", priority=999999)
    assert _priority(conn, tid) == 800000
    # The record lands VERBATIM, extra keys and all. Its ``status`` stays inside the
    # nested record, which is why a policy cannot write over a key the kernel owns.
    assert _created_payload(conn, tid)["priority_policy"] == {
        "asked": 999999,
        "applied": 800000,
        "clamped": True,
        "reason": "outside the band",
        "status": "done",
    }
    assert _created_payload(conn, tid)["status"] == "ready"


def test_a_card_inside_its_band_is_not_clamped(conn, home):
    _wire(home, BAND)
    tid = kb.create_task(conn, title="normal", assignee="coder", priority=500)
    assert _priority(conn, tid) == 500
    # Unmoved: no key at all, rather than a record saying nothing moved.
    assert "priority_policy" not in _created_payload(conn, tid)


def test_the_policy_runs_exactly_once_per_filing(conn, home):
    _wire(home, COUNTS)
    tid = kb.create_task(conn, title="once", assignee="coder", priority=0)
    # Applied once: a second pass would re-feed the applied value and land on 2.
    assert _priority(conn, tid) == 1
    assert (home / "calls.log").read_text(encoding="utf-8").count("call") == 1


def test_an_idempotent_replay_does_not_re_apply_the_policy(conn, home):
    """The other half of "at birth": a replay returns the card as it was born."""
    _wire(home, COUNTS)
    first = kb.create_task(
        conn, title="once", assignee="coder", priority=0, idempotency_key="k1",
    )
    assert _priority(conn, first) == 1
    again = kb.create_task(
        conn, title="once", assignee="coder", priority=4000000, idempotency_key="k1",
    )
    assert again == first
    assert _priority(conn, first) == 1
    assert (home / "calls.log").read_text(encoding="utf-8").count("call") == 1


def test_the_policy_sees_the_routes_it_ranks_on(conn, home):
    _wire(home, RECORDS_ARGS)
    tid = kb.create_task(
        conn, title="ranked card", assignee="coder", priority=3, body="declaration line",
    )
    assert json.loads((home / "args.json").read_text(encoding="utf-8")) == [
        3, conn.execute("SELECT assignee FROM tasks WHERE id = ?", (tid,)).fetchone()["assignee"],
        "default", "ranked card", "declaration line",
    ]


def test_the_second_writer_bands_decomposed_children_too(conn, home):
    """A fan-out is born through kanban_db_graph, not ``create_task``."""
    _wire(home, FIXED)
    root = kb.create_task(conn, title="root", assignee="coder", triage=True)
    kids = kbg.decompose_triage_task(
        conn, root, root_assignee="coder",
        children=[{"title": "a", "assignee": "coder"}, {"title": "b", "assignee": "coder"}],
    )
    assert kids and len(kids) == 2
    for kid in kids:
        assert _priority(conn, kid) == 800000
        assert _created_payload(conn, kid)["priority_policy"]["applied"] == 800000


def test_children_are_banded_by_the_board_their_connection_is_on(home):
    """Not by whatever board is ambient: a caller holding another board's connection.

    The current board here has NO policy, so reading the policy off it would leave these
    children unbanded. The board the connection is open against is the one that decides.
    """
    kb.create_board("other")
    _wire(home, FIXED, board="other")
    assert kb.get_current_board() == "default"
    with kbc.connect_closing(board="other") as other_conn:
        assert kb.board_for_connection(other_conn) == "other"
        root = kb.create_task(
            other_conn, title="root", assignee="coder", triage=True, board="other",
        )
        kids = kbg.decompose_triage_task(
            other_conn, root, root_assignee="coder",
            children=[{"title": "a", "assignee": "coder"}],
        )
        assert kids and len(kids) == 1
        assert _priority(other_conn, kids[0]) == 800000
        assert _created_payload(other_conn, kids[0])["priority_policy"]["applied"] == 800000


def test_a_boards_own_connection_names_its_slug(conn):
    assert kb.board_for_connection(conn) == "default"


def test_a_database_no_board_claims_reports_no_board(tmp_path):
    """A worker runs with ``HERMES_KANBAN_DB`` pinned; an unclaimed path is not a board."""
    with kbc.connect_closing(tmp_path / "pinned.db") as pinned:
        assert kb.board_for_connection(pinned) is None


# --- the fail-open cases ------------------------------------------------------------

def test_policy_that_raises_keeps_the_filers_priority(conn, home):
    _wire(home, RAISES)
    tid = kb.create_task(conn, title="still lands", assignee="coder", priority=1)
    assert _priority(conn, tid) == 1
    record = _created_payload(conn, tid)["priority_policy"]
    assert record["status"] == "unavailable"
    # Which wiring is broken, durably - the module and function, and what it did.
    assert record["policy"].endswith("band_policy.py:band_birth")
    assert "policy exploded" in record["error"]


def test_record_without_applied_keeps_the_filers_priority(conn, home):
    _wire(home, NO_APPLIED)
    tid = kb.create_task(conn, title="still lands", assignee="coder", priority=42)
    assert _priority(conn, tid) == 42
    record = _created_payload(conn, tid)["priority_policy"]
    assert record["status"] == "unavailable"
    assert record["error"] == "returned no 'applied' value"
    assert record["policy"].endswith("band_policy.py:band_birth")


def test_missing_module_keeps_the_filers_priority(conn, home):
    kb.write_board_metadata(
        "default", priority_policy={"module": str(home / "gone.py"), "function": "band_birth"},
    )
    tid = kb.create_task(conn, title="still lands", assignee="coder", priority=7)
    assert _priority(conn, tid) == 7
    assert _created_payload(conn, tid)["priority_policy"]["status"] == "unavailable"


def test_named_function_that_is_absent_keeps_the_filers_priority(conn, home):
    _wire(home, BAND, function="no_such_policy")
    tid = kb.create_task(conn, title="still lands", assignee="coder", priority=8)
    assert _priority(conn, tid) == 8
    assert _created_payload(conn, tid)["priority_policy"]["status"] == "unavailable"


def test_hand_edited_empty_spec_keeps_the_filers_priority(conn, home):
    """A ``{}`` in board.json is a configured key that names nothing - not "no policy"."""
    path = kb.board_metadata_path("default")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"slug": "default", "priority_policy": {}}), encoding="utf-8")
    tid = kb.create_task(conn, title="still lands", assignee="coder", priority=9)
    assert _priority(conn, tid) == 9
    assert _created_payload(conn, tid)["priority_policy"]["status"] == "unavailable"


def test_every_filing_survives_a_wrecked_policy(conn, home):
    """The invariant a filing loop depends on, over the values a filer might ask for."""
    kb.write_board_metadata("default", priority_policy={
        "module": str(home / "no_such_file_at_all.py"), "function": "band_birth",
    })
    for requested in (0, 1, 800_000, 9_999_999):
        tid = kb.create_task(
            conn, title="t%d" % requested, assignee="coder", priority=requested,
        )
        assert _priority(conn, tid) == requested
    # A filing that failed would have left a gap here; four cards, four filers served.
    assert _task_count(conn) == 4


def test_the_wiring_path_still_raises_where_a_filing_reports(home):
    """The split the whole rule rests on: validators RAISE, filings REPORT.

    One broken spec, two behaviours: ``load_callable`` raises ``PolicyError`` so whoever is
    wiring the board is told, while ``priority_for_create`` never raises so a filer is not.
    """
    broken = {"module": str(home / "nope.py"), "function": "band_birth"}
    # ``kpp.PolicyError``, not a re-import: the class that must match is the one this very
    # module object raises. A test elsewhere in the suite that reloads ``hermes_cli``
    # rebinds the module, and a fresh import would then hand back a different class.
    with pytest.raises(kpp.PolicyError):
        kpp.load_callable(broken)
    verdict = kpp.priority_for_create(42, board="default", spec=broken)
    assert verdict is not None
    assert verdict.applied == 42
    assert verdict.record is not None and verdict.record["status"] == "unavailable"


@pytest.mark.parametrize("raw", [
    "relative/policy.py",                 # resolves against the filer's cwd
    "/absolute/but/not/dot/py",
    {"module": "/abs/policy.py", "function": "not a name"},
])
def test_malformed_specs_are_rejected_by_the_reader(raw):
    # Calls the reader directly, so ``kpp``'s own frozen class is the right expectation.
    with pytest.raises(kpp.PolicyError):
        kpp.normalize_spec(raw)


def test_none_is_the_only_inert_spec():
    assert kpp.normalize_spec(None) is None


# --- the writer surface ---------------------------------------------------------------

def test_cli_sets_reports_and_clears_the_policy(conn, home, capsys):
    path = _policy_file(home, BAND)
    assert _run_boards(["set-priority-policy", "default", "--module", str(path)]) == 0
    assert kb.board_priority_policy("default") == {"module": str(path), "function": "band_birth"}
    capsys.readouterr()

    assert _run_boards(["show", "default", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["slug"] == "default"
    assert shown["priority_policy"] == {"module": str(path), "function": "band_birth"}

    assert _run_boards(["set-priority-policy", "default"]) == 0
    assert kb.board_priority_policy("default") is None
    raw = json.loads(kb.board_metadata_path("default").read_text(encoding="utf-8"))
    assert "priority_policy" not in raw


def test_cli_refuses_a_policy_that_cannot_load(conn, home, capsys):
    broken = _policy_file(home, "def band_birth(:\n", name="broken.py")
    assert _run_boards(["set-priority-policy", "default", "--module", str(broken)]) == 2
    assert kb.board_priority_policy("default") is None
    assert "nothing written" in capsys.readouterr().err


def test_cli_refuses_a_named_function_that_is_absent(conn, home, capsys):
    path = _policy_file(home, BAND)
    assert _run_boards([
        "set-priority-policy", "default", "--module", str(path), "--function", "nope",
    ]) == 2
    assert kb.board_priority_policy("default") is None
    assert "nothing written" in capsys.readouterr().err
