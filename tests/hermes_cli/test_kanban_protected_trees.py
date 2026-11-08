"""Invariant: "no guard here" and "the guard was here and is gone" are NOT the same state.

Card t_2ec1c7c8. ``hermes_cli/kanban_protected_trees.py`` reads the protected-root
declaration out of the shared-trees guard. Until this card, every way of failing to
read that guard — absent, unreadable, declaring nothing, a home that wires a path
that no longer exists — collapsed into the same answer: an EMPTY root list, with no
error, no log and no refusal. A deleted guard therefore narrowed a security check
silently, which is the defect class the whole module exists to oppose.

So the tests pin the two halves of the fix together:

  * the gate stays FAIL-OPEN — no state may turn into a completion refusal;
  * an EXPECTED guard that cannot be read is REPORTED (state + one stderr line),
    while a host that has never had one stays legitimately, silently empty.

The third half is the repoint itself: ``DEPLOYED_GUARD`` names the ``ops-checks``
deploy unit's live dest, not the ``yaan-majordomo`` shim farm that aliased it and was
retired. That constant is pinned by assertion because it is a path in a security
check — a stale one reads as a working one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_protected_trees as protected

#: The retired shim farm. No constant here may name it again.
RETIRED_SHIM = "/opt/hermes_prod/yaan-majordomo"

DECLARES_ROOTS = "_SHARED_SOURCE_ROOTS = ['/opt/hermes_sandbox/repo']\n"


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    """No test reads the live host's guard, its config, or a cached reading of either."""
    protected.reset_cache()
    protected._WARNED.clear()
    monkeypatch.delenv(protected.ENV_GUARD, raising=False)
    monkeypatch.delenv("GUARD_SHARED_TREES", raising=False)
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "hermes-home")
    yield
    protected.reset_cache()
    protected._WARNED.clear()


def _deploy(monkeypatch, tmp_path, *, dest_exists: bool = True, guard_body: str | None = None) -> Path:
    """Point ``DEPLOYED_GUARD`` at a decoy dest — the unit's directory, and maybe its guard."""
    dest = tmp_path / "ops-checks"
    if dest_exists:
        dest.mkdir()
    path = dest / "guard-shared-trees.py"
    if guard_body is not None:
        path.write_text(guard_body, encoding="utf-8")
    monkeypatch.setattr(protected, "DEPLOYED_GUARD", str(path))
    protected.reset_cache()
    return path


def _wired(monkeypatch, tmp_path, command_path: str) -> None:
    """A home whose own ``config.yaml`` declares this guard, as the fleet declares it."""
    config = tmp_path / "hermes-home" / "config.yaml"
    config.write_text(
        "hooks:\n"
        "  pre_tool_call:\n"
        "    - command: 'hook-launcher.py %s'\n" % command_path,
        encoding="utf-8",
    )
    protected.reset_cache()


def test_the_deployed_constant_names_the_ops_checks_dest():
    """The repoint, pinned: the dest itself, never the shim farm that aliased it."""
    assert protected.DEPLOYED_GUARD == "/opt/hermes_prod/ops-checks/guard-shared-trees.py"
    assert RETIRED_SHIM not in protected.DEPLOYED_GUARD
    assert "yaan-majordomo" not in protected.DEPLOYED_GUARD


def test_an_expected_deployed_guard_that_is_absent_is_reported(monkeypatch, tmp_path, capsys):
    """The card's own case: no override, no wiring, DEPLOYED_GUARD absent — SAY SO.

    The dest directory existing is what proves this host DID carry the unit, so the
    empty root list is a loss (a regression in the security check), not a fresh host.
    """
    _deploy(monkeypatch, tmp_path, dest_exists=True, guard_body=None)

    diagnosis = protected.guard_diagnosis()

    assert diagnosis["state"] == protected.GUARD_MISSING
    assert diagnosis["expected"] is True
    assert diagnosis["roots"] == []
    assert protected.shared_source_roots() == []

    err = capsys.readouterr().err
    assert "protected-trees guard missing" in err
    assert str(tmp_path / "ops-checks") in err      # the dest, so a reader can act
    assert "protecting NOTHING" in err              # the consequence, not just a fact


def test_a_host_that_never_had_a_guard_stays_legitimately_empty(monkeypatch, tmp_path, capsys):
    """Fail-open on a fresh host is a CHOICE, and it is a silent one."""
    _deploy(monkeypatch, tmp_path, dest_exists=False, guard_body=None)

    diagnosis = protected.guard_diagnosis()

    assert diagnosis["state"] == protected.GUARD_ABSENT
    assert diagnosis["expected"] is False
    assert diagnosis["roots"] == []
    assert protected.shared_source_roots() == []
    assert capsys.readouterr().err == ""


def test_the_absence_is_reported_once_per_process(monkeypatch, tmp_path, capsys):
    """One line per fault: completions are hot, and a per-call warning is noise."""
    _deploy(monkeypatch, tmp_path, dest_exists=True, guard_body=None)

    protected.shared_source_roots()
    protected.reset_cache()          # a cache reset must not re-announce the same fault
    protected.shared_source_roots()
    protected.guard_diagnosis()

    assert capsys.readouterr().err.count("protected-trees guard") == 1


def test_a_guard_that_cannot_be_imported_is_reported(monkeypatch, tmp_path, capsys):
    """Exists-but-cannot-be-read is the ``unreadable`` state, not an empty declaration."""
    _deploy(
        monkeypatch, tmp_path,
        dest_exists=True,
        guard_body="raise RuntimeError('guard is broken')\n",
    )

    diagnosis = protected.guard_diagnosis()

    assert diagnosis["state"] == protected.GUARD_UNREADABLE
    assert diagnosis["expected"] is True
    assert protected.shared_source_roots() == []
    err = capsys.readouterr().err
    assert "guard is broken" in err
    assert "protecting NOTHING" in err


def test_a_guard_that_declares_nothing_is_reported(monkeypatch, tmp_path, capsys):
    """An importable file that declares no root list is a broken guard, not a quiet host."""
    _deploy(monkeypatch, tmp_path, dest_exists=True, guard_body="NOT_THE_LIST = []\n")

    diagnosis = protected.guard_diagnosis()

    assert diagnosis["state"] == protected.GUARD_NO_DECLARATION
    assert diagnosis["expected"] is True
    assert protected.shared_source_roots() == []
    assert "_SHARED_SOURCE_ROOTS" in capsys.readouterr().err


def test_a_wired_home_whose_guard_is_gone_is_reported(monkeypatch, tmp_path, capsys):
    """The post-repoint failure shape: config still names the old shim path, and it is gone."""
    gone = str(tmp_path / "retired-dest" / "guard-shared-trees.py")
    _wired(monkeypatch, tmp_path, gone)

    diagnosis = protected.guard_diagnosis()

    assert diagnosis["state"] == protected.GUARD_MISSING
    assert diagnosis["source"] == protected.SOURCE_CONFIG
    assert diagnosis["expected"] is True
    err = capsys.readouterr().err
    assert gone in err
    assert "config names" in err


def test_an_override_naming_no_file_is_absent_not_a_regression(monkeypatch, tmp_path, capsys):
    """An override pointing at nothing is the caller's own "no guard" (tests do this).

    It must stay silent: this is the idiom every completion-boundary fixture uses, and
    turning it into a fault report would make the suite noisy for no signal.
    """
    monkeypatch.setenv(protected.ENV_GUARD, str(tmp_path / "no-such-guard.py"))
    protected.reset_cache()

    diagnosis = protected.guard_diagnosis()

    assert diagnosis["state"] == protected.GUARD_ABSENT
    assert diagnosis["expected"] is False
    assert protected.shared_source_roots() == []
    assert capsys.readouterr().err == ""


def test_a_healthy_guard_yields_its_roots_and_says_nothing(monkeypatch, tmp_path, capsys):
    """The control: a readable declaration is read, and produces no report."""
    _deploy(monkeypatch, tmp_path, dest_exists=True, guard_body=DECLARES_ROOTS)

    diagnosis = protected.guard_diagnosis()

    assert diagnosis["state"] == protected.GUARD_OK
    assert diagnosis["expected"] is False
    assert diagnosis["source"] == protected.SOURCE_DEPLOYED
    assert diagnosis["roots"] == ["/opt/hermes_sandbox/repo"]
    assert protected.shared_source_roots() == ["/opt/hermes_sandbox/repo"]
    assert protected.shared_source_root("/opt/hermes_sandbox/repo/work/x") == "/opt/hermes_sandbox/repo"
    assert capsys.readouterr().err == ""
