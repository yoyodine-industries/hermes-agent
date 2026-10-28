"""Tests for tools/skill_admission.py — the pen-side admission judgement.

The rule under test is the publish door's (`yaan-platform:scripts/skills_publish_nodes.py`):
similarity on the INTENT SURFACE, materiality against the context the skill costs on every
load, and exactly one disposition out of `new` / `fold into <skill>` / `reject`. The numeric
constants are asserted literally here so a drift on THIS side fails even where the door's
checkout is absent; `tests/conformance/test_skill_admission_door_parity.py` runs the same
fixtures through both implementations.
"""

import json

import pytest

from tools import skill_admission as adm


def _skill(name, description, body, tier=None):
    lines = ["---", f"name: {name}", f"description: {description}"]
    if tier is not None:
        lines.append(f"tier: {tier}")
    lines += ["---", "", body, ""]
    return "\n".join(lines)


KANBAN_BODY = """\
# Kanban card transitions

A card moves ready -> running -> done; the claim fence refuses a second worker and the review
column owns the verdict. Terminal states are done, blocked and archived.

## Claiming a card

Claim before writing, and heartbeat on long runs.

## Dispositions

Complete, request review, block with a reason, or request changes.
"""

POSTGRES_BODY = """\
# Postgres migration

Add a migration file, run it forward, verify the schema, and never edit a shipped migration.
The advisory lock prevents two runners applying the same version.

## Writing the migration

Number it, keep it idempotent, and add the rollback.

## Verifying

Read the schema back and diff it against the model.
"""

DELIVERY_BODY = """\
# Delivery windows

Declare the window, arm the pause, broadcast each leg, then close the window and release the
pause. Scheduled jobs defer into a reserved band and resume after the window closes.

## Arming

The pause is a flag every scheduler reads before departure.

## Closing

Close is idempotent and releases whatever the window paused.
"""


SPREAD_BODY = """

A card moves ready running done. Claim the card and finish it; the fence refuses a second worker.

Write a postgres migration and verify it. Postgres migrations: write and verify.

Declare the window, arm the pause, close the window and release the pause.

Dispositions: claim, review, finish, done. The review column owns the verdict.

The migration and the schema verify. Writing and verifying.

Arming and closing for the window. The pause: declare and release.

Kanban card transitions: claiming, dispositions, terminal states.

Postgres migration: writing, verifying, and the schema.

Delivery windows: arming, closing, and the pause.
"""


def _corpus(tmp_path, skills):
    for name, (description, body) in skills.items():
        d = tmp_path / name
        d.mkdir()
        (d / "SKILL.md").write_text(_skill(name, description, body), encoding="utf-8")
    return tmp_path


# --- The constants are the door's, literally ------------------------------------------

def test_the_fold_and_duplicate_lines_are_the_doors():
    assert adm.ADMISSION_FOLD_COVERAGE == 0.60
    assert adm.ADMISSION_DESCRIPTION_DUPLICATE == 0.80
    assert adm.ADMISSION_MIN_NOVELTY == 0.15
    assert adm.ADMISSION_STUB_BYTES == 200
    assert adm.ADMISSION_MATCHES == 3
    assert adm.ADMISSION_SKIP_DIRS == frozenset({"absorbed", "depth", "scripts", "_shared", "docs"})


# --- The intent surface ----------------------------------------------------------------

def test_frontmatter_reads_description_and_nested_tier():
    fm, body = adm.frontmatter(_skill("x", "Does a thing.", "# X\n\nBody.", tier="2"))
    assert fm["description"] == "Does a thing."
    assert fm["tier"] == "2"
    assert body.strip().startswith("# X")


def test_frontmatter_joins_a_folded_description():
    raw = "---\nname: x\ndescription: >\n  Two lines\n  of promise.\n---\n\n# X\n"
    fm, _ = adm.frontmatter(raw)
    assert fm["description"] == "Two lines of promise."


def test_no_frontmatter_is_not_a_crash():
    fm, body = adm.frontmatter("# Just a heading\n")
    assert fm == {}
    assert "Just a heading" in body


def test_intent_surface_is_description_opening_and_headings_only():
    fm, body = adm.frontmatter(_skill("x", "Claim and finish kanban cards.", KANBAN_BODY))
    surface = adm.intent_text(fm, body)
    assert "kanban" in surface.lower()
    assert "claiming a card" in surface.lower()          # heading
    assert "dispositions" in surface.lower()             # heading
    # A later section's prose is depth, not intent: it must not widen the surface.
    assert "request changes" not in surface.lower()


def test_intent_tokens_drop_filler_and_short_tokens():
    tokens = adm.intent_tokens("The card is an id and must move to done")
    assert "card" in tokens and "done" in tokens
    assert "the" not in tokens and "is" not in tokens


# --- The dispositions -----------------------------------------------------------------

def test_a_draft_that_restates_one_skill_folds_into_it(tmp_path):
    corpus = _corpus(tmp_path, {"kanban-transitions": ("Claim and finish kanban cards.", KANBAN_BODY)})
    verdict = adm.verdict_for_content(
        "card-movement", _skill("card-movement", "Claim and finish kanban cards.", KANBAN_BODY), [corpus])
    assert verdict["disposition"] == "fold"
    assert verdict["fold_target"] == "kanban-transitions"
    assert verdict["fold_path"].endswith("kanban-transitions/SKILL.md")
    assert verdict["novelty"] < adm.ADMISSION_MIN_NOVELTY
    assert verdict["matches"][0]["coverage"] >= adm.ADMISSION_FOLD_COVERAGE


def test_a_duplicate_description_alone_folds(tmp_path):
    corpus = _corpus(tmp_path, {"kanban-transitions": ("Claim and finish kanban cards.", KANBAN_BODY)})
    # Same promise, different words in the body.
    draft = _skill("board-ops", "Claim and finish kanban cards.",
                   "# Board ops\n\n" + "\n".join("unique%d alpha beta gamma" % i for i in range(30)))
    verdict = adm.verdict_for_content("board-ops", draft, [corpus])
    assert verdict["disposition"] == "fold"
    assert verdict["matches"][0]["description_overlap"] >= adm.ADMISSION_DESCRIPTION_DUPLICATE


def test_a_genuinely_new_skill_is_admitted(tmp_path):
    corpus = _corpus(tmp_path, {
        "kanban-transitions": ("Claim and finish kanban cards.", KANBAN_BODY),
        "postgres-migration": ("Write and verify postgres migrations.", POSTGRES_BODY)})
    verdict = adm.verdict_for_content(
        "delivery-windows", _skill("delivery-windows", "Declare and close delivery windows.",
                                   DELIVERY_BODY), [corpus])
    assert verdict["disposition"] == "new"
    assert verdict["novelty"] > 0.5
    assert verdict["corpus_size"] == 2


def test_a_spread_restatement_with_no_owner_is_rejected(tmp_path):
    corpus = _corpus(tmp_path, {
        "kanban-transitions": ("Claim and finish kanban cards.", KANBAN_BODY),
        "postgres-migration": ("Write and verify postgres migrations.", POSTGRES_BODY),
        "delivery-windows": ("Declare and close delivery windows.", DELIVERY_BODY)})
    # Every token already exists, spread across three skills; no one skill covers 60%.
    mixed = SPREAD_BODY
    verdict = adm.verdict_for_content(
        "everything", _skill("everything", "Cards, migrations and windows.", mixed), [corpus])
    assert verdict["disposition"] == "reject"
    assert "spread restatement" in verdict["reason"]
    assert verdict["novelty"] < adm.ADMISSION_MIN_NOVELTY


def test_a_stub_is_rejected(tmp_path):
    corpus = _corpus(tmp_path, {"kanban-transitions": ("Claim and finish kanban cards.", KANBAN_BODY)})
    verdict = adm.verdict_for_content("tiny", _skill("tiny", "Tiny.", "# Tiny\n\nDone."), [corpus])
    assert verdict["disposition"] == "reject"
    assert "stub" in verdict["reason"]


def test_an_empty_corpus_admits_and_says_why(tmp_path):
    verdict = adm.verdict_for_content(
        "first", _skill("first", "The first skill in a fresh install.", POSTGRES_BODY), [tmp_path])
    assert verdict["disposition"] == "new"
    assert verdict["corpus_size"] == 0
    assert "nothing to duplicate" in verdict["reason"]


def test_materiality_states_the_context_cost(tmp_path):
    corpus = _corpus(tmp_path, {"kanban-transitions": ("Claim and finish kanban cards.", KANBAN_BODY)})
    verdict = adm.verdict_for_content(
        "card-movement", _skill("card-movement", "Claim and finish kanban cards.", KANBAN_BODY), [corpus])
    assert "tokens per load" in verdict["materiality"]
    assert verdict["context_tokens"] == max(1, round(verdict["bytes"] / 4.0))


# --- The corpus never reads as itself -------------------------------------------------

def test_corpus_reads_nested_categories_and_skips_non_skills(tmp_path):
    (tmp_path / "platform" / "kanban-transitions").mkdir(parents=True)
    (tmp_path / "platform" / "kanban-transitions" / "SKILL.md").write_text(
        _skill("kanban-transitions", "Claim and finish kanban cards.", KANBAN_BODY))
    for skipped in ("absorbed", "docs", "_shared", "scripts", "depth"):
        (tmp_path / skipped / "old-skill").mkdir(parents=True)
        (tmp_path / skipped / "old-skill" / "SKILL.md").write_text(
            _skill("old-skill", "Retired.", KANBAN_BODY))
    (tmp_path / "platform" / "kanban-transitions" / "references").mkdir()
    (tmp_path / "platform" / "kanban-transitions" / "references" / "SKILL.md").write_text("# data\n")
    names = [d["name"] for d in adm.corpus_docs([tmp_path])]
    assert names == ["kanban-transitions"]


def test_a_skill_never_matches_itself(tmp_path):
    corpus = _corpus(tmp_path, {"kanban-transitions": ("Claim and finish kanban cards.", KANBAN_BODY)})
    verdict = adm.verdict_for_content(
        "kanban-transitions", _skill("kanban-transitions", "Claim and finish kanban cards.",
                                     KANBAN_BODY), [corpus],
        exclude=[corpus / "kanban-transitions"])
    assert all(m["name"] != "kanban-transitions" for m in verdict["matches"])


def test_duplicate_roots_are_counted_once(tmp_path):
    corpus = _corpus(tmp_path, {"kanban-transitions": ("Claim and finish kanban cards.", KANBAN_BODY)})
    assert len(adm.corpus_docs([corpus, corpus])) == 1


# --- New support files ----------------------------------------------------------------

def test_a_support_file_that_restates_another_skill_folds(tmp_path):
    corpus = _corpus(tmp_path, {"postgres-migration": ("Write and verify postgres migrations.",
                                                       POSTGRES_BODY)})
    own = corpus / "kanban-transitions"
    own.mkdir()
    (own / "SKILL.md").write_text(_skill("kanban-transitions", "Claim and finish kanban cards.",
                                         KANBAN_BODY))
    verdict = adm.verdict_for_support_file("kanban-transitions", POSTGRES_BODY, [corpus],
                                           exclude=[own])
    assert verdict["disposition"] == "fold"
    assert verdict["fold_target"] == "postgres-migration"


def test_a_depth_file_that_is_genuinely_new_is_not_refused(tmp_path):
    corpus = _corpus(tmp_path, {"postgres-migration": ("Write and verify postgres migrations.",
                                                       POSTGRES_BODY)})
    own = corpus / "kanban-transitions"
    own.mkdir()
    (own / "SKILL.md").write_text(_skill("kanban-transitions", "Claim and finish kanban cards.",
                                         KANBAN_BODY))
    verdict = adm.verdict_for_support_file("kanban-transitions", DELIVERY_BODY, [corpus],
                                           exclude=[own])
    assert verdict["disposition"] != "fold"


# --- The refusal is actionable ---------------------------------------------------------

def test_the_refusal_names_the_skill_the_path_the_reference_file_and_the_rule(tmp_path):
    corpus = _corpus(tmp_path, {"kanban-transitions": ("Claim and finish kanban cards.", KANBAN_BODY)})
    verdict = adm.verdict_for_content(
        "card-movement", _skill("card-movement", "Claim and finish kanban cards.", KANBAN_BODY), [corpus])
    message = adm.refusal_message(verdict, references_hint=str(
        corpus / "kanban-transitions" / "references" / "card-movement.md"))
    assert "Refused" in message
    assert "kanban-transitions" in message
    assert str(corpus / "kanban-transitions" / "SKILL.md") in message
    assert "references/card-movement.md" in message
    assert "60%" in message and "80%" in message
    assert "skill_manage(action='patch'" in message


def test_the_reject_refusal_names_the_nearest_skill_and_the_rule(tmp_path):
    corpus = _corpus(tmp_path, {
        "kanban-transitions": ("Claim and finish kanban cards.", KANBAN_BODY),
        "postgres-migration": ("Write and verify postgres migrations.", POSTGRES_BODY),
        "delivery-windows": ("Declare and close delivery windows.", DELIVERY_BODY)})
    mixed = SPREAD_BODY
    verdict = adm.verdict_for_content(
        "everything", _skill("everything", "Cards, migrations and windows.", mixed), [corpus])
    message = adm.refusal_message(verdict)
    assert "adds nothing to the corpus" in message
    assert "Nearest skill" in message
    assert "15%" in message


def test_every_refusal_that_names_a_skill_resolves_it_through_one_place(tmp_path):
    """Prose and structured refusal come from ONE resolution, on every naming disposition.

    `fold` names the skill the knowledge folds into; `reject` names the nearest skill, which is
    where the rule belongs instead. Both messages name a skill, so `named_skill` — what the
    message AND the tool's admission payload are built from — has to return it in both cases: a
    consumer that reads the field rather than parsing a sentence is told nothing otherwise.
    """
    corpus = _corpus(tmp_path, {
        "kanban-transitions": ("Claim and finish kanban cards.", KANBAN_BODY),
        "postgres-migration": ("Write and verify postgres migrations.", POSTGRES_BODY),
        "delivery-windows": ("Declare and close delivery windows.", DELIVERY_BODY)})

    folded = adm.verdict_for_content(
        "card-movement", _skill("card-movement", "Claim and finish kanban cards.", KANBAN_BODY),
        [corpus])
    assert folded["disposition"] == "fold"
    assert adm.named_skill(folded) == (
        "kanban-transitions", str(corpus / "kanban-transitions" / "SKILL.md"))
    assert "Existing skill: kanban-transitions " in adm.refusal_message(folded)

    rejected = adm.verdict_for_content(
        "everything", _skill("everything", "Cards, migrations and windows.", SPREAD_BODY), [corpus])
    assert rejected["disposition"] == "reject"
    name, path = adm.named_skill(rejected)
    assert (name, path) == (rejected["matches"][0]["name"], rejected["matches"][0]["path"])
    assert f"Nearest skill: {name} " in adm.refusal_message(rejected)


def test_a_refusal_that_names_no_skill_resolves_to_nothing():
    """Nothing to point at: an empty corpus, or a disposition that refuses on its own merits."""
    assert adm.named_skill({"disposition": "new", "matches": []}) == ("", "")
    assert adm.named_skill({"disposition": "reject", "matches": []}) == ("", "")
    assert adm.named_skill({}) == ("", "")


def test_the_verdict_is_json_serialisable_for_the_tool_result(tmp_path):
    corpus = _corpus(tmp_path, {"kanban-transitions": ("Claim and finish kanban cards.", KANBAN_BODY)})
    verdict = adm.verdict_for_content(
        "card-movement", _skill("card-movement", "Claim and finish kanban cards.", KANBAN_BODY), [corpus])
    assert json.loads(json.dumps(verdict))["disposition"] == "fold"
