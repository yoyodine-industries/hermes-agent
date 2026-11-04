"""A receipt may say ``success`` only when the code it installed is PROVEN to be what its services
are serving (t_81e506a1).

The measured failure: the updater's restart phase left a gateway on the pre-update SHA, the fleet
matrix printed the stale row, and the run still finalized ``outcome: success`` -- because the
updater never consumed its own post-condition. ``success_with_stale_service`` is the vocabulary
for "the code landed, a declared service did not": it names the service and both SHAs, it is never
a silent ``success`` and never a ``failed`` update (contract C3 -- the tree is new, so the owed
restart is a follow-up, not a rollback).
"""

import contextlib
import io
import json
import os

import hermes_cli.update_cmd_fleet as fleet_mod
import hermes_cli.update_cmd_fleet_verify as fleet_verify
import hermes_cli.update_receipt as ur

OLD = "a" * 40
HEAD = "b" * 40


def _fleet_homes(monkeypatch, tmp_path, records: dict[str, dict]) -> None:
    """Default home + named profiles, each with a state file the identity resolver verifies as-is."""
    root = tmp_path / ".hermes"
    (root / "profiles").mkdir(parents=True)
    homes = {"default": root}
    for profile, record in records.items():
        home = root if profile == "default" else root / "profiles" / profile
        home.mkdir(exist_ok=True)
        homes[profile] = home
        (home / "gateway_state.json").write_text(json.dumps(record), encoding="utf-8")
    by_home = {str(home): records[profile]["pid"] for profile, home in homes.items()}
    monkeypatch.setattr("hermes_cli.version_info.get_code_identity",
                        lambda refresh=False: {"sha": HEAD, "version": "1.0"})
    monkeypatch.setattr("hermes_cli.profiles._get_default_hermes_home", lambda: root)
    monkeypatch.setattr("hermes_cli.profiles._get_profiles_root", lambda: root / "profiles")
    monkeypatch.setattr("gateway.control_socket.identify_gateway", lambda h, **k: None)
    monkeypatch.setattr("gateway.status.live_gateway_pid_for_home", lambda h: by_home.get(str(h)))
    monkeypatch.setattr(ur, "_gateway_code_root", lambda pid, home: None)
    # The tree this update installed IS ``HEAD``: the post-condition compares the rows against it.
    monkeypatch.setattr(fleet_mod, "_current_checkout_sha", lambda: HEAD)


def _run_verify(monkeypatch, restart) -> tuple[dict, str]:
    """Drive the real verify phase and return the receipt it finalized plus its stdout."""
    monkeypatch.setattr(fleet_verify, "_print_legacy_units_warning", lambda: None)
    monkeypatch.setattr("hermes_cli.update_cmd_maint._refresh_dashboard_after_update", lambda *a, **k: None)
    monkeypatch.setattr("hermes_cli.update_cmd._surviving_pre_update_serve_runtimes", lambda plan: [])
    monkeypatch.setattr(fleet_mod._time, "sleep", lambda s: None)
    monkeypatch.setattr(fleet_mod, "_clear_fleet_restart_pending_marker", lambda: None)
    monkeypatch.setattr("hermes_cli.gateway_migrate.maybe_auto_migrate_after_update", lambda: None)
    ur.begin_update_receipt()
    with contextlib.redirect_stdout(io.StringIO()) as out:
        fleet_verify._verify_fleet_after_update(
            restart, _pre_update_plan=None, _windows_gateway_resume=None, update_complete=True,
        )
    path = ur._receipt_dir() / "latest.json"
    return json.loads(path.read_text(encoding="utf-8")), out.getvalue()


def _restart(**overrides) -> "fleet_mod._GatewayRestartOutcome":
    fields = dict(
        incomplete=False, phase_errors=[], pre_restart_gateway_pids=[], restarted_services=[],
        failed_or_stale_units=[], relaunched_profiles=[], externally_supervised_profiles=[],
        killed_pids=set(), self_restart_pending_pids=set(),
    )
    fields.update(overrides)
    return fleet_mod._GatewayRestartOutcome(**fields)


def test_a_stale_service_downgrades_success_and_names_both_shas(monkeypatch, tmp_path):
    """Acceptance (a): the restart left a gateway on the old SHA -> non-success receipt naming it."""
    pid = os.getpid()
    _fleet_homes(monkeypatch, tmp_path, {
        "default": {"pid": pid, "gateway_state": "running", "code_sha": HEAD},
        "ops": {"pid": os.getppid(), "gateway_state": "running", "code_sha": OLD},
    })

    receipt, out = _run_verify(monkeypatch, _restart(
        incomplete=True, pre_restart_gateway_pids=[pid, os.getppid()],
        restarted_services=["hermes-gateway"], relaunched_profiles=["default"],
    ))

    assert receipt["outcome"] == ur.STALE_SERVICE_OUTCOME == "success_with_stale_service"
    verdict = receipt["served_code"]
    assert verdict["proven"] is False
    assert verdict["installed_sha"] == HEAD
    assert [(row["service"], row["state"], row["running_sha"], row["installed_sha"])
            for row in verdict["stale_services"]] == [("ops", "stale", OLD, HEAD)]
    # The operator-facing line names the service and both SHAs, and does not claim success.
    assert "service(s) are NOT serving it" in out
    assert "ops: serving aaaaaaaa" in out and "installed bbbbbbbb" in out
    assert "success_with_stale_service" in out
    # Contract C3: the code landed, so this is not a failed update process.
    assert ur.outcome_exit_code(receipt["outcome"]) == 0


def test_a_clean_fleet_still_finalizes_success(monkeypatch, tmp_path):
    """Acceptance (a), the other half: nothing stale -> the receipt is still a plain success."""
    pid = os.getpid()
    _fleet_homes(monkeypatch, tmp_path, {
        "default": {"pid": pid, "gateway_state": "running", "code_sha": HEAD},
    })

    receipt, out = _run_verify(monkeypatch, _restart(
        pre_restart_gateway_pids=[pid], restarted_services=["hermes-gateway"],
        relaunched_profiles=["default"],
    ))

    assert receipt["outcome"] == "success"
    assert receipt["served_code"] == {"installed_sha": HEAD, "proven": True, "stale_services": []}
    assert "NOT serving it" not in out


class TestServedCodeFailures:
    """The post-condition's own rules, stated once where the verify phase reads them."""

    def test_only_this_checkouts_stale_or_down_rows_fail_it(self):
        fleet = [
            {"profile": "ops", "pid": 2, "state": "stale", "code_sha": OLD, "code_version": "0.22.4"},
            {"profile": "work", "pid": 3, "state": "down"},
            {"profile": "default", "pid": 1, "state": "current", "code_sha": HEAD},
            # An unstamped service is no proof of a stale one.
            {"profile": "pending", "pid": 4, "state": "unknown"},
            # The gateway this updater runs inside can only restart after we exit.
            {"profile": "self", "pid": 5, "state": "restart_pending", "code_sha": OLD},
            # Another checkout is updated separately: it can never fail THIS run's post-condition.
            {"profile": "ext", "pid": 6, "state": "external", "code_sha": OLD},
        ]
        failures = ur.served_code_failures(fleet, installed_sha=HEAD)
        assert [(row["service"], row["state"]) for row in failures] == [("ops", "stale"), ("work", "down")]
        assert failures[0]["running_sha"] == OLD and failures[0]["installed_sha"] == HEAD
        assert failures[1]["service"] == "work"

    def test_no_fleet_is_not_a_failure(self):
        """An empty snapshot is a probe problem the matrix already owns, not a stale service."""
        assert ur.served_code_failures([], installed_sha=HEAD) == []
        assert ur.served_code_failures(None, installed_sha=HEAD) == []


class TestOutcomeVocabulary:
    def test_committed_covers_the_stale_outcome(self):
        assert ur.is_committed_outcome("success") is True
        assert ur.is_committed_outcome(ur.STALE_SERVICE_OUTCOME) is True
        assert ur.is_committed_outcome("partial") is True
        assert ur.is_committed_outcome("failed") is False
        assert ur.is_committed_outcome(None) is False

    def test_the_stale_outcome_exits_zero_but_partial_does_not(self):
        assert ur.outcome_exit_code("success") == 0
        assert ur.outcome_exit_code(ur.STALE_SERVICE_OUTCOME) == 0
        assert ur.outcome_exit_code("partial") == 1
        assert ur.outcome_exit_code("failed") == 1
