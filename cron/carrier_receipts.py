"""The carrier's OWN receipt: written by the run, settled in three places, acted on.

Design: card ``t_9fbcfe35`` (``DESIGN-t_9fbcfe35-carrier-receipt-and-restart-notice.md``),
unit U1.  Rulings R1-R3.

WHY THIS EXISTS.  A ``no_agent`` cron carrier's only output channel was a pipe held by the
scheduler.  When the gateway restarted at 02:43:23 mid-run, the new process's startup sweep found
the owner gone, wrote ``status='unknown'`` on the attempt and logged one warning - no incident, no
notice, no ``last_run_at`` advance, no card - while the run itself had a durable
``completed / verdict=fail`` row in ``yoyoflow-state.db`` the whole time.  Silence is the defect:
the attempt's terminal state belonged to the scheduler, and the scheduler died.

This module owns the receipt the CARRIER writes (R1) and the settlement of it (R2):

  * :func:`receipt_path` / :func:`write_receipt` / :func:`read_receipt` / :func:`iter_receipts` -
    ``$HERMES_HOME/cron/receipts/<execution_id>.json``, atomic and ``0600``.  One file per
    attempt, so the receipt survives the scheduler, the gateway and a host reboot.  ``run_id`` /
    ``verdict`` / ``published_path`` are the carrier's own OPTIONAL declarations; this module
    NEVER infers them from prose (only ``exit_code`` and the declared fields are verdicts).
  * :func:`reconcile_receipts` - idempotent, safe every tick, free when the directory is empty.
    It settles each receipt into the execution ledger, the job row (``mark_job_run`` advances
    ``last_run_at``) and a consumer-visible notice, and when the notice cannot be delivered it
    files exactly ONE card instead of losing the run.  Every step is recorded on the receipt
    before the next one starts, so a crash between any two writes re-settles cleanly on the next
    tick instead of double-noticing or double-filing.

R3 IS THE ONE THAT BIT US.  A receipt whose ``status`` is ``running`` and whose carrier pid is
still live is SKIPPED - never terminalized, never noticed.  A run that is still executing is not a
run whose outcome is unknown; treating it as one is exactly the premature ``unknown`` of 02:43.
The liveness test reuses the ledger's own semantics (``cron.executions._owner_is_live``), so a pid
that was reused by an unrelated process cannot masquerade as the carrier.

EVERY STEP IS BEST-EFFORT and never raises into the scheduler: a reconcile tick that fails logs
and leaves the receipt in place for the next tick, and a failure to file the card is recorded on
the incident row (``unreachable_card_error``) exactly as ``cron/unreachable_path.py`` records its
own - a failure to act must itself be observable.

Kill switch: nothing here runs unless a call site calls :func:`reconcile_receipts`; removing that
seam restores today's behaviour byte for byte.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterator, Optional, Protocol

logger = logging.getLogger(__name__)

#: The receipt schema version.  A record without ``schema`` is still read (older/foreign writers
#: are not refused outright): the fields this module needs are checked individually.
SCHEMA = 1

RECEIPTS_SUBDIR = "receipts"
#: Test seam, mirroring ``cron.executions.EXECUTIONS_FILE``: when set, receipts live here instead
#: of ``$HERMES_HOME/cron/receipts``.  Never set in production.
RECEIPTS_DIR: Optional[Path] = None

STATUS_RUNNING = "running"
STATUS_EXITED = "exited"

#: The error class of a carrier that died without writing its terminal receipt.
CARRIER_EXITED_WITHOUT_RECEIPT = "carrier_exited_without_receipt"

#: Attempt states whose settle belongs to the ATTEMPT'S OWN FIRER.  ``unknown`` is deliberately
#: NOT here: an unknown row is exactly what a departed owner leaves behind and what this module
#: exists to settle (the ledger's ``settle_carrier_receipt`` accepts it for the same reason).
FIRER_FINISHED_STATES = frozenset({"completed", "failed", "cancelled", "missed"})

#: The card's idempotency-key family.  One card per ``(job_id, execution_id)``, so a crash-looping
#: reconciler cannot bury the board and a re-run of the same attempt lands on the same card.
CARD_KEY_PREFIX = "cron-carrier-orphan:"
#: The card's ``created_by``: the honest filer is the act itself.
CARD_AUTHOR = "cron-carrier-receipts"

#: The substrate owns an execution nobody reconciled, so an unroutable receipt goes to platform.
DEFAULT_OWNER = "platform-stl"

#: Bounds on captured text.  A carrier's stderr can be megabytes; a card and a chat notice must
#: not be.
STDERR_CARD_CHARS = 2000
STDERR_NOTICE_CHARS = 1200


class BoardSink(Protocol):
    """The board operation this act needs (structurally :class:`cron.unreachable_path.KanbanSink`).

    ``file`` must be idempotent on ``idempotency_key``: filing the same key twice returns the card
    the board already holds rather than a second card.
    """

    def file(self, *, board: str, idempotency_key: str, title: str, body: str,
             assignee: str) -> Optional[str]:
        ...


# ── Paths ────────────────────────────────────────────────────────────────────────────────────


def receipts_dir() -> Path:
    """The directory holding this profile's receipts, resolved at CALL time.

    Resolved late (not at import) for the same reason ``cron.executions`` resolves its ledger at
    transaction time: a dashboard operation that temporarily enters another profile must read and
    write that profile's receipts, not the ones frozen at import.
    """
    if RECEIPTS_DIR is not None:
        return Path(RECEIPTS_DIR)
    from hermes_constants import get_hermes_home

    return get_hermes_home().resolve() / "cron" / RECEIPTS_SUBDIR


def receipt_path(execution_id: str) -> Path:
    """``$HERMES_HOME/cron/receipts/<execution_id>.json``.

    The execution id is a hex token minted by the ledger; anything that could escape the receipts
    directory is refused rather than silently written somewhere else.
    """
    eid = str(execution_id or "").strip()
    if not eid or eid in (".", "..") or "/" in eid or "\\" in eid or "\x00" in eid:
        raise ValueError(f"not an execution id: {execution_id!r}")
    return receipts_dir() / f"{eid}.json"


# ── Schema ───────────────────────────────────────────────────────────────────────────────────

#: Every field the schema names, with the value a carrier that has not reached that moment yet
#: leaves behind.  ``run_id`` / ``verdict`` / ``published_path`` stay ``None`` until the carrier
#: declares them - absent is legal and is never inferred.
_DEFAULTS: dict[str, Any] = {
    "schema": SCHEMA,
    "job_id": "",
    "execution_id": "",
    "profile": "",
    "status": STATUS_RUNNING,
    "carrier_pid": None,
    "carrier_started_at": None,
    "started_at": None,
    "finished_at": None,
    "exit_code": None,
    "timeout_s": None,
    "stdout_path": None,
    "stderr_path": None,
    "run_id": None,
    "verdict": None,
    "published_path": None,
}

#: Keys this module adds on the record it settles; they are the RECONCILER's own bookkeeping and
#: never carrier-declared.  ``settlement`` is the crash journal: each completed write is recorded
#: before the next one is attempted, so a retry resumes instead of repeating.
_RECONCILER_KEYS = ("settlement",)


def new_receipt(**fields: Any) -> dict[str, Any]:
    """A schema-complete receipt record.

    The supervisor (U2) builds the ``running`` record before ``exec`` and rewrites it ``exited``
    when the carrier ends; keeping the shape in one place is the whole point of owning the schema.
    Unknown keys are preserved, so a caller can carry extra carrier-declared facts.
    """
    record = dict(_DEFAULTS)
    record.update({key: value for key, value in fields.items() if value is not None})
    if not record.get("execution_id"):
        raise ValueError("a receipt needs an execution_id")
    record["schema"] = _as_int(record.get("schema")) or SCHEMA
    return record


def _normalise(record: dict[str, Any]) -> dict[str, Any]:
    """Fill the schema's fields without inventing values for the optional declarations."""
    out = dict(_DEFAULTS)
    out.update(record)
    out["schema"] = _as_int(out.get("schema")) or SCHEMA
    return out


def _as_int(value: Any) -> Optional[int]:
    """``value`` as an int, or ``None`` when it is absent/junk (never raises)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ── Read / write ─────────────────────────────────────────────────────────────────────────────


def write_receipt(record: dict[str, Any]) -> Path:
    """Atomically write *record* to its own path, ``0600``, directory ``fsync``ed.

    Mode ``0600`` from creation: a receipt can name a run id, a stderr tail and artifact paths on
    a machine with several accounts, and the ledger's own files are no more readable than that.
    """
    from utils.files import atomic_json_write

    path = receipt_path(str(record.get("execution_id") or ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(path, _normalise(record), mode=0o600, fsync_dir=True)
    return path


def read_receipt(execution_id: str) -> Optional[dict[str, Any]]:
    """The receipt for *execution_id*, or ``None`` when there is none (or it is unreadable)."""
    try:
        path = receipt_path(execution_id)
    except ValueError:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("carrier receipt %s is unreadable (%s); left in place", path, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("carrier receipt %s is not a JSON object; left in place", path)
        return None
    return data


def delete_receipt(execution_id: str) -> bool:
    """Remove the receipt; True when one was there.  Best-effort - a failure is logged, never
    raised, because deletion must never be the thing that breaks a tick."""
    try:
        path = receipt_path(execution_id)
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except (OSError, ValueError) as exc:
        logger.warning("carrier receipt %s could not be removed: %s", execution_id, exc)
        return False


def iter_receipts() -> Iterator[dict[str, Any]]:
    """Every readable receipt, in a stable (filename) order.

    Cheap when the directory is empty or absent: one ``listdir`` and no store access at all, which
    is what keeps a healthy fleet's every tick free of this module's cost.
    """
    directory = receipts_dir()
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError as exc:
        logger.warning("carrier receipt directory %s is unreadable: %s", directory, exc)
        return
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("carrier receipt %s is unreadable (%s); left in place", path, exc)
            continue
        if isinstance(data, dict):
            yield data


# ── Routing / text helpers ──────────────────────────────────────────────────────────────────


def _run_label(record: dict[str, Any]) -> str:
    """The identity a human reads for this run: the carrier's declared run id, else the attempt.

    ``never invent a run id`` is a standing rule here, so an absent declaration falls back to the
    execution id and SAYS SO where the text is read (the notice and the card both spell it out).
    """
    run_id = str(record.get("run_id") or "").strip()
    return run_id or str(record.get("execution_id") or "").strip() or "unknown"


def _run_label_note(record: dict[str, Any]) -> str:
    """The parenthetical that keeps ``_run_label`` honest about a non-declared run id."""
    return "" if str(record.get("run_id") or "").strip() else " (not declared by the carrier)"


def owner_for_receipt(record: dict[str, Any]) -> str:
    """The lane that owns this unreconciled execution.

    Routed by the artifact the carrier declared, through the SAME table
    ``cron.unreachable_path`` owns (a research artifact routes to research-stl, a financially one
    to financially-stl).  With no declared artifact there is no tree to route on, and the
    substrate owns an execution nobody reconciled: ``platform-stl``.
    """
    from cron.unreachable_path import owner_for_path

    try:
        return owner_for_path(str(record.get("published_path") or "")) or DEFAULT_OWNER
    except Exception:
        return DEFAULT_OWNER


def _board_for(owner: str) -> str:
    from cron.unreachable_path import board_for_owner

    return board_for_owner(owner)


def _redact(text: str) -> str:
    """Scrub secrets before prose leaves this module (a notice is a chat message; a card is a
    durable record).  Best-effort: redaction never fails the act."""
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(text, force=True)
    except Exception:
        return text


def _tail(path_value: Any, limit: int) -> str:
    """The LAST *limit* characters of the file at *path_value*, redacted; ``""`` when unreadable.

    The tail, not the head: a carrier that dies writes its reason last.
    """
    raw = str(path_value or "").strip()
    if not raw:
        return ""
    try:
        path = Path(raw)
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > limit:
                handle.seek(size - limit)
            data = handle.read()
    except (OSError, ValueError):
        return ""
    return _redact(data.decode("utf-8", "replace")).strip()


def _captured_stdout(record: dict[str, Any]) -> str:
    """The carrier's own report text, as captured to ``stdout_path``."""
    raw = str(record.get("stdout_path") or "").strip()
    if not raw:
        return ""
    try:
        return Path(raw).read_text(encoding="utf-8", errors="replace").strip()
    except (OSError, ValueError):
        return ""


# ── Liveness (R3) ───────────────────────────────────────────────────────────────────────────


def _carrier_is_live(record: dict[str, Any]) -> bool:
    """Whether the attempt's carrier is still running, using the ledger's own death semantics.

    Deliberately fails SAFE: when liveness cannot be proved either way the carrier is treated as
    live and nothing is settled.  Rewriting a live run is the 02:43 bug; deferring a dead one by a
    tick costs nothing.
    """
    try:
        pid = int(record.get("carrier_pid"))
    except (TypeError, ValueError):
        return False
    started_at = _as_int(record.get("carrier_started_at"))
    try:
        from cron.executions import _owner_is_live

        if started_at is None:
            # No start-time fingerprint: plain pid existence.  ``_owner_is_live``'s None branch
            # answers "is this MY pid", which is the ledger owner's question, not the carrier's.
            from gateway.status import _pid_exists

            return bool(_pid_exists(pid))
        return bool(_owner_is_live(pid, started_at))
    except Exception as exc:  # cannot prove death -> must not rewrite state
        logger.debug("carrier receipt %s: liveness probe failed (%s); treating as live",
                     record.get("execution_id"), exc)
        return True


# ── Verdict (never inferred from prose) ────────────────────────────────────────────────────


def _verdict(record: dict[str, Any]) -> Optional[tuple[bool, Optional[str]]]:
    """``(success, error)`` for a settleable receipt, or ``None`` while it must be left alone.

    Only ``exit_code`` and the carrier's declared fields decide.  A ``running`` receipt settles as
    failed only once its carrier is PROVABLY gone - which is what ``carrier_exited_without_receipt``
    names.
    """
    status = str(record.get("status") or "")
    if status == STATUS_RUNNING:
        if _carrier_is_live(record):
            return None
        return False, (
            f"{CARRIER_EXITED_WITHOUT_RECEIPT}: run {_run_label(record)} died with no "
            "terminal receipt"
        )
    if status == STATUS_EXITED:
        code = _as_int(record.get("exit_code"))
        if code is None:
            # An exited receipt with no exit code cannot be judged; leaving it (and logging) is
            # safer than inventing an outcome.
            logger.warning("carrier receipt %s exited without an exit code; left in place",
                           record.get("execution_id"))
            return None
        if code == 0:
            return True, None
        return False, f"carrier exited with code {code} (run {_run_label(record)})"
    logger.warning("carrier receipt %s has unknown status %r; left in place",
                   record.get("execution_id"), status)
    return None


# ── Settlement: the ledger ──────────────────────────────────────────────────────────────────


def _settle_ledger(execution_id: str, job_id: str, *, success: bool,
                   error: Optional[str]) -> bool:
    """Write the attempt's terminal row; True once the ledger proves it terminal.

    ``cron.executions.settle_carrier_receipt`` is unit U3's function.  It is imported LATE and
    looked up every call, exactly as ``cron/unreachable_path.py`` imports late: a scheduler daemon
    that outlives an on-disk upgrade already has the old ``cron.executions`` cached, and a missing
    name must degrade to the existing receipt-recovery path (which carries the same contract:
    settle ``claimed|running|unknown``, refuse a live owner, never rewrite a terminal row) rather
    than fail this unit while U3 lands.
    """
    from cron import executions

    try:
        settle = getattr(executions, "settle_carrier_receipt", None)
        if callable(settle):
            settle(execution_id, job_id, success=success, error=error, delivery_outcome=None)
        else:
            executions.recover_receipted_execution(
                execution_id, job_id, success=success, error=error, delivery_outcome=None)
    except Exception as exc:
        logger.warning("carrier receipt %s: ledger settle failed: %s", execution_id, exc)
        return False
    return _ledger_is_terminal(execution_id) or _execution_row(execution_id) is None


def _execution_row(execution_id: str) -> Optional[dict[str, Any]]:
    from cron import executions

    try:
        return executions.get_execution(execution_id)
    except Exception as exc:
        logger.warning("carrier receipt %s: ledger read failed: %s", execution_id, exc)
        return None


def _ledger_is_terminal(execution_id: str) -> bool:
    row = _execution_row(execution_id)
    return row is not None and str(row.get("status") or "") in FIRER_FINISHED_STATES


def _row_owner_live(execution_id: str, job_id: str) -> bool:
    """True while the attempt's own firer can still finish it, so this module must not act.

    This is the normal in-flight case: the supervisor has written an ``exited`` receipt but the
    scheduler is still delivering and settling.  Acting now would duplicate the scheduler's own
    notice and fight it for the ledger row.
    """
    from cron import executions

    try:
        return bool(executions.execution_owner_live(execution_id, job_id))
    except Exception as exc:
        logger.debug("carrier receipt %s: owner liveness check failed (%s); skipped this tick",
                     execution_id, exc)
        return True


# ── Settlement: the job row ────────────────────────────────────────────────────────────────


def _project_job_row(job_id: str, execution_id: str, execution_row: Optional[dict[str, Any]],
                     record: dict[str, Any], *, success: bool, error: Optional[str]) -> str:
    """Advance the job row (``last_run_at``) unless a newer fire owns it; return what happened.

    ``mark_job_run`` is DoD item 2 and it happens regardless of delivery: an outcome that is not
    projected onto the job row leaves the schedule looking like it never ran.  Two fences keep it
    from stomping a NEWER fire:

      * a ``fire_claim`` that is still live means another fire holds the job right now - its own
        settle owns the row;
      * a ``last_run_at`` at or after this attempt's claim means a later run already projected
        the row, so writing this (older) outcome would move the schedule backwards.

    Anything else is projected with ``expected_fire_owner`` set to the (now stale) claim's owner,
    so a takeover that happens between this read and the write refuses the completion instead of
    applying a stale one.
    """
    from cron import jobs

    try:
        job = jobs.get_job(job_id)
    except Exception as exc:
        logger.warning("carrier receipt %s: job %s could not be read: %s",
                       execution_id, job_id, exc)
        return "skipped:read-failed"
    if not job:
        logger.warning("carrier receipt %s names job %s, which is not in the store",
                       execution_id, job_id)
        return "skipped:no-job"

    claim = job.get("fire_claim")
    owner = str(claim.get("by") or "") if isinstance(claim, dict) else ""
    if owner:
        try:
            from cron.constants import FIRE_CLAIM_TTL_SECONDS
            from hermes_time import now as _now

            if jobs._claim_is_live(claim, _now(), FIRE_CLAIM_TTL_SECONDS):
                logger.info(
                    "carrier receipt %s: job %s is claimed by a live fire (%s); ledger settled, "
                    "job row left to it", execution_id, job_id, owner)
                return "skipped:newer-fire"
        except Exception as exc:
            # Fail safe: an unreadable claim is not proof that no newer fire exists.
            logger.debug("carrier receipt %s: claim check failed (%s)", execution_id, exc)
            return "skipped:claim-unreadable"

    claimed_at = str((execution_row or {}).get("claimed_at") or "")
    last_run_at = str(job.get("last_run_at") or "")
    if claimed_at and last_run_at:
        try:
            from datetime import datetime

            if datetime.fromisoformat(last_run_at) >= datetime.fromisoformat(claimed_at):
                logger.info(
                    "carrier receipt %s: job %s already recorded a run at %s (this attempt fired "
                    "at %s); job row left alone", execution_id, job_id, last_run_at, claimed_at)
                return "skipped:newer-fire"
        except (TypeError, ValueError):
            # A legacy naive/manual stamp is not a comparison we can trust; the live-claim
            # fence above already ran, so projecting is the lesser risk.
            pass

    delivery_error = None
    settlement = record.get("settlement")
    if isinstance(settlement, dict):
        delivery_error = settlement.get("notice_error")
    try:
        marked = jobs.mark_job_run(
            job_id,
            success,
            error,
            delivery_error=delivery_error,
            expected_fire_owner=owner or None,
            execution_id=execution_id,
        )
    except Exception as exc:
        logger.warning("carrier receipt %s: job row mark failed: %s", execution_id, exc)
        return "skipped:mark-failed"
    if not marked:
        logger.warning("carrier receipt %s: job %s refused the completion (fire claim moved)",
                       execution_id, job_id)
        return "skipped:not-marked"
    return "marked"


# ── Settlement: the notice ─────────────────────────────────────────────────────────────────


def failure_notice_text(job: dict[str, Any], record: dict[str, Any],
                        error: Optional[str] = None) -> str:
    """The failure notice: names the run id, the artifact (or its WITHHELD state), and the cause.

    Deterministic text, no inference: ``published_path`` empty is stated as ``publication
    withheld`` in those words, because a consumer must be able to tell "the carrier published
    nothing" from "the notice forgot to mention it".
    """
    name = job.get("name") or job.get("id") or record.get("job_id") or "cron job"
    run = _run_label(record)
    lines = [
        f"Cron carrier '{name}' run {run} FAILED - its outcome never reached a consumer",
        "",
        f"- job: `{record.get('job_id')}` ({job.get('id')})",
        f"- execution: `{record.get('execution_id')}`",
        f"- run id: `{run}`{_run_label_note(record)}",
        f"- attempt status: `{record.get('status')}`"
        + (f", exit code {record.get('exit_code')}" if record.get("status") == STATUS_EXITED else ""),
    ]
    published = str(record.get("published_path") or "").strip()
    if published:
        lines.append(f"- artifact: `{published}`")
    else:
        lines.append("- artifact: publication withheld (the carrier declared no published artifact)")
    verdict = str(record.get("verdict") or "").strip()
    if verdict:
        lines.append(f"- carrier verdict: `{verdict}`")
    tail = _tail(record.get("stderr_path"), STDERR_NOTICE_CHARS)
    if tail:
        lines += ["", "captured stderr (tail):", "```", tail, "```"]
    if error:
        lines += ["", "cause:", "```", str(error), "```"]
    lines += ["", "The attempt was settled from the carrier's own receipt."]
    return "\n".join(lines)


def _deliver_notice(job: dict[str, Any], text: str, *, for_failure: bool,
                    adapters=None, loop=None) -> Optional[str]:
    """Send *text* through the job's normal lane; ``None`` when delivery settled.

    The single delivery seam.  Late import for the same reason every other cron import here is
    late: the scheduler owns the heavy delivery module and this one is loaded by it.

    ``for_failure=True`` resolves ``failure_deliver`` (the job's failure lane) and is the same
    entry ``_deliver_crash_failure`` uses.  The incident ACK gate is deliberately not applied: ack
    suppression is per failure SIGNATURE, and a carrier orphan is per EXECUTION - there is nothing
    acked for this execution to suppress, and the receipt's own settlement marker (not a mutable
    cooldown) is what guarantees one notice per execution id.
    """
    from cron.scheduler_delivery import _deliver_result

    return _deliver_result(job, text, adapters=adapters, loop=loop, for_failure=for_failure)


def _send_notice(job: dict[str, Any], record: dict[str, Any], *, success: bool,
                 error: Optional[str], adapters=None, loop=None) -> tuple[str, Optional[str]]:
    """Deliver the receipt's outcome; return ``(outcome, delivery_error)``.

    ``outcome`` is ``delivered``, ``silent`` (a success whose carrier reported nothing - the same
    semantic as an empty stdout on the normal ``no_agent`` path) or ``failed``.  Exceptions are
    converted to a delivery error: a delivery that raises must still file the card, not abort the
    reconcile.
    """
    try:
        if success:
            text = _captured_stdout(record)
            if not text.strip():
                logger.info("carrier receipt %s: empty captured stdout - silent success",
                            record.get("execution_id"))
                return "silent", None
            delivery_error = _deliver_notice(
                job, text, for_failure=False, adapters=adapters, loop=loop)
        else:
            delivery_error = _deliver_notice(
                job, failure_notice_text(job, record, error), for_failure=True,
                adapters=adapters, loop=loop)
    except Exception as exc:
        return "failed", f"{type(exc).__name__}: {exc}"
    if delivery_error:
        return "failed", str(delivery_error)
    return "delivered", None


# ── The ACT ────────────────────────────────────────────────────────────────────────────────


def _orphan_card_body(*, job: dict[str, Any], record: dict[str, Any], owner: str,
                      error: Optional[str], delivery_error: Optional[str]) -> str:
    """The card body: what ran, what it said, where its artifact is, and what the owner does."""
    run = _run_label(record)
    published = str(record.get("published_path") or "").strip()
    artifact = f"`{published}`" if published else "**publication withheld** (no carrier-declared artifact)"
    stderr = _tail(record.get("stderr_path"), STDERR_CARD_CHARS) or "(nothing captured)"
    verdict = str(record.get("verdict") or "").strip() or "not declared"
    lines = [
        "A scheduled carrier finished (or died) and its outcome never reached a consumer: the "
        "notice could not be delivered, so the substrate is filing this card instead of losing "
        "the run.",
        "",
        f"- job: `{job.get('name') or record.get('job_id')}` (`{record.get('job_id')}`)",
        f"- execution: `{record.get('execution_id')}`",
        f"- attempt status: `{record.get('status')}`"
        + (f", exit code `{record.get('exit_code')}`" if record.get("status") == STATUS_EXITED else ""),
        f"- run id: `{run}`{_run_label_note(record)}",
        f"- carrier verdict: `{verdict}`",
        f"- artifact: {artifact}",
        f"- owner: `{owner}`",
        f"- notice delivery: {delivery_error or 'no lane resolved for this job'}",
    ]
    if error:
        lines += ["", "settled cause:", "```", str(error), "```"]
    lines += ["", "captured stderr (tail):", "```", stderr, "```", "",
              "this run's outcome never reached a consumer; reconcile it"]
    return "\n".join(lines)


def _incident_for(job: dict[str, Any], record: dict[str, Any],
                  error: Optional[str]) -> Optional[str]:
    """Open/refresh the incident row this execution's failure belongs to; ``None`` if unavailable."""
    try:
        from cron.incidents import upsert_incident

        incident_id, _is_new = upsert_incident(
            str(record.get("job_id") or job.get("id") or ""),
            str(error or f"{CARRIER_EXITED_WITHOUT_RECEIPT}: run {_run_label(record)}"),
            job_name=job.get("name"),
            output_file=_exec_output_file(record),
        )
        return incident_id
    except Exception as exc:
        logger.debug("carrier receipt %s: incident write failed (act unaffected): %s",
                     record.get("execution_id"), exc)
        return None


def _exec_output_file(record: dict[str, Any]) -> Optional[str]:
    raw = str(record.get("stdout_path") or "").strip()
    return raw or None


def _record_card_error(incident_id: Optional[str], board: str, error: str) -> None:
    """Record a FAILURE to file on the incident row; never break the act.

    Only the failure side is stamped.  ``record_unreachable_card``'s ``unreachable_card_id`` is
    also ``cron.unreachable_path``'s close-on-recovery index, and a carrier orphan must NOT be
    closed by the job's next success: a later run does not reconcile the run whose outcome was
    lost.  So a filed card is its own record, and only "the act could not happen" is written where
    ``cron/incidents.py`` already looks for it.
    """
    if not incident_id:
        return
    try:
        from cron.incidents import record_unreachable_card

        record_unreachable_card(incident_id, card_id=None, board=board, error=error)
    except Exception as exc:
        logger.debug("carrier receipt card failure not recorded for %s: %s", incident_id, exc)


def file_orphan_card(job: dict[str, Any], record: dict[str, Any], *,
                     error: Optional[str] = None,
                     delivery_error: Optional[str] = None,
                     sink: Optional[BoardSink] = None) -> Optional[str]:
    """File (or find) the ONE card for an execution that reached no consumer; return its id.

    Idempotent by construction: the key is ``cron-carrier-orphan:<job_id>:<execution_id>`` and the
    board returns the card it already holds for the same key, so a reconciler that retries after a
    crash lands on the same card.  A filing that is refused or raises is recorded on the incident
    row (``unreachable_card_error``) so a failure to act is itself observable.
    """
    from cron.unreachable_path import KanbanSink

    job_id = str(record.get("job_id") or "")
    execution_id = str(record.get("execution_id") or "")
    if not job_id or not execution_id:
        return None
    owner = owner_for_receipt(record)
    board = _board_for(owner)
    key = f"{CARD_KEY_PREFIX}{job_id}:{execution_id}"
    title = (
        f"[CARRIER-ORPHAN] cron job {job.get('name') or job_id} run {_run_label(record)} "
        "reached no consumer"
    )
    body = _orphan_card_body(job=job, record=record, owner=owner, error=error,
                             delivery_error=delivery_error)
    incident_id = _incident_for(job, record, error)
    try:
        card_id = (sink or KanbanSink()).file(
            board=board,
            idempotency_key=key,
            title=title,
            body=body,
            assignee=owner,
        )
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        _record_card_error(incident_id, board, reason[:500])
        logger.warning("carrier orphan card for %s could not be filed: %s", key, exc)
        return None
    if not card_id:
        _record_card_error(incident_id, board,
                           "the board filed no card (refused: see the log line)")
        logger.warning("carrier orphan card for %s was refused", key)
        return None
    logger.info("carrier orphan card %s filed for %s", card_id, key)
    return str(card_id)


# ── The reconciler ─────────────────────────────────────────────────────────────────────────


def reconcile_receipts(*, adapters=None, loop=None,
                       sink: Optional[BoardSink] = None) -> int:
    """Settle every receipt that is due; return how many reached a terminal settlement.

    Idempotent and safe every tick.  ``sink`` is the board seam tests drive (the same shape
    ``cron.unreachable_path.act_on_failure`` accepts); ``adapters``/``loop`` are the live gateway
    delivery handles.  Nothing here raises: one bad receipt is logged and left for the next tick.
    """
    settled = 0
    for record in iter_receipts():
        try:
            if _reconcile_one(record, adapters=adapters, loop=loop, sink=sink):
                settled += 1
        except Exception as exc:
            logger.warning(
                "carrier receipt %s not settled this tick: %s",
                record.get("execution_id"), exc, exc_info=True)
    return settled


def _settlement(record: dict[str, Any]) -> dict[str, Any]:
    state = record.get("settlement")
    if not isinstance(state, dict):
        state = {}
        record["settlement"] = state
    return state


def _reconcile_one(record: dict[str, Any], *, adapters=None, loop=None,
                   sink: Optional[BoardSink] = None) -> bool:
    """One receipt, in order: ledger -> job row -> notice -> act -> delete.

    Returns True only when the receipt reached a terminal settlement (and was removed).  Each
    completed write is journalled in ``record['settlement']`` and persisted before the next one is
    attempted, so a retry resumes at the failed step.
    """
    execution_id = str(record.get("execution_id") or "").strip()
    job_id = str(record.get("job_id") or "").strip()
    if not execution_id or not job_id:
        logger.warning("carrier receipt %r names no execution/job; left in place", execution_id)
        return False
    if record.get("schema") is not None:
        schema = _as_int(record.get("schema"))
        if schema is not None and schema > SCHEMA:
            logger.warning(
                "carrier receipt %s uses schema %s (this code knows %s); left in place",
                execution_id, schema, SCHEMA)
            return False

    state = _settlement(record)
    row = _execution_row(execution_id)

    # Already settled elsewhere, and we never touched it: the firer (or another reconciler) owns
    # this attempt.  Nothing to act on, nothing to keep.
    if row is not None and _row_status(row) in FIRER_FINISHED_STATES and not state.get("ledger"):
        delete_receipt(execution_id)
        return False
    # The attempt is still owned by its firer: leave everything alone this tick.
    if not state.get("ledger") and _row_owner_live(execution_id, job_id):
        return False

    # 1. LEDGER - the terminal verdict, written before anything a human sees.
    if not state.get("ledger"):
        verdict = _verdict(record)
        if verdict is None:
            return False
        success, error = verdict
        if not _settle_ledger(execution_id, job_id, success=success, error=error):
            return False
        state.update({"ledger": True, "success": success, "error": error})
        write_receipt(record)
    else:
        success = bool(state.get("success"))
        error = state.get("error")

    # 2. JOB ROW - last_run_at advances regardless of what the notice does.
    if state.get("job") is None:
        state["job"] = _project_job_row(
            job_id, execution_id, row, record, success=success, error=error)
        write_receipt(record)

    # 3. NOTICE - the carrier's own report on success, the failure shape on failure.
    try:
        job = _job_for_notice(job_id)
    except Exception as exc:
        logger.warning("carrier receipt %s: job %s unreadable for the notice: %s",
                       execution_id, job_id, exc)
        return False
    if not job:
        # The record is gone (a one-shot that retired itself, or an operator deleted it).  The run
        # still happened, so its outcome must still reach somewhere: a job-less notice resolves no
        # lane, which routes it straight to the card act below.
        logger.warning("carrier receipt %s names job %s, which is not in the store",
                       execution_id, job_id)
        job = {"id": job_id}
    if state.get("notice") is None:
        outcome, delivery_error = _send_notice(
            job, record, success=success, error=error, adapters=adapters, loop=loop)
        state.update({"notice": outcome, "notice_error": delivery_error})
        write_receipt(record)

    # 4. ACT - an undelivered outcome is a card, never silence.
    if state.get("notice") == "failed" and not state.get("card"):
        card_id = file_orphan_card(
            job, record, error=error, delivery_error=state.get("notice_error"), sink=sink)
        if card_id:
            state["card"] = card_id
            write_receipt(record)

    # 5. DELETE - only once the ledger is terminal AND the outcome reached somewhere: a delivered
    #    notice, or a card.  A refused filing keeps the receipt, and the next tick retries it.
    notice_ok = state.get("notice") in ("delivered", "silent")
    card_filed = bool(state.get("card"))
    if not (notice_ok or card_filed):
        return False
    delete_receipt(execution_id)
    return True


def _row_status(row: dict[str, Any]) -> str:
    return str(row.get("status") or "")


def _job_for_notice(job_id: str) -> Optional[dict[str, Any]]:
    from cron import jobs

    job = jobs.get_job(job_id)
    return job if isinstance(job, dict) else None
