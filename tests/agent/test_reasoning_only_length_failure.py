"""A reasoning-only ``finish_reason="length"`` response must FAIL the turn.

A thinking model whose output cap is at or below its thinking length returns HTTP 200 with
``content=""``, ``finish_reason="length"`` and the reasoning in its own field. The failure is
invisible on the wire: it is only in the two channels together. Before this guard the loop
treated it as ordinary truncation, spent four full-prompt continuations, and then ended the
turn as a *successful* partial whose answer was the ⚠️ banner — a green turn, and a green
``hermes -z`` exit, with no answer in it.

The rule these tests pin: no visible text after one reasoning-off continuation means the turn
failed with the ``empty_response`` class. Text that DOES arrive keeps today's partial-success
behaviour, and the ``<think>``-tag shape is unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_constants import FINISH_REASON_LENGTH


@pytest.fixture()
def loop_agent():
    from run_agent import AIAgent
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        a.client = MagicMock()
        a._cached_system_prompt = "You are helpful."
        a._use_prompt_caching = False
        a.compression_enabled = False
        a.save_trajectories = False
        return a


def _usage(reasoning_tokens=0, prompt=100, completion=0):
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion or reasoning_tokens,
        total_tokens=prompt + (completion or reasoning_tokens),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens),
    )


def _length_response(content="", *, reasoning=None, reasoning_tokens=0):
    """One chat-completions response cut off by the output cap."""
    msg = SimpleNamespace(content=content, tool_calls=None)
    if reasoning is not None:
        msg.reasoning_content = reasoning
    return SimpleNamespace(
        choices=[SimpleNamespace(index=0, message=msg, finish_reason=FINISH_REASON_LENGTH)],
        model="test/model",
        usage=_usage(reasoning_tokens),
    )


def _run(agent, message, history=None):
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        return agent.run_conversation(message, conversation_history=history)


def _script(agent, *responses):
    agent.client.chat.completions.create.side_effect = list(responses)


class TestReasoningOnlyLengthResponse:
    def test_fails_the_turn_instead_of_returning_a_banner(self, loop_agent):
        """The defect: the ceiling exit was a successful partial whose text was the banner."""
        _script(
            loop_agent,
            _length_response(reasoning="thinking, thinking, thinking", reasoning_tokens=512),
            _length_response(reasoning="still thinking", reasoning_tokens=512),
            # Spare responses: at the unfixed base the loop spends four continuations.
            _length_response(reasoning="still thinking", reasoning_tokens=512),
            _length_response(reasoning="still thinking", reasoning_tokens=512),
        )
        result = _run(loop_agent, "triage this finding")

        assert result.get("failed") is True, (
            "A response with no visible text must not settle as a successful turn, however "
            f"much reasoning it carries. Got: {sorted(result)}"
        )
        assert result.get("failure_reason") == "empty_response"
        assert result.get("completed") is False
        assert result.get("partial") is None

    def test_caps_the_recovery_at_one_reasoning_off_continuation(self, loop_agent):
        """One reasoning-off continuation is a real recovery; four are four re-billed prompts."""
        _script(
            loop_agent,
            _length_response(reasoning="thinking", reasoning_tokens=512),
            _length_response(reasoning="thinking", reasoning_tokens=512),
            _length_response(reasoning="thinking", reasoning_tokens=512),
            _length_response(reasoning="thinking", reasoning_tokens=512),
        )
        result = _run(loop_agent, "triage this finding")

        assert loop_agent.client.chat.completions.create.call_count == 2, (
            "1 initial call + exactly ONE reasoning-off continuation."
        )
        # The recovery continuation must actually go out with thinking disabled.
        assert result.get("api_calls") == 2

    def test_the_single_continuation_disables_thinking(self, loop_agent):
        from agent import chat_completion_helpers as cch

        sent: list = []
        original = cch._reasoning_config_for_wire

        def _record(agent):
            cfg = original(agent)
            sent.append(cfg)
            return cfg

        _script(
            loop_agent,
            _length_response(reasoning="thinking", reasoning_tokens=512),
            _length_response(reasoning="thinking", reasoning_tokens=512),
            _length_response(reasoning="thinking", reasoning_tokens=512),
        )
        with patch.object(cch, "_reasoning_config_for_wire", side_effect=_record):
            _run(loop_agent, "triage this finding")

        assert len(sent) >= 2
        assert isinstance(sent[1], dict) and sent[1].get("enabled") is False, (
            f"the continuation must go out with thinking off, got {sent[1]!r}"
        )

    def test_usage_reasoning_tokens_alone_is_evidence(self, loop_agent):
        """Some providers report the thinking only in usage, not as a message field."""
        _script(
            loop_agent,
            _length_response(reasoning_tokens=512),
            _length_response(reasoning_tokens=512),
            _length_response(reasoning_tokens=512),
        )
        result = _run(loop_agent, "triage this finding")

        assert result.get("failed") is True
        assert loop_agent.client.chat.completions.create.call_count == 2


class TestUnchangedShapes:
    def test_partial_with_text_keeps_partial_success(self, loop_agent):
        """A length response that carries visible text is a partial answer, not a failure."""
        _script(
            loop_agent,
            _length_response("part one "),
            _length_response("part two "),
            _length_response("part three "),
            _length_response("part four."),
        )
        result = _run(loop_agent, "write me a long report")

        assert loop_agent.client.chat.completions.create.call_count == 4
        assert result.get("completed") is False
        assert result.get("partial") is True
        assert not result.get("failed")
        for part in ("part one", "part four"):
            assert part in (result["final_response"] or "")

    def test_think_tag_exhaustion_still_aborts_immediately(self, loop_agent):
        """The inline-``<think>`` shape (Ollama-style) keeps its immediate abort."""
        _script(loop_agent, _length_response("<think>reasoning only</think>"))
        result = _run(loop_agent, "triage this finding")

        assert loop_agent.client.chat.completions.create.call_count == 1
        assert not result.get("failed")
        assert "Thinking Budget Exhausted" in (result["final_response"] or "")

    def test_empty_length_response_without_reasoning_evidence_keeps_the_ladder(self, loop_agent):
        """No reasoning evidence: ordinary truncation, four continuations, unchanged."""
        _script(
            loop_agent,
            _length_response(),
            _length_response(),
            _length_response(),
            _length_response(),
        )
        result = _run(loop_agent, "write me a long report")

        assert loop_agent.client.chat.completions.create.call_count == 4
        assert result.get("partial") is True
