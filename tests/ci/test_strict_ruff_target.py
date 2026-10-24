"""Two invariants of the strict lint config, both about what a fix may do to a working tree.

**Autofix target.** ruff's version-gated autofixes (UP037 strips annotation quotes, UP017/UP006/
UP007 swap in newer spellings) are safe only for ``target-version`` and newer. The Python floor is
``requires-python`` in pyproject.toml; before 3.14 annotations are evaluated eagerly, so a quote
stripped for a py314 target (a method annotated with its own class, a later-defined class) raises
NameError at import on 3.11-3.13. Pinning the target to the floor keeps every such fix valid on
every supported Python.

**Unused directives.** The select list is a published rule census with many rows deliberately
commented out (``BLE001`` among them), so ordinary code carries ``# noqa: BLE001`` guards that are
dead against this config. RUF100 (``unused-noqa``) reports those dead directives, so selecting it
turns an untouched file into a commit-blocking error the moment it is staged -- see the test below.
"""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_strict_ruff_target_is_the_supported_python_floor():
    floor = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["requires-python"]
    major, minor = re.search(r">=\s*(\d+)\.(\d+)", floor).groups()
    strict = tomllib.loads((ROOT / "ruff.strict.toml").read_text(encoding="utf-8"))
    assert strict["target-version"] == f"py{major}{minor}", (
        f"ruff.strict.toml target-version {strict['target-version']!r} is not the requires-python "
        f"floor {floor!r}: its autofixes would emit syntax the oldest supported Python cannot import")


def test_strict_ruff_does_not_select_unused_noqa():
    strict = tomllib.loads((ROOT / "ruff.strict.toml").read_text(encoding="utf-8"))
    selected = strict["lint"]["select"]
    assert "RUF100" not in selected, (
        "ruff.strict.toml selects RUF100 (unused-noqa). It flags every noqa naming a rule this "
        "config does not select -- and since most of this census is deliberately off, ordinary "
        "code carries '# noqa: BLE001' guards that raise it the moment the file is staged, so a "
        "landing in progress cannot be committed (t_1b256007 had to bypass the hook). Drop the "
        "RUF100 row; fix the file's noqa instead of re-adding it")
