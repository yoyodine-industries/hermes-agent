"""Explicit worker persistence facade; never opens the canonical SQLite store.

This first operation family is deliberately NOT a complete SessionDB substitute.
No existing cron/child/compute consumer is switched until its inventory is covered.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
from pathlib import Path
import queue
import random
import sys
import threading
import time
from hermes_dir_acl import make_user_dir

logger = logging.getLogger(__name__)

_worker_process = False

# One worker.persist receipt, and the total patience for re-asking it.
#
# A missed receipt is not a lost write. The owner stores one receipt per
# (execution, sequence, payload digest) and answers an exact replay with the stored result
# (``apply_worker_receipt``), so re-asking the SAME request is idempotent even when the first
# copy is still being applied. What the single 20s budget actually measures is whether the
# owner's event loop got round to the socket at all: a stalled loop (host load, a long
# owner-side call) misses it while the write itself commits and the ACK sits in flight.
#
# Before this, ONE miss made the store's failure sticky for the rest of the turn, and the
# close-time flush turned it into ``worker_receipt_timeout``: the run died at the save step,
# the owner recorded ``worker_lost``, and the card was re-dispatched as if nothing had run.
# Re-asking inside a bounded window keeps that trade cheap.
RECEIPT_BUDGET_SECONDS = 20.0
RECEIPT_BACKOFF_SECONDS = (1.0, 3.0, 8.0, 15.0)
# Only a method whose replay the owner dedupes may be re-asked after a missed ACK.
_REPLAY_SAFE_METHODS = frozenset({'worker.persist'})

# Repeating a mutation and confirming its outcome are different questions. These verbs are a
# producer claim, so a blind resend is not idempotent - but the owner already HOLDS the row they
# write, so a lost ACK is resolvable by ASKING rather than giving up. Value = the status strings
# that prove the write landed; the read is ``worker.status`` (read-only, same claim, same owner).
_CONFIRM_BY_READ = {
    'worker.adopt': ('running',),
    'worker.register': ('registered', 'running'),
}
_CONFIRM_READ_METHOD = 'worker.status'
_CONFIRM_READ_SCOPE = ('profile_id', 'session_id', 'execution_id', 'generation', 'pid', 'birth', 'secret')
# Failures that mean "the owner is busy", never "this store is broken": a flush reports these
# as False and keeps the journal (the outbox a later reopen replays), exactly like the
# in-process SessionDB contract ("False on timeout ... Never raises").
_TRANSIENT_RECEIPT_FAILURES = frozenset({'worker_receipt_timeout', 'pending_receipt', 'owner_unavailable'})


def _env_seconds(name, default):
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


RECEIPT_PATIENCE_SECONDS = _env_seconds('HERMES_WORKER_RECEIPT_PATIENCE', 75.0)

# A worker asks a descriptor the multiplexer rewrites on every owner restart, so 'not ready'
# is routinely TRANSIENT - and a save that dies on it was never attempted (nothing is lost,
# it is simply not written, and the death then costs the caller its whole retry budget).
# Waited out with a bounded, jittered backoff: the caller's write budget pays for the
# patience, so it stays short, and a genuinely absent owner still surfaces as
# owner_unavailable naming the attempts instead of hanging.
DISCOVERY_PATIENCE_SECONDS = _env_seconds('HERMES_WORKER_DISCOVERY_PATIENCE', 20.0)
DISCOVERY_BACKOFF_SECONDS = (0.05, 0.1, 0.2, 0.4, 0.8, 1.5, 2.0)


def _receipt_is_transient(failure):
    """True when a failure says the owner was busy, not that the store is unusable."""
    return isinstance(failure, str) and (
        failure in _TRANSIENT_RECEIPT_FAILURES
        or failure.startswith(('ConnectionClosed', 'TimeoutError', 'OSError', 'ConnectionError')))


def is_worker_process():
    """A successful self-registration makes this interpreter compute-only.

    Process-scoped (not ContextVar): import-time recovery and fresh helper threads
    must not regain owner-ledger access. This is cooperative runtime ownership,
    not OS confinement or an authorization credential.
    """
    return _worker_process



def apply_row_annotations(messages, annotations):
    """Mirror the owner's in-place row stamps onto the worker's dicts (None = the owner popped it)."""
    for message, annotation in zip(messages, annotations, strict=True):
        for key, value in annotation.items():
            if value is None:
                message.pop(key, None)
            else:
                message[key] = value

class WorkerPersistenceError(RuntimeError):
    pass


def _caller_summary(limit=8):
    """module:function:line of the frames above this store (its own frames skipped), never
    locals or argument values: best-effort callers swallow the guard's raise (#139029)."""
    frame, frames = sys._getframe(2), []
    while frame is not None and len(frames) < limit:
        module = frame.f_globals.get('__name__', '?')
        if not module.startswith('agent.runtime_session_'):
            frames.append(f'{module}:{frame.f_code.co_name}:{frame.f_lineno}')
        frame = frame.f_back
    return ' <- '.join(frames)


def call_off_event_loop(fn, *args, **kwargs):
    """Call ``fn`` here, or on a worker thread when this thread is running an event loop.

    For synchronous best-effort writers that can run inside a callback Relay drives on its private
    loop (provider/tool callbacks): there ``WorkerRPC`` refuses the call (``synchronous_rpc_on_event_loop``,
    sticky for the rest of the turn; #139029). The callback already blocks that loop, so waiting for the
    thread costs it nothing; the context is copied so session-scoped state survives the hop, and errors
    re-raise here. Async code awaits ``asyncio.to_thread`` instead.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return fn(*args, **kwargs)
    context = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='worker-persist-off-loop') as pool:
        return pool.submit(context.run, fn, *args, **kwargs).result()


class _OwnerLink:
    """One owner WebSocket and the only thread that reads it. Events the owner broadcasts to every
    transport are discarded as they arrive, idle or not: left queued, websockets' bounded receive
    buffer pauses socket reads, which also stalls pongs and the close handshake. The reply to the
    one outstanding request id goes to its caller; a closed connection goes to it as the exception."""
    def __init__(self, ws):
        self.ws = ws
        self.awaiting = None
        self.replies = queue.SimpleQueue()
        self.reader = threading.Thread(target=self._read, name='worker-rpc-reader', daemon=True)
        self.reader.start()

    def _read(self):
        try:
            while True:
                try:
                    frame = json.loads(self.ws.recv())
                except ValueError:
                    continue
                if isinstance(frame, dict) and self.awaiting is not None and frame.get('id') == self.awaiting:
                    self.replies.put(frame)
        except Exception as exc:  # health: allow BLE001 -- handed to the caller, which raises it (ConnectionClosed included)
            self.replies.put(exc)

    def send(self, request_id, message):
        self.awaiting = request_id
        self.ws.send(message)

    def receive(self, timeout):
        try:
            reply = self.replies.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError('worker_receipt_timeout') from None
        if isinstance(reply, BaseException):
            raise reply
        return reply


class WorkerRPC:
    """Bounded authenticated calls to an existing owner; never starts a daemon.

    No owner-loop synchronous self-RPC or SQLite fallback is permitted.
    One connection serves every call (serialized by ``lock``, correlated by id); it is
    discovered, identified and ticketed on connect exactly as each call once was.
    """
    def __init__(self, home):
        self.home = Path(home).resolve()
        self.lock = threading.Lock()
        self.endpoint = None
        self.link = None
        self.request_id = 0

    def _connection(self):
        if self.link is not None:
            if self.link.reader.is_alive():
                return self.link
            # The owner closed it while idle, before any send: redial with a fresh ticket.
            self._drop()
        from hermes_cli.gateway_client import _session_ticket, gateway_ws_target
        from hermes_cli.gateway_runtime_discovery import query_identify
        from websockets.sync.client import connect
        # A served secondary has no socket; its multiplexer's descriptor names it.
        self._await_owner()
        from hermes_cli.gateway_runtime import control_home_for
        endpoint = self.endpoint
        descriptor = query_identify(control_home_for(self.home, endpoint), timeout=5)
        if descriptor.get('pid') == os.getpid():
            raise WorkerPersistenceError('synchronous_self_rpc')
        ticket = _session_ticket(self.home, endpoint, purpose='worker-adoption')
        url, protocols = gateway_ws_target(endpoint, ticket)
        # The owner is loopback, like connect_gateway's peer: an inherited HTTPS_PROXY must not carry it.
        # No library keepalive: its pong deadline would run independently of (and could fail) a call
        # inside its receipt budget; that budget and the observed close are the liveness checks.
        self.link = _OwnerLink(connect(url, subprotocols=protocols, open_timeout=5, close_timeout=1,
                                       max_size=8 * 1024 * 1024, proxy=None, ping_interval=None))
        return self.link

    def _await_owner(self):
        """Wait out a transient descriptor, bounded, and NAME the owner when it never comes.

        The descriptor is rewritten whenever the owner restarts or the multiplexer
        re-registers, so 'not ready' is routinely momentary. This used to be asked exactly
        once: a worker that hit the rewrite died on a save it had not yet attempted, and
        that death then spent the caller's retry budget on a write SQLite never saw.
        """
        from hermes_cli.gateway_runtime import discover_gateway_endpoint
        started = time.monotonic()
        attempt = 0
        while True:
            discovery = discover_gateway_endpoint(self.home, timeout=5)
            if discovery.state == 'ready' and discovery.endpoint is not None:
                self.endpoint = discovery.endpoint
                if attempt:
                    logger.info('owner became ready after %d discovery attempt(s) in %.2fs',
                                attempt + 1, time.monotonic() - started)
                return discovery
            if discovery.state == 'draining' and self.endpoint is not None:
                return discovery
            backoff = DISCOVERY_BACKOFF_SECONDS[min(attempt, len(DISCOVERY_BACKOFF_SECONDS) - 1)]
            # A fleet restarting together must not re-ask the descriptor in lockstep.
            backoff *= 0.75 + 0.5 * random.random()
            attempt += 1
            if time.monotonic() + backoff > started + DISCOVERY_PATIENCE_SECONDS:
                logger.warning(
                    'owner_unavailable after %d discovery attempt(s) in %.2fs (last state %r, '
                    'patience %.0fs): the owner never became ready, so this write was NOT '
                    'attempted - nothing was lost by stopping here',
                    attempt, time.monotonic() - started, discovery.state,
                    DISCOVERY_PATIENCE_SECONDS)
                raise WorkerPersistenceError('owner_unavailable')
            logger.info('owner not ready (state %r); re-asking discovery in %.2fs (attempt %d)',
                        discovery.state, backoff, attempt)
            time.sleep(backoff)

    def _drop(self):
        link, self.link = self.link, None
        if link is not None:
            with contextlib.suppress(OSError):  # a broken socket is already gone
                link.ws.close()

    def close(self):
        with self.lock:
            self._drop()

    def __call__(self, method, **params):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            logger.warning('synchronous_rpc_on_event_loop: %s on thread %s; caller: %s',
                           method, threading.current_thread().name, _caller_summary())
            raise WorkerPersistenceError('synchronous_rpc_on_event_loop')
        with self.lock:
            self.request_id += 1
            request_id = self.request_id
            message = json.dumps({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
            # Only a receipt the owner dedupes by (execution, sequence, digest) may be re-asked.
            replay_safe = method in _REPLAY_SAFE_METHODS and isinstance(params.get('sequence'), int)
            confirmable = method in _CONFIRM_BY_READ
            retried = False
            try:
                link = self._send(self._connection(), request_id, message)
                started = time.monotonic()
                deadline = started + RECEIPT_PATIENCE_SECONDS
                attempt = 0
                while True:
                    sent = time.monotonic()
                    try:
                        response = link.receive(RECEIPT_BUDGET_SECONDS)
                        break
                    except TimeoutError:
                        waited = time.monotonic() - sent
                        self._drop()
                        if confirmable:
                            # A mutation may not be repeated blind, but its OUTCOME is not
                            # unknowable: the owner holds the row this verb writes. Ask it, and
                            # never discard a joined, working worker because its ACK was slow.
                            committed, row = self._confirm_by_read(method, params, deadline)
                            if committed:
                                row = row or {}
                                logger.warning(
                                    'worker_receipt_confirmed_by_read after %.1fs: the owner did not ACK '
                                    '%s but holds this execution as %s (owner_epoch %s); proceeding on '
                                    'that row, the write is not lost', waited, method,
                                    row.get('status'), row.get('owner_epoch'))
                                return self._settle(method, params, row)
                            if retried:
                                raise TimeoutError('worker_receipt_timeout') from None
                            # The owner answered and the write genuinely did not land: the one case
                            # where re-issuing the call is idempotent, and it happens exactly once.
                            retried = True
                            attempt += 1
                            logger.warning(
                                'worker_receipt_timeout after %.1fs: the owner holds no committed %s for '
                                'this execution (read says not applied); retrying the call once, '
                                'never blindly', waited, method)
                            self.request_id += 1
                            request_id = self.request_id
                            message = json.dumps({'jsonrpc': '2.0', 'id': request_id,
                                                  'method': method, 'params': params})
                            link = self._send(self._connection(), request_id, message)
                            continue
                        backoff = RECEIPT_BACKOFF_SECONDS[min(attempt, len(RECEIPT_BACKOFF_SECONDS) - 1)]
                        attempt += 1
                        if not replay_safe or time.monotonic() + backoff > deadline:
                            logger.warning(
                                'worker_receipt_timeout after %.1fs (attempt %d, %.1fs total): the owner did '
                                'not ACK %s; this receipt is lost, the write it names is not',
                                waited, attempt, time.monotonic() - started, method)
                            raise
                        # A busy owner is waited out, then re-asked: the replay resolves the receipt
                        # the first copy committed. Never a blind resend - only an idempotent one.
                        logger.warning(
                            'worker_receipt_timeout after %.1fs (attempt %d): owner busy (budget %.0fs, '
                            'patience %.0fs); re-asking %s in %.1fs',
                            waited, attempt, RECEIPT_BUDGET_SECONDS, RECEIPT_PATIENCE_SECONDS, method, backoff)
                        time.sleep(backoff)
                        link = self._send(self._connection(), request_id, message)
            except BaseException:
                # Every attempt is spent: drop the link and surface the error; the next call redials.
                self._drop()
                raise
            if 'error' in response:
                raise WorkerPersistenceError(response['error'].get('message', 'persistence_failed'))
            return self._settle(method, params, response['result'])

    def _settle(self, method, params, result):
        """One exit for an ACKed call and for one confirmed by read, so both bind the process the
        same way (``_worker_process`` gates the owner-side launch path)."""
        if method in ('worker.register', 'worker.adopt') and params.get('pid') == os.getpid():
            global _worker_process
            _worker_process = True
        return result

    def _confirm_by_read(self, method, params, deadline):
        """Resolve a lost ACK for a mutation by ASKING the owner, never by resending it.

        Returns ``(True, row)`` when the owner already holds the row this verb writes, and
        ``(False, None)`` when it answered and the write did not land. Raises when the read
        itself cannot be answered inside the receipt patience: an owner that answers neither the
        call nor the read is a ``receipt_timeout`` (a cause of harness loss), never a guess about
        what it committed.
        """
        committed = _CONFIRM_BY_READ[method]
        read_params = {key: params[key] for key in _CONFIRM_READ_SCOPE if key in params}
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(
                    'worker_receipt_read_unanswered: the owner answered neither %s nor %s inside the '
                    'receipt patience; the outcome is not knowable from here (receipt_timeout)',
                    method, _CONFIRM_READ_METHOD)
                raise TimeoutError('worker_receipt_timeout') from None
            self.request_id += 1
            request_id = self.request_id
            message = json.dumps({'jsonrpc': '2.0', 'id': request_id,
                                  'method': _CONFIRM_READ_METHOD, 'params': read_params})
            link = self._send(self._connection(), request_id, message)
            try:
                response = link.receive(min(RECEIPT_BUDGET_SECONDS, remaining))
            except TimeoutError:
                # A read is idempotent, so it is re-asked inside the same patience.
                self._drop()
                time.sleep(min(RECEIPT_BACKOFF_SECONDS[0], max(remaining, 0.0)))
                continue
            if 'error' in response:
                reason = response['error'].get('message', 'persistence_failed')
                if reason in ('not_found', 'stale_generation'):
                    # The owner answered: it holds no row this claim may have written.
                    logger.warning('worker_receipt_read_not_applied: the owner holds no %s row for this '
                                   'execution (%s); the write did not land', method, reason)
                    return False, None
                raise WorkerPersistenceError(reason)
            row = response['result'] or {}
            if row.get('status') in committed:
                return True, row
            logger.warning('worker_receipt_read_not_applied: the owner reports this execution as %r, '
                           'not %r; the write did not land', row.get('status'), committed)
            return False, None

    def _send(self, link, request_id, message):
        """Send once; a link the owner closed before the frame was written is redialed once."""
        from websockets.exceptions import ConnectionClosed
        try:
            link.send(request_id, message)
            return link
        except ConnectionClosed:
            # websockets raises this from send only before a complete frame is written (closed
            # on entry, or the socket write failed): the owner closed the link after the idle
            # check and never saw this request, so redial and send it once more.
            self._drop()
            link = self._connection()
            link.send(request_id, message)
            return link


from agent.runtime_session_compression import RuntimeSessionCompressionMixin


from agent.runtime_session_lifecycle import RuntimeSessionLifecycleMixin


class RuntimeSessionStore(RuntimeSessionCompressionMixin, RuntimeSessionLifecycleMixin):
    """Synchronous durable results with a private byte-bounded retry journal.

    A failed write remains pending, and failure is sticky until explicit retry or
    adoption. Local queue acceptance is never returned as canonical commit.
    Pending journals live outside age-pruned cache trees.
    """
    # The process's async-delegation ledger belongs to the adopted execution, never a reserved child.
    binds_delegation_ledger = True

    def __init__(self, rpc, scope, outbox_dir, adopted=None, *, max_bytes=4 * 1024 * 1024):
        self.rpc = rpc
        self.scope = dict(scope)
        self.max_bytes = max_bytes
        self.lock = threading.RLock()
        self.failure = None
        self.path = Path(outbox_dir) / 'pending.json'
        make_user_dir(self.path.parent, mode=0o700, parents=True, exist_ok=True)
        if self.path.parent.is_symlink() or self.path.is_symlink():
            raise WorkerPersistenceError('unsafe_outbox')
        os.chmod(self.path.parent, 0o700)
        from gateway.status import _try_acquire_file_lock
        lock_path = self.path.parent / 'owner.lock'
        if lock_path.is_symlink():
            raise WorkerPersistenceError('unsafe_outbox')
        self._outbox_owner = lock_path.open('a+', encoding='utf-8')
        os.chmod(lock_path, 0o600)
        if not _try_acquire_file_lock(self._outbox_owner):
            self._outbox_owner.close()
            raise WorkerPersistenceError('outbox_in_use')
        try:
            if self.path.exists():
                if self.path.stat().st_size > max_bytes:
                    raise WorkerPersistenceError('outbox_full')
                self.journal = json.loads(self.path.read_text(encoding="utf-8-sig"))
                reconciled = self.journal['scope'] != self.scope
                if reconciled:
                    self._reconcile_adopted(adopted)
            else:
                reconciled = False
                self.journal = {'scope': self.scope, 'next_sequence': 1, 'pending': []}
                self._save(self.journal)
            if self.journal['pending']:
                self.retry_pending()
            if reconciled and self.scope != dict(scope):
                # Replay must land the journal exactly on the adopted assignment (a pending
                # publication moves it to the child); anything else is not this successor's outbox.
                raise WorkerPersistenceError('outbox_scope_mismatch')
            if is_worker_process() and self.binds_delegation_ledger:
                from tools.async_delegation_worker import bind_worker_delegation_store
                bind_worker_delegation_store(self)
        except Exception:
            self._outbox_owner.close()
            raise

    def _reconcile_adopted(self, adopted):
        """Carry a retained outbox across verified owner replacement.

        ``adopted`` is the separately authenticated ``worker.adopt`` result for the requested
        scope; never self-adopt. Only the owner epoch (forward) and the physical target may
        differ from the journal (a same-epoch restart can still find the target rotated).
        Pending entries replay under the journal's own target with the adopted epoch, so an
        exact lost-ACK retry resolves its original receipt instead of creating a second one;
        the constructor then requires the replayed scope to equal the adopted one.
        """
        old, new = self.journal['scope'], self.scope
        fixed = set(new) - {'epoch', 'session_id'}
        if (not isinstance(adopted, dict) or set(old) != set(new)
                or any(old[key] != new[key] for key in fixed)
                or adopted.get('execution_id') != new.get('execution_id')
                or adopted.get('generation') != new.get('generation')
                or adopted.get('session_id') != new.get('session_id')
                or adopted.get('owner_epoch') != new.get('epoch') or adopted.get('status') != 'running'
                or type(old.get('epoch')) is not int or type(new.get('epoch')) is not int
                or old['epoch'] > new['epoch']
                # Only a pending publication can still move the target; replay must prove it.
                or (old['session_id'] != new['session_id'] and not any(
                    entry['operation'] == 'compression.publish' for entry in self.journal['pending']))):
            raise WorkerPersistenceError('outbox_scope_mismatch')
        candidate = dict(self.journal, scope=dict(old, epoch=new['epoch']))
        self._save(candidate)
        self.scope = candidate['scope']
        self.journal = candidate

    def _save(self, journal):
        if self._outbox_owner.closed:
            raise WorkerPersistenceError('outbox_closed')
        encoded = json.dumps(journal, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode()
        if len(encoded) > self.max_bytes:
            self.failure = 'outbox_full'
            raise WorkerPersistenceError(self.failure)
        from utils import atomic_write_bytes
        # Owner-only journal (the dir is 0700 too); the rename and its directory entry are durable.
        atomic_write_bytes(self.path, encoded, tmp_prefix='.pending-', mode=0o600, fsync_dir=True)

    def _session(self, session_id):
        if session_id != self.scope['session_id']:
            raise WorkerPersistenceError('permission_denied')

    def _apply(self, operation, payload):
        with self.lock:
            if self.failure:
                raise WorkerPersistenceError(self.failure)
            if self.journal['pending']:
                raise WorkerPersistenceError('pending_receipt')
            entry = {'sequence': self.journal['next_sequence'], 'operation': operation, 'payload': payload}
            candidate = json.loads(json.dumps(self.journal))
            candidate['pending'].append(json.loads(json.dumps(entry, allow_nan=False)))
            candidate['next_sequence'] += 1
            try:
                self._save(candidate)
            except Exception as exc:
                self.failure = str(exc)
                raise
            self.journal = candidate
            return self.retry_pending()[0]

    def retry_pending(self):
        with self.lock:
            results = []
            try:
                while self.journal['pending']:
                    entry = self.journal['pending'][0]
                    result = self.rpc('worker.persist', **self.scope, **entry)
                    candidate = dict(self.journal, pending=self.journal['pending'][1:])
                    candidate = self._compression_receipt_journal(candidate, result)
                    self._save(candidate)
                    self.scope = candidate['scope']
                    self.journal = candidate
                    results.append(result)
            except Exception as exc:
                self.failure = str(exc)
                raise
            self.failure = None
            return results

    def adopt(self, epoch):
        """Install only a separately verified worker.adopt result; never self-adopt."""
        with self.lock:
            candidate = dict(self.journal, scope=dict(self.scope, epoch=epoch))
            self._save(candidate)
            self.scope = candidate['scope']
            self.journal = candidate
            self.failure = None

    def append_messages_batch(self, session_id, messages, compression_lock_holder=None,
                              turn_lease_holder=None, chunk_rows=None, turn_lease_ttl_seconds=300.0):
        self._session(session_id)
        if chunk_rows is not None:
            raise WorkerPersistenceError('unsupported_operation')
        if compression_lock_holder is not None or turn_lease_ttl_seconds != 300.0:
            return self._append_compression_messages(session_id, messages, compression_lock_holder,
                turn_lease_holder, turn_lease_ttl_seconds)
        result = self._apply('transcript.append', {'messages': messages, 'turn_lease_holder': turn_lease_holder})
        apply_row_annotations(messages, result['annotations'])
        return result['count']

    def try_acquire_session_turn_lease(self, session_id, holder, *, ttl_seconds=300.0, patience_s=None):
        self._session(session_id)
        return self._apply('turn.acquire', {'holder': holder, 'ttl_seconds': ttl_seconds})['value']

    def acquire_session_turn_lease(self, session_id, holder, *, ttl_seconds=300.0,
            wait_seconds=1800.0, poll_interval_seconds=1.0, on_wait=None,
            wait_notice_interval_seconds=15.0, should_abort=None, acquire_patience_s=0.5,
            on_contended=None):
        # Reuse only the local polling orchestrator, not the SQLite mixin surface. Same signature
        # as the owner's (``on_contended``: a busy-database notice; the RPC try never raises it).
        from hermes_state_compression import SessionCompressionMixin
        return SessionCompressionMixin.acquire_session_turn_lease(self, session_id, holder,
            ttl_seconds=ttl_seconds, wait_seconds=wait_seconds,
            poll_interval_seconds=poll_interval_seconds, on_wait=on_wait,
            wait_notice_interval_seconds=wait_notice_interval_seconds,
            should_abort=should_abort, acquire_patience_s=acquire_patience_s,
            on_contended=on_contended)

    def refresh_session_turn_lease(self, session_id, holder, *, ttl_seconds=300.0, patience_s=None):
        self._session(session_id)
        return self._apply('turn.renew', {'holder': holder, 'ttl_seconds': ttl_seconds})['value']

    def release_session_turn_lease(self, session_id, holder):
        # DurableTurnLease retains its admission target across physical rotation.
        self._apply('turn.cleanup', {'target': session_id, 'holder': holder})

    def queue_token_counts(self, session_id, **usage):
        self._session(session_id)
        self._apply('usage.main', usage)

    def record_auxiliary_usage(self, session_id, task, **usage):
        self._session(session_id)
        self._apply('usage.auxiliary', dict(usage, task=task))

    def flush_token_counts(self, timeout=5.0):
        """Block until everything this turn wrote is acknowledged, inside a bounded window.

        Same contract as the in-process store ("False on timeout ... Never raises"): a busy
        owner is waited out with a measured backoff and then REPORTED, never raised, so a
        completed turn is not turned into a ``worker_lost`` by a receipt that is merely late.
        Whatever stays unacknowledged stays in the journal - the outbox a reopen or an adopt
        replays - and ``close`` leaves it on disk intact.
        """
        with self.lock:
            if self.failure or self.journal['pending']:
                self._drain_retained_receipts(timeout)
            if self.failure and not _receipt_is_transient(self.failure):
                raise WorkerPersistenceError(self.failure)
            if self.failure or self.journal['pending']:
                logger.warning('persistence_degraded: %s (%d retained); outbox kept at %s',
                               self.failure or 'pending_receipt', len(self.journal['pending']), self.path)
                return False
            return True

    def _drain_retained_receipts(self, timeout):
        """Re-ask the retained journal inside ``timeout`` of measured backoff.

        A receipt timeout means the owner was too busy to answer, not that the store is
        broken: replay is idempotent, so waiting is always safe. Past the window - or on a
        failure that is not about a busy owner - this returns False and leaves the journal
        on disk; the caller classifies it.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        attempt = 0
        while True:
            if not self.journal['pending']:
                # Nothing to replay: this failure is not a late receipt, and re-asking an empty
                # journal would clear it (retry_pending's success path) and lose the diagnosis.
                return False
            attempt += 1
            try:
                self.retry_pending()
            except Exception:
                if not _receipt_is_transient(self.failure):
                    return False
            else:
                if attempt > 1:
                    logger.info('persistence_receipt_recovered after %d attempt(s)', attempt)
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.25 * 2 ** (attempt - 1), 2.0, remaining))

    def get_session_title(self, session_id):
        return self.get_session(session_id).get('title')

    # Transcript-tail reads the agent core makes on its session_db (failed-turn boundary, in-place
    # compaction coverage); a missing one raised AttributeError inside a best-effort guard.
    def latest_conversation_role(self, session_id):
        self._session(session_id)
        return self._apply('session.tail_role', {})['value']

    def get_message_role(self, session_id, row_id):
        self._session(session_id)
        return self._apply('session.row_role', {'row_id': int(row_id)})['value']

    def get_compression_failure_cooldown_row(self, session_id):
        row = self.get_session(session_id)
        return {'session_exists': True, 'cooldown_until': row.get('compression_failure_cooldown_until'),
                'error': row.get('compression_failure_error')}

    def get_compression_failure_cooldown(self, session_id):
        import time
        now = time.time()
        row = self.get_compression_failure_cooldown_row(session_id)
        deadline = row['cooldown_until']
        if deadline is None or float(deadline) <= now:
            return None
        return {'cooldown_until': float(deadline), 'remaining_seconds': float(deadline) - now,
                'error': row['error']}

    def _session_number(self, session_id, column, cast, zero):
        row = self.get_session(session_id)
        try:
            return max(zero, cast(row.get(column) or zero))
        except (TypeError, ValueError):
            return zero

    def get_compression_fallback_streak(self, session_id):
        return self._session_number(session_id, 'compression_fallback_streak', int, 0)

    def get_compression_ineffective_count(self, session_id):
        return self._session_number(session_id, 'compression_ineffective_count', int, 0)

    def get_compression_recovery_deadline(self, session_id):
        return self._session_number(session_id, 'compression_recovery_deadline', float, 0.0)

    def get_session_model_config_value(self, session_id, key, default=None):
        from hermes_state_sessions import _parse_model_config
        return _parse_model_config(self.get_session(session_id).get('model_config')).get(key, default)

    def update_conversation_model_config_value(self, session_id, key, update):
        # A worker owns exactly its session row, so the read-modify-write is that row's; the owner applies
        # the patch in its own transaction (``None`` removes the key, as on SessionDB).
        self._session(session_id)
        self.patch_session_model_config(session_id, {key: update(self.get_session_model_config_value(session_id, key))})
        return session_id

    def session_ids_with_model_config_key(self, key):
        # The scan a worker can answer: its own session (it may read no other row).
        sid = self.scope['session_id']
        return [sid] if self.get_session_model_config_value(sid, key) is not None else []

    def update_system_prompt(self, session_id, system_prompt):
        self._session(session_id)
        self._apply('session.prompt', {'system_prompt': system_prompt})

    def patch_session_model_config(self, session_id, patch):
        self._session(session_id)
        self._apply('session.sidecars', {'patch': patch})

    def update_session_tool_names(self, session_id, pin):
        # The tools[] freeze pin ({"version", "tools"[, "delegation_backend"]}; None clears): without it every
        # fresh worker re-probes check_fns and a config flip between turns silently forks the cached prefix.
        self._session(session_id)
        self._apply('session.tools', {'tool_names': pin})

    def finish(self):
        return self._apply('execution.finish', {})

    def close(self):
        with self.lock:
            try:
                self.flush_token_counts()
            finally:
                self._outbox_owner.close()
