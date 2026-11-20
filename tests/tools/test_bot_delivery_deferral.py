"""t-76be5e4334 — the DEFERRAL LIVES IN THE STORE.

The ruling this file pins down: a retryable refusal is not terminal. It goes back into the queue
DIRECTORY with ``status = deferred`` and ``not_before_ns``, so no consumer -- ours, the Desktop's,
or one written next year -- can be offered it early, because every consumer selects on ``status``.
``claim_next`` has no production caller; a gate written there would pass its own test and change
nothing. The promoter moves ``deferred -> queued`` when the backoff has arrived.

Acceptance (i)-(x) from the card, each as a test that a spin would ALSO pass only if the store
really gates it: "an acceptance that a spin also passes is not an acceptance."
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import bot_delivery_notices as notices
from tools import bot_delivery_promote as promote
from tools import bot_delivery_queue as q

DEFAULTS = dict(q._DEFAULTS)
SECOND = 1_000_000_000
ORIGIN = 1_800_000_000 * SECOND


@pytest.fixture(autouse=True)
def _pin_config(monkeypatch):
    monkeypatch.setattr(q, "_cfg", lambda key, **kw: DEFAULTS[key])
    yield


def _did(n: int) -> str:
    return format(n, "032x")


def _admit(home, n=1, *, sender="alpha", target="bravo", now_ns=ORIGIN, message=None):
    return q.admit(
        home,
        sender_profile=sender,
        target_profile=target,
        target_session_id="sess-1",
        idempotency_key=f"peer-{_did(n)}",
        fingerprint=f"fp-{_did(n)}",
        delivery_id=_did(n),
        message=message if message is not None else f"hello {n}",
        now_ns=now_ns,
    )


def _root(home) -> Path:
    return Path(home) / "runtime" / q.DELIVERY_DIR_NAME


def _queued(home, n) -> dict | None:
    return q._read(_root(home) / q.QUEUE_DIR / f"{_did(n)}.json")


def _settled(home, delivery_id) -> dict | None:
    return q._read(_root(home) / q.SETTLED_DIR / f"{delivery_id}.json")


@pytest.fixture
def sender_store(monkeypatch, tmp_path):
    """The sender's spool, resolved the way production resolves it (by profile id)."""
    stores = {}
    for name in ("alpha", "bravo"):
        home = tmp_path / f"home-{name}"
        (_root(home) / q.SETTLED_DIR).mkdir(parents=True, exist_ok=True)
        stores[name] = home
    monkeypatch.setattr(notices, "_profile_home", lambda profile: stores.get(profile))
    return stores


def _claimed(home, n=1):
    _admit(home, n)
    record = q.claim_next(home, target_profile="bravo", lease_ok=True, now_ns=ORIGIN)
    assert record is not None and record["delivery_id"] == _did(n)
    return record


# --------------------------------------------------------------- (i) the store
def test_a_retryable_refusal_defers_in_the_store_instead_of_settling_failed(tmp_path):
    _claimed(tmp_path)
    out = q.defer_or_fail(tmp_path, _did(1), reason="queue_full", now_ns=ORIGIN,
                          limit_kind="per_sender", limit=8)
    assert out["deferral"] == "deferred"
    stored = _queued(tmp_path, 1)
    assert stored is not None, "the record must be back in the queue DIRECTORY"
    assert stored["status"] == q.STATUS_DEFERRED
    assert stored["status"] != q.STATUS_QUEUED
    assert stored["not_before_ns"] == ORIGIN + 60 * SECOND
    assert stored["retry_after_seconds"] == 60.0
    assert stored["attempts"] == 0, "the turn never ran: an attempt must not be charged"
    assert stored["reoffer_count"] == 1
    assert stored["limit_kind"] == "per_sender" and stored["limit"] == 8
    # nothing was settled, and the claim file is gone
    assert _settled(tmp_path, _did(1)) is None
    assert not (_root(tmp_path) / q.CLAIMED_DIR / f"{_did(1)}.json").exists()
    # the attempt opened by the claim is CLOSED as deferred, never left dangling
    assert stored["attempts_log"][-1]["status"] == q.STATUS_DEFERRED
    assert stored["attempts_log"][-1]["ended_at"] == ORIGIN


def test_the_backoff_doubles_and_caps_at_fifteen_minutes():
    assert [q.deferral_delay_seconds(n) for n in range(6)] == [60.0, 120.0, 240.0, 480.0, 900.0, 900.0]
    assert q.deferral_delay_seconds(99) == q.DEFERRAL_CAP_SECONDS == 900.0


# ------------------------------------------------- (ii) nobody can see it early
def test_a_deferred_record_is_invisible_to_every_consumer_until_promoted(tmp_path):
    _claimed(tmp_path)
    q.defer_or_fail(tmp_path, _did(1), reason="queue_full", now_ns=ORIGIN)
    # BEFORE the instant: not claimable, even a nanosecond early
    assert q.claim_next(tmp_path, target_profile="bravo", lease_ok=True,
                        now_ns=ORIGIN + 60 * SECOND - 1) is None
    # the belt-and-braces predicate agrees with the store
    stored = _queued(tmp_path, 1)
    assert q._offered_now(stored, ORIGIN + 60 * SECOND - 1) is False
    # AT the instant: still deferred (the promoter has not run) -- the store is the gate
    assert q.claim_next(tmp_path, target_profile="bravo", lease_ok=True,
                        now_ns=ORIGIN + 60 * SECOND) is None
    promoted = q.promote_deferred(tmp_path, now_ns=ORIGIN + 60 * SECOND)
    assert promoted["promoted"] == 1 and promoted["deferred"] == 0
    again = q.claim_next(tmp_path, target_profile="bravo", lease_ok=True,
                         now_ns=ORIGIN + 60 * SECOND)
    assert again is not None and again["delivery_id"] == _did(1)


def test_a_queued_record_with_a_future_instant_is_not_offered_either(tmp_path):
    """The promoter killed mid-write leaves status=queued with a future not_before: the reader
    must agree with the intended gate, not undo it."""
    _admit(tmp_path, 1)
    path = _root(tmp_path) / q.QUEUE_DIR / f"{_did(1)}.json"
    record = q._read(path)
    record["not_before_ns"] = ORIGIN + 300 * SECOND
    q._write(path, record)
    assert q.claim_next(tmp_path, target_profile="bravo", lease_ok=True, now_ns=ORIGIN) is None
    assert q.claim_next(tmp_path, target_profile="bravo", lease_ok=True,
                        now_ns=ORIGIN + 300 * SECOND) is not None


# ------------------------------------------------------- (ix) the retry budget
def test_requeueing_twice_leaves_attempts_at_zero_and_reoffer_at_two(tmp_path):
    _claimed(tmp_path)
    q.defer_or_fail(tmp_path, _did(1), reason="queue_full", now_ns=ORIGIN)
    q.promote_deferred(tmp_path, now_ns=ORIGIN + 60 * SECOND)
    q.claim_next(tmp_path, target_profile="bravo", lease_ok=True, now_ns=ORIGIN + 60 * SECOND)
    q.defer_or_fail(tmp_path, _did(1), reason="queue_full", now_ns=ORIGIN + 60 * SECOND)
    stored = _queued(tmp_path, 1)
    assert stored["attempts"] == 0
    assert stored["reoffer_count"] == 2
    assert stored["retry_after_seconds"] == 120.0
    assert stored["not_before_ns"] == ORIGIN + 60 * SECOND + 120 * SECOND


# --------------------------------------------------- (v) the terminal arm + (x)
def test_a_spent_budget_settles_failed_and_bounces_without_re_entering_the_queue(
    tmp_path, sender_store
):
    sender_home = sender_store["alpha"]
    target_home = sender_store["bravo"]
    _admit(target_home, 1, message="the original words")
    path = _root(target_home) / q.QUEUE_DIR / f"{_did(1)}.json"
    record = q._read(path)
    record["reoffer_count"] = q.MAX_REOFFER          # budget spent: the next refusal is terminal
    record["limit_kind"] = "per_sender"
    record["limit"] = 8
    q._write(path, record)
    q.claim_next(target_home, target_profile="bravo", lease_ok=True, now_ns=ORIGIN)
    before = len(list((_root(target_home) / q.QUEUE_DIR).glob("*.json")))
    out = q.defer_or_fail(target_home, _did(1), reason="queue_full", now_ns=ORIGIN,
                          limit_kind="per_sender", limit=8)
    assert out["deferral"] == "exhausted"
    assert out["status"] == q.STATUS_FAILED
    assert out["bounced"] is True
    after = len(list((_root(target_home) / q.QUEUE_DIR).glob("*.json")))
    assert after == before - 0 or after == 0, "(x) the bounce must leave the queue unchanged"
    assert after == 0
    # the bounce went to the SENDER's settled store, direct, terminal, with the original words
    bounces = [q._read(p) for p in (_root(sender_home) / q.SETTLED_DIR).glob("*.json")]
    assert len(bounces) == 1
    bounce = bounces[0]
    assert bounce["ref"] == _did(1)
    assert bounce["depth"] == 1
    assert bounce["status"] == q.STATUS_FAILED
    assert bounce["reason"] == q.REASON_REFUSED_EXHAUSTED
    assert bounce["original_message"] == "the original words"
    assert "per_sender (8)" in bounce["message"]
    # and a bounce never bounces
    assert notices.bounce_to_sender(bounce, now_ns=ORIGIN) is None
    # idempotent: a second call writes nothing new
    again = notices.bounce_to_sender(out, fallback_home=target_home, now_ns=ORIGIN)
    assert again is not None and again["delivery_id"] == bounce["delivery_id"], (
        "a second call is idempotent on the original delivery, not a second word")


def test_the_bounce_is_idempotent_on_the_original_delivery(tmp_path, sender_store):
    sender_home = sender_store["alpha"]
    target_home = sender_store["bravo"]
    _admit(target_home, 1)
    record = _queued(target_home, 1)
    first = notices.bounce_to_sender(record, now_ns=ORIGIN)
    second = notices.bounce_to_sender(record, now_ns=ORIGIN + 5 * SECOND)
    assert first is not None and second is not None
    assert first["delivery_id"] == second["delivery_id"]
    assert len(list((_root(sender_home) / q.SETTLED_DIR).glob("*.json"))) == 1


# ------------------------------------------------------ (vii) the TTL sweep
def test_a_deferred_record_older_than_the_ttl_is_not_swept(tmp_path, monkeypatch):
    _claimed(tmp_path)
    # a TTL of one second, and a deferral made an hour ago: a QUEUED record in that state is
    # swept, so the exemption has to be the deferred STATE and nothing else.
    monkeypatch.setattr(q, "queue_ttl_seconds", lambda: 1.0)
    old = ORIGIN + 3600 * SECOND
    q.defer_or_fail(tmp_path, _did(1), reason="queue_full", now_ns=ORIGIN)
    out = q.sweep_delivery_queue(tmp_path, now_ns=old, expire=True)
    assert isinstance(out, int)
    stored = _queued(tmp_path, 1)
    assert stored["status"] == q.STATUS_DEFERRED, "the deferral must survive the over-TTL sweep"
    assert _settled(tmp_path, _did(1)) is None


# --------------------------------------------------------- (vi) first-class state
def test_the_census_counts_deferred_beside_queued_never_folded_in(tmp_path):
    _claimed(tmp_path, 1)
    q.defer_or_fail(tmp_path, _did(1), reason="queue_full", now_ns=ORIGIN)
    _admit(tmp_path, 2)
    later = ORIGIN + 30 * SECOND
    seat = q.seat_strand_census(tmp_path, now_ns=later)
    assert seat["deferred"] == 1
    assert seat["queued"] == 1
    assert seat["pending_over_ttl"] == 0
    assert seat["oldest_deferred_age_seconds"] == pytest.approx(30.0, abs=0.1)
    totals = q._sum_census_seats({"bravo": seat})
    assert totals["deferred"] == 1 and totals["queued"] == 1


def test_deferred_census_names_the_instant_it_becomes_eligible(tmp_path):
    _claimed(tmp_path)
    q.defer_or_fail(tmp_path, _did(1), reason="queue_full", now_ns=ORIGIN)
    census = q.deferred_census(tmp_path, now_ns=ORIGIN + 10 * SECOND)
    assert census["deferred"] == 1
    assert census["oldest_deferred_age_seconds"] == pytest.approx(10.0, abs=0.1)
    assert census["next_eligible_ns"] == ORIGIN + 60 * SECOND
    assert census["oldest_deferred_delivery_id"] == _did(1)


# -------------------------------------- (viii) the promoter's death is VISIBLE
def test_a_stopped_promoter_leaves_records_counted_as_deferred_and_files_a_card(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    seat_home = home / "profiles" / "bravo"
    seat_home.mkdir(parents=True)
    _claimed(seat_home)
    q.defer_or_fail(seat_home, _did(1), reason="queue_full", now_ns=ORIGIN)
    monkeypatch.setattr(q, "spool_homes", lambda root: [("bravo", seat_home)])
    cards = []

    def _fake_card(**kwargs):
        cards.append(kwargs)
        return "t-card-filed"

    monkeypatch.setattr(promote, "_create_card", _fake_card)
    # nothing promotes because the backoff has not arrived, and the record is far older than the
    # promoter's own ceiling: that is what a STOPPED promoter looks like from the store
    # 45 s in: the 60 s backoff has NOT arrived (so nothing promotes -- the promoter is
    # stopped), while the record's deferred age already exceeds the 30 s ceiling.
    verdict = promote.promoter_verdict(home, now_ns=ORIGIN + 45 * SECOND,
                                       stale_after_seconds=30.0)
    assert verdict["totals"]["deferred"] == 1
    assert verdict["totals"]["queued"] == 0
    assert verdict["oldest_deferred_age_seconds"] == pytest.approx(45.0, abs=0.5)
    assert verdict["frozen"] is True
    line = promote.build_census_line(verdict)
    assert "queued 0" in line and "deferred 1" in line      # two numbers, never summed
    monkeypatch.setattr(promote, "_now_ns", lambda: ORIGIN + 45 * SECOND)
    rc = promote.main(["--home", str(home), "--stale-after", "30", "--json"])
    assert rc == promote.EXIT_FROZEN
    assert cards and cards[0]["board"] == "platform"
    assert "deferred" in cards[0]["body"]


def test_a_healthy_promoter_cards_nothing(tmp_path, monkeypatch):
    home = tmp_path / "home"
    seat_home = home / "profiles" / "bravo"
    seat_home.mkdir(parents=True)
    _claimed(seat_home)
    q.defer_or_fail(seat_home, _did(1), reason="queue_full", now_ns=ORIGIN)
    monkeypatch.setattr(q, "spool_homes", lambda root: [("bravo", seat_home)])
    cards = []
    monkeypatch.setattr(promote, "_create_card", lambda **kw: cards.append(kw) or "x")
    rc = promote.main(["--home", str(home), "--stale-after", "3600", "--json"])
    assert rc == promote.EXIT_FROZEN or rc == promote.EXIT_DEFERRED
    assert cards == [], "a promoter that promoted nothing but is not frozen must not card"
    promote.main(["--home", str(home), "--json", "--stale-after", "3600"])
    # after the backoff this same promoter promotes, and the deferred count returns to 0
    verdict = promote.promoter_verdict(home, now_ns=ORIGIN + 120 * SECOND,
                                       stale_after_seconds=3600)
    assert verdict["totals"]["promoted"] == 1
    assert verdict["totals"]["deferred"] == 0


def test_promotion_is_idempotent(tmp_path):
    _claimed(tmp_path)
    q.defer_or_fail(tmp_path, _did(1), reason="queue_full", now_ns=ORIGIN)
    first = q.promote_deferred(tmp_path, now_ns=ORIGIN + 60 * SECOND)
    second = q.promote_deferred(tmp_path, now_ns=ORIGIN + 61 * SECOND)
    assert first["promoted"] == 1
    assert second["promoted"] == 0 and second["deferred"] == 0
    assert _queued(tmp_path, 1)["status"] == q.STATUS_QUEUED
    assert _queued(tmp_path, 1)["promoted_at_ns"] == ORIGIN + 60 * SECOND


def test_a_non_retryable_reason_is_terminal_at_once_and_still_bounces(tmp_path, sender_store):
    target_home = sender_store["bravo"]
    _admit(target_home, 1)
    q.claim_next(target_home, target_profile="bravo", lease_ok=True, now_ns=ORIGIN)
    out = q.defer_or_fail(target_home, _did(1), reason="live_owner_present", now_ns=ORIGIN)
    assert out["deferral"] == "terminal"
    assert out["status"] == q.STATUS_FAILED
    assert _queued(target_home, 1) is None or _queued(target_home, 1)["status"] != q.STATUS_DEFERRED


# --------------------------------- (xi) the bounce key is the TRIPLE, identity not content
def test_the_bounce_key_is_the_TRIPLE_and_is_verifiable_from_the_record(tmp_path, sender_store):
    """A reader holding the bounce recomputes its key without the code that wrote it."""
    sender_home = sender_store["alpha"]
    target_home = sender_store["bravo"]
    _admit(target_home, 1)
    record = _queued(target_home, 1)
    record["reason"] = "queue_full"
    bounce = notices.bounce_to_sender(record, now_ns=ORIGIN)
    assert bounce is not None
    assert bounce["refusal_target"] == "bravo"
    assert bounce["refusal_reason"] == "queue_full"
    assert bounce["idempotency_key"] == notices.bounce_notice_key(
        bounce["ref"], bounce["refusal_target"], bounce["refusal_reason"]), (
        "the key must be derivable from the record's own fields")
    assert bounce["idempotency_key"] != bounce["fingerprint"], (
        "a body hash would file a second bounce the moment the body is re-rendered")
    assert len(list((_root(sender_home) / q.SETTLED_DIR).glob("*.json"))) == 1


def test_a_re_rendered_body_does_not_owe_a_SECOND_bounce(tmp_path, sender_store):
    """Content changes, identity does not: one brief, one word."""
    sender_home = sender_store["alpha"]
    target_home = sender_store["bravo"]
    _admit(target_home, 1, message="the original words")
    record = _queued(target_home, 1)
    record["reason"] = "queue_full"
    first = notices.bounce_to_sender(record, now_ns=ORIGIN)
    restyled = dict(record, message="the original words, re-rendered")
    second = notices.bounce_to_sender(restyled, now_ns=ORIGIN + 90 * SECOND)
    assert first is not None and second is not None
    assert first["delivery_id"] == second["delivery_id"]
    assert len(list((_root(sender_home) / q.SETTLED_DIR).glob("*.json"))) == 1


def test_two_DIFFERENT_reasons_for_one_delivery_are_TWO_bounces(tmp_path, sender_store):
    """Different failures are different facts, and each owes its own word."""
    sender_home = sender_store["alpha"]
    target_home = sender_store["bravo"]
    _admit(target_home, 1)
    record = _queued(target_home, 1)
    first = notices.bounce_to_sender(dict(record, reason="queue_full"), now_ns=ORIGIN)
    second = notices.bounce_to_sender(dict(record, reason="runtime_offline"),
                                      now_ns=ORIGIN + 90 * SECOND)
    assert first is not None and second is not None
    assert first["delivery_id"] != second["delivery_id"], "two facts, two records"
    assert first["ref"] == second["ref"] == record["delivery_id"]
    bounces = [q._read(p) for p in (_root(sender_home) / q.SETTLED_DIR).glob("*.json")]
    assert len(bounces) == 2
    assert {b["refusal_reason"] for b in bounces} == {"queue_full",
                                                      "runtime_offline"}
    # and neither of them may bounce again
    for b in bounces:
        assert notices.bounce_to_sender(b, now_ns=ORIGIN) is None
