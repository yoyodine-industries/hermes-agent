"""A retired entry is never unlinked while a live process still maps it (card t_5012d16d).

The store's holding predicate must be IDENTITY, not existence and not the name: a swap
re-installs a build under the same path at the moment of the cutover, so "the path exists" goes
green over a process holding the REPLACED vnode, and a name grep misses the hold under any other
name (measured in card t_b3709ecc). These fixtures hold the vnode with a live process and assert
what PM does about it - and what it must NOT do, which is delete the vnode the process is running.

The holder is a real second process, never a mock: the predicate reads the kernel's view of the
mapping (lsof / /proc), so a mock would only prove the mock.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from pm import mapped
from pm.install import _discard_entry
from pm.lock import Facts
from pm.store import Store
from tests.pm._fixtures import served as served  # noqa: PLC0414 -- the self-alias suppresses F811 for the fixture parameter
from tests.pm.test_pm_core import pm_env as pm_env  # noqa: PLC0414 -- the self-alias suppresses F811 for the fixture parameter

_HOLD_SECONDS = 120

# The holder maps a file inside the tree and sleeps: a mapping is what the predicate reads, and
# it is the same kernel state an interpreter's own txt mapping lives in (one row, one vnode).
_HOLD = """
import mmap, sys, time
handle = open(sys.argv[1], "rb")
region = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
time.sleep(float(sys.argv[2]))
"""


def _probe_or_skip() -> None:
    if mapped.snapshot() is None:
        pytest.skip("no lsof and no /proc here: the hold cannot be proven, and PM must not guess")


def _seen(root: Path, pid: int, timeout: float = 20.0) -> bool:
    """Wait until the kernel reports ``pid`` holding something in ``root``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        held = mapped.holders(root)
        if held is not None and any(m.pid == pid for m in held):
            return True
        time.sleep(0.05)
    return False


@contextmanager
def _holder(path: Path):
    """A live process mapping ``path``; yields its pid once the kernel can see it."""
    proc = subprocess.Popen([sys.executable, "-c", _HOLD, str(path), str(_HOLD_SECONDS)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        assert _seen(path.parent, proc.pid), "the holder never appeared in the predicate"
        yield proc.pid
    finally:
        proc.terminate()
        proc.wait(timeout=15)


# --- the predicate's own reading of each probe format -------------------------------------------------

def test_lsof_rows_carry_the_mapped_vnode():
    """``lsof -F pfni`` output: a ``p`` line opens a process, an ``f`` line a file record.

    The real shape, measured on the host: a txt row is ``f``, ``t``, ``i<inode>``, ``n<path>``,
    and a socket row's name is not a path at all and must never become a row.
    """
    dump = (
        "p4242\n"
        "fcwd\ni261\nn/private/tmp\n"
        "ftxt\ni1152921500312607307\nn/bin/bash\n"
        "ftxt\ni2559189874\nn/var/tree/.reclaim-abc/bin/python3\n"
        "f3\ntPIPE\nd0x1234\nn->0x5678\n"
        "p99\n"
        "ftxt\ni2559189875\nn/var/tree/.reclaim-abc/lib/python3.14/os.py\n"
    )
    assert mapped._parse_lsof(dump) == (
        (4242, "/private/tmp", 261, False),
        (4242, "/bin/bash", 1152921500312607307, False),
        (4242, "/var/tree/.reclaim-abc/bin/python3", 2559189874, False),
        (99, "/var/tree/.reclaim-abc/lib/python3.14/os.py", 2559189875, False),
    )


def test_proc_maps_rows_carry_the_mapped_vnode():
    """``/proc/<pid>/maps`` rows: ``addr perms offset dev inode path``, " (deleted)" included.

    A deleted vnode's row says so in the path column, and that flag alone is proof of ORPHANED -
    it must survive parsing, or a replacement file at the same path would read as healthy.
    """
    dump = (
        "7f00-7f01 r--p 00000000 08:01 2559189874  /var/tree/bin/python3\n"
        "7f01-7f02 r-xp 00001000 08:01 2559189875  /var/tree/lib/libpython3.14.so (deleted)\n"
        "7f02-7f03 rw-p 00002000 00:00 0        [heap]\n"
        "7f03-7f04 rw-s 00003000 00:00 41       /dev/shm/socket\n"
    )
    assert mapped._parse_proc_maps(7, dump) == (
        (7, "/var/tree/bin/python3", 2559189874, False),
        (7, "/var/tree/lib/libpython3.14.so", 2559189875, True),
        (7, "/dev/shm/socket", 41, False),
    )


def test_identity_separates_mapped_from_orphaned_and_remapped(tmp_path):
    """The three answers, from one path: mapped, ORPHANED once gone, REMAPPED under the same name.

    The last one is the case that bit us: the swap RE-INSTALLS a build at the same path, so an
    existence test reports the path healthy while the process holds the vnode that was replaced.
    """
    blob = tmp_path / "blob.bin"
    blob.write_bytes(b"x" * 64)
    inode = blob.stat().st_ino
    rows = ((42, str(blob), inode, False),)
    assert [m.state for m in mapped.under(rows, tmp_path)] == [mapped.MAPPED]

    blob.unlink()
    assert [m.state for m in mapped.under(rows, tmp_path)] == [mapped.ORPHANED]

    blob.write_bytes(b"y" * 64)  # same name, different vnode
    assert [m.state for m in mapped.under(rows, tmp_path)] == [mapped.REMAPPED]


def test_rows_outside_the_tree_are_not_holders(tmp_path):
    """A sibling whose name merely starts with the tree's name is a different tree."""
    tree = tmp_path / ".reclaim-abc"
    tree.mkdir()
    (tree / "blob.bin").write_bytes(b"x")
    sibling = tmp_path / ".reclaim-abcd"
    sibling.mkdir()
    (sibling / "blob.bin").write_bytes(b"x")
    rows = ((7, str(sibling / "blob.bin"), (sibling / "blob.bin").stat().st_ino, False),)
    assert mapped.under(rows, tree) == ()


# --- the retirement door -----------------------------------------------------------------------------

@pytest.mark.platforms("posix")
def test_retirement_moves_a_mapped_entry_instead_of_unlinking_it(pm_env, caplog):
    """A replaced entry a live process maps is RETIRED to ``.reclaim-*``: bytes intact, pid named.

    Then the same gc reclaims it once the holder is gone - the change must not turn every
    uninstall into a leak.
    """
    _probe_or_skip()
    from pm.cli import cmd_gc
    from pm.install import ensure

    _, runtime, *_ = pm_env
    ensure("faketool", base_env={})
    entry = runtime / Facts(runtime / "facts.json").get("faketool")["entry"]
    blob = entry / "blob.bin"
    blob.write_bytes(b"x" * 4096)
    inode = blob.stat().st_ino

    with _holder(blob) as pid:
        with caplog.at_level(logging.WARNING, logger="pm.install"):
            ensure("faketool", explicit=True, base_env={})
        retired = list(runtime.glob(".reclaim-*"))
        assert len(retired) == 1, "the replaced entry was not retired"
        moved = retired[0] / "blob.bin"
        assert moved.is_file() and moved.stat().st_ino == inode, "the mapped bytes were unlinked"
        assert (entry / "bin/faketool").is_file()
        assert f"pid {pid}" in caplog.text, "the log must name the process to recycle"

    cmd_gc(None)
    assert not list(runtime.glob(".reclaim-*")), "an unheld set-aside tree must still be reclaimed"
    assert (entry / "bin/faketool").is_file()


@pytest.mark.platforms("posix")
def test_retirement_refuses_a_name_that_escapes_the_store(pm_env, tmp_path):
    """The retire is a RENAME inside one directory: a name that leaves the store root is refused.

    Without the same-store assertion a caller bug renames bytes into somebody else's tree (or
    fails EXDEV after the entry has already moved), and nothing downstream could tell.
    """
    _, runtime, *_ = pm_env
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(RuntimeError, match="outside the store root"):
        _discard_entry(Store(runtime), "../outside")
    assert outside.is_dir()


# --- the reclaim door (gc AND the next install share it) ---------------------------------------------

@pytest.mark.platforms("posix")
def test_gc_keeps_a_set_aside_tree_a_live_process_maps_then_reclaims_it(pm_env, caplog):
    _probe_or_skip()
    from pm.cli import cmd_gc
    from pm.install import ensure

    _, runtime, *_ = pm_env
    ensure("faketool", base_env={})
    tree = runtime / ".reclaim-0123456789abcdef0123456789abcdef"
    tree.mkdir()
    blob = tree / "blob.bin"
    blob.write_bytes(b"x" * 4096)

    with _holder(blob) as pid:
        with caplog.at_level(logging.WARNING, logger="pm.install"):
            cmd_gc(None)
        assert tree.is_dir(), "a mapped set-aside tree was unlinked"
        assert f"pid {pid}" in caplog.text, "the log must name the process to recycle"

    cmd_gc(None)
    assert not tree.exists(), "the tree must be reclaimed once nothing maps it"


@pytest.mark.platforms("posix")
def test_gc_removes_an_unheld_set_aside_tree(pm_env):
    """The false-positive half: with nothing mapped the tree IS deleted, as it always was."""
    _probe_or_skip()
    from pm.cli import cmd_gc
    from pm.install import ensure

    _, runtime, *_ = pm_env
    ensure("faketool", base_env={})
    tree = runtime / ".reclaim-fedcba9876543210fedcba9876543210"
    (tree / "bin").mkdir(parents=True)
    (tree / "bin" / "faketool").write_bytes(b"#!x")

    cmd_gc(None)
    assert not tree.exists()


@pytest.mark.platforms("posix")
def test_gc_keeps_an_orphaned_entry_a_live_process_maps(pm_env, capsys):
    """An orphaned package version is the same hazard as a set-aside tree, so gc asks too.

    It was skipped from the card's census because it is not a *retired* tree, but deleting it
    blinds a process exactly the same way - one guard, every door that unlinks a published tree.
    """
    _probe_or_skip()
    from pm.cli import cmd_gc
    from pm.install import ensure

    _, runtime, *_ = pm_env
    ensure("faketool", base_env={})
    orphan = runtime / "faketool-0.9-nowhere"
    orphan.mkdir()
    blob = orphan / "blob.bin"
    blob.write_bytes(b"x" * 4096)

    with _holder(blob) as pid:
        cmd_gc(None)
        out = capsys.readouterr().out
        assert orphan.is_dir(), "a mapped orphaned entry was unlinked"
        assert f"pid {pid}" in out, "gc must name the process holding the entry it kept"

    cmd_gc(None)
    assert not orphan.exists()


# --- the constraint the whole change exists for ------------------------------------------------------

_EXEC_HOLD = """
import os, sys, time
go = sys.argv[1]
print("ready", flush=True)
deadline = time.time() + 60
text = ""
while time.time() < deadline:
    try:
        text = open(go, encoding="utf-8").read().strip()
    except OSError:
        text = ""
    if text:
        break
    time.sleep(0.05)
stat = os.stat(text)                                  # the path it maps, by its NEW name
fresh = os.path.join(os.path.dirname(os.path.dirname(text)), "fresh.txt")
with open(fresh, "w", encoding="utf-8") as handle:     # a fresh open of a path it never touched
    handle.write("ok")
print(f"{os.getpid()} {stat.st_ino} {os.path.exists(fresh)}", flush=True)
"""


@pytest.mark.platforms("posix")
def test_a_running_interpreter_keeps_working_after_its_tree_is_renamed(tmp_path):
    """The point of rename-only retirement: the holder's mapping survives the move.

    Not a name check: the process itself stats ITS mapped path under the new name and opens a
    file it has never touched, which is the consent path a deleted or replaced vnode breaks.
    """
    _probe_or_skip()
    source = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    tree = tmp_path / ".reclaim-exec"
    (tree / "bin").mkdir(parents=True)
    interpreter = tree / "bin" / "python3"
    shutil.copyfile(source, interpreter)
    interpreter.chmod(0o755)
    inode = interpreter.stat().st_ino

    go = tmp_path / "go.txt"
    env = dict(os.environ, PYTHONHOME=str(source.parent.parent))
    proc = subprocess.Popen([str(interpreter), "-c", _EXEC_HOLD, str(go)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env)
    try:
        if proc.stdout.readline().strip() != "ready":
            proc.kill()
            _out, err = proc.communicate(timeout=15)
            pytest.skip(f"a copy of {source} does not run on this host (no exec mapping to test): {err}")
        assert _seen(tree, proc.pid), "the interpreter's own mapping never appeared in the predicate"
        moved = tmp_path / ".reclaim-exec-moved"
        tree.rename(moved)
        go.write_text(str(moved / "bin" / "python3"), encoding="utf-8")
        out = proc.stdout.readline()
        _out, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=15)

    assert out.split() == [str(proc.pid), str(inode), "True"], f"the holder lost its mapping: {err}"
