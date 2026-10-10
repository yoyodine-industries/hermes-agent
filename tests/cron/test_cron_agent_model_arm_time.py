"""Arm-time vs fire-time model resolution for agent-mode cron jobs (card t_2dd613e1).

ONE defect, TWO arms, and each direction is measured here:

* FIRE time must HEAL. The scheduler resolves a job's model as
  per-job pin -> profile ``cron.model`` -> this profile's ``config.yaml model.default`` ->
  the INSTALL ROOT's ``config.yaml model.default`` -> ``HERMES_MODEL``. A profile config OVERRIDES
  the root default; it does not ERASE it, so a profile that declares no ``model`` key inherits the
  install root's. Before this change the tick raised
  ``RuntimeError: Cron job '...' has no model configured`` — a one-shot carrier died at its first
  fire with its single repetition already consumed, so it could never fire again.

* ARM time must REFUSE. ``create_job`` and the arming branches of ``update_job`` raise (naming the
  job) when nothing resolves, so a carrier cannot be BORN dead. The refusal persists nothing;
  bookkeeping writes and the fire path never meet it, so a row that predates the guard still fires
  and is still pausable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cron import jobs

#: The install root's config: the default every ordinary profile inherits.
ROOT_CONFIG = "model:\n  default: root-default-model\n  provider: deepseek\n"
#: A profile config that declares NO ``model`` key — the platform-stl shape.
PROFILE_CONFIG_WITHOUT_MODEL = "agent:\n  reasoning_effort: high\n"


@pytest.fixture()
def root_home(tmp_path, monkeypatch):
    """A hermetic install ROOT with ``model.default`` and a profile that declares no model.

    ``HERMES_HOME`` points at the profile, so ``get_hermes_home()`` and the root derivation
    (``<root>/profiles/<name>`` -> ``<root>``) agree with the on-disk layout, and the real host
    install's config never leaks into a test.
    """
    root = tmp_path / "hermes"
    profile = root / "profiles" / "probe"
    (profile / "cron").mkdir(parents=True)
    (root / "config.yaml").write_text(ROOT_CONFIG, encoding="utf-8")
    (profile / "config.yaml").write_text(PROFILE_CONFIG_WITHOUT_MODEL, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    # The cron tests' autouse fixture pins a default model: this card is about what happens when
    # NOTHING resolves, so it must be gone.
    monkeypatch.delenv("HERMES_MODEL", raising=False)
    return root, profile


@pytest.fixture()
def bare_home(tmp_path, monkeypatch):
    """A hermetic home with NO ``config.yaml`` and no model anywhere."""
    home = tmp_path / "home"
    (home / "cron").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_MODEL", raising=False)
    return home


def store(home: Path):
    """``use_cron_store`` takes a HERMES HOME: the store itself lands in ``<home>/cron``."""
    return jobs.use_cron_store(home)


# ---------------------------------------------------------------------------
# Arm 1 — FIRE time: the root default heals a profile that declares no model
# ---------------------------------------------------------------------------

def test_resolver_falls_back_to_the_install_root_default(root_home):
    root, profile = root_home
    assert jobs.resolve_agent_model(None, home=profile) == "root-default-model"


def test_profile_model_default_overrides_the_root_default(root_home):
    """A profile config OVERRIDES; it does not erase (the root default is the fallback only)."""
    root, profile = root_home
    (profile / "config.yaml").write_text(
        "model:\n  default: profile-model\n", encoding="utf-8")
    assert jobs.resolve_agent_model(None, home=profile) == "profile-model"


def test_job_pin_beats_both_config_layers(root_home):
    root, profile = root_home
    assert jobs.resolve_agent_model("pinned-model", home=profile) == "pinned-model"


def test_profile_cron_model_fleet_default_beats_the_root_default(root_home):
    root, profile = root_home
    (profile / "config.yaml").write_text("cron:\n  model: fleet-model\n", encoding="utf-8")
    assert jobs.resolve_agent_model(None, home=profile) == "fleet-model"


def test_scheduler_resolves_the_root_default_at_fire_time(root_home, monkeypatch):
    """The fire-time arm end to end: no job pin, profile declares no model, ROOT does.

    Before this change this raised ``RuntimeError: ... has no model configured``.
    """
    from cron import scheduler as sched

    root, profile = root_home
    monkeypatch.setattr(sched, "_get_hermes_home", lambda: profile)
    jc = sched._load_cron_job_config({"id": "j", "name": "j", "prompt": "x"}, "j", "j")
    assert jc.model == "root-default-model"


def test_scheduler_still_refuses_when_truly_nothing_resolves(bare_home, monkeypatch):
    """The fail-fast guard is NOT weakened: with no config anywhere it still refuses."""
    from cron import scheduler as sched

    monkeypatch.setattr(sched, "_get_hermes_home", lambda: bare_home)
    with pytest.raises(RuntimeError, match="has no model configured"):
        sched._load_cron_job_config({"id": "j", "name": "j", "prompt": "x"}, "j", "j")


# ---------------------------------------------------------------------------
# Arm 2 — ARM time: an unresolvable agent-mode job is refused at the door
# ---------------------------------------------------------------------------

def test_create_job_refuses_an_agent_job_with_no_resolvable_model(bare_home):
    """The failure moves from FIRE time to ARM time, and the refusal NAMES the job."""
    with store(bare_home):
        with pytest.raises(ValueError) as excinfo:
            jobs.create_job(prompt="carry the board write", schedule="in 5m",
                            name="oneshot-executor", deliver="local")
        message = str(excinfo.value)
        assert "oneshot-executor" in message
        assert "ARM time" in message
        # Nothing was persisted by the refusal.
        assert jobs.load_jobs() == []


def test_create_job_control_with_an_explicit_model_succeeds(bare_home):
    """The CONTROL: the same create with a resolvable model is accepted (not a blanket refusal)."""
    with store(bare_home):
        job = jobs.create_job(prompt="carry the board write", schedule="in 5m",
                              name="oneshot-executor", deliver="local", model="deepseek-flash")
        assert job["model"] == "deepseek-flash"
        assert [row["id"] for row in jobs.load_jobs()] == [job["id"]]


def test_create_job_control_a_root_default_is_enough(root_home):
    """A profile with no model key but a ROOT default arms fine — the born-dead case is gone."""
    root, profile = root_home
    with store(profile):
        job = jobs.create_job(prompt="carry the board write", schedule="in 5m", deliver="local")
        assert job["id"]


def test_paused_create_is_staged_and_the_enable_door_refuses_it(bare_home):
    """A PAUSED create is a staging write (a shipped distribution's jobs, the approval queue),
    not an arm: it is accepted, and resuming it is refused while nothing resolves."""
    with store(bare_home):
        job = jobs.create_job(prompt="staged carrier", schedule="every 1h", deliver="local",
                              paused=True, paused_reason="awaiting operator approval")
        assert job["enabled"] is False and job["state"] == "paused"
        with pytest.raises(ValueError, match="no model resolves"):
            jobs.resume_job(job["id"])


def test_create_job_does_not_refuse_a_no_agent_script_job(bare_home):
    """A ``no_agent`` script row never needs a model."""
    (bare_home / "scripts").mkdir()
    (bare_home / "scripts" / "probe.sh").write_text("#!/bin/bash\necho ok\n", encoding="utf-8")
    with store(bare_home):
        job = jobs.create_job(prompt=None, schedule="every 5m", script="probe.sh",
                              no_agent=True, deliver="local")
        assert job["no_agent"] is True


def test_resume_refuses_to_arm_a_dead_agent_job(bare_home):
    """The ENABLE door is guarded too: resuming a dead row would just re-arm the corpse."""
    with store(bare_home):
        jobs.save_jobs([{
            "id": "deadbeef0001", "name": "dead carrier", "prompt": "x", "model": None,
            "schedule": {"kind": "interval", "every": 3600, "display": "every 1h"},
            "enabled": False, "state": "paused",
        }])
        with pytest.raises(ValueError) as excinfo:
            jobs.resume_job("deadbeef0001")
        assert "dead carrier" in str(excinfo.value)
        row = jobs.load_jobs()[0]
        assert row["enabled"] is False and row["state"] == "paused"


def test_update_job_refuses_to_set_an_unresolvable_model(bare_home):
    """An explicit model change is an arming write: clearing the pin back to nothing is refused."""
    with store(bare_home):
        job = jobs.create_job(prompt="x", schedule="every 1h", model="deepseek-flash")
        with pytest.raises(ValueError, match="no model resolves"):
            jobs.update_job(job["id"], {"model": None})
        assert jobs.load_jobs()[0]["model"] == "deepseek-flash"


def test_rearm_refuses_a_dead_oneshot(bare_home):
    """``rearm_oneshot`` is the same arm door one field over (the released executor's re-arm)."""
    with store(bare_home):
        jobs.save_jobs([{
            "id": "deadbeef0002", "name": "dead oneshot", "prompt": "x", "model": None,
            "schedule": {"kind": "once", "run_at": "2999-01-01T00:00:00+00:00",
                         "display": "2999-01-01"},
            "repeat": {"times": 1, "completed": 1},
            "enabled": True, "state": "completed",
        }])
        with pytest.raises(ValueError, match="no model resolves"):
            jobs.rearm_oneshot("deadbeef0002", "2999-01-02T00:00:00+00:00")


# ---------------------------------------------------------------------------
# The guard lives on the ARM path only — a pre-existing dead row still runs
# ---------------------------------------------------------------------------

def test_fire_path_bookkeeping_and_pause_never_meet_the_guard(bare_home):
    """The fire path's update_job callers pass bookkeeping fields only, and a dead row stays pausable.

    If the guard fired on these, repairing an already-dead carrier (pin the model, then pause to
    re-arm) would be impossible and a mid-fire bookkeeping write would fail the run.
    """
    with store(bare_home):
        jobs.save_jobs([{
            "id": "deadbeef0003", "name": "legacy dead carrier", "prompt": "x", "model": None,
            "schedule": {"kind": "interval", "every": 3600, "display": "every 1h"},
            "enabled": True, "state": "scheduled",
        }])
        jobs.update_job("deadbeef0003", {"last_delivery_error": "gateway shutdown"})
        jobs.update_job("deadbeef0003", {"last_delivery_queued": None})
        jobs.update_job("deadbeef0003", {"monitor_state": {"last_output_hash": "abc"}})
        # A dead row must remain pausable: that is the first half of the repair.
        jobs.pause_job("deadbeef0003", reason="pin a model first")
        row = jobs.load_jobs()[0]
        assert row["last_delivery_error"] == "gateway shutdown"
        assert row["state"] == "paused" and row["enabled"] is False


def test_repaired_dead_row_can_then_be_armed(bare_home):
    """Pin the model on the dead row, and the enable door opens — the whole repair, in order."""
    with store(bare_home):
        jobs.save_jobs([{
            "id": "deadbeef0004", "name": "legacy dead carrier", "prompt": "x", "model": None,
            "schedule": {"kind": "interval", "every": 3600, "display": "every 1h"},
            "enabled": False, "state": "paused",
        }])
        jobs.update_job("deadbeef0004", {"model": "deepseek-flash"})
        armed = jobs.resume_job("deadbeef0004")
        assert armed is not None and armed["enabled"] is True
