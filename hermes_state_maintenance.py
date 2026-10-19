"""Retention pruning, stale-session archiving and VACUUM policy mixin for SessionDB."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from hermes_state_common import (
    AUTO_VACUUM_MIN_FREELIST_RATIO, RECLAIM_CHUNK_PAGES, RECLAIM_CHUNK_YIELD_MS,
    RECLAIM_MAX_PAGES_PER_PASS, RECLAIM_MAX_SECONDS_PER_PASS, RECLAIM_MIN_FREELIST_PAGES,
    RECLAIM_MIGRATE_MIN_FREELIST_RATIO,
    _id_chunks, _non_continuation_child_sql, _placeholders, _sql_session_last_active,
    escape_like as _escape_like
)
from hermes_startup_watchdog import report_startup_progress

# caplog tests pin the "hermes_state" logger name.
logger = logging.getLogger("hermes_state")

_LAST_ACTIVE_SQL = _sql_session_last_active("s")
_TOKENS_SQL = "(COALESCE(s.input_tokens, 0) + COALESCE(s.output_tokens, 0))"
_COST_SQL = "COALESCE(s.actual_cost_usd, s.estimated_cost_usd, 0)"


def _like(value: str) -> str:
    return f"%{_escape_like(value.lower())}%"


def _cwd_prefix_filter(value: str) -> tuple[list[str], list]:
    from hermes_state_sessions import _cwd_prefix_clause
    clause, params = _cwd_prefix_clause(value)
    return [clause], list(params)


def _one(clause: str, conv=None):
    return lambda v: ([clause], [conv(v) if conv else v])


def _seconds_since(now: float, raw) -> Optional[float]:
    """Age of a state_meta timestamp; None when unset or corrupt (= no prior run)."""
    try:
        return now - float(raw) if raw else None
    except (TypeError, ValueError):
        return None


# Prune/archive filters in evaluation order: (kwarg, applies-when, builder -> (clauses, params)).
# ``applies-when``: "notnone" (numeric/time bounds; 0 is a real bound) or "truthy" ("" = unset).
_PRUNE_FILTERS = (
    # Orphan-swept rows age from the sweep, not old activity, or the next prune deletes them before recovery.
    ("last_active_before", "notnone", lambda v: (
        [_LAST_ACTIVE_SQL + " < ?",
         "(COALESCE(s.end_reason, '') != 'startup_orphan_reap' OR s.ended_at < ?)"],
        [v, v])),
    ("last_active_after", "notnone", _one(_LAST_ACTIVE_SQL + " >= ?")),
    ("started_before", "notnone", _one("s.started_at < ?")),
    ("started_after", "notnone", _one("s.started_at >= ?")),
    ("source", "truthy", _one("s.source = ?")),
    ("title_like", "truthy", _one("LOWER(COALESCE(s.title, '')) LIKE ? ESCAPE '\\'", _like)),
    ("end_reason", "truthy", _one("s.end_reason = ?")),
    ("cwd_prefix", "truthy", _cwd_prefix_filter),
    ("min_messages", "notnone", _one("s.message_count >= ?")),
    ("max_messages", "notnone", _one("s.message_count <= ?")),
    ("model_like", "truthy", _one("LOWER(COALESCE(s.model, '')) LIKE ? ESCAPE '\\'", _like)),
    ("provider", "truthy", _one("LOWER(COALESCE(s.billing_provider, '')) = ?", str.lower)),
    ("user_id", "truthy", _one("s.user_id = ?")),
    ("chat_id", "truthy", _one("s.chat_id = ?")),
    ("chat_type", "truthy", _one("s.chat_type = ?")),
    ("branch_like", "truthy", _one("LOWER(COALESCE(s.git_branch, '')) LIKE ? ESCAPE '\\'", _like)),
    ("min_tokens", "notnone", _one(_TOKENS_SQL + " >= ?")),
    ("max_tokens", "notnone", _one(_TOKENS_SQL + " <= ?")),
    ("min_cost", "notnone", _one(_COST_SQL + " >= ?")),
    ("max_cost", "notnone", _one(_COST_SQL + " <= ?")),
    ("min_tool_calls", "notnone", _one("COALESCE(s.tool_call_count, 0) >= ?")),
    ("max_tool_calls", "notnone", _one("COALESCE(s.tool_call_count, 0) <= ?")),
)
_PRUNE_FILTER_NAMES = frozenset(name for name, _, _ in _PRUNE_FILTERS) | {"archived", "include_pinned", "lineage_tips_only"}

# Child ``c`` continues compression-ended ``p`` (same predicate as compression's child lookup).
_CONTINUATION_EDGE_SQL = "p.end_reason = 'compression'\n" + _non_continuation_child_sql("c.", "p.id")


def _continued_ancestors_sql(candidates_where: str) -> str:
    """Compression ancestors of every row *candidates_where* (alias ``s``) does not select."""
    return ("WITH RECURSIVE kept(id) AS ("
            " SELECT p.id FROM sessions c JOIN sessions p ON p.id = c.parent_session_id"
            f" WHERE {_CONTINUATION_EDGE_SQL}"
            f" AND NOT EXISTS (SELECT 1 FROM sessions s WHERE s.id = c.id AND {candidates_where})"
            " UNION"
            " SELECT p.id FROM kept k JOIN sessions c ON c.id = k.id JOIN sessions p ON p.id = c.parent_session_id"
            f" WHERE {_CONTINUATION_EDGE_SQL}"
            ") SELECT id FROM kept")


# A pin covers the whole conversation, but a store can hold a pinned segment whose later
# continuations were published unpinned; those still belong to the pinned chat.
_PINNED_TAIL_SQL = ("WITH RECURSIVE tail(id) AS ("
                    " SELECT c.id FROM sessions c JOIN sessions p ON p.id = c.parent_session_id"
                    f" WHERE COALESCE(p.pinned, 0) = 1 AND {_CONTINUATION_EDGE_SQL}"
                    " UNION"
                    " SELECT c.id FROM tail t JOIN sessions p ON p.id = t.id JOIN sessions c ON c.parent_session_id = p.id"
                    f" WHERE {_CONTINUATION_EDGE_SQL}"
                    ") SELECT id FROM tail")


def _not_pinned_sql(alias: str = "s") -> str:
    """Predicate sparing pinned rows and the unpinned continuations a pinned segment covers."""
    return f"COALESCE({alias}.pinned, 0) = 0 AND {alias}.id NOT IN ({_PINNED_TAIL_SQL})"


# Rows per DELETE chunk in :meth:`SessionMaintenanceMixin.prune_archived_messages` (card
# t_3e9bccd0).  A single unbatched delete of 122,431 archived rows was measured at 115 s in ONE
# transaction — minutes of write lock, which a live gateway writer waits out — while ~0.9 ms/row
# puts 2,000 rows at ~2 s per chunk, inside every live writer's patience budget.  Each chunk is its
# own transaction with a duty-cycle yield between them, so a live turn commits between chunks.
_ARCHIVED_MESSAGE_CHUNK_ROWS = 2000


class SessionMaintenanceMixin:
    """Retention pruning, stale-session archiving and VACUUM policy for SessionDB."""

    def prune_empty_ghost_sessions(self, sessions_dir: Optional[Path] = None) -> int:
        """Remove empty TUI ghost sessions (no messages, no title, >24hr old)."""
        cutoff = time.time() - 86400
        def _do(conn):
            ids = [r[0] for r in conn.execute("""
                SELECT id FROM sessions
                WHERE source = 'tui'
                  AND title IS NULL
                  AND ended_at IS NOT NULL
                  AND started_at < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM messages WHERE messages.session_id = sessions.id
                  )
            """, (cutoff,)).fetchall()]
            for chunk in _id_chunks(ids):
                conn.execute(f"DELETE FROM sessions WHERE id IN ({_placeholders(chunk)})", chunk)
            if ids:
                self._delete_unreferenced_system_prompts(conn)
            return ids
        removed_ids = self._execute_write(_do) or []
        for sid in removed_ids if sessions_dir else ():
            self._remove_session_files(sessions_dir, sid)
        return len(removed_ids)

    def _guarded_ids(self, conn, ids: Iterable[str]) -> set:
        """Ids in *ids* protected by a live turn lease / compression lock. Idle compression-ended
        parents are closed, not live, so they are not guarded (prune and delete share this)."""
        return {sid for sid in ids if self._write_guards_reject(conn, sid, allow_closed_compression_parent=True)}

    def _write_guards_reject(self, conn, sid: str, **kwargs) -> bool:
        """True when a live turn lease / compression lock protects ``sid``; expired or
        dead-holder guards are reclaimed and fenced as a side effect."""
        from hermes_state import SessionCompressionInProgressError
        from hermes_state_errors import SessionTurnLeaseLostError
        try:
            self._check_transcript_write_guards(
                conn, sid, compression_lock_holder=None, turn_lease_holder=None,
                reject_active_turn_lease=True, reject_active_compression_lock=True, **kwargs)
        except (SessionCompressionInProgressError, SessionTurnLeaseLostError):
            return True
        return False

    def sweep_orphaned_sessions(
        self, *, max_idle_seconds: float, sources: tuple[str, ...] = ("tui", "desktop", "subagent"),
        exclude_ids: tuple[str, ...] = (), exclude_pinned: bool = False,
        heartbeat_staleness_seconds: Optional[float] = None,
        heartbeat_ownership_grace_seconds: Optional[float] = None, respect_gateway_heartbeats: bool = True,
    ) -> list[str]:
        """Close session rows orphaned by a dead gateway process (its in-process disconnect grace timer died
        with it, leaving ``ended_at IS NULL`` forever).  Rows of ``sources`` whose ``started_at`` AND
        canonical last activity are both older than ``max_idle_seconds`` get
        ``end_reason='startup_orphan_reap'`` (the ``started_at`` predicate protects fresh compression/branch
        children whose copied activity is old).  Only pass sources whose lifecycle the caller owns — never
        messaging platforms like ``telegram`` (ending those triggers a routing loop).  ``exclude_ids`` spares
        rows this process still holds.  Non-destructive: messages kept, row resumable, first-reason-wins.
        With ``respect_gateway_heartbeats`` a row is reaped only when no live backend (heartbeat within
        ``heartbeat_staleness_seconds``, default ``2 * max_idle_seconds``) could own it: B owns S if
        ``B.started_at <= S.started_at + grace`` (default = staleness) — grace covers a migrating backend
        whose sessions predate its first heartbeat, bounded so a PID-reuse respawn cannot protect rows
        forever.  Disable the gate only for state.db-owned sources.  SELECT, live-lease validation and UPDATE
        run in one ``BEGIN IMMEDIATE`` transaction; active leases/locks spare the row, expired guards are
        removed so their owner is fenced.

        See #65194.
        ``exclude_pinned`` is intended for broad automatic sweeps; pinned rows remain explicitly
        recoverable. See #60609.
        """
        srcs = tuple(s for s in sources if s)
        if max_idle_seconds <= 0 or not srcs:
            return []
        hb_staleness, hb_grace = heartbeat_staleness_seconds, heartbeat_ownership_grace_seconds
        if not (hb_staleness and hb_staleness > 0):
            hb_staleness = max_idle_seconds * 2
        if not (hb_grace is not None and hb_grace >= 0):
            hb_grace = hb_staleness
        cutoff = (now := time.time()) - max_idle_seconds
        pin_scope = f" AND {_not_pinned_sql('sessions')}" if exclude_pinned else ""
        orphan_predicate = f"started_at < ? AND {_sql_session_last_active('sessions')} < ?"
        heartbeat_params: tuple[float, ...] = ()
        if respect_gateway_heartbeats:
            orphan_predicate += (" AND NOT EXISTS (SELECT 1 FROM gateway_heartbeats h WHERE"
                                 " h.last_heartbeat >= ? AND h.started_at <= sessions.started_at + ?)")
            heartbeat_params = (now - hb_staleness, hb_grace)
        scope_sql = f" AND source IN ({_placeholders(srcs)}){pin_scope} AND {orphan_predicate}"
        scope_params = (*srcs, cutoff, cutoff, *heartbeat_params)
        def _do(conn):
            rows = conn.execute(f"SELECT id FROM sessions WHERE ended_at IS NULL{scope_sql}",
                                scope_params).fetchall()
            excluded = {str(x) for x in exclude_ids if x}
            victims = [sid for sid in (str(row["id"]) for row in rows)
                       if sid not in excluded and not self._write_guards_reject(conn, sid)]
            if not victims:
                return []
            # Re-apply every predicate under the write lock.
            conn.execute(
                f"UPDATE sessions SET ended_at = ?, end_reason = 'startup_orphan_reap'"
                f" WHERE id IN ({_placeholders(victims)}) AND ended_at IS NULL{scope_sql}",
                (time.time(), *victims, *scope_params))
            return victims
        return self._execute_write(_do) or []

    @staticmethod
    def _prune_filter_where(*, archived: Optional[bool] = None, include_pinned: bool = False,
                            lineage_tips_only: bool = False, **filters) -> tuple[str, list]:
        """Shared WHERE clause for bulk prune/archive selection (alias ``s``): ``_PRUNE_FILTERS``
        AND together, only ended sessions are ever candidates, ``archived`` is tri-state
        (None = both), ``*_like`` are case-insensitive substrings, the rest exact.
        ``lineage_tips_only`` (bulk archive) drops compression ancestors: they are archived with
        their tip, never on their own age — matching an old ancestor would fan out over the lineage
        and hide its OPEN, recently active tip (#115489)."""
        unknown = set(filters) - _PRUNE_FILTER_NAMES
        if unknown:
            raise TypeError("SessionMaintenanceMixin._prune_filter_where() got an unexpected "
                            f"keyword argument {min(unknown)!r}")
        clauses = ["s.ended_at IS NOT NULL"]
        if lineage_tips_only:
            clauses.append("COALESCE(s.end_reason, '') <> 'compression'")
        params: list = []
        for name, applies, build in _PRUNE_FILTERS:
            value = filters.get(name)
            if (value is not None) if applies == "notnone" else bool(value):
                new_clauses, new_params = build(value)
                clauses.extend(new_clauses)
                params.extend(new_params)
        if isinstance(archived, bool):
            clauses.append(f"s.archived = {int(archived)}")
        # Pinned is a durable "keep" flag: bulk prune/delete/archive exclude pinned rows unless opted in.
        if not include_pinned:
            clauses.append(_not_pinned_sql())
        return " AND ".join(clauses), params

    def _prune_where(self, older_than_days, source, filters, *, whole_lineages: bool = False) -> tuple[str, list]:
        """Translate the legacy age window into the shared activity filter, then build WHERE.
        ``whole_lineages`` (prune) keeps a compression ancestor while any continuation after it
        is unmatched."""
        if (older_than_days is not None and filters.get("last_active_before") is None
                and filters.get("started_before") is None):
            if older_than_days < 0:
                raise ValueError(
                    f"older_than_days must be >= 0, got {older_than_days!r}: a negative "
                    "retention builds a future cutoff that matches every ended session.")
            filters["last_active_before"] = time.time() - (older_than_days * 86400)
        where, params = self._prune_filter_where(source=source, **filters)
        if not whole_lineages:
            return where, params
        # A compressed-away segment ages with its conversation, not on its own: while any later
        # segment stays, deleting it would cut the start off a chat that is still in use.
        return f"{where} AND s.id NOT IN ({_continued_ancestors_sql(where)})", [*params, *params]

    def list_prune_candidates(self, older_than_days: Optional[float] = None, source: str | None = None, *,
                              whole_lineages: bool = False, **filters) -> list[dict[str, Any]]:
        """Dry-run: sessions a matching prune/archive would touch, oldest first (``older_than_days``
        = inactivity threshold: freshest of ``last_activity_at`` / latest message / ``started_at``)."""
        where, params = self._prune_where(older_than_days, source, filters, whole_lineages=whole_lineages)
        return [dict(row) for row in self._read_all(
            f"""SELECT s.id, s.source, s.title, s.model, s.started_at,
                           {_LAST_ACTIVE_SQL} AS last_active,
                           s.ended_at, s.message_count, s.archived
                    FROM sessions s WHERE {where}
                    ORDER BY last_active ASC, s.started_at ASC""", params)]

    def count_prune_matches(self, older_than_days: Optional[float] = None, source: str | None = None, *,
                            pinned_only: bool = False, **filters) -> int:
        """Count-only :meth:`list_prune_candidates`; ``pinned_only`` counts rows carrying the pin
        itself, not the continuations it protects (CLI reports spared pinned sessions)."""
        if pinned_only:
            filters["include_pinned"] = True
        where, params = self._prune_where(older_than_days, source, filters)
        if pinned_only:
            where += " AND COALESCE(s.pinned, 0) = 1"
        return int(self._read_one(f"SELECT COUNT(*) FROM sessions s WHERE {where}", params)[0])

    def count_open_prune_matches(self, older_than_days: Optional[float] = None, source: str | None = None,
                                 **filters) -> int:
        """Count open sessions a matching prune skips (``ended_at`` guard inverted); visibility-only."""
        where, params = self._prune_where(older_than_days, source, filters)
        ended_guard = "s.ended_at IS NOT NULL"
        if not where.startswith(ended_guard):
            raise RuntimeError("prune filter lost its ended-session safety guard")
        open_where = f"s.ended_at IS NULL{where[len(ended_guard):]}"
        return int(self._read_one(f"SELECT COUNT(*) FROM sessions s WHERE {open_where}", params)[0])

    def archive_stale_sessions(self, idle_days: float, *, exclude_pinned: bool = True) -> int:
        """Archive every session untouched for ``idle_days`` (freshest of ``last_activity_at`` /
        latest message / ``started_at``); may archive unended sessions.  ``archived = 0`` makes
        repeats no-ops; only lineage tips (``end_reason <> 'compression'``) are candidates — a
        stale tip archives its chain via :meth:`set_session_archived`, so an old compressed-away
        root with a recent continuation is never matched.  The hidden canonical Bot Chat (same
        predicate as :meth:`set_session_pinned`) is exempt: only a deliberate archive may retire
        it, since archiving releases its registry title to the next Bot open."""
        if idle_days is None or idle_days < 0:
            return 0
        cutoff = time.time() - float(idle_days) * 86400.0
        pin_clause = f"AND {_not_pinned_sql()}" if exclude_pinned else ""
        rows = self._read_all(
            f"""
            SELECT s.id FROM sessions s
            WHERE s.archived = 0
              AND COALESCE(s.end_reason, '') <> 'compression'
              {pin_clause}
              AND NOT (COALESCE(s.hidden, 0) <> 0 AND COALESCE(s.title, '') = ?)
              AND {_LAST_ACTIVE_SQL} < ?
            ORDER BY s.started_at ASC
            """, (self.CANONICAL_BOT_CHAT_TITLE, cutoff))
        for row in rows:
            # Sweep provenance: a later compression/resume of this lineage un-hides it (#117713).
            self._auto_archive_lineage(row[0])
        return len(rows)

    def prune_sessions(self, older_than_days: Optional[float] = 90, source: str | None = None,
                       sessions_dir: Optional[Path] = None, exclude_active_write_guards: bool = False,
                       **filters) -> int:
        """Delete ended sessions inactive for ``older_than_days`` (an explicit ``started_before`` /
        ``last_active_before`` overrides it; None = no implicit bound) matching the filters.
        Children outside the window are orphaned (parent NULLed), not cascade-deleted.  With
        *sessions_dir*, transcript files are removed outside the DB transaction.
        ``exclude_active_write_guards`` (automatic maintenance) skips rows under a live turn lease
        or compression lock while expired/dead holders are reclaimed and fenced.  A compression
        ancestor is deleted only together with every continuation after it (``whole_lineages``)."""
        where, where_params = self._prune_where(older_than_days, source, filters, whole_lineages=True)
        removed_ids: list[str] = []
        def _do(conn):
            cursor = conn.execute(f"SELECT s.id FROM sessions s WHERE {where}", where_params)
            session_ids = {row["id"] for row in cursor.fetchall()}
            if exclude_active_write_guards:
                session_ids -= self._guarded_ids(conn, session_ids)
            if not session_ids:
                return 0
            # Batched: a cron-heavy store prunes tens of thousands of ids in one call.
            for chunk in _id_chunks(session_ids):
                ph = _placeholders(chunk)
                conn.execute(f"UPDATE sessions SET parent_session_id = NULL WHERE parent_session_id IN ({ph})", chunk)
                conn.execute(f"DELETE FROM messages WHERE session_id IN ({ph})", chunk)
                conn.execute(f"DELETE FROM sessions WHERE id IN ({ph})", chunk)
                removed_ids.extend(chunk)
            self._delete_unreferenced_system_prompts(conn)
            return len(session_ids)
        count = self._execute_write(_do)
        for sid in removed_ids:
            self._remove_session_files(sessions_dir, sid)
        return count

    def reap_orphan_messages(self) -> int:
        """Delete ``messages`` rows whose ``session_id`` no longer names a session.

        ``messages.session_id`` references ``sessions(id)`` but the schema declares no
        ``ON DELETE CASCADE``, so a session row removed while foreign-key enforcement was OFF
        (a legacy importer, an older writer that never issued ``PRAGMA foreign_keys=ON``, a
        bulk cleanup) strands that session's whole transcript. Orphan rows are invisible to
        every reader — nothing joins them back to a session — yet they stay in ``messages``
        and in BOTH FTS5 indexes forever, and the session-level retention policy can never
        age them out because there is no session left to grow old.

        The standard index's ``messages_fts_delete`` trigger fires on any message DELETE and
        drops the row.  The TRIGRAM index's ``messages_fts_trigram_delete`` does NOT: its
        ``WHEN`` clause requires the message's session row to still exist
        (``EXISTS (SELECT 1 FROM sessions WHERE id = old.session_id AND source NOT IN
        ('cron','subagent') ...)``) — exactly false for an orphan, whose session is already
        gone.  So this method purges the trigram index EXPLICITLY (the FTS5 ``'delete'``
        command, bounded to rows the index actually holds via ``messages_fts_trigram_docsize``)
        before deleting the messages; the ``DELETE`` then fires the standard trigger.  Returns
        the number of rows deleted (0 when there are no orphans).
        """
        orphan = "session_id NOT IN (SELECT id FROM sessions)"
        def _do(conn):
            # sessions.id is the TEXT primary key (never NULL) and messages.session_id is
            # NOT NULL, so ``NOT IN`` cannot hit the NULL-subquery trap that would match
            # nothing.
            try:
                conn.execute(
                    "INSERT INTO messages_fts_trigram(messages_fts_trigram, rowid, content, tool_name) "
                    "SELECT 'delete', m.id, m.content, m.tool_name FROM messages m "
                    f"WHERE m.{orphan} AND m.id IN (SELECT id FROM messages_fts_trigram_docsize)")
            except sqlite3.OperationalError as exc:
                # No trigram index (older CJK-less store): nothing to purge.
                logger.debug("No trigram-index purge for orphan reap: %s", exc)
            cursor = conn.execute(f"DELETE FROM messages WHERE {orphan}")
            return int(cursor.rowcount or 0)
        return self._execute_write(_do) or 0

    def prune_archived_messages(
        self, older_than_days: Optional[float] = None,
        sources: Optional[tuple[str, ...]] = None,
        chunk_rows: int = _ARCHIVED_MESSAGE_CHUNK_ROWS,
    ) -> int:
        """Apply the DECLARED retention window to archived message ROWS of *sources* (card
        t_3e9bccd0): delete rows no reader can see and no session-level rule can reach.

        Session retention keys on ``ended_at IS NOT NULL`` *and* idleness, so a source whose
        session is written to forever (``api_server``'s canonical Bot Chat: 176,834 rows, 62% of
        the store over 43 days) is structurally out of its reach — the session never ends and is
        never idle, and only 50 of its rows were ``active``.  The rows below the live window are
        compaction archive: invisible to every model-facing read, unbounded by any policy.

        The predicate is ALL of, in one SQL statement (:attr:`SessionDB.MESSAGE_RETENTION_SOURCES`
        scopes the sources; an empty tuple is fail-closed and deletes nothing):

        * the owning session's ``source`` is in *sources*;
        * ``active = 0`` — THE safety invariant.  Every model-facing read applies
          ``_active_clause(False, False)`` = ``AND active = 1`` (:meth:`get_messages_as_conversation`,
          including ancestor segments), so no live turn can reference a row this rule deletes;
        * ``compacted = 1`` — the compaction archive, not Undo/Rewind rows (widening to those is
          measured at +15% and deliberately deferred);
        * ``COALESCE(_compressed_summary, 0) = 0`` — a compaction SUMMARY is display history, kept;
        * ``timestamp < now - older_than_days * 86400`` — the same window, one policy, one meaning
          (``sessions.retention_days``; no second knob).

        Deletes in bounded chunks, each its own ``_execute_write`` transaction (write lock released
        between chunks) with the FTS backfill's duty cycle between them, renewing the startup
        watchdog's lease per chunk.  Returns the number of rows deleted; idempotent — a re-run
        matches only what is still there.
        """
        if older_than_days is None or older_than_days < 0:
            # A negative window builds a future cutoff and would delete the whole archive; the
            # caller's retention guard is the entry point, this is the fail-closed backstop.
            logger.warning(
                "state.db message retention skipped: older_than_days=%r is outside the allowed "
                "range (a whole number of days >= 0)", older_than_days)
            return 0
        source_list = tuple(self.MESSAGE_RETENTION_SOURCES if sources is None else sources)
        if not source_list:
            return 0
        cutoff = time.time() - float(older_than_days) * 86400.0
        where = ("m.session_id IN (SELECT s.id FROM sessions s WHERE s.source IN "
                 f"({_placeholders(source_list)})) AND m.active = 0 AND m.compacted = 1 "
                 "AND COALESCE(m._compressed_summary, 0) = 0 AND m.timestamp < ?")
        params = (*source_list, cutoff)

        def _chunk(conn) -> list:
            ids = [row[0] for row in conn.execute(
                f"SELECT m.id FROM messages m WHERE {where} LIMIT {int(chunk_rows)}", params).fetchall()]
            if ids:
                # The FTS delete triggers own both indexes for a row whose session still exists,
                # so the purge keeps pace with the delete (no explicit index work needed here).
                conn.execute(f"DELETE FROM messages WHERE id IN ({_placeholders(ids)})", ids)
            return ids

        total, chunks, max_hold_ms = 0, 0, 0.0
        while True:
            # Renew per chunk: the delete is I/O-bound with ~zero CPU, which the watchdog's CPU
            # fallback misreads as a parked deadlock (same reason as the steps in
            # maybe_auto_prune_and_vacuum). Leases are clamped to _MAX_LEASE_S, so renew per chunk.
            report_startup_progress(900.0, phase="state_db_auto_prune_archive")
            started = time.monotonic()
            ids = self._execute_write(_chunk)
            hold_ms = (time.monotonic() - started) * 1000.0
            if not ids:
                break
            total += len(ids)
            chunks += 1
            max_hold_ms = max(max_hold_ms, hold_ms)
            # Yield the write lock between chunks: back-to-back BEGIN IMMEDIATE chunks starve a
            # live gateway/CLI out of its lock retries (the FTS backfill's duty cycle, reused
            # rather than duplicated — it is the one place in this module that enforces it).
            time.sleep(max(self._FTS_REBUILD_MIN_PAUSE,
                           (hold_ms / 1000.0) * self._FTS_REBUILD_DUTY_FACTOR))
        if total:
            logger.info(
                "state.db message retention: deleted %d archived message row(s) older than %.0f "
                "day(s) from %s in %d chunk(s) (longest write-lock hold %.0f ms)",
                total, older_than_days, ",".join(source_list), chunks, max_hold_ms)
        return total

    def _record_state_db_maintenance(self, result: dict[str, Any], now: float) -> None:
        """Persist one row per maintenance run for audit: the counters the run produced plus
        the store geometry AFTER it. Written to ``state_meta['last_state_db_maintenance']``
        (the last run, cheap to read back) and appended as one JSON line to
        ``<home>/logs/state-db-maintenance.jsonl`` (the per-run history). Never raises:
        a bookkeeping failure must not fail maintenance.
        """
        try:
            pages = self._page_pragmas(
                ("page_count", "page_size", "freelist_count"),
                "Could not read page counts for the maintenance record: %s")
            record = {
                "at": now,
                "at_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
                "page_count": pages[0] if pages else None,
                "page_size": pages[1] if pages else None,
                "freelist_count": pages[2] if pages else None,
                "retention_days": result.get("retention_days"),
                "pruned": result.get("pruned"),
                "pruned_archived_messages": result.get("pruned_archived_messages"),
                "closed": result.get("closed"),
                "reaped_orphan_messages": result.get("reaped_orphan_messages"),
                "fts_indexes_optimized": result.get("fts_indexes_optimized"),
                "vacuumed": result.get("vacuumed"),
                "error": result.get("error"),
            }
            self.set_meta("last_state_db_maintenance", json.dumps(record))
            log_dir = Path(self.db_path).parent / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            with open(log_dir / "state-db-maintenance.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except Exception as exc:
            logger.debug("Could not record the state.db maintenance run: %s", exc)

    def _page_pragmas(self, names: tuple[str, ...], fail_msg: str) -> Optional[list]:
        """Integer PRAGMAs over the existing connection (never a byte probe); None + debug log on failure."""
        try:
            with self._read_ctx() as conn:
                if self._conn is None:
                    return None
                return [int(conn.execute(f"PRAGMA {name}").fetchone()[0]) for name in names]
        except Exception as exc:
            logger.debug(fail_msg, exc)
            return None

    def logical_size_bytes(self) -> Optional[int]:
        """``page_count * page_size``: main-file size once the WAL is checkpointed in.  Prefer
        over ``os.path.getsize`` when reporting a VACUUM: in WAL mode the rewrite lands in
        ``-wal`` and the checkpoint is refused while another connection holds a read-mark, so
        a stat() delta understates the win and can go negative."""
        values = self._page_pragmas(("page_count", "page_size"), "Could not read logical DB size: %s")
        return None if values is None else values[0] * values[1]

    def _freelist_ratio(self) -> Optional[float]:
        """Reclaimable fraction (``freelist_count / page_count``) gating VACUUM in
        :meth:`maybe_auto_prune_and_vacuum`; None = fall back to the time throttle.

        ``PRAGMA freelist_count / PRAGMA page_count`` read over the existing connection (never a byte-level
        probe of the live file — see ``sqlite_safe_read``). This is what VACUUM would actually give back; it
        is the gate :meth:`maybe_auto_prune_and_vacuum` uses to decide whether a full rewrite pays off
        (#54189).
        """
        values = self._page_pragmas(("page_count", "freelist_count"), "Could not read freelist ratio: %s")
        return None if values is None else (values[1] / values[0] if values[0] > 0 else 0.0)

    def _try_checkpoint(self, mode: str, fail_msg: str) -> None:
        try:
            self._conn.execute(f"PRAGMA wal_checkpoint({mode})")
        except Exception as exc:
            logger.debug(fail_msg, exc)

    # ── bounded incremental reclaim (card t_f8db44f6) ─────────────────────────

    def _auto_vacuum_mode(self) -> Optional[int]:
        """``PRAGMA auto_vacuum`` (0 = NONE, 1 = FULL, 2 = INCREMENTAL); None = unreadable.

        A ``PRAGMA auto_vacuum = INCREMENTAL`` takes effect only at the NEXT VACUUM, so this
        reports the header — i.e. whether the store has actually been MIGRATED — not intent."""
        values = self._page_pragmas(("auto_vacuum",), "Could not read auto_vacuum mode: %s")
        return None if values is None else values[0]

    def _freelist_pages(self) -> tuple[int, int]:
        """``(page_size, freelist_count)`` over the connection; ``(0, 0)`` when unreadable."""
        values = self._page_pragmas(("page_size", "freelist_count"), "Could not read freelist pages: %s")
        return (0, 0) if values is None else (values[0], values[1])

    def incremental_vacuum(
        self, *, chunk_pages: int = RECLAIM_CHUNK_PAGES, max_pages: int = RECLAIM_MAX_PAGES_PER_PASS,
        max_seconds: float = RECLAIM_MAX_SECONDS_PER_PASS,
        min_freelist_pages: int = RECLAIM_MIN_FREELIST_PAGES,
    ) -> dict[str, Any]:
        """Bounded ``PRAGMA incremental_vacuum`` drain.  NEVER issues a VACUUM.

        The store must already be ``auto_vacuum = INCREMENTAL`` (see :meth:`reclaim` for the
        one-time migration); on a NONE store the pragma is a no-op and running it anyway would
        report a drain that never happened.

        Each chunk is its own implicit transaction under ``self._lock``, so the write lock is
        released between chunks and a live writer interleaves instead of waiting behind a
        whole-file rewrite.  One statement returns at most one page to the file system
        (measured on SQLite 3.54.0 — the N argument is a bound, not a batch), so the drain
        loops.  It stops at ``max_pages`` freed, ``max_seconds`` elapsed, an empty freelist or
        a stalled pass — whichever comes first.  ``max_lock_hold_ms`` is the longest single
        write-lock hold observed, which is the number this exists to bound.
        """
        out: dict[str, Any] = {
            "ran": False, "pages": 0, "bytes": 0, "chunks": 0, "max_lock_hold_ms": 0.0,
            "page_size": 0, "freelist_before": 0, "freelist_after": 0, "stopped": "noop",
        }
        mode = self._auto_vacuum_mode()
        if mode is None:
            out["stopped"] = "unreadable"
            return out
        if mode != 2:
            out["stopped"] = "not-incremental"
            return out
        page_size, freelist = self._freelist_pages()
        out["page_size"] = page_size
        out["freelist_before"] = out["freelist_after"] = freelist
        # The pass is selected: report it as run even when the floor below makes it a no-op.
        out["ran"] = True
        if freelist < max(1, int(min_freelist_pages)):
            out["stopped"] = "below-floor"
            return out
        freed = 0
        deadline = time.monotonic() + max(0.0, float(max_seconds))
        stopped = "pages"
        while freed < max_pages:
            if time.monotonic() >= deadline:
                stopped = "seconds"
                break
            want = min(int(chunk_pages), int(max_pages) - freed)
            with self._lock:
                if self._conn is None:
                    self._reopen_after_close_locked(context="write")
                self._raise_if_db_replaced()
                before_pages = int(self._conn.execute("PRAGMA page_count").fetchone()[0])
                started = time.monotonic()
                self._conn.execute(f"PRAGMA incremental_vacuum({int(want)})")
                hold_ms = (time.monotonic() - started) * 1000.0
                after_pages = int(self._conn.execute("PRAGMA page_count").fetchone()[0])
            out["chunks"] += 1
            out["max_lock_hold_ms"] = max(out["max_lock_hold_ms"], hold_ms)
            gained = before_pages - after_pages
            if gained <= 0:
                stopped = "drained"
                break
            freed += gained
            if freed < max_pages:
                # Hand the write lock over between chunks: without this the loop re-acquires it
                # immediately and starves a competing writer for the whole pass (see
                # RECLAIM_CHUNK_YIELD_MS). The seconds budget below still bounds the pass.
                time.sleep(RECLAIM_CHUNK_YIELD_MS / 1000.0)
        _, out["freelist_after"] = self._freelist_pages()
        out["pages"] = freed
        out["bytes"] = freed * page_size
        out["stopped"] = stopped
        return out

    def reclaim(
        self, *, min_vacuum_freelist_ratio: float = RECLAIM_MIGRATE_MIN_FREELIST_RATIO,
        chunk_pages: int = RECLAIM_CHUNK_PAGES, max_pages: int = RECLAIM_MAX_PAGES_PER_PASS,
        max_seconds: float = RECLAIM_MAX_SECONDS_PER_PASS,
    ) -> dict[str, Any]:
        """Bounded space reclaim for the automatic maintenance path.

        ``PRAGMA auto_vacuum`` decides the branch:

        * NOT INCREMENTAL — set the header flag and run ONE ``VACUUM`` to convert the file.
          That rewrite is exclusive, so it keeps the freelist-ratio admission the full-VACUUM
          path always had (``None`` ratio = unreadable pragmas, fall back to the time
          throttle).  It is issued ONCE per store and never again: the next pass reads
          INCREMENTAL and takes the drain branch.
        * INCREMENTAL — drain bounded chunks via :meth:`incremental_vacuum`.  No VACUUM is ever
          issued again, so the store is never rewritten whole.

        The automatic path checkpoints PASSIVE only, never TRUNCATE (#110054).  The caller owns
        the holder gate and the time throttle; this method owns the mechanics.
        """
        out: dict[str, Any] = {
            "ran": False, "migrated": False, "reclaimed_bytes": 0, "reclaimed_pages": 0,
            "max_lock_hold_ms": 0.0, "mode": None, "mode_after": None,
            "freelist_ratio": None, "stopped": None,
        }
        self._raise_if_db_corrupt()
        mode = self._auto_vacuum_mode()
        out["mode"] = mode
        if mode is None:
            out["stopped"] = "unreadable"
            return out
        if mode != 2:
            ratio = self._freelist_ratio()
            out["freelist_ratio"] = ratio
            if ratio is not None and ratio <= min_vacuum_freelist_ratio:
                out["stopped"] = "freelist-ratio"
                return out
            before = self.logical_size_bytes() or 0
            with self._lock:
                if self._conn is None:
                    self._reopen_after_close_locked(context="write")
                self._raise_if_db_replaced()
                # The pragma only lands at the next VACUUM, so a failed rewrite below leaves
                # the store in NONE and the migration is simply retried on the next pass.
                self._conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            started = time.monotonic()
            self.vacuum(truncate_checkpoint=False)
            out["max_lock_hold_ms"] = (time.monotonic() - started) * 1000.0
            out["migrated"] = True
            out["ran"] = True
            out["stopped"] = "migrated"
            out["reclaimed_bytes"] = max(0, before - (self.logical_size_bytes() or before))
            out["mode_after"] = self._auto_vacuum_mode()
            return out
        drain = self.incremental_vacuum(
            chunk_pages=chunk_pages, max_pages=max_pages, max_seconds=max_seconds)
        out.update({
            "ran": drain["ran"], "reclaimed_pages": drain["pages"], "reclaimed_bytes": drain["bytes"],
            "max_lock_hold_ms": drain["max_lock_hold_ms"], "stopped": drain["stopped"],
        })
        self._try_checkpoint("PASSIVE", "WAL checkpoint (PASSIVE) after incremental reclaim failed: %s")
        out["mode_after"] = self._auto_vacuum_mode()
        return out

    def vacuum(self, *, truncate_checkpoint: bool = True) -> int:
        """VACUUM to reclaim space after large deletes (SQLite never shrinks on its own).
        Takes an exclusive lock — callers must ensure no other writers are active.  FTS5
        segments are merged first (:meth:`optimize_fts`) so their pages are reclaimed too;
        returns the number of FTS indexes optimized (0 on merge failure / no FTS). A quarantined
        handle (corrupt image, replaced file, lost WAL generation) raises before any rewrite: a
        VACUUM reads every page and commits the result back, turning contained damage into an
        amplified one (#105670). Same guard ``_execute_write`` applies to every write.

        *truncate_checkpoint* defaults to True for the manual command. The AUTOMATIC path
        passes False: a TRUNCATE reset retires the WAL generation a sibling writer still holds,
        which is the state every agent then refuses turns in (#110054). See :meth:`reclaim`."""
        self._raise_if_db_corrupt()
        optimized = 0
        try:
            optimized = self.optimize_fts()  # manages its own lock
        except Exception as exc:
            logger.warning("FTS optimize before VACUUM failed: %s", exc)
        with self._lock:
            self._raise_if_db_replaced()
            if self._conn is None:
                self._reopen_after_close_locked(context="write")
            # PASSIVE, not TRUNCATE: a manual `hermes sessions vacuum` runs in a transient CLI
            # process; a TRUNCATE reset here would race a live gateway writer.
            self._try_checkpoint("PASSIVE", "WAL checkpoint (PASSIVE) before VACUUM failed: %s")
            self._conn.execute("VACUUM")
            if truncate_checkpoint:
                # VACUUM rewrites every page THROUGH the WAL; without this TRUNCATE a 3 GB DB leaves a 3 GB -wal.
                self._try_checkpoint("TRUNCATE", "WAL checkpoint (TRUNCATE) after VACUUM failed: %s")
                # TRUNCATE may replace the WAL inode; adopt the new sidecars so the
                # write-path generation guard does not halt this connection. Not re-recorded on
                # the automatic path: a PASSIVE checkpoint of ours did not replace the inode, so
                # a fresh identity here could only mask a FOREIGN replacement.
                self._record_db_file_identity()
        return optimized

    def maybe_auto_prune_and_vacuum(
        self, retention_days: int = 90, min_interval_hours: int = 24, vacuum: bool = True,
        sessions_dir: Optional[Path] = None, min_vacuum_interval_days: int = 1,
        min_vacuum_freelist_ratio: float = AUTO_VACUUM_MIN_FREELIST_RATIO,
        vacuum_chunk_pages: int = RECLAIM_CHUNK_PAGES,
        vacuum_max_pages_per_pass: int = RECLAIM_MAX_PAGES_PER_PASS,
        vacuum_max_seconds_per_pass: float = RECLAIM_MAX_SECONDS_PER_PASS,
    ) -> dict[str, Any]:
        """Idempotent startup auto-maintenance (never raises): prune inactive sessions, reap stale open
        state-owned rows, optional space reclaim.  Runs at most once per ``min_interval_hours``; the
        reclaim has its own ``min_vacuum_interval_days`` throttle (default 1 — the stored bytes must
        actually come back, and a bounded pass is cheap enough to run every day).  Stale-open
        reconciliation: cron/kanban/subagent/one-shot CLI rows never set ``ended_at``
        when their process dies and prune only deletes ended rows, so after pruning, open rows from
        :attr:`_AUTO_PRUNE_STALE_OPEN_SOURCES` older than ``retention_days`` are closed
        (``startup_orphan_reap``); they stay resumable and age from their close.  After pruning it
        applies the SAME declared window to archived message ROWS of the declared stateless sources
        (:meth:`prune_archived_messages` — rows no reader can see, which session retention cannot
        reach for a session that never goes idle), reaps orphan ``messages`` rows
        (:meth:`reap_orphan_messages`) and merges BOTH FTS5 indexes (:meth:`optimize_fts`), then
        optionally reclaims; a per-run record is written by :meth:`_record_state_db_maintenance`.
        Returns ``{"skipped", "pruned", "pruned_archived_messages", "closed",
        "reaped_orphan_messages", "fts_indexes_optimized", "vacuumed"}`` plus
        ``"reclaimed_bytes"`` / ``"max_lock_hold_ms"`` / ``"reclaim_mode"`` / ``"freelist_ratio"``
        when a reclaim was attempted, and ``"error"`` on failure.

        The reclaim itself is :meth:`reclaim`: a store already in ``auto_vacuum = INCREMENTAL``
        drains bounded chunks (tens of ms of write lock each); a store still in NONE gets ONE
        migration ``VACUUM`` — admitted by the same holder scan and ``freelist_count /
        page_count`` > ``min_vacuum_freelist_ratio`` gate a full rewrite always needed, so a small
        prune on a dense multi-GB database never pays for one — and is never rewritten whole again.

        Records the last run timestamp in state_meta so subsequent calls within ``min_interval_hours``
        no-op. Designed to be called once at startup from long-lived entrypoints (CLI, gateway, cron
        scheduler). See #54189.
        When *sessions_dir* is provided, on-disk transcript files (``.json`` / ``.jsonl`` /
        ``request_dump_*``) for pruned sessions are removed as part of the same sweep (issue #3015).
        Messaging and UI sources are never touched here. See #54189.
        """
        from hermes_state_repair import _release_auto_maintenance_lock, _try_acquire_auto_maintenance_lock
        result: dict[str, Any] = {"skipped": False, "pruned": 0, "closed": 0, "vacuumed": False}
        result["retention_days"] = retention_days
        if retention_days is None or retention_days < 0:
            # A negative retention would build a future cutoff and match every ended
            # session; auto_prune=false is the disable switch, not a negative bound.
            logger.warning(
                "state.db auto-maintenance skipped: sessions.retention_days=%r is outside the allowed "
                "range (a whole number of days >= 0); set sessions.auto_prune: false to disable pruning",
                retention_days)
            result["skipped"] = True
            return result
        maintenance_lock = _try_acquire_auto_maintenance_lock(self.db_path)
        if maintenance_lock is None:
            result["skipped"] = True
            return result
        try:
            now = time.time()
            since_prune = _seconds_since(now, self.get_meta("last_auto_prune"))
            if since_prune is not None and since_prune < min_interval_hours * 3600:
                result["skipped"] = True
                return result
            # Prune first: orphans closed below get a full retention window.
            # Startup-watchdog leases: each long step is I/O-bound (near-zero CPU), which the
            # watchdog's CPU fallback misreads as a parked deadlock. Leases are clamped to
            # _MAX_LEASE_S=900 per call, so a multi-minute step renews per step rather than
            # once at entry. No-op when the watchdog is not armed; never raises.
            report_startup_progress(900.0, phase="state_db_auto_prune")
            result["pruned"] = pruned = self.prune_sessions(
                older_than_days=retention_days, sessions_dir=sessions_dir, exclude_active_write_guards=True)
            # Message-level retention (card t_3e9bccd0): the SAME declared window, applied to
            # ROWS of the declared stateless sources and restricted to rows no reader can see. A
            # session that never goes idle is structurally out of prune_sessions' reach, so
            # without this step its compaction archive is unbounded — and that archive is the
            # store's growth (~120 MB/day measured). Counts as freed rows for the admission below.
            result["pruned_archived_messages"] = archived = self.prune_archived_messages(
                older_than_days=retention_days)
            report_startup_progress(900.0, phase="state_db_auto_sweep")
            closed = self.sweep_orphaned_sessions(
                max_idle_seconds=float(retention_days) * 86400.0,
                sources=self._AUTO_PRUNE_STALE_OPEN_SOURCES, exclude_pinned=True,
                respect_gateway_heartbeats=False,  # state-owned lifecycles, not gateway heartbeats
            )
            result["closed"] = len(closed)
            # Orphan reap: a ``messages`` row whose session row was removed without going
            # through prune_sessions is invisible to every reader yet lives on in
            # ``messages`` and in BOTH FTS5 shadow indexes, and no retention window can age
            # it out. Reap it BEFORE the FTS merge so the merged index does not carry its
            # segments, and count it as freed rows for the admission below.
            report_startup_progress(900.0, phase="state_db_auto_reap_orphan_messages")
            result["reaped_orphan_messages"] = reaped = self.reap_orphan_messages()
            # Merge both FTS5 indexes on EVERY maintenance run, independent of the reclaim
            # gates below. 'optimize' is what reclaims the index segments left behind by
            # deleted rows, and the freelist-ratio gate SKIPS the rewrite on a dense store —
            # exactly when the merge is the only reclaim available.
            report_startup_progress(900.0, phase="state_db_auto_fts_optimize")
            try:
                result["fts_indexes_optimized"] = self.optimize_fts()
            except Exception as exc:
                logger.warning("state.db FTS optimize failed: %s", exc)
            # Reclaim only if rows were freed AND the time throttle passed.  The store's own
            # auto_vacuum mode decides the admission:
            #  * INCREMENTAL — bounded chunks, each holding the write lock for tens of ms and a
            #    pass that stops on a budget, so a live writer interleaves instead of waiting
            #    behind a rewrite. No holder gate, no freelist gate;
            #  * NONE — ONE exclusive rewrite converts the file. That is the admission
            #    `hermes sessions optimize` runs, so it keeps its holder gate and its freelist
            #    gate. It is the LAST whole-file rewrite this path issues for the store.
            since_vacuum = _seconds_since(now, self.get_meta("last_vacuum"))
            vacuum_due = since_vacuum is None or since_vacuum >= min_vacuum_interval_days * 86400
            if vacuum and (pruned > 0 or reaped > 0 or archived > 0) and vacuum_due:
                needs_migration = self._auto_vacuum_mode() != 2
                holders: list = []
                ratio: Optional[float] = None
                if needs_migration:
                    result["freelist_ratio"] = ratio = self._freelist_ratio()
                    # Same admission `hermes sessions optimize` runs: VACUUM plus the TRUNCATE checkpoint
                    # retire the WAL generation a sibling writer (gateway, Desktop, dashboard, cron) still
                    # holds, and that is exactly the state every agent then refuses turns in (#110054).
                    # The foreign scan skips our own pid, so it is paired with the in-process arm: under
                    # one multiplexed process another live SessionDB generation for this same path is
                    # just as much a holder as another process would be.
                    # Automatic maintenance only ever SKIPS — a turn is never refused over housekeeping.
                    from hermes_state_holders import (
                        foreign_state_db_holders, in_process_state_db_holders)
                    holders = (foreign_state_db_holders(self.db_path)
                               + in_process_state_db_holders(self.db_path, exclude=self))
                if holders:
                    result["vacuum_skipped_holders"] = len(holders)
                    logger.debug(
                        "state.db auto-maintenance: skipping VACUUM, %d other process(es) hold the "
                        "store or a WAL sidecar (%s)",
                        len(holders), ", ".join(f"{pid}:{target}" for pid, target in holders[:3]))
                else:
                    try:
                        # Reclaim is I/O-bound with ~zero CPU: renew the lease here so a slow
                        # migration rewrite on a large state.db never outlives the clamp.
                        report_startup_progress(900.0, phase="state_db_auto_vacuum")
                        rec = self.reclaim(
                            min_vacuum_freelist_ratio=min_vacuum_freelist_ratio,
                            chunk_pages=vacuum_chunk_pages, max_pages=vacuum_max_pages_per_pass,
                            max_seconds=vacuum_max_seconds_per_pass)
                        result["freelist_ratio"] = rec["freelist_ratio"]
                        result["reclaim_stopped"] = rec["stopped"]
                        if rec["ran"]:
                            result["vacuumed"] = True
                            result["reclaim_mode"] = "migrate" if rec["migrated"] else "incremental"
                            result["reclaimed_bytes"] = rec["reclaimed_bytes"]
                            result["reclaimed_pages"] = rec["reclaimed_pages"]
                            result["max_lock_hold_ms"] = round(rec["max_lock_hold_ms"], 3)
                            self.set_meta("last_vacuum", str(now))
                        elif rec["stopped"] == "freelist-ratio" and ratio is not None:
                            logger.debug("state.db auto-maintenance: skipping VACUUM, only "
                                         "%.1f%% of pages reclaimable (threshold %.0f%%)",
                                         ratio * 100.0, min_vacuum_freelist_ratio * 100.0)
                    except Exception as exc:
                        logger.warning("state.db reclaim failed: %s", exc)
            # Record even when pruned == 0 so the throttle holds.
            self.set_meta("last_auto_prune", str(now))
            self._record_state_db_maintenance(result, now)
            if closed or pruned > 0 or reaped > 0 or archived > 0:
                logger.info(
                    "state.db auto-maintenance: closed %d stale open session(s), pruned %d session(s) "
                    "inactive for %d days, deleted %d archived message row(s), reaped %d orphan "
                    "message row(s), merged %d FTS index(es)%s",
                    len(closed), pruned, retention_days, archived, reaped,
                    result.get("fts_indexes_optimized") or 0,
                    " + VACUUM" if result["vacuumed"] else "")
        except Exception as exc:
            # Maintenance must never block startup.
            logger.warning("state.db auto-maintenance failed: %s", exc)
            result["error"] = str(exc)
        finally:
            _release_auto_maintenance_lock(maintenance_lock)
        return result
