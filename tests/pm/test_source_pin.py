"""The generation↔checkout contract at its edges: a generation owns DEPENDENCIES, the checkout owns CODE.

The build and sync tests prove the pin reaches a real generation; these cover the edges they cannot
cheaply reach — a self-pin is refused, the pin is byte-stable and never rewritten when it is already
correct, and a sealed payload's own venv (not a generation at all) is left untouched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pm import environments


def _generation(root: Path) -> Path:
    """A minimal managed generation: ``<root>/environments/gen1`` with a venv and site-packages."""
    generation = root / "environments" / "gen1"
    venv = generation / "venv"
    venv.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("version = 3.14.7\n", encoding="utf-8")
    environments.site_packages(venv).mkdir(parents=True)
    (generation / ".lease-managed").write_text("", encoding="utf-8")
    return generation


def _payload(root: Path) -> Path:
    """A sealed payload ``<root>/{manifest.json,hermes-agent/,venv/}``; returns the payload's tree."""
    payload = root / "payload"
    core = payload / "hermes-agent"
    core.mkdir(parents=True)
    venv = payload / "venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text("version = 3.14.7\n", encoding="utf-8")
    environments.site_packages(venv).mkdir(parents=True)
    (payload / "manifest.json").write_text(json.dumps({
        "repo": "hermes-agent", "venv": "venv", "store": "store",
        "runtime": {"commands": {"hermes": "bin/hermes"}},
    }), encoding="utf-8")
    return core


def _pin(generation: Path) -> Path:
    return environments.site_packages(generation / "venv") / environments.SOURCE_PIN_FILENAME


def test_write_source_pin_skips_a_root_inside_the_generation(tmp_path):
    """The generation's own ``workspace/`` is a build snapshot, never a code source: a pin naming it
    would be a self-pin asserting nothing."""
    generation = _generation(tmp_path / "install")
    workspace = generation / "workspace"
    workspace.mkdir()

    assert environments.write_source_pin(generation, workspace) is None
    assert not environments.source_stamp_path(generation).exists()
    assert not _pin(generation).exists()
    # Nothing was claimed, so the generation still reports honestly as unpinned.
    assert environments.source_pin_problem(generation) is not None


def test_ensure_source_pin_skips_a_generation_own_workspace(tmp_path, monkeypatch):
    generation = _generation(tmp_path / "install")
    workspace = generation / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(environments, "committed_venv", lambda project_root: generation / "venv")

    assert environments.ensure_source_pin(workspace) is None
    assert not environments.source_stamp_path(generation).exists()
    assert not _pin(generation).exists()


def test_source_pin_is_byte_stable_and_ensure_rewrites_nothing(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    generation = _generation(tmp_path / "install")
    pin = _pin(generation)

    environments.write_source_pin(generation, checkout)
    first = pin.read_bytes()
    environments.write_source_pin(generation, checkout)
    assert pin.read_bytes() == first, "the same root must produce the same pin bytes"

    # An already-correct generation is not rewritten at all — content AND mtime survive.
    os.utime(pin, (1_000_000, 1_000_000))
    untouched = pin.stat().st_mtime_ns
    monkeypatch.setattr(environments, "committed_venv", lambda project_root: generation / "venv")
    assert environments.ensure_source_pin(checkout) == pin
    assert pin.read_bytes() == first
    assert pin.stat().st_mtime_ns == untouched
    assert environments.source_pin_problem(generation) is None


def test_ensure_source_pin_is_a_no_op_for_a_payload_venv(tmp_path, monkeypatch):
    """A sealed payload ships its own venv; it is not a generation, so there is nothing to pin."""
    core = _payload(tmp_path)
    monkeypatch.setattr(environments, "install_state_dir", lambda project_root: tmp_path / "state")
    venv = environments.committed_venv(core)
    assert venv is not None and venv.is_dir(), "the fixture must resolve the payload's own venv"

    assert environments.ensure_source_pin(core) is None
    assert not (environments.site_packages(venv) / environments.SOURCE_PIN_FILENAME).exists()
    assert not environments.source_stamp_path(venv.parent).exists()
    # Read naively it looks like an unpinned generation — which is why doctor must gate on the
    # managed marker rather than on this answer alone.
    assert environments.source_pin_problem(venv.parent) is not None
