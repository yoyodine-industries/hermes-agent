"""``worker.status`` is the READ half of the lost-ACK contract.

A worker whose ``worker.adopt``/``worker.register`` ACK is lost must never resend the claim (it is
not idempotent) and must never discard itself either (the owner may have joined it). What it does
is ask the owner what it HOLDS for that execution - ``agent/runtime_session_store._CONFIRM_BY_READ``
- under the same producer claim, and the answer is exactly the receipt the verb itself answers
with. Real SQLite, real process.
"""
import subprocess
import sys
from types import SimpleNamespace

import psutil
import pytest

from hermes_cli.process_identity import worker_birth


@pytest.mark.asyncio
async def test_worker_status_reads_the_row_under_the_same_claim(tmp_path, monkeypatch):
    from gateway.config import Platform
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_contract import SessionActor, SessionRef
    from gateway.session_worker import worker_request
    from hermes_state import SessionDB
    from hermes_state_runtime import RuntimeStoreError, begin_runtime_epoch

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('owned', 'telegram')
    runner = SimpleNamespace(_draining=False, config=SimpleNamespace(multiplex_profiles=False),
                             _adapter_for_source=lambda source: None)
    authority = SessionAuthority(runner, profile_id='p', instance_id='owner', db=db,
                                 epoch=begin_runtime_epoch(db, instance_id='owner'))
    authority.sessions['owned'] = LiveSession(SimpleNamespace(platform=Platform.TELEGRAM, user_id='human'), 'route')
    ref = SessionRef('p', 'owned')
    child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'], stdin=subprocess.PIPE,  # noqa: ASYNC220 -- Popen returns immediately
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def scope(pid):
        return {'profile_id': 'p', 'session_id': 'owned', 'execution_id': f'compute-{pid}', 'generation': 0,
                'pid': pid, 'birth': worker_birth(psutil.Process(pid)), 'secret': 'w' * 32, 'kind': 'compute'}

    def read_scope(pid):
        # ``worker.status`` proves the same claim as adopt and takes no producer-specific field.
        return {key: value for key, value in scope(pid).items() if key != 'kind'}
    interactive = SimpleNamespace(authority=authority, actor=SessionActor(
        'human', 'p', frozenset({'session:create', 'session:read', 'session:submit', 'session:control'}), 't'))
    worker = SimpleNamespace(authority=authority, actor=SessionActor('human', 'p', frozenset({'worker:adopt'}), 't'))
    try:
        # The read is gated like the write it confirms, and it answers `not_found` before any write.
        with pytest.raises(RuntimeStoreError, match='permission_denied'):
            await worker_request(interactive, ref, read_scope(child.pid), operation='status')
        with pytest.raises(RuntimeStoreError, match='not_found'):
            await worker_request(worker, ref, read_scope(child.pid), operation='status')
        registered = await worker_request(worker, ref, scope(child.pid), operation='register')
        assert (await worker_request(worker, ref, read_scope(child.pid), operation='status')) == registered
        adopted = await worker_request(worker, ref, read_scope(child.pid), operation='adopt')
        assert adopted['status'] == 'running'
        read = await worker_request(worker, ref, read_scope(child.pid), operation='status')
        # The confirm-by-read answer IS the adopt receipt: same row, same claim, no digest.
        assert read == adopted and 'adoption_digest' not in read
        # A claim that does not own the row cannot use the read to learn anything about it.
        with pytest.raises(RuntimeStoreError, match='permission_denied'):
            await worker_request(worker, ref, dict(read_scope(child.pid), secret='z' * 32), operation='status')
    finally:
        child.stdin.close()
        child.wait(timeout=5)
        db.close()
