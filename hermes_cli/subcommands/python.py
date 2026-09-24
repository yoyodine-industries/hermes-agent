"""``hermes python`` — the fleet's ONE resolver for "which interpreter runs this".

Every fleet script used to pick its own interpreter at call time: a hardcoded venv path,
an ambient-PATH ``python3``, a candidate list that landed on ``/usr/bin/python3``. The
consequence is measured, not theoretical — one ``cron-inflight-probe.py`` run under four
interpreters, one of which was SILENTLY BLIND on a missing module and so made a
fail-closed guardrail fail open.

``hermes python --role <role>`` replaces "whoever's PATH wins" with a DECLARATION. The
role names the job, the host's config names the interpreter, and the resolver ASSERTS the
declaration — version floor plus every declared module — before it hands back a path in
one subprocess. A role that cannot satisfy its declaration exits non-zero naming the
interpreter and the miss; it never falls back, and it never reports a default.

Where the mapping lives (``config.yaml``; the shipped defaults are in
``hermes_cli/config_defaults.py``)::

    python:
      roles:
        runtime:            # callers that import the Hermes tree
          interpreter: ""               # default: the interpreter running this CLI
          min_version: "3.11"
          modules: [yaml, psutil]
        ops:                # fleet scripts with no Hermes imports
          interpreter: /opt/hermes_prod/shared-venv/bin/python3
          min_version: "3.11"
          modules: [yaml, psutil]

Shell use — the resolver, never a literal path::

    PY="$(hermes python --role runtime)"                 # the interpreter
    eval "$(hermes python --role runtime --export)"       # HERMES_PYTHON in the environment
    PY="$(hermes python --role ops --need psutil)"        # assert the call site's own imports

``--role`` is REQUIRED on purpose: a caller that does not say which job it is doing can
be handed the wrong venv and never notice, which is the disease. A role that is not
configured exits 3 rather than resolving to something plausible.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

EXIT_OK = 0
EXIT_DECLARATION_FAILED = 2
EXIT_UNKNOWN_ROLE = 3
PROBE_TIMEOUT_SECONDS = 20

# One subprocess answers the whole declaration — version floor AND every module — so the
# resolver can never report half an answer. A module that exists but raises on import is a
# MISS: the failure this replaces was an import-time one, not a missing-path one.
_PROBE = r"""
import importlib
import json
import sys

missing = {}
for name in %(modules)r:
    try:
        importlib.import_module(name)
    except BaseException as exc:
        missing[name] = "%%s: %%s" %% (type(exc).__name__, exc)
print(json.dumps({
    "version": "%%d.%%d.%%d" %% sys.version_info[:3],
    "executable": sys.executable,
    "missing": missing,
}))
"""


class DeclarationError(RuntimeError):
    """A role exists but cannot satisfy its declaration. Never a fallback."""


class UnknownRoleError(RuntimeError):
    """The named role is not configured — refuse, do not guess."""


def _default_interpreter(role: str) -> str:
    """The interpreter for ``role`` when the config's own value is empty.

    ``runtime`` defaults to the interpreter running this CLI: by construction it satisfies
    the Hermes tree's own imports, and it survives a home migration, a uv upgrade and a
    python version bump without an edit. Anything else has no safe dynamic answer, so the
    config must name it (an empty config value is reported, never guessed).
    """
    if role == "runtime":
        return sys.executable
    return ""


def _configured_roles(config: Optional[Mapping[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
    if config is None:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
    section = config.get("python") if isinstance(config, Mapping) else None
    roles = section.get("roles") if isinstance(section, Mapping) else None
    return dict(roles) if isinstance(roles, Mapping) else {}


def _merged_specs(config: Optional[Mapping[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
    """Shipped defaults merged with the host's config, per role and per key (config wins)."""
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    specs: Dict[str, Dict[str, Any]] = {}
    for source in (DEFAULT_CONFIG.get("python", {}).get("roles", {}), _configured_roles(config)):
        for role, spec in (source or {}).items():
            merged = dict(specs.get(role, {}))
            merged.update(spec or {})
            specs[role] = merged
    return specs


def known_roles(config: Optional[Mapping[str, Any]] = None) -> Tuple[str, ...]:
    return tuple(sorted(_merged_specs(config)))


def role_terms(role: str, config: Optional[Mapping[str, Any]] = None) -> Tuple[str, str, Tuple[str, ...]]:
    """``(interpreter, min_version, modules)`` for ``role``, or ``UnknownRoleError``."""
    specs = _merged_specs(config)
    if role not in specs:
        raise UnknownRoleError(
            "unknown role '%s' (known roles: %s)" % (role, ", ".join(sorted(specs)) or "none")
        )
    spec = specs[role]
    interpreter = spec.get("interpreter") or _default_interpreter(role)
    modules = tuple(spec.get("modules") or ())
    return interpreter, str(spec.get("min_version") or ""), modules


def _version_tuple(text: str) -> Tuple[int, ...]:
    parts = []
    for chunk in str(text).split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        if digits:
            parts.append(int(digits))
    return tuple(parts)


def probe(interpreter: str, modules: Sequence[str]) -> Dict[str, Any]:
    """Run the one probe; raise ``DeclarationError`` if the interpreter cannot be run."""
    try:
        completed = subprocess.run(
            [interpreter, "-c", _PROBE % {"modules": list(modules)}],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS,
        )
    except OSError as exc:
        raise DeclarationError("interpreter %s could not be executed: %s" % (interpreter, exc))
    except subprocess.TimeoutExpired:
        raise DeclarationError("interpreter %s did not answer within %ds"
                               % (interpreter, PROBE_TIMEOUT_SECONDS))
    if completed.returncode != 0:
        raise DeclarationError("interpreter %s failed its probe (rc=%d): %s"
                               % (interpreter, completed.returncode,
                                  (completed.stderr or "").strip().splitlines()[-1:] or "no output"))
    try:
        return json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise DeclarationError("interpreter %s returned an unreadable probe answer: %r"
                               % (interpreter, completed.stdout[:200]))


def resolve(role: str, *, need: Iterable[str] = (), config: Optional[Mapping[str, Any]] = None
            ) -> Dict[str, Any]:
    """Resolve ``role`` to an absolute interpreter, having ASSERTED its declaration.

    Raises ``UnknownRoleError`` for a role that is not configured, ``DeclarationError``
    when the interpreter is missing, not absolute, below the version floor, or missing a
    declared module. Callers of this function must not be given a path otherwise.
    """
    interpreter, min_version, modules = role_terms(role, config)
    if not interpreter:
        raise DeclarationError(
            "role '%s' has no interpreter configured and no safe default — set "
            "python.roles.%s.interpreter in config.yaml" % (role, role))
    path = Path(interpreter)
    if not path.is_absolute():
        raise DeclarationError(
            "role '%s' names a relative interpreter (%s); the resolver hands back an "
            "absolute path or nothing" % (role, interpreter))
    if not (path.is_file() and path.stat().st_mode & 0o111):
        raise DeclarationError("role '%s': interpreter %s is not an executable file"
                               % (role, path))

    declared = tuple(dict.fromkeys(list(modules) + [m for m in need if m]))
    answer = probe(str(path), declared)

    floor = _version_tuple(min_version)
    actual = _version_tuple(answer.get("version", "0"))
    if floor and actual[:len(floor)] < floor:
        raise DeclarationError("role '%s': interpreter %s is Python %s, below the declared "
                               "floor %s" % (role, path, answer.get("version"), min_version))
    missing = answer.get("missing") or {}
    if missing:
        detail = "; ".join("%s (%s)" % (m, missing[m]) for m in sorted(missing))
        raise DeclarationError("role '%s': interpreter %s (Python %s) is missing %s"
                               % (role, path, answer.get("version"), detail))
    return {
        "role": role,
        "interpreter": str(path),
        "version": answer.get("version"),
        "min_version": min_version or None,
        "modules": list(declared),
    }


def cmd_python(args: argparse.Namespace) -> int:
    need = [m.strip() for m in str(getattr(args, "need", "") or "").split(",") if m.strip()]
    try:
        resolved = resolve(args.role, need=need)
    except UnknownRoleError as exc:
        print("hermes python: %s" % exc, file=sys.stderr)
        return EXIT_UNKNOWN_ROLE
    except DeclarationError as exc:
        print("hermes python: %s" % exc, file=sys.stderr)
        return EXIT_DECLARATION_FAILED
    if getattr(args, "json", False):
        print(json.dumps(resolved, sort_keys=True))
    elif getattr(args, "export", False):
        print("export HERMES_PYTHON='%s'" % resolved["interpreter"])
    else:
        print(resolved["interpreter"])
    return EXIT_OK


DESCRIPTION = (
    "Resolve the absolute interpreter for a NAMED role (runtime: callers that import the "
    "Hermes tree; ops: fleet scripts with no Hermes imports), from this host's config, and "
    "assert the role's version floor and declared modules before handing back the path. "
    "A role that cannot satisfy its declaration exits non-zero naming the interpreter and "
    "the miss — it never falls back."
)


def build_python_parser(subparsers, *, handler=None) -> None:
    """Attach ``hermes python`` to ``subparsers``."""
    parser = subparsers.add_parser(
        "python",
        help="Resolve a role to the absolute interpreter for it (asserted, never guessed)",
        description=DESCRIPTION,
    )
    parser.add_argument(
        "--role", required=True,
        help="Which job is asking: 'runtime' (imports the Hermes tree) or 'ops' "
             "(fleet scripts with no Hermes imports). Roles come from python.roles in config.yaml.")
    parser.add_argument(
        "--need", default="",
        help="Extra modules this call site declares, comma-separated; the resolver asserts "
             "them before handing back the path.")
    parser.add_argument("--export", action="store_true",
                        help="Print 'export HERMES_PYTHON=<path>' for eval in a shell")
    parser.add_argument("--json", action="store_true",
                        help="Print the resolution as JSON (role, interpreter, version, modules)")
    parser.set_defaults(func=handler or cmd_python)
