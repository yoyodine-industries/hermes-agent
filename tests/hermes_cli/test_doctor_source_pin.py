"""The committed dependency generation must route ``hermes_*`` to the checkout that built it.

A generation owns DEPENDENCIES; the checkout owns CODE. These tests pin the doctor surface that
names a generation which lost that (or was built before it existed), and the repair it applies.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, cast

from hermes_cli import doctor_platform
from hermes_cli.doctor_report import Finding
from pm import environments

# ``@doctor_check()`` turns a check into ``(should_fix) -> Finding``; pyright only sees the body.
check_source_pin = cast(Callable[[bool], Finding], doctor_platform._check_source_pin)


def _generation(root: Path) -> Path:
    """A minimal managed generation: ``<root>/environments/gen1`` with a venv and site-packages."""
    generation = root / "environments" / "gen1"
    venv = generation / "venv"
    venv.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("version = 3.11.9\n", encoding="utf-8")
    environments.site_packages(venv).mkdir(parents=True)
    (generation / ".lease-managed").write_text("", encoding="utf-8")
    return generation


def test_doctor_names_and_repairs_an_unpinned_generation(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    generation = _generation(tmp_path / "install")
    monkeypatch.setattr("hermes_cli.doctor.PROJECT_ROOT", checkout)
    monkeypatch.setattr(environments, "committed_venv", lambda project_root: generation / "venv")

    finding = check_source_pin(False)
    assert finding.issues and str(generation) in finding.issues[0]
    assert "doctor --fix" in finding.issues[0]
    assert not finding.fixed

    finding = check_source_pin(True)
    assert finding.fixed == 1
    assert environments.source_pin_problem(generation) is None
    stamp = environments.read_source_stamp(generation)
    assert stamp is not None and stamp["root"] == str(checkout.resolve())
    pin = environments.site_packages(generation / "venv") / environments.SOURCE_PIN_FILENAME
    assert environments.source_pin_line(checkout.resolve()) in pin.read_text(encoding="utf-8")

    assert not check_source_pin(False).issues


def test_doctor_names_a_generation_pinned_to_another_checkout(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    other = tmp_path / "other-checkout"
    checkout.mkdir()
    other.mkdir()
    generation = _generation(tmp_path / "install")
    environments.write_source_pin(generation, other)
    monkeypatch.setattr("hermes_cli.doctor.PROJECT_ROOT", checkout)
    monkeypatch.setattr(environments, "committed_venv", lambda project_root: generation / "venv")

    finding = check_source_pin(False)
    assert finding.issues and str(other.resolve()) in finding.issues[0]

    finding = check_source_pin(True)
    assert finding.fixed == 1
    stamp = environments.read_source_stamp(generation)
    assert stamp is not None and stamp["root"] == str(checkout.resolve())


def test_source_pin_problem_names_a_moved_checkout(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    generation = _generation(tmp_path / "install")
    environments.write_source_pin(generation, checkout)
    assert environments.source_pin_problem(generation) is None

    stamp = json.loads(environments.source_stamp_path(generation).read_text(encoding="utf-8"))
    assert stamp["schema"] == 1 and stamp["root"] == str(checkout.resolve())
    assert stamp["recorded_at"].endswith("+00:00")

    # The checkout moves: the generation's own record has to say so, not a booted probe.
    checkout.rename(tmp_path / "moved")
    problem = environments.source_pin_problem(generation)
    assert problem is not None and "no longer exists" in problem


def test_doctor_reports_a_payload_venv_as_not_applicable(tmp_path, monkeypatch, capsys):
    """A sealed payload ships its own venv beside its tree; it is not a generation.

    Read as one it looks merely unpinned ("carries no source.json provenance stamp"), so the check
    used to warn and, under ``--fix``, name a repair that cannot apply to a payload. It must say
    what it measured instead, and ``--fix`` must not write a pin onto a payload.
    """
    payload = tmp_path / "payload"
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
    assert environments.payload_venv(core) is not None, "the fixture must read as a payload"
    monkeypatch.setattr("hermes_cli.doctor.PROJECT_ROOT", core)
    monkeypatch.setattr(environments, "install_state_dir", lambda project_root: tmp_path / "state")

    finding = check_source_pin(False)
    out = capsys.readouterr().out
    assert "not applicable" in out and str(venv.parent) in out
    assert "runs this checkout's code" not in out
    assert not finding.issues and not finding.manual_issues and not finding.fixed

    finding = check_source_pin(True)
    assert not finding.fixed
    assert not (environments.site_packages(venv) / environments.SOURCE_PIN_FILENAME).exists()
