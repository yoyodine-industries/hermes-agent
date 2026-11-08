"""Board-store resolution: a board NAME resolves to exactly ONE store, or refuses.

The measured defect (2026-09-27, card t_d867ddbd): ``kanban_db_path()`` consulted the
``HERMES_KANBAN_DB`` path pin BEFORE the ``board=`` argument, so with a worker's pin set
every registered board — and an unregistered slug — resolved to the ONE pinned store
(``boards/defcon/kanban.db``). Requirement: an explicitly named board always resolves to
its OWN store, and a miss is never dressed up as the current/default board's store.

RULING (card t_c21ea472, 2026-11-06, platform-stl — DESIGN AUTHORITY). The original
requirement also said an unregistered name must be REFUSED BY THE LAYOUT RESOLVER. That
half is withdrawn, and the layer split is now the rule:

* LAYOUT (this module's ``kanban_db_path``/``_board_path``) answers "where WOULD this
  board's store be". It is registration-agnostic BY NECESSITY: ``create_board`` derives
  ``board.json``'s ``db_path`` through it (``read_board_metadata``) BEFORE that file
  exists, so a refusing layout resolver cannot register a board at all — measured, not
  argued: routing ``assert_board_registered`` through ``_board_path`` produced 42 failed
  + 21 errors across four kanban test modules, the root of it every ``create_board``.
* EXISTENCE is the CLI's own question before dispatch (``hermes_cli/kanban.py``:
  ``board_exists`` -> ``board '<x>' does not exist``).
* REGISTRATION is the doors' question: the filing seam (``board_for_fired_card``), the
  model-facing resolver (``tools.kanban_tools._board`` -> ``assert_board_registered``)
  and the creating seam (``kanban_db_connect._refuse_dead_board_resurrection``, which
  refuses to mint a store for an unregistered slug).

So these tests pin the PROPERTY the incident was about — a name nobody registered never
becomes an estate, and no door substitutes a different board — not the abandoned
mechanism. ``test_create_board_still_registers`` is the ratchet: re-plumbing registration
into the layout resolver fails it first, with the reason in its assertion text.


The cards below are a STAND-IN for the card fixtures the board layout normally carries;
the resolution logic never consults them (it is filesystem-only, so ``hermes kanban
boards`` works before ``kanban init``).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Ensure the worktree (not the stale global clone) is first on sys.path.
_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb  # noqa: E402


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with no prior kanban state and no ambient pins."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_ATTACHMENTS_ROOT",
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


def _register(home: Path, *slugs: str) -> dict:
    """Register boards on disk (board.json + store) exactly as ``create_board`` does."""
    out = {}
    for slug in slugs:
        meta = kb.create_board(slug)
        out[slug] = Path(meta["db_path"]) if "db_path" in meta else kb.kanban_db_path(board=slug)
    return out


# ---------------------------------------------------------------------------
# One canonical resolver: a named board is never shadowed by an ambient pin
# ---------------------------------------------------------------------------

class TestNamedBoardWins:

    def test_named_board_wins_over_a_pinned_store(self, fresh_home, tmp_path, monkeypatch):
        """THE measured defect: a worker's pin made every board resolve to one store."""
        boards = _register(fresh_home, "ops", "defcon", "financially", "research")
        pinned = boards["defcon"]
        monkeypatch.setenv("HERMES_KANBAN_DB", str(pinned))
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "defcon")

        # The pin still answers the no-name (active) question...
        assert kb.kanban_db_path() == pinned
        # ...and every NAMED board resolves to its own store regardless of it.
        for slug, own in boards.items():
            assert kb.kanban_db_path(board=slug) == own, (
                f"board {slug!r} resolved to {kb.kanban_db_path(board=slug)} "
                f"instead of its own store {own}"
            )

    def test_registered_boards_never_collapse_to_one_store(self, fresh_home, monkeypatch):
        """Four registered boards under a pin yield four DISTINCT stores."""
        boards = _register(fresh_home, "ops", "defcon", "financially", "research")
        monkeypatch.setenv("HERMES_KANBAN_DB", str(boards["defcon"]))
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "defcon")

        resolved = {s: kb.kanban_db_path(board=s) for s in boards}
        assert len(set(resolved.values())) == len(resolved) == 4

    def test_metadata_db_path_is_per_board_under_a_pin(self, fresh_home, monkeypatch):
        """``board.json``'s ``db_path`` (the operator-visible field) stays per-board."""
        boards = _register(fresh_home, "ops", "defcon")
        monkeypatch.setenv("HERMES_KANBAN_DB", str(boards["defcon"]))
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "defcon")

        for slug in ("default", "ops", "defcon"):
            assert kb.read_board_metadata(slug)["db_path"] == str(kb.kanban_db_path(board=slug))

    def test_workspaces_and_attachments_roots_are_per_board_too(self, fresh_home, monkeypatch):
        """The same rule governs every per-board path, not just the store."""
        _register(fresh_home, "ops", "defcon")
        monkeypatch.setenv("HERMES_KANBAN_DB", str(fresh_home / "kanban" / "boards" / "defcon" / "kanban.db"))
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "defcon")
        monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(kb.workspaces_root(board="defcon")))

        assert kb.workspaces_root(board="ops") == fresh_home / "kanban" / "boards" / "ops" / "workspaces"
        assert kb.attachments_root(board="ops") == fresh_home / "kanban" / "boards" / "ops" / "attachments"

    def test_default_board_keeps_its_legacy_store(self, fresh_home, monkeypatch):
        _register(fresh_home, "ops")
        monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path(board="ops")))
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "ops")

        assert kb.kanban_db_path(board="default") == fresh_home / "kanban.db"


# ---------------------------------------------------------------------------
# A name nobody registered never becomes an estate (ruling t_c21ea472)
# ---------------------------------------------------------------------------

class TestUnregisteredNameNeverBecomesAnEstate:
    """The PROPERTY at the layer that owns it.

    The layout resolver answers a would-be path (it must: ``create_board`` writes
    ``board.json`` THROUGH it before the board exists); every door in front of a creating
    ``connect()`` refuses; and a would-be path is never a store.
    """

    def test_the_layout_resolver_answers_the_named_boards_would_be_path(self, fresh_home):
        """A miss is ANSWERED as the named slug's path — the layout answer, not a refusal."""
        _register(fresh_home, "ops")
        assert kb.kanban_db_path(board="no-such-board") == (
            fresh_home / "kanban" / "boards" / "no-such-board" / "kanban.db"
        )

    def test_a_miss_is_never_dressed_up_as_the_current_boards_store(self, fresh_home):
        """The defect's other half still holds: a miss must not resolve to the ACTIVE store."""
        boards = _register(fresh_home, "ops")
        kb.set_current_board("ops")
        for missing in ("typo-board", "defcon"):
            resolved = kb.kanban_db_path(board=missing)
            assert resolved != boards["ops"], (
                f"{missing!r} resolved to the CURRENT board's store {resolved}"
            )
            assert resolved == fresh_home / "kanban" / "boards" / missing / "kanban.db"

    def test_the_would_be_path_is_inert_nothing_is_minted(self, fresh_home):
        """Reading the path mints nothing; opening it REFUSES at the creating seam."""
        about_to_be = kb.kanban_db_path(board="no-such-board")
        assert not about_to_be.exists() and not about_to_be.parent.exists()
        with pytest.raises(ValueError) as exc:
            kb.init_db(board="no-such-board")
        assert "no-such-board" in str(exc.value) and "does not exist" in str(exc.value)
        assert not about_to_be.parent.exists(), "the refusal must not mint the board"

    def test_the_door_refuses_the_name_and_mints_nothing(self, fresh_home):
        """``assert_board_registered`` — the door the layout resolver is NOT — refuses."""
        with pytest.raises(kb.BoardResolutionError) as exc:
            kb.assert_board_registered("no-such-board")
        assert "no-such-board" in str(exc.value)
        assert not kb.board_dir("no-such-board").exists()

    def test_the_pin_cannot_make_an_unregistered_slug_fileable(self, fresh_home):
        """A pin changes WHERE the process resolves; it never makes a board registered."""
        phantom = fresh_home / "kanban" / "boards" / "phantom" / "kanban.db"
        os.environ["HERMES_KANBAN_DB"] = str(phantom)
        os.environ["HERMES_KANBAN_BOARD"] = "phantom"
        try:
            with pytest.raises(kb.BoardResolutionError):
                kb.assert_board_registered("phantom")
        finally:
            del os.environ["HERMES_KANBAN_DB"]
            del os.environ["HERMES_KANBAN_BOARD"]

    def test_malformed_slug_refuses_and_uppercase_is_normalisation_not_refusal(self, fresh_home):
        # "UPPER" is NOT malformed — slugs are lowercased by design (a board named in an
        # operator's capitals is the same board), so it is asserted separately below.
        # "" is not malformed either: an empty name means "the active board".
        for bad in ("../escape", "-leading", "a/b", "with space"):
            with pytest.raises(ValueError):
                kb.kanban_db_path(board=bad)
        assert kb.kanban_db_path(board="UPPER") == (
            fresh_home / "kanban" / "boards" / "upper" / "kanban.db"
        )

    def test_create_board_still_registers_through_the_layout_resolver(self, fresh_home):
        """THE RATCHET for the withdrawn reading: registration cannot live in the resolver.

        ``create_board`` writes ``board.json`` — whose ``db_path`` is derived by
        ``read_board_metadata`` -> ``kanban_db_path`` — BEFORE the board is registered.
        Make the layout resolver refuse an unregistered name and this raises
        ``BoardResolutionError`` from inside ``create_board``: no board could ever be
        created (measured 2026-11-06: 42 failed + 21 errors across four kanban modules).
        """
        meta = kb.create_board("brand-new")
        assert kb.board_is_registered("brand-new") is True
        assert meta["db_path"] == str(
            fresh_home / "kanban" / "boards" / "brand-new" / "kanban.db"
        )

    def test_board_exists_is_the_probe_for_a_missing_board(self, fresh_home):
        """Callers that mean "does it exist" ask ``board_exists``, not the resolver."""
        assert kb.board_exists("default") is True
        assert kb.board_exists("nope") is False



# ---------------------------------------------------------------------------
# The pin keeps its worker semantics, and refuses to be self-contradictory
# ---------------------------------------------------------------------------

class TestActiveBoardPin:

    def test_no_board_named_still_honours_the_worker_pin(self, fresh_home):
        boards = _register(fresh_home, "ops", "defcon")
        kb.set_current_board("ops")
        import os as _os
        _os.environ["HERMES_KANBAN_DB"] = str(boards["defcon"])
        _os.environ["HERMES_KANBAN_BOARD"] = "defcon"
        try:
            assert kb.kanban_db_path() == boards["defcon"]
            assert kb.get_current_board() == "defcon"
        finally:
            del _os.environ["HERMES_KANBAN_DB"]
            del _os.environ["HERMES_KANBAN_BOARD"]

    def test_pin_is_honoured_verbatim_for_the_active_board(self, fresh_home):
        """The worker pin may name ANY store file — a filename/shape check was TRIED and
        REJECTED: it broke 36 tests that use the documented channel (``apiserver.db``,
        ``triage-wake.db``, ...), and it buys nothing against the incident class because
        the 2026-09-27 clobber's destination was a perfectly-shaped
        ``boards/<slug>/kanban.db``. What guards a write is the destination guard
        (card t_190f99c9), not the pin's filename. The pin's SAFETY property is the one
        below: it is consulted for the active board only, never over a named board."""
        import os as _os
        forced = fresh_home / "scratch-copy.db"
        _os.environ["HERMES_KANBAN_DB"] = str(forced)
        try:
            assert kb.kanban_db_path() == forced
            boards = _register(fresh_home, "ops")
            assert kb.kanban_db_path(board="ops") == boards["ops"]
        finally:
            del _os.environ["HERMES_KANBAN_DB"]


# ---------------------------------------------------------------------------
# The dispatcher's own pin-writing path stays consistent
# ---------------------------------------------------------------------------

class TestSpawnPin:

    @pytest.mark.parametrize("slug", ["default", "ops"])
    def test_the_pin_pair_the_dispatcher_writes_is_self_consistent(self, fresh_home, slug):
        """``_default_spawn`` writes ``HERMES_KANBAN_DB`` and ``HERMES_KANBAN_BOARD``
        together (its own two lines); the resolver must accept that pair unchanged, or
        every spawned worker refuses its own board."""
        import os as _os
        _register(fresh_home, "ops")
        db = str(kb.kanban_db_path(board=slug))
        board_pin = kb._normalize_board_slug(slug) or kb.get_current_board()
        _os.environ["HERMES_KANBAN_DB"] = db
        _os.environ["HERMES_KANBAN_BOARD"] = board_pin
        try:
            assert kb.kanban_db_path() == Path(db)
            assert kb.get_current_board() == slug
        finally:
            del _os.environ["HERMES_KANBAN_DB"]
            del _os.environ["HERMES_KANBAN_BOARD"]


class TestScopedBoardName:

    def test_board_scope_outranks_the_pin(self, fresh_home, monkeypatch):
        """``--board`` / a scoped board is a NAME, so it outranks the ambient path pin.

        Measured live 2026-09-27 from inside a defcon-pinned worker:
        ``kanban --board ops show <ops card>`` answered "no such task" because the CLI's
        ``--board`` only scoped the current board while the pin still chose the store.
        """
        for slug in ("ops", "defcon"):
            kb.create_board(slug)
        monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path(board="defcon")))
        other = kb.kanban_db_path(board="ops")
        with kb.scoped_current_board("ops"):
            assert kb.kanban_db_path() == other
            assert kb.kanban_db_path(board="ops") == other
            assert kb.kanban_db_path(board="defcon") == kb.kanban_db_path(board="defcon")
        # Out of scope, the pin is back in charge of the ACTIVE board.
        assert kb.kanban_db_path() == kb.kanban_db_path(board="defcon")

    def test_scoping_an_unregistered_board_never_falls_back_to_default(self, fresh_home):
        """A scoped name nobody registered answers ITS path, and the door refuses it.

        This used to assert that the layout resolver RAISES (withdrawn, ruling
        t_c21ea472). What must hold is the other half of the incident: the scope never
        degrades to the ambient/default board's store, and nothing is minted for it.
        """
        _register(fresh_home, "ops")
        kb.set_current_board("ops")
        with kb.scoped_current_board("ghost"):
            assert kb.kanban_db_path() == (
                fresh_home / "kanban" / "boards" / "ghost" / "kanban.db"
            )
            with pytest.raises(kb.BoardResolutionError):
                kb.assert_board_registered("ghost")
        assert not kb.board_dir("ghost").exists()
