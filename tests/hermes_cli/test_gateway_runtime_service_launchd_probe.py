"""The launchd discovery probe must survive a domain it cannot address.

Measured on a healthy host (t_484ee879): the gateway was loaded and running in launchd's
``system`` domain while ``launchctl print gui/<uid>/ai.hermes.gateway`` returned 125 ("Domain does
not support specified action") from the context the workers run in. The probe read that as
``service_manager_unavailable`` — a dead service manager — and every worker refused to start,
starving dispatch for days. 125 means "I cannot ask this domain", never "the service is gone".
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.platforms("macos")

from hermes_cli import gateway_runtime_service as service


def _dump(domain: str, label: str, home: Path, argv: list[str], *, state: str = "running") -> str:
    """A ``launchctl print <domain>/<label>`` body shaped like launchd's own."""
    kind = "LaunchDaemon" if domain == "system" else "LaunchAgent"
    lines = [f"{domain}/{label} = {{", "\ttype = " + kind,
             "\tpath = /Library/LaunchDaemons/" + label + ".plist", "\tstate = " + state,
             "\tprogram = " + argv[0], "\targuments = {"]
    lines += ["\t\t" + arg for arg in argv]
    lines += ["\t}", "\tenvironment = {", "\t\tHERMES_HOME => " + str(home),
              "\t\tHERMES_SUPERVISED_CHILD => 1", "\t}", "\tusername = owner", "}"]
    return "\n".join(lines) + "\n"


def _listing(label: str, argv: list[str], pid: int | None) -> str:
    """A ``launchctl list <label>`` body: launchd's old-style plist, with no domain in it."""
    parts = ["{", '\t"Label" = "' + label + '";']
    if pid is not None:
        parts.append('\t"PID" = ' + str(pid) + ";")
    parts += ['\t"Program" = "' + argv[0] + '";', '\t"ProgramArguments" = (']
    parts += ['\t\t"' + arg + '";' for arg in argv]
    parts += ["\t);", '\t"LastExitStatus" = 0;', "};"]
    return "\n".join(parts) + "\n"


class Host:
    """A fake launchctl boundary: per-domain print answers plus the domain-agnostic listing."""

    def __init__(self, monkeypatch, tmp_path: Path, answers: dict[str, object], listing=None):
        self.home = tmp_path / "hermes-home"
        self.home.mkdir(mode=0o700)
        monkeypatch.setenv("HERMES_HOME", str(self.home))
        import pwd
        monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path), pw_name="owner"))
        suffix = service.service_suffix(self.home)
        self.label = "ai.hermes.gateway" + ("-" + suffix if suffix else "")
        self.argv = [sys.executable, "-m", "hermes_cli.main", "gateway", "run", "--external-supervisor"]
        self.answers = answers
        self.listing = listing
        self.calls: list[list[str]] = []
        monkeypatch.setattr(service, "_run", self.run)

    def run(self, argv, _deadline=None, **_kwargs):
        self.calls.append(list(argv))
        if argv[1] == "print":
            answer = self.answers[argv[2].split("/")[0]]
            if isinstance(answer, int):
                return SimpleNamespace(returncode=answer, stdout="")
            return SimpleNamespace(returncode=0, stdout=answer)
        if argv[1] == "list":
            if self.listing is None:
                return SimpleNamespace(returncode=113, stdout="")
            return SimpleNamespace(returncode=0, stdout=self.listing)
        raise AssertionError(f"unexpected launchctl verb {argv!r}")

    def discover(self):
        return service.discover_existing_gateway_service(self.home, deadline=time.monotonic() + 5)


def test_a_domain_restricted_context_resolves_the_loaded_system_daemon(tmp_path, monkeypatch):
    """THE GUARDRAIL: gui print → 125, user print → 113, system print → the live daemon.

    The service is loaded and running; the probe must resolve it, not raise.
    """
    answers = {}
    host = Host(monkeypatch, tmp_path, answers)
    answers.update({
        "gui": 125,   # the domain is unaddressable from here — NOT a dead service manager
        "user": 113,  # addressed, job simply not loaded there
        "system": _dump(f"system/{host.label}", host.label, host.home, host.argv),
    })
    found = host.discover()
    assert found is not None, "a loaded, running gateway must resolve, not raise"
    assert found.backend == "launchd" and found.running is True
    assert found.start_argv == ("launchctl", "kickstart", f"system/{host.label}")
    assert [call[2] for call in host.calls if call[1] == "print"] == [
        f"gui/{os.getuid()}/{host.label}", f"user/{os.getuid()}/{host.label}", f"system/{host.label}"]


def test_125_alone_is_never_fatal_while_a_later_domain_answers(tmp_path, monkeypatch):
    """125 must skip, never raise: the loop carries the next domain precisely to be asked."""
    host = Host(monkeypatch, tmp_path, {})
    host.answers.update({
        "gui": 125,
        "user": 125,
        "system": _dump(f"system/{host.label}", host.label, host.home, host.argv),
    })
    found = host.discover()
    assert found is not None and found.running is True


def test_the_domain_agnostic_listing_proves_a_service_loaded_nowhere_print_can_reach(tmp_path, monkeypatch):
    """Every print unavailable, but the listing sees the job running: resolve it (pid liveness)."""
    host = Host(monkeypatch, tmp_path, {"gui": 125, "user": 125, "system": 125})
    host.listing = _listing(host.label, host.argv, os.getpid())
    found = host.discover()
    assert found is not None and found.running is True
    assert ["launchctl", "list", host.label] in host.calls


def test_a_loaded_job_no_domain_can_start_is_unaddressable_not_unavailable(tmp_path, monkeypatch):
    """Loaded but stopped, with no addressable domain: name that, do not call launchd dead."""
    host = Host(monkeypatch, tmp_path, {"gui": 125, "user": 125, "system": 125})
    host.listing = _listing(host.label, host.argv, None)
    with pytest.raises(service.RuntimeStartError) as raised:
        host.discover()
    assert raised.value.reason == "service_manager_unaddressable"
    assert raised.value.reason != "service_manager_unavailable"
    assert not any(call[1] in {"kickstart", "bootstrap"} for call in host.calls)


def test_no_domain_answers_and_no_job_is_registered_is_also_unaddressable(tmp_path, monkeypatch):
    """Nothing could be asked and the listing has no row: that is not "the service is absent"."""
    host = Host(monkeypatch, tmp_path, {"gui": 125, "user": 125, "system": 125})
    with pytest.raises(service.RuntimeStartError) as raised:
        host.discover()
    assert raised.value.reason == "service_manager_unaddressable"


def test_an_addressable_domain_reporting_no_job_still_means_no_service(tmp_path, monkeypatch):
    """A first run on a host with no gateway installed must stay a clean ``None``."""
    host = Host(monkeypatch, tmp_path, {"gui": 113, "user": 113, "system": 113})
    assert host.discover() is None


def test_the_listing_never_adopts_a_job_that_is_not_a_gateway(tmp_path, monkeypatch):
    """A listed job is only adopted when its argv is a ``gateway run`` for this install."""
    host = Host(monkeypatch, tmp_path, {"gui": 125, "user": 125, "system": 125})
    not_a_gateway = [sys.executable, "-m", "hermes_cli.main", "dashboard"]
    host.listing = _listing(host.label, not_a_gateway, os.getpid())
    with pytest.raises(service.RuntimeStartError) as raised:
        host.discover()
    assert raised.value.reason == "service_identity_unverified"


def test_the_listing_for_another_label_is_not_this_service(tmp_path, monkeypatch):
    """A listing that reports a different label is not evidence about our job."""
    host = Host(monkeypatch, tmp_path, {"gui": 113, "user": 113, "system": 113})
    host.listing = _listing("ai.hermes.gateway-somebodyelse", host.argv, os.getpid())
    assert host.discover() is None
