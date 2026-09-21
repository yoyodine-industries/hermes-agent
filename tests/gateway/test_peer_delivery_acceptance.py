"""ACCEPTANCE — ONE scripted peer delivery walks all three card criteria in order.

The three criteria (peer-send-result-spec §1.5 ENUM A, §2.x queue/backoff, §7):

  1. B-T1  a send to a BUSY target is ACCEPTED, never failed: ``result == "receipt"``,
     ``status == "queued"``, ``busy is True``, ``retryable is False``, ``attempts == 0``,
     and the client maps ``receipt`` to exit code 0 (a receipt is success).
  2. the sender must NOT enter a retry loop on ``target_busy``: the outcome is not
     retryable, and a second identical send (a sender that retries) starts no extra turn
     and creates no second delivery — ``adapter.calls`` is unchanged.
  3. B-T2  same-key retries deliver EXACTLY ONCE: three identical sends under one
     idempotency key (one of them a retry while the target is busy) run exactly ONE turn,
     leave exactly ONE delivery record, share ONE ``delivery_id``, and every response
     after the first is a replay.

Everything is driven through the real gateway handler (``_handle_session_chat`` +
``_bot_send_probe_and_run`` / ``_bot_send_turn``), the real ``hermes peer dm`` client
command, the real idempotency store and the real on-disk delivery queue under a tmp
HERMES_HOME. Assertions are made on returned envelopes, files under the delivery root
and ``adapter.calls`` — never on source text.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from types import SimpleNamespace

import pytest

from gateway.platforms import api_server as api
from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore
from hermes_cli.subcommands import peer as peer_cmd
from tools import bot_delivery_queue as q


# ── the gateway seam (same fixture/helper style as test_peer_delivery_gateway.py) ──

@pytest.fixture()
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(path))
    return path


class _FakeResponse:
    def __init__(self, payload, status=200, headers=None):
        self.status = status
        self.body = json.dumps(payload).encode()
        self.headers = headers or {}


class _FakeWeb:
    """Minimal ``aiohttp.web`` stand-in: aiohttp is not installed in this venv, and
    every assertion here is on the real JSON payload and status code."""

    @staticmethod
    def json_response(payload, status=200, headers=None):
        return _FakeResponse(payload, status, headers)


@pytest.fixture()
def adapter(home, monkeypatch):
    monkeypatch.setattr(api, "web", _FakeWeb)
    a = api.APIServerAdapter.__new__(api.APIServerAdapter)
    a._run_idempotency_store = RunIdempotencyStore(":memory:")
    a._run_statuses = {}
    a._run_idempotency_ids = set()
    a._run_owners = {}
    a._run_owner_pid = os.getpid()
    a._run_owner_started = 0
    a._model_name = "test-model"
    a._api_key = ""            # no key configured -> the auth check passes through
    a._pending_agent_requests = 0
    a._max_concurrent_runs = 0   # cap disabled: /api/sessions/chat now checks it (#7483)
    a._prepare_session_chat = _prepared
    a._run_idempotency_scope = lambda request: "peer-scope"
    a.calls = []

    async def _history(session_id):
        return []

    async def _run_agent(conversation_history=None, **kwargs):
        a.calls.append(kwargs)
        return {"final_response": "pong"}, {}

    a._conversation_history_for_session = _history
    a._run_agent = _run_agent
    # Keep the probe loop honest but fast: the real durations are asserted elsewhere.
    monkeypatch.setattr(q, "receipt_after_seconds", lambda: 0.05)
    monkeypatch.setattr(q, "probe_delay", lambda n, **kw: 0.01)
    yield a
    a._run_idempotency_store.close()


async def _prepared(request):
    return ({"session_id": "sess-1",
             "user_message": getattr(request, "user_message", "hi"),
             "gateway_session_key": None, "body": {}, "runtime_request": {},
             "lock_active": False, "run_kwargs": {"session_id": "sess-1", "turn_author": None}}, None)


def _headers(key, sender="sender", wait="30"):
    headers = {}
    if key is not None:
        headers["Idempotency-Key"] = key
    if sender is not None:
        headers["X-Hermes-Sender-Profile"] = sender
    if wait is not None:
        headers["X-Hermes-Wait-Seconds"] = wait
    return headers


def _request(**kwargs):
    message = kwargs.pop("message", "hi")
    return SimpleNamespace(
        headers=_headers(**kwargs), path="/api/sessions/sess-1/chat",
        user_message=message)


def _send(adapter, **kwargs):
    """One real HTTP-shaped peer send through the gateway handler."""
    body = asyncio.run(adapter._handle_session_chat(_request(**kwargs))).body
    return json.loads(body)


def _hold_turn_lock(home, profile="default"):
    """Hold the target profile's turn lock, which is what makes a target BUSY."""
    from tools.bot_mode_probe import _hermes_root
    from tools.bot_relay import acquire_turn_lock

    return acquire_turn_lock(_hermes_root(home), profile, timeout_seconds=0)


def _ctx(session_id="sess-1"):
    """The prepared-request context a receiver-side drainer hands to the turn runner."""
    return {"session_id": session_id, "user_message": "drain", "gateway_session_key": None,
            "body": {}, "runtime_request": {}, "lock_active": False,
            "run_kwargs": {"session_id": session_id}}


# ── the delivery root as a real artifact (exactly one delivery record?) ──────────

def _delivery_records(home):
    """Every on-disk delivery record, as (lifecycle-dir, filename) pairs."""
    root = q.delivery_root(home)
    return sorted(
        (sub, path.name)
        for sub in (q.QUEUE_DIR, q.CLAIMED_DIR, q.SETTLED_DIR)
        for path in (root / sub).glob("*.json")
    )


# ── the client seam (same fixture style as test_peer_receipt_client.py) ──────────

def _client_args(**kw):
    base = {"peer_action": "dm", "target": "spark", "message": "ping", "json": True,
            "idempotency_key": None, "wait_seconds": None}
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture()
def peer_client(monkeypatch):
    """Isolate the peer registry + Bot Chat session, capturing every request."""
    calls: list[dict] = []

    monkeypatch.setattr(peer_cmd, "_load_peers",
                        lambda: {"spark": {"url": "http://spark.lan:8377"}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "k" * 20)
    monkeypatch.setattr(peer_cmd, "_ensure_bot_chat", lambda base, key: "bc_1")
    monkeypatch.setattr(peer_cmd, "_sender_profile", lambda: "lane-alpha")

    def fake_request(url, api_key, **kw):
        calls.append({"url": url, "api_key": api_key, **kw})
        return peer_client.response

    peer_client.response = {}
    monkeypatch.setattr(peer_cmd, "_request", fake_request)
    peer_client.calls = calls
    return peer_client


# ── the scripted acceptance walk ────────────────────────────────────────────────

MESSAGE = "acceptance walk: receipt on busy, no retry loop, exactly once"
KEY = q.validate_idempotency_key("auto:acceptance:walk:1")


def test_busy_receipt_no_retry_loop_and_same_key_exactly_once(
        adapter, home, peer_client, capsys):
    """One delivery, three criteria, walked end to end in sequence."""
    profile = adapter._bot_send_target_profile(home)

    # ── (1) BUSY target -> accepted receipt, never a failure ────────────────────
    with _hold_turn_lock(home):
        first = _send(adapter, key=KEY, message=MESSAGE)

    assert first["object"] == q.OBJECT_NAME
    assert first["result"] == q.RESULT_RECEIPT
    assert first["status"] == q.STATUS_QUEUED
    assert first["busy"] is True
    assert first["retryable"] is False
    assert first["attempts"] == 0
    assert first["replayed"] is False
    assert first["reply"] is None
    assert first["queue_position"] == 1
    assert first["status_detail"] == q.STATUS_DETAIL_TARGET_BUSY
    assert first["detail"] == q.DETAIL_RECEIPT
    assert adapter.calls == [], "the held slot must not run the payload in-call"

    delivery_id = first["delivery_id"]
    assert q.read_record(home, delivery_id)["status"] == q.STATUS_QUEUED
    assert _delivery_records(home) == [(q.QUEUE_DIR, f"{delivery_id}.json")]

    # ...and the client-side mapping of `receipt` is SUCCESS: exit code 0 (§1.5).
    # Feed the REAL gateway envelope through the REAL `hermes peer dm` command.
    peer_client.response = dict(first)
    assert peer_cmd.cmd_peer(_client_args(message=MESSAGE)) == 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["result"] == "receipt"
    assert emitted["status"] == "queued"
    assert emitted["delivery_id"] == delivery_id
    assert emitted["attempts"] == 0

    # ── (2) no retry loop: a sender that retries starts no extra turn ───────────
    calls_before = len(adapter.calls)
    with _hold_turn_lock(home):
        second = _send(adapter, key=KEY, message=MESSAGE)

    assert second["retryable"] is False, "target_busy is never handed back as retryable"
    assert second["delivery_id"] == delivery_id
    assert second["replayed"] is True
    assert second["status"] == q.STATUS_QUEUED
    assert second["attempts"] == 0
    assert len(adapter.calls) == calls_before, "a retry must not start a turn"
    assert adapter.calls == []
    assert _delivery_records(home) == [(q.QUEUE_DIR, f"{delivery_id}.json")]
    assert q.queue_depth(home, profile) == 1

    # ── (3) same-key retries deliver EXACTLY ONCE ──────────────────────────────
    # 3rd identical send, same key: the slot is free now, and dedup is key-bound,
    # not lock-bound — it still may not run the payload a second time.
    third = _send(adapter, key=KEY, message=MESSAGE)
    assert third["delivery_id"] == delivery_id
    assert third["replayed"] is True
    assert third["result"] == q.RESULT_RECEIPT
    assert third["status"] == q.STATUS_QUEUED
    assert adapter.calls == [], "no send may ever re-run an accepted delivery"

    # The durable queue is the only thing allowed to run it: the receiver drains it
    # exactly once, through the same turn runner the in-call curve uses.
    with _hold_turn_lock(home):
        drained = json.loads(asyncio.run(adapter._bot_send_turn(
            _request(key=KEY, message=MESSAGE), _ctx(), home=home,
            delivery_id=delivery_id, target_profile=profile,
            session_id="sess-1", waited=0.0)).body)
    assert drained["result"] == q.RESULT_DELIVERED
    assert drained["status"] == q.STATUS_DELIVERED
    assert drained["attempts"] == 1
    assert drained["reply"] == "pong"
    assert drained["delivery_id"] == delivery_id

    # exactly ONE turn ran, for exactly ONE delivery record...
    assert len(adapter.calls) == 1
    assert _delivery_records(home) == [(q.SETTLED_DIR, f"{delivery_id}.json")]
    record = q.read_record(home, delivery_id)
    assert record["status"] == q.STATUS_DELIVERED and record["attempts"] == 1

    # ...every response carried the SAME delivery_id, and every response after the
    # first was a replay (never a re-run).
    assert {r["delivery_id"] for r in (first, second, third)} == {delivery_id}
    assert [r["replayed"] for r in (first, second, third)] == [False, True, True]

    # Extra guard: a retry after delivery replays the settled outcome, still one turn.
    fourth = _send(adapter, key=KEY, message=MESSAGE)
    assert fourth["delivery_id"] == delivery_id
    assert fourth["replayed"] is True
    assert fourth["result"] == q.RESULT_DELIVERED
    assert len(adapter.calls) == 1
    assert _delivery_records(home) == [(q.SETTLED_DIR, f"{delivery_id}.json")]


# ── D1: the queue is DRAINED, not merely written ────────────────────────────────

def test_drained_head_runs_even_when_its_own_request_is_gone(adapter, home):
    """D1: an accepted record must RUN -- including one whose sender has left.

    Live evidence: 53 records sat in ``queued`` with a full body each and no runner,
    because the in-call curve only ever ran the delivery its own request admitted.
    Here the OLDER delivery is accepted first and its request then abandons it (a
    restart or a timed-out sender does exactly that). When a LATER request takes the
    slot, the queue head must run -- not be handed back as one more receipt.
    """
    profile = adapter._bot_send_target_profile(home)
    older_key = q.validate_idempotency_key("auto:d1:older")
    our_key = q.validate_idempotency_key("auto:d1:ours")

    with _hold_turn_lock(home):
        older = _send(adapter, key=older_key, message="older payload")
        ours = _send(adapter, key=our_key, message="our payload")
    assert (older["status"], ours["status"]) == (q.STATUS_QUEUED, q.STATUS_QUEUED)
    assert (older["queue_position"], ours["queue_position"]) == (1, 2)
    assert adapter.calls == [], "a held slot runs nothing in-call"

    # The slot frees and OUR request admits: it runs the head first, then itself.
    with _hold_turn_lock(home):
        last = json.loads(asyncio.run(adapter._bot_send_turn(
            _request(key=our_key, message="our payload"), _ctx(), home=home,
            delivery_id=ours["delivery_id"], target_profile=profile,
            session_id="sess-1", waited=0.0)).body)

    assert [call.get("user_message") for call in adapter.calls] == ["older payload", None], (
        "the drained head must RUN, and run FIRST")
    assert last["delivery_id"] == ours["delivery_id"]
    assert last["status"] == q.STATUS_DELIVERED
    assert q.read_record(home, older["delivery_id"])["status"] == q.STATUS_DELIVERED, (
        "the foreign head is delivered, never requeued into a queue nothing drains")
    assert q.read_record(home, ours["delivery_id"])["status"] == q.STATUS_DELIVERED
    assert _delivery_records(home) == sorted(
        (q.SETTLED_DIR, f"{d['delivery_id']}.json") for d in (older, ours))
    assert q.queue_depth(home, profile) == 0


def test_sweep_drains_a_free_slot_and_never_expires_a_held_one(adapter, home):
    """D1 triggers 3-4 + the TTL rule: no live request needed, no holder fought.

    The live slot was held by a UI/desktop session, not a card worker (a 1800.3s
    lease wait reproduced against one), so the sweep skips a held slot, leaves the
    record queued -- and the over-age sweep must NOT expire it merely because its
    target is mid-turn: the holder is the turn that drains it next.
    """
    from gateway.platforms import api_server_bot_delivery as drain

    profile = adapter._bot_send_target_profile(home)
    with _hold_turn_lock(home):
        first = _send(adapter, key=q.validate_idempotency_key("auto:d1:s1"), message="one")
        second = _send(adapter, key=q.validate_idempotency_key("auto:d1:s2"), message="two")
    assert q.queue_depth(home, profile) == 2

    # (a) an over-age record whose slot is HELD is kept, not expired.
    future = time.time_ns() + 10 * 24 * 3600 * 1_000_000_000
    assert q.sweep_delivery_queue(home, now_ns=future,
                                  slot_held_fn=lambda h, p: True) == 0
    assert q.read_record(home, first["delivery_id"])["status"] == q.STATUS_QUEUED

    # (b) the drainer itself skips a held slot; it never fights for it.
    with _hold_turn_lock(home):
        assert asyncio.run(drain.drain_once(adapter, home)) == 0
    assert adapter.calls == []
    assert q.queue_depth(home, profile) == 2

    # (c) the slot frees -> the whole backlog runs, in FIFO order.
    assert asyncio.run(drain.drain_once(adapter, home)) == 2
    assert [call["user_message"] for call in adapter.calls] == ["one", "two"]
    assert q.queue_depth(home, profile) == 0
    assert [q.read_record(home, r["delivery_id"])["status"]
            for r in (first, second)] == [q.STATUS_DELIVERED, q.STATUS_DELIVERED]


def test_sweep_drains_a_named_profile_home_under_the_root(adapter, home):
    """The drainer must drain EVERY lane's queue, not just the default home's.

    Live evidence: the multiplexing gateway admits a peer delivery into the
    TARGET lane's own home (``_bot_send_home`` resolves the request-scoped
    profile to ``profiles/<lane>``), but the 30s sweep drained only
    ``_default_home()`` -- so 13 records sat in two lanes' per-profile queues
    while the root queue stayed empty and the sweep logged ``actions=0``
    forever. The drainer runs against the root and must enumerate the roster.
    """
    from gateway.platforms import api_server_bot_delivery as drain

    profile = "lane-beta"
    lane_home = home / "profiles" / profile  # root/profiles/lane-beta

    record = q.admit(
        lane_home,
        sender_profile="lane-alpha",
        target_profile=profile,
        target_session_id="sess-1",
        idempotency_key=q.validate_idempotency_key("auto:sweep:named"),
        fingerprint="fp",
        delivery_id="d" * 32,
        message="hello lane-beta",
    )
    assert record["status"] == q.STATUS_QUEUED
    assert q.queued_target_profiles(home) == [], "the root/default home is empty"
    assert q.queued_target_profiles(lane_home) == [profile]

    # One sweep tick against the ROOT home must drain the named lane's queue.
    assert asyncio.run(drain.drain_once(adapter, home)) == 1
    assert [call["user_message"] for call in adapter.calls] == ["hello lane-beta"]
    assert q.read_record(lane_home, record["delivery_id"])["status"] == q.STATUS_DELIVERED
    assert q.queue_depth(lane_home, profile) == 0


def test_drain_resessions_a_record_pinned_to_a_dead_session(adapter, home, monkeypatch):
    """DoD #2: a drained record pinned to a dead session runs in the lane's current
    Bot Chat tip instead of dead-lettering forever."""
    from gateway.platforms import api_server_bot_delivery as drain

    profile = "lane-beta"
    lane_home = home / "profiles" / profile
    record = q.admit(
        lane_home,
        sender_profile="lane-alpha",
        target_profile=profile,
        target_session_id="api_dead_tip",
        idempotency_key=q.validate_idempotency_key("auto:resession"),
        fingerprint="fp",
        delivery_id="e" * 32,
        message="hello again",
    )
    assert record["status"] == q.STATUS_QUEUED
    monkeypatch.setattr(drain, "_canonical_bot_chat_tip", lambda home: "api_current_tip")

    assert asyncio.run(drain.drain_once(adapter, home)) == 1
    assert [call["session_id"] for call in adapter.calls] == ["api_current_tip"]
    assert q.read_record(lane_home, record["delivery_id"])["status"] == q.STATUS_DELIVERED


def test_drain_keeps_a_record_pinned_to_the_current_tip(adapter, home, monkeypatch):
    """A record already pinned to the current tip is not re-sessioned."""
    from gateway.platforms import api_server_bot_delivery as drain

    profile = "lane-beta"
    lane_home = home / "profiles" / profile
    record = q.admit(
        lane_home,
        sender_profile="lane-alpha",
        target_profile=profile,
        target_session_id="api_current_tip",
        idempotency_key=q.validate_idempotency_key("auto:resession-current"),
        fingerprint="fp",
        delivery_id="f" * 32,
        message="hello",
    )
    monkeypatch.setattr(drain, "_canonical_bot_chat_tip", lambda home: "api_current_tip")

    assert asyncio.run(drain.drain_once(adapter, home)) == 1
    assert [call["session_id"] for call in adapter.calls] == ["api_current_tip"]
    assert q.read_record(lane_home, record["delivery_id"])["status"] == q.STATUS_DELIVERED


def test_drain_keeps_pinned_session_when_no_tip_resolvable(adapter, home, monkeypatch):
    """No provable tip: the drainer degrades to the pinned session, never drops."""
    from gateway.platforms import api_server_bot_delivery as drain

    profile = "lane-beta"
    lane_home = home / "profiles" / profile
    record = q.admit(
        lane_home,
        sender_profile="lane-alpha",
        target_profile=profile,
        target_session_id="api_dead_tip",
        idempotency_key=q.validate_idempotency_key("auto:resession-fallback"),
        fingerprint="fp",
        delivery_id="a" * 32,
        message="hello",
    )
    monkeypatch.setattr(drain, "_canonical_bot_chat_tip", lambda home: "")

    assert asyncio.run(drain.drain_once(adapter, home)) == 1
    assert [call["session_id"] for call in adapter.calls] == ["api_dead_tip"]
    assert q.read_record(lane_home, record["delivery_id"])["status"] == q.STATUS_DELIVERED


def test_queued_receipt_reports_the_slot_state_observed(adapter, home):
    """D2: a receipt says WHY it queued, from the slot's real state.

    The old receipt hard-coded "target busy", so a 53-deep backlog on a free slot
    read as one busy turn. With the slot free the honest cause is the backlog, and
    ``busy`` follows the same field the envelope prints (D7).
    """
    with _hold_turn_lock(home):
        mine = _send(adapter, key=q.validate_idempotency_key("auto:d2"), message="hi")
    assert mine["status"] == q.STATUS_QUEUED

    receipt = adapter._bot_send_receipt(
        mine["delivery_id"], q.read_record(home, mine["delivery_id"]),
        waited=0.0, home=home)
    body = json.loads(receipt.body)
    assert body["status_detail"] == q.STATUS_DETAIL_BACKLOG
    assert body["busy"] is False
    stored = q.read_record(home, mine["delivery_id"])
    assert stored["status_detail"] == q.STATUS_DETAIL_BACKLOG
    assert q.busy_from_detail(stored) is False


# ── D1 sub-item 2: the drain invariant (N keys, free slot -> N turns, once) ────

def test_drain_invariant_n_keys_free_lock_n_turns_exactly_once(adapter, home):
    """D1 sub-item 2: with a free slot, N admitted keys run N turns exactly once.

    Sub-item 2 of the verification was PARTIAL because "delivered exactly once"
    could not be exercised while zero turns executed for receipted deliveries.
    Now that a receipt is a promise of execution, the invariant is testable
    end-to-end: six distinct keys admitted while the slot is held all run --
    each exactly once, in FIFO order -- and a same-key replay afterwards starts
    no second turn.
    """
    from gateway.platforms import api_server_bot_delivery as drain

    profile = adapter._bot_send_target_profile(home)
    count = 6
    keys = [q.validate_idempotency_key(f"auto:inv:{i}") for i in range(count)]
    messages = [f"invariant payload {i}" for i in range(count)]

    # The slot is held, so every send is receipted rather than run in-call.
    with _hold_turn_lock(home):
        admitted = [_send(adapter, key=k, message=m)
                    for k, m in zip(keys, messages)]
    assert [r["result"] for r in admitted] == [q.RESULT_RECEIPT] * count
    assert len({r["delivery_id"] for r in admitted}) == count
    assert adapter.calls == [], "a held slot runs nothing in-call"
    assert q.queue_depth(home, profile) == count

    # The slot frees with no live request: the drainer runs the whole backlog.
    assert asyncio.run(drain.drain_once(adapter, home)) == count
    ran = [call.get("user_message") for call in adapter.calls]
    assert ran == messages, "FIFO, one turn per admitted key"
    assert len(ran) == len(set(ran)) == count, "each key ran exactly once"
    assert q.queue_depth(home, profile) == 0
    for record in admitted:
        assert q.read_record(home, record["delivery_id"])["status"] == q.STATUS_DELIVERED
    assert _delivery_records(home) == sorted(
        (q.SETTLED_DIR, f"{r['delivery_id']}.json") for r in admitted)

    # A same-key replay after settlement must not start a second turn.
    before = len(adapter.calls)
    replay = _send(adapter, key=keys[0], message=messages[0])
    assert replay["delivery_id"] == admitted[0]["delivery_id"]
    assert replay["replayed"] is True
    assert len(adapter.calls) == before, "a replay never starts a second turn"
    assert q.queue_depth(home, profile) == 0


# ── fairness of the tick's budget (ops t_59de8ea2 / t_adecd536) ─────────────────
#
# drain_once spends ONE time budget (SWEEP_DRAIN_BUDGET_SECONDS, 60 s) and a drained
# record runs a full agent turn inline (measured 359-566 s), so the first lane with
# traffic spent the whole tick. The roster order is stable, so every lane behind it was
# skipped -- no claim attempt, no log line -- on every tick, forever.

class _TurnClock:
    """A monotonic clock a fake turn charges, so a tick's budget is exact.

    ``drain_once`` measures its budget with ``time.monotonic``; charging the clock a
    fixed cost per turn makes "this turn ran past the deadline" a statement instead of
    a wall-clock race, and keeps these tests off real sleeps.
    """

    def __init__(self, cost: float = 1.0) -> None:
        self.now = 0.0
        self.cost = cost

    def monotonic(self) -> float:
        return self.now

    def charge_turn(self) -> None:
        self.now += self.cost


@pytest.fixture()
def turn_clock(adapter, monkeypatch):
    """Give the drainer a clock whose advance is exactly one turn's cost."""
    from gateway.platforms import api_server_bot_delivery as drain

    clock = _TurnClock()
    inner = adapter._run_agent

    async def _run_agent(*args, **kwargs):
        clock.charge_turn()
        return await inner(*args, **kwargs)

    monkeypatch.setattr(drain, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(adapter, "_run_agent", _run_agent)
    return clock


@pytest.fixture(autouse=True)
def _fresh_roster_rotation():
    """Every test walks the roster from its head: the rotation is module state.

    Without this a test that asserts an order would inherit the offset another test
    left behind, so the assertion would depend on file order.
    """
    from gateway.platforms import api_server_bot_delivery as drain

    drain._roster_offset = 0
    yield
    drain._roster_offset = 0


def _admit(profile_home, index, message, *, target_profile, sender_profile="lane-alpha"):
    """One queued record in ``profile_home``'s own queue (no HTTP round trip)."""
    record = q.admit(
        profile_home,
        sender_profile=sender_profile,
        target_profile=target_profile,
        target_session_id="sess-1",
        idempotency_key=q.validate_idempotency_key(f"auto:fair:{index}"),
        fingerprint="fp",
        delivery_id=f"{index:032x}",
        message=message,
    )
    assert record is not None, "an admitted record is on disk"
    return record


def test_rotate_roster_is_pure_and_wraps():
    """The rotation helper is the unit that makes the walk start where it must."""
    from gateway.platforms import api_server_bot_delivery as drain

    roster = ["a", "b", "c"]
    assert drain._rotate_roster(roster, 0) == ["a", "b", "c"]
    assert drain._rotate_roster(roster, 1) == ["b", "c", "a"]
    assert drain._rotate_roster(roster, 2) == ["c", "a", "b"]
    assert drain._rotate_roster(roster, 4) == ["b", "c", "a"], "wraps past the end"
    assert drain._rotate_roster([], 3) == []
    assert roster == ["a", "b", "c"], "the roster handed in is never re-ordered"


def test_drain_rotates_the_roster_so_a_budget_skipped_lane_leads_the_next_pass(
        adapter, home, turn_clock):
    """A lane the spent budget cut off at the tail leads the pass after it.

    The head lane still holds a queued record here, so the second pass has to choose
    between the lane that has just run and the lane the budget starved: without the
    rotation it picks the head lane again, which is the starvation that left
    platform-worker and the research lanes unreached (ops t_59de8ea2 / t_adecd536).
    """
    from gateway.platforms import api_server_bot_delivery as drain

    profile = adapter._bot_send_target_profile(home)
    lane = "lane-beta"
    lane_home = home / "profiles" / lane
    _admit(home, 1, "head one", target_profile=profile)
    _admit(home, 2, "head two", target_profile=profile)
    tail = _admit(lane_home, 3, "tail one", target_profile=lane)
    turn_clock.cost = 1.0  # one turn costs more than a whole tick

    # Pass 1: the head lane's turn spends the budget, so the tail lane is skipped.
    assert asyncio.run(drain.drain_once(adapter, home, budget_seconds=0.5)) == 1
    assert [call["user_message"] for call in adapter.calls] == ["head one"]
    assert q.read_record(lane_home, tail["delivery_id"])["status"] == q.STATUS_QUEUED

    # Pass 2 starts one position further along, so the skipped lane leads it.
    assert asyncio.run(drain.drain_once(adapter, home, budget_seconds=0.5)) == 1
    assert [call["user_message"] for call in adapter.calls] == ["head one", "tail one"], (
        "the lane the budget skipped must lead the next pass, not wait behind the "
        "lane that already ran")
    assert q.queue_depth(home, profile) == 1, (
        "the head lane keeps its own backlog for a later pass")
    assert q.read_record(lane_home, tail["delivery_id"])["status"] == q.STATUS_DELIVERED


def test_drain_takes_at_most_one_record_per_lane_per_pass(adapter, home, turn_clock):
    """One record per lane per pass: a backlog cannot be spent in front of the rest.

    Two lanes with two records each and a budget with room for all four turns. The walk
    gives each lane one turn before it returns to the first, so the order interleaves
    lanes -- first-come drained lane by lane, which is how a deep backlog consumed the
    whole tick.
    """
    from gateway.platforms import api_server_bot_delivery as drain

    profile = adapter._bot_send_target_profile(home)
    lane = "lane-beta"
    lane_home = home / "profiles" / lane
    _admit(home, 1, "alpha one", target_profile=profile)
    _admit(home, 2, "alpha two", target_profile=profile)
    _admit(lane_home, 3, "beta one", target_profile=lane)
    _admit(lane_home, 4, "beta two", target_profile=lane)
    turn_clock.cost = 1.0

    assert asyncio.run(drain.drain_once(adapter, home, budget_seconds=100.0)) == 4
    assert [call["user_message"] for call in adapter.calls] == [
        "alpha one", "beta one", "beta two", "alpha two"], (
        "each lane takes one record per pass: neither lane may take a second turn "
        "before the other lane's first")
    assert q.queue_depth(home, profile) == 0
    assert q.queue_depth(lane_home, lane) == 0


def test_drain_names_every_lane_the_spent_budget_could_not_reach(
        adapter, home, turn_clock, caplog):
    """Each unreached lane is logged with its queued depth -- the skip was silent.

    A silent skip is why the starved cohort read as an idle sweep: the tick logged
    ``actions=0`` while two lanes held a backlog behind the first lane.
    """
    from gateway.platforms import api_server_bot_delivery as drain

    profile = adapter._bot_send_target_profile(home)
    _admit(home, 1, "head one", target_profile=profile)
    _admit(home, 2, "head two", target_profile=profile)
    for index, lane in enumerate(("lane-beta", "lane-gamma"), start=3):
        _admit(home / "profiles" / lane, index, f"{lane} payload", target_profile=lane)
    turn_clock.cost = 1.0

    caplog.set_level(logging.INFO, logger="tools.bot_delivery_queue")
    assert asyncio.run(drain.drain_once(adapter, home, budget_seconds=0.5)) == 1

    lines = [record.getMessage() for record in caplog.records
             if "drain_budget_exhausted" in record.getMessage()]
    assert len(lines) == 2, "one line per lane the budget could not reach"
    named = sorted(line.split("target=")[1].split()[0] for line in lines)
    assert named == ["lane-beta", "lane-gamma"]
    assert all("queued=1" in line for line in lines), (
        "each line carries why it was skipped: the lane's queued depth")


def test_drain_keeps_the_head_claim_and_the_two_skip_logs(
        adapter, home, turn_clock, caplog):
    """No regression: head-first claim, ``drain_deferred``, ``drain_nothing_claimable``."""
    from gateway.platforms import api_server_bot_delivery as drain

    profile = adapter._bot_send_target_profile(home)
    first = _admit(home, 1, "first payload", target_profile=profile)
    _admit(home, 2, "second payload", target_profile=profile)
    turn_clock.cost = 1.0

    # (a) the OLDEST record is claimed first, and a free slot drains the backlog.
    assert asyncio.run(drain.drain_once(adapter, home)) == 2
    assert [call["user_message"] for call in adapter.calls] == [
        "first payload", "second payload"]
    assert q.read_record(home, first["delivery_id"])["status"] == q.STATUS_DELIVERED

    # (b) a HELD slot is deferred, never fought for: the record stays queued.
    waiting = _admit(home, 3, "held payload", target_profile=profile)
    caplog.set_level(logging.INFO, logger="tools.bot_delivery_queue")
    with _hold_turn_lock(home):
        assert asyncio.run(drain.drain_once(adapter, home)) == 0
    assert "drain_deferred" in caplog.text and "reason=slot_held" in caplog.text
    assert q.read_record(home, waiting["delivery_id"])["status"] == q.STATUS_QUEUED

    # (c) a lane that still lists a queued record whose claim comes back empty is
    #     named, not passed over in silence (a record claimed elsewhere but left in
    #     the queue dir is exactly that state).
    caplog.clear()
    queued_file = q.delivery_root(home) / q.QUEUE_DIR / f"{waiting['delivery_id']}.json"
    payload = json.loads(queued_file.read_text())
    payload["status"] = q.STATUS_RUNNING
    queued_file.write_text(json.dumps(payload))
    assert q.queued_target_profiles(home) == [profile]
    assert asyncio.run(drain.drain_once(adapter, home)) == 0
    assert "drain_nothing_claimable" in caplog.text
    assert "reason=claim_empty_while_queued" in caplog.text
    assert "queued=1" in caplog.text


def test_the_drained_lane_holds_its_turn_lock_across_claim_and_turn(
        adapter, home, monkeypatch):
    """The claim, the None check and the WHOLE turn run inside the lane's lock.

    The flock IS the lane's busy signal (what makes a concurrent in-call delivery see
    ``slot_held`` and take a receipt instead of starting a turn). A drained turn runs
    for minutes (measured 359-566 s), so a drain that released the lock after claiming
    -- or after the turn -- admits a second turn alongside a live one for the same
    profile. Probed with the real lock from inside both seams.
    """
    from gateway.platforms import api_server_bot_delivery as drain
    from tools.bot_mode_probe import _hermes_root
    from tools.bot_relay import TurnBusyError, acquire_turn_lock

    profile = adapter._bot_send_target_profile(home)
    root = _hermes_root(home)
    _admit(home, 1, "locked payload", target_profile=profile)

    seen: list[str] = []

    def _probe(where: str) -> None:
        try:
            with acquire_turn_lock(root, profile, timeout_seconds=0):
                seen.append(f"{where}:free")
        except TurnBusyError:
            seen.append(f"{where}:busy")

    real_claim = q.claim_next
    real_run = adapter._run_agent

    def _claim(*args, **kwargs):
        _probe("claim")
        return real_claim(*args, **kwargs)

    async def _run_agent(*args, **kwargs):
        _probe("turn")
        return await real_run(*args, **kwargs)

    monkeypatch.setattr(q, "claim_next", _claim)
    monkeypatch.setattr(adapter, "_run_agent", _run_agent)

    assert asyncio.run(drain.drain_once(adapter, home)) == 1
    assert seen == ["claim:busy", "turn:busy"], (
        "the lane's turn lock must be held across the claim AND the whole turn: "
        "releasing it early lets a concurrent delivery start a second turn beside a "
        "live one")

    # And the lock is free again once the pass is over, so the next caller is admitted.
    with acquire_turn_lock(root, profile, timeout_seconds=0):
        pass
