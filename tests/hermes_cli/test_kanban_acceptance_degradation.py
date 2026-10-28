"""Acceptance never deadlocks: degrade, name the contract, repair, and say what ``force`` is.

Card ``t_c4a9d940``.  On 2026-10-19 a card with nothing left to prove was refused FIVE times
and parked ``blocked``/``capability`` while its work was pushed *and its PR merged*.  Three
shapes were on its event log, and all three are fileable bugs of the same class: a close door
that cannot accept evidence it can verify is as wrong as one that accepts evidence it cannot.

* ``infra`` -- the forge could not be read, so the card could never close again;
* ``missing`` -- the stored contract was a bare space, and every retry was told to supply
  ``metadata.published_pr`` "matching the persisted completion contract" without ever being
  told what that contract is;
* ``--force`` -- silently did not bypass acceptance at all, while the refusal implied a door.

The rulings pinned here (platform-stl, card ``t_c4a9d940``):

* R1 **degrade, never deadlock** -- an unreadable forge is ``recorded, unverified``: the
  completion is accepted with the receipt and a WARN row, never refused forever;
* R2 **name the contract verbatim** -- no refusal may say "the persisted contract" without
  quoting it;
* R3 **``--force`` says what it does** -- it overrides the live-claim guard only, and a
  forced refusal must say so;
* R4 **corrupt state is repaired at load** -- a stored contract that is not a readable
  declaration is repaired to ``local-only`` with an event, instead of failing every close.

The counter-proof lives here too: a PR the forge CAN read stays refused while it is open or
red, so degrading never becomes a way to close a ``[land]`` card over an unlanded PR.

No network and no ``gh``: the transport is monkeypatched on the module generation the live
call resolves through ``sys.modules`` (the same seam the completion-contract suite uses).
"""
from __future__ import annotations

import importlib
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect, write_txn

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
def forge(monkeypatch):
    """Both forge doors, with the failures as knobs.

    ``state["raise"]`` makes every ``collect_acceptance`` endpoint fail (an unreachable or
    unauthenticated forge); ``state["landing_state"]`` is what the landing-carrier clause's own
    forge read answers (``merged`` means the landing is not owed).
    """
    state = {
        "raise": None,
        "required": [{"context": "ci", "app": {"databaseId": 1}}],
        "conclusion": "success",
        "status": "completed",
        "pr_state": "OPEN",
        "landing_state": "merged",
        "calls": 0,
    }

    def fake_api(endpoint, *, query=None, paginate=False, profile_home=None):
        state["calls"] += 1
        if state["raise"] is not None:
            raise state["raise"]
        if endpoint == "graphql":
            return {"data": {"repository": {"pullRequest": {
                "headRefOid": SHA, "baseRefName": "main", "state": state["pr_state"],
                "mergeCommit": {"oid": "b" * 40},
                "baseRef": {"branchProtectionRule": {
                    "requiredStatusChecks": state["required"]}},
            }}}}
        if "/rules/branches/" in endpoint:
            return [[]]
        if "/check-runs" in endpoint:
            return [{"total_count": 1, "check_runs": [{
                "id": 42, "name": "ci", "head_sha": SHA, "app": {"id": 1},
                "status": state["status"], "conclusion": state["conclusion"],
                "html_url": "https://github.com/acme/repo/actions/runs/42"}]}]
        if "/statuses" in endpoint:
            return [[]]
        if "/pulls/" in endpoint:
            return {"head": {"sha": SHA}, "base": {"ref": "main"}, "state": "open"}
        raise AssertionError(f"unexpected endpoint {endpoint}")

    def fake_forge_state(repo, number, *, profile_home=None):
        return state["landing_state"]

    live_acceptance = importlib.import_module("hermes_cli.kanban_pr_acceptance")
    live_store = importlib.import_module("hermes_cli.kanban_pr_acceptance_store")
    live_carrier = importlib.import_module("hermes_cli.kanban_landing_carrier")
    monkeypatch.setattr(live_acceptance, "_api", fake_api)
    monkeypatch.setattr(live_carrier, "forge_state", fake_forge_state)
    bound_globals = getattr(getattr(live_store, "collect_acceptance", None), "__globals__", None)
    if bound_globals is not None and bound_globals is not live_acceptance.__dict__:
        monkeypatch.setitem(bound_globals, "_api", fake_api)
    assert live_acceptance._api is fake_api
    return state


def _contract(conn, tid):
    return conn.execute(
        "SELECT completion_contract FROM tasks WHERE id = ?", (tid,)).fetchone()[0]


def _store_contract(conn, tid, value):
    """Write a contract past ``create_task``'s validation -- a board that already holds one."""
    with write_txn(conn):
        conn.execute("UPDATE tasks SET completion_contract = ? WHERE id = ?", (value, tid))


def _events(conn, tid, kind):
    return [json.loads(row[0]) for row in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?", (tid, kind))]


def _refusal(outcome):
    assert isinstance(outcome, kb.CompletionRefusal), outcome
    return outcome


def _carrier(conn, tid):
    return kb.create_task(conn, title=f"[land] {PR_URL} for {tid}", initial_status="running")


# --------------------------------------------------------------------------- R4: repair


def test_a_contract_no_retry_can_satisfy_is_repaired_at_load(board):
    """R4: a stored contract that is not a declaration is repaired, not left fatal.

    Card ``t_c4a9d940`` held a column that read blank. Every close was refused with "supply
    metadata.published_pr matching the persisted completion contract" -- a contract that
    cannot be read and is never repaired, so the card was unclosable forever.
    """
    with connect() as conn:
        tid = kb.create_task(conn, title="corrupt", completion_contract="local-only")
        _store_contract(conn, tid, " ")

        assert kb.complete_task(conn, tid, result="done") is True
        assert kb.get_task(conn, tid).status == "done"
        assert _contract(conn, tid) == "local-only"
        repaired = _events(conn, tid, "completion_contract_repaired")
        assert len(repaired) == 1
        assert repaired[0]["previous"] == " "
        assert repaired[0]["previous_repr"] == "' '"
        assert repaired[0]["repaired_to"] == "local-only"
        assert "UNVERIFIED" in repaired[0]["warn"]        # the card's WARN row
        # The repair replaces the acceptance leg rather than skipping it silently: no
        # repository was ever judged, so no acceptance receipt is invented.
        assert _events(conn, tid, "pr_acceptance") == []


def test_an_empty_stored_contract_is_repaired_too(board):
    """R4 names an EMPTY column: an empty value cannot be told apart from a truncated one."""
    with connect() as conn:
        tid = kb.create_task(conn, title="empty", completion_contract="local-only")
        _store_contract(conn, tid, "")
        assert kb.complete_task(conn, tid, result="done") is True
        assert kb.get_task(conn, tid).status == "done"
        assert _contract(conn, tid) == "local-only"
        repaired = _events(conn, tid, "completion_contract_repaired")[-1]
        assert repaired["previous"] == ""
        assert "EMPTY" in repaired["reason"]
        assert "UNVERIFIED" in repaired["warn"]


@pytest.mark.parametrize("published_pr", [None, 657, PR_URL],
                         ids=["no-metadata", "bare-number", "full-url"])
def test_the_filing_card_history_closes_in_every_shape(board, forge, published_pr):
    """t_8805786a's exact history: blank stored contract + these three shapes, all closed.

    On 2026-10-19 these were the three refusals -- ``missing``, ``infra`` and ``missing`` --
    on a card whose land had demonstrably merged, and the merge sat on main while the log
    said the landing was still owed.

    The filing card's PR WAS MERGED (yan-hermes-skills#657 -> b8666eff), so every arm carries
    ``pr_state="MERGED"``: a reproduction of that history has to reproduce the merge state, or
    it proves only that an open PR on a check-green repository closes (comment 2255, DEFECT 2).
    The unmerged shape is pinned on its own, below.
    """
    forge["pr_state"] = "MERGED"
    with connect() as conn:
        tid = kb.create_task(conn, title="the filing card", completion_contract="local-only")
        _store_contract(conn, tid, " ")
        metadata = {} if published_pr is None else {"published_pr": published_pr}
        assert kb.complete_task(conn, tid, result="done", metadata=metadata) is True
        assert kb.get_task(conn, tid).status == "done"
        assert _contract(conn, tid) == "local-only"
        assert _events(conn, tid, "completion_contract_repaired")[-1]["previous"] == " "
        # No duplicate obligation is left behind: no blocked-landing row, nothing that a later
        # seat would read as "the land is still owed".
        assert _events(conn, tid, "completion_blocked_landing_carrier") == []


def test_a_repaired_contract_does_not_bind_the_card_to_the_declared_pr(board, forge):
    """The DEFECT-1 regression guard: the fleet's ordinary ``[land]`` shape must still close.

    ``empty contract + published_pr + landing_card + open PR`` -- and the repository declares
    NO required checks, which is the normal shape fleet-wide (no yoyodine repo can declare
    them). Measured at review (comment 2255): this CLOSED at base, and was REFUSED
    ``acceptance_refusal / pending`` by the revision that repaired the contract to the declared
    PR URL, leaving the card ``ready`` with its completion_contract bound to a declaration that
    succeeds only once the PR is MERGED -- the card's own defect class, and undoable only by an
    owner ``set-contract``.

    The ruling pinned here: the repair target is ``local-only`` (R4's letter), so NO acceptance
    leg is claimed and none is judged; the close is recorded unverified and the LANDING fence
    judges the declared PR, which is carried. An open PR with a carrier therefore closes -- as
    it did before the repair existed.
    """
    forge["pr_state"] = "OPEN"
    forge["required"] = []                       # no repository-required checks: the fleet shape
    with connect() as conn:
        tid = kb.create_task(conn, title="carried open pr", completion_contract="local-only")
        _store_contract(conn, tid, "")
        assert kb.complete_task(conn, tid, result="done", metadata={
            "published_pr": PR_URL, "landing_card": _carrier(conn, tid)}) is True
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "done"
        # Repaired to the value that claims nothing -- never to the PR URL.
        assert _contract(conn, tid) == "local-only"
        repaired = _events(conn, tid, "completion_contract_repaired")[-1]
        assert repaired["repaired_to"] == "local-only"
        assert "UNVERIFIED" in repaired["warn"]
        # Nothing is reported green: no acceptance leg ran, so no receipt is invented.
        assert _events(conn, tid, "pr_acceptance") == []
        assert task.last_failure_error is None


def test_a_repaired_contract_invents_no_landing_refusal(board, forge):
    """The repair never ADDS a refusal the board did not already have.

    The same close with NO carrier: an EMPTY contract is refused ``landing_carrier_missing``
    at base, and a repaired ``local-only`` is refused by the same cause with the same recorded
    escapes -- because ``_contract_names_no_pr`` normalises for ``owner/repo#N`` and neither
    value does. The contract therefore moves to the value that claims nothing and the close is
    judged exactly where it was judged before; nothing about the repair makes the card harder to
    close than the corrupt column did.
    """
    forge["pr_state"] = "OPEN"
    forge["landing_state"] = "open"
    with connect() as conn:
        tid = kb.create_task(conn, title="no carrier", completion_contract="local-only")
        _store_contract(conn, tid, "")
        refusal = _refusal(kb.complete_task(conn, tid, result="done",
                                            metadata={"published_pr": PR_URL}))
        assert refusal.cause == "landing_carrier_missing"
        assert _contract(conn, tid) == "local-only"      # repaired before the fence judged it
        assert "landing_card" in refusal.detail and "landing_deferred" in refusal.detail
        task = kb.get_task(conn, tid)
        assert task is not None and task.status != "done"


# ------------------------------------------------------------------ R1: degrade, not refuse


def test_an_unreachable_forge_closes_the_card_recording_it_unverified(board, forge):
    """R1: ``infra`` is evidence recorded unverified -- never a card that can never close."""
    forge["raise"] = OSError("gh: command not found")
    with connect() as conn:
        tid = kb.create_task(conn, title="unreadable forge", completion_contract="acme/repo")
        assert kb.complete_task(conn, tid, result="done", metadata={
            "published_pr": PR_URL, "landing_card": _carrier(conn, tid)}) is True

        task = kb.get_task(conn, tid)
        assert task.status == "done"
        assert task.last_failure_error is None          # a WARN, not a failure
        receipt = _events(conn, tid, "pr_acceptance")[-1]
        assert receipt["classification"] == "infra"
        assert receipt["degraded"] is True
        assert receipt["ok"] is False                   # recorded, never reported green
        assert "UNVERIFIED" in receipt["warn"]
        warn = _events(conn, tid, "pr_acceptance_unverified")[-1]
        assert warn["classification"] == "infra"
        assert warn["contract"] == "acme/repo"
        assert "acme/repo" in warn["warn"]


def test_an_identity_failure_still_refuses_and_names_the_fix(board, forge):
    """``auth`` stays a REFUSAL: an identity the operator can change, with its fix named.

    Deliberately outside R1's degrade (flagged to platform-stl on card ``t_c4a9d940``): the
    ruling named ``infra`` only, and this door is pinned by its own tests -- an assigned card's
    acceptance must never fall through to the ambient login (#122689). What R1 does insist on
    is that the refusal NAMES the contract it checked (R2).
    """
    acceptance = importlib.import_module("hermes_cli.kanban_pr_acceptance")
    forge["raise"] = acceptance._GateAuthError("HTTP 403 on graphql acme/repo")
    with connect() as conn:
        tid = kb.create_task(conn, title="no login", completion_contract="acme/repo")
        refusal = _refusal(kb.complete_task(conn, tid, result="done",
                                            metadata={"published_pr": PR_URL}))
        assert refusal.cause == "acceptance_refusal"
        assert "'acme/repo'" in refusal.detail
        assert _events(conn, tid, "pr_acceptance")[-1]["classification"] == "auth"
        assert _events(conn, tid, "pr_acceptance_unverified") == []


def test_degrading_does_not_excuse_the_landing_leg(board, forge):
    """R1 is about a FORGE read, not a board fence: an unverified close still owes a carrier."""
    forge["raise"] = OSError("gh: command not found")
    forge["landing_state"] = "open"          # the forge's own answer: the PR has not landed
    with connect() as conn:
        tid = kb.create_task(conn, title="unreadable forge", completion_contract="acme/repo")
        refusal = _refusal(kb.complete_task(conn, tid, result="done",
                                            metadata={"published_pr": PR_URL}))
        # The unread forge did not buy the close: the LANDING fence refused it, not acceptance.
        assert refusal.cause == "landing_carrier_missing"
        assert kb.get_task(conn, tid).status != "done"
        assert _events(conn, tid, "pr_acceptance_unverified") == []

        assert kb.complete_task(conn, tid, result="done", metadata={
            "published_pr": PR_URL, "landing_card": _carrier(conn, tid)}) is True
        assert kb.get_task(conn, tid).status == "done"


# --------------------------------------------------- R1 counter-proof: readable => refused


def test_an_open_pr_with_a_pending_check_still_refuses(board, forge):
    """The counter-proof: a forge that CAN be read still refuses an unlanded PR."""
    forge["status"] = "in_progress"
    with connect() as conn:
        tid = kb.create_task(conn, title="open pr", completion_contract="acme/repo")
        refusal = _refusal(kb.complete_task(conn, tid, result="done",
                                            metadata={"published_pr": PR_URL}))
        assert refusal.cause == "acceptance_refusal"
        assert kb.get_task(conn, tid).status != "done"
        assert _events(conn, tid, "pr_acceptance")[-1]["classification"] == "pending"
        assert _events(conn, tid, "pr_acceptance_unverified") == []


def test_a_red_check_still_refuses(board, forge):
    """A check that ran and failed is evidence, not an absent read."""
    forge["conclusion"] = "failure"
    with connect() as conn:
        tid = kb.create_task(conn, title="red ci", completion_contract="acme/repo")
        refusal = _refusal(kb.complete_task(conn, tid, result="done",
                                            metadata={"published_pr": PR_URL}))
        assert refusal.cause == "acceptance_refusal"
        assert _events(conn, tid, "pr_acceptance")[-1]["classification"] == "failure"
        assert _events(conn, tid, "pr_acceptance_unverified") == []


# ------------------------------------------------------------- R2: name the contract


def test_a_mismatched_publication_refusal_quotes_the_contract(board, forge):
    """R2: the refusal names the declaration verbatim, and what was offered against it."""
    with connect() as conn:
        tid = kb.create_task(conn, title="wrong repo", completion_contract="acme/repo")
        refusal = _refusal(kb.complete_task(conn, tid, result="done", metadata={
            "published_pr": "https://github.com/other/repo/pull/7"}))
        assert "'acme/repo'" in refusal.detail
        assert "other/repo" in refusal.detail
        assert "set-contract" in refusal.detail          # the remedy, not a generic retry


def test_a_non_string_publication_is_a_declaration_error_not_infra(board, forge):
    """R2: a bare number is a caller error to NAME, not a forge outage to retry.

    Run 2 of card ``t_c4a9d940`` persisted ``classification: infra`` for
    ``published_pr: 657``: a ``TypeError`` inside the URL match was reported as a GitHub
    outage, which sends the worker to check ``gh`` auth for a malformed card.
    """
    with connect() as conn:
        tid = kb.create_task(conn, title="bare number", completion_contract="acme/repo")
        refusal = _refusal(kb.complete_task(conn, tid, result="done",
                                            metadata={"published_pr": 657}))
        assert "657" in refusal.detail
        assert "'acme/repo'" in refusal.detail
        receipt = _events(conn, tid, "pr_acceptance")[-1]
        assert receipt["classification"] == "missing"
        assert receipt["cause"] == "declaration_mismatch"


# ------------------------------------------------------------------------- R3: force


def test_force_does_not_bypass_acceptance_and_the_refusal_says_so(board, forge):
    """R3: ``--force`` overrides the live-claim guard only -- and now says so out loud."""
    forge["status"] = "in_progress"
    with connect() as conn:
        tid = kb.create_task(conn, title="forced", completion_contract="acme/repo")
        refusal = _refusal(kb.complete_task(conn, tid, result="done", force=True,
                                            metadata={"published_pr": PR_URL}))
        assert refusal.cause == "acceptance_refusal"
        assert "--force does not bypass" in refusal.detail
        assert kb.get_task(conn, tid).status != "done"
