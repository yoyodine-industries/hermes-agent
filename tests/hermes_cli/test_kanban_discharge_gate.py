"""The completion-seam DISCHARGE gate: a claim that closes a card must be checkable.

Design record: platform-stl card ``t_0608ce6d``. Implementation card: ``t_9a2aaaea``.

The measured episode this gate answers: card ``t_241c8074`` closed ``done`` on "the unit this
card carries no longer exists" - while both flows it named were live in the deploy source - and
both premises the close rested on were false. Nothing read the argument, so nothing could.

The tests below pin the two gates' three-valued judgment:

* Gate E (refs): ``present`` admits, ``absent`` refuses, ``unknown`` NEVER refuses.
* Gate F (void claims): a void-claim phrase obliges a ``premise_void`` block whose measurements
  the KERNEL evaluates; all hold -> admit and record, any fails -> refuse naming what held.

Plus the kill switch (``off`` silent, ``measure`` records and admits) and the operator override
(``force`` waives and records) - because a gate nobody can turn off or audit is a gate that gets
deleted outright six weeks later.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb  # noqa: E402
from hermes_cli import kanban_db_connect as kbc  # noqa: E402
from hermes_cli import kanban_completion_refs as refs  # noqa: E402


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME: no prior boards, no ambient pins, no gate file."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_ATTACHMENTS_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
        refs.GATE_ENV,
    ):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants

        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    refs._MODE_CACHE["key"], refs._MODE_CACHE["mode"] = None, refs.GATE_DEFAULT
    return home


def _boards(*slugs: str) -> None:
    for slug in slugs:
        kb.create_board(slug)


def _evidence() -> dict:
    """A completion must still satisfy the EVIDENCE door that sits above this one."""
    return {"evidence": {"class": "none", "why": "unit test of the discharge gate seam"}}


def _complete(conn, card, *, summary="", result=None, metadata=None, force=False):
    meta = dict(_evidence())
    meta.update(metadata or {})
    return kb.complete_task(conn, card, summary=summary, result=result or summary,
                            metadata=meta, force=force)


def _events(conn, card, kind):
    return [e for e in kb.list_events(conn, card) if e.kind == kind]


# --- Gate E: the refs half ---------------------------------------------------------------


def test_sibling_board_reference_is_not_a_phantom_and_the_completion_proceeds(fresh_home):
    """Case 1: a card that exists on a SIBLING board is a citation, not a fabrication.

    The measured episode: a handoff named ``t_e62aade2``, which lives on the ``platform``
    board while the completion ran on another - it was flagged as suspect. Here the sibling id
    must both pass the gate and be absent from the advisory event stream.
    """
    _boards("alpha", "beta")
    with kbc.connect(board="beta") as beta:
        sibling = kb.create_task(beta, title="the real card", assignee="platform-worker")

    with kbc.connect(board="alpha") as conn:
        card = kb.create_task(conn, title="closing card", assignee="platform-coder")
        done = _complete(conn, card, result=f"fixes {sibling}, no phantom here")
        assert done is True, f"a cross-board citation refused a legitimate completion: {done!r}"
        assert kb.get_task(conn, card).status == "done"
        assert _events(conn, card, "suspected_hallucinated_references") == []
        assert _events(conn, card, "completion_blocked_phantom_refs") == []


def test_invented_reference_refuses_and_leaves_the_card_untouched(fresh_home):
    """Case 2: an id no board carries REFUSES, and the card is byte-untouched.

    This is the whole point: the defect was a card that closed on a fabricated record. A gate
    that admits it "for visibility" is not a gate.
    """
    _boards("alpha")
    with kbc.connect(board="alpha") as conn:
        card = kb.create_task(conn, title="closing card", assignee="platform-coder")
        before = kb.get_task(conn, card)
        with pytest.raises(refs.PhantomRefsRefused) as excinfo:
            _complete(conn, card, result="landed with t_deadbeefdeadbeef")
        after = kb.get_task(conn, card)
        events = _events(conn, card, "completion_blocked_phantom_refs")

    assert "t_deadbeefdeadbeef" in str(excinfo.value)
    assert after.status == before.status, "a refusal must not move the card"
    assert after.result == before.result, "a refusal must not write a result"
    assert len(events) == 1, f"expected one auditable refusal event, got {events!r}"
    assert events[0].payload["phantom_refs"] == ["t_deadbeefdeadbeef"]
    assert "alpha" in events[0].payload["boards_searched"]


def test_unreadable_sibling_store_is_unknown_and_never_refuses(fresh_home):
    """Case 4: a store that raises is a COVERAGE GAP, not a verdict.

    Measured 2026-10-24: a transient read-only open failure took four board stores out for four
    minutes. Had that been read as "absent", every completion citing a card on those boards
    would have been refused - an outage would have stopped the fleet. ``unknown`` admits, and
    the card is durably told its coverage was incomplete.
    """
    _boards("alpha", "gamma")
    # A store that is a file but not a database: connect() succeeds, the SELECT raises - the
    # same shape as a locked or corrupt sibling, without patching the kernel's own sqlite3.
    (fresh_home / "kanban" / "boards" / "gamma" / "kanban.db").write_bytes(b"not a database")
    with kbc.connect(board="alpha") as conn:
        card = kb.create_task(conn, title="closing card", assignee="platform-coder")
        done = _complete(conn, card, result="see t_feedfacefeedface for the follow-up")
        assert done is True, f"unknown coverage refused a completion: {done!r}"
        unverified = _events(conn, card, "phantom_refs_unverified")

    assert len(unverified) == 1, "an unreadable store must be visible, not silent"
    payload = unverified[0].payload
    assert payload["unverified"] == ["t_feedfacefeedface"]
    assert "gamma" in payload["unknown_boards"]


def test_off_mode_writes_nothing_and_admits(fresh_home, monkeypatch):
    """The kill switch: ``off`` is silent - no refusal, no event, no cost."""
    _boards("alpha")
    monkeypatch.setenv(refs.GATE_ENV, "off")
    with kbc.connect(board="alpha") as conn:
        card = kb.create_task(conn, title="closing card", assignee="platform-coder")
        assert _complete(conn, card, result="see t_deadbeefdeadbeef") is True
        assert _events(conn, card, "completion_blocked_phantom_refs") == []
        assert _events(conn, card, "suspected_hallucinated_references") == []


def test_measure_mode_records_and_admits(fresh_home, monkeypatch):
    """The soak: ``measure`` keeps the whole measurement and drops only the refusal."""
    _boards("alpha")
    monkeypatch.setenv(refs.GATE_ENV, "measure")
    with kbc.connect(board="alpha") as conn:
        card = kb.create_task(conn, title="closing card", assignee="platform-coder")
        assert _complete(conn, card, result="see t_deadbeefdeadbeef") is True
        measured = _events(conn, card, "discharge_gate_measure")

    assert len(measured) == 1
    assert measured[0].payload["gate"] == "phantom_refs"
    assert measured[0].payload["phantom_refs"] == ["t_deadbeefdeadbeef"]


def test_force_waives_and_records_the_waiver(fresh_home):
    """``force`` is the operator override, and it is on the record - never a silent pass."""
    _boards("alpha")
    with kbc.connect(board="alpha") as conn:
        card = kb.create_task(conn, title="closing card", assignee="platform-coder")
        assert _complete(conn, card, result="see t_deadbeefdeadbeef", force=True) is True
        waived = _events(conn, card, "phantom_refs_waived")
    assert len(waived) == 1
    assert waived[0].payload["absent"] == ["t_deadbeefdeadbeef"]


def test_unknown_gate_value_is_ignored_never_a_waiver(fresh_home, monkeypatch):
    """A typo in the switch must not disable the gate (the evidence gate's own rule)."""
    _boards("alpha")
    monkeypatch.setenv(refs.GATE_ENV, "of")  # not a mode
    with kbc.connect(board="alpha") as conn:
        card = kb.create_task(conn, title="closing card", assignee="platform-coder")
        with pytest.raises(refs.PhantomRefsRefused):
            _complete(conn, card, result="see t_deadbeefdeadbeef")


# --- Gate F: the void-claim half ----------------------------------------------------------


def _git_repo(tmp_path: Path, tracked: str) -> tuple[Path, str]:
    """A real git repo with ``tracked`` at HEAD; returns it and HEAD's short sha."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / tracked).parent.mkdir(parents=True, exist_ok=True)
    (repo / tracked).write_text("# live\n", encoding="utf-8")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    for args in (["init", "-q"], ["add", "."], ["commit", "-qm", "seed"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
                         check=True, capture_output=True, text=True).stdout.strip()
    return repo, sha


def test_void_claim_without_a_measurement_refuses(fresh_home):
    """Case 9: the phrase alone obliges a measurement. This is the defect, pinned."""
    _boards("alpha")
    with kbc.connect(board="alpha") as conn:
        card = kb.create_task(conn, title="closing card", assignee="platform-coder")
        with pytest.raises(refs.VoidClaimUnmeasured) as excinfo:
            _complete(conn, card, result="the unit this card carries no longer exists")
        assert kb.get_task(conn, card).status != "done"
        refused = _events(conn, card, "completion_blocked_void_claim")

    assert "premise_void" in str(excinfo.value)
    assert len(refused) == 1
    assert refused[0].payload["cause"] == "premise_void_missing"


def test_false_premise_refuses_and_reports_what_was_observed(fresh_home, tmp_path):
    """Case 5: a measured claim that does NOT hold refuses, naming target and observation.

    The ``t_241c8074`` shape exactly: the completion said the unit no longer existed, and the
    file was tracked at HEAD the whole time.
    """
    _boards("alpha")
    repo, sha = _git_repo(tmp_path, "hermes_cli/kanban_db_phantom.py")
    with kbc.connect(board="alpha") as conn:
        card = kb.create_task(conn, title="closing card", assignee="platform-coder")
        with pytest.raises(refs.VoidClaimUnmeasured) as excinfo:
            _complete(
                conn, card,
                result="premise is void: the unit this card carries no longer exists",
                metadata={"premise_void": {
                    "claim": "hermes_cli/kanban_db_phantom.py is gone",
                    "measurements": [{"kind": "git_absent", "repo": str(repo),
                                      "path": "hermes_cli/kanban_db_phantom.py"}],
                }},
            )
        assert kb.get_task(conn, card).status != "done"
        refused = _events(conn, card, "completion_blocked_void_claim")

    message = str(excinfo.value)
    assert f"present at HEAD {sha}" in message, message
    observed = refused[0].payload["observed"][0]
    assert observed["kind"] == "git_absent"
    assert observed["observed"] == f"present at HEAD {sha}"
    assert observed["holds"] is False


def test_true_premise_admits_and_records_the_measurement(fresh_home, tmp_path):
    """Case 6: a measurement that HOLDS admits the close and records what was measured."""
    _boards("alpha")
    repo, _sha = _git_repo(tmp_path, "hermes_cli/keep.py")
    gone = tmp_path / "build" / "legacy_flow.py"
    with kbc.connect(board="alpha") as conn:
        card = kb.create_task(conn, title="closing card", assignee="platform-coder")
        done = _complete(
            conn, card,
            result="the legacy flow no longer exists; nothing to repair",
            metadata={"premise_void": {
                "claim": "the legacy flow was deleted in the cutover",
                "measurements": [
                    {"kind": "path_absent", "target": str(gone)},
                    {"kind": "git_absent", "repo": str(repo), "path": "build/legacy_flow.py"},
                    {"kind": "glob_absent", "root": str(repo), "pattern": "**/legacy_flow*"},
                ],
            }},
        )
        assert done is True, f"a measured-true void claim was refused: {done!r}"
        measured = _events(conn, card, "void_claim_measured")

    assert len(measured) == 1
    payload = measured[0].payload
    assert payload["kinds"] == ["path_absent", "git_absent", "glob_absent"]
    assert all(row["holds"] for row in payload["observed"])


def test_unrecognised_measurement_kind_refuses(fresh_home):
    """Case 7: the vocabulary is closed - a new kind is a waiver by inventing a word."""
    _boards("alpha")
    with kbc.connect(board="alpha") as conn:
        card = kb.create_task(conn, title="closing card", assignee="platform-coder")
        with pytest.raises(refs.VoidClaimRefused) as excinfo:
            _complete(
                conn, card, result="the premise is void, nothing to do",
                metadata={"premise_void": {
                    "claim": "gone",
                    "measurements": [{"kind": "vibes_absent", "target": "/tmp"}],
                }},
            )
    assert "vibes_absent" in str(excinfo.value)


def test_incidental_phrase_disclaimed_or_refused(fresh_home):
    """Case 8: the honest opt-out needs a real ``why``; a stub is refused.

    On a 30-day corpus 6.7% of completions contain one of these phrases, so the vocabulary
    WILL fire on prose that is not a claim. The opt-out exists for exactly that, and the
    ``why`` floor is what keeps it honest.
    """
    _boards("alpha")
    with kbc.connect(board="alpha") as conn:
        stub = kb.create_task(conn, title="stub why", assignee="platform-coder")
        with pytest.raises(refs.VoidClaimRefused):
            _complete(
                conn, stub, result="the premise is void",
                metadata={"premise_void": {"basis": "not_the_basis", "why": "n/a"}},
            )
        assert kb.get_task(conn, stub).status != "done"

        card = kb.create_task(conn, title="real disclaimer", assignee="platform-coder")
        done = _complete(
            conn, card,
            result="fixed the renderer; the old ids no longer exist in the fixture",
            metadata={"premise_void": {
                "basis": "not_the_basis",
                "why": "the phrase is about fixture ids the card does not close on",
            }},
        )
        assert done is True, f"a well-argued disclaimer was refused: {done!r}"
        disclaimed = _events(conn, card, "void_claim_disclaimed")

    assert len(disclaimed) == 1
    assert "fixture ids" in disclaimed[0].payload["why"]


def test_unknown_block_key_is_refused(fresh_home):
    """A typo must never silently waive: unknown keys in the block are refused."""
    _boards("alpha")
    with kbc.connect(board="alpha") as conn:
        card = kb.create_task(conn, title="closing card", assignee="platform-coder")
        with pytest.raises(refs.VoidClaimRefused) as excinfo:
            _complete(
                conn, card, result="nothing to do here",
                metadata={"premise_void": {"measurments": [{"kind": "path_absent",
                                                            "target": "/tmp/x"}]}},
            )
    assert "measurments" in str(excinfo.value)


@pytest.mark.skipif(
    not Path("/opt/hermes_sandbox/platform/repos/yaan-platform").is_dir(),
    reason="prod source-of-truth mount is not present on this host",
)
def test_measured_against_the_prod_source_of_truth(fresh_home):
    """The card's own STEP 5: measure against the REAL deploy source, not just a temp repo.

    Guards the ``t_241c8074`` episode: a file that is tracked in the deploy source must be
    observed as ``present at HEAD <sha>``, so the claim "it no longer exists" cannot close.
    """
    repo = "/opt/hermes_sandbox/platform/repos/yaan-platform"
    probe = subprocess.run(["git", "-C", repo, "ls-tree", "-r", "--name-only", "HEAD"],
                           capture_output=True, text=True, check=True).stdout.splitlines()
    tracked = next((line for line in probe if line.endswith(".py")), None)
    if not tracked:
        pytest.skip("the prod source of truth has no tracked python file to probe")
    sha = subprocess.run(["git", "-C", repo, "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    holds, observed = refs._observed_git_absent(repo, tracked)
    assert holds is False, f"{tracked} is tracked at HEAD but the measurement said otherwise"
    assert observed == f"present at HEAD {sha}"


# --- visibility: the two families ---------------------------------------------------------


def test_event_families_counts_every_outcome(fresh_home, monkeypatch):
    """``kanban gates report`` needs countable outcomes, or the gate cannot be audited."""
    _boards("alpha")
    with kbc.connect(board="alpha") as conn:
        refused = kb.create_task(conn, title="refused", assignee="platform-coder")
        with pytest.raises(refs.PhantomRefsRefused):
            _complete(conn, refused, result="see t_deadbeefdeadbeef")

        measured = kb.create_task(conn, title="measured", assignee="platform-coder")
        assert _complete(conn, measured, result="fixed it, nothing to repair",
                         metadata={"premise_void": {
                             "claim": "gone", "measurements": [
                                 {"kind": "path_absent", "target": "/nonexistent/x"}]}}) is True

        monkeypatch.setenv(refs.GATE_ENV, "measure")
        soaked = kb.create_task(conn, title="soaked", assignee="platform-coder")
        assert _complete(conn, soaked, result="see t_feedfacefeedface") is True

        families = refs.event_families(conn)

    assert families["mode"] == "measure"
    assert families["families"]["phantom_refs"]["refused"] == 1
    assert families["families"]["phantom_refs"]["measure_mode"] == 1
    assert families["families"]["void_claims"]["measured"] == 1
    assert families["total"] >= 3


def test_gates_report_carries_the_two_families(fresh_home, monkeypatch, capsys):
    """The visibility door: ``hermes kanban gates report --json`` carries both families.

    They are TELEMETRY and never part of ``open_total`` - a refusal a worker then answered is a
    gate working, not a standing violation, so it must neither pin the exit code red nor be
    invisible to the operator who wants to know how loud the gate is.
    """
    import argparse
    import json

    from hermes_cli import kanban as kanban_cli

    _boards("alpha")
    with kbc.connect(board="alpha") as conn:
        card = kb.create_task(conn, title="closing card", assignee="platform-coder")
        with pytest.raises(refs.PhantomRefsRefused):
            _complete(conn, card, result="see t_deadbeefdeadbeef")

    monkeypatch.setenv("HERMES_KANBAN_BOARD", "alpha")
    rc = kanban_cli._cmd_gates(argparse.Namespace(action="report", board="alpha", json=True))
    out = json.loads(capsys.readouterr().out)

    assert rc == 0, f"telemetry must not move the exit code, got {rc}"
    assert out["board"] == "alpha"
    assert out["armed"]["discharge_gate"] == "refuse"
    discharge = out["discharge"]
    assert discharge["families"]["phantom_refs"]["refused"] == 1
    assert discharge["families"]["void_claims"]["refused"] == 0
    assert out["open_total"] == 0


def test_a_schemaless_store_answers_not_here_and_a_phantom_still_refuses(fresh_home):
    """An openable store with no ``tasks`` table holds no cards - it is not an outage.

    Measured live 2026-10-25 on the card this gate answers: ``boards/_archived/kanban.db`` on this
    host is an initialised-but-empty husk with no ``tasks`` table. Counting such a store as
    ``unknown`` made EVERY phantom ref unverified - which is not a refusal - so Gate E's refusal
    arm was advisory-only in production while its unit tests stayed green (they only ever had
    stores the resolver could read). A store that holds no cards ANSWERS "not here": the ids stay
    ABSENT, and the completion is refused.
    """
    _boards("alpha")
    husk = kb.boards_root() / "husk" / "kanban.db"
    husk.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(str(husk)).close()      # openable, zero tables - the live husk's exact shape
    with kbc.connect(board="alpha") as conn:
        card = kb.create_task(conn, title="husk on the host", assignee="platform-coder")
        with pytest.raises(refs.PhantomRefsRefused) as excinfo:
            _complete(conn, card, result="landed with t_deadbeefdeadbeef")
        task = kb.get_task(conn, card)
        assert task is not None and task.status != "done"
        refused = _events(conn, card, "completion_blocked_phantom_refs")

    assert len(refused) == 1
    payload = refused[0].payload
    assert payload["phantom_refs"] == ["t_deadbeefdeadbeef"]
    assert payload["unknown_boards"] == [], "a schemaless store answered; nothing was unknown"
    assert "husk" in payload["boards_searched"], "the husk is still named as searched"
    assert "husk" in str(excinfo.value), "the refusal names the boards it searched"


def test_a_store_that_cannot_be_read_still_never_refuses(fresh_home, monkeypatch):
    """The outage rule the fix must not have eaten: a READ FAILURE is unknown, not absent.

    Sibling of the schemaless case above, one line apart in the resolver and opposite in verdict.
    """
    _boards("alpha")
    broken = kb.boards_root() / "broken" / "kanban.db"
    broken.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(str(broken)).close()

    real_connect = sqlite3.connect

    def _explode(*args, **kwargs):
        if "broken" in str(args[0] if args else "") or "broken" in str(kwargs.get("database", "")):
            raise sqlite3.OperationalError("database is locked")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(refs.sqlite3, "connect", _explode)
    with kbc.connect(board="alpha") as conn:
        card = kb.create_task(conn, title="outage on the host", assignee="platform-coder")
        assert _complete(conn, card, result="landed with t_deadbeefdeadbeef") is True
        unverified = _events(conn, card, "phantom_refs_unverified")

    assert unverified, "an unreadable store must be recorded, not silently dropped"
    assert len(unverified) == 1
    assert "broken" in unverified[0].payload["unknown_boards"]


