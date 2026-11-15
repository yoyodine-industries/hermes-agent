"""P11 (§4.8): ENUM B is a BOUNDARY translation, never a storage rename.

The mailbox keeps queued -> claimed -> settled on disk; anything surfaced to a
sender says queued -> running -> delivered.
"""
from __future__ import annotations

import json

import pytest

from tools import bot_live_delivery as mailbox
from tools import bot_mode_dm


@pytest.fixture()
def owner(tmp_path):
    return dict(profile_home=str(tmp_path.resolve()), session_id="chat",
                lease_id="lease", live_session_id="live")


def test_public_status_maps_only_the_storage_words():
    assert mailbox.public_delivery_status("claimed") == "running"
    assert mailbox.public_delivery_status("settled") == "delivered"
    assert mailbox.public_delivery_status("queued") == "queued"
    assert mailbox.public_delivery_status("ambiguous") == "ambiguous"
    assert mailbox.public_delivery_status("") == ""


def test_public_read_translates_without_touching_storage(tmp_path, owner):
    delivery_id = "b" * 32
    mailbox.deliver_to_live_owner(tmp_path, owner, "hello", delivery_id=delivery_id)
    assert mailbox.read_public_delivery_result(tmp_path, delivery_id)["status"] == "queued"

    assert mailbox.claim_pending_delivery(tmp_path, owner) is not None
    assert mailbox.read_delivery_result(tmp_path, delivery_id)["status"] == "claimed"
    public = mailbox.read_public_delivery_result(tmp_path, delivery_id)
    assert public["status"] == "running" and public["message"] == "hello"

    mailbox.complete_delivery(tmp_path, delivery_id, status="settled", reply="answer")
    assert mailbox.read_delivery_result(tmp_path, delivery_id)["status"] == "settled"
    public = mailbox.read_public_delivery_result(tmp_path, delivery_id)
    assert public["status"] == "delivered" and public["reply"] == "answer"
    # storage keeps its own word, and the caller's copy is a copy
    assert mailbox.read_delivery_result(tmp_path, delivery_id)["status"] == "settled"
    assert mailbox.read_public_delivery_result(tmp_path, "c" * 32) is None


def test_wait_live_dm_prints_enum_b_while_disk_keeps_the_storage_word(
    tmp_path, owner, monkeypatch, capsys
):
    delivery_id = "d" * 32
    monkeypatch.setattr(bot_mode_dm, "_LIVE_WAIT_SECONDS", 0)
    mailbox.deliver_to_live_owner(tmp_path, owner, "hello", delivery_id=delivery_id)
    mailbox.claim_pending_delivery(tmp_path, owner)

    assert bot_mode_dm._wait_live_dm(str(tmp_path), delivery_id) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "running", "a sender never sees the word 'claimed'"
    assert payload["detail"].startswith("Delivery remains pending")
    assert mailbox.read_delivery_result(tmp_path, delivery_id)["status"] == "claimed"

    mailbox.complete_delivery(tmp_path, delivery_id, status="settled", reply="answer")
    assert bot_mode_dm._wait_live_dm(str(tmp_path), delivery_id) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "delivered" and payload["reply"] == "answer"
    assert mailbox.read_delivery_result(tmp_path, delivery_id)["status"] == "settled"


def test_failed_delivery_keeps_its_own_word(tmp_path, owner, monkeypatch, capsys):
    delivery_id = "e" * 32
    monkeypatch.setattr(bot_mode_dm, "_LIVE_WAIT_SECONDS", 0)
    mailbox.deliver_to_live_owner(tmp_path, owner, "hello", delivery_id=delivery_id)
    mailbox.claim_pending_delivery(tmp_path, owner)
    mailbox.complete_delivery(tmp_path, delivery_id, status="failed", error="boom")
    assert bot_mode_dm._wait_live_dm(str(tmp_path), delivery_id) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed" and payload["error"] == "boom"


def test_ambiguous_without_a_record_is_reported_honestly(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bot_mode_dm, "_LIVE_WAIT_SECONDS", 0)
    assert bot_mode_dm._wait_live_dm(str(tmp_path), "f" * 32) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ambiguous"
    assert "Do not resend" in payload["detail"], "an unknown outcome is never a licence to resend"


# ------------------------------------------------------- the pre-dial door (t-8e280ed8c3)
# Deliberately in THIS file, beside the boundary rules: both answer "what is the sender told",
# and the door's receipt is where the boundary layer is read from a live process.

from types import SimpleNamespace


def _door(state, endpoint):
    return SimpleNamespace(state=state, endpoint=endpoint)


def test_door_names_the_state_fact_and_keeps_the_endpoint_fact():
    err = mailbox._not_ready_error(_door('stopped', object()))
    assert err.payload['which'] == 'state'
    assert err.payload['endpoint_present'] is True
    assert 'endpoint_present=True' in str(err)


def test_door_names_the_endpoint_fact_when_state_is_ready():
    err = mailbox._not_ready_error(_door('ready', None))
    assert err.payload['which'] == 'endpoint'
    assert err.payload['endpoint_present'] is False


def test_door_names_both_facts_when_both_hold():
    err = mailbox._not_ready_error(_door('stopped', None))
    assert err.payload['which'] == 'state+endpoint'
    assert err.payload['state'] == 'stopped'


def test_absent_payload_is_not_an_empty_payload():
    assert mailbox.PreDialError('x').payload is None
    assert mailbox.PreDialError('x', payload={}).payload == {}


def test_door_refusal_is_still_a_value_error():
    assert isinstance(mailbox._not_ready_error(_door('ready', None)), ValueError)


def test_pre_dial_wrap_carries_the_wrapped_payload(monkeypatch):
    import hermes_cli.gateway_client as gateway_client
    import hermes_cli.gateway_runtime as gateway_runtime

    class Bone(Exception):
        payload = {'which': 'endpoint', 'endpoint_present': False}

    monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint',
                        lambda home, timeout=None: _door('ready', object()))
    monkeypatch.setattr(gateway_client, '_session_ticket',
                        lambda home, endpoint: (_ for _ in ()).throw(Bone('ticket died')))
    with pytest.raises(mailbox.PreDialError) as caught:
        mailbox._authority_rpc('/tmp/does-not-matter', 'bot_relay.deliver', {})
    assert caught.value.payload == {'which': 'endpoint', 'endpoint_present': False}
    assert 'ticket died' in str(caught.value)


def test_empty_str_exception_names_its_class(monkeypatch):
    import hermes_cli.gateway_client as gateway_client
    import hermes_cli.gateway_runtime as gateway_runtime

    monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint',
                        lambda home, timeout=None: _door('ready', object()))
    monkeypatch.setattr(gateway_client, '_session_ticket',
                        lambda home, endpoint: (_ for _ in ()).throw(TimeoutError()))
    with pytest.raises(mailbox.PreDialError) as caught:
        mailbox._authority_rpc('/tmp/does-not-matter', 'bot_relay.deliver', {})
    assert str(caught.value).startswith('TimeoutError:')


def test_self_source_hash_matches_the_file_on_disk():
    import hashlib
    import pathlib
    source = pathlib.Path(mailbox.__file__).read_bytes()
    assert mailbox.SOURCE_SHA256 == hashlib.sha256(source).hexdigest()


def test_ambiguous_receipts_carry_the_loaded_source_hash():
    out = json.loads(bot_mode_dm._live_outcome_unknown('/tmp/evidence', TimeoutError()))
    assert out['script_sha256'] == mailbox.SOURCE_SHA256
    assert 'TimeoutError' in out['error']
    assert 'unknown: .' not in out['error']
