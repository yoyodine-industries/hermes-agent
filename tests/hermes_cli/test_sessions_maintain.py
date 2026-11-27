"""``session.maintenance`` + ``hermes sessions maintain`` + the doctor overdue guard (t_bf94858f).

State.db maintenance is OWNER-APPLIED: the always-on gateway owns the store, so the pass runs on
the OWNER's live handle (`session.maintenance`, the counterpart of `session.prune`) and a CLI never
writes under it. With no owner the pass runs locally, behind the holder scan and with no `--force`.

The tests below pin the four things that make that safe:
  * no owner -> the local pass runs, and a foreign holder makes it refuse with the holder list;
  * a ready owner -> the RPC performs the pass and the CLI prints the receipt;
  * a foreign holder -> the reclaim is REFUSED as data (holders named, page_count unchanged);
  * the one-time migration (NONE -> INCREMENTAL) and the bounded drain that follows it;
  * authorisation is prune's (native owner + session:operator + same profile);
  * doctor reports a store whose tick can never run, and stops reporting once the key is set.
"""
import json
import subprocess
import sys
import time
from argparse import Namespace
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_state_runtime as rt
from gateway.session_authority import SessionAuthority
from gateway.session_controls import AuthorityConnection
from hermes_cli import sessions_cmd
from hermes_cli import doctor_state
from hermes_state import SessionDB
from hermes_state_maintenance import run_state_db_maintenance

pytestmark = [
    pytest.mark.platforms("posix"),  # the holder scan refuses on POSIX only
    # The holder scan enumerates EVERY process on the host (psutil open-files), which reads paths
    # outside the isolated home by design; nothing here writes outside tmp_path.
    pytest.mark.allow_real_home_io,
]

_HOLDER = ("import sqlite3, sys\nconn = sqlite3.connect(sys.argv[1])\n"
           "conn.execute('SELECT count(*) FROM sqlite_master')\nprint('ready', flush=True)\n"
           "sys.stdin.readline()\n")
_BLOB = "z" * 4000


def _aged(db, sid, days):
    at = time.time() - days * 86400

    def write(conn):
        conn.execute('UPDATE sessions SET started_at=?, last_activity_at=?, ended_at=? WHERE id=?',
                     (at, at, at, sid))
        conn.execute('UPDATE messages SET timestamp=? WHERE session_id=?', (at, sid))
    db._execute_write(write)


def _home(tmp_path, monkeypatch):
    """Every test owns its whole Hermes home, so no CLI path resolves the session-sandbox one."""
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _own_home(tmp_path, monkeypatch):
    return _home(tmp_path, monkeypatch)


def _owned(tmp_path, monkeypatch):
    """A live owner: its own SessionDB plus the production AuthorityConnection dispatch on top."""
    db = SessionDB(db_path=tmp_path / 'state.db')
    epoch = rt.begin_runtime_epoch(db, instance_id='owner')
    runner = SimpleNamespace(_draining=False,
                             session_store=SimpleNamespace(retire_runtime_sessions=lambda ids: None))
    authority = SessionAuthority(runner, profile_id=str(tmp_path), instance_id='owner', db=db, epoch=epoch)
    return db, authority


def _connection(authority, *, operator=True, native=True):
    identity = {'user_id': 'local-operator', 'provider': 'local', 'profile_id': authority.profile_id,
                'instance_id': authority.instance_id, 'native_bootstrap': native,
                'capabilities': ['session:create', 'session:read', 'session:submit', 'session:control']}
    return AuthorityConnection(authority, object(), identity, operator=operator)


async def _dispatch(authority, params, **conn):
    connection = _connection(authority, **conn)
    try:
        return await connection.dispatch({'id': 1, 'method': 'session.maintenance', 'params': params})
    finally:
        await connection.close()


def _seed_prunable(db, sid='old', days=200):
    db.create_session(sid, 'cli')
    db.append_message(sid, 'user', sid)
    _aged(db, sid, days)


def _seed_bulky(db, sid='grow', rows=600):
    db.create_session(sid, 'cli')
    for _ in range(rows):
        db.append_message(sid, 'assistant', _BLOB)
    db.end_session(sid, 'done')
    db._conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')


def _args(**kw):
    base = dict(sessions_action='maintain', dry_run=False, json=False)
    base.update(kw)
    return Namespace(**base)


def _no_owner(monkeypatch):
    from hermes_cli import gateway_runtime
    monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint',
                        lambda home, **_: gateway_runtime.GatewayDiscovery('absent'))


@contextmanager
def _holder(path):
    """A REAL foreign process holding the store (as the gateway does), not a stubbed scan."""
    proc = subprocess.Popen([sys.executable, '-c', _HOLDER, str(path)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert proc.stdout is not None and proc.stdout.readline().strip() == 'ready'
    try:
        yield proc
    finally:
        if proc.poll() is None:
            assert proc.stdin is not None
            proc.stdin.close()
            proc.wait(timeout=10)


# ── 1. no owner: the local pass, and the holder refusal ──────────────────────

def test_with_no_owner_the_local_pass_runs(tmp_path, monkeypatch, capsys):
    _no_owner(monkeypatch)
    with closing(SessionDB(db_path=tmp_path / 'state.db')) as db:
        _seed_prunable(db)
    assert sessions_cmd._cmd_maintain(_args()) == 0
    out = capsys.readouterr().out
    assert 'state.db maintenance' in out and 'pruned 1 session(s)' in out, out
    with closing(SessionDB(db_path=tmp_path / 'state.db')) as db:
        assert db.get_session('old') is None
        assert db.get_meta('last_state_db_maintenance'), 'the pass must record its own receipt'


def test_with_no_owner_and_a_foreign_holder_the_pass_refuses_and_names_it(tmp_path, monkeypatch, capsys):
    _no_owner(monkeypatch)
    with closing(SessionDB(db_path=tmp_path / 'state.db')) as db:
        _seed_prunable(db)
    with _holder(tmp_path / 'state.db') as proc:
        assert sessions_cmd._cmd_maintain(_args()) == 1
        out = capsys.readouterr().out
        assert 'Refusing `hermes sessions maintain`' in out, out
        assert f'PID {proc.pid}' in out, out
        assert '--force' not in out, 'this verb offers no override'
    with closing(SessionDB(db_path=tmp_path / 'state.db')) as db:
        assert db.get_session('old') is not None, 'a refused pass must not have written'


# ── 2. a ready owner: the RPC performs the pass, the CLI prints the receipt ──

def test_owner_performs_the_pass_and_the_cli_prints_the_receipt(tmp_path, monkeypatch, capsys):
    from contextlib import asynccontextmanager
    from hermes_cli import gateway_client, gateway_runtime
    from hermes_cli.gateway_client import rpc_error
    db, authority = _owned(tmp_path, monkeypatch)
    _seed_prunable(db)
    calls = []

    @asynccontextmanager
    async def owner_socket(*_args, **_kwargs):
        connection = _connection(authority)

        async def rpc(method, _timeout=None, **params):
            calls.append((method, params))
            frame = await connection.dispatch({'id': 1, 'method': method, 'params': params})
            if 'error' in frame:
                raise rpc_error(frame['error'])
            return frame['result']
        try:
            yield SimpleNamespace(rpc=rpc)
        finally:
            await connection.close()

    monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint',
                        lambda home, **_: gateway_runtime.GatewayDiscovery('ready', endpoint=object()))
    monkeypatch.setattr(gateway_client, 'connect_gateway', owner_socket)
    try:
        assert sessions_cmd._cmd_maintain(_args()) == 0
        out = capsys.readouterr().out
        # The write is the owner's: this process never opened a second SessionDB at all.
        assert calls and calls[0][0] == 'session.maintenance', calls
        assert db.get_session('old') is None
        assert db.get_meta('last_state_db_maintenance')
        assert 'state.db maintenance — owner' in out and 'retention 90 day(s)' in out, out
    finally:
        db.close()


# ── 3. a foreign holder: the reclaim is refused AS DATA, nothing rewritten ───

@pytest.mark.asyncio
async def test_a_foreign_holder_makes_the_reclaim_refuse_without_rewriting(tmp_path, monkeypatch):
    db, authority = _owned(tmp_path, monkeypatch)
    _seed_prunable(db)
    try:
        with _holder(tmp_path / 'state.db') as proc:
            receipt = (await _dispatch(authority, {}))['result']
        assert receipt['pruned'] == 1, receipt
        assert receipt['vacuumed'] is False, receipt
        assert receipt['vacuum_skipped_holders'] > 0, receipt
        assert any(f'PID {proc.pid}' in entry for entry in receipt['vacuum_skipped_holder_pids']), receipt
    finally:
        db.close()
    # No structural rewrite: the store was never migrated and no page was returned.
    assert receipt['after']['auto_vacuum'] == 0, receipt
    assert receipt['after']['page_count'] == receipt['before']['page_count'], receipt


# ── 4./5. the one-time migration, then a bounded drain ───────────────────────

def test_first_pass_migrates_a_none_store_then_the_second_drains(tmp_path):
    db = SessionDB(db_path=tmp_path / 'state.db')
    cfg = {'min_interval_hours': 0, 'min_vacuum_interval_days': 0}
    try:
        _seed_bulky(db)
        first = run_state_db_maintenance(db, sessions_cfg=cfg, retention_days=0)
        assert first['pruned'] == 1, first
        assert first['reclaim_mode'] == 'migrate', first
        assert first['vacuumed'] is True, first
        assert first['after']['auto_vacuum'] == 2, first['after']

        _seed_bulky(db, 'grow-2')
        calls = []
        real = db.vacuum
        db.vacuum = lambda **kw: (calls.append(kw), real(**kw))[1]
        second = run_state_db_maintenance(db, sessions_cfg=cfg, retention_days=0)
        assert second['pruned'] == 1, second
        assert second['reclaim_mode'] == 'incremental', second
        assert calls == [], f'an incremental drain must never issue VACUUM: {calls!r}'
        assert second['after']['auto_vacuum'] == 2, second['after']
        assert second['max_lock_hold_ms'] is not None, second
    finally:
        db.close()


# ── 6. authorisation is prune's ──────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize('grant', ['viewer', 'remote_operator'])
async def test_only_the_native_local_operator_may_run_the_pass(tmp_path, monkeypatch, grant):
    db, authority = _owned(tmp_path, monkeypatch)
    _seed_prunable(db)
    try:
        frame = await _dispatch(authority, {}, operator=grant == 'remote_operator',
                                native=grant != 'remote_operator')
        assert frame['error']['message'] == 'permission_denied', frame
        assert db.get_session('old') is not None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_a_dry_run_writes_nothing(tmp_path, monkeypatch):
    db, authority = _owned(tmp_path, monkeypatch)
    _seed_prunable(db)
    try:
        receipt = (await _dispatch(authority, {'dry_run': True}))['result']
        assert receipt['dry_run'] is True, receipt
        assert receipt['prune_candidates'] == 1, receipt
        assert db.get_session('old') is not None
        assert db.get_meta('last_state_db_maintenance') is None
    finally:
        db.close()


# ── 7. the doctor overdue guard ──────────────────────────────────────────────

def _profile_home(tmp_path, *, config, record=True, name='lane-a'):
    home = tmp_path / name
    home.mkdir()
    home.joinpath('config.yaml').write_text(config, encoding='utf-8')
    with closing(SessionDB(db_path=home / 'state.db')) as db:
        if record:
            db.set_meta('last_state_db_maintenance',
                        json.dumps({'at': time.time(), 'at_iso': '2026-11-26T20:00:00-0500'}))
    return SimpleNamespace(name=name, path=home)


def test_doctor_reports_a_profile_whose_tick_cannot_run_until_a_gate_is_set(tmp_path, monkeypatch, capsys):
    from hermes_cli import profiles
    home = _profile_home(tmp_path, config='sessions:\n  auto_prune: false\n  auto_archive: false\n')
    monkeypatch.setattr(profiles, 'list_profiles', lambda **_: [home])
    finding = doctor_state._check_state_db_maintenance(False)
    out = capsys.readouterr().out
    assert 'lane-a' in out and 'sessions.auto_prune' in out, out
    assert any('sessions.auto_prune' in issue for issue in finding.manual_issues), finding

    # One gate set: the profile's own tick can run, the record is fresh -> no row.
    (home.path / 'config.yaml').write_text('sessions:\n  auto_prune: true\n  auto_archive: false\n',
                                           encoding='utf-8')
    capsys.readouterr()
    again = doctor_state._check_state_db_maintenance(False)
    out = capsys.readouterr().out
    assert 'lane-a' not in out, out
    assert again.manual_issues == [], again


def test_doctor_reports_a_store_whose_last_pass_is_overdue(tmp_path, monkeypatch, capsys):
    from hermes_cli import profiles
    home = _profile_home(tmp_path, config='sessions:\n  auto_prune: true\n')
    with closing(SessionDB(db_path=home.path / 'state.db')) as db:
        db.set_meta('last_state_db_maintenance', json.dumps({'at': time.time() - 30 * 86400}))
    monkeypatch.setattr(profiles, 'list_profiles', lambda **_: [home])
    doctor_state._check_state_db_maintenance(False)
    out = capsys.readouterr().out
    assert 'lane-a' in out and '30.0 days ago' in out, out
