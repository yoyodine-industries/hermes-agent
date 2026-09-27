"""Tests for the strict gateway command-line matcher.

Regression guard for the Windows ``hermes gateway restart`` silent-outage bug:
the previous loose substring match (``"... gateway" in cmdline``) false-matched
``gateway status``/``dashboard`` siblings and unrelated processes such as
``python -m tui_gateway``, which let ``restart()`` race a still-draining old
process and ``status``/``start`` report false positives.
"""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

import pytest

from gateway.status import (
    gateway_spawn_intent_subcommand as spawn_intent,
    inline_bootstrap_argv,
    looks_like_gateway_command_line as matches,
    looks_like_gateway_runtime_command_line as matches_runtime,
)
from hermes_cli import _launchers, venv_sync
from hermes_cli.update_cmd_windows import _hermes_holder_subcommand


ACCEPT = [
    "pythonw.exe -m hermes_cli.main gateway run",
    r"C:\Users\me\hermes\venv\Scripts\pythonw.exe -m hermes_cli.main gateway run",
    "python -m hermes_cli.main --profile work gateway run",
    "python -m hermes_cli.main gateway run --replace",
    "python -m hermes_cli/main.py gateway run",
    "python gateway/run.py",
    "hermes-gateway.exe",
    "hermes gateway",          # bare `hermes gateway` defaults to run
    "hermes gateway run",
    # profile selector AFTER the `gateway` token (argv is profile-position
    # agnostic — _apply_profile_override strips --profile/-p anywhere)
    "hermes gateway --profile work run",
    "python -m hermes_cli.main gateway -p work run",
    "hermes gateway --profile=work run",
    # a profile literally NAMED "gateway"
    "hermes -p gateway gateway run",
    "python -m hermes_cli.main --profile gateway gateway run",
    # quoted Windows paths with spaces (shlex-aware tokenization)
    r'"C:\Program Files\Hermes\hermes-gateway.exe"',
    r'"C:\Program Files\Hermes\gateway\run.py" run',
    r'"C:\Program Files\Py\pythonw.exe" -m hermes_cli.main gateway run',
]

REJECT = [
    "python -m tui_gateway",                              # unrelated module
    "python -m hermes_cli.main gateway status",           # other subcommand
    "python -m hermes_cli.main gateway restart",
    "python -m hermes_cli.main gateway stop",
    "python -m hermes_cli.main --profile x dashboard",    # non-gateway subcommand
    "some random python -m mygateway thing",
    "",
    None,
]


@pytest.mark.parametrize("cmd", ACCEPT)
def test_accepts_real_gateway_run(cmd):
    assert matches(cmd) is True


@pytest.mark.parametrize("cmd", REJECT)
def test_rejects_non_gateway_run(cmd):
    assert matches(cmd) is False


# ``python -c <src> <old_pid> <gateway argv…>`` — the detached restart watcher
# (hermes_cli.gateway._spawn_gateway_restart_watcher). Its trailing argv is the command it will
# spawn LATER, so reading identity off it made the updater's post-relaunch liveness poll vouch for
# the watcher instead of a gateway (#107002).
INLINE_SOURCE_REJECT = [
    'python -c "import time; time.sleep(1)" 14980 python -m hermes_cli.main gateway run',
    r'"C:\Users\me\hermes\venv\Scripts\python.exe" -c "import os" 14980 '
    r'"C:\Users\me\hermes\venv\Scripts\python.exe" -m hermes_cli.main gateway run',
    'python -u -c "import os" 14980 python -m hermes_cli.main --profile work gateway run',
    'python -uc "import os" 14980 hermes gateway run',
    # Options that take a SEPARATE operand must not end the option walk before ``-c`` (the operand
    # is not the start of the program's own argv). The repo itself spawns ``-I -S -B -X utf8 …``
    # (hermes_cli/_old_updater.py, _update_takeover.py), so this shape is not hypothetical.
    'python -X utf8 -c "import os" 14980 python -m hermes_cli.main gateway run',
    'python -W ignore -c "import os" 14980 python -m hermes_cli.main gateway run',
    'python --check-hash-based-pycs always -c "import os" 14980 hermes gateway run',
    'python -I -S -B -X utf8 -c "import os" 14980 python -m hermes_cli.main gateway run',
    # ``-q`` (quiet) takes NO operand, unlike ``-Q``; a case-folded walk would skip past the ``-c``.
    'python -q -c "import os" 14980 python -m hermes_cli.main gateway run',
]


# Real gateways whose interpreter carries operand-taking options must STILL be recognised — the
# value-aware walk must not over-reject. Mirror image of INLINE_SOURCE_REJECT.
INTERPRETER_OPTION_ACCEPT = [
    "python -X utf8 -m hermes_cli.main gateway run",
    "python -W ignore -m hermes_cli.main gateway run",
    "python -q -m hermes_cli.main gateway run",
    "python -I -S -B -X utf8 -m hermes_cli.main gateway run",
    "python --check-hash-based-pycs always -m hermes_cli.main gateway run",
]


@pytest.mark.parametrize("cmd", INTERPRETER_OPTION_ACCEPT)
def test_accepts_gateway_behind_operand_taking_interpreter_options(cmd):
    assert matches(cmd) is True


# The repo's own non-gateway ``-X utf8`` spawn shapes must stay unmatched.
@pytest.mark.parametrize(
    "cmd",
    [
        "python -I -S -B -X utf8 /tmp/update_takeover.py",
        "python -X utf8 -E script.py",
    ],
)
def test_operand_taking_options_do_not_manufacture_a_gateway(cmd):
    assert matches(cmd) is False


@pytest.mark.parametrize("cmd", INLINE_SOURCE_REJECT)
def test_rejects_interpreter_running_inline_source(cmd):
    assert matches(cmd) is False
    assert matches_runtime(cmd) is False


# Spawn INTENT is the mirror image of process identity: the same wrapper that must not be read as a
# live gateway MUST still be recognised as "launching this eventually produces a gateway runtime".
# tests/_fixtures/live_system_guard.py relies on it — without this, the autouse guard stopped
# blocking the detached restart watcher and real gateways leaked out of the test run.
@pytest.mark.parametrize("cmd", INLINE_SOURCE_REJECT)
def test_spawn_intent_sees_through_the_inline_source_wrapper(cmd):
    assert spawn_intent(cmd) == "run"


@pytest.mark.parametrize("cmd", ACCEPT)
def test_spawn_intent_matches_plain_gateway_run(cmd):
    assert spawn_intent(cmd) == "run"


@pytest.mark.parametrize("cmd", REJECT)
def test_spawn_intent_rejects_non_gateway_commands(cmd):
    assert spawn_intent(cmd) != "run"


def test_spawn_intent_keeps_read_only_subcommands_spawnable():
    """The guard only blocks run/start/restart; a ``-c``-wrapped ``gateway status`` must stay
    launchable (tests/test_live_system_guard_self_test.py asserts it passes through)."""
    cmd = 'python -c "import sys; print(sys.argv[1:])" -m hermes_cli.main gateway status'
    assert spawn_intent(cmd) == "status"


def test_spawn_intent_ignores_inline_source_without_a_gateway_argv():
    assert spawn_intent('python -c "import time; time.sleep(1)" 14980') is None


# Atomic Hermes' bundled desktop runner (regression for #22418): it shares
# HERMES_HOME with the CLI and must be recognised as a gateway so
# ``gateway run --replace`` enters the replace/lock-handoff path instead of
# colliding with the desktop runner's still-held scoped locks.
ATOMIC_DESKTOP = (
    "/Applications/Atomic Hermes.app/Contents/Resources/python-server/python "
    "/Applications/Atomic Hermes.app/Contents/Resources/python-server/desktop-gateway.py"
)


def test_accepts_atomic_desktop_gateway():
    assert matches(ATOMIC_DESKTOP) is True
    assert matches_runtime(ATOMIC_DESKTOP) is True


# ---------------------------------------------------------------------------
# Hermes' OWN inline bootstraps are the exception to #107002 (#124318)
# ---------------------------------------------------------------------------
# The store launcher (``_launchers.runtime_command``, also the Windows updater's relaunch), the
# published launcher script (POSIX shell launcher and the Windows ``.cmd`` base64 wrapper) and the
# ``venv_sync`` re-entry all run ``python -I -c <source> …`` with the entry point IN that process,
# so their argv IS the process's identity. Readers hand the matcher different strings: ``/proc``,
# psutil and ``ps`` space-join argv (the source splits across tokens), Windows CIM reports the
# CreateProcess line (``list2cmdline``).

ROOT = Path("/opt/Hermes Agent/hermes-agent")
PY = "/opt/venv/bin/python3"
_LAUNCHER_SCRIPT = _launchers._launcher_script("hermes", ROOT, None)
_JOINS = {"space-joined": " ".join, "windows": subprocess.list2cmdline}


def _forms(argv: list[str]) -> dict[str, list[str]]:
    return {
        "store-launcher": _launchers.runtime_command(ROOT, argv, python=Path(PY)),
        "launcher-script": [PY, "-I", "-c", _LAUNCHER_SCRIPT, *argv],
        "cmd-launcher": [
            PY,
            "-I",
            "-c",
            f"import base64; exec(base64.b64decode('{base64.b64encode(_LAUNCHER_SCRIPT.encode()).decode()}'))",
            *argv,
        ],
        "venv-reentry": venv_sync.relaunch_command(
            Path(PY),
            ROOT,
            [str(ROOT / "hermes_cli" / "main.py"), *argv],
            ["/old/python", "-m", "hermes_cli.main", *argv],
            "hermes_cli.main",
        ),
    }


@pytest.mark.parametrize("join", _JOINS)
@pytest.mark.parametrize("form", _forms([]))
def test_accepts_gateway_behind_hermes_own_inline_bootstrap(form: str, join: str) -> None:
    cmd = _JOINS[join]([str(t) for t in _forms(["gateway", "run", "--replace"])[form]])
    assert matches(cmd) is True
    assert matches_runtime(cmd) is True
    assert _hermes_holder_subcommand(cmd) == "gateway"


# The shape the LaunchDaemon gateway actually runs today (macOS, measured on a live pid): the
# interpreter with ``-I``, ``-c`` and venv_sync's re-entry source carrying the argv INSIDE it —
# there is no trailing argv to read.
LIVE_SHIM: str = " ".join([
    PY,
    "-I",
    "-c",
    "import sys, runpy; sys.path.insert(0, '/opt/Hermes Agent/hermes-agent'); "
    "sys.argv = ['/opt/Hermes Agent/hermes-agent/hermes_cli/main.py', 'gateway', 'run', '--replace']; "
    "runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True)",
])


def test_accepts_the_live_venv_sync_shim_cmdline() -> None:
    assert matches(LIVE_SHIM) is True
    assert matches_runtime(LIVE_SHIM) is True
    assert _hermes_holder_subcommand(LIVE_SHIM) == "gateway"
    assert inline_bootstrap_argv(LIVE_SHIM.split()) == [
        PY, "-m", "hermes_cli.main", "gateway", "run", "--replace",
    ]


INLINE_BOOTSTRAP_NEGATIVE = [
    # The detached restart watcher CARRIES a gateway command it spawns LATER (#107002): identity
    # must not be read off data. Same for a program whose source merely mentions a gateway.
    ('python -c "import os, sys, time; pid = int(sys.argv[1])" 40688 python -m hermes_cli.main gateway run', None),
    ("python -c \"print('hermes gateway run')\"", None),
    # Prose, not a command line.
    ('git commit -m "hermes gateway restart"', None),
    # A real (non-run) gateway invocation is a holder, never a gateway RUNTIME.
    ("python -m hermes_cli.main gateway status", "gateway"),
]


@pytest.mark.parametrize("cmd, holder", INLINE_BOOTSTRAP_NEGATIVE)
def test_inline_bootstrap_recognition_does_not_widen(cmd: str, holder) -> None:
    assert matches(cmd) is False
    assert matches_runtime(cmd) is False
    assert _hermes_holder_subcommand(cmd) == holder


@pytest.mark.parametrize("join", _JOINS)
def test_inline_bootstrap_argv_is_identity_only_for_the_process_running_it(join: str) -> None:
    store = [str(t) for t in _launchers.runtime_command(ROOT, ["gateway", "run"], python=Path(PY))]
    chat = _JOINS[join]([str(t) for t in _launchers.runtime_command(ROOT, ["chat"], python=Path(PY))])
    watcher = _JOINS[join]([PY, "-c", "import os, sys, time\npid = int(sys.argv[1])\n", "1234", *store])
    assert matches(chat) is False and _hermes_holder_subcommand(chat) == "chat"
    assert matches(watcher) is False and _hermes_holder_subcommand(watcher) is None


