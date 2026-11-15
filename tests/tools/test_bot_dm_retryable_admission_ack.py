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
from tools import bot_mode_dm


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


# ---------------------------------------------------------------------------------------
# the reason is a FIELD, not a phrase - and the two members of the set are not alike
# ---------------------------------------------------------------------------------------

class _CarriesReason(Exception):
    """The shape every refusal exception in this estate already has (bot_relay.py:79-81)."""

    def __init__(self, reason, text):
        super().__init__(text)
        self.reason = reason


def test_runtime_offline_is_RETRYABLE_even_though_its_text_never_says_its_code():
    """THE INVERSE OF THE DEFECT: `runtime_offline` never appears in its own error text.

    bot_relay raises `EnvelopeRefusedError("runtime_offline", "<lane> is offline right now - the
    message was NOT queued ...")`. A substring scan finds nothing, falls to the final branch, and
    answers a DEFINITE NO-SEND with "Do not resend." - resending was safe. The code is carried as
    a FIELD, so the field is read.
    """
    exc = _CarriesReason("runtime_offline",
                         "peer is offline right now - the message was NOT queued. Try again once "
                         "that machine reconnects to the Desktop.")
    assert "runtime_offline" not in str(exc), "the text must not contain the code, or the case proves nothing"
    assert bot_mode_dm._retryable_reason(exc) == "runtime_offline"


def test_a_target_that_is_UNAVAILABLE_today_is_classified_by_the_vocabulary_s_word():
    """FINDING 2: this module emits `runtime_unavailable`, which is in NEITHER set.

    A target that is down today would emit a code no vocabulary knows, so reading the field alone
    would still classify it final. The emitted code maps to the vocabulary's word for it.
    """
    exc = _CarriesReason("runtime_unavailable", "the live target is unavailable")
    assert bot_mode_dm._retryable_reason(exc) == "runtime_offline"


def test_the_attribute_wins_over_the_text_when_they_disagree():
    """A rendering is not the datum: if the field says final and the text names a retryable code,
    the FIELD decides - otherwise a message that quotes its cause reclassifies the exception."""
    exc = _CarriesReason("profile-authority-not-ready", "upstream said queue_full")
    assert bot_mode_dm._retryable_reason(exc) == ""


def test_every_reason_this_module_EMITS_is_in_the_closed_vocabulary_or_mapped_to_it():
    """The guard against the next class inventing a code that no set knows.

    tools/bot_failure_reasons.py declares itself the closed vocabulary. An emitted `reason` code
    that appears in neither that vocabulary nor the alias map is a defect no other test would see,
    because the refusal still renders - it just classifies as final forever.
    """
    import ast

    source = Path(bot_mode_dm.__file__).read_text(encoding="utf-8")
    emitted = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ClassDef):
            continue
        for sub in node.body:
            if not isinstance(sub, ast.Assign):
                continue
            if not any(getattr(t, "id", None) == "reason" for t in sub.targets):
                continue
            if isinstance(sub.value, ast.Constant) and isinstance(sub.value.value, str):
                emitted.add(sub.value.value)

    from tools import bot_failure_reasons as vocabulary
    known = {value for name, value in vars(vocabulary).items()
             if name.isupper() and isinstance(value, str)}
    mapped = set(getattr(bot_mode_dm, "_REASON_ALIASES", {}))
    assert emitted, "no emitted reason found - the scan is looking at the wrong shape"
    unknown = sorted(emitted - known - mapped)
    assert not unknown, (
        f"bot_mode_dm emits reason code(s) {unknown} that the closed vocabulary "
        "(tools/bot_failure_reasons) and the alias map both lack - a target that reports one is "
        "classified final no matter what it means")


def test_the_attempt_counter_reads_a_BOUNDED_tail_of_the_log(tmp_path, monkeypatch, sender_log):
    """The ack path re-reads this log on every refusal, so its cost must not scale with the file.

    The bound is stated rather than implied: only the last _ATTEMPT_SCAN_WINDOW rows are counted,
    so this pins the reader to the tail (a full-file read would also pass this test, which is why
    the window is asserted as a DECLARED number below) and pins that a delivery whose retry sits
    inside the window is still counted exactly.
    """
    assert 0 < bot_mode_dm._ATTEMPT_SCAN_WINDOW <= 65536, "retention must be a stated, sane bound"

    log = sender_log
    log.parent.mkdir(parents=True, exist_ok=True)
    unrelated = [json.dumps({"delivery_id": f"d{i:05d}", "attempt": 1}) for i in range(5000)]
    log.write_text("\n".join(unrelated) + "\n", encoding="utf-8")

    dm_files = _spy_dm_files(bot_mode_dm, monkeypatch)
    dm_file = str(tmp_path / "dm.json")
    Path(dm_file).write_text("# DM\n", encoding="utf-8")
    delivery_id = bot_mode_dm._dm_delivery_id(dm_file)

    first = bot_mode_dm._record_admission_attempt(
        dm_file, RuntimeError("queue_full"), reason="queue_full", author=None)
    second = bot_mode_dm._record_admission_attempt(
        dm_file, RuntimeError("queue_full"), reason="queue_full", author=None)
    assert (first, second) == (1, 2), "a retry inside the window must still be counted"

    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert sum(1 for r in rows if r.get("delivery_id") == delivery_id) == 2

    tail = bot_mode_dm._tail_rows(log, 3)
    assert len(tail) == 3, "the reader must stop at the window, not parse the file"
    assert [r.get("delivery_id") for r in tail] == [r.get("delivery_id") for r in rows[-3:]]
    windowed = bot_mode_dm._tail_rows(log, bot_mode_dm._ATTEMPT_SCAN_WINDOW)
    assert len(windowed) == bot_mode_dm._ATTEMPT_SCAN_WINDOW, "the window is the retention it declares"
    assert windowed[0]["delivery_id"] == rows[-bot_mode_dm._ATTEMPT_SCAN_WINDOW]["delivery_id"]
    assert bot_mode_dm._tail_rows(log, 100000) and len(bot_mode_dm._tail_rows(log, 100000)) == len(rows), \
        "a window larger than the file returns the whole file, not a truncation artefact"
    assert set(dm_files) == set(), "no dm file is rewritten by the counter"
