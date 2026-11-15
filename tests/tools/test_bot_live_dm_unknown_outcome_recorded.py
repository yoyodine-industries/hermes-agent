"""The unknown-outcome receipt must BE a record, not a promise about a temp file.

Measured on the live store (5,833 records, 2026-11-15): a delivery answered
``status=ambiguous`` with ``"… queue_full. Do not resend."`` and ``evidence_file`` under TMPDIR
had NO record — ``queued`` stayed at 47 while a receipt claimed retention, and its id was a
fingerprint of a path the 24 h sweep then deleted, so afterwards the delivery could not even be
looked up. ``ambiguous`` is representable in the store (49 live records carry it), so the defect
was never a missing status: this branch returned the receipt and wrote nothing.

The negative control asserts against the STORE, before and after: ``records_in_store`` 0 -> 1.
A test that only inspects the returned JSON passes on the leaking code.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import bot_live_delivery as store
from tools import bot_mode_dm
from tools.bot_delivery_queue import QueueFullError
from tools.bot_live_delivery import AuthorityNotReady

OWNER = {"profile_home": "/tmp/profile-home", "session_id": "tip-session",
         "lease_id": "lease-1", "live_session_id": "tip-session"}


def _dm(tmp_path: Path, message: str = "the payload that must survive") -> str:
    dm_file = tmp_path / "hermes-dm" / "dm-fixture.txt"
    dm_file.parent.mkdir(parents=True, exist_ok=True)
    dm_file.write_text(message, encoding="utf-8")
    (tmp_path / "hermes-dm" / "dm-fixture.txt.live.json").write_text(json.dumps({
        "owner": dict(OWNER, profile_home=str(tmp_path)),
        "message": message,
        "delivery_id": bot_mode_dm._dm_delivery_id(dm_file),
        "author": {"Agent": "platform-coder"},
    }), encoding="utf-8")
    return str(dm_file)


def _records(home: Path, delivery_id: str) -> int:
    """Count this id's tickets in the store — the before/after instrument."""
    return len(list((home / "runtime" / "bot_live_delivery").glob(f"{delivery_id}.json")))


def test_unknown_outcome_writes_the_record_it_promises(tmp_path):
    dm_file = _dm(tmp_path)
    delivery_id = bot_mode_dm._dm_delivery_id(dm_file)
    assert _records(tmp_path, delivery_id) == 0, "precondition: the leak is the absent record"

    receipt = json.loads(bot_mode_dm._live_outcome_unknown(
        dm_file, QueueFullError("platform-stl", limit=32), profile_home=tmp_path))

    assert _records(tmp_path, delivery_id) == 1, "the receipt promised retention and wrote nothing"
    record = store.read_delivery_result(tmp_path, delivery_id)
    assert record is not None, "the id the receipt names does not resolve in the store"
    assert record["status"] == "ambiguous"
    assert isinstance(record["created_at"], int) and isinstance(record["sequence"], int)
    assert record["payload"] == "the payload that must survive", "the message itself must survive"
    assert record["source_path"] == dm_file


def test_the_receipt_states_retention_with_its_term_and_names_the_rung(tmp_path):
    dm_file = _dm(tmp_path)
    receipt = json.loads(bot_mode_dm._live_outcome_unknown(
        dm_file, QueueFullError("platform-stl", limit=32), profile_home=tmp_path))

    assert receipt["retained"].startswith("at "), receipt["retained"]
    assert Path(receipt["retained"][3:]).is_file(), "the named path must exist"
    assert "durable" in receipt["retention"]
    assert receipt["temp_copy"] == dm_file, "a remaining temp copy is named, with its expiry"
    assert receipt["temp_copy_expires_at"].endswith("Z")
    assert receipt["rung"].startswith("queue_full("), "the rung names the gate, not a bare string"


def test_an_authority_refusal_and_a_full_queue_do_not_read_the_same(tmp_path):
    dm_file = _dm(tmp_path)
    authority = bot_mode_dm._ambiguous_rung(dm_file, AuthorityNotReady("starting"))
    full = bot_mode_dm._ambiguous_rung(dm_file, QueueFullError("platform-stl", limit=32))
    other = bot_mode_dm._ambiguous_rung(dm_file, RuntimeError("something else"))

    assert authority == "authority_not_ready(starting)"
    assert full.startswith("queue_full(") and full.endswith("/32)"), full
    assert other == "unattributed_rung(RuntimeError)", "an unclassified cause says so, not 'queue full'"


def test_census_counts_the_clockless_and_lists_unresolved_ambiguous_ids(tmp_path):
    dm_file = _dm(tmp_path)
    delivery_id = bot_mode_dm._dm_delivery_id(dm_file)
    bot_mode_dm._live_outcome_unknown(dm_file, QueueFullError("p", limit=8), profile_home=tmp_path)

    counts = store.census(tmp_path)
    assert counts["ambiguous_ids"] == [delivery_id]
    assert counts["ambiguous_unresolved"] == [], "an ambiguous receipt with no record is the loss"
    assert counts["clockless"] == 0, "every record this writer produces carries a clock"


def test_a_discovery_pin_that_never_arrived_still_leaves_a_readable_record(tmp_path):
    """The record's value is the payload and the clock, so a missing pin must not silence it."""
    dm_file = tmp_path / "hermes-dm" / "dm-nopin.txt"
    dm_file.parent.mkdir(parents=True)
    dm_file.write_text("payload without an intent", encoding="utf-8")

    receipt = json.loads(bot_mode_dm._live_outcome_unknown(
        str(dm_file), RuntimeError("no intent"), profile_home=tmp_path))
    delivery_id = bot_mode_dm._dm_delivery_id(str(dm_file))

    assert receipt["retained"] is not None, receipt.get("retention_error")
    assert _records(tmp_path, delivery_id) == 1
    from tools import bot_live_delivery
    assert bot_live_delivery._scan_read(Path(receipt["retained"][3:])) is not None, \
        "a ticket the scans cannot read is the hole this closes"


def _admit(home: Path, delivery_id: str, status="terminal", outcome="completed") -> None:
    """Write the TARGET home's admission row — the witness the receipt must consult (A1)."""
    import sqlite3

    db = home / "state.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE session_admissions (seq INTEGER PRIMARY KEY, admission_id TEXT,"
                 " request_id TEXT, status TEXT, outcome TEXT)")
    conn.execute("INSERT INTO session_admissions (admission_id, request_id, status, outcome)"
                 " VALUES (?,?,?,?)", ("4605958e67f64d409af5a012e9208800",
                                       f"bot:{delivery_id}", status, outcome))
    conn.commit()
    conn.close()


def test_no_admission_row_in_the_target_home_means_re_send_not_dont_resend(tmp_path):
    """A2: the advice is a property of the LOOKUP. Absent row => not resending LOSES the message."""
    dm_file = _dm(tmp_path)
    receipt = json.loads(bot_mode_dm._live_outcome_unknown(
        dm_file, QueueFullError("platform-stl", limit=32), profile_home=tmp_path))

    assert receipt["admission"] is None
    assert "Do not resend" not in receipt["error"], "the advice is inverted without an admission row"
    assert "NEVER ADMITTED: RE-SEND" in receipt["error"]
    assert receipt["retained"], "the payload path must be carried so a re-send is possible"


def test_an_admission_row_in_the_target_home_licenses_do_not_resend(tmp_path):
    dm_file = _dm(tmp_path)
    delivery_id = bot_mode_dm._dm_delivery_id(dm_file)
    _admit(tmp_path, delivery_id)

    receipt = json.loads(bot_mode_dm._live_outcome_unknown(
        dm_file, RuntimeError("dial died"), profile_home=tmp_path))

    assert receipt["admission"] == {"admission_id": "4605958e67f64d409af5a012e9208800",
                                    "status": "terminal", "outcome": "completed"}
    assert "Do not resend" in receipt["error"]
    assert "terminal/completed" in receipt["error"], "the row's own words, not a paraphrase"


def test_the_lookup_reads_the_target_home_not_the_callers(tmp_path):
    """A5d: the case that produced the amendment — caller home has nothing, target home has the row."""
    caller, target = tmp_path / "caller", tmp_path / "target"
    target.mkdir()
    dm_file = _dm(caller)
    delivery_id = bot_mode_dm._dm_delivery_id(dm_file)
    _admit(target, delivery_id)

    receipt = json.loads(bot_mode_dm._live_outcome_unknown(
        dm_file, QueueFullError("platform-stl", limit=8), profile_home=target))

    assert receipt["admission"]["admission_id"] == "4605958e67f64d409af5a012e9208800"
    assert "Do not resend" in receipt["error"]
    assert bot_mode_dm._target_admission(delivery_id, caller) is None, \
        "a sender-side lookup answers 'never admitted' for a delivered message"


def test_an_empty_cause_is_named_never_blank(tmp_path):
    dm_file = _dm(tmp_path)
    receipt = json.loads(bot_mode_dm._live_outcome_unknown(dm_file, "", profile_home=tmp_path))

    assert receipt["error"].strip(), "an empty error reads as 'no problem'"
    assert ": ." not in receipt["error"] and "unknown: ." not in receipt["error"]
    assert "<no admission row in target home>" in receipt["error"], receipt["error"]
