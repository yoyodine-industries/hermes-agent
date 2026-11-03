"""The drainer must reach EVERY lane's spool, not just the gateway's own home (t_8a7a47d7).

The live failure this pins: 20 peer deliveries sat in two lanes' per-profile queues for
three days with ``attempts=0`` while the gateway's 30s sweep logged ``actions=0`` forever.
A peer delivery is admitted into its TARGET lane (``profiles/<lane>/runtime/bot_delivery``),
but the sweep and the drain only ever enumerated ``_default_home()``, so a named lane's
backlog was invisible -- and the sender's "queued" receipt was never made true.

Every assertion is on the real on-disk queue through the real sweep.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import sys
import time
from typing import Callable

import pytest

from gateway.platforms import api_server as api
from gateway.platforms import api_server_bot_delivery as drain
from tools import bot_delivery_queue as q
from tools import bot_relay

DEFAULTS = dict(q._DEFAULTS)


@pytest.fixture(autouse=True)
def _pin_config(monkeypatch):
    """Pin bot_mode config to the spec defaults so TTLs and budgets are deterministic."""
    monkeypatch.setattr(q, "_cfg", lambda key, **kw: DEFAULTS[key])


@pytest.fixture()
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    (root / "profiles").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root


def _lane(home, name):
    """A profile home the way the installer lays it out (no delivery spool yet)."""
    path = home / "profiles" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


class _LogCapture:
    """The module logger, captured: the deferral events are the only trace there is."""

    def __init__(self):
        self.lines = []

    def info(self, msg, *args, **kwargs):
        self.lines.append(msg % args if args else str(msg))

    def exception(self, msg, *args, **kwargs):  # pragma: no cover - failure path
        self.lines.append("EXCEPTION " + (msg % args if args else str(msg)))

    def debug(self, msg, *args, **kwargs):
        self.lines.append("debug " + (msg % args if args else str(msg)))

    def warning(self, msg, *args, **kwargs):
        self.lines.append("warning " + (msg % args if args else str(msg)))

    def __contains__(self, needle):
        return any(needle in line for line in self.lines)


def _did(n: int) -> str:
    return "%032x" % n


def _admit(lane_home, *, target, sender="platform-coder", message="hello", n=1, age_seconds=0):
    now_ns = time.time_ns() - int(age_seconds * 1_000_000_000)
    return q.admit(
        lane_home,
        sender_profile=sender,
        target_profile=target,
        target_session_id="sess-1",
        idempotency_key=f"peer-{_did(n)}",
        fingerprint=f"fp-{_did(n)}",
        delivery_id=_did(n),
        message=message,
        now_ns=now_ns,
    )


class _Adapter:
    """The gateway seam a drained turn sees: history, the turn, and the lane scope."""

    def __init__(self):
        self.calls = []
        self.scopes = []
        self.profile_during_turn = None
        # The AGENT-WORK cap, as the drainer and the expiry sweep both ask it.
        self._agent_gate_has_room: Callable[[], bool] = lambda: True

    async def _conversation_history_for_session(self, session_id):
        return []

    async def _run_agent(self, conversation_history=None, **kwargs):
        self.profile_during_turn = api._api_request_profile.get()
        self.calls.append(kwargs)
        return {"final_response": "pong"}, {}

    def _bot_send_record_status(self, delivery_id, record, **kwargs):
        return None

    @contextlib.contextmanager
    def _profile_scope(self, profile):
        self.scopes.append(profile)
        yield


# ── (1) the named lane is drained at all ──────────────────────────────────────

def test_the_sweep_drains_a_named_lanes_queue(home):
    lane = _lane(home, "platform-stl")
    record = _admit(lane, target="platform-stl", message="the DM nobody read")
    adapter = _Adapter()

    # The default home's own queue is EMPTY: before this fix the sweep found
    # nothing to do here and logged actions=0 while the lane's queue grew.
    assert q.queued_target_profiles(home) == []

    assert asyncio.run(drain.sweep_once(adapter, home)) == 1

    assert [call["user_message"] for call in adapter.calls] == ["the DM nobody read"]
    settled = q.read_record(lane, record["delivery_id"])
    assert settled["status"] == q.STATUS_DELIVERED
    assert [entry["attempt"] for entry in settled["attempts_log"]] == [1], (
        "the attempt must be recorded, not just the outcome")
    assert q.queue_depth(lane, "platform-stl") == 0


def test_a_drained_turn_runs_in_its_target_lanes_scope(home):
    lane = _lane(home, "research-stl")
    _admit(lane, target="research-stl", message="please read me")
    adapter = _Adapter()

    asyncio.run(drain.sweep_once(adapter, home))

    assert adapter.scopes == ["research-stl"], (
        "an unscoped turn applies the lane's session id to the DEFAULT home's state.db")
    assert adapter.profile_during_turn == "research-stl"


def test_the_sweep_never_plants_a_spool_in_an_unused_profile(home):
    _lane(home, "quiet-lane")
    _admit(_lane(home, "platform-stl"), target="platform-stl")

    asyncio.run(drain.sweep_once(_Adapter(), home))

    assert not (home / "profiles" / "quiet-lane" / "runtime" / "bot_delivery").exists(), (
        "walking every profile must not create queue state where none was ever used")


# ── (2) never attempted ⇒ offered to a turn before TTL can reap it ────────────

def test_a_never_attempted_record_is_drained_before_it_can_expire(home):
    lane = _lane(home, "platform-coder")
    age = q.queue_ttl_seconds() * 2 + 60
    record = _admit(lane, target="platform-coder", message="three days late", age_seconds=age)
    adapter = _Adapter()

    assert asyncio.run(drain.sweep_once(adapter, home)) == 1

    settled = q.read_record(lane, record["delivery_id"])
    assert settled["status"] == q.STATUS_DELIVERED, (
        "a record never offered to a turn must not be dead-lettered by the same tick's expiry")
    assert [call["user_message"] for call in adapter.calls] == ["three days late"]


# ── (3) an expiry that DOES happen goes back to the sender as a FAILURE ───────

def _offered(lane_home, target="platform-stl"):
    """An OFFERED record: claimed by a turn that STARTED, then handed back contended.

    This is the ONLY shape that still files a failure notice -- a record a turn was
    actually offered and left unread (t_43f11055 c). Since the A1/A6 evidence protocol,
    "offered" is the claim's own ``turn_started_at``, not the bare attempt counter: a
    claim that never started a turn was never offered the payload and classifies
    ``never_offered``.
    """
    record = q.claim_next(lane_home, target_profile=target, lease_ok=True)
    assert record is not None and record["attempts"] == 1
    assert q.note_turn_start(lane_home, record["delivery_id"]) is not None
    requeued = q.requeue(lane_home, record["delivery_id"])
    assert requeued["attempts"] == 1 and requeued["status"] == q.STATUS_QUEUED
    return requeued


def test_an_expired_record_files_one_sender_visible_failure(home):
    target = _lane(home, "platform-stl")
    sender = _lane(home, "platform-coder")
    age = q.queue_ttl_seconds() * 2 + 60
    _admit(target, target="platform-stl", sender="platform-coder", age_seconds=age)
    record = _offered(target)

    q.sweep_delivery_queue(target, on_expired=q.notice_expired_to_sender)

    assert q.read_record(target, record["delivery_id"])["status"] == q.STATUS_EXPIRED
    notices = [rec for rec in q._list(q.delivery_root(sender), q.QUEUE_DIR)]
    assert len(notices) == 1, "the sender must learn the delivery failed -- exactly once"
    notice = notices[0]
    assert notice["target_profile"] == "platform-coder"
    assert notice["sender_profile"] == q.EXPIRED_NOTICE_SENDER
    assert record["delivery_id"] in notice["message"]
    assert "NEVER read" in notice["message"] and "Escalate" in notice["message"]


def test_the_failure_notice_is_idempotent(home):
    target = _lane(home, "platform-stl")
    sender = _lane(home, "platform-coder")
    age = q.queue_ttl_seconds() * 2 + 60
    _admit(target, target="platform-stl", sender="platform-coder", age_seconds=age)
    _offered(target)

    q.sweep_delivery_queue(target, on_expired=q.notice_expired_to_sender)
    q.sweep_delivery_queue(target, on_expired=q.notice_expired_to_sender)

    assert len(q._list(q.delivery_root(sender), q.QUEUE_DIR)) == 1


def test_a_notice_about_a_notice_is_never_filed(home):
    lane = _lane(home, "platform-stl")
    record = {
        "delivery_id": _did(9),
        "sender_profile": q.EXPIRED_NOTICE_SENDER,
        "target_profile": "platform-stl",
        "created_at": time.time_ns() - 10**12,
        "attempts": 1,
        "attempts_log": [],
    }
    assert q.notice_expired_to_sender(record) is None


# ── (4) the cap and the classification: t_43f11055 ────────────────────────────

def test_a_full_agent_cap_expires_nothing(home, monkeypatch):
    """(a) The cap is a FLEET-WIDE condition, not this record's outcome."""
    lane = _lane(home, "platform-stl")
    age = q.queue_ttl_seconds() * 2 + 60
    record = _admit(lane, target="platform-stl", message="waiting on a free place",
                    age_seconds=age)
    calls = _LogCapture()
    monkeypatch.setattr(q, "logger", calls)
    adapter = _Adapter()
    adapter._agent_gate_has_room = lambda: False

    assert asyncio.run(drain.sweep_once(adapter, home)) == 0

    settled = q.read_record(lane, record["delivery_id"])
    assert settled is not None
    assert settled["status"] == q.STATUS_QUEUED, (
        "expiry dead-lettered a record the DRAIN was deferring on the cap")
    assert settled["attempts"] == 0
    assert "age_kept_agent_cap" in calls, "the deferral must be traceable"
    assert "age_kept_slot_held" not in calls, "the slot was free; the cap was not"
    assert adapter.calls == []


def test_the_backlog_runs_once_the_cap_frees_a_place(home):
    """The same record, one tick later: offered, attempted, delivered."""
    lane = _lane(home, "platform-stl")
    age = q.queue_ttl_seconds() * 2 + 60
    record = _admit(lane, target="platform-stl", message="late but not lost",
                    age_seconds=age)
    state = {"room": False}
    adapter = _Adapter()
    adapter._agent_gate_has_room = lambda: state["room"]

    asyncio.run(drain.sweep_once(adapter, home))
    assert q.read_record(lane, record["delivery_id"])["status"] == q.STATUS_QUEUED

    state["room"] = True
    assert asyncio.run(drain.sweep_once(adapter, home)) == 1

    settled = q.read_record(lane, record["delivery_id"])
    assert settled["status"] == q.STATUS_DELIVERED
    assert settled["attempts"] == 1, "an offered record records the attempt"
    assert [call["user_message"] for call in adapter.calls] == ["late but not lost"]


def test_an_unoffered_expiry_is_classified_and_files_no_notice(home):
    """(b)+(c): reason ``never_offered``, and NO new queued record anywhere."""
    target = _lane(home, "platform-stl")
    sender = _lane(home, "platform-coder")
    age = q.queue_ttl_seconds() * 2 + 60
    record = _admit(target, target="platform-stl", sender="platform-coder",
                    age_seconds=age)

    q.sweep_delivery_queue(target, on_expired=q.notice_expired_to_sender)

    settled = q.read_record(target, record["delivery_id"])
    assert settled["status"] == q.STATUS_EXPIRED
    assert settled["reason"] == q.REASON_NEVER_OFFERED
    assert settled["reason"] != q.REASON_QUEUED_EXPIRED
    assert "never offered" in settled["status_detail"]
    assert q._list(q.delivery_root(sender), q.QUEUE_DIR) == [], (
        "an unoffered expiry must not queue a fresh turn behind the same busy lane")
    assert q._list(q.delivery_root(target), q.QUEUE_DIR) == []


def test_the_failure_reaches_the_senders_live_context_when_it_has_one(home, monkeypatch):
    """(c) first branch: into the sender's NEXT TURN CONTEXT, never a queued turn."""
    from tools import bot_live_delivery as live

    target = _lane(home, "platform-stl")
    sender = _lane(home, "platform-coder")
    owner = {"profile_home": str(sender), "session_id": "s", "lease_id": "l",
             "live_session_id": "s"}
    calls = []
    monkeypatch.setattr(live, "find_canonical_live_owner", lambda _home: dict(owner))
    monkeypatch.setattr(
        live,
        "deliver_to_live_owner",
        lambda _home, _owner, message, **kwargs: calls.append((message, kwargs))
        or {"delivery_id": kwargs.get("delivery_id")},
    )
    age = q.queue_ttl_seconds() * 2 + 60
    _admit(target, target="platform-stl", sender="platform-coder", age_seconds=age)

    q.sweep_delivery_queue(target, on_expired=q.notice_expired_to_sender)

    assert len(calls) == 1, "the sender must still learn its delivery died"
    message, kwargs = calls[0]
    assert "NEVER read" in message and "never offered" in message
    assert kwargs["notification_category"] == "diagnostic"
    assert q._list(q.delivery_root(sender), q.QUEUE_DIR) == []
    assert not (q.delivery_root(sender) / "settled").exists() or (
        q._list(q.delivery_root(sender), q.SETTLED_DIR) == []
    )


def test_the_notice_loop_cannot_form(home):
    """(c) end to end: repeated sweeps of both lanes create no second-order record."""
    target = _lane(home, "platform-stl")
    sender = _lane(home, "platform-coder")
    age = q.queue_ttl_seconds() * 2 + 60
    _admit(target, target="platform-stl", sender="platform-coder", age_seconds=age)

    for _ in range(3):
        q.sweep_delivery_queue(target, on_expired=q.notice_expired_to_sender)
        q.sweep_delivery_queue(sender, on_expired=q.notice_expired_to_sender)

    assert q._list(q.delivery_root(sender), q.QUEUE_DIR) == []
    assert q._list(q.delivery_root(sender), q.SETTLED_DIR) == [], (
        "a notice about a notice is how the loop used to feed itself")
    assert len(q._list(q.delivery_root(target), q.SETTLED_DIR)) == 1


# ── the orphaned-claim recovery (t_6e43ae0a), end to end ────────────────────────
def _dead_pid() -> int:
    """A pid that has EXITED and been reaped: the shape an unclean gateway exit leaves."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    assert bot_relay._pid_alive(proc.pid) is False
    return proc.pid


def _orphaned(lane, *, target, message="orphan probe", n=1, turn_started=False,
              age_seconds=60):
    """A claim taken by a process that is now GONE and that never closed its attempt."""
    record = _admit(lane, target=target, message=message, n=n)
    assert q.claim_next(lane, target_profile=target, lease_ok=True) is not None
    path = q.delivery_root(lane) / q.CLAIMED_DIR / f"{record['delivery_id']}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    claimed_at = time.time_ns() - int(age_seconds * 1_000_000_000)
    raw["created_at"] = claimed_at
    raw["claimed_at"] = claimed_at
    for entry in raw["attempts_log"]:
        if entry.get("ended_at") is None:
            entry["started_at"] = claimed_at
            entry["pid"] = _dead_pid()
            if turn_started:
                entry["turn_started_at"] = claimed_at + 1
    path.write_text(json.dumps(raw), encoding="utf-8")
    return record


def test_the_sweep_recovers_an_orphaned_claim_and_delivers_it(home):
    """The live shape: a payload that never reached a turn is delivered, not destroyed.

    One tick: the claim is recovered in pass 1 and the drain in pass 2 delivers it. The
    reader gets the body intact, behind exactly ONE line saying why it is late.
    """
    target = _lane(home, "platform-stl")
    _lane(home, "platform-coder")
    record = _orphaned(target, target="platform-stl")
    adapter = _Adapter()

    assert asyncio.run(drain.sweep_once(adapter, home)) == 1

    assert len(adapter.calls) == 1, "the recovered payload must actually be delivered"
    body = adapter.calls[0]["user_message"]
    assert body.count("orphan probe") == 1, "no duplicate payload"
    assert body.endswith("\norphan probe")
    announcement, _sep, message = body.partition("\n")
    assert announcement.startswith("[delayed 1m: ")
    assert "orphaned before any turn started" in announcement
    assert "re-offered it once" in announcement
    assert message == "orphan probe"

    settled = q.read_record(target, record["delivery_id"])
    assert settled["status"] == q.STATUS_DELIVERED
    assert settled["attempts"] == 2, "the recovered claim, then the delivered one"
    assert q.recovery_replays(settled) == 1


def test_a_drained_turn_records_that_its_turn_started(home):
    """The A1 call site: a real turn leaves the evidence the sweep dispatches on."""
    target = _lane(home, "platform-stl")
    _lane(home, "platform-coder")
    record = _admit(target, target="platform-stl", message="the DM nobody read")

    assert asyncio.run(drain.sweep_once(_Adapter(), home)) == 1

    settled = q.read_record(target, record["delivery_id"])
    assert settled["status"] == q.STATUS_DELIVERED
    assert settled["attempts_log"][-1]["turn_started_at"] > 0
    assert settled["attempts_log"][-1]["marker"] == q.CLAIM_MARKER_VERSION


def test_the_sweep_settles_an_orphaned_started_turn_as_ambiguous_and_never_reruns_it(home):
    """A turn that STARTED may have been read: ambiguous + notice, never a second run."""
    target = _lane(home, "platform-stl")
    sender = _lane(home, "platform-coder")
    record = _orphaned(target, target="platform-stl", turn_started=True)

    assert q.sweep_delivery_queue(target) == 1

    settled = q.read_record(target, record["delivery_id"])
    assert settled["status"] == q.STATUS_AMBIGUOUS
    assert settled["reason"] == q.REASON_LEASE_LOST
    notice = q._list(q.delivery_root(sender), q.QUEUE_DIR)
    assert len(notice) == 1
    assert "MAY have been read" in notice[0]["message"]
    assert "NEVER delivered" not in notice[0]["message"]

    # the drain hands the sender its notice, and the payload to nobody
    adapter = _Adapter()
    assert asyncio.run(drain.sweep_once(adapter, home)) == 1
    assert [call["user_message"] for call in adapter.calls] == [notice[0]["message"]]

    # the ONE replay goes to the reader, wearing its delayed banner -- nothing else does
    assert q.reoffer_ambiguous(target, record["delivery_id"]) is not None
    replay = _Adapter()
    assert asyncio.run(drain.sweep_once(replay, home)) == 1
    assert len(replay.calls) == 1
    assert replay.calls[0]["user_message"].startswith("[delayed 1m: ")
    assert "orphan probe" in replay.calls[0]["user_message"]
