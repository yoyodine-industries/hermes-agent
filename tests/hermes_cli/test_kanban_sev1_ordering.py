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

#: The same policy with the wired module's OWN punctuation class, ``"`` included.
SEV_BAND_QUOTED = r'''
import re

SEV = re.compile(r'(?i)^\s*[\[(\'"*\-]*\s*sev[ \t_\-]?([123])\b')
TIPS = {"1": 999000, "2": 995000, "3": 992000}


def band_birth(requested, assignee, board, title, body):
    m = SEV.match(title or "")
    if m:
        tip = TIPS[m.group(1)]
        return {"asked": requested, "applied": tip, "clamped": tip != int(requested),
                "sev": "sev%s" % m.group(1), "sev_priority": tip, "reason": "declared severity"}
    applied = min(max(int(requested), 600000), 900000)
    return {"asked": requested, "applied": applied, "clamped": applied != int(requested),
            "reason": "the board tier"}
'''

#: The wired ladder's ORDER, which is the point: a negative ask is exempted BEFORE the severity
#: is read, so ``applied`` comes back -1 while ``sev_priority`` is 999000.
SEV_BAND_EXEMPTS_NEGATIVES = r'''
import re

SEV = re.compile(r'(?i)^\s*[\[(\'\*\-]*\s*sev[ \t_\-]?([123])\b')
TIPS = {"1": 999000, "2": 995000, "3": 992000}


def band_birth(requested, assignee, board, title, body):
    asked = int(requested or 0)
    m = SEV.match(title or "")
    rank = "sev%s" % m.group(1) if m else ""
    if asked < 0:
        return {"asked": asked, "applied": asked, "clamped": False, "sev": rank,
                "sev_priority": TIPS[m.group(1)] if m else None,
                "reason": "below every band: exempt, never touched"}
    if rank:
        tip = TIPS[m.group(1)]
        return {"asked": asked, "applied": tip, "clamped": tip != asked, "sev": rank,
                "sev_priority": tip, "reason": "declared severity"}
    applied = min(max(asked, 600000), 900000)
    return {"asked": asked, "applied": applied, "clamped": applied != asked,
            "reason": "the board tier"}
'''

#: A board whose OWN reader decides what a declaration is: text the kernel's copy does not
#: recognise at all (``PAGER``) is a SEV1 here.
WIRED_READER = r'''
TIPS = {"sev1": 999000}


def sev_rank(title, body=""):
    return "sev1" if "PAGER" in (title or "") else ""


def band_birth(requested, assignee, board, title, body):
    rank = sev_rank(title, body)
    if rank:
        tip = TIPS[rank]
        return {"asked": requested, "applied": tip, "clamped": tip != int(requested),
                "sev": rank, "sev_priority": tip, "reason": "the wired reader declared it"}
    applied = min(max(int(requested), 600000), 800000)
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
    # 423 of 461 SEV filings ask 900000 - the tier top AND the dominant ask - so equality with
    # the ask is NOT evidence that nothing moved. Only the CLAMP FACT may say "clamped".
    assert kpp.decision(900000, {"asked": 900000, "applied": 900000},
                        domain_clamped=True) == "clamped"
    # with no record and no clamp fact, "asked" is the caller's fallback: nothing recorded a move
    assert kpp.decision(123, None) == "asked"
    assert kpp.decision(123, None, fallback="clamped") == "clamped"
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


# --- 7. the SECOND row-creation seam: the decomposer (review round 1, gap 1) ---------------

def test_a_decomposed_sev1_child_is_derived_and_the_armed_guard_admits_it(conn, home):
    """The fan-out is a filing: a SEV1-titled child must not land at the ordinary top.

    Measured before the fix: ``_insert_decomposed_child`` called the policy without ``conn=``,
    so a SEV1-titled child was stored at 900000 (``decision=clamped``, no designation) while
    the same card filed through ``create_task`` derived 999000. Wiring the connection alone
    would have ABORTED the fan-out (the guard refuses a tranche value with no ledger row), so
    the designation write had to become one helper both seams call.
    """
    from hermes_cli import kanban_db_graph as kbg

    _wire(home)
    root = kb.create_task(conn, title="decompose me", assignee="default", triage=True)
    kb.arm_priority_tranche_guard("default")
    children = kbg.decompose_triage_task(
        conn, root, root_assignee="default",
        children=[{"title": "SEV1 [gateway] child run failed", "assignee": "platform-coder"},
                  {"title": "SEV1 [gateway] second child", "assignee": "platform-coder"}],
    )
    assert children and len(children) == 2
    for cid in children:
        assert _priority(conn, cid) == 999000
        assert _designation(conn, cid) is not None
        record = _created_payload(conn, cid)["priority_policy"]
        assert record["decision"] == "derived"
        assert record["domain"]["admitted"] == "sev-token"


# --- 8. every rank the ladder maps, and the guard that must agree (gap 2) ------------------

def test_sev2_and_sev3_titles_derive_their_own_tip_and_the_armed_guard_admits_them(conn, home):
    """The band module maps SEV1/SEV2/SEV3 to 999000/995000/992000, so all three can be stored
    in the tranche. A guard that read only SEV1 would abort the INSERT of the other two."""
    _wire(home)
    kb.arm_priority_tranche_guard("default")
    sev2 = kb.create_task(conn, title="[SEV2] gateway latency", assignee="default", priority=0)
    sev3 = kb.create_task(conn, title="SEV3 host slow", assignee="default", priority=0)
    assert _priority(conn, sev2) == 995000
    assert _priority(conn, sev3) == 992000
    assert _designation(conn, sev2) is not None and _designation(conn, sev3) is not None
    assert _created_payload(conn, sev2)["priority_policy"]["decision"] == "derived"


def test_the_quoted_title_token_derives_and_is_admitted(conn, home):
    """``"SEV1" [x]`` - the ``"`` the kernel's first reader omitted from its punctuation class.

    A card the wired module raises to 999000 was then denied the value by the kernel's copy, so
    the INSERT landed at the ordinary edge with ``decision=clamped``.
    """
    _wire(home, SEV_BAND_QUOTED)
    tid = kb.create_task(conn, title='"SEV1" [gateway] down', assignee="default", priority=900000)
    assert _priority(conn, tid) == 999000
    assert _created_payload(conn, tid)["priority_policy"]["domain"]["admitted"] == "sev-token"


# --- 9. the single reading: the WIRED module answers (gap 2) -------------------------------

def test_the_kernel_copy_does_not_decide_what_a_declaration_is(conn, home):
    """``sev_rank`` on the board's module is the ONE reading.

    Review round 1: the kernel's copy omitted the ``"`` in the title token's punctuation class
    and read no sev2/sev3 labelled form, so the seam that raised a card and the seam that
    decided whether it may hold the value disagreed. Here the board's own reader declares a
    severity on text the kernel's copy does not recognise at all (``PAGER``): the card must
    still derive, be admitted, and be designated - the wired reader decides.
    """
    path = home / "band_policy_reader.py"
    path.write_text(WIRED_READER, encoding="utf-8")
    kb.write_board_metadata("default", priority_policy={"module": str(path),
                                                       "function": "band_birth"})
    kb.arm_priority_tranche_guard("default")
    assert kb.wired_sev_rank("default", "PAGER: gateway down") == "sev1"
    assert kb.declared_sev_rank("PAGER: gateway down", board="default") == "sev1"
    tid = kb.create_task(conn, title="PAGER: gateway down", assignee="default", priority=0)
    assert _priority(conn, tid) == 999000
    assert _designation(conn, tid) is not None
    assert _created_payload(conn, tid)["priority_policy"]["domain"]["admitted"] == "sev1"


def test_the_kernel_copy_still_reads_the_quoted_token_where_no_wired_reader_exists(conn, home):
    """The kernel's copy is byte-equal to the wired reader, ``"`` included."""
    assert kb.declared_sev_rank('"SEV1" [gateway] down') == "sev1"
    assert kb.declared_sev_rank("[SEV2] latency") == "sev2"
    assert kb.declared_sev_rank("x", "severity: SEV3") == "sev3"
    assert kb.declared_sev_rank("x", "priority-class: critical") == "sev1"
    assert kb.declared_sev_rank("cards are orderable BELOW SEV1 work") == ""


def test_sev2_can_ask_above_the_ceiling_and_the_ceiling_decision_is_recorded(conn, home):
    """The above-ceiling path emitted no ``decision`` at all (review round 1, gap 2)."""
    applied, record = kb.apply_above_tranche_guard(
        conn, 1000001, 999999, board="default", title="SEV2 [x] y",
        body="Operator-ask: default/1\n")
    assert applied == 999999
    assert record["above_tranche"]["decision"] == "clamped"
    merged = kb._merge_policy_record({"asked": 1000001, "applied": 999999},
                                     record)
    assert merged["decision"] == "clamped"


def test_a_title_token_sev1_above_the_ceiling_files_and_carries_its_designation(conn, home):
    """The above-ceiling door admitted a title-token SEV1 but wrote no designation.

    Its marker is read from the title, while the storage guard's SQL reads a
    ``label: value`` line - so the admission was invisible to the guard and the INSERT aborted
    with a raw ``IntegrityError`` instead of filing the incident. The birth-designation helper
    reads the above-ceiling record's marker too, so the row and its ledger admission are one.
    """
    _wire(home)
    kb.arm_priority_tranche_guard("default")
    tid = kb.create_task(conn, title="SEV1 [x] y", assignee="default", priority=1000001)
    assert _priority(conn, tid) == 999999
    payload = _created_payload(conn, tid)["priority_policy"]
    assert payload["above_tranche"]["decision"] == "clamped"
    assert payload["decision"] == "clamped"
    assert _designation(conn, tid) is not None


# --- 10. the refusal's vocabulary and the board that cannot derive (gap 3) -----------------

@pytest.mark.parametrize("asked", [10, 45, 999])
def test_a_sub_thousand_ask_is_no_ask_and_is_derived_not_refused(conn, home, asked):
    """997 live platform filings ask 10..999; 7 of those declare a severity.

    Those filers reach for "very urgent" on the retired ordinal scale, so refusing them at
    create would delete an incident card to protect a distinction nobody made. Derived, with
    the ignored ask RECORDED (``overridden_ask``).
    """
    _wire(home)
    tid = kb.create_task(conn, title="SEV1 [x] y", assignee="default", priority=asked)
    assert _priority(conn, tid) == 999000
    record = _created_payload(conn, tid)["priority_policy"]
    assert record["decision"] == "derived"
    assert record["overridden_ask"] == asked
    assert "no-ask vocabulary" in record["overridden_reason"]


def test_a_negative_ask_cannot_leave_a_sev1_below_routine_work(conn, home):
    """The wired ladder exempts a negative ask BEFORE it reads the severity, returning
    applied=-1 with sev_priority=999000 beside it - the same lie this card exists to end."""
    _wire(home, SEV_BAND_EXEMPTS_NEGATIVES)
    tid = kb.create_task(conn, title="SEV1 [x] y", assignee="default", priority=-1)
    assert _priority(conn, tid) == 999000
    record = _created_payload(conn, tid)["priority_policy"]
    assert record["decision"] == "derived"
    assert record["ladder_applied"] == -1


def test_an_unwired_board_stores_a_declared_sev1_at_the_ordinary_top_not_refused(conn, home):
    """No policy: there is no tip to derive, and a refusal there deletes the card.

    22 cards across 3 boards declare a severity with a sub-900000 priority; defcon has no
    ``priority_policy`` at all, so on that board the pre-fix rule had no derive path and the
    card would simply never exist.
    """
    tid = kb.create_task(conn, title="SEV1 [x] y", assignee="default", priority=699000)
    assert _priority(conn, tid) == kpp.ORDINARY_MAX
    record = _created_payload(conn, tid)["priority_policy"]
    assert record["decision"] == "derived"
    assert record["derived_by"] == "sev-floor"
    assert record["applied"] == kpp.ORDINARY_MAX
    assert _task_count(conn) == 1


def test_an_unwired_board_still_leaves_ordinary_work_untouched(conn, home):
    """The fallback is scoped to a DECLARED severity: an ordinary ask keeps today's bytes."""
    tid = kb.create_task(conn, title="Reconcile the board", assignee="default", priority=699000)
    assert _priority(conn, tid) == 699000
    assert "priority_policy" not in _created_payload(conn, tid)


def test_the_birth_designation_carries_an_iso_stamp(conn, home):
    """``designated_at`` is the ledger's TEXT stamp, the same one the designation door writes."""
    _wire(home)
    tid = kb.create_task(conn, title="SEV1 [x] y", assignee="default", priority=0)
    stamp = conn.execute(
        "SELECT designated_at FROM priority_designations WHERE task_id = ?", (tid,)
    ).fetchone()[0]
    assert isinstance(stamp, str) and stamp.endswith("Z") and "T" in stamp
