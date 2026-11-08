"""Dispatcher-tree fence — BOTH arms, not one (card t_70e91ef2).

The dispatching process records the ``hermes_cli`` tree it dispatches from; an
estate-mutating CLI verb running from a DIFFERENT tree refuses fail-closed with the
greppable ``tree-skew refusal:`` prefix.

Arms:
  A  record names another tree  -> REFUSAL, naming both trees + dispatcher pid + record path
  B  record names this tree     -> ALLOWED
  C  no record                  -> ALLOWED + one-line warning naming the caller tree and the path
  D  dispatcher's own in-process writes (and every read verb) -> NEVER fenced
  E  the retired ``HERMES_ALLOW_TREE_SKEW`` (card t_73155b4a)  -> NO effect: the fence is absolute

``set-model`` is fenced (card t_73155b4a, F2): it retargets the model a card dispatches on, an
estate write. The delegated-child fence does not list it — a named divergence, not an oversight.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]

#: The waiver this module used to honour and no longer does (card t_73155b4a, arm b). Spelled as a
#: literal on purpose: the test asserts the NAME has no effect, so it must not be importable from
#: the module under test.
RETIRED_ALLOW_SKEW_ENV = "HERMES_ALLOW_TREE_SKEW"

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import tree_identity as ti


def _record_path(home: Path) -> Path:
    return home / "kanban" / ti.RECORD_FILENAME


def _arm_record(home: Path, tree: str, *, pid: int = 4242) -> Path:
    """Write a dispatcher-tree record naming ``tree`` (the shape the gateway writes)."""
    rec = _record_path(home)
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(
        json.dumps({"version": 1, "tree": tree, "pid": pid, "process_start": 1790526000.0,
                    "recorded_at": 1790526614.0, "purpose": "test"}),
        encoding="utf-8",
    )
    return rec


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated kanban home (the record's own resolution root)."""
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    # The retired waiver must never leak in from the ambient env (card t_73155b4a).
    monkeypatch.delenv(RETIRED_ALLOW_SKEW_ENV, raising=False)
    return root


# --- arm A / B / C: the helper's own contract ------------------------------------------

def test_foreign_recorded_tree_refuses_and_names_everything(home):
    """A) record names another tree -> refusal naming both trees, the pid and the record."""
    foreign = "/opt/other-checkout/hermes-agent"
    rec = _arm_record(home, foreign, pid=4242)

    msg = ti.dispatching_tree_refusal("complete")

    assert msg is not None
    assert msg.startswith(ti.REFUSAL_PREFIX)
    assert str(ti.current_tree()) in msg, "the CALLING tree must be named"
    assert foreign in msg, "the dispatching tree must be named"
    assert "4242" in msg, "the dispatcher pid must be named"
    assert str(rec) in msg, "the record path must be named"


def test_record_naming_this_tree_allows(home):
    """B) record names this tree -> allowed (no refusal)."""
    _arm_record(home, str(ti.current_tree()))

    assert ti.dispatching_tree_refusal("complete") is None


def test_absent_record_allows_with_a_warning(home, caplog):
    """C) no record -> allowed, but never silent about it."""
    assert not _record_path(home).exists()

    with caplog.at_level(logging.WARNING, logger="hermes_cli.tree_identity"):
        assert ti.dispatching_tree_refusal("complete") is None

    assert caplog.records, "the allow arm must still emit a warning"
    text = caplog.text
    assert str(ti.current_tree()) in text
    assert str(_record_path(home)) in text


def test_corrupt_record_is_treated_as_absent_not_as_a_brick(home, caplog):
    """A hand-edited/garbage record must not lock the whole estate out of its own board."""
    rec = _record_path(home)
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text("{not json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="hermes_cli.tree_identity"):
        assert ti.dispatching_tree_refusal("complete") is None
    assert str(rec) in caplog.text


# --- arm D: reads and the dispatcher's own writes are never fenced ----------------------

@pytest.mark.parametrize("verb", ["list", "ls", "show", "stats", "health", "runs", "log",
                                  "assignees", "context", "attachments", "notify-list",
                                  "diagnostics", "tail", "watch"])
def test_read_verbs_are_never_fenced(verb):
    """D) reads stay allowed by construction (the classifier, not the record, decides)."""
    assert ti.cli_action_moves_estate(verb, None) is False


@pytest.mark.parametrize("verb", ["dispatch", "claim", "heartbeat", "complete", "block",
                                  "promote", "decompose", "defcon", "reclaim", "set-contract",
                                  "set-model",
                                  "create", "assign", "comment", "attach", "archive", "unblock",
                                  "schedule", "request-review", "request-changes", "reopen-review",
                                  "specify", "gc", "repair", "edit", "link", "unlink", "swarm",
                                  "reassign", "attach-rm", "init", "notify-subscribe",
                                  "notify-unsubscribe", "daemon"])
def test_estate_mutating_verbs_are_fenced(verb):
    """The card's list, and the delegated-child list, are both covered."""
    assert ti.cli_action_moves_estate(verb, None) is True


def test_board_metadata_mutations_are_fenced_reads_are_not():
    assert ti.cli_action_moves_estate("boards", "switch") is True
    assert ti.cli_action_moves_estate("boards", "create") is True
    assert ti.cli_action_moves_estate("boards", "list") is False
    assert ti.cli_action_moves_estate("boards", None) is False


def test_the_fence_is_at_least_as_wide_as_the_delegated_child_fence():
    """Drift guard: a verb the delegation fence calls a mutation must also be tree-fenced."""
    from hermes_cli import kanban as kanban_cli

    missed = sorted(
        a for a in kanban_cli._DELEGATED_CHILD_DENIED_ACTIONS
        if not ti.cli_action_moves_estate(a, None)
    )
    assert missed == [], f"tree fence is narrower than the delegated fence: {missed}"
    assert kanban_cli._TREE_SKEW_DENIED_ACTIONS is kanban_cli._DELEGATED_CHILD_DENIED_ACTIONS


def test_set_model_is_tree_fenced_though_the_delegated_fence_omits_it():
    """F2, card t_73155b4a: ``set-model`` retargets the model a card dispatches on.

    That is an estate write, so a skewed CLI must not perform it. The delegated-child fence does
    not list it — a NAMED divergence (its width is platform-stl's ruling), not an oversight; this
    test pins the divergence so closing it is a deliberate edit.
    """
    from hermes_cli import kanban as kanban_cli

    assert ti.cli_action_moves_estate("set-model", None) is True
    assert "set-model" in ti.ESTATE_MUTATING_ACTIONS
    assert "set-model" not in kanban_cli._DELEGATED_CHILD_DENIED_ACTIONS


def test_dispatcher_in_process_writes_never_consult_the_fence(home):
    """D) a foreign record does not stop the DB layer — the fence lives in the CLI seam only."""
    _arm_record(home, "/opt/other-checkout/hermes-agent")
    kb.init_db()
    conn = kbc.connect()

    tid = kb.create_task(conn, title="in-process write while a foreign record is armed")

    assert tid
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert row is not None
    # Structural half: the DB/dispatch layer must not grow a dependency on the fence module.
    from hermes_cli import kanban_db_dispatch as kbd

    for mod in (kb, kbd):
        src = Path(mod.__file__ or "").read_text(encoding="utf-8")
        assert "tree_identity" not in src, f"{mod.__name__} must not import the fence"


def test_no_env_var_waives_the_fence(home):
    """E) The installer waiver this module used to honour is GONE (card t_73155b4a, arm b).

    Measurement, 2026-09-29: no caller needs it. The waiver could only be read by a tree carrying
    this module, and the trees the installer door resolves carry none (its PATH-first resolutions
    land on the self-updater's isolated runtime venv, whose editable finder binds ``hermes_cli``
    to ``~/.hermes/installs/<id>/environments/<id>/workspace`` — measured: no ``tree_identity.py``
    and no fence call in that tree's kanban.py). A waiver would also be the wrong repair for those
    callers: both resolve their CLI from ``PATH``, so exempting them exempts board writes from
    whatever tree PATH happens to lead to.
    """
    _arm_record(home, "/opt/other-checkout/hermes-agent")

    os.environ[RETIRED_ALLOW_SKEW_ENV] = "1"
    try:
        assert ti.dispatching_tree_refusal("complete") is not None
    finally:
        os.environ.pop(RETIRED_ALLOW_SKEW_ENV, None)

    assert not hasattr(ti, "ALLOW_SKEW_ENV"), "the waiver constant must be gone, not merely unset"
    assert not hasattr(ti, "allowlist_env_set"), "the waiver reader must be gone too"


# --- the record the dispatching process writes ------------------------------------------

def test_record_dispatching_tree_names_this_tree_its_pid_and_start_time(home):
    """Shape 1: tree + pid + start time in ONE overwritten file, never appended to."""
    assert ti.record_dispatching_tree(purpose="test-arm") is True

    rec = _record_path(home)
    data = json.loads(rec.read_text(encoding="utf-8"))
    assert data["tree"] == str(ti.current_tree())
    assert data["pid"] == os.getpid()
    assert data["version"] == ti.RECORD_VERSION
    assert data["purpose"] == "test-arm"
    assert data["recorded_at"]
    assert data["process_start"] is None or isinstance(data["process_start"], float)
    assert stat.S_IMODE(rec.stat().st_mode) == 0o600


def test_record_is_overwritten_never_appended(home):
    ti.record_dispatching_tree(purpose="first")
    first_size = _record_path(home).stat().st_size
    ti.record_dispatching_tree(purpose="second")

    data = json.loads(_record_path(home).read_text(encoding="utf-8"))
    assert data["purpose"] == "second"
    assert _record_path(home).stat().st_size < first_size + 64


def test_record_round_trip_allows_the_tree_that_recorded_it(home):
    ti.record_dispatching_tree(purpose="test-arm")

    assert ti.dispatching_tree_refusal("complete") is None


def test_assert_dispatching_tree_is_fail_closed(home):
    """The named entry point raises rather than returning a verdict."""
    _arm_record(home, "/opt/other-checkout/hermes-agent", pid=4242)

    with pytest.raises(ti.TreeSkewRefusal) as exc:
        ti.assert_dispatching_tree("complete")
    assert str(exc.value).startswith(ti.REFUSAL_PREFIX)
    assert str(ti.current_tree()) in str(exc.value)

    _arm_record(home, str(ti.current_tree()))
    ti.assert_dispatching_tree("complete")


# --- end-to-end: the real CLI, both arms ------------------------------------------------

def _run_hermes(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["HERMES_KANBAN_HOME"] = str(home)
    for name in ("HERMES_KANBAN_BOARD", "HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT",
                 "HERMES_DELEGATED_CHILD_CONTEXT"):
        env.pop(name, None)
    # E) The retired waiver is set on EVERY e2e child (card t_73155b4a): a tree that is not the
    # dispatching one must refuse anyway, and the allow arm must still be allowed by the RECORD,
    # never by the env var.
    env[RETIRED_ALLOW_SKEW_ENV] = "1"
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "hermes_cli.main", *args],
                          cwd=ROOT, env=env, capture_output=True, text=True, check=False,
                          timeout=90)


def _refusal_lines(stderr: str) -> list[str]:
    return [ln for ln in stderr.splitlines() if ln.startswith(ti.REFUSAL_PREFIX)]


def test_cli_refuses_a_mutation_from_a_tree_that_is_not_the_dispatching_one(tmp_path):
    """End-to-end arm A: the refusal is a non-zero exit with the greppable prefix."""
    home = tmp_path / "hermes"
    home.mkdir()
    created = _run_hermes(home, "kanban", "create", "tree-skew e2e probe", "--json")
    assert created.returncode == 0, created.stderr
    tid = json.loads(created.stdout)["id"]

    foreign = "/opt/other-checkout/hermes-agent"
    _arm_record(home, foreign, pid=4242)

    refused = _run_hermes(home, "kanban", "complete", tid, "--result", "tree-skew e2e probe")
    assert refused.returncode != 0, refused.stdout
    assert _refusal_lines(refused.stderr), refused.stderr
    assert foreign in refused.stderr
    assert str(ROOT.resolve()) in refused.stderr

    # The board did not move.
    after = json.loads(_run_hermes(home, "kanban", "show", tid, "--json").stdout)
    assert after["task"]["status"] != "done"

    # A read still works while the fence is armed.
    listed = _run_hermes(home, "kanban", "list")
    assert listed.returncode == 0, listed.stderr


def test_cli_allows_a_mutation_when_the_record_names_this_tree(tmp_path):
    """End-to-end arm B: same verb, same board — allowed once the record names the caller."""
    home = tmp_path / "hermes"
    home.mkdir()
    created = _run_hermes(home, "kanban", "create", "tree-skew e2e allow", "--json")
    assert created.returncode == 0, created.stderr
    tid = json.loads(created.stdout)["id"]

    _arm_record(home, str(ROOT.resolve()), pid=os.getpid())

    done = _run_hermes(home, "kanban", "complete", tid, "--result", "tree-skew e2e allow")
    assert done.returncode == 0, done.stderr
    assert not _refusal_lines(done.stderr), done.stderr
    after = json.loads(_run_hermes(home, "kanban", "show", tid, "--json").stdout)
    assert after["task"]["status"] == "done"


def test_cli_refuses_set_model_from_a_tree_that_is_not_the_dispatching_one(tmp_path):
    """End-to-end F2 (acceptance 1, card t_73155b4a): a skewed ``set-model`` refuses.

    ``set-model`` retargets the model a card dispatches on, so a tree that is not the
    dispatcher's must not reach it — with the unchanged ``tree-skew refusal:`` text — while the
    same verb on the same board succeeds from the dispatching tree.
    """
    home = tmp_path / "hermes"
    home.mkdir()
    created = _run_hermes(home, "kanban", "create", "tree-skew set-model probe", "--json")
    assert created.returncode == 0, created.stderr
    tid = json.loads(created.stdout)["id"]

    # Same verb, same board, dispatching tree (no foreign record yet) -> allowed.
    allowed = _run_hermes(home, "kanban", "set-model", tid, "tree-skew-probe-model")
    assert allowed.returncode == 0, allowed.stderr
    assert not _refusal_lines(allowed.stderr), allowed.stderr

    foreign = "/opt/other-checkout/hermes-agent"
    _arm_record(home, foreign, pid=4242)

    refused = _run_hermes(home, "kanban", "set-model", tid, "another-probe-model")
    assert refused.returncode != 0, refused.stdout
    lines = _refusal_lines(refused.stderr)
    assert lines, refused.stderr
    assert lines[0].startswith(ti.REFUSAL_PREFIX)
    assert str(ti.current_tree()) in refused.stderr
    assert foreign in refused.stderr
    assert str(ROOT.resolve()) in refused.stderr
