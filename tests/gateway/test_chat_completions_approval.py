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
