"""Private terminal-only tombstone queries; never reconstruct executable history.

The authenticated edge supplies the original principal/target/request and frozen
payload. A retired target permits only that exact digest, never fresh admission.
Worker receipt reads additionally require the original private adoption proof.
"""
import hashlib
import hmac
import json
import time

from hermes_state_runtime import ADMITTED_REQUEST_CONFLICT, RuntimeStoreError, _epoch, _json, _row, _secret_digest, _text, admission_fingerprint

RESULT_PREFIX = 'gateway.admission.result.v1.'
ADMISSION_PREFIX = 'gateway.terminal_admission.v1.'
IDENTITY_PREFIX = 'gateway.terminal_identity.v1.'
WORKER_PREFIX = 'gateway.terminal_worker.v1.'

# The settled-result rows above are a CACHE nobody read back for ever: an idempotent exact retry
# reads one, and nothing else does. Left unbounded they grew to 759 MB / 10,704 rows on one
# profile's state.db (a quarter of the file) because every turn's per-turn message suffix was
# stored whole and nothing expired it. Three bounds, all enforced in the settlement write itself
# so a store cannot grow between maintenance passes:
#   * RESULT_TTL_SECONDS   - an exact retry horizon; older rows are dropped.
#   * RESULT_CACHE_MAX_BYTES - hard cap for the whole prefix; least-recently-settled rows are
#     evicted first, so the cache never grows past this whatever the turn rate.
#   * RESULT_ROW_MAX_BYTES - a single row stores the verdict and a digest of the body it dropped,
#     never a whole result body (a long agentic turn's tool traffic is the bulk).
RESULT_TTL_SECONDS = 7 * 86400
RESULT_CACHE_MAX_BYTES = 256 * 1024 * 1024
RESULT_ROW_MAX_BYTES = 64 * 1024
_RESULT_CACHE_SEEDED_KEY = 'gateway.admission.result.cache_seeded'


def _project_verdict(stored, *, max_bytes):
    """A stored result row over *max_bytes* keeps its verdict and a digest, never the whole body.

    ``result.messages`` is the turn's tool traffic and is the bulk of a long agentic turn; no
    reader of a *stored* result needs it (the transcript is the record of the turn, and every
    live reader takes the in-process result). The dropped body's exact sha256 is kept so a
    caller can prove what it is not seeing, and the verdict fields (final_response, flags,
    error, ledger keys) ride through unchanged.
    """
    full = _json(stored)
    raw = full.encode('utf-8')
    projected = dict(stored)
    value = stored.get('result') if isinstance(stored, dict) else None
    omitted = 0
    if isinstance(value, dict):
        value = dict(value)
        messages = value.pop('messages', None)
        omitted = len(messages) if isinstance(messages, list) else 0
        projected['result'] = value
    projected['_body'] = {'v': 1, 'sha256': hashlib.sha256(raw).hexdigest(),
                          'bytes': len(raw), 'messages_omitted': omitted}
    if len(_json(projected).encode('utf-8')) <= max_bytes:
        return projected
    # The verdict itself is over budget (a very long reply): keep a bounded prefix and say so,
    # so the row honors the per-row bound no matter what the turn produced.
    value = projected.get('result')
    reply = value.get('final_response') if isinstance(value, dict) else None
    if isinstance(reply, str) and reply:
        over = len(_json(projected).encode('utf-8')) - max_bytes
        keep = max(0, len(reply) - over - 32)
        value = dict(value)
        value['final_response'] = reply[:keep]
        value['_truncated'] = {'final_response_chars_dropped': len(reply) - keep}
        projected['result'] = value
    return projected


def stored_result_json(result, *, max_bytes=None):
    """The exact text settlement stores for *result*: whole when it fits the per-row budget,
    otherwise the verdict + digest projection (``_project_verdict``)."""
    budget = RESULT_ROW_MAX_BYTES if max_bytes is None else max_bytes
    encoded = encode_result(result)
    text = _json(encoded)
    if budget and len(text.encode('utf-8')) > budget:
        text = _json(_project_verdict(encoded, max_bytes=budget))
    return text


def note_result_row(conn, key, text, *, now=None):
    """Record one cache row in the ledger the expiry/cap pass orders by (``admission_result_cache``)."""
    stamp = time.time() if now is None else now
    size = len(text.encode('utf-8')) if isinstance(text, str) else 0
    conn.execute('INSERT INTO admission_result_cache(key,stored_at,rank,bytes) VALUES(?,?,?,?) '
                 'ON CONFLICT(key) DO UPDATE SET stored_at=excluded.stored_at, '
                 'rank=excluded.rank, bytes=excluded.bytes', (key, stamp, stamp, size))


def store_result(conn, admission_id, text):
    """Persist one settlement result row and its cache ledger entry on *conn*'s transaction."""
    key = RESULT_PREFIX + admission_id
    conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?) '
                 'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, text))
    note_result_row(conn, key, text)
    return key


def _drop_result_rows(conn, keys):
    keys = list(keys)
    for start in range(0, len(keys), 500):
        batch = keys[start:start + 500]
        marks = ','.join('?' * len(batch))
        conn.execute(f'DELETE FROM state_meta WHERE key IN ({marks})', batch)
        conn.execute(f'DELETE FROM admission_result_cache WHERE key IN ({marks})', batch)
    return len(keys)


def _seed_result_cache(conn):
    """Index result rows written before this ledger existed, once per store.

    Their real age is unknown, so they get ``stored_at = 0`` (never TTL-dropped) and rank by the
    admission's own ``seq`` — the insertion order of the turn — which makes the accumulated
    legacy rows the oldest thing the cap evicts first.
    """
    if conn.execute('SELECT 1 FROM state_meta WHERE key=?', (_RESULT_CACHE_SEEDED_KEY,)).fetchone():
        return
    conn.execute(
        'INSERT OR IGNORE INTO admission_result_cache(key, stored_at, rank, bytes) '
        'SELECT m.key, 0, COALESCE(a.seq, 0), length(CAST(m.value AS BLOB)) FROM state_meta m '
        'LEFT JOIN session_admissions a ON m.key = ? || a.admission_id WHERE m.key GLOB ?',
        (RESULT_PREFIX, RESULT_PREFIX + '*'))
    conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?) '
                 'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (_RESULT_CACHE_SEEDED_KEY, str(time.time())))


def trim_result_cache(conn, *, max_bytes=None, ttl_seconds=None, now=None,
                      seed=True, max_evictions=None):
    """Expire and cap-evict the admission-result cache inside *conn*'s transaction.

    Drops rows whose admission was retired (no replay value), rows older than the TTL, then
    least-recently-settled rows until the prefix fits *max_bytes*. Returns
    ``{'expired', 'evicted', 'retired', 'bytes'}``. Cheap on a hot path: one indexed SUM plus a
    bounded index walk, no JSON parsing.

    Settlement calls this with ``seed=False`` and ``max_evictions`` so the first write after an
    unbounded store is upgraded never pays the legacy store's one-time full eviction; the
    maintenance pass (``SessionDB.trim_admission_result_cache``) seeds and evicts whole.
    """
    budget = RESULT_CACHE_MAX_BYTES if max_bytes is None else max_bytes
    ttl = RESULT_TTL_SECONDS if ttl_seconds is None else ttl_seconds
    now = time.time() if now is None else now
    if seed:
        _seed_result_cache(conn)
    retired = _drop_result_rows(conn, [key for (key,) in conn.execute(
        'SELECT c.key FROM admission_result_cache c WHERE NOT EXISTS '
        '(SELECT 1 FROM state_meta m WHERE m.key = c.key)')])
    expired = 0
    if ttl and ttl > 0:
        expired = _drop_result_rows(conn, [key for (key,) in conn.execute(
            'SELECT key FROM admission_result_cache WHERE stored_at > 0 AND stored_at < ?',
            (now - ttl,))])
    total = conn.execute('SELECT COALESCE(SUM(bytes),0) FROM admission_result_cache').fetchone()[0]
    evicted = []
    if budget is not None and total > budget:
        for key, size in conn.execute('SELECT key, bytes FROM admission_result_cache ORDER BY rank ASC'):
            if total <= budget:
                break
            if max_evictions is not None and len(evicted) >= max_evictions:
                break
            evicted.append(key)
            total -= size or 0
        _drop_result_rows(conn, evicted)
        total = conn.execute('SELECT COALESCE(SUM(bytes),0) FROM admission_result_cache').fetchone()[0]
    return {'expired': expired, 'evicted': len(evicted), 'retired': retired, 'bytes': int(total)}


def compact_result(result, *, user_message=None):
    """Retain replay/accounting and current-turn API output, never cumulative history."""
    result = dict(result)
    value = result.get('result')
    if not isinstance(value, dict):
        return result
    value = dict(value)
    # The tool schema list rides every turn result for in-process transcript logging; it is the
    # same session-wide copy each turn (tens of KB, more with MCP) and no stored-result reader uses it.
    value.pop('tools', None)
    messages = value.get('messages')
    if isinstance(messages, list):
        # Agent results include all previous turns. APIs only project this turn's
        # assistant/tool suffix; previous_response_id stores its own history. Compression
        # is the exception: its replacement snapshot is the only immutable record of the
        # rewritten prefix for this turn. A later live transcript may include a successor.
        if not value.get('_compressed') and not value.get('_messages_are_turn_suffix'):
            from agent.turn_context import reanchor_current_turn_user_idx
            anchor = value.get('current_turn_user_idx')
            if not (type(anchor) is int and 0 <= anchor < len(messages)
                    and isinstance(messages[anchor], dict) and messages[anchor].get('role') == 'user'):
                anchor = reanchor_current_turn_user_idx(messages, user_message)
            value['messages'] = messages[anchor + 1:]
            if anchor >= 0 and isinstance(messages[anchor], dict) and messages[anchor].get('api_content'):
                # The user row as the model saw it (memory prefetch, plugin context): a chained
                # Responses turn replays it instead of a row rebuilt from the raw request text.
                value['_turn_user_row'] = {k: messages[anchor][k] for k in ('role', 'content', 'api_content')}
            # Recovery/verification nudges are later user-role rows within this same turn.
            # Consumers must not infer a new boundary from them after the real anchor is gone.
            value['_messages_are_turn_suffix'] = True
            value.pop('current_turn_user_idx', None)
    result['result'] = value
    return result


# Top-level row key only ``encode_result`` writes. The row's top level is built by the gateway
# (``result``/``usage``), never by the agent or a message, so message fields can't collide with it.
_STORED = '_stored'


def encode_result(result):
    """The stored row keeps the reply once. When the turn's closing assistant message carries
    exactly ``final_response``, its ``content`` is stored as null and the row gets
    ``_stored: {v: 1, reply_ref: <that message's index>}``. Retirement drops ``messages`` and
    keeps ``final_response``, so a deleted session's exact retry still has its reply. A result
    that already has ``_stored`` is stored whole with that value escaped as ``{v: 1, raw: ...}``."""
    if not isinstance(result, dict):
        return result
    if _STORED in result:
        return {**result, _STORED: {'v': 1, 'raw': result[_STORED]}}
    value = result.get('result')
    if not isinstance(value, dict) or not isinstance(value.get('messages'), list):
        return result
    reply, messages = value.get('final_response'), list(value['messages'])
    last = next((i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], dict)
                 and messages[i].get('role') == 'assistant'), None)
    if not (isinstance(reply, str) and reply and last is not None and messages[last].get('content') == reply):
        return result
    messages[last] = {**messages[last], 'content': None}
    return {**result, 'result': {**value, 'messages': messages}, _STORED: {'v': 1, 'reply_ref': last}}


def load_result(text):
    """Decode a stored result row. Only a row carrying ``encode_result``'s exact envelope is
    decoded (to the dict it encoded, key order included); every other row, including all rows
    written before it, is returned as stored."""
    # Decodes in place: safe only because each call parses its own copy. A cache in front of this
    # must hand out copies, never the decoded dict itself.
    result = json.loads(text)
    if not isinstance(result, dict) or not isinstance(envelope := result.get(_STORED), dict):
        return result
    if envelope.keys() == {'v', 'raw'} and type(envelope['v']) is int and envelope['v'] == 1:
        result[_STORED] = envelope['raw']
        return result
    value, ref = result.get('result'), envelope.get('reply_ref')
    messages = value.get('messages') if isinstance(value, dict) else None
    if not (envelope.keys() == {'v', 'reply_ref'} and type(envelope['v']) is int and envelope['v'] == 1
            and type(ref) is int and isinstance(messages, list) and 0 <= ref < len(messages)
            and isinstance(value.get('final_response'), str) and isinstance(messages[ref], dict)
            and messages[ref].get('role') == 'assistant' and messages[ref].get('content', 0) is None):
        return result
    messages[ref] = {k: value['final_response'] if k == 'content' else v for k, v in messages[ref].items()}
    del result[_STORED]
    return result


def identity_key(principal_id, session_id, request_id):
    return IDENTITY_PREFIX + admission_fingerprint(canonical_target=session_id,
        payload={'principal': principal_id, 'request': request_id})


def terminal_api_admissions(conn, request_id):
    """Retired API admissions that carried *request_id*: a deleted session's tombstones, kept for
    the exact-retry horizon, still spend its Idempotency-Key for every other target."""
    return [json.loads(row[0]) for row in conn.execute(
        "SELECT value FROM state_meta WHERE key GLOB ? "
        "AND json_extract(value, '$.principal_id')='api' AND json_extract(value, '$.request_id')=?",
        (ADMISSION_PREFIX + '*', request_id))]


def terminal_admission(conn, admission_id):
    saved = conn.execute('SELECT value FROM state_meta WHERE key=?',
                         (ADMISSION_PREFIX + admission_id,)).fetchone()
    return json.loads(saved[0]) if saved else None


def retry_terminal_admission(db, *, epoch, principal_id, session_id, request_id, payload, intent='queue'):
    """None means a live target; a retired miss/conflict is a definitive refusal.

    This is a read-only identity lookup, not authorization to attach, restore a
    route, or write. HTTP authentication/grant checks must precede this query.
    """
    for value in (principal_id, session_id, request_id):
        _text(value)
    digest = admission_fingerprint(canonical_target=session_id,
        payload={'input': json.loads(_json(payload)), 'intent': intent})
    with db._read_ctx() as conn:
        _epoch(conn, epoch)
        from hermes_state_mutation_retirement import RETIRED_PREFIX
        if not conn.execute('SELECT 1 FROM state_meta WHERE key=?', (RETIRED_PREFIX + session_id,)).fetchone():
            return None
        saved = conn.execute('SELECT value FROM state_meta WHERE key=?',
                             (identity_key(principal_id, session_id, request_id),)).fetchone()
        if saved is None:
            raise RuntimeStoreError('not_found')
        row = terminal_admission(conn, json.loads(saved[0]))
        if row['payload_digest'] != digest:
            raise RuntimeStoreError('admission_conflict', **ADMITTED_REQUEST_CONFLICT)
        return _row(row)


def terminal_worker_receipt(db, *, execution_id, session_id, generation, sequence,
                            adoption_secret, payload_digest):
    """Exact retired producer evidence; possession never reactivates the worker."""
    proof = _secret_digest(adoption_secret)
    with db._read_ctx() as conn:
        saved = conn.execute('SELECT value FROM state_meta WHERE key=?',
                             (WORKER_PREFIX + execution_id,)).fetchone()
        if saved is None:
            raise RuntimeStoreError('not_found')
        worker = json.loads(saved[0])
        if worker['session_id'] != session_id or not hmac.compare_digest(worker['adoption_digest'], proof):
            raise RuntimeStoreError('permission_denied')
        if type(generation) is not int or generation != worker['generation']:
            raise RuntimeStoreError('stale_generation')
        receipt = next((r for r in worker['receipts'] if r['sequence'] == sequence), None)
        if receipt is None:
            raise RuntimeStoreError('stale_generation')
        if receipt['payload_digest'] != payload_digest:
            raise RuntimeStoreError('admission_conflict')
        if receipt['result_json'] is None:
            raise RuntimeStoreError('stale_generation')
        return json.loads(receipt['result_json'])
