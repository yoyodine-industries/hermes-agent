"""Deliverable-5 proof: mirror + per-card worktrees vs per-card clones.

Runs the checkout this file lives in (prints the imported module path and
asserts it came from there), materialises three per-card worktrees off ONE bare
mirror of a real repo, and compares against three independent clones.

`--no-local` forces REAL object copies (a local clone hardlinks, which would
report ~0). Read-only against the source checkout: the mirror, the worktrees and
the clones all live in one temp dir which is REMOVED before exit unless
`--keep` is passed (review round 1: the script used to leave ~117 MiB behind).

Exit status: 0 = every verdict clean; 1 = a finding (release-on-close did not
release); 2 = the proof could not be run.

Usage:
    <live-venv>/bin/python evidence/proof_mirror.py [--keep] [--source <repo>]
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

CHECKOUT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CHECKOUT))

from hermes_cli import kanban_db_workspace as kbw  # noqa: E402

assert str(CHECKOUT) in kbw.__file__, kbw.__file__


def size_of(p: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            fp = os.path.join(root, f)
            try:
                total += os.lstat(fp).st_size
            except OSError:
                pass
    return total


def human(n: int) -> str:
    if n >= 1024**3:
        return f"{n/1024**3:.2f} GiB"
    if n >= 1024**2:
        return f"{n/1024**2:.1f} MiB"
    if n >= 1024:
        return f"{n/1024:.0f} KiB"
    return f"{n} B"


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="keep the temp tree (default: remove it)")
    ap.add_argument("--source", default=os.environ.get("KBW_PROOF_SOURCE",
                                                       "/opt/hermes_sandbox/yaan-platform"))
    args = ap.parse_args()

    src = Path(args.source)
    if not (src / ".git").exists() and not (src / "HEAD").exists():
        print(f"cannot run: source repo not found at {src}", file=sys.stderr)
        return 2
    print("module under test:", kbw.__file__)
    print("source repo:", src)

    tmp = Path(tempfile.mkdtemp(prefix="mirrorproof-"))
    try:
        # 1. ONE bare canonical mirror per upstream (real objects).
        mirror = tmp / "mirror.git"
        subprocess.run(["git", "clone", "-q", "--bare", "--no-local", str(src), str(mirror)],
                       check=True, capture_output=True, text=True)

        # Reproduce the live shape the review measured: the mirror's HEAD need not
        # be `main` -- it is whatever branch the source was checked out on. Point
        # HEAD at a non-trunk branch when the source has one, so this proof also
        # exercises release-on-close for an off-trunk mirror HEAD.
        heads = [ln.strip() for ln in
                 git(mirror, "for-each-ref", "--format=%(refname:short)", "refs/heads").stdout.splitlines()]
        off_trunk = next((h for h in heads if h not in ("main", "master")), None)
        if off_trunk:
            git(mirror, "symbolic-ref", "HEAD", f"refs/heads/{off_trunk}")
        print("mirror HEAD branch:", git(mirror, "symbolic-ref", "--short", "HEAD").stdout.strip())
        print("local trunk present:", [h for h in heads if h in ("main", "master")])

        kbw._kb.read_board_metadata = lambda board: {"default_workdir": str(mirror)}
        lane = tmp / "platform-coder"
        lane.mkdir()
        cards = ["t_proof0001", "t_proof0002", "t_proof0003"]
        for cid in cards:
            kbw._resolve_worktree_workspace(
                SimpleNamespace(id=cid, branch_name=None, workspace_path=str(lane / cid),
                                workspace_kind="worktree"),
                board="defcon",
            )

        mirror_sz = size_of(mirror)
        wt_sizes = {c: size_of(lane / c) for c in cards}
        total_shared = mirror_sz + sum(wt_sizes.values())
        print("\n--- ONE bare mirror + 3 per-card worktrees (shared object store) ---")
        print(f"bare mirror {human(mirror_sz)}")
        for c in cards:
            print(f"  worktree {c}  {human(wt_sizes[c])}")
        print(f"TOTAL       {human(total_shared)}")

        # 2. THREE independent clones of the same repo, for comparison.
        clone_each = []
        for cid in cards:
            d = tmp / f"clone-{cid}"
            subprocess.run(["git", "clone", "-q", "--no-local", str(src), str(d)],
                           check=True, capture_output=True, text=True)
            clone_each.append(size_of(d))
        clone_total = sum(clone_each)
        print("\n--- 3 independent per-card clones ---")
        print("per-clone:", ", ".join(human(s) for s in clone_each))
        print(f"TOTAL       {human(clone_total)}")
        print(f"\nsaving for 3 cards: {human(clone_total - total_shared)} "
              f"({clone_total/total_shared:.2f}x smaller)")

        print("object store SHARED (common dir is the ONE mirror):")
        shared = True
        for c in cards:
            common = kbw._git_common_dir(lane / c)
            # Resolve both sides: a temp dir under /var reports as /private/var.
            if common is None or Path(common).resolve() != Path(mirror).resolve():
                shared = False
            print("  ", c, "->", common)

        # 3. release on close, for a worktree cut from the (possibly off-trunk) mirror HEAD.
        kbw._cleanup_worktree_workspace(cards[0], str(lane / cards[0]), None)
        released = not (lane / cards[0]).exists()
        print("\nrelease on close ->", cards[0], "exists:", (lane / cards[0]).exists())
        print("git worktree list still references it:",
              str(lane / cards[0]) in git(mirror, "worktree", "list", "--porcelain").stdout)

        if not shared:
            print("FINDING: worktrees do not share the mirror's object store", file=sys.stderr)
            return 1
        if not released:
            print("FINDING: release-on-close did not release a clean worktree", file=sys.stderr)
            return 1
        print("VERDICT: clean (shared object store + release-on-close)")
        return 0
    finally:
        if args.keep:
            print("tmp kept:", tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)
            print("tmp removed:", tmp)


if __name__ == "__main__":
    raise SystemExit(main())
