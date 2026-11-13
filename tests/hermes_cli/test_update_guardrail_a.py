"""The guardrail-A contract for the updater (R1 + R2 + R4, card t_c54b589f).

Three defects, one change, and these are the tests that hold each of them down:

* **R4** — a FAILED update used to park its autostash unconditionally and forget it ("tree state
  unknown"). After an abort git itself completed the tree state is KNOWN, so the dirt goes back and
  the pending pointer is never cleared on a clean abort. A TORN tree still keeps today's behaviour.
* **R1** — liveness is a LEASE: the marker carries a boot id and a heartbeat, a dead claim is
  reclaimed within ``LEASE_TTL + LEASE_GRACE`` with no human action, a live claim with a fresh
  heartbeat still refuses, a refusal never says ``pid=0`` without naming the descendants that hold
  the checkout, and the reclaim is a NAMED event (``reclaimed_dead_lease``).
* **R2** — an abnormal exit always closes the receipt: the shutdown watchdog writes the terminal
  ``interrupted`` outcome (reason + last stage) before ``os._exit``, a second writer records
  ``already_closed_by`` instead of a second outcome, and a resumed run announces
  ``resumed_at_stage``.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from hermes_cli import update_cmd, update_lock, update_pause_record
from hermes_cli import update_receipt as ur
from hermes_cli.update_lock import UpdateLock

REPO_ROOT = Path(__file__).resolve().parents[2]
GIT = ["git"]

# The claim is written by a REAL process, which is then SIGKILLed: the tests must not fake a dead
# pid, or they would pass against an implementation that only believes its own record.
_CLAIM_CHILD = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, {repo!r})
    from hermes_cli.update_lock import UpdateLock
    lock = UpdateLock(path={marker!r})
    if not lock.acquire():
        print("REFUSED", flush=True)
        raise SystemExit(3)
    print("READY", flush=True)
    time.sleep(120)
    """
)


def _spawn_claim(tmp_path: Path) -> tuple[subprocess.Popen, Path]:
    marker = tmp_path / ".hermes-update-in-progress"
    script = _CLAIM_CHILD.format(repo=str(REPO_ROOT), marker=str(marker))
    child = subprocess.Popen([sys.executable, "-I", "-S", "-c", script],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert child.stdout is not None
    line = child.stdout.readline().strip()
    if line != "READY":
        child.kill()
        err = child.stderr.read() if child.stderr else ""
        raise AssertionError(f"child could not claim: {line!r} {err}")
    return child, marker


def _kill(child: subprocess.Popen) -> int:
    pid = child.pid
    child.send_signal(signal.SIGKILL)
    child.wait(timeout=30)
    return pid


def _age_heartbeat(marker: Path, seconds: float) -> None:
    """Rewrite the claim's ``hb:`` line into the past — the only honest way to age a lease."""
    lines = marker.read_text(encoding="utf-8-sig").splitlines()
    stamp = f"hb:{time.time() - seconds:.3f}"
    for index, line in enumerate(lines):
        if line.startswith("hb:"):
            lines[index] = stamp
            break
    else:
        lines.append(stamp)
    marker.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _marker_lines(marker: Path) -> list[str]:
    return marker.read_text(encoding="utf-8-sig").splitlines()


# --------------------------------------------------------------------------- R1: the lease


def test_a_live_update_with_a_fresh_heartbeat_still_refuses(tmp_path, monkeypatch):
    """The lease may not be so eager that it steals a window from a run that is still working."""
    monkeypatch.setattr(update_lock, "update_marker_path", lambda: tmp_path / ".hermes-update-in-progress")
    child, marker = _spawn_claim(tmp_path)
    try:
        pid = _kill(child)  # its heartbeat was written a moment ago: the lease has NOT run out
        holder = update_lock.read_live_update(path=marker)
        assert holder is not None and holder.pid == pid, (
            "a fresh heartbeat is proof of life: the claim must still refuse")
        second = UpdateLock(path=marker)
        assert second.acquire() is False
        assert second.holder is not None and second.holder.pid == pid
        assert marker.exists(), "a refused claim must not be deleted by the loser"
    finally:
        if child.poll() is None:  # pragma: no cover - defensive
            child.kill()


def test_a_sigkilled_holder_is_reclaimed_by_the_next_window(tmp_path, monkeypatch):
    """Acceptance 1 of R1: no human action, and the reclaim names the dead pid and the fd holders."""
    marker = tmp_path / ".hermes-update-in-progress"
    lock_path = tmp_path / "checkout.lock"
    monkeypatch.setattr(update_lock, "update_marker_path", lambda: marker)
    monkeypatch.setattr(update_lock, "checkout_lock_path", lambda root=None: lock_path)
    child, _ = _spawn_claim(tmp_path)
    try:
        dead_pid = _kill(child)
        # The lease bound, elapsed: LEASE_TTL + LEASE_GRACE with nothing refreshing the heartbeat.
        _age_heartbeat(marker, update_lock.LEASE_TTL_SECONDS + update_lock.LEASE_GRACE_SECONDS + 5)
        assert update_lock._read_marker(marker).claim_expired(), (
            "the lease (heartbeat + grace) has run out: this claim is reclaimable")

        reclaimed = update_lock.reconcile_interrupted_claim()
        assert reclaimed is not None, "the pre-lock reconciler must report the reclaim it made"
        assert reclaimed["dead_pid"] == dead_pid
        assert reclaimed["event"] == "reclaimed_dead_lease"

        ledger = lock_path.parent / "hermes-update-events.jsonl"
        events = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
        named = [e for e in events if e.get("event") == "reclaimed_dead_lease"]
        assert named, "a reclaim that leaves no named event is the silence that blocked the fleet"
        assert named[-1]["dead_pid"] == dead_pid
        assert "lock_fd_holders" in named[-1]
        assert named[-1]["heartbeat_age_s"] > update_lock.LEASE_TTL_SECONDS

        # And the window opens: the same lock the dead claim refused now claims.
        lock = UpdateLock(path=marker)
        assert lock.acquire() is True
        try:
            assert int(_marker_lines(marker)[0]) == os.getpid()
        finally:
            lock.release()
    finally:
        if child.poll() is None:  # pragma: no cover - defensive
            child.kill()


def test_an_expired_dead_claim_that_the_checkout_flock_survives_is_reclaimed(tmp_path, monkeypatch):
    """R1 item 2: the flock may BOUND a dead claim, never decide it forever."""
    marker = tmp_path / ".hermes-update-in-progress"
    lock_path = tmp_path / "checkout.lock"
    marker.write_text(f"999999\n{int(time.time()) - 600}\nct:1.0\nhb:{time.time() - 600:.3f}\n")
    monkeypatch.setattr(update_lock, "checkout_lock_path", lambda root=None: lock_path)
    monkeypatch.setattr(update_lock, "checkout_lock_held", lambda root=None: True)
    monkeypatch.setattr(update_lock, "_lock_fd_holders", lambda path: [4242])

    assert update_lock._reclaim_dead(marker) == "reclaimed", (
        "past LEASE_TTL + LEASE_GRACE the flock may no longer keep a dead claim alive")
    assert not marker.exists()
    events = [json.loads(line) for line in
              (lock_path.parent / "hermes-update-events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[-1]["dead_pid"] == 999999
    assert events[-1]["lock_fd_holders"] == [4242], "the survivor must be visible in the record"


def test_a_dead_claim_whose_flock_is_held_names_the_descendants_never_pid_zero(tmp_path, monkeypatch):
    """R1 item 4 + acceptance 3: the refusal says who holds the checkout, it does not say ``pid=0``."""
    marker = tmp_path / ".hermes-update-in-progress"
    # Dead pid, heartbeat older than the TTL — but NOT past TTL+grace, so the flock still binds it.
    stale = update_lock.LEASE_TTL_SECONDS + 5
    marker.write_text(f"999999\n{int(time.time()) - stale}\nct:1.0\nhb:{time.time() - stale:.3f}\n")
    monkeypatch.setattr(update_lock, "checkout_lock_held", lambda root=None: True)
    monkeypatch.setattr(update_lock, "_lock_fd_holders", lambda path: [4242, os.getpid()])

    holder = update_lock.read_live_update(path=marker)
    assert holder is not None and holder.held is True
    assert holder.reason, "a refusal must name the holder it believes in"
    assert "pids=4242" in holder.reason
    assert "age=" in holder.reason
    assert "999999" in holder.reason, "say WHICH dead update left the process tree behind"
    assert update_lock.describe_holder(holder).startswith("✗ The previous update did not finish")


def test_pause_frees_a_dead_claim_before_it_refuses(tmp_path, monkeypatch):
    """Acceptance: ``hermes pause`` succeeds after a killed update (R1 item 3, pause/drain path)."""
    marker = tmp_path / ".hermes-update-in-progress"
    marker.write_text(f"999999\n{int(time.time()) - 600}\nct:1.0\nhb:{time.time() - 600:.3f}\n")
    monkeypatch.setattr(update_lock, "update_marker_path", lambda: marker)
    monkeypatch.setattr(update_lock, "checkout_lock_path", lambda root=None: tmp_path / "checkout.lock")
    monkeypatch.setattr(update_pause_record, "install_root", lambda: tmp_path)

    assert update_pause_record._live_update_elsewhere() is False
    assert not marker.exists(), "pause must reclaim the dead lease instead of refusing on it"


# --------------------------------------------------------------------------- R2: the verdict


def _receipt_store(tmp_path, monkeypatch):
    directory = tmp_path / "logs" / "update_receipts"
    directory.mkdir(parents=True)
    monkeypatch.setattr(ur, "_receipt_dir", lambda: directory)
    monkeypatch.setattr(ur, "_receipt_dirs", lambda: [directory])
    return directory


def _write_open_run(directory: Path, update_id: str = "u1") -> Path:
    record = {
        "schema": 1, "update_id": update_id, "started_at": "2026-11-13T10:00:00Z", "pid": 999999,
        "outcome": "running", "finished_at": None,
        "stages": [{"name": "plan", "outcome": "success", "at": "2026-11-13T10:00:01Z"}],
        "stage_starts": [{"name": "apply", "at": "2026-11-13T10:00:02Z", "target_sha": "abc"}],
    }
    run_file = directory / f"update_20261113_100000_999999_{update_id}.json"
    payload = json.dumps(record, indent=2)
    run_file.write_text(payload, encoding="utf-8")
    (directory / "latest.json").write_text(payload, encoding="utf-8")
    return run_file


def test_an_out_of_process_killer_closes_the_open_run_with_its_last_stage(tmp_path, monkeypatch):
    """R2 item 1: the run that needs a verdict most is the one whose writer never got to write it."""
    directory = _receipt_store(tmp_path, monkeypatch)
    run_file = _write_open_run(directory)

    path = ur.close_open_run_from_outside(reason="gateway shutdown watchdog forced exit", exit_code=137)
    assert path == run_file
    record = json.loads(run_file.read_text(encoding="utf-8"))
    assert record["outcome"] == "interrupted"
    assert record["finished_at"], "a terminal outcome carries its timestamp"
    assert record["exit_code"] == 137
    assert "apply (unfinished)" in record["stop_reason"], (
        "the verdict has to say WHERE the run died, not only that it died")
    assert "shutdown_watchdog" in record["closed_by"]
    assert json.loads((directory / "latest.json").read_text(encoding="utf-8"))["outcome"] == "interrupted"


def test_a_second_writer_records_already_closed_by_and_never_a_second_outcome(tmp_path, monkeypatch):
    """R2 item 2: two writers, one rule."""
    directory = _receipt_store(tmp_path, monkeypatch)
    run_file = _write_open_run(directory)

    ur.close_open_run_from_outside(reason="first", exit_code=137)
    first = json.loads(run_file.read_text(encoding="utf-8"))
    ur.close_open_run_from_outside(reason="second", exit_code=1)

    second = json.loads(run_file.read_text(encoding="utf-8"))
    assert second["outcome"] == "interrupted", "the second writer must not overwrite the outcome"
    assert second["stop_reason"] == first["stop_reason"]
    assert second["closed_by"] == first["closed_by"]
    assert "second" not in (second.get("stop_reason") or "")
    assert second.get("already_closed_by"), "the second writer must record that it was too late"
    assert "shutdown_watchdog" in second["already_closed_by"]


def test_the_watchdog_closes_the_update_receipt_before_os_exit(monkeypatch):
    """R2 item 1 at the door that actually runs: ``_hard_exit`` follows ``_mark_exited_quietly``
    immediately, and that stamp now closes the UPDATE receipt first — ``os._exit`` runs no
    ``except``/``finally``, so a receipt left open here is a receipt nobody can close."""
    from gateway import shutdown_watchdog as wd
    import hermes_cli.update_receipt as receipt_module

    order: list[tuple] = []
    monkeypatch.setattr(receipt_module, "close_open_run_from_outside",
                        lambda **kw: order.append(("update_receipt", kw["reason"], kw["exit_code"])))
    monkeypatch.setattr("gateway.lifecycle_ledger.mark_exited",
                        lambda code, reason="": order.append(("lifecycle", code, reason)))
    monkeypatch.setattr("hermes_cli.observability.shared_metrics_process.stamp_exit", lambda *a, **k: None)
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **k: None)

    wd._mark_exited_quietly(1, "shutdown_watchdog")

    assert order and order[0][0] == "update_receipt", (
        "the terminal update outcome must be written before the lifecycle stamp, and long before "
        "the os._exit that follows it")
    assert "shutdown_watchdog" in order[0][1]
    assert order[0][2] == 1


def test_a_resumed_run_announces_resumed_at_stage(tmp_path, monkeypatch):
    """R2 item 3: a resume is a named event, not an inference from a missing refusal."""
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(ur, "resumable_stage",
                        lambda: {"name": "apply", "target_sha": "abc", "snapshot_id": "snap1",
                                 "update_id": "u0"})
    monkeypatch.setattr(ur, "record_event", lambda name, **facts: events.append((name, facts)))
    monkeypatch.setattr(update_cmd, "_RESUME_ANNOUNCED", False)

    first = update_cmd._resumable_stage()
    update_cmd._resumable_stage()

    assert first["name"] == "apply"
    assert [name for name, _ in events] == ["resumed_at_stage"], "announced exactly once per run"
    assert events[0][1]["stage"] == "apply"
    assert events[0][1]["resumed_update_id"] == "u0"


# --------------------------------------------------------------------------- R4: the autostash


def _git(repo, *args, check=True):
    return subprocess.run(GIT + list(args), cwd=repo, capture_output=True, text=True, check=check)


def _commit(repo, name, text):
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "commit", "-q", "-m", f"add {name}")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture()
def conflicting_custom_branch(tmp_path):
    """A checkout on ``carrier`` whose merge of ``origin/main`` conflicts, with local dirt stashed.

    This is the shape of the live failure: the checkout is a carrier branch, upstream touched the
    same file, the merge conflicts, and the update aborts with ``merge --abort``.
    """
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _commit(upstream, "shared.txt", "base\n")

    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "-q", str(upstream), str(checkout))
    _git(checkout, "checkout", "-q", "-b", "carrier")
    _commit(checkout, "carrier.txt", "carrier\n")
    pre_pull = _commit(checkout, "shared.txt", "carrier change\n")

    # Upstream moves the same line, so merging origin/main into `carrier` conflicts.
    _commit(upstream, "shared.txt", "upstream change\n")
    _git(checkout, "fetch", "-q", "origin")

    # The user's uncommitted work: the autostash the update takes (and must give back).
    (checkout / "carrier.txt").write_text("carrier\nlocal edit\n", encoding="utf-8")
    _git(checkout, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "stash", "push", "-q", "-m", "local dirt")
    stash_ref = _git(checkout, "rev-parse", "stash@{0}").stdout.strip()
    return checkout, pre_pull, stash_ref


def _run_pull(checkout, stash_ref):
    update_cmd._pull_updates(GIT, "main", stash_ref, prompt_for_restore=False, gw_input_fn=None,
                             discard_local_changes=False, keep_stash=False)


def _stash_count(checkout) -> int:
    return len([line for line in _git(checkout, "stash", "list").stdout.splitlines() if line.strip()])


def test_a_clean_abort_gives_the_autostash_back(conflicting_custom_branch, monkeypatch):
    """R4: ``merge --abort`` leaves a KNOWN tree, so refusing to restore manufactured `carry-lost`."""
    checkout, pre_pull, stash_ref = conflicting_custom_branch
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", checkout)

    with pytest.raises(SystemExit):
        _run_pull(checkout, stash_ref)

    assert _git(checkout, "rev-parse", "HEAD").stdout.strip() == pre_pull, "the abort restored HEAD"
    assert (checkout / "carrier.txt").read_text(encoding="utf-8") == "carrier\nlocal edit\n", (
        "the user's uncommitted work must be back in the worktree after a clean abort")
    assert _stash_count(checkout) == 0, "a restored stash is dropped: no orphan entry is left behind"


def test_a_torn_tree_still_keeps_the_stash_parked(conflicting_custom_branch, monkeypatch):
    """R4 keeps today's behaviour for a TORN move: the two cases must not be confused."""
    checkout, _pre_pull, stash_ref = conflicting_custom_branch
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", checkout)
    cleared: list[bool] = []
    monkeypatch.setattr(update_cmd, "_clear_pending_autostash", lambda: cleared.append(True))

    def torn(git_cmd, branch, merge_ref, target_sha, pre_sha):
        # git died mid-move: HEAD is still pre_pull but a tracked file is half-written.
        (checkout / "carrier.txt").write_text("half applied\n", encoding="utf-8")
        raise SystemExit(1)

    monkeypatch.setattr(update_cmd, "_move_checkout_to", torn)

    with pytest.raises(SystemExit):
        _run_pull(checkout, stash_ref)

    assert (checkout / "carrier.txt").read_text(encoding="utf-8") == "half applied\n"
    assert _stash_count(checkout) == 1, "a torn tree must keep the stash: the state is unknown"
    assert cleared, "a torn tree still clears the pointer, as before"
