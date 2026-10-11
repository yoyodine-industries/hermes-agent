"""The agent-tool surface of the bulk rule (card t_bf9605f8).

The operator's 2026-09-27 rule is enforced by classification at the CLI seam. A kanban TOOL call
never passes that seam, so `tools/kanban_tools.py` maps a bulk-capable tool name to the guard's own
verb and gates the call in the single tool funnel — the gate is therefore structural, and these
tests are what keeps it honest:

* every mapped tool names a verb the guard classifies as a destructive BULK action;
* no shipped tool looks bulk-capable while sitting outside the map (the hole this wiring closes);
* an unapproved bulk-capable call is REFUSED with the same remedy the CLI prints;
* a call carrying the real approval bundle for its exact action passes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_bulk_guard as kbg
from hermes_cli import kanban_db as kb
from tests.hermes_cli.bulk_guard_admission import admit
from tools import kanban_tools as kt


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME + ledger so the gate never reads the host's own state."""
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.setenv(kbg.LEDGER_DIR_ENV, str(tmp_path / "bulk_actions"))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    kb.init_db()
    return root


def test_every_mapped_tool_names_a_bulk_verb():
    """A tool is gated only if the guard owns its verb as a bulk surface."""
    assert kt._BULK_TOOL_VERBS, "the tool map is empty: the tool surface would be ungated"
    for tool_name, verb in kt._BULK_TOOL_VERBS.items():
        assert verb in kbg.BULK_SURFACES, (
            f"{tool_name} maps to `{verb}`, which the guard does not own as a bulk surface"
        )


def test_no_shipped_tool_is_bulk_capable_outside_the_map():
    """The registry itself must not carry a bulk-capable tool the gate does not know about."""
    names = {entry[0] for entry in kt._TOOLS}
    suspect = {n for n in names if any(k in n for k in ("archive", "gc", "purge", "repair", "swarm"))}
    assert suspect <= set(kt._BULK_TOOL_VERBS), (
        f"bulk-capable-looking tool(s) missing from _BULK_TOOL_VERBS: "
        f"{sorted(suspect - set(kt._BULK_TOOL_VERBS))}"
    )


def test_an_unapproved_bulk_tool_call_is_refused_with_the_remedy(home):
    """No bundle, no sweep — and the refusal says exactly what to file."""
    with pytest.raises(kt._Reject) as excinfo:
        kt._gate_bulk_tool("kanban_gc", {})
    message = excinfo.value.args[0]
    assert "kanban_gc refused" in message
    assert "bulk_unapproved" in message
    assert "bulk-approvals ask" in message


def test_a_bulk_tool_call_with_the_bundle_for_its_exact_action_passes(home, tmp_path, monkeypatch):
    """The same call, admitted the way the estate admits it, is not refused."""
    verb = "gc"
    params = kt._bulk_tool_params(verb, {})
    board = kb.get_current_board()
    digest = admit(monkeypatch, tmp_path, board=board, verb=verb, scope=kbg.classify(verb, params))

    assert kt._gate_bulk_tool("kanban_gc", {"approval": digest}) is None


def test_the_gate_ignores_an_unmapped_tool(home):
    """Only mapped tools are gated — the map, not the tool name, decides (no false refusals)."""
    assert kt._gate_bulk_tool("kanban_comment", {}) is None
    assert kt._gate_bulk_tool("kanban_complete", {}) is None
