"""The DURABLE consent license for the protected-instruction-file class (t_28bbf346).

The card's acceptance is a LIVE one (the AGENTS.md patch applies under an approved row with
no human at a keyboard); these are its unit-level preconditions. Each test names which of the
card's (a)-(e) it is.

PROVENANCE IS THE POINT. The license keys on the card the SESSION runs under, so the gate
tests below put this process where the dispatcher puts a worker (``HERMES_KANBAN_TASK``) and
none of them can license a write by naming a card in an argument -- see
``test_a_caller_named_card_can_only_demote_never_license``.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from hermes_cli import protected_instruction_license as lic
from tools import file_tools_write_guards as guards

REPO = "yaan-trading-strategy"
OTHER_REPO = "yaan-cli"
CARD = "t_8f06c301"          # the correction card the write is done under
OTHER_CARD = "t_82485707"    # the card APR-0756 was filed against (the live failure)
TOKEN = f"project-instruction-file:{REPO}"


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    """A throwaway home, an empty approvals path, and this process under the correction card.

    Setting ``HERMES_KANBAN_TASK`` here IS the mechanism: it is what the dispatcher sets when
    it spawns a worker, and it is the only way any test in this file can be licensed.
    """
    for name in ("KANBAN_APPROVALS_DB", "HERMES_KANBAN_DB", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    monkeypatch.setenv("KANBAN_APPROVALS_DB", str(tmp_path / "approvals.db"))
    monkeypatch.setenv("HERMES_KANBAN_TASK", CARD)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, text=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"})


def _repo(tmp_path: Path, name: str = REPO, *, origin: str | None = None,
          dirname: str | None = None) -> Path:
    """A real git checkout whose ``origin`` names ``name`` -- the token's repo half."""
    root = tmp_path / "work" / (dirname or name)
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    if origin is None:
        origin = f"git@github.com:yoyodine/{name}.git"
    _git(root, "remote", "add", "origin", origin)
    (root / "AGENTS.md").write_text("# agents\n")
    return root


def _store(tmp_path: Path, rows: list[tuple]) -> str:
    """``rows`` are ``(id, status, idempotency_key, card_id)``; returns the store path."""
    path = tmp_path / "approvals.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE approvals (id INTEGER PRIMARY KEY, project TEXT DEFAULT '', "
        "status TEXT, decision TEXT, reason TEXT, task TEXT DEFAULT '', "
        "context TEXT DEFAULT '', idempotency_key TEXT, card_id TEXT, board TEXT, "
        "superseded_by TEXT)"
    )
    con.executemany(
        "INSERT INTO approvals (id, status, decision, reason, task, context, "
        "idempotency_key, card_id) VALUES (?, ?, ?, '', '', '', ?, ?)",
        [(rid, status, status, key, card) for rid, status, key, card in rows],
    )
    con.commit()
    con.close()
    return str(path)


def _approved(card: str = CARD, scope: str = TOKEN, rid: int = 756) -> list[tuple]:
    return [(rid, "approved", f"platform\x1f{scope}", card)]


# --- the token --------------------------------------------------------------


def test_the_repo_half_survives_every_spelling_a_remote_takes():
    for spelling in (
        "git@github.com:yoyodine/yaan-trading-strategy.git",
        "https://github.com/yoyodine/yaan-trading-strategy",
        "/opt/hermes_sandbox/yaan-trading-strategy",
        "yaan-trading-strategy.git",
    ):
        assert lic.normalise_repo(spelling) == REPO, spelling
    assert lic.scope_token(REPO) == TOKEN


def test_an_unidentifiable_repo_yields_no_token():
    assert lic.normalise_repo("") == ""
    assert lic.scope_token("") == ""


def test_the_repo_identity_is_the_remotes_not_the_worktree_directory(tmp_path):
    """The live case: the patch is written in ``ts-t27f67857``, the repo is the remote's."""
    repo = _repo(tmp_path, dirname="ts-t27f67857")
    assert lic.repo_identity(repo / "AGENTS.md") == REPO
    assert lic.repo_identity(repo / "AGENTS.md") != "ts-t27f67857"


def test_a_file_that_does_not_exist_yet_resolves_through_its_parent(tmp_path):
    repo = _repo(tmp_path)
    assert lic.repo_identity(repo / "CLAUDE.md") == REPO


def test_a_path_outside_any_repository_has_no_identity(tmp_path):
    loose = tmp_path / "loose"
    loose.mkdir()
    (loose / "AGENTS.md").write_text("x\n")
    assert lic.repo_identity(loose / "AGENTS.md") is None


# --- the card's provenance --------------------------------------------------


def test_the_live_card_is_read_from_the_session_not_an_argument(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", CARD)
    assert lic.live_card_id() == CARD
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    assert lic.live_card_id() == ""


def test_a_session_with_no_card_cannot_be_licensed_by_an_argument(tmp_path, monkeypatch):
    """No live card at all: an approved row plus a card argument still licenses nothing."""
    _store(tmp_path, _approved(card=CARD))
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    repo = _repo(tmp_path)
    verdict, card = lic.assess([repo / "AGENTS.md"], declared=CARD)
    assert (verdict.licensed, verdict.cause, card) == (False, "no_card", "")


def test_a_caller_named_card_can_only_demote_never_license(tmp_path):
    """THE PROVENANCE CHECK. The license keys on the session's card, not on an argument.

    The approved row is filed under ``OTHER_CARD`` while this process runs under ``CARD``: a
    caller that hands the gate ``OTHER_CARD`` must not be licensed by it, and must not reach
    the prompt either -- it is REFUSED, and the refusal names both cards.
    """
    _store(tmp_path, _approved(card=OTHER_CARD))
    repo = _repo(tmp_path)
    refusal = guards._request_protected_instruction_approval(
        ["AGENTS.md"], OTHER_CARD, paths=[str(repo / "AGENTS.md")])
    assert refusal and "BLOCKED" in refusal
    assert "the caller named" in refusal
    assert f"the write is being done under {CARD}" in refusal
    # the inverse: ``default`` (what the tool layer passes for a session with no claim) neither
    # licenses nor breaks anything -- this write has no approved row, so it still refuses.
    assert guards._request_protected_instruction_approval(
        ["AGENTS.md"], "default", paths=[str(repo / "AGENTS.md")]) is not None


# --- the license, unit level ------------------------------------------------


def test_card_no_means_no_license(tmp_path, monkeypatch):
    _store(tmp_path, _approved())
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    repo = _repo(tmp_path)
    verdict = lic.license_for([repo / "AGENTS.md"])
    assert (verdict.licensed, verdict.cause) == (False, "no_card")


def test_a_pending_row_never_licenses(tmp_path):
    _store(tmp_path, [(756, "pending", f"platform\x1f{TOKEN}", CARD)])
    repo = _repo(tmp_path)
    verdict = lic.license_for([repo / "AGENTS.md"])
    assert (verdict.licensed, verdict.cause) == (False, "no_approved_row")


def test_a_superseded_approved_row_does_not_license(tmp_path):
    path = _store(tmp_path, _approved())
    con = sqlite3.connect(path)
    con.execute("UPDATE approvals SET superseded_by = 'APR-0900' WHERE id = 756")
    con.commit()
    con.close()
    repo = _repo(tmp_path)
    verdict = lic.license_for([repo / "AGENTS.md"])
    assert verdict.licensed is False


def test_one_license_does_not_cover_a_second_repo_in_the_same_patch(tmp_path):
    """Ruling 4: one approval licenses ONE file class in ONE repo -- not a patch's second."""
    _store(tmp_path, _approved())
    first = _repo(tmp_path)
    second = _repo(tmp_path, name=OTHER_REPO)
    verdict = lic.license_for([first / "AGENTS.md", second / "AGENTS.md"])
    assert verdict.licensed is False
    assert verdict.tokens == (f"project-instruction-file:{OTHER_REPO}",)


def test_an_unreadable_store_fails_closed(tmp_path):
    repo = _repo(tmp_path)
    (tmp_path / "approvals.db").write_bytes(b"not a database")
    verdict = lic.license_for([repo / "AGENTS.md"])
    assert (verdict.licensed, verdict.cause) == (False, "store_unreadable")


# --- the gate, at the site the card names -----------------------------------


def test_a_an_approved_row_on_this_card_licenses_the_write(tmp_path):
    """(a) approved row, card_id == the working card, matching scope -> LICENSED."""
    _store(tmp_path, _approved())
    repo = _repo(tmp_path)
    assert guards._request_protected_instruction_approval(
        ["AGENTS.md"], CARD, paths=[str(repo / "AGENTS.md")]) is None
    # and through the gate itself, reason + path matching included
    assert guards._check_protected_instruction_write([str(repo / "AGENTS.md")], CARD) is None


def test_b_the_same_row_under_a_different_card_does_not_license(tmp_path):
    """(b) THE LIVE FAILURE: APR-0756 carries another card than the correction card."""
    _store(tmp_path, _approved(card=OTHER_CARD))
    repo = _repo(tmp_path)
    refusal = guards._request_protected_instruction_approval(
        ["AGENTS.md"], CARD, paths=[str(repo / "AGENTS.md")])
    assert refusal and "BLOCKED" in refusal
    assert TOKEN in refusal                       # names the scope to file
    assert f"card_id = {CARD}" in refusal         # names the card the row must carry
    assert OTHER_CARD not in refusal


def test_c_a_pending_row_never_licenses_through_the_gate(tmp_path):
    """(c) pending never licenses, however well it matches."""
    _store(tmp_path, [(756, "pending", f"platform\x1f{TOKEN}", CARD)])
    repo = _repo(tmp_path)
    refusal = guards._request_protected_instruction_approval(
        ["AGENTS.md"], CARD, paths=[str(repo / "AGENTS.md")])
    assert refusal and "BLOCKED" in refusal
    assert "none of them is approved" in refusal


def test_d_an_approved_row_scoped_to_another_repo_does_not_license_this_one(tmp_path):
    """(d) scope names another repo -> refused, and the refusal says why."""
    _store(tmp_path, _approved(scope=f"project-instruction-file:{OTHER_REPO}"))
    repo = _repo(tmp_path)
    refusal = guards._request_protected_instruction_approval(
        ["AGENTS.md"], CARD, paths=[str(repo / "AGENTS.md")])
    assert refusal and "BLOCKED" in refusal
    assert "narrower or different scope" in refusal
    assert TOKEN in refusal


def test_e_no_row_at_all_refuses_and_names_the_row_shape_to_file(tmp_path):
    """(e) nothing filed -> refused, and the refusal is filable as-is."""
    _store(tmp_path, [])
    repo = _repo(tmp_path)
    refusal = guards._request_protected_instruction_approval(
        ["AGENTS.md"], CARD, paths=[str(repo / "AGENTS.md")])
    assert refusal and "BLOCKED" in refusal
    assert "holds NO row whose card_id is this card" in refusal
    assert f"card_id = {CARD}" in refusal
    assert "status  = approved" in refusal
    assert f"scope   = {TOKEN}" in refusal


def test_the_broad_surface_scope_licenses_any_repo(tmp_path):
    """Ruling 3: the broad ``project-instruction-file`` scope is acceptable."""
    _store(tmp_path, _approved(scope="project-instruction-file"))
    repo = _repo(tmp_path, name=OTHER_REPO)
    assert guards._request_protected_instruction_approval(
        ["AGENTS.md"], CARD, paths=[str(repo / "AGENTS.md")]) is None


def test_a_row_with_no_key_licenses_only_the_full_token_in_its_text(tmp_path):
    """A pre-vocabulary row declares its scope in prose; the FULL token, never the bare one."""
    path = _store(tmp_path, [(756, "approved", "", CARD)])
    repo = _repo(tmp_path)
    verdict = lic.license_for([repo / "AGENTS.md"])
    assert (verdict.licensed, verdict.cause) == (False, "scope_not_covered")

    con = sqlite3.connect(path)
    con.execute("UPDATE approvals SET reason = ? WHERE id = 756",
                (f"operator consented to one write under {TOKEN}",))
    con.commit()
    con.close()
    assert lic.license_for([repo / "AGENTS.md"]).licensed is True


def test_the_license_never_prompts_and_never_asks_for_a_channel(tmp_path):
    """The licensed road returns before the prompt machinery is even imported."""
    _store(tmp_path, _approved())
    repo = _repo(tmp_path)
    assert guards._check_protected_instruction_write([str(repo / "AGENTS.md")], CARD) is None
