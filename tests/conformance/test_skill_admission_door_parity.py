"""Conformance: the pen-side admission judgement and the publish door's are ONE rule.

`tools/skill_admission.py` exists because the pen (a create that never goes through the publish
door) could otherwise mint exactly what the door refuses. The only defence against that module
quietly drifting into a second, different rule is this test: it loads the door's own
implementation from its checkout, feeds the SAME corpus and the SAME drafts to both, and
requires identical dispositions, fold targets, novelty and nearest match.

It SKIPS where the door's checkout is not on the box (an upstream CI container has no
yaan-platform) and prints every path it searched, so a silent skip is impossible to mistake for
a pass. The numeric constants are additionally pinned in tests/tools/test_skill_admission.py,
which runs everywhere — so a threshold change on the door's side without this port still fails,
just with a less specific message.

Where the divergence lives, if one ever appears: this test names the fixture, both
dispositions, and both novelty scores.
"""

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tools import skill_admission as pen

DOOR_CANDIDATES = (
    os.environ.get("SKILLS_PUBLISH_NODES", ""),
    "/opt/hermes_prod/yaan-platform/scripts/skills_publish_nodes.py",
    "/opt/hermes_sandbox/yaan-platform/scripts/skills_publish_nodes.py",
    str(Path.home() / "yaan-platform" / "scripts" / "skills_publish_nodes.py"),
)


def _load_door():
    searched = [p for p in DOOR_CANDIDATES if p]
    for candidate in searched:
        path = Path(candidate)
        if path.is_file():
            spec = importlib.util.spec_from_file_location("_door_skills_publish_nodes", path)
            module = importlib.util.module_from_spec(spec)
            # The door resolves its OPS HEAD at import time by reading OWNERS in the real
            # corpus. Tests are forbidden from touching the real home (tests/home_io_guard.py),
            # and admission needs none of that: pin the documented rehearsal override for the
            # duration of the import so the module loads without estate I/O.
            previous = os.environ.get("SKILLS_PUBLISH_OPS_HEAD")
            os.environ["SKILLS_PUBLISH_OPS_HEAD"] = "admission-parity-fixture"
            try:
                spec.loader.exec_module(module)
            except Exception as e:  # pragma: no cover - environment, not logic
                pytest.skip("the door's module could not be imported from %s: %r" % (path, e))
            finally:
                if previous is None:
                    os.environ.pop("SKILLS_PUBLISH_OPS_HEAD", None)
                else:
                    os.environ["SKILLS_PUBLISH_OPS_HEAD"] = previous
            return module
    pytest.skip("the publish door's checkout is not on this host; searched: %s"
                % ", ".join(searched))


def _skill(name, description, body):
    return "---\nname: %s\ndescription: %s\n---\n\n%s\n" % (name, description, body)


KANBAN = _skill(
    "kanban-transitions", "Claim and finish kanban cards through terminal states.",
    "# Kanban card transitions\n\n"
    "A card moves ready -> running -> done; the claim fence refuses a second worker and the "
    "review column owns the verdict. Terminal states are done, blocked and archived.\n\n"
    "## Claiming a card\n\nClaim before writing.\n\n## Dispositions\n\nComplete or block.\n")
POSTGRES = _skill(
    "postgres-migration", "Write and verify postgres migrations safely.",
    "# Postgres migration\n\n"
    "Add a migration file, run it forward, verify the schema, and never edit a shipped "
    "migration. The advisory lock prevents two runners applying the same version.\n\n"
    "## Writing the migration\n\nNumber it and keep it idempotent.\n\n"
    "## Verifying\n\nRead the schema back.\n")
DELIVERY = _skill(
    "delivery-windows", "Declare and close delivery windows without a stranded pause.",
    "# Delivery windows\n\n"
    "Declare the window, arm the pause, broadcast each leg, then close the window and release "
    "the pause. Scheduled jobs defer into a reserved band and resume after the window closes.\n\n"
    "## Arming\n\nThe pause is a flag every scheduler reads.\n\n"
    "## Closing\n\nClose is idempotent.\n")
SKILLS_PUBLISH = _skill(
    "skills-publish", "Publish a verified skill commit through the door.",
    "# Skills publish\n\n"
    "Resolve the corpus, census the route, gate admission, commit into the corpus, verify the "
    "commit and open the pull request. The door refuses a restatement of an existing skill.\n\n"
    "## Census\n\nThe route names where each skill lands.\n\n## Verify\n\nA stamp names the "
    "commit that landed.\n")

CORPUS = {"kanban-transitions": KANBAN, "postgres-migration": POSTGRES,
          "delivery-windows": DELIVERY, "skills-publish": SKILLS_PUBLISH}

DRAFTS = {
    # A restatement: different name, same promise, same body.
    "restatement": _skill("card-movement", "Claim and finish kanban cards through terminal states.",
                          KANBAN.split("---\n", 2)[2]),
    # A duplicate description with a different body.
    "duplicate-description": _skill(
        "board-ops", "Claim and finish kanban cards through terminal states.",
        "# Board ops\n\n" + "\n".join("unique%d alpha beta gamma delta" % i for i in range(20))),
    # Genuinely new: nothing in the corpus states it.
    "new": _skill(
        "llama-tuning", "Tune llama batch size against decode latency.",
        "# Llama tuning\n\nMeasure decode latency per batch, then scale the batch until the "
        "quantised kernel saturates.\n\n## Measuring\n\nRecord tokens per second.\n"),
    # A spread restatement: every token exists, no single skill owns 60%.
    "spread": _skill(
        "everything", "Cards, migrations and windows.",
        "\n\nA card moves ready running done and the fence refuses a second worker. Claim the "
        "card.\n\nWrite a postgres migration and verify the schema. The advisory lock prevents "
        "two runners.\n\nDeclare the window and arm the pause. Broadcast each leg and close the "
        "window.\n\nKanban card transitions: claiming and dispositions.\n\nPostgres migration: "
        "writing and verifying.\n\nDelivery windows: arming and closing.\n\nSkills publish: "
        "census and verify the commit.\n"),
    # A stub.
    "stub": _skill("tiny", "Tiny.", "# Tiny\n\nDone."),
    # No description at all.
    "no-description": _skill("nameless", "", "# Nameless\n\n" + "Body prose. " * 40),
}


@pytest.fixture()
def door_corpus(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    for name, content in CORPUS.items():
        d = root / name
        d.mkdir()
        (d / "SKILL.md").write_text(content, encoding="utf-8")
    return root


@pytest.fixture()
def draft_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    for name, content in DRAFTS.items():
        d = repo / "drafts" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "fixtures"], check=True)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()
    return repo, head


def test_the_constants_are_the_doors(door_corpus):
    door = _load_door()
    assert pen.ADMISSION_FOLD_COVERAGE == door.ADMISSION_FOLD_COVERAGE
    assert pen.ADMISSION_DESCRIPTION_DUPLICATE == door.ADMISSION_DESCRIPTION_DUPLICATE
    assert pen.ADMISSION_MIN_NOVELTY == door.ADMISSION_MIN_NOVELTY
    assert pen.ADMISSION_STUB_BYTES == door.ADMISSION_STUB_BYTES
    assert pen.ADMISSION_MATCHES == door.ADMISSION_MATCHES
    assert pen.ADMISSION_SKIP_DIRS == door.ADMISSION_SKIP_DIRS
    assert pen.ADMISSION_STOPWORDS == door.ADMISSION_STOPWORDS


def test_the_corpus_readers_agree(door_corpus):
    door = _load_door()
    mine = pen.corpus_docs([door_corpus])
    theirs = door._skill_docs(door_corpus)
    assert [(d["name"], d["path"], d["tier"]) for d in mine] == \
           [(d["name"], d["path"], d["tier"]) for d in theirs]
    assert [d["intent"] for d in mine] == [d["intent"] for d in theirs]
    assert [d["description"] for d in mine] == [d["description"] for d in theirs]


def test_every_draft_gets_the_same_verdict_from_both_doors(door_corpus, draft_repo):
    door = _load_door()
    repo, head = draft_repo
    door_docs = door._skill_docs(door_corpus)
    door_idf, door_unseen = door._idf(door_docs)

    disagreements, seen = [], set()
    for fixture, content in DRAFTS.items():
        rel = "drafts/%s/SKILL.md" % fixture
        name = fixture
        theirs = door._admission_entry(repo, head, rel, door_docs, door_idf, door_unseen)
        assert theirs is not None, "the door could not read %s" % rel

        # (a) the same corpus, both engines
        mine_same_corpus = pen.judge(pen.draft_from_skill_md(name, content), door_docs,
                                     door_idf, door_unseen)
        # (b) the pen's own corpus reader and entry point
        mine = pen.verdict_for_content(name, content, [door_corpus])

        seen.add(theirs["disposition"])
        for label, verdict in (("same-corpus", mine_same_corpus), ("own-corpus", mine)):
            if (verdict["disposition"], verdict["fold_target"], round(verdict["novelty"], 4)) != \
               (theirs["disposition"], theirs["fold_target"], round(theirs["novelty"], 4)):
                disagreements.append(
                    "%s [%s]: door=%s/%s/%.4f pen=%s/%s/%.4f" % (
                        fixture, label, theirs["disposition"], theirs["fold_target"] or "-",
                        theirs["novelty"], verdict["disposition"], verdict["fold_target"] or "-",
                        verdict["novelty"]))
    assert not disagreements, "\n".join(disagreements)
    # The fixtures must actually exercise every branch, or the agreement means nothing.
    assert seen == {"fold", "reject", "new"}, "fixtures did not cover every disposition: %s" % seen


def test_the_refusals_match_field_for_field(door_corpus, draft_repo):
    door = _load_door()
    repo, head = draft_repo
    door_docs = door._skill_docs(door_corpus)
    door_idf, door_unseen = door._idf(door_docs)
    for fixture in ("restatement", "duplicate-description", "spread", "stub", "no-description"):
        content = DRAFTS[fixture]
        theirs = door._admission_entry(repo, head, "drafts/%s/SKILL.md" % fixture, door_docs,
                                       door_idf, door_unseen)
        mine = pen.verdict_for_content(fixture, content, [door_corpus])
        assert mine["reason"] == theirs["reason"], fixture
        assert mine["materiality"] == theirs["materiality"], fixture
        assert mine["context_tokens"] == theirs["context_tokens"], fixture
        assert mine["bytes"] == theirs["bytes"], fixture
        assert [m["name"] for m in mine["matches"]] == [m["name"] for m in theirs["matches"]], fixture
