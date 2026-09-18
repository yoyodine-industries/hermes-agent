"""Tests for the deterministic kanban scope gate (hermes_cli.kanban_scope)."""

import pytest

from hermes_cli.kanban_scope import (
    ScopeParseError,
    count_files,
    find_scope_line,
    parse_scope,
    scope_verdict,
)


def test_no_scope_line_returns_none():
    assert find_scope_line("just a body with no scope") is None


def test_find_scope_line_ignores_case_and_indent():
    body = "some text\n   scope: 12 files under /tmp\nmore"
    assert find_scope_line(body) == "12 files under /tmp"


def test_parse_scope_basic():
    decl = parse_scope("SCOPE: 12 files under /tmp/foo")
    assert decl.count == 12
    assert decl.path == "/tmp/foo"
    assert decl.exclusions == []


def test_parse_scope_with_exclusions():
    decl = parse_scope(
        "SCOPE: 98 files under ~/.hermes/skills, excluding .git/** and .curator_ledger.jsonl"
    )
    assert decl.count == 98
    assert decl.path == "~/.hermes/skills"
    assert decl.exclusions == [".git/**", ".curator_ledger.jsonl"]


def test_parse_scope_comma_separated_exclusions():
    decl = parse_scope("SCOPE: 5 files under /x, excluding *.pyc, __pycache__/")
    assert decl.exclusions == ["*.pyc", "__pycache__/"]


def test_parse_scope_unparseable_raises():
    with pytest.raises(ScopeParseError):
        parse_scope("SCOPE: a lot of things")


def test_parse_scope_missing_files_under_raises():
    with pytest.raises(ScopeParseError):
        parse_scope("SCOPE: 12 things")


def test_count_files_counts_recursively(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "sub" / "b.py").write_text("x")
    (tmp_path / "sub" / "c.txt").write_text("x")
    assert count_files(str(tmp_path), []) == 3


def test_count_files_applies_exclusions(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / ".git" / "objects").mkdir(parents=True)
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "keep.pyc").write_text("x")
    (tmp_path / "sub" / "drop.pyc").write_text("x")
    (tmp_path / ".git" / "config").write_text("x")
    (tmp_path / ".git" / "objects" / "deadbeef").write_text("x")
    n = count_files(str(tmp_path), [".git/**", "*.pyc"])
    assert n == 1  # only a.py


def test_count_files_missing_path_returns_neg_one():
    assert count_files("/nonexistent/definitely/not/here", []) == -1


def test_count_files_single_file():
    import os

    f = os.path.join(os.path.dirname(__file__), "__init__.py")
    assert count_files(f, []) == 1


def test_verdict_ok_within_threshold(tmp_path):
    (tmp_path / "a.py").write_text("x")
    body = f"SCOPE: 1 files under {tmp_path}"
    v = scope_verdict(body, threshold=30)
    assert v.status == "ok"
    assert v.actual == 1
    assert v.exit_code() == 0


def test_verdict_oversize(tmp_path):
    for i in range(5):
        (tmp_path / f"f{i}.py").write_text("x")
    body = f"SCOPE: 5 files under {tmp_path}"
    v = scope_verdict(body, threshold=2)
    assert v.status == "oversize"
    assert v.effective == 5
    assert v.exit_code() == 1


def test_verdict_no_scope():
    v = scope_verdict("no scope line here")
    assert v.status == "no_scope"
    assert v.exit_code() == 1


def test_verdict_unparseable():
    v = scope_verdict("SCOPE: seventy files somewhere")
    assert v.status == "unparseable"
    assert v.exit_code() == 1


def test_verdict_missing_path_falls_back_to_declared():
    # Path cannot be read, so the declared count is the only signal.
    v = scope_verdict("SCOPE: 500 files under /nonexistent/host/path", threshold=30)
    assert v.status == "oversize"
    assert v.actual is None
    assert v.effective == 500


def test_verdict_actual_beats_declared(tmp_path):
    # Declared 500, but only 2 files on disk -> within threshold.
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "b.py").write_text("x")
    body = f"SCOPE: 500 files under {tmp_path}"
    v = scope_verdict(body, threshold=30)
    assert v.status == "ok"
    assert v.actual == 2
    assert v.effective == 2
