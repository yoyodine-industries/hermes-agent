"""No caller without its callee: every cross-module reference in a split cluster must resolve.

The kanban kernel is ONE program spread over several modules (``kanban_db``,
``kanban_gate_invariants``, ``kanban_swarm``). Each imports the others LAZILY, inside the
function that uses them, so a name one module expects from another is an ``AttributeError`` an
hour into a run -- not an ``ImportError`` at boot. Nothing in the language or in the test suite
notices the gap: the module resolves, the function runs, and only the one branch that reaches
the missing name dies.

That is not hypothetical. On 2026-10-22 a hot edit to ``hermes_cli/kanban_db.py`` called
``_gates.gate_filing_shape(...)`` while ``hermes_cli/kanban_gate_invariants.py`` -- the module
``_gates`` names -- had never defined it, and every caller of that path died at call time.
The repair (card t_5c20221e) then landed the call site WITHOUT the function and the split went
live a second time. A human read the two files and missed it twice.

This module closes the class statically. Declare the module set (``CROSS_MODULE_SET``, extended
by one constant), parse each member's source -- no imports: the check must stay cheap enough to
run on any tree, and importing the cluster is exactly what breaks -- and report every reference
made through another member's name whose target is not defined there. ``--runtime`` opts into
the ``hasattr`` assertion against the imported members for a gate that wants that belt too; the
default pass reads sources only.

    python -m hermes_cli.module_ref_guard --tree <checkout>     # 0 clean, 1 misses
    python -m hermes_cli.module_ref_guard --tree . --json       # machine-readable
    python -m hermes_cli.module_ref_guard --tree . --runtime    # + hasattr on the real modules

Reference forms resolved:

* ``alias.attr`` -- ``alias`` being a set member imported under any name, e.g. the
  ``from hermes_cli import kanban_gate_invariants as _gates`` that the episode's caller used;
* ``package.member.attr`` when the member was imported as ``import hermes_cli.kanban_db``;
* ``getattr(alias, "attr")`` with a literal attribute name;
* ``from hermes_cli.<member> import name`` -- a symbol import dies at call time too.

Two deliberate silences, both in the direction of no false positives: a name that is ALSO
assigned in the module (a local shadow of the alias) is not treated as the module, and a
provider whose namespace cannot be read off its source (a star import, ``globals()`` mutation,
a module-level ``__getattr__``) is reported as *unresolved* rather than as a failure.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Optional

#: The split cluster the guard covers. One tuple: adding a member is a one-line change, and the
#: discovery is per-member so a new member's existing split is measured the moment it is named.
CROSS_MODULE_SET = ("kanban_db", "kanban_gate_invariants", "kanban_swarm")

#: The package the members live in.
PACKAGE = "hermes_cli"

#: Attributes every module object carries regardless of its source.
_ALWAYS_PRESENT = frozenset({
    "__all__", "__annotations__", "__builtins__", "__cached__", "__dict__", "__doc__",
    "__file__", "__loader__", "__name__", "__package__", "__path__", "__spec__",
})

#: Callables whose presence anywhere in a module makes its namespace unreadable from source.
_OPEN_NAMESPACE_CALLS = frozenset({"globals", "vars", "exec", "eval"})


@dataclass(frozen=True)
class ModuleRefMiss:
    """One reference through another module's name whose target that module does not define."""

    file: str
    line: int
    column: int
    form: str
    expression: str
    provider: str
    symbol: str
    hint: str

    def render(self) -> str:
        phrase = {
            "attribute": "has no attribute",
            "getattr": "has no attribute (reached by getattr)",
            "import": "cannot satisfy the symbol import of",
        }.get(self.form, "has no attribute")
        line = f"{self.file}:{self.line}:{self.column}"
        tail = f" [hint: {self.hint}]" if self.hint else ""
        return (f"MISS {line}  {self.expression} -> {PACKAGE}.{self.provider} {phrase} "
                f"'{self.symbol}'; this reference dies at call time{tail}")


@dataclass
class ModuleNamespace:
    """What one module's source says it exposes."""

    name: str
    path: Path
    names: set
    open_reason: Optional[str] = None

    @property
    def open(self) -> bool:
        return self.open_reason is not None

    def defines(self, symbol: str) -> bool:
        return symbol in _ALWAYS_PRESENT or symbol in self.names


@dataclass
class ModuleScan:
    """One referencing module: its aliases, every judgement it made, and what failed."""

    name: str
    path: Path
    aliases: dict
    judged: list
    misses: list
    unresolved: list

    @property
    def references(self) -> int:
        return len(self.judged)


def _bound_names(target: ast.expr) -> Iterable[str]:
    """Names bound by an assignment/for/with target."""
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from _bound_names(element)
    elif isinstance(target, ast.Starred):
        yield from _bound_names(target.value)


def _walk_module_level(body: Iterable[ast.stmt]) -> Iterable[str]:
    """Every name a module-level statement binds, descending the compound statements that keep
    module scope (``if``/``try``/``for``/``with``) and never entering a function or class."""
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.name
            continue
        if isinstance(node, ast.Assign):
            for target in node.targets:
                yield from _bound_names(target)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            yield from _bound_names(node.target)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name != "*":
                    yield alias.asname or alias.name.split(".")[0]
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            yield from _bound_names(node.target)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    yield from _bound_names(item.optional_vars)

        for field in ("body", "orelse", "finalbody"):
            yield from _walk_module_level(getattr(node, field, []) or [])
        for handler in getattr(node, "handlers", []) or []:
            yield from _walk_module_level(handler.body)


def _open_reason(tree: ast.Module) -> Optional[str]:
    """Why this module's namespace cannot be read off its source, or ``None``."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
            return f"a star import from {node.module or '.'}"
        if isinstance(node, ast.FunctionDef) and node.name == "__getattr__":
            return "a module-level __getattr__"
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in _OPEN_NAMESPACE_CALLS):
            return f"a {node.func.id}() call"
    return None


def read_namespace(tree_root: Path, name: str, package: str = PACKAGE) -> ModuleNamespace:
    """The names one module's source exposes. Raises ``FileNotFoundError`` if it is not there."""
    path = tree_root / package / f"{name}.py"
    source = path.read_text(encoding="utf-8-sig")
    module = ast.parse(source, filename=str(path))
    return ModuleNamespace(
        name=name,
        path=path,
        names=set(_walk_module_level(module.body)),
        open_reason=_open_reason(module),
    )


def _module_import_target(node: ast.AST, members: tuple, package: str) -> Optional[str]:
    """The set member an import binds, or ``None``.

    ``from hermes_cli import kanban_db as kb``, ``from . import kanban_db``,
    ``from hermes_cli import kanban_db`` and ``import hermes_cli.kanban_db as kb`` all name a
    module; ``from hermes_cli.kanban_db import kanban_db_path`` names a SYMBOL and is judged
    separately.
    """
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        # ``from hermes_cli import <member>`` / ``from . import <member>``
        if module in ("", package) and node.level <= 1:
            names = [a.name for a in node.names if a.name in members]
            return names[0] if len(names) == 1 else None
        return None
    if isinstance(node, ast.Import):
        for alias in node.names:
            parts = alias.name.split(".")
            if len(parts) == 2 and parts[0] == package and parts[1] in members:
                return parts[1]
    return None


def _alias_map(module: ast.Module, members: tuple, package: str) -> tuple:
    """``(alias -> member, ambiguous names)`` for one module, from every import anywhere in it.

    Imports are collected at ANY depth: the cluster imports lazily inside functions, which is
    exactly why the gap is invisible at import time.
    """
    targets: dict = {}
    ambiguities = set()
    for node in ast.walk(module):
        member = _module_import_target(node, members, package)
        if member is None:
            continue
        if isinstance(node, ast.ImportFrom):
            bound = [a.asname or a.name for a in node.names if a.name == member]
        else:
            bound = [a.asname or a.name.split(".")[-1] for a in node.names
                     if a.name.split(".")[-1] == member]
        for name in bound:
            previous = targets.get(name)
            if previous is not None and previous != member:
                ambiguities.add(name)
            targets[name] = member

    # A name that is also a plain assignment target may be a local shadow, not the module.
    for node in ast.walk(module):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                for name in _bound_names(target):
                    if name in targets:
                        ambiguities.add(name)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            for name in _bound_names(node.target):
                if name in targets:
                    ambiguities.add(name)
    return targets, ambiguities


def _symbol_import_provider(node: ast.AST, members: tuple, package: str) -> Optional[str]:
    """The set member whose SYMBOLS an ``from ... import name`` statement pulls, or ``None``."""
    if not isinstance(node, ast.ImportFrom):
        return None
    module = node.module or ""
    if module == package:
        return None
    parts = module.split(".")
    if not parts or parts[-1] not in members:
        return None
    if len(parts) == 1 and node.level != 1:
        return None
    if len(parts) == 2 and parts[0] != package:
        return None
    return parts[-1]


def _resolve_references(
    module: ast.Module,
    scanner: ModuleScan,
    namespaces: dict,
    members: tuple,
    package: str,
) -> None:
    """Judge every reference the scanner's module makes through another member's name."""
    targets = scanner.aliases

    def judge(provider: str, symbol: str, node: ast.AST, expression: str,
              form: str) -> None:
        line = getattr(node, "lineno", 0)
        scanner.judged.append((provider, symbol, line, form, expression))
        namespace = namespaces.get(provider)
        if namespace is None or namespace.defines(symbol):
            return
        miss = ModuleRefMiss(
            file=str(scanner.path),
            line=line,
            column=getattr(node, "col_offset", 0) + 1,
            form=form,
            expression=expression,
            provider=provider,
            symbol=symbol,
            hint="",
        )
        if namespace.open:
            scanner.unresolved.append(miss)
        else:
            scanner.misses.append(miss)

    for node in ast.walk(module):
        if isinstance(node, ast.Attribute):
            base = node.value
            if isinstance(base, ast.Name) and base.id in targets:
                judge(targets[base.id], node.attr, node, f"{base.id}.{node.attr}", "attribute")
            elif (isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name)
                    and base.value.id == package and base.attr in members):
                judge(base.attr, node.attr, node, f"{package}.{base.attr}.{node.attr}",
                      "attribute")
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "getattr" and len(node.args) >= 2):
            base, symbol_node = node.args[0], node.args[1]
            if (isinstance(base, ast.Name) and base.id in targets
                    and isinstance(symbol_node, ast.Constant)
                    and isinstance(symbol_node.value, str)):
                judge(targets[base.id], symbol_node.value, node,
                      f'{base.id} getattr "{symbol_node.value}"', "getattr")
        elif isinstance(node, ast.ImportFrom):
            provider = _symbol_import_provider(node, members, package)
            if provider is None:
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                scanner.judged.append((provider, alias.name, node.lineno, "import",
                                       f"from {node.module} import {alias.name}"))
                namespace = namespaces.get(provider)
                if namespace is None or namespace.defines(alias.name):
                    continue
                miss = ModuleRefMiss(
                    file=str(scanner.path),
                    line=node.lineno,
                    column=node.col_offset + 1,
                    form="import",
                    expression=f"from {node.module} import {alias.name}",
                    provider=provider,
                    symbol=alias.name,
                    hint="",
                )
                (scanner.unresolved if namespace.open else scanner.misses).append(miss)


def _hint(tree_root: Path, provider: str, symbol: str, package: str = PACKAGE) -> str:
    """Where ``symbol`` IS defined, so the report names the remedy and not just the wound."""
    if symbol in _ALWAYS_PRESENT:
        return ""
    found = []
    package_dir = tree_root / package
    for path in sorted(package_dir.glob("*.py")):
        try:
            module = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        except (SyntaxError, OSError, UnicodeDecodeError):
            continue
        if symbol in set(_walk_module_level(module.body)):
            found.append(f"{package}/{path.name}")
    if not found:
        return f"no module in {package}/ defines '{symbol}'"
    if provider in [Path(f).stem for f in found]:
        return ""
    return f"'{symbol}' is defined in {', '.join(found)}"


def check_tree(tree_root, modules: Optional[Iterable[str]] = None,
               package: str = PACKAGE) -> tuple:
    """Scan one tree. Returns ``(scans, misses, unresolved)``; raises ``FileNotFoundError``.

    ``tree_root`` is a checkout carrying ``<package>/<module>.py`` for every member -- a live
    install, a worktree, or a temp copy of the sources. Nothing is imported.
    """
    root = Path(tree_root)
    members = tuple(modules) if modules else CROSS_MODULE_SET
    namespaces = {name: read_namespace(root, name, package) for name in members}

    scans = []
    for name in members:
        namespace = namespaces[name]
        module = ast.parse(namespace.path.read_text(encoding="utf-8-sig"),
                           filename=str(namespace.path))
        targets, ambiguous = _alias_map(module, members, package)
        scanner = ModuleScan(
            name=name,
            path=namespace.path,
            aliases={k: v for k, v in targets.items() if k not in ambiguous},
            judged=[],
            misses=[],
            unresolved=[],
        )
        _resolve_references(module, scanner, namespaces, members, package)
        for miss in scanner.misses + scanner.unresolved:
            object.__setattr__(miss, "hint", _hint(root, miss.provider, miss.symbol, package))
        scans.append(scanner)

    misses = [m for s in scans for m in s.misses]
    unresolved = [m for s in scans for m in s.unresolved]
    return scans, misses, unresolved


def runtime_misses(scans: Iterable[ModuleScan], tree_root,
                   package: str = PACKAGE) -> list:
    """The design's own assertion -- ``hasattr`` against the IMPORTED members.

    Off by default because the static scan is the cheap, side-effect-free pass; a gate that wants
    the runtime truth as well asks for it. Importing the cluster costs ~0.14s, so the tree under
    test goes first on ``sys.path``, each member is imported once, and every reference the scan
    judged is ``hasattr``-ed. A pair the static scan already reported is not repeated, but a pair
    it declined to judge (an open namespace) IS promoted here when the import says it is missing.
    """
    root = str(Path(tree_root).resolve())
    if root not in sys.path:
        sys.path.insert(0, root)

    already = {(m.provider, m.symbol) for scan in scans for m in scan.misses}
    imported = {}
    misses = []
    for scan in scans:
        for provider, symbol, line, form, expression in scan.judged:
            if (provider, symbol) in already:
                continue
            if provider not in imported:
                try:
                    imported[provider] = importlib.import_module(f"{package}.{provider}")
                except Exception as exc:  # the import failure IS the finding
                    imported[provider] = None
                    misses.append(ModuleRefMiss(
                        file=str(scan.path), line=line, column=1, form="runtime",
                        expression=f"import {package}.{provider}", provider=provider,
                        symbol="<module import>",
                        hint=f"the member does not import: {exc}"))
            module = imported.get(provider)
            if module is None or hasattr(module, symbol):
                continue
            misses.append(ModuleRefMiss(
                file=str(scan.path), line=line, column=1, form="runtime",
                expression=expression, provider=provider, symbol=symbol,
                hint=f"hasattr({package}.{provider}, '{symbol}') is False at runtime"))
    return misses


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m hermes_cli.module_ref_guard",
        description="Every cross-module reference in a split cluster must resolve.",
    )
    parser.add_argument("--tree", default=".", help="checkout to scan (default: .)")
    parser.add_argument("--module", action="append", dest="modules",
                        help=f"member to scan (repeatable; default: {' '.join(CROSS_MODULE_SET)})")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    parser.add_argument("--quiet", action="store_true", help="only the verdict line")
    parser.add_argument("--runtime", action="store_true",
                        help="also assert hasattr() against the imported members (imports them)")
    args = parser.parse_args(argv)

    try:
        scans, misses, unresolved = check_tree(args.tree, args.modules)
    except FileNotFoundError as exc:
        print(f"module-ref-guard: no such module source: {exc.filename}", file=sys.stderr)
        return 2
    except SyntaxError as exc:
        print(f"module-ref-guard: unparsable source: {exc.filename}:{exc.lineno}: {exc.msg}",
              file=sys.stderr)
        return 2

    if args.runtime:
        misses = misses + runtime_misses(scans, args.tree)

    references = sum(s.references for s in scans)
    if args.json:
        print(json.dumps({
            "tree": str(Path(args.tree).resolve()),
            "modules": [s.name for s in scans],
            "references": references,
            "runtime": bool(args.runtime),
            "misses": [asdict(m) for m in misses],
            "unresolved": [asdict(m) for m in unresolved],
            "ok": not misses,
        }, indent=2))
    else:
        if not args.quiet:
            for miss in misses + unresolved:
                print(miss.render())
            for miss in unresolved:
                print(f"note: {miss.expression} is against an open namespace -- not judged")
            if misses:
                print()
        verdict = "OK" if not misses else "FAIL"
        scanner_lines = ", ".join(f"{s.name} {s.references}" for s in scans)
        mode = " + hasattr" if args.runtime else ""
        print(f"{verdict}: {references} cross-module references across {len(scans)} modules "
              f"({scanner_lines}){mode}; {len(misses)} dangling, {len(unresolved)} unresolved")
        if misses:
            print("each MISS names a caller whose callee this tree does not carry: add the "
                  "callee to its module, or stop calling it.")
    return 0 if not misses else 1


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI test
    raise SystemExit(main())
