"""Hard failure for an update autostash whose restore did NOT happen.

``hermes update`` stashes the user's local modifications before it moves the checkout. When the
restore conflicts it parks them and the tree the fleet would restart onto is missing the local
override set -- restarting there is the live outage. These tests pin the guard: no restart, a
deterministic remediation card, and a verdict that is never green.
"""

import contextlib

import pytest

from hermes_cli import update_hard_failure


STASH_REF = "c67e05ca4b8178daf8e5c5ac6da4a659b536cf6f"
CONFLICTED = "hermes_cli/kanban_db.py"


def _hard_fact(**overrides):
    fact = {
        "stash_ref": STASH_REF,
        "file_count": 230,
        "conflicted": [CONFLICTED],
        "detail": "restore hit conflicts",
        "reapply": update_hard_failure.reapply_command(STASH_REF),
    }
    fact.update(overrides)
    return fact


def _parked_receipt(fact=None, step_ok=False):
    """One receipt shaped as ``UpdateReceipt.__init__`` writes it (``steps``/``skips``/``stages``)."""
    return {
        "schema": 1,
        "update_id": "u" * 32,
        "outcome": "running",
        "steps": [{"name": "local_changes_stash", "ok": step_ok,
                   "detail": f"parked: {STASH_REF} (restore hit conflicts)"}],
        "skips": [], "stages": [], "fleet": [],
        **({update_hard_failure.FACT: fact} if fact is not None else {}),
    }


class _Probe:
    """Minimal active receipt: ``update_receipt._record`` clones it per call, so the test reads
    ``update_receipt._current.get().data`` rather than this object's own dict."""

    def __init__(self):
        self.data = {}

    def step(self, name, ok, detail=""):
        self.data.setdefault("steps", []).append({"name": name, "ok": bool(ok), "detail": detail})

    def fact(self, key, value):
        self.data[key] = value

    def skip(self, name, reason):
        self.data.setdefault("skips", []).append({"name": name, "reason": reason})

    def stage(self, name, outcome, **facts):
        self.data.setdefault("stages", []).append({"name": name, "outcome": outcome, **facts})


@contextlib.contextmanager
def _active(probe):
    from hermes_cli import update_receipt

    token = update_receipt._current.set(probe)
    try:
        yield
    finally:
        update_receipt._current.reset(token)


# --- the verdict: deterministic, keyed on the step result + the recorded conflicted files ---

def test_the_hard_failure_keys_on_the_stash_step_and_the_conflicted_files():
    hard = update_hard_failure.unrestored_local_changes(_parked_receipt(_hard_fact()))
    assert hard is not None
    assert hard["stash_ref"] == STASH_REF
    assert hard["file_count"] == 230
    assert hard["conflicted"] == [CONFLICTED]
    assert hard["reapply"] == f"git stash show -p {STASH_REF} | git apply --3way"
    # The card names all four facts a human must act on, with no prose to interpret.
    body = update_hard_failure.card_body(hard)
    for expected in (STASH_REF, "230", CONFLICTED, "git stash show -p", "git apply --3way"):
        assert expected in body, (expected, body)
    assert "restart was SKIPPED" in body


def test_a_park_the_user_asked_for_is_not_a_hard_failure():
    """``--keep-stash``/a declined restore records ``ok=False`` too; only the unsolicited park
    writes the fact, and only the fact holds the fleet."""
    assert update_hard_failure.unrestored_local_changes(_parked_receipt()) is None


def test_a_later_restore_that_succeeded_clears_the_hard_failure():
    receipt = _parked_receipt(_hard_fact())
    receipt["steps"].append({"name": "local_changes_stash", "ok": True, "detail": f"restored: {STASH_REF}"})
    assert update_hard_failure.unrestored_local_changes(receipt) is None


def test_the_card_is_keyed_on_the_stash_so_a_re_run_cannot_file_a_second_one(tmp_path, monkeypatch):
    """The stash ref is the idempotency key: the same verdict files one card, twice is still one."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    hard = update_hard_failure.unrestored_local_changes(_parked_receipt(_hard_fact()))
    assert hard is not None
    first = update_hard_failure.file_card(hard)
    second = update_hard_failure.file_card(hard)
    assert first and second == first
    import sqlite3

    with sqlite3.connect(tmp_path / "kanban.db") as conn:
        rows = conn.execute("SELECT id, assignee, status, title FROM tasks").fetchall()
    assert [row[0] for row in rows] == [first]
    assert rows[0][1] == update_hard_failure.CARD_ASSIGNEE
    assert rows[0][3].startswith("Update autostash unrestored:")


# --- the feed: the real conflict path records the fact ---

def test_conflicted_restore_records_the_hard_failure_on_the_receipt(tmp_path):
    import subprocess

    from hermes_cli import main as hermes_main, update_receipt

    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, text=True, check=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    source = tmp_path / "tools" / "terminal_tool.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "init")

    source.write_text("VALUE = 2\n", encoding="utf-8")          # local edit ...
    stash_ref = hermes_main._stash_local_changes_if_needed(["git"], tmp_path)
    assert stash_ref
    git("checkout", "HEAD")
    source.write_text("VALUE = 3\n", encoding="utf-8")          # ... that the pull moves over
    git("add", "-A")
    git("commit", "-qm", "pulled change")

    probe = _Probe()
    with _active(probe):
        assert hermes_main._restore_stashed_changes(["git"], tmp_path, stash_ref, prompt_user=False) is False
        data = update_receipt._current.get().data

    hard = update_hard_failure.unrestored_local_changes(data)
    assert hard is not None, data
    assert hard["stash_ref"] == stash_ref
    assert hard["file_count"] == 1
    assert hard["conflicted"] == ["tools/terminal_tool.py"], hard


def test_keep_stash_records_no_hard_failure(capsys):
    """The user-asked park (the Desktop updater's --keep-stash) must never hold the fleet."""
    from hermes_cli import update_receipt
    import hermes_cli.update_cmd_stash as stash_mod

    probe = _Probe()
    with _active(probe):
        stash_mod._park_stashed_changes(STASH_REF)
        data = update_receipt._current.get().data

    assert update_hard_failure.unrestored_local_changes(data) is None
    assert [row for row in data["steps"] if row["name"] == "local_changes_stash"][0]["ok"] is False


# --- the receipt verdict: never green for a stripped tree ---

@pytest.mark.parametrize("requested", ["success", "partial"])
def test_a_stripped_tree_never_finalizes_success_or_partial(requested):
    from hermes_cli import update_receipt

    receipt = update_receipt.UpdateReceipt()
    receipt.data.update(_parked_receipt(_hard_fact()))
    receipt.data["user_action"] = {"step": "local_changes", "reason": "parked"}
    receipt.finalize(requested)
    assert receipt.data["outcome"] == "failed"


def test_a_parked_stash_the_user_asked_for_still_finalizes_partial():
    from hermes_cli import update_receipt

    receipt = update_receipt.UpdateReceipt()
    receipt.data.update(_parked_receipt())
    receipt.data["user_action"] = {"step": "local_changes", "reason": "declined"}
    receipt.finalize("success")
    assert receipt.data["outcome"] == "partial"


# --- the fleet: the run does not reach the restart stage ---

def _completion_request(root, receipt, *, completion_message=None):
    return {
        "schema": 1, "source": str(root), "home": str(root), "branch": "main",
        "desktop": False, "assume_yes": True, "gateway_mode": False,
        "pre_update_version": "old", "snapshot_id": None, "sibling_snapshots": {},
        "plan": {"runtimes": []}, "receipt": receipt, "windows_resume": {},
        **({"completion_message": completion_message} if completion_message else {}),
    }


@pytest.fixture
def completion_probe(tmp_path, monkeypatch):
    """``_complete_selected`` with its collaborators stubbed; records whether the fleet restarted."""
    from hermes_cli import source_completion, update_cmd

    restarted = []
    root = tmp_path / "checkout"
    (root / "hermes_cli").mkdir(parents=True)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setattr(update_cmd, "_sweep_bytecode_after_update", lambda branch: None)
    monkeypatch.setattr(update_cmd, "_fleet_restart_skip_reason", lambda plan: None)
    monkeypatch.setattr(update_cmd, "_restart_gateway_fleet_after_update",
                        lambda plan, gateway_mode: restarted.append(plan) or type("R", (), {"incomplete": False})())
    monkeypatch.setattr(update_cmd, "_resume_windows_gateways_and_merge_outcome", lambda *a, **k: None)
    monkeypatch.setattr(update_cmd, "_verify_fleet_after_update", lambda *a, **k: None)
    monkeypatch.setattr(source_completion, "complete_source_checkout", lambda *a, **k: True)
    return root, restarted


def _run_completion(root, receipt, completion_message=None):
    from hermes_cli import update_completion, update_receipt

    request = _completion_request(root, receipt, completion_message=completion_message)
    update_completion._resume_receipt(receipt)
    try:
        complete = update_completion._complete_selected(request)
        data = update_receipt._current.get().data
    finally:
        update_receipt._current.set(None)
    return complete, data


def test_a_stripped_tree_does_not_restart_and_files_the_card(completion_probe, capsys):
    import sqlite3

    root, restarted = completion_probe
    receipt = _parked_receipt(_hard_fact())

    complete, data = _run_completion(root, receipt, completion_message="⚠ local changes are parked")

    assert restarted == []                                   # requirement 1: no restart
    assert complete is False                                 # nothing claims "Update complete"
    assert [row["outcome"] for row in data["stages"] if row["name"] == "restart"] == ["skipped"]
    assert any(row["name"] == "gateway_restart" for row in data["skips"])
    with sqlite3.connect(root.parent / "kanban.db") as conn:  # requirement 2: the card
        rows = conn.execute("SELECT assignee, title FROM tasks").fetchall()
    assert len(rows) == 1 and rows[0][0] == update_hard_failure.CARD_ASSIGNEE
    assert STASH_REF[:12] in rows[0][1]
    assert "Remediation card filed:" in capsys.readouterr().out


def test_a_run_without_the_hard_failure_still_restarts(completion_probe):
    """Negative control: the gate keys on the hard failure, it does not simply always hold."""
    root, restarted = completion_probe
    receipt = _parked_receipt()

    complete, data = _run_completion(root, receipt)

    assert complete is True
    assert restarted != []
    assert [row["outcome"] for row in data["stages"] if row["name"] == "restart"] == ["success"]
