"""Behavioral tests for the state-holder and repair-admission authority."""

import os
from types import SimpleNamespace

import pytest

import hermes_state_holders


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("identity", ["alias", "different-inode", "different-device"])
def test_foreign_holder_uses_device_and_inode_not_path(
    tmp_path, monkeypatch, identity
):
    """Descriptor identity is authoritative even when /proc spells another path."""
    db_path = tmp_path / "state.db"
    db_path.touch()
    alias_path = tmp_path / "namespace-alias" / "state.db" if identity == "alias" else db_path

    proc_root = tmp_path / "proc"
    for pid in (111, 222):
        (proc_root / str(pid) / "fd").mkdir(parents=True)
    os.symlink(db_path, proc_root / "222" / "fd" / "3")

    # Keep the /proc projection local: patching os globally also intercepts
    # pytest's home guard and pathlib while they inspect these same symlinks.
    projected_os = SimpleNamespace(**vars(os))
    projected_os.getpid = lambda: 111
    monkeypatch.setattr(hermes_state_holders, "os", projected_os)
    real_listdir = os.listdir

    def _listdir(path):
        if isinstance(path, str):
            path = path.replace("/proc", str(proc_root))
        return real_listdir(path)

    monkeypatch.setattr(hermes_state_holders.os, "listdir", _listdir)

    def _readlink(path):
        if path == "/proc/222/fd/3":
            return str(alias_path)
        return os.readlink(path.replace("/proc", str(proc_root)))

    monkeypatch.setattr(hermes_state_holders.os, "readlink", _readlink)
    real_stat = os.stat

    def _stat(path, *args, **kwargs):
        mapped = str(path).replace("/proc", str(proc_root))
        result = real_stat(mapped, *args, **kwargs)
        if str(path) == "/proc/222/fd/3" and identity != "alias":
            values = list(result)
            values[1 if identity == "different-inode" else 2] += 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(hermes_state_holders.os, "stat", _stat)

    assert hermes_state_holders.foreign_state_db_holders(db_path) == (
        [(222, str(alias_path))] if identity == "alias" else []
    )


def test_windows_restart_manager_scan_sizes_then_excludes_self(monkeypatch, tmp_path):
    """The rstrtmgr lane sizes on ERROR_MORE_DATA, drops our own pid, and fails closed."""
    import ctypes

    db_path = tmp_path / "state.db"
    db_path.touch()
    (tmp_path / "state.db-wal").touch()
    calls = []

    class _Fn:
        def __init__(self, impl):
            self.impl = impl

        def __call__(self, *args):
            return self.impl(*args)

    def start(session, _flags, _key):
        session._obj.value = 7
        return 0

    def register(_session, count, names, *_rest):
        calls.append(("register", count, sorted(names)))
        return 0

    def get_list(_session, needed, count, apps, _reasons):
        needed._obj.value = 2
        if apps is None:
            return 234  # ERROR_MORE_DATA: sizing call
        apps[0].process.pid = os.getpid()
        apps[1].process.pid = 4242
        count._obj.value = 2
        return 0

    class _Api:
        RmStartSession = _Fn(start)
        RmRegisterResources = _Fn(register)
        RmGetList = _Fn(get_list)
        RmEndSession = _Fn(lambda _session: calls.append("end") or 0)

    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: _Api(), raising=False)

    holders = hermes_state_holders._windows_restart_manager_holders(db_path)

    assert [pid for pid, _ in holders] == [4242]
    assert calls[0] == ("register", 2, sorted(str(p) for p in (db_path, tmp_path / "state.db-wal")))
    assert calls[-1] == "end"

    _Api.RmStartSession = _Fn(lambda *_args: 5)
    with pytest.raises(OSError):
        hermes_state_holders._windows_restart_manager_holders(db_path)


@pytest.mark.platforms("macos")  # the psutil fallback arm
def test_psutil_fallback_never_canonicalizes_a_path_outside_the_db_directory(tmp_path, monkeypatch):
    """Only the DB's own directory can hold our file.

    libproc reports kernel-resolved paths, so a holder of ``db_path`` always names it inside
    ``realpath(db_path)``'s directory. Every other open file on the machine is a stranger's path:
    resolving it is I/O against whatever that process is using — the operator's real home when the
    gateway is up — and a refusal there must not be mistaken for evidence about this DB.
    """
    db_path = tmp_path / "state.db"
    db_path.touch()
    stranger = os.path.join(os.path.expanduser("~"), ".hermes", "kanban", "logs", "worker.log")
    foreign = str(tmp_path.parent / "someone-elses.db")
    resolved = []
    real_realpath = os.path.realpath

    def spy(path, *args, **kwargs):
        resolved.append(os.fspath(path))
        return real_realpath(path, *args, **kwargs)

    monkeypatch.setattr(hermes_state_holders, "_lsof_path", lambda: None)   # force the fallback
    monkeypatch.setattr(os.path, "realpath", spy)
    monkeypatch.setattr(hermes_state_holders, "psutil", _fake_psutil(
        {11: [stranger, foreign], 12: [str(db_path)]}))

    holders = hermes_state_holders.foreign_state_db_holders(db_path)

    assert holders == [(12, str(db_path))]
    assert [path for path in resolved if path in (stranger, foreign)] == []


@pytest.mark.platforms("macos")
def test_lsof_answers_for_the_named_paths_only(tmp_path, monkeypatch):
    """The macOS arm asks lsof about our files and never enumerates the host's other open files."""
    db_path = tmp_path / "state.db"
    db_path.touch()
    seen = []

    class _Completed:
        returncode = 0
        stdout = f"p4242\nn{db_path}\n"
        stderr = ""

    def fake_run(argv, **kwargs):
        seen.append(argv)
        return _Completed()

    monkeypatch.setattr(hermes_state_holders.subprocess, "run", fake_run)
    monkeypatch.setattr(hermes_state_holders, "_lsof_path", lambda: "/usr/sbin/lsof")

    holders = hermes_state_holders.foreign_state_db_holders(db_path)

    assert holders == [(4242, str(db_path))]
    assert len(seen) == 1
    assert seen[0][1:] == ["-n", "-P", "-F", "pn"] + list(
        hermes_state_holders._sqlite_family(str(db_path)))


@pytest.mark.platforms("macos")
def test_lsof_cannot_look_is_not_an_all_clear(tmp_path, monkeypatch):
    """lsof exits 1 for a missing WAL sidecar (harmless) AND for a path it cannot stat (not
    harmless): only the second means the answer is unknown."""
    db_path = tmp_path / "state.db"
    db_path.touch()

    class _Denied:
        returncode = 1
        stdout = ""
        stderr = f"lsof: status error on {db_path}: Permission denied\n"
        # a missing sidecar is benign and must not turn a real answer into an unknown
        stderr += "lsof: status error on /tmp/state.db-wal: No such file or directory\n"

    monkeypatch.setattr(hermes_state_holders.subprocess, "run", lambda argv, **kw: _Denied())
    monkeypatch.setattr(hermes_state_holders, "_lsof_path", lambda: "/usr/sbin/lsof")

    holders = hermes_state_holders.foreign_state_db_holders(db_path)

    assert len(holders) == 1
    assert hermes_state_holders.is_holder_scan_failure(holders[0])
    assert "status error" in holders[0][1] and str(db_path)[:20] in holders[0][1]


@pytest.mark.platforms("macos")  # the psutil fallback arm
def test_an_incomplete_scan_is_never_reported_as_a_holder(tmp_path, monkeypatch):
    """"Could not look" is not "someone is there": the sentinel is its own class, and the refusal
    says so instead of naming a holder that was never seen."""
    db_path = tmp_path / "state.db"
    db_path.touch()

    class _Denied:
        def process_iter(self, attrs=None):
            raise PermissionError("proc_pidinfo denied")

    monkeypatch.setattr(hermes_state_holders, "_lsof_path", lambda: None)
    monkeypatch.setattr(hermes_state_holders, "psutil", _Denied())

    holders = hermes_state_holders.foreign_state_db_holders(db_path)

    assert len(holders) == 1
    assert hermes_state_holders.is_holder_scan_failure(holders[0])
    assert "proc_pidinfo denied" in holders[0][1]
    assert not hermes_state_holders.is_holder_scan_failure((12, str(db_path)))

    refusal = hermes_state_holders.held_store_refusal(db_path, command="optimize-storage")
    assert refusal is not None
    assert "cannot prove" in refusal
    assert "another process is using" not in refusal
    assert "proc_pidinfo denied" in refusal


def _fake_psutil(open_files_by_pid):
    class _Process:
        def __init__(self, pid):
            self.info = {
                "pid": pid,
                "open_files": [SimpleNamespace(path=path) for path in open_files_by_pid[pid]],
            }

    class _Psutil:
        def process_iter(self, attrs=None):
            return iter([_Process(pid) for pid in open_files_by_pid])

    return _Psutil()

