"""A finite one-shot pass that is lost settles itself, and the settle says so once.

Measured 2026-11-21 on the live host: every ``unknown`` admission (16 rows across 5 stores) was a
seat-diddler pass admitted as ``{"finite": true, "text": ...}`` under the person's own principal,
with no ``unattended`` key. ``is_unattended`` therefore answered "attended" for the whole
population, so the release edge that exists for exactly this class skipped every row and each
session's FIFO stayed paused until an operator noticed. The marker that is always present is
``finite``; requiring a caller to remember a second flag made the edge unreachable.

The cases below pin the class (and its boundary: a person's ordinary turn still pauses), and pin
the settle's receipt - the resolution id the census prints, which makes a double-run visible as
the SAME id rather than a second decision.

Real ``SessionStore`` SQLite; no store monkeypatching.
"""
import sqlite3
from pathlib import Path

import pytest

from gateway.config import GatewayConfig
from gateway.session import SessionStore
from gateway.session_unattended_unknown import (is_unattended, settle_recovered_unattended)
from hermes_state_runtime import (RuntimeStoreError, admit_session_input, begin_runtime_epoch,
                                  claim_session_input, get_session_admission)

PERSON = 'uid:502'


def _db(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    db = store._db
    db.create_session('s', source='gateway')
    return db


def _admit(db, epoch, *, finite, text='pass'):
    payload = {'text': text}
    if finite:
        payload['finite'] = True
    return admit_session_input(db, epoch=epoch, principal_id=PERSON, session_id='s',
                               request_id='req-%s' % text, payload=payload)


def _lose(db, admission_id):
    """The shape a lost turn leaves: claimed (started) and then fenced unknown by its owner."""
    db._execute_write(lambda conn: conn.execute(
        "UPDATE session_admissions SET status='unknown' WHERE admission_id=?", (admission_id,)))


def _resolution(db, admission_id):
    with db._read_ctx() as conn:
        row = conn.execute('SELECT value FROM state_meta WHERE key=?',
                           ('session_input.resolution.v1.%s' % admission_id,)).fetchone()
    return row[0] if row else None


def test_a_finite_pass_is_unattended_even_without_the_second_flag():
    assert is_unattended({'principal_id': PERSON, 'payload': {'finite': True, 'text': 'x'}}) is True


def test_a_persons_ordinary_turn_is_still_attended():
    """The boundary: widening must not auto-interrupt a message somebody is waiting on."""
    assert is_unattended({'principal_id': PERSON, 'payload': {'text': 'x'}}) is False
    assert is_unattended({'principal_id': PERSON, 'payload': {'finite': False, 'text': 'x'}}) is False


def test_the_release_edge_settles_a_lost_finite_pass_and_records_one_resolution(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    epoch = begin_runtime_epoch(db, instance_id='owner-1')
    row = _admit(db, epoch, finite=True)
    claim_session_input(db, epoch=epoch, session_id='s')
    _lose(db, row['admission_id'])

    assert settle_recovered_unattended(db, epoch) == 1
    settled = get_session_admission(db, admission_id=row['admission_id'])
    assert (settled['status'], settled['outcome']) == ('terminal', 'interrupted')
    receipt = _resolution(db, row['admission_id'])
    import json
    record = json.loads(receipt)
    assert record['edge'] == 'boot' and record['seq'] == row['seq'] and record['resolution_id']
    # idempotent: nothing left to settle, and the receipt is ONE id, not a second decision
    assert settle_recovered_unattended(db, epoch) == 0
    assert _resolution(db, row['admission_id']) == receipt


def test_a_lost_persons_turn_still_pauses_the_claim_and_the_refusal_names_the_row(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    epoch = begin_runtime_epoch(db, instance_id='owner-1')
    row = _admit(db, epoch, finite=False, text='mine')
    claim_session_input(db, epoch=epoch, session_id='s')
    _lose(db, row['admission_id'])

    assert settle_recovered_unattended(db, epoch) == 0  # a person's row is not ours to settle
    with pytest.raises(RuntimeStoreError) as caught:
        claim_session_input(db, epoch=epoch, session_id='s')
    assert caught.value.reason == 'unknown_execution'
    assert caught.value.detail == 'seq=%d cleared_by=none' % row['seq']
