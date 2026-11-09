"""The admission FIFO carries two classes on one queue: a peer DM or an operator prompt is a fact
another seat is waiting on, an automation notice (a watch tick, a completion batch, a route wake) is
a hint the seat can read later. A full class of hints must never refuse a fact, and the fact must
drain ahead of them (t_0b41230f).
"""
import pytest

from hermes_state import SessionDB
from hermes_state_runtime import (
    ADMISSION_QUEUE_MAX, AUTOMATION, INTERACTIVE, RuntimeStoreError, admission_class,
    admission_claim_rank, admit_session_input, begin_runtime_epoch, claim_session_input,
    list_session_admissions,
)

SESSION = 's'


def _db(tmp_path):
    db = SessionDB(db_path=tmp_path / 'state.db')
    db.create_session(SESSION, source='test')
    return db, begin_runtime_epoch(db, instance_id='current')


def _notice(n):
    """An automation notice: a gateway-produced descriptor with no ``turn_author``."""
    return {'local_automation_v1': {'display_kind': 'watch', 'identity': f'watch:{n}',
                                    'owner': SESSION, 'target': SESSION},
            'text': f'watch tick {n}'}


def _peer_dm(n):
    """A peer DM relayed into this Bot Chat: an automation-shaped descriptor WITH ``turn_author``."""
    return {'local_automation_v1': {'identity': f'bot:{n}', 'owner': SESSION, 'target': SESSION,
                                    'turn_author': {'id': 'bot:platform-stl', 'name': 'platform-stl',
                                                    'is_bot': True}},
            'text': f'message from a peer {n}'}


def _prompt(n):
    """A person's prompt or an API turn: no automation descriptor at all."""
    return {'text': f'prompt {n}', 'input': f'prompt {n}'}


def _admit(db, epoch, request_id, payload, principal_id='automation:route'):
    return admit_session_input(db, epoch=epoch, principal_id=principal_id, session_id=SESSION,
                               request_id=request_id, payload=payload)


def _queued(db):
    return [row for row in list_session_admissions(db, session_id=SESSION) if row['status'] == 'queued']


def test_the_classes_are_told_apart_by_the_payload():
    assert admission_class(_notice(1)) == AUTOMATION
    assert admission_class(_peer_dm(1)) == INTERACTIVE
    assert admission_class(_prompt(1)) == INTERACTIVE
    assert admission_class({}) == INTERACTIVE
    assert admission_class(None) == INTERACTIVE


def test_a_full_automation_fifo_still_admits_a_peer_dm_and_an_operator_prompt(tmp_path):
    """The regression the card names: >= the cap in automation notices pending, and the fact
    classes are admitted anyway (before the split, every one of these was refused queue_full)."""
    db, epoch = _db(tmp_path)
    for n in range(ADMISSION_QUEUE_MAX):
        _admit(db, epoch, f'notice-{n}', _notice(n))
    assert len(_queued(db)) == ADMISSION_QUEUE_MAX

    peer = _admit(db, epoch, 'peer-dm', _peer_dm(1), principal_id='automation:route')
    prompt = _admit(db, epoch, 'operator', _prompt(1), principal_id='human')
    assert (peer['status'], prompt['status']) == ('queued', 'queued')

    # The automation class keeps its own bound: one more notice is still held back, and the
    # refusal says WHICH budget fired.
    with pytest.raises(RuntimeStoreError) as refused:
        _admit(db, epoch, 'notice-overflow', _notice(99))
    assert refused.value.reason == 'queue_full'
    assert refused.value.field == 'automation_fifo'
    assert len(_queued(db)) == ADMISSION_QUEUE_MAX + 2


def test_a_full_interactive_fifo_refuses_the_next_fact_and_still_admits_hints(tmp_path):
    db, epoch = _db(tmp_path)
    for n in range(ADMISSION_QUEUE_MAX):
        _admit(db, epoch, f'peer-{n}', _peer_dm(n), principal_id='automation:route')
    with pytest.raises(RuntimeStoreError) as refused:
        _admit(db, epoch, 'peer-overflow', _peer_dm(99), principal_id='automation:route')
    assert refused.value.reason == 'queue_full'
    assert refused.value.field == 'interactive_fifo'
    # The hint class is not collateral damage of the fact backlog: it has room of its own.
    assert _admit(db, epoch, 'notice', _notice(1))['status'] == 'queued'


def test_the_claim_drains_a_peer_dm_ahead_of_a_full_automation_backlog(tmp_path):
    """Admitting the fact is not enough: it must not wait behind 30 hints' worth of turns."""
    db, epoch = _db(tmp_path)
    for n in range(ADMISSION_QUEUE_MAX):
        _admit(db, epoch, f'notice-{n}', _notice(n))
    peer = _admit(db, epoch, 'peer-dm', _peer_dm(1), principal_id='automation:route')

    first = claim_session_input(db, epoch=epoch, session_id=SESSION)
    assert first['admission_id'] == peer['admission_id']
    # Inside the class the FIFO still holds: the oldest notice is next.
    assert admission_claim_rank(_queued(db)[0]) == (1, 1)
    db._execute_write(lambda conn: conn.execute(
        "UPDATE session_admissions SET status='terminal',outcome='completed' WHERE admission_id=?",
        (peer['admission_id'],)))
    assert claim_session_input(db, epoch=epoch, session_id=SESSION)['request_id'] == 'notice-0'


def test_a_replacement_is_admitted_at_a_full_automation_fifo(tmp_path):
    """A liveness tick that supersedes its own queued predecessor takes the slot it frees: checked
    before the cancellation it would be refused queue_full forever, at a depth it keeps bounded."""
    db, epoch = _db(tmp_path)
    rows = [_admit(db, epoch, f'notice-{n}', _notice(n)) for n in range(ADMISSION_QUEUE_MAX)]
    replaced = rows[-1]

    def replaces(conn):
        changed = conn.execute("UPDATE session_admissions SET status='terminal',outcome='cancelled' "
                               "WHERE admission_id=? AND status='queued'", (replaced['admission_id'],)).rowcount
        assert changed == 1

    replacement = admit_session_input(db, epoch=epoch, principal_id='automation:route',
                                      session_id=SESSION, request_id='notice-replacement',
                                      payload=_notice(99), _authorize_write=replaces)
    assert replacement['status'] == 'queued'
    assert len(_queued(db)) == ADMISSION_QUEUE_MAX
