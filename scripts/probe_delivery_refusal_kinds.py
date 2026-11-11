#!/usr/bin/env python3
"""POST-DEPLOY write probe for queue_full refusal rows (t-37b8e0c562 e).

After the change that persists the refusal's cap is deployed, the question stops being "does the
code say the right thing" and becomes "are the rows being WRITTEN carrying it". This probe answers
that from the spool itself -- read-only, no inference, no network:

  * the newest ``--need`` (default 50) queue_full rows settled AFTER ``--since`` each carry the cap
    that fired (``limit_kind`` for a delivery cap, ``limit_class`` for a session budget) and the
    ``bot_delivery/refusal/2`` schema stamp;
  * no envelope of those rows prints a cap its record does not carry: a row with no ``limit_kind``
    renders ``cap unknown (field not persisted at write)`` and a row with one renders exactly that
    kind. This is the assertion that would have caught the historical fallback.

Exit codes: 0 = all checks hold; 2 = NOT READY (fewer than ``--need`` post-deploy rows yet -- not
a failure, just too early to conclude); 3 = at least one violation.

  python3 scripts/probe_delivery_refusal_kinds.py --since 2026-11-11T18:00:00Z
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import bot_delivery_queue as q  # noqa: E402


def _parse_instant(value: str) -> int:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    stamp = datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return int(stamp.timestamp() * 1e9)


def post_deploy_rows(root_home: str | Path, *, since_ns: int) -> list[tuple[str, dict]]:
    """Every queue_full settlement written after *since_ns*, newest first, with its seat."""
    found: list[tuple[str, dict]] = []
    for seat, home in q.spool_homes(root_home):
        for record in q._list(q.delivery_root(home), q.SETTLED_DIR):
            if record.get("reason") != q.STATUS_DETAIL_QUEUE_FULL:
                continue
            stamp = int(record.get("updated_at") or record.get("created_at") or 0)
            if stamp >= since_ns:
                found.append((seat, record))
    found.sort(key=lambda pair: int(pair[1].get("updated_at") or 0), reverse=True)
    return found


def check(rows: list[tuple[str, dict]]) -> list[str]:
    """Every way a post-deploy refusal row can fail to carry its cap. Empty list = clean."""
    problems: list[str] = []
    for seat, record in rows:
        delivery_id = record.get("delivery_id")
        kind = record.get("limit_kind")
        klass = record.get("limit_class")
        if record.get("schema") != q.REFUSAL_SCHEMA_V2:
            problems.append(f"{seat}/{delivery_id}: no {q.REFUSAL_SCHEMA_V2} stamp")
        if not (kind or klass):
            problems.append(f"{seat}/{delivery_id}: settled queue_full with NO cap recorded")
        if kind is not None and kind not in q.RENDERABLE_LIMIT_KINDS:
            problems.append(f"{seat}/{delivery_id}: limit_kind {kind!r} is not a delivery cap")
        detail = str(q.build_envelope(record).get("detail") or "")
        expected = q.queue_full_cap_literal(record)
        if expected not in detail:
            problems.append(f"{seat}/{delivery_id}: envelope detail {detail!r} does not print "
                            f"{expected!r} -- it names a cap its record does not carry")
        for other in (f"cap {x} (" for x in q.RENDERABLE_LIMIT_KINDS if x != kind):
            if kind is None and other in detail:
                problems.append(f"{seat}/{delivery_id}: envelope invents {other!r} from absence")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--home", default=None, help="install root (default: the live hermes home)")
    ap.add_argument("--since", default=None, help="ISO instant of the deploy; default: now-24h")
    ap.add_argument("--need", type=int, default=50, help="post-deploy rows required (default 50)")
    args = ap.parse_args(argv)

    from tools.bot_mode_probe import _default_home, _hermes_root

    root_home = _hermes_root(Path(args.home)) if args.home else _hermes_root(Path(_default_home()))
    since_ns = _parse_instant(args.since) if args.since else int(datetime.now(UTC).timestamp() * 1e9) - 86_400 * 10**9

    rows = post_deploy_rows(root_home, since_ns=since_ns)
    problems = check(rows)
    summary = {
        "root": str(root_home),
        "since": q._iso_instant(since_ns),
        "need": args.need,
        "post_deploy_rows": len(rows),
        "with_limit_kind": sum(1 for _s, r in rows if r.get("limit_kind")),
        "with_limit_class": sum(1 for _s, r in rows if r.get("limit_class")),
        "problems": problems[:20],
        "problem_count": len(problems),
        "census_line": q.fleet_refusal_census_line(root_home),
    }
    print(json.dumps(summary, indent=1))

    if problems:
        print(f"FAIL: {len(problems)} post-deploy refusal row(s) do not carry the cap that fired",
              file=sys.stderr)
        return 3
    if len(rows) < args.need:
        print(f"NOT READY: {len(rows)}/{args.need} post-deploy refusal rows so far "
              f"(no violation in the ones written)", file=sys.stderr)
        return 2
    print(f"OK: {len(rows)} post-deploy refusal rows, every one carrying its cap")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
