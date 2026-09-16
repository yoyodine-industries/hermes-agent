"""Tests for tools/bot_delivery_queue.py (spec §2 queue + §1 envelope)."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pytest

from tools import bot_delivery_queue as q

DEFAULTS = dict(q._DEFAULTS)


@pytest.fixture(autouse=True)
def _pin_config(monkeypatch):
    """Pin bot_mode config to the spec defaults so tests are deterministic."""
    monkeypatch.setattr(q, "_cfg", lambda key, **kw: DEFAULTS[key])
    yield


def _did(n: int) -> str:
    return "%032x" % n


def _admit(home, n=1, *, sender="alpha", target="bravo", profile_hint=None, now_ns=None):
    return q.admit(
        home,
        sender_profile=sender,
        target_profile=target,
        target_session_id="sess-1",
        idempotency_key=f"peer-{_did(n)}",
        fingerprint=f"fp-{_did(n)}",
        delivery_id=_did(n),
        message=f"hello {n}",
        now_ns=now_ns,
    )


def _queued_path(home, n):
    return Path(home) / "runtime" / q.DELIVERY_DIR_NAME / q.QUEUE_DIR / f"{_did(n)}.json"


def _claimed_path(home, n):
    return Path(home) / "runtime" / q.DELIVERY_DIR_NAME / q.CLAIMED_DIR / f"{_did(n)}.json"


def _settled_path(home, n):
    return Path(home) / "runtime" / q.DELIVERY_DIR_NAME / q.SETTLED_DIR / f"{_did(n)}.json"


# ---------------------------------------------------------------- vocabulary
def test_no_target_busy_in_vocabulary():
    assert len(q.STATUSES) == 7
    assert "target_busy" not in q.STATUSES
    assert set(q.STATUSES) == {
        "queued",
        "running",
        "delivered",
        "failed",
        "expired",
        "cancelled",
        "ambiguous",
    }
    assert set(q.RESULTS) == {"delivered", "receipt", "failed", "refused", "unknown"}


def test_failure_reason_vocabulary_adds_only_queue_full():
    from tools import bot_failure_reasons as reasons

    assert "queue_full" in reasons.ALL_REASONS
    assert "target_busy" not in reasons.ALL_REASONS
    assert "queue_full" not in reasons.AUTO_RETRYABLE


def test_field_counts():
    assert len(q.ENVELOPE_FIELDS) == 23
    assert q.ENVELOPE_FIELDS[0] == "object"
    assert len(q.RECORD_FIELDS) == 20


# ---------------------------------------------------------------- enqueue
def test_defaults_match_spec():
    assert DEFAULTS["receipt_after_seconds"] == 15
    assert DEFAULTS["delivery_queue_ttl_seconds"] == 1800
    assert DEFAULTS["delivery_queue_max_per_profile"] == 32
    assert DEFAULTS["delivery_queue_max_per_sender"] == 8
    assert DEFAULTS["delivery_retry_base_seconds"] == 2
    assert DEFAULTS["delivery_sweep_seconds"] == 30
    assert DEFAULTS["delivery_max_turn_attempts"] == 3
    assert DEFAULTS["delivery_probe_base_seconds"] == 0.5
    assert DEFAULTS["delivery_probe_max_seconds"] == 2.0
    assert DEFAULTS["lease_probe_seconds"] == 2


def test_admit_writes_twenty_field_record(tmp_path):
    record = _admit(tmp_path, 1)
    assert list(record) == list(q.RECORD_FIELDS)
    assert record["status"] == "queued"
    assert record["attempts"] == 0
    assert record["reoffer_count"] == 0
    assert record["queue_position"] == 1
    assert record["sequence"] == 1
    assert record["claimed_at"] is None
    assert record["attempts_log"] == []
    assert record["reply"] is None and record["error"] is None
    assert record["reason"] is None
    assert record["status_detail"].startswith("target_busy: turn slot held")
    assert record["created_at"] == record["updated_at"]
    assert record["created_at"] > 10 ** 18  # ns epoch

    on_disk = json.loads(_queued_path(tmp_path, 1).read_text())
    assert on_disk == record


def test_admit_is_idempotent_on_delivery_id(tmp_path):
    first = _admit(tmp_path, 1)
    second = _admit(tmp_path, 1)
    assert second == first
    files = list(
        (Path(tmp_path) / "runtime" / q.DELIVERY_DIR_NAME / q.QUEUE_DIR).glob("*.json")
    )
    assert len(files) == 1
    assert q.queue_depth(tmp_path, target_profile="bravo") == 1


def test_admit_assigns_fifo_sequence_and_position(tmp_path):
    a = _admit(tmp_path, 1)
    b = _admit(tmp_path, 2)
    c = _admit(tmp_path, 3)
    assert [a["sequence"], b["sequence"], c["sequence"]] == [1, 2, 3]
    assert [a["queue_position"], b["queue_position"], c["queue_position"]] == [1, 2, 3]


def test_capacity_per_profile_is_32(tmp_path):
    for n in range(1, 33):
        _admit(tmp_path, n, sender=f"sender-{n}")
    assert q.queue_depth(tmp_path, target_profile="bravo") == 32
    with pytest.raises(q.QueueFullError) as excinfo:
        _admit(tmp_path, 99, sender="sender-99")
    assert excinfo.value.reason == "queue_full"
    assert "queue full (32 per profile)" in str(excinfo.value)
    assert excinfo.value.retry_after_seconds == 60.0
    # no reservation was taken: the 33rd delivery left nothing behind
    assert q.queue_depth(tmp_path, target_profile="bravo") == 32


def test_capacity_per_sender_is_8(tmp_path):
    for n in range(1, 9):
        _admit(tmp_path, n, sender="alpha")
    with pytest.raises(q.QueueFullError) as excinfo:
        _admit(tmp_path, 9, sender="alpha")
    assert excinfo.value.reason == "queue_full"
    # a different sender still fits under the per-profile cap
    _admit(tmp_path, 10, sender="charlie")
    assert q.queue_depth(tmp_path, target_profile="bravo") == 9


def test_admit_rejects_bad_keys_and_ids(tmp_path):
    with pytest.raises(ValueError):
        q.validate_idempotency_key("")
    with pytest.raises(ValueError):
        q.validate_idempotency_key("bad\nkey")
    with pytest.raises(ValueError):
        q.validate_idempotency_key("x" * 256)
    assert q.validate_idempotency_key("  peer-abc  ") == "peer-abc"
    with pytest.raises(ValueError):
        q.validate_delivery_id("NOTHEX")


# ---------------------------------------------------------------- claim
def test_claim_next_is_fifo(tmp_path):
    _admit(tmp_path, 1)
    _admit(tmp_path, 2)
    _admit(tmp_path, 3)
    claimed = [q.claim_next(tmp_path, target_profile="bravo", lease_ok=True) for _ in range(3)]
    assert [r["delivery_id"] for r in claimed] == [_did(1), _did(2), _did(3)]
    assert all(r["status"] == "running" for r in claimed)
    assert [r["attempts"] for r in claimed] == [1, 1, 1]
    assert claimed[0]["claimed_at"] is not None
    assert claimed[0]["attempts_log"][0]["status"] == "running"
    assert claimed[0]["attempts_log"][0]["ended_at"] is None
    assert _queued_path(tmp_path, 1).exists() is False
    assert _claimed_path(tmp_path, 1).exists() is True
    assert q.claim_next(tmp_path, target_profile="bravo", lease_ok=True) is None


def test_claim_contended_lease_keeps_attempts_and_reoffers(tmp_path):
    _admit(tmp_path, 1)
    assert q.claim_next(tmp_path, target_profile="bravo", lease_ok=False) is None
    record = q.read_record(tmp_path, _did(1))
    assert record["status"] == "queued"
    assert record["attempts"] == 0  # contention never consumes an attempt
    assert record["reoffer_count"] == 1
    assert record["status_detail"] == q.STATUS_DETAIL_LEASE_CONTENDED
    assert record["claimed_at"] is None
    assert _claimed_path(tmp_path, 1).exists() is False
    q.claim_next(tmp_path, target_profile="bravo", lease_ok=False)
    assert q.read_record(tmp_path, _did(1))["reoffer_count"] == 2


def test_claim_ignores_other_profiles(tmp_path):
    _admit(tmp_path, 1, target="bravo")
    _admit(tmp_path, 2, target="delta")
    claimed = q.claim_next(tmp_path, target_profile="delta", lease_ok=True)
    assert claimed["delivery_id"] == _did(2)


# ---------------------------------------------------------------- requeue
def test_requeue_leaves_attempts_unchanged(tmp_path):
    _admit(tmp_path, 1)
    claimed = q.claim_next(tmp_path, target_profile="bravo", lease_ok=True)
    assert claimed["attempts"] == 1
    requeued = q.requeue(tmp_path, _did(1))
    assert requeued["status"] == "queued"
    assert requeued["attempts"] == 1  # unchanged by requeue
    assert requeued["reoffer_count"] == 1
    assert requeued["claimed_at"] is None
    assert requeued["attempts_log"][0]["ended_at"] is not None
    assert requeued["attempts_log"][0]["reason"] == "requeued"
    assert _queued_path(tmp_path, 1).exists()
    assert not _claimed_path(tmp_path, 1).exists()
    # idempotent
    assert q.requeue(tmp_path, _did(1))["reoffer_count"] == 1


def test_requeue_missing_record(tmp_path):
    with pytest.raises(FileNotFoundError):
        q.requeue(tmp_path, _did(7))


# ---------------------------------------------------------------- settle
def test_settle_terminal_delivered(tmp_path):
    _admit(tmp_path, 1)
    q.claim_next(tmp_path, target_profile="bravo", lease_ok=True)
    settled = q.settle(tmp_path, _did(1), status="delivered", reply="pong")
    assert settled["status"] == "delivered"
    assert settled["reply"] == "pong"
    assert settled["attempts"] == 1
    assert settled["attempts_log"][0]["ended_at"] is not None
    assert _settled_path(tmp_path, 1).exists()
    assert not _claimed_path(tmp_path, 1).exists()
    assert q.queue_depth(tmp_path, target_profile="bravo") == 0
    # idempotent replay of the same terminal outcome
    assert q.settle(tmp_path, _did(1), status="delivered")["status"] == "delivered"


def test_settle_rejects_non_terminal_and_conflicting_replay(tmp_path):
    _admit(tmp_path, 1)
    q.claim_next(tmp_path, target_profile="bravo", lease_ok=True)
    with pytest.raises(ValueError):
        q.settle(tmp_path, _did(1), status="queued")
    q.settle(tmp_path, _did(1), status="delivered")
    with pytest.raises(ValueError):
        q.settle(tmp_path, _did(1), status="failed", error="boom")


def test_settle_failed_carries_error_and_reason(tmp_path):
    _admit(tmp_path, 1)
    q.claim_next(tmp_path, target_profile="bravo", lease_ok=True)
    settled = q.settle(
        tmp_path, _did(1), status="failed", error="turn failed", reason="runtime_offline"
    )
    env = q.build_envelope(settled)
    assert env["result"] == "failed"
    assert env["error"] == "turn failed"
    assert env["reason"] == "runtime_offline"
    assert env["retryable"] is True


# ---------------------------------------------------------------- expiry + sweep
def test_sweep_expires_over_age_records(tmp_path):
    old = time.time_ns() - int(2 * 3600 * 1e9)
    _admit(tmp_path, 1, now_ns=old)
    assert q.sweep_delivery_queue(tmp_path) == 1
    record = q.read_record(tmp_path, _did(1))
    assert record["status"] == "expired"
    assert record["reason"] == q.REASON_QUEUED_EXPIRED
    assert record["status_detail"] == "queued 1800s without a free turn slot; not delivered"
    assert _settled_path(tmp_path, 1).exists()
    assert q.sweep_delivery_queue(tmp_path) == 0


def test_sweep_leaves_fresh_records_alone(tmp_path):
    _admit(tmp_path, 1)
    assert q.sweep_delivery_queue(tmp_path) == 0
    assert q.read_record(tmp_path, _did(1))["status"] == "queued"


def test_sweep_recovers_orphaned_claim(tmp_path):
    record = _admit(tmp_path, 1)
    claimed_dir = Path(tmp_path) / "runtime" / q.DELIVERY_DIR_NAME / q.CLAIMED_DIR
    claimed_dir.mkdir(parents=True, exist_ok=True)
    (claimed_dir / f"{_did(1)}.json").write_text(json.dumps(record))
    assert q.sweep_delivery_queue(tmp_path) == 1
    assert _queued_path(tmp_path, 1).exists()
    assert not _claimed_path(tmp_path, 1).exists()


# ---------------------------------------------------------------- derivation
def test_queued_seconds_counts_waiting_only(tmp_path):
    day_ago = time.time_ns() - int(86400 * 1e9)
    record = _admit(tmp_path, 1, now_ns=day_ago)
    waiting = q.queued_seconds(record)
    assert 86390 < waiting < 86410
    q.claim_next(tmp_path, target_profile="bravo", lease_ok=True)
    running = q.read_record(tmp_path, _did(1))
    # the 24h wait before the turn started is still counted; no new queued time
    assert q.queued_seconds(running) == pytest.approx(86400.0, abs=1.0)

    fresh = _admit(tmp_path, 2, now_ns=time.time_ns())
    assert q.queued_seconds(fresh) < 1.0
    q.claim_next(tmp_path, target_profile="bravo", lease_ok=True)
    assert q.queued_seconds(q.read_record(tmp_path, _did(2))) < 1.0


def test_backoff_curves(tmp_path):
    class _Mid:
        def uniform(self, a, b):
            return (a + b) / 2

    mid = _Mid()
    assert [round(q.probe_delay(n, rng=mid), 3) for n in range(4)] == [0.5, 1.0, 2.0, 2.0]
    assert [round(q.drainer_delay(n, rng=mid), 3) for n in range(7)] == [
        1.5,
        3.0,
        6.0,
        12.0,
        22.5,
        22.5,
        22.5,
    ]


def test_no_send_id_or_reoffer_in_record(tmp_path):
    record = _admit(tmp_path, 1)
    assert "send_id" not in record
    assert "reoffer_count" in record
    envelope = q.build_envelope(record)
    assert "reoffer_count" not in envelope
    assert len(envelope["send_id"]) == 32
    assert q.ENVELOPE_FIELDS.count("send_id") == 1
    assert "send_id" not in q.RECORD_FIELDS


# ---------------------------------------------------------------- envelope
def test_build_envelope_field_order(tmp_path):
    record = _admit(tmp_path, 1)
    env = q.build_envelope(record, send_id=_did(5), busy=True, waited_seconds=0.2)
    assert list(env) == list(q.ENVELOPE_FIELDS)
    assert env["object"] == "hermes.peer.send_result"
    assert env["result"] == "receipt"
    assert env["status"] == "queued"
    assert env["status_detail"].startswith("target_busy")
    assert env["send_id"] == _did(5)
    assert env["delivery_id"] == _did(1)
    assert env["idempotency_key"] == f"peer-{_did(1)}"
    assert env["replayed"] is False
    assert env["profile"] == "bravo"
    assert env["session_id"] == "sess-1"
    assert env["queue_position"] == 1
    assert env["attempts"] == 0
    assert env["waited_seconds"] == 0.2
    assert env["busy"] is True
    assert env["retryable"] is False
    assert env["retry_after_seconds"] is None
    assert env["reply"] is None
    assert env["error"] is None
    assert env["reason"] is None
    assert env["detail"] == q.DETAIL_RECEIPT
    assert env["at"] > 10 ** 9


def test_build_receipt_is_busy_zero_attempts(tmp_path):
    record = _admit(tmp_path, 1)
    env = q.build_receipt(record)
    assert env["result"] == "receipt"
    assert env["busy"] is True
    assert env["attempts"] == 0
    assert env["status"] == "queued"
    assert env["detail"] == q.DETAIL_RECEIPT
    assert env["waited_seconds"] == 15


def test_build_envelope_delivered(tmp_path):
    _admit(tmp_path, 1)
    q.claim_next(tmp_path, target_profile="bravo", lease_ok=True)
    record = q.settle(tmp_path, _did(1), status="delivered", reply="pong")
    env = q.build_envelope(record)
    assert env["result"] == "delivered"
    assert env["status"] == "delivered"
    assert env["attempts"] == 1
    assert env["reply"] == "pong"
    assert env["queue_position"] is None
    assert env["detail"] is None
    assert env["busy"] is False


def test_build_envelope_expired_and_unknown(tmp_path):
    old = time.time_ns() - int(2 * 3600 * 1e9)
    _admit(tmp_path, 1, now_ns=old)
    q.sweep_delivery_queue(tmp_path)
    env = q.build_envelope(q.read_record(tmp_path, _did(1)))
    assert env["result"] == "failed"
    assert env["status"] == "expired"
    assert env["reason"] == q.REASON_QUEUED_EXPIRED
    assert env["detail"] == "queued 1800s without a free turn slot; not delivered"
    assert env["retryable"] is False


def test_retryable_is_reason_only():
    assert q.retryable({"result": "failed", "reason": "queue_full"}) is True
    assert q.retryable({"result": "failed", "reason": "runtime_offline"}) is True
    assert q.retryable({"result": "failed", "reason": "delivery_failed"}) is False
    assert q.retryable({"result": "receipt", "reason": "queue_full"}) is False
    assert q.retryable({"result": "delivered", "reason": None}) is False


def test_notification_text_strings(tmp_path):
    record = _admit(tmp_path, 1)
    assert q.build_notification_text(record) == q.DETAIL_RECEIPT
    live = dict(record, status_detail=q.STATUS_DETAIL_LIVE_OWNER)
    assert q.build_notification_text(live) == (
        "Delivery remains pending or its outcome is unknown. "
        "Do not resend; receipt is retained."
    )
    assert "do NOT resend" in q.DETAIL_RECEIPT


# --------------------------------------------------- admission-path helpers
def test_check_capacity_matches_admit_caps(tmp_path):
    """The pre-reserve capacity probe must agree with admit()'s own gate (§4.6 step 3)."""
    q.check_capacity(tmp_path, target_profile="bravo", sender_profile="alpha")  # empty: fine
    for n in range(1, q.max_per_sender() + 1):
        _admit(tmp_path, n)
    with pytest.raises(q.QueueFullError) as exc:
        q.check_capacity(tmp_path, target_profile="bravo", sender_profile="alpha")
    assert exc.value.reason == "queue_full"
    assert exc.value.retry_after_seconds == 60.0
    # A different sender is blocked by the same per-sender cap only for itself.
    q.check_capacity(tmp_path, target_profile="bravo", sender_profile="charlie")


def test_check_capacity_per_profile_cap(tmp_path):
    for n in range(1, q.max_per_profile() + 1):
        _admit(tmp_path, n, sender=f"s{n}")
    with pytest.raises(q.QueueFullError):
        q.check_capacity(tmp_path, target_profile="bravo", sender_profile="zeta")


def test_check_capacity_takes_no_reservation(tmp_path):
    for n in range(1, q.max_per_profile() + 1):
        _admit(tmp_path, n, sender=f"s{n}")
    with pytest.raises(q.QueueFullError):
        q.check_capacity(tmp_path, target_profile="bravo", sender_profile="alpha")
    assert not _queued_path(tmp_path, q.max_per_profile() + 1).exists()
    assert len(list((Path(tmp_path) / "runtime" / q.DELIVERY_DIR_NAME / q.QUEUE_DIR).glob("*.json"))) == q.max_per_profile()


def test_mark_contended_stamps_detail_without_consuming_attempts(tmp_path):
    _admit(tmp_path, 1)
    record = q.mark_contended(tmp_path, _did(1), now_ns=123)
    assert record["status"] == "queued"
    assert record["status_detail"] == "target_busy: turn slot held by another turn; delivery queued"
    assert record["attempts"] == 0
    assert record["reoffer_count"] == 0
    assert record["updated_at"] == 123
    assert q.read_record(tmp_path, _did(1))["status_detail"] == record["status_detail"]


def test_mark_contended_returns_none_for_unknown_delivery(tmp_path):
    assert q.mark_contended(tmp_path, _did(9)) is None


def test_mark_contended_leaves_a_claimed_record_alone(tmp_path):
    _admit(tmp_path, 1)
    claimed = q.claim_next(tmp_path, target_profile="bravo", lease_ok=True)
    assert q.mark_contended(tmp_path, _did(1)) is None
    stored = q.read_record(tmp_path, _did(1))
    assert stored["status"] == "running"
    assert stored["attempts"] == 1
    assert stored["status_detail"] == claimed["status_detail"]


# ------------------------------------------------- crash recovery (one replay)
def test_reoffer_ambiguous_requeues_exactly_once(tmp_path):
    _admit(tmp_path, 1)
    q.claim_next(tmp_path, target_profile="bravo", lease_ok=True)
    q.settle(tmp_path, _did(1), status="ambiguous", error="outcome unknown")
    assert _settled_path(tmp_path, 1).exists()

    record = q.reoffer_ambiguous(tmp_path, _did(1))
    assert record["status"] == "queued"
    assert record["reoffer_count"] == 1
    assert record["attempts"] == 1  # the re-run's attempt is counted at claim time
    assert record["status_detail"] == q.STATUS_DETAIL_TARGET_BUSY
    assert _queued_path(tmp_path, 1).exists()
    assert not _settled_path(tmp_path, 1).exists()

    # At most one recovery replay: the second same-key attempt gets nothing back.
    q.claim_next(tmp_path, target_profile="bravo", lease_ok=True)
    q.settle(tmp_path, _did(1), status="ambiguous")
    assert q.reoffer_ambiguous(tmp_path, _did(1)) is None


def test_reoffer_ambiguous_refuses_non_ambiguous_and_unknown(tmp_path):
    _admit(tmp_path, 1)
    q.claim_next(tmp_path, target_profile="bravo", lease_ok=True)
    q.settle(tmp_path, _did(1), status="delivered", reply="pong")
    assert q.reoffer_ambiguous(tmp_path, _did(1)) is None  # a delivered record never re-runs
    assert q.reoffer_ambiguous(tmp_path, _did(1)) is None  # unknown id
    assert q.read_record(tmp_path, _did(1))["status"] == "delivered"


def test_requeue_unstarted_rolls_back_an_uncharged_attempt(tmp_path):
    """Contention must never consume an attempt (§2.7 step 2, §4.9)."""
    _admit(tmp_path, 1)
    claimed = q.claim_next(tmp_path, target_profile="bravo", lease_ok=True)
    assert claimed["attempts"] == 1 and len(claimed["attempts_log"]) == 1

    back = q.requeue_unstarted(tmp_path, _did(1))
    assert back["status"] == "queued"
    assert back["attempts"] == 0
    assert back["attempts_log"] == []
    assert back["reoffer_count"] == 1
    assert back["claimed_at"] is None
    assert back["status_detail"] == q.STATUS_DETAIL_LEASE_CONTENDED
    assert _queued_path(tmp_path, 1).exists()
    assert not _claimed_path(tmp_path, 1).exists()

    # The rollback is not a free pass: the next claim charges its own attempt.
    again = q.claim_next(tmp_path, target_profile="bravo", lease_ok=True)
    assert again["delivery_id"] == _did(1) and again["attempts"] == 1

    # A second contention rolls that claim back too; once queued the call is a no-op.
    rolled_back = q.requeue_unstarted(tmp_path, _did(1))
    assert rolled_back["attempts"] == 0
    untouched = q.requeue_unstarted(tmp_path, _did(1))
    assert untouched["status"] == "queued" and untouched["attempts"] == 0


# ── D6: the refusal detail names the cap that fired ────────────────────────────

def test_queue_full_envelope_names_the_cap_that_fired():
    """D6: a per-sender refusal must not be reported as the per-profile cap."""
    base = {
        "delivery_id": _did(1),
        "idempotency_key": "peer-1",
        "target_profile": "bravo",
        "sender_profile": "alpha",
        "status": q.STATUS_FAILED,
        "reason": "queue_full",
        "created_at": time.time_ns(),
        "updated_at": time.time_ns(),
    }

    per_sender = q.build_envelope(
        {**base, "limit_kind": q.LIMIT_PER_SENDER, "limit": 8})
    assert per_sender["result"] == q.RESULT_FAILED
    assert per_sender["retryable"] is True
    assert "8 pending from this sender" in per_sender["detail"]
    assert "per profile" not in per_sender["detail"]

    per_profile = q.build_envelope(
        {**base, "limit_kind": q.LIMIT_PER_PROFILE, "limit": q.max_per_profile()})
    assert f"{q.max_per_profile()} per profile" in per_profile["detail"]

    # A row that lost the fired cap (an older record) reads as it always did.
    legacy = q.build_envelope(base)
    assert f"{q.max_per_profile()} per profile" in legacy["detail"]


# ------------------------------------------------------- running-claim lease
def test_sweep_reaps_lapsed_running_claim_and_frees_slot(tmp_path):
    """A dead turn's running claim must not hold an admission slot forever."""
    for n in range(1, q.max_per_sender() + 1):
        _admit(tmp_path, n)
    # Claim the oldest -> running (1 running + 7 queued = still 8/8).
    q.claim_next(tmp_path, target_profile="bravo", lease_ok=True)
    with pytest.raises(q.QueueFullError):
        _admit(tmp_path, 9)

    # Backdate the running claim's lease so the sweep treats it as lapsed.
    claimed = q.read_record(tmp_path, _did(1))
    assert claimed["status"] == "running"
    claimed["claimed_at"] = time.time_ns() - int(2 * 3600 * 1e9)
    _claimed_path(tmp_path, 1).write_text(json.dumps(claimed))

    # The sweep reaps the lapsed claim: slot reclaimed, record settled ambiguous.
    assert q.sweep_delivery_queue(tmp_path) == 1
    reaped = q.read_record(tmp_path, _did(1))
    assert reaped["status"] == "ambiguous"
    assert reaped["reason"] == q.REASON_LEASE_LAPSED
    assert reaped["claimed_at"] is None
    assert reaped["attempts_log"][-1]["reason"] == q.REASON_LEASE_LAPSED
    assert not _claimed_path(tmp_path, 1).exists()
    assert _settled_path(tmp_path, 1).exists()

    # A new send from the same sender is now admitted.
    assert _admit(tmp_path, 9)["delivery_id"] == _did(9)


def test_sweep_leaves_fresh_running_claim_alone(tmp_path):
    """A live turn's claim within its lease is not reaped."""
    _admit(tmp_path, 1)
    q.claim_next(tmp_path, target_profile="bravo", lease_ok=True)
    assert q.read_record(tmp_path, _did(1))["status"] == "running"
    assert q.sweep_delivery_queue(tmp_path) == 0
    assert q.read_record(tmp_path, _did(1))["status"] == "running"
    assert _claimed_path(tmp_path, 1).exists()


def test_live_turn_past_lease_still_lands_delivered(tmp_path):
    """A still-alive turn whose lease lapsed must still land its true outcome."""
    _admit(tmp_path, 1)
    q.claim_next(tmp_path, target_profile="bravo", lease_ok=True)

    # The turn runs past its lease; the sweep retires the claim as lease_lapsed.
    claimed = q.read_record(tmp_path, _did(1))
    claimed["claimed_at"] = time.time_ns() - int(2 * 3600 * 1e9)
    _claimed_path(tmp_path, 1).write_text(json.dumps(claimed))
    assert q.sweep_delivery_queue(tmp_path) == 1
    assert q.read_record(tmp_path, _did(1))["status"] == "ambiguous"

    # The still-alive turn finishes and settles for real -- no exception, and the
    # outcome is delivered (not the sweep's premature "unknown").
    settled = q.settle(tmp_path, _did(1), status="delivered", reply="pong")
    assert settled["status"] == "delivered"
    assert settled["reply"] == "pong"
    assert settled["reason"] is None
    assert settled["claimed_at"] is None
    assert settled["attempts_log"][-1]["status"] == "delivered"
    assert settled["attempts_log"][-1]["reason"] is None
    assert not _claimed_path(tmp_path, 1).exists()
    assert _settled_path(tmp_path, 1).exists()

    env = q.build_envelope(settled)
    assert env["result"] == "delivered"
    assert env["status"] == "delivered"

    # Replaying the same terminal outcome stays idempotent.
    assert q.settle(tmp_path, _did(1), status="delivered")["status"] == "delivered"


def test_live_turn_past_lease_still_lands_failed(tmp_path):
    """A still-alive turn that fails after its lease lapsed lands ``failed``."""
    _admit(tmp_path, 1)
    q.claim_next(tmp_path, target_profile="bravo", lease_ok=True)
    claimed = q.read_record(tmp_path, _did(1))
    claimed["claimed_at"] = time.time_ns() - int(2 * 3600 * 1e9)
    _claimed_path(tmp_path, 1).write_text(json.dumps(claimed))
    q.sweep_delivery_queue(tmp_path)

    settled = q.settle(
        tmp_path, _did(1), status="failed", error="turn failed", reason="runtime_offline"
    )
    assert settled["status"] == "failed"
    assert settled["error"] == "turn failed"
    assert settled["reason"] == "runtime_offline"
    assert settled["attempts_log"][-1]["status"] == "failed"
    assert q.build_envelope(settled)["result"] == "failed"


def test_requeue_unstarted_after_lease_lapsed_rolls_back(tmp_path):
    """A contended turn swept as lease_lapsed re-queues without charging the attempt."""
    _admit(tmp_path, 1)
    q.claim_next(tmp_path, target_profile="bravo", lease_ok=True)
    claimed = q.read_record(tmp_path, _did(1))
    claimed["claimed_at"] = time.time_ns() - int(2 * 3600 * 1e9)
    _claimed_path(tmp_path, 1).write_text(json.dumps(claimed))
    q.sweep_delivery_queue(tmp_path)
    assert q.read_record(tmp_path, _did(1))["status"] == "ambiguous"

    back = q.requeue_unstarted(tmp_path, _did(1))
    assert back["status"] == "queued"
    assert back["attempts"] == 0
    assert back["attempts_log"] == []
    assert back["reoffer_count"] == 1
    assert back["reason"] is None
    assert back["claimed_at"] is None
    assert back["status_detail"] == q.STATUS_DETAIL_LEASE_CONTENDED
    assert _queued_path(tmp_path, 1).exists()
    assert not _settled_path(tmp_path, 1).exists()


def test_sweep_slot_held_exemption_protects_queued_not_lapsed_claims(tmp_path):
    """The slot-held exemption protects queued records, never dead claims."""
    old = time.time_ns() - int(2 * 3600 * 1e9)
    # Over-age queued record for bravo: kept while the slot is held.
    _admit(tmp_path, 1, target="bravo", now_ns=old)
    # Fresh record for charlie, claimed and left to lapse.
    _admit(tmp_path, 2, target="charlie")
    q.claim_next(tmp_path, target_profile="charlie", lease_ok=True)
    claimed = q.read_record(tmp_path, _did(2))
    claimed["claimed_at"] = old
    _claimed_path(tmp_path, 2).write_text(json.dumps(claimed))

    def _held(_home, _target):
        return True

    # The queued record is held; the lapsed claim is reaped regardless.
    assert q.sweep_delivery_queue(tmp_path, slot_held_fn=_held) == 1
    assert q.read_record(tmp_path, _did(1))["status"] == "queued"
    assert q.read_record(tmp_path, _did(2))["status"] == "ambiguous"


def test_settle_after_lease_lapsed_reoffer_lands_delivered(tmp_path):
    """The one permitted replay landing first must not lose the live turn's outcome."""
    _admit(tmp_path, 1)
    q.claim_next(tmp_path, target_profile="bravo", lease_ok=True)

    # The turn runs past its lease: the sweep retires the claim, then the sender's
    # one permitted recovery replay rewinds it back to queued (no duplicate DM yet).
    claimed = q.read_record(tmp_path, _did(1))
    claimed["claimed_at"] = time.time_ns() - int(2 * 3600 * 1e9)
    _claimed_path(tmp_path, 1).write_text(json.dumps(claimed))
    assert q.sweep_delivery_queue(tmp_path) == 1
    assert q.read_record(tmp_path, _did(1))["status"] == "ambiguous"

    reoffered = q.reoffer_ambiguous(tmp_path, _did(1))
    assert reoffered["status"] == "queued"
    assert reoffered["reoffer_count"] == 1
    assert reoffered["reason"] is None
    assert _queued_path(tmp_path, 1).exists()
    assert not _settled_path(tmp_path, 1).exists()

    # The still-alive turn settles for real: no exception, the true outcome lands,
    # and the pending replay is retired (the delivered record is never re-runnable).
    settled = q.settle(tmp_path, _did(1), status="delivered", reply="pong")
    assert settled["status"] == "delivered"
    assert settled["reply"] == "pong"
    assert settled["reason"] is None
    assert settled["claimed_at"] is None
    assert settled["attempts_log"][-1]["status"] == "delivered"
    assert settled["attempts_log"][-1]["reason"] is None
    assert _settled_path(tmp_path, 1).exists()
    assert not _queued_path(tmp_path, 1).exists()
    assert not _claimed_path(tmp_path, 1).exists()

    env = q.build_envelope(settled)
    assert env["result"] == "delivered"
    assert env["status"] == "delivered"

    # A delivered record is not re-offerable again: the replay is retired.
    assert q.reoffer_ambiguous(tmp_path, _did(1)) is None


def test_requeue_unstarted_after_lease_lapsed_reoffer_rolls_back(tmp_path):
    """A contended turn whose reoffered record is re-queued rolls back cleanly."""
    _admit(tmp_path, 1)
    q.claim_next(tmp_path, target_profile="bravo", lease_ok=True)
    claimed = q.read_record(tmp_path, _did(1))
    claimed["claimed_at"] = time.time_ns() - int(2 * 3600 * 1e9)
    _claimed_path(tmp_path, 1).write_text(json.dumps(claimed))
    q.sweep_delivery_queue(tmp_path)
    q.reoffer_ambiguous(tmp_path, _did(1))
    assert q.read_record(tmp_path, _did(1))["status"] == "queued"

    # No exception; the uncharged attempt is rolled back even though the record
    # was already rewound to queued by the one permitted replay.
    back = q.requeue_unstarted(tmp_path, _did(1))
    assert back["status"] == "queued"
    assert back["attempts"] == 0
    assert back["attempts_log"] == []
    assert back["reoffer_count"] == 2
    assert back["reason"] is None
    assert back["claimed_at"] is None
    assert back["status_detail"] == q.STATUS_DETAIL_LEASE_CONTENDED
    assert _queued_path(tmp_path, 1).exists()
    assert not _settled_path(tmp_path, 1).exists()
    assert not _claimed_path(tmp_path, 1).exists()

    # A second call is a no-op on the already-rolled-back queued record.
    again = q.requeue_unstarted(tmp_path, _did(1))
    assert again["attempts"] == 0
    assert again["reoffer_count"] == 2


def test_settle_after_lease_lapsed_reoffer_and_reclaim_lands_delivered(tmp_path):
    """A reoffered record re-claimed by a later turn still tolerates the live turn."""
    _admit(tmp_path, 1)
    q.claim_next(tmp_path, target_profile="bravo", lease_ok=True)
    claimed = q.read_record(tmp_path, _did(1))
    claimed["claimed_at"] = time.time_ns() - int(2 * 3600 * 1e9)
    _claimed_path(tmp_path, 1).write_text(json.dumps(claimed))
    q.sweep_delivery_queue(tmp_path)
    q.reoffer_ambiguous(tmp_path, _did(1))

    # A later drain turn re-claims the reoffered record before the original
    # (still-alive) turn finishes and settles.
    q.claim_next(tmp_path, target_profile="bravo", lease_ok=True)
    assert q.read_record(tmp_path, _did(1))["status"] == "running"

    # No exception; the live turn's true outcome still wins and the replay retires.
    settled = q.settle(tmp_path, _did(1), status="delivered", reply="pong")
    assert settled["status"] == "delivered"
    assert settled["reply"] == "pong"
    assert settled["attempts"] == 2
    assert settled["attempts_log"][-1]["status"] == "delivered"
    assert _settled_path(tmp_path, 1).exists()
    assert not _claimed_path(tmp_path, 1).exists()
    assert q.build_envelope(settled)["result"] == "delivered"
    assert q.reoffer_ambiguous(tmp_path, _did(1)) is None


# ── holder identity: after reap -> reoffer -> re-claim, one holder one writer ───

def _two_holders(tmp_path, n=1) -> tuple[dict, dict]:
    """Drive admit -> claim -> sweep reap -> reoffer -> re-claim; return both claims.

    Reproduces the measured two-live-holder window: the original turn (attempt 1)
    outlives its lease and the sweep retires it, the one permitted recovery replay
    rewinds the record, and a later drain turn (attempt 2) re-claims it while the
    original is still running.
    """
    _admit(tmp_path, n)
    original = q.claim_next(tmp_path, target_profile="bravo", lease_ok=True)
    claimed = q.read_record(tmp_path, _did(n))
    claimed["claimed_at"] = time.time_ns() - int(2 * 3600 * 1e9)
    _claimed_path(tmp_path, n).write_text(json.dumps(claimed))
    assert q.sweep_delivery_queue(tmp_path) == 1
    assert q.read_record(tmp_path, _did(n))["status"] == "ambiguous"
    assert q.reoffer_ambiguous(tmp_path, _did(n))["status"] == "queued"
    reclaimer = q.claim_next(tmp_path, target_profile="bravo", lease_ok=True)
    assert q.read_record(tmp_path, _did(n))["status"] == "running"
    assert original is not None and reclaimer is not None
    return original, reclaimer


@pytest.mark.parametrize("reclaimer_first", [True, False])
@pytest.mark.parametrize("loser_status", ["delivered", "failed"])
def test_only_the_current_owner_writes_the_outcome(
    tmp_path, caplog, reclaimer_first, loser_status
):
    """Both orderings x agreeing/disagreeing statuses: the owner wins, nothing raises."""
    original, reclaimer = _two_holders(tmp_path)
    assert (original["attempts"], reclaimer["attempts"]) == (1, 2)
    owner_attempt, original_attempt = int(reclaimer["attempts"]), int(original["attempts"])

    def _owner_settles():
        return q.settle(
            tmp_path, _did(1), status="delivered", reply="pong",
            holder_attempt=owner_attempt,
        )

    def _non_owner_settles():
        return q.settle(
            tmp_path, _did(1), status=loser_status, reply="stale", error="stale",
            holder_attempt=original_attempt,
        )

    with caplog.at_level(logging.INFO, logger="tools.bot_delivery_queue"):
        if reclaimer_first:
            owner_call, loser_call = _owner_settles(), _non_owner_settles()
        else:
            loser_call, owner_call = _non_owner_settles(), _owner_settles()

    # The non-owner's call is a logged no-op that hands back the CURRENT record:
    # already settled by the owner if the owner went first, still running under the
    # owner's own open attempt otherwise.
    if reclaimer_first:
        assert loser_call["status"] == "delivered"
        assert loser_call["reply"] == "pong"
    else:
        assert loser_call["status"] == "running"
        assert q._open_attempt(loser_call)["attempt"] == 2
        assert q._open_attempt(loser_call)["ended_at"] is None
    assert "settle_conflict" in caplog.text
    assert f"holder_attempt={original_attempt}" in caplog.text

    # The owner's outcome and reply survive in both orderings -- the loser neither
    # raised nor wrote anything (its "stale" reply/error are nowhere).
    final = q.read_record(tmp_path, _did(1))
    assert final == owner_call
    assert final["status"] == "delivered"
    assert final["reply"] == "pong"
    assert final["error"] in (None, "")
    assert final["reason"] is None
    assert final["attempts"] == 2
    assert _settled_path(tmp_path, 1).exists()
    assert not _claimed_path(tmp_path, 1).exists()
    assert not _queued_path(tmp_path, 1).exists()

    # Exactly one entry per attempt, each carrying its own turn's outcome.
    log = final["attempts_log"]
    assert [entry["attempt"] for entry in log] == [1, 2]
    assert (log[0]["status"], log[0]["reason"]) == ("ambiguous", "lease_lapsed")
    assert log[0]["ended_at"] is not None
    assert (log[1]["status"], log[1]["reason"]) == ("delivered", None)
    assert log[1]["ended_at"] is not None
    assert q.build_envelope(final)["result"] == "delivered"


def test_non_owner_requeue_unstarted_on_a_settled_delivery_is_a_noop(tmp_path, caplog):
    """Already settled by another holder: the loser gets the record, never an error."""
    original, reclaimer = _two_holders(tmp_path)
    q.settle(
        tmp_path, _did(1), status="delivered", reply="pong",
        holder_attempt=int(reclaimer["attempts"]),
    )

    with caplog.at_level(logging.INFO, logger="tools.bot_delivery_queue"):
        back = q.requeue_unstarted(
            tmp_path, _did(1), holder_attempt=int(original["attempts"])
        )

    assert back["status"] == "delivered"
    assert back["attempts"] == 2
    assert back["reoffer_count"] == 1
    assert back["reply"] == "pong"
    assert "requeue_conflict" in caplog.text
    assert f"holder_attempt={original['attempts']}" in caplog.text
    assert _settled_path(tmp_path, 1).exists()
    assert not _queued_path(tmp_path, 1).exists()


def test_non_owner_requeue_unstarted_leaves_the_owner_open_attempt(tmp_path, caplog):
    """A non-owner must not roll back the current owner's open claim."""
    original, reclaimer = _two_holders(tmp_path)

    with caplog.at_level(logging.INFO, logger="tools.bot_delivery_queue"):
        back = q.requeue_unstarted(
            tmp_path, _did(1), holder_attempt=int(original["attempts"])
        )

    assert back["status"] == "running"
    assert back["attempts"] == 2
    assert back["reoffer_count"] == 1
    assert "requeue_conflict" in caplog.text
    assert _claimed_path(tmp_path, 1).exists()
    assert not _queued_path(tmp_path, 1).exists()

    # The owner's claim is intact and can still land its own outcome afterwards.
    held = q.read_record(tmp_path, _did(1))
    open_entry = q._open_attempt(held)
    assert open_entry["attempt"] == int(reclaimer["attempts"])
    assert open_entry["ended_at"] is None

    settled = q.settle(
        tmp_path, _did(1), status="delivered", reply="pong",
        holder_attempt=int(reclaimer["attempts"]),
    )
    assert settled["status"] == "delivered"
    assert [entry["status"] for entry in settled["attempts_log"]] == [
        "ambiguous",
        "delivered",
    ]


def test_owner_requeue_unstarted_after_reclaim_rolls_back_only_its_own_attempt(tmp_path):
    """The current owner's contention rollback leaves the retired attempt alone."""
    _original, reclaimer = _two_holders(tmp_path)

    back = q.requeue_unstarted(
        tmp_path, _did(1), holder_attempt=int(reclaimer["attempts"])
    )
    assert back["status"] == "queued"
    assert back["attempts"] == 1
    assert back["claimed_at"] is None
    assert back["reason"] is None
    assert back["reoffer_count"] == 2
    assert back["status_detail"] == q.STATUS_DETAIL_LEASE_CONTENDED
    assert [entry["attempt"] for entry in back["attempts_log"]] == [1]
    assert (
        back["attempts_log"][0]["status"],
        back["attempts_log"][0]["reason"],
    ) == ("ambiguous", "lease_lapsed")
    assert _queued_path(tmp_path, 1).exists()


def test_holder_identity_is_a_no_op_for_a_sole_holder_settle(tmp_path):
    """Single-holder delivery settles exactly as before, identity or not."""
    _admit(tmp_path, 1)
    claimed = q.claim_next(tmp_path, target_profile="bravo", lease_ok=True)
    assert claimed["attempts"] == 1

    settled = q.settle(
        tmp_path, _did(1), status="delivered", reply="pong", holder_attempt=1
    )
    assert (settled["status"], settled["reply"]) == ("delivered", "pong")
    entry = settled["attempts_log"][0]
    assert (entry["status"], entry["reason"]) == ("delivered", None)
    assert entry["ended_at"] is not None

    # The same holder replaying the same status is still the idempotent return ...
    assert q.settle(tmp_path, _did(1), status="delivered", holder_attempt=1) == settled
    # ... and ValueError stays reserved for a conflicting double-settle, whether the
    # identity is carried or omitted (omitted is exactly today's behaviour).
    with pytest.raises(ValueError):
        q.settle(tmp_path, _did(1), status="failed", holder_attempt=1)
    with pytest.raises(ValueError):
        q.settle(tmp_path, _did(1), status="failed")


def test_holder_identity_is_a_no_op_for_a_sole_holder_requeue(tmp_path):
    """A sole holder's requeue_unstarted still rolls the uncharged attempt back."""
    _admit(tmp_path, 1)
    claimed = q.claim_next(tmp_path, target_profile="bravo", lease_ok=True)
    assert claimed["attempts"] == 1

    back = q.requeue_unstarted(tmp_path, _did(1), holder_attempt=1)
    assert back["status"] == "queued"
    assert back["attempts"] == 0
    assert back["attempts_log"] == []
    assert back["reoffer_count"] == 1
    assert back["claimed_at"] is None
    assert back["status_detail"] == q.STATUS_DETAIL_LEASE_CONTENDED
    assert _queued_path(tmp_path, 1).exists()
    assert not _claimed_path(tmp_path, 1).exists()

    # The next claim charges its own attempt, and the omitted-identity call rolls
    # that claim back too -- unchanged.
    again = q.claim_next(tmp_path, target_profile="bravo", lease_ok=True)
    assert again["attempts"] == 1
    assert q.requeue_unstarted(tmp_path, _did(1))["attempts"] == 0
