"""The api_server peer transports honour the ``hermes pause`` ESTOP allowlist.

``hermes peer dm`` (``POST /api/sessions/{id}/chat``) and ``hermes peer run`` (``POST /v1/runs``)
each make NEW agent work on the receiving gateway, but neither consulted ``agent.estop``: the
inbound messaging path was gated (``gateway/run_inbound.py::_hm_estop_gate``) while the peer path
ran the turn — or admitted it to a live Bot Chat owner — with the fleet paused.

These tests drive the REAL routes over a real aiohttp app (the ``hermes peer dm`` client's own
transport, identity middleware included) against a REAL armed sentinel in a throwaway
``HERMES_HOME``; only the model turn and the allowlist predicate are faked.

``agent.estop.is_allowed`` ships in its own change, so the predicate is stubbed here. Until it
lands the route must still refuse while paused — ``is_allowed`` fails closed on a missing
predicate, the stance the inbound gate takes too — which
``test_paused_fleet_refuses_without_the_predicate`` pins.
"""

from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter, _api_request_profile
from hermes_state import SessionDB

SESSION_ID = "peer_dm_estop"
DM = "estop window probe"
OPERATOR_ID = "operator-uid-7"
MAINTENANCE_PROFILE = "maintenance"


def _ok(text: str) -> dict:
    return {"final_response": text, "failed": False, "completed": True,
            "messages": [], "api_calls": 1}


def _app(adapter: APIServerAdapter) -> web.Application:
    """The peer transports over the adapter's OWN identity middleware (as served)."""
    app = web.Application(middlewares=[adapter._make_profile_prefix_middleware()])
    app.router.add_post("/api/sessions/{session_id}/chat", adapter._handle_session_chat)
    app.router.add_post(
        "/api/sessions/{session_id}/chat/stream", adapter._handle_session_chat_stream)
    app.router.add_post("/v1/runs", adapter._handle_runs)
    return app


def _adapter(tmp_path) -> tuple[APIServerAdapter, str]:
    db = SessionDB(tmp_path / "state.db")
    sid = db.create_session(SESSION_ID, "api_server")
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter._session_db = db
    return adapter, sid


def _identity(adapter: APIServerAdapter) -> tuple:
    """The (principal, profile) pair the adapter's own middleware derives for a bare peer call."""
    return (adapter._derive_browser_control_principal("default"), "default")


def _fake_agent(seen: list) -> MagicMock:
    agent = MagicMock()
    agent.session_id = SESSION_ID
    agent.session_prompt_tokens = 0
    agent.session_completion_tokens = 0
    agent.session_total_tokens = 0
    agent.run_conversation.side_effect = lambda user_message=None, **_kw: (
        seen.append(user_message) or dict(_ok("pong")))
    return agent


def _paused(tmp_path, monkeypatch, *, home=None):
    """Arm the REAL sentinel in a throwaway HERMES_HOME and hand back the real estop module."""
    monkeypatch.setenv("HERMES_HOME", str(home if home is not None else tmp_path))
    from agent import estop
    estop.engage(reason="estop window probe")
    return estop


def _install_predicate(estop, monkeypatch, *, user_ids=(), profiles=(), calls=None,
                       decision=None):
    """Stub ``agent.estop.is_allowed`` with the real predicate's semantics; record every call."""
    def _is_allowed(user_id=None, profile=None, state=None):
        if calls is not None:
            calls.append((user_id, profile))
        if decision is not None:
            return decision
        if user_id is not None and str(user_id) in user_ids:
            return True
        return bool(profile) and str(profile) in profiles
    monkeypatch.setattr(estop, "is_allowed", _is_allowed, raising=False)
    return _is_allowed


@pytest.mark.asyncio
async def test_peer_dm_is_refused_while_paused_and_the_turn_never_runs(tmp_path, monkeypatch):
    estop = _paused(tmp_path, monkeypatch)
    calls: list = []
    _install_predicate(estop, monkeypatch, calls=calls)
    adapter, sid = _adapter(tmp_path)

    with patch.object(adapter, "_create_agent") as create_agent:
        async with TestClient(TestServer(_app(adapter))) as cli:
            resp = await cli.post(f"/api/sessions/{sid}/chat", json={"message": DM})
            body = await resp.json()

    assert resp.status == 503, "a paused fleet must refuse the peer DM, not run it"
    assert body["error"]["message"] == estop.paused_reply()
    assert "Hermes is paused" in body["error"]["message"], (
        "the peer client prints error.message, so the pause notice must BE that message")
    assert body["error"]["code"] == "estop_paused"
    assert not create_agent.called, "no agent turn may run for a refused peer DM"
    assert calls == [_identity(adapter)], (
        "the gate consults the predicate with the authenticated request identity")


@pytest.mark.asyncio
async def test_peer_run_is_refused_while_paused_and_no_run_is_recorded(tmp_path, monkeypatch):
    estop = _paused(tmp_path, monkeypatch)
    calls: list = []
    _install_predicate(estop, monkeypatch, calls=calls)
    adapter, _sid = _adapter(tmp_path)

    async with TestClient(TestServer(_app(adapter))) as cli:
        resp = await cli.post("/v1/runs", json={"input": DM})
        body = await resp.json()

    assert resp.status == 503
    assert "Hermes is paused" in body["error"]["message"]
    assert body["error"]["code"] == "estop_paused"
    assert not adapter._run_owners, "a refused run leaves no run record behind"
    assert calls == [_identity(adapter)]


@pytest.mark.asyncio
async def test_streamed_peer_dm_is_refused_while_paused(tmp_path, monkeypatch):
    """The streamed form runs the same turn — it must not be the way past the pause."""
    estop = _paused(tmp_path, monkeypatch)
    _install_predicate(estop, monkeypatch)
    adapter, sid = _adapter(tmp_path)

    with patch.object(adapter, "_create_agent") as create_agent:
        async with TestClient(TestServer(_app(adapter))) as cli:
            resp = await cli.post(f"/api/sessions/{sid}/chat/stream", json={"message": DM})
            body = await resp.json()

    assert resp.status == 503
    assert "Hermes is paused" in body["error"]["message"]
    assert not create_agent.called


@pytest.mark.asyncio
async def test_allowlisted_principal_is_served_while_paused(tmp_path, monkeypatch):
    """The identity the middleware derives from the presented API key is what an operator
    allowlists — and then the same peer DM goes through with the fleet still paused."""
    estop = _paused(tmp_path, monkeypatch)
    adapter, sid = _adapter(tmp_path)
    principal, profile = _identity(adapter)
    calls: list = []
    _install_predicate(estop, monkeypatch, user_ids=(principal,), calls=calls)
    seen: list = []

    with patch.object(adapter, "_create_agent", side_effect=lambda **_kw: [_fake_agent(seen)][0]):
        async with TestClient(TestServer(_app(adapter))) as cli:
            resp = await cli.post(f"/api/sessions/{sid}/chat", json={"message": DM})
            body = await resp.json()

    assert resp.status == 200, "an allowlisted identity keeps working through the pause"
    assert body["message"]["content"] == "pong"
    assert seen == [DM], "the allowlisted peer DM actually ran its turn"
    assert calls == [(principal, profile)]


@pytest.mark.asyncio
async def test_body_author_cannot_allowlist_itself(tmp_path, monkeypatch):
    """``author`` is a memory-attribution label the caller writes; it grants nothing, so naming an
    allowlisted id in the body must not admit the turn."""
    estop = _paused(tmp_path, monkeypatch)
    calls: list = []
    _install_predicate(estop, monkeypatch, user_ids=(OPERATOR_ID,), calls=calls)
    adapter, sid = _adapter(tmp_path)

    with patch.object(adapter, "_create_agent") as create_agent:
        async with TestClient(TestServer(_app(adapter))) as cli:
            resp = await cli.post(f"/api/sessions/{sid}/chat", json={
                "message": DM, "author": {"id": OPERATOR_ID, "name": "operator"}})
            body = await resp.json()

    assert resp.status == 503, "a body-supplied id cannot admit a turn the fleet paused"
    assert "Hermes is paused" in body["error"]["message"]
    assert calls == [_identity(adapter)], "the body's author never reaches the predicate"
    assert not create_agent.called


@pytest.mark.asyncio
async def test_relaxed_traffic_is_untouched_when_no_sentinel_is_armed(tmp_path, monkeypatch):
    """No engagement → the route behaves exactly as before (and never consults the allowlist)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from agent import estop
    calls: list = []
    _install_predicate(estop, monkeypatch, calls=calls)
    adapter, sid = _adapter(tmp_path)
    seen: list = []

    with patch.object(adapter, "_create_agent", side_effect=lambda **_kw: [_fake_agent(seen)][0]):
        async with TestClient(TestServer(_app(adapter))) as cli:
            resp = await cli.post(f"/api/sessions/{sid}/chat", json={"message": DM})

    assert resp.status == 200
    assert seen == [DM]
    assert calls == [], "with no sentinel engaged the allowlist is not consulted at all"


@pytest.mark.asyncio
async def test_paused_fleet_refuses_without_the_predicate(tmp_path, monkeypatch):
    """A paused fleet with no predicate admits nobody: the pre-allowlist tree must fail CLOSED
    (the inbound gate's own stance), never run the turn."""
    estop = _paused(tmp_path, monkeypatch)
    monkeypatch.delattr(estop, "is_allowed", raising=False)
    adapter, sid = _adapter(tmp_path)

    with patch.object(adapter, "_create_agent") as create_agent:
        async with TestClient(TestServer(_app(adapter))) as cli:
            resp = await cli.post(f"/api/sessions/{sid}/chat", json={"message": DM})
            body = await resp.json()

    assert resp.status == 503
    assert "Hermes is paused" in body["error"]["message"]
    assert not create_agent.called


def test_gate_takes_the_identity_from_the_request_context(tmp_path, monkeypatch):
    """The identity comes from the authenticated context only — the gate is handed no request at
    all, so no body field can influence it."""
    estop = _paused(tmp_path, monkeypatch)
    calls: list = []
    _install_predicate(estop, monkeypatch, profiles=(MAINTENANCE_PROFILE,), calls=calls)
    adapter, _sid = _adapter(tmp_path)

    assert adapter._estop_peer_pause_notice() is not None, "an unnamed caller is refused"
    assert calls == [(None, "default")]

    token = _api_request_profile.set(MAINTENANCE_PROFILE)
    try:
        assert adapter._estop_peer_pause_notice() is None, "the allowlisted lane is admitted"
    finally:
        _api_request_profile.reset(token)
    assert calls == [(None, "default"), (None, MAINTENANCE_PROFILE)]


def test_gate_names_the_profile_the_process_serves_not_the_literal_default(tmp_path, monkeypatch):
    """A per-profile gateway's own lane must be nameable in the allowlist: with no ``/p/`` prefix
    the identity is the profile this process serves, never the string ``default``."""
    home = tmp_path / "profiles" / MAINTENANCE_PROFILE
    home.mkdir(parents=True)
    estop = _paused(tmp_path, monkeypatch, home=home)
    calls: list = []
    _install_predicate(estop, monkeypatch, profiles=(MAINTENANCE_PROFILE,), calls=calls)
    adapter = APIServerAdapter(PlatformConfig(enabled=True))

    assert adapter._estop_peer_pause_notice() is None, "the lane's own profile is allowlisted"
    assert calls == [(None, MAINTENANCE_PROFILE)]
