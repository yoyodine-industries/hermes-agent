"""Deterministic scope gate for kanban triage cards.

A card declares its scope as a FILE COUNT with explicit exclusions:

    SCOPE: 98 files under ~/.hermes/skills, excluding .git/** and .curator_ledger.jsonl

The gate returns a deterministic verdict - no LLM judgement - so an oversized
card is refused at grooming instead of being dispatched whole (the failure mode
that produced a "20507 occurrences" card whose real scope was 105 files).

The check counts REAL files on disk under the declared path (honouring the
exclusions) when that path resolves on this host; it falls back to the declared
count only when the path cannot be read (future path, another host). The real
count is the ground truth - a declared count is a claim, not a fact.

Verdicts (status):

    ok            scope declared and within threshold
    oversize      scope declared and over threshold  -> must be split
    no_scope      no SCOPE line                      -> flag; cannot size
    unparseable   SCOPE line present but unreadable  -> flag; cannot size

Exit codes for a CLI caller separate the three classes the SDLC standard
requires: 0 = clean (ok), 1 = finding (oversize/no_scope/unparseable),
2 = unverified (could not read the target path). This module is pure: no DB,
no network, no LLM.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

# A SCOPE line: "SCOPE:" at line start (optional indent), capture the rest.
_SCOPE_LINE_RE = re.compile(r"^\s*SCOPE\s*:\s*(?P<body>.+?)\s*$", re.MULTILINE | re.IGNORECASE)

# "N files under <path>" then an optional exclusion clause.
_DECL_RE = re.compile(
    r"^\s*(?P<count>\d+)\s+files?\s+under\s+(?P<path>.+?)"
    r"(?:\s*,\s*excluding\s+(?P<excl>.+)|(?:\s+excluding\s+(?P<excl2>.+)))?\s*$",
    re.IGNORECASE,
)

# Split an exclusion clause on commas and " and ".
_EXCL_SEP_RE = re.compile(r"\s*(?:,|\band\b)\s*", re.IGNORECASE)


class ScopeParseError(ValueError):
    """A SCOPE line was present but did not match the declared grammar."""


@dataclass
class ScopeDecl:
    """A parsed SCOPE declaration."""

    count: int
    path: str
    exclusions: list[str] = field(default_factory=list)
    raw: str = ""


@dataclass
class ScopeVerdict:
    """The deterministic result of gating a card body."""

    status: str  # ok | oversize | no_scope | unparseable
    declared: Optional[int] = None
    actual: Optional[int] = None
    effective: Optional[int] = None
    path: Optional[str] = None
    exclusions: list[str] = field(default_factory=list)
    threshold: int = 30
    reason: str = ""

    @property
    def pass_(self) -> bool:
        return self.status == "ok"

    def exit_code(self) -> int:
        if self.status == "ok":
            return 0
        if self.status == "unparseable" or self.status == "no_scope":
            return 1
        if self.status == "oversize":
            return 1
        # Unverified: path missing, so only the declared count could be read.
        if self.status == "ok" and self.actual is None:
            return 2
        return 1

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "declared": self.declared,
            "actual": self.actual,
            "effective": self.effective,
            "path": self.path,
            "exclusions": self.exclusions,
            "threshold": self.threshold,
            "reason": self.reason,
        }


def find_scope_line(body: str) -> Optional[str]:
    """Return the raw text after the SCOPE: marker, or None if absent."""
    if not body:
        return None
    m = _SCOPE_LINE_RE.search(body)
    return m.group("body") if m else None


def parse_scope(body: str) -> Optional[ScopeDecl]:
    """Parse a card body into a ScopeDecl.

    Returns None when there is no SCOPE line, and raises ScopeParseError when a
    SCOPE line is present but unreadable.
    """
    raw = find_scope_line(body)
    if raw is None:
        return None
    m = _DECL_RE.match(raw)
    if not m:
        raise ScopeParseError(f"unparseable SCOPE line: {raw!r}")
    count = int(m.group("count"))
    path = m.group("path").strip()
    excl = m.group("excl") or m.group("excl2") or ""
    exclusions = [e.strip() for e in _EXCL_SEP_RE.split(excl) if e.strip()]
    return ScopeDecl(count=count, path=path, exclusions=exclusions, raw=raw)


# ---------------------------------------------------------------------------
# File counting
# ---------------------------------------------------------------------------

def _normalize_glob(glob: str) -> str:
    return glob.strip().rstrip("/")


def _is_excluded(rel: str, basename: str, exclusions: list[str]) -> bool:
    """True if rel (posix, relative to scope root) matches any exclusion."""
    for raw in exclusions:
        g = _normalize_glob(raw)
        if not g:
            continue
        # Directory-style globs: match the dir itself and everything beneath.
        if g.endswith("/**"):
            prefix = g[: -len("/**")]
            if rel == prefix or rel.startswith(prefix + "/") or basename == prefix:
                return True
            # also allow fnmatch to catch e.g. nested explicit names
            if fnmatch.fnmatch(rel, g):
                return True
            continue
        if g.endswith("/"):
            prefix = g[: -len("/")]
            if rel == prefix or rel.startswith(prefix + "/") or basename == prefix:
                return True
            continue
        if fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(basename, g):
            return True
    return False


def count_files(path: str, exclusions: list[str]) -> int:
    """Count regular files under path, honouring exclusions.

    A single file path counts as 1 (or 0 if excluded). A directory is walked
    recursively. Symlinks are not followed (avoids cycles). Returns -1 when the
    path does not exist on this host.
    """
    p = Path(os.path.expandvars(os.path.expanduser(path)))
    if not p.exists():
        return -1
    if p.is_file():
        rel = p.name
        return 0 if _is_excluded(rel, p.name, exclusions) else 1
    root = p
    total = 0
    for entry in root.rglob("*"):
        if entry.is_symlink():
            continue
        try:
            rel = entry.relative_to(root).as_posix()
        except ValueError:
            rel = entry.as_posix()
        if _is_excluded(rel, entry.name, exclusions):
            continue
        if entry.is_file():
            total += 1
    return total


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

DEFAULT_THRESHOLD = 30


def scope_verdict(body: str, threshold: int = DEFAULT_THRESHOLD) -> ScopeVerdict:
    """Run the deterministic scope gate on a card body."""
    v = ScopeVerdict(status="", threshold=threshold)
    try:
        decl = parse_scope(body)
    except ScopeParseError as e:
        v.status = "unparseable"
        v.reason = str(e)
        return v

    if decl is None:
        v.status = "no_scope"
        v.reason = "no SCOPE line (declare scope as 'SCOPE: N files under <path>[, excluding ...]')"
        return v

    v.declared = decl.count
    v.path = decl.path
    v.exclusions = decl.exclusions

    actual = count_files(decl.path, decl.exclusions)
    v.actual = actual if actual >= 0 else None
    effective = actual if actual >= 0 else decl.count
    v.effective = effective

    if effective > threshold:
        v.status = "oversize"
        v.reason = (
            f"scope {effective} files exceeds one-turn threshold {threshold}"
            f" (declared {decl.count})"
        )
    else:
        v.status = "ok"
        v.reason = f"scope {effective} files within threshold {threshold}"
    return v


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Deterministic kanban scope gate.")
    ap.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    ap.add_argument("body", help="card body, or @path to read a file")
    args = ap.parse_args(argv)

    body = args.body
    if body.startswith("@"):
        body = Path(body[1:]).read_text(encoding="utf-8")

    v = scope_verdict(body, threshold=args.threshold)
    import json

    print(json.dumps(v.to_dict(), indent=2))
    return v.exit_code()


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
