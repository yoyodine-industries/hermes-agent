"""Contract between the model-facing dispatch helper and ``delegate_task`` itself.

Live outage this guards: ``AIAgent._dispatch_delegate_task`` — the live model path, reached
from ``agent/inline_tool_executors.py`` and ``agent/tool_executor.py`` — forwards
``images=function_args.get("images")``. A stale whole-file copy of ``tools/delegate_tool.py``
in the live checkout predated the ``images`` parameter, so *every* delegation (single task,
batch, background or synchronous) died at the call with::

    TypeError: delegate_task() got an unexpected keyword argument 'images'

The neighbouring dispatch tests patch ``delegate_task`` with a ``**kwargs`` fake, which is
exactly why the skew reviewed green: a mock accepts any kwarg, so the caller and the callee can
drift apart invisibly. These tests drive the REAL callee through the REAL helper, so the same
skew fails loudly.
"""

import base64
import threading
import unittest
from unittest.mock import MagicMock, patch

import run_agent

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABh6FO1AAAAABJRU5ErkJggg=="
)
_DATA_URL = "data:image/png;base64," + base64.b64encode(_PNG).decode()


def _mock_parent(depth=1):
    """A parent agent carrying the fields the delegation contract reads.

    ``depth`` > 0 makes the dispatch helper request the synchronous path (an orchestrator
    subagent needs its children's results within its own turn), so the child's summary comes
    back in the tool result instead of as a later message.
    """
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "sk-test"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "anthropic/claude-sonnet-4"
    parent.platform = "cli"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = depth
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    return parent


class TestDelegateDispatchKwargContract(unittest.TestCase):
    def _dispatch(self, function_args):
        """Run the real dispatch helper into the real delegate_task, with a stubbed child.

        The parent is a depth-1 orchestrator subagent, so the helper asks for the synchronous
        path and the summary comes back in the tool result. The depth gate reads
        ``delegation.max_spawn_depth`` (1 in the isolated test home), so it is raised to 2 here —
        otherwise this asserts the depth refusal instead of the kwarg contract.
        """
        dispatch = run_agent.AIAgent._dispatch_delegate_task
        child = MagicMock()
        child.run_conversation.return_value = {
            "final_response": "child summary", "completed": True, "api_calls": 1,
        }
        with patch("run_agent.AIAgent") as MockAgent, \
                patch("tools.delegate_tool._get_max_spawn_depth", return_value=2):
            MockAgent.return_value = child
            out = dispatch(_mock_parent(), function_args)
        return out, child

    def test_bare_single_task_call_returns_child_summary(self):
        """A bare single-task dispatch — no ``images`` key anywhere — reaches the child and
        returns its summary rather than raising on the forwarded kwarg set."""
        out, child = self._dispatch({"goal": "Summarize the README"})

        child.run_conversation.assert_called_once()
        self.assertIn("child summary", out)

    def test_images_kwarg_is_accepted_and_reaches_the_child(self):
        """When the model does pass ``images``, the callee accepts it instead of raising, and the
        pixels ride the child's first turn as native ``image_url`` parts."""
        with patch("agent.image_routing.decide_image_input_mode", return_value="native"):
            out, child = self._dispatch({"goal": "Describe this screenshot", "images": [_DATA_URL]})

        self.assertIn("child summary", out)
        self.assertEqual(getattr(child, "_delegate_images", None), [_DATA_URL])
        call = child.run_conversation.call_args
        user_message = call.args[0] if call.args else call.kwargs["user_message"]
        self.assertIsInstance(user_message, list)
        self.assertEqual([p["type"] for p in user_message].count("image_url"), 1)


if __name__ == "__main__":
    unittest.main()
