"""Lost-lease recovery: a claim nobody is honouring is recovered, counted, and told.

The defect these pin, measured live on 2026-10-23: ELEVEN records
sat in ``claimed/`` with ``status=running`` and an attempt entry that never got an
``ended_at`` (oldest 181.7h). They were invisible on both sides of the queue:

* the sweep only recovered ``status=queued`` records in ``claimed/``, so a leaked
  claim matched nothing and stayed forever;
* an OPEN attempt freezes ``queued_seconds``, so ``pending_over_ttl`` -- and with it
  the stranded census and its alert -- could not see them;
* no terminal state was ever written, so no sender notice was ever filed.

The ruling on the defect is: a claim is a LEASE -- ``2 x`` the queue
TTL -- and its holder is probed, never trusted.

The second half of the ruling (``RULING-t_6e43ae0a-orphaned-claim-recovery.md``)
answers what the SHIPPED treatment then got wrong: it dead-lettered every lost lease,
including claims that had provably never started a turn -- a payload that was never
read, reported to its sender as NEVER delivered, and destroyed. The dispatch is now on
the claim's OWN evidence: never started => re-offered ONCE under the same delivery_id;
started and never settled => ``ambiguous`` (may have been read) with an honest notice;
no evidence (a pre-marker claim) => the terminal arm, unchanged.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools import bot_delivery_queue as q
from tools import bot_delivery_strand_alert as alert_mod
from tools import bot_failure_reasons as reasons
from tools import bot_relay

DEFAULTS = dict(q._DEFAULTS)
TTL = DEFAULTS["delivery_queue_ttl_seconds"]
LEASE = 2 * TTL
RECOVERY_MAX_AGE = int(q.RECOVERY_MAX_AGE_MULTIPLIER * TTL)

_KEEP = object()


@pytest.fixture(autouse=True)
def _pin_config(monkeypatch):
    """Pin bot_mode config to the spec defaults so tests are deterministic."""
    monkeypatch.setattr(q, "_cfg", lambda key, **kw: DEFAULTS[key])


def _did(n: int) -> str:
    return f"{n:032x}"


def _root(home) -> Path:
    return Path(home)


def _spool(home) -> Path:
    return Path(home) / "runtime" / q.DELIVERY_DIR_NAME


def _admit(home, n=1, *, sender="alpha", target="bravo", message=None, age_seconds=0):
    return q.admit(
        home,
        sender_profile=sender,
        target_profile=target,
        target_session_id="sess-1",
        idempotency_key=f"peer-{_did(n)}",
        fingerprint=f"fp-{_did(n)}",
        delivery_id=_did(n),
        message=message if message is not None else f"hello {n}",
        now_ns=time.time_ns() - int(age_seconds * 1e9),
    )


def _dead_pid() -> int:
    """The pid of a process that has EXITED and been reaped: provably not alive."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    pid = proc.pid
    # The probe is the point of the fixture: fail loudly rather than flake.
    assert bot_relay._pid_alive(pid) is False
    return pid


def _orphan_claim(path, *, age_seconds, waited_seconds=0, legacy=False,
                  turn_started=False, pid=_KEEP, reoffer_count=None):
    """Age the OPEN claim on ``path`` into the shape a torn claim leaves behind.

    ``legacy`` strips the evidence marker (every claim written before the A1 change has
    no marker at all). ``turn_started`` stamps ``turn_started_at`` on the open attempt --
    the case whose payload may already have been read. ``pid`` overrides the claiming pid
    (default: this live, provably-alive test process, so only the lease rule can make the
    claim lose).
    """
    record = json.loads(path.read_text(encoding="utf-8"))
    claimed_at = time.time_ns() - int(age_seconds * 1e9)
    record["created_at"] = claimed_at - int(waited_seconds * 1e9)
    record["claimed_at"] = claimed_at
    if reoffer_count is not None:
        record["reoffer_count"] = reoffer_count
    for entry in record["attempts_log"]:
        if entry.get("ended_at") is None:
            entry["started_at"] = claimed_at
            if legacy:
                entry.pop("marker", None)
            if turn_started:
                entry["turn_started_at"] = claimed_at + 1
            if pid is not _KEEP:
                entry["pid"] = pid
    path.write_text(json.dumps(record), encoding="utf-8")
    return record


def _leaked_claim(home, n=1, *, age_seconds=None, reoffer_count=0, sender="alpha",
                  waited_seconds=0, legacy=False, turn_started=False, pid=_KEEP):
    """A record claimed for a turn that never ended, its claim aged past the lease."""
    age_seconds = (LEASE + 600) if age_seconds is None else age_seconds
    _admit(home, n, sender=sender)
    assert q.claim_next(home, target_profile="bravo", lease_ok=True) is not None
    path = _spool(home) / q.CLAIMED_DIR / f"{_did(n)}.json"
    record = _orphan_claim(
        path,
        age_seconds=age_seconds,
        waited_seconds=waited_seconds,
        legacy=legacy,
        turn_started=turn_started,
        pid=pid,
        reoffer_count=reoffer_count,
    )
    return record, path


def _settled(home, n=1) -> dict:
    return json.loads(
        (_spool(home) / q.SETTLED_DIR / f"{_did(n)}.json").read_text(encoding="utf-8")
    )


def _queued(home, n=1) -> dict:
    return json.loads(
        (_spool(home) / q.QUEUE_DIR / f"{_did(n)}.json").read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------- the predicate
def test_lease_is_twice_the_queue_ttl():
    assert q.lease_expiry_seconds() == 2 * q.queue_ttl_seconds()


def test_the_recovery_bound_is_four_times_the_ttl():
    """A7: derived from the TTL like the lease, so no knob can resurrect old work."""
    assert q.recovery_max_age_seconds() == 4 * q.queue_ttl_seconds()


def test_a_young_claim_is_not_a_lost_lease(tmp_path):
    home = _root(tmp_path / "hermes")
    record, path = _leaked_claim(home, 1, age_seconds=60)
    assert q.lease_lost(record, home=home) is False
    assert path.exists()
    assert q.sweep_delivery_queue(home, on_lease_lost=None) == 0
    assert path.exists()


def test_an_old_claim_with_no_live_holder_is_a_lost_lease(tmp_path):
    home = _root(tmp_path / "hermes")
    record, _path = _leaked_claim(home, 1)
    assert (q.claim_age_seconds(record) or 0) >= LEASE
    assert q.lease_holder_alive(home, "bravo") is False
    assert q.lease_lost(record, home=home) is True


def test_a_closed_attempt_is_never_a_lost_lease(tmp_path):
    home = _root(tmp_path / "hermes")
    record, _path = _leaked_claim(home, 1)
    closed = dict(record)
    closed["attempts_log"] = [dict(record["attempts_log"][0], ended_at=time.time_ns())]
    assert q.lease_lost(closed, home=home) is False


# ---------------------------------------------------------------- A8: dead claimer
def test_a_dead_claiming_pid_loses_its_lease_immediately(tmp_path):
    """The guardrail: an unclean gateway exit is repaired on the next tick.

    The claim is WELL inside the lease (60s of a 3600s lease) and no holder is live, so
    the lease rule alone would wait an hour. The claimer is provably gone -- the exact
    state a SIGKILL leaves -- so the record must not sit there for the rest of the hour.
    """
    home = _root(tmp_path / "hermes")
    record, claimed_path = _leaked_claim(home, 1, age_seconds=60, pid=_dead_pid())

    assert (q.claim_age_seconds(record) or 0) < LEASE
    assert q.lease_lost(record, home=home) is True
    assert q.lost_lease_kind(record, home=home) == q.CLAIM_NEVER_STARTED
    assert q.sweep_delivery_queue(home, on_lease_lost=None) == 1
    assert not claimed_path.exists()
    assert _queued(home)["status"] == q.STATUS_QUEUED


def test_an_unprobeable_pid_falls_back_to_the_lease_rule(tmp_path):
    """Unknown is not dead: no pid (or a torn one) must never buy a recovery."""
    home = _root(tmp_path / "hermes")
    record, _path = _leaked_claim(home, 1, age_seconds=60, pid=None)
    entry = record["attempts_log"][-1]
    assert entry.get("pid") is None
    assert bot_relay._pid_alive(entry.get("pid")) is None
    assert q.lease_lost(record, home=home) is False


# ---------------------------------------------------------------- (a) recovery: A3
def test_an_orphaned_claim_that_never_started_a_turn_is_reoffered_once(tmp_path):
    """Ruling 1, the production shape: the claim PROVABLY never started a turn.

    Nothing read the payload, so the sender's receipt is still true: the record goes back
    to ``queue/`` with the SAME delivery_id, idempotency_key, sequence and message, its
    attempt closed ``claim_never_started``, and NO notice is filed.
    """
    home = _root(tmp_path / "hermes")
    record, claimed_path = _leaked_claim(home, 1, age_seconds=LEASE + 600,
                                        reoffer_count=216)
    assert q.lease_lost(record, home=home) is True
    assert q.lost_lease_kind(record, home=home) == q.CLAIM_NEVER_STARTED
    notices = []

    assert q.sweep_delivery_queue(home, on_lease_lost=notices.append) == 1

    assert not claimed_path.exists()
    assert not (_spool(home) / q.SETTLED_DIR / f"{_did(1)}.json").exists(), (
        "a claim that never ran was not a failure and must not be dead-lettered")
    recovered = _queued(home)
    assert recovered["status"] == q.STATUS_QUEUED
    assert recovered["claimed_at"] is None
    assert recovered["delivery_id"] == record["delivery_id"]
    assert recovered["idempotency_key"] == record["idempotency_key"]
    assert recovered["sequence"] == record["sequence"]
    assert recovered["message"] == record["message"]
    assert recovered["status_detail"] == q.STATUS_DETAIL_RECOVERED
    assert recovered["reoffer_count"] == 216, "a contention counter is not the budget"
    entry = recovered["attempts_log"][-1]
    assert entry["ended_at"], "the very shape being repaired must not survive the repair"
    assert entry["reason"] == q.CLAIM_NEVER_STARTED
    assert q.recovery_replays(recovered) == 1
    assert notices == [], "the sender's receipt is still true; no notice is owed"


def test_the_recovered_record_survives_the_sweep_that_recovered_it(tmp_path):
    """The recovery must not be reaped by the same tick's expiry pass."""
    home = _root(tmp_path / "hermes")
    _leaked_claim(home, 1)
    q.sweep_delivery_queue(home, on_lease_lost=None)
    assert (_spool(home) / q.QUEUE_DIR / f"{_did(1)}.json").exists()
    # only the flush between the sweep's clock and this read, never the old wait
    assert q.queued_seconds(_queued(home)) == pytest.approx(0.0, abs=2.0)


def test_recovery_is_idempotent(tmp_path):
    home = _root(tmp_path / "hermes")
    _leaked_claim(home, 1)
    assert q.sweep_delivery_queue(home, on_lease_lost=None) == 1
    recovered = _queued(home)
    assert q.sweep_delivery_queue(home, on_lease_lost=None) == 0
    again = _queued(home)
    assert again["updated_at"] == recovered["updated_at"]
    assert q.recovery_replays(again) == 1


def test_a_live_holder_is_untouched(tmp_path):
    """(c) the holder probe protects a genuinely running turn, whatever its age."""
    home = _root(tmp_path / "hermes")
    record, claimed_path = _leaked_claim(home, 1)

    with bot_relay.acquire_turn_lock(home, "bravo", timeout_seconds=0,
                                     session=bot_relay.BOT_CHAT_SESSION_KEY):
        holder = bot_relay.read_turn_lock_holder(home, "bravo",
                                                 session=bot_relay.BOT_CHAT_SESSION_KEY)
        assert holder and holder["alive"] is True
        assert q.lease_holder_alive(home, "bravo") is True
        assert q.lease_lost(record, home=home) is False
        assert q.sweep_delivery_queue(home, on_lease_lost=None) == 0
        after = json.loads(claimed_path.read_text(encoding="utf-8"))
        assert after["status"] == q.STATUS_RUNNING
        assert after["attempts_log"][-1].get("ended_at") is None

    # the lock is gone now, and only NOW is the record recoverable
    assert q.lease_lost(record, home=home) is True
    assert q.sweep_delivery_queue(home, on_lease_lost=None) == 1
    assert _queued(home)["status"] == q.STATUS_QUEUED


def test_a_live_holder_does_not_freeze_the_reading_forever(tmp_path):
    """(b) past the lease the frozen reading is void -- even for a live holder."""
    home = _root(tmp_path / "hermes")
    _leaked_claim(home, 1)
    with bot_relay.acquire_turn_lock(home, "bravo", timeout_seconds=0):
        assert q.queued_seconds(json.loads(
            (_spool(home) / q.CLAIMED_DIR / f"{_did(1)}.json").read_text()
        )) > TTL


def test_a_young_attempt_still_freezes(tmp_path):
    """(c) inside the lease the historical freeze is exactly what it was."""
    home = _root(tmp_path / "hermes")
    record, _path = _leaked_claim(home, 1, age_seconds=60, waited_seconds=60)
    # waited 60s before the claim, then frozen: the 60s of attempt do NOT accrue
    assert q.queued_seconds(record) == pytest.approx(60.0, abs=2.0)


# ------------------------------------------------- (b) the terminal arms: A2/A5
def test_a_legacy_claim_with_no_evidence_is_dead_lettered_as_before(tmp_path):
    """No marker: today's terminal arm, byte for byte."""
    home = _root(tmp_path / "hermes")
    record, claimed_path = _leaked_claim(
        home, 1, legacy=True, reoffer_count=int(DEFAULTS["delivery_max_turn_attempts"]))
    assert q.lost_lease_kind(record, home=home) == q.LEGACY_CLAIM

    assert q.sweep_delivery_queue(home, on_lease_lost=None) >= 1

    assert not claimed_path.exists()
    settled = _settled(home)
    assert settled["status"] == q.STATUS_FAILED
    assert settled["reason"] == q.REASON_LEASE_LOST
    assert settled["reason"] in reasons.ALL_REASONS
    assert q.REASON_LEASE_LOST not in reasons.AUTO_RETRYABLE
    assert settled["status_detail"] == q.STATUS_DETAIL_LEASE_DEAD_LETTER
    assert settled["reoffer_count"] == DEFAULTS["delivery_max_turn_attempts"]
    # the very shape being repaired must not survive the repair: no open attempt
    assert settled["attempts_log"]
    assert all(entry.get("ended_at") for entry in settled["attempts_log"])
    assert settled["attempts_log"][-1]["reason"] == q.REASON_LEASE_LOST
    assert settled["claimed_at"] == record["claimed_at"]


def test_an_orphaned_turn_that_started_settles_ambiguous(tmp_path):
    """Ruling 2: a turn DID start, so the payload may have been read -- say so."""
    home = _root(tmp_path / "hermes")
    record, claimed_path = _leaked_claim(home, 1, turn_started=True)
    assert q.lost_lease_kind(record, home=home) == q.TURN_STARTED_UNKNOWN
    notices = []

    assert q.sweep_delivery_queue(home, on_lease_lost=notices.append) == 1

    assert not claimed_path.exists()
    assert not (_spool(home) / q.QUEUE_DIR / f"{_did(1)}.json").exists(), (
        "a payload that may have been read must never be re-offered")
    settled = _settled(home)
    assert settled["status"] == q.STATUS_AMBIGUOUS
    assert settled["reason"] == q.REASON_LEASE_LOST
    assert settled["reason"] in reasons.ALL_REASONS
    assert q.REASON_LEASE_LOST not in reasons.AUTO_RETRYABLE
    assert settled["status_detail"] == q.STATUS_DETAIL_AMBIGUOUS_LEASE
    assert settled["attempts_log"][-1]["ended_at"]
    assert settled["attempts_log"][-1]["reason"] == q.REASON_LEASE_LOST
    assert q.recovery_replays(settled) == 0
    assert [r["delivery_id"] for r in notices] == [_did(1)]
    # terminal, not a backlog entry: a second sweep changes nothing
    assert q.sweep_delivery_queue(home, on_lease_lost=notices.append) == 0
    assert len(notices) == 1


def test_reoffer_ambiguous_consumes_the_ambiguous_lease(tmp_path):
    """Ruling 3: the shared crash-mid-turn door still gives exactly one replay."""
    home = _root(tmp_path / "hermes")
    _leaked_claim(home, 1, turn_started=True)
    q.sweep_delivery_queue(home, on_lease_lost=None)
    assert _settled(home)["status"] == q.STATUS_AMBIGUOUS

    reoffered = q.reoffer_ambiguous(home, _did(1))
    assert reoffered is not None
    assert reoffered["status"] == q.STATUS_QUEUED
    assert q.recovery_replays(reoffered) == 1

    # the replayed attempt dies with its turn started: ambiguous again, and now refused
    assert q.claim_next(home, target_profile="bravo", lease_ok=True) is not None
    path = _spool(home) / q.CLAIMED_DIR / f"{_did(1)}.json"
    _orphan_claim(path, age_seconds=LEASE + 60, turn_started=True)
    q.sweep_delivery_queue(home, on_lease_lost=None)
    assert _settled(home)["status"] == q.STATUS_AMBIGUOUS
    assert q.reoffer_ambiguous(home, _did(1)) is None


def test_reoffer_ambiguous_uses_the_recovery_budget(tmp_path):
    """Ruling 9/F2: the budget is recoveries spent, never ``reoffer_count``."""
    home = _root(tmp_path / "hermes")
    _leaked_claim(home, 1, turn_started=True, reoffer_count=216)
    q.sweep_delivery_queue(home, on_lease_lost=None)
    settled = _settled(home)
    assert settled["status"] == q.STATUS_AMBIGUOUS
    assert settled["reoffer_count"] == 216, "lease contention, not recovery budget"
    assert q.recovery_replays(settled) == 0

    assert q.reoffer_ambiguous(home, _did(1)) is not None, (
        "216 contention re-offers must not spend the one recovery replay")
    assert q.claim_next(home, target_profile="bravo", lease_ok=True) is not None
    _orphan_claim(_spool(home) / q.CLAIMED_DIR / f"{_did(1)}.json",
                  age_seconds=LEASE + 60, turn_started=True)
    q.sweep_delivery_queue(home, on_lease_lost=None)
    assert q.reoffer_ambiguous(home, _did(1)) is None


def test_two_orphans_kill_the_delivery(tmp_path):
    """Ruling 4: one replay, then the terminal arm -- and the budget is not read off F2."""
    home = _root(tmp_path / "hermes")
    _leaked_claim(home, 1, reoffer_count=216)   # a contended record, budget unspent
    assert q.sweep_delivery_queue(home, on_lease_lost=None) == 1
    assert _queued(home)["status"] == q.STATUS_QUEUED

    # the re-offered claim is orphaned too, before any turn starts
    assert q.claim_next(home, target_profile="bravo", lease_ok=True) is not None
    _orphan_claim(_spool(home) / q.CLAIMED_DIR / f"{_did(1)}.json", age_seconds=LEASE + 60)
    notices = []
    assert q.sweep_delivery_queue(home, on_lease_lost=notices.append) == 1

    assert not (_spool(home) / q.QUEUE_DIR / f"{_did(1)}.json").exists()
    settled = _settled(home)
    assert settled["status"] == q.STATUS_FAILED
    assert settled["reason"] == q.REASON_LEASE_LOST
    assert settled["status_detail"] == q.STATUS_DETAIL_LEASE_DEAD_LETTER
    assert settled["reoffer_count"] == 216, "never read as the budget"
    closed = [entry for entry in settled["attempts_log"] if entry.get("ended_at")]
    assert [entry["reason"] for entry in closed] == [
        q.CLAIM_NEVER_STARTED, q.REASON_LEASE_LOST,
    ]
    assert q.recovery_replays(settled) == 1
    assert [r["delivery_id"] for r in notices] == [_did(1)]


def test_a_never_started_claim_past_the_recovery_bound_is_not_reoffered(tmp_path):
    """A7: the age bound, derived from the TTL -- the legacy terminal arm applies."""
    home = _root(tmp_path / "hermes")
    record, claimed_path = _leaked_claim(home, 1, age_seconds=RECOVERY_MAX_AGE + 600)
    assert q.lease_lost(record, home=home) is True
    assert q.lost_lease_kind(record, home=home) == q.LEGACY_CLAIM

    assert q.sweep_delivery_queue(home, on_lease_lost=None) == 1
    assert not claimed_path.exists()
    assert not (_spool(home) / q.QUEUE_DIR / f"{_did(1)}.json").exists()
    assert _settled(home)["status"] == q.STATUS_FAILED


# ---------------------------------------------------------------- A1: the note
def test_a_claim_stamps_its_evidence_marker_and_pid(tmp_path):
    home = _root(tmp_path / "hermes")
    _admit(home, 1)
    claimed = q.claim_next(home, target_profile="bravo", lease_ok=True)
    entry = claimed["attempts_log"][-1]
    assert entry["marker"] == q.CLAIM_MARKER_VERSION == 2
    assert entry["pid"] == os.getpid()
    assert "turn_started_at" not in entry
    assert q.never_started_a_turn(claimed) is True


def test_note_turn_start_stamps_the_open_attempt(tmp_path):
    home = _root(tmp_path / "hermes")
    _admit(home, 1)
    q.claim_next(home, target_profile="bravo", lease_ok=True)

    stamped = q.note_turn_start(home, _did(1))
    assert stamped is not None
    assert stamped["attempts_log"][-1]["turn_started_at"] > 0
    assert stamped["attempts_log"][-1]["pid"] == os.getpid()
    assert q.never_started_a_turn(stamped) is False
    assert q.recovery_replays(stamped) == 0
    # on disk, not just in the return value
    assert _queued_or_claimed(home, 1)["attempts_log"][-1]["turn_started_at"] > 0


def test_note_turn_start_never_raises_on_a_missing_or_terminal_record(tmp_path):
    home = _root(tmp_path / "hermes")
    assert q.note_turn_start(home, _did(7)) is None

    _admit(home, 2)
    q.claim_next(home, target_profile="bravo", lease_ok=True)
    q.settle(home, _did(2), status=q.STATUS_DELIVERED, reply="ok")
    assert q.note_turn_start(home, _did(2)) is None


def _queued_or_claimed(home, n=1) -> dict:
    path = _spool(home) / q.CLAIMED_DIR / f"{_did(n)}.json"
    if not path.exists():
        path = _spool(home) / q.QUEUE_DIR / f"{_did(n)}.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- A6: the proxy
def test_a_recovered_record_that_expires_reads_as_never_offered():
    """Ruling 10: ``attempts >= 1`` with no turn started is still NEVER OFFERED."""
    frozen = {
        "status": q.STATUS_RUNNING,
        "attempts": 1,
        "attempts_log": [
            {"attempt": 1, "started_at": 1, "ended_at": None, "status": "running",
             "reason": None, "marker": q.CLAIM_MARKER_VERSION, "pid": os.getpid()},
        ],
    }
    assert q.expiry_classification(frozen) == (
        q.REASON_NEVER_OFFERED, q.never_offered_detail())

    fresh = {"status": q.STATUS_QUEUED, "attempts": 0, "attempts_log": []}
    assert q.expiry_classification(fresh) == (
        q.REASON_NEVER_OFFERED, q.never_offered_detail())

    offered = {
        "status": q.STATUS_QUEUED,
        "attempts": 1,
        "attempts_log": [
            {"attempt": 1, "started_at": 1, "ended_at": 2, "status": "queued",
             "reason": None, "turn_started_at": 2},
        ],
    }
    assert q.expiry_classification(offered) == (
        q.REASON_QUEUED_EXPIRED, q.expired_detail())


# ---------------------------------------------------------------- the census
def _census(home):
    return q.seat_strand_census(home)


def test_census_counts_a_leaked_claim_as_claimed_stuck(tmp_path):
    home = _root(tmp_path / "hermes")
    _leaked_claim(home, 1)
    seat = _census(home)
    assert seat["claimed_stuck"] == 1
    assert seat["pending_over_ttl"] == 0     # never counted twice
    assert seat["stranded"] == 1


def test_census_keeps_the_stuck_term_out_of_the_over_ttl_term(tmp_path):
    home = _root(tmp_path / "hermes")
    _leaked_claim(home, 1)
    _admit(home, 2, sender="charlie")        # a plain over-TTL queued record
    path = _spool(home) / q.QUEUE_DIR / f"{_did(2)}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["created_at"] = time.time_ns() - int((TTL + 120) * 1e9)
    path.write_text(json.dumps(record), encoding="utf-8")

    seat = _census(home)
    assert (seat["claimed_stuck"], seat["pending_over_ttl"], seat["stranded"]) == (1, 1, 2)


def test_a_live_holder_is_never_claimed_stuck(tmp_path):
    home = _root(tmp_path / "hermes")
    _leaked_claim(home, 1)
    with bot_relay.acquire_turn_lock(home, "bravo", timeout_seconds=0,
                                     session=bot_relay.BOT_CHAT_SESSION_KEY):
        seat = _census(home)
        assert seat["claimed_stuck"] == 0
        assert seat["pending_over_ttl"] == 1   # unfrozen: the age is honest again


def test_the_alert_carries_the_claimed_stuck_count(tmp_path):
    home = _root(tmp_path / "hermes")
    _leaked_claim(home, 1)
    seat = _census(home)
    totals = q._sum_census_seats({"bravo": seat})
    assert totals["claimed_stuck"] == 1
    assert totals["stranded"] == 1
    census = {"root": str(home), "ttl_seconds": TTL, "now_ns": time.time_ns(),
              "seats": {"bravo": seat}, "totals": totals}
    verdict = {
        "census": census,
        "fleet_total": totals["stranded"],
        "previous_total": 0,
        "grown_seats": ["bravo"],
        "generation": 1,
        "card_id": None,
        "counts": {"bravo": totals["stranded"]},
        "recovery": {},
    }
    body = alert_mod.build_card_body(verdict, idempotency_key="strand-alert:1")
    assert "claimed stuck" in body
    assert "claimed stuck: 1" in body
    assert "claims whose lease was lost (`claimed_stuck`)" in body


# ---------------------------------------------------------------- the failure node
def test_the_sweep_files_the_sender_notice_by_default(tmp_path):
    home = _root(tmp_path / "hermes")
    _leaked_claim(home, 1, legacy=True)      # no evidence: the terminal arm tells the sender
    seen = []
    # the default notifier, observed: the sweep's door is the notice itself
    original = q.notice_lease_lost_to_sender

    def spy(record, **kwargs):
        seen.append((record["delivery_id"], record.get("reason")))
        return original(record, **kwargs)

    import tools.bot_delivery_queue as mod
    mod.notice_lease_lost_to_sender = spy
    try:
        q.sweep_delivery_queue(home)
    finally:
        mod.notice_lease_lost_to_sender = original
    assert seen == [(_did(1), q.REASON_LEASE_LOST)]
    # no sender profile named 'alpha' in this sandbox: the notice is skipped, never raised
    assert q.queue_depth(home, "alpha") == 0


def test_a_recovered_claim_files_no_notice(tmp_path):
    """Nothing was lost: the sender's receipt is still true, so it is told nothing."""
    home = _root(tmp_path / "hermes")
    _leaked_claim(home, 1)
    original = q.notice_lease_lost_to_sender
    seen = []
    import tools.bot_delivery_queue as mod

    mod.notice_lease_lost_to_sender = lambda record, **kw: seen.append(record)
    try:
        q.sweep_delivery_queue(home)
    finally:
        mod.notice_lease_lost_to_sender = original
    assert seen == []


def test_notice_is_one_delivery_into_the_senders_own_spool(tmp_path):
    home = _root(tmp_path / "hermes")
    sender_home = _root(tmp_path / "sender_home")
    sender_home.mkdir()
    record, _path = _leaked_claim(home, 1)
    dead = dict(record, status=q.STATUS_FAILED, reason=q.REASON_LEASE_LOST)

    admitted = q.notice_lease_lost_to_sender(dead, home=sender_home)
    assert admitted is not None
    assert admitted["target_profile"] == "alpha"
    assert admitted["sender_profile"] == q.EXPIRED_NOTICE_SENDER
    assert dead["delivery_id"] in admitted["message"]
    assert "lease_lost" in admitted["message"]
    assert "NOT a pending promise" in admitted["message"]

    again = q.notice_lease_lost_to_sender(dead, home=sender_home)
    assert again is not None
    assert again["delivery_id"] == admitted["delivery_id"]
    assert q.queue_depth(sender_home, "alpha") == 1


def test_the_ambiguous_lease_notice_never_claims_never_delivered(tmp_path):
    """Ruling 5: the sender is told the TRUTH -- it may have been read."""
    home = _root(tmp_path / "hermes")
    sender_home = _root(tmp_path / "sender_home")
    sender_home.mkdir()
    _leaked_claim(home, 1, turn_started=True)
    q.sweep_delivery_queue(home, on_lease_lost=None)
    settled = _settled(home)
    assert settled["status"] == q.STATUS_AMBIGUOUS

    admitted = q.notice_lease_lost_to_sender(settled, home=sender_home)
    assert admitted is not None
    assert "NEVER delivered" not in admitted["message"]
    assert "NEVER read" not in admitted["message"]
    assert "MAY have been read" in admitted["message"]
    assert "SAME idempotency key" in admitted["message"]
    assert settled["delivery_id"] in admitted["message"]


def test_a_notice_about_a_notice_is_never_filed(tmp_path):
    home = _root(tmp_path / "hermes")
    record, _path = _leaked_claim(home, 1)
    dead = dict(record, status=q.STATUS_FAILED, reason=q.REASON_LEASE_LOST,
                sender_profile=q.EXPIRED_NOTICE_SENDER)
    assert q.notice_lease_lost_to_sender(dead, home=home) is None
