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

#: A heredoc introducer: ``<<EOF``, ``<<-EOF``, ``<<'YAML'``, ``<< "MARKER"``. Not ``<<<``
#: (a herestring carries its data on the command line) and not inside quotes (the shell only
#: opens a heredoc for an unquoted ``<<``).
_HEREDOC_RE = re.compile(r"<<-?[ \t]*([\"']?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _heredoc_delimiters(line: str) -> list[str]:
    """The heredoc delimiters *line* opens, skipping any ``<<`` that sits inside quotes."""
    delimiters: list[str] = []
    quote = ""
    index = 0
    while index < len(line):
        char = line[index]
        if quote:
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in "\"'":
            quote = char
            index += 1
            continue
        if line.startswith("<<", index) and not line.startswith("<<<", index):
            match = _HEREDOC_RE.match(line, index)
            if match:
                delimiters.append(match.group(2))
                index = match.end()
                continue
        index += 1
    return delimiters


def _without_heredoc_bodies(command: str) -> str:
    """*command* with every heredoc BODY dropped, leaving the lines the shell executes.

    A heredoc body is stdin DATA, not a command line: ``cat > notes.md <<'EOF'`` whose text
    quotes ``python -m yoyoflow.run`` launches nothing, and detaching it would take the
    caller's own file write away from it (t_4b9843a4 review). Lines carrying an introducer are
    kept -- a launcher on that line is real -- and only the body up to the delimiter goes. An
    unterminated heredoc swallows the rest, exactly as the shell would.
    """
    lines = command.splitlines()
    kept: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        index += 1
        for delimiter in _heredoc_delimiters(line):
            index = _past_delimiter(lines, index, delimiter)
    return "\n".join(kept)


def _past_delimiter(lines: list[str], index: int, delimiter: str) -> int:
    """Index just past the line closing *delimiter*'s heredoc, or EOF."""
    while index < len(lines):
        stripped = lines[index].strip()
        index += 1
        if stripped == delimiter:
            break
    return index


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
            with open(raw, encoding="utf-8-sig") as fh:
                lines = [ln.strip() for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]
        except OSError:
            return DEFAULT_PATTERNS
        return tuple(lines) or DEFAULT_PATTERNS
    return tuple(p.strip() for p in raw.split("|") if p.strip()) or DEFAULT_PATTERNS


def is_departure_command(command: object) -> bool:
    """True when *command* INVOKES a workflow carrier (the only thing that detaches).

    Read-only inspection of a carrier (``grep ... yoyoflow.run``, ``cat cron/sdlc.sh``) is
    not a departure: the patterns anchor on the launcher form, not on the name. Heredoc
    BODIES are data and never matched (see :func:`_without_heredoc_bodies`), so writing a
    document that quotes a carrier invocation does not detach the write.
    """
    if not isinstance(command, str) or not command.strip():
        return False
    executable = _without_heredoc_bodies(command)
    for pattern in departure_patterns():
        try:
            if re.search(pattern, executable):
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
    except ImportError:
        return False
    try:
        return is_departure_env(psutil.Process(pid).environ())
    except psutil.Error:
        # Gone, a zombie, or an environ we may not read (another user's process): no evidence of
        # the marker, which is exactly what "not a departure" means here.
        return False
