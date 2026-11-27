"""Gate DIAGNOSTICS: a gauge, a named route, a named concentration (t_4ee24aa2 → t_730885d3).

The ruling that closes the load-term question leaves three observability gaps, and each is
asserted here through the real handler / real function:

  (1) a refusal names the surface that made it — every `_concurrency_limited_response()` call
      site passes its own route label, so "refused unknown-route" cannot be produced again;
  (2) the holders summary names the SESSION concentrating the slots — 16/16 from one session
      was invisible in a per-holder list without counting ids by eye;
  (3) the status payload publishes host pressure (`host`) BESIDE the gate as a gauge that
      cannot fail, and no admission input reads it.

Assertions are on log records, returned envelopes and the published payload — never on
source text.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from types import SimpleNamespace

import pytest

from gateway.platforms import api_server as api
from gateway.platforms import api_server_runs as _runs

LOG = "gateway.platforms.api_server"


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


class _BodyRequest:
    """A POST with a JSON body: the shape /v1/runs reads before it reaches the gate."""

    def __init__(self, path, body):
        self.path = path
        self.method = "POST"
        self.headers = {}
        self._body = body

    async def json(self):
        return self._body


@pytest.fixture()
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(path))
    return path


@pytest.fixture()
def adapter(home, monkeypatch):
    """A one-slot adapter, saturated by one in-flight turn: every surface refuses."""
    monkeypatch.setattr(api, "web", _FakeWeb)
    a = api.APIServerAdapter.__new__(api.APIServerAdapter)
    a._host, a._port = "127.0.0.1", 8642
    a._api_key = ""
    a._run_statuses = {}
    a._agent_gate_holders = {}
    a._pending_agent_requests = 0
    a._inflight_agent_runs = 1          # the one slot the cap has is held
    a._active_run_tasks = {}
    a._max_concurrent_runs = 1
    # Metrics the published payload reads.
    a._metrics_day = api.APIServerAdapter._metrics_day_key()
    a._metrics_last_heartbeat_at = None
    a._metrics_last_request_at = None
    a._metrics_requests_today = 0
    a._metrics_messages_today = 0
    a._metrics_tokens_today = 0
    a._metrics_latency_ms = []
    return a


def _spy_refusals(adapter, monkeypatch):
    """Record every refusal's route AND let the real warning reach the log."""
    routes: list[str] = []
    original = adapter._log_gate_refusal

    def _spy(snapshot, *, route, target_profile):
        routes.append(route)
        return original(snapshot, route=route, target_profile=target_profile)

    monkeypatch.setattr(adapter, "_log_gate_refusal", _spy)
    return routes


# ── (1) every refusal names its surface ───────────────────────────────────────

def _chat_completions(adapter, request):
    return asyncio.run(adapter._handle_chat_completions(request))


def _responses(adapter, request):
    return asyncio.run(adapter._handle_responses(request))


def _runs_surface(adapter, monkeypatch, request):
    # The gate sits after the request is parsed and normalised: stub only the parsing seam,
    # so the code path under test is the production one.
    adapter._openai_error = api._openai_error
    adapter._model_name = "test-model"
    adapter._parse_session_key_header = lambda request: (None, None)

    async def _normalize(request, body):
        return body, None

    adapter._normalize_room_dispatch = _normalize
    monkeypatch.setattr(api, "_request_turn_author", lambda body: None)
    monkeypatch.setattr(api, "_request_agent_overrides",
                        lambda body, virtual_model=None: {})
    adapter._resolve_route = lambda model: {}
    adapter._request_route_conflict_error = lambda **kwargs: None

    async def _history(self, body, raw_input, _openai_error=None):
        return [], "", "", None, ""

    monkeypatch.setattr(_runs, "_resolve_chained_history", _history)
    return asyncio.run(adapter._handle_runs(request))


def _internal_wake(adapter, monkeypatch):
    # A wake retries with backoff; with no delays it gives up after the one refused attempt.
    from gateway import wake
    monkeypatch.setattr(wake, "_RETRY_DELAYS_SECONDS", ())
    adapter._draining_response = lambda: None
    with pytest.raises(RuntimeError, match="internal wake gave up"):
        asyncio.run(_runs.run_internal_session_turn(
            adapter, session_id="sess-1", text="ping", profile="platform-coder",
            _api_server=api))


_SURFACES = [
    ("POST /v1/chat/completions",
     lambda a, m: _chat_completions(a, _BodyRequest("/v1/chat/completions", {}))),
    ("POST /v1/responses",
     lambda a, m: _responses(a, _BodyRequest("/v1/responses", {"input": "hi"}))),
    ("POST /v1/runs",
     lambda a, m: _runs_surface(a, m, _BodyRequest("/v1/runs", {"input": "hi"}))),
    ("internal wake", lambda a, m: _internal_wake(a, m)),
]


@pytest.mark.parametrize("expected,invoke", _SURFACES,
                         ids=["chat_completions", "responses", "runs", "internal_wake"])
def test_every_refusal_names_its_own_surface(adapter, monkeypatch, caplog, expected, invoke):
    routes = _spy_refusals(adapter, monkeypatch)
    with caplog.at_level(logging.WARNING, logger=LOG):
        invoke(adapter, monkeypatch)

    assert routes == [expected]
    assert "unknown-route" not in caplog.text
    assert f"agent-work gate refused {expected}" in caplog.text


def test_refusals_still_carry_the_count_limit_and_terms(adapter, monkeypatch, caplog):
    _spy_refusals(adapter, monkeypatch)
    with caplog.at_level(logging.WARNING, logger=LOG):
        _chat_completions(adapter, _BodyRequest("/v1/chat/completions", {}))

    # A diagnostic that names the route but loses the arithmetic is not the diagnostic.
    assert "count=1 limit=1" in caplog.text
    assert "inflight_turns" in caplog.text


# ── (2) the concentration is named ────────────────────────────────────────────

def test_holders_summary_names_the_session_concentrating_the_slots():
    session = "local-47f5003e-aa11-bb22"
    snapshot = {"holders": [
        {"term": "live_runs", "route": "POST /v1/runs", "session_id": session,
         "run_id": f"run_{index}", "held_seconds": 30.0}
        for index in range(16)
    ]}

    summary = api._gate_holders_summary(snapshot)

    assert "top_sessions=[local-47f500=16]" in " ".join(summary)


def test_holders_summary_ranks_top_three_and_skips_the_unattributed_bucket():
    snapshot = {"holders": [
        {"term": "pending_requests", "session_id": "session-aaaa-1"},
        {"term": "pending_requests", "session_id": "session-bbbb-2"},
        {"term": "pending_requests", "session_id": "session-bbbb-2"},
        {"term": "pending_requests", "session_id": "session-cccc-3"},
        {"term": "pending_requests", "session_id": "session-cccc-3"},
        {"term": "pending_requests", "session_id": "session-cccc-3"},
        {"term": "pending_requests", "session_id": "session-dddd-4"},
        {"term": "pending_requests", "session_id": "session-dddd-4"},
        {"term": "pending_requests", "session_id": "session-dddd-4"},
        {"term": "pending_requests", "session_id": "session-dddd-4"},
        {"term": "pending_requests"},  # no session: never its own entry
    ]}

    summary = api._gate_holders_summary(snapshot)

    assert summary[-1] == "top_sessions=[session-dddd=4, session-cccc=3, session-bbbb=2]"
    assert all("\n" not in fragment for fragment in summary)


def test_holders_summary_stays_one_fragment_with_no_holders():
    assert api._gate_holders_summary({"holders": []})[-1] == "top_sessions=[]"


# ── (3) host pressure is a gauge, published beside the gate ───────────────────

def test_status_payload_publishes_host_load_beside_the_gate(adapter, monkeypatch):
    monkeypatch.setattr(api.os, "getloadavg", lambda: (22.0, 20.5, 18.25))
    monkeypatch.setattr(api.os, "cpu_count", lambda: 14)

    payload = adapter._api_server_status_payload()

    host = payload["host"]
    assert host["cpu_count"] == 14
    assert host["load1"] == 22.0
    assert host["load5"] == 20.5
    assert host["load15"] == 18.25
    assert host["load1_per_core"] == round(22.0 / 14, 2)
    # The gate is published in the SAME object, which is the point of the card.
    assert payload["agent_work_gate"]["limit"] == 1
    # The bind address kept its value under its own name (``host`` is the gauge now).
    assert payload["bind_host"] == "127.0.0.1"


def test_status_payload_survives_a_failing_load_read(adapter, monkeypatch):
    def _unsupported():
        raise OSError("no load average on this platform")

    monkeypatch.setattr(api.os, "getloadavg", _unsupported)

    payload = adapter._api_server_status_payload()

    host = payload["host"]
    assert host["load1"] is None and host["load5"] is None and host["load15"] is None
    assert host["load1_per_core"] is None
    assert isinstance(host["cpu_count"], int)
    assert payload["agent_work_gate"]["enabled"] is True


def test_status_payload_survives_a_missing_load_average_attribute(adapter, monkeypatch):
    monkeypatch.delattr(api.os, "getloadavg", raising=False)

    payload = adapter._api_server_status_payload()

    assert payload["host"]["load1"] is None


def test_host_load_is_not_an_admission_input(adapter, monkeypatch):
    """A load average far above any threshold neither refuses nor admits anything."""
    monkeypatch.setattr(api.os, "getloadavg", lambda: (999.0, 999.0, 999.0))
    monkeypatch.setattr(api.os, "cpu_count", lambda: 1)

    # A saturated gate still refuses, and an empty one still admits, whatever the gauge says.
    assert adapter._concurrency_limited_response(route="POST /v1/runs") is not None
    adapter._inflight_agent_runs = 0
    assert adapter._concurrency_limited_response(route="POST /v1/runs") is None
    assert adapter._agent_gate_snapshot()["terms"] == {
        "pending_requests": 0, "inflight_turns": 0, "live_runs": 0}
