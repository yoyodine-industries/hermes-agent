"""The split cluster's cross-module references resolve -- and the reader agrees with the runtime.

What these tests are for (card t_43f7b7fc, episodes t_5c20221e / 2026-10-22): the kanban kernel
is one program over three modules that import each other LAZILY, so a caller whose callee was
never defined is not an ImportError at boot -- it is an AttributeError at call time, and nothing
in the suite notices. The guard is only worth its file if it is BOTH sides of that: silent on a
whole tree, and loud on a caller without its callee, naming file, line and symbol.

The parity test is the anti-vacuity proof: for every reference the static reader judges in the
REAL sources, ``hasattr`` is checked against the imported module. A reader that quietly stopped
resolving references would still pass a "no misses" test; it cannot pass that one.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import module_ref_guard as guard

REPO = Path(__file__).resolve().parents[2]
MEMBERS = ("kanban_db", "kanban_gate_invariants", "kanban_swarm")


def _sources(root: Path) -> dict:
    return {name: (root / "hermes_cli" / f"{name}.py").read_text(encoding="utf-8-sig")
            for name in MEMBERS}


def _write_cluster(root: Path, **bodies: str) -> Path:
    """A synthetic tree carrying every declared member (the guard reads the whole set)."""
    package = root / "hermes_cli"
    package.mkdir(parents=True, exist_ok=True)
    for name in guard.CROSS_MODULE_SET:
        (package / f"{name}.py").write_text(bodies.get(name, ""), encoding="utf-8")
    return root


@pytest.fixture
def real_cluster(tmp_path) -> Path:
    """The three real module sources, copied, so a test can edit them without touching the tree."""
    root = tmp_path / "tree"
    package = root / "hermes_cli"
    package.mkdir(parents=True)
    for name, source in _sources(REPO).items():
        (package / f"{name}.py").write_text(source, encoding="utf-8")
    return root


def _line_of(path: Path, needle: str) -> int:
    for number, text in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if needle in text:
            return number
    raise AssertionError(f"{needle!r} not found in {path}")


# --- the guard covers the cluster it claims to -------------------------------------------------


def test_the_declared_set_is_the_split_cluster():
    assert set(MEMBERS) <= set(guard.CROSS_MODULE_SET)


def test_the_tree_resolves_every_cross_module_reference():
    """Silent on a whole tree -- and it really did look at something (references > 0)."""
    scans, misses, unresolved = guard.check_tree(REPO)
    assert [s.name for s in scans] == list(guard.CROSS_MODULE_SET)
    assert sum(s.references for s in scans) > 0, "the guard judged nothing at all"
    assert not unresolved, [m.render() for m in unresolved]
    assert not misses, [m.render() for m in misses]


def test_every_judged_reference_is_true_at_runtime():
    """The static reader and the imported modules agree, reference by reference."""
    import importlib

    namespaces = {name: guard.read_namespace(REPO, name) for name in guard.CROSS_MODULE_SET}
    judged = set()
    for name in guard.CROSS_MODULE_SET:
        path = namespaces[name].path
        module = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        targets, ambiguous = guard._alias_map(module, guard.CROSS_MODULE_SET, guard.PACKAGE)
        for reference, target in (
            (n, n.value) for n in ast.walk(module) if isinstance(n, ast.Attribute)
        ):
            if isinstance(target, ast.Name) and target.id in targets \
                    and target.id not in ambiguous:
                judged.add((targets[target.id], reference.attr))

    assert len(judged) > 0, "no references were resolved at all"
    for provider, symbol in sorted(judged):
        module = importlib.import_module(f"{guard.PACKAGE}.{provider}")
        assert hasattr(module, symbol), f"{guard.PACKAGE}.{provider}.{symbol} is missing"


# --- the episode: a caller without its callee --------------------------------------------------


def test_a_caller_without_its_callee_is_a_miss_naming_file_line_and_symbol(real_cluster):
    """t_5c20221e's exact split, replayed on the real sources: the lazy import plus the call."""
    caller = real_cluster / "hermes_cli" / "kanban_db.py"
    caller.write_text(caller.read_text(encoding="utf-8-sig") + (
        "\n\ndef _probe_gate_filing_shape(shape):\n"
        "    from hermes_cli import kanban_gate_invariants as _gates\n"
        "    return _gates.gate_filing_shape(shape)\n"
    ), encoding="utf-8")
    call_line = _line_of(caller, "_gates.gate_filing_shape(")

    _, misses, unresolved = guard.check_tree(real_cluster)

    assert not unresolved
    assert len(misses) == 1, [m.render() for m in misses]
    miss = misses[0]
    assert miss.file.endswith("kanban_db.py")
    assert miss.line == call_line
    assert miss.symbol == "gate_filing_shape"
    assert miss.provider == "kanban_gate_invariants"
    assert miss.form == "attribute"
    assert "_gates.gate_filing_shape" in miss.expression
    assert "no module in hermes_cli/ defines 'gate_filing_shape'" in miss.hint
    rendered = miss.render()
    assert f"kanban_db.py:{call_line}:" in rendered and "gate_filing_shape" in rendered


def test_defining_the_callee_makes_the_same_tree_clean(real_cluster):
    """Two-sided: the repair is the callee, not a deleted call."""
    caller = real_cluster / "hermes_cli" / "kanban_db.py"
    caller.write_text(caller.read_text(encoding="utf-8-sig") + (
        "\n\ndef _probe_gate_filing_shape(shape):\n"
        "    from hermes_cli import kanban_gate_invariants as _gates\n"
        "    return _gates.gate_filing_shape(shape)\n"
    ), encoding="utf-8")
    assert guard.check_tree(real_cluster)[1], "the injected caller must be a miss first"

    callee = real_cluster / "hermes_cli" / "kanban_gate_invariants.py"
    callee.write_text(callee.read_text(encoding="utf-8-sig") + (
        "\n\ndef gate_filing_shape(shape):\n    return shape\n"
    ), encoding="utf-8")
    _, misses, _ = guard.check_tree(real_cluster)
    assert not misses, [m.render() for m in misses]


# --- the other reference forms ------------------------------------------------------------------


def test_a_symbol_import_of_a_missing_name_is_a_miss(tmp_path):
    root = _write_cluster(
        tmp_path,
        kanban_db="def present():\n    return 1\n",
        kanban_swarm=(
            "from hermes_cli.kanban_db import present, absent\n"
            "\n\ndef f():\n    return present(), absent()\n"
        ),
    )
    _, misses, _ = guard.check_tree(root)
    assert [(m.form, m.symbol, m.line) for m in misses] == [("import", "absent", 1)]


def test_getattr_with_a_literal_name_is_judged(tmp_path):
    root = _write_cluster(
        tmp_path,
        kanban_db="def present():\n    return 1\n",
        kanban_swarm=(
            "from hermes_cli import kanban_db as kb\n"
            "\n\ndef f():\n    return getattr(kb, 'absent')\n"
        ),
    )
    _, misses, _ = guard.check_tree(root)
    assert [(m.form, m.symbol) for m in misses] == [("getattr", "absent")]


def test_a_local_shadow_of_the_alias_is_not_a_false_positive(tmp_path):
    """``kb = _board()`` in one function must not make every ``kb.x`` elsewhere a miss."""
    root = _write_cluster(
        tmp_path,
        kanban_db="def close():\n    return 1\n",
        kanban_swarm=(
            "from hermes_cli import kanban_db as kb\n"
            "\n\ndef _board():\n    return object()\n"
            "\n\ndef shadowed():\n    kb = _board()\n    return kb.close()\n"
        ),
    )
    _, misses, _ = guard.check_tree(root)
    assert not misses, [m.render() for m in misses]


def test_an_open_namespace_is_unresolved_rather_than_a_failure(tmp_path):
    """A provider that builds its namespace at runtime cannot be judged; say so, don't refuse."""
    root = _write_cluster(
        tmp_path,
        kanban_gate_invariants="from hermes_cli.kanban_db import *\n",
        kanban_db="def present():\n    return 1\n",
        kanban_swarm=(
            "from hermes_cli import kanban_gate_invariants as gates\n"
            "\n\ndef f():\n    return gates.assembled_at_runtime()\n"
        ),
    )
    _, misses, unresolved = guard.check_tree(root)
    assert not misses
    assert [m.symbol for m in unresolved] == ["assembled_at_runtime"]


# --- it stays cheap: a source reader, not an importer -------------------------------------------


def test_checking_a_tree_does_not_import_the_cluster():
    """``ast.parse`` only: importing the cluster is exactly what breaks, and what costs seconds."""
    probe = (
        "import sys\n"
        "from hermes_cli import module_ref_guard as g\n"
        "scans, misses, unresolved = g.check_tree('.')\n"
        "print(sorted(m for m in sys.modules if m.startswith('hermes_cli.kanban_')))\n"
    )
    out = subprocess.run([sys.executable, "-c", probe], cwd=str(REPO), capture_output=True,
                         text=True, timeout=180)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]", out.stdout


# --- the command ---------------------------------------------------------------------------------


def test_runtime_mode_asserts_hasattr_on_the_imported_members():
    """``--runtime`` is the design's own assertion: nothing dangling, so nothing to report."""
    out = subprocess.run(
        [sys.executable, "-m", "hermes_cli.module_ref_guard", "--tree", ".", "--runtime"],
        cwd=str(REPO), capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "+ hasattr" in out.stdout, out.stdout


def test_runtime_mode_keeps_the_static_miss_and_promotes_an_open_namespace(tmp_path):
    """A pair the static scan declined to judge is still checked once the module is imported."""
    root = _write_cluster(
        tmp_path,
        kanban_gate_invariants=(
            "from hermes_cli.kanban_db import *\n"          # open namespace: not judged statically
            "\n__all__ = ['present']\n"
        ),
        kanban_db="def present():\n    return 1\n",
        kanban_swarm=(
            "from hermes_cli import kanban_gate_invariants as gates\n"
            "\n\ndef f():\n    return gates.absent_at_runtime()\n"
        ),
    )
    _, misses, unresolved = guard.check_tree(root)
    assert not misses and [m.symbol for m in unresolved] == ["absent_at_runtime"]

    (tmp_path / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
    runtime = guard.runtime_misses(guard.check_tree(root)[0], root)
    assert [(m.form, m.symbol) for m in runtime] == [("runtime", "absent_at_runtime")]
    assert "hasattr" in runtime[0].hint


def test_the_command_exits_zero_on_a_whole_tree():
    out = subprocess.run([sys.executable, "-m", "hermes_cli.module_ref_guard", "--tree", "."],
                         cwd=str(REPO), capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.startswith("OK:"), out.stdout


def test_the_command_fails_and_reports_json_on_a_dangling_caller(tmp_path):
    root = _write_cluster(
        tmp_path,
        kanban_db="def present():\n    return 1\n",
        kanban_swarm=(
            "from hermes_cli import kanban_db as kb\n"
            "\n\ndef f():\n    return kb.absent()\n"
        ),
    )
    out = subprocess.run(
        [sys.executable, "-m", "hermes_cli.module_ref_guard", "--tree", str(root), "--json"],
        cwd=str(REPO), capture_output=True, text=True, timeout=180)
    assert out.returncode == 1, out.stdout + out.stderr
    report = json.loads(out.stdout)
    assert report["ok"] is False
    assert [(m["symbol"], m["form"], m["line"]) for m in report["misses"]] == [("absent",
                                                                             "attribute", 5)]


def test_the_command_refuses_a_tree_without_the_modules(tmp_path):
    out = subprocess.run(
        [sys.executable, "-m", "hermes_cli.module_ref_guard", "--tree", str(tmp_path)],
        cwd=str(REPO), capture_output=True, text=True, timeout=180)
    assert out.returncode == 2
    assert "no such module source" in out.stderr


def test_the_command_scans_an_explicit_module_set(tmp_path):
    """Extensible by one flag: a new member is measured the moment it is named."""
    package = tmp_path / "hermes_cli"
    package.mkdir(parents=True)
    (package / "alpha.py").write_text("def one():\n    return 1\n", encoding="utf-8")
    (package / "beta.py").write_text(
        "from hermes_cli import alpha as a\n\n\ndef f():\n    return a.two()\n", encoding="utf-8")
    _, misses, _ = guard.check_tree(tmp_path, modules=["alpha", "beta"])
    assert [(m.symbol, m.provider) for m in misses] == [("two", "alpha")]


def test_a_missing_member_is_a_hard_error():
    with pytest.raises(FileNotFoundError):
        guard.check_tree(REPO, modules=["kanban_db", "no_such_module"])


def test_the_real_cluster_copy_is_identical_to_the_tree(real_cluster):
    """The replay tests edit the copy; a stale copy would make them prove nothing."""
    assert _sources(real_cluster) == _sources(REPO)
