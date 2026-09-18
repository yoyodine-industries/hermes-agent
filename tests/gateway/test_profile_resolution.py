"""Tests for GatewayRunner._resolve_profile_home_for_source — profile resolution logic."""

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.session import SessionSource, build_session_key
from gateway.run import GatewayRunner
from gateway.profile_routing import ProfileRoute, ProfileRouteRejected
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.event import MessageEvent


@pytest.fixture
def mock_runner():
    """Create a minimal mock GatewayRunner with the methods we need."""
    runner = MagicMock(spec=GatewayRunner)
    runner.config = MagicMock(profile_routes=[])
    # Bind the actual methods to the mock
    runner._profile_name_for_source = GatewayRunner._profile_name_for_source.__get__(runner)
    runner._resolve_profile_home_for_source = GatewayRunner._resolve_profile_home_for_source.__get__(runner)
    # _handle_message's ingress gates (profile route rejection) live in this helper.
    runner._hm_admit_event = GatewayRunner._hm_admit_event.__get__(runner)
    return runner


@pytest.fixture
def discord_source():
    """Create a basic Discord SessionSource for testing."""
    return SessionSource(
        platform=MagicMock(value="discord"),
        chat_id="123456",
        guild_id="789",
        thread_id=None,
        parent_chat_id=None,
    )


@pytest.fixture
def telegram_source():
    """Create a basic Telegram SessionSource for testing.

    Telegram (like Slack/Feishu/etc.) has no ``guild_id`` — only ``chat_id``.
    Used to prove profile routing is platform-generic, not Discord-only.
    """
    return SessionSource(
        platform=MagicMock(value="telegram"),
        chat_id="-1001234567890",
        guild_id=None,
        thread_id=None,
        parent_chat_id=None,
    )


class TestResolutionOrder:
    """Tests that profile resolution follows the correct priority order."""
    
    def test_source_profile_wins_over_routing(self, mock_runner, discord_source):
        """source.profile should be used even if routing would match."""
        discord_source.profile = "from-source"
        
        with patch("hermes_cli.profiles.get_active_profile_name", return_value="active"):
            with patch("hermes_cli.profiles.get_profile_dir") as mock_get_dir:
                with patch("hermes_cli.profiles.profile_exists", return_value=True):
                    mock_get_dir.return_value = Path("/hermes/profiles/from-source")
                    result = mock_runner._resolve_profile_home_for_source(discord_source)
                    
                    assert result == Path("/hermes/profiles/from-source")
                    mock_get_dir.assert_called_once_with("from-source")
    
    
    


class TestMissingProfileWarning:
    """Tests for warning when a profile doesn't exist on disk."""
    
    def test_nonexistent_profile_warning(self, mock_runner, discord_source, caplog):
        """When source.profile points to a nonexistent profile, log a WARNING."""
        discord_source.profile = "nonexistent"
        
        with patch("hermes_cli.profiles.get_active_profile_name", return_value="active"):
            with patch("hermes_cli.profiles.get_profile_dir") as mock_get_dir:
                mock_get_dir.return_value = Path("/hermes/profiles/nonexistent")
                with patch("hermes_cli.profiles.profile_exists", return_value=False):
                    with patch("hermes_constants.get_hermes_home", return_value=Path("/hermes")):
                        with caplog.at_level(logging.WARNING):
                            result = mock_runner._resolve_profile_home_for_source(discord_source)
                            
                            # Should fall back to global HERMES_HOME
                            assert result == Path("/hermes")
                            
                            # Should have logged a warning
                            assert len(caplog.records) == 1
                            assert caplog.records[0].levelname == "WARNING"
                            assert "nonexistent" in caplog.records[0].message
                            assert "does not exist" in caplog.records[0].message
                            assert "discord" in caplog.records[0].message
                            assert "123456" in caplog.records[0].message
    
    
    


class TestExceptionHandling:
    """Tests for exception handling in profile resolution."""
    
    def test_get_profile_dir_exception_logs_warning(self, mock_runner, discord_source, caplog):
        """When get_profile_dir raises an exception, log a WARNING with context."""
        discord_source.profile = "bad-profile"
        
        with patch("hermes_cli.profiles.get_active_profile_name", return_value="active"):
            with patch("hermes_cli.profiles.get_profile_dir", side_effect=ValueError("Invalid profile name")):
                with patch("hermes_constants.get_hermes_home", return_value=Path("/hermes")):
                    with caplog.at_level(logging.WARNING):
                        result = mock_runner._resolve_profile_home_for_source(discord_source)
                        
                        # Should fall back to global HERMES_HOME
                        assert result == Path("/hermes")
                        
                        # Should have logged a warning with exception info
                        assert len(caplog.records) == 1
                        assert caplog.records[0].levelname == "WARNING"
                        assert "bad-profile" in caplog.records[0].message
                        assert "Failed to resolve profile directory" in caplog.records[0].message
    


class TestRoutingConsultation:
    """Tests that _profile_name_for_source is consulted when source.profile is empty."""
    
    def test_routing_consulted_when_source_profile_empty(self, mock_runner, discord_source):
        """_profile_name_for_source should be called when source.profile is empty."""
        discord_source.profile = None
        
        with patch("hermes_cli.profiles.get_active_profile_name", return_value="active"):
            with patch("hermes_cli.profiles.get_profile_dir") as mock_get_dir:
                mock_get_dir.return_value = Path("/hermes/profiles/routed")
                
                mock_runner._profile_name_for_source = MagicMock(return_value="routed")
                
                mock_runner._resolve_profile_home_for_source(discord_source)
                
                # Should have called routing
                mock_runner._profile_name_for_source.assert_called_once_with(discord_source)
    


class TestNonDiscordProfileRouting:
    """Profile routing must be platform-generic, not Discord-only.

    Regression coverage for the ``gateway_runner`` injection gap: previously
    only Discord's adapter pre-declared ``gateway_runner``, so only Discord
    ever had ``build_source`` call ``_profile_name_for_source``. Telegram /
    Feishu / Slack / etc. silently fell through to the default profile. These
    tests pin the resolution half for a non-Discord platform (Telegram).
    """

    def test_telegram_route_resolves(self, mock_runner, telegram_source):
        """A configured Telegram route resolves to its profile via the real
        ``_profile_name_for_source`` (bound onto the mock runner)."""
        mock_runner.config.profile_routes = [
            ProfileRoute(name="tg", platform="telegram", profile="tg-profile",
                         chat_id="-1001234567890"),
        ]
        telegram_source.profile = None

        with patch(
            "hermes_cli.profiles.profiles_to_serve",
            return_value=[("default", Path("/profiles/default")),
                          ("tg-profile", Path("/profiles/tg-profile"))],
        ):
            assert mock_runner._profile_name_for_source(telegram_source) == "tg-profile"

    def test_route_to_served_profile_resolves(self, mock_runner, telegram_source):
        mock_runner.config.profile_routes = [
            ProfileRoute(
                name="worker-route",
                platform="telegram",
                profile="worker",
                chat_id="route-chat",
            )
        ]
        telegram_source.chat_id = "route-chat"

        with patch(
            "hermes_cli.profiles.profiles_to_serve",
            return_value=[("default", Path("/profiles/default")),
                          ("worker", Path("/profiles/worker"))],
        ) as enumerate_profiles:
            assert mock_runner._profile_name_for_source(telegram_source) == "worker"

        enumerate_profiles.assert_called_once_with(multiplex=True)

    def test_route_to_unserved_profile_rejects(self, mock_runner, telegram_source, caplog):
        mock_runner.config.profile_routes = [
            ProfileRoute(
                name="restricted-route",
                platform="telegram",
                profile="restricted",
                chat_id="route-chat",
            )
        ]
        telegram_source.chat_id = "route-chat"

        with patch(
            "hermes_cli.profiles.profiles_to_serve",
            return_value=[("default", Path("/profiles/default")),
                          ("worker", Path("/profiles/worker"))],
        ), caplog.at_level(logging.WARNING, logger="gateway.run"):
            with pytest.raises(ProfileRouteRejected):
                mock_runner._profile_name_for_source(telegram_source)

        assert "target profile 'restricted' is not served" in caplog.text

    def test_no_route_match_preserves_default_sentinel(self, mock_runner, telegram_source):
        mock_runner.config.profile_routes = [
            ProfileRoute(
                name="other-chat",
                platform="telegram",
                profile="worker",
                chat_id="different-chat",
            )
        ]
        telegram_source.chat_id = "route-chat"

        assert mock_runner._profile_name_for_source(telegram_source) is None
        adapter = _stub_adapter(Platform.TELEGRAM, mock_runner)
        source = adapter.build_source(chat_id="route-chat", chat_type="group")
        assert source.profile is None


class TestGatewayRunnerInjection:
    """``BasePlatformAdapter`` declares ``gateway_runner`` so the gateway's
    unconditional injection reaches every platform adapter — the foundation
    that makes the routing in TestNonDiscordProfileRouting reachable at runtime.
    """

    def test_base_adapter_declares_gateway_runner(self):
        from gateway.platforms.base import BasePlatformAdapter

        # Class-level attribute exists and defaults to None.
        assert hasattr(BasePlatformAdapter, "gateway_runner")
        assert BasePlatformAdapter.gateway_runner is None

    def test_factory_binds_every_adapter_to_runner(self, monkeypatch):
        """``_create_adapter`` binds the runner regardless of which branch
        built the adapter (plugin registry OR built-in if/elif) — every
        lifecycle path (startup, reconnect, secondary profiles) goes through
        it, so this is the single seam that makes profile_routes reachable
        for built-ins like Signal (#68332 / #70831)."""
        from gateway.config import PlatformConfig

        runner = object.__new__(GatewayRunner)
        adapter = MagicMock(spec=BasePlatformAdapter)
        monkeypatch.setattr(runner, "_instantiate_adapter", lambda platform, config: adapter)
        assert runner._create_adapter(Platform.SIGNAL, PlatformConfig(enabled=True)) is adapter
        assert adapter.gateway_runner is runner
        monkeypatch.setattr(runner, "_instantiate_adapter", lambda platform, config: None)
        assert runner._create_adapter(Platform.SIGNAL, PlatformConfig(enabled=True)) is None

    @pytest.mark.asyncio
    async def test_real_signal_factory_routes_inbound_group_event(self, monkeypatch):
        """A factory-built (built-in) Signal adapter resolves profile_routes
        for a real inbound envelope — fails on main where the Signal branch
        returned a bare ``SignalAdapter(config)`` with no runner."""
        from gateway.config import PlatformConfig

        group_id = "test-signal-route"
        monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", group_id)
        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(
            multiplex_profiles=True,
            profile_routes=[
                ProfileRoute(name="signal", platform="signal", profile="ops", chat_id=f"group:{group_id}"),
            ],
        )
        adapter = runner._create_adapter(
            Platform.SIGNAL,
            PlatformConfig(enabled=True, extra={"http_url": "http://127.0.0.1:18080", "account": "+15555550123"}),
        )
        assert adapter is not None and adapter.gateway_runner is runner

        captured = {}

        async def capture_event(event):
            captured["event"] = event

        adapter.handle_message = capture_event
        with patch(
            "hermes_cli.profiles.profiles_to_serve",
            return_value=[("default", Path("/profiles/default")), ("ops", Path("/profiles/ops"))],
        ):
            await adapter._handle_envelope({
                "envelope": {
                    "sourceNumber": "+15555550124",
                    "sourceName": "Test Operator",
                    "timestamp": 1700000000000,
                    "dataMessage": {
                        "message": "diagnose the cluster",
                        "groupInfo": {"groupId": group_id, "groupName": "US East 7"},
                    },
                },
            })
        source = captured["event"].source
        assert source.profile == "ops"
        assert build_session_key(source, profile=source.profile).startswith("agent:ops:")


# A concrete adapter we can instantiate without the full platform stack.
# ``build_source`` only reads ``self.platform`` and ``self.gateway_runner``, so a
# bare instance with those two attrs exercises the real BasePlatformAdapter
# method end-to-end. Clearing ``__abstractmethods__`` lets ``__new__`` bypass
# the ABC instantiation guard without stubbing connect/send/get_chat_info/…
class _StubAdapter(BasePlatformAdapter):
    pass


_StubAdapter.__abstractmethods__ = frozenset()  # type: ignore[attr-defined]


def _stub_adapter(platform: Platform, runner) -> "_StubAdapter":
    a = _StubAdapter.__new__(_StubAdapter)
    a.platform = platform
    a.gateway_runner = runner
    return a


class TestAdapterToSessionKeyIntegration:
    """Adapter -> ``source.profile`` -> session-key integration coverage.

    The review asked for integration coverage for Discord AND a non-Discord
    platform. These drive a concrete adapter's real ``build_source``
    (BasePlatformAdapter) with an injected ``gateway_runner``, assert the
    matched route's profile is stamped on the source, and that the resulting
    session key is profile-scoped (``agent:<profile>:...`` rather than the
    shared ``agent:main:...``). The Telegram case is the bug-#2 regression:
    pre-fix it never received ``gateway_runner`` and fell through to default.
    """

    @staticmethod
    def _routes():
        return [
            ProfileRoute(name="dc", platform="discord", profile="coder",
                         guild_id="111", chat_id="222"),
            ProfileRoute(name="tg", platform="telegram", profile="ops",
                         chat_id="-1001234567890"),
        ]

    def test_discord_adapter_stamps_profile_and_scopes_key(self, mock_runner):
        mock_runner.config.profile_routes = self._routes()
        adapter = _stub_adapter(Platform.DISCORD, mock_runner)

        with patch(
            "hermes_cli.profiles.profiles_to_serve",
            return_value=[("default", Path("/profiles/default")),
                          ("coder", Path("/profiles/coder"))],
        ):
            source = adapter.build_source(
                chat_id="222", chat_type="group", guild_id="111", user_id="u1",
            )
        assert source.profile == "coder"

        key = build_session_key(source, profile=source.profile)
        assert key.startswith("agent:coder:"), key
        # A default-profile key would land in agent:main — must differ.
        assert key != build_session_key(source, profile=None)

    @pytest.mark.asyncio
    async def test_adapter_drops_rejected_route_before_dispatch(self, mock_runner):
        mock_runner.config.profile_routes = [
            ProfileRoute(
                name="restricted-route",
                platform="telegram",
                profile="restricted",
                chat_id="route-chat",
            )
        ]
        adapter = _stub_adapter(Platform.TELEGRAM, mock_runner)

        with patch(
            "hermes_cli.profiles.profiles_to_serve",
            return_value=[("default", Path("/profiles/default"))],
        ):
            source = adapter.build_source(chat_id="route-chat", chat_type="group")

        assert source.profile is None
        assert source.profile_route_rejected is True
        roundtrip = SessionSource.from_dict(source.to_dict())
        assert roundtrip.profile_route_rejected is False
        assert roundtrip == source
        result = await GatewayRunner._handle_message(
            mock_runner,
            MessageEvent(text="discard me", source=source),
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_direct_source_is_rejected_at_shared_ingress(self, mock_runner):
        mock_runner.config.multiplex_profiles = True
        mock_runner.config.profile_routes = [
            ProfileRoute(
                name="restricted-route",
                platform="telegram",
                profile="restricted",
                chat_id="route-chat",
            )
        ]
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="route-chat")

        with patch(
            "hermes_cli.profiles.profiles_to_serve",
            return_value=[("default", Path("/profiles/default"))],
        ):
            result = await GatewayRunner._handle_message(
                mock_runner,
                MessageEvent(text="discard me", source=source),
            )

        assert result is None
        assert source.profile is None
        assert source.profile_route_rejected is True


class TestMultiplexGate:
    """``profile_routes`` only activates under ``gateway.multiplex_profiles``.

    Routing stamps ``source.profile``, which namespaces session/batch keys —
    but the profile-scoped agent run (``_profile_runtime_scope``) only engages
    when multiplexing is on. Without the gate, a configured route with
    multiplexing off would split batch/session keys into ``agent:<profile>``
    while the agent still served the turn from ``agent:main``'s home.
    """

    def test_routes_ignored_when_multiplex_off(self, mock_runner, discord_source):
        mock_runner.config.multiplex_profiles = False
        mock_runner.config.profile_routes = [
            ProfileRoute(name="dc", platform="discord", profile="coder",
                         guild_id="789", chat_id="123456"),
        ]
        discord_source.profile = None

        assert mock_runner._profile_name_for_source(discord_source) is None


class TestMissingProfileFallbackDiagnostics:
    """A missing profile name must not flood ``gateway.log`` from a hot caller.

    The fallback line is the only evidence of which record carries a stale name
    and who keeps re-resolving it, so the first occurrence names its provenance
    and later occurrences are counted instead of re-logged.
    """

    @pytest.fixture(autouse=True)
    def _clean_dedupe_state(self):
        from gateway.run_profile_fallback import reset_profile_fallback_log_state
        reset_profile_fallback_log_state()
        yield
        reset_profile_fallback_log_state()

    def test_lane_handle_resolves_to_its_serving_profile(
        self, mock_runner, discord_source, caplog, tmp_path,
    ):
        """A lane handle where a profile name belongs is aliased, not reported missing.

        The roster is a real file on disk (read through ``read_remote_roster``),
        so this covers the alias path end to end, not just the caller.
        """
        from tools.bot_relay import write_remote_roster
        write_remote_roster(tmp_path, [{
            "profile": "serving-profile", "handle": "retired-lane",
            "connection_id": "conn-retired",
        }])
        discord_source.profile = "retired-lane"

        with patch("tools.bot_mode_probe._hermes_root", return_value=Path(tmp_path)), \
             patch("hermes_cli.profiles.get_active_profile_name", return_value="active"), \
             patch("hermes_cli.profiles.get_profile_dir",
                   return_value=Path("/profiles/serving-profile")) as get_dir, \
             patch("hermes_cli.profiles.profile_exists",
                   side_effect=lambda name: name == "serving-profile"):
            with caplog.at_level(logging.INFO):
                for _ in range(3):
                    resolved = mock_runner._resolve_profile_home_for_source(discord_source)
                    assert resolved == Path("/profiles/serving-profile")

        assert [c.args for c in get_dir.call_args_list] == [("serving-profile",)] * 3
        # One alias notice for the hot caller — no per-resolution INFO spam, no warning.
        assert [r.levelname for r in caplog.records] == ["INFO"], [r.message for r in caplog.records]
        assert "retired-lane" in caplog.records[0].message
        assert "serving-profile" in caplog.records[0].message

    def test_repeat_missing_profile_logs_once_then_summarises(
        self, mock_runner, discord_source, caplog, tmp_path, monkeypatch,
    ):
        """Repeats are counted, not re-logged; the summary keeps the storm visible."""
        from gateway import run_profile_fallback
        monkeypatch.setattr(run_profile_fallback, "PROFILE_FALLBACK_REPORT_EVERY", 3)
        discord_source.profile = "ghost-lane"

        with patch("tools.bot_mode_probe._hermes_root", return_value=Path(tmp_path)), \
             patch("hermes_cli.profiles.get_active_profile_name", return_value="active"), \
             patch("hermes_cli.profiles.get_profile_dir",
                   return_value=Path("/hermes/profiles/ghost-lane")), \
             patch("hermes_cli.profiles.profile_exists", return_value=False), \
             patch("hermes_constants.get_hermes_home", return_value=Path("/hermes")):
            with caplog.at_level(logging.INFO):
                for _ in range(5):
                    assert mock_runner._resolve_profile_home_for_source(discord_source) == Path("/hermes")

        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        infos = [r for r in caplog.records if r.levelname == "INFO"]
        assert len(warnings) == 1, [r.message for r in warnings]
        message = warnings[0].message
        assert "ghost-lane" in message and "does not exist" in message
        # Provenance: the reader learns who keeps resolving and which record carries the name.
        assert "test_profile_resolution.py" in message   # real caller frame, not "unknown"
        assert "session_key='?" not in message           # real session key, not the fallback
        assert "explicit_profile='ghost-lane'" in message
        assert "owner_profile=None" in message
        # The 4th resolution reports the 3 suppressed repeats; the 5th is silent.
        assert len(infos) == 1, [r.message for r in infos]
        assert "3 repeated resolutions suppressed" in infos[0].message
