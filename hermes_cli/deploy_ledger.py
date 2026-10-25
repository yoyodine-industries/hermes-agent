"""Segment-aware reader contract for the deploy ledger (card t_1b4d6bac).

THE PROBLEM. ``/opt/hermes_prod/deploy-ledger.jsonl`` is an append-only receipt log that every
landing adds a row to. By 2026-10-25 it held 34 MB and was still growing by ~33 KB per six
minutes, and every consumer read that one file whole. Truncating it is NOT an option: the
landing rows are the evidence the deploy gate checks (gate.py check 2) and the kanban proof
gate (``kanban_proof_gate.py`` R2) depend on, so a truncation silently deletes proof of work
that has already landed.

THE CONTRACT, one rule and three parts:

  * ANCHOR. The anchor path (``/opt/hermes_prod/deploy-ledger.jsonl``) is where writers append.
    It always exists and it always holds the NEWEST rows.
  * SEGMENTS. Rotation renames the anchor to a sibling segment,
    ``deploy-ledger.<UTC stamp>.jsonl`` - a rename, never a truncation, never a copy-then-empty.
    The next writer recreates the anchor by appending to it.
  * READERS RESOLVE THE SET. Nobody opens the anchor alone. ``segments(path)`` returns the
    segments oldest-first plus the anchor last, so a reader that walks the list sees the ledger
    in append order and the newest row wins as it always did. A reader that only opens the
    anchor loses every landed row a rotation has already moved, which is exactly the failure
    this module exists to prevent.

This module is deliberately dependency-free so the same logic can be mirrored verbatim by the
control-plane readers (``gate.py``, ``land.sh``) that cannot import the Hermes package.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, List, Optional

__all__ = [
    "ANCHOR",
    "segment_stamp",
    "segments",
    "iter_rows",
    "read_rows",
    "rotate",
]

ANCHOR = "/opt/hermes_prod/deploy-ledger.jsonl"

# deploy-ledger.20261025T031500Z.jsonl  (also accepts the plain .jsonl.<stamp> spelling, and a
# "-2" collision suffix from two rotations inside one second)
_STAMP = r"\d{8}T\d{6}Z(?:-\d+)?"
_SEGMENT_RE = re.compile(
    r"^(?P<stem>.+?)\.(?P<stamp>%s)(?:\.jsonl)?$|^(?P<stem2>.+?)\.jsonl\.(?P<stamp2>%s)$" % (_STAMP, _STAMP)
)


def segment_stamp(now: Optional[datetime] = None) -> str:
    """The UTC stamp used in a segment name. Fixed width, sorts lexicographically."""
    moment = now or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _segment_name(anchor: Path, stamp: str) -> str:
    base = anchor.name
    if base.endswith(".jsonl"):
        base = base[: -len(".jsonl")]
    return "%s.%s.jsonl" % (base, stamp)


def segments(path: str = ANCHOR) -> List[str]:
    """Every file that makes up the ledger at *path*, OLDEST FIRST, anchor last.

    A missing anchor with segments present is normal: the ledger is read between a rotation and
    the next append. Only the returned paths are the ledger; every file is a rename away from
    the same append-only history.
    """
    anchor = Path(path)
    directory = anchor.parent if str(anchor.parent) else Path(".")
    base = anchor.name
    stem = base[: -len(".jsonl")] if base.endswith(".jsonl") else base

    found: List[tuple[str, str]] = []
    try:
        entries = list(directory.iterdir())
    except OSError:
        entries = []

    for entry in entries:
        if not entry.is_file() or entry.name == base:
            continue
        match = _SEGMENT_RE.match(entry.name)
        if not match:
            continue
        name_stem = match.group("stem") or match.group("stem2") or ""
        stamp = match.group("stamp") or match.group("stamp2") or ""
        if name_stem == stem:
            found.append((stamp, entry.name))

    found.sort()
    ordered = [str(directory / name) for _, name in found]
    ordered.append(str(anchor))
    return ordered


def iter_rows(path: str = ANCHOR, *, line_cap: int = 8 * 1024 * 1024) -> Iterator[dict]:
    """Yield every JSON object row across the segment set, in append order.

    *line_cap* skips a pathological line (a partial write, a runaway row) instead of reading it
    into memory; callers that already had a cap keep theirs.
    """
    for segment in segments(path):
        try:
            handle = open(segment, "r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line or (line_cap and len(line) > line_cap):
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    yield row


def read_rows(path: str = ANCHOR) -> List[dict]:
    """:func:`iter_rows` materialised - the shape callers that list() the ledger already use."""
    return list(iter_rows(path))


def rotate(path: str = ANCHOR, *, min_bytes: int = 0, now: Optional[datetime] = None) -> Optional[str]:
    """Rotate the anchor into a stamped segment. Returns the new segment name, or None.

    Returns None when the anchor is missing/empty (nothing to rotate) or still below
    *min_bytes*. The rename is atomic within the directory and carries the whole file, so no
    row is ever dropped: after a rotation the ledger is ``segments(path)`` and every reader that
    resolves the set sees exactly the rows it saw before, plus whatever lands next.
    """
    anchor = Path(path)
    try:
        size = anchor.stat().st_size
    except OSError:
        return None
    if size <= 0 or size < int(min_bytes):
        return None

    stamp = segment_stamp(now)
    target = anchor.parent / _segment_name(anchor, stamp)
    suffix = 1
    while target.exists():  # two rotations inside one second must not collide
        suffix += 1
        target = anchor.parent / _segment_name(anchor, "%s-%d" % (stamp, suffix))

    os.rename(str(anchor), str(target))
    try:  # make the rename durable before a writer recreates the anchor
        directory_fd = os.open(str(anchor.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        pass
    return target.name


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Rotate the deploy ledger into a stamped segment.")
    parser.add_argument("--path", default=ANCHOR)
    parser.add_argument("--min-bytes", type=int, default=8 * 1024 * 1024,
                        help="rotate only when the anchor is at least this large (default 8 MiB)")
    parser.add_argument("--segments", action="store_true", help="print the segment set and exit")
    arguments = parser.parse_args(list(argv) if argv is not None else None)

    if arguments.segments:
        for segment in segments(arguments.path):
            print(segment)
        return 0

    rotated = rotate(arguments.path, min_bytes=arguments.min_bytes)
    if rotated is None:
        print("no rotation: anchor missing, empty, or below %d bytes" % arguments.min_bytes)
        return 0
    print(rotated)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
