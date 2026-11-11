"""The end-state guard: an update must hand back the tree its install stamp describes.

``hermes update`` had a single-flight guard but no *end-state* guard. On 2026-11-06 it
parked on ``overrides/live`` (289 commits not in ``origin/main``), switched the running
checkout to ``main`` — the venv and ``install-stamp.json`` were built for
``overrides/live@6444bc1acf`` — and returned success. Nothing refused that end state; the
*next* launch discovered it and could not repair it (six attempts, then a permanent
"a source update could not be finished automatically"). A stage log is not evidence of the
tree that was handed back; the stamp/HEAD pair is.
"""

from __future__ import annotations

from pathlib import Path


def _short(commit: str | None) -> str:
    return (commit or "?")[:10]


def checkout_off_install_stamp(root: Path) -> str | None:
    """One prominent line when *root* is NOT the tree its install stamp names, else ``None``.

    Only a *proven* divergence refuses: a tree with no stamp, no stamp commit, or no git
    identity (the ZIP fallback publishes none) claims nothing, so nothing is contradicted.
    An adopted stamp (``adoptedAt``) is identity without a build, but it still names the
    checkout it was taken on — a later branch switch off it is exactly what must be caught.
    """
    from hermes_cli.steward import read_install_stamp
    from hermes_cli.version_info import _git_version_info

    root = Path(root)
    stamp = read_install_stamp(root)
    commit = stamp.get("commit")
    if not isinstance(commit, str) or not commit:
        return None
    info = _git_version_info(root, include_untracked=True)
    if info.commit is None:
        return None
    stamp_branch = stamp.get("branch")
    stamp_branch = stamp_branch if isinstance(stamp_branch, str) and stamp_branch else None
    head_branch = info.branch
    if commit == info.commit and (stamp_branch is None or head_branch is None or stamp_branch == head_branch):
        return None
    stamped = f"{stamp_branch or 'detached'}@{_short(commit)}"
    checked_out = f"{head_branch or 'detached'}@{_short(info.commit)}"
    return (
        f"✗ Update finished with the checkout OFF its install stamp: the stamp says {stamped}, "
        f"the tree is {checked_out}.\n"
        f"  The install and its dependencies were built for the stamped commit, so nothing "
        f"about this tree is proven current.\n"
        f"  Return the tree to the stamped branch and re-run the update:\n"
        f"    git -C {root} checkout {stamp_branch or commit} && hermes update"
    )
