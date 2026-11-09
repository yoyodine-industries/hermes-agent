"""Declared test-environment preflight: an absent dependency is ONE typed refusal.

Why this exists (measured 2026-11-08, card t_cf7c63c0): the Hermes-side suite
imports a few optional-capability modules *unguarded* in the tests that exercise
them. When the interpreter lacks one, pytest reports one collection error per
importing module -- 102 in tests/hermes_cli and 7 in tests/tools on a shared venv
that had pytest but not the runtime deps. Every one of those errors was a true
statement about the INTERPRETER being read as a statement about the TREE.

So the entry point asks the environment question first, and answers it with one
line: ``error_class=env/missing-dependency modules=...``. Code failures keep
collecting exactly as before; only an environment gap short-circuits.

The declaration lives here, in the repo, next to the pins it must agree with
(``pyproject.toml`` [dependency-groups] test). ``tests/test_env_preflight.py``
asserts the two cannot drift.
"""

from __future__ import annotations

import importlib
import os
import sys
from dataclasses import dataclass

#: The typed class a caller (runner, CI, a lane) can branch on. Never free text.
ERROR_CLASS = "env/missing-dependency"

#: Require extra modules for one run without editing this file. Used by the
#: refusal's own test, and by a lane proving a specific gap is an environment.
EXTRA_REQUIRE_ENV = "HERMES_TEST_PREFLIGHT_REQUIRE"

#: Exit code when the environment is incomplete. Distinct from pytest's own
#: codes (1 failures, 2 interrupted, 5 no tests) so a wrapper can tell them apart.
EXIT_ENV_INCOMPLETE = 3


@dataclass(frozen=True)
class Declared:
    """One module the suite needs, who provides it, and which tests need it."""

    module: str
    distribution: str
    why: str


#: The declared set. Each row names the module pytest would fail to import, the
#: distribution that provides it (so the fix is an install, not a hunt), and the
#: surface that needs it (so a future reader can tell whether it is still true).
DECLARED: tuple[Declared, ...] = (
    Declared("pytest", "pytest", "the runner itself"),
    Declared(
        "prompt_toolkit",
        "prompt_toolkit",
        "cli.py imports it at module level; tests/hermes_cli covers the REPL surface",
    ),
    Declared(
        "psutil",
        "psutil",
        "process/pid management in hermes_cli (kanban owner recovery, dispatcher)",
    ),
    Declared(
        "ruamel.yaml",
        "ruamel-yaml",
        "hermes_yaml.py imports it at module level; tests/conftest.py imports hermes_yaml",
    ),
    Declared(
        "keyring",
        "keyring",
        "credential-store tests in tests/hermes_cli",
    ),
    Declared(
        "mcp",
        "mcp",
        "the MCP client surface under tests/tools",
    ),
    Declared(
        "fal_client",
        "fal-client",
        "the image-generation backend under tests/tools",
    ),
)


def extra_required() -> tuple[Declared, ...]:
    """Modules additionally required for this run, from ``$HERMES_TEST_PREFLIGHT_REQUIRE``."""
    raw = os.environ.get(EXTRA_REQUIRE_ENV, "")
    names = [part.strip() for part in raw.split(",") if part.strip()]
    return tuple(Declared(name, name, "required by %s" % EXTRA_REQUIRE_ENV) for name in names)


def declared() -> tuple[Declared, ...]:
    """The full set checked this run: the repo declaration plus any run-local extra."""
    return DECLARED + extra_required()


def _importable(module: str) -> bool:
    try:
        importlib.import_module(module)
    except Exception:  # noqa: BLE001 - any import-time failure means "not usable here"
        return False
    return True


def missing() -> list[Declared]:
    """Every declared dependency this interpreter cannot import, in declared order."""
    return [row for row in declared() if not _importable(row.module)]


def interpreter() -> str:
    """The interpreter and its environment root, so a verdict is attributable."""
    prefix = sys.prefix
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    kind = "venv" if in_venv else "no-venv"
    return "interpreter=%s version=%s %s=%s" % (
        sys.executable,
        sys.version.split()[0],
        kind,
        prefix,
    )


def refusal(rows: list[Declared]) -> str:
    """One line naming the class and every absent module with its distribution."""
    modules = ",".join(row.module for row in rows)
    installs = ",".join(sorted({row.distribution for row in rows}))
    return (
        "error_class=%s modules=%s install=[%s] %s"
        % (ERROR_CLASS, modules, installs, interpreter())
    )


def check() -> str:
    """``""`` when the environment is complete, else the one-line typed refusal."""
    rows = missing()
    return "" if not rows else refusal(rows)


def require() -> None:
    """Exit ``EXIT_ENV_INCOMPLETE`` with the typed refusal when a dependency is absent."""
    line = check()
    if line:
        sys.stderr.write(line + "\n")
        raise SystemExit(EXIT_ENV_INCOMPLETE)


def main(argv: list[str] | None = None) -> int:
    """CLI: print the interpreter, then the verdict. Zero when complete."""
    print(interpreter())
    line = check()
    if line:
        print(line)
        return EXIT_ENV_INCOMPLETE
    print("preflight=ok modules=%d" % len(declared()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
