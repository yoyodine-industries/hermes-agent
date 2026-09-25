"""Tests for the close-path deliverable gate (hermes_cli.kanban_completion_gate).

The shape under test is the one that closed ``t_b579f394``: a completion that
declares a commit authored inside the card's scratch tree and artifacts under a
*profile* scratch directory. That card's DoD was satisfiable by workspace-local
evidence, so the close succeeded, and within hours the commit resolved in no
repository on the host and the artifacts were pruned. The gate must refuse it,
name the claims, leave the card exactly where it was, and still admit every
legitimate completion — including the same card once the artifacts are staged or
the commit resolves in a real checkout.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_completion_gate as gate
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw

# The commit and the digest t_b579f394's completion actually declared.
INCIDENT_SHA = "018511a"
INCIDENT_SHA256 = "386dc0210b1df0f174f0122219202a3ad61678ce63cecd0e58293dc72410f76f"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB and a test-owned search set."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # The gate searches the fleet's checkouts by default; a test owns its search set.
    monkeypatch.setattr(gate, "_FLEET_REPO_ROOTS", ())
    kb.init_db()
    return home


def _init_git_repo(repo: Path) -> str:
    """A real checkout with one commit; returns its HEAD sha."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "kanban@example.com"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Kanban Test"], check=True, capture_output=True, text=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True, text=True)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _card_with_scratch_workspace(conn, title: str = "card") -> tuple[str, Path]:
    tid = kb.create_task(conn, title=title)
    ws = kbw.resolve_workspace(kb.get_task(conn, tid))
    kbw.set_workspace_path(conn, tid, ws)
    ws.mkdir(parents=True, exist_ok=True)
    return tid, ws


def _incident_completion(home: Path) -> tuple[dict, Path, Path]:
    """t_b579f394's declaration: a scratch-tree commit and profile-scratch artifacts."""
    scratch = home / "profiles" / "platform-stl" / "cache" / "scratch" / "t_b579f394"
    scratch.mkdir(parents=True, exist_ok=True)
    diff = scratch / "dag-diff.txt"
    diff.write_text("--- a\n+++ b\n", encoding="utf-8")
    missing = scratch / "board-shape.json"
    metadata = {
        "branch": "card/t_b579f394-train-handoff-deterministic",
        "commit": INCIDENT_SHA,
        "artifacts": [str(diff), str(missing)],
        "determinism_proof": f"two replays of one board byte-identical, sha256 {INCIDENT_SHA256}",
    }
    return metadata, diff, missing


# ---------------------------------------------------------------------------
# Precision: what counts as a claim
# ---------------------------------------------------------------------------


def test_sha256_digest_is_never_a_revision_claim():
    """The incident completion carried a 64-hex determinism proof; a gate that read
    it as a commit would refuse a legitimate close."""
    metadata = {"determinism_proof": f"byte-identical replays, sha256 {INCIDENT_SHA256}"}
    prose = f"determinism proof #1 : {INCIDENT_SHA256}"
    assert gate.revision_claims(metadata, prose) == []


def test_revision_metadata_key_is_a_claim():
    assert gate.revision_claims({"commit": INCIDENT_SHA}) == [INCIDENT_SHA]


def test_nested_revision_keys_are_walked():
    metadata = {"handoff": {"branch": "card/x", "target_sha": INCIDENT_SHA}}
    assert gate.revision_claims(metadata) == [INCIDENT_SHA]


def test_a_hash_key_is_not_a_revision_claim():
    assert gate.revision_claims({"hash": INCIDENT_SHA}) == []


def test_prose_revision_marker_is_a_claim():
    prose = f"Committed on card/t_b579f394-train-handoff @ {INCIDENT_SHA}"
    assert gate.revision_claims({}, prose) == [INCIDENT_SHA]


def test_bare_hex_in_prose_without_a_marker_is_not_a_claim():
    assert gate.revision_claims({}, f"the {INCIDENT_SHA} change touched 3 files") == []


def test_checksum_context_is_not_a_claim():
    thirty_nine = "386dc0210b1df0f174f0122219202a3ad61678"
    assert gate.revision_claims({}, f"sha1 of the tarball: {thirty_nine}") == []


# ---------------------------------------------------------------------------
# Precision: artifact verdicts
# ---------------------------------------------------------------------------


def test_url_and_durable_path_are_admitted():
    """A URL, or a file in durable storage, is retrievable — no refusal.

    The durable path is this test file itself: real, absolute, outside every
    ephemeral root. (``tmp_path`` is deliberately NOT used here — pytest's tmp
    lives under the system temp dir, which the gate refuses on purpose.)
    """
    assert gate.artifact_problem("https://example.invalid/run.json",
                                 workspace_kind="scratch", workspace=None) is None
    durable = Path(__file__).resolve()
    assert durable.exists()
    assert gate.artifact_problem(str(durable), workspace_kind="scratch", workspace=None) is None


def test_system_temp_artifact_is_refused(tmp_path):
    artifact = tmp_path / "run.json"
    artifact.write_text("{}", encoding="utf-8")
    problem = gate.artifact_problem(str(artifact), workspace_kind="scratch", workspace=None)
    assert problem is not None and "temp" in problem


def test_profile_scratch_artifact_is_refused(kanban_home):
    scratch = kanban_home / "profiles" / "platform-stl" / "cache" / "scratch" / "t_b579f394"
    scratch.mkdir(parents=True, exist_ok=True)
    artifact = scratch / "dag-diff.txt"
    artifact.write_text("diff", encoding="utf-8")
    problem = gate.artifact_problem(str(artifact), workspace_kind="scratch", workspace=None)
    assert problem is not None and "scratch" in problem


def test_relative_and_missing_artifacts_are_refused(tmp_path):
    relative = gate.artifact_problem("evidence/dag-diff.txt", workspace_kind="scratch", workspace=None)
    missing = gate.artifact_problem(str(tmp_path / "gone.txt"), workspace_kind="scratch", workspace=None)
    assert relative is not None and "relative" in relative
    assert missing is not None and "not on disk" in missing


def test_worktree_workspace_artifact_is_refused(tmp_path):
    ws = tmp_path / ".worktrees" / "t_x"
    ws.mkdir(parents=True, exist_ok=True)
    artifact = ws / "report.json"
    artifact.write_text("{}", encoding="utf-8")
    problem = gate.artifact_problem(str(artifact), workspace_kind="worktree", workspace=ws)
    assert problem is not None and "worktree" in problem


def test_scratch_workspace_artifact_is_admitted(kanban_home):
    """The kernel stages scratch-workspace artifacts into durable attachments."""
    ws = kanban_home / "kanban" / "workspaces" / "t_x"
    ws.mkdir(parents=True, exist_ok=True)
    artifact = ws / "chart.png"
    artifact.write_bytes(b"png")
    assert gate.artifact_problem(str(artifact), workspace_kind="scratch", workspace=ws) is None


# ---------------------------------------------------------------------------
# The close path
# ---------------------------------------------------------------------------


def test_complete_task_refuses_the_scratch_tree_commit_and_artifacts(kanban_home):
    """t_b579f394 replay: nothing the completion declares outlives the close."""
    with kbc.connect() as conn:
        tid, ws = _card_with_scratch_workspace(conn, title="train handoff")
        metadata, diff, missing = _incident_completion(kanban_home)
        summary = (f"done: the train handoff is deterministic — committed on "
                   f"card/t_b579f394-train-handoff-deterministic @ {INCIDENT_SHA}")
        with pytest.raises(gate.UnretrievableDeliverableError) as caught:
            kb.complete_task(conn, tid, result=summary, metadata=metadata)
        assert kb.get_task(conn, tid).status != "done"
        kinds = [event.kind for event in kb.list_events(conn, tid)]
        assert "completion_blocked_unretrievable_deliverable" in kinds

    findings = {finding["claim"]: finding for finding in caught.value.findings}
    assert set(findings) == {INCIDENT_SHA, str(diff), str(missing)}
    assert findings[INCIDENT_SHA]["claim_kind"] == "revision"
    assert str(diff) in findings
    assert findings[str(diff)]["claim_kind"] == "artifact"
    # A refusal changes nothing: the declared file is still where it was.
    assert diff.exists()
    assert str(diff) in str(caught.value)


def test_complete_task_admits_a_commit_that_resolves_in_a_real_checkout(kanban_home):
    """The same card closes once its commit exists in a checkout — the gate is not
    a blanket refusal."""
    with kbc.connect() as conn:
        tid, ws = _card_with_scratch_workspace(conn, title="pushed work")
        sha = _init_git_repo(ws / "checkout")
        artifact = ws / "run.json"
        artifact.write_text("{}", encoding="utf-8")
        assert kb.complete_task(
            conn, tid,
            result=f"done: landed the fix, merged as {sha[:12]}",
            metadata={"commit": sha, "artifacts": [str(artifact)]},
        )
        task = kb.get_task(conn, tid)
        attachments = [a.filename for a in kb.list_attachments(conn, tid)]
    assert task.status == "done"
    assert attachments == ["run.json"]


def test_complete_task_admits_an_unresolvable_short_sha_once_its_checkout_is_declared(kanban_home, monkeypatch):
    """A revision in a checkout this host has not been told about is fixed by naming
    it in HERMES_KANBAN_REPO_ROOTS — the refusal names both the claim and that
    remedy."""
    with kbc.connect() as conn:
        tid, ws = _card_with_scratch_workspace(conn, title="external checkout")
        outside = Path(ws).parent / "outside-checkout"
        sha = _init_git_repo(outside)
        with pytest.raises(gate.UnretrievableDeliverableError) as caught:
            kb.complete_task(conn, tid, result="done: landed elsewhere", metadata={"commit": sha})
        assert sha in str(caught.value)
        assert "HERMES_KANBAN_REPO_ROOTS" in str(caught.value)
        monkeypatch.setenv("HERMES_KANBAN_REPO_ROOTS", str(outside))
        assert kb.complete_task(conn, tid, result="done: landed elsewhere", metadata={"commit": sha})
        assert kb.get_task(conn, tid).status == "done"


def test_complete_task_admits_the_normal_scratch_artifact_handoff(kanban_home):
    """Existing behaviour is untouched: a scratch workspace artifact is staged into
    durable attachments and the close goes through."""
    with kbc.connect() as conn:
        tid, ws = _card_with_scratch_workspace(conn, title="render chart")
        artifact = ws / "chart.png"
        artifact.write_bytes(b"png-bytes")
        assert kb.complete_task(conn, tid, result="ok", metadata={"artifacts": [str(artifact)]})
        attachments = [(a.filename, Path(a.stored_path).exists()) for a in kb.list_attachments(conn, tid)]
    assert attachments == [("chart.png", True)]
    assert not ws.exists(), "scratch workspace is still cleaned up"


def test_force_records_the_operator_override_and_completes(kanban_home):
    """`hermes kanban complete --force` is the escape hatch — and it is audited."""
    with kbc.connect() as conn:
        tid, _ws = _card_with_scratch_workspace(conn, title="forced close")
        metadata, _diff, _missing = _incident_completion(kanban_home)
        assert kb.complete_task(conn, tid, result="done: waived", metadata=metadata, force=True)
        task = kb.get_task(conn, tid)
        forced = [e for e in kb.list_events(conn, tid) if e.kind == "deliverable_gate_forced"]
    assert task.status == "done"
    assert len(forced) == 1
    assert {finding["claim"] for finding in forced[0].payload["findings"]} == {
        INCIDENT_SHA, str(_diff), str(_missing),
    }


def test_an_abbreviated_sha_that_exists_is_admitted(kanban_home):
    """A handoff may cite a 7-char abbreviation, and git answers with the full oid --
    so a resolver that matches answers by equality reports every short sha as
    missing and refuses a legitimate close."""
    with kbc.connect() as conn:
        tid, ws = _card_with_scratch_workspace(conn, title="short sha")
        full = _init_git_repo(ws / "checkout")
        assert kb.complete_task(conn, tid, result="done: tidy", metadata={"commit": full[:7]})
        task = kb.get_task(conn, tid)
    assert task.status == "done"


def test_revision_lookup_never_lazy_fetches(kanban_home, tmp_path, monkeypatch):
    """The lookup stays local: on a promisor (partial) clone an on-demand fetch would
    stall a close for as long as the network takes to answer."""
    with kbc.connect() as conn:
        _tid, ws = _card_with_scratch_workspace(conn, title="local only")
        repo = ws / "checkout"
        full = _init_git_repo(repo)
    real_git = shutil.which("git")
    assert real_git, "this test needs a real git on PATH"
    log = tmp_path / "git-env.log"
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text(f'#!/bin/sh\nenv >> "{log}"\nexec "{real_git}" "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
    assert gate.resolves_in(repo, full) is True
    assert "GIT_NO_LAZY_FETCH=1" in log.read_text(encoding="utf-8")
