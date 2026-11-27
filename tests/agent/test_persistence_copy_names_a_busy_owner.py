"""A save whose receipt never came back names the BUSY GATEWAY, not a full disk.

The classifier already names ``worker_receipt_timeout`` / ``pending_receipt`` as
``receipt_timeout`` (card t_5800fe41). The turn explainer's cause set did not carry that cause,
so the most common worker death - 12 of the 26 crashed/reclaimed research runs in 72 h - still
rendered ``explainer.persistence.default``: a sentence that asserts no measured cause and gives
the operator nothing to act on. ``receipt_timeout`` now has its own copy.

The last test is the invariant, not one string: every cause the classifier can return must have
an explainer key AND a rendered copy, so a future named cause cannot silently fall back to the
default sentence again.
"""
from agent.i18n import t
from agent.runtime_session_store import WorkerPersistenceError
from agent.turn_explainers import (
    TurnExplainersMixin,
    _PERSISTENCE_CAUSES,
    _persistence_explanation_key,
)
from hermes_state_errors import PERSISTENCE_ERROR_CAUSES, classify_persistence_error


def test_a_missed_receipt_classifies_and_renders_its_own_copy():
    exc = WorkerPersistenceError('worker_receipt_timeout')
    cause = classify_persistence_error(exc)
    assert cause == 'receipt_timeout'
    assert cause in _PERSISTENCE_CAUSES
    assert _persistence_explanation_key(cause) == 'explainer.persistence.receipt_timeout'
    assert t(_persistence_explanation_key(cause)) != t('explainer.persistence.default')


def test_pending_receipt_is_the_same_class():
    assert classify_persistence_error(WorkerPersistenceError('pending_receipt')) == 'receipt_timeout'
    # The longer form the store actually raises after its retry window.
    long_form = ('worker_receipt_timeout after 75.0s (attempt 4, 75.0s total): '
                 'the owner did not ack worker.persist')
    assert classify_persistence_error(long_form) == 'receipt_timeout'


def test_the_busy_owner_copy_names_the_gateway_the_retry_and_the_loss():
    copy = t('explainer.persistence.receipt_timeout')
    assert copy != 'explainer.persistence.receipt_timeout'
    # The cause: the owner's loop was restarting or stalled, not the disk and not a lock.
    assert 'gateway' in copy
    assert 'restarting' in copy or 'stalled' in copy
    # What happened to the message, and what to do next.
    assert 'never written' in copy
    assert 'not lost' in copy
    assert 'Send your message again' in copy
    assert 'out of room' not in copy
    assert 'holding the database' not in copy


def test_the_whole_explainer_sentence_names_the_busy_gateway():
    body = TurnExplainersMixin._format_turn_completion_explanation(
        'session_persistence_failed', persistence_cause='receipt_timeout',
    )
    assert body
    assert 'gateway' in body
    assert 'not lost' in body
    assert 'out of room' not in body
    assert 'holding the database' not in body


def test_every_classified_cause_has_an_explainer_key_and_a_rendered_copy():
    """No named cause may fall through to the default copy again."""
    named = [cause for cause in PERSISTENCE_ERROR_CAUSES if cause != 'unknown']
    assert named, 'the cause list must not be empty'
    assert [c for c in named if c not in _PERSISTENCE_CAUSES] == []
    assert [c for c in named
            if t(f'explainer.persistence.{c}') == f'explainer.persistence.{c}'] == []
