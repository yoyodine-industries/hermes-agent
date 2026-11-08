"""The critical-section hold (ESTOP): the end-to-end contract the update window depends on.

This is the proof surface for the critical-section hold — the engine's arm/lift, the CLI,
and the consumers (cron dispatch, kanban dispatch, new gateway turns) — as ONE suite that
a lane can run headless, deterministically, against a throwaway ``HERMES_HOME``.

    migrations/t_6c929d82   this suite (proof surface; proof of t_939b063a, re-filed)
    migrations/t_e4da9955   the leaked-hold CLASS — clauses 4, 6 and 7 are its falsifier
    t_b70753e9              the engine fix those red clauses point at
    t_7ec6ade9              the carrier fix (the wind-down arm path)
    t_0b3d7d87 / t_5ea8a0ce / t_a9e60d8a / t_bc67bdef / t_56a91192 / t_b6b7e788 / t_04b83dca
                            the real failures each clause is grounded in

THE PROPERTY
    An armed critical-section hold can never outlive its owning run: there is no reachable
    state where the guard is armed AND its owner is dead AND the guarded section is over,
    regardless of how the owning run failed.

CLAUSE COVERAGE — where each clause of the card's contract is locked down
    clause 1  arm is scoped ....................... cited (the four files named below)
    clause 2  the hold survives a gateway bounce .. tests here, engine + process boundary
    clause 3  release lifts exactly its own handle  cited (tests/cron/test_estop_lockdown.py)
    clause 4  a failed release cannot strand ....... tests here (RED today)
    clause 5  no advertised TTL that is not enforced tests here (RED today) + a green guard
    clause 6  every transition is observable ....... tests here: the explicit-release half
                                                     PASSES, the DEADMAN half is RED today
    clause 7  a leaked hold is detected ............ tests here (RED today)
    clause 8  is_engaged() fails HELD on a stat err  cited: tests/cron/test_estop.py::
                                                      test_is_engaged_fails_safe_on_stat_error

Clauses 1 and 3 are cited, never duplicated, and every citation is machine-checked in
`CITATIONS` below. Their covering tests live in `tests/cron/test_estop_lockdown.py`,
`tests/cron/test_estop_arm_fence.py`, `tests/gateway/test_estop_allowlist.py` and
`tests/hermes_cli/test_kanban_dispatch_lockdown.py` — all TRACKED on the committed home since
the override-generation bootstrap `37b2a9e7d5` (card `t_babd2b2e` replaced the print-only
`LIVE_LINE_ONLY_CITATIONS` tuple with that check). Resolvability is not greenness: the measured
red state of those tests, and the card converting them, sit beside `CITATIONS`.

THE FOUR RED CLAUSES ARE NOT SOFTENED
    Each opens with ``xfail(strict=True)`` naming its card. That is an ENFORCED expectation,
    not a pass: run ``--runxfail`` and the clause fails on today's bytes exactly as measured.
    ``strict=True`` means the moment an engine fix lands, the test XPASSes and the file turns
    RED again until the marker is deleted — so the marker cannot rot into a permanent excuse.
    Nothing is skipped, weakened or asserted-away.

ISOLATION (the card's DoD)
    Deterministic, headless, mutating NO live fleet state.  Every scenario runs in a fresh
    process with ``HOME``/``HERMES_HOME`` pointed at a private temp tree, so the engine's
    sentinel, its key, its ledger AND the fleet-root candidate path it always adds resolve
    inside that tree.  The ``isolation`` precondition asserts that and REFUSES to run
    otherwise.  A dispatched kanban worker is FENCED from arm/lift by ``arm_admission()``, so
    every arm happens in a subprocess with the delegation/kanban markers stripped — which is
    also how the update's own wind_down node runs (unattended, not a card worker).

    The engine under test is the checkout's own ``agent/estop.py`` — never a copy, never a
    mirror (``yaan-mirror-test-detection``). ``test_engine_under_test_is_the_repo_module``
    pins that and prints the sha256 of the bytes actually exercised.

RUN
    scripts/run_tests.sh tests/cron/test_estop_critical_section.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE = REPO_ROOT / "agent" / "estop.py"
SELF = Path(__file__).resolve()

# The owning run's actor handle and the exact reason string the 2026-10-10 wind-down arm used.
SIM_RUN = "bot:hermes-update:SIM-0000"
SIM_REASON = "wind-down hold, TTL 90m"
# The remaining LEGAL hold shape: scoped (lanes + a bounded lease). A `mode: estop` arm — the
# fleet-wide halt — is not a supported operation any more (operator ruling 2026-10-10, card
# t_e04b4c81), so every scenario here arms a lockdown and the clauses read THAT shape. The
# run-ownership/lease guarantees for it are  clauses 4/5 and the engine card t_b70753e9's.
_SCOPED = {"mode": "lockdown", "allow": {"profiles": ["platform-coder"]}}

_SCENARIO_ENV = "HERMES_ESTOP_TEST_SCENARIO"
_ROOT_ENV = "HERMES_ESTOP_TEST_ROOT"
_ARM_ENV = "HERMES_ESTOP_TEST_ARM"

# A dispatched card worker (HERMES_KANBAN_*) and a delegated child are FENCED from arm/lift by
# arm_admission(); the unattended markers deny the operator's name. All are stripped so each
# scenario is an admitted, unattended venue — the wind_down node's own standing.
_STRIPPED_ENV = (
    "HERMES_DELEGATED_CHILD_CONTEXT",
    "HERMES_KANBAN_TASK", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_WORKSPACE",
    "HERMES_CRON_JOB", "HERMES_CRON_JOB_ID", "HERMES_CRON_RUN",
    "YOYOFLOW_RUN_ID", "YOYOFLOW_WORKFLOW", "YOYOFLOW_NODE",
    "HERMES_UPDATE_WINDOW", "HERMES_UPDATE_RUN_ID",
)

# Clauses 1, 3 and 8 are covered by tests that already exist — cited, not duplicated. A
# citation that no longer resolves is a silent coverage gap, so ALL of them are checked below
# (card t_babd2b2e: clauses 1 and 3 used to sit in a print-only tuple because their covering
# tests had no tracked home; they are tracked now, so the check verifies them too).
#
# MEASURED STATE OF THE CITED TESTS (2026-10-11, card t_babd2b2e) — a citation RESOLVING is not
# the same as the cited test being GREEN. On the committed home three of the four below are
# RED: the pre-retirement `mode: estop` arming (EstopTotalHaltRetired x20 across the four
# files) and the retired notice shape; the dispatcher-seam ones additionally need the union
# generation t_41d413b4 (blocked) + deploy t_c8c846a7, and are 13/14 green on the converged
# estop line 62e09de9f5. The conversion is carded as t_dc664c58. The check below verifies
# RESOLVABILITY — what a clean checkout can prove — never that the cited bytes pass.
CITATIONS = (
    # (clause, path relative to the repo root, test function)
    ("clause 8  is_engaged() fails HELD on a stat error",
     "tests/cron/test_estop.py", "test_is_engaged_fails_safe_on_stat_error"),
    ("clause 1  arm is scoped",
     "tests/cron/test_estop_lockdown.py", "test_cli_pause_lockdown_arms_the_named_lanes"),
    ("clause 1  arm is scoped",
     "tests/gateway/test_estop_allowlist.py", "test_operator_identity_is_served_while_a_peer_is_refused"),
    ("clause 1  arm is scoped",
     "tests/hermes_cli/test_kanban_dispatch_lockdown.py", "test_an_allowlisted_lane_spawns_on_both_boards"),
    ("clause 3  release lifts exactly its own handle",
     "tests/cron/test_estop_lockdown.py", "test_release_by_owner_leaves_a_co_holders_entry_untouched"),
)


# --------------------------------------------------------------------------- #
# scenario plumbing — every arm/read happens in its own clean process
# --------------------------------------------------------------------------- #
def _child_env(root: Path) -> dict:
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")}
    for name in _STRIPPED_ENV:
        env.pop(name, None)
    env["HOME"] = str(root / "home")
    env["HERMES_HOME"] = str(root / "home" / ".hermes")
    env["HERMES_PROFILE"] = "platform-coder"
    env["HERMES_ESTOP_ACTOR"] = SIM_RUN
    env["PYTHONPATH"] = str(REPO_ROOT)
    env[_ROOT_ENV] = str(root)
    return env


def _run_scenario(root: Path, scenario: str, timeout: int = 120, **arm) -> dict:
    """Run ONE scenario in its own process and return its ``RESULT`` row."""
    env = _child_env(root)
    env[_SCENARIO_ENV] = scenario
    if arm:
        env[_ARM_ENV] = json.dumps(arm)
    proc = subprocess.run(
        [sys.executable, str(SELF)], env=env, cwd=str(REPO_ROOT),
        capture_output=True, text=True, timeout=timeout,
    )
    rows = [line for line in proc.stdout.splitlines() if line.startswith("RESULT ")]
    if not rows:
        tail = (proc.stderr.strip().splitlines() or ["<no stderr>"])[-1]
        return {"ok": False,
                "detail": f"scenario {scenario!r} crashed rc={proc.returncode}: {tail}"}
    return json.loads(rows[-1][len("RESULT "):])


# --------------------------------------------------------------------------- #
# the scenarios (each returns {"ok": bool, "detail": str})
# --------------------------------------------------------------------------- #
def _s_isolation(estop, root: Path, arm: dict) -> dict:
    """PRECONDITION: the engine resolves every path it writes INSIDE the temp tree."""
    paths = [str(p) for p in estop._candidate_sentinel_paths()]
    paths += [str(estop.sentinel_path()), str(estop.ledger_path())]
    outside = [p for p in paths if not p.startswith(str(root))]
    engine = Path(estop.__file__).resolve()
    return {"ok": not outside and engine == ENGINE.resolve(),
            "detail": f"sentinel/ledger paths={paths} outside={outside} | engine={engine}"}


def _s_arm(estop, root: Path, arm: dict) -> dict:
    res = estop.arm(actor=SIM_RUN, **_SCOPED, **arm)
    return {"ok": True, "detail": f"armed handle={res.handle} ttl_s={res.ttl_s!r}"}


def _s_deadman(estop, root: Path, arm: dict) -> dict:
    """clause 5, green half: a TTL that IS passed is enforced at read time."""
    res = estop.arm(owner="operator", reason="ttl clause", ttl="2s", actor=SIM_RUN, **_SCOPED)
    armed = estop.is_engaged()
    time.sleep(3)
    after = estop.is_engaged()
    rows = [r for r in estop.read_events() if r.get("handle") == res.handle]
    return {"ok": bool(armed) and not after,
            "detail": "ttl_s={!r} engaged={} engaged_after_ttl={} ledger_events={}".format(res.ttl_s, armed, after, [r.get("event") for r in rows])}


def _s_bounce_read(estop, root: Path, arm: dict) -> dict:
    """clause 2: a DIFFERENT process armed the hold and exited; this one must still see it.

    The update arms the guard, then restarts the gateway. A design that cleared holds on
    restart would let the guarded section run unprotected, so a red here is a
    release-on-restart regression, which the ruling forbids outright.
    """
    state = estop.read_state()
    return {"ok": bool(state.engaged) and not state.expired,
            "detail": f"engaged={state.engaged} holders={state.owners} total={state.total} expired={len(state.expired)} (armed by another process)"}


def _s_run_bound(estop, root: Path, arm: dict) -> dict:
    """clause 4 root cause: a run-owned arm carries an ENFORCED bound or a liveness binding."""
    res = estop.arm(owner="operator", reason=SIM_REASON, actor=SIM_RUN, **_SCOPED)
    state = estop.read_state()
    hold = next((h for h in state.holds if h["handle"] == res.handle), None)
    if hold is None:
        return {"ok": False, "detail": "the arm did not land at all"}
    has_deadline = bool(hold.get("expires_at"))
    ownership = sorted(k for k in hold if "run" in k.lower() or "pid" in k.lower())
    return {"ok": bool(has_deadline or ownership),
            "detail": "expires_at={!r} ttl_s={!r} ownership_fields={} -> the hold is UNBOUNDED and "
                      "names no owner a reaper could check".format(hold.get("expires_at"), res.ttl_s, ownership)}


def _s_explicit_release(estop, root: Path, arm: dict) -> dict:
    """clause 6, green half: an explicit release DOES land a row (t_56a91192's other half)."""
    res = estop.arm(owner="operator", reason="observable clause", actor=SIM_RUN, **_SCOPED)
    lifted = estop.release(handle=res.handle, actor=SIM_RUN)
    rows = [r for r in estop.read_events() if r.get("handle") == res.handle]
    lifts = [r for r in rows if str(r.get("event", "")).startswith(("release", "retire", "expire"))]
    return {"ok": bool(lifted.released) and bool(lifts),
            "detail": "released={} ledger_events={}".format(lifted.released, [r.get("event") for r in rows])}


def _s_deadman_row(estop, root: Path, arm: dict) -> dict:
    """clause 6, the DEADMAN half: a hold that self-lifted must leave a row too.

    The explicit-release half passes today; this one is why the log of the 2026-10-10 leak
    ends at ``arm``+``arm_renamed`` with no lift row at all.
    """
    res = estop.arm(owner="operator", reason="observable clause", ttl="2s", actor=SIM_RUN, **_SCOPED)
    time.sleep(3)
    estop.is_engaged()                       # the tick path that retires an expired hold
    rows = [r for r in estop.read_events() if r.get("handle") == res.handle]
    lifts = [r for r in rows if str(r.get("event", "")).startswith(("release", "retire", "expire"))]
    return {"ok": bool(lifts),
            "detail": "ledger_events=%s -> the deadman lift of this hold is INVISIBLE in "
                      ".estop-events.jsonl" % [r.get("event") for r in rows]}


def _s_leaked_detected(estop, root: Path, arm: dict) -> dict:
    """clauses 4 + 7: a leaked hold is DETECTED, or lifted, by machinery INDEPENDENT of the owner.

    Scenario, exactly like 2026-10-10: an unattended run armed the guard with NO ttl, the
    process then exited without reaching its release step, and the section is over. The guard
    must not still be pausing the fleet, and if it is, something must SAY SO.
    """
    state = estop.read_state()
    still = estop.is_engaged()
    rows = estop.read_events()
    orphan = [r for r in rows if str(r.get("cause") or "") == "orphan"]
    return {"ok": (not still) or (state.defect is not None) or bool(orphan),
            "detail": (f"owner={SIM_RUN} exited without releasing (no ttl); is_engaged()={still} "
                       f"defect={state.defect!r} orphan_rows={len(orphan)} holders={state.owners} "
                       "-> nothing detects the leak; the fleet stays paused indefinitely")}


def _s_advertised_ttl(estop, root: Path, arm: dict) -> dict:
    """clause 5: an arm must not ADVERTISE a TTL it does not record (t_bc67bdef F1).

    Tonight's wind-down arm put ``TTL 90m`` in the reason PROSE and passed no ``--ttl``, so the
    ledger's arm row has no ``ttl_s`` key at all and the hold carries no deadline.
    """
    reason = str(arm.get("reason") or SIM_REASON)
    res = estop.arm(owner="operator", reason=reason, actor=SIM_RUN, **_SCOPED)
    row = next((r for r in estop.read_events()
                if r.get("event") == "arm" and r.get("handle") == res.handle), None)
    hold = next((h for h in estop.read_state().holds if h["handle"] == res.handle), None)
    advertised = bool(re.search(r"TTL\s*\d", str((row or {}).get("reason") or reason)))
    bounded = bool(row and row.get("ttl_s")) or bool(hold and hold.get("expires_at"))
    return {"ok": (not advertised) or bounded,
            "detail": "reason advertises a TTL={} but arm_row_ttl_s={!r} expires_at={!r} -> the arm "
                      "claims a bound it never records".format(advertised, (row or {}).get("ttl_s"), (hold or {}).get("expires_at"))}


_SCENARIOS = {
    "isolation": _s_isolation,
    "arm": _s_arm,
    "deadman": _s_deadman,
    "bounce_read": _s_bounce_read,
    "run_bound": _s_run_bound,
    "explicit_release": _s_explicit_release,
    "deadman_row": _s_deadman_row,
    "leaked_detected": _s_leaked_detected,
    "advertised_ttl": _s_advertised_ttl,
}


def _child_main() -> int:
    """The scenario child: re-exec of this file, one scenario, one RESULT row."""
    root = Path(os.environ[_ROOT_ENV])
    sys.path.insert(0, str(REPO_ROOT))
    from agent import estop

    scenario = os.environ.get(_SCENARIO_ENV, "")
    runner = _SCENARIOS.get(scenario)
    if runner is None:
        print("RESULT " + json.dumps({"ok": False, "detail": f"unknown scenario {scenario!r}"}))
        return 0
    try:
        result = runner(estop, root, json.loads(os.environ.get(_ARM_ENV) or "{}"))
    except BaseException as exc:  # a scenario fault is a FAILURE, never a silent pass
        result = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    print("RESULT " + json.dumps(result))
    return 0


if __name__ == "__main__" and os.environ.get(_SCENARIO_ENV):
    sys.exit(_child_main())


# --------------------------------------------------------------------------- #
# the refusing precondition
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session", autouse=True)
def _engine_paths_stay_inside_a_temp_tree(tmp_path_factory):
    """REFUSE to run if the engine would touch a path outside a throwaway tree."""
    probe = tmp_path_factory.mktemp("estop-isolation")
    (probe / "home" / ".hermes").mkdir(parents=True, exist_ok=True)
    result = _run_scenario(probe, "isolation")
    if not result.get("ok"):
        pytest.exit("REFUSING TO RUN the critical-section suite: {}".format(result.get("detail")),
                    returncode=2)
    yield


@pytest.fixture
def root(tmp_path):
    """A private HOME + HERMES_HOME for one scenario; nothing outside it is ever touched."""
    (tmp_path / "home" / ".hermes").mkdir(parents=True, exist_ok=True)
    return tmp_path


# --------------------------------------------------------------------------- #
# the engine under test is the repo's own module (mirror guard)
# --------------------------------------------------------------------------- #
def test_engine_under_test_is_the_repo_module():
    """The bytes exercised are THIS checkout's ``agent/estop.py`` — never a copy or a mirror."""
    from agent import estop

    assert Path(estop.__file__).resolve() == ENGINE.resolve(), (
        f"the suite imported {estop.__file__}, not this checkout's {ENGINE}")
    digest = hashlib.sha256(ENGINE.read_bytes()).hexdigest()
    print(f"engine under test: {ENGINE} sha256={digest}")
    assert len(digest) == 64


def test_isolation_precondition_holds_in_this_environment(root):
    """clause ISOLATION: the engine resolves its sentinel, key and ledger inside the temp tree."""
    result = _run_scenario(root, "isolation")
    assert result["ok"], result["detail"]


# --------------------------------------------------------------------------- #
# clause 2 — the hold SURVIVES a gateway bounce (green: must keep passing)
# --------------------------------------------------------------------------- #
def test_clause2_the_hold_survives_a_process_bounce(root):
    armed = _run_scenario(root, "arm", reason=SIM_REASON)
    assert armed["ok"], armed["detail"]
    after = _run_scenario(root, "bounce_read")
    assert after["ok"], "the hold did not survive the bounce: {}".format(after["detail"])


# --------------------------------------------------------------------------- #
# clause 5 — no advertised TTL that is not enforced
# --------------------------------------------------------------------------- #
def test_clause5_a_ttl_that_is_passed_is_enforced_at_read_time(root):
    """A supplied TTL bounds the hold and self-lifts it. (Passes today — keep it passing.)"""
    result = _run_scenario(root, "deadman", timeout=180)
    assert result["ok"], result["detail"]


@pytest.mark.xfail(strict=True, reason=(
    "clause 5 on the ARM PATH: the wind-down arm advertises 'TTL 90m' in its reason and records "
    "no ttl_s at all (t_bc67bdef F1 / 2026-10-10 arm). Fix: engine t_b70753e9, carriers t_7ec6ade9."))
def test_clause5_no_ttl_may_be_advertised_without_being_recorded(root):
    result = _run_scenario(root, "advertised_ttl", reason=SIM_REASON)
    assert result["ok"], result["detail"]


# --------------------------------------------------------------------------- #
# clause 6 — every transition is observable
# --------------------------------------------------------------------------- #
def test_clause6_an_explicit_release_writes_a_ledger_row(root):
    """The half that already works: a real release lands a row. (Passes today.)"""
    result = _run_scenario(root, "explicit_release")
    assert result["ok"], result["detail"]


def test_clause6_a_deadman_lift_writes_a_ledger_row(root):
    """A hold that self-lifted must leave a row too.

    This was the measured gap behind the 2026-10-10 leak (a log ending at ``arm``): the
    retirement card's R3 (t_e04b4c81) writes it, so the marker that named this clause as RED
    is gone — per this file's own doctrine, an XPASS means delete the marker.
    """
    result = _run_scenario(root, "deadman_row", timeout=180)
    assert result["ok"], result["detail"]


# --------------------------------------------------------------------------- #
# clauses 4 + 7 — the leaked-hold class (the falsifier of t_e4da9955)
# --------------------------------------------------------------------------- #
@pytest.mark.xfail(strict=True, reason=(
    "clause 4 root cause: a run-owned arm lands UNBOUNDED and names no owner a reaper could "
    "check — expires_at=None, ttl_s=None, no ownership field. Engine card t_b70753e9."))
def test_clause4_a_run_owned_arm_is_bounded_or_liveness_bound(root):
    result = _run_scenario(root, "run_bound")
    assert result["ok"], result["detail"]


@pytest.mark.xfail(strict=True, reason=(
    "clauses 4+7: owner DEAD, section over, and the fleet is still paused with no defect named "
    "and no orphan release row — 2026-10-10 reproduced (t_0b3d7d87). Engine card t_b70753e9."))
def test_clause4_and_7_a_hold_whose_owner_died_does_not_strand_the_fleet(root):
    armed = _run_scenario(root, "arm", reason=SIM_REASON)
    assert armed["ok"], armed["detail"]
    result = _run_scenario(root, "leaked_detected")
    assert result["ok"], result["detail"]


# --------------------------------------------------------------------------- #
# the cited clauses — a citation that no longer resolves is a silent gap
# --------------------------------------------------------------------------- #
def test_covering_tests_for_the_cited_clauses_exist():
    """clauses 1, 3 and 8 are covered elsewhere; every citation must resolve in THIS tree.

    A TRACKED-home check, not a greenness check: it proves a clean checkout carries the cited
    bytes (card t_babd2b2e). The cited tests' own pass/fail state is named beside `CITATIONS`
    and carded as t_dc664c58.
    """
    missing = []
    for clause, path, name in CITATIONS:
        source = REPO_ROOT / path
        if not source.is_file():
            missing.append(f"{clause}: {path} is absent")
            continue
        if (f"def {name}(") not in source.read_text(encoding="utf-8"):
            missing.append(f"{clause}: {path} no longer defines {name}")
    assert not missing, "a cited covering test no longer resolves: {}".format("; ".join(missing))
