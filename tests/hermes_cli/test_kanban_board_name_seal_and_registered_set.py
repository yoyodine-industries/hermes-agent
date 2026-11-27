"""The operator door's default board, the board NAME field, and the seal as a FIELD.

Card t_62917d87. Three defects, one class:

* a refusal for an unregistered board told the caller to run a CLI and never said what IS
  registered — the remedy was the caller's homework, and the *default* that produced it
  could not be checked until the card was already composed;
* the ``default`` board stored an operator DIRECTIVE
  (``"SEALED - not a fleet board (operator directive 2026-11-04)"``, 60 characters) in
  ``board.json:name``, so ``hermes kanban boards list`` printed policy prose in the NAME
  column and every reader of the register — dashboard, tool list, operator — was handed a
  sentence where a display label belongs;
* the name field normalized instead of validating, so an empty string silently became the
  title-cased slug and prose of any length was stored verbatim.

The fix under test: :func:`kanban_db.unregistered_board_refusal` prints the REGISTERED SET
read from the register at the moment of refusal; ``name`` is validated on write and REFUSES
(never truncates) an empty value, a line break, or prose longer than
:data:`kanban_db_boards.MAX_BOARD_NAME_LEN`; the seal lives in ``sealed`` + ``sealed_reason``
and renders as a marker beside a name, never as the name.

Nothing here registers ``migrations`` or ``_archived``: the discriminator for those is their
CONTENT and a declared owner, and a test that registered them would be the repair script the
card forbids.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# The worktree under test, not the live install, must win the import.
_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_boards as kbb

#: The exact string the live ``default`` board carried in its name field (measured
#: 2026-11-13, 60 characters). Named here once so the refusal is tested against the real
#: defect rather than against a made-up long string.
LIVE_POLICY_NAME = "SEALED - not a fleet board (operator directive 2026-11-04)"


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with no prior kanban state (same contract as test_kanban_boards)."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants

        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    return home


def _board_json(home: Path, slug: str) -> Path:
    return home / "kanban" / "boards" / slug / "board.json"


def _cli(args: list[str], home: Path) -> subprocess.CompletedProcess:
    """Run ``hermes kanban …`` in this worktree, unpinned, against ``home``."""
    env = dict(os.environ)
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_DELEGATED_CHILD_CONTEXT",
    ):
        env.pop(var, None)
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = str(_WORKTREE)
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban"] + args,
        env=env, capture_output=True, text=True, timeout=180,
    )


# ---------------------------------------------------------------------------
# §2 — the NAME field validates, it does not truncate
# ---------------------------------------------------------------------------

class TestNameFieldRefuses:
    def test_live_policy_sentence_is_refused_and_nothing_is_created(self, fresh_home):
        with pytest.raises(ValueError) as exc:
            kb.create_board("estate", name=LIVE_POLICY_NAME)
        message = str(exc.value)
        # The refusal states the cap, the length, and WHERE the prose belongs.
        assert str(kbb.MAX_BOARD_NAME_LEN) in message
        assert str(len(LIVE_POLICY_NAME)) in message
        assert "boards seal" in message and "sealed" in message
        assert not _board_json(fresh_home, "estate").exists()

    @pytest.mark.parametrize("name", ["", "   ", "\t\n"])
    def test_empty_or_whitespace_name_is_refused(self, fresh_home, name):
        with pytest.raises(ValueError) as exc:
            kb.create_board("estate", name=name)
        assert "empty" in str(exc.value)
        assert not _board_json(fresh_home, "estate").exists()

    def test_a_line_break_is_refused(self, fresh_home):
        with pytest.raises(ValueError) as exc:
            kb.create_board("estate", name="Estate\nSEALED - do not use")
        assert "line" in str(exc.value)

    def test_a_normal_name_is_stored_verbatim(self, fresh_home):
        meta = kb.create_board("estate", name=" Dispatch Ops - fleet-stall sink ")
        # Surrounding whitespace is trimmed; interior text is untouched, never truncated.
        assert meta["name"] == "Dispatch Ops - fleet-stall sink"
        assert json.loads(_board_json(fresh_home, "estate").read_text())["name"] == \
            "Dispatch Ops - fleet-stall sink"

    def test_rename_refuses_rather_than_writing_a_shortened_name(self, fresh_home):
        kb.create_board("estate", name="Estate")
        with pytest.raises(ValueError):
            kb.write_board_metadata("estate", name=LIVE_POLICY_NAME)
        # The stored name is UNCHANGED — a refused write is not a partial write.
        assert kb.read_board_metadata("estate")["name"] == "Estate"


# ---------------------------------------------------------------------------
# §2 — a seal is a FIELD, and the name column stays a name
# ---------------------------------------------------------------------------

class TestSealIsAField:
    def test_seal_round_trip_keeps_the_directive_out_of_the_name(self, fresh_home):
        kb.create_board("estate", name="Estate")
        meta = kb.write_board_metadata(
            "estate", sealed=True, sealed_reason=LIVE_POLICY_NAME)
        assert meta["name"] == "Estate"
        assert meta["sealed"] is True
        assert meta["sealed_reason"] == LIVE_POLICY_NAME
        stored = json.loads(_board_json(fresh_home, "estate").read_text())
        assert stored["name"] == "Estate"
        assert stored["sealed"] is True
        assert stored["sealed_reason"] == LIVE_POLICY_NAME

    def test_seal_does_not_change_dispatch_admission(self, fresh_home):
        """A seal is a declaration; ``dispatch: false`` is the flag the dispatcher reads."""
        kb.create_board("estate", name="Estate")
        kb.write_board_metadata("estate", sealed=True, sealed_reason="operator directive")
        assert kbb.board_dispatch_enabled("estate") is True

    def test_unseal_clears_the_flag_and_the_reason(self, fresh_home):
        kb.create_board("estate", name="Estate", sealed=True, sealed_reason="directive")
        meta = kb.write_board_metadata("estate", sealed=False, sealed_reason="")
        assert meta["sealed"] is False
        assert "sealed_reason" not in json.loads(_board_json(fresh_home, "estate").read_text())

    def test_an_unrelated_write_leaves_the_seal_alone(self, fresh_home):
        kb.create_board("estate", name="Estate", sealed=True, sealed_reason="directive")
        kb.write_board_metadata("estate", description="something else")
        meta = kb.read_board_metadata("estate")
        assert meta["sealed"] is True and meta["sealed_reason"] == "directive"

    def test_list_prints_the_name_and_a_seal_marker(self, fresh_home):
        kb.create_board("estate", name="Estate", sealed=True, sealed_reason=LIVE_POLICY_NAME)
        kb.create_board("plain", name="Plain")
        out = _cli(["boards", "list"], fresh_home)
        assert out.returncode == 0, out.stderr
        # Every registered board prints a NAME; the seal is MARKED beside it.
        assert "Estate [sealed]" in out.stdout
        assert "Plain" in out.stdout
        # The policy sentence is NOT in the name column.
        for line in out.stdout.splitlines():
            if "SEALED - not a fleet board" in line:
                pytest.fail(f"the directive is printed as a name: {line!r}")


# ---------------------------------------------------------------------------
# §1 — the refusal is at the resolver, and it PRINTS THE REGISTERED SET
# ---------------------------------------------------------------------------

class TestRegisteredSetIsTheRemedy:
    def test_refusal_prints_registered_slugs_and_the_remedy(self, fresh_home):
        kb.create_board("alpha", name="Alpha")
        kb.create_board("beta", name="Beta")
        with pytest.raises(kb.BoardResolutionError) as exc:
            kb._require_registered_board("defcon")
        message = str(exc.value)
        assert "Registered boards:" in message
        for slug in ("default", "alpha", "beta"):
            assert slug in message
        # The caller still gets the registration remedy — now beside the set.
        assert "hermes kanban boards create defcon" in message
        # ... and the door does NOT file elsewhere on the caller's behalf.
        assert "will NOT file elsewhere" in message
        assert "defcon" in message

    def test_the_set_is_read_at_refusal_time_not_import_time(self, fresh_home):
        """A board registered after import appears in the very next refusal."""
        with pytest.raises(kb.BoardResolutionError) as first:
            kb._require_registered_board("ghost")
        assert "latecomer" not in str(first.value)
        kb.create_board("latecomer", name="Latecomer")
        with pytest.raises(kb.BoardResolutionError) as second:
            kb._require_registered_board("ghost")
        assert "latecomer" in str(second.value)

    def test_an_archived_record_is_not_offered_as_a_filing_target(self, fresh_home):
        kb.create_board("retired", name="Retired")
        kb.remove_board("retired", archive=True)
        with pytest.raises(kb.BoardResolutionError) as exc:
            kb._require_registered_board("ghost")
        assert "retired" not in str(exc.value)

    def test_a_board_directory_without_board_json_is_not_registered(self, fresh_home):
        """The migration corpse shape: a real store and NO board.json is not a board."""
        orphan = fresh_home / "kanban" / "boards" / "orphan"
        orphan.mkdir(parents=True)
        (orphan / "kanban.db").write_bytes(b"SQLite format 3\x00")
        with pytest.raises(kb.BoardResolutionError) as exc:
            kb._require_registered_board("orphan")
        assert "orphan" in str(exc.value)
        assert "Registered boards:" in str(exc.value)


# ---------------------------------------------------------------------------
# Acceptance: registration writes board.json only — the store's bytes are untouched
# ---------------------------------------------------------------------------

class TestRegistrationDoesNotTouchTheStore:
    def test_registering_an_existing_store_changes_no_store_bytes(self, fresh_home):
        kb.create_board("estate", name="Estate")
        store = kb.kanban_db_path(board="estate")
        before = hashlib.sha256(store.read_bytes()).hexdigest()
        # The registration path: board.json only (the ops seat's 2026-11-13 repair of
        # `defcon` was exactly this call, and the store's sha256 did not move).
        meta = kb.create_board("estate", name="Estate (Reworked)")
        after = hashlib.sha256(store.read_bytes()).hexdigest()
        assert meta["name"] == "Estate (Reworked)"
        assert after == before, "a registration/rename must not rewrite the task store"


# ---------------------------------------------------------------------------
# CLI: the refusals reach the operator with a non-zero exit
# ---------------------------------------------------------------------------

class TestCliRefusals:
    def test_boards_create_refuses_policy_prose_with_exit_2(self, fresh_home):
        out = _cli(["boards", "create", "estate", "--name", LIVE_POLICY_NAME], fresh_home)
        assert out.returncode == 2, out.stdout + out.stderr
        assert "boards seal" in (out.stdout + out.stderr)
        assert not _board_json(fresh_home, "estate").exists()

    def test_boards_seal_and_unseal_verbs(self, fresh_home):
        kb.create_board("estate", name="Estate")
        sealed = _cli(["boards", "seal", "estate", "--reason", LIVE_POLICY_NAME], fresh_home)
        assert sealed.returncode == 0, sealed.stderr
        meta = kb.read_board_metadata("estate")
        assert meta["sealed"] is True and meta["sealed_reason"] == LIVE_POLICY_NAME
        assert meta["name"] == "Estate"
        unsealed = _cli(["boards", "unseal", "estate"], fresh_home)
        assert unsealed.returncode == 0, unsealed.stderr
        assert kb.read_board_metadata("estate")["sealed"] is False
