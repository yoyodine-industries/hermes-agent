"""Tests: the relay lane's delivery handler (``tui_gateway/methods_bot_relay.py``).

Split out of ``tests/tools/test_bot_turn_lock.py`` when that module's lock/holder tests moved to
``platforms("posix")`` (t_f93b2489). These three drive the bound ``bot_relay.deliver`` handler
through ``tui_gateway.server``, whose import boots ``hermes_bootstrap`` — which settles a pending
update against the checkout's gitdir. On a lane whose checkout lives under the real home that stat
is exactly what ``tests/home_io_guard.py`` refuses to allow, so the declaration stays ``linux``:
the same lane these ran on before the split, and a guard trip there would be a harness bug, not
this handler's.

The holder half these assert (§3.7): ``data.reason`` is the only field the Desktop forwards, so
the blocked turn's holder rides BESIDE it — a refusal that ships only a JSON-RPC code reaches the
sending agent as "target_busy" and nobody, which is the SEV1 gap this closes.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

import pytest

pytestmark = pytest.mark.platforms("linux")

from tests.tools.test_bot_turn_lock import _WithReason, _hold_flock, _hold_flock_with_holder
from tools import bot_relay
from tools.bot_relay import TurnBusyError, turn_lock_path


def test_relay_deliver_returns_target_busy_error(tmp_path, monkeypatch):
    import tui_gateway.server as srv

    h = tmp_path / "h"
    (h / "profiles" / "ops").mkdir(parents=True)
    (h / "profiles" / "ops" / "config.yaml").touch()  # identity marker: bare dirs are not profiles
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setattr(bot_relay, "turn_wait_seconds", lambda: 0.2)

    spawned = {}

    # Deterministic spawn detection: sentinel argv from the exact factory the
    # deliver handler uses. A global subprocess.run patch also intercepts
    # unrelated gateway-init calls (git rev-parse / ls-remote in CI), so
    # never fuzzy-match argv — mark the delivery command itself.
    monkeypatch.setattr(
        bot_relay, "local_delivery_command", lambda prof, tmp: ["__delivery__", prof]
    )

    def _fake_run(argv, **kwargs):
        argv = list(argv or [])
        if argv and argv[0] == "__delivery__":
            spawned["argv"] = argv

        class _Done:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Done()

    monkeypatch.setattr("hermes_cli.quiet_single_query.run_reported_turn", _fake_run)

    held = threading.Event()
    release = threading.Event()
    t = threading.Thread(
        target=_hold_flock_with_holder,
        args=(turn_lock_path(h, "ops"), "ops", held, release),
        kwargs={"started_at": time.time() - 45},
    )
    t.start()
    assert held.wait(timeout=5)
    try:
        out = srv._methods["bot_relay.deliver"](1, {"profile": "ops", "message": "x"})
        assert "error" in out
        assert out["error"]["code"] == 5096
        assert "target_busy" in out["error"]["message"]
        assert out["error"]["data"]["reason"] == "target_busy"
        # The holder rides beside data.reason (§3.7): the refusal names the process holding
        # the seat, in the structured field AND in the message text the sender reads.
        holder = out["error"]["data"]["holder"]
        assert holder["kind"] == "turn_lock"
        assert holder["pid"] == os.getpid()
        assert holder["age_seconds"] >= 45
        assert holder["alive"] is True
        assert f"held by pid {os.getpid()}" in out["error"]["message"]
        assert not spawned, "turn must not spawn while the profile is busy"
    finally:
        release.set()
        t.join(timeout=5)


def test_relay_deliver_serializes_then_succeeds(tmp_path, monkeypatch):
    import tui_gateway.server as srv

    h = tmp_path / "h"
    (h / "profiles" / "ops").mkdir(parents=True)
    (h / "profiles" / "ops" / "config.yaml").touch()  # identity marker: bare dirs are not profiles
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setattr(bot_relay, "turn_wait_seconds", lambda: 5.0)

    class _Proc:
        returncode = 0
        stdout = "pong"
        stderr = ""

    monkeypatch.setattr("hermes_cli.quiet_single_query.run_reported_turn", lambda *a, **k: _Proc())

    held = threading.Event()
    release = threading.Event()
    t = threading.Thread(
        target=_hold_flock, args=(turn_lock_path(h, "ops"), held, release)
    )
    t.start()
    assert held.wait(timeout=5)
    threading.Timer(0.3, release.set).start()
    start = time.monotonic()
    out = srv._methods["bot_relay.deliver"](1, {"profile": "ops", "message": "x"})
    t.join(timeout=5)
    assert "error" not in out, out
    assert out["result"]["reply"] == "pong"
    assert time.monotonic() - start >= 0.2, "deliver should have queued"


@pytest.mark.parametrize(
    ("failure", "code", "reason"),
    [
        (TurnBusyError("ops", 0.2), 5096, "target_busy"),
        (subprocess.TimeoutExpired(["hermes"], 600), 5093, "delivery_timeout"),
        (RuntimeError("Error code: 401 - invalid api key"), 5094, "provider_auth_or_access"),
        (RuntimeError("something nobody has a rule for"), 5094, "unknown"),
        (_WithReason("CERTIFICATE_VERIFY_FAILED", "ssl handshake failed"), 5094, "unknown"),
        (_WithReason("provider_quota_limit", "quota exhausted"), 5094, "provider_quota_limit"),
    ],
    ids=["busy", "turn-timed-out", "classifiable-failure", "unclassifiable-failure",
         "reason-outside-the-vocabulary", "reason-inside-the-vocabulary"],
)
def test_every_relay_refusal_carries_its_typed_reason(tmp_path, monkeypatch, failure, code, reason):
    """`data.reason` is the only channel the Desktop forwards: it reads `error.data.reason` and puts
    it in the sender's reply file, and the sender re-classifies from free text otherwise — which can
    never produce these codes. A refusal that ships only a JSON-RPC code reaches the sending agent as
    `[reason: unknown]`, so it cannot tell "retry shortly" from an auth failure. The turn-failure
    branch already did this; these three did not."""
    import tui_gateway.server as srv

    h = tmp_path / "h"
    (h / "profiles" / "ops").mkdir(parents=True)
    (h / "profiles" / "ops" / "config.yaml").touch()  # identity marker: bare dirs are not profiles
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setattr(bot_relay, "local_delivery_command", lambda prof, tmp: ["__delivery__", prof])

    def _raise(argv, **kwargs):
        raise failure

    monkeypatch.setattr("hermes_cli.quiet_single_query.run_reported_turn", _raise)

    out = srv._methods["bot_relay.deliver"](1, {"profile": "ops", "message": "x"})

    assert out["error"]["code"] == code
    assert out["error"]["data"]["reason"] == reason
    if reason != "target_busy":
        # The plain TurnBusyError in this table names no holder: additive means the refusal is
        # byte-for-byte what it was — no `holder` key, and reason/class/code all unchanged.
        assert "holder" not in out["error"]["data"]
