"""The landing-carrier clause (``hermes_cli/kanban_landing_carrier.py``) over the REAL door.

Card ``t_88f6cb12`` (design ruling ``t_9333ca24``): a review close that DECLARES a
deliverable PR must name its landing leg, or the close is refused
``landing_carrier_missing``. Card ``t_502a28bd`` (ruling platform-stl 2026-10-14)
WIDENED the trigger from the approval TRANSITION to the DECLARATION itself:
``evaluate()`` disposed of a close by its status (``status != "review" -> None``),
so a card whose own completion metadata recorded a PUBLISHED PR closed
``local-only`` with no carrier and no owner -- three measured orphans on
2026-10-14 (``yaan-hermes-skills#542``/``#545``, ``yaan-platform#1373``), each
carried by hand tens of seconds AFTER the close the clause never judged. The tests
below therefore drive BOTH transitions through the production door --
``kanban_db.complete_task`` for the kernel door, ``tools.kanban_tools._handle_complete``
for the tool door -- against a real board store and a real schema, so what is
asserted is the install's own behaviour and not a re-implementation of the clause.
Two seams are stubbed, and only two: the FORGE, at the clause's own transport seam
(``kanban_landing_carrier._default_forge``), and -- in the contract-declaration
tests alone -- ``kanban_pr_acceptance_store.collect_acceptance``, the
repository-CI door the landing fence deliberately sits behind.

Regression surface (the measured class): 538 review closes in a 7-day window,
396 of them declaring a deliverable PR, 48 declared PRs OPEN at the forge with no
carrier on any board -- plus every worker close that records a publication.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_landing_carrier as klc

PR = "example-org/example-repo#7"
PR_URL = "https://github.com/example-org/example-repo/pull/7"


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    """An isolated kanban root with a real (empty) board store."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_WORKSPACES_ROOT"):
        monkeypatch.delenv(var, raising=False)
    import hermes_constants

    hermes_constants._default_hermes_root_memo = None
    kb._INITIALIZED_PATHS.clear()
    kbc.init_db()
    return home


@pytest.fixture
def conn(fresh_home):
    connection = kbc.connect()
    yield connection
    connection.close()


@pytest.fixture
def forge(monkeypatch):
    """Stub the clause's forge transport; records every PR it was asked about.

    ``state['value']`` is the answer for any PR not named in ``per_pr`` (keyed
    ``(owner, number)``); the value ``"unreachable"`` makes the transport raise.
    """
    asked: list[tuple[str, str]] = []
    state = {"value": "open"}
    per_pr: dict[tuple[str, str], str] = {}

    def _default_forge(_assignee):
        def _call(repo, number):
            if state["value"] == "unreachable":
                raise klc.ForgeUnreachable("stubbed: gh could not reach the forge")
            asked.append((repo, number))
            return per_pr.get((repo, number), state["value"])

        return _call

    monkeypatch.setattr(klc, "_default_forge", _default_forge)
    return {"asked": asked, "state": state, "per_pr": per_pr}


def _review_card(conn, title="a review card") -> str:
    task_id = kb.create_task(conn, title=title, initial_status="running")
    assert kb.request_review(conn, task_id, summary="ready for review") is True
    assert _status(conn, task_id) == "review"
    return task_id


def _seed_run(conn, task_id, metadata) -> None:
    """A completed run carrying the declaration (the fleet writes PR keys on runs)."""
    conn.execute(
        "INSERT INTO task_runs (task_id, status, started_at, ended_at, outcome, metadata) "
        "VALUES (?, 'done', ?, ?, 'completed', ?)",
        (task_id, int(time.time()) - 10, int(time.time()) - 5, json.dumps(metadata)),
    )
    conn.commit()


def _status(conn, task_id) -> str:
    return conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]


def _event_kinds(conn, task_id) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (task_id,))]


def _blocked_events(conn, task_id) -> list[dict]:
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'completion_blocked_landing_carrier'",
        (task_id,),
    ).fetchall()
    return [json.loads(row[0] or "{}") for row in rows]


def _row_snapshot(conn, task_id):
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()[:]


# --------------------------------------------------------------------------- #
# the refusal
# --------------------------------------------------------------------------- #


def test_open_declared_pr_with_no_carrier_is_refused(conn, forge):
    """The class this clause exists for: PR still OPEN, no carrier, board would go green."""
    task_id = _review_card(conn)
    before = _row_snapshot(conn, task_id)

    refusal = kb.complete_task(
        conn, task_id, summary="reviewed: LGTM", metadata={"published_pr": PR_URL},
    )

    assert isinstance(refusal, kb.CompletionRefusal)
    assert refusal.cause == "landing_carrier_missing"
    assert bool(refusal) is False
    # The remedy names the PR, the carrier spec, and both escapes -- inline, so the
    # refused card can always be unblocked without another card landing.
    assert PR in refusal.detail
    assert f'kanban_create(title="[land] {PR} for {task_id}"' in refusal.detail
    assert "metadata.landing_deferred" in refusal.detail
    assert "metadata.landing_not_owed" in refusal.detail
    assert "metadata.landing_card" in refusal.detail
    assert forge["asked"] == [("example-org/example-repo", "7")]

    # Byte-untouched: the card is still in review, its row is identical, and the
    # transition recorded exactly ONE auditable event.
    assert _row_snapshot(conn, task_id) == before
    assert _status(conn, task_id) == "review"
    events = _blocked_events(conn, task_id)
    assert len(events) == 1
    assert events[0]["cause"] == "landing_carrier_missing"
    assert events[0]["facts"]["unsatisfied"] == [PR]
    assert _event_kinds(conn, task_id).count("completed") == 0


def test_declaration_on_a_run_not_the_closing_call_is_still_the_card_s_declaration(conn, forge):
    """``published_pr`` lives on whichever run opened the PR (711 uses/7d)."""
    task_id = _review_card(conn)
    _seed_run(conn, task_id, {"published_pr": PR_URL})

    refusal = kb.complete_task(conn, task_id, summary="approved")

    assert isinstance(refusal, kb.CompletionRefusal)
    assert refusal.cause == "landing_carrier_missing"


def test_force_does_not_bypass_the_fence(conn, forge):
    """``--force`` and drag-to-done are not doors around a declaration-based fence."""
    task_id = _review_card(conn)

    refusal = kb.complete_task(
        conn, task_id, summary="forcing it", metadata={"published_pr": PR_URL}, force=True,
    )

    assert isinstance(refusal, kb.CompletionRefusal)
    assert refusal.cause == "landing_carrier_missing"
    assert _status(conn, task_id) == "review"


def test_unreachable_forge_with_nothing_declared_is_landing_state_unknown(conn, forge):
    """Fail-closed, but on a channel whose remedy needs no network."""
    forge["state"]["value"] = "unreachable"
    task_id = _review_card(conn)

    refusal = kb.complete_task(conn, task_id, summary="approved", metadata={"published_pr": PR_URL})

    assert isinstance(refusal, kb.CompletionRefusal)
    assert refusal.cause == "landing_state_unknown"
    assert "landing_deferred" in refusal.detail
    events = _blocked_events(conn, task_id)
    assert len(events) == 1
    assert events[0]["cause"] == "landing_state_unknown"
    assert _status(conn, task_id) == "review"


# --------------------------------------------------------------------------- #
# the passes
# --------------------------------------------------------------------------- #


def test_merged_declared_pr_passes(conn, forge):
    forge["state"]["value"] = "merged"
    task_id = _review_card(conn)

    assert kb.complete_task(conn, task_id, summary="landed", metadata={"published_pr": PR_URL}) is True
    assert _status(conn, task_id) == "done"
    assert _blocked_events(conn, task_id) == []


def test_declared_landing_card_passes_without_touching_the_forge(conn, forge):
    """Escape (a): name the carrier that already exists."""
    carrier = kb.create_task(conn, title=f"[land] {PR} for somebody", initial_status="running")
    task_id = _review_card(conn)

    assert kb.complete_task(
        conn, task_id, summary="carried", metadata={"published_pr": PR_URL, "landing_card": carrier},
    ) is True
    assert forge["asked"] == []


def test_linked_child_naming_the_pr_passes_without_touching_the_forge(conn, forge):
    """Escape (b): the doctrine chain review -> [land*] -> deploy, expressed as an edge."""
    task_id = _review_card(conn)
    child = kb.create_task(conn, title="carry the landing", body=f"lands {PR_URL}",
                           initial_status="running")
    kb.link_tasks(conn, task_id, child)

    assert kb.complete_task(conn, task_id, summary="carried", metadata={"published_pr": PR_URL}) is True
    assert forge["asked"] == []


def test_carrier_card_on_another_board_passes_without_touching_the_forge(conn, forge):
    """Escape (c): any board's ``[land ...``/``[owner act]`` card naming the PR.

    Measured on the fleet: ``[owner act] Land t_0051db75 ...`` is a real carrier a
    ``^[land`` filter misses -- hence the two title shapes.
    """
    kb.create_board("other-board")
    with kbc.connect(board="other-board") as other:
        kb.create_task(other, title=f"[owner act] Land the reviewed change ({PR})",
                       initial_status="running")
    task_id = _review_card(conn)

    assert kb.complete_task(conn, task_id, summary="carried", metadata={"published_pr": PR_URL}) is True
    assert forge["asked"] == []


def test_landing_not_owed_naming_the_replacement_passes_without_touching_the_forge(conn, forge):
    task_id = _review_card(conn)

    assert kb.complete_task(
        conn, task_id, summary="superseded", metadata={
            "published_pr": PR_URL,
            "landing_not_owed": "superseded by example-org/example-repo#9 (re-cut on main)",
        },
    ) is True
    assert forge["asked"] == []


def test_bodyless_not_owed_does_not_exempt_the_close(conn, forge):
    """An exemption that names no replacement is the silent door, so it is not an escape."""
    task_id = _review_card(conn)

    refusal = kb.complete_task(
        conn, task_id, summary="says it is fine",
        metadata={"published_pr": PR_URL, "landing_not_owed": "superseded"},
    )

    assert isinstance(refusal, kb.CompletionRefusal)
    assert refusal.cause == "landing_carrier_missing"


def test_bare_recorded_deferral_is_not_an_escape(conn, forge):
    """Tightened 2026-10-28: "owed, not yet carried" IS the open state.

    A >= 20-character reason used to close a card whose PR was unmerged; three cards
    closed ``done`` that way in one day while the defects they targeted stayed live.
    """
    forge["state"]["value"] = "open"
    task_id = _review_card(conn)

    refusal = kb.complete_task(
        conn, task_id, summary="landing owed to the reconciler",
        metadata={"published_pr": PR_URL,
                  "landing_deferred": "owed: the reconciler has not filed the carrier yet"},
    )

    assert isinstance(refusal, kb.CompletionRefusal)
    assert refusal.cause == "landing_carrier_missing"
    assert _status(conn, task_id) == "review"


def test_deferral_naming_a_real_carrier_card_passes(conn, forge):
    """The deferral is a POINTER to the carrier, and the carrier does the work."""
    carrier = kb.create_task(conn, title=f"[land] {PR} for somebody", initial_status="running")
    task_id = _review_card(conn)

    assert kb.complete_task(
        conn, task_id, summary="carried",
        metadata={"published_pr": PR_URL, "landing_deferred": f"owed until {carrier} lands it"},
    ) is True
    assert forge["asked"] == []


def test_deferral_naming_a_card_that_does_not_carry_the_pr_is_refused(conn, forge):
    """A named card that does not name the PR is not a carrier -- it is a typo with a reason."""
    other = kb.create_task(conn, title="an unrelated card", initial_status="running")
    forge["state"]["value"] = "open"
    task_id = _review_card(conn)

    refusal = kb.complete_task(
        conn, task_id, summary="carried",
        metadata={"published_pr": PR_URL, "landing_deferred": f"owed until {other} lands it"},
    )

    assert isinstance(refusal, kb.CompletionRefusal)
    assert refusal.cause == "landing_carrier_missing"


def test_deploy_stamp_that_resolves_in_the_ledger_passes(conn, forge, monkeypatch):
    """The EVIDENCE leg: a landed revision named by the dest stamp closes the card."""
    from hermes_cli import kanban_landing_carrier as clause

    monkeypatch.setattr(clause, "ledger_resolves", lambda sha: str(sha).startswith("abc1234"))
    forge["state"]["value"] = "open"
    task_id = _review_card(conn)

    assert kb.complete_task(
        conn, task_id, summary="landed by the train",
        metadata={"published_pr": PR_URL, "deploy_stamp": "abc1234def"},
    ) is True
    assert forge["asked"] == []


def test_deploy_stamp_that_does_not_resolve_is_refused(conn, forge, monkeypatch):
    """An unverifiable stamp is intent, not evidence."""
    from hermes_cli import kanban_landing_carrier as clause

    monkeypatch.setattr(clause, "ledger_resolves", lambda sha: False)
    forge["state"]["value"] = "open"
    task_id = _review_card(conn)

    refusal = kb.complete_task(
        conn, task_id, summary="landed by the train",
        metadata={"published_pr": PR_URL, "deploy_stamp": "deadbeef11"},
    )

    assert isinstance(refusal, kb.CompletionRefusal)
    assert refusal.cause == "landing_carrier_missing"


def test_stub_deferral_is_not_an_escape(conn, forge):
    """Under the floor the deferral is a silent door, so the close is refused."""
    task_id = _review_card(conn)

    refusal = kb.complete_task(
        conn, task_id, summary="deferred", metadata={"published_pr": PR_URL, "landing_deferred": "later"},
    )

    assert isinstance(refusal, kb.CompletionRefusal)
    assert refusal.cause == "landing_carrier_missing"


# --------------------------------------------------------------------------- #
# untouched by construction
# --------------------------------------------------------------------------- #


def test_card_declaring_nothing_is_untouched(conn, forge):
    task_id = _review_card(conn)

    assert kb.complete_task(conn, task_id, summary="a design card: approved") is True
    assert _status(conn, task_id) == "done"
    assert forge["asked"] == []
    assert _blocked_events(conn, task_id) == []


def test_bare_pr_number_without_a_repository_is_not_a_declaration(conn, forge):
    """No repo can be derived from a bare number without guesswork -- so it declares nothing."""
    task_id = _review_card(conn)

    assert kb.complete_task(conn, task_id, summary="approved", metadata={"pr_number": 7}) is True
    assert forge["asked"] == []


# --------------------------------------------------------------------------- #
# the WIDENED trigger: the declaration + the close, on every transition
# (card t_502a28bd; ruling platform-stl 2026-10-14)
#
# ``evaluate()`` used to return None for anything that was not ``status ==
# "review"``, so a worker's own close could record a PUBLISHED PR and be admitted
# with no carrier and no owner. These tests are the hole, closed.
# --------------------------------------------------------------------------- #


def _worker_card(conn, title="a worker card", **kw) -> str:
    """A card in the state a dispatcher's worker closes from: ``running``.

    ``create_task`` lands an UNPARENTED card in ``ready`` (``initial_task_state`` --
    ``initial_status="running"`` is the dispatcher's request, not the stored state),
    so the row is moved to ``running`` exactly as a claim does. This is the
    transition the clause used to dispose of: ``if status != "review": return None``.
    Measured on the two 2026-10-14 orphans (``t_0384ee12``, ``t_a884432e``): each
    carries ``spawned``/``claimed``/``heartbeat``/``completed`` events and NO
    ``review_requested`` at all -- neither ever entered ``review``, so the clause was
    never asked about the PR their own close recorded.
    """
    task_id = kb.create_task(conn, title=title, initial_status="running", **kw)
    conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task_id,))
    conn.commit()
    return task_id


def test_worker_close_declaring_a_pr_with_no_carrier_is_refused(conn, forge):
    """(a) The measured orphan shape: the card's own close records a PR, nothing carries it."""
    task_id = _worker_card(conn)
    before = _row_snapshot(conn, task_id)

    refusal = kb.complete_task(conn, task_id, summary="shipped it",
                               metadata={"published_pr": PR_URL})

    assert isinstance(refusal, kb.CompletionRefusal)
    assert refusal.cause == "landing_carrier_missing"
    assert PR in refusal.detail
    assert f'kanban_create(title="[land] {PR} for {task_id}"' in refusal.detail
    assert forge["asked"] == [("example-org/example-repo", "7")]

    # Byte-untouched, still a worker card, and exactly ONE auditable event -- whose
    # facts now carry the transition the close came from.
    assert _row_snapshot(conn, task_id) == before
    assert _status(conn, task_id) == "running"
    events = _blocked_events(conn, task_id)
    assert len(events) == 1
    assert events[0]["cause"] == "landing_carrier_missing"
    assert events[0]["facts"]["unsatisfied"] == [PR]
    assert events[0]["facts"]["status"] == "running"
    assert _event_kinds(conn, task_id).count("completed") == 0


def test_worker_close_declaring_its_landing_card_passes_without_touching_the_forge(conn, forge):
    """(b) The same close, with the carrier named, is admitted -- and costs no forge read."""
    carrier = kb.create_task(conn, title=f"[land] {PR} for somebody", initial_status="running")
    task_id = _worker_card(conn)

    assert kb.complete_task(
        conn, task_id, summary="shipped it",
        metadata={"published_pr": PR_URL, "landing_card": carrier},
    ) is True
    assert _status(conn, task_id) == "done"
    assert forge["asked"] == []
    assert _blocked_events(conn, task_id) == []


def test_worker_close_declaring_nothing_is_untouched(conn, forge):
    """(c) Untouched BY CONSTRUCTION -- the property that keeps design/research/local cards out."""
    task_id = _worker_card(conn)

    assert kb.complete_task(conn, task_id, summary="a design card: done") is True
    assert _status(conn, task_id) == "done"
    assert forge["asked"] == []
    assert _blocked_events(conn, task_id) == []


def test_force_does_not_bypass_the_fence_on_a_worker_close(conn, forge):
    """(d) ``--force`` is not a door around the widened fence either."""
    task_id = _worker_card(conn)

    refusal = kb.complete_task(conn, task_id, summary="forcing it",
                               metadata={"published_pr": PR_URL}, force=True)

    assert isinstance(refusal, kb.CompletionRefusal)
    assert refusal.cause == "landing_carrier_missing"
    assert _status(conn, task_id) == "running"


def test_local_only_contract_with_a_declaration_names_the_contradiction_first(conn, forge):
    """(e) The contract word denies the publication its own metadata records.

    Both remedies are offered and the contract is NOT demanded to change: the close can
    name its landing leg, or an owner can re-scope the declaration to the exact PR URL --
    a route that now CLOSES (a repository with no required checks is satisfied by the
    PR's MERGED state). ``local-only`` is never offered as the remedy, because a card
    that published has already refuted it (ruling 2026-10-18, card ``t_9082c751``).
    """
    task_id = _worker_card(conn, title="a local-only card that published",
                           completion_contract="local-only")

    refusal = kb.complete_task(conn, task_id, summary="done", metadata={"published_pr": PR_URL})

    assert isinstance(refusal, kb.CompletionRefusal)
    assert refusal.cause == "landing_carrier_missing"
    detail = refusal.detail
    assert detail.startswith("CONTRADICTION FIRST"), detail[:160]
    assert "'local-only'" in detail
    assert PR in detail
    assert "set-contract" in detail, detail
    # The offered target must be the URL form: `set-contract` refuses an `owner/repo#N` short
    # ref, so naming the normalised ref there would be advice that cannot be followed.
    assert f"set-contract <task> {PR_URL}" in detail, detail
    assert "set-contract <task> example-org/example-repo#7" not in detail
    assert "MERGED" in detail, detail
    assert "Do not re-declare it 'local-only'" in detail, detail
    assert "does NOT have to be flipped" not in detail
    assert f'kanban_create(title="[land] {PR} for {task_id}"' in detail
    assert "metadata.landing_deferred" in detail
    assert _status(conn, task_id) == "running"


def test_a_fleet_looser_key_is_a_tier_two_declaration(conn, forge):
    """The ``t_45655ee5`` shape: a card whose only declaration is ``pr`` is JUDGED.

    ``pr`` / ``pr_url`` / ``pr_number`` / ``merge_pr`` / ``pull_request`` are tier-2
    DECLARATIONS (ruling 2026-10-23, card ``t_f1dc66dc`` -- ONE DECLARATION SURFACE),
    aligned onto the key set the landing reconciler reads
    (``landing_reconcile_nodes.PR_KEYS``). The defect this closes: the reconciler filed
    carrier ``t_f4349dba`` from ``t_45655ee5``'s ``metadata.pr`` while the verdict door --
    the one sanctioned way to discharge it -- refused the same card as declaring nothing,
    leaving a prose comment or an endless carrier re-file.

    This SUPERSEDES the 2026-10-18 ruling (card ``t_9082c751``) under which these keys were
    mentions only and created no obligation: the close is now the clause's business, and the
    recorded escapes (``landing_not_owed`` / ``landing_deferred``) are the way out. The keys
    are still reported as ``mentions`` on the verdict's facts -- recording, not judging.
    """
    task_id = _worker_card(conn, title="a card that declares in pr",
                           completion_contract="local-only")

    refusal = kb.complete_task(
        conn, task_id, summary="done",
        metadata={"pr": PR_URL},
    )

    assert isinstance(refusal, kb.CompletionRefusal)
    assert refusal.cause == "landing_carrier_missing"
    assert PR in refusal.detail
    assert _status(conn, task_id) == "running"
    facts = _blocked_events(conn, task_id)[0]["facts"]
    assert facts["declaration_tier"] == 2, "the fleet's looser key resolves at tier 2"
    assert facts["declaration_keys"] == ["pr"]
    assert forge["asked"] == [("example-org/example-repo", "7")]


@pytest.mark.parametrize("key", ["merge_pr", "pull_request"])
def test_the_reconciler_s_other_keys_declare_too(conn, forge, key):
    """The tier-2 surface is the reconciler's ``PR_KEYS``, not just the three most common."""
    task_id = _review_card(conn)

    refusal = kb.complete_task(conn, task_id, summary="approved", metadata={key: PR_URL})

    assert isinstance(refusal, kb.CompletionRefusal)
    assert refusal.cause == "landing_carrier_missing"
    assert _blocked_events(conn, task_id)[0]["facts"]["declaration_keys"] == [key]
    assert forge["asked"] == [("example-org/example-repo", "7")]


def test_the_tier_two_surface_is_the_reconciler_s_key_set():
    """Pin tier 2 to the reconciler's key set AS MEASURED at yaan-platform @6091b5da.

    ``landing_reconcile_nodes.PR_KEYS`` (yaan-platform ``origin/main`` @6091b5da) minus
    ``published_pr`` -- which is this clause's tier 1 -- IS tier 2, so a carrier can no
    longer be filed from a declaration the door cannot discharge (exactly the hole
    ``t_45655ee5`` / ``t_f4349dba`` measured).

    SCOPE OF THE GUARD (corrected in review round 1, R4): ``reconciler_pr_keys`` below is a
    FROZEN LOCAL COPY, so this test catches drift on THIS side only. A key added to the
    reconciler's ``PR_KEYS`` in yaan-platform does NOT fail this test -- the copy must be
    updated by hand, and that manual step is the guard's known limit, not a claim it makes.
    """
    reconciler_pr_keys = (
        "published_pr", "pr", "pr_url", "pr_number", "merge_pr", "pull_request",
    )

    assert klc.TIER_KEYS[0] == ("published_pr",), "tier 1 is the kernel's publication key"
    assert klc.TIER_KEYS[1] == tuple(k for k in reconciler_pr_keys if k != "published_pr")


def test_measured_mentions_are_recorded_on_the_verdict_facts(conn, forge):
    """A refusal keeps the mentions it did not judge, so the audit trail is complete."""
    task_id = _review_card(conn)
    _seed_run(conn, task_id, {"pr_url": "https://github.com/other-org/other-repo/pull/12"})

    refusal = kb.complete_task(conn, task_id, summary="approved",
                               metadata={"published_pr": PR_URL})

    assert isinstance(refusal, kb.CompletionRefusal)
    mentions = _blocked_events(conn, task_id)[0]["facts"]["mentions"]
    assert {"key": "pr_url",
            "value": "https://github.com/other-org/other-repo/pull/12",
            "ref": "other-org/other-repo#12"} in mentions


def test_contradiction_is_not_named_when_the_contract_declares_the_pr(conn, forge, monkeypatch):
    """Negative control: a contract that already names a PR is not a contradiction."""
    from hermes_cli import kanban_pr_acceptance_store as store

    monkeypatch.setattr(store, "collect_acceptance", lambda contract, published_pr, assignee=None: {
        "ok": True, "classification": "success", "detail": "", "recovery": "", "checks": []})
    task_id = kb.create_task(conn, title="contract card", initial_status="running",
                             completion_contract=PR_URL)

    refusal = kb.complete_task(conn, task_id, summary="done", metadata={"published_pr": PR_URL})

    assert isinstance(refusal, kb.CompletionRefusal)
    assert refusal.cause == "landing_carrier_missing"
    assert "CONTRADICTION FIRST" not in refusal.detail


def test_exact_pr_url_contract_is_a_declaration(conn, forge, monkeypatch):
    """Tier 3: the card's own completion contract.

    The repository acceptance door is stubbed green because the fence sits AFTER it
    (a PR with red CI is told CI is red: a close the acceptance clause already refused
    is answered by that clause and the landing fence never costs a forge read) --
    this test is about the declaration, not about CI.
    """
    from hermes_cli import kanban_pr_acceptance_store as store

    monkeypatch.setattr(store, "collect_acceptance", lambda contract, published_pr, assignee=None: {
        "ok": True, "classification": "success", "detail": "", "recovery": "", "checks": []})
    task_id = kb.create_task(conn, title="contract card", initial_status="running",
                             completion_contract=PR_URL)
    assert kb.request_review(conn, task_id, summary="ready for review") is True

    refusal = kb.complete_task(conn, task_id, summary="approved")

    assert isinstance(refusal, kb.CompletionRefusal)
    assert refusal.cause == "landing_carrier_missing"


def test_published_pr_beats_the_fleet_pr_key(conn, forge):
    """Tier precedence is deterministic: the kernel's own key wins over the fleet's."""
    task_id = _review_card(conn)
    _seed_run(conn, task_id, {"pr": "example-org/example-repo#99"})

    refusal = kb.complete_task(conn, task_id, summary="approved", metadata={"published_pr": PR_URL})

    assert isinstance(refusal, kb.CompletionRefusal)
    assert PR in refusal.detail
    assert "example-repo#99" not in refusal.detail
    assert forge["asked"] == [("example-org/example-repo", "7")]


def test_every_declared_pr_must_satisfy_the_rule(conn, forge):
    """A card may declare two PRs (measured); each owes its own landing leg."""
    forge["per_pr"][("example-org/example-repo", "7")] = "merged"
    task_id = _review_card(conn, title="two declared PRs")
    _seed_run(conn, task_id, {"published_pr": "other-org/other-repo#12"})

    refusal = kb.complete_task(conn, task_id, summary="approved", metadata={"published_pr": PR_URL})

    assert isinstance(refusal, kb.CompletionRefusal)
    assert refusal.cause == "landing_carrier_missing"
    # Both declarations were adjudicated; only the unlanded one is named as owed.
    assert sorted(forge["asked"]) == [("example-org/example-repo", "7"),
                                      ("other-org/other-repo", "12")]
    assert "other-org/other-repo#12" in refusal.detail
    assert PR not in refusal.detail
    assert _status(conn, task_id) == "review"


def test_unresolvable_assignee_profile_fails_closed_without_touching_the_forge(conn, monkeypatch):
    """An assignee that cannot be resolved is an UNREACHABLE channel, not the ambient login."""
    def _boom(*_args, **_kwargs):
        raise AssertionError("the forge must not be consulted for an unresolvable assignee")

    monkeypatch.setattr(klc, "forge_state", _boom)
    task_id = _review_card(conn)
    conn.execute("UPDATE tasks SET assignee = ? WHERE id = ?", ("no-such-lane-zzz", task_id))
    conn.commit()

    refusal = kb.complete_task(conn, task_id, summary="approved", metadata={"published_pr": PR_URL})

    assert isinstance(refusal, kb.CompletionRefusal)
    assert refusal.cause == "landing_state_unknown"
    assert _status(conn, task_id) == "review"


# --------------------------------------------------------------------------- #
# the operator door renders the same refusal
# --------------------------------------------------------------------------- #


def test_cli_complete_renders_the_cause_and_its_remedy(conn, forge, capsys, monkeypatch):
    """`hermes kanban complete` must show the refusal, not "unknown id or terminal state".

    The refusal is DATA; a door that swallows it leaves the operator with a card that
    is neither unknown nor terminal and no way to satisfy the fence.
    """
    import argparse

    from hermes_cli import kanban as kc

    task_id = _review_card(conn)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    rc = kc._cmd_complete(argparse.Namespace(
        task_ids=[task_id], result=None, summary="approved",
        metadata=json.dumps({"published_pr": PR_URL}), force=False))

    printed = capsys.readouterr()
    text = printed.out + printed.err
    assert rc != 0
    assert "landing_carrier_missing" in text
    assert PR in text
    assert "landing_deferred" in text
    assert "unknown id or terminal state" not in text
    assert _status(conn, task_id) == "review"


# --------------------------------------------------------------------------- #
# the install's own failure mode
# --------------------------------------------------------------------------- #


def test_missing_clause_module_fails_closed_with_no_deferral_waiver(conn, forge, monkeypatch):
    """A lost override must never silently admit closes -- including via a deferral.

    Fail-closed on the same refusal channel, and the closing call's own recorded
    deferral is NOT a way out (tightened 2026-10-28: it used to be, which is the one
    state where nobody could judge the landing at all). The remedy names the carrier
    metadata the close owes once the clause is loadable again.
    """
    import hermes_cli

    monkeypatch.delattr(hermes_cli, "kanban_landing_carrier")
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_landing_carrier", None)

    task_id = _review_card(conn)
    refusal = kb.complete_task(conn, task_id, summary="approved", metadata={"published_pr": PR_URL})
    assert isinstance(refusal, kb.CompletionRefusal)
    assert refusal.cause == "landing_state_unknown"
    assert _status(conn, task_id) == "review"

    deferred = kb.complete_task(
        conn, task_id, summary="approved",
        metadata={"published_pr": PR_URL,
                  "landing_deferred": "owed: carrier not filed yet, recorded for the reconciler"},
    )
    assert isinstance(deferred, kb.CompletionRefusal)
    assert deferred.cause == "landing_state_unknown"
    assert "metadata.landing_card" in deferred.detail
    assert _status(conn, task_id) == "review"


# --------------------------------------------------------------------------- #
# ordering is PINNED: the acceptance clause answers its close first
# --------------------------------------------------------------------------- #


def test_acceptance_clause_answers_before_the_landing_fence(conn, forge, monkeypatch):
    """A card that fails BOTH clauses is told the acceptance verdict, and no forge read happens.

    The fence sits AFTER ``prepare_acceptance``: a PR with red CI is told CI is red, and a
    close the acceptance clause already refused never costs a forge read. Reordering the two
    -- the defect this test pins -- is how the record's claimed safety property goes false.
    """
    from hermes_cli import kanban_pr_acceptance_store as store

    monkeypatch.setattr(store, "collect_acceptance", lambda contract, published_pr, assignee=None: {
        "ok": False, "classification": "failure", "detail": "the required check is red",
        "recovery": "push a fix", "checks": []})
    task_id = kb.create_task(conn, title="contract card", initial_status="running",
                             completion_contract=PR_URL)
    assert kb.request_review(conn, task_id, summary="ready for review") is True

    refusal = kb.complete_task(conn, task_id, summary="approved",
                               metadata={"published_pr": PR_URL})

    assert isinstance(refusal, kb.CompletionRefusal)
    assert refusal.cause == "acceptance_refusal", refusal.cause
    assert forge["asked"] == []                       # the landing fence never ran
    assert _blocked_events(conn, task_id) == []


# --------------------------------------------------------------------------- #
# the tool door renders the same refusal
# --------------------------------------------------------------------------- #


def _tool_door_close(conn, monkeypatch, task_id, metadata) -> str:
    """Close through ``tools.kanban_tools._handle_complete`` -- the review lane's own door.

    No ``HERMES_KANBAN_TASK``: a card sitting in ``review`` has no owning run (the review
    transition ends it), so the closing caller arrives on the orchestrator/operator path
    with an explicit ``task_id`` -- the shape the review approval actually takes.
    """
    from tools import kanban_tools as kt

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    out = json.loads(kt._handle_complete(
        {"task_id": task_id, "summary": "approved", "metadata": metadata}))
    return out.get("error") or ""


def test_tool_door_renders_the_landing_cause_and_its_remedy(conn, forge, monkeypatch):
    """``kanban_complete`` must render the fence, not a run-ownership sentence.

    ``_handle_complete`` renders ``deferral_*`` and ``acceptance_refusal``; every other
    ``CompletionRefusal`` falls to ``kb.live_row_refusal(...)``, which answers a landing
    refusal with "the live row is status=review; no run owns it" -- no cause, no PR, no
    ``kanban_create`` spec, no escape. The REVIEW LANE closes through this door, so a fence
    that is unreadable here is a fence the fleet cannot satisfy.
    """
    task_id = _review_card(conn)

    err = _tool_door_close(conn, monkeypatch, task_id, {"published_pr": PR_URL})

    assert "landing_carrier_missing" in err, err
    assert PR in err, err
    assert f'kanban_create(title="[land] {PR} for {task_id}"' in err, err
    assert "metadata.landing_deferred" in err, err
    assert "metadata.landing_not_owed" in err, err
    assert "no run owns it" not in err, err
    assert _status(conn, task_id) == "review"


def test_tool_door_renders_the_unknown_landing_state_too(conn, forge, monkeypatch):
    """``landing_state_unknown`` needs the same rendering: its escape needs no network."""
    forge["state"]["value"] = "unreachable"
    task_id = _review_card(conn)

    err = _tool_door_close(conn, monkeypatch, task_id, {"published_pr": PR_URL})

    assert "landing_state_unknown" in err, err
    assert "landing_deferred" in err, err
    assert "no run owns it" not in err, err
    assert _status(conn, task_id) == "review"


def test_tool_door_renders_the_widened_refusal_for_a_worker_close(conn, forge, monkeypatch):
    """The tool door must render the fence on a NON-review close too (card t_502a28bd).

    ``_handle_complete`` branches on the CAUSE, so nothing in the tool layer needed
    widening -- and this is the test that keeps it that way: a worker close that
    declares a PR is now refused by the kernel, and the worker has to be able to
    read why. The old fall-through answered it with a run-ownership sentence.
    """
    task_id = _worker_card(conn)

    err = _tool_door_close(conn, monkeypatch, task_id, {"published_pr": PR_URL})

    assert "landing_carrier_missing" in err, err
    assert PR in err, err
    assert f'kanban_create(title="[land] {PR} for {task_id}"' in err, err
    assert "metadata.landing_card" in err, err
    assert "metadata.landing_deferred" in err, err
    assert "no run owns it" not in err, err
    assert _status(conn, task_id) == "running"


def test_tool_door_renders_the_contradiction_for_a_local_only_worker_close(conn, forge, monkeypatch):
    """The contradiction reaches the worker's own door, not just the kernel's."""
    task_id = _worker_card(conn, title="local-only worker card",
                           completion_contract="local-only")

    err = _tool_door_close(conn, monkeypatch, task_id, {"published_pr": PR_URL})

    assert "CONTRADICTION FIRST" in err, err
    assert "landing_carrier_missing" in err, err
    assert _status(conn, task_id) == "running"


# --------------------------------------------------------------------------- #
# the carrier IDENTITY: the key first, prose as the legacy fallback
# (card t_4a87bb75, ruling t_2bc058fc)
# --------------------------------------------------------------------------- #


def _pr_url(ref: str) -> str:
    repo, _, number = ref.partition("#")
    return f"https://github.com/{repo}/pull/{number}"


def _archived_carrier(conn, *, key=None, title=None, body=None) -> str:
    """A carrier card that served its landing and was then archived by the sweep.

    Archiving is what the board does to a carrier once its PR lands, so an archived
    tombstone is the NORMAL end state of a carrier -- and its key is the record that
    the landing was carried, not an absence.
    """
    card = kb.create_task(
        conn,
        title=title or f"[land] {PR} for somebody",
        body=body,
        initial_status="running",
        idempotency_key=key,
    )
    assert kb.archive_task(conn, card) is True
    assert _status(conn, card) == "archived"
    return card


def test_an_archived_keyed_tombstone_admits_the_close(conn, forge):
    """ACCEPTANCE: the ONLY carrier is an archived, KEYED tombstone -> ADMITTED.

    The PR is still OPEN at the forge, so nothing but the carrier can admit this
    close: if the keyed lookup did not find the archived row the close is refused
    ``landing_carrier_missing``. No forge read is spent on the admitted path.
    """
    tombstone = _archived_carrier(conn, key=f"landing-carrier:{PR}")
    task_id = _review_card(conn)

    assert kb.complete_task(
        conn, task_id, summary="carried", metadata={"published_pr": PR_URL},
    ) is True
    assert _status(conn, task_id) == "done"
    assert forge["asked"] == []
    assert _blocked_events(conn, task_id) == []

    # And the carrier the clause found IS the tombstone, named by its key.
    described = klc._find_carrier(
        PR, conn=conn, task_id=task_id, landing_cards=[], stores=klc._board_stores(),
    )
    assert described == (
        f"the keyed carrier card {tombstone} on board default "
        f"(idempotency_key landing-carrier:{PR})"
    ), described


def test_an_archived_carrier_is_admitted_by_the_id_path_and_already_was_by_prose(conn, forge):
    """Both sides of the inconsistency this card removes, in one test.

    Arc 1 -- the DECLARED id path (``metadata.landing_card`` -> ``_card_by_id``): the
    base refused an archived card here, because that read carried
    ``status != 'archived'``. Arc 2 -- the PROSE path (``_candidate_cards``): the base
    carried no status filter at all, so the SAME archived card was already admitted.
    One question ("does a carrier exist for this ref") answered two ways; after the
    fix both admit, and arc 2 is the control that proves the filter was never a
    safety property.
    """
    declared = _archived_carrier(conn)  # titled "[land] ..." AND named below
    first = _review_card(conn)
    described = klc._find_carrier(
        PR, conn=conn, task_id=first, landing_cards=[declared], stores=klc._board_stores(),
    )
    assert described == f"the declared landing_card {declared}", described
    assert kb.complete_task(
        conn, first, summary="carried",
        metadata={"published_pr": PR_URL, "landing_card": declared},
    ) is True

    _archived_carrier(conn)  # the prose path alone: no declaration names it
    second = _review_card(conn)
    assert kb.complete_task(
        conn, second, summary="carried", metadata={"published_pr": PR_URL},
    ) is True

    assert forge["asked"] == []


def test_an_archived_linked_child_naming_the_pr_still_carries(conn, forge):
    """The third existence read (``_child_cards``) admits an archived child too."""
    task_id = _review_card(conn)
    child = kb.create_task(
        conn, title="carry the landing", body=f"lands {PR_URL}", initial_status="running",
    )
    kb.link_tasks(conn, task_id, child)
    assert kb.archive_task(conn, child) is True

    assert kb.complete_task(
        conn, task_id, summary="carried", metadata={"published_pr": PR_URL},
    ) is True
    assert forge["asked"] == []


def test_the_remedy_prints_the_keyed_create_and_says_archived_is_enough(conn, forge):
    """ACCEPTANCE: the one instruction the fleet copies carries the key verbatim."""
    task_id = _review_card(conn)

    refusal = kb.complete_task(
        conn, task_id, summary="approved", metadata={"published_pr": PR_URL},
    )

    assert isinstance(refusal, kb.CompletionRefusal)
    detail = refusal.detail
    # The paste-ready create, with the canonical key IN the call.
    assert f'kanban_create(title="[land] {PR} for {task_id}"' in detail, detail
    assert f'parents=["{task_id}"]' in detail, detail
    assert f'idempotency_key="landing-carrier:{PR}"' in detail, detail
    # The doctrine sentence now AGREES with the code: an archived keyed card counts.
    assert "ARCHIVED IS ENOUGH" in detail, detail
    assert "non-archived card on any board" not in detail, detail
    # ... and the legacy alias is named as the read-only fallback, so a lane holding
    # one of the older carriers knows it still counts.
    assert "landing-reconcile:<repo>#<N>" in detail, detail


def test_a_legacy_alias_naming_another_owner_does_not_admit_the_close(conn, forge):
    """ACCEPTANCE: ``landing-reconcile:hermes-agent#101`` is owner-less.

    The fork and upstream both have a PR #101, so the alias alone must never admit a
    close for ``NousResearch/hermes-agent#101`` when the card it found names the
    FORK's #101 instead. The forge is consulted -- and reports the PR open -- so the
    close is refused.
    """
    declared = "NousResearch/hermes-agent#101"
    kb.create_task(
        conn,
        title="Land #101: the fork's carrier",
        body="carries yoyodine-industries/hermes-agent#101",
        initial_status="running",
        idempotency_key="landing-reconcile:hermes-agent#101",
    )
    task_id = _review_card(conn)

    refusal = kb.complete_task(
        conn, task_id, summary="carried", metadata={"published_pr": _pr_url(declared)},
    )

    assert isinstance(refusal, kb.CompletionRefusal), refusal
    assert refusal.cause == "landing_carrier_missing"
    assert declared in refusal.detail
    assert forge["asked"] == [("NousResearch/hermes-agent", "101")]


def test_a_legacy_alias_that_also_names_the_full_ref_admits_the_close(conn, forge):
    """The alias IS accepted when the card it finds also names the full owner/repo ref.

    That is the whole read-only compatibility rule: no migration -- the flow's keyed
    rows keep working, and the ones that name the ref keep working.
    """
    declared = "NousResearch/hermes-agent#101"
    carrier = kb.create_task(
        conn,
        title="Land #101: upstream",
        body=f"carries {declared}",
        initial_status="running",
        idempotency_key="landing-reconcile:hermes-agent#101",
    )
    task_id = _review_card(conn)

    assert kb.complete_task(
        conn, task_id, summary="carried", metadata={"published_pr": declared},
    ) is True
    assert forge["asked"] == []
    described = klc._find_carrier(
        declared, conn=conn, task_id=task_id, landing_cards=[], stores=klc._board_stores(),
    )
    assert described == (
        f"the legacy-keyed carrier card {carrier} on board default "
        f"(idempotency_key landing-reconcile:hermes-agent#101, and the card names {declared})"
    ), described


def test_a_keyless_carrier_still_satisfies_the_fence(conn, forge):
    """ACCEPTANCE: 521 of 801 measured carriers are keyless -- none may start failing."""
    kb.create_task(conn, title=f"[land] {PR} for somebody", initial_status="running")
    task_id = _review_card(conn)

    assert kb.complete_task(
        conn, task_id, summary="carried", metadata={"published_pr": PR_URL},
    ) is True
    assert forge["asked"] == []


def test_the_carrier_key_builders_are_the_one_shape():
    """One builder each, no ad-hoc string formatting at call sites."""
    assert klc._carrier_key("example-org/example-repo#7") == "landing-carrier:example-org/example-repo#7"
    # The legacy alias is OWNER-LESS by construction -- which is why a hit is only
    # accepted when the card also names the full ref.
    assert klc._carrier_key_legacy("example-org/example-repo#7") == "landing-reconcile:example-repo#7"
