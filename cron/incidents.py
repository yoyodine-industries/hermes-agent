"""Durable cron failure incidents with signature dedup and ack.

The executions ledger records every attempt; this module groups the *failures* into incidents keyed
by ``(job_id, error signature)`` so the same job failing with the same error does not re-ping the
operator every run once acknowledged. Lifecycle: ``detected`` → ``alerted`` → ``closed``. The same
job + same normalized error resolves to the SAME incident id, so a closed incident stays closed
until the error text changes and mints a new one. ``alerted`` means a failure ping actually reached
the operator (``alerted_at`` = when the latest one did; the scheduler withholds repeats until
``cron.failure_repeat_alert_hours`` have passed). Incidents share ``cron/executions.db`` with
``cron.executions`` (one ledger file).
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from cron import executions as _executions
from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now

# Optional test override (mirrors ``cron.executions.EXECUTIONS_FILE``).
EXECUTIONS_FILE: Optional[Path] = None

# ``resolved``: the job ran OK after the failure (auto); ``closed``: the operator acked the signature
# and wants it silent. Only ``closed`` is terminal; a repeat of a resolved error re-opens it.
INCIDENT_STATES = ("detected", "alerted", "resolved", "closed")
#: The failure class whose jobs CANNOT run rather than merely failing: the job invokes a path
#: that is not there. Judged by the predicates below rather than by the keyword table - every one
#: of these failures also contains "script" (the `no_agent` wrapper writes it), so the table would
#: file them all as ``script`` and the act that depends on this class (``cron/unreachable_path.py``)
#: would never fire. ``exit 127`` and the shell's own "No such file or directory" are the only
#: reliable discriminators.
UNREACHABLE_PATH = "unreachable_path"
# ``bash: /opt/x/y.sh: No such file or directory`` and, with a line number in front of it,
# ``bash: line 50: exec /opt/x/python3: No such file or directory`` - the path is captured, not
# swallowed: the act routes the card from it. Case-insensitive because a shell message is not
# guaranteed to keep the reference capitalization.
_NO_SUCH_FILE_RE = re.compile(
    r"(?:bash: )?(?:line \d+: )?(?P<path>/\S*?): no such file or directory", re.IGNORECASE
)
# The 127 exit says the same thing when the shell's stderr did not survive into the error text.
_EXIT_127_RE = re.compile(
    r"\bexit(?:ed)?\b[^0-9\n]{0,20}\b127\b|\b(?:code|status)[\s:=]+127\b", re.IGNORECASE
)
# Last resort for a 127 that names its path in a shape the shell message above does not cover
# (``/opt/x/y: command not found``): the first absolute path in the text.
_FIRST_ABS_PATH_RE = re.compile(r"(?P<path>/[\w./+@-]+)")
_FAILURE_TYPE_ORDER = (
    ("rate_limit", (r"\b429\b", "rate limit", "usage limit", "quota")),
    ("timeout", ("timeout", "timed out")),
    ("auth", (r"\b401\b", "unauthorized", "authentication", "auth")),
    ("delivery", ("delivery", "deliver", "delivering")),
    ("config", ("config", "configuration", "validation")),
    ("script", ("script", "no_agent")),
    ("agent", ("agent", "model", "provider", "inference")),
)
MAX_ERROR_CHARS = 500
_MAX_SIGNATURE_ERROR_CHARS = 200
# Measured durations ("idle for 603s", "retry in 12.5 seconds") differ run to run for the same
# failure, so they are masked out of the signature. Status codes and other numbers still count.
_DURATION_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:ms|s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?)\b"
)

_lock = threading.RLock()


def _db_path() -> Path:
    """Shared cron DB path. The ``cron.executions`` override wins when installed so redirecting the
    executions ledger also redirects the incident table (they must stay in the SAME database); then
    this module's own override, then the canonical profile home."""
    for override in (_executions.EXECUTIONS_FILE, EXECUTIONS_FILE):
        if override is not None:
            return Path(override)
    return get_hermes_home().resolve() / "cron" / "executions.db"


def _connect() -> sqlite3.Connection:
    # Late imports: a scheduler daemon that outlives an on-disk upgrade already has the OLD
    # ``hermes_cli.sqlite_util`` / ``cron.jobs`` cached, so new names must be resolved at call time,
    # not at import time (the guarantee cron/ledger.py used to carry, see e24c8499).
    from cron.jobs import _ensure_cron_dir
    from hermes_cli.sqlite_util import open_db

    path = _db_path()
    _ensure_cron_dir(path.parent)
    return open_db(path, db_label="cron/executions.db", synchronous_full=True, initialize=_initialize_schema)


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_cli.sqlite_util import add_column_if_missing

    conn.execute(
        """CREATE TABLE IF NOT EXISTS cron_incidents (
             id            TEXT PRIMARY KEY,
             job_id        TEXT NOT NULL,
             error_sig     TEXT NOT NULL,
             state         TEXT NOT NULL,
             failure_type  TEXT NOT NULL DEFAULT 'unknown',
             first_seen_at TEXT NOT NULL,
             last_seen_at  TEXT NOT NULL,
             acked_at      TEXT,
             alerted_at    TEXT,
             closed_at     TEXT,
             error         TEXT NOT NULL,
             output_file   TEXT
           )"""
    )
    # Ledgers created before the alert-once gate lack ``alerted_at``; add it in place.
    add_column_if_missing(conn, "cron_incidents", "alerted_at", "alerted_at TEXT")
    # The unreachable_path ACT (``cron/unreachable_path.py``) stamps the card it filed - and the
    # board that holds it - on the row it filed it for, or the reason it could not. The row is the
    # index recovery closes from, so a failure-to-act is observable in the same place the operator
    # reads the failure. Ledgers created before the act lack all three; add them in place.
    add_column_if_missing(conn, "cron_incidents", "unreachable_card_id",
                          "unreachable_card_id TEXT")
    add_column_if_missing(conn, "cron_incidents", "unreachable_card_board",
                          "unreachable_card_board TEXT")
    add_column_if_missing(conn, "cron_incidents", "unreachable_card_error",
                          "unreachable_card_error TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cron_incidents_job "
        "ON cron_incidents(job_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cron_incidents_state "
        "ON cron_incidents(state)"
    )


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    from hermes_cli.sqlite_util import transaction

    with _lock, transaction(_connect()) as conn:
        yield conn


def _normalize_error(error: str) -> str:
    """Strip whitespace and lowercase before signing (dedup normalization)."""
    return re.sub(r"\s+", " ", str(error or "")).strip().lower()


def _redact_error(error: str) -> str:
    """Redact secrets (best-effort; the scheduler path never fails on it) then bound the length."""
    text = str(error or "")
    try:
        from agent.redact import redact_sensitive_text

        text = redact_sensitive_text(text, force=True)  # persisted to disk: always scrub
    except Exception:
        pass
    return text[:MAX_ERROR_CHARS]


def _error_signature(job_id: str, error: str) -> str:
    """Dedup key: stable for same job + same normalized error prefix."""
    normalized = _DURATION_RE.sub("#s", _normalize_error(error))[:_MAX_SIGNATURE_ERROR_CHARS]
    return hashlib.sha256(job_id.encode() + normalized.encode()).hexdigest()[:12]


def _incident_id(job_id: str, error_sig: str) -> str:
    return f"{job_id[:6]}_{error_sig}"


def is_unreachable_path(error: str) -> bool:
    """Whether ``error`` names a path that is not there (or carries the ``127`` exit).

    Read from the RAW text, not the normalized one: the captured path is case-sensitive on disk,
    and ``_normalize_error`` lowercases. Both predicates are shell facts, not keywords.
    """
    text = str(error or "")
    if not text.strip():
        return False
    return bool(_NO_SUCH_FILE_RE.search(text) or _EXIT_127_RE.search(text))


def captured_path(error: str) -> Optional[str]:
    """The path the failure says is missing; ``None`` when the text names none.

    The shell's own message wins (it is the path the kernel was handed); a bare ``127`` with no
    such message falls back to the first absolute path in the text, because a 127 that names its
    path as ``<path>: command not found`` is the same defect and must still route somewhere.
    """
    text = str(error or "")
    if not text.strip():
        return None
    match = _NO_SUCH_FILE_RE.search(text) or _FIRST_ABS_PATH_RE.search(text)
    return match.group("path") if match else None


def _classify_failure_type(error: str) -> str:
    """Classify a failure from error-text keywords; ``unknown`` is the default."""
    text = _normalize_error(error)
    if not text:
        return "unknown"
    # The unreachable-path class is judged FIRST and by its own predicates: see UNREACHABLE_PATH
    # above for why the keyword table below cannot be trusted to find it.
    if is_unreachable_path(error):
        return UNREACHABLE_PATH
    for kind, patterns in _FAILURE_TYPE_ORDER:
        for pattern in patterns:
            if pattern.startswith("\\b") and pattern.endswith("\\b"):
                if re.search(pattern, text):
                    return kind
            elif pattern in text:
                return kind
    return "unknown"


def upsert_incident(
    job_id: str, error: str, *, job_name: Optional[str] = None, failure_type: Optional[str] = None,
    output_file: Optional[str] = None,
) -> tuple[str, bool]:
    """Record (or refresh) the incident for ``job_id`` + ``error``; returns ``(incident_id,
    is_new)``. An existing row for the signature refreshes
    ``last_seen_at``/``error``/``output_file`` and keeps its state — a ``closed`` incident stays
    closed, while a ``resolved`` one (job recovered, then broke the same way again) re-opens as
    ``detected`` so the operator is alerted once more. A changed error text mints a new incident."""
    job_id = str(job_id or "")
    sig = _error_signature(job_id, error)
    stored_error = _redact_error(error)
    incident_id = _incident_id(job_id, sig)
    now = _hermes_now().isoformat()
    failure_type = failure_type or _classify_failure_type(error)
    output_file = str(output_file) if output_file is not None else None

    with _transaction() as conn:
        row = conn.execute(
            "SELECT id, state FROM cron_incidents WHERE id=?", (incident_id,)
        ).fetchone()
        if row is not None:
            reopen = row["state"] == "resolved"
            conn.execute(
                """UPDATE cron_incidents
                   SET last_seen_at=?, error=?, output_file=?,
                       state=CASE WHEN state='resolved' THEN 'detected' ELSE state END,
                       alerted_at=CASE WHEN state='resolved' THEN NULL ELSE alerted_at END,
                       closed_at=CASE WHEN state='resolved' THEN NULL ELSE closed_at END
                   WHERE id=?""",
                (now, stored_error, output_file, incident_id),
            )
            return incident_id, reopen
        conn.execute(
            """INSERT INTO cron_incidents
               (id, job_id, error_sig, state, failure_type,
                first_seen_at, last_seen_at, error, output_file)
               VALUES (?, ?, ?, 'detected', ?, ?, ?, ?, ?)""",
            (incident_id, job_id, sig, failure_type, now, now,
             stored_error, output_file),
        )
        return incident_id, True


def set_incident_state(incident_id: str, state: str) -> bool:
    """Transition an incident's lifecycle state; return whether it changed. ``closed`` is terminal
    for that signature (re-open happens by a changed error minting a NEW incident). Unknown states
    are rejected (no-op, ``False``). ``alerted`` also stamps ``alerted_at`` — every time, so the
    cooldown reminder (see ``cron.scheduler._upsert_incident_for_failure``) restarts its window."""
    if state not in INCIDENT_STATES:
        return False
    now = _hermes_now().isoformat()
    with _transaction() as conn:
        row = conn.execute(
            "SELECT state FROM cron_incidents WHERE id=?", (incident_id,)
        ).fetchone()
        if row is None or row["state"] == "closed":
            return False
        if state == "alerted":
            conn.execute(
                "UPDATE cron_incidents SET state='alerted', alerted_at=? WHERE id=?",
                (now, incident_id),
            )
            return True
        if row["state"] == state:
            return False
        if state == "closed":
            conn.execute(
                """UPDATE cron_incidents
                   SET state='closed', closed_at=?, acked_at=?
                   WHERE id=? AND state != 'closed'""",
                (now, now, incident_id),
            )
        else:
            conn.execute(
                "UPDATE cron_incidents SET state=? WHERE id=?",
                (state, incident_id),
            )
        return True


def ack_incident(incident_id: str) -> bool:
    """Acknowledge (close) an incident; ``False`` when missing or already closed."""
    return set_incident_state(incident_id, "closed")


def close_incidents_for_recovered_job(job_id: str) -> int:
    """Mark every open incident for ``job_id`` ``resolved`` after a successful run; returns how many.
    Without this the ledger only ever grows: a one-off failure (a config drift skip, a provider
    outage) stayed ``detected``/``alerted`` forever after the job recovered, so ``hermes cron
    incidents`` showed dozens of "open" incidents for jobs that had been green for weeks (32 of 32 on
    one install). ``resolved`` is distinct from the operator's ``closed`` on purpose: a repeat of the
    same error re-opens a resolved incident and alerts again (see ``upsert_incident``), whereas
    ``closed`` keeps that signature silent."""
    now = _hermes_now().isoformat()
    with _transaction() as conn:
        cursor = conn.execute(
            """UPDATE cron_incidents SET state='resolved', closed_at=?
               WHERE job_id=? AND state IN ('detected', 'alerted')""",
            (now, str(job_id or "")),
        )
        return int(cursor.rowcount or 0)


def _state_filter(state: Optional[str]) -> tuple[str, tuple]:
    return ("", ()) if state is None else (" WHERE state=?", (state,))


def list_incidents(state: Optional[str] = None) -> list[dict[str, Any]]:
    """Return incidents, newest-activity first, optionally filtered by state."""
    if state is not None and state not in INCIDENT_STATES:
        return []
    where, params = _state_filter(state)
    with _transaction() as conn:
        # last_seen_at carries a DST-varying offset: order by instant, not text.
        rows = conn.execute(
            "SELECT * FROM cron_incidents" + where
            + " ORDER BY julianday(last_seen_at) DESC, last_seen_at DESC, id DESC",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def get_incident(incident_id: str) -> Optional[dict[str, Any]]:
    with _transaction() as conn:
        row = conn.execute(
            "SELECT * FROM cron_incidents WHERE id=?", (incident_id,)
        ).fetchone()
    return dict(row) if row is not None else None


def count_incidents(state: Optional[str] = None) -> int:
    if state is not None and state not in INCIDENT_STATES:
        return 0
    where, params = _state_filter(state)
    with _transaction() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM cron_incidents" + where, params).fetchone()
    return int(row["n"]) if row is not None else 0


def record_unreachable_card(
    incident_id: str, *, card_id: Optional[str] = None, board: Optional[str] = None,
    error: Optional[str] = None,
) -> bool:
    """Stamp the card the unreachable-path act filed, or why it filed none.

    A ``card_id`` records the act; an ``error`` (with no card) records a failure-to-act, which is
    the case a reader must be able to see - a job failing every tick with neither a card nor a
    reason on its row is the silent failure the act exists to end.
    """
    if not incident_id:
        return False
    with _transaction() as conn:
        cursor = conn.execute(
            """UPDATE cron_incidents
               SET unreachable_card_id=?, unreachable_card_board=?, unreachable_card_error=?
               WHERE id=?""",
            (
                str(card_id) if card_id else None,
                str(board) if board else None,
                _redact_error(error) if error else None,
                str(incident_id),
            ),
        )
        return bool(cursor.rowcount)


def record_unreachable_card_closed(incident_id: Optional[str], card_id: Optional[str]) -> bool:
    """Drop the row's card stamp once recovery has closed it, so the next success is cheap.

    The ``unreachable_card_error`` is cleared with it: the act's own record is spent the moment
    the card it points at is closed, and a stale pointer would make every later success pay for
    a board read.
    """
    if not incident_id or not card_id:
        return False
    with _transaction() as conn:
        cursor = conn.execute(
            """UPDATE cron_incidents
               SET unreachable_card_id=NULL, unreachable_card_board=NULL, unreachable_card_error=NULL
               WHERE id=? AND unreachable_card_id=?""",
            (str(incident_id), str(card_id)),
        )
        return bool(cursor.rowcount)


def open_unreachable_cards(job_id: str) -> list[dict[str, Any]]:
    """Rows for ``job_id`` that still point at a card the act filed.

    This is the recovery INDEX, and the reason a healthy job costs no board access at all: an
    empty answer here means nothing was ever filed for the job, so there is nothing to close.
    """
    with _transaction() as conn:
        rows = conn.execute(
            """SELECT id, unreachable_card_id, unreachable_card_board, unreachable_card_error
               FROM cron_incidents
               WHERE job_id=? AND unreachable_card_id IS NOT NULL
                     AND unreachable_card_id != ''
               ORDER BY last_seen_at DESC""",
            (str(job_id or ""),),
        ).fetchall()
    return [dict(row) for row in rows]

