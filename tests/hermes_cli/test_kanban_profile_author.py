"""The audit author on a kanban row must be the ACTOR, not a name inherited from a parent process.

``HERMES_PROFILE`` is a pin set at startup, and a child that never went through resolution (or that
was spawned before the pin existed) can still carry another profile's name. The author is therefore
DERIVED from the home the process actually runs in (kanban t_f6011a57).
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_identity(monkeypatch):
    for name in ("HERMES_HOME", "HERMES_PROFILE", "HERMES_PROFILE_NAME", "USER"):
        monkeypatch.delenv(name, raising=False)


def _home(tmp_path: Path, *names: str) -> Path:
    root = tmp_path / ".hermes"
    (root / "profiles").mkdir(parents=True, exist_ok=True)
    for name in names:
        (root / "profiles" / name).mkdir(parents=True, exist_ok=True)
    return root


def test_a_stale_inherited_name_does_not_win_over_the_home(tmp_path, monkeypatch):
    """The regression: a operator session that inherited peer-lane's environment stamped its
    rows ``peer-lane``."""
    root = _home(tmp_path, "peer-lane")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "peer-lane"))
    monkeypatch.setenv("HERMES_PROFILE", "operator")

    from hermes_cli import kanban

    assert kanban._profile_author() == "peer-lane"


def test_the_default_profile_attributes_to_default(tmp_path, monkeypatch):
    """The operator's own identity is ``default`` — not ``user``, and not a profile it was
    spawned from."""
    root = _home(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban

    assert kanban._profile_author() == "default"


def test_a_home_with_no_canonical_name_falls_back_to_the_operators_own_name(tmp_path, monkeypatch):
    """A directory that is not a profile home has no canonical name to derive, so the operator's own
    export is the honest answer."""
    root = _home(tmp_path, "peer.lane")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "peer.lane"))
    monkeypatch.setenv("HERMES_PROFILE", "custom-build")

    from hermes_cli import kanban

    assert kanban._profile_author() == "custom-build"


def test_nothing_known_still_attributes_to_user(tmp_path, monkeypatch):
    """Last resort only: no canonical name and no export leaves the generic CLI author."""
    root = _home(tmp_path, "peer.lane")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "peer.lane"))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.delenv("HERMES_PROFILE_NAME", raising=False)
    monkeypatch.delenv("USER", raising=False)

    from hermes_cli import kanban

    assert kanban._profile_author() == "user"


@pytest.mark.parametrize("module_name", ["kanban_specify", "kanban_decompose"])
def test_the_triage_and_decompose_mirrors_agree(tmp_path, monkeypatch, module_name):
    """Sibling call paths: the specifier/decomposer stamps must derive the same way, or the same
    session is attributed to two different actors depending on which hop wrote the row."""
    import importlib

    root = _home(tmp_path, "reviewer-lane")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "reviewer-lane"))
    monkeypatch.setenv("HERMES_PROFILE", "operator")

    module = importlib.import_module(f"hermes_cli.{module_name}")

    assert module._profile_author() == "reviewer-lane"
