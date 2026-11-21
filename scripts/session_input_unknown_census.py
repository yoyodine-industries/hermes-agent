#!/usr/bin/env python3
"""Census of `unknown` session admissions across every store - the before/after receipt.

WHY THIS EXISTS. An `unknown` admission pauses its session's FIFO until somebody settles it,
so the population is a health number and not a curiosity: it is minted whenever a turn is lost
(or a settlement write fails), and it is supposed to be cleared by the unattended release edge.
A before/after claim about that population has to be reconcilable with somebody else's count,
and the only way to make it so is to print the population you counted:

    STORE <path> · epoch · instance
    TABLE session_admissions · PREDICATE status='unknown' · rows N

Two people quoting 14 and 17 disagreed because one had counted a different TABLE in the same
file and both had measured a moving target. So every row prints its identity
(seq, status, outcome, owner_epoch, generation), its CLASS under the RUNTIME's own predicate
(`gateway.session_unattended_unknown.is_unattended` - imported, never re-spelled here) and any
resolution id already recorded for it.

READ-ONLY. Every store is opened with `mode=ro`; this script cannot mint, settle or move a row.

    python3 scripts/session_input_unknown_census.py            # human table
    python3 scripts/session_input_unknown_census.py --json     # same rows, machine readable
    python3 scripts/session_input_unknown_census.py --store PATH --store PATH
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gateway.session_unattended_unknown import is_unattended  # noqa: E402  - ONE predicate

PREDICATE = "status='unknown'"
TABLE = "session_admissions"
RESOLUTION_KEY = "session_input.resolution.v1.%s"
COLUMNS = ("seq", "status", "outcome", "owner_epoch", "generation")


def _stores(cli_stores):
    if cli_stores:
        return list(cli_stores)
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    found = [os.path.join(home, "state.db")]
    found += sorted(glob.glob(os.path.join(home, "profiles", "*", "state.db")))
    return [path for path in found if os.path.exists(path)]


def _connect(path):
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True)


def _epoch(conn):
    try:
        row = conn.execute("SELECT epoch, instance_id FROM runtime_epoch WHERE singleton=1").fetchone()
    except sqlite3.Error:
        return None, None
    return (row[0], row[1]) if row else (None, None)


def _resolutions(conn, admission_ids):
    """Resolution ids already recorded, keyed by admission id - the idempotency record."""
    out = {}
    for admission_id in admission_ids:
        try:
            row = conn.execute("SELECT value FROM state_meta WHERE key=?",
                               (RESOLUTION_KEY % admission_id,)).fetchone()
        except sqlite3.Error:
            return out
        if row:
            try:
                out[admission_id] = json.loads(row[0]).get("resolution_id")
            except (ValueError, AttributeError):
                out[admission_id] = row[0]
    return out


def census_one(path):
    conn = _connect(path)
    try:
        epoch, instance = _epoch(conn)
        try:
            rows = conn.execute(
                "SELECT %s, admission_id, target_session_id, intent, principal_id, payload_json "
                "FROM %s WHERE %s ORDER BY seq" % (", ".join(COLUMNS), TABLE, PREDICATE)).fetchall()
        except sqlite3.Error as exc:
            return {"store": path, "epoch": epoch, "instance_id": instance,
                    "table": TABLE, "predicate": PREDICATE, "error": str(exc), "rows": []}
        admissions = [row[len(COLUMNS)] for row in rows]
        resolutions = _resolutions(conn, admissions)
        records = []
        for raw in rows:
            head = dict(zip(COLUMNS, raw[:len(COLUMNS)]))
            admission_id, session_id = raw[len(COLUMNS)], raw[len(COLUMNS) + 1]
            intent, principal, payload_json = (raw[len(COLUMNS) + 2], raw[len(COLUMNS) + 3],
                                               raw[len(COLUMNS) + 4])
            try:
                payload = json.loads(payload_json or "{}")
            except ValueError:
                payload = {}
            row = {"principal_id": principal, "payload": payload, "status": head["status"]}
            records.append({
                **head,
                "admission_id": admission_id,
                "session_id": session_id,
                "intent": intent,
                "principal_id": principal,
                "payload_kind": (sorted(set(payload) & {"kanban_card_v1", "local_automation_v1",
                                                        "webhook_delivery", "native_text_v1"})
                                 or ["-"]),
                "class": "unattended" if is_unattended(row) else "attended",
                "cleared_by": resolutions.get(admission_id) or "-",
            })
        return {"store": path, "epoch": epoch, "instance_id": instance, "table": TABLE,
                "predicate": PREDICATE, "rows": records}
    finally:
        conn.close()


def _print_human(stores):
    total = unattended = 0
    epochs = set()
    for store in stores:
        label = store["store"].replace(os.path.expanduser("~"), "~")
        print("STORE %s" % label)
        print("  epoch=%s instance=%s" % (store.get("epoch"), store.get("instance_id")))
        print("  TABLE %s  PREDICATE %s  rows %d" % (store["table"], store["predicate"],
                                                      len(store["rows"])))
        if store.get("error"):
            print("  ERROR %s" % store["error"])
            continue
        for row in store["rows"]:
            epochs.add(row["owner_epoch"])
            total += 1
            if row["class"] == "unattended":
                unattended += 1
            print("  %6s | %-7s | outcome=%-8s | owner_epoch=%-4s | gen=%s | %-10s | %s | "
                  "cleared_by=%s" % (row["seq"], row["status"], row["outcome"],
                                     row["owner_epoch"], row["generation"], row["class"],
                                     ",".join(row["payload_kind"]), row["cleared_by"]))
    print("SUMMARY stores=%d rows=%d unattended=%d attended=%d owner_epochs=%s"
          % (len(stores), total, unattended, total - unattended,
             ",".join(str(e) for e in sorted(x for x in epochs if x is not None)) or "-"))
    return total, unattended


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", action="append", default=[],
                        help="a state.db to census (repeatable; default: HERMES_HOME + profiles/*)")
    parser.add_argument("--json", action="store_true", help="emit the same rows as JSON")
    args = parser.parse_args(argv)
    stores = [census_one(path) for path in _stores(args.store)]
    if args.json:
        print(json.dumps({"table": TABLE, "predicate": PREDICATE, "stores": stores}, indent=2))
        return 0
    _print_human(stores)
    return 0


if __name__ == "__main__":
    sys.exit(main())
