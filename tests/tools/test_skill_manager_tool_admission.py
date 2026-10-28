"""The pen-side admission gate, exercised through the tools that are actually called.

`tools/skill_admission.py` is the judgement; this file proves the WIRING — that a create which
restates an existing skill never reaches disk, that the refusal is actionable and recorded, and
that every path into a create (including the background-review/curator fork) meets the same
function. The Hub door is covered here too, because it is the other way a copy arrives.
"""

import json
import re
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.skill_manager_tool import _create_skill, _write_file, skill_manage

KANBAN = """\
---
name: kanban-transitions
description: Claim and finish kanban cards through their terminal states.
tier: 1
---

# Kanban card transitions

A card moves ready -> running -> done; the claim fence refuses a second worker and the review
column owns the verdict. Terminal states are done, blocked and archived.

## Claiming a card

Claim before writing, and heartbeat on long runs.

## Dispositions

Complete, request review, block with a reason, or request changes.
"""

DELIVERY = """\
---
name: delivery-windows
description: Declare and close delivery windows without a stranded pause.
tier: 1
---

# Delivery windows

Declare the window, arm the pause, broadcast each leg, then close the window and release the
pause. Scheduled jobs defer into a reserved band and resume after the window closes.

## Arming

The pause is a flag every scheduler reads before departure.

## Closing

Close is idempotent and releases whatever the window paused.
"""

# A different name and a different description, but the same promise and the same body.
RESTATEMENT = """\
---
name: card-movement
description: Claim and finish kanban cards through their terminal states.
---

# Card movement

A card moves ready -> running -> done; the claim fence refuses a second worker and the review
column owns the verdict. Terminal states are done, blocked and archived.

## Moving a card

Claim before writing, and heartbeat on long runs.

## Endings

Complete, request review, block with a reason, or request changes.
"""

# Below the stub line: refused on size, with no single skill owning it.
STUB = """\
---
name: tiny-note
description: Tiny.
---

# Tiny

One line about board cards.
"""

# Every sentence exists in one of two skills, neither owns 60% of it: a spread restatement.
SPREAD = """\
---
name: cards-and-windows
description: Cards, pauses and window closings.
---

# Cards and windows

A card moves ready -> running -> done; the claim fence refuses a second worker and the review
column owns the verdict. Terminal states are done, blocked and archived.

Declare the window, arm the pause, broadcast each leg, then close the window and release the
pause. Scheduled jobs defer into a reserved band and resume after the window closes.
"""


@contextmanager
def _skills_root(tmp_path):
    """Point the tool at a temp skills root; the ledger follows HERMES_HOME (conftest-pinned)."""
    root = tmp_path / "skills"
    root.mkdir()
    with patch("tools.skill_manager_tool.SKILLS_DIR", root), \
         patch("agent.skill_utils.get_all_skills_dirs", return_value=[root]):
        yield root


def _seed(root, name, content):
    d = root / "platform" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(content, encoding="utf-8")
    return d


def _ledger():
    from tools import skill_usage
    path = skill_usage._admission_refusal_file()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# --- The create gate -------------------------------------------------------------------

def test_a_create_that_restates_an_existing_skill_is_refused_and_writes_nothing(tmp_path):
    with _skills_root(tmp_path) as root:
        _seed(root, "kanban-transitions", KANBAN)
        result = _create_skill("card-movement", RESTATEMENT)
        assert result["success"] is False
        assert result["refused_by"] == "admission"
        assert result["admission"]["fold_target"] == "kanban-transitions"
        assert not (root / "platform" / "card-movement").exists()
        assert not (root / "card-movement").exists()


def test_the_refusal_names_the_skill_its_path_the_reference_file_and_the_rule(tmp_path):
    with _skills_root(tmp_path) as root:
        seeded = _seed(root, "kanban-transitions", KANBAN)
        message = _create_skill("card-movement", RESTATEMENT)["error"]
        assert "kanban-transitions" in message
        assert str(seeded / "SKILL.md") in message
        assert "references/card-movement.md" in message
        assert "60%" in message and "80%" in message
        assert "skill_manage(action='patch'" in message


def test_a_genuinely_new_skill_is_admitted_and_lands(tmp_path):
    from tools.skill_manager_tool import _find_skill

    with _skills_root(tmp_path) as root:
        _seed(root, "kanban-transitions", KANBAN)
        result = _create_skill("delivery-windows", DELIVERY)
        assert result["success"] is True, result
        found = _find_skill("delivery-windows")
        assert found and Path(found["path"], "SKILL.md").exists()


def test_the_refusal_is_recorded_in_the_ledger(tmp_path):
    with _skills_root(tmp_path) as root:
        _seed(root, "kanban-transitions", KANBAN)
        _create_skill("card-movement", RESTATEMENT)
        entries = [e for e in _ledger() if e["skill"] == "card-movement"]
        assert entries, "the refusal must be recorded, not just returned"
        assert entries[-1]["disposition"] == "fold"
        assert entries[-1]["existing_skill"] == "kanban-transitions"
        assert entries[-1]["reason"]


def test_the_ledger_records_the_skill_a_reject_names(tmp_path):
    """The record and the payload carry the same fact, or the ledger points nowhere."""
    with _skills_root(tmp_path) as root:
        _seed(root, "kanban-transitions", KANBAN)
        _seed(root, "delivery-windows", DELIVERY)
        result = _create_skill("tiny-note", STUB)
        entries = [e for e in _ledger() if e["skill"] == "tiny-note"]
    assert result["admission"]["disposition"] == "reject"
    assert entries and entries[-1]["existing_skill"] == result["admission"]["fold_target"]
    assert entries[-1]["existing_skill"]


_NAMED = re.compile(r"(?:Existing skill|Nearest skill): (\S+) \(")


@pytest.mark.parametrize("name,content,disposition", [
    ("card-movement", RESTATEMENT, "fold"),
    ("tiny-note", STUB, "reject"),
    ("cards-and-windows", SPREAD, "reject"),
])
def test_the_structured_refusal_carries_the_skill_the_message_names(
        tmp_path, name, content, disposition):
    """Prose and field must name the same skill — on EVERY disposition that names one.

    The message is what an agent reads; `admission.fold_target` is what automation reads. A
    refusal that names the skill only inside a sentence is a dead end for a consumer whose
    whole job on a refusal is "extend the skill you were told about", so the two are compared
    here rather than trusted to stay in step.
    """
    with _skills_root(tmp_path) as root:
        _seed(root, "kanban-transitions", KANBAN)
        _seed(root, "delivery-windows", DELIVERY)
        result = _create_skill(name, content)
        assert not (root / "platform" / name).exists()
        assert not (root / name).exists()
    assert result["success"] is False, result
    assert result["refused_by"] == "admission"
    assert result["admission"]["disposition"] == disposition
    named = _NAMED.search(result["error"])
    assert named, f"the message names no skill:\n{result['error']}"
    assert result["admission"]["fold_target"] == named.group(1)
    assert Path(result["admission"]["fold_path"]).is_file()


def test_the_curator_or_review_fork_cannot_bypass_the_gate(tmp_path):
    """Same function, same verdict: the write ORIGIN does not change admission."""
    from tools.skill_provenance import (
        BACKGROUND_REVIEW,
        reset_current_write_origin,
        set_current_write_origin,
    )

    token = set_current_write_origin(BACKGROUND_REVIEW)
    try:
        with _skills_root(tmp_path) as root:
            _seed(root, "kanban-transitions", KANBAN)
            result = skill_manage(action="create", name="card-movement", content=RESTATEMENT)
    finally:
        reset_current_write_origin(token)
    assert json.loads(result)["success"] is False
    assert json.loads(result)["refused_by"] == "admission"


def test_the_gate_can_be_switched_off_for_a_corpus_bootstrap(tmp_path):
    with _skills_root(tmp_path) as root, \
         patch("hermes_cli.config.load_config", return_value={"skills": {"admission_gate": False}}):
        _seed(root, "kanban-transitions", KANBAN)
        result = _create_skill("card-movement", RESTATEMENT)
        assert result["success"] is True, result


def test_the_gate_is_on_by_default(tmp_path):
    with _skills_root(tmp_path) as root, \
         patch("hermes_cli.config.load_config", return_value={}):
        _seed(root, "kanban-transitions", KANBAN)
        assert _create_skill("card-movement", RESTATEMENT)["success"] is False


def test_a_bug_in_the_judge_fails_open_rather_than_stopping_all_learning(tmp_path):
    with _skills_root(tmp_path) as root:
        _seed(root, "kanban-transitions", KANBAN)
        with patch("tools.skill_admission.verdict_for_content", side_effect=RuntimeError("boom")):
            result = _create_skill("card-movement", RESTATEMENT)
        assert result["success"] is True, result


# --- A fault is admitted, but never silently ---------------------------------------------
#
# Fail-open is the choice; an UNRECORDED fail-open is the defect. These pin the second half:
# an admission the gate never judged must leave a row that says so, or the ledger of a broken
# gate is byte-identical in meaning to the ledger of a gate that found nothing to refuse.

def _faults_since(before):
    return [e for e in _ledger()[before:] if e.get("disposition") == "fault"]


def test_a_judge_fault_leaves_a_fault_row_naming_what_could_not_be_judged(tmp_path):
    with _skills_root(tmp_path) as root:
        _seed(root, "kanban-transitions", KANBAN)
        before = len(_ledger())
        # A healthy judge still REFUSES this draft: the fault path launders nothing.
        refused = _create_skill("card-movement", RESTATEMENT)
        assert refused["success"] is False
        assert refused["refused_by"] == "admission"
        with patch("tools.skill_admission.verdict_for_content", side_effect=RuntimeError("boom")):
            result = _create_skill("card-movement", RESTATEMENT)
        assert result["success"] is True, result
        faults = _faults_since(before)
        assert len(faults) == 1, faults
        assert faults[0]["skill"] == "card-movement"
        assert "RuntimeError" in faults[0]["reason"] and "boom" in faults[0]["reason"]
        # A fault is not a refusal: it names no fold target, so no reader can fold into it.
        assert faults[0]["existing_skill"] == ""
        # ... and the refusal that a healthy judge wrote is still its own row, in the same ledger.
        assert [e["disposition"] for e in _ledger()[before:]] == ["fold", "fault"]


def test_a_support_file_judge_fault_leaves_a_fault_row_too(tmp_path):
    with _skills_root(tmp_path) as root:
        _seed(root, "kanban-transitions", KANBAN)
        own = _seed(root, "delivery-windows", DELIVERY)
        before = len(_ledger())
        with patch("tools.skill_admission.verdict_for_support_file",
                   side_effect=RuntimeError("boom")):
            result = _write_file("delivery-windows", "references/broadcast.md",
                                 "# Broadcast legs\n\nEach leg is announced once, and a failed leg "
                                 "leaves the window armed so the next pass resumes it.\n")
        assert result["success"] is True, result
        assert (own / "references" / "broadcast.md").exists()
        faults = _faults_since(before)
        assert [f["skill"] for f in faults] == ["delivery-windows/references/broadcast.md"]


def test_a_corpus_fallback_names_the_root_it_used_and_records_one_fault(tmp_path):
    from tools.skill_manager_tool import _admission_corpus_roots

    with _skills_root(tmp_path) as root:
        before = len(_ledger())
        with patch("agent.skill_utils.get_all_skills_dirs", side_effect=OSError("no roots")), \
             patch("tools.skill_manager_tool._CORPUS_FALLBACK_RECORDED", False):
            first = _admission_corpus_roots()
            second = _admission_corpus_roots()
        assert first == [root] and second == [root]
        faults = [f for f in _faults_since(before) if f["skill"] == "admission-corpus"]
        assert len(faults) == 1, faults
        assert str(root) in faults[0]["reason"]
        assert faults[0]["corpus"] == str(root)


# --- New support files ------------------------------------------------------------------

def test_a_new_reference_file_that_restates_another_skill_is_refused(tmp_path):
    with _skills_root(tmp_path) as root:
        _seed(root, "kanban-transitions", KANBAN)
        own = _seed(root, "delivery-windows", DELIVERY)
        result = _write_file("delivery-windows", "references/kanban.md", KANBAN)
        assert result["success"] is False
        assert result["refused_by"] == "admission"
        assert result["admission"]["fold_target"] == "kanban-transitions"
        assert not (own / "references" / "kanban.md").exists()


def test_a_new_reference_file_with_new_depth_is_written(tmp_path):
    with _skills_root(tmp_path) as root:
        _seed(root, "kanban-transitions", KANBAN)
        own = _seed(root, "delivery-windows", DELIVERY)
        result = _write_file("delivery-windows", "references/broadcast.md",
                             "# Broadcast legs\n\nEach leg is announced once, and a failed leg "
                             "leaves the window armed so the next pass resumes it.\n")
        assert result["success"] is True, result
        assert (own / "references" / "broadcast.md").exists()


# --- The Hub door -----------------------------------------------------------------------

def _bundle(tmp_path, name, content):
    bundle = tmp_path / "quarantine" / name
    bundle.mkdir(parents=True)
    (bundle / "SKILL.md").write_text(content, encoding="utf-8")
    return bundle


def test_a_hub_bundle_that_duplicates_a_local_skill_is_refused(tmp_path):
    from tools.skills_hub_install import _check_admission

    with _skills_root(tmp_path) as root:
        _seed(root, "kanban-transitions", KANBAN)
        bundle = _bundle(tmp_path, "card-movement", RESTATEMENT)
        install_dir = root / "platform" / "card-movement"
        try:
            _check_admission(install_dir, "card-movement", bundle)
        except ValueError as e:
            assert "kanban-transitions" in str(e)
            assert "references/card-movement.md" in str(e)
        else:
            raise AssertionError("a duplicating hub bundle must be refused")


def test_a_genuinely_new_hub_bundle_installs(tmp_path):
    from tools.skills_hub_install import _check_admission

    with _skills_root(tmp_path) as root:
        _seed(root, "kanban-transitions", KANBAN)
        bundle = _bundle(tmp_path, "delivery-windows", DELIVERY)
        _check_admission(root / "platform" / "delivery-windows", "delivery-windows", bundle)


def test_a_hub_bundle_judge_fault_installs_and_leaves_a_fault_row(tmp_path):
    from tools.skills_hub_install import _check_admission

    with _skills_root(tmp_path) as root:
        _seed(root, "kanban-transitions", KANBAN)
        bundle = _bundle(tmp_path, "card-movement", RESTATEMENT)
        before = len(_ledger())
        with patch("tools.skill_admission.verdict_for_content", side_effect=RuntimeError("boom")):
            _check_admission(root / "platform" / "card-movement", "card-movement", bundle)
        faults = _faults_since(before)
        assert [f["skill"] for f in faults] == ["card-movement"]
        assert "RuntimeError" in faults[0]["reason"]


def test_an_update_of_an_installed_skill_is_not_a_collision(tmp_path):
    from tools.skills_hub_install import _check_admission

    with _skills_root(tmp_path) as root:
        installed = _seed(root, "delivery-windows", DELIVERY)
        bundle = _bundle(tmp_path, "delivery-windows", DELIVERY)
        # The corpus excludes the target directory: a skill updating itself is not a duplicate.
        _check_admission(installed, "delivery-windows", bundle)
