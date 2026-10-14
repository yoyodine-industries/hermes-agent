"""Deploy-proof gate: a ``landed`` card may not close on evidence from before its landing.

Covers the clauses of ``hermes_cli/kanban_proof_gate.py`` and the kernel firing path
(``complete_task`` -> ``_gate_deploy_proof`` -> ``ProofGateError``), including the two
shapes the gate exists for: a run dated BEFORE the landing (refused), and a run dated
after it (admitted). The negative control is the predating run — the exact defect that
closed a card done while nothing was running the landed bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_pr_acceptance as acceptance
from hermes_cli import kanban_proof_gate as gate


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kbc.connect() as c:
        yield c


def _artifact(tmp_path: Path, name: str = "artifact.py", body: str = "landed bytes\n"):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _override_pin(tmp_path: Path, path: Path, digest: str, rel: str = "artifact.py"):
    """An R3 override-manifest record backed by a TMP MANIFEST + byte copy.

    NEVER reads the live ~/.hermes/yoyoflow/untracked-overrides: the caller passes the
    returned root to gate.evaluate(overrides_root=...). Returns (record, root).
    """
    root = tmp_path / "untracked-overrides"
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / "MANIFEST.md"
    if not manifest.exists():
        manifest.write_text(
            "# untracked overrides\n\n| repo-relative path | sha256 | why |\n|---|---|---|\n",
            encoding="utf-8")
    with manifest.open("a", encoding="utf-8") as handle:
        handle.write(f"| `{rel}` | `{digest}` | test fixture |\n")
    copy = root / rel
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_bytes(path.read_bytes())
    return {"kind": "override-manifest", "id": rel}, root


def _ledger(tmp_path: Path, *, at: str, admission: str = "admitted", exit_code: int = 0,
            landed_sha: str = "abc123def4567890", card: str = "t_witness1"):
    path = tmp_path / "deploy-ledger.jsonl"
    path.write_text(json.dumps({
        "kind": "landing", "admission": admission, "exit": exit_code, "at": at,
        "landed_sha": landed_sha, "card": card, "pr": 210, "repo": "/opt/hermes_prod/unit",
    }) + "\n", encoding="utf-8")
    return path


def _run_store(tmp_path: Path, *, run_id: str = "20260926-1835", status: str = "completed",
               finished_at: str = "2026-09-26T23:00:00+00:00", workflow: str = "review-currency",
               verdict: str | None = None):
    path = tmp_path / "yoyoflow-state.db"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE runs (id TEXT PRIMARY KEY, workflow_name TEXT NOT NULL, status TEXT NOT NULL,"
        " started_at TEXT NOT NULL, finished_at TEXT, owner_host TEXT, owner_pid INTEGER,"
        " gate_fail_count INTEGER, verdict TEXT, released_by TEXT)")
    db.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, 'yoyodine', 1, NULL, ?, NULL)",
               (run_id, workflow, status, "2026-09-26T22:50:00+00:00", finished_at, verdict))
    db.commit()
    db.close()
    return path


def _task(conn, *, contract: str | None = None, assignee: str = "coder", title: str = "demo"):
    tid = kb.create_task(conn, title=title, assignee=assignee, completion_contract=contract)
    assert kb.claim_task(conn, tid, claimer=kb._claimer_id()) is not None
    return tid


def test_landed_contract_is_authorable_and_not_a_repository_check():
    assert acceptance.validate_contract("landed") == "landed"
    assert acceptance.needs_repository_checks("landed") is False
    assert acceptance.needs_repository_checks("local-only") is False
    assert acceptance.needs_repository_checks("yoyodine-industries/hermes-agent") is True


def test_no_proof_is_refused_by_name():
    verdict = gate.evaluate(None)
    assert verdict.ok is False and verdict.cause == "no_proof"
    assert "landed" in verdict.detail


def test_predating_run_is_refused(tmp_path):
    path, digest = _artifact(tmp_path)
    ledger = _ledger(tmp_path, at="2026-09-26T23:30:00-04:00")
    store = _run_store(tmp_path, finished_at="2026-09-26T23:00:00+00:00")
    verdict = gate.evaluate(
        {"artifacts": [{"path": str(path), "sha256": digest}],
         "landing": {"ref": "abc123def4567890"},
         "run": {"store": "yoyoflow", "id": "20260926-1835"}},
        ledger_path=str(ledger), run_store_path=str(store))
    assert verdict.ok is False and verdict.cause == "proof_predates_landing"
    assert verdict.facts["run_id"] == "20260926-1835"
    assert verdict.facts["landed_at"].startswith("2026-09-27")


def test_post_landing_green_run_is_admitted(tmp_path):
    path, digest = _artifact(tmp_path)
    ledger = _ledger(tmp_path, at="2026-09-26T18:47:50-04:00")  # 22:47:50Z, before the run
    store = _run_store(tmp_path, finished_at="2026-09-26T23:00:00+00:00")
    verdict = gate.evaluate(
        {"artifacts": [{"path": str(path), "sha256": digest}],
         "landing": {"ref": "abc123def4567890"},
         "run": {"store": "yoyoflow", "id": "20260926-1835"}},
        ledger_path=str(ledger), run_store_path=str(store))
    assert verdict.ok is True, verdict.detail
    assert verdict.facts["run_status"] == "completed"
    assert verdict.facts["landed_at"] == "2026-09-26T22:47:50Z"


def test_artifact_mtime_is_the_landing_when_no_ledger_row_is_named(tmp_path):
    path, digest = _artifact(tmp_path)
    store = _run_store(tmp_path, finished_at="2026-09-26T23:00:00+00:00")
    import os
    os.utime(path, (1_790_000_000, 1_790_000_000))  # landing long before the run
    record, overrides = _override_pin(tmp_path, path, digest)
    verdict = gate.evaluate(
        {"artifacts": [{"path": str(path), "sha256": digest, "record": record}],
         "run": {"store": "yoyoflow", "id": "20260926-1835"}},
        ledger_path=str(tmp_path / "absent.jsonl"), run_store_path=str(store),
        overrides_root=str(overrides))
    assert verdict.ok is True, verdict.detail


def test_declared_hash_must_be_the_bytes_on_disk(tmp_path):
    """The identity clause refuses — and only ever as the LAST clause named.

    A payload whose only fault is the stale pin is still refused on the pin (the
    handoff must be about the bytes that are live). A payload with a stale pin AND no
    evidence at all is refused for the missing run, with the pin listed beside it: the
    clause whose remedy is a re-pin never pre-empts the clause the DoD is about.
    """
    path, digest = _artifact(tmp_path)
    store = _run_store(tmp_path, finished_at="2026-09-26T23:00:00+00:00")
    ledger = _ledger(tmp_path, at="2026-09-26T18:47:50-04:00")  # 22:47:50Z, before the run

    fresh = gate.evaluate(
        {"artifacts": [{"path": str(path), "sha256": digest}],
         "landing": {"ref": "abc123def4567890"},
         "run": {"store": "yoyoflow", "id": "20260926-1835"}},
        ledger_path=str(ledger), run_store_path=str(store))
    assert fresh.ok is True, fresh.detail

    stale = gate.evaluate(
        {"artifacts": [{"path": str(path), "sha256": "0" * 64}],
         "landing": {"ref": "abc123def4567890"},
         "run": {"store": "yoyoflow", "id": "20260926-1835"}},
        ledger_path=str(ledger), run_store_path=str(store))
    assert stale.cause == "artifact_hash_mismatch"
    assert [f["clause"] for f in stale.failures] == ["artifact_hash_mismatch"]

    bare = gate.evaluate({"artifacts": [{"path": str(path), "sha256": "0" * 64}]},
                         ledger_path=str(ledger))
    assert bare.cause == "no_run_or_probe"
    assert {f["clause"] for f in bare.failures} == {
        "no_run_or_probe", "artifact_hash_mismatch", "artifact_unpinned"}


def test_missing_artifact_and_missing_run_are_named(tmp_path):
    missing = gate.evaluate({"artifacts": [{"path": str(tmp_path / "nope.py")}]})
    assert missing.cause == "artifact_missing"
    path, digest = _artifact(tmp_path)
    store = _run_store(tmp_path)
    unknown = gate.evaluate(
        {"artifacts": [{"path": str(path), "sha256": digest}],
         "run": {"store": "yoyoflow", "id": "20260101-0000"}},
        run_store_path=str(store))
    assert unknown.cause == "run_unknown"


def test_red_run_and_unadmitted_landing_are_named(tmp_path):
    path, digest = _artifact(tmp_path)
    store = _run_store(tmp_path, status="failed", finished_at="2026-09-26T23:00:00+00:00")
    import os
    os.utime(path, (1_790_000_000, 1_790_000_000))
    red = gate.evaluate(
        {"artifacts": [{"path": str(path), "sha256": digest}],
         "run": {"store": "yoyoflow", "id": "20260926-1835"}},
        ledger_path=str(tmp_path / "absent.jsonl"), run_store_path=str(store))
    assert red.cause == "run_not_green"
    ledger = _ledger(tmp_path, at="2026-09-26T22:47:50-04:00", admission="refused", exit_code=1)
    unadmitted = gate.evaluate(
        {"artifacts": [{"path": str(path), "sha256": digest}],
         "landing": {"ref": "abc123def4567890"},
         "run": {"store": "yoyoflow", "id": "20260926-1835"}},
        ledger_path=str(ledger), run_store_path=str(store))
    assert unadmitted.cause == "landing_not_admitted"


def test_kernel_refuses_predating_proof_then_admits_post_landing_run(conn, tmp_path, monkeypatch):
    path, digest = _artifact(tmp_path)
    ledger = _ledger(tmp_path, at="2026-09-26T23:30:00-04:00")
    store = _run_store(tmp_path, finished_at="2026-09-26T23:00:00+00:00")
    monkeypatch.setenv("HERMES_PROOF_LEDGER", str(ledger))
    monkeypatch.setenv("HERMES_PROOF_RUN_STORE", str(store))
    tid = _task(conn, contract="landed")
    proof = {"artifacts": [{"path": str(path), "sha256": digest}],
             "landing": {"ref": "abc123def4567890"},
             "run": {"store": "yoyoflow", "id": "20260926-1835"}}

    with pytest.raises(kb.ProofGateError) as caught:
        kb.complete_task(conn, tid, summary="landed and proven", metadata={"proof": proof})
    assert caught.value.clause == "proof_predates_landing"
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert row["status"] == "running"  # nothing mutated
    kinds = [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,)).fetchall()]
    assert "completion_blocked_proof_gate" in kinds and "completed" not in kinds

    # Move the landing before the run: the same card now proves itself and closes.
    ledger.write_text(json.dumps({
        "kind": "landing", "admission": "admitted", "exit": 0,
        "at": "2026-09-26T18:47:50-04:00", "landed_sha": "abc123def4567890",
        "card": "t_witness1", "pr": 210, "repo": "/opt/hermes_prod/unit",
    }) + "\n", encoding="utf-8")
    assert kb.complete_task(conn, tid, summary="landed and proven", metadata={"proof": proof})
    kinds = [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,)).fetchall()]
    assert "proof_gate_admitted" in kinds and "completed" in kinds


def test_card_store_run_record_proves_a_kernel_turn(conn, tmp_path, monkeypatch):
    """The kernel's own production run IS a card turn: a sibling run dated after the landing."""
    path, digest = _artifact(tmp_path)
    monkeypatch.setenv("HERMES_PROOF_LEDGER", str(tmp_path / "absent.jsonl"))
    monkeypatch.setenv("HERMES_PROOF_RUN_STORE", str(tmp_path / "absent.db"))
    witness = _task(conn, title="witness")
    run = conn.execute("SELECT id, started_at FROM task_runs WHERE task_id = ?",
                       (witness,)).fetchone()
    conn.execute("UPDATE task_runs SET ended_at = ?, outcome = 'completed' WHERE id = ?",
                 (run["started_at"] + 60, run["id"]))
    conn.commit()
    import os
    os.utime(path, (run["started_at"] - 600, run["started_at"] - 600))
    override_record, overrides = _override_pin(tmp_path, path, digest)
    monkeypatch.setenv("HERMES_PROOF_OVERRIDES", str(overrides))
    owner = _task(conn, contract="landed", title="mechanism")
    proof = {"artifacts": [{"path": str(path), "sha256": digest,
                            "record": override_record}],
             "run": {"store": "card", "task": witness, "id": run["id"]}}
    assert kb.complete_task(conn, owner, summary="proved by the next real turn",
                            metadata={"proof": proof})
    facts = json.loads(conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'proof_gate_admitted'",
        (owner,)).fetchone()["payload"])
    assert facts["facts"]["run_store"] == "card"


def test_local_only_and_review_cards_are_untouched_by_the_gate(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_PROOF_LEDGER", str(tmp_path / "absent.jsonl"))
    monkeypatch.setenv("HERMES_PROOF_RUN_STORE", str(tmp_path / "absent.db"))
    tid = _task(conn, contract="local-only")
    # A handoff with no proof at all still closes: the fence is the contract, not the board.
    assert kb.complete_task(conn, tid, summary="purely local work")


def test_no_lane_is_exempt_including_the_ops_head(conn, tmp_path, monkeypatch):
    """The fence is contract-keyed: an ops-head card is refused exactly like any other."""
    monkeypatch.setenv("HERMES_PROOF_LEDGER", str(tmp_path / "absent.jsonl"))
    monkeypatch.setenv("HERMES_PROOF_RUN_STORE", str(tmp_path / "absent.db"))
    for assignee in ("coder", "default", "platform-stl"):
        tid = _task(conn, contract="landed", assignee=assignee, title=f"lane-{assignee}")
        with pytest.raises(kb.ProofGateError) as caught:
            kb.complete_task(conn, tid, summary="done", metadata={"proof": {}})
        assert caught.value.clause == "malformed_proof"


def test_probe_clause_dates_a_service_after_its_deploy(tmp_path):
    path, digest = _artifact(tmp_path, body="service v2\n")
    import os
    os.utime(path, (1790553600, 1790553600))  # deployed 2026-09-28T00:00:00Z
    record, overrides = _override_pin(tmp_path, path, digest)
    entry = {"path": str(path), "sha256": digest, "record": record}
    stale = gate.evaluate({"artifacts": [entry],
                           "probe": {"at": "2026-09-26T22:00:00+00:00", "result": "ok"}},
                          overrides_root=str(overrides))
    assert stale.cause == "proof_predates_landing"
    fresh = gate.evaluate({"artifacts": [entry],
                           "probe": {"at": "2026-09-29T02:00:00+00:00", "result": "ok"}},
                          overrides_root=str(overrides))
    assert fresh.ok is True, fresh.detail


# ---------------------------------------------------------------------------
# Defect regressions: clause masking, and a landing that re-dates itself from
# later deploy rows. The two filed defects, each pinned by its own case.
# ---------------------------------------------------------------------------

REF = "abc123def4567890"
LANDING_AT = "2026-09-26T18:47:50-04:00"          # 22:47:50Z
RUN_AFTER = "2026-09-26T23:00:00+00:00"
ARRIVAL_AT = "2026-09-26T18:48:56+00:00"


def _dest_artifact(tmp_path: Path, rel: str = "cron/deploy-train.sh",
                   body: str = "landed bytes\n"):
    """An artifact placed under a dest root, the way a car deploys it."""
    dest = tmp_path / "prod" / "unit"
    path = dest / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return dest, path, hashlib.sha256(path.read_bytes()).hexdigest()


def _ledger_with(tmp_path: Path, rows: list, name: str = "ledger.jsonl"):
    path = tmp_path / name
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    return path


def _landing_row(at: str, sha: str = REF, admission: str = "admitted", exit_code: int = 0,
                 row_recorded: bool | None = None):
    row = {"kind": "landing", "admission": admission, "exit": exit_code, "at": at,
           "landed_sha": sha, "card": "t_witness1", "pr": 210,
           "repo": "/opt/hermes_sandbox/unit"}
    if row_recorded is not None:
        row["row_recorded"] = row_recorded
    return row


def _deploy_row(at: str, sha: str, dest: str, *, stamped: bool = True,
                allowed: bool = True):
    """A gate/deploy row: no ``kind``, the shape ``deploy.sh`` appends per dest."""
    return {"repo": "/opt/hermes_sandbox/unit", "sha": sha, "tree": "8" * 40,
            "dest": dest, "at": at, "stamped": stamped, "allowed": allowed}


def _write_stamp(dest: Path, commit: str, deployed: str = "2026-09-26T23:21:47-0400"):
    (dest / ".deployed-from").write_text(
        "repo: /opt/hermes_sandbox/unit\nbranch: main\n"
        f"commit: {commit}\ntree: {'8' * 40}\ndeployed: {deployed}\n"
        "owner: platform-stl\nverified: copied tree matches git archive HEAD\n",
        encoding="utf-8")


def _run_db(tmp_path: Path, sub: str, **kwargs):
    """A run store in its own directory — the existing helper creates one per path."""
    directory = tmp_path / sub
    directory.mkdir(parents=True, exist_ok=True)
    return _run_store(directory, **kwargs)


def _proof(path: Path, digest: str, *, ref: str = REF, run_id: str = "20260926-1835",
           extra: dict | None = None):
    proof = {"artifacts": [{"path": str(path), "sha256": digest}],
             "landing": {"ref": ref},
             "run": {"store": "yoyoflow", "id": run_id}}
    proof.update(extra or {})
    return proof


def test_a_stale_pin_cannot_mask_the_timing_clause(tmp_path):
    """DEFECT 1: the identity clause must not pre-empt the clause the DoD is about."""
    path, _ = _artifact(tmp_path)
    ledger = _ledger_with(tmp_path, [_landing_row("2026-09-26T23:30:00-04:00")])  # 03:30Z
    store = _run_store(tmp_path, finished_at=RUN_AFTER)
    verdict = gate.evaluate(
        {"artifacts": [{"path": str(path), "sha256": "0" * 64}],  # the stale pin
         "landing": {"ref": REF},
         "run": {"store": "yoyoflow", "id": "20260926-1835"}},
        ledger_path=str(ledger), run_store_path=str(store))
    assert verdict.cause == "proof_predates_landing"
    assert {f["clause"] for f in verdict.failures} == {
        "proof_predates_landing", "artifact_hash_mismatch"}
    text = gate.render("t_demo", verdict)
    assert "artifact_hash_mismatch" in text and "proof_predates_landing" in text
    assert "Re-pinning the artifact hash cannot clear a timing failure" in text


def test_an_identical_redeploy_at_the_same_dest_does_not_redate_the_landing(tmp_path):
    """DEFECT 2: a re-deploy of the SAME bytes is not a new landing."""
    dest, path, digest = _dest_artifact(tmp_path)
    ledger = _ledger_with(tmp_path, [
        _landing_row(LANDING_AT),
        _deploy_row(ARRIVAL_AT, REF, str(dest)),
        _deploy_row("2026-09-27T00:05:12+00:00", REF, str(dest)),  # same bytes again
    ])
    store = _run_store(tmp_path, finished_at=RUN_AFTER)
    verdict = gate.evaluate(_proof(path, digest), ledger_path=str(ledger),
                            run_store_path=str(store))
    assert verdict.ok is True, verdict.detail
    assert verdict.facts["arrival_at"] == "2026-09-26T18:48:56Z"  # not the 00:05Z re-deploy


def test_bytes_that_changed_and_came_back_move_the_arrival(tmp_path):
    """The arrival leg is content-aware: different bytes in between DO re-date it."""
    dest, path, digest = _dest_artifact(tmp_path)
    ledger = _ledger_with(tmp_path, [
        _landing_row(LANDING_AT),
        _deploy_row(ARRIVAL_AT, REF, str(dest)),
        _deploy_row("2026-09-26T23:00:00+00:00", "f" * 16, str(dest)),  # another commit
        _deploy_row("2026-09-27T00:05:12+00:00", REF, str(dest)),       # and back
    ])
    store = _run_store(tmp_path, finished_at="2026-09-26T23:30:00+00:00")
    verdict = gate.evaluate(_proof(path, digest), ledger_path=str(ledger),
                            run_store_path=str(store))
    assert verdict.cause == "proof_predates_landing"
    assert verdict.facts["arrival_at"] == "2026-09-27T00:05:12Z"


def test_a_deploy_of_the_same_sha_to_another_dest_cannot_date_the_artifact(tmp_path):
    """The measured 99021a52 shape: a row for another dest is not this artifact's."""
    dest, path, digest = _dest_artifact(tmp_path)
    foreign = _deploy_row("2026-09-27T03:21:56+00:00", REF, str(tmp_path / "prod" / "other"))
    store = _run_store(tmp_path, finished_at=RUN_AFTER)
    with_landing = _ledger_with(tmp_path, [
        _landing_row(LANDING_AT), _deploy_row(ARRIVAL_AT, REF, str(dest)), foreign,
    ], name="with_landing.jsonl")
    verdict = gate.evaluate(_proof(path, digest), ledger_path=str(with_landing),
                            run_store_path=str(store))
    assert verdict.ok is True, verdict.detail
    assert verdict.facts["arrival_at"] == "2026-09-26T18:48:56Z"
    assert verdict.facts["landing_row_at"] == "2026-09-26T22:47:50Z"

    # A foreign-dest row cannot stand in for a landing either.
    foreign_only = _ledger_with(tmp_path, [foreign], name="foreign_only.jsonl")
    unresolved = gate.evaluate(_proof(path, digest), ledger_path=str(foreign_only),
                               run_store_path=str(store))
    assert unresolved.cause == "landing_unresolved"


def test_a_refused_deploy_row_cannot_date_the_artifact(tmp_path):
    """A row the gate refused deployed nothing; it cannot date an arrival."""
    dest, path, digest = _dest_artifact(tmp_path)
    ledger = _ledger_with(tmp_path, [
        _deploy_row("2026-09-27T00:25:30+00:00", REF, str(dest), allowed=False),
    ])
    store = _run_store(tmp_path, finished_at=RUN_AFTER)
    verdict = gate.evaluate(_proof(path, digest), ledger_path=str(ledger),
                            run_store_path=str(store))
    assert verdict.cause == "landing_unresolved"


def test_the_run_must_postdate_the_arrival_not_only_the_merge(tmp_path):
    """The merge is not the whole bound: the run must have had these bytes to run."""
    dest, path, digest = _dest_artifact(tmp_path)
    ledger = _ledger_with(tmp_path, [
        _landing_row("2026-09-26T06:00:00-04:00"),          # 10:00Z on trunk
        _deploy_row(ARRIVAL_AT, REF, str(dest)),            # 18:48Z at the dest
    ])
    early = _run_db(tmp_path, "early", finished_at="2026-09-26T11:00:00+00:00", run_id="early")
    predates = gate.evaluate(_proof(path, digest, run_id="early"),
                             ledger_path=str(ledger), run_store_path=str(early))
    assert predates.cause == "proof_predates_landing"

    late = _run_db(tmp_path, "late", finished_at="2026-09-26T19:00:00+00:00", run_id="late")
    admitted = gate.evaluate(_proof(path, digest, run_id="late"),
                             ledger_path=str(ledger), run_store_path=str(late))
    assert admitted.ok is True, admitted.detail


def test_an_admitted_landing_beats_a_newer_unadmitted_one(tmp_path):
    path, digest = _artifact(tmp_path)
    ledger = _ledger_with(tmp_path, [
        _landing_row("2026-09-26T18:00:00-04:00"),                                   # 22:00Z
        _landing_row("2026-09-26T19:30:00-04:00", admission="refused", exit_code=1),  # 23:30Z
    ])
    store = _run_store(tmp_path, finished_at=RUN_AFTER)
    verdict = gate.evaluate(_proof(path, digest), ledger_path=str(ledger),
                            run_store_path=str(store))
    assert verdict.ok is True, verdict.detail
    assert verdict.facts["landing_row_at"] == "2026-09-26T22:00:00Z"


# --- land.sh's documented exit 3 is a LANDED row (t_04bd0287) -----------------
#
# land.sh's exit table gives exit 3 to "landed": the merge is real, and the only
# post-merge step it skips is the flow's `merged` transition, which the next sdlc
# sync tick records. So the exit must not be read alone -- the one sub-case that
# IS a provenance gap (the merge sha's review row was never recorded) carries
# `row_recorded: false` on the same row.


@pytest.mark.parametrize("admission, exit_code, row_recorded, admitted", [
    # Every (admission, exit, row_recorded) class present on the live deploy ledger,
    # measured 2026-10-11 (1158 landing rows) -- with the verdict each one owes.
    ("admitted", 0, True, True),
    ("admitted", 0, None, True),
    (None, 3, False, False),        # genuinely non-admitted: must STAY refused
    ("admitted", 3, True, True),    # land.sh's "landed" leg (t_04bd0287)
    (None, 6, False, False),
    ("refused", 5, False, False),
    (None, 1, False, False),
    ("admitted", 0, False, True),
    (None, 0, False, True),
    ("waived", 0, True, False),
    ("refused", 5, True, False),
    (None, 4, False, False),
    ("admitted", 6, True, False),
    ("refused", 0, False, False),
    # the exit-3 sub-cases the class rule must not blur
    ("admitted", 3, False, False),  # the provenance gap the exit half exists for
    ("admitted", 3, None, False),   # an unstated row is not a "recorded" one
    # regression: every class the gate admitted before is unchanged
    ("admitted", None, None, True),
    (None, None, None, True),
])
def test_the_admitted_landing_classes(admission, exit_code, row_recorded, admitted):
    row = {"admission": admission, "exit": exit_code}
    if row_recorded is not None:
        row["row_recorded"] = row_recorded
    assert gate._is_admitted(row) is admitted


def test_an_exit3_landed_row_is_admitted_end_to_end(tmp_path):
    """The card's own shape: the only landing row is (admitted, exit 3, row_recorded true)."""
    path, digest = _artifact(tmp_path)
    ledger = _ledger_with(tmp_path, [_landing_row(LANDING_AT, exit_code=3, row_recorded=True)])
    store = _run_store(tmp_path, finished_at=RUN_AFTER)
    verdict = gate.evaluate(_proof(path, digest), ledger_path=str(ledger),
                            run_store_path=str(store))
    assert verdict.ok is True, verdict.detail
    assert verdict.facts["landing_source"] == "ledger"
    assert verdict.facts["landing_row_at"] == "2026-09-26T22:47:50Z"


def test_an_exit3_row_without_its_review_record_stays_refused_end_to_end(tmp_path):
    """The gap the exit half exists for: exit 3 with row_recorded false is NOT admitted."""
    path, digest = _artifact(tmp_path)
    ledger = _ledger_with(tmp_path, [_landing_row(LANDING_AT, exit_code=3, row_recorded=False)])
    store = _run_store(tmp_path, finished_at=RUN_AFTER)
    verdict = gate.evaluate(_proof(path, digest), ledger_path=str(ledger),
                            run_store_path=str(store))
    assert verdict.ok is False
    assert verdict.cause == "landing_not_admitted"


def _git_repo(tmp_path: Path, rel: str = "cron/deploy-train.sh", body: str = "landed bytes\n"):
    """A real work tree with one committed file -> (repo, commit, rel, committed bytes)."""
    repo = tmp_path / "checkout"
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2026-09-26T22:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-09-26T22:00:00+00:00",
        "GIT_AUTHOR_NAME": "gate", "GIT_AUTHOR_EMAIL": "gate@example.invalid",
        "GIT_COMMITTER_NAME": "gate", "GIT_COMMITTER_EMAIL": "gate@example.invalid",
    }
    for argv in (["git", "init", "--quiet", str(repo)],
                 ["git", "-C", str(repo), "add", "-A"],
                 ["git", "-C", str(repo), "commit", "--quiet", "-m", "land"]):
        subprocess.run(argv, check=True, capture_output=True, env=env)
    commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                            check=True, capture_output=True, text=True, env=env).stdout.strip()
    return repo, commit, rel, target.read_bytes()


def _deployed_copy(tmp_path: Path, rel: str, body: bytes):
    dest = tmp_path / "prod" / "unit"
    path = dest / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return dest, path


def _blob_fixture(tmp_path: Path):
    repo, commit, rel, blob = _git_repo(tmp_path)
    dest, path = _deployed_copy(tmp_path, rel, blob)
    ledger = _ledger_with(tmp_path, [_landing_row(LANDING_AT),
                                     _deploy_row(ARRIVAL_AT, REF, str(dest))])
    store = _run_store(tmp_path, finished_at=RUN_AFTER)

    def judge(entry, run_store=None, run_id="20260926-1835"):
        return gate.evaluate({"artifacts": [entry], "landing": {"ref": REF},
                              "run": {"store": "yoyoflow", "id": run_id}},
                             ledger_path=str(ledger),
                             run_store_path=str(run_store or store))

    return repo, commit, rel, blob, path, judge


def test_a_blob_pin_pins_the_merged_bytes_not_the_live_hash(tmp_path):
    """The DoD's named refusal: the live artifact must BE the blob at that commit."""
    repo, commit, rel, blob, path, judge = _blob_fixture(tmp_path)
    digest = hashlib.sha256(blob).hexdigest()
    pin = {"path": str(path), "blob": f"{commit}:{rel}", "repo": str(repo)}

    admitted = judge(pin)
    assert admitted.ok is True, admitted.detail
    row = admitted.facts["artifacts"][str(path)]
    assert row["blob_sha256"] == digest and row["blob_commit"] == commit
    assert row["blob_path"] == rel

    # declared hash and blob together: both must be the live bytes
    assert judge({**pin, "sha256": digest}).ok is True
    assert judge({**pin, "sha256": "0" * 64}).cause == "artifact_hash_mismatch"

    # bytes built from a LATER commit: the live file is no longer the merged blob
    path.write_text("bytes from a later commit\n", encoding="utf-8")
    moved = judge(pin)
    assert moved.cause == "artifact_hash_mismatch"
    assert "is not the merged blob" in moved.detail

    # and re-pinning the live hash does not launder it: the blob still rules
    repinned = judge({"path": str(path),
                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                      "blob": f"{commit}:{rel}", "repo": str(repo)})
    assert repinned.cause == "artifact_hash_mismatch"
    assert "is not the merged blob" in repinned.detail


def test_a_blob_pin_without_a_declared_repo_reads_the_artifacts_own_checkout(tmp_path):
    repo, _first, rel, blob = _git_repo(tmp_path)
    deployed = repo / "deployed" / rel            # a deployed copy the repo itself tracks
    deployed.parent.mkdir(parents=True, exist_ok=True)
    deployed.write_bytes(blob)
    for argv in (["git", "-C", str(repo), "add", "-A"],
                 ["git", "-C", str(repo), "commit", "--quiet", "-m", "deploy"]):
        subprocess.run(argv, check=True, capture_output=True)
    commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                            check=True, capture_output=True, text=True).stdout.strip()
    ledger = _ledger_with(tmp_path, [_landing_row(LANDING_AT)])
    store = _run_store(tmp_path, finished_at=RUN_AFTER)

    def judge(entry):
        return gate.evaluate({"artifacts": [entry], "landing": {"ref": REF},
                              "run": {"store": "yoyoflow", "id": "20260926-1835"}},
                             ledger_path=str(ledger), run_store_path=str(store))

    # no ``repo`` declared: the in-repo path is read off the deployed file's own checkout
    admitted = judge({"path": str(deployed), "blob": f"{commit}:{rel}"})
    assert admitted.ok is True, admitted.detail
    assert admitted.facts["artifacts"][str(deployed)]["blob_repo"] == str(repo)

    deployed.write_text("drifted on disk\n", encoding="utf-8")
    drifted = judge({"path": str(deployed), "blob": f"{commit}:{rel}"})
    assert drifted.cause == "artifact_hash_mismatch"
    assert "is not the merged blob" in drifted.detail


def test_an_unresolvable_blob_pin_is_named_not_guessed(tmp_path):
    repo, commit, rel, blob, path, judge = _blob_fixture(tmp_path)

    unknown_commit = judge({"path": str(path), "blob": f"{'d' * 40}:{rel}", "repo": str(repo)})
    assert unknown_commit.cause == "artifact_blob_unresolved"
    assert "is not a blob in" in unknown_commit.detail

    unknown_path = judge({"path": str(path), "blob": f"{commit}:cron/other.sh", "repo": str(repo)})
    assert unknown_path.cause == "artifact_blob_unresolved"

    malformed = judge({"path": str(path), "blob": commit, "repo": str(repo)})
    assert malformed.cause == "artifact_blob_unresolved"
    assert "<commit>:<path in repo>" in malformed.detail

    # deployed outside a checkout with no ``repo`` declared: there is nothing to read in
    nowhere = judge({"path": str(path), "blob": f"{commit}:{rel}"})
    assert nowhere.cause == "artifact_blob_unresolved"
    assert "no git work tree holds" in nowhere.detail

    # an unresolvable pin still does not mask the timing clause
    early = _run_db(tmp_path, "blob-early", finished_at="2026-09-26T20:00:00+00:00",
                    run_id="20260926-1835")
    stale = judge({"path": str(path), "blob": f"{'d' * 40}:{rel}", "repo": str(repo)},
                  run_store=early)
    assert stale.cause == "proof_predates_landing"
    assert "artifact_blob_unresolved" in {f["clause"] for f in stale.failures}


def test_the_deploy_stamp_dates_the_artifact_when_no_landing_ref_is_authored(tmp_path):
    """No landing block: the stamp is preferred to the filesystem mtime."""
    dest, path, digest = _dest_artifact(tmp_path)
    _write_stamp(dest, "9" * 40, deployed="2026-09-26T23:21:47-0400")  # 03:21:47Z
    absent = str(tmp_path / "absent.jsonl")
    record, overrides = _override_pin(tmp_path, path, digest, rel="cron/deploy-train.sh")
    proof = {"artifacts": [{"path": str(path), "sha256": digest, "record": record}],
             "run": {"store": "yoyoflow", "id": "late"}}

    late = _run_db(tmp_path, "late", finished_at="2026-09-27T04:00:00+00:00", run_id="late")
    admitted = gate.evaluate(proof, ledger_path=absent, run_store_path=str(late),
                             overrides_root=str(overrides))
    assert admitted.ok is True, admitted.detail
    assert admitted.facts["landing_source"] == "deploy-stamp"

    early = _run_db(tmp_path, "early", finished_at="2026-09-26T23:00:00+00:00", run_id="late")
    stale = gate.evaluate(proof, ledger_path=absent, run_store_path=str(early),
                          overrides_root=str(overrides))
    assert stale.cause == "proof_predates_landing"


# ---------------------------------------------------------------------------
# The LEDGER-LESS class: bytes no departure delivers. The ref cannot resolve,
# because no departure ever wrote one — so the class is DECLARED and pays for
# itself with a probe that names what it observed (card t_d867ddbd, 2026-09-27).
# ---------------------------------------------------------------------------

LIVE_TREE_LANDING = {"ref": "card t_d867ddbd", "source": "live-tree"}


def _ledger_less_artifact(tmp_path: Path, *, mtime: int = 1_790_000_000):
    """A checkout under no ledger dest, the way a live-tree override lands."""
    path = tmp_path / "hermes-agent" / "hermes_cli" / "kanban_db.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("live tree bytes\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))                 # 2026-09-21T14:13:20Z
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _probe(at: str, observations=None) -> dict:
    probe = {"at": at, "result": "ok"}
    if observations is not None:
        probe["observations"] = observations
    return probe


def _run_after_landing(tmp_path: Path, sub: str = "live"):
    return _run_db(tmp_path, sub, finished_at="2026-09-27T12:00:00+00:00",
                   run_id="20260926-1835")


def test_the_live_tree_class_is_admitted_on_a_probe_that_names_its_observations(tmp_path):
    """The satisfiable path for bytes no departure delivered."""
    path, digest = _ledger_less_artifact(tmp_path)
    record, overrides = _override_pin(tmp_path, path, digest, rel="hermes_cli/kanban_db.py")
    verdict = gate.evaluate(
        {"artifacts": [{"path": str(path), "sha256": digest, "record": record}],
         "landing": dict(LIVE_TREE_LANDING),
         "probe": _probe("2026-09-27T11:30:00+00:00",
                         ["an unregistered board name is refused, naming it",
                          "a write lands in each registered store, read back per store",
                          "the reproduction is refused from a worker context"])},
        ledger_path=str(tmp_path / "absent.jsonl"), overrides_root=str(overrides))
    assert verdict.ok is True, verdict.detail
    assert verdict.facts["landing_source"] == "live-tree"
    assert verdict.facts["landing_refs_unresolved_in_ledger"] == ["card t_d867ddbd"]
    assert len(verdict.facts["probe_observations"]) == 3


def test_the_live_tree_class_must_name_its_observations(tmp_path):
    """The class is a price, not a bypass: no named observations, no admission."""
    path, digest = _ledger_less_artifact(tmp_path)
    ledger = str(tmp_path / "absent.jsonl")
    record, overrides = _override_pin(tmp_path, path, digest, rel="hermes_cli/kanban_db.py")
    entries = [{"path": str(path), "sha256": digest, "record": record}]

    bare = gate.evaluate({"artifacts": entries, "landing": dict(LIVE_TREE_LANDING),
                          "probe": _probe("2026-09-27T11:30:00+00:00")},
                         ledger_path=ledger, overrides_root=str(overrides))
    assert bare.cause == "live_tree_unproven"
    assert "observations" in bare.detail

    empty = gate.evaluate({"artifacts": entries, "landing": dict(LIVE_TREE_LANDING),
                           "probe": _probe("2026-09-27T11:30:00+00:00", [])},
                          ledger_path=ledger, overrides_root=str(overrides))
    assert empty.cause == "live_tree_unproven"

    # the same probe WITHOUT the declaration is a plain probe: no class, no obligation
    plain = gate.evaluate({"artifacts": entries,
                           "probe": _probe("2026-09-27T11:30:00+00:00")},
                          ledger_path=ledger, overrides_root=str(overrides))
    assert plain.ok is True, plain.detail


def test_a_card_turn_is_not_evidence_for_the_live_tree_class(conn, tmp_path, monkeypatch):
    """A probe of the deployed behaviour, or a real run — never a card turn."""
    path, digest = _ledger_less_artifact(tmp_path)
    monkeypatch.setenv("HERMES_PROOF_LEDGER", str(tmp_path / "absent.jsonl"))
    witness = _task(conn, title="witness")
    run = conn.execute("SELECT id, started_at FROM task_runs WHERE task_id = ?",
                       (witness,)).fetchone()
    conn.execute("UPDATE task_runs SET ended_at = ?, outcome = 'completed' WHERE id = ?",
                 (run["started_at"] + 60, run["id"]))
    conn.commit()
    verdict = gate.evaluate(
        {"artifacts": [{"path": str(path), "sha256": digest}],
         "landing": dict(LIVE_TREE_LANDING),
         "run": {"store": "card", "task": witness, "id": run["id"]}},
        conn=conn, ledger_path=str(tmp_path / "absent.jsonl"))
    assert verdict.cause == "live_tree_unproven"
    assert "card turn" in verdict.detail


def test_the_live_tree_class_still_postdates_the_bytes(tmp_path):
    """The class moves the LANDING instant, it does not move the fence."""
    path, digest = _ledger_less_artifact(tmp_path)
    record, overrides = _override_pin(tmp_path, path, digest, rel="hermes_cli/kanban_db.py")
    entry = {"path": str(path), "sha256": digest, "record": record}
    early = gate.evaluate(
        {"artifacts": [entry],
         "landing": dict(LIVE_TREE_LANDING),
         "probe": _probe("2026-09-20T00:00:00+00:00", ["anything"])},
        ledger_path=str(tmp_path / "absent.jsonl"), overrides_root=str(overrides))
    assert early.cause == "proof_predates_landing"

    late = gate.evaluate(
        {"artifacts": [entry],
         "landing": dict(LIVE_TREE_LANDING),
         "probe": _probe("2026-09-27T11:30:00+00:00", ["anything"])},
        ledger_path=str(tmp_path / "absent.jsonl"), overrides_root=str(overrides))
    assert late.ok is True, late.detail


def test_a_train_delivered_artifact_cannot_declare_itself_ledger_less(tmp_path):
    """A dest in the ledger means the ledger leg exists: prove it on the train."""
    dest, path, digest = _dest_artifact(tmp_path)
    ledger = _ledger_with(tmp_path, [_landing_row(LANDING_AT),
                                     _deploy_row(ARRIVAL_AT, REF, str(dest))])
    denied = gate.evaluate(
        {"artifacts": [{"path": str(path), "sha256": digest}],
         "landing": {"ref": "not-on-the-train", "source": "live-tree"},
         "probe": _probe("2026-09-27T11:30:00+00:00", ["anything"])},
        ledger_path=str(ledger))
    assert denied.cause == "landing_live_tree_denied"
    assert str(dest) in denied.detail

    # declared ledger-less but the ledger DOES resolve the ref: the ledger wins
    resolved = gate.evaluate(
        {"artifacts": [{"path": str(path), "sha256": digest}],
         "landing": {"ref": REF, "source": "live-tree"},
         "run": {"store": "yoyoflow", "id": "20260926-1835"}},
        ledger_path=str(ledger), run_store_path=str(_run_store(tmp_path)))
    assert resolved.ok is True, resolved.detail
    assert resolved.facts["landing_source"] == "ledger"
    assert resolved.facts["live_tree_ignored"] == "the ledger resolves these bytes"


def test_a_stale_dest_row_from_another_revision_does_not_deny_the_class(tmp_path):
    """A dest row that does not account for the LIVE bytes is not a train leg.

    Measured 2026-09-27 on the fork checkout: its only ledger row is a 2026-09-21 arrival
    whose sha is not the checkout's, the dest carries no deploy stamp and the work-tree
    file is modified. Refusing the class there would make it unprovable for exactly the
    artifact the ledger happened to touch once — the remedy the refusal names (prove it on
    the train) does not exist for those bytes.
    """
    path, digest = _ledger_less_artifact(tmp_path)          # tmp_path/hermes-agent/...
    dest = tmp_path / "hermes-agent"
    ledger = _ledger_with(tmp_path, [
        _deploy_row("2026-09-21T13:16:39-0400", "75cbde58" + "1" * 32, str(dest),
                    stamped=False),
    ])
    record, overrides = _override_pin(tmp_path, path, digest, rel="hermes_cli/kanban_db.py")
    verdict = gate.evaluate(
        {"artifacts": [{"path": str(path), "sha256": digest, "record": record}],
         "landing": dict(LIVE_TREE_LANDING),
         "probe": _probe("2026-09-27T11:30:00+00:00",
                         ["an unregistered board name is refused, and it names the board"])},
        ledger_path=str(ledger), overrides_root=str(overrides))
    assert verdict.ok is True, verdict.detail
    assert verdict.facts["landing_source"] == "live-tree"
    # considered, recorded, and not a refusal: a third party can read why
    assert verdict.facts["live_tree_dest_rows"][0]["sha"].startswith("75cbde58")
    assert verdict.facts["live_tree_dest_rows"][0]["stamped"] is False


def test_a_dest_stamp_that_names_the_row_denies_the_class(tmp_path):
    """The dest's own stamp recording the row's sha means the train IS the carrier."""
    dest, path, digest = _dest_artifact(tmp_path)
    _write_stamp(dest, REF)
    ledger = _ledger_with(tmp_path, [_deploy_row(ARRIVAL_AT, REF, str(dest), stamped=False)])
    denied = gate.evaluate(
        {"artifacts": [{"path": str(path), "sha256": digest}],
         "landing": {"ref": "not-on-the-train", "source": "live-tree"},
         "probe": _probe("2026-09-27T11:30:00+00:00", ["anything"])},
        ledger_path=str(ledger))
    assert denied.cause == "landing_live_tree_denied"
    assert REF in denied.detail
    assert denied.facts["live_tree_denied"]["row_sha"] == REF


def test_every_clause_is_evaluated_with_the_landing_first(tmp_path):
    """The order a landed card owes: landing, then the run, then the artifact.

    A handoff naming both an unresolvable landing ref and a stale pin is told about the
    LANDING first — the identity clause never pre-empts the DoD clause — and a declared
    path that is not a readable file is named as the structural subject while the clauses
    beside it are reported rather than short-circuited.
    """
    path, digest = _ledger_less_artifact(tmp_path)

    masked = gate.evaluate(
        {"artifacts": [{"path": str(path), "sha256": "0" * 64}],
         "landing": {"ref": "no-such-landing"},
         "run": {"store": "yoyoflow", "id": "20260926-1835"}},
        ledger_path=str(tmp_path / "absent.jsonl"),
        run_store_path=str(_run_after_landing(tmp_path)))
    assert masked.cause == "landing_unresolved"
    assert [f["clause"] for f in masked.failures] == [
        "landing_unresolved", "artifact_hash_mismatch", "artifact_unpinned"]

    missing = gate.evaluate({"artifacts": [{"path": str(tmp_path / "nope.py")}]},
                            ledger_path=str(tmp_path / "absent.jsonl"))
    assert missing.cause == "artifact_missing"
    assert missing.facts["landing_undated"] == "no readable artifact to date the landing"
    assert {f["clause"] for f in missing.failures} == {"no_run_or_probe", "artifact_missing"}


# ---------------------------------------------------------------------------
# THE DURABLE REFERENT (ruling t_6f582c2b, D1/D2): a pin carrying only path +
# sha256 is refused `artifact_unpinned`, and the R3 `record` admits the
# ledger-less live-tree class. The read-only tri-state is the census a reader
# gets for already-accepted pins (D3(iii)).
# ---------------------------------------------------------------------------

ABSENT_LEDGER = "absent.jsonl"


def _live_tree_proof(entries, *, probe_at="2026-09-27T11:30:00+00:00", observations=None):
    return {"artifacts": entries,
            "landing": dict(LIVE_TREE_LANDING),
            "probe": _probe(probe_at, observations if observations is not None
                            else ["an unregistered board name is refused, naming it"])}


def test_a_pin_with_no_durable_referent_is_refused_artifact_unpinned(tmp_path):
    """(a) ``{path, sha256}`` on a landed proof: nothing the gate can re-resolve."""
    path, digest = _ledger_less_artifact(tmp_path)
    verdict = gate.evaluate(_live_tree_proof([{"path": str(path), "sha256": digest}]),
                            ledger_path=str(tmp_path / ABSENT_LEDGER))
    assert verdict.cause == "artifact_unpinned"
    assert {f["clause"] for f in verdict.failures} == {"artifact_unpinned"}
    assert str(path) in verdict.detail and digest in verdict.detail
    assert "blob" in verdict.detail and "record" in verdict.detail


def test_a_bare_path_string_is_refused_artifact_unpinned(tmp_path):
    """(b) a bare path: parseable, but it can never pass on a landed card."""
    path, _digest = _ledger_less_artifact(tmp_path)
    verdict = gate.evaluate(_live_tree_proof([str(path)]),
                            ledger_path=str(tmp_path / ABSENT_LEDGER))
    assert verdict.cause == "artifact_unpinned"
    assert str(path) in verdict.detail


def test_an_override_manifest_record_admits_the_live_tree_class(tmp_path):
    """(c) MANIFEST row + byte copy carry the bytes: the record admits."""
    path, digest = _ledger_less_artifact(tmp_path)
    record, overrides = _override_pin(tmp_path, path, digest, rel="hermes_cli/kanban_db.py")
    verdict = gate.evaluate(
        _live_tree_proof([{"path": str(path), "sha256": digest, "record": record}]),
        ledger_path=str(tmp_path / ABSENT_LEDGER), overrides_root=str(overrides))
    assert verdict.ok is True, verdict.detail
    row = verdict.facts["artifacts"][str(path)]
    assert row["referent"] == "record"
    assert row["record_kind"] == "override-manifest"
    assert row["record_row_sha256"] == digest


def test_an_override_manifest_row_that_disagrees_is_refused(tmp_path):
    """(d) the row disagrees with the pin: refused, naming the row and the pin."""
    path, digest = _ledger_less_artifact(tmp_path)
    record, overrides = _override_pin(tmp_path, path, "0" * 64, rel="hermes_cli/kanban_db.py")
    verdict = gate.evaluate(
        _live_tree_proof([{"path": str(path), "sha256": digest, "record": record}]),
        ledger_path=str(tmp_path / ABSENT_LEDGER), overrides_root=str(overrides))
    assert verdict.cause == "artifact_unpinned"
    assert "MANIFEST row" in verdict.detail
    assert ("0" * 16) in verdict.detail and digest[:16] in verdict.detail


def test_an_override_manifest_without_a_row_is_refused(tmp_path):
    """(e) the row is absent: an unreadable record fails closed."""
    path, digest = _ledger_less_artifact(tmp_path)
    root = tmp_path / "untracked-overrides"
    root.mkdir()
    (root / "MANIFEST.md").write_text(
        "| repo-relative path | sha256 | why |\n|---|---|---|\n", encoding="utf-8")
    record = {"kind": "override-manifest", "id": "hermes_cli/kanban_db.py"}
    verdict = gate.evaluate(
        _live_tree_proof([{"path": str(path), "sha256": digest, "record": record}]),
        ledger_path=str(tmp_path / ABSENT_LEDGER), overrides_root=str(root))
    assert verdict.cause == "artifact_unpinned"
    assert "has no row" in verdict.detail


def _payload_record(tmp_path: Path, name: str = "t_deadbeef"):
    root = tmp_path / "r5-preroll"
    root.mkdir(parents=True, exist_ok=True)
    record = root / f"{name}-payload.diff"
    record.write_bytes(b"--- a/x\n+++ b/x\n+landed\n")
    good = hashlib.sha256(record.read_bytes()).hexdigest()
    (root / (record.name + ".sha256")).write_text(f"{good}  {record.name}\n", encoding="utf-8")
    return {"kind": "payload-record", "id": name}, root, record, good


def test_a_payload_record_admits_and_a_mismatched_sidecar_is_refused(tmp_path):
    """(f) the r5-preroll record + a matching sidecar admits; a stale sidecar refuses."""
    path, digest = _ledger_less_artifact(tmp_path)
    record, root, record_file, good = _payload_record(tmp_path)
    admitted = gate.evaluate(
        _live_tree_proof([{"path": str(path), "sha256": digest, "record": record}]),
        ledger_path=str(tmp_path / ABSENT_LEDGER), preroll_root=str(root))
    assert admitted.ok is True, admitted.detail
    assert admitted.facts["artifacts"][str(path)]["record_sha256"] == good

    (root / (record_file.name + ".sha256")).write_text("f" * 64 + "\n", encoding="utf-8")
    refused = gate.evaluate(
        _live_tree_proof([{"path": str(path), "sha256": digest, "record": record}]),
        ledger_path=str(tmp_path / ABSENT_LEDGER), preroll_root=str(root))
    assert refused.cause == "artifact_unpinned"
    assert "sidecar" in refused.detail


def test_existing_blob_and_ledger_pins_are_still_admitted(tmp_path):
    """(g) no regression: the ledger leg and the merged-blob pin still admit."""
    path, digest = _artifact(tmp_path)
    ledger = _ledger(tmp_path, at="2026-09-26T18:47:50-04:00")
    store = _run_db(tmp_path, "bound", finished_at="2026-09-26T23:00:00+00:00",
                    run_id="20260926-1835")
    bound = gate.evaluate(
        {"artifacts": [{"path": str(path), "sha256": digest}],
         "landing": {"ref": "abc123def4567890"},
         "run": {"store": "yoyoflow", "id": "20260926-1835"}},
        ledger_path=str(ledger), run_store_path=str(store))
    assert bound.ok is True, bound.detail
    assert bound.facts["artifacts"][str(path)]["referent"] == "ledger"

    repo, commit, rel, _blob, deployed, judge = _blob_fixture(tmp_path)
    admitted = judge({"path": str(deployed), "blob": f"{commit}:{rel}", "repo": str(repo)})
    assert admitted.ok is True, admitted.detail
    assert admitted.facts["artifacts"][str(deployed)]["referent"] == "blob"


def test_a_stale_hash_with_no_referent_names_both_clauses(tmp_path):
    """(h) a stale pin does not hide behind the unpinned refusal, and vice versa."""
    path, _digest = _ledger_less_artifact(tmp_path)
    verdict = gate.evaluate(_live_tree_proof([{"path": str(path), "sha256": "0" * 64}]),
                            ledger_path=str(tmp_path / ABSENT_LEDGER))
    assert verdict.cause == "artifact_unpinned"
    assert {f["clause"] for f in verdict.failures} == {
        "artifact_unpinned", "artifact_hash_mismatch"}
    text = gate.render("t_demo", verdict)
    assert "[artifact_unpinned]" in text and "[artifact_hash_mismatch]" in text


def test_audit_pin_is_a_read_only_tri_state(tmp_path):
    """(i) resolves / superseded / unverifiable, decided by the recorded facts."""
    path, digest = _ledger_less_artifact(tmp_path)
    record, overrides = _override_pin(tmp_path, path, digest, rel="hermes_cli/kanban_db.py")
    pin = {"path": str(path), "sha256": digest, "record": record}

    resolving = gate.audit_pin(pin, overrides_root=str(overrides))
    assert resolving["state"] == "resolves"
    assert resolving["referent"] == "record"

    # the live bytes move on, but the record still resolves: landed then superseded
    path.write_text("a later payload overwrote these bytes\n", encoding="utf-8")
    superseded = gate.audit_pin(pin, overrides_root=str(overrides))
    assert superseded["state"] == "superseded"
    assert superseded["referent"] == "record"

    # a legacy pin with NO durable referent: unverifiable even while live still matches
    legacy_path, legacy_digest = _artifact(tmp_path, name="legacy.py")
    legacy = gate.audit_pin({"path": str(legacy_path), "sha256": legacy_digest})
    assert legacy["state"] == "unverifiable"
    assert legacy["live"] == legacy_digest
    assert legacy["referent"] is None

    census = gate.render_audit("t_demo", [resolving, superseded, legacy])
    assert census.count("[unverifiable]") == 1


def _kill_proof_gate_import(monkeypatch):
    """Make ``from hermes_cli import kanban_proof_gate`` raise ``ImportError``.

    Poisoning ``sys.modules`` ALONE is not enough (measured): once the submodule has been
    imported once the name is an attribute of the ``hermes_cli`` package, and
    ``from hermes_cli import X`` resolves through ``getattr`` WITHOUT consulting
    ``sys.modules`` -- so both the package attribute and the ``sys.modules`` entry must go.
    """
    import sys

    import hermes_cli

    monkeypatch.delattr(hermes_cli, "kanban_proof_gate", raising=False)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_proof_gate", None)


@pytest.mark.parametrize("contract", ["local-only", None])
def test_a_missing_proof_gate_module_does_not_block_a_non_landed_completion(
        conn, tmp_path, monkeypatch, contract):
    """(regression, t_c7a4666f) The untracked gate module is pinned live state; a tree that
    has lost it must refuse only a ``landed`` card -- never every completion on the board."""
    monkeypatch.setenv("HERMES_PROOF_LEDGER", str(tmp_path / "absent.jsonl"))
    monkeypatch.setenv("HERMES_PROOF_RUN_STORE", str(tmp_path / "absent.db"))
    _kill_proof_gate_import(monkeypatch)
    tid = _task(conn, contract=contract, title="local work on a tree missing the gate module")

    assert kb.complete_task(conn, tid, summary="purely local work")
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert row["status"] == "done"


def test_a_missing_proof_gate_module_still_fails_closed_on_a_landed_completion(
        conn, tmp_path, monkeypatch):
    """(regression, t_c7a4666f) ...and the fence is NOT weakened: a ``landed`` card still
    refuses (fail-closed, by type) when the gate module cannot be imported, and the card is
    left untouched -- so a lost module can never silently admit a landed completion."""
    monkeypatch.setenv("HERMES_PROOF_LEDGER", str(tmp_path / "absent.jsonl"))
    monkeypatch.setenv("HERMES_PROOF_RUN_STORE", str(tmp_path / "absent.db"))
    _kill_proof_gate_import(monkeypatch)
    tid = _task(conn, contract="landed", title="landed work on a tree missing the gate module")

    with pytest.raises(ImportError):
        kb.complete_task(conn, tid, summary="landed and proven", metadata={"proof": {}})

    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert row["status"] == "running"  # fail closed: the card was not mutated


# ---------------------------------------------------------------------------
# Status is not outcome (card t_63e4bfc6). The engine writes the run's OUTCOME
# to ``runs.verdict`` (``yoyoflow/engine.py::resolve_outcome``) - ``pass`` iff the
# column is exactly "pass" - while ``runs.status`` records what the engine did.
# The gate must score green from BOTH, with a red status dominating.
# ---------------------------------------------------------------------------

def test_gated_run_with_pass_verdict_is_green(tmp_path):
    """A run the outcome gate PASSED (verdict='pass') is green, not the false ``run_not_green``.

    The defect this fixes: green was scored from ``(verdict or status) in
    GREEN_RUN_STATUSES``, and that set carries no ``pass`` token - so run
    ``20261005-1846`` (status=completed, verdict=pass) read NOT green and a ``landed``
    card was refused on evidence that was legitimately green.
    """
    path, digest = _artifact(tmp_path)
    ledger = _ledger(tmp_path, at="2026-09-26T18:47:50-04:00")  # 22:47:50Z, before the run
    store = _run_store(tmp_path, status="completed", verdict="pass",
                       finished_at="2026-09-26T23:00:00+00:00")
    verdict = gate.evaluate(
        {"artifacts": [{"path": str(path), "sha256": digest}],
         "landing": {"ref": "abc123def4567890"},
         "run": {"store": "yoyoflow", "id": "20260926-1835"}},
        ledger_path=str(ledger), run_store_path=str(store))
    assert verdict.ok is True, verdict.detail
    assert verdict.facts["run_verdict"] == "pass"
    assert verdict.facts["run_status"] == "completed"


def test_fail_verdict_is_not_green_even_when_completed(tmp_path):
    """status='completed' + verdict='fail': the outcome REVOKES the green status."""
    path, digest = _artifact(tmp_path)
    ledger = _ledger(tmp_path, at="2026-09-26T18:47:50-04:00")  # 22:47:50Z, before the run
    store = _run_store(tmp_path, status="completed", verdict="fail",
                       finished_at="2026-09-26T23:00:00+00:00")
    red = gate.evaluate(
        {"artifacts": [{"path": str(path), "sha256": digest}],
         "landing": {"ref": "abc123def4567890"},
         "run": {"store": "yoyoflow", "id": "20260926-1835"}},
        ledger_path=str(ledger), run_store_path=str(store))
    assert red.ok is False
    assert red.cause == "run_not_green"


def test_failed_status_dominates_a_pass_verdict(tmp_path):
    """The engine's ``status-failed`` dominates: a verdict never MANUFACTURES a green run."""
    path, digest = _artifact(tmp_path)
    ledger = _ledger(tmp_path, at="2026-09-26T18:47:50-04:00")  # 22:47:50Z, before the run
    store = _run_store(tmp_path, status="failed", verdict="pass",
                       finished_at="2026-09-26T23:00:00+00:00")
    red = gate.evaluate(
        {"artifacts": [{"path": str(path), "sha256": digest}],
         "landing": {"ref": "abc123def4567890"},
         "run": {"store": "yoyoflow", "id": "20260926-1835"}},
        ledger_path=str(ledger), run_store_path=str(store))
    assert red.ok is False
    assert red.cause == "run_not_green"


# ---------------------------------------------------------------------------
# The defect this pair pins (ruling t_90767078, card t_28dcede4): a ``landed``
# card could not close by ANY door. The PR-acceptance clause fired on it and
# could never pass (16 receipts, every one ``classification:"missing"``, one
# supplying PR #1082 head-on), while the landed clause had NO live call site --
# so ``landed`` was a contract the kernel could persist, could not author, and
# could not discharge. These two cases are the pair the defect lacked:
#   (a) an ADMITTED proof CLOSES the card, and the PR clause never runs;
#   (b) an UNADMITTED proof is refused BY THE LANDED CLAUSE, not by the PR clause.
# ---------------------------------------------------------------------------

def test_landed_completion_with_admitted_proof_closes_without_the_pr_clause(
        conn, tmp_path, monkeypatch):
    """(a) The PR-acceptance clause is INAPPLICABLE to a ``landed`` card (D1/D2).

    The fence is the deploy-proof gate: the landing ledger (22:47:50Z) precedes the
    run (23:00Z), so the proof is admitted and the card closes. No ``pr_acceptance``
    receipt is written at all -- that clause can no longer strand a landed card, and
    the silence is asserted rather than assumed.
    """
    path, digest = _artifact(tmp_path)
    ledger = _ledger(tmp_path, at="2026-09-26T18:47:50-04:00")  # landing before the run
    store = _run_store(tmp_path, finished_at="2026-09-26T23:00:00+00:00")
    monkeypatch.setenv("HERMES_PROOF_LEDGER", str(ledger))
    monkeypatch.setenv("HERMES_PROOF_RUN_STORE", str(store))
    tid = _task(conn, contract="landed", title="landed work")
    proof = {"artifacts": [{"path": str(path), "sha256": digest}],
             "landing": {"ref": "abc123def4567890"},
             "run": {"store": "yoyoflow", "id": "20260926-1835"}}

    assert kb.complete_task(conn, tid, summary="landed and proven",
                            metadata={"proof": proof}) is True

    kinds = [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,)).fetchall()]
    assert "proof_gate_admitted" in kinds
    assert "completed" in kinds
    assert "pr_acceptance" not in kinds  # the PR clause never fired
    row = conn.execute(
        "SELECT status, last_failure_error FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert row["status"] == "done"
    assert not row["last_failure_error"]


def test_landed_completion_with_unadmitted_proof_is_refused_by_the_landed_clause(
        conn, tmp_path, monkeypatch):
    """(b) An unadmitted proof is refused by the LANDED clause, not by the PR clause.

    The run (23:00Z) predates the landing (23:30-04:00 == 03:30Z next day), so the
    gate refuses ``proof_predates_landing``. The card is not mutated, and the absence
    of a ``pr_acceptance`` receipt plus a clean ``last_failure_error`` proves the PR
    clause took no part in the verdict -- which is exactly what the defect hid behind
    an always-``missing`` acceptance receipt.
    """
    path, digest = _artifact(tmp_path)
    ledger = _ledger(tmp_path, at="2026-09-26T23:30:00-04:00")  # landing AFTER the run
    store = _run_store(tmp_path, finished_at="2026-09-26T23:00:00+00:00")
    monkeypatch.setenv("HERMES_PROOF_LEDGER", str(ledger))
    monkeypatch.setenv("HERMES_PROOF_RUN_STORE", str(store))
    tid = _task(conn, contract="landed", title="landed work with a predating run")
    proof = {"artifacts": [{"path": str(path), "sha256": digest}],
             "landing": {"ref": "abc123def4567890"},
             "run": {"store": "yoyoflow", "id": "20260926-1835"}}

    with pytest.raises(kb.ProofGateError) as caught:
        kb.complete_task(conn, tid, summary="landed but the run predates it",
                         metadata={"proof": proof})

    assert caught.value.clause == "proof_predates_landing"
    row = conn.execute(
        "SELECT status, last_failure_error FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert row["status"] == "running"  # nothing mutated
    assert not row["last_failure_error"]
    kinds = [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,)).fetchall()]
    assert "completion_blocked_proof_gate" in kinds
    assert "pr_acceptance" not in kinds
    assert "completed" not in kinds
