"""The board's three write-door invariants (``hermes_cli/kanban_gate_invariants.py``).

Each family is proven TWO-SIDED here, because a gate that only refuses is as untrustworthy as
one that only warns: the violating write must be refused AND the legitimate write must pass,
with the refusal naming the honest path. The measurements behind ``hermes kanban gates
report`` / ``reconcile`` are covered too, since they are what makes a disabled gate visible.

These tests ARM the evidence gate explicitly (``HERMES_KANBAN_EVIDENCE_GATE=refuse``), because
the suite as a whole declares ``measure`` (see ``tests/conftest.py``) — the kernel default is
refuse, and the refusal is what production runs.
"""

from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_gate_invariants as gates


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A temp HERMES_HOME with its own boards and NO profiles on disk."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_EVIDENCE_GATE", "refuse")
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_WORKSPACE", "HERMES_PROOF_RUN_STORE"):
        monkeypatch.delenv(var, raising=False)
    db_path = kb.kanban_db_path(board="default")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return tmp_path


@pytest.fixture
def conn(home):
    with kbc.connect_closing() as c:
        yield c


def _profile(home, name: str) -> None:
    """A profile on disk: the roster the assignee invariant judges against."""
    path = home / "profiles" / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.yaml").write_text("{}\n", encoding="utf-8")


def _lane(home, *names: str) -> None:
    """A declared pull lane: the honest path for a handle with no profile."""
    path = home / "kanban" / "terminal_lanes"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(names) + "\n", encoding="utf-8")


class _Refusal:
    """Catch a gate refusal by CONTRACT rather than by class identity.

    Every refusal is a ``ValueError`` carrying the ``invariant`` that refused it (and, for the
    evidence gate, the ``cause`` inside that invariant). Asserting the contract instead of
    ``pytest.raises(gates.AssigneeRefused)`` is deliberate: this suite sandboxes homes, paths
    and ``sys.modules``, so the class object a test module holds at import is not guaranteed to
    be the object the running kernel raises. The contract is what a caller can depend on —
    ``invariant`` and ``cause`` are what the CLI and the refusal vocabulary read.
    """

    def __init__(self, invariant: str, cause=None) -> None:
        self.invariant = invariant
        self.cause = cause
        self.value = None

    def __enter__(self) -> "_Refusal":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is None:
            raise AssertionError(f"expected a {self.invariant!r} refusal; none was raised")
        if not isinstance(exc, ValueError):
            return False
        if getattr(exc, "invariant", None) != self.invariant:
            return False
        if self.cause is not None and getattr(exc, "cause", None) != self.cause:
            return False
        self.value = exc
        return True


def _refusal(invariant: str, cause=None) -> _Refusal:
    return _Refusal(invariant, cause)


# ── Invariant B: the assignee can run ───────────────────────────────────────

def test_a_handle_with_no_profile_is_refused_and_a_real_profile_passes(conn, home):
    _profile(home, "real-lane")
    tid = kb.create_task(conn, title="ok", assignee="real-lane")
    assert tid.startswith("t_")

    with _refusal("assignee") as excinfo:
        kb.create_task(conn, title="stranded", assignee="retired-lane")
    message = str(excinfo.value)
    assert "retired-lane" in message
    # The refusal must name both honest paths, or it is a wall rather than a gate.
    assert "real-lane" in message
    assert "terminal_lanes" in message
    left = [r["id"] for r in conn.execute("SELECT id FROM tasks WHERE title='stranded'")]
    assert left == []


def test_a_declared_pull_lane_is_accepted_without_a_profile(conn, home):
    _profile(home, "real-lane")
    _lane(home, "orchestrator")
    assert kb.create_task(conn, title="pulled", assignee="orchestrator").startswith("t_")


def test_reassignment_onto_a_dead_handle_is_refused_too(conn, home):
    _profile(home, "real-lane")
    tid = kb.create_task(conn, title="ok", assignee="real-lane")
    with _refusal("assignee"):
        kb.assign_task(conn, tid, "gone-lane")
    assert kb.get_task(conn, tid).assignee == "real-lane"


def test_with_no_roster_and_no_lanes_the_gate_stands_down_loudly(conn, home):
    """Nothing on disk to judge BY: the write passes, and the report says the family is unarmed."""
    assert kb.create_task(conn, title="harness card", assignee="anything") is not None
    record = gates.report(conn)
    assert record["armed"]["assignee_roster"] == 0
    assert gates.assignee_violations(conn, "default") == []


def test_stranded_rows_are_measured_and_repaired_only_from_a_declared_alias(conn, home):
    _profile(home, "financially-stl")
    tid = kb.create_task(conn, title="legacy", assignee="financially-stl")
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "legacy")
    conn.execute("UPDATE tasks SET assignee='financially-sme' WHERE id=?", (tid,))

    measured = gates.assignee_violations(conn, "default")
    assert [row["task_id"] for row in measured] == [tid]

    # No alias declared: the pass names it and repairs nothing. Re-typing it would be a guess.
    first = gates.reconcile(conn, "default")
    assert first["repaired"] == []
    assert [row["task_id"] for row in first["unrepaired"]] == [tid]
    assert kb.get_task(conn, tid).assignee == "financially-sme"

    (home / "kanban").mkdir(parents=True, exist_ok=True)
    (home / "kanban" / "assignee_aliases.json").write_text(
        '{"financially-sme": "financially-stl"}\n', encoding="utf-8")
    second = gates.reconcile(conn, "default")
    assert second["repaired"] == [{"task_id": tid, "from": "financially-sme",
                                  "to": "financially-stl"}]
    assert kb.get_task(conn, tid).assignee == "financially-stl"
    kinds = [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id=? ORDER BY created_at", (tid,))]
    assert "assignee_aliased" in kinds
    assert gates.assignee_violations(conn, "default") == []


# ── Invariant C: the block carries the edge ────────────────────────────────

def test_a_dependency_block_that_only_names_the_card_in_prose_is_refused(conn, home):
    _profile(home, "lane")
    parent = kb.create_task(conn, title="parent", assignee="lane")
    child = kb.create_task(conn, title="child", assignee="lane")
    with _refusal("dependency") as excinfo:
        kb.block_task(conn, child, reason=f"waiting on {parent} to land", kind="dependency")
    assert parent in str(excinfo.value)
    assert "waits_on" in str(excinfo.value)
    assert kb.get_task(conn, child).status != "blocked"


def test_waits_on_creates_the_edge_and_parks_the_card_as_a_dependency_wait(conn, home):
    _profile(home, "lane")
    parent = kb.create_task(conn, title="parent", assignee="lane")
    child = kb.create_task(conn, title="child", assignee="lane")
    assert kb.block_task(conn, child, reason=f"waiting on {parent}", kind="dependency",
                         waits_on=[parent]) is True
    edges = [r["parent_id"] for r in conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id=?", (child,))]
    assert edges == [parent]
    assert kb.get_task(conn, child).status == "todo"  # a dependency wait, not a human block
    assert gates.dependency_violations(conn, "default") == []


def test_waits_on_naming_a_card_that_does_not_exist_is_refused(conn, home):
    _profile(home, "lane")
    child = kb.create_task(conn, title="child", assignee="lane")
    with _refusal("dependency"):
        kb.block_task(conn, child, reason="waiting", kind="dependency", waits_on=["t_deadbeef11"])
    assert kb.get_task(conn, child).status == "ready"


def test_a_prose_only_wait_of_any_other_kind_is_measured_not_guessed_at(conn, home):
    """An untyped block is allowed at the door, and the board still NAMES the wait it cannot see."""
    _profile(home, "lane")
    parent = kb.create_task(conn, title="parent", assignee="lane")
    child = kb.create_task(conn, title="child", assignee="lane")
    assert kb.block_task(conn, child, reason=f"blocked on {parent}") is True
    measured = gates.dependency_violations(conn, "default")
    assert [row["task_id"] for row in measured] == [child]
    assert measured[0]["unedged"] == [parent]
    pass_one = gates.reconcile(conn, "default")
    assert pass_one["repaired"] == []
    assert "not a kind='dependency' block" in pass_one["unrepaired"][0]["why"]
    assert [r["parent_id"] for r in conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id=?", (child,))] == []


def test_reconcile_repairs_a_legacy_dependency_block_that_claims_the_wait(conn, home):
    _profile(home, "lane")
    parent = kb.create_task(conn, title="parent", assignee="lane")
    child = kb.create_task(conn, title="child", assignee="lane")
    kb.block_task(conn, child, reason="parked")           # no names, no edge
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET block_kind='dependency' WHERE id=?", (child,))
        kb._append_event(conn, child, "blocked", {"reason": f"waiting on {parent}"})
    assert [row["task_id"] for row in gates.dependency_violations(conn, "default")] == [child]
    out = gates.reconcile(conn, "default")
    assert {"task_id": child, "edges": [parent]} in out["repaired"]
    assert gates.dependency_violations(conn, "default") == []


# ── Invariant A: the completion declares its evidence ──────────────────────

def test_a_completion_with_no_evidence_is_refused(conn, home):
    _profile(home, "lane")
    tid = kb.create_task(conn, title="card", assignee="lane")
    with _refusal("evidence") as excinfo:
        kb.complete_task(conn, tid, result="done, trust me")
    assert excinfo.value.cause == "no_evidence"
    assert kb.get_task(conn, tid).status != "done"
    kinds = [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id=?", (tid,))]
    assert "completion_blocked_evidence" in kinds


def test_a_none_declaration_costs_a_why_and_is_recorded(conn, home):
    _profile(home, "lane")
    tid = kb.create_task(conn, title="card", assignee="lane")
    with _refusal("evidence"):
        kb.complete_task(conn, tid, result="done",
                         metadata={"evidence": {"class": "none", "why": ""}})
    assert kb.complete_task(conn, tid, result="done", metadata={
        "evidence": {"class": "none", "why": "a skill edit, no runnable artifact"}}) is True
    payload = [r["payload"] for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='evidence_none'", (tid,))]
    assert payload and "skill edit" in payload[0]


def test_a_declared_run_that_does_not_resolve_is_refused_in_the_gate_vocabulary(conn, home):
    _profile(home, "lane")
    tid = kb.create_task(conn, title="card", assignee="lane")
    with _refusal("evidence") as excinfo:
        kb.complete_task(conn, tid, result="done", metadata={
            "evidence": {"class": "run", "run": {"store": "yoyoflow", "id": 987654}}})
    assert excinfo.value.cause == "run_unknown"
    assert kb.get_task(conn, tid).status != "done"


def test_a_malformed_declaration_is_refused_with_its_own_cause(conn, home):
    _profile(home, "lane")
    tid = kb.create_task(conn, title="card", assignee="lane")
    with _refusal("evidence") as excinfo:
        kb.complete_task(conn, tid, result="done", metadata={"evidence": {"class": "vibes"}})
    assert excinfo.value.cause == "evidence_malformed"


def test_the_kill_switch_records_itself_instead_of_waiving_silently(conn, home, monkeypatch):
    _profile(home, "lane")
    monkeypatch.setenv("HERMES_KANBAN_EVIDENCE_GATE", "measure")
    tid = kb.create_task(conn, title="card", assignee="lane")
    assert kb.complete_task(conn, tid, result="done, undeclared") is True
    kinds = [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id=?", (tid,))]
    assert "evidence_gate_measure" in kinds
    assert gates.evidence_gate_mode() == "measure"

    # A typo must never read as a waiver.
    monkeypatch.setenv("HERMES_KANBAN_EVIDENCE_GATE", "mesure")
    assert gates.evidence_gate_mode() == "refuse"
    monkeypatch.setenv("HERMES_KANBAN_EVIDENCE_GATE", "refuse")
    other = kb.create_task(conn, title="card2", assignee="lane")
    with _refusal("evidence"):
        kb.complete_task(conn, other, result="done")


def test_an_approval_out_of_review_is_structurally_exempt(conn, home):
    _profile(home, "lane")
    tid = kb.create_task(conn, title="card", assignee="lane")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
    assert kb.complete_task(conn, tid, result="approved") is True
    kinds = [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id=?", (tid,))]
    assert "evidence_exempt" in kinds


def test_the_evidence_family_measures_what_the_gate_recorded(conn, home):
    """No stamp, no measurement: a board with ten years of history is not ten years of violations."""
    _profile(home, "lane")
    tid = kb.create_task(conn, title="card", assignee="lane")
    assert gates.evidence_violations(conn, "default") == []          # unarmed: nothing to measure
    assert kb.complete_task(conn, tid, result="done", metadata={
        "evidence": {"class": "none", "why": "documentation only"}}) is True
    stamped = gates.reconcile(conn, "default")
    assert stamped["evidence_gate_since"]
    assert gates.evidence_violations(conn, "default") == []
    # A completion that never passed the door - an ungated carrier writing its own done row -
    # is exactly what the family is for.
    other = kb.create_task(conn, title="card2", assignee="lane")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='done', completed_at=? WHERE id=?",
                     (gates.evidence_gate_since("default") + 1, other))
    measured = [row["task_id"] for row in gates.evidence_violations(conn, "default")]
    assert measured == [other]
    assert gates.report(conn)["counts"]["evidence"]["total"] == 1
    assert gates.reconcile(conn, "default")["violations_after"]["evidence"] == 1
