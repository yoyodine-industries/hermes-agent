"""The carrier's SUPERVISOR: file-backed output and a terminal receipt that outlive the scheduler.

Design: card ``t_9fbcfe35`` (``DESIGN-t_9fbcfe35-carrier-receipt-and-restart-notice.md``), unit U2.
Ruling R1.

WHY THIS EXISTS.  A ``no_agent`` script job used to be spawned with
``stdout=subprocess.PIPE, stderr=subprocess.PIPE`` - the scheduler owned both pipes.  When the
gateway restarted mid-run (2026-11-27 02:43:23) the orphaned carrier's writes went nowhere (EPIPE),
its exit was unobservable, and the attempt died as ``unknown`` with one log line.  The carrier had
no output channel and no terminal record that outlived the scheduler.

This process IS that channel.  ``cron.scheduler_script._run_job_script`` Popens it instead of the
carrier when the fire carries ``HERMES_CRON_EXECUTION_ID``; the supervisor:

  1. writes the receipt ``status:"running"`` naming ITSELF, before the carrier exists, then again
     naming the CARRIER's pid and start-time fingerprint (U1's :mod:`cron.carrier_receipts`),
  2. runs the carrier argv with stdout/stderr redirected to
     ``$HERMES_HOME/cron/runs/<execution_id>.out`` / ``.err`` - real files, not pipes, so a dead
     scheduler changes nothing for the writer,
  3. writes ``status:"exited"`` with the carrier's exit code (or, when the scheduler's own budget
     fired, the timeout facts the wiring recorded before tree-killing us) and only then,
  4. copies the captured bytes to its own stdout/stderr BEST-EFFORT and exits with the carrier's
     exit status.

A DEAD SCHEDULER MUST NEVER CHANGE THE OUTCOME.  The pass-through catches ``BrokenPipeError`` and
``OSError`` and then points fds 1/2 at ``os.devnull`` so the interpreter's shutdown flush cannot
turn a closed pipe into a non-zero exit.  The receipt is already on disk by that point; the exit
code is the carrier's, never the delivery channel's.

THE SUPERVISOR MUST NOT BE ABLE TO DENY THE CARRIER ITS RUN.  Every receipt operation is
best-effort (a failure is written to ``<execution_id>.supervisor.log`` and the carrier still runs),
and the receipt module is loaded WITHOUT executing ``cron/__init__.py``: that package pulls the
whole scheduler into a process whose only job is to spawn a script and write two small JSON files.

Kill switch: the WIRING reads ``HERMES_CRON_CARRIER_RECEIPTS`` through
``cron.env_settings.cron_env_setting``; ``0`` restores the direct ``Popen`` path byte for byte.
This module is never imported on that path.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

#: Separates the supervisor's own arguments from the carrier argv it wraps.
ARGV_SEPARATOR = "--"

#: The kill switch the WIRING reads (see the module docstring).  Named here for the reader; the
#: supervisor never reads it - the wiring decides whether this process is spawned at all.
KILL_SWITCH = "HERMES_CRON_CARRIER_RECEIPTS"

#: The occurrence identity the fire exports (``cron.execution_identity.cron_execution_env``).
EXECUTION_ENV = "HERMES_CRON_EXECUTION_ID"
JOB_ENV = "HERMES_CRON_JOB_ID"

#: The signal the scheduler's tree-kill uses when a script timeout fires
#: (``agent.deadline.kill_process_tree`` defaults to SIGKILL, so the supervisor is not given a
#: chance to run its own terminal write - the wiring records the timeout BEFORE the kill).
#: Windows has no ``SIGKILL`` name and its tree-kill is ``taskkill /F``; the closest defined name
#: is recorded instead so the notice still says how it ended.
_KILL_SIGNAL = getattr(signal, "SIGKILL", None) or signal.SIGTERM
TIMEOUT_SIGNAL = "SIGKILL" if hasattr(signal, "SIGKILL") else "taskkill /F"

#: Copy buffer for the captured output; a carrier report can be large and must not be slurped.
_COPY_CHUNK = 64 * 1024

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

_RECEIPTS: Any = None


# ── Loading ──────────────────────────────────────────────────────────────────────────────────


def _repo_root() -> Path:
    """The checkout root this module lives in (``.../cron/carrier_supervisor.py`` -> ``...``)."""
    return Path(__file__).resolve().parent.parent


def _ensure_importable() -> None:
    """Put the checkout root on ``sys.path``.

    The wiring runs this file by ABSOLUTE PATH (the job's cwd is the script's own directory), so
    ``sys.path[0]`` is ``.../cron`` and neither ``cron`` nor ``hermes_constants`` is importable
    from it.  Doing this once here keeps the supervisor independent of the caller's PYTHONPATH.
    """
    root = str(_repo_root())
    if root not in sys.path:
        sys.path.insert(0, root)


def _receipts() -> Any:
    """U1's :mod:`cron.carrier_receipts`, loaded as a module without ``cron/__init__.py``.

    Importing the ``cron`` package would execute the scheduler (and the agent stack) inside the
    carrier's parent.  ``carrier_receipts`` itself has no import-time dependency on the package,
    so loading the file directly is both cheaper and strictly safer: a supervisor that fails to
    import must never prevent the carrier from running.
    """
    global _RECEIPTS
    if _RECEIPTS is None:
        import importlib.util

        loaded = sys.modules.get("cron.carrier_receipts")
        if loaded is not None:
            # The scheduler's own process already has U1's module; use that instance so there is
            # exactly one receipt implementation per process.
            _RECEIPTS = loaded
            return _RECEIPTS
        _ensure_importable()
        path = Path(__file__).resolve().with_name("carrier_receipts.py")
        spec = importlib.util.spec_from_file_location("_hermes_cron_carrier_receipts", path)
        if spec is None or spec.loader is None:  # pragma: no cover - only on a broken checkout
            raise ImportError(f"cannot load carrier receipts from {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _RECEIPTS = module
    return _RECEIPTS


# ── Paths and small facts ───────────────────────────────────────────────────────────────────


def runs_dir() -> Path:
    """``$HERMES_HOME/cron/runs`` - the captured output of every supervised carrier."""
    from hermes_constants import get_hermes_home

    return get_hermes_home().resolve() / "cron" / "runs"


def carrier_output_paths(execution_id: str) -> tuple[Path, Path]:
    """``(<execution_id>.out, <execution_id>.err)`` under :func:`runs_dir`.

    The execution id is validated by the receipt module's own path guard, so a junk value can
    never address a file outside this profile's run directory.
    """
    receipts = _receipts()
    receipts.receipt_path(execution_id)  # raises ValueError on a value that is not an id
    directory = runs_dir()
    return directory / f"{execution_id}.out", directory / f"{execution_id}.err"


def _now() -> str:
    try:
        from hermes_time import now

        return now().isoformat()
    except Exception:  # pragma: no cover - hermes_time is always importable in-tree
        from datetime import datetime

        return datetime.now().astimezone().isoformat()


def _fingerprint(pid: int) -> Optional[int]:
    """The ledger's own start-time fingerprint for *pid*, or None when it cannot be read."""
    try:
        from gateway.status import get_process_start_time

        return get_process_start_time(int(pid))
    except Exception:
        return None


def _profile_name() -> str:
    try:
        from hermes_constants import get_hermes_home, profile_name_for_home

        return profile_name_for_home(get_hermes_home()) or "default"
    except Exception:  # pragma: no cover - hermes_constants is always importable in-tree
        return "default"


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _diagnostic(execution_id: Optional[str], message: str) -> None:
    """Record a supervisor-side problem without touching the carrier's own ``.err``.

    ``.err`` is the carrier's error text and the notice quotes it, so supervisor diagnostics go
    to their own file beside the captured output.  Best-effort: a logging failure is never allowed
    to affect the run.
    """
    try:
        directory = runs_dir()
        directory.mkdir(parents=True, exist_ok=True)
        token = execution_id if execution_id and _TOKEN_RE.fullmatch(execution_id) else "supervisor"
        with (directory / f"{token}.supervisor.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{_now()} [{os.getpid()}] {message}\n")
    except Exception:
        pass


# ── Receipt writes ──────────────────────────────────────────────────────────────────────────


def _write(record: dict[str, Any]) -> bool:
    """Write *record* atomically; never raises."""
    try:
        _receipts().write_receipt(dict(record))
        return True
    except Exception as exc:
        _diagnostic(str(record.get("execution_id") or ""), f"receipt write failed: {exc}")
        return False


def _new_record(execution_id: str, out_path: Path, err_path: Path) -> dict[str, Any]:
    return _receipts().new_receipt(
        execution_id=execution_id,
        job_id=_env(JOB_ENV),
        profile=_profile_name(),
        status="running",
        started_at=_now(),
        stdout_path=str(out_path),
        stderr_path=str(err_path),
    )


def record_timeout(
    identity: Optional[Mapping[str, str]],
    timeout_s: int,
    *,
    signal_name: str = TIMEOUT_SIGNAL,
) -> bool:
    """Record the SCHEDULER's own budget killing the run, BEFORE the tree-kill lands.

    Called by ``cron.scheduler_script._run_job_script`` when its script deadline expires.  The
    kill is SIGKILL over the whole tree, so the supervisor gets no terminal write of its own: if
    this record is not on disk first, the attempt would settle without ever saying why.

    A receipt the carrier (or an earlier timeout) already made terminal is NEVER overwritten -
    the carrier's own exit is the more precise truth.  Returns True when a record was written.
    """
    execution_id = str((identity or {}).get(EXECUTION_ENV) or "").strip()
    if not execution_id:
        return False
    try:
        receipts = _receipts()
        record = receipts.read_receipt(execution_id)
        if record is not None and str(record.get("status") or "") == receipts.STATUS_EXITED:
            return False
        if record is None:
            out_path, err_path = carrier_output_paths(execution_id)
            record = _new_record(execution_id, out_path, err_path)
        record.update(
            status=receipts.STATUS_EXITED,
            exit_code=-int(_KILL_SIGNAL),
            finished_at=_now(),
            timeout_s=int(timeout_s),
            signal=str(signal_name),
        )
        return _write(record)
    except Exception as exc:
        _diagnostic(execution_id, f"could not record the scheduler timeout: {exc}")
        return False


def _finish(record: dict[str, Any], exit_code: int) -> None:
    """Write the terminal receipt, unless one is already terminal (the scheduler's timeout)."""
    execution_id = str(record.get("execution_id") or "")
    try:
        existing = _receipts().read_receipt(execution_id)
        if existing is not None and str(existing.get("status") or "") == "exited":
            return
    except Exception:
        pass
    record.update(status="exited", exit_code=int(exit_code), finished_at=_now())
    _write(record)


# ── Output pass-through ────────────────────────────────────────────────────────────────────


def _passthrough(path: Path, stream: Any) -> None:
    """Copy *path* to *stream* in chunks; a closed reader is ignored, never raised.

    The scheduler may be gone (its pipe write end closed) - that is the exact case this unit
    exists for, and it must not change the carrier's exit code or the receipt.
    """
    try:
        target = getattr(stream, "buffer", None)
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_COPY_CHUNK)
                if not chunk:
                    break
                if target is not None:
                    target.write(chunk)
                else:  # a replaced/closed text stream: decode the way the reader would
                    stream.write(chunk.decode("utf-8", "replace"))
        if target is not None:
            target.flush()
        else:
            stream.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass


def _detach_std_streams() -> None:
    """Point fds 1/2 at ``os.devnull`` after the best-effort copy.

    Without this, a buffered byte the copy could not flush is retried by the interpreter's
    shutdown flush; on a closed pipe CPython reports the ignored exception and the process can
    exit non-zero, which would present as a carrier failure the carrier never had.
    """
    for fd in (1, 2):
        try:
            null_fd = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(null_fd, fd)
            finally:
                os.close(null_fd)
        except OSError:  # pragma: no cover - devnull is always openable
            pass


def _exit_status(returncode: int) -> int:
    """The supervisor's own exit status for the carrier's ``returncode``.

    A process cannot exit with a negative status: a carrier killed by signal N exits as
    ``128 + N`` (the shell convention), while the receipt keeps the carrier's real
    ``-N`` returncode.
    """
    return returncode if returncode >= 0 else 128 + (-returncode)


# ── Running ─────────────────────────────────────────────────────────────────────────────────


def supervisor_argv(
    carrier_argv: Sequence[str], python_exe: Optional[str] = None
) -> list[str]:
    """The argv the wiring Popens: this file, by absolute path, wrapping *carrier_argv*."""
    return [
        python_exe or sys.executable or "python3",
        str(Path(__file__).resolve()),
        ARGV_SEPARATOR,
        *carrier_argv,
    ]


def _run_inherited(carrier_argv: Sequence[str]) -> int:
    """Run the carrier on the inherited stdio - the pre-U2 shape, for a fire with no receipt id."""
    try:
        proc = subprocess.Popen(list(carrier_argv))
    except OSError as exc:
        sys.stderr.write(f"carrier supervisor: cannot run carrier: {exc}\n")
        return 127
    return _exit_status(proc.wait())


def _run_supervised(
    carrier_argv: Sequence[str], execution_id: str, out_path: Path, err_path: Path
) -> int:
    record = _new_record(execution_id, out_path, err_path)
    # Pre-exec: name OURSELVES, so there is no window in which the attempt has no receipt at all.
    record.update(carrier_pid=os.getpid(), carrier_started_at=_fingerprint(os.getpid()))
    _write(record)

    out_handle = err_handle = None
    try:
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_handle = out_path.open("wb")
            err_handle = err_path.open("wb")
            carrier = subprocess.Popen(
                list(carrier_argv), stdout=out_handle, stderr=err_handle
            )
        except OSError as exc:
            message = f"carrier supervisor: cannot run carrier: {exc}\n"
            try:
                err_path.parent.mkdir(parents=True, exist_ok=True)
                with err_path.open("a", encoding="utf-8") as handle:
                    handle.write(message)
            except OSError:
                pass
            _finish(record, 127)
            _passthrough(err_path, sys.stderr)
            _detach_std_streams()
            return 127

        # Post-spawn: name the CARRIER - the pid and fingerprint the reconciler judges.
        record.update(
            carrier_pid=carrier.pid, carrier_started_at=_fingerprint(carrier.pid)
        )
        _write(record)
        exit_code = carrier.wait()
    finally:
        for handle in (out_handle, err_handle):
            if handle is not None:
                try:
                    handle.close()
                except OSError:  # pragma: no cover - close of a regular file
                    pass

    # The terminal record lands BEFORE the pass-through: a dead scheduler cannot lose it.
    _finish(record, exit_code)
    _passthrough(out_path, sys.stdout)
    _passthrough(err_path, sys.stderr)
    _detach_std_streams()
    return _exit_status(exit_code)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == ARGV_SEPARATOR:
        args = args[1:]
    if not args:
        sys.stderr.write("carrier supervisor: no carrier argv given\n")
        return 2

    execution_id = _env(EXECUTION_ENV)
    if not execution_id:
        # Not a supervised fire (off-schedule/manual caller): run the carrier unchanged.
        return _run_inherited(args)
    try:
        out_path, err_path = carrier_output_paths(execution_id)
    except Exception as exc:
        _diagnostic(execution_id, f"receipt paths unusable ({exc}); running without receipts")
        return _run_inherited(args)
    return _run_supervised(args, execution_id, out_path, err_path)


if __name__ == "__main__":
    sys.exit(main())
