"""Tests for the write-path truncation guard on content-bearing tool arguments.

Same rule as the outbound-DM guard (``tools/dm_body_guard``), different damage: a long tool
argument cut where it was AUTHORED lands as a partial FILE with a success status. The marker
is literal text in the payload, and it leaves the serialized JSON valid, so nothing downstream
can tell a cut file from a short one. What matters most is that the write is refused BEFORE any
side effect: nothing created, nothing modified (see ``TestWriteFileRegistryE2E``).
"""

import json

import pytest

import tools.file_tools  # noqa: F401 — importing registers the write_file/patch handlers
from tools.dm_body_guard import (
    CONTENT_ARG_FIELDS,
    CONTENT_TAIL_WINDOW,
    DEFAULT_REPORT_TARGET,
    TAIL_WINDOW,
    content_refusal,
    find_truncation_marker,
    guard_tool_content_arguments,
)

# The real-world sample: a Python file whose docstring just stops, cut at authoring time.
CUT_DOCSTRING = (
    '"""Daily report builder.\n'
    "\n"
    "Collects the day's numbers, writes them to the sheet, and the docstring simply\n"
    "stops mid-sentence because the argument was cut at authoring time...[truncated]"
)

CLEAN_DOCSTRING = (
    '"""Daily report builder.\n'
    "\n"
    "Collects the day's numbers and writes them to the sheet.\n"
    '"""\n'
)

# A sample shape reported from the operator's own pass: two file writes landed as 309- and
# 311-byte stubs (from arguments of ~900 bytes and 1.2 KB) ending in the literal marker, while a
# 2.8 KB argument from the same session posted intact. The cut is intermittent and mid-word, so
# the length is the point — the refusal must not depend on it.
OPERATOR_STUB = (
    '"""Wire the delivery drainer to the outbound queue.\n'
    "\n"
    "The script asserted its own preconditions before touching the queue, and the\n"
    "docstring simply stops mid-sentence because the argument was cut at authoring\n"
    "time while the write reported success, and nothing in the write path could tell\n"
    "this partial file apart from a complete one until a human read the tail back"
    "...[truncated]"
)


@pytest.mark.parametrize("value", [
    "[truncated]",                        # exactly the marker, nothing else
    CUT_DOCSTRING,                        # cut inside a docstring, mid-sentence
    "body text\n[...truncated]",          # leading ellipsis
    "body text\n[TRUNCATED]",             # case variant
    "body text\n[truncated 4096 chars]",  # annotated marker
    "def build():\n    return 1\n\n[truncated]\n\n",  # followed only by blank lines
    OPERATOR_STUB,                        # the operator's ~310-byte stub, cut mid-word
])
def test_marker_tailed_content_is_refused_by_the_guard(value):
    refusal = content_refusal("content", value)
    assert refusal is not None
    assert "REFUSED" in refusal


def test_refusal_names_the_field_marker_length_and_what_happened():
    refusal = content_refusal("file_content", CUT_DOCSTRING, report_to="the duty reviewer")
    assert refusal is not None
    assert "'file_content'" in refusal
    assert "'[truncated]'" in refusal
    assert f"({len(CUT_DOCSTRING.strip())} chars)" in refusal
    # The three things the agent must be told: nothing landed, this is not a size problem,
    # and who to notify.
    assert "NOTHING was written" in refusal
    assert "no file was created" in refusal
    assert "no file was modified" in refusal
    assert "not a size limit" in refusal
    assert "the duty reviewer" in refusal

    # The rule carries no deployment's identifiers: unset, it names nobody in particular.
    unnamed = content_refusal("file_content", CUT_DOCSTRING)
    assert unnamed is not None
    assert DEFAULT_REPORT_TARGET in unnamed
    assert "the duty reviewer" not in unnamed


@pytest.mark.parametrize("value", [
    "",
    None,
    CLEAN_DOCSTRING,
    "the log line is truncated, so we re-read the file.",                 # bare word in prose
    "we replaced the \"[truncated]\" placeholder before sending",         # mid-text quotation
    "the DMs looked like this: [truncated] and then they stopped arriving",
    "[details omitted] deliberately, no machinery involved",              # bracketed, not a marker
    'x = "[truncated]"  # the literal token, quoted in code\n',
    "a" * 5000,                                                          # long, no marker at all
])
def test_prose_quotations_and_clean_content_are_not_refused_by_the_guard(value):
    assert content_refusal("content", value) is None


def test_content_marker_window_is_generous_and_the_dm_window_is_unchanged():
    # Contract between the two windows: content is cut mid-docstring / mid-paragraph, so it
    # needs the wider look-back; the DM path keeps its short one.
    assert CONTENT_TAIL_WINDOW > TAIL_WINDOW
    # The DM callers still work positionally, with their own default preserved.
    assert find_truncation_marker("done. [truncated]") == "[truncated]"
    assert find_truncation_marker("done. [truncated]", window=CONTENT_TAIL_WINDOW) == "[truncated]"
    # A marker deep inside a long payload (outside the narrow DM window) is still found.
    long_tail = ("paragraph. " * 40) + "cut...[truncated]"
    assert content_refusal("content", long_tail) is not None


def test_old_string_is_not_gated_by_the_content_guard():
    # A cut old_string is a search pattern: it fails to match loudly on its own, so gating it
    # would only add false refusals.
    assert set(CONTENT_ARG_FIELDS) == {"content", "file_content", "new_string"}
    assert guard_tool_content_arguments(
        {"new_string": "clean", "old_string": "cut...[truncated]"}) is None


def test_guard_finds_a_suspect_nested_in_a_skill_manage_operations_list():
    args = {
        "operations": [
            {"action": "write_file", "name": "x", "file_path": "references/ok.md",
             "file_content": "fine\n"},
            {"action": "write_file", "name": "x", "file_path": "references/cut.md",
             "file_content": "the procedure, and then it stops...[truncated]"},
        ]
    }
    refusal = guard_tool_content_arguments(args)
    assert refusal is not None
    assert "'file_content'" in refusal


def test_guard_finds_a_suspect_at_the_top_level_too():
    refusal = guard_tool_content_arguments({"path": "x.md", "content": "t...[truncated]"})
    assert refusal is not None
    assert "'content'" in refusal


def test_guard_ignores_non_content_fields_and_clean_payloads():
    # A marker in a path/name is not a content payload and must not be refused.
    assert guard_tool_content_arguments({"path": "weird...[truncated]", "content": "fine"}) is None
    assert guard_tool_content_arguments(
        {"operations": [{"action": "create", "name": "x", "content": CLEAN_DOCSTRING}]}) is None
    assert guard_tool_content_arguments({}) is None
    assert guard_tool_content_arguments(None) is None
    assert guard_tool_content_arguments([{"name": "x"}, None, 3]) is None


class TestWriteFileRegistryE2E:
    """The assertion that matters: a refused write leaves NOTHING behind.

    Dispatched through the real registry (``registry.dispatch`` -> the registered
    ``write_file`` / ``patch`` handler), not a direct function call, and against a real temp
    filesystem so a partial write would actually land and be visible.
    """

    @staticmethod
    def _call(tool, args, task_id="content-guard-e2e"):
        from tools.registry import registry

        out = registry.dispatch(tool, args, task_id=task_id)
        return json.loads(out) if isinstance(out, str) else out

    def test_marker_tailed_write_is_refused_and_no_file_is_created(self, tmp_path):
        # Write into a directory of its own: "nothing landed" is then a plain emptiness check
        # that cannot be fooled by anything else the test session leaves in tmp_path.
        target_dir = tmp_path / "targets"
        target_dir.mkdir()
        target = target_dir / "partial.md"
        result = self._call("write_file", {"path": str(target), "content": CUT_DOCSTRING})

        assert "error" in result, result
        assert "REFUSED" in result["error"]
        assert "NOTHING was written" in result["error"]
        assert not target.exists(), "a refused write must not create the file"
        assert list(target_dir.iterdir()) == [], "a refused write must leave nothing behind"

    def test_marker_tailed_write_does_not_modify_an_existing_file(self, tmp_path):
        target = tmp_path / "existing.md"
        target.write_text("original body\n")
        result = self._call("write_file", {"path": str(target), "content": "t...[truncated]"})

        assert "error" in result, result
        assert target.read_text() == "original body\n", "the existing file must be untouched"

    def test_clean_write_still_lands_through_the_registry_with_no_marker(self, tmp_path):
        target = tmp_path / "complete.md"
        result = self._call("write_file", {"path": str(target), "content": CLEAN_DOCSTRING})

        assert "error" not in result, result
        assert target.read_text() == CLEAN_DOCSTRING

    def test_marker_tailed_patch_is_refused_and_leaves_the_file_unchanged(self, tmp_path):
        target = tmp_path / "patched.md"
        target.write_text("before\n")
        result = self._call("patch", {
            "path": str(target),
            "old_string": "before",
            "new_string": "after, and then the replacement text stops...[truncated]",
        })

        assert "error" in result, result
        assert "REFUSED" in result["error"]
        assert target.read_text() == "before\n", "the patch must not have applied"
