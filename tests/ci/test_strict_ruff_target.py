"""Two invariants of the strict lint config, both about what the config may do to a working tree.

**Autofix target.** ruff's version-gated autofixes (UP037 strips annotation quotes, UP017/UP006/
UP007 swap in newer spellings) are safe only for ``target-version`` and newer. The Python floor is
``requires-python`` in pyproject.toml; before 3.14 annotations are evaluated eagerly, so a quote
stripped for a py314 target (a method annotated with its own class, a later-defined class) raises
NameError at import on 3.11-3.13. Pinning the target to the floor keeps every such fix valid on
every supported Python.

**Narrow-subset judgement.** ``[lint].select`` is a hand-picked census, far narrower than the rule
sets the tree's ``# noqa:`` directives are written against (upstream, IDEs, pyproject). RUF100
(``unused-noqa``) asks "does *this* config still need this directive", so under a narrow census
every noqa naming an unselected rule reads as unused: the linter refuses a file nobody touched, and
``lefthook.yml``'s ``ruff check --config ./ruff.strict.toml {staged_files}`` turns that into a
commit refusal mid-refactor. The guard below states the property RUF100 depends on, and is
deliberately VACUOUS when RUF100 is not selected (nothing else judges a directive against the
census, so every code it names may legitimately sit outside it).
"""

import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: A directive's code list: the marker, then one or more comma/space separated codes.
_NOQA = re.compile(r"#\s*noqa\s*:\s*(?P<codes>[A-Z]+[0-9]+(?:[\s,]+[A-Z]+[0-9]+)*)")
_CODE = re.compile(r"[A-Z]+[0-9]+")


def test_strict_ruff_target_is_the_supported_python_floor():
    floor = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["requires-python"]
    major, minor = re.search(r">=\s*(\d+)\.(\d+)", floor).groups()
    strict = tomllib.loads((ROOT / "ruff.strict.toml").read_text(encoding="utf-8"))
    assert strict["target-version"] == f"py{major}{minor}", (
        f"ruff.strict.toml target-version {strict['target-version']!r} is not the requires-python "
        f"floor {floor!r}: its autofixes would emit syntax the oldest supported Python cannot import")


def _noqa_codes() -> set[str]:
    """Every rule code named by a ``# noqa:`` directive in the tracked ``*.py`` tree."""
    proc = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "--", "*.py"],
        capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"tracked-file inventory failed: git ls-files exited {proc.returncode}"
    codes: set[str] = set()
    for rel in filter(None, proc.stdout.split("\0")):
        try:
            text = (ROOT / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for directive in _NOQA.finditer(text):
            codes.update(_CODE.findall(directive.group("codes")))
    return codes


def test_every_noqa_code_is_carried_by_the_strict_select():
    lint = tomllib.loads((ROOT / "ruff.strict.toml").read_text(encoding="utf-8"))["lint"]
    carried = set(lint["select"]) | set(lint.get("external", []))
    codes = _noqa_codes()
    assert len(codes) >= 10, (
        f"the scan found only {len(codes)} noqa code(s) in the tracked tree ({sorted(codes)}): the "
        f"inventory or the directive pattern has broken, so this guard would pass without looking")
    if "RUF100" not in carried:
        # Vacuous by design: with RUF100 off, no enabled rule judges a directive against this
        # census, so a code outside it is not "unused" and must not be reported as such.
        return
    uncarried = sorted(codes - carried)
    assert not uncarried, (
        f"ruff.strict.toml selects RUF100 (unused-noqa) but does not carry {uncarried}: it will "
        f"report every noqa naming one of them as unused, so a file carrying such a directive can "
        f"never be staged (lefthook runs ruff on the staged files) and a landing mid-refactor is "
        f"refused -- t_1b256007 had to bypass the hook with --no-verify. Either drop the RUF100 row "
        f"or carry the codes; do not leave the two halves disagreeing")
