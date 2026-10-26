"""Workflow departures: production flights that must outlive the process that launched them.

A yoyoflow workflow run launched from a gateway-served turn starts life as a child of the
gateway's process tree. If it stays there, a sanctioned bounce reaps a live production run
mid-flight (t_4b9843a4): the drain census counts *agent turns*, a workflow run is not a
turn, so the drain says "0 in-flight cron job(s)" while a 32-minute production flight dies
under it and its ledger row is left with a bare ``interrupted (SystemExit(143))``.

The contract this module states, and ``gateway.workflow_runs`` / the terminal tool enforce:

* a command that IS a workflow departure is spawned **detached** -- its own session, owned
  by no turn and no shutdown sweep, so a bounce cannot reap it. Only an explicit operator
  stop (``process_manage kill``) reaches a departure;
* ``HERMES_WORKFLOW_DEPARTURE`` in the departure's environment is the durable marker that
  says "production flight", so a sweep or an attribution pass can recognise one without
  guessing from a pid;
* ``HERMES_WORKFLOW_DETACH=0`` is the kill switch: a host (or a fixture) that sets it runs
  departures as ordinary command trees. That arm is not silently "fine" -- the gateway's
  drain then counts the run and stamps its record with the bounce as the actor.

Detection is deliberately narrow: only an *invocation* of a workflow carrier counts (the
engine entry point, a cron shim, a shim script). A grep for a carrier's name is not a
departure.
"""

from __future__ import annotations

import os
import re
from typing import Mapping, Optional

#: Environment marker carried by a detached workflow departure.
ENV_MARKER = "HERMES_WORKFLOW_DEPARTURE"

#: Kill switch: ``0``/``false``/``no``/``off`` runs departures as ordinary commands.
ENV_GATE = "HERMES_WORKFLOW_DETACH"

#: Override the carrier patterns (``|``-separated regexes, or a path to a file with one
#: regex per line). A host that renames its shims re-points this instead of the code.
ENV_PATTERNS = "HERMES_WORKFLOW_DEPARTURE_PATTERNS"

#: The invocations that ARE workflow departures. Everything here launches a production
#: flight (a yoyoflow run); nothing here reads, greps or documents one.
DEFAULT_PATTERNS: tuple[str, ...] = (
    # `python3 -u -m yoyoflow.run ...` / `python -m yoyoflow.run ...`
    r"-m\s+yoyoflow\.run\b",
    # `yoyoflow run ...`
    r"\byoyoflow\s+run\b",
    # `bash /opt/.../cron/sdlc.sh`, `sh cron/deploy-train.sh`
    r"(?:^|[\s;&|(])(?:bash|sh|zsh)\s+(?:-\S+\s+)*(?:\S*/)?cron/[A-Za-z0-9_.-]+\.sh\b",
    # `bash /opt/.../scripts/sdlc-shim.sh`, `bash scripts/sdlc-admit-shim.sh`
    r"(?:^|[\s;&|(])(?:bash|sh|zsh)\s+(?:-\S+\s+)*(?:\S*/)?scripts/[A-Za-z0-9_.-]*shim\.sh\b",
)

_FALSE_VALUES = frozenset({"0", "false", "no", "off", "disabled"})


def detach_enabled() -> bool:
    """Whether workflow departures are detached on this host (default: yes)."""
    raw = os.environ.get(ENV_GATE, "")
    return str(raw).strip().lower() not in _FALSE_VALUES


def departure_patterns() -> tuple[str, ...]:
    """The carrier-invocation patterns in force (``ENV_PATTERNS`` override or defaults)."""
    raw = (os.environ.get(ENV_PATTERNS) or "").strip()
    if not raw:
        return DEFAULT_PATTERNS
    if os.path.isfile(raw):
        try:
            with open(raw, encoding="utf-8") as fh:
                lines = [ln.strip() for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]
        except OSError:
            return DEFAULT_PATTERNS
        return tuple(lines) or DEFAULT_PATTERNS
    return tuple(p.strip() for p in raw.split("|") if p.strip()) or DEFAULT_PATTERNS


def is_departure_command(command: object) -> bool:
    """True when *command* INVOKES a workflow carrier (the only thing that detaches).

    Read-only inspection of a carrier (``grep ... yoyoflow.run``, ``cat cron/sdlc.sh``) is
    not a departure: the patterns anchor on the launcher form, not on the name.
    """
    if not isinstance(command, str) or not command.strip():
        return False
    for pattern in departure_patterns():
        try:
            if re.search(pattern, command):
                return True
        except re.error:
            continue
    return False


def marker_env(env: Optional[Mapping[str, str]] = None) -> dict:
    """Copy of *env* (default ``os.environ``) carrying the departure marker."""
    marked = dict(os.environ if env is None else env)
    marked[ENV_MARKER] = "1"
    return marked


def is_departure_env(env: Optional[Mapping[str, str]]) -> bool:
    """True when *env* is a departure's environment (marker present and truthy)."""
    if not env:
        return False
    return str(env.get(ENV_MARKER, "")).strip().lower() not in ("", "0", "false", "no", "off")


def pid_is_departure(pid: int) -> bool:
    """True when the live process *pid* carries the departure marker (best effort).

    Used by kill sweeps that only have a pid -- an environment teardown that walks a
    descendant tree must not reap a production flight that merely shares an ancestor.
    """
    if not pid or pid <= 0:
        return False
    try:
        import psutil  # local dependency; imported lazily so the module costs nothing
    except Exception:
        return False
    try:
        return is_departure_env(psutil.Process(pid).environ())
    except Exception:
        return False
