"""The pre-dial door: two facts tested separately, and a payload that survives the wrap.

Before this, ``state != 'ready' or endpoint is None`` raised one sentence for two possible
causes and ``PreDialError(str(exc))`` dropped whatever the wrapped exception knew — so a
socket, ticket or discovery failure all read as the same string, and an absent payload was
indistinguishable from an empty one.
"""
from types import SimpleNamespace

import pytest

from tools import bot_live_delivery as door


def _discovery(state, endpoint):
    return SimpleNamespace(state=state, endpoint=endpoint)


def test_state_failure_names_state_and_keeps_endpoint_fact():
    err = door._not_ready_error(_discovery('stopped', object()))
    assert err.payload['which'] == 'state'
    assert err.payload['endpoint_present'] is True
    assert 'endpoint_present=True' in str(err)


def test_endpoint_failure_names_endpoint_when_state_is_ready():
    err = door._not_ready_error(_discovery('ready', None))
    assert err.payload['which'] == 'endpoint'
    assert err.payload['endpoint_present'] is False


def test_both_facts_can_hold_at_once_and_both_are_named():
    # One sentence for two causes was the defect: this shape used to look like "state only".
    err = door._not_ready_error(_discovery('stopped', None))
    assert err.payload['which'] == 'state+endpoint'
    assert err.payload['endpoint_present'] is False
    assert err.payload['state'] == 'stopped'


def test_absent_payload_is_not_an_empty_payload():
    # A sender reading payload must be able to tell "nothing was known" from "known empty".
    assert door.PreDialError('x').payload is None
    assert door.PreDialError('x', payload={}).payload == {}
    assert door.PreDialError('x', payload={}).payload is not None


def test_refusal_is_still_a_value_error_for_existing_callers():
    err = door._not_ready_error(_discovery('ready', None))
    assert isinstance(err, ValueError)


def test_pre_dial_wrap_carries_the_wrapped_payload(monkeypatch):
    """``_authority_rpc`` must not truncate what its cause knew."""
    import hermes_cli.gateway_client as gateway_client
    import hermes_cli.gateway_runtime as gateway_runtime

    class Bone(Exception):
        payload = {'which': 'endpoint', 'endpoint_present': False}

    monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint',
                        lambda home, timeout=None: _discovery('ready', object()))
    monkeypatch.setattr(gateway_client, '_session_ticket',
                        lambda home, endpoint: (_ for _ in ()).throw(Bone('ticket died')))

    with pytest.raises(door.PreDialError) as caught:
        door._authority_rpc('/tmp/does-not-matter', 'bot_relay.deliver', {})

    assert caught.value.payload == {'which': 'endpoint', 'endpoint_present': False}
    assert 'ticket died' in str(caught.value)


def test_empty_str_exception_still_names_its_class(monkeypatch):
    """A bare ``TimeoutError()`` has str() == '': the wrap must not read ``...: ``."""
    import hermes_cli.gateway_client as gateway_client
    import hermes_cli.gateway_runtime as gateway_runtime

    monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint',
                        lambda home, timeout=None: _discovery('ready', object()))
    monkeypatch.setattr(gateway_client, '_session_ticket',
                        lambda home, endpoint: (_ for _ in ()).throw(TimeoutError()))

    with pytest.raises(door.PreDialError) as caught:
        door._authority_rpc('/tmp/does-not-matter', 'bot_relay.deliver', {})

    assert str(caught.value).startswith('TimeoutError:')
    assert caught.value.payload is None  # absent stays absent


def test_outcome_unknown_names_the_cause_class():
    from tools import bot_mode_dm

    out = bot_mode_dm._live_outcome_unknown('/tmp/evidence', TimeoutError())
    assert 'TimeoutError' in out
    assert 'unknown: .' not in out
