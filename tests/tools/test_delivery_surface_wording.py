"""The delivery surface names the component that failed, and a no-send hands out no receipt.

Two wording defects were measured against the 04:52-05:17Z authority-epoch gap (card
t_ffa66b16). Neither changes send semantics; both made a reader diagnose the wrong component
or the wrong event:

* LEG 1 - a discovery TIMEOUT was reported as ``profile authority is not ready``. During a
  >10 s event-loop stall the gateway cannot answer its control socket while the target profile
  is perfectly ready, so the refusal accused the innocent party.
* LEG 2 - a definite no-send carried ``delivery_id`` = sha256 of the resolved DM FILE PATH:
  a value that exists in no runtime ledger, repeats for every retry of the same body, and
  therefore reads as an admission receipt while proving the opposite.

Every assertion here is about WORDS IN THE SURFACE. Nothing in these cases asserts a change
to send semantics: a refusal stays fail-closed and a no-send stays a definite no-send.
"""
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import bot_live_delivery as mailbox
from tools import bot_mode_dm

#: The shape a DELIVERED receipt's id has (``bot_live_delivery._delivery_id``). A refuse-time
#: payload must not carry a value in this shape, because a sender cannot tell it from a receipt.
_RECEIPT_SHAPED = re.compile(r"^[0-9a-f]{32,64}$")


def _discovery(state, *, endpoint=None, reason_code=None, detail=None):
    return SimpleNamespace(state=state, endpoint=endpoint, reason_code=reason_code, detail=detail)


@pytest.fixture
def scripted_discovery(monkeypatch):
    """Answer every discovery call with one scripted verdict, recording what was asked."""
    from hermes_cli import gateway_runtime

    def script(answer):
        seen = []

        def discover(home, timeout):
            seen.append((Path(home), timeout))
            return answer

        monkeypatch.setattr(gateway_runtime, "discover_gateway_endpoint", discover)
        return seen

    return script


# ---------------------------------------------------------------------------------------
# LEG 1 - the refusal names DISCOVERY, and a stalled caller is not a dead target
# ---------------------------------------------------------------------------------------


def test_a_discovery_TIMEOUT_names_discovery_and_never_the_target(tmp_path, scripted_discovery):
    """The measured case: the endpoint cannot answer while the target profile is ready.

    The refusal must point at the discovery path (a timeout, possibly the caller's own stalled
    event loop) and must make NO claim about the target's authority - that is the accusation
    that sent readers to check, restart and re-verify the party that could not be the cause.
    """
    seen = scripted_discovery(_discovery("inaccessible", reason_code="control_timeout"))

    with pytest.raises(ValueError) as raised:
        mailbox.find_canonical_live_owner(tmp_path)

    message = str(raised.value)
    assert "discovery timeout after 5s" in message, message
    assert "did not answer its control socket" in message, message
    assert "stalled event loop" in message, message
    assert "authority" not in message.lower(), message
    assert seen == [(tmp_path.resolve(), mailbox.DISCOVERY_TIMEOUT_SECONDS)]


def test_REFUSED_and_UNKNOWN_and_TIMEOUT_are_three_different_sentences(tmp_path, scripted_discovery):
    """A reader must be able to tell "nothing is listening" from "it did not answer in time".

    Both used to be the same sentence, so neither could be acted on: one is a dead target, the
    other is a blocked one (and may be the caller itself).
    """
    scripted_discovery(_discovery("absent"))
    with pytest.raises(ValueError) as refused:
        mailbox.find_canonical_live_owner(tmp_path)
    refused_message = str(refused.value)

    scripted_discovery(_discovery("inaccessible", reason_code="control_timeout"))
    with pytest.raises(ValueError) as timed_out:
        mailbox.find_canonical_live_owner(tmp_path)
    timeout_message = str(timed_out.value)

    assert "connection refused" in refused_message, refused_message
    assert "endpoint unknown" in refused_message, refused_message
    assert "discovery timeout" in timeout_message, timeout_message
    assert refused_message != timeout_message
    for message in (refused_message, timeout_message):
        assert "authority" not in message.lower(), message


def test_the_DIAL_keeps_the_discovery_outcome_in_its_pre_dial_refusal(tmp_path, scripted_discovery):
    """A caller that reaches the RPC (not the owner lookup) must still hear what discovery saw.

    ``_authority_rpc`` wraps pre-dial failures in ``PreDialError``; wrapping must not flatten the
    outcome back into one sentence, and the type stays a ``ValueError`` for existing callers.
    """
    scripted_discovery(_discovery("inaccessible", reason_code="control_timeout"))

    with pytest.raises(mailbox.PreDialError) as raised:
        mailbox.authority_delivery(tmp_path, {"id": "0" * 64, "message": "probe"})

    message = str(raised.value)
    assert "discovery timeout after 5s" in message, message
    assert "authority" not in message.lower(), message
    assert isinstance(raised.value, ValueError)


# ---------------------------------------------------------------------------------------
# LEG 2 - a definite no-send carries no field that can be read as a receipt
# ---------------------------------------------------------------------------------------


def _no_send(dm_file: Path) -> dict:
    exc = bot_mode_dm.LiveTargetUnavailableError(
        "gateway endpoint discovery failed: discovery timeout after 5s")
    return json.loads(bot_mode_dm._unavailable_payload(str(dm_file), exc))


def test_a_definite_no_send_has_no_delivery_id_and_no_receipt_shaped_value(tmp_path):
    """``delivery_id`` was sha256 of the temp DM file path - a correlation key, not a receipt."""
    dm_file = tmp_path / "dm.txt"
    dm_file.write_text("hello teammate", encoding="utf-8")

    payload = _no_send(dm_file)

    assert payload["send_status"] == "not_sent", payload
    assert "delivery_id" not in payload, payload
    assert not [field for field, value in payload.items()
                if isinstance(value, str) and _RECEIPT_SHAPED.match(value)], payload
    # The wording that classifies the outcome downstream (bot_mode_dm_outcome) is unchanged.
    assert "NOT sent" in payload["error"]
    assert "safe to resend" in payload["error"]


def test_two_identical_retries_do_not_produce_the_SAME_claimed_receipt(tmp_path):
    """The inverse of a receipt, pinned: an id that repeats per retry proves nothing.

    The removed field was derived from the DM file path, so N attempts of one body produced
    N copies of ONE value (indistinguishable from a single attempt) and two values for two
    files that carried one intent. A no-send has nothing to admit, so it now carries no key
    at all: two identical retries produce no claimed-receipt value on either.
    """
    dm_file = tmp_path / "dm.txt"
    dm_file.write_text("hello teammate", encoding="utf-8")

    first, second = _no_send(dm_file), _no_send(dm_file)

    for payload in (first, second):
        assert "delivery_id" not in payload, payload
        assert not [field for field, value in payload.items()
                    if isinstance(value, str) and _RECEIPT_SHAPED.match(value)], payload
    assert first == second  # nothing volatile is manufactured to fake per-attempt identity
