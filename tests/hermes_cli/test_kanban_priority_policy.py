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
import os
import sqlite3

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
    # ``HERMES_KANBAN_OPERATOR_ASK`` leaks the worker shell's own ask into every card the
    # test files (``_resolve_operator_ask`` honours it), making them all ask-carrying and
    # letting the above-tranche doors return early — the edit-door refusals then never fire.
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_WORKSPACE", "HERMES_KANBAN_OPERATOR_ASK",
                "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_ADVISORY_SKILLS"):
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
    for requested, stored in (
        (0, 0), (1, 1), (800_000, 800_000),
        # R1 (the domain): a wired board clamps the filer's own value into the ordinary domain
        # even when the policy cannot be used - the card still lands, still at a legal priority.
        # The value is 999_999, not 9_999_999: anything ABOVE the ceiling is the above-tranche
        # door's business and is refused there, before any board policy is consulted
        # (t_6ce41549, operator ruling 2026-09-28), so the wrecked-policy clamp this test exists
        # for is exercised at the ordinary domain's own edge instead.
        (999_999, kpp.ORDINARY_MAX),
    ):
        tid = kb.create_task(
            conn, title="t%d" % requested, assignee="coder", priority=requested,
        )
        assert _priority(conn, tid) == stored
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


# --- the domain (R1/R2): the four doors ------------------------------------------------
#
# A card is filed inside the ordinary domain, a wired board REFUSES a re-rank outside it while
# an unwired board keeps every caller's semantics, a raw SQL write into the reserved tranche is
# refused by a trigger, and ``hermes kanban defcon`` is the one writer that may put a card in
# it. Each door is asserted on BOTH sides - wired and unwired, refusal and the door that is
# allowed - because "inert until wired" is the property the whole design rests on.

TOP_OF_FLEET = '''
def band_birth(requested, assignee, board, title, body):
    return {"asked": requested, "applied": 999999, "reason": "the top of the fleet"}
'''

VERBATIM = '''
def band_birth(requested, assignee, board, title, body):
    return {"applied": requested}
'''

TIER = '''
def band_birth(requested, assignee, board, title, body):
    applied = min(int(requested), 300000)
    return {"asked": requested, "applied": applied, "clamped": applied != int(requested),
            "reason": "the board's tier", "status": "done"}
'''

BLANK = '''
def band_birth(requested, assignee, board, title, body):
    return {"asked": requested, "reason": "no opinion"}
'''


def _run_kanban(argv):
    from hermes_cli.kanban import kanban_command
    from hermes_cli.kanban_parser import build_parser

    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers())
    return kanban_command(parser.parse_args(["kanban", *argv]))


def _json_tail(capsys):
    """The JSON block on a CLI's stdout, whatever human lines were printed before it."""
    out = capsys.readouterr().out
    return json.loads(out[out.index("{"):])


def _wire_cli(home, source, *, function="band_birth", board="default"):
    """Wire a board the way the fleet does it - the CLI, so the storage guard follows."""
    path = _policy_file(home, source)
    assert _run_boards([
        "set-priority-policy", board, "--module", str(path), "--function", function,
    ]) == 0
    return path


# door 1: the create door clamps (and only when the board is wired)

def test_an_unwired_board_stores_what_the_filer_asked(conn, home):
    tid = kb.create_task(conn, title="off book", assignee="coder", priority=999999)
    assert _priority(conn, tid) == 999999
    assert "priority_policy" not in _created_payload(conn, tid)
    assert kb.priority_tranche_guards(conn) == []


def test_a_wired_board_clamps_an_out_of_domain_filing_into_the_domain(conn, home):
    """A policy that answers inside the reserved tranche cannot land a card there."""
    _wire(home, TOP_OF_FLEET)
    tid = kb.create_task(conn, title="over the top", assignee="coder", priority=12345)
    assert _priority(conn, tid) == kpp.ORDINARY_MAX
    record = _created_payload(conn, tid)["priority_policy"]
    # The clamp rides the policy's OWN record - no second provenance key.
    assert record["reason"] == "the top of the fleet"
    assert record["clamped"] is True
    assert record["domain"] == {
        "requested": 999999,
        "applied": kpp.ORDINARY_MAX,
        "bounds": [kpp.ORDINARY_MIN, kpp.ORDINARY_MAX],
        "reason": kpp.domain_reason(999999),
    }


def test_a_wired_board_clamps_the_filers_own_out_of_domain_value(conn, home):
    """A policy with no opinion still cannot make the domain optional."""
    _wire(home, VERBATIM)
    tid = kb.create_task(conn, title="filed at the block", assignee="coder", priority=999999)
    assert _priority(conn, tid) == kpp.ORDINARY_MAX
    record = _created_payload(conn, tid)["priority_policy"]
    assert record["clamped"] is True
    assert record["domain"]["requested"] == 999999
    assert record["domain"]["reason"] == kpp.domain_reason(999999)


def test_a_wired_board_births_the_offset_blowup_value_in_band(conn, home):
    """The measured class: 341447 is banded by the board's policy, and the event says so."""
    _wire(home, TIER)
    tid = kb.create_task(conn, title="offset blowup", assignee="coder", priority=341447)
    assert _priority(conn, tid) == 300000
    record = _created_payload(conn, tid)["priority_policy"]
    assert record["asked"] == 341447
    assert record["applied"] == 300000
    assert record["clamped"] is True
    # In-domain: the domain clamp has nothing to add, so it adds nothing.
    assert "domain" not in record


def test_a_wired_board_clamps_below_the_domain_too(conn, home):
    _wire(home, BLANK)
    tid = kb.create_task(conn, title="under the floor", assignee="coder", priority=-5000)
    assert _priority(conn, tid) == kpp.ORDINARY_MIN
    assert _created_payload(conn, tid)["priority_policy"]["domain"]["requested"] == -5000


def test_a_wired_board_bounds_a_broken_policys_filer_value(conn, home):
    """The filer's value stands when a policy cannot be used - but never outside the domain."""
    _wire(home, RAISES)
    # 999_999, the tranche floor-to-ceiling band's top, not an above-ceiling value: the ceiling
    # is refused at the create seam before any board policy runs (t_6ce41549), so this test's
    # subject - the wrecked policy still bounding the filer's value - is pinned at the edge.
    tid = kb.create_task(conn, title="bounded anyway", assignee="coder", priority=999_999)
    assert _priority(conn, tid) == kpp.ORDINARY_MAX
    record = _created_payload(conn, tid)["priority_policy"]
    assert record["status"] == "unavailable"
    assert record["domain"]["requested"] == 999_999


# door 2: the edit door refuses

def test_the_edit_door_refuses_out_of_domain_on_a_wired_board(conn, home, capsys):
    _wire(home, BAND)
    tid = kb.create_task(conn, title="ordinary", assignee="coder", priority=12345)
    with pytest.raises(kpp.PriorityOutOfDomain):
        kb.edit_task(conn, tid, priority=999999)
    assert _priority(conn, tid) == 12345
    # Through the CLI: the refusal is an error and a non-zero exit, never a silent re-rank.
    assert _run_kanban(["edit", tid, "--priority", "999999"]) == 1
    assert "outside the ordinary domain" in capsys.readouterr().err
    assert _priority(conn, tid) == 12345
    # An in-domain re-rank is none of this door's business.
    assert _run_kanban(["edit", tid, "--priority", "341447"]) == 0
    assert _priority(conn, tid) == 341447


def test_the_edit_door_is_unconditional_on_an_unwired_board(conn, home):
    """The DOMAIN half is the KERNEL's, not a board's (card t_ecbfb34b, 2026-09-29).

    This test used to pin the opposite - that an unwired board let ``edit --priority 999999``
    land - and that is exactly the hole the card was filed to close: ``defcon`` carried 101
    rows at 1100000 with nothing wired. The reserved tranche stays reachable, by the
    designation door and by the marker-carrying ceiling move; an ordinary card does not.
    """
    tid = kb.create_task(conn, title="ordinary", assignee="coder", priority=12345)
    with pytest.raises(kpp.PriorityOutOfDomain):
        kb.edit_task(conn, tid, priority=999999)
    assert _priority(conn, tid) == 12345
    # ...and the domain's own top is still writable, so only the out-of-range value is refused.
    assert kb.edit_task(conn, tid, priority=900000) is True
    assert _priority(conn, tid) == 900000


# door 3: the storage guard, applied by the wiring action

def test_the_storage_guard_refuses_a_raw_tranche_update(conn, home):
    _wire_cli(home, BAND)
    tid = kb.create_task(conn, title="ordinary", assignee="coder", priority=12345)
    assert kb.priority_tranche_guards(conn) == list(kb.PRIORITY_TRANCHE_TRIGGERS)
    with pytest.raises(sqlite3.IntegrityError) as refused:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET priority = 999999 WHERE id = ?", (tid,))
    assert "DESIGNATED" in str(refused.value)
    assert _priority(conn, tid) == 12345


def test_the_storage_guard_refuses_a_raw_tranche_insert(conn, home):
    """The guard is not the clamp: armed on its own, a filing straight into the block is refused."""
    kb.arm_priority_tranche_guard("default")
    with pytest.raises(sqlite3.IntegrityError):
        kb.create_task(conn, title="straight into the block", assignee="coder", priority=990000)
    assert _task_count(conn) == 0


def test_the_wiring_action_arms_and_disarms_the_storage_guard(conn, home, capsys):
    path = _policy_file(home, BAND)
    assert _run_boards(["set-priority-policy", "default", "--module", str(path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["storage_guard"] == "armed"
    assert kb.priority_tranche_guards(conn) == list(kb.PRIORITY_TRANCHE_TRIGGERS)

    tid = kb.create_task(conn, title="ordinary", assignee="coder", priority=12345)
    with pytest.raises(sqlite3.IntegrityError):
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET priority = 990000 WHERE id = ?", (tid,))

    assert _run_boards(["set-priority-policy", "default", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["storage_guard"] == "unarmed"
    assert kb.priority_tranche_guards(conn) == []
    # Unarmed means the board is exactly what it was before the seam existed.
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET priority = 990000 WHERE id = ?", (tid,))
    assert _priority(conn, tid) == 990000


def test_the_storage_guard_lets_the_designation_through(conn, home):
    _wire_cli(home, BAND)
    tid = kb.create_task(conn, title="ordinary", assignee="coder", priority=899998)
    row = kb.designate_priority(
        conn, tid, reason="the release will not survive the night", authority="operator",
    )
    assert row["priority"] == kpp.DESIGNATED_PRIORITY == 990000
    # The card was born at the band's cap (800000), and the ledger records THAT - so revoke
    # returns it exactly where it stood instead of to a value re-derived after the fact.
    assert row["restore_priority"] == 800000
    assert _priority(conn, tid) == kpp.DESIGNATED_PRIORITY
    assert kb.is_priority_designated(conn, tid, board="default") is True
    kept = kb.priority_designation(conn, tid)
    assert kept["reason"] == "the release will not survive the night"
    assert kept["authority"] == "operator"
    assert kept["board"] == "default"


# door 4: the designation verb, and revoke

def test_revoke_returns_the_card_to_the_priority_it_held(conn, home):
    _wire_cli(home, BAND)
    tid = kb.create_task(conn, title="ordinary", assignee="coder", priority=899998)
    kb.designate_priority(conn, tid, reason="defcon 2", authority="operator")
    back = kb.revoke_priority_designation(conn, tid, reason="the release shipped")
    assert back["restored_priority"] == 800000
    assert _priority(conn, tid) == 800000
    assert kb.is_priority_designated(conn, tid) is False
    assert kb.priority_designation(conn, tid)["revoked_at"] == back["revoked_at"]


def test_revoking_a_card_that_was_never_designated_changes_nothing(conn, home):
    tid = kb.create_task(conn, title="ordinary", assignee="coder", priority=12345)
    assert kb.revoke_priority_designation(conn, tid) is None
    assert _priority(conn, tid) == 12345


def test_a_designation_needs_a_reason_and_a_real_card(conn, home):
    tid = kb.create_task(conn, title="ordinary", assignee="coder", priority=12345)
    with pytest.raises(ValueError):
        kb.designate_priority(conn, tid, reason="   ")
    with pytest.raises(ValueError):
        kb.designate_priority(conn, "t_not_a_card", reason="defcon 1")
    assert kb.priority_designation(conn, tid) is None
    assert _priority(conn, tid) == 12345


def test_the_designation_verb_is_scoped_to_the_board_that_owns_the_card(conn, home, capsys):
    """Naming a board the card is not on refuses; an unknown slug never opens one.

    The verb writes the board's OWN ledger, so the board named in the invocation IS the trust
    scope: a designation that landed on a different board would write the row where that board's
    guard does not read it, and a typo'd slug must not silently create an empty board to write on.
    """
    _wire_cli(home, BAND)                              # the card's own board
    kb.create_board("other")                           # a real second board, wired or not
    tid = kb.create_task(conn, title="mine", assignee="coder", priority=7)

    assert _run_kanban(["--board", "other", "defcon", "designate", tid,
                        "--reason", "defcon 1"]) == 1
    assert "no such task" in capsys.readouterr().err
    assert _priority(conn, tid) == 7
    assert kb.priority_designation(conn, tid) is None

    assert _run_kanban(["--board", "t_not_a_board", "defcon", "designate", tid,
                        "--reason", "defcon 1"]) == 1
    assert "does not exist" in capsys.readouterr().err
    assert _priority(conn, tid) == 7
    assert kb.priority_designation(conn, tid) is None


def test_the_cli_designates_and_revokes(conn, home, capsys):
    _wire_cli(home, BAND)
    capsys.readouterr()  # drop the wiring output; only the verb's own output is parsed
    tid = kb.create_task(conn, title="ordinary", assignee="coder", priority=899998)
    assert _run_kanban(["defcon", "designate", tid, "--reason", "defcon 2", "--json"]) == 0
    row = _json_tail(capsys)
    assert row["priority"] == kpp.DESIGNATED_PRIORITY
    assert row["reason"] == "defcon 2"
    assert row["authority"]
    assert _priority(conn, tid) == kpp.DESIGNATED_PRIORITY

    args = ["defcon", "revoke", tid, "--reason", "the release shipped", "--json"]
    assert _run_kanban(args) == 0
    back = _json_tail(capsys)
    assert back["restored_priority"] == 800000
    assert back["revoke_reason"] == "the release shipped"
    assert _priority(conn, tid) == 800000

    # Revoking twice is refused, not silently ignored; a reason is mandatory on both verbs.
    assert _run_kanban(["defcon", "revoke", tid, "--reason", "again"]) == 1
    assert "no live designation" in capsys.readouterr().err
    assert _run_kanban(["defcon", "revoke", tid]) == 2
    assert "--reason is required" in capsys.readouterr().err
    assert _run_kanban(["defcon", "designate", tid]) == 2
    assert "--reason is required" in capsys.readouterr().err


def test_the_designation_door_is_closed_to_a_delegated_child(conn, home, monkeypatch, capsys):
    tid = kb.create_task(conn, title="ordinary", assignee="coder", priority=12345)
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(kb.kanban_home()))
    assert _run_kanban(["defcon", "designate", tid, "--reason", "self-service"]) != 0
    assert "delegate_task child" in capsys.readouterr().err
    # And the same write is refused under the CLI too - the trust boundary is the DB layer.
    with pytest.raises(PermissionError):
        kb.designate_priority(conn, tid, reason="self-service")
    assert kb.priority_designation(conn, tid) is None
    assert _priority(conn, tid) == 12345


# --- the FLOOR: one number, the board's own policy, read at every door -----------------
#
# A board wired with a floor trims its ordinary domain to its top band: no card may sit below
# it, and the reserved tranche above stays designation-only. The number lives in the board's
# policy module (``band_floor(board)``) and nowhere else - the kernel only asks - so the lift a
# filing gets and the refusals a re-rank, a raw write and a revoke get are the same number.
# Every test here names its door, and the two inert cases (no policy at all, a policy with no
# ``band_floor``) are asserted first, because "inert until wired" is what the design rests on.

BIRTH_PASSES_THROUGH = '''
def band_birth(requested, assignee, board, title, body):
    return {"applied": int(requested)}
'''

FLOORED_LIFTS = '''
FLOOR = 900000


def band_floor(board=""):
    return FLOOR


def band_birth(requested, assignee, board, title, body):
    applied = max(int(requested), FLOOR)
    return {"asked": requested, "applied": applied, "clamped": applied != int(requested),
            "floor": FLOOR,
            "reason": "carried on a board whose floor is %d: below it (%d < %d), lifted to "
                      "the floor" % (FLOOR, int(requested), FLOOR)}
'''


def _policy_with_floor(body: str) -> str:
    """A policy whose ``band_floor`` body is *body*, with a birth that passes the value through.

    The pass-through keeps the BIRTH door out of the way, so a test of the reader - and of the
    storage guard, and of the revoke clamp - measures the floor and nothing else. The fleet's
    own module lifts at birth too (``FLOORED_LIFTS`` above); the kernel does not care which,
    because the lift is the policy's business and the refusals are its own.
    """
    return BIRTH_PASSES_THROUGH + "\n\ndef band_floor(board=\"\"):\n" + body + "\n"


FLOORED = _policy_with_floor("    return 900000")
FLOOR_ANSWERS_NONE = _policy_with_floor("    return None")
FLOOR_RAISES = _policy_with_floor("    raise RuntimeError('the floor table is unreadable')")
FLOOR_ABOVE_THE_DOMAIN = _policy_with_floor("    return 950000")
FLOOR_NOT_AN_INT = _policy_with_floor("    return 'the top band'")


def _reprioritized_payload(conn, task_id):
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'reprioritized' "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    return json.loads(row["payload"])


def test_the_ddl_without_a_floor_is_the_literal_it_has_always_been():
    """The no-floor DDL is byte-identical: an unwired board's guard does not move.

    This is the assertion that keeps ``floor is None`` honest - the clause adds a WHEN term and
    a message, so a mistake here would change every armed board in the fleet, not only the
    floored one.
    """
    insert, update = kb.priority_tranche_trigger_ddl(None)
    literal = ("SELECT RAISE(ABORT, 'priority %d-%d is DESIGNATED, never requested: use "
               "''hermes kanban defcon designate''');" % (kpp.TRANCHE_FLOOR, kpp.TRANCHE_TOP))
    for statement in (insert, update):
        assert literal in statement
        assert "NEW.priority <" not in statement          # no floor clause
        assert "CASE WHEN" not in statement               # no message chosen
        assert "OLD.priority" not in statement
    # And with a floor, the SAME two triggers carry both clauses and BOTH messages, chosen by
    # whichever clause fired (a refusal must not hand the caller the other clause's remedy).
    insert, update = kb.priority_tranche_trigger_ddl(900000)
    tranche_text = ("priority %d-%d is DESIGNATED, never requested: use ''hermes kanban defcon "
                    "designate''" % (kpp.TRANCHE_FLOOR, kpp.TRANCHE_TOP))
    for statement in (insert, update):
        assert tranche_text in statement
        assert "CASE WHEN NEW.priority BETWEEN %d AND %d" % (kpp.TRANCHE_FLOOR,
                                                             kpp.TRANCHE_TOP) in statement
        assert "ELSE 'priority is below this board''s floor 900000" in statement
    assert "NEW.priority < 900000" in insert
    assert "NEW.priority <> OLD.priority AND NEW.priority < 900000" in update


def _spec_for(home, source, name="band_policy.py"):
    return {"module": str(_policy_file(home, source, name=name)), "function": "band_birth"}


# the reader, and the two inert cases

def test_a_board_with_no_floor_answers_none(home):
    """No policy at all, and a policy with no ``band_floor``: both are "no floor here"."""
    assert kpp.board_floor(None, "default") is None
    assert kpp.board_floor("", "default") is None
    spec = _spec_for(home, BAND)
    assert kpp.board_floor(spec, "default") is None
    assert kpp.board_floor(_spec_for(home, FLOOR_ANSWERS_NONE), "default") is None


def test_the_reader_answers_the_boards_own_floor_and_only_that_boards(home):
    spec = _spec_for(home, _policy_with_floor(
        "    return 900000 if board in ('', 'default') else None"))
    assert kpp.board_floor(spec, "default") == 900000
    assert kpp.board_floor(spec, "ops") is None


def test_a_floor_that_cannot_be_read_is_unusable_wiring_not_an_unfloored_board(home):
    """Present but wrong must be LOUD: a silent None would read as "this board has no floor"."""
    for source in (FLOOR_RAISES, FLOOR_ABOVE_THE_DOMAIN, FLOOR_NOT_AN_INT):
        with pytest.raises(kpp.PolicyError):
            kpp.board_floor(_spec_for(home, source), "default")


def test_the_wiring_verb_refuses_a_floor_it_cannot_read_and_writes_nothing(conn, home, capsys):
    path = _policy_file(home, FLOOR_RAISES)
    assert _run_boards(["set-priority-policy", "default", "--module", str(path)]) == 2
    assert "nothing written" in capsys.readouterr().err
    assert kb.board_priority_policy("default") is None
    assert kb.priority_tranche_guards(conn) == []


# door 1: the board's own policy lifts the filing, and the kernel stores what it says

def test_birth_lift_on_a_floored_board(conn, home):
    _wire(home, FLOORED_LIFTS)
    for asked in (0, 500000):
        tid = kb.create_task(conn, title="filed low", assignee="coder", priority=asked)
        assert _priority(conn, tid) == 900000
        record = _created_payload(conn, tid)["priority_policy"]
        assert record["floor"] == 900000
        assert "900000" in record["reason"] and "below" in record["reason"]
    # At the floor: the card is not moved, so the event keeps today's shape.
    tid = kb.create_task(conn, title="at the floor", assignee="coder", priority=900000)
    assert _priority(conn, tid) == 900000
    assert "priority_policy" not in _created_payload(conn, tid)


def test_the_lift_cannot_reach_the_tranche(conn, home):
    """A filing over the top is still clamped into the ORDINARY domain, never lifted past it."""
    _wire(home, FLOORED_LIFTS)
    tid = kb.create_task(conn, title="over the top", assignee="coder", priority=999999)
    assert _priority(conn, tid) == kpp.ORDINARY_MAX < kpp.TRANCHE_FLOOR


# door 2: the edit door refuses below the floor

def test_the_edit_door_refuses_below_the_floor_and_names_it(conn, home, capsys):
    _wire(home, FLOORED)
    tid = kb.create_task(conn, title="at the floor", assignee="coder", priority=900000)
    with pytest.raises(kpp.PriorityOutOfDomain) as refused:
        kb.edit_task(conn, tid, priority=0)
    assert "900000" in str(refused.value)
    assert _priority(conn, tid) == 900000
    # Through the CLI: an error and a non-zero exit, never a silent re-rank.
    assert _run_kanban(["edit", tid, "--priority", "0"]) == 1
    assert "below this board's floor 900000" in capsys.readouterr().err
    assert _priority(conn, tid) == 900000
    # At the floor the door is open: the floor is the bottom of the board, not a wall.
    assert kb.edit_task(conn, tid, priority=900000) is True
    assert _priority(conn, tid) == 900000


def test_the_edit_door_still_bounds_the_top_of_the_domain(conn, home):
    _wire(home, FLOORED)
    tid = kb.create_task(conn, title="at the floor", assignee="coder", priority=900000)
    with pytest.raises(kpp.PriorityOutOfDomain):
        kb.edit_task(conn, tid, priority=990000)


def test_the_edit_door_is_inert_when_the_policy_has_no_floor(conn, home):
    """A module without ``band_floor`` is byte-identical to the behaviour before the clause."""
    _wire(home, BAND)
    tid = kb.create_task(conn, title="ordinary", assignee="coder", priority=12345)
    assert kb.edit_task(conn, tid, priority=0) is True
    assert _priority(conn, tid) == 0


# door 3: the armed storage guard refuses a CHANGE below the floor

def test_the_storage_guard_refuses_a_raw_change_below_the_floor(conn, home):
    _wire_cli(home, FLOORED)
    tid = kb.create_task(conn, title="at the floor", assignee="coder", priority=900000)
    assert kb.priority_tranche_guards(conn) == list(kb.PRIORITY_TRANCHE_TRIGGERS)
    with pytest.raises(sqlite3.IntegrityError) as refused:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET priority = 0 WHERE id = ?", (tid,))
    # The refusal names the culprit clause - the floor, NOT the designation door, which is the
    # remedy for the other clause entirely.
    assert "below this board's floor 900000" in str(refused.value)
    assert "DESIGNATED" not in str(refused.value)
    assert _priority(conn, tid) == 900000


def test_the_storage_guard_refuses_a_row_born_below_the_floor(conn, home):
    """The guard is not the lift: armed over a pass-through policy, a raw filing low is refused."""
    _wire_cli(home, FLOORED)
    with pytest.raises(sqlite3.IntegrityError):
        kb.create_task(conn, title="straight under the floor", assignee="coder", priority=0)
    assert _task_count(conn) == 0


def test_the_storage_guards_floor_clause_is_a_change_clause(conn, home):
    """A no-op write that RE-SETS a below-floor value must not abort, or the drain breaks.

    Every generic field-update path rewrites all columns; on a board whose tail is still being
    drained those rows are below the floor by definition, and refusing to re-set the value they
    already hold would take the whole board down with them. The row here is created BEFORE the
    guard is armed - which is the state the fleet's own below-floor rows are in - and the guard
    is then armed over it.
    """
    _wire(home, FLOORED)                           # wired; the guard follows the key, not yet armed
    tid = kb.create_task(conn, title="already low", assignee="coder", priority=899999)
    assert _priority(conn, tid) == 899999
    assert kb.arm_priority_tranche_guard("default") == list(kb.PRIORITY_TRANCHE_TRIGGERS)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET priority = priority WHERE id = ?", (tid,))
        conn.execute("UPDATE tasks SET title = 'a generic update' WHERE id = ?", (tid,))
    assert _priority(conn, tid) == 899999
    # ... and the same write BELOW the floor is still refused once the value changes.
    with pytest.raises(sqlite3.IntegrityError):
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET priority = 899998 WHERE id = ?", (tid,))


def test_the_wiring_action_re_arms_the_guard_with_the_boards_floor(conn, home, capsys):
    """The floor is baked in at arm time, so the wiring verb has to be able to REPLACE it."""
    path = _policy_file(home, FLOORED)
    assert _run_boards(["set-priority-policy", "default", "--module", str(path)]) == 0
    capsys.readouterr()
    ddl = [row["sql"] for row in conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
        (kb.PRIORITY_TRANCHE_TRIGGERS[1],))]
    assert len(ddl) == 1 and "NEW.priority <> OLD.priority AND NEW.priority < 900000" in ddl[0]
    # A board wired with a policy that has no floor is armed WITHOUT the clause.
    assert _run_boards(["set-priority-policy", "default", "--module", str(_policy_file(home, BAND))]) == 0
    capsys.readouterr()
    ddl = [row["sql"] for row in conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
        (kb.PRIORITY_TRANCHE_TRIGGERS[1],))]
    assert len(ddl) == 1 and "900000" not in ddl[0]


# door 5: the revoke clamp - D2, the measured defect

def test_a_designated_card_revokes_even_when_the_ledger_holds_a_tranche_value(conn, home):
    """D2: the restore value the ledger recorded can be one no card may hold without a
    designation, and the armed guard used to refuse it INSIDE the transaction that sets
    ``revoked_at`` - so the designation could never be released at all.

    The restore is now clamped to what the board allows after the revocation (inside the
    ordinary domain, never below the board's floor), and the correction rides the event.
    """
    _wire_cli(home, FLOORED)
    tid = kb.create_task(conn, title="hand filed", assignee="coder", priority=900000)
    kb.designate_priority(conn, tid, reason="the release will not survive the night",
                          authority="operator")
    assert _priority(conn, tid) == kpp.DESIGNATED_PRIORITY
    # The ledger the fleet actually holds: a raw-SQL filing wrote 999000 as the restore value.
    with kb.write_txn(conn):
        conn.execute("UPDATE priority_designations SET priority = 999000 WHERE task_id = ?",
                     (tid,))
    back = kb.revoke_priority_designation(conn, tid, reason="the release shipped")
    assert back["stored_priority"] == 999000
    assert back["restored_priority"] == 900000     # the floor, not the tranche value
    assert back["correction"] == {
        "stored": 999000, "applied": 900000, "floor": 900000,
        "reason": "the reserved tranche 990000-999999 is designated, never requested",
    }
    assert _priority(conn, tid) == 900000
    assert kb.is_priority_designated(conn, tid, board="default") is False
    assert kb.priority_designation(conn, tid)["revoked_at"] == back["revoked_at"]
    payload = _reprioritized_payload(conn, tid)
    assert payload["designation"] == "revoked"
    assert payload["correction"]["stored"] == 999000
    assert payload["correction"]["applied"] == 900000


def test_a_revoke_from_the_tranche_lands_on_the_domain_edge_without_a_floor(conn, home):
    """The domain clamp is unconditional, so a floored-free board is bounded too."""
    _wire_cli(home, FLOORED)                       # born at 900000, the domain's top
    tid = kb.create_task(conn, title="hand filed", assignee="coder", priority=900000)
    kb.designate_priority(conn, tid, reason="severe")
    with kb.write_txn(conn):
        conn.execute("UPDATE priority_designations SET priority = 990000 WHERE task_id = ?",
                     (tid,))
    back = kb.revoke_priority_designation(conn, tid, reason="shipped")
    assert back["stored_priority"] == 990000
    assert back["restored_priority"] == kpp.ORDINARY_MAX


def test_a_revoke_never_aborts_on_an_unreadable_floor(conn, home):
    """A floor nobody can read must not hold the exit shut - and must not be swallowed either.

    The wiring verb refuses to ARM a floor it cannot read, so this is the other route to the
    same state: a board armed while its policy was readable, and the policy broken afterwards
    (edited, or redeployed to something that raises).
    """
    path = _wire_cli(home, FLOORED)
    tid = kb.create_task(conn, title="hand filed", assignee="coder", priority=900000)
    kb.designate_priority(conn, tid, reason="severe")
    with kb.write_txn(conn):
        conn.execute("UPDATE priority_designations SET priority = 999000 WHERE task_id = ?",
                     (tid,))
    path.write_text(FLOOR_RAISES, encoding="utf-8")
    back = kb.revoke_priority_designation(conn, tid, reason="shipped")
    assert back["restored_priority"] == kpp.ORDINARY_MAX
    payload = _reprioritized_payload(conn, tid)
    assert "unreadable" in payload["floor_unavailable"]
    assert kb.priority_designation(conn, tid)["revoked_at"] == back["revoked_at"]


def test_an_ordinary_revoke_is_untouched_by_the_floor(conn, home):
    """A ledger value that needs no correction keeps the event and the row it always had."""
    _wire_cli(home, FLOORED)
    tid = kb.create_task(conn, title="at the floor", assignee="coder", priority=900000)
    kb.designate_priority(conn, tid, reason="severe")
    back = kb.revoke_priority_designation(conn, tid, reason="shipped")
    assert back["restored_priority"] == 900000
    assert back["correction"] is None
    payload = _reprioritized_payload(conn, tid)
    assert "correction" not in payload
    assert "floor_unavailable" not in payload


# ============================================================================================
# THE GATE INVARIANT - a card must never rank below a card it gates (card t_ef547958).
#
# Everything above is a board's OWN table; this section is the one relation the kernel holds
# whatever a board is wired with. The cases are the ones that decide the design: the lift runs
# at EVERY seam that writes the graph (a filing that names parents, a link, a re-rank, a
# release), in BOTH directions (a raised child lifts its parents; a lowered parent is raised to
# its children), in BOTH bands (the ordinary one and the reserved tranche, which the lift may
# only enter through the designation door), it NEVER demotes or refuses the gated card, a clean
# graph is left byte-identical, and a transaction that rolls back leaves no lift behind.
# ============================================================================================

def _gate_events(conn, task_id):
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'reprioritized' "
        "ORDER BY id", (task_id,),
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


def _designation(conn, task_id):
    return conn.execute(
        "SELECT * FROM priority_designations WHERE task_id = ?", (task_id,),
    ).fetchone()


def _link_direct(conn, parent_id, child_id):
    """Write an edge by RAW SQL - the fleet's re-rank lever, and the only way to reach the
    state the seams cannot: it is what the deterministic pass exists to clean up."""
    with kb.write_txn(conn):
        conn.execute("INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
                     (parent_id, child_id))


def _set_priority_raw(conn, task_id, value):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET priority = ? WHERE id = ?", (value, task_id))


def test_a_filing_that_names_a_lower_parent_lifts_the_parent(conn, home):
    """The seam the operator hit: the card is born, and the gate it names moves up to it."""
    gate = kb.create_task(conn, title="the gate", assignee="coder", priority=5)
    child = kb.create_task(conn, title="the work", assignee="coder", priority=800, parents=[gate])
    assert _priority(conn, gate) == 800            # lifted TO the child, not above it
    assert _priority(conn, child) == 800           # the gated card is never demoted
    payload = _reprioritized_payload(conn, gate)
    assert payload["priority"] == 800
    assert payload["before"] == 5
    assert payload["gate"] == child                # the EDGE that caused it
    assert payload["gate_priority"] == 800
    assert payload["cause"] == "create"
    assert payload["tranche"] is False
    # The child's filing is untouched: the lift never rewrites the card it was asked for.
    assert "priority_policy" not in _created_payload(conn, child)
    assert _gate_events(conn, child) == []


def test_the_lift_walks_the_whole_ancestor_chain_nearest_first(conn, home):
    """A grandparent gates the parent, so a link two levels down moves both - in one pass."""
    grand = kb.create_task(conn, title="grand", assignee="coder", priority=1)
    parent = kb.create_task(conn, title="parent", assignee="coder", priority=2, parents=[grand])
    child = kb.create_task(conn, title="child", assignee="coder", priority=700)
    kb.link_tasks(conn, parent, child)
    assert _priority(conn, parent) == 700
    assert _priority(conn, grand) == 700
    assert _reprioritized_payload(conn, grand)["cause"] == "link"
    # Both descendants now hold 700, so the recorded edge is the tie-break's pick - what matters
    # is that it names an edge it really gates, and the value that edge sets.
    assert _reprioritized_payload(conn, grand)["gate"] in (parent, child)
    assert _reprioritized_payload(conn, grand)["gate_priority"] == 700


def test_a_link_with_a_higher_parent_is_left_alone_byte_identical(conn, home):
    """The ordinary case must cost nothing and leave no trace: no lift, no event."""
    parent = kb.create_task(conn, title="parent", assignee="coder", priority=900)
    child = kb.create_task(conn, title="child", assignee="coder", priority=300)
    before_events = conn.execute(
        "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ?", (parent,)
    ).fetchone()["n"]
    kb.link_tasks(conn, parent, child)
    assert _priority(conn, parent) == 900
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ?", (parent,)
    ).fetchone()["n"] == before_events
    assert _gate_events(conn, parent) == []


def test_a_child_at_the_same_priority_is_not_a_violation(conn, home):
    """The invariant is non-strict: equality is the target, and re-lifting it would be a loop."""
    parent = kb.create_task(conn, title="parent", assignee="coder", priority=600)
    kb.create_task(conn, title="child", assignee="coder", priority=600, parents=[parent])
    assert _priority(conn, parent) == 600
    assert _gate_events(conn, parent) == []


def test_a_delivered_intermediary_ends_the_chain(conn, home):
    """A `done` parent waits for nothing, so nothing behind it is waiting on its gate either -
    and a delivered card is never lifted (priority only orders work that has not run)."""
    grand = kb.create_task(conn, title="grand", assignee="coder", priority=1)
    assert kb.complete_task(conn, grand, result="delivered") is True
    middle = kb.create_task(conn, title="middle", assignee="coder", priority=2, parents=[grand])
    assert kb.complete_task(conn, middle, result="delivered") is True
    assert kb._task_status(conn, middle) == "done"     # the precondition this case is about
    leaf = kb.create_task(conn, title="leaf", assignee="coder", priority=800, parents=[middle])
    assert _priority(conn, leaf) == 800
    assert _priority(conn, middle) == 2            # delivered: the edge is no longer a gate
    assert _priority(conn, grand) == 1             # and the chain does NOT run through it
    assert kb.gate_violations(conn) == []


def test_an_edit_that_would_drop_a_gate_is_raised_to_its_floor(conn, home):
    """A parent cannot be re-ranked under its own children: the ask is raised, in the same write."""
    child = kb.create_task(conn, title="child", assignee="coder", priority=600)
    parent = kb.create_task(conn, title="parent", assignee="coder", priority=900)
    kb.link_tasks(conn, parent, child)
    assert kb.edit_task(conn, parent, priority=1) is True
    assert _priority(conn, parent) == 600          # the floor its child sets, not the ask
    assert _priority(conn, child) == 600           # and the gated card is untouched
    payload = _reprioritized_payload(conn, parent)
    assert payload["priority"] == 600
    assert payload["gate"]["cause"] == "edit"
    assert payload["gate"]["gate"] == child
    assert payload["gate"]["gate_priority"] == 600


def test_an_edit_that_raises_a_card_lifts_the_parents_it_overtook(conn, home):
    """The other direction: a raised child outranks the things it waits on, so they follow."""
    parent = kb.create_task(conn, title="parent", assignee="coder", priority=100)
    child = kb.create_task(conn, title="child", assignee="coder", priority=100, parents=[parent])
    assert kb.edit_task(conn, child, priority=890000) is True
    assert _priority(conn, child) == 890000
    assert _priority(conn, parent) == 890000
    assert _reprioritized_payload(conn, parent)["cause"] == "edit"


def test_a_tranche_child_carries_the_lift_through_the_designation_door(conn, home):
    """A gate whose child holds 999999 must hold 999999: the reserved band is entered the one
    way it may be, ledger row first, and the row says WHICH card it gates."""
    gate = kb.create_task(conn, title="gate", assignee="coder", priority=12)
    child = kb.create_task(conn, title="designated work", assignee="coder", priority=12)
    kb.designate_priority(conn, child, reason="severe", authority="operator")
    kb.edit_task(conn, child, priority=999999)
    _set_priority_raw(conn, child, 999999)          # the ceiling re-rank of a marked card
    kb.link_tasks(conn, gate, child)
    assert _priority(conn, gate) == 999999
    assert kb.is_priority_designated(conn, gate, board="default") is True
    row = _designation(conn, gate)
    assert row["authority"] == kb.GATE_AUTHORITY
    assert child in row["reason"]                   # the reason names the card it gates
    assert row["priority"] == 12                    # the ledger holds the rest it will return to
    payload = _reprioritized_payload(conn, gate)
    assert payload["tranche"] is True
    assert payload["gate"] == child
    assert payload["priority"] == 999999
    # The child itself is untouched: the gated card is never re-ranked by the lift.
    assert _priority(conn, child) == 999999
    assert _designation(conn, child)["authority"] == "operator"


def test_the_lift_never_lowers_a_released_gate(conn, home):
    """One-way, and this is the case that proves it: releasing a designation does not demote the
    gate that was lifted to it - a value is only ever raised by this mechanism."""
    gate = kb.create_task(conn, title="gate", assignee="coder", priority=12)
    child = kb.create_task(conn, title="child", assignee="coder", priority=12, parents=[gate])
    kb.designate_priority(conn, child, reason="severe", authority="operator")
    _set_priority_raw(conn, child, 999999)
    assert _priority(conn, gate) == 12             # the raw write went around the seams
    kb.reconcile_gate_priorities(conn)
    assert _priority(conn, gate) == 999999
    kb.revoke_priority_designation(conn, child, reason="shipped")
    assert _priority(conn, child) == 12
    assert _priority(conn, gate) == 999999          # not lowered to follow the release


def test_a_revoke_that_would_leave_a_gate_under_its_child_is_re_lifted(conn, home):
    """The release runs first, the floor after it: a gate of designated work cannot rest in the
    ordinary band, and the returned row says the relation moved it back."""
    child = kb.create_task(conn, title="child", assignee="coder", priority=12)
    kb.designate_priority(conn, child, reason="severe", authority="operator")
    _set_priority_raw(conn, child, 999999)
    gate = kb.create_task(conn, title="gate", assignee="coder", priority=12)
    kb.link_tasks(conn, gate, child)
    assert _priority(conn, gate) == 999999          # lifted at the link, through the door
    back = kb.revoke_priority_designation(conn, gate, reason="no longer needed")
    assert back["restored_priority"] == 12
    assert _priority(conn, gate) == 999999          # the invariant outranks the release
    lifts = back["gate_lifts"]
    assert lifts[0]["cause"] == "revoke"
    assert lifts[0]["gate"] == child
    assert kb.is_priority_designated(conn, gate, board="default") is True


def test_a_decomposed_root_lifts_every_child_that_waits_on_it(conn, home):
    """The live shape on ops: a decomposed root is the CHILD of its children, so its priority is
    their floor - the second writer gets the same rule as the first."""
    root = kb.create_task(conn, title="root", assignee="coder", priority=285, triage=True)
    kb.designate_priority(conn, root, reason="the operator's ask", authority="operator")
    _set_priority_raw(conn, root, 999999)
    children = kbg.decompose_triage_task(
        conn, root, root_assignee="coder",
        children=[{"title": "one", "assignee": "coder"}, {"title": "two", "assignee": "coder"}],
    )
    assert children
    for cid in children:
        assert _priority(conn, cid) == 999999
        assert _designation(conn, cid)["authority"] == kb.GATE_AUTHORITY
        assert _reprioritized_payload(conn, cid)["cause"] == "decompose"
    assert _priority(conn, root) == 999999


def test_a_rolled_back_filing_leaves_no_lift_behind(conn, home):
    """The lift rides the caller's transaction: a filing that never happened moved nothing."""
    gate = kb.create_task(conn, title="gate", assignee="coder", priority=10)
    with pytest.raises(RuntimeError):
        with kb.write_txn(conn):
            kb.create_task(conn, title="child", assignee="coder", priority=800, parents=[gate])
            raise RuntimeError("the caller rolled back")
    assert _priority(conn, gate) == 10
    assert kb.gate_violations(conn) == []


def test_the_pass_normalises_a_hand_written_violation_and_is_idempotent(conn, home):
    """The backfill: a value written around the seams is lifted, counted, and not repeated."""
    parent = kb.create_task(conn, title="parent", assignee="coder", priority=3)
    child = kb.create_task(conn, title="child", assignee="coder", priority=700)
    _link_direct(conn, parent, child)
    _set_priority_raw(conn, parent, 0)
    assert len(kb.gate_violations(conn)) == 1
    record = kb.reconcile_gate_priorities(conn)
    assert record["violations_before"] == 1
    assert record["lifts_total"] == 1
    assert record["lifts"][0]["cause"] == "reconcile"
    assert record["violations_after"] == 0
    assert record["failed"] == []
    assert _priority(conn, parent) == 700
    again = kb.reconcile_gate_priorities(conn)
    assert (again["violations_before"], again["lifts_total"], again["violations_after"]) == (0, 0, 0)


def test_the_pass_reports_what_it_could_not_hold_instead_of_claiming_success(conn, home):
    """A child left above the scale's top by a raw write is CAPPED, never overshot silently."""
    parent = kb.create_task(conn, title="parent", assignee="coder", priority=1)
    child = kb.create_task(conn, title="child", assignee="coder", priority=2)
    _link_direct(conn, parent, child)
    _set_priority_raw(conn, child, kpp.MAX_PRIORITY + 1)
    record = kb.reconcile_gate_priorities(conn)
    assert _priority(conn, parent) == kpp.MAX_PRIORITY
    lift = record["lifts"][0]
    assert lift["capped_from"] == kpp.MAX_PRIORITY + 1
    assert record["violations_after"] == 1          # honest: the edge is still out of order
    assert record["remaining"][0]["child_id"] == child


def test_the_verb_answers_with_its_exit_code(conn, home, capsys):
    """The carrier contract: 0 when the invariant holds, 1 when it does not, and `report` writes
    nothing - a lane can ask the question without being able to change the answer."""
    parent = kb.create_task(conn, title="parent", assignee="coder", priority=4)
    child = kb.create_task(conn, title="child", assignee="coder", priority=5)
    _link_direct(conn, parent, child)
    assert _run_kanban(["gates", "report"]) == 1
    assert "ranks below the card it gates" in capsys.readouterr().out
    assert _priority(conn, parent) == 4             # report is read-only
    assert _run_kanban(["gates", "reconcile", "--json"]) == 0
    record = _json_tail(capsys)
    assert (record["violations_before"], record["lifts_total"], record["violations_after"]) == (1, 1, 0)
    assert _priority(conn, parent) == 5
    assert _run_kanban(["gates", "report"]) == 0


# ============================================================================================
# A FLOOR ON THE TRANCHE BOUNDARY (card t_90b30457).
#
# A board's floor may be a value inside the ordinary domain OR exactly TRANCHE_FLOOR (990000) -
# the ONE reserved-tranche value a board may name - so a top board can trim its ordinary domain
# to the tranche boundary without opening the tranche. On such a board a row AT the floor is the
# board's own declaration and is admitted WITHOUT a marker; every OTHER value in the reserved
# tranche stays designation-only, on every board. These measure the reader, the create door, the
# edit door, the ARMED GUARD and the release clamp on a boundary-floored board, pin the DDL
# drift to exactly the boundary clause against the CAPTURED pre-change literal, and keep the
# negative controls (no floor, an ordinary floor) exactly as they were.
# ============================================================================================

BOUNDARY_FLOORED = _policy_with_floor("    return 990000")
BOUNDARY_LIFTS = '''
FLOOR = 990000


def band_floor(board=""):
    return FLOOR


def band_birth(requested, assignee, board, title, body):
    applied = max(int(requested), FLOOR)
    return {"asked": requested, "applied": applied, "clamped": applied != int(requested),
            "floor": FLOOR,
            "reason": "carried on a board whose floor is %d: lifted to the floor" % FLOOR}
'''


def _door_refusal():
    """``pytest.raises`` for the class the DOOR raises, resolved at call time.

    ``kanban_db`` binds the policy module INSIDE each door (``from hermes_cli import
    kanban_priority_policy as policy``), so it raises whatever object ``sys.modules``
    holds then. A sibling kanban test file's env fixture deletes the hermes modules and
    re-imports them, leaving this module's collection-time ``kpp`` binding pointing at a
    stale copy; catching THAT class would never match the door's. Resolve it the same way
    the door does instead of trusting the import-time binding.
    """
    import importlib
    return pytest.raises(
        importlib.import_module("hermes_cli.kanban_priority_policy").PriorityOutOfDomain)


def _baseline_ddl():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "tranche_ddl_baseline.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def test_the_clamp_admits_only_the_boundary_floor_and_nothing_else():
    """Seam 2, unit-level: the exemption is EXACTLY a floor on TRANCHE_FLOOR."""
    assert kpp.clamp_to_domain(990000, None, kpp.TRANCHE_FLOOR) == (990000, None)
    assert kpp.clamp_to_domain(990000, None, None)[0] == kpp.ORDINARY_MAX
    assert kpp.clamp_to_domain(990000, None, 900000)[0] == kpp.ORDINARY_MAX
    assert kpp.clamp_to_domain(995000, None, kpp.TRANCHE_FLOOR)[0] == kpp.ORDINARY_MAX
    # a floor that is not the boundary does not buy a tranche value anything, even if it matches
    assert kpp.clamp_to_domain(995000, None, 995000)[0] == kpp.ORDINARY_MAX
    # in-domain values are untouched whatever the floor is
    assert kpp.clamp_to_domain(123, {"applied": 123}, kpp.TRANCHE_FLOOR) == (123, {"applied": 123})


def test_the_reader_accepts_the_boundary_and_refuses_the_rest_of_the_tranche(home):
    """(a) Seam 1: 990000 is a usable floor; everything else in/above the tranche is not.

    Each candidate gets its OWN module filename: the loader caches by (path, mtime_ns, size),
    so two same-size bodies written to one path can collide on the stamp and serve a stale read.
    """
    assert kpp.board_floor(_spec_for(home, BOUNDARY_FLOORED, name="floor_ok.py"),
                           "default") == kpp.TRANCHE_FLOOR
    for name, body in {
        "gap.py": "    return 950000",
        "gap_top.py": "    return 900001",
        "tranche.py": "    return 990001",
        "tranche_top.py": "    return 999999",
        "above.py": "    return 1000000",
    }.items():
        with pytest.raises(kpp.PolicyError):
            kpp.board_floor(_spec_for(home, _policy_with_floor(body), name=name), "default")
    # the refusal names WHICH bound was crossed
    with pytest.raises(kpp.PolicyError) as gap:
        kpp.board_floor(_spec_for(home, _policy_with_floor("    return 950000"), name="gap2.py"),
                        "default")
    assert "gap between the ordinary top" in str(gap.value)
    with pytest.raises(kpp.PolicyError) as inside:
        kpp.board_floor(_spec_for(home, _policy_with_floor("    return 990001"), name="tr2.py"),
                        "default")
    assert "not its floor" in str(inside.value)


def test_the_kernel_floor_reader_answers_the_boundary_floor(conn, home):
    """The reader the doors and the wiring verb call resolves 990000 for the wired board."""
    _wire_cli(home, BOUNDARY_FLOORED)
    assert kb.board_priority_policy("default") is not None
    assert kb._board_priority_floor("default") == kpp.TRANCHE_FLOOR


def test_the_wiring_verb_arms_the_boundary_floor_into_the_triggers(conn, home, capsys):
    """(a) Seam 5: the live armed SQL carries BOTH the boundary exclusion and the floor clause."""
    _wire_cli(home, BOUNDARY_FLOORED)
    capsys.readouterr()
    ddl = [row["sql"] for row in conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
        (kb.PRIORITY_TRANCHE_TRIGGERS[1],))]
    assert len(ddl) == 1
    assert "NEW.priority <> 990000" in ddl[0]       # the boundary exclusion reached the DB
    assert "NEW.priority < 990000" in ddl[0]        # the floor clause is still there


def test_a_card_is_born_at_the_boundary_floor_without_a_designation(conn, home):
    """(a) create door: the policy's lift to 990000 survives the clamp, with no marker written."""
    _wire(home, BOUNDARY_LIFTS)
    tid = kb.create_task(conn, title="the top card", assignee="coder", priority=0)
    assert _priority(conn, tid) == kpp.TRANCHE_FLOOR
    assert kb.priority_designation(conn, tid) is None
    assert kb.is_priority_designated(conn, tid, board="default") is False
    assert _created_payload(conn, tid)["priority_policy"]["applied"] == kpp.TRANCHE_FLOOR


def test_the_edit_door_admits_the_boundary_floor_and_refuses_the_rest(conn, home):
    """(a) edit door: a re-rank UP to 990000 is admitted without a marker; 995000 is refused."""
    _wire(home, BOUNDARY_FLOORED)
    tid = kb.create_task(conn, title="under the floor", assignee="coder", priority=5)
    assert kb.edit_task(conn, tid, priority=kpp.TRANCHE_FLOOR) is True
    assert _priority(conn, tid) == kpp.TRANCHE_FLOOR
    assert kb.priority_designation(conn, tid) is None
    with _door_refusal():
        kb.edit_task(conn, tid, priority=995000)
    assert _priority(conn, tid) == kpp.TRANCHE_FLOOR


def test_the_armed_guard_admits_the_boundary_floor_without_a_marker(conn, home):
    """(a) door 3: the armed trigger lets a write AT the floor through and still refuses 995000."""
    _wire(home, BOUNDARY_FLOORED)                 # policy wired; guard NOT yet armed
    low = kb.create_task(conn, title="below the floor", assignee="coder", priority=5)
    assert _priority(conn, low) == 5
    assert kb.arm_priority_tranche_guard("default") == list(kb.PRIORITY_TRANCHE_TRIGGERS)
    # INSERT clause: a card BORN at the floor passes the armed guard and carries no marker.
    born = kb.create_task(conn, title="born on the floor", assignee="coder", priority=990000)
    assert _priority(conn, born) == kpp.TRANCHE_FLOOR
    assert kb.priority_designation(conn, born) is None
    # UPDATE clause: a raw move UP onto the floor passes with no marker...
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET priority = 990000 WHERE id = ?", (low,))
    assert _priority(conn, low) == kpp.TRANCHE_FLOOR
    # ...and a raw move onto any OTHER tranche value is still refused by the designation clause.
    with pytest.raises(sqlite3.IntegrityError) as refused:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET priority = 995000 WHERE id = ?", (born,))
    assert "DESIGNATED" in str(refused.value)
    assert "below this board's floor" not in str(refused.value)
    assert _priority(conn, born) == kpp.TRANCHE_FLOOR


def test_a_revoke_returns_to_the_boundary_floor_not_the_ordinary_edge(conn, home):
    """Seam 2's release caller: the ledger's tranche value is clamped to the BOUNDARY floor."""
    _wire_cli(home, BOUNDARY_FLOORED)
    tid = kb.create_task(conn, title="on the boundary", assignee="coder", priority=990000)
    kb.designate_priority(conn, tid, reason="severe")
    assert _priority(conn, tid) == kpp.DESIGNATED_PRIORITY
    with kb.write_txn(conn):
        conn.execute("UPDATE priority_designations SET priority = 999000 WHERE task_id = ?", (tid,))
    back = kb.revoke_priority_designation(conn, tid, reason="shipped")
    assert back["stored_priority"] == 999000
    assert back["restored_priority"] == kpp.TRANCHE_FLOOR    # the boundary, not ORDINARY_MAX
    assert back["correction"] == {
        "stored": 999000, "applied": kpp.TRANCHE_FLOOR, "floor": kpp.TRANCHE_FLOOR,
        "reason": "the reserved tranche 990000-999999 is designated, never requested",
    }
    assert _priority(conn, tid) == kpp.TRANCHE_FLOOR
    assert kb.is_priority_designated(conn, tid, board="default") is False


def test_no_floor_still_clamps_the_boundary_value_into_the_domain(conn, home):
    """Negative control: a board with NO floor keeps today's behaviour for 990000."""
    _wire(home, FLOOR_ANSWERS_NONE)
    tid = kb.create_task(conn, title="asked the tranche", assignee="coder", priority=990000)
    assert _priority(conn, tid) == kpp.ORDINARY_MAX
    with _door_refusal():
        kb.edit_task(conn, tid, priority=990000)
    assert _priority(conn, tid) == kpp.ORDINARY_MAX


def test_an_ordinary_floor_does_not_open_the_tranche(conn, home):
    """Negative control: a board floored at 900000 still refuses 990000 at every door."""
    _wire_cli(home, FLOORED)                       # floor 900000, guard armed
    tid = kb.create_task(conn, title="at the floor", assignee="coder", priority=900000)
    with _door_refusal():
        kb.edit_task(conn, tid, priority=990000)
    with pytest.raises(sqlite3.IntegrityError):
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET priority = 990000 WHERE id = ?", (tid,))
    assert _priority(conn, tid) == 900000


def test_the_floor_ddl_is_byte_identical_without_a_boundary_and_differs_only_there():
    """(b) Seam 4 against the CAPTURED pre-change literal, on the shipped bytes.

    ``tranche_ddl_baseline.json`` was captured from the pre-change live line for floors
    ``None``/``900000``/``990000``. The no-floor and ordinary-floor statements must stay
    byte-identical; the boundary floor may differ ONLY by the explicit boundary clause.
    """
    base = _baseline_ddl()
    ent = kb._sql_tranche_entitlement("NEW.id", "NEW.title", "NEW.body")
    old_clause = "NEW.priority BETWEEN %d AND %d AND NOT (%s)" % (
        kpp.TRANCHE_FLOOR, kpp.TRANCHE_TOP, ent)
    boundary_clause = old_clause + " AND NEW.priority <> %d" % kpp.TRANCHE_FLOOR
    # no floor, and a floor inside the ordinary domain: the pre-change literal, byte for byte.
    for floor in (None, 900000):
        key = "None" if floor is None else str(floor)
        new_insert, new_update = kb.priority_tranche_trigger_ddl(floor)
        assert new_insert == base[key]["insert"]
        assert new_update == base[key]["update"]
        assert "NEW.priority <> %d" % int(floor if floor is not None else 0) not in new_insert
    # the old rendering of the boundary floor is the ordinary one with only the number changed.
    assert base["990000"]["insert"] == base["900000"]["insert"].replace("900000", "990000")
    assert base["990000"]["update"] == base["900000"]["update"].replace("900000", "990000")
    # the new rendering differs from that pre-change literal ONLY by the boundary clause.
    new_insert, new_update = kb.priority_tranche_trigger_ddl(kpp.TRANCHE_FLOOR)
    assert new_insert == base["990000"]["insert"].replace(old_clause, boundary_clause)
    assert new_update == base["990000"]["update"].replace(old_clause, boundary_clause)
    for statement in (new_insert, new_update):
        assert statement.count(boundary_clause) == 1
        assert statement.count("NEW.priority <> %d" % kpp.TRANCHE_FLOOR) == 1
        assert "NEW.priority < %d" % kpp.TRANCHE_FLOOR in statement
