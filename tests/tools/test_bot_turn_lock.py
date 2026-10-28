"""Tests: per-profile bot turn lock (#93091 — tools/bot_relay.py).

Two deliveries into the same target profile must serialize on a
cross-process flock; the queued one waits a bounded budget and then fails
with a structured 'target_busy' refusal. Real flock on real (short)
tmp_path lockfiles — flock contends between separate fds even within one
process, so threads exercise the true kernel-lock semantics.

``posix``, not ``linux``: the subject is ``fcntl.flock`` plus the holder record a live
turn publishes (paths, pids, ``os.kill(pid, 0)``), all of which behave the same on macOS —
where the delivery lane this protects actually runs (t_4a32d803: the routable seat wedged
under ``hermes -p default chat``, and a file gated to linux exercises none of it there).
The three ``tui_gateway.server``-driven handler tests moved to
``tests/tools/test_bot_relay_deliver_lane.py`` and stay declared ``linux``: importing that
server boots ``hermes_bootstrap``, which settles an update against the checkout's gitdir.
"""

from __future__ import annotations

try:
    import fcntl  # POSIX-only; on Windows the module is skipped wholesale
except ImportError:  # pragma: no cover - Windows
    fcntl = None
import json
import os
import subprocess
import sys
import threading
import time

import pytest

pytestmark = pytest.mark.platforms("posix")

from tools import bot_delivery_queue as delivery_queue
from tools import bot_mode_dm, bot_relay
from tools.bot_relay import (
    TurnBusyError,
    acquire_delivery_turn_lock,
    acquire_turn_lock,
    turn_lock_path,
)


@pytest.fixture
def root(tmp_path):
    # Keep the lockfile path SHORT (macOS-safe).
    r = tmp_path / "r"
    r.mkdir()
    return r


def _hold_flock(path, hold_event, release_event):
    """Grab the profile lock on a separate fd, signal, hold until told."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    hold_event.set()
    release_event.wait(timeout=10)
    os.close(fd)  # close releases the flock — process-death semantics


def test_second_delivery_waits_then_succeeds(root):
    held = threading.Event()
    release = threading.Event()
    t = threading.Thread(
        target=_hold_flock, args=(turn_lock_path(root, "ops"), held, release)
    )
    t.start()
    assert held.wait(timeout=5)

    # Release shortly after the waiter starts probing.
    threading.Timer(0.3, release.set).start()
    start = time.monotonic()
    with acquire_turn_lock(root, "ops", timeout_seconds=5):
        waited = time.monotonic() - start
    t.join(timeout=5)
    assert waited >= 0.2, "second delivery should have queued behind the holder"


def test_timeout_is_structured_target_busy(root):
    held = threading.Event()
    release = threading.Event()
    t = threading.Thread(
        target=_hold_flock, args=(turn_lock_path(root, "ops"), held, release)
    )
    t.start()
    assert held.wait(timeout=5)
    try:
        with pytest.raises(TurnBusyError) as excinfo:
            with acquire_turn_lock(root, "ops", timeout_seconds=0.3):
                pass  # pragma: no cover — must not acquire
        err = excinfo.value
        assert err.reason == "target_busy"
        assert err.profile == "ops"
        assert err.waited_seconds >= 0.3
    finally:
        release.set()
        t.join(timeout=5)


def test_different_profiles_do_not_contend(root):
    held = threading.Event()
    release = threading.Event()
    t = threading.Thread(
        target=_hold_flock, args=(turn_lock_path(root, "ops"), held, release)
    )
    t.start()
    assert held.wait(timeout=5)
    try:
        start = time.monotonic()
        with acquire_turn_lock(root, "scout", timeout_seconds=5):
            pass
        # Upper bound generous for loaded CI runners — the point is only
        # that 'scout' never waited the busy 'ops' budget out.
        assert time.monotonic() - start < 2.5
    finally:
        release.set()
        t.join(timeout=5)


def test_lock_released_when_holder_fd_closes(root):
    """flock dies with the holder's fd — a crashed turn can't wedge the profile."""
    path = turn_lock_path(root, "ops")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    os.close(fd)  # simulate holder process death (kernel releases the lock)
    with acquire_turn_lock(root, "ops", timeout_seconds=0.5):
        pass  # acquires immediately — no TurnBusyError


# ── the holder record (t_4a32d803 SEV1 residual / t_f93b2489 §2-§3) ──────────
# A wedged profile used to refuse every sender with "another delivery turn is
# already running" and name nobody: the seat stayed dead for 30+ hours because no
# waiter (and no operator) could tell WHICH process held the turn or how old it was.
# The holder now publishes itself into its own lockfile and every receipt/refusal
# that reports the slot as held names it.


def _hold_flock_with_holder(path, profile, hold_event, release_event, *, pid=None, started_at=None):
    """Grab the lock on a separate fd and publish a holder record, as a live turn does."""
    assert fcntl is not None  # POSIX-only module (pytestmark pins it)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    record = {"pid": os.getpid() if pid is None else pid, "ppid": os.getppid(), "profile": profile,
              "started_at": time.time() if started_at is None else started_at, "host": "test-host"}
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, json.dumps(record).encode("utf-8"))
    hold_event.set()
    release_event.wait(timeout=10)
    os.close(fd)  # close releases the flock — process-death semantics


_HOLDER_CHILD = '''\
import fcntl, json, os, sys, time
path, profile, hold_seconds = sys.argv[1], sys.argv[2], float(sys.argv[3])
os.makedirs(os.path.dirname(path), exist_ok=True)
fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
os.ftruncate(fd, 0)
os.lseek(fd, 0, os.SEEK_SET)
os.write(fd, (json.dumps({"pid": os.getpid(), "ppid": os.getppid(), "profile": profile,
                          "started_at": time.time(), "host": "child"}) + "\\n").encode("utf-8"))
print(os.getpid(), flush=True)
time.sleep(hold_seconds)
'''


def _spawn_holder_child(tmp_path, root, profile, hold_seconds=30.0):
    """A REAL separate process holding ``profile``'s turn, publishing its own pid → (proc, pid)."""
    script = tmp_path / "holder_child.py"
    script.write_text(_HOLDER_CHILD, encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(script), str(turn_lock_path(root, profile)), profile, str(hold_seconds)],
        stdout=subprocess.PIPE, text=True,
    )
    assert proc.stdout is not None
    try:
        pid = int(proc.stdout.readline().strip())
    except ValueError:  # pragma: no cover - the child never published
        proc.kill()
        raise
    return proc, pid


def test_turn_holder_is_published_while_held_and_cleared_after(root):
    """A held turn names itself (pid/profile/start), and leaves nothing stale behind."""
    with acquire_turn_lock(root, "ops", timeout_seconds=0.5):
        holder = bot_relay.read_turn_lock_holder(root, "ops")
        assert holder is not None
        assert holder["kind"] == "turn_lock"
        assert holder["pid"] == os.getpid()
        assert holder["profile"] == "ops"
        assert holder["host"]
        assert holder["alive"] is True, "the probe must see the live holder, not a guess"
        assert 0 <= holder["age_seconds"] < 30
    assert bot_relay.read_turn_lock_holder(root, "ops") is None
    assert turn_lock_path(root, "ops").read_bytes() == b"", "release clears the record"


def test_refusal_names_the_holder_and_its_age(root):
    """The bounded refusal carries who held the turn and for how long (the SEV1 acceptance)."""
    held, release = threading.Event(), threading.Event()
    t = threading.Thread(
        target=_hold_flock_with_holder,
        args=(turn_lock_path(root, "default"), "default", held, release),
        kwargs={"started_at": time.time() - 300},
    )
    t.start()
    assert held.wait(timeout=5)
    try:
        with pytest.raises(TurnBusyError) as excinfo:
            with acquire_turn_lock(root, "default", timeout_seconds=0.3):
                pass  # pragma: no cover — must not acquire
    finally:
        release.set()
        t.join(timeout=5)
    err = excinfo.value
    assert err.holder["pid"] == os.getpid()
    assert err.holder["alive"] is True
    assert err.holder["age_seconds"] is not None and err.holder["age_seconds"] >= 300
    assert f"pid {os.getpid()}" in str(err)
    assert "for 5m" in str(err), f"the refusal must state the holder's AGE: {err}"
    payload = err.holder_payload()
    assert payload["kind"] == "turn_lock"
    assert payload["pid"] == os.getpid()
    assert payload["profile"] == "default"
    assert payload["alive"] is True


def test_refusal_without_a_holder_record_stays_structured_target_busy(root):
    """An older build's empty lockfile refuses exactly as before — unknown, never an error."""
    held, release = threading.Event(), threading.Event()
    t = threading.Thread(target=_hold_flock, args=(turn_lock_path(root, "ops"), held, release))
    t.start()
    assert held.wait(timeout=5)
    try:
        with pytest.raises(TurnBusyError) as excinfo:
            with acquire_turn_lock(root, "ops", timeout_seconds=0.3):
                pass  # pragma: no cover
    finally:
        release.set()
        t.join(timeout=5)
    err = excinfo.value
    assert err.reason == "target_busy"
    assert err.holder is None and err.holder_payload() == {}
    assert "held by pid" not in str(err), "no pid is named when the lockfile carries no record"
    assert str(err).endswith("Do not resend."), "nothing is appended for an unknown holder"
    assert "Do not resend." in str(err)
    assert "target_busy: turn slot held by another turn; delivery queued" in str(err)


def test_a_stale_holders_record_is_reported_as_not_running(root, monkeypatch):
    """A leftover record from a killed holder says so — that is not a live turn to wait behind."""
    monkeypatch.setattr(bot_relay, "_pid_alive", lambda pid: False)
    held, release = threading.Event(), threading.Event()
    t = threading.Thread(
        target=_hold_flock_with_holder,
        args=(turn_lock_path(root, "default"), "default", held, release), kwargs={"pid": 4242},
    )
    t.start()
    assert held.wait(timeout=5)
    try:
        with pytest.raises(TurnBusyError) as excinfo:
            with acquire_turn_lock(root, "default", timeout_seconds=0.3):
                pass  # pragma: no cover
    finally:
        release.set()
        t.join(timeout=5)
    err = excinfo.value
    assert "pid 4242, no longer running" in str(err)
    assert err.holder_payload()["alive"] is False


def test_the_named_holder_can_be_reclaimed_and_the_queued_delivery_completes(tmp_path, root):
    """ACCEPTANCE (t_4a32d803): a real, separate holder process is NAMED by the refusal, so
    reclaiming exactly that process lets the queued delivery complete — no seat, gateway or
    agent process is touched, and no human has to guess which pid wedged the profile."""
    proc, pid = _spawn_holder_child(tmp_path, root, "default")
    try:
        with pytest.raises(TurnBusyError) as excinfo:
            with acquire_turn_lock(root, "default", timeout_seconds=0.3):
                pass  # pragma: no cover — the child holds it
        err = excinfo.value
        assert err.holder["pid"] == pid, "the refusal must name the OTHER process holding the seat"
        assert err.holder["alive"] is True
        assert f"pid {pid}" in str(err)

        # Reclaim exactly the named holder (kill it); its fd closes and the kernel drops the flock.
        proc.kill()
        proc.wait(timeout=10)
        assert proc.stdout is not None
        proc.stdout.close()

        start = time.monotonic()
        with acquire_turn_lock(root, "default", timeout_seconds=5):
            holder = bot_relay.read_turn_lock_holder(root, "default")
            assert holder is not None and holder["pid"] == os.getpid()
        assert time.monotonic() - start < 1.0, "the queued delivery must proceed, not wedge again"
    finally:
        if proc.poll() is None:  # pragma: no cover - the assertion path killed it
            proc.kill()
            proc.wait(timeout=10)



def test_lock_path_is_short_and_sanitized(root):
    p = turn_lock_path(root, "we/ird nam√©" + "x" * 200)
    assert p.parent == bot_relay.relay_root(root) / bot_relay.LOCKS_DIR
    assert len(p.name) <= 70
    assert "/" not in p.name.replace(".lock", "")


def test_turn_wait_seconds_falls_back_to_module_constant(monkeypatch):
    def _boom():
        raise RuntimeError("no config")

    monkeypatch.setattr("hermes_cli.config.load_config", _boom)
    assert bot_relay.turn_wait_seconds() == float(bot_relay.TURN_WAIT_SECONDS_FALLBACK)


def test_turn_wait_seconds_reads_config(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"bot_mode": {"turn_wait_seconds": 7}},
    )
    assert bot_relay.turn_wait_seconds() == 7.0


# ── wiring: local teammate delivery (tools/bot_mode_dm.py) ──────────────────


def test_run_delivery_holds_the_bot_chat_lock_during_turn(root, tmp_path, monkeypatch):
    """The local `hermes -p <profile>` delivery turn runs UNDER the profile's BOT CHAT lock.

    …and only under that one (``t_47082c1d``): while the turn runs, a second delivery into the same
    Bot Chat must fail, while a turn on another transcript of the same seat must not.
    """
    home = root / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    dm = tmp_path / "dm.txt"
    dm.write_text("hi", encoding="utf-8")
    observed = {}

    def _fake_run(argv, **kwargs):
        # While the turn runs, a second acquire on the same transcript must fail…
        with pytest.raises(TurnBusyError):
            with acquire_turn_lock(home, "ops", timeout_seconds=0.15,
                                   session=bot_relay.BOT_CHAT_SESSION_KEY):
                pass  # pragma: no cover
        # …and a different transcript of the same profile is free.
        with acquire_turn_lock(home, "ops", timeout_seconds=0.15,
                               session=bot_relay.room_session_key("other-room")):
            pass
        observed["argv"] = argv

        class _P:
            returncode = 0
            stdout = ""
            stderr = ""

        return _P()

    monkeypatch.setattr(bot_mode_dm.subprocess, "run", _fake_run)
    rc = bot_mode_dm._run_delivery(
        ["hermes", "-p", "ops", "chat"], str(dm), stdin_file=False
    )
    assert rc == 0
    assert observed["argv"][:3] == ["hermes", "-p", "ops"]
    # …and after the turn, the lock is free again.
    with acquire_turn_lock(home, "ops", timeout_seconds=0.5,
                           session=bot_relay.BOT_CHAT_SESSION_KEY):
        pass


def test_turn_busy_message_says_queued_not_failed():
    """§3.1/P10(a): a held turn slot queues the delivery — the old 'NOT delivered' text is gone.

    The holder is additive (§2): with none known, the message is the LIVE one verbatim — the
    durable-queue sentence, ``Do not resend.`` and the typed ``target_busy`` reason all survive.
    """
    from tools.bot_failure_reasons import delivery_failure_reason

    error = TurnBusyError("ops", 0.05)
    assert error.reason == "target_busy"
    assert error.status_detail == "target_busy: turn slot held by another turn; delivery queued"
    assert ("The delivery is durably queued; the receiver re-offers it when the turn slot frees "
            "(status_detail: 'target_busy: turn slot held by another turn; delivery queued'). "
            "Do not resend.") in str(error)
    assert "NOT delivered" not in str(error) and "retry shortly" not in str(error)
    assert delivery_failure_reason(error) == "target_busy"
    # Unknown holder ⇒ nothing appended: the fragment is the ONLY difference (append at the END).
    assert not str(error).endswith(" ")
    assert error.holder is None and error.holder_payload() == {}

    named = TurnBusyError("ops", 0.05, {"kind": "turn_lock", "pid": 1234, "age_seconds": 252.0,
                                        "alive": True})
    assert str(named) == str(error) + " — held by pid 1234 for 4m 12s"
    assert delivery_failure_reason(named) == "target_busy"


def test_delivery_turn_lock_probe_is_non_blocking(root, tmp_path, monkeypatch):
    """P10(b)/§2.5 step 1: the delivery path probes with timeout 0 — a held lock raises now."""
    home = root / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(bot_relay, "turn_wait_seconds", lambda: 30)  # must be irrelevant
    held = threading.Event()
    release = threading.Event()
    t = threading.Thread(target=_hold_flock,
                         args=(turn_lock_path(home, "ops", bot_relay.BOT_CHAT_SESSION_KEY),
                               held, release))
    t.start()
    assert held.wait(timeout=5)
    try:
        started = time.monotonic()
        with pytest.raises(TurnBusyError):
            with acquire_delivery_turn_lock(home, "ops", bot_relay.BOT_CHAT_SESSION_KEY):
                pass
        assert time.monotonic() - started < 1.0, "the delivery probe must never wait"
    finally:
        release.set()
        t.join(timeout=5)


def test_delivery_main_reports_target_busy_json(root, tmp_path, monkeypatch, capsys):
    """A delivery that cannot take the turn slot is RETAINED: the sender gets a receipt.

    P9(b)/§3.1 — stdout carries the 24-field envelope, the exit code stays 0, and
    ``target_busy`` appears only as the receipt's ``status_detail`` (never as a reason).
    NEGATIVE CONTROL (§5.2, D2 "measured, never assumed"): this holder writes NO record — an
    older build's empty lockfile — so ``holder`` stays ``None`` and ``detail`` is unchanged.
    """
    home = root / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(bot_relay, "turn_wait_seconds", lambda: 0.2)
    dm = tmp_path / "dm.txt"
    dm.write_text("hi", encoding="utf-8")

    held = threading.Event()
    release = threading.Event()
    t = threading.Thread(
        target=_hold_flock,
        args=(turn_lock_path(home, "ops", bot_relay.BOT_CHAT_SESSION_KEY), held, release)
    )
    t.start()
    assert held.wait(timeout=5)
    try:
        rc = bot_mode_dm._delivery_main(
            ["--run-delivery", "query-file", str(dm), "hermes", "-p", "ops", "chat"]
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["result"] == "receipt"
        assert payload["status"] == "queued"
        assert payload["status_detail"] == "target_busy: turn slot held by another turn; delivery queued"
        assert payload["busy"] is True and payload["attempts"] == 0
        assert payload["reason"] is None
        assert payload["profile"] == "ops"
        assert list(payload) == list(delivery_queue.ENVELOPE_FIELDS)  # §1.3 order, no extras
        assert payload["holder"] is None, "no holder record ⇒ unknown, and `{}` is never printed"
        assert payload["detail"] == delivery_queue.DETAIL_RECEIPT, "no fragment without a holder"
        assert "reoffer_count" not in payload  # internal bookkeeping only
    finally:
        release.set()
        t.join(timeout=5)
    assert not dm.exists(), "DM plaintext must be reclaimed even when the delivery is retained"


def test_delivery_main_names_the_holder_in_the_retained_receipt(root, tmp_path, monkeypatch, capsys):
    """§5.1: a held slot's receipt NAMES the holder, in the envelope AND in the human text.

    The sender agent reads the receipt's ``detail`` (the Desktop forwards only ``data.reason``
    and the TEXT), so a receipt that reports "busy" without a name leaves the SEV1 gap open.
    """
    home = root / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(bot_relay, "turn_wait_seconds", lambda: 0.2)
    dm = tmp_path / "dm.txt"
    dm.write_text("hi", encoding="utf-8")

    held = threading.Event()
    release = threading.Event()
    t = threading.Thread(
        target=_hold_flock_with_holder,
        args=(turn_lock_path(home, "ops", bot_relay.BOT_CHAT_SESSION_KEY), "ops", held, release),
        kwargs={"started_at": time.time() - 90},
    )
    t.start()
    assert held.wait(timeout=5)
    try:
        rc = bot_mode_dm._delivery_main(
            ["--run-delivery", "query-file", str(dm), "hermes", "-p", "ops", "chat"]
        )
        assert rc == 0, "a held slot is a durable hand-off, never a failure"
        payload = json.loads(capsys.readouterr().out.strip())
    finally:
        release.set()
        t.join(timeout=5)

    assert payload["result"] == "receipt"
    assert payload["status"] == "queued"
    assert payload["status_detail"] == delivery_queue.STATUS_DETAIL_TARGET_BUSY, "byte-identical"
    assert payload["busy"] is True and payload["attempts"] == 0
    assert payload["reason"] is None
    assert list(payload) == list(delivery_queue.ENVELOPE_FIELDS)
    holder = payload["holder"]
    assert holder["kind"] == "turn_lock"
    assert holder["pid"] == os.getpid(), "the REAL holder's pid"
    assert holder["age_seconds"] >= 90
    assert holder["alive"] is True
    assert "held by pid" in payload["detail"], payload["detail"]
    assert payload["detail"] == f"{delivery_queue.DETAIL_RECEIPT} — held by pid {os.getpid()} for 1m" \
                                f" {int(holder['age_seconds']) % 60:02d}s"


def test_peer_stdin_delivery_skips_local_lock(root, tmp_path, monkeypatch):
    """Peer transports run their turn on the remote gateway — no local lock."""
    home = root / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    dm = tmp_path / "dm.txt"
    dm.write_text("hi", encoding="utf-8")

    held = threading.Event()
    release = threading.Event()
    t = threading.Thread(
        target=_hold_flock, args=(turn_lock_path(home, "ops"), held, release)
    )
    t.start()
    assert held.wait(timeout=5)
    try:

        def _fake_run(argv, **kwargs):
            class _P:
                returncode = 0

            return _P()

        monkeypatch.setattr(bot_mode_dm.subprocess, "run", _fake_run)
        rc = bot_mode_dm._run_delivery(
            ["hermes", "peer", "dm", "spark/ops"], str(dm), stdin_file=True
        )
        assert rc == 0  # did not contend with the held 'ops' lock
    finally:
        release.set()
        t.join(timeout=5)


# ── wiring: relay deliver RPC (tui_gateway/methods_bot_relay.py) ─────────────
# The bound handler is driven in tests/tools/test_bot_relay_deliver_lane.py
# (platforms("linux"): importing tui_gateway.server boots the gateway bootstrap).
# What is asserted here is importable without that boot.


def test_local_delivery_command_never_reenters_the_lock():
    """The gateway deliver handler runs local_delivery_command ALREADY holding
    the profile lock. That argv must stay a raw hermes CLI invocation:
    routing it through the --run-delivery wrapper would make the child hit
    _delivery_lock (hermes CLI + '-p'), burn the full wait
    budget against its parent's flock, and fail every relay delivery with
    target_busy. argv[0] may be a resolved venv path (#93590) — the lock
    matcher and this assertion both go by basename."""
    from pathlib import Path

    argv = bot_relay.local_delivery_command("ops", "/tmp/q.txt")
    assert argv[1:3] == ["-p", "ops"]
    assert Path(argv[0]).name in ("hermes", "hermes.exe")
    assert "--run-delivery" not in argv
    assert not any("bot_mode_dm" in part for part in argv)


class _WithReason(RuntimeError):
    """An exception carrying its own ``reason``. ``reason`` is a stdlib attribute on
    ``ssl.SSLError`` and ``urllib.error.URLError`` too, so a refusal must not forward whatever
    it finds there into a channel whose consumers expect a closed vocabulary.

    Shared with ``tests/tools/test_bot_relay_deliver_lane.py``, which imports it from here.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (RuntimeError("Error code: 401 - invalid api key"), "provider_auth_or_access"),
        (RuntimeError("something nobody has a rule for"), "unknown"),
        (_WithReason("CERTIFICATE_VERIFY_FAILED", "ssl handshake failed"), "unknown"),
    ],
    ids=["classifiable-failure", "unclassifiable-failure", "reason-outside-the-vocabulary"],
)
def test_delivery_main_reports_every_failure_as_typed_json(tmp_path, monkeypatch, capsys, failure, reason):
    """The local lane's runner stdout IS the sender's completion notification. A failure other
    than target_busy used to reach the sender as stderr prose with no reason, so it could not
    tell an auth failure from a transient one; it now rides the same vocabulary as the relay."""
    dm = tmp_path / "dm.txt"
    dm.write_text("hi", encoding="utf-8")

    def _raise(*args, **kwargs):
        raise failure

    monkeypatch.setattr(bot_mode_dm, "_run_delivery", _raise)

    rc = bot_mode_dm._delivery_main(["--run-delivery", "query-file", str(dm), "hermes", "-p", "ops", "chat"])

    assert rc == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload == {"error": str(failure), "reason": reason}
