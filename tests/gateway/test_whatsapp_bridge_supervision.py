"""An ADOPTED WhatsApp bridge (``_bridge_process is None``) that dies must be respawned, not retried forever.

The adapter adopts a bridge it did not spawn so a gateway restart does not drop the WhatsApp session.
That leaves no child to poll, so ``_check_managed_bridge_exit()`` can never see the loss: the poll loop
used to retry a dead endpoint forever while runtime status kept publishing ``connected``. These tests
drive the recovery against a REAL bridge process over REAL HTTP and kill it mid-flight.

The bridge stand-in speaks the adapter's own surface (/health with its own script hash, /messages,
/read). The adapter spawns it through ``find_node_executable``, which the tests point at this
interpreter — the spawn / wait / attach path under test is the production one, unchanged.
"""

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform

_STUB_BRIDGE = '''\
import hashlib, json, os, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[sys.argv.index("--port") + 1])
SCRIPT = os.path.abspath(sys.argv[0])


class Handler(BaseHTTPRequestHandler):
    def _send(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            with open(SCRIPT, "rb") as fh:
                self._send({"status": "connected", "scriptHash": hashlib.sha256(fh.read()).hexdigest()[:16],
                            "sendReadReceipts": True})
        elif self.path.startswith("/messages"):
            self._send([])
        else:
            self._send({}, status=404)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._send({"messageId": "stub"})

    def log_message(self, *args):
        pass


class Server(ThreadingHTTPServer):
    allow_reuse_address = True


for _attempt in range(20):  # the respawn rebinds this port right after the old listener died
    try:
        Server(("127.0.0.1", PORT), Handler).serve_forever()
        break
    except OSError:
        time.sleep(0.25)
'''


def _make_adapter(*, bridge_script: Path, session_path: Path, port: int):
    """Create a WhatsAppAdapter with test attributes (bypass __init__), as the sibling bridge tests do."""
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter

    adapter = WhatsAppAdapter.__new__(WhatsAppAdapter)
    adapter.platform = Platform.WHATSAPP
    adapter.config = MagicMock()
    adapter._bridge_port = port
    adapter._bridge_script = str(bridge_script)
    adapter._session_path = session_path
    adapter._bridge_log_fh = None
    adapter._bridge_log = None
    adapter._bridge_process = None
    adapter._reply_prefix = None
    adapter._send_read_receipts = True
    adapter._dm_policy = "pairing"
    adapter._allow_from = set()
    adapter._running = True
    adapter._message_handler = None
    adapter._fatal_error_code = None
    adapter._fatal_error_message = None
    adapter._fatal_error_retryable = True
    adapter._fatal_error_handler = None
    adapter._active_sessions = {}
    adapter._pending_messages = {}
    adapter._background_tasks = set()
    adapter._auto_tts_disabled_chats = set()
    adapter._message_queue = asyncio.Queue()
    adapter._http_session = None
    adapter._poll_task = None
    adapter._shutting_down = False
    adapter._bridge_respawn_attempts = 0
    return adapter


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_stub(stub: Path, port: int, log_path: Path) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, str(stub), "--port", str(port)],
                            stdout=open(log_path, "ab"), stderr=subprocess.STDOUT)


def _health(port: int) -> dict:
    """``/health`` JSON of the bridge on *port*; ``{}`` while nothing answers."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as resp:
            return json.loads(resp.read())
    except Exception:
        return {}


def _wait_for_stub(port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _health(port):
            return
        time.sleep(0.1)
    raise AssertionError(f"stub bridge never answered /health on port {port}")


async def _wait_until(predicate, timeout: float = 25.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.1)
    return False


def _platform_state(status_path: Path) -> str:
    """Platform state the adapter last published to the runtime status file (``""`` while unreadable)."""
    try:
        return json.loads(status_path.read_text(encoding="utf-8"))["platforms"]["whatsapp"]["state"]
    except Exception:
        return ""


async def _sample_platform_states(states: list, status_path: Path) -> None:
    """Record every distinct platform state the adapter publishes while recovery runs."""
    while True:
        state = _platform_state(status_path)
        if state and (not states or states[-1] != state):
            states.append(state)
        await asyncio.sleep(0.02)


async def _adopt_stub(tmp_path: Path, monkeypatch) -> tuple:
    """Real stub process + adapter that ADOPTS it (``_bridge_process is None``); returns (adapter, proc)."""
    import plugins.platforms.whatsapp.adapter as adapter_mod

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(adapter_mod, "find_node_executable", lambda name="node": sys.executable)
    session_path = tmp_path / "session"
    session_path.mkdir(parents=True, exist_ok=True)
    stub = tmp_path / "bridge.js"
    stub.write_text(_STUB_BRIDGE, encoding="utf-8")
    port = _free_port()
    proc = _start_stub(stub, port, tmp_path / "stub.log")
    _wait_for_stub(port)
    from plugins.platforms.whatsapp.adapter import _write_bridge_pidfile

    _write_bridge_pidfile(session_path, proc.pid)  # the orphan's own pidfile: what a prior gateway left behind
    adapter = _make_adapter(bridge_script=stub, session_path=session_path, port=port)
    assert await adapter._reuse_running_bridge(stub) is True, "adoption precondition: a matching bridge is running"
    assert adapter._bridge_process is None, "adopted bridges must not be recorded as a managed child"
    return adapter, proc


async def _teardown(adapter, *procs) -> None:
    for proc in procs:
        if proc is not None and proc.poll() is None:
            proc.kill()
    child = getattr(adapter, "_bridge_process", None)
    if child is not None and child.poll() is None:
        child.kill()
    if adapter._poll_task is not None:
        adapter._poll_task.cancel()
    if adapter._http_session is not None and not adapter._http_session.closed:
        await adapter._http_session.close()


@pytest.mark.asyncio
async def test_killed_adopted_bridge_is_respawned_and_state_corrected(tmp_path, monkeypatch):
    """Kill the adopted bridge: within one health cycle the platform serves again AND said so in between."""
    adapter, first = await _adopt_stub(tmp_path, monkeypatch)
    second = None
    try:
        assert _health(adapter._bridge_port)["status"] == "connected"
        status_path = tmp_path / "home" / "gateway_state.json"
        states: list = []
        sampler = asyncio.create_task(_sample_platform_states(states, status_path))

        os.kill(first.pid, signal.SIGKILL)  # the orphan dies under us; nobody supervises it
        first.wait(timeout=5)
        # Wait for the whole episode: loss published, bridge respawned, recovery published.
        respawned = await _wait_until(
            lambda: adapter._bridge_process is not None
            and bool(_health(adapter._bridge_port))
            and "retrying" in states
            and _platform_state(status_path) == "connected")
        sampler.cancel()

        assert respawned, f"a killed adopted bridge was never respawned (states={states})"
        second = adapter._bridge_process
        assert second.pid != first.pid and second.poll() is None, "the respawned bridge must be a live new child"
        assert _health(adapter._bridge_port)["status"] == "connected"
        assert adapter._http_session is not None and not adapter._http_session.closed, "polling must resume"
        # Nobody may read ``connected`` while the bridge is gone (retrying is not a healthy platform state).
        assert "retrying" in states, f"loss was never published: {states}"
        assert states[-1] == "connected", f"recovery was never published: {states}"
    finally:
        await _teardown(adapter, first, second)


@pytest.mark.asyncio
async def test_transient_poll_error_does_not_churn_a_serving_bridge(tmp_path, monkeypatch):
    """A poll error while ``/health`` still answers must leave the adopted bridge alone."""
    adapter, first = await _adopt_stub(tmp_path, monkeypatch)
    try:
        assert await adapter._recover_adopted_bridge(RuntimeError("read timeout")) is False
        assert adapter._bridge_process is None, "a serving bridge must not be replaced"
        assert first.poll() is None and _health(adapter._bridge_port)
        state = json.loads((tmp_path / "home" / "gateway_state.json").read_text(encoding="utf-8"))
        assert state["platforms"]["whatsapp"]["state"] == "connected"
    finally:
        await _teardown(adapter, first)


@pytest.mark.asyncio
async def test_managed_child_death_is_left_to_the_fatal_path(tmp_path, monkeypatch):
    """A bridge we spawned has a returncode to poll: in-place recovery must not double-supervise it."""
    adapter, first = await _adopt_stub(tmp_path, monkeypatch)
    spawn = AsyncMock(return_value=True)
    try:
        adapter._bridge_process = first  # adopted handle replaced by a managed child
        adapter._spawn_bridge_process = spawn
        assert await adapter._recover_adopted_bridge(RuntimeError("bridge gone")) is False
        spawn.assert_not_called()
    finally:
        adapter._bridge_process = None
        await _teardown(adapter, first)
