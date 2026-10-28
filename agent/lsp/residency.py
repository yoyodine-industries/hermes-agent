"""Host-wide census of resident language-server processes.

``lsp.max_clients`` bounds one profile's resident set; it says nothing about the HOST.  Ten
profiles with ``max_clients: 3`` is a host ceiling of **thirty** language servers, which is the
defect shape card t_83304682 was filed against (a per-unit cap multiplying past a host cap:
measured 2026-10-26, 6-7 ``pyright-langserver`` processes at 10.8-13.3 GB RSS and ~2.4 cores).
``lsp.max_resident_servers`` is the host ceiling, and this module is what makes it enforceable
across processes: every service registers each server it spawns in one directory shared by every
profile, and each service evicts the host-wide LRU — wherever it was spawned — after its own spawn.

Design notes
------------
* **Directory, one file per server.**  A single shared JSON file would need locking for every
  touch; one entry file per server makes registration an atomic ``os.replace`` and leaves the
  hot path (:meth:`HostResidency.touch`) a single ``os.utime`` syscall.
* **Recency is the entry's mtime**, i.e. the last time its owner served a request through that
  client.  No timestamp in the payload to keep coherent between processes.
* **Liveness is psutil, not signals.**  ``os.kill(pid, 0)`` is a silent killer on Windows; psutil
  is this project's canonical answer (``pyproject.toml``) and also gives ``create_time()``, which
  is what tells a live server apart from a recycled pid.
* **An orphan is reaped.**  If the owning process died without unregistering (SIGKILL, a crashed
  gateway) its server survives ``start_new_session=True`` and holds a host slot forever; the
  census terminates it, which is the leak this card measured in the first place.
* **Nothing here raises.**  LSP is off the critical path of a session: an unwritable registry
  costs the host bound, never the turn.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("agent.lsp.residency")

#: Environment override for the census directory.  Tests point it at ``tmp_path`` so a test can
#: never evict the operator's real servers; a host whose state root differs can set it too.
RESIDENT_DIR_ENV = "HERMES_LSP_RESIDENT_DIR"

_ENTRY_SUFFIX = ".json"
_TMP_PREFIX = ".tmp-"
# A pid recycled into a different process is a different process; allow for clock rounding only.
_CREATE_TIME_TOLERANCE = 1.0


def default_resident_dir() -> str:
    """The host-wide census directory: ``<hermes root>/run/lsp-resident``.

    The *default* root, not ``HERMES_HOME``: the whole point is one directory shared by every
    profile on the host (``get_default_hermes_root()`` maps ``<root>/profiles/<name>`` back to
    ``<root>``).
    """
    override = os.environ.get(RESIDENT_DIR_ENV, "").strip()
    if override:
        return os.path.expanduser(override)
    try:
        from hermes_constants import get_default_hermes_root

        root = str(get_default_hermes_root())
    except Exception:  # pragma: no cover - config-layer import failure must not break LSP
        root = os.path.expanduser("~/.hermes")
    return os.path.join(root, "run", "lsp-resident")


def current_profile_name() -> str:
    """The profile this process serves, for the census's ``profile`` column."""
    try:
        from hermes_constants import get_hermes_home, named_profile_home

        named = named_profile_home(get_hermes_home())
        return named.name if named is not None else "default"
    except Exception:  # pragma: no cover - display only
        return "default"


def _psutil():
    try:
        import psutil

        return psutil
    except Exception:  # pragma: no cover - psutil is a core dependency
        return None


def process_state(pid: int) -> tuple[str, float]:
    """``("alive"|"gone"|"unknown", create_time)`` for *pid*.

    ``unknown`` is deliberate: an unreadable (foreign-owner) process must neither be pruned nor
    killed on a guess.  ``create_time`` is ``0.0`` when it could not be read.
    """
    psutil = _psutil()
    if psutil is None:  # pragma: no cover - psutil is a core dependency
        return "unknown", 0.0
    try:
        proc = psutil.Process(int(pid))
        create_time = float(proc.create_time())
    except Exception as exc:
        return ("gone" if type(exc).__name__ in {"NoSuchProcess", "ZombieProcess"} else "unknown"), 0.0
    try:
        if proc.status() == psutil.STATUS_ZOMBIE:
            return "gone", create_time
    except Exception:
        pass
    return "alive", create_time


@dataclass(frozen=True)
class ResidentServer:
    """One registered language-server process, as read from the host-wide census."""

    token: str
    server_id: str
    workspace_root: str
    profile: str
    pid: int
    owner_pid: int
    create_time: float
    started: float
    used: float

    def as_status(self) -> dict[str, Any]:
        """JSON-safe row for ``hermes lsp status --json``."""
        now = time.time()
        return {
            "server_id": self.server_id,
            "workspace_root": self.workspace_root,
            "profile": self.profile,
            "pid": self.pid,
            "owner_pid": self.owner_pid,
            "age_seconds": round(max(0.0, now - self.started), 1),
            "idle_seconds": round(max(0.0, now - self.used), 1),
        }


class HostResidency:
    """The host-wide resident set: registration, census, and host-wide LRU eviction.

    Cheap and stateless apart from ``directory``/``profile``; safe to construct per service and
    also on its own, read-only, by a status command in a process that owns no servers.
    """

    def __init__(self, directory: Optional[str] = None, *, profile: Optional[str] = None) -> None:
        self.directory = os.path.expanduser(directory) if directory else default_resident_dir()
        self.profile = profile or current_profile_name()

    # ---- this process's own entries ----

    def register(self, *, server_id: str, workspace_root: str, pid: Optional[int]) -> Optional[str]:
        """Publish one spawned server; returns its token (``None`` when it cannot be published)."""
        if pid is None:
            return None
        state, create_time = process_state(pid)
        if state == "gone":
            # A server that is already dead is not resident; registering it would evict a live one.
            return None
        owner_pid = os.getpid()
        _, owner_create_time = process_state(owner_pid)
        token = f"{owner_pid}-{int(pid)}-{uuid.uuid4().hex[:8]}"
        record = {
            "server_id": server_id,
            "workspace_root": workspace_root,
            "profile": self.profile,
            "pid": int(pid),
            "owner_pid": owner_pid,
            "create_time": create_time,
            "owner_create_time": owner_create_time,
            "started": time.time(),
        }
        return token if self._write(token, record) else None

    def touch(self, token: Optional[str]) -> None:
        """Refresh an entry's recency (the mtime the census reads as ``used``)."""
        if not token:
            return
        with contextlib.suppress(OSError):
            os.utime(self._path(token), None)

    def unregister(self, token: Optional[str]) -> None:
        """Drop this process's entry (idempotent; a foreign process's entry is left alone)."""
        if not token:
            return
        with contextlib.suppress(OSError):
            os.unlink(self._path(token))

    # ---- host-wide view ----

    def census(self) -> list[ResidentServer]:
        """Every live resident server on the host, least recently used first.

        Stale entries are removed on the way past: a dead pid, a pid recycled into another
        process, and an entry whose owning process is gone (whose orphaned server is terminated —
        it can never be shut down through a protocol nobody owns).
        """
        entries: list[ResidentServer] = []
        try:
            names = sorted(os.listdir(self.directory))
        except OSError:
            return entries
        for name in names:
            if not name.endswith(_ENTRY_SUFFIX):
                continue
            path = os.path.join(self.directory, name)
            token = name[: -len(_ENTRY_SUFFIX)]
            if (entry := self._read_entry(token, path)) is not None:
                entries.append(entry)
        entries.sort(key=lambda e: (e.used, e.token))
        return entries

    def victims(self, limit: int, *, keep: Optional[str] = None) -> list[ResidentServer]:
        """The entries that must go to bring the HOST within *limit* servers, LRU first.

        Pure selection — no signalling — so a caller can shut its own servers down through the
        protocol and only signal the ones another process owns.  Never returns *keep*.
        """
        if limit <= 0:
            return []
        entries = self.census()
        excess = len(entries) - limit
        if excess <= 0:
            return []
        return [entry for entry in entries if entry.token != keep][:excess]

    def terminate(self, entry: ResidentServer) -> bool:
        """SIGTERM one resident server process (a host-wide eviction victim); True when signalled.

        The entry is re-validated first: a pid recycled into another process — or a server that
        already died — is only unregistered, never signalled.
        """
        state, create_time = process_state(entry.pid)
        identity_ok = (
            not entry.create_time
            or not create_time
            or abs(create_time - entry.create_time) <= _CREATE_TIME_TOLERANCE
        )
        signalled = False
        if state == "alive" and identity_ok:
            try:
                from agent.deadline import kill_process_tree

                signalled = bool(kill_process_tree(entry.pid, sig=signal.SIGTERM))
            except Exception as exc:  # pragma: no cover - defensive: a bound must not raise
                logger.debug("host residency: could not terminate pid %s: %s", entry.pid, exc)
        self._unlink(entry.token)
        return signalled

    # ---- filesystem ----

    def _path(self, token: str) -> str:
        return os.path.join(self.directory, token + _ENTRY_SUFFIX)

    def _write(self, token: str, record: dict[str, Any]) -> bool:
        tmp = os.path.join(self.directory, f"{_TMP_PREFIX}{os.getpid()}-{uuid.uuid4().hex[:8]}")
        try:
            os.makedirs(self.directory, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(record, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path(token))
            return True
        except OSError as exc:
            logger.debug("host residency: cannot publish %s in %s: %s", token, self.directory, exc)
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            return False

    def _unlink(self, token: str) -> None:
        with contextlib.suppress(OSError):
            os.unlink(self._path(token))

    def _read_entry(self, token: str, path: str) -> Optional[ResidentServer]:
        try:
            with open(path, encoding="utf-8") as fh:
                record = json.load(fh)
            if not isinstance(record, dict):
                raise ValueError("entry is not an object")
            pid = int(record["pid"])
            owner_pid = int(record["owner_pid"])
            used = os.stat(path).st_mtime
        except (OSError, ValueError, KeyError, TypeError):
            self._unlink(token)
            return None
        state, create_time = process_state(pid)
        if state == "gone" or not _same_process(record.get("create_time"), create_time):
            self._unlink(token)
            return None
        if not self._owner_is_live(owner_pid, record.get("owner_create_time")):
            # Owner gone: nobody can shut this server down through the protocol, and it holds a
            # host slot for as long as it lives.  Reap it — this is the measured leak class.
            logger.info("lsp residency: reaping orphaned %s server pid %s (owner %s gone)",
                        record.get("server_id", "?"), pid, owner_pid)
            self.terminate(ResidentServer(
                token=token, server_id=str(record.get("server_id", "?")),
                workspace_root=str(record.get("workspace_root", "")),
                profile=str(record.get("profile", "?")), pid=pid, owner_pid=owner_pid,
                create_time=create_time, started=float(record.get("started") or 0.0), used=used,
            ))
            return None
        return ResidentServer(
            token=token,
            server_id=str(record.get("server_id", "?")),
            workspace_root=str(record.get("workspace_root", "")),
            profile=str(record.get("profile", "?")),
            pid=pid,
            owner_pid=owner_pid,
            create_time=create_time,
            started=float(record.get("started") or 0.0),
            used=used,
        )

    @staticmethod
    def _owner_is_live(owner_pid: int, recorded_create_time: Any) -> bool:
        if owner_pid == os.getpid():
            return True
        state, create_time = process_state(owner_pid)
        if state == "gone":
            return False
        if state == "unknown":
            return True  # cannot prove it dead: never reap on a guess
        return _same_process(recorded_create_time, create_time)


def _same_process(recorded: Any, observed: float) -> bool:
    """True when a recorded ``create_time`` still identifies the process now at that pid."""
    if not recorded or not observed:
        return True  # identity unverifiable: fall back to bare liveness
    try:
        return abs(float(recorded) - observed) <= _CREATE_TIME_TOLERANCE
    except (TypeError, ValueError):
        return True


__all__ = [
    "RESIDENT_DIR_ENV",
    "HostResidency",
    "ResidentServer",
    "current_profile_name",
    "default_resident_dir",
    "process_state",
]
