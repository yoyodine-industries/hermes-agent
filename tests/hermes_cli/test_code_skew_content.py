"""The ONE staleness primitive (card ``t_4dbee32e``): content skew, and its two blind spots.

What these tests are pinned to, and why each one exists:

  * ``test_content_skew_is_invisible_to_mtime`` -- THE incident. On 2026-10-10 a gateway that
    booted at 13:06:29 served in-process gate bytes from 2026-09-30 while every mtime-based
    staleness check reported ``stale: false``, because the stale process itself defined the
    newest ``.py`` mtime in the tree. The test reproduces that shape literally: content changes,
    then every byte's mtime is pushed BACK far enough that no mtime test can see it, and the
    primitive still answers skewed.
  * ``test_unknown_is_never_clear`` -- three ways to know nothing (no boot record, a recycled
    pid, an unreadable tree) and the rule that they must not be reported as clear.
  * ``test_the_dispatcher_and_the_tree_module_ask_this_unit`` -- the card's central constraint:
    the dispatcher's own gate may not carry a second comparison.
  * ``test_cli_reports_all_three_verdicts_as_distinct_exit_codes`` -- a shell consumer must not be
    able to fold unknown into clear.
  * ``test_the_persisted_record_carries_the_process_identity`` -- the record is readable from
    OUTSIDE the process, and a pid another process now holds answers unknown.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import code_skew_content as csc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import tree_fingerprint as tf

TREE_FILES = (
    ("hermes_cli", "kanban_db.py"),
    ("hermes_cli", "kanban_db_dispatch.py"),
    ("gateway", "run.py"),
)


# ---------------------------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_boot_state(monkeypatch):
    """No boot record, no memo, and a sandbox kanban home: a test never reads the live host's."""
    tf.reset_boot()
    tf._content_memo.clear()
    yield
    tf.reset_boot()
    tf._content_memo.clear()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "checkout"
    for package, module in TREE_FILES:
        directory = root / package
        directory.mkdir(parents=True, exist_ok=True)
        (directory / module).write_text(f"# {package}/{module}\nVALUE = 1\n", encoding="utf-8")
    return root


def _edit(root: Path, body: str = "VALUE = 2\n") -> None:
    (root / "hermes_cli" / "kanban_db.py").write_text(
        f"# hermes_cli/kanban_db.py\n{body}", encoding="utf-8"
    )


def _age_every_byte(root: Path, *, seconds_ago: int) -> int:
    """Push every file's mtime ``seconds_ago`` into the past; return that stamp."""
    stamp = int(time.time()) - seconds_ago
    for path in root.rglob("*.py"):
        os.utime(path, (stamp, stamp))
    return stamp


# ---------------------------------------------------------------------------------------------
# (1) the incident: content skew that no mtime test can see
# ---------------------------------------------------------------------------------------------


def test_content_skew_is_invisible_to_mtime(home, tree):
    """A rewrite that is OLDER than the process start is still a skew, and mtime cannot see it.

    This is the live shape, not a contrived one: the frozen process booted at 13:06:29 and the
    bytes it was serving came from ~270h before that, so every "newest .py mtime > process start"
    test answered clear by construction.
    """
    boot = tf.record_boot(root=tree)
    assert boot and boot["fingerprint"]
    process_started = int(time.time())

    _edit(tree)
    stamp = _age_every_byte(tree, seconds_ago=3600)
    newest_mtime = max(int(p.stat().st_mtime) for p in tree.rglob("*.py"))
    assert newest_mtime < process_started, "precondition: mtime staleness is BLIND here"

    measured = csc.measure(os.getpid(), tree)
    assert measured["verdict"] == csc.VERDICT_SKEWED, measured
    assert measured["skew"] is True
    assert measured["boot"] == boot["fingerprint"]
    assert measured["disk"] != measured["boot"]
    assert csc.content_skew(os.getpid(), tree) is True
    # ...and the mtime form of the same question, for contrast: nothing is newer than the boot.
    assert newest_mtime <= process_started and stamp < process_started


def test_a_byte_identical_tree_is_clear(home, tree):
    """The other side of the pair: no skew is a real, positive answer (not just 'no exception')."""
    tf.record_boot(root=tree)
    assert csc.content_skew(os.getpid(), tree) is False
    assert csc.measure(os.getpid(), tree)["verdict"] == csc.VERDICT_CLEAR


# ---------------------------------------------------------------------------------------------
# (2) unknown is never clear
# ---------------------------------------------------------------------------------------------


def test_unknown_is_never_clear(home, tree):
    """Three ways to know nothing, and none of them may read as 'clear'."""
    # a) nothing was ever recorded for this process
    assert csc.process_tree_fingerprint(os.getpid()) is None
    assert csc.content_skew(os.getpid(), tree) is None
    assert csc.measure(os.getpid(), tree)["verdict"] == csc.VERDICT_UNKNOWN

    # b) a persisted record whose pid another process now holds: a RECYCLED pid is not a
    #    fingerprint. PID 1 is alive on every host, and its identity is not the one we invent.
    tf.record_boot(root=tree)
    record = dict(tf.recorded_boot(os.getpid()) or {})
    assert record, "the boot record must be readable from outside the writing call"
    foreign = home / "kanban" / "tree_boot_records" / "1.json"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text(json.dumps({**record, "pid": 1, "identity": "bootsession:not-this-host|0|0"}),
                       encoding="utf-8")
    assert csc.process_tree_fingerprint(1) is None
    assert csc.content_skew(1, tree) is None

    # c) a tree that is not there
    assert csc.disk_tree_fingerprint(home / "nope") is None
    assert csc.content_skew(os.getpid(), home / "nope") is None


def test_the_persisted_record_carries_the_process_identity(home, tree):
    """The record exists for OUTSIDE readers, and it names the process it belongs to."""
    boot = tf.record_boot(root=tree)
    recorded = tf.recorded_boot(os.getpid())
    assert recorded is not None, "record_boot must persist its record"
    assert recorded["fingerprint"] == boot["fingerprint"]
    assert Path(recorded["tree"]).resolve() == tree.resolve()
    assert recorded["identity"] == csc.process_identity(os.getpid())
    assert csc.process_tree_fingerprint(os.getpid()) == boot["fingerprint"]
    # The path is the contract the carrier resolves: <kanban home>/kanban/tree_boot_records.
    assert tf.boot_records_dir() == Path(os.environ["HERMES_KANBAN_HOME"]) / "kanban" / "tree_boot_records"


def test_a_record_for_another_tree_is_unknown_not_skewed(home, tree, tmp_path):
    """A fingerprint of a DIFFERENT tree cannot be compared with this one: unknown, not skewed."""
    tf.record_boot(root=tree)
    other = tmp_path / "elsewhere"
    (other / "hermes_cli").mkdir(parents=True)
    (other / "hermes_cli" / "kanban_db.py").write_text("VALUE = 9\n", encoding="utf-8")
    assert csc.content_skew(os.getpid(), other) is None
    assert csc.measure(os.getpid(), other)["verdict"] == csc.VERDICT_UNKNOWN


# ---------------------------------------------------------------------------------------------
# (3) one definition: the dispatcher's gate and tree_fingerprint both ask THIS unit
# ---------------------------------------------------------------------------------------------


def test_the_dispatcher_and_the_tree_module_ask_this_unit(home, tree, monkeypatch):
    """The card's constraint, pinned: no second comparison behind the dispatcher's gate."""
    tf.record_boot(root=tree)
    calls = []

    def fake_measure(pid=None, tree_arg=None, **kwargs):
        calls.append((pid, tree))
        return {
            "pid": pid, "tree": str(tree), "boot": "boot", "disk": "disk",
            "skew": True, "verdict": csc.VERDICT_SKEWED, "source": "persisted", "why": "fake",
        }

    monkeypatch.setattr(csc, "measure", fake_measure)
    skew = kbd._consumer_tree_skew()
    assert skew == ("boot", "disk", os.getpid()), skew
    assert calls, "the dispatcher's gate must ASK code_skew_content.measure"
    assert calls[0][0] == os.getpid()

    # ...and the legacy door delegates rather than re-deriving the comparison.
    detected = tf.detect_skew()
    assert detected == ("boot", "disk"), detected


def test_the_process_identity_has_one_definition(monkeypatch):
    """``_process_fingerprint`` (liveness) and the persisted record must agree byte for byte."""
    monkeypatch.setattr(csc, "process_identity", lambda pid: f"identity:{int(pid)}")
    assert kbd._process_fingerprint(4242) == "identity:4242"


# ---------------------------------------------------------------------------------------------
# (4) the CLI door
# ---------------------------------------------------------------------------------------------


def _cli(home: Path, *args: str) -> subprocess.CompletedProcess:
    repo = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["HERMES_KANBAN_HOME"] = str(home)
    env["HERMES_HOME"] = str(home)
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.code_skew_content", *args],
        capture_output=True, text=True, env=env, cwd=str(repo), timeout=120,
    )


def test_cli_reports_all_three_verdicts_as_distinct_exit_codes(home, tree):
    """0 clear / 3 skewed / 4 unknown, each on a real subprocess (the carrier's consumer shape)."""
    tf.record_boot(root=tree)
    pid = str(os.getpid())

    clear = _cli(home, "--pid", pid, "--tree", str(tree), "--json")
    assert clear.returncode == csc.EXIT_CLEAR, (clear.returncode, clear.stdout, clear.stderr)
    assert json.loads(clear.stdout)["verdict"] == csc.VERDICT_CLEAR

    _edit(tree)
    skewed = _cli(home, "--pid", pid, "--tree", str(tree), "--json")
    assert skewed.returncode == csc.EXIT_SKEWED, (skewed.returncode, skewed.stdout, skewed.stderr)
    body = json.loads(skewed.stdout)
    assert body["verdict"] == csc.VERDICT_SKEWED
    assert body["boot"] != body["disk"]

    unknown = _cli(home, "--pid", "999999", "--tree", str(tree))
    assert unknown.returncode == csc.EXIT_UNKNOWN, (unknown.returncode, unknown.stdout, unknown.stderr)
    assert "no boot fingerprint is recorded" in unknown.stdout


def test_exit_codes_are_distinct_and_none_of_them_is_zero_by_accident():
    """The three verdicts cannot share a code: that folding is the false green this removes."""
    assert len({csc.EXIT_CLEAR, csc.EXIT_SKEWED, csc.EXIT_UNKNOWN}) == 3
    assert csc.EXIT_CLEAR == 0
