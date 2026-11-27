"""A save that never reached the store names the OWNER, not a full disk or a held lock.

The unclassified fallback used to guess "the drive is out of room, or another Hermes process is
holding the database" - both false for the measured worker deaths, where the owner's descriptor
was momentarily not ready and the write was never attempted at all.
"""
from agent.i18n import t
from agent.turn_explainers import _persistence_explanation_key


def test_owner_unavailable_names_itself_instead_of_falling_through():
    assert (_persistence_explanation_key('owner_unavailable')
            == 'explainer.persistence.owner_unavailable')


def test_the_unclassified_copy_does_not_assert_a_cause_it_never_measured():
    copy = t('explainer.persistence.default')
    assert 'out of room' not in copy
    assert 'holding the database' not in copy


def test_the_owner_copy_names_the_gateway_and_says_nothing_was_lost():
    copy = t('explainer.persistence.owner_unavailable')
    assert 'gateway' in copy
    assert 'not lost' in copy
