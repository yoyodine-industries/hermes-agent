"""A one-shot whose socket died mid-turn: the turn is the OWNER's, so the viewer redials, resumes the
session and settles the admission from its receipt — never the unknown-outcome error, and never a
resend of work that may already have run."""
import asyncio
from contextlib import asynccontextmanager

import pytest

from hermes_cli.gateway_chat_view import GatewayChatView
from hermes_cli.gateway_client import DISCONNECTED, GatewayClientError
from tests.waiting import async_wait_until as until

SNAPSHOT = {"stored_session_id": "s", "execution_generation": 3,
            "pending": [{"admission_id": "a", "status": "started"}], "prompts": []}


class Peer:
    """One gateway life as this viewer sees it."""

    def __init__(self, receipt=None, *, submit=None):
        self.events = asyncio.Queue()
        self.calls = []
        self.receipt = receipt if receipt is not None else {
            "admission_id": "a", "status": "terminal", "outcome": "completed",
            "result": {"final_response": "DONE", "completed": True}}
        self._submit = submit or {"admission_id": "a", "status": "queued"}

    async def rpc(self, method, **params):
        self.calls.append((method, params))
        if method == "prompt.submit":
            return self._submit
        if method == "session.resume":
            return dict(SNAPSHOT)
        if method == "prompt.receipt":
            return dict(self.receipt) if self.receipt.get("status") != "started" else {
                "admission_id": "a", "status": "started", "outcome": None}
        return {}


@asynccontextmanager
async def dial_to(peer):
    yield peer


async def one_shot(view, monkeypatch):
    """Run ``view`` as ``hermes -z`` and cut its socket once the turn is admitted."""
    view.unattended = True  # `-z`: the reply is the only thing on stdout
    task = asyncio.create_task(view.run("q", oneshot=True))
    await until(lambda: view.finite_admission or task.done())
    return task


@pytest.mark.asyncio
async def test_a_dead_socket_settles_the_turn_from_its_receipt(capsys):
    first, second = Peer(), Peer()
    view = GatewayChatView(first, {"stored_session_id": "s"})
    view.dial = lambda: dial_to(second)
    task = await one_shot(view, None)
    first.events.put_nowait(GatewayClientError(DISCONNECTED))

    assert await asyncio.wait_for(task, 10) == 0
    assert capsys.readouterr().out.strip() == "DONE"
    assert ("session.resume", {"session_id": "s"}) in second.calls
    assert any(method == "prompt.receipt" and params.get("admission_id") == "a"
               for method, params in second.calls)
    assert not any(method == "prompt.submit" for method, _ in second.calls), "the prompt was resent"


@pytest.mark.asyncio
async def test_a_turn_still_running_is_polled_until_it_settles(monkeypatch):
    second = Peer(receipt={"admission_id": "a", "status": "started", "outcome": None})
    view = GatewayChatView(Peer(), {"stored_session_id": "s"})
    view.dial = lambda: dial_to(second)
    task = await one_shot(view, monkeypatch)
    view.client.events.put_nowait(GatewayClientError(DISCONNECTED))

    await until(lambda: any(method == "prompt.receipt" for method, _ in second.calls))
    second.receipt = {"admission_id": "a", "status": "terminal", "outcome": "completed",
                      "result": {"final_response": "LATE", "completed": True}}
    assert await asyncio.wait_for(task, 10) == 0
    assert view.completions["a"]["text"] == "LATE"


@pytest.mark.asyncio
async def test_an_unreachable_owner_keeps_the_honest_unknown_error():
    view = GatewayChatView(Peer(), {"stored_session_id": "s"})

    def refuse():
        raise GatewayClientError("Gateway connection failed; no local fallback")

    view.dial = refuse
    task = await one_shot(view, None)
    view.client.events.put_nowait(GatewayClientError(DISCONNECTED))
    with pytest.raises(GatewayClientError, match="outcome is unknown"):
        await asyncio.wait_for(task, 10)


@pytest.mark.asyncio
async def test_a_lost_turn_is_reported_lost_not_completed(capsys):
    second = Peer(receipt={"admission_id": "a", "status": "unknown", "outcome": None})
    view = GatewayChatView(Peer(), {"stored_session_id": "s"})
    view.dial = lambda: dial_to(second)
    task = await one_shot(view, None)
    view.client.events.put_nowait(GatewayClientError(DISCONNECTED))

    assert await asyncio.wait_for(task, 10) == 2
    assert "couldn't confirm how this turn ended" in capsys.readouterr().out
