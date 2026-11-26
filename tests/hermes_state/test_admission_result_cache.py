"""The settled-result replay cache is BOUNDED: TTL, hard size cap, LRU eviction, digest rows.

Measured before this: one profile's state.db carried 759 MB / 10,704 ``gateway.admission.result.v1.*``
rows (a quarter of a 2.7 GB file) because nothing expired them and each stored the whole per-turn
message suffix. These tests pin the three bounds and the open-session half of the same leak
(a state-owned session nothing ever ends can never be pruned).
"""
import hashlib
import json
import time

from gateway.session_results import admission_result, retain_result
from hermes_state import SessionDB
import hermes_state_runtime as rt
from hermes_state_terminal import RESULT_PREFIX, note_result_row, trim_result_cache


def _settle(db, epoch, session_id, request_id, text, result):
    rt.admit_session_input(db, epoch=epoch, principal_id='api', session_id=session_id,
                           request_id=request_id, payload={'text': text})
    row = rt.claim_session_input(db, epoch=epoch, session_id=session_id)
    retain_result(db, epoch=epoch, row=row, result=result)
    return row


def _cache_bytes(db, prefix=RESULT_PREFIX):
    with db._read_ctx() as conn:
        return conn.execute('SELECT COALESCE(SUM(length(value)),0) FROM state_meta WHERE key GLOB ?',
                            (prefix + '*',)).fetchone()[0]


def _cache_rows(db, prefix=RESULT_PREFIX):
    with db._read_ctx() as conn:
        return conn.execute('SELECT COUNT(*) FROM state_meta WHERE key GLOB ?', (prefix + '*',)).fetchone()[0]


def test_ten_thousand_rows_evict_to_the_size_cap(tmp_path):
    """10k admission rows + a 2 MB cap = the newest rows that fit, never the whole set."""
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('s', 'cli')
        now = time.time()
        row_size, cap = 2000, 2_000_000

        def write(conn):
            for i in range(10_000):
                key = RESULT_PREFIX + f'{i:032x}'
                conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)', (key, 'x' * row_size))
                # ranked by settlement time, oldest first
                note_result_row(conn, key, 'x' * row_size, now=now + i)
            return trim_result_cache(conn, max_bytes=cap, ttl_seconds=10 ** 9, now=now + 20_000)

        stats = db._execute_write(write)
        assert stats['evicted'] > 0
        assert _cache_bytes(db) <= cap
        assert _cache_rows(db) == cap // row_size
        # LRU: what survives is the newest suffix of admissions, not an arbitrary subset.
        with db._read_ctx() as conn:
            kept = [k for (k,) in conn.execute('SELECT key FROM state_meta WHERE key GLOB ?', (RESULT_PREFIX + '*',))]
            ranks = {k: conn.execute('SELECT rank FROM admission_result_cache WHERE key=?', (k,)).fetchone()[0]
                     for k in kept}
        assert min(ranks.values()) >= now + 9_000, sorted(ranks.values())[:3]
        assert max(ranks.values()) <= now + 9_999
    finally:
        db.close()


def test_a_row_older_than_the_ttl_is_dropped(tmp_path):
    """The exact-retry horizon is enforced: an expired row is gone, a fresh one stays."""
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('s', 'api_server')
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        old = _settle(db, epoch, 's', 'old', 'ask old', {'final_response': 'old reply', 'messages': []})
        fresh = _settle(db, epoch, 's', 'fresh', 'ask fresh', {'final_response': 'fresh reply', 'messages': []})
        now = time.time()
        ttl = 7 * 86400

        def age(conn):
            conn.execute('UPDATE admission_result_cache SET stored_at=?, rank=? WHERE key=?',
                         (now - ttl - 60, now - ttl - 60, RESULT_PREFIX + old['admission_id']))
        db._execute_write(age)

        stats = db._execute_write(lambda conn: trim_result_cache(conn, ttl_seconds=ttl, now=now))
        assert stats['expired'] == 1
        assert admission_result(db, old['admission_id']) is None
        assert admission_result(db, fresh['admission_id'])['final_response'] == 'fresh reply'
        with db._read_ctx() as conn:
            assert conn.execute('SELECT COUNT(*) FROM admission_result_cache').fetchone()[0] == 1
    finally:
        db.close()


def test_a_whole_result_body_is_stored_bounded_not_stripped(tmp_path):
    """A row over the per-row budget keeps the current-turn output, bounded.

    Every tool row survives (that is what ``/v1/responses`` renders as
    ``function_call_output``), with its payload capped; if the bounded list still overflows, the
    OLDEST rows go and ``_body.messages_dropped_sha256`` fingerprints exactly the list that went
    — a digest a caller can re-derive from the original body plus the reported allowance.
    """
    from hermes_state_runtime import _json
    from hermes_message_bounds import cap_tool_payloads
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('s', 'api_server')
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        body = [{'role': 'tool', 'tool_call_id': f'c{i}', 'content': 'y' * 4096} for i in range(64)]
        row = _settle(db, epoch, 's', 'big', 'ask big',
                      {'result': {'final_response': 'the reply', 'messages': body, 'completed': True},
                       'usage': {'input_tokens': 3}})
        with db._read_ctx() as conn:
            text = conn.execute('SELECT value FROM state_meta WHERE key=?',
                                (RESULT_PREFIX + row['admission_id'],)).fetchone()[0]
        assert len(text.encode('utf-8')) <= 64 * 1024
        stored = json.loads(text)
        assert stored['result']['final_response'] == 'the reply'
        assert stored['result']['completed'] is True
        assert stored['usage'] == {'input_tokens': 3}
        meta = stored['_body']
        assert meta['tool_payloads_capped'] == 64
        # The turn's tool rows are still there, newest-last, ids intact, payloads capped.
        kept = stored['result']['messages']
        assert kept and all(m['role'] == 'tool' for m in kept)
        assert kept[-1]['tool_call_id'] == 'c63'
        assert len(kept[-1]['content']) < 4096
        # The drop (if any) is exactly the bounded prefix of the original body, and its digest
        # verifies against a reconstruction a caller can build: original body, same allowance.
        dropped_count = meta.get('messages_dropped', 0)
        assert len(kept) + dropped_count == 64
        if dropped_count:
            reconstructible = cap_tool_payloads(body, meta['tool_payload_limit_chars'])[:dropped_count]
            assert hashlib.sha256(_json({'messages': reconstructible}).encode('utf-8')).hexdigest() \
                == meta['messages_dropped_sha256']
        # Readers still get the verdict.
        saved = admission_result(db, row['admission_id'])
        assert saved['result']['final_response'] == 'the reply'
    finally:
        db.close()


def test_a_fitting_turn_is_stored_verbatim(tmp_path):
    """Under the budget nothing is projected: the row is the encoded result, byte for byte."""
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('s', 'api_server')
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        body = [{'role': 'tool', 'tool_call_id': 'c0', 'content': 'small'}]
        row = _settle(db, epoch, 's', 'small', 'ask small',
                      {'result': {'final_response': 'ok', 'messages': body}})
        with db._read_ctx() as conn:
            stored = conn.execute('SELECT value FROM state_meta WHERE key=?',
                                  (RESULT_PREFIX + row['admission_id'],)).fetchone()[0]
        assert '_body' not in json.loads(stored)
        assert json.loads(stored)['result']['messages'] == body
    finally:
        db.close()


def test_a_dropping_projection_still_decodes_the_reply_row(tmp_path):
    """When the oldest messages are dropped, ``_stored.reply_ref`` follows them down.

    ``encode_result`` stores the closing assistant message's content as null and points at it
    with ``_stored.reply_ref``; a bounded projection that drops a leading prefix must shift that
    index, or ``load_result`` can no longer restore the reply and the reader sees a null message.
    """
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('s', 'api_server')
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        body = [{'role': 'tool', 'tool_call_id': f'c{i}', 'content': 'y' * 4096} for i in range(600)]
        body.append({'role': 'assistant', 'content': 'the reply'})
        row = _settle(db, epoch, 's', 'huge', 'ask huge',
                      {'result': {'final_response': 'the reply', 'messages': body}})
        with db._read_ctx() as conn:
            raw = conn.execute('SELECT value FROM state_meta WHERE key=?',
                               (RESULT_PREFIX + row['admission_id'],)).fetchone()[0]
        stored = json.loads(raw)
        assert stored['_body']['messages_dropped'] > 0
        assert '_stored' in stored and stored['_stored']['reply_ref'] < len(stored['result']['messages'])
        saved = admission_result(db, row['admission_id'])
        assert '_stored' not in saved
        assert saved['result']['messages'][-1] == {'role': 'assistant', 'content': 'the reply'}
    finally:
        db.close()


def test_retirement_keeps_the_ledger_bytes_true(tmp_path):
    """A result row shrunk in place at retirement must re-measure its ledger row.

    ``_compact_result`` replaces the stored text with a messages-free copy; the cap sums
    ``admission_result_cache.bytes``, so a ledger left holding the pre-compaction size over-counts
    and the trim evicts more live rows than the cap requires.  Recency (rank/stored_at) must NOT
    move: retirement is not a fresh settlement.
    """
    from hermes_state_mutation_retirement import _compact_result

    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('s', 'api_server')
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        body = [{'role': 'tool', 'tool_call_id': f'c{i}', 'content': 'y' * 4096} for i in range(64)]
        row = _settle(db, epoch, 's', 'big', 'ask big',
                      {'result': {'final_response': 'the reply', 'messages': body}})
        key = RESULT_PREFIX + row['admission_id']

        def census():
            with db._read_ctx() as conn:
                ledger = conn.execute('SELECT bytes, rank, stored_at FROM admission_result_cache '
                                      'WHERE key=?', (key,)).fetchone()
                actual = conn.execute('SELECT length(CAST(value AS BLOB)) FROM state_meta WHERE key=?',
                                      (key,)).fetchone()[0]
                return ledger, actual

        ledger_before, actual_before = census()
        db._execute_write(lambda conn: _compact_result(conn, row['admission_id']))
        ledger_after, actual_after = census()
        assert actual_after < actual_before          # the row really shrank
        assert ledger_after[0] == actual_after       # and the ledger says so
        assert ledger_after[1:] == ledger_before[1:]  # rank/stored_at untouched
    finally:
        db.close()


def test_a_session_that_never_ends_is_closed_by_the_reaper(tmp_path):
    """A crashed state-owned worker row is closed after the bound; an owned chat source is not."""
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('dead-worker', 'kanban')
        db.create_session('live-chat', 'telegram')
        stale = time.time() - 2 * 86400

        def age(conn):
            for sid in ('dead-worker', 'live-chat'):
                conn.execute('UPDATE sessions SET started_at=?, last_activity_at=? WHERE id=?',
                             (stale, stale, sid))
        db._execute_write(age)
        assert db.maybe_reap_abandoned_sessions(max_idle_seconds=86400, min_interval_hours=0) == 1
        dead = db.get_session('dead-worker')
        assert dead['ended_at'] is not None and dead['end_reason'] == 'startup_orphan_reap'
        assert db.get_session('live-chat')['ended_at'] is None
        # Throttled: the second call inside the interval does nothing.
        db.create_session('dead-worker-2', 'kanban')
        db._execute_write(lambda conn: conn.execute(
            'UPDATE sessions SET started_at=?, last_activity_at=? WHERE id=?', (stale, stale, 'dead-worker-2')))
        assert db.maybe_reap_abandoned_sessions(max_idle_seconds=86400) == 0
    finally:
        db.close()


def test_the_cache_does_not_grow_across_one_thousand_admissions(tmp_path, monkeypatch):
    """Settlement itself enforces the cap: 1k turns at 2 KB leave the prefix at the cap, not 2 MB."""
    import hermes_state_terminal as terminal
    monkeypatch.setattr(terminal, 'RESULT_CACHE_MAX_BYTES', 50_000)
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('s', 'api_server')
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        blob = 'x' * 2000
        for i in range(1000):
            _settle(db, epoch, 's', f'r{i}', f'ask {i}',
                    {'result': {'final_response': 'ok', 'messages': [{'role': 'assistant', 'content': blob}]},
                     'usage': {'input_tokens': 1}})
        assert _cache_bytes(db) <= terminal.RESULT_CACHE_MAX_BYTES
        # 2 KB + row overhead per admission => the cap holds ~24 rows, never 1000.
        assert _cache_rows(db) <= 30
        # The newest admission is kept (a live retry window) and the oldest was evicted.
        with db._read_ctx() as conn:
            newest = conn.execute('SELECT MAX(rank) FROM admission_result_cache').fetchone()[0]
            oldest = conn.execute('SELECT MIN(rank) FROM admission_result_cache').fetchone()[0]
        assert newest - oldest < 3600 * 24
    finally:
        db.close()
