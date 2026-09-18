"""Regression tests for the profile IDENTITY chain (kanban t_f6011a57).

The contract: ``HERMES_HOME`` is the authority for WHERE a process runs; ``HERMES_PROFILE`` is a PIN
of the canonical name derived from that home, written at startup (``profiles.pin_profile_env``) and
by any spawner that hands a session to another profile. A child inherits its parent's environment,
so an unpinned name is another profile's name — every reader that trusts it runs as, and attributes
its rows to, the wrong actor.

These exercise the real functions with a real temp ``HERMES_HOME`` (the repo's E2E-style boundary
tests do the same), never the source text.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _hermes_root(tmp_path: Path, *profiles: str) -> Path:
    root = tmp_path / ".hermes"
    (root / "profiles").mkdir(parents=True, exist_ok=True)
    for name in profiles:
        (root / "profiles" / name).mkdir(parents=True, exist_ok=True)
    return root


def _boot(tmp_path, monkeypatch, *, home, argv, active_profile=None, env=None):
    """Run ``main._apply_profile_override`` with *home*/*argv* in place; return
    ``(HERMES_HOME, HERMES_PROFILE)`` as the booted process would see them."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for name in ("HERMES_HOME", "HERMES_PROFILE", "HERMES_PROFILE_NAME", "HERMES_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)
    if home is not None:
        monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(sys, "argv", argv)

    from hermes_cli.main import _apply_profile_override

    _apply_profile_override()
    return os.environ.get("HERMES_HOME", ""), os.environ.get("HERMES_PROFILE")


def test_explicit_flag_pins_the_name_over_an_inherited_one(tmp_path, monkeypatch):
    """The failure that started this: a session spawned with ``-p guest-lane`` while carrying a
    peer's identity must resolve to the flag, by home AND by name."""
    root = _hermes_root(tmp_path, "peer-lane", "guest-lane")
    home, name = _boot(
        tmp_path,
        monkeypatch,
        home=root / "profiles" / "peer-lane",
        argv=["hermes", "-p", "guest-lane", "chat"],
        env={"HERMES_PROFILE": "peer-lane", "HERMES_PROFILE_NAME": "peer-lane"},
    )
    assert Path(home) == root / "profiles" / "guest-lane"
    assert name == "guest-lane"


def test_inherited_profile_home_pins_its_own_name(tmp_path, monkeypatch):
    """No flag: a home under ``profiles/`` names the actor, and the name must be pinned from it."""
    root = _hermes_root(tmp_path, "peer-lane")
    home, name = _boot(
        tmp_path,
        monkeypatch,
        home=root / "profiles" / "peer-lane",
        argv=["hermes", "chat"],
    )
    assert Path(home) == root / "profiles" / "peer-lane"
    assert name == "peer-lane"


def test_a_stale_name_never_outlives_resolution(tmp_path, monkeypatch):
    """A name that disagrees with the home it came with is a leftover, not a second opinion."""
    root = _hermes_root(tmp_path, "peer-lane")
    _, name = _boot(
        tmp_path,
        monkeypatch,
        home=root / "profiles" / "peer-lane",
        argv=["hermes", "chat"],
        env={"HERMES_PROFILE": "operator", "HERMES_PROFILE_NAME": "operator"},
    )
    assert name == "peer-lane"


def test_a_dangling_inherited_home_is_not_adopted_as_identity(tmp_path, monkeypatch):
    """A path that is not a profile home is not an identity: resolution falls through to the
    profile the user actually selected, instead of trusting the inherited path."""
    root = _hermes_root(tmp_path, "guest-lane")
    (root / "active_profile").write_text("guest-lane", encoding="utf-8")
    home, name = _boot(
        tmp_path,
        monkeypatch,
        home=root / "profiles" / "ghost",
        argv=["hermes", "chat"],
    )
    assert Path(home) == root / "profiles" / "guest-lane"
    assert name == "guest-lane"


def test_the_root_home_pins_the_default_actor(tmp_path, monkeypatch):
    """The operator case: a default-profile session must report ITSELF as ``default``."""
    root = _hermes_root(tmp_path)
    home, name = _boot(tmp_path, monkeypatch, home=root, argv=["hermes", "chat"])
    assert Path(home) == root
    assert name == "default"


def test_a_custom_root_pins_its_own_default_actor(tmp_path, monkeypatch):
    """A root — native or custom — IS its own default profile, so the actor is ``default`` and any
    name inherited from another home is replaced rather than trusted."""
    custom = tmp_path / "elsewhere"
    custom.mkdir()
    _, name = _boot(
        tmp_path,
        monkeypatch,
        home=custom,
        argv=["hermes", "chat"],
        env={"HERMES_PROFILE": "peer-lane"},
    )
    assert name == "default"


def test_a_home_with_no_canonical_name_clears_a_stale_name(tmp_path, monkeypatch):
    """A directory that is not a profile home has no actor to pin: a stale name must not survive as
    a description of the process."""
    root = _hermes_root(tmp_path)
    nameless = root / "profiles" / "not.a.profile"
    nameless.mkdir(parents=True)
    _, name = _boot(
        tmp_path,
        monkeypatch,
        home=nameless,
        argv=["hermes", "chat"],
        env={"HERMES_PROFILE": "peer-lane"},
    )
    assert name is None


def test_dotenv_cannot_repoint_the_resolved_home(tmp_path, monkeypatch, capsys):
    """Configuration loads after identity: a ``.env`` that re-points HERMES_HOME is the bug behind
    "the settings I edited are not the ones being read", so resolution wins and says so."""
    root = _hermes_root(tmp_path, "guest-lane")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv(
        "HERMES_HOME", str(root / "profiles" / "guest-lane")
    )  # what the dotenv installs
    monkeypatch.setenv("HERMES_PROFILE", "peer-lane")  # ...and what it installs with it

    from hermes_cli.main import _pin_identity_after_dotenv

    _pin_identity_after_dotenv(str(root / "profiles" / "guest-lane"))
    assert Path(os.environ["HERMES_HOME"]) == root / "profiles" / "guest-lane"
    assert os.environ["HERMES_PROFILE"] == "guest-lane"

    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_PROFILE", "peer-lane")
    _pin_identity_after_dotenv(str(root / "profiles" / "guest-lane"))
    assert Path(os.environ["HERMES_HOME"]) == root / "profiles" / "guest-lane"
    assert os.environ["HERMES_PROFILE"] == "guest-lane"
    assert "re-pointed HERMES_HOME" in capsys.readouterr().err


def test_dotenv_may_supply_the_home_when_nothing_resolved_one(tmp_path, monkeypatch):
    """The other half of the rule: when the process started with no home, the dotenv is allowed to
    supply one — and the name is then pinned from the home it supplied."""
    root = _hermes_root(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "peer-lane")

    from hermes_cli.main import _pin_identity_after_dotenv

    # The dotenv supplied the root: its own default actor is the name, not the stale export.
    monkeypatch.setenv("HERMES_HOME", str(root))
    _pin_identity_after_dotenv(None)
    assert os.environ["HERMES_PROFILE"] == "default"

    # Nothing resolved a home and no dotenv supplied one: no identity is invented.
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    _pin_identity_after_dotenv(None)
    assert "HERMES_PROFILE" not in os.environ


class TestProfileNameHelpers:
    """``profiles.profile_name_for_home`` / ``pin_profile_env`` — the derivation every reader of the
    actor shares."""

    def test_derives_default_profile_and_custom(self, tmp_path, monkeypatch):
        from hermes_cli import profiles

        root = _hermes_root(tmp_path, "guest-lane")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("HERMES_HOME", raising=False)
        assert profiles.profile_name_for_home(root) == "default"
        assert profiles.profile_name_for_home(root / "profiles" / "guest-lane") == "guest-lane"
        # A root — custom or native — is its own default: HERMES_HOME pointing at it names ``default``.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "elsewhere"))
        assert profiles.profile_name_for_home() == "default"
        monkeypatch.delenv("HERMES_HOME", raising=False)
        # A path that is not a profile home has no canonical name to offer.
        assert profiles.profile_name_for_home(tmp_path / "elsewhere") == "custom"
        assert profiles.profile_name_for_home(root / "profiles" / "guest-lane" / "cron") == "custom"
        assert profiles.profile_name_for_home(root / "profiles" / "not.a.profile") == "custom"

    def test_pin_writes_the_derived_name(self, tmp_path, monkeypatch):
        from hermes_cli import profiles

        root = _hermes_root(tmp_path, "guest-lane")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setenv("HERMES_PROFILE", "peer-lane")
        monkeypatch.setenv("HERMES_PROFILE_NAME", "peer-lane")
        assert profiles.pin_profile_env(root / "profiles" / "guest-lane") == "guest-lane"
        assert os.environ["HERMES_PROFILE"] == "guest-lane"
        # The legacy alias must not keep a name the home contradicts.
        assert "HERMES_PROFILE_NAME" not in os.environ

    def test_pin_clears_identity_for_a_nameless_home(self, tmp_path, monkeypatch):
        from hermes_cli import profiles

        root = _hermes_root(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setenv("HERMES_PROFILE", "peer-lane")
        monkeypatch.setenv("HERMES_PROFILE_NAME", "peer-lane")
        assert profiles.pin_profile_env(root / "profiles" / "not.a.profile") == "custom"
        assert "HERMES_PROFILE" not in os.environ
        assert "HERMES_PROFILE_NAME" not in os.environ

    def test_scrub_drops_only_identity(self):
        from hermes_cli import profiles

        env = {
            "PATH": "/usr/bin",
            "HERMES_HOME": "/x/profiles/peer-lane",
            "HERMES_PROFILE": "peer-lane",
            "HERMES_PROFILE_NAME": "peer-lane",
            "HERMES_SESSION_ID": "keep-me",
        }
        assert profiles.scrub_profile_identity_env(env) == {
            "PATH": "/usr/bin",
            "HERMES_SESSION_ID": "keep-me",
        }


_PROBE = """
import os
import sys

sys.argv = ["hermes", "-p", "coder", "config", "path"]
import hermes_cli.main  # noqa: E402  boot sequence runs here

print("HOME=" + (os.environ.get("HERMES_HOME") or ""))
print("PROFILE=" + (os.environ.get("HERMES_PROFILE") or ""))
"""


def test_booted_process_keeps_its_identity_against_a_clobbering_dotenv(tmp_path):
    """End to end, in a real interpreter: a profile ``.env`` that re-points HERMES_HOME must not be
    able to run the process it was loaded for in a different profile."""
    root = _hermes_root(tmp_path, "coder")
    (root / "profiles" / "coder" / ".env").write_text(
        f"HERMES_HOME={root}\nHERMES_PROFILE=peer-lane\nHERMES_HOME_MODE=2770\n",
        encoding="utf-8",
    )
    probe = tmp_path / "probe_identity.py"
    probe.write_text(_PROBE, encoding="utf-8")

    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_")}
    env["HOME"] = str(tmp_path)
    env["PATH"] = os.environ.get("PATH", "")
    proc = subprocess.run(
        [sys.executable, str(probe)],
        cwd=str(Path(__file__).resolve().parents[2]),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    lines = {line.split("=", 1)[0]: line.split("=", 1)[1] for line in proc.stdout.splitlines() if "=" in line}
    assert Path(lines["HOME"]) == root / "profiles" / "coder", proc.stdout[-2000:]
    assert lines["PROFILE"] == "coder", proc.stdout[-2000:]
    # The guard must have FIRED: without it this assertion cannot pass, because the .env above
    # re-points HERMES_HOME at the root on import.
    assert "re-pointed HERMES_HOME" in proc.stderr, proc.stderr[-2000:]
