"""Trigger 3 (the 30s sweep) must drain every lane CONCURRENTLY, one task per lane.

The sweep visit was a single sequential loop: ``for lane: take that lane's slot ->
run its turns -> next lane``. ``await run_record(...)`` is a whole agent turn, so a
slow lane (a 28-minute delivery was observed live; also a lease wait, or a
desktop-held slot) held the loop for the entire turn and every lane behind it went
unserved -- however idle that lane was and however deep its own backlog, because
for an idle lane this sweep is the ONLY drain path. Slots are independent (one
lockfile per profile), so the lanes are independent too.

What is asserted here, on real on-disk deliveries under a tmp HERMES_HOME, real
per-profile turn locks and real turn settlement:

1. two lanes, three queued records each, behind a slow ``_run_agent``: all six
   drain in roughly MAX(lane duration), not the sum, with per-lane FIFO preserved;
2. a lane whose slot is HELD defers only itself -- the free lanes still drain in
   the same pass -- and it is the only lane that logs ``drain_deferred``;
3. a lane wedged mid-turn does not stall a free lane behind it;
4. cancelling the sweep (gateway shutdown) cancels every lane's in-flight turn
   and leaves no leaked turn task behind.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import pytest

from gateway.platforms import api_server as api
from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore
from tools import bot_delivery_queue as q

SLOW_TURN_SECONDS = 0.2


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


def _queue_lane(home, profile, count, *, tag=""):
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
                    f"auto:conc:{profile}:{tag}:{i}"
                ),
                fingerprint="fp",
                delivery_id=f"{profile}{tag}{i}".encode().hex()[:32].ljust(32, "0"),
                message=f"{profile}-{i}",
            )
        )
    assert [r["status"] for r in records] == [q.STATUS_QUEUED] * count
    return records


def _per_lane_calls(calls):
    """``{profile: [user_message, ...]}`` in the order the turns actually ran."""
    per_lane: dict[str, list[str]] = {}
    for call in calls:
        lane = str(call.get("session_id") or "").replace("sess-", "")
        per_lane.setdefault(lane, []).append(str(call.get("user_message")))
    return per_lane


def test_two_lanes_drain_concurrently_and_fifo_within_a_lane(adapter, home):
    """Wall-clock ≈ max(lane durations), not sum: the lanes run at the same time.

    One lane at a time is timed FIRST (the same 3 slow turns, no other lane), so
    the two-lane number is compared against the drainer's real per-turn cost --
    turn overhead included -- rather than a hard-coded sleep budget.
    """
    from gateway.platforms import api_server_bot_delivery as drain

    in_flight = 0
    peak = 0

    async def _run_agent(conversation_history=None, **kwargs):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        adapter.calls.append(kwargs)
        await asyncio.sleep(SLOW_TURN_SECONDS)
        in_flight -= 1
        return {"final_response": "pong"}, {}

    adapter._run_agent = _run_agent

    solo: dict[str, float] = {}
    for profile in ("alpha", "beta"):
        _queue_lane(home, profile, 3, tag="solo")
        started = time.monotonic()
        assert asyncio.run(drain.drain_once(adapter, home)) == 3
        solo[profile] = time.monotonic() - started

    peak = 0
    adapter.calls.clear()
    for profile in ("alpha", "beta"):
        _queue_lane(home, profile, 3, tag="both")

    started = time.monotonic()
    drained = asyncio.run(drain.drain_once(adapter, home))
    elapsed = time.monotonic() - started

    assert drained == 6, "both lanes' whole backlogs drain in one pass"
    assert _per_lane_calls(adapter.calls) == {
        "alpha": ["alpha-0", "alpha-1", "alpha-2"],
        "beta": ["beta-0", "beta-1", "beta-2"],
    }, "FIFO order is preserved inside each lane"
    assert peak == 2, (
        f"lanes were drained one after the other: peak in-flight turns was {peak}"
    )
    one_at_a_time = solo["alpha"] + solo["beta"]
    assert elapsed < 0.75 * one_at_a_time, (
        f"{elapsed:.2f}s for both lanes against {one_at_a_time:.2f}s one at a time: "
        f"the two lanes were drained sequentially, not concurrently"
    )
    for profile in ("alpha", "beta"):
        assert q.queue_depth(_lane_home(home, profile), profile) == 0


def test_held_slot_defers_only_that_lane(adapter, home, caplog):
    """A HELD slot skips its own lane; the free lanes drain in the same pass."""
    from gateway.platforms import api_server_bot_delivery as drain
    from tools.bot_mode_probe import _hermes_root
    from tools.bot_relay import acquire_turn_lock

    _queue_lane(home, "lane-a-held", 1)
    _queue_lane(home, "lane-m-free", 1)
    _queue_lane(home, "lane-z-free", 1)

    async def _run_agent(conversation_history=None, **kwargs):
        adapter.calls.append(kwargs)
        return {"final_response": "pong"}, {}

    adapter._run_agent = _run_agent

    caplog.set_level(logging.INFO, logger="tools.bot_delivery_queue")
    with acquire_turn_lock(_hermes_root(home), "lane-a-held", timeout_seconds=0):
        drained = asyncio.run(drain.drain_once(adapter, home))

    assert drained == 2, "the two free lanes must drain despite the held lane"
    assert sorted(_per_lane_calls(adapter.calls)) == ["lane-m-free", "lane-z-free"]
    # The held lane's delivery is untouched, still queued for the slot holder.
    assert q.queue_depth(_lane_home(home, "lane-a-held"), "lane-a-held") == 1
    deferred = [
        rec.getMessage() for rec in caplog.records if "drain_deferred" in rec.getMessage()
    ]
    assert len(deferred) == 1, f"exactly one lane defers: {deferred}"
    assert "target=lane-a-held" in deferred[0] and "reason=slot_held" in deferred[0]


def test_wedged_lane_does_not_stall_a_free_lane(adapter, home):
    """A lane stuck mid-turn must not hold a free lane's delivery for the whole turn."""
    from gateway.platforms import api_server_bot_delivery as drain

    _queue_lane(home, "lane-a-wedged", 1)
    _queue_lane(home, "lane-z-free", 1)

    async def _scenario():
        wedged_release = asyncio.Event()
        free_done = asyncio.Event()

        async def _run_agent(conversation_history=None, **kwargs):
            adapter.calls.append(kwargs)
            if kwargs.get("session_id") == "sess-lane-a-wedged":
                await wedged_release.wait()
            else:
                free_done.set()
            return {"final_response": "pong"}, {}

        adapter._run_agent = _run_agent
        task = asyncio.ensure_future(drain.drain_once(adapter, home))
        try:
            await asyncio.wait_for(free_done.wait(), timeout=1.0)
            served = True
        except asyncio.TimeoutError:
            served = False
        wedged_release.set()
        return served, await task

    served, drained = asyncio.run(_scenario())

    assert served, "a free lane's delivery waited on the wedged lane's turn"
    assert drained == 2
    assert q.queue_depth(_lane_home(home, "lane-a-wedged"), "lane-a-wedged") == 0
    assert q.queue_depth(_lane_home(home, "lane-z-free"), "lane-z-free") == 0


def test_cancelling_the_sweep_cancels_every_lane_turn(adapter, home):
    """Gateway shutdown: the gather propagates the cancel, leaving no turn task."""
    from gateway.platforms import api_server_bot_delivery as drain

    for profile in ("alpha", "beta"):
        _queue_lane(home, profile, 1)

    started_lanes: set[str] = set()
    both_started = asyncio.Event()
    cancelled: set[str] = set()

    async def _scenario():
        async def _run_agent(conversation_history=None, **kwargs):
            session_id = str(kwargs.get("session_id"))
            adapter.calls.append(kwargs)
            started_lanes.add(session_id)
            if len(started_lanes) >= 2:
                both_started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.add(session_id)
                raise
            return {"final_response": "pong"}, {}

        adapter._run_agent = _run_agent
        task = asyncio.ensure_future(drain.drain_once(adapter, home))
        await asyncio.wait_for(both_started.wait(), timeout=2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        leaked = [
            t
            for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()
        ]
        return leaked

    leaked = asyncio.run(_scenario())

    assert cancelled == {"sess-alpha", "sess-beta"}, "every lane's turn was cancelled"
    assert leaked == [], f"turn tasks outlived the sweep: {leaked}"
