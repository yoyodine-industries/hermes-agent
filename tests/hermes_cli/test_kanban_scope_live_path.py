"""The deterministic scope gate ON THE LIVE TRIAGE PATH (specify / decompose).

Card t_45aa1bc5, DoD 2. The gate itself (``hermes_cli.kanban_scope``) is a pure
function; THIS file is the part that has to hold on the live line: an OVERSIZE
declared scope is refused by the very verbs the board actually runs
(``specify_task`` / ``decompose_task`` — the CLI sweep, the dashboard route and
the gateway ``auto_decompose`` tick all land on these two), while a
within-budget card still takes the ordinary path.

Negative control (pre-fix revision): at HEAD before this change
``hermes_cli.kanban_scope`` does not exist, so every test here fails at import
(``ModuleNotFoundError``) — the gate is provably absent, not merely unused.
"""
from __future__ import annotations

import json as jsonlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_decompose as decomp
from hermes_cli import kanban_specify as spec
from hermes_cli.kanban_scope import scope_refusal_reason

THRESHOLD_ENV = "HERMES_KANBAN_SCOPE_THRESHOLD"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv(THRESHOLD_ENV, raising=False)
    kb.init_db()
    return home


def _scope_dir(tmp_path: Path, name: str, n: int) -> Path:
    d = tmp_path / name
    d.mkdir()
    for i in range(n):
        (d / f"f{i}.py").write_text("x")
    return d


def _triage(conn, body: str) -> str:
    return kb.create_task(conn, title="rough idea", body=body, triage=True)


def _aux_response(content: str):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


# ---------------------------------------------------------------------------
# specify_task
# ---------------------------------------------------------------------------


def test_specify_refuses_oversize_scope(kanban_home, tmp_path, monkeypatch):
    monkeypatch.setenv(THRESHOLD_ENV, "2")
    big = _scope_dir(tmp_path, "big", 5)
    with kbc.connect() as conn:
        tid = _triage(conn, f"Please do the thing.\n\nSCOPE: 5 files under {big}\n")

    mock_fn = MagicMock(return_value=_aux_response("{}"))
    with patch("agent.auxiliary_client.call_llm", mock_fn):
        outcome = spec.specify_task(tid, author="t")

    assert outcome.ok is False
    assert "scope gate" in outcome.reason
    assert "5 files" in outcome.reason and "threshold 2" in outcome.reason
    # Refused BEFORE the aux LLM is spent on a card that must be split first.
    mock_fn.assert_not_called()

    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "triage"


def test_specify_within_budget_control(kanban_home, tmp_path, monkeypatch):
    monkeypatch.setenv(THRESHOLD_ENV, "30")
    small = _scope_dir(tmp_path, "small", 2)
    with kbc.connect() as conn:
        tid = _triage(conn, f"Please do the thing.\n\nSCOPE: 2 files under {small}\n")

    content = jsonlib.dumps({"title": "Refined", "body": "**Goal**\nA goal."})
    mock_fn = MagicMock(return_value=_aux_response(content))
    with patch("agent.auxiliary_client.call_llm", mock_fn):
        outcome = spec.specify_task(tid, author="t")

    assert outcome.ok is True, outcome.reason
    assert mock_fn.call_count == 1


def test_specify_without_scope_line_is_not_refused(kanban_home):
    """No SCOPE line is flagged by the gate but never wedges the live path."""
    with kbc.connect() as conn:
        tid = _triage(conn, "no scope declaration at all")

    content = jsonlib.dumps({"title": "Refined", "body": "**Goal**\nA goal."})
    mock_fn = MagicMock(return_value=_aux_response(content))
    with patch("agent.auxiliary_client.call_llm", mock_fn):
        outcome = spec.specify_task(tid, author="t")

    assert outcome.ok is True, outcome.reason


# ---------------------------------------------------------------------------
# decompose_task
# ---------------------------------------------------------------------------


def test_decompose_refuses_oversize_scope(kanban_home, tmp_path, monkeypatch):
    monkeypatch.setenv(THRESHOLD_ENV, "3")
    big = _scope_dir(tmp_path, "big2", 7)
    with kbc.connect() as conn:
        tid = _triage(conn, f"Split me.\n\nSCOPE: 7 files under {big}\n")

    mock_fn = MagicMock(return_value=_aux_response("{}"))
    with patch("agent.auxiliary_client.call_llm", mock_fn):
        outcome = decomp.decompose_task(tid, author="t")

    assert outcome.ok is False
    assert "scope gate" in outcome.reason
    assert "7 files" in outcome.reason and "threshold 3" in outcome.reason
    mock_fn.assert_not_called()

    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "triage"


def test_decompose_within_budget_control(kanban_home, tmp_path, monkeypatch):
    monkeypatch.setenv(THRESHOLD_ENV, "30")
    small = _scope_dir(tmp_path, "small2", 1)
    with kbc.connect() as conn:
        tid = _triage(conn, f"One unit.\n\nSCOPE: 1 files under {small}\n")

    content = jsonlib.dumps({"fanout": False, "rationale": "x", "title": "T", "body": "B"})
    mock_fn = MagicMock(return_value=_aux_response(content))
    with patch("agent.auxiliary_client.call_llm", mock_fn):
        outcome = decomp.decompose_task(tid, author="t")

    assert outcome.ok is True, outcome.reason
    assert mock_fn.call_count == 1


# ---------------------------------------------------------------------------
# the pure helper the live path consults
# ---------------------------------------------------------------------------


def test_scope_refusal_reason_only_blocks_oversize(tmp_path, monkeypatch):
    monkeypatch.setenv(THRESHOLD_ENV, "2")
    big = _scope_dir(tmp_path, "big3", 4)
    assert scope_refusal_reason(f"SCOPE: 4 files under {big}") is not None
    assert scope_refusal_reason("no scope here") is None
    assert scope_refusal_reason("SCOPE: nonsense") is None
    small = _scope_dir(tmp_path, "small3", 1)
    assert scope_refusal_reason(f"SCOPE: 1 files under {small}") is None


def test_declared_oversize_refused_when_path_unreadable(monkeypatch):
    """Path off this host: the declared count is the only signal, and it blocks."""
    monkeypatch.setenv(THRESHOLD_ENV, "30")
    reason = scope_refusal_reason("SCOPE: 500 files under /nonexistent/host/tree")
    assert reason is not None and "500 files" in reason
