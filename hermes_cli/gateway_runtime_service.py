"""Non-installing service-manager operations for an explicit canonical profile.

The legacy start helpers refresh units, regenerate plists, or fall back to
unmanaged processes. Client startup must not call those helpers.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
import hashlib
import io
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import time
from xml.parsers.expat import ExpatError

from gateway.runtime_contract import RuntimeState
from hermes_cli._subprocess_compat import windows_hide_flags


class RuntimeStartError(RuntimeError):
    def __init__(self, reason: str, state: RuntimeState = "inaccessible", *, detail: str | None = None):
        # ``reason`` is the stable code callers match on; ``detail`` is what the user reads.
        super().__init__(reason)
        self.reason, self.state = reason, state
        # One line, bounded: Desktop prints the first line of ``detail`` after the reason code. It echoes
        # service definitions and child output that users paste into reports, so redact before truncating.
        if detail:
            from agent.redact import redact_sensitive_text
            detail = " ".join(redact_sensitive_text(detail, force=True, redact_url_credentials=True).split())[:500]
        self.detail = detail or None


def remaining(deadline: float) -> float:
    budget = deadline - time.monotonic()
    if budget <= 0:
        raise TimeoutError
    return budget


def service_suffix(home: Path) -> str:
    """Host-service suffix for the REQUESTED home, never the initiating client's env.

    Same rule as ``hermes_cli.gateway._profile_suffix``: only the platform-native
    default home owns the bare unit name; ``<root>/profiles/<name>`` yields the
    profile name; any other root (Docker, a temp harness) yields a path hash so a
    custom root can never resolve to the production ``hermes-gateway`` unit.
    """
    from hermes_constants import account_default_hermes_home
    from hermes_cli.gateway import _profile_name_from_home
    default = account_default_hermes_home().resolve()
    home = home.resolve()
    if home == default:
        return ""
    root = default
    if not home.is_relative_to(default):
        root = home.parent.parent if home.parent.name == "profiles" else home
    if home != root:
        name = _profile_name_from_home(home, root)
        if name:
            return name
    return hashlib.sha256(str(home).encode()).hexdigest()[:8]


@dataclass(frozen=True)
class ExistingService:
    backend: str
    start_argv: tuple[str, ...]
    running: bool = False


def _run(argv: list[str] | tuple[str, ...], deadline: float, *, encoding: str | None = "utf-8"):
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                          text=encoding is not None, encoding=encoding, errors="strict" if encoding else None,
                          creationflags=windows_hide_flags(), timeout=remaining(deadline), check=False)


def _verify_binding(verify, *args, **kwargs):
    try:
        return verify(*args, **kwargs)
    except (ValueError, TypeError, KeyError, AttributeError, ExpatError) as exc:
        reason = str(exc)
        if reason in {"profile_mismatch", "service_account_mismatch", "service_disabled"}:
            raise RuntimeStartError(reason) from None
        raise RuntimeStartError("service_identity_unverified", detail=_unverified_detail(exc)) from None


def _unverified_detail(exc: Exception) -> str:
    return getattr(exc, "check", None) or f"{type(exc).__name__} while reading the service definition: {exc}"


def _child_failure(argv: list[str] | tuple[str, ...], code: int, output: str) -> str:
    first = next((line.strip() for line in (output or "").splitlines() if line.strip()), "no output")
    return f"{' '.join(argv)} exited {code}: {first}"


def _exists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _systemd(home: Path, deadline: float) -> ExistingService | None:
    from hermes_cli import gateway as gw
    from hermes_cli.service_manager import _s6_running
    from hermes_cli.gateway_runtime_service_identity import ForeignRoot, SYSTEMD_IDENTITY_PROPERTIES, verify_systemd
    if _s6_running():
        raise RuntimeStartError("external_supervisor")
    suffix = service_suffix(home)
    unit = f"hermes-gateway{'-' + suffix if suffix else ''}.service"
    paths = (Path.home() / ".config/systemd/user" / unit, Path("/etc/systemd/system") / unit)
    # On non-systemd hosts there is no manager to own transient units. Installed
    # definitions still count: a broken manager is not permission to bypass one.
    # Every Hermes entrypoint ensures the gateway, so a host with no installed unit must not
    # touch the service manager at all (a headless runner has no user bus to answer it).
    # With a unit installed anywhere, both scopes are asked: two claiming the name is a conflict.
    installed = (*paths, Path("/usr/lib/systemd/system") / unit, Path("/lib/systemd/system") / unit)
    if not any(_exists(p) for p in installed):
        return None
    found = []
    for system in (False, True):
        command = gw._systemctl_cmd(system)
        result = _run([*command, "show", unit, "--no-pager", "--all",
                       "--property=LoadState,ActiveState,SubState,UnitFileState," + ",".join(SYSTEMD_IDENTITY_PROPERTIES)], deadline)
        props = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if not _exists(paths[int(system)]) and (result.returncode or props.get("LoadState") == "not-found"):
            # Nothing installed in this scope: an unreachable or empty manager there has no
            # unit that could serve or block this home.
            continue
        if result.returncode or props.get("LoadState") != "loaded":
            raise RuntimeStartError("service_manager_unavailable")
        if props.get("UnitFileState") in {"masked", "masked-runtime"}:
            raise RuntimeStartError("service_masked")
        state = props.get("ActiveState")
        if state == "deactivating":
            raise RuntimeStartError("service_draining", "draining")
        if state not in {"inactive", "active", "activating", "reloading", "failed"}:
            raise RuntimeStartError("service_state_unknown")
        if state == "failed":
            raise RuntimeStartError("service_failed")
        environment = _run([*command, "show-environment"], deadline)
        if environment.returncode:
            raise RuntimeStartError("service_identity_unverified", detail=_child_failure(
                [*command, "show-environment"], environment.returncode, environment.stderr or environment.stdout))
        try:
            verify_systemd(props, environment.stdout, home, system=system)
        except ForeignRoot:
            # A unit named like ours but pinned to another HOME root belongs to a
            # different install (a sandbox HOME beside a real one); it neither serves
            # nor blocks this home.
            continue
        except ValueError as exc:
            reason = str(exc)
            if reason in {"profile_mismatch", "service_account_mismatch"}:
                raise RuntimeStartError(reason) from None
            raise RuntimeStartError("service_identity_unverified", detail=_unverified_detail(exc)) from None
        found.append(ExistingService("systemd", tuple([*command, "--no-ask-password", "start", unit]),
                                     state != "inactive"))
    if len(found) > 1:
        raise RuntimeStartError("service_scope_conflict", "conflict")
    return found[0] if found else None


def _s6(home: Path, deadline: float) -> ExistingService | None:
    """Find the container gateway slot that owns ``home``.

    TUI clients call ``ensure_gateway_runtime`` inside the Docker image too.
    They must request that s6 starts the registered slot, rather than falling
    through the Linux systemd probe and treating s6 as inaccessible.
    """
    from hermes_cli.service_manager import S6ServiceManager
    from hermes_cli.gateway_runtime_multiplex import implied_host_root

    # with-contenv is also the service run script's authority. The initiating
    # client may have selected a named profile or a different install root.
    try:
        container_home = Path("/run/s6/container_environment/HERMES_HOME").read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError) as exc:
        raise RuntimeStartError("service_identity_unverified",
                                detail=f"cannot read the container's HERMES_HOME: {exc}") from None
    if not container_home:
        raise RuntimeStartError("service_identity_unverified", detail="the container's HERMES_HOME is empty")
    root = Path(container_home).resolve()
    home = home.resolve()
    if home != root and home.parent != root / "profiles":
        raise RuntimeStartError("profile_mismatch")
    # A normal named profile belongs to the root multiplexer. Returning None
    # lets ensure select its host, including a first boot with no saved verdict.
    if home != root and implied_host_root(home) == root:
        return None
    profile = "default" if home == root else home.name
    manager = S6ServiceManager()
    service_name = f"gateway-{profile}"
    if not (manager.scandir / service_name).is_dir():
        raise RuntimeStartError("service_manager_unavailable")
    return ExistingService("s6", (service_name,), manager.is_running(service_name))


# ``launchctl print`` exit codes that mean "the job is NOT loaded in the domain I asked": 3 and 113
# are launchctl's absent-service codes, and 125 ("Domain does not support specified action") means
# the domain itself cannot be addressed from this process at all — e.g. an Aqua ``gui/<uid>`` probed
# from a LaunchDaemon's context on macOS 26+. None of the three says anything about the service
# manager, so each one SKIPS to the next domain: reading 125 as fatal turned "I cannot ask this
# domain" into "the gateway is gone" and starved every worker on a healthy host (t_484ee879).
_LAUNCHD_NOT_LOADED_CODES = frozenset({3, 113, 125})


def _launchd_domains(uid: int) -> tuple[str, ...]:
    """Every domain a Hermes gateway job can be loaded into, in probe order (``windows-footgun: ok``
    — native launchd only): ``gui/<uid>`` (Aqua LaunchAgent), ``user/<uid>`` (Background/SSH
    LaunchAgent), then ``system`` (a LaunchDaemon, including one whose ``UserName`` is a non-root
    user). Mirrors ``gateway.restart._launchd_probe_domains``: a probe that only asks the two
    per-user domains cannot see that daemon at all, and a host that installs one gets no answer."""
    return (f"gui/{uid}", f"user/{uid}", "system")


def _launchd_listed_job(output: str, label: str) -> tuple[int | None, list[str]] | None:
    """``launchctl list <label>``'s old-style plist as ``(pid, argv)``, or None when it does not
    report ``label``. That listing answers from the caller's own bootstrap namespace, so it reaches
    a job loaded in a domain that ``print`` cannot address from here — evidence the service is
    LOADED, which is never evidence the manager is dead."""
    if not re.search(rf'^\t"Label" = "?{re.escape(label)}"?;$', output, re.MULTILINE):
        return None
    pid = re.search(r'^\t"PID" = (\d+);$', output, re.MULTILINE)
    block = re.search(r'^\t"ProgramArguments" = \(\n(.*?)^\t\);$', output, re.MULTILINE | re.DOTALL)
    argv = [item.replace('\\"', '"').replace("\\\\", "\\")
            for item in re.findall(r'^\t\t"(.*)";$', block.group(1), re.MULTILINE)] if block else []
    return (int(pid.group(1)) if pid else None), argv


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)  # windows-footgun: ok — reached only from _launchd
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # the process exists; it is just not ours to signal
    except OSError:
        return False
    return True


def _launchd(home: Path, deadline: float) -> ExistingService | None:
    import pwd
    suffix = service_suffix(home)
    label = f"ai.hermes.gateway{'-' + suffix if suffix else ''}"
    account = pwd.getpwuid(os.getuid())  # windows-footgun: ok — native launchd only
    account_home = Path(account.pw_dir)
    plist = account_home / "Library/LaunchAgents" / f"{label}.plist"
    from hermes_cli.gateway_runtime_service_identity import (
        verify_gateway_argv, verify_launchd_loaded, verify_launchd_plist, read_definition)
    installed = _exists(plist)
    domains = _launchd_domains(os.getuid())
    found = []
    loaded = []
    unaddressable = 0
    for domain in domains:
        result = _run(["launchctl", "print", f"{domain}/{label}"], deadline)
        if result.returncode == 0:
            loaded.append(result.stdout)
            found.append(ExistingService("launchd", ("launchctl", "kickstart", f"{domain}/{label}"),
                                         "state = running" in result.stdout))
            continue
        if result.returncode in _LAUNCHD_NOT_LOADED_CODES:
            unaddressable += result.returncode == 125
            continue
        # Any other failure keeps its uncertainty: launchctl could not answer for a reason that
        # says nothing about the job.
        raise RuntimeStartError("service_manager_unavailable")
    if len(found) > 1:
        raise RuntimeStartError("service_scope_conflict", "conflict")
    if found:
        _verify_binding(verify_launchd_loaded, loaded[0], home, uid=os.getuid(), username=account.pw_name)  # windows-footgun: ok — native launchd only
        return found[0]
    # No domain that answered is running the job. Before concluding it is absent, ask the listing
    # that is NOT domain-explicit: it reaches a job loaded in a domain ``print`` cannot address.
    listed = _run(["launchctl", "list", label], deadline)
    job = _launchd_listed_job(listed.stdout or "", label) if listed.returncode == 0 else None
    if job is not None:
        pid, argv = job
        # The same argv binding a print dump gets: a loaded job that is not THIS install's gateway
        # is refused, never adopted.
        _verify_binding(verify_gateway_argv, argv, home)
        if pid is not None and _pid_alive(pid):
            return ExistingService("launchd", ("launchctl", "kickstart", f"{domains[0]}/{label}"), True)
        # Loaded but not running, and no answering domain could start it. Saying "the manager is
        # unavailable" here would report a healthy launchd as dead; bootstrapping a second owner
        # beside a job we can see but not address would be worse.
        raise RuntimeStartError("service_manager_unaddressable", detail=(
            f"{label} is loaded in launchd (pid {pid if pid is not None else 'none'}) but no domain "
            f"answered `launchctl print` ({', '.join(domains)}), so it cannot be started from here"))
    if installed:
        definition = _verify_binding(plistlib.loads, read_definition(plist))
        _verify_binding(verify_launchd_plist, definition, label, home)
        # Load ONLY the existing file, never bootout/rewrite or kickstart -k.
        return ExistingService("launchd", ("launchctl", "bootstrap", domains[0], str(plist)))
    if unaddressable == len(domains):
        # Not one domain could be asked: that is not "no service", and the caller must not read it
        # as a dead gateway (nor spawn an owner beside one it cannot see).
        raise RuntimeStartError("service_manager_unaddressable", detail=(
            f"no launchd domain answered `launchctl print` ({', '.join(domains)}): launchctl cannot "
            "address the service manager from this context"))
    return None


def _windows_task_name(home: Path) -> str:
    suffix = service_suffix(home)
    return f"Hermes_Gateway{'_' + suffix if suffix else ''}"


def refresh_ready_owner_launcher(home: Path, observed) -> None:
    """Repair a ready owner's logon launcher (``refresh_dead_launcher``). Ensure returns a ready owner
    before discovery, yet one can run on the new payload while its launcher still names the deleted
    interpreter: started by ``hermes gateway start``, or the old payload removed once it outlived it."""
    if observed.state != "ready" or sys.platform != "win32":
        return
    from hermes_cli.gateway_runtime import control_home_for
    from hermes_cli.gateway_windows import refresh_dead_launcher
    owner = control_home_for(home, observed.endpoint)
    refresh_dead_launcher(owner, _windows_task_name(owner))


def _windows(home: Path, deadline: float) -> ExistingService | None:
    from hermes_cli.gateway_windows import _decode_schtasks_output, refresh_dead_launcher
    name = _windows_task_name(home)
    # Before the task runs it, and for the next logon: a Store update deletes the launcher's interpreter.
    refresh_dead_launcher(home, name)

    def query(refusal: str, *argv: str) -> str:
        # schtasks/whoami write the console code page, and /XML declares UTF-16 over those bytes:
        # decode like the drift check does (#116193) instead of the locale codec or raw bytes (#139083).
        result = _run(list(argv), deadline, encoding=None)
        if result.returncode:
            output = _decode_schtasks_output(result.stderr or result.stdout or b"")
            raise RuntimeStartError(refusal, detail=_child_failure(argv, result.returncode, output))
        return _decode_schtasks_output(result.stdout or b"")

    rows = csv.reader(io.StringIO(query("service_manager_unavailable", "schtasks.exe", "/Query", "/FO", "CSV", "/NH")))
    # Orphaned TaskCache entries print localized one-field diagnostics among the rows (#142140). Only a
    # well-formed row can name our task, so skipping the rest can only fall through to the unmanaged path.
    if any(len(row) == 3 and row[0].lstrip("\\") == name for row in rows):
        from hermes_cli.gateway_runtime_service_identity import verify_windows_task
        unverified = "service_identity_unverified"
        definition = query(unverified, "schtasks.exe", "/Query", "/TN", name, "/XML")
        accounts = list(csv.reader(io.StringIO(query(unverified, "whoami.exe", "/USER", "/FO", "CSV", "/NH"))))
        if len(accounts) != 1 or len(accounts[0]) != 2:
            raise RuntimeStartError("service_identity_unverified", detail=(
                f"whoami.exe /USER printed {len(accounts)} row(s) of {[len(row) for row in accounts]} field(s), "
                "expected one account and SID"))
        _verify_binding(verify_windows_task, definition, home, *accounts[0])
        return ExistingService("windows", ("schtasks.exe", "/Run", "/TN", name))
    # A Startup-folder entry (the fallback when Scheduled Task install was declined/blocked) is login
    # persistence only, with no start verb or identity to verify. Starting a stopped owner is the
    # same detached direct spawn manual `hermes gateway start` uses beside it; the unmanaged path
    # pins the explicit home and refuses a job-bound (no-breakaway) child, and the runtime lock
    # plus host attach arbitrate against the entry firing again at the next login.
    return None


def _no_supervisor(home: Path, deadline: float) -> None:
    """Android/Termux has no service manager (main refuses `gateway install/start` there and says
    "Run manually: hermes gateway"): an unmanaged detached start is its only owner. Python 3.13+
    reports ``sys.platform == "android"``; older Termux interpreters report linux and reach the
    systemd probe, which finds no unit and lands on the same unmanaged start."""


def discover_existing_gateway_service(home: Path, *, deadline: float) -> ExistingService | None:
    from hermes_cli.service_manager import _s6_running

    backend = _s6 if _s6_running() else {"linux": _systemd, "darwin": _launchd, "win32": _windows,
                                         "android": _no_supervisor}.get(sys.platform)
    if backend is None:
        raise RuntimeStartError("unsupported_supervisor_platform")
    try:
        return backend(home, deadline)
    except subprocess.TimeoutExpired:
        raise TimeoutError from None
    except (OSError, ValueError) as exc:
        raise RuntimeStartError("service_manager_unavailable") from exc


def start_existing_gateway_service(service: ExistingService, *, deadline: float) -> None:
    """Request start once; only live control readiness can establish success."""
    if service.running:
        return
    if service.backend == "s6":
        from hermes_cli.service_manager import S6ServiceManager
        try:
            S6ServiceManager().start(service.start_argv[0])
        except subprocess.TimeoutExpired:
            raise TimeoutError from None
        except (OSError, RuntimeError, ValueError) as exc:
            raise RuntimeStartError("service_start_failed") from exc
        return
    try:
        result = _run(service.start_argv, deadline, encoding=None)
    except subprocess.TimeoutExpired:
        raise TimeoutError from None
    except OSError as exc:
        raise RuntimeStartError("service_start_failed") from exc
    if result.returncode:
        raise RuntimeStartError("service_start_failed")
