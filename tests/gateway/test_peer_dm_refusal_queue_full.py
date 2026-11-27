"""A peer-DM ``queue_full`` refusal is a NAMED no-send with its measurement and its remedy.

The admission cap is read BEFORE the queued insert, so a ``queue_full`` refusal admitted
nothing: the outcome is known and the sender may retry once the FIFO drains. The sender
(``tools/bot_mode_dm``) already says so; the peer-DM door printed the bare reason, which
dropped both the check that fired and that remedy (t-8ff0ed888d b).
"""

from gateway.platforms.api_server_bot_chat import _refusal
from hermes_state_runtime import RuntimeStoreError


def test_queue_full_refusal_names_the_check_its_measurement_and_the_retry():
    reason, error = _refusal(None, {"id": "local-1", "chat_id": "local-1"},
                             RuntimeStoreError("queue_full", field="interactive_fifo",
                                               detail="32/32 queued; nothing was reserved",
                                               retry_after=30.0))

    assert reason == "queue_full"
    assert "interactive_fifo" in error
    assert "32/32 queued" in error
    assert "nothing was admitted" in error
    assert "retry" in error
    # The label that belongs only to an admission that may have happened.
    assert "Do not resend" not in error


def test_any_other_refusal_keeps_its_bare_reason():
    reason, error = _refusal(None, {"id": "local-1", "chat_id": "local-1"},
                             RuntimeStoreError("admission_conflict"))

    assert reason == "admission_conflict"
    assert error == "Bot Chat admission refused: admission_conflict"
