"""Legacy /v1/chat/completions streams surface approval requests that the client can resolve
through ``POST /v1/runs/{completion_id}/approval`` (#51871)."""
import asyncio
import json
import time
from unittest.mock import MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

# Imported at MODULE scope, not inside the test: importing ``gateway.run`` runs
# ``hermes_bootstrap`` → ``pm.environments.activate_dependencies``, which probes
# ``<checkout>/../manifest.json``. On a default install the checkout lives INSIDE
# ``~/.hermes``, so that probe is a real-home read that the session's
# ``_forbid_real_hermes_home_io`` guard refuses once it is installed. Collection-time
# imports run before the fixture, so the probe happens where it is legitimate.
from gateway.run import _redact_approval_command

from tests.gateway.test_api_server import _create_app, _make_adapter


def _approval_agent(decisions):
    agent = MagicMock()
    agent.session_prompt_tokens = agent.session_completion_tokens = agent.session_total_tokens = 0

    def run_conversation(**_kw):
        # The real blocking wait the terminal guard uses, reached through the turn's contextvar.
        from tools.approval import _gateway_notify_cb
        from tools.approval_context import get_current_session_key
        from tools.approval_gateway_wait import _await_gateway_decision
        key = get_current_session_key(default="")
        decisions.append(_await_gateway_decision(
            key, _gateway_notify_cb(key), {"command": "rm -rf /tmp/x", "description": "dangerous",
                                           "pattern_key": "rm"}))
        return {"final_response": "ok", "messages": []}
    agent.run_conversation.side_effect = run_conversation
    return agent


@pytest.mark.asyncio
async def test_streamed_completion_approval_resolves_via_runs_endpoint(monkeypatch):
    # Bound the agent thread's blocking wait so a regression fails fast instead of holding teardown.
    monkeypatch.setattr("tools.approval_context._get_approval_timeout", lambda: 5)
    adapter, decisions = _make_adapter(), []
    app = _create_app(adapter)
    app.router.add_post("/v1/runs/{run_id}/approval", adapter._handle_run_approval)
    async with TestClient(TestServer(app)) as cli:
        with patch.object(adapter, "_create_agent", side_effect=lambda **_k: _approval_agent(decisions)):
            resp = await cli.post("/v1/chat/completions", json={
                "model": "hermes-agent", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
            assert resp.status == 200
            async def _first_approval():
                event = None
                async for raw in resp.content:
                    line = raw.decode().strip()
                    if line == "event: approval.request":
                        event = "next"
                    elif event == "next" and line.startswith("data: "):
                        return json.loads(line[6:])
                return None
            event = await asyncio.wait_for(_first_approval(), 10)
            assert event and event["run_id"].startswith("chatcmpl") and event["command"]
            # t_86e21fa3: the row a client is about to approve is HELD — the stream's inline
            # handle exists from admission, so the fence can't mistake live work for an orphan.
            assert adapter._run_has_live_handle(event["run_id"]) is True
            assert event["run_id"] not in adapter._active_run_tasks, "an inline turn never enters the gate"
            approval = await cli.post(f"/v1/runs/{event['run_id']}/approval", json={"choice": "once"})
            assert approval.status == 200, await approval.text()
            assert "[DONE]" in (await asyncio.wait_for(resp.text(), 10))
    assert decisions and decisions[0]["choice"] == "once"
    assert event["run_id"] not in adapter._run_approval_sessions
    assert adapter._run_statuses[event["run_id"]]["status"] == "completed"
    # The done callback is the turn's last exit path, so the handle is gone with it.
    assert event["run_id"] not in adapter._live_run_handles


_APPROVAL_DATA = {"command": "rm -rf /tmp/x", "description": "dangerous", "pattern_key": "rm"}


def _register_stream_run(adapter, completion_id):
    """Register one streaming completion exactly as ``_handle_chat_completions`` does."""
    request = MagicMock()
    request.headers = {}
    stream_q = MagicMock()
    approval_notify, on_done = adapter._register_stream_approval(
        request, completion_id, stream_q, "session-under-test")
    return request, stream_q, approval_notify, on_done


def test_waiting_for_approval_row_is_held_at_admission_and_survives_the_sweep():
    """(a) t_86e21fa3: a parked approval is live work, not an orphan.

    The handle is taken in the same frame as the row (before it), it is an ``inline``
    token rather than an ``_active_run_tasks`` entry (``_run_agent`` already counts the
    turn), and the parked row reads back unchanged through the fence's own read path and
    through the sweep that would otherwise close it.
    """
    adapter = _make_adapter()
    completion_id = "chatcmpl_held"
    request, stream_q, approval_notify, _on_done = _register_stream_run(adapter, completion_id)
    try:
        assert adapter._run_statuses[completion_id]["status"] == "running"
        assert adapter._live_run_handles[completion_id]["family"] == "inline"
        assert completion_id not in adapter._active_run_tasks
        assert adapter._run_has_live_handle(completion_id) is True

        approval_notify(dict(_APPROVAL_DATA))
        row = adapter._run_statuses[completion_id]
        assert row["status"] == "waiting_for_approval"
        assert row["last_event"] == "approval.request"
        kind, queued_event = stream_q.put_threadsafe.call_args[0][0]
        assert kind == "__approval__"
        assert queued_event["command"] == _redact_approval_command(_APPROVAL_DATA["command"])

        # The read path the approval POST uses: no fence while the handle is held.
        read = adapter._durable_run_status(request, completion_id)
        assert read["status"] == "waiting_for_approval"
        # And the sweep — which never waits for a reader — leaves it alone too.
        adapter._sweep_orphaned_runs_once(time.time())
        assert row["status"] == "waiting_for_approval"
        assert row["last_event"] == "approval.request"
    finally:
        adapter._release_live_run_handle(completion_id)


@pytest.mark.asyncio
async def test_on_done_releases_the_stream_handle_on_completion_and_on_cancellation():
    """(b) t_86e21fa3: every exit path of the turn drops the token.

    ``on_done`` runs in the agent task's done callback, so a turn that answers and a turn
    that is cancelled both pass through it; the token must not outlive either.
    """
    adapter = _make_adapter()
    request, _stream_q, approval_notify, on_done = _register_stream_run(adapter, "chatcmpl_done")
    approval_notify(dict(_APPROVAL_DATA))
    assert adapter._run_has_live_handle("chatcmpl_done") is True

    async def _turn():
        return ({"final_response": "ok"}, {})

    finished = asyncio.ensure_future(_turn())
    await finished
    on_done(finished)
    assert "chatcmpl_done" not in adapter._live_run_handles
    assert "chatcmpl_done" not in adapter._run_approval_sessions
    assert adapter._run_statuses["chatcmpl_done"]["status"] == "completed"
    assert adapter._durable_run_status(request, "chatcmpl_done")["status"] == "completed"

    # Cancellation is the same callback with a cancelled future.
    _request, _q, approval_notify2, on_done2 = _register_stream_run(adapter, "chatcmpl_cancelled")
    approval_notify2(dict(_APPROVAL_DATA))
    never = asyncio.ensure_future(asyncio.Event().wait())
    never.cancel()
    with pytest.raises(asyncio.CancelledError):
        await never
    on_done2(never)
    assert "chatcmpl_cancelled" not in adapter._live_run_handles
    assert adapter._run_statuses["chatcmpl_cancelled"]["status"] == "cancelled"


def test_a_stream_handle_dropped_without_on_done_is_fenced_on_read_and_by_the_sweep():
    """(c) t_86e21fa3: the genuine-orphan arm still fires.

    A worker that dies between admission and its done callback leaves a parked row nobody
    holds; the read path and the sweep must both close it, or the token would be a latch
    that makes every non-terminal row look alive.
    """
    adapter = _make_adapter()

    # Read arm: the approval POST (or any status read) is the first visitor.
    request, _q, approval_notify, _on_done = _register_stream_run(adapter, "chatcmpl_orphan_read")
    approval_notify(dict(_APPROVAL_DATA))
    assert adapter._run_statuses["chatcmpl_orphan_read"]["status"] == "waiting_for_approval"
    adapter._release_live_run_handle("chatcmpl_orphan_read")   # died: on_done never runs
    fenced = adapter._durable_run_status(request, "chatcmpl_orphan_read")
    assert fenced["status"] == "interrupted"
    assert fenced["last_event"] == "run.interrupted"
    assert "no live worker" in fenced["error"]

    # Sweep arm: nobody ever polls this one.
    _request, _q2, approval_notify2, _on_done2 = _register_stream_run(adapter, "chatcmpl_orphan_swept")
    approval_notify2(dict(_APPROVAL_DATA))
    adapter._release_live_run_handle("chatcmpl_orphan_swept")
    adapter._sweep_orphaned_runs_once(time.time())
    assert adapter._run_statuses["chatcmpl_orphan_swept"]["status"] == "interrupted"


def _canonical_approval_turn(owner, decisions):
    """A controlled canonical turn: the real blocking gateway wait, published through the
    authority's shared controls exactly as ``TurnRunner._approval_notify_sync`` does."""
    async def handle(event):
        from gateway.session_results import execution_result
        from tools.approval_gateway_wait import _await_gateway_decision
        sid = event.source.chat_id
        live, generation = owner.sessions[sid], owner.db.get_session(sid)['runtime_generation']

        def notify(data):
            owner.register_approval(sid, generation, live.route, data)
        decisions.append(await asyncio.to_thread(_await_gateway_decision, live.route, notify, {
            "command": "rm -rf /tmp/x", "description": "dangerous", "pattern_key": "rm"}))
        execution_result.get()['result'] = {'final_response': 'ok', 'messages': []}
        return 'ok'
    return handle


async def _sse_event(resp, name):
    pending = None
    async for raw in resp.content:
        line = raw.decode().strip()
        if line == f"event: {name}":
            pending = True
        elif pending and line.startswith("data: "):
            return json.loads(line[6:])
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["chat_completions", "session_stream"])
async def test_canonical_streamed_approval_reaches_sse_and_resolves_via_runs_endpoint(api, owner, monkeypatch, surface):
    """Under the session authority the advertised stream run id is bound to its exact durable
    admission: the shared approval prompt reaches the SSE, and the generation-fenced reply to
    ``POST /v1/runs/{run_id}/approval`` resolves the same waiter WS viewers see."""
    monkeypatch.setattr("tools.approval_context._get_approval_timeout", lambda: 5)
    decisions = []
    owner.runner._handle_message = _canonical_approval_turn(owner, decisions)
    app = _create_app(api)
    app.router.add_post("/v1/runs/{run_id}/approval", api._handle_run_approval)
    async with TestClient(TestServer(app)) as cli:
        if surface == "chat_completions":
            resp = await cli.post("/v1/chat/completions", json={
                "model": "hermes-agent", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
            prefix = "chatcmpl"
        else:
            owner.db.create_session("api-stream", source="api_server")
            resp = await cli.post("/api/sessions/api-stream/chat/stream", json={"message": "hi"})
            prefix = "run_"
        assert resp.status == 200, await resp.text()
        event = await asyncio.wait_for(_sse_event(resp, "approval.request"), 10)
        assert event and event["run_id"].startswith(prefix) and event["command"], event
        assert set(event["choices"]) == {"once", "session", "always", "deny"}
        stale = await cli.post(f"/v1/runs/{event['run_id']}/approval", json={
            "request_id": event["request_id"], "execution_generation": event["execution_generation"] + 1,
            "choice": "once"})
        assert stale.status == 409 and (await stale.json())["error"]["code"] == "stale_generation"
        approval = await cli.post(f"/v1/runs/{event['run_id']}/approval", json={
            "request_id": event["request_id"], "execution_generation": event["execution_generation"],
            "choice": "once"})
        assert approval.status == 200, await approval.text()
        assert (await approval.json())["status"] == "resolved"
        await asyncio.wait_for(resp.text(), 10)
    assert decisions and decisions[0]["choice"] == "once"


@pytest.mark.asyncio
async def test_exact_streamed_retry_replays_the_already_pending_approval(api, owner, monkeypatch):
    """A client that disconnects while an approval is pending and retries the unchanged request
    (same session, Idempotency-Key, auth scope) reuses the admission under a new completion id.
    The prompt was published before the retry subscribed, so the retry stream must replay it from
    the authority's pending controls, or an SSE-only client waits on a prompt it never sees."""
    monkeypatch.setattr("tools.approval_context._get_approval_timeout", lambda: 15)
    decisions = []
    owner.runner._handle_message = _canonical_approval_turn(owner, decisions)
    api._api_key = "pending-retry-key-long-enough"
    headers = {"Authorization": "Bearer " + api._api_key, "X-Hermes-Session-Id": "retry-sse",
               "Idempotency-Key": "retry-once"}
    body = {"model": "hermes-agent", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
    app = _create_app(api)
    app.router.add_post("/v1/runs/{run_id}/approval", api._handle_run_approval)
    async with TestClient(TestServer(app)) as cli:
        first = await cli.post("/v1/chat/completions", json=body, headers=headers)
        assert first.status == 200, await first.text()
        original = await asyncio.wait_for(_sse_event(first, "approval.request"), 10)
        assert original, "first stream never saw the approval"
        first.close()  # disconnect while the prompt is still pending
        retry = await cli.post("/v1/chat/completions", json=body, headers=headers)
        assert retry.status == 200, await retry.text()
        replayed = await asyncio.wait_for(_sse_event(retry, "approval.request"), 5)
        assert replayed["request_id"] == original["request_id"]
        assert replayed["execution_generation"] == original["execution_generation"]
        assert replayed["run_id"].startswith("chatcmpl") and replayed["run_id"] != original["run_id"]
        approval = await cli.post(f"/v1/runs/{replayed['run_id']}/approval", json={
            "request_id": replayed["request_id"], "execution_generation": replayed["execution_generation"],
            "choice": "once"}, headers={"Authorization": headers["Authorization"]})
        assert approval.status == 200, await approval.text()
        assert (await approval.json())["status"] == "resolved"
        text = await asyncio.wait_for(retry.text(), 10)
    assert "[DONE]" in text and text.count("event: approval.request") == 0  # replayed exactly once
    assert [d["choice"] for d in decisions] == ["once"]
    with owner.db._read_ctx() as conn:
        rows = conn.execute("SELECT status FROM session_admissions WHERE principal_id='api'").fetchall()
    assert [r[0] for r in rows] == ["terminal"]
