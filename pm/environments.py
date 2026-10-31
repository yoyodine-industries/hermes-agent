"""Dependency-environment layout: where a project's venv generations live, which one
is selected, and the interpreter inside any venv. Shared by PM and pre-import launchers.

Only stdlib and hermes_constants: environment selection must work before
any dependency from that environment has been imported.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from hermes_constants import get_default_hermes_root, project_venv_dir


def install_key(project_root: Path) -> str:
    canonical = str(Path(project_root).resolve())
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def dependency_home_root() -> Path:
    """Scope dependency state like a process launched in the active home."""
    from hermes_constants import get_default_hermes_root, get_hermes_home_override

    override = get_hermes_home_override()
    return get_default_hermes_root(home=override) if override else get_default_hermes_root()


def installs_root() -> Path:
    return dependency_home_root() / "installs"


def install_state_dir(project_root: Path) -> Path:
    return installs_root() / install_key(project_root)


def owning_home_root(project_root: Path) -> Path | None:
    """The data root that owns this checkout when the active root only borrows it, else ``None``.

    Dependency state is scoped per data root (``<root>/installs/<install_key>``), but a source
    checkout -- its launchers, product builds and install stamp -- exists once. A launch under
    another root (a test's temporary ``HERMES_HOME``, a per-task home, a CI service home) borrows
    it: the root that installed it already holds committed state for it, under the root the
    checkout sits in (``<root>/hermes-agent``) or else the platform default root. ``None`` when
    the active root's state is that state (the owner itself, or one of its profiles), and when no
    such root has state for this checkout -- a fresh install, or a custom root that owns its own
    tree -- so those keep today's behaviour (#123238).

    A borrowing launch's own sync leaves ``facts.json`` under the borrower too, so state alone
    cannot name the owner. The checkout's ``hermes`` launcher can: only the owner publishes it,
    and it execs the owner's store Python. With no live launcher to ask, the root the checkout
    sits in outranks the platform default.
    """
    from hermes_constants import _get_platform_default_hermes_home

    root = Path(project_root).resolve()
    key = install_key(root)
    candidates = [candidate for candidate in dict.fromkeys((root.parent, _get_platform_default_hermes_home()))
                  if (candidate / "installs" / key / "facts.json").is_file()]
    if not candidates:
        return None
    owner = _launcher_bound_root(root, candidates) or candidates[0]
    try:
        if (owner / "installs" / key).resolve() == install_state_dir(root).resolve():
            return None
    except OSError:
        pass
    return owner


def _launcher_bound_root(project_root: Path, candidates: list[Path]) -> Path | None:
    """The candidate whose ``tools/`` holds the live interpreter the checkout's launcher execs."""
    from hermes_cli._launchers import _launcher_python

    local = project_root / ".hermes" / "bin"
    for name in (("hermes.exe", "hermes.cmd") if os.name == "nt" else ("hermes",)):
        python = _launcher_python(local / name)
        if python is None or not python.is_file():
            continue
        for candidate in candidates:
            try:
                store = (candidate / "tools").resolve()
                if any(parent.resolve() == store for parent in python.parents):
                    return candidate
            except (OSError, RuntimeError, ValueError):
                continue
    return None


def install_state_permission_message(project_root: Path, exc: PermissionError) -> str | None:
    """Describe an access failure inside this install's dependency state."""
    if not exc.filename:
        return None
    denied = Path(exc.filename).resolve()
    if not denied.is_relative_to(install_state_dir(project_root).resolve()):
        return None
    return (f"install state is not writable by this user ({denied}); "
            "run as the install owner or grant write access")


def runtime_facts_path(project_root: Path) -> Path:
    return install_state_dir(project_root) / "facts.json"


# The files that decide the dependency set. `scripts/run-in-hermes-env` re-syncs
# when any of them differs in mtime from its stamp under activation_inputs_dir.
ACTIVATION_INPUTS = ("uv.lock", "pyproject.toml", "pm/lock.json")


def activation_inputs_dir(project_root: Path) -> Path:
    """Beside facts.json, so the runner finds it from ``$__HERMES_ACTIVATED``."""
    return install_state_dir(project_root) / "inputs"


def activation_input_mtimes(project_root: Path) -> dict[str, int]:
    """Snapshot before installing, so an input edited mid-install records its
    pre-install mtime and the next run re-activates."""
    root = Path(project_root)
    return {name: (root / name).stat().st_mtime_ns for name in ACTIVATION_INPUTS if (root / name).is_file()}


def record_activation_inputs(stamps: Path, mtimes: dict[str, int], project_root: Path, *, test_environment: bool) -> None:
    """Give each stamp the exact mtime of the input the install was verified against.

    Recorded on every successful install, including no-op syncs: a checkout that
    rewrites an input without changing it moves the mtime, and only this record
    brings the stamp back to equal. The runner compares for equality, not order,
    because switching branches can move an input's mtime in either direction.
    """
    import shutil

    shutil.rmtree(stamps, ignore_errors=True)
    # The sentinel is inherited by child shells; equal input mtimes in another
    # checkout must never make their test interpreter appear current here.
    stamps.mkdir(parents=True, exist_ok=True)
    (stamps / ".project-root").write_text(str(Path(project_root).resolve()), encoding="utf-8")
    if test_environment:
        (stamps / ".test-environment").touch()
    for name, mtime in mtimes.items():
        stamp = stamps / name
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
        os.utime(stamp, ns=(mtime, mtime))


def _payload_manifest(root: Path) -> dict | None:
    """The manifest of the sealed payload whose tree is the resolved *root*, or ``None``.

    Every install asks, so a ``manifest.json`` that some other tool left beside a
    checkout, or one with a mistyped field, must read as "not a payload", never as an
    error. Readers then take each field with ``.get`` and treat an absent one as unset.
    """
    try:
        manifest = json.loads((root.parent / "manifest.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("repo"), str):
        return None
    runtime = manifest.get("runtime", {})
    commands = runtime.get("commands", {}) if isinstance(runtime, dict) else None
    well_formed = (isinstance(commands, dict)
                   and isinstance(commands.get("hermes", ""), str)
                   and all(isinstance(manifest.get(key, ""), str) for key in ("venv", "store")))
    return manifest if well_formed and (root.parent / manifest["repo"]).resolve() == root else None


def _payload_path(root: Path, relative: str, what: str) -> Path:
    """*relative* resolved inside the payload that holds *root*; a manifest may not point outside it."""
    path = (root.parent / relative).resolve()
    if not path.is_relative_to(root.parent):
        raise RuntimeError(f"payload {what} escapes its root")
    return path


def payload_venv(project_root: Path) -> Path | None:
    """The environment a sealed payload ships beside its tree, or ``None``."""
    root = Path(project_root).resolve()
    venv = (_payload_manifest(root) or {}).get("venv")
    return _payload_path(root, venv, "environment") if venv else None


def _payload_hermes_command(root: Path) -> Path | None:
    command = (_payload_manifest(root) or {}).get("runtime", {}).get("commands", {}).get("hermes")
    return _payload_path(root, command, "launcher") if command else None


def payload_command_dir(project_root: Path) -> Path | None:
    """The directory holding a sealed payload's own launchers (``<payload>/bin``), or ``None``."""
    command = _payload_hermes_command(Path(project_root).resolve())
    return command.parent if command else None


def cli_command_name(project_root: Path) -> str:
    """The name a person types to run this install's CLI.

    A desktop channel build publishes its launcher under a qualified name
    (``hermes-canary``) so it never shadows another install's ``hermes``.
    """
    command = _payload_hermes_command(Path(project_root).resolve())
    return command.name.removesuffix(".exe") if command else "hermes"


def base_venv(project_root: Path) -> Path:
    return payload_venv(project_root) or project_venv_dir(Path(project_root).resolve()) or Path(project_root).resolve() / "venv"


def store_root(project_root: Path, *, honor_runtime_override: bool = True) -> Path:
    """Resolve a payload-relative or stamped store before PM imports.

    ``HERMES_RUNTIME_DIR`` exists so a running process can point PM at a
    non-default runtime location. Publication paths must pass
    ``honor_runtime_override=False``: a persisted artifact (an installed
    launcher) has to bind the store of the tree it serves, never a runtime
    directory inherited through the environment.

    A tree that records no store of its own resolves the OWNING root's
    (``_unstamped_store``), never the caller's ``HERMES_HOME``-derived
    ``tools``: a boot under another home (a rehearsal hold, a per-task home)
    would otherwise mint the checkout's shared launchers against a store that
    home's later cleanup deletes (#131745's exit-127 class, one step earlier
    than the ``HERMES_RUNTIME_DIR`` override that fix already pins). A tree
    nothing owns is a fresh install -- there the caller's home IS the store,
    so it is unchanged.
    """
    if honor_runtime_override:
        override = os.environ.get("HERMES_RUNTIME_DIR")
        if override:
            return Path(override).resolve()
    root = Path(project_root).resolve()
    store = (_payload_manifest(root) or {}).get("store")
    if store:
        return _payload_path(root, store, "store")
    from pm.paths import install_stamp_path

    for directory in (root, *root.parents):
        stamp = install_stamp_path(directory)
        if stamp.is_file():
            try:
                data = json.loads(stamp.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                return _unstamped_store(root)
            value = data.get("runtimeDir") if isinstance(data, dict) else None
            return Path(value).resolve() if value else _unstamped_store(root)
    return _unstamped_store(root)


def _unstamped_store(project_root: Path) -> Path:
    """The store of a tree that records none of its own: the owning root's.

    Two stamps reach here: one that names no ``runtimeDir`` (source stamps never do) and none
    at all (a checkout whose untracked ``install-stamp.json`` a fresh clone or a git worktree
    never carries).

    The data root's ``tools/`` is the default, but a borrowing root's would be a new store per
    temporary home -- a full tool download on its first launch, and an interpreter that dies
    with the home. The checkout's own launchers exec the owner's interpreter, so a borrowing
    launch resolves the same one (#123238).
    """
    return (owning_home_root(project_root) or get_default_hermes_root()) / "tools"


def flush_before_selecting() -> None:
    """Make a finished generation tree durable before a selection record names it.

    The selection record (the install's facts.json, a side environment's
    active.json) is written last, atomically and fsynced, so it doubles as the
    generation's completion marker -- but only if every file it vouches for
    reached the disk first. Otherwise a power loss can persist the record while
    the tree's data is still in the page cache, selecting a half-written venv.
    One filesystem-wide sync instead of an fsync per file: a venv holds tens of
    thousands of files, and per-file flushes cost minutes on slow disks.

    Windows has no whole-filesystem flush reachable from Python (os.sync does
    not exist there), and FlushFileBuffers per file is the slow path rejected
    above. There the record's atomic write is the only guarantee: it is never
    torn, but NTFS journals metadata, not file contents, so power loss right
    after a publish can still leave the selected tree with incomplete files.
    """
    sync = getattr(os, "sync", None)
    if sync is not None:
        sync()


def selected_venv(project_root: Path) -> Path:
    """Use the committed environment, or the original install before first sync.

    A broken committed selection is an error, not permission to load an older
    dependency set silently. Reading this function never creates user state.
    The record itself is the completion marker: it is only written after
    ``flush_before_selecting``, so the ``pyvenv.cfg`` probe below is a sanity
    check against a vanished tree, not the durability guarantee.
    """
    return _recorded_venv(project_root) or base_venv(project_root)


def committed_venv(project_root: Path) -> Path | None:
    """The environment PM committed for this install (or a sealed payload's own), else ``None``.

    Unlike ``selected_venv`` this never answers with the in-tree ``venv``/``.venv``: that tree
    predates PM and is built for whichever interpreter created it, so loading it from PM's store
    Python mixes ABIs (compiled modules vanish) and PM deletes it once a generation is committed.
    """
    return _recorded_venv(project_root) or payload_venv(project_root)


def _recorded_venv(project_root: Path) -> Path | None:
    path = runtime_facts_path(project_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot read dependency environment: {path}") from exc
    try:
        fact = data.get("packages", {}).get("venv", {})
        value = fact.get("environment")
    except AttributeError as exc:
        raise RuntimeError(f"invalid dependency environment record: {path}") from exc
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeError("invalid dependency environment path")
    environment = Path(value).resolve()
    generations = install_state_dir(project_root) / "environments"
    if not environment.is_relative_to(generations.resolve()) or not (environment / "pyvenv.cfg").is_file():
        raise RuntimeError(f"dependency environment is missing or outside this install: {environment}")
    return environment


def venv_bin_dir(venv: Path, *, windows: bool | None = None) -> Path:
    """``Scripts`` on Windows, ``bin`` elsewhere. Returned unconditionally — callers
    differ on whether a missing venv is an error. *windows* lets a POSIX process
    reason about a Windows layout (update hand-off, launcher repair)."""
    if windows is None:
        windows = os.name == "nt"
    return Path(venv) / ("Scripts" if windows else "bin")


def venv_python(venv: Path, *, windows: bool | None = None) -> Path:
    """The interpreter inside *venv* (may not exist)."""
    bin_dir = venv_bin_dir(venv, windows=windows)
    return bin_dir / ("python.exe" if bin_dir.name == "Scripts" else "python")


def project_python(project_root: Path) -> Path:
    """The interpreter of the committed dependency environment for *project_root*."""
    return venv_python(selected_venv(project_root))


def _payload_store_python(root: Path) -> Path | None:
    """A sealed payload's own interpreter when a venv must not be entered through its own."""
    manifest = _payload_manifest(root)
    if manifest is None or str(manifest.get("target", "")).endswith("-bionic"):
        return None  # Termux payloads launch through their venv interpreter by design.
    relative = manifest.get("runtime", {}).get("storePython")
    if not isinstance(relative, str) or not relative:
        return None
    return _payload_path(root, relative, "interpreter")


def venv_command(project_root: Path, venv: Path, options: tuple[str, ...] | list[str] = ()) -> list[str]:
    """The argv prefix that runs Python inside *venv*; append a script, ``-c`` or ``-m``.

    A sealed payload never enters a venv through ``Scripts\\python.exe``. That redirector sits
    outside the package and starts PM's writable copy of the Python (``pm._uv._toolchain``),
    and a process on that copy has no package identity, so Windows refuses it the package's
    git, rg and PM worker (WinError 5). Instead the payload's own interpreter attaches the
    venv's site-packages (``pm/_venv_entry.py``): the same contract as the payload's
    launchers and its PM worker. Elsewhere the venv's interpreter runs as is. *options* are
    interpreter flags (``-I``, ``-u``, ``-X …``).
    """
    root = Path(project_root).resolve()
    store_python = _payload_store_python(root)
    if store_python is None:
        return [str(venv_python(venv)), *options]
    entry = Path(__file__).resolve().with_name("_venv_entry.py")
    flags = [*options, *([] if "-S" in options else ["-S"])]
    return [str(store_python), *flags, str(entry), str(site_packages(Path(venv)))]


def venv_python_version(venv: Path) -> tuple[int, int] | None:
    """The interpreter version a POSIX venv actually holds, or ``None``.

    ``site_packages`` must not date the tree from the CALLER's ``sys.version_info``:
    an update can rebuild the dependency environment with a different Python than
    the launcher that later imports it. Observed on an app-driven upgrade -- PM
    built the environment with CPython 3.14 while the PATH shim ran 3.11, so the
    shim composed ``lib/python3.11/site-packages`` inside a 3.14 venv, found no
    tree, and failed *after* a successful update.
    """
    try:
        for line in (venv / "pyvenv.cfg").read_text(encoding="utf-8-sig").splitlines():
            key, _, value = line.partition("=")
            if key.strip() != "version":
                continue
            major, _, rest = value.strip().partition(".")
            minor, _, _ = rest.partition(".")
            if major.isdigit() and minor.isdigit():
                return int(major), int(minor)
    except OSError:
        pass
    try:
        candidates = sorted((venv / "lib").glob("python3*"))
    except OSError:
        return None
    for candidate in candidates:
        major, _, rest = candidate.name.removeprefix("python").partition(".")
        minor, _, _ = rest.partition(".")
        if major.isdigit() and minor.isdigit():
            return int(major), int(minor)
    return None


def site_packages(venv: Path) -> Path:
    import sys

    if os.name == "nt":
        return venv / "Lib/site-packages"
    version = venv_python_version(venv) or (sys.version_info.major, sys.version_info.minor)
    return venv / f"lib/python{version[0]}.{version[1]}/site-packages"


# A generation owns DEPENDENCIES; the checkout owns CODE. The generation's venv installs the
# core project as an *editable* install bound to its build-time `workspace/` snapshot, so
# `venv/bin/hermes`, `venv/bin/python -m hermes_cli…` and anything else entering that
# interpreter would otherwise execute the snapshot's code — indefinitely, because
# `expected_stamp()` is keyed on dependency inputs only and a code-only update mints no new
# generation. This `.pth` is the same contract `activate_dependencies()` already applies to
# booted processes (checkout first on `sys.path`), applied to *every* entry into the venv:
# `PathFinder` precedes the editable finder in `sys.meta_path`, so a `sys.path` entry wins.
SOURCE_PIN_FILENAME = "__hermes_source_pin.pth"
SOURCE_STAMP_FILENAME = "source.json"


def source_stamp_path(generation: Path) -> Path:
    """Where *generation* records its provenance: the GENERATION root, never inside ``workspace/``.

    The snapshot must stay a byte-faithful build input — it is also ``resolved_lock.parent`` and
    the replay source for a later ``repair``.
    """
    return Path(generation) / SOURCE_STAMP_FILENAME


def read_source_stamp(generation: Path) -> dict | None:
    """The provenance *generation* records, or ``None`` when it carries no readable stamp."""
    try:
        data = json.loads(source_stamp_path(generation).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != 1:
        return None
    root = data.get("root")
    return data if isinstance(root, str) and root else None


def source_pin_line(project_root: Path) -> str:
    """The one ``import``-prefixed line the pin ``.pth`` executes at site init.

    Guarded by ``os.path.isdir`` — an unmounted or moved checkout must not break every
    interpreter in the generation — and order-safe: a root already leading ``sys.path`` is
    never inserted twice.
    """
    root = str(project_root)
    return (f"import os, sys; _hermes_src = {root!r}; "
            "sys.path.insert(0, _hermes_src) if os.path.isdir(_hermes_src) "
            "and sys.path[:1] != [_hermes_src] else None")


def _source_revision(project_root: Path) -> dict | None:
    """Best-effort git provenance (``{"head": sha, "dirty": bool}``) — never fails an install."""
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(project_root), *args],
                              capture_output=True, text=True, timeout=10)

    try:
        head = git("rev-parse", "HEAD")
        if head.returncode != 0 or not head.stdout.strip():
            return None
        dirty = git("diff-index", "--quiet", "HEAD", "--")
        return {"head": head.stdout.strip(), "dirty": dirty.returncode == 1}
    except (OSError, subprocess.SubprocessError):
        return None


def _atomic_write(path: Path, text: str) -> None:
    """Write *text* to *path* via a sibling temp file, so no reader ever sees a partial pin."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def write_source_pin(generation: Path, project_root: Path) -> Path:
    """Record *project_root* as *generation*'s code source and pin its venv to it.

    Raises when the pin cannot be written: a generation that cannot route its own code to the
    checkout that built it must fail loudly at build time, never install and quietly resolve a
    stale snapshot instead.
    """
    generation = Path(generation)
    venv = generation / "venv"
    if not (venv / "pyvenv.cfg").is_file():
        raise FileNotFoundError(f"no environment to pin at {venv}")
    root = Path(project_root).resolve()
    stamp = {"schema": 1, "root": str(root), "revision": _source_revision(root),
             "recorded_at": datetime.now(UTC).isoformat(timespec="seconds")}
    _atomic_write(source_stamp_path(generation), json.dumps(stamp, indent=2, sort_keys=True) + "\n")
    pin = site_packages(venv) / SOURCE_PIN_FILENAME
    _atomic_write(pin, f"# Hermes source pin — {root} owns this generation's code.\n"
                       f"{source_pin_line(root)}\n")
    return pin


def source_pin_problem(generation: Path) -> str | None:
    """What is wrong with *generation*'s source pin, or ``None`` when it is correct.

    File reads only — no subprocess, nothing imported from the generation: doctor has to be
    able to name a stale generation *before* anything is repaired or booted.
    """
    generation = Path(generation)
    venv = generation / "venv"
    if not (venv / "pyvenv.cfg").is_file():
        return None  # not a managed generation
    recorded = read_source_stamp(generation)
    if not recorded:
        return f"carries no {SOURCE_STAMP_FILENAME} provenance stamp"
    root = str(recorded["root"])
    revision = recorded.get("revision")
    revision = revision if isinstance(revision, dict) else {}
    head = str(revision.get("head") or "")[:10] or "unknown"
    dirty = " (dirty)" if revision.get("dirty") else ""
    if not Path(root).is_dir():
        return f"records code source {root} (revision {head}{dirty}), which no longer exists"
    try:
        pin_lines = (site_packages(venv) / SOURCE_PIN_FILENAME).read_text(encoding="utf-8").splitlines()
    except OSError:
        return (f"records code source {root} (revision {head}{dirty}) "
                f"but carries no {SOURCE_PIN_FILENAME}")
    if source_pin_line(Path(root)) not in pin_lines:
        return (f"records code source {root} (revision {head}{dirty}) but its "
                f"{SOURCE_PIN_FILENAME} names a different root")
    return None


def ensure_source_pin(project_root: Path) -> Path | None:
    """Pin the generation PM has committed for *project_root*; ``None`` when none is managed.

    The no-rebuild path: a sync over an unchanged dependency graph mints no generation, so an
    install that predates source pinning is healed here instead of never.
    """
    venv = committed_venv(project_root)
    if venv is None or not venv.is_dir() or not (venv.parent / ".lease-managed").is_file():
        return None
    root = str(Path(project_root).resolve())
    recorded = read_source_stamp(venv.parent)
    if recorded is not None and recorded["root"] == root and source_pin_problem(venv.parent) is None:
        return site_packages(venv) / SOURCE_PIN_FILENAME
    return write_source_pin(venv.parent, project_root)


def running_from_selected_environment(project_root: Path) -> bool:
    """Does this process run on the environment PM selected for the install (base venv or committed
    generation)?

    A lazy sync from any other interpreter — a build_environment test venv, a developer's own venv,
    a Nix store Python — must not commit the install's selection: activation is a boot decision, so
    this process keeps running unchanged while every process booted afterwards swaps onto a
    generation that lacks whatever the foreign interpreter carried.

    activate_dependencies puts the selection's site-packages on sys.path without changing
    sys.prefix, so sys.path is the signal (the same one ensure_import reads after a sync).
    """
    import sys

    try:
        selected = site_packages(selected_venv(project_root)).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    return any(Path(entry).resolve() == selected for entry in sys.path if entry)


def _require_own_dependencies(project_root: Path) -> None:
    """With nothing committed, an interpreter keeps the packages it booted with.

    PM's store Python boots with none, so for it there is nothing to keep: refuse instead of
    running on whatever PYTHONPATH it inherited (historically the pre-PM in-tree venv).
    """
    import sys

    if sys.prefix != sys.base_prefix:
        return  # a venv interpreter (developer .venv, test env) carries its own packages
    if Path(sys.base_prefix).resolve().is_relative_to(store_root(project_root).resolve()):
        raise RuntimeError("no dependency environment is committed for this install")


def activate_dependencies(project_root: Path) -> None:
    """Select the committed tree at process boot, before third-party imports.

    A process with no extension selection keeps its original launch contract.
    Already-running processes are never switched after a dependency install.
    """
    import sys

    state = install_state_dir(project_root)
    if state.is_dir():
        from hermes_cli.runtime_state import runtime_lock, recover_publication, lease_generation
        # The lock's holder may be another profile's backend running a full dependency rebuild;
        # this process only reads the committed selection, so it proceeds without waiting rather
        # than leaving the backend unbound (see runtime_lock).
        with runtime_lock(project_root) as held:
            if held:
                recover_publication(project_root)
            environment = committed_venv(project_root)
            if environment is None:
                return _require_own_dependencies(project_root)
            release = lease_generation(environment)
            # Without the lock, an installer may commit a new generation between the
            # read and the lease, leaving the leased one unselected and collectable.
            while not held and (current := committed_venv(project_root)) not in (None, environment):
                release()
                environment, release = current, lease_generation(current)
            selected = site_packages(environment)
            if not selected.is_dir() and not runtime_facts_path(project_root).is_file():
                return
    else:
        # Sealed payloads still select once, before imports.
        # Never consult VIRTUAL_ENV: it can describe the invoking shell's Python.
        environment = payload_venv(project_root)
        if environment is None:
            return _require_own_dependencies(project_root)
        selected = site_packages(environment)
        if not selected.is_dir():
            return  # External/Nix interpreter owns its original sys.path.
    if not selected.is_dir():
        raise RuntimeError(f"dependency environment has no site-packages: {selected}")
    import site

    sys.path[:] = [entry for entry in sys.path
                   if Path(entry).name not in ("site-packages", "dist-packages")
                   and Path(entry).resolve() != project_root.resolve()]
    # uv editable members are activated by .pth files, not by sys.path alone.
    site.addsitedir(str(selected))
    sys.path[:] = [str(project_root.resolve()), str(selected),
                   *[entry for entry in sys.path if Path(entry).resolve() != selected.resolve()]]
    os.environ["PYTHONPATH"] = os.pathsep.join([str(project_root.resolve()), str(selected)])
    os.environ.pop("VIRTUAL_ENV", None)
    # The venv's own `hermes`/`hermes-acp` console scripts are editable installs bound to
    # the build-time source snapshot, not this checkout (#124627): a child that resolves
    # `hermes` off PATH must hit the checkout's own launcher first, never the venv's copy.
    # A sealed payload's launchers live in <payload>/bin instead.
    directories = [payload_command_dir(project_root) or project_root.resolve() / ".hermes" / "bin"]
    # A sealed payload's Windows venv redirectors never go on PATH: a shipped venv's still
    # names the build machine, and a generation built since names PM's writable Python copy,
    # which cannot start the package's executables (see venv_command).
    if not (os.name == "nt" and _payload_manifest(project_root.resolve()) is not None):
        directories.append(venv_bin_dir(environment))
    prefix = [str(path) for path in directories if path.is_dir()]
    if prefix:
        os.environ["PATH"] = os.pathsep.join([*prefix, os.environ.get("PATH", "")])


def activation_environment(project_root: Path) -> dict[str, str]:
    """Read the installed PM environment; do not provision or switch imports."""
    from pm.install import env_for
    from pm.registry import all_packages

    env = env_for(*all_packages())
    environment = committed_venv(project_root)
    env.pop("PYTHONHOME", None)
    env.pop("VIRTUAL_ENV", None)
    # Nothing committed: the child's own hermes_bootstrap decides (a bare store Python refuses),
    # rather than inheriting the pre-PM in-tree venv from here.
    env["PYTHONPATH"] = os.pathsep.join([str(project_root.resolve()),
                                         *([str(site_packages(environment))] if environment else [])])
    # The child-process sentinel. Its VALUE is the installed-state file this
    # environment was composed against, so a consumer learns that it inherited
    # an activated shell and which checkout/profile that shell came from. Its
    # directory also holds activation_inputs_dir, the input-mtime stamps
    # `scripts/_activation.sh` compares against to decide staleness.
    env["__HERMES_ACTIVATED"] = str(runtime_facts_path(project_root))
    # The suite's interpreter (pm.testenv): an isolated side environment, so it
    # never appears on PYTHONPATH/PATH above. scripts/run_tests.sh reads it.
    from pm.testenv import testenv_python

    test_python = testenv_python(project_root)
    if test_python is not None:
        env["__HERMES_TEST_PYTHON"] = str(test_python)
    return env


def _fish_quote(value: str) -> str:
    """Inside fish single quotes only backslash and the quote itself are special."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


# fish refuses to assign these, and each is either inherited unchanged from the
# invoking shell (PWD, SHLVL, _) or fish's own state, so skipping loses nothing.
_FISH_READ_ONLY = frozenset({
    "PWD", "SHLVL", "_", "status", "version", "hostname", "fish_pid", "history",
    "pipestatus", "status_generation", "umask", "FISH_VERSION",
})

# dialect -> (export statement, names the shell cannot assign). fish splits
# values of variables named *PATH on colons when they are set from a single
# word, so PATH stays a list.
_SHELL_DIALECTS = {
    "sh": (lambda name, value: f"export {name}={shlex.quote(value)}", frozenset()),
    "fish": (lambda name, value: f"set -gx {name} {_fish_quote(value)}", _FISH_READ_ONLY),
}
_SHELL_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def shell_exports(env: dict[str, str], dialect: str) -> str:
    """The composed environment as a script a shell of ``dialect`` can evaluate.

    Windows carries names like ``ProgramFiles(ARM)`` that no shell can assign;
    they pass through untouched instead of failing the whole script.
    """
    statement, read_only = _SHELL_DIALECTS[dialect]
    return "\n".join(statement(name, str(value)) for name, value in env.items()
                     if _SHELL_IDENTIFIER.fullmatch(name) and name not in read_only)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Print the composed activation environment.")
    parser.add_argument("--format", choices=["json", *_SHELL_DIALECTS], default="json")
    options = parser.parse_args(argv)
    env = activation_environment(Path(__file__).resolve().parents[1])
    print(json.dumps(env) if options.format == "json" else shell_exports(env, options.format))


if __name__ == "__main__":
    main()
