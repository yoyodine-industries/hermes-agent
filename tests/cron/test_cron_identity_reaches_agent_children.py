"""The occurrence identity reaches the children of an AGENT fire, not only a script's.

``cron.execution_identity`` documents the contract as "the env a fire's child processes
receive", and two paths implement it: a ``no_agent`` script (``cron/scheduler_script.py``
overlays the helper on its own subprocess env) and a restart-safe external worker
(``cron/scheduler.py`` overlays it on ``worker_env``). The third path - the ordinary
in-process AGENT fire, whose tools spawn children (``terminal``, ``execute_code``, hooks,
git) from the scheduler's own environ - never exported it, so inside a scheduled agent run
``env | grep '^HERMES_CRON_'`` showed the five legacy names and none of the four identity
names (measured live 2026-10-28, card t_dd37b553).

The agent cannot re-derive the identity for its children: the ledger row records the
SCHEDULER's pid, so a child has no way back to its own row. The fire therefore has to carry
it, and the thread-safe carrier already exists - the ContextVar bound on the
``claimed`` -> ``running`` win (``enter_cron_execution``). Reading it where the child env is
built stays correct for concurrent fires on a multi-profile host, which a process-global
``os.environ`` write would not: two jobs firing on different threads would overwrite each
other's identity for each other's children.

Every spawn surface funnels through ``tools.environments.local._finalize_child_env`` (the
terminal backend's ``_make_run_env`` as well as ``build_subprocess_env`` /
``_sanitize_subprocess_env`` / ``hermes_subprocess_env``), beside the two bridges already
there - the gateway session context and the delegated-child scrub. That is where this test
looks, because that is the only place where a fix is complete for every surface at once.
"""

from __future__ import annotations

import pytest

_FOUR = (
    "HERMES_CRON_JOB_ID",
    "HERMES_CRON_EXECUTION_ID",
    "HERMES_CRON_SCHEDULED_INSTANT",
    "HERMES_CRON_SOURCE",
)


@pytest.fixture(autouse=True)
def _no_bin_dir_probe(monkeypatch):
    """Pin PATH's hermes-install probe: this module asserts identity, not installer layout.

    ``_resolve_hermes_bin_dir`` probes ``shutil.which`` and ``os.path.isdir``, which the
    suite's real-home I/O guard refuses when the checkout under test is not the installed
    tree. Nothing here depends on that answer, so resolve it to "absent" for the module.
    """
    from tools.environments import local

    monkeypatch.setattr(local, "_HERMES_BIN_DIR", None)


@pytest.fixture
def fire():
    """Bind a real cron fire in this context, the way the scheduler binds one."""
    from cron.execution_identity import enter_cron_execution, exit_cron_execution

    record = {
        "source": "builtin",
        "scheduled_instant": "2026-10-28T14:40:00-04:00",
        "started_at": "2026-10-28T14:40:01-04:00",
    }
    token = enter_cron_execution({"id": "occ-identity", "name": "occ identity"}, "exec-abc123", record)
    try:
        yield {"execution_id": "exec-abc123", "record": record}
    finally:
        exit_cron_execution(token)


def test_terminal_child_env_carries_the_fire_identity(monkeypatch, fire):
    """The terminal backend's child env (what the live probe ran inside) must carry all four."""
    from tools.environments.local import _make_run_env

    for name in _FOUR:  # a stale inherited value must not be what makes this pass
        monkeypatch.delenv(name, raising=False)

    env = _make_run_env({})

    assert env["HERMES_CRON_JOB_ID"] == "occ-identity"
    assert env["HERMES_CRON_EXECUTION_ID"] == "exec-abc123"
    assert env["HERMES_CRON_SOURCE"] == "builtin"
    # The occurrence the scheduler consumed, verbatim - never the wall clock at spawn time.
    assert env["HERMES_CRON_SCHEDULED_INSTANT"] == "2026-10-28T14:40:00-04:00"


def test_every_spawn_surface_carries_it(monkeypatch, fire):
    """``build_subprocess_env`` and ``hermes_subprocess_env`` are the other two factories."""
    from tools.environments.local import build_subprocess_env, hermes_subprocess_env

    for name in _FOUR:
        monkeypatch.delenv(name, raising=False)

    for env in (
        build_subprocess_env(),                       # scrubbed (terminal/execute_code policy)
        build_subprocess_env(scrub_secrets=False),     # git credential flows
        hermes_subprocess_env(),                       # non-terminal children
    ):
        assert env["HERMES_CRON_EXECUTION_ID"] == "exec-abc123"
        assert env["HERMES_CRON_JOB_ID"] == "occ-identity"


def test_no_fire_leaves_the_child_env_alone(monkeypatch):
    """Outside a fire the builder must not invent an identity, nor blank an inherited one."""
    from tools.environments.local import _make_run_env

    for name in _FOUR:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_CRON_JOB_ID", "outer-process-value")

    assert _make_run_env({}).get("HERMES_CRON_JOB_ID") == "outer-process-value"


def test_off_schedule_fire_omits_the_occurrence(monkeypatch):
    """A manual fire has no occurrence: the key is ABSENT, never present-and-empty."""
    from cron.execution_identity import enter_cron_execution, exit_cron_execution
    from tools.environments.local import _make_run_env

    for name in _FOUR:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_CRON_SCHEDULED_INSTANT", "2026-10-28T13:00:00-04:00")

    token = enter_cron_execution(
        {"id": "occ-identity"}, "exec-manual",
        {"source": "direct", "scheduled_instant": None, "started_at": None})
    try:
        env = _make_run_env({})
    finally:
        exit_cron_execution(token)

    assert "HERMES_CRON_SCHEDULED_INSTANT" not in env
    assert env["HERMES_CRON_SOURCE"] == "direct"
    assert env["HERMES_CRON_EXECUTION_ID"] == "exec-manual"


def test_delegated_child_is_not_the_occurrence(monkeypatch, fire):
    """A subagent is not the fire: the identity belongs to the worker that owns the occurrence."""
    from agent.delegation_context import delegated_child_context
    from tools.environments.local import _make_run_env

    for name in _FOUR:
        monkeypatch.delenv(name, raising=False)

    with delegated_child_context("session-under-test"):
        assert "HERMES_CRON_EXECUTION_ID" not in _make_run_env({})
