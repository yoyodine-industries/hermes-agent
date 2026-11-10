"""A run killed INSIDE a stage leaves structured debt, and the next run finishes that stage.

`record_stage` alone only names the stage that last FINISHED: a run killed halfway through
`apply` looks exactly like one killed between stages, so the next update cannot tell whether the
work must be redone or merely completed. The start mark (`begin_stage`) makes that deterministic,
`resumable_stage` hands it back, and `_resume_interrupted_apply` completes the move instead of
firing a second one.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from hermes_cli import update_cmd, update_receipt


def _latest(home):
    return json.loads((home / "logs/update_receipts/latest.json").read_text())


def _kill_record(home) -> None:
    """Make the open record name a process that is gone, as a real kill would."""
    dead = subprocess.Popen(["true"])
    dead.wait()
    for path in (home / "logs/update_receipts").glob("*.json"):
        data = json.loads(path.read_text())
        data.update(pid=dead.pid, writer_pid=dead.pid, pid_create_time=None, writer_create_time=None)
        path.write_text(json.dumps(data))
    update_receipt._current.set(None)


def test_an_unfinished_stage_is_structured_debt_the_next_run_can_read(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    update_receipt.begin_update_receipt()
    update_receipt.begin_stage("snapshot", mode="quick")
    update_receipt.record_stage("snapshot", "success", snapshot_id="snap-42")
    update_receipt.begin_stage("apply", mode="git", target_sha="2528e8407ad30c77c86d587b2a00639fb828b5ae")
    _kill_record(tmp_path)

    # The next run opens its receipt, which reclaims the dead one.
    update_receipt.begin_update_receipt()
    out = capsys.readouterr().out
    assert "interrupted" in out and "apply (unfinished)" in out

    stage = update_receipt.resumable_stage()
    assert stage is not None and stage["name"] == "apply"
    assert stage["target_sha"] == "2528e8407ad30c77c86d587b2a00639fb828b5ae"
    # The snapshot the dead run already captured is carried too: it is work already paid for.
    assert stage["snapshot_id"] == "snap-42"
    update_receipt.finalize_update_receipt("success")


def test_a_stage_that_ended_is_not_debt(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    update_receipt.begin_update_receipt()
    update_receipt.begin_stage("snapshot")
    update_receipt.record_stage("snapshot", "success", snapshot_id="snap-7")
    _kill_record(tmp_path)

    update_receipt.begin_update_receipt()
    assert update_receipt.resumable_stage() is None
    assert "stage_starts" not in _latest(tmp_path)
    update_receipt.finalize_update_receipt("success")


def test_an_interrupted_apply_is_resumed_rather_than_restarted(monkeypatch):
    target = "2528e8407ad30c77c86d587b2a00639fb828b5ae"
    monkeypatch.setattr(update_cmd, "_resumable_stage",
                        lambda: {"name": "apply", "target_sha": target, "mode": "git"})
    monkeypatch.setattr(update_cmd, "_current_checkout_sha", lambda: target)
    request = {"expected_sha": "stale-fetch-sha"}

    resumed = update_cmd._resume_interrupted_apply(request)

    assert resumed == {"target_sha": target, "head": target}
    # The completion is pinned to the code already on disk, so a later fetch cannot start a
    # SECOND move under the same receipt.
    assert request["expected_sha"] == target


def test_an_apply_whose_tree_never_moved_is_applied_again(monkeypatch):
    target = "2528e8407ad30c77c86d587b2a00639fb828b5ae"
    monkeypatch.setattr(update_cmd, "_resumable_stage",
                        lambda: {"name": "apply", "target_sha": target, "mode": "git"})
    monkeypatch.setattr(update_cmd, "_current_checkout_sha", lambda: "1111111111111111111111111111111111111111")

    assert update_cmd._resume_interrupted_apply({"expected_sha": None}) is None


@pytest.mark.parametrize("stage", [{"name": "deps", "snapshot_id": "snap-9"}, {}, {"snapshot_id": "snap-9"}])
def test_only_a_snapshot_or_apply_debt_reuses_a_snapshot(monkeypatch, stage):
    monkeypatch.setattr(update_cmd, "_resumable_stage", lambda: dict(stage))
    expected = (stage.get("snapshot_id") if stage.get("name") in (None, "snapshot", "apply") else None)
    assert update_cmd._resume_snapshot_id() == expected
