"""The work-carrier gate: loop-shaped work may not be filed as a standalone card.

The defect this pins (platform-stl ruling 2026-11-05): an agent with multi-phase work reaches
for `kanban create` because a card is the reflex, and the work then lives as a row nobody
loops over. The gate refuses at the filing door, before a row is written.
"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_work_carrier_gate as gate


@pytest.mark.parametrize("title,body,expect_hit", [
    ("Standing filing keys -- PFK-1.2 shared-tree filer", "", "PFK-1.2"),
    ("Fix the filers", "L2/L3 detail lives in the project", "LOOPING/" if False else "L2"),
    ("Board hygiene", "write the phases into LOOPING/task_list.md", "LOOPING/"),
    ("Do the thing", "workstream 3: the deploy proof", "workstream"),
    ("Milestone: all four filers keyed", "", "Milestone"),
    ("Ship it", "phase 2 of the rollout", "phase 2"),
    ("Ship it", "L1: standing keys\nL2: per filer", "L1:"),
])
def test_loop_artefacts_are_refused_without_a_project(title, body, expect_hit):
    refusal = gate.check(title=title, body=body)
    assert refusal, f"{title!r} / {body!r} should be refused"
    assert gate.REFUSAL_CODE in refusal
    assert expect_hit in refusal


def test_a_project_anchors_the_card_and_nothing_is_refused():
    """The project IS the loop; a card bound to it is the ONE card per L1."""
    assert gate.check(title="PFK-1.2 shared-tree filer", body="workstream 2",
                      project="standing-filing-keys") is None


def test_a_declared_finding_gets_through():
    """A defect that merely quotes loop vocabulary declares its carrier, auditably."""
    body = "The milestone badge renders twice.\n\nCARRIER: card\n"
    assert gate.check(title="Milestone badge renders twice", body=body) is None


def test_plain_findings_are_untouched():
    """No loop artefact, no refusal: the gate must not tax ordinary filing."""
    assert gate.check(title="deploy-train filed 34 cards for one gate",
                      body="the same owner behind the same gate, once per departure") is None


def test_the_refusal_names_the_real_commands():
    """A remedy must be executable: the missing-verb defect (t_9e1e4d1d) is not repeated."""
    refusal = gate.check(title="Board hygiene", body="see LOOPING/task_list.md")
    assert refusal
    assert "hermes project create" in refusal
    assert "--project" in refusal
    assert "CARRIER: card" in refusal
