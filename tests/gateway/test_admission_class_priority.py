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


def _native_notice(n):
    """The gateway's SECOND automation shape: a native route writes ``native_text_v1.automation``
    (``snapshot_automation``'s non-local branch) and no ``local_automation_v1`` at all."""
    return {'native_text_v1': {'automation': {'display_kind': 'watch', 'identity': f'watch:{n}',
                                              'owner': SESSION}, 'route': 'route'},
            'text': f'native watch tick {n}'}


def _native_peer_dm(n):
    """A peer DM relayed onto a native route: the native shape carrying ``turn_author``."""
    return {'native_text_v1': {'automation': {'identity': f'bot:{n}', 'owner': SESSION,
                                              'turn_author': {'id': 'bot:platform-stl',
                                                              'name': 'platform-stl', 'is_bot': True}},
                               'route': 'route'},
            'text': f'peer message on a native route {n}'}


def _prompt(n):
    """A person's prompt or an API turn: no automation descriptor at all."""
    return {'text': f'prompt {n}', 'input': f'prompt {n}'}


def _malformed(shape):
    """A descriptor a transport wrote as something other than an object: neither twin recognises it,
    and both must fall back to a fact rather than one of them guessing ``automation``."""
    return {'text': 'malformed descriptor', **shape}


def _admit(db, epoch, request_id, payload, principal_id='automation:route'):
    return admit_session_input(db, epoch=epoch, principal_id=principal_id, session_id=SESSION,
                               request_id=request_id, payload=payload)


def _queued(db):
    return [row for row in list_session_admissions(db, session_id=SESSION) if row['status'] == 'queued']


def test_the_classes_are_told_apart_by_the_payload():
    assert admission_class(_notice(1)) == AUTOMATION
    assert admission_class(_peer_dm(1)) == INTERACTIVE
    assert admission_class(_native_notice(1)) == AUTOMATION
    assert admission_class(_native_peer_dm(1)) == INTERACTIVE
    assert admission_class(_prompt(1)) == INTERACTIVE
    assert admission_class({}) == INTERACTIVE
    assert admission_class(None) == INTERACTIVE


def test_the_sql_twin_and_the_python_helper_agree_on_every_shape(tmp_path):
    """The budget COUNT and the claim's ORDER BY read the SQL twin; a caller ranking rows reads the
    Python helper. One shape known to only one of them holds a fact back or gives a hint a fact's
    slot, so every shape the gateway or a transport writes is checked both ways (t_1bc575c0)."""
    import hermes_state_runtime as runtime
    db, epoch = _db(tmp_path)
    shapes = {'local notice': (_notice(1), AUTOMATION),
              'native notice': (_native_notice(1), AUTOMATION),
              'local peer dm': (_peer_dm(1), INTERACTIVE),
              'native peer dm': (_native_peer_dm(1), INTERACTIVE),
              'bare prompt': (_prompt(1), INTERACTIVE),
              'empty payload': ({}, INTERACTIVE),
              'local descriptor not an object': (_malformed({'local_automation_v1': 'watch'}),
                                                 INTERACTIVE),
              'native descriptor not an object': (_malformed(
                  {'native_text_v1': {'automation': ['watch']}}), INTERACTIVE)}
    for n, (name, (payload, expected)) in enumerate(shapes.items()):
        row = _admit(db, epoch, f'shape-{n}', payload)
        stored = next(r for r in list_session_admissions(db, session_id=SESSION, pending_only=False)
                      if r['admission_id'] == row['admission_id'])
        sql = db._read_all('SELECT ' + runtime._AUTOMATION_ROW_SQL + ' AS automation, '
                           'NOT ' + runtime._AUTOMATION_ROW_SQL + ' AS interactive '
                           'FROM session_admissions WHERE admission_id=?', (row['admission_id'],))
        # Never NULL on either side: the budget queries read ``pred`` for the automation class and
        # ``NOT pred`` for the interactive one, and a NULL row is counted by NEITHER, which silently
        # unfills both FIFOs. ``bool(None) == False`` would hide exactly that.
        assert sql[0]['automation'] in (0, 1) and sql[0]['interactive'] in (0, 1), name
        assert bool(sql[0]['automation']) == (expected == AUTOMATION), name
        assert bool(sql[0]['interactive']) == (expected == INTERACTIVE), name
        assert admission_class(stored['payload']) == expected, name


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


def test_a_full_native_automation_fifo_admits_a_native_peer_dm_and_holds_its_own_notice(tmp_path):
    """Both halves of the native shape, because they fail in opposite directions (t_1bc575c0):
    a peer DM relayed on a native route must be a fact at 32 native-shape notices, and a native
    notice must still be refused by its OWN budget rather than borrowing the interactive one."""
    db, epoch = _db(tmp_path)
    for n in range(ADMISSION_QUEUE_MAX):
        _admit(db, epoch, f'native-notice-{n}', _native_notice(n))
    assert len(_queued(db)) == ADMISSION_QUEUE_MAX

    peer = _admit(db, epoch, 'native-peer-dm', _native_peer_dm(1), principal_id='automation:route')
    assert peer['status'] == 'queued'

    with pytest.raises(RuntimeStoreError) as refused:
        _admit(db, epoch, 'native-notice-overflow', _native_notice(99))
    assert (refused.value.reason, refused.value.field) == ('queue_full', 'automation_fifo')

    # The claim's ORDER BY reads the same predicate: the fact drains ahead of the 32 hints.
    assert claim_session_input(db, epoch=epoch, session_id=SESSION)['admission_id'] == peer['admission_id']
