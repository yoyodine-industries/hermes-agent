"""One real worker client for the session-store acceptance run.

This is the PRODUCTION client stack, not a simulation: ``WorkerRPC`` discovers the live owner
over its control socket and ticket, ``worker.register`` reserves the execution, and
``RuntimeSessionStore`` journals then asks ``worker.persist`` for a receipt — the exact path that
reported "Hermes couldn't save this conversation" when the owner missed its budget.

Protocol with the harness (line-delimited JSON on stdout, plus a JSONL events file):
  * registers, takes the turn lease, prints {"event": "ready"}
  * waits for the file named by --go
  * appends a marker, prints the persist outcome and the receipt timing
  * on a refusal, asks the retained outbox to drain (the production recovery path)
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import psutil

from hermes_cli.process_identity import worker_birth


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', required=True)
    parser.add_argument('--session-id', required=True)
    parser.add_argument('--execution-id', required=True)
    parser.add_argument('--marker', required=True)
    parser.add_argument('--go', required=True)
    parser.add_argument('--events', required=True)
    args = parser.parse_args()

    home = Path(args.home)
    events = Path(args.events)
    start = time.time()

    def emit(event, **detail):
        row = {'pid': os.getpid(), 'session_id': args.session_id, 'event': event, 'at': time.time(),
               'since_start': round(time.time() - start, 3), **detail}
        with events.open('a') as handle:
            handle.write(json.dumps(row) + '\n')

    from agent import runtime_session_store as module

    transport = module.WorkerRPC(home)
    scope = {
        'profile_id': str(home),
        'session_id': args.session_id,
        'execution_id': args.execution_id,
        'generation': 0,
        'pid': os.getpid(),
        'birth': worker_birth(psutil.Process()),
        'secret': 'harness-secret-' + args.execution_id,
    }
    registration = transport('worker.register', **scope, kind='compute')
    scope['epoch'] = registration['owner_epoch']
    store = module.RuntimeSessionStore(transport, scope, home / 'worker-outboxes' / args.execution_id, max_bytes=200000)
    lease = store.try_acquire_session_turn_lease(args.session_id, args.execution_id + '-lease')
    emit('ready', lease=bool(lease))

    while not Path(args.go).exists():
        time.sleep(0.02)
    emit('go_seen')

    messages = [{'role': 'user', 'content': args.marker},
                {'role': 'assistant', 'content': 'ack ' + args.marker}]
    emit('persist_sent')
    try:
        count = store.append_messages_batch(args.session_id, messages)
        emit('persist_receipt', count=count)
    except Exception as exc:  # the named refusal, or a receipt timeout
        emit('persist_refused', code=str(exc), refusal=getattr(exc, 'refusal', None),
             exception=type(exc).__name__)
    # Production recovery: whatever the owner refused or left unacknowledged is retained in the
    # outbox and re-asked here; a flush reports True only when nothing is left pending.
    emit('flush_start')
    try:
        settled = store.flush_token_counts(timeout=30.0)
        emit('flush_done', settled=bool(settled), pending=len(store.journal.get('pending') or ()))
    except Exception as exc:
        emit('flush_raised', code=str(exc), exception=type(exc).__name__)
    emit('exit')
    transport.close()


if __name__ == '__main__':
    sys.exit(main())
