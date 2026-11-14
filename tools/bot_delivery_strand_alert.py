"""Growth alarm for the stranded peer-delivery count (DoD 4; card t_703af8d1).

The ``peer-delivery`` project exists because an UNALERTED strand count let a lane
close a board while production still stranded messages: seven ``expired`` records
sat in a store nothing watched and were found only because someone hand-measured
twice in one night. This module is the recurrence guard. It runs inside the LIVE
gateway's own delivery sweep -- trigger 3, :func:`sweep_loop` in
``gateway/platforms/api_server_bot_delivery.py`` -- reads the stranded census from
the spools themselves, and when the count GROWS it files exactly ONE
idempotency-keyed card routed to the design owner. No inference, no prose a human
must interpret, and nothing that depends on a human noticing.

The design ruled on card t_703af8d1 (platform-stl), implemented here as written:

* **the count** is :func:`tools.bot_delivery_queue.strand_census` -- over-TTL queued
  records, plus claims whose lease was lost, plus settled ``expired`` records whose
  TERMINAL WRITE is inside the expire window (default 24 h,
  ``HERMES_STRAND_EXPIRE_WINDOW_SECONDS``). The expired TOTAL is history and is never
  added to it (card t_330bee02: counting it made the meter monotonic, so recovery was
  unreachable and this guard went one-shot). Read LIVE from every spool, reproducible
  by one command (this module's ``__main__`` prints exactly it).
* **it runs in PROD**, inside the drainer's own 30s sweep: no shadow runner, no
  sandbox checkout, no new daemon, no cron shim pointing at a working tree.
* **it acts on GROWTH**, not on a fixed threshold: a steady backlog that is being
  drained is not the failure. The growth test is PER SEAT (measured 2026-10-14:
  seven strands fleet-wide is nothing, six on one seat is a pattern).
* **one card per INCIDENT**, idempotency-keyed, so a sustained or growing backlog
  can never flood the board: a repeat attempt returns the SAME card id and writes
  no second row. An incident opens on a growth and closes when the count returns to
  zero; recovery CANCELS and REOPENS nothing. The ledger also READS its own card:
  when the card is closed on the board while the backlog stands, the generation
  advances and a fresh card is filed, so the guard cannot silence itself.
* **one log line per evaluation**, either way.

The card BODY carries the raw counts, the per-seat breakdown, the oldest stranded
record's age and its path, so a reader needs no other tooling. The card's priority
is ASKED of the board's own band module (``kanban_priority_policy``, the same door
the dispatcher asks through) and never typed.

Read-only census, one command::

    cd /Users/hermes_user/.hermes/hermes-agent && venv/bin/python3 -m tools.bot_delivery_strand_alert

(the same command with an explicit root home as ``argv[1]``; it FILES NOTHING).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

from tools import bot_delivery_queue as delivery_queue

logger = logging.getLogger(__name__)

#: The board the alarm files on: the dispatch channel (a card filed anywhere else is
#: parked by construction), and the board the rest of the estate's alarms ride.
STRAND_ALERT_BOARD = os.environ.get("HERMES_STRAND_ALERT_BOARD", "migrations")

#: The design/architecture owner of the stranded-count defect. Deterministic routing:
#: a strand count is a design question (why the stranding happens), so it is the STL's.
STRAND_ALERT_ASSIGNEE = os.environ.get("HERMES_STRAND_ALERT_ASSIGNEE", "platform-stl")

#: A non-profile producer origin, so the card's provenance names the MACHINE that filed
#: it (like ``pr-evergreen`` / ``deploy-train`` / ``kanban-dispatcher``), not a lane.
STRAND_ALERT_CREATED_BY = "bot-delivery-drainer"

#: One key per INCIDENT (see :func:`alert`): the generation is bumped only when the
#: count returns to zero, so a sustained backlog cannot mint a second row and a genuine
#: new incident can.
STRAND_ALERT_IDEMPOTENCY_PREFIX = "peer-strand-alert:"

STRAND_ALERT_TITLE = (
    "[peer-delivery] stranded peer deliveries GREW -- the drainer cannot clear them by itself"
)

#: The incident ledger. Lives beside the spools it measures, one per root home.
STATE_FILENAME = "bot_delivery_strand_alert.json"
STATE_VERSION = 1

#: A card in one of these states is CLOSED: the incident it recorded is over on the
#: board while the backlog it recorded stands. The ledger must not keep pointing at it
#: -- that is how the recurrence guard went one-shot: generation stayed 0 forever, so
#: the key ``peer-strand-alert:0`` stayed pinned to one terminal card and the next
#: growth could only return that closed card or be refused as a duplicate-closed-key.
STRAND_ALERT_TERMINAL_CARD_STATUSES = frozenset({"done", "archived", "cancelled"})


def _read_card_status(card_id: str, *, board: Optional[str] = None) -> Optional[str]:
    """One card's status through the board seam, or ``None`` when the row is ABSENT.

    The ONE seam the tests stub for the ledger's self-check. A board read that RAISES
    is deliberately not swallowed here: :func:`evaluate` owns that decision, because a
    hiccup must never mint a duplicate card and must never clear a live ``card_id``.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect_closing(board=board or STRAND_ALERT_BOARD) as conn:
        task = kb.get_task(conn, card_id)
    if task is None:
        return None
    return str(getattr(task, "status", "") or "")


# --------------------------------------------------------------------------
# the incident ledger
# --------------------------------------------------------------------------
def alert_state_path(root_home: str | os.PathLike[str]) -> Path:
    """``<root>/runtime/bot_delivery_strand_alert.json`` (next to the spools)."""
    return Path(root_home) / "runtime" / STATE_FILENAME


def read_state(path: str | os.PathLike[str]) -> dict[str, Any]:
    """The last evaluation's ledger, or ``{}`` when there is none / it is unreadable.

    A ledger that cannot be read is treated as absent -- a first evaluation -- never as
    an error: the alarm's whole job is to be impossible to silence.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_state(path: str | os.PathLike[str], state: dict[str, Any]) -> None:
    """Atomically replace the ledger (temp file + ``os.replace``, spool idiom)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-strand-alert-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# the evaluation (pure: no board write, no ledger write)
# --------------------------------------------------------------------------
def evaluate(root_home: str | os.PathLike[str], *,
             state_path: str | os.PathLike[str] | None = None,
             census: Optional[dict[str, Any]] = None,
             now_ns: Optional[int] = None,
             board: Optional[str] = None,
             read_card: Optional[Callable[..., Optional[str]]] = None) -> dict[str, Any]:
    """The census plus the growth verdict for this evaluation.

    ``grown_seats`` is the whole growth test: a seat whose stranded count is HIGHER
    than the last evaluation's. A first evaluation (no ledger) has a baseline of
    zero, so a live backlog is reported as growth -- an existing backlog has never
    been alerted and is exactly what nothing else watches. An incident closes only
    when the count is back to zero, which bumps ``generation`` so the NEXT incident
    gets its own card.

    ``stranded`` is the ACTIONABLE, windowed count (see
    ``bot_delivery_queue.seat_strand_census``), so "back to zero" now means what it
    says: nothing over TTL, no stuck claim, and nothing expired inside the expire
    window. That is DoD 8's sustained clause, and no second threshold is needed.

    THE LEDGER'S SELF-CHECK (ruling D3, card t_330bee02). While ``stranded > 0`` and a
    ``card_id`` is recorded, the card is READ: a card whose row is gone or is CLOSED
    (``done``/``archived``/``cancelled``) means the incident ended on the board while
    the backlog stands, so ``generation`` is bumped, ``card_id`` cleared, and the next
    filing gets a fresh key. A read that RAISES changes nothing -- the current
    ``card_id`` is kept and nothing is filed -- because a board hiccup must never mint
    a duplicate card. The one-card-per-generation guarantee is the idempotency KEY;
    this read only notices a closure.
    """
    path = Path(state_path) if state_path is not None else alert_state_path(root_home)
    census = delivery_queue.strand_census(root_home, now_ns=now_ns) if census is None else census
    state = read_state(path)
    prev = {str(k): int(v or 0) for k, v in (state.get("seats") or {}).items()}
    counts = {seat: int(data.get("stranded") or 0) for seat, data in census["seats"].items()}
    grown = sorted(seat for seat, n in counts.items() if n > prev.get(seat, 0))
    total = int(census["totals"]["stranded"])
    prev_total = int(state.get("fleet_total") or 0)
    recovery = bool(prev_total > 0 and total == 0)
    generation = int(state.get("generation") or 0) + (1 if recovery else 0)
    card_id = None if recovery else (str(state.get("card_id") or "") or None)
    closed_card: Optional[str] = None
    card_read_failed = False
    if total > 0 and card_id:
        reader = read_card or _read_card_status
        try:
            status = reader(card_id, board=board)
        except Exception as exc:
            card_read_failed = True
            logger.warning(
                "[bot_delivery] strand alert could not read card %s -- keeping it, "
                "filing nothing this evaluation: %s", card_id, exc,
            )
        else:
            if status is None or str(status) in STRAND_ALERT_TERMINAL_CARD_STATUSES:
                closed_card = card_id
                generation += 1
                card_id = None
    return {
        "state_path": str(path),
        "census": census,
        "counts": counts,
        "previous": prev,
        "previous_total": prev_total,
        "grown_seats": grown,
        "fleet_total": total,
        "recovery": recovery,
        "generation": generation,
        "card_id": card_id,
        "closed_card": closed_card,
        "card_read_failed": card_read_failed,
    }


# --------------------------------------------------------------------------
# acting: the card
# --------------------------------------------------------------------------
def _band_priority(board: str, *, assignee: str, title: str, body: str) -> tuple[int, str]:
    """``(priority, note)`` ASKED of the board's own band module, never typed.

    The same door the dispatcher's own acting card asks through
    (``kanban_db_dispatch._tick_yield_actor_priority``): the board's
    ``board.json:priority_policy`` spec through ``kanban_priority_policy``, at the
    kernel ask (0). A ladder that cannot be read is RECORDED, never invented away --
    the card is still filed, at the kernel default, and the note rides into its body.
    """
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_priority_policy as policy

        spec = (kb._board_meta_for(board) or {}).get("priority_policy")
        verdict = policy.priority_for_create(
            0, assignee=assignee, board=board, title=title, body=body, spec=spec,
        )
        if verdict is None:
            return 0, f"no priority policy on board {board}: filed at the kernel default"
        return int(verdict.applied), ""
    except Exception as exc:  # pragma: no cover - defensive: a ladder must never block a filing
        return 0, f"the ladder could not be read: {exc.__class__.__name__}: {exc}"


def _create_card(*, title: str, body: str, assignee: str, priority: int, board: str,
                 idempotency_key: str) -> str:
    """The ONE seam the tests stub: the fork's own board API.

    The alarm runs in the GATEWAY process -- not a ``delegate_task`` child context --
    so the ordinary board write is permitted here; that is the whole reason this
    action belongs in the gateway's sweep and not in a CLI.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect_closing(board=board) as conn:
        return str(kb.create_task(
            conn, title=title, body=body, assignee=assignee,
            created_by=STRAND_ALERT_CREATED_BY, priority=priority,
            idempotency_key=idempotency_key,
        ) or "")


def _age_text(seconds: Any) -> str:
    try:
        total = float(seconds)
    except (TypeError, ValueError):
        return "unknown"
    if total < 3600:
        return f"{total:.0f}s"
    if total < 86400:
        return f"{total / 3600:.1f}h"
    return f"{total / 86400:.1f}d"


def _count(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _window_text(value: Any) -> str:
    """A window in seconds, printed as an int when it is one (``86400``, not ``86400.0``)."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return "unknown"
    return str(int(seconds)) if seconds == int(seconds) else f"{seconds:g}"


def build_card_body(verdict: dict[str, Any], *, idempotency_key: str,
                    priority_note: str = "") -> str:
    """The card body: every number a reader needs, and no other tooling.

    The ``stranded`` line DECOMPOSES into its three terms, the expired line prints
    ``total / within window``, and both name the window and the read instant --
    because a bare total is what made 1192 dead records look like an incident
    (ruling D4, card t_330bee02).
    """
    census = verdict["census"]
    totals = census["totals"]
    seats = census["seats"]
    grown = verdict["grown_seats"]
    oldest = totals.get("oldest_age_seconds")
    ttl = census.get("ttl_seconds")
    window = census.get("window_seconds")
    if window is None:
        window = delivery_queue.expire_window_seconds()
    window_text = _window_text(window)
    over_ttl = _count(totals.get("pending_over_ttl"))
    stuck = _count(totals.get("claimed_stuck"))
    expired = _count(totals.get("expired"))
    within = _count(totals.get("expired_within_window"))
    undated = _count(totals.get("expired_undated"))
    read_at = census.get("now_ns")
    read_text = f"{int(read_at) / 1e9:.3f}" if read_at else "unknown"
    rows = [
        f"| seat | stranded | over-TTL queued | claimed stuck | expired in {window_text}s "
        "| expired (total) | expired undated | ambiguous | failed | delivered | oldest |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for seat in sorted(seats):
        data = seats[seat]
        rows.append(
            f"| `{seat}` | {_count(data.get('stranded'))} "
            f"| {_count(data.get('pending_over_ttl'))} "
            f"| {_count(data.get('claimed_stuck'))} "
            f"| {_count(data.get('expired_within_window'))} "
            f"| {_count(data.get('expired'))} "
            f"| {_count(data.get('expired_undated'))} "
            f"| {_count(data.get('ambiguous'))} | {_count(data.get('failed'))} "
            f"| {_count(data.get('delivered'))} | {_age_text(data.get('oldest_age_seconds'))} |"
        )
    lines = [
        "## Observed -- the stranded peer-delivery count GREW",
        "",
        "The receiver-side delivery queue holds messages that were ACCEPTED and never",
        "delivered. This card is filed by the LIVE gateway's own delivery sweep (the 30s",
        "drainer tick on the routable seat), because nothing else watched this count:",
        "seven `expired` records sat in the store for three weeks and were found only by",
        "hand on 2026-10-14 (project `peer-delivery`).",
        "",
        f"- **stranded: {verdict['fleet_total']}** (was {verdict['previous_total']}) -- "
        f"= {over_ttl} over-TTL queued + {stuck} claimed stuck + {within} expired in the "
        f"last {window_text}s. These three terms are the whole count: every one of them",
        "  describes something that could still be a live failure",
        f"- claimed stuck: {stuck} -- claims whose lease was lost (`claimed_stuck`): a "
        f"claim older than 2x the TTL (TTL {ttl}s) that no live holder stands behind, a "
        "delivery nobody is running and nothing was counting",
        f"- expired: {expired} total / {within} within the last {window_text}s -- "
        f"`expired_undated`: {undated}. The TOTAL is HISTORY: `expired` is a settled",
        "  terminal status, nothing drains it and no sweep reaps it, so only the window is",
        "  added to `stranded`. A month-old record is not a message lost yesterday.",
        f"- read at: {read_text} (epoch seconds; expire window {window_text}s, queue TTL {ttl}s)",
        f"- grew on seat(s): {', '.join(f'`{s}`' for s in grown) or '(new backlog, no seat named)'}",
        f"- oldest stranded record: {_age_text(oldest)} old"
        f" -- `{totals.get('oldest_path')}` (delivery_id `{totals.get('oldest_delivery_id')}`)",
        f"- root home: `{census.get('root')}`",
        "",
        "### Per-seat census",
        "",
        *rows,
        "",
        "`ambiguous` is the crash-mid-turn terminal state -- an HONEST 'outcome unknown',",
        "not a strand. It is surfaced here only because the gateway died mid-turn when it",
        "was written, and nothing else said so. It is never added to `stranded`.",
        "",
        "### What the alarm did NOT do",
        "",
        "It filed this one card and nothing else: no cancel, no reopen, no drain, no",
        "remediation. Fixing the stranding rides the existing cards on this board.",
        "",
        "### The recurrence guard",
        "",
        f"- idempotency key: `{idempotency_key}` (one row per INCIDENT -- while this backlog",
        "  stands, a repeat evaluation returns THIS card id and writes no second row). The",
        "  key advances only when the count returns to zero, or when this card is CLOSED on",
        "  the board while the backlog stands (the ledger reads it and re-opens the incident",
        "  under the next generation, so the guard cannot go one-shot)",
        "- filed by `bot-delivery-drainer` from the gateway's delivery sweep: deterministic,",
        "  no model consulted.",
    ]
    if priority_note:
        lines += ["", f"_priority note: {priority_note}_"]
    return "\n".join(lines)


def _file_alert(verdict: dict[str, Any], *, board: str, assignee: str,
                create: Optional[Callable[..., str]] = None,
                priority: Optional[int] = None,
                priority_note: Optional[str] = None) -> str:
    """File the incident's ONE card (idempotency-keyed); returns its id or ``''``."""
    key = f"{STRAND_ALERT_IDEMPOTENCY_PREFIX}{verdict['generation']}"
    if priority is None or priority_note is None:
        priority, priority_note = _band_priority(
            board, assignee=assignee, title=STRAND_ALERT_TITLE,
            body=build_card_body(verdict, idempotency_key=key),
        )
    filer = create or _create_card
    body = build_card_body(verdict, idempotency_key=key, priority_note=priority_note)
    try:
        return str(filer(title=STRAND_ALERT_TITLE, body=body, assignee=assignee,
                         priority=int(priority), board=board, idempotency_key=key) or "")
    except Exception as exc:
        logger.warning("[bot_delivery] strand alert could not be filed: %s", exc, exc_info=True)
        return ""


# --------------------------------------------------------------------------
# the loop's entry point
# --------------------------------------------------------------------------
def alert(root_home: str | os.PathLike[str], *,
          state_path: str | os.PathLike[str] | None = None,
          board: Optional[str] = None,
          assignee: Optional[str] = None,
          create: Optional[Callable[..., str]] = None,
          census: Optional[dict[str, Any]] = None,
          now_ns: Optional[int] = None,
          log_fn: Optional[Callable[..., None]] = None,
          read_card: Optional[Callable[..., Optional[str]]] = None) -> dict[str, Any]:
    """One evaluation: census, ONE log line, and -- on growth -- ONE keyed card.

    Never raises on the board's account, and never lets a filing failure hide the
    evaluation: the log line is emitted either way, with `card=-` when nothing was
    filed. Returns the verdict plus ``card_id`` / ``filed`` / ``priority``.

    A board READ that fails files nothing this evaluation (ruling D3): the recorded
    ``card_id`` is kept untouched, so a hiccup can neither duplicate a card nor
    orphan one.
    """
    log = log_fn or delivery_queue._log
    board = board or STRAND_ALERT_BOARD
    assignee = assignee or STRAND_ALERT_ASSIGNEE
    path = Path(state_path) if state_path is not None else alert_state_path(root_home)
    verdict = evaluate(root_home, state_path=path, census=census, now_ns=now_ns,
                       board=board, read_card=read_card)
    card_id = verdict["card_id"]
    filed = False
    priority: Optional[int] = None
    # ACT on growth -- and on a first sighting of a non-zero backlog with no card
    # recorded yet, which is a filing that FAILED last tick, not a new incident. A
    # failed board read suppresses BOTH arms (D3): nothing is filed blind.
    if not verdict.get("card_read_failed") and (
            verdict["grown_seats"] or (verdict["fleet_total"] > 0 and not card_id)):
        priority, priority_note = _band_priority(
            board, assignee=assignee, title=STRAND_ALERT_TITLE,
            body=build_card_body(verdict, idempotency_key=(
                f"{STRAND_ALERT_IDEMPOTENCY_PREFIX}{verdict['generation']}")),
        )
        filed_id = _file_alert(verdict, board=board, assignee=assignee, create=create,
                               priority=priority, priority_note=priority_note)
        if filed_id:
            filed = not card_id
            card_id = filed_id
    totals = verdict["census"]["totals"]
    census_dict = verdict["census"]
    window = census_dict.get("window_seconds")
    if window is None:
        window = delivery_queue.expire_window_seconds()
    log("strand_alert", None,
        stranded=verdict["fleet_total"],
        previous=verdict["previous_total"],
        grown=len(verdict["grown_seats"]),
        seats=delivery_queue.stranded_breakdown(verdict["census"]),
        expired=int(totals.get("expired") or 0),
        expired_within_window=int(totals.get("expired_within_window") or 0),
        expired_undated=int(totals.get("expired_undated") or 0),
        window_seconds=int(window),
        over_ttl=int(totals.get("pending_over_ttl") or 0),
        claimed_stuck=int(totals.get("claimed_stuck") or 0),
        ambiguous=int(totals.get("ambiguous") or 0),
        generation=verdict["generation"],
        card=card_id or "-")
    try:
        write_state(path, {
            "version": STATE_VERSION,
            "generation": verdict["generation"],
            "seats": verdict["counts"],
            "fleet_total": verdict["fleet_total"],
            "card_id": card_id,
            "recovery": verdict["recovery"],
            "updated_at": time.time(),
        })
    except Exception:  # pragma: no cover - a ledger write must not kill the sweep
        logger.warning("[bot_delivery] strand alert ledger could not be written", exc_info=True)
    result = dict(verdict)
    result.update({"card_id": card_id, "filed": filed, "board": board, "assignee": assignee,
                   "priority": priority})
    return result


def evaluate_and_alert(home: str | os.PathLike[str], **kwargs: Any) -> Optional[dict[str, Any]]:
    """The sweep's door: evaluate ``home``'s ROOT and never raise.

    ``home`` is the gateway's ambient home; the census is fleet-wide, so the root is
    derived from it (a gateway running on a profile's home measures the same fleet).
    """
    from tools.bot_mode_probe import _hermes_root

    try:
        return alert(_hermes_root(Path(home)), **kwargs)
    except Exception:
        logger.warning("[bot_delivery] strand alert evaluation failed", exc_info=True)
        return None


# --------------------------------------------------------------------------
# the one-command census (read-only: FILES NOTHING)
# --------------------------------------------------------------------------
def _main(argv: Optional[list[str]] = None) -> int:
    import sys

    from tools.bot_mode_probe import _default_home, _hermes_root

    args = list(sys.argv[1:] if argv is None else argv)
    root = _hermes_root(Path(args[0])) if args else _hermes_root(Path(_default_home()))
    census = delivery_queue.strand_census(root)
    verdict = evaluate(root, census=census)
    window = census.get("window_seconds")
    if window is None:
        window = delivery_queue.expire_window_seconds()
    window = int(window) if float(window) == int(window) else window
    print(json.dumps({
        "root": census["root"],
        "ttl_seconds": census["ttl_seconds"],
        "window_seconds": window,
        "read_at": round(int(census["now_ns"]) / 1e9, 3),
        "stranded": census["totals"]["stranded"],
        "claimed_stuck": census["totals"]["claimed_stuck"],
        "expired": census["totals"]["expired"],
        "expired_within_window": census["totals"]["expired_within_window"],
        "expired_undated": census["totals"]["expired_undated"],
        "pending_over_ttl": census["totals"]["pending_over_ttl"],
        "ambiguous": census["totals"]["ambiguous"],
        "failed": census["totals"]["failed"],
        "delivered": census["totals"]["delivered"],
        "oldest_age_seconds": census["totals"]["oldest_age_seconds"],
        "oldest_path": census["totals"]["oldest_path"],
        "seats": {seat: {"stranded": data["stranded"], "expired": data["expired"],
                         "expired_within_window": data["expired_within_window"],
                         "expired_undated": data["expired_undated"],
                         "pending_over_ttl": data["pending_over_ttl"],
                         "claimed_stuck": data["claimed_stuck"],
                         "ambiguous": data["ambiguous"], "failed": data["failed"],
                         "delivered": data["delivered"],
                         "oldest_age_seconds": data["oldest_age_seconds"]}
                  for seat, data in census["seats"].items()},
        "grown_seats": verdict["grown_seats"],
        "generation": verdict["generation"],
        "card_id": verdict["card_id"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - operator surface
    raise SystemExit(_main())
