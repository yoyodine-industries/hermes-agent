"""A SEV1 is orderable ABOVE routine work, by construction (card t_4301046d).

The measured defect this file pins. On a board whose policy DERIVES a severity value, the
create seam clamped that value back to the ordinary domain edge - and the domain edge is the
top of the routine bands, so a SEV1 came to be stored LEVEL WITH ordinary work and ordered
behind it. The ``created`` event proved it without any inference from ordering: 237 SEV1
payloads carried ``sev_priority`` 999000 beside ``applied`` 900000, and no field said which of
the two the card was ordered at.

Three properties carry the repair, and the third is the one that keeps it from breaking the
fleet:

1. a card whose own text declares a severity may hold the value its severity derives - the
   domain edge is a claim about DESIGNATION, and a declared severity is not a designation the
   card lacks;
2. the value is only admissible because the row then CARRIES the admission in the ledger the
   storage guard reads, so the reserved tranche still holds only designated rows and an armed
   board cannot abort the INSERT (an abort inside the dispatch loop dispatches nothing);
3. ordinary work is untouched: a routine card that asks into the tranche is clamped exactly as
   before and NOT refused - 503 live filings on the platform board have always asked 990000+
   for ordinary work and had it clamped into their tier, and a refusal there would fail them.
"""

from __future__ import annotations

import argparse
import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_priority_policy as kpp


#: The band policy the fleet runs, reduced to the ONE clause this card is about: a declared
#: severity DERIVES its tip, and routine work is projected into its tier.
SEV_BAND = '''
import re

SEV = re.compile(r"(?i)^\\s*[\\[('\\*\\-]*\\s*sev[ \\t_\\-]?([123])\\b")
TIPS = {"1": 999000, "2": 995000, "3": 992000}


def band_birth(requested, assignee, board, title, body):
    m = SEV.match(title or "")
    if m:
        tip = TIPS[m.group(1)]
        return {"asked": requested, "applied": tip, "clamped": tip != int(requested),
                "sev1": m.group(1) == "1", "sev": "sev%s" % m.group(1),
                "sev_priority": tip, "reason": "declared severity"}
    applied = min(max(int(requested), 600000), 900000)
    return {"asked": requested, "applied": applied, "clamped": applied != int(requested),
            "reason": "the board tier"}
'''


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A temp HERMES_HOME: its own boards, and no board wiring of any kind."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
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


def _wire(home, source=SEV_BAND):
    path = home / "band_policy.py"
    path.write_text(source, encoding="utf-8")
    kb.write_board_metadata("default", priority_policy={"module": str(path),
                                                        "function": "band_birth"})
    return path


def _priority(conn, task_id):
    return conn.execute("SELECT priority FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]


def _created_payload(conn, task_id):
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'created' "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    return json.loads(row["payload"])


def _task_count(conn):
    return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


def _designation(conn, task_id):
    return conn.execute(
        "SELECT priority, authority, reason FROM priority_designations WHERE task_id = ?",
        (task_id,),
    ).fetchone()


# --- 1. the entitlement predicate ---------------------------------------------------------

def test_the_title_token_is_the_fleets_second_sev_convention(conn, home):
    """``SEV1 [flow] run failed`` is a declaration, and prose is not.

    The policy module reads the PREFIX as a severity (that is where the 999000 came from);
    the entitlement resolver has to read the same text or it rejects the card the policy just
    raised, which is exactly the state the 420 SEV1 filings measured in.
    """
    assert kb.tranche_entitlement(conn, title="SEV1 [deploy-train] run 1 failed") == "sev-token"
    assert kb.tranche_entitlement(conn, title="[SEV2] gateway down") == "sev-token"
    assert kb.tranche_entitlement(conn, title="SEV3 host slow") == "sev-token"
    # prose is not a declaration: the token has to be the prefix
    assert kb.tranche_entitlement(conn, title="cards are orderable BELOW SEV1 work") == ""
    assert kb.tranche_entitlement(conn, title="plain card") == ""
    # the labelled line is the other, stronger reading, and it keeps its own name
    assert kb.tranche_entitlement(conn, title="x", body="Severity: SEV1") == "sev1"


# --- 2. the card lands ABOVE routine work -------------------------------------------------

@pytest.mark.parametrize("asked", [0, 1, 5, 900000, 950000, 995000, 999999])
def test_a_sev1_card_lands_above_the_routine_band_whatever_it_asks(conn, home, asked):
    """Any admissible ask lands at the severity tip - strictly above every routine band."""
    _wire(home)
    tid = kb.create_task(conn, title="SEV1 [gateway] run 20261117-0900 failed",
                         assignee="platform-coder", priority=asked)
    assert _priority(conn, tid) == 999000
    assert _priority(conn, tid) > kpp.ORDINARY_MAX
    record = _created_payload(conn, tid)["priority_policy"]
    # ... and the event says WHICH rule decided, not just what it decided
    assert record["decision"] == "derived"
    assert record["sev_priority"] == 999000
    assert record["domain"]["admitted"] == "sev-token"
    assert record["domain"]["applied"] == 999000


def test_the_derived_value_survives_the_domain_clamp_without_a_floor(conn, home):
    """The platform board declares NO floor, and that was the hole.

    ``clamp_to_domain`` admitted a tranche value only when the board's floor sat on the
    tranche boundary; a board with ``floor: null`` - the platform board, where the SEV1
    evidence in this card was measured - clamped every severity value back to 900000.
    """
    _wire(home)
    assert kb.board_priority_policy("default") is not None
    tid = kb.create_task(conn, title="SEV1 [x] y", assignee="platform-coder", priority=900000)
    assert _priority(conn, tid) == 999000


# --- 3. the admission is carried, designed, and durable -----------------------------------

def test_the_birth_admission_writes_the_designation_the_storage_guard_reads(conn, home):
    """The tranche still holds only DESIGNATED rows, and the restore value is the ordinary one."""
    _wire(home)
    tid = kb.create_task(conn, title="SEV1 [x] y", assignee="platform-coder", priority=900000)
    row = _designation(conn, tid)
    assert row is not None, "a tranche value with no designation row is what door 3 aborts on"
    priority, authority, reason = row
    assert authority == "declared-sev"
    assert "SEV1" in reason and "sev-token" in reason
    # the ledger keeps the ORDINARY value, so revoking returns the card to the domain edge
    assert priority == kpp.ORDINARY_MAX


def test_the_armed_storage_guard_admits_the_sev1_birth_and_still_refuses_a_bare_one(conn, home):
    """Crash-safety, measured on the real trigger rather than assumed.

    ``arm_priority_tranche_guard`` installs the DDL every door has to satisfy. If the birth
    seam stored 999000 without the ledger row, this INSERT would abort - and an abort in the
    dispatcher's reclaim phase dispatches nothing at all (measured 2026-10-01: 6.5 h).
    """
    _wire(home)
    kb.arm_priority_tranche_guard("default")
    tid = kb.create_task(conn, title="SEV1 [x] y", assignee="platform-coder", priority=900000)
    assert _priority(conn, tid) == 999000

    # the guard is ARMED: the same value with no evidence is refused by the database itself
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO tasks (id, title, body, assignee, status, priority, created_by) "
            "VALUES ('bare', 'just a card', NULL, 'coder', 'ready', 999000, 'test')")


# --- 4. the contradiction is named, not clamped over --------------------------------------

def test_a_sev1_ask_that_contradicts_the_declaration_is_refused_by_name(conn, home):
    """The card's own example: "SEV1" and "699000" cannot both be true.

    Refused, not silently clamped - the two readings of one card disagree and only the filer
    can say which was meant. Nothing lands, so no reader is left to discover the lie later.
    """
    _wire(home)
    with pytest.raises(kb.SeverityContradiction) as err:
        kb.create_task(conn, title="SEV1 [x] y", assignee="platform-coder", priority=699000)
    message = str(err.value)
    assert "declares SEV1" in message
    assert "699000" in message and "900000" in message
    assert "no priority" in message          # the fix is in the message
    assert _task_count(conn) == 0


def test_a_bare_ask_is_not_a_contradiction(conn, home):
    """``0`` is the CLI default, i.e. no ask at all: the declaration decides, nothing is refused."""
    _wire(home)
    tid = kb.create_task(conn, title="SEV1 [x] y", assignee="platform-coder", priority=0)
    assert _priority(conn, tid) == 999000


# --- 5. routine work is untouched --------------------------------------------------------

def test_routine_work_asking_into_the_tranche_is_still_clamped_and_not_refused(conn, home):
    """503 live filings ask 990000+ for ORDINARY work and have always been clamped into tier.

    A refusal here would fail every one of them, so the contradiction rule is scoped to a card
    that DECLARES a severity - not to an over-ask, which the tier clamp handles and records.
    """
    _wire(home)
    tid = kb.create_task(conn, title="Reconcile the board", assignee="default", priority=990000)
    assert _priority(conn, tid) == 900000
    record = _created_payload(conn, tid)["priority_policy"]
    assert record["decision"] == "clamped"
    assert "admitted" not in record.get("domain", {})
    assert _designation(conn, tid) is None


# --- 6. the decision field, at its own level ---------------------------------------------

def test_decision_names_the_rule_that_placed_the_card():
    derived = {"asked": 900000, "applied": 999000, "sev_priority": 999000}
    clamped = {"asked": 600000, "applied": 799998, "sev_priority": None}
    asked = {"asked": 799998, "applied": 799998, "sev_priority": None}
    assert kpp.decision(999000, derived) == "derived"
    assert kpp.decision(799998, clamped) == "clamped"
    assert kpp.decision(799998, asked) == "asked"
    assert kpp.decision(123, None) == "clamped"          # the default fallback
    assert kpp.decision(123, None, fallback="asked") == "asked"
    # a record that states no ask cannot say more than the caller already knows
    assert kpp.decision(123, {"applied": 123}, fallback="asked") == "asked"
    assert kpp.decision(900000, {"applied": 123}) == "clamped"


def test_the_clamp_admits_a_tranche_value_only_with_a_marker():
    """No marker, no admission - the clamp still holds the line for an ordinary over-ask."""
    assert kpp.clamp_to_domain(999000, None, None)[0] == kpp.ORDINARY_MAX
    assert kpp.clamp_to_domain(999000, None, None, marker="")[0] == kpp.ORDINARY_MAX
    admitted, record = kpp.clamp_to_domain(999000, None, None, marker="sev-token")
    assert admitted == 999000
    assert record["domain"]["admitted"] == "sev-token"
    assert record["clamped"] is False
