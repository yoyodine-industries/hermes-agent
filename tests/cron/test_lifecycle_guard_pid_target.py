"""Branch F: the gateway-lifecycle guard keyed on the TARGET of the act, not its spelling.

The guard's older branches need a ``hermes``/``gateway`` token in the command (A-D) or a killer aimed
at the interpreter IMAGE (E). None of them sees the act the fleet actually reached for: a bare signal
to the pid the gateway is running as — ``kill -USR1 <pid>`` / ``kill -TERM <pid>`` — which terminates
the process hosting the guard and every agent session inside it, and contains no gateway token at all.
Branch F resolves the operand through Hermes' own identity ladder
(``gateway.status.get_running_pid`` / ``hermes_cli.gateway.find_gateway_pids``) and compares.

These tests are hermetic: the resolved identity set is monkeypatched, so no gateway has to be running
and no process is ever signalled — every candidate is handed over as TEXT.
"""

from __future__ import annotations

import gateway.status
import hermes_cli.gateway
import pytest

import cron.lifecycle_guard as lifecycle_guard

# The live gateway on this host, in the shape the resolver returns: (pid, name, cmdline).
HOST_PID = 17285
HOST_NAME = "python3"
HOST_CMDLINE = (
    "/Users/hermes_user/.hermes/tools/python-3.14.12/bin/python3 -I -c import sys, runpy; "
    "sys.path.insert(0, '/Users/hermes_user/.hermes/hermes-agent'); sys.argv = "
    "['/Users/hermes_user/.hermes/hermes-agent/hermes_cli/main.py', 'gateway', 'run', '--replace']; "
    "runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True)"
)
SIBLING_PID = 20301
DECOY_PID = 99967

CARRIER = "python3 /Users/hermes_user/.hermes/yoyoflow/scripts/hermes_update_nodes.py bounce"


@pytest.fixture
def live_gateway(monkeypatch):
    """One live gateway, resolved — the host's own shape."""
    monkeypatch.setattr(
        lifecycle_guard,
        "_live_gateway_identities",
        lambda: ((HOST_PID, HOST_NAME, HOST_CMDLINE),),
    )


@pytest.fixture
def live_gateways(monkeypatch):
    """Two live gateways (this profile's and a sibling's)."""
    monkeypatch.setattr(
        lifecycle_guard,
        "_live_gateway_identities",
        lambda: ((HOST_PID, HOST_NAME, HOST_CMDLINE), (SIBLING_PID, HOST_NAME, HOST_CMDLINE + " 2")),
    )


def flagged(text: str) -> bool:
    return lifecycle_guard.contains_gateway_lifecycle_command(text)


# --- the mechanism: a signal whose resolved target is the live gateway ---------------------


@pytest.mark.parametrize("command", [
    f"kill -USR1 {HOST_PID}",
    f"kill -TERM {HOST_PID}",
    f"kill {HOST_PID}",
    f"kill -SIGTERM {HOST_PID}",
    f"kill -9 {HOST_PID}",
    f"kill -s TERM {HOST_PID}",
    f"kill -s TERM {SIBLING_PID}",
    f"kill --signal TERM {HOST_PID}",
    f"kill --signal=9 {HOST_PID}",
    f"kill -n 9 {HOST_PID}",
    f"sudo kill -TERM {HOST_PID}",
    f"kill -TERM {HOST_PID} && echo sent",
    f"kill -USR1 {DECOY_PID} {HOST_PID}",
])
def test_pid_signal_at_the_live_gateway_is_flagged(command, live_gateways):
    assert flagged(command) is True


def test_signal_is_flagged_through_the_referenced_script_and_sh_c_scan(live_gateway):
    """The choke point is shared, so the walk inherits Branch F like it inherits the other branches."""
    assert lifecycle_guard.contains_gateway_lifecycle_command_or_referenced_script(
        f"sh -c 'kill -TERM {HOST_PID}'"
    ) is True


@pytest.mark.parametrize("command", [
    f"kill -USR1 {DECOY_PID}",          # a live pid, just not a gateway
    f"kill {DECOY_PID}",
    f"kill -TERM $$",                   # the shell's own pid: not an integer literal at all
    f"kill -TERM {HOST_PID}0",          # near-miss neighbour of the gateway pid
    "kill -USR1 1",                     # init
    f"kill -0 {HOST_PID}",              # existence probe: signals nothing
    f"kill -s 0 {HOST_PID}",
    f"kill --signal 0 {HOST_PID}",
    "kill -l",
    "kill -l 9",
    f"grep -n 'kill -USR1 {HOST_PID}' notes.md",   # documentation, not the act
    f"echo kill -TERM {HOST_PID}",
])
def test_signals_that_do_not_target_the_live_gateway_are_not_flagged(command, live_gateways):
    assert flagged(command) is False


def test_no_resolvable_gateway_leaves_the_same_pid_unflagged(monkeypatch):
    """Fail-open: with nothing resolvable the rung has no target, so the spelling proves nothing."""
    monkeypatch.setattr(lifecycle_guard, "_live_gateway_identities", lambda: ())
    assert flagged(f"kill -USR1 {HOST_PID}") is False


def test_resolution_is_what_flags_it_not_the_token(monkeypatch):
    """The same text flips with the resolved set alone — the rung reads the TARGET, not the spelling."""
    monkeypatch.setattr(
        lifecycle_guard,
        "_live_gateway_identities",
        lambda: ((DECOY_PID, HOST_NAME, HOST_CMDLINE),),
    )
    assert flagged(f"kill -TERM {DECOY_PID}") is True
    assert flagged(f"kill -TERM {HOST_PID}") is False


# --- name-based killers and enumerator feeds ------------------------------------------------


@pytest.mark.parametrize("command", [
    "pkill -f hermes_cli",
    "pkill -f hermes_cli/main.py",
    "pkill --full 'hermes_cli.*gateway'",
    "pkill -f 'runpy.run_module'",
    f"pgrep -f hermes_cli && kill -TERM {DECOY_PID}",   # feed + a killer in the same text
])
def test_name_based_targets_resolving_to_the_live_gateway_are_flagged(command, live_gateway):
    assert flagged(command) is True


@pytest.mark.parametrize("command", [
    "pkill -f not-the-gateway",
    "pkill -f 'gateway run'",           # no such substring in the shim's argv: selects nothing
    "pkill -f 'my_hermes_bot'",
    "pkill -x hermes_cli",              # exact-name match against argv[0]
    "killall hermes_cli",
    "pgrep -f hermes_cli",              # an enumeration on its own is not a signal
    "pgrep -c hermes",
    "python3 -c \"import subprocess as sp; sp.run(['pkill', '-f', 'not-the-gateway'])\"",
])
def test_name_based_targets_that_select_nothing_are_not_flagged(command, live_gateway):
    assert flagged(command) is False


def test_invalid_ere_operand_is_not_a_match(live_gateway):
    """An operand that is not a valid ERE selects no process, so Branch F does not claim it: the rung
    returns a verdict instead of raising, which is what keeps a malformed pattern from taking the scan
    down. (``pkill -f 'hermes_cli['`` is the argv-list normalization at work — `[`/`]`/`,` are stripped
    before an operand is read, exactly as the other token passes do, so it is read as `hermes_cli` and
    IS a target.)"""
    assert lifecycle_guard.command_targets_live_gateway("pkill -f my_hermes_bot?*") is False
    assert flagged("pkill -f my_hermes_bot?*") is False
    assert lifecycle_guard.command_targets_live_gateway("pkill -f 'hermes_cli['") is True


# --- the bounce carrier ---------------------------------------------------------------------


@pytest.mark.parametrize("command", [
    CARRIER,
    "/opt/hermes_sandbox/yoyoflow/.venv/bin/python "
    "/Users/hermes_user/.hermes/yoyoflow/scripts/hermes_update_nodes.py bounce",
    "hermes_update_nodes bounce",
    "hermes_update_nodes.py bounce",
])
def test_bounce_carrier_invocations_are_flagged(command, live_gateway):
    assert flagged(command) is True


@pytest.mark.parametrize("command", [
    # A yoyoflow `code:` block, a comment or prose mentions the module without invoking it.
    'import hermes_update_nodes as hun\nbounce = hun.bounce(inputs.get("update"))',
    "hermes_update_nodes.py outcome-finalize 69 completed",
    "hermes_update_nodes --help",
    "cat /Users/hermes_user/.hermes/yoyoflow/scripts/hermes_update_nodes.py",
    "grep -n 'def bounce' hermes_update_nodes.py",
    "# the bounce node calls hun.bounce(...)",
])
def test_bounce_carrier_mentions_are_not_flagged(command, live_gateway):
    """Command-shaped only: the module must be EXECUTED and `bounce` its first argument."""
    assert flagged(command) is False


# --- fail-open and totality -----------------------------------------------------------------


def test_resolver_that_raises_is_not_flagged(monkeypatch):
    def explode():
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(lifecycle_guard, "_live_gateway_identities", explode)
    text = f"kill -USR1 {HOST_PID}"
    assert lifecycle_guard.command_targets_live_gateway(text) is False
    assert flagged(text) is False


def test_rung_that_raises_is_not_flagged_at_the_choke_point(monkeypatch):
    def explode(*_args, **_kwargs):
        raise RuntimeError("rung exploded")

    monkeypatch.setattr(lifecycle_guard, "command_targets_live_gateway", explode)
    assert flagged(f"kill -USR1 {HOST_PID}") is False


def test_resolution_runs_at_most_once_per_text(monkeypatch, live_gateway):
    """The cost contract: a text full of candidates still pays ONE identity resolution."""
    calls: list[int] = []
    monkeypatch.setattr(
        lifecycle_guard,
        "_live_gateway_identities",
        lambda: calls.append(1) or ((HOST_PID, HOST_NAME, HOST_CMDLINE),),
    )
    text = f"kill -TERM {HOST_PID}; kill -USR1 {HOST_PID}; pkill -f not-the-gateway"
    assert flagged(text) is True
    assert len(calls) == 1


def test_text_without_a_candidate_token_never_resolves(monkeypatch):
    """The cheap prefilter is what keeps this rung off the cost path of every other command."""
    def explode():
        raise AssertionError("resolution reached for text with no candidate token")

    monkeypatch.setattr(lifecycle_guard, "_live_gateway_identities", explode)
    for command in ("ls -la /tmp && git status", "hermes gateway status", "cat /etc/hosts"):
        assert flagged(command) is False


# --- the resolver itself --------------------------------------------------------------------


def test_resolver_reads_both_sources_and_drops_unreadable_command_lines(monkeypatch):
    monkeypatch.setattr(gateway.status, "get_running_pid", lambda: HOST_PID)
    monkeypatch.setattr(hermes_cli.gateway, "find_gateway_pids", lambda *a, **k: [HOST_PID, SIBLING_PID])
    monkeypatch.setattr(
        gateway.status,
        "_read_process_cmdline",
        lambda pid: HOST_CMDLINE if pid == HOST_PID else None,
    )
    identities = lifecycle_guard._live_gateway_identities()
    # One entry per pid, de-duplicated across the two sources; the unreadable one is dropped rather
    # than guessed at (fail-open: an unreadable target is not a target).
    assert identities == ((HOST_PID, HOST_NAME, HOST_CMDLINE),)


def test_resolver_survives_both_sources_failing(monkeypatch):
    def explode(*_args, **_kwargs):
        raise RuntimeError("identity ladder down")

    monkeypatch.setattr(gateway.status, "get_running_pid", explode)
    monkeypatch.setattr(hermes_cli.gateway, "find_gateway_pids", explode)
    assert lifecycle_guard._live_gateway_identities() == ()


def test_resolver_ignores_non_pid_values(monkeypatch):
    monkeypatch.setattr(gateway.status, "get_running_pid", lambda: None)
    monkeypatch.setattr(hermes_cli.gateway, "find_gateway_pids", lambda *a, **k: [None, "17285", -1, 0])
    assert lifecycle_guard._live_gateway_identities() == ()
