"""A refusing authority says WHICH state it saw, and a sender acts on that value.

The defect these pin (#139303 sibling): ``state != 'ready'`` collapsed every non-ready discovery
into one string, so a gateway that was BOOTING - the ordinary outcome of a delivery that raced a
restart - was indistinguishable from one that is GONE. The two call for opposite acts (wait and
resend, or stop and tell the user the target is absent), and a sender that string-matches the
sentence cannot tell them apart at all.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import bot_live_delivery as mailbox


def _discovery(state, *, endpoint=None, reason_code=None, detail=None):
    return SimpleNamespace(state=state, endpoint=endpoint, reason_code=reason_code, detail=detail)


def _endpoint(instance_id="lease-1"):
    return SimpleNamespace(instance_id=instance_id)


@pytest.fixture
def probes(monkeypatch):
    """Answer discovery from a scripted list, recording every probe the module made."""
    from hermes_cli import gateway_runtime

    seen = []

    def script(*answers):
        def discover(home, timeout):
            seen.append((Path(home), timeout))
            return answers[min(len(seen), len(answers)) - 1]
        monkeypatch.setattr(gateway_runtime, "discover_gateway_endpoint", discover)
        return seen
    return script


def test_a_BOOTING_owner_is_re_probed_and_the_endpoint_it_publishes_is_returned(tmp_path, probes):
    """`starting` is not-yet-an-answer: the re-probe is what turns a restart race into a delivery."""
    seen = probes(_discovery("starting", reason_code="gateway_booting"),
                  _discovery("ready", endpoint=_endpoint("lease-9")))

    endpoint = mailbox.discover_serving_endpoint(tmp_path, attempts=4, interval=0.0)

    assert endpoint.instance_id == "lease-9"
    assert len(seen) == 2, "the booting probe must be followed by another, not answered by it"


def test_a_BOOTING_owner_that_never_serves_is_a_RETRY_LATER_verdict_carrying_its_state(tmp_path, probes):
    """Bounded: a live tool call cannot wait forever, and the refusal names the state it ended on."""
    seen = probes(_discovery("starting", reason_code="gateway_booting", detail="pid 4242 warming"))

    with pytest.raises(mailbox.AuthorityNotReady) as raised:
        mailbox.discover_serving_endpoint(tmp_path, attempts=3, interval=0.0)

    assert len(seen) == 3, "the probe budget is exactly the bound"
    assert raised.value.state == "starting"
    assert raised.value.decision == mailbox.RETRY_LATER_GATEWAY_BOOTING
    assert raised.value.reason_code == "gateway_booting"
    assert "state=starting" in str(raised.value), str(raised.value)


@pytest.mark.parametrize("state", ["inaccessible", "incompatible"])
def test_an_ABSENT_owner_is_a_single_verdict_and_never_re_probed(tmp_path, probes, state):
    """A target that is not there is a verdict now: re-probing it only delays the answer."""
    seen = probes(_discovery(state))

    with pytest.raises(mailbox.AuthorityNotReady) as raised:
        mailbox.discover_serving_endpoint(tmp_path, attempts=4, interval=0.0)

    assert len(seen) == 1
    assert raised.value.state == state
    assert raised.value.decision == mailbox.FAILED_TARGET_ABSENT
    assert f"state={state}" in str(raised.value)


def test_the_DECISION_is_the_VALUE_the_sender_reads_and_the_message_is_only_a_description(tmp_path, probes):
    """The invariant: a copy edit of the sentence can never turn a retry into a discard.

    The exception stays a ``PreDialError``/``ValueError`` so the callers that already catch those
    keep working, and the state survives the dial's wrapper (``AuthorityNotReady`` is re-raised,
    never flattened back to a bare ``PreDialError`` whose text is all that is left).
    """
    probes(_discovery("starting", reason_code="gateway_booting"))
    with pytest.raises(mailbox.AuthorityNotReady) as booting:
        mailbox.discover_serving_endpoint(tmp_path, attempts=1, interval=0.0)
    probes(_discovery("inaccessible"))
    with pytest.raises(mailbox.AuthorityNotReady) as absent:
        mailbox.discover_serving_endpoint(tmp_path, attempts=1, interval=0.0)

    assert isinstance(booting.value, mailbox.PreDialError) and isinstance(booting.value, ValueError)
    assert booting.value.decision != absent.value.decision
    assert booting.value.decision == mailbox.RETRY_LATER_GATEWAY_BOOTING
    assert absent.value.decision == mailbox.FAILED_TARGET_ABSENT


def test_the_dial_keeps_the_not_ready_STATE_instead_of_flattening_it(tmp_path, probes):
    """A sender that reaches the dial (not the discovery) must still be able to tell wait from gone."""
    probes(_discovery("starting", reason_code="gateway_booting"))

    with pytest.raises(mailbox.AuthorityNotReady) as raised:
        mailbox.authority_delivery(tmp_path, {"id": "0" * 64, "message": "probe"})

    assert raised.value.state == "starting"
    assert raised.value.decision == mailbox.RETRY_LATER_GATEWAY_BOOTING
