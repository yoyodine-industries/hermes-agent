"""Private terminal-only tombstone queries; never reconstruct executable history.

The authenticated edge supplies the original principal/target/request and frozen
payload. A retired target permits only that exact digest, never fresh admission.
Worker receipt reads additionally require the original private adoption proof.
"""
import hashlib
import hmac
import json
import time

from hermes_message_bounds import cap_tool_payloads_counted
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
#   * RESULT_ROW_MAX_BYTES - one row is BOUNDED, never the whole body: the verdict, the usage and
#     the current-turn messages survive (tool payloads capped, oldest rows dropped only under the
#     bound) because the API-server readers rebuild /v1/responses output from this row.
RESULT_TTL_SECONDS = 7 * 86400
RESULT_CACHE_MAX_BYTES = 256 * 1024 * 1024
RESULT_ROW_MAX_BYTES = 64 * 1024
# Share of a row's budget the current-turn messages may take (the rest is the verdict, usage,
# flags and the envelope). Within it each tool payload is capped to one adaptive allowance, never
# below the floor, so every function_call/tool row survives with its ids and a truncated payload
# instead of disappearing.
_RESULT_MESSAGES_BUDGET_SHARE = 0.72
_RESULT_TOOL_PAYLOAD_MAX_CHARS = 4000
_RESULT_TOOL_PAYLOAD_MIN_CHARS = 160
_RESULT_CACHE_SEEDED_KEY = 'gateway.admission.result.cache_seeded'


def _bounded_messages(messages, *, budget, keep_index=None):
    """Copy of *messages* bounded to *budget* bytes: ``(kept, dropped, meta)``.

    Tool payloads are capped first, with one allowance derived from the budget and the number of
    tool rows — every row keeps its role, ids and ``tool_calls`` (what
    ``_extract_output_items`` renders as ``function_call``/``function_call_output`` items), only
    the payload text is cut.  Only if the capped list still overflows is the OLDEST prefix dropped
    — never *keep_index*, the row ``_stored.reply_ref`` points at — and the exact list that went
    is fingerprinted, so a caller holding the original body can verify what it is not seeing.
    """
    tool_rows = sum(
        1 for m in messages
        if isinstance(m, dict) and (m.get('role') == 'tool' or (
            m.get('role') == 'assistant' and isinstance(m.get('tool_calls'), list))))
    allowance = max(_RESULT_TOOL_PAYLOAD_MIN_CHARS,
                    min(_RESULT_TOOL_PAYLOAD_MAX_CHARS, budget // max(1, tool_rows)))
    capped, capped_count = cap_tool_payloads_counted(messages, allowance)
    # One refinement pass: the allowance above ignores per-row JSON overhead, so scale it down by
    # how far the capped list overshot and cap the ORIGINAL rows again.  This is what lets a long
    # tool-heavy turn keep every function_call/function_call_output row (with shorter payloads)
    # instead of losing the earliest ones to the drop below.
    size = len(_json({'messages': capped}).encode('utf-8'))
    if size > budget and allowance > _RESULT_TOOL_PAYLOAD_MIN_CHARS:
        scaled = max(_RESULT_TOOL_PAYLOAD_MIN_CHARS, allowance * budget // size - 64)
        if scaled < allowance:
            capped, capped_count = cap_tool_payloads_counted(messages, scaled)
            allowance = scaled
    meta: dict = {'tool_payloads_capped': capped_count, 'tool_payload_limit_chars': allowance}
    start, total = len(capped), 0
    for i in range(len(capped) - 1, -1, -1):
        cost = len(_json({'m': capped[i]}).encode('utf-8'))
        if total + cost > budget:
            break
        total += cost
        start = i
    if keep_index is not None and 0 <= keep_index < start:
        start = keep_index  # keep the reply row even if it costs a little over the share
    if start <= 0:
        return capped, [], meta
    dropped = capped[:start]
    meta['messages_dropped'] = len(dropped)
    meta['messages_dropped_sha256'] = hashlib.sha256(
        _json({'messages': dropped}).encode('utf-8')).hexdigest()
    return capped[start:], dropped, meta


def _project_verdict(stored, *, max_bytes):
    """Bound a stored result row that is over *max_bytes* — never blank it.

    WHICH FIELDS SURVIVE MATTERS: on the live admission path the API server reads this row back
    from ``state_meta`` (``api_server._run_agent`` -> ``session_api_turn._settled_api_result``)
    and builds ``/v1/responses`` ``output`` items and the ``run.completed`` transcript from
    ``result.messages`` — a ``function_call`` per assistant ``tool_calls`` row and a
    ``function_call_output`` per tool row.  So the verdict, the usage and the current-turn
    messages all ride through; only their BYTES are bounded (``_bounded_messages``): tool
    payloads are capped, and only if that is not enough are the oldest messages dropped, never
    the reply row.  The dropped list's sha256 is recorded, so a caller holding the original body
    can prove exactly what went.

    ``_body`` reports what was bounded (``bytes`` = the full row this projected, the caps and
    the drops).  It carries no digest of the WHOLE body: once a row is bounded that pre-image is
    not reconstructible, and a digest nobody can verify is decoration.
    """
    raw = _json(stored).encode('utf-8')
    projected: dict = dict(stored)
    meta: dict = {'v': 2, 'bytes': len(raw)}
    value = stored.get('result') if isinstance(stored, dict) else None
    if isinstance(value, dict):
        value = dict(value)
        messages = value.get('messages')
        envelope = stored.get(_STORED)
        reply_ref = envelope.get('reply_ref') if isinstance(envelope, dict) else None
        if isinstance(messages, list) and messages:
            kept, dropped, dropped_meta = _bounded_messages(
                messages, budget=max(0, int(max_bytes * _RESULT_MESSAGES_BUDGET_SHARE)),
                keep_index=reply_ref)
            value['messages'] = kept
            meta.update(dropped_meta)
            if dropped and isinstance(envelope, dict) and isinstance(reply_ref, int):
                projected[_STORED] = {**envelope, 'reply_ref': reply_ref - len(dropped)}
        projected['result'] = value
    projected['_body'] = meta
    if len(_json(projected).encode('utf-8')) <= max_bytes:
        return projected
    # The verdict itself is over budget (a very long reply): keep a bounded prefix and say so.
    value = projected.get('result')
    reply = value.get('final_response') if isinstance(value, dict) else None
    if isinstance(reply, str) and reply:
        over = len(_json(projected).encode('utf-8')) - max_bytes
        keep = max(0, len(reply) - over - 64)
        value = dict(value)
        value['final_response'] = reply[:keep]
        value['_truncated'] = {'final_response_chars_dropped': len(reply) - keep}
        projected['result'] = value
    # Last resort for a pathologically framed row: the per-row bound is hard, so the message list
    # goes (the verdict stays).  Fingerprint the list that went like any other drop.
    if len(_json(projected).encode('utf-8')) > max_bytes:
        value = projected.get('result')
        kept = value.get('messages') if isinstance(value, dict) else None
        if isinstance(kept, list) and kept:
            value = dict(value)
            value['messages'] = []
            projected['result'] = value
            projected.pop(_STORED, None)  # the referenced reply row went with the messages
            meta['messages_dropped'] = meta.get('messages_dropped', 0) + len(kept)
            meta['messages_dropped_sha256'] = hashlib.sha256(
                _json({'messages': kept}).encode('utf-8')).hexdigest()
    return projected


def stored_result_json(result, *, max_bytes=None):
    """The exact text settlement stores for *result*: whole when it fits the per-row budget,
    otherwise the bounded projection (``_project_verdict``)."""
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
