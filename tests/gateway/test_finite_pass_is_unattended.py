"""The class the release edge exists for, and the boundary it must not cross.

A turn lost across an owner restart is fenced ``unknown`` and pauses its session's FIFO, because
only the person who sent it knows whether to resend it. A one-shot pass has no such person: nobody
can be asked to resend it, so a pause on it is permanent. ``settle_recovered_unattended`` (boot) and
the drain's claim retry both release exactly the ``is_unattended`` rows, so this predicate IS the
release edge's population.

It required a caller to set ``unattended`` beside ``finite``; a lost one-shot carries ``finite``
alone (``admit_finite`` refuses ``unattended`` unless finite, and the finite runner is the one-shot/
API/automation path), so the predicate answered False for the whole population and every one of
those sessions stayed paused. ``finite`` is the marker that is always there; the boundary is that a
person's ordinary turn (finite absent or false) must still pause.
"""
import pytest

from gateway.session_unattended_unknown import is_unattended

PERSON = 'uid:502'


def test_a_lost_finite_one_shot_is_unattended():
    assert is_unattended({'principal_id': PERSON, 'payload': {'finite': True, 'text': 'pass'}}) is True


@pytest.mark.parametrize('payload', [{'text': 'mine'}, {'finite': False, 'text': 'mine'}])
def test_a_persons_ordinary_turn_still_waits_for_them(payload):
    assert is_unattended({'principal_id': PERSON, 'payload': payload}) is False
