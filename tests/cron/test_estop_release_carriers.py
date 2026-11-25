"""A hold must never be able to stop its own RELEASE CARRIERS.

migrations/t_7ec6ade9 — the carrier half of ``RULING-leaked-hold-class.md`` (t_e4da9955),
absorbing the measured defect of t_950de3e5:

    a lane-scoped ``--lockdown --allow-profile platform-stl ...`` admits PROFILES, and the two
    carriers the fleet's route to prod rides — ``deploy-train`` (id ``c21eb1835846``) and
    ``hermes-update`` (id ``2106923395b8``) — live in the ``default`` profile's cron store. So the
    window refused its own release leg every tick:

        WARNING cron.scheduler: Cron job c21eb1835846 held by the lane-scoped lockdown - profile
        'default' is not on the allowlist (admitted: platform-coder, platform-stl); it stays due

The exemption is STRUCTURAL (``agent.estop.RELEASE_CARRIER_JOBS``): it lives in the engine, so a
hold cannot name its own releaser as held whatever the operator passed at arm time, and it applies
only under ``lockdown`` — a pre-ruling total halt (``mode: estop``) still stops the carriers.

ISOLATION: a private ``HERMES_HOME`` per test (the contract the sibling estop suites use); nothing
here arms or reads live fleet state.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent import estop

#: The two release carriers, exactly as they sit in the ``default`` store today.
DEPLOY_TRAIN = {"id": "c21eb1835846", "name": "deploy-train"}
HERMES_UPDATE = {"id": "2106923395b8", "name": "hermes-update"}
ORDINARY = {"id": "deadbeefcafe", "name": "nightly-refresh"}


@pytest.fixture(autouse=True)
def run_context(monkeypatch):
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv(DELEGATED_CHILD_ENV_MARKER, raising=False)
    monkeypatch.delenv("YOYOFLOW_RUN_ID", raising=False)
    monkeypatch.delenv("HERMES_UPDATE_RUN_ID", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "platform-coder")
    monkeypatch.setenv("HERMES_ESTOP_ACTOR", "bot:hermes-update:RUN-0001")
    monkeypatch.setattr(estop, "_stdin_is_tty", lambda: False)


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nobody")
    return home


def _lockdown(**allow) -> estop.EstopState:
    """A live-lockdown state with the given ``allow`` entries (never touches disk)."""
    allow.setdefault("profiles", [])
    hold = {"mode": estop.MODE_LOCKDOWN, "allow": allow, "owner": "operator", "handle": "h"}
    return estop.EstopState(holds=(hold,))


def _total_halt() -> estop.EstopState:
    hold = {"mode": estop.MODE_ESTOP, "allow": {}, "owner": "operator", "handle": "h"}
    return estop.EstopState(holds=(hold,))


# ── the reserved carriers: structural, not sentinel-declared ────────────────────

def test_a_release_carrier_is_admitted_under_a_lockdown_that_never_named_it(hermes_home):
    """The measured defect: the window admits platform-stl alone; the carriers still dispatch."""
    state = _lockdown(profiles=["platform-stl"])

    assert estop.work_admitted("default", job=DEPLOY_TRAIN, state=state) is True
    assert estop.work_admitted("default", job=HERMES_UPDATE, state=state) is True


def test_an_ordinary_default_job_is_still_held(hermes_home):
    """The exemption is the two carriers, not the whole ``default`` lane (t_950de3e5 §4)."""
    state = _lockdown(profiles=["platform-stl"])

    assert estop.work_admitted("default", job=ORDINARY, state=state) is False
    assert estop.work_admitted("default", state=state) is False


def test_a_total_halt_still_stops_the_carriers(hermes_home):
    """A pre-ruling ``mode: estop`` halt keeps "no exemptions" — the operator's panic button."""
    state = _total_halt()

    assert estop.work_admitted("default", job=DEPLOY_TRAIN, state=state) is False
    assert estop.work_admitted("default", job=HERMES_UPDATE, state=state) is False
    assert estop.work_admitted("platform-stl", job=DEPLOY_TRAIN, state=state) is False


def test_the_carrier_match_is_on_the_id_or_the_name(hermes_home):
    """A row is recognised however it is labelled — id or name, case-folded."""
    assert estop.is_release_carrier({"id": "c21eb1835846", "name": "deploy-train"}) is True
    assert estop.is_release_carrier({"name": "DEPLOY-TRAIN"}) is True
    assert estop.is_release_carrier({"id": "2106923395b8"}) is False  # id alone does not match
    assert estop.is_release_carrier("hermes-update") is True
    assert estop.is_release_carrier(ORDINARY) is False


# ── the explicit job-level admission (allow.jobs) ───────────────────────────────

def test_allow_jobs_admits_one_named_row(hermes_home):
    state = _lockdown(profiles=["platform-stl"], jobs=["nightly-refresh"])

    assert estop.work_admitted("default", job=ORDINARY, state=state) is True
    assert estop.work_admitted("default", job={"name": "other-job"}, state=state) is False


def test_allow_jobs_matches_a_raw_row_id(hermes_home):
    state = _lockdown(profiles=["platform-stl"], jobs=["c21eb1835846"])

    assert estop.work_admitted("default", job=DEPLOY_TRAIN, state=state) is True


def test_a_lockdown_with_no_allow_grants_nothing_but_the_reserved_carriers(hermes_home):
    """The default stays fail-CLOSED: with no allow, only the engine's reserved carriers pass."""
    state = _lockdown(profiles=[])

    assert estop.work_admitted("default", state=state) is False
    assert estop.work_admitted("default", job=ORDINARY, state=state) is False
    assert estop.work_admitted("default", job=DEPLOY_TRAIN, state=state) is True


# ── allow.jobs survives the round trip through the sentinel ─────────────────────

def test_allow_jobs_is_persisted_on_the_arm_and_read_back(hermes_home):
    from agent import estop as _estop

    res = _estop.arm(mode=_estop.MODE_LOCKDOWN, reason="window",
                     allow={"profiles": ["platform-stl"], "jobs": ["deploy-train", "hermes-update"]},
                     ttl="30m")

    state = _estop.read_state()
    live = next(h for h in state.holds if h["handle"] == res.handle)
    assert live["allow"]["jobs"] == ["deploy-train", "hermes-update"]
    assert state.allow_jobs == frozenset({"deploy-train", "hermes-update"})
    assert state.as_dict()["allow"]["jobs"] == ["deploy-train", "hermes-update"]


def test_the_cli_exposes_allow_job():
    """`hermes pause --allow-job J` exists and reaches the allow payload (structural guard)."""
    import argparse

    from hermes_cli.subcommands.pause import build_pause_parser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    build_pause_parser(sub)

    args = parser.parse_args(["pause", "--lockdown", "--allow-profile", "platform-stl",
                              "--allow-job", "deploy-train"])
    assert args.allow_job == ["deploy-train"]
