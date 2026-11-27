"""Custom / Ollama (local) provider profile: any endpoint registered as
provider="custom" (Ollama, vLLM, llama.cpp, GLM-5.2 on ARK, …)."""

import json
import threading
import time
import urllib.request
from typing import Any
from urllib.parse import urlparse

from agent.reasoning_effort import OPENAI_COMPAT_WIRE_EFFORTS, clamp_effort
from providers import register_provider
from providers.base import ProviderProfile
from utils import base_url_host_matches

# llama.cpp has no ``none`` level on its OpenAI-compatible wire: the accepted ladder is
# default|minimal|low|medium|high|xhigh|max, and a value outside it is DISCARDED SILENTLY
# (no log line, unlike a numeric effort) so the chat template's own default — usually the
# most expensive — applies to a seat that believes reasoning is off. On that wire the client
# must send no ``reasoning_effort`` at all and let ``--reasoning on|off|auto`` decide.
_LLAMACPP_PROP_TIMEOUT_S = 0.5
_LLAMACPP_PROP_TTL_S = 600.0  # a confirmed llama-server keeps this shape for its build's life
_LLAMACPP_MISS_TTL_S = 30.0  # re-check a non-answer: the server may still be booting
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_llamacpp_cache: dict[str, tuple[bool, float]] = {}
_llamacpp_probing: set[str] = set()
_llamacpp_lock = threading.Lock()


def _loopback_root(base_url: str | None) -> str | None:
    """``scheme://host:port`` root of a LOOPBACK base_url, else None.

    Only a local endpoint is fingerprinted: the probe is an extra request to the user's own
    server, and a remote OpenAI-compatible relay is never assumed to be llama.cpp.
    """
    raw = (base_url or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    scheme = (parsed.scheme or "http").lower()
    if scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower()
    if host not in _LOOPBACK_HOSTS:
        return None
    try:  # urlparse defers the ValueError-happy port read to the caller
        port = parsed.port
    except ValueError:
        return None
    return f"{scheme}://{f'[{host}]' if ':' in host else host}:{port or 8080}"


def _props_says_llamacpp(root: str) -> bool:
    """Fingerprint the wire by ``GET /props``: llama-server always reports a non-empty build_info.

    Same signal ``hermes_cli.local_runtime.detect.probe_port`` uses to tell llama-server apart
    from Ollama, LM Studio, vLLM and SGLang, which answer /v1/models but carry no build_info.
    Any failure — refused, timeout, non-JSON, a 404 from a vLLM/SGLang wire — is a NEGATIVE,
    never an error: an unproven wire keeps the previous behaviour.
    """
    try:
        with urllib.request.urlopen(f"{root}/props", timeout=_LLAMACPP_PROP_TIMEOUT_S) as resp:
            props = json.loads(resp.read() or b"null")
    except Exception:  # noqa: BLE001 - any failure means "not proven llama.cpp"
        return False
    return isinstance(props, dict) and bool(str(props.get("build_info") or "").strip())


def _is_llamacpp_wire(base_url: str | None) -> bool:
    """True when base_url provably speaks the llama.cpp wire (cached, never raises).

    Called from the request path, so: at most one probe per root per TTL, and a caller that
    finds a probe already in flight gets the previous answer (emit ``none``) instead of
    blocking on another thread's socket.
    """
    root = _loopback_root(base_url)
    if root is None:
        return False
    now = time.monotonic()
    cached = _llamacpp_cache.get(root)
    if cached and cached[1] > now:
        return cached[0]
    with _llamacpp_lock:
        cached = _llamacpp_cache.get(root)
        if cached and cached[1] > now:
            return cached[0]
        if root in _llamacpp_probing:
            return False
        _llamacpp_probing.add(root)
    try:
        verdict = _props_says_llamacpp(root)
    except Exception:  # noqa: BLE001 - the predicate must never break the kwargs build
        verdict = False
    with _llamacpp_lock:
        _llamacpp_probing.discard(root)
        _llamacpp_cache[root] = (
            verdict,
            time.monotonic() + (_LLAMACPP_PROP_TTL_S if verdict else _LLAMACPP_MISS_TTL_S),
        )
    return verdict


def _reset_llamacpp_wire_cache() -> None:
    """Drop the cached fingerprints (tests, and when an endpoint changes under one port)."""
    with _llamacpp_lock:
        _llamacpp_cache.clear()
        _llamacpp_probing.clear()


def _looks_like_ollama_endpoint(base_url: str | None) -> bool:
    """True only for explicit Ollama signatures (port 11434 or an ``ollama`` host label).
    ``think`` is Ollama-native; strict hosts (Mistral, Groq) 422 on it, and
    arbitrary localhost may be llama.cpp / vLLM / LM Studio."""
    raw = (base_url or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    try:  # urlparse raises ValueError on malformed ports ("host:99999"); treat as not-Ollama.
        if parsed.port == 11434:
            return True
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return bool(host) and (host == "ollama.com" or host.endswith(".ollama.com") or "ollama" in host.split("."))


class CustomProfile(ProviderProfile):
    """Custom/Ollama local provider — think=false and num_ctx support."""

    def supported_reasoning_efforts(self, model: str | None) -> tuple[str, ...]:
        """The OpenAI-compat wire set, mirroring this profile's own chat-completions clamp.

        Without this declaration the Responses transport clamps onto the OpenAI
        per-model ladder (``codex_supported_efforts``), where ``max`` is gpt-5.6-only —
        so a custom relay's model had a configured ``max`` silently demoted to
        ``xhigh`` while the same provider over chat-completions forwarded ``max``
        unchanged (#114249). A custom endpoint's vocabulary is undiscoverable, so
        the widest OpenAI-compat set is the honest ceiling; ``ultra`` still clamps
        to ``max`` via the shared ``clamp_effort`` policy.
        """
        return OPENAI_COMPAT_WIRE_EFFORTS

    def default_reasoning_config(self, model: str | None = None) -> dict | None:
        """Unset ``agent.reasoning_effort`` → ``medium``, as on the Nous / OpenRouter profiles.

        Leaving the field off lets the endpoint's own default apply, and for a hosted reasoning
        model that default can be its ceiling: kimi-k3 behind an OpenAI-compatible relay defaults
        to ``max`` — 3x the reasoning tokens and ~3x the latency of medium. The agent skips this
        default for models the catalog marks non-reasoning (``agent.reasoning_params``).
        """
        return {"enabled": True, "effort": "medium"}

    def build_api_kwargs_extras(
        self, *, reasoning_config: dict | None = None, ollama_num_ctx: int | None = None, **ctx: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extra_body: dict[str, Any] = {}
        top_level: dict[str, Any] = {}
        if ollama_num_ctx:
            extra_body["options"] = {"num_ctx": ollama_num_ctx}
        # disabled -> top-level reasoning_effort="none" (Ollama's /v1 ignores
        # extra_body.think) plus think=False only on Ollama URLs; enabled+effort ->
        # top-level reasoning_effort clamped to the OpenAI-compat wire (GLM/ARK,
        # vLLM and SGLang all top out at "max"; "ultra" verbatim 400s); None ->
        # omit so the server default applies (auxiliary calls without an effort, and
        # the main loop after the route rejected the reasoning field — an unset main
        # effort arrives here already filled by default_reasoning_config). Never emit
        # think=True (Ollama-only flag).
        if reasoning_config and isinstance(reasoning_config, dict):
            effort = (reasoning_config.get("effort") or "").strip().lower()
            if effort == "none" or reasoning_config.get("enabled", True) is False:
                # See #14820. llama.cpp is the exception: its wire has no "none" level
                # (default|minimal|low|medium|high|xhigh|max) and silently DISCARDS a value
                # it does not know, leaving the chat template's own default — the most
                # expensive one — on a seat that asked for no reasoning. Omit the field
                # there and let the server's --reasoning on|off|auto decide.
                if not _is_llamacpp_wire(ctx.get("base_url")):
                    top_level["reasoning_effort"] = "none"
                if _looks_like_ollama_endpoint(ctx.get("base_url")):
                    extra_body["think"] = False
            elif effort and base_url_host_matches(str(ctx.get("base_url") or ""), "api.groq.com"):
                # Groq's OpenAI-compatible wire accepts top-level reasoning_effort only as
                # "none" / "default"; any graded level ("medium", "high") 400s (#75089).
                top_level["reasoning_effort"] = "default"
            elif effort:
                top_level["reasoning_effort"] = clamp_effort(effort, OPENAI_COMPAT_WIRE_EFFORTS)
        return extra_body, top_level

    def fetch_models(
        self, *, api_key: str | None = None, base_url: str | None = None, timeout: float = 8.0
    ) -> list[str] | None:
        """base_url is user-configured; fetch only if set."""
        if not (base_url or self.base_url):
            return None
        return super().fetch_models(api_key=api_key, base_url=base_url, timeout=timeout)


custom = CustomProfile(
    name="custom", aliases=("ollama", "local", "vllm", "llamacpp", "llama.cpp", "llama-cpp"),
    env_vars=(),  # No fixed key — custom endpoint
    base_url="",  # User-configured
    # An arbitrary client ceiling can exceed a local server's actual output limit.
    # The endpoint owns its generation default.
)

register_provider(custom)
