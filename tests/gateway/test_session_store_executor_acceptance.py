"""ACCEPTANCE (real run, not a unit test): session-store persistence off the owner's event loop.

The failure this pins is fleet-wide and measured: one gateway owner serves every profile, a
secondary's save is an RPC to it, and every session-store call — including the ``worker.persist``
receipt a worker waits up to 20 s for — was offloaded to the event loop's DEFAULT executor. When
that shared pool was busy, the owner missed receipts for every lane at once, the workers reported
``worker_receipt_timeout`` -> ``owner_unavailable`` -> ``persistence_degraded``, and the user saw
"Hermes couldn't save this conversation".

This test boots the REAL gateway owner (``python -m gateway.run``) from this checkout and drives
the REAL worker client stack (``WorkerRPC`` + ``RuntimeSessionStore``) against it, so every step
runs through the production paths: control socket, ticket, authority, epoch, admission, journal,
``worker.persist`` receipt, the session-store executor and a real SQLite commit.

The executor is blocked the way the design allows — with a REAL slow lock: an external connection
holds ``BEGIN IMMEDIATE`` on the owner's ``state.db``, so the owner's writes cannot get the SQLite
write lock and must wait it out inside ``_execute_write`` (patience 20 s). Nothing is injected into
the gateway to simulate slowness.

Proven here:
  (a) a worker turn's save still completes and its receipt still arrives;
  (b) the owner's event loop keeps serving other lanes' reads while the executor is wedged;
  (c) no receipt is answered before the commit: none arrives while the write lock is held, and each
      receipt lands after the lock is released;
  (d) a saturated executor refuses BY NAME (``session_store_saturated``) with the queue depth and
      the wait, immediately, instead of hanging — and the production outbox drain recovers it.
Plus the approved lever is honoured: with ``gateway.write_sessions_json: false`` the legacy mirror
is never written and ``state.db`` is the only primary.
"""
import asyncio
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from tests.gateway.fixtures.local_recovery_probe import child_env, daemon, rpc, websocket

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / 'tests/gateway/fixtures/session_store_acceptance_worker.py'

# Small enough that the backpressure is reachable deliberately; the gateway logs the sizing.
WORKERS, QUEUE = 1, 1


def _events(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _wait_for(events, session_id, event, timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for row in _events(events):
            if row['session_id'] == session_id and row['event'] == event:
                return row
        time.sleep(0.05)
    raise AssertionError(f'timed out waiting for {event} ({session_id}): {_events(events)}')


def test_gateway_keeps_saving_and_serving_while_the_session_store_executor_is_wedged(tmp_path):
    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir(mode=0o700)
    user.mkdir()
    (home / 'config.yaml').write_text(json.dumps({
        # The approved mitigation lever: the legacy mirror is OFF, state.db is the only primary.
        'gateway': {'multiplex_profiles': False, 'write_sessions_json': False,
                    'session_store_executor': 'dedicated'},
        'model': {'provider': 'custom', 'default': 'inert', 'base_url': 'http://127.0.0.1:9/v1'},
        'auxiliary': {'title_generation': {'enabled': False}},
        'platform_toolsets': {'cli': []},
    }))
    env = child_env()
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(ROOT),
               OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL='http://127.0.0.1:9/v1',
               PYTHONUNBUFFERED='1',
               HERMES_SESSION_STORE_WORKERS=str(WORKERS), HERMES_SESSION_STORE_QUEUE=str(QUEUE),
               HERMES_SESSION_STORE_DEADLINE_S='15')
    events = tmp_path / 'worker-events.jsonl'
    clients = {}

    async def create_sessions(desc, count):
        ids = []
        async with websocket(home, desc) as ws:
            for index in range(count):
                reply = await rpc(ws, 'session.create', request_id=f'accept-{index}', source='cli')
                assert 'result' in reply, reply
                ids.append(reply['result']['session_id'])
        return ids

    async def probe_reads_while_wedged(desc, until, samples):
        async with websocket(home, desc) as ws:
            while time.monotonic() < until:
                started = time.monotonic()
                reply = await rpc(ws, 'session.list')
                samples.append((time.monotonic() - started) * 1000)
                assert 'result' in reply, reply
                await asyncio.sleep(0.1)

    def spawn(label, session_id):
        go = tmp_path / f'go-{label}'
        proc = subprocess.Popen(
            [sys.executable, str(WORKER), '--home', str(home), '--session-id', session_id,
             '--execution-id', f'accept-{label}', '--marker', f'MARK-{label}',
             '--go', str(go), '--events', str(events)],
            cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        clients[label] = {'proc': proc, 'go': go, 'session': session_id, 'marker': f'MARK-{label}'}
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if any(row['event'] == 'ready' and row['pid'] == proc.pid for row in _events(events)):
                return
            time.sleep(0.05)
        output = proc.stdout.read() if proc.stdout is not None else ''
        raise AssertionError(f'worker {label} never registered: {output}')

    with daemon(ROOT, home, env, barrier=False) as (_owner, desc):
        sessions = asyncio.run(create_sessions(desc, 4))
        for label, session_id in zip('ABCD', sessions):
            spawn(label, session_id)

        # ---- baseline: an ordinary save, nothing blocked --------------------------------
        clients['A']['go'].write_text('go')
        baseline = _wait_for(events, clients['A']['session'], 'persist_receipt')
        assert baseline['at'] > _wait_for(events, clients['A']['session'], 'persist_sent')['at']

        # ---- wedge the executor with a real slow lock -----------------------------------
        blocker = sqlite3.connect(str(home / 'state.db'), isolation_level=None, timeout=30)
        try:
            blocker.execute('PRAGMA busy_timeout=30000')
            blocker.execute('BEGIN IMMEDIATE')
            lock_held = time.time()
            samples = []

            async def blocked_window():
                probe = asyncio.ensure_future(probe_reads_while_wedged(desc, time.monotonic() + 4.6, samples))
                # B takes the single executor worker and blocks on the lock; C fills the single
                # queue slot; D must be refused by the saturated executor.
                clients['B']['go'].write_text('go')
                await asyncio.sleep(0.8)
                clients['C']['go'].write_text('go')
                await asyncio.sleep(0.8)
                clients['D']['go'].write_text('go')
                await probe

            asyncio.run(blocked_window())
            lock_released = time.time()
        finally:
            blocker.execute('ROLLBACK')
            blocker.close()

        for label in 'BCD':
            clients[label]['proc'].wait(timeout=120)

    # ---- (c) the baseline receipt is post-commit, and B/C receipts waited out the lock ---
    for label in ('B', 'C'):
        rows = [r for r in _events(events) if r['session_id'] == clients[label]['session']]
        receipts = [r for r in rows if r['event'] == 'persist_receipt']
        assert len(receipts) == 1, (label, rows)
        # (a) the save completed and the receipt arrived ...
        assert receipts[0]['at'] >= lock_released, (label, receipts[0]['at'], lock_released)
        # ... only after the commit could happen: the write lock forbade one before the release.
        refused = [r for r in rows if r['event'] == 'persist_refused']
        assert not refused, (label, refused)

    # ---- (d) the saturated queue refused by name, with the depth and the wait -----------
    d_rows = [r for r in _events(events) if r['session_id'] == clients['D']['session']]
    refusals = [r for r in d_rows if r['event'] == 'persist_refused']
    assert refusals and refusals[0]['code'] == 'session_store_saturated', d_rows
    refusal = refusals[0]['refusal'] or {}
    assert refusal.get('field') == 'session_store_executor', refusal
    assert f'queue_depth={QUEUE}' in refusal.get('detail', ''), refusal
    assert 'wait_ms=' in refusal.get('detail', ''), refusal
    assert refusals[0]['at'] >= lock_held, refusals[0]
    # The production recovery path drains the retained outbox once the pool frees up.
    flush = next(r for r in d_rows if r['event'] == 'flush_done')
    assert flush['settled'] is True, d_rows

    # ---- (b) the loop kept serving other lanes' reads while the executor was wedged -----
    assert len(samples) >= 20, samples
    assert max(samples) < 1000, sorted(samples)[-5:]

    # ---- the mirror stays off; state.db is the only primary -----------------------------
    mirror = home / 'sessions' / 'sessions.json'
    with sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True) as db:
        stored = db.execute("SELECT COUNT(*) FROM messages WHERE content LIKE '%MARK-%'").fetchone()[0]
        routing = db.execute('SELECT COUNT(*) FROM gateway_routing').fetchone()[0]
    assert not mirror.exists(), 'write_sessions_json: false must leave no mirror'
    assert stored >= 8, stored      # four workers x (marker + its acknowledgement)
    assert routing >= 4, routing
