"""Unit tests for the custom provider profile's reasoning wiring.

``provider=custom`` covers any OpenAI-compatible endpoint the user points
Hermes at — local Ollama, vLLM, llama.cpp, and hosted reasoning APIs like
GLM-5.2 on Volcengine ARK. Before #57601's salvage, ``CustomProfile`` emitted
nothing when reasoning was *enabled*, so a configured ``reasoning_effort``
was silently dropped for every custom endpoint.

These tests pin the wire-shape contract:
    - disabled on Ollama  → extra_body.think = False + reasoning_effort=none
    - disabled elsewhere  → reasoning_effort=none, no think (strict APIs 422)
    - disabled on llama.cpp → NO reasoning_effort at all: that wire has no ``none`` level
                          (default|minimal|low|medium|high|xhigh|max) and silently discards
                          an unknown value, applying the chat template's own (expensive)
                          default instead — fleet-wide, with no warning anywhere
    - enabled + effort    → top-level reasoning_effort (native OpenAI-compat
                          format GLM/ARK expect), passed through verbatim
                          including ``max``/``xhigh``
    - enabled + no effort → nothing emitted (endpoint's server default applies)
    - ollama_num_ctx      → extra_body.options.num_ctx, orthogonal to reasoning
"""

from __future__ import annotations

import importlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

# What llama-server's /props answers (abridged, b12588 on 127.0.0.1:7777): build_info is the
# field hermes_cli.local_runtime.detect fingerprints to tell llama.cpp from every other
# OpenAI-compatible wire.
LLAMACPP_PROPS = {
    "build_info": "b12588-2a3beff7",
    "model_alias": "yaan-main",
    "model_ftype": "Q8_0",
    "default_generation_settings": {"n_ctx": 65536},
}
VLLM_PROPS = {"model": "Qwen/Qwen3-8B", "max_model_len": 32768}


@pytest.fixture
def custom_profile():
    """Resolve the registered custom profile via the global registry.

    Importing ``model_tools`` triggers plugin discovery, which registers the
    ``custom`` profile. Going through ``get_provider_profile`` keeps the test
    honest — if the registered class is ever downgraded to a plain
    ``ProviderProfile``, the assertions below collapse.
    """
    import model_tools
    import providers

    profile = providers.get_provider_profile("custom")
    assert profile is not None, "custom provider profile must be registered"
    return profile


class TestCustomReasoningWireShape:
    """``build_api_kwargs_extras`` produces the correct wire format."""

    def test_no_reasoning_config_emits_nothing(self, custom_profile):
        """Unset reasoning → omit everything so the endpoint's default applies."""
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config=None, model="glm-5.2"
        )
        assert eb == {}
        assert tl == {}

    def test_disabled_sends_think_false(self, custom_profile):
        """enabled=False on an Ollama URL → reasoning_effort='none' + think=False.

        Both fields are required on Ollama: /v1/chat/completions silently
        ignores extra_body.think (only /api/chat honours it — ollama#14820)
        but respects top-level reasoning_effort (#25758). think=False stays
        for proxies and the native /api/chat path.
        """
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False},
            model="qwen3",
            base_url="http://127.0.0.1:11434/v1",
        )
        assert eb == {"think": False}
        assert tl == {"reasoning_effort": "none"}

    def test_effort_none_sends_think_false(self, custom_profile):
        """effort='none' is the disable alias → same dual emission on Ollama."""
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "none"},
            model="qwen3",
            base_url="http://localhost:11434/v1",
        )
        assert eb == {"think": False}
        assert tl == {"reasoning_effort": "none"}

    def test_disabled_omits_think_on_mistral(self, custom_profile):
        """Strict OpenAI-compat hosts forbid extra ``think`` (HTTP 422)."""
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "none"},
            model="mistral-small-latest",
            base_url="https://api.mistral.ai/v1",
        )
        assert "think" not in eb
        assert tl == {"reasoning_effort": "none"}

    def test_disabled_omits_think_without_base_url(self, custom_profile):
        """Unknown custom endpoint — do not send the Ollama-only flag."""
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False}, model="glm-5.2"
        )
        assert "think" not in eb
        assert tl == {"reasoning_effort": "none"}

    @pytest.mark.parametrize(
        "base_url",
        [
            "http://127.0.0.1:8080/v1",
            "http://localhost:1234/v1",
            "https://api.groq.com/openai/v1",
        ],
    )
    def test_disabled_omits_think_on_non_ollama_relays(self, custom_profile, base_url):
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"effort": "none"},
            model="llama3",
            base_url=base_url,
        )
        assert "think" not in eb
        assert tl == {"reasoning_effort": "none"}

    def test_disabled_sends_think_false_on_ollama_cloud_host(self, custom_profile):
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False},
            model="qwen3",
            base_url="https://ollama.com/v1",
        )
        assert eb == {"think": False}
        assert tl == {"reasoning_effort": "none"}

    @pytest.mark.parametrize(
        "base_url",
        [
            "http://myhost:99999/v1",  # out-of-range port: OpenAI client accepts it
            "http://localhost:80a/v1",  # non-integer port
            "http://localhost:11434./v1",  # trailing-dot port
        ],
    )
    def test_malformed_port_does_not_raise(self, custom_profile, base_url):
        """Malformed ports must not raise — urlparse's ``port`` is ValueError-happy.

        The OpenAI client accepts ``http://myhost:99999/v1`` at construction
        (only httpx fails later), so these URLs reach ``build_api_kwargs_extras``
        in production. The heuristic must treat them as non-Ollama rather than
        killing the kwargs build.
        """
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False},
            model="qwen3",
            base_url=base_url,
        )
        assert "think" not in eb
        assert tl == {"reasoning_effort": "none"}

    @pytest.mark.parametrize(
        "effort", ["minimal", "low", "medium", "high", "xhigh", "max"]
    )
    def test_enabled_effort_goes_top_level(self, custom_profile, effort):
        """enabled + effort → TOP-LEVEL reasoning_effort, passed through verbatim.

        GLM-5.2/ARK and OpenAI-compatible reasoning APIs read reasoning_effort
        as a top-level string, not nested in extra_body. ``max`` is GLM's
        native deep-reasoning level and must survive.
        """
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": effort}, model="glm-5.2"
        )
        assert tl == {"reasoning_effort": effort}
        assert "reasoning_effort" not in eb
        assert "think" not in eb


    def test_does_not_force_think_true_on_enable(self, custom_profile):
        """We must never send think=True on enable — it's Ollama-only and
        would 400 on GLM/vLLM endpoints that don't recognize it."""
        eb, _ = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "high"}, model="glm-5.2"
        )
        assert eb.get("think") is not True

    @pytest.mark.parametrize(
        "reasoning_config, expected",
        [({"enabled": True, "effort": "high"}, "default"), ({"enabled": False, "effort": "medium"}, "none")],
    )
    def test_groq_host_clamps_effort_to_groq_vocabulary(self, custom_profile, reasoning_config, expected):
        """api.groq.com accepts top-level reasoning_effort only as 'none' / 'default' (#75089).

        Drives the main transport so the clamp is proven where production reads it.
        """
        from agent.transports.chat_completions import ChatCompletionsTransport

        kwargs = ChatCompletionsTransport().build_kwargs(
            model="qwen/qwen3.6-27b", messages=[{"role": "user", "content": "ping"}], tools=None,
            provider_profile=custom_profile, reasoning_config=reasoning_config,
            base_url="https://api.groq.com/openai/v1", provider_name="custom",
        )
        assert kwargs["reasoning_effort"] == expected
        assert "think" not in kwargs.get("extra_body", {}) and "reasoning" not in kwargs.get("extra_body", {})


class TestCustomReasoningWithNumCtx:
    """Ollama num_ctx and reasoning are independent and compose."""

    def test_num_ctx_alone(self, custom_profile):
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config=None, ollama_num_ctx=8192, model="qwen3"
        )
        assert eb == {"options": {"num_ctx": 8192}}
        assert tl == {}


class TestCustomResponsesEffortVocabulary:
    """The Responses transport honours a custom endpoint's configured ``max`` (#114249).

    A custom relay's models are not on the OpenAI per-model ladder; without the
    profile declaration the Responses transport clamped a configured ``max`` to
    ``xhigh`` while chat-completions on the same provider forwarded ``max``.
    """

    def test_named_relay_keeps_configured_max_where_chat_completions_does(self, custom_profile):
        from agent.transports.codex import _resolve_reasoning

        reasoning = {"enabled": True, "effort": "max"}
        _, top_level = custom_profile.build_api_kwargs_extras(
            reasoning_config=reasoning, base_url="https://relay.example/v1"
        )
        effort, enabled = _resolve_reasoning(
            "deepseek-flash",
            {"provider": "custom:relay", "base_url": "https://relay.example/v1", "reasoning_config": reasoning},
        )
        assert (effort, enabled) == (top_level["reasoning_effort"], True) == ("max", True)

    def test_custom_entry_at_the_official_openai_origin_keeps_the_per_model_ladder(self):
        """api.openai.com's vocabulary is known per model; the custom declaration must not
        put ``max`` on the wire for a slug OpenAI rejects it on."""
        from agent.transports.codex import _resolve_reasoning

        effort, enabled = _resolve_reasoning(
            "gpt-5.2",
            {
                "provider": "custom:oai",
                "base_url": "https://api.openai.com/v1",
                "reasoning_config": {"enabled": True, "effort": "max"},
            },
        )
        assert (effort, enabled) == ("xhigh", True)


@pytest.fixture
def props_server():
    """Local /props responder; the returned factory yields ``http://127.0.0.1:<port>``.

    Real HTTP, not a mock: the wire predicate is a fingerprint of what a server answers, so a
    stubbed predicate would prove only that the test can return a boolean.
    """
    servers = []

    def _serve(body=None, status: int = 200) -> str:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's name
                if self.path.split("?")[0] != "/props":
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(status)
                if status == 200:
                    raw = json.dumps(body).encode()
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                else:
                    self.end_headers()

            def log_message(self, format, *args):  # noqa: A002 - the parent's own signature
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    yield _serve
    for srv in servers:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def wire_module(custom_profile):
    """The registered profile's own module, with the fingerprint cache emptied around the test."""
    mod = importlib.import_module(custom_profile.__class__.__module__)
    reset = getattr(mod, "_reset_llamacpp_wire_cache", None)  # absent before this change
    if reset is not None:
        reset()
    yield mod
    if reset is not None:
        reset()


class TestLlamaCppWireOmitsReasoningEffort:
    """llama.cpp's wire has no ``none`` level — emitting it is silently discarded.

    Measured on b12588: ``"none"`` is dropped with no log line, and the chat template's 0.9
    default applies, so a seat configured with reasoning disabled pays the HIGHEST thinking
    cost while every log says nothing. The field must be absent, and the server's own
    ``--reasoning on|off|auto`` decides. ``none`` stays legal on vLLM/SGLang/OpenAI-compat,
    so this is a wire-shaped omission, never a blanket remap to a lower level.
    """

    def test_disabled_omits_the_key_on_a_llamacpp_wire(
        self, custom_profile, wire_module, props_server
    ):
        root = props_server(LLAMACPP_PROPS)
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False},
            model="yaan-main",
            base_url=f"{root}/v1",
        )
        assert "reasoning_effort" not in tl
        assert tl == {}
        # think is Ollama-native; llama.cpp is not Ollama and must not receive it.
        assert eb == {}

    def test_effort_none_omits_the_key_on_a_llamacpp_wire(
        self, custom_profile, wire_module, props_server
    ):
        """``effort: none`` is the other spelling of the disable path — same omission."""
        root = props_server(LLAMACPP_PROPS)
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "none"},
            model="yaan-main",
            base_url=f"{root}/v1",
        )
        assert "reasoning_effort" not in tl
        assert eb == {}

    def test_outgoing_payload_carries_no_reasoning_effort(
        self, custom_profile, wire_module, props_server
    ):
        """THE primary gate: the payload the chat-completions transport actually builds.

        For both disable spellings, on a proven llama.cpp wire, ``reasoning_effort`` is absent
        from the whole kwargs dict — top level and extra_body alike.
        """
        from agent.transports.chat_completions import ChatCompletionsTransport

        root = props_server(LLAMACPP_PROPS)
        for reasoning_config in ({"enabled": False}, {"enabled": True, "effort": "none"}):
            kwargs = ChatCompletionsTransport().build_kwargs(
                model="yaan-main",
                messages=[{"role": "user", "content": "ping"}],
                tools=None,
                provider_profile=custom_profile,
                reasoning_config=reasoning_config,
                base_url=f"{root}/v1",
                provider_name="custom",
            )
            assert "reasoning_effort" not in kwargs, reasoning_config
            assert "reasoning_effort" not in (kwargs.get("extra_body") or {}), reasoning_config

    @pytest.mark.parametrize(
        "body, status",
        [
            (VLLM_PROPS, 200),  # vLLM answers /props without build_info
            (None, 404),  # SGLang / LM Studio have no /props at all
        ],
    )
    def test_other_openai_compat_wires_still_carry_none(
        self, custom_profile, wire_module, props_server, body, status
    ):
        """vLLM/SGLang/OpenAI-compat accept ``none`` — the omission must not leak onto them."""
        root = props_server(body, status)
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False},
            model="Qwen3-8B",
            base_url=f"{root}/v1",
        )
        assert tl == {"reasoning_effort": "none"}
        assert eb == {}

    def test_probe_verdict_is_cached_per_root(
        self, custom_profile, wire_module, props_server, monkeypatch
    ):
        """One fingerprint per server per TTL — the request path must not re-probe each call."""
        root = props_server(LLAMACPP_PROPS)
        calls = []
        real = wire_module._props_says_llamacpp

        def _counting(root_url):
            calls.append(root_url)
            return real(root_url)

        monkeypatch.setattr(wire_module, "_props_says_llamacpp", _counting)
        for _ in range(3):
            _, tl = custom_profile.build_api_kwargs_extras(
                reasoning_config={"enabled": False}, model="m", base_url=f"{root}/v1"
            )
            assert tl == {}
        assert calls == [root]

    def test_non_loopback_endpoint_is_never_probed(
        self, custom_profile, wire_module, monkeypatch
    ):
        """A remote relay is not assumed to be llama.cpp, and we never probe it."""

        def _boom(root):
            raise AssertionError(f"probed a non-loopback endpoint: {root}")

        monkeypatch.setattr(wire_module, "_props_says_llamacpp", _boom)
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False},
            model="glm-5.2",
            base_url="https://relay.example.com/v1",
        )
        assert tl == {"reasoning_effort": "none"}

    def test_unreachable_loopback_endpoint_keeps_previous_behaviour(
        self, custom_profile, wire_module
    ):
        """A closed port yields no fingerprint → ``none`` is still emitted (no regression)."""
        import socket

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False},
            model="m",
            base_url=f"http://127.0.0.1:{port}/v1",
        )
        assert tl == {"reasoning_effort": "none"}

    def test_malformed_loopback_port_does_not_raise(self, custom_profile, wire_module):
        """urlparse's ``port`` raises on ``:99999`` — the predicate must treat it as unknown."""
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False},
            model="m",
            base_url="http://127.0.0.1:99999/v1",
        )
        assert tl == {"reasoning_effort": "none"}

    @pytest.mark.parametrize(
        "effort", ["minimal", "low", "medium", "high", "xhigh", "max"]
    )
    def test_graded_effort_is_untouched_on_a_llamacpp_wire(
        self, custom_profile, wire_module, props_server, effort
    ):
        """Only the disable path is wire-shaped; a configured level still goes out verbatim."""
        root = props_server(LLAMACPP_PROPS)
        _, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": effort},
            model="yaan-main",
            base_url=f"{root}/v1",
        )
        assert tl == {"reasoning_effort": effort}
