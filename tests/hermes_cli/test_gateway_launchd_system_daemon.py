"""A root-owned launchd daemon IS this install's gateway — adopt it, never fight it.

A macOS host can supervise the gateway from ``/Library/LaunchDaemons/<label>.plist``: root-owned,
loaded into the machine-wide ``system`` domain, and reloadable only by root. Before this change the
install only ever looked at its own ``~/Library/LaunchAgents`` plist, so a host whose gateway ran under
such a daemon got:

* ``hermes gateway status`` describing a launchd-supervised gateway as "running manually, not as a
  system service" (the plist it probed does not exist), and ``_installed_service_kind_for`` returning
  None;
* ``hermes gateway restart`` falling through to the manual stop + foreground ``run_gateway``, which
  stamps the restart CLI's own PID into gateway.pid — every KeepAlive respawn then refuses with
  "Gateway already running (PID <restart>)" (#110637), the trap R1 closed for the inline-bootstrap
  case and this change closes for the daemon case;
* ``install``/``start``/``stop`` aiming at a competing ``~/Library/LaunchAgents/<label>.plist`` — two
  definitions for one label, both loaded at boot, both serving the same bot tokens.

The contract pinned here: launchd's own answer decides supervision and domain, the plist's pinned
home/tree decides whether the job is ours at all, the job is reported as installed + running under its
domain, restart hands the live process back to launchd (SIGUSR1 drain) and verifies a fresh PID in the
SAME domain, and no verb bootouts, bootstraps, kickstarts or rewrites a plist it does not own.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from types import SimpleNamespace

import pytest

import hermes_cli.gateway as gateway_cli
import hermes_cli.gateway_launchd as gateway_launchd

LABEL = "ai.hermes.gateway"
DAEMON_PID = 4242
FRESH_PID = 4343
MUTATING_VERBS = {"bootout", "bootstrap", "kickstart", "load", "unload"}


def _gateway_argv(tree) -> list[str]:
    return [f"{tree}/venv/bin/python", "-m", "hermes_cli.main", "gateway", "run", "--replace"]


class DaemonHost:
    """A macOS host with both launchd locations under ``tmp_path`` and controllable loaded domains.

    No test here reads or writes a real account's launchd directory, and no literal home path appears:
    the daemon plist is generated into ``tmp_path`` and pins the sandboxed ``HERMES_HOME``.
    """

    def __init__(self, monkeypatch, tmp_path):
        self.monkeypatch = monkeypatch
        self.tmp_path = tmp_path
        self.home = tmp_path / "hermes-home"
        self.home.mkdir()
        self.tree = tmp_path / "hermes-agent"
        self.agent_dir = tmp_path / "Library" / "LaunchAgents"
        self.daemon_dir = tmp_path / "Library" / "LaunchDaemons"
        self.domains: dict[str, int | None] = {}
        self.launchctl_calls: list[list[str]] = []
        self.sigusr1_calls: list[int] = []

        monkeypatch.setenv("HERMES_HOME", str(self.home))
        monkeypatch.setattr(
            gateway_launchd, "launchd_plist_dirs",
            lambda: [("agent", self.agent_dir), ("daemon", self.daemon_dir)],
        )
        monkeypatch.setattr(gateway_cli, "get_launchd_label", lambda: LABEL)
        monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: self.agent_plist)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: True)
        monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: False)
        monkeypatch.setattr(gateway_cli, "_systemd_unit_installed", lambda: False)
        monkeypatch.setattr(gateway_cli, "_launchd_print_service_pid", self._print_service_pid)
        self._real_run = subprocess.run
        monkeypatch.setattr(gateway_launchd.subprocess, "run", self._run)

    # ── the host's two seams: launchd's domain answers, and the launchctl CLI ────────────────────
    @property
    def agent_plist(self):
        return self.agent_dir / f"{LABEL}.plist"

    @property
    def daemon_plist(self):
        return self.daemon_dir / f"{LABEL}.plist"

    def write_daemon_plist(self, *, home=None, argv=None) -> None:
        """Install the root-owned daemon definition this install must adopt (or must refuse)."""
        self.daemon_plist.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "Label": LABEL,
            "ProgramArguments": argv if argv is not None else _gateway_argv(self.tree),
            "RunAtLoad": True,
            "KeepAlive": True,
        }
        if home is not None:
            data["EnvironmentVariables"] = {"HERMES_HOME": str(home)}
        self.daemon_plist.write_bytes(plistlib.dumps(data))

    def _print_service_pid(self, domain, label):
        if domain in self.domains:
            return (True, self.domains[domain])
        return (False, None)

    def _run(self, argv, **kwargs):
        if not argv or argv[0] != "launchctl":
            return self._real_run(argv, **kwargs)
        self.launchctl_calls.append(list(argv))
        verb = argv[1] if len(argv) > 1 else ""
        if verb in MUTATING_VERBS:
            raise AssertionError(
                f"launchctl {verb} must never reach a launchd job this install does not own: {argv}"
            )
        target = str(argv[2]) if len(argv) > 2 else ""
        for domain in self.domains:
            if target == f"{domain}/{LABEL}":
                pid = self.domains[domain]
                return SimpleNamespace(
                    returncode=0, stdout=f"\tpid = {pid}\n\tlast exit code = 0: OK\n", stderr=""
                )
        return SimpleNamespace(returncode=1, stdout="", stderr="Could not find service")

    def printed_targets(self) -> list[str]:
        return [call[2] for call in self.launchctl_calls if len(call) > 2 and call[1] == "print"]

    def supervise_daemon(self, pid: int = DAEMON_PID) -> None:
        self.domains["system"] = pid

    def drain_replaces_daemon(self, pid: int = FRESH_PID):
        """The SIGUSR1 hand-back a daemon host performs: launchd revives it on a fresh PID."""

        def _sigusr1(old_pid, budget, **kwargs):
            self.sigusr1_calls.append(old_pid)
            self.domains["system"] = pid
            return True

        self.monkeypatch.setattr(gateway_cli, "_graceful_restart_via_sigusr1", _sigusr1)
        self.monkeypatch.setattr(gateway_cli, "_get_restart_exit_wait_budget", lambda: 5.0)
        self.monkeypatch.setattr(gateway_cli, "probe_gateway_loop_liveness", lambda pid: "healthy")


@pytest.fixture
def daemon_host(monkeypatch, tmp_path) -> DaemonHost:
    host = DaemonHost(monkeypatch, tmp_path)
    host.write_daemon_plist(home=host.home)
    host.supervise_daemon()
    return host


# ── D1/D2: the locations table and launchd's domain answer ───────────────────────────────────────


def test_a_daemon_plist_belongs_to_the_system_domain():
    """D1/D2: one table names the locations and the domain kind each loads into."""
    kinds = [kind for kind, _dir in gateway_launchd.launchd_plist_dirs()]
    assert kinds == ["agent", "agent", "daemon"]
    assert gateway_launchd.launchd_domains_for_kind("daemon") == ("system",)
    uid = os.getuid()
    assert gateway_launchd.launchd_domains_for_kind("agent") == (f"gui/{uid}", f"user/{uid}")


def test_the_domain_probe_reads_the_system_domain(monkeypatch):
    """D2: ``system`` is probed, so a root-owned daemon is found where launchd put it."""
    targets: list[str] = []

    def fake_run(argv, **kwargs):
        targets.append(argv[2])
        if argv[2] == f"system/{LABEL}":
            return SimpleNamespace(returncode=0, stdout="")
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(gateway_launchd.subprocess, "run", fake_run)
    assert gateway_launchd._probe_launchd_domain_for_label(LABEL) == "system"
    uid = os.getuid()
    assert targets == [f"gui/{uid}/{LABEL}", f"user/{uid}/{LABEL}", f"system/{LABEL}"]


def test_the_user_domains_still_win_when_they_load_the_label(monkeypatch):
    """D2 order: ``gui/<uid>`` first, then ``user/<uid>``, and only then ``system``."""
    uid = os.getuid()
    loaded = f"gui/{uid}/{LABEL}"

    def fake_run(argv, **kwargs):
        if argv[2] == loaded:
            return SimpleNamespace(returncode=0, stdout="")
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(gateway_launchd.subprocess, "run", fake_run)
    assert gateway_launchd._probe_launchd_domain_for_label(LABEL) == f"gui/{uid}"


# ── D3: the trust rule — adopt only THIS install's gateway ───────────────────────────────────────


def test_a_daemon_job_for_this_install_is_adopted(daemon_host):
    job = gateway_launchd.launchd_gateway_job()

    assert job is not None
    assert (job.kind, job.domain, job.pid) == ("daemon", "system", DAEMON_PID)
    assert job.is_daemon is True
    assert job.is_own_plist is False  # the agent plist is ours; this daemon definition is not


def test_a_daemon_pointed_at_another_home_is_not_ours(daemon_host):
    """The fail-closed clause: a plist pinning a different HERMES_HOME is another tree's job."""
    daemon_host.write_daemon_plist(home=daemon_host.tmp_path / "some-other-tree")

    assert gateway_launchd.launchd_gateway_job() is None
    assert gateway_launchd.launchd_supervising_gateway_job() is None


def test_a_lookalike_job_at_our_label_is_not_adopted(daemon_host):
    """A same-label plist that does not run a Hermes gateway is foreign: report nothing, touch nothing."""
    daemon_host.write_daemon_plist(argv=["/bin/sleep", "6000"])

    assert gateway_launchd.launchd_gateway_job() is None


def test_a_malformed_plist_is_skipped_not_fatal(daemon_host):
    """A hand-edited operator plist must not abort the scan (ExpatError is not a ValueError)."""
    daemon_host.daemon_plist.write_text("<plist><dict><key>Label", encoding="utf-8")

    assert gateway_launchd.launchd_gateway_job() is None


def test_a_plist_without_a_pinned_home_needs_our_tree_in_its_program(daemon_host):
    """No pinned home ⇒ the program path must live in this install's own tree."""
    daemon_host.write_daemon_plist(argv=["/usr/local/other-tree/venv/bin/python", "-m", "hermes_cli.main", "gateway", "run"])
    assert gateway_launchd.launchd_gateway_job() is None

    daemon_host.write_daemon_plist(argv=_gateway_argv(gateway_launchd._gw().PROJECT_ROOT))
    job = gateway_launchd.launchd_gateway_job()
    assert job is not None and job.is_daemon is True


# ── D8/D2: what the install REPORTS ─────────────────────────────────────────────────────────────


def test_the_installed_kind_is_launchd_for_a_daemon_supervised_gateway(daemon_host):
    assert gateway_cli._installed_service_kind_for(lambda: False) == "launchd"
    assert gateway_cli._is_service_running() is True


def test_the_runtime_snapshot_names_the_system_daemon_scope(daemon_host):
    monkeypatch_pids = daemon_host.monkeypatch
    monkeypatch_pids.setattr(gateway_cli, "find_gateway_pids", lambda: [DAEMON_PID])

    snapshot = gateway_cli.get_gateway_runtime_snapshot()

    assert snapshot.service_installed is True
    assert snapshot.service_running is True
    assert snapshot.manager == "launchd (system daemon)"
    assert snapshot.has_process_service_mismatch is False


def test_a_supervised_daemon_prints_no_manual_foreground_warning(daemon_host, capsys):
    daemon_host.monkeypatch.setattr(gateway_cli, "find_gateway_pids", lambda: [DAEMON_PID])
    gateway_cli._print_gateway_process_mismatch(gateway_cli.get_gateway_runtime_snapshot())

    output = capsys.readouterr().out
    assert output == ""


def test_a_job_that_runs_nothing_names_the_daemon_not_a_manual_run(daemon_host, capsys):
    """The mismatch case: the job is defined, launchd runs nothing, a process is running anyway."""
    daemon_host.supervise_daemon(pid=None)
    daemon_host.monkeypatch.setattr(gateway_cli, "find_gateway_pids", lambda: [7777])

    snapshot = gateway_cli.get_gateway_runtime_snapshot()
    assert snapshot.has_process_service_mismatch is True
    gateway_cli._print_gateway_process_mismatch(snapshot)

    output = capsys.readouterr().out
    assert "launchd (system) supervises none" in output
    assert "manual foreground" not in output
    assert f"sudo launchctl kickstart -k system/{LABEL}" in output


def test_status_reports_the_daemon_and_who_may_change_it(daemon_host, capsys):
    daemon_host.monkeypatch.setattr(gateway_cli, "find_gateway_pids", lambda: [DAEMON_PID])

    gateway_launchd.launchd_status()

    output = capsys.readouterr().out
    assert str(daemon_host.daemon_plist) in output
    assert f"✓ Gateway is supervised by launchd (PID {DAEMON_PID})" in output
    assert "not this install's to rewrite" in output
    assert "hermes gateway start" not in output  # that verbs cannot load a root-owned daemon


# ── D5: restart hands the live process back to launchd, in the SAME domain ───────────────────────


def test_restart_drains_the_daemon_and_verifies_a_fresh_pid_in_the_same_domain(daemon_host, capsys):
    daemon_host.drain_replaces_daemon()

    gateway_launchd.launchd_restart()

    output = capsys.readouterr().out
    assert daemon_host.sigusr1_calls == [DAEMON_PID]
    # Every launchctl read went to the domain launchd supervises it in — never the agent domain.
    assert daemon_host.printed_targets() == [f"system/{LABEL}"]
    assert "✓ Service restart requested" in output
    assert "last exit code" in output  # launchd's account of the previous incarnation
    assert daemon_host.agent_plist.exists() is False  # never a competing agent definition


def test_restart_refuses_loudly_when_launchd_supervises_no_process(daemon_host, capsys):
    daemon_host.supervise_daemon(pid=None)
    daemon_host.drain_replaces_daemon()

    with pytest.raises(SystemExit) as exit_info:
        gateway_launchd.launchd_restart()

    assert exit_info.value.code == 1
    output = capsys.readouterr().out
    assert "launchd supervises no process" in output
    assert f"sudo launchctl kickstart -k system/{LABEL}" in output
    assert daemon_host.sigusr1_calls == []


def test_restart_refuses_when_launchd_does_not_revive_it(daemon_host, capsys):
    """A drain that leaves launchd running nothing is reported, never papered over."""
    daemon_host.monkeypatch.setattr(gateway_cli, "_graceful_restart_via_sigusr1", lambda pid, budget, **k: True)
    daemon_host.monkeypatch.setattr(gateway_cli, "_get_restart_exit_wait_budget", lambda: 5.0)
    daemon_host.monkeypatch.setattr(gateway_cli, "probe_gateway_loop_liveness", lambda pid: "healthy")
    daemon_host.monkeypatch.setattr(gateway_cli, "_wait_for_launchd_service_pid", lambda *a, **k: False)

    with pytest.raises(SystemExit) as exit_info:
        gateway_launchd.launchd_restart()

    assert exit_info.value.code == 1
    output = capsys.readouterr().out
    assert "is not supervising a new process" in output
    assert f"sudo launchctl kickstart -k system/{LABEL}" in output


# ── D4/D7: no verb fights a definition it does not own ───────────────────────────────────────────


def test_stop_refuses_instead_of_printing_a_false_success(daemon_host, capsys):
    with pytest.raises(SystemExit) as exit_info:
        gateway_launchd.launchd_stop()

    assert exit_info.value.code == 1
    output = capsys.readouterr().out
    assert "cannot stop it" in output
    assert "KeepAlive would relaunch it" in output


def test_install_never_writes_a_competing_agent_plist(daemon_host, capsys):
    gateway_launchd.launchd_install()

    output = capsys.readouterr().out
    assert daemon_host.agent_plist.exists() is False
    assert f"launchd is supervising it (PID {DAEMON_PID})" in output
    assert "No LaunchAgent was written for the same label" in output


def test_start_reports_the_daemon_instead_of_loading_an_agent(daemon_host, capsys):
    gateway_launchd.launchd_start()

    output = capsys.readouterr().out
    assert daemon_host.agent_plist.exists() is False
    assert "launchd is supervising it" in output


def test_uninstall_refuses_to_remove_a_daemon_it_cannot_boot_out(daemon_host, capsys):
    with pytest.raises(SystemExit) as exit_info:
        gateway_launchd.launchd_uninstall()

    assert exit_info.value.code == 1
    assert "cannot uninstall it" in capsys.readouterr().out


def test_a_label_launchd_loads_twice_is_a_conflict(daemon_host, capsys):
    """D2: two domains loading one label race for the same bot tokens — name both, never pick one."""
    uid = os.getuid()
    daemon_host.domains[f"gui/{uid}"] = 5555

    with pytest.raises(SystemExit) as exit_info:
        gateway_launchd.launchd_restart()

    assert exit_info.value.code == 1
    output = capsys.readouterr().out
    assert f"gui/{uid}" in output and "system" in output
    assert daemon_host.sigusr1_calls == []


# ── D6: restart must never reach a foreground run while a supervisor holds the gateway ───────────


@pytest.fixture
def restart_calls(daemon_host, monkeypatch):
    """Drive ``_cmd_restart`` past every earlier branch to the foreground fallback."""
    calls = {"foreground": False, "stopped": False}
    monkeypatch.setattr(gateway_cli, "_refuse_from_inside_gateway", lambda *a, **k: None)
    monkeypatch.setattr(gateway_cli, "_guard_named_profile_under_multiplexer", lambda **k: None)
    monkeypatch.setattr(gateway_cli, "_dispatch_via_service_manager_if_s6", lambda *a, **k: False)
    monkeypatch.setattr(gateway_cli, "_dispatch_all_via_service_manager_if_s6", lambda *a, **k: False)
    monkeypatch.setattr(gateway_cli, "stop_profile_gateway", lambda: calls.__setitem__("stopped", True) or True)
    monkeypatch.setattr(gateway_cli, "run_gateway", lambda **k: calls.__setitem__("foreground", True))
    from hermes_cli import gateway_profile_lifecycle

    monkeypatch.setattr(gateway_profile_lifecycle, "profile_lifecycle", lambda verb, args: False)
    return calls


def test_restart_never_reaches_a_foreground_run_behind_a_supervisor(daemon_host, restart_calls, capsys):
    """The last door (#110637): even when the kind probe misses the daemon, no ``run_gateway`` here."""
    daemon_host.monkeypatch.setattr(gateway_cli, "_installed_service_kind_for", lambda windows: None)

    with pytest.raises(SystemExit) as exit_info:
        gateway_cli._cmd_restart(SimpleNamespace(system=False, all=False, force=False))

    assert exit_info.value.code == 1
    assert restart_calls == {"foreground": False, "stopped": False}
    output = capsys.readouterr().out
    assert "Refusing to start a foreground gateway" in output
    assert "launchd (system daemon)" in output


def test_restart_routes_a_daemon_through_launchd_not_the_foreground_path(daemon_host, restart_calls, capsys):
    """With the daemon detected, the launchd hand-back IS the restart and the fallback is never reached."""
    daemon_host.drain_replaces_daemon()

    gateway_cli._cmd_restart(SimpleNamespace(system=False, all=False, force=False))

    assert restart_calls["foreground"] is False
    assert daemon_host.sigusr1_calls == [DAEMON_PID]
    assert "✓ Service restart requested" in capsys.readouterr().out
