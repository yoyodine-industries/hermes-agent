"""A full admission FIFO is a NAMED refusal with a remedy, never the ambiguous "do not resend".

The queue cap is read before the queued insert (``persistence/sqlite/authority/_admission_store``),
so a ``queue_full`` refusal admitted nothing: its outcome is known, and the sender may retry. Both
the sender (``tools/bot_mode_dm``) and the peer-DM door (``gateway/platforms/api_server_bot_chat``)
used to answer it with a bare reason or with the generic "outcome unknown ... Do not resend".
"""

import json
from pathlib import Path

import pytest

from hermes_cli.gateway_client import GatewayRPCError
from tools import bot_mode_dm as dm


def _pinned_intent(tmp_path: Path) -> str:
    """A DM payload with a pinned live intent, so the delivery goes straight to the owner door."""
    dm_file = dm._write_dm_file("hello")
    intent = Path(dm._live_intent_file(dm_file))
    intent.write_text(json.dumps(dict(owner={"profile_home": str(tmp_path), "canonical": True},
                                      message="hello", delivery_id=dm._dm_delivery_id(dm_file))),
                      encoding="utf-8")
    return dm_file


def test_queue_full_refusal_is_a_named_no_send_not_an_ambiguous_outcome(tmp_path, monkeypatch):
    from tools import bot_live_delivery as live

    dm_file = _pinned_intent(tmp_path)
    monkeypatch.setattr(live, "read_delivery_result", lambda home, delivery_id: None)

    def full(home, owner, message, **kwargs):
        raise GatewayRPCError("queue_full", data={"retry_after": 12})

    monkeypatch.setattr(live, "deliver_to_live_owner", full)

    with pytest.raises(dm.LiveTargetBusyError) as caught:
        dm._admit_live_dm(None, dm_file)

    assert caught.value.reason == "queue_full"
    assert caught.value.retry_after == 12
    payload = json.loads(dm._unavailable_payload(dm_file, caught.value))
    assert payload["reason"] == "queue_full"
    assert payload["retry_after"] == 12
    assert "NOT sent" in payload["error"]
    assert "safe to resend" in payload["error"]
    assert "Do not resend" not in payload["error"]


def test_outcome_unknown_reports_a_queue_full_cause_as_the_known_refusal(tmp_path):
    dm_file = dm._write_dm_file("hello")

    payload = json.loads(dm._live_outcome_unknown(dm_file, GatewayRPCError("queue_full")))

    assert payload["reason"] == "queue_full"
    assert payload["retry_after"] == dm.QUEUE_FULL_RETRY_SECONDS
    assert "safe to resend" in payload["error"]
    assert "Do not resend" not in payload["error"]
    assert "outcome unknown" not in payload["error"]


def test_outcome_unknown_keeps_genuine_ambiguity_ambiguous(tmp_path):
    dm_file = dm._write_dm_file("hello")

    payload = json.loads(dm._live_outcome_unknown(dm_file, GatewayRPCError("turn_in_progress")))

    assert payload["status"] == "ambiguous"
    assert "Do not resend" in payload["error"]


def test_peer_dm_refusal_names_the_queue_check_and_its_measurement():
    from gateway.platforms.api_server_bot_chat import _refusal
    from persistence.core.errors import AuthorityRefusal

    reason, error = _refusal(None, {"id": "local-1", "chat_id": "local-1"},
                             AuthorityRefusal("queue_full", field="admission_queue",
                                              detail="32 queued inputs of a cap of 32"))

    assert reason == "queue_full"
    assert "admission_queue" in error
    assert "32 queued inputs of a cap of 32" in error
    assert "nothing was admitted" in error
    assert "resend" not in error.lower() or "retry" in error.lower()
