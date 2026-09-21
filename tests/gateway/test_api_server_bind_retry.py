"""A restart must not lose the API server listener to the outgoing instance's socket tail.

Regression for the 2026-09-19 fleet outage: the gateway restart at 12:13:52 tried to bind
127.0.0.1:8644 ONCE, the outgoing instance still held the address, `connect()` returned False with a
non-retryable fatal error, and `platforms.api_server` — the address `bot_peers.yoyodine` points at —
stayed silent, so every lane's peer DM answered connection refused until a manual restart. The port
was free again minutes later: one retry would have restored the channel.

Three behaviours are covered, and they belong together: the bind retries while the port is held
(``_BIND_BACKOFF_SECONDS``, ~30s to outlast a macOS TIME_WAIT tail), a socket tail that nobody
answers on is reclaimable at all, and an exhausted window with NOBODY listening leaves the platform
retryable (``run_startup`` line ~1114 queues any non-fatal failure as ``retrying`` for the reconnect
watcher) — while a live foreign listener still sets the non-retryable fatal error that keeps that
watcher from looping forever (#52132, asserted in test_api_server_bind_guard.py).
"""

import socket
import threading
import time

import pytest

from gateway.config import PlatformConfig
from gateway.platforms import api_server
from gateway.platforms.api_server import APIServerAdapter

pytestmark = pytest.mark.asyncio

_KEY = "sk-test-4f9c2a8e1b7d0365af2c91ee"  # ≥16 chars: a short key is refused before the bind


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_adapter(port: int) -> APIServerAdapter:
    return APIServerAdapter(
        PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": port, "key": _KEY})
    )


def _hold_port(port: int) -> socket.socket:
    """A live listener on 127.0.0.1:port — the outgoing instance still serving during a hand-off."""
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", port))
    holder.listen(5)
    return holder


def _hold_port_deaf(port: int) -> socket.socket:
    """Take 127.0.0.1:port without listening: nothing answers, the kernel still refuses the bind.

    A bare ``bind`` with no ``listen`` is the address-taken-nobody-answering shape, and unlike
    TIME_WAIT it is reproducible on every host instead of only where a strict bind is the default.
    """
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", port))
    return holder


def _assert_time_wait_tail(port: int) -> None:
    """Precondition, so the test fails loudly instead of passing vacuously where it holds no tail.

    Measured on macOS: the strict bind is refused with EADDRINUSE and only the SO_REUSEADDR rebind
    gets the address back, while the tail accepts nothing (asserted in the test body).
    """
    strict = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(OSError):
            strict.bind(("127.0.0.1", port))
    finally:
        strict.close()
    reuse = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    reuse.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        reuse.bind(("127.0.0.1", port))
    finally:
        reuse.close()


def _leave_time_wait_tail(port: int) -> None:
    """Leave 127.0.0.1:port in TIME_WAIT: the server closes the accepted connection first.

    On macOS the adapter binds without SO_REUSEADDR (an exclusive bind, so two listeners cannot split
    traffic), and the tail then refuses the incoming bind for 2*MSL (~30s) even though nobody listens.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    accepted, _ = srv.accept()
    accepted.close()  # active close on the accepted socket -> TIME_WAIT on (127.0.0.1, port)
    client.close()
    srv.close()
    _assert_time_wait_tail(port)


async def test_bind_retries_until_the_outgoing_instance_releases_the_port():
    """The restart recovers by itself: the port frees after the first failed attempt and the retry
    binds it — no process restart, no fatal error, and the listener answers that address."""
    port = _free_port()
    holder = _hold_port(port)
    release = threading.Timer(0.3, holder.close)
    release.start()
    adapter = _make_adapter(port)
    try:
        assert await adapter.connect() is True
        assert adapter.has_fatal_error is False
        assert api_server._has_live_listener("127.0.0.1", port) is True
    finally:
        release.cancel()
        holder.close()
        await adapter.disconnect()


@pytest.mark.macos_only
async def test_time_wait_tail_does_not_block_the_restart():
    """The root cause: macOS keeps SO_REUSEADDR off (an exclusive bind), so the previous instance's
    TIME_WAIT socket refuses the incoming bind although nobody listens. The bind must reclaim it."""
    port = _free_port()
    _leave_time_wait_tail(port)
    adapter = _make_adapter(port)
    try:
        assert await adapter.connect() is True
        assert adapter.has_fatal_error is False
        assert api_server._has_live_listener("127.0.0.1", port) is True
    finally:
        await adapter.disconnect()


@pytest.mark.macos_only
async def test_bind_failure_without_a_listener_leaves_the_platform_retryable(monkeypatch):
    """A bind that fails with nobody listening is TRANSIENT: no fatal error, so run_startup queues the
    platform as ``retrying`` for the reconnect watcher (backoff + NEEDS_ATTENTION) instead of dropping
    it for the life of the gateway — the one-shot log line followed by permanent silence.

    The retry window and the EADDRINUSE are real; only the OS probe is stubbed, because the persistent
    shape it has to rule out is not constructible here. On macOS a TIME_WAIT tail is reclaimed by the
    rebind (test_time_wait_tail_does_not_block_the_restart), and a stale socket that never listened
    silently DROPS the probe's SYN, so the probe times out and conservatively reports a live holder —
    which is what keeps a real conflict non-retryable and the watcher bounded (#52132).
    """
    monkeypatch.setattr(api_server, "_BIND_BACKOFF_SECONDS", (), raising=False)  # window exhausted at once
    monkeypatch.setattr(api_server, "_has_live_listener", lambda host, port: False, raising=False)
    port = _free_port()
    deaf = _hold_port_deaf(port)
    adapter = _make_adapter(port)
    try:
        assert await adapter.connect() is False
        try:
            assert adapter.has_fatal_error is False
            assert adapter.fatal_error_code is None
        finally:
            await adapter.disconnect()
    finally:
        deaf.close()


async def test_shutdown_waits_for_the_address_to_be_released():
    """disconnect() returns only once nothing accepts on our address, so the replacement instance
    that binds the same host:port seconds later cannot race a live socket."""
    port = _free_port()
    holder = _hold_port(port)
    release = threading.Timer(0.3, holder.close)
    release.start()
    adapter = _make_adapter(port)
    started = time.monotonic()
    try:
        await adapter._await_listener_release()
        assert time.monotonic() - started >= 0.3
        assert api_server._has_live_listener("127.0.0.1", port) is False
    finally:
        release.cancel()
        holder.close()
