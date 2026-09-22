"""Regression tests for CLI gateway run exit behavior.

``hermes gateway run`` enters through hermes_cli.gateway, not gateway.run.main().
After graceful teardown it must use the same hard-exit backstop as gateway.run.main()
so Python finalization does not wait on non-daemon worker threads (for example
in-flight cron ThreadPoolExecutor jobs) and delay service-managed restarts.
"""

from __future__ import annotations

import types

import pytest


class _HardExitObserved(BaseException):
    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


def gateway_run_module():
    """The ``gateway.run`` module object, so tests can patch the exit-driver seam the CLI imports."""
    import gateway.run as gateway_run

    return gateway_run


def _prepare(monkeypatch):
    import hermes_cli.gateway as gateway_cli
    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_cli, "_guard_official_docker_root_gateway", lambda: None)
    monkeypatch.setattr(gateway_cli, "_guard_named_profile_under_multiplexer", lambda force=False: None)
    monkeypatch.setattr(gateway_cli, "_guard_supervised_gateway_conflict", lambda force=False: None)
    monkeypatch.setattr(gateway_cli, "_guard_existing_gateway_process_conflict", lambda replace=False: None)
    monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: False)
    monkeypatch.setattr(gateway_cli.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    monkeypatch.setenv("HERMES_GATEWAY_EXIT_DIAG", "0")

    async def _start_gateway(*args, **kwargs):  # pragma: no cover - never awaited by fake run
        return True

    def _hard_exit(code: int) -> None:
        raise _HardExitObserved(code)

    monkeypatch.setattr(gateway_run, "start_gateway", _start_gateway)
    monkeypatch.setattr(gateway_run, "_exit_after_graceful_shutdown", _hard_exit)
    return gateway_cli


def test_run_gateway_hard_exits_after_clean_return(monkeypatch):
    """Also pins the seam: the CLI must drive the gateway through gateway.run's exit driver, NOT
    asyncio.run, whose teardown joins the loop's default executor with no leash (t_58fe16cb)."""
    gateway_cli = _prepare(monkeypatch)

    driven = []

    def _fake_driver(coro):
        coro.close()
        driven.append(True)
        return True

    def _asyncio_run_must_not_be_used(coro):  # pragma: no cover - only on regression
        coro.close()
        raise AssertionError("hermes gateway run must use the wedge-proof exit driver")

    monkeypatch.setattr(gateway_run_module(), "_run_gateway_until_verdict", _fake_driver)
    monkeypatch.setattr(gateway_cli.asyncio, "run", _asyncio_run_must_not_be_used)

    with pytest.raises(_HardExitObserved) as excinfo:
        gateway_cli.run_gateway()

    assert excinfo.value.code == 0
    assert driven == [True]


def test_run_gateway_hard_exits_after_keyboard_interrupt(monkeypatch):
    """KeyboardInterrupt (console Ctrl+C) must also hard-exit, not return.

    A bare ``return`` would let Python finalization join non-daemon worker
    threads, the same wedge this backstop prevents on the other exit paths.
    """
    gateway_cli = _prepare(monkeypatch)

    def _fake_driver(coro):
        coro.close()
        raise KeyboardInterrupt()

    monkeypatch.setattr(gateway_run_module(), "_run_gateway_until_verdict", _fake_driver)

    with pytest.raises(_HardExitObserved) as excinfo:
        gateway_cli.run_gateway()

    assert excinfo.value.code == 0
