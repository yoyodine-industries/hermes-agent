#!/usr/bin/env python3
"""Replay the t_14655d0c cron-store incident against THIS build's write path.

The incident: a writer holding a 13-row snapshot of jobs.json wrote it back over the
44-row live store ~10 hours later, and nothing noticed. This replays that shape - a
stale snapshot written back over a store that grew underneath it - and proves the guard
REFUSES it rather than applying it.

The replay runs against a byte-copy of the live store in a throwaway HERMES_HOME, so a
build with a broken guard cannot damage the real store.

    python3 scripts/store_guard_replay.py [--store PATH]

Exit 0 = the guard refused both attempts and the copied store is byte-identical.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--store",
        default=str(Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "cron" / "jobs.json"),
        help="store to copy from (read-only)",
    )
    args = parser.parse_args()
    source = Path(args.store)
    if not source.exists():
        print(f"FAIL: no store at {source}")
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="store-guard-replay-"))
    home = workdir / ".hermes"
    (home / "cron").mkdir(parents=True)
    replay_store = home / "cron" / "jobs.json"
    shutil.copyfile(source, replay_store)
    original_bytes = replay_store.read_bytes()

    os.environ["HERMES_HOME"] = str(home)
    sys.path.insert(0, str(REPO_ROOT))
    from cron import jobs  # noqa: E402  (import after HERMES_HOME is set)

    held = json.loads(original_bytes.decode("utf-8-sig"))["jobs"]
    if not held:
        print(f"FAIL: the store at {source} has no rows to replay")
        return 2

    # 1) The writer reads now; the snapshot it will hold is exactly the live rows.
    snapshot_generation = jobs.store_generation()
    snapshot_payload = jobs.load_jobs()
    print(f"store under replay : {replay_store} (copy of {source})")
    print(f"rows in snapshot   : {len(held)}  [{snapshot_generation.describe()}]")

    # 2) Ten hours of other work: 31 more rows land in the store.
    midnight = [dict(r) for r in snapshot_payload]
    for i in range(31):
        with jobs._jobs_lock():
            current = jobs.load_jobs()
            jobs.save_jobs(current + [{
                "id": f"replay{i:06d}",
                "name": f"replay-{i}",
                "prompt": f"replay prompt {i}",
                "schedule": "every 5m",
                "deliver": "local",
                "enabled": True,
            }])
    live = jobs.store_generation()
    print(f"rows now           : {live.row_count}  [{live.describe()}]")
    before = _sha(replay_store)
    failures = 0

    # 3) The stale snapshot is written back, presenting the generation it read.
    try:
        jobs.save_jobs(snapshot_payload, expected_generation=snapshot_generation)
        print("FAIL: the stale snapshot was APPLIED (rule 1 did not fire)")
        failures += 1
    except jobs.StoreGenerationMismatch as exc:
        print(f"refused (rule 1)   : {type(exc).__name__}: {exc}")
    if _sha(replay_store) != before:
        print("FAIL: the store changed during the refused write")
        failures += 1

    # 4) The same payload with no generation at all: rule 2 must catch the unnamed drops.
    try:
        jobs.save_jobs(snapshot_payload)
        print("FAIL: the stale snapshot was APPLIED (rule 2 did not fire)")
        failures += 1
    except jobs.UnnamedRemovalRefused as exc:
        print(f"refused (rule 2)   : {type(exc).__name__}: {exc}")
    if _sha(replay_store) != before:
        print("FAIL: the store changed during the refused write")
        failures += 1

    audit = replay_store.parent / jobs.REMOVAL_AUDIT_NAME
    if audit.exists():
        print(f"FAIL: a refused write recorded an audit row in {audit}")
        failures += 1

    print(f"rows after replay  : {jobs.store_generation().row_count}")
    print(f"store sha256       : {before[:16]} == {_sha(replay_store)[:16]}")
    shutil.rmtree(workdir, ignore_errors=True)
    if failures:
        print(f"FAIL: {failures} check(s)")
        return 1
    print("PASS: the incident is refused, not applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
