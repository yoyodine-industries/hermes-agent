"""Per-task skills must be loadable by the assignee profile's worker (card t_477bd4eb).

A worker is launched with ``--skills <name>`` and resolves the name against the ASSIGNEE
profile's skill directories. A name it cannot load aborts the session before the agent
starts (``Unknown skill(s)``), so the card burns an attempt and auto-blocks after
``kanban.failure_limit``, with the reason readable only in the worker log. Refusing the
name where the card is created — and wherever an assignee is ATTACHED afterwards — keeps
that failure out of the queue entirely.

Restore of the fork lineage's gate (b4a5e76c4b and its predecessors), which the 2026-09-23
checkout switch dropped from the live line. It lives in its own module because the live
line's ``tests/hermes_cli/test_kanban_db.py`` already sits at its own size ceiling
(``scripts/check --only health``, FILE_LINES), so new tests go in a new file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB (the shape ``test_kanban_db.py`` uses)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _write_skill(skills_dir: Path, name: str) -> None:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\n---\n\n# {name}\n", encoding="utf-8")


@pytest.fixture
def profile_skills(kanban_home, monkeypatch):
    """``kanban_home`` plus two named profiles that each own a different skill."""
    from hermes_cli import profiles

    profiles_root = kanban_home / "profiles"
    homes = {}
    for profile in ("demo", "other"):
        home = profiles_root / profile
        (home / "skills").mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("{}\n", encoding="utf-8")
        homes[profile] = home
    _write_skill(homes["demo"] / "skills", "demo-skill")
    _write_skill(homes["other"] / "skills", "other-skill")
    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: kanban_home)
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: profiles_root)
    return homes


def _task(conn, tid):
    """``get_task`` with the missing-row case failing loudly (also narrows for type checkers)."""
    task = kb.get_task(conn, tid)
    assert task is not None
    return task


# ---------------------------------------------------------------------------
# The create-time half: a name judged against the profile it will run as
# ---------------------------------------------------------------------------


def test_create_task_refuses_skill_the_assignee_profile_cannot_load(kanban_home, profile_skills):
    """A name no worker for that profile can load is refused, with the closest name."""
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError) as excinfo:
            kb.create_task(conn, title="typo", assignee="demo", skills=["demo-skil"])
        message = str(excinfo.value)
        assert "demo-skil" in message
        assert "demo" in message, "refusal must name the profile the names are judged against"
        assert "demo-skill" in message, "refusal must point at the closest registered name"
        assert kb.list_tasks(conn) == [], "a refused card must not reach the board"


def test_create_task_skill_check_is_scoped_to_the_assignee_profile(kanban_home, profile_skills):
    """``other-skill`` is registered — but for another profile, so `demo`'s worker can't load it."""
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError):
            kb.create_task(conn, title="wrong profile", assignee="demo", skills=["other-skill"])
        tid = kb.create_task(conn, title="right profile", assignee="other", skills=["other-skill"])
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.skills == ["other-skill"]


def test_create_task_refuses_disabled_skill(kanban_home, profile_skills):
    """A registered-but-disabled name is unloadable for the worker, so it is refused too."""
    (profile_skills["demo"] / "config.yaml").write_text(
        "skills:\n  disabled: [demo-skill]\n", encoding="utf-8")
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError) as excinfo:
            kb.create_task(conn, title="disabled", assignee="demo", skills=["demo-skill"])
    assert "disabled" in str(excinfo.value).lower()


def test_create_task_skill_check_skips_when_no_profile_is_resolvable(kanban_home, profile_skills):
    """No assignee means no dispatch; an unknown profile is its own error. Neither
    has a registry to judge names against, so neither produces a SKILL verdict.

    The unresolvable-profile arm used to route through ``assign_task``, because the
    assign door had no assignee-resolution gate (only the create door did, and that
    verdict is an assignee-resolution one, not the skill verdict this test pins).
    Since the assign door carries ``gate_assignee`` too (ruling ESC-01650 /
    platform/t_52183ba9, card t_041d673a), an unresolvable handle can no longer be
    ATTACHED at all — so the arm now asserts exactly that: the refusal is the
    assignee-resolution verdict, the card keeps its unjudged names, and no skill
    verdict is ever produced for a profile that cannot exist.
    """
    with kbc.connect_closing() as conn:
        unassigned = kb.get_task(conn, kb.create_task(conn, title="unassigned", skills=["nope"]))
        assert unassigned is not None
        assert unassigned.skills == ["nope"]
        with pytest.raises(ValueError) as excinfo:
            kb.assign_task(conn, unassigned.id, "ghost")
        assert getattr(excinfo.value, "invariant", None) == "assignee", (
            "an unresolvable assignee is the ASSIGNEE verdict, never a skill verdict")
        stuck = _task(conn, unassigned.id)
        assert stuck.assignee in (None, "")
        assert stuck.skills == ["nope"], "an unresolvable profile judges no skill names"


# ---------------------------------------------------------------------------
# …and again wherever an assignee is ATTACHED to a card that already has skills
#
# The create-time check above is scoped to a profile, so a card created with no
# assignee passes it with its names unjudged. Assigning later is the other half
# of the same question: the pair (assignee, skills) is what the launched worker
# gets, so it is judged at every site that writes an assignee — `assign_task`,
# `request_review` (which reassigns to the reviewer) and the dispatcher's
# `kanban.default_assignee` fallback.
# ---------------------------------------------------------------------------


def test_assign_task_refuses_skill_the_target_profile_cannot_load(kanban_home, profile_skills):
    """A card created unassigned must not be able to reach a worker with names
    its new owner cannot load — refused at the assign, with the closest name."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="typo", skills=["demo-skil"])
        before = _task(conn, tid)
        with pytest.raises(ValueError) as excinfo:
            kb.assign_task(conn, tid, "demo")
        message = str(excinfo.value)
        assert "demo-skil" in message
        assert "demo" in message, "refusal must name the profile the names are judged against"
        assert "demo-skill" in message, "refusal must point at the closest registered name"
        after = _task(conn, tid)
        assert after.assignee is None, "a refused assign must not be written"
        assert after.status == before.status, "a refused assign must not move the card"
        assert [e for e in kb.list_events(conn, tid) if e.kind == "assigned"] == []


def test_assign_task_judges_skills_against_the_profile_being_attached(kanban_home, profile_skills):
    """``other-skill`` is registered — for ``other``. Reassigning to ``demo`` hands
    the card to a worker that cannot load it, so that reassign is refused too."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="cross profile", assignee="other", skills=["other-skill"])
        with pytest.raises(ValueError):
            kb.assign_task(conn, tid, "demo")
        assert _task(conn, tid).assignee == "other", "the refused reassign keeps the owner"
        assert kb.assign_task(conn, tid, "other") is True


def test_assign_task_allows_skills_the_target_profile_can_load(kanban_home, profile_skills):
    """The check must not over-refuse: names the target's worker can load assign fine."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="fine", skills=["demo-skill"])
        assert kb.assign_task(conn, tid, "demo") is True
        assert _task(conn, tid).assignee == "demo"


def test_assign_task_still_unassigns_a_card_with_unloadable_skills(kanban_home, profile_skills):
    """Clearing the assignee stays available: no worker, so nothing to load — this
    is the operator's way out of a bad pair without deleting the card."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="escape hatch", assignee="other", skills=["other-skill"])
        assert kb.assign_task(conn, tid, None) is True
        assert _task(conn, tid).assignee is None


def test_assign_task_allows_a_terminal_card_despite_unloadable_skills(kanban_home, profile_skills):
    """The gate itself (b4a5e76c4b): a finished card never launches a worker again,
    so the names stored on it cannot fail to load — assigning one is attribution on
    a closed card, not a dispatch hazard, and must not be refused. Both terminal
    statuses, because ``done`` cards get archived and the assign has to keep
    working across that."""
    with kbc.connect_closing() as conn:
        done_id = kb.create_task(conn, title="shipped", skills=["other-skill"])
        assert kb.complete_task(conn, done_id, result="shipped") is True
        assert _task(conn, done_id).status == "done"
        assert kb.assign_task(conn, done_id, "demo") is True
        assert _task(conn, done_id).assignee == "demo"
        assert [e for e in kb.list_events(conn, done_id) if e.kind == "assigned"] != [], (
            "the assign on a terminal card is recorded, not silently skipped")

        archived_id = kb.create_task(conn, title="shelved", skills=["other-skill"])
        assert kb.archive_task(conn, archived_id, reason="superseded") is True
        assert _task(conn, archived_id).status == "archived"
        assert kb.assign_task(conn, archived_id, "demo") is True
        assert _task(conn, archived_id).assignee == "demo"


def test_assign_task_refuses_a_non_terminal_card_with_the_same_pair(kanban_home, profile_skills):
    """The other arm of the SAME pair: identical skills, identical assignee, only the
    status differs from the terminal case above — so the gate, not the name, is what
    decides. Without this arm the terminal test would pass for a tree with no check
    at all."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="open", skills=["other-skill"])
        assert _task(conn, tid).status == "ready"
        with pytest.raises(ValueError) as excinfo:
            kb.assign_task(conn, tid, "demo")
        assert "other-skill" in str(excinfo.value)
        assert _task(conn, tid).assignee is None


def test_request_review_refuses_reviewer_that_cannot_load_the_cards_skills(kanban_home, profile_skills):
    """A review handoff reassigns the card to the reviewer, so the reviewer is
    judged like any other assignee — and a refusal leaves the card running."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="reviewer", assignee="demo", skills=["demo-skill"])
        kb.claim_task(conn, tid)
        run_id = _task(conn, tid).current_run_id
        assert run_id is not None
        refused = kb.request_review(
            conn, tid, reviewer="other", expected_run_id=run_id, with_reason=True)
        assert isinstance(refused, tuple), refused
        ok, reason = refused
        assert ok is False
        assert reason and "demo-skill" in reason
        assert "other-skill" in reason, "reason must point at the closest name for the reviewer"
        task = _task(conn, tid)
        assert task.status == "running", "a refused handoff must not move the card"
        assert task.assignee == "demo"
        assert [e for e in kb.list_events(conn, tid) if e.kind == "review_requested"] == []


def test_request_review_allows_reviewer_that_can_load_the_cards_skills(kanban_home, profile_skills):
    """The reviewer check must not over-refuse: a reviewer that can load the names
    takes the card as before."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="reviewer ok", assignee="other", skills=["other-skill"])
        kb.claim_task(conn, tid)
        run_id = _task(conn, tid).current_run_id
        assert run_id is not None
        assert kb.request_review(conn, tid, reviewer="other", expected_run_id=run_id) is True
        task = _task(conn, tid)
        assert task.status == "review"
        assert task.assignee == "other"


def test_default_assignee_refuses_a_card_whose_skills_the_default_cannot_load(
    kanban_home, profile_skills, caplog,
):
    """``kanban.default_assignee`` is a config value with no operator in the loop,
    so this attach is refused with a log instead of a raise — and is NOT written:
    the card does not get dispatched into a crash-loop it cannot diagnose."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="unjudged", skills=["demo-skil"])
    with kbc.connect_closing() as conn:
        with caplog.at_level("WARNING"):
            assert kbd._apply_default_assignee(conn, tid, "demo", dry_run=False) is False
        assert _task(conn, tid).assignee is None, "the assign must not be written"
    assert any("default_assignee" in r.getMessage() for r in caplog.records), caplog.text


def test_default_assignee_applies_when_the_default_can_load_the_skills(kanban_home, profile_skills):
    """The dispatcher fallback keeps working for a pair that is loadable, and for a
    card that carries no skills at all (the common case)."""
    with kbc.connect_closing() as conn:
        loadable = kb.create_task(conn, title="loadable", skills=["demo-skill"])
        plain = kb.create_task(conn, title="plain")
    with kbc.connect_closing() as conn:
        assert kbd._apply_default_assignee(conn, loadable, "demo", dry_run=False) is True
        assert kbd._apply_default_assignee(conn, plain, "demo", dry_run=False) is True
        assert _task(conn, loadable).assignee == "demo"
        assert _task(conn, plain).assignee == "demo"
