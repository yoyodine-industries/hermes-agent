"""Trigger 3's tick must not wait for the pass it scheduled.

``sweep_loop`` used to ``await drain_once(...)`` inside the tick. The pass itself is
concurrent per lane (see ``test_sweep_lane_concurrency.py``), but the LOOP still ran
one pass at a time, and a pass is only over when its slowest lane's turn is over --
minutes, live. So the tick period degenerated from ``sweep_seconds`` to "the slowest
lane's turn": every lane that got a delivery after the pass started went unserved
until that turn ended, and for an idle lane this sweep is the ONLY drain path.

What is asserted here, on real on-disk deliveries under a tmp HERMES_HOME, real
per-profile turn locks and real turn settlement:

1. a lane that is free at tick time settles its delivery while another lane's turn is
   still running (a pass is no longer the unit of progress);
2. a delivery admitted to a THIRD, idle lane while that slow turn is still running is
   picked up by a later tick, inside that turn's window -- the admission every 30s
   re-scan exists for, and the assertion an awaited pass cannot satisfy;
3. the slow lane's own record still settles once its turn ends (decoupling does not
   lose the turn's settlement);
4. cancelling the loop (gateway shutdown) cancels the in-flight pass's turn and
   leaves no leaked turn or pass task behind.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from gateway.platforms import api_server as api
from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore
from tools import bot_delivery_queue as q

# The live knob is 30s; the assertions are event-driven (never a sleep budget), so a
# short period only shortens the test's wall clock.
SWEEP_SECONDS = 0.05


@pytest.fixture()
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(path))
    return path


@pytest.fixture()
def adapter(home, monkeypatch):
    a = api.APIServerAdapter.__new__(api.APIServerAdapter)
    a._run_idempotency_store = RunIdempotencyStore(":memory:")
    a._run_statuses = {}
    a._run_idempotency_ids = set()
    a._run_owners = {}
    a._run_owner_pid = os.getpid()
    a._run_owner_started = 0
    a._model_name = "test-model"
    a._api_key = ""
    a._pending_agent_requests = 0
    a.calls = []

    async def _history(session_id):
        return []

    a._conversation_history_for_session = _history
    yield a
    a._run_idempotency_store.close()


def _lane_home(home, profile):
    return home / "profiles" / profile


def _queue_lane(home, profile, count=1):
    """``count`` real queued deliveries in ``profile``'s own home, FIFO."""
    lane_home = _lane_home(home, profile)
    lane_home.mkdir(parents=True, exist_ok=True)
    records = []
    for i in range(count):
        records.append(
            q.admit(
                lane_home,
                sender_profile="sender",
                target_profile=profile,
                target_session_id=f"sess-{profile}",
                idempotency_key=q.validate_idempotency_key(
                    f"auto:decoupled:{profile}:{i}"
                ),
                fingerprint="fp",
                delivery_id=f"{profile}{i}".encode().hex()[:32].ljust(32, "0"),
                message=f"{profile}-{i}",
            )
        )
    assert [r["status"] for r in records] == [q.STATUS_QUEUED] * count
    return records


async def _settles(profile, *, home, timeout=2.0):
    """True once ``profile``'s queue is empty, i.e. its delivery was settled."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if q.queue_depth(_lane_home(home, profile), profile) == 0:
            return True
        await asyncio.sleep(0.01)
    return False


def _live_tasks():
    return [
        t
        for t in asyncio.all_tasks()
        if t is not asyncio.current_task() and not t.done()
    ]


def test_a_slow_lane_turn_does_not_hold_the_next_sweep_tick(adapter, home, monkeypatch):
    """A pass in flight must not stop the loop re-scanning the roster."""
    from gateway.platforms import api_server_bot_delivery as drain

    monkeypatch.setattr(q, "sweep_seconds", lambda: SWEEP_SECONDS)
    _queue_lane(home, "lane-slow")
    _queue_lane(home, "lane-free")

    async def _scenario():
        slow_release = asyncio.Event()
        slow_finished = asyncio.Event()
        observed: dict[str, Any] = {}

        async def _run_agent(conversation_history=None, **kwargs):
            adapter.calls.append(kwargs)
            if kwargs.get("session_id") == "sess-lane-slow":
                await slow_release.wait()
                slow_finished.set()
            return {"final_response": "pong"}, {}

        adapter._run_agent = _run_agent
        sweep = asyncio.ensure_future(drain.sweep_loop(adapter))
        try:
            # 1. the lane that is free at tick time is served while the slow lane's
            #    turn is still running.
            served = await _settles("lane-free", home=home)
            observed["free_while_slow_turn_running"] = (
                served and not slow_finished.is_set()
            )

            # 2. an admission to a THIRD, idle lane, made while the slow lane's turn
            #    is still running: the pass in flight at step 1 never sees it.
            _queue_lane(home, "lane-mid")
            served = await _settles("lane-mid", home=home)
            observed["mid_turn_admission_while_slow_turn_running"] = (
                served and not slow_finished.is_set()
            )

            # 3. the slow lane's own record still settles once its turn ends.
            slow_release.set()
            observed["slow_lane_settles_after_release"] = await _settles(
                "lane-slow", home=home
            )
        finally:
            slow_release.set()
            sweep.cancel()
            cancelled_ok = False
            try:
                await sweep
            except asyncio.CancelledError:
                cancelled_ok = True
            observed["sweep_cancels"] = cancelled_ok
            observed["leaked"] = _live_tasks()
        return observed

    observed = asyncio.run(_scenario())

    assert observed["free_while_slow_turn_running"], (
        "a free lane's delivery did not settle until the slow lane's turn ended: the "
        "tick waited for the pass it scheduled instead of moving on"
    )
    assert observed["mid_turn_admission_while_slow_turn_running"], (
        "a delivery admitted to an idle lane mid-turn stayed queued until the slow "
        "lane's turn ended: the tick did not run a second pass over the roster"
    )
    assert observed["slow_lane_settles_after_release"], (
        "the slow lane's own delivery never settled after its turn ended"
    )
    assert observed["sweep_cancels"], "the sweep loop swallowed its cancellation"
    assert observed["leaked"] == [], f"tasks outlived the sweep: {observed['leaked']}"


def test_cancelling_the_sweep_loop_cancels_an_in_flight_pass(adapter, home, monkeypatch):
    """Gateway shutdown: the loop cancels the pass it scheduled, leaving no task."""
    from gateway.platforms import api_server_bot_delivery as drain

    monkeypatch.setattr(q, "sweep_seconds", lambda: SWEEP_SECONDS)
    _queue_lane(home, "lane-slow")

    async def _scenario():
        turn_started = asyncio.Event()
        cancelled: list[str] = []

        async def _run_agent(conversation_history=None, **kwargs):
            adapter.calls.append(kwargs)
            turn_started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.append(str(kwargs.get("session_id")))
                raise
            return {"final_response": "pong"}, {}

        adapter._run_agent = _run_agent
        sweep = asyncio.ensure_future(drain.sweep_loop(adapter))
        await asyncio.wait_for(turn_started.wait(), timeout=2.0)
        sweep.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sweep
        return cancelled, _live_tasks()

    cancelled, leaked = asyncio.run(_scenario())

    assert cancelled == ["sess-lane-slow"], "the in-flight lane's turn was not cancelled"
    assert leaked == [], f"turn or pass tasks outlived the sweep: {leaked}"
