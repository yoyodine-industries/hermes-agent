#!/usr/bin/env python3
"""Fork-symbol census: the fork's own symbols must still EXIST and still have a CALLER.

WHY (ruling t-df0c4501f3 (4); the lesson is the merge-result census's, one layer up). A merge
into `overrides/live` that takes upstream's file whole does not delete the fork's capability
LOUDLY — it leaves its symbol behind as code nothing calls. A missing symbol fails a test; a
DEAD symbol passes forever, so the next reader treats a test-green tree as proof the capability
is live while production never runs a line of it. That is exactly what happened to the held
receipt: the fork's transport printed it, the merged runner answered a bare `reason`, and the
fork's tests stayed green over a function the merge had dropped.

So this census fails on TWO things, not one:
  * ABSENT      - a symbol of the fork delta is gone from the tree under test;
  * NO-REFERENCE - it is present, but no non-test code calls or imports it (the dead-symbol
                   case, which is the whole point: presence alone is the weaker half).

THE SYMBOL LIST IS DERIVED, NEVER HAND-WRITTEN (requirement (4c)): it comes from the FORK
DELTA — names present on the fork line and absent from the upstream base — because a hand list
rots into the very silence it was meant to break. When HEAD is a merge, the two parents ARE
that delta, so the defaults are `--fork HEAD^1 --upstream-base HEAD^2`, and the census answers
on the MERGE RESULT without being told which refs to compare.

Exit code: 0 when every fork symbol exists and is referenced and every required code literal is
present; 1 on any finding. This is an instrument: it writes nothing.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

#: Directory names that are NOT product code. A symbol whose only reference is a test is a
#: symbol production does not call - that is precisely the defect this census names.
NON_PRODUCT = ("tests", "test", "__pycache__", "website", "node_modules")

#: The code literals the desktop sender acts on (ruling t-df0c4501f3 (4)): a number is
#: checkable, so these go in as literals rather than as a paragraph of prose.
DEFAULT_CODES = ("5093", "5096")


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo)] + list(args),
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit("git %s failed: %s" % (" ".join(args), proc.stderr.strip()))
    return proc.stdout


def rev(repo: Path, ref: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref],
                          capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def is_merge(repo: Path, ref: str) -> bool:
    parents = git(repo, "rev-list", "--parents", "-n", "1", ref.split()[0]).split()
    return len(parents) > 2



def symbols_of(source: str, path: str) -> dict:
    """Top-level DEFINITIONS in one module: `{name: line}`.

    Top-level only, and definitions only: a census that counted every assignment would list
    locals and imports as "the fork's symbols", and the fork delta would then be noise. What
    a merge can silently drop is a module-level name - a function, a class, a constant.
    """
    found: dict = {}
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return found
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found[node.name] = node.lineno
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = node.lineno
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found[node.target.id] = node.lineno
    return found



#: A definition the fork ADDED: what a merge can drop without ever failing a test.
DEF_LINE = re.compile(r"^\+(?:async\s+def|def|class)\s+([A-Za-z_]\w*)")
ASSIGN_LINE = re.compile(r"^\+([A-Za-z_]\w*)\s*(?::[^=]+)?=")


def show(repo: Path, ref: str, path: str) -> str:
    """One blob at `ref`, or "" when the file does not exist there."""
    proc = subprocess.run(["git", "-C", str(repo), "show", "%s:%s" % (ref, path)],
                          capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else ""


def fork_delta(repo: Path, fork: str, upstream_base: str) -> dict:
    """`{path: {name: line}}` for names on the FORK line that the upstream base does NOT have.

    Per FILE, not globally: a name that moved files is still the fork's symbol, and treating a
    move as "absent upstream, therefore fork-only" would be true but useless - the check that
    matters is whether the name survives in the tree under test at all.
    """
    # ONE DIFF, NOT A FETCH PER FILE: the fork-only names are exactly the DEFINITION LINES the
    # fork line ADDED, so the delta is read out of a single `git diff -U0` instead of two
    # `git show`s per changed file. The first version of this needed minutes on a deep base -
    # a census slow enough to skip is a census that runs never.
    diff = git(repo, "diff", "-U0", "--no-color", upstream_base, fork, "--", "*.py")
    delta: dict = {}
    path = ""
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            candidate = line[6:].strip()
            # A TEST-ONLY SYMBOL IS NOT A FORK CAPABILITY: `test_*` functions are defined in
            # tests/ and called by the runner, so including them would report every new test
            # as a dead symbol and bury the one finding that matters.
            path = candidate if is_product_path(candidate) else ""
            continue
        if not path or not line.startswith("+") or line.startswith("+++"):
            continue
        match = DEF_LINE.match(line) or ASSIGN_LINE.match(line)
        if match and match.group(1) != "_":  # `_ = ...` is a throwaway, not a capability
            delta.setdefault(path, {})[match.group(1)] = 0
    return delta


def is_product_path(path: str) -> bool:
    """True for a tracked module that is PRODUCT code (the mirror of `_product_sources`)."""
    parts = set(Path(path).parts[:-1])
    return not (parts & set(NON_PRODUCT))


def _product_sources(repo: Path) -> list:
    out = []
    for path in repo.rglob("*.py"):
        if not is_product_path(str(path.relative_to(repo))):
            continue
        out.append(path)
    return out


def references_bulk(repo: Path, names, limit: int = 5) -> dict:
    """`{name: ["file:line", ...]}`, in ONE pass over the product sources.

    One pass, not one scan per symbol: a census that walks the tree for every name it checks
    is slow enough that nobody runs it, and an instrument nobody runs is the silence this
    census exists to break. Definitions themselves are counted as references - the caller
    decides what a definition means for its own symbol.
    """
    wanted = sorted({name for name in names if name})
    out: dict = {name: [] for name in wanted}
    if not wanted:
        return out
    # TOKENS, NOT AN ALTERNATION: a regex with one branch per wanted name is quadratic in
    # practice on a deep delta (measured: minutes), and the token set answers the same
    # question in one pass. A file that mentions none of the names is skipped outright.
    # `\w+` and not `[A-Za-z_]\w*`: half the codes the sender acts on are NUMBERS (5093,
    # 5096), and a token pattern that cannot match a digit reports every one of them absent.
    token = re.compile(r"\w+")
    wanted_set = set(wanted)
    for path in _product_sources(repo):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        words = set(token.findall(text))
        present = words & wanted_set
        if not present:
            continue
        rel = str(path.relative_to(repo))
        for number, line in enumerate(text.splitlines(), start=1):
            for name in set(token.findall(line)) & present:
                bucket = out.get(name)
                if bucket is not None and len(bucket) < limit:
                    bucket.append("%s:%d" % (rel, number))
    return out



def census(repo: Path, fork: str, upstream_base: str, codes=DEFAULT_CODES) -> dict:
    delta = fork_delta(repo, fork, upstream_base)
    all_names = [name for names in delta.values() for name in names] + list(codes)
    bulk = references_bulk(repo, all_names)
    # The tree under test is the WORKING TREE (a merge result may still be uncommitted), and
    # only the delta's own files are parsed: anything else cannot hold a fork symbol.
    present = {}
    for path in delta:
        disk = repo / path
        text = ""
        if disk.is_file():
            text = disk.read_text(encoding="utf-8", errors="replace")
        else:
            text = show(repo, "HEAD", path) or ""
        present[path] = symbols_of(text, path)
    findings = []
    rows = []
    for path, names in sorted(delta.items()):
        for name, line in sorted(names.items()):
            where = present.get(path, {})
            if name not in where:
                findings.append({"class": "ABSENT", "symbol": name, "path": path,
                                 "why": "the upstream base never had it, and the tree under "
                                        "test no longer defines it - the merge dropped it"})
                rows.append({"symbol": name, "path": path, "status": "ABSENT",
                             "refs": []})
                continue
            # A DEFINITION IS NOT A CALLER: drop the symbol's own definition line, and keep
            # every other mention (an import, a call, a re-export) as a reference.
            hits = [hit for hit in bulk.get(name, [])
                    if hit != "%s:%s" % (path, where.get(name, line))]
            status = "ok" if hits else "NO-REFERENCE"
            row = {"symbol": name, "path": path, "line": where.get(name, line),
                   "status": status, "refs": hits[:5]}
            rows.append(row)
            if status == "NO-REFERENCE":
                findings.append({"class": "NO-REFERENCE", "symbol": name, "path": path,
                                 "why": "present, but no non-test code names it: a dead fork "
                                        "symbol passes every test forever"})
    code_rows = []
    for code in codes:
        hits = list(bulk.get(code, []))
        code_rows.append({"code": code, "refs": hits[:5], "status": "ok" if hits else "ABSENT"})
        if not hits:
            findings.append({"class": "ABSENT", "symbol": code, "path": "",
                             "why": "the code literal the sender acts on is gone from the "
                                    "non-test tree"})
    return {"fork": fork, "upstream_base": upstream_base, "rows": rows,
            "codes": code_rows, "findings": findings}


#: Where the known-dead list lives, relative to the repo root.
BASELINE = "scripts/fork_symbol_census_baseline.txt"


def _key(finding: dict) -> str:
    """One stable spelling per finding, so a baseline line is greppable and diffable."""
    if finding.get("path"):
        return "%s::%s" % (finding["path"], finding["symbol"])
    return "code:%s" % finding["symbol"]


def load_baseline(path: Path) -> set:
    if not path.is_file():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")}


def render(result: dict) -> str:
    lines = ["fork delta: %s .. upstream %s" % (result["fork"][:12],
                                               result["upstream_base"][:12])]
    for row in result["rows"]:
        where = "%s:%s" % (row["path"], row.get("line") or "-")
        lines.append("  %-13s %-34s %-46s refs=%d" % (
            row["status"], row["symbol"], where, len(row["refs"])))
    for row in result["codes"]:
        lines.append("  %-13s code %-29s %-46s refs=%d" % (
            row["status"], row["code"], "", len(row["refs"])))
    if result["findings"]:
        lines.append("")
        lines.append("FINDINGS (%d):" % len(result["findings"]))
        for finding in result["findings"]:
            lines.append("  %-13s %-30s %s" % (finding["class"], finding["symbol"],
                                               finding["why"]))
    lines.append("")
    lines.append("census: %d fork symbol(s), %d finding(s)"
                 % (len(result["rows"]), len(result["findings"])))
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--repo", default=".", help="the checkout to census")
    parser.add_argument("--fork", default="",
                        help="the fork line (default: HEAD^1 when HEAD is a merge)")
    parser.add_argument("--upstream-base", default="",
                        help="the upstream base (default: HEAD^2 when HEAD is a merge)")
    parser.add_argument("--codes", default=",".join(DEFAULT_CODES),
                        help="code literals that must still appear (comma separated)")
    parser.add_argument("--baseline", default="",
                        help="known dead fork symbols, one `path::symbol` per line: a gate "
                             "that fails on long-standing findings is a gate nobody can merge "
                             "past, so the baseline is a RATCHET - unknown findings and "
                             "baseline entries that are now referenced both fail")
    parser.add_argument("--update-baseline", action="store_true",
                        help="rewrite the baseline from this run's findings")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    if not rev(repo, "HEAD"):
        raise SystemExit("not a git checkout: %s" % repo)
    fork = args.fork
    upstream = args.upstream_base
    if not fork or not upstream:
        if not is_merge(repo, "HEAD"):
            raise SystemExit("HEAD is not a merge: name --fork and --upstream-base explicitly "
                             "(the census cannot derive a delta it was not told)")
        parents = git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
        fork = fork or parents[0]
        upstream = upstream or parents[1]
    codes = tuple(code.strip() for code in args.codes.split(",") if code.strip())
    result = census(repo, fork, upstream, codes)
    baseline_path = Path(args.baseline) if args.baseline else repo / BASELINE
    if args.update_baseline:
        baseline_path.write_text("".join(sorted(key + "\n" for key in
                                               {_key(finding) for finding in result["findings"]})),
                                 encoding="utf-8")
        print("baseline written: %s (%d entries)" % (baseline_path, len(result["findings"])))
        return 0
    known = load_baseline(baseline_path)
    new_findings, stale = [], []
    seen = set()
    for finding in result["findings"]:
        key = _key(finding)
        seen.add(key)
        (new_findings if key not in known else []).append(finding)
    stale = sorted(known - seen)
    result["new_findings"] = new_findings
    result["stale_baseline"] = stale
    if args.json:
        print(json.dumps(result, indent=1))
    else:
        print(render(result))
    if not args.json:
        if known:
            print("  baseline: %d known dead fork symbol(s), %d new, %d stale"
                  % (len(known), len(new_findings), len(stale)))
        for key in stale:
            print("  STALE-BASELINE %s is referenced now: drop it from the baseline" % key)
    return 1 if (new_findings or stale) else 0


if __name__ == "__main__":
    sys.exit(main())
