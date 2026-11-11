"""The retryable-refusal ack: a reason in RETRYABLE_REASONS may never say "Do not resend".

Measured defect the case pins (card t-76be5e4334): ``_start_delivery``'s bare ``except Exception``
answered every unexplained admission failure with "…could not be confirmed: <exc>. Do not resend."
For ``queue_full`` — a member of ``bot_delivery_queue.RETRYABLE_REASONS`` — that sentence tells the
sender to abandon a send the queue considers retryable. Twenty peer briefs were lost that way with
``attempts=1`` and nothing durable recording that anyone had tried.
"""

import json
import os
from pathlib import Path

import pytest

from tools import bot_live_delivery as mailbox
from tools import bot_delivery_queue as delivery_queue


def _owner(home):
    return dict(profile_home=str(Path(home).resolve()), session_id="local-bot",
                lease_id="authority-1", live_session_id="local-bot")


def _spy_dm_files(bot_mode_dm, monkeypatch):
    dm_files = []
    real_write = bot_mode_dm._write_dm_file

    def spy_write(content):
        path = real_write(content)
        dm_files.append(path)
        return path

    monkeypatch.setattr(bot_mode_dm, "_write_dm_file", spy_write)
    return dm_files


@pytest.fixture()
def sender_log(tmp_path, monkeypatch):
    """Point the durable sender-side log at tmp and return its path."""
    log_dir = tmp_path / "sender-state"
    monkeypatch.setenv("HERMES_DELIVERY_LOG_DIR", str(log_dir))
    return log_dir / "bot_delivery_attempts.jsonl"


def _refusing(tmp_path, monkeypatch, bot_mode_dm, exc):
    monkeypatch.setattr(mailbox, "find_canonical_live_owner", lambda home: _owner(tmp_path))

    def refused(home, params):
        raise exc

    monkeypatch.setattr(mailbox, "authority_delivery", refused)
    return _spy_dm_files(bot_mode_dm, monkeypatch)


def test_queue_full_is_NOT_told_do_not_resend_and_is_recorded_durably(tmp_path, monkeypatch, sender_log):
    from tools import bot_mode_dm

    assert "queue_full" in delivery_queue.RETRYABLE_REASONS
    dm_files = _refusing(tmp_path, monkeypatch, bot_mode_dm, RuntimeError("queue_full"))
    payload = json.loads(bot_mode_dm._start_delivery(
        ["hermes", "-p", "researcher"], "hello", "@researcher",
        stdin_file=False, task_id=None, agent=None, profile_home=tmp_path))
    assert payload["evidence_file"] == dm_files[0]   # the temp file is the evidence, not the record

    assert payload["reason"] == "queue_full"
    assert payload["retryable"] is True
    assert payload["attempt"] == 1
    assert payload["retry_at"]
    assert "Do not resend" not in payload["error"], payload["error"]
    assert "SAME idempotency key" in payload["error"], payload["error"]
    assert "attempt 1" in payload["error"], payload["error"]

    rows = [json.loads(line) for line in sender_log.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["reason"] == "queue_full" and rows[0]["retryable"] is True
    assert rows[0]["delivery_id"] == payload["delivery_id"]
    # the durable record is NOT the prunable evidence file
    assert Path(sender_log) != Path(payload["evidence_file"])


def test_the_second_refusal_for_the_same_delivery_is_attempt_2(tmp_path, monkeypatch, sender_log):
    """The attempt is COUNTED off the durable log, so a repeat is never reported as the first try."""
    from tools import bot_mode_dm

    _refusing(tmp_path, monkeypatch, bot_mode_dm, RuntimeError("queue_full"))
    first = json.loads(bot_mode_dm._start_delivery(
        ["hermes", "-p", "researcher"], "hello", "@researcher",
        stdin_file=False, task_id=None, agent=None, profile_home=tmp_path))
    # same DM file -> same delivery id -> the log must report the second attempt
    dm_file = first["evidence_file"]
    second = json.loads(bot_mode_dm._refused_admission_payload(
        dm_file, RuntimeError("queue_full"), author=None))

    assert second["delivery_id"] == first["delivery_id"]
    assert second["attempt"] == 2, second
    rows = [json.loads(line) for line in sender_log.read_text(encoding="utf-8").splitlines()]
    assert [row["attempt"] for row in rows] == [1, 2]


def test_a_FINAL_reason_keeps_do_not_resend(tmp_path, monkeypatch, sender_log):
    """The pairing is the claim: only a NON-retryable reason may tell the sender to stop."""
    from tools import bot_mode_dm

    _refusing(tmp_path, monkeypatch, bot_mode_dm, RuntimeError("profile-authority-not-ready"))
    payload = json.loads(bot_mode_dm._start_delivery(
        ["hermes", "-p", "researcher"], "hello", "@researcher",
        stdin_file=False, task_id=None, agent=None, profile_home=tmp_path))

    assert payload["retryable"] is False
    assert payload.get("reason") is None
    assert "Do not resend" in payload["error"], payload["error"]
    rows = [json.loads(line) for line in sender_log.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["retryable"] is False and rows[0]["reason"] is None


def test_the_classification_is_READ_from_the_queue_not_restated_here(monkeypatch):
    """A new retryable reason must land in one place: the queue's set, not a copy in this module."""
    from tools import bot_mode_dm

    monkeypatch.setattr(delivery_queue, "RETRYABLE_REASONS", frozenset({"brand_new_reason"}))
    assert bot_mode_dm._retryable_reason(RuntimeError("refused: brand_new_reason")) == "brand_new_reason"
    assert bot_mode_dm._retryable_reason(RuntimeError("queue_full")) == ""


def test_the_log_path_is_durable_and_env_overridable(tmp_path, monkeypatch):
    from tools import bot_mode_dm

    monkeypatch.delenv("HERMES_DELIVERY_LOG_DIR", raising=False)
    assert bot_mode_dm._delivery_attempt_log() == Path.home() / ".hermes" / "state" / "bot_delivery_attempts.jsonl"
    monkeypatch.setenv("HERMES_DELIVERY_LOG_DIR", str(tmp_path))
    assert bot_mode_dm._delivery_attempt_log() == tmp_path / "bot_delivery_attempts.jsonl"
