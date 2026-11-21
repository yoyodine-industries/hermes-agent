"""A queue_full refusal at the peer-DM door names its budget, and the remedy is a retry.

The settled row already persists ``limit_class``/``limit`` (t-37b8e0c562 a), but the sentence the
remote SENDER reads was the bare reason: it named neither the budget that fired nor that resending
is safe. The admission cap is read BEFORE the queued insert, so nothing was admitted and "do not
resend" - the label for an admission that may have happened - would be false here.
"""
import hermes_state_runtime

from gateway.platforms import api_server_bot_chat as bot_chat

TIP = {'id': 'sess-1', 'chat_id': 'local-bot-chat'}


def _refused(**kwargs):
    """``_refusal`` for a queue_full refusal; ``limit`` is set as an attribute because only the
    admission store's own error subclass carries it."""
    limit = kwargs.pop('limit', None)
    exc = hermes_state_runtime.RuntimeStoreError('queue_full', **kwargs)
    if limit is not None:
        setattr(exc, 'limit', limit)
    return bot_chat._refusal(None, TIP, exc)


def test_a_queue_full_refusal_names_the_budget_that_fired():
    reason, error = _refused(field='automation_fifo', limit=32)
    assert reason == 'queue_full'
    assert 'automation_fifo' in error
    assert '32' in error


def test_a_queue_full_refusal_says_resending_is_safe_and_never_do_not_resend():
    _, error = _refused(field='automation_fifo', limit=32, retry_after=45)
    assert 'safe to resend in 45s' in error
    assert 'do not resend' not in error.lower()


def test_a_queue_full_refusal_without_an_owner_retry_after_uses_the_floor():
    _, error = _refused(field='queue')
    assert f'safe to resend in {hermes_state_runtime.ADMISSION_RETRY_AFTER_SECONDS:.0f}s' in error
    assert 'queue' in error


def test_a_queue_full_refusal_without_a_class_still_names_the_reason():
    reason, error = _refused()
    assert reason == 'queue_full'
    assert 'queue_full' in error
    assert 'safe to resend' in error


def test_another_refusal_keeps_the_bare_shape():
    reason, error = bot_chat._refusal(None, TIP, hermes_state_runtime.RuntimeStoreError('runtime_draining'))
    assert (reason, error) == ('runtime_draining', 'Bot Chat admission refused: runtime_draining')
