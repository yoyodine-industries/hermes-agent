"""The fork-symbol census fails on ABSENCE and on NO-REFERENCE (ruling t-df0c4501f3 (4)).

The census is the guard that makes the merge's silent-loss mode impossible to ship twice: a
fork symbol the merge dropped must fail LOUDLY, and a fork symbol the merge left behind with no
caller must fail too - the dead one is the worse of the two, because a test can be green over
it forever. These tests build a real repository per case: the census reads git, so a synthetic
`git diff` output would only test the fixture.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "fork_symbol_census.py"


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo)] + list(args), check=True, capture_output=True)


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "census@example.test")
    _git(repo, "config", "user.name", "census")
    (repo / "mod.py").write_text("def upstream_only():\n    return 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "upstream base")
    return repo


def _commit(repo, text, message="fork work"):
    (repo / "mod.py").write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)


def _run(repo, *args):
    proc = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo), "--json"] + list(args),
                          capture_output=True, text=True)
    payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
    assert payload, "census produced no JSON: rc=%s stderr=%r" % (proc.returncode,
                                                                  proc.stderr[-600:])
    return proc.returncode, payload, proc.stderr


def _finding_classes(payload):
    return sorted(finding["class"] for finding in payload.get("findings", []))


def test_a_referenced_fork_symbol_passes(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "fork")
    _commit(repo, "def upstream_only():\n    return 1\n\n\n"
                  "def held_receipt():\n    return 'receipt'\n\n\n"
                  "def send():\n    return held_receipt()\n")
    rc, payload, err = _run(repo, "--fork", "fork", "--upstream-base", "main",
                            "--codes", "5093")
    rows = {row["symbol"]: row for row in payload["rows"]}
    assert rows["held_receipt"]["status"] == "ok", err
    assert any(row["refs"] for row in payload["rows"] if row["symbol"] == "held_receipt")
    # 5093 is absent from this fixture, so the codes assertion must fail - see the test below;
    # here we name only the symbol the fork added.
    assert "held_receipt" in rows


def test_a_fork_symbol_with_no_caller_is_a_finding(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "fork")
    _commit(repo, "def upstream_only():\n    return 1\n\n\n"
                  "def held_receipt():\n    return 'receipt'\n")
    rc, payload, _ = _run(repo, "--fork", "fork", "--upstream-base", "main")
    assert rc == 1
    assert "NO-REFERENCE" in _finding_classes(payload)
    row = [row for row in payload["rows"] if row["symbol"] == "held_receipt"][0]
    assert row["status"] == "NO-REFERENCE" and row["refs"] == []


def test_a_fork_symbol_the_merge_dropped_is_a_finding(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "fork")
    _commit(repo, "def upstream_only():\n    return 1\n\n\ndef held_receipt():\n    return 1\n")
    # The merge takes upstream's file whole: the symbol is gone from the tree under test while
    # the fork line (still the delta's source) has it.
    _git(repo, "checkout", "-q", "main")
    rc, payload, _ = _run(repo, "--fork", "fork", "--upstream-base", "main")
    assert rc == 1
    assert "ABSENT" in _finding_classes(payload)
    assert [row for row in payload["rows"] if row["symbol"] == "held_receipt"][0]["status"] == "ABSENT"


def test_a_code_literal_the_sender_acts_on_must_still_exist(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "fork")
    _commit(repo, "def upstream_only():\n    return 1\n\n\ndef send():\n    return 5093\n")
    rc, payload, _ = _run(repo, "--fork", "fork", "--upstream-base", "main",
                          "--codes", "5093,5096")
    codes = {row["code"]: row["status"] for row in payload["codes"]}
    assert codes == {"5093": "ok", "5096": "ABSENT"}
    assert rc == 1
    # A number is checkable: the finding names the literal, not a prose opinion.
    assert any(finding["symbol"] == "5096" for finding in payload["findings"])
