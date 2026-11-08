"""The completion-contract release: immutable declarations, typed refusals, a terminal park.

A card's ``completion_contract`` is a DECLARATION (``local-only`` / ``OWNER/REPO`` / an exact
PR URL). Acceptance used to rebind it to the first published PR, which froze the card to that
head — a superseded PR left it unsatisfiable forever with no verb to release it — and a
completion that could never pass was refused with a bare ``False``, indistinguishable from a
mistyped id, so the worker respawned against the same verdict. These tests pin the
replacement contract:

* publication rides the acceptance receipt; the stored declaration never moves,
* ``complete_task`` refuses with a falsy ``CompletionRefusal`` that names its cause,
* a contract no retry can satisfy parks the card ``blocked`` / ``capability``,
* ``kanban set-contract`` is the release, top-level only, refused once a card is terminal.

No network and no ``gh``: the GitHub evidence paths are monkeypatched at the live
``kanban_pr_acceptance._api``, so the real classifier and the real SQLite board are
exercised.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect


ROOT = Path(__file__).parents[2]
SHA = "a" * 40
PR_URL = "https://github.com/acme/repo/pull/7"


@pytest.fixture
def board(tmp_path, monkeypatch):
    """An isolated board: no live ``HERMES_KANBAN_DB`` pin, no delegated-child fence."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "home"))
    for name in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(name, raising=False)
    kb.init_db()


@pytest.fixture
def github(monkeypatch):
    """The GitHub contract ``collect_acceptance`` reads: one required check named ``ci``.

    The transport is redirected on the module generation the LIVE call will use, resolved
    through ``sys.modules`` at test time. A sibling test in this directory purges
    ``hermes_cli*`` from ``sys.modules`` (``test_kanban_cli_dispatch_passthrough``), so the
    module object captured at import time can be a dead generation whose ``_api`` nothing
    calls any more — patching it would leave the real ``gh`` transport in place, and the
    card would fail closed as "acceptance evidence unavailable" instead of exercising the
    classifier. ``kanban_db`` imports the store inside the call, so both of these resolve to
    the generation that call will use; the liveness assert at teardown fails the test rather
    than letting a fake that was never reached pass for a result.
    """
    import importlib

    state = {
        "required": [{"context": "ci", "app": {"databaseId": 1}}],
        "runs": [{"id": 42, "name": "ci", "head_sha": SHA, "app": {"id": 1},
                  "status": "completed", "conclusion": "success",
                  "html_url": "https://github.com/acme/repo/actions/runs/42"}],
        "calls": 0,
        # The PR's own state, which is the acceptance evidence once the repository
        # declares no required checks (ruling t_9082c751).
        "pr_state": "OPEN",
        "merge_sha": "b" * 40,
    }

    def fake_api(endpoint, *, query=None, paginate=False, profile_home=None):
        state["calls"] += 1
        if endpoint == "graphql":
            return {"data": {"repository": {"pullRequest": {
                "headRefOid": SHA, "baseRefName": "main", "state": state["pr_state"],
                "mergeCommit": {"oid": state["merge_sha"]},
                "baseRef": {"branchProtectionRule": {"requiredStatusChecks": state["required"]}},
            }}}}
        if "/rules/branches/" in endpoint:
            return [[]]
        if "/check-runs" in endpoint:
            return [{"total_count": len(state["runs"]), "check_runs": state["runs"]}]
        if "/statuses" in endpoint:
            return [[]]
        if "/pulls/" in endpoint:
            return {"head": {"sha": SHA}, "base": {"ref": "main"}, "state": "open"}
        # The create-time contract-verification read (card t_b58332a4): ``acme/repo``
        # declares one required check, so a card born on that contract keeps it.
        if endpoint == "acme/repo":
            return {"default_branch": "main"}
        if endpoint.endswith("/protection"):
            return {"required_status_checks": {"contexts": ["ci"], "checks": []}}
        raise AssertionError(f"unexpected endpoint {endpoint}")

    live_acceptance = importlib.import_module("hermes_cli.kanban_pr_acceptance")
    live_store = importlib.import_module("hermes_cli.kanban_pr_acceptance_store")
    monkeypatch.setattr(live_acceptance, "_api", fake_api)
    # A store generation older than the live acceptance module would still hold the real
    # transport in its own globals dict; patch that one too when it is a different object.
    bound_globals = getattr(getattr(live_store, "collect_acceptance", None), "__globals__", None)
    if bound_globals is not None and bound_globals is not live_acceptance.__dict__:
        monkeypatch.setitem(bound_globals, "_api", fake_api)
        assert bound_globals["_api"] is fake_api
    assert live_acceptance._api is fake_api
    return state


def _receipts(conn, tid: str) -> list[dict]:
    return [json.loads(row[0]) for row in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'pr_acceptance'", (tid,))]


def _refusal(outcome) -> kb.CompletionRefusal:
    """The refused outcome of ``complete_task``, asserting it is the typed refusal."""
    assert isinstance(outcome, kb.CompletionRefusal), outcome
    assert bool(outcome) is False
    return outcome


def test_completion_records_the_publication_without_rebinding_the_contract(board, github):
    """D2: the published PR is recorded on the receipt, never written back to the card."""
    with connect() as conn:
        tid = kb.create_task(conn, title="publish", completion_contract="acme/repo")
        # A close that RECORDS a published PR owes its landing leg on EVERY transition --
        # the landing-carrier clause's trigger was widened off the review transition by
        # card t_502a28bd, so this fixture has to name the carrier. The fence is not this
        # test's subject; the receipt is, and it is still the only thing asserted below.
        carrier = kb.create_task(conn, title=f"[land] {PR_URL} for {tid}",
                                 initial_status="running")
        assert kb.complete_task(conn, tid, result="done",
                                metadata={"published_pr": PR_URL,
                                          "landing_card": carrier}) is True
        task = kb.get_task(conn, tid)
        assert task.status == "done"
        assert task.completion_contract == "acme/repo"  # the declaration never moves
        receipt = _receipts(conn, tid)[-1]
        assert receipt["ok"] is True and receipt["pr_url"] == PR_URL
        assert receipt["published_pr"] == PR_URL
        assert github["calls"] > 0  # the classifier read this evidence, not a stale generation


def test_sibling_repository_published_pr_is_still_refused(board, github):
    """The declaration is the fence: a PR from another repo cannot sign the card off."""
    with connect() as conn:
        tid = kb.create_task(conn, title="publish", completion_contract="acme/repo")
        refusal = _refusal(kb.complete_task(conn, tid, result="done", metadata={
            "published_pr": "https://github.com/other/repo/pull/7"}))
        assert refusal.cause == "acceptance_refusal"
        assert kb.get_task(conn, tid).status != "done"


def test_refusal_causes_are_distinguishable(board, github):
    """D3: an unknown id, a non-completable card and a blocked parent do not read alike."""
    with connect() as conn:
        assert bool(kb.complete_task(conn, "t_missing", result="done")) is False
        assert _refusal(kb.complete_task(conn, "t_missing", result="done")).cause == "unknown_id"

        tid = kb.create_task(conn, title="local", completion_contract="local-only")
        assert kb.complete_task(conn, tid, result="done") is True
        assert _refusal(kb.complete_task(conn, tid, result="done again")).cause == "not_running"

        parent = kb.create_task(conn, title="parent", completion_contract="local-only")
        child = kb.create_task(conn, title="child", completion_contract="local-only",
                               parents=(parent,))
        gated = _refusal(kb.complete_task(conn, child, result="done"))
        assert gated.cause == "parent_gate_unsatisfied"
        assert parent in gated.detail  # names the blocker, not "unknown id"


def test_no_required_checks_is_satisfied_by_merge_state(board, github):
    """D4, re-ruled by t_9082c751: a repository with no required checks is CLOSABLE.

    The old answer refused such a contract as unsatisfiable and parked the card
    ``blocked``/``capability`` with its work done -- which, beside the landing fence
    refusing ``local-only`` next to the same publication, left the card no legal
    contract at all. Now an OPEN PR there is ``pending`` (retryable, never parked) and
    a MERGED one is the acceptance evidence.
    """
    github["required"] = []
    with connect() as conn:
        tid = kb.create_task(conn, title="ci task", completion_contract="acme/repo")
        refusal = _refusal(kb.complete_task(conn, tid, result="done",
                                            metadata={"published_pr": PR_URL}))

        task = kb.get_task(conn, tid)
        assert task.status != "blocked"          # NOT parked: the verdict is retryable
        assert task.block_kind is None
        assert refusal.cause == "acceptance_refusal"
        assert "MERGED" in refusal.detail and "merge the PR" in refusal.detail
        assert "local-only" not in refusal.detail
        receipt = _receipts(conn, tid)[-1]
        assert receipt["classification"] == "pending"
        assert receipt["required"] == []

        # ... and once the PR merges, the SAME declaration closes the card: the acceptance
        # door passes on merge state, and the landing fence (unchanged, and right) still
        # wants the published PR's landing leg named.
        carrier = kb.create_task(conn, title=f"[land] {PR_URL} for {tid}",
                                 initial_status="running")
        github["pr_state"] = "MERGED"
        assert kb.complete_task(conn, tid, result="done",
                                metadata={"published_pr": PR_URL,
                                          "landing_card": carrier}) is True
        assert kb.get_task(conn, tid).status == "done"
        done_receipt = _receipts(conn, tid)[-1]
        assert done_receipt["classification"] == "success"
        assert done_receipt["merge_sha"] == github["merge_sha"]
        assert kb.get_task(conn, tid).completion_contract == "acme/repo"


def test_a_persisted_unsatisfiable_receipt_offers_the_merge_route_not_local_only():
    """Direction 3 (t_9082c751): a card that HAS published is never told to use ``local-only``.

    ``collect_acceptance`` can no longer produce the shape, but receipts persisted before
    the ruling still sit on cards; releasing one must name a route that exists.
    """
    assert kb._contract_is_unsatisfiable({"classification": "missing", "required": []}) is True
    published = kb._unsatisfiable_contract_release(
        "t_abc123", "acme/repo", {"classification": "missing", "required": [], "pr_url": PR_URL})
    assert "MERGED" in published
    assert "local-only" not in published
    unpublished = kb._unsatisfiable_contract_release(
        "t_abc123", "acme/repo", {"classification": "missing", "required": []})
    assert "local-only" in unpublished      # no publication to contradict: the old door is legal
    assert "MERGED" in unpublished


def _blocked_reasons(conn, tid: str) -> list[str]:
    """The reason strings the ``blocked`` events carry — the card's own record of the park."""
    return [json.loads(row[0]).get("reason") or "" for row in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked'", (tid,))]


def test_the_park_door_stamps_its_named_release_on_the_card(board):
    """The lost sibling of the two above: the PARK door itself, driven directly.

    ``collect_acceptance`` cannot produce an unsatisfiable receipt any more (t_9082c751), but
    ``_park_unsatisfiable_contract`` stays live for receipts PERSISTED before that ruling, and it
    had no test of its own — the generation that covered it also covered patches the live line no
    longer carries. Driving the door rather than the classifier keeps this independent of the
    forge: a contract no retry can satisfy parks the card ``blocked`` / ``capability``, the
    ``blocked`` event's reason names the contract AND the release that EXISTS for that card (the
    merge route once something is published, ``set-contract`` while nothing is), and the stamped
    release really releases the card.
    """
    with connect() as conn:
        tids = {name: kb.create_task(conn, title=f"parked, {name}",
                                     completion_contract="local-only")
                for name in ("published", "unpublished")}
        for tid in tids.values():
            # The declaration the park is about, without a create-time forge read.
            assert kb.set_contract(conn, tid, "acme/repo", reason="fixture declaration") is True

        assert kb._park_unsatisfiable_contract(
            conn, tids["published"], "acme/repo",
            receipt={"classification": "missing", "required": [], "pr_url": PR_URL}) is True
        assert kb._park_unsatisfiable_contract(
            conn, tids["unpublished"], "acme/repo",
            receipt={"classification": "missing", "required": []}) is True

        for name, tid in tids.items():
            parked = kb.get_task(conn, tid)
            assert parked.status == "blocked", name         # parked, not silently closed
            assert parked.block_kind == "capability", name  # no human input owed
            reasons = _blocked_reasons(conn, tid)
            assert len(reasons) == 1, name
            assert "acme/repo" in reasons[0], name          # names the contract it is about

        # A card that published is never sent back to `local-only`: the reason offers the merge.
        published_reason = _blocked_reasons(conn, tids["published"])[0]
        assert "MERGED" in published_reason
        assert "local-only" not in published_reason
        # A card that published nothing may take the declaration release.
        unpublished_reason = _blocked_reasons(conn, tids["unpublished"])[0]
        assert "set-contract" in unpublished_reason

        # The stamped door is REAL, not prose: the release it names is the release that works.
        assert kb.unblock_task(conn, tids["published"]) is True
        assert kb.set_contract(conn, tids["published"], "local-only",
                               reason="repository declares no required checks") is True
        released = kb.get_task(conn, tids["published"])
        assert released.status != "blocked", released.status
        assert released.completion_contract == "local-only"


def test_retryable_missing_check_does_not_park(board, github):
    """A required check that has not reported yet stays retryable — no park, no terminal state."""
    github["runs"] = []
    with connect() as conn:
        tid = kb.create_task(conn, title="pending ci", completion_contract="acme/repo")
        refusal = _refusal(kb.complete_task(conn, tid, result="done",
                                            metadata={"published_pr": PR_URL}))
        assert refusal.cause == "acceptance_refusal"
        assert kb.get_task(conn, tid).status != "blocked"
        assert kb.get_task(conn, tid).block_kind is None
        assert github["calls"] > 0  # the pending check was read, not assumed


def test_set_contract_requires_a_reason_and_refuses_a_terminal_card(board):
    with connect() as conn:
        tid = kb.create_task(conn, title="release", completion_contract="acme/repo",
                             created_by="platform-coder")
        assert kb.set_contract(conn, tid, "local-only", reason="repo requires no checks",
                               actor="platform-coder")
        task = kb.get_task(conn, tid)
        assert task.completion_contract == "local-only"
        events = [json.loads(row[0]) for row in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'contract_changed'",
            (tid,))]
        assert events == [{"old": "acme/repo", "new": "local-only",
                           "reason": "repo requires no checks", "actor": "platform-coder"}]

        with pytest.raises(ValueError):
            kb.set_contract(conn, tid, "local-only", reason="   ")
        with pytest.raises(ValueError):
            kb.set_contract(conn, tid, "not-a-contract", reason="typo")
        assert kb.set_contract(conn, "t_missing", "local-only", reason="x") is False

        assert kb.complete_task(conn, tid, result="done") is True
        with pytest.raises(RuntimeError):
            kb.set_contract(conn, tid, "acme/repo", reason="too late")
        assert kb.get_task(conn, tid).completion_contract == "local-only"
        changed = conn.execute("SELECT count(*) FROM task_events WHERE task_id = ? "
                               "AND kind = 'contract_changed'", (tid,)).fetchone()[0]
        assert changed == 1  # a refusal writes no event


def _run_hermes(home: Path, *args: str, marker: bool = False) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["HERMES_KANBAN_HOME"] = str(home)
    for name in ("HERMES_KANBAN_BOARD", "HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT"):
        env.pop(name, None)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    if marker:
        env["HERMES_DELEGATED_CHILD_CONTEXT"] = "1"
    else:
        env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
    return subprocess.run([sys.executable, "-m", "hermes_cli.main", *args],
                          cwd=ROOT, env=env, capture_output=True, text=True, check=False, timeout=60)


def test_cli_set_contract_is_the_documented_release_and_top_level_only(tmp_path):
    """The operator's verb, end to end: create warns, set-contract releases, children cannot."""
    home = tmp_path / "hermes"
    home.mkdir()

    created = _run_hermes(home, "kanban", "create", "contract probe",
                          "--completion-contract", "acme/repo")
    assert created.returncode == 0, created.stderr
    assert "set-contract" in created.stdout  # the one-line authoring advisory
    tid = created.stdout.split("Created ", 1)[1].split()[0]

    released = _run_hermes(home, "kanban", "set-contract", tid, "local-only",
                           "--reason", "repo requires no checks", "--author", "platform-coder")
    assert released.returncode == 0, released.stderr
    assert "contract_changed" in released.stdout

    blank = _run_hermes(home, "kanban", "set-contract", tid, "acme/repo", "--reason", "")
    assert blank.returncode == 2, blank.stdout + blank.stderr

    refused = _run_hermes(home, "kanban", "set-contract", tid, "acme/repo",
                          "--reason", "worker wants out", marker=True)
    assert refused.returncode == 1
    assert "delegate_task" in refused.stderr
