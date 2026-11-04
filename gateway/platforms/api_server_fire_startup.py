"""Startup gate for the Chronos fire webhook (``POST /api/cron/fire``).

A backend that stops the guest on sleep wakes the agent FOR the fire, so the fire reaches the webhook in
the first second of a cold boot. The api_server adapter listens before the gateway has published any
adapter into ``runner.adapters`` (an adapter is registered only after its ``connect()`` returns), so a
fire taken then ran its job with no live adapters and a relay-fronted platform failed with "has no live
gateway transport" (the result was lost). Wait for the gateway to finish starting instead.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any, Optional

from aiohttp import web

# How long a fire may spend in the handler (token verify included) before a still-starting gateway is
# refused as retryable. Kept under the dashboard forwarder's 10s timeout: past it NAS sees a 503 while
# this handler still runs the job, and the retry would run it twice.
FIRE_STARTUP_WAIT_SECONDS = 7.0
_POLL_SECONDS = 0.1
# Stable code for the still-starting half of this refusal. A drain says ``gateway_draining``
# (gateway.drain_control.DRAIN_REFUSAL_CODE, with the declared window's end); this one must not
# borrow that code, because nothing is draining and there is no window end to name.
GATEWAY_STARTING_CODE = "gateway_starting"


def runner_is_draining(runner: Any) -> bool:
    """Whether ``runner`` refuses new work (an in-process drain, or one an external supervisor started)."""
    return bool(getattr(runner, "_draining", False) or getattr(runner, "_external_drain_active", False))


async def refuse_until_started(runner: Any, job_id: str, *, received_at: float) -> Optional[web.Response]:
    """None once ``runner`` has finished starting (``_running``: every platform connect attempted and its
    adapters published), or when there is no runner to ask (a self-hosted api_server, a test double).
    Otherwise the retryable 503, worded like the dashboard's own "gateway unreachable" so NAS classifies
    it the same, carrying the drain refusal contract the api_server's own drain 503 carries
    (``gateway.drain_control.drain_refusal``): the stable code, and the expected end of the window when
    the drain declared one. ``Retry-After`` is that helper's — the window's remaining seconds, capped at
    the dashboard's 60s (``_CRON_FIRE_RETRY_AFTER_SECONDS``).

    ``received_at`` is the loop time the handler was entered: time already spent verifying the token
    counts against the budget."""
    if runner is None:
        return None
    loop = asyncio.get_running_loop()
    deadline = received_at + FIRE_STARTUP_WAIT_SECONDS
    while True:
        # Re-checked after every poll: the handler's drain check ran before this wait, and a gateway that
        # starts draining meanwhile (a restart keeps _running True; a stop mid-boot never sets it) must not
        # claim the fire.
        draining = runner_is_draining(runner)
        if not draining and getattr(runner, "_running", True):
            return None
        if draining or loop.time() >= deadline:
            from gateway.drain_control import DRAIN_RETRY_AFTER_MAX_SECONDS, drain_refusal
            if draining:
                fields, retry_after = drain_refusal()
            else:
                fields, retry_after = {"code": GATEWAY_STARTING_CODE}, int(DRAIN_RETRY_AFTER_MAX_SECONDS)
            return web.json_response(
                {"error": "gateway unreachable; retry", "job_id": job_id, **fields},
                status=503, headers={"Retry-After": str(retry_after)})
        await asyncio.sleep(_POLL_SECONDS)


async def live_adapters_once_started(
        api_adapter: Any, request: web.Request, job_id: str, *, received_at: float,
) -> tuple[Optional[web.Response], Any]:
    """``(refusal, adapters)`` for a fire: the startup gate's retryable 503, else the gateway's LIVE adapters
    (parity with the built-in ticker: E2EE / relay-fronted platforms have no native credential, so without
    them delivery fails). ``None`` adapters when there is no runner (a self-hosted api_server)."""
    runner = api_adapter.gateway_runner or request.app.get("gateway_runner")
    if runner is None:
        with suppress(Exception):
            from gateway.run import _gateway_runner_ref
            runner = _gateway_runner_ref()
    refusal = await refuse_until_started(runner, job_id, received_at=received_at)
    return refusal, (getattr(runner, "adapters", None) or None)
