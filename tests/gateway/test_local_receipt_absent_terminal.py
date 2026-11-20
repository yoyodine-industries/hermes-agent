"""A receipt the store says does not exist is ``not_found`` and ENDS the recovery (#t_0b90f1db).

The gateway logged ``Local session local-a608… receipt refused: storage_unavailable at
local_receipt:394`` every 2-6 s for at least 20 minutes against a session id that existed in none of
the nine state stores, with no backoff and no terminal action. Two defects made that possible: absence
was reported as a storage outage (so every caller read "try again"), and the retry had no bound (so
"try again" was forever, one log line per client retry).

These tests pin the two classes apart, the terminal action for absence, the bounded retry for a store
that cannot serve, the attempt count in the failure record, and that a given-up session is not
scheduled by a second recovery pass.
"""
from types import SimpleNamespace

import pytest

from gateway.session_controls import AuthorityConnection


async def _owner(tmp_path, monkeypatch, instance_id):
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority

    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'platform_toolsets': {'cli': []}})
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    runner = SimpleNamespace(session_store=store, _session_db=store._db, adapters={}, _draining=False,
                             _cached_agent_for=lambda route: None, _adapter_for_source=lambda source: None)
    authority = await initialize_session_authority(runner, profile_id=str(tmp_path), instance_id=instance_id)
    return authority, AuthorityConnection(authority, object(), {'user_id': 'owner'})


def _new_session(authority, connection, request_id='r'):
    from gateway.session_local import create_local_session
    return create_local_session(authority, connection.actor, {
        'request_id': request_id, 'source': 'tui', 'cwd': str(authority.db.db_path.parent),
        'model': 'm', 'toolsets': []})


def _delete_session_and_receipt(db, sid):
    """The live shape: the receipt row and the session row are both gone, as in all nine stores."""
    def write(conn):
        conn.execute('DELETE FROM state_meta WHERE key=?', ('gateway.local_policy.v1:' + sid,))
        conn.execute('DELETE FROM sessions WHERE id=?', (sid,))
    db._execute_write(write)


def _refusals(caplog):
    return [r for r in caplog.records if 'receipt refused' in r.getMessage() or 'GIVEN UP' in r.getMessage()]


@pytest.mark.asyncio
async def test_an_absent_receipt_is_not_found_and_an_unreadable_one_is_storage_unavailable(tmp_path, monkeypatch):
    """The two refusal classes are distinguishable in the type, not in the string: a store that
    answered "no row" is not a store that failed, and only one of them can heal by retrying."""
    from hermes_state_local import local_receipt
    from hermes_state_runtime import RuntimeStoreError

    authority, connection = await _owner(tmp_path, monkeypatch, 'absent')
    try:
        with pytest.raises(RuntimeStoreError) as absent:
            local_receipt(authority.db, 'local-does-not-exist')
        assert absent.value.reason == 'not_found'
        assert absent.value.field == 'receipt'
        assert 'gateway.local_policy.v1:local-does-not-exist' in absent.value.detail

        # A row that exists and cannot be parsed IS a store that cannot serve: still retryable.
        def write(conn):
            conn.execute('INSERT INTO state_meta(key, value) VALUES(?, ?)',
                         ('gateway.local_policy.v1:local-corrupt', '{not json'))
        authority.db._execute_write(write)
        with pytest.raises(RuntimeStoreError) as corrupt:
            local_receipt(authority.db, 'local-corrupt')
        assert corrupt.value.reason == 'storage_unavailable'
        assert corrupt.value.reason != absent.value.reason
    finally:
        await connection.close()
        authority.db.close()


@pytest.mark.asyncio
async def test_a_missing_receipt_ends_the_recovery_after_one_attempt(tmp_path, monkeypatch, caplog):
    """Absence is terminal at once: one refusal line naming the session, the ``state_meta`` key and
    the store, a terminal record, and no further log line from the retries that follow."""
    from gateway import session_local_recovery as recovery
    from hermes_state_runtime import RuntimeStoreError

    authority, connection = await _owner(tmp_path, monkeypatch, 'terminal')
    try:
        ref = _new_session(authority, connection)
        sid = ref.session_id
        _delete_session_and_receipt(authority.db, sid)
        caplog.set_level('WARNING', logger='gateway.session_local_recovery')

        for attempt in range(1, 4):  # a client retrying every 2-6 s: each one hits the terminal record
            with pytest.raises(RuntimeStoreError) as refused:
                recovery.restore_local_session(authority, sid)
            assert refused.value.reason == 'not_found'
            if attempt > 1:
                assert refused.value.field == 'recovery_terminal'

        record = recovery.local_recovery_record(authority, sid)
        assert record is not None and record['terminal'] is True
        assert record['attempts'] == 1, 'a terminal session must not spend more attempts'
        assert record['key'] == 'gateway.local_policy.v1:' + sid
        assert record['store'] == str(authority.db.db_path)
        assert 'not_found at local_receipt' in record['detail']

        lines = _refusals(caplog)
        assert len(lines) == 1, [r.getMessage() for r in lines]
        assert 'GIVEN UP' in lines[0].getMessage()
        assert sid in lines[0].getMessage() and record['key'] in lines[0].getMessage()
        assert str(authority.db.db_path) in lines[0].getMessage()
        assert lines[0].levelname == 'ERROR'
    finally:
        await connection.close()
        authority.db.close()


@pytest.mark.asyncio
async def test_an_intermittently_failing_store_retries_and_the_count_is_in_the_record(tmp_path, monkeypatch, caplog):
    """A store that cannot serve still retries, with exponential backoff, and gives up at the bound —
    the attempt count in the failure record, the store untouched once the budget is spent."""
    from gateway import session_local_recovery as recovery
    from hermes_state_runtime import RuntimeStoreError

    authority, connection = await _owner(tmp_path, monkeypatch, 'transient')
    try:
        ref = _new_session(authority, connection)
        sid = ref.session_id
        calls = []
        real = recovery.local_receipt

        def flaky(db, session_id):
            calls.append(session_id)
            raise RuntimeStoreError('storage_unavailable', field='db', detail='disk i/o error')
        monkeypatch.setattr(recovery, 'local_receipt', flaky)
        clock = {'now': 1000.0}
        monkeypatch.setattr(recovery, '_monotonic', lambda: clock['now'])
        caplog.set_level('WARNING', logger='gateway.session_local_recovery')

        seen = []
        for step in range(recovery.RECOVERY_MAX_ATTEMPTS):
            with pytest.raises(RuntimeStoreError) as refused:
                recovery.restore_local_session(authority, sid)
            seen.append(recovery.local_recovery_record(authority, sid))
            assert refused.value.reason == 'storage_unavailable'
            clock['now'] += 120  # past every backoff window

        assert [r['attempts'] for r in seen] == [1, 2, 3, 4, 5]
        assert [r['terminal'] for r in seen] == [False, False, False, False, True]
        assert seen[-1]['reason'] == 'storage_unavailable'
        assert 'storage_unavailable at flaky' in seen[-1]['detail']
        assert len(calls) == recovery.RECOVERY_MAX_ATTEMPTS
        assert len(_refusals(caplog)) == recovery.RECOVERY_MAX_ATTEMPTS

        # Budget spent: the next restore is refused from the record, without reading the store.
        with pytest.raises(RuntimeStoreError) as spent:
            recovery.restore_local_session(authority, sid)
        assert spent.value.field == 'recovery_terminal'
        assert len(calls) == recovery.RECOVERY_MAX_ATTEMPTS, 'a given-up session must not touch the store'
        assert len(_refusals(caplog)) == recovery.RECOVERY_MAX_ATTEMPTS

        # Inside the backoff window the refusal is silent too: no store read, no log line.
        monkeypatch.setattr(recovery, 'local_receipt', real)
    finally:
        await connection.close()
        authority.db.close()


@pytest.mark.asyncio
async def test_an_intermittent_store_that_heals_keeps_retrying_then_drops_its_record(tmp_path, monkeypatch):
    """A store that fails twice and then serves is recovered on the third attempt: the backoff is a
    delay, not a death sentence, and a healed session carries no failure state."""
    from gateway import session_local_recovery as recovery
    from hermes_state_runtime import RuntimeStoreError

    authority, connection = await _owner(tmp_path, monkeypatch, 'heals')
    try:
        ref = _new_session(authority, connection)
        sid = ref.session_id
        real = recovery.local_receipt
        failures = {'left': 2}

        def intermittent(db, session_id):
            if failures['left']:
                failures['left'] -= 1
                raise RuntimeStoreError('storage_unavailable', field='db', detail='disk busy')
            return real(db, session_id)
        monkeypatch.setattr(recovery, 'local_receipt', intermittent)
        clock = {'now': 1000.0}
        monkeypatch.setattr(recovery, '_monotonic', lambda: clock['now'])

        for _ in range(2):
            with pytest.raises(RuntimeStoreError):
                recovery.restore_local_session(authority, sid)
            assert recovery.local_recovery_record(authority, sid)['attempts'] in (1, 2)
            if _ == 0:
                break  # the backoff window is asserted from attempt 1, before the clock moves
            clock['now'] += 120  # past the backoff window

        # Inside a backoff window the restore is refused without touching the store.
        monkeypatch.setattr(recovery, 'local_receipt', lambda db, session_id: (_ for _ in ()).throw(
            AssertionError('the backoff window must not read the store')))
        with pytest.raises(RuntimeStoreError) as backed_off:
            recovery.restore_local_session(authority, sid)
        assert backed_off.value.field == 'recovery_backoff'

        clock['now'] += 120
        monkeypatch.setattr(recovery, 'local_receipt', intermittent)
        with pytest.raises(RuntimeStoreError):
            recovery.restore_local_session(authority, sid)  # attempt 2: the store fails again
        assert recovery.local_recovery_record(authority, sid)['attempts'] == 2

        clock['now'] += 120
        recovered = recovery.restore_local_session(authority, sid)  # attempt 3: the store serves
        assert recovered.session_id == sid
        assert recovery.local_recovery_record(authority, sid) is None
    finally:
        await connection.close()
        authority.db.close()


@pytest.mark.asyncio
async def test_a_second_recovery_pass_does_not_reschedule_a_given_up_session(tmp_path, monkeypatch, caplog):
    """The boot sweep skips a session the retry budget already spent: no store read, no new line, and
    no ``_schedule`` for it — the give-up is final for this owner."""
    from gateway import session_local_recovery as recovery
    from hermes_state_runtime import RuntimeStoreError

    authority, connection = await _owner(tmp_path, monkeypatch, 'sweep')
    try:
        ref = _new_session(authority, connection)
        sid = ref.session_id

        def broken(db, session_id):
            raise RuntimeStoreError('storage_unavailable', field='db', detail='disk i/o error')
        monkeypatch.setattr(recovery, 'local_receipt', broken)
        clock = {'now': 1000.0}
        monkeypatch.setattr(recovery, '_monotonic', lambda: clock['now'])
        caplog.set_level('WARNING', logger='gateway.session_local_recovery')
        for _ in range(recovery.RECOVERY_MAX_ATTEMPTS):
            with pytest.raises(RuntimeStoreError):
                recovery.restore_local_session(authority, sid)
            clock['now'] += 120
        before = len(_refusals(caplog))
        assert recovery.local_recovery_record(authority, sid)['terminal'] is True

        scheduled = []
        monkeypatch.setattr(authority, '_schedule', lambda ref: scheduled.append(ref), raising=False)
        recovery.recover_local_sessions(authority, schedule=True)

        assert sid not in [getattr(entry, 'session_id', entry) for entry in scheduled]
        assert len(_refusals(caplog)) == before, 'a given-up session must not be retried or re-logged'
        assert recovery.local_recovery_record(authority, sid)['attempts'] == recovery.RECOVERY_MAX_ATTEMPTS
    finally:
        await connection.close()
        authority.db.close()
