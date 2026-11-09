#!/usr/bin/env python3
"""Build the DECLARED test environment, reproducibly, from the repo's own pins.

Why (card t_cf7c63c0): no single interpreter on the host could run the suite --
the live venv had prompt_toolkit but no pytest, the shared venv had pytest but
not the runtime deps -- so every lane assembled its own PYTHONPATH. An
interpreter assembled differently on each run is an UNRECORDED ENVIRONMENT: the
same tree is green for one lane and red for another and neither reading is
reproducible by the other.

This script is the declared build. It reads the pins from ``pyproject.toml``
(the single source of truth: the ``test``/``dev`` groups plus the runtime
dependency the suite imports) and installs exactly those into a fresh venv.
``python -m pm.build_env --source . --out .venv --group dev --group test``
remains the canonical full build; this is the minimal one the preflight checks,
and it exists so the declaration cannot live in a shell history.

Usage:
    scripts/test_env_bootstrap.py [--out PATH] [--python EXE]

After it finishes, point the runner at the interpreter it printed:
    HERMES_PYTHON=<out>/bin/python scripts/run_tests.sh tests/
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Modules the suite imports UNGUARDED, mapped to the distribution that
#: provides them. Kept identical to tests/preflight.py's declaration; the
#: preflight test asserts each distribution is pinned in pyproject.
REQUIRED = (
    "pytest",
    "prompt_toolkit",
    "psutil",
    "keyring",
    "mcp",
    "fal-client",
    "ruamel.yaml",
)

#: Optional extras the suite needs beyond the runtime pins. Chosen, not "all":
#: installing every extra pulls native voice/wake backends no test imports.
EXTRAS = ("keychain", "fal")


def _requirement_name(spec: str) -> str:
    """``"mcp==2.3.0"`` -> ``"mcp"``; drops markers and extras for comparison."""
    head = spec.split(";", 1)[0].strip()
    for sep in ("==", ">=", "<=", "~=", "!=", ">", "<", " @ "):
        head = head.split(sep, 1)[0]
    return head.strip().split("[", 1)[0].lower()


def declared_pins() -> list[str]:
    """The minimal declared install set: every module the suite imports unguarded.

    Deliberately NOT ``-e .``: installing the whole runtime drags native extras
    whose pins conflict in a fresh venv (measured 2026-11-09: ``Pillow==13.0.0``
    against ``browser-harness``'s ``pillow==12.3.0`` -> ResolutionImpossible),
    which is a packaging question for ``pm.build_env``, not something a lane
    should paper over. This build exists so the unguarded-import set is one
    reproducible command; the full environment stays the canonical
    ``python -m pm.build_env --source . --out .venv --group dev --group test``.
    """
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    groups = data.get("dependency-groups", {})

    pool: list[str] = []
    for name in ("test", "dev"):
        pool.extend(groups.get(name, []))
    pool.extend(data["project"].get("dependencies", []))
    for specs in data["project"].get("optional-dependencies", {}).values():
        pool.extend(specs)

    wanted = {name.lower() for name in REQUIRED}
    chosen: dict[str, str] = {}
    for spec in pool:
        name = _requirement_name(spec)
        if name in wanted and name not in chosen:
            chosen[name] = spec
    absent = sorted(wanted - set(chosen))
    if absent:
        raise SystemExit(
            "error_class=env/undeclared-dependency modules=%s -- declare them in "
            "pyproject.toml [dependency-groups] test" % ",".join(absent)
        )
    return [chosen[name] for name in sorted(chosen)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / ".venv-test"))
    parser.add_argument("--python", default=os.environ.get("HERMES_BOOTSTRAP_PYTHON", "python3"))
    args = parser.parse_args(argv)

    out = Path(args.out).expanduser().resolve()
    pins = declared_pins()
    print("declared pins: %s" % ", ".join(pins))
    if not out.exists():
        subprocess.run([args.python, "-m", "venv", str(out)], check=True)
    python = out / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run([str(python), "-m", "pip", "install", "--quiet", "--upgrade", "pip"], check=True)
    subprocess.run([str(python), "-m", "pip", "install", "--quiet", *pins], check=True)

    check = subprocess.run(
        [str(python), "-m", "tests.preflight"], cwd=str(ROOT), capture_output=True, text=True
    )
    sys.stdout.write(check.stdout)
    sys.stderr.write(check.stderr)
    if check.returncode != 0:
        return check.returncode
    print("built: %s" % python)
    print("next: HERMES_PYTHON=%s scripts/run_tests.sh tests/" % python)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
