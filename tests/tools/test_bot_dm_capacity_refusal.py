"""A capacity refusal reserves nothing, so it is RETRYABLE and never says "do not resend".

The queue cap is checked before ``admit_session_input`` writes its idempotency row: a refusal
there leaves no admission anywhere. A refusal that reserved an id (an ``admission_conflict``, a
held owner's settled id) keeps the ambiguous "do not resend" wording; a capacity refusal must
carry ``retry_after`` instead.
"""
import json

import pytest

import hermes_state_runtime as rt
from hermes_state import SessionDB
from tools import bot_mode_dm


class _Refusal(Exception):
    """The shape the gateway RPC client raises: a bounded reason code plus a data frame."""

    def __init__(self, reason, **data):
        super().__init__(reason)
        self.data = data


def test_queue_full_reserves_no_row_and_carries_retry_after(tmp_path):
    db = SessionDB(db_path=tmp_path / 'state.db')
    db.create_session('s', source='test')
    epoch = rt.begin_runtime_epoch(db, instance_id='boot')
    with db:
        for n in range(rt.ADMISSION_QUEUE_MAX):
            rt.admit_session_input(db, epoch=epoch, principal_id='human', session_id='s',
                                   request_id=f'r{n}', payload={'text': n})
        before = rt.list_session_admissions(db, session_id='s', pending_only=False)
        with pytest.raises(rt.RuntimeStoreError) as refused:
            rt.admit_session_input(db, epoch=epoch, principal_id='human', session_id='s',
                                   request_id='overflow', payload={'text': 'x'})
        after = rt.list_session_admissions(db, session_id='s', pending_only=False)
    assert refused.value.reason == 'queue_full'
    assert refused.value.retryable is True
    assert refused.value.retry_after == rt.ADMISSION_RETRY_AFTER_SECONDS
    # Nothing was reserved: no idempotency row, no seq - so a retry is not a duplicate.
    assert len(after) == len(before) == rt.ADMISSION_QUEUE_MAX
    assert not any(row['request_id'] == 'overflow' for row in after)
    db.close()


def test_capacity_refusal_is_reported_retryable_not_as_do_not_resend(tmp_path):
    dm_file = tmp_path / 'dm.txt'
    dm_file.write_text('hello')
    busy = bot_mode_dm.LiveTargetBusyError("the lane's queue is full (32/32 queued)",
                                          retry_after=rt.ADMISSION_RETRY_AFTER_SECONDS)
    payload = json.loads(bot_mode_dm._unavailable_payload(str(dm_file), busy))
    assert payload['reason'] == 'queue_full'
    assert payload['retryable'] is True
    assert payload['retry_after'] == rt.ADMISSION_RETRY_AFTER_SECONDS
    assert 'Do not resend' not in payload['error']
    assert 'safe to resend' in payload['error']

    # The RPC refusal names the cap by code only; the client still recognises it as retryable.
    assert bot_mode_dm._capacity_retry_after(_Refusal('queue_full')) == rt.ADMISSION_RETRY_AFTER_SECONDS
    assert bot_mode_dm._capacity_retry_after(_Refusal('queue_full', retry_after=5.0)) == 5.0
    assert bot_mode_dm._capacity_retry_after(_Refusal('admission_conflict')) is None


def test_an_empty_cause_is_named_instead_of_leaving_a_bare_refusal(tmp_path):
    class _Bare(Exception):
        def __str__(self):
            return ''

    dm_file = tmp_path / 'dm.txt'
    dm_file.write_text('hello')
    payload = json.loads(bot_mode_dm._live_outcome_unknown(str(dm_file), _Bare()))
    assert 'Live admission outcome unknown' in payload['error']
    assert '_Bare with no message' in payload['error']
