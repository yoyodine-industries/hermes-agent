"""Default runtime cap — slot recycling (ruling ``t_b2865b89``, F2 of the spawn-slot work).

The live measurement behind the card: six running cards, five of them goal-armed,
every one of them with ``max_runtime_seconds IS NULL``. ``enforce_max_runtime`` is
gated on ``max_runtime_seconds IS NOT NULL``, so a card that carries no cap is
*never reaped* — its slot is held until the worker exits on its own, and the tick
budget sat at 0 for hours while hundreds of cards waited behind those six.

``kanban.default_max_runtime_seconds`` (default 7200) closes that: the dispatcher
stamps it onto a card that carries no cap AT CLAIM TIME, so every running slot is
always bounded and reapable. A card's own value always wins, and an explicit ``0``
its author set stays unlimited.

The half that is easy to get wrong: the defaulted cap is a SCHEDULING device, not
a budget anyone authored. ``_worker_terminal_timeout_env`` raises a worker's
``TERMINAL_TIMEOUT`` to ``cap - 30 s``; letting the default through there would
jump every lane in the fleet from the generic terminal default to 7170 s inside a
scheduling fix. These tests pin both halves.

Hermetic: every board lives under a per-test ``HERMES_HOME``.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

import pytest

_LANES = ("alice", "bob", "carol", "dave", "erin", "worker")


@pytest.fixture()
def env(monkeypatch):
    """Fresh HERMES_HOME with a profile dir per lane, plus the kanban modules.

    Same shape as ``test_kanban_lane_fair_spawn.py``: a bare directory is not a
    profile, so each lane gets a ``config.yaml`` marker, and the kanban path pins
    from the *worker* environment are cleared so no scratch board can ever resolve
    to the operator's live board.

    The ``hermes_cli`` purge is UNDONE on teardown: this module sorts before the
    other kanban test files, so leaving re-imported module objects in ``sys.modules``
    would hand a later file a different ``hermes_cli.kanban_db`` than the one it bound
    at collection time.
    """
    test_home = tempfile.mkdtemp(prefix="kanban_default_runtime_test_")
    for prof in (*_LANES, "default"):
        os.makedirs(os.path.join(test_home, "profiles", prof), exist_ok=True)
        with open(os.path.join(test_home, "profiles", prof, "config.yaml"), "w") as fh:
            fh.write("{}\n")
    monkeypatch.setenv("HERMES_HOME", test_home)
    for name in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME",
                 "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_DELEGATED_CHILD_CONTEXT",
                 "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(name, raising=False)
    # The child env a spawn builds is a snapshot of the RUNNER's ambient environment:
    # ``build_subprocess_env`` carries ``TERMINAL_*`` through, and a routed lane strips only
    # the config-mapped names — ``TERMINAL_MAX_FOREGROUND_TIMEOUT`` is not one of them. A
    # worker session whose OWN card carries an explicit cap therefore exports
    # ``TERMINAL_MAX_FOREGROUND_TIMEOUT=7170``, the very value
    # ``test_the_default_does_not_raise_the_worker_terminal_timeout`` compares against, so the
    # suite's greenness would depend on which worker happened to run it. Clear the pair here
    # so that test measures the dispatcher, not the session invoking pytest; the test pins
    # ``TERMINAL_TIMEOUT`` itself, so clearing it only removes the ambient baseline.
    for name in ("TERMINAL_TIMEOUT", "TERMINAL_MAX_FOREGROUND_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)

    def _is_hermes_ish(mod: str) -> bool:
        return (mod.startswith("hermes_cli") or mod.startswith("hermes_state")
                or mod == "hermes_constants")

    saved = {m: sys.modules[m] for m in list(sys.modules) if _is_hermes_ish(m)}
    for mod in list(sys.modules.keys()):
        if _is_hermes_ish(mod):
            del sys.modules[mod]
    from hermes_cli import (kanban_db, kanban_db_connect, kanban_db_dispatch,
                            kanban_priority_policy)
    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
    yield kanban_db, kanban_db_connect, kanban_db_dispatch, kanban_priority_policy
    # Hand the collection-time modules back to whatever file runs next.
    for mod in list(sys.modules.keys()):
        if _is_hermes_ish(mod):
            del sys.modules[mod]
    sys.modules.update(saved)


def _open(env):
    _, kbc, _, _ = env
    return kbc.connect_closing()


def _mk(kb, conn, title, assignee, *, max_runtime_seconds=None):
    return kb.create_task(
        conn, title=title, assignee=assignee, max_runtime_seconds=max_runtime_seconds,
    )


class _SpawnRecorder:
    """Spawn stub that records what the dispatcher actually handed it."""

    def __init__(self, pid=999999):
        self.pid = pid
        self.calls: list[tuple] = []

    def __call__(self, task, workspace, *args, **kwargs):
        self.calls.append((task, workspace))
        return self.pid


def _dispatch(env, conn, *, budget=1, default_max_runtime_seconds=None, rec=None):
    _, _, kbd, _ = env
    rec = rec or _SpawnRecorder()
    kwargs = dict(spawn_fn=rec, dry_run=False, max_spawn=int(budget))
    if default_max_runtime_seconds is not None:
        kwargs["default_max_runtime_seconds"] = default_max_runtime_seconds
    res = kbd.dispatch_once(conn, **kwargs)
    return res, rec


def _cap(conn, tid):
    row = conn.execute(
        "SELECT max_runtime_seconds FROM tasks WHERE id = ?", (tid,)).fetchone()
    return row[0]


# ---------------------------------------------------------------------------
# T5a — the default is stamped onto a card that carries none
# ---------------------------------------------------------------------------

def test_default_max_runtime_seconds_is_stamped_when_the_card_has_none(env):
    """A card with no cap gets 7200 written at claim, with an audit event.

    Without this the card is invisible to ``enforce_max_runtime`` for its whole
    life: a NULL cap is never reaped, which is how six workers held every slot.
    """
    kb, _kbc, _kbd, _ = env
    with _open(env) as conn:
        tid = _mk(kb, conn, "uncapped", "alice")
        assert _cap(conn, tid) is None
        res, rec = _dispatch(env, conn, default_max_runtime_seconds=7200)
        assert [t for t, _lane, _ws in res.spawned] == [tid]
        assert _cap(conn, tid) == 7200
        # The stamp is auditable, not a silent column mutation.
        events = kb.list_events(conn, tid)
        stamp = [e for e in events if e.kind == "runtime_cap_defaulted"]
        assert len(stamp) == 1
        assert stamp[0].payload["max_runtime_seconds"] == 7200
        assert stamp[0].payload["source"] == "kanban.default_max_runtime_seconds"


# ---------------------------------------------------------------------------
# T5b — a card's OWN cap always wins (900, 0 = the author's opt-out, 86400)
# ---------------------------------------------------------------------------

def test_an_explicit_cap_is_never_overwritten(env):
    """900 / 0 / 86400 all survive: the stamp is a COALESCE, and ``0`` is a real value.

    ``0`` is the author's "no budget" opt-out — the create path stores it on purpose
    (``--max-runtime`` rejects ``0`` but ``kanban edit``/API may set it). A stamp that
    treated ``0`` as "unset" would silently bound a card its author deliberately left
    unbounded, and a stamp that overwrote ``900`` would lengthen a deliberate cap.
    """
    kb, _kbc, _kbd, _ = env
    with _open(env) as conn:
        keep_900 = _mk(kb, conn, "short", "alice", max_runtime_seconds=900)
        keep_0 = _mk(kb, conn, "unbounded", "bob", max_runtime_seconds=0)
        keep_big = _mk(kb, conn, "long", "carol", max_runtime_seconds=86400)
        res, _rec = _dispatch(env, conn, budget=3, default_max_runtime_seconds=7200)
        assert len(res.spawned) == 3
        assert _cap(conn, keep_900) == 900
        assert _cap(conn, keep_0) == 0
        assert _cap(conn, keep_big) == 86400
        # None of them was "defaulted", so none carries the audit event.
        for tid in (keep_900, keep_0, keep_big):
            assert not [e for e in kb.list_events(conn, tid)
                        if e.kind == "runtime_cap_defaulted"]


# ---------------------------------------------------------------------------
# T5c — the knob's off switch
# ---------------------------------------------------------------------------

def test_zero_disables_the_stamp(env):
    """``default_max_runtime_seconds: 0`` leaves the column NULL — no behaviour change."""
    kb, _kbc, _kbd, _ = env
    with _open(env) as conn:
        tid = _mk(kb, conn, "uncapped", "alice")
        _dispatch(env, conn, default_max_runtime_seconds=0)
        assert _cap(conn, tid) is None
        assert not [e for e in kb.list_events(conn, tid)
                    if e.kind == "runtime_cap_defaulted"]


def test_malformed_config_falls_open_to_the_production_default(env):
    """A typo must not silently disable slot recycling (fail open, not off)."""
    _, _, kbd, _ = env
    assert kbd.default_max_runtime_seconds_config({}) == 7200
    assert kbd.default_max_runtime_seconds_config(
        {"default_max_runtime_seconds": 0}) == 0
    assert kbd.default_max_runtime_seconds_config(
        {"default_max_runtime_seconds": "1800"}) == 1800
    for bad in ("", "off", None, -1, "30 bananas"):
        assert kbd.default_max_runtime_seconds_config(
            {"default_max_runtime_seconds": bad}) == 7200


# ---------------------------------------------------------------------------
# T5d — the defaulted cap must NOT reach the worker's terminal timeout
# ---------------------------------------------------------------------------

def test_the_default_does_not_raise_the_worker_terminal_timeout(env, monkeypatch):
    """The spawned worker keeps the generic TERMINAL_TIMEOUT; only an EXPLICIT cap raises it.

    ``_worker_terminal_timeout_env`` raises the child's terminal default to
    ``cap - 30 s``. Applied to the defaulted cap that is a fleet-wide jump from the
    generic terminal default to 7170 s, smuggled in by a scheduling change. The
    dispatcher therefore hands the spawn the card's EXPLICIT pre-stamp value.
    """
    kb, _, kbd, _ = env

    # The magnitude of the trap, stated: an explicit 7200 cap DOES widen the child.
    assert kbd._worker_terminal_timeout_env(7200, "3600") == "7170"
    # A defaulted card has no explicit cap, so there is nothing to widen from.
    assert kbd._worker_terminal_timeout_env(None, "3600") is None

    with _open(env) as conn:
        tid = _mk(kb, conn, "uncapped", "alice")
        _res, rec = _dispatch(env, conn, default_max_runtime_seconds=7200)
        assert len(rec.calls) == 1
        task, workspace = rec.calls[0]
        # The CARD now carries the scheduler's default...
        assert _cap(conn, tid) == 7200
        # ...but the Task handed to the spawn carries only what the author set.
        assert task.max_runtime_seconds is None

        # And the child env really is unaffected: capture it at the one seam that builds it.
        captured: dict = {}

        class _FakePopen:
            pid = 424242

            def __init__(self, cmd, **kwargs):
                captured["env"] = dict(kwargs.get("env") or {})
                captured["cmd"] = list(cmd)

        class _SubprocessShim:
            """Swap ONLY ``kanban_db_dispatch.subprocess.Popen``.

            Patching the stdlib module globally would also break ``subprocess.run``
            (the live-system guard shells out to ``git rev-parse``), so the shim is
            bound to the dispatcher's own module attribute and proxies everything else.
            """

            def __init__(self, popen):
                self._popen = popen

            def __getattr__(self, name):
                import subprocess as _sp
                return getattr(_sp, name)

            def Popen(self, *args, **kwargs):
                return self._popen(*args, **kwargs)

        monkeypatch.setattr(kbd, "subprocess", _SubprocessShim(_FakePopen))
        monkeypatch.setenv("TERMINAL_TIMEOUT", "3600")
        kbd._default_spawn(task, str(workspace), board="default")

        assert captured.get("env") is not None, "child env was never built"
        # The `env` fixture clears the ambient TERMINAL_* pair, so nothing the RUNNER's own
        # card exported can supply the literal below — that is what made the suite's colour
        # depend on which worker invoked pytest. With the baseline removed this check is
        # exact: a module regression that let the defaulted 7200 cap past the explicit-only
        # gate would set BOTH variables to 7170 (7200 − the 30 s grace, see the magnitude
        # check above), and nothing else in this child env legitimately carries that value.
        for var in ("TERMINAL_TIMEOUT", "TERMINAL_MAX_FOREGROUND_TIMEOUT"):
            assert captured["env"].get(var) != "7170", (
                f"{var} picked up the defaulted cap — the boundary leaked")


def test_the_worker_context_reports_the_explicit_cap_only(env):
    """The prompt header must not claim a terminal timeout the child does not have.

    A worker told ``Terminal timeout: 7170s`` starts a command it will be killed in.
    """
    kb, _kbc, _kbd, _ = env
    with _open(env) as conn:
        defaulted = _mk(kb, conn, "uncapped", "alice")
        explicit = _mk(kb, conn, "capped", "bob", max_runtime_seconds=7200)
        _dispatch(env, conn, budget=2, default_max_runtime_seconds=7200)

        # Defaulted card: its cap is shown (true — it WILL be reaped at 7200s), but
        # no terminal timeout is claimed off a cap nobody authored.
        ctx = kb.build_worker_context(conn, defaulted)
        assert "Max runtime: 7200s" in ctx
        assert "Terminal timeout:" not in ctx

        # Explicit card: unchanged — the author's cap still widens the terminal default.
        ctx = kb.build_worker_context(conn, explicit)
        assert "Max runtime: 7200s" in ctx
        assert "Terminal timeout: 7170s" in ctx


# ---------------------------------------------------------------------------
# T5e — the stamp is LIVE, not decorative: the sweep actually reaps
# ---------------------------------------------------------------------------

def test_a_worker_past_the_defaulted_cap_is_reaped(env, monkeypatch):
    """The point of the stamp: a card that had no cap is now reapable.

    ``enforce_max_runtime`` only ever looks at rows with a non-NULL cap, so this is
    the end-to-end proof the defaulted cap reaches the sweep.
    """
    kb, kbc, kbd, _ = env
    import hermes_cli.kanban_db as _kb

    killed: list = []
    with _open(env) as conn:
        tid = _mk(kb, conn, "uncapped", "alice")
        _dispatch(env, conn, default_max_runtime_seconds=7200)
        assert _cap(conn, tid) == 7200

        # The worker is long past the cap (backdate the active run, not just the task).
        old = int(time.time()) - 7200 - 60
        with _kb.write_txn(conn):
            conn.execute("UPDATE tasks SET started_at = ? WHERE id = ?", (old, tid))
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)", (old, tid))
        row = conn.execute("SELECT worker_pid FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert row["worker_pid"] is not None, "dispatcher did not record the spawned pid"

        # The recorded pid is not this process; make the liveness probe deterministic
        # (the sweep must not signal a stranger, and must not wait on a real process).
        monkeypatch.setattr(_kb, "_pid_alive", lambda pid: False)

        timed_out = kbd.enforce_max_runtime(conn, signal_fn=lambda pid, sig: killed.append((pid, sig)))
        assert tid in timed_out
        assert kb.get_task(conn, tid).status == "ready"
        event = next(e for e in kb.list_events(conn, tid) if e.kind == "timed_out")
        assert event.payload["limit_seconds"] == 7200


def test_an_unstamped_card_is_not_reaped(env):
    """The negative control: with the stamp off, the same card stays invisible to the sweep.

    This is the pre-fix state the card was filed about — a running card that no sweep
    will ever release.
    """
    kb, kbc, kbd, _ = env
    import hermes_cli.kanban_db as _kb

    with _open(env) as conn:
        tid = _mk(kb, conn, "uncapped", "alice")
        _dispatch(env, conn, default_max_runtime_seconds=0)
        assert _cap(conn, tid) is None
        old = int(time.time()) - 7200 - 60
        with _kb.write_txn(conn):
            conn.execute("UPDATE tasks SET started_at = ? WHERE id = ?", (old, tid))
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)", (old, tid))
        assert kbd.enforce_max_runtime(conn, signal_fn=lambda pid, sig: None) == []
        assert kb.get_task(conn, tid).status == "running"


# ---------------------------------------------------------------------------
# T5f — a stored ``0`` is "no cap": the sweep must not reap it (t_f6f7e27f)
# ---------------------------------------------------------------------------

def _backdate_attempt(conn, tid, *, seconds_ago=7200):
    """Move the card and its ACTIVE run row into the past (runtime is per attempt)."""
    import hermes_cli.kanban_db as _kb

    old = int(time.time()) - int(seconds_ago) - 60
    with _kb.write_txn(conn):
        conn.execute("UPDATE tasks SET started_at = ? WHERE id = ?", (old, tid))
        conn.execute(
            "UPDATE task_runs SET started_at = ? "
            "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)", (old, tid))
    return old


def test_a_zero_cap_is_not_reaped(env, monkeypatch):
    """A stored ``0`` is the author's "no cap" opt-out, not a 0-second budget.

    The column's ``0`` means no cap: ``_parse_duration`` normalises the CLI's ``0``
    to unset for exactly this reason, and F2's stamp deliberately treats a stored
    ``0`` as the card's own value (so it survives a claim). The sweep's population
    must therefore exclude non-positive caps — otherwise ``elapsed < limit`` is
    false for ANY elapsed and the card is SIGTERMed on the next tick with
    ``limit_seconds 0``, burning a strike toward the consecutive-failure breaker.
    """
    kb, _kbc, kbd, _ = env
    import hermes_cli.kanban_db as _kb

    with _open(env) as conn:
        tid = _mk(kb, conn, "unbounded", "alice", max_runtime_seconds=0)
        # The default stamp is ON and must still leave the explicit 0 alone
        # (that is the shape that reaches the sweep with a stored 0).
        _dispatch(env, conn, default_max_runtime_seconds=7200)
        assert _cap(conn, tid) == 0

        _backdate_attempt(conn, tid)
        # If the row reached the liveness probe on the buggy predicate, this makes
        # the worker PROVEN dead and the reap deterministic.
        monkeypatch.setattr(_kb, "_pid_alive", lambda pid: False)

        assert kbd.enforce_max_runtime(conn, signal_fn=lambda pid, sig: None) == []
        assert kb.get_task(conn, tid).status == "running"
        assert not [e for e in kb.list_events(conn, tid) if e.kind == "timed_out"]


def test_a_one_second_cap_is_reaped(env, monkeypatch):
    """Positive control: the same sweep DOES reap a real (1 s) cap.

    Proves the exclusion is ``<= 0`` and not a blanket "never reap".
    """
    kb, _kbc, kbd, _ = env
    import hermes_cli.kanban_db as _kb

    killed: list = []
    with _open(env) as conn:
        tid = _mk(kb, conn, "tiny", "alice", max_runtime_seconds=1)
        _dispatch(env, conn, default_max_runtime_seconds=7200)
        assert _cap(conn, tid) == 1

        _backdate_attempt(conn, tid)
        monkeypatch.setattr(_kb, "_pid_alive", lambda pid: False)

        timed_out = kbd.enforce_max_runtime(
            conn, signal_fn=lambda pid, sig: killed.append((pid, sig)))
        assert tid in timed_out
        assert kb.get_task(conn, tid).status == "ready"
        event = next(e for e in kb.list_events(conn, tid) if e.kind == "timed_out")
        assert event.payload["limit_seconds"] == 1
