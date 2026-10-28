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
"""

from __future__ import annotations

import json
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


def _admit(home, n=1, *, sender="alpha", target="bravo"):
    return q.admit(
        home,
        sender_profile=sender,
        target_profile=target,
        target_session_id="sess-1",
        idempotency_key=f"peer-{_did(n)}",
        fingerprint=f"fp-{_did(n)}",
        delivery_id=_did(n),
        message=f"hello {n}",
    )


def _leaked_claim(home, n=1, *, age_seconds=None, reoffer_count=0, sender="alpha",
                  waited_seconds=0):
    """A record claimed for a turn that never ended, its claim aged past the lease."""
    age_seconds = (LEASE + 600) if age_seconds is None else age_seconds
    _admit(home, n, sender=sender)
    assert q.claim_next(home, target_profile="bravo", lease_ok=True) is not None
    path = _spool(home) / q.CLAIMED_DIR / f"{_did(n)}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    claimed_at = time.time_ns() - int(age_seconds * 1e9)
    record["created_at"] = claimed_at - int(waited_seconds * 1e9)
    record["claimed_at"] = claimed_at
    record["reoffer_count"] = reoffer_count
    for entry in record["attempts_log"]:
        if entry.get("ended_at") is None:
            entry["started_at"] = claimed_at
    path.write_text(json.dumps(record), encoding="utf-8")
    return record, path


def _settled(home, n=1) -> dict:
    return json.loads(
        (_spool(home) / q.SETTLED_DIR / f"{_did(n)}.json").read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------- the predicate
def test_lease_is_twice_the_queue_ttl():
    assert q.lease_expiry_seconds() == 2 * q.queue_ttl_seconds()


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


# ---------------------------------------------------------------- (a) recovery
def test_lost_lease_is_dead_lettered_never_reoffered(tmp_path):
    """The production shape: the YOUNGEST lost lease (claimed_at = 2 x TTL + 1s).

    At its own constants a lost lease has always passed its delivery deadline, so
    the shipping outcome is the dead-letter arm -- ``failed`` / ``lease_lost`` with
    the open attempt closed, ``reoffer_count`` untouched, exactly ONE sender
    notice, and nothing put back in ``queue/``.
    """
    home = _root(tmp_path / "hermes")
    # the youngest possible lost lease: one second past 2 x TTL, no live holder.
    # (The boundary itself is not asserted -- a wall-clock age of exactly 2 x TTL
    # turns into "older than" the moment the clock ticks; the ruling is strict.)
    record, claimed_path = _leaked_claim(home, 1, age_seconds=LEASE + 1,
                                         reoffer_count=2)
    assert q.lease_lost(record, home=home) is True
    notices = []

    assert q.sweep_delivery_queue(home, on_lease_lost=notices.append) == 1

    assert not claimed_path.exists()
    settled = _settled(home)
    assert settled["status"] == q.STATUS_FAILED
    assert settled["reason"] == q.REASON_LEASE_LOST
    assert settled["status_detail"] == q.STATUS_DETAIL_LEASE_DEAD_LETTER
    assert settled["reoffer_count"] == 2          # unchanged: it was not re-offered
    assert all(entry.get("ended_at") for entry in settled["attempts_log"])
    assert [r["delivery_id"] for r in notices] == [_did(1)]   # exactly one notice
    # zero requeues: nothing was returned to queue/
    assert not (_spool(home) / q.QUEUE_DIR / f"{_did(1)}.json").exists()
    # a second sweep is a no-op -- the record is terminal, not a backlog entry
    assert q.sweep_delivery_queue(home, on_lease_lost=notices.append) == 0
    assert len(notices) == 1


def test_lost_lease_past_its_deadline_is_dead_lettered(tmp_path):
    """(a) with attempts spent -- and with the deadline gone -- it fails, truthfully."""
    home = _root(tmp_path / "hermes")
    record, claimed_path = _leaked_claim(home, 1, reoffer_count=int(DEFAULTS[
        "delivery_max_turn_attempts"]))

    assert q.sweep_delivery_queue(home, on_lease_lost=None) >= 1

    assert not claimed_path.exists()
    settled = _settled(home)
    assert settled["status"] == q.STATUS_FAILED
    assert settled["reason"] == q.REASON_LEASE_LOST
    assert settled["reason"] in reasons.ALL_REASONS
    assert q.REASON_LEASE_LOST not in reasons.AUTO_RETRYABLE
    assert settled["status_detail"] == q.STATUS_DETAIL_LEASE_DEAD_LETTER
    # the very shape being repaired must not survive the repair: no open attempt
    assert settled["attempts_log"]
    assert all(entry.get("ended_at") for entry in settled["attempts_log"])
    assert settled["attempts_log"][-1]["reason"] == q.REASON_LEASE_LOST
    assert settled["claimed_at"] == record["claimed_at"]


def test_recovery_is_idempotent(tmp_path):
    home = _root(tmp_path / "hermes")
    _leaked_claim(home, 1)
    q.sweep_delivery_queue(home, on_lease_lost=None)
    settled = _settled(home)
    assert q.sweep_delivery_queue(home, on_lease_lost=None) == 0
    assert _settled(home)["updated_at"] == settled["updated_at"]


# ---------------------------------------------------------------- (c) live holder
def test_a_live_holder_is_untouched(tmp_path):
    """(c) the holder probe protects a genuinely running turn, whatever its age."""
    home = _root(tmp_path / "hermes")
    record, claimed_path = _leaked_claim(home, 1)

    with bot_relay.acquire_turn_lock(home, "bravo", timeout_seconds=0):
        holder = bot_relay.read_turn_lock_holder(home, "bravo")
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
    assert _settled(home)["reason"] == q.REASON_LEASE_LOST


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
    with bot_relay.acquire_turn_lock(home, "bravo", timeout_seconds=0):
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
    _leaked_claim(home, 1)
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


def test_a_notice_about_a_notice_is_never_filed(tmp_path):
    home = _root(tmp_path / "hermes")
    record, _path = _leaked_claim(home, 1)
    dead = dict(record, status=q.STATUS_FAILED, reason=q.REASON_LEASE_LOST,
                sender_profile=q.EXPIRED_NOTICE_SENDER)
    assert q.notice_lease_lost_to_sender(dead, home=home) is None
