"""The duplicate-start guard must see a live gateway that already released its PID file.

A manual stop+start (the fallback after a service stop reported nothing to stop) lands in the
drain window: the PID file is gone and the runtime lock is free, so the new process opened sockets
beside the still-serving gateway — two gateways, one of them unsupervised. The process scan closes
that window. ``--replace`` keeps its authority: replacing the incumbent is its whole job.
"""

import gateway.run as gateway_run
from gateway.status import get_running_pid, release_gateway_runtime_lock, remove_pid_file


def _release(monkeypatch) -> None:
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)


def test_refuses_when_a_live_gateway_process_exists_without_a_pid_file(monkeypatch):
    _release(monkeypatch)
    monkeypatch.setattr(gateway_run, "_start_gateway_other_live_gateway_pids", lambda: [4242])

    assert gateway_run._start_gateway_claim_pid_file() is False
    assert get_running_pid() is None  # nothing was claimed on the way out


def test_replace_ignores_a_live_gateway_process(monkeypatch):
    _release(monkeypatch)
    monkeypatch.setattr(gateway_run, "_start_gateway_other_live_gateway_pids", lambda: [4242])

    try:
        assert gateway_run._start_gateway_claim_pid_file(replace=True) is True
    finally:
        remove_pid_file()
        release_gateway_runtime_lock()


def test_claims_the_pid_file_when_no_other_gateway_process_is_alive(monkeypatch):
    _release(monkeypatch)
    monkeypatch.setattr(gateway_run, "_start_gateway_other_live_gateway_pids", lambda: [])

    try:
        assert gateway_run._start_gateway_claim_pid_file() is True
    finally:
        remove_pid_file()
        release_gateway_runtime_lock()
