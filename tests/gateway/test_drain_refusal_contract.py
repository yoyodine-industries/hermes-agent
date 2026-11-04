"""The drain refusal contract (t_81e506a1).

During a planned drain the gateway refuses new work with a 503. Both refusal paths — the api_server's
agent-admission 503 and the cron-fire startup gate's — must say the SAME thing: the stable
``gateway_draining`` code and, when the drain DECLARED a window, its expected end, so a caller backs
off until the gateway is back instead of retrying blind. ``Retry-After`` is that window's remaining
seconds (capped at the house 60s), never a hand-written constant that disagrees with the body.
"""

import asyncio
import json
from datetime import datetime, timedelta, UTC
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway import drain_control as dc
from gateway.config import PlatformConfig
from gateway.platforms import api_server_fire_startup as fire_startup
from gateway.platforms.api_server import APIServerAdapter


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolate the drain marker: a stray one from the host must never shape a refusal here."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def _draining_runner():
    return SimpleNamespace(_draining=True, _external_drain_active=False)


def _api_app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    return app


async def _api_drain_refusal(runner):
    """``(response, body)`` from the api_server admission 503, over real HTTP."""
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    with patch("gateway.run._gateway_runner_ref", lambda: runner):
        async with TestClient(TestServer(_api_app(adapter))) as client:
            response = await client.post("/v1/chat/completions", json={})
            return response, await response.json()


async def _fire_drain_refusal(runner, job_id="job-1"):
    """``(response, body)`` from the cron-fire gate's 503 (the second refusal path)."""
    response = await fire_startup.refuse_until_started(
        runner, job_id, received_at=asyncio.get_running_loop().time())
    assert response is not None, "a draining runner must never admit a fire"
    return response, json.loads(response.body.decode())


# ---------------------------------------------------------------------------
# The marker declares the window; the refusals quote it, never invent it
# ---------------------------------------------------------------------------


class TestDeclaredWindow:
    def test_marker_records_the_declared_end_and_nothing_when_absent(self, home):
        payload = dc.write_drain_request(principal="nas", window_seconds=210)
        declared_end = datetime.fromisoformat(payload["expected_end_at"])
        assert abs((declared_end - datetime.now(UTC)) - timedelta(seconds=210)) < timedelta(seconds=5)

        dc.clear_drain_request()
        assert "expected_end_at" not in dc.write_drain_request(principal="nas")
        assert "expected_end_at" not in dc.write_drain_request(principal="nas", window_seconds=0)
        assert "expected_end_at" not in dc.write_drain_request(principal="nas", window_seconds="soon")

    def test_a_window_longer_than_the_marker_max_age_is_bounded_by_it(self, home):
        # The marker stops being honoured at max-age; promising a later end would be a lie.
        payload = dc.write_drain_request(principal="nas", window_seconds=dc.DRAIN_REQUEST_MAX_AGE_SECONDS * 10)
        declared_end = datetime.fromisoformat(payload["expected_end_at"])
        assert abs((declared_end - datetime.now(UTC)) - timedelta(
            seconds=dc.DRAIN_REQUEST_MAX_AGE_SECONDS)) < timedelta(seconds=5)

    @pytest.mark.asyncio
    async def test_api_refusal_names_the_end_and_its_retry_after_agrees(self, home):
        declared_end = dc.write_drain_request(principal="nas", window_seconds=30)["expected_end_at"]

        response, body = await _api_drain_refusal(_draining_runner())

        assert response.status == 503
        assert body["code"] == "gateway_draining"
        assert body["error"]["code"] == "gateway_draining"  # the pre-existing OpenAI field survives
        assert datetime.fromisoformat(body["expected_end_at"]) == datetime.fromisoformat(declared_end)
        # Same window, same numbers: the header IS the body's remaining seconds here (under the cap).
        assert response.headers["Retry-After"] == str(body["seconds_remaining"])
        assert 1 <= body["seconds_remaining"] <= 30

    @pytest.mark.asyncio
    async def test_retry_after_is_capped_but_the_body_keeps_the_true_end(self, home):
        declared_end = dc.write_drain_request(principal="nas", window_seconds=210)["expected_end_at"]

        response, body = await _api_drain_refusal(_draining_runner())

        assert datetime.fromisoformat(body["expected_end_at"]) == datetime.fromisoformat(declared_end)
        assert 205 <= body["seconds_remaining"] <= 210
        assert response.headers["Retry-After"] == str(int(dc.DRAIN_RETRY_AFTER_MAX_SECONDS))

    def test_an_end_already_past_asks_the_caller_to_retry_now(self, home):
        dc.write_drain_request(principal="nas", window_seconds=60)
        past = datetime.now(UTC) - timedelta(seconds=5)
        fields, retry_after = dc.drain_refusal(now=past + timedelta(seconds=70))
        assert fields["seconds_remaining"] == 0 and retry_after == 1


class TestNoDeclaredEnd:
    @pytest.mark.asyncio
    async def test_api_refusal_keeps_the_code_and_invents_no_time(self, home):
        dc.write_drain_request(principal="nas")  # a drain with no declared end

        response, body = await _api_drain_refusal(_draining_runner())

        assert response.status == 503
        assert body["code"] == "gateway_draining"
        assert "expected_end_at" not in body and "seconds_remaining" not in body
        assert response.headers["Retry-After"] == str(int(dc.DRAIN_RETRY_AFTER_MAX_SECONDS))

    @pytest.mark.asyncio
    async def test_fire_refusal_keeps_the_code_and_invents_no_time(self, home):
        dc.write_drain_request(principal="nas")

        response, body = await _fire_drain_refusal(_draining_runner())

        assert response.status == 503
        assert body["code"] == "gateway_draining"
        assert "expected_end_at" not in body and "seconds_remaining" not in body
        assert body["error"] == "gateway unreachable; retry"  # NAS's classification string
        assert response.headers["Retry-After"] == str(int(dc.DRAIN_RETRY_AFTER_MAX_SECONDS))


class TestBothPathsAgree:
    @pytest.mark.asyncio
    async def test_draining_api_and_cron_fire_refusals_are_the_same_contract(self, home):
        dc.write_drain_request(principal="nas", window_seconds=210)
        runner = _draining_runner()

        api_response, api_body = await _api_drain_refusal(runner)
        fire_response, fire_body = await _fire_drain_refusal(runner)

        assert api_response.status == fire_response.status == 503
        assert api_body["code"] == fire_body["code"] == "gateway_draining"
        assert api_body["expected_end_at"] == fire_body["expected_end_at"]
        assert abs(api_body["seconds_remaining"] - fire_body["seconds_remaining"]) <= 1
        assert api_response.headers["Retry-After"] == fire_response.headers["Retry-After"]

    @pytest.mark.asyncio
    async def test_a_still_starting_gateway_is_not_reported_as_a_drain(self, home, monkeypatch):
        """The gate's other half: nothing is draining, so no drain code and no window end."""
        monkeypatch.setattr(fire_startup, "FIRE_STARTUP_WAIT_SECONDS", 0.05)
        runner = SimpleNamespace(_draining=False, _external_drain_active=False, _running=False)

        _response, body = await _fire_drain_refusal(runner, job_id="job-1")

        assert body["code"] == fire_startup.GATEWAY_STARTING_CODE
        assert "expected_end_at" not in body and "seconds_remaining" not in body
