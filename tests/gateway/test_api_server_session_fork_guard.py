"""A bot's canonical session id must not fork across profile stores.

POST /api/sessions on a multiplexed gateway writes the row into the request-scoped home.
When the same id already exists in a DIFFERENT served profile's state.db, the create must
refuse (409) rather than silently split one canonical chat into two divergent lineages.

Live repro: a bot's "Bot Chat" id existed in ~/.hermes/profiles/<bot>/state.db AND in the
default home ~/.hermes/state.db (source='desktop', thousands of messages, ~1% content overlap)
— a fork, not a mirror. Root cause: a session-create/resolve request that lacked the
/p/<profile>/ prefix materialised the id into the default home.
"""

from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB


@pytest.fixture
def multiplex_adapter(tmp_path, monkeypatch):
    """An APIServerAdapter whose request-scoped home is tmp_path (the DEFAULT home), serving
    one foreign profile 'worker' -> tmp_path/profiles/worker."""
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter._session_db = SessionDB(tmp_path / "state.db")
    adapter.gateway_runner = SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=True, multiplex_profile_allowlist=None)
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex, profile_allowlist: [("worker", None)],
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir",
        lambda name: str(tmp_path / "profiles" / name),
    )
    # The request's own home == tmp_path, matching adapter._session_db.
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: str(tmp_path))
    try:
        yield adapter
    finally:
        close = getattr(adapter._session_db, "close", None)
        if callable(close):
            close()


@pytest.fixture
def foreign_db(tmp_path):
    """The foreign profile's store (tmp_path/profiles/worker/state.db)."""
    home = tmp_path / "profiles" / "worker"
    home.mkdir(parents=True, exist_ok=True)
    db = SessionDB(home / "state.db")
    try:
        yield db
    finally:
        close = getattr(db, "close", None)
        if callable(close):
            close()


def _session_app(adapter):
    app = web.Application()
    app.router.add_post("/api/sessions", adapter._handle_create_session)
    return app


@pytest.mark.asyncio
async def test_create_session_refuses_id_owned_by_another_profile(multiplex_adapter, foreign_db):
    # A bot's canonical "Bot Chat" already lives in the foreign profile's store.
    foreign_db.create_session("api_0000000000_deadbeef", "api_server")

    app = _session_app(multiplex_adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            "/api/sessions",
            json={"id": "api_0000000000_deadbeef", "source": "desktop", "title": "Bot Chat"},
        )
        assert resp.status == 409, await resp.text()
        body = await resp.json()
        assert body["error"]["code"] == "session_exists_foreign_profile"

    # The default home must NOT have forked a second row under the same id.
    assert multiplex_adapter._session_db.get_session("api_0000000000_deadbeef") is None


@pytest.mark.asyncio
async def test_create_session_allows_fresh_id_on_multiplexed_gateway(multiplex_adapter, foreign_db):
    foreign_db.create_session("owned-by-worker", "api_server")

    app = _session_app(multiplex_adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            "/api/sessions",
            json={"id": "fresh-session", "source": "desktop"},
        )
        assert resp.status == 201, await resp.text()

    assert multiplex_adapter._session_db.get_session("fresh-session") is not None
