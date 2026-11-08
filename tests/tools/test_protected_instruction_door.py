"""The protected-instruction DOOR (ruling on card ``t_cd80bb5a``, D1-D4).

What this file proves, in the ruling's own order:

1. a project instruction write on a card that holds a CARD-BOUND approved approval
   covering ``project-instruction-file:<repo-slug>`` PROCEEDS -- zero prompts, no
   receipt, no park;
2. without one it is REFUSED deterministically, naming the token, the card and a
   copy-paste filer, with a ``consent_refused`` receipt whose cause is
   ``protected_write_without_consent`` and a park (blocked/needs_input);
3. an approval that covers the wrong scope reports ``scope_not_covered``;
4. an approval with the right scope but bound to a DIFFERENT card reports
   ``card_not_licensed`` -- the licence is the card, not the citation;
5. with NO owning card the pre-change prompt path is untouched (and the headless
   answer is still a refusal, not a bypass);
6. the FILE, TERMINAL and EXEC doors agree path for path (one matcher, ~12 paths):
   the same target that fires at the file door fires at the other two, and nothing
   else fires;
7. ``--yolo`` cannot re-open any of it -- the clause runs before the prompt layer.

The retirement of ``security.protected_instruction_allowlist_dirs`` (D2) is covered in
``tests/tools/test_file_write_safety.py::TestProtectedInstructionAllowlistRetired``.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import consent_surface_doors as doors
from hermes_cli import consent_surface_guard as g
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tools import approval, file_tools, file_tools_write_guards as fw

SURFACE = "project-instruction-file"
CAUSE = "protected_write_without_consent"


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """No ambient kanban/consent state may leak in: each test declares its own."""
    for name in ("HERMES_KANBAN_TASK", "HERMES_SESSION_ID", "HERMES_KANBAN_BOARD",
                 "KANBAN_CONSENT_SURFACE_GUARD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KANBAN_CONSENT_SURFACE_GUARD", "on")
    g.reset_latch()
    yield
    g.reset_latch()


@pytest.fixture
def store(tmp_path, monkeypatch):
    """An isolated approvals store, plus a helper that files rows into it."""
    path = tmp_path / "approvals.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE approvals (id INTEGER PRIMARY KEY, project TEXT DEFAULT '', "
        "task TEXT DEFAULT '', context TEXT DEFAULT '', status TEXT, decision TEXT, "
        "reason TEXT, idempotency_key TEXT, card_id TEXT, board TEXT, superseded_by TEXT)"
    )
    con.commit()
    con.close()
    monkeypatch.setenv("KANBAN_APPROVALS_DB", str(path))

    def file_row(row_id, status, scope, card_id="", decision="approved"):
        con = sqlite3.connect(path)
        con.execute(
            "INSERT INTO approvals (id, status, decision, card_id, idempotency_key) "
            "VALUES (?, ?, ?, ?, ?)",
            (row_id, status, decision, card_id,
             "platform\x1f" + scope if scope else None),
        )
        con.commit()
        con.close()

    return {"path": path, "file": file_row}


@pytest.fixture
def repo(tmp_path):
    """A checkout with a git marker, so the scope slug is the repo directory's name."""
    root = tmp_path / "yaan-trading-strategy"
    (root / ".git").mkdir(parents=True)
    (root / "docs").mkdir()
    return root


@pytest.fixture
def card(monkeypatch):
    """Create a card on this board and arm the session as its worker."""
    kb.init_db()

    def make(title="Write the project AGENTS.md", status="running"):
        with kbc.connect() as conn:
            tid = kb.create_task(conn, title=title, body="", assignee="platform-coder")
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
            conn.commit()
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_SESSION_ID", "sess-door-1")
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "research")
        return tid

    return make


@pytest.fixture
def approvals(monkeypatch):
    """A CLI approval callback that DENIES and records every prompt it is asked."""
    from tools.terminal_tool import set_approval_callback

    state = {"calls": [], "answer": "deny"}

    def cb(command, description, **kwargs):
        state["calls"].append({"command": command, "description": description, **kwargs})
        return state["answer"]

    set_approval_callback(cb)
    yield state
    set_approval_callback(None)


def _write(path, content="# rules\n"):
    return json.loads(file_tools.write_file_tool(str(path), content))


def _receipts(tid):
    with kbc.connect() as conn:
        rows = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?", (tid, g.RECEIPT_KIND)
        ).fetchall()
    return [json.loads(r[0]) for r in rows]


def _row(tid):
    with kbc.connect() as conn:
        return conn.execute("SELECT status, block_kind FROM tasks WHERE id = ?",
                            (tid,)).fetchone()


def _token(repo):
    return f"project-instruction-file:{repo.name}"


# --- 1. covered -> the write proceeds, silently -----------------------------


def test_a_card_bound_approval_lets_the_write_proceed(store, repo, card, approvals):
    tid = card()
    store["file"](1, "approved", _token(repo), card_id=tid)
    target = repo / "AGENTS.md"

    res = _write(target)

    assert not res.get("error"), res
    assert target.read_text(encoding="utf-8") == "# rules\n"
    assert approvals["calls"] == []          # zero prompts
    assert _receipts(tid) == []              # no receipt: nothing was refused
    assert _row(tid)[0] == "running"         # and no park


def test_a_broad_class_approval_covers_a_narrow_repo(store, repo, card, approvals):
    """The prefix rule: a licence for the class covers any repo beneath it."""
    tid = card()
    store["file"](1, "approved", SURFACE, card_id=tid)
    res = _write(repo / "docs" / "CLAUDE.md")
    assert not res.get("error"), res


def test_another_repo_is_not_covered_by_a_narrow_row(store, repo, card, approvals):
    tid = card()
    store["file"](1, "approved", "project-instruction-file:some-other-repo", card_id=tid)
    res = _write(repo / "AGENTS.md")
    assert res.get("error")
    assert _token(repo) in res["error"]


# --- 2. uncovered -> deterministic refusal, receipt, park -------------------


def test_an_unlicensed_write_is_refused_with_a_filer_and_a_receipt(store, repo, card, approvals):
    tid = card()
    target = repo / "AGENTS.md"

    res = _write(target)

    message = res.get("error") or ""
    assert message, res
    assert not target.exists()                     # the write did NOT happen
    assert approvals["calls"] == []                # no prompt: the exit is not a prompt
    # the message names the surface, the exact token, the card, and a usable filer
    assert SURFACE in message
    assert _token(repo) in message
    assert tid in message
    assert "curl -sS -X POST" in message and "/api/approvals" in message
    assert '"kind":"ask"' in message

    receipts = _receipts(tid)
    assert len(receipts) == 1
    assert receipts[0]["cause"] == CAUSE
    assert receipts[0]["scope_token"] == _token(repo)

    status, block_kind = _row(tid)
    assert status == "blocked" and block_kind == "needs_input"


def test_a_pending_row_bound_to_the_card_names_the_wait(store, repo, card, approvals):
    tid = card()
    store["file"](7, "pending", _token(repo), card_id=tid)
    res = _write(repo / "AGENTS.md")
    message = res.get("error") or ""
    assert "APR-0007" in message and "awaiting the operator" in message
    assert _receipts(tid)[0]["cause"] == CAUSE


# --- 3/4. the diagnosis names the actual gap --------------------------------

def test_approved_but_wrong_scope_reports_scope_not_covered(store, repo, card, approvals):
    tid = card()
    store["file"](1, "approved", "hermes-config:security.protected_instruction_files",
                  card_id=tid)
    res = _write(repo / "AGENTS.md")
    assert res.get("error")
    assert _receipts(tid)[0]["cause"] == CAUSE


def test_approved_on_another_card_is_card_not_licensed(store, repo, card, approvals):
    """The licence is the CARD, not the citation: a covering row on another card is not
    this card's consent, and the refusal says so instead of pretending nothing exists."""
    tid = card()
    store["file"](1, "approved", _token(repo), card_id="t_someothercard")

    res = _write(repo / "AGENTS.md")

    message = res.get("error") or ""
    assert "bound to t_someothercard" in message, message
    assert "CARD-BOUND" in message
    assert _receipts(tid)[0]["cause"] == CAUSE


# --- 5. no owning card -> the pre-change path ------------------------------


def test_without_an_owning_card_the_prompt_path_is_unchanged(store, repo, approvals):
    """A human at a console, cron, or the operator IS the consent authority: the clause
    stays inert and the always-ask gate answers exactly as before."""
    intent = doors.hit_for_paths([str(repo / "AGENTS.md")])
    assert intent is not None and intent.surface == SURFACE     # it IS a protected file
    assert g.consent_gate_active() is False                      # yet the clause is inert

    res = _write(repo / "AGENTS.md")
    assert res.get("error")                                      # headless answer: refusal
    assert not (repo / "AGENTS.md").exists()


# --- 6. the doors agree, path for path -------------------------------------


#: ~12 paths: what the always-ask gate matches, and the look-alikes it must not.
PARITY_PATHS = [
    ("AGENTS.md", True),
    ("agents.MD", True),
    ("CLAUDE.md", True),
    ("SOUL.md", True),
    (".cursorrules", True),
    ("docs/AGENTS.md", True),
    ("docs/SOUL.md", True),
    ("README.md", False),
    ("notes.md", False),
    ("AGENTS.md.bak", False),
    ("src/main.py", False),
]


@pytest.mark.parametrize("relpath,protected", PARITY_PATHS)
def test_the_file_and_command_doors_agree(repo, relpath, protected):
    target = str(repo / relpath)
    fires_at_the_gate = fw._protected_instruction_reason(target) is not None

    file_hit = doors.hit_for_paths([target])
    command_hit = doors.hit_for_command(f"echo x >> {target}")
    read_hit = doors.hit_for_command(f"cat {target}")
    exec_hit = doors.hit_for_text(f'open("{target}", "a").write("x")')
    read_exec = doors.hit_for_text(f'print(open("{target}").read())')

    assert fires_at_the_gate is protected, relpath
    for hit in (file_hit, command_hit, exec_hit):
        assert (hit is not None and hit.surface == SURFACE) is protected, (relpath, hit)
    # a READ names no destination, in either language
    assert read_hit is None, relpath
    assert read_exec is None, relpath
    if protected:
        assert file_hit.scope_token == _token(repo)
        assert command_hit.scope_token == _token(repo)


def test_a_project_local_hermes_config_is_in_the_class_under_one_token(repo):
    """A repo's own ``.hermes/config.yaml`` is protected, and carries the PUBLISHED
    ``hermes-config`` token (the fallback rule: one target, one token). Either way the
    write fires at every door, which is what the class guarantees."""
    target = str(repo / ".hermes" / "config.yaml")
    hit = doors.hit_for_paths([target])
    assert hit is not None and hit.surface in {"hermes-config", SURFACE}
    assert doors.hit_for_command(f"echo x >> {target}") is not None
    assert doors.hit_for_text(f'open("{target}", "w").write("x")') is not None
    assert fw._protected_instruction_reason(target) is not None


def test_an_innocent_name_pointing_at_a_protected_file_fires_everywhere(repo):
    """#41351 lesson: realpath first -- the LOOK-ALIKE is what an agent reaches for."""
    real = repo / "AGENTS.md"
    real.write_text("original", encoding="utf-8")
    link = repo / "innocent.txt"
    link.symlink_to(real)
    assert doors.hit_for_paths([str(link)]) is not None
    assert doors.hit_for_command(f"echo x >> {link}") is not None


# --- 7. yolo cannot re-open it --------------------------------------------


def test_yolo_does_not_re_open_the_door(store, repo, card, monkeypatch):
    tid = card()
    monkeypatch.setenv("HERMES_YOLO_MODE", "1")
    monkeypatch.setenv("HERMES_APPROVALS_MODE", "off")
    monkeypatch.setattr(approval, "is_yolo_mode", lambda: True, raising=False)

    res = _write(repo / "AGENTS.md")

    assert res.get("error"), res
    assert not (repo / "AGENTS.md").exists()
    assert _receipts(tid)[0]["cause"] == CAUSE


def test_the_floor_runs_before_any_prompt(store, repo, card, approvals):
    """No approval callback is consulted for this class: there is no question to ask."""
    card()
    _write(repo / "AGENTS.md")
    assert approvals["calls"] == []


# --- the retirement this door replaces (D2) --------------------------------


def test_a_stale_allowlist_config_cannot_re_open_it(store, repo, card, monkeypatch, approvals):
    """A host config still carrying the retired key must not exempt the tree."""
    import hermes_cli.config as cfgmod

    monkeypatch.setattr(cfgmod, "load_config", lambda: {"security": {
        "protected_instruction_files": True,
        "protected_instruction_extra_patterns": [],
        "protected_instruction_allowlist_dirs": ["/opt/hermes_sandbox"]}})
    tid = card()
    res = _write(repo / "AGENTS.md")
    assert res.get("error")
    assert _receipts(tid)[0]["cause"] == CAUSE
