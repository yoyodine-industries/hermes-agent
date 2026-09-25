"""Close-path gate: a declared deliverable must be retrievable from outside the
producing workspace.

WHY THIS EXISTS
---------------
Card ``t_b579f394`` was closed ``done`` on evidence that only ever existed inside
the tree that produced it: a commit (``018511a``) authored in the card's scratch
workspace and never pushed, and six artifacts under a *profile* scratch dir
(``.../profiles/platform-stl/cache/scratch/t_b579f394/``). Card scratch is pruned
on idle, so within hours the commit resolved in no repository on the host (GitHub
answered 422 for it), the artifacts were gone, no pull request existed and no file
had been attached to the card. The completion was well formed, its DoD was
satisfiable by workspace-local evidence, and nothing on the close path asked
whether the declared deliverable was reachable from anywhere but the producing
tree.

This module is that question, asked BEFORE the closing write, on the completion's
own declaration:

1. **Revisions.**  Every revision the completion claims -- ``metadata['commit']``
   and its siblings, or prose of the form ``... @ <sha>`` / ``commit <sha>`` --
   must resolve as a git object in at least one checkout this host can reach. A
   sha that resolves nowhere is refused: that is the shape of a commit that died
   with the scratch tree it was authored in.
2. **Artifacts.**  Every path in ``metadata['artifacts']`` -- the kernel merges
   workspace paths named in prose into that list first -- must be retrievable
   from outside the producing workspace: an http(s) URL; a file inside the card's
   *scratch* workspace (the kernel stages those into durable task attachments
   before cleanup); or a file in durable storage. A file under an EPHEMERAL root
   the close path does **not** stage -- a ``cache/scratch`` directory, the system
   temp dir, the card's own worktree workspace -- is refused, as are a path that
   is not on disk and a relative path (it means something different to every
   reader).

PRECISION IS THE DESIGN CONSTRAINT
----------------------------------
This gate runs on every close, so it refuses only what it can prove, and a
refusal names the failing claim, the checkpoints it searched and the remedy:

* a 64-hex token is a sha256, never a revision. The ``t_b579f394`` completion
  carried one as its determinism proof; a gate that read it as a commit would
  have refused a legitimate close;
* a bare hex token in prose is a *claim* only next to a revision marker (``@``,
  ``commit``, ``sha``, ``tip``, ``rev``, ``merge``, or a ``key: value`` revision
  field) -- and a token whose context is a checksum (``sha1``/``md5``/``digest``)
  is never read as one, because a file digest is not a repository object;
* an unresolvable revision is refused *with the repositories that were searched*,
  so a sha that legitimately lives in a checkout this host has not been told
  about is fixed by adding that checkout to ``HERMES_KANBAN_REPO_ROOTS``
  (``os.pathsep``-separated), not by weakening the gate;
* a claim is never refused for want of a network call: resolution is local and
  deterministic, so the close path stays as fast as it is offline.

The operator's escape is ``complete_task(force=True)``
(``hermes kanban complete --force <id>``): it records a
``deliverable_gate_forced`` event and closes anyway.
"""
from __future__ import annotations

import contextlib
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable, Optional

# --------------------------------------------------------------------------
# Extraction: what does this completion CLAIM?
# --------------------------------------------------------------------------

# A git object name as cited in a completion: 7-40 hex, on word boundaries. The
# upper bound is deliberate -- a 64-hex token is a sha256 digest (a checksum the
# completion may well be proving), which is not a repository object and must
# never be read as a revision.
_REVISION_RE = re.compile(r"(?<![0-9A-Za-z])([0-9a-f]{7,40})(?![0-9A-Za-z])")

# Prose wordings that make the following hex token a claim ABOUT A REVISION.
_REVISION_MARKER_RE = re.compile(
    r"(?:\bcommits?\b|\bshas?\b|\btip\b|\brev\b|\brevision\b|\bmerged?\b|\bhead\b|@|=|:)\s*$",
    re.IGNORECASE,
)

# ...unless the marker itself says the token is a CHECKSUM: ``sha256 #1 : <hex>``,
# ``md5: <hex>``. Git object names are sha1/sha256, so the word alone cannot
# settle it -- but a digest named as one is not an object name claim.
_CHECKSUM_CONTEXT_RE = re.compile(
    r"(?:\bsha-?256\b|\bsha-?512\b|\bsha-?1\b|\bmd5\b|\bdigest\b|\bchecksum\b|\bhash\b)",
    re.IGNORECASE,
)

# How much prose before a token is its context.
_CONTEXT_CHARS = 40

# Metadata keys that declare a revision of something. Matched case-insensitively,
# with a trailing ``_sha``/``_commit`` suffix so ``head_sha``/``target_sha`` and
# friends are covered without enumerating every producer's spelling. ``hash`` is
# deliberately absent: a hash is a content digest, not a repository object.
_REVISION_KEYS = frozenset({
    "commit", "commits", "sha", "shas", "head", "tip", "rev", "revision",
    "merge_commit",
})
_REVISION_KEY_SUFFIXES = ("_sha", "_commit", "_rev", "_revision")

# Caps: the gate runs on every close, so its cost is bounded on purpose. A local
# object lookup is ~13ms, so sixty checkouts is under a second, and a handoff that
# cites a revision outside the first sixty still gets a refusal that names the
# remedy.
MAX_CLAIMS = 10
MAX_REPO_ROOTS = 60

# Fleet checkouts a claimed revision is searched in; ``None`` derives them from
# home. Tests -- and installs whose checkouts live elsewhere -- set this directly.
_FLEET_REPO_ROOTS: Optional[tuple[Path, ...]] = None


def _is_revision_key(key: Any) -> bool:
    name = str(key).strip().lower()
    return name in _REVISION_KEYS or name.endswith(_REVISION_KEY_SUFFIXES)


def _walk_revision_values(value: Any) -> Iterable[str]:
    """Every string under a revision-ish key, at any depth (``published`` blocks
    and nested handoff dicts carry them)."""
    if isinstance(value, dict):
        for key, item in value.items():
            if _is_revision_key(key):
                yield from _strings(item)
            else:
                yield from _walk_revision_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_revision_values(item)


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def _revision_from_declaration(value: str) -> Optional[str]:
    """The revision a declared value names, or ``None``.

    A revision field may hold a bare sha, a ``<branch> @ <sha>`` phrase, or a URL
    ending in one; the first 7-40 hex token is the claim.
    """
    match = _REVISION_RE.search(value)
    return match.group(1) if match else None


def revision_claims(metadata: Any, prose: str = "") -> list[str]:
    """Revisions this completion claims, in declaration order, deduplicated.

    Two sources only, both of which are unambiguous claims:

    * a revision-ish metadata key (bare sha, ``branch @ sha``, URL, nested dict);
    * prose in which a hex token follows a revision marker -- ``... @ 018511a``,
      ``commit 018511a`` -- and is not in checksum context.
    """
    claims: list[str] = []
    for value in _walk_revision_values(metadata):
        token = _revision_from_declaration(value)
        if token:
            claims.append(token)
    for match in _REVISION_RE.finditer(prose or ""):
        token = match.group(1)
        before = (prose or "")[max(0, match.start() - _CONTEXT_CHARS):match.start()]
        if not _REVISION_MARKER_RE.search(before):
            continue
        if _CHECKSUM_CONTEXT_RE.search(before):
            continue
        claims.append(token)
    deduped: list[str] = []
    for token in claims:
        if token not in deduped:
            deduped.append(token)
    return deduped[:MAX_CLAIMS]


def artifact_claims(metadata: Any) -> list[str]:
    """Paths/URLs the completion declares as deliverables.

    ``metadata['artifacts']`` is the kernel's own declaration channel (prose paths
    under the card workspace are merged into it by
    :func:`hermes_cli.kanban_db._merge_completion_prose_artifacts` before this gate
    runs), so that is the whole claim set -- prose is deliberately not re-scanned
    for paths, which would read ordinary sentences as claims.
    """
    if not isinstance(metadata, dict):
        return []
    raw = metadata.get("artifacts")
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item).strip() for item in raw if isinstance(item, str) and str(item).strip()]


# --------------------------------------------------------------------------
# Resolution: is the claimed object retrievable from outside the workspace?
# --------------------------------------------------------------------------

_SCRATCH_PARTS = ("cache", "scratch")


def _resolve_quietly(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:  # pragma: no cover - unreadable parent
        return path


def _is_within(path: Path, root: Optional[Path]) -> bool:
    if root is None:
        return False
    try:
        return path == root or path.is_relative_to(root)
    except (OSError, ValueError):
        return False


def ephemeral_scratch_dir(path: Path) -> bool:
    """True for a managed scratch directory the close path does not preserve.

    Hermes' scratch convention is ``<...>/cache/scratch/<task>`` (a profile's
    ``cache/scratch``, the default profile's). Those entries are pruned on idle
    and are NOT staged into task attachments -- only the card's own *kanban*
    scratch workspace is -- so a deliverable that lives there dies on a timer.
    """
    parts = path.parts
    return any(parts[i:i + 2] == _SCRATCH_PARTS for i in range(len(parts) - 1))


def _temp_roots() -> list[Path]:
    candidates = {Path("/tmp"), Path("/var/tmp"), Path(tempfile.gettempdir())}
    return sorted({_resolve_quietly(p) for p in candidates})


def artifact_problem(raw: str, *, workspace_kind: Optional[str], workspace: Optional[Path]) -> Optional[str]:
    """Why *raw* is not retrievable from outside the producing workspace, or ``None``."""
    if not raw:
        return "declared artifact is an empty string"
    if raw.startswith(("http://", "https://")):
        return None
    if not (raw.startswith("/") or raw.startswith("~")):
        return ("a relative path is not retrievable from outside the producing workspace: "
                "it resolves against whatever directory the reader happens to be in")
    path = Path(raw).expanduser()
    resolved = _resolve_quietly(path)
    in_workspace = _is_within(resolved, workspace)
    if in_workspace:
        if not path.exists():
            return f"declared artifact is not on disk ({raw})"
        if workspace_kind == "worktree":
            return ("inside the card's worktree workspace, which is removed when the card closes "
                    "and is NOT staged into task attachments")
        if workspace_kind == "scratch":
            # Staged into durable task attachments by _persist_scratch_completion_artifacts.
            return None
        return None
    if not path.exists():
        return f"declared artifact is not on disk ({raw})"
    if ephemeral_scratch_dir(resolved):
        return ("under an ephemeral scratch directory that is pruned on idle and is NOT staged "
                "into task attachments")
    if any(_is_within(resolved, root) for root in _temp_roots()):
        return "under the system temp directory, which is not preserved"
    return None


def _fleet_repo_roots() -> tuple[Path, ...]:
    """Checkouts the fleet keeps its work in: the live agent tree, plus the
    sandbox root (every child checkout of which is searched). ``None`` means
    "derive from home"; tests and unusual installs set the module global.
    """
    if _FLEET_REPO_ROOTS is not None:
        return _FLEET_REPO_ROOTS
    return (Path.home() / ".hermes" / "hermes-agent", Path("/opt/hermes_sandbox"))


def repo_roots(workspace: Optional[Path] = None) -> list[Path]:
    """Checkouts a claimed revision may be resolved against, in search order.

    The card's own workspace and the clones workers keep inside it come first (the
    tree the work was authored in is the one most likely to still hold the
    object); then ``HERMES_KANBAN_REPO_ROOTS``; then the fleet's checkouts.
    Non-checkouts are dropped, duplicates collapse, and the list is capped so a
    close stays cheap.
    """
    candidates: list[Path] = []

    def _add(path: Path) -> None:
        if path.is_dir() and (path / ".git").exists():
            resolved = _resolve_quietly(path)
            if resolved not in candidates:
                candidates.append(resolved)

    if workspace is not None:
        _add(workspace)
        with contextlib.suppress(OSError):
            for child in sorted(workspace.iterdir()):
                _add(child)
                if len(candidates) >= MAX_REPO_ROOTS:
                    break
    override = os.environ.get("HERMES_KANBAN_REPO_ROOTS", "").strip()
    for part in override.split(os.pathsep) if override else []:
        if part.strip():
            _add(Path(part).expanduser())
    for fleet_root in _fleet_repo_roots():
        _add(fleet_root)
        # Newest checkouts first. A sandbox root holds thousands of entries -- stale
        # adhoc trees, finished rebases -- and taking them alphabetically spends the
        # whole budget on directories nobody has touched in weeks, which is how a
        # legitimate claim comes back "unresolvable". Work a handoff cites lives in a
        # checkout touched lately.
        with contextlib.suppress(OSError):
            for child in sorted(fleet_root.iterdir(), key=_newest_first):
                _add(child)
                if len(candidates) >= MAX_REPO_ROOTS:
                    break
    return candidates[:MAX_REPO_ROOTS]


def _newest_first(path: Path) -> float:
    """Sort key: most recently modified first; unusable paths sort last."""
    try:
        return -path.stat().st_mtime
    except OSError:
        return float("inf")


_OBJECT_TYPES = frozenset({"commit", "tag", "tree", "blob"})

# The fleet's checkouts are partial (promisor) clones, so without GIT_NO_LAZY_FETCH a
# full 40-hex miss makes git reach for the network and stalls the close until the
# fetch gives up (measured: 30s per checkout, 90s for a single handoff). A miss must
# stay a miss; a local object lookup is milliseconds, so the budget stays small.
_BATCH_TIMEOUT = 5

# (root, token) -> exists. A dispatcher closing a run of cards, or an audit walking
# history, asks the same question about the same checkouts repeatedly; the answer
# cannot change within a run, so it is worth remembering.
_RESOLUTION_MEMO: dict[tuple[str, str], bool] = {}
_MEMO_MAX = 4096


def _batch_check(root: Path, tokens: list[str]) -> set[str]:
    """Objects that exist in *root* among *tokens*, in ONE git process.

    ``git cat-file --batch-check`` rather than one ``cat-file -t`` per claim: a
    handoff may declare ten revisions across twenty-five checkouts, and the close
    path must not pay two hundred processes for that.
    """
    root_key = str(root)
    is_checkout = (root / ".git").exists()
    known = {token for token in tokens if (root_key, token) in _RESOLUTION_MEMO}
    pending = [token for token in tokens if token not in known]
    hits: set[str] = set()
    if pending:
        try:
            proc = subprocess.run(
                ["git", "-C", root_key, "cat-file", "--batch-check"],
                input="\n".join(pending) + "\n", capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=_BATCH_TIMEOUT,
                check=False, env={**os.environ, "GIT_NO_LAZY_FETCH": "1"},
            )
        except (OSError, subprocess.SubprocessError):
            return set()
        # git answers with the canonical oid, so an abbreviated claim comes back
        # full length: match the answer back to the claim by prefix, never by
        # equality -- equality silently reports every short sha as missing.
        answered = [line.split()[0] for line in proc.stdout.splitlines()
                    if len(line.split()) >= 3 and line.split()[1] in _OBJECT_TYPES]
        hits = {token for token in pending
                if any(oid.startswith(token) for oid in answered)}
        # Only positive answers are remembered: a checkout can gain an object (a
        # fetch), and caching a miss would turn a later legitimate close into a
        # refusal.
        if is_checkout and len(_RESOLUTION_MEMO) < _MEMO_MAX:
            for token in hits:
                _RESOLUTION_MEMO[(root_key, token)] = True
    hits |= known
    return hits


def resolves_in(root: Path, token: str) -> bool:
    """True when *token* names an object that exists in the checkout *root*."""
    return token in _batch_check(root, [token])


def unresolved_revisions(roots: list[Path], tokens: list[str]) -> set[str]:
    """The tokens no checkout in *roots* holds."""
    pending = list(tokens)
    resolved: set[str] = set()
    for root in roots:
        if not pending:
            break
        hits = _batch_check(root, pending)
        resolved |= hits
        pending = [token for token in pending if token not in hits]
    return {token for token in tokens if token not in resolved}


def audit_completion(
    *,
    result: Optional[str] = None,
    summary: Optional[str] = None,
    metadata: Any = None,
    workspace_kind: Optional[str] = None,
    workspace: Optional[Path] = None,
) -> list[dict]:
    """Findings for a completion: empty means every declared deliverable is
    retrievable from outside the producing workspace."""
    prose = "\n".join(part for part in (summary, result) if part)
    findings: list[dict] = []

    revisions = revision_claims(metadata, prose)
    if revisions:
        roots = repo_roots(workspace)
        searched = [str(root) for root in roots]
        unresolved = unresolved_revisions(roots, revisions)
        for token in revisions:
            if token not in unresolved:
                continue
            findings.append({
                "claim_kind": "revision",
                "claim": token,
                "problem": "resolves in no checkout this host can reach",
                "searched": searched,
            })

    for raw in artifact_claims(metadata):
        problem = artifact_problem(raw, workspace_kind=workspace_kind, workspace=workspace)
        if problem:
            findings.append({"claim_kind": "artifact", "claim": raw, "problem": problem})

    return findings


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


class UnretrievableDeliverableError(ValueError):
    """``complete_task`` refused: the completion declares a deliverable that is not
    retrievable from outside the producing workspace (``.findings``). A
    ``ValueError`` so tool error handlers treat it as recoverable -- the worker can
    push the commit, attach the file, or fix the declared path and retry."""

    def __init__(self, task_id: str, findings: list[dict]):
        self.task_id = task_id
        self.findings = [dict(finding) for finding in findings]
        details = "; ".join(
            f"{finding['claim_kind']} {finding['claim']!r}: {finding['problem']}"
            for finding in self.findings
        )
        super().__init__(
            f"completion blocked: {task_id} declares {len(self.findings)} deliverable(s) that are "
            f"not retrievable from outside the producing workspace -- {details}. "
            "Push the commit (or add its checkout to HERMES_KANBAN_REPO_ROOTS), attach the file to "
            "the card, or declare a durable path; then retry. An operator can override with "
            f"`hermes kanban complete --force {task_id}`."
        )


def gate_completion(
    conn,
    task_id: str,
    *,
    result: Optional[str] = None,
    summary: Optional[str] = None,
    metadata: Any = None,
    force: bool = False,
) -> list[dict]:
    """Refuse a close whose declared deliverable cannot be retrieved.

    Runs before the closing transaction: a refusal leaves the card exactly where it
    was, records an auditable event, and raises
    :class:`UnretrievableDeliverableError`. ``force`` is the operator's explicit
    override (``hermes kanban complete --force``); it records
    ``deliverable_gate_forced`` and returns the findings without refusing.
    """
    row = conn.execute(
        "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    workspace: Optional[Path] = None
    workspace_kind: Optional[str] = None
    if row is not None:
        workspace_kind = row["workspace_kind"]
        if row["workspace_path"]:
            workspace = Path(row["workspace_path"]).expanduser()
    findings = audit_completion(
        result=result, summary=summary, metadata=metadata,
        workspace_kind=workspace_kind, workspace=workspace,
    )
    if not findings:
        return []
    from hermes_cli import kanban_db as _kb

    if force:
        with _kb.write_txn(conn):
            _kb._append_event(conn, task_id, "deliverable_gate_forced", {"findings": findings})
        return findings
    with _kb.write_txn(conn):
        _kb._append_event(conn, task_id, "completion_blocked_unretrievable_deliverable",
                          {"findings": findings})
    raise UnretrievableDeliverableError(task_id, findings)
