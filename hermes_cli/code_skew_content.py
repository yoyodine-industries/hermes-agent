"""ONE staleness primitive: the code a process IMPORTED vs the tree on disk.

WHY this module exists (card ``t_4dbee32e``, incident 2026-10-10)

``mtime`` and a boot fingerprint answer DIFFERENT questions. ``mtime`` says *something changed
after this process started*; the boot fingerprint says *the bytes this process imported are not
the bytes on disk*. Only the second can describe a process that has been serving stale code
since it booted, because every landing since then is OLDER than its start time. That blindness
is not theoretical: on 2026-10-10 a gateway that booted at 13:06:29 kept serving a dispatcher
that yielded forever on in-process gate bytes from 2026-09-30 --- ~270 h older than the process
running them --- while ``service_state gateway`` reported ``stale: false`` and the reason line
``idle - no service is older than the newest live-tree .py byte`` (the stale process defines the
newest byte). Four seats were starved for 24 h and a seat burned 27 h diagnosing it.

The primitive is CONTENT SKEW: the content fingerprint of the tree a process recorded at ITS own
boot, against the same fingerprint of the tree on disk NOW.

NOTHING here re-implements the fingerprint or the process identity
---------------------------------------------------------------------------
``hermes_cli.tree_fingerprint`` owns the fingerprint (full-path + content digest over every
``.py`` in the tree; inode times cannot fool it, see its ``_content_py_files``) and
``gateway.status`` owns the restart-stable process identity. This module is the one place that
COMPARES them, and every staleness question in the fleet is a call into it:

  * the dispatcher's own gate (:func:`hermes_cli.kanban_db_dispatch._consumer_tree_skew`) ---
    the check that was the ONLY detector, and which lives inside the frozen process it is
    judging, so it cannot see a process that never re-imported;
  * the carrier's content trigger (``gateway_bounce_lull`` ``stale_services()``) --- the check
    that runs in a DIFFERENT, freshly-started process and can therefore see it;
  * the CLI door below, for shell consumers and a human.

Two implementations of "is this stale?" is exactly how the 2026-10-10 blindness survived review,
so there is one: :func:`content_skew`.

A boot fingerprint must be PERSISTED to be readable from OUTSIDE the process
--------------------------------------------------------------------------
The in-process boot record (:func:`hermes_cli.tree_fingerprint.boot_record`) is a process global:
a carrier cannot read another process's memory. :func:`hermes_cli.tree_fingerprint.record_boot`
therefore persists each boot's record under ``<kanban home>/kanban/tree_boot_records/<pid>.json``,
and :func:`process_tree_fingerprint` reads it back and VERIFIES the identity it carries against
the live process --- a recycled PID answers ``None`` (unknown), never a fingerprint belonging to
the process that held the PID before.

Three verdicts, and ``None`` is one of them
-------------------------------------------
``True`` skewed, ``False`` clear, ``None`` UNKNOWN --- no boot record, an unreadable disk, a
recorded tree that is not the tree under comparison, a recycled PID. ``None`` is never quietly
folded into ``False``: a caller that treats unknown as clear reports a false green, which is the
defect this module exists to kill. The CLI maps the three verdicts to distinct exit codes so a
shell consumer cannot lose the distinction either.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

#: The import is clear: the process imported exactly the bytes on disk.
EXIT_CLEAR = 0
#: The process is SERVING STALE CODE --- actionable, and the carrier's trigger.
EXIT_SKEWED = 3
#: Nothing could be compared (no record / unreadable tree / recycled pid). NOT clear.
EXIT_UNKNOWN = 4

#: ``measure()`` verdicts, as words, so a state report can name the answer without inferring it.
VERDICT_CLEAR = "clear"
VERDICT_SKEWED = "skewed"
VERDICT_UNKNOWN = "unknown"


# --------------------------------------------------------------------------- identity


def process_identity(pid: int) -> Optional[str]:
    """Restart-stable identity of a live process: ``"<boot witness>|<boot reference>|<start>"``.

    The witness names the boot itself (``gateway.status.host_boot_witness``); the start is
    centiseconds since boot and the reference is the value that start is expressed against
    (``gateway.status.get_process_uptime_fingerprint``). A reboot breaks the pair, and --- unlike
    the absolute form --- so does nothing else while the reference holds. The reference is
    recorded because it does NOT always hold: macOS moves ``kern.boottime`` with the system
    clock, which shifts every start taken before the move by exactly the move, and a comparator
    that cannot see the move reads that shift as a recycled PID (#124570). A platform whose start
    is already boot-relative records ``0``. ``None`` when a half is unreadable --- including "the
    platform has no boot witness", whose only honest reading is inability to certify identity,
    never a difference.

    This is the ONE definition; ``kanban_db_dispatch._process_fingerprint`` delegates here so a
    persisted boot record and the liveness contract cannot drift apart.
    """
    from gateway.status import get_process_uptime_fingerprint, host_boot_witness

    witness = host_boot_witness()
    if not witness:
        return None
    fingerprint = get_process_uptime_fingerprint(int(pid))
    if fingerprint is None:
        return None
    reference, start = fingerprint
    return f"{witness}|{reference}|{start}"


# --------------------------------------------------------------------------- fingerprints


def default_tree() -> Optional[Path]:
    """The tree THIS module executes from --- the process's own live tree, resolved."""
    try:
        return Path(__file__).resolve().parent.parent
    except Exception:  # pragma: no cover - a tree without a resolvable file cannot answer
        return None


def _resolve_tree(tree) -> Optional[Path]:
    if tree is None:
        return default_tree()
    try:
        return Path(str(tree)).expanduser().resolve()
    except Exception:  # pragma: no cover - an unusable path is unknown, not clear
        return None


def disk_tree_fingerprint(tree=None) -> Optional[str]:
    """Content fingerprint of ``tree`` as it stands ON DISK now, or ``None`` when unreadable.

    A thin door onto ``hermes_cli.tree_fingerprint.content_fingerprint``: one fingerprint
    definition in the fleet, and exactly ONE content read per comparison.
    """
    resolved = _resolve_tree(tree)
    if resolved is None or not resolved.is_dir():
        return None
    try:
        from hermes_cli.tree_fingerprint import content_fingerprint

        return content_fingerprint(resolved)
    except Exception:  # pragma: no cover - an unreadable tree is unknown, never clear
        return None


def _boot_record_for(pid: int) -> Optional[dict]:
    """The VERIFIED boot record for ``pid``: in-process first, else the persisted one.

    In-process when this IS that process (``os.getpid()`` matches the record's pid): the
    process's own record is authoritative and needs no identity probe --- it cannot be another
    process. For any other pid the persisted record is used, and its recorded identity must
    still match the live process, so a recycled PID reads as unknown.
    """
    try:
        from hermes_cli import tree_fingerprint as tf

        record = tf.boot_record()
        if record and _as_pid(record.get("pid")) == int(pid) and _fingerprint_of(record):
            return dict(record, source="in-process")
        recorded = tf.recorded_boot(int(pid))
        if not recorded or not _fingerprint_of(recorded):
            return None
        identity = str(recorded.get("identity") or "").strip()
        if not identity:
            return None
        if process_identity(int(pid)) != identity:
            # A different process now holds this pid: nothing was recorded about IT.
            return None
        return dict(recorded, source="persisted")
    except Exception:  # pragma: no cover - a tree without the module answers unknown
        return None


def _as_pid(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _fingerprint_of(record: dict) -> str:
    value = record.get("fingerprint") if isinstance(record, dict) else None
    return str(value or "").strip()


def process_tree_fingerprint(pid: Optional[int] = None) -> Optional[str]:
    """The content fingerprint process ``pid`` recorded at ITS boot, or ``None`` (unknown).

    ``None`` means "nothing was recorded about this process's imported tree" --- a process that
    booted before this record existed, a dead pid, a recycled pid, an unwritable kanban home. It
    is NOT "clear": see the module docstring.
    """
    pid = os.getpid() if pid is None else _as_pid(pid)
    if pid is None:
        return None
    record = _boot_record_for(pid)
    if not record:
        return None
    return _fingerprint_of(record) or None


# --------------------------------------------------------------------------- the comparison


def measure(pid: Optional[int] = None, tree=None) -> dict:
    """The full measurement behind :func:`content_skew` --- verdict plus the evidence for it.

    Returns a dict::

        {"pid": int, "tree": str|None, "boot": str|None, "disk": str|None,
         "skew": True|False|None, "verdict": "skewed"|"clear"|"unknown",
         "source": "in-process"|"persisted"|None, "why": str}

    ONE content read per call (``disk_tree_fingerprint``), which is what lets the dispatcher's
    per-tick gate keep its exactly-once-per-tick guarantee while asking this unit.
    """
    pid = os.getpid() if pid is None else _as_pid(pid)
    tree_path = _resolve_tree(tree)
    out = {
        "pid": pid,
        "tree": str(tree_path) if tree_path else None,
        "boot": None,
        "disk": None,
        "skew": None,
        "verdict": VERDICT_UNKNOWN,
        "source": None,
        "why": "",
    }
    if pid is None:
        out["why"] = "no pid to measure"
        return out
    record = _boot_record_for(pid)
    if not record:
        out["why"] = (
            f"no boot fingerprint is recorded for pid {pid} (it booted before the record existed, "
            "or no longer runs), so its imported tree cannot be compared"
        )
        return out
    out["boot"] = _fingerprint_of(record) or None
    out["source"] = record.get("source")
    recorded_tree = _resolve_tree(record.get("tree")) if record.get("tree") else None
    if recorded_tree and tree_path and recorded_tree != tree_path:
        out["why"] = f"the recorded fingerprint belongs to {recorded_tree}, not to {tree_path}"
        return out
    out["disk"] = disk_tree_fingerprint(tree_path)
    if not out["disk"]:
        out["why"] = f"the disk fingerprint of {tree_path} could not be read"
        return out
    out["skew"] = out["boot"] != out["disk"]
    if out["skew"]:
        out["verdict"] = VERDICT_SKEWED
        out["why"] = "the imported tree content differs from the tree on disk"
    else:
        out["verdict"] = VERDICT_CLEAR
        out["why"] = "the imported tree content matches the tree on disk"
    return out


def content_skew(pid: Optional[int] = None, tree=None) -> Optional[bool]:
    """Is the code process ``pid`` imported different from the code on disk?

    ``True`` skewed, ``False`` clear, ``None`` UNKNOWN (never silently clear). The single
    staleness comparison in the fleet; see the module docstring for why there is only one.
    """
    return measure(pid, tree).get("skew")


# --------------------------------------------------------------------------- CLI door


def main(argv: Optional[list] = None) -> int:
    """``python -m hermes_cli.code_skew_content [--pid N] [--tree PATH] [--json]``.

    Exit ``0`` clear, ``3`` skewed, ``4`` unknown (see :data:`EXIT_CLEAR` / :data:`EXIT_SKEWED` /
    :data:`EXIT_UNKNOWN`). Distinct codes on purpose: a shell consumer that folds unknown into
    clear reinstates the false green this module was built to remove.
    """
    parser = argparse.ArgumentParser(
        prog="hermes code-skew",
        description="Compare the tree a process imported with the tree on disk (content skew).",
    )
    parser.add_argument("--pid", type=int, default=None,
                        help="process to ask about (default: this process)")
    parser.add_argument("--tree", default=None,
                        help="tree to compare against (default: the tree this module runs from)")
    parser.add_argument("--json", action="store_true", help="emit the measurement as JSON")
    args = parser.parse_args(argv)

    result = measure(args.pid, args.tree)
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(
            f"content-skew pid={result['pid']} tree={result['tree']} "
            f"boot={result['boot'] or '-'} disk={result['disk'] or '-'} "
            f"verdict={result['verdict']}: {result['why']}"
        )
    return {
        VERDICT_CLEAR: EXIT_CLEAR,
        VERDICT_SKEWED: EXIT_SKEWED,
    }.get(result["verdict"], EXIT_UNKNOWN)


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI test
    sys.exit(main())
