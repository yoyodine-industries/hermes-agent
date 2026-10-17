"""The AGENT-WORK gate: measured, attributable, never latent (t_376b3fbd).

The live failure this pins: ``ConcurrentRunLimitError`` / HTTP 429 at count 20 vs cap 16,
refusing 9/9 lanes while nobody could say which four requests made up the excess — and the
refusal was logged NOWHERE. Four properties are asserted here, each through the real
gateway handler and the real on-disk delivery queue:

  (1) a peer delivery is an ACCEPTANCE, never a 429: with the cap saturated the delivery is
      durably queued and receipted, and NO turn runs in-call;
  (2) an ordinary caller keeps today's 429 exactly — the bypass is delivery-shaped only;
  (3) the accepted record waits for a SLOT: the drainer defers while the cap is full and
      runs the turn as soon as it frees;
  (4) the gate does not latch: a handler that raises, a cancelled request and a double
      release all leave count == 0 and no holder entries, and saturation is logged once per
      episode with the count/limit/holders that make it up.

Assertions are on returned envelopes, on-disk records, adapter calls and log records —
never on source text.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from types import SimpleNamespace

import pytest

from gateway.platforms import api_server as api
from gateway.platforms import api_server_bot_delivery as drain
from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore
from tools import bot_delivery_queue as q

LOG = "gateway.platforms.api_server"


# ── the gateway seam (same shape as test_peer_delivery_acceptance.py) ──────────

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
    """``aiohttp.web`` stand-in: aiohttp is not installed in this venv."""

    @staticmethod
    def json_response(payload, status=200, headers=None):
        return _FakeResponse(payload, status, headers)


async def _prepared(request):
    return ({"session_id": "sess-1",
             "user_message": getattr(request, "user_message", "hi"),
             "gateway_session_key": None, "body": {}, "runtime_request": {},
             "lock_active": False,
             "run_kwargs": {"session_id": "sess-1", "turn_author": None}}, None)


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
    a._run_owner_pid = os.getpid()
    a._model_name = "test-model"
    a._api_key = ""
    a._pending_agent_requests = 0
    a._inflight_agent_runs = 0
    a._active_run_tasks = {}
    a._max_concurrent_runs = 1          # the cap UNDER TEST: one slot only
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
    monkeypatch.setattr(q, "receipt_after_seconds", lambda: 0.05)
    monkeypatch.setattr(q, "probe_delay", lambda n, **kw: 0.01)
    yield a
    a._run_idempotency_store.close()


def _request(key="gate-1", message="hello", delivery=True, path="/api/sessions/sess-1/chat",
             method="POST"):
    headers = {}
    if delivery:
        headers = {"Idempotency-Key": key, "X-Hermes-Sender-Profile": "sender",
                   "X-Hermes-Wait-Seconds": "30"}
    return SimpleNamespace(headers=headers, path=path, method=method, user_message=message)


def _send(adapter, **kwargs):
    response = asyncio.run(adapter._handle_session_chat(_request(**kwargs)))
    return response.status, json.loads(response.body)


def _ctx():
    return {"session_id": "sess-1", "user_message": "drain", "gateway_session_key": None,
            "body": {}, "runtime_request": {}, "lock_active": False,
            "run_kwargs": {"session_id": "sess-1"}}


def _saturate(adapter):
    """One turn in flight against a one-slot cap: the gate is full, truthfully."""
    adapter._inflight_agent_runs = 1


# ── (1) a delivery is an acceptance, never a 429 ───────────────────────────────

def test_delivery_is_accepted_and_queued_when_the_cap_is_saturated(adapter, home):
    _saturate(adapter)

    status, body = _send(adapter, message="a notice that must not be lost")

    assert status == 200, "a saturated cap must never refuse a delivery"
    assert body["result"] == q.RESULT_RECEIPT
    assert body["status"] == q.STATUS_QUEUED
    assert body["status_detail"] == q.STATUS_DETAIL_ADMISSION_CAP
    assert body["busy"] is False, "the target slot is free; the CAP is what is full"
    assert q.DETAIL_ADMISSION_CAP in body["detail"]
    assert adapter.calls == [], "no turn may be started in-call while the cap is full"
    record = q.read_record(home, body["delivery_id"])
    assert record is not None and record["status"] == q.STATUS_QUEUED, (
        "the acceptance is durable, not a promise held in memory")
    assert q.queue_depth(home, adapter._bot_send_target_profile(home)) == 1


# ── (2) the ordinary caller keeps today's 429 ─────────────────────────────────

def test_an_ordinary_caller_is_still_refused_and_the_refusal_is_logged(adapter, caplog):
    _saturate(adapter)

    with caplog.at_level(logging.WARNING, logger=LOG):
        status, body = _send(adapter, delivery=False)

    assert status == 429
    assert body["error"]["code"] == "rate_limit_exceeded"
    refusals = [r for r in caplog.records if "agent-work gate refused" in r.getMessage()]
    assert len(refusals) == 1, "a refusal that logs nothing is undiagnosable"
    line = refusals[0].getMessage()
    assert "count=1" in line and "limit=1" in line, line
    assert "inflight_turns" in line, "the term breakdown must name where the slot went"


# ── (3) the accepted record runs when a slot frees ────────────────────────────

def test_the_queued_delivery_defers_then_runs_when_the_cap_frees(adapter, home):
    _saturate(adapter)
    _, body = _send(adapter, message="run me later")
    delivery_id = body["delivery_id"]
    assert adapter.calls == []

    # Still saturated: the drainer asks the same gate the 429 does and defers.
    assert asyncio.run(drain.drain_once(adapter, home)) == 0
    assert adapter.calls == [], "the drainer may not exceed the cap it exists to respect"
    assert q.read_record(home, delivery_id)["status"] == q.STATUS_QUEUED

    # A slot frees: the very same record runs, exactly once.
    adapter._inflight_agent_runs = 0
    assert asyncio.run(drain.drain_once(adapter, home)) == 1
    assert [call.get("user_message") for call in adapter.calls] == ["run me later"]
    assert q.read_record(home, delivery_id)["status"] == q.STATUS_DELIVERED


# ── (4) the gate does not latch ───────────────────────────────────────────────

def test_a_raising_handler_releases_its_slot_and_its_holder(adapter):
    async def boom(self, request):
        raise RuntimeError("handler blew up")

    wrapped = api._admit_api_agent_request(boom)
    assert adapter.active_agent_work_count() == 0
    with pytest.raises(RuntimeError):
        asyncio.run(wrapped(adapter, _request(delivery=False)))

    assert adapter._pending_agent_requests == 0
    assert adapter.active_agent_work_count() == 0
    assert adapter._agent_gate_holders == {}, "a released slot leaves no attribution behind"


def test_a_cancelled_request_releases_its_slot(adapter):
    started = asyncio.Event()
    never = asyncio.Event()

    async def hang(self, request):
        started.set()
        await never.wait()

    wrapped = api._admit_api_agent_request(hang)

    async def scenario():
        task = asyncio.create_task(wrapped(adapter, _request(delivery=False)))
        await started.wait()
        mid = adapter._agent_gate_snapshot()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return mid

    mid = asyncio.run(scenario())
    assert mid["count"] == 1 and mid["saturated"] is True
    assert mid["holders"] and mid["holders"][0]["term"] == "pending_requests"
    assert mid["unattributed"] == 0, "every counted unit must name itself"

    after = adapter._agent_gate_snapshot()
    assert after["count"] == 0 and after["saturated"] is False
    assert after["holders"] == []


def test_a_double_release_cannot_push_the_gate_negative(adapter):
    reservation = {"active": True}
    adapter._pending_agent_requests = 1
    api._release_pending_api_work(adapter, reservation)
    api._release_pending_api_work(adapter, reservation)
    assert adapter._pending_agent_requests == 0
    assert adapter._agent_work_terms()["pending_requests"] == 0
    assert adapter.active_agent_work_count() == 0


# ── publication: the numbers ops can act on ───────────────────────────────────

def test_gate_state_reports_the_count_limit_pair_and_names_the_holders(adapter):
    _saturate(adapter)
    token = api._gate_hold(adapter, "inflight_turns", route="POST /api/sessions/{id}/chat",
                           profile="yoyodine-coder", session_id="sess-9")

    snapshot = adapter._agent_gate_snapshot()
    assert (snapshot["count"], snapshot["limit"]) == (1, 1)
    assert snapshot["saturated"] is True and snapshot["enabled"] is True
    assert snapshot["terms"]["inflight_turns"] == 1
    assert snapshot["unattributed"] == 0
    held = {h["term"]: h for h in snapshot["holders"]}
    assert held["inflight_turns"]["profile"] == "yoyodine-coder"
    assert held["inflight_turns"]["session_id"] == "sess-9"
    assert held["inflight_turns"]["route"] == "POST /api/sessions/{id}/chat"

    api._gate_release(adapter, token)
    assert adapter._agent_gate_snapshot()["holders"] == []


def test_the_gate_state_route_is_registered_and_read_only(adapter):
    routes = {(method, path) for method, path, _ in adapter._http_route_table()}
    assert ("GET", "/api/gate/state") in routes

    _saturate(adapter)
    response = asyncio.run(adapter._handle_gate_state(_request(delivery=False)))
    payload = json.loads(response.body)
    assert payload["object"] == "hermes.gate.state"
    assert (payload["count"], payload["limit"], payload["saturated"]) == (1, 1, True)
    # Read-only: the same call again reports the same thing (nothing was consumed).
    again = json.loads(asyncio.run(adapter._handle_gate_state(_request(delivery=False))).body)
    assert (again["count"], again["limit"]) == (1, 1)


def test_the_gate_block_is_published_with_the_heartbeat(adapter, monkeypatch):
    _saturate(adapter)
    published = {}

    def _write(name, **fields):
        published.update(fields)

    monkeypatch.setattr(type(adapter), "is_connected", property(lambda self: True))
    adapter._write_runtime_status_safe = _write
    adapter._reset_metrics_if_needed = lambda: None
    adapter._metrics_last_heartbeat_at = None
    adapter._metrics_day = "2026-10-17"
    adapter._metrics_requests_today = 0
    adapter._metrics_messages_today = 0
    adapter._metrics_tokens_today = 0
    adapter._metrics_latency_ms = []
    adapter._metrics_last_request_at = None
    adapter._host, adapter._port = "127.0.0.1", 8644
    adapter._active_structured_run_count = lambda: 0
    adapter._latency_p95_ms = lambda samples: None

    adapter._publish_runtime_status()

    gate = published["platform_metrics"]["agent_work_gate"]
    assert (gate["count"], gate["limit"]) == (1, 1)


def test_saturation_warns_once_per_episode_and_recovery_is_logged(adapter, caplog):
    _saturate(adapter)
    with caplog.at_level(logging.INFO, logger=LOG):
        adapter._note_gate_saturation(adapter._agent_gate_snapshot())
        adapter._note_gate_saturation(adapter._agent_gate_snapshot())
        adapter._inflight_agent_runs = 0
        adapter._note_gate_saturation(adapter._agent_gate_snapshot())

    messages = [r.getMessage() for r in caplog.records]
    saturated = [m for m in messages if "gate SATURATED" in m]
    recovered = [m for m in messages if "gate recovered" in m]
    assert len(saturated) == 1, "one WARNING per episode, not one per refusal"
    assert len(recovered) == 1
    assert "count=1" in saturated[0] and "limit=1" in saturated[0]
