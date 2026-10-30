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
  exists to remove;
* a KEYED identity whose card was already closed is refused as ``duplicate-closed-key`` — the
  line names the closed card — and is admitted again only when the caller passes
  ``resurrect=true``. That leg is a board read, so it answers even when the pool cannot. It
  runs ONLY for an identity CLASS the policy data declares: an undeclared class (the estate's
  recurring ``failure:``/``dirty-shared-tree|`` conditions) is admitted and writes its row,
  and a policy that declares nothing refuses nothing.

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
        # The DATA that decides which keyed families the closure leg may refuse on. The
        # kernel carries no family of its own; a test can pass ``closure_leg={}`` to declare
        # nothing (and expects nothing refused on a key).
        "closure_leg": {"identities": {"pr-disposition": {"_why": "the measured episode family"}}},
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


# --- a keyed identity that was already closed -------------------------------------------------
#
# The kernel's idempotency early-return answers any row whose status is not ``archived``, so
# before this leg a producer that ticks on a schedule re-filed the same identity the moment its
# card was archived: 344 rows over 133 keys, 211 of them excess on the `pr-disposition:`
# family alone. These four tests are the contract: the duplicate is NAMED, the pool is not
# consulted to answer it, and the caller who means to re-open the identity says so.

#: REAL key shapes lifted from the live board, one per family the estate actually re-files.
#: The EPISODE family is the one the policy declares (a re-birth of the same disposition is a
#: duplicate); the CONDITION families name a state that legitimately recurs and must never be
#: refused — the estate's own filer split (PR #2273) decides the same way. Literals on
#: purpose: a test that derives its inputs from the artifact it tests cannot fail.
EPISODE_KEY = "pr-disposition:yoyodine-industries/yaan-platform#2268"
CONDITION_KEYS = (
    "failure:skills-publish:unit-x",
    "dirty-shared-tree|/opt/hermes_sandbox/yaan-platform|abc123",
    "landing-refused:yoyodine-industries/yaan-repo-control#150",
    "mass-crash:mass-crash-sweep:count=4,ended_at=1793062",
)

DUPLICATE_LINE = re.compile(
    r"^refused: duplicate-closed-key \(key=(\S+) closed=(t_\w+) status=(\w+) window=(\d+)h "
    r"surface=(\S+) precedence=(\d+); pass resurrect=true to file it again\)$"
)


def _archive(conn, task_id):
    assert kb.archive_task(conn, task_id, reason="superseded") is True


def _age(conn, task_id, hours):
    """Push a row's ``created_at`` back, so the window can be tested without a clock."""
    conn.execute("UPDATE tasks SET created_at = created_at - ? WHERE id = ?",
                 (int(hours * 3600), task_id))
    conn.commit()


def test_an_identity_class_is_the_token_before_the_first_separator():
    """The class rule the DATA is written against, stated once, here."""
    assert kfp.identity_class(EPISODE_KEY) == "pr-disposition"
    assert kfp.identity_class("dirty-shared-tree|/opt/x|abc123") == "dirty-shared-tree"
    assert kfp.identity_class("failure:skills-publish:unit-x") == "failure"
    assert kfp.identity_class("plain-key") is None
    assert kfp.identity_class(":leading-separator") is None
    assert kfp.identity_class(None) is None


def test_a_declaration_written_with_its_separator_is_the_same_class():
    """The data author may write ``pr-disposition:``; the kernel reads ``pr-disposition``.

    This is the token the two repositories agree on: the estate's policy file declares the
    family, this parser resolves a real key to it.
    """
    policy = {"closure_leg": {"identities": {"pr-disposition:": {}, "dirty-shared-tree|": {}}}}
    assert set(kfp.declared_identities(policy)) == {"pr-disposition", "dirty-shared-tree"}
    assert kfp.declared_identities({"closure_leg": {"identities": ["pr-disposition"]}}) == {
        "pr-disposition": {}}
    # ...and a policy that declares nothing declares nothing.
    assert kfp.declared_identities({}) == {}
    assert kfp.declared_identities({"closure_leg": None}) == {}
    assert kfp.declared_identities({"closure_leg": {}}) == {}
    assert kfp.declared_identities({"closure_leg": {"identities": {}}}) == {}


@pytest.mark.parametrize("key", CONDITION_KEYS)
def test_an_undeclared_condition_key_with_a_closed_row_is_admitted_and_writes_one_row(
        conn, home, instrument, tmp_path, key):
    """THE ARM THE FIRST CUT LACKED: a recurring condition is not an episode.

    The estate's fault filers key on a state that legitimately RECURS and read a refusal as
    "not filed", so a leg that refused these would silence live faults. Measured at review:
    1,199 condition-shaped re-filings against 6 episode-shaped on the live board.
    """
    policy_file(tmp_path, instrument_path=instrument(merges=44, births={"notice-lane": 1}))
    first = kb.create_task(conn, title="a condition", assignee="coder",
                           created_by="notice-lane", idempotency_key=key)
    assert not kfp.is_refusal(first), first
    _archive(conn, first)
    before = _tasks(conn)

    again = kb.create_task(conn, title="a condition", assignee="coder",
                           created_by="notice-lane", idempotency_key=key)

    assert not kfp.is_refusal(again), again
    assert isinstance(again, str) and again.startswith("t_") and again != first
    # The closed row is inside the window and holds the same key — the admission stands, and
    # it writes EXACTLY one row.
    assert _tasks(conn) == before + 1
    record = _created_payload(conn, again)["filing_policy"]
    assert record["status"] == "admitted"
    assert record["closure_leg_skipped"] == "undeclared-identity"


def test_a_policy_that_declares_no_identity_refuses_nothing(conn, home, instrument, tmp_path):
    """Fail-open by DATA: with no declared class, even the episode family is admitted."""
    policy_file(tmp_path, instrument_path=instrument(merges=44, births={"notice-lane": 1}),
                closure_leg={})
    first = kb.create_task(conn, title="a disposition", assignee="coder",
                           created_by="notice-lane", idempotency_key=EPISODE_KEY)
    assert not kfp.is_refusal(first), first
    _archive(conn, first)
    before = _tasks(conn)

    again = kb.create_task(conn, title="a disposition", assignee="coder",
                           created_by="notice-lane", idempotency_key=EPISODE_KEY)

    assert not kfp.is_refusal(again), again
    assert _tasks(conn) == before + 1
    assert _created_payload(conn, again)["filing_policy"]["closure_leg_skipped"] == \
        "undeclared-identity"


def test_the_declared_family_comes_from_the_data_not_from_this_module(conn, home, instrument,
                                                                     tmp_path):
    """The kernel hardcodes NO family: declare another class and the verdicts swap."""
    policy_file(tmp_path, instrument_path=instrument(merges=44, births={"notice-lane": 1}),
                closure_leg={"identities": {"sdlc-refusal": {}}})
    for key in (EPISODE_KEY, *CONDITION_KEYS, "sdlc-refusal:carrier-42"):
        tid = kb.create_task(conn, title="a keyed filing", assignee="coder",
                             created_by="notice-lane", idempotency_key=key)
        assert not kfp.is_refusal(tid), (key, tid)
        _archive(conn, tid)

    # The declared class is refused; the family the previous fixture declared is not.
    answer = kb.create_task(conn, title="a keyed filing", assignee="coder",
                            created_by="notice-lane", idempotency_key="sdlc-refusal:carrier-42")
    assert kfp.is_refusal(answer), answer
    assert DUPLICATE_LINE.match(str(answer)).group(1) == "sdlc-refusal:carrier-42"

    admitted = kb.create_task(conn, title="a keyed filing", assignee="coder",
                              created_by="notice-lane", idempotency_key=EPISODE_KEY)
    assert not kfp.is_refusal(admitted), admitted


def test_a_closed_key_is_refused_and_names_the_duplicate(conn, home, instrument, tmp_path, capsys):
    """The archived identity is not re-born: the door names the card that already held it."""
    policy_file(tmp_path, instrument_path=instrument(merges=44, births={"notice-lane": 1}))
    first = kb.create_task(conn, title="pr-disposition: yaan-platform#1", assignee="coder",
                           created_by="notice-lane", idempotency_key="pr-disposition:1")
    assert not kfp.is_refusal(first), first
    _archive(conn, first)
    before = _tasks(conn)

    answer = kb.create_task(conn, title="pr-disposition: yaan-platform#1", assignee="coder",
                            created_by="notice-lane", idempotency_key="pr-disposition:1")

    assert kfp.is_refusal(answer), answer
    assert not answer
    match = DUPLICATE_LINE.match(str(answer))
    assert match, str(answer)
    # ...and the line NAMES what it refused on: the key, the closed card and its status.
    assert match.group(1) == "pr-disposition:1"
    assert match.group(2) == first
    assert match.group(3) == "archived"
    assert match.group(5) == "notice-lane"
    assert _tasks(conn) == before, "a refused filing must leave no row behind"
    assert str(answer) in capsys.readouterr().err


def test_the_duplicate_refusal_is_answered_by_the_board_not_the_pool(
        conn, home, tmp_path, monkeypatch):
    """The leg is a BOARD read: an unmeasurable instrument cannot turn it into an admission."""
    # No stand-in instrument at all: the pool cannot be measured, which admits every OTHER
    # governed filing (see the unmeasured tests). The duplicate still does not get through.
    policy_file(tmp_path, instrument_path=tmp_path / "instrument_that_is_not_there.py")
    first = kb.create_task(conn, title="a keyed notice", assignee="coder",
                           created_by="notice-lane", idempotency_key="pr-disposition:dup-1")
    assert not kfp.is_refusal(first), first
    _archive(conn, first)

    answer = kb.create_task(conn, title="a keyed notice", assignee="coder",
                            created_by="notice-lane", idempotency_key="pr-disposition:dup-1")
    assert kfp.is_refusal(answer), answer
    match = DUPLICATE_LINE.match(str(answer))
    assert match, str(answer)
    assert match.group(2) == first


def test_a_closed_key_outside_the_window_is_a_new_birth(conn, home, instrument, tmp_path):
    """The window is the policy's own: a closure older than it is history, not a duplicate."""
    policy_file(tmp_path, instrument_path=instrument(merges=44, births={"notice-lane": 1}))
    first = kb.create_task(conn, title="a keyed notice", assignee="coder",
                           created_by="notice-lane", idempotency_key="pr-disposition:old-1")
    assert not kfp.is_refusal(first), first
    _archive(conn, first)
    _age(conn, first, hours=48)  # the policy's window is 24h

    again = kb.create_task(conn, title="a keyed notice", assignee="coder",
                           created_by="notice-lane", idempotency_key="pr-disposition:old-1")
    assert not kfp.is_refusal(again), again
    assert again != first
    assert _created_payload(conn, again)["filing_policy"]["reason"] == "under-pool"


def test_resurrect_admits_a_closed_key(conn, home, instrument, tmp_path):
    """``resurrect=true`` is the caller SAYING it means to re-open the identity."""
    policy_file(tmp_path, instrument_path=instrument(merges=44, births={"notice-lane": 1}))
    first = kb.create_task(conn, title="a keyed notice", assignee="coder",
                           created_by="notice-lane", idempotency_key="pr-disposition:re-1")
    assert not kfp.is_refusal(first), first
    _archive(conn, first)

    again = kb.create_task(conn, title="a keyed notice", assignee="coder",
                           created_by="notice-lane", idempotency_key="pr-disposition:re-1",
                           resurrect=True)
    assert not kfp.is_refusal(again), again
    assert isinstance(again, str) and again.startswith("t_") and again != first
    record = _created_payload(conn, again)["filing_policy"]
    assert record["status"] == "admitted"
    # The card STATES that a closed row was deliberately superseded, so the pool's arithmetic
    # (the same key twice inside one window) is auditable rather than mysterious.
    assert record["resurrected_from"] == {"id": first, "status": "archived"}


def test_a_live_key_is_returned_rather_than_refused(conn, home, instrument, tmp_path):
    """A key whose row is still LIVE is the kernel's own answer: the same id, never a refusal."""
    policy_file(tmp_path, instrument_path=instrument(merges=44, births={"notice-lane": 1}))
    first = kb.create_task(conn, title="a keyed notice", assignee="coder",
                           created_by="notice-lane", idempotency_key="live-1")
    again = kb.create_task(conn, title="a keyed notice", assignee="coder",
                           created_by="notice-lane", idempotency_key="live-1")
    assert not kfp.is_refusal(again), again
    assert again == first
    assert _tasks(conn) == 1


def test_the_tool_and_cli_doors_pass_the_key_through(conn, home, instrument, tmp_path):
    """Both doors reach the same leg: the CLI refuses, and its refusal is the same line."""
    policy_file(tmp_path, instrument_path=instrument(merges=44, births={"notice-lane": 1}))
    first = kb.create_task(conn, title="a keyed notice", assignee="coder",
                           created_by="notice-lane", idempotency_key="pr-disposition:door-1")
    _archive(conn, first)

    rc = _kanban(["create", "a keyed notice", "--assignee", "coder",
                  "--created-by", "notice-lane", "--idempotency-key", "pr-disposition:door-1"])
    assert rc == 3
    # And --resurrect gets through the same CLI door.
    rc = _kanban(["create", "a keyed notice", "--assignee", "coder",
                  "--created-by", "notice-lane", "--idempotency-key", "pr-disposition:door-1",
                  "--resurrect"])
    assert rc == 0


def test_a_closed_key_is_admitted_again_when_the_surface_is_exempt(conn, home, instrument,
                                                                  tmp_path):
    """The exempt classes are never refused — not even by the closure leg."""
    policy_file(tmp_path, instrument_path=instrument(merges=0, births={"platform-stl": 900}))
    first = kb.create_task(conn, title="operator work", assignee="coder", created_by="user",
                           idempotency_key="op-1")
    assert not kfp.is_refusal(first), first
    _archive(conn, first)
    again = kb.create_task(conn, title="operator work", assignee="coder", created_by="user",
                           idempotency_key="op-1")
    assert not kfp.is_refusal(again), again
    assert again != first


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


# --- the SECOND writer: the decompose seam (review round 3) -----------------------------------
#
# A decomposed child is born at ``kanban_db_graph.decompose_triage_task``, not through
# ``create_task``. A door wired into one of the kernel's two row-creation seams caps nothing: the
# fan-out writes N children in one go, and the auto-decomposer alone wrote 2,093 of them in the
# measured day. These tests hold the seam to the SAME door: one decision for the whole fan-out,
# the pool paying per birth, the refusal carried to the decomposer's own output, and — the part
# that costs the most thought — ALL OR NOTHING, so a refused decomposition leaves the root
# exactly where the next pass can pick it up.

from hermes_cli.kanban_db_graph import decompose_triage_task  # noqa: E402
from hermes_cli import kanban_decompose as kdecomp  # noqa: E402
from hermes_cli import kanban_triage_drain as ktdrain  # noqa: E402

#: A fan-out's refusal is the SAME line plus the size it claimed: one decision, N births.
BATCH_REFUSAL_LINE = re.compile(
    r"^refused: budget spent \(births=(\d+) merges=(\d+) window=(\d+)h surface=(\S+) "
    r"precedence=(\d+) batch=(\d+)\)$"
)


def _children(n: int, assignee: str = "coder") -> list[dict]:
    return [{"title": f"child {i}", "body": "work", "assignee": assignee, "parents": []}
            for i in range(n)]


def _triage_root(conn, title="rough idea", body=None) -> str:
    return kb.create_task(conn, title=title, body=body, triage=True)


def _decomposer_policy(tmp_path, instrument, *, merges, births, precedence=4):
    """The estate's data shape for the fan-out's own identity: DECLARED, not fallback."""
    return policy_file(
        tmp_path, instrument_path=instrument(merges=merges, births=births),
        surfaces={
            "auto-decomposer": {"class": "governed", "precedence": precedence,
                                "owner": "platform-stl",
                                "_why": "the automatic fan-out: 2,093 children in the measured "
                                        "day"},
            "decomposer": {"class": "governed", "precedence": precedence,
                           "owner": "platform-stl", "_why": "the CLI/drain fan-out"},
        },
    )


def _arm(monkeypatch, tmp_path, instrument, *, merges, births, precedence=4):
    """Turn the budget on AFTER the root exists: the root's own filing is not the subject here.

    (A test that armed the policy first would be measuring ``create_task`` — the ROOT's birth
    spends the pool and would be refused before any fan-out was attempted.)
    """
    monkeypatch.setenv(kfp.ENV_POLICY, str(_decomposer_policy(
        tmp_path, instrument, merges=merges, births=births, precedence=precedence)))


def _filing_reading(conn, task_id) -> dict | None:
    return _created_payload(conn, task_id).get("filing_policy")


def test_a_fanout_writes_no_row_when_the_pool_cannot_pay_for_every_child(
        conn, home, instrument, tmp_path, monkeypatch):
    """The refusal is decided BEFORE the write: no child, no link, no event, root still triage."""
    tid = _triage_root(conn, title="[design] split the picker")
    _arm(monkeypatch, tmp_path, instrument, merges=5, births={"auto-decomposer": 5})
    before = _tasks(conn)

    refused = decompose_triage_task(conn, tid, root_assignee="orch",
                                    children=_children(3), author="auto-decomposer")

    assert kfp.is_refusal(refused), refused
    line = str(refused)
    assert line.startswith("refused: budget spent"), line
    assert BATCH_REFUSAL_LINE.match(line), line
    # The whole fan-out is one ask, and the line says how many births it claimed.
    assert line.endswith(" batch=3)"), line
    assert _tasks(conn) == before, "a refused decomposition wrote a row"
    assert kb.get_task(conn, tid).status == "triage", "the root left triage without a fan-out"
    assert conn.execute("SELECT COUNT(*) AS n FROM task_events WHERE task_id = ? "
                        "AND kind = 'decomposed'", (tid,)).fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM task_links WHERE parent_id = ?",
                        (tid,)).fetchone()["n"] == 0


def test_the_batch_arithmetic_is_the_headroom_not_merely_a_nonzero_pool(
        conn, home, instrument, tmp_path, monkeypatch):
    """Headroom for ONE is not headroom for THREE — the seam may not be born past the pool."""
    tid = _triage_root(conn, title="[design] split the picker")
    tid2 = _triage_root(conn, title="[design] split the other picker")
    _arm(monkeypatch, tmp_path, instrument, merges=5, births={"auto-decomposer": 3})
    # One filing still fits: 3 births spent, 2 left in a 5-merge pool.
    single = kb.create_task(conn, title="[design] one design", assignee="coder",
                            created_by="auto-decomposer")
    assert isinstance(single, str) and single.startswith("t_")

    refused = decompose_triage_task(conn, tid, root_assignee="orch",
                                    children=_children(3), author="auto-decomposer")
    assert kfp.is_refusal(refused), refused
    assert "births=3 merges=5" in str(refused), str(refused)
    assert str(refused).endswith(" batch=3)"), str(refused)

    # And the fit is exact, not approximate: two rows still fit the two units left.
    child_ids = decompose_triage_task(conn, tid2, root_assignee="orch",
                                      children=_children(2), author="auto-decomposer")
    assert not kfp.is_refusal(child_ids), child_ids
    assert len(child_ids) == 2


def test_a_fanout_that_fits_is_born_as_before_and_carries_its_filing_reading(
        conn, home, instrument, tmp_path, monkeypatch):
    monkeypatch.setenv(kfp.ENV_POLICY, str(_decomposer_policy(
        tmp_path, instrument, merges=44, births={"auto-decomposer": 2})))
    tid = _triage_root(conn, title="[design] split the picker")

    child_ids = decompose_triage_task(conn, tid, root_assignee="orch",
                                      children=_children(3), author="auto-decomposer")

    assert isinstance(child_ids, list) and len(child_ids) == 3, child_ids
    assert kb.get_task(conn, tid).status == "todo"
    reading = _filing_reading(conn, child_ids[0])
    assert reading is not None, "an admitted fan-out states which pool it drew from"
    assert reading["status"] == "admitted" and reading["surface"] == "auto-decomposer"
    assert reading["precedence"] == 4
    assert reading["batch"] == 3, reading


def test_an_exempt_root_carries_its_whole_fanout_over_a_spent_pool(
        conn, home, instrument, tmp_path, monkeypatch):
    """An alarm is never budgeted - and its decomposition is the alarm's own work."""
    monkeypatch.setenv(kfp.ENV_POLICY, str(_decomposer_policy(
        tmp_path, instrument, merges=1, births={"auto-decomposer": 40})))
    tid = _triage_root(conn, title="sev1: gateway is down")
    child_ids = decompose_triage_task(conn, tid, root_assignee="orch",
                                      children=_children(4), author="auto-decomposer")
    assert isinstance(child_ids, list) and len(child_ids) == 4, child_ids
    assert _filing_reading(conn, child_ids[0])["status"] == "admitted"


def test_a_host_without_a_policy_leaves_the_fanout_seam_untouched(conn, home):
    tid = _triage_root(conn)
    child_ids = decompose_triage_task(conn, tid, root_assignee="orch",
                                      children=_children(2), author="auto-decomposer")
    assert isinstance(child_ids, list) and len(child_ids) == 2
    assert "filing_policy" not in _created_payload(conn, child_ids[0])


def test_the_decomposer_reports_the_refusal_in_its_own_outcome(
        conn, home, instrument, tmp_path, monkeypatch):
    """Not a no-op, not a DB error: the producer's own outcome carries the door's line."""
    tid = _triage_root(conn, title="[design] split the picker")
    _arm(monkeypatch, tmp_path, instrument, merges=5, births={"auto-decomposer": 5})
    routing = kdecomp._Routing(orchestrator="orch", default_assignee="coder",
                               auto_promote=True, roster=[], valid_names={"coder"})
    parsed = {"fanout": True, "tasks": _children(3)}

    outcome = kdecomp._apply_fanout(tid, parsed, routing, "auto-decomposer")

    assert outcome.ok is False
    assert outcome.refused is True
    assert BATCH_REFUSAL_LINE.match(outcome.reason), outcome.reason
    assert kb.get_task(conn, tid).status == "triage"


def test_the_drain_reports_the_refusal_as_its_own_action(
        conn, home, instrument, tmp_path, monkeypatch):
    """The triage drain is a producer too: its Action says what the door decided."""
    tid = _triage_root(conn, title="[design] split the picker")
    _arm(monkeypatch, tmp_path, instrument, merges=5, births={"auto-decomposer": 5})
    monkeypatch.setattr(ktdrain, "_call_aux", lambda *a, **k: (
        json.dumps({"fanout": True, "tasks": [{"title": f"child {i}"} for i in range(3)]}), ""))
    routing = type("R", (), {"orchestrator": "orch", "default_assignee": "coder",
                             "roster": [], "valid_names": {"coder"}})()

    action = ktdrain._decompose_capped(
        kb.get_task(conn, tid), routing, ktdrain.DrainCaps(),
        author="auto-decomposer", remaining_budget=6, timeout=5, dry_run=False)

    assert action.disposition == "refuse", action
    assert BATCH_REFUSAL_LINE.match(action.reason), action.reason
    assert action.children_created == 0
    assert kb.get_task(conn, tid).status == "triage"


def test_the_dispatcher_says_a_budget_refusal_at_warning_level(caplog):
    """A refusal is a DECISION: debug-only would be the silent failure the ruling forbids."""
    from gateway.kanban_watchers_dispatcher import _KanbanDispatcher

    line = ("refused: budget spent (births=12 merges=12 window=24h "
            "surface=auto-decomposer precedence=4 batch=3)")
    stub = type("D", (), {"decompose_task": staticmethod(
        lambda tid, author=None: kdecomp.DecomposeOutcome(tid, False, line, refused=True))})()

    with caplog.at_level("DEBUG"):
        assert _KanbanDispatcher._decompose_one(stub, "platform", "t_dead") == 0

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("refused by the filing budget" in r.getMessage() for r in warnings), \
        [r.getMessage() for r in caplog.records]

