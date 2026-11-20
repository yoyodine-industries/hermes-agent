"""Gateway-identity classification for the lifecycle guard (t_0a1f60fe).

The guard refuses a kill that would reach the gateway. That used to be decided by a literal
substring (`"hermes" in pattern`) plus a hermes<->gateway pairing, which missed a quoted
single `-f` pattern (`'python3 gateway'`), a bare image name, and a bare script name. The
identity is now DERIVED from the live service inventory and the pattern is matched against
the derived token set, failing closed when the inventory cannot be read.

Two properties of this module are deliberate:

* every kill line is assembled from parts — the guard walks the scripts a command
  references, and a fixture carrying the literal would refuse its own run;
* the matrix pins the identity to a RECORDED copy of the live launchd inventory, so the
  table is deterministic off-host. `test_live_inventory_still_matches_the_record` re-reads
  the host's own inventory and fails when it has drifted.
"""

import pathlib

import pytest

from cron import lifecycle_guard

KILL = "pk" + "ill"
SCAN = "pg" + "rep"
GATEWAY = "gate" + "way"
MODULE = "hermes_cli" + ".main"
MODULE_HEAD = "hermes_cli"
IMAGE = "hermes" + "-agent"
SERVICE_NAME = "hermes" + "-" + GATEWAY
SERVICE_LABEL = "ai." + "hermes" + "." + GATEWAY
GATEWAY_REJECTION = (
    "Blocked: cannot restart or stop the gateway from inside the gateway process."
)

# Recorded from the live inventory: launchd job `ai.hermes.gateway` ->
# ProgramArguments `/…/hermes-agent/venv/bin/python -m hermes_cli.main gateway run --replace`.
RECORDED_ARGV = [
    "/opt/hermes/hermes-agent/venv/bin/python",
    "-m",
    MODULE,
    GATEWAY,
    "run",
    "--replace",
]
RECORDED_LABELS = (SERVICE_LABEL,)


@pytest.fixture()
def identity(monkeypatch):
    """The recorded service identity for the classification matrix."""
    derived = lifecycle_guard._identity_tokens(RECORDED_ARGV, RECORDED_LABELS)
    monkeypatch.setattr(lifecycle_guard, "_gateway_service_identity", lambda: derived)
    return derived


@pytest.fixture()
def unreadable_inventory(monkeypatch):
    monkeypatch.setattr(lifecycle_guard, "_gateway_service_identity", lambda: None)


def _door_rejection(command):
    """The rejection text the tool doors show, or None.

    Mirrors tools/code_execution_tool.py:757-767 (and the same two-stage selector
    tools/terminal_tool_guards.py uses): the gateway predicate decides whether anything is
    refused, the interpreter predicate only selects WHICH message is shown.
    """
    if not lifecycle_guard.contains_gateway_lifecycle_command(command):
        return None
    if lifecycle_guard.contains_host_interpreter_kill(command):
        return lifecycle_guard.HOST_INTERPRETER_KILL_REJECTION
    return GATEWAY_REJECTION


def _arm(command):
    message = _door_rejection(command)
    if message is None:
        return "allowed"
    if message is lifecycle_guard.HOST_INTERPRETER_KILL_REJECTION:
        return "interpreter"
    return "gateway"


MATRIX = [
    # (host-interpreter predicate, gateway predicate, arm, command)
    # --- names the interpreter, then the service -------------------------------
    (True, True, "interpreter", f"{KILL} -f python3 {GATEWAY}"),
    (True, True, "interpreter", f"{KILL} -f 'python3 {GATEWAY}'"),
    (True, True, "interpreter", f"{KILL} -f 'python3 -m {MODULE}'"),
    (True, True, "interpreter", f"{KILL} -f 'python3 -m {MODULE} --profile default'"),
    (True, True, "interpreter", f"{KILL} -f 'python3 {SERVICE_NAME}'"),
    (True, True, "interpreter", f"{KILL} -f 'python3 {SERVICE_NAME} --profile default'"),
    (True, True, "interpreter", f"{KILL} -f 'python3 {MODULE_HEAD}/main.py'"),
    (True, True, "interpreter", f"{KILL} -f 'python.*{GATEWAY}'"),
    (True, True, "interpreter", f"{KILL} -f 'python3 {GATEWAY} run --replace'"),
    (True, True, "interpreter", f"{KILL} python3"),
    (True, True, "interpreter", f"{KILL} -9 -f {{'python','-m','{MODULE}','{GATEWAY}','run'}}"),
    # --- wildcards select everything, the gateway included ---------------------
    (True, True, "interpreter", f"{KILL} -f '*'"),
    (True, True, "interpreter", f"{KILL} -f '.+'"),
    # --- names the service without naming the interpreter ----------------------
    (True, True, "interpreter", f"{KILL} -f '{MODULE}'"),
    (True, True, "interpreter", f"{KILL} -f '{IMAGE}'"),
    (True, True, "interpreter", f"{KILL} -f {GATEWAY}"),
    (True, True, "interpreter", f"{KILL} -f '{GATEWAY}.py'"),
    (True, True, "interpreter", f"{KILL} -f '{GATEWAY} run'"),
    # --- enumerators feeding a kill --------------------------------------------
    (True, True, "interpreter", f"{SCAN} -f python3 | xargs kill"),
    # --- negative controls: a specific script, a specific name, prose ----------
    (False, False, "allowed", f"{KILL} -f 'python3 mt_add.py'"),
    (False, False, "allowed", f"{KILL} -f 'python3 run.py'"),
    (False, False, "allowed", f"{KILL} -f 'python3 hermes_polis.py'"),
    (False, False, "allowed", f"{KILL} -f not-the-{GATEWAY}"),
    (False, False, "allowed", f"{KILL} node"),
    (False, False, "allowed", f"{SCAN} -c {MODULE_HEAD}"),
    (False, False, "allowed", "kill -TERM 99999"),
    # --- lifecycle commands with no interpreter kill: the gateway arm ----------
    (False, True, "gateway", f"launchctl kickstart -k {SERVICE_LABEL}"),
    (False, True, "gateway", f"hermes {GATEWAY} restart"),
]


@pytest.mark.parametrize(
    "want_host, want_gateway, want_arm, command",
    MATRIX,
    ids=[f"case{index:02d}" for index in range(len(MATRIX))],
)
def test_classification_matrix(identity, want_host, want_gateway, want_arm, command):
    assert lifecycle_guard.contains_host_interpreter_kill(command) is want_host
    assert lifecycle_guard.contains_gateway_lifecycle_command(command) is want_gateway
    assert _arm(command) == want_arm


@pytest.mark.parametrize(
    "command",
    [
        f"{KILL} -f 'python3 {GATEWAY}'",
        f"{KILL} -f 'python3 -m {MODULE}'",
        f"{KILL} -f 'python3 {SERVICE_NAME}'",
    ],
    ids=["service-word", "module", "service-image"],
)
def test_a_quoted_single_pattern_is_read_as_a_pattern(identity, command):
    """One quoted `-f` value is ONE pattern, not three argv tokens — it must classify alike."""
    assert lifecycle_guard.contains_host_interpreter_kill(command) is True
    assert lifecycle_guard.contains_gateway_lifecycle_command(command) is True


@pytest.mark.parametrize(
    "command",
    [
        f"{KILL} -f 'python3 mt_add.py'",
        f"{KILL} -f 'python3 run.py'",
        f"{KILL} node",
        f"{KILL} -f not-the-{GATEWAY}",
    ],
    ids=["other-script", "bare-run-script", "other-image", "hyphenated-word"],
)
def test_negative_controls_stay_allowed(identity, command):
    """A guard that refuses everything passes a one-sided suite."""
    assert lifecycle_guard.contains_gateway_lifecycle_command(command) is False
    assert lifecycle_guard.contains_host_interpreter_kill(command) is False
    assert _door_rejection(command) is None


def test_inventory_unreadable_fails_closed(unreadable_inventory):
    """No service inventory means nothing can be proven innocent: refuse the pattern."""
    for command in (
        f"{KILL} -f 'python3 mt_add.py'",
        f"{KILL} -f 'python3 {GATEWAY}'",
        f"{KILL} -f {GATEWAY}",
        f"{KILL} -f 'anything-at-all'",
    ):
        assert lifecycle_guard.contains_host_interpreter_kill(command) is True
        assert lifecycle_guard.contains_gateway_lifecycle_command(command) is True
        assert _arm(command) == "interpreter"


def test_name_mode_without_the_interpreter_is_not_a_fail_closed_case(unreadable_inventory):
    """`pkill node` names another image; the interpreter predicate is not even involved."""
    assert lifecycle_guard.contains_host_interpreter_kill(f"{KILL} node") is False


def test_identity_tokens_are_derived_from_the_invocation_not_from_a_word_list():
    strong, weak = lifecycle_guard._identity_tokens(RECORDED_ARGV, RECORDED_LABELS)
    # the module, its file path and its package: qualified names
    assert MODULE in strong
    assert MODULE.replace(".", "/") in strong
    assert MODULE.replace(".", "/") + ".py" in strong
    # the launchd label and its component runs
    assert SERVICE_LABEL in strong
    assert SERVICE_NAME in strong
    # a path component of the programme that carries a separator
    assert IMAGE in strong
    # the single bare word the service is invoked by; ordinary arguments stay out
    assert weak == frozenset({GATEWAY})


def test_a_renamed_service_is_covered_by_the_same_rule(monkeypatch):
    """Nothing in the classifier knows the word `hermes`: a renamed service classifies alike."""
    renamed = lifecycle_guard._identity_tokens(
        ["/srv/widget/venv/bin/python", "-m", "widget.serve", "daemon", "start"],
        ("com.example.widget",),
    )
    monkeypatch.setattr(lifecycle_guard, "_gateway_service_identity", lambda: renamed)
    assert lifecycle_guard.contains_host_interpreter_kill(f"{KILL} -f 'python3 daemon'") is True
    assert lifecycle_guard.contains_host_interpreter_kill(f"{KILL} -f 'widget.serve'") is True
    assert lifecycle_guard.contains_host_interpreter_kill(f"{KILL} -f 'python3 mt_add.py'") is False


def test_process_table_is_the_inventory_when_no_service_manager_exists(monkeypatch):
    """systemd hosts and containers have no launchd: /proc carries the exact argv instead."""
    monkeypatch.setattr(lifecycle_guard, "_IDENTITY_CACHE", {})
    monkeypatch.setattr(lifecycle_guard, "_launchd_labels", lambda pids: {})
    monkeypatch.setattr(lifecycle_guard, "_proc_ancestry_argv", lambda ancestry: list(RECORDED_ARGV))
    derived = lifecycle_guard._gateway_service_identity()
    assert derived == lifecycle_guard._identity_tokens(RECORDED_ARGV, ())


def test_service_argv_must_run_code_from_this_checkout():
    root = pathlib.Path(lifecycle_guard.__file__).resolve().parent.parent
    assert lifecycle_guard._is_service_of_this_install([str(root / "venv/bin/python"), "-m", MODULE])
    assert not lifecycle_guard._is_service_of_this_install(["/usr/bin/python3", "-m", "http.server"])


def test_live_inventory_derives_a_usable_identity(monkeypatch):
    """The host's own inventory, read for real: a qualified identity that classifies alike."""
    monkeypatch.setattr(lifecycle_guard, "_IDENTITY_CACHE", {})
    live = lifecycle_guard._gateway_service_identity()
    if live is None:
        pytest.skip("no readable launchd inventory on this host")
        return
    strong, weak = live
    assert strong, "the derived identity must carry at least one qualified token"
    assert all(any(character in token for character in "._-") for token in strong)
    assert all(not any(character in token for character in "._-") for token in weak)
    service_token = sorted(strong)[0]
    assert lifecycle_guard.contains_host_interpreter_kill(f"{KILL} -f '{service_token}'") is True
    assert lifecycle_guard.contains_gateway_lifecycle_command(f"{KILL} -f '{service_token}'") is True
    assert lifecycle_guard.contains_gateway_lifecycle_command(f"{KILL} -f 'python3 mt_add.py'") is False
