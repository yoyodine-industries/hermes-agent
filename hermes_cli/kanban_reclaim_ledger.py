"""The reaper's two-sided ledger, as a QUERY instead of a rumour (card t_48194194).

The reaper's kept side already existed (``reclaim_deferred``) and its took side already existed
(``reclaimed`` / ``crashed`` / ``timed_out``); what did not exist was a way to read them as one
ledger, and a clamp population that could be tied to the cap that killed it. This module is the
reader. It writes nothing, opens its own read-only connection unless handed one, and returns plain
dicts so a card, a test and an operator all quote the same numbers.

WHY BOTH AXES, NEVER ONE: "the clamp stays for live workers" is a KEEP count that must not fall;
"the clamp shrank for dead claims" is a TOOK count that must rise. A fix that starts killing live
workers moves only the first number -- which is why the kept side has to be in the same query as
the took side instead of being inferred from a total going down.

THE SIGNATURE QUERY (the shape this module reproduces, in SQL):

    -- TOOK (shrank): claims released, by the gate that fired and why
    SELECT json_extract(payload,'$.gate')  AS gate,
           json_extract(payload,'$.reason') AS reason,
           COUNT(DISTINCT run_id)          AS runs
      FROM task_events
     WHERE created_at > :since
       AND (json_extract(payload,'$.verdict') = 'reclaimed'
            OR (json_extract(payload,'$.verdict') IS NULL
                AND kind IN ('reclaimed','crashed','timed_out')))   -- legacy rows: kind is the side
     GROUP BY 1,2;

    -- KEPT (still clamped -- CORRECT for a live worker), by gate and reason
    SELECT json_extract(payload,'$.gate')  AS gate,
           json_extract(payload,'$.reason') AS reason,
           COUNT(DISTINCT run_id)          AS runs
      FROM task_events
     WHERE created_at > :since
       AND (json_extract(payload,'$.verdict') IN ('reclaim_deferred','reclaim_refused','claim_kept')
            OR (json_extract(payload,'$.verdict') IS NULL AND kind = 'reclaim_deferred'))
     GROUP BY 1,2;

    -- THE CLAMP POPULATION ITSELF, with its cap attached. The third predicate is only valid once
    -- the cap is written onto the run row at termination (item 2 of the card); ``cap_share`` below
    -- reports how much of the population can carry it, so the predicate is never quoted bare.
    SELECT COUNT(*)                                              AS clamps,
           SUM(max_runtime_seconds IS NOT NULL)                  AS with_cap,
           SUM(ended_at - started_at >= max_runtime_seconds)     AS predicate_matches
      FROM task_runs
     WHERE status = 'timed_out'
       AND error LIKE 'elapsed %> limit %';     -- the runtime cap's DOCUMENTED prose prefix
                                                -- (kanban_db.LIMIT_ERROR_PREFIXES), never a guess

Run it::

    python -m hermes_cli.kanban_reclaim_ledger --hours 12
    python -m hermes_cli.kanban_reclaim_ledger --db /path/kanban.db --json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from typing import Any, Optional

from hermes_cli.kanban_db import (
    LIMIT_KIND_ITERATION_BUDGET,
    LIMIT_KIND_RUNTIME_CAP,
    limit_kind_for_error,
)

#: Which side of the ledger each event kind reports when the row predates the ``verdict`` field. A
#: row in one of these is still evidence; it just names its side by ``kind`` instead of ``verdict``.
LEGACY_KIND_SIDES = {
    "reclaimed": "took",
    "crashed": "took",
    "timed_out": "took",
    "reclaim_deferred": "kept",
    "reclaim_refused": "kept",
    "claim_extended": "kept",
}

TOOK_VERDICTS = frozenset({"reclaimed"})
KEPT_VERDICTS = frozenset({"reclaim_deferred", "reclaim_refused", "claim_kept"})


def _connect(db: Optional[str]) -> sqlite3.Connection:
    """Open READ-ONLY: this module is a reader, and a reader that can initialize is not one."""
    if not db:
        from hermes_cli import kanban_db as _kb

        db = str(_kb.kanban_db_path())
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _side(kind: Optional[str], verdict: Optional[str]) -> Optional[str]:
    if verdict in KEPT_VERDICTS:
        return "kept"
    if verdict in TOOK_VERDICTS:
        return "took"
    return LEGACY_KIND_SIDES.get(kind or "")


def verdict_ledger(conn: sqlite3.Connection, *, since: Optional[int] = None) -> dict[str, Any]:
    """Every examination verdict in the window, split by side, gate and reason.

    Returns ``{"since", "took": [...], "kept": [...], "took_runs", "kept_runs", "outstanding"}``.
    Grain is ``DISTINCT run_id`` (a claim is a run), with the row count carried alongside as a
    diagnostic: two rows for one run is a renewal or a duplicate, and the row/runs pair is what
    makes that visible instead of silently re-graining the balance.
    """
    sql = (
        "SELECT kind, run_id, payload FROM task_events "
        "WHERE (? IS NULL OR created_at > ?) "
        "  AND (kind IN ('reclaimed','crashed','timed_out','reclaim_deferred','reclaim_refused',"
        "               'claim_extended') OR payload LIKE '%\"verdict\"%')"
    )
    tok = (since, since)
    buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
    took_runs: set = set()
    kept_runs: set = set()
    for row in conn.execute(sql, tok):
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        verdict = payload.get("verdict")
        side = _side(row["kind"], verdict)
        if side is None:
            continue
        gate = payload.get("gate") or ("legacy" if not verdict else "")
        reason = payload.get("reason") or ""
        key = (side, str(gate), str(reason))
        bucket = buckets.setdefault(key, {"side": side, "gate": key[1], "reason": key[2],
                                          "rows": 0, "run_ids": set()})
        bucket["rows"] += 1
        if row["run_id"] is not None:
            bucket["run_ids"].add(row["run_id"])
        if side == "kept":
            kept_runs.add(row["run_id"])
        else:
            took_runs.add(row["run_id"])

    rows = []
    for bucket in buckets.values():
        runs = bucket.pop("run_ids")
        bucket["runs"] = len(runs)
        rows.append(bucket)
    rows.sort(key=lambda r: (-r["rows"], r["side"], r["gate"], r["reason"]))
    took = [r for r in rows if r["side"] == "took"]
    kept = [r for r in rows if r["side"] == "kept"]
    return {
        "since": since,
        "took": took,
        "kept": kept,
        "took_rows": sum(r["rows"] for r in took),
        "kept_rows": sum(r["rows"] for r in kept),
        "took_runs": len(took_runs),
        "kept_runs": len(kept_runs),
        # NOT an equality: a deferral that is still outstanding has no reclaim row YET. The gap is
        # reported as a number to explain ("outstanding deferrals"), never asserted as a constant.
        "outstanding_deferrals": len(kept_runs - took_runs),
    }


def clamp_population(conn: sqlite3.Connection, *, since: Optional[int] = None) -> dict[str, Any]:
    """The runtime-clamp population, with the cap that killed it attached.

    ``predicate_matches`` is the count ``dur >= max_runtime_seconds`` returns; ``cap_share`` is the
    fraction of the population that can carry that predicate at all. Reporting both is the point:
    a clamp count without its cap coverage is a number whose predicate nobody stated.
    """
    sql = (
        "SELECT error, started_at, ended_at, max_runtime_seconds "
        "FROM task_runs WHERE status = 'timed_out'"
    )
    params: tuple = ()
    if since is not None:
        sql += " AND ended_at > ?"
        params = (since,)
    total = with_cap = predicate = 0
    by_kind: dict[str, int] = {}
    for row in conn.execute(sql, params):
        kind = limit_kind_for_error(row["error"])
        if kind is None:
            continue
        by_kind[kind] = by_kind.get(kind, 0) + 1
        if kind != LIMIT_KIND_RUNTIME_CAP:
            continue
        total += 1
        cap = row["max_runtime_seconds"]
        if cap is None:
            continue
        with_cap += 1
        if row["started_at"] is not None and row["ended_at"] is not None:
            if int(row["ended_at"]) - int(row["started_at"]) >= int(cap):
                predicate += 1
    return {
        "clamps": total,
        "with_cap": with_cap,
        "without_cap": total - with_cap,
        "cap_share": round(with_cap / total, 4) if total else None,
        "predicate_matches": predicate,
        "by_limit_kind": by_kind,
        "runtime_cap": LIMIT_KIND_RUNTIME_CAP,
        "iteration_budget": LIMIT_KIND_ITERATION_BUDGET,
    }


def signature(conn: sqlite3.Connection, *, since: Optional[int] = None) -> dict[str, Any]:
    """The whole graded table: took, kept, and the clamp population with its cap coverage."""
    ledger = verdict_ledger(conn, since=since)
    return {
        "generated_at": int(time.time()),
        "since": since,
        "took": ledger["took"],
        "kept": ledger["kept"],
        "took_rows": ledger["took_rows"],
        "kept_rows": ledger["kept_rows"],
        "took_runs": ledger["took_runs"],
        "kept_runs": ledger["kept_runs"],
        "outstanding_deferrals": ledger["outstanding_deferrals"],
        "clamp": clamp_population(conn, since=since),
    }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--db", default=None, help="kanban.db path (default: the active board)")
    ap.add_argument("--hours", type=float, default=None, help="window in hours (default: all time)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = ap.parse_args(argv)
    since = int(time.time() - args.hours * 3600) if args.hours else None
    conn = _connect(args.db)
    try:
        report = signature(conn, since=since)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    print(f"window: {'all time' if since is None else f'last {args.hours}h'}  since={since}")
    for side in ("took", "kept"):
        print(f"\n{side.upper()}  rows={report[f'{side}_rows']} runs={report[f'{side}_runs']}")
        for row in report[side]:
            print(f"  {row['gate']:24s} {row['reason']:34s} rows={row['rows']:5d} runs={row['runs']:5d}")
    clamp = report["clamp"]
    print("\nCLAMP POPULATION (runtime_cap only; iteration_budget counted separately)")
    print(f"  clamps={clamp['clamps']} with_cap={clamp['with_cap']} "
          f"without_cap={clamp['without_cap']} cap_share={clamp['cap_share']}")
    print(f"  dur>=cap predicate matches={clamp['predicate_matches']}")
    print(f"  by_limit_kind={clamp['by_limit_kind']}")
    print(f"\noutstanding deferrals (kept runs with no reclaim yet) = {report['outstanding_deferrals']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
