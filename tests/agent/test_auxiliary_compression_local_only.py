"""Compression must stay local: ``task="compression"`` is never served by a cloud provider.

Context: the compression route on this host is a local llama-server (``yoyodine-compaction``,
``http://127.0.0.1:7778``). When it timed out, the auxiliary recovery ladder walked on to its last
resort — the *main agent model*, a cloud provider — so the conversation summary was silently sent
off-box (345 "falling back to main agent model" log events across 25 homes, plus 138
``session_model_usage`` rows with ``task='compression'`` served by ``deepseek-flash``).

Invariants asserted here (see AGENTS.md "Behavior contracts over snapshots"):

1. When the compression route is a local endpoint, no fallback rung may hand the summary to an
   off-box provider — not the configured chain, not the discovery chain, not the main agent model.
   Each refusal is one INFO line naming the route and the outcome, and the string the operator greps
   for ("falling back to main agent model") is never logged for compression.
2. A timeout on the local route retries that same local endpoint inside the bounded window
   (``_transient_retry_plan``: 15 s of retry sleeps, independent of ``auxiliary.transient_retries``) and
   then aborts the pass: no cloud client is ever called.
3. Contrast cases — another auxiliary task and the vision task keep their main-agent fallback and keep
   the generic attempt-count-bounded window (no seconds ceiling), and vision keeps skipping the
   same-provider timeout retry. Compression is the only task fenced.
"""

import contextlib
import logging
import os
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import agent.auxiliary_client as aux
from agent.context_compressor import ContextCompressor

LOCAL_PROVIDER = "custom:yoyodine-compaction"
LOCAL_BASE_URL = "http://127.0.0.1:7778"
LOCAL_MODEL = "yaan-compaction"
CLOUD_PROVIDER = "deepseek"
CLOUD_MODEL = "deepseek-flash"


class _Timeout(Exception):
    """Shaped like ``openai.APITimeoutError`` for ``_is_timeout_error``/``_is_connection_error``."""


_Timeout.__name__ = "APITimeoutError"


def _local_compression_config():
    return {
        "provider": LOCAL_PROVIDER,
        "model": LOCAL_MODEL,
        "base_url": LOCAL_BASE_URL,
        "api_key": "local-key",
    }


def _cloud_compression_config():
    """A compression route that is deliberately NOT local — the fence must stay off for it."""
    return {"provider": "openrouter", "model": "some/model"}


def _patch_common(monkeypatch, *, task_config, client, cloud_created):
    """Wire the aux machinery onto a local client plus a trackable cloud main-agent model."""
    monkeypatch.setattr(
        aux, "_get_auxiliary_task_config",
        lambda task: dict(task_config) if task == "compression" else {},
    )
    monkeypatch.setattr(
        aux, "_resolve_task_provider_model",
        lambda *a, **k: (LOCAL_PROVIDER, LOCAL_MODEL, LOCAL_BASE_URL, "local-key", None),
    )
    monkeypatch.setattr(aux, "_get_cached_client", lambda *a, **k: (client, LOCAL_MODEL))
    monkeypatch.setattr(aux, "_validate_llm_response", lambda resp, _task, **_kw: resp)
    monkeypatch.setattr(aux, "_read_main_provider", lambda: CLOUD_PROVIDER)
    monkeypatch.setattr(aux, "_read_main_model", lambda: CLOUD_MODEL)
    # Zero the backoff so the bounded retry window costs no wall time in tests.
    monkeypatch.setattr(aux, "_TRANSIENT_RETRY_BACKOFF_BASE", 0.0)

    cloud_client = MagicMock()
    cloud_client.base_url = "https://api.deepseek.com/v1"
    cloud_client.chat.completions.create.side_effect = lambda **kw: cloud_created.append(kw) or {}
    resolved = []

    def _resolve(provider=None, *a, **k):
        resolved.append(provider)
        return cloud_client, CLOUD_MODEL

    monkeypatch.setattr(aux, "resolve_provider_client", _resolve)
    return cloud_created, resolved


def _local_client():
    client = MagicMock()
    client.base_url = LOCAL_BASE_URL
    client.chat.completions.create.side_effect = _Timeout("Request timed out.")
    return client


def _messages():
    return [{"role": "user", "content": "summarise this"}]


# ---------------------------------------------------------------------------
# Invariant 1: the last-resort rung is closed for a local compression route
# ---------------------------------------------------------------------------


def test_local_compression_refuses_main_agent_model_fallback(monkeypatch, caplog):
    """The main-agent last resort must refuse compression on a local route and say so once."""
    cloud_created, resolved = _patch_common(
        monkeypatch, task_config=_local_compression_config(), client=_local_client(),
        cloud_created=[],
    )
    with caplog.at_level(logging.INFO):
        result = aux._try_main_agent_model_fallback(
            LOCAL_PROVIDER, task="compression", reason="timeout")

    assert result == (None, None, ""), "no cloud client may be returned for compression"
    assert resolved == [], "the cloud main-agent client must not even be resolved"
    assert cloud_created == []
    messages = [r.getMessage() for r in caplog.records]
    assert not any("falling back to main agent model" in m for m in messages), (
        "the operator's acceptance grep must never match for compression"
    )
    refusals = [m for m in messages if "refusing" in m and "compression" in m]
    assert len(refusals) == 1, f"expected exactly one refusal INFO line, got {messages}"
    assert CLOUD_PROVIDER in refusals[0], "the refused (off-host) route must be named"
    assert LOCAL_PROVIDER in refusals[0] and LOCAL_BASE_URL in refusals[0], (
        "the protected local route must be named so the line is self-explanatory"
    )
    assert "local-only" in refusals[0] and "stays on this host" in refusals[0], (
        "the outcome must be named"
    )


@pytest.mark.parametrize("task", ["session_search", "vision"])
def test_other_tasks_keep_main_agent_model_fallback(monkeypatch, caplog, task):
    """Contrast: the fence is compression-scoped — other tasks still reach the main model."""
    cloud_created, resolved = _patch_common(
        monkeypatch, task_config=_local_compression_config(), client=_local_client(),
        cloud_created=[],
    )
    with caplog.at_level(logging.INFO):
        client, model, label = aux._try_main_agent_model_fallback(
            LOCAL_PROVIDER, task=task, reason="timeout")

    assert client is not None and label == f"main-agent({CLOUD_PROVIDER})"
    assert resolved == [CLOUD_PROVIDER]
    assert any(
        "falling back to main agent model" in r.getMessage() for r in caplog.records
    ), "non-compression tasks keep the existing main-agent fallback"


def test_cloud_compression_route_is_not_fenced(monkeypatch, caplog):
    """A compression route that is *not* local is not a local-only task: the fence stays off."""
    cloud_created, resolved = _patch_common(
        monkeypatch, task_config=_cloud_compression_config(), client=_local_client(),
        cloud_created=[],
    )
    with caplog.at_level(logging.INFO):
        client, _model, _label = aux._try_main_agent_model_fallback(
            "openrouter", task="compression", reason="timeout")

    assert client is not None, "no local route configured → compression keeps its fallbacks"
    assert resolved == [CLOUD_PROVIDER]


# ---------------------------------------------------------------------------
# Invariant 2: bounded local retry, then abort — no cloud call
# ---------------------------------------------------------------------------


def test_local_compression_timeout_retries_local_endpoint_then_aborts(monkeypatch, caplog):
    """A timeout retries the local endpoint (bounded) and the pass aborts, never escalating."""
    local = _local_client()
    cloud_created = []
    _patch_common(
        monkeypatch, task_config=_local_compression_config(), client=local,
        cloud_created=cloud_created,
    )
    with caplog.at_level(logging.INFO), pytest.raises(_Timeout):
        aux.call_llm(task="compression", messages=_messages())

    retries = aux._transient_retry_plan("compression")[0]
    attempts = local.chat.completions.create.call_count
    assert attempts == 1 + retries, (
        f"local endpoint must be retried within the bounded window: {attempts} attempts "
        f"for the compression retry plan={retries}"
    )
    assert aux._transient_retry_plan("compression")[1] is not None, (
        "compression's window carries a seconds ceiling, not an attempt count alone"
    )
    assert cloud_created == [], "compression must never reach a cloud endpoint"
    messages = [r.getMessage() for r in caplog.records]
    assert not any("falling back to main agent model" in m for m in messages)


def test_should_skip_same_provider_retry_is_compression_scoped():
    """Compression now retries its own (local) endpoint; vision keeps the skip."""
    timeout = _Timeout("Request timed out.")
    assert aux._should_skip_same_provider_retry("compression", timeout) is False
    assert aux._should_skip_same_provider_retry("vision", timeout) is True


# ---------------------------------------------------------------------------
# Invariant 3: the compressor itself must not re-issue a local-only summary on
# the main agent model (the second escape hatch, in context_compressor.py)
# ---------------------------------------------------------------------------


def _summary_messages():
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "msg 1"},
        {"role": "assistant", "content": "msg 2"},
        {"role": "user", "content": "msg 3"},
        {"role": "assistant", "content": "msg 4"},
        {"role": "user", "content": "msg 5"},
        {"role": "assistant", "content": "msg 6"},
        {"role": "user", "content": "msg 7"},
    ]


def _make_compressor():
    with patch("agent.context_compressor.get_model_context_length", return_value=100000):
        return ContextCompressor(
            model=CLOUD_MODEL, quiet_mode=True, protect_first_n=2, protect_last_n=2,
            abort_on_summary_failure=True,
        )


def _run_summary_failure(monkeypatch, *, route_is_local: bool):
    """Drive one summary failure; return (compressor, attempts, result)."""
    compressor = _make_compressor()
    compressor.summary_model = LOCAL_MODEL  # distinct from compressor.model
    attempts = []

    def _fail(**_kwargs):
        attempts.append(1)
        raise _Timeout("Request timed out.")

    monkeypatch.setattr("agent.context_compressor.compression_route_is_local", lambda: route_is_local)
    monkeypatch.setattr("agent.context_compressor.call_llm", _fail)
    messages = _summary_messages()
    return compressor, attempts, compressor.compress(messages)


def test_local_compression_route_does_not_retry_summary_on_main_model(monkeypatch):
    """Local-only route: one attempt, then the pass aborts — no main-model retry.

    The retry would re-issue the summary on the main agent model and report
    "falling back to main model" for a route that cannot leave this host.
    """
    compressor, attempts, result = _run_summary_failure(monkeypatch, route_is_local=True)

    assert len(attempts) == 1, "a local-only route must not retry the pass on the main model"
    assert getattr(compressor, "_summary_model_fallen_back", False) is False
    assert compressor._last_compress_aborted is True, "abort_on_summary_failure must surface it"
    assert result == _summary_messages(), "original turns preserved"


def test_non_local_compression_route_still_retries_summary_on_main_model(monkeypatch):
    """Contrast: an off-host summary route keeps the one-shot main-model retry."""
    compressor, attempts, _result = _run_summary_failure(monkeypatch, route_is_local=False)

    assert len(attempts) == 2, "non-local routes keep the one-shot main-model retry"
    assert compressor._summary_model_fallen_back is True


# ---------------------------------------------------------------------------
# Invariant 4: the real path — config propagation through a temp HERMES_HOME
# ---------------------------------------------------------------------------


def _write_home_config() -> None:
    """Pin compression to a local endpoint with a cloud main agent + cloud fallback chain.

    Only the transport is faked from here on: the route, the task config and the ladder are the
    real ones, read from a real ``config.yaml`` in the per-test HERMES_HOME (AGENTS.md: E2E
    validation for resolution chains, not just green unit mocks).
    """
    home = Path(os.environ["HERMES_HOME"])
    (home / "config.yaml").write_text(
        textwrap.dedent(
            """
            model:
              provider: openrouter
              name: deepseek/deepseek-chat
            providers:
              openrouter:
                api_key: sk-test-cloud
            auxiliary:
              compression:
                provider: custom:yoyodine-compaction
                model: yaan-compaction
                base_url: http://127.0.0.1:7778
                timeout: 120
                fallback_chain:
                  - provider: openrouter
                    model: deepseek/deepseek-chat
            custom_providers:
              - name: yoyodine-compaction
                base_url: http://127.0.0.1:7778
            """
        ).lstrip(),
        encoding="utf-8",
    )


def test_e2e_temp_home_local_compression_never_resolves_a_cloud_client(monkeypatch, caplog):
    """Real config → real route resolution: the cloud chain is refused, the local route retried."""
    _write_home_config()

    assert aux.compression_route_is_local() is True, "the fence must arm on the real config"
    assert aux._custom_health_base_url("yoyodine-compaction") == LOCAL_BASE_URL

    local = _local_client()
    cloud = MagicMock()
    cloud_resolved = []

    def _resolve_cloud(provider, *args, **kwargs):  # pragma: no cover - must never run
        cloud_resolved.append(provider)
        return cloud, CLOUD_MODEL

    monkeypatch.setattr(aux, "_get_cached_client", lambda *a, **k: (local, LOCAL_MODEL))
    monkeypatch.setattr(aux, "resolve_provider_client", _resolve_cloud)
    monkeypatch.setattr(aux, "_TRANSIENT_RETRY_BACKOFF_BASE", 0.0)

    with caplog.at_level(logging.INFO), pytest.raises(_Timeout):
        aux.call_llm(task="compression", messages=_messages())

    assert cloud_resolved == [], "no cloud client may be resolved for a local-only compression route"
    retries = aux._transient_retry_plan("compression")[0]
    assert local.chat.completions.create.call_count == 1 + retries
    messages = [r.getMessage() for r in caplog.records]
    assert not any("falling back to main agent model" in m for m in messages), messages
    refusals = [m for m in messages if "refusing" in m]
    assert refusals, f"each refused rung needs one INFO line: {messages}"
    assert all("compression" in m for m in refusals)
    assert any("openrouter" in m for m in refusals), "the configured cloud rung is named"


# ---------------------------------------------------------------------------
# Invariant 5: the compression window is bounded in SECONDS, and only compression has it
# ---------------------------------------------------------------------------


class _LoadingModel(Exception):
    """Shaped like the local llama-server's 503 while it reloads the model."""

    status_code = 503

    def __init__(self):
        super().__init__(
            'Error code: 503 - {"error": {"message": "Loading model", "type": "unavailable_error"}}')


def test_compression_retry_sleeps_are_capped_by_seconds_not_attempts(monkeypatch):
    """Compression retries inside one ~15 s window, and a raised attempt count cannot widen it.

    The route is local-only and the retry exists to outlast a llama-server restart, so the sleeps are
    expected to fill the window rather than stop at the generic count — and to stop at the seconds
    ceiling once the count is raised, so a persistent outage aborts instead of holding the turn.
    """
    monkeypatch.setattr(aux, "_COMPRESSION_TRANSIENT_RETRIES", 12)
    local = _local_client()
    _patch_common(monkeypatch, task_config=_local_compression_config(), client=local, cloud_created=[])
    monkeypatch.setattr(aux, "_TRANSIENT_RETRY_BACKOFF_BASE", 1.0)
    slept = []
    monkeypatch.setattr("time.sleep", slept.append)

    with pytest.raises(_Timeout):
        aux.call_llm(task="compression", messages=_messages())

    budget = aux._COMPRESSION_TRANSIENT_RETRY_BUDGET_SECONDS
    assert local.chat.completions.create.call_count == len(slept) + 1
    assert slept == sorted(slept), f"backoff must be non-decreasing: {slept}"
    assert len(slept) > aux._transient_retry_count(), (
        "compression gets a longer window than the generic count"
    )
    assert sum(slept) <= budget, f"retry sleeps must stay inside the {budget}s ceiling: {slept}"
    assert sum(slept) >= 0.8 * budget, f"the window must be spent, not cut short: {slept}"


def test_loading_model_503_on_compression_is_retried_not_terminal(monkeypatch, caplog):
    """A 503 "Loading model" is a retry-soon signal on compression: retry inside the window, then abort."""
    local = MagicMock()
    local.base_url = LOCAL_BASE_URL
    local.chat.completions.create.side_effect = _LoadingModel()
    cloud_created = []
    _patch_common(monkeypatch, task_config=_local_compression_config(), client=local,
                  cloud_created=cloud_created)

    with caplog.at_level(logging.INFO), pytest.raises(_LoadingModel):
        aux.call_llm(task="compression", messages=_messages())

    retries = aux._transient_retry_plan("compression")[0]
    assert local.chat.completions.create.call_count == 1 + retries, (
        "the model-reload 503 must be retried inside the window, not treated as terminal"
    )
    assert cloud_created == [], "a reloading local endpoint must not push the summary off-box"
    assert any("transient transport error" in r.getMessage() for r in caplog.records)


def test_non_compression_task_keeps_the_generic_retry_budget(monkeypatch):
    """Contrast: only compression carries a seconds ceiling; other tasks keep ``transient_retries``."""
    client = MagicMock()
    client.base_url = LOCAL_BASE_URL
    client.chat.completions.create.side_effect = Exception("Connection refused")
    _patch_common(monkeypatch, task_config=_local_compression_config(), client=client, cloud_created=[])
    monkeypatch.setattr(aux, "_transient_retry_count", lambda: 6)

    with contextlib.suppress(Exception):
        aux.call_llm(task="session_search", messages=_messages())

    retries, budget = aux._transient_retry_plan("session_search")
    assert budget is None, "the generic window has no seconds ceiling to enforce"
    assert retries == 6
    assert client.chat.completions.create.call_count == 1 + 6, (
        "a non-compression task still spends every configured retry"
    )


def test_a_stalled_attempt_spends_the_window_instead_of_buying_a_retry(monkeypatch):
    """The ceiling is checked on the wall clock too: a slow failure must not multiply a whole timeout.

    ``auxiliary.compression.timeout`` is 600 s on this host, so a bound that counted only attempts would
    let one bad pass hold the turn for a full timeout per retry. A stalled attempt spends the window even
    though it never sleeps, and the pass then aborts.
    """
    clock = [0.0]
    local = MagicMock()
    local.base_url = LOCAL_BASE_URL

    def _stall(**_kwargs):
        clock[0] += 60.0  # a stalled attempt: no sleeps, but real time passes
        raise _Timeout()

    local.chat.completions.create.side_effect = _stall
    _patch_common(monkeypatch, task_config=_local_compression_config(), client=local, cloud_created=[])
    monkeypatch.setattr(aux.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(aux, "_TRANSIENT_RETRY_BACKOFF_BASE", 0.2)
    slept = []
    monkeypatch.setattr("time.sleep", slept.append)

    with pytest.raises(_Timeout):
        aux.call_llm(task="compression", messages=_messages())

    budget = aux._COMPRESSION_TRANSIENT_RETRY_BUDGET_SECONDS
    assert local.chat.completions.create.call_count == 1 + len(slept)
    assert sum(slept) <= budget, f"retry sleeps must stay inside the {budget}s ceiling: {slept}"
    assert len(slept) < aux._COMPRESSION_TRANSIENT_RETRIES, (
        f"a 60 s stall must spend the window, not buy every retry: {slept}"
    )