"""Durable mailbox invariants, using real disk and exec boundaries."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.mark.parametrize("terminal_status", ["settled", "failed", "cancelled"])
def test_delivery_is_idempotent_fenced_and_permanent(tmp_path, terminal_status):
    from tools import bot_live_delivery as mailbox

    owner = dict(profile_home=str(tmp_path.resolve()), session_id="chat",
                 lease_id="lease", live_session_id="live")
    delivery_id = "a" * 32
    queued = mailbox.deliver_to_live_owner(tmp_path, owner, "hello", delivery_id=delivery_id)
    assert queued["status"] == "queued"
    assert mailbox.deliver_to_live_owner(tmp_path, owner, "hello", delivery_id=delivery_id) == queued
    with pytest.raises(ValueError):
        mailbox.deliver_to_live_owner(tmp_path, owner, "different", delivery_id=delivery_id)
    assert mailbox.claim_pending_delivery(tmp_path, dict(owner, lease_id="other")) is None
    assert mailbox.claim_pending_delivery(tmp_path, dict(owner, live_session_id="other")) is None
    script = (
        "import json,sys; from tools.bot_live_delivery import claim_pending_delivery; "
        "print(json.dumps(claim_pending_delivery(sys.argv[1],json.loads(sys.argv[2]))))"
    )
    children = [subprocess.Popen([sys.executable, "-c", script, str(tmp_path), json.dumps(owner)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True) for _ in range(2)]
    results = []
    for child in children:
        out, err = child.communicate(timeout=30)
        assert child.returncode == 0, err
        results.append(json.loads(out))
    claims = [r for r in results if r is not None]
    assert len(claims) == 1 and claims[0]["message"] == "hello"
    assert mailbox.read_delivery_result(tmp_path, delivery_id)["status"] == "claimed"
    assert mailbox.claim_pending_delivery(tmp_path, owner) is None
    receipt = mailbox.complete_delivery(tmp_path, delivery_id, status=terminal_status, reply="answer")
    assert mailbox.read_delivery_result(tmp_path, delivery_id) == receipt
    assert mailbox.complete_delivery(tmp_path, delivery_id, status=terminal_status, reply="answer") == receipt
    with pytest.raises(ValueError):
        mailbox.complete_delivery(tmp_path, delivery_id, status=terminal_status, reply="rewrite")
    assert mailbox.deliver_to_live_owner(tmp_path, owner, "hello", delivery_id=delivery_id) == receipt
    assert mailbox.claim_pending_delivery(tmp_path, owner) is None
    if os.name != "nt":
        for path in (tmp_path / "runtime" / mailbox.DELIVERY_DIR_NAME).iterdir():
            assert path.stat().st_mode & 0o077 == 0


def test_fifo_survives_clock_rollback(tmp_path, monkeypatch):
    from tools import bot_live_delivery as mailbox

    owner = dict(profile_home=str(tmp_path.resolve()), session_id="chat",
                 lease_id="lease", live_session_id="live")
    for timestamp, message in ((100, "first"), (90, "second")):
        monkeypatch.setattr(mailbox.time, "time_ns", lambda: timestamp)
        mailbox.deliver_to_live_owner(tmp_path, owner, message)
    assert mailbox.claim_pending_delivery(tmp_path, owner)["message"] == "first"
    assert mailbox.claim_pending_delivery(tmp_path, owner)["message"] == "second"


@pytest.mark.parametrize("capable", [True, False])
def test_only_canonical_capable_owner_receives_across_compression(tmp_path, capable):
    from hermes_state import SessionDB
    from hermes_cli.active_sessions import try_acquire_active_session, transfer_active_session
    from tools import bot_live_delivery as mailbox

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="chat", source="cli")
    db.set_session_title("chat", "Bot Chat")
    meta = dict(live_session_id="live", bot_live_delivery_consumer=capable)
    lease, refusal = try_acquire_active_session(session_id="chat", surface="desktop", config={},
                                               registry_home=tmp_path, metadata=meta)
    assert refusal is None
    try:
        owner = mailbox.find_canonical_live_owner(tmp_path)
        if not capable:
            assert owner is None
            return
        assert owner["lease_id"] == lease.lease_id
        queued = mailbox.deliver_to_live_owner(tmp_path, owner, "before compression")
        db.end_session("chat", "compression")
        db.create_session(session_id="tip", source="cli", parent_session_id="chat")
        assert transfer_active_session(lease, session_id="tip", metadata=meta)
        current = mailbox.find_canonical_live_owner(tmp_path)
        assert current["session_id"] == "tip"
        claim = mailbox.claim_pending_delivery(tmp_path, current)
        assert claim["delivery_id"] == queued["delivery_id"]
        assert claim["session_id"] == "chat"
        assert mailbox.claim_pending_delivery(tmp_path, current) is None
    finally:
        lease.release()
        db.close()


def _lease_owner_who_exits(registry_home, session_id, live_session_id):
    """A lease whose owning process is gone: how a stranded record's owner actually looks."""
    from pathlib import Path

    script = (
        "import json,sys;"
        "from hermes_cli.active_sessions import try_acquire_active_session as acquire;"
        "lease,refusal=acquire(session_id=sys.argv[1],surface='desktop',config={},registry_home=sys.argv[2],"
        "metadata={'live_session_id':sys.argv[3],'bot_live_delivery_consumer':True});"
        "print(json.dumps({'lease_id':lease.lease_id if lease else None,"
        "'refusal':(str(refusal) if refusal else None)}))"
    )
    child = subprocess.run(
        [sys.executable, "-c", script, session_id, str(registry_home), live_session_id],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=60,
    )
    assert child.returncode == 0, child.stderr
    payload = json.loads(child.stdout.strip().splitlines()[-1])
    assert payload["refusal"] is None, payload["refusal"]
    assert payload["lease_id"]
    return payload["lease_id"]


def _chat_home(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="chat", source="cli")
    db.set_session_title("chat", "Bot Chat")
    return db


def _live_owner(tmp_path, session_id, live_session_id, *, surface="tui"):
    from hermes_cli.active_sessions import try_acquire_active_session

    lease, refusal = try_acquire_active_session(
        session_id=session_id, surface=surface, config={}, registry_home=tmp_path,
        metadata={"live_session_id": live_session_id, "bot_live_delivery_consumer": True},
    )
    assert refusal is None, refusal
    assert lease is not None
    return lease


def test_a_dead_owner_lease_is_reclaimed_by_the_live_owner(tmp_path):
    from hermes_cli.active_sessions import active_session_registry_snapshot
    from tools import bot_live_delivery as mailbox

    db = _chat_home(tmp_path)
    try:
        dead_lease = _lease_owner_who_exits(tmp_path, "chat", "stale-live")
        stranded = mailbox.deliver_to_live_owner(
            tmp_path, dict(profile_home=str(tmp_path.resolve()), session_id="chat",
                           lease_id=dead_lease, live_session_id="stale-live"),
            "stranded peer message")
        assert stranded["status"] == "queued"

        live = _live_owner(tmp_path, "chat", "live-now")
        try:
            owner = mailbox.find_canonical_live_owner(tmp_path)
            assert owner is not None and owner["lease_id"] == live.lease_id
            claimed = mailbox.claim_pending_delivery(tmp_path, owner)
            assert claimed is not None
            assert claimed["delivery_id"] == stranded["delivery_id"]
            assert claimed["message"] == "stranded peer message"
            assert [e["lease_id"] for e in
                    active_session_registry_snapshot(registry_home=tmp_path)] == [live.lease_id]
        finally:
            live.release()
    finally:
        db.close()


def test_a_record_pinned_to_a_live_owner_is_never_stolen(tmp_path):
    from tools import bot_live_delivery as mailbox

    db = _chat_home(tmp_path)
    try:
        held = _live_owner(tmp_path, "chat", "live-owner")
        queued = mailbox.deliver_to_live_owner(
            tmp_path, dict(profile_home=str(tmp_path.resolve()), session_id="chat",
                           lease_id=held.lease_id, live_session_id="live-owner"),
            "held by a live owner")
        db.end_session("chat", "compression")
        db.create_session(session_id="tip", source="cli", parent_session_id="chat")
        successor = _live_owner(tmp_path, "tip", "live-successor")
        try:
            owner = mailbox.find_canonical_live_owner(tmp_path)
            assert owner is not None and owner["lease_id"] == successor.lease_id
            assert mailbox.claim_pending_delivery(tmp_path, owner) is None
            assert mailbox.read_delivery_result(tmp_path, queued["delivery_id"])["status"] == "queued"
        finally:
            successor.release()
            held.release()
    finally:
        db.close()


def test_reclaiming_an_already_delivered_record_settles_it_without_rerunning(tmp_path):
    from tools import bot_live_delivery as mailbox

    db = _chat_home(tmp_path)
    try:
        dead_lease = _lease_owner_who_exits(tmp_path, "chat", "stale-live")
        stranded = mailbox.deliver_to_live_owner(
            tmp_path, dict(profile_home=str(tmp_path.resolve()), session_id="chat",
                           lease_id=dead_lease, live_session_id="stale-live"),
            "already delivered body")
        db.append_message("chat", role="user", content="already delivered body")

        live = _live_owner(tmp_path, "chat", "live-now")
        try:
            assert mailbox.claim_pending_delivery(tmp_path, mailbox.find_canonical_live_owner(tmp_path)) is None
            receipt = mailbox.read_delivery_result(tmp_path, stranded["delivery_id"])
            assert receipt["status"] == "settled"
            assert receipt["reply"] == ""
        finally:
            live.release()
    finally:
        db.close()


def test_a_truncated_record_is_quarantined_and_never_wedges_the_lane(tmp_path):
    from tools import bot_live_delivery as mailbox

    owner = dict(profile_home=str(tmp_path.resolve()), session_id="chat",
                 lease_id="lease", live_session_id="live")
    root = tmp_path / "runtime" / mailbox.DELIVERY_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    (root / "truncated.json").write_text('{"delivery_id": "truncated", "status": "que')

    queued = mailbox.deliver_to_live_owner(tmp_path, owner, "still admitted")
    assert queued["status"] == "queued"
    claimed = mailbox.claim_pending_delivery(tmp_path, owner)
    assert claimed is not None and claimed["delivery_id"] == queued["delivery_id"]
    assert not (root / "truncated.json").exists()
    assert any((root / "quarantine").iterdir())


def _age_lease_stamp(registry_home, lease_id, *, seconds):
    """Backdate a lease's stamps the way a poll loop that stopped renewing leaves them."""
    path = Path(registry_home) / "runtime" / "active_sessions.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    for entry in payload["entries"]:
        if entry["lease_id"] == lease_id:
            entry["updated_at"] -= seconds
            entry["started_at"] = min(entry["started_at"], entry["updated_at"])
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_a_consumer_that_stopped_polling_is_not_a_live_owner(tmp_path):
    """Growth-stop: a lease whose poll loop died must not keep pinning new mail.

    Measured fleet case: the only capability-advertising entry was ~14.9h stale (its process was
    still alive) and still resolved as the canonical owner, so every new peer message was pinned
    to a mailbox nobody was draining. Renewal is what makes a lease a destination again.
    """
    from hermes_cli.active_sessions import touch_active_session_lease
    from tools import bot_live_delivery as mailbox

    db = _chat_home(tmp_path)
    try:
        lease = _live_owner(tmp_path, "chat", "live")
        assert mailbox.find_canonical_live_owner(tmp_path)["lease_id"] == lease.lease_id
        _age_lease_stamp(tmp_path, lease.lease_id,
                         seconds=mailbox.LIVE_CONSUMER_TTL_SECONDS + 60)
        assert mailbox.find_canonical_live_owner(tmp_path) is None
        assert touch_active_session_lease(lease.lease_id, registry_home=tmp_path,
                                          live_session_id="live", force=True) is True
        assert mailbox.find_canonical_live_owner(tmp_path)["lease_id"] == lease.lease_id
    finally:
        lease.release()
        db.close()


def _age_record(profile_home, delivery_id, *, seconds):
    """Backdate a queued record's clock, as one left in the spool that long would be."""
    path = Path(profile_home) / "runtime" / "bot_live_delivery" / f"{delivery_id}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["created_at"] = time.time_ns() - int(seconds * 1e9)
    path.write_text(json.dumps(record), encoding="utf-8")
    return record


def test_an_unreachable_record_becomes_an_explicit_failure(tmp_path):
    """Expiry: a record this consumer can never reach is reported, never left silent in the spool.

    A record on a foreign lineage (its session is not on this consumer's compression chain) can
    never be claimed, and unlike a dead-owner reclaim there is no store body to settle it with:
    past the window it must become an explicit failure that carries the ask back to the sender.
    """
    from tools import bot_live_delivery as mailbox

    db = _chat_home(tmp_path)
    try:
        lease = _live_owner(tmp_path, "chat", "live")
        owner = {"profile_home": str(tmp_path.resolve()), "session_id": "chat",
                 "lease_id": lease.lease_id, "live_session_id": "live"}
        foreign = dict(owner, session_id="elsewhere", lease_id="dead-lease",
                       live_session_id="dead-live")
        stale = mailbox.deliver_to_live_owner(tmp_path, foreign, "unreachable ask")
        _age_record(tmp_path, stale["delivery_id"],
                    seconds=mailbox.UNREACHABLE_AFTER_SECONDS + 60)

        assert mailbox.claim_pending_delivery(tmp_path, owner) is None
        receipt = mailbox.read_delivery_result(tmp_path, stale["delivery_id"])
        assert receipt["status"] == "failed"
        assert receipt["reason"] == "no_live_consumer"
        assert receipt["message"] == "unreachable ask"

        recent = mailbox.deliver_to_live_owner(tmp_path, dict(foreign, lease_id="dead-two",
                                                             live_session_id="dead-two"),
                                               "recent ask")
        assert mailbox.claim_pending_delivery(tmp_path, owner) is None
        assert mailbox.read_delivery_result(tmp_path, recent["delivery_id"])["status"] == "queued"
    finally:
        lease.release()
        db.close()


def test_delivery_keeps_the_sender_and_refuses_a_different_one_under_the_same_id(tmp_path):
    from tools import bot_live_delivery as mailbox

    owner = dict(profile_home=str(tmp_path.resolve()), session_id="chat", lease_id="lease", live_session_id="live")
    author = {"id": "bot:coder", "name": "coder", "is_bot": True}
    queued = mailbox.deliver_to_live_owner(tmp_path, owner, "hello", delivery_id="b" * 32, author=author)
    assert queued["author"] == author
    assert mailbox.deliver_to_live_owner(tmp_path, owner, "hello", delivery_id="b" * 32, author=author) == queued
    with pytest.raises(ValueError):
        mailbox.deliver_to_live_owner(tmp_path, owner, "hello", delivery_id="b" * 32, author={**author, "id": "bot:other"})
    assert "author" not in mailbox.deliver_to_live_owner(tmp_path, owner, "no sender", delivery_id="c" * 32)
