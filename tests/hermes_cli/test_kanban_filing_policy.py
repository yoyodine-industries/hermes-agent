"""The filing budget is enforced at the kernel's ONE filing door (card t_80ff9b6a).

What carries this design is not the happy path. It is:

* a host with NO policy file is upstream's behaviour, byte for byte — nothing read, nothing
  printed, no ``filing_policy`` key on the ``created`` event, no estate module imported;
* a governed filing past the pool is REFUSED with the ruling's exact line, and the refusal
  leaves NO row behind (the door is before the write, not after it);
* the refusal is an ANSWER, not an exception: ``create_task`` returns a falsy ``str`` whose
  value is the line, and both doors (``hermes kanban create`` → rc 3, the tool verb → its
  error envelope) report it without a traceback;
* the pool is spent IN BAND ORDER, so notices over-filing can never strand a defect, and an
  exempt surface (the operator's own filing) is never refused;
* a broken policy or a broken instrument ADMITS the filing and records why — an unmeasurable
  door is not a budget, and a filing silently lost is the failure mode this whole change
  exists to remove.

The pool itself is never re-implemented here: the tests point the policy at a small
instrument file and assert the module calls it, so the estate's real counter stays the only
counter (its own suite covers the counter; this one covers the door).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_filing_policy as kfp


# --- the instrument the tests stand in for the estate's real one ------------------------------

INSTRUMENT = '''
import json
import os


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


class Budget:
    def __init__(self, seconds):
        self.seconds = float(seconds)


def _state():
    with open(os.environ["FILING_PROBE_STATE"], "r", encoding="utf-8") as fh:
        return json.load(fh)


def count_births(since_epoch, boards_root=None, config=None, budget=None):
    state = _state()
    by_surface = dict(state.get("births") or {})
    return {"births": sum(int(v) for v in by_surface.values()),
            "births_by_surface": by_surface,
            "births_by_board": state.get("births_by_board") or {},
            "identities_reused": 0, "notices": 0, "rows": sum(by_surface.values())}


def count_merges(since_epoch, names, config, budget=None):
    state = _state()
    return {"merges": int(state.get("merges") or 0),
            "merges_by_repo": {name: 0 for name in names}, "errors": []}
'''

EXPLODES = '''
def load_config(path):
    raise RuntimeError("instrument on fire")


def count_births(*a, **k):
    raise RuntimeError("instrument on fire")
'''

#: The ruling's line, exactly: a second format would be a second contract.
REFUSAL_LINE = re.compile(
    r"^refused: budget spent \(births=(\d+) merges=(\d+) window=(\d+)h surface=(\S+) "
    r"precedence=(\d+)\)$"
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A temp HERMES_HOME: its own boards, and no policy of any kind."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv(kfp.ENV_POLICY, raising=False)
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


@pytest.fixture
def instrument(home, monkeypatch, tmp_path):
    """The stand-in instrument, plus the knob the tests turn its reading with."""
    path = tmp_path / "instrument.py"
    path.write_text(INSTRUMENT, encoding="utf-8")
    state_file = tmp_path / "instrument_state.json"

    def reading(*, merges=44, births=None, **extra):
        payload = {"merges": merges, "births": dict(births or {})}
        payload.update(extra)
        state_file.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setenv("FILING_PROBE_STATE", str(state_file))
        return path

    return reading


def policy_file(tmp_path, *, instrument_path, merges="from-instrument", **overrides):
    """A policy writing the roster the tests need; ``merges`` is never a policy key."""
    policy = {
        "version": 1,
        "enabled": True,
        "default_precedence": 5,
        "unknown_creator": {"class": "governed", "precedence": 5},
        "precedence": {
            "1": {"name": "remediation-keyed", "zero_cost": True},
            "2": {"name": "sev1-alarm", "exempt": True, "markers": ["sev1:", "[sev1]"]},
            "3": {"name": "defect", "markers": ["[defect]", "defect:"]},
            "4": {"name": "design", "markers": ["[design]", "design:"]},
            "5": {"name": "notice-and-other"},
        },
        "surfaces": {
            "platform-stl": {"class": "governed", "precedence": 4, "owner": "platform-stl"},
            "notice-lane": {"class": "governed", "precedence": 5, "owner": "platform-stl"},
            "user": {"class": "exempt", "owner": "operator", "why": "the operator files what "
                     "the estate must do", "expires": "2099-12-31"},
        },
        "pool": {
            "instrument": str(instrument_path),
            "config": str(tmp_path / "instrument_config.json"),
            "window_hours": 24,
            "repos": ["yaan-platform"],
            "budget_s": 20,
        },
    }
    (tmp_path / "instrument_config.json").write_text(json.dumps({"flows": {}}), encoding="utf-8")
    policy.update(overrides)
    path = tmp_path / "filing_policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    return path


def _tasks(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]


def _created_payload(conn, task_id):
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'created' "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    return json.loads(row["payload"])


def _kanban(argv):
    from hermes_cli.kanban import kanban_command
    from hermes_cli.kanban_parser import build_parser

    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers())
    return kanban_command(parser.parse_args(["kanban", *argv]))


# --- the inert host ---------------------------------------------------------------------------

def test_a_host_without_a_policy_is_upstream_behaviour(conn, home):
    """No policy file: the estate module is never consulted and the event is unchanged."""
    tid = kb.create_task(conn, title="plain", assignee="coder", created_by="platform-stl")
    assert isinstance(tid, str) and tid.startswith("t_")
    assert "filing_policy" not in _created_payload(conn, tid)
    assert kfp.policy_path() is None


def test_an_explicit_policy_path_that_does_not_exist_is_not_a_broken_host(conn, home, monkeypatch):
    monkeypatch.setenv(kfp.ENV_POLICY, str(home / "nope.json"))
    tid = kb.create_task(conn, title="plain", assignee="coder")
    assert isinstance(tid, str)
    assert "filing_policy" not in _created_payload(conn, tid)


def test_the_policy_may_live_in_the_hermes_root_or_its_config_dir(home):
    (home / "filing_policy.json").write_text("{}", encoding="utf-8")
    assert kfp.policy_path() == str(home / "filing_policy.json")
    (home / "filing_policy.json").unlink()
    (home / "config").mkdir()
    (home / "config" / "filing_policy.json").write_text("{}", encoding="utf-8")
    assert kfp.policy_path() == str(home / "config" / "filing_policy.json")


# --- the governed case ------------------------------------------------------------------------

def test_a_governed_create_beyond_the_pool_is_refused_and_writes_nothing(
        conn, home, instrument, tmp_path, capsys):
    policy_file(tmp_path, instrument_path=instrument(merges=3, births={"platform-stl": 12}))
    before = _tasks(conn)
    answer = kb.create_task(conn, title="[design] one more", assignee="coder",
                            created_by="platform-stl")
    assert kfp.is_refusal(answer), answer
    # Falsy: the idiom a caller already writes ("if not task_id") catches it.
    assert not answer
    assert REFUSAL_LINE.match(str(answer)), str(answer)
    assert REFUSAL_LINE.match(str(answer)).group(4) == "platform-stl"
    assert _tasks(conn) == before, "a refused filing must leave no row behind"
    assert str(answer) in capsys.readouterr().err, "the refusal is printed where a producer reads"


def test_a_governed_create_inside_the_pool_is_admitted_and_states_its_reading(
        conn, home, instrument, tmp_path):
    policy_file(tmp_path, instrument_path=instrument(merges=44, births={"notice-lane": 40}))
    tid = kb.create_task(conn, title="a notice", assignee="coder", created_by="notice-lane")
    assert not kfp.is_refusal(tid), tid
    assert isinstance(tid, str) and tid.startswith("t_")
    record = _created_payload(conn, tid)["filing_policy"]
    assert record["status"] == "admitted" and record["reason"] == "under-pool"
    assert record["merges"] == 44 and record["precedence"] == 5


def test_an_exempt_surface_is_never_refused(conn, home, instrument, tmp_path):
    """The operator's filing survives a pool spent ten times over."""
    policy_file(tmp_path, instrument_path=instrument(merges=0, births={"platform-stl": 900}))
    tid = kb.create_task(conn, title="do this", assignee="coder", created_by="user")
    assert not kfp.is_refusal(tid)
    record = _created_payload(conn, tid)["filing_policy"]
    assert record["status"] == "admitted" and record["reason"] == "exempt"
    assert record["exemption"]["owner"] == "operator"


def test_an_unknown_creator_is_governed(conn, home, instrument, tmp_path):
    policy_file(tmp_path, instrument_path=instrument(merges=0, births={"platform-stl": 900}))
    answer = kb.create_task(conn, title="from a new lane", assignee="coder",
                            created_by="brand-new-lane")
    assert kfp.is_refusal(answer), answer
    assert REFUSAL_LINE.match(str(answer)).group(4) == "brand-new-lane"
    assert REFUSAL_LINE.match(str(answer)).group(5) == "5"


def test_a_missing_created_by_is_governed_too(conn, home, instrument, tmp_path):
    policy_file(tmp_path, instrument_path=instrument(merges=0, births={"platform-stl": 900}))
    answer = kb.create_task(conn, title="anonymous", assignee="coder")
    assert kfp.is_refusal(answer), answer
    assert REFUSAL_LINE.match(str(answer)).group(4) == "unknown"


def test_a_keyed_refresh_does_not_draw_on_the_pool(conn, home, instrument, tmp_path):
    """A second filing of an identity the board holds is not a birth (and never was)."""
    inst = instrument(merges=10, births={"notice-lane": 1})
    policy_file(tmp_path, instrument_path=inst)
    first = kb.create_task(conn, title="[defect] keyed", assignee="coder",
                           created_by="platform-stl", idempotency_key="k-1")
    assert not kfp.is_refusal(first), first
    # The pool is now spent many times over - and the keyed refresh still answers the id it
    # already holds: the hook sits AFTER the idempotency early-return, so no birth is judged.
    instrument(merges=0, births={"platform-stl": 900})
    again = kb.create_task(conn, title="[defect] keyed", assignee="coder",
                           created_by="platform-stl", idempotency_key="k-1")
    assert not kfp.is_refusal(again), again
    assert again == first
    assert _tasks(conn) == 1


# --- the bands --------------------------------------------------------------------------------

def test_a_defect_is_not_stranded_by_notices_that_over_filed(conn, home, instrument, tmp_path):
    """The pool is spent in BAND ORDER: later bands cannot refuse an earlier one."""
    policy_file(tmp_path, instrument_path=instrument(merges=2, births={"notice-lane": 50}))
    defect = kb.create_task(conn, title="[defect] prod unit down", assignee="coder",
                            created_by="platform-stl")
    assert not kfp.is_refusal(defect), defect
    notice = kb.create_task(conn, title="a fresh notice", assignee="coder",
                            created_by="notice-lane")
    assert kfp.is_refusal(notice), notice


def test_a_sev1_marker_is_exempt_and_needs_no_pool(conn, home, instrument, tmp_path):
    policy_file(tmp_path, instrument_path=instrument(merges=0, births={"platform-stl": 900}))
    tid = kb.create_task(conn, title="SEV1: gateway down", assignee="coder",
                         created_by="platform-stl")
    assert not kfp.is_refusal(tid), tid
    assert _created_payload(conn, tid)["filing_policy"]["reason"] == "sev1"


def test_an_exemption_past_its_date_is_still_honoured_but_stated(conn, home, instrument, tmp_path):
    policy_file(tmp_path, instrument_path=instrument(merges=0, births={"platform-stl": 900}),
                surfaces={"user": {"class": "exempt", "owner": "operator", "why": "w",
                                   "expires": "2020-01-01"}})
    tid = kb.create_task(conn, title="do this", assignee="coder", created_by="user")
    assert not kfp.is_refusal(tid)
    assert _created_payload(conn, tid)["filing_policy"]["exemption_expired"] is True


# --- fail-open, visibly -----------------------------------------------------------------------

def test_an_unreadable_instrument_admits_the_filing_and_records_why(
        conn, home, tmp_path, capsys):
    policy_file(tmp_path, instrument_path=tmp_path / "not-there.py")
    tid = kb.create_task(conn, title="a notice", assignee="coder", created_by="platform-stl")
    assert not kfp.is_refusal(tid), tid
    record = _created_payload(conn, tid)["filing_policy"]
    assert record["status"] == "unmeasured" and record["error"]
    assert "unmeasured" in capsys.readouterr().err


def test_an_instrument_that_raises_admits_the_filing(conn, home, tmp_path):
    path = tmp_path / "explodes.py"
    path.write_text(EXPLODES, encoding="utf-8")
    policy_file(tmp_path, instrument_path=path)
    tid = kb.create_task(conn, title="a notice", assignee="coder", created_by="platform-stl")
    assert not kfp.is_refusal(tid), tid
    assert _created_payload(conn, tid)["filing_policy"]["error"].startswith("RuntimeError")


def test_an_instrument_that_cannot_attribute_its_births_is_unmeasured(conn, home, tmp_path, capsys):
    """A counter that says HOW MANY but not WHO must not read as a pool of zero births.

    This is the stale-deploy case: the door module without `births_by_surface` would attribute
    every birth to nothing, every band would look unspent, and the door would admit the estate
    while believing it had measured it.
    """
    path = tmp_path / "no_breakdown.py"
    path.write_text(
        "def load_config(path):\n"
        "    return {}\n"
        "\n"
        "def count_births(since_epoch, boards_root=None, config=None, budget=None):\n"
        "    return {'births': 900, 'births_by_board': {}}\n"
        "\n"
        "def count_merges(since_epoch, names, config, budget=None):\n"
        "    return {'merges': 0}\n",
        encoding="utf-8")
    policy_file(tmp_path, instrument_path=path)
    tid = kb.create_task(conn, title="a notice", assignee="coder", created_by="platform-stl")
    assert not kfp.is_refusal(tid), tid
    record = _created_payload(conn, tid)["filing_policy"]
    assert record["status"] == "unmeasured"
    assert "births_by_surface" in record["error"]
    assert "unmeasured" in capsys.readouterr().err


def test_a_policy_that_is_not_json_admits_the_filing(conn, home, tmp_path):
    (home / "filing_policy.json").write_text("{not json", encoding="utf-8")
    tid = kb.create_task(conn, title="a notice", assignee="coder", created_by="platform-stl")
    assert not kfp.is_refusal(tid), tid
    assert _created_payload(conn, tid)["filing_policy"]["status"] == "unmeasured"


def test_a_disabled_policy_admits_everything(conn, home, instrument, tmp_path):
    policy_file(tmp_path, instrument_path=instrument(merges=0, births={"platform-stl": 900}),
                enabled=False)
    tid = kb.create_task(conn, title="a notice", assignee="coder", created_by="platform-stl")
    assert not kfp.is_refusal(tid)
    assert _created_payload(conn, tid)["filing_policy"]["reason"] == "disabled"


# --- the doors --------------------------------------------------------------------------------

def test_create_task_reported_returns_no_receipt_for_a_refusal(conn, home, instrument, tmp_path):
    policy_file(tmp_path, instrument_path=instrument(merges=0, births={"platform-stl": 900}))
    task_id, outcome = kb.create_task_reported(conn, title="a notice", assignee="coder",
                                               created_by="platform-stl")
    assert kfp.is_refusal(task_id)
    assert outcome is None


def test_the_tool_door_reports_a_refusal_in_its_error_envelope(
        conn, home, instrument, tmp_path, monkeypatch):
    """A worker must never believe it filed a card that does not exist."""
    from tools import kanban_tools

    policy_file(tmp_path, instrument_path=instrument(merges=0, births={"platform-stl": 900}))
    monkeypatch.setattr(kanban_tools, "_persisted_identity", lambda: "platform-stl")
    out = json.loads(kanban_tools._handle_create({"title": "a notice", "assignee": "coder"}))
    # The tool layer's own refusal envelope (an error, not a success carrying an id).
    assert "refused: budget spent" in json.dumps(out), out
    assert out.get("ok") is not True, out
    assert "task_id" not in out, out


def test_the_cli_door_exits_3_and_names_the_refusal(conn, home, instrument, tmp_path, capsys):
    policy_file(tmp_path, instrument_path=instrument(merges=0, births={"platform-stl": 900}))
    rc = _kanban(["create", "a notice", "--assignee", "coder",
                  "--created-by", "platform-stl"])
    assert rc == 3
    captured = capsys.readouterr()
    assert REFUSAL_LINE.match(captured.err.splitlines()[0]), captured.err
    assert "Created" not in captured.out


def test_the_cli_door_still_creates_when_the_pool_is_open(conn, home, instrument, tmp_path, capsys):
    policy_file(tmp_path, instrument_path=instrument(merges=44, births={"notice-lane": 1}))
    rc = _kanban(["create", "a notice", "--assignee", "coder",
                  "--created-by", "notice-lane"])
    assert rc == 0
    assert "Created t_" in capsys.readouterr().out
