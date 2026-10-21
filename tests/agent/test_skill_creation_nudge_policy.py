"""Every prompt that authorises a skill CREATE must carry the admission gate.

The defect class (t_6c0fb5f2): four separate prompts told the model to create skills — the
background-review prompt, the combined memory+skill review prompt, the curator's umbrella path,
and a nudge in the live ``## Skills`` system prompt that offered to "save as a skill" after any
difficult task — and none of them said a create can be REFUSED. A create is now refused
deterministically by ``tools/skill_admission.py``, so:

  * a nudge that does not mention the refusal is a false promise, and
  * a nudge that says "create" without the extend-first rule is an inflow pump — it manufactures
    exactly the narrow siblings the gate then refuses.

Two layers of test:

  1. a STRUCTURAL guard: any paragraph in any of these prompts that instructs a create must also
     name the gate/refusal in the SAME paragraph, so a later edit cannot reintroduce a bare create
     instruction without failing this suite;
  2. content assertions pinning the policy sentences that carry the reason (so a reword that
     keeps the shape but drops the meaning still fails).
"""

from __future__ import annotations

import pytest

from agent.background_review import (
    _COMBINED_REVIEW_PROMPT,
    _MEMORY_REVIEW_PROMPT,
    _SKILL_REVIEW_PROMPT,
)
from agent.curator import CURATOR_REVIEW_PROMPT
from agent.prompt_builder import SKILLS_GUIDANCE, build_skills_system_prompt

# A paragraph that carries one of these authorises a new skill.
CREATE_MARKERS = ("action=create", "CREATE A NEW", "create a new skill", "a new skill only when")
# ... and it must also say where the decision comes from.
GATE_MARKERS = ("admission", "gate", "refus")

PROMPTS = {
    "_SKILL_REVIEW_PROMPT": _SKILL_REVIEW_PROMPT,
    "_COMBINED_REVIEW_PROMPT": _COMBINED_REVIEW_PROMPT,
    "CURATOR_REVIEW_PROMPT": CURATOR_REVIEW_PROMPT,
}


def _paragraphs(text: str) -> list[str]:
    return [p for p in text.split("\n\n") if p.strip()]


def _create_paragraphs(text: str) -> list[str]:
    return [p for p in _paragraphs(text) if any(m in p for m in CREATE_MARKERS)]


class TestNoBareCreateInstruction:
    """The structural guard: no prompt may tell the model to create without naming the gate."""

    @pytest.mark.parametrize("name", sorted(PROMPTS))
    def test_prompt_has_a_create_paragraph(self, name):
        # If a prompt stops authorising creates at all, this test tells the next reader that the
        # guard below became vacuous — that is a deliberate change, not an accident.
        assert _create_paragraphs(PROMPTS[name]), "%s no longer authorises any create" % name

    @pytest.mark.parametrize("name", sorted(PROMPTS))
    def test_every_create_paragraph_names_the_gate(self, name):
        bare = [p for p in _create_paragraphs(PROMPTS[name])
                if not any(m in p.lower() for m in GATE_MARKERS)]
        assert not bare, (
            "%s authorises a create without naming the admission gate/refusal in the same "
            "paragraph:\n\n%s" % (name, "\n\n---\n\n".join(bare)))


class TestSkillReviewPrompt:
    def test_gate_thresholds_are_stated(self):
        for needle in (">=60%", ">=80%", "15% new"):
            assert needle in _SKILL_REVIEW_PROMPT, needle

    def test_extend_is_the_default_action(self):
        assert "the default action is a PATCH" in _SKILL_REVIEW_PROMPT

    def test_a_refusal_is_an_answer_not_an_obstacle(self):
        assert "A refusal is the ANSWER, not an obstacle" in _SKILL_REVIEW_PROMPT
        # ... and the way a model used to dodge the gate is named and closed.
        assert "Never retry the same draft under a new name, a reworded description, or a " \
               "narrower trigger" in _SKILL_REVIEW_PROMPT

    def test_create_requires_naming_the_skill_it_checked(self):
        assert "your reply must name the existing skill you checked and what it does not cover" \
               in _SKILL_REVIEW_PROMPT

    def test_create_item_4_is_gate_linked(self):
        item4 = next(p for p in _paragraphs(_SKILL_REVIEW_PROMPT) if "4. CREATE A NEW" in p)
        assert "admission gate above admits it" in item4
        assert "Your reply names the skill you checked" in item4


class TestCombinedReviewPrompt:
    def test_gate_thresholds_and_refusal_path(self):
        for needle in (">=60%", ">=80%", "15% new", "A refusal is the ANSWER, not an obstacle"):
            assert needle in _COMBINED_REVIEW_PROMPT, needle

    def test_create_requires_naming_the_skill_it_checked(self):
        assert "naming in your reply the existing skill you checked and what it does not cover" \
               in _COMBINED_REVIEW_PROMPT

    def test_retry_ban_is_stated(self):
        assert "never retry the same draft under a new name, a reworded description, or a " \
               "narrower trigger" in _COMBINED_REVIEW_PROMPT

    def test_create_item_4_is_gate_linked(self):
        item4 = next(p for p in _paragraphs(_COMBINED_REVIEW_PROMPT) if "4. CREATE A NEW" in p)
        assert "admission gate admits it" in item4


class TestMemoryReviewPrompt:
    """Memory is the other store: it must not become a second (ungated) skill library."""

    def test_procedures_route_to_the_governing_skill(self):
        assert "belongs in the skill that governs that task" in _MEMORY_REVIEW_PROMPT

    def test_nothing_to_save_is_the_normal_outcome(self):
        assert "'Nothing to save.'" in _MEMORY_REVIEW_PROMPT
        assert "not a missed one" in _MEMORY_REVIEW_PROMPT

    def test_memory_prompt_never_authorises_creating_a_skill(self):
        assert not _create_paragraphs(_MEMORY_REVIEW_PROMPT)


class TestCuratorPrompt:
    def test_umbrella_create_path_is_gate_linked(self):
        way_b = next(p for p in _paragraphs(CURATOR_REVIEW_PROMPT) if "b. CREATE A NEW UMBRELLA" in p)
        assert "This path is ADMISSION-GATED" in way_b
        assert "A refusal is the ANSWER, not an obstacle" in way_b
        # The curator's dodge would be re-naming the umbrella to slip past the gate.
        assert "Never rename the umbrella, reword its description, or narrow its trigger" in way_b

    def test_toolset_line_flags_create_as_gated(self):
        line = next(ln for ln in CURATOR_REVIEW_PROMPT.split("\n")
                    if ln.strip().startswith("- skill_manage action=create"))
        assert "admission-gated" in line and "refusal names" in line

    def test_volume_quota_is_bounded_by_state(self):
        # "fewer than 10 archives means you stopped too early" was an unbounded volume command.
        assert "do not merge for the count" in CURATOR_REVIEW_PROMPT


@pytest.fixture()
def interactive_skills_prompt(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_SINGLE_QUERY_SESSION", raising=False)
    d = tmp_path / "skills" / "misc" / "demo-skill"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Demo skill for tests.\n---\n# Demo\n", encoding="utf-8")
    return build_skills_system_prompt(
        available_tools={"skill_manage", "skill_view", "skills_list"},
        skills_dir_override=tmp_path / "skills",
    )


class TestLiveSystemPromptNudge:
    """The nudge every session sees — not only the background reviewer."""

    def test_old_bare_create_pump_is_gone(self, interactive_skills_prompt):
        assert "offer to save as a skill" not in interactive_skills_prompt

    def test_nudge_leads_with_the_patch(self, interactive_skills_prompt):
        assert "offer to record what it taught" in interactive_skills_prompt
        assert "a patch to that skill first" in interactive_skills_prompt

    def test_nudge_names_the_refusal(self, interactive_skills_prompt):
        assert "refused by the admission gate" in interactive_skills_prompt

    def test_skills_guidance_records_into_the_governing_skill(self):
        assert "patch the skill that governs the task" in SKILLS_GUIDANCE
        # The bare "record it for future reuse" nudge is what this replaced.
        assert "record it with skill_manage for future reuse" not in SKILLS_GUIDANCE

    def test_skills_guidance_avoids_the_filter_rejected_phrasing(self):
        # #82154: this construction reproduced a billing-shaped HTTP 400 on subscription OAuth.
        assert "save the approach as a skill" not in SKILLS_GUIDANCE
