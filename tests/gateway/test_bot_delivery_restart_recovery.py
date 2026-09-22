"""The drained record's already-delivered guard (restart recovery, DoD #4).

Recovery re-offers a record whose turn died mid-flight
(``bot_delivery_queue.recover_running_claim``). If that turn had already put the
message into the session before it died, running it again answers the sender
twice; the guard settles the record ``delivered`` instead. It must fire ONLY on
a re-offered record whose body is already in the history -- a normal first-time
drain has no matching user message and still runs its turn.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

from gateway.platforms import api_server_bot_delivery as drain
from tools import bot_delivery_queue as q

BODY = "Message from bot (@sender): are you there?"


class _FakeAdapter:
    """Just the adapter surface ``run_record`` reaches for."""

    def __init__(self, history):
        self.history = history
        self.turns = []
        self.statuses = []

    def _profile_scope(self, profile):
        return contextlib.nullcontext()

    async def _conversation_history_for_session(self, session_id):
        return list(self.history)

    async def _run_agent(self, **kwargs):
        self.turns.append(kwargs)
        return {"final_response": "pong"}, {}

    def _bot_send_record_status(self, delivery_id, record, **fields):
        self.statuses.append((delivery_id, dict(record), fields))


def _claimed_record(home, *, message=BODY):
    q.admit(
        home,
        sender_profile="sender",
        target_profile="bravo",
        target_session_id="sess-1",
        idempotency_key="peer-guard-1",
        fingerprint="fp-guard-1",
        delivery_id="%032x" % 1,
        message=message,
    )
    return q.claim_next(home, target_profile="bravo", lease_ok=True)


@pytest.mark.asyncio
async def test_reoffered_record_already_in_history_settles_without_rerunning(tmp_path):
    record = _claimed_record(tmp_path)
    adapter = _FakeAdapter([{"role": "user", "content": BODY}])

    settled, reply = await drain.run_record(adapter, Path(tmp_path), record)

    assert adapter.turns == []  # the turn was never re-run
    assert settled["status"] == "delivered"
    assert (tmp_path / "runtime" / q.DELIVERY_DIR_NAME / q.SETTLED_DIR / f"{record['delivery_id']}.json").exists()
    # the sender is told it was delivered, not handed a second answer
    assert adapter.statuses[-1][2]["result"] == q.RESULT_DELIVERED
    assert reply in (None, "")


@pytest.mark.asyncio
async def test_first_time_drain_still_runs_the_turn(tmp_path):
    record = _claimed_record(tmp_path)
    adapter = _FakeAdapter([{"role": "user", "content": "something else entirely"}])

    settled, reply = await drain.run_record(adapter, Path(tmp_path), record)

    assert len(adapter.turns) == 1
    assert adapter.turns[0]["user_message"] == BODY
    assert settled["status"] == "delivered"
    assert reply == "pong"


@pytest.mark.asyncio
async def test_guard_ignores_a_same_text_tool_or_assistant_turn(tmp_path):
    """Only a USER message counts: a tool echo must not short-circuit a turn."""
    record = _claimed_record(tmp_path)
    adapter = _FakeAdapter(
        [
            {"role": "assistant", "content": BODY},
            {"role": "tool", "content": BODY},
        ]
    )

    settled, _reply = await drain.run_record(adapter, Path(tmp_path), record)

    assert len(adapter.turns) == 1
    assert settled["status"] == "delivered"
