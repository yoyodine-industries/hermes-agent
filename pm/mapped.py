"""Which live processes still map files under a tree PM is about to reclaim.

POSIX unlinks a file another process has mapped without a word, so unlike Windows
(#124807) the OS never reports the hold: the tree is unlinked and the process is left
holding a vnode with no path. What then fails is consent per process - TCC refuses a
process whose executable was deleted or replaced under it - so PM has to ask the
question the OS will not.

The answer must be IDENTITY, not existence and not the name (card t_b3709ecc, measured).
A swap RE-INSTALLS a build under the same path at the moment of the cutover, so "the path
still exists" goes green over a live process holding the replaced vnode, and a name grep
misses the same hold under any other name. ``lsof`` and ``/proc/<pid>/maps`` report the
mapped vnode's inode per row, so a row is compared with ``lstat`` of its path NOW: same
inode = MAPPED, path gone = ORPHANED, another inode = REMAPPED.

Stdlib only, like the rest of pm: this runs before PM has an interpreter to lean on, and
a probe must never fail a build. An unanswerable probe is ``None`` (unknowable), which a
caller must never read as "unheld".

What "unanswerable" cannot mean: on Windows a mapped image cannot be unlinked at all, so
the predicate answers empty there and the caller's unlink is itself the probe.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

MAPPED = "mapped"
ORPHANED = "orphaned"
REMAPPED = "remapped"

#: ``(pid, mapped path, inode as the probe reported it, gone-as-already-reported)``. Inode is
#: None when the probe names a path without one (an fd symlink), and "gone" when the probe
#: itself saw the vnode unlinked (``/proc`` marks those " (deleted)").
Row = tuple[int, str, "int | None", bool]

#: One row that matters: a live process and the vnode it holds, checked against the path NOW.
@dataclass(frozen=True)
class Mapping:
    pid: int
    path: str
    state: str

    def __str__(self) -> str:
        return f"pid {self.pid} ({self.state}: {self.path})"


def _lsof() -> str | None:
    found = shutil.which("lsof")
    if found:
        return found
    return next((p for p in ("/usr/sbin/lsof", "/usr/bin/lsof") if os.path.isfile(p)), None)


def _parse_lsof(text: str) -> tuple[Row, ...]:
    """Rows from ``lsof -F pfni``: a ``p`` line opens a process, an ``f`` line a file record."""
    rows: list[Row] = []
    pid: int | None = None
    name: str | None = None
    inode: int | None = None
    ended = True

    def flush() -> None:
        nonlocal ended
        if not ended and pid is not None and name is not None and name.startswith(os.sep):
            rows.append((pid, name, inode, False))
        ended = True

    for line in text.splitlines():
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag == "p":
            flush()
            pid = int(value) if value.isdigit() else None
        elif tag == "f":
            flush()
            ended, name, inode = False, None, None
        elif tag == "n":
            name = value
        elif tag == "i":
            inode = int(value) if value.isdigit() else None
    flush()
    return tuple(rows)


def _parse_proc_maps(pid: int, text: str) -> tuple[Row, ...]:
    """Rows from ``/proc/<pid>/maps``: ``addr perms offset dev inode  path``."""
    rows: list[Row] = []
    for line in text.splitlines():
        fields = line.split(None, 5)
        if len(fields) < 6:
            continue
        name = fields[5]
        gone = name.endswith(" (deleted)")
        if gone:
            name = name[: -len(" (deleted)")]
        if not name.startswith(os.sep):
            continue
        inode = int(fields[4]) if fields[4].isdigit() else None
        rows.append((pid, name, inode, gone))
    return tuple(rows)


def _proc_snapshot(proc: Path) -> tuple[Row, ...]:
    rows: list[Row] = []
    for pid_dir in proc.glob("[0-9]*"):  # /proc: Linux only
        try:
            pid = int(pid_dir.name)
        except ValueError:
            continue
        try:
            maps = (pid_dir / "maps").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue  # exited, or another uid's process we may not read: nothing to prove either way
        rows.extend(_parse_proc_maps(pid, maps))
        try:
            handles = list(os.scandir(pid_dir / "fd"))
        except OSError:
            continue
        for handle in handles:
            try:
                target = os.readlink(handle.path)
            except OSError:
                continue  # the fd closed between scandir and readlink
            gone = target.endswith(" (deleted)")
            if gone:
                target = target[: -len(" (deleted)")]
            if target.startswith(os.sep):
                rows.append((pid, target, None, gone))
    return tuple(rows)


def snapshot() -> tuple[Row, ...] | None:
    """Every mapped path on the box, or None when no probe can tell us.

    One sweep answers every tree a caller sweeps: filtering is :func:`under`.
    """
    if os.name == "nt":
        return ()  # a mapped image cannot be unlinked there: the caller's unlink is the probe
    proc = Path("/proc")
    if (proc / "self" / "maps").is_file():
        return _proc_snapshot(proc)
    lsof = _lsof()
    if lsof is None:
        return None
    try:
        found = subprocess.run([lsof, "-n", "-F", "pfni"], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=60, stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if found.returncode not in (0, 1):  # lsof exits 1 when there is nothing to list
        return None
    return _parse_lsof(found.stdout)


def _state(path: str, inode: int | None, gone: bool) -> str:
    if gone:
        return ORPHANED
    try:
        current = os.lstat(path).st_ino
    except OSError:
        return ORPHANED
    if inode is None or inode == current:
        return MAPPED  # inode unknown (an fd): the path still resolves, so it is a live hold
    return REMAPPED


def under(rows: tuple[Row, ...], root: Path) -> tuple[Mapping, ...]:
    """The rows naming ``root`` or something beneath it, classified against the paths NOW.

    The root is resolved first: the kernel reports a vnode by its canonical path, while a caller
    may hold the tree by a symlinked spelling of it (/var is /private/var on macOS), and a prefix
    test on the unresolved text silently misses every holder. The rows are NOT re-resolved - a
    probe row is already the vnode's own path, and resolving thousands of them would touch every
    tree on the box to answer a question about one.
    """
    text = os.path.realpath(os.fspath(root))
    prefix = text.rstrip(os.sep) + os.sep
    found: dict[tuple[int, str], Mapping] = {}
    for pid, path, inode, gone in rows:
        if path != text and not path.startswith(prefix):
            continue
        # One vnode is one hold however many ways the probe lists it (a mapping shows up once
        # per region): the caller logs and counts HOLDERS, not rows.
        found.setdefault((pid, path), Mapping(pid, path, _state(path, inode, gone)))
    return tuple(found.values())


def holders(root: Path) -> tuple[Mapping, ...] | None:
    """Processes still holding a path in ``root``; empty when none; None when unanswerable."""
    rows = snapshot()
    return None if rows is None else under(rows, root)


def describe(mappings: tuple[Mapping, ...], *, limit: int = 4) -> str:
    """``pid 4242 (mapped: /…/libpython3.14.dylib); pid 77 (orphaned: …)``, bounded for a log line."""
    shown = [str(m) for m in mappings[:limit]]
    if len(mappings) > limit:
        shown.append(f"and {len(mappings) - limit} more")
    return "; ".join(shown)
