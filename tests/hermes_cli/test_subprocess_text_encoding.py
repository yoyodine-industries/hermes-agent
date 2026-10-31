"""Guard: text-mode subprocess readers in the runtime trees pin utf-8 decoding (#55658).

On Windows, ``text=True`` without ``encoding`` decodes child output with the ANSI
code page (e.g. 'gbk'), and non-ASCII bytes raise UnicodeDecodeError inside
``subprocess._readerthread``, killing the backend before it becomes ready.

## Scope rule

The scan root follows the **shipped surface**, not the directory the bug was first
seen in. #55658 was first observed in ``hermes_cli/``; the class is a property of
every shipped runtime tree, so this guard walks all of ``RUNTIME_TREES`` (eight
trees) with one case per tree, each aggregating every violation into a single
assertion message. There is deliberately no per-tree fork of the guard, no skip
list of instance paths and no ``xfail`` ratchet: a guard that has to be told which
of its findings are real is not a guard.

## What counts as a violation (ruling t_66568c9c, §4)

A call is a violation when **all** of these hold:

1. its callee resolves to the ``subprocess`` API — ``subprocess.<fn>(...)``, a
   module alias (``import subprocess as sp``) or a name imported from it
   (``from subprocess import run as _run``); ``fn`` is one of ``run``, ``Popen``,
   ``check_output``, ``check_call``, ``call``, ``getoutput``,
   ``getstatusoutput``; **and**
2. it carries a ``text`` keyword whose value is present and is *not* the literal
   ``False`` — ``text=True`` and ``text=some_param`` are both violations, because a
   wrapper's ``text`` parameter defaults to ``False`` and is one caller away from
   the ANSI code page; **and**
3. it carries no ``encoding`` keyword.

Plus **one level of ``**`` resolution**: a ``subprocess`` call that splats a
module-level name whose value is a ``dict(...)`` call or a dict literal of
constants (plain or annotated assignment), and that bag carries ``text``
(non-``False``) and no ``encoding``, is a violation. That is the shape that catches
``agent/verify/runner.py``'s ``_SUBPROCESS_KW``.

Anything else carrying ``text=True`` is **not** a violation: ``tempfile.mkstemp(...,
text=True)``, ``catalog_mapping(..., text=True)``, an SDK's
``get_contents(text=True)`` and ``_send_messages(..., text=True)`` carry the keyword
for their own reasons, and demanding ``encoding=`` from them raises ``TypeError``.

## What is not scanned, and why

| tree | why |
|---|---|
| ``tests/`` (any path component) | the suite itself; a decode failure here is a red test, loud by construction |
| ``evals/`` | dev probes, run by hand, fail loudly |
| ``scripts/`` | dev/CI tooling, not shipped runtime; loud in the job that runs it (its 11 sites are pinned by a separate card, t_ae7f7c5b, and are deliberately not scanned here) |
| ``evidence/``, ``website/``, ``nix/``, ``optional-skills/`` | not shipped runtime: one-off proof harnesses, site/build tooling, opt-in skill assets |
| ``.venv/``, ``venv/``, ``node_modules/``, ``build/``, ``dist/``, ``.worktrees/``, ``__pycache__/`` | vendored/generated, not ours |

The exclusion is the ``tests/`` **directory** (any path component), never the
filename: filename heuristics are deliberately **not** used, so a shipped module
whose *filename* merely contains ``test`` (``hermes_cli/approvals_test.py``,
``pm/testenv.py``) is scanned like any other. Do not re-introduce a
``"test" in path.name`` skip — it hides shipped runtime modules from the walk.

## Residual gaps — review responsibilities, not guard claims

- A ``text``/``encoding`` keyword arriving through a ``**bag`` built dynamically is
  not statically resolvable. ``agent/proxy_sources/iron_proxy.py:_run`` does
  ``kwargs.update(text=True, encoding="utf-8", errors="replace")`` before
  ``subprocess.run``; the pin is real and this walk cannot see it.
- A wrapper whose ``subprocess`` call lives in *another* module is not visible from
  the caller's file. The guard covers the direct-call surface only.

## How to fix a finding — traps measured on this codebase

1. **Pin at the call, never inside a ``**splat`` dict.** A splat is invisible to
   this AST walk, and a second ``encoding=`` alongside a dict that already carries
   one is ``TypeError: multiple values for keyword argument 'encoding'`` on win32.
2. **Platform-gate the encoding when the platform already had a pin.** Unconditional
   UTF-8 regressed POSIX in ``cron/scheduler_script.py`` (upstream #66566,
   ``8be901ac0f``); the landed shape there is
   ``encoding="utf-8" if sys.platform == "win32" else None``.
3. **Pin ``errors=`` too.** A strict decode failure inside ``communicate()``
   discards the whole output (#105582: one bad byte failed an entire cron run);
   ``errors="replace"`` turns it into visible U+FFFD instead.
4. **A wrapper that takes ``text: bool = False`` must pin conditionally.** CPython
   sets ``text_mode`` from ``encoding or errors or text`` (``subprocess.py:889``),
   so an unconditional ``encoding=`` silently converts the wrapper's binary callers
   into text callers.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

RUNTIME_TREES = ("hermes_cli", "agent", "cron", "gateway", "plugins", "pm", "tools", "tui_gateway")

SUBPROCESS_FUNCS = frozenset(
    {"run", "Popen", "check_output", "check_call", "call", "getoutput", "getstatusoutput"}
)
"""Frame "subprocess" -> the functions whose text mode decides the child's decoding."""

SKIP_DIRS = frozenset(
    {"venv", ".git", "__pycache__", "node_modules", ".worktrees", "build", "dist", ".venv"}
)


def _subprocess_aliases(tree: ast.AST) -> tuple[set[str], dict[str, str]]:
    """Map the names bound to the subprocess module and to its functions."""
    module_names: set[str] = set()
    func_names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    module_names.add(alias.asname or "subprocess")
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "subprocess":
            for alias in node.names:
                if alias.name in SUBPROCESS_FUNCS:
                    func_names[alias.asname or alias.name] = alias.name
    return module_names, func_names


def _dict_bag_items(value: ast.expr | None) -> dict[str, ast.expr] | None:
    """Keywords of a ``dict(...)`` call or a constant-keyed dict literal, else None."""
    if isinstance(value, ast.Call):
        func = value.func
        if not (isinstance(func, ast.Name) and func.id == "dict" and not value.args):
            return None
        items: dict[str, ast.expr] = {}
        for keyword in value.keywords:
            if keyword.arg is None:
                return None
            items[keyword.arg] = keyword.value
        return items
    if isinstance(value, ast.Dict):
        items = {}
        for key, item in zip(value.keys, value.values):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                return None
            items[key.value] = item
        return items
    return None


def _module_dict_bags(tree: ast.Module) -> dict[str, dict[str, ast.expr]]:
    """Module-level names bound to a statically readable ``dict`` bag."""
    bags: dict[str, dict[str, ast.expr]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            value, targets = node.value, node.targets
        elif isinstance(node, ast.AnnAssign):
            value, targets = node.value, [node.target]
        else:
            continue
        items = _dict_bag_items(value)
        if items is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                bags[target.id] = items
    return bags


def _is_literal_false(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _subprocess_fn(call: ast.Call, module_names: set[str], func_names: dict[str, str]) -> str | None:
    """The subprocess function this call invokes, or None if it is not one."""
    func = call.func
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id in module_names
        and func.attr in SUBPROCESS_FUNCS
    ):
        return func.attr
    if isinstance(func, ast.Name) and func.id in func_names:
        return func_names[func.id]
    return None


def _violation_reason(
    call: ast.Call,
    module_names: set[str],
    func_names: dict[str, str],
    bags: dict[str, dict[str, ast.expr]],
) -> str | None:
    fn = _subprocess_fn(call, module_names, func_names)
    if fn is None:
        return None
    keywords = {kw.arg: kw.value for kw in call.keywords if kw.arg is not None}
    if "encoding" in keywords:
        return None
    if "text" in keywords:
        if _is_literal_false(keywords["text"]):
            return None
        return f"subprocess.{fn}(text=...) without encoding="
    for keyword in call.keywords:
        if keyword.arg is not None or not isinstance(keyword.value, ast.Name):
            continue
        bag = bags.get(keyword.value.id)
        if bag is None or "encoding" in bag or "text" not in bag:
            continue
        if _is_literal_false(bag["text"]):
            continue
        return f"subprocess.{fn}(**{keyword.value.id}) carrying text= and no encoding="
    return None


def find_violations(source: str, name: str) -> list[str]:
    """Every §4 violation in ``source``, as ``name:line: reason`` strings."""
    tree = ast.parse(source)
    module_names, func_names = _subprocess_aliases(tree)
    if not module_names and not func_names:
        return []
    bags = _module_dict_bags(tree)
    found: list[str] = []
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        reason = _violation_reason(call, module_names, func_names, bags)
        if reason is not None:
            found.append(f"{name}:{call.lineno}: {reason}")
    return found


def _scanned_files(tree: str) -> list[Path]:
    root = REPO_ROOT / tree
    files: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        if set(path.relative_to(REPO_ROOT).parts) & SKIP_DIRS:
            continue
        if "tests" in path.relative_to(root).parts:
            continue
        files.append(path)
    return files


def _violations_in_tree(tree: str) -> list[str]:
    found: list[str] = []
    for path in _scanned_files(tree):
        relative = path.relative_to(REPO_ROOT).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:  # unreadable source is a finding, not a skip
            found.append(f"{relative}: unreadable as utf-8: {exc}")
            continue
        found.extend(find_violations(source, relative))
    return found


@pytest.mark.parametrize("tree", RUNTIME_TREES)
def test_runtime_tree_pins_utf8_decode(tree: str) -> None:
    found = _violations_in_tree(tree)
    assert not found, (
        f"{len(found)} text-mode subprocess call(s) in {tree}/ decode with the locale/ANSI "
        "code page instead of utf-8 (#55658):\n  "
        + "\n  ".join(found)
        + "\nPin encoding=\"utf-8\", errors=\"replace\" AT THE CALL (a **splat is invisible "
        "to this guard, and a second encoding= beside one is a TypeError on win32)."
    )


# --- the predicate itself, pinned over synthetic source -------------------------
#
# Without these the next tweak to find_violations() can silently weaken the guard.


def test_flags_direct_text_true_without_encoding() -> None:
    source = "import subprocess\nsubprocess.run(['x'], text=True)\n"
    assert find_violations(source, "<s>") == ["<s>:2: subprocess.run(text=...) without encoding="]


def test_allows_text_true_with_encoding() -> None:
    source = (
        "import subprocess\n"
        "subprocess.run(['x'], text=True, encoding='utf-8', errors='replace')\n"
    )
    assert find_violations(source, "<s>") == []


def test_allows_text_false() -> None:
    source = "import subprocess\nsubprocess.run(['x'], text=False)\n"
    assert find_violations(source, "<s>") == []


def test_flags_text_parameter_as_well_as_literal_true() -> None:
    source = (
        "import subprocess\n"
        "def _probe(cmd, text=False):\n"
        "    return subprocess.run(cmd, text=text)\n"
    )
    assert find_violations(source, "<s>") == ["<s>:3: subprocess.run(text=...) without encoding="]


def test_ignores_text_true_on_a_non_subprocess_call() -> None:
    source = (
        "import tempfile\n"
        "def catalog_mapping(url, text=False):\n"
        "    return url\n"
        "tempfile.mkstemp(text=True)\n"
        "catalog_mapping('u', text=True)\n"
    )
    assert find_violations(source, "<s>") == []


def test_resolves_module_alias() -> None:
    source = "import subprocess as sp\nsp.Popen(['x'], text=True)\n"
    assert find_violations(source, "<s>") == ["<s>:2: subprocess.Popen(text=...) without encoding="]


def test_resolves_name_imported_from_subprocess() -> None:
    source = "from subprocess import run as _run\n_run(['x'], text=True)\n"
    assert find_violations(source, "<s>") == ["<s>:2: subprocess.run(text=...) without encoding="]


def test_flags_splat_bag_from_dict_call() -> None:
    source = (
        "import subprocess\n"
        "_KW = dict(text=True, errors='replace')\n"
        "subprocess.run(['x'], **_KW)\n"
    )
    assert find_violations(source, "<s>") == [
        "<s>:3: subprocess.run(**_KW) carrying text= and no encoding="
    ]


def test_flags_splat_bag_from_annotated_dict_literal() -> None:
    source = (
        "import subprocess\n"
        "_KW: dict[str, object] = {'text': True, 'errors': 'replace'}\n"
        "subprocess.Popen(['x'], **_KW)\n"
    )
    assert find_violations(source, "<s>") == [
        "<s>:3: subprocess.Popen(**_KW) carrying text= and no encoding="
    ]


def test_allows_splat_bag_that_pins_encoding() -> None:
    source = (
        "import subprocess\n"
        "_KW = dict(text=True, encoding='utf-8', errors='replace')\n"
        "subprocess.run(['x'], **_KW)\n"
    )
    assert find_violations(source, "<s>") == []


def test_allows_splat_bag_with_text_false() -> None:
    source = (
        "import subprocess\n"
        "_KW = dict(text=False, capture_output=True)\n"
        "subprocess.run(['x'], **_KW)\n"
    )
    assert find_violations(source, "<s>") == []


def test_aggregates_every_violation_in_one_source() -> None:
    source = (
        "import subprocess\n"
        "subprocess.run(['a'], text=True)\n"
        "subprocess.check_output(['b'], text=True)\n"
    )
    assert find_violations(source, "<s>") == [
        "<s>:2: subprocess.run(text=...) without encoding=",
        "<s>:3: subprocess.check_output(text=...) without encoding=",
    ]


def test_filename_containing_test_no_longer_exempts_a_shipped_module(tmp_path, monkeypatch) -> None:
    """The scan root is the DIRECTORY: a ``test``-containing filename is not a skip."""
    source = "import subprocess\nsubprocess.run(['x'], text=True)\n"
    probe = tmp_path / "pm" / "testenv_probe.py"
    probe.parent.mkdir(parents=True)
    probe.write_text(source, encoding="utf-8")
    under_tests = tmp_path / "pm" / "tests" / "test_pm.py"
    under_tests.parent.mkdir(parents=True)
    under_tests.write_text(source, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", tmp_path)

    # scanned purely because it ships in pm/ — its filename does not exempt it ...
    assert _scanned_files("pm") == [probe]
    # ... and the reader inside it is reported, while the tests/ directory is still skipped
    assert find_violations(source, "pm/testenv_probe.py") == [
        "pm/testenv_probe.py:2: subprocess.run(text=...) without encoding="
    ]
