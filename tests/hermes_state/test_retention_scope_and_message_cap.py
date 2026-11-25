"""Retention scope + per-session cap (ruling t_cd34976b, cards t_441fb640 / t_747ed5d7).

The ruled window ran for weeks, deleted 0 rows and reported ``pruned_archived_messages: 0``
because ``MESSAGE_RETENTION_SOURCES`` named ONE source (``api_server``) that the store barely
used, while a single ``local`` session held 365,847 rows (84.6% of the store) with 224 active.
These drive the real SessionDB (temp file DB) and hold the ruling's four boundaries:

* the widened allowlist REACHES every measured source and stays FAIL-CLOSED for an unlisted one,
  which the run record then makes visible (``archived_unreachable_rows_by_source``);
* evidence rows — compaction summaries, ``effect_disposition`` tool-effect audit rows and
  ``display_kind='failed_turn'`` records — survive BOTH bounds;
* the cap trims oldest-first to ``sessions.message_cap``, never touches the newest ``cap`` rows,
  and skips a session under a live turn lease;
* the knob is config, not a constant: ``0`` disables it and a non-default value is honoured by
  both carriers (CLI startup and the gateway housekeeping timer).
"""
import os
import time

import pytest

from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    d = SessionDB(tmp_path / "state.db")
    yield d
    try:
        d.close()
    except Exception:
        pass


def _mk_rows(db, sid, n, *, source="local", age_days=30.0, state="archived"):
    """A session with *n* rows, aged past any window, in one of three row states.

    ``archived`` = ``active=0, compacted=1`` (the retention archive: invisible to every reader);
    ``active``   = live transcript rows; ``plain`` = neither archived nor active.
    """
    db.create_session(sid, source=source)
    for index in range(n):
        db.append_message(sid, "user", f"{sid} payload {index}")
    ts = time.time() - age_days * 86400.0
    with db._lock:
        db._conn.execute("UPDATE messages SET timestamp=? WHERE session_id=?", (ts, sid))
        if state == "archived":
            db._conn.execute(
                "UPDATE messages SET active=0, compacted=1 WHERE session_id=?", (sid,))
        elif state == "active":
            db._conn.execute(
                "UPDATE messages SET active=1, compacted=0 WHERE session_id=?", (sid,))
        db._conn.commit()
        return [row[0] for row in db._conn.execute(
            "SELECT id FROM messages WHERE session_id=? ORDER BY id ASC", (sid,)).fetchall()]


def _mark(db, ids, **columns):
    """Set columns on specific message rows (``_compressed_summary`` / ``effect_disposition`` /
    ``display_kind`` / ...), the way the store records evidence and summaries."""
    with db._lock:
        for mid in ids:
            assignments = ", ".join(f"{name}=?" for name in columns)
            db._conn.execute(f"UPDATE messages SET {assignments} WHERE id=?",
                             (*columns.values(), mid))
        db._conn.commit()


def _ids(db, sid):
    with db._lock:
        return {row[0] for row in db._conn.execute(
            "SELECT id FROM messages WHERE session_id=?", (sid,)).fetchall()}


# ── 1. the widened scope REACHES a local session (the defect this card exists for) ──────────

def test_retention_scope_reaches_an_archived_local_session(db):
    ids = _mk_rows(db, "s-local", 10, source="local")

    removed = db.prune_archived_messages(older_than_days=14)

    assert removed == 10, "default sources must reach a local session's archive"
    assert _ids(db, "s-local") == set()
    assert set(ids)  # the fixture really had rows


def test_retention_scope_reaches_every_measured_source(db):
    """The tuple is the measured inventory, not a claim about which source can hold a long
    session — all of these are reachable with default sources."""
    for index, source in enumerate(
            ("api_server", "local", "kanban", "oneshot", "tui", "cron", "subagent",
             "bot_room", "whatsapp")):
        _mk_rows(db, f"s-src-{index}", 3, source=source)

    removed = db.prune_archived_messages(older_than_days=14)

    assert removed == 27


# ── 2. the allowlist stays FAIL-CLOSED, and an unlisted source is REPORTED ──────────────────

def test_unlisted_sources_are_untouched_and_censused(db):
    _mk_rows(db, "s-acp", 5, source="acp")
    _mk_rows(db, "s-telegram", 3, source="telegram")
    _mk_rows(db, "s-local", 4, source="local")

    removed = db.prune_archived_messages(older_than_days=14)

    assert removed == 4, "only the listed source is reachable"
    assert len(_ids(db, "s-acp")) == 5
    assert len(_ids(db, "s-telegram")) == 3

    census = db.archived_unreachable_rows_by_source(14)

    # A source that appears later must be VISIBLE the day its archive crosses the window.
    assert census == {"acp": 5, "telegram": 3}
    assert "local" not in census


def test_unreachable_census_is_empty_when_the_scope_covers_the_store(db):
    _mk_rows(db, "s-local", 4, source="local")
    db.prune_archived_messages(older_than_days=14)
    assert db.archived_unreachable_rows_by_source(14) == {}


# ── 3. evidence rows survive BOTH bounds ───────────────────────────────────────────────────

def test_retention_spares_summary_and_evidence_rows(db):
    ids = _mk_rows(db, "s-evidence", 6, source="local")
    _mark(db, [ids[0]], _compressed_summary=1)
    _mark(db, [ids[1]], effect_disposition="unknown")
    _mark(db, [ids[2]], display_kind="failed_turn")

    removed = db.prune_archived_messages(older_than_days=14)

    assert removed == 3, "only the three plain archived rows are deletable"
    assert _ids(db, "s-evidence") == {ids[0], ids[1], ids[2]}


def test_cap_spares_summary_and_evidence_rows_in_the_trimmed_window(db):
    ids = _mk_rows(db, "s-cap-preserved", 110, state="archived")
    _mark(db, [ids[0]], _compressed_summary=1)
    _mark(db, [ids[1]], effect_disposition="denied")
    _mark(db, [ids[2]], display_kind="failed_turn")

    result = db.cap_session_messages(cap=10)

    # 110 rows, cap 10 -> 100 excess, but 3 of the oldest 100 are evidence: 97 go, and the
    # effective floor is cap + the preserved rows (ruled: never "optimise" these away).
    assert result["capped_messages"] == 97, result
    survivors = _ids(db, "s-cap-preserved")
    assert len(survivors) == 13
    assert {ids[0], ids[1], ids[2]} <= survivors
    assert set(ids[-10:]) <= survivors, "the newest cap rows are never candidates"


# ── 4. the cap: oldest-first, exactly the excess, newest `cap` untouched ───────────────────

def test_cap_trims_exactly_the_oldest_excess(db):
    ids = _mk_rows(db, "s-cap", 110, state="archived")

    result = db.cap_session_messages(cap=10)

    assert result["capped_messages"] == 100, result
    assert result["capped_active_rows"] == 0, result
    assert result["sessions_capped"] == 1, result
    assert result["skipped_guarded"] == 0, result
    assert isinstance(result["max_lock_hold_ms"], float)
    assert _ids(db, "s-cap") == set(ids[-10:]), "the NEWEST cap rows survive; the oldest go"


def test_cap_leaves_a_small_session_alone(db):
    ids = _mk_rows(db, "s-small", 10, state="archived")

    result = db.cap_session_messages(cap=5000)

    assert result == {"capped_messages": 0, "capped_active_rows": 0, "sessions_capped": 0,
                      "skipped_guarded": 0, "max_lock_hold_ms": 0.0}
    assert _ids(db, "s-small") == set(ids)


def test_cap_chunks_do_not_change_the_outcome(db):
    """Chunking is a lock-discipline detail, not a semantics one."""
    ids = _mk_rows(db, "s-chunked", 25, state="archived")

    result = db.cap_session_messages(cap=5, chunk_rows=4)

    assert result["capped_messages"] == 20, result
    assert _ids(db, "s-chunked") == set(ids[-5:])


# ── 5. a live turn is unharmed: the session is skipped, then trimmed next pass ─────────────

def test_cap_skips_a_session_under_a_live_turn_lease_then_trims_it(db):
    ids = _mk_rows(db, "s-live", 30, state="archived")
    holder = f"pid={os.getpid()}:turn=cap"
    assert db.try_acquire_session_turn_lease("s-live", holder, ttl_seconds=300.0) is True

    first = db.cap_session_messages(cap=10)

    assert first["skipped_guarded"] == 1, first
    assert first["capped_messages"] == 0, first
    assert first["sessions_capped"] == 0, first
    assert len(_ids(db, "s-live")) == 30, "the live turn's context is untouched"

    db.release_session_turn_lease("s-live", holder)
    second = db.cap_session_messages(cap=10)

    assert second["capped_messages"] == 20, second
    assert _ids(db, "s-live") == set(ids[-10:])


# ── 6. ACTIVE rows alone over the cap: excess oldest go, counters recomputed ───────────────

def test_cap_on_active_rows_deletes_excess_and_recomputes_counters(db):
    ids = _mk_rows(db, "s-active", 25, state="active")

    result = db.cap_session_messages(cap=10)

    assert result["capped_active_rows"] == 15, result
    assert result["capped_messages"] == 15, result
    assert _ids(db, "s-active") == set(ids[-10:])
    session = db.get_session("s-active")
    assert session["message_count"] == 10, "counters are recomputed, never left stale"


# ── 7. the knob: 0 disables, a configured value is honoured by BOTH carriers ───────────────

def test_cap_zero_disables_the_bound(db):
    ids = _mk_rows(db, "s-cap", 40, state="archived")

    result = db.cap_session_messages(cap=0)

    assert result["capped_messages"] == 0 and result["sessions_capped"] == 0, result
    assert _ids(db, "s-cap") == set(ids)


def test_default_config_carries_the_ruled_ceiling():
    from hermes_cli.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["sessions"]["message_cap"] == 5000


def test_cli_carrier_forwards_the_configured_cap(monkeypatch, tmp_path):
    from unittest.mock import MagicMock
    import cli
    import hermes_cli.config
    import hermes_constants

    session_db = MagicMock()
    session_db.get_meta.return_value = "already-done"
    monkeypatch.setattr(
        hermes_cli.config, "load_config",
        lambda: {"sessions": {"auto_prune": True, "retention_days": 14, "message_cap": 1234}})
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)

    cli._run_state_db_auto_maintenance(session_db)

    assert session_db.maybe_auto_prune_and_vacuum.call_args.kwargs["message_cap"] == 1234


def test_gateway_carrier_forwards_the_configured_cap(monkeypatch, tmp_path):
    from unittest.mock import MagicMock
    import gateway.run as gateway_run
    import hermes_cli.config
    import hermes_state_registry

    session_db = MagicMock()
    monkeypatch.setattr(
        hermes_cli.config, "load_config",
        lambda: {"sessions": {"auto_prune": True, "retention_days": 14, "message_cap": 700}})
    monkeypatch.setattr(hermes_state_registry, "acquire", lambda *a, **k: session_db)
    monkeypatch.setattr(hermes_state_registry, "release_or_close", lambda *a, **k: None)

    gateway_run._housekeeping_state_db_maintenance()

    assert session_db.maybe_auto_prune_and_vacuum.call_args.kwargs["message_cap"] == 700


# ── the maintenance pass: ordering, admission and the recorded fields ──────────────────────

def test_maintenance_pass_caps_after_retention_and_records_the_fields(db, monkeypatch):
    """Retention first, cap second; the run record carries the cap counters AND the
    unreachable census, and a capped row counts as freed for the reclaim admission."""
    _mk_rows(db, "s-archive", 6, source="local", age_days=30.0)
    _mk_rows(db, "s-unlisted", 4, source="acp", age_days=30.0)
    cap_ids = _mk_rows(db, "s-cap", 30, source="local", age_days=0.0, state="archived")

    # Isolate the holder probe: it walks the REAL hermes home, which tests/home_io_guard.py
    # refuses from a checkout inside that home — an environmental refusal, not this behaviour.
    import hermes_state_holders as holders
    monkeypatch.setattr(holders, "foreign_state_db_holders", lambda *_a, **_k: [])
    monkeypatch.setattr(holders, "in_process_state_db_holders", lambda *_a, **_k: [])
    monkeypatch.setattr(db, "prune_sessions", lambda **_kwargs: 0)

    result = db.maybe_auto_prune_and_vacuum(
        retention_days=14, min_interval_hours=0, message_cap=10)

    assert result.get("error") is None, result
    assert result["pruned_archived_messages"] == 6, result
    assert result["capped_messages"] == 20, result
    assert result["sessions_capped"] == 1, result
    assert result["archived_unreachable_rows_by_source"] == {"acp": 4}, result
    assert result["message_cap"] == 10
    assert _ids(db, "s-cap") == set(cap_ids[-10:])

    # The jsonl line carries the same facts, so an unlisted source is visible in the log.
    import json
    from pathlib import Path
    record = json.loads(
        (Path(db.db_path).parent / "logs" / "state-db-maintenance.jsonl")
        .read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["capped_messages"] == 20
    assert record["sessions_capped"] == 1
    assert record["archived_unreachable_rows_by_source"] == {"acp": 4}
    assert record["message_cap"] == 10
