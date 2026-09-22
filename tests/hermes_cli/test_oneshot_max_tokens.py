"""Tests for the -z/--oneshot --max-tokens output-token cap.

``--max-tokens N`` is a per-invocation output-token ceiling threaded from the CLI
through ``run_oneshot`` -> ``_run_agent`` -> ``AIAgent(max_tokens=N)``, which the
agent forwards to the provider on routes that honor an explicit cap (the managed
local llama-server and Anthropic Messages). These tests pin the parsing contract,
the positivity validation, and the forwarding seam -- the three places a cap
could silently drop before it reaches the model request.
"""

import pytest

from hermes_cli._parser import build_top_level_parser, top_level_value_flag_sets


def test_max_tokens_flag_parses_at_top_level_with_oneshot():
    parser, _subparsers, _chat = build_top_level_parser()
    args = parser.parse_args(["--max-tokens", "64", "-z", "hello"])
    assert args.max_tokens == 64
    assert args.oneshot == "hello"


def test_max_tokens_flag_parses_before_chat_subcommand():
    parser, _subparsers, _chat = build_top_level_parser()
    args = parser.parse_args(["--max-tokens", "128", "chat", "-q", "hello"])
    assert args.max_tokens == 128


def test_max_tokens_is_classified_as_value_flag():
    required, optional = top_level_value_flag_sets()
    assert "--max-tokens" in (required | optional)


def test_run_oneshot_rejects_non_positive_max_tokens(capsys):
    from hermes_cli.oneshot import run_oneshot

    assert run_oneshot("hello", max_tokens=0) == 2
    assert run_oneshot("hello", max_tokens=-5) == 2
    err = capsys.readouterr().err
    assert "positive integer" in err


def test_run_agent_forwards_max_tokens_to_agent(monkeypatch):
    import run_agent
    from types import SimpleNamespace

    import hermes_cli.oneshot as oneshot

    captured = {}

    class FakeAIAgent:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs

        def run_conversation(self, prompt, conversation_history=None):
            return {"final_response": ""}

    choice = SimpleNamespace(
        model="test/model", provider=None, base_url=None, api_key=None, api_mode=None,
    )

    monkeypatch.setattr(oneshot, "_resolve_model_and_provider", lambda cfg, model, provider: choice)
    monkeypatch.setattr(oneshot, "_create_session_db_for_oneshot", lambda: None)
    monkeypatch.setattr(oneshot, "_load_resume_target", lambda db, resume: (None, None, None))
    monkeypatch.setattr(oneshot, "_apply_stored_session_runtime", lambda c, meta, explicit_model: c)
    monkeypatch.setattr(oneshot, "_build_preloaded_skills_prompt", lambda skills: None)
    monkeypatch.setattr(oneshot, "get_fallback_chain", lambda cfg: None)
    monkeypatch.setattr(oneshot, "_close_agent", lambda agent, db: None)
    monkeypatch.setattr(run_agent, "AIAgent", FakeAIAgent)

    import hermes_cli.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "load_config", lambda: {})

    import hermes_cli.runtime_provider as rp
    monkeypatch.setattr(rp, "resolve_runtime_provider", lambda **kw: {})

    import hermes_cli.tools_config as tc
    monkeypatch.setattr(tc, "_get_platform_tools", lambda cfg, platform: [])

    import hermes_constants as hc
    monkeypatch.setattr(hc, "resolve_reasoning_config", lambda cfg, model: None)

    import hermes_cli.mcp_startup as mcp
    monkeypatch.setattr(mcp, "ensure_mcp_discovery_before_agent_build", lambda **kw: None)

    oneshot._run_agent("hello", max_tokens=64)

    assert captured["kwargs"]["max_tokens"] == 64
