"""The ``unreachable_path`` failure class ACTS: one card, routed, then closed on recovery.

Design: card t_fb94d105.

A job whose failure names a path that is no longer there (``No such file or directory``, or the
``127`` exit that says the same thing) is not a job that failed to run -- it is a job that CANNOT
run, and it fails identically on every tick.  ``cron.incidents`` records that, but a ledger row is
not an act: nothing reaches a lane that owns the fix, so the job stays red and the ledger grows.

This module is the missing act, and it is deliberately deterministic - no inference, no clock, no
prose a human must interpret.  From the job and the failure text it

  * CLASSIFIES through :func:`cron.incidents.is_unreachable_path` -- one predicate, one place, so
    the ledger row and the act cannot disagree about what the class is;
  * CAPTURES the offending path (:func:`cron.incidents.captured_path`) and derives its dedupe
    ROOT;
  * ROUTES the card from that root (:func:`owner_for_path`);
  * FILES exactly ONE card per ``(job_id, path_root)`` under an idempotency key the board holds,
    so twelve failures of one script collapse to one card instead of twelve;
  * CLOSES it on the job's next success, exactly as ``close_incidents_for_recovered_job``
    resolves the ledger rows.

WHERE THE CARD GOES (measured 2026-11-01 on this host, not remembered).  The board is the OWNER's
domain board -- ``platform`` / ``research`` / ``financially`` -- which is what
``/opt/hermes_prod/yaan-platform/config/escalation_routing.json -> board_by_domain`` names, and
the only three boards with dispatch enabled (``board_dispatch_enabled``: platform/research/
financially = true).  The fleet's older ``defcon`` intake board was torn down by operator ruling
2026-10-24 ("EXACTLY three boards exist on this host") and measures today as
``is_archived_stub=true`` / ``dispatch_enabled=false``: a card filed there is picked up by
nothing, which is the silent failure this module exists to end, so no filer here targets it.

PRIORITY IS THE BOARD'S.  Filing passes no priority; every live board carries a
``priority_policy`` (``band_birth``) that bands the value at the INSERT, and the kernel refuses an
above-tranche value nobody designated.  A filer that invented one would be over-claiming a tier.

EVERY STEP IS BEST-EFFORT and never raises into the scheduler: a failure to file is recorded on
the incident row (``unreachable_card_error``), so a failure-to-act is itself observable.
"""

from __future__ import annotations

import logging
from pathlib import PurePosixPath
from typing import Any, Optional, Protocol

logger = logging.getLogger(__name__)

#: The board the card lands on, by the owner lane it routes to.  Mirrors
#: ``escalation_routing.json -> board_by_domain`` (and ``lane_by_domain``'s STL seats): a card
#: must land on the board whose dispatcher serves the lane that owns the fix, or it strands.
BOARD_BY_OWNER = {
    "platform-stl": "platform",
    "research-stl": "research",
    "financially-stl": "financially",
}
DEFAULT_BOARD = "platform"

#: The prod root every scheduled job's entry point lives under.  A path outside it has no
#: component we can route on, so it goes to the substrate's owner.
PROD_ROOT = PurePosixPath("/opt/hermes_prod")

#: First component under the prod root -> the lane that owns it.  Everything not named here is
#: ``DEFAULT_OWNER``: the platform domain owns the substrate every lane runs on, so an unknown
#: broken path is platform's to diagnose (a wrong owner is recoverable, an unowned card is not).
OWNER_BY_ROOT = {
    "yaan-cross-markets-research": "research-stl",
    "yaan-trading-strategy": "research-stl",
}

#: The financially domain's trees are prefixed by the domain name and may carry several project
#: directories that rotate; the prefix is the stable part, so it is matched as a prefix.
FINANCIALLY_PREFIX = "financially"
DEFAULT_OWNER = "platform-stl"

#: The card's ``created_by`` and the comment author: the honest filer is the act itself.
CARD_AUTHOR = "cron-unreachable-path"

#: Idempotency key prefix.  Its class (the text before the first ``:``) is what a board's filing
#: policy reads, so the family can be governed - or left inert - as data.
KEY_PREFIX = "cron-unreachable-path:"

#: Statuses a card is closed at; anything else is still owed work.
TERMINAL_STATUSES = ("done", "archived")


class BoardSink(Protocol):
    """The board operations this act needs, so a test can drive the REAL filing path with a stub.

    ``file`` must be idempotent on ``idempotency_key``: filing the same key twice returns the
    card the board already holds (the kernel's own early-return) rather than a second card.
    """

    def file(self, *, board: str, idempotency_key: str, title: str, body: str,
             assignee: str) -> Optional[str]:
        ...

    def close(self, card_id: str, *, board: str, comment: str, result: str) -> bool:
        ...


# ── Classification passthrough ───────────────────────────────────────────────────────────────
# Both predicates live in the incident store, next to the failure types they feed: the ledger row
# and the act must never disagree about what counts as an unreachable path.


def classify(error: str) -> bool:
    """Whether ``error`` is an ``unreachable_path`` failure (delegates to the store)."""
    from cron.incidents import is_unreachable_path

    return is_unreachable_path(error)


def captured_path(error: str) -> Optional[str]:
    """The offending path named by ``error`` (delegates to the store); ``None`` when absent."""
    from cron.incidents import captured_path as _captured_path

    return _captured_path(error)


# ── Routing ──────────────────────────────────────────────────────────────────────────────────


def _under_prod_root(path: str) -> Optional[tuple[str, ...]]:
    """``path``'s components below the prod root, or ``None`` when it is not under it."""
    raw = str(path or "").strip()
    if not raw:
        return None
    try:
        rel = PurePosixPath(raw).relative_to(PROD_ROOT)
    except ValueError:
        # A relative form ("opt/hermes_prod/...") is not something a shell prints, but a job's
        # own tooling might; treat it by components rather than by the path API.
        parts = PurePosixPath(raw).parts
        if len(parts) >= 2 and (parts[0], parts[1]) == ("opt", "hermes_prod"):
            return tuple(parts[2:])
        return None
    return tuple(rel.parts)


def path_root(path: str) -> str:
    """The dedupe root of ``path``: its first two components under the prod root.

    ``/opt/hermes_prod/yaan-platform/cron/tip-guard.sh`` -> ``yaan-platform/cron``.  One card per
    component PAIR, not per script: a renamed entry point is ONE broken unit even when several
    scripts of that pair reach it, and a per-file key would file a card per script that breaks
    for the same reason the next time the unit moves.

    A path outside the prod root has no component we know, so its own first two components stand
    in - stable across ticks, which is all the key needs, and never empty.
    """
    rel = _under_prod_root(path)
    if rel is None:
        rel = PurePosixPath(str(path or "")).parts[1:] or PurePosixPath(str(path or "")).parts
    root = "/".join(part for part in rel[:2] if part)
    return root or "unknown"


def owner_for_path(path: str) -> str:
    """The lane that owns ``path``: the STL of the domain whose tree it sits in."""
    rel = _under_prod_root(path)
    if not rel:
        # Off-root (or a bare "/"): the platform domain owns the substrate.
        return DEFAULT_OWNER
    top = rel[0]
    if top.startswith(FINANCIALLY_PREFIX):
        return "financially-stl"
    return OWNER_BY_ROOT.get(top, DEFAULT_OWNER)


def board_for_owner(owner: str) -> str:
    """The board that dispatches ``owner``'s lane."""
    return BOARD_BY_OWNER.get(str(owner or "").strip(), DEFAULT_BOARD)


def dedupe_key(job_id: str, root: str) -> str:
    """The idempotency key: ONE card per ``(job_id, path_root)``, however often it fails."""
    return f"{KEY_PREFIX}{job_id}:{root}"


# ── The card ─────────────────────────────────────────────────────────────────────────────────


def card_title(job_id: str, job_name: Optional[str], path: str) -> str:
    return f"[UNREACHABLE-PATH] cron job {job_name or job_id} invokes {path}"


def card_body(*, job_id: str, job_name: Optional[str], path: str, owner: str,
              error: str, root: str) -> str:
    """The card body: what broke, who owns it, and the two ways to fix it.

    No clock and no random text, so a re-filing of the same key produces the same body and the
    card carries no bytes the act did not measure.
    """
    from cron.incidents import MAX_ERROR_CHARS

    return (
        f"A scheduled job's failure names a path that does not exist, so the job CANNOT run - "
        f"and it will fail the same way on every tick until the path is restored or the job is "
        f"repointed.\n\n"
        f"- job: `{job_name or job_id}` (`{job_id}`)\n"
        f"- missing path: `{path}`\n"
        f"- path root: `{root}` (one card per job + root, so repeat failures land here)\n"
        f"- owner: `{owner}`\n\n"
        f"MEASURED FAILURE (the job's own error text, redacted):\n\n"
        f"```\n{error[:MAX_ERROR_CHARS]}\n```\n\n"
        f"WHAT THE OWNER DOES: restore `{path}`, or repoint the job at whatever replaced it "
        f"(`hermes cron edit {job_id}` / the job's script inside its own tree). If the script "
        f"was retired on purpose, disable or delete the job instead - a job pointing at a path "
        f"nobody restores is a green-looking schedule that never runs.\n\n"
        f"This card closes itself on the job's next successful run; no separate confirmation "
        f"is needed."
    )


# ── The real sink ────────────────────────────────────────────────────────────────────────────


def _task_status(conn: Any, card_id: str) -> Optional[str]:
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (card_id,)).fetchone()
    if row is None:
        return None
    try:
        return row["status"]
    except (TypeError, IndexError):  # a connection without a Row factory
        return row[0]


class KanbanSink:
    """The board, through the kernel's own filing / comment / completion calls.

    ``connect`` is injectable so a test can drive this class - the REAL calls - against a
    throwaway board instead of the fleet's own store.
    """

    def __init__(self, connect=None):
        self._connect = connect

    def _conn(self, board: str):
        if self._connect is not None:
            return self._connect(board)
        from hermes_cli.kanban_db_connect import connect

        return connect(board=board)

    def file(self, *, board: str, idempotency_key: str, title: str, body: str,
             assignee: str) -> Optional[str]:
        from hermes_cli import kanban_db

        conn = self._conn(board)
        try:
            card_id = kanban_db.create_task(
                conn,
                title=title,
                body=body,
                assignee=assignee,
                created_by=CARD_AUTHOR,
                workspace_kind="scratch",   # never inherit a project/tree: the owner works it
                idempotency_key=idempotency_key,
                board=board,
            )
            # A filing-budget refusal is an ANSWER (a falsy str), not an exception: it names the
            # reason in its own text, which the caller records on the incident row.
            if not card_id:
                logger.warning("unreachable-path filing refused (%s): %s", idempotency_key, card_id)
                return None
            status = _task_status(conn, card_id)
            if status in TERMINAL_STATUSES:
                # The card for this (job, root) was already closed by an earlier recovery and
                # the condition is live AGAIN: the key answered the finished card, so re-open it
                # rather than let a second filing pile up (the estate's measured re-birth loop).
                kanban_db.reopen_task(
                    conn, card_id, actor=CARD_AUTHOR,
                    reason=f"the condition is live again: {idempotency_key}",
                )
            return card_id
        finally:
            conn.close()

    def close(self, card_id: str, *, board: str, comment: str, result: str) -> bool:
        from hermes_cli import kanban_db

        conn = self._conn(board)
        try:
            status = _task_status(conn, card_id)
            if status is None or status in TERMINAL_STATUSES:
                return False
            # Close first, comment second: the note says the card WAS closed, and a close that
            # cannot land leaves no note claiming it did. A card under a live worker claim
            # raises - that worker owns it, so the next successful run tries again.
            done = kanban_db.complete_task(
                conn, card_id, result=result, summary=result, force=False,
            )
            if not done:
                logger.warning(
                    "unreachable-path card %s on %s did not close: %s", card_id, board, done)
                return False
            kanban_db.add_comment(conn, card_id, CARD_AUTHOR, comment)
            return True
        except Exception as exc:
            logger.warning(
                "unreachable-path card %s on %s did not close: %s", card_id, board, exc)
            return False
        finally:
            conn.close()


# ── The act ─────────────────────────────────────────────────────────────────────────────────


def _record(incident_id: Optional[str], card_id: Optional[str], board: Optional[str],
            error: Optional[str]) -> None:
    """Stamp the outcome on the incident row; never let bookkeeping break the act."""
    if not incident_id:
        return
    try:
        from cron.incidents import record_unreachable_card

        record_unreachable_card(incident_id, card_id=card_id, board=board, error=error)
    except Exception as exc:  # the ledger is best-effort here, exactly as it is in the scheduler
        logger.debug("unreachable-path incident recording failed for %s: %s", incident_id, exc)


def act_on_failure(
    job_id: str, error: str, *, job_name: Optional[str] = None,
    incident_id: Optional[str] = None, sink: Optional[BoardSink] = None,
) -> Optional[str]:
    """File (or re-open) the ONE card for this job's unreachable path; return its id.

    Returns ``None`` when the failure names no path, when the filing is refused, or when the
    board is unreachable - the reason is logged and, when an ``incident_id`` was given, stamped
    on the incident row so the failure-to-act is visible where the operator reads the failure.
    """
    job_id = str(job_id or "")
    if not job_id:
        return None
    if not classify(error):
        # Defensive: the caller already classified. Acting on anything else would file cards for
        # classes that have their own owners.
        return None
    path = captured_path(error)
    if not path:
        _record(incident_id, None, None,
                "unreachable_path with no path in the failure text: nothing to route")
        logger.warning(
            "unreachable_path failure for job %s names no path; no card filed", job_id)
        return None

    root = path_root(path)
    owner = owner_for_path(path)
    board = board_for_owner(owner)
    from cron.incidents import _redact_error

    body = card_body(job_id=job_id, job_name=job_name, path=path, owner=owner,
                     error=_redact_error(error), root=root)
    try:
        card_id = (sink or KanbanSink()).file(
            board=board,
            idempotency_key=dedupe_key(job_id, root),
            title=card_title(job_id, job_name, path),
            body=body,
            assignee=owner,
        )
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        _record(incident_id, None, board, reason[:500])
        logger.warning(
            "unreachable_path act failed for job %s (%s): %s", job_id, dedupe_key(job_id, root), exc)
        return None
    if not card_id:
        _record(incident_id, None, board, "the board filed no card (refused: see the log line)")
        return None
    _record(incident_id, card_id, board, None)
    return card_id


def close_cards_for_recovered_job(
    job_id: str, *, sink: Optional[BoardSink] = None,
) -> int:
    """Close the cards this job's unreachable-path incidents filed; return how many closed.

    The incident rows are the index (nothing scans the boards), and a job that never recorded a
    card costs ONE local read and no board access at all - which is what keeps a healthy fleet's
    every successful run cheap.
    """
    job_id = str(job_id or "")
    if not job_id:
        return 0
    try:
        from cron.incidents import open_unreachable_cards

        cards = open_unreachable_cards(job_id)
    except Exception as exc:
        logger.debug("unreachable-path recovery skipped for %s: %s", job_id, exc)
        return 0
    if not cards:
        return 0

    sink = sink or KanbanSink()
    result = (
        f"closed by cron: job {job_id} ran successfully after the unreachable-path failure"
    )
    comment = (
        f"The job ran successfully on its next tick, so the condition this card files is over. "
        f"Closed automatically by `{CARD_AUTHOR}`; no confirmation needed."
    )
    closed = 0
    for row in cards:
        card_id = row.get("unreachable_card_id")
        board = row.get("unreachable_card_board") or DEFAULT_BOARD
        if not card_id:
            continue
        try:
            if sink.close(card_id, board=board, comment=comment, result=result):
                closed += 1
                try:
                    from cron.incidents import record_unreachable_card_closed

                    record_unreachable_card_closed(row.get("id"), card_id)
                except Exception as exc:
                    logger.debug("unreachable-path close not recorded for %s: %s", card_id, exc)
        except Exception as exc:
            logger.warning(
                "unreachable-path recovery failed for card %s on %s: %s", card_id, board, exc)
    return closed
