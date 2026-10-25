"""Data is not code, and a mention is not an execution (card t_1b4d6bac).

MEASURED 2026-10-25: `python gate.py --admit-rev ...` (and `./land.sh`, and `review.sh`)
were REFUSED at the tool boundary for every gateway-hosted lane, with

    `/opt/hermes_prod/deploy-ledger.jsonl` is larger than the scan cap (1048576 bytes)

Nothing executed that ledger and nothing named it: gate.py:147 merely carries it as
DEFAULT_LEDGER, the walk recursed into gate.py with `candidate_executed` INHERITED from the
parent, and the append-only receipt log (34 MB, mode 664, first bytes `{"`) was then judged as
a script the command runs. The same walk also yielded prose fragments of gate.py's docstrings
as "referenced scripts".

The rule these arms pin: a path may fail closed on size ONLY when the command actually
executes it - the command word, or an interpreter's argument, transitively through scripts
those run. A path found by text-scanning another script, or named as an option value, is DATA
unless a bounded head read proves otherwise (executable bit, or `#!` in the first two bytes).
Data is never scanned and never fails closed, however large. And a refusal that DOES fire
leaves a row the run and the audit can read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cron import lifecycle_guard

guard = lifecycle_guard.contains_gateway_lifecycle_command_or_referenced_script


@pytest.fixture(autouse=True)
def _small_cap_and_private_ledger(monkeypatch, tmp_path):
    """A tiny scan cap (so "oversized" is cheap) and a private refusal ledger."""
    monkeypatch.setattr(lifecycle_guard, "_MAX_REFERENCED_SCRIPT_BYTES", 4096)
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_BYTES", 4096)
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_LINE_BYTES", 4096)
    monkeypatch.setenv("LIFECYCLE_GUARD_REFUSAL_LEDGER", str(tmp_path / "refusals.jsonl"))
    return tmp_path


def _data_file(path: Path, size: int = 4096, first: bytes = b'{"at": ') -> Path:
    path.write_bytes(first + b"x" * (size - len(first)))
    return path


def _script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _runner(tmp_path: Path, mentions: list[str], *, option: str = "") -> Path:
    """A script that NAMES *mentions* the way real code does - an assignment value, which is
    exactly the shape gate.py:147 has (`DEFAULT_LEDGER = "/opt/.../deploy-ledger.jsonl"`) - and,
    if *option* is set, passes the first mention as an option value (`--ledger <path>`)."""
    lines = ["#!/bin/sh", str(mentions[0])]
    lines += ["# mentioned in prose too: %s" % m for m in mentions[1:]]
    if option:
        lines.append('echo "%s %s"' % (option, mentions[0]))
    lines.append("exit 0")
    path = tmp_path / "runner.sh"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)
    return path


# --- the incident -----------------------------------------------------------------------------

def test_a_large_data_file_MENTIONED_in_a_script_does_not_refuse(tmp_path, monkeypatch):
    """The 34 MB deploy-ledger shape: named inside the code the command runs, never executed."""
    ledger = _data_file(tmp_path / "deploy-ledger.jsonl", 8192)
    runner = _runner(tmp_path, [str(ledger)])

    assert guard("./runner.sh", cwd=str(tmp_path)) is False
    assert lifecycle_guard.scan_gateway_lifecycle(
        "./runner.sh", cwd=str(tmp_path))[1] is None


def test_a_large_data_file_passed_as_an_OPTION_VALUE_does_not_refuse(tmp_path):
    """`gate.py --admit-rev ... --ledger <40 MB>` inside a scanned script: data, not code."""
    ledger = _data_file(tmp_path / "deploy-ledger.jsonl", 8192)
    _runner(tmp_path, [str(ledger)], option="--ledger")

    assert guard("./runner.sh", cwd=str(tmp_path)) is False


def test_the_data_file_is_not_read_past_its_head(tmp_path, monkeypatch):
    """Data is never scanned - so it is never even charged against the walk budget."""
    ledger = _data_file(tmp_path / "deploy-ledger.jsonl", 8192)
    _runner(tmp_path, [str(ledger)])

    probes: list[str] = []
    real = lifecycle_guard._read_referenced_script

    def traced(script_path, max_bytes=None):
        probes.append(str(script_path))
        return real(script_path, max_bytes=max_bytes)

    monkeypatch.setattr(lifecycle_guard, "_read_referenced_script", traced)
    assert guard("./runner.sh", cwd=str(tmp_path)) is False
    assert not [p for p in probes if p.endswith("deploy-ledger.jsonl")], probes


# --- the cap still bites where it should ------------------------------------------------------

def test_an_oversized_SCRIPT_still_fails_closed_BY_NAME(tmp_path):
    """A real script (shebang + exec bit) that is over the cap is still refused, and named."""
    big = tmp_path / "huge.sh"
    big.write_text("#!/bin/sh\n" + "# pad\n" * 1200, encoding="utf-8")
    big.chmod(0o755)
    _runner(tmp_path, [str(big)])

    unsafe, refusal = lifecycle_guard.scan_gateway_lifecycle("./runner.sh", cwd=str(tmp_path))
    assert unsafe is True
    assert refusal and str(big) in refusal, refusal


def test_an_oversized_file_with_only_the_exec_bit_is_a_script(tmp_path):
    """No shebang, but executable: the mode is the file's own claim, so it is scanned."""
    big = tmp_path / "compiled-ish"
    big.write_text("x" * 4096, encoding="utf-8")
    big.chmod(0o755)
    _runner(tmp_path, [str(big)])

    unsafe, refusal = lifecycle_guard.scan_gateway_lifecycle("./runner.sh", cwd=str(tmp_path))
    assert unsafe is True and refusal and str(big) in refusal, refusal


def test_an_oversized_COMMAND_WORD_script_still_fails_closed(tmp_path):
    """Depth 0 is unchanged: the script the user actually runs answers for its size."""
    big = tmp_path / "big.sh"
    big.write_text("#!/bin/sh\n" + "# pad\n" * 1200, encoding="utf-8")
    big.chmod(0o755)

    unsafe, refusal = lifecycle_guard.scan_gateway_lifecycle("./big.sh", cwd=str(tmp_path))
    assert unsafe is True and refusal and str(big) in refusal, refusal


def test_a_plain_data_file_beside_the_command_changes_nothing(tmp_path):
    """The pre-incident behaviour that must NOT regress: no references, no refusal."""
    _data_file(tmp_path / "deploy-ledger.jsonl", 8192)
    assert guard("echo hello", cwd=str(tmp_path)) is False


# --- a refusal leaves a row -------------------------------------------------------------------

def test_a_refusal_writes_a_row_the_audit_can_read(tmp_path):
    big = tmp_path / "huge.sh"
    big.write_text("#!/bin/sh\n" + "# pad\n" * 1200, encoding="utf-8")
    big.chmod(0o755)
    _runner(tmp_path, [str(big)])

    assert guard("./runner.sh", cwd=str(tmp_path)) is True

    ledger = Path(tmp_path / "refusals.jsonl")
    assert ledger.is_file(), "a refusal left no row"
    rows = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["kind"] == "lifecycle-guard-refusal"
    assert row["path"] == str(big)
    assert str(big) in row["reason"]
    assert "./runner.sh" in row["command"]
    assert row["at"] and row["pid"]


def test_the_row_writer_never_changes_the_verdict(tmp_path, monkeypatch):
    """A guard that cannot write its row must still answer its question."""
    monkeypatch.setenv("LIFECYCLE_GUARD_REFUSAL_LEDGER", "/proc/definitely/not/writable.jsonl")
    big = tmp_path / "huge.sh"
    big.write_text("#!/bin/sh\n" + "# pad\n" * 1200, encoding="utf-8")
    big.chmod(0o755)
    _runner(tmp_path, [str(big)])

    assert guard("./runner.sh", cwd=str(tmp_path)) is True


def test_a_child_EXECUTED_by_an_interpreter_still_answers_for_its_size(tmp_path):
    """`bash child.sh` IS an execution even when the child has no shebang and no exec bit:
    the mention rule must not become a hole a wrapper can hide an oversized script in."""
    child = tmp_path / "child.sh"
    child.write_text("echo step\n" * 1200, encoding="utf-8")  # no shebang, mode 644
    hub = tmp_path / "hub.sh"
    hub.write_text("#!/bin/sh\nbash %s\nexit 0\n" % child, encoding="utf-8")
    hub.chmod(0o755)

    unsafe, refusal = lifecycle_guard.scan_gateway_lifecycle("./hub.sh", cwd=str(tmp_path))
    assert unsafe is True and refusal and str(child) in refusal, refusal


def test_an_allowed_command_writes_no_row(tmp_path):
    _runner(tmp_path, [str(_data_file(tmp_path / "deploy-ledger.jsonl", 8192))])
    assert guard("./runner.sh", cwd=str(tmp_path)) is False
    assert not (tmp_path / "refusals.jsonl").exists()
