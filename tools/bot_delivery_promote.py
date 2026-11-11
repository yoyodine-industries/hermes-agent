#!/usr/bin/env python3
"""The bot-delivery PROMOTER: ``deferred -> queued`` when a record's backoff has arrived.

Why this is a program and not a predicate in the drainer (card t-76be5e4334, platform-stl's
ruling): the deferral must live in the STORE. ``claim_next`` has no production caller -- the real
consumer of the queue is the Desktop's drainer, which is not our code -- so a gate written in a
reader would pass its own unit test and change nothing in production. A record in
``status == "deferred"`` is invisible to every consumer that selects on ``status``; only this
promoter moves it back, and it does so by rewriting the store.

The failure mode this program exists FOR is its own death: a promoter that stops running leaves
records frozen as ``deferred`` -- strictly worse than the terminal refusal it replaced, because a
refused record at least reads as failed. So the promoter:

* prints ``deferred`` and ``oldest_deferred_age`` BESIDE ``queued`` (never folded in), so
  "0 queued" can never read as healthy while N records sit frozen;
* exits non-zero when the oldest deferred record is older than its own ceiling, which means the
  promoter is not running or is failing;
* on that verdict, and on any exception, FILES A CARD -- deterministically, once per hour per
  home, through the same board seam the strand alarm uses. A log line nobody reads is a silent
  failure, which is the whole defect class this card is about.

Usage:
    bot_delivery_promote.py [--home PATH] [--json] [--no-card] [--stale-after SECONDS]
                            [--dry-run]
Exit codes: 0 healthy · 2 a record is deferred but none is overdue · 3 FROZEN (carded)
            · 1 unexpected error (carded).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # run as a script from a checkout
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import bot_delivery_queue as q  # noqa: E402

logger = logging.getLogger("bot_delivery_promote")

PROMOTER_CREATED_BY = "bot-delivery-promoter"
PROMOTER_BOARD = "platform"
PROMOTER_ASSIGNEE = "platform-coder"
#: A deferred record older than this means the promoter has stopped promoting. It is well past
#: the largest backoff (``DEFERRAL_CAP_SECONDS`` = 900 s), so a healthy promoter can never trip it;
#: three missed cycles at the cap is a stopped promoter, not a slow one.
DEFAULT_STALE_AFTER_SECONDS = 3 * q.DEFERRAL_CAP_SECONDS

EXIT_HEALTHY = 0
EXIT_DEFERRED = 2
EXIT_FROZEN = 3
EXIT_ERROR = 1


def _now_ns() -> int:
    """The promoter's clock, in one place so a test can pin it."""
    return time.time_ns()


def _default_home() -> Path:
    from tools.bot_mode_probe import _default_home as default_home

    return Path(default_home())


def _iso(ns: int | None) -> str:
    if not ns:
        return "-"
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ns) / 1e9))


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


def promoter_verdict(
    root_home: str | os.PathLike[str],
    *,
    now_ns: int | None = None,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    promote: bool = True,
) -> dict[str, Any]:
    """Promote every seat (unless ``promote=False``), then report the fleet's deferred population.

    ``promote=False`` is the CENSUS read: same store list, same instants, no write.
    """
    now_ns = _now_ns() if now_ns is None else int(now_ns)
    promoted = (q.promote_deferred_all(root_home, now_ns=now_ns) if promote
                else {"now_ns": now_ns, "seats": {}, "totals": {"promoted": 0, "deferred": 0}})
    seats: dict[str, dict[str, Any]] = {}
    totals = {"queued": 0, "deferred": 0, "promoted": int(promoted["totals"].get("promoted") or 0)}
    oldest: float | None = None
    oldest_seat: str | None = None
    next_eligible: int | None = None
    for seat, seat_home in q.spool_homes(root_home):
        census = q.deferred_census(seat_home, now_ns=now_ns)
        row = dict(census)
        row["promoted"] = int((promoted["seats"].get(seat) or {}).get("promoted") or 0)
        row["queued"] = sum(
            1 for record in q._list(q.delivery_root(seat_home), q.QUEUE_DIR)
            if record.get("status") == q.STATUS_QUEUED
        )
        seats[seat] = row
        totals["queued"] += row["queued"]
        totals["deferred"] += int(census.get("deferred") or 0)
        age = census.get("oldest_deferred_age_seconds")
        if age is not None and (oldest is None or float(age) > oldest):
            oldest = float(age)
            oldest_seat = seat
        nb = census.get("next_eligible_ns")
        if nb and (next_eligible is None or int(nb) < next_eligible):
            next_eligible = int(nb)
    frozen = oldest is not None and oldest > float(stale_after_seconds)
    return {
        "now_ns": now_ns,
        "stores": [str(q.delivery_root(seat_home)) for _seat, seat_home in q.spool_homes(root_home)],
        "seats": seats,
        "totals": totals,
        "oldest_deferred_age_seconds": oldest,
        "oldest_deferred_seat": oldest_seat,
        "next_eligible_ns": next_eligible,
        "stale_after_seconds": float(stale_after_seconds),
        "frozen": bool(frozen),
    }


def build_census_line(verdict: dict[str, Any]) -> str:
    """One line: ``queued`` and ``deferred`` as two numbers, with the instant and the stores."""
    totals = verdict["totals"]
    parts = [
        f"queued {totals['queued']}",
        f"deferred {totals['deferred']}",
        f"promoted {totals['promoted']}",
        f"oldest_deferred_age {_age_text(verdict.get('oldest_deferred_age_seconds'))}",
        f"next_eligible_at {_iso(verdict.get('next_eligible_ns'))}",
        f"at {_iso(verdict['now_ns'])}",
        f"stores {len(verdict.get('stores') or [])}",
    ]
    if verdict.get("frozen"):
        parts.append(f"FROZEN past {_age_text(verdict.get('stale_after_seconds'))}")
    return " · ".join(parts)


def build_card_body(verdict: dict[str, Any], *, idempotency_key: str) -> str:
    totals = verdict["totals"]
    lines = [
        "## Observed -- the bot-delivery PROMOTER has stopped promoting",
        "",
        f"`deferred` records are frozen: the oldest is "
        f"{_age_text(verdict.get('oldest_deferred_age_seconds'))} old "
        f"(seat `{verdict.get('oldest_deferred_seat')}`), past the promoter's own ceiling of "
        f"{_age_text(verdict.get('stale_after_seconds'))}.",
        "",
        "A frozen deferral is strictly worse than the terminal refusal it replaced: a refused "
        "record reads as `failed`, a frozen one reads as nothing at all. The records are safe -- "
        "`deferred` is exempt from the over-TTL sweep -- but they will never be offered again "
        "until the promoter runs.",
        "",
        f"`queued {totals['queued']} · deferred {totals['deferred']} · "
        f"promoted {totals['promoted']}` at {_iso(verdict['now_ns'])}",
        "",
        "| seat | queued | deferred | oldest deferred | next eligible |",
        "|---|---|---|---|---|",
    ]
    for seat in sorted(verdict["seats"]):
        row = verdict["seats"][seat]
        lines.append(
            f"| `{seat}` | {row['queued']} | {int(row.get('deferred') or 0)} "
            f"| {_age_text(row.get('oldest_deferred_age_seconds'))} "
            f"| {_iso(row.get('next_eligible_ns'))} |"
        )
    lines += [
        "",
        "## Stores read",
        "",
        *[f"- {path}" for path in verdict.get("stores") or []],
        "",
        f"idempotency: `{idempotency_key}`",
    ]
    return "\n".join(lines)


def _create_card(*, title: str, body: str, assignee: str, priority: int, board: str,
                 idempotency_key: str) -> str:
    """The ONE seam the tests stub: the fork's own board API."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect_closing(board=board) as conn:
        return str(kb.create_task(
            conn, title=title, body=body, assignee=assignee,
            created_by=PROMOTER_CREATED_BY, priority=priority,
            idempotency_key=idempotency_key,
        ) or "")


def card_key(root_home: str | os.PathLike[str], *, now_ns: int) -> str:
    """Deterministic, one card per home per hour: a repeating alarm must not spam the board."""
    home_hash = hashlib.sha256(str(root_home).encode("utf-8")).hexdigest()[:12]
    bucket = int(now_ns // 3_600_000_000_000)
    return f"bot-delivery-promoter:{home_hash}:{bucket}"


def card_frozen(verdict: dict[str, Any], *, root_home: str | os.PathLike[str]) -> str:
    key = card_key(root_home, now_ns=int(verdict["now_ns"]))
    try:
        return _create_card(
            title="bot-delivery promoter FROZEN: deferred records are not being promoted",
            body=build_card_body(verdict, idempotency_key=key),
            assignee=PROMOTER_ASSIGNEE,
            priority=1,
            board=PROMOTER_BOARD,
            idempotency_key=key,
        )
    except Exception:  # a card that cannot be filed must still be loud on stderr
        logger.exception("bot_delivery_promote: could not file the frozen-promoter card")
        return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", default=None, help="the Hermes root home")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-card", action="store_true", help="never file a card (tests, probes)")
    parser.add_argument("--dry-run", action="store_true", help="census only; promote nothing")
    parser.add_argument("--stale-after", type=float, default=DEFAULT_STALE_AFTER_SECONDS)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    root_home = Path(args.home) if args.home else _default_home()
    try:
        if args.dry_run:
            verdict = promoter_verdict(root_home, stale_after_seconds=args.stale_after,
                                       promote=False)
        else:
            verdict = promoter_verdict(root_home, stale_after_seconds=args.stale_after)
    except Exception as exc:
        logger.exception("bot_delivery_promote failed: %s", exc)
        if not args.no_card:
            try:
                key = card_key(root_home, now_ns=_now_ns())
                _create_card(
                    title="bot-delivery promoter CRASHED",
                    body=f"`bot_delivery_promote.py` raised {exc.__class__.__name__}: {exc}\n\n"
                         f"home: `{root_home}`\nidempotency: `{key}`",
                    assignee=PROMOTER_ASSIGNEE, priority=1, board=PROMOTER_BOARD,
                    idempotency_key=key,
                )
            except Exception:
                logger.exception("bot_delivery_promote: could not file the crash card")
        return EXIT_ERROR
    line = build_census_line(verdict)
    if args.json:
        print(json.dumps(dict(verdict, line=line), indent=2, default=str))
    else:
        print(line)
    if verdict.get("frozen"):
        if not args.no_card:
            card_id = card_frozen(verdict, root_home=root_home)
            print(f"FROZEN: carded {card_id or '(filing FAILED -- see stderr)'}")
        return EXIT_FROZEN
    if verdict["totals"]["deferred"]:
        return EXIT_DEFERRED
    return EXIT_HEALTHY


if __name__ == "__main__":
    raise SystemExit(main())
